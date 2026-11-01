"""NIGHT / overnight profile (prompts v2.1, split into two modes by v2.2): remote overnight, evening, weekend or
flexible work, judged and scored separately from PRIMARY. It never changes a job's PRIMARY state, gate result or score.

Shared with PRIMARY: US/home-state/remote-only rules, excluded employers, excluded titles, scam signals, any employer-level
eligibility hold (gate.held_parents), and
the candidate's hard requirements (degree, licence, certification, clearance, work authorisation, years).
NIGHT-specific: its own title families, working supervisors/team leads allowed (managers and above are not), part-time
allowed, a schedule that must sit outside normal daytime hours, and two modes:
- SUPPLEMENTAL: work that can sit alongside a daytime job (part-time, evening, weekend, flexible, or hours unknown);
- REPLACEMENT: a full-time / contract overnight or third-shift job that could become the main job.
Each mode has its own pay settings (unset = flagged, never a rejection), weights and leaderboard. Every result carries
an eligibility state (ELIGIBLE / CONDITIONAL / HOLD / INELIGIBLE).
"""
from __future__ import annotations

import re

from careeros.normalize.posting import contains_term, excluded_term, norm

from .gate import (GateResult, annualize, comp_trusted, excluded_employer, held_rule, location_rules, requirement_rules,
                   scam_rules)
from .profile import Profile

NIGHT_VERSION = "night-2026-10-21a"  # 10-21a: Codex Pass 2 (the same hours on other days or zones are another schedule);
# 10-20a: Codex review (each stated range measured, MDT, zone only from the shift's sentence, agency aliases); 10-18a: review fixes (days, zones, every stated range, both seasons, held families);
# 10-17a: familiar names without a resolved identity; 10-16b: workload in real time
# zones; 10-16a: family lookup for conflicts
_MANAGER = re.compile(r"\b(manager|director|head|vp|vice president|chief)\b")
PHONE_VALUE = {"light": 1.0, "unknown": 0.6, "heavy": 0.2}


def settings(policy) -> dict:
    return policy.get("night", default={}) or {}


def enabled(policy) -> bool:
    return bool(settings(policy).get("enabled"))


def night_family(title_norm: str, policy) -> str | None:
    for name, terms in (settings(policy).get("families") or {}).items():
        if any(contains_term(title_norm, norm(t)) for t in terms):
            return name
    return None


# A negation reaches a few words ahead ("no night shifts or weekend shifts") but never past the end of its sentence
# or clause ("No prior experience needed. Night shift role." is a night-shift role).
_NEGATION = re.compile(r"\b(no|not|never|without|non)\b(\W+\w+){0,6}\W*$")
_CLAUSE_END = re.compile(r"[.;:!?\n]")


def _pattern(term: str) -> str:
    words = norm(term).split()
    return r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])"


def _incidental(before: str, after: str, policy) -> bool:
    """'occasional after-hours work', 'after-hours support as needed': a day job with some extra hours, not night work."""
    words = (settings(policy).get("schedule") or {}).get("incidental") or []
    return any(re.search(_pattern(w), before[-40:]) or re.search(_pattern(w), after[:40]) for w in words)


def _stated(text: str, term: str, policy=None) -> bool:
    """The schedule phrase appears in the posting (any punctuation between its words), not negated, not incidental."""
    for m in re.finditer(_pattern(term), text):
        clause = _CLAUSE_END.split(text[max(0, m.start() - 80):m.start()])[-1]
        after = _CLAUSE_END.split(text[m.end():m.end() + 80])[0]
        if _NEGATION.search(clause) or (policy is not None and _incidental(clause, after, policy)):
            continue
        return True
    return False


_CLOCK = r"(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s*m?\.?"
_RANGE_12H = re.compile(r"(?<![\d:])" + _CLOCK + r"\s*(?:-|\u2013|\u2014|to|until|thru|through)\s*" + _CLOCK + r"(?![a-z])")
_RANGE_24H = re.compile(r"(?<![\d:])([01]\d|2[0-3]):?([0-5]\d)\s*(?:-|\u2013|\u2014|to)\s*([01]\d|2[0-3]):?([0-5]\d)(?![\d:])")


def _hours_kind(start: int, end: int) -> str | None:
    if start in (20, 21, 22, 23, 0, 1, 2) and 4 <= end <= 10:
        return "overnight"
    if 13 <= start <= 18 and (end >= 21 or end <= 2):
        return "evening"
    if 5 <= start <= 10 and 13 <= end <= 19:
        return "daytime"
    return None


# Daytime hours that describe the business rather than the role ("operational hours 9AM-5PM", "hours for training",
# "Business Hours are 6a-6p") are not evidence enough to reject a NIGHT job: they make it CONDITIONAL instead.
_INDIRECT_HOURS = re.compile(r"\b(training|business hours|operational hours|operating hours|office hours|support hours|"
                             r"hours of operation|open|coverage|clients?|customers?|providers?)\b")


_SENTENCE_END = re.compile(r"[.;!?\n](?!\d)")  # a colon does not end it ("hours for training: 9am-5pm")


def _indirect(text: str, start: int) -> bool:
    clause = _SENTENCE_END.split(text[max(0, start - 120):start])[-1]
    return bool(_INDIRECT_HOURS.search(clause))


def _ranges(text: str, every: bool = False) -> list[tuple[str, str, int, int, int, int]]:
    """Every recognised time range as (kind, evidence, start minute, end minute, match start, match end): minutes after
    midnight, then where the range sits in `text` (for the time zone and days written next to it). `every` also keeps
    ranges of no recognised kind ("12pm-8:30pm", "7pm-3am") as 'other', for the workload (2026-10-18)."""
    found = []
    for m in _RANGE_12H.finditer(text):
        h1, h2 = int(m.group(1)), int(m.group(4))
        if not (1 <= h1 <= 12 and 1 <= h2 <= 12):
            continue
        start = h1 % 12 + (12 if m.group(3) == "p" else 0)
        end = h2 % 12 + (12 if m.group(6) == "p" else 0)
        kind = _hours_kind(start, end) or ("other" if every else None)
        if kind:
            found.append(("daytime_likely" if kind == "daytime" and _indirect(text, m.start()) else kind, m.group(0).strip(),
                          start * 60 + int(m.group(2) or 0), end * 60 + int(m.group(5) or 0), m.start(), m.end()))
    for m in _RANGE_24H.finditer(text):
        kind = _hours_kind(int(m.group(1)), int(m.group(3))) or ("other" if every else None)
        if kind:
            found.append(("daytime_likely" if kind == "daytime" and _indirect(text, m.start()) else kind, m.group(0).strip(),
                          int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4)), m.start(), m.end()))
    return found


def _preferred_range(text: str):
    found = _ranges(text)
    for preferred in ("overnight", "evening", "daytime", "daytime_likely"):
        for item in found:
            if item[0] == preferred:
                return item
    return None


def hours_of(text: str) -> tuple[str, str] | None:
    """The kind of hours stated as a time range ("10:00 PM - 6:30 AM", "2300-0730"), if any. Night ranges win.
    Daytime ranges stated as business, training or coverage hours come back as 'daytime_likely'."""
    item = _preferred_range(text)
    return (item[0], item[1]) if item else None


def shift_window(text: str) -> tuple[int, int] | None:
    """(start, end) minutes after midnight of the stated overnight or evening shift, if the posting gives one."""
    found = shift_reading((text or "").lower())
    return found[0] if found else None


def shift_reading(lowered: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """((start, end) minutes, (match start, match end) in `lowered`) of the stated overnight or evening shift."""
    item = _preferred_range(lowered)
    return ((item[2], item[3]), (item[4], item[5])) if item and item[0] in ("overnight", "evening") else None


def workload(text: str, schedule: str, policy, *, now: str | None = None, location: str | None = None) -> dict:
    """Workload of SUPPLEMENTAL work stacked on your day job (2026-10-16): measured on dated intervals in real time zones
    by careeros/qualify/worktime.py (overlap, hours per duty span, breaks, sleep opportunity, weekly hours). Informational:
    only a CONFIRMED overlap (zone resolved, days stated) makes Supplemental impossible."""
    from datetime import datetime, timezone

    from . import worktime
    cfg = settings(policy)
    lowered = (text or "").lower()
    # EVERY stated range counts for the workload (2026-10-18), not only evening or overnight ones: "Hours: 9am to 5pm"
    # on a NIGHT posting overlaps your day job. Ranges that may describe the business can never confirm a result.
    found = _ranges(lowered, every=True)
    # each distinct stated range is measured, not only the preferred one (independent Codex review of f984d15,
    # 2026-10-20): "6pm-10pm and 9am-5pm" must never be confirmed from the evening range alone. Distinct means the whole
    # reading (Codex Pass 2, 2026-10-21): the same hours on other days or in another zone are another schedule, and only a
    # restatement of the same hours, days and zone is measured once
    readings: list = []
    seen = set()
    for kind in ("overnight", "evening", "daytime", "other", "daytime_likely"):
        for f in found:
            if f[0] != kind:
                continue
            span = (f[4], f[5])
            days, day_evidence = worktime.days_reading(lowered, span, schedule)
            zone = worktime.zone_reading(lowered, span, location)
            key = (f[2], f[3], tuple(days) if days else None, zone.get("basis"), tuple(zone.get("zones") or ()),
                   kind == "daytime_likely")
            if key not in seen:
                seen.add(key)
                readings.append((f, days, day_evidence, zone))
    if not readings:   # no stated range: one reading that says so
        days, day_evidence = worktime.days_reading(lowered, None, schedule)
        readings.append((None, days, day_evidence, worktime.zone_reading(lowered, None, location)))
    reference = (datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)).date()
    loads = []
    for item, days, day_evidence, zone in readings:
        shift = (item[2], item[3]) if item else None
        load = worktime.assess(cfg.get("day_job") or {}, shift, days, zone, reference, cfg.get("workload"),
                               hours_direct=not item or item[0] != "daytime_likely")
        load["days_evidence"] = day_evidence
        loads.append(load)
    return loads[0] if len(loads) == 1 else worktime.combine(loads)


def schedule_of(text: str, policy, title: str = "") -> tuple[str, str]:
    """(overnight | evening | weekend | flexible | daytime | unknown, the evidence). Evidence order: bare words in the
    job title, then schedule phrases in the posting, then stated hours. Never bare words in the body that describe the
    company ("change doesn't happen overnight"), negated phrases or incidental extra hours."""
    lowered = (text or "").lower()
    terms = settings(policy).get("schedule") or {}
    categories = ("overnight", "evening", "weekend", "flexible")
    title_lowered = (title or "").lower()
    for category in categories:
        for term in (terms.get("title_terms") or {}).get(category, []):
            if title_lowered and _stated(title_lowered, term):
                return category, f"title: {term}"
    for category in categories:
        for term in terms.get(category, []):
            if _stated(lowered, term, policy):
                return category, term
    hours = hours_of(lowered)
    if hours and hours[0] in ("overnight", "evening"):
        return hours
    for term in terms.get("daytime_only", []):
        if _stated(lowered, term, policy):
            return "daytime", term
    if hours:
        return hours
    for term in terms.get("daytime_likely", []):
        if _stated(lowered, term, policy):
            return "daytime_likely", term
    return "unknown", ""


def phone_level(text: str, policy) -> tuple[str, list]:
    blob = norm(text)
    cfg = settings(policy)
    heavy = [t for t in cfg.get("phone_heavy", []) if contains_term(blob, norm(t))]
    light = [t for t in cfg.get("low_phone", []) if contains_term(blob, norm(t))]
    if heavy and len(heavy) >= len(light):
        return "heavy", heavy
    return ("light", light) if light else ("unknown", [])


def _hourly(amount, period):
    yearly = annualize(amount, period)
    return None if yearly is None else yearly / 2080


def mode_for(schedule: str, employment: str, policy) -> str:
    """Your rule (2026-10-12): full-time-type work on a true overnight / third shift is REPLACEMENT; everything else
    (part-time, evening, weekend, flexible, unknown hours) is SUPPLEMENTAL."""
    full = employment in (settings(policy).get("replacement_employment") or ["full_time", "contract", "contract_to_hire"])
    return "replacement" if schedule == "overnight" and full else "supplemental"


def familiar_family(job: dict, profile: Profile, policy) -> bool:
    """The posting's employer is one you have worked for or are placed with (profile familiar_employers). Either stored
    name counts on its own (2026-10-17): it resolves into a familiar employer's family ('NorthgateRx' for Northgate, through
    the shared employer-family lookup), or it is a familiar name with a legal suffix or more words after it ('Brightpath
    Staffing, LLC'). The two names need not agree: a stale parent or an agency posting must still raise the review prompt,
    so an unresolved or conflicting identity is not read as "not familiar". A name you listed as a different
    organisation never counts. An employer that names nothing familiar is not familiar (a review prompt, never a
    permission)."""
    if profile.familiar_with(job.get("employer_name"), job.get("parent")):
        return True
    familiar = [str(n) for n in profile.data.get("familiar_employers") or [] if n] + hold_names(policy)
    if not familiar:
        return False
    from careeros.core import private_sites
    from careeros.normalize.employer_identity import RESOLVED, legal_norm, listed_agencies, resolve, starts_with_name
    parents = policy.get("parents", default={}) or {}
    different = policy.get("employer_identity", "different_organisations", default=[]) or []
    agencies = [*listed_agencies(policy), *private_sites.staffing_agencies()]
    keys = {found.key for found in (resolve(n, None, parents, different=different) for n in familiar)
            if found.status == RESOLVED and found.key}
    names = {legal_norm(n) for n in familiar} - {""}
    for value in (job.get("employer_name"), job.get("parent")):
        one = resolve(value, None, parents, agencies=agencies, different=different)
        if one.evidence[0].rule in ("empty", "different_organisation"):
            continue
        if one.status == RESOLVED and one.key in keys:
            return True
        if any(starts_with_name(one.evidence[0].normalized, n) for n in names):
            return True
    return False


def hold_names(policy) -> list[str]:
    """The employers your submission holds name (or, while those are unset, the gate's held_parents): employers you are
    placed with, so their family counts as familiar for the conflict-of-interest prompt (2026-10-18). Read as written;
    malformed settings add nothing here (submission refuses them on its own)."""
    holds = policy.get("submission", "holds", default=None)
    if holds is None:
        legacy = policy.get("gate", "held_parents", default=[]) or []
        return [n for n in legacy if isinstance(n, str) and n.strip()] if isinstance(legacy, list) else []
    names = []
    for hold in holds if isinstance(holds, list) else []:
        if isinstance(hold, dict) and isinstance(hold.get("names"), list):
            names += [n for n in hold["names"] if isinstance(n, str) and n.strip()]
    return names


def coi_review(job: dict, profile: Profile, policy, *, overlap_minutes: int = 0, application_conflict: bool = False) -> dict:
    """Two separate levels for SUPPLEMENTAL work (2026-10-12), never a legal conclusion:
    - manual_coi_review_required: a concrete reason to check before applying (same employer family, a known client,
      vendor or competitor of your daytime employer, your employment agreement, a posting that forbids other work,
      overlapping hours, or an application-path / right-to-represent conflict). Makes the job CONDITIONAL.
    - industry_overlap: only the same industry as your daytime work (PBM / payer claims). Informational."""
    from .scores import domain_level
    coi = settings(policy).get("coi") or {}
    names = norm(" ".join(filter(None, [job.get("employer_name"), job.get("parent")])))
    listed = lambda key: [e for e in coi.get(key) or [] if norm(e) and contains_term(names, norm(e))]
    reasons = []
    if familiar_family(job, profile, policy):
        reasons.append("same employer family as one you have worked for or are placed with")
    if listed("clients_vendors"):
        reasons.append("a known client or vendor of your daytime employer")
    if listed("competitors"):
        reasons.append("a direct competitor of your daytime employer")
    if coi.get("agreement_restricts_outside_work"):
        reasons.append("your employment agreement restricts outside work")
    text = norm(job.get("description_text") or "")
    forbidding = [t for t in coi.get("posting_terms") or [] if contains_term(text, norm(t))]
    if forbidding:
        reasons.append(f"the posting limits other employment ('{forbidding[0]}')")
    if overlap_minutes:
        reasons.append("its hours overlap your day job")
    if application_conflict:
        reasons.append("this role already has an application through another channel (right to represent)")
    level = domain_level(job, policy)["level"]
    overlap = level in ("direct", "payer")
    return {"manual_coi_review_required": bool(reasons), "reasons": reasons, "industry_overlap": overlap,
            "industry": ("PBM / pharmacy claims" if level == "direct" else "payer / claims") if overlap else None}


def night_state(result: GateResult, policy) -> str:
    if result.hard_failures:
        return "INELIGIBLE"
    codes = {f["code"] for f in result.flags}
    if "held_d13" in codes:
        return "HOLD"
    material = set(policy.get("eligibility_states", "material_unknowns", default=[]) or [])
    material |= set(settings(policy).get("material_unknowns_extra") or [])
    material.add("possible_day_job_overlap")   # always material, whatever a settings list says (2026-10-18)
    return "CONDITIONAL" if codes & material else "ELIGIBLE"


def screen(job: dict, profile: Profile, policy) -> GateResult:
    """The cheap part (title, employer, location, employment type): run before reading requirements."""
    result = GateResult("eligible")
    cfg = settings(policy)
    title_norm = job["title_norm"]
    if excluded_employer(job, policy):
        result.fail("employer_excluded", "employer is on your exclusion list")
    term = excluded_term(title_norm, policy.get("title_exclude", default=[]) + list(cfg.get("title_exclude_extra", [])))
    if term:
        result.fail("title_excluded", f"title contains excluded term '{term}'")
    if _MANAGER.search(title_norm):  # NIGHT allows working supervisors and team leads, not managers and above
        result.fail("night_manager_role", "manager-level or senior leadership title")
    if not night_family(title_norm, policy):
        result.fail("no_night_family", "title does not match any NIGHT job family")
    location_rules(result, job, profile, policy, remote_only=True)
    employment = job.get("employment_type") or "unknown"
    if employment not in cfg.get("employment_types_allowed", []):
        result.fail("employment_type", f"employment type '{employment}' is not one NIGHT accepts")
    if result.hard_failures:
        result.decision = "rejected"
    return result


def _pay(result: GateResult, job: dict, mode: str, comp: dict) -> None:
    if mode == "supplemental":
        floor, top, unit = comp.get("floor_hourly"), _hourly(job.get("comp_max"), job.get("comp_period")), "hour"
    else:
        floor, top, unit = comp.get("floor_annual"), annualize(job.get("comp_max"), job.get("comp_period")), "year"
    if not floor:
        result.flag("night_pay_unconfigured", f"NIGHT {mode} pay floor not set: pay needs your decision")
    elif top is not None and top < floor:
        if comp_trusted(job):
            result.fail("below_night_pay_floor", f"posted maximum ${top:,.2f}/{unit} is below your NIGHT {mode} floor",
                        profile=f"NIGHT {mode} floor: ${floor:,.2f}/{unit}")
        else:
            result.flag("pay_untrusted_below_floor", "pay shown by an aggregator site is below your floor (an estimate)")


def _compensation_value(job: dict, mode: str, comp: dict) -> float:
    if job.get("comp_min") is None and job.get("comp_max") is None:
        return 0.5
    low, high = job.get("comp_min") or job.get("comp_max"), job.get("comp_max") or job.get("comp_min")
    if mode == "supplemental":
        mid, target = ((_hourly(low, job.get("comp_period")) or 0) + (_hourly(high, job.get("comp_period")) or 0)) / 2, \
            comp.get("target_hourly") or comp.get("floor_hourly")
    else:
        mid, target = ((annualize(low, job.get("comp_period")) or 0) + (annualize(high, job.get("comp_period")) or 0)) / 2, \
            comp.get("target_annual") or comp.get("floor_annual")
    if not target:
        return 0.5
    return max(0.2, min(1.0, 0.6 + 0.4 * (mid - target) / max(1.0, target * 0.25)))


def assess(job: dict, requirements: list, profile: Profile, policy, fresh_score: float, *,
           application_conflict: bool = False, now: str | None = None) -> dict:
    from .gate import principal_rule
    from .scores import candidate_fit
    cfg = settings(policy)
    result = screen(job, profile, policy)
    principal_rule(result, job, policy)
    text = f"{job.get('title') or ''}\n{job.get('location_raw') or ''}\n{job.get('description_text') or ''}"
    scam_rules(result, norm(text), policy)
    schedule, schedule_term = schedule_of(text, policy, title=job.get("title") or "")
    if schedule == "daytime":
        result.fail("daytime_schedule", f"daytime schedule ('{schedule_term}'); NIGHT needs overnight, evening, weekend or flexible hours",
                    schedule_term)
    elif schedule == "daytime_likely":
        result.flag("daytime_hours_likely", f"posting gives daytime business or training hours ('{schedule_term}'); "
                                            "check whether the role itself works them", schedule_term)
    elif schedule == "unknown":
        result.flag("schedule_unknown", "posting does not state its hours; not assumed to fit")
    employment = job.get("employment_type") or "unknown"
    mode = mode_for(schedule, employment, policy)
    mode_cfg = (cfg.get("modes") or {}).get(mode) or {}
    comp = mode_cfg.get("compensation") or {}
    _pay(result, job, mode, comp)
    load, coi = None, None
    if mode == "supplemental":
        load = workload(text, schedule, policy, now=now, location=job.get("location_raw"))
        confirmed_overlap = load["overlap_minutes"] if load.get("confirmed") else 0
        if confirmed_overlap:
            result.fail("overlaps_day_job", f"stated hours overlap your day job ({load['basis']}); Supplemental work must "
                                            "fit around it", schedule_term, profile="night.day_job hours", confidence=0.9)
        elif load["overlap_minutes"]:
            result.flag("possible_day_job_overlap", f"the stated hours may overlap your day job ({load['basis']}); check the "
                                                    "time zone and days before relying on it")
        coi = coi_review(job, profile, policy, overlap_minutes=confirmed_overlap, application_conflict=application_conflict)
        if coi["manual_coi_review_required"]:
            result.flag("night_coi_review", f"check for a conflict with your daytime job ({'; '.join(coi['reasons'])}): "
                                            "your manual review, not a legal judgement")
        elif coi["industry_overlap"]:
            result.flag("night_industry_overlap", f"same industry as your daytime work ({coi['industry']}): informational only")
    held_rule(result, job, policy)
    requirement_rules(result, requirements, profile, policy, job)
    if result.hard_failures:
        result.decision = "rejected"
    state = night_state(result, policy)

    family = night_family(job["title_norm"], policy)
    phone, phone_terms = phone_level(text, policy)
    out = {"decision": result.decision, "state": state, "mode": mode, "failures": result.hard_failures, "flags": result.flags,
           "family": family, "schedule": schedule, "phone": phone, "priority": None, "tier": None, "dims": {},
           "workload": load, "coi": coi}
    if result.decision != "eligible":
        return out

    stress_terms = [t for t in cfg.get("stress_terms", []) if contains_term(norm(text), norm(t))]
    stability = float((policy.get("stability", default={}) or {}).get(employment, 0.6))
    if contains_term(norm(text), "benefits"):
        stability = min(1.0, stability + 0.1)
    dims = {
        "schedule": float((mode_cfg.get("schedule_value") or {}).get(schedule, 0.4)),
        "compensation": _compensation_value(job, mode, comp),
        "candidate_fit": candidate_fit(job, requirements, profile, policy)["score"] / 100,
        "low_phone_stress": max(0.1, PHONE_VALUE[phone] - 0.15 * len(stress_terms)),
        "stability": stability,
        "freshness": fresh_score / 100,
    }
    weights = mode_cfg.get("weights") or {}
    total = sum(weights.values()) or 1
    priority = round(100 * sum(dims[k] * w for k, w in weights.items()) / total, 1)
    tiers = cfg.get("tiers") or {}
    eligible = state == "ELIGIBLE"
    tier = ("A" if eligible and priority >= tiers.get("A", 90) else "B" if eligible and priority >= tiers.get("B", 82)
            else "near_miss" if priority >= tiers.get("near_miss", 65) else "archive")
    out.update(priority=priority, tier=tier, dims={k: round(v, 3) for k, v in dims.items()},
               evidence={"schedule": schedule_term, "phone": phone_terms[:5], "stress": stress_terms[:5]})
    return out
