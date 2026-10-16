"""Hard eligibility gate (v0.3 §7.1). A hard failure is never averaged away by a score.

Every failure and flag carries a machine code, a readable detail and, where it came from the posting, the
verbatim source line. Every hard failure also records the profile fact it was judged against and a confidence
(prompts v2.2: evidence-backed rejections; kept in the local database only). Unknown candidate facts produce flags,
not passes. `state_for` turns a result into ELIGIBLE / CONDITIONAL / HOLD / INELIGIBLE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from careeros.normalize.posting import contains_term, excluded_term, norm, pay_quote

from careeros.extract.model import requirement_strength

from . import certifications as certification_mod
from . import experience as experience_mod
from .profile import DEGREE_RANK, Profile, alternatives

# Bump when gate logic changes: results recorded under an older version are re-evaluated automatically.
RULES_VERSION = "gate-2026-10-15b"  # b: certifications named, matched by status, alternatives honoured; a: Experience Match
PROFESSIONAL_LICENSES = ["pharmacist", "pharmacy technician", "registered nurse", "rn license", "nursing license", "cpa",
                         "attorney", "bar admission", "lpn", "physician", "insurance license", "life and health"]
HOURS_PER_YEAR = 2080
YEARS_CATEGORIES = ("skill", "years_experience", "experience", "domain")
# Pay shown on these sites may be the site's own estimate: informational only, never grounds for rejection (v2.2).
AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "ziprecruiter.com", "simplyhired.com", "monster.com",
                    "careerbuilder.com", "salary.com", "talent.com", "jooble.org", "builtin.com", "wellfound.com")
STRONG_SCAM_TERMS = {"gift card", "wire transfer", "check deposit", "purchase equipment", "processing fee", "training fee", "crypto payment"}


@dataclass
class GateResult:
    decision: str                       # eligible | rejected
    hard_failures: list = field(default_factory=list)
    flags: list = field(default_factory=list)

    def fail(self, code, detail, source=None, profile=None, confidence=1.0, strength=None):
        self.hard_failures.append({"code": code, "detail": detail, "source_text": source, "profile_evidence": profile,
                                   "confidence": round(float(confidence), 2), "strength": strength})

    def flag(self, code, detail, source=None):
        self.flags.append({"code": code, "detail": detail, "source_text": source})

    def has_flag(self, code: str) -> bool:
        return any(f["code"] == code for f in self.flags)


def state_for(result: GateResult, policy) -> str:
    """INELIGIBLE (a hard failure), HOLD (employer-level hold), CONDITIONAL (a material fact unknown) or ELIGIBLE."""
    if result.hard_failures:
        return "INELIGIBLE"
    codes = {f["code"] for f in result.flags}
    if "held_d13" in codes:
        return "HOLD"
    material = set(policy.get("eligibility_states", "material_unknowns", default=[]) or [])
    return "CONDITIONAL" if codes & material else "ELIGIBLE"


def comp_trusted(job: dict) -> bool:
    """Pay is trusted for a hard rejection unless the posting came from an aggregator page (v2.2)."""
    host = urlsplit(job.get("url") or "").netloc.lower()
    return not any(a in host for a in AGGREGATOR_HOSTS)


def generic_title_check(job: dict, policy) -> str | None:
    """'no_context' when a generic title ('Data Analyst') has no relevant context in the title or posting,
    'unverified' when there is no posting text to check, None when the title is specific or context is present."""
    cfg = policy.get("generic_titles", default={}) or {}
    title_norm = job.get("title_norm") or ""
    generic = [norm(t) for t in cfg.get("titles", []) if contains_term(title_norm, norm(t))]
    if not generic:
        return None
    rest = title_norm
    for g in sorted(generic, key=len, reverse=True):
        rest = rest.replace(g, " ")
    context = [norm(t) for t in cfg.get("context_terms", [])]
    if any(contains_term(rest, t) for t in context):
        return None
    text = norm((job.get("description_text") or "")[:6000])
    if len(text) < 200:  # too little text to judge (e.g. a stub posting): flag it, never reject on it
        return "unverified"
    return None if any(contains_term(text, t) for t in context) else "no_context"


PERIODS_PER_YEAR = {"hour": HOURS_PER_YEAR, "day": 260, "week": 52, "month": 12, "year": 1}


def annualize(amount: float | None, period: str | None) -> float | None:
    """Pay as a yearly amount: hourly x 2080, daily x 260, weekly x 52, monthly x 12 (2026-10-12: all units)."""
    if amount is None:
        return None
    return amount * PERIODS_PER_YEAR.get(period or "year", 1)


def excluded_employer(job: dict, policy) -> str | None:
    """The exclusion-list entry matching this employer or its parent (the list and its reasons are private)."""
    names = norm(" ".join(filter(None, [job.get("employer_name"), job.get("parent")])))
    for entry in policy.get("gate", "excluded_employers", default=[]) or []:
        if norm(entry) and contains_term(names, norm(entry)):
            return entry
    return None


def evaluate(job: dict, requirements: list, profile: Profile, policy) -> GateResult:
    result = GateResult("eligible")
    gate = policy.get("gate", default={})
    title_norm = job["title_norm"]
    text_norm = norm(job.get("description_text") or "")

    if excluded_employer(job, policy):
        result.fail("employer_excluded", "employer is on your exclusion list")

    term = excluded_term(title_norm, policy.get("title_exclude", default=[]))
    if term:
        result.fail("title_excluded", f"title contains excluded term '{term}'", job.get("title"))
    if not job.get("family"):
        result.fail("no_target_family", "title does not match any target job family", job.get("title"))
    generic = generic_title_check(job, policy)
    if generic == "no_context":
        result.fail("generic_title_no_context", "generic title with no claims/healthcare/workflow context in the posting",
                    job.get("title"), confidence=0.8)
    elif generic == "unverified":
        result.flag("generic_title_unverified", "generic title; posting text not available to confirm relevant context")

    location_rules(result, job, profile, policy, remote_only=bool(policy.get("location", "remote_only")))

    from careeros.normalize.posting import seniority
    from .scores import SENIORITY_RANK
    posting_level = seniority(title_norm, policy.get("seniority_terms", default={}))
    mine = profile.level_rank()
    max_gap = gate.get("max_seniority_gap", 2)
    if mine is not None and SENIORITY_RANK[posting_level] - mine > max_gap:
        result.fail("seniority_gap", f"{posting_level}-level role; more than {max_gap} levels above your current level",
                    job.get("title"), profile=f"current level: {profile.data.get('current_level')}")

    principal_rule(result, job, policy)

    employment = job.get("employment_type") or "unknown"
    if employment not in gate.get("employment_types_allowed", []):
        result.fail("employment_type", f"employment type '{employment}' is not one you accept",
                    profile=f"accepted: {', '.join(gate.get('employment_types_allowed', []))}")

    scam_rules(result, text_norm, policy)

    comp = policy.get("compensation", default={})
    floor = comp.get("floor_annual") or (comp.get("floor_hourly") * HOURS_PER_YEAR if comp.get("floor_hourly") else None)
    top = annualize(job.get("comp_max"), job.get("comp_period"))
    if floor and top is not None and top < floor:
        if comp_trusted(job):
            result.fail("below_pay_floor", f"posted maximum {job.get('comp_max'):,.0f}/{job.get('comp_period')} is below your floor",
                        source=pay_quote(job.get("description_text"))
                        or f"pay posted in the board's own fields: {job.get('comp_min') or job.get('comp_max'):,.0f}-"
                           f"{job.get('comp_max'):,.0f} per {job.get('comp_period')}",
                        profile=f"pay floor: {floor:,.0f}/year")
        else:
            result.flag("pay_untrusted_below_floor", "pay shown by an aggregator site is below your floor (an estimate, "
                                                     "not grounds for rejection): check the employer's own posting")

    held_rule(result, job, policy)
    _requirement_rules(result, requirements, profile, policy, job)
    if result.hard_failures:
        result.decision = "rejected"
    return result


def location_rules(result: GateResult, job: dict, profile: Profile, policy, remote_only: bool) -> None:
    """US only, state eligibility for remote roles, and remote-only / commute rules (shared by PRIMARY and NIGHT)."""
    if policy.get("gate", "require_us") and job.get("country") == "non_US":
        result.fail("outside_us", f"location '{job.get('location_raw')}' is outside the US", job.get("location_raw"),
                    profile="US roles only")

    home = policy.get("location", "home_state") or profile.state
    states = job.get("remote_states") or []
    excluded = job.get("remote_excluded_states") or []
    if job.get("remote_type") in ("remote", "hybrid") and (states or excluded):
        if not home:
            result.flag("home_state_unknown", "posting limits eligible states but your state is not set")
        elif home in excluded:
            result.fail("state_excluded", f"role cannot hire in {', '.join(excluded)}; you are in {home}",
                        job.get("location_raw"), profile=f"home state: {home}", confidence=0.9)
        elif states and home not in states:
            result.fail("state_not_eligible", f"role limited to {', '.join(states)}; you are in {home}",
                        job.get("location_raw"), profile=f"home state: {home}", confidence=0.9)

    cities = [norm(c) for c in policy.get("location", "commute_cities", default=[]) or []]
    if remote_only:
        if job.get("remote_type") in ("onsite", "hybrid"):
            result.fail("not_remote", f"{job.get('remote_type')} role in '{job.get('location_raw')}'; you only accept remote roles",
                        job.get("location_raw"), profile="remote roles only")
        elif job.get("remote_type") != "remote":
            result.flag("remote_unknown", "posting does not say whether the role is remote")
    elif job.get("remote_type") in ("onsite", "hybrid"):
        location = norm(job.get("location_raw") or "")
        if not cities:
            result.flag("commute_unknown", f"{job.get('remote_type')} role in '{job.get('location_raw')}'; commute cities not set")
        elif not any(c and c in location for c in cities):
            result.fail("outside_commute", f"{job.get('remote_type')} role in '{job.get('location_raw')}' is outside your commute cities")


def principal_rule(result: GateResult, job: dict, policy) -> None:
    """'Principal' (2026-10-12) is a strong seniority signal, not an automatic rejection. A Principal title is rejected
    only when the posting also asks for people leadership or for strategic/architecture ownership (two or more distinct
    strategy terms); a single strategy term is a flag for you to read. Years go through the normal years rule."""
    if not contains_term(job.get("title_norm") or "", "principal"):
        return
    cfg = policy.get("gate", "principal_scope", default={}) or {}
    text = norm(job.get("description_text") or "")
    leading = [t for t in cfg.get("leadership_terms", []) if contains_term(text, norm(t))]
    strategy = [t for t in cfg.get("strategy_terms", []) if contains_term(text, norm(t))]
    if leading or len(strategy) >= 2:
        result.fail("principal_strategic_scope", "Principal role with people-leadership or strategic/architecture ownership: "
                    + ", ".join((leading + strategy)[:4]), job.get("title"), profile="individual-contributor scope",
                    confidence=0.8, strength="hard")
    elif strategy:
        result.flag("principal_scope_check", f"Principal role mentions '{strategy[0]}': check the scope is individual-contributor")


def scam_rules(result: GateResult, text_norm: str, policy) -> None:
    hits = [t for t in policy.get("gate", "scam_block_terms", default=[]) if contains_term(text_norm, t)]
    if len(hits) >= 2 or any(t in STRONG_SCAM_TERMS for t in hits):
        result.fail("scam_signals", f"scam indicators: {', '.join(hits)}")
    elif hits:
        result.flag("scam_signal", f"possible scam indicator: {hits[0]}")


def held_rule(result: GateResult, job: dict, policy) -> None:
    held = [norm(p) for p in policy.get("gate", "held_parents", default=[]) or []]
    if norm(job.get("parent") or "") in held or norm(job.get("employer_name") or "") in held:
        result.flag("held_d13", "employer is on hold until you decide about it (D13)")


def _education_summary(profile: Profile) -> str:
    done = [e.get("level") for e in profile.data.get("education") or [] if e.get("completed")]
    partial = [e.get("level") for e in profile.data.get("education") or [] if e.get("completed") is False]
    return f"completed: {', '.join(done) or 'none'}" + (f"; not completed: {', '.join(partial)}" if partial else "")


def best_years(profile: Profile, name: str) -> float | None:
    """Your strongest years across the alternatives a requirement lists ("consulting, business analyst or
    healthcare"): an OR-list is met by any one of them. None when none of them is known."""
    found = [y for y in (profile.years_for(a) for a in alternatives(name)) if y is not None]
    return max(found) if found else None


def degree_stretch(job: dict | None, policy) -> bool:
    """Your 2026-10-12 decision: a required bachelor's degree does not reject a PBM product role (product manager /
    owner / analyst with PBM, pharmacy-benefit or RxClaim context); the job is kept as a STRETCH instead."""
    if not job:
        return False
    cfg = policy.get("gate", "degree_stretch", default={}) or {}
    title = job.get("title_norm") or ""
    if not any(contains_term(title, norm(t)) for t in cfg.get("title_terms", [])):
        return False
    from .scores import domain_level
    return domain_level(job, policy)["level"] in (cfg.get("domain_levels") or ["direct"])


def requirement_rules(result: GateResult, requirements: list, profile: Profile, policy, job: dict | None = None) -> None:
    """Degree, certification, license, clearance, work authorization and years (shared by PRIMARY and NIGHT)."""
    _requirement_rules(result, requirements, profile, policy, job)


def _requirement_rules(result: GateResult, requirements: list, profile: Profile, policy, job: dict | None = None) -> None:
    gate = policy.get("gate", default={})
    min_conf = policy.get("extraction", "min_confidence", default=0.6)
    minor_gap, major_gap = gate.get("years_minor_gap", 2), gate.get("years_major_gap", 4)
    degree_rank = profile.degree_rank()
    auth = profile.data.get("work_authorization") or {}
    # One posting line can yield several records ("9+ years in consulting, healthcare or pharmacy" -> a record per
    # domain). They are alternatives, not separate demands: judge each line once, on your best years across them.
    line_best: dict = {}
    for r in requirements:
        if r.years is not None and r.category in YEARS_CATEGORIES:
            found = best_years(profile, r.name)
            key = (r.source_text, r.years)
            if found is not None:
                line_best[key] = max(line_best.get(key, found), found)
    judged: set = set()
    # Certifications (2026-10-15): judged per set of alternatives, on what you hold (careeros/qualify/certifications.py)
    cert_records = certification_mod.analyse(requirements, profile, policy)
    cert_group = {m["index"]: group for group in certification_mod.gate_groups(cert_records) for m in group}
    held = [c["name"] for c in certification_mod.profile_credentials(profile, policy) if c["status"] == "held"]

    for req_index, req in enumerate(requirements):
        hard = req.requirement_type == "hard_required"
        soft_required = req.requirement_type in ("required_with_equivalency", "ambiguous")
        years_line = (req.source_text, req.years)
        if req.years is not None and req.category in YEARS_CATEGORIES:
            if years_line in judged:
                continue
            judged.add(years_line)
        if req.requirement_type == "preferred" and req.years is not None and req.category in YEARS_CATEGORIES:
            have = line_best.get(years_line)
            if have is not None and req.years > have:  # preferred years: a fit penalty only, never a rejection
                result.flag("years_preferred_short", f"{req.years:g}+ years of {req.name} preferred; you have {have:g}", req.source_text)
            continue
        if not (hard or soft_required):
            continue
        if hard and req.confidence < min_conf:
            result.flag("low_confidence_requirement", f"uncertain reading: {req.name}", req.source_text)

        if req.category == "education":
            needed = DEGREE_RANK.get(req.name, 0)
            if degree_rank is None:
                result.flag("education_unknown", f"{req.name} degree requested; your education is not set", req.source_text)
            elif degree_rank < needed:
                if hard and degree_stretch(job, policy):
                    result.flag("degree_stretch", f"{req.name} degree required: counted as a STRETCH for this PBM product "
                                                  "role (your 2026-10-12 decision), never Tier A/B", req.source_text)
                elif hard:
                    result.fail("degree_required", f"{req.name} degree required without an equivalency option", req.source_text,
                                profile=_education_summary(profile), confidence=req.confidence, strength=requirement_strength(req.requirement_type))
                else:
                    result.flag("degree_equivalency", f"{req.name} degree or equivalent experience", req.source_text)

        elif req.category == "certification":
            group = cert_group.get(req_index)
            if not group or group[0]["index"] != req_index:   # each set of alternatives is judged once, at its first record
                continue
            effects = {m["decision_effect"] for m in group}
            names = " or ".join(m["normalized_name"] for m in group)
            notes = "".join(certification_mod.status_note(m) for m in group)
            if effects == {"hard_failure"}:   # every alternative is a named credential you do not hold
                result.fail("certification_required", f"{names} required{notes}", req.source_text,
                            profile=f"certifications held: {', '.join(held) or 'none'}",
                            confidence=req.confidence, strength=requirement_strength(req.requirement_type))
                result.hard_failures[-1]["certifications"] = group
            elif "requirement_satisfied" not in effects and effects & {"check_required", "hard_failure"}:
                named = all(m["kind"] == "credential" for m in group)
                code, what = ("certification_gap", f"{names} requested{notes}") if named else \
                    ("certification_check", f'a certification is mentioned: {names}{notes} ("{group[0]["mention"]}")')
                result.flag(code, what, req.source_text)
                result.flags[-1]["certifications"] = group

        elif req.category == "license":
            professional = any(t in req.name for t in PROFESSIONAL_LICENSES)
            if professional and not profile.licenses() and hard:
                result.fail("license_required", "professional license required", req.source_text,
                            profile="licenses: none", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
            else:
                result.flag("license_check", "a license is mentioned", req.source_text)

        elif req.category == "clearance":
            clearance = norm(str(profile.data.get("clearance") or ""))
            if hard and clearance in ("", "none"):
                result.fail("clearance_required", "security clearance required", req.source_text,
                            profile="clearance: none", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
            elif not clearance:
                result.flag("clearance_check", "clearance mentioned", req.source_text)

        elif req.category == "work_authorization":
            if req.name == "no_sponsorship" and auth.get("needs_sponsorship") is True:
                result.fail("sponsorship_unavailable", "role does not sponsor visas", req.source_text,
                            profile="needs sponsorship", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
            elif req.name == "us_work_authorization" and auth.get("us_authorized") is False:
                result.fail("work_authorization", "US work authorization required", req.source_text,
                            profile="not US work authorized", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
            elif req.name == "us_citizenship":
                result.flag("citizenship_check", "US citizenship may be required", req.source_text)
            elif auth.get("us_authorized") is None:
                result.flag("work_authorization_unknown", "work authorization not set in your profile", req.source_text)

        elif req.years is not None and req.category in YEARS_CATEGORIES:
            have = line_best.get(years_line)
            if have is None:
                result.flag("years_unknown", f"{req.years:g}+ years of {req.name} requested; your years not set", req.source_text)
            else:
                # Prompts v2.1 (2026-10-12): severity-based. Up to 2 years short: moderate penalty; more than 2: major
                # penalty; more than 4 short on a clearly REQUIRED number: rejected. Never on preferred years.
                gap = req.years - have
                if gap > major_gap and hard:
                    result.fail("years_far_short", f"{req.years:g}+ years of {req.name} required; you have {have:g}", req.source_text,
                                profile=f"{have:g} years (your best match on this line)", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
                    # 2026-10-15: the rejection keeps the figure and the line's Experience Match, so it can be explained
                    result.hard_failures[-1].update({"required_years": req.years, "experience": experience_mod.gate_line(
                        [r for r in requirements if (r.source_text, r.years) == years_line], profile, policy)})
                elif gap > minor_gap:
                    result.flag("years_gap", f"{req.years:g}+ years of {req.name} requested; you have {have:g}", req.source_text)
                elif gap > 0:
                    result.flag("years_slightly_short", f"{req.years:g}+ years of {req.name}; you have {have:g}", req.source_text)

    for conflict in experience_mod.conflicts(requirements):  # separate hard lines, one area, different years
        result.flag("years_conflicting", f"{', '.join(f'{y:g}' for y in conflict['years'])} years of {conflict['area']} "
                                         f"stated; the higher figure ({conflict['counted']:g}) counts", conflict["source_text"])
