"""Pydantic schemas aligned with the FRN weekly work-report Excel template."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReportStatus(str, Enum):
    RECEIVED = "received"
    TRANSCRIBED = "transcribed"
    EXTRACTED = "extracted"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompletedActivityCategory(str, Enum):
    PLANNED = "planned_completed"
    UNPLANNED = "unplanned_completed"
    SELF_INITIATED = "self_initiated"


TEMPLATE_ACTIVITY_ROWS = 15  # Excel rows 11–25 inclusive


class TimedActivity(BaseModel):
    """One activity text with optional approximate hours."""

    description: str
    hours: float | None = Field(default=None, ge=0)

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("hours", mode="before")
    @classmethod
    def _blank_hours_to_none(cls, value: Any) -> Any:
        if value == "" or value is False or value is None:
            return None
        return value


class UnclassifiedCompletedActivity(BaseModel):
    """Completed work that could not be confidently categorized."""

    description: str
    hours: float | None = Field(default=None, ge=0)
    reason: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("hours", mode="before")
    @classmethod
    def _blank_hours_to_none(cls, value: Any) -> Any:
        if value == "" or value is False or value is None:
            return None
        return value


class SuggestedNextWeekTask(TimedActivity):
    """Model-proposed next-week work; not written to Excel until accepted."""

    accepted: bool = False


class WeeklyReport(BaseModel):
    """Structured weekly report mapped to FRN..Work-Report.xlsx."""

    employee_name: str | None = None
    report_date_range: str | None = None
    department_or_project: str | None = None

    planned_completed: list[TimedActivity] = Field(default_factory=list)
    unplanned_completed: list[TimedActivity] = Field(default_factory=list)
    self_initiated: list[TimedActivity] = Field(default_factory=list)
    next_week_planned: list[TimedActivity] = Field(default_factory=list)
    problems_and_suggestions: list[str] = Field(default_factory=list)

    # Completed activities that need the user to choose a template category.
    needs_classification: list[UnclassifiedCompletedActivity] = Field(
        default_factory=list
    )
    # Follow-up ideas from the model; user must accept before they enter next_week_planned.
    suggested_next_week_tasks: list[SuggestedNextWeekTask] = Field(default_factory=list)

    @field_validator(
        "employee_name",
        "report_date_range",
        "department_or_project",
        mode="before",
    )
    @classmethod
    def _blank_optional_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("problems_and_suggestions", mode="before")
    @classmethod
    def _clean_problems(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [str(item).strip() for item in value if str(item).strip()]

    def has_meaningful_content(self) -> bool:
        """True when at least one template activity/problem field is populated."""
        return bool(
            self.planned_completed
            or self.unplanned_completed
            or self.self_initiated
            or self.next_week_planned
            or self.problems_and_suggestions
            or self.needs_classification
        )

    def has_pending_classification(self) -> bool:
        return bool(self.needs_classification)

    def classify_unresolved_as_planned(self) -> int:
        """Default activities with unknown completion category to planned work."""
        if not self.needs_classification:
            return 0
        existing = {activity.description.strip() for activity in self.planned_completed}
        moved = 0
        for activity in self.needs_classification:
            if activity.description.strip() not in existing:
                self.planned_completed.append(
                    TimedActivity(description=activity.description, hours=activity.hours)
                )
                existing.add(activity.description.strip())
                moved += 1
        self.needs_classification.clear()
        return moved

    def merge_voice_update(self, update: WeeklyReport) -> None:
        """Append newly extracted voice content while retaining this week's report."""
        activity_fields = (
            "planned_completed",
            "unplanned_completed",
            "self_initiated",
            "next_week_planned",
        )
        for field_name in activity_fields:
            current: list[TimedActivity] = getattr(self, field_name)
            additions: list[TimedActivity] = getattr(update, field_name)
            by_description = {
                item.description.strip().casefold(): item for item in current
            }
            for item in additions:
                key = item.description.strip().casefold()
                existing = by_description.get(key)
                if existing is None:
                    current.append(item.model_copy(deep=True))
                    by_description[key] = current[-1]
                elif existing.hours is None and item.hours is not None:
                    existing.hours = item.hours

        existing_problems = {item.strip().casefold() for item in self.problems_and_suggestions}
        for item in update.problems_and_suggestions:
            key = item.strip().casefold()
            if key not in existing_problems:
                self.problems_and_suggestions.append(item)
                existing_problems.add(key)

        existing_suggestions = {
            item.description.strip().casefold()
            for item in self.suggested_next_week_tasks
        }
        for item in update.suggested_next_week_tasks:
            key = item.description.strip().casefold()
            if key not in existing_suggestions:
                self.suggested_next_week_tasks.append(item.model_copy(deep=True))
                existing_suggestions.add(key)

        self.classify_unresolved_as_planned()

    def overflowing_categories(self, limit: int = TEMPLATE_ACTIVITY_ROWS) -> list[str]:
        checks = [
            ("فعالیت‌های انجام‌شده طبق برنامه", self.planned_completed),
            ("فعالیت‌های انجام‌شده خارج از برنامه", self.unplanned_completed),
            ("فعالیت‌های خودخواسته", self.self_initiated),
            ("فعالیت‌های هفته آینده", self.next_week_planned),
            ("مشکلات و پیشنهادات", self.problems_and_suggestions),
        ]
        return [label for label, items in checks if len(items) > limit]

    def populated_fields_summary(self) -> dict[str, Any]:
        return {
            "employee_name_set": bool(self.employee_name),
            "report_date_range_set": bool(self.report_date_range),
            "department_or_project_set": bool(self.department_or_project),
            "planned_completed_count": len(self.planned_completed),
            "unplanned_completed_count": len(self.unplanned_completed),
            "self_initiated_count": len(self.self_initiated),
            "next_week_planned_count": len(self.next_week_planned),
            "problems_count": len(self.problems_and_suggestions),
            "needs_classification_count": len(self.needs_classification),
            "suggested_next_week_count": len(self.suggested_next_week_tasks),
            "has_meaningful_content": self.has_meaningful_content(),
        }

    def pending_suggestions(self) -> list[SuggestedNextWeekTask]:
        return [item for item in self.suggested_next_week_tasks if not item.accepted]

    def accept_suggestion(self, index: int) -> SuggestedNextWeekTask | None:
        if index < 0 or index >= len(self.suggested_next_week_tasks):
            return None
        item = self.suggested_next_week_tasks[index]
        if not item.accepted:
            already = {
                activity.description.strip() for activity in self.next_week_planned
            }
            if item.description.strip() not in already:
                self.next_week_planned.append(
                    TimedActivity(description=item.description, hours=item.hours)
                )
            item.accepted = True
        return item

    def accept_all_suggestions(self) -> None:
        for index in range(len(self.suggested_next_week_tasks)):
            self.accept_suggestion(index)

    def dismiss_suggestions(self) -> None:
        self.suggested_next_week_tasks = []

    def reject_suggestion(self, index: int) -> SuggestedNextWeekTask | None:
        if index < 0 or index >= len(self.suggested_next_week_tasks):
            return None
        return self.suggested_next_week_tasks.pop(index)


WEEKLY_REPORT_JSON_SCHEMA: dict[str, Any] = WeeklyReport.model_json_schema()

# Gemini-friendly schema: avoid union null types that often yield empty structured output.
WEEKLY_REPORT_INLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "employee_name": {"type": "string"},
        "report_date_range": {"type": "string"},
        "department_or_project": {"type": "string"},
        "planned_completed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                },
                "required": ["description"],
            },
        },
        "unplanned_completed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                },
                "required": ["description"],
            },
        },
        "self_initiated": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                },
                "required": ["description"],
            },
        },
        "next_week_planned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                },
                "required": ["description"],
            },
        },
        "problems_and_suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "needs_classification": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["description"],
            },
        },
        "suggested_next_week_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hours": {"type": "number"},
                },
                "required": ["description"],
            },
        },
    },
    "required": [
        "planned_completed",
        "unplanned_completed",
        "self_initiated",
        "next_week_planned",
        "problems_and_suggestions",
        "needs_classification",
    ],
}

# Backward-compatible aliases referenced by older imports/tests during migration.
WeeklyActivity = TimedActivity
ActivityStatus = CompletedActivityCategory
