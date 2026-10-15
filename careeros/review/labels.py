"""Your labels (the acceptance data) and agreement metrics against the system's decisions.

PRIMARY and NIGHT are labelled and measured separately (prompts v2.2): a NIGHT label never counts toward PRIMARY
precision or false-reject rate, and vice versa.
"""
from __future__ import annotations

import uuid

from careeros.db.store import Store

LABELS = ("apply", "stretch", "skip")
PROFILES = ("primary", "night")


def record_label(store: Store, job_id: str, label: str, reason: str | None, key: str, profile: str = "primary") -> str:
    if label not in LABELS:
        raise ValueError("unknown_label")
    if profile not in PROFILES:
        raise ValueError("unknown_profile")
    job = store.one("SELECT job_id, current_state FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    writer = store.writer("review")
    with store.tx():
        payload = {"job_id": job_id, "label": label, "reason": reason or "", "profile": profile}
        if writer.receipt("label", key, payload if profile != "primary" else {k: v for k, v in payload.items() if k != "profile"},
                          actor_id="user") == "replay":
            return "replay"
        if profile == "night":
            night = store.one("SELECT tier, decision FROM night_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
            system_tier, system_decision = (night["tier"], night["decision"]) if night else (None, None)
        else:
            score = store.one("SELECT tier FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
            system_tier, system_decision = (score["tier"] if score else None), job["current_state"]
        writer.insert("labels", {"label_id": f"label:{uuid.uuid4().hex}", "job_id": job_id, "at": store.clock.now(), "label": label,
                                 "reason": (reason or "").strip()[:300] or None, "system_tier": system_tier,
                                 "system_decision": system_decision, "profile": profile})
        writer.event("job_labelled", outcome="informational", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id="user", metadata={"label": label, "system_state": system_decision, "profile": profile})
    return "new"


def agreement(store: Store, profile: str = "primary") -> dict:
    """Latest label per job (for this profile) vs the system's current view for that profile."""
    if profile == "night":
        return _night_agreement(store)
    rows = store.all("""SELECT l.job_id, l.label, j.current_state,
                          (SELECT tier FROM scores s WHERE s.job_id = l.job_id ORDER BY at DESC, rowid DESC LIMIT 1) AS tier
                        FROM labels l JOIN jobs j ON j.job_id = l.job_id
                        WHERE j.exclude_from_kpi = 0 AND l.profile = 'primary'
                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id AND profile = 'primary'
                                            ORDER BY at DESC, rowid DESC LIMIT 1)""")
    picked = [r for r in rows if r["current_state"] == "scored" and r["tier"] in ("A", "B")]
    wanted = [r for r in rows if r["label"] in ("apply", "stretch")]
    false_rejects = [r for r in wanted if r["current_state"] == "rejected" or (r["current_state"] == "scored" and r["tier"] == "archive")]
    agreed = [r for r in picked if r["label"] in ("apply", "stretch")]
    unexplained = store.one("""SELECT COUNT(*) c FROM jobs j JOIN transitions t ON t.workflow_id = j.workflow_id
                               AND t.sequence = j.state_sequence WHERE j.current_state = 'rejected'
                               AND (t.reason_code IS NULL OR t.reason_code = '')""")["c"]
    return {
        "profile": "primary",
        "labelled": len(rows),
        "system_picks_labelled": len(picked),
        "precision_of_picks": round(len(agreed) / len(picked), 3) if picked else None,
        "you_would_apply": len(wanted),
        "false_rejects": len(false_rejects),
        "false_reject_rate": round(len(false_rejects) / len(wanted), 3) if wanted else None,
        "false_reject_jobs": [r["job_id"] for r in false_rejects],
        "unexplained_rejections": unexplained,
        "targets": {"labelled_at_least": 50, "precision_of_picks_at_least": 0.9, "false_reject_rate_at_most": 0.1},
    }


def _night_agreement(store: Store) -> dict:
    rows = store.all("""SELECT l.job_id, l.label, l.reason, n.decision, n.tier, n.mode FROM labels l
                        JOIN jobs j ON j.job_id = l.job_id
                        LEFT JOIN night_results n ON n.result_id = (SELECT result_id FROM night_results WHERE job_id = l.job_id
                                                                     ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.exclude_from_kpi = 0 AND l.profile = 'night'
                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id AND profile = 'night'
                                            ORDER BY at DESC, rowid DESC LIMIT 1)""")

    def measure(subset: list) -> dict:
        picked = [r for r in subset if r["decision"] == "eligible" and r["tier"] in ("A", "B")]
        wanted = [r for r in subset if r["label"] in ("apply", "stretch")]
        false_rejects = [r for r in wanted if r["decision"] == "rejected" or r["tier"] == "archive"]
        schedule_misfits = [r for r in picked if r["label"] == "skip" and (r["reason"] or "").lower().startswith("schedule")]
        return {"labelled": len(subset), "system_picks_labelled": len(picked),
                "precision_of_picks": round(sum(r["label"] in ("apply", "stretch") for r in picked) / len(picked), 3) if picked else None,
                "false_reject_rate": round(len(false_rejects) / len(wanted), 3) if wanted else None,
                "schedule_fit_false_positive_rate": round(len(schedule_misfits) / len(picked), 3) if picked else None}

    return {"profile": "night", **measure(rows),
            "by_mode": {mode: measure([r for r in rows if r["mode"] == mode]) for mode in ("supplemental", "replacement")},
            "targets": {"labelled_at_least": 50, "precision_of_picks_at_least": 0.9, "false_reject_rate_at_most": 0.1,
                        "schedule_fit_false_positive_rate_at_most": 0.1}}
