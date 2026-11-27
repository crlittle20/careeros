"""Shared test helpers. All data here is synthetic."""
from __future__ import annotations

from careeros.core.clock import FixedClock
from careeros.core.transitions import load_matrix
from careeros.db.store import open_store
from careeros.paths import CONFIG_DIR

JOB_MATRIX = load_matrix(CONFIG_DIR / "workflows" / "job.json")


def new_store(clock=None, policy_fingerprint="test-policy"):
    clock = clock or FixedClock()
    return open_store(":memory:", clock, policy_fingerprint), clock


def add_job(store, job_id="gh:acme:1", **overrides):
    now = store.clock.now()
    row = {
        "job_id": job_id, "workflow_id": f"job:{job_id}", "employer_id": None, "employer_name": "Acme Health",
        "parent": "Acme Health", "source": "fixture", "ats": "greenhouse", "external_id": job_id.rsplit(":", 1)[-1],
        "title": "Business Systems Analyst", "title_norm": "business systems analyst", "first_seen_at": now,
        "last_seen_at": now, "content_fingerprint": "c" * 64, "current_state": "discovered", "state_sequence": 0,
        "entered_state_at": now,
    }
    row.update(overrides)
    # a fixture's text stands for what an intake route stores: the canonical form (the D190 design, section 3a)
    from careeros.db.store import CANONICAL_TEXT
    from careeros.normalize.sanitize import canonical
    row.update({k: canonical(v) for k, v in row.items() if k in CANONICAL_TEXT["jobs"] and isinstance(v, str)})
    store.writer("discovery").insert("jobs", row)
    return job_id


def give_label(store, policy, profile, job_id, label, reason=None, key=None, who="primary"):
    """A label given as you give it: Apply or Stretch from a preview of the job (the D190 design, section 3, "The label
    write"), under `policy` and your applicant `profile`; Skip with none. Fails the test when the job offers no preview."""
    import secrets

    from careeros.review.labels import capture, record_label, write_preview
    preview = None
    if label in ("apply", "stretch"):
        captured = capture(store, policy, profile, job_id, who)
        if captured["waiting"]:
            raise AssertionError(f"no preview for {job_id}: {captured['waiting']}")
        preview = write_preview(store, captured)
    return record_label(store, job_id, label, reason, key or f"test:{secrets.token_hex(6)}", who, preview=preview,
                        policy=policy, applicant=profile)


def make_previewable(store, policy, profile, job_id, decision="eligible"):
    """Give a fixture job built row by row what a read and a screening give it (the D190 design, section 3): its source
    record from its columns, the values the one derivation makes of that record, and a gate result current for it. For
    report tests whose cards must offer Apply from a preview."""
    import secrets
    from types import SimpleNamespace

    from careeros.normalize.posting import RawPosting
    from careeros.pipeline import (Context, decision_fingerprint, derive, source_digest, source_json, source_record)
    from careeros.review.labels import SHOWN_AS_READ, SHOWN_DERIVED
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    ats, token, external = job_id.split(":", 2) if job_id.count(":") >= 2 else ("greenhouse", "fixture", job_id)
    p = RawPosting(ats=job["ats"] or ats, board_token=token, external_id=job["external_id"] or external,
                   title=job["title"] or "", location=job["location_raw"] or "", url=job["url"], apply_url=job["apply_url"],
                   posted_at=job["posted_at"], description_text=job["description_text"] or "",
                   requisition_id=job["requisition_id"])
    record = source_record(p, {"employer_id": job["employer_id"], "name": job["employer_name"], "parent": job["parent"],
                               "resolved": "stored"})
    fields = derive(Context(store, policy, profile, None), record, job_id)
    stored = source_json(record)
    disc = store.writer("discovery")
    disc.update("jobs", {**{k: fields[k] for k in (*SHOWN_DERIVED, *SHOWN_AS_READ)}, "source_json": stored},
                "job_id = ?", (job_id,))
    store.writer("qualify").insert("gate_results", {
        "result_id": f"gate:{secrets.token_hex(6)}", "job_id": job_id, "at": store.clock.now(),
        "policy_fingerprint": decision_fingerprint(SimpleNamespace(policy=policy)), "profile_fingerprint": profile.fingerprint,
        "extraction_fingerprint": job["extraction_fingerprint"] or "unread", "decision": decision,
        "hard_failures_json": "[]", "flags_json": "[]", "source_digest": source_digest(stored)})
    return job_id


def stored_label(store, job_id, label, reason=None, key=None, who="primary"):
    """A labels row as a report reads it, written without a preview (an Apply or Stretch label given before the D190
    design, or by an older engine): for tests of what the report, the metrics and the agreement read from labels. P1
    never routes it (label_unconfirmed)."""
    import secrets

    from careeros.review.labels import LABELS, PROFILES, record_label
    if label in ("skip",):
        return record_label(store, job_id, label, reason, key or f"test:{secrets.token_hex(6)}", who)
    assert label in LABELS and who in PROFILES
    writer = store.writer("review")
    label_id = f"label:{secrets.token_hex(8)}"
    with store.tx():
        job = store.one("SELECT current_state FROM jobs WHERE job_id = ?", (job_id,))
        score = store.one("SELECT tier FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
        writer.insert("labels", {"label_id": label_id, "job_id": job_id, "at": store.clock.now(), "label": label,
                                 "reason": (reason or "").strip() or None, "system_tier": score["tier"] if score else None,
                                 "system_decision": job["current_state"] if job else None, "profile": who})
        writer.event("job_labelled", outcome="informational", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id="user", metadata={"label": label, "profile": who, "label_id": label_id})
    return "new"
