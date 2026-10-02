"""Connectivity probe evaluation tests (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.services.avalai_client import (
    PROBE_EXPECTED_TOKEN,
    AvalAIClient,
    AvalAIProviderNoticeError,
    evaluate_native_genai_probe,
    evaluate_openai_chat_probe,
    is_non_transcript_response,
    is_provider_notice,
    sanitize_error_message,
)


def _openai_response(
    *,
    content: str | None,
    finish_reason: str = "stop",
    completion_tokens: int | None = 8,
    reasoning_tokens: int | None = None,
    model: str | None = "gemini-3.1-pro-preview",
) -> SimpleNamespace:
    details = None
    if reasoning_tokens is not None:
        details = SimpleNamespace(reasoning_tokens=reasoning_tokens)
    usage = SimpleNamespace(
        completion_tokens=completion_tokens,
        prompt_tokens=5,
        completion_tokens_details=details,
    )
    message = SimpleNamespace(content=content, refusal=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


def test_evaluate_openai_success_with_expected_token() -> None:
    evaluation = evaluate_openai_chat_probe(
        _openai_response(content=PROBE_EXPECTED_TOKEN),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is True
    assert evaluation["diagnostics"]["instruction_followed"] is True
    assert evaluation["diagnostics"]["requested_model"] == "gemini-3.1-pro-preview"


def test_evaluate_openai_provider_notice_is_failure() -> None:
    notice = (
        "Gemini 3 Pro is no longer available. Please switch to Gemini 3.1 Pro Preview."
    )
    evaluation = evaluate_openai_chat_probe(
        _openai_response(content=notice, model="gemini-3-pro-preview"),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is False
    assert evaluation["diagnostics"]["is_provider_notice"] is True
    assert evaluation["diagnostics"]["response_model"] == "gemini-3-pro-preview"
    assert "provider notice" in evaluation["reason"]


def test_evaluate_openai_non_matching_content_is_failure() -> None:
    evaluation = evaluate_openai_chat_probe(
        _openai_response(content="OK"),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is False
    assert evaluation["diagnostics"]["instruction_followed"] is False
    assert "expected probe token" in evaluation["reason"]


def test_evaluate_openai_empty_content_is_failure_not_exception() -> None:
    evaluation = evaluate_openai_chat_probe(
        _openai_response(
            content="",
            finish_reason="length",
            completion_tokens=8,
            reasoning_tokens=8,
        ),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is False
    assert "message.content was empty" in evaluation["reason"]


def test_evaluate_openai_no_choices() -> None:
    evaluation = evaluate_openai_chat_probe(
        SimpleNamespace(choices=[], usage=None, model=None),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is False
    assert "no choices" in evaluation["reason"]


def test_evaluate_native_genai_success() -> None:
    evaluation = evaluate_native_genai_probe(
        SimpleNamespace(text=f" {PROBE_EXPECTED_TOKEN} "),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is True


def test_evaluate_native_genai_empty() -> None:
    evaluation = evaluate_native_genai_probe(
        SimpleNamespace(text="", candidates=[]),
        requested_model="gemini-3.1-pro-preview",
    )
    assert evaluation["ok"] is False


def test_is_provider_notice() -> None:
    assert is_provider_notice(
        "Gemini 3 Pro is no longer available. Please switch to Gemini 3.1 Pro"
    )
    assert not is_provider_notice(PROBE_EXPECTED_TOKEN)


def test_sanitize_error_message_redacts_secrets() -> None:
    raw = "Authorization: Bearer sk-secretvalue123 api_key=supersecret token=abc"
    cleaned = sanitize_error_message(raw)
    assert "sk-secretvalue123" not in cleaned
    assert "supersecret" not in cleaned
    assert "***" in cleaned


@pytest.mark.asyncio
async def test_connectivity_probe_uses_requested_model_and_token_prompt() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="t", AVALAI_API_KEY="k")
    client = AvalAIClient(settings)

    mock_response = _openai_response(content=PROBE_EXPECTED_TOKEN, completion_tokens=20)
    openai_create = MagicMock(return_value=mock_response)
    client._openai = MagicMock()
    client._openai.chat.completions.create = openai_create

    genai_response = SimpleNamespace(text=PROBE_EXPECTED_TOKEN)
    genai_client = MagicMock()
    genai_client.models.generate_content.return_value = genai_response
    client._genai = genai_client

    result = await client.connectivity_probe()
    assert result["openai_compatible"] is True
    assert result["native_genai"] is True
    assert result["requested_model"] == "gemini-3.1-pro-preview"
    kwargs = openai_create.call_args.kwargs
    assert kwargs["model"] == "gemini-3.1-pro-preview"
    assert PROBE_EXPECTED_TOKEN in kwargs["messages"][0]["content"]
    assert kwargs["max_tokens"] == 512


@pytest.mark.asyncio
async def test_connectivity_probe_rejects_provider_notice() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="t", AVALAI_API_KEY="k")
    client = AvalAIClient(settings)

    notice = "Gemini 3 Pro is no longer available. Please switch to Gemini 3.1 Pro."
    client._openai = MagicMock()
    client._openai.chat.completions.create = MagicMock(
        return_value=_openai_response(content=notice, model="gemini-3-pro-preview")
    )

    genai_client = MagicMock()
    genai_client.models.generate_content.return_value = SimpleNamespace(
        text=PROBE_EXPECTED_TOKEN
    )
    client._genai = genai_client

    result = await client.connectivity_probe()
    assert result["openai_compatible"] is False
    assert result["native_genai"] is True
    assert result["structured_text_recommendation"] == "native_genai"
    assert result["diagnostics"]["openai_compatible"]["is_provider_notice"] is True


@pytest.mark.asyncio
async def test_chat_completion_prefer_native_order() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="t",
        AVALAI_API_KEY="k",
        TEXT_GENERATION_BACKEND="auto",
    )
    client = AvalAIClient(settings)
    client._native_generate_text = AsyncMock(return_value='{"ok": true}')  # type: ignore[method-assign]
    client._openai_chat_completion_text = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("openai should not be called first")
    )
    text = await client.chat_completion_text(
        system_prompt="sys",
        user_prompt="user",
        prefer_native=True,
    )
    assert text == '{"ok": true}'
    client._native_generate_text.assert_awaited_once()


def test_non_transcript_detection_persian_audio_missing() -> None:
    assert is_non_transcript_response(
        "[فایل صوتی دریافت نشد. لطفاً فایل صوتی خود را ارسال کنید.]"
    )
    assert is_non_transcript_response("لطفاً فایل صوتی خود را ارسال کنید.")
    assert not is_non_transcript_response(
        "این هفته روی پروژه پورتال سه ساعت کار کردم و فردا تست می‌گیرم."
    )


@pytest.mark.asyncio
async def test_generate_content_with_audio_tries_next_surface_on_fake_reply() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="t", AVALAI_API_KEY="k")
    client = AvalAIClient(settings)
    client._openai_transcribe_audio_file = MagicMock(  # type: ignore[method-assign]
        return_value="[فایل صوتی دریافت نشد. لطفاً فایل صوتی خود را ارسال کنید.]"
    )
    client._native_transcribe_audio_inline_b64 = MagicMock(  # type: ignore[method-assign]
        return_value="این هفته جلسه هماهنگی داشتم"
    )
    client._native_transcribe_audio_from_bytes = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should stop after second surface")
    )
    text = await client.generate_content_with_audio(
        prompt="transcribe",
        audio_bytes=b"ogg-bytes",
        mime_type="audio/ogg",
    )
    assert "جلسه" in text
    client._openai_transcribe_audio_file.assert_called_once()
    client._native_transcribe_audio_inline_b64.assert_called_once()
