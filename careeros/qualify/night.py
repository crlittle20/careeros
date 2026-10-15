"""NIGHT / overnight profile (prompts v2.1, split into two modes by v2.2): remote overnight, evening, weekend or
flexible work, judged and scored separately from PRIMARY. It never changes a job's PRIMARY state, gate result or score.

Shared with PRIMARY: US/home-state/remote-only rules, excluded employers, excluded titles, scam signals, the D13 hold, and
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

NIGHT_VERSION = "night-2026-10-12h"
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


def _ranges(text: str) -> list[tuple[str, str, int, int]]:
    """Every recognised time range as (kind, evidence, start minute, end minute), minutes after midnight."""
    found = []
    for m in _RANGE_12H.finditer(text):
        h1, h2 = int(m.group(1)), int(m.group(4))
        if not (1 <= h1 <= 12 and 1 <= h2 <= 12):
            continue
        start = h1 % 12 + (12 if m.group(3) == "p" else 0)
        end = h2 % 12 + (12 if m.group(6) == "p" else 0)
        kind = _hours_kind(start, end)
        if kind:
            found.append(("daytime_likely" if kind == "daytime" and _indirect(text, m.start()) else kind, m.group(0).strip(),
                          start * 60 + int(m.group(2) or 0), end * 60 + int(m.group(5) or 0)))
    for m in _RANGE_24H.finditer(text):
        kind = _hours_kind(int(m.group(1)), int(m.group(3)))
        if kind:
            found.append(("daytime_likely" if kind == "daytime" and _indirect(text, m.start()) else kind, m.group(0).strip(),
                          int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))))
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
    item = _preferred_range((text or "").lower())
    return (item[2], item[3]) if item and item[0] in ("overnight", "evening") else None


def _clock(value) -> int | None:
    match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(value or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def _span(start: int, end: int) -> int:
    return (end - start) % 1440 or 1440


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Minutes two daily windows share (either may cross midnight)."""
    minutes = lambda w: {(w[0] + i) % 1440 for i in range(_span(*w))}
    return len(minutes(a) & minutes(b))


def workload(schedule: str, shift: tuple[int, int] | None, policy) -> dict:
    """Informational workload sustainability for NIGHT-SUPPLEMENTAL work stacked on your daytime job (2026-10-12):
    green / caution / severe / unknown. Never a rejection by itself; overlapping hours are reported separately
    (`overlap_minutes`) because they make Supplemental impossible rather than merely tiring."""
    cfg = settings(policy)
    day = cfg.get("day_job") or {}
    limits = {"caution_total_hours": 12, "severe_total_hours": 14, "caution_min_rest_hours": 8, "severe_min_rest_hours": 6,
              **(cfg.get("workload") or {})}
    day_window = (_clock(day.get("start")), _clock(day.get("end")))
    if None in day_window:
        return {"level": "unknown", "basis": "your day-job hours are not set (night.day_job in vault overrides)",
                "overlap_minutes": 0}
    weekdays = {"mon", "tue", "wed", "thu", "fri"}
    if schedule == "weekend" and set(day.get("days") or weekdays) <= weekdays:
        return {"level": "green", "basis": "weekend work does not stack on a weekday day job", "overlap_minutes": 0}
    if shift is None:
        return {"level": "unknown", "basis": "the posting does not state its hours", "overlap_minutes": 0}
    overlap = _overlap(day_window, shift)
    day_minutes, shift_minutes = _span(*day_window), _span(*shift)
    total = (day_minutes + shift_minutes) / 60
    rest = max((shift[0] - day_window[1]) % 1440, (day_window[0] - shift[1]) % 1440) / 60 if not overlap else 0.0
    if overlap:
        level = "severe"
    elif total >= limits["severe_total_hours"] or rest < limits["severe_min_rest_hours"]:
        level = "severe"
    elif total > limits["caution_total_hours"] or rest < limits["caution_min_rest_hours"]:
        level = "caution"
    else:
        level = "green"
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    basis = (f"shift {fmt(shift[0])}-{fmt(shift[1])} overlaps your day job by {overlap} minutes" if overlap else
             f"shift {fmt(shift[0])}-{fmt(shift[1])} + day job {fmt(day_window[0])}-{fmt(day_window[1])}: "
             f"{total:g} working hours a day, longest rest {rest:g} hours")
    return {"level": level, "basis": basis, "overlap_minutes": overlap}


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
    held = [norm(p) for p in policy.get("gate", "held_parents", default=[]) or []]
    reasons = []
    if profile.familiar_with(job.get("employer_name"), job.get("parent")) or norm(job.get("parent") or "") in held:
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
           application_conflict: bool = False) -> dict:
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
        load = workload(schedule, shift_window(text), policy)
        if load["overlap_minutes"]:
            result.fail("overlaps_day_job", f"stated hours overlap your day job ({load['basis']}); Supplemental work must "
                                            "fit around it", schedule_term, profile="night.day_job hours", confidence=0.9)
        coi = coi_review(job, profile, policy, overlap_minutes=load["overlap_minutes"], application_conflict=application_conflict)
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
