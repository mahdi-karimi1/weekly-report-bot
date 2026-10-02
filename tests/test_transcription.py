"""Transcription service tests with mocked AvalAI (no network)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.services.avalai_client import AvalAIError, AvalAIUnsupportedEndpointError
from bot.services.transcription import TranscriptionError, TranscriptionService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        AVALAI_API_KEY="k",
        TEMP_AUDIO_DIR=str(tmp_path),
        ENABLE_FALLBACK_TRANSCRIPTION=False,
    )


@pytest.mark.asyncio
async def test_transcribe_success(settings: Settings, tmp_path: Path) -> None:
    audio = tmp_path / "sample.ogg"
    audio.write_bytes(b"fake-ogg-bytes")
    avalai = MagicMock()
    avalai.generate_content_with_audio = AsyncMock(return_value="سلام این یک گزارش است")
    service = TranscriptionService(settings, avalai)
    text = await service.transcribe(audio, mime_type="audio/ogg")
    assert "گزارش" in text
    avalai.generate_content_with_audio.assert_awaited()


@pytest.mark.asyncio
async def test_transcribe_rejects_audio_missing_reply(
    settings: Settings, tmp_path: Path
) -> None:
    audio = tmp_path / "sample.ogg"
    audio.write_bytes(b"fake-ogg-bytes")
    avalai = MagicMock()
    avalai.generate_content_with_audio = AsyncMock(
        return_value="[فایل صوتی دریافت نشد. لطفاً فایل صوتی خود را ارسال کنید.]"
    )
    service = TranscriptionService(settings, avalai)
    with pytest.raises(TranscriptionError):
        await service.transcribe(audio, mime_type="audio/ogg")


@pytest.mark.asyncio
async def test_fallback_disabled_raises(settings: Settings, tmp_path: Path) -> None:
    audio = tmp_path / "sample.ogg"
    audio.write_bytes(b"fake-ogg-bytes")
    avalai = MagicMock()
    avalai.generate_content_with_audio = AsyncMock(
        side_effect=AvalAIUnsupportedEndpointError("audio unsupported")
    )
    service = TranscriptionService(settings, avalai)
    with pytest.raises(TranscriptionError) as exc:
        await service.transcribe(audio)
    assert "fallback is disabled" in str(exc.value).lower() or "disabled" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_all_gemini_failures_raise_when_fallback_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    audio = tmp_path / "sample.ogg"
    audio.write_bytes(b"fake-ogg-bytes")
    avalai = MagicMock()
    avalai.generate_content_with_audio = AsyncMock(side_effect=AvalAIError("boom"))
    service = TranscriptionService(settings, avalai)
    with pytest.raises(TranscriptionError):
        await service.transcribe(audio, mime_type="audio/ogg")
