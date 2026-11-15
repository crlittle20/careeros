"""The resilience batch (D83, 2026-11-03): schema guards and `careeros migrate` (OPP-50), the integrity checks in
`careeros verify` (OPP-50), and the weekly restore drill with the backup of a failed run (OPP-25). The hash-locked
runtime lock (OPP-71) is checked in test_config, the runtime line of `careeros status` (OPP-55) in test_ops and the AI
reader's default (OPP-69) in test_ai."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.core.clock import FixedClock
from careeros.core.locks import SingleWriterLock
from careeros.core.policy import load_policy
from careeros.core.transitions import load_matrix
from careeros.db import store as store_mod
from careeros.db.store import SCHEMA_DIR, MigrationPending, SchemaTooNew, newest_schema, open_store
from careeros.ops import backup as backup_mod
from careeros.paths import CONFIG_DIR, vault_paths
from careeros.qualify.profile import load_profile
from tests.helpers import add_job

FIXTURES = Path(__file__).parent / "fixtures"
NEWEST = newest_schema()


def meta(path: Path, key: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def set_meta(path: Path, key: str, value) -> None:
    con = sqlite3.connect(path)
    try:
        if value is None:
            con.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
        con.commit()
    finally:
        con.close()


def schema_before_newest(path: Path) -> None:
    """A real database one schema behind this code: every migration but the newest, as an older engine left it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) < NEWEST:
                con.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(NEWEST - 1),))
        con.commit()
    finally:
        con.close()


class SchemaGuardTests(unittest.TestCase):
    """OPP-50: an engine never opens a database newer than itself unless the newer migration said it may, and a command
    without the writer lock never migrates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "careeros.db"

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, **kw) -> None:
        open_store(self.db, FixedClock(), "test-policy", **kw).close()

    def test_an_older_engine_opens_a_newer_database_only_where_it_is_marked_safe(self):
        self.open()
        set_meta(self.db, "schema_version", NEWEST + 1)
        for marker in (NEWEST + 1, None):   # marked for newer engines only, or not marked at all
            set_meta(self.db, "oldest_engine_schema", marker)
            for mode in (True, False):
                with self.subTest(marker=marker, migrate=mode), self.assertRaises(SchemaTooNew):
                    self.open(migrate=mode)
        set_meta(self.db, "oldest_engine_schema", NEWEST)   # the newer migration said this engine may run on it
        for mode in (True, False):
            with self.subTest(marked_safe=True, migrate=mode):
                self.open(migrate=mode)
        self.assertEqual((meta(self.db, "schema_version"), meta(self.db, "oldest_engine_schema")), (str(NEWEST + 1), str(NEWEST)))

    def test_each_migration_records_which_older_engines_may_run_on_it(self):
        self.assertEqual(store_mod.RUNS_ON_OLDER.get(15), 14)   # rehearsed: the schema-14 engine ran on schema 15
        expected = str(store_mod.RUNS_ON_OLDER.get(NEWEST, NEWEST))
        self.open()
        self.assertEqual(meta(self.db, "oldest_engine_schema"), expected)
        set_meta(self.db, "oldest_engine_schema", None)   # a database migrated before this record existed
        self.open(migrate=False)
        self.assertIsNone(meta(self.db, "oldest_engine_schema"))   # a read-only open writes nothing
        self.open()
        self.assertEqual(meta(self.db, "oldest_engine_schema"), expected)

    def test_a_read_only_open_never_migrates(self):
        schema_before_newest(self.db)
        with self.assertRaises(MigrationPending):
            self.open(migrate=False)
        self.assertEqual(meta(self.db, "schema_version"), str(NEWEST - 1))
        self.open(migrate=None)   # `careeros migrate`'s own open: it backs up before it migrates
        self.assertEqual(meta(self.db, "schema_version"), str(NEWEST - 1))
        self.open()
        self.assertEqual(meta(self.db, "schema_version"), str(NEWEST))


class SchemaCommandTests(unittest.TestCase):
    """OPP-50 at the command line: read-only commands refuse a pending migration in one line, `careeros migrate`
    applies it after a verified backup, and `careeros verify` checks the file and its foreign keys too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "profile").mkdir(parents=True)
        (self.vault / "policy").mkdir()
        shutil.copy(FIXTURES / "synthetic_profile.yaml", self.vault / "profile" / "candidate_profile_v0.yaml")
        (self.vault / "policy" / "overrides.yaml").write_text("{}\n", encoding="utf-8")
        self.paths = vault_paths(self.vault)
        schema_before_newest(self.paths["db"])

    def tearDown(self):
        for handler in list(cli.log.handlers):   # release the temporary log file
            handler.close()
            cli.log.removeHandler(handler)
        self.tmp.cleanup()

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(self.vault)}), redirect_stdout(out), redirect_stderr(err), \
                mock.patch("careeros.ops.notify.toast", lambda *a, **k: True):
            try:
                cli.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_a_read_only_command_refuses_and_migrate_backs_up_first(self):
        code, _, err = self.cli("status")
        self.assertEqual(code, 2)
        self.assertIn(f"Refused: the database is at schema {NEWEST - 1} and this engine needs {NEWEST}", err)
        self.assertEqual(meta(self.paths["db"], "schema_version"), str(NEWEST - 1))
        code, out, _ = self.cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("(verified)", out)
        self.assertIn(f"schema {NEWEST - 1} -> {NEWEST}", out)
        copies = [p for p in self.paths["backups"].iterdir() if (p / backup_mod.MANIFEST).is_file()]
        self.assertEqual(len(copies), 1)
        self.assertEqual(meta(copies[0] / "careeros.db", "schema_version"), str(NEWEST - 1))   # the copy is from before
        self.assertEqual(self.cli("status")[0], 0)
        self.assertIn(f"schema {NEWEST}: nothing to migrate", self.cli("migrate")[1])
        # N1 (Codex CR-2026-11-03-PYTHON-314): a checkpoint of its own, which the same day's backup leaves in place
        self.assertTrue(copies[0].name.startswith(f"before-migration-{NEWEST - 1}-to-{NEWEST}-"), copies[0].name)
        self.assertEqual(self.cli("backup")[0], 0)
        backup_mod.prune(self.paths["backups"], keep=0)
        self.assertEqual(meta(copies[0] / "careeros.db", "schema_version"), str(NEWEST - 1))

    def test_unlock_writes_only_holding_the_writer_lock(self):
        """F1 (Codex CR-2026-11-03-PYTHON-314): a command can take the lock the moment the stale one is removed. Unlock
        then neither migrates nor marks runs, and a second unlock finishes once that command is done."""
        con = sqlite3.connect(self.paths["db"])
        try:
            con.execute("INSERT INTO runs(run_id, kind, started_at, status) VALUES ('run:1', 'daily', '2026-11-04T07:00:00.000Z', "
                        "'running')")
            con.commit()
        finally:
            con.close()
        self.paths["lock"].write_text('{"pid": 0, "nonce": "crashed"}', encoding="utf-8")   # its process is gone
        other, real = SingleWriterLock(self.paths["lock"]), cli.force_unlock

        def then_another_command_starts(path):
            removed = real(path)
            other.acquire()
            return removed
        with mock.patch.object(cli, "force_unlock", then_another_command_starts):
            code, out, _ = self.cli("unlock")
        try:
            self.assertEqual(code, 0)
            self.assertIn("removed stale lock", out)
            self.assertIn("interrupted runs not marked: another CareerOS command took the lock first", out)
            self.assertEqual(meta(self.paths["db"], "schema_version"), str(NEWEST - 1))   # not migrated under its lock
            self.assertEqual(json.loads(self.paths["lock"].read_text(encoding="utf-8"))["nonce"], other.nonce)
        finally:
            other.release()
        code, out, _ = self.cli("unlock")
        self.assertEqual((code, "no lock present" in out, "marked 1 interrupted run(s) as failed" in out), (0, True, True), out)
        self.assertEqual(meta(self.paths["db"], "schema_version"), str(NEWEST))   # holding the lock, it migrates
        self.assertFalse(self.paths["lock"].exists())
        con = sqlite3.connect(self.paths["db"])
        try:
            self.assertEqual(con.execute("SELECT status FROM runs").fetchone()[0], "failed")
        finally:
            con.close()

    def test_migrate_migrates_nothing_when_its_backup_does_not_verify(self):
        with mock.patch.object(backup_mod, "verify", lambda folder: ["careeros.db"]):
            code, out, _ = self.cli("migrate")
        self.assertEqual(code, 2)
        self.assertIn("nothing migrated", out)
        self.assertEqual(meta(self.paths["db"], "schema_version"), str(NEWEST - 1))

    def test_verify_checks_the_file_and_its_foreign_keys(self):
        self.cli("migrate")
        code, out, _ = self.cli("verify")
        self.assertEqual(code, 0, out)
        self.assertIn('"quick_check": [\n    "ok"\n  ]', out)
        self.assertIn('"foreign_key_problem_count": 0', out)
        store = open_store(self.paths["db"], FixedClock(), "test-policy")
        try:
            store.conn.execute("PRAGMA foreign_keys = OFF")   # as a bad restore or a hand edit might leave it
            add_job(store, "gh:acme:9", duplicate_of="gh:missing:1")
        finally:
            store.close()
        code, out, _ = self.cli("verify")
        self.assertEqual(code, 2)
        self.assertIn('"foreign_key_problem_count": 1', out)


class RestoreDrillTests(unittest.TestCase):
    """OPP-25: once a week the daily run reads its newest backup back as a restore would, and a failed run still makes
    its backup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = vault_paths(Path(self.tmp.name) / "vault")
        self.store = open_store(self.paths["db"], FixedClock(), "test-policy")
        add_job(self.store)
        self.store.writer("discovery").transition(load_matrix(CONFIG_DIR / "workflows" / "job.json"), "gh:acme:1", "normalized")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_a_backup_reads_back_whole_and_a_damaged_one_does_not(self):
        folder = backup_mod.backup(self.store, self.paths)
        result = backup_mod.restore_drill(folder, live_jobs=1)
        self.assertEqual((result["ok"], result["problems"], result["jobs"]), (True, [], 1))
        self.assertGreater(result["events"], 0)
        self.assertEqual(backup_mod.restore_drill(folder, live_jobs=0)["problems"], ["jobs:1_of_0"])
        con = sqlite3.connect(folder / "careeros.db")   # one event damaged in the copy, as a bad disk might
        try:
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'events'").fetchall():
                con.execute(f'DROP TRIGGER "{name}"')
            con.execute("UPDATE events SET hash = ? WHERE seq = (SELECT MIN(seq) FROM events)", ("f" * 64,))
            con.commit()
        finally:
            con.close()
        damaged = backup_mod.restore_drill(folder, live_jobs=1)
        self.assertFalse(damaged["ok"])
        self.assertIn("careeros.db", damaged["problems"])   # the manifest's hash
        self.assertTrue(any(p.startswith("event_chain_breaks:") for p in damaged["problems"]), damaged["problems"])

    def test_the_drill_is_weekly_and_daily_after_a_failure_and_status_says_so(self):
        backups = self.paths["backups"]
        backups.mkdir(parents=True, exist_ok=True)
        self.assertTrue(backup_mod.drill_due(backups, "2026-11-04T12:00:00.000Z"))
        self.assertEqual(cli._drill_line(self.paths, "2026-11-04T12:00:00.000Z"),
                         "restore drill: none yet (the daily run reads the newest backup back once a week)")
        backup_mod.record_drill(backups, {"folder": "2026-11-04", "ok": True, "problems": [], "jobs": 1, "events": 2},
                                "2026-11-04T12:00:00.000Z")
        self.assertFalse(backup_mod.drill_due(backups, "2026-11-10T12:00:00.000Z"))
        self.assertTrue(backup_mod.drill_due(backups, "2026-11-11T12:00:00.000Z"))
        self.assertEqual(cli._drill_line(self.paths, "2026-11-05T12:00:00.000Z"), "restore drill: 2026-11-04 ok (2026-11-04: 1 jobs, 2 events)")
        self.assertTrue(cli._drill_line(self.paths, "2026-11-14T12:00:00.000Z").startswith("!! restore drill: the last one (2026-11-04) is 10 days old"))
        backup_mod.record_drill(backups, {"folder": "2026-11-11", "ok": False, "problems": ["careeros.db"], "jobs": 0, "events": 0},
                                "2026-11-11T12:00:00.000Z")
        self.assertTrue(backup_mod.drill_due(backups, "2026-11-12T12:00:00.000Z"))
        self.assertEqual(cli._drill_line(self.paths, "2026-11-12T12:00:00.000Z"), "!! restore drill failed 2026-11-11 on 2026-11-11: careeros.db")

    def test_a_failed_daily_run_still_gets_its_backup_and_its_drill(self):
        policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        ctx = cli.Context(self.store, policy, load_profile(FIXTURES / "synthetic_profile.yaml"),
                          load_matrix(CONFIG_DIR / "workflows" / "job.json"), self.paths["snapshots"], None)
        summaries = []
        with mock.patch.object(cli, "cmd_discover", side_effect=RuntimeError("no network")), \
                mock.patch("careeros.ops.notify.summary", lambda store, started: summaries.append(started) or None), \
                redirect_stdout(io.StringIO()) as out, self.assertRaises(RuntimeError):
            cli.cmd_run(ctx, self.paths, SimpleNamespace())
        copies = [p for p in self.paths["backups"].iterdir() if (p / backup_mod.MANIFEST).is_file()]
        self.assertEqual(len(copies), 1)
        self.assertTrue(backup_mod.last_drill(self.paths["backups"])["ok"])
        self.assertIn("Restore drill:", out.getvalue())
        self.assertEqual(summaries, [])   # no "run finished" summary for a run that failed
        self.assertEqual(self.store.one("SELECT status FROM runs")["status"], "failed")

    def test_a_failed_drill_is_recorded_and_notified(self):
        folder = backup_mod.backup(self.store, self.paths)
        policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        ctx = cli.Context(self.store, policy, None, None, None, None)
        shown = []
        with mock.patch.object(backup_mod, "verify", lambda f: ["careeros.db"]), \
                mock.patch("careeros.ops.notify.problem", lambda title, body: shown.append(title)), redirect_stdout(io.StringIO()):
            cli._weekly_drill(ctx, self.paths, folder)
        self.assertEqual(shown, ["CareerOS: restore drill failed"])
        self.assertFalse(backup_mod.last_drill(self.paths["backups"])["ok"])
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "restore_drill_failed")

    def ctx(self):
        policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        return cli.Context(self.store, policy, None, None, None, None)

    def test_a_damaged_drill_record_makes_the_drill_due_and_status_says_so(self):
        """F2 (Codex CR-2026-11-03-PYTHON-314): a record that reads as JSON but is not a drill's is never trusted."""
        backups = self.paths["backups"]
        backups.mkdir(parents=True, exist_ok=True)
        for text in ('{"ok": true, "at": "not-a-timestamp"}', '{"ok": true, "at": "2026-11-04T12:00:00.000Z", "folder": "x"}',
                     '{"ok": false, "at": "2026-11-04T12:00:00.000Z", "folder": "x", "problems": "careeros.db"}',
                     '["not", "a", "record"]', "{not json"):
            with self.subTest(record=text):
                (backups / backup_mod.DRILL_RECORD).write_text(text, encoding="utf-8")
                self.assertEqual(backup_mod.last_drill(backups), {"ok": False, "unreadable": True})
                self.assertTrue(backup_mod.drill_due(backups, "2026-11-05T12:00:00.000Z"))
                self.assertTrue(cli._drill_line(self.paths, "2026-11-05T12:00:00.000Z").startswith(
                    "!! restore drill: its record (vault\\backups\\restore-drill.json) cannot be read"))
        with redirect_stdout(io.StringIO()):   # the next daily run drills again and writes a good record
            cli._after_run(self.ctx(), self.paths, "2026-11-05T00:00:00.000Z", notify=False)
        self.assertTrue(backup_mod.last_drill(backups)["ok"])

    def test_the_drills_own_trouble_never_fails_the_run_nor_hides_its_error(self):
        """F2: a drill result that cannot be saved is recorded and notified; any other trouble in the housekeeping (the
        drill, its bookkeeping, the summary) is a warning, and a failed run's own error is the one raised."""
        from careeros.db.store import Writer
        shown = []
        with mock.patch.object(backup_mod, "record_drill", side_effect=PermissionError("read-only folder")), \
                mock.patch("careeros.ops.notify.problem", lambda title, body: shown.append((title, body))), \
                redirect_stdout(io.StringIO()) as out, self.assertLogs(cli.log, "ERROR"):
            cli._after_run(self.ctx(), self.paths, "2026-11-05T00:00:00.000Z", notify=False)
        self.assertEqual(shown, [("CareerOS: restore drill failed", "Its result could not be saved. Details: careeros status")])
        self.assertIn("result not saved (PermissionError: read-only folder)", out.getvalue())
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "restore_drill_failed")
        with mock.patch.object(cli, "_weekly_drill", side_effect=ValueError("bad record")), \
                mock.patch("careeros.ops.notify.summary", side_effect=RuntimeError("no summary")), \
                redirect_stdout(io.StringIO()) as out, self.assertLogs(cli.log, "ERROR"):
            cli._after_run(self.ctx(), self.paths, "2026-11-05T00:00:00.000Z")
        self.assertIn("restore drill could not finish: ValueError: bad record", out.getvalue())
        self.assertIn("notification not shown: RuntimeError: no summary", out.getvalue())
        with mock.patch.object(backup_mod, "backup", side_effect=OSError("disk full")), \
                mock.patch.object(Writer, "exception", side_effect=sqlite3.OperationalError("disk I/O error")), \
                mock.patch("careeros.ops.notify.problem", lambda title, body: True), redirect_stdout(io.StringIO()) as out, \
                self.assertLogs(cli.log, "ERROR"):
            cli._after_run(self.ctx(), self.paths, "2026-11-05T00:00:00.000Z", notify=False)
        self.assertIn("backup failed: disk full", out.getvalue())
        ctx = cli.Context(self.store, self.ctx().policy, load_profile(FIXTURES / "synthetic_profile.yaml"),
                          load_matrix(CONFIG_DIR / "workflows" / "job.json"), self.paths["snapshots"], None)
        with mock.patch.object(cli, "cmd_discover", side_effect=RuntimeError("no network")), \
                mock.patch.object(cli, "_weekly_drill", side_effect=ValueError("bad record")), \
                redirect_stdout(io.StringIO()), self.assertLogs(cli.log, "ERROR"), self.assertRaisesRegex(RuntimeError, "no network"):
            cli.cmd_run(ctx, self.paths, SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
