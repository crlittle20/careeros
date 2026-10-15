"""Hiring-outcome funnel and source performance (prompts v2.2, completed in v2.5).

recommended (Tier A/B/near-miss in PRIMARY or NIGHT) -> approved (your Apply/Stretch label) -> applied -> responded
(a real person replied) -> screening -> interviewing -> offer -> hired (accepted), plus the negative outcomes
rejected / closed / withdrawn / ghosted / declined. Every application's furthest stage is read from its audited history.
Automatic confirmations ("acknowledged") never count as a response; "responded" or any later stage does. Rates use
applications as the denominator. Small samples are labelled as such: one result is an observation, not a trend.
Nothing here changes a rule: the funnel informs your decisions, it never tunes CareerOS by itself.
"""
from __future__ import annotations

import json
from collections import defaultdict

from careeros.db.store import Store

TOP = ("recommended", "approved")
STAGES = ("applied", "responded", "screening", "interviewing", "offer", "hired")
NEGATIVES = ("rejected", "closed", "withdrawn", "ghosted", "declined")
REACHED = {
    "responded": {"responded", "screening", "interviewing", "final_interview", "offer", "accepted", "declined"},
    "screening": {"screening", "interviewing", "final_interview", "offer", "accepted", "declined"},
    "interviewing": {"interviewing", "final_interview", "offer", "accepted", "declined"},
    "offer": {"offer", "accepted", "declined"},
    "hired": {"accepted"},
}
DIMENSIONS = ("source", "family", "profile", "tier", "channel", "domain", "night_mode", "employment_type", "employer")
SMALL_SAMPLE = 10


def applications_with_stages(store: Store) -> list[dict]:
    out = []
    for app in store.all("SELECT * FROM applications ORDER BY applied_at"):
        seen = {r["to_state"] for r in store.all("SELECT to_state FROM transitions WHERE workflow_id = ?", (app["workflow_id"],))}
        seen.add(app["current_state"])
        context = json.loads(app["context_json"] or "{}")
        context["channel"] = "agency" if app["channel"] == "agency" else ("referral" if app["channel"] == "referral" else "direct")
        stages = {"applied"} | {stage for stage, states in REACHED.items() if seen & states}
        out.append({"application_id": app["application_id"], "context": context, "stages": stages,
                    "outcome": app["current_state"] if app["current_state"] in NEGATIVES else None})
    return out


def top_of_funnel(store: Store) -> list[dict]:
    """Jobs CareerOS recommended (latest PRIMARY tier A/B/near-miss, or NIGHT A/B/near-miss) and the ones you approved
    (latest label Apply or Stretch, either profile), each with its current context for the funnel dimensions."""
    from .applications import application_context
    recommended = {r["job_id"] for r in store.all(
        """SELECT j.job_id FROM jobs j JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id
           ORDER BY at DESC, rowid DESC LIMIT 1) WHERE j.current_state = 'scored' AND s.tier IN ('A','B','near_miss')
           AND j.variant_of IS NULL""")}
    recommended |= {r["job_id"] for r in store.all(
        """SELECT n.job_id FROM night_results n WHERE n.result_id = (SELECT result_id FROM night_results WHERE job_id = n.job_id
           ORDER BY at DESC, rowid DESC LIMIT 1) AND n.decision = 'eligible' AND n.tier IN ('A','B','near_miss')""")}
    approved = {r["job_id"] for r in store.all(
        """SELECT l.job_id FROM labels l WHERE l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id
           ORDER BY at DESC, rowid DESC LIMIT 1) AND l.label IN ('apply','stretch')""")}
    out = []
    for job_id in sorted(recommended | approved):
        context = application_context(store, job_id)
        context["channel"] = "not applied"
        out.append({"job_id": job_id, "context": context,
                    "stages": ({"recommended"} if job_id in recommended else set()) | ({"approved"} if job_id in approved else set())})
    return out


def funnel(store: Store, by: str = "source") -> list[dict]:
    if by not in DIMENSIONS:
        raise ValueError(f"unknown_dimension:{by}")
    groups: dict = defaultdict(lambda: {stage: 0 for stage in TOP + STAGES + NEGATIVES})
    for job in top_of_funnel(store):
        key = job["context"].get(by) or "unknown"
        for stage in job["stages"]:
            groups[key][stage] += 1
    for app in applications_with_stages(store):
        key = app["context"].get(by) or "unknown"
        for stage in app["stages"]:
            groups[key][stage] += 1
        if app["outcome"]:
            groups[key][app["outcome"]] += 1
    rows = []
    for key, counts in sorted(groups.items(), key=lambda kv: (-kv[1]["applied"], -kv[1]["recommended"], str(kv[0]))):
        applied = counts["applied"]
        rows.append({by: key, **counts,
                     "response_rate": round(counts["responded"] / applied, 3) if applied else None,
                     "interview_rate": round(counts["interviewing"] / applied, 3) if applied else None,
                     "offer_rate": round(counts["offer"] / applied, 3) if applied else None,
                     "small_sample": applied < SMALL_SAMPLE})
    return rows


def discovered_by_source(store: Store) -> dict:
    """Jobs worth a look (scored Tier A/B/near-miss) by where they came from: the top of each source's funnel."""
    from .applications import source_of
    counts: dict = defaultdict(int)
    for job in store.all("""SELECT j.* FROM jobs j JOIN scores s ON s.score_id = (SELECT score_id FROM scores
                             WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                             WHERE j.current_state = 'scored' AND s.tier IN ('A','B','near_miss') AND j.variant_of IS NULL"""):
        counts[source_of(store, job)[0]] += 1
    return dict(counts)
