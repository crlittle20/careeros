"""Leads you have looked at, and your decisions about them. The alert-email reader and lead resolution (discovery) create
leads and link them to their postings; here only your own actions change a lead, each one recorded:
- dismiss a lead (it leaves the review page);
- confirm or reject a LIKELY match (2026-11-02; ADR 0003, amendment of that date): one posting at the lead's employer
  with a near-identical title, the same seniority and a compatible location, which lead resolution never links by itself.
  Confirm links the lead to that job. Reject keeps the lead open, and that job is never proposed or linked for it again.
- unlink a lead from a job it was linked to by mistake (a correction, with your reason; it becomes new again).
Confirm, reject and unlink are idempotent (a receipt), a lead_matches row (append-only, actor `user`) and an event in the
audit chain. Confirm and reject must name the job they are about (the review page's form does, and `careeros leads` prints
the command with `--job`; Codex CR-2026-11-02-LEAD-RESOLUTION F5), and apply only when that job is the likely match waiting
now, so a stale page or command records nothing.
"""
from __future__ import annotations

import uuid

from careeros.db.store import Store

OPEN = ("new", "no_match")


def dismiss_lead(store: Store, lead_id: str, *, key: str) -> str:
    if store.one("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)) is None:
        raise ValueError("unknown_lead")
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("dismiss_lead", key, {"lead_id": lead_id}, actor_id="user") == "replay":
            return "replay"
        writer.update("leads", {"status": "dismissed"}, f"lead_id = ? AND status IN ({','.join('?' * len(OPEN))})", (lead_id, *OPEN))
        writer.event("lead_dismissed", outcome="informational", entity_type="lead", entity_id=lead_id,
                     actor_type="human", actor_id="user")
    return "new"


def pending(store: Store, lead_id: str, policy):
    """The likely match waiting for you on this lead (its lead_matches row), or None: the lead's latest decision must be
    `likely`, the lead still open, the job still open, and the proposal still eligible (`eligible`)."""
    lead = store.one("SELECT status FROM leads WHERE lead_id = ?", (lead_id,))
    if lead is None or lead["status"] not in OPEN:
        return None
    row = store.one("SELECT * FROM lead_matches WHERE lead_id = ? ORDER BY decided_at DESC, rowid DESC LIMIT 1", (lead_id,))
    if row is None or row["decision"] != "likely" or not row["job_id"]:
        return None
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],))
    if job is None or job["current_state"] in ("duplicate", "closed"):
        return None
    return row if eligible(store, policy, lead_id, row, job) else None


def eligible(store: Store, policy, lead_id: str, row, job) -> bool:
    """The proposal still meets the rules that made it (Codex CR-2026-11-02-LEAD-RESOLUTION F6), read now: the lead's
    employer and the job's resolve to the same one (employer_identity), and a proposal made after a scan (it names the
    board's mapping) still has its board admitted (board_registry.scan_gate: not paused, not retired). A proposal made from
    your own paste or add (no mapping) needs no admitted board, as when it was made. Otherwise it is hidden, and Confirm or
    Reject records nothing."""
    from types import SimpleNamespace

    from careeros.discovery import board_discovery as bd
    from careeros.discovery import board_registry as reg
    from careeros.discovery.lead_resolution import Resolver
    lead = store.one("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    from careeros.discovery.alerts import local_only_urls
    facts = bd.candidate_from_lead(lead, policy, local_only_urls(store)) if lead is not None else None
    if facts is None or facts.relationship != "employer":
        return False
    if row["mapping_id"]:
        employer = store.one("SELECT * FROM employers WHERE employer_id = ?", (job["employer_id"],)) if job["employer_id"] else None
        if employer is None or not reg.scan_gate(store, employer, policy, store.clock.now())[0]:
            return False
    resolver = Resolver(SimpleNamespace(store=store, policy=policy))
    key = resolver.key(facts.employer_name)
    return bool(key) and key == resolver.key(job["employer_name"], job["parent"])


def pending_all(store: Store, policy) -> list[dict]:
    """Every likely match waiting for you: the lead, the candidate job and why, oldest lead first."""
    out = []
    for lead in store.all(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY first_seen_at, lead_id", OPEN):
        row = pending(store, lead["lead_id"], policy)
        if row is not None:
            job = store.one("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],))
            out.append({"lead": lead, "match": row, "job": job})
    return out


def _candidate(store: Store, lead_id: str, job_id: str | None, policy):
    if store.one("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)) is None:
        raise ValueError("unknown_lead")
    if not job_id:   # Codex CR-2026-11-02-LEAD-RESOLUTION F5: never whatever proposal waits now, only the one you saw
        raise ValueError("job_required: name the job the likely match showed you (--job JOB_ID, as careeros leads prints it)")
    row = pending(store, lead_id, policy)   # read inside the caller's transaction, just before it records anything
    if row is None:
        raise ValueError("no_likely_match: this lead has no likely match waiting for you, or it no longer qualifies (its board "
                         "left the daily scan, or the employer no longer matches); see careeros leads")
    if job_id != row["job_id"]:
        raise ValueError(f"not_the_likely_match: the match waiting for this lead is {row['job_id']}, not {job_id}")
    return row


def confirm_likely(store: Store, lead_id: str, *, policy, job_id: str | None = None, note: str | None = None, key: str) -> str:
    """You confirm the likely match: the lead is linked to its job (decision `confirmed`, your statement)."""
    from careeros.discovery.lead_resolution import link
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("confirm_lead_match", key, {"lead_id": lead_id, "job_id": job_id}, actor_id="user") == "replay":
            return "replay"
        row = _candidate(store, lead_id, job_id, policy)
        said = (note or "").strip()
        reason = "you confirmed the likely match" + (f": {said}" if said else "")
        link(store, lead_id, row["job_id"], method="owner", mapping_id=row["mapping_id"], candidates=[row["job_id"]], reason=reason,
             decision="confirmed", actor="user", component="review")
    return "new"


def reject_likely(store: Store, lead_id: str, *, policy, job_id: str | None = None, reason: str | None = None, key: str) -> str:
    """You reject the likely match: the lead stays open, and that job is never proposed or linked for it again."""
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("reject_lead_match", key, {"lead_id": lead_id, "job_id": job_id}, actor_id="user") == "replay":
            return "replay"
        row = _candidate(store, lead_id, job_id, policy)
        now = store.clock.now()
        said = (reason or "").strip()
        why = "you rejected the likely match" + (f": {said}" if said else "")
        writer.insert("lead_matches", {"match_id": f"lm:{uuid.uuid4().hex[:16]}", "lead_id": lead_id, "job_id": row["job_id"],
                                       "mapping_id": row["mapping_id"], "method": "owner", "decision": "rejected",
                                       "candidates_json": row["candidates_json"], "decided_at": now, "audit": None, "audited_at": None,
                                       "actor": "user", "reason": why[:300]})
        writer.update("leads", {"status_reason": why[:300], "status_at": now}, "lead_id = ?", (lead_id,))
        writer.event("lead_match_rejected", outcome="informational", entity_type="lead", entity_id=lead_id, actor_type="human",
                     actor_id="user", metadata={"job_id": row["job_id"]})
    return "new"


def unlink_lead(store: Store, lead_id: str, *, reason: str, key: str) -> str:
    """You correct a wrong link (item 8 of the lead-resolution batch; ADR 0008: a later record, never an edit of history):
    the lead leaves the job it is linked to and becomes new again, so lead resolution matches it with its own facts after
    the next scan of its employer's admitted board; that job is never linked to it again. The earlier link stays in
    lead_matches; an `unlinked` row (actor `user`, your reason) and a `lead_unlinked` event in the audit chain record who,
    when and why. A reason is required."""
    why = (reason or "").strip()
    if not why:
        raise ValueError("reason_required: say why the link is wrong")
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("unlink_lead", key, {"lead_id": lead_id, "reason": why}, actor_id="user") == "replay":
            return "replay"
        lead = store.one("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
        if lead is None:
            raise ValueError("unknown_lead")
        if lead["status"] != "ingested" or not lead["job_id"]:
            raise ValueError(f"not_linked: the lead is {lead['status']} and linked to no job")
        now, job_id = store.clock.now(), lead["job_id"]
        writer.insert("lead_matches", {"match_id": f"lm:{uuid.uuid4().hex[:16]}", "lead_id": lead_id, "job_id": job_id,
                                       "mapping_id": None, "method": "owner", "decision": "unlinked", "candidates_json": "[]",
                                       "decided_at": now, "audit": None, "audited_at": None, "actor": "user", "reason": why[:300]})
        writer.update("leads", {"status": "new", "job_id": None, "status_reason": f"you unlinked it from {job_id}: {why}"[:300],
                                "status_at": now}, "lead_id = ? AND status = 'ingested'", (lead_id,))
        writer.event("lead_unlinked", outcome="success", entity_type="lead", entity_id=lead_id, actor_type="human", actor_id="user",
                     metadata={"job_id": job_id, "reason": why[:300]})
    return "new"
