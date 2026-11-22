"""UTC time helpers.

All business timestamps are timezone aware and normalised to UTC.  Intervals are
left-closed, right-open: ``[start, end)``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class TimeFormatError(ValueError):
    """A timestamp is missing a timezone or is otherwise unusable."""


def parse_ts(value: str | datetime, *, field: str = "time") -> datetime:
    """Parse an ISO-8601 timestamp and return an aware datetime in UTC.

    Naive timestamps are rejected instead of being silently interpreted as UTC:
    the source contract requires a timezone on every recorded instant.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value:
            raise TimeFormatError(f"{field} must be a non-empty ISO-8601 string")
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TimeFormatError(f"{field} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimeFormatError(f"{field} must include a timezone: {value!r}")
    return parsed.astimezone(UTC)


def format_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return parse_ts(value).isoformat().replace("+00:00", "Z")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Return ``[first day 00:00 UTC, first day of next month 00:00 UTC)``."""

    try:
        year_text, month_text = month.split("-")
        year, month_number = int(year_text), int(month_text)
        start = datetime(year, month_number, 1, tzinfo=UTC)
    except (ValueError, AttributeError) as exc:
        raise TimeFormatError(f"month must look like YYYY-MM: {month!r}") from exc
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=UTC)
    return start, end


def day_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return start, start + timedelta(days=1)
