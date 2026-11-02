"""Observability (the batch of 2026-11-01): CareerOS tells Chris when something is wrong. The label backlog (OPP-01), a
daily run held by a stale writer lock (OPP-05), the last completed run (OPP-06), failure notices (OPP-54), pursued
postings that closed (OPP-36), the daily log opened before the engine loads (OPP-52) and redacted (OPP-70), and closed
postings out of the shortlist (ticket 1). Synthetic data only; no real notification is shown, and the processes started
here use a temporary vault."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.core.clock import FixedClock, format_utc, parse_utc
from careeros.core.redact import redact
from careeros.db.store import open_store
from careeros.discovery.http import FetchError
from careeros.ops import daily_log, notify
from careeros.ops.notify import summary
from careeros.paths import ENGINE_ROOT
from careeros.pipeline import Context
from careeros.review.dismiss import dismiss
from careeros.review.labels import label_backlog, record_label
from careeros.review.report import build_report, run_health
from careeros.review.shortlist import shortlist, shortlisted_job_ids

from tests.helpers import JOB_MATRIX, add_job
from tests.test_label_findings import PROFILE
from tests.test_saved import POLICY
from tests.test_v21_policy import policy_with

NOW = "2026-11-01T12:00:00.000Z"


def ago(days: float = 0, hours: float = 0) -> str:
    return format_utc(parse_utc(NOW) - timedelta(days=days, hours=hours))


def scored(store, job_id: str, tier: str, days: float = 0, **job) -> str:
    add_job(store, job_id, current_state=job.pop("current_state", "scored"), first_seen_at=ago(days), **job)
    store.writer("qualify").insert("scores", {
        "score_id": f"s:{job_id}", "job_id": job_id, "at": store.clock.now(), "profile_fingerprint": "p", "policy_fingerprint": "q",
        "eligibility": 90, "candidate_fit": 80, "career_value": 70, "freshness": 90, "apply_priority": 80, "tier": tier,
        "interview_band": "Moderate", "components_json": json.dumps({"eligibility": {"matched_skills": [], "missing_skills": []},
                                                                     "candidate_fit": {"matched_skills": []}})})
    return job_id


class StatusMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = {"lock": Path(self.tmp.name) / "data" / "writer.lock"}
        self.store = open_store(":memory:", FixedClock(NOW), "test-policy")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def status(self) -> str:
        ctx = Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None)
        out = io.StringIO()
        with mock.patch.object(cli, "_ai_status", lambda ctx: "off"), redirect_stdout(out):
            cli.cmd_status(ctx, self.paths, SimpleNamespace())
        return out.getvalue()


class LabelBacklogTests(StatusMixin, unittest.TestCase):
    """OPP-01: qualified postings that wait for a label are counted in one place and shown in the report header,
    `careeros status` and, once the oldest has waited 7 days, the daily notification."""

    def test_only_qualified_unlabelled_open_postings_wait(self):
        store = self.store
        scored(store, "gh:a:old", "A", days=9)
        scored(store, "gh:a:b", "B", days=2)
        scored(store, "gh:a:near", "near_miss", days=1)
        record_label(store, scored(store, "gh:a:night", "B", days=12), "apply", None, key="k-night", profile="night")  # NIGHT only
        scored(store, "gh:x:archive", "archive", days=20)
        scored(store, "gh:x:variant", "A", days=20, variant_of="gh:a:old")
        scored(store, "gh:x:duplicate", "A", days=20, current_state="duplicate")
        scored(store, "gh:x:closed", "A", days=20, current_state="closed")
        record_label(store, scored(store, "gh:x:labelled", "A", days=20), "skip", None, key="k-skip")
        dismiss(store, scored(store, "gh:x:dismissed", "B", days=20), "pay")
        add_job(store, "gh:x:unscored", current_state="eligible", first_seen_at=ago(30))
        self.assertEqual(label_backlog(store), {"count": 4, "oldest_days": 12,
                                                "text": "4 qualified postings await your label; oldest 12 days"})
        record_label(store, "gh:a:night", "stretch", None, key="k-primary")
        self.assertEqual(label_backlog(store)["text"], "3 qualified postings await your label; oldest 9 days")

    def test_one_posting_and_none(self):
        self.assertIsNone(label_backlog(self.store))
        scored(self.store, "gh:a:1", "A", days=1.5)
        self.assertEqual(label_backlog(self.store)["text"], "1 qualified posting awaits your label; oldest 1 day")

    def test_the_report_header_and_status_show_it(self):
        self.assertIn("label backlog: none", self.status())
        self.assertNotIn("await your label", build_report(self.store, PROFILE, POLICY))
        scored(self.store, "gh:a:1", "A", days=3)
        scored(self.store, "gh:a:2", "near_miss", days=1)
        page = build_report(self.store, PROFILE, POLICY)
        line = "2 qualified postings await your label; oldest 3 days"
        self.assertIn(f"<b>{line}</b>", page[:page.index("<h2")])   # in the header, above every section
        self.assertIn(f"label backlog: {line}", self.status())

    def test_the_notification_names_a_backlog_once_the_oldest_has_waited_7_days(self):
        started = ago(hours=1)
        scored(self.store, "gh:a:1", "A", days=6.9)
        self.assertIsNone(summary(self.store, started))   # a quiet day stays silent while the oldest is under 7 days
        scored(self.store, "gh:a:2", "B", days=7)
        self.assertEqual(summary(self.store, started), ("CareerOS: 2 qualified postings await your label; oldest 7 days",
                                                        "Open the review page to see them."))
        scored(self.store, "gh:new:1", "A", title="Claims Analyst", employer_name="Synth Health")   # found during this run
        title, body = summary(self.store, started)
        self.assertEqual(title, "CareerOS: 1 new job worth a look")
        self.assertEqual(body, "3 qualified postings await your label; oldest 7 days. Claims Analyst (Synth Health)")


def stale_lock(path: Path, pid: int = 999999, at: str = "2026-11-01T06:00:00.000Z") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "nonce": "left-behind"}), encoding="utf-8")
    moment = parse_utc(at).timestamp()
    os.utime(path, (moment, moment))
    return path


class HeldRunTests(StatusMixin, unittest.TestCase):
    """OPP-05: a run killed by Task Scheduler leaves the writer lock behind, and every later 07:00 run stops at it. The
    held run now shows a notification and logs it, and `careeros status` says HELD; nothing removes the lock."""

    def run_cli(self, overrides: str = "{}\n") -> tuple[int, list, str]:
        vault = Path(self.tmp.name) / "vault"
        (vault / "policy").mkdir(parents=True, exist_ok=True)
        (vault / "policy" / "overrides.yaml").write_text(overrides, encoding="utf-8")
        shown = []
        try:
            with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(vault)}), redirect_stdout(io.StringIO()), \
                    mock.patch("careeros.ops.notify.toast", lambda title, body, **kw: shown.append((title, body)) or True):
                with self.assertRaises(SystemExit) as stop:
                    cli.main(["run"])
        finally:
            for handler in list(cli.log.handlers):   # release the temporary log file
                handler.close()
                cli.log.removeHandler(handler)
        log = vault / "logs" / "careeros.log"
        return stop.exception.code, shown, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_a_held_daily_run_shows_a_notification_and_logs_it_and_leaves_the_lock(self):
        lock = stale_lock(Path(self.tmp.name) / "vault" / "data" / "writer.lock")
        code, shown, log = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(shown, [("CareerOS: daily run held", "Run: careeros unlock")])
        self.assertIn("daily run held: the writer lock left since 2026-11-01T06:00:00.000Z", log)
        self.assertTrue(lock.exists())

    def test_notifications_off_still_logs_and_a_live_writer_is_not_a_held_run(self):
        lock = stale_lock(Path(self.tmp.name) / "vault" / "data" / "writer.lock")
        code, shown, log = self.run_cli("operations:\n  notifications: false\n")
        self.assertEqual((code, shown), (1, []))
        self.assertIn("daily run held", log)
        stale_lock(lock, pid=os.getpid())   # a run in progress: busy, not held
        code, shown, log = self.run_cli()
        self.assertEqual((code, shown), (1, []))
        self.assertTrue(lock.exists())

    def test_status_says_held_since_the_lock_time_only_when_its_process_is_gone(self):
        self.assertNotIn("HELD", self.status())
        stale_lock(self.paths["lock"], pid=os.getpid())
        self.assertNotIn("HELD", self.status())
        stale_lock(self.paths["lock"])
        first = self.status().splitlines()[0]
        self.assertTrue(first.startswith("HELD since 2026-11-01T06:00:00.000Z: "), first)
        self.assertIn("careeros unlock", first)
        self.paths["lock"].write_text("not json", encoding="utf-8")
        self.assertIn("the writer lock is unreadable", self.status())
        self.assertTrue(self.paths["lock"].exists())


class LastRunTests(StatusMixin, unittest.TestCase):
    """OPP-06: when the last daily run completed, and a warning when none has for more than 26 hours or the latest
    failed, in the report header and `careeros status`."""

    def run_row(self, status: str, started: str, ended: str | None = None) -> None:
        self.store.writer("runner").insert("runs", {"run_id": f"run:{started}", "kind": "daily", "started_at": started,
                                                    "ended_at": ended, "status": status})

    def header(self) -> str:
        page = build_report(self.store, PROFILE, POLICY)
        return page[:page.index("<h2")]

    def test_the_last_completed_run_and_its_age(self):
        self.assertEqual(run_health(self.store), ("last completed run: none yet", None))   # a new vault: nothing to warn about
        self.run_row("completed", ago(hours=27), ago(hours=26.5))
        self.run_row("completed", ago(hours=2.5), ago(hours=2))
        self.assertEqual(run_health(self.store), ("last completed run: 2026-11-01 10:00 UTC (2 h ago)", None))
        self.assertIn("last completed run: 2026-11-01 10:00 UTC (2 h ago)", self.header())
        self.assertNotIn('class="banner"', self.header())
        status = self.status()
        self.assertIn("last completed run: 2026-11-01 10:00 UTC (2 h ago)", status)
        self.assertNotIn("!!", status)

    def test_a_warning_once_no_run_has_completed_for_more_than_26_hours(self):
        self.run_row("completed", ago(hours=26.5), ago(hours=26))
        self.assertIsNone(run_health(self.store)[1])   # 26 hours exactly: not yet
        self.store.clock.advance(hours=4)
        self.run_row("running", ago(hours=1))   # started, never finished (a stopped run)
        line, warning = run_health(self.store)
        self.assertEqual(line, "last completed run: 2026-10-31 10:00 UTC (30 h ago)")
        self.assertEqual(warning, "No daily run has completed for 30 hours. Check Task Scheduler and careeros status.")
        self.assertIn(f'<div class="banner"><b>{warning}</b></div>', self.header())
        self.assertIn(f"!! {warning}", self.status().splitlines()[0])

    def test_a_warning_when_the_latest_run_failed(self):
        self.run_row("completed", ago(hours=3), ago(hours=2.5))
        self.run_row("failed", ago(hours=1), ago(hours=0.5))
        warning = "The latest daily run failed (started 2026-11-01 11:00 UTC). Details are in vault\\logs\\daily-run.log."
        self.assertEqual(run_health(self.store), ("last completed run: 2026-11-01 09:30 UTC (2 h ago)", warning))
        self.assertIn("The latest daily run failed (started 2026-11-01 11:00 UTC)", self.header())
        self.assertIn(f"!! {warning}", self.status())
        self.run_row("completed", ago(hours=0.2), ago(hours=0.1))   # the next run completes: the warning goes
        self.assertIsNone(run_health(self.store)[1])


class SavedReportHealthTests(StatusMixin, unittest.TestCase):
    """Codex CR-2026-11-01-OBS-REVIEW, F5: the report a daily run saves shows that run's own health. The run writes it
    just before it is marked completed, so the header counts the run as completed then; a run whose report cannot be
    written is recorded as failed, as before."""

    def run_row(self, status: str, started: str, ended: str) -> None:
        self.store.writer("runner").insert("runs", {"run_id": f"run:{started}", "kind": "daily", "started_at": started,
                                                    "ended_at": ended, "status": status})

    def daily_run(self, report_fails: bool = False) -> Path:
        """`careeros run` itself, with the work stages doing nothing; the report goes to the temporary folder."""
        paths = {"reports": Path(self.tmp.name) / "reports"}
        with ExitStack() as stack:
            for name in ("cmd_discover", "_daily_gmail", "_daily_inbox", "_daily_discovery", "_board_maintenance", "cmd_process",
                         "_publish_index"):
                stack.enter_context(mock.patch.object(cli, name, return_value=Counter()))
            stack.enter_context(mock.patch.object(cli, "_ai_status", return_value="off"))
            stack.enter_context(mock.patch.object(cli, "_after_run"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if report_fails:
                stack.enter_context(mock.patch.object(cli, "build_report", side_effect=RuntimeError("disk full")))
            cli.cmd_run(Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None), paths, SimpleNamespace())
        return paths["reports"] / f"report-{self.store.clock.now()[:10]}.html"

    def saved_header(self) -> str:
        page = self.daily_run().read_text(encoding="utf-8")
        return page[:page.index("<h2")]

    def test_the_first_successful_run_saves_its_own_health(self):
        header = self.saved_header()
        self.assertIn("last completed run: 2026-11-01 12:00 UTC (0 h ago)", header)
        self.assertNotIn("none yet", header)
        self.assertNotIn('class="banner"', header)
        run = self.store.one("SELECT status, ended_at FROM runs")
        self.assertEqual(tuple(run), ("completed", NOW))   # the time the header shows
        self.assertEqual(run_health(self.store), ("last completed run: 2026-11-01 12:00 UTC (0 h ago)", None))

    def test_a_run_after_a_gap_or_a_failure_saves_no_stale_warning(self):
        self.run_row("completed", ago(hours=49), ago(hours=48))   # Codex's case: recovery after 48 hours
        header = self.saved_header()
        self.assertIn("last completed run: 2026-11-01 12:00 UTC (0 h ago)", header)
        self.assertNotIn("No daily run has completed", header)
        self.assertNotIn('class="banner"', header)
        self.store.clock.advance(days=1)
        self.run_row("failed", ago(hours=1), ago(hours=0.5))   # yesterday's run failed; this one recovers
        header = self.saved_header()
        self.assertIn("last completed run: 2026-11-02 12:00 UTC (0 h ago)", header)
        self.assertNotIn("The latest daily run failed", header)
        self.assertEqual(run_health(self.store)[1], None)

    def test_a_run_whose_report_fails_is_recorded_failed(self):
        self.run_row("completed", ago(hours=25), ago(hours=24))
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            self.daily_run(report_fails=True)
        self.assertFalse((Path(self.tmp.name) / "reports" / f"report-{NOW[:10]}.html").exists())
        latest = self.store.one("SELECT status, ended_at FROM runs ORDER BY rowid DESC LIMIT 1")
        self.assertEqual(tuple(latest), ("failed", NOW))   # never marked completed first
        line, warning = run_health(self.store)
        self.assertEqual(line, "last completed run: 2026-10-31 12:00 UTC (24 h ago)")
        self.assertEqual(warning, "The latest daily run failed (started 2026-11-01 12:00 UTC). Details are in "
                                  "vault\\logs\\daily-run.log.")


class FailureNoticeTests(StatusMixin, unittest.TestCase):
    """OPP-54: a failed backup, a provider turning degraded, a held run (HeldRunTests) and a run where every board failed
    each show a notice through notify.problem, the summary's own path; never with notifications off."""

    def setUp(self):
        super().setUp()
        self.shown = []
        patcher = mock.patch("careeros.ops.notify.toast", lambda title, body, **kw: self.shown.append((title, body)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, notifications: bool = True) -> Context:
        return Context(self.store, policy_with(operations__notifications=notifications), PROFILE, JOB_MATRIX, None, None)

    def test_problem_shows_through_toast_within_the_same_limits(self):
        self.assertTrue(notify.problem("t" * 200, "b" * 500))
        self.assertEqual(self.shown, [("t" * notify.TITLE_LIMIT, "b" * notify.BODY_LIMIT)])

    def test_a_failed_backup_shows_a_notice(self):
        with mock.patch("careeros.ops.backup.backup", side_effect=OSError("disk full")), redirect_stdout(io.StringIO()), \
                self.assertLogs("careeros", "ERROR"):
            cli._after_run(self.ctx(), {}, NOW)
            self.assertEqual(self.shown, [("CareerOS: daily backup failed", "Details are in vault\\logs\\careeros.log")])
            self.shown.clear()
            cli._after_run(self.ctx(notifications=False), {}, NOW)
        self.assertEqual(self.shown, [])
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM exceptions WHERE error_code = 'backup_failed'")["c"], 2)

    def test_a_provider_turning_degraded_shows_one_notice_and_only_when_it_changes(self):
        from tests.test_board_verification import scan, setup
        store, ctx = setup()
        for token in ("a1", "a2", "a3", "a4", "ok"):
            store.writer("admin").insert("employers", {"employer_id": f"greenhouse:{token}", "name": f"Board {token}",
                                                       "parent": f"Board {token}", "ats": "greenhouse", "board_token": token,
                                                       "status": "verified", "admitted_via": "legacy"})
        outage = lambda token: [] if token == "ok" else FetchError("network:ConnectError", None, retryable=True)
        with redirect_stdout(io.StringIO()):
            scan(ctx, outage)
            self.assertEqual(self.shown, [("CareerOS: job-board provider degraded", "greenhouse (4 of 5 boards failed). No employer is "
                                                                                    "penalised. See: careeros sources providers")])
            store.clock.advance(days=1)
            scan(ctx, outage)   # still degraded: not a change, no second notice
        self.assertEqual(len(self.shown), 1)

    def test_a_run_where_every_board_failed_shows_a_notice(self):
        def run(scanned: Counter, notifications: bool = True) -> list:
            self.shown.clear()
            with ExitStack() as stack:   # the rest of the run does nothing here
                for name in ("_daily_gmail", "_daily_inbox", "_daily_discovery", "_board_maintenance", "cmd_process", "_publish_index"):
                    stack.enter_context(mock.patch.object(cli, name, return_value=Counter()))
                for name in ("cmd_report", "_after_run"):
                    stack.enter_context(mock.patch.object(cli, name))
                stack.enter_context(mock.patch.object(cli, "_ai_status", return_value="off"))
                stack.enter_context(mock.patch.object(cli, "cmd_discover", return_value=scanned))
                stack.enter_context(redirect_stdout(io.StringIO()))
                cli.cmd_run(self.ctx(notifications), {}, SimpleNamespace())
            return self.shown[:]
        self.assertEqual(run(Counter(board_scans_attempted=3, board_errors=3)),
                         [("CareerOS: every job board failed", "All 3 boards scanned this run failed (no network?). Details are in "
                                                               "vault\\logs\\daily-run.log")])
        self.assertEqual(run(Counter(board_scans_attempted=3, board_errors=2)), [])
        self.assertEqual(run(Counter(board_scans_attempted=0, board_errors=0)), [])   # nothing was due: not a failure
        self.assertEqual(run(Counter(board_scans_attempted=3, board_errors=3), notifications=False), [])
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM runs WHERE status = 'completed'")["c"], 4)


class PursuedClosureTests(StatusMixin, unittest.TestCase):
    """OPP-36: the 07:00 summary counts the postings you chose to pursue (latest label Apply or Stretch) that closed
    without an application since the last completed run, even on a day with nothing new. Counts only, no names."""

    def pursued(self, job_id: str, label: str = "apply", closed_hours: float | None = None, **job) -> str:
        add_job(self.store, job_id, current_state="closed" if closed_hours is not None else "scored",
                closed_at=ago(hours=closed_hours) if closed_hours is not None else None, **job)
        record_label(self.store, job_id, label, None, key=f"k:{job_id}:{label}")
        return job_id

    def run_row(self, status: str, started: str, ended: str) -> None:
        self.store.writer("runner").insert("runs", {"run_id": f"run:{started}", "kind": "daily", "started_at": started,
                                                    "ended_at": ended, "status": status})

    def test_pursued_postings_closed_since_the_last_completed_run_are_counted_on_a_quiet_day(self):
        since = ago(hours=1)   # this run's start
        self.run_row("completed", ago(hours=25), ago(hours=24))   # the previous completed run
        self.run_row("failed", ago(hours=13), ago(hours=12))      # a failed run sent no summary: its closures still count
        self.run_row("completed", since, ago(hours=0.1))          # this run, completed before its summary
        self.pursued("gh:p:1", "apply", closed_hours=12.5)
        self.pursued("gh:p:2", "stretch", closed_hours=0.5)       # closed during this run
        self.pursued("gh:p:old", "apply", closed_hours=30)        # closed before the previous run ended: counted then
        self.pursued("gh:p:open", "apply")                        # still open
        self.pursued("gh:p:skip", "apply", closed_hours=2)
        record_label(self.store, "gh:p:skip", "skip", None, key="k:later-skip")   # your latest label wins
        add_job(self.store, "gh:p:night", current_state="closed", closed_at=ago(hours=2))
        record_label(self.store, "gh:p:night", "apply", None, key="k:night", profile="night")   # NIGHT labels are not PRIMARY
        self.pursued("gh:p:applied", "apply", closed_hours=2)
        add_job(self.store, "gh:p:applied-variant", variant_of="gh:p:applied")
        self.store.writer("tracker").insert("applications", {   # an application to the same role, on its variant
            "application_id": "app:1", "workflow_id": "application:app:1", "job_id": "gh:p:applied-variant", "channel": "direct",
            "applied_at": ago(days=3), "first_seen_at": ago(days=3), "current_state": "applied", "entered_state_at": ago(days=3)})
        self.assertEqual(notify.pursued_closures(self.store, since), 2)
        self.assertEqual(summary(self.store, since), ("CareerOS: 2 pursued postings closed since the last run",
                                                      "Open the review page to see them."))

    def test_a_posting_you_dismissed_is_left_out_until_reinstated(self):
        """The latest dismissal event decides (ADR 0007, amendment of 2026-11-01): a pursued posting you dismissed is not
        announced when it closes; after a reinstatement it counts again."""
        from careeros.review.dismiss import dismiss, reinstate
        since = ago(hours=1)
        self.pursued("gh:p:kept", "apply", closed_hours=0.5)
        self.pursued("gh:p:dismissed", "apply", closed_hours=0.5)
        dismiss(self.store, "gh:p:dismissed", "pay")
        self.assertEqual(notify.pursued_closures(self.store, since), 1)
        reinstate(self.store, "gh:p:dismissed")
        self.assertEqual(notify.pursued_closures(self.store, since), 2)

    def test_a_void_application_does_not_hide_a_closure(self):
        """A void application (recorded by mistake, ADR 0007 amendment of 2026-11-01) is no application: the closure of a
        pursued posting whose only application is void is announced."""
        from careeros.review.applications import VOID, application_matrix, record_application, update_application
        since = ago(hours=1)
        self.pursued("gh:p:void", "apply", closed_hours=0.5)
        app = record_application(self.store, application_matrix(), "gh:p:void", "direct", policy=POLICY, key="a1")
        self.assertEqual(notify.pursued_closures(self.store, since), 0)
        update_application(self.store, application_matrix(), app, VOID, reason="wrong posting", key="v1")
        self.assertEqual(notify.pursued_closures(self.store, since), 1)

    def test_without_a_previous_run_only_this_run_counts_and_nothing_closed_stays_silent(self):
        since = ago(hours=1)
        self.pursued("gh:p:before", "apply", closed_hours=3)
        self.assertEqual(notify.pursued_closures(self.store, since), 0)
        self.assertIsNone(summary(self.store, since))
        self.pursued("gh:p:now", "apply", closed_hours=0.5)
        self.assertEqual(summary(self.store, since)[0], "CareerOS: 1 pursued posting closed since the last run")

    def test_it_leads_the_text_when_there_is_news(self):
        since = ago(hours=1)
        self.pursued("gh:p:now", "stretch", closed_hours=0.5)
        scored(self.store, "gh:new:1", "A", title="Claims Analyst", employer_name="Synth Health")
        scored(self.store, "gh:old:1", "B", days=8)
        self.assertEqual(summary(self.store, since), (
            "CareerOS: 1 new job worth a look",
            "1 pursued posting closed since the last run. 2 qualified postings await your label; oldest 8 days. "
            "Claims Analyst (Synth Health)"))


class DailyLogTests(unittest.TestCase):
    """OPP-52: `python -m careeros run --output-file FILE` (the scheduled task) points its output at the file before the
    engine is imported, so a broken install or a bad vault path leaves a trace there; otherwise the log is as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "daily-run.log"

    def careeros(self, *argv: str, code: str | None = None, vault: Path | None = None) -> subprocess.CompletedProcess:
        """Starts CareerOS in another process, from this tree, as the scheduled task does; never with the real vault."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAREEROS_DEMO")}
        env["CAREEROS_VAULT"] = str(vault or Path(self.tmp.name) / "vault")
        command = [sys.executable, "-c", code, *argv] if code else [sys.executable, "-m", "careeros", *argv]
        return subprocess.run(command, cwd=ENGINE_ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)

    def test_an_engine_that_fails_to_import_leaves_its_traceback_in_the_log(self):
        broken = ("import runpy, sys; sys.modules['careeros.cli'] = None; "   # a broken install: the engine cannot be imported
                  "runpy.run_module('careeros', run_name='__main__')")
        done = self.careeros("run", "--output-file", str(self.log), code=broken)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "")   # nothing is lost to a window the scheduled task does not have
        text = self.log.read_text(encoding="utf-8")
        self.assertRegex(text, r"^=== \d{4}-\d\d-\d\dT[\d:.]+Z careeros: the engine failed to load ===\n")
        self.assertIn("ModuleNotFoundError", text)

    def test_a_vault_path_that_is_refused_leaves_its_traceback_in_the_log(self):
        done = self.careeros("run", "--output-file", str(self.log), vault=ENGINE_ROOT / "vault-inside-the-engine")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "")
        self.assertIn("vault_inside_engine_repo", self.log.read_text(encoding="utf-8"))
        self.assertFalse((ENGINE_ROOT / "vault-inside-the-engine").exists())

    def test_a_run_that_starts_writes_one_entry_as_before(self):
        vault = Path(self.tmp.name) / "vault"
        (vault / "policy").mkdir(parents=True)
        (vault / "policy" / "overrides.yaml").write_text("operations:\n  notifications: false\n", encoding="utf-8")
        stale_lock(vault / "data" / "writer.lock")   # the run stops at once: held
        done = self.careeros("run", "--output-file", str(self.log))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", ""))
        text = self.log.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^=== .* careeros run ===$", text, re.M)), 1, text)
        self.assertIn("CareerOS is busy: stale_lock_held", text)

    def test_only_a_run_with_an_output_file_is_redirected(self):
        self.assertEqual(daily_log.requested(["run", "--output-file", "a.log"]), "a.log")
        self.assertEqual(daily_log.requested(["run", "--output-file=b.log"]), "b.log")
        for argv in (["run"], ["status"], ["review", "--output-file", "a.log"], [], ["run", "--output-file"]):
            self.assertIsNone(daily_log.requested(argv), argv)

    def test_the_file_is_opened_once_for_both_streams(self):
        saved = sys.stdout, sys.stderr
        try:
            daily_log.redirect(str(self.log))
            first = sys.stdout
            daily_log.redirect(str(self.log))   # cli.py after __main__.py
            self.assertIs(sys.stdout, first)
            self.assertIs(sys.stderr, first)
        finally:
            sys.stdout, sys.stderr = saved
            first.close()


class RedactedLogTests(unittest.TestCase):
    """OPP-70: daily-run.log, the file pasted into an AI session when a run fails, is redacted like careeros.log: printed
    lines and tracebacks go through careeros/core/redact.py."""
    FAKE_KEY = "sk-" + "ant-" + "abcdefghijk"   # built at runtime so the repository never holds a key-shaped string

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "daily-run.log"

    def test_the_writer_redacts_whole_lines_and_holds_a_line_until_it_ends(self):
        out = io.StringIO()
        writer = daily_log.RedactingWriter(out)
        writer.write("mail jane.doe@exa")
        self.assertEqual(out.getvalue(), "")   # half an address is held, never redacted on its own
        writer.write(f"mple.com\nkey {self.FAKE_KEY} job gh:acme:4012345678\npartial (512) 555-")
        writer.write("0147")
        writer.flush()   # an ordinary flush keeps the unfinished line (Codex CR-2026-11-01-OBS-REVIEW, F1)
        self.assertEqual(out.getvalue(), "mail [REDACTED_EMAIL]\nkey [REDACTED_KEY] job gh:acme:4012345678\n")
        writer.finish()   # nothing more can follow: the last line, redacted
        self.assertEqual(out.getvalue(), "mail [REDACTED_EMAIL]\nkey [REDACTED_KEY] job gh:acme:4012345678\npartial [REDACTED_PHONE]")

    # F1 (Codex CR-2026-11-01-OBS-REVIEW): synthetic values only, built at runtime so the repository never holds them whole
    SECRETS = {"email": "probe.person" + "@example.com", "key": "sk-" + "ant-" + "abcdefghijkLMNOP",
               "token": "Bearer " + "AbCdEf0123456789", "phone": "(512) " + "555-0147", "ssn": "123-" + "45-6789"}

    def test_a_flush_in_the_middle_of_a_line_never_writes_half_a_secret(self):
        """write + flush + write, at every cut point and in several chunkings: the line is written once, whole and
        redacted, and nothing at all before its end."""
        chunkings = {
            "one flush": lambda s, n: [s[:n], None, s[n:]],
            "flush after each piece": lambda s, n: [s[:n], None, s[n:], None],
            "two flushes": lambda s, n: [s[:n], None, None, s[n:]],
            "three pieces": lambda s, n: [s[:max(1, n // 2)], None, s[max(1, n // 2):n], None, s[n:]],
            "a character at a time": lambda s, n: [x for ch in s for x in (ch, None)],
        }
        for label, secret in self.SECRETS.items():
            line = f"context {secret} end"
            for name, pieces in chunkings.items():
                for n in range(1, len(secret)):
                    with self.subTest(secret=label, chunking=name, cut=n):
                        out = io.StringIO()
                        writer = daily_log.RedactingWriter(out)
                        writer.write("context ")
                        for piece in pieces(secret, n):
                            writer.flush() if piece is None else writer.write(piece)
                            self.assertEqual(out.getvalue(), "")   # the line has not ended: nothing is written
                        writer.write(" end\n")
                        self.assertEqual(out.getvalue(), redact(line) + "\n")
                        self.assertNotIn(secret, out.getvalue())

    def test_print_pieces_with_flush_true_are_redacted_whole(self):
        for label, secret in self.SECRETS.items():
            with self.subTest(secret=label):
                out = io.StringIO()
                writer = daily_log.RedactingWriter(out)
                cut = len(secret) // 2
                print("contact=" + secret[:cut], end="", flush=True, file=writer)
                print(secret[cut:-1], end="", flush=True, file=writer)
                self.assertEqual(out.getvalue(), "")
                print(secret[-1:], file=writer)
                self.assertEqual(out.getvalue(), redact("contact=" + secret) + "\n")
                self.assertNotIn(secret, out.getvalue())

    def test_a_plain_line_still_appears_at_once(self):
        out = io.StringIO()
        writer = daily_log.RedactingWriter(out)
        print("Discovering...", file=writer)
        self.assertEqual(out.getvalue(), "Discovering...\n")   # no flush needed, none held
        print("  board_scans_attempted=3", file=writer, flush=True)
        self.assertEqual(out.getvalue(), "Discovering...\n  board_scans_attempted=3\n")

    def test_close_writes_the_unfinished_last_line_redacted_once(self):
        for label, secret in self.SECRETS.items():
            with self.subTest(secret=label):
                path = Path(self.tmp.name) / f"close-{label}.log"
                writer = daily_log.RedactingWriter(open(path, "a", encoding="utf-8"))
                writer.write("done\nlast " + secret[:3])
                writer.flush()
                writer.write(secret[3:])
                writer.flush()
                writer.close()
                writer.close()   # twice: nothing more
                self.assertEqual(path.read_text(encoding="utf-8"), "done\n" + redact("last " + secret))

    def unfinished_line_at_exit(self, ending: str) -> tuple[subprocess.CompletedProcess, str]:
        """A process that redirects to the log, prints the last line in pieces with flush=True and never ends it, then
        ends as `ending` says. Only careeros.ops.daily_log is imported: no engine, no vault."""
        email, key = self.SECRETS["email"], self.SECRETS["key"]
        code = ("import sys\nfrom careeros.ops import daily_log\n"
                f"daily_log.redirect({str(self.log)!r})\n"
                "print('a whole line')\n"
                f"print('last ' + {email[:9]!r}, end='', flush=True)\n"
                f"print({email[9:]!r} + ' ' + {key[:6]!r}, end='', flush=True)\n"
                f"print({key[6:]!r}, end='', flush=True)\n" + ending)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAREEROS_DEMO")}
        env["CAREEROS_VAULT"] = str(Path(self.tmp.name) / "vault")
        done = subprocess.run([sys.executable, "-c", code], cwd=ENGINE_ROOT, env=env, capture_output=True, text=True,
                              timeout=120, check=False)
        return done, self.log.read_text(encoding="utf-8")

    def test_the_unfinished_last_line_is_written_redacted_at_a_normal_exit(self):
        done, text = self.unfinished_line_at_exit("")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(text, "a whole line\nlast [REDACTED_EMAIL] [REDACTED_KEY]")
        for secret in (self.SECRETS["email"], self.SECRETS["key"]):
            self.assertNotIn(secret, text)

    def test_the_unfinished_last_line_is_written_redacted_after_an_exception(self):
        done, text = self.unfinished_line_at_exit("raise SystemExit(3)\n")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "", ""))
        self.assertEqual(text, "a whole line\nlast [REDACTED_EMAIL] [REDACTED_KEY]")
        email = self.SECRETS["email"]
        done, text = self.unfinished_line_at_exit(f"raise RuntimeError('cannot reach ' + {email[:5]!r} + {email[5:]!r})\n")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", ""))
        self.assertTrue(text.startswith("a whole line\nlast [REDACTED_EMAIL] [REDACTED_KEY]"), text)
        self.assertIn("RuntimeError: cannot reach [REDACTED_EMAIL]\n", text)   # the traceback, as ever
        for secret in self.SECRETS.values():
            self.assertNotIn(secret, text)

    def test_a_failed_run_s_traceback_in_daily_run_log_is_redacted(self):
        vault = Path(self.tmp.name) / "vault"
        (vault / "policy").mkdir(parents=True)
        (vault / "policy" / "overrides.yaml").write_text("{}\n", encoding="utf-8")
        failure = RuntimeError(f"cannot reach jane.doe@example.com with {self.FAKE_KEY}")
        saved, stream = (sys.stdout, sys.stderr), None
        try:
            with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(vault)}), mock.patch.object(cli, "_context", side_effect=failure), \
                    mock.patch("careeros.ops.notify.toast", return_value=True):
                with self.assertRaises(RuntimeError):
                    cli.main(["run", "--output-file", str(self.log)])
                stream = sys.stdout
        finally:
            sys.stdout, sys.stderr = saved
            if stream is not None and stream is not saved[0]:
                stream.close()
            for handler in list(cli.log.handlers):   # release the temporary log file
                handler.close()
                cli.log.removeHandler(handler)
        text = self.log.read_text(encoding="utf-8")
        self.assertRegex(text, r"^=== .* careeros run ===\n")
        self.assertIn("RuntimeError: cannot reach [REDACTED_EMAIL] with [REDACTED_KEY]", text)
        self.assertNotIn("jane.doe", text)
        self.assertNotIn(self.FAKE_KEY[:10], text)


class ClosedShortlistTests(StatusMixin, unittest.TestCase):
    """Ticket 1 (owner: "Yes, it's a bug"): a closed posting leaves the shortlist, as careeros/review/shortlist.py's
    docstring says, whether a Tier A/B pick or your Apply/Stretch label put it there. It stays in its own report sections
    (the job counts, your applications)."""

    def test_a_closed_posting_leaves_the_shortlist_and_stays_in_its_own_sections(self):
        store = self.store
        scored(store, "gh:a:open-pick", "B")
        record_label(store, scored(store, "gh:a:open-stretch", "archive"), "stretch", None, key="k1")
        scored(store, "gh:x:closed-pick", "A", current_state="closed", closed_at=ago(1))
        record_label(store, scored(store, "gh:x:closed-apply", "near_miss", current_state="closed", closed_at=ago(1),
                                   title="Claims Analyst", title_norm="claims analyst"), "apply", None, key="k2")
        store.writer("tracker").insert("applications", {   # you applied before it closed
            "application_id": "app:closed", "workflow_id": "application:app:closed", "job_id": "gh:x:closed-apply",
            "channel": "direct", "applied_at": ago(3), "first_seen_at": ago(3), "current_state": "applied",
            "entered_state_at": ago(3)})
        self.assertEqual(sorted(shortlisted_job_ids(store)), ["gh:a:open-pick", "gh:a:open-stretch"])
        self.assertEqual(sorted(i["job_id"] for i in shortlist(store, POLICY)), ["gh:a:open-pick", "gh:a:open-stretch"])
        page = build_report(store, PROFILE, POLICY)
        shortlisted = page[page.index('id="shortlist"'):page.index("<h2", page.index('id="shortlist"'))]
        self.assertIn("Shortlist (2)", shortlisted)
        self.assertNotIn("gh:x:closed", shortlisted)
        self.assertIn("closed: 2", page[:page.index("<h2")])        # the job counts in the header
        self.assertIn('id="app:closed"', page)                      # your application to it stays in its section


if __name__ == "__main__":
    unittest.main()
