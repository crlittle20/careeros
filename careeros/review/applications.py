"""Applications you have made and their outcomes. Tracked from day one so response data starts accumulating.

One application per role: the role includes the job's location variants and duplicates. A second application
through a different channel (e.g. direct after an agency submitted you) is a right-to-represent conflict and is
refused unless explicitly allowed.
"""
from __future__ import annotations

import json
import uuid

from careeros.core.transitions import Matrix, TransitionRejected, load_matrix
from careeros.discovery.note import is_alert_source
from careeros.db.store import Store
from careeros.paths import CONFIG_DIR

CHANNELS = ("direct", "agency", "referral", "other")
OPEN_STATES = ("applied", "acknowledged", "screening", "interviewing", "final_interview", "offer", "ghosted")


class ApplicationConflict(ValueError):
    pass


def application_matrix() -> Matrix:
    return load_matrix(CONFIG_DIR / "workflows" / "application.json")


def role_group(store: Store, job_id: str, limit: int = 200) -> list[str]:
    """The job plus everything linked to it as the same role for applying purposes: location variants, duplicates
    and reposts, followed in both directions."""
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        return []
    members, frontier = {job_id}, [job_id]
    while frontier and len(members) < limit:
        marks = ",".join("?" * len(frontier))
        rows = store.all(f"SELECT job_id, variant_of, duplicate_of, repost_of FROM jobs WHERE job_id IN ({marks}) "
                         f"OR variant_of IN ({marks}) OR duplicate_of IN ({marks}) OR repost_of IN ({marks})", tuple(frontier) * 4)
        found = {x for r in rows for x in (r["job_id"], r["variant_of"], r["duplicate_of"], r["repost_of"]) if x}
        frontier = sorted(found - members)
        members |= found
    return sorted(members)


# ChatGPT tags jobs it found in your Gmail alerts in the issue Note: `source=linkedin_alert` (prompts v2.5) or the
# older free-form "source: LinkedIn alert" (v2.3/v2.4). Both are read by careeros.discovery.note.is_alert_source.


def source_of(store: Store, job) -> tuple[str, str]:
    """(discovery source, profile) for a job: ChatGPT's Gmail-alert finds (its `source: ... alert` Note tag) or its
    web finds via the GitHub inbox, an alert email CareerOS read itself, CareerOS's own board scan, or a manual import;
    NIGHT when it reached CareerOS only as a NIGHT recommendation."""
    lead = store.one("SELECT sender, profile, context FROM leads WHERE job_id = ? ORDER BY first_seen_at LIMIT 1",
                     (job["job_id"],))
    if lead:
        if (lead["sender"] or "").startswith("GitHub inbox"):
            source = "chatgpt_gmail_alert" if is_alert_source(lead["context"]) else "chatgpt_inbox"
        else:
            source = "alert_email"
        return source, lead["profile"] or "primary"
    return ("board_scan" if job["source"] == "ats_api" else "manual_import"), "primary"


def application_context(store: Store, job_id: str) -> dict:
    """What was known when you applied, so outcomes can later be measured by source, family, profile, tier, channel
    and domain (prompts v2.2 conversion analytics). Stored once; never rewritten."""
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    score = store.one("SELECT tier, state, apply_priority, components_json FROM scores WHERE job_id = ? "
                      "ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    source, profile = source_of(store, job)
    domain = (json.loads(score["components_json"]).get("domain") or {}).get("level") if score else None
    night = store.one("SELECT mode FROM night_results WHERE job_id = ? AND decision = 'eligible' ORDER BY at DESC, rowid DESC "
                      "LIMIT 1", (job_id,))
    return {"source": source, "profile": profile, "family": job["family"], "employer": job["employer_name"],
            "tier": score["tier"] if score and job["current_state"] == "scored" else None,
            "state": score["state"] if score else None, "priority": score["apply_priority"] if score else None,
            "domain": domain, "night_mode": night["mode"] if night else None, "employment_type": job["employment_type"]}


def _describe(app) -> str:
    return f"{app['channel']}{' (' + app['agency'] + ')' if app['agency'] else ''}"


def record_application(store: Store, matrix: Matrix, job_id: str, channel: str, *, agency: str | None = None,
                       notes: str | None = None, key: str, allow_second_channel: bool = False) -> str:
    if channel not in CHANNELS:
        raise ValueError("unknown_channel")
    agency = (agency or "").strip() or None
    if channel == "agency" and not agency:
        raise ValueError("agency_name_required")
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        raise ValueError("unknown_job")
    writer = store.writer("tracker")
    with store.tx():
        payload = {"job_id": job_id, "channel": channel, "agency": agency or "", "notes": notes or ""}
        if writer.receipt("record_application", key, payload, actor_id="user") == "replay":
            return "replay"
        group = role_group(store, job_id)
        marks = ",".join("?" * len(group))
        existing = store.all(f"SELECT * FROM applications WHERE job_id IN ({marks}) AND current_state IN "
                             f"({','.join('?' * len(OPEN_STATES))})", (*group, *OPEN_STATES))
        for app in existing:
            if app["channel"] == channel and (app["agency"] or "") == (agency or ""):
                raise ApplicationConflict(f"already_applied: you applied to this role on {app['applied_at'][:10]} via {_describe(app)}")
            if not allow_second_channel:
                raise ApplicationConflict(f"right_to_represent_conflict: this role already has an application via {_describe(app)}; "
                                          "applying through a second channel can disqualify you")
        now = store.clock.now()
        app_id = f"app:{uuid.uuid4().hex[:16]}"
        writer.insert("applications", {"application_id": app_id, "workflow_id": f"application:{app_id}", "job_id": job_id,
                                       "channel": channel, "agency": agency, "applied_at": now, "first_seen_at": now,
                                       "current_state": matrix.initial_state, "state_sequence": 0, "entered_state_at": now,
                                       "notes": (notes or "").strip()[:500] or None,
                                       "context_json": json.dumps(application_context(store, job_id))})
        writer.event("application_recorded", outcome="success", entity_type="application", entity_id=app_id,
                     workflow_id=f"application:{app_id}", actor_type="human", actor_id="user",
                     metadata={"job_id": job_id, "channel": channel, "agency": agency, "second_channel": bool(existing)})
    return app_id


def update_application(store: Store, matrix: Matrix, app_id: str, to_state: str, *, notes: str | None = None, key: str) -> str:
    writer = store.writer("tracker")
    with store.tx():
        if writer.receipt("update_application", key, {"app_id": app_id, "to": to_state, "notes": notes or ""}, actor_id="user") == "replay":
            return "replay"
        try:
            writer.transition(matrix, app_id, to_state, entity="application", trigger_type="human", trigger_id="user",
                              notes=(notes or "").strip()[:300] or None)
        except TransitionRejected as exc:
            raise ValueError(exc.code) from exc
    return "new"


def applications(store: Store) -> list:
    return store.all("""SELECT a.*, j.title, j.employer_name, j.url FROM applications a LEFT JOIN jobs j ON j.job_id = a.job_id
                        ORDER BY a.applied_at DESC""")
