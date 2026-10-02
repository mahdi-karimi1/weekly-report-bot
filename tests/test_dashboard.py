from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.dashboard import (
    HELP,
    MAIN_KEYBOARD,
    MY_PROFILE,
    MY_REPORTS,
    START_REPORT,
    _report_prompt_keyboard,
    _report_prompt_text,
    _append_report_button_label,
    dashboard_text,
    dashboard_text_handler,
)
from bot.utils.calendar import format_shamsi_range, get_shamsi_week


def test_dashboard_shows_week_status_and_primary_actions() -> None:
    text = dashboard_text("علی رضایی")

    assert "👋 سلام علی رضایی" in text
    assert "📅 هفته جاری" in text
    assert "📊 وضعیت گزارش" in text
    assert "هنوز گزارشی برای این هفته ثبت نشده" in text

    labels = [button.text for row in MAIN_KEYBOARD.keyboard for button in row]
    assert labels == [START_REPORT, MY_REPORTS, MY_PROFILE, HELP]


def test_report_prompt_defaults_to_current_week_and_offers_custom_range() -> None:
    prompt = _report_prompt_text(SimpleNamespace(user_data={}))
    week = get_shamsi_week()
    callback_data = [
        button.callback_data
        for row in _report_prompt_keyboard().inline_keyboard
        for button in row
    ]

    assert format_shamsi_range(week.display_range) in prompt
    assert "متن فارسی تایپ کنید" in prompt
    assert "پیام صوتی بفرستید" in prompt
    assert "dashboard:custom_range" in callback_data
    assert "dashboard:cancel_report" in callback_data


def test_append_prompt_does_not_offer_changing_selected_week() -> None:
    callback_data = [
        button.callback_data
        for row in _report_prompt_keyboard(allow_custom_range=False).inline_keyboard
        for button in row
    ]

    assert callback_data == ["dashboard:cancel_report"]


def test_add_report_button_shows_report_date_range_not_database_id() -> None:
    label = _append_report_button_label("۴ مهر ۱۴۰۵ تا ۸ مهر ۱۴۰۵")

    assert label == "➕ افزودن به ۴ مهر ۱۴۰۵ تا ۸ مهر ۱۴۰۵"
    assert "هفته 42" not in label


@pytest.mark.asyncio
async def test_dashboard_routes_typed_report_input_when_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_text = AsyncMock()
    monkeypatch.setattr("bot.handlers.dashboard.text_report_handler", route_text)
    monkeypatch.setattr(
        "bot.handlers.dashboard.get_database",
        lambda: SimpleNamespace(
            is_user_verified=lambda _user_id: True,
            get_active_report_for_user=lambda _user_id: None,
        ),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(text="این هفته روی پروژه کار کردم", reply_text=AsyncMock()),
    )
    context = SimpleNamespace(user_data={"awaiting_voice": True})

    await dashboard_text_handler(update, context)

    route_text.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_start_report_keeps_custom_range_option_when_current_week_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bot.handlers.dashboard.get_database",
        lambda: SimpleNamespace(
            get_user_by_telegram_id=lambda _user_id: SimpleNamespace(),
            is_user_verified=lambda _user_id: True,
            get_active_report_for_user=lambda _user_id: None,
        ),
    )
    monkeypatch.setattr(
        "bot.handlers.dashboard._find_report_for_range",
        lambda *_args: 123,
    )
    message = SimpleNamespace(text=START_REPORT, reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
    context = SimpleNamespace(user_data={})

    await dashboard_text_handler(update, context)

    keyboard = message.reply_text.await_args.kwargs["reply_markup"]
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "dashboard:custom_range" in callback_data


@pytest.mark.asyncio
async def test_custom_report_range_requires_valid_increasing_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bot.handlers.dashboard.get_database",
        lambda: SimpleNamespace(get_reports_for_user=lambda *_args, **_kwargs: []),
    )
    user_data = {"report_custom_date_step": "start"}
    user = SimpleNamespace(id=42)

    async def send_text(text: str) -> None:
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=user, message=message)
        await dashboard_text_handler(
            update,
            SimpleNamespace(user_data=user_data),
        )

    await send_text("۱۴۰۵/۰۷/۰۴")
    assert user_data["report_custom_date_step"] == "end"
    await send_text("۱۴۰۵/۰۷/۰۳")
    assert user_data["report_custom_date_step"] == "end"
    await send_text("۱۴۰۵/۰۷/۰۸")

    assert user_data["report_date_range"] == "1405/07/04 الی 1405/07/08"
    assert "report_custom_date_step" not in user_data
    assert user_data["awaiting_voice"] is True


def test_dashboard_uses_known_status_labels() -> None:
    text = dashboard_text("علی", "awaiting_confirmation")

    assert "یک پیش‌نویس در انتظار تأیید دارید" in text
