"""Persian speech-to-text via AvalAI Gemini (same configured MODEL).

Primary path: AvalAI multimodal chat / native GenAI audio (see AvalAIClient).
Optional fallback: a separately configured OpenAI-compatible transcription
provider. Disabled by default and never used silently.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from bot.config import Settings
from bot.services.avalai_client import (
    AvalAIClient,
    AvalAIError,
    AvalAIUnsupportedEndpointError,
    is_non_transcript_response,
    is_provider_notice,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)

TRANSCRIPTION_PROMPT = """\
You are a precise Persian speech-to-text engine for workplace weekly reports.

An audio file is attached to this message. Transcribe it completely in Persian.
Rules:
- The audio IS present in this request. Never claim the file is missing.
- Preserve project names, numbers, dates, percentages, and technical terms exactly.
- Do not invent words when the recording is unclear; mark unclear segments as [نامفهوم].
- Do not summarize; return the full transcript only.
- Do not add commentary, titles, speaker labels, or system messages.
- Output ONLY the spoken transcript text.
"""


class TranscriptionError(Exception):
    """Raised when transcription cannot be completed."""


class TranscriptionService:
    """Independent audio transcription service (no Telegram coupling)."""

    def __init__(self, settings: Settings, avalai: AvalAIClient) -> None:
        self.settings = settings
        self.avalai = avalai

    async def transcribe(self, audio_path: Path, mime_type: str = "audio/ogg") -> str:
        if not audio_path.exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        attempts: list[tuple[Path, str]] = []
        converted: Path | None = None

        # Telegram voice is OGG/Opus. AvalAI lists OGG Vorbis; convert to WAV when possible.
        normalized = (mime_type or "audio/ogg").split(";")[0].strip().lower()
        if normalized in {"audio/ogg", "audio/opus", "application/ogg"}:
            converted = await self._maybe_convert_to_wav(audio_path)
            if converted is not None:
                attempts.append((converted, "audio/wav"))
            attempts.append((audio_path, "audio/ogg"))
        else:
            attempts.append((audio_path, normalized or "audio/ogg"))

        last_error: Exception | None = None
        try:
            for path, attempt_mime in attempts:
                try:
                    text = await self._transcribe_with_gemini(path, attempt_mime)
                    return text
                except (AvalAIError, TranscriptionError) as exc:
                    last_error = exc
                    logger.warning(
                        "Gemini transcription attempt failed mime=%s reason=%s",
                        attempt_mime,
                        type(exc).__name__,
                    )

            # If every Gemini surface failed, optional separate fallback provider.
            cause = last_error or AvalAIError("All Gemini audio attempts failed.")
            if isinstance(cause, AvalAIUnsupportedEndpointError):
                return await self._fallback_transcribe(audio_path, cause=cause)
            return await self._fallback_transcribe(audio_path, cause=cause)
        finally:
            if converted is not None:
                converted.unlink(missing_ok=True)

    async def _transcribe_with_gemini(self, path: Path, mime_type: str) -> str:
        audio_bytes = await asyncio.to_thread(path.read_bytes)
        if not audio_bytes:
            raise TranscriptionError("Audio file is empty.")
        if len(audio_bytes) > self.settings.max_audio_bytes:
            raise TranscriptionError("Audio exceeds configured size limit.")

        logger.info(
            "Sending audio to AvalAI for STT bytes=%s mime=%s",
            len(audio_bytes),
            mime_type,
        )
        text = await self.avalai.generate_content_with_audio(
            prompt=TRANSCRIPTION_PROMPT,
            audio_bytes=audio_bytes,
            mime_type=mime_type,
        )
        cleaned = text.strip()
        if not cleaned:
            logger.warning("Transcription empty after Gemini response")
            raise TranscriptionError("Transcription returned empty text.")
        if is_provider_notice(cleaned) or is_non_transcript_response(cleaned):
            logger.warning(
                "Transcription rejected as non-transcript chars=%s",
                len(cleaned),
            )
            raise TranscriptionError(
                "Model returned a system/audio-missing reply instead of a transcript."
            )
        logger.info(
            "Transcription completed chars=%s empty=%s",
            len(cleaned),
            False,
        )
        if self.settings.log_transcriptions:
            logger.debug("Transcription preview_chars=%s", min(len(cleaned), 80))
        return cleaned

    async def _fallback_transcribe(self, audio_path: Path, *, cause: Exception) -> str:
        if not self.settings.enable_fallback_transcription:
            raise TranscriptionError(
                "Gemini audio transcription via AvalAI failed and fallback is disabled. "
                "Set ENABLE_FALLBACK_TRANSCRIPTION=true and configure "
                "FALLBACK_TRANSCRIPTION_* only if you intentionally want a separate provider. "
                f"Primary error: {cause}"
            ) from cause

        if not (
            self.settings.fallback_transcription_base_url
            and self.settings.fallback_transcription_api_key
            and self.settings.fallback_transcription_model
        ):
            raise TranscriptionError(
                "Fallback transcription enabled but FALLBACK_TRANSCRIPTION_BASE_URL / "
                "API_KEY / MODEL are not fully configured."
            ) from cause

        logger.warning(
            "Using explicitly configured fallback transcription provider (%s)",
            self.settings.fallback_transcription_model,
        )

        def _call() -> str:
            from openai import OpenAI

            client = OpenAI(
                api_key=self.settings.fallback_transcription_api_key,
                base_url=self.settings.fallback_transcription_base_url.rstrip("/"),
                timeout=self.settings.api_timeout_seconds,
            )
            with audio_path.open("rb") as handle:
                result = client.audio.transcriptions.create(
                    model=self.settings.fallback_transcription_model,
                    file=handle,
                    response_format="text",
                )
            if isinstance(result, str):
                return result.strip()
            text = getattr(result, "text", None)
            if not text:
                raise TranscriptionError("Fallback transcription returned empty text.")
            return str(text).strip()

        text = await asyncio.to_thread(_call)
        if is_non_transcript_response(text):
            raise TranscriptionError(
                "Fallback transcription also returned a non-transcript reply."
            ) from cause
        return text

    async def _maybe_convert_to_wav(self, source: Path) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.info("FFmpeg not found; skipping audio conversion.")
            return None

        target = source.with_suffix(".wav")

        def _run() -> Path:
            import subprocess

            completed = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(target),
                ],
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0 or not target.exists():
                raise TranscriptionError(
                    f"FFmpeg conversion failed: {completed.stderr.decode(errors='ignore')[:300]}"
                )
            return target

        try:
            converted = await asyncio.to_thread(_run)
            logger.info(
                "Converted Telegram voice to WAV path=%s size=%s",
                converted.name,
                converted.stat().st_size,
            )
            return converted
        except Exception as exc:  # noqa: BLE001
            logger.warning("Audio conversion failed: %s", exc)
            return None
