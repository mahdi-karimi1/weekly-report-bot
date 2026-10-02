from __future__ import annotations

import jdatetime

from bot.utils.calendar import (
    format_shamsi_range,
    get_shamsi_week,
    parse_shamsi_date,
)


def test_shamsi_reporting_week_is_saturday_through_wednesday() -> None:
    week = get_shamsi_week(jdatetime.date(1405, 7, 5))

    assert week.start == jdatetime.date(1405, 7, 4)
    assert week.end == jdatetime.date(1405, 7, 8)
    assert week.display_range == "1405/07/04 الی 1405/07/08"


def test_parse_shamsi_date_accepts_persian_and_latin_digits() -> None:
    assert parse_shamsi_date("۱۴۰۵/۰۷/۰۴") == jdatetime.date(1405, 7, 4)
    assert parse_shamsi_date("1405-07-04") == jdatetime.date(1405, 7, 4)
    assert parse_shamsi_date("1405/13/04") is None
    assert parse_shamsi_date("تاریخ نامعتبر") is None


def test_format_shamsi_range_uses_persian_digits_and_month_name() -> None:
    assert format_shamsi_range("1405/07/01 الی 1405/07/04") == (
        "۱ مهر ۱۴۰۵ تا ۴ مهر ۱۴۰۵"
    )