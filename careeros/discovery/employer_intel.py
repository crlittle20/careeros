"""Employer intelligence (2026-10-13): how useful each verified employer has been, from CareerOS's own history.

CAREER FIT answers "is this employer worth scanning often?", never "is this board legitimate?" (that is Board Trust)
and never "is this board working?" (that is Board Health). It is rebuilt from every job CareerOS has seen at the
employer's boards, using CareerOS's own first-seen / last-seen dates rather than the employer's posting dates:

  recency   35  last relevant role seen <=14 days ago 35, <=30 30, <=60 22, <=120 12, <=240 5
  volume    25  relevant roles first seen in the last 180 days, 5 each
  quality   20  relevant roles that reached a review tier (A, B or near-miss), 10 each
  remote    10  share of relevant roles that are remote in the US
  breadth   10  distinct target role groups seen (claims/PBM, BSA, data, operations, automation), 4 each

A relevant role = one of your job families, not outside the US, remote or unstated, and not manager level or above.
Priority: A >= 85, B >= 70, C >= 50, otherwise dormant. Every change is kept in career_fit_history.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter

from careeros.core.clock import parse_utc
from careeros.normalize.posting import norm, relevant_role

GROUPS = {"pbm_claims_operations": "claims_pbm", "healthcare_bsa": "bsa", "data_reporting": "data",
          "process_analyst": "operations", "power_platform_automation": "automation"}
PRIORITY_BANDS = (("A", 85), ("B", 70), ("C", 50))


def _days(now: str, then: str | None) -> float | None:
    return None if not then else (parse_utc(now) - parse_utc(then)).total_seconds() / 86400


def employer_ids(store, entity_id: str) -> list[str]:
    return [r["employer_id"] for r in store.all("SELECT employer_id FROM employers WHERE entity_id = ?", (entity_id,))]


def stats(store, entity_id: str, policy, now: str) -> dict:
    ids = employer_ids(store, entity_id)
    if not ids:
        return {"relevant_total": 0}
    marks = ",".join("?" for _ in ids)
    rows = store.all(f"""SELECT j.job_id, j.title, j.title_norm, j.family, j.country, j.remote_type, j.remote_scope, j.first_seen_at,
                                j.last_seen_at, j.closed_at, j.description_text,
                                (SELECT tier FROM scores s WHERE s.job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1) tier
                         FROM jobs j WHERE j.employer_id IN ({marks})""", tuple(ids))
    relevant = [r for r in rows if relevant_role(r["title_norm"] or norm(r["title"]), r["family"], r["remote_type"], r["country"], policy)]
    groups = Counter(GROUPS.get(r["family"], "other") for r in relevant)
    title = lambda r, word: word in (r["title_norm"] or "")  # noqa: E731
    lives = [_days(r["closed_at"], r["first_seen_at"]) for r in relevant if r["closed_at"]]
    first = min((r["first_seen_at"] for r in rows), default=None)
    span_days = max(30.0, _days(now, first) or 30.0)
    return {
        "jobs_seen": len(rows),
        "relevant_total": len(relevant),
        "relevant_180d": sum(1 for r in relevant if (_days(now, r["first_seen_at"]) or 0) <= 180),
        "qualified": sum(1 for r in relevant if r["tier"] in ("A", "B", "near_miss")),
        "remote_us": sum(1 for r in relevant if r["remote_type"] == "remote"),
        "groups": dict(groups),
        "analyst_roles": sum(1 for r in relevant if title(r, "analyst")),
        "claims_roles": sum(1 for r in relevant if title(r, "claim")),
        "pbm_roles": sum(1 for r in relevant if "pbm" in (r["title_norm"] or "") or "pharmacy benefit" in norm(r["description_text"] or "")[:4000]),
        "configuration_roles": sum(1 for r in relevant if title(r, "configuration")),
        "implementation_roles": sum(1 for r in relevant if title(r, "implementation")),
        "first_relevant_at": min((r["first_seen_at"] for r in relevant), default=None),
        "last_relevant_at": max((r["first_seen_at"] for r in relevant), default=None),
        "avg_posting_days": round(sum(lives) / len(lives), 1) if lives else None,
        "relevant_per_30d": round(len(relevant) * 30 / span_days, 2),
    }


def score(s: dict, now: str) -> tuple[int, str, dict]:
    """(career fit 0-100, priority, components)."""
    if not s.get("relevant_total"):
        return 0, "dormant", {"recency": 0, "volume": 0, "quality": 0, "remote": 0, "breadth": 0}
    age = _days(now, s["last_relevant_at"])
    recency = next((points for limit, points in ((14, 35), (30, 30), (60, 22), (120, 12), (240, 5)) if age is not None and age <= limit), 0)
    parts = {"recency": recency, "volume": min(25, 5 * s["relevant_180d"]), "quality": min(20, 10 * s["qualified"]),
             "remote": round(10 * s["remote_us"] / s["relevant_total"]),
             "breadth": min(10, 4 * len([g for g in s["groups"] if g != "other"]))}
    total = int(sum(parts.values()))
    priority = next((name for name, floor in PRIORITY_BANDS if total >= floor), "dormant")
    return total, priority, parts


def refresh(ctx) -> Counter:
    """Recompute career fit for every employer record with a board; keep a history row when it changes."""
    store, now = ctx.store, ctx.store.clock.now()
    counts = Counter()
    writer = store.writer("discovery")
    for ent in store.all("SELECT * FROM employer_entities WHERE entity_id IN (SELECT entity_id FROM employers WHERE entity_id IS NOT NULL)"):
        s = stats(store, ent["entity_id"], ctx.policy, now)
        total, priority, parts = score(s, now)
        if total == ent["career_fit_score"] and priority == ent["career_priority"]:
            continue
        with store.tx():
            writer.update("employer_entities", {"career_fit_score": total, "career_priority": priority,
                                                "first_relevant_at": s.get("first_relevant_at"), "last_relevant_at": s.get("last_relevant_at"),
                                                "updated_at": now}, "entity_id = ?", (ent["entity_id"],))
            writer.insert("career_fit_history", {"history_id": f"fit:{uuid.uuid4().hex[:16]}", "entity_id": ent["entity_id"], "at": now,
                                                 "score": total, "priority": priority,
                                                 "stats_json": json.dumps({**s, "components": parts}, sort_keys=True)})
            writer.event("career_fit_updated", outcome="informational", entity_type="employer", entity_id=ent["entity_id"],
                         metadata={"score": total, "priority": priority, "previous": ent["career_fit_score"]})
        counts["career_fit_updated"] += 1
    return counts


def priority_of(store, employer) -> str | None:
    if not employer["entity_id"]:
        return None
    row = store.one("SELECT career_priority FROM employer_entities WHERE entity_id = ?", (employer["entity_id"],))
    return row["career_priority"] if row else None
