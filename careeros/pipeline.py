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
from careeros.db.store import Store, TransactionLost
from careeros.extract import rules
from careeros.extract.model import Requirement
from careeros.normalize.posting import deadline_of, excluded_term
from careeros.normalize.posting import (RawPosting, canonical_url, classify_family, content_fingerprint, dedupe_key,
                                        detect_remote, employment_type, norm, parse_comp, remote_scope,
                                        remote_state_rules, timezone_requirement, placed_country, title_employment)
from careeros.normalize.sanitize import canonical, canonical_cut
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
# its own fingerprint below). Changing them must not re-evaluate every PRIMARY job. `submission` (who may submit, and the
# representation facts) and `employer_identity` (names that are other organisations) decide permission to submit only.
NOT_PRIMARY_DECISIONS = ("operations", "night", "scanning", "source_discovery", "board_verification", "submission",
                         "preparation", "assistance",
                         "employer_identity")


def decision_fingerprint(ctx: Context) -> str:
    """Policy data + gate/scoring code versions: a change to any of them re-evaluates earlier decisions.
    The 'operations' section (notifications, backups) is excluded: it never changes a decision."""
    return policy_decision_fingerprint(ctx.policy)


def policy_decision_fingerprint(policy: PolicyBundle) -> str:
    """decision_fingerprint from the policy alone (packages.route_for compares a score's with it)."""
    decisive = {k: v for k, v in policy.data.items() if k not in NOT_PRIMARY_DECISIONS}
    from careeros.qualify.title_rescue import RESCUE_VERSION  # a new rescue rule re-checks every earlier decision
    return fingerprint({"policy": fingerprint(decisive), "gate": gate_mod.RULES_VERSION, "scores": score_mod.SCORES_VERSION,
                        "rescue": RESCUE_VERSION})


def refresh_derived_fields(ctx: Context) -> int:
    """Recompute fields derived from stored posting text when the derivation code improves (state limits, an
    employment type stated in the title). A job with a source record is derived again by the one derivation (the D190
    design, section 3a); a row stored before 017 by the older rules below, until the upgrade pass gives it a record."""
    store, changed = ctx.store, 0
    for row in store.all(f"SELECT * FROM jobs WHERE {OPEN} AND source_json IS NOT NULL ORDER BY job_id"):
        fields = derive(ctx, json.loads(row["source_json"]), row["job_id"])
        values = {k: v for k, v in fields.items() if k != "source_updated_at" and row[k] != v}
        if not values:
            continue
        with store.tx():
            store.writer("discovery").update("jobs", values, "job_id = ?", (row["job_id"],))
            if row["current_state"] not in ("discovered", "normalized"):
                store.writer("discovery").transition(ctx.matrix, row["job_id"], "normalized",
                                                     reason_code="derived_fields_refreshed")
        changed += 1
    rows = store.all(f"SELECT job_id, location_raw, description_text, remote_type, remote_states_json, remote_excluded_states_json, "
                     f"current_state FROM jobs WHERE {OPEN} AND remote_type IN ('remote','hybrid') AND source_json IS NULL")
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
                         "FROM jobs WHERE remote_scope IS NULL AND source_json IS NULL"):  # 2026-10-13: descriptive only, so no re-evaluation
        store.writer("discovery").update("jobs", {
            "remote_scope": remote_scope(row["remote_type"], row["country"], json.loads(row["remote_states_json"] or "[]"),
                                         json.loads(row["remote_excluded_states_json"] or "[]"), row["description_text"] or ""),
            "timezone_requirement": timezone_requirement(row["description_text"] or "")}, "job_id = ?", (row["job_id"],))
    from careeros.discovery.manual_intake import location_declared
    for row in store.all(f"SELECT job_id, ats, title, employment_type, country, current_state, location_raw, description_text, "
                         f"remote_type, remote_states_json, remote_excluded_states_json, typed_countries_json FROM jobs "
                         f"WHERE {OPEN} AND source_json IS NULL"):
        values = {}
        stated = title_employment(row["title"])
        if stated and stated != row["employment_type"]:
            values["employment_type"] = stated
        # the country as the current rule reads the stored posting: a job placed by its text under an older rule (not
        # rescanned since, or never on a board: a paste, an alert) is re-screened with the question open (Codex
        # CR-2026-11-15-GATE-FIXES F1, Pass 4: the text never places a posting)
        # a pasted posting's location places it only when it is the posting's own "Location:" line or yours (--location):
        # an older parser saved phrases from the prose (Codex CR-2026-11-15-GATE-FIXES H2, Pass 6)
        location = (row["location_raw"] or "") if location_declared(store, dict(row)) else ""
        country = placed_country(row["title"], location, row["description_text"] or "", row["remote_type"],
                                 json.loads(row["typed_countries_json"] or "[]"))
        if country != row["country"]:
            values["country"] = country
            values["remote_scope"] = remote_scope(row["remote_type"], country, json.loads(row["remote_states_json"] or "[]"),
                                                  json.loads(row["remote_excluded_states_json"] or "[]"), row["description_text"] or "")
        if not values:
            continue
        with store.tx():
            store.writer("discovery").update("jobs", values, "job_id = ?", (row["job_id"],))
            if row["current_state"] not in ("discovered", "normalized"):
                store.writer("discovery").transition(ctx.matrix, row["job_id"], "normalized", reason_code="derived_fields_refreshed")
        changed += 1
    return changed


# --- the source record (the D190 design, section 3a) ---------------------------------------------------------------------
# Every input normalized_fields reads: each RawPosting field unless it is declared bookkeeping here, the raw payload's
# values its readers read (RAW_INPUTS), and the employer entry with how it was resolved. Two tests enforce that the
# record leaves nothing out (tests/test_d190.py SourceRecordTests): a structural one over the dataclass and the raw keys
# the code reads, and a behavioural one over the bookkeeping inputs.
SOURCE_BOOKKEEPING = ("raw", "updated_at")   # the payload itself apart from RAW_INPUTS; the board's own update time
RAW_INPUTS = ("location_source", "reader")   # isListed is bookkeeping: it decides only whether a listing admits a posting
# what a reader filled in, inferred or cut, in the record only where it did (the D190 design, section 3, "Absent versus
# stated"; BATCH_5 D1): never recorded as stated, so a later read that states it differently is a change
MARKS = ("inferred", "cut")
# the record key each mark's name qualifies ("Absent versus stated, on a copy": Codex Pass 18, D203): "company" the
# employer entry's name, "text" the posting's text; "title" and "location" (and any other name) their own key
MARKED = {"company": ("employer", "name"), "text": ("description_text",)}
RAW_BOOKKEEPING = ("isListed",)
ENTRY_KEYS = ("employer_id", "name", "parent", "resolved")
# the fields a read can find kept against it on purpose ("Retained fields"): your location (with where it came
# from), a settled board job's location and the typed countries the settle cleared, a pasted job's own links
RETAINED = ("location", "location_source", "typed_countries", "url", "apply_url")
PAY_FIELDS = frozenset({"comp_min", "comp_max", "comp_period", "comp_currency"})


def source_record(p: RawPosting, entry: dict | None) -> dict:
    """The source record of a read: each RawPosting field as read (before normalized_fields cuts the department and the
    workplace hint), the raw payload's values its readers read, and the employer entry it was read with."""
    from dataclasses import fields as dataclass_fields
    record = {f.name: getattr(p, f.name) for f in dataclass_fields(p) if f.name not in SOURCE_BOOKKEEPING}
    raw = p.raw if isinstance(p.raw, dict) else {}
    record.update({key: raw.get(key) for key in RAW_INPUTS})
    record.update({key: raw[key] for key in MARKS if raw.get(key)})
    record["employer"] = {key: (entry or {}).get(key) for key in ENTRY_KEYS}
    return record


def source_digest(stored: str | None) -> str | None:
    """The digest of a stored source record (jobs.source_json), or None for a row that has none: what each screening
    records as the record it judged (gate_results.source_digest; the D190 design, part c)."""
    import hashlib
    return hashlib.sha256(stored.encode("utf-8")).hexdigest() if stored else None


def sighting_facts(record: dict) -> dict:
    """What an observation records to be a sighting (the D190 design, section 3, part d): the incoming read's source
    record, taken before any retained field is substituted, and its digest. P1 reads only these observations."""
    return {"sighting": True, "source": record, "source_digest": source_digest(source_json(record))}


def source_json(record: dict) -> str:
    """A source record as stored (jobs.source_json): canonical JSON, its text in the canonical form."""
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def employer_entry(ctx: Context, *, row=None, name: str | None = None, parent: str | None = None, job=None) -> dict:
    """The one resolver of the employer entry a read is written with (the D190 design, section 3, "Unchanged reads"):
    a scan's or a board link's registry row; your --parent with the name the source stated; else parent_for under the
    policy; a read that comes with none (the shortlist refresh) keeps the job's stored entry. `resolved` says which, so
    each processing run can resolve a known entry again. No parent comes from anywhere else."""
    if row is not None:
        return {"employer_id": row["employer_id"], "name": canonical(row["name"]),
                "parent": canonical(row["parent"] or row["name"]), "resolved": "registry"}
    if name is not None:
        if parent:
            return {"employer_id": None, "name": canonical(name), "parent": canonical(parent), "resolved": "argument"}
        from careeros.discovery.saved import parent_for
        return {"employer_id": None, "name": canonical(name), "parent": canonical(parent_for(canonical(name), ctx.policy)),
                "resolved": "parent_for"}
    if job is not None:
        stored = json.loads(job["source_json"]).get("employer") if job["source_json"] else None
        if stored:
            return {key: stored.get(key) for key in ENTRY_KEYS}
        return {"employer_id": job["employer_id"], "name": job["employer_name"], "parent": job["parent"],
                "resolved": "stored"}
    return {key: None for key in ENTRY_KEYS} | {"resolved": "none"}


def same_read(stored: dict | None, incoming: dict, *, board: bool) -> bool:
    """Whether a read is unchanged (the D190 design, section 3, "Unchanged reads"): its source record, taken before any
    retained field is substituted, is equal to the stored one, each retained field compared with what its retention is
    bound to (the stored record's `bindings`). No other normalization takes part, and content_fingerprint is not the
    predicate. The employer entry counts by its values (another parent is another read, Codex H19), not by how it was
    resolved. On a board read every input counts as stated, with null and "" as one value: a board clearing a field is a
    change. On a copy a field its reader did not find says nothing about it and is not compared, and a field it found
    that the stored record marks inferred or cut, and it does not, is a change (dropped_marks)."""
    if not stored or stored.get("unknown"):
        # a record the upgrade rebuilt from stored columns lists the inputs they could not give: the job's first
        # read after it counts as a change, never as the same read (the D190 design, section 7; Codex Pass 6)
        return False
    bindings = stored.get("bindings") or {}
    for key in (set(stored) | set(incoming)) - {"bindings"}:
        if key == "employer":
            mine, theirs = stored.get(key) or {}, incoming.get(key) or {}
            if any(_blank(mine.get(k)) != _blank(theirs.get(k)) for k in ("employer_id", "name", "parent")):
                return False
            continue
        if not board and _blank(incoming.get(key)) is None:
            continue
        expected = bindings[key] if key in bindings else stored.get(key)
        if _blank(expected) != _blank(incoming.get(key)):
            return False
    # a copy that finds a field the stored record marks and does not mark it states it: a change, its last mark
    # included. On top of the comparison above, never instead of it (Codex Pass 18, D203: strictly tighter)
    return board or not dropped_marks(stored, incoming)


def marked_value(record: dict, name):
    """The value of a source record a mark's name qualifies (MARKED), or None."""
    if not isinstance(name, str):
        return None
    value: object = record
    for key in MARKED.get(name, (name,)):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def dropped_marks(stored: dict, incoming: dict) -> dict:
    """What a copy drops of the stored record's marks (the D190 design, section 3, "Absent versus stated, on a copy";
    Codex Pass 18, D203): each field the stored record marks inferred or cut that the copy found, and does not mark.
    {mark: [names]}, only the marks with any. A field the copy did not find says nothing, as in same_read. A mark on a
    retained field describes the read its binding holds (retention substitutes the value, never the marks)."""
    out = {}
    for mark in MARKS:
        mine, theirs = stored.get(mark), incoming.get(mark)
        theirs = theirs if isinstance(theirs, list) else []
        names = [name for name in (mine if isinstance(mine, list) else [])
                 if name not in theirs and _blank(marked_value(incoming, name)) is not None]
        if names:
            out[mark] = names
    return out


def _blank(value):
    """null, "" and an empty list or dict are one value."""
    return None if value in (None, "", [], {}) else value


def location_places(p: RawPosting) -> bool:
    """Whether a read's location may place the job (D190 rule 2: text never says US): a board's own location field
    always; a copy's when it is your argument (--location, careeros location), its own same-line "Location:" label, or a
    saved file's own location field as the current saved-file reader wrote it. The rule manual_intake.location_declared
    applies to a stored row, applied to the read itself, so a rebuild and a fresh intake agree (the D190 design, section
    3a, "One derivation"). A read that records no location source (an older caller) places as before."""
    if p.ats != "manual" or not (p.location or "").strip():
        return True
    raw = p.raw if isinstance(p.raw, dict) else {}
    source = raw.get("location_source")
    if source in (None, "argument"):
        return True
    if source == "label":
        from careeros.discovery.manual_intake import label_location
        return label_location(p.description_text or "") == p.location
    if source == "file":
        from careeros.discovery.saved import READER
        return raw.get("reader") == READER
    return False


def family_of(ctx: Context, title_norm: str, job_id: str | None = None) -> str | None:
    """A job's family under the current policy, else the title rescue's own decision under its current version
    (title_reviews), which the one derivation takes as a non-posting input (the D190 design, section 3a)."""
    from careeros.qualify.title_rescue import RESCUE_VERSION
    family = classify_family(title_norm, ctx.policy.get("families", default={}))
    if family or job_id is None:
        return family
    row = ctx.store.one("SELECT family FROM title_reviews WHERE job_id = ? AND rules_version = ? AND outcome = 'rescued'",
                        (job_id, RESCUE_VERSION))
    return row["family"] if row else None


def posting_from_record(record: dict) -> RawPosting:
    """The read a source record holds, as a RawPosting."""
    from dataclasses import fields as dataclass_fields
    names = {f.name for f in dataclass_fields(RawPosting)} - set(SOURCE_BOOKKEEPING)
    return RawPosting(**{k: record[k] for k in names if k in record},
                      raw={k: record[k] for k in RAW_INPUTS if record.get(k) is not None})


def derive(ctx: Context, record: dict, job_id: str | None = None) -> dict:
    """The one derivation (the D190 design, section 3a): every derived value of a job, made by normalized_fields from its
    source record and the non-posting inputs (the policy, the profile, the title rescue's decisions). The processing
    run's refresh, your location write and the family writes all use it, so P1's re-derivation and the stored values
    never diverge."""
    return normalized_fields(ctx, record.get("employer"), posting_from_record(record), job_id=job_id)


def reresolve_entries(ctx: Context) -> int:
    """Each processing run resolves the employer entry of every open job again where its resolution is known (the
    registry row, or parent_for under the policy; your --parent and an entry whose resolution is unknown stay): a
    different entry is written into the source record in one transaction with the job's derived values and its screen
    invalidated, so P1 waits and then compares, as for a read (the D190 design, section 3, "Unchanged reads"; Codex H19:
    a parent change reaches a job no one reads again)."""
    store, changed = ctx.store, 0
    for row in store.all(f"SELECT * FROM jobs WHERE {OPEN} AND source_json IS NOT NULL ORDER BY job_id"):
        record = json.loads(row["source_json"])
        entry = record.get("employer") or {}
        if entry.get("resolved") == "registry" and entry.get("employer_id"):
            registered = store.one("SELECT * FROM employers WHERE employer_id = ?", (entry["employer_id"],))
            fresh = employer_entry(ctx, row=registered) if registered is not None else None
        elif entry.get("resolved") == "parent_for" and entry.get("name"):
            fresh = employer_entry(ctx, name=entry["name"])
        else:
            continue
        if fresh is None or all(fresh.get(k) == entry.get(k) for k in ENTRY_KEYS):
            continue
        record["employer"] = fresh
        fields = derive(ctx, record, row["job_id"])
        values = {k: v for k, v in fields.items() if k != "source_updated_at" and row[k] != v}
        values["source_json"] = source_json(record)
        with store.tx():
            store.writer("discovery").update("jobs", values, "job_id = ?", (row["job_id"],))
            if row["current_state"] not in ("discovered", "normalized"):
                store.writer("discovery").transition(ctx.matrix, row["job_id"], "normalized", reason_code="employer_reresolved")
        changed += 1
    return changed


def read_entry(ctx: Context, employer: dict | None, existing) -> dict:
    """The entry a read is written with: the caller's (an entry employer_entry resolved, or a registry row), else the
    job's stored entry (a read that comes with none). A plain dict from an older caller is kept, its resolution unknown."""
    if employer is None:
        return employer_entry(ctx, job=existing) if existing is not None else employer_entry(ctx)
    if "resolved" in employer:
        return {key: employer.get(key) for key in ENTRY_KEYS}
    if employer.get("employer_id"):
        return employer_entry(ctx, row=employer)
    return {"employer_id": None, "name": employer.get("name"), "parent": employer.get("parent"), "resolved": "unknown"}


# --- ingest ------------------------------------------------------------------------------------------
def normalized_fields(ctx: Context, employer: dict | None, p: RawPosting, job_id: str | None = None) -> dict:
    text = p.description_text or ""
    remote = detect_remote(p.location, p.workplace_hint, text)
    # the pay tier for your state when the posting says which it is (2026-11-04); the gate reads the same home state
    comp = parse_comp(text, p.comp, ctx.policy.get("location", "home_state") or ctx.profile.state) or {}
    # the employer entry's name and parent in the canonical form (the D190 design, section 3a, point 6)
    name = canonical((employer or {}).get("name") or p.board_token)
    parent = canonical((employer or {}).get("parent") or name)
    title_norm = norm(p.title)
    allowed, excluded = remote_state_rules(p.location, text, remote)
    country = placed_country(p.title, p.location if location_places(p) else "", text, remote, p.typed_countries)
    fields = {
        "employer_id": (employer or {}).get("employer_id"), "employer_name": name, "parent": parent,
        "requisition_id": p.requisition_id, "title": p.title, "title_norm": title_norm,
        "family": family_of(ctx, title_norm, job_id),
        "location_raw": p.location, "remote_type": remote,
        "remote_states_json": json.dumps(allowed), "remote_excluded_states_json": json.dumps(excluded),
        "remote_scope": remote_scope(remote, country, allowed, excluded, text), "timezone_requirement": timezone_requirement(text),
        "country": country, "employment_type": employment_type(p.employment_type_hint, text, p.title),
        "comp_min": comp.get("min"), "comp_max": comp.get("max"), "comp_period": comp.get("period"),
        "comp_currency": comp.get("currency"), "posted_at": p.posted_at, "url": p.url, "apply_url": p.apply_url,
        "canonical_url": canonical_url(p.url), "description_text": text, "dedupe_key": dedupe_key(parent, title_norm, p.location),
        "department": canonical_cut(p.department, 200) or None, "source_updated_at": p.updated_at, "deadline_at": deadline_of(text),
        # the board's own workplace hint and pay fields, as it gives them (migration 016; Codex's D190 design review)
        "workplace_hint": canonical_cut(p.workplace_hint, 100) or None, "board_pay_json": board_pay_json(p.comp),
        "typed_countries_json": json.dumps(p.typed_countries, ensure_ascii=False) if p.typed_countries else None,
    }
    fields["content_fingerprint"] = content_fingerprint(fields)  # department / dates are not content: never "changed"
    return fields


def board_pay_json(comp: dict | None) -> str | None:
    """The board's own pay fields (RawPosting.comp: min, max, period, currency) as canonical JSON, or None when it gives
    none: what the board said, kept apart from the pay CareerOS reads (comp_min and the rest)."""
    if not comp or not any(comp.get(k) not in (None, "") for k in comp):
        return None
    return json.dumps({k: comp[k] for k in sorted(comp) if comp[k] not in (None, "")}, sort_keys=True, ensure_ascii=False)


def _pay_reread(existing, fields: dict, p: RawPosting) -> bool:
    """The stored pay (and state limits) differ only because CareerOS reads the same words better: the title, location,
    text, requisition and address are unchanged; the pay comes from the text now (not the board's own pay fields,
    which can change on their own) and came from it before (none stored, or a range the text states: a board that
    drops its own pay fields is a change); and pay and state eligibility are the only meaningful differences."""
    from careeros.normalize.posting import _RANGE, _amount
    from careeros.openings import meaningful_changes
    if p.comp and (p.comp.get("min") or p.comp.get("max")):
        return False
    if any((existing[k] or "") != (fields.get(k) or "")
           for k in ("title", "location_raw", "description_text", "requisition_id", "canonical_url")):
        return False
    if existing["comp_max"] is not None and not any(
            abs(max(_amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))) - existing["comp_max"]) < 1.0
            for m in _RANGE.finditer(fields.get("description_text") or "")):
        return False
    changes = meaningful_changes(existing, fields)
    return any(c["what"] == "pay" for c in changes) and all(c["what"] in ("pay", "state eligibility") for c in changes)


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


def _duplicate_of(store: Store, job_id: str, fields: dict) -> tuple[str, str] | None:
    """(reason, other job) a new job is suggested beside, by the owner's D188 (2026-11-19, "Exact links only"): anything
    that looks alike (the same careers-page link, the same requisition at the employer, the same title, employer and
    place) is at most a suggestion, linked for your review as a possible duplicate, never merged, never a variant. The
    same posting by its exact link is never a new job: every read of it is written into its job of record
    (job_of_record; the red team's batch 3), so it never reaches this. Only open jobs are looked at."""
    scope, extra = OPEN, ()
    if fields.get("canonical_url"):
        other = store.one(f"SELECT job_id FROM jobs WHERE canonical_url = ? AND job_id != ? AND {scope} "
                          f"ORDER BY first_seen_at, job_id LIMIT 1", (fields["canonical_url"], job_id, *extra))
        if other:
            return "same_link_other_opening", other["job_id"]
    if fields.get("requisition_id") and fields.get("employer_id") and re.search(r"\d", fields["requisition_id"]):
        other = store.one(f"SELECT job_id FROM jobs WHERE employer_id = ? AND requisition_id = ? AND job_id != ? AND {scope} "
                          f"ORDER BY first_seen_at LIMIT 1", (fields["employer_id"], fields["requisition_id"], job_id, *extra))
        if other:
            return "same_requisition_other_opening", other["job_id"]
    from careeros.discovery.seen_index import norm_company
    name, parent = norm_company(fields.get("employer_name")), norm_company(fields.get("parent"))
    for row in store.all(f"SELECT job_id, employer_name, parent FROM jobs WHERE dedupe_key = ? AND job_id != ? AND {scope} "
                         f"ORDER BY first_seen_at, job_id", (fields["dedupe_key"], job_id, *extra)):
        theirs = norm_company(row["employer_name"])
        if name and theirs and name != theirs and name != norm_company(row["parent"]) and theirs != parent:
            continue   # another employer's posting under a common parent is not even suggested (RT-15)
        return "same_title_location", row["job_id"]
    return None


NO_DECISIONS = (None, False, False)


def owner_decisions(store: Store, job_id: str) -> tuple:
    """(your latest PRIMARY label, dismissed now, an application recorded) for one job: what a join must never pick
    between (the coordinator's batch 3 condition 4)."""
    from careeros.review.dismiss import is_dismissed
    label = store.one("SELECT label FROM labels WHERE job_id = ? AND profile = 'primary' ORDER BY rowid DESC LIMIT 1",
                      (job_id,))
    applied = store.one("SELECT 1 FROM applications WHERE job_id = ? LIMIT 1", (job_id,))
    return (label["label"] if label else None, is_dismissed(store, job_id), applied is not None)


def same_posting_twins(store: Store, job_id: str) -> list[str]:
    """Other jobs that are the same posting as `job_id` and are not anyone's duplicate (twins not joined yet, or held
    apart because your decisions on them differ): its label waits while any exists (packages.route_for)."""
    from careeros.discovery.board_discovery import job_identity, jobs_with_identity
    row = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        return []
    return [r["job_id"] for r in jobs_with_identity(store, job_identity(row), exclude=job_id)
            if r["current_state"] != "duplicate" and not r["duplicate_of"]]


def rederive_identity(ctx: Context) -> Counter:
    """At every processing run (the owner's D187 and D188): what an earlier engine stored as one job, and the current
    rules would not, is undone. A duplicate that is not its original's posting (job_identity) returns to screening
    (duplicate -> normalized, with a reason), and a variant or a repost link is cleared; each pair stays linked for your
    review. Run every time, never once: a rollback to an older engine can store more, and nothing the current engine
    writes is undone (the red team's fr1-faults-1). Returns counts."""
    from careeros.discovery.board_discovery import same_posting
    from careeros.openings import link
    store, counts = ctx.store, Counter[str]()
    disc = store.writer("discovery")
    for job in store.all("SELECT * FROM jobs WHERE current_state = 'duplicate' AND duplicate_of IS NOT NULL ORDER BY job_id"):
        original = store.one("SELECT * FROM jobs WHERE job_id = ?", (job["duplicate_of"],))
        if original is None or same_posting(job, original):
            continue   # still the same posting
        with store.tx():
            disc.update("jobs", {"duplicate_of": None}, "job_id = ?", (job["job_id"],))
            disc.transition(ctx.matrix, job["job_id"], "normalized", reason_code="identity_rederived",
                            notes=f"no longer a duplicate of {job['duplicate_of']}: not the same posting (D188)")
            link(store, job["job_id"], job["duplicate_of"], "possible_duplicate",
                 "a duplicate of it under an earlier rule; not the same posting under the current one (D188)")
        counts["duplicates_rederived"] += 1
    # same-posting twins (the red team's gr1-identity-3): two jobs that are one posting by its exact link (one written
    # while the other was closed, or by an older engine) are one job, so an application, a label and a handoff of either
    # count for both. The one that carries your decisions (a label, a dismissal, an application) is the job of record,
    # the oldest when several carry the same ones; twins whose decisions differ are never joined by CareerOS: each is
    # linked to the other for your review and their labels wait (packages.route_for) until you make them agree (the
    # coordinator's batch 3 condition 4)
    from careeros.discovery.board_discovery import job_identity
    from careeros.openings import link
    groups: dict = {}
    for job in store.all("SELECT job_id, ats, external_id, url FROM jobs WHERE current_state != 'duplicate' "
                         "AND duplicate_of IS NULL ORDER BY first_seen_at, job_id"):
        identity = job_identity(job)
        if identity is not None:
            groups.setdefault(identity, []).append(job["job_id"])
    for rows in groups.values():
        if len(rows) < 2:
            continue
        decided = {job: decisions for job in rows if (decisions := owner_decisions(store, job)) != NO_DECISIONS}
        if len(set(decided.values())) > 1:
            with store.tx():
                for job in rows:
                    for other in rows:
                        if other != job:
                            link(store, job, other, "possible_duplicate",
                                 "one posting by its exact link, with different decisions of yours: make them agree")
            counts["twins_held"] += 1
            continue
        record = record_of(store, rows)   # target_of's own rule (BATCH_5 B2)
        kept = (store.one("SELECT source_json FROM jobs WHERE job_id = ?", (record,)) or {"source_json": None})["source_json"]
        for twin in rows:
            if twin == record:
                continue
            with store.tx():
                copy = store.one("SELECT url, source_json FROM jobs WHERE job_id = ?", (twin,))
                if copy is not None and copy["source_json"] and copy["source_json"] != kept:
                    # what the joined member says is a copy of the posting: recorded as a sighting of the job of
                    # record, so part (d) shows it and a label after it binds it (BATCH_5 B3)
                    from careeros.openings import record_observation
                    twin_record = json.loads(copy["source_json"])
                    record_observation(store, source_kind="manual_import", evidence_level="human", job_id=record,
                                       url=copy["url"], facts={**sighting_facts(twin_record), "joined_twin": twin})
                    counts["twin_copies_recorded"] += 1
                disc.update("jobs", {"duplicate_of": record}, "job_id = ?", (twin,))
                disc.transition(ctx.matrix, twin, "duplicate", reason_code="same_posting",
                                notes=f"the same posting as {record} by its exact link (D188): one job")
            counts["twins_joined"] += 1
    for column, relation in (("variant_of", "possible_duplicate"), ("repost_of", "possible_repost")):
        for job in store.all(f"SELECT job_id, {column} AS other FROM jobs WHERE {column} IS NOT NULL ORDER BY job_id"):
            with store.tx():
                disc.update("jobs", {column: None}, "job_id = ?", (job["job_id"],))
                link(store, job["job_id"], job["other"], relation,
                     f"a {column.replace('_of', '')} of it under an earlier rule; not the same posting under the current one (D188)")
            counts[f"{column.replace('_of', '')}s_cleared"] += 1
    if counts:
        disc.event("identity_rederived", outcome="success", metadata=dict(counts))
    return counts


def location_digest(location: str | None) -> str:
    """The digest a location write records: the observation keeps only 500 characters of a location, and a board's
    joined locations can be longer."""
    from careeros.core.fingerprint import text_fingerprint
    return text_fingerprint(" ".join((location or "").split()))


def scan_location_current(store: Store, job_id: str) -> bool:
    """Whether a job board's own listing scan wrote, or last confirmed, the job's location as it is now. A single-link
    fetch (add-url, an alert, a lead, a saved board link) writes it "single": the same board's data, but not its listing,
    so standing preparation waits for the next scan to list it the same way (Codex CR-2026-11-15-GATE-FIXES H21, Pass
    12; the owner's D182). A location written before 2026-11-18 has no record and waits for that scan too."""
    from careeros.discovery.manual_intake import location_write
    job = store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job_id,))
    write = location_write(store, job_id)
    return job is not None and write is not None and write["source"] == "scan" and \
        write.get("digest") == location_digest(job["location_raw"])


def _confirm_scan_location(ctx: Context, job_id: str, p: RawPosting) -> None:
    """A board's listing scan lists the job at the location it has: recorded once as the scan's, so standing may rely on
    it again after a single-link fetch wrote it (H21, Pass 12). A listing at another location confirms nothing."""
    from careeros.openings import record_observation
    row = ctx.store.one("SELECT location_raw, current_state, canonical_url FROM jobs WHERE job_id = ?", (job_id,))
    if row is None or row["current_state"] in ("duplicate", "closed") or (row["location_raw"] or "") != (p.location or "") \
            or scan_location_current(ctx.store, job_id):
        return
    record_observation(ctx.store, source_kind="ats_scan", evidence_level="live_ats", job_id=job_id, url=p.url,
                       canonical=row["canonical_url"], key=f"{p.ats}:{p.board_token}:{p.external_id}",
                       requisition_id=p.requisition_id, title=p.title, location=row["location_raw"],
                       facts={"location_write": "scan", "location_digest": location_digest(row["location_raw"]),
                              "confirmed_by_scan": True})


def _scan_not_stored(ctx: Context, job_id: str, p: RawPosting) -> None:
    """A board's listing scan read the job but could not store it: standing no longer relies on an earlier scan's
    location until a scan stores or confirms it again (the red team after Codex Pass 12, RT1-1)."""
    from careeros.openings import record_observation
    try:
        with ctx.store.tx():
            if ctx.store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
                record_observation(ctx.store, source_kind="ats_scan", evidence_level="live_ats", job_id=job_id,
                                   key=f"{p.ats}:{p.board_token}:{p.external_id}", facts={"location_write": "scan_not_stored"})
    except Exception:   # noqa: BLE001 - the failure is already recorded as the ingest exception
        pass


def _observe_posting(ctx: Context, job_id: str, p: RawPosting, fields: dict, source: str = "ats_api",
                     incoming: dict | None = None) -> None:
    from careeros.openings import record_observation
    record_observation(ctx.store, source_kind="ats_scan" if p.ats != "manual" else "manual_import",
                       evidence_level="live_ats" if p.ats != "manual" else "human", job_id=job_id, url=p.url,
                       canonical=fields.get("canonical_url"), key=f"{p.ats}:{p.board_token}:{p.external_id}",
                       requisition_id=p.requisition_id, title=p.title, company=fields.get("employer_name"),
                       location=p.location, published_at=p.posted_at, updated_at=p.updated_at,
                       facts={**{k: fields.get(k) for k in ("comp_min", "comp_max", "comp_period", "remote_type", "employment_type")},
                              # where a manual job's location came from, written with it (Codex CR-2026-11-15-GATE-FIXES
                              # H2, Pass 7): argument, label, file or none
                              **({"location_write": p.raw["location_source"],
                                  **({"reader": p.raw["reader"]} if p.raw.get("reader") else {})}
                                 if p.ats == "manual" and isinstance(p.raw, dict) and p.raw.get("location_source") else {}),
                              # a board posting: written by the board's listing scan or by a single-link fetch (H21,
                              # Pass 12), with the digest of the whole location
                              **({"location_write": "scan" if source == "ats_api" else "single",
                                  "location_digest": location_digest(fields.get("location_raw"))}
                                 if p.ats != "manual" else {}),
                              # the read as it came, before any retained field: a sighting (the D190 design, part d)
                              **(sighting_facts(incoming) if incoming is not None else {})})


def _copy_sighting(ctx: Context, job_id: str, p: RawPosting, incoming: dict) -> None:
    """A copy of a posting a board's own read has written (a saved page, an import, add-url --text-file) is never
    written into the job: its read is recorded as a sighting, which P1 compares with each label (the D190 design,
    section 3, part d; today's branch recorded nothing)."""
    from careeros.openings import record_observation
    record_observation(ctx.store, source_kind="manual_import", evidence_level="human", job_id=job_id, url=p.url,
                       key=f"{p.ats}:{p.board_token}:{p.external_id}", requisition_id=p.requisition_id, title=p.title,
                       company=(incoming.get("employer") or {}).get("name"), location=p.location,
                       published_at=p.posted_at, updated_at=p.updated_at, facts=sighting_facts(incoming))


def _own_id(store: Store, employer: dict | None, p: RawPosting) -> tuple[RawPosting, str | None]:
    """A manual posting is written under an id that holds this same posting: a held id is written only when it is this
    posting (by link, "#" part included, requisition and text), whatever the employer; a different posting gets its
    own id, the one it was given before when it comes again (`manual_intake.place_manual`). Returns the posting and the
    id of the different posting it collided with, or None (Codex CR-2026-11-15-GATE-FIXES H8, H10, H11, Passes 8-9)."""
    from dataclasses import replace

    from careeros.discovery.manual_intake import place_manual
    external, collided = place_manual(store, p.ats, p.board_token, p.external_id, url=p.url,
                                      requisition_id=p.requisition_id, text=p.description_text or "",
                                      employer=(employer or {}).get("name") or p.board_token, title=p.title,
                                      location=p.location)
    return (replace(p, external_id=external) if external != p.external_id else p), collided


def _keep_your_location(store: Store, job_id: str, p: RawPosting) -> RawPosting:
    """A manual posting whose location you set (careeros location, or --location when you pasted or added it) keeps it
    when the same posting comes again without one of yours: an edited copy added or imported again is that posting when
    its exact link names it (the owner's D188, 2026-11-19), and its new text is written, never the copy's location. Only
    a location you give again replaces yours. The red team after Codex Pass 11 (rt4_6), kept under D188."""
    from dataclasses import replace

    from careeros.discovery.manual_intake import _yours, active_settle, location_write
    if p.ats != "manual":
        # a board job whose location you settled (careeros location: its board's field left it unknown or abroad) keeps
        # yours while the board's field is the one you settled; another field from the board supersedes it (the owner's
        # D192 build, part 4; Codex's D190 design review H6)
        held = store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job_id,))
        settled = active_settle(store, job_id, held["location_raw"]) if held is not None else None
        if settled is not None and held is not None and (p.location or "") == settled["board_field"]:
            return replace(p, location=held["location_raw"], typed_countries=[])
        return p
    if isinstance(p.raw, dict) and p.raw.get("location_source") == "argument":
        return p
    held = store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job_id,))
    if held is not None and (held["location_raw"] or "").strip() and not (p.location or "").strip():
        # a copy of the posting that states no location says nothing about it: the stored field stays, with the record
        # of where it came from (no location write: the earlier one still decides placement). The coordinator's fuzzer
        # seed 68 (an add-url re-read blanked 'Remote'); the call is recorded with batch 3. Where it came from stays the
        # stored record's (BATCH_5 D2: dropped, the source read as an older caller's and placed the job)
        raw = {**(p.raw if isinstance(p.raw, dict) else {}), **_stored_provenance(store, job_id)}
        return replace(p, location=held["location_raw"], raw=raw)
    write = location_write(store, job_id, with_facts=True)
    if held is None or write is None or write["source"] != "argument" or not _yours(write.get("facts") or {}):
        return p
    raw = {**(p.raw if isinstance(p.raw, dict) else {}), "location_source": "argument"}
    return replace(p, location=held["location_raw"], raw=raw)   # no location write recorded: yours stays the last


def _stored_provenance(store: Store, job_id: str) -> dict:
    """Where a job's stored location came from (its source record's location_source and reader); unknown, which places
    nothing, for a job with no record."""
    row = store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job_id,))
    record = json.loads(row["source_json"]) if row is not None and row["source_json"] else {}
    if not record:
        return {"location_source": "unknown"}
    return {"location_source": record.get("location_source") or "unknown", "reader": record.get("reader")}


def _write_read(ctx: Context, job_id: str, existing, fields: dict, p: RawPosting, source: str, now: str):
    """A read that is not unchanged, written to its job: every field, its snapshot and observation, the change it
    makes (a new requisition or address with it: RT-04 and RT-05), and the job screened again. Returns (the
    meaningful changes, the identifiers it moved)."""
    from careeros.openings import meaningful_changes, record_change
    store, disc = ctx.store, ctx.store.writer("discovery")
    changes = meaningful_changes(existing, fields)
    disc.update("jobs", {**fields, "last_seen_at": now, "absent_scans": 0}, "job_id = ?", (job_id,))
    _snapshot(ctx, job_id, fields, p.raw)
    _observe_posting(ctx, job_id, p, fields, source)
    moved_ids = _moved(existing, fields)
    record_change(store, job_id, changes + moved_ids)
    if existing["current_state"] not in ("discovered", "normalized"):
        disc.transition(ctx.matrix, job_id, "normalized",
                        reason_code="posting_changed" if changes else "posting_edited_cosmetic")
    return changes, moved_ids


def _moved(existing, fields: dict) -> list[dict]:
    """A job_changes entry for each identifier of a stored job (its requisition, its address) the new fields change:
    structured, so a value with a space in it is read whole (identity_change_since; the red team's fr1-routes-1)."""
    return [{"field": k, "what": f"identifiers changed: {k}", "before": existing[k] or "-", "after": fields.get(k) or "-"}
            for k in ("requisition_id", "canonical_url") if (existing[k] or None) != (fields.get(k) or None)]


def board_written(store: Store, job_id: str) -> bool:
    """Whether a job holds a job board's own data for its posting: a board's job, or a job you pasted, added or saved
    first that a board's read (its listing scan or a fetch of its own link) has since written (the red team's batch 3,
    group A). A manual copy never writes over a board's data: the board's own reads carry the posting's changes."""
    row = store.one("SELECT ats FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        return False
    from careeros.discovery.board_discovery import BOARD_ATS
    return row["ats"] in BOARD_ATS or store.one(
        "SELECT 1 FROM observations WHERE job_id = ? AND source_kind = 'ats_scan' LIMIT 1", (job_id,)) is not None


def posting_identity_of(p: RawPosting, job_id: str) -> tuple[str, str] | None:
    """The posting a read is (board_discovery.job_identity), from the read alone."""
    from careeros.discovery.board_discovery import job_identity
    return job_identity({"ats": p.ats, "external_id": p.external_id, "url": p.url, "job_id": job_id})


def target_of(store: Store, employer: dict | None, p: RawPosting) -> tuple[RawPosting, str, str | None]:
    """The job a read is written to: the only function that maps an incoming read to a job (the D190 design, section 7;
    Codex H20). Read-only. A manual posting is placed first (its own id, the one it was given before, or a new one beside
    another posting: place_manual), then every read goes to the job of record of the posting its exact link names
    (job_of_record). When every job with that identity is a duplicate (only a stale duplicate under an older engine's
    rule, whose original is another posting), the read gets its own job: writing it into that original would join two
    postings (D188). The paste, ingest and the write-ahead step all resolve through it. Returns (the posting, under the
    external id it is placed under; the target job's id; the id of a different posting it was placed beside, or None).
    PlacementRefused when no free id is left."""
    collided = None
    if p.ats == "manual":
        p, collided = _own_id(store, employer, p)
    return p, job_of_record(store, f"{p.ats}:{p.board_token}:{p.external_id}", p), collided


def job_of_record(store: Store, job_id: str, p: RawPosting) -> str:
    """The job a read of posting `p` (written as `job_id` if it were new) is written into, by the owner's D188 ("Exact
    links only": one posting is one job) and the red team's batch 3, group A: the oldest job that is that posting
    (board_discovery.job_identity), open or closed, from any route, unless it is a duplicate. Every read of the posting,
    a board's listing, a fetch of its own link, a saved copy, reaches that job, so a re-identification, a retitle, a
    new location and a closure reach the job that carries your label, whichever route read the posting first. A posting
    whose link names no posting is its own job (`job_id`)."""
    from careeros.discovery.board_discovery import jobs_with_identity
    members = [row["job_id"] for row in jobs_with_identity(store, posting_identity_of(p, job_id))
               if row["current_state"] != "duplicate" and not row["duplicate_of"]]
    return record_of(store, members) if members else job_id


def record_of(store: Store, members: list[str]) -> str:
    """The job of record among a posting's jobs that are no one's duplicate, oldest first: the one that carries your
    decisions (a label, a dismissal, an application), the oldest of several that carry the same ones, else the oldest.
    Where your decisions on them differ they are never joined (their labels wait: packages.route_for) and a read goes to
    the oldest. One rule for target_of, read by read, and for rederive_identity's joins (BATCH_5 B2)."""
    decided = {job: decisions for job in members if (decisions := owner_decisions(store, job)) != NO_DECISIONS}
    if len(set(decided.values())) > 1:
        return members[0]
    return next(iter(decided), members[0])


_KEPT_LINK = ("url", "apply_url", "canonical_url")


def is_board_read(p: RawPosting) -> bool:
    """Whether a read is the board's own record of the posting: a board CareerOS reads (BOARD_ATS) and may read now (the
    site policy allows it). Only such a stored read lifts a hold (the D190 design, section 7)."""
    from careeros.discovery.board_discovery import BOARD_ATS
    from careeros.discovery.sources import status
    return p.ats in BOARD_ATS and status(p.ats) == "allowed"


def _stored_again(store: Store, job_id: str, *, board: bool, hold_id: str | None = None,
                  exclude: frozenset[str] = frozenset()) -> None:
    """A read of the job was stored, in that read's own transaction (the D190 design, section 7; Codex H21 to H25): the
    write-ahead hold written for this read is cleared by its id; and when the read is a board read (is_board_read), every
    other hold of this posting lifts too (posting_holds: a row for the job's id or its posting identity, a file line for
    either), but for the write-ahead holds of the batch's other occurrences (`exclude`, Z1). A hold that reaches the job
    only because its own posting cannot be read (Z8) is never lifted by another posting's read (Codex H39). A copy,
    retried or not, lifts nothing (the red team's gr1-faults-0, B.5; hr1-routes-0)."""
    from careeros.review.packages import clear_hold, posting_holds
    if hold_id:
        clear_hold(store, job_id, hold_id, "the read it was written ahead of was stored")
    if board:
        for hold in posting_holds(store, job_id):
            if hold["hold_id"] not in exclude and hold["hold_id"] != hold_id:
                clear_hold(store, job_id, hold["hold_id"], "a board read of the posting was stored")


class BatchAborted(RuntimeError):
    """A posting of a nested batch resolved to a job that existed before the batch and was not held ahead of it (or to
    another job than the one held): the batch is rolled back, that job is held, and the command stops (the D190 design,
    section 7, step 3)."""

    def __init__(self, job_id: str, identity: tuple | None):
        super().__init__(f"batch_aborted: a posting resolved to {job_id}, a job not held ahead of the batch")
        self.job_id, self.identity = job_id, identity


def write_ahead(store: Store, reads: list[tuple[int, str]]) -> dict[int, tuple[str, str]]:
    """Step 2 of a nested batch (the D190 design, section 7; Codex H18, H20): a read_pending hold on every existing job
    among the batch's targets, one per read (`reads`: its position in the batch and its target, from target_of), in one
    committed transaction of its own; if that commit fails, beside the database; if that fails too, the batch does not
    start (the error propagates). Returns {position: (hold_id, target)}: carried by position, never by a posting's
    generated id, which two occurrences can share (Z1)."""
    from careeros.discovery.board_discovery import job_identity
    from careeros.review.packages import HOLD_RULE, add_pending_hold, identity_key
    targets = [(position, target) for position, target in reads
               if target and store.one("SELECT 1 FROM jobs WHERE job_id = ?", (target,))]
    message = "read_pending: a read of this posting was being written; it holds until that write commits"
    ahead: dict[int, tuple[str, str]] = {}
    try:
        with store.tx():
            disc = store.writer("discovery")
            for position, target in targets:
                job = store.one("SELECT * FROM jobs WHERE job_id = ?", (target,))
                hold_id = disc.exception("data_quality", "low", message, entity_type="job", entity_id=target,
                                         error_code="read_pending",
                                         metadata={"posting_identity": identity_key(job_identity(job)), "rule": HOLD_RULE})
                ahead[position] = (hold_id, target)
    except Exception:   # noqa: BLE001 - the database refused them: beside it, or the batch does not start
        ahead = {}
        for position, target in targets:
            job = store.one("SELECT * FROM jobs WHERE job_id = ?", (target,))
            ahead[position] = (add_pending_hold(store, target, job_identity(job), message), target)
    return ahead


def hold_aborted(store: Store, exc: BatchAborted) -> None:
    """The hold for the job an aborted batch would have written into, in a fresh transaction, else beside the database."""
    _hold_failed_read(store, exc.job_id, exc.identity, exc)


def nested_ingest(ctx: Context, employer: dict | None, postings: list[RawPosting], *, placed: dict,
                  source: str = "manual_url", after=None) -> Counter:
    """A nested batch (an intake with its receipt; the D190 design, section 7): each posting's target, read-only; the
    write-ahead holds on the existing ones, committed first; then one transaction that writes each posting to its target,
    clearing its own hold by id with its write, and runs `after(counts)` (the caller's sighting and receipt, or a
    refusal that rolls the whole batch back, leaving the write-ahead holds in place). BatchAborted: rolled back, the job
    held, re-raised."""
    store = ctx.store
    reads = []
    for position, p in enumerate(postings):
        try:
            reads.append((position, target_of(store, employer, p)[1]))
        except Exception:   # noqa: BLE001 - refused or failed: ingest reports it in the batch
            continue
    ahead = write_ahead(store, reads)
    try:
        with store.tx():
            mark = store.all("SELECT COALESCE(MAX(rowid), 0) AS n FROM exceptions")[0]["n"]
            counts = ingest(ctx, employer, postings, source=source, full_board=False, placed=placed, ahead=ahead)
            if after is not None:
                try:
                    after(counts)
                except Exception as exc:
                    # what the batch recorded goes with its rollback: carried out, so the caller can record why
                    exc.reasons = [dict(r) for r in store.all(   # type: ignore[attr-defined]
                        "SELECT error_code, message, entity_id FROM exceptions WHERE rowid > ?", (mark,))]
                    raise
    except BatchAborted as exc:
        hold_aborted(store, exc)
        raise
    finally:
        from careeros.review.packages import prune_pending_holds
        prune_pending_holds(store)
    return counts


def _hold_failed_read(store: Store, job_id: str, identity: tuple | None, exc: BaseException) -> None:
    """A read of a posting that could not be stored holds the label of the job that is that posting (B.5): an open
    ingest failure recorded for `job_id` with the posting's identity, so it reaches the job of record even when which
    job that is could not be read (the red team's hr1-routes-0). When even that write fails, the hold is kept beside the
    database until the next command's start records it (packages.pending_holds), so it holds at once."""
    from careeros.review.packages import HOLD_RULE, add_pending_hold, identity_key
    message = f"ingest failed: {type(exc).__name__}: {exc}"
    try:
        store.writer("discovery").exception("data_quality", "medium", message, entity_type="job", entity_id=job_id,
                                            error_code="ingest_failed",
                                            metadata={"posting_identity": identity_key(identity), "rule": HOLD_RULE})
    except Exception:   # noqa: BLE001 - the database refused the hold too: kept outside it, or the command stops
        add_pending_hold(store, job_id, identity, message)


def hold_lost_reads(store: Store, reads: list[tuple[str, tuple | None]], cause: BaseException) -> None:
    """The holds written when SQLite ended the whole transaction a read was nested in (TransactionLost): for each read
    whose write was lost, `_hold_failed_read`, in a fresh transaction (none is open once SQLite ended it), else beside
    the database (the D190 design, revision 4, section 7). The one place this is decided. NOT complete coverage: today
    `reads` is the posting whose write failed; another posting the same lost transaction had already changed (an
    earlier card of the same email) loses its update with it and is not held until the email is read again (Codex's
    D190 design review, Pass 4, H18: revision 5's write-ahead holds are expected to replace this)."""
    for job_id, identity in reads:
        _hold_failed_read(store, job_id, identity, cause)


def ingest(ctx: Context, employer: dict | None, postings: list[RawPosting], *, source: str = "ats_api",
           full_board: bool = True, placed: dict | None = None, ahead: dict | None = None,
           exclude: frozenset[str] = frozenset(), batch_created: set[str] | None = None) -> Counter:
    """`placed`, when given, receives each posting's generated id -> the job id it was written under (a collision gives
    a manual posting another: Codex CR-2026-11-15-GATE-FIXES H11, Pass 9, so leads link to the right job), for postings
    actually written only: a refused or failed one is absent (H14, Pass 10), so no lead is linked and no file is filed
    as imported for it. `ahead`, in a nested batch (nested_ingest), maps each posting's position to its write-ahead
    hold and expected target: the posting must still resolve there (else BatchAborted), and its hold is cleared by id
    with its write or sighting (the D190 design, section 7; Z1). A batch that calls ingest once per occurrence (an
    alert email) passes its other occurrences' write-ahead holds (`exclude`) and the jobs it created so far
    (`batch_created`), so a second card of a new posting is not taken for a job outside the batch."""
    store, counts, seen, unsure = ctx.store, Counter(), set(), False
    disc = store.writer("discovery")
    written: list[tuple[str, str]] = []
    failed: set[str] = set()
    from careeros.discovery.manual_intake import PlacementRefused
    # jobs this batch created: a later posting of the batch may resolve to one
    created: set[str] = batch_created if batch_created is not None else set()
    for position, p in enumerate(postings):
        generated = f"{p.ats}:{p.board_token}:{p.external_id}"
        identity = posting_identity_of(p, generated)   # read from the posting alone: no database read
        try:
            # the one resolver (the D190 design, section 7): a manual posting's own id, then the job of record
            p, job_id, collided = target_of(store, employer, p)
        except PlacementRefused as exc:   # no free id: reported, never written over another posting (H11, Pass 9)
            disc.exception("data_quality", "medium", f"ingest refused: {exc}", entity_type="job", entity_id=generated,
                           error_code="manual_id_collision")
            counts["errors"] += 1
            continue
        except Exception as exc:   # noqa: BLE001 - recorded like any failed posting below
            job_id = generated
            failed.add(job_id)
            # which job this posting is could not be read: the hold carries the posting's identity, and this scan
            # counts no job of the board absent (the red team's hr1-routes-0)
            unsure = True
            _hold_failed_read(store, job_id, identity, exc)
            counts["errors"] += 1
            continue
        expected = (ahead or {}).get(position)
        if ahead is not None and job_id not in created and (
                (expected is not None and job_id != expected[1])
                or (expected is None and store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)))):
            # it resolves to a job that existed before the batch and was not held for it: abort (step 3)
            raise BatchAborted(job_id, identity)
        hold_id = expected[0] if expected is not None else None
        # the write-ahead holds of the batch's other occurrences stay until their own writes (Z1)
        others = frozenset(h for at, (h, _) in (ahead or {}).items() if at != position) | exclude
        unlisted = isinstance(p.raw, dict) and p.raw.get("isListed") is False and source == "ats_api"
        if unlisted and not store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
            # an unlisted posting in a board's listing data: not admitted from the listing (its board leaves it out), and
            # not absent either: the job you added by its link stays open (the red team's gr1-boards-1)
            continue
        written.append((generated, job_id))
        seen.add(job_id)
        try:
            with store.tx():
                existing = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
                if p.ats == "manual" and board_written(store, job_id):
                    # a copy you saved or added of a posting a board's own read has written: a sighting only, never
                    # written over the board's data (the board's reads carry its changes)
                    _copy_sighting(ctx, job_id, p, source_record(p, read_entry(ctx, employer, existing)))
                    _stored_again(store, job_id, board=False, hold_id=hold_id, exclude=others)   # a copy lifts nothing
                    counts["already_tracked"] += 1
                    continue
                entry = read_entry(ctx, employer, existing)   # the one resolver (the D190 design)
                incoming = source_record(p, entry)   # as read, before any retained field is substituted
                p = _keep_your_location(store, job_id, p)
                fields = normalized_fields(ctx, entry, p, job_id=job_id)
                record = source_record(p, entry)   # the record as written, retention applied (section 3a)
                now = store.clock.now()
                first_board_read = existing is not None and existing["ats"] == "manual" and p.ats != "manual" \
                    and not board_written(store, job_id)
                if existing is not None and existing["ats"] == "manual" and job_id != generated:
                    # a read of the posting under another id (its board's, or another link form of it) written into a
                    # job you pasted, added or saved first: the job keeps its own link, so it stays that posting
                    # (job_identity reads a manual job by its link)
                    fields.update({k: existing[k] for k in _KEPT_LINK})
                    record.update({"url": existing["url"], "apply_url": existing["apply_url"]})
                # each field retention substituted is bound to what this read sent: a later read is compared with that
                bindings = {k: incoming[k] for k in RETAINED if record.get(k) != incoming.get(k)}
                if bindings:
                    record["bindings"] = bindings
                fields["source_json"] = source_json(record)
                board_read = p.ats != "manual"   # the board's own record: a scan, or a fetch of the posting's own link
                # a board posting read open at its own link (a single fetch) is open: a job closed only because a
                # listing left it out reopens (the red team's gr1-boards-1)
                own_link_read = source != "ats_api" and p.ats != "manual"
                stored = True
                if existing is None:
                    disc.insert("jobs", {"job_id": job_id, "workflow_id": f"job:{job_id}", "source": source, "ats": p.ats,
                                         "external_id": p.external_id, "first_seen_at": now, "last_seen_at": now,
                                         "current_state": ctx.matrix.initial_state, "state_sequence": 0, "entered_state_at": now,
                                         **fields})
                    _snapshot(ctx, job_id, fields, p.raw)
                    _observe_posting(ctx, job_id, p, fields, source, incoming)
                    disc.event("job_discovered", outcome="success", entity_type="job", entity_id=job_id,
                               metadata={"source": source, "ats": p.ats, "board": p.board_token})
                    if collided:   # given its own id beside another posting: linked to it (D172; H8, Pass 9)
                        from careeros.openings import link
                        link(store, job_id, collided, "possible_duplicate", f"the same generated id as {collided}: another posting")
                    match = _duplicate_of(store, job_id, fields)
                    if match:   # a suggestion for your review only (D188)
                        from careeros.openings import link
                        link(store, job_id, match[1], "possible_duplicate", match[0])
                        counts["possible_duplicate"] += 1
                    repost = store.one("SELECT job_id FROM jobs WHERE dedupe_key = ? AND job_id != ? "
                                       "AND current_state = 'closed' ORDER BY closed_at DESC LIMIT 1", (fields["dedupe_key"], job_id))
                    if repost:
                        # a suggestion for your review only, never repost_of: one posting is one exact link (D188)
                        from careeros.openings import link
                        link(store, job_id, repost["job_id"], "possible_repost", "same title and location as a closed posting")
                        counts["repost"] += 1
                    disc.transition(ctx.matrix, job_id, "normalized")
                    created.add(job_id)
                    counts["new"] += 1
                elif existing["current_state"] == "closed" and (full_board or own_link_read):
                    # listed again on a complete board scan, or read open at its own link: reopened (a meaningful
                    # change), never silently
                    from careeros.openings import meaningful_changes, record_change
                    disc.update("jobs", {**fields, "last_seen_at": now, "closed_at": None, "absent_scans": 0}, "job_id = ?", (job_id,))
                    # what it reopened as is recorded: another requisition, address or title suspends a label given
                    # before (the red team before Codex Pass 15, RT-05)
                    moved_ids = _moved(existing, fields)
                    record_change(store, job_id, meaningful_changes(existing, fields) + moved_ids,
                                  extra="reopened: " + ("listed again after being closed" if full_board
                                                        else "read open at its own link after being closed"))
                    disc.transition(ctx.matrix, job_id, "normalized", reason_code="reopened")
                    counts["reopened"] += 1
                elif existing["current_state"] in ("duplicate", "closed"):
                    disc.update("jobs", {"last_seen_at": now}, "job_id = ?", (job_id,))
                    counts["inactive_seen"] += 1
                    stored = False   # nothing of the read is stored, so it lifts no hold (BATCH_5 B4)
                elif board_read and same_read(json.loads(existing["source_json"]) if existing["source_json"] else None,
                                              incoming, board=True):
                    # the same read (the D190 design, "Unchanged reads"): nothing of its record is written. A value a
                    # reader derives otherwise now (pay read better, the country, the remote scope) is written, and the
                    # job is screened again, recorded apart from an employer's edit
                    # null and "" are one value here, as same_read holds them: the column keeps the record's value,
                    # so a field read as null on one scan and "" on the next never re-screens the job (BATCH_5 F2)
                    refreshed = {k: v for k, v in fields.items() if k != "source_json" and existing[k] != v
                                 and not (_blank(existing[k]) is None and _blank(v) is None)}
                    disc.update("jobs", {"last_seen_at": now, "absent_scans": 0, **refreshed}, "job_id = ?", (job_id,))
                    if refreshed and existing["current_state"] not in ("discovered", "normalized"):
                        disc.transition(ctx.matrix, job_id, "normalized", reason_code="derived_fields_refreshed")
                    counts["pay_reread" if refreshed.keys() & PAY_FIELDS else "unchanged"] += 1
                elif board_read:
                    # any other board read is written (a department, a parent, a destination, a cleared field), and
                    # the job is screened again before P1 compares it with what the card showed
                    changes, moved_ids = _write_read(ctx, job_id, existing, fields, p, source, now)
                    counts["changed" if changes else "reidentified" if moved_ids else "edited_cosmetic"] += 1
                elif existing["content_fingerprint"] != fields["content_fingerprint"] and _pay_reread(existing, fields, p):
                    # the same words, read better (2026-11-04: cents, the period, your pay tier): not a change the
                    # employer made, so no job_changes row; the posting is screened again with the new pay
                    disc.update("jobs", {**fields, "last_seen_at": now, "absent_scans": 0}, "job_id = ?", (job_id,))
                    _snapshot(ctx, job_id, fields, p.raw)
                    if existing["current_state"] not in ("discovered", "normalized"):
                        disc.transition(ctx.matrix, job_id, "normalized", reason_code="derived_fields_refreshed")
                    counts["pay_reread"] += 1
                elif existing["content_fingerprint"] != fields["content_fingerprint"] \
                        or existing["source_json"] != fields["source_json"]:
                    # a read of a job no board reads (its reads are its source: the latest one is written) whose text or
                    # source record as written differs, its first read after the upgrade included (the D190 design,
                    # section 7), is written whole and the job screened again: a source record never changes without
                    # its screen (section 3, "Unchanged reads")
                    changes, moved_ids = _write_read(ctx, job_id, existing, fields, p, source, now)
                    counts["changed" if changes else "edited_cosmetic"] += 1
                else:
                    # the same record: nothing of it is written
                    extra = {k: fields[k] for k in ("department", "source_updated_at", "deadline_at", "remote_scope",
                                                     "timezone_requirement", "workplace_hint", "board_pay_json",
                                                     "typed_countries_json")
                             if existing[k] != fields[k]}
                    # the country is derived with the remote scope and is written with it: a parser improvement that
                    # changes it on unchanged text re-screens the job, recorded apart from an employer's edit (Codex
                    # CR-2026-11-15-GATE-FIXES F2: the scope said US, the country stayed unknown, nothing re-screened)
                    rederived = existing["country"] != fields["country"]
                    if rederived:
                        extra["country"] = fields["country"]
                    if fields.get("employer_id") and existing["employer_id"] != fields["employer_id"]:
                        # the board that lists it now (a renamed board, or a job you added first): its absence there
                        # closes it (the red team's gr1-boards-0)
                        extra.update({k: fields[k] for k in ("employer_id", "employer_name", "parent")})
                    # the same text under a new requisition or address is a new identity for that posting (2026-10-18):
                    # recorded as a meaningful change, so a submission release for the old one no longer counts
                    moved = {k: fields[k] for k in ("requisition_id", "canonical_url") if (existing[k] or None) != (fields[k] or None)}
                    disc.update("jobs", {"last_seen_at": now, "absent_scans": 0, **extra, **moved}, "job_id = ?", (job_id,))
                    if rederived and existing["current_state"] not in ("discovered", "normalized"):
                        disc.transition(ctx.matrix, job_id, "normalized", reason_code="derived_fields_refreshed")
                    if moved:
                        from careeros.openings import record_change
                        record_change(store, job_id, _moved(existing, fields))
                    counts["reidentified" if moved else "unchanged"] += 1
                if first_board_read and not board_written(store, job_id):
                    # recorded once: the board's data is its own now
                    _observe_posting(ctx, job_id, p, fields, source, incoming)
                if source == "ats_api" and p.ats != "manual":
                    _confirm_scan_location(ctx, job_id, p)   # H21, Pass 12
                # in this read's own transaction: never cleared apart from its write (section 7); only a branch that
                # stores the read, in the job P1 compares it with, lifts a hold
                if stored:
                    _stored_again(store, job_id, board=is_board_read(p), hold_id=hold_id, exclude=others)
        except TransactionLost as exc:
            # the transaction this ingest is nested in (an alert email's) was ended whole by SQLite: nothing of it is
            # kept, so the work stops here, after the hold for this posting (hold_lost_reads: the D190 design, revision
            # 4, section 7; its limit is Pass 4's H18). A scan's posting is its own transaction: its failure arrives as
            # the original error, below
            failed.add(job_id)
            hold_lost_reads(store, [(job_id, identity)], exc.original or exc)
            raise
        except Exception as exc:  # one bad posting never stops the run
            failed.add(job_id)
            _hold_failed_read(store, job_id, identity, exc)
            counts["errors"] += 1
            if source == "ats_api" and p.ats != "manual":
                _scan_not_stored(ctx, job_id, p)
    if placed is not None:
        placed.update({generated: job_id for generated, job_id in written if job_id not in failed
                       and store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))})
    from careeros.review.packages import prune_pending_holds
    prune_pending_holds(store)   # a scan's postings committed one by one: the lines they cleared go (not in a batch)

    if full_board and employer and not unsure:
        # Possible absence first, confirmed closure only after `close_after_missed_scans` complete scans in a row.
        # Callers pass full_board=False for failed, partial or suspicious scans, so those never count.
        # Every open job that is a posting of this board counts, whatever route read it first (the red team's
        # gr1-boards-3: a posting added by its link, or a job you pasted first that the board's reads have written).
        from careeros.discovery.board_discovery import job_identity
        board_ats = employer["ats"] if "ats" in employer.keys() else None
        needed = int(ctx.policy.get("scanning", "close_after_missed_scans", default=2) or 2)
        stale = [row for row in store.all(f"SELECT * FROM jobs WHERE employer_id = ? AND {OPEN}", (employer["employer_id"],))
                 if (identity := job_identity(row)) is not None and identity[0] == (board_ats or identity[0])]
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
             "certifications": ctx.policy.get("certifications"),   # 2026-10-15: a catalog change re-reads postings
             "alias_exclusions": ctx.policy.get("skill_alias_exclusions", default={})}   # 2026-11-03: so does an exclusion
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
    family = _current_family(ctx, job)   # the current policy's, or a rescue under the current rules
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
                                       "source_digest": source_digest(job["source_json"]),
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
        "source_digest": source_digest(job["source_json"]),
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
    if job["source_json"]:   # the one derivation (the D190 design, section 3a)
        return derive(ctx, json.loads(job["source_json"]), job["job_id"])["family"]
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
                                       "source_digest": source_digest(job["source_json"]),
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
    rows = store.all("""SELECT j.*, g.policy_fingerprint AS gp, g.profile_fingerprint AS gpr,
                               g.extraction_fingerprint AS gate_extraction FROM jobs j
                        JOIN gate_results g ON g.result_id = (SELECT result_id FROM gate_results WHERE job_id = j.job_id
                                                              ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state IN ('scored','rejected','eligible','needs_review')""")
    for row in rows:
        reason = "policy_or_rules_changed" if row["gp"] != decision_fingerprint(ctx) else "profile_changed" if row["gpr"] != ctx.profile.fingerprint else None
        if reason:
            # A title screen may have rejected a posting after an earlier revision was extracted. Rescreen it
            # and re-read the current text instead of grading it against requirements cached from that revision.
            rescreen = (row["gate_extraction"] == TITLE_SCREEN or row["extraction_fingerprint"] is None
                        or row["current_state"] == "needs_review")
            target = "normalized" if rescreen else "extracted"
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
                        "identity": fingerprint(ctx.policy.data.get("employer_identity") or {}),   # read by the NIGHT conflict check
                        "holds": fingerprint((ctx.policy.data.get("submission") or {}).get("holds") or []),   # held families are familiar
                        "profile": ctx.profile.fingerprint, "content": job["content_fingerprint"],
                        "derived": [job["employment_type"], job["remote_type"], job["remote_states_json"],
                                    job["remote_excluded_states_json"], job["country"], job["comp_max"]],
                        "applied": _has_application(ctx, job["job_id"])})


def _has_application(ctx: Context, job_id: str) -> bool:
    """An open application anywhere in the job's role group (a right-to-represent check for NIGHT Supplemental), from the
    shared check; a duplicate search that could not finish counts as a possible conflict, never as clearance."""
    from careeros.review.applications import preflight
    if ctx.store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        return False
    check = preflight(ctx.store, job_id)
    return bool(check.open_applications) or not check.complete


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
                                   application_conflict=_has_application(ctx, job["job_id"]), now=store.clock.now())
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
    counts.update(rederive_identity(ctx))   # every run: what an earlier engine made one job (D187, D188)
    from careeros.upgrade import rebuild_forbidden
    counts.update(rebuild_forbidden(ctx))   # every run: W1 for a board the site policy forbids now (BATCH_5 G2)
    if reresolved := reresolve_entries(ctx):   # the employer entry, resolved again (the D190 design; Codex H19)
        counts["entries_reresolved"] = reresolved
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
