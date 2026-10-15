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
    store.writer("discovery").insert("jobs", row)
    return job_id
