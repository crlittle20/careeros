"""CareerOS command line.

    careeros init                     create the vault folders and database, load the employer list
    careeros run                      discover + process + write today's report (the daily job)
    careeros discover [--employer T]  poll job boards
    careeros add-url URL [...]        add one posting from a link (Greenhouse/Lever/Ashby/Workable, or any page with --text-file)
    careeros process                  extract, gate and score pending jobs
    careeros report                   write the HTML report to the vault
    careeros review                   open the local review page (label jobs Apply / Stretch / Skip)
    careeros label JOB apply|stretch|skip [--reason ...]
    careeros applied JOB --channel direct|agency|referral|other [--agency NAME] [--notes ...]
    careeros app-status APPLICATION STATE [--notes ...]   e.g. screening, interviewing, rejected, ghosted
    careeros app-status APPLICATION void --reason "..."   an application recorded by mistake (kept, never counted)
    careeros release JOB [--hold ID --channel C [--agency NAME] --reason ...]   submission holds: show, or release one
    careeros release --revoke RELEASE_ID --reason ...      withdraw a release
    careeros dismiss JOB --reason R [--note ...] | careeros dismiss JOB --undo [--note ...]   dismiss, or reinstate
    careeros applications             list your applications and their status
    careeros funnel [--by source|family|profile|tier|channel|domain]   hiring outcomes and conversion rates
    careeros eval                     agreement between your labels and the system (Phase 1 exit check; three-way)
    careeros certifications           which certifications your target jobs ask for, and your status for each
    careeros employers list|add|verify
    careeros gmail connect|status|fetch|disconnect   read job-alert emails from your Gmail (see GMAIL_SETUP.md)
    careeros inbox                    read job recommendations from the private GitHub inbox now (also daily)
    careeros backup                   copy the database, profile and policy overrides to vault\\backups (also daily)
    careeros status | verify | unlock
"""
from __future__ import annotations

import argparse
import codecs
import io
import json
import logging
import re
import secrets
import sys
import uuid
from collections import Counter
from pathlib import Path

import httpx

from careeros.core.clock import Clock
from careeros.core.fingerprint import text_fingerprint
from careeros.core.locks import LockError, SingleWriterLock, force_unlock, hold
from careeros.core.policy import load_policy, load_yaml
from careeros.core.redact import RedactingFilter
from careeros.core.transitions import load_matrix
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board, fetch_single, parse_job_url
from careeros.discovery.http import FetchError, HttpClient, RequestBudget
from careeros.normalize.posting import RawPosting, canonical_url
from careeros.normalize.sanitize import clean_text, html_to_text
from careeros.paths import CONFIG_DIR, vault_paths
from careeros.pipeline import Context, ingest, process
from careeros.qualify.profile import load_profile
from careeros.review.applications import (CONFIRM_CODES, ApplicationConflict, ApplicationHold, SessionOpen, application_matrix,
                                          applications, record_application, update_application)
from careeros.review.labels import agreement, label_backlog, record_label, three_way
from careeros.review.report import build_report, last_review_at, run_health

log = logging.getLogger("careeros")


def _setup_logging(paths) -> None:
    paths["logs"].mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(paths["logs"] / "careeros.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _context(paths, with_ai: bool = False, clock=None) -> Context:
    policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", paths["overrides"])
    profile = load_profile(paths["profile"])
    store = open_store(paths["db"], clock or Clock(), policy.fingerprint)
    ai = None
    if with_ai:
        from careeros.extract.ai import build_reader
        ai = build_reader(policy)
    return Context(store, policy, profile, load_matrix(CONFIG_DIR / "workflows" / "job.json"), paths["snapshots"], ai)


def _ai_status(ctx: Context) -> str:
    if ctx.ai is not None:
        return f"on ({ctx.ai.model})"
    if not ctx.policy.get("extraction", "ai", "enabled"):
        return "off (disabled in policy)"
    from careeros.extract.ai import read_api_key
    return "ready" if read_api_key() else "off (no API key stored as 'CareerOS/anthropic')"


def _employer_row(store, ats: str, token: str):
    return store.one("SELECT * FROM employers WHERE ats = ? AND board_token = ?", (ats, token))


def _add_employer(ctx: Context, name: str, ats: str, token: str, parent: str | None = None, industry: str | None = None,
                  status: str = "candidate", admitted_via: str | None = None) -> dict:
    """An employers row. Unless `admitted_via` says otherwise (the curated seed list is 'legacy'), the board is NOT in
    the daily scan until it is verified against the employer's own site (or you approve it)."""
    existing = _employer_row(ctx.store, ats, token)
    if existing:
        return dict(existing)
    row = {"employer_id": f"{ats}:{token}", "name": name, "parent": parent or name, "ats": ats, "board_token": token,
           "industry": industry, "status": status, "admitted_via": admitted_via}
    writer = ctx.store.writer("admin")
    with ctx.store.tx():
        writer.insert("employers", row)
        writer.event("employer_added", outcome="success", entity_type="employer", entity_id=row["employer_id"],
                     metadata={"ats": ats, "status": status})
    return row


def _run_record(ctx: Context, kind: str):
    run_id = f"run:{uuid.uuid4().hex[:12]}"
    ctx.store.writer("runner").insert("runs", {"run_id": run_id, "kind": kind, "started_at": ctx.store.clock.now(),
                                               "status": "running", "policy_fingerprint": ctx.policy.fingerprint})
    return run_id


def _finish_run(ctx: Context, run_id: str, counts: Counter, status: str = "completed", ended: str | None = None) -> None:
    ctx.store.writer("runner").update("runs", {"ended_at": ended or ctx.store.clock.now(), "status": status,
                                               "counts_json": json.dumps(dict(counts))}, "run_id = ?", (run_id,))


# --- commands ----------------------------------------------------------------------------------------
def cmd_init(ctx: Context, paths, args) -> None:
    for key in ("snapshots", "reports", "logs"):
        paths[key].mkdir(parents=True, exist_ok=True)
    seeds = load_yaml(CONFIG_DIR / "employers.yaml").get("employers", [])
    added = 0
    for e in seeds:
        if not _employer_row(ctx.store, e["ats"], e["token"]):  # the curated seed list: scanned, re-verified like legacy boards
            _add_employer(ctx, e["name"], e["ats"], e["token"], e.get("parent"), e.get("industry"), admitted_via="legacy")
            added += 1
    print(f"Vault ready at {paths['root']}\nDatabase: {paths['db']}\nEmployers added: {added}")


def cmd_employers(ctx: Context, paths, args) -> None:
    if args.action == "add":
        row = _add_employer(ctx, args.name, args.ats, args.token, args.parent, args.industry)
        from careeros.discovery import board_registry as registry
        registry.sync_legacy(ctx.store)
        print(f"{row['employer_id']} ({row['status']}): scanned once it is verified against the employer's own site; "
              f"see careeros sources explain {row['employer_id']}, or careeros sources approve {row['employer_id']} --reason ...")
        return
    rows = ctx.store.all("SELECT * FROM employers ORDER BY status, name")
    if args.action == "verify":
        client = HttpClient()
        try:
            for r in rows:
                if r["status"] == "disabled":
                    continue
                try:
                    count = len(fetch_board(client, r["ats"], r["board_token"]))
                    _set_employer(ctx, r, "verified")
                    print(f"verified  {r['name']:28} {count} jobs")
                except FetchError as exc:
                    _set_employer(ctx, r, "invalid" if exc.code == "not_found" else r["status"])
                    print(f"{exc.code:9} {r['name']}")
        finally:
            client.close()
        return
    for r in rows:
        print(f"{r['status']:10} {r['ats']:10} {r['board_token']:20} {r['name']}")


def _set_employer(ctx: Context, row, status: str) -> None:
    values = {"status": status}
    if status == "verified":
        values["verified_at"] = ctx.store.clock.now()
    ctx.store.writer("admin").update("employers", values, "employer_id = ?", (row["employer_id"],))


def cmd_discover(ctx: Context, paths, args) -> Counter:
    from careeros.discovery import board_registry as registry
    from careeros.discovery import source_health as health
    from careeros.discovery.http import PROVIDER_HOSTS
    counts = Counter()
    registry.sync_legacy(ctx.store)  # every board has an employer record and a lifecycle before it is scanned
    rows = ctx.store.all("SELECT * FROM employers WHERE status IN ('candidate','verified') ORDER BY name")
    if args.employer:
        rows = [r for r in rows if r["board_token"] == args.employer]
    # every request and redirect hop stays on the four public listing APIs
    client = HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5), allowed_hosts=set(PROVIDER_HOSTS))
    now = ctx.store.clock.now()
    failures, attempts = [], Counter()
    try:
        from careeros.discovery import sources
        for r in rows:
            if sources.status(r["ats"]) != "allowed":  # site policy changed: skip quietly, never fetch
                counts["board_skipped_by_policy"] += 1
                continue
            admitted, reason = registry.scan_gate(ctx.store, r, ctx.policy, now)
            if not admitted:  # not verified against the employer's own site, or paused
                counts["board_not_admitted"] += 1
                continue
            scan_now, level, why = health.due(ctx.store, r, ctx.policy, now)
            if not scan_now and not args.employer:  # adaptive scanning: a quiet board waits for its next turn
                counts[f"board_not_due_{level}"] += 1
                continue
            attempts[r["ats"]] += 1
            before = client.requests
            try:
                postings = fetch_board(client, r["ats"], r["board_token"])
            except FetchError as exc:
                failures.append((r, exc.code, exc.retryable, client.requests - before))
                counts["board_errors"] += 1
                print(f"  ! {r['name']}: {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001 - e.g. a board answering in an unexpected shape
                failures.append((r, f"board_unexpected_error:{type(exc).__name__}", False, client.requests - before))
                counts["board_errors"] += 1
                print(f"  ! {r['name']}: {type(exc).__name__}")
                continue
            if r["status"] != "verified":
                _set_employer(ctx, r, "verified")
            ctx.store.writer("discovery").update("employers", {"last_polled_at": ctx.store.clock.now()}, "employer_id = ?", (r["employer_id"],))
            changed = registry.check_drift(ctx.store, r, postings)
            if changed:  # the board may now belong to someone else: nothing is ingested until it is re-verified
                m = registry.mapping_for_employer(ctx.store, r)
                registry.pause(ctx.store, m["mapping_id"], f"board identity changed: {changed}", failure="identity_changed",
                               event="board_fingerprint_changed")
                health.record_scan(ctx.store, r["employer_id"], "suspicious", postings=len(postings), requests=client.requests - before,
                                   error_code="identity_changed")
                counts["board_identity_changed"] += 1
                print(f"  ! {r['name']}: paused, {changed}")
                continue
            outcome = health.classify_scan(r["last_posting_count"], len(postings), ctx.policy)
            result = ingest(ctx, dict(r), postings, full_board=outcome == "complete")  # a suspicious scan never closes jobs
            health.record_scan(ctx.store, r["employer_id"], outcome, postings=len(postings), requests=client.requests - before,
                               closed=result.get("closed", 0), possibly_absent=result.get("possibly_absent", 0))
            _record_board_health(ctx, r, postings=len(postings))
            if outcome == "complete":
                ctx.store.writer("discovery").update("employers", {"last_complete_scan_at": now, "last_posting_count": len(postings)},
                                                     "employer_id = ?", (r["employer_id"],))
            else:
                counts["board_suspicious"] += 1
            counts.update(result)
            print(f"  {r['name']:28} {len(postings):4} postings  " + ("(SUSPICIOUS: kept, nothing closed) " if outcome != "complete" else "")
                  + " ".join(f"{k}={v}" for k, v in sorted(result.items())))
        counts.update(_settle_scan_failures(ctx, failures, attempts))
        counts["board_scans_attempted"] = sum(attempts.values())
        if ctx.policy.get("scanning", "adaptive", default=False):
            counts.update(_refresh_shortlisted(ctx, client, now))
    finally:
        client.close()
    return counts


def _record_board_health(ctx: Context, employer, postings: int | None = None, failure: str | None = None) -> None:
    """After every scan: the board's health score, and its activity (active / empty / unavailable) as last seen."""
    from careeros.discovery import board_registry as registry
    from careeros.discovery import source_health as health
    m = registry.mapping_for_employer(ctx.store, employer)
    if m is not None:
        values = {"board_health_score": health.board_health(ctx.store, employer["employer_id"])}
        if postings is not None:
            values["board_activity"] = "active" if postings else "empty"
        elif failure == "not_found":
            values["board_activity"] = "unavailable"
        registry.update_board(ctx.store, m["mapping_id"], values)


def _settle_scan_failures(ctx: Context, failures: list, attempts: Counter) -> Counter:
    """Failures are judged after the whole scan: when many boards of one provider fail together, the provider (or
    CareerOS's adapter for it) is degraded, and no single employer is held responsible."""
    from careeros.discovery import board_registry as registry
    from careeros.discovery import source_health as health
    store, counts = ctx.store, Counter()
    degraded = health.degraded_providers(Counter(r["ats"] for r, *_ in failures), attempts, ctx.policy)
    newly = []
    for provider in attempts:
        failed = sum(1 for r, *_ in failures if r["ats"] == provider)
        if health.set_provider_state(store, provider, "degraded" if provider in degraded else "healthy",
                                     f"{failed} of {attempts[provider]} boards failed this run" if failed else None) and provider in degraded:
            store.writer("discovery").exception("integration", "high", f"{provider}: {failed} of {attempts[provider]} boards failed at once; "
                                                "treated as a provider or adapter problem, not as employer failures",
                                                entity_type="ats_provider", entity_id=provider, error_code="provider_degraded", retryable=True)
            print(f"  ! {provider} looks degraded ({failed} of {attempts[provider]} boards failed): no employer is penalised")
            newly.append(f"{provider} ({failed} of {attempts[provider]} boards failed)")
    if newly:   # OPP-54: a day of lost intake if nobody notices; one notice for every provider that turned degraded
        _problem(ctx, "CareerOS: job-board provider degraded", f"{', '.join(newly)}. No employer is penalised. "
                                                               "See: careeros sources providers")
    counts["providers_degraded"] = len(degraded)
    for r, code, retryable, requests in failures:
        outage = r["ats"] in degraded
        if not outage:
            store.writer("discovery").exception("integration", "low" if retryable else "medium", f"board fetch failed: {r['name']} ({code})",
                                                entity_type="employer", entity_id=r["employer_id"], error_code=code[:80], retryable=retryable)
        outcome = "parse_error" if code.startswith("board_unexpected_error") else health.failure_outcome(code)
        health.record_scan(store, r["employer_id"], outcome, requests=requests, error_code=code[:80], provider_degraded=outage)
        _record_board_health(ctx, r, failure=None if outage else code)
        if outage:
            continue
        m = registry.mapping_for_employer(store, r)
        if code == "not_found" and m is not None:  # the board is gone: paused until the employer's site is checked again
            registry.pause(store, m["mapping_id"], "the board no longer exists (HTTP 404)", failure="board_not_found")
            counts["board_paused_not_found"] += 1
        elif code == "access_restricted" and health.restricted_streak(store, r["employer_id"]) >= int(
                ctx.policy.get("scanning", "restricted_disable_after", default=3) or 3):
            _set_employer(ctx, r, "disabled")  # restricted again and again: stop asking
        elif m is not None and health.failure_streak(store, r["employer_id"]) >= int(
                ctx.policy.get("scanning", "failures_before_reverify", default=3) or 3):
            registry.update_board(store, m["mapping_id"], {"reverify_at": store.clock.now()})  # re-check at the source
            counts["board_reverify_triggered"] += 1
    return counts


def _refresh_shortlisted(ctx: Context, client: HttpClient, now: str) -> Counter:
    """Shortlisted openings stay fresh even when their board is scanned less often: one single-posting check each.
    A 404 from Greenhouse / Lever's own posting endpoint is a confirmed closure; anything else is not."""
    from careeros.review.shortlist import shortlisted_job_ids
    counts = Counter()
    for job_id in shortlisted_job_ids(ctx.store):
        job = ctx.store.one("SELECT j.*, e.last_complete_scan_at FROM jobs j LEFT JOIN employers e ON e.employer_id = j.employer_id "
                            "WHERE j.job_id = ?", (job_id,))
        if not job or job["ats"] not in ("greenhouse", "lever") or (job["last_complete_scan_at"] or "")[:10] == now[:10]:
            continue
        try:
            fetch_single(client, job["ats"], job["job_id"].split(":")[1], job["external_id"])
            ctx.store.writer("discovery").update("jobs", {"last_seen_at": now, "absent_scans": 0}, "job_id = ?", (job_id,))
            counts["shortlist_still_open"] += 1
        except FetchError as exc:
            if exc.code == "not_found" and job["current_state"] not in ("closed", "duplicate"):
                with ctx.store.tx():
                    ctx.store.writer("discovery").update("jobs", {"closed_at": now}, "job_id = ?", (job_id,))
                    ctx.store.writer("discovery").transition(ctx.matrix, job_id, "closed", reason_code="posting_removed")
                counts["shortlist_closed"] += 1
    return counts


def _ingest_board_link(ctx: Context, client: HttpClient, url: str, employer_name: str | None = None):
    """Fetch one posting from a public job board link and ingest it. Returns (posting, counts, job_id)."""
    ats, token, external_id = parse_job_url(url)
    posting = fetch_single(client, ats, token, external_id)  # first: a link that does not resolve adds no employer
    employer = _employer_row(ctx.store, ats, token)
    if employer is None:  # self-growing employer list, named after the company when the link came with one
        from careeros.discovery.saved import parent_for
        employer = _add_employer(ctx, employer_name or token, ats, token,
                                 parent=parent_for(employer_name, ctx.policy) if employer_name else None)
    _set_employer(ctx, employer, "verified")
    result = ingest(ctx, dict(employer), [posting], source="manual_url", full_board=False)
    job = ctx.store.one("SELECT job_id FROM jobs WHERE ats = ? AND employer_id = ? AND external_id = ?",
                        (ats, employer["employer_id"], posting.external_id))
    return posting, result, job["job_id"] if job else None


def cmd_add_url(ctx: Context, paths, args) -> None:
    if parse_job_url(args.url) and not args.text_file:
        client = HttpClient()
        try:
            posting, result, _ = _ingest_board_link(ctx, client, args.url, args.employer)
        finally:
            client.close()
        print(f"{posting.title} — " + " ".join(f"{k}={v}" for k, v in result.items()))
        return
    if not (args.text_file and args.employer and args.title):
        sys.exit("For links that are not Greenhouse/Lever/Ashby/Workable, save the posting text to a file and pass "
                 "--text-file FILE --employer NAME --title TITLE [--location ...].")
    raw = Path(args.text_file).read_text(encoding="utf-8")
    text = html_to_text(raw) if "<" in raw and ">" in raw else clean_text(raw)
    url = canonical_url(args.url) or args.url
    slug = re.sub(r"[^a-z0-9]+", "-", args.employer.lower()).strip("-")
    posting = RawPosting(ats="manual", board_token=slug, external_id=text_fingerprint(url)[:16], title=args.title,
                         location=args.location or "", url=url, apply_url=url, description_text=text,
                         raw={"url": url, "text": text, "employer": args.employer})
    from careeros.discovery.saved import parent_for
    result = ingest(ctx, {"employer_id": None, "name": args.employer, "parent": args.parent or parent_for(args.employer, ctx.policy)},
                    [posting], source="manual_url", full_board=False)
    print(f"{args.title} — " + " ".join(f"{k}={v}" for k, v in result.items()))


def _move(path: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    n = 1
    while target.exists():
        target = folder / f"{path.stem}.{n}{path.suffix}"
        n += 1
    path.rename(target)
    return target


def cmd_import(ctx: Context, paths, args) -> Counter:
    """Import postings you saved into the vault inbox (sites CareerOS may not read automatically)."""
    from careeros.discovery.saved import SavedPostingError, parent_for, parse_saved_file
    inbox = paths["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    day = ctx.store.clock.now()[:10]
    counts = Counter()
    files = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm", ".txt", ".eml"))
    if not files:
        print(f"Inbox is empty: save posting pages (Ctrl+S, 'Webpage, HTML only') or job-alert emails (.eml) into {inbox}")
        return counts
    client = None
    for path in files:
        if path.suffix.lower() == ".eml":
            client = client or HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5))
            counts.update(_import_alert(ctx, path, client, inbox, day))
            continue
        try:
            posting, employer = parse_saved_file(path)
        except (SavedPostingError, UnicodeError) as exc:
            ctx.store.writer("discovery").exception("validation", "low", f"saved posting not imported ({path.name}): {exc}",
                                                    error_code="saved_posting_unreadable")
            _move(path, inbox / "failed" / day)
            counts["failed"] += 1
            print(f"  ! {path.name}: {exc}")
            continue
        parent = parent_for(employer, ctx.policy)
        result = ingest(ctx, {"employer_id": None, "name": employer, "parent": parent}, [posting], source="manual_url", full_board=False)
        counts.update(result)
        _move(path, inbox / "imported" / day)
        print(f"  {posting.title} | {employer}{' (' + parent + ')' if parent != employer else ''} | "
              + " ".join(f"{k}={v}" for k, v in result.items()))
    if client:
        client.close()
    counts.update(process(ctx))
    cmd_report(ctx, paths, args)
    return counts


def _import_alert(ctx: Context, path: Path, client: HttpClient, inbox: Path, day: str) -> Counter:
    """One saved job-alert email file (.eml)."""
    from careeros.discovery.alerts import AlertEmailError, is_correspondence, parse_eml
    try:
        alert = parse_eml(path)
    except (AlertEmailError, UnicodeError, LookupError) as exc:
        ctx.store.writer("discovery").exception("validation", "low", f"alert email not imported ({path.name}): {exc}",
                                                error_code="alert_email_unreadable")
        _move(path, inbox / "failed" / day)
        print(f"  ! {path.name}: {exc}")
        return Counter(failed=1)
    if is_correspondence(alert.subject):  # application / interview / recruiter mail is never read for jobs
        _move(path, inbox / "failed" / day)
        print(f"  ! {path.name}: not a job alert (application or recruiter correspondence); skipped")
        return Counter(correspondence_skipped=1)
    counts = _record_alert(ctx, alert, client)
    _move(path, inbox / "imported" / day)
    print(f"  {path.name}: {len(alert.links)} links | " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts


def _record_alert(ctx: Context, alert, client: HttpClient, outcomes: list | None = None, profile: str = "primary") -> Counter:
    """Links on readable job boards are graded; every other job link becomes a lead for you to open yourself.
    `outcomes`, when given, receives (link, outcome) per link: graded / already_tracked / lead /
    outside_target_families / seen_before / unreadable_kept_as_lead / no_title / not_a_job_link / suppressed /
    malformed_row / row_error. One bad line never stops the others.
    `profile` "night" judges relevance with the NIGHT title families and tags the leads as NIGHT."""
    def note(link, outcome):
        if outcomes is not None:
            outcomes.append((link, outcome))

    from careeros.discovery.alerts import greenhouse_candidate, lead_id
    from careeros.normalize.posting import classify_family, excluded_term, norm
    from careeros.normalize.posting import contains_term
    from careeros.qualify.night import night_family
    counts = Counter()
    families, excluded = ctx.policy.get("families", default={}), ctx.policy.get("title_exclude", default=[])
    blocked = [norm(e) for e in ctx.policy.get("gate", "excluded_employers", default=[]) or [] if norm(e)]
    writer, now = ctx.store.writer("discovery"), ctx.store.clock.now()
    for link in alert.links:
        if link.site == "invalid":  # an inbox line whose link could not be read: reported, never stored
            note(link, "malformed_row")
            continue
        try:  # untrusted input: one bad line is flagged and logged, the rest of the batch carries on
            if not link.text:
                note(link, "no_title")
                continue
            if any(contains_term(norm(f"{link.text} {link.context} {link.url}"), b) for b in blocked):
                counts["lead_suppressed"] += 1  # excluded employer: not stored, not fetched, reason never written anywhere
                note(link, "suppressed")
                continue
            title_norm = norm(link.text)
            if profile == "night":
                family = None if excluded_term(title_norm, excluded) else night_family(title_norm, ctx.policy)
                storage_profile = "night"
            elif profile == "unified":
                # Route once for intake storage; the independent NIGHT processor
                # evaluates all tracked jobs when enabled.
                if excluded_term(title_norm, excluded):
                    primary_family = night_only_family = None
                else:
                    primary_family = classify_family(title_norm, families)
                    night_only_family = night_family(title_norm, ctx.policy)
                family = primary_family or night_only_family
                storage_profile = "night" if night_only_family and not primary_family else "primary"
            else:
                family = None if excluded_term(title_norm, excluded) else classify_family(title_norm, families)
                storage_profile = "primary"
            if not (link.job_like or family):  # navigation, settings and other links that are not postings
                note(link, "not_a_job_link")
                continue
            lid = lead_id(link.url)
            if ctx.store.one("SELECT 1 FROM leads WHERE lead_id = ?", (lid,)):
                counts["lead_seen_before"] += 1
                note(link, "seen_before")
                continue
            row = {"lead_id": lid, "url": link.url, "site": link.site, "title": link.text, "title_norm": title_norm, "family": family,
                   "context": link.context or None, "sender": alert.sender or None, "subject": alert.subject or None,
                   "received_at": alert.received_at, "first_seen_at": now, "status": "new", "job_id": None, "profile": storage_profile}
            from careeros.discovery import sources
            known = _known_job(ctx, link.url)
            parsed = parse_job_url(link.url)
            board_url = link.url if parsed else greenhouse_candidate(link.url)
            if board_url and sources.status(parse_job_url(board_url)[0]) != "allowed":
                board_url = None  # the source is manual or blocked under the current site policy: list it, never fetch it
            unverified_board = False
            if profile == "unified" and board_url:
                # A working ATS API does not prove that this is the named
                # employer's board. Only a separately approved active mapping
                # can authorize automatic neutral-intake fetching; otherwise
                # leave a lead for employer/board verification.
                from careeros.discovery.board_registry import mapping_for_board
                from careeros.discovery.posting_evidence import board_mapping_trusted
                board_parts = parse_job_url(board_url)
                if board_parts is None:
                    unverified_board = True
                    board_url = None
                    counts["unified_board_unverified"] += 1
                else:
                    board_ats, board_token, _ = board_parts
                    mapping = mapping_for_board(ctx.store, board_ats, board_token)
                    if not board_mapping_trusted(dict(mapping) if mapping else None, board_ats, board_token):
                        counts["unified_board_unverified"] += 1
                        unverified_board = True
                        board_url = None
            if not known and not board_url:
                known = _ledger_match(ctx, link, title_norm)
                if not known and _ledger_lead(ctx, link, title_norm):
                    counts["lead_seen_before"] += 1  # the same job already listed through another site or URL
                    note(link, "seen_before")
                    continue
            outcome = None
            if known:  # a job CareerOS already tracks (e.g. a careers-page link to a board posting it polls)
                row.update(status="ingested", job_id=known)
                outcome = "already_tracked"
            elif family and board_url:  # a public job board CareerOS may read: grade it like any posting
                try:
                    _, result, job_id = _ingest_board_link(ctx, client, board_url, _company_name(link.context))
                    row.update(status="ingested", job_id=job_id)
                    counts.update(result)
                    outcome = "graded"
                except FetchError as exc:
                    counts["lead_fetch_failed"] += 1
                    outcome = "unreadable_kept_as_lead" if board_url == link.url else None  # a wrong board-name guess is just a lead
                    if outcome:
                        print(f"    ! could not read {link.url}: {exc.code} (kept as a lead)")
            writer.insert("leads", row)
            from careeros.discovery.note import context_note
            from careeros.openings import record_observation
            fields = context_note(link.context)["fields"]
            from_inbox = (alert.sender or "").startswith("GitHub inbox")
            record_observation(ctx.store, source_kind="recommendation" if from_inbox else "alert",
                               evidence_level="recommendation" if from_inbox else "alert", job_id=row["job_id"], lead_id=lid,
                               url=link.url, requisition_id=fields.get("req"), title=link.text, company=_company_name(link.context),
                               pay_text=fields.get("pay"), published_at=None)
            counts["lead_ingested" if row["status"] == "ingested" else ("lead_new" if family else "lead_other_family")] += 1
            note(link, outcome or ("board_unverified" if unverified_board
                                   else "review_lead" if profile == "unified" and not family
                                   else "lead" if family else "outside_target_families"))
        except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
            writer.exception("data_quality", "medium", f"alert line not processed: {type(exc).__name__}: {exc}",
                             error_code="alert_line_failed")
            counts["lead_errors"] += 1
            note(link, "row_error")
    return counts


def _company_name(context: str | None) -> str | None:
    """The employer's display name as written next to a link ("Avalere Health · Remote"), for a newly found board.
    Untrusted text: plain names only (no email addresses or Note keys), at most 100 characters."""
    name = re.sub(r"\s+", " ", (context or "").split(" · ")[0]).strip()
    if not name or len(name) > 100 or re.search(r"[@=<>]|https?:", name):
        return None
    return name


def _company(context: str | None) -> str:
    """The employer named next to a link: alert emails and inbox issues both put it first ("Company · Location")."""
    from careeros.normalize.posting import norm
    return norm((context or "").split(" · ")[0])


def _same_company(row, company: str) -> bool:
    from careeros.normalize.posting import contains_term, norm
    names = norm(f"{row['employer_name'] or ''} {row['parent'] or ''}")
    return contains_term(names, company) or bool(norm(row["employer_name"] or "") and contains_term(company, norm(row["employer_name"])))


def _ledger_match(ctx: Context, link, title_norm: str) -> str | None:
    """Cross-source duplicate check (prompts v2.2 fingerprint fallback, requisition-aware since v2.5): a tracked job at
    the same employer with the same requisition id (`req=` in the inbox Note), or with the same title when the two
    requisition ids do not conflict. A different requisition id under the same title is a different job."""
    from careeros.discovery.note import requisition
    from careeros.discovery.seen_index import norm_req
    company = _company(link.context)
    if not company:
        return None
    req = norm_req(requisition(link.context))
    if req:
        for row in ctx.store.all("SELECT job_id, employer_name, parent, requisition_id FROM jobs WHERE requisition_id IS NOT NULL "
                                 "AND current_state != 'closed'"):
            if norm_req(row["requisition_id"]) == req and _same_company(row, company):
                return row["job_id"]
    for row in ctx.store.all("SELECT job_id, employer_name, parent, requisition_id FROM jobs WHERE title_norm = ? "
                             "AND current_state != 'closed'", (title_norm,)):
        if req and row["requisition_id"] and norm_req(row["requisition_id"]) != req:
            continue
        if _same_company(row, company):
            return row["job_id"]
    return None


def _ledger_lead(ctx: Context, link, title_norm: str) -> bool:
    """An earlier lead at the same employer reached through a different site or URL: the same requisition id, or the
    same title when the requisition ids do not conflict."""
    from careeros.discovery.note import requisition
    from careeros.discovery.seen_index import norm_req
    company = _company(link.context)
    if not company:
        return False
    req = norm_req(requisition(link.context))
    for r in ctx.store.all("SELECT title_norm, context FROM leads WHERE context IS NOT NULL AND (title_norm = ? OR context LIKE ?)",
                           (title_norm, "%req=%")):
        if _company(r["context"]) != company:
            continue
        other = norm_req(requisition(r["context"]))
        if req and other:
            if req == other:
                return True
        elif r["title_norm"] == title_norm:
            return True
    return False


def _known_job(ctx: Context, url: str) -> str | None:
    """The tracked job a link points to: same posting URL, or a careers-page link carrying a known Greenhouse job id."""
    from urllib.parse import parse_qs, urlsplit
    row = ctx.store.one("SELECT job_id FROM jobs WHERE url = ? OR canonical_url = ? LIMIT 1", (url, url))
    if row:
        return row["job_id"]
    gh_jid = (parse_qs(urlsplit(url).query).get("gh_jid") or [""])[0]
    if gh_jid.isdigit():
        row = ctx.store.one("SELECT job_id FROM jobs WHERE ats = 'greenhouse' AND external_id = ? LIMIT 1", (gh_jid,))
        if row:
            return row["job_id"]
    return None


def _gmail_pull(ctx: Context, client: HttpClient, query: str | None = None, credentials=None, gmail=None) -> Counter:
    """New job-alert emails from Gmail since the last pull. Raises GmailError (e.g. reconnect_needed)."""
    from careeros.discovery.alerts import AlertEmailError, is_correspondence, parse_eml_bytes
    from careeros.discovery.gmail import GmailError
    query = query or ctx.policy.get("operations", "gmail_query")
    if gmail is None:
        from careeros.core.credentials import WindowsCredentials
        gmail = _gmail_reader(credentials or WindowsCredentials())
        if gmail is None:
            raise GmailError("not_connected", "Gmail is not set up (see GMAIL_SETUP.md)")
    counts, writer = Counter(), ctx.store.writer("discovery")
    try:
        for message_id in gmail.search(query):
            if ctx.store.one("SELECT 1 FROM alert_messages WHERE message_id = ?", (message_id,)):
                continue
            try:
                alert = parse_eml_bytes(gmail.raw(message_id))
            except (AlertEmailError, UnicodeError, LookupError) as exc:
                writer.exception("validation", "low", f"Gmail alert not readable: {exc}", error_code="alert_email_unreadable")
                counts["failed"] += 1
                links = 0
            except GmailError:
                raise  # Gmail itself is down: stop, report, and let the rest of the daily run carry on
            except Exception as exc:  # noqa: BLE001 - one odd message never stops the others
                writer.exception("validation", "low", f"Gmail alert not readable: {type(exc).__name__}",
                                 error_code="alert_email_unreadable")
                counts["failed"] += 1
                links = 0
            else:
                if is_correspondence(alert.subject):  # e.g. a LinkedIn application update from the alert sender
                    counts["correspondence_skipped"] += 1
                    links = 0
                else:
                    counts.update(_record_alert(ctx, alert, client))
                    links = len(alert.links)
            writer.insert("alert_messages", {"message_id": message_id, "source": "gmail", "processed_at": ctx.store.clock.now(),
                                              "links": links})
            counts["gmail_messages"] += 1
    finally:
        gmail.close()
    return counts


def _inbox_pull(ctx: Context, client: HttpClient, repo: str, runner=None) -> Counter:
    """Open issues in the private recommendations repo: grade or list each job, reply on the issue, close it."""
    import subprocess
    from careeros.discovery import github_inbox as inbox
    from careeros.qualify import night
    runner = runner or subprocess.run
    counts, writer = Counter(), ctx.store.writer("discovery")
    for row in ctx.store.all("SELECT number, reply FROM inbox_issues WHERE repo = ? AND closed = 0", (repo,)):
        try:  # read earlier but not closed (e.g. GitHub was unreachable): finish the reply now
            inbox.close_with_reply(repo, row["number"], row["reply"], runner)
            writer.update("inbox_issues", {"closed": 1}, "repo = ? AND number = ?", (repo, row["number"]))
        except inbox.InboxError:
            counts["inbox_close_failed"] += 1
    for issue in inbox.open_issues(repo, runner):
        if ctx.store.one("SELECT 1 FROM inbox_issues WHERE repo = ? AND number = ?", (repo, issue["number"])):
            continue
        profile = inbox.issue_profile(issue)
        if profile in ("night", "unified") and not night.enabled(ctx.policy):
            # A neutral intake may contain NIGHT-only jobs. Never silently
            # treat it as PRIMARY when the NIGHT evaluator is unavailable.
            counts["inbox_night_waiting"] += 1
            print(f"  issue #{issue['number']} needs NIGHT evaluation: left open (NIGHT profile is off)")
            continue
        try:  # one unreadable issue never stops the others; it stays open and is retried next run
            alert, outcomes = inbox.issue_alert(issue), []
            counts.update(_record_alert(ctx, alert, client, outcomes, profile=profile))
            if profile == "unified":
                # A malformed or failed row was never safely recorded. Keep the
                # neutral issue open so it can be corrected/retried; any valid
                # rows already received are idempotently recognized next time.
                unresolved = sum(outcome in ("malformed_row", "row_error", "no_title")
                                 for _, outcome in outcomes)
                if unresolved:
                    counts["inbox_unresolved_rows"] += unresolved
                    print(f"  ! issue #{issue['number']} has {unresolved} unresolved row(s): kept open for retry")
                    continue
            gmail = inbox.gmail_status(issue.get("body") or "")
            reply = inbox.reply_text(ctx.store.clock.now()[:10], outcomes, gmail)
            writer.insert("inbox_issues", {"repo": repo, "number": issue["number"], "processed_at": ctx.store.clock.now(),
                                           "links": len(alert.links), "reply": reply, "closed": 0, "gmail_status": gmail,
                                           "profile": profile})
        except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
            writer.exception("data_quality", "medium", f"inbox issue #{issue['number']} not processed: {type(exc).__name__}: {exc}",
                             error_code="inbox_issue_failed")
            counts["inbox_issue_errors"] += 1
            print(f"  ! issue #{issue['number']} could not be processed: {type(exc).__name__} (left open, retried next run)")
            continue
        counts["inbox_issues"] += 1
        print(f"  issue #{issue['number']}: {len(alert.links)} links")
        try:
            inbox.close_with_reply(repo, issue["number"], reply, runner)
            writer.update("inbox_issues", {"closed": 1}, "repo = ? AND number = ?", (repo, issue["number"]))
        except inbox.InboxError as exc:
            counts["inbox_close_failed"] += 1
            print(f"    ! could not reply on issue #{issue['number']}: {exc} (will retry next run)")
    return counts


def _daily_inbox(ctx: Context) -> Counter:
    """The GitHub recommendations inbox on the daily run, once configured. Problems are reported, never fatal."""
    import subprocess
    from careeros.discovery.github_inbox import InboxError
    from careeros.demo.sandbox import enabled as demo_mode
    repo = ctx.policy.get("operations", "github_inbox")
    if not repo or demo_mode():  # Demo Mode never contacts GitHub
        return Counter()
    print(f"Reading job recommendations from GitHub ({repo})...")
    client = HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5))
    try:
        counts = _inbox_pull(ctx, client, repo)
        print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "  no new recommendations")
        return counts
    except (InboxError, OSError, subprocess.SubprocessError, ValueError) as exc:
        ctx.store.writer("discovery").exception("integration", "medium", f"GitHub inbox not read: {exc}",
                                                error_code=getattr(exc, "code", "inbox_failed"))
        print(f"  ! GitHub inbox: {exc}")
        return Counter(inbox_errors=1)
    finally:
        client.close()


def cmd_inbox(ctx: Context, paths, args) -> None:
    from careeros.demo.sandbox import refuse
    refuse("the GitHub recommendation inbox")
    if not ctx.policy.get("operations", "github_inbox"):
        sys.exit("No GitHub inbox configured (operations.github_inbox in vault\\policy\\overrides.yaml).")
    counts = _daily_inbox(ctx)
    counts.update(process(ctx))
    counts.update(_publish_index(ctx, paths))
    cmd_report(ctx, paths, args)


def _publish_index(ctx: Context, paths, runner=None) -> Counter:
    """The seen-jobs index and Gmail intake status, rebuilt from the local ledger and published to the inbox repo
    (prompts v2.5). A local copy is always written to vault\\reports. Publishing problems are reported, never fatal."""
    import subprocess
    from careeros.discovery import github_inbox as inbox
    from careeros.discovery import seen_index
    repo = ctx.policy.get("operations", "github_inbox")
    try:
        records = seen_index.build(ctx.store)
        files = {**seen_index.files(records, int(ctx.policy.get("operations", "seen_index_max_per_file", default=1500))),
                 **seen_index.intake_file(ctx.store)}
        local = paths["reports"] / "inbox-index"
        local.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            target = local / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        from careeros.demo.sandbox import enabled as demo_mode
        if not repo or demo_mode() or not ctx.policy.get("operations", "publish_seen_index", default=True):
            return Counter(index_records=len(records))  # Demo Mode keeps the local copy only, never publishes
        changed = inbox.publish_files(repo, files, "CareerOS: update seen-jobs index and intake status", runner or subprocess.run)
        print(f"Seen-jobs index: {len(records)} jobs; " + (f"updated {', '.join(changed)}" if changed else "unchanged"))
        return Counter(index_records=len(records), index_files_changed=len(changed))
    except Exception as exc:  # noqa: BLE001 - GitHub down or a refused record: the daily run carries on
        ctx.store.writer("discovery").exception("integration", "medium", f"seen-jobs index not published: {exc}",
                                                error_code=getattr(exc, "code", "index_publish_failed"))
        print(f"  ! seen-jobs index not published: {exc}")
        return Counter(index_errors=1)


def cmd_seen_index(ctx: Context, paths, args) -> None:
    counts = _publish_index(ctx, paths)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Local copy: {paths['reports'] / 'inbox-index'}")


def _gmail_method(credentials) -> str | None:
    """'api' (Google Cloud sign-in) or 'imap' (app password), whichever is stored; the API route wins if both are."""
    from careeros.discovery import gmail, imap
    if credentials.read(gmail.CREDENTIAL_TARGET):
        return "api"
    if credentials.read_entry(imap.CREDENTIAL_TARGET):
        return "imap"
    return None


def _gmail_reader(credentials):
    from careeros.discovery import gmail, imap
    method = _gmail_method(credentials)
    if method == "api":
        return gmail.GmailClient(credentials)
    if method == "imap":
        return imap.ImapGmailClient(credentials)
    return None


def cmd_gmail(ctx: Context, paths, args) -> None:
    from careeros.demo.sandbox import refuse
    refuse("Gmail")
    import webbrowser
    from careeros.core.credentials import WindowsCredentials
    from careeros.discovery import gmail
    credentials = WindowsCredentials()
    try:
        if args.action == "connect":
            client_file = Path(args.client_file) if args.client_file else paths["root"] / "secrets" / "gmail_oauth_client.json"
            client = gmail.load_client(client_file)
            print("Opening Google sign-in in your browser. Approve read-only Gmail access there.")
            address = gmail.connect(client, credentials, open_browser=webbrowser.open)
            print(f"Connected to {address}. Job-alert emails will be read on each daily run.")
            print(f"Gmail search used: {ctx.policy.get('operations', 'gmail_query')}")
        elif args.action == "disconnect":
            from careeros.discovery import imap
            if gmail.disconnect(credentials):
                print("Google sign-in disconnected and token revoked.")
            elif credentials.delete(imap.CREDENTIAL_TARGET):
                print("App password removed from this PC. Also delete it at https://myaccount.google.com/apppasswords")
            else:
                print("Gmail was not set up.")
        elif args.action == "status":
            method = _gmail_method(credentials)
            if method is None:
                print("not set up (see GMAIL_SETUP.md)")
            else:
                try:
                    reader = _gmail_reader(credentials)
                    try:
                        print(f"working ({'Google sign-in' if method == 'api' else 'app password'}): {reader.address()}")
                        found = reader.search(ctx.policy.get("operations", "gmail_query"))
                        print(f"alert emails matching the search right now: {len(found)}")
                    finally:
                        reader.close()
                except gmail.GmailError as exc:
                    print(f"not working: {exc}")
            seen = ctx.store.one("SELECT COUNT(*) c, MAX(processed_at) t FROM alert_messages")
            print(f"alert emails read so far: {seen['c']} (last {seen['t'] or 'never'})")
        else:  # fetch now
            query = ctx.policy.get("operations", "gmail_query")
            if args.days:
                query = re.sub(r"newer_than:\d+d", f"newer_than:{int(args.days)}d", query)
            client = HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5))
            try:
                counts = _gmail_pull(ctx, client, query, credentials)
            finally:
                client.close()
            print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "  no new alert emails")
            counts.update(process(ctx))
            cmd_report(ctx, paths, args)
    except gmail.GmailError as exc:
        sys.exit(f"Gmail: {exc}")


def cmd_process(ctx: Context, paths, args) -> Counter:
    counts = process(ctx)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts


def cmd_report(ctx: Context, paths, args) -> Path:
    """The saved report. `args.completing` (run_id, end time), set by cmd_run: the daily run writing it, shown as completed
    in its header (Codex CR-2026-11-01-OBS-REVIEW, F5)."""
    paths["reports"].mkdir(parents=True, exist_ok=True)
    path = paths["reports"] / f"report-{ctx.store.clock.now()[:10]}.html"
    path.write_text(build_report(ctx.store, ctx.profile, ctx.policy, since=last_review_at(ctx.store), app_matrix=application_matrix(),
                                 completing=getattr(args, "completing", None)), encoding="utf-8")
    print(f"Report: {path}")
    return path


def cmd_run(ctx: Context, paths, args) -> None:
    run_id = _run_record(ctx, "daily")
    started = ctx.store.clock.now()
    counts = Counter()
    try:
        print("Discovering...")
        args.employer = None
        scanned = cmd_discover(ctx, paths, args)
        counts.update(scanned)
        if scanned["board_scans_attempted"] and scanned["board_errors"] >= scanned["board_scans_attempted"]:   # OPP-54
            _problem(ctx, "CareerOS: every job board failed", f"All {scanned['board_scans_attempted']} boards scanned this run failed "
                                                              "(no network?). Details are in vault\\logs\\daily-run.log")
        counts.update(_daily_gmail(ctx))
        counts.update(_daily_inbox(ctx))
        counts.update(_daily_discovery(ctx, paths))
        counts.update(_board_maintenance(ctx, started))
        print(f"Processing... (AI requirement reading: {_ai_status(ctx)})")
        counts.update(cmd_process(ctx, paths, args))
        counts.update(_publish_index(ctx, paths))
        ended = ctx.store.clock.now()   # F5: the report shows this run as completed then; a report that fails fails it
        args.completing = (run_id, ended)
        cmd_report(ctx, paths, args)
        _finish_run(ctx, run_id, counts, ended=ended)
    except Exception:
        _finish_run(ctx, run_id, counts, "failed")
        raise
    _after_run(ctx, paths, started)


def _daily_gmail(ctx: Context) -> Counter:
    """Gmail alerts on the daily run, only once connected. A Gmail problem is reported, never fatal to the run."""
    from careeros.core.credentials import CredentialError, WindowsCredentials
    from careeros.demo.sandbox import enabled as demo_mode
    from careeros.discovery.gmail import GmailError
    if demo_mode():  # Demo Mode never reads mail
        return Counter()
    try:
        credentials = WindowsCredentials()
        if _gmail_method(credentials) is None:
            return Counter()
    except CredentialError:
        return Counter()
    print("Reading job-alert emails from Gmail...")
    client = HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5))
    try:
        counts = _gmail_pull(ctx, client, credentials=credentials)
        print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "  no new alert emails")
        return counts
    except (GmailError, httpx.HTTPError, OSError) as exc:
        ctx.store.writer("discovery").exception("integration", "medium", f"Gmail alerts not read: {exc}",
                                                error_code=getattr(exc, "code", "gmail_failed"))
        print(f"  ! Gmail: {exc}")
        code = getattr(exc, "code", "")
        if code in ("reconnect_needed", "imap_login_failed") and ctx.policy.get("operations", "notifications", default=True):
            from careeros.ops.notify import toast
            toast("CareerOS: Gmail needs attention", "Gmail refused access. Run: careeros gmail status")
        return Counter(gmail_errors=1)
    finally:
        client.close()


def _problem(ctx: Context, title: str, body: str) -> None:
    """A failure notice (careeros.ops.notify.problem, OPP-54) when operations.notifications is on. Never fails the run."""
    if ctx.policy.get("operations", "notifications", default=True):
        from careeros.ops.notify import problem
        problem(title, body)


def _after_run(ctx: Context, paths, started: str) -> None:
    """Backup and notification: housekeeping that must never turn a good run into a failed one."""
    from careeros.ops.backup import backup
    from careeros.ops.notify import summary, toast
    try:
        print(f"Backup: {backup(ctx.store, paths, keep=int(ctx.policy.get('operations', 'backup_keep', default=14)))}")
    except Exception as exc:  # noqa: BLE001 - recorded as an exception for you to see in status
        log.exception("backup failed")
        ctx.store.writer("runner").exception("system", "medium", f"daily backup failed: {exc}", error_code="backup_failed")
        print(f"  ! backup failed: {exc}")
        _problem(ctx, "CareerOS: daily backup failed", "Details are in vault\\logs\\careeros.log")
    if ctx.policy.get("operations", "notifications", default=True):
        message = summary(ctx.store, started)
        if message:
            print(f"Notification: {message[0]}" + ("" if toast(*message) else " (could not be shown)"))


def cmd_backup(ctx: Context, paths, args) -> None:
    from careeros.ops.backup import backup, verify
    folder = backup(ctx.store, paths, keep=int(ctx.policy.get("operations", "backup_keep", default=14)))
    problems = verify(folder)
    print(f"Backup: {folder} ({'verified' if not problems else 'PROBLEMS: ' + ', '.join(problems)})")
    if problems:
        sys.exit(2)


def cmd_review(ctx: Context, paths, args) -> None:
    from careeros.review.server import serve
    serve(ctx.store, ctx.profile, ctx.policy, paths["lock"], port=args.port, open_browser=not args.no_browser)


def cmd_label(ctx: Context, paths, args) -> None:
    print(record_label(ctx.store, args.job_id, args.label, args.reason, key=f"cli:{secrets.token_hex(8)}",
                       profile="night" if args.night else "primary"))


def cmd_applied(ctx: Context, paths, args) -> None:
    try:
        app_id = record_application(ctx.store, application_matrix(), args.job_id, args.channel, policy=ctx.policy, agency=args.agency,
                                    notes=args.notes, key=f"cli:{secrets.token_hex(8)}", reapply=args.reapply, confirm=args.confirm,
                                    evidence=getattr(args, "evidence", "owner_attested"))
    except SessionOpen as exc:   # the open or uncertain session records it when you close it (OPP-77)
        sys.exit(f"Not recorded: {exc}")
    except ApplicationHold as exc:
        codes = exc.check.codes()
        sys.exit(f"Not recorded yet: {exc}."
                 + ("\n  Applying to this role again? Add --reapply \"<why>\"." if "applied_before" in codes else "")
                 + ("\n  Already applied this way? Add --confirm \"<your explanation>\" (it is kept with the record and never "
                    "releases a submission hold)." if any(c in CONFIRM_CODES for c in codes) else ""))
    except ApplicationConflict as exc:
        sys.exit(f"Not recorded: {exc}.")
    except ValueError as exc:
        sys.exit(f"Not recorded: {exc}")
    print(f"recorded {app_id}")


def cmd_release(ctx: Context, paths, args) -> None:
    """Submission holds (R2): show what holds a job and whether each channel may submit; release one hold for one
    posting and one channel; or revoke a release. A release never changes a tier and never skips the other checks."""
    from careeros.review import submission
    try:
        if args.revoke:
            print(f"revoked {args.revoke}: {submission.revoke(ctx.store, args.revoke, reason=args.reason, key=f'cli:{secrets.token_hex(8)}')}")
            return
        if not args.job_id:
            sys.exit("Give a JOB_ID (or --revoke RELEASE_ID).")
        if args.hold:
            if not args.channel:
                sys.exit("Not changed: --channel is required with --hold.")
            release_id = submission.release(ctx.store, ctx.policy, args.job_id, args.channel, hold_id=args.hold, reason=args.reason,
                                            agency=args.agency, key=f"cli:{secrets.token_hex(8)}")
            print(f"released {args.hold} for {args.job_id} via {args.channel}{' (' + args.agency + ')' if args.agency else ''}: "
                  f"{release_id}. Only that hold, posting and channel; every other check still applies.")
        print(submission.describe(ctx.store, ctx.policy, args.job_id, agency=args.agency, profile=ctx.profile))
    except ValueError as exc:
        sys.exit(f"Not changed: {exc}")


def cmd_app_status(ctx: Context, paths, args) -> None:
    try:
        update_application(ctx.store, application_matrix(), args.application_id, args.state, notes=args.notes,
                           key=f"cli:{secrets.token_hex(8)}", reason=getattr(args, "reason", None))
    except ValueError as exc:
        sys.exit(f"Not changed: {exc}")
    print(f"{args.application_id} -> {args.state}")


def cmd_applications(ctx: Context, paths, args) -> None:
    rows = applications(ctx.store)
    if not rows:
        print("No applications recorded yet. After you apply, use the 'I applied' button on the review page.")
    for r in rows:
        via = r["channel"] + (f" ({r['agency']})" if r["agency"] else "")
        print(f"{r['application_id']}  {r['applied_at'][:10]}  {r['current_state']:15} {via:22} {r['title']} | {r['employer_name']}")


def cmd_funnel(ctx: Context, paths, args) -> None:
    from careeros.review.funnel import discovered_by_source, funnel
    rows = funnel(ctx.store, args.by)
    if args.by == "source":
        print("jobs worth a look by source:", ", ".join(f"{k}={v}" for k, v in sorted(discovered_by_source(ctx.store).items())) or "none")
    if not rows:
        print("Nothing recommended or applied yet; the funnel fills in as CareerOS recommends jobs and you record outcomes.")
        return
    print(f"{args.by:16} recom approv applied resp screen interv offer hired | rej closed wdrawn ghost  resp%  intv%  offer%")
    for r in rows:
        pct = lambda v: "   -  " if v is None else f"{v:6.0%}"
        print(f"{str(r[args.by])[:16]:16} {r['recommended']:5} {r['approved']:6} {r['applied']:7} {r['responded']:4} "
              f"{r['screening']:6} {r['interviewing']:6} {r['offer']:5} {r['hired']:5} | {r['rejected']:3} {r['closed']:6} "
              f"{r['withdrawn']:6} {r['ghosted']:5} {pct(r['response_rate'])} {pct(r['interview_rate'])} {pct(r['offer_rate'])}"
              + ("  (small sample: observation only)" if r["applied"] and r["small_sample"] else ""))


# --- 2026-10-12: source discovery, title audit, intake, shortlist, drafts, metrics -------------------------------
def cmd_sources(ctx: Context, paths, args) -> None:
    """Verified employer-board discovery (Parts 1, 2, 8, 9). Nothing here opens a restricted site."""
    from careeros.discovery import board_discovery as bd
    from careeros.discovery import discovery_queue as dq
    from careeros.discovery import source_health
    store, action = ctx.store, args.action
    if action == "backfill":
        stats = dq.build_backfill(ctx, args.days)
        stats.update({f"curated_{k}": v for k, v in dq.load_curated(ctx, paths["curated"]).items()})
        print("queued: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        action = "run" if args.run else "plan"
    if action == "plan":
        rows = dq.plan(ctx, args.limit)
        print(f"{len(rows)} employer(s) due. Dry run: no request sent.")
        for r in rows:
            print(f"  {r['priority']:5.1f}  {r['employer'][:40]:40} {r['origin']:14} leads={r['leads']:<3} "
                  + (f"manual-only ({r['manual_system']})" if r["manual_system"] else
                     f"{r['relationship']}: " + (", ".join(r["targets"]) or "no safe board name to try")
                     + f" (<= {r['max_requests']} requests)"))
        if not args.run:
            print("Run it with: careeros sources run   (or backfill --run). Boards found are NOT added to the daily scan "
                  "until you activate them.")
        return
    if action == "run":
        stats = dq.run(ctx, limit=args.limit)
        print("discovery: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        return
    from careeros.discovery import board_registry as registry
    from careeros.discovery import board_verifier
    if action == "list":
        registry.sync_legacy(store)
        for m in store.all("SELECT * FROM board_mappings ORDER BY verification_status, updated_at DESC"):
            print(f"  {m['mapping_id']:40} {m['verification_status']:21} {m['employer_name'][:28]:28} trust={m['board_trust_score']} "
                  f"health={m['board_health_score']} relevant={m['relevant_roles']}" + (f"  PAUSED: {m['paused_reason']}" if m["paused_reason"] else ""))
        return
    if action == "health":
        registry.sync_legacy(store)
        rows = source_health.health(store, ctx.policy, store.clock.now())
        adaptive = bool(ctx.policy.get("scanning", "adaptive", default=False))
        print(f"adaptive scanning: {'ON' if adaptive else 'off (every board daily; cadence shown as a preview)'}")
        print(f"  {'employer':28} {'scanned':8} {'cadence':12} {'trust':5} {'fit':7} {'health':6} why")
        for r in rows:
            fit = f"{r['priority'] or '-'}:{r['career_fit'] if r['career_fit'] is not None else '-'}"
            print(f"  {r['employer'][:28]:28} {'yes' if r['admitted'] else 'no':8} {r['cadence']:12} {str(r['trust'] or '-'):5} {fit:7} "
                  f"{str(r['board_health'] if r['board_health'] is not None else '-'):6} {r['admission'][:40]}; {r['why'][:40]}")
        return
    if action == "providers":
        for p in store.all("SELECT * FROM provider_status ORDER BY provider"):
            print(f"  {p['provider']:11} {p['state']:9} since {p['since'][:16]}  {p['detail'] or ''}")
        return
    if action == "reviews":
        for c in store.all("SELECT * FROM board_reviews WHERE status = 'open' ORDER BY created_at"):
            print(f"  {c['case_id']}  {c['reason_code']:22} {c['mapping_id'] or c['entity_id'] or '':34} {c['detail'][:120]}")
        return
    if action == "validate":
        with board_verifier.discovery_client_for(ctx) as client:
            for m in store.all("SELECT mapping_id FROM board_mappings WHERE verification_status = 'quarantined'"
                               + (" AND mapping_id = ?" if args.target else ""), (args.target,) if args.target else ()):
                print(f"  {m['mapping_id']}: {board_verifier.validate_board(ctx, m['mapping_id'], client)}")
        return
    if action == "check":
        if not registry.site_checks_enabled(ctx.policy):
            sys.exit("official-site checks are off: set board_verification.fetch_official_sites: true in your policy overrides first")
        rows = [registry.mapping(store, args.target)] if args.target else board_verifier.due_reverifications(store, store.clock.now())[:args.limit]
        if not rows or rows[0] is None:
            sys.exit("usage: careeros sources check [MAPPING_ID]  (no target: boards due for re-verification)")
        budget = RequestBudget(int(registry.bcfg(ctx.policy)["daily_site_requests"]), {})
        with board_verifier.discovery_client_for(ctx) as client:
            for m in rows:
                entity_id = m["entity_id"] or registry.ensure_entity(store, m["employer_name"], domain=m["official_domain"])
                result = board_verifier.verify_employer(ctx, entity_id, site_factory=board_verifier.default_site_factory(ctx.policy, budget),
                                                        ats_client=client, claim_mapping_id=m["mapping_id"],
                                                        kind="initial" if m["verification_status"] == "candidate" else "reverify")
                print(f"  {m['mapping_id']}: {result['outcome']} ({result.get('comparison')}) {result['boards']}")
        return
    target = args.target
    if action in ("verify", "approve", "reject", "activate", "explain", "resolve", "domain") and not target:
        sys.exit(f"usage: careeros sources {action} MAPPING_ID")
    if action == "explain":
        print("\n".join(board_verifier.explain(store, target)))
    elif action == "resolve":
        count = registry.resolve_cases(store, case_id=target, resolution=args.note or "resolved by you", component="admin")
        print(f"{target}: {'resolved' if count else 'no open case with that id'}")
    elif action == "domain":
        domain = registry.clean_domain(args.value or args.domain)
        if not domain:
            sys.exit('usage: careeros sources domain "Employer Name"|MAPPING_ID example.com')
        m = registry.mapping(store, target)
        entity_id = m["entity_id"] if m and m["entity_id"] else registry.ensure_entity(store, m["employer_name"] if m else target)
        store.writer("admin").update("employer_entities", {"official_domain": domain, "domain_status": "unverified",
                                                           "updated_at": store.clock.now()}, "entity_id = ?", (entity_id,))
        registry.resolve_cases(store, entity_id=entity_id, reasons=("domain_unknown", "domain_not_employer", "domain_unconfirmed"),
                               resolution=f"official website set to {domain}", component="admin")
        if m is not None:
            registry.update_board(store, m["mapping_id"], {"reverify_at": store.clock.now()}, component="admin")  # check it next run
        print(f"{target}: official website candidate {domain} (checked against the employer's own site before it counts)")
    elif action in ("verify", "approve"):
        try:
            status = registry.approve(store, target, reason=args.reason or args.note or "",
                                      days=args.days or registry.bcfg(ctx.policy)["override_days"], domain=args.domain)
        except ValueError as exc:
            sys.exit(f"{target}: {exc}")
        print(f"{target}: approved by you ({status}); the approval expires in {args.days or registry.bcfg(ctx.policy)['override_days']} days")
    elif action == "reject":
        try:
            bd.reject(store, target, note=args.note)
        except ValueError as exc:
            sys.exit(f"{target}: {exc}")
        m = registry.mapping(store, target)
        employer = store.one("SELECT employer_id FROM employers WHERE ats = ? AND lower(board_token) = lower(?)",
                             (m["ats"], m["board_token"])) if m else None
        from careeros.pipeline import close_open_jobs
        closed, failed = close_open_jobs(ctx, employer["employer_id"], "board_retired", notes=f"board rejected: {args.note or ''}"[:200],
                                         trigger_type="human", trigger_id="user") if employer else (0, 0)
        print(f"{target}: rejected (never scanned); {closed} open job(s) closed, history kept"
              + (f"; {failed} could not be closed (see careeros status)" if failed else ""))
    elif action == "activate":
        try:
            print(f"{target}: added to the daily scan as {bd.activate(store, target)}")
        except ValueError as exc:
            sys.exit(str(exc))
    elif action == "alias":
        if not (target and args.of):
            sys.exit('usage: careeros sources alias "Alias Name" --of "Employer Name" [--relation alias|parent|subsidiary|former|legal]')
        bd.add_alias(store, target, args.of, args.relation, evidence=args.note)
        print(f"recorded: {target} is a verified {args.relation} of {args.of}")
    elif action == "suggest":
        if not target:
            sys.exit('usage: careeros sources suggest "Employer Name" [--domain example.com] [--board URL] [--note why]')
        candidate = bd.Candidate(employer_name=target, origin="curated", official_domain=args.domain, board_url=args.board,
                                 origin_ref=args.note)
        print(f"{target}: {dq.enqueue(store, candidate, 3 + (5 if args.domain or args.board else 0))} to the discovery queue")


def cmd_audit(ctx: Context, paths, args) -> None:
    from careeros.review import title_audit
    if args.what == "titles":
        rows = title_audit.sample(ctx.store, args.sample, seed=args.seed)
        print(f"{len(rows)} sampled title rejections (mark each: careeros audit mark JOB_ID include|correct)")
        for r in rows:
            print(f"  {r['job_id']:48} {r['reason']:18} {r['title'][:50]} | {r['employer'] or ''}")
        print(title_audit.summary(ctx.store))
    elif args.what == "mark":
        title_audit.mark(ctx.store, args.target, args.verdict)
        print(f"{args.target}: {args.verdict}")
    elif args.what == "match":
        title_audit.audit_match(ctx.store, args.target, args.verdict)
        print(f"{args.target}: {args.verdict}")


def cmd_export(ctx: Context, paths, args) -> None:
    """The analytics export for Power BI (docs/analytics_data_dictionary.md): validated CSV files and a manifest."""
    from careeros.analytics.export import ExportError, export
    from careeros.demo import sandbox
    from careeros.paths import ENGINE_ROOT
    demo = sandbox.enabled()
    root = Path(paths["root"]).resolve()
    out = Path(args.out).resolve() if args.out else root / "exports" / "analytics"
    if demo and not out.is_relative_to(root):
        sys.exit(f"In Demo Mode the export stays in the demo vault ({root}).")
    if not demo and out.is_relative_to(ENGINE_ROOT):
        sys.exit("This export holds your private job-search data, so it never goes inside the code repository. "
                 "Choose a folder outside it.")
    try:
        manifest = export(ctx.store, ctx.policy, out, source="demo" if demo else "private")
    except ExportError as exc:
        sys.exit(f"Export not written: {exc}")
    print(f"Analytics export ({manifest['source']} data, {manifest['schema_version']}) in {out}")
    for info in manifest["tables"].values():
        print(f"  {info['file']:28} {info['rows']:>6} rows")
    print(f"  all {len(manifest['checks'])} checks passed (see manifest.json)")
    print("KPIs: " + ", ".join(f"{k}={v}" for k, v in manifest["kpis"].items()))


def cmd_explain(ctx: Context, paths, args) -> None:
    """Why a job is where it is: source, dates, employer, verification, duplicates, eligibility, fit, career value,
    routing, status, reasons and actions, from the recorded decisions only."""
    from careeros.review.explain import _verdict, explain_job, render
    try:
        explanation = explain_job(ctx.store, ctx.policy, args.job_id)
    except ValueError as exc:
        sys.exit(str(exc))
    print(json.dumps(explanation, indent=1, ensure_ascii=False) if args.json
          else render(explanation, args.job_id, _verdict(ctx.store, ctx.policy, args.job_id)))   # the verdict first (OPP-03)


def cmd_dismiss(ctx: Context, paths, args) -> None:
    """Dismiss a job with a reason, or with --undo reinstate it: a later event, never an edit; the latest event decides
    (ADR 0007, amendment of 2026-11-01)."""
    from careeros.review.dismiss import current, dismiss, reinstate
    if getattr(args, "undo", False):
        if args.reason:
            sys.exit("Not changed: --undo takes no --reason (add a --note if you like)")
        was = current(ctx.store, args.job_id)
        try:
            reinstate(ctx.store, args.job_id, args.note)
        except ValueError as exc:
            sys.exit(f"Not changed: {exc}")
        print(f"{args.job_id}: reinstated (the dismissal of {was['at'][:10]}, {was['reason_code'].replace('_', ' ')}, is undone)")
        return
    if not args.reason:
        sys.exit("Not changed: --reason is required (or --undo to reinstate a dismissed job)")
    try:
        dismiss(ctx.store, args.job_id, args.reason, args.note)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"{args.job_id}: dismissed ({args.reason})")


def cmd_paste(ctx: Context, paths, args) -> None:
    """Grade a posting you opened yourself, from pasted text. The link is recorded, never fetched."""
    from careeros.discovery.manual_intake import intake
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif args.clipboard:
        import subprocess
        from careeros.demo.sandbox import refuse
        refuse("reading the clipboard")  # it may hold anything private; in Demo Mode paste from a file or stdin
        text = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, encoding="utf-8", timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    else:
        text = sys.stdin.read()
    try:
        result = intake(ctx, text, args.url, company=args.company, title=args.title, location=args.location)
    except ValueError as exc:
        sys.exit(f"Not added: {exc}")
    print(f"{result['job_id']}: {result['status']}" + (f"; manual source recorded ({result['manual_source']})" if result["manual_source"] else ""))
    counts = process(ctx)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))
    cmd_report(ctx, paths, args)


def cmd_shortlist(ctx: Context, paths, args) -> None:
    from careeros.review.shortlist import ageing, shortlist
    items = shortlist(ctx.store, ctx.policy)
    if not items:
        print("Shortlist is empty: label jobs Apply or Stretch (careeros review).")
    for i in items:
        print(f"- {i['job_id']} | {i['title']} | {i['employer']} | {i['status']} | tier {i['tier']} {i['state']}"
              f"{' | STRETCH' if i['stretch'] else ''}")   # the id first: label, package and assist all need it (OPP-15)
        print(f"    {ageing(i)}")   # first seen, label age, package (OPP-08 + OPP-17)
        print(f"    next: {i['next_action']}")
        if i.get("submission"):
            print(f"    {i['submission']}")
        for f in i["incompatible"] + i["unknowns"][:3]:
            print(f"    {f['status']}: {f['facet']}: {f['detail']}")


def cmd_draft(ctx: Context, paths, args) -> None:
    from careeros.review.shortlist import draft
    try:
        from careeros.review import approved_facts
        path = draft(ctx.store, ctx.profile, args.job_id, paths["drafts"], ctx.policy, approved_facts.load(paths["approved_facts"]))
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"Draft (from your approved profile only; nothing submitted): {path}")


def _package_inputs(paths) -> tuple[list, dict]:
    from careeros.review import answers, approved_facts
    return approved_facts.load(paths["approved_facts"]), answers.load(paths["answers"])


def cmd_package(ctx: Context, paths, args) -> None:
    """Application packages (ADR 0009): prepare one for a job you pursue (or the standing policy picks), show whether
    it's ready, approve or revoke a Stretch package, list them, or read a Greenhouse posting's form questions."""
    from careeros.review import packages
    root = paths["root"]
    try:
        if args.action == "list":   # the preparation queue: each package's state now
            from careeros.review import assist
            facts, library = _package_inputs(paths)
            rows = ctx.store.all("SELECT p.*, j.title, j.employer_name FROM packages p JOIN jobs j ON j.job_id = p.job_id "
                                 "WHERE p.version = (SELECT MAX(version) FROM packages q WHERE q.job_id = p.job_id) ORDER BY p.at DESC")
            for r in rows:
                ready = packages.readiness(ctx.store, ctx.policy, ctx.profile, r["job_id"], facts=facts, library=library,
                                           vault_root=root, questions=packages.load_questions(root, r["job_id"]))
                sessions = [s["state"] for s in assist.role_sessions(ctx.store, r["job_id"]) if s["state"] in ("open", "uncertain")]
                state = ready["state"] + (f", session {sessions[-1]}" if sessions else "")
                print(f"{r['package_id']}  v{r['version']}  {r['route']:<8}  {state:<24}  {r['job_id']}  {r['title']} at {r['employer_name']}")
            print(f"{len(rows)} package(s)")
            return
        if args.action == "gaps":
            from careeros.review import answers
            gaps = packages.common_gaps(ctx.store)
            for label, count, slot in gaps:   # a document or standard field is known by the form's key, not its words
                if slot in answers.DOCUMENT_KINDS:
                    how = f"document: answers.yaml covers it with documents.{slot}"
                elif slot:
                    how = f"standard field: answers.yaml covers it with field: {slot}"
                else:   # only a question that reads as a reusable kind is worth an answer in the file
                    kinds = answers.kinds_for(label)
                    how = 'kind: ' + ', '.join(kinds) if kinds else 'no reusable kind: answer it on each form'
                print(f"{count:>3}  {label}  ({how})")
            if gaps:
                print("Answer the ones with a kind once in vault\\profile\\answers.yaml (with that kind), and the standard "
                      "fields and documents with the entry shown, to cover every package that asks them. The rest stay "
                      "yours on each form.")
            _, library = _package_inputs(paths)
            held = [(a, answers.withheld(a, ctx.store.clock.now()[:10])) for a in library["answers"]]
            if any(why for _, why in held):   # say which words held an answer back, so a false alarm can be reworded
                print("\nAnswers in your file that are not used now:")
                for entry, why in held:
                    if why:
                        print(f"  {entry['id']}: {why}")
            return
        if not args.target and not (args.action == "prepare" and args.standing):
            sys.exit("Give the JOB_ID, PACKAGE_ID or APPROVAL_ID this action is for.")
        if args.action == "revoke":
            print(f"revoked: {packages.revoke(ctx.store, args.target, reason=args.reason, key=f'cli:{secrets.token_hex(8)}')}")
            return
        if args.action == "approve":
            approval = packages.approve(ctx.store, args.target, reason=args.reason, batch=args.batch,
                                        key=f"cli:{secrets.token_hex(8)}")
            print(f"approved {args.target}: {approval}. It covers this exact version only; a new version needs a new approval.")
            return
        if args.action == "questions":
            from careeros.discovery.http import HttpClient
            from careeros.review.adapters import fetch_greenhouse_questions
            found = fetch_greenhouse_questions(HttpClient(), args.target)
            path = packages.save_questions(root, args.target, found, "greenhouse job-board API", ctx.store.clock.now())
            print(f"{len(found)} question(s) read into {path}")
            return
        facts, library = _package_inputs(paths)
        if args.action == "prepare" and args.standing:
            picked = packages.standing_candidates(ctx.store, ctx.policy)
            for job_id in picked:
                out = packages.prepare(ctx.store, ctx.policy, ctx.profile, job_id, facts=facts, library=library, vault_root=root,
                                       questions=packages.load_questions(root, job_id), actor="standing_policy")
                print(("prepared" if out["new"] else "unchanged") + f" {job_id} v{out['version']} ({len(out['gaps'])} open item(s))")
            print(f"{len(picked)} job(s) picked by the standing preparation policy" + ("" if packages.settings(ctx.policy)["enabled"]
                                                                                      else " (it is off)"))
            return
        questions = packages.load_questions(root, args.target)
        if args.action == "prepare":
            out = packages.prepare(ctx.store, ctx.policy, ctx.profile, args.target, facts=facts, library=library,
                                   vault_root=root, questions=questions)
            print(("prepared" if out["new"] else "unchanged") + f" {out['package_id']} v{out['version']} ({out['route']}) in "
                  f"vault\\{out['folder']}")
            for gap in out["gaps"]:
                print(f"  open: {gap}")
        ready = packages.readiness(ctx.store, ctx.policy, ctx.profile, args.target, facts=facts, library=library,
                                   vault_root=root, questions=questions)
        print(f"{args.target}: {ready['state'].replace('_', ' ')}" + "".join(f"\n  {r}" for r in ready["reasons"]))
    except ValueError as exc:
        sys.exit(f"Not done: {exc}")


def cmd_assist(ctx: Context, paths, args) -> None:
    """Assistance sessions (ADR 0009): open one to apply (it reserves the role; nothing is submitted), close it with what
    happened, list sessions, or report the measured time and corrections by mode."""
    from careeros.review import assist
    try:
        if args.action == "report":
            print(json.dumps(assist.report(ctx.store), indent=1))
            return
        if args.action == "list":
            rows = ctx.store.all("SELECT * FROM assist_sessions" + (" WHERE job_id = ?" if args.target else "")
                                 + " ORDER BY opened_at DESC", (args.target,) if args.target else ())
            for r in rows:
                print(f"{r['session_id']}  {assist.session_state(ctx.store, r['session_id']):<13} {r['mode']:<8} {r['job_id']}  "
                      f"opened {r['opened_at'][:16]}")
            print(f"{len(rows)} session(s)")
            return
        if args.action == "open":
            from careeros.review import packages
            facts, library = _package_inputs(paths)
            out = assist.open_session(ctx.store, ctx.policy, ctx.profile, args.target, args.mode, facts=facts, library=library,
                                      vault_root=paths["root"], questions=packages.load_questions(paths["root"], args.target),
                                      key=f"cli:{secrets.token_hex(8)}")
            print(f"session {out['session_id']} open ({out['mode']}). Answers to copy: vault\\{out['answers']}")
            if out.get("handoff"):
                print(f"  handoff for the extension: vault\\{out['handoff']} (expires {out['expires_at'][:16]} UTC)")
            print("  Review the form, submit it yourself, then: careeros assist close "
                  f"{out['session_id']} --outcome submitted|not_submitted|uncertain")
            return
        if not args.target or not args.outcome:
            sys.exit("Give the SESSION_ID (or the JOB_ID) and --outcome submitted|not_submitted|uncertain.")
        session_id = assist.session_for(ctx.store, args.target)   # a JOB_ID names its role's one open session (OPP-77)
        out = assist.close_session(ctx.store, application_matrix(), ctx.policy, session_id, args.outcome,
                                   key=f"cli:{secrets.token_hex(8)}", evidence=args.evidence, minutes=args.minutes,
                                   corrections=args.corrections, custom_questions=args.custom_questions,
                                   login_steps=args.login_steps, notes=args.notes, reapply=args.reapply, confirm=args.confirm)
        print(f"closed {session_id}: {args.outcome}" + (f"; application {out['application_id']} recorded" if out["application_id"] else "")
              + ("; this reconciles an uncertain session" if out["reconciled"] else ""))
    except ApplicationHold as exc:
        sys.exit(f"Not closed yet: {exc}. Add --reapply or --confirm with your explanation, as for 'careeros applied'.")
    except ValueError as exc:
        sys.exit(f"Not done: {exc}")


def cmd_metrics(ctx: Context, paths, args) -> None:
    from careeros.review.discovery_metrics import log_review_minutes, metrics
    if args.log_review_minutes is not None:
        log_review_minutes(ctx.store, args.log_review_minutes)
        print(f"logged {args.log_review_minutes:g} review minutes")
    print(json.dumps(metrics(ctx.store, ctx.policy, args.days), indent=1, default=str))


def _daily_discovery(ctx: Context, paths) -> Counter:
    """Board discovery for new leads on the daily run, only when source_discovery.enabled is on (off by default)."""
    from careeros.discovery import discovery_queue as dq
    if not ctx.policy.get("source_discovery", "enabled", default=False):
        return Counter()
    try:
        dq.build_backfill(ctx, int(ctx.policy.get("source_discovery", "lookback_days", default=90)))
        dq.load_curated(ctx, paths["curated"])
        stats = dq.run(ctx)
        print("Board discovery: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        return Counter({f"discovery_{k}": v for k, v in stats.items() if isinstance(v, int)})
    except Exception as exc:  # noqa: BLE001 - discovery problems never stop the daily run
        ctx.store.writer("discovery").exception("integration", "medium", f"board discovery failed: {type(exc).__name__}: {exc}",
                                                error_code="discovery_failed")
        return Counter(discovery_errors=1)


def _board_maintenance(ctx: Context, started: str) -> Counter:
    """Board verification upkeep on the daily run: legacy boards registered, expired approvals, re-verification and
    new-board verification (only when official-site checks are on), validation scans, career fit. Never fatal."""
    from careeros.discovery import board_verifier
    try:
        stats = board_verifier.maintain(ctx, run_started=started)
        shown = {k: v for k, v in stats.items() if v}
        if shown:
            print("Board verification: " + " ".join(f"{k}={v}" for k, v in sorted(shown.items())))
        return Counter({f"boards_{k}": v for k, v in stats.items() if isinstance(v, int)})
    except Exception as exc:  # noqa: BLE001 - verification problems never stop the daily run
        log.exception("board maintenance failed")
        ctx.store.writer("discovery").exception("integration", "medium", f"board verification failed: {type(exc).__name__}: {exc}",
                                                error_code="board_verification_failed")
        return Counter(board_verification_errors=1)


def cmd_eval(ctx: Context, paths, args) -> None:
    print(json.dumps({"primary": agreement(ctx.store), "night": agreement(ctx.store, "night"),
                      "primary_three_way": three_way(ctx.store, ctx.policy, ctx.profile)}, indent=2))


def cmd_certifications(ctx: Context, paths, args) -> None:
    """Certification demand across your target jobs and your status for each (2026-10-15). Read only."""
    from careeros.review.certification_demand import demand
    print(json.dumps(demand(ctx.store, ctx.policy, ctx.profile), indent=1, default=str))


def cmd_status(ctx: Context, paths, args) -> None:
    store = ctx.store
    states = store.all("SELECT current_state, COUNT(*) c FROM jobs GROUP BY current_state ORDER BY c DESC")
    tiers = store.all("""SELECT s.tier, COUNT(*) c FROM jobs j JOIN scores s ON s.score_id =
                         (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                         WHERE j.current_state = 'scored' GROUP BY s.tier""")
    runs = store.one("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    held = hold(paths["lock"])   # OPP-05: every daily run waits until you act; only read here, never removed
    if held:
        print(f"HELD since {held[0]}: " + ("the writer lock's process is gone (a stopped run?). Once no CareerOS run is "
                                           "active, run: careeros unlock" if held[1] == "stale" else
                                           "the writer lock is unreadable; inspect it and remove it by hand"))
    last_run, run_warning = run_health(store)   # OPP-06
    if run_warning:
        print(f"!! {run_warning}")
    print("profile:", "approved" if ctx.profile.approved else "DRAFT (scores provisional)")
    from careeros.qualify import certifications as certification_mod
    for warning in certification_mod.profile_warnings(ctx.profile, ctx.policy):
        print("  certification:", warning)
    for item in certification_mod.evidence_review(ctx.profile, ctx.policy):
        print(f"  evidence to review: {item['item']} rests on {item['credential']} ({item['status'].replace('_', ' ')})")
    print("AI requirement reading:", _ai_status(ctx))
    print("AI calls today:", store.one("SELECT COUNT(*) c FROM events WHERE event_type='ai_extraction_call' "
                                       "AND substr(occurred_at,1,10)=?", (store.clock.now()[:10],))["c"])
    print("jobs:", ", ".join(f"{r['current_state']}={r['c']}" for r in states) or "none")
    print("tiers:", ", ".join(f"{r['tier']}={r['c']}" for r in tiers) or "none")
    backlog = label_backlog(store)
    print("label backlog:", backlog["text"] if backlog else "none")
    print("open exceptions:", store.one("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved'")["c"])
    from careeros.qualify import night
    if night.enabled(ctx.policy):
        rows = store.all("""SELECT a.mode, a.tier, COUNT(*) c FROM night_results a WHERE a.decision = 'eligible' AND a.result_id =
                            (SELECT result_id FROM night_results WHERE job_id = a.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                            GROUP BY a.mode, a.tier""")
        print("NIGHT profile: on;", ", ".join(f"{r['mode']}/{r['tier']}={r['c']}" for r in rows) or "no eligible jobs yet")
    else:
        print("NIGHT profile: off (turned on in vault overrides once its pay settings are approved)")
    if runs:
        print(f"last run: {runs['started_at']} {runs['status']}")
    print(last_run)


def cmd_verify(ctx: Context, paths, args) -> None:
    broken = ctx.store.verify_event_chain()
    bad_jobs = {}
    for row in ctx.store.all("SELECT job_id FROM jobs"):
        errors = ctx.store.verify_job_consistency(ctx.matrix, row["job_id"])
        if errors:
            bad_jobs[row["job_id"]] = sorted(errors)
    app_matrix, bad_apps = application_matrix(), {}
    for row in ctx.store.all("SELECT application_id FROM applications"):
        errors = ctx.store.verify_job_consistency(app_matrix, row["application_id"], entity="application")
        if errors:
            bad_apps[row["application_id"]] = sorted(errors)
    print(json.dumps({"event_chain_breaks": broken, "inconsistent_jobs": bad_jobs, "inconsistent_applications": bad_apps}, indent=2))
    if broken or bad_jobs or bad_apps:
        sys.exit(2)


WRITES = {"init", "employers", "discover", "add-url", "import", "process", "run", "label", "applied", "app-status", "backup", "gmail", "inbox",
          "sources", "audit", "dismiss", "paste", "draft", "metrics",
          "seen-index", "release", "package", "assist",
          "export"}   # the export only reads, but takes the lock so it never reads a run half-way through


def _write_utf8() -> None:
    """Piped or redirected, Python on Windows writes the ANSI code page, which can't spell every label a form or a
    posting uses: a non-breaking hyphen stopped `package gaps` part-way (live data, 2026-10-26). Such a stream writes
    UTF-8 instead. A console, a stream already in UTF-8 (PYTHONIOENCODING) and a missing one are left as they are."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, io.TextIOWrapper) and codecs.lookup(stream.encoding).name != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv=None) -> None:
    _write_utf8()
    parser = argparse.ArgumentParser(prog="careeros", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("run")
    p.add_argument("--output-file", help="append all output to this file (used by the scheduled task)")
    p = sub.add_parser("discover")
    p.add_argument("--employer")
    p = sub.add_parser("add-url")
    p.add_argument("url")
    p.add_argument("--text-file")
    p.add_argument("--employer")
    p.add_argument("--parent")
    p.add_argument("--title")
    p.add_argument("--location")
    sub.add_parser("process")
    sub.add_parser("import", help="import postings you saved into vault\\inbox")
    sub.add_parser("report")
    p = sub.add_parser("review")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("label")
    p.add_argument("job_id")
    p.add_argument("label", choices=["apply", "stretch", "skip"])
    p.add_argument("--reason")
    p.add_argument("--night", action="store_true", help="a NIGHT-profile label (measured separately from PRIMARY)")
    p = sub.add_parser("applied", help="record that you applied to a job")
    p.add_argument("job_id")
    p.add_argument("--channel", choices=["direct", "agency", "referral", "other"], required=True)
    p.add_argument("--agency", help="agency name, e.g. 'Harbor Staffing' (required with --channel agency)")
    p.add_argument("--notes")
    p.add_argument("--reapply", metavar="REASON", help="you applied to this role before (rejected, withdrawn...): why again")
    p.add_argument("--evidence", choices=["owner_attested", "confirmation_observed"], default="owner_attested",
                   help="confirmation_observed when you saw the employer's confirmation (default: your statement)")
    p.add_argument("--confirm", metavar="REASON", help="record it despite another application to the role, a possible duplicate, "
                                                       "an unfinished duplicate check, a submission hold or an unidentified "
                                                       "employer: your explanation (never releases a hold)")
    p = sub.add_parser("release", help="submission holds: show a job's, release one hold for one posting and channel, or revoke")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--hold", help="the hold to release (its id, as shown by 'careeros release JOB')")
    p.add_argument("--channel", choices=["direct", "agency", "referral", "other"])
    p.add_argument("--agency", help="with --channel agency: the agency")
    p.add_argument("--reason", help="why you release it (required with --hold or --revoke)")
    p.add_argument("--revoke", metavar="RELEASE_ID", help="withdraw a release")
    p = sub.add_parser("app-status", help="update an application's status; void (recorded by mistake) needs --reason")
    p.add_argument("application_id")
    p.add_argument("state")
    p.add_argument("--notes")
    p.add_argument("--reason", help="why: required for void, an application recorded by mistake (it stays in the history)")
    sub.add_parser("applications")
    p = sub.add_parser("funnel", help="recommended -> approved -> applied -> ... -> hired, by a dimension")
    p.add_argument("--by", choices=["source", "family", "profile", "tier", "channel", "domain", "night_mode", "employment_type",
                                    "employer"], default="source")
    p = sub.add_parser("eval", help="agreement with your labels; --scoring compares production, the baseline and the shadow rubric")
    p.add_argument("--scoring", action="store_true",
                   help="shadow scoring evaluation on a read-only snapshot (never writes the database)")
    p.add_argument("--snapshot", help="with --scoring: evaluate this database copy instead of taking a new snapshot")
    p.add_argument("--draw", action="store_true", help="with --scoring: draw a calibration list (statuses hidden; predictions "
                                                       "frozen; needs your adoption criteria written down first)")
    p.add_argument("--seed", help="with --draw: the random seed (recorded in the list; default: a new random one)")
    p = sub.add_parser("employers")
    p.add_argument("action", choices=["list", "add", "verify"])
    p.add_argument("--name")
    p.add_argument("--ats", choices=["greenhouse", "lever", "ashby", "workable"])
    p.add_argument("--token")
    p.add_argument("--parent")
    p.add_argument("--industry")
    sub.add_parser("backup")
    sub.add_parser("inbox", help="read job recommendations posted as issues in the private GitHub inbox")
    sub.add_parser("seen-index", help="rebuild the seen-jobs index and Gmail intake status and publish them to the inbox repo")
    p = sub.add_parser("sources", help="employer boards: discovery (backfill, plan, run, suggest), verification (check, "
                                       "validate, explain, approve, reject, activate, reviews, resolve, domain) and health")
    p.add_argument("action", choices=["backfill", "plan", "run", "list", "verify", "approve", "reject", "activate", "alias", "suggest",
                                      "health", "check", "validate", "explain", "reviews", "resolve", "domain", "providers"])
    p.add_argument("target", nargs="?")
    p.add_argument("value", nargs="?", help="with domain: the employer's official website, e.g. example.com")
    p.add_argument("--days", type=int, help="backfill lookback (default 90); with approve: days until the approval expires")
    p.add_argument("--run", action="store_true", help="with backfill: run discovery after queueing (default: preview only)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--domain")
    p.add_argument("--board")
    p.add_argument("--note")
    p.add_argument("--reason", help="with approve: why you vouch for this board (required)")
    p.add_argument("--of", help="with alias: the employer it belongs to")
    p.add_argument("--relation", default="alias", choices=["alias", "parent", "subsidiary", "former", "legal"])
    p = sub.add_parser("audit", help="sample title-screen rejections, or record an audit verdict")
    p.add_argument("what", choices=["titles", "mark", "match"])
    p.add_argument("target", nargs="?")
    p.add_argument("verdict", nargs="?", choices=["include", "correct", "wrong"])
    p.add_argument("--sample", type=int, default=40)
    p.add_argument("--seed", type=int)
    p = sub.add_parser("demo", help="Demo Mode: a synthetic, offline, credential-free demonstration (init, run [--all], "
                                    "status, reset, tour)")
    p.add_argument("action", choices=["init", "run", "status", "reset", "tour"])
    p.add_argument("--all", action="store_true", help="with run: every remaining simulated day")
    p = sub.add_parser("export", help="analytics export for Power BI: star-schema CSV files, validated (docs/analytics_data_dictionary.md)")
    p.add_argument("--out", help="folder to write (default: exports\\analytics in the vault)")
    p = sub.add_parser("explain", help="why a job is where it is (all recorded decisions, in plain text or --json)")
    p.add_argument("job_id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("dismiss", help="dismiss a job, with the reason; --undo reinstates a dismissed job")
    p.add_argument("job_id")
    p.add_argument("--reason", help="why (required unless --undo): pay, location, remote, schedule, seniority, skills, ...")
    p.add_argument("--note")
    p.add_argument("--undo", action="store_true", help="reinstate it: a later event undoes the dismissal in force")
    p = sub.add_parser("paste", help="grade a posting you opened yourself (text + link; the link is never fetched)")
    p.add_argument("--url")
    p.add_argument("--file")
    p.add_argument("--clipboard", action="store_true")
    p.add_argument("--company")
    p.add_argument("--title")
    p.add_argument("--location")
    sub.add_parser("shortlist", help="shortlisted openings with evidence, open questions and the next action")
    p = sub.add_parser("draft", help="an application brief from your approved profile (Apply/Stretch jobs only)")
    p.add_argument("job_id")
    p = sub.add_parser("package", help="application packages: prepare, show, approve/revoke (Stretch), list, questions")
    p.add_argument("action", choices=["prepare", "show", "approve", "revoke", "list", "questions", "gaps"])
    p.add_argument("--standing", action="store_true", help="with prepare: every job the standing preparation policy picks")
    p.add_argument("target", nargs="?", help="JOB_ID (prepare, show, questions), PACKAGE_ID (approve) or APPROVAL_ID (revoke)")
    p.add_argument("--reason", help="why (required to approve or revoke)")
    p.add_argument("--batch", help="with approve: a name for packages approved together")
    p = sub.add_parser("assist", help="assistance sessions: open one to apply, close it with what happened, list, report")
    p.add_argument("action", choices=["open", "close", "list", "report"])
    p.add_argument("target", nargs="?", help="JOB_ID (open, list), or SESSION_ID or JOB_ID (close: its role's one open session)")
    p.add_argument("--mode", choices=["manual", "native", "assisted"], default="manual")
    p.add_argument("--outcome", choices=["submitted", "not_submitted", "uncertain"])
    p.add_argument("--evidence", choices=["owner_attested", "confirmation_observed"], help="with --outcome submitted")
    p.add_argument("--minutes", type=float, help="how long applying took (P3 measurement)")
    p.add_argument("--corrections", type=int, help="fields you had to correct")
    p.add_argument("--custom-questions", type=int, help="questions the package didn't answer")
    p.add_argument("--login-steps", type=int, help="sign-in or verification steps")
    p.add_argument("--notes")
    p.add_argument("--reapply", metavar="REASON")
    p.add_argument("--confirm", metavar="REASON")
    p = sub.add_parser("metrics", help="discovery and opportunity metrics (pilot instrumentation)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--log-review-minutes", type=float)
    p = sub.add_parser("gmail", help="read job-alert emails from your Gmail (read-only)")
    p.add_argument("action", choices=["connect", "status", "fetch", "disconnect"])
    p.add_argument("--client-file", help="OAuth client JSON from Google Cloud (default: vault/secrets/gmail_oauth_client.json)")
    p.add_argument("--days", type=int, help="with fetch: look back this many days instead of the default")
    sub.add_parser("certifications", help="which certifications your target jobs ask for, and your status for each")
    sub.add_parser("status")
    sub.add_parser("verify")
    sub.add_parser("unlock")
    args = parser.parse_args(argv)

    from careeros.demo import sandbox
    try:
        _dispatch(args)
    except sandbox.DemoModeViolation as exc:  # a refusal, not a crash: one line, no traceback (Functional MVP gap G6)
        print(f"Refused: {exc}", file=sys.stderr)
        sys.exit(2)


def _notifications_on(paths) -> bool:
    """operations.notifications, for a notice given before the run has its context. A policy that cannot be read
    counts as the default (on): the notice is about a run that cannot start."""
    try:
        return bool(load_policy(CONFIG_DIR / "policy" / "defaults.yaml", paths["overrides"]).get("operations", "notifications", default=True))
    except Exception:  # noqa: BLE001 - never hides the notice it gates
        return True


def _held_run(paths) -> None:
    """A stale writer lock holds every later daily run, and the scheduled run has no window (OPP-05, 2026-11-01): one
    killed run must not silently stop the next ones. Logged, and a notification; the lock itself is left as it is."""
    try:
        since = (hold(paths["lock"]) or ("an unknown time",))[0]
    except OSError:
        since = "an unknown time"
    log.warning("daily run held: the writer lock left since %s belongs to a process that is gone; run careeros unlock", since)
    if _notifications_on(paths):
        from careeros.ops.notify import problem
        problem("CareerOS: daily run held", "Run: careeros unlock")


def _dispatch(args) -> None:
    from careeros.demo import sandbox
    if args.command == "demo":  # Demo Mode: synthetic world, throwaway vault, no credentials, no real network
        sandbox.enable()
        from careeros.demo import runner
        runner.main(args)
        return
    demo_clock = None
    if sandbox.enabled():  # CAREEROS_DEMO_MODE=true: every command works on the demo vault and the synthetic world
        from careeros.demo import runner
        paths = runner.paths()
        runner._require(paths)
        runner.install_world(paths)
        demo_clock = runner.demo_clock(paths)
        print(f"[Demo Mode: synthetic data in {paths['root']}]")
    else:
        paths = vault_paths()
    if args.command == "unlock":
        removed = force_unlock(paths["lock"])
        print("removed stale lock" if removed else "no lock present")
        if removed and paths["db"].exists():  # no writer is active, so any run still marked running was interrupted
            store = open_store(paths["db"], Clock())
            count = store.writer("runner").update("runs", {"status": "failed", "ended_at": store.clock.now()}, "status = 'running'")
            print(f"marked {count} interrupted run(s) as failed")
        return
    if getattr(args, "output_file", None):  # scheduled runs: no console window, so output goes to a log file
        from careeros.ops import daily_log
        daily_log.redirect(args.output_file)   # already done when `python -m careeros` started the run (OPP-52)
        print(f"=== {Clock().now()} careeros {args.command} ===")
    if args.command == "eval" and args.scoring:  # read-only: a snapshot copy, no lock, no Store, no log file
        from careeros.review import scoring_eval
        scoring_eval.main(paths, args, clock=demo_clock)
        return
    _setup_logging(paths)
    paths["db"].parent.mkdir(parents=True, exist_ok=True)
    handlers = {"init": cmd_init, "run": cmd_run, "discover": cmd_discover, "add-url": cmd_add_url, "process": cmd_process,
                "import": cmd_import,
                "report": cmd_report, "review": cmd_review, "label": cmd_label, "eval": cmd_eval, "employers": cmd_employers,
                "applied": cmd_applied, "app-status": cmd_app_status, "applications": cmd_applications, "funnel": cmd_funnel, "backup": cmd_backup, "gmail": cmd_gmail, "inbox": cmd_inbox,
                "seen-index": cmd_seen_index, "sources": cmd_sources, "audit": cmd_audit, "dismiss": cmd_dismiss, "explain": cmd_explain,
                "paste": cmd_paste, "shortlist": cmd_shortlist, "draft": cmd_draft, "metrics": cmd_metrics,
                "status": cmd_status, "verify": cmd_verify, "export": cmd_export,
                "certifications": cmd_certifications, "release": cmd_release, "package": cmd_package, "assist": cmd_assist}
    read_only = ((args.command == "employers" and args.action == "list") or (args.command == "gmail" and args.action != "fetch")
                 or (args.command == "release" and not (args.hold or args.revoke))
                 or (args.command == "package" and args.action in ("show", "list", "gaps"))
                 or (args.command == "assist" and args.action in ("list", "report")))
    lock = SingleWriterLock(paths["lock"]) if args.command in WRITES and not read_only else None
    try:
        if lock:
            lock.acquire()
        ctx = _context(paths, with_ai=args.command in ("run", "process", "add-url", "import"), clock=demo_clock)
        log.info("command %s policy %s ai %s", args.command, ctx.policy.fingerprint[:12], ctx.ai is not None)
        handlers[args.command](ctx, paths, args)
    except LockError as exc:
        print(f"CareerOS is busy: {exc}")
        if args.command == "run" and str(exc).startswith("stale_lock_held"):
            _held_run(paths)
        sys.exit(1)
    except sandbox.DemoModeViolation:
        raise  # main() reports it in one line
    except Exception:
        import traceback
        traceback.print_exc()  # with --output-file this lands in the log instead of vanishing
        if args.command == "run":  # the scheduled run has no window: make a failure visible
            try:
                from careeros.ops.notify import toast
                toast("CareerOS: daily run failed", "Details are in vault\\logs\\daily-run.log")
            except Exception:  # noqa: BLE001 - never mask the original error
                pass
        raise
    finally:
        if lock and lock.held:
            lock.release()


if __name__ == "__main__":
    main()
