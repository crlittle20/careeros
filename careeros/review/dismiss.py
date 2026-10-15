"""Explicit dismissals (2026-10-12, Part 5). Not applying to a job is not evidence that it was unsuitable: only a
recorded dismissal, with its reason, says that. Dismissals are append-only history and never delete anything."""
from __future__ import annotations

import uuid

from careeros.db.store import Store

REASONS = ("pay", "location", "remote", "schedule", "seniority", "skills", "qualifications", "domain", "employer",
           "duplicate", "closed", "applied_elsewhere", "not_interested", "other")


def dismiss(store: Store, job_id: str, reason: str, text: str | None = None, *, actor: str = "user") -> str:
    if reason not in REASONS:
        raise ValueError(f"unknown_reason: use one of {', '.join(REASONS)}")
    if not store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
        raise ValueError("unknown_job")
    dismissal_id = f"dis:{uuid.uuid4().hex[:16]}"
    writer = store.writer("review")
    with store.tx():
        writer.insert("dismissals", {"dismissal_id": dismissal_id, "job_id": job_id, "reason_code": reason,
                                     "reason_text": (text or "").strip()[:300] or None, "at": store.clock.now(), "actor": actor})
        writer.event("job_dismissed", outcome="success", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id=actor, metadata={"reason": reason})
    return dismissal_id


def latest(store: Store, job_id: str):
    return store.one("SELECT * FROM dismissals WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
