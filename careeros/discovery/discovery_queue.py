"""Discovery queue: historical backfill (Part 2) and curated employer suggestions (Part 9), 2026-10-12.

One queue row per employer (deduplicated through verified aliases), so a company named in ten alerts is discovered
once. Priority: repeated relevant leads, evidence-bearing hints (a board link or official domain), curated
suggestions, recency. Processing is idempotent and resumable: every decision is recorded as it happens, an employer
is not retried before its outcome's retry window (slug absent 60 days, empty board 14, identity unresolved 30, no
qualified roles 30, access restricted 90, request failed 1 or the server's Retry-After), and an interrupted run simply
continues next time. Curated suggestions get at most `curated_share` of each run's request budget, so they widen
discovery without crowding out lead-derived employers. `dry_run` previews the plan without a single request.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from careeros.core.clock import parse_utc
from careeros.core.policy import load_yaml

from . import board_discovery as bd
from .http import LimitedBudget, RequestBudget
from .seen_index import norm_company


def _canonical(store, employer_norm: str) -> str:
    row = store.one("SELECT employer_norm FROM employer_aliases WHERE alias_norm = ? AND relation = 'alias'", (employer_norm,))
    return row["employer_norm"] if row else employer_norm


def _active_employers(store) -> set[str]:
    return {norm_company(r["name"]) for r in store.all("SELECT name FROM employers WHERE status IN ('candidate','verified')")}


def enqueue(store, candidate: bd.Candidate, priority: float) -> str:
    """Add or merge a candidate (keyed by employer). Returns 'added' or 'merged'."""
    key = _canonical(store, candidate.employer_norm)
    now = store.clock.now()
    existing = store.one("SELECT * FROM discovery_queue WHERE employer_norm = ?", (key,))
    # employer-level evidence only: each lead's job facts (title, link, requisition, location) stay with the lead and are
    # read from it when its board is matched (gap 2, 2026-11-02: the first lead's facts were used for every lead)
    hint = {"domain": candidate.official_domain, "board_url": candidate.board_url, "relationship": candidate.relationship}
    writer = store.writer("discovery")
    if existing:
        leads = sorted(set(json.loads(existing["lead_ids_json"])) | set(candidate.lead_ids))
        evidence = json.loads(existing["evidence_json"])
        for k, v in hint.items():
            if v and not evidence.get(k):
                evidence[k] = v
        values = {"lead_ids_json": json.dumps(leads), "evidence_json": json.dumps(evidence),
                  "priority": max(existing["priority"], priority), "updated_at": now,
                  "official_domain": existing["official_domain"] or candidate.official_domain,
                  "board_url": existing["board_url"] or candidate.board_url}
        if existing["state"] in ("done", "skipped") and (candidate.board_url or candidate.official_domain) and not existing["board_url"]:
            values.update(state="pending", next_attempt_at=None)  # credible new evidence: reconsider
        writer.update("discovery_queue", values, "employer_norm = ?", (key,))
        return "merged"
    writer.insert("discovery_queue", {
        "employer_norm": key, "employer_name": candidate.employer_name[:100], "official_domain": candidate.official_domain,
        "board_url": candidate.board_url, "origin": candidate.origin, "priority": priority,
        "evidence_json": json.dumps({k: v for k, v in hint.items() if v}), "lead_ids_json": json.dumps(sorted(candidate.lead_ids)),
        "state": "pending", "last_outcome": None, "next_attempt_at": None, "added_at": now, "updated_at": now})
    return "added"


def build_backfill(ctx, days: int | None = None) -> dict:
    """Queue employers from leads seen in the last `days` (alerts, recommendations, manual leads), most promising first."""
    store, policy = ctx.store, ctx.policy
    days = int(days or bd.cfg(policy)["lookback_days"])
    since = (parse_utc(store.clock.now()) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    groups: dict = {}
    from careeros.discovery.alerts import agency_setting_problem, local_only_urls
    if agency_setting_problem(store):   # fail closed (D125): no lead-based discovery while an agency entry is missing
        return {"agency_setting_missing": 1}
    local = local_only_urls(store)
    for lead in store.all("SELECT * FROM leads WHERE first_seen_at >= ? ORDER BY first_seen_at", (since,)):
        candidate = bd.candidate_from_lead(lead, policy, local)
        if candidate is None:
            continue
        key = _canonical(store, candidate.employer_norm)
        group = groups.setdefault(key, {"candidate": candidate, "relevant": 0, "total": 0, "hints": 0, "recent": 0})
        group["total"] += 1
        group["relevant"] += int(bool(lead["family"]))
        group["hints"] += int(bool(candidate.board_url or candidate.official_domain))
        age = (parse_utc(store.clock.now()) - parse_utc(lead["first_seen_at"])) / timedelta(days=1)
        group["recent"] += int(age <= 14)
        if lead["lead_id"] not in group["candidate"].lead_ids:
            group["candidate"].lead_ids.append(lead["lead_id"])
        for attr in ("board_url", "official_domain"):
            if getattr(candidate, attr) and not getattr(group["candidate"], attr):
                setattr(group["candidate"], attr, getattr(candidate, attr))
    active, stats = _active_employers(store), {"added": 0, "merged": 0, "already_scanned": 0}
    for key, g in groups.items():
        if key in active:
            stats["already_scanned"] += 1
            continue
        priority = min(10, 2 * g["relevant"]) + 0.5 * g["total"] + (5 if g["hints"] else 0) + (1 if g["recent"] else 0)
        g["candidate"].origin = "backfill" if g["candidate"].origin == "alert" else g["candidate"].origin
        stats[enqueue(store, g["candidate"], priority)] += 1
    return stats


def load_curated(ctx, path: Path) -> dict:
    """Curated employers found independently of LinkedIn / Indeed (a vault file you or ChatGPT maintain):
    - {name: Foo Health, domain: foohealth.com, board: https://jobs.lever.co/foohealth, note: why}"""
    stats = {"added": 0, "merged": 0, "invalid": 0}
    if not path.exists():
        return stats
    for item in (load_yaml(path) or {}).get("employers") or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            stats["invalid"] += 1
            continue
        candidate = bd.Candidate(employer_name=str(item["name"]).strip()[:100], origin="curated",
                                 official_domain=(str(item.get("domain") or "").strip().lower() or None),
                                 board_url=(str(item.get("board") or "").strip() or None), origin_ref=str(item.get("note") or "")[:100] or None)
        stats[enqueue(ctx.store, candidate, 3 + (5 if candidate.board_url or candidate.official_domain else 0))] += 1
    return stats


def _still_eligible(store, row, local: frozenset[str]) -> bool:
    """A queued row made from leads is probed only while one of its leads is still an ordinary lead: a lead that has
    since turned out to be an agency card's personal link (a marked twin, CR-AD-02) no longer counts. Curated rows and
    rows without leads are unchanged."""
    lead_ids = json.loads(row["lead_ids_json"] or "[]")
    if row["origin"] == "curated" or not lead_ids:
        return True
    from careeros.discovery.alerts import is_agency_lead
    for lead_id in lead_ids:
        lead = store.one("SELECT lead_id, url FROM leads WHERE lead_id = ?", (lead_id,))
        if lead is not None and not is_agency_lead(lead, local):
            return True
    return False


def _candidate_from_row(row) -> bd.Candidate:
    """The employer a queue row stands for, with its leads. No job facts: a row written before 2026-11-02 may still hold
    its first lead's title, link and requisition in evidence_json, and they are never read (discover reads each lead's own)."""
    evidence = json.loads(row["evidence_json"] or "{}")
    return bd.Candidate(employer_name=row["employer_name"], origin=row["origin"], official_domain=row["official_domain"],
                        board_url=row["board_url"], lead_ids=json.loads(row["lead_ids_json"] or "[]"),
                        relationship=evidence.get("relationship") or "employer")


def plan(ctx, limit: int = 50) -> list[dict]:
    """The dry run: what would be probed, in order, without sending any request."""
    now = ctx.store.clock.now()
    out: list[dict] = []
    from careeros.discovery.alerts import agency_setting_problem, local_only_urls
    if agency_setting_problem(ctx.store):   # fail closed (D125)
        return out
    local = local_only_urls(ctx.store)
    for row in _due(ctx.store, now)[:limit]:
        if not _still_eligible(ctx.store, row, local):
            continue
        c = _candidate_from_row(row)
        targets = ([bd.parse_board_url(c.board_url)] if bd.parse_board_url(c.board_url or "") else
                   [] if bd.manual_system(c.board_url or "") else
                   [(ats, slug) for slug in bd.slug_variants(c.employer_name, int(bd.cfg(ctx.policy)["max_slug_variants"])) for ats in bd.BOARD_HOSTS])
        out.append({"employer": c.employer_name, "origin": c.origin, "priority": row["priority"], "leads": len(c.lead_ids),
                    "relationship": c.relationship, "manual_system": bd.manual_system(c.board_url or ""),
                    "targets": [f"{a}:{s}" for a, s in targets], "max_requests": len(targets) + sum(1 for a, _ in targets if a == "greenhouse")})
    return out


def _due(store, now: str) -> list:
    return store.all("""SELECT * FROM discovery_queue WHERE state IN ('pending','deferred','done')
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY priority DESC, added_at""", (now,))


def run(ctx, *, budget: RequestBudget | None = None, client=None, limit: int = 100) -> dict:
    """Process due queue rows until the request budget is spent. Returns counts by outcome plus requests used."""
    store, policy = ctx.store, ctx.policy
    c = bd.cfg(policy)
    budget = budget or RequestBudget(int(c["daily_request_budget"]),
                                     {p: int(c["per_provider_budget"]) for p in ("greenhouse", "lever", "ashby", "workable")})
    own_client = client is None
    client = client or bd.discovery_client(policy, budget)
    curated_cap = int(budget.total * float(c["curated_share"]))
    now = store.clock.now()
    stats: dict = {"requests": 0}
    curated_used = 0
    from careeros.discovery.alerts import agency_setting_problem, local_only_urls
    if agency_setting_problem(store):   # fail closed (D125): nothing is probed while an agency entry is missing
        if own_client:
            client.close()
        return {**stats, "agency_setting_missing": 1}
    local = local_only_urls(store)
    try:
        for row in _due(store, now)[:limit]:
            if agency_setting_problem(store):   # stopped mid-run: nothing more is probed (CR-AD-V2-12)
                stats["agency_setting_missing"] = 1
                break
            local = local_only_urls(store)
            if not _still_eligible(store, row, local):
                stats["agency_lead_skipped"] = stats.get("agency_lead_skipped", 0) + 1
                continue
            candidate = _candidate_from_row(row)
            curated = row["origin"] == "curated"
            if curated and curated_used >= curated_cap:
                stats["curated_over_share"] = stats.get("curated_over_share", 0) + 1
                continue
            before = client.requests
            parent = client.budget
            if curated and parent is not None:  # curated suggestions can never spend past their share, even mid-probe
                client.budget = LimitedBudget(parent, curated_cap - curated_used)
            try:
                result = bd.discover(ctx, candidate, client, now=now)
            finally:
                client.budget = parent
            used = client.requests - before
            if curated:
                curated_used += used
            stats[result["outcome"]] = stats.get(result["outcome"], 0) + 1
            stats["matches"] = stats.get("matches", 0) + result["matches"]
            stats["requests"] += used
            if result["outcome"] == "budget_exhausted":
                store.writer("discovery").update("discovery_queue", {"state": "deferred", "last_outcome": "budget_exhausted",
                                                                     "updated_at": now}, "employer_norm = ?", (row["employer_norm"],))
                if curated and budget.remaining() > 0:
                    continue  # only the curated share ran out: lead-derived discovery goes on
                break
            wait = bd.retry_days(policy, result["outcome"])
            nxt = (parse_utc(now) + timedelta(days=wait)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            state = "skipped" if result["outcome"] == "skipped_policy" else "done"
            store.writer("discovery").update("discovery_queue", {"state": state, "last_outcome": result["outcome"], "next_attempt_at": nxt,
                                                                 "updated_at": now}, "employer_norm = ?", (row["employer_norm"],))
            if budget.remaining() <= 0:
                break
    finally:
        if own_client:
            client.close()
    return stats
