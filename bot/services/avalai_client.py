"""AvalAI client wrappers.

Endpoint separation (verified against AvalAI docs):

- Native Gemini / Google GenAI SDK:
  base URL = https://api.avalai.ir  (NO /v1)
  path style = /v1beta/models/{model}:generateContent
  Used for multimodal audio understanding / transcription, and as a
  structured-text fallback when the OpenAI-compatible surface is unreliable.

- OpenAI-compatible SDK:
  base URL = https://api.avalai.ir/v1
  Used for text chat completions and JSON structured extraction when it
  actually serves the configured model (not a provider notice).

Do not assume these surfaces have identical capabilities.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot.config import Settings
from bot.utils.logging import get_logger

logger = get_logger(__name__)

# Deterministic probe token — must appear in a successful instruction-following reply.
PROBE_EXPECTED_TOKEN = "AVALAI-PROBE-7F3A"
PROBE_USER_PROMPT = (
    f"Reply with exactly this token and nothing else: {PROBE_EXPECTED_TOKEN}"
)

PROVIDER_NOTICE_PATTERNS = (
    r"no longer available",
    r"please switch to",
    r"has been discontinued",
    r"is deprecated",
    r"gemini\s*3\s*pro\s+is\s+no\s+longer",
    r"migrate to gemini",
)

# Model replies that mean audio never reached the model (seen in production DB).
NON_TRANSCRIPT_PATTERNS = (
    r"فایل\s*صوتی\s*دریافت\s*نشد",
    r"لطفاً?\s*فایل\s*صوتی",
    r"صوتی\s*خود\s*را\s*ارسال",
    r"audio\s*(file\s*)?(not\s*)?(received|found|missing|provided)",
    r"please\s*(send|provide|upload).{0,40}audio",
    r"no\s+audio\s+(file|was|provided|attached)",
    r"i\s+(did\s+not|didn't|cannot|can't)\s+(receive|get|find).{0,30}audio",
    r"send\s+(me\s+)?(an?\s+)?audio",
)

TextBackend = Literal["openai", "native", "auto"]


class AvalAIError(Exception):
    """Base AvalAI integration error."""


class AvalAIUnsupportedEndpointError(AvalAIError):
    """Raised when an endpoint or capability is unavailable."""


class AvalAITransientError(AvalAIError):
    """Transient network / rate-limit error eligible for retry."""


class AvalAIProviderNoticeError(AvalAIError):
    """Raised when the API returns a provider migration/deprecation notice."""


class AvalAIClient:
    """Facade over AvalAI OpenAI-compatible and native Gemini clients."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._openai: OpenAI | None = None
        self._genai = None

    @property
    def openai_client(self) -> OpenAI:
        if self._openai is None:
            api_key = self.settings.require_avalai_key()
            self._openai = OpenAI(
                api_key=api_key,
                base_url=self.settings.avalai_openai_base_url,
                timeout=self.settings.api_timeout_seconds,
                max_retries=0,  # retries handled explicitly
            )
        return self._openai

    def get_genai_client(self) -> Any:
        """Lazy Google GenAI client pointed at AvalAI native base URL."""
        if self._genai is not None:
            return self._genai
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency declared
            raise AvalAIError(
                "google-genai is required for native Gemini audio processing."
            ) from exc

        api_key = self.settings.require_avalai_key()
        # AvalAI docs: base_url must be https://api.avalai.ir without /v1
        self._genai = genai.Client(
            api_key=api_key,
            http_options={"base_url": self.settings.avalai_base_url},
        )
        return self._genai

    async def chat_completion_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        prefer_native: bool = False,
    ) -> str:
        """Generate text for report extraction.

        Prefer the OpenAI-compatible endpoint when it returns genuine model
        output. If that surface returns a provider notice / empty content, or
        is configured to use native, fall back to native GenAI structured
        generation for the same configured model.

        When ``prefer_native`` is True (used by report extraction), try the
        native GenAI surface first because AvalAI's OpenAI-compatible route has
        been unreliable for gemini-3.1-pro-preview on some accounts.
        """
        backend = self.settings.text_generation_backend
        errors: list[str] = []

        if backend == "auto":
            order = ("native", "openai") if prefer_native else ("openai", "native")
        elif backend == "native":
            order = ("native",)
        else:
            order = ("openai",)

        for surface in order:
            try:
                if surface == "openai":
                    return await self._openai_chat_completion_text(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        response_json_schema=response_json_schema,
                        temperature=temperature,
                    )
                return await self._native_generate_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_json_schema=response_json_schema,
                    temperature=temperature,
                )
            except (AvalAIProviderNoticeError, AvalAIError) as exc:
                errors.append(f"{surface}: {sanitize_error_message(str(exc))}")
                if len(order) == 1:
                    raise
                logger.warning(
                    "%s text generation unusable for model=%s; trying next surface. reason=%s",
                    surface,
                    self.settings.model,
                    sanitize_error_message(str(exc)),
                )

        raise AvalAIError(
            "Text generation failed on available AvalAI surfaces. " + " | ".join(errors)
        )

    async def _openai_chat_completion_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> str:
        def _call() -> str:
            kwargs: dict[str, Any] = {
                "model": self.settings.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if response_json_schema is not None:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "weekly_report",
                        "strict": True,
                        "schema": _openai_compatible_schema(response_json_schema),
                    },
                }

            try:
                response = self._retrying_chat_create(**kwargs)
            except APIStatusError as exc:
                if response_json_schema is not None and exc.status_code in {400, 404, 422}:
                    logger.warning(
                        "json_schema response_format unsupported (%s); retrying json_object",
                        exc.status_code,
                    )
                    kwargs["response_format"] = {"type": "json_object"}
                    try:
                        response = self._retrying_chat_create(**kwargs)
                    except APIStatusError as inner:
                        if inner.status_code in {400, 404, 422}:
                            logger.warning(
                                "json_object response_format unsupported; retrying without format"
                            )
                            kwargs.pop("response_format", None)
                            response = self._retrying_chat_create(**kwargs)
                        else:
                            raise AvalAIError(sanitize_error_message(str(inner))) from inner
                else:
                    raise AvalAIError(sanitize_error_message(str(exc))) from exc

            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise AvalAIError(
                    "Empty completion content from AvalAI OpenAI-compatible API."
                )
            if is_provider_notice(content):
                returned_model = getattr(response, "model", None)
                raise AvalAIProviderNoticeError(
                    "OpenAI-compatible endpoint returned a provider notice instead of "
                    f"model output (requested_model={self.settings.model}, "
                    f"response_model={returned_model})."
                )
            return content

        return await asyncio.to_thread(_call)

    async def _native_generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> str:
        """Structured/text generation via native Gemini generateContent on AvalAI."""

        def _call() -> str:
            client = self.get_genai_client()
            try:
                from google.genai import types
            except ImportError as exc:  # pragma: no cover
                raise AvalAIError("google-genai types unavailable") from exc

            config_kwargs: dict[str, Any] = {"temperature": temperature}
            # Always ask for JSON when a schema is provided OR when the caller
            # omitted the schema but still wants JSON (response_json_schema=None
            # with JSON instructions in the prompt). Use mime type alone then.
            wants_json = response_json_schema is not None or "JSON" in (
                system_prompt + user_prompt
            ).upper()
            if wants_json:
                config_kwargs["response_mime_type"] = "application/json"
            if response_json_schema is not None:
                config_kwargs["response_json_schema"] = response_json_schema

            prompt = f"{system_prompt.strip()}\n\n{user_prompt.strip()}"

            @retry(
                reraise=True,
                stop=stop_after_attempt(self.settings.api_max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=20),
                retry=retry_if_exception_type(
                    (TimeoutError, ConnectionError, AvalAITransientError)
                ),
            )
            def _inner() -> str:
                try:
                    response = client.models.generate_content(
                        model=self.settings.model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                except Exception as exc:  # noqa: BLE001
                    message = str(exc).lower()
                    # If schema enforcement fails, retry once as JSON mime only.
                    if response_json_schema is not None and any(
                        token in message
                        for token in ("schema", "invalid", "400", "json")
                    ):
                        logger.warning(
                            "Native GenAI schema rejected; retrying JSON mime only"
                        )
                        retry_config = {
                            "temperature": temperature,
                            "response_mime_type": "application/json",
                        }
                        response = client.models.generate_content(
                            model=self.settings.model,
                            contents=prompt,
                            config=types.GenerateContentConfig(**retry_config),
                        )
                    elif any(
                        token in message
                        for token in ("timeout", "temporar", "rate", "429", "503", "502")
                    ):
                        raise AvalAITransientError(
                            sanitize_error_message(str(exc))
                        ) from exc
                    else:
                        raise AvalAIError(sanitize_error_message(str(exc))) from exc

                text = _extract_genai_text(response)
                if not text:
                    raise AvalAIError("Empty completion content from AvalAI native GenAI.")
                if is_provider_notice(text):
                    raise AvalAIProviderNoticeError(
                        "Native GenAI returned a provider notice instead of model output "
                        f"(requested_model={self.settings.model})."
                    )
                return text

            return _inner()

        return await asyncio.to_thread(_call)

    def _retrying_chat_create(self, **kwargs: Any) -> Any:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.api_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(
                (APITimeoutError, APIConnectionError, RateLimitError, AvalAITransientError)
            ),
        )
        def _inner() -> Any:
            try:
                return self.openai_client.chat.completions.create(**kwargs)
            except RateLimitError as exc:
                raise AvalAITransientError("AvalAI rate limit") from exc
            except (APITimeoutError, APIConnectionError):
                raise
            except APIStatusError as exc:
                if exc.status_code in {408, 429, 500, 502, 503, 504}:
                    raise AvalAITransientError(sanitize_error_message(str(exc))) from exc
                raise

        return _inner()

    async def generate_content_with_audio(
        self,
        *,
        prompt: str,
        audio_bytes: bytes,
        mime_type: str,
    ) -> str:
        """Transcribe audio with the configured Gemini model via AvalAI.

        AvalAI's documented Gemini-audio path is OpenAI-compatible chat with a
        ``type: file`` part and ``data:{mime};base64,...``. The native
        ``Part.from_bytes`` path is kept as a fallback, but in production it
        often yields "audio not received" style replies — those are treated as
        failures and the next surface is tried.
        """
        if not audio_bytes:
            raise AvalAIError("Cannot transcribe empty audio bytes.")

        errors: list[str] = []
        for label, runner in (
            ("openai_file", self._openai_transcribe_audio_file),
            ("native_inline_b64", self._native_transcribe_audio_inline_b64),
            ("native_from_bytes", self._native_transcribe_audio_from_bytes),
        ):
            try:
                text = await asyncio.to_thread(
                    runner,
                    prompt=prompt,
                    audio_bytes=audio_bytes,
                    mime_type=mime_type,
                )
                cleaned = (text or "").strip()
                if not cleaned:
                    raise AvalAIError(f"{label}: empty transcription response")
                if is_provider_notice(cleaned) or is_non_transcript_response(cleaned):
                    raise AvalAIError(
                        f"{label}: unusable transcription reply "
                        f"preview={_content_preview(cleaned)}"
                    )
                logger.info(
                    "Audio transcription succeeded via=%s chars=%s mime=%s bytes=%s",
                    label,
                    len(cleaned),
                    mime_type,
                    len(audio_bytes),
                )
                return cleaned
            except (AvalAIProviderNoticeError, AvalAIError, AvalAITransientError) as exc:
                errors.append(f"{label}: {sanitize_error_message(str(exc))}")
                logger.warning(
                    "Audio transcription surface failed surface=%s reason=%s",
                    label,
                    sanitize_error_message(str(exc)),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {sanitize_error_message(str(exc))}")
                logger.warning(
                    "Audio transcription surface crashed surface=%s reason=%s",
                    label,
                    sanitize_error_message(str(exc)),
                )

        raise AvalAIError(
            "Audio transcription failed on all AvalAI surfaces. " + " | ".join(errors)
        )

    def _openai_transcribe_audio_file(
        self,
        *,
        prompt: str,
        audio_bytes: bytes,
        mime_type: str,
    ) -> str:
        """AvalAI-documented Gemini audio path: chat.completions + file_data data URL."""
        import base64

        data_url = f"data:{mime_type};base64,{base64.b64encode(audio_bytes).decode('ascii')}"

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.api_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(
                (TimeoutError, ConnectionError, AvalAITransientError)
            ),
        )
        def _inner() -> str:
            try:
                response = self.openai_client.chat.completions.create(
                    model=self.settings.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "file",
                                    "file": {"file_data": data_url},
                                },
                            ],
                        }
                    ],
                    temperature=0,
                )
            except RateLimitError as exc:
                raise AvalAITransientError("AvalAI rate limit") from exc
            except (APITimeoutError, APIConnectionError):
                raise
            except APIStatusError as exc:
                if exc.status_code in {408, 429, 500, 502, 503, 504}:
                    raise AvalAITransientError(sanitize_error_message(str(exc))) from exc
                raise AvalAIError(sanitize_error_message(str(exc))) from exc

            choices = getattr(response, "choices", None) or []
            if not choices:
                raise AvalAIError("OpenAI audio chat returned no choices.")
            message = getattr(choices[0], "message", None)
            content = (getattr(message, "content", None) or "").strip()
            if is_provider_notice(content):
                raise AvalAIProviderNoticeError(
                    f"Provider notice in audio chat: {_content_preview(content)}"
                )
            if not content:
                raise AvalAIError("OpenAI audio chat returned empty content.")
            return content

        return _inner()

    def _native_transcribe_audio_inline_b64(
        self,
        *,
        prompt: str,
        audio_bytes: bytes,
        mime_type: str,
    ) -> str:
        """Native generateContent with explicit base64 inline_data (AvalAI-style)."""
        import base64

        client = self.get_genai_client()
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": encoded}},
                ],
            }
        ]

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.api_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(
                (TimeoutError, ConnectionError, AvalAITransientError)
            ),
        )
        def _inner() -> str:
            try:
                response = client.models.generate_content(
                    model=self.settings.model,
                    contents=contents,
                )
            except Exception as exc:  # noqa: BLE001
                raise _map_genai_audio_exception(exc) from exc

            text = _extract_genai_text(response)
            if is_provider_notice(text):
                raise AvalAIProviderNoticeError(
                    f"Provider notice in native audio: {_content_preview(text)}"
                )
            if not text:
                raise AvalAIError("Empty transcription response from AvalAI Gemini.")
            return text

        return _inner()

    def _native_transcribe_audio_from_bytes(
        self,
        *,
        prompt: str,
        audio_bytes: bytes,
        mime_type: str,
    ) -> str:
        """Native generateContent using google-genai Part.from_bytes."""
        client = self.get_genai_client()
        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise AvalAIError("google-genai types unavailable") from exc

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.api_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(
                (TimeoutError, ConnectionError, AvalAITransientError)
            ),
        )
        def _inner() -> str:
            try:
                response = client.models.generate_content(
                    model=self.settings.model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                raise _map_genai_audio_exception(exc) from exc

            text = _extract_genai_text(response)
            if is_provider_notice(text):
                raise AvalAIProviderNoticeError(
                    f"Provider notice in native audio: {_content_preview(text)}"
                )
            if not text:
                raise AvalAIError("Empty transcription response from AvalAI Gemini.")
            return text

        return _inner()

    async def connectivity_probe(self) -> dict[str, Any]:
        """Connectivity check that requires instruction-following, not HTTP 200.

        Uses a deterministic token prompt. Provider deprecation/migration notices
        are treated as failures even when non-empty. Diagnostics include the exact
        requested model id and the model id returned by the API when present.
        """
        requested_model = self.settings.model
        result: dict[str, Any] = {
            "openai_compatible": False,
            "native_genai": False,
            "model": requested_model,
            "requested_model": requested_model,
            "openai_base_url": self.settings.avalai_openai_base_url,
            "native_base_url": self.settings.avalai_base_url,
            "probe_expected_token": PROBE_EXPECTED_TOKEN,
            "text_generation_backend": self.settings.text_generation_backend,
            "errors": {},
            "diagnostics": {},
        }

        def _openai_probe() -> None:
            response = self.openai_client.chat.completions.create(
                model=requested_model,
                messages=[{"role": "user", "content": PROBE_USER_PROMPT}],
                max_tokens=512,
                temperature=0,
            )
            evaluation = evaluate_openai_chat_probe(
                response,
                requested_model=requested_model,
                expected_token=PROBE_EXPECTED_TOKEN,
            )
            result["openai_compatible"] = evaluation["ok"]
            result["diagnostics"]["openai_compatible"] = evaluation["diagnostics"]
            if not evaluation["ok"]:
                result["errors"]["openai_compatible"] = evaluation["reason"]

        def _genai_probe() -> None:
            client = self.get_genai_client()
            response = client.models.generate_content(
                model=requested_model,
                contents=PROBE_USER_PROMPT,
            )
            evaluation = evaluate_native_genai_probe(
                response,
                requested_model=requested_model,
                expected_token=PROBE_EXPECTED_TOKEN,
            )
            result["native_genai"] = evaluation["ok"]
            result["diagnostics"]["native_genai"] = evaluation["diagnostics"]
            if not evaluation["ok"]:
                result["errors"]["native_genai"] = evaluation["reason"]

        try:
            await asyncio.to_thread(_openai_probe)
        except Exception as exc:  # noqa: BLE001
            result["errors"]["openai_compatible"] = sanitize_error_message(str(exc))
            result["diagnostics"]["openai_compatible"] = {
                "exception_type": type(exc).__name__,
                "requested_model": requested_model,
            }

        try:
            await asyncio.to_thread(_genai_probe)
        except Exception as exc:  # noqa: BLE001
            result["errors"]["native_genai"] = sanitize_error_message(str(exc))
            result["diagnostics"]["native_genai"] = {
                "exception_type": type(exc).__name__,
                "requested_model": requested_model,
            }

        # Advise whether structured extraction can rely on native GenAI.
        if result["native_genai"] and not result["openai_compatible"]:
            result["structured_text_recommendation"] = "native_genai"
        elif result["openai_compatible"]:
            result["structured_text_recommendation"] = "openai_compatible"
        else:
            result["structured_text_recommendation"] = "unavailable"
        return result


def sanitize_error_message(message: str) -> str:
    """Strip secrets / credential-looking values from diagnostic strings."""
    redacted = message
    redacted = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", redacted)
    redacted = re.sub(
        r"(?i)(api[_-]?key|authorization|token|password|secret)\s*[:=]\s*['\"]?([^\s'\",;]+)",
        r"\1=***",
        redacted,
    )
    redacted = re.sub(r"(?i)sk-[A-Za-z0-9\-_]{8,}", "sk-***", redacted)
    redacted = re.sub(r"(?i)AIza[0-9A-Za-z\-_]{20,}", "AIza***", redacted)
    return redacted


def is_provider_notice(text: str) -> bool:
    """Detect provider migration/deprecation notices that are not model answers."""
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in PROVIDER_NOTICE_PATTERNS)


def is_non_transcript_response(text: str) -> bool:
    """True when the model replied about missing audio / asked for a file instead of STT."""
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if is_provider_notice(cleaned):
        return True
    lowered = cleaned.lower()
    return any(re.search(pattern, lowered) for pattern in NON_TRANSCRIPT_PATTERNS)


def _map_genai_audio_exception(exc: Exception) -> AvalAIError:
    message = str(exc).lower()
    if any(token in message for token in ("timeout", "temporar", "rate", "429", "503", "502")):
        return AvalAITransientError(sanitize_error_message(str(exc)))
    if any(
        token in message
        for token in ("unsupported", "not found", "404", "invalid mime", "audio")
    ):
        return AvalAIUnsupportedEndpointError(sanitize_error_message(str(exc)))
    return AvalAIError(sanitize_error_message(str(exc)))


def instruction_followed(content: str, expected_token: str) -> bool:
    """True when the model reply contains the expected deterministic token."""
    normalized = " ".join(content.strip().split())
    return expected_token in normalized


def _content_preview(content: str, limit: int = 120) -> str:
    preview = " ".join(content.strip().split())
    if len(preview) > limit:
        return preview[: limit - 1] + "…"
    return preview


def _extract_genai_text(response: Any) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        chunks = [p.text for p in parts if getattr(p, "text", None)]
        return "\n".join(chunks).strip()
    except Exception:  # noqa: BLE001
        return ""


def evaluate_openai_chat_probe(
    response: Any,
    *,
    requested_model: str,
    expected_token: str = PROBE_EXPECTED_TOKEN,
) -> dict[str, Any]:
    """Validate OpenAI-compatible probe responses for instruction following."""
    diagnostics: dict[str, Any] = {
        "requested_model": requested_model,
        "response_model": getattr(response, "model", None),
        "has_choices": False,
        "finish_reason": None,
        "content_length": 0,
        "content_preview": None,
        "refusal": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "prompt_tokens": None,
        "is_provider_notice": False,
        "instruction_followed": False,
        "expected_token": expected_token,
    }

    choices = getattr(response, "choices", None) or []
    diagnostics["has_choices"] = bool(choices)
    if not choices:
        return {
            "ok": False,
            "reason": "OpenAI-compatible response contained no choices.",
            "diagnostics": diagnostics,
        }

    choice = choices[0]
    diagnostics["finish_reason"] = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)
    content = ""
    if message is not None:
        content = (getattr(message, "content", None) or "").strip()
        refusal = getattr(message, "refusal", None)
        if refusal:
            diagnostics["refusal"] = str(refusal)[:200]

    diagnostics["content_length"] = len(content)
    if content:
        diagnostics["content_preview"] = _content_preview(content)

    usage = getattr(response, "usage", None)
    if usage is not None:
        diagnostics["completion_tokens"] = getattr(usage, "completion_tokens", None)
        diagnostics["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            diagnostics["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)

    if not content:
        finish = diagnostics["finish_reason"]
        hints = [
            "OpenAI-compatible HTTP call succeeded but message.content was empty."
        ]
        if finish:
            hints.append(f"finish_reason={finish}")
        if diagnostics["reasoning_tokens"]:
            hints.append(
                f"reasoning_tokens={diagnostics['reasoning_tokens']}; "
                "thinking may have consumed the output budget"
            )
        return {"ok": False, "reason": " | ".join(hints), "diagnostics": diagnostics}

    if is_provider_notice(content):
        diagnostics["is_provider_notice"] = True
        return {
            "ok": False,
            "reason": (
                "OpenAI-compatible endpoint returned a provider notice rather than "
                f"following the probe instruction (requested_model={requested_model}, "
                f"response_model={diagnostics['response_model']})."
            ),
            "diagnostics": diagnostics,
        }

    followed = instruction_followed(content, expected_token)
    diagnostics["instruction_followed"] = followed
    if not followed:
        return {
            "ok": False,
            "reason": (
                "OpenAI-compatible response did not include the expected probe token "
                f"{expected_token!r} (requested_model={requested_model}, "
                f"response_model={diagnostics['response_model']})."
            ),
            "diagnostics": diagnostics,
        }

    return {"ok": True, "reason": None, "diagnostics": diagnostics}


def evaluate_native_genai_probe(
    response: Any,
    *,
    requested_model: str,
    expected_token: str = PROBE_EXPECTED_TOKEN,
) -> dict[str, Any]:
    """Validate native GenAI probe responses for instruction following."""
    text = _extract_genai_text(response)
    model_version = getattr(response, "model_version", None) or getattr(
        response, "model", None
    )
    diagnostics: dict[str, Any] = {
        "requested_model": requested_model,
        "response_model": model_version,
        "content_length": len(text),
        "content_preview": _content_preview(text) if text else None,
        "is_provider_notice": False,
        "instruction_followed": False,
        "expected_token": expected_token,
    }

    if not text:
        return {
            "ok": False,
            "reason": "Native GenAI response contained no usable text.",
            "diagnostics": diagnostics,
        }

    if is_provider_notice(text):
        diagnostics["is_provider_notice"] = True
        return {
            "ok": False,
            "reason": (
                "Native GenAI returned a provider notice rather than following "
                f"the probe instruction (requested_model={requested_model})."
            ),
            "diagnostics": diagnostics,
        }

    followed = instruction_followed(text, expected_token)
    diagnostics["instruction_followed"] = followed
    if not followed:
        return {
            "ok": False,
            "reason": (
                "Native GenAI response did not include the expected probe token "
                f"{expected_token!r} (requested_model={requested_model})."
            ),
            "diagnostics": diagnostics,
        }

    return {"ok": True, "reason": None, "diagnostics": diagnostics}


def _openai_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic JSON schema for providers that require additionalProperties=false."""
    cloned = json.loads(json.dumps(schema))

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            node.setdefault("additionalProperties", False)
            props = node.get("properties")
            if isinstance(props, dict):
                node["required"] = list(props.keys())
                for child in props.values():
                    _walk(child)
        if "items" in node:
            _walk(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node and isinstance(node[key], list):
                for child in node[key]:
                    _walk(child)
        if "$defs" in node and isinstance(node["$defs"], dict):
            for child in node["$defs"].values():
                _walk(child)
        if "definitions" in node and isinstance(node["definitions"], dict):
            for child in node["definitions"].values():
                _walk(child)

    _walk(cloned)
    return cloned
