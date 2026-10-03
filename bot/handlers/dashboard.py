"""Dashboard navigation and user-facing profile/report pages."""

from __future__ import annotations

from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from bot.handlers.correction import correction_handler
from bot.handlers.voice import text_report_handler
from bot.models.database import get_database
from bot.models.report import ReportStatus, WeeklyReport
from bot.utils.calendar import (
    format_shamsi_range,
    get_shamsi_week,
    parse_shamsi_date,
)
from bot.utils.formatting import format_report_for_review, report_review_keyboard


START_REPORT = "📝 ثبت گزارش هفتگی"
MY_REPORTS = "📋 گزارش‌های من"
MY_PROFILE = "👤 پروفایل من"
HELP = "❓ راهنما"
CANCEL = "❌ انصراف"
EDIT_PROFILE = "✏️ ویرایش اطلاعات"
BACK = "← بازگشت"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[START_REPORT], [MY_REPORTS, MY_PROFILE], [HELP]],
    resize_keyboard=True,
    is_persistent=True,
)


def dashboard_text(user_name: str, report_status: str | None = None) -> str:
    week = get_shamsi_week()
    status = {
        ReportStatus.AWAITING_CONFIRMATION.value: "یک پیش‌نویس در انتظار تأیید دارید",
        ReportStatus.COMPLETED.value: "گزارش این هفته تکمیل شده است",
        ReportStatus.RECEIVED.value: "گزارش این هفته در حال پردازش است",
        ReportStatus.TRANSCRIBED.value: "گزارش این هفته در حال پردازش است",
        ReportStatus.EXTRACTED.value: "گزارش این هفته در حال پردازش است",
        ReportStatus.CONFIRMED.value: "گزارش این هفته در حال آماده‌سازی است",
    }.get(report_status or "", "هنوز گزارشی برای این هفته ثبت نشده")
    return (
        f"👋 سلام {user_name}\n\n"
        f"📅 هفته جاری\n{format_shamsi_range(week.display_range)}\n\n"
        f"📊 وضعیت گزارش\n{status}\n\n"
        "━━━━━━━━━━━━━━\n"
        "از دکمه‌های زیر برای ادامه استفاده کنید."
    )


def _current_week_status(telegram_id: int) -> str | None:
    db = get_database()
    active = db.get_active_report_for_user(telegram_id)
    if active is not None:
        return active.status
    week = get_shamsi_week()
    week_range = f"{week.start_str} الی {week.end_str}"
    for report in db.get_reports_for_user(telegram_id, limit=10):
        if report.status != ReportStatus.COMPLETED.value or not report.structured_json:
            continue
        structured = WeeklyReport.model_validate_json(report.structured_json)
        if structured.report_date_range == week_range:
            return report.status
    return None


def _profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(EDIT_PROFILE, callback_data="dashboard:edit_profile")]]
    )


def _report_prompt_keyboard(*, allow_custom_range: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if allow_custom_range:
        rows.append(
            [
                InlineKeyboardButton(
                    "📅 انتخاب بازه دیگر",
                    callback_data="dashboard:custom_range",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "❌ انصراف",
                callback_data="dashboard:cancel_report",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _find_report_for_range(telegram_id: int, report_date_range: str) -> int | None:
    for report in get_database().get_reports_for_user(telegram_id, limit=100):
        if report.status not in {
            ReportStatus.AWAITING_CONFIRMATION.value,
            ReportStatus.COMPLETED.value,
        } or not report.structured_json:
            continue
        structured = WeeklyReport.model_validate_json(report.structured_json)
        if structured.report_date_range == report_date_range:
            return report.id
    return None


def _append_report_button_label(period: str) -> str:
    return f"➕ افزودن به {period}"


def _report_prompt_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    selected_range = context.user_data.get("report_date_range")
    if not selected_range:
        week = get_shamsi_week()
        selected_range = f"{week.start_str} الی {week.end_str}"
    return (
        "📝 ثبت گزارش هفتگی\n\n"
        f"📅 بازه گزارش: {format_shamsi_range(str(selected_range))}\n"
        "برای تغییر بازه، دکمه «انتخاب بازه دیگر» را بزنید.\n\n"
        "گزارش فعالیت‌های این بازه را به صورت متن فارسی تایپ کنید یا پیام صوتی بفرستید.\n\n"
        "مثلاً این موارد را در گزارش بیاورید:\n"
        "• کارهای انجام‌شده\n"
        "• کارهای خارج از برنامه\n"
        "• مشکلات\n"
        "• برنامه هفته آینده\n\n"
        "⏱ مدت پیشنهادی ویس: ۲ تا ۵ دقیقه"
    )


async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    db = get_database()
    profile = db.get_user_by_telegram_id(user.id)
    if profile is None:
        return
    await message.reply_text(
        dashboard_text(profile.display_name or user.first_name, _current_week_status(user.id)),
        reply_markup=MAIN_KEYBOARD,
    )


async def dashboard_text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_user is None or update.message is None or not update.message.text:
        return

    text = update.message.text.strip()
    user = update.effective_user
    db = get_database()

    if text == CANCEL:
        context.user_data.pop("awaiting_voice", None)
        context.user_data.pop("report_custom_date_step", None)
        context.user_data.pop("report_custom_start_date", None)
        context.user_data.pop("report_date_range", None)
        context.user_data.pop("append_voice_report_id", None)
        context.user_data.pop("profile_edit_step", None)
        context.user_data.pop("profile_edit_name", None)
        await update.message.reply_text("عملیات لغو شد.", reply_markup=MAIN_KEYBOARD)
        return

    date_step = context.user_data.get("report_custom_date_step")
    if date_step == "start":
        start_date = parse_shamsi_date(text)
        if start_date is None:
            await update.message.reply_text(
                "تاریخ معتبر نیست. تاریخ شروع را با قالب سال/ماه/روز وارد کنید؛ "
                "مثلاً ۱۴۰۵/۰۷/۰۴."
            )
            return
        context.user_data["report_custom_start_date"] = start_date
        context.user_data["report_custom_date_step"] = "end"
        await update.message.reply_text(
            "حالا تاریخ پایان بازه را وارد کنید؛ مثلاً ۱۴۰۵/۰۷/۰۸.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "❌ انصراف",
                        callback_data="dashboard:cancel_report",
                    )
                ]]
            ),
        )
        return
    if date_step == "end":
        end_date = parse_shamsi_date(text)
        start_date = context.user_data.get("report_custom_start_date")
        if end_date is None:
            await update.message.reply_text(
                "تاریخ معتبر نیست. تاریخ پایان را با قالب سال/ماه/روز وارد کنید؛ "
                "مثلاً ۱۴۰۵/۰۷/۰۸."
            )
            return
        if start_date is None:
            context.user_data.pop("report_custom_date_step", None)
            await update.message.reply_text(
                "تاریخ شروع پیدا نشد. دوباره بازه دلخواه را انتخاب کنید."
            )
            return
        if end_date < start_date:
            await update.message.reply_text(
                "تاریخ پایان نمی‌تواند پیش از تاریخ شروع باشد. تاریخ پایان را دوباره وارد کنید:"
            )
            return
        context.user_data["report_date_range"] = (
            f"{start_date.strftime('%Y/%m/%d')} الی {end_date.strftime('%Y/%m/%d')}"
        )
        context.user_data.pop("report_custom_start_date", None)
        context.user_data.pop("report_custom_date_step", None)
        context.user_data["awaiting_voice"] = True
        matching_report_id = _find_report_for_range(
            user.id,
            str(context.user_data["report_date_range"]),
        )
        if matching_report_id is not None:
            context.user_data["append_voice_report_id"] = matching_report_id
        else:
            context.user_data.pop("append_voice_report_id", None)
        await update.message.reply_text(
            (
                "♻️ برای این بازه گزارش قبلی پیدا شد؛ ورودی جدید به همان گزارش اضافه می‌شود.\n\n"
                if matching_report_id is not None
                else ""
            )
            + _report_prompt_text(context),
            reply_markup=_report_prompt_keyboard(),
        )
        return

    profile_step = context.user_data.get("profile_edit_step")
    if profile_step and text in {START_REPORT, MY_REPORTS, MY_PROFILE, HELP}:
        context.user_data.pop("profile_edit_step", None)
        context.user_data.pop("profile_edit_name", None)
        profile_step = None
    if profile_step == "name":
        if len(text) < 3:
            await update.message.reply_text("لطفاً نام و نام خانوادگی معتبر وارد کنید.")
            return
        context.user_data["profile_edit_name"] = text
        context.user_data["profile_edit_step"] = "department"
        await update.message.reply_text("حالا نام دپارتمان یا واحد کاری را وارد کنید:")
        return
    if profile_step == "department":
        if len(text) < 2:
            await update.message.reply_text("لطفاً نام دپارتمان را وارد کنید.")
            return
        profile = db.get_user_by_telegram_id(user.id)
        name = context.user_data.pop("profile_edit_name", None)
        context.user_data.pop("profile_edit_step", None)
        if profile is None or not name:
            await update.message.reply_text("ویرایش اطلاعات کامل نشد. از /start دوباره آغاز کنید.")
            return
        db.complete_onboarding(
            user.id,
            full_name=str(name),
            department=text,
            username=user.username,
        )
        await update.message.reply_text(
            "✅ اطلاعات پروفایل به‌روزرسانی شد.",
            reply_markup=MAIN_KEYBOARD,
        )
        await show_dashboard(update, context)
        return

    if text == START_REPORT:
        profile = db.get_user_by_telegram_id(user.id)
        if profile is None or not db.is_user_verified(user.id):
            await update.message.reply_text("برای ثبت گزارش ابتدا /start را بزنید.")
            return
        week = get_shamsi_week()
        selected_range = f"{week.start_str} الی {week.end_str}"
        matching_report_id = _find_report_for_range(user.id, selected_range)
        active = db.get_active_report_for_user(user.id)
        if (
            active
            and active.status == ReportStatus.AWAITING_CONFIRMATION.value
            and active.id != matching_report_id
        ):
            await update.message.reply_text(
                "یک پیش‌نویس در انتظار بررسی دارید. از «گزارش‌های من» آن را ادامه دهید.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        context.user_data.pop("report_date_range", None)
        context.user_data.pop("report_custom_date_step", None)
        context.user_data.pop("report_custom_start_date", None)
        if matching_report_id is not None:
            context.user_data["append_voice_report_id"] = matching_report_id
        else:
            context.user_data.pop("append_voice_report_id", None)
        context.user_data["awaiting_voice"] = True
        await update.message.reply_text(
            (
                "♻️ برای این هفته گزارش قبلی پیدا شد؛ ورودی جدید به همان گزارش اضافه می‌شود.\n\n"
                if matching_report_id is not None
                else ""
            )
            + _report_prompt_text(context),
            reply_markup=_report_prompt_keyboard(),
        )
        return

    if text == MY_REPORTS:
        await show_report_list(update, context)
        return
    if text == MY_PROFILE:
        await show_profile(update, context)
        return
    if text == HELP:
        await update.message.reply_text(
            "❓ راهنما\n\n"
            "برای ثبت گزارش، «ثبت گزارش هفتگی» را بزنید و گزارش را به صورت متن فارسی تایپ "
            "کنید یا ویس ۲ تا ۵ دقیقه‌ای بفرستید. پیش‌نویس را می‌توانید با پیام متنی "
            "ویرایش و سپس تأیید کنید.\n"
            "گزارش‌های قبلی و وضعیت آن‌ها در «گزارش‌های من» قابل مشاهده است.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if context.user_data.get("awaiting_voice"):
        await text_report_handler(update, context)
        return

    if context.user_data.get("awaiting_correction_for"):
        await correction_handler(update, context)
        return
    if text == BACK:
        await show_dashboard(update, context)
        return
    await show_dashboard(update, context)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    profile = get_database().get_user_by_telegram_id(user.id)
    if profile is None:
        await message.reply_text("پروفایل پیدا نشد. لطفاً /start را بزنید.")
        return
    safe_id = str(user.id)
    masked_id = f"••••{safe_id[-4:]}"
    await message.reply_text(
        "👤 پروفایل من\n\n"
        f"نام:\n{profile.display_name or 'ثبت نشده'}\n\n"
        f"دپارتمان:\n{profile.department or 'ثبت نشده'}\n\n"
        f"شناسه:\n{masked_id}\n\n"
        "━━━━━━━━━━━━━━",
        reply_markup=_profile_keyboard(),
    )


async def show_report_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    reports = get_database().get_reports_for_user(user.id, limit=10)
    if not reports:
        await message.reply_text(
            "📋 گزارش‌های من\n\nهنوز گزارشی ثبت نکرده‌اید.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    lines = ["📋 گزارش‌های من", ""]
    rows: list[list[InlineKeyboardButton]] = []
    status_labels = {
        ReportStatus.COMPLETED.value: "🟢 تکمیل شده",
        ReportStatus.AWAITING_CONFIRMATION.value: "🟡 در حال تکمیل / آماده تأیید",
        ReportStatus.CONFIRMED.value: "🟡 در حال آماده‌سازی",
        ReportStatus.RECEIVED.value: "⏳ در حال پردازش",
        ReportStatus.TRANSCRIBED.value: "⏳ در حال پردازش",
        ReportStatus.EXTRACTED.value: "⏳ در حال پردازش",
        ReportStatus.FAILED.value: "🔴 پردازش ناموفق",
        ReportStatus.CANCELLED.value: "⚪ لغو شده",
    }
    for report in reports:
        structured = (
            WeeklyReport.model_validate_json(report.structured_json)
            if report.structured_json
            else None
        )
        period = (
            format_shamsi_range(structured.report_date_range)
            if structured
            else report.created_at.strftime("%Y-%m-%d")
        )
        lines.append(f"📅 {period}\n   {status_labels.get(report.status, '⚪ ' + report.status)}")
        if report.status == ReportStatus.AWAITING_CONFIRMATION.value:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"ادامه گزارش {report.id}",
                        callback_data=f"dashboard:continue:{report.id}",
                    )
                ]
            )
        if report.status in {
            ReportStatus.AWAITING_CONFIRMATION.value,
            ReportStatus.COMPLETED.value,
        }:
            rows.append(
                [
                    InlineKeyboardButton(
                        _append_report_button_label(period),
                        callback_data=f"dashboard:append:{report.id}",
                    )
                ]
            )
        if report.status == ReportStatus.COMPLETED.value and report.excel_path:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📎 دریافت Excel {report.id}",
                        callback_data=f"dashboard:excel:{report.id}",
                    ),
                    InlineKeyboardButton(
                        "مشاهده",
                        callback_data=f"dashboard:view:{report.id}",
                    ),
                ]
            )
    rows.append([InlineKeyboardButton("🏠 صفحه اصلی", callback_data="dashboard:home")])
    await message.reply_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def dashboard_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.data is None:
        return
    await query.answer()
    parts = query.data.split(":")
    db = get_database()
    if query.data == "dashboard:home":
        context.user_data.pop("profile_edit_step", None)
        context.user_data.pop("profile_edit_name", None)
        profile = db.get_user_by_telegram_id(user.id)
        await query.edit_message_text(
            dashboard_text(
                profile.display_name if profile else user.first_name,
                _current_week_status(user.id),
            )
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="از منوی زیر انتخاب کنید:",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if query.data == "dashboard:edit_profile":
        context.user_data["profile_edit_step"] = "name"
        await query.edit_message_text(
            "✏️ ابتدا نام و نام خانوادگی جدید را وارد کنید:"
        )
        return
    if query.data == "dashboard:custom_range":
        context.user_data["report_custom_date_step"] = "start"
        context.user_data.pop("report_custom_start_date", None)
        context.user_data.pop("append_voice_report_id", None)
        context.user_data.pop("report_date_range", None)
        await query.edit_message_text(
            "📅 انتخاب بازه دلخواه\n\n"
            "تاریخ شروع را با قالب سال/ماه/روز وارد کنید؛ "
            "مثلاً ۱۴۰۵/۰۷/۰۴.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "❌ انصراف",
                        callback_data="dashboard:cancel_report",
                    )
                ]]
            ),
        )
        return
    if query.data == "dashboard:cancel_report":
        context.user_data.pop("awaiting_voice", None)
        context.user_data.pop("report_custom_date_step", None)
        context.user_data.pop("report_custom_start_date", None)
        context.user_data.pop("report_date_range", None)
        context.user_data.pop("append_voice_report_id", None)
        await query.edit_message_text("ثبت گزارش لغو شد.")
        return
    if len(parts) != 3 or parts[0] != "dashboard":
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    action = parts[1]
    try:
        report_id = int(parts[2])
    except ValueError:
        await query.edit_message_text("شناسه گزارش نامعتبر است.")
        return
    report = db.get_report_for_user(report_id, user.id)
    if report is None:
        await query.edit_message_text("گزارش یافت نشد.")
        return
    if action == "continue" and report.status == ReportStatus.AWAITING_CONFIRMATION.value:
        if not report.structured_json:
            await query.edit_message_text("پیش‌نویس گزارش موجود نیست.")
            return
        structured = WeeklyReport.model_validate_json(report.structured_json)
        context.user_data["awaiting_correction_for"] = report.id
        await query.edit_message_text(
            format_report_for_review(structured, report.id),
            reply_markup=report_review_keyboard(report.id, structured),
        )
    elif action == "append" and report.status in {
        ReportStatus.AWAITING_CONFIRMATION.value,
        ReportStatus.COMPLETED.value,
    }:
        context.user_data["append_voice_report_id"] = report.id
        context.user_data["awaiting_voice"] = True
        period = (
            format_shamsi_range(
                WeeklyReport.model_validate_json(report.structured_json).report_date_range
            )
            if report.structured_json
            else "این هفته"
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"➕ افزودن به گزارش {period}\n\n"
                "متن فارسی گزارش را تایپ کنید یا ویس جدید بفرستید. موارد تازه به پیش‌نویس "
                "همین هفته اضافه می‌شود و موارد تکراری دوباره ثبت نمی‌شوند.\n"
                "پس از هر ورودی می‌توانید گزارش را بررسی کنید و هر زمان آماده بودید تأیید کنید."
            ),
            reply_markup=_report_prompt_keyboard(allow_custom_range=False),
        )
    elif action == "view" and report.structured_json:
        structured = WeeklyReport.model_validate_json(report.structured_json)
        await query.edit_message_text(
            format_report_for_review(structured, report.id),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 صفحه اصلی", callback_data="dashboard:home")]]
            ),
        )
    elif action == "excel" and report.status == ReportStatus.COMPLETED.value:
        if not report.excel_path or not Path(report.excel_path).is_file():
            await query.edit_message_text("فایل Excel این گزارش در دسترس نیست.")
            return
        with Path(report.excel_path).open("rb") as handle:
            await context.bot.send_document(
                chat_id=user.id,
                document=handle,
                filename=Path(report.excel_path).name,
                caption="📊 فایل Excel گزارش هفتگی شما",
            )
    else:
        await query.edit_message_text("این عملیات برای وضعیت فعلی گزارش ممکن نیست.")
