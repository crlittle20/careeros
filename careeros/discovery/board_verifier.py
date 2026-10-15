"""Board verification (2026-10-13): the employer's own website first, the ATS second.

For one employer, and independently of what discovery claimed (search results, alerts, ChatGPT research, a guessed
board name are only CANDIDATES):
 1 official domain   https://<domain>/ is read safely (safe_fetch) and must name the employer;
 2 careers page      found from the homepage's own links, or a usual careers path on the same domain;
 3 ATS handshake     the employer's own pages link to, embed, or redirect to a board on a supported system;
 4 identity          the board itself agrees: its own name, the official domain in its postings, or its postings
                     naming the employer (a different company's name with nothing else = a mismatch);
 5 second link       the employer's pages link to one posting on that board, or the board's postings link back to
                     the official domain;
 6 board function    the board's public listing API answers with well-formed postings (an empty board is valid).
Then the discovery claim (pass A) is compared with what the employer's own site showed (pass B): match, mismatch
(another supported board, or a manual-only system such as Workday), or not found.

BOARD TRUST (legitimacy only; whether the jobs suit you never enters it), out of 100:
  40 handshake, 20 identity, 15 second link, 10 board function, 10 domain agreement (5 the homepage names the
  employer, 5 the board points back to the domain), 5 independent agreement (discovery and the site agree, or two
  employer pages show the board).
Automatic scanning needs ALL of: trust >= 85, handshake confirmed, identity confirmed, the official domain names the
employer, a supported system, and then a passed validation scan. A score never overrides a missing handshake; 65-84
goes to your review; below 65 is never admitted.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from careeros.normalize.posting import canonical_url, contains_term, detect_country, detect_remote, norm, remote_scope

from . import board_registry as reg
from .board_urls import board_url
from .http import FetchError, RequestBudget
from .safe_fetch import SECURITY_FAILURES, OffsiteRedirect, Page, SiteFetcher, SiteFetchError
from .seen_index import norm_company
from .site_inspect import COMMON_PATHS, board_key, board_of, inspect, names_employer, system_of

TRUST_POINTS = {"handshake": 40, "identity": 20, "secondary": 15, "function": 10, "domain_named": 5, "domain_linked": 5,
                "independent": 5}
_DOMAIN = re.compile(r"(?<![a-z0-9.-])(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![a-z0-9-])", re.I)
_NOT_EMPLOYER_DOMAINS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "linkedin.com", "indeed.com", "glassdoor.com",
                         "google.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "eeoc.gov",
                         "dol.gov", "w3.org", "schema.org", "bit.ly", "ziprecruiter.com", "myworkdayjobs.com", "icims.com",
                         "e-verify.gov", "uscis.gov", "adp.com", "ada.gov", "ftc.gov", "builtin.com")


# --- evidence ---------------------------------------------------------------------------------------------------------
@dataclass
class RunLog:
    store: object
    kind: str
    entity_id: str | None
    mapping_id: str | None = None
    claim: str | None = None
    run_id: str = field(default_factory=lambda: f"vrun:{uuid.uuid4().hex[:16]}")
    items: list = field(default_factory=list)
    started_at: str = ""

    def __post_init__(self):
        self.started_at = self.store.clock.now()

    def add(self, evidence_type: str, result: str, *, points: int = 0, source_url=None, destination_url=None, method=None,
            notes=None, mapping_id=None, content_hash=None) -> None:
        self.items.append({"evidence_type": evidence_type, "result": result, "points": points, "source_url": source_url,
                           "destination_url": destination_url, "method": method, "notes": (notes or "")[:300] or None,
                           "mapping_id": mapping_id or self.mapping_id, "content_hash": content_hash})

    def finish(self, outcome: str, *, failure: str | None = None, trust: int | None = None, boards=(), comparison=None,
               site_requests: int = 0, ats_requests: int = 0, detail: str | None = None) -> None:
        store, now = self.store, self.store.clock.now()
        writer = store.writer("discovery")
        with store.tx():
            writer.insert("verification_runs", {
                "run_id": self.run_id, "entity_id": self.entity_id, "mapping_id": self.mapping_id, "kind": self.kind,
                "started_at": self.started_at, "finished_at": now, "outcome": outcome, "failure_class": failure,
                "trust_score": trust, "discovery_board": self.claim, "verifier_boards": json.dumps(sorted(boards)),
                "comparison": comparison, "site_requests": site_requests, "ats_requests": ats_requests,
                "detail": (detail or "")[:300] or None})
            for item in self.items:
                writer.insert("verification_evidence", {"evidence_id": f"ev:{uuid.uuid4().hex[:16]}", "run_id": self.run_id,
                                                        "entity_id": self.entity_id, "observed_at": now, **item})


@dataclass
class Finding:
    ats: str
    token: str
    how: str
    page: str
    url: str
    job_links: int = 0
    pages: set = field(default_factory=set)


# --- pure decisions (tested directly) ------------------------------------------------------------------------------------
def trust_score(*, handshake: bool, identity: str, secondary: bool, function_ok: bool, domain_named: bool,
                domain_linked: bool, independent: bool) -> int:
    p = TRUST_POINTS
    return ((p["handshake"] if handshake else 0) + (p["identity"] if identity == "confirmed" else 0)
            + (p["secondary"] if secondary else 0) + (p["function"] if function_ok else 0)
            + (p["domain_named"] if domain_named else 0) + (p["domain_linked"] if domain_linked else 0)
            + (p["independent"] if independent else 0))


def decide(*, handshake: bool, identity: str, domain_named: bool, supported: bool, trust: int, auto_trust: int = 85,
           review_trust: int = 65) -> tuple[bool, str]:
    """(eligible for automatic scanning?, why). Mandatory rules come first; the number never overrides them."""
    if not supported:
        return False, "unsupported system"
    if identity == "mismatch":
        return False, "identity_mismatch"
    missing = [label for ok, label in ((handshake, "no official-site handshake"), (identity == "confirmed", "identity not confirmed"),
                                       (domain_named, "official domain does not name the employer")) if not ok]
    if missing:
        return False, "; ".join(missing)
    if trust < auto_trust:
        return False, f"trust {trust} below {auto_trust}" + (" (review band)" if trust >= review_trust else "")
    return True, f"trust {trust}"


def identity_check(names: set[str], board_name: str | None, domain_seen: bool, shares: dict, *, handshake: bool,
                   previous: str | None = None) -> tuple[str, list[str]]:
    """('confirmed' | 'unresolved' | 'mismatch', signals) from the board's own content."""
    signals = []
    name_matches = bool(board_name) and (norm_company(board_name) in names or any(
        len(n) >= 4 and (contains_term(norm(board_name), n) or n.replace(" ", "") == norm(board_name).replace(" ", "")) for n in names))
    if name_matches:
        signals.append("board_name")
    if domain_seen:
        signals.append("official_domain")
    long_names = [n for n in names if len(n) >= 8 or " " in n]
    named_postings = round(shares.get("name_share", 0) * shares.get("postings", 0))
    if named_postings >= 2 and (long_names or (handshake and shares.get("name_share", 0) >= 0.5)):
        signals.append("postings_name_employer")
    if signals:
        return "confirmed", signals
    if board_name and not name_matches:
        return "mismatch", [f"board names another company: {board_name}"]
    if previous == "confirmed" and shares.get("postings", 0) == 0:
        return "confirmed", ["unchanged (empty board, nothing contradicts the earlier confirmation)"]
    return "unresolved", []


def domain_candidates(names: set[str], texts: list[str]) -> list[str]:
    """Official-domain CANDIDATES named in a board's own content (verification must still confirm them): only domains
    whose name contains the employer's name are offered."""
    counts = Counter()
    for text in texts:
        for match in _DOMAIN.finditer((text or "").lower()):
            host = match.group(1).lower().strip(".")
            if any(host == d or host.endswith("." + d) for d in _NOT_EMPLOYER_DOMAINS):
                continue
            labels = host.split(".")
            root = ".".join(labels[-3:]) if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") and len(labels[-1]) == 2 \
                else ".".join(labels[-2:])
            counts[root] += 1
    words = {w for n in names for w in n.split() if len(w) >= 4} | {n.replace(" ", "") for n in names if len(n.replace(" ", "")) >= 4}
    ranked = [d for d, _ in counts.most_common() if any(w in d.split(".")[0].replace("-", "") for w in words)]
    return [d for d in ranked if reg.clean_domain(d)]


# --- one verification --------------------------------------------------------------------------------------------------
def _fetch(ats_client, ats: str, token: str, cache: dict):
    from .board_discovery import _fetch_board
    key = board_key(ats, token)
    if key not in cache:
        cache[key] = _fetch_board(ats_client, ats, token)
    return cache[key]


def _record_site(store, entity_id: str, page: Page, site_type: str, found_by: str, refs: list) -> str:
    canonical = canonical_url(page.final_url) or page.final_url
    now = store.clock.now()
    existing = store.one("SELECT site_id FROM career_sites WHERE entity_id = ? AND canonical_url = ?", (entity_id, canonical))
    values = {"url": page.final_url[:500], "site_type": site_type, "found_by": found_by, "ats_seen": json.dumps(refs[:20]),
              "content_hash": page.content_hash, "last_checked_at": now, "status": "ok"}
    if refs:
        values["verified_at"] = now
    if existing:
        store.writer("discovery").update("career_sites", values, "site_id = ?", (existing["site_id"],))
        return existing["site_id"]
    site_id = f"site:{uuid.uuid4().hex[:12]}"
    store.writer("discovery").insert("career_sites", {"site_id": site_id, "entity_id": entity_id, "canonical_url": canonical,
                                                      "verified_at": values.pop("verified_at", None), **values})
    return site_id


def verify_employer(ctx, entity_id: str, *, site_factory, ats_client, claim_mapping_id: str | None = None,
                    kind: str = "initial") -> dict:
    store, policy = ctx.store, ctx.policy
    cfg = reg.bcfg(policy)
    ent = reg.entity(store, entity_id)
    names = reg.entity_names(store, entity_id)
    claim = reg.mapping(store, claim_mapping_id)
    claim_key = board_key(claim["ats"], claim["board_token"]) if claim and claim["collection"] == "allowed" else None
    log = RunLog(store, kind, entity_id, claim_mapping_id, claim=claim_key)
    cache: dict = {}
    ats_before = ats_client.requests
    result = {"outcome": None, "boards": {}, "comparison": None}

    def done(outcome, failure=None, trust=None, boards=(), comparison=None, site=None, detail=None):
        log.finish(outcome, failure=failure, trust=trust, boards=boards, comparison=comparison,
                   site_requests=site.requests if site else 0, ats_requests=ats_client.requests - ats_before, detail=detail)
        if site:
            site.close()
        result.update(outcome=outcome, comparison=comparison, failure=failure)
        return result

    # 1. official domain (a candidate from discovery or from the claimed board's own content; never from a token)
    domain = ent["official_domain"]
    if not domain and claim_key:
        try:
            postings, board_name, board_text = _fetch(ats_client, claim["ats"], claim["board_token"], cache)
            found = domain_candidates(names, [board_text] + [f"{p.description_text} {p.url or ''} {p.apply_url or ''}" for p in postings[:40]])
        except FetchError:
            found = []
        if found:
            domain = found[0]
            log.add("official_domain_candidate", "info", method="board_content", notes=f"{domain} named in {claim_key}")
            store.writer("discovery").update("employer_entities", {"official_domain": domain, "updated_at": store.clock.now()},
                                             "entity_id = ?", (entity_id,))
    if not domain:
        log.add("official_domain", "fail", notes="no official website known")
        if not _quiet(claim):
            reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="domain_unknown",
                          detail=f"CareerOS needs {ent['canonical_name']}'s official website to verify its job board: "
                                 f"careeros sources domain \"{ent['canonical_name']}\" example.com")
        _not_proven(ctx, claim, "official domain unknown", "domain_unknown")
        return done("domain_unknown", "domain_unknown")
    try:
        site = site_factory(domain)
    except SiteFetchError as exc:
        if exc.failure == "blocked_site":  # a job site or a site CareerOS never automates is not an employer's own site
            log.add("official_domain", "fail", notes=f"{domain} is not an employer website CareerOS may read")
            reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="domain_not_employer",
                          detail=f"{domain} cannot be {ent['canonical_name']}'s official website for verification; "
                                 f"set the right one with careeros sources domain")
            store.writer("discovery").update("employer_entities", {"domain_status": "failed", "updated_at": store.clock.now()},
                                             "entity_id = ?", (entity_id,))
            _not_proven(ctx, claim, "official domain is not an employer website", "domain_not_employer")
            return done("domain_not_employer", "domain_not_employer")
        log.add("official_domain", "fail", notes=f"unsafe domain: {exc}")
        _security_failure(ctx, claim, exc.failure if exc.failure in SECURITY_FAILURES else "unsafe_url", f"unsafe domain {domain}")
        return done("unsafe_domain", "unsafe_url")

    # 2. homepage
    home_url = f"https://{domain}/"
    try:
        home_page = site.get(home_url)
    except SiteFetchError as exc:
        return done(*_site_failure(ctx, log, claim, entity_id, exc, home_url), site=site)
    if isinstance(home_page, OffsiteRedirect):
        log.add("official_domain", "fail", source_url=home_url, destination_url=home_page.destination, method="redirect",
                notes="the homepage sends visitors to another site (rebrand or acquisition?)")
        reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="offsite_homepage",
                      detail=f"{home_url} redirects to {home_page.destination}; confirm the employer's current website")
        _not_proven(ctx, claim, "official homepage redirects elsewhere", "offsite_homepage")
        return done("offsite_homepage", "offsite_redirect", site=site)
    home = inspect(home_page.text, home_page.final_url, domain)
    named = names_employer(home, names)
    if named:
        log.add("official_domain", "pass", points=TRUST_POINTS["domain_named"], source_url=home_page.final_url, method=f"names:{named}",
                content_hash=home_page.content_hash)
        store.writer("discovery").update("employer_entities", {"domain_status": "verified", "domain_verified_at": store.clock.now(),
                                                               "updated_at": store.clock.now()}, "entity_id = ?", (entity_id,))
        reg._event(store, "discovery", "official_domain_verified", "success", entity_id, domain=domain)
    else:
        log.add("official_domain", "fail", source_url=home_page.final_url, notes="the homepage does not name the employer")
        reg._event(store, "discovery", "official_domain_failed", "failure", entity_id, domain=domain)
        if not _quiet(claim):
            reg.open_case(store, entity_id=entity_id, reason_code="domain_unconfirmed",
                          detail=f"{home_page.final_url} does not name {ent['canonical_name']}; is it the right website?")
    pages = [("homepage", home, home_page, "homepage")]
    _record_site(store, entity_id, home_page, "homepage", "homepage", sorted(home.boards) + sorted(home.systems))

    # 3. careers pages (from the homepage's own links first; usual paths on the same domain otherwise)
    findings: dict[str, Finding] = {}
    systems: dict[str, str] = dict(home.systems)
    offsite: list[str] = list(home.offsite_jobs)
    candidates = [(u, "homepage_link") for u in home.careers_links[:3]] or [(f"https://{domain}{p}", "common_path") for p in COMMON_PATHS]
    fetched_careers = 0
    for url, found_by in candidates:
        if fetched_careers >= 2 or (found_by == "common_path" and fetched_careers):
            break
        try:
            page = site.get(url)
        except SiteFetchError as exc:
            if exc.failure in SECURITY_FAILURES:
                log.add("careers_page", "fail", source_url=url, notes=f"blocked: {exc}")
                return done(*_security_failure(ctx, claim, exc.failure, f"careers link {url}: {exc}", log=log), site=site)
            if exc.failure == "budget_exhausted":
                break
            continue
        if isinstance(page, OffsiteRedirect):
            target = board_of(page.destination)
            if target:
                findings.setdefault(board_key(*target), Finding(target[0], target[1], "redirect", url, page.destination)).pages.add(url)
            elif system_of(page.destination):
                systems.setdefault(system_of(page.destination), page.destination)
            else:
                offsite.append(page.destination)
            log.add("careers_page", "info", source_url=url, destination_url=page.destination, method="redirect")
            fetched_careers += 1
            continue
        insp = inspect(page.text, page.final_url, domain)
        pages.append(("careers_page", insp, page, found_by))
        systems.update({k: v for k, v in insp.systems.items() if k not in systems})
        offsite.extend(insp.offsite_jobs)
        _record_site(store, entity_id, page, "careers_page", found_by, sorted(insp.boards) + sorted(insp.systems))
        log.add("careers_page", "pass", source_url=page.final_url, method=found_by, content_hash=page.content_hash)
        fetched_careers += 1
    for _, insp, page, _found in pages:
        for key, b in insp.boards.items():
            findings.setdefault(key, Finding(b["ats"], b["token"], b["how"], page.final_url, b["url"])).pages.add(page.final_url)
        for ats, token, _job, url in insp.jobs:
            key = board_key(ats, token)
            findings.setdefault(key, Finding(ats, token, "job_link", page.final_url, url)).pages.add(page.final_url)
            findings[key].job_links += 1
    careers_seen = fetched_careers > 0 or bool(findings or systems)
    reg._event(store, "discovery", "careers_page_found" if careers_seen else "careers_page_not_found",
               "success" if careers_seen else "failure", entity_id, domain=domain)
    if not careers_seen:
        log.add("careers_page", "fail", notes="no careers page found on the official site")
    if systems and not findings:
        primary = next(iter(systems.values()))
        store.writer("discovery").update("employer_entities", {"primary_careers_url": primary[:500], "updated_at": store.clock.now()},
                                         "entity_id = ?", (entity_id,))
    elif pages[1:]:
        store.writer("discovery").update("employer_entities", {"primary_careers_url": pages[1][2].final_url[:500],
                                                               "updated_at": store.clock.now()}, "entity_id = ?", (entity_id,))

    # 4. pass A vs pass B
    comparison = ("no_claim" if not claim_key else "match" if claim_key in findings
                  else "mismatch" if (findings or systems) else "not_found")
    if claim_key:
        reg._event(store, "discovery", "ats_candidate_found", "informational", claim_mapping_id, comparison=comparison,
                   verifier_boards=sorted(findings))
    log.add("independent_comparison", "pass" if comparison == "match" else "info" if comparison == "no_claim" else "fail",
            notes=f"discovery claimed {claim_key or 'nothing'}; the employer's site shows {sorted(findings) or sorted(systems) or 'nothing'}")
    for system, url in systems.items():
        mid = reg.ensure_mapping(store, entity_id, system, urlsplit(url).netloc.lower() or system, name=ent["canonical_name"],
                                 provenance="official_site", origin="verification", board_url=url[:500], collection="manual_only",
                                 status="manual_only", domain=domain)
        reg._event(store, "discovery", "employer_manual_only", "informational", mid, system=system)
        log.add("ats_handshake", "info", mapping_id=mid, source_url=pages[-1][2].final_url, destination_url=url, method="manual_system",
                notes=f"the employer uses {system}, which CareerOS never reads automatically")
    if offsite and not findings and not systems:
        # CareerOS never follows a link off the official domain. The other site may be a second website of the employer
        # (then set it as the official one) or a job system CareerOS does not recognise.
        other = urlsplit(offsite[0]).netloc.lower().removeprefix("www.")
        log.add("careers_page", "info", destination_url=offsite[0][:500], method="offsite_link",
                notes=f"the careers link leads to another website ({other}); not followed")
        if not _quiet(claim):
            reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="careers_offsite",
                          detail=f"the careers page sends applicants to {offsite[0]}, outside {domain}. If {other} is the "
                                 f"employer's own site: careeros sources domain \"{ent['canonical_name']}\" {other}")
    if claim_key and claim_key not in findings:
        _claim_not_shown(ctx, log, claim, kind, findings, systems, careers_seen, entity_id)

    # 5. every board the employer's own site shows
    order = sorted(findings, key=lambda k: (k != claim_key, k))[:int(cfg["max_boards_per_employer"])]
    if len(findings) > 1:
        reg._event(store, "discovery", "ats_candidate_found", "informational", entity_id, boards=sorted(findings))
    trusts = []
    for key in order:
        status, trust = _evaluate_board(ctx, log, ent, names, domain, bool(named), findings[key], kind, ats_client, cache,
                                        independent=(key == claim_key) or len(findings[key].pages) >= 2,
                                        migrated_from=result.get("migrated_from"))
        result["boards"][key] = status
        if trust is not None:
            trusts.append(trust)
    if claim and claim_key and claim_key not in findings and kind == "reverify" and findings and claim["handshake_status"] == "confirmed":
        _migrate(ctx, log, claim, findings, order)
    outcome = ("verified" if any(s in ("quarantined", "active") for s in result["boards"].values())
               else "manual_only" if systems and not findings else "review_required" if findings else "no_handshake")
    return done(outcome, None if outcome in ("verified", "manual_only") else outcome, max(trusts) if trusts else None,
                boards=sorted(findings), comparison=comparison, site=site)


def _quiet(claim) -> bool:
    """A legacy board that is still scanned (not paused, not active): an inconclusive check asks nothing of you. It is
    recorded on the board and summarised on the review page, not raised as a review case."""
    return (claim is not None and claim["provenance"] == "legacy_unknown" and claim["verification_status"] != "active"
            and not claim["paused_reason"])


def _not_proven(ctx, claim, reason: str, failure: str) -> None:
    """The claimed board could not be checked against the employer's site this time."""
    if claim is None or claim["collection"] != "allowed":
        return
    store = ctx.store
    later = reg.plus_days(store.clock.now(), reg.bcfg(ctx.policy)["candidate_retry_days"])
    if claim["verification_status"] == "candidate":
        reg.update_board(store, claim["mapping_id"], {"verification_failure": failure, "reverify_at": later})
    elif claim["provenance"] == "legacy_unknown" and claim["verification_status"] != "active":
        # proof unavailable: the legacy board keeps its history and its grace, and waits for verification
        reg.update_board(store, claim["mapping_id"], {"verification_status": "verification_required", "verification_failure": failure,
                                                       "reverify_at": later})


def _security_failure(ctx, claim, failure: str, detail: str, log=None) -> tuple:
    store = ctx.store
    if claim is not None and claim["collection"] == "allowed":
        if claim["verification_status"] == "active" or claim["provenance"] == "legacy_unknown":
            reg.pause(store, claim["mapping_id"], f"unsafe official-site address: {detail}", failure=failure)
        else:
            reg.update_board(store, claim["mapping_id"], {"verification_status": "rejected", "verification_failure": failure})
            reg._event(store, "discovery", "employer_rejected", "failure", claim["mapping_id"], reason=failure)
    reg.open_case(store, mapping_id=claim["mapping_id"] if claim else None, reason_code="unsafe_url", detail=detail)
    return "unsafe", failure


def _site_failure(ctx, log, claim, entity_id, exc: SiteFetchError, url: str) -> tuple:
    """The official site could not be read. Transport problems never change trust; they are counted, and only
    repeated failures ask for your review. Security problems and robots.txt refusals are recorded as such."""
    store = ctx.store
    if exc.failure == "budget_exhausted":  # CareerOS ran out of its own page budget: nothing is wrong with the site
        log.add("official_domain", "info", source_url=url, notes="not checked: the daily page budget was used up", method="fetch")
        if claim is not None and claim["collection"] == "allowed":
            reg.update_board(store, claim["mapping_id"], {"reverify_at": store.clock.now()})  # first in line next run
        return "deferred", "budget_exhausted"
    log.add("official_domain", "fail", source_url=url, notes=f"{exc.failure}: {exc.detail}"[:300], method="fetch")
    if exc.failure in SECURITY_FAILURES:
        return _security_failure(ctx, claim, exc.failure, f"{url}: {exc}", log=log)
    if claim is not None and claim["collection"] == "allowed":
        failures = (claim["site_failures"] or 0) + 1
        reg.update_board(store, claim["mapping_id"], {"site_failures": failures,
                                                       "reverify_at": reg.plus_days(store.clock.now(), 1 if failures < 3 else 7)})
        if not _quiet(claim) and (exc.failure in ("robots_disallowed", "auth_required")
                                  or failures >= reg.bcfg(ctx.policy)["site_failures_before_review"]):
            reg.open_case(store, mapping_id=claim["mapping_id"], entity_id=entity_id,
                          reason_code="site_unreadable" if exc.failure in ("robots_disallowed", "auth_required") else "site_unavailable",
                          detail=f"the employer's site could not be checked ({exc.failure}); verify the board yourself or approve it")
    elif exc.failure in ("robots_disallowed", "auth_required"):
        reg.open_case(store, entity_id=entity_id, reason_code="site_unreadable", detail=f"{url}: {exc.failure}")
    return "site_unavailable", exc.failure


def _claim_not_shown(ctx, log, claim, kind, findings, systems, careers_seen, entity_id) -> None:
    store = ctx.store
    mid = claim["mapping_id"]
    legacy_or_live = claim["verification_status"] == "active" or claim["provenance"] == "legacy_unknown"
    proven_before = claim["handshake_status"] == "confirmed"  # only a board the employer's site linked before can "move"
    if (systems or findings) and kind == "reverify" and legacy_or_live and not proven_before:
        # an unproven legacy board next to a different system on the employer's site: maybe a migration, maybe a
        # second board the site does not show. Never retired on that alone: your review, and it keeps its grace.
        shown = ", ".join(sorted(findings) or sorted(systems))
        reg.update_board(store, mid, {"verification_status": "review_required", "verification_failure": "possible_migration",
                                      "reverify_at": reg.plus_days(store.clock.now(), reg.bcfg(ctx.policy)["candidate_retry_days"])})
        reg.open_case(store, mapping_id=mid, entity_id=entity_id, reason_code="possible_migration",
                      detail=f"the employer's site shows {shown}, not {claim['ats']}:{claim['board_token']} (a move, or a second board?)")
        log.add("ats_handshake", "fail", mapping_id=mid, notes=f"the employer's site shows {shown} instead")
        return
    if systems and not findings:  # the official site uses a manual-only system: the claimed board is contradicted
        if kind == "reverify" and legacy_or_live:
            target = next(iter(systems))
            _retire(ctx, claim, f"the employer's site now uses {target}", migrated_to=None)
            reg._event(store, "discovery", "ats_migration_detected", "informational", mid, to=target)
            reg.open_case(store, mapping_id=mid, entity_id=entity_id, reason_code="migration_detected",
                          detail=f"{claim['employer_name']} moved from {claim['ats']} to {target} (manual only); the old board is kept as history")
        else:
            reg.update_board(store, mid, {"verification_status": "rejected", "verification_failure": "official_site_contradicts",
                                          "handshake_status": "failed"})
            reg._event(store, "discovery", "employer_rejected", "failure", mid, reason="official site uses " + ", ".join(systems))
        log.add("ats_handshake", "fail", mapping_id=mid, notes=f"the employer's site uses {', '.join(systems)}, not {claim['ats']}")
        return
    if findings:  # another supported board: a migration on reverification, a mismatch otherwise
        if kind == "reverify" and legacy_or_live:
            return  # handled by _migrate once the new board has been evaluated
        reg.update_board(store, mid, {"verification_status": "review_required", "verification_failure": "ats_mismatch",
                                      "handshake_status": "failed"})
        reg.open_case(store, mapping_id=mid, entity_id=entity_id, reason_code="ats_mismatch",
                      detail=f"discovery found {claim['ats']}:{claim['board_token']}, but the employer's own site shows {', '.join(sorted(findings))}")
        log.add("ats_handshake", "fail", mapping_id=mid, notes="the employer's site shows a different board")
        return
    log.add("ats_handshake", "fail", mapping_id=mid, notes="the employer's site does not link to this board")
    reg._event(store, "discovery", "ats_handshake_failed", "failure", mid)
    if claim["handshake_status"] == "confirmed" and claim["verification_status"] == "active":
        reg.pause(store, mid, "the employer's careers page no longer links to this board", failure="handshake_lost")
        return
    later = reg.plus_days(store.clock.now(), reg.bcfg(ctx.policy)["candidate_retry_days"])
    if claim["verification_status"] == "active" and claim["override_reason"]:
        status = "active"  # your approval (e.g. a careers page that loads jobs with JavaScript) stands until it expires
    elif claim["provenance"] == "legacy_unknown":
        status = "verification_required"  # proof unavailable: history and grace kept
    elif careers_seen:
        status = "review_required"
    else:
        status = claim["verification_status"]
    reg.update_board(store, mid, {"verification_status": status, "verification_failure": "no_handshake", "reverify_at": later})
    if not _quiet(claim):
        reg.open_case(store, mapping_id=mid, entity_id=entity_id, reason_code="site_unreadable" if careers_seen else "careers_page_not_found",
                      detail=(f"the employer's site does not show a link to {claim['board_url']} (it may load jobs with JavaScript); "
                              f"check it and run careeros sources approve {mid} --reason ... if it is right"))


def _evaluate_board(ctx, log, ent, names, domain, domain_named, finding: Finding, kind, ats_client, cache, *, independent: bool,
                    migrated_from=None) -> tuple[str, int | None]:
    store, cfg = ctx.store, reg.bcfg(ctx.policy)
    now = store.clock.now()
    mid = reg.ensure_mapping(store, ent["entity_id"], finding.ats, finding.token, name=ent["canonical_name"],
                             provenance="official_site", origin="verification", board_url=board_url(finding.ats, finding.token),
                             domain=domain)
    m = reg.mapping(store, mid)
    log.add("ats_handshake", "pass", points=TRUST_POINTS["handshake"], mapping_id=mid, source_url=finding.page,
            destination_url=finding.url, method=finding.how)
    reg._event(store, "discovery", "ats_handshake_confirmed", "success", mid, via=finding.how)
    try:
        postings, board_name, board_text = _fetch(ats_client, finding.ats, finding.token, cache)
    except FetchError as exc:
        if exc.code == "not_found":
            log.add("board_function", "fail", mapping_id=mid, notes="the board the employer links to does not exist")
            reg.update_board(store, mid, {"board_activity": "unavailable", "handshake_status": "confirmed", "last_checked_at": now,
                                          "verification_status": "review_required" if m["verification_status"] != "active" else "active",
                                          "verification_failure": "board_not_found"})
            reg.open_case(store, mapping_id=mid, entity_id=ent["entity_id"], reason_code="board_not_found",
                          detail=f"the employer's site links to {finding.url}, which no longer exists")
            reg._event(store, "discovery", "board_unavailable", "failure", mid)
        else:  # transport / provider trouble: nothing about the board's legitimacy changes
            log.add("board_function", "info", mapping_id=mid, notes=f"board not read this time ({exc.code})")
        return m["verification_status"], None
    shares = reg.identity_shares(postings, names, domain)
    domain_seen = bool(domain) and (domain in (board_text or "").lower() or shares["domain_share"] > 0)
    identity, signals = identity_check(names, board_name, domain_seen, shares, handshake=True, previous=m["identity_check"])
    log.add("identity", "pass" if identity == "confirmed" else "fail", points=TRUST_POINTS["identity"] if identity == "confirmed" else 0,
            mapping_id=mid, notes=", ".join(signals) or "no signal on the board itself")
    reg._event(store, "discovery", "identity_verified" if identity == "confirmed" else
               "identity_mismatch" if identity == "mismatch" else "identity_unresolved",
               "success" if identity == "confirmed" else "failure", mid, signals=signals)
    own_links = sum(1 for p in postings if domain and any(domain in (u or "").lower() for u in (p.url, p.apply_url)))
    secondary = finding.job_links > 0 or own_links > 0
    log.add("second_link", "pass" if secondary else "info", points=TRUST_POINTS["secondary"] if secondary else 0, mapping_id=mid,
            notes=f"{finding.job_links} posting link(s) on the employer's pages; {own_links} posting(s) link back to {domain}")
    complete = sum(1 for p in postings if p.external_id and p.title)
    function_ok = not postings or complete / len(postings) >= 0.95
    activity = "active" if postings else "empty"
    log.add("board_function", "pass" if function_ok else "fail", points=TRUST_POINTS["function"] if function_ok else 0, mapping_id=mid,
            notes=f"{len(postings)} postings ({complete} complete); board {activity}")
    if domain_seen:
        log.add("domain_link", "pass", points=TRUST_POINTS["domain_linked"], mapping_id=mid, notes=f"the board points back to {domain}")
    if independent:
        log.add("independent_agreement", "pass", points=TRUST_POINTS["independent"], mapping_id=mid)
    trust = trust_score(handshake=True, identity=identity, secondary=secondary, function_ok=function_ok, domain_named=domain_named,
                        domain_linked=domain_seen, independent=independent)
    eligible, why = decide(handshake=True, identity=identity, domain_named=domain_named, supported=True, trust=trust,
                           auto_trust=int(cfg["auto_trust"]), review_trust=int(cfg["review_trust"]))
    fp = reg.board_fingerprint(finding.ats, finding.token, entity_id=ent["entity_id"], board_name=board_name, domain=domain, shares=shares)
    reg._event(store, "discovery", "board_fingerprint_created" if not m["board_fingerprint"] else
               "board_fingerprint_changed" if m["board_fingerprint"] != fp else "board_fingerprint_confirmed", "informational", mid)
    values = {"entity_id": ent["entity_id"], "handshake_status": "confirmed", "identity_check": identity, "board_trust_score": trust,
              "board_activity": activity, "board_fingerprint": fp, "canonical_ats_url": board_url(finding.ats, finding.token),
              "last_checked_at": now, "official_domain": domain, "site_failures": 0, "relevant_roles": None,
              "identity_signals": json.dumps({"board_name": board_name, **shares})}
    from .board_discovery import relevant_roles
    values["relevant_roles"] = relevant_roles(postings, ctx.policy)
    status = m["verification_status"]
    legacy = m["provenance"] == "legacy_unknown"
    if eligible:
        count = (m["verify_count"] or 0) + 1
        days = cfg["reverify_days"]
        values.update(verification_failure=None, verify_count=count, identity_status="verified", verified_by="careeros",
                      verified_method="official_site", verified_at=now,
                      reverify_at=reg.plus_days(now, days["new"] if count <= 1 else days["stable"] if count <= 4 else days["long_term"]))
        if status == "active" or (legacy and status in reg.LEGACY_GRACE_STATUSES):
            values.update(verification_status="active", paused_reason=None, validation_status=m["validation_status"] or "passed")
            reg.update_board(store, mid, values)
            reg.resolve_cases(store, mapping_id=mid, resolution=f"re-verified ({why})")
            reg._event(store, "discovery", "board_active", "success", mid, trust=trust, kind=kind)
            return "active", trust
        values.update(verification_status="quarantined", quarantined_at=now, validation_status="pending", paused_reason=None)
        reg.update_board(store, mid, values)
        reg.resolve_cases(store, mapping_id=mid, resolution=f"verified ({why}); validation scan next")
        reg._event(store, "discovery", "board_quarantined", "success", mid, trust=trust)
        return "quarantined", trust
    later = reg.plus_days(now, cfg["candidate_retry_days"])
    if identity == "mismatch":
        values["verification_failure"] = "identity_mismatch"
        reg.update_board(store, mid, values)
        if status == "active" or legacy:
            reg.pause(store, mid, f"the board now belongs to someone else: {', '.join(signals)}", failure="identity_mismatch")
            return "verification_required", trust
        reg.update_board(store, mid, {"verification_status": "rejected", "reverify_at": later})
        reg.open_case(store, mapping_id=mid, entity_id=ent["entity_id"], reason_code="identity_mismatch",
                      detail=f"{finding.url} is linked from {domain} but the board names another company ({', '.join(signals)})")
        reg._event(store, "discovery", "employer_rejected", "failure", mid, reason="identity_mismatch")
        return "rejected", trust
    values.update(verification_failure=why[:200], reverify_at=later)
    if status == "active" and not legacy:
        reg.update_board(store, mid, values)
        reg.pause(store, mid, f"re-verification fell short: {why}", failure="trust_below_threshold")
        return "verification_required", trust
    values["verification_status"] = "review_required"
    reg.update_board(store, mid, values)
    reg.open_case(store, mapping_id=mid, entity_id=ent["entity_id"], reason_code="trust_review_band" if trust >= int(cfg["review_trust"]) else "verification_incomplete",
                  detail=f"{board_url(finding.ats, finding.token)}: {why} (trust {trust}/100)")
    return "review_required", trust


def _retire(ctx, claim, reason: str, *, migrated_to: str | None) -> None:
    store, now = ctx.store, ctx.store.clock.now()
    reg.update_board(store, claim["mapping_id"], {"verification_status": "historical" if migrated_to or "now uses" in reason else "retired",
                                                   "retired_at": now, "migrated_to": migrated_to, "paused_reason": f"retired: {reason}"[:200],
                                                   "handshake_status": "failed"})
    employer = store.one("SELECT employer_id FROM employers WHERE ats = ? AND lower(board_token) = lower(?)", (claim["ats"], claim["board_token"]))
    if not employer:
        return
    from careeros.pipeline import close_open_jobs  # history is kept; the openings close because the board is no longer theirs
    close_open_jobs(ctx, employer["employer_id"], "board_retired", notes=reason[:200])


def _migrate(ctx, log, claim, findings: dict, order: list) -> None:
    """The employer's site now shows a different supported board: the old board becomes history, the new one goes
    through quarantine and validation like any new board."""
    store = ctx.store
    new_key = order[0]
    new = reg.mapping_for_board(store, findings[new_key].ats, findings[new_key].token)
    _retire(ctx, claim, f"the employer moved to {new_key}", migrated_to=new["mapping_id"] if new else None)
    reg._event(store, "discovery", "ats_migration_detected", "informational", claim["mapping_id"], to=new_key)
    reg.open_case(store, mapping_id=claim["mapping_id"], entity_id=claim["entity_id"], reason_code="migration_detected",
                  detail=f"{claim['employer_name']} moved from {claim['ats']}:{claim['board_token']} to {new_key}; the new board is "
                         "being verified, the old one is kept as history")
    log.add("ats_migration", "info", mapping_id=claim["mapping_id"], destination_url=findings[new_key].url,
            notes=f"from {claim['ats']} to {new_key}")


# --- validation scan (quarantine -> active) ------------------------------------------------------------------------------
def validate_board(ctx, mapping_id: str, ats_client, *, provider_degraded: bool = False) -> str:
    """The quarantined board is read like a production scan would read it, without ingesting anything."""
    store = ctx.store
    m = reg.mapping(store, mapping_id)
    if m is None or m["verification_status"] != "quarantined":
        return "skipped"
    log = RunLog(store, "validation", m["entity_id"], mapping_id, claim=board_key(m["ats"], m["board_token"]))
    reg._event(store, "discovery", "validation_scan_started", "informational", mapping_id)
    before = ats_client.requests
    from .board_discovery import _fetch_board
    try:
        postings, board_name, _text = _fetch_board(ats_client, m["ats"], m["board_token"])
    except FetchError as exc:
        transient = exc.code not in ("invalid_json", "unexpected_shape", "not_found")
        if transient or provider_degraded:
            log.add("validation", "info", notes=f"board not read ({exc.code}); retried next run")
            log.finish("deferred", failure=exc.code, ats_requests=ats_client.requests - before)
            return "deferred"
        return _validation_failed(ctx, log, m, f"the board could not be read ({exc.code})", ats_client.requests - before)
    problems = []
    complete = [p for p in postings if p.external_id and p.title and (p.url or p.apply_url)]
    if postings and len(complete) / len(postings) < 0.95:
        problems.append(f"{len(postings) - len(complete)} of {len(postings)} postings lack an id, title or link")
    if any(p.url and not canonical_url(p.url) for p in postings):
        problems.append("posting links cannot be normalised")
    malformed = 0
    for p in postings:
        try:
            remote = detect_remote(p.location, p.workplace_hint, p.description_text or "")
            remote_scope(remote, detect_country(p.location, p.description_text or "", remote), [], [])
        except Exception:  # noqa: BLE001 - a malformed record is counted, never fatal
            malformed += 1
    if postings and malformed > 0.1 * len(postings):
        problems.append(f"{malformed} postings could not be classified")
    urls = [canonical_url(p.url) for p in postings if p.url]
    if urls:
        marks = ",".join("?" for _ in urls[:200])
        elsewhere = store.one(f"SELECT COUNT(DISTINCT canonical_url) c FROM jobs WHERE canonical_url IN ({marks}) AND employer_id IS NOT NULL "
                              f"AND lower(employer_id) != lower(?)", (*urls[:200], f"{m['ats']}:{m['board_token']}"))["c"]
        if elsewhere >= 3 and elsewhere >= 0.5 * len(urls):
            problems.append(f"{elsewhere} postings are already collected from another board (a duplicate board?)")
    baseline = json.loads(m["identity_signals"] or "null")
    shares = reg.identity_shares(postings, reg.entity_names(store, m["entity_id"]), m["official_domain"])
    changed = reg.drift(baseline, shares, board_name)
    if changed:
        problems.append(f"the board looks different from when it was verified: {changed}")
    if problems:
        return _validation_failed(ctx, log, m, "; ".join(problems), ats_client.requests - before)
    activity = "active" if postings else "empty"
    log.add("validation", "pass", notes=f"{len(postings)} postings read and classified; board {activity}")
    reg._event(store, "discovery", "validation_scan_passed", "success", mapping_id, postings=len(postings))
    reg._event(store, "discovery", "board_empty" if not postings else "board_active_listing", "informational", mapping_id)
    values = {"validation_status": "passed", "board_activity": activity, "last_checked_at": store.clock.now()}
    from .board_discovery import cfg as discovery_cfg
    if discovery_cfg(ctx.policy)["auto_activate"] or m["override_reason"]:
        values.update(verification_status="active")
        reg.update_board(store, mapping_id, values)
        reg.admit(store, mapping_id, via="override" if m["override_reason"] else "verified", actor="careeros:validation")
        reg._event(store, "discovery", "employer_auto_approved", "success", mapping_id, trust=m["board_trust_score"])
        reg._event(store, "discovery", "board_active", "success", mapping_id)
        if store.one("SELECT 1 FROM board_mappings WHERE migrated_to = ?", (mapping_id,)):
            reg._event(store, "discovery", "ats_migration_verified", "success", mapping_id)
        outcome = "active"
    else:
        reg.update_board(store, mapping_id, values)
        reg.open_case(store, mapping_id=mapping_id, entity_id=m["entity_id"], reason_code="ready_to_activate",
                      detail=f"{m['board_url']} is verified and passed its validation scan: careeros sources activate {mapping_id}")
        outcome = "validated"
    log.finish(outcome, trust=m["board_trust_score"], ats_requests=ats_client.requests - before)
    return outcome


def _validation_failed(ctx, log, m, why: str, requests: int) -> str:
    store = ctx.store
    reg.update_board(store, m["mapping_id"], {"validation_status": "failed", "verification_status": "review_required",
                                               "verification_failure": f"validation_failed: {why}"[:200]})
    reg.open_case(store, mapping_id=m["mapping_id"], entity_id=m["entity_id"], reason_code="validation_failed", detail=why)
    reg._event(store, "discovery", "validation_scan_failed", "failure", m["mapping_id"], why=why[:200])
    log.add("validation", "fail", notes=why)
    log.finish("validation_failed", failure="validation_failed", ats_requests=requests, detail=why)
    return "validation_failed"


# --- the daily maintenance pass ----------------------------------------------------------------------------------------------
@contextmanager
def discovery_client_for(ctx):
    """An ATS client restricted to the four public listing APIs, with the verification request budget."""
    from .board_discovery import discovery_client
    total = int(reg.bcfg(ctx.policy)["ats_request_budget"])
    client = discovery_client(ctx.policy, RequestBudget(total, {p: total for p in ("greenhouse", "lever", "ashby", "workable")}))
    try:
        yield client
    finally:
        client.close()


def default_site_factory(policy, budget: RequestBudget):
    cfg = reg.bcfg(policy)

    def factory(domain: str) -> SiteFetcher:
        return SiteFetcher(domain, budget=budget, max_pages=int(cfg["max_pages_per_employer"]))
    return factory


def due_reverifications(store, now: str) -> list:
    return store.all(
        """SELECT * FROM board_mappings WHERE collection = 'allowed' AND entity_id IS NOT NULL
           AND verification_status IN ('active','verification_required','review_required')
           AND (reverify_at <= ? OR (reverify_at IS NULL AND provenance = 'legacy_unknown'))
           ORDER BY (paused_reason IS NOT NULL) DESC, (verification_status = 'active') DESC, COALESCE(relevant_roles, 0) DESC,
                    COALESCE(reverify_at, created_at)""", (now,))


def pending_candidates(store, now: str) -> list:
    return store.all(
        """SELECT * FROM board_mappings WHERE collection = 'allowed' AND verification_status = 'candidate'
           AND identity_status != 'rejected' AND (reverify_at IS NULL OR reverify_at <= ?)
           ORDER BY COALESCE(relevant_roles, 0) DESC, created_at""", (now,))


def maintain(ctx, *, site_factory=None, ats_client=None, run_started: str | None = None) -> Counter:
    """Daily: legacy import, expired overrides, re-verification and candidate verification (only when official-site
    checks are on), validation scans of quarantined boards, and career-fit refresh."""
    from .board_discovery import discovery_client
    from . import employer_intel
    store, policy = ctx.store, ctx.policy
    cfg = reg.bcfg(policy)
    counts = Counter()
    if not cfg["enforce"]:
        return counts
    counts["boards_registered"] = reg.sync_legacy(store)
    counts["overrides_expired"] = reg.expire_overrides(store)
    now = store.clock.now()
    run_started = run_started or now
    own_client = ats_client is None
    if own_client:
        total = int(cfg["ats_request_budget"])
        ats_client = discovery_client(policy, RequestBudget(total, {p: total for p in ("greenhouse", "lever", "ashby", "workable")}))
    try:
        for m in pending_candidates(store, now):
            if m["entity_id"] is None:
                reg.update_board(store, m["mapping_id"], {"entity_id": reg.ensure_entity(store, m["employer_name"], domain=m["official_domain"],
                                                                                         origin=m["origin"])})
        if reg.site_checks_enabled(policy):
            budget = RequestBudget(int(cfg["daily_site_requests"]), {"site": int(cfg["daily_site_requests"])})
            factory = site_factory or default_site_factory(policy, budget)
            seen: set = set()
            work = ([("reverify", m) for m in due_reverifications(store, now)[:int(cfg["max_reverifications_per_run"])]]
                    + [("initial", m) for m in pending_candidates(store, now)[:int(cfg["max_verifications_per_run"])]])
            for kind, m in work:
                if m["entity_id"] in seen or budget.remaining() <= 0:
                    continue
                seen.add(m["entity_id"])
                try:
                    outcome = verify_employer(ctx, m["entity_id"], site_factory=factory, ats_client=ats_client,
                                              claim_mapping_id=m["mapping_id"], kind=kind)["outcome"]
                except FetchError as exc:
                    if exc.code == "budget_exhausted":
                        break
                    outcome = "error"
                counts[f"verification_{kind}_{outcome}"] += 1
        else:
            for m in pending_candidates(store, now):
                if reg.open_case(store, mapping_id=m["mapping_id"], entity_id=m["entity_id"], reason_code="site_check_disabled",
                                 detail="official-site checks are off (board_verification.fetch_official_sites), so CareerOS "
                                        f"cannot verify {m['board_url']}; approve it yourself with careeros sources approve "
                                        f"{m['mapping_id']} --reason \"...\", or turn the checks on"):
                    counts["verification_waiting_for_site_checks"] += 1
        degraded = {r["provider"] for r in store.all("SELECT provider FROM provider_status WHERE state != 'healthy'")}
        for m in store.all("SELECT * FROM board_mappings WHERE verification_status = 'quarantined' AND quarantined_at < ? "
                           "ORDER BY quarantined_at", (run_started,))[:int(cfg["max_validations_per_run"])]:
            counts[f"validation_{validate_board(ctx, m['mapping_id'], ats_client, provider_degraded=m['ats'] in degraded)}"] += 1
    finally:
        if own_client:
            ats_client.close()
    counts.update(employer_intel.refresh(ctx))
    return counts


def explain(store, mapping_id: str) -> list[str]:
    """Why CareerOS trusts (or does not trust) a board: its status and the latest evidence chain."""
    m = reg.mapping(store, mapping_id)
    if m is None:
        return [f"{mapping_id}: unknown board"]
    lines = [f"{mapping_id}  {m['employer_name']}  status={m['verification_status']}"
             + (f" (paused: {m['paused_reason']})" if m["paused_reason"] else "")
             + f"  trust={m['board_trust_score']}  health={m['board_health_score']}  handshake={m['handshake_status']}"
             f"  identity={m['identity_check']}  provenance={m['provenance']}"]
    if m["override_reason"]:
        lines.append(f"  manual approval by {m['override_by']}: {m['override_reason']} (expires {m['override_expires_at']})")
    run = store.one("SELECT * FROM verification_runs WHERE mapping_id = ? OR run_id IN (SELECT run_id FROM verification_evidence "
                    "WHERE mapping_id = ?) ORDER BY finished_at DESC, rowid DESC LIMIT 1", (mapping_id, mapping_id))
    if not run:
        lines.append("  never verified against the employer's own site")
        return lines
    lines.append(f"  last check {run['finished_at'][:16]} ({run['kind']}): {run['outcome']}; discovery claimed "
                 f"{run['discovery_board'] or 'nothing'}, the employer's site showed {', '.join(json.loads(run['verifier_boards'])) or 'nothing'}")
    for e in store.all("SELECT * FROM verification_evidence WHERE run_id = ? AND (mapping_id = ? OR mapping_id IS NULL OR evidence_type "
                       "IN ('official_domain','careers_page')) ORDER BY rowid", (run["run_id"], mapping_id)):
        lines.append(f"  {'+' if e['result'] == 'pass' else '-' if e['result'] == 'fail' else ' '} {e['evidence_type']:22} "
                     f"{('+' + str(e['points'])) if e['points'] else '':>4}  "
                     + " ".join(x for x in (e["method"], e["source_url"] and f"from {e['source_url']}",
                                             e["destination_url"] and f"to {e['destination_url']}", e["notes"]) if x)[:220])
    return lines
