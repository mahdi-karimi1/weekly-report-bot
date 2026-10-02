"""Solar Hijri (Shamsi) work-week helpers. Saturday–Wednesday inclusive."""

from __future__ import annotations

from dataclasses import dataclass
import re

import jdatetime

_DATE_RE = re.compile(r"^\s*(\d{1,4})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{1,2})\s*$")
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_PERSIAN_DIGITS_ONLY = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_MONTH_NAMES = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)


@dataclass(frozen=True)
class ShamsiWeek:
    start: jdatetime.date
    end: jdatetime.date

    @property
    def start_str(self) -> str:
        return self.start.strftime("%Y/%m/%d")

    @property
    def end_str(self) -> str:
        return self.end.strftime("%Y/%m/%d")

    @property
    def week_key(self) -> str:
        return f"{self.start.strftime('%Y%m%d')}_{self.end.strftime('%Y%m%d')}"

    @property
    def display_range(self) -> str:
        return f"{self.start_str} الی {self.end_str}"


def get_shamsi_week(today: jdatetime.date | None = None) -> ShamsiWeek:
    """Active reporting week: Saturday (0) through Wednesday (4) in jdatetime."""
    current = today or jdatetime.date.today()
    saturday = current - jdatetime.timedelta(days=current.weekday())
    wednesday = saturday + jdatetime.timedelta(days=4)
    return ShamsiWeek(start=saturday, end=wednesday)


def parse_shamsi_date(value: str) -> jdatetime.date | None:
    """Parse a Persian-calendar date in YYYY/MM/DD or YYYY-MM-DD form."""
    normalized = value.translate(_PERSIAN_DIGITS)
    match = _DATE_RE.fullmatch(normalized)
    if match is None:
        return None
    try:
        year, month, day = (int(part) for part in match.groups())
        return jdatetime.date(year, month, day)
    except ValueError:
        return None


def format_shamsi_date(value: str) -> str:
    """Format a stored YYYY/MM/DD date with Persian digits and month name."""
    parsed = parse_shamsi_date(value)
    if parsed is None:
        return value
    month = _MONTH_NAMES[parsed.month - 1]
    return f"{parsed.day} {month} {parsed.year}".translate(_PERSIAN_DIGITS_ONLY)


def format_shamsi_range(value: str | None) -> str:
    """Format a stored Shamsi date range for user-facing Telegram messages."""
    if not value:
        return "نامشخص"
    parts = value.split(" الی ", maxsplit=1)
    if len(parts) != 2:
        return format_shamsi_date(value)
    return f"{format_shamsi_date(parts[0])} تا {format_shamsi_date(parts[1])}"
