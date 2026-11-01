"""Shadow recommendation policy v1 (SCORING_SHADOW_SPEC 2026-10-16, section 5). PURE: it turns a rubric Assessment
into a status, and a status plus your decisions into a queue. It never writes anything and never changes a production
tier; production keeps `scores.tier_for` until you adopt something explicitly.

Status S (CareerOS's own judgement) and actionability (S combined with your decisions and preferences) are separate:
evaluation always measures S, because actionability is partly built from your labels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from careeros.qualify.rubric import ABSENT, UNKNOWN, Assessment

ORDER = ("archive", "near_miss", "stretch_review", "check_first", "B", "A")   # low to high
CORE_ALTERNATIVES = ("R0", "R1", "R2", "R3")


def rank(status: str) -> int:
    return ORDER.index(status)


@dataclass
class Decision:
    numeric: str                     # N: before caps (or rejected / held)
    status: str                      # S: after caps
    branch: str                      # step1 | step2 | 3a .. 3f
    caps: list[tuple[str, str]] = field(default_factory=list)   # (reason, cap) that applied
    facts: list[str] = field(default_factory=list)              # facts to resolve (for Check First ordering)


def core_caps(assessment: Assessment, core: str) -> list[tuple[str, str]]:
    """R0 none; R1 a hard-required core item you don't have -> Stretch review; R2 also a hard-required core item with no
    profile evidence -> Check First; R3 also any core item unknown or absent -> Near-miss. Monotonic: R3 <= R2 <= R1 <= R0."""
    if core not in CORE_ALTERNATIVES:
        raise ValueError(f"unknown core alternative: {core}")
    items = assessment.core_items
    caps = []
    if core != "R0" and any(i["requirement_type"] == "hard_required" and i["state"] == ABSENT for i in items):
        caps.append(("core_confirmed_absent", "stretch_review"))
    if core in ("R2", "R3") and any(i["requirement_type"] == "hard_required" and i["state"] == UNKNOWN for i in items):
        caps.append(("core_no_profile_evidence", "check_first"))
    if core == "R3" and any(i["state"] in (UNKNOWN, ABSENT) for i in items):
        caps.append(("core_gap_any", "near_miss"))
    return caps


def decide(assessment: Assessment, *, state: str, degree_stretch: bool, cfg: dict, core: str = "R2") -> Decision:
    """The status for one job. `state` is the eligibility state: INELIGIBLE, HOLD, CONDITIONAL or ELIGIBLE."""
    if state == "INELIGIBLE":
        return Decision("rejected", "rejected", "step1")
    if state == "HOLD":
        return Decision("held", "held", "step2")
    t = cfg["thresholds"]
    q = assessment.qualification
    tolerated = set(t.get("tolerated_unknowns_for_a") or [])
    untolerated = [f for f in assessment.decisive_unknowns if f not in tolerated]
    eligible = state == "ELIGIBLE"
    listed_ok = assessment.listed_weight >= assessment.min_weight
    fully_known = math.isclose(q.coverage, 100.0)
    if (eligible and q.low >= t["tier_a"] and assessment.cov_low >= t["coverage_a"] and math.isclose(assessment.cov_known, 100.0)
            and listed_ok and assessment.gap in ("strong_match", "none") and not untolerated):
        numeric, branch = "A", "3a"
    elif (eligible and q.low >= t["tier_b"] and assessment.cov_low >= t["coverage_b"] and listed_ok
          and assessment.gap in ("strong_match", "small_gap", "none")):
        numeric, branch = "B", "3b"
    elif q.high >= t["tier_b"] and (not fully_known or state == "CONDITIONAL" or untolerated):
        numeric, branch = "check_first", "3c"
    elif q.low >= t["stretch"]:
        numeric, branch = "stretch_review", "3d"
    elif q.high >= t["near_miss"]:
        numeric, branch = "near_miss", "3e"
    else:
        numeric, branch = "archive", "3f"
    caps = []
    if degree_stretch:
        caps.append(("degree_stretch", "stretch_review"))
    if assessment.gap in ("medium_gap", "large_gap"):
        caps.append(("experience_gap", "stretch_review"))
    if assessment.gap == "severe_gap":
        caps.append(("severe_experience_gap", "near_miss"))
    caps += core_caps(assessment, core)
    status = min([numeric] + [cap for _, cap in caps], key=rank)
    facts = list(assessment.facts_to_resolve) + [f"{f} unknown" for f in untolerated]
    if state == "CONDITIONAL":
        facts.append("material fact unknown (CONDITIONAL)")
    return Decision(numeric, status, branch, [c for c in caps if rank(c[1]) < rank(numeric)], facts)


def actionability(latest_label: str | None, dismissed: bool, preference_conflict: bool) -> str:
    """Your decisions come first: a Skip or a dismissal declines the job; a confirmed dealbreaker preference conflict
    sets it aside for you to look at. Neither changes the status."""
    if dismissed or latest_label == "skip":
        return "declined"
    if preference_conflict:
        return "preference_conflict"
    return "actionable"


QUEUES = {"A": "apply", "B": "apply", "check_first": "check_first", "stretch_review": "stretch", "near_miss": "near_miss",
          "archive": "hidden", "rejected": "rejected", "held": "held"}


def queue(status: str, act: str) -> str:
    if status in ("rejected", "held"):
        return status
    if act == "declined":
        return "declined"
    if act == "preference_conflict":
        return "hidden" if status == "archive" else "preference_conflict"
    return QUEUES[status]


def lower_desirability(assessment: Assessment, cfg: dict) -> bool:
    """The annotation uses the UPPER Opportunity bound (no imputation): unknown pay never triggers it."""
    return assessment.opportunity.high < cfg["thresholds"]["opportunity_floor"]


def order_key(status: str, assessment: Assessment, decision: Decision, job_id: str, cfg: dict, *,
              check_first: str = "O1", opportunity: str = "OM1", avoid: bool = False) -> tuple:
    """Deterministic order inside a queue; ties broken by job id. A job matching an "avoid" preference comes after
    the others of the same status (an annotation only: its status is unchanged)."""
    q, o = assessment.qualification, assessment.opportunity
    opp = o.mid if opportunity == "OM1" else o.low
    last = 1 if avoid else 0
    if status in ("A", "B"):
        return (0 if status == "A" else 1, last, 1 if lower_desirability(assessment, cfg) else 0, -q.low, -opp, job_id)
    if status == "check_first":
        if check_first == "O1":
            return (last, -q.high, -q.coverage, job_id)
        return (last, -q.low, len(decision.facts), job_id)
    if status == "stretch_review":
        return (last, -q.low, -opp, job_id)
    return (last, -q.high, job_id)
