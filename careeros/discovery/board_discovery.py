"""Verified employer-board discovery (2026-10-12, Parts 1, 2 and 9).

Turns recommendations, alert leads, manual leads and curated suggestions into employer -> board mappings on the four
allowed ATSs (Greenhouse, Lever, Ashby, Workable), using only their documented public listing endpoints.

Two separate decisions:
- EMPLOYER IDENTITY: is this board really this employer's? Evidence, strongest first:
    supplied_board_link    a recommendation, alert or person supplied a link on this exact board;
    official_domain        the employer's official domain appears in the board's own content or postings;
    board_name             the board's own display name is the employer (or a verified alias);
    description_name       the employer's full name appears in the board's posting texts.
  Verified = one strong kind (supplied link / official domain), or both name kinds. A matching job TITLE never
  counts. Anything less stays "unresolved" for you to confirm (`careeros sources verify`).
- JOB MATCHING: which posting on a verified board is the lead's job? Requisition id, then exact link, then title +
  location + seniority. Several candidates = "ambiguous" (reviewable), none = "no_match", and an old lead with no
  match = "posting_closed" (the board is still worth keeping).

Boundaries: discovery never opens LinkedIn / Indeed / Glassdoor / Workday / SmartRecruiters, sites the job seeker must not automate, or any
lead's original link, and never crawls websites. A link to an excluded system (e.g. Workday) becomes a manual-only
record. Every request goes through a client restricted to the four API hosts (redirects included) with a global and
per-provider budget; Retry-After is honoured. Staffing agencies are recorded as such and their clients are never
guessed. Excluded and held employers are skipped.

2026-10-13: discovery only ever produces CANDIDATES. The identity evidence above decides whether a lead's posting may
be graded; it never admits a board to the daily scan. Admission needs board verification against the employer's own
website (board_verifier: official-site handshake, quarantine, validation scan) or your expiring approval.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from careeros.core import private_sites
from careeros.core.clock import parse_utc
from careeros.normalize.posting import classify_family, contains_term, detect_country, detect_remote, norm

from .board_urls import BOARD_HOSTS, board_url, manual_system, parse_board_url
from .http import PROVIDER_HOSTS, FetchError, HttpClient, RequestBudget
from .seen_index import norm_company, norm_req

OUTCOME_RETRY_DAYS = {"slug_absent": 60, "board_empty": 14, "identity_unresolved": 30, "no_qualified_roles": 30,
                      "access_restricted": 90, "request_failed": 1, "budget_exhausted": 0, "verified": 30,
                      "manual_only": 365, "skipped_policy": 365}
_GENERIC_NAMES = {"health", "healthcare", "care", "group", "solutions", "services", "partners", "global", "united", "national",
                  "american", "clear", "simple", "one", "first", "prime", "plus", "pro", "the", "company", "consulting",
                  "systems", "technologies", "tech", "labs", "digital", "data", "medical", "pharmacy", "benefits", "insurance"}


def cfg(policy) -> dict:
    return {"enabled": False, "auto_activate": False, "slug_guessing": True, "max_slug_variants": 2, "daily_request_budget": 60,
            "per_provider_budget": 20, "curated_share": 0.2, "lookback_days": 90, "closed_after_days": 45,
            "retry_days": {}, "staffing_agencies": [], **(policy.get("source_discovery", default={}) or {})}


def retry_days(policy, outcome: str) -> float:
    return float({**OUTCOME_RETRY_DAYS, **(cfg(policy).get("retry_days") or {})}.get(outcome, 30))


@dataclass
class Candidate:
    employer_name: str
    origin: str                                   # lead | recommendation | alert | manual | curated | backfill
    official_domain: str | None = None
    board_url: str | None = None                  # an explicit link on a board (posting or board root)
    posting_url: str | None = None
    requisition_id: str | None = None
    title: str | None = None
    location: str | None = None
    lead_ids: list = field(default_factory=list)
    origin_ref: str | None = None
    relationship: str = "employer"
    first_seen_at: str | None = None
    evidence: list = field(default_factory=list)

    @property
    def employer_norm(self) -> str:
        return norm_company(self.employer_name)


# --- building candidates ---------------------------------------------------------------------------------
def is_staffing_agency(name: str, policy) -> bool:
    text = norm(name)
    agencies = [*cfg(policy)["staffing_agencies"], *private_sites.staffing_agencies()]   # private ones from the vault
    return any(contains_term(text, norm(a)) for a in agencies) or bool(
        re.search(r"\b(staffing|recruiting|recruitment|talent solutions)\b", text))


def candidate_from_lead(lead, policy) -> Candidate | None:
    """A lead (alert or recommendation) as a discovery candidate: its company, any board / domain hints in its Note,
    and the job facts needed for matching later. None when the lead names no company."""
    from careeros.discovery.note import context_note
    context = lead["context"] or ""
    company = re.sub(r"\s+", " ", context.split(" · ")[0]).strip() if context else ""
    if not company or len(company) > 100 or re.search(r"[@=<>]|https?:", company):
        return None
    note = context_note(context)["fields"]
    parts = context.split(" · ")
    sender = lead["sender"] or ""
    origin = "recommendation" if sender.startswith("GitHub inbox") else "alert"
    hint = note.get("board") or (lead["url"] if parse_board_url(lead["url"] or "") or manual_system(lead["url"] or "") else None)
    candidate = Candidate(employer_name=company, origin=origin, official_domain=note.get("domain"), board_url=hint,
                          posting_url=lead["url"], requisition_id=note.get("req"), title=lead["title"],
                          location=parts[1] if len(parts) > 1 else None, lead_ids=[lead["lead_id"]],
                          origin_ref=lead["lead_id"], first_seen_at=lead["first_seen_at"])
    if note.get("agency") or is_staffing_agency(company, policy):
        candidate.relationship = "staffing_agency"
    return candidate


def slug_variants(name: str, limit: int = 2) -> list[str]:
    """A few distinctive full-name board names: "Foo Health, Inc." -> foohealth, foo-health. Never shortened names, and
    nothing for a name too generic to identify one company ("Health Partners", "Clear")."""
    words = norm_company(name).split()
    words = [w for w in words if w != "the"]
    if not words or (len(words) == 1 and (words[0] in _GENERIC_NAMES or len(words[0]) < 5)):
        return []
    if all(w in _GENERIC_NAMES for w in words):
        return []
    variants = ["".join(words)]
    if len(words) > 1:
        variants.append("-".join(words))
    return [v for v in variants if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", v)][:max(1, limit)]


# --- identity -----------------------------------------------------------------------------------------------
def _aliases(store, employer_norm: str) -> set[str]:
    names = {employer_norm}
    for row in store.all("SELECT alias_norm, employer_norm FROM employer_aliases WHERE alias_norm = ? OR employer_norm = ?",
                         (employer_norm, employer_norm)):
        names |= {row["alias_norm"], row["employer_norm"]}
    return {n for n in names if n}


def identity(store, candidate: Candidate, ats: str, token: str, board_name: str | None, board_text: str,
             postings: list) -> tuple[str, list[dict]]:
    """('verified' | 'unresolved', evidence) for one board. Title matches are deliberately not evidence."""
    evidence = []
    if candidate.board_url and parse_board_url(candidate.board_url) == (ats, token):
        evidence.append({"kind": "supplied_board_link", "detail": candidate.board_url[:200]})
    domain = (candidate.official_domain or "").lower().removeprefix("www.")
    if domain:
        texts = [board_text] + [f"{p.description_text} {p.apply_url or ''} {p.url or ''}" for p in postings[:40]]
        if any(domain in (t or "").lower() for t in texts):
            evidence.append({"kind": "official_domain", "detail": domain})
    names = _aliases(store, candidate.employer_norm)
    if board_name and norm_company(board_name) in names:
        evidence.append({"kind": "board_name", "detail": board_name[:100]})
    full = [n for n in names if len(n.split()) >= 2 or len(n) >= 8]
    mentions = sum(1 for p in postings[:40] if any(contains_term(norm(p.description_text), n) for n in full))
    if mentions >= 2:
        evidence.append({"kind": "description_name", "detail": f"named in {mentions} posting texts"})
    kinds = {e["kind"] for e in evidence}
    verified = bool(kinds & {"supplied_board_link", "official_domain"}) or {"board_name", "description_name"} <= kinds
    return ("verified" if verified else "unresolved"), evidence


def relevant_roles(postings: list, policy) -> int:
    from careeros.normalize.posting import relevant_role
    families, count = policy.get("families", default={}), 0
    for p in postings:
        title = norm(p.title)
        remote = detect_remote(p.location, p.workplace_hint, p.description_text)
        if relevant_role(title, classify_family(title, families), remote, detect_country(p.location, p.description_text, remote), policy):
            count += 1
    return count


# --- job matching (separate from identity) ---------------------------------------------------------------------
def match_lead(candidate: Candidate, postings: list, policy, now: str) -> dict:
    from careeros.normalize.posting import seniority
    req = norm_req(candidate.requisition_id)
    if req:
        hits = [p for p in postings if norm_req(p.requisition_id) == req or norm_req(p.external_id) == req]
        if len(hits) == 1:
            return {"decision": "matched", "method": "requisition", "posting": hits[0], "candidates": [hits[0].external_id]}
    if candidate.posting_url:
        hits = [p for p in postings if p.url and p.url.rstrip("/") == candidate.posting_url.rstrip("/")]
        if len(hits) == 1:
            return {"decision": "matched", "method": "canonical_link", "posting": hits[0], "candidates": [hits[0].external_id]}
    title = norm(candidate.title or "")
    terms = policy.get("seniority_terms", default={})
    same = [p for p in postings if title and norm(p.title) == title]
    if candidate.location and "remote" in candidate.location.lower():
        same = [p for p in same if detect_remote(p.location, p.workplace_hint, p.description_text) in ("remote", "unknown")] or same
    same = [p for p in same if seniority(norm(p.title), terms) == seniority(title, terms)]
    if len(same) == 1 and not req:
        return {"decision": "matched", "method": "title_location", "posting": same[0], "candidates": [same[0].external_id]}
    if same:
        return {"decision": "ambiguous", "method": "title_location", "posting": None, "candidates": [p.external_id for p in same][:10]}
    old = candidate.first_seen_at and (parse_utc(now) - parse_utc(candidate.first_seen_at)) / timedelta(days=1) > cfg(policy)["closed_after_days"]
    return {"decision": "posting_closed" if old else "no_match", "method": "none", "posting": None, "candidates": []}


# --- running one candidate ---------------------------------------------------------------------------------------
def _recent(store, employer_norm: str, ats: str | None, slug: str | None, policy, now: str):
    row = store.one("SELECT * FROM discovery_attempts WHERE employer_norm = ? AND COALESCE(ats,'') = ? AND COALESCE(slug,'') = ? "
                    "ORDER BY attempted_at DESC, rowid DESC LIMIT 1", (employer_norm, ats or "", slug or ""))
    if not row:
        return None
    if row["retry_after"]:  # the server said when to come back: that replaces the outcome's default window
        return row if row["retry_after"] > now else None
    age = (parse_utc(now) - parse_utc(row["attempted_at"])) / timedelta(days=1)
    return row if age < retry_days(policy, row["outcome"]) else None


def _attempt(store, candidate: Candidate, outcome: str, *, ats=None, slug=None, detail=None, requests=0, retry_after=None):
    store.writer("discovery").insert("discovery_attempts", {
        "attempt_id": f"att:{uuid.uuid4().hex[:16]}", "employer_norm": candidate.employer_norm, "ats": ats, "slug": slug,
        "outcome": outcome, "detail": (detail or "")[:300] or None, "requests": requests, "origin": candidate.origin,
        "attempted_at": store.clock.now(), "retry_after": retry_after})


def _upsert_mapping(store, candidate: Candidate, ats: str, token: str, *, collection: str, status: str, evidence: list,
                    relevant: int | None, verified_method: str | None) -> str:
    mapping_id = f"{ats}:{token}"
    now = store.clock.now()
    existing = store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,))
    writer = store.writer("discovery")
    from . import board_registry as registry
    entity_id = registry.ensure_entity(store, candidate.employer_name, domain=candidate.official_domain, origin=candidate.origin)
    if existing:
        merged = {json.dumps(e, sort_keys=True) for e in json.loads(existing["identity_evidence"] or "[]")} | \
                 {json.dumps(e, sort_keys=True) for e in evidence}
        keep_verified = existing["identity_status"] == "verified"  # never downgrade a verified mapping on weaker news
        values = {"identity_evidence": json.dumps([json.loads(e) for e in sorted(merged)]), "relevant_roles": relevant,
                  "updated_at": now}
        if not keep_verified and status == "verified":
            values.update(identity_status="verified", verified_by="careeros", verified_method=verified_method, verified_at=now)
        if candidate.official_domain and not existing["official_domain"]:
            values["official_domain"] = candidate.official_domain
        if not existing["entity_id"]:
            values["entity_id"] = entity_id
        writer.update("board_mappings", values, "mapping_id = ?", (mapping_id,))
        return mapping_id
    writer.insert("board_mappings", {
        "mapping_id": mapping_id, "employer_norm": candidate.employer_norm, "employer_name": candidate.employer_name[:100],
        "official_domain": candidate.official_domain, "ats": ats, "board_token": token, "board_url": board_url(ats, token),
        "collection": collection, "identity_status": status, "identity_evidence": json.dumps(evidence),
        "verified_by": "careeros" if status == "verified" else None, "verified_method": verified_method if status == "verified" else None,
        "verified_at": now if status == "verified" else None, "relationship": candidate.relationship, "origin": candidate.origin,
        "origin_ref": candidate.origin_ref, "relevant_roles": relevant, "activated_at": None, "created_at": now, "updated_at": now,
        "entity_id": entity_id, "provenance": "discovery",
        "verification_status": "manual_only" if collection == "manual_only" else "candidate"})
    return mapping_id


def _fetch_board(client: HttpClient, ats: str, token: str) -> tuple[list, str | None, str]:
    """(postings, board display name, board text) using only the documented public listing endpoints."""
    from .boards import FETCHERS, _token
    name, text = None, ""
    if ats == "greenhouse":
        meta = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}")
        name, text = (meta or {}).get("name"), (meta or {}).get("content") or ""
    if ats == "workable":
        data = client.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{_token(token)}?details=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FetchError("unexpected_shape")
        from .boards import workable_posting
        postings = [workable_posting(token, item) for item in data["jobs"] if item.get("shortcode")]
        return postings, data.get("name"), f"{data.get('description') or ''} {data.get('url') or data.get('website') or ''}"
    return FETCHERS[ats](client, token), name, text


def discovery_client(policy, budget: RequestBudget, transport=None, sleep=None) -> HttpClient:
    kwargs = {"transport": transport} if transport else {}
    if sleep:
        kwargs["sleep"] = sleep
    return HttpClient(min_interval=0.6, allowed_hosts=set(PROVIDER_HOSTS), budget=budget, **kwargs)


def discover(ctx, candidate: Candidate, client: HttpClient, *, now: str | None = None) -> dict:
    """Run discovery for one candidate. Returns {'outcome', 'mappings': [...], 'requests', 'matches': n}."""
    from careeros.discovery import sources
    from careeros.qualify.gate import excluded_employer
    store, policy = ctx.store, ctx.policy
    now = now or store.clock.now()
    start = client.requests
    result = {"outcome": None, "mappings": [], "requests": 0, "matches": 0}

    held = {norm_company(p) for p in policy.get("gate", "held_parents", default=[]) or []}
    if excluded_employer({"employer_name": candidate.employer_name}, policy) or candidate.employer_norm in held:
        _attempt(store, candidate, "skipped_policy", detail="excluded or held employer")
        result["outcome"] = "skipped_policy"
        return result
    if candidate.relationship == "staffing_agency":
        _attempt(store, candidate, "skipped_policy", detail="staffing agency: the hiring client is not disclosed, never guessed")
        result["outcome"] = "skipped_policy"
        return result

    manual = manual_system(candidate.board_url or "")
    if manual:  # Workday / SmartRecruiters / ... : a useful manual-source record, never automatic collection
        host = urlsplit(candidate.board_url).netloc.lower()
        mapping = _upsert_mapping(store, candidate, manual, host, collection="manual_only", status="unresolved",
                                  evidence=[{"kind": "supplied_board_link", "detail": candidate.board_url[:200]}],
                                  relevant=None, verified_method=None)
        _attempt(store, candidate, "manual_only", ats=manual, slug=host, detail="excluded system: open it yourself")
        result.update(outcome="manual_only", mappings=[mapping])
        return result

    explicit = parse_board_url(candidate.board_url or "")
    if explicit:
        targets = [explicit]
    elif cfg(policy)["slug_guessing"]:
        targets = [(ats, slug) for slug in slug_variants(candidate.employer_name, int(cfg(policy)["max_slug_variants"]))
                   for ats in BOARD_HOSTS if sources.status(ats) == "allowed"]
        if not targets:
            _attempt(store, candidate, "skipped_policy", detail="name too generic to guess a board safely")
            result["outcome"] = "skipped_policy"
            return result
    else:
        targets = []
    outcomes = []
    for ats, token in targets:
        if sources.status(ats) != "allowed":
            continue
        if _recent(store, candidate.employer_norm, ats, token, policy, now):
            outcomes.append("cached")
            continue
        before = client.requests
        try:
            postings, name, text = _fetch_board(client, ats, token)
        except FetchError as exc:
            if exc.code == "budget_exhausted":
                _attempt(store, candidate, "budget_exhausted", ats=ats, slug=token, requests=client.requests - before)
                outcomes.append("budget_exhausted")
                break
            outcome = {"not_found": "slug_absent", "access_restricted": "access_restricted"}.get(exc.code, "request_failed")
            retry_at = None
            if exc.retry_after:
                retry_at = (parse_utc(now) + timedelta(seconds=exc.retry_after)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            _attempt(store, candidate, outcome, ats=ats, slug=token, detail=exc.code, requests=client.requests - before,
                     retry_after=retry_at)
            outcomes.append(outcome)
            continue
        if not postings:
            _attempt(store, candidate, "board_empty", ats=ats, slug=token, requests=client.requests - before)
            outcomes.append("board_empty")
            continue
        status, evidence = identity(store, candidate, ats, token, name, text, postings)
        relevant = relevant_roles(postings, policy)
        method = ",".join(sorted({e["kind"] for e in evidence})) or None
        mapping = _upsert_mapping(store, candidate, ats, token, collection="allowed", status=status, evidence=evidence,
                                  relevant=relevant, verified_method=method)
        result["mappings"].append(mapping)
        verified = store.one("SELECT identity_status FROM board_mappings WHERE mapping_id = ?", (mapping,))["identity_status"] == "verified"
        outcome = ("verified" if relevant else "no_qualified_roles") if verified else "identity_unresolved"
        _attempt(store, candidate, outcome, ats=ats, slug=token, requests=client.requests - before,
                 detail=f"{len(postings)} postings, {relevant} relevant; evidence: {method or 'none'}")
        outcomes.append(outcome)
        for lead_id in candidate.lead_ids:
            result["matches"] += _record_match(ctx, candidate, lead_id, mapping, postings, verified, now)
        # a candidate only: admission to the daily scan is board_verifier's decision (the employer's own site first)
        if verified:
            break  # one verified board per employer is enough
    order = ["verified", "no_qualified_roles", "identity_unresolved", "manual_only", "board_empty", "access_restricted",
             "request_failed", "budget_exhausted", "slug_absent", "cached"]
    result["outcome"] = next((o for o in order if o in outcomes), "slug_absent" if targets else "skipped_policy")
    result["requests"] = client.requests - start
    return result


def _record_match(ctx, candidate: Candidate, lead_id: str, mapping_id: str, postings: list, verified: bool, now: str) -> int:
    """Job matching, kept apart from identity. A match on a verified board grades the lead's job (one posting read
    from an allowed source, already in hand); on an unresolved board it is only recorded for review."""
    from careeros.pipeline import ingest
    store = ctx.store
    match = match_lead(candidate, postings, ctx.policy, now)
    job_id = None
    if match["decision"] == "matched" and verified:
        posting = match["posting"]
        mapping = store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,))
        employer = store.one("SELECT * FROM employers WHERE ats = ? AND board_token = ?", (mapping["ats"], mapping["board_token"]))
        from careeros.discovery.saved import parent_for
        owner = dict(employer) if employer else {"employer_id": None, "name": mapping["employer_name"],
                                                 "parent": parent_for(mapping["employer_name"], ctx.policy)}
        ingest(ctx, owner, [posting], source="ats_api" if employer else "manual_url", full_board=False)
        job_id = f"{posting.ats}:{posting.board_token}:{posting.external_id}"
        if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)):
            store.writer("discovery").update("leads", {"status": "ingested", "job_id": job_id}, "lead_id = ? AND job_id IS NULL", (lead_id,))
        else:
            job_id = None
    store.writer("discovery").insert("lead_matches", {
        "match_id": f"lm:{uuid.uuid4().hex[:16]}", "lead_id": lead_id, "job_id": job_id, "mapping_id": mapping_id,
        "method": match["method"], "decision": match["decision"] if verified or match["decision"] != "matched" else "ambiguous",
        "candidates_json": json.dumps(match["candidates"]), "decided_at": now, "audit": None, "audited_at": None})
    return int(job_id is not None)


def activate(store, mapping_id: str, *, actor: str = "user") -> str:
    """Put a board into the daily scan. Only a board that passed verification against the employer's own site AND its
    validation scan (or one you approved, after its validation scan) can be activated; manual-only systems never."""
    from . import board_registry as registry
    mapping = registry.mapping(store, mapping_id)
    if not mapping:
        raise ValueError("unknown_mapping")
    if mapping["collection"] != "allowed":
        raise ValueError("manual_only: this board is on a system CareerOS never reads automatically")
    if mapping["verification_status"] == "quarantined" and mapping["validation_status"] == "passed":
        registry.update_board(store, mapping_id, {"verification_status": "active"}, component="admin")
    elif mapping["verification_status"] != "active":
        raise ValueError(f"not verified ({mapping['verification_status']}): the employer's own site must link to this board "
                         f"(careeros sources check {mapping_id}) or you approve it (careeros sources approve {mapping_id} --reason ...)")
    return registry.admit(store, mapping_id, via="override" if mapping["override_reason"] else "verified", actor=actor)


def verify(store, mapping_id: str, *, domain: str | None = None, note: str | None = None, actor: str = "user",
           days: float = 90) -> str:
    """A person vouches for a board: an expiring manual approval (board_registry.approve)."""
    from . import board_registry as registry
    return registry.approve(store, mapping_id, reason=note or f"confirmed by {actor}", days=days, actor=actor, domain=domain)


def reject(store, mapping_id: str, *, note: str | None = None, actor: str = "user") -> None:
    mapping = store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,))
    if not mapping:
        raise ValueError("unknown_mapping")
    evidence = json.loads(mapping["identity_evidence"] or "[]") + [{"kind": "human_rejected", "detail": (note or actor)[:200]}]
    store.writer("admin").update("board_mappings", {"identity_status": "rejected", "identity_evidence": json.dumps(evidence),
                                                    "verification_status": "rejected", "verification_failure": "known_unrelated",
                                                    "paused_reason": f"rejected by {actor}", "updated_at": store.clock.now()},
                                 "mapping_id = ?", (mapping_id,))


def add_alias(store, alias: str, employer: str, relation: str = "alias", *, actor: str = "user", evidence: str | None = None) -> None:
    if relation not in ("alias", "parent", "subsidiary", "former", "legal"):
        raise ValueError("relation must be alias, parent, subsidiary, former or legal")
    row = store.one("SELECT entity_id FROM employer_entities WHERE canonical_norm = ? ORDER BY created_at LIMIT 1", (norm_company(employer),))
    store.writer("admin").insert("employer_aliases", {"alias_norm": norm_company(alias), "employer_norm": norm_company(employer),
                                                      "relation": relation, "verified_by": actor, "verified_at": store.clock.now(),
                                                      "evidence": (evidence or "")[:200] or None,
                                                      "entity_id": row["entity_id"] if row else None})
