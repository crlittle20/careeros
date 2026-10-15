"""Auditing the title screen and lead matches (2026-10-12, Parts 1 and 3).

Title audit: a stratified sample of title-screen rejections (spread across employers and rejection reasons), each with
your verdict: `include` (it should have passed: a false rejection) or `correct`. The estimate is for finding false
negatives, not for assuming every rejection is wrong. Match audit: confirm or refute an automatic lead -> posting match,
which feeds the pilot's match-accuracy measure.
"""
from __future__ import annotations

import random
from collections import defaultdict

from careeros.db.store import Store

REASONS = ("no_target_family", "title_excluded", "generic_title_no_context")


def sample(store: Store, n: int = 40, seed: int | None = None) -> list[dict]:
    rows = store.all("""SELECT j.job_id, j.title, j.employer_name, t.reason_code FROM jobs j JOIN transitions t
                        ON t.workflow_id = j.workflow_id AND t.sequence = j.state_sequence
                        WHERE j.current_state = 'rejected' AND t.reason_code IN ('no_target_family','title_excluded','generic_title_no_context')""")
    strata = defaultdict(list)
    for r in rows:
        strata[(r["employer_name"], r["reason_code"])].append(r)
    rng = random.Random(seed if seed is not None else store.clock.now()[:10])
    keys = sorted(strata)
    rng.shuffle(keys)
    picked, round_ = [], 0
    while len(picked) < n and any(len(strata[k]) > round_ for k in keys):  # round-robin across employer x reason
        for k in keys:
            if len(strata[k]) > round_ and len(picked) < n:
                picked.append(strata[k][round_] if round_ else rng.choice(strata[k]))
        round_ += 1
    now = store.clock.now()
    writer = store.writer("discovery")
    out = []
    for r in picked:
        if not store.one("SELECT 1 FROM title_audit WHERE job_id = ? AND verdict IS NULL", (r["job_id"],)):
            writer.insert("title_audit", {"job_id": r["job_id"], "sampled_at": now, "reason": r["reason_code"], "verdict": None,
                                          "verdict_at": None})
        out.append({"job_id": r["job_id"], "title": r["title"], "employer": r["employer_name"], "reason": r["reason_code"]})
    return out


def mark(store: Store, job_id: str, verdict: str) -> None:
    value = {"include": "should_include", "correct": "correct_reject"}.get(verdict)
    if not value:
        raise ValueError("verdict must be include or correct")
    row = store.one("SELECT sampled_at FROM title_audit WHERE job_id = ? ORDER BY sampled_at DESC LIMIT 1", (job_id,))
    if not row:
        raise ValueError("not sampled: run `careeros audit titles` first")
    store.writer("review").update("title_audit", {"verdict": value, "verdict_at": store.clock.now()},
                                  "job_id = ? AND sampled_at = ?", (job_id, row["sampled_at"]))


def summary(store: Store) -> str:
    done = store.all("SELECT verdict, reason FROM title_audit WHERE verdict IS NOT NULL")
    if not done:
        return "No verdicts yet."
    include = sum(1 for r in done if r["verdict"] == "should_include")
    by = defaultdict(lambda: [0, 0])
    for r in done:
        by[r["reason"]][0] += r["verdict"] == "should_include"
        by[r["reason"]][1] += 1
    detail = "; ".join(f"{k}: {v[0]}/{v[1]}" for k, v in sorted(by.items()))
    return f"Estimated false rejections: {include}/{len(done)} audited ({detail}). Small samples are observations, not trends."


def audit_match(store: Store, match_id: str, verdict: str) -> None:
    if verdict not in ("correct", "wrong"):
        raise ValueError("verdict must be correct or wrong")
    if not store.one("SELECT 1 FROM lead_matches WHERE match_id = ?", (match_id,)):
        raise ValueError("unknown match id (see careeros metrics / the report's Sources section)")
    store.writer("review").update("lead_matches", {"audit": verdict, "audited_at": store.clock.now()}, "match_id = ?", (match_id,))
