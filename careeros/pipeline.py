"""Phase 1 pipeline: ingest → normalize/dedupe → extract → gate → score.

Each stage uses its own component writer, and every state change goes through the job workflow matrix.
Failures on one job or board become ExceptionRecords; they never stop the run or close jobs by mistake.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from careeros.core.fingerprint import fingerprint
from careeros.core.policy import PolicyBundle
from careeros.core.transitions import Matrix
from careeros.db.store import Store
from careeros.extract import rules
from careeros.extract.model import Requirement
from careeros.normalize.posting import deadline_of, excluded_term
from careeros.normalize.posting import (RawPosting, canonical_url, classify_family, content_fingerprint, dedupe_key,
                                        detect_country, detect_remote, employment_type, norm, parse_comp, remote_scope,
                                        remote_state_rules, timezone_requirement, title_country, title_employment)
from careeros.qualify import gate as gate_mod
from careeros.qualify import scores as score_mod
from careeros.qualify.profile import Profile

OPEN = "current_state NOT IN ('duplicate','closed')"
MIN_TEXT = 300


@dataclass
class Context:
    store: Store
    policy: PolicyBundle
    profile: Profile
    matrix: Matrix
    snapshots_dir: Path | None = None
    ai: object | None = None  # careeros.extract.ai.AIReader when AI reading is enabled and a key is available
    rescue_budget: int = 0    # title-screen rescues still allowed in this processing run (Part 3)


def job_view(row) -> dict:
    job = dict(row)
    job["remote_states"] = json.loads(job.get("remote_states_json") or "[]")
    job["remote_excluded_states"] = json.loads(job.get("remote_excluded_states_json") or "[]")
    return job


# Policy sections that never change a PRIMARY decision: how CareerOS runs, and the separate NIGHT profile (which has
# its own fingerprint below). Changing them must not re-evaluate every PRIMARY job.
NOT_PRIMARY_DECISIONS = ("operations", "night", "scanning", "source_discovery", "board_verification")


def decision_fingerprint(ctx: Context) -> str:
    """Policy data + gate/scoring code versions: a change to any of them re-evaluates earlier decisions.
    The 'operations' section (notifications, backups) is excluded: it never changes a decision."""
    decisive = {k: v for k, v in ctx.policy.data.items() if k not in NOT_PRIMARY_DECISIONS}
    from careeros.qualify.title_rescue import RESCUE_VERSION  # a new rescue rule re-checks every earlier decision
    return fingerprint({"policy": fingerprint(decisive), "gate": gate_mod.RULES_VERSION, "scores": score_mod.SCORES_VERSION,
                        "rescue": RESCUE_VERSION})


def refresh_derived_fields(ctx: Context) -> int:
    """Recompute fields derived from stored posting text when the derivation code improves (state limits, an
    employment type stated in the title)."""
    store, changed = ctx.store, 0
    rows = store.all(f"SELECT job_id, location_raw, description_text, remote_type, remote_states_json, remote_excluded_states_json, "
                     f"current_state FROM jobs WHERE {OPEN} AND remote_type IN ('remote','hybrid')")
    for row in rows:
        allowed, excluded = remote_state_rules(row["location_raw"] or "", row["description_text"] or "", row["remote_type"])
        if json.dumps(allowed) == row["remote_states_json"] and json.dumps(excluded) == row["remote_excluded_states_json"]:
            continue
        with store.tx():
            store.writer("discovery").update("jobs", {"remote_states_json": json.dumps(allowed),
                                                      "remote_excluded_states_json": json.dumps(excluded)}, "job_id = ?", (row["job_id"],))
            if row["current_state"] not in ("discovered", "normalized"):
                store.writer("discovery").transition(ctx.matrix, row["job_id"], "normalized", reason_code="derived_fields_refreshed")
        changed += 1
    for row in store.all("SELECT job_id, remote_type, country, remote_states_json, remote_excluded_states_json, description_text "
                         "FROM jobs WHERE remote_scope IS NULL"):  # 2026-10-13: descriptive only, so no re-evaluation
        store.writer("discovery").update("jobs", {
            "remote_scope": remote_scope(row["remote_type"], row["country"], json.loads(row["remote_states_json"] or "[]"),
                                         json.loads(row["remote_excluded_states_json"] or "[]"), row["description_text"] or ""),
            "timezone_requirement": timezone_requirement(row["description_text"] or "")}, "job_id = ?", (row["job_id"],))
    for row in store.all(f"SELECT job_id, title, employment_type, country, current_state FROM jobs WHERE {OPEN}"):
        values = {}
        stated = title_employment(row["title"])
        if stated and stated != row["employment_type"]:
            values["employment_type"] = stated
        if title_country(row["title"]) == "non_US" and row["country"] != "non_US":
            values["country"] = "non_US"
        if not values:
            continue
        with store.tx():
            store.writer("discovery").update("jobs", values, "job_id = ?", (row["job_id"],))
            if row["current_state"] not in ("discovered", "normalized"):
                store.writer("discovery").transition(ctx.matrix, row["job_id"], "normalized", reason_code="derived_fields_refreshed")
        changed += 1
    return changed


# --- ingest ------------------------------------------------------------------------------------------
def normalized_fields(ctx: Context, employer: dict | None, p: RawPosting) -> dict:
    text = p.description_text or ""
    remote = detect_remote(p.location, p.workplace_hint, text)
    comp = parse_comp(text, p.comp) or {}
    name = (employer or {}).get("name") or p.board_token
    parent = (employer or {}).get("parent") or name
    title_norm = norm(p.title)
    allowed, excluded = remote_state_rules(p.location, text, remote)
    country = "non_US" if title_country(p.title) == "non_US" else detect_country(p.location, text, remote)
    fields = {
        "employer_id": (employer or {}).get("employer_id"), "employer_name": name, "parent": parent,
        "requisition_id": p.requisition_id, "title": p.title, "title_norm": title_norm,
        "family": classify_family(title_norm, ctx.policy.get("families", default={})),
        "location_raw": p.location, "remote_type": remote,
        "remote_states_json": json.dumps(allowed), "remote_excluded_states_json": json.dumps(excluded),
        "remote_scope": remote_scope(remote, country, allowed, excluded, text), "timezone_requirement": timezone_requirement(text),
        "country": country, "employment_type": employment_type(p.employment_type_hint, text, p.title),
        "comp_min": comp.get("min"), "comp_max": comp.get("max"), "comp_period": comp.get("period"),
        "comp_currency": comp.get("currency"), "posted_at": p.posted_at, "url": p.url, "apply_url": p.apply_url,
        "canonical_url": canonical_url(p.url), "description_text": text, "dedupe_key": dedupe_key(parent, title_norm, p.location),
        "department": (p.department or "")[:200] or None, "source_updated_at": p.updated_at, "deadline_at": deadline_of(text),
    }
    fields["content_fingerprint"] = content_fingerprint(fields)  # department / dates are not content: never "changed"
    return fields


def _snapshot(ctx: Context, job_id: str, fields: dict, raw: dict) -> None:
    store = ctx.store
    if store.one("SELECT 1 FROM job_snapshots WHERE job_id = ? AND content_fingerprint = ?", (job_id, fields["content_fingerprint"])):
        return
    path = None
    if ctx.snapshots_dir:
        day = store.clock.now()[:10]
        folder = Path(ctx.snapshots_dir) / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', job_id)}__{fields['content_fingerprint'][:12]}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    store.writer("discovery").insert("job_snapshots", {"job_id": job_id, "content_fingerprint": fields["content_fingerprint"],
                                                        "captured_at": store.clock.now(), "raw_path": str(path) if path else None})


def _duplicate_of(store: Store, job_id: str, fields: dict) -> tuple[str, str, bool] | None:
    """(reason, other job, is_duplicate). Same opening in the same place = duplicate; same requisition in another
    location, or same title/location with different text = variant (kept, evaluated, linked)."""
    if fields.get("requisition_id") and fields.get("employer_id"):
        other = store.one(f"SELECT job_id, location_raw FROM jobs WHERE employer_id = ? AND requisition_id = ? AND job_id != ? "
                          f"AND {OPEN} ORDER BY first_seen_at LIMIT 1", (fields["employer_id"], fields["requisition_id"], job_id))
        if other:
            same_place = norm(other["location_raw"]) == norm(fields.get("location_raw"))
            return ("same_requisition", other["job_id"], True) if same_place else ("same_requisition_other_location", other["job_id"], False)
    if fields.get("canonical_url"):
        other = store.one(f"SELECT job_id FROM jobs WHERE canonical_url = ? AND job_id != ? AND {OPEN}", (fields["canonical_url"], job_id))
        if other:
            return "same_url", other["job_id"], True
    other = store.one(f"SELECT job_id, description_text, requisition_id FROM jobs WHERE dedupe_key = ? AND job_id != ? AND {OPEN} "
                      f"ORDER BY first_seen_at LIMIT 1", (fields["dedupe_key"], job_id))
    if other:
        if fields.get("requisition_id") and other["requisition_id"] and fields["requisition_id"] != other["requisition_id"]:
            # identical titles, different requisitions: two vacancies. Linked for your review, never merged.
            return "different_requisition_same_title", other["job_id"], None
        if norm(other["description_text"]) == norm(fields.get("description_text")):
            return "same_posting_text", other["job_id"], True
        return "same_title_location_different_text", other["job_id"], False
    return None


def _observe_posting(ctx: Context, job_id: str, p: RawPosting, fields: dict) -> None:
    from careeros.openings import record_observation
    record_observation(ctx.store, source_kind="ats_scan" if p.ats != "manual" else "manual_import",
                       evidence_level="live_ats" if p.ats != "manual" else "human", job_id=job_id, url=p.url,
                       canonical=fields.get("canonical_url"), key=f"{p.ats}:{p.board_token}:{p.external_id}",
                       requisition_id=p.requisition_id, title=p.title, company=fields.get("employer_name"),
                       location=p.location, published_at=p.posted_at, updated_at=p.updated_at,
                       facts={k: fields.get(k) for k in ("comp_min", "comp_max", "comp_period", "remote_type", "employment_type")})


def ingest(ctx: Context, employer: dict | None, postings: list[RawPosting], *, source: str = "ats_api",
           full_board: bool = True) -> Counter:
    store, counts, seen = ctx.store, Counter(), set()
    disc = store.writer("discovery")
    for p in postings:
        job_id = f"{p.ats}:{p.board_token}:{p.external_id}"
        seen.add(job_id)
        try:
            with store.tx():
                fields = normalized_fields(ctx, employer, p)
                now = store.clock.now()
                existing = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
                if existing is None:
                    disc.insert("jobs", {"job_id": job_id, "workflow_id": f"job:{job_id}", "source": source, "ats": p.ats,
                                         "external_id": p.external_id, "first_seen_at": now, "last_seen_at": now,
                                         "current_state": ctx.matrix.initial_state, "state_sequence": 0, "entered_state_at": now,
                                         **fields})
                    _snapshot(ctx, job_id, fields, p.raw)
                    _observe_posting(ctx, job_id, p, fields)
                    disc.event("job_discovered", outcome="success", entity_type="job", entity_id=job_id,
                               metadata={"source": source, "ats": p.ats, "board": p.board_token})
                    match = _duplicate_of(store, job_id, fields)
                    if match and match[2] is None:
                        from careeros.openings import link
                        link(store, job_id, match[1], "possible_duplicate", match[0])
                        counts["possible_duplicate"] += 1
                        match = None
                    if match and match[2]:
                        reason, other, _ = match
                        disc.update("jobs", {"duplicate_of": other}, "job_id = ?", (job_id,))
                        disc.transition(ctx.matrix, job_id, "duplicate", reason_code=reason, notes=f"duplicate of {other}")
                        counts["duplicate"] += 1
                        continue
                    if match:
                        disc.update("jobs", {"variant_of": match[1]}, "job_id = ?", (job_id,))
                        disc.event("job_variant_linked", outcome="informational", entity_type="job", entity_id=job_id,
                                   metadata={"variant_of": match[1], "reason": match[0]})
                        counts["variant"] += 1
                    repost = store.one("SELECT job_id FROM jobs WHERE dedupe_key = ? AND job_id != ? AND current_state = 'closed' "
                                       "ORDER BY closed_at DESC LIMIT 1", (fields["dedupe_key"], job_id))
                    if repost:
                        from careeros.openings import link
                        disc.update("jobs", {"repost_of": repost["job_id"]}, "job_id = ?", (job_id,))
                        link(store, job_id, repost["job_id"], "possible_repost", "same title and location as a closed posting")
                        counts["repost"] += 1
                    disc.transition(ctx.matrix, job_id, "normalized")
                    counts["new"] += 1
                elif existing["current_state"] == "closed" and full_board:
                    # listed again on a complete board scan: reopened (a meaningful change), never silently
                    from careeros.openings import record_change
                    disc.update("jobs", {**fields, "last_seen_at": now, "closed_at": None, "absent_scans": 0}, "job_id = ?", (job_id,))
                    record_change(store, job_id, [], extra="reopened: listed again after being closed")
                    disc.transition(ctx.matrix, job_id, "normalized", reason_code="reopened")
                    counts["reopened"] += 1
                elif existing["current_state"] in ("duplicate", "closed"):
                    disc.update("jobs", {"last_seen_at": now}, "job_id = ?", (job_id,))
                    counts["inactive_seen"] += 1
                elif existing["content_fingerprint"] != fields["content_fingerprint"]:
                    from careeros.openings import meaningful_changes, record_change
                    changes = meaningful_changes(existing, fields)
                    disc.update("jobs", {**fields, "last_seen_at": now, "absent_scans": 0}, "job_id = ?", (job_id,))
                    _snapshot(ctx, job_id, fields, p.raw)
                    _observe_posting(ctx, job_id, p, fields)
                    record_change(store, job_id, changes)
                    if existing["current_state"] not in ("discovered", "normalized"):
                        disc.transition(ctx.matrix, job_id, "normalized",
                                        reason_code="posting_changed" if changes else "posting_edited_cosmetic")
                    counts["changed" if changes else "edited_cosmetic"] += 1
                else:
                    extra = {k: fields[k] for k in ("department", "source_updated_at", "deadline_at", "remote_scope",
                                                     "timezone_requirement") if existing[k] != fields[k]}
                    disc.update("jobs", {"last_seen_at": now, "absent_scans": 0, **extra}, "job_id = ?", (job_id,))
                    counts["unchanged"] += 1
        except Exception as exc:  # one bad posting never stops the run
            disc.exception("data_quality", "medium", f"ingest failed: {type(exc).__name__}: {exc}", entity_type="job",
                           entity_id=job_id, error_code="ingest_failed")
            counts["errors"] += 1

    if full_board and employer:
        # Possible absence first, confirmed closure only after `close_after_missed_scans` complete scans in a row.
        # Callers pass full_board=False for failed, partial or suspicious scans, so those never count.
        needed = int(ctx.policy.get("scanning", "close_after_missed_scans", default=2) or 2)
        stale = store.all(f"SELECT job_id, absent_scans FROM jobs WHERE employer_id = ? AND source = 'ats_api' AND {OPEN}",
                          (employer["employer_id"],))
        for row in stale:
            if row["job_id"] not in seen:
                missed = (row["absent_scans"] or 0) + 1
                with store.tx():
                    if missed >= needed:
                        disc.update("jobs", {"closed_at": store.clock.now(), "absent_scans": missed}, "job_id = ?", (row["job_id"],))
                        disc.transition(ctx.matrix, row["job_id"], "closed", reason_code="no_longer_listed")
                        counts["closed"] += 1
                    else:
                        disc.update("jobs", {"absent_scans": missed}, "job_id = ?", (row["job_id"],))
                        counts["possibly_absent"] += 1
    return counts


# --- process -----------------------------------------------------------------------------------------
def _extraction_fingerprint(ctx: Context, job, use_ai: bool | None = None) -> str:
    vocab = {"aliases": ctx.policy.get("skill_aliases"), "domains": ctx.policy.get("domain_terms"),
             "certifications": ctx.policy.get("certifications")}   # 2026-10-15: a catalog change re-reads postings
    use_ai = (ctx.ai is not None) if use_ai is None else use_ai
    extractor = f"{rules.EXTRACTOR_ID}|{ctx.ai.extractor_id}" if use_ai else rules.EXTRACTOR_ID
    return fingerprint({"extractor": extractor, "content": job["content_fingerprint"], "vocab": fingerprint(vocab)})


def _ai_calls_today(store: Store) -> int:
    return store.one("SELECT COUNT(*) c FROM events WHERE event_type = 'ai_extraction_call' AND substr(occurred_at, 1, 10) = ?",
                     (store.clock.now()[:10],))["c"]


def _read_requirements(ctx: Context, job):
    """(extraction, fingerprint). AI reading when available and under the daily cap; rules otherwise."""
    from careeros.extract.ai import AIUnavailable
    rules_extraction = rules.extract(job["description_text"], ctx.policy)
    cap = ctx.policy.get("extraction", "ai", "max_calls_per_day", default=100)
    if ctx.ai is None or _ai_calls_today(ctx.store) >= cap:
        return rules_extraction, _extraction_fingerprint(ctx, job, use_ai=False)
    writer = ctx.store.writer("extract")
    try:
        extraction, meta = ctx.ai.extract(job["title"], job["description_text"], rules_extraction)
    except AIUnavailable as exc:
        writer.event("ai_extraction_call", outcome="failure", entity_type="job", entity_id=job["job_id"],
                     metadata={"error": str(exc), "model": ctx.ai.model})
        writer.exception("integration", "low", f"AI requirement reading unavailable ({exc}); rules used",
                         entity_type="job", entity_id=job["job_id"], error_code=f"ai_{exc}", retryable=True)
        return rules_extraction, _extraction_fingerprint(ctx, job, use_ai=False)
    writer.event("ai_extraction_call", outcome="success", entity_type="job", entity_id=job["job_id"],
                 metadata={**meta, "records": len(extraction.requirements), "notes": extraction.notes})
    return extraction, _extraction_fingerprint(ctx, job, use_ai=True)


def load_requirements(store: Store, job) -> list[Requirement]:
    rows = store.all("SELECT * FROM requirements WHERE job_id = ? AND extraction_fingerprint = ? ORDER BY req_id",
                     (job["job_id"], job["extraction_fingerprint"]))
    return [Requirement(r["category"], r["name"], r["requirement_type"], r["source_text"], r["confidence"], r["years"]) for r in rows]


TITLE_SCREEN = "title_screen"


def _rescuable(ctx: Context, job) -> bool:
    """Worth a second review: an ambiguous title, and nothing else in the cheap screen (remote / US / manager level /
    a title you never want) would reject it anyway. A live run on 2026-10-12 showed early rescues failing exactly there."""
    from careeros.normalize.posting import seniority
    from careeros.qualify import title_rescue
    if not title_rescue.prefilter(job["title_norm"], ctx.policy) or title_rescue.skipped_title(job["title_norm"], ctx.policy):
        return False
    if ctx.policy.get("location", "remote_only") and job["remote_type"] in ("onsite", "hybrid"):
        return False
    return job["country"] != "non_US" and seniority(job["title_norm"], ctx.policy.get("seniority_terms", default={})) != "manager"


def _second_review(ctx: Context, job) -> str | None:
    """Part 3: a recorded, budgeted second look at an ambiguous title that matched no family."""
    from careeros.qualify import title_rescue
    if not _rescuable(ctx, job):
        return None
    done = ctx.store.one("SELECT outcome, family FROM title_reviews WHERE job_id = ? AND rules_version = ?",
                         (job["job_id"], title_rescue.RESCUE_VERSION))
    if done:
        return done["family"] if done["outcome"] == "rescued" else None
    decision = title_rescue.review(job_view(job), ctx.policy)
    if decision["outcome"] == "rescued" and ctx.rescue_budget <= 0:
        return None  # over this run's rescue budget: no record, so a later run reviews it again
    ctx.store.writer("discovery").insert("title_reviews", {
        "job_id": job["job_id"], "rules_version": title_rescue.RESCUE_VERSION, "reviewed_at": ctx.store.clock.now(),
        "outcome": decision["outcome"], "family": decision["family"], "rule": decision["rule"],
        "evidence": decision["evidence"][:400], "original_reason": "no_target_family"})
    if decision["outcome"] == "rescued":
        ctx.rescue_budget -= 1
        return decision["family"]
    return None


def _rescue_pass(ctx: Context) -> int:
    """Earlier title rejects that never had a second review (or were over budget) go back through the title screen,
    at most `max_rescue_reviews_per_run` per run."""
    from careeros.qualify import title_rescue
    limit = int((ctx.policy.get("title_rescue", default={}) or {}).get("max_rescue_reviews_per_run", 200))
    if not (ctx.policy.get("title_rescue", default={}) or {}).get("enabled") or limit <= 0:
        return 0
    moved = 0
    rows = ctx.store.all("""SELECT j.* FROM jobs j WHERE j.current_state = 'rejected' AND j.family IS NULL
                            AND NOT EXISTS (SELECT 1 FROM title_reviews r WHERE r.job_id = j.job_id AND r.rules_version = ?)
                            AND (SELECT reason_code FROM transitions t WHERE t.entity_id = j.job_id ORDER BY sequence DESC LIMIT 1)
                                = 'no_target_family' ORDER BY j.first_seen_at""", (title_rescue.RESCUE_VERSION,))
    for job in rows:
        if moved >= limit:
            break
        if _rescuable(ctx, job):  # the same test as the review itself, so nothing bounces back and forth
            ctx.store.writer("discovery").transition(ctx.matrix, job["job_id"], "normalized", reason_code="title_second_review")
            moved += 1
    return moved


def _title_screen(ctx: Context, job) -> str:
    """Cheap first pass: titles outside every target family (or on the exclude list) are rejected before extraction.
    An ambiguous title may still be rescued by the recorded second review (Part 3)."""
    family = classify_family(job["title_norm"], ctx.policy.get("families", default={}))
    if not family and not excluded_term(job["title_norm"], ctx.policy.get("title_exclude", default=[])) \
            and not gate_mod.excluded_employer(dict(job), ctx.policy):
        family = _second_review(ctx, job)
    if family != job["family"]:  # families come from the current policy, never a stale value saved at discovery
        ctx.store.writer("discovery").update("jobs", {"family": family}, "job_id = ?", (job["job_id"],))
    term = excluded_term(job["title_norm"], ctx.policy.get("title_exclude", default=[]))
    if gate_mod.excluded_employer(dict(job), ctx.policy):
        failure = {"code": "employer_excluded", "detail": "employer is on your exclusion list"}
    elif term:
        failure = {"code": "title_excluded", "detail": f"title contains excluded term '{term}'"}
    elif not family:
        failure = {"code": "no_target_family", "detail": "title does not match any target job family"}
    elif gate_mod.generic_title_check(dict(job), ctx.policy) == "no_context":
        failure = {"code": "generic_title_no_context",
                   "detail": "generic title with no claims/healthcare/workflow context in the posting"}
    elif ctx.policy.get("gate", "require_us") and job["country"] == "non_US":
        failure = {"code": "outside_us", "detail": f"location '{job['location_raw']}' is outside the US"}
    elif ctx.policy.get("location", "remote_only") and job["remote_type"] in ("onsite", "hybrid"):
        failure = {"code": "not_remote", "detail": f"{job['remote_type']} role in '{job['location_raw']}'; remote only"}
    else:
        failure = None
    if failure is None:
        return ""
    store, writer = ctx.store, ctx.store.writer("qualify")
    with store.tx():
        writer.insert("gate_results", {"result_id": f"gate:{uuid.uuid4().hex}", "job_id": job["job_id"], "at": store.clock.now(),
                                       "policy_fingerprint": decision_fingerprint(ctx), "profile_fingerprint": ctx.profile.fingerprint,
                                       "extraction_fingerprint": TITLE_SCREEN, "decision": "rejected",
                                       "hard_failures_json": json.dumps([{**failure, "source_text": job["title"]}]), "flags_json": "[]"})
        writer.transition(ctx.matrix, job["job_id"], "rejected", reason_code=failure["code"], notes=failure["detail"])
    return "rejected_by_title"


def _record_review(ctx: Context, job, code: str, detail: str) -> None:
    """A 'needs review' outcome is a decision like any other: recorded with the policy and profile it was made under,
    so a later rule or policy change re-checks it (it used to be skipped by re-evaluation and could stay parked)."""
    ctx.store.writer("qualify").insert("gate_results", {
        "result_id": f"gate:{uuid.uuid4().hex}", "job_id": job["job_id"], "at": ctx.store.clock.now(),
        "policy_fingerprint": decision_fingerprint(ctx), "profile_fingerprint": ctx.profile.fingerprint,
        "extraction_fingerprint": job["extraction_fingerprint"] or "unread", "decision": "needs_review",
        "hard_failures_json": "[]", "flags_json": json.dumps([{"code": code, "detail": detail}])})


def _extract_one(ctx: Context, job) -> str:
    store, writer = ctx.store, ctx.store.writer("extract")
    screened = _title_screen(ctx, job)
    if screened:
        return screened
    if len(job["description_text"] or "") < MIN_TEXT:
        with store.tx():
            _record_review(ctx, job, "posting_text_too_short", "paste the full posting text with `careeros add-url --text-file`")
            writer.transition(ctx.matrix, job["job_id"], "needs_review", reason_code="posting_text_too_short",
                              notes="paste the full posting text with `careeros add-url --text-file`")
        return "needs_review"
    fp = _extraction_fingerprint(ctx, job)
    extraction = None
    if not store.one("SELECT 1 FROM requirements WHERE job_id = ? AND extraction_fingerprint = ?", (job["job_id"], fp)):
        extraction, fp = _read_requirements(ctx, job)  # network call happens outside the database transaction
    # Only a re-read caused by an edit to the posting can be a requirement change (a reader upgrade re-reads everything)
    edited = store.one("SELECT reason_code FROM transitions WHERE entity_id = ? ORDER BY sequence DESC LIMIT 1", (job["job_id"],))
    previous = store.all("SELECT category, name, requirement_type, years FROM requirements WHERE job_id = ? AND extraction_fingerprint = ?",
                         (job["job_id"], job["extraction_fingerprint"])) if job["extraction_fingerprint"] else []
    with store.tx():
        if extraction is not None and not store.one("SELECT 1 FROM requirements WHERE job_id = ? AND extraction_fingerprint = ?",
                                                    (job["job_id"], fp)):
            now = store.clock.now()
            for index, req in enumerate(extraction.requirements):
                writer.insert("requirements", {"req_id": f"{job['job_id']}:{fp[:12]}:{index:03d}", "job_id": job["job_id"],
                                               "extraction_fingerprint": fp, "extractor": extraction.extractor, "created_at": now,
                                               **req.as_dict()})
            if previous and edited and edited["reason_code"] in ("posting_changed", "posting_edited_cosmetic"):
                from careeros.openings import describe_requirement_change, record_change, requirement_digest
                change = describe_requirement_change(requirement_digest(previous), requirement_digest(
                    [{"category": r.category, "name": r.name, "requirement_type": r.requirement_type, "years": r.years}
                     for r in extraction.requirements]))
                if change:
                    record_change(store, job["job_id"], [], extra=change)
            writer.update("jobs", {"extraction_fingerprint": fp, "requirements_fingerprint": extraction.requirements_fingerprint()},
                          "job_id = ?", (job["job_id"],))
        else:
            writer.update("jobs", {"extraction_fingerprint": fp}, "job_id = ?", (job["job_id"],))
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],))
        reqs = load_requirements(store, job)
        material = [r for r in reqs if r.requirement_type != "responsibility"]
        if len(material) < ctx.policy.get("extraction", "min_requirements", default=2):
            _record_review(ctx, job, "requirements_unclear", f"only {len(material)} requirement(s) could be read")
            writer.transition(ctx.matrix, job["job_id"], "needs_review", reason_code="requirements_unclear",
                              notes=f"only {len(material)} requirement(s) could be read")
            return "needs_review"
        writer.transition(ctx.matrix, job["job_id"], "extracted")
    return "extracted"


def _current_family(ctx: Context, job) -> str | None:
    """The job's family under the CURRENT policy and rescue rules: a family stored by an older policy or an older rescue
    version never survives a re-evaluation (a rule change must be able to take a rescue back)."""
    from careeros.qualify.title_rescue import RESCUE_VERSION
    family = classify_family(job["title_norm"], ctx.policy.get("families", default={}))
    if family:
        return family
    row = ctx.store.one("SELECT family FROM title_reviews WHERE job_id = ? AND rules_version = ? AND outcome = 'rescued'",
                        (job["job_id"], RESCUE_VERSION))
    return row["family"] if row else None


def _gate_one(ctx: Context, job) -> str:
    store, writer = ctx.store, ctx.store.writer("qualify")
    family = _current_family(ctx, job)
    if family != job["family"]:
        store.writer("discovery").update("jobs", {"family": family}, "job_id = ?", (job["job_id"],))
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],))
    result = gate_mod.evaluate(job_view(job), load_requirements(store, job), ctx.profile, ctx.policy)
    with store.tx():
        writer.insert("gate_results", {"result_id": f"gate:{uuid.uuid4().hex}", "job_id": job["job_id"], "at": store.clock.now(),
                                       "policy_fingerprint": decision_fingerprint(ctx), "profile_fingerprint": ctx.profile.fingerprint,
                                       "extraction_fingerprint": job["extraction_fingerprint"], "decision": result.decision,
                                       "hard_failures_json": json.dumps(result.hard_failures), "flags_json": json.dumps(result.flags)})
        if result.decision == "rejected":
            writer.transition(ctx.matrix, job["job_id"], "rejected", reason_code=result.hard_failures[0]["code"],
                              notes="; ".join(f["detail"] for f in result.hard_failures)[:500])
        else:
            writer.transition(ctx.matrix, job["job_id"], "eligible")
    return result.decision


def _score_one(ctx: Context, job) -> str:
    store, writer = ctx.store, ctx.store.writer("qualify")
    gate_row = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job["job_id"],))
    result = gate_mod.GateResult(gate_row["decision"], json.loads(gate_row["hard_failures_json"]), json.loads(gate_row["flags_json"]))
    card = score_mod.score(job_view(job), load_requirements(store, job), ctx.profile, result, ctx.policy, store.clock.now())
    first_qualified = card.tier in ("A", "B", "near_miss") and not store.one(
        "SELECT 1 FROM scores WHERE job_id = ? AND tier IN ('A','B','near_miss') LIMIT 1", (job["job_id"],))
    if first_qualified and job["employer_id"]:  # a distinct qualified arrival keeps its board on the daily scan
        store.writer("discovery").update("employers", {"last_qualified_at": store.clock.now()}, "employer_id = ?", (job["employer_id"],))
    with store.tx():
        writer.insert("scores", {"score_id": f"score:{uuid.uuid4().hex}", "job_id": job["job_id"], "at": store.clock.now(),
                                 "policy_fingerprint": decision_fingerprint(ctx), "profile_fingerprint": ctx.profile.fingerprint,
                                 "eligibility": card.eligibility, "candidate_fit": card.candidate_fit, "career_value": card.career_value,
                                 "freshness": card.freshness, "apply_priority": card.apply_priority, "tier": card.tier,
                                 "interview_band": card.interview_band, "components_json": json.dumps(card.components),
                                 "state": card.state})
        writer.transition(ctx.matrix, job["job_id"], "scored")
    return card.tier


def close_open_jobs(ctx: Context, employer_id: str, reason_code: str, notes: str | None = None, *,
                    trigger_type: str = "system", trigger_id: str | None = None) -> tuple[int, int]:
    """Close every open job from one board that left the scan (retired after a move, or rejected by you). History is
    kept and a later listing on a scanned board can reopen them. A job that cannot be closed is recorded as an
    exception, never skipped silently. Returns (closed, failed)."""
    store, closed, failed = ctx.store, 0, 0
    for job in store.all(f"SELECT job_id FROM jobs WHERE employer_id = ? AND {OPEN}", (employer_id,)):
        try:
            with store.tx():
                store.writer("discovery").update("jobs", {"closed_at": store.clock.now()}, "job_id = ?", (job["job_id"],))
                store.writer("discovery").transition(ctx.matrix, job["job_id"], "closed", reason_code=reason_code, notes=notes,
                                                     trigger_type=trigger_type, trigger_id=trigger_id)
            closed += 1
        except Exception as exc:  # noqa: BLE001 - recorded for you to see, never silent
            store.writer("discovery").exception("data_quality", "medium", f"job not closed with its board: {type(exc).__name__}: {exc}",
                                                entity_type="job", entity_id=job["job_id"], error_code="board_job_close_failed")
            failed += 1
    return closed, failed


def _reevaluate(ctx: Context) -> int:
    """Jobs judged under an older policy or profile go back through the gate (append-only history keeps the old result)."""
    store, writer, moved = ctx.store, ctx.store.writer("qualify"), 0
    rows = store.all("""SELECT j.*, g.policy_fingerprint AS gp, g.profile_fingerprint AS gpr FROM jobs j
                        JOIN gate_results g ON g.result_id = (SELECT result_id FROM gate_results WHERE job_id = j.job_id
                                                              ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state IN ('scored','rejected','eligible','needs_review')""")
    for row in rows:
        reason = "policy_or_rules_changed" if row["gp"] != decision_fingerprint(ctx) else "profile_changed" if row["gpr"] != ctx.profile.fingerprint else None
        if reason:
            # Title-screened jobs were never extracted, and a job parked for review is re-read from the start (the reading
            # rules may be what changed), so both go back through screening and extraction.
            target = "normalized" if row["extraction_fingerprint"] is None or row["current_state"] == "needs_review" else "extracted"
            writer.transition(ctx.matrix, row["job_id"], target, reason_code=reason)
            moved += 1
    for row in store.all("SELECT job_id FROM jobs j WHERE current_state = 'needs_review' AND NOT EXISTS "
                         "(SELECT 1 FROM gate_results g WHERE g.job_id = j.job_id AND g.decision = 'needs_review')"):
        # parked before review decisions were recorded (2026-10-13): re-checked once, recorded from then on
        writer.transition(ctx.matrix, row["job_id"], "normalized", reason_code="review_rechecked")
        moved += 1
    # A job whose latest decision came from the title screen never used its extraction, so a new reading vocabulary
    # cannot change it. Sending it back anyway re-rejected it on every run (Functional MVP gate, 2026-10-13).
    vocab_stale = store.all("""SELECT j.* FROM jobs j WHERE j.current_state IN ('scored','rejected','eligible','needs_review','extracted')
                               AND j.extraction_fingerprint IS NOT NULL AND NOT EXISTS (SELECT 1 FROM gate_results g WHERE g.result_id =
                               (SELECT result_id FROM gate_results WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                               AND g.extraction_fingerprint = ?)""", (TITLE_SCREEN,))
    for job in vocab_stale:
        if job["extraction_fingerprint"] != _extraction_fingerprint(ctx, job):
            store.writer("extract").transition(ctx.matrix, job["job_id"], "normalized", reason_code="extraction_vocabulary_changed")
            moved += 1
    return moved


def _night_fingerprint(ctx: Context, job) -> str:
    from careeros.qualify import night
    return fingerprint({"v": night.NIGHT_VERSION, "rules": gate_mod.RULES_VERSION, "policy": decision_fingerprint(ctx),
                        "night": fingerprint(ctx.policy.data.get("night") or {}),
                        "profile": ctx.profile.fingerprint, "content": job["content_fingerprint"],
                        "derived": [job["employment_type"], job["remote_type"], job["remote_states_json"],
                                    job["remote_excluded_states_json"], job["country"], job["comp_max"]],
                        "applied": _has_application(ctx, job["job_id"])})


def _has_application(ctx: Context, job_id: str) -> bool:
    """An open application anywhere in the job's role group (a right-to-represent check for NIGHT Supplemental)."""
    from careeros.review.applications import OPEN_STATES, role_group
    group = role_group(ctx.store, job_id)
    if not group:
        return False
    marks = ",".join("?" * len(group))
    return ctx.store.one(f"SELECT 1 FROM applications WHERE job_id IN ({marks}) AND current_state IN "
                         f"({','.join('?' * len(OPEN_STATES))}) LIMIT 1", (*group, *OPEN_STATES)) is not None


def night_process(ctx: Context) -> Counter:
    """NIGHT assessments for open jobs whose posting, policy or profile changed since their last assessment. Only
    the NIGHT table is written: PRIMARY states, gate results and scores are never touched."""
    from careeros.qualify import night
    counts = Counter()
    if not night.enabled(ctx.policy):
        return counts
    store, writer = ctx.store, ctx.store.writer("qualify")
    latest = {r["job_id"]: r["fingerprint"] for r in store.all(
        """SELECT a.job_id, a.fingerprint FROM night_results a WHERE a.result_id =
           (SELECT result_id FROM night_results WHERE job_id = a.job_id ORDER BY at DESC, rowid DESC LIMIT 1)""")}
    for job in store.all(f"SELECT * FROM jobs WHERE {OPEN}"):
        try:  # one bad job never stops the other NIGHT assessments
            fp = _night_fingerprint(ctx, job)
            if latest.get(job["job_id"]) == fp:
                continue
            view = job_view(job)
            quick = night.screen(view, ctx.profile, ctx.policy)
            if quick.decision == "rejected":  # cheap rejection first: no need to read requirements
                out = {"decision": "rejected", "state": "INELIGIBLE", "mode": None, "failures": quick.hard_failures,
                       "flags": quick.flags, "family": None, "schedule": None, "phone": None, "priority": None, "tier": None,
                       "dims": {}}
            else:
                requirements = rules.extract(job["description_text"] or "", ctx.policy).requirements
                fresh = score_mod.freshness(view, store.clock.now(), ctx.policy)["score"]
                out = night.assess(view, requirements, ctx.profile, ctx.policy, fresh,
                                   application_conflict=_has_application(ctx, job["job_id"]))
            writer.insert("night_results", {
                "result_id": f"night:{uuid.uuid4().hex}", "job_id": job["job_id"], "at": store.clock.now(), "fingerprint": fp,
                "decision": out["decision"], "state": out["state"], "mode": out["mode"], "tier": out["tier"],
                "priority": out["priority"], "family": out["family"],
                "schedule": out["schedule"], "phone": out["phone"], "failures_json": json.dumps(out["failures"]),
                "flags_json": json.dumps(out["flags"]),
                "components_json": json.dumps({"dims": out["dims"], "evidence": out.get("evidence", {}),
                                               "workload": out.get("workload"), "coi": out.get("coi")})})
            counts[f"night_{out['mode'] + '_' if out['mode'] else ''}{out['tier'] or out['decision']}"] += 1
        except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
            writer.exception("system", "medium", f"NIGHT assessment failed: {type(exc).__name__}: {exc}", entity_type="job",
                             entity_id=job["job_id"], error_code="night_assess_failed")
            counts["night_errors"] += 1
    return counts


def process(ctx: Context) -> Counter:
    store, counts = ctx.store, Counter()
    ctx.rescue_budget = int((ctx.policy.get("title_rescue", default={}) or {}).get("max_rescues_per_run", 10))
    counts["refreshed"] = refresh_derived_fields(ctx)
    counts["reevaluated"] = _reevaluate(ctx)
    counts["second_review_queued"] = _rescue_pass(ctx)
    stages = [("normalized", _extract_one), ("extracted", _gate_one), ("eligible", _score_one)]
    for state, step in stages:
        for job in store.all("SELECT * FROM jobs WHERE current_state = ? ORDER BY first_seen_at", (state,)):
            try:
                counts[f"{state}->{step(ctx, job)}"] += 1
            except Exception as exc:
                store.writer("qualify").exception("system", "medium", f"{step.__name__} failed: {type(exc).__name__}: {exc}",
                                                  entity_type="job", entity_id=job["job_id"], error_code=step.__name__)
                counts["errors"] += 1
    try:
        counts.update(night_process(ctx))
    except Exception as exc:  # NIGHT problems never stop PRIMARY processing
        store.writer("qualify").exception("system", "medium", f"night_process failed: {type(exc).__name__}: {exc}",
                                          error_code="night_process")
        counts["errors"] += 1
    return counts
