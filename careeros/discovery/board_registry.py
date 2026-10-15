"""Employer records and the board lifecycle (2026-10-13).

EMPLOYER (employer_entities, a stable id never derived from a name or a board token)
  -> CAREER SITE (career_sites: the employer-controlled pages a verification actually read)
  -> ATS BOARD (board_mappings: one row per board; an employer may have several, and former ones are kept)
  -> JOBS (jobs, via the employers row the daily scan uses)

Board lifecycle (board_mappings.verification_status):
  candidate -> quarantined -> active                       the normal path (verification, then a validation scan)
  manual_only | unsupported                                a system CareerOS never reads automatically
  review_required | verification_required | rejected       waiting for evidence, for you, or refused
  historical | retired                                     kept for history (e.g. after an ATS migration)
`paused_reason` set = automatic ingestion paused, whatever the status.

Admission (scan_gate): a board is scanned automatically only when it is active, or when it is a LEGACY board (scanned
before verification existed) that is waiting for re-verification and has not failed. Nothing is ever marked verified
just because it already existed. Similar names never merge employers: a shared official domain does; a shared name with
a different domain becomes a separate employer and a review case.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from careeros.core.clock import parse_utc
from careeros.core.fingerprint import fingerprint
from careeros.normalize.posting import contains_term, norm

from .seen_index import norm_company

LEGACY_GRACE_STATUSES = ("verification_required", "review_required", "candidate", "quarantined")
# Problems no manual override may bypass.
HARD_FAILURES = {"identity_mismatch", "unsafe_url", "unsafe_redirect", "private_network", "unsupported_protocol",
                 "malformed_url", "known_unrelated", "board_compromised", "official_site_contradicts"}
DEFAULTS = {"enforce": True, "fetch_official_sites": False, "daily_site_requests": 40, "ats_request_budget": 40,
            "max_pages_per_employer": 8, "max_verifications_per_run": 5, "max_reverifications_per_run": 5,
            "max_validations_per_run": 10, "max_boards_per_employer": 3, "legacy_grace_days": None, "auto_trust": 85,
            "review_trust": 65, "override_days": 90, "reverify_days": {"new": 7, "stable": 45, "long_term": 90},
            "candidate_retry_days": 30, "site_failures_before_review": 3}


def bcfg(policy) -> dict:
    return {**DEFAULTS, **(policy.get("board_verification", default={}) or {})}


def site_checks_enabled(policy) -> bool:
    """Official-site checks need both your switch (policy) and the site policy's permission."""
    from .sources import status
    return bool(bcfg(policy)["fetch_official_sites"]) and status("employer_site") == "allowed"


def iso(moment) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plus_days(now: str, days: float) -> str:
    return iso(parse_utc(now) + timedelta(days=days))


def clean_domain(value: str | None) -> str | None:
    """'https://www.Example.com/careers' -> 'example.com'; None for anything that is not a public host name."""
    from .safe_fetch import SiteFetchError, check_url
    text = (value or "").strip().lower()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        host = check_url(text.replace("http://", "https://", 1).split("#")[0])
    except SiteFetchError:
        return None
    return host.removeprefix("www.")


def board_key(ats: str, token: str) -> str:
    return f"{ats}:{(token or '').lower()}"


# --- employers ---------------------------------------------------------------------------------------------------
def _event(store, component: str, event_type: str, outcome: str, entity_id: str | None, **metadata) -> None:
    store.writer(component).event(event_type, outcome=outcome, entity_type="employer_board", entity_id=entity_id,
                                  metadata={k: v for k, v in metadata.items() if v is not None})


def entity(store, entity_id: str | None):
    return store.one("SELECT * FROM employer_entities WHERE entity_id = ?", (entity_id,)) if entity_id else None


def entity_names(store, entity_id: str) -> set[str]:
    row = entity(store, entity_id)
    if not row:
        return set()
    names = {row["canonical_norm"]}
    for alias in store.all("SELECT alias_norm, employer_norm FROM employer_aliases WHERE entity_id = ? OR employer_norm = ? "
                           "OR alias_norm = ?", (entity_id, row["canonical_norm"], row["canonical_norm"])):
        names |= {alias["alias_norm"], alias["employer_norm"]}
    return {n for n in names if n}


def ensure_entity(store, name: str, *, domain: str | None = None, origin: str | None = None) -> str:
    """The employer record for `name` (+ official domain candidate). A shared official domain is the same employer;
    a shared name is the same employer only when no official domain contradicts it."""
    name_norm, domain = norm_company(name), clean_domain(domain)
    now, writer = store.clock.now(), store.writer("discovery")
    if domain:
        same = store.one("SELECT * FROM employer_entities WHERE official_domain = ? ORDER BY created_at LIMIT 1", (domain,))
        if same:
            if name_norm and name_norm != same["canonical_norm"] and not store.one(
                    "SELECT 1 FROM employer_aliases WHERE alias_norm = ? AND employer_norm = ?", (name_norm, same["canonical_norm"])):
                writer_admin = store.writer("admin")
                writer_admin.insert("employer_aliases", {"alias_norm": name_norm, "employer_norm": same["canonical_norm"],
                                                         "relation": "alias", "verified_by": "careeros:same_domain",
                                                         "verified_at": now, "evidence": f"same official domain {domain}",
                                                         "entity_id": same["entity_id"]})
            return same["entity_id"]
    rows = store.all("SELECT * FROM employer_entities WHERE canonical_norm = ? ORDER BY created_at", (name_norm,))
    compatible = [r for r in rows if not domain or not r["official_domain"] or r["official_domain"] == domain]
    if compatible:
        chosen = compatible[0]
        if domain and not chosen["official_domain"]:
            writer.update("employer_entities", {"official_domain": domain, "updated_at": now}, "entity_id = ?", (chosen["entity_id"],))
        if len(compatible) > 1:
            open_case(store, entity_id=chosen["entity_id"], reason_code="identity_ambiguous",
                      detail=f"{len(compatible)} employer records share the name {name!r}; confirm which one this is")
        return chosen["entity_id"]
    entity_id = f"ent:{uuid.uuid4().hex[:16]}"
    writer.insert("employer_entities", {
        "entity_id": entity_id, "canonical_name": (name or name_norm)[:120], "canonical_norm": name_norm, "official_domain": domain,
        "domain_status": "unverified", "domain_verified_at": None, "primary_careers_url": None, "parent_entity_id": None,
        "career_fit_score": None, "career_priority": None, "first_seen_at": now, "first_relevant_at": None,
        "last_relevant_at": None, "created_at": now, "updated_at": now})
    _event(store, "discovery", "employer_discovered", "informational", entity_id, name=name[:120], domain=domain, origin=origin)
    if rows:  # same name, different official domain: never merged
        open_case(store, entity_id=entity_id, reason_code="identity_ambiguous",
                  detail=f"another employer named {name!r} uses {rows[0]['official_domain']}; this one uses {domain}")
    return entity_id


# --- boards ----------------------------------------------------------------------------------------------------------
def mapping(store, mapping_id: str | None):
    return store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,)) if mapping_id else None


def mapping_for_board(store, ats: str, token: str):
    return store.one("SELECT * FROM board_mappings WHERE ats = ? AND lower(board_token) = lower(?) "
                     "ORDER BY (verification_status = 'active') DESC, created_at LIMIT 1", (ats, token))


def mapping_for_employer(store, employer):
    return mapping(store, employer["mapping_id"]) or mapping_for_board(store, employer["ats"], employer["board_token"])


def ensure_mapping(store, entity_id: str, ats: str, token: str, *, name: str, provenance: str, origin: str,
                   board_url: str, collection: str = "allowed", status: str = "candidate", domain: str | None = None) -> str:
    existing = mapping_for_board(store, ats, token) if collection == "allowed" else mapping(store, f"{ats}:{token}")
    now = store.clock.now()
    if existing:
        if not existing["entity_id"]:
            store.writer("discovery").update("board_mappings", {"entity_id": entity_id, "updated_at": now}, "mapping_id = ?",
                                             (existing["mapping_id"],))
        return existing["mapping_id"]
    mapping_id = f"{ats}:{token}"
    store.writer("discovery").insert("board_mappings", {
        "mapping_id": mapping_id, "employer_norm": norm_company(name), "employer_name": name[:100], "official_domain": domain,
        "ats": ats, "board_token": token, "board_url": board_url, "collection": collection, "identity_status": "unresolved",
        "identity_evidence": "[]", "relationship": "employer", "origin": origin, "origin_ref": None, "relevant_roles": None,
        "activated_at": None, "created_at": now, "updated_at": now, "entity_id": entity_id, "verification_status": status,
        "provenance": provenance})
    return mapping_id


def update_board(store, mapping_id: str, values: dict, *, component: str = "discovery") -> None:
    store.writer(component).update("board_mappings", {**values, "updated_at": store.clock.now()}, "mapping_id = ?", (mapping_id,))


def sync_legacy(store) -> int:
    """Every employers row gets an employer record and a board mapping. Rows scanned before verification existed
    become legacy boards awaiting re-verification (never verified by default); any other row is a candidate."""
    created = 0
    for row in store.all("SELECT * FROM employers"):
        entity_id = row["entity_id"]
        existing = mapping_for_employer(store, row)
        if not entity_id:
            entity_id = (existing["entity_id"] if existing and existing["entity_id"] else
                         ensure_entity(store, row["name"], domain=existing["official_domain"] if existing else None, origin="legacy"))
        if existing is None:
            legacy = row["admitted_via"] == "legacy"
            mapping_id = ensure_mapping(store, entity_id, row["ats"], row["board_token"], name=row["name"],
                                        provenance="legacy_unknown" if legacy else "job_link", origin="legacy" if legacy else "job_link",
                                        board_url=_board_url(row["ats"], row["board_token"]),
                                        status="verification_required" if legacy else "candidate")
            if legacy:
                update_board(store, mapping_id, {"activated_at": row["verified_at"] or store.clock.now()}, component="admin")
            created += 1
        else:
            mapping_id = existing["mapping_id"]
            if not existing["entity_id"]:
                update_board(store, mapping_id, {"entity_id": entity_id})
        if row["entity_id"] != entity_id or row["mapping_id"] != mapping_id:
            store.writer("admin").update("employers", {"entity_id": entity_id, "mapping_id": mapping_id}, "employer_id = ?",
                                         (row["employer_id"],))
    return created


def _board_url(ats: str, token: str) -> str:
    from .board_urls import board_url
    return board_url(ats, token)


# --- admission -------------------------------------------------------------------------------------------------------
def scan_gate(store, employer, policy, now: str | None = None) -> tuple[bool, str]:
    """(scan this board automatically?, why)."""
    cfg = bcfg(policy)
    if not cfg["enforce"]:
        return True, "verification not enforced"
    m = mapping_for_employer(store, employer)
    if m is None:
        return (employer["admitted_via"] == "legacy"), ("legacy board" if employer["admitted_via"] == "legacy" else "not verified")
    if m["paused_reason"]:
        return False, f"paused: {m['paused_reason']}"
    if m["verification_status"] == "active":
        return True, "verified"
    if m["provenance"] == "legacy_unknown" and m["verification_status"] in LEGACY_GRACE_STATUSES:
        grace = cfg["legacy_grace_days"]
        if grace is not None and now and (parse_utc(now) - parse_utc(m["created_at"])) / timedelta(days=1) > float(grace):
            return False, "legacy grace period ended without verification"
        return True, "legacy board awaiting re-verification"
    return False, m["verification_status"]


def pause(store, mapping_id: str, reason: str, *, status: str = "verification_required", failure: str | None = None,
          event: str = "employer_reverification_required") -> None:
    m = mapping(store, mapping_id)
    if m is None or (m["paused_reason"] == reason and m["verification_status"] == status):
        return
    values = {"paused_reason": reason[:200], "verification_status": status, "reverify_at": store.clock.now()}
    if failure:
        values["verification_failure"] = failure
    update_board(store, mapping_id, values)
    _event(store, "discovery", event, "failure", mapping_id, reason=reason[:200], previous=m["verification_status"])
    open_case(store, mapping_id=mapping_id, entity_id=m["entity_id"], reason_code=failure or "verification_required", detail=reason)


# --- review cases -----------------------------------------------------------------------------------------------------
def open_case(store, *, reason_code: str, detail: str, mapping_id: str | None = None, entity_id: str | None = None) -> str | None:
    if store.one("SELECT 1 FROM board_reviews WHERE status = 'open' AND reason_code = ? AND COALESCE(mapping_id,'') = ? "
                 "AND COALESCE(entity_id,'') = ?", (reason_code, mapping_id or "", entity_id or "")):
        return None
    case_id = f"rev:{uuid.uuid4().hex[:12]}"
    store.writer("discovery").insert("board_reviews", {
        "case_id": case_id, "entity_id": entity_id, "mapping_id": mapping_id, "reason_code": reason_code, "detail": detail[:400],
        "created_at": store.clock.now(), "status": "open", "resolved_at": None, "resolution": None})
    _event(store, "discovery", "employer_review_required", "informational", mapping_id or entity_id, reason=reason_code)
    return case_id


def resolve_cases(store, *, resolution: str, mapping_id: str | None = None, case_id: str | None = None,
                  reasons: tuple | None = None, entity_id: str | None = None, component: str = "discovery") -> int:
    where, params = ["status = 'open'"], []
    if case_id:
        where.append("case_id = ?")
        params.append(case_id)
    if mapping_id:
        where.append("mapping_id = ?")
        params.append(mapping_id)
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    if reasons:
        where.append(f"reason_code IN ({','.join('?' for _ in reasons)})")
        params.extend(reasons)
    return store.writer(component).update("board_reviews", {"status": "resolved", "resolved_at": store.clock.now(),
                                                            "resolution": resolution[:300]}, " AND ".join(where), tuple(params))


# --- identity signals and fingerprints ----------------------------------------------------------------------------------
def identity_shares(postings: list, names: set[str], domain: str | None) -> dict:
    """How strongly the board's own postings point at the employer: the share naming it, and the share naming (or
    linking to) its official domain."""
    total = len(postings[:60])
    if not total:
        return {"postings": 0, "name_share": 0.0, "domain_share": 0.0}
    usable = [n for n in names if len(n) >= 4]
    named = linked = 0
    for p in postings[:60]:
        text = norm(p.description_text or "")
        if any(contains_term(text, n) for n in usable):
            named += 1
        blob = f"{p.description_text or ''} {p.url or ''} {p.apply_url or ''}".lower()
        if domain and domain in blob:
            linked += 1
    return {"postings": total, "name_share": round(named / total, 2), "domain_share": round(linked / total, 2)}


def drift(baseline: dict | None, current: dict, board_name: str | None = None) -> str | None:
    """A reason when the board no longer looks like the same employer's board, else None."""
    if not baseline:
        return None
    if board_name and baseline.get("board_name") and norm_company(board_name) != norm_company(baseline["board_name"]):
        return f"board name changed from {baseline['board_name']!r} to {board_name!r}"
    strong = baseline.get("postings", 0) >= 3 and max(baseline.get("name_share", 0), baseline.get("domain_share", 0)) >= 0.5
    if strong and current.get("postings", 0) >= 3 and current.get("name_share", 0) == 0 and current.get("domain_share", 0) == 0:
        return "no posting names the employer or its website any more"
    return None


def check_drift(store, employer, postings: list) -> str | None:
    """Run on every scan before anything is ingested: a reason when the board no longer looks like the employer's
    (it then pauses), else None. The first scan of a board without a baseline records one."""
    m = mapping_for_employer(store, employer)
    if m is None or not m["entity_id"]:
        return None
    ent = entity(store, m["entity_id"])
    shares = identity_shares(postings, entity_names(store, m["entity_id"]), m["official_domain"] or (ent["official_domain"] if ent else None))
    baseline = json.loads(m["identity_signals"] or "null")
    if baseline is None:
        if shares["postings"] >= 3:
            update_board(store, m["mapping_id"], {"identity_signals": json.dumps({**shares, "board_name": None, "source": "first_scan"})})
        return None
    reason = drift(baseline, shares)
    if reason is None and shares["postings"] >= max(3, baseline.get("postings", 0)) and \
            max(shares["name_share"], shares["domain_share"]) >= max(baseline.get("name_share", 0), baseline.get("domain_share", 0)):
        # a fuller scan that looks at least as much like the employer strengthens the baseline (it never weakens it)
        update_board(store, m["mapping_id"], {"identity_signals": json.dumps({**baseline, **shares})})
    return reason


def board_fingerprint(ats: str, token: str, *, entity_id: str | None, board_name: str | None, domain: str | None,
                      shares: dict) -> str:
    return fingerprint({"ats": ats, "token": (token or "").lower(), "entity": entity_id, "board_name": norm_company(board_name or ""),
                        "domain": domain, "names_employer": shares.get("name_share", 0) >= 0.5,
                        "links_domain": shares.get("domain_share", 0) > 0})[:24]


# --- manual overrides ----------------------------------------------------------------------------------------------------
def approve(store, mapping_id: str, *, reason: str, days: float = 90, actor: str = "user", domain: str | None = None) -> str:
    """A person vouches for a board (manual override). It expires; it never bypasses a hard failure (an employer
    mismatch, an unsafe address, a contradicting official site); a manual-only system is never scanned. Returns the new
    status: quarantined (a validation scan still runs) or active (validation already passed)."""
    m = mapping(store, mapping_id)
    if m is None:
        raise ValueError("unknown_mapping")
    if m["collection"] != "allowed":
        raise ValueError("manual_only: this system is never read automatically")
    if m["identity_check"] == "mismatch" or (m["verification_failure"] or "") in HARD_FAILURES:
        raise ValueError(f"cannot override: {m['verification_failure'] or 'identity_mismatch'} (a hard failure)")
    if m["verification_status"] in ("historical", "retired"):
        raise ValueError(f"cannot override a {m['verification_status']} board")
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    now = store.clock.now()
    status = "active" if m["validation_status"] == "passed" else "quarantined"
    values = {"override_reason": reason.strip()[:200], "override_by": actor, "override_at": now,
              "override_prev_status": m["verification_status"], "override_expires_at": plus_days(now, days),
              "verification_status": status, "paused_reason": None, "identity_status": "verified", "verified_by": actor,
              "verified_method": "human_override", "verified_at": now}
    if status == "quarantined":
        values.update(quarantined_at=now, validation_status="pending")
    if domain and clean_domain(domain):
        values["official_domain"] = clean_domain(domain)
    update_board(store, mapping_id, values, component="admin")
    store.writer("admin").event("manual_override_created", outcome="success", entity_type="employer_board", entity_id=mapping_id,
                                actor_type="human", actor_id=actor,
                                metadata={"reason": reason.strip()[:200], "previous": m["verification_status"], "new": status,
                                          "expires_at": values["override_expires_at"]})
    if status == "active":
        admit(store, mapping_id, via="override", actor=actor)
    return status


def expire_overrides(store) -> int:
    now = store.clock.now()
    expired = 0
    for m in store.all("SELECT * FROM board_mappings WHERE override_expires_at IS NOT NULL AND override_expires_at <= ?", (now,)):
        legacy = m["provenance"] == "legacy_unknown"
        update_board(store, m["mapping_id"], {
            "verification_status": "verification_required", "reverify_at": now,
            "paused_reason": None if legacy else "manual override expired: re-verification needed",
            "override_reason": None, "override_by": None, "override_at": None, "override_prev_status": None,
            "override_expires_at": None}, component="admin")
        store.writer("admin").event("manual_override_expired", outcome="informational", entity_type="employer_board",
                                    entity_id=m["mapping_id"], metadata={"reason": m["override_reason"], "by": m["override_by"]})
        open_case(store, mapping_id=m["mapping_id"], entity_id=m["entity_id"], reason_code="override_expired",
                  detail=f"your approval ({m['override_reason']}) expired; the board is re-verified before it is scanned again")
        expired += 1
    return expired


# --- activation and retirement ---------------------------------------------------------------------------------------------
def admit(store, mapping_id: str, *, via: str, actor: str) -> str:
    """Put an active board into the daily scan (an employers row)."""
    m = mapping(store, mapping_id)
    if m is None:
        raise ValueError("unknown_mapping")
    if m["verification_status"] != "active" or m["paused_reason"] or m["collection"] != "allowed":
        raise ValueError(f"not active: {m['verification_status']}")
    employer_id = f"{m['ats']}:{m['board_token']}"
    now = store.clock.now()
    existing = store.one("SELECT * FROM employers WHERE ats = ? AND lower(board_token) = lower(?)", (m["ats"], m["board_token"]))
    with store.tx():
        if existing is None:
            store.writer("admin").insert("employers", {
                "employer_id": employer_id, "name": m["employer_name"], "parent": m["employer_name"], "ats": m["ats"],
                "board_token": m["board_token"], "industry": None, "status": "candidate", "mapping_id": mapping_id,
                "entity_id": m["entity_id"], "admitted_via": via, "last_evidence_at": now})
            store.writer("admin").event("employer_added", outcome="success", entity_type="employer", entity_id=employer_id,
                                        metadata={"ats": m["ats"], "status": "candidate", "via": via, "actor": actor})
        else:
            employer_id = existing["employer_id"]
            values = {"mapping_id": mapping_id, "entity_id": m["entity_id"]}
            if existing["admitted_via"] != "legacy":
                values["admitted_via"] = via
            if existing["status"] in ("invalid", "disabled"):
                values["status"] = "candidate"
            store.writer("admin").update("employers", values, "employer_id = ?", (employer_id,))
        update_board(store, mapping_id, {"activated_at": now}, component="admin")
    return employer_id


def json_list(value) -> list:
    try:
        return json.loads(value or "[]")
    except ValueError:
        return []
