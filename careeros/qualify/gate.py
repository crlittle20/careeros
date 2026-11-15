"""Hard eligibility gate (v0.3 §7.1). A hard failure is never averaged away by a score.

Every failure and flag carries a machine code, a readable detail and, where it came from the posting, the
verbatim source line. Every hard failure also records the profile fact it was judged against and a confidence
(prompts v2.2: evidence-backed rejections; kept in the local database only). Unknown candidate facts produce flags,
not passes. `state_for` turns a result into ELIGIBLE / CONDITIONAL / HOLD / INELIGIBLE.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from careeros.normalize.posting import (NEGATION_WORD, contains_term, excluded_term, home_mention, location_states,
                                       negated_mentions, norm, pay_quote, state_clauses, state_statements,
                                       targeted_supervisor, text_state_lists, unlisted_cues)

from careeros.extract.model import requirement_strength
from careeros.extract.rules import (demanded_line, extract, figure_subjects, mentions_years, requirement_lines,
                                    year_spans)

from . import certifications as certification_mod
from . import experience as experience_mod
from . import role_years
from .profile import DEGREE_RANK, Profile, alternatives

# Bump when gate logic changes: results recorded under an older version are re-evaluated automatically.
RULES_VERSION = "gate-2026-11-13a"  # 11-13a: 11-11a with the D149/D150 fail-safes (Codex Pass 2 of the combined
# candidate): a role years shortfall never rejects on its own; pay tiers and periods read every way when unclear; a
# location conflict is to verify when the description names the home state; one new version, so every result is
# judged again exactly once (not yet deployed)
# 11-11a: 11-10a with the intake hotfix's final rules (d662c91): a certification never
# rejects on its own (D140, "verify"; an equivalency stays a note), the AI agreement rule (D138); one new version, so every
# result is judged again exactly once (not yet deployed)
# 11-10a: the D118-D120 corrections under the owner's D123 fail-safe rules (a role
# line with any alternative cue, a state list from the description, an uncertain pay period or tier: CONDITIONAL, never a
# rejection or a silent pass), the hotfix's QA weak-word rule (d156418) and the F8-03 offsite fix; one new version, so
# every result is judged again exactly once (not yet deployed)
# 11-09a: the combined candidate (11-04a below) joined with the live rules of
# 2026-11-08 (an explicit supervisor-scope flag is conditional, not an automatic pass); one new version, so every
# result recorded under either is judged again exactly once (not yet deployed)
# 11-04a: pay listed by location on both sides of the floor, with no word on which
# applies to you, is pay_tier_unknown (a flag), never a rejection or a pass on whichever range comes first; a pay
# period the posting leaves open across your floor is pay_period_unknown, and a state limit read from a residence
# sentence is residence_limit_check: flags, never a rejection (the fix-check of 2026-11-05)
# 2026-11-08-supervisor-review (live 2026-11-09): explicit supervisor-scope flag is conditional, not an automatic pass
# 2026-11-09-intake-hotfix (D116): QA titles take the health-plan operations words as context
_PREVIOUS_RULES = "gate-2026-11-03a"  # 11-03a (OPP-43): years as a role, in a function or doing an activity are met only by
# years in it (qualify/role_years.py); when nothing in your record names it, years_role_unconfirmed, never a rejection
# 10-18a: certification alternatives only when "or" joins them; unknown kinds and
# courses never count; a course and a certification of one name kept apart; issuer requirements by issuer only
# 10-16a: certification status must be explicit (missing = unknown), courses never count
# 10-15b: certifications named, matched by status, alternatives honoured; 10-15a: Experience Match
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
    return "CONDITIONAL" if codes & (material | {"supervisor_scope_review"}) else "ELIGIBLE"


def comp_trusted(job: dict) -> bool:
    """Pay is trusted for a hard rejection unless the posting came from an aggregator page (v2.2)."""
    host = urlsplit(job.get("url") or "").netloc.lower()
    return not any(a in host for a in AGGREGATOR_HOSTS)


def generic_title_check(job: dict, policy) -> str | None:
    """'no_context' when a generic title ('Data Analyst') has no relevant context in the title or posting,
    'unverified' when there is no posting text to check, 'weak_context' when a QA title's only context is a word that
    also fits software or finance work, None when the title is specific or context is present."""
    cfg = policy.get("generic_titles", default={}) or {}
    title_norm = job.get("title_norm") or ""
    generic = [norm(t) for t in cfg.get("titles", []) if contains_term(title_norm, norm(t))]
    if not generic:
        return None
    rest = title_norm
    for g in sorted(generic, key=len, reverse=True):
        rest = rest.replace(g, " ")
    # QA titles take their own context list (no software QA), kept in the policy with the titles it applies to (2026-11-09)
    qa_titles = {norm(t) for t in cfg.get("qa_titles", [])}
    context_key = "qa_context_terms" if qa_titles.intersection(generic) else "context_terms"
    context = [norm(t) for t in cfg.get(context_key, cfg.get("context_terms", []))]
    text = norm((job.get("description_text") or "")[:6000])
    qa = context_key == "qa_context_terms"
    weak = [norm(t) for t in cfg.get("qa_weak_context_terms", [])] if qa else []
    # A QA posting that names a software-testing tool (Selenium, pytest, Postman, ...) passes only on two different
    # health-plan words; with some health-plan word but fewer it is flagged for review, never passed; with none at all
    # (strong or weak) it is software QA and rejected, as live does (CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-04, D126:
    # one boilerplate word admitted software QA, and many tools rejected an 834 audit). A stub posting (under 200
    # characters) is never judged by it: it is flagged "unverified" below, as live does (Pass 3, D126 C3)
    # a tool named in the title ("QA Analyst - Selenium") counts as one named in the posting (Pass 3, CR-HF-04, D127 B4)
    if qa and len(text) >= 200 and any(contains_term(text, norm(t)) or contains_term(title_norm, norm(t))
                                       for t in cfg.get("qa_software_terms", [])):
        strong = {t for t in context if contains_term(rest, t) or contains_term(text, t)}
        distinct = {t for t in strong if not any(t != u and contains_term(u, t) for u in strong)}   # "provider data": one
        if not strong and not any(contains_term(rest, t) or contains_term(text, t) for t in weak):
            return "no_context"
        return None if len(distinct) >= 2 else "weak_context"
    if any(contains_term(rest, t) for t in context):
        return None
    if len(text) < 200:  # too little text to judge (e.g. a stub posting): flag it, never reject on it
        return "unverified"
    if any(contains_term(text, t) for t in context):
        return None
    # A QA title's weak words (enrollment, reconciliation, eligibility, perks, the EDI codes) fit software or finance
    # work too: they only flag the posting for review (CR-2026-11-09-INTAKE-HOTFIX CR-HF-04)
    return "weak_context" if any(contains_term(rest, t) or contains_term(text, t) for t in weak) else "no_context"


PERIODS_PER_YEAR = {"hour": HOURS_PER_YEAR, "day": 260, "week": 52, "month": 12, "year": 1, "biweekly": 26, "semimonthly": 24}


def annualize(amount: float | None, period: str | None) -> float | None:
    """Pay as a yearly amount: hourly x 2080, daily x 260, weekly x 52, monthly x 12 (2026-10-12: all units)."""
    if amount is None:
        return None
    return amount * PERIODS_PER_YEAR.get(period or "year", 1)


def _pay_tiers_straddle(job: dict, floor: float | None, home: str | None) -> bool:
    """True when the stored pay came from the posting's text, the text lists two or more ranges without saying plainly
    which applies to `home` (no tier names it plainly and no "all other locations" tier is left for it), and those
    ranges fall on both sides of the floor (2026-11-04). Then neither the first range (usually the highest, sometimes
    the lowest) nor any other is grounds to pass or reject: you confirm it. Since D123 (the fourth correction of
    2026-11-09) this holds whatever the ranges' labels are: "Remote", a label after the range, levels or roles (a
    needless CONDITIONAL is accepted; a wrong rejection or pass on the first range is not)."""
    from careeros.normalize.posting import home_pay_tier, period_readings
    if not floor:
        return False
    text = job.get("description_text") or ""
    status, _, tiers = home_pay_tier(text, home)
    if status not in ("unknown", "ambiguous") or job.get("comp_max") is None:
        return False   # one range (repeated or not), or the posting says which is yours: it decides
    if not any(abs(t.high - job["comp_max"]) < 0.5 for t in tiers):   # the board's own pay fields: not these tiers
        return False
    # every tier read every way it could be paid (Codex Pass 1, F1: a second zone "paid monthly" can clear the floor)
    placed = tiers if status == "ambiguous" else None
    tops = [t.high * PERIODS_PER_YEAR.get(period, 1) for t in tiers for period in period_readings(text, t, placed or [t])]
    return any(top < floor for top in tops) and any(top >= floor for top in tops)


def _pay_period_straddle(job: dict, floor: float | None, home: str | None) -> bool:
    """True when the stored pay is a range of the posting's text whose period its words or its size leave open, and
    the periods it could mean fall on both sides of the floor (2026-11-05): "$42 - $48 Weekly Pay!", "Daily rate
    $650 - $750, paid weekly". Then the period read is a guess, and it decides nothing: you confirm it."""
    from careeros.normalize.posting import pay_period_readings
    if not floor:
        return False
    found = pay_period_readings(job.get("description_text") or "", job.get("comp_max"), home)
    if not found:
        return False
    high, periods = found
    tops = [high * PERIODS_PER_YEAR.get(period, 1) for period in periods]
    return any(top < floor for top in tops) and any(top >= floor for top in tops)


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
    elif generic == "weak_context":
        result.flag("generic_title_unverified", "generic QA title; the posting's context words (eligibility, enrollment, "
                    "reconciliation, benefits, EDI codes, or software-testing tools with fewer than two health-plan words) "
                    "also fit software or finance work: confirm it is health-plan operations")

    location_rules(result, job, profile, policy, remote_only=bool(policy.get("location", "remote_only")))

    from careeros.normalize.posting import seniority
    from .scores import SENIORITY_RANK
    posting_level = seniority(title_norm, policy.get("seniority_terms", default={}))
    mine = profile.level_rank()
    max_gap = gate.get("max_seniority_gap", 2)
    if targeted_supervisor(title_norm, policy):
        result.flag("supervisor_scope_review", "verify the supervisor role's people-management scope and experience",
                    job.get("title"))
    elif mine is not None and SENIORITY_RANK[posting_level] - mine > max_gap:
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
    straddle = _pay_tiers_straddle(job, floor, policy.get("location", "home_state") or profile.state) if floor else False
    period_open = _pay_period_straddle(job, floor, policy.get("location", "home_state") or profile.state)
    if straddle:   # ranges by place on both sides of the floor, and the posting doesn't say which is yours
        result.flag("pay_tier_unknown", "the posting lists pay by location, with ranges above and below your floor, "
                                        "and doesn't say which applies to you: confirm your range",
                    source=pay_quote(job.get("description_text")))
    elif period_open:   # read per hour it clears your floor, per week or year it doesn't (or the reverse)
        result.flag("pay_period_unknown", "the posting doesn't say plainly whether its pay is per hour, day, week, month "
                                          "or year, and the readings fall on both sides of your floor: confirm the period",
                    source=pay_quote(job.get("description_text"), job.get("comp_max")))
    elif floor and top is not None and top < floor:
        if comp_trusted(job):
            result.fail("below_pay_floor", f"posted maximum {job.get('comp_max'):,.0f}/{job.get('comp_period')} is below your floor",
                        source=pay_quote(job.get("description_text"), job.get("comp_max"))
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
    # D123 (2026-11-09), a fail-safe rule, after three rounds of telling a requirement from "no limit" in description
    # lists each left new wordings passing silently: a needless CONDITIONAL is the accepted cost; a wrong silent pass or
    # a wrong rejection is not. A state list read from the description (text_state_lists) never rejects; one naming your
    # state beside an exclusion or negation word, or naming only others, flags residence_limit_check, once, quoting the
    # first. A location field holding an exclusion or negation word ("Remote - US, excluding <yours>") flags, quoting it.
    # D151 (red team round 1, R1, R2, R4; the owner's allow-style rule under D149): "Remote - <another>" rejected
    # (state_not_eligible) beside a description naming your state in a form no reader knew ("Fla.", "all 50 U.S.
    # states"), and "<another> (Remote)", "US-<another>-Remote", "<another>" or "<city>, <another>" were never read. NO
    # STATE IN THE LOCATION FIELD REJECTS: a state named anywhere in it (location_states), in any format, that leaves
    # your state out is CONDITIONAL residence_limit_check quoting it; only a location outside the US rejects (outside_us).
    # D151 (R3): THE DESCRIPTION IS SILENT ON RESIDENCE ONLY WHEN EXACTLY ONE CLAUSE OF IT NAMES ANY STATE, as one list,
    # naming your state with no exclusion or negation word, and nothing else restricts states (a residence, hiring or
    # eligibility statement of "states" with none named): "... <yours> or <another>. <Yours> positions have been
    # filled.", "... <yours> or <another> (<yours> pending approval).", "... <yours> or <another>, but this requisition is
    # for <another>." flag.
    location = job.get("location_raw") or ""
    description = job.get("description_text") or ""
    remote = job.get("remote_type") in ("remote", "hybrid")
    located = location_states(location) if job.get("country") != "non_US" else set()
    lists = text_state_lists(description) if remote else []
    if not home:
        if remote and (located or lists or job.get("remote_states") or job.get("remote_excluded_states")):
            result.flag("home_state_unknown", "posting limits eligible states but your state is not set")
    elif NEGATION_WORD.search(location) and (remote or located):
        result.flag("residence_limit_check", f"the location field holds an exclusion or negation ({location}); you are in "
                    f"{home}: confirm it before applying", source=location)
    elif located and home not in located:
        # ... and only while no sentence or line of the description naming your state holds such a word (the check of
        # f1a17e1: "... reside in <yours>, <another>. <Yours> residents are not eligible at this time." passed silently)
        doubted = home in negated_mentions(description)
        mention = home_mention(description, home)
        others = ", ".join(sorted(located))
        if any(home in named_there and not negated and not doubted for _listed, named_there, _quote, negated in lists):
            detail = f"the location names only {others}, but a list of states in the posting names {home}"
        elif mention:
            detail = f"the location names only {others}, but the posting names {home} elsewhere (\"{mention}\")"
        else:
            detail = f"the location names only {others}"
        result.flag("residence_limit_check", f"{detail}; you are in {home}: confirm it before applying", source=location)
    elif remote:
        doubted = home in negated_mentions(description)
        allows = [home in named_there and not negated and not doubted for _listed, named_there, _quote, negated in lists]
        mentions = state_clauses(description)
        statements = state_statements(description)
        # D155 (Codex Pass 3, F7): a cue line over lines naming no state ("Eligible states:\n- Northeast region") is a
        # restriction naming none; and a statement either reader reads with a state is kept: one naming states but
        # not yours plainly flags, whatever the other reader made of it
        restrictions = [s for s in statements if not s.states and not s.quote.endswith(":")] + unlisted_cues(description)
        foreign = [s for s in statements if s.states and (home not in s.states or s.negated)]
        if not all(allows):
            listed, _named_there, quote, negated = lists[allows.index(False)]
            where = f"names {', '.join(listed)}" + (" beside an exclusion or negation" if negated else
                                                    f"; the posting names {home} beside one elsewhere" if doubted else "")
            result.flag("residence_limit_check", f"a list of states in the posting may limit this role ({where}); you are "
                        f"in {home}: confirm it before applying", source=quote)
        elif restrictions or foreign or len(mentions) > 1 or (mentions and (home not in mentions[0].states
                                                                             or mentions[0].negated or doubted)):
            what = ("a statement of where the role can hire names no state" if restrictions else
                    "the posting names states more than once, and a later mention may narrow the first" if len(mentions) > 1
                    else "a statement of where the role can hire may limit it")
            quote = (restrictions[0] if restrictions else mentions[1] if len(mentions) > 1 else
                     mentions[0] if mentions else foreign[0]).quote
            result.flag("residence_limit_check", f"{what}; you are in {home}: confirm it before applying", source=quote)

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
    """An employer-level ELIGIBILITY hold (policy gate.held_parents; none by default): the job is judged but kept out of
    Tier A/B. The flag keeps its historical code name. Permission to submit is a separate layer (review/submission.py);
    since 2026-10-16 the D13 employer is held there, not here."""
    held = [norm(p) for p in policy.get("gate", "held_parents", default=[]) or []]
    if norm(job.get("parent") or "") in held or norm(job.get("employer_name") or "") in held:
        result.flag("held_d13", "employer is on an eligibility hold (gate.held_parents) until you decide about it")


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


# Figures offered as alternatives: "or", "and/or" or "/" right before the second figure, with only words that qualify it
# between ("or at least", "or more than", "or, alternatively,", "or a minimum of") (the Fable fix-checks, D113)
_OR_FIGURE = re.compile(r"(?:\b(?:or|and/or)\b|/)[\s,]*(?:(?:alternatively|more than|over|at least|a minimum of|"
                        r"minimum(?: of)?|min\.|an? equivalent(?: of)?|approximately|around|about|roughly)[\s,]*)*$")
# An "or" a few words before a figure that the joiners above do not name ("or similar, ideally 3+ years"): the figures
# may be alternatives, so neither is a rejection on its own; "and", "including", "with" ... after the "or" end it
_OR_NEAR = re.compile(r"\b(?:or|and/or)\b[\s,]*(?:(?!(?:and|including|with|plus|of|in)\b)[a-z.\-]+[\s,]+){0,3}$")
_RANK = {"pass": 0, "unknown": 1, "reject": 2}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _homes(text: str, lines: list[str]) -> list[str]:
    """The posting lines a requirement's quote comes from (D149, the AI-quote High): the line itself, else every line
    holding the quote ("5+ years as a BSA" from "- 5+ years as a BSA"), else every line inside a quote that spans
    several. [] when no line holds it. Cues are read from these posting lines, never from the quote."""
    if text in lines:
        return [text]
    quote = _squash(text)
    if not quote:
        return []
    inside = [line for line in lines if quote in _squash(line)]
    return inside or [line for line in lines if _squash(line) and _squash(line) in quote]


def _posting_lines(requirements: list, job: dict | None) -> list[str]:
    """The posting's lines in order, blank ones included, as the extractor splits them, to see which line follows which.
    A record whose quote is not a whole line (an AI quote without its "- ") never replaces them (D149, the AI-quote
    High); a quote no posting line holds is added after them, on its own. Only when the job's text holds none of the
    requirements (or there is none) are the requirements' own lines used, as before."""
    every = ((job or {}).get("description_text") or "").split("\n")
    texts = list(dict.fromkeys(r.source_text for r in requirements))
    filled = [line for line in every if line.strip()]
    unhomed = [text for text in texts if not _homes(text, filled)]
    if texts and len(unhomed) == len(texts):
        return texts
    return every + [line for text in unhomed for line in ("", text)]


def _section_lines(requirements: list, job: dict | None) -> list[tuple[str, str | None]]:
    """(line, section) for the posting's requirements lines (`rules.requirement_lines`); without a posting text, the
    requirements' own lines, a line whose records are all preferred counted as preferred (D149)."""
    text = (job or {}).get("description_text") or ""
    if text.strip():
        return requirement_lines(text)
    found: dict = {}
    for r in requirements:
        found.setdefault(r.source_text, set()).add(r.requirement_type)
    return [(line, "preferred" if types == {"preferred"} else None) for line, types in found.items()]


def _unread_years(requirements: list, job: dict | None, policy=None) -> list[tuple[str, float | None]]:
    """D149 R-Y2 (Codex Pass 2 C, 2026-11-12): every years figure on a demanded requirements line ("5+ years in
    underwriting with SQL; 2+ years with Excel") that no extracted record from that line carries, as (line, figure), and,
    by the word (the coordinator's follow-up), every demanded requirements line that says "year", "yr" or "decade" ("5 or
    more years", "Eleven years", "a decade of underwriting") and from which no years record came, as (line, None). Such
    a demand was dropped or never read: it is to verify (CONDITIONAL), never a silent pass. Ages, degree lengths and
    preferred lines are left out.

    D155 (3) (Codex Pass 3 F3): counted per figure, by subject, never as a set of amounts. Each figure must be carried by a
    record of its own: one of the skill or domain its own phrase opens with ("2+ years with Excel"), else a record that
    is no skill ("2+ years in underwriting with SQL" opens with underwriting: SQL's record, which took the other 2, does
    not carry it). Figures naming a skill or a domain take their records first; a record carries one figure only."""
    sections = _section_lines(requirements, job)
    lines = [line for line, _ in sections]
    aliases = policy.get("skill_aliases", default={}) or {} if policy is not None else {}
    domain_terms = policy.get("domain_terms", default={}) or {} if policy is not None else {}
    pools: dict = {}
    for r in requirements:
        if r.years is not None:
            subject = (r.category, r.name) if r.category in ("skill", "domain") else ("other",)
            for home in _homes(r.source_text, lines):
                pools.setdefault(home, Counter())[(subject, r.years)] += 1
    found = []
    for line, section in sections:
        if not demanded_line(line, section):
            continue
        pool = pools.get(line, Counter())
        figures: list[tuple[str, float | None]] = []
        figures_here = figure_subjects(line, aliases, domain_terms)
        seen: Counter = Counter()
        roles = []   # whether the reader reads each figure as a role: years in a role are never met by a tool's years
        for value, _upper, _subject in figures_here:
            reading = role_years.read(line, value, policy, seen[value]) if policy is not None else None
            roles.append(reading is not None and reading.kind == "role")
            seen[value] += 1
        ordered = sorted(zip(figures_here, roles, strict=True), key=lambda f: f[0][2] == ("other",))
        for (value, upper, subject), role in ordered:
            # its own subject first, then a record that is no skill (a role, total years, a domain); a skill's record
            # carries a figure read as a role ("5+ years as a BSA; SQL is a must"), which the reader judges on the role,
            # never one read as a function whose tool would credit it ("2+ years in underwriting with SQL")
            wanted = [(subject, v) for v in (value, upper) if v is not None] if subject != ("other",) else []
            wanted += [(s, v) for v in (value, upper) if v is not None for s, y in list(pool)
                       if y == v and (s[0] != "skill" or role) and pool[(s, y)] > 0]
            hit = next((w for w in wanted if pool[w] > 0), None)
            if hit is None:
                figures.append((line, value))
            else:
                pool[hit] -= 1
        if not figures and not pools.get(line) and mentions_years(line):
            figures = [(line, None)]   # a years demand no reader turned into a record
        found += figures
    return found


def _identity(r) -> tuple:
    """A record's whole identity (D155 (2)): category, name, strength, line and figure; never line and figure alone."""
    return r.category, r.name, r.requirement_type, r.source_text, r.years


def _not_allowed(plain: bool, traced: bool, cue: str | None, total: bool = True) -> str:
    """Why a years demand may not reject or take a years_gap (D151, D155), in the flag's words; "" when it may."""
    if not plain:
        return "it names a role, a function or an activity, never a rejection on its own (D151)"
    if not total:
        return "it names a domain or a tool, and only total professional years reject on their own (D155)"
    if not traced:
        return "the rules reader did not read it from the posting line (an AI reading alone never rejects, D151)"
    if cue:
        return f"the requirements may offer an alternative (\"{cue}\")"
    return ""


def _blocks(lines: list[str]) -> list[list[str]]:
    """The posting's lines in blocks: a blank line ends one, and a line ending in ":" (a heading, "Either:") opens one."""
    blocks: list = [[]]
    for line in lines:
        if not line.strip() or line.rstrip().endswith(":"):
            blocks.append([])
        if line.strip():
            blocks[-1].append(line)
    return [block for block in blocks if block]


_LIST_ITEM = re.compile(r"^\s*(?:[-–—•*·▪]|\d+[.)]|[a-z][.)]|\(\w{1,3}\))\s")


def _listed(lines: list[str]) -> list[str]:
    """The list directly below each lead-in ("Either:", "One of the following"), across the blank lines HTML puts after a
    paragraph: its items and the lines that go on with them, up to a blank line, a heading or prose (the check of
    5df563c). A lead-in followed by prose leads in nothing."""
    found = []
    for at, line in enumerate(lines):
        if not role_years.leads_in(line):
            continue
        below = at + 1
        while below < len(lines) and not lines[below].strip():
            below += 1
        first = below
        while below < len(lines) and lines[below].strip() and not lines[below].rstrip().endswith(":") and (
                _LIST_ITEM.match(lines[below]) or (below > first and lines[below][:1].isspace())):
            found.append(lines[below])
            below += 1
    return found


def _cues(by_line: dict, readings: dict, lines: list) -> dict:
    """D123 R1/R3 (2026-11-09): for each years figure read as a role, a function or an activity, the cue that its line may
    offer an alternative (`role_years.alternative_cue`); for any years figure, a joiner its line, or the posting line
    before it, ends in (", or" / "- 8+ years of SQL": the bullets may be alternatives). Such a figure's shortfall is
    CONDITIONAL, never a rejection; nothing here ever makes a figure pass, and posting lines are never merged: each line
    keeps its own demands. A line that is only a joiner ("OR"), opens with one ("- or 5+ years as ...") or leads in
    alternatives ("Either:", "One of the following:") does the same for every line of its block, and a joiner line for
    the lines on either side of it across blank lines (D123's check)."""
    filled = [line for line in lines if line.strip()]
    joined: dict = {}
    for before, after in zip(filled, filled[1:], strict=False):
        if role_years.ends_in_joiner(before):
            joined.setdefault(after, "or (the line before ends in it)")
        if role_years.opens_alternatives(after):
            joined.setdefault(before, "or (a line after it)")
    for block in _blocks(lines):
        if any(role_years.opens_alternatives(line) for line in block):
            for line in block:
                joined.setdefault(line, "or (a line of its block)")
    for line in _listed(lines):
        joined.setdefault(line, "one of the following (a lead-in above it)")
    cues = {}
    for key in by_line:
        # read on the posting lines the record comes from, never on its quote alone (D149, the AI-quote High)
        homes = _homes(key[0], filled) or [key[0]]
        cue = next((found for home in homes if (found := role_years.alternative_cue(home))), None) if key in readings else None
        if not cue and any(role_years.ends_in_joiner(home) for home in homes):
            cue = "or (the line ends in it)"
        if not cue:
            cue = next((joined[home] for home in homes if home in joined), None)
        if cue:
            cues[key] = cue
    return cues


def _or_figures(by_line: dict, line_best: dict, outcome, roles: dict) -> tuple[set, set]:
    """(the years figures left unjudged, those that may not reject on their own). Figures joined by "or" ("5+ years as a
    BSA or 3+ years as a Product Owner") are alternatives: the line is judged once, on its best alternative, one that
    passes before one nothing in your record names (CONDITIONAL) before one that rejects, the closest first, so it is
    rejected only when every alternative would reject (the Fable fix-checks of ff3c7f7 and f74cdcc, 2026-11-05, D113).
    The groups are formed over every figure on the line, so a figure no record carries still separates its neighbours.
    Only figures on one line are joined: a posting line is never merged with the next (D123 R3). In a set that holds a
    figure read as a role, a function or an activity (`roles`: the kind of each), only years in a role the line names
    pass it ("5+ years as a BSA or 3+ years as a Product Owner"); a figure of another kind (SQL, healthcare, a setting, a
    function) never does (D123 R2): the set is judged on its one read figure, or on the strictest of several, whose
    shortfall then flags at most."""
    skip: set = set()
    loose: set = set()
    for text in dict.fromkeys(text for text, _ in by_line):
        spans = year_spans(text)
        # A second figure under one name ("Senior designers bring 5+ years ... Staff designers bring 8+ years"): the
        # duplicate check dropped it until f74cdcc's fix-check, so it is judged now, but may not reject on its own
        named: dict = {}
        for span in spans:
            for r in by_line.get((text, span[2]), ()):
                if r.category == "years_experience":
                    named.setdefault(r.name, []).append((text, span[2]))
        for keys in named.values():
            loose |= set(keys) - {keys[0]}
        # On a line that names a skill, a figure only a years_experience record carries is one #4 added: it may be the
        # skill's own figure or no demand at all, so it flags, never rejects on its own (the final fix-check, D115)
        keys = [key for key in by_line if key[0] == text]
        if any(r.category == "skill" for key in keys for r in by_line[key]):
            loose |= {key for key in keys if all(r.category == "years_experience" for r in by_line[key])}
        groups, group = [], spans[:1]
        for before, span in zip(spans, spans[1:], strict=False):
            between = text[before[1]:span[0]].lower()
            if _OR_FIGURE.search(between):
                group.append(span)
                continue
            if _OR_NEAR.search(between):
                loose |= {(text, before[2]), (text, span[2])} & by_line.keys()
            groups.append(group)
            group = [span]
        groups.append(group)
        for group in groups:
            members = [key for key in dict.fromkeys((text, span[2]) for span in group) if key in by_line]
            figures = [span[2] for span in group]
            # One figure named twice as alternatives ("5+ years as a BSA or 5+ years with SQL") is one record, judged on
            # its first phrase only: it flags, never rejects on its own (D115)
            loose |= {(text, figure) for figure in figures if figures.count(figure) > 1} & by_line.keys()
            if len(members) > 1:
                rank = lambda key: (_RANK[outcome(key)], key[1] - line_best.get(key, key[1]))
                held = [key for key in members if key in roles]
                met = [key for key in held if roles[key] == "role" and outcome(key) == "pass"]
                if met:   # years in a role the line names pass it (D123 R2)
                    best = min(met, key=rank)
                elif held:   # D123 R2: a figure of another kind never meets a role, and one function never meets another
                    best = held[0] if len(held) == 1 else max(held, key=rank)
                    loose.add(best)   # judged on it, and a shortfall on it only flags
                else:
                    best = min(members, key=rank)
                skip |= set(members) - {best}
    return skip, loose


def demand_occurrences(text: str, figure: float) -> list[int]:
    """Which times a years figure appears on a line (counted from 0) are demands of their own (D120, 2026-11-09): the
    first in each group of figures joined by "or". The same figure stated again for another demand ("8+ years as a BSA
    and 8+ years of healthcare experience") is judged beside the first, and the stricter decides; stated again as an
    alternative ("5+ years as a BSA or 5+ years with SQL") it stays one demand, which flags at most (D115)."""
    spans = year_spans(text or "")
    found, groups, group, seen = [], set(), 0, 0
    for index, span in enumerate(spans):
        if index and not _OR_FIGURE.search((text or "")[spans[index - 1][1]:span[0]].lower()):
            group += 1
        if span[2] == figure:
            if group not in groups:
                groups.add(group)
                found.append(seen)
            seen += 1
    return found or [0]


def demands_cued(text: str, figure: float) -> str | None:
    """D123 R4 (2026-11-09): the cue of an alternative ("or", "either", "/", ...: `role_years.alternative_cue`) anywhere
    between the first and the last demand of a figure stated again, or None. Then whether the demands are joined by "or"
    or by "and" is unclear: the stricter demand still decides (never a pass on the other), but its rejection is
    CONDITIONAL, never a rejection. Experience Match scores such a line as AND."""
    found = demand_occurrences(text, figure)
    if len(found) < 2:
        return None
    same = [span for span in year_spans(text or "") if span[2] == figure]
    return role_years.alternative_cue((text or "")[same[found[0]][1]:same[found[-1]][0]])


def _demand(key, text: str, records: list, profile: Profile, policy, occurrence: int) -> dict:
    """How one demand of a years line is judged: its reading (None: the line as before OPP-43) of the line's `text`, your
    years on it (`have`), an uncertain credit (`unsure`), the old years set aside and what they were for (`old_name`),
    and the years each other reading gives."""
    counted = [(years, r.name) for r in records if (years := best_years(profile, r.name)) is not None]
    old, old_name = max(counted) if counted else (None, "")
    reading = role_years.read(text, key[1], policy, occurrence)
    found = {"occurrence": occurrence, "reading": reading, "have": old if reading is None else None, "old_name": old_name}
    if reading is None:
        return found
    credit = role_years.line_credit(records, reading, profile, policy)
    if credit is not None and _unconfirmed(credit, key[1], _major_gap(policy)):
        found["unsure"] = credit   # short on what is known, and an alternative is unconfirmed: unknown (Codex F2, F4)
    elif credit is not None:
        found["have"] = credit["years"]
    if old is not None and (credit is None or old > credit["years"]):
        found["set_aside"] = old
    if reading.doubt:   # the line's other plausible readings, each judged too (the years branch below)
        found["doubted"] = [_reading_years(other, records, old, profile, policy, key[1]) for other in reading.others]
    return found


def _demand_outcome(found: dict, required: float, hard: bool, major_gap: float) -> str:
    """What the years branch makes of one demand, its other readings included."""
    mine = _years_outcome(required, found["have"], hard, major_gap)
    return "unknown" if any(_years_outcome(required, years, hard, major_gap) != mine
                            for years in found.get("doubted", ())) else mine


def _unconfirmed(credit: dict, required: float, major_gap: float = 4) -> bool:
    """Whether a credit falls short only on what your record confirms, while the line offers an alternative it does not
    confirm (nothing names it, or only partly): that alternative is to be verified, so the line is unknown, never a
    rejection (Codex Pass 1 F2, F4, 2026-11-05; D1, D21). A credit that is only a partial match confirms no pass
    either (D3; the Fable fix-check of dba7b35). Roles joined by "and" (`parts`, D123's check) pass only when every one
    is met and reject only when every one is far short: any other mix is to be verified."""
    parts = [years for _, years in credit.get("parts", ())]
    if parts and min(parts) < required and max(parts) >= required - major_gap:
        return True
    return bool(credit.get("partial")) or (bool(credit.get("unconfirmed")) and credit["years"] < required)


def _major_gap(policy) -> float:
    return (policy.get("gate", default={}) or {}).get("years_major_gap", 4)


def _known(credit: dict) -> str:
    """What an uncertain credit rests on, for a flag: "you have 0.5 as business systems analyst; product owner
    unconfirmed"."""
    names = ", ".join(re.sub(r"^(?:an?|the)\s+", "", a) for a in credit["unconfirmed"])
    return (f"you have {credit['years']:g} as {credit['item']}{' (a partial match)' if credit.get('partial') else ''}"
            + (f"; {names} unconfirmed" if names else ""))


def _reading_years(reading, records: list, old: float | None, profile: Profile, policy, required: float) -> float | None:
    """Your years on a years line as one reading of it judges them (None for a reading: the line as before OPP-43)."""
    if reading is None:
        return old
    credit = role_years.line_credit(records, reading, profile, policy)
    return credit["years"] if credit is not None and not _unconfirmed(credit, required, _major_gap(policy)) else None


def _years_outcome(required: float, have: float | None, hard: bool, major_gap: float) -> str:
    """What the years branch makes of a line: unknown (CONDITIONAL), reject, or pass (with or without a flag)."""
    if have is None:
        return "unknown"
    return "reject" if hard and required - have > major_gap else "pass"


def _other_readings(reading, others_years, old_name: str) -> list[tuple[str, float | None]]:
    """(how else the line reads, your years read that way) for each other plausible reading of a line; the reading as
    before OPP-43 names what its years were for (the fix-check of 947e709: not the line's first record)."""
    if reading is None:
        return []
    return [((other.phrase() if other is not None else f"of {old_name}"), years)
            for other, years in zip(reading.others, others_years, strict=True)]


def _yours(years: float | None) -> str:
    return f"you have {years:g}" if years is not None else "nothing in your record names it"


def _requirement_rules(result: GateResult, requirements: list, profile: Profile, policy, job: dict | None = None) -> None:
    gate = policy.get("gate", default={})
    min_conf = policy.get("extraction", "min_confidence", default=0.6)
    minor_gap, major_gap = gate.get("years_minor_gap", 2), gate.get("years_major_gap", 4)
    degree_rank = profile.degree_rank()
    auth = profile.data.get("work_authorization") or {}
    # One posting line can yield several records ("9+ years in consulting, healthcare or pharmacy" -> a record per
    # domain). They are alternatives, not separate demands: judge each line once, on your best years across them.
    # OPP-43 (2026-11-03): a line that names a role, a function or an activity is judged on years IN it only
    # (qualify/role_years.py); `set_aside` keeps the years the old reading would have counted, to say so.
    by_line: dict = {}
    for r in requirements:
        if r.years is not None and r.category in YEARS_CATEGORIES:
            by_line.setdefault((r.source_text, r.years), []).append(r)
    line_best, readings, set_aside, doubted, old_names, unsure, demand, labels = {}, {}, {}, {}, {}, {}, {}, {}
    either_way: set = set()   # demands that may be alternatives (D123 R4): never a rejection on its own
    # D151 (under D149): the posting's own lines, the line each record's quote comes from (an AI quote may be part of
    # one: the role is read on the whole line, Y2), and the demands the rules extractor itself read from them (b)
    posting = _posting_lines(requirements, job)
    filled = [line for line in posting if line.strip()]
    description = (job or {}).get("description_text") or ""
    # D155 (2) (Codex Pass 3, the AI record identity High): a record is the rules' own only when its whole identity
    # (category, name, strength, line, figure) is one the rules extractor read from this posting; a demand rejects only
    # on such records (D151 (b)), and a line holding both is judged on the rules' records alone: an AI record never
    # replaces, hides or stands in for one. Where the AI's records judge the line otherwise, it is to verify
    rules_ids = {_identity(r) for r in extract(description, policy).requirements
                 if r.years is not None} if description.strip() else set()
    ai_beside: dict = {}
    for key in list(by_line):
        ours = [r for r in by_line[key] if _identity(r) in rules_ids]
        if ours and len(ours) < len(by_line[key]):
            ai_beside[key] = [r for r in by_line[key] if _identity(r) not in rules_ids]
            by_line[key] = ours

    def home(quote: str) -> str:
        found = _homes(quote, filled)
        return found[0] if len(found) == 1 else quote
    plain: set = set()       # demands that name only total experience, a domain or a tool (D151 (a))
    total: set = set()       # demands that name only total professional experience (D155 (1))
    untrusted: set = set()   # demands the reader did not read whose words name something else: never judged on old years
    for key, records in by_line.items():
        # D120: a figure stated again for a demand of its own ("8+ years as a BSA and 8+ years of healthcare experience")
        # shares this key; each demand is judged on the records its own words name, and the stricter decides (a
        # rejection, then an unknown, then the fewer years), so no demand is lost behind another's reading
        hard = any(r.requirement_type == "hard_required" for r in records)
        line = home(key[0])
        occurrences = demand_occurrences(line, key[1])
        demands = [_demand(key, line, records if len(occurrences) == 1 else
                           role_years.demand_records(records, line, key[1], n, policy), profile, policy, n)
                   for n in occurrences]
        for d in demands:
            # D151 (a): plain in its own words on the line, and every record it is judged on names total experience, a
            # domain or a tool (an AI record named "business systems analyst" is no plain demand)
            mine = records if len(occurrences) == 1 else role_years.demand_records(records, line, key[1], d["occurrence"],
                                                                                   policy)
            d["plain"] = d["reading"] is None and role_years.plain_demand(line, key[1], policy, d["occurrence"]) and all(
                role_years.area_kind(r.name, policy) != "specific" for r in mine)
            # D155 (1): only total professional experience, no domain, tool, role or function, in words and records
            d["total"] = d["plain"] and role_years.total_demand(line, key[1], policy, d["occurrence"]) and all(
                r.category in ("years_experience", "experience") and role_years.area_kind(r.name, policy) == "generic"
                for r in mine)

        def strictness(d: dict, hard=hard, required=key[1]) -> tuple:
            ruling = _demand_outcome(d, required, hard, major_gap)
            if d["reading"] is None and not d["plain"]:   # D151 (a): words the reader could not read: never judged
                ruling = "unknown"
            if ruling == "reject" and d["reading"] is not None:   # D149 R-Y0: a role's shortfall never rejects, so
                ruling = "unknown"                               # another demand's rejection is never hidden behind it
            return _RANK[ruling], -d["have"] if d["have"] is not None else 0.0
        found = max(demands, key=strictness)
        if strictness(found)[0] == _RANK["reject"] and demands_cued(key[0], key[1]):
            # D123 R4: a cue of an alternative between the demands ("8+ years as a BSA, or a Bachelor's degree and 8+
            # years of healthcare experience", "... in a payer or provider setting and 8+ years of ..."): whether they
            # are joined by "or" or by "and" is unclear, so the stricter one's rejection only flags; never a pass on the
            # other demand.
            either_way.add(key)
        known = [d for d in demands if d["have"] is not None]
        if known and all(r.requirement_type == "preferred" for r in records):
            # a preference flags the known shortfall; an unknown demand beside it never hides it (D123's check)
            found = min(known, key=lambda d: d["have"])
        demand[key], old_names[key] = found["occurrence"], found["old_name"]   # and what the old years were for
        if len(occurrences) > 1 and found["reading"] is None and found["old_name"]:
            labels[key] = f"of {found['old_name']}"   # the demand judged, not the line's first record
        if found["have"] is not None:
            line_best[key] = found["have"]
        if found["reading"] is not None:
            readings[key] = found["reading"]
        if "unsure" in found:
            unsure[key] = found["unsure"]
        if "set_aside" in found:
            set_aside[key] = found["set_aside"]
        if "doubted" in found:
            doubted[key] = found["doubted"]
        if found["plain"]:
            plain.add(key)
            if found["total"]:
                total.add(key)
        elif found["reading"] is None and found["have"] is not None:
            untrusted.add(key)

    # D123 R1/R3: a figure read as a role, a function or an activity whose line holds any cue of an alternative, and any
    # figure on or after a line ending in a joiner, never rejects on its shortfall (CONDITIONAL); a role passes only on
    # years in it
    cues = _cues(by_line, readings, posting)
    # D149 R-Y1 and D151 (c) (red team Y3): a years demand, of any kind, rejects only when the posting's requirements
    # sections hold no cue of an alternative anywhere (`role_years.SECTION_CUES`, read from the posting's own lines);
    # with one, its far shortfall is CONDITIONAL. It never makes a line soft or a demand pass
    wide_cue = role_years.section_cue(line for line, _ in _section_lines(requirements, job))
    wide = {key: wide_cue for key in by_line if key not in cues} if wide_cue else {}

    def traced(key) -> bool:   # D151 (b), D155 (2): judged on records whose whole identity the rules extractor read
        return bool(by_line[key]) and all(_identity(r) in rules_ids for r in by_line[key])

    def allowed(key) -> bool:
        """D151, D155 (the owner, under D149): the one place a years demand may reject (years_far_short) or take a
        years_gap: only when (1) it names only total professional experience (`role_years.total_demand`; never a domain,
        a tool, a role, a function or an activity, nor words the reader cannot classify), (b) it is judged on records
        the rules extractor itself read, whole identity included (never an AI record), and (c) the posting's
        requirements hold no cue of an alternative anywhere."""
        return key in total and traced(key) and not wide_cue

    def outcome(key) -> str:   # what the years branch below makes of a figure, its other readings included
        hard = any(r.requirement_type == "hard_required" for r in by_line[key]) or (key in cues and key in readings)
        mine = _years_outcome(key[1], line_best.get(key), hard, major_gap)
        if key in untrusted:
            return "unknown"   # D151 (a): judged on years the line may not ask for: verify
        if mine == "reject" and (key in cues or key in either_way or key in wide or key in readings or not allowed(key)):
            return "unknown"   # D149 R-Y0, D151: only an allowed demand rejects
        return "unknown" if any(_years_outcome(key[1], years, hard, major_gap) != mine
                                for years in doubted.get(key, ())) else mine
    # "5+ years as a BSA or 3+ years as a PO" is judged once, on the line's role figures (D123 R2)
    judged, loose = _or_figures(by_line, line_best, outcome, {key: reading.kind for key, reading in readings.items()})
    loose |= either_way
    # Certifications (2026-10-15): judged per set of alternatives, on what you hold (careeros/qualify/certifications.py)
    cert_records = certification_mod.analyse(requirements, profile, policy)
    cert_group = {m["index"]: group for group in certification_mod.gate_groups(cert_records) for m in group}
    held = certification_mod.held_names(profile, policy)

    for req_index, req in enumerate(requirements):
        hard = req.requirement_type == "hard_required"
        soft_required = req.requirement_type in ("required_with_equivalency", "ambiguous")
        years_line = (req.source_text, req.years)
        if req.years is not None and req.category in YEARS_CATEGORIES:
            if years_line in judged or (years_line in ai_beside and req not in by_line[years_line]):
                continue   # D155 (2): a line the rules read is judged at one of the rules' own records, never at the AI's
            judged.add(years_line)
        what = readings[years_line].phrase() if years_line in readings else labels.get(years_line, f"of {req.name}")
        if req.requirement_type == "preferred" and req.years is not None and req.category in YEARS_CATEGORIES:
            have = line_best.get(years_line)
            if have is None and years_line in unsure:   # the known alternative's years (the fix-check of dba7b35)
                have = unsure[years_line]["years"]
            if have is not None and req.years > have:  # preferred years: a fit penalty only, never a rejection
                result.flag("years_preferred_short", f"{req.years:g}+ years {what} preferred; you have {have:g}", req.source_text)
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
            # Certifications never reject on their own (CR-2026-11-09-INTAKE-HOTFIX Pass 6, the owner's D140 F1): a
            # requirement you do not meet, however it is worded (hard, unclear, a set of alternatives none of which you
            # hold, or one only the AI read), is "verify the certification" (certification_check, material: CONDITIONAL),
            # never INELIGIBLE, in PRIMARY and NIGHT alike. A named credential required "or equivalent experience" that you
            # do not hold stays a note (certification_gap, not material: ELIGIBLE), as before D140 (the owner's answer,
            # 2026-11-11). A met one and a preferred one are as before
            if "requirement_satisfied" not in effects and effects & {"check_required", "hard_failure"}:
                if any(m["requirement_type"] == "ambiguous" for m in group):   # unclear: required or only helpful (D126)
                    what = f'{names}: unclear whether required or only helpful{notes} ("{group[0]["mention"]}")'
                elif all(m["kind"] == "credential" and m["requirement_type"] == "required_with_equivalency" for m in group):
                    result.flag("certification_gap", f"{names} requested{notes}", req.source_text)
                    result.flags[-1]["certifications"] = group
                    continue
                elif effects == {"hard_failure"}:   # every alternative is a named credential you do not hold
                    what = f"{names} required{notes}; certifications held: {', '.join(held) or 'none'}"
                elif all(m["kind"] == "credential" for m in group):
                    what = f"{names} requested{notes}"
                else:
                    what = f'a certification is mentioned: {names}{notes} ("{group[0]["mention"]}")'
                result.flag("certification_check", f"verify the certification: {what}", req.source_text)
                result.flags[-1]["certifications"] = group
                result.flags[-1]["hard_reading"] = effects == {"hard_failure"}   # read as required, you hold none

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
            # D123 R1: on a role line with a cue, a far shortfall is to verify even where the line reads as soft ("or
            # equivalent", "in lieu of ..."): never a years_gap pass
            hard = hard or (years_line in cues and years_line in readings)
            mine = _years_outcome(req.years, have, hard, major_gap)
            others = _other_readings(readings.get(years_line), doubted.get(years_line, ()), old_names.get(years_line, ""))
            split = next(((phrase, years) for phrase, years in others
                          if _years_outcome(req.years, years, hard, major_gap) != mine), None)
            if not split and mine == "pass":   # every reading passes: the strictest one's figure is shown (fix-check)
                for phrase, years in others:
                    if years is not None and have is not None and years < have:
                        what, have = phrase, years
            if split:
                # The Fable fix-check of ff3c7f7 (2026-11-05, D113): the line was read by a judgement call, and another
                # plausible reading judges it differently. Uncertain, so CONDITIONAL (D92), never a rejection.
                mine_text = _known(unsure[years_line]) if years_line in unsure else _yours(have)
                result.flag("years_role_unconfirmed",
                            f"{req.years:g}+ years {what} requested ({mine_text}); read another way, {req.years:g}+ "
                            f"years {split[0]} ({_yours(split[1])}): the two readings judge it differently, so it is not "
                            "a rejection: verify before applying", req.source_text)
            elif years_line in cues and mine == "reject":
                # D123 R1 (your decision 2026-11-09): the line may offer an alternative to the role, which the gate does
                # not try to read; a shortfall on the role is to be verified, never a rejection (a needless flag is fine)
                result.flag("years_role_unconfirmed",
                            f"{req.years:g}+ years {what} required ({_yours(have)}); the line may offer an alternative "
                            f"(\"{cues[years_line]}\"), so it is not a rejection: verify before applying", req.source_text)
            elif years_line in wide and mine == "reject":
                # D149 R-Y1: the posting's requirements offer an alternative somewhere ("OR:" on the next bullet, "...
                # may satisfy this requirement"), which the gate does not try to place: verify, never a rejection
                result.flag("years_role_unconfirmed",
                            f"{req.years:g}+ years {what} required ({_yours(have)}); the requirements may offer an "
                            f"alternative (\"{wide[years_line]}\"), so it is not a rejection: verify before applying",
                            req.source_text)
            elif years_line in loose and mine == "reject":
                # An "or" a few words before or after this figure names no alternative the gate knows ("or similar,
                # generally 3+ years"), or the figure is a second one under one name: it may be an alternative, so the
                # shortfall is not a rejection on its own (the fix-check of f74cdcc, D113)
                result.flag("years_role_unconfirmed",
                            f"{req.years:g}+ years {what} required ({_yours(have)}); the line may offer another figure "
                            "as an alternative, so it is not a rejection: verify before applying", req.source_text)
            elif years_line in readings and mine == "reject":
                # D149 R-Y0 (the owner's D149, the analog of D140 for certifications): years in a role, a function or an
                # activity never reject on their own, whatever the wording; far short, they are to verify (CONDITIONAL).
                # Total years (no role named), a domain or a tool far short, and every other hard failure still reject
                result.flag("years_role_unconfirmed",
                            f"{req.years:g}+ years {what} required ({_yours(have)}); a shortfall in a role, function or "
                            "activity is never a rejection on its own: verify before applying", req.source_text)
            elif have is None and years_line in unsure:
                # Codex Pass 1 F2, F4: what your record confirms falls short, but the line offers an alternative it does
                # not confirm, so that one is to be verified; never a rejection on the known one alone (D1, D21)
                credit = unsure[years_line]
                if credit.get("parts"):   # roles joined by "and" (D123's check): every one is a demand
                    each = ", ".join(f"{years:g} as {part}" for part, years in credit["parts"])
                    result.flag("years_role_unconfirmed",
                                f"{req.years:g}+ years {what} requested; it names {len(credit['parts'])} roles and you have "
                                f"{each}: each must be met, so verify before applying", req.source_text)
                else:
                    result.flag("years_role_unconfirmed",
                                f"{req.years:g}+ years {what} requested; you have {credit['years']:g} as {credit['item']}"
                                f"{' (a partial match)' if credit.get('partial') else ''}, and nothing in your record "
                                f"confirms {', '.join(re.sub(r'^(?:an?|the)\s+', '', a) for a in credit['unconfirmed'])}: "
                                "verify before applying", req.source_text)
            elif have is None and years_line in set_aside:
                # OPP-43, your decision D92: no role, function or skill in your record names it, so it is unconfirmed
                # (CONDITIONAL: verify before applying), never a rejection
                result.flag("years_role_unconfirmed", f"{req.years:g}+ years {what} requested; nothing in your record names "
                                                      f"it, and the {set_aside[years_line]:g} years counted for it before (a "
                                                      "domain, a tool or your total years) are not years in it: verify "
                                                      "before applying", req.source_text)
            elif have is None:
                result.flag("years_unknown", f"{req.years:g}+ years {what} requested; your years not set", req.source_text)
            else:
                # Prompts v2.1 (2026-10-12): severity-based. Up to 2 years short: moderate penalty; more than 2: major
                # penalty; more than 4 short on a clearly REQUIRED number: rejected. Never on preferred years.
                gap = req.years - have
                why = _not_allowed(years_line in plain, traced(years_line), wide_cue, years_line in total)
                if years_line in untrusted:
                    # D151 (a), red team Y1: the line names a role, a function or words the reader could not read
                    # ("Product Owner: 10+ years", "roles such as Product Owner"); the years counted for it (your total
                    # years, a domain) may not be years in what it asks, so it never passes or rejects on them
                    result.flag("years_role_unconfirmed",
                                f"{req.years:g}+ years required on a line that names more than total experience, a domain "
                                f"or a tool, which the reader could not read ({have:g} years counted for it are not "
                                "confirmed to be years in it): verify before applying", req.source_text)
                elif gap > minor_gap and why:
                    # D151: only an allowed demand rejects or takes a years_gap; any other shortfall is to verify
                    result.flag("years_role_unconfirmed",
                                f"{req.years:g}+ years {what} required ({_yours(have)}); {why}, so it is not a rejection: "
                                "verify before applying", req.source_text)
                elif gap > major_gap and hard:
                    result.fail("years_far_short", f"{req.years:g}+ years {what} required; you have {have:g}", req.source_text,
                                profile=f"{have:g} years (your best match on this line)", confidence=req.confidence, strength=requirement_strength(req.requirement_type))
                    # 2026-10-15: the rejection keeps the figure and the line's Experience Match, so it can be explained
                    result.hard_failures[-1].update({"required_years": req.years, "experience": experience_mod.gate_line(
                        [r for r in requirements if (r.source_text, r.years) == years_line], profile, policy,
                        demand.get(years_line, 0))})
                elif gap > minor_gap:
                    result.flag("years_gap", f"{req.years:g}+ years {what} requested; you have {have:g}", req.source_text)
                elif gap > 0:
                    result.flag("years_slightly_short", f"{req.years:g}+ years {what}; you have {have:g}", req.source_text)

    # D155 (2): an AI record on a line the rules also read never replaces the rules' records (judged above on them alone);
    # where the AI's records would judge the line otherwise (met against short, or one unknown), it is to verify
    for key, others in ai_beside.items():
        if all(r.requirement_type == "preferred" for r in by_line[key] + others):
            continue

        def most(records: list) -> float | None:
            found = [y for r in records if (y := best_years(profile, r.name)) is not None]
            return max(found) if found else None

        def judged_as(years: float | None, required=key[1]) -> str:
            return "unknown" if years is None else "met" if years >= required else "short"
        rules_years, ai_years = most(by_line[key]), most(others)
        if judged_as(rules_years) != judged_as(ai_years):
            result.flag("years_role_unconfirmed",
                        f"{key[1]:g}+ years: the AI read this line as {', '.join(r.name for r in others)} "
                        f"({_yours(ai_years)}), the rules as {', '.join(r.name for r in by_line[key])} ({_yours(rules_years)}): "
                        "the readings judge it differently, so verify before applying", key[0])

    # D149 R-Y2: a years figure on a demanded requirements line that no extracted record carries was never judged, so it
    # is to verify (a material flag: CONDITIONAL), never a silent pass; a plain failure elsewhere still rejects
    for line, figure in _unread_years(requirements, job, policy):
        what = f"{figure:g}+ years on this requirements line were" if figure is not None else \
            "the years this requirements line asks for were"
        result.flag("years_role_unconfirmed", f"{what} not read as any requirement, so they were never judged: verify "
                                              "before applying", line)

    for conflict in experience_mod.conflicts(requirements):  # separate hard lines, one area, different years
        result.flag("years_conflicting", f"{', '.join(f'{y:g}' for y in conflict['years'])} years of {conflict['area']} "
                                         f"stated; the higher figure ({conflict['counted']:g}) counts", conflict["source_text"])
