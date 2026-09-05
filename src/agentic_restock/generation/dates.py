"""Day-index to date-key conversion.

Every pair's history *ends* at `TODAY`, but the windows have different lengths (the history-depth
cohorts in docs/dataset_generator_spec.md §3.1). So a day index is only meaningful relative to
its own series length: index `n-1` is always today, index 0 is `n-1` days ago.

Getting this backwards — anchoring index 0 to a fixed start date — would put short-history pairs'
data three years in the past, where a trailing-window estimator would see nothing at all.
"""

from __future__ import annotations

from datetime import date, timedelta

TODAY = date(2026, 9, 4)


def date_for(day_index: int, series_length: int) -> date:
    """Calendar date for a day index within a series of `series_length` days, ending today."""
    return TODAY - timedelta(days=series_length - 1 - day_index)


def date_key(value: date) -> int:
    """`yyyyMMdd` integer, the form every fact table's *_DATE_KEY column uses."""
    return value.year * 10000 + value.month * 100 + value.day


def day_index_key(day_index: int, series_length: int) -> int:
    return date_key(date_for(day_index, series_length))


def day_of_year(day_index: int, series_length: int) -> int:
    return date_for(day_index, series_length).timetuple().tm_yday


def start_day_of_year(series_length: int) -> int:
    """Day-of-year at index 0 — the phase the seasonal model needs to line up with the calendar."""
    return day_of_year(0, series_length)
