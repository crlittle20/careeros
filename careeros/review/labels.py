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


# --- three-way agreement (2026-10-15): apply, stretch and skip measured separately, for Experience Match calibration -----
SYSTEM_CLASSES = ("apply", "stretch", "near_miss_unflagged", "skip", "not_judged")


def system_class(state: str | None, tier: str | None, stretch: bool) -> str:
    """Tier A/B = apply; a near-miss carrying an explicit stretch flag = stretch; any other near-miss stays
    'near_miss_unflagged' (never counted as stretch); archive or rejected = skip; anything else is not judged."""
    if state == "rejected":
        return "skip"
    if tier in ("A", "B"):
        return "apply"
    if tier == "near_miss":
        return "stretch" if stretch else "near_miss_unflagged"
    if tier == "archive":
        return "skip"
    return "not_judged"


def _experience_for(store: Store, job, score, gate_row, policy, profile) -> tuple[dict | None, bool]:
    """(block, computed): the stored Experience Match, else one computed now from the stored requirements (a job
    rejected before scoring, or scored before 2026-10-15). Computing never writes anything."""
    import json

    from careeros.pipeline import load_requirements
    from careeros.qualify import experience
    from careeros.qualify.gate import GateResult
    comps = json.loads(score["components_json"]) if score else {}
    if comps.get("experience"):
        return comps["experience"], False
    if profile is None or not job["extraction_fingerprint"]:
        return None, False
    gate = GateResult(gate_row["decision"], json.loads(gate_row["hard_failures_json"] or "[]"),
                      json.loads(gate_row["flags_json"] or "[]")) if gate_row else None
    block = experience.evaluate(load_requirements(store, job), profile, policy, gate, score["tier"] if score else None,
                                comps.get("skill_coverage"), (comps.get("domain") or {}).get("level"))
    return block, True


def _hypothetical_class(current: str, block: dict | None, stretch: bool) -> str:
    if not block or current == "not_judged":
        return current
    tier, after = block.get("tier"), block.get("hypothetical_tier")
    if block.get("effect") == "reject_by_existing_gate" or after == "rejected":
        return "skip"
    if after == tier:
        return current
    return "stretch" if block.get("effect") == "stretch" else system_class("scored", after, stretch)


def three_way(store: Store, policy, profile=None) -> dict:
    """Your latest PRIMARY label per job against the system: a 3x3 matrix (plus near-misses without a stretch flag and
    jobs not judged), the misses that matter, the gap classes, and the agreement Experience Match WOULD give if its
    enforcement were switched on. Read only."""
    import json
    from collections import Counter

    from careeros.qualify import experience
    rows = store.all("""SELECT l.job_id, l.label FROM labels l JOIN jobs j ON j.job_id = l.job_id
                        WHERE l.profile = 'primary' AND j.exclude_from_kpi = 0
                          AND l.label_id = (SELECT label_id FROM labels WHERE profile = 'primary' AND job_id = l.job_id
                                            ORDER BY at DESC, rowid DESC LIMIT 1) ORDER BY l.job_id""")
    confusion = {label: dict.fromkeys(SYSTEM_CLASSES, 0) for label in LABELS}
    hypothetical = {label: dict.fromkeys(SYSTEM_CLASSES, 0) for label in LABELS}
    gaps: dict = {label: Counter() for label in LABELS}
    moves: Counter = Counter()
    apply_missed, stretch_missed, skip_picked = [], [], []
    computed = with_years = 0
    for row in rows:
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],))
        if job is None:
            continue
        score = store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (row["job_id"],))
        gate_row = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (row["job_id"],))
        stretch = bool(json.loads(score["components_json"]).get("stretch")) if score else False
        current = system_class(job["current_state"], score["tier"] if score else None, stretch)
        block, was_computed = _experience_for(store, job, score, gate_row, policy, profile)
        computed += was_computed
        if block and block.get("decisive_line") is not None \
                and block["lines"][block["decisive_line"]]["strength"] in experience.REQUIRED_TYPES:
            with_years += 1
        after = _hypothetical_class(current, block, stretch)
        label = row["label"]
        confusion[label][current] += 1
        hypothetical[label][after] += 1
        gaps[label][(block or {}).get("gap_class", "unknown")] += 1
        if after != current:
            moves[f"{current} -> {after}"] += 1
        if label == "apply" and current not in ("apply", "not_judged"):
            apply_missed.append(row["job_id"])
        if label == "stretch" and current not in ("stretch", "not_judged"):
            stretch_missed.append(row["job_id"])
        if label == "skip" and current == "apply":
            skip_picked.append(row["job_id"])

    def agreement_of(matrix: dict) -> float | None:
        judged = sum(n for label in LABELS for cls, n in matrix[label].items() if cls != "not_judged")
        agreed = sum(matrix[label][label] for label in LABELS)
        return round(agreed / judged, 3) if judged else None

    all_scored: Counter = Counter()
    for score in store.all("""SELECT s.components_json FROM scores s JOIN jobs j ON j.job_id = s.job_id
                               WHERE j.current_state = 'scored' AND s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id
                                                                                   ORDER BY at DESC, rowid DESC LIMIT 1)"""):
        block = json.loads(score["components_json"]).get("experience")
        all_scored[block["gap_class"] if block else "not_computed_yet"] += 1
    cfg = experience.settings(policy)["calibration"]
    ready = len(rows) >= cfg["min_labels"] and with_years >= cfg["min_years_labels"]
    return {
        "profile": "primary",
        "labelled": len(rows),
        "confusion": confusion,
        "agreement": agreement_of(confusion),
        "agreement_note": "exact matches over judged jobs; a near-miss without a stretch flag never counts as agreement",
        "apply_false_negatives": {"count": len(apply_missed), "jobs": apply_missed},
        "stretch_misclassified": {"count": len(stretch_missed), "jobs": stretch_missed},
        "skip_false_positives": {"count": len(skip_picked), "jobs": skip_picked},
        "false_reject_rate": agreement(store)["false_reject_rate"],
        "gap_classes": {"labelled": {label: dict(c) for label, c in gaps.items()}, "all_scored": dict(all_scored)},
        "if_experience_enforced": {"confusion": hypothetical, "agreement": agreement_of(hypothetical), "moves": dict(moves)},
        "experience_computed_for_this_report": computed,
        "calibration": {"labels": len(rows), "labels_with_years_requirement": with_years,
                        "needed": {"labels": cfg["min_labels"], "labels_with_years_requirement": cfg["min_years_labels"]},
                        "status": "EXPERIENCE_CALIBRATION_READY" if ready else "not_ready",
                        "enforced": bool(experience.settings(policy)["enforce"]),
                        "note": "ready only means enough labels to review the thresholds; enforcement stays the owner's decision"},
    }
