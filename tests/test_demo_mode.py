"""Demo Mode (2026-10-13): `careeros demo ...` and CAREEROS_DEMO_MODE=true.

The demo runs the production commands on a throwaway demo vault and a synthetic, offline world. DemoTourTest runs the
four-day tour once, next to a decoy private vault, under a jail that records every attempt to reach the real network,
resolve a real name, start another program, read a credential, open a mailbox or launch a browser. It then proves:
- nothing left the sandbox, and the decoy private vault is untouched (same files, same bytes, same timestamps);
- the tour exercised the production commands and covers every scenario the demo promises;
- repeating the tour gives the same result.
DemoBoundaryTest proves each guard on its own: the private vault cannot be located, shared or nested with the demo
vault; reset only deletes marked demo vaults; Gmail, IMAP, GitHub, credentials, the clipboard, the AI reader and
notifications refuse; a client built in Demo Mode without a synthetic world fails instead of reaching the internet;
and outside Demo Mode nothing changes.
"""
import hashlib
import imaplib
import io
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import webbrowser
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from careeros import cli
from careeros import paths as vault_layout
from careeros.core.credentials import CredentialError, WindowsCredentials
from careeros.demo import runner, sandbox, world
from careeros.discovery import github_inbox, gmail, imap
from careeros.discovery.http import PROVIDER_HOSTS, HttpClient
from careeros.discovery.safe_fetch import SiteFetcher
from careeros.extract import ai
from careeros.ops import notify

# What the four-day tour produces (docs/demo_mode.md, sections 4-5). A change to the synthetic world or to the rules that
# moves any of these numbers must be a deliberate one: update this record and the document together.
RECORDED = {
    "day": 4, "of_days": 4,
    "boards": {"active": 8, "historical": 1, "manual_only": 2, "rejected": 1, "review_required": 1},
    "jobs": {"closed": 4, "duplicate": 1, "rejected": 13, "scored": 27},
    "tiers": {"near_miss": 13, "B": 5, "archive": 9},
    "remote_scopes": {"US_nationwide": 6, "US_unspecified": 34, "unknown": 1, "hybrid": 1, "onsite": 1, "US_restricted_states": 2},
    "review_cases": {"site_unreadable": 1, "board_not_found": 1, "migration_detected": 1, "identity_mismatch": 1},
    "rejections": {"scam_signals": 1, "not_remote": 2, "title_excluded": 4, "state_not_eligible": 1, "below_pay_floor": 3,
                   "no_target_family": 2},
    "board_activity": {"active": 8, "unavailable": 1, "empty": 1},
    "discovery_outcomes": {"verified": 3, "manual_only": 2, "no_qualified_roles": 1, "slug_absent": 1, "skipped_policy": 1},
    "lifecycle_events": {"board_active": 9, "board_quarantined": 3, "identity_mismatch": 1, "employer_rejected": 1,
                         "ats_migration_detected": 1, "validation_scan_passed": 3, "ats_migration_verified": 1},
    "leads": 3, "changes": {"meaningful": 1, "cosmetic": 1}, "night_routed": 13,
}
SECRET = "PRIVATE-SENTINEL-4f9a2c"   # planted in the decoy private vault: must never appear in anything the demo writes; release-scan: fake


def snapshot(root: Path) -> dict:
    """Every file and folder under root, with each file's hash and modification time."""
    return {str(f.relative_to(root)): ("dir" if f.is_dir() else (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns))
            for f in sorted(root.rglob("*"))}


def decoy_private_vault(root: Path) -> None:
    for name, text in {"data/careeros.db": f"not a database {SECRET}", "profile/candidate_profile_v0.yaml": f"name: {SECRET}\n",
                       "policy/overrides.yaml": f"operations:\n  github_inbox: someone/{SECRET}\n",
                       "applications/notes.md": f"Recruiter Jane at Real Employer {SECRET}", "inbox/alert.eml": f"Subject: {SECRET}\n"}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


class Jail:
    """Records, and refuses, every way out of the sandbox. A refusal is an OSError, which the engine may well catch and
    carry on from, so the tests check the record rather than relying on a crash."""

    EXITS = [(httpx.HTTPTransport, "handle_request", "real HTTP"), (socket, "getaddrinfo", "DNS"),
             (socket, "create_connection", "socket"), (socket.socket, "connect", "socket"),
             (subprocess, "Popen", "another program"), (imaplib, "IMAP4_SSL", "IMAP"), (webbrowser, "open", "browser"),
             (WindowsCredentials, "read_entry", "credential store"), (WindowsCredentials, "write", "credential store"),
             (WindowsCredentials, "delete", "credential store"), (ai, "read_api_key", "AI key")]

    def __init__(self):
        self.attempts, self._stack = [], ExitStack()

    def _refuse(self, what):
        def refuse(*args, **kwargs):
            self.attempts.append(what)
            raise OSError(f"jail: {what}")
        return refuse

    def __enter__(self):
        for owner, name, what in self.EXITS:
            self._stack.enter_context(mock.patch.object(owner, name, self._refuse(what)))
        return self

    def __exit__(self, *exc):
        self._stack.close()


def close_log_files(under: Path) -> None:
    """cli.main adds a log file handler per call; close the ones pointing into the test folder so it can be removed."""
    for handler in list(cli.log.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(under):
            handler.close()
            cli.log.removeHandler(handler)


class DemoTourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="careeros-demo-test-"))
        cls.private, cls.demo = cls.tmp / "private-vault", cls.tmp / "demo-vault"
        decoy_private_vault(cls.private)
        env = mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(cls.private), sandbox.VAULT_ENV: str(cls.demo), sandbox.ENV: ""})
        env.start()
        cls.addClassCleanup(env.stop)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(close_log_files, cls.tmp)
        cls.addClassCleanup(sandbox.install, None)
        cls.before = snapshot(cls.private)
        cls.hosts = []
        cls.jail, cls.status, cls.calls = cls.tour()

    @classmethod
    def tour(cls):
        """`careeros demo tour` under the jail; returns the jail, the demo status, and how often each command ran."""
        def spy(handler):
            def record(self, request):
                cls.hosts.append(request.url.host)
                return handler(self, request)
            return record
        with Jail() as jail, ExitStack() as stack, redirect_stdout(io.StringIO()):
            stack.enter_context(mock.patch.object(world.DemoWorld, "ats_handler", spy(world.DemoWorld.ats_handler)))
            stack.enter_context(mock.patch.object(world.DemoWorld, "web_handler", spy(world.DemoWorld.web_handler)))
            run = stack.enter_context(mock.patch.object(cli, "cmd_run", wraps=cli.cmd_run))
            imported = stack.enter_context(mock.patch.object(cli, "cmd_import", wraps=cli.cmd_import))
            cli.main(["demo", "tour"])
            status = runner.status()
        return jail, status, {"run": run.call_count, "import": imported.call_count}

    def store(self):
        ctx = cli._context(runner.paths(), clock=runner.demo_clock())
        self.addCleanup(ctx.store.close)
        return ctx

    # --- nothing left the sandbox --------------------------------------------------------------------------------------
    def test_nothing_reached_the_real_network_programs_credentials_or_mail(self):
        self.assertEqual(self.jail.attempts, [])

    def test_every_request_went_to_the_synthetic_world(self):
        self.assertTrue(self.hosts)
        outside = {h for h in self.hosts if h not in PROVIDER_HOSTS and not h.endswith(".example.com")}
        self.assertEqual(outside, set())
        self.assertFalse(any("linkedin" in h or "indeed" in h or "glassdoor" in h for h in self.hosts))

    def test_private_vault_untouched_and_nothing_from_it_in_the_demo(self):
        self.assertEqual(snapshot(self.private), self.before)
        for f in self.demo.rglob("*"):
            if f.is_file():
                self.assertNotIn(SECRET.encode(), f.read_bytes(), f)

    def test_the_demo_vault_is_marked_and_separate(self):
        self.assertEqual(Path(self.status["vault"]), self.demo.resolve())
        self.assertTrue((self.demo / sandbox.MARKER).exists())
        self.assertFalse(self.demo.resolve().is_relative_to(vault_layout.ENGINE_ROOT))

    # --- the production business logic ran ------------------------------------------------------------------------------
    def test_the_production_commands_ran_each_day(self):
        self.assertEqual(self.calls, {"run": world.DAYS, "import": 1})
        ctx = self.store()
        runs = ctx.store.all("SELECT status, started_at FROM runs ORDER BY started_at")
        self.assertEqual([(r["status"], r["started_at"]) for r in runs], [("completed", world.day_start(d)) for d in range(1, world.DAYS + 1)])
        for day in range(1, world.DAYS + 1):
            self.assertTrue((self.demo / "reports" / f"report-{world.day_start(day)[:10]}.html").exists())

    # --- the scenarios the demo promises ---------------------------------------------------------------------------------
    def test_the_tour_produces_exactly_the_recorded_result(self):
        self.assertEqual(len(world.EMPLOYERS), 15)
        self.assertEqual({k: v for k, v in self.status.items() if k != "vault"}, RECORDED)
        self.assertTrue(40 <= sum(RECORDED["jobs"].values()) <= 60)

    def test_boards_cover_every_verification_outcome(self):
        ctx = self.store()
        by_status = {}
        for r in ctx.store.all("SELECT employer_name, ats, verification_status FROM board_mappings"):
            by_status.setdefault(r["verification_status"], set()).add((r["employer_name"], r["ats"]))
        self.assertEqual({ats for _, ats in by_status["active"]}, {"greenhouse", "lever", "ashby", "workable"})  # supported ATSs
        self.assertIn(("Northwind Benefits", "ashby"), by_status["active"])                    # a newly verified employer
        self.assertEqual({name for name, _ in by_status["manual_only"]}, {"Summit Payer Services", "Lakeview Pharmacy Solutions"})
        self.assertEqual(by_status["rejected"], {("Quillon Health", "greenhouse")})             # identity mismatch
        self.assertEqual(by_status["review_required"], {("Pinecrest Care Navigation", "ashby")})  # manual review
        self.assertEqual(by_status["historical"], {("Harborline Health", "greenhouse")})        # ATS migration: old board kept
        self.assertIn(("Harborline Health", "ashby"), by_status["active"])                     # ... and the new one verified
        self.assertEqual(self.status["discovery_outcomes"]["slug_absent"], 1)                  # invalid employer (Atlas)
        self.assertIn("board_not_found", self.status["review_cases"])
        self.assertEqual(self.status["discovery_outcomes"]["skipped_policy"], 1)               # the staffing agency
        events = self.status["lifecycle_events"]
        for event in ("board_quarantined", "validation_scan_passed", "identity_mismatch", "ats_migration_detected", "ats_migration_verified"):
            self.assertGreaterEqual(events.get(event, 0), 1, event)
        self.assertEqual(self.status["board_activity"]["empty"], 1)                            # Evergreen: real and empty
        empty = ctx.store.one("SELECT verification_status FROM board_mappings WHERE board_activity = 'empty'")
        self.assertEqual(empty["verification_status"], "active")

    def test_jobs_cover_every_outcome(self):
        ctx = self.store()
        outcomes = {}  # (title, employer) -> {(state, reason of the last transition)}
        for r in ctx.store.all("SELECT j.title, j.employer_name, j.current_state, (SELECT reason_code FROM transitions "
                               "WHERE entity_id = j.job_id ORDER BY sequence DESC LIMIT 1) reason FROM jobs j"):
            outcomes.setdefault((r["title"], r["employer_name"]), set()).add((r["current_state"], r["reason"]))
        for title, employer, outcome in (
                ("Director of Pharmacy Operations", "Meridian Rx", ("rejected", "title_excluded")),    # director level
                ("Senior Software Engineer", "Meridian Rx", ("rejected", "title_excluded")),            # poor fit
                ("Office Coordinator", "Northwind Benefits", ("rejected", "no_target_family")),         # poor fit
                ("Healthcare Business Systems Analyst", "Bluepeak Health Plan", ("rejected", "not_remote")),  # hybrid
                ("Reporting Analyst", "Bluepeak Health Plan", ("rejected", "not_remote")),              # onsite
                ("Claims Operations Specialist", "Bluepeak Health Plan", ("rejected", "state_not_eligible")),  # NY/NJ only
                ("Rebate Operations Analyst", "Meridian Rx", ("rejected", "below_pay_floor")),
                ("Remote Claims Analyst", "Brightwater Benefits", ("rejected", "scam_signals")),
                ("Claims Operations Specialist", "Meridian Rx", ("closed", "no_longer_listed")),
                ("PBM Operations Analyst", "Meridian Rx", ("duplicate", "same_requisition"))):
            self.assertIn(outcome, outcomes.get((title, employer), set()), title)
        self.assertEqual(ctx.store.one("SELECT COUNT(*) c FROM jobs WHERE current_state = 'duplicate' "
                                       "AND duplicate_of = 'greenhouse:meridianrx:101'")["c"], 1)
        scored = {r["title"] for r in ctx.store.all("SELECT title FROM jobs WHERE current_state = 'scored'")}
        for kind, title in {"claims": "Claims Analyst II", "benefits": "Benefits Operations Analyst", "BSA": "Pharmacy Benefit Analyst",
                            "reporting": "Senior Reporting Analyst", "operations": "Process Improvement Analyst",
                            "configuration": "Benefit Configuration Analyst", "implementation": "Benefits Implementation Analyst",
                            "payment integrity": "Payment Integrity Analyst", "automation": "Automation Analyst",
                            "PBM": "PBM Client Reporting Analyst"}.items():
            self.assertIn(title, scored, kind)
        scopes = self.status["remote_scopes"]
        for scope in ("US_nationwide", "US_restricted_states", "hybrid", "onsite"):
            self.assertGreaterEqual(scopes.get(scope, 0), 1, scope)
        self.assertEqual(self.status["changes"], {"meaningful": 1, "cosmetic": 1})              # updated jobs
        migrated = ctx.store.all("SELECT title FROM jobs WHERE current_state = 'closed' AND job_id LIKE 'greenhouse:harborline:%'")
        self.assertEqual(len(migrated), 3)                                                       # closed with the retired board
        self.assertGreaterEqual(self.status["night_routed"], 1)
        late = ctx.store.one("SELECT first_seen_at FROM jobs WHERE job_id = 'greenhouse:meridianrx:108'")
        self.assertEqual(late["first_seen_at"], world.day_start(3))                              # a role that appears mid-demo
        self.assertEqual(ctx.store.one("SELECT status FROM leads WHERE site = 'linkedin'")["status"], "new")  # recorded, never fetched

    def test_repeating_the_tour_gives_the_same_result(self):
        again = self.tmp / "demo-vault-2"
        with mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(again)}):
            jail, status, calls = self.tour()
            ctx = cli._context(runner.paths(), clock=runner.demo_clock())
            second = {(r["job_id"], r["current_state"]) for r in ctx.store.all("SELECT job_id, current_state FROM jobs")}
            ctx.store.close()
        first = {(r["job_id"], r["current_state"]) for r in self.store().store.all("SELECT job_id, current_state FROM jobs")}
        self.assertEqual(jail.attempts, [])
        self.assertEqual({k: v for k, v in status.items() if k != "vault"}, {k: v for k, v in self.status.items() if k != "vault"})
        self.assertEqual(second, first)
        self.assertEqual(snapshot(self.private), self.before)

        def report(vault):  # the last day's report; record identifiers (run, review case...) are random by design
            text = (vault / "reports" / f"report-{world.day_start(world.DAYS)[:10]}.html").read_text(encoding="utf-8")
            return re.sub(r"\b(rev|run|evt|exc|att|lm|ent|app):[0-9a-f]{8,32}\b", r"\1:ID", text.replace(str(vault.resolve()), "VAULT"))
        self.assertEqual(report(again), report(self.demo))

    # --- normal commands while Demo Mode is on --------------------------------------------------------------------------
    def test_commands_with_demo_mode_on_use_the_demo_vault(self):
        with mock.patch.dict(os.environ, {sandbox.ENV: "true"}), Jail() as jail, redirect_stdout(io.StringIO()) as out:
            cli.main(["sources", "health"])
            cli.main(["explain", "greenhouse:meridianrx:101"])
            with self.assertRaises(SystemExit) as refused, mock.patch("sys.stderr", io.StringIO()) as err:
                cli.main(["gmail", "status"])
        self.assertEqual(refused.exception.code, 2)   # a refusal is one line, not a traceback (gap G6)
        self.assertEqual(err.getvalue(), "Refused: Gmail is not available in Demo Mode\n")
        text = out.getvalue()
        self.assertIn(f"[Demo Mode: synthetic data in {self.demo.resolve()}]", text)
        self.assertIn("Why greenhouse:meridianrx:101 is where it is", text)
        self.assertIn("official_domain: meridianrx.example.com", text)
        self.assertEqual(jail.attempts, [])
        self.assertEqual(snapshot(self.private), self.before)

    def test_the_reviewer_walkthrough_shows_what_the_commands_print(self):
        """docs/demo_walkthrough.md quotes these lines (Portfolio Prompt 12); each must be real output and in the document."""
        quoted = {("status",): ["jobs: scored=27, rejected=13, closed=4, duplicate=1",
                                "NIGHT profile: on; supplemental/archive=3, supplemental/near_miss=10"],
                  ("sources", "list"): ["greenhouse:harborline historical Harborline Health", "greenhouse:quillon rejected Quillon Health",
                                        "ashby:pinecrest review_required Pinecrest Care Navigation"],
                  ("sources", "explain", "greenhouse:quillon"): ["status=rejected", "identity=mismatch",
                                                                 "board names another company: Quillon Logistics"],
                  ("explain", "greenhouse:meridianrx:102"): ["decision: duplicate", "of: greenhouse:meridianrx:101", "reason: same_requisition"],
                  ("explain", "greenhouse:meridianrx:101"): ["tier: near_miss", "apply_priority: 80.7"],
                  ("shortlist",): ["Pharmacy Claims Analyst | Meridian Rx | open | tier B ELIGIBLE"],
                  ("verify",): ['"event_chain_breaks": []'],
                  ("export",): ["all 78 checks passed"]}
        flat = lambda text: " ".join(text.split())
        walkthrough = flat((vault_layout.ENGINE_ROOT / "docs" / "demo_walkthrough.md").read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {sandbox.ENV: "true"}), Jail() as jail:
            for argv, lines in quoted.items():
                with redirect_stdout(io.StringIO()) as out:
                    cli.main(list(argv))
                for line in lines:
                    self.assertIn(flat(line), flat(out.getvalue()), argv)
                    self.assertIn(flat(line), walkthrough, argv)
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()) as err, redirect_stdout(io.StringIO()):
                cli.main(["gmail", "status"])
        self.assertIn(err.getvalue().strip(), walkthrough)
        self.assertEqual(jail.attempts, [])

    def test_the_seen_index_is_kept_locally_and_never_published(self):
        ctx = self.store()

        class Publishing:  # the demo's policy, plus an inbox repo and publishing switched on
            def get(self, *keys, default=None):
                return {("operations", "github_inbox"): "someone/inbox", ("operations", "publish_seen_index"): True}.get(
                    keys, ctx.policy.get(*keys, default=default))
        publish = mock.Mock()
        with mock.patch.dict(os.environ, {sandbox.ENV: "true"}):
            counts = cli._publish_index(replace(ctx, policy=Publishing()), runner.paths(), runner=publish)
        self.assertEqual(set(counts), {"index_records"})
        publish.assert_not_called()
        self.assertTrue((self.demo / "reports" / "inbox-index").is_dir())

    def test_reported_verified_boards_are_the_boards_verification_admitted(self):
        """Functional MVP gate finding (2026-10-13): the 30-day "verified boards" figure counted the older discovery
        identity flag, which also marks the impostor board verification rejected (Quillon) and the one awaiting review."""
        from careeros.review.discovery_metrics import metrics
        ctx = self.store()
        employers = metrics(ctx.store, ctx.policy, 30)["employers"]
        self.assertEqual(employers["verified"], self.status["boards"]["active"])
        self.assertEqual(employers["awaiting_your_verification"], self.status["boards"]["review_required"])
        report = (self.demo / "reports" / f"report-{world.day_start(world.DAYS)[:10]}.html").read_text(encoding="utf-8")
        self.assertIn(f'{self.status["boards"]["active"]} verified boards', report)


class DemoBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="careeros-demo-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.private = self.tmp / "private-vault"
        decoy_private_vault(self.private)
        self.before = snapshot(self.private)
        self.addCleanup(sandbox.install, sandbox.world())
        env = mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(self.private), sandbox.VAULT_ENV: str(self.tmp / "demo"), sandbox.ENV: "true"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_switch(self):
        for value, on in (("true", True), ("1", True), ("YES", True), (" on ", True), ("", False), ("false", False), ("0", False)):
            with mock.patch.dict(os.environ, {sandbox.ENV: value}):
                self.assertEqual(sandbox.enabled(), on, value)

    def test_outside_demo_mode_nothing_changes(self):
        with mock.patch.dict(os.environ, {sandbox.ENV: ""}):
            self.assertIsNone(sandbox.transport("ats"))
            self.assertIsNone(sandbox.resolver())
            sandbox.refuse("anything")
            self.assertEqual(vault_layout.vault_dir(), self.private.resolve())
            client = HttpClient()
            self.addCleanup(client.close)
            self.assertNotIsInstance(client.client._transport, httpx.MockTransport)

    def test_the_private_vault_cannot_be_located_in_demo_mode(self):
        with self.assertRaises(sandbox.DemoModeViolation):
            vault_layout.vault_dir()
        with self.assertRaises(sandbox.DemoModeViolation):
            vault_layout.vault_paths()

    def test_the_demo_vault_must_be_separate_from_the_private_vault_and_the_code(self):
        for demo in (self.private, self.private / "demo", self.tmp, vault_layout.ENGINE_ROOT / "demo-vault"):
            with mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(demo)}), self.subTest(demo=demo):
                with self.assertRaises(sandbox.DemoModeViolation):
                    sandbox.demo_vault()
        self.assertEqual(sandbox.demo_vault(), (self.tmp / "demo").resolve())

    def test_without_a_synthetic_world_clients_fail_instead_of_reaching_the_internet(self):
        sandbox.install(None)
        with self.assertRaises(sandbox.DemoModeViolation):
            HttpClient()
        with self.assertRaises(sandbox.DemoModeViolation):
            SiteFetcher("meridianrx.example.com")

    def test_with_a_world_names_outside_it_never_resolve(self):
        from careeros.discovery.safe_fetch import SiteFetchError
        sandbox.install(world.DemoWorld(1))
        resolve = sandbox.resolver()
        with Jail() as jail:
            self.assertEqual(resolve("meridianrx.example.com"), [sandbox.DEMO_ADDRESS])
            with self.assertRaises(SiteFetchError):
                resolve("www.example.org")
        self.assertEqual(jail.attempts, [])

    def test_integrations_with_private_sources_refuse(self):
        program, stored = mock.Mock(), mock.Mock()
        with Jail() as jail:
            with self.assertRaises(CredentialError):
                WindowsCredentials()
            with self.assertRaises(sandbox.DemoModeViolation):
                github_inbox._gh(["issue", "list"], runner=program)
            with self.assertRaises(sandbox.DemoModeViolation):
                imap._connect()
            with self.assertRaises(sandbox.DemoModeViolation):
                gmail.GmailClient(stored)
            self.assertFalse(notify.toast("title", "body", runner=program))
        with mock.patch.object(ai, "read_api_key") as key:
            self.assertIsNone(ai.build_reader(SimpleNamespace(get=lambda *keys, default=None: {"enabled": True})))
        self.assertEqual(jail.attempts, [])
        program.assert_not_called()
        stored.read.assert_not_called()
        key.assert_not_called()

    def test_commands_that_read_private_sources_refuse(self):
        ctx = SimpleNamespace(policy=SimpleNamespace(get=lambda *keys, default=None: "someone/inbox"))
        with mock.patch.object(subprocess, "Popen") as program:
            for command, args in ((cli.cmd_gmail, SimpleNamespace(action="status")), (cli.cmd_inbox, SimpleNamespace()),
                                  (cli.cmd_paste, SimpleNamespace(file=None, clipboard=True))):
                with self.subTest(command=command.__name__), self.assertRaises(sandbox.DemoModeViolation):
                    command(ctx, {}, args)
        program.assert_not_called()

    def test_the_daily_run_skips_mail_and_the_github_inbox(self):
        ctx = SimpleNamespace(policy=SimpleNamespace(get=lambda *keys, default=None: "someone/inbox"))
        with mock.patch("careeros.core.credentials.WindowsCredentials") as credentials, mock.patch.object(subprocess, "Popen") as program:
            self.assertEqual(cli._daily_gmail(ctx), Counter())
            self.assertEqual(cli._daily_inbox(ctx), Counter())
        credentials.assert_not_called()
        program.assert_not_called()

    def test_reset_and_init_only_touch_marked_demo_vaults(self):
        folder = self.tmp / "demo"
        self.assertFalse(runner.reset())                                    # nothing there: nothing removed
        folder.mkdir()
        (folder / "keep.txt").write_text("not a demo", encoding="utf-8")
        with self.assertRaises(sandbox.DemoModeViolation):
            runner.reset()
        with self.assertRaises(sandbox.DemoModeViolation):
            runner.init()
        self.assertEqual((folder / "keep.txt").read_text(encoding="utf-8"), "not a demo")
        with self.assertRaises(SystemExit):                                 # run and status need an initialised demo
            runner.run()
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["status"])
        self.assertEqual(snapshot(self.private), self.before)


if __name__ == "__main__":
    unittest.main()
