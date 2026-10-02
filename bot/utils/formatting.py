"""Persian message formatting for Telegram review screens (FRN categories)."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models.report import TimedActivity, WeeklyReport
from bot.utils.calendar import format_shamsi_range


def _bullets(items: list[str], empty: str = "—") -> str:
    """Telegram-only empty marker. Never written into Excel cells."""
    if not items:
        return empty
    return "\n".join(f"• {item}" for item in items)


def _format_timed(activities: list[TimedActivity]) -> list[str]:
    lines: list[str] = []
    for activity in activities:
        if activity.hours is None:
            lines.append(activity.description)
        else:
            lines.append(f"{activity.description} ({activity.hours:g} ساعت)")
    return lines


def format_report_for_review(report: WeeklyReport, report_id: int) -> str:
    header_bits = [f"شناسه گزارش: {report_id}"]
    if report.employee_name:
        header_bits.append(f"👤 کارشناس: {report.employee_name}")
    if report.report_date_range:
        header_bits.append(f"📅 {format_shamsi_range(report.report_date_range)}")

    sections = [
        "📊 پیش‌نویس گزارش هفتگی",
        "\n".join(header_bits),
        "",
        "🟢 طبق برنامه",
        _bullets(_format_timed(report.planned_completed)),
        "",
        "🔵 خارج از برنامه",
        _bullets(_format_timed(report.unplanned_completed)),
        "",
        "🟣 خودخواسته",
        _bullets(_format_timed(report.self_initiated)),
        "",
        "🟠 هفته آینده",
        _bullets(_format_timed(report.next_week_planned)),
        "",
        "🔴 مشکلات",
        _bullets(report.problems_and_suggestions),
    ]

    if report.needs_classification:
        pending = []
        for item in report.needs_classification:
            line = item.description
            if item.hours is not None:
                line += f" ({item.hours:g} ساعت)"
            if item.reason:
                line += f" — علت ابهام: {item.reason}"
            pending.append(line)
        sections.extend(
            [
                "",
                "❓ نیاز به طبقه‌بندی توسط شما:",
                _bullets(pending),
                "",
                "قبل از تأیید، با «ویرایش گزارش» مشخص کنید هر مورد "
                "طبق برنامه، خارج از برنامه، یا خودخواسته است.",
            ]
        )

    if report.pending_suggestions():
        sections.extend(
            [
                "",
                "━━━━━━━━━━━━━━",
                "💡 پیشنهادهای هفته آینده",
                "این پیشنهادها تا پذیرش شما بخشی از گزارش قطعی نیستند:",
                _bullets(_format_timed(report.pending_suggestions())),
                "برای افزودن، از دکمه‌های ➕ استفاده کنید.",
            ]
        )

    overflowing = report.overflowing_categories()
    if overflowing:
        sections.extend(
            [
                "",
                "⛔ تعداد موارد بیش از ظرفیت ۱۵ ردیف قالب است:",
                _bullets(overflowing),
                "لطفاً با ویرایش تعداد را کاهش دهید.",
            ]
        )

    sections.append("")
    sections.extend(
        [
            "━━━━━━━━━━━━━━",
            "می‌توانید در طول هفته ویس‌های بیشتری به این گزارش اضافه کنید؛ "
            "برای نهایی‌سازی «تأیید گزارش» را بزنید.",
        ]
    )
    return "\n".join(sections)


def confirmation_keyboard(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تأیید گزارش",
                    callback_data=f"confirm:{report_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ ویرایش گزارش",
                    callback_data=f"edit:{report_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"cancel:{report_id}",
                )
            ],
        ]
    )


def suggestions_keyboard(report_id: int, report: WeeklyReport) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(report.suggested_next_week_tasks):
        if item.accepted:
            continue
        label = item.description.strip()
        if len(label) > 28:
            label = label[:27] + "…"
        hours = f" ({item.hours:g}س)" if item.hours is not None else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"➕ {label}{hours}",
                    callback_data=f"sugg:accept:{report_id}:{index}",
                ),
                InlineKeyboardButton(
                    "✖️ رد",
                    callback_data=f"sugg:reject:{report_id}:{index}",
                ),
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ قبول همه",
                    callback_data=f"sugg:all:{report_id}",
                ),
                InlineKeyboardButton(
                    "❌ رد همه",
                    callback_data=f"sugg:none:{report_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def report_review_keyboard(report_id: int, report: WeeklyReport) -> InlineKeyboardMarkup:
    suggestions = suggestions_keyboard(report_id, report).inline_keyboard
    actions = confirmation_keyboard(report_id).inline_keyboard
    return InlineKeyboardMarkup(
        [
            *suggestions,
            [
                InlineKeyboardButton(
                    "🎙 افزودن ویس به این هفته",
                    callback_data=f"dashboard:append:{report_id}",
                )
            ],
            *actions,
        ]
    )
