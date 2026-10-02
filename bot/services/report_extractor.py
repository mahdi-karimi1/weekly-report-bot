"""LLM-based weekly report extraction for the FRN Excel template categories."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from bot.config import Settings
from bot.models.report import WEEKLY_REPORT_INLINE_SCHEMA, WeeklyReport
from bot.services.avalai_client import AvalAIClient, AvalAIError
from bot.utils.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
شما استخراج‌کننده گزارش هفتگی برای فرم سازمانی FRN هستید.
ورودی متن فارسی گزارش (گفتاری یا تایپ‌شده) است. خروجی فقط JSON معتبر است.

دسته‌ها:
1) planned_completed — انجام‌شده طبق برنامه
2) unplanned_completed — انجام‌شده خارج از برنامه / اضطراری / درخواست‌شده ناگهانی
3) self_initiated — خودخواسته / مطالعه شخصی مرتبط با کار
4) next_week_planned — برنامه هفته آینده
5) problems_and_suggestions — مشکلات و پیشنهادات
6) needs_classification — کار انجام‌شده‌ای که نوعش مبهم است؛ این موارد در نهایت طبق برنامه ثبت می‌شوند
7) suggested_next_week_tasks — ۳ تا ۴ پیشنهاد منطقی برای هفته بعد بر اساس کارهای انجام‌شده
   (این‌ها برنامه قطعی کاربر نیستند؛ فقط پیشنهاد)

قوانین حیاتی:
- اگر در متن کار/فعالیت/جلسه/تسک ذکر شده، هرگز همه آرایه‌ها را خالی برنگردان.
- وقتی نوع فعالیت انجام‌شده مشخص نیست، آن را در needs_classification بگذار (نه اینکه حذفش کنی)؛ کد ربات آن را به‌طور پیش‌فرض طبق برنامه ثبت می‌کند.
- برنامه‌هایی که خود کاربر برای هفته بعد گفته را فقط در next_week_planned بگذار.
- پیشنهادهای خودت را فقط در suggested_next_week_tasks بگذار؛ عنوان کوتاه (زیر ۴۰ نویسه).
- ساعت را فقط اگر صریحاً گفته شده به‌صورت عدد بنویس؛ وگرنه فیلد hours را نیاور یا null بگذار. صفر حدس نزن.
- چیزی از خودت نساز، ولی محتوای گفته‌شده را از دست نده.
- فقط JSON برگردان.
"""

USER_PROMPT_TEMPLATE = """\
متن گزارش:

---
{transcription}
---

JSON با این کلیدها برگردان:
employee_name, report_date_range, department_or_project,
planned_completed, unplanned_completed, self_initiated,
next_week_planned, problems_and_suggestions, needs_classification,
suggested_next_week_tasks

هر آیتم فعالیت: {{"description": "...", "hours": عدد یا null}}
اگر مطمئن نیستی فعالیت انجام‌شده در کدام ستون برود، حتماً needs_classification را پر کن؛ این موارد به‌طور پیش‌فرض طبق برنامه ثبت می‌شوند.
برای suggested_next_week_tasks سه یا چهار پیگیری منطقی هفته بعد پیشنهاد بده.
"""

EMPTY_RETRY_PROMPT_TEMPLATE = """\
JSON قبلی خالی بود، اما متن ورودی حاوی کار است. این غیرقابل قبول است.

متن:
---
{transcription}
---

JSON خالی قبلی:
{bad_json}

دوباره استخراج کن. حداقل فعالیت‌های گفته‌شده را در needs_classification قرار بده
اگر دسته‌شان مبهم است. مشکلات را در problems_and_suggestions بگذار.
فقط JSON.
"""

SALVAGE_PROMPT_TEMPLATE = """\
از متن زیر فقط فهرست کارهای ذکرشده را استخراج کن.
همه فعالیت‌های انجام‌شده را در needs_classification بگذار.
برنامه آینده را در next_week_planned و مشکلات را در problems_and_suggestions بگذار.
ساعت فقط اگر گفته شده.

متن:
---
{transcription}
---

فقط JSON با کلیدهای استاندارد فرم FRN.
"""

REPAIR_PROMPT_TEMPLATE = """\
JSON نامعتبر بود.

خطا:
{errors}

JSON:
{bad_json}

متن:
---
{transcription}
---

JSON معتبر اصلاح‌شده را برگردان. فعالیت‌های موجود را حذف نکن.
"""

CORRECTION_PROMPT_TEMPLATE = """\
گزارش فعلی:
{current_json}

اصلاح کاربر:
---
{correction}
---

گزارش را به‌روز کن. اگر کاربر دسته فعالیت مبهم را مشخص کرد،
از needs_classification به دسته درست منتقل کن. فقط JSON.
اگر دسته یک فعالیت انجام‌شده پس از اصلاح هنوز مشخص نیست، آن را در planned_completed
قرار بده؛ پیش‌فرض فعالیت‌های دسته‌بندی‌نشده «طبق برنامه» است.
"""


class ReportExtractionError(Exception):
    """Raised when structured extraction fails after retries."""


class EmptyReportExtractionError(ReportExtractionError):
    """Raised when the model returns a schema-valid but empty report."""


class ReportExtractor:
    def __init__(self, settings: Settings, avalai: AvalAIClient) -> None:
        self.settings = settings
        self.avalai = avalai
        self.max_repair_attempts = 2

    async def extract(self, transcription: str) -> WeeklyReport:
        transcription = (transcription or "").strip()
        if not transcription:
            logger.warning("Extraction skipped: transcription is empty")
            raise ReportExtractionError("Cannot extract report from empty transcription.")

        logger.info(
            "Extraction starting transcription_chars=%s",
            len(transcription),
        )

        # Prefer native GenAI for extraction: AvalAI OpenAI-compatible often
        # fails to serve gemini-3.1-pro-preview reliably for this account/route.
        raw = await self._generate_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT_TEMPLATE.format(transcription=transcription),
            use_schema=True,
            temperature=0.2,
        )
        logger.info("Extraction raw_response_chars=%s", len(raw or ""))

        try:
            report = self._parse_and_validate(raw)
        except (ValidationError, json.JSONDecodeError, ReportExtractionError) as first_error:
            logger.warning(
                "Initial extraction validation failed: %s",
                type(first_error).__name__,
            )
            report = await self._repair(transcription, raw, first_error)

        if not report.has_meaningful_content():
            raw_keys: list[str] = []
            try:
                raw_keys = list(_extract_json_object(raw).keys())
            except Exception:  # noqa: BLE001
                raw_keys = []
            logger.warning(
                "Extraction empty after first pass; retrying. fields=%s keys=%s",
                report.populated_fields_summary(),
                raw_keys,
            )
            report = await self._retry_nonempty(transcription, report)

        if not report.has_meaningful_content():
            logger.warning(
                "Extraction still empty after retries. fields=%s",
                report.populated_fields_summary(),
            )
            raise EmptyReportExtractionError(
                "Structured extraction returned no meaningful activities."
            )

        defaulted = report.classify_unresolved_as_planned()
        if defaulted:
            logger.info(
                "Unresolved activities defaulted to planned count=%s",
                defaulted,
            )
        logger.info("Extraction succeeded fields=%s", report.populated_fields_summary())
        return report

    async def extract_voice_update(
        self,
        transcription: str,
        current_report: WeeklyReport,
    ) -> WeeklyReport:
        """Extract one report update and merge it into an existing weekly report."""
        update = await self.extract(transcription)
        merged = current_report.model_copy(deep=True)
        merged.merge_voice_update(update)
        return merged

    async def _retry_nonempty(self, transcription: str, previous: WeeklyReport) -> WeeklyReport:
        # 1) Retry with schema + stronger prompt
        try:
            raw = await self._generate_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=EMPTY_RETRY_PROMPT_TEMPLATE.format(
                    transcription=transcription,
                    bad_json=previous.model_dump_json(),
                ),
                use_schema=True,
                temperature=0.0,
            )
            report = self._parse_and_validate(raw)
            if report.has_meaningful_content():
                return report
        except (ValidationError, json.JSONDecodeError, ReportExtractionError, AvalAIError) as exc:
            logger.warning("Empty retry with schema failed: %s", type(exc).__name__)

        # 2) Retry without JSON schema enforcement (mime/json prompt only)
        try:
            raw = await self._generate_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=EMPTY_RETRY_PROMPT_TEMPLATE.format(
                    transcription=transcription,
                    bad_json=previous.model_dump_json(),
                ),
                use_schema=False,
                temperature=0.0,
            )
            report = self._parse_and_validate(raw)
            if report.has_meaningful_content():
                return report
        except (ValidationError, json.JSONDecodeError, ReportExtractionError, AvalAIError) as exc:
            logger.warning("Empty retry without schema failed: %s", type(exc).__name__)

        # 3) Salvage: force activities into needs_classification
        try:
            raw = await self._generate_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=SALVAGE_PROMPT_TEMPLATE.format(transcription=transcription),
                use_schema=False,
                temperature=0.0,
            )
            return self._parse_and_validate(raw)
        except (ValidationError, json.JSONDecodeError, ReportExtractionError, AvalAIError) as exc:
            logger.warning("Salvage extraction failed: %s", type(exc).__name__)
            return previous

    async def _generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        use_schema: bool,
        temperature: float,
    ) -> str:
        return await self.avalai.chat_completion_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_json_schema=WEEKLY_REPORT_INLINE_SCHEMA if use_schema else None,
            temperature=temperature,
            prefer_native=True,
        )

    async def apply_correction(self, report: WeeklyReport, correction_text: str) -> WeeklyReport:
        prompt = CORRECTION_PROMPT_TEMPLATE.format(
            current_json=report.model_dump_json(indent=2),
            correction=correction_text.strip(),
        )
        raw = await self._generate_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            use_schema=True,
            temperature=0.1,
        )
        try:
            updated = self._parse_and_validate(raw)
        except (ValidationError, json.JSONDecodeError, ReportExtractionError) as err:
            updated = await self._repair(
                transcription=correction_text,
                bad_json=raw,
                error=err,
                base_context=report.model_dump_json(),
            )
        updated.classify_unresolved_as_planned()
        logger.info("Correction applied fields=%s", updated.populated_fields_summary())
        return updated

    async def _repair(
        self,
        transcription: str,
        bad_json: str,
        error: Exception,
        base_context: str | None = None,
    ) -> WeeklyReport:
        current = bad_json
        last_error: Exception = error
        for attempt in range(1, self.max_repair_attempts + 1):
            repair_prompt = REPAIR_PROMPT_TEMPLATE.format(
                errors=str(last_error),
                bad_json=current,
                transcription=(
                    transcription
                    if base_context is None
                    else f"{transcription}\n\nContext:\n{base_context}"
                ),
            )
            try:
                current = await self._generate_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=repair_prompt,
                    use_schema=(attempt == 1),
                    temperature=0.0,
                )
                return self._parse_and_validate(current)
            except (ValidationError, json.JSONDecodeError, ReportExtractionError, AvalAIError) as exc:
                logger.warning("Repair attempt %s failed: %s", attempt, type(exc).__name__)
                last_error = exc
        raise ReportExtractionError(
            f"Failed to extract a valid weekly report after repair: {last_error}"
        ) from last_error

    def _parse_and_validate(self, raw: str) -> WeeklyReport:
        try:
            payload = normalize_extraction_payload(_extract_json_object(raw))
            return WeeklyReport.model_validate(payload)
        except ValidationError as exc:
            logger.warning("Pydantic validation error count=%s", exc.error_count())
            raise


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_extraction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map alternate LLM shapes into the FRN WeeklyReport schema."""
    data = dict(payload)

    alias_lists = {
        "planned_completed": [
            "planned_completed",
            "planned",
            "completed_planned",
            "according_to_plan",
        ],
        "unplanned_completed": [
            "unplanned_completed",
            "unplanned",
            "completed_unplanned",
            "out_of_plan",
        ],
        "self_initiated": [
            "self_initiated",
            "self_initiated_activities",
            "voluntary",
        ],
        "next_week_planned": [
            "next_week_planned",
            "next_week",
            "next_steps",
            "next_week_plans",
        ],
        "problems_and_suggestions": [
            "problems_and_suggestions",
            "problems",
            "problems_summary",
            "suggestions",
        ],
        "needs_classification": [
            "needs_classification",
            "unclassified",
            "ambiguous_activities",
            "activities",
            "completed_summary",
            "unfinished_summary",
        ],
        "suggested_next_week_tasks": [
            "suggested_next_week_tasks",
            "suggested_next_week",
            "next_week_suggestions",
        ],
    }

    normalized: dict[str, Any] = {
        "employee_name": _blank_to_none(
            data.get("employee_name") or data.get("name") or data.get("person_name")
        ),
        "report_date_range": _blank_to_none(
            data.get("report_date_range")
            or data.get("week_range")
            or data.get("week_start")
            or data.get("date_range")
        ),
        "department_or_project": _blank_to_none(
            data.get("department_or_project")
            or data.get("project_name")
            or data.get("department")
            or data.get("project")
        ),
        "planned_completed": [],
        "unplanned_completed": [],
        "self_initiated": [],
        "next_week_planned": [],
        "problems_and_suggestions": [],
        "needs_classification": [],
        "suggested_next_week_tasks": [],
    }

    # If both week_start and week_end exist, combine them.
    if not normalized["report_date_range"]:
        start = _blank_to_none(data.get("week_start"))
        end = _blank_to_none(data.get("week_end"))
        if start and end:
            normalized["report_date_range"] = f"{start} تا {end}"
        elif start or end:
            normalized["report_date_range"] = start or end

    used_keys: set[str] = set()
    for target, aliases in alias_lists.items():
        for alias in aliases:
            if alias in data and alias not in used_keys:
                if target == "problems_and_suggestions":
                    normalized[target] = _normalize_string_list(data.get(alias))
                elif target == "needs_classification" and alias in {
                    "activities",
                    "completed_summary",
                    "unfinished_summary",
                }:
                    # Only use generic buckets if dedicated FRN lists are empty.
                    if not any(
                        normalized[k]
                        for k in (
                            "planned_completed",
                            "unplanned_completed",
                            "self_initiated",
                            "needs_classification",
                        )
                    ):
                        normalized["needs_classification"] = _normalize_activity_list(
                            data.get(alias),
                            default_reason="دسته‌بندی از متن مبهم بود",
                        )
                else:
                    if target == "needs_classification":
                        normalized[target] = _normalize_activity_list(data.get(alias))
                    else:
                        normalized[target] = _normalize_activity_list(data.get(alias))
                used_keys.add(alias)
                break

    return normalized


def _blank_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(
                item.get("description")
                or item.get("text")
                or item.get("activity_description")
                or ""
            ).strip()
        else:
            text = str(item).strip()
        if text:
            items.append(text)
    return items


def _normalize_activity_list(value: Any, default_reason: str | None = None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        item: dict[str, Any] = {"description": text, "hours": None}
        if default_reason:
            item["reason"] = default_reason
        return [item]
    if not isinstance(value, list):
        return []

    items: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            text = entry.strip()
            if not text:
                continue
            item = {"description": text, "hours": None}
            if default_reason:
                item["reason"] = default_reason
            items.append(item)
            continue
        if not isinstance(entry, dict):
            continue
        description = str(
            entry.get("description")
            or entry.get("activity_description")
            or entry.get("text")
            or entry.get("title")
            or ""
        ).strip()
        if not description:
            continue
        item = {
            "description": description,
            "hours": _coerce_hours(entry.get("hours") or entry.get("approximate_hours")),
        }
        reason = entry.get("reason")
        if reason:
            item["reason"] = str(reason).strip()
        elif default_reason:
            item["reason"] = default_reason
        items.append(item)
    return items


def _coerce_hours(value: Any) -> float | None:
    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    text = str(value).translate(_PERSIAN_DIGITS)
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    number = float(match.group(1))
    return number if number >= 0 else None


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ReportExtractionError("Structured output root must be a JSON object.")
    return data
