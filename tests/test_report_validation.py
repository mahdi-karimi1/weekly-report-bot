"""Tests for WeeklyReport validation against FRN categories."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bot.models.report import TimedActivity, UnclassifiedCompletedActivity, WeeklyReport
from bot.services.report_extractor import _extract_json_object


def test_single_planned_activity() -> None:
    report = WeeklyReport.model_validate(
        {
            "employee_name": "علی رضایی",
            "report_date_range": "۲۰ تا ۲۶ شهریور",
            "department_or_project": "سامانه گزارش",
            "planned_completed": [
                {"description": "پیاده‌سازی ماژول تبدیل گفتار", "hours": 8}
            ],
            "unplanned_completed": [],
            "self_initiated": [],
            "next_week_planned": [{"description": "تست یکپارچه", "hours": None}],
            "problems_and_suggestions": [],
            "needs_classification": [],
        }
    )
    assert len(report.planned_completed) == 1
    assert report.planned_completed[0].hours == 8
    assert report.next_week_planned[0].hours is None
    assert report.has_meaningful_content() is True


def test_needs_classification_and_overflow() -> None:
    report = WeeklyReport(
        needs_classification=[
            UnclassifiedCompletedActivity(description="کار مبهم", hours=2)
        ],
        planned_completed=[TimedActivity(description=f"p{i}") for i in range(16)],
    )
    assert report.has_pending_classification() is True


def test_unresolved_activities_default_to_planned_without_duplicates() -> None:
    report = WeeklyReport(
        planned_completed=[TimedActivity(description="کار تکراری", hours=1)],
        needs_classification=[
            UnclassifiedCompletedActivity(description="کار تکراری", hours=2),
            UnclassifiedCompletedActivity(description="کار جدید", hours=3),
        ],
    )

    moved = report.classify_unresolved_as_planned()

    assert moved == 1
    assert report.needs_classification == []
    assert [(item.description, item.hours) for item in report.planned_completed] == [
        ("کار تکراری", 1),
        ("کار جدید", 3),
    ]


def test_blank_strings_become_none() -> None:
    report = WeeklyReport.model_validate(
        {
            "employee_name": "  ",
            "report_date_range": "",
            "department_or_project": "",
            "planned_completed": [],
            "unplanned_completed": [],
            "self_initiated": [],
            "next_week_planned": [],
            "problems_and_suggestions": ["", "مشکل واقعی"],
            "needs_classification": [],
        }
    )
    assert report.employee_name is None
    assert report.report_date_range is None
    assert report.department_or_project is None
    assert report.problems_and_suggestions == ["مشکل واقعی"]


def test_invalid_hours_rejected() -> None:
    with pytest.raises(ValidationError):
        TimedActivity(description="x", hours=-1)


def test_extract_json_from_fenced_block() -> None:
    raw = """```json
{"employee_name": null, "planned_completed": [], "unplanned_completed": [],
 "self_initiated": [], "next_week_planned": [], "problems_and_suggestions": [],
 "needs_classification": []}
```"""
    data = _extract_json_object(raw)
    assert data["planned_completed"] == []


def test_roundtrip_json() -> None:
    report = WeeklyReport(
        self_initiated=[TimedActivity(description="مطالعه")],
        problems_and_suggestions=["قطع شبکه"],
    )
    restored = WeeklyReport.model_validate_json(report.model_dump_json())
    assert restored.problems_and_suggestions == ["قطع شبکه"]
    assert json.loads(report.model_dump_json())["self_initiated"][0]["description"] == "مطالعه"
