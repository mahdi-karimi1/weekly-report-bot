"""Confirmation / cancellation callback handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.models.database import get_database
from bot.models.report import ReportStatus, WeeklyReport
from bot.services.excel_generator import ExcelCapacityError, ExcelGenerationError
from bot.utils.calendar import format_shamsi_range
from bot.utils.formatting import format_report_for_review, report_review_keyboard
from bot.utils.logging import get_logger

logger = get_logger(__name__)


async def suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None or query.data is None:
        return

    await query.answer()
    parts = query.data.split(":")
    if len(parts) not in (3, 4) or parts[0] != "sugg":
        await query.edit_message_text("دادهٔ دکمه نامعتبر است.")
        return
    action = parts[1]
    try:
        report_id = int(parts[2])
        index = int(parts[3]) if len(parts) == 4 else None
    except ValueError:
        await query.edit_message_text("دادهٔ دکمه نامعتبر است.")
        return
    if (action in {"all", "none"} and index is not None) or (
        action in {"accept", "reject"} and index is None
    ) or action not in {"accept", "reject", "all", "none"}:
        await query.edit_message_text("دادهٔ دکمه نامعتبر است.")
        return

    db = get_database()
    report = db.get_report_for_user(report_id, update.effective_user.id)
    if report is None or report.status != ReportStatus.AWAITING_CONFIRMATION.value:
        await query.edit_message_text("گزارش در انتظار بررسی یافت نشد.")
        return
    if not report.structured_json:
        await query.edit_message_text("داده ساختاریافته گزارش موجود نیست.")
        return

    structured = WeeklyReport.model_validate_json(report.structured_json)
    if action == "none":
        structured.dismiss_suggestions()
    elif action == "all":
        structured.accept_all_suggestions()
    elif action == "accept":
        if structured.accept_suggestion(index) is None:
            await query.edit_message_text("این پیشنهاد دیگر در دسترس نیست.")
            return
    elif structured.reject_suggestion(index) is None:
        await query.edit_message_text("این پیشنهاد دیگر در دسترس نیست.")
        return

    db.update_report(report_id, structured_json=structured.model_dump_json())
    await query.edit_message_text(
        format_report_for_review(structured, report_id),
        reply_markup=report_review_keyboard(report_id, structured),
    )


async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None or query.data is None:
        return

    await query.answer()
    user = update.effective_user
    try:
        action, report_id_raw = query.data.split(":", 1)
        report_id = int(report_id_raw)
    except ValueError:
        await query.edit_message_text("دادهٔ دکمه نامعتبر است.")
        return

    db = get_database()
    report = db.get_report_for_user(report_id, user.id)
    if report is None:
        await query.edit_message_text("گزارش یافت نشد یا متعلق به شما نیست.")
        return

    if report.status != ReportStatus.AWAITING_CONFIRMATION.value:
        await query.edit_message_text(
            "این گزارش در وضعیت قابل تأیید/ویرایش نیست (احتمالاً قبلاً پردازش شده)."
        )
        return

    if action == "cancel":
        db.update_report(report_id, status=ReportStatus.CANCELLED)
        context.user_data["awaiting_correction_for"] = None
        await query.edit_message_text(
            "عملیات لغو شد. برای ثبت گزارش جدید، متن فارسی تایپ کنید یا پیام صوتی بفرستید."
        )
        return

    if action == "edit":
        context.user_data["awaiting_correction_for"] = report_id
        await query.edit_message_text(
            "✏️ چه چیزی را می‌خواهید اصلاح کنید؟\n\n"
            "مثلاً بنویسید: «جلسه با تیم مالی را از خارج برنامه به طبق برنامه منتقل کن.»\n"
            "یا: «فعالیت داشبورد را حذف کن.»\n\n"
            "برای فعالیت‌های مبهم، مشخص کنید «طبق برنامه»، «خارج از برنامه» یا «خودخواسته» هستند.\n"
            "فقط بخش‌هایی که باید تغییر کنند را بنویسید؛ بقیه اطلاعات حفظ می‌شود.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "↩️ بازگشت به گزارش",
                        callback_data=f"back:{report_id}",
                    )
                ]]
            ),
        )
        return

    if action == "back":
        if not report.structured_json:
            await query.edit_message_text("پیش‌نویس گزارش موجود نیست.")
            return
        context.user_data["awaiting_correction_for"] = None
        structured = WeeklyReport.model_validate_json(report.structured_json)
        await query.edit_message_text(
            format_report_for_review(structured, report_id),
            reply_markup=report_review_keyboard(report_id, structured),
        )
        return

    if action == "confirm":
        await _finalize_report(update, context, report_id)
        return

    await query.edit_message_text("اقدام ناشناخته.")


async def _finalize_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    report_id: int,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    db = get_database()
    report = db.get_report_for_user(report_id, user.id)
    if report is None or not report.structured_json:
        await query.edit_message_text("داده گزارش برای تولید Excel موجود نیست.")
        return

    services = context.application.bot_data["services"]
    try:
        structured = WeeklyReport.model_validate_json(report.structured_json)

        if structured.has_pending_classification():
            await query.edit_message_text(
                format_report_for_review(structured, report_id)
                + "\n\n⛔ تا وقتی فعالیت‌های مبهم طبقه‌بندی نشوند نمی‌توان فایل Excel را تولید کرد.\n"
                "لطفاً «ویرایش گزارش» را بزنید.",
                reply_markup=report_review_keyboard(report_id, structured),
            )
            return

        overflowing = structured.overflowing_categories()
        if overflowing:
            await query.edit_message_text(
                "⛔ تعداد موارد در برخی ستون‌ها از ظرفیت ۱۵ ردیف قالب بیشتر است:\n"
                + "\n".join(f"• {name}" for name in overflowing)
                + "\n\nلطفاً با ویرایش تعداد را کاهش دهید. هیچ موردی حذف خودکار نمی‌شود.",
                reply_markup=report_review_keyboard(report_id, structured),
            )
            return

        db.update_report(
            report_id,
            status=ReportStatus.CONFIRMED,
            confirmed_at=datetime.now(timezone.utc),
        )
        await query.edit_message_text("در حال تولید فایل Excel…")

        output_path = await asyncio.to_thread(
            services["excel"].generate,
            structured,
            telegram_user_id=user.id,
        )
        db.update_report(
            report_id,
            status=ReportStatus.COMPLETED,
            excel_path=str(output_path),
        )

        profile = db.get_user_by_telegram_id(user.id)
        activity_count = sum(
            len(items)
            for items in (
                structured.planned_completed,
                structured.unplanned_completed,
                structured.self_initiated,
                structured.next_week_planned,
            )
        )
        success_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📥 دریافت Excel",
                        callback_data=f"dashboard:excel:{report_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📋 مشاهده گزارش",
                        callback_data=f"dashboard:view:{report_id}",
                    ),
                    InlineKeyboardButton(
                        "🏠 صفحه اصلی",
                        callback_data="dashboard:home",
                    ),
                ],
            ]
        )
        await query.edit_message_text(
            "✅ گزارش هفتگی آماده شد\n\n"
            "گزارش شما با موفقیت تأیید و فایل Excel تولید شد.\n\n"
            f"📅 هفته: {format_shamsi_range(structured.report_date_range)}\n"
            f"👤 {profile.display_name if profile else structured.employee_name or 'کاربر'}\n"
            f"📊 {activity_count} فعالیت ثبت شد",
            reply_markup=success_keyboard,
        )
        context.user_data["awaiting_correction_for"] = None
    except ExcelCapacityError as exc:
        logger.warning("Excel capacity exceeded report=%s: %s", report_id, exc)
        db.update_report(
            report_id,
            status=ReportStatus.AWAITING_CONFIRMATION,
            error_message="excel_capacity",
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⛔ تعداد فعالیت‌ها از ظرفیت ۱۵ ردیف قالب بیشتر است.\n"
                + "\n".join(f"• {name}" for name in exc.overflowing)
                + "\nلطفاً گزارش را ویرایش و تعداد را کاهش دهید."
            ),
        )
    except ExcelGenerationError:
        logger.exception("Excel generation failed report=%s", report_id)
        db.update_report(report_id, status=ReportStatus.FAILED, error_message="excel_failed")
        await context.bot.send_message(
            chat_id=user.id,
            text="تولید فایل Excel با خطا مواجه شد. لطفاً بعداً دوباره تلاش کنید.",
        )
    except Exception:
        logger.exception("Finalize failed report=%s", report_id)
        db.update_report(report_id, status=ReportStatus.FAILED, error_message="finalize_failed")
        await context.bot.send_message(
            chat_id=user.id,
            text="خطا در نهایی‌سازی گزارش رخ داد.",
        )
