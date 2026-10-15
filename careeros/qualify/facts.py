"""Eligibility evidence, kept apart from fit (2026-10-12, Part 5).

For each fact that decides whether you could take a job (state, remote, pay, schedule, employment type, training
attendance, travel, licences / certifications / clearance, mandatory qualifications) the status is one of:
  compatible    the posting says something that fits you;
  incompatible  the posting says something that rules you out;
  unknown       the posting is silent or unclear: what to verify is spelled out;
  conflicting   sources disagree (see careeros.openings.conflicts).
Nothing is inferred. Each fact carries its supporting text and where it came from.

Pay is judged on what you would actually be offered: a range whose maximum clears your minimum is NOT compatible on
that alone. Geographic tiers, ranges that straddle your minimum, and ranges that include bonus / commission / equity
stay "unknown" until the base pay for your location is confirmed. Units (hour, day, week, month, year) are annualised.
"""
from __future__ import annotations

import re

from careeros.normalize.posting import _RANGE, US_STATES, _amount

from .gate import GateResult, annualize, comp_trusted

DECISIVE = ("pay", "location_state", "remote", "employment_type", "licenses", "qualifications", "training", "schedule", "travel")
_GEO = re.compile(r"(geographic|geo[- ]?(?:zone|tier|differential|market)|location[- ]based|zone [a-z0-9]\b|tier [1-4]\b|"
                  r"varies by (?:location|market|region)|depend(?:s|ing)? on (?:your |the )?(?:location|market|region)|"
                  r"based on (?:your |the )?(?:location|market)|cost[- ]of[- ]labor|pay zones?)", re.I)
_VARIABLE = re.compile(r"\b(ote|on[- ]target earnings|commission|bonus(?:es)?|equity|stock|rsus?|incentive pay)\b", re.I)
_BASE = re.compile(r"\bbase (?:salary|pay|compensation|rate)\b", re.I)
_TRAINING_ONSITE = re.compile(r"(on[- ]?site|in[- ]person|in office|at (?:our|the) office|must attend|travel to)[^.\n]{0,60}"
                              r"\btraining\b|\btraining\b[^.\n]{0,60}(on[- ]?site|in[- ]person|in office|at our [a-z]+ office)", re.I)
_TRAINING_REMOTE = re.compile(r"(remote|virtual|online)[^.\n]{0,30}\btraining\b|\btraining\b[^.\n]{0,30}(remote|virtual|online)", re.I)


def _fact(facet: str, status: str, detail: str, evidence: str | None = None, source: str = "posting") -> dict:
    return {"facet": facet, "status": status, "detail": detail, "evidence": (evidence or "")[:200] or None, "source": source}


def _money(amount: float) -> str:
    return f"${amount:,.0f}"


def _home(policy) -> str:
    """The home state by name, for messages ('CO' -> 'Colorado'). The state itself is a private vault setting."""
    code = policy.get("location", "home_state")
    return next((name.title() for name, abbr in US_STATES.items() if abbr == code), code or "your state")


def pay_fact(job: dict, policy) -> dict:
    comp = policy.get("compensation", default={}) or {}
    floor = comp.get("floor_annual") or (comp.get("floor_hourly") * 2080 if comp.get("floor_hourly") else None)
    text = job.get("description_text") or ""
    if job.get("comp_min") is None and job.get("comp_max") is None:
        return _fact("pay", "unknown", f"pay is not posted: ask for the base pay range for {_home(policy)} / remote")
    period = job.get("comp_period") or "year"
    low = annualize(job.get("comp_min") or job.get("comp_max"), period)
    high = annualize(job.get("comp_max") or job.get("comp_min"), period)
    shown = f"{_money(low)}–{_money(high)}/year" + (f" (posted per {period})" if period != "year" else "")
    if not comp_trusted(job):
        return _fact("pay", "unknown", f"{shown} is an aggregator estimate: confirm on the employer's own posting", source="aggregator")
    if not floor:
        return _fact("pay", "unknown", f"{shown}; your minimum is not set")
    if high < floor:
        return _fact("pay", "incompatible", f"{shown}: the top of the range is under your {_money(floor)} minimum")
    near = text[max(0, text.lower().find("$") - 300): text.lower().find("$") + 600] if "$" in text else text[:1500]
    geo, variable = _GEO.search(near), _VARIABLE.search(near)
    lows = [annualize(_amount(m.group(1), m.group(2)), period) for m in _RANGE.finditer(text)]
    lowest = min([low] + [x for x in lows if x])
    if geo and lowest < floor:
        return _fact("pay", "unknown", f"pay varies by location ('{geo.group(0)}'); the lowest tier starts at {_money(lowest)}: "
                                       f"confirm the {_home(policy)} / remote tier reaches your {_money(floor)} minimum", geo.group(0))
    if variable and not _BASE.search(near):
        return _fact("pay", "unknown", f"{shown} may include {variable.group(0)}: confirm the BASE pay reaches {_money(floor)}",
                     variable.group(0))
    if low < floor:
        return _fact("pay", "unknown", f"{shown} straddles your {_money(floor)} minimum: confirm where your offer would land")
    return _fact("pay", "compatible", f"{shown}: the whole range meets your {_money(floor)} minimum"
                 + (" (location tiers mentioned, all at or above it)" if geo else ""))


def eligibility_facts(job: dict, requirements: list, gate: GateResult, policy) -> list[dict]:
    codes = {f["code"]: f for f in gate.hard_failures}
    flags = {f["code"]: f for f in gate.flags}
    facts = []

    home = policy.get("location", "home_state")
    states, excluded = job.get("remote_states") or [], job.get("remote_excluded_states") or []
    if "state_excluded" in codes or "state_not_eligible" in codes:
        f = codes.get("state_excluded") or codes.get("state_not_eligible")
        facts.append(_fact("location_state", "incompatible", f["detail"], f.get("source_text")))
    elif states and home in states:
        facts.append(_fact("location_state", "compatible", f"{home} is in the posting's state list", ", ".join(states)))
    elif job.get("country") == "US" and job.get("remote_type") == "remote" and not states and not excluded:
        facts.append(_fact("location_state", "compatible", "US-remote with no state limits stated", job.get("location_raw")))
    else:
        facts.append(_fact("location_state", "unknown", "the posting does not say which states it hires in", job.get("location_raw")))

    remote = job.get("remote_type")
    facts.append(_fact("remote", "compatible" if remote == "remote" else "incompatible" if remote in ("hybrid", "onsite") else "unknown",
                       {"remote": "remote role", "hybrid": "hybrid role", "onsite": "on-site role"}.get(remote, "remote status not stated"),
                       job.get("location_raw")))

    facts.append(pay_fact(job, policy))

    allowed = (policy.get("gate", default={}) or {}).get("employment_types_allowed", [])
    employment = job.get("employment_type") or "unknown"
    facts.append(_fact("employment_type", "unknown" if employment == "unknown" else "compatible" if employment in allowed else "incompatible",
                       f"employment type: {employment.replace('_', ' ')}"))

    schedule = [r for r in requirements if r.category == "schedule" and r.requirement_type != "preferred"]
    if schedule:
        facts.append(_fact("schedule", "unknown", f"the posting asks for {schedule[0].name}: confirm it works for you", schedule[0].source_text))
    travel = [r for r in requirements if r.category == "travel"]
    if travel:
        facts.append(_fact("travel", "unknown", f"travel is mentioned: {travel[0].name}", travel[0].source_text))
    text = job.get("description_text") or ""
    onsite_training, remote_training = _TRAINING_ONSITE.search(text), _TRAINING_REMOTE.search(text)
    if onsite_training and remote == "remote":
        facts.append(_fact("training", "conflicting", "a remote role that mentions on-site / in-person training: confirm "
                                                      "whether you would have to travel for it", onsite_training.group(0)))
    elif onsite_training:
        facts.append(_fact("training", "unknown", "in-person training is mentioned", onsite_training.group(0)))
    elif remote_training:
        facts.append(_fact("training", "compatible", "training is remote / virtual", remote_training.group(0)))

    licence = [codes[c] for c in ("license_required", "certification_required", "clearance_required") if c in codes]
    checks = [flags[c] for c in ("license_check", "certification_check", "clearance_check", "certification_gap") if c in flags]
    if licence:
        facts.append(_fact("licenses", "incompatible", licence[0]["detail"], licence[0].get("source_text")))
    elif checks:
        facts.append(_fact("licenses", "unknown", f"{checks[0]['detail']}: check whether it is required and whether you hold it",
                           checks[0].get("source_text")))

    hard = [codes[c] for c in ("degree_required", "years_far_short", "work_authorization", "sponsorship_unavailable") if c in codes]
    soft = [flags[c] for c in ("education_unknown", "years_unknown", "work_authorization_unknown", "citizenship_check") if c in flags]
    if hard:
        facts.append(_fact("qualifications", "incompatible", hard[0]["detail"], hard[0].get("source_text")))
    elif soft:
        facts.append(_fact("qualifications", "unknown", soft[0]["detail"], soft[0].get("source_text")))
    elif "degree_stretch" in flags:
        facts.append(_fact("qualifications", "unknown", flags["degree_stretch"]["detail"], flags["degree_stretch"].get("source_text")))
    elif any(r.requirement_type in ("hard_required", "required_with_equivalency") for r in requirements):
        facts.append(_fact("qualifications", "compatible", "the mandatory qualifications that could be read are met"))
    return facts


def decisive_unknowns(facts: list[dict]) -> list[dict]:
    return [f for f in facts if f["facet"] in DECISIVE and f["status"] in ("unknown", "conflicting")]
