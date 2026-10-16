"""Why a job is where it is (2026-10-13): one explanation per job, assembled only from what CareerOS already recorded
(the job row, its transitions, gate results, scores, NIGHT results, sightings, leads, labels, dismissals, applications
and the board's verification record). Nothing is recomputed here, so the explanation is the decision as it was made.

Every explanation answers the same 14 questions (FIELDS). A question that does not apply to a job says so and why
("not applicable: ..."), so an explanation is never silently incomplete.
"""
from __future__ import annotations

import json

from careeros.pipeline import TITLE_SCREEN
from careeros.qualify import certifications, experience

FIELDS = ("source", "first_seen_at", "last_seen_at", "employer", "verification", "duplicate_decision", "eligibility", "fit",
          "career_value", "routing", "status", "rejection_reason", "review_reason", "actions")


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _na(why: str) -> str:
    return f"not applicable: {why}"


def explain_job(store, policy, job_id: str) -> dict:
    from careeros.discovery import board_registry as reg
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError(f"unknown job: {job_id}")
    gate = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    score = store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    night = store.one("SELECT * FROM night_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    history = store.all("SELECT from_state, to_state, reason_code, notes, transitioned_at FROM transitions WHERE entity_id = ? "
                        "ORDER BY sequence", (job_id,))
    state = job["current_state"]
    comps = _loads(score["components_json"], {}) if score else {}
    out: dict = {}

    # source: where the posting came from, and every later sighting of it
    sightings = store.all("SELECT source_kind, evidence_level, observed_at, url FROM observations WHERE job_id = ? ORDER BY observed_at",
                          (job_id,))
    leads = store.all("SELECT lead_id, site, sender, first_seen_at FROM leads WHERE job_id = ? ORDER BY first_seen_at", (job_id,))
    out["source"] = {"kind": {"ats_api": "employer job board (public listing API)", "manual_url": "a single link or pasted text",
                              "fixture": "test data"}.get(job["source"], job["source"]),
                     "system": job["ats"], "board": job["employer_id"], "url": job["url"],
                     "sightings": [{"kind": s["source_kind"], "evidence": s["evidence_level"], "at": s["observed_at"]} for s in sightings],
                     "leads": [{"lead": lead["lead_id"], "site": lead["site"], "from": (lead["sender"] or "")[:40], "at": lead["first_seen_at"]}
                               for lead in leads]}
    out["first_seen_at"] = {"careeros": job["first_seen_at"], "employer_posted_at": job["posted_at"],
                            "employer_updated_at": job["source_updated_at"]}
    out["last_seen_at"] = {"careeros": job["last_seen_at"], "closed_at": job["closed_at"],
                           "missed_complete_scans": job["absent_scans"]}

    # employer and the verification of the board it came from
    employer = store.one("SELECT * FROM employers WHERE employer_id = ?", (job["employer_id"],)) if job["employer_id"] else None
    entity = reg.entity(store, employer["entity_id"]) if employer and employer["entity_id"] else None
    out["employer"] = {"name": job["employer_name"], "parent": job["parent"],
                       "record": entity["canonical_name"] if entity else None,
                       "official_domain": entity["official_domain"] if entity else None}
    mapping = reg.mapping_for_employer(store, employer) if employer else None
    if mapping:
        admitted, why = reg.scan_gate(store, employer, policy, store.clock.now())
        out["verification"] = {"board_status": mapping["verification_status"], "provenance": mapping["provenance"],
                               "handshake": mapping["handshake_status"], "identity": mapping["identity_check"],
                               "trust": mapping["board_trust_score"], "health": mapping["board_health_score"],
                               "career_fit": entity["career_fit_score"] if entity else None,
                               "scanned": admitted, "why": why, "paused": mapping["paused_reason"]}
    elif employer:
        out["verification"] = {"board_status": "not registered yet", "scanned": employer["admitted_via"] == "legacy"}
    else:
        out["verification"] = _na("graded from a single link or pasted text; no board is scanned for it")

    # duplicate decision
    links = store.all("SELECT other_id, relation, evidence FROM opening_links WHERE job_id = ? OR other_id = ?", (job_id, job_id))
    if state == "duplicate":
        reason = next((h["reason_code"] for h in reversed(history) if h["to_state"] == "duplicate"), None)
        out["duplicate_decision"] = {"decision": "duplicate", "of": job["duplicate_of"], "reason": reason}
    else:
        out["duplicate_decision"] = {"decision": "distinct opening", "requisition": job["requisition_id"],
                                     "variant_of": job["variant_of"], "repost_of": job["repost_of"],
                                     "related": [{"job": r["other_id"], "relation": r["relation"], "evidence": r["evidence"]} for r in links]}

    # eligibility
    if gate is None:
        out["eligibility"] = _na("a duplicate is never judged on its own") if state == "duplicate" else \
            f"not judged yet (state {state})"
    else:
        failures = _loads(gate["hard_failures_json"], [])
        years_rejection = next((experience.rejection_block(f) for f in failures if f.get("code") == "years_far_short"), None)
        experience_block = comps.get("experience") or years_rejection
        cert_records = comps.get("certifications") or [m for f in failures + _loads(gate["flags_json"], []) for m in f.get("certifications") or []]
        out["eligibility"] = {"decision": gate["decision"],
                              "stage": "title screen" if gate["extraction_fingerprint"] == TITLE_SCREEN else
                              "review" if gate["decision"] == "needs_review" else "eligibility rules",
                              "hard_failures": failures, "flags": _loads(gate["flags_json"], []),
                              "facts": comps.get("facts", []), "decided_at": gate["at"],
                              # Experience Match (2026-10-15): as scored, or as recorded with a years rejection
                              "experience": experience.explain(experience_block) if experience_block else None,
                              # Certifications (2026-10-15): as scored, or as recorded with the gate's decision
                              "certifications": certifications.explain(cert_records) if cert_records else None}

    # fit, career value and routing (only scored jobs have them)
    not_scored = _na("rejected before scoring") if state == "rejected" else _na("duplicate") if state == "duplicate" else \
        _na("parked for review") if state == "needs_review" else _na(f"not scored (state {state})")
    if score:
        out["fit"] = {"candidate_fit": score["candidate_fit"], "eligibility_score": score["eligibility"], "state": score["state"],
                      "components": {k: comps.get(k) for k in ("eligibility", "candidate_fit") if k in comps}}
        out["career_value"] = {"score": score["career_value"], "components": comps.get("career_value")}
        out["routing"] = {"tier": score["tier"], "apply_priority": score["apply_priority"], "interview_band": score["interview_band"],
                          "stretch": bool(comps.get("stretch")), "stretch_reason": comps.get("stretch_reason")}
    else:
        out["fit"] = out["career_value"] = not_scored
        out["routing"] = {"tier": None, "why": not_scored}
    if isinstance(out["routing"], dict):
        out["routing"]["night"] = ({"mode": night["mode"], "decision": night["decision"], "tier": night["tier"]} if night
                                   else _na("not assessed for overnight work"))

    out["status"] = {"state": state, "since": job["entered_state_at"],
                     "history": [{"from": h["from_state"], "to": h["to_state"], "reason": h["reason_code"], "at": h["transitioned_at"]}
                                 for h in history[-12:]], "transitions": len(history)}

    # rejection and review reasons
    if state == "rejected" and gate is not None:
        failure = (_loads(gate["hard_failures_json"], []) or [{}])[0]
        out["rejection_reason"] = {"code": failure.get("code"), "detail": failure.get("detail"), "quote": failure.get("source_text")}
    elif state == "rejected":
        out["rejection_reason"] = {"code": history[-1]["reason_code"] if history else None}
    elif state == "duplicate":
        out["rejection_reason"] = {"code": "duplicate", "detail": f"duplicate of {job['duplicate_of']}"}
    elif state == "closed":
        out["rejection_reason"] = _na("not rejected; closed: " + (next((h["reason_code"] for h in reversed(history)
                                                                        if h["to_state"] == "closed"), None) or "closed"))
    else:
        out["rejection_reason"] = _na("not rejected")
    if state == "needs_review":
        last = next((h for h in reversed(history) if h["to_state"] == "needs_review"), None)
        out["review_reason"] = {"code": last["reason_code"] if last else None, "detail": last["notes"] if last else None}
    elif score and (score["state"] != "ELIGIBLE" or comps.get("decisive_unknowns")):
        out["review_reason"] = {"state": score["state"], "flags": _loads(gate["flags_json"], []) if gate else [],
                                "check_before_applying": comps.get("decisive_unknowns") or []}
    else:
        out["review_reason"] = _na("nothing needs your review")

    out["actions"] = _actions(store, job, state, score)
    return {field: out[field] for field in FIELDS}


def _actions(store, job, state, score) -> dict:
    from careeros.review.shortlist import shortlist, shortlisted_job_ids
    job_id = job["job_id"]
    labels = store.all("SELECT label, reason, profile, at FROM labels WHERE job_id = ? ORDER BY at", (job_id,))
    dismissals = store.all("SELECT reason_code, reason_text, at FROM dismissals WHERE job_id = ? ORDER BY at", (job_id,))
    apps = store.all("SELECT application_id, channel, agency, applied_at, current_state FROM applications WHERE job_id = ? "
                     "ORDER BY applied_at", (job_id,))
    if job_id in shortlisted_job_ids(store):
        nxt = next((i["next_action"] for i in shortlist(store) if i["job_id"] == job_id), None)
    elif state in ("rejected", "duplicate", "closed"):
        nxt = "none"
    elif state == "needs_review":
        nxt = "read the posting yourself and label it"
    elif score:
        nxt = "label it Apply / Stretch / Skip on the review page"
    else:
        nxt = "none yet: waiting for the next processing step"
    return {"labels": [{"label": r["label"], "reason": r["reason"], "profile": r["profile"], "at": r["at"]} for r in labels],
            "dismissals": [{"reason": r["reason_code"], "note": r["reason_text"], "at": r["at"]} for r in dismissals],
            "applications": [{"id": a["application_id"], "channel": a["channel"], "agency": a["agency"], "applied_at": a["applied_at"],
                              "outcome": a["current_state"]} for a in apps],
            "next_action": nxt}


def render(explanation: dict, job_id: str) -> str:
    """Plain text for the command line: one block per question."""
    lines = [f"Why {job_id} is where it is"]
    for field in FIELDS:
        value = explanation[field]
        lines.append(f"\n{field.replace('_', ' ').upper()}")
        if isinstance(value, dict):
            for key, item in value.items():
                if item in (None, [], {}, ""):
                    continue
                text = json.dumps(item, ensure_ascii=False) if isinstance(item, (list, dict)) else str(item)
                lines.append(f"  {key}: {text[:400]}")
        else:
            lines.append(f"  {value}")
    return "\n".join(lines)
