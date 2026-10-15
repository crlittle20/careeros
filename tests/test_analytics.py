"""Analytics export (Portfolio Prompt 7, 2026-10-13): the star schema for Power BI.

DemoExportTest runs the four-day demo and `careeros export` once, as a user would, and proves:
- the export is valid and synthetic-only, reconciles with the application's own figures, and is byte-identical to
  the committed sample (samples/analytics_demo, rebuilt by tools/build_demo_export.py);
- every table and column is documented in docs/analytics_data_dictionary.md, and every KPI value quoted in
  docs/powerbi_dashboard_spec.md is what the export measures;
- the validation catches broken exports; the export only reads; a private export never lands in the code repository,
  a demo export never leaves the demo vault, and an export never replaces a folder it did not write.
"""
import copy
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from careeros import cli
from careeros.analytics.export import TABLES, build, reconcile, validate
from careeros.demo import runner, sandbox
from careeros.paths import ENGINE_ROOT

SAMPLE = ENGINE_ROOT / "samples" / "analytics_demo"
DICTIONARY = ENGINE_ROOT / "docs" / "analytics_data_dictionary.md"
SPEC = ENGINE_ROOT / "docs" / "powerbi_dashboard_spec.md"


def quiet(*argv) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(list(argv))
    return out.getvalue()


def close_log_files(under: Path) -> None:
    for handler in list(cli.log.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(under):
            handler.close()
            cli.log.removeHandler(handler)


def failed(checks: list, prefix: str) -> bool:
    return any(not c["ok"] for c in checks if c["check"].startswith(prefix))


class DemoExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="careeros-analytics-test-"))
        env = mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(cls.tmp / "demo"), "CAREEROS_VAULT": str(cls.tmp / "private"),
                                           sandbox.ENV: ""})
        env.start()
        cls.addClassCleanup(env.stop)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(close_log_files, cls.tmp)
        cls.addClassCleanup(sandbox.install, None)
        quiet("demo", "tour")
        quiet("export")      # Demo Mode is on: `careeros demo` switched it on
        cls.out = cls.tmp / "demo" / "exports" / "analytics"
        cls.manifest = json.loads((cls.out / "manifest.json").read_text(encoding="utf-8"))

    def demo_tables(self):
        ctx = cli._context(runner.paths(), clock=runner.demo_clock())
        self.addCleanup(ctx.store.close)
        return ctx, build(ctx.store, ctx.policy)

    def read(self, table: str) -> list[dict]:
        import csv
        with open(self.out / f"{table}.csv", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    # --- the export itself ------------------------------------------------------------------------------------------
    def test_the_demo_export_is_valid_synthetic_and_reconciles_with_the_application(self):
        self.assertEqual((self.manifest["source"], self.manifest["valid"]), ("demo", True))
        self.assertEqual([c["check"] for c in self.manifest["checks"] if not c["ok"]], [])
        names = " | ".join(c["check"] for c in self.manifest["checks"])
        for expected in ("the only email addresses are fictional example.com ones", "jobs_qualified equals the application's own figure",
                         "scan_reliability equals the application's own figure", "fact_job_evaluation has one row per source record",
                         "fact_job_evaluation.employer_key -> dim_employer: every reference exists"):
            self.assertIn(expected, names)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted([f"{t}.csv" for t in TABLES] + ["manifest.json"]))

    def test_the_committed_sample_is_exactly_this_export(self):
        fresh = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(sorted(p.name for p in SAMPLE.iterdir()), fresh)
        for name in fresh:
            self.assertEqual((SAMPLE / name).read_bytes(), (self.out / name).read_bytes(),
                             f"samples/analytics_demo/{name} is out of date: run tools/build_demo_export.py")

    def test_the_demo_shows_a_job_seeker_pipeline(self):
        apps = self.read("fact_application")
        self.assertEqual(sorted(a["current_state"] for a in apps), ["applied", "applied", "rejected", "screening"])
        jobs = self.read("fact_job_evaluation")
        self.assertEqual(sorted(j["label"] for j in jobs if j["label"]), ["apply", "apply", "apply", "apply", "skip", "stretch"])
        self.assertEqual(sum(int(j["has_application"]) for j in jobs), 4)

    def test_csv_conventions(self):
        raw = (self.out / "fact_job_evaluation.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                    # UTF-8 with a byte-order mark
        self.assertIn(b"\r\n", raw)
        rows = self.read("fact_job_evaluation")
        unscored = next(r for r in rows if r["is_rejected"] == "1")
        self.assertEqual((unscored["candidate_fit"], unscored["tier"]), ("", ""))   # missing measures stay empty, never 0
        self.assertTrue(all(r["employer_key"] for r in rows))                          # dimension keys are never empty
        self.assertTrue(all(re.fullmatch(r"\d{8}", r["first_seen_date_key"]) for r in rows))

    # --- documentation ---------------------------------------------------------------------------------------------
    def test_every_table_and_column_is_documented_in_order(self):
        documented, table = {}, None
        for line in DICTIONARY.read_text(encoding="utf-8").splitlines():
            heading = re.match(r"^### `?((?:dim|fact)_\w+)`?", line)
            if heading:
                table = heading.group(1)
                documented[table] = []
            elif table and re.match(r"^\| `\w+` \|", line):
                documented[table].append(re.match(r"^\| `(\w+)` \|", line).group(1))
        self.assertEqual(list(documented), list(TABLES))
        for name, columns in TABLES.items():
            self.assertEqual(documented[name], [c for c, _ in columns], name)

    def test_every_kpi_value_quoted_in_the_dashboard_spec_is_measured(self):
        quoted = {}
        for line in SPEC.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and re.fullmatch(r"`\w+`", cells[1]):
                quoted[cells[1].strip("`")] = cells[-1]
        kpis = self.manifest["kpis"]
        self.assertEqual(set(quoted), set(kpis))
        for name, text in quoted.items():
            value = kpis[name]
            if value is None:
                self.assertEqual(text, "(none)", name)
            elif text.endswith("%"):
                self.assertAlmostEqual(float(text.rstrip("%")) / 100, value, places=3, msg=name)
            else:
                self.assertEqual(float(text.replace(",", "")), float(value), name)

    # --- validation ------------------------------------------------------------------------------------------------
    def test_validation_catches_broken_exports(self):
        ctx, tables = self.demo_tables()
        self.assertTrue(all(c["ok"] for c in validate(tables) + reconcile(tables, ctx.store, ctx.policy)))
        jobs = tables["fact_job_evaluation"]
        rejected = next(i for i, j in enumerate(jobs) if j["is_rejected"])
        scored = next(i for i, j in enumerate(jobs) if j["is_scored"])
        cases = [
            (lambda t: t["dim_job"].append(dict(t["dim_job"][0])), "dim_job: the primary key"),
            (lambda t: t["fact_job_evaluation"][0].update(employer_key="nobody"), "fact_job_evaluation.employer_key -> dim_employer"),
            (lambda t: t["fact_job_evaluation"][0].update(board_key=None), "fact_job_evaluation.board_key -> dim_board"),
            (lambda t: t["fact_job_evaluation"][scored].update(candidate_fit=120), "scores are within 0-100"),
            (lambda t: t["fact_job_evaluation"][scored].update(is_scored=2), "every value has its column's type"),
            (lambda t: t["fact_job_evaluation"][rejected].update(is_qualified=1), "job flags agree with the job's state"),
            (lambda t: t["dim_date"].pop(3), "dim_date is one row per day with no gaps"),
            (lambda t: t["fact_job_evaluation"][0].update(extra=1), "every table is present with its documented columns"),
        ]
        for corrupt, check in cases:
            broken = copy.deepcopy(tables)
            corrupt(broken)
            with self.subTest(check=check):
                self.assertTrue(failed(validate(broken), check))
        broken = copy.deepcopy(tables)
        broken["fact_board_scan"].pop()
        self.assertTrue(failed(reconcile(broken, ctx.store, ctx.policy), "fact_board_scan has one row per source record"))
        broken = copy.deepcopy(tables)
        broken["fact_job_evaluation"][rejected].update(is_qualified=1)
        self.assertTrue(failed(reconcile(broken, ctx.store, ctx.policy), "jobs_qualified equals the application's own figure"))

    def test_rows_marked_as_drills_or_tests_are_left_out(self):
        from tests.test_job_lifecycle import jid, world
        store, ctx = world()
        store.conn.execute("UPDATE jobs SET exclude_from_kpi = 1 WHERE job_id = ?", (jid(1),))
        tables = build(store, ctx.policy)
        for table in ("dim_job", "fact_job_evaluation", "fact_job_observation"):
            self.assertNotIn(jid(1), {r["job_key"] for r in tables[table]}, table)
        self.assertTrue(all(c["ok"] for c in validate(tables) + reconcile(tables, store, ctx.policy)))

    # --- what the export may touch ---------------------------------------------------------------------------------
    def test_the_export_only_reads_and_repeats_exactly(self):
        db = runner.paths()["db"]
        con = sqlite3.connect(db)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        count = lambda: {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: E731
        before = count()
        again = runner.paths()["root"] / "exports" / "again"
        quiet("export", "--out", str(again))
        after = count()
        con.close()
        self.assertEqual(after, before)
        for p in self.out.iterdir():
            self.assertEqual((again / p.name).read_bytes(), p.read_bytes(), p.name)

    def test_an_export_that_fails_a_check_is_never_written(self):
        target = runner.paths()["root"] / "exports" / "invalid"
        failing = [{"check": "a planted failure", "ok": False, "detail": ""}]
        with mock.patch("careeros.analytics.export.validate", return_value=failing), self.assertRaises(SystemExit) as stop:
            quiet("export", "--out", str(target))
        self.assertIn("a planted failure", str(stop.exception.code))
        self.assertFalse(target.exists())

    def test_a_demo_export_stays_in_the_demo_vault(self):
        outside = self.tmp / "outside"
        with self.assertRaises(SystemExit):
            quiet("export", "--out", str(outside))
        self.assertFalse(outside.exists())

    def test_an_export_never_replaces_a_folder_it_did_not_write(self):
        folder = runner.paths()["root"] / "exports" / "mine"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(SystemExit):
            quiet("export", "--out", str(folder))
        self.assertEqual([p.name for p in folder.iterdir()], ["notes.txt"])

    def test_a_private_export_never_goes_into_the_code_repository(self):
        from careeros.demo import world
        private = self.tmp / "private"   # a stand-in private vault, with the fictional persona
        for path, text in ((private / "profile" / "candidate_profile_v0.yaml", world.PROFILE),
                           (private / "policy" / "overrides.yaml", world.OVERRIDES)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        inside = ENGINE_ROOT / "exports-test-should-not-exist"
        with mock.patch.dict(os.environ, {sandbox.ENV: ""}):
            with self.assertRaises(SystemExit):
                quiet("export", "--out", str(inside))
            self.assertFalse(inside.exists())
            quiet("export")   # the (empty) private vault's own exports folder is fine
        manifest = json.loads((self.tmp / "private" / "exports" / "analytics" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["source"], manifest["valid"]), ("private", True))
        self.assertEqual(manifest["tables"]["fact_job_evaluation"]["rows"], 0)


if __name__ == "__main__":
    unittest.main()
