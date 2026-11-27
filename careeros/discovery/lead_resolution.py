"""Lead resolution (2026-11-02, the owner's decision D68; ADR 0003 and ADR 0001, amendments of 2026-11-02).

A lead is a job seen only in an alert or a recommendation (LinkedIn, Indeed, any site CareerOS never reads). CareerOS never
opens its link; it finds the employer's own posting instead:
- ONLY THROUGH ADMITTED BOARDS. A lead is linked to a job only when the job's board is one the daily scan admits
  (board_registry.scan_gate) and the lead's employer and the board's employer resolve to the same employer through
  employer_identity.resolve(), with the inputs submission uses (staffing agencies, board aliases, your guards). An
  agency, a confidential client or a conflicting name never resolves, so it never links.
- MATCHED AGAIN. After every scan of such a board, and when a board is admitted, the open leads (new, no_match) of that
  employer are matched again against its open jobs, each lead with its own title, link, requisition and location
  (board_discovery.match_lead). A lead whose job is scanned later is linked then.
- WHAT LINKS. The same requisition, the same link or the same title (same seniority, compatible location) links the
  lead. A LIKELY match (one posting with a near-identical title, filler such as remote or I/II aside, the same seniority
  and a compatible location) is recorded as a candidate for the owner to confirm or reject (careeros.review.leads: the
  `careeros leads` command and the review page), never linked here. Two or more candidates stay ambiguous.
- STATES. After a complete scan with no match a lead is `no_match` (matched again on later scans); with no match after
  `closed_after_days` it is `closed`, but only when every admitted board of its employer was read completely in this scan
  run (Codex F3: one board's scan is not evidence about the others, whatever their order). A scan that was not complete
  never sets either. Each state keeps its reason.
Every decision is a lead_matches row (append-only; a repeat of the lead's latest decision is not written again). A job you
rejected for a lead, or unlinked from it, is never linked to it or proposed for it again.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter

from careeros.normalize.employer_identity import RESOLVED, resolve

from . import board_discovery as bd
from . import board_registry as reg
from .alerts import kept_for_review

OPEN_STATES = ("new", "no_match")


def _now(store) -> str:
    return store.clock.now()


# why a posting is proposed to you as a lead's likely match (board_discovery.match_lead): since the owner's D188
# (2026-11-19, "Exact links only") none of these links a lead by itself
LIKELY_WHY = {"requisition": "the same requisition id, the same title and a place that can be the lead's",
              "title_location": "the same title, the same seniority and a compatible location",
              "likely_title": "a near-identical title, the same seniority and a compatible location"}


class Resolver:
    """One pass of lead matching (a scan run, an admission): the identity inputs are read once and each open lead's
    employer is resolved once."""

    def __init__(self, ctx, *, batch_started: str | None = None):
        """`batch_started`: when the scan run began (cmd_discover's own time); a board counts as read in this run when its
        last complete scan is at or after it. Default: now."""
        from careeros.review.submission import _identity_inputs
        self.ctx, self.store, self.policy = ctx, ctx.store, ctx.policy
        self.batch_started = batch_started or ctx.store.clock.now()
        self.inputs = _identity_inputs(self.store, self.policy)
        self.parents = self.policy.get("parents", default={}) or {}
        self._leads: dict[str, tuple] | None = None
        self._revision: str | None = None   # the private settings' revision the open leads were read under

    # --- identity ------------------------------------------------------------------------------------------------
    def key(self, name: str | None, parent: str | None = None) -> str | None:
        """The employer key of a name (and a stored parent), or None when it does not resolve to one employer."""
        found = resolve(name, parent, self.parents, **self.inputs)
        return found.key if found.status == RESOLVED else None

    def open_leads(self) -> dict[str, tuple]:
        """{lead_id: (row, its own facts, employer key)} for the open leads that name an employer CareerOS can resolve.
        Checked before every use, not only the first: while an agency setting is missing there are none, and a change of
        the private settings reads them again, so a resolver kept across a scan never matches on a stale reading
        (CR-AD-V2-12)."""
        from careeros.core import private_sites
        from careeros.discovery.alerts import agency_setting_problem, local_only_urls
        if agency_setting_problem(self.store):
            self._leads = None
            return {}
        revision = private_sites.revision()
        if self._leads is None or self._revision != revision:
            self._revision = revision
            self._leads = {}
            local = local_only_urls(self.store)
            for row in self.store.all(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(OPEN_STATES))}) "
                                      "ORDER BY first_seen_at, lead_id", OPEN_STATES):
                facts = bd.candidate_from_lead(row, self.policy, local)
                if facts is None or facts.relationship != "employer":   # an agency's client is never guessed
                    continue
                if kept_for_review(row["url"] or ""):   # never matched, set no_match or closed: only you dismiss it (D140 F2)
                    continue
                key = self.key(facts.employer_name)
                if key:
                    self._leads[row["lead_id"]] = (row, facts, key)
        return self._leads

    def admitted_boards(self, key: str) -> list:
        """The employers rows of the scan that admits them, whose employer is `key`."""
        now = _now(self.store)
        return [r for r in self.store.all("SELECT * FROM employers ORDER BY employer_id")
                if self.key(r["name"], r["parent"]) == key and reg.scan_gate(self.store, r, self.policy, now)[0]]

    def postings(self, employer_ids: list[str]) -> list:
        if not employer_ids:
            return []
        marks = ",".join("?" * len(employer_ids))
        rows = self.store.all(f"SELECT * FROM jobs WHERE employer_id IN ({marks}) AND current_state NOT IN ('duplicate','closed') "
                              "ORDER BY first_seen_at, job_id", tuple(employer_ids))
        return [bd.TrackedPosting(key=r["job_id"], title=r["title"] or "", url=r["url"], requisition_id=r["requisition_id"],
                                  external_id=r["external_id"], location=r["location_raw"] or "", remote_type=r["remote_type"] or "unknown",
                                  employer=r["employer_name"], parent=r["parent"])
                for r in rows]

    def covered(self, key: str) -> bool:
        """Every admitted board of this employer was read completely in this scan run (Codex CR-2026-11-02-LEAD-RESOLUTION
        F3): only then is "no posting matches" evidence about the whole employer, so only then may a lead close. One board's
        complete scan, or a run where another board failed, was skipped or was suspicious, is not."""
        boards = self.admitted_boards(key)
        return bool(boards) and all((b["last_complete_scan_at"] or "") >= self.batch_started for b in boards)

    def refused(self, lead_id: str) -> set[str]:
        """Jobs you rejected for this lead, or unlinked from it: never linked or proposed again."""
        return {r["job_id"] for r in self.store.all("SELECT job_id FROM lead_matches WHERE lead_id = ? AND decision IN ('rejected','unlinked') "
                                                    "AND job_id IS NOT NULL", (lead_id,))}

    # --- matching -------------------------------------------------------------------------------------------------
    def rematch(self, employer, *, complete: bool, why: str) -> Counter:
        """Match the open leads of `employer`'s employer against the open jobs of its admitted boards. `complete`: the scan
        that just ran was complete (only then may a lead become no_match or closed)."""
        counts: Counter = Counter()
        key = self.key(employer["name"], employer["parent"])
        if not key or not reg.scan_gate(self.store, employer, self.policy, _now(self.store))[0]:
            return counts   # a board that is not admitted never resolves a lead
        leads = [item for item in self.open_leads().values() if item[2] == key]
        if not leads:
            return counts
        boards = self.admitted_boards(key)
        postings = self.postings([b["employer_id"] for b in boards])
        mapping = reg.mapping_for_employer(self.store, employer)
        from careeros.discovery.alerts import agency_setting_problem
        for row, facts, _key in leads:
            if agency_setting_problem(self.store):   # a stop found part-way: the rest of the batch is left (Codex Pass 3)
                self._leads = None
                counts["leads_stopped"] += 1
                break
            outcome = self.decide(row, facts, postings, complete=complete, why=why, employer=employer,
                                  mapping_id=mapping["mapping_id"] if mapping else None)
            if outcome:
                counts[f"leads_{outcome}"] += 1
        return counts

    def decide(self, lead, facts, postings: list, *, complete: bool, why: str, employer, mapping_id: str | None) -> str | None:
        store, now = self.store, _now(self.store)
        refused = self.refused(lead["lead_id"])
        pool = [p for p in postings if p.key not in refused]
        match = bd.match_lead(facts, pool, self.policy, now)
        decision = match["decision"]
        name = employer["name"]
        if decision == "matched":
            posting = match["posting"]
            link(store, lead["lead_id"], posting.key, method=match["method"], mapping_id=mapping_id, candidates=match["candidates"],
                 reason=f"linked by {match['method'].replace('_', ' ')} to {posting.title} at {name} ({why})")
            self.open_leads().pop(lead["lead_id"], None)
            return "linked"
        if decision == "likely":   # never linked here: the owner confirms or rejects it (careeros leads, the review page)
            posting = match["posting"]
            note = f"likely match: {posting.title} at {name} ({LIKELY_WHY[match['method']]}; {why}); confirm or reject it"
            self._record(lead, "likely", match["method"], posting.key, mapping_id, match["candidates"], note, status="new")
            return "likely"
        if decision == "ambiguous":
            note = f"{len(match['candidates'])} postings at {name} could be this lead ({why}); it is not linked"
            self._record(lead, "ambiguous", match["method"], None, mapping_id, match["candidates"], note, status="new")
            return "ambiguous"
        if not complete:
            return None   # an incomplete scan says nothing about what is missing
        if decision == "posting_closed" and not self.covered(self.key(employer["name"], employer["parent"]) or ""):
            note = (f"no open posting at {name} matches this lead ({why}); not closed yet: not every admitted board of {name} "
                    "was read completely in this scan")
            self._record(lead, "no_match", "none", None, mapping_id, [], note, status="no_match")
            return "no_match"
        if decision == "posting_closed":
            days = int(bd.cfg(self.policy)["closed_after_days"])
            note = f"no posting at {name} matched within {days} days of the lead; its posting has likely closed ({why})"
            self._record(lead, "posting_closed", "none", None, mapping_id, [], note, status="closed")
            self.open_leads().pop(lead["lead_id"], None)
            return "closed"
        note = f"no open posting at {name} matches this lead ({why}); matched again after later scans"
        self._record(lead, "no_match", "none", None, mapping_id, [], note, status="no_match")
        return "no_match"

    def _record(self, lead, decision: str, method: str, job_id: str | None, mapping_id: str | None, candidates: list, reason: str, *,
                status: str) -> None:
        """A system decision for a lead that does not link it: a lead_matches row unless it repeats the lead's latest one,
        and the lead's state with its reason."""
        store, now = self.store, _now(self.store)
        latest = store.one("SELECT decision, job_id, candidates_json FROM lead_matches WHERE lead_id = ? ORDER BY decided_at DESC, "
                           "rowid DESC LIMIT 1", (lead["lead_id"],))
        same = latest is not None and (latest["decision"], latest["job_id"], json.loads(latest["candidates_json"] or "[]")) == (
            decision, job_id, list(candidates))
        writer = store.writer("discovery")
        with store.tx():
            current = store.one("SELECT status, status_reason FROM leads WHERE lead_id = ?", (lead["lead_id"],))
            if current is None or current["status"] not in OPEN_STATES:
                return   # linked, dismissed or closed meanwhile
            if not same:
                writer.insert("lead_matches", _match_row(lead["lead_id"], job_id, mapping_id, method, decision, candidates, now,
                                                         actor="careeros", reason=reason))
            if (current["status"], current["status_reason"]) != (status, reason[:300]):
                writer.update("leads", {"status": status, "status_reason": reason[:300], "status_at": now},
                              f"lead_id = ? AND status IN ({','.join('?' * len(OPEN_STATES))})", (lead["lead_id"], *OPEN_STATES))
                if status != current["status"]:
                    writer.event("lead_status_changed", outcome="informational", entity_type="lead", entity_id=lead["lead_id"],
                                 metadata={"from": current["status"], "to": status, "decision": decision})


def _match_row(lead_id: str, job_id: str | None, mapping_id: str | None, method: str, decision: str, candidates: list, now: str, *,
               actor: str, reason: str | None) -> dict:
    return {"match_id": f"lm:{uuid.uuid4().hex[:16]}", "lead_id": lead_id, "job_id": job_id, "mapping_id": mapping_id,
            "method": method, "decision": decision, "candidates_json": json.dumps(list(candidates)[:10]), "decided_at": now,
            "audit": None, "audited_at": None, "actor": actor, "reason": (reason or "")[:300] or None}


def link(store, lead_id: str, job_id: str, *, method: str, mapping_id: str | None, candidates: list, reason: str,
         decision: str = "matched", actor: str = "careeros", component: str = "discovery", states: tuple = OPEN_STATES) -> bool:
    """Link a lead to its job: the lead becomes ingested with the reason, a lead_matches row and an audit event record it.
    Only a lead in one of `states` is linked; False when it was not (already linked, dismissed ...)."""
    now = store.clock.now()
    writer = store.writer(component)
    with store.tx():
        changed = writer.update("leads", {"status": "ingested", "job_id": job_id, "status_reason": reason[:300], "status_at": now},
                                f"lead_id = ? AND status IN ({','.join('?' * len(states))})", (lead_id, *states))
        if not changed:
            return False
        writer.insert("lead_matches", _match_row(lead_id, job_id, mapping_id, method, decision, candidates or [job_id], now,
                                                 actor=actor, reason=reason))
        writer.event("lead_linked", outcome="success", entity_type="lead", entity_id=lead_id,
                     actor_type="human" if actor == "user" else "system", actor_id=actor,
                     metadata={"job_id": job_id, "method": method, "decision": decision})
    return True


def after_scan(ctx, employer, *, complete: bool, resolver: Resolver | None = None) -> Counter:
    """After a scan of `employer`'s board: its employer's open leads, matched again."""
    resolver = resolver or Resolver(ctx)
    return resolver.rematch(employer, complete=complete, why=f"scan of {employer['ats']}:{employer['board_token']} on "
                                                              f"{ctx.store.clock.now()[:10]}")


def after_admission(ctx, mapping_id: str) -> Counter:
    """When a board is admitted: its employer's open leads, matched against the jobs CareerOS holds (the first scan
    after admission adds the rest and matches again). Never no_match or closed here: the board has not been scanned."""
    m = reg.mapping(ctx.store, mapping_id)
    if m is None:
        return Counter()
    employer = ctx.store.one("SELECT * FROM employers WHERE ats = ? AND lower(board_token) = lower(?)", (m["ats"], m["board_token"]))
    if employer is None:
        return Counter()
    return Resolver(ctx).rematch(employer, complete=False, why=f"{mapping_id} admitted on {ctx.store.clock.now()[:10]}")


def link_same_posting(ctx, job_id: str | None, *, via: str) -> Counter:
    """You pasted or added a posting yourself (careeros paste, the review page's paste box, careeros add-url, careeros
    import): a lead for the same posting is linked to it (ADR 0003, amendment of 2026-11-02). By the owner's D188
    (2026-11-19, "Exact links only") only the lead's own link naming this very posting (board_discovery.job_identity)
    links it; otherwise, when the lead's employer resolves to the job's (employer_identity), the same requisition or the
    same title only proposes the job to you, as after a scan (board_discovery.match_lead: a likely match). The match is
    made against every open tracked job of that employer, not this job alone (Codex CR-2026-11-02-LEAD-RESOLUTION F4): a
    lead that two postings could be gets no proposal. Open and closed leads are linked; a dismissed one stays dismissed,
    and a job you rejected or unlinked for a lead is never linked to it. This is your own evidence, so it needs no
    admitted board."""
    store, counts = ctx.store, Counter[str]()
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) if job_id else None
    if job is not None and job["current_state"] == "duplicate" and job["duplicate_of"]:
        original = store.one("SELECT * FROM jobs WHERE job_id = ?", (job["duplicate_of"],))
        if original is not None:   # a duplicate is its original's posting (D188), and stands for it
            job = original
    if job is None:
        return counts
    if any(u and kept_for_review(u) for u in (job["url"], job["canonical_url"])):
        return counts   # a posting at a review link never links a lead, by any strategy (the owner's D142 R-b)
    resolver = Resolver(ctx)
    key = resolver.key(job["employer_name"], job["parent"])
    pool = _employer_pool(store, resolver, key, job) if key else [_tracked(job)]   # the job, and the employer's other open jobs
    states = (*OPEN_STATES, "closed")
    from careeros.discovery.alerts import agency_setting_problem, is_agency_lead, local_only_urls
    local = local_only_urls(store)
    for lead in store.all(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(states))}) ORDER BY first_seen_at, lead_id", states):
        if job["job_id"] in resolver.refused(lead["lead_id"]):
            continue
        if lead["url"] and kept_for_review(lead["url"]):
            # A lead kept for review (no single job ID) is never linked by a posting you paste, add or import, not even
            # one with its byte-identical URL: it stays open on the review page until you dismiss it, checked first
            # (CR-2026-11-09-INTAKE-HOTFIX Pass 6, CR-HF-01, the owner's D140 F2)
            continue
        own = bd.posting_identity(lead["url"])
        if own is not None and own == bd.job_identity(job) and not is_agency_lead(lead, local):
            # the one automatic link (D188): the lead's own link names this very posting
            if link(store, lead["lead_id"], job["job_id"], method="manual_intake", mapping_id=None, candidates=[job["job_id"]],
                    reason=f"you {via} the same posting (the same link)", states=states):
                counts["leads_linked"] += 1
            continue
        if bd._same_link(lead["url"], job["url"]):
            # the same link but another title or requisition (H16, Pass 11), or the lead's own link names another
            # opening of the same portal (it differs after "#"): never this job by name or title (Codex
            # CR-2026-11-15-GATE-FIXES H12, Pass 10)
            continue
        facts = bd.candidate_from_lead(lead, ctx.policy, local)
        if not key or facts is None or facts.relationship != "employer" or resolver.key(facts.employer_name) != key:
            continue
        if agency_setting_problem(store):   # a stop found part-way: no more matching in this batch (the red team before
            counts["leads_stopped"] += 1    # Pass 4, as in rematch)
            break
        refused = resolver.refused(lead["lead_id"])
        match = bd.match_lead(facts, [p for p in pool if p.key not in refused], ctx.policy, store.clock.now())
        if match["posting"] is None or match["posting"].key != job["job_id"]:
            continue   # another posting, or several could be this lead: the ambiguity stays, nothing is linked
        if match["decision"] == "matched":
            if link(store, lead["lead_id"], job["job_id"], method="manual_intake", mapping_id=None, candidates=[job["job_id"]],
                    reason=f"you {via} the same posting (the same {match['method'].replace('_location', '').replace('_', ' ')})",
                    states=states):
                counts["leads_linked"] += 1
        elif match["decision"] == "likely" and lead["status"] in OPEN_STATES:
            note = (f"likely match: {job['title']} at {job['employer_name']}, which you {via} ({LIKELY_WHY[match['method']]}); "
                    "confirm or reject it")
            resolver._record(lead, "likely", match["method"], job["job_id"], None, [job["job_id"]], note, status="new")
            counts["leads_likely"] += 1
    return counts


# the identity rules the stored lead links are checked against (recheck_linked_leads), named in each reopened lead's reason
IDENTITY_RULES = "identity-2026-11-19b"   # 11-19b: exact links only (the owner's D188)


def recheck_linked_leads(ctx) -> Counter:
    """At every processing run (the owner's D188, 2026-11-19, "Exact links only"): every lead linked to a job stays
    linked only when its own link names that very posting (board_discovery.job_identity), or when you made or confirmed
    that link yourself (the latest record of that lead and that job is yours, never only an earlier unlink or rejection:
    the red team's fr1-alerts-3). Any other link (made by title, requisition, place or a shared page, by this engine or
    an older one, before or after a rollback) is undone: the lead is open again for you, and the job is not refused for
    it. Run every time, never once: nothing the current rules make is undone (the red team's fr1-alerts-1 and
    fr1-faults-1). Returns counts."""
    from careeros.discovery.alerts import agency_setting_problem, is_agency_lead, local_only_urls
    store, counts = ctx.store, Counter[str]()
    if agency_setting_problem(store):
        # an agency entry missing from the private settings: no lead can be told from an agency's, so nothing is
        # undone until it is restored, as alert intake and lead matching stop (D125; the red team's gr1-alerts-2)
        counts["leads_recheck_held"] += 1
        return counts
    local = local_only_urls(store)
    now = store.clock.now()
    writer = store.writer("discovery")
    for lead in store.all("SELECT * FROM leads WHERE status = 'ingested' AND job_id IS NOT NULL ORDER BY first_seen_at, lead_id"):
        latest = store.one("SELECT actor FROM lead_matches WHERE lead_id = ? AND job_id = ? ORDER BY decided_at DESC, rowid DESC "
                           "LIMIT 1", (lead["lead_id"], lead["job_id"]))
        if latest is not None and latest["actor"] == "user":
            continue   # you made or confirmed this very link
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (lead["job_id"],))
        if job is None:
            continue
        own = bd.posting_identity(lead["url"])
        if own is not None and own == bd.job_identity(job) and not is_agency_lead(lead, local):
            continue   # its own link names this very posting
        reason = (f"reopened: under the identity rules of {IDENTITY_RULES[9:]} (exact links only, D188) this line is not "
                  f"{job['title']} at {job['employer_name']} ({job['job_id']}) by its own link; confirm it yourself if it is")
        with store.tx():
            if writer.update("leads", {"status": "new", "job_id": None, "status_reason": reason[:300], "status_at": now},
                             "lead_id = ? AND status = 'ingested'", (lead["lead_id"],)):
                writer.insert("lead_matches", _match_row(lead["lead_id"], None, None, "none", "no_match", [job["job_id"]], now,
                                                         actor="careeros", reason=reason))
                counts["leads_reopened"] += 1
    if counts:
        writer.event("lead_links_rechecked", outcome="success", metadata={"rules": IDENTITY_RULES, **counts})
    return counts


def _tracked(row) -> bd.TrackedPosting:
    return bd.TrackedPosting(key=row["job_id"], title=row["title"] or "", url=row["url"], requisition_id=row["requisition_id"],
                             external_id=row["external_id"], location=row["location_raw"] or "", remote_type=row["remote_type"] or "unknown",
                             employer=row["employer_name"], parent=row["parent"])


def _employer_pool(store, resolver: Resolver, key: str, job) -> list:
    """`job` first, then every other open tracked job (any source, your pastes included) whose employer resolves to `key`."""
    keys: dict = {}
    pool = [_tracked(job)]
    for row in store.all("SELECT * FROM jobs WHERE current_state NOT IN ('duplicate','closed') ORDER BY first_seen_at, job_id"):
        names = (row["employer_name"], row["parent"])
        if names not in keys:
            keys[names] = resolver.key(*names)
        if keys[names] == key and row["job_id"] != job["job_id"]:
            pool.append(_tracked(row))
    return pool


# --- the domain route (Part C; ADR 0003 and ADR 0001, amendments of 2026-11-02) ---------------------------------------------
DOMAIN_ROUTE_DONE = ("rejected", "retired", "historical")


def lead_domains(ctx, now: str) -> dict:
    """Open leads that carry `domain=` (the employer's official website, as a lead's source saw it): only a hint.
    For each employer (never an agency, an excluded, held or skipped employer):
    - the hint becomes the employer record's official-domain candidate when it has none and no other record uses it
      (board_registry.apply_domain_hint: never a merge, never an alias);
    - a board of that employer waiting for an unknown domain is due again now ("retry"), instead of in 30 days;
    - an employer with no board known at all is checked at its own site ("check"): the official-site check finds the
      board its careers page links to. Not again within candidate_retry_days, unless the domain changed since.
    Returns {"retry": mapping ids, "check": entity ids, "counts": Counter}. Nothing here fetches anything."""
    from careeros.discovery.seen_index import norm_company
    from careeros.qualify.gate import excluded_employer
    store, policy = ctx.store, ctx.policy
    retry: set[str] = set()
    check: list[str] = []
    counts: Counter[str] = Counter()
    seen: set[tuple] = set()
    held = {norm_company(p) for p in [*(policy.get("gate", "held_parents", default=[]) or []), *bd.cfg(policy)["skip_employers"]]}
    rows = store.all(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(OPEN_STATES))}) AND context LIKE '%domain=%' "
                     "ORDER BY first_seen_at, lead_id", OPEN_STATES)
    from careeros.discovery.alerts import agency_setting_problem, local_only_urls
    local = local_only_urls(store)
    for lead in rows:
        facts = bd.candidate_from_lead(lead, policy, local)
        if facts is None or not facts.official_domain or facts.relationship != "employer":
            continue
        if agency_setting_problem(store):   # a stop found part-way: no more hints from leads (the red team before Pass 4)
            counts["lead_domain_stopped"] += 1
            break
        if excluded_employer({"employer_name": facts.employer_name}, policy) or facts.employer_norm in held:
            continue
        if (facts.employer_norm, facts.official_domain) in seen:
            continue
        seen.add((facts.employer_norm, facts.official_domain))
        entity_id, outcome = reg.apply_domain_hint(store, facts.employer_name, facts.official_domain)
        counts[f"lead_domain_{outcome}"] += 1
        if outcome not in ("set", "same"):
            continue   # another domain stands, or another employer uses this one: the hint is left out
        ent = reg.entity(store, entity_id)
        boards = store.all("SELECT * FROM board_mappings WHERE (entity_id = ? OR employer_norm = ?) AND verification_status NOT IN "
                           f"({','.join('?' * len(DOMAIN_ROUTE_DONE))})", (entity_id, ent["canonical_norm"], *DOMAIN_ROUTE_DONE))
        waiting = [m for m in boards if m["collection"] == "allowed" and m["verification_failure"] == "domain_unknown"]
        for m in waiting:
            reg.update_board(store, m["mapping_id"], {"reverify_at": now})
            reg.resolve_cases(store, mapping_id=m["mapping_id"], reasons=("domain_unknown",),
                              resolution=f"a lead supplied the website {ent['official_domain']} (a hint, checked at the site)")
            retry.add(m["mapping_id"])
        reg.resolve_cases(store, entity_id=entity_id, reasons=("domain_unknown",),
                          resolution=f"a lead supplied the website {ent['official_domain']} (a hint, checked at the site)")
        if boards or entity_id in check:
            continue
        last = store.one("SELECT MAX(finished_at) t FROM verification_runs WHERE entity_id = ?", (entity_id,))["t"]
        since_domain = ent["domain_set_at"] and last and last < ent["domain_set_at"]
        if last is None or since_domain or last <= reg.plus_days(now, -float(reg.bcfg(policy)["candidate_retry_days"])):
            check.append(entity_id)
    counts["lead_domain_retry"] += len(retry)
    counts["lead_domain_check"] += len(check)
    return {"retry": retry, "check": check, "counts": +counts}
