"""Discovery and opportunity metrics (2026-10-12), instrumented for a future 60-day pilot.

Kept separate on purpose:
- NEW OPPORTUNITIES: qualified openings that came from a discovered board after its activation, or from a rescued title,
  and that no alert or recommendation had already given you;
- LEAD ENRICHMENT: leads you already had that discovery turned into graded postings (time saved, not new supply).
Rescans, cosmetic edits, duplicates and reposts never count as success. Pilot targets are shown as provisional
targets next to the measured value; nothing claims a target is met before the pilot window has run.
"""
from __future__ import annotations

from datetime import timedelta

from careeros.core.clock import parse_utc
from careeros.db.store import Store

QUALIFIED = "('A','B','near_miss')"


def _since(store: Store, days: int) -> str:
    return (parse_utc(store.clock.now()) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _qualified(store: Store, job_ids) -> list[str]:
    out = []
    for job_id in job_ids:
        row = store.one(f"""SELECT 1 FROM jobs j JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id
                            ORDER BY at DESC, rowid DESC LIMIT 1) WHERE j.job_id = ? AND s.tier IN {QUALIFIED}
                            AND j.current_state != 'duplicate' AND COALESCE(j.exclude_from_kpi, 0) = 0""", (job_id,))
        if row:
            out.append(job_id)
    return out


def metrics(store: Store, policy, days: int = 30) -> dict:
    since = _since(store, days)
    one = lambda sql, *a: store.one(sql, a)["c"]
    considered = one("SELECT COUNT(*) c FROM discovery_queue WHERE added_at >= ?", since)
    # verified = admitted by board verification (the employer's own site first); the older discovery identity flag also
    # marks boards that verification later rejected, so it is not used here (Functional MVP gate, 2026-10-13)
    verified = one("SELECT COUNT(*) c FROM board_mappings WHERE verification_status = 'active' AND collection = 'allowed' "
                   "AND updated_at >= ?", since)
    unresolved = one("SELECT COUNT(*) c FROM board_mappings WHERE verification_status = 'review_required' AND collection = 'allowed'")
    manual_only = one("SELECT COUNT(*) c FROM board_mappings WHERE collection = 'manual_only'")
    # a wrong-employer admission: a board that was admitted to the daily scan and later failed an identity check
    wrong_employer = one("SELECT COUNT(*) c FROM board_mappings WHERE activated_at IS NOT NULL AND "
                         "verification_failure IN ('identity_mismatch','identity_changed')")
    audited = store.one("""SELECT SUM(audit = 'correct') ok, SUM(audit = 'wrong') bad FROM lead_matches
                           WHERE decision = 'matched' AND method != 'none' AND audit IS NOT NULL""")
    auto = one("SELECT COUNT(*) c FROM lead_matches WHERE decision = 'matched' AND decided_at >= ?", since)
    ambiguous = one("SELECT COUNT(*) c FROM lead_matches WHERE decision = 'ambiguous' AND audit IS NULL")

    # new opportunities: from activated discovered boards (after activation) or rescued titles, never already a lead
    from_boards = [r["job_id"] for r in store.all(
        """SELECT j.job_id FROM jobs j JOIN employers e ON e.employer_id = j.employer_id JOIN board_mappings m ON m.mapping_id = e.mapping_id
           WHERE m.activated_at IS NOT NULL AND j.first_seen_at >= m.activated_at AND j.first_seen_at >= ? AND j.variant_of IS NULL
             AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.job_id = j.job_id AND l.first_seen_at <= j.first_seen_at)""", (since,))]
    rescued = store.all("SELECT job_id FROM title_reviews WHERE outcome = 'rescued' AND reviewed_at >= ?", (since,))
    rescued_ids = [r["job_id"] for r in rescued]
    rescued_qualified = _qualified(store, rescued_ids)
    new_qualified = sorted(set(_qualified(store, from_boards)) | set(rescued_qualified))
    enriched = one("SELECT COUNT(DISTINCT lead_id) c FROM lead_matches WHERE decision = 'matched' AND job_id IS NOT NULL AND decided_at >= ?", since)

    distinct_qualified = one(f"""SELECT COUNT(*) c FROM jobs j JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id
                                 ORDER BY at DESC, rowid DESC LIMIT 1) WHERE s.tier IN {QUALIFIED} AND j.variant_of IS NULL
                                 AND j.current_state = 'scored' AND j.first_seen_at >= ? AND COALESCE(j.exclude_from_kpi, 0) = 0""", since)
    # drills and test rows (exclude_from_kpi) never count (AUD-006; found by the analytics export's reconciliation)
    duplicates_avoided = one("SELECT COUNT(*) c FROM jobs WHERE current_state = 'duplicate' AND first_seen_at >= ? "
                             "AND COALESCE(exclude_from_kpi, 0) = 0", since) + \
        one("SELECT COUNT(*) c FROM opening_links WHERE created_at >= ? AND job_id NOT IN "
            "(SELECT job_id FROM jobs WHERE exclude_from_kpi = 1)", since)
    from careeros.review.shortlist import shortlist
    items = shortlist(store)
    unknown_items = sum(1 for i in items if i["unknowns"])
    scans = {r["outcome"]: r["c"] for r in store.all("SELECT outcome, COUNT(*) c FROM source_scans WHERE at >= ? GROUP BY outcome", (since,))}
    scan_requests = one("SELECT COALESCE(SUM(requests), 0) c FROM source_scans WHERE at >= ?", since)
    discovery_requests = one("SELECT COALESCE(SUM(requests), 0) c FROM discovery_attempts WHERE attempted_at >= ?", since)
    pursued = [i for i in items if i["label"] in ("apply", "stretch")]
    applied = sum(1 for i in items if i["applied"])
    minutes = store.all("SELECT metadata_json FROM events WHERE event_type = 'review_minutes_logged' AND occurred_at >= ?", (since,))
    import json
    review_minutes = sum(float(json.loads(m["metadata_json"] or "{}").get("minutes", 0)) for m in minutes)
    weeks = max(1.0, days / 7)
    pilot = ((policy.get("source_discovery", default={}) or {}).get("pilot") or {})
    targets = pilot.get("targets") or {"new_apply_worthy_per_30_days": 5, "audited_match_accuracy": 0.95,
                                       "wrong_employer_admissions": 0, "extra_review_minutes_per_week": 15}
    accuracy = (audited["ok"] or 0) / ((audited["ok"] or 0) + (audited["bad"] or 0)) if (audited["ok"] or audited["bad"]) else None
    return {
        "window_days": days,
        "employers": {"considered": considered, "verified": verified, "awaiting_your_verification": unresolved,
                      "manual_only_sources": manual_only, "wrong_employer_admissions": wrong_employer},
        "matching": {"automatic_matches": auto, "audited_correct": audited["ok"] or 0, "audited_wrong": audited["bad"] or 0,
                     "audited_accuracy": accuracy, "ambiguous_waiting": ambiguous},
        "opportunities": {"distinct_qualified_openings": distinct_qualified, "new_qualified_from_discovery": len(new_qualified),
                          "rescued_titles": len(rescued_ids), "rescued_and_qualified": len(rescued_qualified),
                          "lead_enrichment_matches": enriched, "duplicate_reviews_avoided": duplicates_avoided},
        "review_burden": {"shortlist": len(items), "shortlist_with_open_questions": unknown_items,
                          "logged_review_minutes_per_week": round(review_minutes / weeks, 1)},
        "sources": {"scans": scans, "scan_requests": scan_requests, "discovery_requests": discovery_requests},
        "outcomes": {"pursued": len(pursued), "applied": applied},
        "verification": verification_metrics(store, since),
        "pilot": {"start": pilot.get("start"), "days": pilot.get("days", 60), "targets": targets,
                  "status": "not started" if not pilot.get("start") else "running: targets are provisional until the window ends"},
    }


def verification_metrics(store: Store, since: str) -> dict:
    """How well board verification itself is working (2026-10-13). Legacy boards are counted separately so the
    verifier's own record is not flattered by boards that existed before it."""
    one = lambda sql, *a: store.one(sql, a)["c"]  # noqa: E731
    by_status = {r["verification_status"]: r["c"] for r in store.all(
        "SELECT verification_status, COUNT(*) c FROM board_mappings GROUP BY verification_status")}
    new = store.all("SELECT verification_status s, created_at, verified_at, verified_method FROM board_mappings "
                    "WHERE provenance != 'legacy_unknown' AND collection = 'allowed' AND created_at >= ?", (since,))
    share = lambda states: round(sum(1 for r in new if r["s"] in states) / len(new), 2) if new else None  # noqa: E731
    from careeros.core.clock import parse_utc
    days = [(parse_utc(r["verified_at"]) - parse_utc(r["created_at"])).total_seconds() / 86400 for r in new
            if r["verified_at"] and r["verified_method"] == "official_site"]
    validations = {r["outcome"]: r["c"] for r in store.all(
        "SELECT outcome, COUNT(*) c FROM verification_runs WHERE kind = 'validation' AND finished_at >= ? GROUP BY outcome", (since,))}
    passed = validations.get("active", 0) + validations.get("validated", 0)
    scans = store.all("""SELECT e.ats, s.outcome, s.provider_degraded, COUNT(*) c FROM source_scans s JOIN employers e
                         ON e.employer_id = s.employer_id WHERE s.at >= ? GROUP BY 1, 2, 3""", (since,))
    adapter = {}
    for r in scans:
        totals = adapter.setdefault(r["ats"], [0, 0])
        totals[1] += r["c"]
        if r["outcome"] in ("failed", "parse_error"):
            totals[0] += r["c"]
    total_scans = sum(r["c"] for r in scans)
    by_priority = {r["p"] or "unmeasured": r["c"] for r in store.all(
        """SELECT en.career_priority p, COUNT(*) c FROM source_scans s JOIN employers e ON e.employer_id = s.employer_id
           LEFT JOIN employer_entities en ON en.entity_id = e.entity_id WHERE s.at >= ? GROUP BY 1""", (since,))}
    active_entities = one("SELECT COUNT(DISTINCT entity_id) c FROM board_mappings WHERE verification_status = 'active'")
    relevant = one("""SELECT COUNT(*) c FROM jobs j JOIN employers e ON e.employer_id = j.employer_id
                      WHERE j.family IS NOT NULL AND j.first_seen_at >= ?""", since)
    events = lambda kind: one("SELECT COUNT(*) c FROM events WHERE event_type = ? AND occurred_at >= ?", kind, since)  # noqa: E731
    return {
        "boards_by_status": by_status,
        "new_candidates": len(new),
        "share_verified": share(("quarantined", "active")), "share_in_review": share(("review_required",)),
        "share_rejected": share(("rejected",)),
        "average_trust": store.one("SELECT ROUND(AVG(board_trust_score), 1) c FROM board_mappings WHERE board_trust_score IS NOT NULL")["c"],
        "average_career_fit": store.one("SELECT ROUND(AVG(career_fit_score), 1) c FROM employer_entities WHERE career_fit_score IS NOT NULL")["c"],
        "overdue_reverification": one("SELECT COUNT(*) c FROM board_mappings WHERE verification_status = 'active' AND reverify_at < ?",
                                      _since(store, 7)),  # stale: re-verification more than a week overdue
        "migrations": one("SELECT COUNT(*) c FROM board_mappings WHERE migrated_to IS NOT NULL"),
        "identity_mismatches": events("identity_mismatch"), "failed_handshakes": events("ats_handshake_failed"),
        "quarantine_pass_rate": round(passed / (passed + validations.get("validation_failed", 0)), 2) if passed or validations else None,
        "adapter_error_rate": {ats: round(bad / total, 2) for ats, (bad, total) in adapter.items() if total},
        "scan_success_rate": round(sum(r["c"] for r in scans if r["outcome"] == "complete") / total_scans, 2) if total_scans else None,
        "false_positive_discoveries": one("SELECT COUNT(*) c FROM board_mappings WHERE provenance IN ('discovery','job_link') "
                                          "AND verification_status = 'rejected'"),
        "boards_later_found_invalid": one("SELECT COUNT(*) c FROM board_mappings WHERE activated_at IS NOT NULL AND verification_failure IN "
                                          "('identity_mismatch','identity_changed','board_not_found','handshake_lost')"),
        "days_discovery_to_verification": round(sum(days) / len(days), 1) if days else None,
        "active_employers": active_entities, "manual_only_boards": by_status.get("manual_only", 0),
        "scans_by_career_priority": by_priority,
        "relevant_jobs_per_active_employer": round(relevant / active_entities, 1) if active_entities else None,
        "open_reviews": one("SELECT COUNT(*) c FROM board_reviews WHERE status = 'open'"),
    }


def log_review_minutes(store: Store, minutes: float) -> None:
    store.writer("review").event("review_minutes_logged", outcome="informational", entity_type="review", entity_id="weekly",
                                 actor_type="human", actor_id="user", metadata={"minutes": float(minutes)})
