"""Timezone-aware UTC timestamps. Naive datetimes are rejected (fail closed)."""

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class NaiveTimestamp(ValueError):
    pass


def parse_utc(value) -> datetime:
    """Parse an ISO-8601 string or aware datetime into an aware UTC datetime.

    Accepts '...Z' and explicit offsets. A timestamp without zone information
    is ambiguous and raises NaiveTimestamp rather than being guessed.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    else:
        raise TypeError(f"cannot parse timestamp from {type(value).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise NaiveTimestamp(f"timestamp has no timezone: {value!r}")
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """Canonical storage form: 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (sortable as text)."""
    if dt.tzinfo is None:
        raise NaiveTimestamp("refusing to serialise a naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utcnow() -> datetime:
    return datetime.now(UTC)


def floor_day(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier) / timedelta(days=1)
