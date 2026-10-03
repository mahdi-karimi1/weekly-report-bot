"""Telegram bot application bootstrap (long polling)."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot.config import Settings, get_settings
from bot.handlers.confirmation import confirmation_callback, suggestion_callback
from bot.handlers.dashboard import dashboard_callback, dashboard_text_handler
from bot.handlers.onboarding import onboarding_conversation
from bot.handlers.voice import report_conversation
from bot.handlers.user_management import (
    access_control,
    unverify_command,
    users_command,
    verify_command,
)
from bot.models.database import init_database
from bot.services.avalai_client import AvalAIClient
from bot.services.excel_generator import ExcelGenerator
from bot.services.report_extractor import ReportExtractor
from bot.services.telegram_audio import TelegramAudioService
from bot.services.transcription import TranscriptionService
from bot.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


def build_services(settings: Settings) -> dict[str, Any]:
    settings.ensure_runtime_dirs()

    if not settings.excel_template_path.exists():
        raise FileNotFoundError(
            f"Excel template missing at {settings.excel_template_path}. "
            "Copy FRN..Work-Report.xlsx to templates/weekly_report.xlsx "
            "(never edit the desktop original in place)."
        )
    if not settings.excel_mapping_path.exists():
        raise FileNotFoundError(
            f"Excel mapping missing at {settings.excel_mapping_path}."
        )

    avalai = AvalAIClient(settings)
    return {
        "avalai": avalai,
        "telegram_audio": TelegramAudioService(settings),
        "transcription": TranscriptionService(settings, avalai),
        "extractor": ReportExtractor(settings, avalai),
        "excel": ExcelGenerator(
            template_path=settings.excel_template_path,
            mapping_path=settings.excel_mapping_path,
            output_dir=settings.output_dir,
        ),
    }


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error: %s", context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "خطای داخلی رخ داد. لطفاً کمی بعد دوباره تلاش کنید."
            )
        except Exception:  # noqa: BLE001
            pass


async def _post_shutdown(application: Application) -> None:
    logger.info("Shutting down application gracefully…")
    try:
        from bot.models import database as db_module

        if db_module._db is not None:
            db_module._db.engine.dispose()
            db_module._db = None
    except Exception:  # noqa: BLE001
        logger.warning("Database dispose during shutdown failed", exc_info=True)


def create_application(settings: Settings | None = None) -> Application:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    token = settings.require_telegram_token()
    settings.require_admin_ids()

    init_database(settings.database_url)
    services = build_services(settings)

    application = (
        Application.builder()
        .token(token)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["services"] = services

    application.add_handler(TypeHandler(Update, access_control), group=-1)
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("unverify", unverify_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(onboarding_conversation())
    application.add_handler(report_conversation())
    application.add_handler(
        CallbackQueryHandler(dashboard_callback, pattern=r"^dashboard:")
    )
    application.add_handler(
        CallbackQueryHandler(suggestion_callback, pattern=r"^sugg:")
    )
    application.add_handler(
        CallbackQueryHandler(
            confirmation_callback,
            pattern=r"^(confirm|edit|cancel|back):\d+$",
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, dashboard_text_handler)
    )
    application.add_error_handler(_error_handler)
    return application


def ensure_event_loop() -> tuple[asyncio.AbstractEventLoop, bool]:
    """Ensure the current thread has a usable event loop.

    Python 3.10+ (notably 3.14) no longer implicitly creates a loop in the
    main thread, so ``Application.run_polling`` can raise
    ``RuntimeError: There is no current event loop``. This helper creates and
    installs one when needed without nesting loops.

    Returns:
        (loop, created) where ``created`` is True when this function allocated
        the loop and the caller owns cleanup.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("current event loop is closed")
        return loop, False
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop, True


def run_polling() -> None:
    """Process entry: blocking long polling with graceful SIGINT/SIGTERM handling."""
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info(
        "Starting weekly report bot (model=%s, openai_base=%s, native_base=%s)",
        settings.model,
        settings.avalai_openai_base_url,
        settings.avalai_base_url,
    )

    # Python 3.14: install a MainThread event loop before PTB touches asyncio.
    loop, loop_created = ensure_event_loop()
    application = create_application(settings)
    try:
        # close_loop=False: we own shutdown of any loop we created above.
        application.run_polling(drop_pending_updates=True, close_loop=False)
    finally:
        if loop_created:
            if not loop.is_closed():
                loop.close()
            # Drop the closed loop from the thread so it is not reused.
            try:
                asyncio.set_event_loop(None)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
