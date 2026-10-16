"""Experience Match (2026-10-15): how far your years fall short of each years requirement, as a 20-point score.

Shadow mode: while the policy's `experience.enforce` is false (the default), the score, its class and the tier effect it
WOULD have are computed, stored and explained, but no tier changes. The owner-approved hard rule stays where it was:
a hard-required line more than `gate.years_major_gap` years short is rejected by the gate, before any score.

Per years requirement:
  absolute shortfall  0-10  months short, from the policy's bands
  coverage            0-6   counted / required years, capped at 100%
  relevance           0-4   the evidence of the ONE profile item whose years were counted
The class (strong_match .. severe_gap) comes from the total; the coverage descriptor comes from coverage alone.

Facts are never rewritten: the counted value is the screening value your profile sets (`screening_years`, else
`years`), the factual `years` is stored beside it, and years from different items are never added together.
Months are exact decimals, so 4 of 5 years is exactly 80% and 12 months short is exactly 12.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from .profile import Profile, alternatives

YEARS_CATEGORIES = ("skill", "years_experience", "experience", "domain")
REQUIRED_TYPES = ("hard_required", "required_with_equivalency", "ambiguous")
EFFECT_RANK = {"none": 0, "cap_b": 1, "stretch": 2, "reject_by_existing_gate": 3}
_OR_LINE = re.compile(r"\bor\b|\band/or\b|\b(?:one|any|either) of\b|[a-z]/[a-z]", re.I)
_CENT = Decimal("0.01")

DEFAULTS = {
    "enforce": False,
    "shortfall_points": [[0, 10], [6, 9], [12, 8], [24, 6], [36, 4], [48, 2]],
    "coverage_points": [[100, 6], [80, 5], [60, 4], [40, 3], [20, 2]],
    "coverage_classes": [[100, "full_coverage"], [80, "high_coverage"], [60, "substantial_coverage"],
                         [40, "moderate_coverage"], [20, "low_coverage"]],
    "lowest_coverage_class": "very_low_coverage",
    "relevance_points": {"direct": 4, "strongly_transferable": 3, "generic": 2, "adjacent": 1, "missing": 1},
    "classes": [[18, "strong_match"], [15, "small_gap"], [11, "medium_gap"], [7, "large_gap"]],
    "lowest_class": "severe_gap",
    "effects": {"hard_required": {"medium_gap": "cap_b", "large_gap": "stretch", "severe_gap": "stretch"},
                "required_with_equivalency": {"large_gap": "cap_b", "severe_gap": "stretch"},
                "ambiguous": {"large_gap": "cap_b", "severe_gap": "stretch"}},
    "high_potential": {"gap_classes": ["medium_gap", "large_gap"], "min_skill_coverage": 0.85, "domain_levels": ["direct"]},
    "calibration": {"min_labels": 50, "min_years_labels": 20},
}


def settings(policy) -> dict:
    """The policy's `experience` section over the defaults above (one source for every threshold)."""
    configured = policy.get("experience", default={}) or {}
    return {**DEFAULTS, **configured}


def months(years: float) -> Decimal:
    return (Decimal(str(years)) * 12).quantize(_CENT, ROUND_HALF_UP)


def _band(value: Decimal, bands: list, at_most: bool, otherwise):
    for limit, result in bands:
        if (value <= Decimal(str(limit))) if at_most else (value >= Decimal(str(limit))):
            return result
    return otherwise


def score_years(required: float, counted: float, evidence_kind: str, cfg: dict) -> dict:
    """The 20-point score for one requirement. `evidence_kind` is direct, strongly_transferable, adjacent, generic or
    missing."""
    need, have = months(required), months(counted)
    short = max(need - have, Decimal(0))
    coverage = Decimal(100) if need <= 0 else min((have / need * 100).quantize(_CENT, ROUND_HALF_UP), Decimal(100))
    absolute = _band(short, cfg["shortfall_points"], True, 0)
    covered = _band(coverage, cfg["coverage_points"], False, 0)
    relevance = cfg["relevance_points"].get(evidence_kind, cfg["relevance_points"]["missing"])
    total = absolute + covered + relevance
    return {"shortfall_months": float(short), "coverage_pct": float(coverage),
            "coverage_class": _band(coverage, cfg["coverage_classes"], False, cfg["lowest_coverage_class"]),
            "absolute_points": absolute, "coverage_points": covered, "relevance_points": relevance, "score": total,
            "gap_class": _band(Decimal(total), cfg["classes"], False, cfg["lowest_class"])}


def evidence_kind(match: dict) -> str:
    """Relevance reuses the evidence model: an item's recorded evidence; a domain you hold counts as direct (as in skill
    coverage); "years of relevant experience" answered by total years is generic; nothing recorded is 'missing'."""
    if match["kind"] == "generic":
        return "generic"
    if match["kind"] == "domain":
        return "direct"
    return match.get("evidence") or "missing"


def best_match(profile: Profile, name: str) -> dict | None:
    """Your strongest item across the alternatives a requirement's area lists (the gate's `best_years`, with the item)."""
    found = [m for m in (profile.years_match(a) for a in alternatives(name)) if m and m["years"] is not None]
    return max(found, key=lambda m: m["years"]) if found else None


def _range_upper(source_text: str, years: float) -> float | None:
    from careeros.extract.rules import year_spans
    return next((upper for _, _, low, upper in year_spans(source_text or "") if low == years and upper), None)


def score_requirement(req, profile: Profile, cfg: dict) -> dict:
    """One requirement record: what was asked, the item counted, and the score (None when your years are unknown)."""
    match = best_match(profile, req.name)
    item = {"area": req.name, "category": req.category, "strength": req.requirement_type, "required_years": req.years,
            "required_upper": _range_upper(req.source_text, req.years)}
    if match is None:
        return {**item, "matched_item": None, "score": None, "gap_class": "unknown"}
    kind = evidence_kind(match)
    return {**item, "matched_item": f"{match['kind']}: {match['item']}", "evidence": kind,
            "factual_years": match["factual_years"], "screening_years": match["years"], "counted_years": match["years"],
            **score_years(req.years, match["years"], kind, cfg)}


def gate_line(records: list, profile: Profile, policy) -> dict | None:
    """The Experience Match kept with a years rejection: the gate judges a line on your best alternative, so this is
    the best-scoring record of the line (None when none can be scored)."""
    cfg = settings(policy)
    known = [m for m in (score_requirement(r, profile, cfg) for r in records) if m["score"] is not None]
    return max(known, key=lambda i: (i["score"], i["counted_years"])) if known else None


def _strictest(items: list) -> dict:
    return min(items, key=lambda i: (i["score"], -i["shortfall_months"]))


def _line(source_text: str, members: list) -> dict:
    """One requirement line (one source line and one years figure). An OR-list is judged on your best alternative, an
    AND-list on the strictest item; items with unknown years are listed but never scored."""
    known = [m for m in members if m["score"] is not None]
    structure = "or" if _OR_LINE.search(source_text or "") else "and" if len(members) > 1 else "single"
    if not known:
        chosen = members[0]
    elif structure == "or":
        chosen = max(known, key=lambda i: (i["score"], i["counted_years"]))
    else:
        chosen = _strictest(known)
    return {"source_text": (source_text or "")[:200], "structure": structure, **chosen,
            "unknown_items": [m["area"] for m in members if m["score"] is None and m is not chosen]}


def _effect(line: dict, cfg: dict, rejected: set) -> str:
    if line["score"] is None:
        return "none"
    if (line["source_text"], line["required_years"]) in rejected:
        return "reject_by_existing_gate"
    return (cfg["effects"].get(line["strength"]) or {}).get(line["gap_class"], "none")


def hypothetical_tier(tier: str | None, effect: str) -> str | None:
    """The tier this effect WOULD give. Experience never raises a tier."""
    if effect == "reject_by_existing_gate":
        return "rejected"
    if effect == "stretch" and tier in ("A", "B"):
        return "near_miss"
    if effect == "cap_b" and tier == "A":
        return "B"
    return tier


def evaluate(requirements: list, profile: Profile, policy, gate=None, tier: str | None = None,
             skill_coverage: float | None = None, domain_level: str | None = None) -> dict:
    """The stored Experience Match block for one job (`scores.components.experience`)."""
    cfg = settings(policy)
    groups: dict = {}
    for r in requirements:
        if r.years is not None and r.category in YEARS_CATEGORIES and r.requirement_type != "responsibility":
            groups.setdefault((r.source_text, r.years, r.requirement_type), []).append(score_requirement(r, profile, cfg))
    rejected = {((f.get("source_text") or "")[:200], f.get("required_years")) for f in (gate.hard_failures if gate else [])
                if f.get("code") == "years_far_short"}
    lines = []
    for (source, _years, _type), members in groups.items():
        line = _line(source, members)
        line["effect"] = _effect(line, cfg, rejected)
        lines.append(line)

    required = [i for i, line in enumerate(lines) if line["strength"] in REQUIRED_TYPES and line["score"] is not None]
    preferred = [i for i, line in enumerate(lines) if line["strength"] not in REQUIRED_TYPES and line["score"] is not None]
    pool = required or preferred
    block = {"enforced": bool(cfg["enforce"]), "lines": lines, "conflicts": conflicts(requirements), "decisive_line": None,
             "score": None, "gap_class": "unknown", "coverage_class": None, "effect": "none", "tier": tier,
             "hypothetical_tier": tier, "high_potential": False}
    if pool:
        decisive = min(pool, key=lambda i: (-EFFECT_RANK[lines[i]["effect"]], lines[i]["score"], -lines[i]["shortfall_months"], i))
        line = lines[decisive]
        effect = line["effect"] if decisive in required else "none"
        block.update({"decisive_line": decisive, "score": line["score"], "gap_class": line["gap_class"],
                      "coverage_class": line["coverage_class"], "effect": effect,
                      "hypothetical_tier": hypothetical_tier(tier, effect)})
        hp = cfg["high_potential"]
        block["high_potential"] = bool(line["gap_class"] in hp["gap_classes"] and skill_coverage is not None
                                       and skill_coverage >= hp["min_skill_coverage"] and domain_level in hp["domain_levels"])
    block["basis"] = {"skill_coverage": skill_coverage, "domain_level": domain_level}
    return block


def conflicts(requirements: list) -> list:
    """The same area asked for with different years on separate hard-required lines: the higher figure counts."""
    seen: dict = {}
    for r in requirements:
        if r.years is not None and r.category in YEARS_CATEGORIES and r.requirement_type == "hard_required":
            seen.setdefault((r.category, r.name), {})[r.years] = r.source_text
    return [{"area": name, "years": sorted(found), "counted": max(found), "source_text": (found[max(found)] or "")[:200]}
            for (_category, name), found in sorted(seen.items()) if len(found) > 1]


def rejection_block(failure: dict) -> dict | None:
    """A years rejection's stored Experience Match as a block `explain` can read."""
    line = failure.get("experience")
    if not line:
        return None
    return {"lines": [line], "decisive_line": 0, "effect": "reject_by_existing_gate", "enforced": False,
            "tier": None, "hypothetical_tier": "rejected", "high_potential": False, "conflicts": []}


def _num(value) -> str:
    return f"{value:g}"


def _duration(months_short: float) -> str:
    return f"{_num(round(months_short / 12, 2))} years ({_num(months_short)} months)"


STRENGTH_WORDS = {"hard_required": "hard required", "required_with_equivalency": "required, or equivalent",
                  "ambiguous": "required (unclear wording)", "preferred": "preferred"}
EFFECT_WORDS = {"none": "none", "cap_b": "cap at Tier B", "stretch": "stretch (Tier A/B lowered to near-miss)",
                "reject_by_existing_gate": "rejected by the existing gate rule"}


def summary(block: dict) -> str:
    """One line for a review card, from the stored fields."""
    line = block["lines"][block["decisive_line"]]
    effect = EFFECT_WORDS.get(block["effect"], block["effect"])
    when = "effect" if block.get("enforced") or block["effect"] == "reject_by_existing_gate" else "shadow mode, if enforced"
    return (f"Experience {line['score']}/20 · {line['gap_class'].replace('_', ' ')} · {line['coverage_class'].replace('_', ' ')}"
            f" · {_num(line['counted_years'])} of {_num(line['required_years'])} years of {line['area']} · {when}: {effect}"
            + (" · high potential" if block.get("high_potential") else ""))


def explain(block: dict) -> list[str]:
    """Deterministic sentences from the stored fields only (identical block, identical text)."""
    if not block or block.get("decisive_line") is None:
        return ["Experience: no years requirement could be scored (none stated, or your years for it are not set)."]
    line = block["lines"][block["decisive_line"]]
    asked = (f"{_num(line['required_years'])}-{_num(line['required_upper'])}" if line.get("required_upper")
             else f"{_num(line['required_years'])}+")
    out = [f"Experience {line['score']}/20: {line['gap_class'].replace('_', ' ')}, {line['coverage_class'].replace('_', ' ')}.",
           f"Required: {asked} years of {line['area']}, {STRENGTH_WORDS.get(line['strength'], line['strength'])}.",
           *([f"Posting line: \"{line['source_text']}\""] if line.get("source_text") else []),
           f"Counted: {_num(line['counted_years'])} years ({line['matched_item']}), evidence {line['evidence']}."]
    if line.get("factual_years") is not None and line["factual_years"] != line["counted_years"]:
        out.append(f"Factual experience: {_num(line['factual_years'])} years; screening value used: {_num(line['counted_years'])} years.")
    out.append(f"Shortfall: {_duration(line['shortfall_months'])}. Coverage: {_num(line['coverage_pct'])}%.")
    out.append(f"Points: shortfall {line['absolute_points']}/10, coverage {line['coverage_points']}/6, "
               f"relevance {line['relevance_points']}/4.")
    if line.get("structure") in ("or", "and"):
        out.append(f"Judged on {'the best alternative' if line['structure'] == 'or' else 'the strictest item'} of the line.")
    effect = EFFECT_WORDS.get(block["effect"], block["effect"])
    if block["effect"] == "reject_by_existing_gate":
        out.append("Effect: rejected by the existing years rule (hard required, more than the allowed gap); Experience "
                   "Match does not change that rule.")
    elif block.get("enforced"):
        out.append(f"Effect: {effect}.")
    else:
        out.append(f"Current effect: none (shadow mode). Future effect if enforced: {effect}"
                   + (f", tier {block['tier']} would become {block['hypothetical_tier']}."
                      if block.get("hypothetical_tier") != block.get("tier") else "."))
    if block.get("high_potential"):
        basis = block.get("basis") or {}
        out.append(f"High potential: yes (weighted skill coverage {basis.get('skill_coverage', 0):.0%}, domain "
                   f"{basis.get('domain_level')}).")
    for conflict in block.get("conflicts") or []:
        out.append(f"Conflicting figures for {conflict['area']}: {', '.join(_num(y) for y in conflict['years'])} years; "
                   f"{_num(conflict['counted'])} counts.")
    return out
