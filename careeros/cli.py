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
    careeros app-status APPLICATION --followed-up [--notes ...]   record a follow-up you sent (the status stays)
    careeros release JOB [--hold ID --channel C [--agency NAME] --reason ...]   submission holds: show, or release one
    careeros release --revoke RELEASE_ID --reason ...      withdraw a release
    careeros dismiss JOB --reason R [--note ...] | careeros dismiss JOB --undo [--note ...]   dismiss, or reinstate
    careeros leads [list|confirm LEAD --job J|reject LEAD --job J|unlink LEAD --reason ...]   your leads and likely matches
    careeros applications             list your applications and their status
    careeros funnel [--by source|family|profile|tier|channel|domain]   hiring outcomes and conversion rates
    careeros eval                     agreement between your labels and the system (Phase 1 exit check; three-way)
    careeros labels disagreements     each label or dismissal the system disagrees with now, and the rule that fired
    careeros certifications           which certifications your target jobs ask for, and your status for each
    careeros employers list|add|verify
    careeros gmail connect|status|fetch|disconnect   read job-alert emails from your Gmail (see GMAIL_SETUP.md)
    careeros inbox                    read job recommendations from the private GitHub inbox now (also daily)
    careeros backup [--offsite]       copy the database, profile and policy overrides to vault\\backups (also daily);
                                      --offsite also writes the encrypted offsite archive now (OPP-26)
    careeros offsite confirm          open the newest offsite archive with the passphrase as you keep it off this PC
    careeros status | verify | unlock
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import io
import json
import logging
import re
import secrets
import sys
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path

import httpx

from careeros.core.clock import Clock
from careeros.core.fingerprint import text_fingerprint
from careeros.core.locks import LockError, SingleWriterLock, force_unlock, hold
from careeros.core.policy import load_policy, load_yaml
from careeros.core.redact import RedactingFilter
from careeros.core.transitions import load_matrix
from careeros.db.store import MigrationPending, SchemaTooNew, TransactionLost, open_store
from careeros.discovery.boards import fetch_board, fetch_single, parse_job_url
from careeros.discovery.http import FetchError, HttpClient, RequestBudget
from careeros.normalize.posting import RawPosting, posting_link
from careeros.normalize.sanitize import clean_text, html_to_text
from careeros.paths import CONFIG_DIR, ENGINE_ROOT, vault_paths
from careeros.pipeline import BatchAborted, Context, hold_aborted, ingest, process
from careeros.qualify.profile import load_profile
from careeros.review.applications import (CONFIRM_CODES, ApplicationConflict, ApplicationHold, SessionOpen, application_matrix,
                                          applications, record_application, record_follow_up, update_application)
from careeros.review.labels import agreement, disagreements, label_backlog, record_label, render_disagreements, three_way
from careeros.review.report import build_report, last_review_at, run_health

log = logging.getLogger("careeros")


def _setup_logging(paths) -> None:
    paths["logs"].mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(paths["logs"] / "careeros.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _context(paths, with_ai: bool = False, clock=None, migrate: bool | None = True) -> Context:
    policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", paths["overrides"])
    profile = load_profile(paths["profile"])
    store = open_store(paths["db"], clock or Clock(), policy.fingerprint, migrate=migrate)
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
    from careeros.normalize.sanitize import canonical
    name, parent = canonical(name), (canonical(parent) if parent else parent)   # the registry write (point 6)
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


def _withdraw_lost_handoffs(ctx: Context, paths) -> int:
    """After a command that writes: every handoff file in the vault is removed (assist.withdraw_handoffs). Browser Fill is
    paused in this release (the owner's D191), so no handoff may stay loadable, one an earlier version wrote included."""
    if not paths or not paths.get("root") or not hasattr(ctx, "policy"):
        return 0
    from careeros.review.assist import withdraw_handoffs
    withdrawn = withdraw_handoffs(ctx.store, ctx.policy, getattr(ctx, "profile", None), paths["root"])
    if withdrawn:
        print(f"  handoffs_withdrawn={withdrawn}")
    return withdrawn


def _command_start(ctx: Context, paths) -> None:
    """At the start of every command that writes (the scheduled run included): every handoff file in the vault is
    removed (browser Fill is paused in this release, the owner's D191), and a hold the database refused, kept beside it,
    is recorded (the red team's hr1-routes-0)."""
    _withdraw_lost_handoffs(ctx, paths)
    from careeros.review.packages import pending_file_problem, record_pending_holds
    if record_pending_holds(ctx.store):
        print("  a posting read earlier could not be stored: its label waits until a later read is stored")
    problem = pending_file_problem(ctx.store)
    if problem:   # every Apply and Stretch label waits on it (BATCH_5 A)
        print(f"  ! {problem}. Every Apply and Stretch label waits until then")
    from careeros import upgrade
    if not upgrade.done(ctx.store):   # rows an older engine stored, handled once (the D190 design, section 7)
        done = upgrade.run(ctx)
        if done:
            print("  upgrade: " + " ".join(f"{k}={v}" for k, v in sorted(done.items())))
        if done["holds_reaching_every_job"]:   # Z8
            print(f"  ! upgrade: {done['holds_reaching_every_job']} failed read(s) an older engine recorded name no "
                  "posting (a posting you added whose job was never created): each is on every card and holds every "
                  "Apply and Stretch label until a label given from such a card acknowledges it (careeros status lists them)")
        if done[upgrade.CLOCK_BEHIND]:   # BATCH_5 G1: reported, never a refused command
            print(f"  ! upgrade: {done[upgrade.CLOCK_BEHIND]} job(s) not rebuilt: this computer's clock "
                  f"({ctx.store.clock.now()[:16]} UTC) is before their latest state change. Each is rebuilt by the first "
                  "command after the clock passes it, and offers no preview until then (check the clock)")


def _sweep_unlocked(paths, clock=None) -> int:
    """A command that could not take the writer lock (another command holds it, a lock left behind, an empty or
    unreadable lock) still removes every handoff file in the vault, with no database read (browser Fill is paused in this
    release, the owner's D191: no handoff is legitimate). Returns how many files it removed."""
    from careeros.review.assist import remove_all_handoffs
    root = paths.get("root") if paths else None
    return remove_all_handoffs(Path(root)) if root else 0


def _command_end(ctx: Context, paths) -> None:
    """In a finally at the end of every command that writes: the same handoff sweep, so a command stopped part-way (a
    changed private-settings file, an error) never leaves a handoff the label or posting no longer stands for (B.3)."""
    try:
        _withdraw_lost_handoffs(ctx, paths)
    except Exception:   # noqa: BLE001 - never masks the command's own outcome; the next command's start sweeps again
        log.exception("handoff sweep at the end of the command failed")


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
    from careeros.discovery import lead_resolution
    resolver = lead_resolution.Resolver(ctx, batch_started=now)   # after each scan, the employer's open leads are matched again
    try:
        from careeros.discovery import sources
        for r in rows:
            _halt_if_changed(ctx.store, "board scan stopped; the rest is scanned on the next run")   # D153 R2
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
                unlisted = list(getattr(postings, "unlisted", ()))   # open at their own link, never listed
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
            _halt_if_changed(ctx.store, "board scan stopped; the rest is scanned on the next run")   # nothing of it kept
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
            # a suspicious scan never closes jobs; an unlisted posting is never admitted from it, nor counted absent
            result = ingest(ctx, dict(r), postings + unlisted, full_board=outcome == "complete")
            _halt_if_changed(ctx.store, "board scan stopped; the rest is scanned on the next run")   # D153 R2
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
            counts.update(_rematch_leads(ctx, r, resolver, complete=outcome == "complete"))
        _halt_if_changed(ctx.store, "board scan stopped after its last board; the next run does the rest")   # D153 R2
        counts.update(_settle_scan_failures(ctx, failures, attempts))
        counts["board_scans_attempted"] = sum(attempts.values())
        if ctx.policy.get("scanning", "adaptive", default=False):
            counts.update(_refresh_shortlisted(ctx, client, now))
    finally:
        client.close()
    withdrawn = _withdraw_lost_handoffs(ctx, paths)
    if withdrawn:
        counts["handoffs_withdrawn"] += withdrawn
    return counts


def _rematch_leads(ctx: Context, employer, resolver, *, complete: bool) -> Counter:
    """The employer's open leads, matched again after a scan of its board (ADR 0003, amendment of 2026-11-02). A failure
    is recorded and never stops the scan of the other boards."""
    from careeros.discovery import lead_resolution
    try:
        found = lead_resolution.after_scan(ctx, employer, complete=complete, resolver=resolver)
    except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
        ctx.store.writer("discovery").exception("data_quality", "medium", f"lead matching after a scan failed: {type(exc).__name__}: {exc}",
                                                entity_type="employer", entity_id=employer["employer_id"], error_code="lead_rematch_failed")
        return Counter(lead_rematch_errors=1)
    if found:
        print("    leads: " + " ".join(f"{k.removeprefix('leads_')}={v}" for k, v in sorted(found.items())))
    return found


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
        _halt_if_changed(ctx.store, "shortlist refresh stopped; the next run does it")   # before every probe (D153 R2)
        job = ctx.store.one("SELECT j.*, e.last_complete_scan_at FROM jobs j LEFT JOIN employers e ON e.employer_id = j.employer_id "
                            "WHERE j.job_id = ?", (job_id,))
        if job is None:
            continue
        endpoint = _posting_endpoint(ctx.store, job)
        if endpoint is None or (job["last_complete_scan_at"] or "")[:10] == now[:10]:
            continue
        try:
            posting = fetch_single(client, *endpoint)
            # what it read is written through the one ingest write: a re-identification, a retitle or a new location
            # reaches the job, its label and its handoffs (the red team's gr1-faults-1)
            employer = _employer_row(ctx.store, endpoint[0], endpoint[1]) or ctx.store.one(
                "SELECT * FROM employers WHERE employer_id = ?", (job["employer_id"],))
            result = ingest(ctx, dict(employer) if employer else None, [posting], source="manual_url", full_board=False)
            counts["shortlist_still_open"] += 1
            counts.update({f"shortlist_{k}": v for k, v in result.items() if k in ("changed", "reidentified", "errors")})
        except FetchError as exc:
            if exc.code == "not_found" and job["current_state"] not in ("closed", "duplicate"):
                with ctx.store.tx():
                    ctx.store.writer("discovery").update("jobs", {"closed_at": now}, "job_id = ?", (job_id,))
                    ctx.store.writer("discovery").transition(ctx.matrix, job_id, "closed", reason_code="posting_removed")
                counts["shortlist_closed"] += 1
    return counts


def _posting_endpoint(store, job) -> tuple[str, str, str] | None:
    """(ats, board token, posting id) to read a shortlisted job's posting by, from its current identity
    (board_discovery.job_identity): the board its job of record now belongs to (a renamed board: the red team's
    hr1-identity-1), else the posting's own link (a job you pasted, saved or added first: hr1-boards-0). Never a token
    taken from the job's id, which a renamed board leaves stale. None for a posting no single-posting endpoint reads."""
    from careeros.discovery.board_discovery import job_identity
    identity = job_identity(job)
    if identity is None or identity[0] not in ("greenhouse", "lever"):
        return None
    kind, posting = identity
    employer = store.one("SELECT ats, board_token FROM employers WHERE employer_id = ?", (job["employer_id"],)) \
        if job["employer_id"] else None
    if employer is not None and employer["ats"] == kind and employer["board_token"]:
        return kind, employer["board_token"], posting
    for url in (job["url"], job["apply_url"]):
        parsed = parse_job_url(url) if url else None
        if parsed and parsed[0] == kind and parsed[2] == posting:
            return parsed
    return None


def _ingest_board_link(ctx: Context, client: HttpClient, url: str, employer_name: str | None = None, *,
                       fetched: dict | None = None, ahead: dict | None = None, exclude: frozenset = frozenset(),
                       created: set | None = None):
    """Fetch one posting from a public job board link and ingest it. Returns (posting, counts, job_id). In an alert
    email's batch the posting was fetched ahead (`fetched`: never inside its transaction, Unfetched when it was not)
    and its write-ahead hold is carried by the line's position (`ahead`, `exclude`, `created`: the D190 design,
    section 7, Z1)."""
    ats, token, external_id = parse_job_url(url)
    if fetched is None:
        posting = fetch_single(client, ats, token, external_id)  # first: a link that does not resolve adds no employer
    else:
        got = fetched.get(url)
        if got is None:
            raise Unfetched(url)
        if isinstance(got, FetchError):
            raise got
        posting = got
    employer = _employer_row(ctx.store, ats, token)
    if employer is None:  # self-growing employer list, named after the company when the link came with one
        from careeros.discovery.saved import parent_for
        employer = _add_employer(ctx, employer_name or token, ats, token,
                                 parent=parent_for(employer_name, ctx.policy) if employer_name else None)
    _set_employer(ctx, employer, "verified")
    placed: dict = {}
    result = ingest(ctx, dict(employer), [posting], source="manual_url", full_board=False, placed=placed, ahead=ahead,
                    exclude=exclude, batch_created=created)
    # the job written: the posting's job of record, which may be a job you pasted or saved first (batch 3, group A)
    return posting, result, placed.get(f"{posting.ats}:{posting.board_token}:{posting.external_id}")


def cmd_add_url(ctx: Context, paths, args) -> str | None:
    if parse_job_url(args.url) and not args.text_file:
        client = HttpClient()
        try:
            posting, result, job_id = _ingest_board_link(ctx, client, args.url, args.employer)
        except FetchError as exc:   # one line, never a traceback (the hand check's HC2)
            sys.exit(f"Not added: {args.url} could not be read: {exc.code}")
        finally:
            client.close()
            _withdraw_lost_handoffs(ctx, paths)   # this branch sweeps too, however it ends (the red team's gr1-saved-3)
        print(f"{posting.title} — " + " ".join(f"{k}={v}" for k, v in result.items()))
        if args.location:
            where = ctx.store.one("SELECT country FROM jobs WHERE job_id = ?", (job_id,)) if job_id else None
            if where is not None and where["country"] != "US":   # its field leaves it unknown or abroad (part 4)
                print(f"  --location not used: to settle this board posting's location, run careeros location {job_id} "
                      f"\"{args.location}\"")
            else:
                print("  --location not used: a job board's posting keeps its board's location")
        if not job_id or result.get("errors"):   # nothing stored: no lead linked (the red team before Codex Pass 11)
            sys.exit("Not added: the posting could not be stored (recorded as an open exception on the review page)")
        _link_leads(ctx, job_id, "added")
        return job_id
    from careeros.discovery.manual_intake import given
    # a blank argument is not given, as its request reads it (request_of: "" and None are one value), so one request is
    # never stored two ways: "   " for --location or --parent is no location and no parent (the owner's D201). Its
    # ingest cuts none of them: RawPosting keeps each text field whole in the canonical form (normalize/posting.py),
    # normalized_fields stores the title and location so, and the employer entry's name and parent are canonical
    # (pipeline.employer_entry): the request's canonical arguments are what is stored
    employer, title, location, parent = given(args.employer), given(args.title), given(args.location), given(args.parent)
    if not (args.text_file and employer and title):
        sys.exit("For links that are not Greenhouse/Lever/Ashby/Workable, save the posting text to a file and pass "
                 "--text-file FILE --employer NAME --title TITLE [--location ...].")
    raw = Path(args.text_file).read_text(encoding="utf-8")
    text = html_to_text(raw) if "<" in raw and ">" in raw else clean_text(raw)
    url = posting_link(args.url) or args.url   # the opening's own link, its "#" part kept (Codex H10, Pass 9)
    # the manual id from the employer's canonical form, as its request binds it (a paste's comes from its kept company):
    # a name typed with an accent composed (U+00E9) or combining (e, U+0301) is one request and one id (the
    # coordinator's item after Codex Pass 17, D201)
    slug = re.sub(r"[^a-z0-9]+", "-", clean_text(employer).lower()).strip("-")
    from careeros.discovery.manual_intake import requisition_in
    posting = RawPosting(ats="manual", board_token=slug, external_id=text_fingerprint(url)[:16], title=title,
                         location=location or "", url=url, apply_url=url, description_text=text,
                         requisition_id=requisition_in(text),
                         raw={"url": url, "text": text, "employer": employer,
                              "location_source": "argument" if location else "none"})
    placed: dict = {}
    from careeros.discovery.manual_intake import begin, complete, operation_for, recorded_job, request_of
    from careeros.pipeline import employer_entry
    # the operation is the request as stored: the link as add-url stores it, the text as its reader took it, and your
    # arguments (the D190 design, section 6)
    op = operation_for("add_url", "", request_of(url, text, employer=employer, parent=parent, title=title,
                                                 location=location),
                       separate=getattr(args, "separate", False), operation_id=getattr(args, "operation", None))
    if op.separate:
        print(f"operation {op.key} (to retry it after an interruption: careeros add-url ... --operation {op.key})")
    state, recorded = recorded_job(ctx.store, op)
    if state == "done" and recorded:
        print(f"{title} — already recorded: {recorded} (operation {op.key})")
        return recorded
    begin(ctx.store, op)
    generated = f"{posting.ats}:{posting.board_token}:{posting.external_id}"
    from careeros.discovery.manual_intake import NotStored, not_stored
    from careeros.pipeline import nested_ingest

    def written(counts) -> None:   # the job and the operation's completion: one transaction (sections 6 and 7)
        if generated not in placed:
            raise NotStored(generated)
        complete(ctx.store, op, placed[generated])
    try:
        result = nested_ingest(ctx, employer_entry(ctx, name=employer, parent=parent), [posting],
                               placed=placed, after=written)
    except NotStored as exc:   # refused or failed: rolled back whole, no lead linked (Codex H14, Pass 10)
        not_stored(ctx.store, generated, "add-url", getattr(exc, "reasons", ()))
        sys.exit("Not added: the posting could not be stored (recorded as an open exception on the review page)")
    print(f"{title} — " + " ".join(f"{k}={v}" for k, v in result.items()))
    _link_leads(ctx, placed[generated], "added")   # the job actually written (Codex H11, Pass 9)
    _withdraw_lost_handoffs(ctx, paths)
    return placed[generated]


def _link_leads(ctx: Context, job_id: str | None, via: str) -> Counter:
    """A posting you added or pasted yourself links the lead for the same posting (2026-11-02)."""
    from careeros.discovery.lead_resolution import link_same_posting
    found = link_same_posting(ctx, job_id, via=via)
    if found:
        print("  leads: " + " ".join(f"{k.removeprefix('leads_')}={v}" for k, v in sorted(found.items())))
    return found


def _move(path: Path, folder: Path, name: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    named = Path(name or path.name)
    target = folder / named.name
    n = 1
    while True:
        try:
            if not target.exists():
                path.rename(target)
                return target
        except FileExistsError:   # another file took the name between the check and the rename: the next name
            pass
        target = folder / f"{named.stem}.{n}{named.suffix}"
        n += 1


CLAIMED = ".importing"   # a saved email being imported: claimed under this suffix before it is read (CR-AD-05)


STOPPED_SINCE = "agency_stop_since"   # meta: when alert intake first stopped (a missing agency setting, L4-02; a changed file, D141)


def _alert_config_check(store, *, enroll: bool = True) -> None:
    """Before any alert intake: the private-sites file reads (2026-11-09), and no enrolled or stored agency host is
    missing from it (D125, fail closed). Either problem raises ValueError, which every intake reports and stops on.
    Hosts are enrolled only after the check passes, so a refused run never enrols the host it refused (L3-01); the time
    of the first refusal is kept, so the Gmail pull after the setting is restored looks back that far (L4-02)."""
    from careeros.core import private_sites
    from careeros.discovery.alerts import agency_setting_problem, enroll_agency_hosts
    private_sites.load()   # a file that cannot be read stops Gmail intake: _gmail_pull keeps its start (the red team)
    problem = agency_setting_problem(store)
    if problem:
        _note_stop(store)
        raise ValueError(problem)
    if enroll:
        enroll_agency_hosts(store)   # every configured tracking host is remembered for good (CR-AD-V2-02)


def _enroll_reading(store) -> None:
    """Every tracking host of the run's reading of the private settings is enrolled before the run uses it, whatever
    the command (alerts.enroll_reading; CR-AD-V2-02, D141)."""
    from careeros.discovery.alerts import enroll_reading
    enroll_reading(store)


class CommandStopped(BaseException):
    """The run's private settings file changed since the run read it, or the run saw a change before: the whole command
    ends at once, with no grading, refresh, report, probe or upload after it (D153 R2, Codex Pass 5). A BaseException,
    so no handler of ordinary errors on the way carries the command on; a transaction open meanwhile rolls back on it,
    as on any exception. main() reports it in one line."""


def _halt_if_changed(store=None, what: str = "") -> None:
    """End the whole command (CommandStopped) when the run's private settings changed since the run read them, or the
    run saw a change before (D141, D153 R2). With a store, the stop is filed as a problem ("private_sites_changed")
    naming what was not done; the next run reads the change and does it."""
    from careeros.core import private_sites
    try:
        private_sites.check()
    except private_sites.SettingsChanged as exc:
        if store is not None:
            store.writer("discovery").exception("validation", "medium", f"{what}: {exc}", error_code="private_sites_changed")
        raise CommandStopped(f"{what}: {exc}") from None


def _note_stop(store) -> None:
    """Keep the time alert intake first stopped for a missing agency setting (L4-02). Called again after a refusal that
    a rolled-back transaction undid, so the catch-up after the stop still looks back that far."""
    row = store.one("SELECT value FROM meta WHERE key = ?", (STOPPED_SINCE,))
    if row is None:
        store.writer("admin").insert("meta", {"key": STOPPED_SINCE, "value": store.clock.now()})
    elif not row["value"]:
        store.writer("admin").update("meta", {"value": store.clock.now()}, "key = ?", (STOPPED_SINCE,))


def _setting_refusal(exc: Exception) -> bool:
    """Whether an error is the private settings refusing a unit of work (missing entry, changed or broken file)."""
    return isinstance(exc, ValueError) and str(exc).startswith(("agency_setting_missing", "private_sites_"))


def _stopped_days(store) -> int | None:
    """Days since alert intake first stopped for a missing agency setting, if it did and has not caught up yet."""
    row = store.one("SELECT value FROM meta WHERE key = ?", (STOPPED_SINCE,))
    if row is None or not row["value"]:
        return None
    from careeros.core.clock import parse_utc
    return int((parse_utc(store.clock.now()) - parse_utc(row["value"])).total_seconds() // 86400) + 2


def _setting_error_code(exc: Exception) -> str:
    """The error code a refused intake files: a missing agency entry, settings that changed mid-read, or a broken file."""
    return next((code for code in ("agency_setting_missing", "private_sites_changed") if str(exc).startswith(code)),
                "private_sites_invalid")


def _is_agency_digest(alert) -> bool:
    """An email recognised as an agency's digest by its sender and bulk header (not one held only for its envelope)."""
    return alert.digest_status is not None and alert.digest_reason not in ("envelope", "unreadable")


def _skip_as_correspondence(alert) -> bool:
    """Application, interview or recruiter mail is never read for jobs. A recognised agency digest whose subject has only
    an ambiguous phrase ("next steps", "message from") is read or held as a digest: read only when every card and the
    recorded layout match, held otherwise. A subject that names an application, an interview, an assessment or an offer
    is skipped whoever sends it, the agency's bulk sender included (L1-01, CR-AD-V2-11)."""
    from careeros.discovery.alerts import explicit_correspondence, is_correspondence
    if not is_correspondence(alert.subject):
        return False
    return explicit_correspondence(alert.subject) or not _is_agency_digest(alert)


def _return_claim(claimed: Path, original: Path) -> Path:
    """A claimed email back in the inbox under its own name, or NAME.recovered-N.eml when another email has that name:
    never over another email, also when one appears just before the rename (CR-AD-V2-05)."""
    target, n = original, 1
    while True:
        try:
            if not target.exists():
                claimed.rename(target)
                return target
        except FileExistsError:
            pass
        target = original.with_name(f"{original.stem}.recovered-{n}{original.suffix}")
        n += 1


class _FileBusy(Exception):
    """A saved email still open for writing in another program: left in the inbox for the next import."""


@contextmanager
def _steady(path: Path, limit: int):
    """The first `limit` bytes of a claimed email, read under a handle that refuses every writer until the email has
    been archived (Windows): an email another program still has open for writing raises _FileBusy and goes back to the
    inbox, so the archived bytes are the bytes that were read (CR-AD-V2-05, Pass 2), and an email is judged by its size
    only once no program is writing it (Codex Pass 3). Elsewhere a plain read; _archived_as_read then checks the
    archived copy."""
    if sys.platform != "win32":
        with open(path, "rb") as plain:
            yield plain.read(limit)
        return
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # GENERIC_READ, shared for reading and for the archive's rename only (never for writing), OPEN_EXISTING
    handle = create(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        if error in (32, 33):   # a sharing or lock violation: another program has it open for writing
            raise _FileBusy(path.name)
        raise ctypes.WinError(error)
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)), "rb") as held:
        try:
            data = held.read(limit)
        except OSError as exc:   # a byte-range lock: another program is writing it (the red team before Pass 4)
            if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (32, 33):
                raise _FileBusy(path.name) from None
            raise
        yield data


def _archived_as_read(archived: Path, data: bytes, original: Path) -> bool:
    """Whether the archived email holds the bytes that were read. If not (a writer changed it after the read, where no
    lock could keep it out), the archive is set back to what was read and the changed bytes go back to the inbox as a new
    email, NAME.changed-N.eml, for the next import (CR-AD-V2-05, Pass 2)."""
    current = archived.read_bytes()
    if current == data:
        return True
    n = 1
    while True:
        target = original.with_name(f"{original.stem}.changed-{n}{original.suffix}")
        try:
            with open(target, "xb") as fresh:
                fresh.write(current)
            break
        except FileExistsError:
            n += 1
    archived.write_bytes(data)
    return False


def cmd_import(ctx: Context, paths, args) -> Counter:
    """Import postings you saved into the vault inbox (sites CareerOS may not read automatically). The whole import uses
    one reading of the private settings (D141)."""
    from careeros.core import private_sites
    with private_sites.run():
        counts = _import_inbox(ctx, paths, args)
    _withdraw_lost_handoffs(ctx, paths)
    return counts


def _import_inbox(ctx: Context, paths, args) -> Counter:
    from careeros.discovery.saved import SavedPostingError, parse_saved_file
    inbox = paths["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    for stale in sorted(inbox.glob("*" + CLAIMED)):   # an import that stopped half-way: the email goes back for this one
        original = stale.with_name(re.sub(r"(\.[0-9a-f]{8})?" + re.escape(CLAIMED) + "$", "", stale.name))
        _return_claim(stale, original)
    day = ctx.store.clock.now()[:10]
    counts = Counter()
    files = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm", ".txt", ".eml"))
    if not files:
        print(f"Inbox is empty: save posting pages (Ctrl+S, 'Webpage, HTML only') or job-alert emails (.eml) into {inbox}")
        return counts
    client = None
    for n, path in enumerate(files):
        _halt_if_changed(ctx.store, f"import stopped; {len(files) - n} file(s) left in the inbox for the next one")
        if path.suffix.lower() == ".eml":
            client = client or HttpClient(min_interval=ctx.policy.get("discovery", "min_interval_seconds", default=0.5))
            counts.update(_import_alert(ctx, path, client, inbox, day))
            continue
        try:
            posting, employer = parse_saved_file(path)
        except Exception as exc:   # noqa: BLE001 - any file it can't read is kept in failed with the reason, and the
            # files after it are still imported (a malformed link: the red team; an unexpected page shape: RT-33)
            if not isinstance(exc, (SavedPostingError, UnicodeError, ValueError)):
                exc = SavedPostingError(f"unreadable saved page ({type(exc).__name__}: {exc})")
            ctx.store.writer("discovery").exception("validation", "low", f"saved posting not imported ({path.name}): {exc}",
                                                    error_code="saved_posting_unreadable")
            _move(path, inbox / "failed" / day)
            counts["failed"] += 1
            print(f"  ! {path.name}: {exc}")
            continue
        import hashlib

        from careeros.discovery.manual_intake import begin, complete, operation_for, recorded_job
        from careeros.pipeline import employer_entry
        entry = employer_entry(ctx, name=employer)   # the one resolver (the D190 design)
        parent = entry["parent"]
        placed: dict = {}
        generated = f"{posting.ats}:{posting.board_token}:{posting.external_id}"
        # the operation is the saved file's bytes (the D190 design, section 6): an import interrupted after its job
        # was written finds it, and writes nothing again
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        op = operation_for("import", digest, {"file": digest}, separate=getattr(args, "separate", False))
        state, recorded = recorded_job(ctx.store, op)
        if state == "done" and recorded:
            _move(path, inbox / "imported" / day)
            counts["already_recorded"] += 1
            print(f"  {posting.title} | {employer} | already recorded: {recorded}")
            continue
        begin(ctx.store, op)
        if op.separate:
            print(f"  operation {op.key} ({path.name})")
        from careeros.discovery.manual_intake import NotStored, not_stored
        from careeros.pipeline import nested_ingest

        def written(result, op=op, generated=generated, placed=placed) -> None:   # sections 6 and 7
            if generated not in placed:
                raise NotStored(generated)
            complete(ctx.store, op, placed[generated])
        try:
            result = nested_ingest(ctx, entry, [posting], placed=placed, after=written)
        except NotStored as exc:   # rolled back whole: the file is kept in failed below
            not_stored(ctx.store, generated, "import", getattr(exc, "reasons", ()))
            result = Counter(errors=1)
        counts.update(result)
        if generated not in placed:
            # refused (no free id) or failed: nothing written, so no lead is linked and the file is kept in failed with
            # the reason in the exceptions (Codex CR-2026-11-15-GATE-FIXES H14, Pass 10; the owner's choice, D178)
            _move(path, inbox / "failed" / day)
            counts["failed"] += 1
            print(f"  ! {path.name}: not stored, kept in failed (recorded as an open exception on the review page)")
            continue
        counts.update(_link_leads(ctx, placed[generated], "imported"))   # the job written (H11, Pass 9)
        _move(path, inbox / "imported" / day)
        print(f"  {posting.title} | {employer}{' (' + parent + ')' if parent != employer else ''} | "
              + " ".join(f"{k}={v}" for k, v in result.items()))
    if client:
        client.close()
    _halt_if_changed(ctx.store, "import stopped before processing; the next import processes and reports")   # D153 R2
    counts.update(process(ctx))
    cmd_report(ctx, paths, args)
    return counts


def _import_alert(ctx: Context, path: Path, client: HttpClient, inbox: Path, day: str) -> Counter:
    """One saved job-alert email file (.eml): claimed, read under a lock that keeps writers out, decided under one
    revision of the private settings, then archived; refused and returned to the inbox when the email is still being
    written or the settings changed meanwhile (CR-AD-V2-05, CR-AD-V2-07)."""
    from careeros.core import private_sites
    from careeros.discovery.alerts import MAX_EML_BYTES
    try:
        _alert_config_check(ctx.store)
    except ValueError as exc:  # a broken private-sites file or a missing agency entry: keep the email for a re-run
        ctx.store.writer("discovery").exception("validation", "medium", f"alert email not imported ({path.name}): {exc}",
                                                error_code=_setting_error_code(exc))
        advice = ("restore the agency entry, then import again" if _setting_error_code(exc) == "agency_setting_missing"
                  else "fix the file and import again")
        print(f"  ! {path.name}: {exc} (left in the inbox; {advice})")
        return Counter(alert_config_error=1)
    claimed = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}{CLAIMED}")   # a name of its own (CR-AD-V2-05)
    try:
        path.rename(claimed)   # claimed before it is read: an email written to the inbox path from now on is a new
    except OSError as exc:     # one, for the next import, and never moved or closed for this one (CR-AD-05)
        print(f"  ! {path.name}: could not be claimed for import ({exc}); left in the inbox")
        return Counter(alert_file_busy=1)
    try:
        with _steady(claimed, MAX_EML_BYTES + 1) as data:   # one snapshot of the claimed file: sized, parsed and archived
            if len(data) > MAX_EML_BYTES:   # judged by its size only once no writer has it (CR-AD-V2-05, Codex Pass 3)
                _move(claimed, inbox / "failed" / day, path.name)
                ctx.store.writer("discovery").exception("validation", "low", f"alert email not imported ({path.name}): "
                                                        "email is larger than 5 MB", error_code="alert_email_unreadable")
                print(f"  ! {path.name}: email is larger than 5 MB")
                return Counter(failed=1)
            try:
                # one reading of the settings for the email, from its check through its fetch-only step (no transaction
                # spans a network call: the D190 design, section 5) to its commit (S2-02, D141)
                with private_sites.pinned():
                    _alert_config_check(ctx.store, enroll=False)   # the stop, under the reading the email is read with (S2-01)
                    prefetched = _prefetch_eml(ctx, data, client)
                    with ctx.store.tx():
                        folder, counts, note = _decide_alert_file(ctx, path, data, client, prefetched)
                        private_sites.check()   # the reading still in force, just before the commit
            except BatchAborted as exc:   # rolled back: the job it would have written is held, and the import stops
                hold_aborted(ctx.store, exc)
                raise
            archived = _move(claimed, inbox / folder / day, path.name)
    except _FileBusy:
        _return_claim(claimed, path)
        print(f"  ! {path.name}: still open in another program; left in the inbox for the next import")
        return Counter(alert_file_busy=1)
    except ValueError as exc:   # the settings changed, went missing or broke while this email was read: back to the inbox
        if not _setting_refusal(exc):
            raise
        _return_claim(claimed, path)
        if _setting_error_code(exc) == "agency_setting_missing":
            _note_stop(ctx.store)
        ctx.store.writer("discovery").exception("validation", "medium", f"alert email not imported ({path.name}): {exc}",
                                                error_code=_setting_error_code(exc))
        print(f"  ! {path.name}: {exc} (left in the inbox)")
        return Counter(alert_config_error=1)
    if not _archived_as_read(archived, data, path):
        counts["alert_file_changed"] += 1
        note += " (the file changed while it was read: the new version is back in the inbox)"
    print(note)
    return counts


def _prefetch_eml(ctx: Context, data: bytes, client: HttpClient) -> dict | None:
    """The fetch-only step for a saved alert email, before its transaction; None when it does not parse (its own pass
    reports that)."""
    from careeros.discovery.alerts import parse_eml_bytes
    try:
        alert = parse_eml_bytes(data)
    except Exception:   # noqa: BLE001 - reported by the email's own pass
        return None
    return _prefetch_alert(ctx, alert, client)


def _decide_alert_file(ctx: Context, path: Path, data: bytes, client: HttpClient,
                       prefetched: dict | None = None) -> tuple[str, Counter, str]:
    """(folder, counts, line to print) for one saved alert email's bytes: read and recorded, held, or skipped."""
    from careeros.discovery.alerts import AlertEmailError, parse_eml_bytes
    try:
        alert = parse_eml_bytes(data)
        identity = "eml:" + hashlib.sha256(data).hexdigest()[:24]  # a held digest's identity
    except (AlertEmailError, UnicodeError, LookupError) as exc:
        ctx.store.writer("discovery").exception("validation", "low", f"alert email not imported ({path.name}): {exc}",
                                                error_code="alert_email_unreadable")
        return "failed", Counter(failed=1), f"  ! {path.name}: {exc}"
    if _skip_as_correspondence(alert):  # application / recruiter mail (L1-01, CR-AD-V2-11)
        return ("failed", Counter(correspondence_skipped=1),
                f"  ! {path.name}: not a job alert (application or recruiter correspondence); skipped")
    counts = _record_alert(ctx, alert, client, prefetched=prefetched)
    if alert.digest_status and (alert.digest_status == "partial" or counts["lead_errors"]):
        # An agency digest read only in part: the cards it is sure of are saved; the file is not "imported" (2026-11-09).
        # Its identity is its bytes, so another email saved under the same name never closes this one (CR-AD-05).
        _hold_digest(ctx, "alert_file", identity, alert, counts)
        counts["agency_digest_partial"] += 1
        return ("failed", counts, f"  ! {path.name}: an agency job-alert digest read only in part ({len(alert.links)} jobs "
                "saved); open the email itself for the rest")
    if alert.digest_status:
        _release_digest(ctx, "alert_file", identity)
    if counts["lead_errors"] or counts["lead_store_failed"] or counts["lead_fetch_retry"]:
        # a line that could not be processed, stored or read now: the email is kept in failed, so importing it again
        # retries that line (the others are seen before); never filed as imported with the line lost (the red team
        # before Pass 11; Z2)
        return ("failed", counts, f"  ! {path.name}: "
                f"{counts['lead_errors'] + counts['lead_store_failed'] + counts['lead_fetch_retry']} link(s) not "
                "processed; kept in failed to import again")
    return "imported", counts, f"  {path.name}: {len(alert.links)} links | " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


HELD_DIGEST = "agency_digest_held"
HELD_TRY = "agency_held_try:"   # meta: a held Gmail digest's last reading, as _held_key() was then


def _reader_fingerprint() -> str:
    """The code that decides how an email is read, as a hash of every CareerOS source file and of Python's version: the
    reader also uses the text cleaning, the board parsers, the settings parser and Python's own email and HTML parsers
    (the red team before Pass 4). An update of either reads every held digest again once."""
    if not _READER:
        digest = hashlib.sha256(sys.version.encode("utf-8"))
        package = Path(__file__).resolve().parent
        for module in sorted(package.rglob("*.py")):
            digest.update(module.relative_to(package).as_posix().encode("utf-8") + b"\0" + module.read_bytes())
        _READER.append(digest.hexdigest()[:16])
    return _READER[0]


_READER: list[str] = []


def _held_key() -> str:
    """What a held digest's reading depends on besides the email itself, which Gmail never changes: the private settings'
    revision (the agency's entry, its layout), the reader's code, and how Gmail is read (the API, or IMAP with or without
    All Mail: a held email IMAP could not reach is fetched again once it can, Codex Pass 3). A held digest is read
    again only when this changed since its last reading."""
    from careeros.core import private_sites
    return f"{private_sites.revision()}:{_reader_fingerprint()}:{_ACCESS.get()}"


UNSTORED_LINE = "alert_line_unstored"


def _hold_unstored(ctx: Context, message_id: str, retry_leads: list | None = None) -> None:
    """A Gmail alert with a job line read but not stored: one open problem under its id, so every later pull reads it
    again by that id, whatever the search window, until a pass stores the line (the red team after Codex Pass 12, RT4-6:
    the retry ended when the email aged out of the search). `retry_leads`: the leads kept for its lines whose fetch
    failed for a moment, which the problem lists, so its closing can say they will not be retried (BATCH_5 E2)."""
    writer = ctx.store.writer("discovery")
    row = ctx.store.one("SELECT exception_id, metadata_json FROM exceptions WHERE error_code = ? AND entity_type = "
                        "'gmail_message' AND entity_id = ? AND status != 'resolved'", (UNSTORED_LINE, message_id))
    if row is not None:
        if retry_leads:
            known = (json.loads(row["metadata_json"] or "{}") or {}).get("retry_leads") or []
            writer.annotate_exception(row["exception_id"], {"retry_leads": sorted(set(known) | set(retry_leads))})
        return
    writer.exception(
        "integration", "medium", "An alert email has a job line that was not processed, not stored or not read for a "
        "moment; it is read again by its id on every pull until every line is stored. A line read but not stored, or not "
        "read for a moment, is a lead on the review page meanwhile, with its link and title; a line not processed is an "
        "alert_line_failed problem under careeros status.",
        error_code=UNSTORED_LINE, entity_type="gmail_message", entity_id=message_id, retryable=True,
        metadata={"retry_leads": sorted(set(retry_leads or []))})


NOT_RETRIED = "will not be retried: the email is no longer read; open its link"


def _close_retry_leads(ctx: Context, metadata_json: str | None) -> int:
    """The leads an email's closed retry kept for its lines whose fetch failed for a moment: their mark says nothing
    retries them now (BATCH_5 E2, the coordinator on 58126d5). Returns how many."""
    try:
        leads = (json.loads(metadata_json or "{}") or {}).get("retry_leads") or []
    except ValueError:
        leads = []
    writer, closed = ctx.store.writer("discovery"), 0
    for lid in leads:
        lead = ctx.store.one("SELECT fetch_failure FROM leads WHERE lead_id = ? AND status = 'new' AND fetch_failure LIKE ?",
                             (lid, RETRY_MARK + "%"))
        if lead is not None and lead["fetch_failure"].endswith("will retry"):
            writer.update("leads", {"fetch_failure": (lead["fetch_failure"][:-len("will retry")] + NOT_RETRIED)[:200]},
                          "lead_id = ?", (lid,))
            closed += 1
    return closed


def _settle_unstored(ctx: Context, message_id: str) -> None:
    writer = ctx.store.writer("discovery")
    for row in ctx.store.all("SELECT exception_id FROM exceptions WHERE error_code = ? AND entity_type = 'gmail_message' "
                             "AND entity_id = ? AND status != 'resolved'", (UNSTORED_LINE, message_id)):
        writer.resolve_exception(row["exception_id"], "every job line of the email was stored on a later pull")


def _note_held_try(ctx: Context, message_id: str, value: str | None = None) -> None:
    """Remember the key a held Gmail digest was last read under ("" forgets it)."""
    key, value = HELD_TRY + message_id, _held_key() if value is None else value
    writer = ctx.store.writer("admin")
    if ctx.store.one("SELECT 1 FROM meta WHERE key = ?", (key,)):
        writer.update("meta", {"value": value}, "key = ?", (key,))
    elif value:
        writer.insert("meta", {"key": key, "value": value})


def _hold_digest(ctx: Context, entity_type: str, entity_id: str, alert, counts: Counter) -> None:
    """One open problem per held-back agency digest (2026-11-09): the review page shows a banner while any is recent, so a
    changed email layout cannot lose jobs in silence. Counts only: no title, link or agency name is written. A Gmail
    digest held for what it is (read only in part, and every card it could read saved) remembers the key it was read
    under, so it is read again only after a change that could read it differently; one where a row failed to save
    forgets it, so the next pull reads it again (Codex Pass 3)."""
    if entity_type == "gmail_message" and alert.digest_status == "partial":
        _note_held_try(ctx, entity_id, "" if counts["lead_errors"] else None)
    if ctx.store.one("SELECT 1 FROM exceptions WHERE error_code = ? AND entity_type = ? AND entity_id = ? AND status != 'resolved'",
                     (HELD_DIGEST, entity_type, entity_id)):
        return  # already reported; a retry that still fails adds nothing
    reasons = {"layout": "its layout differs from the recorded one: a changed template, or a job behind another link",
               "no layout recorded": "no layout fingerprint is recorded for this agency (careeros agency-layout)",
               "plain text": "it has no HTML part", "cards": "a job card could not be read",
               "links": "a link in it is never closed, so it cannot be judged",
               "markup": "its HTML has a part a browser could show that CareerOS cannot read as a browser does",
               "correspondence": "it reads like application, interview, assessment or offer mail, so no job in it was read",
               "envelope": "it carries the agency's tracking links or name but not its digest's sender or bulk-mail header",
               "unreadable": "it could not be read as an email",
               "attachment": "it has an attachment or another part besides its text and HTML body, which a reader could "
                             "open, so none of it was read (D153)"}
    if alert.digest_status == "partial":
        reason = reasons.get(alert.digest_reason or "cards", reasons["cards"])
    else:
        reason = "a job could not be saved"
    retry = ("CareerOS reads it again, whatever the Gmail search window, after any change that could read it "
             "differently (the agency's entry or layout, or a CareerOS update). careeros agency-held lists held digests; "
             "--dismiss ID closes one you have looked at." if entity_type == "gmail_message"
             else "The file is in the inbox's failed folder; import it again after a fix, or close the problem with "
                  "careeros agency-held --dismiss ID.")
    ctx.store.writer("discovery").exception(
        "integration", "medium",
        f"An agency job-alert digest was read only in part ({reason}; {len(alert.links)} jobs saved). Open the email "
        f"itself for the rest. {retry}",
        error_code=HELD_DIGEST, entity_type=entity_type, entity_id=entity_id, retryable=True,
        metadata={"status": alert.digest_status, "jobs_saved": len(alert.links), "row_errors": counts["lead_errors"],
                  **({"account": _ACCESS.get().rsplit(":", 1)[-1]} if entity_type == "gmail_message" else {})})


def _release_digest(ctx: Context, entity_type: str, entity_id: str) -> None:
    """A held-back digest that a later pass read in full: its problem is resolved, with an event."""
    writer = ctx.store.writer("discovery")
    for row in ctx.store.all("SELECT exception_id FROM exceptions WHERE error_code = ? AND entity_type = ? AND entity_id = ? "
                             "AND status != 'resolved'", (HELD_DIGEST, entity_type, entity_id)):
        writer.resolve_exception(row["exception_id"], "the agency digest was read in full on a later pass")
    if entity_type == "gmail_message":
        _note_held_try(ctx, entity_id, "")


class Unfetched(Exception):
    """A line's board posting was not fetched ahead of its email's transaction (the planning pass records the need; in
    the email's own pass it means a line the plan did not foresee): never fetched inside a write transaction."""


class _Planned(Exception):
    pass


RETRY_MARK = "its own link: fetch failed for a moment"


def _keep_retry_lead(ctx: Context, row: dict, why: str, now: str) -> None:
    """The lead of an alert line whose fetch failed for a moment (or was not made yet), with the card's link and title,
    marked to be read again: the card's trace while its email stays unsettled, and after its retry record closes
    (BATCH_5 E2). The retry reads it again (never "seen before") and updates it."""
    mark = f"{RETRY_MARK} ({why}, {now[:10]}); will retry"[:200]
    writer = ctx.store.writer("discovery")
    with ctx.store.tx():
        if ctx.store.one("SELECT 1 FROM leads WHERE lead_id = ?", (row["lead_id"],)):
            writer.update("leads", {"fetch_failure": mark}, "lead_id = ?", (row["lead_id"],))
        else:
            writer.insert("leads", {**row, "status": "new", "job_id": None, "fetch_failure": mark})


def _line_failure(exc: Exception) -> FetchError:
    """Any exception from one alert line's fetch, as that line's failure (BATCH_5 E1): a timeout, a network error, a 5xx
    or a rate limit may pass by itself (the line is read again, Z2); anything else (too many redirects, an answer that
    cannot be decoded or has a shape the reader does not know) is definite, and the line is kept as a lead with it. The
    rest of the email is applied either way."""
    import httpx
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return FetchError(f"network:{type(exc).__name__}", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429):
        return FetchError(f"http_{exc.response.status_code}", status=exc.response.status_code, retryable=True)
    return FetchError(f"unreadable_answer:{type(exc).__name__}")


def transient(exc: FetchError) -> bool:
    """A fetch failure that may pass by itself (a timeout, a network error, a 5xx, a rate limit, the run's request budget):
    the line stays unapplied and its email unsettled, and the next run retries it. Any other answer (not found,
    unsupported, a source not allowed) is definite: the line is kept as a lead with its reason (Z2, the coordinator's
    reading of section 5)."""
    return bool(exc.retryable) or exc.code == "budget_exhausted"


def _prefetch_alert(ctx: Context, alert, client: HttpClient, profile: str = "primary") -> dict:
    """The fetch-only step (the D190 design, section 5; Codex Pass 2 H7): which lines need a board fetch is learned by a
    pass over the email that keeps nothing it writes (rolled back), each such posting is fetched with no transaction
    open, and a read_pending hold is committed on every existing job they resolve to, by the line's position (section 7,
    Z1). Returns {"fetched": {link: posting or FetchError}, "ahead": {position: (hold_id, target)}}."""
    from careeros.pipeline import target_of, write_ahead
    needed: dict[int, str] = {}
    try:
        with ctx.store.tx():
            _alert_lines(ctx, alert, client, profile=profile, needed=needed)
            raise _Planned
    except _Planned:
        pass
    except Exception:   # noqa: BLE001 - the email's own pass reports it; nothing is fetched ahead
        needed = {}
    fetched: dict = {}
    for url in dict.fromkeys(needed.values()):
        parsed = parse_job_url(url)
        if parsed is None:
            continue
        try:
            fetched[url] = fetch_single(client, *parsed)
        except FetchError as exc:
            fetched[url] = exc
        except Exception as exc:   # noqa: BLE001 - one line's answer, never the whole email's (BATCH_5 E1)
            fetched[url] = _line_failure(exc)
    reads = []
    for position, url in needed.items():
        if isinstance(fetched.get(url), RawPosting):
            try:
                reads.append((position, target_of(ctx.store, None, fetched[url])[1]))
            except Exception:   # noqa: BLE001 - which job it is could not be read: ingest reports it in the email
                continue
    return {"fetched": fetched, "ahead": write_ahead(ctx.store, reads)}


def _record_alert(ctx: Context, alert, client: HttpClient, outcomes: list | None = None, profile: str = "primary",
                  *, prefetched: dict | None = None) -> Counter:
    """An alert's lines recorded (_alert_lines), with its board postings fetched ahead (`prefetched`)."""
    return _alert_lines(ctx, alert, client, outcomes, profile, prefetched=prefetched)


def _alert_lines(ctx: Context, alert, client: HttpClient, outcomes: list | None = None, profile: str = "primary",
                 *, prefetched: dict | None = None, needed: dict | None = None) -> Counter:
    """Links on readable job boards are graded; every other job link becomes a lead for you to open yourself.
    `outcomes`, when given, receives (link, outcome) per link: graded / already_tracked / lead /
    outside_target_families / seen_before / unreadable_kept_as_lead / kept_as_lead_other_posting / notice /
    notice_seen_before / no_title / not_a_job_link / suppressed / malformed_row / row_error. One bad line never stops
    the others. A card whose own link names no posting is a notice (part 3 of the owner's D192 build), never a lead.
    `profile` "night" judges relevance with the NIGHT title families and tags the leads as NIGHT. `prefetched`: the
    fetch-only step's postings and write-ahead holds (_prefetch_alert), so nothing is fetched inside the email's
    transaction; `needed`, in the planning pass, receives each line that needs a board fetch."""
    def note(link, outcome):
        if outcomes is not None:
            outcomes.append((link, outcome))

    from careeros.discovery import sources
    from careeros.discovery.alerts import agency_lead_id, is_local_link, kept_for_review, lead_id
    from careeros.normalize.posting import classify_family, excluded_term, norm
    from careeros.normalize.posting import contains_term
    from careeros.qualify.night import night_family
    counts = Counter()
    for reason, n in (getattr(alert, "skipped", None) or {}).items():  # search pages and unreadable links: counted
        counts[f"link_{reason}_skipped"] += n
    families, excluded = ctx.policy.get("families", default={}), ctx.policy.get("title_exclude", default=[])
    blocked = [norm(e) for e in ctx.policy.get("gate", "excluded_employers", default=[]) or [] if norm(e)]
    writer, now = ctx.store.writer("discovery"), ctx.store.clock.now()
    identity_cache: dict = {}
    planned = prefetched is not None or needed is not None   # the email's batch: fetched ahead, held ahead
    batch = prefetched or {"fetched": {}, "ahead": {}}
    batch_created: set = set()
    for position, link in enumerate(alert.links):
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
            else:
                family = None if excluded_term(title_norm, excluded) else classify_family(title_norm, families)
            if not (link.job_like or family):  # navigation, settings and other links that are not postings
                note(link, "not_a_job_link")
                continue
            tracker = is_local_link(link.url)   # an agency's tracking link, or a review link kept under a local one (D156)
            lid = agency_lead_id(link.url) if tracker else lead_id(link.url)   # the agency mark stays with the row (CR-AD-02)
            from careeros.discovery.board_discovery import names_one_posting
            # a link that names no single posting is a shared page, however job-like it looks ("/jobs/search",
            # "#/jobs": the red team after Codex Pass 12, RT3-5): its card is a notice, never a lead (D190 rule 3; the
            # D190 design, revision 3, section 5; part 3 of the owner's D192 build), one per occurrence, so no older
            # card with the same link, title or stated identity hides it (Codex's D190 design review M4)
            if not tracker and not names_one_posting(link.url) and not kept_for_review(link.url):
                noticed = _record_notice(ctx, alert, link, position, title_norm, family, profile, identity_cache)
                counts["notice_new" if noticed == "notice" else "notice_seen_before"] += 1
                note(link, noticed)
                continue
            stalled = ctx.store.one("SELECT 1 FROM leads WHERE lead_id = ? AND status = 'new' AND fetch_failure LIKE ?",
                                    (lid, "its own link: read but not stored%"))
            # a lead kept for a line whose fetch failed for a moment is read again too, never "seen before" (BATCH_5 E2),
            # by a card whose title is in a target family, which fetches it; any other card (one outside the families,
            # another email's) is "seen before" and leaves the mark, so the line that needs the read keeps retrying (the
            # fuzzer's seed 100081 on 51a8406)
            retrying = bool(family) and ctx.store.one(
                "SELECT 1 FROM leads WHERE lead_id = ? AND status = 'new' AND fetch_failure LIKE ?", (lid, RETRY_MARK + "%"))
            if not stalled and not retrying and ctx.store.one("SELECT 1 FROM leads WHERE lead_id IN (?, ?)",
                                                              (lid, agency_lead_id(link.url))):
                counts["lead_seen_before"] += 1  # the same exact link; also a stored agency card's, whatever the settings say now (CR-AD-06)
                note(link, "seen_before")
                continue
            # a stalled lead (read, not stored) is tried again under its own id (Codex CR-2026-11-15-GATE-FIXES H19, Pass 11)
            row = {"lead_id": lid, "url": link.url, "site": link.site, "title": link.text, "title_norm": title_norm, "family": family,
                   "context": link.context or None, "sender": alert.sender or None, "subject": alert.subject or None,
                   "received_at": alert.received_at, "first_seen_at": now, "status": "new", "job_id": None, "profile": profile}
            if tracker and sources.status(link.site) == "blocked":
                counts["lead_suppressed"] += 1
                note(link, "suppressed")
                continue
            if tracker and ctx.store.one("SELECT 1 FROM leads WHERE lead_id = ? OR (url = ? AND lead_id NOT LIKE 'lead:agency:%')",
                                         (lead_id(link.url), link.url)):   # under any ordinary id (a shared page's too)
                # The same personal link was stored earlier as an ordinary lead (the setting was missing then): a marked
                # twin, closed, keeps every row with this link local from now on (CR-AD-06).
                with ctx.store.tx():
                    writer.insert("leads", {**row, "status": "closed", "status_at": now,
                                            "status_reason": "the same personal tracking link as an earlier lead: kept local"})
                counts["lead_seen_before"] += 1
                note(link, "seen_before")
                continue
            # the same agency and job number as an earlier lead, in either arrival order: a possible repeat shown beside
            # it, never "seen" and never closed (Codex's D190 design review H8; part 3 of the owner's D192 build)
            row["repeat_of"] = _agency_repeat(ctx, link, lid) if tracker else _agency_job(ctx, link, lid)
            review = kept_for_review(link.url)   # never matched or fetched: an open lead until you dismiss it (D140 F2)
            known = board_url = None
            if not tracker:   # an agency's tracking link is never matched or fetched either
                known = None if review else _known_job(ctx, link.url, link.text, link.context)
                # a line here names one posting by its own link: it is fetched by that link or not at all (a careers
                # page's Greenhouse guess is a suggestion on its notice: part 3 of D192)
                board_url = None if review or not parse_job_url(link.url) else link.url
            if board_url and sources.status(parse_job_url(board_url)[0]) != "allowed":
                board_url = None  # the source is manual or blocked under the current site policy: list it, never fetch it
            if not tracker and not known and not board_url and _ledger_lead(ctx, link, title_norm):
                counts["lead_seen_before"] += 1  # the same job already listed through another site or URL
                note(link, "seen_before")
                continue
            outcome = None
            if known:  # a job CareerOS already tracks (e.g. a careers-page link to a board posting it polls)
                row.update(status="ingested", job_id=known)
                outcome = "already_tracked"
            elif family and board_url:  # a public job board CareerOS may read: grade it like any posting
                try:
                    held = batch["ahead"]
                    _, result, job_id = _ingest_board_link(
                        ctx, client, board_url, _company_name(link.context),
                        fetched=batch["fetched"] if planned else None,
                        ahead=({0: held[position]} if position in held else {}) if planned else None,
                        exclude=frozenset(h for at, (h, _) in held.items() if at != position), created=batch_created)
                    counts.update(result)
                    if job_id and not result.get("errors") and not _fetched_agrees(ctx, link, job_id, board_url):
                        # the posting read is not the line's: another title, employer or requisition (a guessed board
                        # link), or another requisition (its own link). It is graded as any posting; the lead stays open
                        # for you (Codex CR-2026-11-15-GATE-FIXES H35, Pass 14)
                        counts["lead_other_posting"] += 1
                        outcome = "kept_as_lead_other_posting"
                    elif job_id and not result.get("errors"):
                        row.update(status="ingested", job_id=job_id)
                        outcome = "graded"
                    else:   # read but not stored: the lead stays open, with why, for the next read (red team, Pass 11)
                        row["fetch_failure"] = f"its own link: read but not stored ({now[:10]})"
                        counts["lead_store_failed"] += 1
                        outcome = "unreadable_kept_as_lead"
                except Unfetched:
                    # not fetched ahead (the planning pass records the need): the line is not applied, and its email
                    # stays unsettled, so the next run reads it again (the D190 design, section 5)
                    if needed is not None:
                        needed[position] = board_url
                    elif not stalled:   # the card's trace meanwhile: its lead, marked to be read again (BATCH_5 E2)
                        _keep_retry_lead(ctx, row, "not fetched yet", now)
                    counts["lead_fetch_retry"] += 1
                    note(link, "fetch_retry")
                    continue
                except FetchError as exc:
                    if transient(exc):
                        # Z2: a failure that may pass by itself: the line is not applied and the email stays
                        # unsettled; the next run retries this occurrence only (the others are seen before). Its lead
                        # is kept meanwhile, marked to be read again, so the card is never stored nowhere; a lead read
                        # but not stored keeps its own mark, and stays pending (H25) (BATCH_5 E2)
                        if not stalled:
                            _keep_retry_lead(ctx, row, exc.code, now)
                        counts["lead_fetch_retry"] += 1
                        note(link, "fetch_retry")
                        print(f"    ! could not read {link.url} now: {exc.code} (read again next time; kept as a lead)")
                        continue
                    counts["lead_fetch_failed"] += 1
                    outcome = "unreadable_kept_as_lead" if board_url == link.url else None  # a wrong board-name guess is just a lead
                    # the reason is kept with the lead (2026-11-02): shown on the review page and by careeros leads
                    row["fetch_failure"] = (f"{'its own link' if board_url == link.url else 'a guessed board link'}: {exc.code} "
                                            f"({now[:10]})")[:200]
                    if stalled:
                        # a retry of a line read but not stored: it stays pending, and so does its email, whatever this
                        # fetch says; the next read tries again (Codex CR-2026-11-15-GATE-FIXES H25, Pass 12)
                        row["fetch_failure"] = f"its own link: read but not stored ({now[:10]}); last try: {exc.code}"[:200]
                        counts["lead_store_failed"] += 1
                    if outcome:
                        print(f"    ! could not read {link.url}: {exc.code} (kept as a lead)")
            from careeros.discovery.note import context_note
            from careeros.openings import record_observation
            fields = context_note(link.context)["fields"]
            from_inbox = (alert.sender or "").startswith("GitHub inbox")
            segments = (link.context or "").split(" · ")  # an agency card: "Agency · location · pay=...; req=..."
            location = segments[1] if tracker and len(segments) > 1 and "=" not in segments[1] else None
            with ctx.store.tx():
                if stalled or retrying:   # the same lead, updated only now that the line is read again (H19; E2)
                    writer.update("leads", {"status": row["status"], "job_id": row["job_id"],
                                            "fetch_failure": row.get("fetch_failure")}, "lead_id = ?", (lid,))
                else:
                    writer.insert("leads", row)
                record_observation(ctx.store, source_kind="recommendation" if from_inbox else "alert",
                                   evidence_level="recommendation" if from_inbox else "alert", job_id=row["job_id"], lead_id=lid,
                                   url=link.url, requisition_id=fields.get("req"), title=link.text, company=_company_name(link.context),
                                   location=location, pay_text=fields.get("pay"), published_at=None)
            counts["lead_ingested" if row["status"] == "ingested" else ("lead_new" if family else "lead_other_family")] += 1
            note(link, outcome or ("lead" if family else "outside_target_families"))
        except (TransactionLost, BatchAborted):
            # SQLite ended the email's whole transaction (a full disk), or a line resolved to a job not held ahead of
            # the email: nothing of the email is kept, and no other card is written outside it; the email is read
            # again next time (the D190 design, revision 4 and 8, section 7)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
            writer.exception("data_quality", "medium", f"alert line not processed: {type(exc).__name__}: {exc}",
                             error_code="alert_line_failed")
            counts["lead_errors"] += 1
            note(link, "row_error")
    return counts


def _fetched_agrees(ctx: Context, link, job_id: str, board_url: str) -> bool:
    """Whether the posting fetched for an alert line is the line's, by the owner's D188 (2026-11-19, "Exact links
    only"): only when it was fetched by the line's own link and is the posting that link names (board_discovery
    job_identity). A posting fetched by a guessed board link (greenhouse_candidate) is graded as any posting, and the
    line's lead stays open for you, whatever its title, employer or requisition say (Codex CR-2026-11-15-GATE-FIXES
    H35, Pass 14: a fetch that succeeds proves only that the guessed posting exists)."""
    from careeros.discovery import board_discovery as bd
    job = ctx.store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    own = bd.posting_identity(link.url)
    return job is not None and board_url == link.url and own is not None and own == bd.job_identity(job)


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


def _ledger_lead(ctx: Context, link, title_norm: str) -> bool:
    """An earlier lead for the same posting reached through another link: by the owner's D188 (2026-11-19, "Exact
    links only"), only when this line's link and the earlier lead's both name one posting, and it is the same posting
    (board_discovery.posting_identity: "/jobs/view/123" and "/jobs/search?currentJobId=123" are one LinkedIn job). The
    same company and title, a requisition, a place or a shared careers page never make a line "seen before": it is
    stored as its own lead (the red team's fr1-identity-1). A link kept for review never counts (D127 B3). An agency's
    job number never makes a line seen: it is a possible repeat on the lead (_agency_job, _agency_repeat: Codex's D190
    design review H8, part 3 of D192). `title_norm` is no longer read."""
    from careeros.discovery import board_discovery as bd
    from careeros.discovery.alerts import kept_for_review
    if kept_for_review(link.url):
        return False
    mine = bd.posting_identity(link.url)
    if mine is not None:
        for r in ctx.store.all("SELECT lead_id, url FROM leads WHERE url IS NOT NULL AND url != ? ORDER BY first_seen_at, lead_id",
                               (link.url,)):
            if not kept_for_review(r["url"]) and bd.posting_identity(r["url"]) == mine:
                return True
    return False


def _agency_job(ctx: Context, link, own: str | None = None) -> str | None:
    """The agency card an ordinary line naming an agency and that agency's own job number may repeat: the earliest agency
    card with that agency and number (CR-AD-03), or None. Advice only: the line is stored as its own lead, with this
    possible repeat beside it, in either arrival order (Codex's D190 design review H8; part 3 of the owner's D192 build).
    Only an agency card counts: two other lines with one requisition are never one job (D188)."""
    from careeros.discovery.alerts import is_agency_lead, local_only_urls
    from careeros.discovery.board_discovery import lead_requisition
    from careeros.discovery.note import requisition
    from careeros.discovery.seen_index import norm_req
    req, company = norm_req(lead_requisition(link.context)), _company(link.context)
    if not req or not company:
        return None
    local = local_only_urls(ctx.store)
    for r in ctx.store.all("SELECT lead_id, url, context FROM leads WHERE context LIKE ? ORDER BY first_seen_at, lead_id",
                           ("%req=%",)):
        if r["lead_id"] != own and is_agency_lead(r, local) and _company(r["context"]) == company \
                and norm_req(requisition(r["context"])) == req:
            return r["lead_id"]
    return None


def _agency_repeat(ctx: Context, link, own: str | None = None) -> str | None:
    """The earlier lead an agency digest card's job number repeats: an earlier card of the same agency (a later digest
    can give the same job a new personal link), or another site's lead naming the same agency and job number, the
    earliest first (CR-AD-03), or None. Advice only: the card is stored as its own open lead with this possible repeat
    beside it, never "seen" and never a closed twin (Codex's D190 design review H8; part 3 of the owner's D192 build).
    Without a job number the exact link is the card's only identity; cards are never merged on their title."""
    from careeros.discovery.note import requisition
    from careeros.discovery.seen_index import norm_req
    req = norm_req(requisition(link.context))
    if not req:
        return None
    company = _company(link.context)
    for r in ctx.store.all("SELECT lead_id, site, context FROM leads WHERE context LIKE ? ORDER BY first_seen_at, lead_id",
                           ("%req=%",)):
        if r["lead_id"] != own and norm_req(requisition(r["context"])) == req and \
                (r["site"] == link.site or (company and _company(r["context"]) == company)):
            return r["lead_id"]
    return None


def _record_notice(ctx: Context, alert, link, position: int, title_norm: str, family: str | None, profile: str,
                   cache: dict) -> str:
    """A card whose own link names no posting, kept as a notice (alert_notices), never a lead: one row per card
    occurrence (its email's key and its position), so reading the same email again adds nothing and no other card hides
    it. The employer the card names is resolved for grouping (advice); a careers page's Greenhouse id is a suggestion on
    it, never fetched (D190 rule 3; the D190 design, revision 3, section 5; part 3 of the owner's D192 build). Returns
    "notice" or "notice_seen_before"."""
    from careeros.discovery.alerts import greenhouse_candidate
    store = ctx.store
    # an alert built without keys (a caller outside the email readers): its position and the line itself
    key = link.occurrence or (f"{getattr(alert, 'message_key', '') or _alert_key(alert)}#{position}."
                              + text_fingerprint(f"{link.url}\n{link.text}\n{link.context}")[:12])
    message_key, _, place = key.rpartition("#")
    if store.one("SELECT 1 FROM alert_notices WHERE notice_id = ?", (key,)):
        return "notice_seen_before"
    employer = _company_name(link.context)
    guess = None if "#" in (posting_link(link.url) or "") else greenhouse_candidate(link.url)
    with store.tx():
        store.writer("discovery").insert("alert_notices", {
            "notice_id": key, "message_key": message_key, "position": place, "url": link.url, "site": link.site,
            "title": link.text, "title_norm": title_norm, "family": family, "context": link.context or None,
            "sender": alert.sender or None, "subject": alert.subject or None, "received_at": alert.received_at,
            "suggested_employer": employer, "employer_key": _employer_key(ctx, employer, cache),
            "suggestion_json": json.dumps({"greenhouse_posting": guess} if guess else {}, sort_keys=True),
            "profile": profile, "first_seen_at": store.clock.now(), "state": "new"})
    return "notice"


def _alert_key(alert) -> str:
    """A key for an alert built with none (a caller outside the email readers): its sender, subject and time."""
    return "alert:" + text_fingerprint(f"{alert.sender}\n{alert.subject}\n{alert.received_at}")[:24]


def _employer_key(ctx: Context, name: str | None, cache: dict) -> str | None:
    """The employer a card names, when it resolves to one (employer_identity), for grouping notices: advice only."""
    if not name:
        return None
    if "inputs" not in cache:
        from careeros.review.submission import _identity_inputs
        cache["inputs"] = _identity_inputs(ctx.store, ctx.policy)
    from careeros.normalize.employer_identity import RESOLVED, resolve
    found = resolve(name, None, ctx.policy.get("parents", default={}) or {}, **cache["inputs"])
    return found.key if found.status == RESOLVED else None


def _known_job(ctx: Context, url: str, title: str | None = None, context: str | None = None) -> str | None:
    """The tracked job a link is, by the owner's D188 (2026-11-19, "Exact links only"): only when the link names one
    posting (a board's posting link, a Workday requisition link, a job site's id) and exactly one tracked job is that
    posting (board_discovery.job_identity: a board-scanned job by its own board id). A careers page, or a careers-page
    link carrying a Greenhouse id, names no tracked job: its lead stays open for you. Never called for a link kept for
    review (D140 F2), and never a job tracked at a review link (the owner's D142 R-b). `title` and `context` are no
    longer read: what a line says never makes it a tracked job."""
    from careeros.discovery import board_discovery as bd
    from careeros.discovery.alerts import kept_for_review
    found = set()
    identity = bd.posting_identity(url)
    for row in bd.jobs_with_identity(ctx.store, identity):
        if row["current_state"] == "duplicate" and row["duplicate_of"]:
            # a duplicate is its original's posting (D188) only when the original is that posting: one an older engine
            # stored by requisition or text is the line's own job until the next processing run re-derives it (the
            # red team's hr1-alerts-3)
            original = ctx.store.one("SELECT * FROM jobs WHERE job_id = ?", (row["duplicate_of"],))
            if original is not None and bd.job_identity(original) == identity:
                row = original
        if not any(u and kept_for_review(u) for u in (row["url"], row["canonical_url"])):
            found.add(row["job_id"])
    return found.pop() if len(found) == 1 else None


def _gmail_pull(ctx: Context, client: HttpClient, query: str | None = None, credentials=None, gmail=None) -> Counter:
    """New job-alert emails from Gmail since the last pull, and every held-back agency digest again by its id until it is
    read in full. Raises GmailError (e.g. reconnect_needed). The whole pull uses one reading of the private settings
    (D141)."""
    from careeros.core import private_sites
    with private_sites.run():
        try:
            return _pull_gmail(ctx, client, query, credentials, gmail)
        except Exception as exc:
            if getattr(exc, "code", None) != "not_connected":   # stopped part-way, whatever the reason (a settings
                _note_stop(ctx.store)   # stop, Gmail asking you to reconnect, a network failure): the next pull looks back to
            raise                       # here (L4-02, D141, the red team before Pass 4, Codex Pass 4)


_ACCESS: ContextVar[str] = ContextVar("careeros_gmail_access", default="")   # the reader of the pull (_held_key)


def _pull_gmail(ctx: Context, client: HttpClient, query: str | None, credentials, gmail) -> Counter:
    from careeros.core import private_sites
    from careeros.discovery.gmail import MAX_MESSAGES, GmailError
    query = query or ctx.policy.get("operations", "gmail_query")
    if gmail is None:
        from careeros.core.credentials import WindowsCredentials
        gmail = _gmail_reader(credentials or WindowsCredentials())
        if gmail is None:
            raise GmailError("not_connected", "Gmail is not set up (see GMAIL_SETUP.md)")
    counts, writer = Counter(), ctx.store.writer("discovery")
    access = None
    try:
        access = _ACCESS.set(_access_of(gmail))
        try:
            _alert_config_check(ctx.store)
        except ValueError as exc:  # a broken private-sites file or a missing agency entry: stop before any message is read
            raise GmailError(_setting_error_code(exc), f"alert emails not read: {exc}") from exc
        caught_up = _stopped_days(ctx.store)
        limit, blocked = MAX_MESSAGES, False
        if caught_up:   # the days missed while intake was stopped are read too (L4-02, CR-AD-V2-10)
            window = re.search(r"\bnewer_than:(\d+)([dmy])\b", query, re.I)
            if (re.search(r"\b(?:after|before|older|newer|older_than):", query, re.I)
                    or len(re.findall(r"\bnewer_than:", query, re.I)) > 1 or re.search(r"-\s*newer_than:", query, re.I)):
                blocked = True   # a date of the search's own may leave the missed days out: reported, the stop kept (Pass 3)
            elif window is None:
                limit = CATCH_UP_LIMIT   # no window: every listed email, up to the catch-up's own cap
            elif int(window.group(1)) * {"d": 1, "m": 28, "y": 365}[window.group(2).lower()] < caught_up:
                query = query[:window.start()] + f"newer_than:{caught_up}d" + query[window.end():]
                limit = CATCH_UP_LIMIT
        if blocked:
            _once(ctx, "gmail_catch_up_blocked", f"Alert intake was stopped {caught_up} days ago, but the Gmail search "
                  "(operations.gmail_query) has a date of its own (after:, before:, older:, older_than:, or a second or "
                  "negated newer_than:), so the days "
                  "missed may not be read. Use newer_than: in the search instead, or save the missed alert emails from "
                  "Gmail (Download message) and run careeros import. The catch-up waits until a search can cover them.")
            counts["gmail_catch_up_blocked"] += 1
        found = list(gmail.search(query) if limit == MAX_MESSAGES else gmail.search(query, limit))   # the readers' own cap: 300
        truncated = getattr(gmail, "truncated", False) is True
        held = [r["entity_id"] for r in ctx.store.all(
            "SELECT entity_id FROM exceptions WHERE error_code = ? AND entity_type = 'gmail_message' AND status != 'resolved' "
            "GROUP BY entity_id ORDER BY MIN(occurred_at)", (HELD_DIGEST,))]
        # an alert with a line read but not stored: read again by its id, whatever the window, as an ordinary alert (the
        # red team after Codex Pass 12, RT4-6); never a held digest
        unstored = [r["entity_id"] for r in ctx.store.all(
            "SELECT entity_id FROM exceptions WHERE error_code = ? AND entity_type = 'gmail_message' AND status != 'resolved' "
            "GROUP BY entity_id ORDER BY MIN(occurred_at)", (UNSTORED_LINE,))]
        if getattr(gmail, "all_mail", True) is False:
            _once(ctx, "imap_inbox_only", "Gmail's All Mail is not shown over IMAP, so only the inbox is read: alert emails "
                  "archived or filed under a label are missed, and a held digest outside the inbox cannot be read again. Turn "
                  "on 'Show in IMAP' for All Mail in Gmail's settings (Labels).")
        if getattr(gmail, "unserved", 0):
            _once(ctx, "gmail_alert_unserved", "Gmail could not serve one or more listed alert emails; the others were read. "
                  "Open Gmail to see whether one of them matters, and save it (Download message) for careeros import.")
        if getattr(gmail, "oversized", 0):
            _once(ctx, "gmail_alert_too_large", "An alert email larger than 5 MB was not read. Save it from Gmail (Download "
                  "message) and run careeros import if it matters.")
        try:
            held_key = _held_key()
        except ValueError as exc:
            raise GmailError(_setting_error_code(exc), f"alert emails not read: {exc}") from exc
        tried = {r["key"][len(HELD_TRY):]: r["value"] for r in ctx.store.all("SELECT key, value FROM meta WHERE key LIKE ?",
                                                                             (HELD_TRY + "%",))}
        for message_id in dict.fromkeys(found + held + unstored):   # every held one by its id, whatever the window
            if ctx.store.one("SELECT 1 FROM alert_messages WHERE message_id = ?", (message_id,)):
                continue   # read before; a hold is settled only by a full read or a dismiss, never by a receipt alone
            if message_id in held and tried.get(message_id) == held_key:   # Gmail never changes an email: only a change
                counts["gmail_held_unchanged"] += 1                          # of the settings or the reader can matter
                continue
            try:
                # one reading of the settings for the email, from its check through its fetch-only step (no
                # transaction spans a network call: the D190 design, section 5) to its commit (D141)
                with private_sites.pinned():
                    try:   # checked again for each message: the settings file can change during a pull (L4-01)
                        _alert_config_check(ctx.store, enroll=False)
                    except ValueError as exc:
                        raise GmailError(_setting_error_code(exc), f"alert emails not read: {exc}") from exc
                    fetched = _gmail_prefetch(ctx, gmail, message_id, client)
                    with ctx.store.tx():   # the whole email in one transaction, committed only under unchanged settings
                        links = _gmail_message(ctx, gmail, message_id, client, counts, retry=message_id not in found,
                                               held=message_id in held, fetched=fetched)
                        if links is not None:   # None: held for a retry, or gone: no receipt. Inside the check (Pass 3)
                            writer.insert("alert_messages", {"message_id": message_id, "source": "gmail",
                                                              "processed_at": ctx.store.clock.now(), "links": links})
                        private_sites.check()   # the reading still in force, just before the commit (S2-02)
                if links is not None:
                    counts["gmail_messages"] += 1
            except private_sites.SettingsChanged as exc:   # nothing of the email is committed: read again next time
                raise GmailError("private_sites_changed", f"alert emails not read: {exc}") from exc
            except BatchAborted as exc:   # rolled back: the job it would have written is held, and the pull stops
                hold_aborted(ctx.store, exc)
                raise
            except ValueError as exc:   # the settings file unreadable when the email's reading began (S2-03)
                if not _setting_refusal(exc):
                    raise
                raise GmailError(_setting_error_code(exc), f"alert emails not read: {exc}") from exc
        if truncated:
            _search_cut_off(ctx, limit)
            counts["gmail_search_cut_off"] += 1
        if caught_up and not blocked and not counts["gmail_message_fetch_failed"]:   # every listed email was read (any
            # beyond the cap is reported; one Gmail would not serve keeps the catch-up waiting): the usual window again
            ctx.store.writer("admin").update("meta", {"value": ""}, "key = ?", (STOPPED_SINCE,))
    finally:
        if access is not None:
            _ACCESS.reset(access)
        gmail.close()
    return counts


CATCH_UP_LIMIT = 10_000   # alert emails the catch-up search after a stop may list (L4-02, CR-AD-V2-10)


def _gmail_prefetch(ctx: Context, gmail, message_id: str, client: HttpClient) -> dict:
    """The fetch-only step for one Gmail email (the D190 design, section 5): the email itself and its lines' board
    postings, read before its transaction, which spans no network call. A failure to fetch the email is kept and
    handled inside, as before."""
    from careeros.discovery.alerts import parse_eml_bytes
    try:
        raw = gmail.raw(message_id)
    except Exception as exc:   # noqa: BLE001 - handled in _gmail_message, as a failed fetch always was
        return {"error": exc}
    try:
        alert = parse_eml_bytes(raw, message_key=f"gmail:{message_id}")
    except Exception:   # noqa: BLE001 - reported by the email's own pass
        return {"raw": raw}
    return {"raw": raw, "prefetched": _prefetch_alert(ctx, alert, client)}


def _gmail_message(ctx: Context, gmail, message_id: str, client: HttpClient, counts: Counter, *, retry: bool,
                   held: bool = False, fetched: dict | None = None) -> int | None:
    """One Gmail alert email read and recorded. The number of its links for its receipt, or None when it gets none: a
    held agency digest (read again after a change), or a held one that is no longer in Gmail. `retry`: a held digest
    fetched by its id rather than found by the search; `held`: one that is held now. A failure to fetch stops the pull
    (no receipt, the hold as it was), except a held digest's own failure, which leaves it held and the pull going on
    (Codex Pass 3); a held digest is settled only by a full read."""
    from careeros.discovery.alerts import AlertEmail, AlertEmailError, parse_eml_bytes, raw_mentions_agency
    from careeros.discovery.gmail import GmailError
    writer = ctx.store.writer("discovery")
    try:
        if fetched is None:
            raw = gmail.raw(message_id)
        elif fetched.get("error") is not None:
            raise fetched["error"]
        else:
            raw = fetched["raw"]
    except GmailError as exc:
        if retry and exc.code in ("http_404", "message_gone") and _held_elsewhere(ctx, message_id):
            counts["gmail_held_not_reachable"] += 1   # held while another Gmail account was read: not gone, only not in
            _note_held_try(ctx, message_id)           # this one; read again once the reader changes (the red team)
            return None
        if retry and exc.code in ("http_404", "message_gone"):   # a held digest deleted from Gmail since
            for row in ctx.store.all("SELECT exception_id, error_code, metadata_json FROM exceptions WHERE error_code IN "
                                     "(?, ?) AND entity_type = 'gmail_message' AND entity_id = ? AND status != 'resolved'",
                                     (HELD_DIGEST, UNSTORED_LINE, message_id)):
                writer.resolve_exception(row["exception_id"], "the held email is no longer in Gmail")
                if row["error_code"] == UNSTORED_LINE:   # its kept leads: nothing retries them now (BATCH_5 E2)
                    counts["leads_no_longer_retried"] += _close_retry_leads(ctx, row["metadata_json"])
            _note_held_try(ctx, message_id, "")
            counts["gmail_held_gone"] += 1
            return None
        if retry and exc.code == "message_not_reachable":   # not in the only mailbox IMAP could open: stays held (FU-02)
            counts["gmail_held_not_reachable"] += 1
            _note_held_try(ctx, message_id)
            return None
        if _one_message(exc):   # an email Gmail will not serve: no receipt, the others read (Codex Pass 3, the red team)
            return _held_fetch_failed(ctx, counts) if retry or held else _listed_fetch_failed(ctx, counts)
        raise  # Gmail itself is down: stop, report, and let the rest of the daily run carry on
    except Exception as exc:   # a network, protocol or decoding failure while fetching: stop, never a receipt (I3-01)
        if _one_message(exc):
            return _held_fetch_failed(ctx, counts) if retry or held else _listed_fetch_failed(ctx, counts)
        raise GmailError("fetch_failed", f"an alert email could not be fetched ({type(exc).__name__}); the pull stopped "
                                         "and reads it next time") from exc
    try:
        alert = parse_eml_bytes(raw, message_key=f"gmail:{message_id}")   # its cards keyed by the message (part 3)
    except Exception as exc:  # noqa: BLE001 - one odd message never stops the others
        if held or raw_mentions_agency(raw):   # an agency's mail that cannot be read: held (L1-06)
            counts["gmail_digests_deferred"] += 1
            _hold_digest(ctx, "gmail_message", message_id,
                         AlertEmail("", "", None, [], digest_status="partial", digest_reason="unreadable"), Counter())
            return None
        detail = exc if isinstance(exc, (AlertEmailError, UnicodeError, LookupError)) else type(exc).__name__
        writer.exception("validation", "low", f"Gmail alert not readable: {detail}", error_code="alert_email_unreadable")
        counts["failed"] += 1
        return 0
    if held and (alert.digest_status is None or _skip_as_correspondence(alert)):
        # a held digest now read as correspondence or as no digest at all stays held (FU-03): only a full read or a
        # dismiss settles it
        counts["gmail_digests_deferred"] += 1
        _note_held_try(ctx, message_id)
        return None
    if _skip_as_correspondence(alert):  # e.g. an application update (L1-01, CR-AD-V2-11)
        counts["correspondence_skipped"] += 1
        return 0
    outcomes: list = []
    result = _record_alert(ctx, alert, client, outcomes, prefetched=(fetched or {}).get("prefetched") if fetched else None)
    counts.update(result)
    if alert.digest_status and (alert.digest_status == "partial" or result["lead_errors"]):
        counts["gmail_digests_deferred"] += 1
        _hold_digest(ctx, "gmail_message", message_id, alert, result)
        return None  # a partial or unreadable digest must remain eligible for a retry
    if alert.digest_status:
        _release_digest(ctx, "gmail_message", message_id)   # read in full: its hold, if any, is settled
    if result["lead_errors"] or result["lead_store_failed"] or result["lead_fetch_retry"]:
        counts["gmail_lines_retried"] += 1
        from careeros.discovery.alerts import lead_id
        _hold_unstored(ctx, message_id, [lead_id(link.url) for link, outcome in outcomes if outcome == "fetch_retry"])
        return None   # no receipt: the next pull reads it again, its other links seen before (the red team, Pass 11)
    _settle_unstored(ctx, message_id)
    return len(alert.links)


def _one_message(exc: Exception) -> bool:
    """Whether a failure to fetch an email is about that email alone (an id or a message Gmail refuses, or bytes that do
    not decode), not about Gmail itself or the connection: only then does a pull go on with the other held digests
    (Codex Pass 3). Anything else stops the pull, as before (I3-01)."""
    import imaplib
    from careeros.discovery.gmail import GmailError
    if isinstance(exc, GmailError):
        return exc.code in ("bad_message_id", "imap_fetch_failed") or (
            exc.code.startswith("http_4") and exc.code not in ("http_401", "http_429"))
    return not isinstance(exc, (OSError, EOFError, httpx.HTTPError, imaplib.IMAP4.error))


def _held_fetch_failed(ctx: Context, counts: Counter) -> int | None:
    """A held digest Gmail would not serve this time: it stays held, with no receipt (None) and no note of this reading,
    so the next pull tries it again; the problem is reported once while open."""
    counts["gmail_held_fetch_failed"] += 1
    _once(ctx, "gmail_held_fetch_failed", "A held agency digest could not be fetched from Gmail; the other held digests "
          "were read. It is tried again on every pull until it is read in full or dismissed (careeros agency-held).")
    return None


def _listed_fetch_failed(ctx: Context, counts: Counter) -> int | None:
    """An alert email the search lists that Gmail would not serve on its own (_one_message): no receipt, so every pull
    tries it again while the search lists it, the catch-up waits, and the problem is reported once while open; the other
    emails are read (the red team before Pass 4)."""
    counts["gmail_message_fetch_failed"] += 1
    _note_stop(ctx.store)   # the catch-up armed: later pulls look back to here until it is read (D153 R9, Codex Pass 5)
    _once(ctx, "gmail_message_fetch_failed", "An alert email Gmail would not serve was not read; the others were. It is "
          "tried again on every pull while the search lists it. Open Gmail to see whether it matters, and save it "
          "(Download message) for careeros import.")
    return None


def _access_of(gmail) -> str:
    """How Gmail is read in this pull, for the held key: the reader, whether IMAP shows All Mail (Codex Pass 3), and
    which account, as a hash of its address, never the address itself (the red team before Pass 4)."""
    from careeros.discovery.gmail import GmailError
    try:
        address = str(gmail.address() or "") if hasattr(gmail, "address") else ""
    except GmailError:
        address = ""
    account = hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()[:12] if address.strip() else ""
    return f"{type(gmail).__name__}:{'all' if getattr(gmail, 'all_mail', True) is not False else 'inbox'}:{account}"


def _held_elsewhere(ctx: Context, message_id: str) -> bool:
    """Whether a held Gmail digest was held while another Gmail account was read, or one CareerOS could not name: "not
    found" then means not in this account, never deleted (the red team before Pass 4)."""
    current = _ACCESS.get().rsplit(":", 1)[-1]
    for row in ctx.store.all("SELECT metadata_json FROM exceptions WHERE error_code = ? AND entity_type = 'gmail_message' "
                             "AND entity_id = ? AND status != 'resolved'", (HELD_DIGEST, message_id)):
        if json.loads(row["metadata_json"] or "{}").get("account", "") != current:
            return True
    return False


def _once(ctx: Context, code: str, message: str) -> None:
    """An open problem with this code, recorded once while open."""
    if not ctx.store.one("SELECT 1 FROM exceptions WHERE error_code = ? AND status != 'resolved'", (code,)):
        ctx.store.writer("discovery").exception("integration", "medium", message, error_code=code, retryable=False)


def _search_cut_off(ctx: Context, limit: int) -> None:
    """An open problem (once while open) when the Gmail search listed more alert emails than it may read in one pull."""
    code = "gmail_search_cut_off"
    if ctx.store.one("SELECT 1 FROM exceptions WHERE error_code = ? AND status != 'resolved'", (code,)):
        return
    ctx.store.writer("discovery").exception(
        "integration", "medium",
        f"The Gmail search listed more than {limit} alert emails, so the oldest of them were not read. Save the ones you "
        "need from Gmail (Download message) into the vault inbox and run careeros import.", error_code=code, retryable=False)


def _inbox_pull(ctx: Context, client: HttpClient, repo: str, runner=None) -> Counter:
    """Open issues in the private recommendations repo: grade or list each job, reply on the issue, close it. The whole
    pull uses one reading of the private settings (D141)."""
    from careeros.core import private_sites
    with private_sites.run():
        return _pull_inbox(ctx, client, repo, runner)


def _pull_inbox(ctx: Context, client: HttpClient, repo: str, runner) -> Counter:
    import subprocess
    from careeros.core import private_sites
    from careeros.discovery import github_inbox as inbox
    from careeros.qualify import night
    runner = runner or subprocess.run
    counts, writer = Counter(), ctx.store.writer("discovery")
    for row in ctx.store.all("SELECT number, reply FROM inbox_issues WHERE repo = ? AND closed = 0", (repo,)):
        try:  # read earlier but not closed (e.g. GitHub was unreachable): finish the reply now
            private_sites.check()   # nothing is sent once the settings changed during the run (D141)
            inbox.close_with_reply(repo, row["number"], row["reply"], runner, before_write=private_sites.check)
            writer.update("inbox_issues", {"closed": 1}, "repo = ? AND number = ?", (repo, row["number"]))
        except inbox.InboxError:
            counts["inbox_close_failed"] += 1
        except private_sites.SettingsChanged as exc:
            return _inbox_settings_changed(ctx, exc, counts)
    try:
        _alert_config_check(ctx.store)
    except ValueError as exc:  # a broken private-sites file or a missing agency entry: every issue stays open (84a47b1, D125)
        writer.exception("validation", "medium", f"inbox issues not read: {exc}", error_code=_setting_error_code(exc))
        print(f"  ! inbox: {exc} (issues left open; fix the file and they are read on the next run)")
        counts["inbox_config_error"] += 1
        return counts
    for issue in inbox.open_issues(repo, runner):
        if ctx.store.one("SELECT 1 FROM inbox_issues WHERE repo = ? AND number = ?", (repo, issue["number"])):
            continue
        profile = inbox.issue_profile(issue)
        if profile == "night" and not night.enabled(ctx.policy):
            # Never graded with PRIMARY rules: left open and untouched until the NIGHT profile is switched on.
            counts["inbox_night_waiting"] += 1
            print(f"  issue #{issue['number']} is a NIGHT issue: left open (NIGHT profile is off)")
            continue
        try:  # one unreadable issue never stops the others; it stays open and is retried next run
            alert, outcomes = inbox.issue_alert(issue), []
            with private_sites.pinned():   # one reading, from the check through the fetch-only step to the commit (D141)
                _alert_config_check(ctx.store, enroll=False)
                prefetched = _prefetch_alert(ctx, alert, client, profile=profile)   # before its transaction (section 5)
                with ctx.store.tx():   # committed only under unchanged settings (S2-02)
                    result = _record_alert(ctx, alert, client, outcomes, profile=profile, prefetched=prefetched)
                    counts.update(result)
                    if result["lead_errors"] or result["lead_store_failed"] or result["lead_fetch_retry"]:
                        # a line read but not stored: the issue stays open and is read again on the next pull, as an
                        # email stays in failed (the red team after Codex Pass 12, RT4-5)
                        counts["inbox_lines_retried"] += 1
                        private_sites.check()   # just before the commit
                        continue
                    gmail = inbox.gmail_status(issue.get("body") or "")
                    reply = inbox.reply_text(ctx.store.clock.now()[:10], outcomes, gmail)
                    writer.insert("inbox_issues", {"repo": repo, "number": issue["number"], "processed_at": ctx.store.clock.now(),
                                                   "links": len(alert.links), "reply": reply, "closed": 0,
                                                   "gmail_status": gmail, "profile": profile})
                    private_sites.check()   # the reading still in force, just before the commit
        except ValueError as exc:   # the settings went missing or changed: every issue left stays open (CR-AD-V2-07)
            if not str(exc).startswith(("agency_setting_missing", "private_sites_")):
                writer.exception("data_quality", "medium", f"inbox issue #{issue['number']} not processed: {type(exc).__name__}: "
                                 f"{exc}", error_code="inbox_issue_failed")
                counts["inbox_issue_errors"] += 1
                continue
            if _setting_error_code(exc) == "agency_setting_missing":
                _note_stop(ctx.store)
            writer.exception("validation", "medium", f"inbox issues not read: {exc}", error_code=_setting_error_code(exc))
            print(f"  ! inbox: {exc} (issues left open; they are read on the next run)")
            counts["inbox_config_error"] += 1
            return counts
        except Exception as exc:  # noqa: BLE001 - recorded for you to see in status
            writer.exception("data_quality", "medium", f"inbox issue #{issue['number']} not processed: {type(exc).__name__}: {exc}",
                             error_code="inbox_issue_failed")
            counts["inbox_issue_errors"] += 1
            print(f"  ! issue #{issue['number']} could not be processed: {type(exc).__name__} (left open, retried next run)")
            continue
        counts["inbox_issues"] += 1
        print(f"  issue #{issue['number']}: {len(alert.links)} links")
        try:
            private_sites.check()   # no reply is sent once the settings changed during the run: the next run sends it (D141)
            inbox.close_with_reply(repo, issue["number"], reply, runner, before_write=private_sites.check)
            writer.update("inbox_issues", {"closed": 1}, "repo = ? AND number = ?", (repo, issue["number"]))
        except inbox.InboxError as exc:
            counts["inbox_close_failed"] += 1
            print(f"    ! could not reply on issue #{issue['number']}: {exc} (will retry next run)")
        except private_sites.SettingsChanged as exc:
            return _inbox_settings_changed(ctx, exc, counts)
    return counts


def _inbox_settings_changed(ctx: Context, exc: Exception, counts: Counter) -> Counter:
    """The private settings changed during an inbox pull: nothing more is read or sent; the next run does it (D141)."""
    ctx.store.writer("discovery").exception("validation", "medium", f"inbox issues not read: {exc}",
                                            error_code="private_sites_changed")
    print(f"  ! inbox: {exc} (issues left open; they are read on the next run)")
    counts["inbox_config_error"] += 1
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
    _halt_if_changed(ctx.store, "inbox stopped before processing; the next run does it")   # D153 R2
    counts.update(process(ctx))
    _halt_if_changed(ctx.store, "inbox stopped before the seen-jobs index; the next run does it")
    counts.update(_publish_index(ctx, paths))
    _halt_if_changed(ctx.store, "inbox stopped before its report; the next run does it")
    cmd_report(ctx, paths, args)


def _publish_index(ctx: Context, paths, runner=None) -> Counter:
    """The seen-jobs index and Gmail intake status, rebuilt from the local ledger and published to the inbox repo
    (prompts v2.5). A local copy is always written to vault\\reports. Publishing problems are reported, never fatal. Built
    under the run's one reading of the private settings; nothing is written or published once the file differs from it
    (Codex Pass 3, D141)."""
    from careeros.core import private_sites
    with private_sites.run():
        return _publish_seen_index(ctx, paths, runner)


def _publish_seen_index(ctx: Context, paths, runner) -> Counter:
    import subprocess
    from careeros.core import private_sites
    from careeros.discovery import github_inbox as inbox
    from careeros.discovery import seen_index
    repo = ctx.policy.get("operations", "github_inbox")
    try:
        records = seen_index.build(ctx.store)
        files = {**seen_index.files(records, int(ctx.policy.get("operations", "seen_index_max_per_file", default=1500))),
                 **seen_index.intake_file(ctx.store)}
        private_sites.check()   # the settings the index was built under, or no copy is written (D141)
        local = paths["reports"] / "inbox-index"
        _replace_folder(local, files, private_sites.check)
        from careeros.demo.sandbox import enabled as demo_mode
        if not repo or demo_mode() or not ctx.policy.get("operations", "publish_seen_index", default=True):
            return Counter(index_records=len(records))  # Demo Mode keeps the local copy only, never publishes
        changed = inbox.publish_files(repo, files, "CareerOS: update seen-jobs index and intake status", runner or subprocess.run,
                                      before_write=private_sites.check)   # checked again before each file is sent
        print(f"Seen-jobs index: {len(records)} jobs; " + (f"updated {', '.join(changed)}" if changed else "unchanged"))
        return Counter(index_records=len(records), index_files_changed=len(changed))
    except Exception as exc:  # noqa: BLE001 - GitHub down or a refused record: the daily run carries on
        code = _setting_error_code(exc) if _setting_refusal(exc) else getattr(exc, "code", "index_publish_failed")
        ctx.store.writer("discovery").exception("integration", "medium", f"seen-jobs index not published: {exc}",
                                                error_code=code)
        print(f"  ! seen-jobs index not published: {exc}")
        return Counter(index_errors=1)


def _replace_folder(folder: Path, files: dict[str, str], check) -> None:
    """The seen-jobs index's local copy, written whole into a staging folder beside it, then `check` (the run's
    settings still as it read them, D141), then swapped in: a refusal leaves the earlier copy as it was, and no file of
    the new one is ever half-written in its place (Codex Pass 4). The earlier copy is kept until the new one is in
    place: a swap stopped half-way puts it back, and so does the next run before anything else (D153 R3)."""
    import shutil
    staging, old = folder.with_name(folder.name + ".writing"), folder.with_name(folder.name + ".old")
    if old.exists() and not folder.exists():   # an earlier swap stopped between its renames: its copy goes back first
        old.rename(folder)
    for leftover in (staging, old):   # the folder is whole here: what is beside it is never the only copy
        if leftover.exists():
            shutil.rmtree(leftover)
    for name, content in files.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    try:
        check()
    except Exception:
        shutil.rmtree(staging)
        raise
    if folder.exists():
        folder.rename(old)
    try:
        staging.rename(folder)
    except OSError:
        if old.exists() and not folder.exists():
            old.rename(folder)   # the earlier copy back in its place (D153 R3)
        raise
    if old.exists():
        shutil.rmtree(old)


def cmd_agency_held(ctx: Context, paths, args) -> None:
    """The agency digests held back for you (read only in part), oldest first. --dismiss ID closes one you have looked
    at: its problem is resolved and a Gmail one is marked read, so it is never fetched again. The id is the exception id
    this lists (a unique beginning of it is enough)."""
    store = ctx.store
    rows = store.all("SELECT exception_id, occurred_at, entity_type, entity_id, message FROM exceptions WHERE error_code = ? "
                     "AND status != 'resolved' ORDER BY occurred_at, exception_id", (HELD_DIGEST,))
    if args.dismiss:
        chosen = [r for r in rows if r["exception_id"].startswith(args.dismiss.strip())]
        if len(chosen) != 1:
            sys.exit(f"{len(chosen)} held digests match {args.dismiss!r}: give one id from careeros agency-held.")
        row = chosen[0]
        store.writer("discovery").resolve_exception(row["exception_id"], "dismissed by you after a look at the email")
        if row["entity_type"] == "gmail_message":
            if not store.one("SELECT 1 FROM alert_messages WHERE message_id = ?", (row["entity_id"],)):
                store.writer("discovery").insert("alert_messages", {"message_id": row["entity_id"], "source": "gmail",
                                                                    "processed_at": store.clock.now(), "links": 0})
            _note_held_try(ctx, row["entity_id"], "")
        print(f"{row['exception_id']} dismissed.")
        return
    if not rows:
        print("No agency digest is held back.")
    for r in rows:
        where = "Gmail" if r["entity_type"] == "gmail_message" else "saved email"
        print(f"{r['exception_id']}  {r['occurred_at'][:10]}  {where}: {r['message']}")


def cmd_agency_hosts(ctx: Context, paths, args) -> None:
    """The agency tracking hosts CareerOS has been set up with (D125): whether each is in the private settings now and how
    many stored agency leads use it, and how many stored agency links carry a host CareerOS cannot read (kept local,
    D153 R5). --forget is turned off: a host once set up stays private for good (D153 R6)."""
    from careeros.discovery.alerts import AGENCY_LEAD, _tracker_hosts, enrolled_hosts, lead_tracker_hosts
    store = ctx.store
    configured = _tracker_hosts()
    known = frozenset(configured | enrolled_hosts(store))
    rows = store.all("SELECT url FROM leads WHERE lead_id LIKE ?", (AGENCY_LEAD + "%",))
    used: Counter = Counter(host for r in rows for host in lead_tracker_hosts(r["url"], known))
    unreadable = sum(1 for r in rows if not lead_tracker_hosts(r["url"], known))
    if args.forget:   # turned off (D153 R6, Codex Pass 5 CR-AD-V2-09): an enrolled tracking host stays private for good
        sys.exit("careeros agency-hosts --forget is turned off: a tracking host CareerOS has been set up with stays private "
                 "for good, so no link stored with it can ever leave this machine. Keep it in the agency's entry "
                 "(tracker_host or former_hosts).")
    hosts = sorted(enrolled_hosts(store) | {h for h in used if h})
    if not hosts:
        print("No agency tracking host has been set up.")
    for host in hosts:
        state = "in the settings" if host in configured else "MISSING from the settings"
        print(f"{host}: {state}; {used[host]} stored agency lead(s)")
    if unreadable:
        print(f"{unreadable} stored agency link(s) carry a host CareerOS cannot read as a browser does: they stay local.")


def cmd_agency_layout(ctx: Context, paths, args) -> None:
    """Saved agency job-alert digests (.eml) as the reader sees them: counts and the layout fingerprint only, never a job
    title, text or link (D125). Put the fingerprint in the agency's private_sites entry as `layout:`; a digest whose
    layout differs from it is held for you, never guessed at."""
    from careeros.discovery.alerts import MAX_EML_BYTES, agency_layout_report
    seen: set = set()
    reports = job_like = 0
    for name in args.files:
        path = Path(name)
        if not path.is_file() or path.stat().st_size > MAX_EML_BYTES:
            print(f"{path.name}: not a readable saved email (missing, or larger than 5 MB)")
            continue
        report = agency_layout_report(path.read_bytes())
        if report is None:
            print(f"{path.name}: not an agency digest under the current settings (agency_digests in private_sites.yaml)")
            continue
        seen.add(report["layout"])
        reports += 1
        job_like = max(job_like, report.get("job_like_links", 0))
        match = ("not recorded yet" if report["recorded"] is None else
                 "matches the recorded layout" if report["layout"] == report["recorded"] else "DIFFERS from the recorded layout")
        print(f"{path.name}: {report['agency']}: {report['cards']} cards read ({report['headings']} titles, {report['buttons']} "
              f"Apply Now buttons); layout {report['layout'] or '-'} ({match}); would be {report['status']}"
              + (f" ({report['reason']})" if report["reason"] else ""))
    if len(seen) > 1:
        print("The files have different layouts: compare them before recording one.")
    elif len(seen) == 1 and reports < 2:   # one digest is not enough to tell its fixed parts from today's (L2-02f)
        print("Save a second digest from another day and run this on both before recording the layout.")
    elif len(seen) == 1 and None not in seen:
        print("Both digests have the same layout: record it in the agency's entry as layout: " + next(iter(seen)))
    if job_like:
        print(f"Warning: {job_like} link(s) outside the job cards have a job-like label (apply, job, position, role, "
              "opening): open the email and check them before recording the layout.")


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
            from careeros.discovery.alerts import agency_setting_problem
            stopped = agency_setting_problem(ctx.store)
            if stopped:
                print(f"but every pull is STOPPED: {stopped}")
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
            _halt_if_changed(ctx.store, "Gmail fetch stopped before processing; the next pull does it")   # D153 R2
            counts.update(process(ctx))
            cmd_report(ctx, paths, args)
    except gmail.GmailError as exc:
        sys.exit(f"Gmail: {exc}")


def cmd_process(ctx: Context, paths, args) -> Counter:
    counts = process(ctx)
    from careeros.review.assist import withdraw_handoffs
    # a handoff whose session lost its label, or was opened under standing preparation before an upgrade, is withdrawn
    # at the first processing run (Codex CR-2026-11-15-GATE-FIXES H34, Pass 14)
    withdrawn = withdraw_handoffs(ctx.store, ctx.policy, ctx.profile, paths["root"])
    if withdrawn:
        counts["handoffs_withdrawn"] += withdrawn
    from careeros.discovery.lead_resolution import recheck_linked_leads
    counts.update(recheck_linked_leads(ctx))   # every run: a lead link stands only by its own link or yours (D188)
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
    succeeded = False
    try:
        print("Discovering...")
        args.employer = None
        scanned = cmd_discover(ctx, paths, args)
        counts.update(scanned)
        if scanned["board_scans_attempted"] and scanned["board_errors"] >= scanned["board_scans_attempted"]:   # OPP-54
            _problem(ctx, "CareerOS: every job board failed", f"All {scanned['board_scans_attempted']} boards scanned this run failed "
                                                              "(no network?). Details are in vault\\logs\\daily-run.log")
        for name, step in (("Gmail", lambda: _daily_gmail(ctx)), ("the GitHub inbox", lambda: _daily_inbox(ctx)),
                           ("discovery", lambda: _daily_discovery(ctx, paths)),
                           ("board maintenance", lambda: _board_maintenance(ctx, started))):
            _halt_if_changed(ctx.store, f"daily run stopped before {name}; the next run does the rest")   # D153 R2
            counts.update(step())
        _halt_if_changed(ctx.store, "daily run stopped before processing; the next run does the rest")
        print(f"Processing... (AI requirement reading: {_ai_status(ctx)})")
        counts.update(cmd_process(ctx, paths, args))
        _halt_if_changed(ctx.store, "daily run stopped before the seen-jobs index; the next run does the rest")
        counts.update(_publish_index(ctx, paths))
        _halt_if_changed(ctx.store, "daily run stopped before its report; the next run does the rest")
        ended = ctx.store.clock.now()   # F5: the report shows this run as completed then; a report that fails fails it
        args.completing = (run_id, ended)
        cmd_report(ctx, paths, args)
        _finish_run(ctx, run_id, counts, ended=ended)
        succeeded = True
    except (Exception, CommandStopped):   # a run stopped for a settings change is not completed (D153 R2)
        _finish_run(ctx, run_id, counts, "failed")
        raise
    finally:
        _after_run(ctx, paths, started, notify=succeeded)   # OPP-25: a failed run still gets its backup


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
        if code == "agency_setting_missing" and ctx.policy.get("operations", "notifications", default=True):   # L3-02
            from careeros.ops.notify import toast
            toast("CareerOS: alert intake stopped", "A job-alert setting is missing or changed. Run: careeros status")
        return Counter(gmail_errors=1)
    finally:
        client.close()


def _problem(ctx: Context, title: str, body: str) -> None:
    """A failure notice (careeros.ops.notify.problem, OPP-54) when operations.notifications is on. Never fails the run."""
    if ctx.policy.get("operations", "notifications", default=True):
        from careeros.ops.notify import problem
        problem(title, body)


def _after_run(ctx: Context, paths, started: str, notify: bool = True) -> None:
    """Backup, the weekly restore drill, the offsite archive and the notification: housekeeping that must never turn a
    good run into a failed one, nor take the place of a failed run's own error (Codex CR-2026-11-03-PYTHON-314, F2).
    The backup is made after a failed run too (OPP-25); the summary notification only after a good one."""
    from careeros.ops.backup import backup
    from careeros.ops.notify import summary, toast
    try:
        folder = backup(ctx.store, paths, keep=int(ctx.policy.get('operations', 'backup_keep', default=14)))
        print(f"Backup: {folder}")
    except Exception as exc:  # noqa: BLE001 - recorded as an exception for you to see in status
        log.exception("backup failed")
        print(f"  ! backup failed: {exc}")
        _record_trouble(ctx, "medium", f"daily backup failed: {exc}", "backup_failed")
        _problem(ctx, "CareerOS: daily backup failed", "Details are in vault\\logs\\careeros.log")
    else:
        try:
            _weekly_drill(ctx, paths, folder)
        except Exception as exc:  # noqa: BLE001 - the drill's own trouble never fails the run
            log.exception("restore drill could not finish")
            print(f"  ! restore drill could not finish: {type(exc).__name__}: {exc}")
        try:
            _offsite(ctx, paths, folder)
        except Exception as exc:  # noqa: BLE001 - the offsite archive's own trouble never fails the run either
            log.exception("offsite archive could not finish")
            print(f"  ! offsite archive could not finish: {type(exc).__name__}: {exc}")
    if notify and ctx.policy.get("operations", "notifications", default=True):
        try:
            message = summary(ctx.store, started)
            if message:
                print(f"Notification: {message[0]}" + ("" if toast(*message) else " (could not be shown)"))
        except Exception as exc:  # noqa: BLE001 - a summary that cannot be made is not a failed run
            log.exception("run summary notification")
            print(f"  ! notification not shown: {type(exc).__name__}: {exc}")


def _record_trouble(ctx: Context, severity: str, message: str, code: str) -> None:
    """An exception for `careeros status`, made best effort: housekeeping is reported, never fatal (F2)."""
    try:
        ctx.store.writer("runner").exception("system", severity, message, error_code=code)
    except Exception:  # noqa: BLE001 - the log and the screen still say it
        log.exception("could not record %s", code)


def cmd_migrate(ctx: Context, paths, args) -> None:
    """OPP-50: the one command meant to migrate the database, under the writer lock, after a verified backup."""
    from careeros.db.store import newest_schema
    from careeros.ops.backup import backup, verify
    before, newest = ctx.store.schema_version(), newest_schema()
    if before >= newest:
        ctx.store.check_schema()   # a database newer than this engine is refused here too
        print(f"schema {before}: nothing to migrate")
        return
    stamp = ctx.store.clock.now()[:19].replace(":", "")   # N1: a checkpoint the same day's backups never replace
    folder = backup(ctx.store, paths, keep=int(ctx.policy.get("operations", "backup_keep", default=14)),
                    name=f"before-migration-{before}-to-{newest}-{stamp}")
    problems = verify(folder)
    if problems:
        print(f"Backup: {folder} (PROBLEMS: {', '.join(problems)}); nothing migrated")
        sys.exit(2)
    print(f"Backup: {folder} (verified)")
    print(f"schema {before} -> {ctx.store.migrate()}")


def _weekly_drill(ctx: Context, paths, folder) -> None:
    """OPP-25: once a week (daily after a failed drill, or when its record cannot be read), the newest backup is read back
    as a restore would read it. A failure is recorded, shown in status and notified; so is a result that cannot be saved
    (F2). Never fails the run."""
    from careeros.ops.backup import drill_due, record_drill, restore_drill
    now = ctx.store.clock.now()
    if not drill_due(paths["backups"], now):
        return
    try:
        jobs = ctx.store.one("SELECT COUNT(*) c FROM jobs")
        result = restore_drill(Path(folder), jobs["c"] if jobs else 0)
    except Exception as exc:  # noqa: BLE001 - a drill that cannot even open the copy is a failed drill
        result = {"folder": Path(folder).name, "ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
    why = "The newest backup did not read back whole."
    try:
        record_drill(paths["backups"], result, now)
    except OSError as exc:   # the copy was read; its result is not saved, so the next run drills again
        log.exception("restore drill result not saved")
        result = {**result, "ok": False, "problems": [*result["problems"], f"result not saved ({type(exc).__name__}: {exc})"]}
        why = "Its result could not be saved." if len(result["problems"]) == 1 else why
    if result["ok"]:
        print(f"Restore drill: {result['folder']} read back whole ({result['jobs']} jobs, {result['events']} events)")
        return
    problems = ", ".join(result["problems"])
    print(f"  ! restore drill failed: {problems}")
    _record_trouble(ctx, "high", f"restore drill failed on {result['folder']}: {problems}", "restore_drill_failed")
    _problem(ctx, "CareerOS: restore drill failed", f"{why} Details: careeros status")


def _offsite(ctx: Context, paths, folder, credentials=None) -> dict | None:
    """OPP-26 (D97): the encrypted offsite archive of the backup just made, and once a week a drill that reads the newest
    archive back. Only when operations.offsite.folder is set, and never in Demo Mode (None then). A failure is recorded,
    shown in status and notified; it never fails the run."""
    from datetime import datetime

    from careeros.demo.sandbox import enabled as demo_mode
    from careeros.ops import offsite
    from careeros.ops.backup import DRILL_EVERY_DAYS
    destination, keep, configured = offsite.settings(ctx.policy)
    if destination is None or demo_mode():
        return None
    now = ctx.store.clock.now()
    passphrase = gpg = None
    try:
        if credentials is None:
            from careeros.core.credentials import WindowsCredentials
            credentials = WindowsCredentials()
        key = credentials.written(offsite.CREDENTIAL)   # the key's generation, not secret (F8); read before the secret, so a
        # passphrase saved between the two reads records the older generation and status asks again (F8-03)
        passphrase, gpg = credentials.read(offsite.CREDENTIAL), offsite.find_gpg(configured)
        result = offsite.make(Path(folder), paths["root"], destination, passphrase=passphrase, gpg=gpg, at=now, keep=keep)
        result["key"] = key
    except Exception as exc:  # noqa: BLE001 - recorded, shown in status and notified; the run goes on
        result = {"ok": False, "archive": None, "problems": [f"{type(exc).__name__}: {exc}"]}
    if result["ok"]:
        previous = (offsite.last(paths["backups"]) or {}).get("drill")
        age = (datetime.fromisoformat(now.replace("Z", "+00:00"))
               - datetime.fromisoformat(previous["at"].replace("Z", "+00:00"))).days if previous else None
        if not previous or not previous.get("ok") or age is None or age >= DRILL_EVERY_DAYS:
            result["drill"] = {**offsite.drill(destination, passphrase=passphrase, gpg=gpg, area=paths["backups"]), "at": now}
    why = "No new offsite copy was made today."
    try:
        offsite.record(paths["backups"], result, now)
    except OSError as exc:   # F2, as for the restore drill: the archive may be made; its record is not saved
        log.exception("offsite record not saved")
        made = result["ok"]
        result = {**result, "ok": False, "problems": [*result["problems"], f"result not saved ({type(exc).__name__}: {exc})"]}
        why = "Its record could not be saved, so status cannot show it." if made else why
    if not result["ok"]:
        problems = ", ".join(result["problems"])
        print(f"  ! offsite archive failed: {problems}")
        _record_trouble(ctx, "high", f"offsite archive failed: {problems}", "offsite_failed")
        _problem(ctx, "CareerOS: offsite archive failed", f"{why} Details: careeros status")
        return result
    print(f"Offsite archive: {result['archive']} ({result['bytes'] / 1e6:.1f} MB, {result['files']} files) in {destination}")
    drill = result.get("drill")
    if drill and not drill["ok"]:
        problems = ", ".join(drill["problems"])
        print(f"  ! offsite drill failed: {problems}")
        _record_trouble(ctx, "high", f"offsite drill failed on {drill['archive']}: {problems}", "offsite_drill_failed")
        _problem(ctx, "CareerOS: offsite drill failed", "The newest offsite archive did not read back whole. Details: careeros status")
    elif drill:
        print(f"Offsite drill: {drill['archive']} decrypted and read back whole ({drill['files']} files)")
    return result


def _offsite_line(policy, paths, now: str) -> str:
    """OPP-26: the offsite archive for `careeros status`, marked !! when it failed, its drill failed, or either is
    overdue (the drill as _drill_line says it of the restore drill: Fable review of 559212b, finding 11)."""
    from datetime import datetime

    from careeros.ops import offsite
    from careeros.ops.backup import DRILL_EVERY_DAYS
    destination = offsite.settings(policy)[0]
    if destination is None:
        return "offsite archive: off (set operations.offsite.folder in your vault overrides; operator manual, Offsite archive)"
    entry = offsite.last(paths["backups"])
    if not entry:
        return f"offsite archive: none yet (the next daily run writes one to {destination})"
    if entry.get("unreadable"):   # F2, as for the restore drill: a damaged record is said, never a crash
        return "!! offsite archive: its record (vault\\backups\\offsite.json) cannot be read; the next daily run writes a new one"
    if not entry.get("ok"):
        return f"!! offsite archive failed {entry['at'][:10]}: {', '.join(entry.get('problems', []))}"
    drill = entry.get("drill")
    if drill and not drill.get("ok"):
        return f"!! offsite drill failed {drill['at'][:10]} on {drill.get('archive')}: {', '.join(drill.get('problems', []))}"
    age = (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))).days
    if age > 2:
        return f"!! offsite archive: the newest ({entry['at'][:10]}) is {age} days old"
    drill_age = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                 - datetime.fromisoformat(drill["at"].replace("Z", "+00:00"))).days if drill else None
    if drill and drill_age is not None and drill_age > DRILL_EVERY_DAYS + 1:
        return f"!! offsite drill: the last one ({drill['at'][:10]}) is {drill_age} days old; the daily run makes one weekly"
    kept = len(offsite.ours(destination))
    return (f"offsite archive: {entry['archive']} ok ({entry['bytes'] / 1e6:.1f} MB; {kept} kept in {destination})"
            + (f"; drill {drill['at'][:10]} ok" if drill else ""))


def _passphrase_line(policy, paths, credentials=None) -> str | None:
    """Pre-review #1 (D111): whether the passphrase copy kept off this PC has opened an archive, for `careeros status`;
    None while the offsite archive is off. A confirmation holds only while the stored passphrase's generation and the
    folder are the ones it was made under (Codex CR-2026-11-05-COMBINED, F8): saved anew, even the same day, or moved,
    the archives may no longer open with the kept copy. Reads the credential's generation only, never the secret."""
    from careeros.ops import offsite
    destination = offsite.settings(policy)[0]
    if destination is None:
        return None
    entry = offsite.confirmed(paths["backups"])
    if entry is None:
        return ("!! offsite passphrase: the copy you keep off this PC has never opened an archive; "
                "run careeros offsite confirm")
    if not entry["ok"]:
        return (f"!! offsite passphrase: the copy you keep off this PC did not open {entry['archive']} on {entry['at'][:10]}; "
                "run careeros offsite confirm")
    if credentials is None:
        try:
            from careeros.core.credentials import WindowsCredentials
            credentials = WindowsCredentials()
        except Exception:  # noqa: BLE001 - no Credential Manager here: the generation cannot be shown current
            credentials = None
    current = credentials.written(offsite.CREDENTIAL) if credentials else None
    if entry.get("key") is None:   # D149 red team 6: it opened bytes this PC did not make with the stored passphrase
        return (f"!! offsite passphrase: the copy you keep off this PC opened {entry['archive']} on {entry['at'][:10]}, but "
                f"{offsite.NOT_MADE}; run careeros backup --offsite, then careeros offsite confirm")
    if entry.get("key") is None or entry.get("key") != current or entry.get("folder") != str(destination):
        return (f"!! offsite passphrase: the copy you keep off this PC opened {entry['archive']} on {entry['at'][:10]}, but "
                "the stored passphrase or the offsite folder has changed since; run careeros offsite confirm")
    # D149 R-O1: and only while no file of that name holds other bytes than the ones it opened (its SHA-256 then); a
    # pruned or removed archive keeps it. A record without the hash (before D149) does not hold
    if not offsite.not_replaced(destination, entry):
        return (f"!! offsite passphrase: the copy you keep off this PC opened {entry['archive']} on {entry['at'][:10]}, but "
                "that archive has been replaced since; run careeros offsite confirm")
    return f"offsite passphrase: the copy you keep off this PC opened {entry['archive']} on {entry['at'][:10]}"


def cmd_offsite(ctx: Context, paths, args) -> None:
    """Pre-review #1 (D111): `careeros offsite confirm` asks for the passphrase as you keep it off this PC (not shown)
    and opens the newest archive with it, as a restore on another computer would. Every other check uses the Credential
    Manager copy, which is lost with this PC; a typo there, or a passphrase it stored wrongly, would only show then."""
    import getpass

    from careeros.demo.sandbox import enabled as demo_mode
    from careeros.ops import offsite
    destination, _, configured = offsite.settings(ctx.policy)
    if destination is None or demo_mode():
        sys.exit("Offsite archive: off (set operations.offsite.folder in your vault overrides; not in Demo Mode)")
    if not offsite.ours(destination):
        sys.exit("No offsite archive yet: run careeros backup --offsite first")
    typed = getpass.getpass("The offsite passphrase, as you keep it off this PC (not shown): ")
    # F8, F8-03, D149 R-O1: after the prompt, confirm reads the archive once and binds the stored passphrase's generation
    # only to those very bytes (the archive record of the same name and SHA-256); otherwise none, and status asks again
    result = offsite.confirm(destination, typed=typed, gpg=offsite.find_gpg(configured), backups_root=paths["backups"],
                             at=ctx.store.clock.now())
    if result.get("recorded") is False:
        sys.exit(f"Not confirmed: {result['archive'] or 'the newest archive'} changed while it was being checked (a sync or "
                 "a backup run?), so nothing was recorded. Run careeros offsite confirm again.")
    if not result["ok"]:
        sys.exit(f"Not confirmed: {result['archive']} did not open with it, or did not read back whole "
                 f"({', '.join(result['problems'])}). Check the copy you keep. If the stored one is wrong, save the right "
                 f"one in Credential Manager as {offsite.CREDENTIAL}, run careeros backup --offsite, then confirm again.")
    if not result.get("bound"):   # D149 red team 6: what is said follows what was recorded (no key generation bound)
        sys.exit(f"Not confirmed: the copy you keep off this PC opened {result['archive']}, but {offsite.NOT_MADE} (its "
                 "bytes are not the ones this PC's archive record names). Run careeros backup --offsite, then confirm again.")
    print(f"Confirmed: the copy you keep off this PC opened {result['archive']}, and all {result['files']} files read back whole")


def _drill_line(paths, now: str) -> str:
    """OPP-25: the last restore drill, for `careeros status`, marked !! when it failed or is overdue."""
    from datetime import datetime

    from careeros.ops.backup import DRILL_EVERY_DAYS, last_drill
    drill = last_drill(paths["backups"])
    if not drill:
        return "restore drill: none yet (the daily run reads the newest backup back once a week)"
    if drill.get("unreadable"):   # F2: a damaged record is said, never a crash; the next daily run makes a new one
        return "!! restore drill: its record (vault\\backups\\restore-drill.json) cannot be read; the next daily run drills again"
    if not drill.get("ok"):
        return f"!! restore drill failed {drill['at'][:10]} on {drill['folder']}: {', '.join(drill.get('problems', []))}"
    age = (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(drill["at"].replace("Z", "+00:00"))).days
    if age > DRILL_EVERY_DAYS + 1:
        return f"!! restore drill: the last one ({drill['at'][:10]}) is {age} days old; the daily run makes one weekly"
    return f"restore drill: {drill['at'][:10]} ok ({drill['folder']}: {drill['jobs']} jobs, {drill['events']} events)"


def cmd_backup(ctx: Context, paths, args) -> None:
    from careeros.ops.backup import backup, verify
    folder = backup(ctx.store, paths, keep=int(ctx.policy.get("operations", "backup_keep", default=14)))
    problems = verify(folder)
    print(f"Backup: {folder} ({'verified' if not problems else 'PROBLEMS: ' + ', '.join(problems)})")
    if problems:
        sys.exit(2)
    if getattr(args, "offsite", False):   # OPP-26: the offsite archive now, e.g. right after setting it up
        result = _offsite(ctx, paths, folder)
        if result is None:
            sys.exit("Offsite archive: off (set operations.offsite.folder in your vault overrides; not in Demo Mode)")
        if not result["ok"]:
            sys.exit(2)


def cmd_review(ctx: Context, paths, args) -> None:
    from careeros.review.server import serve
    serve(ctx.store, ctx.profile, ctx.policy, paths["lock"], port=args.port, open_browser=not args.no_browser)


def _show_preview(ctx: Context, args, who: str) -> bool:
    """Print the card an Apply or Stretch label would approve now, recorded as a preview, and how to confirm it (the
    D190 design, section 3, "The preview"). False when the job offers no preview now."""
    from careeros.review.labels import capture, preview_lines, write_preview
    captured = capture(ctx.store, ctx.policy, ctx.profile, args.job_id, who)
    for line in preview_lines(captured, ctx.store, ctx.store.clock.now()):
        print(line)
    if captured["waiting"]:
        return False
    preview_id = write_preview(ctx.store, captured)
    print(f"To label it {args.label} as shown: careeros label {args.job_id} {args.label} --confirm {preview_id}"
          + (" --night" if who == "night" else ""))
    return True


def cmd_label(ctx: Context, paths, args) -> None:
    from careeros.review.labels import PreviewRefused
    who = "night" if args.night else "primary"
    args.confirm = getattr(args, "confirm", None)
    if args.confirm and args.label == "skip":
        sys.exit("Not labelled: Skip needs no preview; run it without --confirm")
    if args.label in ("apply", "stretch") and not args.confirm:
        # an Apply or Stretch label is given from a preview of what it approves: this prints one (the D190 design)
        try:
            shown = _show_preview(ctx, args, who)
        except ValueError as exc:
            sys.exit(f"Not labelled: {exc}")
        if not shown:
            sys.exit("Not labelled: no preview now")
        return
    try:
        print(record_label(ctx.store, args.job_id, args.label, args.reason, key=f"cli:{secrets.token_hex(8)}",
                           profile=who, preview=args.confirm, policy=ctx.policy, applicant=ctx.profile))
    except PreviewRefused as exc:
        print(f"Not labelled: {exc}")
        try:   # refused with a fresh preview, when there is one
            _show_preview(ctx, args, who)
        except ValueError:
            pass
        sys.exit(2)
    except ValueError as exc:
        sys.exit(f"Not labelled: {exc}")
    from careeros.review.assist import withdraw_handoffs
    # a handoff never outlives the label it was issued under: a Skip withdraws it at once, not at the next run (the red
    # team before Codex Pass 15, RT-01)
    withdrawn = withdraw_handoffs(ctx.store, ctx.policy, ctx.profile, paths["root"]) if paths.get("root") else 0
    if withdrawn:
        print(f"  handoffs_withdrawn={withdrawn}")


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
    if getattr(args, "followed_up", False):   # an event, not a state (OPP-21): the status and its history stay as they are
        if args.state or getattr(args, "reason", None):
            sys.exit("Not recorded: --followed-up records a follow-up you sent and keeps the status; give no status or --reason with it")
        try:
            record_follow_up(ctx.store, args.application_id, notes=args.notes, key=f"cli:{secrets.token_hex(8)}")
        except ValueError as exc:
            sys.exit(f"Not recorded: {exc}")
        print(f"{args.application_id}: follow-up recorded; the status is unchanged and the follow-up reminder counts from today")
        return
    if not args.state:
        sys.exit("Not changed: give the new status (e.g. screening), or --followed-up to record a follow-up you sent")
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
        from careeros.discovery.alerts import agency_setting_problem
        stopped = agency_setting_problem(ctx.store)
        if stopped:
            print(f"Board discovery is STOPPED: {stopped}")
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
        # a board you (or a lead) named that does not exist: nothing is admitted, and you can see it here (the Functional
        # MVP gate, 2026-11-15: it was only in the discovery_attempts table). Only a probe of exactly that board counts,
        # dated by the probe, shown as the board's own address, never the link as stored (Codex CR-2026-11-15-GATE-FIXES F5)
        absent = bd.absent_suggestions(store)
        if absent:
            print("Suggested boards that do not exist (nothing admitted; suggest the right link with careeros sources suggest):")
            for name, address, probed in absent:
                print(f"  {name[:40]:40} {address}  (checked {probed[:10]})")
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
                entity_id = m["entity_id"] or registry.ensure_entity_for(store, m["employer_name"], domain=m["official_domain"],
                                                                         origin=m["origin"])
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
                                                           "domain_origin": "owner", "domain_set_at": store.clock.now(),
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
        if status == "active":
            _leads_after_admission(ctx, target)
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
        _leads_after_admission(ctx, target)
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


def _leads_after_admission(ctx: Context, mapping_id: str) -> None:
    """A board just admitted: its employer's open leads, matched against the jobs CareerOS holds from it."""
    from careeros.discovery import lead_resolution
    found = lead_resolution.after_admission(ctx, mapping_id)
    if found:
        print("  leads: " + " ".join(f"{k.removeprefix('leads_')}={v}" for k, v in sorted(found.items())))


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
    _withdraw_lost_handoffs(ctx, paths)   # every handoff file goes (browser Fill is paused, D191)


def cmd_location(ctx: Context, paths, args) -> None:
    """Set a pasted or added posting's location by naming the job (the owner's D180): the job is screened again."""
    from careeros.discovery.manual_intake import set_location
    try:
        result = set_location(ctx, args.job_id, args.location)
    except ValueError as exc:
        sys.exit(f"Location not set: {exc}")
    print(f"{result['job_id']}: location " + ("set; screened again now" if result["changed"] else "recorded as yours (unchanged)"))
    counts = process(ctx)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))


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
    from careeros.db.store import IdempotencyCollision
    from careeros.discovery.manual_intake import MAX_TEXT, operation_for, paste_request
    request = paste_request(args.url, text, company=args.company, title=args.title, location=args.location)
    op = operation_for("paste", "", request, separate=getattr(args, "separate", False),
                       operation_id=getattr(args, "operation", None))
    if op.separate:
        print(f"operation {op.key} (to retry it after an interruption: careeros paste ... --operation {op.key})")
    try:
        result = intake(ctx, text, args.url, company=args.company, title=args.title, location=args.location, operation=op)
    except ValueError as exc:
        sys.exit(f"Not added: {exc}")
    except IdempotencyCollision:
        # a retry by --operation of another request is refused, never answered with the recorded job (Codex H43)
        sys.exit(f"Not added: operation {op.key} was recorded for another request: its link, its text, your arguments "
                 f"(as typed, for one that runs past its limit), or whether its text runs past the {MAX_TEXT:,}-character "
                 "limit differ. Paste it without --operation "
                 "to take it in as a request of its own")
    print(f"{result['job_id']}: {result['status']}" + (f"; manual source recorded ({result['manual_source']})" if result["manual_source"] else ""))
    if result.get("location_confirmed"):
        print("  location confirmed" + ("; it is screened again now" if result.get("location_changed") else ""))
    if result.get("location_note"):
        print(f"  {result['location_note']}")
    if result.get("id_collision"):
        print(f"  tracked as its own job: {result['id_collision']} is another posting (linked as a possible duplicate)")
    _link_leads(ctx, result["job_id"], "pasted")
    counts = process(ctx)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))
    _withdraw_lost_handoffs(ctx, paths)
    cmd_report(ctx, paths, args)


def cmd_notices(ctx: Context, paths, args) -> None:
    """Alert notices (part 3 of the owner's D192 build): cards whose own link names no posting (a careers page), never
    leads. List them grouped by the employer each names, dismiss one, or add its posting: by the posting's own link
    (--url, through add-url), or the notice's link with the posting's text (--text-file)."""
    from careeros.review import notices
    store = ctx.store
    if args.action == "list":
        total, cache = 0, dict[str, object]()
        for label, rows in notices.groups(store, ("new", "dismissed", "added") if args.all else ("new",)):
            print(f"{label} ({len(rows)})")
            for n in rows:
                total += 1
                print(f"  {n['notice_id']}  {n['state']:9} {(n['received_at'] or n['first_seen_at'])[:10]}  {n['site']:9} "
                      f"{n['title'][:60]}")
                print(f"    {n['url']}")
                if n["context"]:
                    print(f"    {n['context'][:160]}")
                guess = json.loads(n["suggestion_json"] or "{}").get("greenhouse_posting")
                if guess:
                    print(f"    may be {guess} (a guess): careeros notices add {n['notice_id']} --url {guess}")
                for job in notices.likely_postings(store, ctx.policy, n, cache):
                    print(f"    a tracked posting with this title: {job['job_id']} ({job['location_raw'] or 'location unknown'})")
        print(f"{total} notice(s)")
        return
    if not args.target:
        sys.exit(f"Give the NOTICE_ID: careeros notices {args.action} NOTICE_ID")
    notice = store.one("SELECT * FROM alert_notices WHERE notice_id = ?", (args.target,))
    if notice is None:
        sys.exit(f"Not done: unknown notice {args.target}")
    key = f"cli:{secrets.token_hex(8)}"
    if args.action == "dismiss":
        notices.dismiss_notice(store, args.target, key=key)
        print(f"{args.target}: dismissed")
        return
    from types import SimpleNamespace
    job_id = cmd_add_url(ctx, paths, SimpleNamespace(
        url=args.url or notice["url"], text_file=args.text_file, employer=args.employer or notice["suggested_employer"],
        title=args.title or notice["title"], location=args.location, parent=None))
    if job_id:
        notices.mark_added(store, args.target, job_id, key=key)
        print(f"{args.target}: added as {job_id}")


def cmd_leads(ctx: Context, paths, args) -> None:
    """Your leads (ADR 0003, amendment of 2026-11-02): the open ones with their state and reason, and the likely matches
    waiting for you; confirm or reject a likely match; unlink a lead linked to the wrong job (a correction, audited).
    Nothing here fetches anything."""
    from careeros.review import leads as review_leads
    store, action = ctx.store, args.action
    if action == "list":
        waiting = {item["lead"]["lead_id"]: item for item in review_leads.pending_all(store, ctx.policy)}
        states = ("new", "no_match", "ingested", "closed", "dismissed") if args.all else review_leads.OPEN
        rows = store.all(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(states))}) ORDER BY first_seen_at DESC, lead_id",
                         states)
        for lead in rows:
            company = (lead["context"] or "").split(" · ")[0][:40]
            print(f"{lead['lead_id']}  {lead['status']:9} {lead['first_seen_at'][:10]}  {lead['site']:9} {lead['title'][:50]} | {company}")
            if lead["job_id"]:
                print(f"    linked to {lead['job_id']}")
            if lead["status_reason"]:
                print(f"    {lead['status_reason']}")
            if lead["fetch_failure"]:
                print(f"    could not be read: {lead['fetch_failure']}")
            if lead["repeat_of"]:   # the same agency and job number (H8, part 3 of D192): advice only
                print(f"    possibly the same as {lead['repeat_of']} (the same job number)")
            item = waiting.get(lead["lead_id"])
            if item:
                job = item["job"]
                print(f"    LIKELY MATCH: {job['job_id']}  {job['title']} | {job['employer_name']} | {job['location_raw'] or 'location unknown'}")
                print(f"      careeros leads confirm {lead['lead_id']} --job {job['job_id']}")
                print(f"      careeros leads reject {lead['lead_id']} --job {job['job_id']} [--reason ...]")
        print(f"{len(rows)} lead(s); {len(waiting)} likely match(es) waiting for you")
        return
    if not args.target:
        sys.exit(f"Give the LEAD_ID: careeros leads {action} LEAD_ID")
    key = f"cli:{secrets.token_hex(8)}"
    try:
        if action == "confirm":
            review_leads.confirm_likely(store, args.target, policy=ctx.policy, job_id=args.job, note=args.note, key=key)
            linked = store.one("SELECT job_id FROM leads WHERE lead_id = ?", (args.target,))
            print(f"{args.target}: linked to {linked['job_id'] if linked else ''} (you confirmed it)")
        elif action == "reject":
            row = review_leads.pending(store, args.target, ctx.policy)
            review_leads.reject_likely(store, args.target, policy=ctx.policy, job_id=args.job, reason=args.reason, key=key)
            print(f"{args.target}: likely match {row['job_id'] if row else ''} rejected; it is never proposed for this lead again")
        elif action == "unlink":
            was = store.one("SELECT job_id FROM leads WHERE lead_id = ?", (args.target,))
            review_leads.unlink_lead(store, args.target, reason=args.reason or "", key=key)
            print(f"{args.target}: unlinked from {was['job_id'] if was else ''} (your correction, in the audit trail); it is new "
                  "again and is matched with its own facts after the next scan of its employer's board")
    except ValueError as exc:
        sys.exit(f"Not changed: {exc}")


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
                try:
                    questions = packages.load_questions(root, r["job_id"])
                except ValueError as exc:   # one job's unreadable file is shown on its line, never ends the list (RT-40)
                    print(f"{r['package_id']}  v{r['version']}  {r['route']:<8}  {'questions unreadable':<24}  {r['job_id']}  "
                          f"{r['title']} at {r['employer_name']}\n  {exc}")
                    continue
                ready = packages.readiness(ctx.store, ctx.policy, ctx.profile, r["job_id"], facts=facts, library=library,
                                           vault_root=root, questions=questions)
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
            job = ctx.store.one("SELECT * FROM jobs WHERE job_id = ?", (args.target,))
            endpoint = _posting_endpoint(ctx.store, job) if job is not None else None
            if endpoint is None or endpoint[0] != "greenhouse":
                raise ValueError("not_a_greenhouse_job" if job is not None else f"unknown_job: {args.target}")
            # the posting's current board, never a token taken from the job id (the red team's hr1-identity-1)
            found = fetch_greenhouse_questions(HttpClient(), args.target, endpoint=endpoint[1:])
            path = packages.save_questions(root, args.target, found, "greenhouse job-board API", ctx.store.clock.now())
            print(f"{len(found)} question(s) read into {path}")
            return
        facts, library = _package_inputs(paths)
        if args.action == "prepare" and args.standing:
            # standing preparation is off (the owner's D183): nothing is prepared without your label
            print("0 job(s): standing preparation is off; label a job Apply or Stretch to prepare it")
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
                                   login_steps=args.login_steps, notes=args.notes, reapply=args.reapply, confirm=args.confirm,
                                   vault_root=paths.get("root"))
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


def cmd_labels(ctx: Context, paths, args) -> None:
    """Where your labels and dismissals disagree with the system now (OPP-20, 2026-11-03), with the rule that fired.
    Read only."""
    for line in render_disagreements(disagreements(ctx.store, ctx.policy)):
        print(line)


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
    from careeros.ops import runtime   # OPP-55: what the engine runs on, against requirements.lock
    line, differ = runtime.summary(ENGINE_ROOT / "requirements.lock")
    print(line)
    for difference in differ:
        print(f"!! {difference}")
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
    # a hold stands until a board read lifts it or a label acknowledges it (the D190 design, section 7): counted apart
    # from the exceptions that wait to be dealt with (build item Y1)
    print("open exceptions:", store.all("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved' "
                                         "AND COALESCE(error_code, '') NOT IN ('ingest_failed', 'read_pending')")[0]["c"])
    print("standing holds:", store.all("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved' "
                                        "AND error_code IN ('ingest_failed', 'read_pending')")[0]["c"])
    from careeros.review.packages import every_job_holds, pending_file_problem
    every = every_job_holds(store)
    if every:   # Z8: each is on every card and holds every Apply and Stretch label it has not been acknowledged by
        print(f"holds that reach every job: {len(every)} (a posting you added could not be stored, and CareerOS cannot "
              f"tell which posting it was: {', '.join(sorted({r['occurred_at'][:10] for r in every}))}); each holds every "
              "Apply and Stretch label until a label given from a card that shows it acknowledges it")
    held_file = pending_file_problem(store)
    if held_file:
        print(f"holds kept beside the database: {held_file}. Every Apply and Stretch label waits until then")
    from careeros.discovery.alerts import agency_setting_problem
    stopped = agency_setting_problem(store, name_hosts=True)
    if stopped:
        print(f"ALERT INTAKE STOPPED: {stopped}")
    for row in store.all("SELECT occurred_at, message FROM exceptions WHERE error_code = ? AND status != 'resolved' "
                         "ORDER BY occurred_at", (HELD_DIGEST,)):  # agency digests read only in part (2026-11-09)
        print(f"  held digest ({row['occurred_at'][:10]}): {row['message']}")
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
    print(_drill_line(paths, store.clock.now()))
    print(_offsite_line(ctx.policy, paths, store.clock.now()))
    passphrase = _passphrase_line(ctx.policy, paths)
    if passphrase:
        print(passphrase)


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
    quick = [row[0] for row in ctx.store.conn.execute("PRAGMA quick_check").fetchall()]   # OPP-50: the file itself
    foreign = [dict(table=r[0], rowid=r[1], parent=r[2]) for r in ctx.store.conn.execute("PRAGMA foreign_key_check").fetchall()]
    print(json.dumps({"event_chain_breaks": broken, "inconsistent_jobs": bad_jobs, "inconsistent_applications": bad_apps,
                      "quick_check": quick, "foreign_key_problems": foreign[:20], "foreign_key_problem_count": len(foreign)},
                     indent=2))
    if broken or bad_jobs or bad_apps or quick != ["ok"] or foreign:
        sys.exit(2)


WRITES = {"init", "employers", "discover", "add-url", "import", "process", "run", "label", "applied", "app-status", "backup", "gmail", "inbox",
          "location",   # it writes the job and screens it again: under the writer lock (the red team's fr1-faults-3)
          "sources", "audit", "dismiss", "paste", "draft", "metrics", "leads", "notices",
          "seen-index", "release", "package", "assist",
          "export", "migrate", "offsite"}   # the export only reads, but takes the lock so it never reads a run half-way through


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
    p.add_argument("--separate", action="store_true", help="with --text-file: take the same request in again as an "
                   "operation of its own (its id is printed)")
    p.add_argument("--operation", metavar="ID", help="with --text-file: retry the separate operation ID")
    sub.add_parser("process")
    p = sub.add_parser("import", help="import postings you saved into vault\\inbox")
    p.add_argument("--separate", action="store_true", help="take each saved file in again as an operation of its own")
    sub.add_parser("report")
    p = sub.add_parser("review")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("label")
    p.add_argument("job_id")
    p.add_argument("label", choices=["apply", "stretch", "skip"])
    p.add_argument("--reason")
    p.add_argument("--night", action="store_true", help="a NIGHT-profile label (measured separately from PRIMARY)")
    p.add_argument("--confirm", metavar="PREVIEW", help="record an Apply or Stretch label from the preview this command "
                   "printed (without it, Apply and Stretch print a preview)")
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
    p = sub.add_parser("app-status", help="update an application's status (void, recorded by mistake, needs --reason), or "
                                          "record a follow-up you sent with --followed-up")
    p.add_argument("application_id")
    p.add_argument("state", nargs="?")
    p.add_argument("--followed-up", action="store_true", help="record that you followed up; the status stays as it is")
    p.add_argument("--notes")
    p.add_argument("--reason", help="why: required for void, an application recorded by mistake (it stays in the history)")
    sub.add_parser("applications")
    p = sub.add_parser("funnel", help="recommended -> approved -> applied -> ... -> hired, by a dimension")
    p.add_argument("--by", choices=["source", "family", "profile", "tier", "channel", "domain", "night_mode", "employment_type",
                                    "employer"], default="source")
    p = sub.add_parser("labels", help="each label or dismissal the system disagrees with now, and the rule that fired")
    p.add_argument("action", nargs="?", default="disagreements", choices=["disagreements"])
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
    p = sub.add_parser("backup", help="a verified backup now; --offsite also writes the encrypted offsite archive")
    p.add_argument("--offsite", action="store_true", help="also write today's offsite archive (operations.offsite.folder)")
    p = sub.add_parser("offsite", help="confirm: open the newest offsite archive with the passphrase as you keep it off this PC")
    p.add_argument("action", choices=["confirm"])
    sub.add_parser("inbox", help="read job recommendations posted as issues in the private GitHub inbox")
    sub.add_parser("seen-index", help="rebuild the seen-jobs index and Gmail intake status and publish them to the inbox repo")
    p = sub.add_parser("agency-held", help="the agency digests held back for you; --dismiss ID closes one you have looked at")
    p.add_argument("--dismiss", metavar="ID")
    p = sub.add_parser("agency-hosts", help="the agency tracking hosts CareerOS has been set up with and the stored links "
                                            "that use them")
    p.add_argument("--forget", metavar="HOST", help=argparse.SUPPRESS)   # turned off (D153 R6): it only says so
    p = sub.add_parser("agency-layout", help="saved agency job-alert digests (.eml): cards read and the layout fingerprint "
                                             "for the private settings, no text or links")
    p.add_argument("files", nargs="+", help="saved digest .eml files")
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
    p.add_argument("--location", help="a new posting's location; a tracked job's is set with careeros location")
    p.add_argument("--separate", action="store_true", help="take the same request in again as an operation of its own "
                   "(its id is printed; a plain rerun is the retry of the first)")
    p.add_argument("--operation", metavar="ID", help="retry the separate operation ID after an interruption")
    p = sub.add_parser("location", help="set a pasted or added posting's location by naming the job (it is screened again)")
    p.add_argument("job_id")
    p.add_argument("location")
    p = sub.add_parser("leads", help="your leads: the open ones and the likely matches waiting for you; confirm or reject one, "
                                     "or unlink a wrong link")
    p.add_argument("action", nargs="?", default="list", choices=["list", "confirm", "reject", "unlink"])
    p.add_argument("target", nargs="?", help="LEAD_ID (confirm, reject, unlink)")
    p.add_argument("--job", help="with confirm or reject (required): the job of the likely match you saw; refused unless it is "
                                 "the one waiting now")
    p.add_argument("--reason", help="with reject: why it is not this lead's posting (optional); with unlink: why the link is "
                                    "wrong (required)")
    p.add_argument("--note", help="with confirm: a note kept with your confirmation (optional)")
    p.add_argument("--all", action="store_true", help="with list: every lead, not only the open ones")
    p = sub.add_parser("notices", help="alert cards whose link names no posting (careers pages): list them, dismiss one, or "
                                       "add its posting")
    p.add_argument("action", nargs="?", default="list", choices=["list", "dismiss", "add"])
    p.add_argument("target", nargs="?", help="NOTICE_ID (dismiss, add)")
    p.add_argument("--all", action="store_true", help="with list: dismissed and added notices too")
    p.add_argument("--url", help="with add: the posting's own link (a board's posting link); else the notice's link, with "
                                 "--text-file")
    p.add_argument("--text-file", help="with add: the posting's text, saved to a file")
    p.add_argument("--employer", help="with add and --text-file: the employer (default: the one the card names)")
    p.add_argument("--title", help="with add and --text-file: the title (default: the card's)")
    p.add_argument("--location", help="with add and --text-file: the location, as you read it")
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
    sub.add_parser("migrate", help="apply a pending schema migration, after a verified backup")
    args = parser.parse_args(argv)
    _canonical_args(args)

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
    log.warning("daily run held: the writer lock left since %s belongs to a process that is gone, or cannot be read; "
                "run careeros unlock", since)
    if _notifications_on(paths):
        from careeros.ops.notify import problem
        problem("CareerOS: daily run held", "Run: careeros unlock")


def _mark_interrupted(paths) -> None:
    """`careeros unlock`, once no stale lock is left: runs still marked running were interrupted, as no writer holds the
    lock. Marked failed under a writer lock of its own, like any writer, which also migrates the database: a command can
    take the lock the moment the stale one is removed (Codex CR-2026-11-03-PYTHON-314, F1). With no lock to remove it
    still marks them, so a second `unlock` finishes what a busy one left."""
    lock = SingleWriterLock(paths["lock"])
    try:
        lock.acquire()
    except LockError as exc:
        print(f"interrupted runs not marked: another CareerOS command took the lock first ({exc}). Once it has finished, "
              "run `careeros unlock` again.")
        return
    try:
        store = open_store(paths["db"], Clock())
        try:
            count = store.writer("runner").update("runs", {"status": "failed", "ended_at": store.clock.now()}, "status = 'running'")
        finally:
            store.close()
    finally:
        lock.release()
    print(f"marked {count} interrupted run(s) as failed")


# your own text arguments that become posting content, an id or provenance (the D190 design, section 3a, point 8)
OWNER_TEXT_ARGS = ("location", "company", "employer", "title", "parent", "name")


def _canonical_args(args) -> None:
    """Your text arguments in the canonical form at argument parsing, before they make an id (the manual slug), are
    stored or are recorded as provenance: a composed and a decomposed spelling are one (point 8)."""
    from careeros.normalize.sanitize import canonical
    for name in OWNER_TEXT_ARGS:
        value = getattr(args, name, None)
        if isinstance(value, str):
            setattr(args, name, canonical(value))


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
        try:
            removed = force_unlock(paths["lock"])
        except LockError as exc:   # said plainly, never a traceback (the red team before Codex Pass 15, RT-41)
            if str(exc).startswith("lock_being_written"):
                sys.exit("Not unlocked: a CareerOS command is taking the lock right now; try again in a minute")
            sys.exit("Not unlocked: the CareerOS command that took the lock is still running; let it finish, then run "
                     "careeros unlock only if a lock is left")
        print("removed stale lock" if removed else "no lock present")
        if paths["db"].exists():
            _mark_interrupted(paths)
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
                "report": cmd_report, "review": cmd_review, "label": cmd_label, "labels": cmd_labels, "eval": cmd_eval, "employers": cmd_employers,
                "applied": cmd_applied, "app-status": cmd_app_status, "applications": cmd_applications, "funnel": cmd_funnel, "backup": cmd_backup, "gmail": cmd_gmail, "inbox": cmd_inbox,
                "seen-index": cmd_seen_index, "agency-layout": cmd_agency_layout, "agency-hosts": cmd_agency_hosts,
                "agency-held": cmd_agency_held, "sources": cmd_sources, "audit": cmd_audit, "dismiss": cmd_dismiss, "explain": cmd_explain,
                "paste": cmd_paste, "location": cmd_location, "shortlist": cmd_shortlist, "draft": cmd_draft, "metrics": cmd_metrics,
                "status": cmd_status, "verify": cmd_verify, "export": cmd_export,
                "certifications": cmd_certifications, "release": cmd_release, "package": cmd_package, "assist": cmd_assist,
                "leads": cmd_leads, "notices": cmd_notices, "migrate": cmd_migrate, "offsite": cmd_offsite}
    read_only = ((args.command == "employers" and args.action == "list") or (args.command == "gmail" and args.action != "fetch")
                 or (args.command == "release" and not (args.hold or args.revoke))
                 or (args.command == "package" and args.action in ("show", "list", "gaps"))
                 or (args.command == "assist" and args.action in ("list", "report"))
                 or (args.command == "leads" and args.action == "list")
                 or (args.command == "notices" and args.action == "list"))
    lock = SingleWriterLock(paths["lock"]) if args.command in WRITES and not read_only else None
    ctx = None
    swept = False   # the command's start ran the handoff sweep: its end runs it again, in the finally below
    try:
        if lock:
            lock.acquire()
        # OPP-50: only a command holding the writer lock migrates; `migrate` opens without, as it backs up first
        ctx = _context(paths, with_ai=args.command in ("run", "process", "add-url", "import"), clock=demo_clock,
                       migrate=None if args.command == "migrate" else lock is not None)
        log.info("command %s policy %s ai %s", args.command, ctx.policy.fingerprint[:12], ctx.ai is not None)
        from careeros.core import private_sites
        run = nullcontext() if args.command == "review" else private_sites.run()   # the review page: one per request
        with run:   # one reading of the private settings for the whole command (D141)
            if lock is not None and args.command not in ("init", "migrate", "backup", "offsite"):
                _enroll_reading(ctx.store)
                swept = True
                _command_start(ctx, paths)
            handlers[args.command](ctx, paths, args)
    except CommandStopped as exc:   # the private settings changed during the command: nothing more was done (D153 R2)
        print(f"Stopped: {exc}", file=sys.stderr)
        if args.command == "run":   # the scheduled run has no window
            from careeros.ops.notify import toast
            toast("CareerOS: daily run stopped", "The private settings changed during the run; the next run reads them.")
        sys.exit(2)
    except (MigrationPending, SchemaTooNew) as exc:   # a refusal, not a crash: one line
        print(f"Refused: {exc}", file=sys.stderr)
        if args.command == "run":   # the scheduled run has no window
            from careeros.ops.notify import toast
            toast("CareerOS: daily run refused", str(exc)[:240])
        sys.exit(2)
    except LockError as exc:
        print(f"CareerOS is busy: {exc}")
        if lock is not None and not lock.held:
            _sweep_unlocked(paths, demo_clock)   # the handoffs still go (the coordinator's batch 3 condition 1)
        if args.command == "run" and str(exc).startswith(("stale_lock_held", "lock_unreadable")):
            _held_run(paths)   # an unreadable lock holds the run too, and says so (the red team's gr1-faults-3)
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
        if ctx is not None and swept and lock is not None and lock.held:
            _command_end(ctx, paths)
        if ctx is not None:   # closed here, not when garbage-collected: Python 3.13+ warns of an unclosed database then
            ctx.store.close()
        if lock and lock.held:
            lock.release()


if __name__ == "__main__":
    main()
