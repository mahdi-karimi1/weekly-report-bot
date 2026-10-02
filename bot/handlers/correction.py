"""Text correction handler for pending report edits."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.models.database import get_database
from bot.models.report import ReportStatus, WeeklyReport
from bot.services.report_extractor import ReportExtractionError
from bot.utils.formatting import format_report_for_review, report_review_keyboard
from bot.utils.logging import get_logger

logger = get_logger(__name__)


async def correction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None or not update.message.text:
        return

    report_id = context.user_data.get("awaiting_correction_for")
    if not report_id:
        await update.message.reply_text(
            "در حال حاضر گزارشی برای ویرایش در انتظار نیست. "
            "برای شروع، متن فارسی گزارش را تایپ کنید یا پیام صوتی بفرستید؛ "
            "برای اصلاح پیش‌نویس از دکمه «ویرایش گزارش» استفاده کنید."
        )
        return

    user = update.effective_user
    db = get_database()
    report = db.get_report_for_user(int(report_id), user.id)
    if report is None or report.status != ReportStatus.AWAITING_CONFIRMATION.value:
        context.user_data["awaiting_correction_for"] = None
        await update.message.reply_text("گزارش قابل ویرایش یافت نشد.")
        return

    if not report.structured_json:
        await update.message.reply_text("داده ساختاریافته گزارش موجود نیست.")
        return

    services = context.application.bot_data["services"]
    status = await update.message.reply_text(
        "✏️ اصلاحات دریافت شد\n\n🔄 در حال به‌روزرسانی پیش‌نویس..."
    )

    try:
        current = WeeklyReport.model_validate_json(report.structured_json)
        updated = await services["extractor"].apply_correction(
            current,
            update.message.text,
        )
        db.update_report(
            report.id,
            structured_json=updated.model_dump_json(),
            status=ReportStatus.AWAITING_CONFIRMATION,
        )
        context.user_data["awaiting_correction_for"] = None
        await status.edit_text(
            format_report_for_review(updated, report.id),
            reply_markup=report_review_keyboard(report.id, updated),
        )
        context.user_data["awaiting_correction_for"] = None
    except ReportExtractionError:
        logger.exception("Correction failed report=%s", report.id)
        await status.edit_text(
            "اعمال اصلاحات ناموفق بود. لطفاً متن اصلاحی را واضح‌تر ارسال کنید."
        )
    except Exception:
        logger.exception("Unexpected correction failure report=%s", report.id)
        await status.edit_text("خطای غیرمنتظره هنگام ویرایش رخ داد.")
