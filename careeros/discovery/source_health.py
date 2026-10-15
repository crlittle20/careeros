"""Source health and adaptive scanning (2026-10-12, Part 8).

Every board scan is recorded with its outcome, size and request cost:
- complete:    the board answered normally; only these scans may count a posting as missing or update "last complete";
- suspicious:  the board answered, but with nothing, or far fewer postings than last time (possible partial response,
               schema change or obsolete endpoint): nothing is closed and the count is kept for comparison;
- failed / restricted / parse_error: nothing is ingested or closed. Restricted (401/403) boards are disabled only after
  `restricted_disable_after` restricted scans in a row; a 404 board is already marked invalid by the scanner.

Scan cadence (used only when `scanning.adaptive` is on; otherwise every board is scanned daily as before), from the
employer's CAREER FIT priority (employer_intel), 2026-10-13:
- daily:        pinned; new evidence (a lead or recommendation) in the last week; a new board (first
                `new_board_days`); career-fit priority A; or no career fit computed yet;
- every 3 days: priority B;
- weekly:       priority C, and dormant employers;
- monthly:      dormant with no relevant role ever, on a board older than `monthly_after_days`.
Hiring inactivity never disables a board, and cadence never weakens re-verification (a separate schedule).

BOARD HEALTH (0-100, operations only; it never changes trust or ownership): the last 10 scans that were not part of a
provider-wide outage, complete = 1, suspicious = 0.6, restricted = 0.2, failed / parse error = 0, minus 10 for
repeated rate limiting. PROVIDER health: when at least `provider_degraded_min_failures` boards of one provider fail in
the same run and they are at least `provider_degraded_ratio` of that provider's boards, the provider (adapter) is
degraded: those failures are recorded as such and held against no employer.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from careeros.core.clock import parse_utc

CADENCE_DAYS = {"daily": 1, "every_3_days": 3, "weekly": 7, "monthly": 30}
SCAN_WEIGHTS = {"complete": 1.0, "suspicious": 0.6, "restricted": 0.2, "failed": 0.0, "parse_error": 0.0}


def _cfg(policy) -> dict:
    return {"adaptive": False, "new_board_days": 14, "monthly_after_days": 120, "suspicious_drop_ratio": 0.4,
            "restricted_disable_after": 3, "priority_cadence": {"A": "daily", "B": "every_3_days", "C": "weekly", "dormant": "weekly"},
            "provider_degraded_min_failures": 3, "provider_degraded_ratio": 0.5, "failures_before_reverify": 3,
            **(policy.get("scanning", default={}) or {})}


def classify_scan(previous_count: int | None, count: int, policy) -> str:
    if previous_count and count == 0 and previous_count >= 3:
        return "suspicious"
    if previous_count and previous_count >= 10 and count < _cfg(policy)["suspicious_drop_ratio"] * previous_count:
        return "suspicious"
    return "complete"


def failure_outcome(code: str) -> str:
    if code == "access_restricted":
        return "restricted"
    if code in ("invalid_json", "unexpected_shape"):
        return "parse_error"
    return "failed"


def record_scan(store, employer_id: str, outcome: str, *, postings: int | None = None, requests: int = 0,
                error_code: str | None = None, closed: int = 0, possibly_absent: int = 0, provider_degraded: bool = False) -> None:
    store.writer("discovery").insert("source_scans", {
        "scan_id": f"scan:{uuid.uuid4().hex[:16]}", "employer_id": employer_id, "at": store.clock.now(), "outcome": outcome,
        "postings": postings, "requests": requests, "error_code": error_code, "closed": closed, "possibly_absent": possibly_absent,
        "provider_degraded": int(provider_degraded)})


def historical_qualified(store, employer_id: str) -> int:
    return store.one("""SELECT COUNT(DISTINCT j.job_id) c FROM jobs j JOIN scores s ON s.job_id = j.job_id
                        WHERE j.employer_id = ? AND s.tier IN ('A','B','near_miss')""", (employer_id,))["c"]


def _age_days(now: str, then: str | None) -> float | None:
    return None if not then else (parse_utc(now) - parse_utc(then)) / timedelta(days=1)


def cadence(store, employer, policy, now: str) -> tuple[str, str]:
    """(daily | every_3_days | weekly | monthly, why) for one employer row."""
    cfg = _cfg(policy)
    if employer["pinned"]:
        return "daily", "pinned"
    evidence = _age_days(now, employer["last_evidence_at"])
    if evidence is not None and evidence <= 7:
        return "daily", "new evidence linked this board in the last week"
    age = _age_days(now, employer["verified_at"] or employer["last_polled_at"])
    if age is None or (age <= cfg["new_board_days"] and employer["admitted_via"] != "legacy"):
        return "daily", "new board"
    fit = store.one("SELECT career_fit_score, career_priority FROM employer_entities WHERE entity_id = ?", (employer["entity_id"],)) \
        if employer["entity_id"] else None
    if not fit or not fit["career_priority"]:
        return "daily", "career fit not measured yet"
    level = cfg["priority_cadence"].get(fit["career_priority"], "weekly")
    why = f"career fit {fit['career_priority']} ({fit['career_fit_score']})"
    if fit["career_priority"] == "dormant" and age > cfg["monthly_after_days"] and historical_qualified(store, employer["employer_id"]) == 0 \
            and not fit["career_fit_score"]:
        return "monthly", why + ", no relevant role ever"
    return level, why


def due(store, employer, policy, now: str) -> tuple[bool, str, str]:
    """(scan now?, cadence, why). Always due when adaptive scanning is off."""
    level, why = cadence(store, employer, policy, now)
    if not _cfg(policy)["adaptive"]:
        return True, level, why
    last = employer["last_complete_scan_at"]
    if not last:
        return True, level, why
    elapsed = (parse_utc(now) - parse_utc(last)) / timedelta(days=1)
    return elapsed >= CADENCE_DAYS[level] - 0.05, level, why


def restricted_streak(store, employer_id: str) -> int:
    streak = 0
    for row in store.all("SELECT outcome FROM source_scans WHERE employer_id = ? AND provider_degraded = 0 "
                         "ORDER BY at DESC, rowid DESC LIMIT 10", (employer_id,)):
        if row["outcome"] != "restricted":
            break
        streak += 1
    return streak


def failure_streak(store, employer_id: str) -> int:
    """Consecutive failed / parse-error scans of this board alone (provider-wide outages excluded)."""
    streak = 0
    for row in store.all("SELECT outcome FROM source_scans WHERE employer_id = ? AND provider_degraded = 0 "
                         "ORDER BY at DESC, rowid DESC LIMIT 10", (employer_id,)):
        if row["outcome"] not in ("failed", "parse_error"):
            break
        streak += 1
    return streak


def board_health(store, employer_id: str) -> int | None:
    rows = store.all("SELECT outcome, error_code FROM source_scans WHERE employer_id = ? AND provider_degraded = 0 "
                     "ORDER BY at DESC, rowid DESC LIMIT 10", (employer_id,))
    if not rows:
        return None
    value = 100 * sum(SCAN_WEIGHTS.get(r["outcome"], 0) for r in rows) / len(rows)
    if sum(1 for r in rows if (r["error_code"] or "") == "rate_limited") >= 2:
        value -= 10
    return max(0, min(100, round(value)))


def degraded_providers(failures: dict, attempts: dict, policy) -> set[str]:
    """Providers whose failures this run look like an outage or an adapter problem rather than one employer's board."""
    cfg = _cfg(policy)
    return {p for p, n in failures.items()
            if n >= int(cfg["provider_degraded_min_failures"]) and n >= float(cfg["provider_degraded_ratio"]) * attempts.get(p, n)}


def set_provider_state(store, provider: str, state: str, detail: str | None = None) -> bool:
    """Record a provider's state; True when it changed (an alert-worthy event)."""
    now = store.clock.now()
    row = store.one("SELECT * FROM provider_status WHERE provider = ?", (provider,))
    writer = store.writer("discovery")
    if row is None:
        writer.insert("provider_status", {"provider": provider, "state": state, "since": now, "updated_at": now, "detail": detail})
        changed = state != "healthy"
    elif row["state"] != state:
        writer.update("provider_status", {"state": state, "since": now, "updated_at": now, "detail": detail}, "provider = ?", (provider,))
        changed = True
    else:
        writer.update("provider_status", {"updated_at": now, "detail": detail}, "provider = ?", (provider,))
        changed = False
    if changed:
        writer.event("ats_adapter_degraded" if state != "healthy" else "ats_adapter_recovered",
                     outcome="failure" if state != "healthy" else "success", entity_type="ats_provider", entity_id=provider,
                     metadata={"state": state, "detail": detail})
    return changed


def touch(store, employer_id: str) -> None:
    """Credible new evidence for a board (an inbox or lead link to it): scan it at the next run again."""
    store.writer("discovery").update("employers", {"last_evidence_at": store.clock.now()}, "employer_id = ?", (employer_id,))


def health(store, policy, now: str) -> list[dict]:
    """One row per employer: cadence, last complete scan, recent outcomes, request cost, qualified arrivals, and the
    three separate scores (trust, career fit, board health)."""
    from careeros.discovery.board_registry import mapping_for_employer, scan_gate
    rows = []
    for e in store.all("SELECT * FROM employers ORDER BY name"):
        recent = store.all("SELECT outcome, requests, postings FROM source_scans WHERE employer_id = ? ORDER BY at DESC, rowid DESC LIMIT 7",
                           (e["employer_id"],))
        level, why = cadence(store, e, policy, now)
        m = mapping_for_employer(store, e)
        fit = store.one("SELECT career_fit_score, career_priority FROM employer_entities WHERE entity_id = ?", (e["entity_id"],)) \
            if e["entity_id"] else None
        admitted, admission = scan_gate(store, e, policy, now)
        rows.append({"employer": e["name"], "employer_id": e["employer_id"], "status": e["status"], "cadence": level, "why": why,
                     "last_complete_scan_at": e["last_complete_scan_at"], "last_qualified_at": e["last_qualified_at"],
                     "recent": [r["outcome"] for r in recent], "requests_7": sum(r["requests"] or 0 for r in recent),
                     "postings": e["last_posting_count"], "qualified_total": historical_qualified(store, e["employer_id"]),
                     "pinned": bool(e["pinned"]), "verification": m["verification_status"] if m else None,
                     "trust": m["board_trust_score"] if m else None, "board_health": board_health(store, e["employer_id"]),
                     "career_fit": fit["career_fit_score"] if fit else None, "priority": fit["career_priority"] if fit else None,
                     "admitted": admitted, "admission": admission})
    return rows
