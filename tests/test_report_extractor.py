"""Tests for report extraction with mocked AvalAI responses (FRN schema)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bot.config import Settings
from bot.models.report import WeeklyReport
from bot.services.report_extractor import (
    EmptyReportExtractionError,
    ReportExtractionError,
    ReportExtractor,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(TELEGRAM_BOT_TOKEN="dummy", AVALAI_API_KEY="dummy")


@pytest.fixture
def extractor(settings: Settings) -> ReportExtractor:
    avalai = AsyncMock()
    return ReportExtractor(settings, avalai)  # type: ignore[arg-type]


def _empty_payload() -> dict[str, Any]:
    return {
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


@pytest.mark.asyncio
async def test_extract_planned_and_next_week(extractor: ReportExtractor) -> None:
    payload = _empty_payload()
    payload["employee_name"] = "سارا"
    payload["planned_completed"] = [{"description": "رفع باگ ورود", "hours": 3}]
    payload["next_week_planned"] = [{"description": "تست", "hours": None}]
    extractor.avalai.chat_completion_text = AsyncMock(return_value=json.dumps(payload))
    report = await extractor.extract("این هفته باگ ورود را درست کردم حدود سه ساعت")
    assert report.employee_name == "سارا"
    assert report.planned_completed[0].hours == 3
    assert report.next_week_planned[0].hours is None


@pytest.mark.asyncio
async def test_extract_defaults_unresolved_activity_to_planned(
    extractor: ReportExtractor,
) -> None:
    payload = _empty_payload()
    payload["needs_classification"] = [
        {
            "description": "جلسه هماهنگی",
            "hours": 2,
            "reason": "مشخص نبود برنامه‌ای است یا خارج از برنامه",
        }
    ]
    extractor.avalai.chat_completion_text = AsyncMock(return_value=json.dumps(payload))
    report = await extractor.extract("دو ساعت جلسه داشتم")
    assert report.has_pending_classification() is False
    assert report.planned_completed[0].description == "جلسه هماهنگی"
    assert report.planned_completed[0].hours == 2


@pytest.mark.asyncio
async def test_extract_voice_update_appends_new_activities_once(
    extractor: ReportExtractor,
) -> None:
    payload = _empty_payload()
    payload["planned_completed"] = [
        {"description": "کار قبلی"},
        {"description": "کار تازه"},
    ]
    extractor.avalai.chat_completion_text = AsyncMock(return_value=json.dumps(payload))
    current = WeeklyReport(
        employee_name="سارا",
        report_date_range="1405/07/04 الی 1405/07/08",
        planned_completed=[{"description": "کار قبلی"}],
    )

    merged = await extractor.extract_voice_update("امروز کار تازه را انجام دادم", current)

    assert merged.employee_name == "سارا"
    assert merged.report_date_range == "1405/07/04 الی 1405/07/08"
    assert [item.description for item in merged.planned_completed] == [
        "کار قبلی",
        "کار تازه",
    ]


@pytest.mark.asyncio
async def test_invalid_json_triggers_repair_then_success(extractor: ReportExtractor) -> None:
    good = _empty_payload()
    good["problems_and_suggestions"] = ["قطعی اینترنت"]
    extractor.avalai.chat_completion_text = AsyncMock(
        side_effect=["not-json{{{", json.dumps(good)]
    )
    report = await extractor.extract("مشکل قطعی اینترنت داشتیم")
    assert report.problems_and_suggestions == ["قطعی اینترنت"]


@pytest.mark.asyncio
async def test_repair_exhaustion_raises(extractor: ReportExtractor) -> None:
    extractor.avalai.chat_completion_text = AsyncMock(return_value="<<<broken>>>")
    with pytest.raises(ReportExtractionError):
        await extractor.extract("متن")


@pytest.mark.asyncio
async def test_apply_correction_moves_classification(extractor: ReportExtractor) -> None:
    current = WeeklyReport.model_validate(
        {
            **_empty_payload(),
            "employee_name": "مینا",
            "needs_classification": [
                {"description": "جلسه", "hours": 1, "reason": "مبهم"}
            ],
        }
    )
    updated_payload = current.model_dump()
    updated_payload["needs_classification"] = []
    updated_payload["planned_completed"] = [{"description": "جلسه", "hours": 1}]
    extractor.avalai.chat_completion_text = AsyncMock(
        return_value=json.dumps(updated_payload)
    )
    result = await extractor.apply_correction(
        current,
        "جلسه جزو فعالیت‌های طبق برنامه بود",
    )
    assert result.employee_name == "مینا"
    assert result.needs_classification == []
    assert result.planned_completed[0].description == "جلسه"


@pytest.mark.asyncio
async def test_empty_then_salvage_defaults_activity_to_planned(
    extractor: ReportExtractor,
) -> None:
    empty = _empty_payload()
    salvage = _empty_payload()
    salvage["needs_classification"] = [
        {"description": "کار روی پورتال", "hours": None, "reason": "دسته مبهم"}
    ]
    extractor.avalai.chat_completion_text = AsyncMock(
        side_effect=[
            json.dumps(empty),  # first pass
            json.dumps(empty),  # schema retry
            json.dumps(empty),  # no-schema retry
            json.dumps(salvage),  # salvage
        ]
    )
    report = await extractor.extract("این هفته روی پورتال کار کردم")
    assert report.has_meaningful_content() is True
    assert report.has_pending_classification() is False
    assert report.planned_completed[0].description == "کار روی پورتال"
    assert extractor.avalai.chat_completion_text.await_count == 4


def test_normalize_alternate_activity_keys() -> None:
    from bot.services.report_extractor import normalize_extraction_payload

    payload = normalize_extraction_payload(
        {
            "name": "علی",
            "project": "دانا",
            "activities": [{"activity_description": "جلسه معماری", "hours": "۳ ساعت"}],
            "problems": ["قطعی اینترنت"],
            "next_week_plans": ["ادامه مستندسازی"],
        }
    )
    report = WeeklyReport.model_validate(payload)
    assert report.employee_name == "علی"
    assert report.department_or_project == "دانا"
    assert report.needs_classification[0].description == "جلسه معماری"
    assert report.needs_classification[0].hours == 3
    assert report.problems_and_suggestions == ["قطعی اینترنت"]
    assert report.next_week_planned[0].description == "ادامه مستندسازی"
