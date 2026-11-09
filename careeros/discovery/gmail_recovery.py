"""Conservative bounded search recovery shared by Gmail API and IMAP adapters.

No network is initiated here: an injected authenticated read-only reader
executes the same authorized Gmail query, split only by time when too many
messages match. Missing/invalid time bounds fail closed rather than lose cards.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .gmail import GmailError

_NEWER = re.compile(r"(?<!\S)newer_than:(\d+)d\b", re.I)
_TIME_FILTER = re.compile(r"(?<!\S)(?:after|before):", re.I)
MIN_WINDOW_SECONDS = 60
MAX_MATCHES = 20000


def search_all(reader, query: str, now_iso: str) -> list[str]:
    """Return all matching IDs or raise; never return a partial Gmail search.

    The first attempt uses the owner's original authorized query. If it exceeds
    the adapter's per-search limit, split a time-bounded equivalent recursively.
    Windows overlap by one second; duplicate IDs are deduplicated in arrival
    order. A dense minute, conflicting filters or oversized batch fails visibly.
    """
    try:
        return reader.search(query)
    except GmailError as exc:
        if exc.code != "search_limit_reached":
            raise
    match = _NEWER.search(query)
    if not match or _TIME_FILTER.search(query):
        raise GmailError("recovery_query_unsupported", "Gmail search overflow requires one newer_than:Nd filter")
    days = int(match.group(1))
    if not 1 <= days <= 60:
        raise GmailError("recovery_window_out_of_range", "Gmail overflow recovery supports 1–60 days")
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("missing timezone")
        now = now.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise GmailError("recovery_clock_invalid", "Gmail recovery needs a valid UTC clock") from exc
    base = _NEWER.sub("", query).strip()
    start = int((now - timedelta(days=days, seconds=2)).timestamp())
    end = int((now + timedelta(seconds=2)).timestamp())

    def collect(left: int, right: int) -> list[str]:
        bounded = f"{base} after:{left - 1} before:{right + 1}"
        try:
            return reader.search(bounded)
        except GmailError as exc:
            if exc.code != "search_limit_reached":
                raise
            if right - left <= MIN_WINDOW_SECONDS:
                raise GmailError("recovery_window_overflow",
                                 "Too many alerts in a one-minute window; no messages silently skipped") from exc
            middle = (left + right) // 2
            first = collect(left, middle)
            second = collect(middle, right)
            return first + second

    all_ids = collect(start, end)
    if len(all_ids) > MAX_MATCHES:
        raise GmailError("recovery_total_overflow", "Gmail backfill exceeds the safe processing limit")
    return list(dict.fromkeys(all_ids))
