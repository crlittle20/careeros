"""Calibration sampling and Horvitz-Thompson estimation (SCORING_SHADOW_SPEC 2026-10-16, section 10). PURE.

The unit is the role group. Each group gets ONE inclusion probability pi, from the first stratum it matches:
  1 discordant              the approaches disagree on recommending it (Tier A/B)          pi 1
  2 recommended_by_all                                                                     pi 1
  3 check_first_or_stretch  any rubric configuration gives Check First or Stretch review  pi 1
  4 near_miss               its best status is a near-miss                                 pi 0.25
  5 archive                 archive under every approach                                   pi 0.10
  6 gate_rejected           rejected by the gate (target job families)                    pi 0.05
(the pi values are the hypothetical ones in config/shadow_scoring.yaml). Each group is drawn once, Bernoulli(pi), with a
recorded seed: the same seed and group always give the same draw, whatever the order. Only labels given on a calibration
list after it was drawn enter population estimates; every other label is descriptive only.

Estimates are ratios of Horvitz-Thompson totals (sum of 1/pi over labelled drawn groups), each reported with its
numerator, denominator and count, and labelled as an estimate. They read the statuses each calibration list FROZE when
it was drawn (careeros/review/scoring_eval.py), never statuses recomputed later.

Check First recovery = wanted groups in Check First / wanted groups v1 did not recommend. The share of ALL wanted
groups that sit in Check First is reported separately, as check_first_share_of_all_wanted.
"""
from __future__ import annotations

import random

STRATA = ("discordant", "recommended_by_all", "check_first_or_stretch", "near_miss", "archive", "gate_rejected")
RECOMMENDED = ("A", "B")
APPROACHES = ("production", "baseline", "v1")


def recommended(status: str | None) -> bool:
    return status in RECOMMENDED


def stratum(statuses: dict) -> str | None:
    """The first stratum a group matches. `statuses` holds the best status per approach ('production', 'baseline',
    'v1' for the primary rubric configuration) and 'v1_all', every rubric configuration's status. Held groups (and any
    group with no judged status) are outside the sampling population: None."""
    flags = [recommended(statuses[a]) for a in APPROACHES]
    every = [statuses["production"], statuses["v1"], *statuses["v1_all"]]
    if "held" in every:
        return None
    if len(set(flags)) > 1:
        return "discordant"
    if all(flags):
        return "recommended_by_all"
    if any(s in ("check_first", "stretch_review") for s in statuses["v1_all"]):
        return "check_first_or_stretch"
    if "near_miss" in every:
        return "near_miss"
    if all(s == "archive" for s in every):
        return "archive"
    if all(s == "rejected" for s in every):
        return "gate_rejected"
    return "near_miss"   # mixed archive / rejected never happens with one shared gate; the safer (larger) pi


def drawn(group_id: str, pi: float, seed: str) -> bool:
    """Bernoulli(pi), reproducible from the seed and the group alone."""
    if pi >= 1:
        return True
    return random.Random(f"{seed}|{group_id}").random() < pi


def ht_ratio(rows: list[dict], numerator, denominator) -> dict:
    """Ratio of Horvitz-Thompson totals over labelled drawn groups: rows carry 'pi' and whatever the predicates read."""
    num = sum(1 / r["pi"] for r in rows if numerator(r))
    den = sum(1 / r["pi"] for r in rows if denominator(r))
    n = sum(1 for r in rows if denominator(r))
    return {"estimate": round(num / den, 3) if den else None, "numerator_ht": round(num, 2), "denominator_ht": round(den, 2),
            "groups_in_denominator": n, "kind": "estimate (Horvitz-Thompson ratio)"}


def upper_95_if_none(n: int) -> float | None:
    """With 0 events in n, the exact one-sided 95% upper bound on the rate (about 3/n)."""
    return round(1 - 0.05 ** (1 / n), 3) if n else None


def estimates(rows: list[dict]) -> dict:
    """Population estimates from labelled calibration groups. Each row: pi, label (apply | stretch | skip) and the
    statuses per approach. Metrics read the STATUS, never actionability."""
    def wanted(r):
        return r["label"] in ("apply", "stretch")

    def skip(r):
        return r["label"] == "skip"

    out: dict = {}
    for approach in APPROACHES:
        def rec(r, a=approach):
            return recommended(r["statuses"][a])
        picked = [r for r in rows if rec(r)]
        skips = sum(1 for r in picked if skip(r))
        block = {
            "skip_share_among_recommendations": {"skips": skips, "labelled_recommendations": len(picked),
                                                 "value": round(skips / len(picked), 3) if picked else None,
                                                 "upper_95_if_none": upper_95_if_none(len(picked)) if not skips else None,
                                                 "kind": "exact while every stratum holding recommended groups has pi = 1"},
            "skip_leakage": ht_ratio(rows, lambda r: skip(r) and rec(r), skip),
            "recall": ht_ratio(rows, lambda r: wanted(r) and rec(r), wanted),
            "false_hard_rejections": ht_ratio(rows, lambda r, a=approach: wanted(r) and r["statuses"][a] == "rejected", wanted),
        }
        if approach == "v1":
            def check_first(r):
                return r["statuses"]["v1"] == "check_first"
            block["recall_with_check_first"] = ht_ratio(rows, lambda r: wanted(r) and (rec(r) or check_first(r)), wanted)
            # recovery: of the wanted groups v1 did NOT recommend, the share Check First brings back to you
            block["check_first_recovery"] = ht_ratio(rows, lambda r: wanted(r) and check_first(r),
                                                     lambda r: wanted(r) and not rec(r))
            # the older all-wanted denominator, kept under its own name
            block["check_first_share_of_all_wanted"] = ht_ratio(rows, lambda r: wanted(r) and check_first(r), wanted)
            block["check_first_yield"] = ht_ratio(rows, lambda r: wanted(r) and check_first(r), check_first)
        out[approach] = block
    return out
