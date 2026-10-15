"""One trusted UTC clock. Times supplied by pages, adapters or users are data, never event time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_PROFILE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("naive_datetime")
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(text: str) -> datetime:
    if not isinstance(text, str) or not _PROFILE.match(text):
        raise ValueError("timestamp_profile")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def from_external(value) -> str | None:
    """Best-effort conversion of a source timestamp (ISO text or epoch ms) to the UTC profile; None if unusable."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return format_utc(datetime.fromtimestamp(value / 1000, tz=timezone.utc))
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return format_utc(moment)
    except (ValueError, OverflowError, OSError):
        return None


def require_order(previous: str | None, current: str) -> None:
    if previous is not None and parse_utc(current) < parse_utc(previous):
        raise ValueError("out_of_order_event")


class Clock:
    def now(self) -> str:
        return format_utc(datetime.now(timezone.utc))


class FixedClock(Clock):
    """Deterministic clock for tests."""

    def __init__(self, start: str = "2026-10-10T12:00:00.000Z"):
        self._moment = parse_utc(start)

    def now(self) -> str:
        return format_utc(self._moment)

    def advance(self, **delta) -> None:
        self._moment += timedelta(**delta)
