"""Eligibility, Candidate Fit, Career Value, Freshness and Apply Priority (v0.3 §7.2–7.7).

Each dimension is 0..1 with a short basis note. Unknown inputs score neutral (0.5) and are listed as unknowns:
they are never treated as zero, and never as a match. Weights come from the policy, so every score is traceable
to the policy fingerprint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from careeros.core.clock import parse_utc
from careeros.normalize.posting import contains_term, norm, seniority

from .gate import GateResult, annualize, state_for
from .profile import EVIDENCE_WEIGHT, Profile

# Bump when scoring logic changes: scores recorded under an older version are recomputed automatically.
SCORES_VERSION = "scores-2026-10-12f"
NEUTRAL = 0.5
# "principal" (2026-10-12): a strong seniority signal, not an automatic rejection; the requirements decide.
SENIORITY_RANK = {"entry": 1, "mid": 2, "unspecified": 2, "senior": 3, "principal": 4, "manager": 5}


@dataclass
class ScoreCard:
    eligibility: float
    candidate_fit: float
    career_value: float
    freshness: float
    apply_priority: float
    tier: str
    interview_band: str
    components: dict = field(default_factory=dict)
    state: str = "ELIGIBLE"


def _weighted(dims: dict, weights: dict) -> float:
    total = sum(weights.values())
    return round(100 * sum(dims[k]["value"] * w for k, w in weights.items()) / total, 1)


def _dim(value: float, basis: str, unknown: bool = False) -> dict:
    return {"value": round(max(0.0, min(1.0, value)), 3), "basis": basis, "unknown": unknown}


def _skill_matches(requirements, profile: Profile, types: tuple) -> tuple[list, list]:
    wanted = sorted({r.name for r in requirements if r.category == "skill" and r.requirement_type in types})
    matched = [(name, profile.skill(name)) for name in wanted if profile.skill(name)]
    missing = [name for name in wanted if not profile.skill(name)]
    return matched, missing


def _seniority_fit(job: dict, profile: Profile, policy) -> tuple[float, str, bool]:
    posting = seniority(job["title_norm"], policy.get("seniority_terms", default={}))
    mine = profile.level_rank()
    if mine is None:
        return NEUTRAL, f"posting level {posting}; your level not set", True
    gap = SENIORITY_RANK[posting] - mine
    value = {0: 1.0, 1: 0.7, -1: 0.85}.get(gap, 0.4 if gap > 0 else 0.6)
    return value, f"posting level {posting}", False


REQUIRED_TYPES = ("hard_required", "required_with_equivalency", "ambiguous")
IMPORTANCE_ORDER = ("generic", "supporting", "important", "core")


def skill_coverage(job, requirements, profile: Profile, policy) -> dict:
    """Weighted required-skill coverage (2026-10-12), deterministic and auditable: each required skill or domain gets an
    importance (core > important > supporting > generic) from the policy, promoted to core when the job title names it
    and one level when it is also one of the role's duties. Coverage = sum(weight x evidence) / sum(weight), so a
    missing peripheral tool (Jira, Tableau) costs far less than a missing core skill (RxClaim, reconciliation).
    Education, work authorization, clearance, licences and certifications are never counted (the gate judges them)."""
    cfg = policy.get("skill_importance", default={}) or {}
    weights = cfg.get("weights") or {"core": 4, "important": 2, "supporting": 1, "generic": 0.5}
    lists = {level: {norm(s) for s in cfg.get(level, []) or []} for level in ("core", "supporting", "generic")}
    aliases = policy.get("skill_aliases", default={}) or {}
    title = job.get("title_norm") or ""
    duties = {r.name for r in requirements if r.category == "skill" and r.requirement_type == "responsibility"}
    wanted: dict = {}
    for r in requirements:
        if r.requirement_type in REQUIRED_TYPES and r.category in ("skill", "domain"):
            wanted.setdefault((r.category, r.name), r.source_text)
    items = []
    for (category, name), source in sorted(wanted.items()):
        if category == "domain":
            level, basis = cfg.get("required_domains", "core"), "required domain"
            have = norm(name) in profile.domains()
            evidence = "direct" if have else None
        else:
            level = next((lv for lv in ("core", "supporting", "generic") if norm(name) in lists[lv]), cfg.get("default", "important"))
            basis = f"policy: {level}" if level != cfg.get("default", "important") else "listed skill"
            if any(contains_term(title, norm(w)) for w in aliases.get(name, [name])):
                level, basis = "core", "named in the job title"
            elif name in duties and level != "core":
                level = IMPORTANCE_ORDER[IMPORTANCE_ORDER.index(level) + 1]
                basis += "; also a stated duty (+1 level)"
            hit = profile.skill(name)
            evidence = (hit.get("evidence") or "adjacent") if hit else None
        weight = float(weights.get(level, 1))
        credit = EVIDENCE_WEIGHT.get(evidence, 0.4) if evidence else 0.0
        items.append({"requirement": name, "category": category, "importance": level, "weight": weight, "basis": basis,
                      "evidence": evidence or "none", "credit": credit, "source_text": (source or "")[:160]})
    total = sum(i["weight"] for i in items)
    coverage = round(sum(i["weight"] * i["credit"] for i in items) / total, 3) if total else None
    found = [i for i in items if i["credit"] > 0]
    return {"method": "weighted-v1", "weighted_coverage": coverage, "items": items,
            "naive_coverage": round(len(found) / len(items), 3) if items else None,
            "weight_found": round(sum(i["weight"] * i["credit"] for i in items), 2), "weight_total": total}


def eligibility(job, requirements, profile, gate: GateResult, policy) -> dict:
    required = REQUIRED_TYPES
    matched, missing = _skill_matches(requirements, profile, required)
    dims = {}
    coverage = skill_coverage(job, requirements, profile, policy)
    min_weight = float((policy.get("skill_importance", default={}) or {}).get("min_weight_to_judge", 0))
    if coverage["items"] and coverage["weight_total"] < min_weight:
        # e.g. only "Agile" and "documentation": too little to judge coverage on, so treated like "none listed"
        dims["skills_coverage"] = _dim(0.7, f"only {coverage['weight_total']:g} importance points of required skills listed "
                                            f"(under {min_weight:g}): too few to judge", True)
    elif coverage["items"]:
        core = [i for i in coverage["items"] if i["importance"] == "core"]
        dims["skills_coverage"] = _dim(coverage["weighted_coverage"],
                                       f"weighted {coverage['weight_found']:g} of {coverage['weight_total']:g} points; "
                                       f"{sum(1 for i in coverage['items'] if i['credit'] > 0)} of {len(coverage['items'])} required "
                                       f"skills/domains found ({sum(1 for i in core if i['credit'] > 0)} of {len(core)} core)")
    else:
        dims["skills_coverage"] = _dim(0.7, "no specific required skills listed", True)

    functional = [r for r in requirements if r.requirement_type in required and r.category in ("experience", "years_experience")]
    if functional:
        hits = [r for r in functional if profile.function(r.name) or profile.years_for(r.name) is not None]
        dims["functional_experience"] = _dim(len(hits) / len(functional), f"{len(hits)} of {len(functional)} experience areas")
    else:
        dims["functional_experience"] = _dim(0.7, "no specific experience areas listed", True)

    years_value, unknown = 1.0, False
    if gate.has_flag("years_gap"):
        years_value = 0.3
    elif gate.has_flag("years_slightly_short"):
        years_value = 0.7
    elif gate.has_flag("years_unknown"):
        years_value, unknown = NEUTRAL, True
    elif gate.has_flag("years_preferred_short"):
        years_value = 0.85
    senior_value, senior_basis, senior_unknown = _seniority_fit(job, profile, policy)
    dims["years_seniority"] = _dim((years_value + senior_value) / 2, senior_basis, unknown or senior_unknown)

    edu_flags = [f for f in gate.flags if f["code"] in ("education_unknown", "certification_check", "license_check", "clearance_check")]
    edu_gaps = [f for f in gate.flags if f["code"] in ("degree_equivalency", "certification_gap")]
    dims["education_certification"] = _dim(0.5 if edu_flags else (0.6 if edu_gaps else 1.0),
                                            "; ".join(f["code"] for f in edu_flags + edu_gaps) or "requirements met", bool(edu_flags))

    loc_flags = [f["code"] for f in gate.flags if f["code"] in ("commute_unknown", "home_state_unknown", "work_authorization_unknown",
                                                                  "citizenship_check", "remote_unknown")]
    dims["location_authorization"] = _dim(0.6 if loc_flags else 1.0, "; ".join(loc_flags) or "eligible location", bool(loc_flags))

    schedule = any(r.category in ("schedule", "travel") and r.requirement_type in required for r in requirements)
    employment_unknown = (job.get("employment_type") or "unknown") == "unknown"
    dims["employment_schedule"] = _dim(0.6 if schedule else (0.8 if employment_unknown else 1.0),
                                       "schedule/travel requirement" if schedule else f"type {job.get('employment_type')}", employment_unknown)
    weights = policy.get("weights", "eligibility")
    return {"score": _weighted(dims, weights), "dims": dims, "matched_skills": [m[0] for m in matched], "missing_skills": missing,
            "coverage": coverage}


def candidate_fit(job, requirements, profile, policy) -> dict:
    all_types = ("hard_required", "required_with_equivalency", "ambiguous", "preferred", "responsibility")
    matched, missing = _skill_matches(requirements, profile, all_types)
    text_norm = norm(job.get("description_text") or "")
    dims = {}

    functional_hits = [r for r in requirements if r.category in ("experience", "years_experience") and profile.function(r.name)]
    functional_total = [r for r in requirements if r.category in ("experience", "years_experience")]
    family_match = 0.6 if job.get("family") else 0.2
    share = (len(functional_hits) / len(functional_total)) if functional_total else (len(matched) / max(1, len(matched) + len(missing)))
    dims["functional_overlap"] = _dim(family_match + 0.4 * share, f"family {job.get('family')}; {len(functional_hits)} experience areas matched")

    posting_domains = {r.name for r in requirements if r.category == "domain"}
    mine = profile.domains()
    if posting_domains:
        overlap = len({d for d in posting_domains if norm(d) in mine}) / len(posting_domains)
        dims["domain_overlap"] = _dim(overlap, f"posting domains {sorted(posting_domains)}")
    else:
        dims["domain_overlap"] = _dim(NEUTRAL, "no domain stated", True)

    if matched or missing:
        dims["tools_overlap"] = _dim(len(matched) / (len(matched) + len(missing)), f"{len(matched)} of {len(matched) + len(missing)} tools/skills")
    else:
        dims["tools_overlap"] = _dim(NEUTRAL, "no tools listed", True)

    if matched:
        strength = sum(EVIDENCE_WEIGHT.get(m[1].get("evidence"), 0.4) for m in matched) / len(matched)
        dims["evidence_strength"] = _dim(strength, "evidence strength of matched skills")
        via_eq = sum(1 for m in matched if m[1].get("via") == "equivalence")
        transferable = sum(1 for m in matched if m[1].get("evidence") == "strongly_transferable")
        dims["transferable_quality"] = _dim(1.0 if not (via_eq or transferable) else 0.75, f"{via_eq} via approved equivalences")
    else:
        dims["evidence_strength"] = _dim(0.3, "no matched skills")
        dims["transferable_quality"] = _dim(0.4, "no matched skills")

    senior_value, senior_basis, senior_unknown = _seniority_fit(job, profile, policy)
    dims["seniority_alignment"] = _dim(senior_value, senior_basis, senior_unknown)
    dims["employer_familiarity"] = _dim(1.0 if profile.familiar_with(job.get("employer_name"), job.get("parent")) else 0.0, "familiar employer")

    terms = policy.get("stakeholder_terms", default=[])
    posting_terms = [t for t in terms if contains_term(text_norm, t)]
    dims["stakeholder_process"] = _dim(min(1.0, len(posting_terms) / 3) if posting_terms else NEUTRAL,
                                       f"{len(posting_terms)} stakeholder/process terms", not posting_terms)
    return {"score": _weighted(dims, policy.get("weights", "candidate_fit")), "dims": dims,
            "matched_skills": [m[0] for m in matched], "missing_skills": missing}


def career_value(job, profile, policy) -> dict:
    dims = {}
    comp = policy.get("compensation", default={})
    target = comp.get("target_annual") or (comp.get("target_hourly") * 2080 if comp.get("target_hourly") else None)
    floor = comp.get("floor_annual") or (comp.get("floor_hourly") * 2080 if comp.get("floor_hourly") else None)
    mid = None
    if job.get("comp_min") is not None or job.get("comp_max") is not None:
        lo = annualize(job.get("comp_min") or job.get("comp_max"), job.get("comp_period"))
        hi = annualize(job.get("comp_max") or job.get("comp_min"), job.get("comp_period"))
        mid = (lo + hi) / 2
    if mid is None or not target:
        dims["compensation"] = _dim(NEUTRAL, "pay not posted" if mid is None else "target pay not set", True)
    elif mid >= target:
        dims["compensation"] = _dim(1.0, "at or above target")
    elif floor and mid >= floor:
        dims["compensation"] = _dim(0.5 + 0.5 * (mid - floor) / max(1, target - floor), "between floor and target")
    else:
        dims["compensation"] = _dim(0.2, "below target")

    posting_level = SENIORITY_RANK[seniority(job["title_norm"], policy.get("seniority_terms", default={}))]
    mine = profile.level_rank()
    if mine is None:
        dims["progression"] = _dim(0.6, "your level not set", True)
    else:
        dims["progression"] = _dim(1.0 if posting_level > mine else (0.8 if posting_level == mine else 0.4), "title level vs current")

    stability = policy.get("stability", default={})
    dims["stability"] = _dim(stability.get(job.get("employment_type") or "unknown", 0.6), f"type {job.get('employment_type')}")

    values = policy.get("location_value", default={})
    remote = job.get("remote_type")
    dims["location"] = _dim(values.get("remote", 1.0) if remote == "remote" else values.get("hybrid_ok", 0.75) if remote == "hybrid"
                            else values.get("onsite_ok", 0.55) if remote == "onsite" else values.get("unknown", 0.5), f"{remote}")

    text_norm = norm((job.get("description_text") or "") + " " + job["title_norm"])
    growth = [t for t in policy.get("growth_skills", default=[]) if contains_term(text_norm, t)]
    dims["skill_growth"] = _dim(min(1.0, len(growth) / 3), f"growth skills: {', '.join(growth[:5]) or 'none'}")

    domain = domain_level(job, policy)
    dims["domain_relevance"] = _dim(domain["value"], f"domain: {domain['level']}"
                                    + (f" ({', '.join(domain['matched'][:3])})" if domain["matched"] else ""))
    return {"score": _weighted(dims, policy.get("weights", "career_value")), "dims": dims, "domain": domain}


def freshness(job, now: str, policy) -> dict:
    posted = job.get("posted_at") or None
    if not posted:
        return {"score": float(policy.get("freshness_unknown", default=50)), "days": None}
    days = (parse_utc(now) - parse_utc(posted)) / timedelta(days=1)
    for band in policy.get("freshness", default=[]):
        if days <= band["max_days"]:
            return {"score": float(band["score"]), "days": round(days, 1)}
    return {"score": 15.0, "days": round(days, 1)}


def domain_level(job: dict, policy) -> dict:
    """Prompts v2.2: direct PBM/RxClaim/pharmacy-claims > payer/claims/benefits > general healthcare > other.
    A Career Value component (highest matching level only), never an extra bonus on top of the priority."""
    levels = policy.get("domain_levels", default={}) or {}
    blob = norm(" ".join(filter(None, [job.get("title_norm"), job.get("industry"), job.get("employer_name"),
                                       (job.get("description_text") or "")[:6000]])))
    for name in ("direct", "payer", "healthcare"):
        level = levels.get(name) or {}
        hits = [t for t in level.get("terms", []) if contains_term(blob, norm(t))]
        if hits:
            return {"level": name, "value": float(level.get("value", 0.5)), "matched": hits[:5]}
    return {"level": "none", "value": float((levels.get("none") or {}).get("value", 0.3)), "matched": []}


def tier_for(priority: float, state: str, skill_coverage: float, policy) -> str:
    """Prompts v2.2 + your 2026-10-12 skill minimum: Tier A/B need state ELIGIBLE and enough required skills.
    CONDITIONAL and HOLD jobs never reach A/B (the report lists them in their own sections)."""
    tiers = policy.get("tiers")
    strong = state == "ELIGIBLE" and skill_coverage >= tiers.get("skill_coverage_min", 0.0)
    if strong and priority >= tiers["A"]["priority"]:
        return "A"
    if strong and priority >= tiers["B"]["priority"]:
        return "B"
    if priority >= tiers["near_miss"]["priority"]:
        return "near_miss"
    return "archive"


def interview_band(elig: float, fit: float) -> str:
    if elig >= 90 and fit >= 90:
        return "Very High"
    if elig >= 85 and fit >= 80:
        return "High"
    if elig >= 75 and fit >= 65:
        return "Moderate"
    return "Low"


def score(job: dict, requirements: list, profile: Profile, gate: GateResult, policy, now: str) -> ScoreCard:
    elig = eligibility(job, requirements, profile, gate, policy)
    fit = candidate_fit(job, requirements, profile, policy)
    value = career_value(job, profile, policy)
    fresh = freshness(job, now, policy)
    w = policy.get("weights", "priority")
    priority = round(w["candidate_fit"] * fit["score"] + w["career_value"] * value["score"] + w["freshness"] * fresh["score"], 1)
    state = state_for(gate, policy)
    coverage = elig["dims"]["skills_coverage"]["value"]
    tier = tier_for(priority, state, coverage, policy)
    stretch = gate.has_flag("degree_stretch")
    if stretch and tier in ("A", "B"):  # a stretch is shown with the near-misses, never as a top pick
        tier = "near_miss"
    from .facts import decisive_unknowns, eligibility_facts  # Part 5: eligibility evidence, kept apart from fit
    facts = eligibility_facts(job, requirements, gate, policy)
    return ScoreCard(elig["score"], fit["score"], value["score"], fresh["score"], priority, tier,
                     interview_band(elig["score"], fit["score"]),
                     {"eligibility": elig, "candidate_fit": fit, "career_value": value, "freshness": fresh,
                      "state": state, "skill_coverage": coverage, "domain": value["domain"],
                      "profile_approved": profile.approved, "stretch": stretch, "facts": facts,
                      "decisive_unknowns": decisive_unknowns(facts)}, state)
