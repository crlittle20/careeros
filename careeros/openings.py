"""One opening across sources (2026-10-12, Parts 4 and 7).

- Observations: every source sighting of an opening is kept with its provenance and evidence level
  (live_ats > recommendation > alert > human), its namespaced posting key, requisition id, links and the source's own
  publication / update dates. Jobs hold the ATS facts; weaker sources never overwrite them, they are shown beside.
- Opening links: `same_opening` (evidence says it is one vacancy), `possible_duplicate` (similar, kept separate for
  your review) and `possible_repost` (identifiers changed). Similar titles never merge on their own.
- Changes: a posting edit is meaningful when pay, remote / state eligibility, schedule, employment type, required
  qualifications or licences, the deadline or the title changed, or when it reopened. Anything else is cosmetic.
- Application status follows the role group (variants, duplicates, reposts) and opening links, so applying, dismissing
  or a recorded outcome survives rediscovery.
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlsplit

from careeros.db.store import Store

EVIDENCE_ORDER = ("live_ats", "recommendation", "alert", "human")
MEANINGFUL_FIELDS = {
    "comp_min": "pay", "comp_max": "pay", "comp_period": "pay",
    "remote_type": "remote", "remote_states_json": "state eligibility", "remote_excluded_states_json": "state eligibility",
    "country": "country", "employment_type": "employment type", "deadline_at": "deadline", "title": "title",
}
REQUIREMENT_CATEGORIES = ("education", "certification", "license", "clearance", "work_authorization", "schedule", "travel",
                          "location")


def posting_key(url: str | None, ats: str | None = None, board: str | None = None, external_id: str | None = None) -> str | None:
    """A namespaced id for an opening at its source: greenhouse:<board>:<id>, linkedin:<id>, indeed:<jk>, ..."""
    if ats and board and external_id and ats != "manual":
        return f"{ats}:{board}:{external_id}"
    if not url:
        return None
    parts = urlsplit(url)
    host, query = parts.netloc.lower(), parse_qs(parts.query)
    if "linkedin.com" in host:
        tail = [s for s in parts.path.split("/") if s]
        return f"linkedin:{tail[-1]}" if tail and tail[-1].isdigit() else None
    if "indeed.com" in host and query.get("jk"):
        return f"indeed:{query['jk'][0]}"
    if "glassdoor." in host and (query.get("jl") or query.get("jobListingId")):
        return f"glassdoor:{(query.get('jl') or query.get('jobListingId'))[0]}"
    return None


def record_observation(store: Store, *, source_kind: str, evidence_level: str, job_id: str | None = None,
                       lead_id: str | None = None, url: str | None = None, canonical: str | None = None,
                       key: str | None = None, requisition_id: str | None = None, title: str | None = None,
                       company: str | None = None, location: str | None = None, pay_text: str | None = None,
                       published_at: str | None = None, updated_at: str | None = None, facts: dict | None = None) -> str:
    oid = f"obs:{uuid.uuid4().hex[:16]}"
    store.writer("discovery").insert("observations", {
        "observation_id": oid, "job_id": job_id, "lead_id": lead_id, "source_kind": source_kind,
        "evidence_level": evidence_level, "posting_key": key or posting_key(url), "requisition_id": requisition_id,
        "url": (url or "")[:500] or None, "canonical_url": canonical, "title": (title or "")[:200] or None,
        "company": (company or "")[:120] or None, "location": (location or "")[:120] or None,
        "pay_text": (pay_text or "")[:80] or None, "source_published_at": published_at, "source_updated_at": updated_at,
        "observed_at": store.clock.now(), "facts_json": json.dumps(facts or {}, sort_keys=True)})
    return oid


def link(store: Store, job_id: str, other_id: str, relation: str, evidence: str) -> None:
    if job_id == other_id or store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ? AND relation = ?",
                                       (job_id, other_id, relation)):
        return
    store.writer("discovery").insert("opening_links", {"job_id": job_id, "other_id": other_id, "relation": relation,
                                                       "evidence": evidence[:300], "created_at": store.clock.now()})


def _show(value) -> str:
    if value in (None, "", "[]"):
        return "none"
    return str(value)


def meaningful_changes(old, new: dict) -> list[dict]:
    """The suitability-relevant differences between a stored job row and freshly normalized fields."""
    out = []
    for column, label in MEANINGFUL_FIELDS.items():
        if column not in new:
            continue
        before, after = old[column] if column in old.keys() else None, new.get(column)
        if isinstance(before, float) or isinstance(after, float):
            same = (before is None and after is None) or (before is not None and after is not None and abs(float(before) - float(after)) < 0.5)
        else:
            same = (before or None) == (after or None)
        if not same:
            out.append({"field": column, "what": label, "before": _show(before), "after": _show(after)})
    return out


def record_change(store: Store, job_id: str, changes: list[dict], *, extra: str | None = None) -> bool:
    """Writes a job_changes row; returns True when the change is meaningful."""
    meaningful = bool(changes) or bool(extra)
    parts = sorted({c["what"] for c in changes})
    summary = "; ".join(([extra] if extra else []) + [
        f"{c['what']}: {c['before']} -> {c['after']}" for c in changes if c["what"] in parts][:6]) or "cosmetic edit only"
    store.writer("discovery").insert("job_changes", {"change_id": f"chg:{uuid.uuid4().hex[:16]}", "job_id": job_id,
                                                     "at": store.clock.now(), "meaningful": int(meaningful),
                                                     "fields_json": json.dumps(changes), "summary": summary[:500]})
    return meaningful


def requirement_digest(rows) -> set:
    """The parts of a posting's requirements that can change suitability (required qualifications, licences,
    schedule, travel, location), as comparable tuples."""
    return {(r["category"], r["name"], r["requirement_type"], r["years"]) for r in rows
            if r["category"] in REQUIREMENT_CATEGORIES or r["years"] is not None}


def describe_requirement_change(before: set, after: set) -> str | None:
    added, removed = sorted(after - before), sorted(before - after)
    if not added and not removed:
        return None
    fmt = lambda r: f"{r[1]}{f' {r[3]:g}y' if r[3] is not None else ''} ({r[2].replace('_', ' ')})"
    parts = ([f"added {', '.join(fmt(r) for r in added[:3])}"] if added else []) + \
            ([f"removed {', '.join(fmt(r) for r in removed[:3])}"] if removed else [])
    return "requirements " + "; ".join(parts)


def application_status(store: Store, job_id: str) -> dict:
    """{'applied': app row or None, 'possibly_applied': [job ids], 'dismissed': row or None} across the role group and
    opening links, so a rediscovered or reposted opening keeps its history."""
    from careeros.review.applications import role_group
    group = role_group(store, job_id) or [job_id]
    marks = ",".join("?" * len(group))
    applied = store.one(f"SELECT * FROM applications WHERE job_id IN ({marks}) ORDER BY applied_at DESC LIMIT 1", tuple(group))
    linked = {r["other_id"] for r in store.all(f"SELECT other_id FROM opening_links WHERE job_id IN ({marks}) AND relation != "
                                               f"'same_opening'", tuple(group))}
    linked |= {r["job_id"] for r in store.all(f"SELECT job_id FROM opening_links WHERE other_id IN ({marks}) AND relation != "
                                              f"'same_opening'", tuple(group))}
    possibly = sorted(j for j in linked - set(group)
                      if store.one("SELECT 1 FROM applications WHERE job_id = ?", (j,)))
    dismissed = store.one(f"SELECT * FROM dismissals WHERE job_id IN ({marks}) ORDER BY at DESC LIMIT 1", tuple(group))
    return {"applied": applied, "possibly_applied": possibly, "dismissed": dismissed}


def conflicts(store: Store, job) -> list[dict]:
    """Facts that weaker sources state differently from the live posting, with their provenance. Shown, never merged."""
    out = []
    for obs in store.all("SELECT * FROM observations WHERE job_id = ? AND evidence_level != 'live_ats' ORDER BY observed_at", (job["job_id"],)):
        if obs["title"] and obs["title"].strip().lower() != (job["title"] or "").strip().lower():
            out.append({"fact": "title", "source": obs["evidence_level"], "said": obs["title"], "at": obs["observed_at"]})
        if obs["pay_text"] and job["comp_max"] is None:
            out.append({"fact": "pay", "source": obs["evidence_level"], "said": obs["pay_text"], "at": obs["observed_at"]})
        if obs["location"] and "remote" in obs["location"].lower() and job["remote_type"] in ("onsite", "hybrid"):
            out.append({"fact": "remote", "source": obs["evidence_level"], "said": obs["location"], "at": obs["observed_at"]})
    return out
