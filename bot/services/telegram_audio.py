"""Telegram audio download and validation helpers."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from telegram import Voice
from telegram.ext import ExtBot

from bot.config import Settings
from bot.utils.logging import get_logger

logger = get_logger(__name__)

# Telegram voice notes are OGG/Opus. Architecture leaves room for future audio docs.
SUPPORTED_VOICE_MIME = "audio/ogg"
SUPPORTED_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".webm"}


class AudioValidationError(ValueError):
    """Raised when an incoming audio file is invalid."""


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    mime_type: str
    file_unique_id: str
    file_size: int
    duration: int | None = None


class TelegramAudioService:
    """Download and validate Telegram voice (and future audio) messages."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.temp_audio_dir.mkdir(parents=True, exist_ok=True)

    async def download_voice(self, bot: ExtBot, voice: Voice, telegram_user_id: int) -> DownloadedAudio:
        if voice.file_size is not None and voice.file_size > self.settings.max_audio_bytes:
            raise AudioValidationError(
                f"Audio exceeds max size of {self.settings.max_audio_bytes} bytes."
            )

        mime_type = voice.mime_type or SUPPORTED_VOICE_MIME
        if not mime_type.startswith("audio/"):
            raise AudioValidationError(f"Unsupported MIME type: {mime_type}")

        safe_name = f"voice_{telegram_user_id}_{uuid.uuid4().hex}.ogg"
        destination = self.settings.temp_audio_dir / safe_name

        try:
            telegram_file = await bot.get_file(voice.file_id)
            await telegram_file.download_to_drive(custom_path=str(destination))
        except Exception as exc:  # noqa: BLE001
            destination.unlink(missing_ok=True)
            raise AudioValidationError("Failed to download Telegram audio.") from exc

        size = destination.stat().st_size
        if size <= 0:
            destination.unlink(missing_ok=True)
            raise AudioValidationError("Downloaded audio file is empty.")
        if size > self.settings.max_audio_bytes:
            destination.unlink(missing_ok=True)
            raise AudioValidationError("Downloaded audio exceeds size limit.")

        logger.info(
            "Downloaded voice for user=%s size=%s duration=%s",
            telegram_user_id,
            size,
            voice.duration,
        )
        return DownloadedAudio(
            path=destination,
            mime_type=mime_type,
            file_unique_id=voice.file_unique_id,
            file_size=size,
            duration=voice.duration,
        )

    async def cleanup(self, path: Path) -> None:
        await asyncio.to_thread(self._safe_unlink, path)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to delete temporary audio file %s", path)
