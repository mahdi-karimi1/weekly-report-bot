"""Regression tests for empty transcription/extraction and FRN report formatting."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.models.database import Database
from bot.models.report import (
    ReportStatus,
    TimedActivity,
    UnclassifiedCompletedActivity,
    WeeklyReport,
)
from bot.handlers.voice import process_text_report, process_voice_report
from bot.services.report_extractor import (
    EmptyReportExtractionError,
    ReportExtractionError,
    ReportExtractor,
)
from bot.utils.formatting import format_report_for_review


def test_format_shows_frn_categories() -> None:
    report = WeeklyReport(
        employee_name="MK",
        department_or_project="هوش مصنوعی",
        planned_completed=[TimedActivity(description="کار برنامه‌ای", hours=2)],
        unplanned_completed=[TimedActivity(description="کار اضطراری")],
        self_initiated=[TimedActivity(description="مطالعه")],
        next_week_planned=[TimedActivity(description="تست", hours=4)],
        problems_and_suggestions=["قطعی VPN"],
        needs_classification=[
            UnclassifiedCompletedActivity(description="جلسه مبهم", hours=1)
        ],
    )
    text = format_report_for_review(report, 1)
    assert "کار برنامه‌ای" in text
    assert "کار اضطراری" in text
    assert "مطالعه" in text
    assert "تست" in text
    assert "قطعی VPN" in text
    assert "نیاز به طبقه‌بندی" in text
    assert "جلسه مبهم" in text


def test_empty_report_has_no_meaningful_content() -> None:
    report = WeeklyReport()
    assert report.has_meaningful_content() is False


def test_format_empty_uses_telegram_placeholders_only() -> None:
    text = format_report_for_review(WeeklyReport(employee_name="MK"), 1)
    assert "کارشناس: MK" in text
    assert "—" in text  # Telegram display only


@pytest.mark.asyncio
async def test_extract_rejects_empty_transcription() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="t", AVALAI_API_KEY="k")
    extractor = ReportExtractor(settings, MagicMock())
    with pytest.raises(ReportExtractionError):
        await extractor.extract("   ")


@pytest.mark.asyncio
async def test_extract_rejects_empty_structured_report() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="t", AVALAI_API_KEY="k")
    avalai = MagicMock()
    empty_payload = {
        "employee_name": None,
        "report_date_range": None,
        "department_or_project": None,
        "planned_completed": [],
        "unplanned_completed": [],
        "self_initiated": [],
        "next_week_planned": [],
        "problems_and_suggestions": [],
        "needs_classification": [],
    }
    avalai.chat_completion_text = AsyncMock(return_value=json.dumps(empty_payload))
    extractor = ReportExtractor(settings, avalai)
    with pytest.raises(EmptyReportExtractionError):
        await extractor.extract("این هفته روی پروژه پورتال کار کردم")
    # first + schema retry + no-schema retry + salvage
    assert avalai.chat_completion_text.await_count >= 4


@pytest.mark.asyncio
async def test_voice_pipeline_status_message_is_editable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'voice.db'}")
    database.create_tables()
    monkeypatch.setattr("bot.handlers.voice.get_database", lambda: database)

    status_message = SimpleNamespace(edit_text=AsyncMock())
    reply_text = AsyncMock(return_value=status_message)
    voice_message = SimpleNamespace(reply_text=reply_text)
    user = SimpleNamespace(id=123, username="staff", first_name="Test", last_name=None)
    update = SimpleNamespace(effective_user=user, message=voice_message)
    audio = SimpleNamespace(path=tmp_path / "voice.ogg", mime_type="audio/ogg")
    activity_report = WeeklyReport(
        planned_completed=[TimedActivity(description="تکمیل کار")],
    )
    telegram_audio = SimpleNamespace(
        download_voice=AsyncMock(return_value=audio),
        cleanup=AsyncMock(),
    )
    transcription = SimpleNamespace(transcribe=AsyncMock(return_value="متن گزارش"))
    extractor = SimpleNamespace(extract=AsyncMock(return_value=activity_report))
    context = SimpleNamespace(
        bot=MagicMock(),
        user_data={},
        application=SimpleNamespace(
            bot_data={
                "services": {
                    "telegram_audio": telegram_audio,
                    "transcription": transcription,
                    "extractor": extractor,
                }
            }
        )
    )

    await process_voice_report(update, context, MagicMock(), "1405/07/04 الی 1405/07/08")

    assert "reply_markup" not in reply_text.await_args.kwargs
    assert status_message.edit_text.await_count >= 3
    saved_report = database.get_active_report_for_user(user.id)
    assert saved_report is not None
    assert saved_report.status == ReportStatus.AWAITING_CONFIRMATION.value


@pytest.mark.asyncio
async def test_voice_update_reuses_and_completes_existing_week_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'voice-update.db'}")
    database.create_tables()
    db_user = database.complete_onboarding(
        124,
        full_name="Test User",
        department="Ops",
    )
    previous = WeeklyReport(
        employee_name="Test User",
        department_or_project="Ops",
        report_date_range="1405/07/04 الی 1405/07/08",
        planned_completed=[TimedActivity(description="کار قبلی")],
    )
    report = database.create_report(db_user.id, ReportStatus.COMPLETED)
    database.update_report(
        report.id,
        transcription="یادداشت اول",
        structured_json=previous.model_dump_json(),
        excel_path=str(tmp_path / "previous.xlsx"),
    )
    monkeypatch.setattr("bot.handlers.voice.get_database", lambda: database)

    status_message = SimpleNamespace(edit_text=AsyncMock())
    reply_text = AsyncMock(return_value=status_message)
    voice_message = SimpleNamespace(reply_text=reply_text)
    user = SimpleNamespace(id=124, username="staff", first_name="Test", last_name=None)
    update = SimpleNamespace(effective_user=user, message=voice_message)
    audio = SimpleNamespace(path=tmp_path / "voice.ogg", mime_type="audio/ogg")
    merged_report = previous.model_copy(deep=True)
    merged_report.planned_completed.append(TimedActivity(description="کار دوم"))
    telegram_audio = SimpleNamespace(
        download_voice=AsyncMock(return_value=audio),
        cleanup=AsyncMock(),
    )
    transcription = SimpleNamespace(transcribe=AsyncMock(return_value="یادداشت دوم"))
    extractor = SimpleNamespace(
        extract_voice_update=AsyncMock(return_value=merged_report)
    )
    context = SimpleNamespace(
        bot=MagicMock(),
        user_data={},
        application=SimpleNamespace(
            bot_data={
                "services": {
                    "telegram_audio": telegram_audio,
                    "transcription": transcription,
                    "extractor": extractor,
                }
            }
        ),
    )

    await process_voice_report(
        update,
        context,
        MagicMock(),
        "1405/07/04 الی 1405/07/08",
        existing_report_id=report.id,
    )

    saved = database.get_report_for_user(report.id, user.id)
    assert saved is not None
    assert saved.status == ReportStatus.AWAITING_CONFIRMATION.value
    assert saved.transcription == "یادداشت اول\n\nیادداشت دوم"
    assert [
        item.description
        for item in WeeklyReport.model_validate_json(saved.structured_json).planned_completed
    ] == ["کار قبلی", "کار دوم"]
    assert extractor.extract_voice_update.await_count == 1


@pytest.mark.asyncio
async def test_typed_report_is_extracted_without_audio_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'text-report.db'}")
    database.create_tables()
    database.complete_onboarding(
        125,
        full_name="Test User",
        department="Ops",
    )
    monkeypatch.setattr("bot.handlers.voice.get_database", lambda: database)

    status_message = SimpleNamespace(edit_text=AsyncMock())
    reply_text = AsyncMock(return_value=status_message)
    text_message = SimpleNamespace(reply_text=reply_text)
    user = SimpleNamespace(id=125, username="staff", first_name="Test", last_name=None)
    update = SimpleNamespace(effective_user=user, message=text_message)
    activity_report = WeeklyReport(
        planned_completed=[TimedActivity(description="تکمیل کار")],
    )
    extractor = SimpleNamespace(extract=AsyncMock(return_value=activity_report))
    context = SimpleNamespace(
        user_data={},
        application=SimpleNamespace(bot_data={"services": {"extractor": extractor}}),
    )

    await process_text_report(
        update,
        context,
        "این هفته کار را تکمیل کردم",
        "1405/07/04 الی 1405/07/08",
    )

    extractor.extract.assert_awaited_once_with("این هفته کار را تکمیل کردم")
    saved_report = database.get_active_report_for_user(user.id)
    assert saved_report is not None
    assert saved_report.status == ReportStatus.AWAITING_CONFIRMATION.value
    assert saved_report.transcription == "این هفته کار را تکمیل کردم"
