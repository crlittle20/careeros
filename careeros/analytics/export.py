"""Analytics export (Portfolio Prompt 7, 2026-10-13): a star schema over the CareerOS database, written as CSV for
Power BI. `careeros export` builds it, validates it and writes it with a manifest. The export only reads.

Every table and column is described in docs/analytics_data_dictionary.md; a test keeps the two in step.
- Keys are natural and stable (job, board, employer, provider, source, family, location, date), or a sequence number in
  recording order for event facts. One database therefore always gives byte-identical files; random record IDs are
  never exported.
- A missing dimension reference points at the "(none)" member of that dimension. A missing date or measure is left
  empty: it is never turned into 0.
- Rows marked exclude_from_kpi (drills and test data) are left out, along with everything that hangs off them.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

from careeros.core.clock import parse_utc
from careeros.discovery import sources
from careeros.paths import CONFIG_DIR

SCHEMA_VERSION = "analytics-1"
NONE = "(none)"
QUALIFIED_TIERS = ("A", "B", "near_miss")       # as in review/discovery_metrics.py
RECOMMENDED_TIERS = ("A", "B")
TITLE_SCREEN = "title_screen"                   # pipeline.TITLE_SCREEN: the gate record of a title-screen decision

# Column types: key / text (text), int, num (decimal), flag (0 or 1), date (YYYY-MM-DD). The first column is the
# table's primary key.
TABLES = {
    "dim_date": [("date_key", "int"), ("date", "date"), ("year", "int"), ("quarter", "int"), ("month", "int"),
                 ("month_name", "text"), ("month_start", "date"), ("iso_week", "int"), ("week_start", "date"),
                 ("day_of_week", "int"), ("day_name", "text"), ("is_weekend", "flag")],
    "dim_employer": [("employer_key", "key"), ("employer_name", "text"), ("official_domain", "text"), ("domain_status", "text"),
                     ("career_fit_score", "num"), ("career_priority", "text"), ("boards_total", "int"), ("boards_active", "int"),
                     ("is_verified_employer", "flag"), ("employer_origin", "text"), ("first_seen_date_key", "int")],
    "dim_board": [("board_key", "key"), ("employer_key", "key"), ("ats_key", "key"), ("board_token", "text"),
                  ("collection", "text"), ("verification_status", "text"), ("provenance", "text"), ("is_legacy", "flag"),
                  ("handshake_status", "text"), ("identity_check", "text"), ("validation_status", "text"),
                  ("board_trust_score", "num"), ("board_health_score", "num"), ("board_activity", "text"), ("verify_count", "int"),
                  ("is_paused", "flag"), ("paused_reason", "text"), ("migrated_to_board_key", "key"),
                  ("created_date_key", "int"), ("activated_date_key", "int"), ("last_checked_date_key", "int"),
                  ("retired_date_key", "int")],
    "dim_ats": [("ats_key", "key"), ("ats_name", "text"), ("collection_mode", "text"), ("is_automated", "flag")],
    "dim_source": [("source_key", "key"), ("source_name", "text"), ("channel", "text"), ("is_automated", "flag")],
    "dim_role_family": [("role_family_key", "key"), ("family_name", "text"), ("is_target", "flag"), ("sort_order", "int")],
    "dim_location": [("location_key", "key"), ("remote_scope", "text"), ("scope_label", "text"), ("work_arrangement", "text"),
                     ("is_remote", "flag"), ("is_us", "flag"), ("country", "text"), ("allowed_states", "text"),
                     ("excluded_states", "text"), ("sort_order", "int")],
    "dim_job": [("job_key", "key"), ("title", "text"), ("title_norm", "text"), ("posted_employer_name", "text"),
                ("department", "text"), ("requisition_id", "text"), ("employment_type", "text"), ("location_raw", "text"),
                ("timezone_requirement", "text"), ("pay_min", "num"), ("pay_max", "num"), ("pay_period", "text"),
                ("pay_currency", "text"), ("annual_pay_min", "num"), ("annual_pay_max", "num"), ("is_pay_stated", "flag"),
                ("posted_date", "date"), ("deadline_date", "date"), ("url", "text"), ("duplicate_of_job_key", "key"),
                ("variant_of_job_key", "key"), ("repost_of_job_key", "key")],
    "fact_job_evaluation": [
        ("job_key", "key"), ("employer_key", "key"), ("board_key", "key"), ("ats_key", "key"), ("role_family_key", "key"),
        ("location_key", "key"), ("discovery_source_key", "key"), ("first_seen_date_key", "int"), ("posted_date_key", "int"),
        ("last_seen_date_key", "int"), ("decided_date_key", "int"), ("closed_date_key", "int"), ("current_state", "text"),
        ("state_reason", "text"), ("rejection_reason", "text"), ("rejection_group", "text"), ("decision_stage", "text"),
        ("gate_decision", "text"), ("eligibility_state", "text"), ("tier", "text"), ("is_evaluated", "flag"),
        ("is_scored", "flag"), ("is_qualified", "flag"), ("is_recommended", "flag"), ("is_manual_review", "flag"),
        ("is_rejected", "flag"), ("is_duplicate", "flag"), ("is_closed", "flag"), ("is_variant", "flag"), ("is_repost", "flag"),
        ("linked_openings", "int"), ("eligibility_score", "num"), ("candidate_fit", "num"), ("career_value", "num"),
        ("freshness", "num"), ("apply_priority", "num"), ("skill_coverage", "num"), ("interview_band", "text"),
        ("decisive_unknowns", "int"), ("night_decision", "text"), ("night_mode", "text"), ("night_state", "text"),
        ("label", "text"), ("label_date_key", "int"), ("has_application", "flag"), ("sightings", "int"), ("source_count", "int"),
        ("meaningful_changes", "int"), ("evaluations", "int"), ("days_to_decision", "num"), ("days_listed", "num")],
    "fact_job_observation": [
        ("observation_key", "int"), ("job_key", "key"), ("employer_key", "key"), ("ats_key", "key"), ("source_key", "key"),
        ("evidence_level", "text"), ("observed_date_key", "int"), ("is_first_sighting", "flag"), ("is_cross_source", "flag"),
        ("has_lead", "flag")],
    "fact_board_scan": [
        ("scan_key", "int"), ("board_key", "key"), ("employer_key", "key"), ("ats_key", "key"), ("scan_date_key", "int"),
        ("outcome", "text"), ("is_complete", "flag"), ("is_failure", "flag"), ("error_code", "text"), ("postings", "int"),
        ("requests", "int"), ("jobs_closed", "int"), ("possibly_absent", "int"), ("provider_degraded", "flag")],
    "fact_verification": [
        ("verification_key", "int"), ("board_key", "key"), ("employer_key", "key"), ("ats_key", "key"), ("kind", "text"),
        ("started_date_key", "int"), ("finished_date_key", "int"), ("outcome", "text"), ("is_pass", "flag"),
        ("failure_class", "text"), ("trust_score", "num"), ("site_requests", "int"), ("ats_requests", "int"),
        ("evidence_items", "int"), ("evidence_points", "num")],
    "fact_application": [
        ("application_key", "int"), ("job_key", "key"), ("employer_key", "key"), ("ats_key", "key"), ("role_family_key", "key"),
        ("channel", "text"), ("applied_date_key", "int"), ("last_change_date_key", "int"), ("current_state", "text"),
        ("state_category", "text"), ("is_open", "flag"), ("got_answer", "flag"), ("reached_screening", "flag"),
        ("reached_interview", "flag"), ("reached_offer", "flag"), ("is_hired", "flag"), ("days_in_process", "num")],
}
DIMENSIONS = [t for t in TABLES if t.startswith("dim_")]
FACTS = [t for t in TABLES if t.startswith("fact_")]
# (table, column) -> the dimension it refers to. Date keys may be empty (the date does not apply); other keys never are.
REFERENCES = {}
for _table, _columns in TABLES.items():
    for _column, _ in _columns[1 if _table.startswith("dim_") else 0:]:   # fact_job_evaluation's own key is also its job reference
        if _column.endswith("_date_key"):
            REFERENCES[(_table, _column)] = "dim_date"
        elif _column in ("employer_key", "board_key", "ats_key", "role_family_key", "location_key", "job_key", "source_key"):
            REFERENCES[(_table, _column)] = f"dim_{_column[:-4]}"
        elif _column == "discovery_source_key":
            REFERENCES[(_table, _column)] = "dim_source"
        elif _column.endswith("_job_key"):
            REFERENCES[(_table, _column)] = "dim_job"
        elif _column == "migrated_to_board_key":
            REFERENCES[(_table, _column)] = "dim_board"
OPTIONAL_KEYS = {("dim_board", "migrated_to_board_key"), ("dim_job", "duplicate_of_job_key"), ("dim_job", "variant_of_job_key"),
                 ("dim_job", "repost_of_job_key")}   # empty when there is nothing to point at

SCOPES = {  # remote_scope -> (label, work arrangement, sort order); values from normalize/posting.remote_scope
    "US_nationwide": ("Remote: anywhere in the US", "Remote", 1), "US_restricted_states": ("Remote: limited US states", "Remote", 2),
    "state_specific": ("Remote: one US state", "Remote", 3), "US_unspecified": ("Remote: US, states not stated", "Remote", 4),
    "hybrid": ("Hybrid", "Hybrid", 5), "onsite": ("On-site", "On-site", 6), "international": ("Outside the US", "International", 7),
    "unknown": ("Unknown", "Unknown", 8)}
SOURCE_NAMES = {  # observation source_kind -> (name, channel, automated)
    "ats_scan": ("Job-board scan", "automatic", 1), "alert": ("Alert email", "email", 1),
    "recommendation": ("Recommendation inbox", "recommendation", 1), "manual_import": ("Saved page or file you imported", "you", 0),
    "human_text": ("Posting you pasted", "you", 0)}
ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workable": "Workable", "workday": "Workday",
             "icims": "iCIMS", "smartrecruiters": "SmartRecruiters", "linkedin": "LinkedIn", "indeed": "Indeed",
             "glassdoor": "Glassdoor", "manual": "Pasted or imported"}
REJECTION_GROUPS = {
    "title": ("title_excluded", "no_target_family", "generic_title_no_context"),
    "location": ("not_remote", "outside_us", "state_not_eligible", "state_excluded", "outside_commute"),
    "pay": ("below_pay_floor",),
    "requirements": ("degree_required", "years_far_short", "seniority_gap", "principal_strategic_scope", "license_required",
                     "certification_required", "clearance_required", "work_authorization", "sponsorship_unavailable",
                     "requirements_unclear"),
    "employment type": ("employment_type",),
    "safety": ("scam_signals",),
    "employer": ("employer_excluded",)}
ANSWERED = {"responded", "screening", "interviewing", "final_interview", "offer", "accepted", "declined", "rejected"}
SCREENED = {"screening", "interviewing", "final_interview", "offer", "accepted", "declined"}
INTERVIEWED = {"interviewing", "final_interview", "offer", "accepted", "declined"}
OFFERED = {"offer", "accepted", "declined"}


class ExportError(RuntimeError):
    pass


def _key(ts: str | None) -> int | None:
    return int(ts[:10].replace("-", "")) if ts else None


def _days(start: str | None, end: str | None) -> float | None:
    if not (start and end):
        return None
    return round((parse_utc(end) - parse_utc(start)).total_seconds() / 86400, 2)


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _group(reason: str | None) -> str | None:
    if not reason:
        return None
    return next((g for g, codes in REJECTION_GROUPS.items() if reason in codes), "other")


def _flag(value) -> int:
    return 1 if value else 0


# --- build -----------------------------------------------------------------------------------------------------------
def build(store, policy) -> dict[str, list[dict]]:
    """Every table of the star schema, as rows (dicts in TABLES column order)."""
    q = lambda sql, *a: [dict(r) for r in store.all(sql, a)]  # noqa: E731
    jobs = q("SELECT * FROM jobs WHERE COALESCE(exclude_from_kpi, 0) = 0 ORDER BY job_id")
    job_by_id = {j["job_id"]: j for j in jobs}

    # employers: one per employer record (entity), keyed by its normalised name
    entities = q("SELECT * FROM employer_entities ORDER BY canonical_norm, rowid")
    entity_key, taken = {}, {}
    for e in entities:
        base = e["canonical_norm"] or _norm(e["canonical_name"])
        taken[base] = taken.get(base, 0) + 1
        entity_key[e["entity_id"]] = base if taken[base] == 1 else f"{base} #{taken[base]}"
    boards = q("SELECT * FROM board_mappings ORDER BY mapping_id")
    board_ids = {b["mapping_id"] for b in boards}
    employer_rows = {r["employer_id"]: r for r in q("SELECT * FROM employers")}

    def job_board(job) -> str:
        row = employer_rows.get(job["employer_id"]) or {}
        board = row.get("mapping_id") or job["employer_id"]
        return board if board in board_ids else NONE

    def job_employer(job) -> str:
        entity = (employer_rows.get(job["employer_id"]) or {}).get("entity_id")
        if entity in entity_key:
            return entity_key[entity]
        return f"unmatched:{_norm(job['employer_name'])}" if job["employer_name"] else NONE

    def board_employer(board_key: str | None, entity: str | None = None) -> str:
        board = next((b for b in boards if b["mapping_id"] == board_key), None)
        entity = entity or (board or {}).get("entity_id") or (employer_rows.get(board_key) or {}).get("entity_id")
        return entity_key.get(entity, NONE)

    tables: dict[str, list[dict]] = {}
    per_entity = {}
    for b in boards:
        counts = per_entity.setdefault(b["entity_id"], [0, 0])
        counts[0] += 1
        counts[1] += b["verification_status"] == "active"
    rows = []
    for e in entities:
        total, active = per_entity.get(e["entity_id"], [0, 0])
        rows.append({"employer_key": entity_key[e["entity_id"]], "employer_name": e["canonical_name"],
                     "official_domain": e["official_domain"], "domain_status": e["domain_status"],
                     "career_fit_score": e["career_fit_score"], "career_priority": e["career_priority"], "boards_total": total,
                     "boards_active": active, "is_verified_employer": _flag(active), "employer_origin": "board" if total else "no board",
                     "first_seen_date_key": _key(e["first_seen_at"])})
    unmatched = {}
    for j in jobs:
        key = job_employer(j)
        if key.startswith("unmatched:") and key not in unmatched:
            unmatched[key] = {"employer_key": key, "employer_name": j["employer_name"], "official_domain": None,
                              "domain_status": None, "career_fit_score": None, "career_priority": None, "boards_total": 0,
                              "boards_active": 0, "is_verified_employer": 0, "employer_origin": "posting only",
                              "first_seen_date_key": _key(j["first_seen_at"])}
        elif key in unmatched:
            unmatched[key]["first_seen_date_key"] = min(unmatched[key]["first_seen_date_key"], _key(j["first_seen_at"]))
    rows += [unmatched[k] for k in sorted(unmatched)]
    rows.append({"employer_key": NONE, "employer_name": "(no employer)", "official_domain": None, "domain_status": None,
                 "career_fit_score": None, "career_priority": None, "boards_total": 0, "boards_active": 0,
                 "is_verified_employer": 0, "employer_origin": "none", "first_seen_date_key": None})
    tables["dim_employer"] = rows

    tables["dim_board"] = [{
        "board_key": b["mapping_id"], "employer_key": entity_key.get(b["entity_id"], NONE), "ats_key": b["ats"] or NONE,
        "board_token": b["board_token"], "collection": b["collection"], "verification_status": b["verification_status"],
        "provenance": b["provenance"], "is_legacy": _flag(b["provenance"] == "legacy_unknown"),
        "handshake_status": b["handshake_status"], "identity_check": b["identity_check"], "validation_status": b["validation_status"],
        "board_trust_score": b["board_trust_score"], "board_health_score": b["board_health_score"], "board_activity": b["board_activity"],
        "verify_count": b["verify_count"], "is_paused": _flag(b["paused_reason"]), "paused_reason": b["paused_reason"],
        "migrated_to_board_key": b["migrated_to"] if b["migrated_to"] in board_ids else None,
        "created_date_key": _key(b["created_at"]), "activated_date_key": _key(b["activated_at"]),
        "last_checked_date_key": _key(b["last_checked_at"]), "retired_date_key": _key(b["retired_at"])} for b in boards]
    tables["dim_board"].append({c: None for c, _ in TABLES["dim_board"]} | {
        "board_key": NONE, "employer_key": NONE, "ats_key": NONE, "board_token": "(no board)", "is_legacy": 0, "is_paused": 0})

    leads = {r["lead_id"]: r for r in q("SELECT lead_id, site, job_id FROM leads")}
    providers = ({"greenhouse", "lever", "ashby", "workable"} | {j["ats"] for j in jobs if j["ats"]}
                 | {b["ats"] for b in boards if b["ats"]} | {r["site"] for r in leads.values() if r["site"]})
    tables["dim_ats"] = [{"ats_key": a, "ats_name": ATS_NAMES.get(a, a.title()),
                          "collection_mode": "you" if a == "manual" else sources.status(a),
                          "is_automated": _flag(a != "manual" and sources.status(a) == "allowed")} for a in sorted(providers)]
    tables["dim_ats"].append({"ats_key": NONE, "ats_name": "(none)", "collection_mode": "none", "is_automated": 0})

    observations = q("SELECT rowid, * FROM observations ORDER BY rowid")
    kinds = set(SOURCE_NAMES) | {o["source_kind"] for o in observations}
    tables["dim_source"] = [{"source_key": k, "source_name": SOURCE_NAMES.get(k, (k,))[0],
                             "channel": SOURCE_NAMES.get(k, (k, "other"))[1], "is_automated": SOURCE_NAMES.get(k, (k, "", 0))[2]}
                            for k in sorted(kinds)]
    tables["dim_source"].append({"source_key": NONE, "source_name": "(unknown)", "channel": "none", "is_automated": 0})

    families = policy.get("families", default={}) or {}
    rows = [{"role_family_key": code, "family_name": (spec or {}).get("label") or code.replace("_", " "), "is_target": 1,
             "sort_order": n} for n, (code, spec) in enumerate(families.items(), 1)]
    for code in sorted({j["family"] for j in jobs if j["family"] and j["family"] not in families}):
        rows.append({"role_family_key": code, "family_name": code.replace("_", " "), "is_target": 1, "sort_order": len(rows) + 1})
    rows.append({"role_family_key": NONE, "family_name": "No target family", "is_target": 0, "sort_order": len(rows) + 1})
    tables["dim_role_family"] = rows

    def states(text: str | None) -> str:
        return ",".join(sorted(json.loads(text or "[]")))

    def location_key(job) -> str:
        return f"{job['remote_scope'] or 'unknown'}|{job['country'] or 'unknown'}|{states(job['remote_states_json'])}|" \
               f"{states(job['remote_excluded_states_json'])}"
    rows = {}
    for j in jobs:
        key = location_key(j)
        scope = j["remote_scope"] or "unknown"
        label, arrangement, order = SCOPES.get(scope, (scope, "Unknown", 9))
        rows[key] = {"location_key": key, "remote_scope": scope, "scope_label": label, "work_arrangement": arrangement,
                     "is_remote": _flag(arrangement == "Remote"), "is_us": _flag(j["country"] == "US"), "country": j["country"] or "unknown",
                     "allowed_states": states(j["remote_states_json"]) or None,
                     "excluded_states": states(j["remote_excluded_states_json"]) or None, "sort_order": order}
    tables["dim_location"] = [rows[k] for k in sorted(rows, key=lambda k: (rows[k]["sort_order"], k))] + [
        {"location_key": NONE, "remote_scope": None, "scope_label": "(none)", "work_arrangement": "Unknown", "is_remote": 0,
         "is_us": 0, "country": None, "allowed_states": None, "excluded_states": None, "sort_order": 99}]

    from careeros.qualify.gate import annualize
    tables["dim_job"] = [{
        "job_key": j["job_id"], "title": j["title"], "title_norm": j["title_norm"], "posted_employer_name": j["employer_name"],
        "department": j["department"], "requisition_id": j["requisition_id"], "employment_type": j["employment_type"],
        "location_raw": j["location_raw"], "timezone_requirement": j["timezone_requirement"], "pay_min": j["comp_min"],
        "pay_max": j["comp_max"], "pay_period": j["comp_period"], "pay_currency": j["comp_currency"],
        "annual_pay_min": annualize(j["comp_min"], j["comp_period"]), "annual_pay_max": annualize(j["comp_max"], j["comp_period"]),
        "is_pay_stated": _flag(j["comp_min"] is not None or j["comp_max"] is not None),
        "posted_date": (j["posted_at"] or "")[:10] or None, "deadline_date": (j["deadline_at"] or "")[:10] or None, "url": j["url"],
        "duplicate_of_job_key": j["duplicate_of"] if j["duplicate_of"] in job_by_id else None,
        "variant_of_job_key": j["variant_of"] if j["variant_of"] in job_by_id else None,
        "repost_of_job_key": j["repost_of"] if j["repost_of"] in job_by_id else None} for j in jobs]
    tables["dim_job"].append({c: None for c, _ in TABLES["dim_job"]} | {"job_key": NONE, "title": "(no job)", "is_pay_stated": 0})

    # --- the latest record of each kind, per job ----------------------------------------------------------------------
    def latest(sql: str) -> dict:
        out = {}
        for r in q(sql):
            out[r["job_id"]] = r
        return out
    gates = latest("SELECT * FROM gate_results ORDER BY at, rowid")
    scores = latest("SELECT * FROM scores ORDER BY at, rowid")
    nights = latest("SELECT * FROM night_results ORDER BY at, rowid")
    labels = latest("SELECT * FROM labels WHERE profile = 'primary' ORDER BY at, rowid")
    last_reason = {r["entity_id"]: r["reason_code"] for r in q("SELECT entity_id, reason_code FROM transitions "
                                                                 "WHERE entity_type = 'job' ORDER BY entity_id, sequence")}
    count = lambda sql: {r["job_id"]: r["c"] for r in q(sql)}  # noqa: E731
    evaluations = count("SELECT job_id, COUNT(*) c FROM gate_results GROUP BY job_id")
    changes = count("SELECT job_id, COUNT(*) c FROM job_changes WHERE meaningful = 1 GROUP BY job_id")
    links = count("SELECT job_id, COUNT(*) c FROM opening_links GROUP BY job_id")
    applications = [a for a in q("SELECT * FROM applications WHERE COALESCE(exclude_from_kpi, 0) = 0 ORDER BY applied_at, rowid")
                    if a["job_id"] in job_by_id]
    applied = {a["job_id"] for a in applications}
    # A job's sightings in the order they happened. At the same moment, the alert, recommendation or paste that led
    # CareerOS to a posting comes before the single board read it triggered (recorded first, but caused by it).
    order = lambda o: (o["observed_at"] or "", 1 if o["source_kind"] == "ats_scan" else 0, o["rowid"])  # noqa: E731
    seen: dict[str, list[str]] = {}
    rank = {}
    for o in sorted((o for o in observations if o["job_id"] in job_by_id), key=order):
        rank[o["rowid"]] = len(seen.get(o["job_id"], []))
        seen.setdefault(o["job_id"], []).append(o["source_kind"])

    rows = []
    for j in jobs:
        jid, state = j["job_id"], j["current_state"]
        g, s, n, lab = gates.get(jid), scores.get(jid), nights.get(jid), labels.get(jid)
        if s and not (g and g["decision"] != "rejected" and s["at"] >= g["at"]):
            s = None   # a score only describes the job while its latest decision is the one it was made under
        comp = json.loads(s["components_json"]) if s else {}
        tier = s["tier"] if s else None
        scored = state == "scored"
        reason = last_reason.get(jid)
        stage = ("deduplication" if state == "duplicate" else "title_screen" if g and g["extraction_fingerprint"] == TITLE_SCREEN
                 else "eligibility_rules" if g else None)
        rows.append({
            "job_key": jid, "employer_key": job_employer(j), "board_key": job_board(j), "ats_key": j["ats"] or NONE,
            "role_family_key": j["family"] or NONE, "location_key": location_key(j),
            "discovery_source_key": (seen.get(jid) or [NONE])[0], "first_seen_date_key": _key(j["first_seen_at"]),
            "posted_date_key": _key(j["posted_at"]), "last_seen_date_key": _key(j["last_seen_at"]),
            "decided_date_key": _key(g["at"]) if g else None, "closed_date_key": _key(j["closed_at"]), "current_state": state,
            "state_reason": reason, "rejection_reason": reason if state == "rejected" else None,
            "rejection_group": _group(reason) if state == "rejected" else None, "decision_stage": stage,
            "gate_decision": g["decision"] if g else None, "eligibility_state": s["state"] if s else None, "tier": tier,
            "is_evaluated": _flag(state in ("scored", "rejected", "needs_review")), "is_scored": _flag(scored),
            "is_qualified": _flag(scored and tier in QUALIFIED_TIERS and not j["variant_of"]),
            "is_recommended": _flag(scored and tier in RECOMMENDED_TIERS),
            "is_manual_review": _flag((scored and tier == "near_miss") or state == "needs_review"),
            "is_rejected": _flag(state == "rejected"), "is_duplicate": _flag(state == "duplicate"), "is_closed": _flag(state == "closed"),
            "is_variant": _flag(j["variant_of"]), "is_repost": _flag(j["repost_of"]), "linked_openings": links.get(jid, 0),
            "eligibility_score": s["eligibility"] if s else None, "candidate_fit": s["candidate_fit"] if s else None,
            "career_value": s["career_value"] if s else None, "freshness": s["freshness"] if s else None,
            "apply_priority": s["apply_priority"] if s else None, "skill_coverage": comp.get("skill_coverage"),
            "interview_band": s["interview_band"] if s else None,
            "decisive_unknowns": len(comp.get("decisive_unknowns") or []) if s else None,
            "night_decision": n["decision"] if n else None, "night_mode": n["mode"] if n else None, "night_state": n["state"] if n else None,
            "label": lab["label"] if lab else None, "label_date_key": _key(lab["at"]) if lab else None,
            "has_application": _flag(jid in applied), "sightings": len(seen.get(jid, [])), "source_count": len(set(seen.get(jid, []))),
            "meaningful_changes": changes.get(jid, 0), "evaluations": evaluations.get(jid, 0),
            "days_to_decision": _days(j["first_seen_at"], g["at"]) if g else None,
            "days_listed": _days(j["first_seen_at"], j["closed_at"] or j["last_seen_at"])})
    tables["fact_job_evaluation"] = rows

    rows = []
    for o in observations:   # recording order
        jid = o["job_id"] if o["job_id"] in job_by_id else None
        if o["job_id"] and jid is None:
            continue  # a sighting of an excluded job
        job = job_by_id.get(jid)
        earlier = set(seen[jid][:rank[o["rowid"]]]) if jid else set()   # sources seen before this one, in event order
        site = (leads.get(o["lead_id"]) or {}).get("site")
        rows.append({"observation_key": len(rows) + 1, "job_key": jid or NONE, "employer_key": job_employer(job) if job else NONE,
                     "ats_key": (job["ats"] if job else site) or NONE, "source_key": o["source_kind"] or NONE,
                     "evidence_level": o["evidence_level"], "observed_date_key": _key(o["observed_at"]),
                     "is_first_sighting": _flag(jid and rank[o["rowid"]] == 0),
                     "is_cross_source": _flag(earlier and o["source_kind"] not in earlier), "has_lead": _flag(o["lead_id"])})
    tables["fact_job_observation"] = rows

    board_ats = {b["mapping_id"]: b["ats"] for b in boards}
    rows = []
    for n, s in enumerate(q("SELECT * FROM source_scans ORDER BY rowid"), 1):
        row = employer_rows.get(s["employer_id"]) or {}
        board = row.get("mapping_id") or s["employer_id"]
        board = board if board in board_ids else NONE
        rows.append({"scan_key": n, "board_key": board, "employer_key": entity_key.get(row.get("entity_id"), board_employer(board)),
                     "ats_key": row.get("ats") or board_ats.get(board) or NONE, "scan_date_key": _key(s["at"]), "outcome": s["outcome"],
                     "is_complete": _flag(s["outcome"] == "complete"), "is_failure": _flag(s["outcome"] in ("failed", "parse_error")),
                     "error_code": s["error_code"], "postings": s["postings"], "requests": s["requests"], "jobs_closed": s["closed"],
                     "possibly_absent": s["possibly_absent"], "provider_degraded": _flag(s["provider_degraded"])})
    tables["fact_board_scan"] = rows

    evidence = {r["run_id"]: r for r in q("SELECT run_id, COUNT(*) c, SUM(COALESCE(points, 0)) p FROM verification_evidence GROUP BY run_id")}
    rows = []
    for n, v in enumerate(q("SELECT * FROM verification_runs ORDER BY rowid"), 1):
        board = v["mapping_id"] if v["mapping_id"] in board_ids else NONE
        ev = evidence.get(v["run_id"]) or {"c": 0, "p": 0}
        rows.append({"verification_key": n, "board_key": board, "employer_key": entity_key.get(v["entity_id"], board_employer(board)),
                     "ats_key": board_ats.get(board) or NONE, "kind": v["kind"], "started_date_key": _key(v["started_at"]),
                     "finished_date_key": _key(v["finished_at"]), "outcome": v["outcome"],
                     "is_pass": _flag(v["outcome"] in ("verified", "active", "validated")), "failure_class": v["failure_class"],
                     "trust_score": v["trust_score"], "site_requests": v["site_requests"], "ats_requests": v["ats_requests"],
                     "evidence_items": ev["c"], "evidence_points": ev["p"]})
    tables["fact_verification"] = rows

    categories = json.loads((CONFIG_DIR / "workflows" / "application.json").read_text(encoding="utf-8"))["states"]
    history: dict[str, set] = {}
    for t in q("SELECT entity_id, to_state FROM transitions WHERE entity_type = 'application'"):
        history.setdefault(t["entity_id"], set()).add(t["to_state"])
    rows = []
    for a in applications:
        job = job_by_id[a["job_id"]]
        reached = history.get(a["application_id"], set()) | {a["current_state"]}
        category = categories.get(a["current_state"])
        rows.append({"application_key": len(rows) + 1, "job_key": a["job_id"], "employer_key": job_employer(job),
                     "ats_key": job["ats"] or NONE, "role_family_key": job["family"] or NONE, "channel": a["channel"],
                     "applied_date_key": _key(a["applied_at"]), "last_change_date_key": _key(a["entered_state_at"]),
                     "current_state": a["current_state"], "state_category": category,
                     "is_open": _flag(category in ("active", "blocked")), "got_answer": _flag(reached & ANSWERED),
                     "reached_screening": _flag(reached & SCREENED), "reached_interview": _flag(reached & INTERVIEWED),
                     "reached_offer": _flag(reached & OFFERED), "is_hired": _flag("accepted" in reached),
                     "days_in_process": _days(a["applied_at"], a["entered_state_at"])})
    tables["fact_application"] = rows

    keys = sorted({v for t in tables.values() for r in t for c, v in r.items() if c.endswith("_date_key") and v})
    tables["dim_date"] = _calendar(keys[0], keys[-1]) if keys else []
    return {name: tables[name] for name in TABLES}


def _calendar(first: int, last: int) -> list[dict]:
    day, end = date(first // 10000, first // 100 % 100, first % 100), date(last // 10000, last // 100 % 100, last % 100)
    rows = []
    while day <= end:
        rows.append({"date_key": int(day.strftime("%Y%m%d")), "date": day.isoformat(), "year": day.year,
                     "quarter": (day.month - 1) // 3 + 1, "month": day.month, "month_name": day.strftime("%B"),
                     "month_start": day.replace(day=1).isoformat(), "iso_week": day.isocalendar()[1],
                     "week_start": (day - timedelta(days=day.weekday())).isoformat(), "day_of_week": day.isoweekday(),
                     "day_name": day.strftime("%A"), "is_weekend": _flag(day.weekday() >= 5)})
        day += timedelta(days=1)
    return rows


# --- measures (the same definitions as the DAX in docs/powerbi_dashboard_spec.md) --------------------------------------
def kpis(tables: dict[str, list[dict]]) -> dict:
    jobs, boards = tables["fact_job_evaluation"], [b for b in tables["dim_board"] if b["board_key"] != NONE]
    scans, apps = tables["fact_board_scan"], tables["fact_application"]
    total = lambda rows, column: sum(r[column] or 0 for r in rows)  # noqa: E731
    rate = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    scored = [j["candidate_fit"] for j in jobs if j["is_scored"] and j["candidate_fit"] is not None]
    judged = [b for b in boards if b["collection"] == "allowed" and b["verification_status"] not in ("historical", "retired")]
    new = [b for b in boards if not b["is_legacy"] and b["collection"] == "allowed"]
    return {
        "jobs_discovered": len(jobs),
        "distinct_openings": sum(1 for j in jobs if not j["is_duplicate"] and not j["is_variant"]),
        "jobs_evaluated": total(jobs, "is_evaluated"),
        "jobs_qualified": total(jobs, "is_qualified"),
        "jobs_recommended": total(jobs, "is_recommended"),
        "jobs_rejected": total(jobs, "is_rejected"),
        "jobs_manual_review": total(jobs, "is_manual_review"),
        "manual_review_rate": rate(total(jobs, "is_manual_review"), total(jobs, "is_evaluated")),
        "duplicates_prevented": total(jobs, "is_duplicate") + total(jobs, "linked_openings"),
        "cross_source_sightings": total(tables["fact_job_observation"], "is_cross_source"),
        "verified_employers": len({b["employer_key"] for b in boards if b["verification_status"] == "active"}),
        "average_fit": round(sum(scored) / len(scored), 1) if scored else None,
        "board_verification_rate": rate(sum(1 for b in judged if b["verification_status"] == "active"), len(judged)),
        "new_board_verification_rate": rate(sum(1 for b in new if b["verification_status"] in ("quarantined", "active")), len(new)),
        "scan_reliability": rate(total(scans, "is_complete"), len(scans)),
        "night_routed": sum(1 for j in jobs if j["night_decision"] == "eligible"),
        "applications": len(apps),
        "applications_open": total(apps, "is_open"),
        "interview_rate": rate(total(apps, "reached_interview"), len(apps)),
    }


# --- validation ------------------------------------------------------------------------------------------------------
def _check(checks: list, name: str, ok: bool, detail="") -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})


def validate(tables: dict[str, list[dict]]) -> list[dict]:
    """Structure: columns, keys, references, types, value ranges and internal consistency."""
    checks: list[dict] = []
    _check(checks, "every table is present with its documented columns, in order",
           list(tables) == list(TABLES) and all(list(r) == [c for c, _ in TABLES[t]] for t in TABLES for r in tables[t]),
           [t for t in TABLES if any(list(r) != [c for c, _ in TABLES[t]] for r in tables.get(t, []))])
    keys = {}
    for table, columns in TABLES.items():
        pk = columns[0][0]
        values = [r[pk] for r in tables[table]]
        keys[table] = set(values)
        _check(checks, f"{table}: the primary key {pk} is present and unique",
               all(v not in (None, "") for v in values) and len(values) == len(set(values)), f"{len(values)} rows")
    for (table, column), dim in REFERENCES.items():
        missing = sorted({str(r[column]) for r in tables[table]
                          if (r[column] not in (None, "") or (dim != "dim_date" and (table, column) not in OPTIONAL_KEYS))
                          and r[column] not in keys[dim]})
        _check(checks, f"{table}.{column} -> {dim}: every reference exists", not missing, missing[:5])
    bad = []
    for table, columns in TABLES.items():
        for column, kind in columns:
            for r in tables[table]:
                v = r[column]
                if v is None:
                    continue
                ok = (kind == "flag" and v in (0, 1)) or (kind == "int" and isinstance(v, int) and not isinstance(v, bool)) \
                    or (kind == "num" and isinstance(v, (int, float))) or (kind == "date" and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v))) \
                    or (kind in ("key", "text") and isinstance(v, str))
                if not ok:
                    bad.append(f"{table}.{column}={v!r}")
    _check(checks, "every value has its column's type (flags 0/1, whole numbers, decimals, ISO dates, text)", not bad, bad[:5])
    jobs = tables["fact_job_evaluation"]
    out_of_range = [j["job_key"] for j in jobs for c in ("eligibility_score", "candidate_fit", "career_value", "freshness", "apply_priority")
                    if j[c] is not None and not 0 <= j[c] <= 100] + \
                   [j["job_key"] for j in jobs if j["skill_coverage"] is not None and not 0 <= j["skill_coverage"] <= 1]
    _check(checks, "scores are within 0-100 and skill coverage within 0-1", not out_of_range, out_of_range[:5])
    states = {"scored": "is_scored", "rejected": "is_rejected", "duplicate": "is_duplicate", "closed": "is_closed"}
    inconsistent = [j["job_key"] for j in jobs
                    if any(j[flag] != _flag(j["current_state"] == state) for state, flag in states.items())
                    or (j["is_qualified"] and not j["is_scored"]) or (j["is_recommended"] and not j["is_qualified"])
                    or (j["is_scored"] and j["tier"] is None) or (j["is_rejected"] and not j["rejection_reason"])]
    _check(checks, "job flags agree with the job's state (scored jobs have a tier, rejections a reason)", not inconsistent, inconsistent[:5])
    dates = [r["date_key"] for r in tables["dim_date"]]
    _check(checks, "dim_date is one row per day with no gaps",
           len(dates) == len(set(dates)) and all(_next(a) == b for a, b in zip(dates, dates[1:], strict=False)), f"{len(dates)} days")
    return checks


def _next(key: int) -> int:
    day = date(key // 10000, key // 100 % 100, key % 100) + timedelta(days=1)
    return int(day.strftime("%Y%m%d"))


def reconcile(tables: dict[str, list[dict]], store, policy) -> list[dict]:
    """The export against the application's own figures: row counts, states, tiers, boards and its metrics."""
    from collections import Counter
    from careeros.review.discovery_metrics import metrics
    checks: list[dict] = []
    one = lambda sql: store.one(sql)["c"]  # noqa: E731
    included = "COALESCE(exclude_from_kpi, 0) = 0"
    for table, sql in (("fact_job_evaluation", f"SELECT COUNT(*) c FROM jobs WHERE {included}"),
                       ("fact_board_scan", "SELECT COUNT(*) c FROM source_scans"),
                       ("fact_verification", "SELECT COUNT(*) c FROM verification_runs"),
                       ("fact_application", f"SELECT COUNT(*) c FROM applications a WHERE {included} AND job_id IN "
                                            f"(SELECT job_id FROM jobs WHERE {included})"),
                       ("fact_job_observation", f"SELECT COUNT(*) c FROM observations WHERE job_id IS NULL OR job_id IN "
                                                f"(SELECT job_id FROM jobs WHERE {included})")):
        _check(checks, f"{table} has one row per source record", len(tables[table]) == one(sql), f"{len(tables[table])} vs {one(sql)}")
    _check(checks, "dim_board has every board (plus the (none) member)",
           len(tables["dim_board"]) == one("SELECT COUNT(*) c FROM board_mappings") + 1, len(tables["dim_board"]))
    jobs = tables["fact_job_evaluation"]
    app_states = {r["current_state"]: r["c"] for r in store.all(f"SELECT current_state, COUNT(*) c FROM jobs WHERE {included} GROUP BY 1")}
    _check(checks, "jobs by state match the application", dict(Counter(j["current_state"] for j in jobs)) == app_states, app_states)
    scopes = {r["remote_scope"] or "unknown": r["c"] for r in store.all(f"SELECT remote_scope, COUNT(*) c FROM jobs WHERE {included} GROUP BY 1")}
    exported = Counter(next(loc["remote_scope"] for loc in tables["dim_location"] if loc["location_key"] == j["location_key"]) for j in jobs)
    _check(checks, "remote classifications match the application", dict(exported) == scopes, scopes)
    boards = {r["verification_status"]: r["c"] for r in store.all("SELECT verification_status, COUNT(*) c FROM board_mappings GROUP BY 1")}
    _check(checks, "boards by verification status match the application",
           dict(Counter(b["verification_status"] for b in tables["dim_board"] if b["board_key"] != NONE)) == boards, boards)
    k = kpis(tables)
    m = metrics(store, policy, 36500)   # the whole history, as the export has it
    v = m["verification"]
    pairs = {"jobs_qualified": m["opportunities"]["distinct_qualified_openings"],
             "duplicates_prevented": m["opportunities"]["duplicate_reviews_avoided"],
             "verified_employers": v["active_employers"],
             "scan_reliability": v["scan_success_rate"], "new_board_verification_rate": v["share_verified"]}
    for name, expected in pairs.items():
        got = k[name]
        same = (got is None and expected is None) or (got is not None and expected is not None and
                                                      round(float(got), 2) == round(float(expected), 2))
        _check(checks, f"{name} equals the application's own figure", same, f"export {got}, application {expected}")
    return checks


def synthetic_only(tables: dict[str, list[dict]]) -> list[dict]:
    """For a Demo Mode export: nothing in it may point outside the fictional world."""
    checks: list[dict] = []
    text = json.dumps(tables)
    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)))
    _check(checks, "the only email addresses are fictional example.com ones", all(e.endswith("example.com") for e in emails), emails[:5])
    domains = [e["official_domain"] for e in tables["dim_employer"] if e["official_domain"]]
    _check(checks, "every employer website is a fictional example.com site", all(d.endswith(".example.com") for d in domains),
           [d for d in domains if not d.endswith(".example.com")][:5])
    return checks


# --- write -----------------------------------------------------------------------------------------------------------
def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(round(value, 4))
    return str(value)


def to_csv(table: str, rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    columns = [c for c, _ in TABLES[table]]
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_cell(r[c]) for c in columns])
    return ("﻿" + buffer.getvalue()).encode("utf-8")   # UTF-8 with a byte-order mark: Power BI and Excel read it as UTF-8


def export(store, policy, out_dir: Path, *, source: str) -> dict:
    """Build, validate and write the export. Writes nothing (and raises) if any check fails; replaces an earlier
    export in out_dir only when the folder holds nothing but an earlier export."""
    tables = build(store, policy)
    checks = validate(tables) + reconcile(tables, store, policy) + (synthetic_only(tables) if source == "demo" else [])
    files = {f"{t}.csv": to_csv(t, rows) for t, rows in tables.items()}
    manifest = {"schema_version": SCHEMA_VERSION, "source": source, "generated_at": store.clock.now(),
                "tables": {t: {"file": f"{t}.csv", "rows": len(rows), "sha256": hashlib.sha256(files[f"{t}.csv"]).hexdigest()}
                           for t, rows in tables.items()},
                "kpis": kpis(tables), "valid": all(c["ok"] for c in checks), "checks": checks}
    if not manifest["valid"]:
        raise ExportError("; ".join(c["check"] for c in checks if not c["ok"]))
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        known = {f"{t}.csv" for t in TABLES} | {"manifest.json"}
        if not (out_dir / "manifest.json").exists() or any(p.name not in known for p in out_dir.iterdir()):
            raise ExportError(f"{out_dir} holds other files: refusing to replace it (choose an empty folder)")
    staging = out_dir.with_name(out_dir.name + ".writing")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name, content in files.items():
        (staging / name).write_bytes(content)
    (staging / "manifest.json").write_bytes((json.dumps(manifest, indent=1) + "\n").encode("utf-8"))   # the same bytes on any OS
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    return manifest
