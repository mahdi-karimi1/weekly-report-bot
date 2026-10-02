"""Persistence and confirmation-flow related tests (no Telegram network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.models.database import Database
from bot.models.report import (
    ReportStatus,
    SuggestedNextWeekTask,
    TimedActivity,
    WeeklyReport,
)
from bot.utils.formatting import (
    confirmation_keyboard,
    format_report_for_review,
    report_review_keyboard,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_tables()
    return database


def test_user_and_report_lifecycle(db: Database) -> None:
    user = db.get_or_create_user(10, "alice", "Alice")
    report = db.create_report(user.id, ReportStatus.RECEIVED)
    db.update_report(
        report.id,
        transcription="متن",
        status=ReportStatus.TRANSCRIBED,
    )
    db.update_report(
        report.id,
        structured_json=WeeklyReport(
            planned_completed=[TimedActivity(description="کار")],
        ).model_dump_json(),
        status=ReportStatus.AWAITING_CONFIRMATION,
    )
    owned = db.get_report_for_user(report.id, 10)
    assert owned is not None
    assert owned.status == ReportStatus.AWAITING_CONFIRMATION.value

    foreign = db.get_report_for_user(report.id, 99)
    assert foreign is None

    active = db.get_active_report_for_user(10)
    assert active is not None
    assert active.id == report.id

    db.update_report(report.id, status=ReportStatus.CANCELLED)
    assert db.get_active_report_for_user(10) is None


def test_report_history_is_scoped_and_newest_first(db: Database) -> None:
    user = db.get_or_create_user(101, "alice", "Alice")
    older = db.create_report(user.id, ReportStatus.CANCELLED)
    newer = db.create_report(user.id, ReportStatus.COMPLETED)
    other = db.get_or_create_user(202, "bob", "Bob")
    db.create_report(other.id, ReportStatus.COMPLETED)

    reports = db.get_reports_for_user(101)

    assert [report.id for report in reports] == [newer.id, older.id]


def test_confirmation_keyboard_actions() -> None:
    markup = confirmation_keyboard(42)
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels == ["✅ تأیید گزارش", "✏️ ویرایش گزارش", "❌ لغو"]
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert data == ["confirm:42", "edit:42", "cancel:42"]


def test_format_report_sections_persian() -> None:
    report = WeeklyReport(
        employee_name="نیما",
        planned_completed=[TimedActivity(description="انجام استقرار", hours=3)],
        next_week_planned=[TimedActivity(description="آزمون پذیرش")],
        problems_and_suggestions=["قطعی VPN"],
    )
    text = format_report_for_review(report, 7)
    assert "شناسه گزارش: 7" in text
    assert "انجام استقرار" in text
    assert "آزمون پذیرش" in text
    assert "قطعی VPN" in text
    assert "🟢 طبق برنامه" in text
    assert "🔴 مشکلات" in text


def test_week_suggestions_are_visually_separate_from_report() -> None:
    report = WeeklyReport(
        next_week_planned=[TimedActivity(description="کار قطعی")],
        suggested_next_week_tasks=[
            SuggestedNextWeekTask(description="پیشنهاد آزمایشی")
        ],
    )

    text = format_report_for_review(report, 12)

    assert "🟠 هفته آینده\n• کار قطعی" in text
    assert "💡 پیشنهادهای هفته آینده" in text
    assert "این پیشنهادها تا پذیرش شما بخشی از گزارش قطعی نیستند" in text


def test_multiple_users_independent_reports(db: Database) -> None:
    u1 = db.get_or_create_user(1, "u1", "One")
    u2 = db.get_or_create_user(2, "u2", "Two")
    r1 = db.create_report(u1.id, ReportStatus.AWAITING_CONFIRMATION)
    r2 = db.create_report(u2.id, ReportStatus.AWAITING_CONFIRMATION)
    db.update_report(r1.id, structured_json=WeeklyReport().model_dump_json())
    db.update_report(
        r2.id,
        structured_json=WeeklyReport(
            planned_completed=[TimedActivity(description="x")]
        ).model_dump_json(),
    )

    assert db.get_report_for_user(r1.id, 1) is not None
    assert db.get_report_for_user(r1.id, 2) is None
    assert db.get_report_for_user(r2.id, 2) is not None
    assert db.get_active_report_for_user(1).id == r1.id  # type: ignore[union-attr]
    assert db.get_active_report_for_user(2).id == r2.id  # type: ignore[union-attr]


def test_onboarding_persists_profile_without_phone_number(db: Database) -> None:
    db.get_or_create_user(12, "staff", "Telegram Name")
    assert not db.is_user_verified(12)

    profile = db.complete_onboarding(
        12,
        full_name="نام کامل",
        department="فناوری",
        username="staff",
    )

    assert profile.phone_number is None
    assert profile.display_name == "نام کامل"
    assert profile.department == "فناوری"
    assert db.is_user_verified(12)


def test_suggestion_acceptance_and_rejection_are_persistable() -> None:
    report = WeeklyReport(
        suggested_next_week_tasks=[
            SuggestedNextWeekTask(description="تکمیل آزمون‌ها"),
            SuggestedNextWeekTask(description="مستندسازی API"),
        ]
    )

    accepted = report.accept_suggestion(0)
    rejected = report.reject_suggestion(1)

    assert accepted is not None and accepted.accepted
    assert [item.description for item in report.next_week_planned] == ["تکمیل آزمون‌ها"]
    assert rejected is not None and rejected.description == "مستندسازی API"
    assert report.suggested_next_week_tasks == [accepted]

    restored = WeeklyReport.model_validate_json(report.model_dump_json())
    assert restored.next_week_planned[0].description == "تکمیل آزمون‌ها"


def test_accept_all_suggestions_adds_each_task_once() -> None:
    report = WeeklyReport(
        next_week_planned=[TimedActivity(description="کار موجود")],
        suggested_next_week_tasks=[
            SuggestedNextWeekTask(description="کار جدید"),
            SuggestedNextWeekTask(description="کار موجود"),
        ],
    )

    report.accept_all_suggestions()

    assert [task.description for task in report.next_week_planned] == [
        "کار موجود",
        "کار جدید",
    ]
    assert all(task.accepted for task in report.suggested_next_week_tasks)


def test_review_keyboard_exposes_per_suggestion_accept_and_reject() -> None:
    report = WeeklyReport(
        suggested_next_week_tasks=[
            SuggestedNextWeekTask(description="تکمیل آزمون‌ها")
        ]
    )

    buttons = [
        button
        for row in report_review_keyboard(42, report).inline_keyboard
        for button in row
    ]

    assert "sugg:accept:42:0" in [button.callback_data for button in buttons]
    assert "sugg:reject:42:0" in [button.callback_data for button in buttons]
    assert "sugg:all:42" in [button.callback_data for button in buttons]
    assert "sugg:none:42" in [button.callback_data for button in buttons]
    assert "confirm:42" in [button.callback_data for button in buttons]
    assert "dashboard:append:42" in [button.callback_data for button in buttons]
