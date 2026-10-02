"""Report input processing for voice messages and typed text."""

from __future__ import annotations

from collections.abc import Callable

from telegram import Message, Update, Voice
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.models.database import Report, User, get_database
from bot.models.report import ReportStatus, WeeklyReport
from bot.services.report_extractor import (
    EmptyReportExtractionError,
    ReportExtractionError,
)
from bot.services.telegram_audio import AudioValidationError
from bot.services.transcription import TranscriptionError
from bot.services.avalai_client import is_non_transcript_response
from bot.utils.calendar import get_shamsi_week
from bot.utils.formatting import format_report_for_review, report_review_keyboard
from bot.utils.logging import get_logger

logger = get_logger(__name__)

EMPTY_TRANSCRIPTION_MESSAGE = (
    "متن قابل استفاده‌ای از ورودی دریافت نشد. "
    "لطفاً گزارش را واضح‌تر تایپ کنید یا یک پیام صوتی بفرستید."
)
AUDIO_NOT_REACHED_MESSAGE = (
    "سرویس هوش مصنوعی فایل صوتی را دریافت نکرد (رونویسی واقعی انجام نشد). "
    "لطفاً دوباره همان پیام صوتی را بفرستید؛ در صورت تکرار، FFmpeg را نصب کنید "
    "یا وضعیت AvalAI/مدل را بررسی کنید."
)
EMPTY_REPORT_MESSAGE = (
    "از متن گزارش هیچ فعالیت یا اطلاعات معناداری استخراج نشد. "
    "لطفاً دوباره با جزئیات بیشتر "
    "(دپارتمان/پروژه، فعالیت‌های انجام‌شده طبق برنامه یا خارج از برنامه یا خودخواسته، "
    "ساعت تقریبی در صورت وجود، برنامه هفته بعد، مشکلات و پیشنهادات) "
    "متن گزارش را تایپ کنید یا پیام صوتی بفرستید."
)


def report_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.VOICE, voice_handler)],
        states={},
        fallbacks=[CommandHandler("cancel", cancel_report_conversation)],
        allow_reentry=True,
        name="report_date_collection",
    )


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user is None or update.message is None or update.message.voice is None:
        return ConversationHandler.END

    if context.user_data.get("report_custom_date_step"):
        await update.message.reply_text(
            "ابتدا واردکردن بازه دلخواه را کامل کنید یا آن را لغو کنید."
        )
        return ConversationHandler.END

    user = update.effective_user
    db = get_database()

    if not db.is_user_verified(user.id):
        await update.message.reply_text(
            "برای ثبت گزارش ابتدا اطلاعات پرسنلی خود را تکمیل کنید. دستور /start را بزنید."
        )
        return ConversationHandler.END

    append_report_id = context.user_data.get("append_voice_report_id")
    existing_report = (
        db.get_report_for_user(int(append_report_id), user.id)
        if append_report_id is not None
        else None
    )
    if append_report_id is not None and (
        existing_report is None
        or existing_report.status
        not in {
            ReportStatus.AWAITING_CONFIRMATION.value,
            ReportStatus.COMPLETED.value,
        }
    ):
        context.user_data.pop("append_voice_report_id", None)
        await update.message.reply_text(
            "گزارش انتخاب‌شده برای افزودن ویس در دسترس نیست. از «گزارش‌های من» دوباره انتخاب کنید."
        )
        return ConversationHandler.END

    active = db.get_active_report_for_user(user.id)
    if (
        active
        and active.status == ReportStatus.AWAITING_CONFIRMATION.value
        and (existing_report is None or active.id != existing_report.id)
    ):
        await update.message.reply_text(
            "یک گزارش در انتظار تأیید دارید. ابتدا آن را تأیید، ویرایش یا لغو کنید."
        )
        return ConversationHandler.END

    context.user_data.pop("awaiting_voice", None)
    week = get_shamsi_week()
    if existing_report and existing_report.structured_json:
        existing_structured = WeeklyReport.model_validate_json(
            existing_report.structured_json
        )
        report_date_range = existing_structured.report_date_range
    else:
        report_date_range = str(
            context.user_data.pop(
                "report_date_range",
                f"{week.start_str} الی {week.end_str}",
            )
        )
    await process_voice_report(
        update,
        context,
        update.message.voice,
        report_date_range,
        existing_report_id=existing_report.id if existing_report else None,
    )
    context.user_data.pop("append_voice_report_id", None)
    return ConversationHandler.END


async def text_report_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if (
        update.effective_user is None
        or update.message is None
        or not update.message.text
        or not context.user_data.get("awaiting_voice")
    ):
        return

    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(
            "لطفاً متن گزارش را بنویسید یا یک پیام صوتی بفرستید."
        )
        return
    if context.user_data.get("report_custom_date_step"):
        return

    user = update.effective_user
    db = get_database()
    if not db.is_user_verified(user.id):
        context.user_data.pop("awaiting_voice", None)
        await update.message.reply_text(
            "برای ثبت گزارش ابتدا اطلاعات پرسنلی خود را تکمیل کنید. دستور /start را بزنید."
        )
        return

    append_report_id = context.user_data.get("append_voice_report_id")
    existing_report = (
        db.get_report_for_user(int(append_report_id), user.id)
        if append_report_id is not None
        else None
    )
    if append_report_id is not None and (
        existing_report is None
        or existing_report.status
        not in {
            ReportStatus.AWAITING_CONFIRMATION.value,
            ReportStatus.COMPLETED.value,
        }
    ):
        context.user_data.pop("append_voice_report_id", None)
        context.user_data.pop("awaiting_voice", None)
        await update.message.reply_text(
            "گزارش انتخاب‌شده برای افزودن ورودی در دسترس نیست. "
            "از «گزارش‌های من» دوباره انتخاب کنید."
        )
        return

    active = db.get_active_report_for_user(user.id)
    if (
        active
        and active.status == ReportStatus.AWAITING_CONFIRMATION.value
        and (existing_report is None or active.id != existing_report.id)
    ):
        await update.message.reply_text(
            "یک گزارش در انتظار تأیید دارید. ابتدا آن را تأیید، ویرایش یا لغو کنید."
        )
        return

    week = get_shamsi_week()
    if existing_report and existing_report.structured_json:
        report_date_range = WeeklyReport.model_validate_json(
            existing_report.structured_json
        ).report_date_range
    else:
        report_date_range = str(
            context.user_data.get(
                "report_date_range",
                f"{week.start_str} الی {week.end_str}",
            )
        )
    processed = await process_text_report(
        update,
        context,
        text,
        report_date_range,
        existing_report_id=existing_report.id if existing_report else None,
    )
    if processed:
        context.user_data.pop("awaiting_voice", None)
        context.user_data.pop("append_voice_report_id", None)


async def cancel_report_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data.pop("awaiting_voice", None)
    context.user_data.pop("report_custom_date_step", None)
    context.user_data.pop("report_custom_start_date", None)
    context.user_data.pop("report_date_range", None)
    context.user_data.pop("append_voice_report_id", None)
    if update.message is not None:
        await update.message.reply_text("ثبت گزارش لغو شد.")
    return ConversationHandler.END


async def process_voice_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    voice: Voice,
    report_date_range: str,
    *,
    existing_report_id: int | None = None,
) -> None:
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    services = context.application.bot_data["services"]
    db = get_database()
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    db_user = db.get_or_create_user(user.id, user.username, display_name or None)
    status_msg = await update.message.reply_text(
        "🎙 ویس دریافت شد\n\n"
        "⏳ در حال آماده‌سازی گزارش...\n\n"
        "🔄 دریافت و بررسی صدا\n"
        "⬜ تبدیل صدا به متن\n"
        "⬜ تحلیل فعالیت‌ها\n"
        "⬜ آماده‌سازی پیش‌نویس",
    )
    report = (
        db.get_report_for_user(existing_report_id, user.id)
        if existing_report_id is not None
        else None
    )
    if existing_report_id is not None and report is None:
        await status_msg.edit_text("گزارش برای افزودن ویس یافت نشد.")
        return
    previous_status = report.status if report is not None else None
    existing_structured = (
        WeeklyReport.model_validate_json(report.structured_json)
        if report is not None and report.structured_json
        else None
    )
    if report is None:
        report = db.create_report(db_user.id, status=ReportStatus.RECEIVED)
    else:
        db.update_report(report.id, status=ReportStatus.RECEIVED)
    audio = None

    def restore_status() -> ReportStatus:
        if previous_status == ReportStatus.COMPLETED.value:
            return ReportStatus.COMPLETED
        if previous_status == ReportStatus.AWAITING_CONFIRMATION.value:
            return ReportStatus.AWAITING_CONFIRMATION
        return ReportStatus.FAILED

    try:
        audio = await services["telegram_audio"].download_voice(
            context.bot,
            voice,
            telegram_user_id=user.id,
        )
        await status_msg.edit_text(
            "🎙 ویس دریافت شد\n\n"
            "✅ دریافت و بررسی صدا\n"
            "🔄 تبدیل صدا به متن\n"
            "⬜ تحلیل فعالیت‌ها\n"
            "⬜ آماده‌سازی پیش‌نویس"
        )
        transcription = await services["transcription"].transcribe(
            audio.path,
            mime_type=audio.mime_type,
        )
        transcription_chars = len(transcription or "")
        transcription_empty = not (transcription or "").strip()
        logger.info(
            "Voice pipeline transcription report_id=%s chars=%s empty=%s",
            report.id,
            transcription_chars,
            transcription_empty,
        )
        if transcription_empty:
            db.update_report(
                report.id,
                status=restore_status(),
                error_message="empty_transcription",
            )
            await status_msg.edit_text(EMPTY_TRANSCRIPTION_MESSAGE)
            return

        if is_non_transcript_response(transcription):
            logger.warning(
                "Voice pipeline got non-transcript reply report_id=%s chars=%s",
                report.id,
                transcription_chars,
            )
            db.update_report(
                report.id,
                status=restore_status(),
                error_message="audio_not_reached_model",
            )
            await status_msg.edit_text(AUDIO_NOT_REACHED_MESSAGE)
            return

        await _extract_and_save_report(
            update,
            context,
            transcription,
            report_date_range,
            report,
            existing_structured,
            db_user,
            status_msg,
            restore_status,
            input_label="🎙 ویس",
            transcription_status=ReportStatus.TRANSCRIBED,
        )
    except AudioValidationError:
        logger.exception("Audio validation failed for user=%s", user.id)
        db.update_report(report.id, status=restore_status(), error_message="invalid_audio")
        await status_msg.edit_text(
            "فایل صوتی نامعتبر است یا حجم آن بیش از حد مجاز است. لطفاً دوباره تلاش کنید."
        )
    except TranscriptionError as exc:
        logger.exception("Transcription failed for user=%s report=%s", user.id, report.id)
        db.update_report(report.id, status=restore_status(), error_message="transcription_failed")
        detail = str(exc).lower()
        if (
            "non-transcript" in detail
            or "audio-missing" in detail
            or "system/audio-missing" in detail
        ):
            await status_msg.edit_text(AUDIO_NOT_REACHED_MESSAGE)
        else:
            await status_msg.edit_text(
                "تبدیل گفتار به متن با خطا مواجه شد. لطفاً بعداً دوباره تلاش کنید."
            )
    except EmptyReportExtractionError:
        logger.warning("Empty extraction for user=%s report=%s", user.id, report.id)
        db.update_report(report.id, status=restore_status(), error_message="empty_extraction")
        await status_msg.edit_text(EMPTY_REPORT_MESSAGE)
    except ReportExtractionError:
        logger.exception("Extraction failed for user=%s report=%s", user.id, report.id)
        db.update_report(report.id, status=restore_status(), error_message="extraction_failed")
        await status_msg.edit_text(
            "استخراج اطلاعات گزارش با خطا مواجه شد. "
            "لطفاً دوباره متن را تایپ کنید یا پیام صوتی بفرستید."
        )
    except Exception:
        logger.exception("Unexpected voice pipeline failure user=%s", user.id)
        db.update_report(report.id, status=restore_status(), error_message="unexpected")
        await status_msg.edit_text(
            "خطای غیرمنتظره‌ای رخ داد. جزئیات برای پشتیبانی در لاگ ثبت شد."
        )
    finally:
        if audio is not None:
            await services["telegram_audio"].cleanup(audio.path)


async def process_text_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    report_date_range: str,
    *,
    existing_report_id: int | None = None,
) -> bool:
    if update.effective_user is None or update.message is None:
        return False

    user = update.effective_user
    user = update.effective_user
    db = get_database()
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    db_user = db.get_or_create_user(user.id, user.username, display_name or None)
    status_msg = await update.message.reply_text(
        "✍️ متن گزارش دریافت شد\n\n"
        "🔄 در حال تحلیل فعالیت‌ها و آماده‌سازی پیش‌نویس..."
    )
    report = (
        db.get_report_for_user(existing_report_id, user.id)
        if existing_report_id is not None
        else None
    )
    if existing_report_id is not None and report is None:
        await status_msg.edit_text("گزارش برای افزودن متن پیدا نشد.")
        return False
    previous_status = report.status if report is not None else None
    existing_structured = (
        WeeklyReport.model_validate_json(report.structured_json)
        if report is not None and report.structured_json
        else None
    )
    if report is None:
        report = db.create_report(db_user.id, status=ReportStatus.RECEIVED)
    else:
        db.update_report(report.id, status=ReportStatus.RECEIVED)

    def restore_status() -> ReportStatus:
        if previous_status == ReportStatus.COMPLETED.value:
            return ReportStatus.COMPLETED
        if previous_status == ReportStatus.AWAITING_CONFIRMATION.value:
            return ReportStatus.AWAITING_CONFIRMATION
        return ReportStatus.FAILED

    try:
        return await _extract_and_save_report(
            update,
            context,
            text.strip(),
            report_date_range,
            report,
            existing_structured,
            db_user,
            status_msg,
            restore_status,
            input_label="✍️ متن",
            transcription_status=ReportStatus.TRANSCRIBED,
        )
    except EmptyReportExtractionError:
        logger.warning("Empty extraction for user=%s report=%s", user.id, report.id)
        db.update_report(report.id, status=restore_status(), error_message="empty_extraction")
        await status_msg.edit_text(EMPTY_REPORT_MESSAGE)
        return False
    except ReportExtractionError:
        logger.exception("Extraction failed for user=%s report=%s", user.id, report.id)
        db.update_report(report.id, status=restore_status(), error_message="extraction_failed")
        await status_msg.edit_text(
            "استخراج اطلاعات گزارش با خطا مواجه شد. "
            "لطفاً دوباره متن را تایپ کنید یا پیام صوتی بفرستید."
        )
        return False
    except Exception:
        logger.exception("Unexpected text report failure user=%s", user.id)
        db.update_report(report.id, status=restore_status(), error_message="unexpected")
        await status_msg.edit_text(
            "خطای غیرمنتظره‌ای رخ داد. جزئیات برای پشتیبانی در لاگ ثبت شد."
        )
        return False


async def _extract_and_save_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    transcription: str,
    report_date_range: str,
    report: Report,
    existing_structured: WeeklyReport | None,
    db_user: User,
    status_msg: Message,
    restore_status: Callable[[], ReportStatus],
    *,
    input_label: str,
    transcription_status: ReportStatus,
) -> bool:
    user = update.effective_user
    if user is None:
        return False
    services = context.application.bot_data["services"]
    db = get_database()
    report_id = report.id
    existing_transcription = report.transcription
    db.update_report(
        report_id,
        transcription=(
            f"{existing_transcription}\n\n{transcription}"
            if existing_transcription
            else transcription
        ),
        status=transcription_status,
    )
    await status_msg.edit_text(
        f"{input_label} دریافت شد\n\n"
        "✅ دریافت ورودی\n"
        "🔄 تحلیل فعالیت‌ها\n"
        "⬜ آماده‌سازی پیش‌نویس"
    )
    if existing_structured is None:
        structured: WeeklyReport = await services["extractor"].extract(transcription)
        structured.employee_name = db_user.display_name
        structured.department_or_project = db_user.department
        structured.report_date_range = report_date_range
    else:
        structured = await services["extractor"].extract_voice_update(
            transcription,
            existing_structured,
        )

    logger.info(
        "Report extraction report_id=%s fields=%s",
        report_id,
        structured.populated_fields_summary(),
    )
    if not structured.has_meaningful_content():
        db.update_report(report_id, status=restore_status(), error_message="empty_extraction")
        await status_msg.edit_text(EMPTY_REPORT_MESSAGE)
        return False

    db.update_report(
        report_id,
        structured_json=structured.model_dump_json(),
        status=ReportStatus.AWAITING_CONFIRMATION,
    )
    await status_msg.edit_text(
        f"{input_label} دریافت شد\n\n"
        "✅ دریافت ورودی\n"
        "✅ تحلیل فعالیت‌ها\n"
        "✅ آماده‌سازی پیش‌نویس"
    )
    await status_msg.edit_text(
        format_report_for_review(structured, report_id),
        reply_markup=report_review_keyboard(report_id, structured),
    )
    context.user_data["awaiting_correction_for"] = None
    return True
