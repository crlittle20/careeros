"""Explicit dismissals (2026-10-12, Part 5). Not applying to a job is not evidence that it was unsuitable: only a
recorded dismissal, with its reason, says that. Dismissals are append-only history and never delete anything.

A dismissal is undone by a later event, a reinstatement (the owner's decision "Confirmation plus undo", ADR 0007,
amendment of 2026-11-01): a new row in the same table whose reason code is `reinstated`. Nothing is updated or
deleted. The latest event decides: a job is dismissed while its latest row is a dismissal, and every reader asks that
through `dismissed_sql`, `is_dismissed` or `current` (the shortlist, package routing, the report, the label backlog,
the daily summary and the analytics export). A later dismissal dismisses it again."""
from __future__ import annotations

import uuid
from datetime import timedelta

from careeros.core.clock import format_utc, parse_utc, require_order
from careeros.db.store import Store

REASONS = ("pay", "location", "remote", "schedule", "seniority", "skills", "qualifications", "domain", "employer",
           "duplicate", "closed", "applied_elsewhere", "not_interested", "other")
REINSTATED = "reinstated"   # the reason code of a reinstatement: never one of REASONS, so it is never a dismissal
RECENT_DAYS = 14            # the review page's "Dismissed recently" section, where Undo is offered


class DismissalChanged(ValueError):
    """Undo was confirmed for a dismissal that is no longer the posting's latest event (Codex CR-2026-11-01-OBS-REVIEW,
    F3): a later dismissal, or a reinstatement, came in between. Nothing is recorded; the review page asks again."""


def dismissed_sql(job: str) -> str:
    """SQL that is true when the job in column `job` is dismissed now: its latest dismissals row (by time, then
    recording order) is a dismissal, not a reinstatement. Its own alias is `dl`: name the outer table otherwise."""
    return (f"COALESCE((SELECT dl.reason_code FROM dismissals dl WHERE dl.job_id = {job} ORDER BY dl.at DESC, dl.rowid DESC "
            f"LIMIT 1), '{REINSTATED}') != '{REINSTATED}'")


def latest(store: Store, job_id: str):
    """The job's latest dismissals row, a dismissal or a reinstatement, or None."""
    return store.one("SELECT * FROM dismissals WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def current(store: Store, job_id: str):
    """The dismissal in force for the job (its latest event, when that is a dismissal), or None."""
    row = latest(store, job_id)
    return row if row is not None and row["reason_code"] != REINSTATED else None


def is_dismissed(store: Store, job_id: str) -> bool:
    return current(store, job_id) is not None


def _record(store: Store, job_id: str, reason: str, text: str | None, actor: str, key: str | None, command: str,
            event: str, undoes: str | None = None) -> str:
    writer = store.writer("review")
    with store.tx():
        if key and writer.receipt(command, key, {"job_id": job_id, "reason": reason, "text": text or ""}, actor_id=actor) == "replay":
            return "replay"
        newest = latest(store, job_id)
        if undoes is not None and (newest["dismissal_id"] if newest else None) != undoes:   # F3: only the one Undo showed
            raise DismissalChanged("dismissal_changed: the posting's latest dismissal event is not the one you confirmed undoing")
        if command == "reinstate" and not is_dismissed(store, job_id):
            raise ValueError("not_dismissed: the posting's latest event is not a dismissal")
        before, now = latest(store, job_id), store.clock.now()
        require_order(before["at"] if before else None, now)   # an earlier time would leave the older event the latest
        dismissal_id = f"dis:{uuid.uuid4().hex[:16]}"
        writer.insert("dismissals", {"dismissal_id": dismissal_id, "job_id": job_id, "reason_code": reason,
                                     "reason_text": (text or "").strip()[:300] or None, "at": now, "actor": actor})
        writer.event(event, outcome="success", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id=actor, metadata={"reason": reason} if command == "dismiss" else
                     {"undoes": before["dismissal_id"], "dismissed_for": before["reason_code"]})
    return dismissal_id


def dismiss(store: Store, job_id: str, reason: str, text: str | None = None, *, actor: str = "user",
            key: str | None = None) -> str:
    """Record a dismissal. With `key` (the review page's one-time form key) a repeat of the same request, such as a double
    click on Confirm, records once and returns "replay"."""
    if reason not in REASONS:
        raise ValueError(f"unknown_reason: use one of {', '.join(REASONS)}")
    if not store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
        raise ValueError("unknown_job")
    return _record(store, job_id, reason, text, actor, key, "dismiss", "job_dismissed")


def reinstate(store: Store, job_id: str, text: str | None = None, *, actor: str = "user", key: str | None = None,
              undoes: str | None = None) -> str:
    """Undo the dismissal in force (`careeros dismiss JOB_ID --undo`, or Undo on the review page): a new row, never an
    update. Refused when the job's latest event is not a dismissal. `text` is your optional note; `key` as for dismiss.
    `undoes` is the dismissal the review page's confirmation showed: when it is no longer the latest event, nothing is
    recorded (DismissalChanged), checked in the same transaction as the write."""
    if not store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
        raise ValueError("unknown_job")
    return _record(store, job_id, REINSTATED, text, actor, key, "reinstate", "job_reinstated", undoes)


def recent(store: Store, days: int = RECENT_DAYS) -> list:
    """Jobs dismissed now whose dismissal is at most `days` old, newest first, with the posting's title and employer:
    the review page's "Dismissed recently" section."""
    since = format_utc(parse_utc(store.clock.now()) - timedelta(days=days))
    return store.all(f"""SELECT d.*, j.title, j.employer_name, j.url FROM dismissals d JOIN jobs j ON j.job_id = d.job_id
                         WHERE d.rowid = (SELECT rowid FROM dismissals WHERE job_id = d.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                           AND d.reason_code != '{REINSTATED}' AND d.at >= ? ORDER BY d.at DESC, d.rowid DESC""", (since,))
