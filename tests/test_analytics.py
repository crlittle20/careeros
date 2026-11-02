"""Analytics export (Portfolio Prompt 7, 2026-10-13): the star schema for Power BI.

DemoExportTest runs the four-day demo and `careeros export` once, as a user would, and proves:
- the export is valid and synthetic-only, reconciles with the application's own figures, and is byte-identical to
  the committed sample (samples/analytics_demo, rebuilt by tools/build_demo_export.py);
- every table and column is documented in docs/analytics_data_dictionary.md, and every KPI value quoted in
  docs/powerbi_dashboard_spec.md is what the export measures;
- each job carries its latest recorded dismissal (2026-10-30), never the dismissal's own note, and the manifest's
  schema_revision says which columns the files have;
- the validation catches broken exports; the export only reads; a private export never lands in the code repository,
  a demo export never leaves the demo vault, and an export never replaces a folder it did not write.
"""
import copy
import hashlib
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
from careeros.analytics.export import SCHEMA_REVISION, SCHEMA_VERSION, TABLES, build, kpis, reconcile, validate
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
        cls.ready = None     # the demo and the export run at the first test (setUp), not here

    def setUp(self):
        # Once per class. Not in setUpClass: a failure there hides the class's tests from the count, and a SystemExit there
        # ends the whole run (the 2026-10-18 full run); here every test of the class reports it.
        cls = type(self)
        if cls.ready is None:
            try:
                quiet("demo", "tour")
                quiet("export")      # Demo Mode is on: `careeros demo` switched it on
                cls.out = cls.tmp / "demo" / "exports" / "analytics"
                cls.manifest = json.loads((cls.out / "manifest.json").read_text(encoding="utf-8"))
                cls.ready = True
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - reported by each test below
                cls.ready = exc
        if cls.ready is not True:
            self.fail(f"the demo tour or the export failed: {cls.ready!r}")

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

    def test_the_contract_revision_names_these_columns(self):
        """Within analytics-1, columns are only ever added, and each addition raises schema_revision (2026-10-30), so a
        manifest says which columns its files have. A change to TABLES must come with a new revision or version."""
        columns = {1: "9137c0fd60d82089", 2: "ed8f6d32b8175028"}   # revision -> fingerprint of TABLES (names and types, in order)
        self.assertEqual((self.manifest["schema_version"], self.manifest["schema_revision"]), ("analytics-1", SCHEMA_REVISION))
        self.assertEqual(hashlib.sha256(json.dumps(TABLES).encode()).hexdigest()[:16], columns.get(SCHEMA_REVISION),
                         "the tables changed: raise SCHEMA_REVISION (columns added at the end) or the version, record the "
                         "new fingerprint here and describe the revision in the data dictionary")
        self.assertIn(f"- Version: 1.1 (schema `{SCHEMA_VERSION}`, revision {SCHEMA_REVISION})", DICTIONARY.read_text(encoding="utf-8"))

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
            (lambda t: t["fact_job_evaluation"][0].update(is_dismissed=1), "dismissed jobs, and only they, have a dismissal date"),
            (lambda t: t["fact_job_evaluation"][0].update(dismissal_reason="pay"), "dismissed jobs, and only they, have a dismissal date"),
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
        broken = copy.deepcopy(tables)   # well-formed, but the demo has no dismissal on record
        broken["fact_job_evaluation"][scored].update(is_dismissed=1, dismissed_date_key=broken["dim_date"][0]["date_key"], dismissal_reason="pay")
        self.assertFalse(failed(validate(broken), "dismissed jobs, and only they"))
        self.assertTrue(failed(reconcile(broken, ctx.store, ctx.policy), "dismissed jobs (by their latest reason) match the application"))

    def test_a_rate_is_compared_as_the_application_rounds_it(self):
        """33 complete scans of 227 is 0.1454, which the application shows as 0.15. Rounded to the manifest's 3 places
        (0.145) and then to 2 (0.14), it used to fail its own reconciliation, so a correct export was refused
        (2026-10-30). The comparison stays exact: a figure the application would show differently is still refused."""
        from careeros.review import discovery_metrics
        ctx, tables = self.demo_tables()
        scans = copy.deepcopy(tables)
        scans["fact_board_scan"] = [dict(tables["fact_board_scan"][0], scan_key=n, is_complete=int(n <= 33)) for n in range(1, 228)]
        figures = discovery_metrics.metrics(ctx.store, ctx.policy, 36500)
        for shown, accepted in ((round(33 / 227, 2), True), (0.14, False), (0.16, False)):
            figures["verification"]["scan_success_rate"] = shown
            with self.subTest(application=shown), mock.patch.object(discovery_metrics, "metrics", return_value=figures):
                self.assertEqual(failed(reconcile(scans, ctx.store, ctx.policy), "scan_reliability equals"), not accepted)

    def test_a_recommended_variant_counts_once_and_never_stops_the_export(self):
        """A variant is another posting of the same opening. It was left out of is_qualified but not of is_recommended,
        so a tier A/B variant was recommended without being qualified, and the export's own check refused to write
        (2026-10-30). Both flags now count openings, as the application's funnel does; the check is unchanged."""
        from tests.test_job_lifecycle import jid, world
        store, ctx = world()   # job 3 is a scored variant of job 1
        for job in (jid(1), jid(3)):   # both rise to tier B in a later score
            score = dict(store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job,)))
            score.update(score_id=score["score_id"] + "-b", tier="B")
            store.conn.execute(f"INSERT INTO scores ({', '.join(score)}) VALUES ({', '.join('?' * len(score))})", tuple(score.values()))
        tables = build(store, ctx.policy)
        self.assertTrue(all(c["ok"] for c in validate(tables) + reconcile(tables, store, ctx.policy)))
        rows = {r["job_key"]: r for r in tables["fact_job_evaluation"]}
        flags = lambda job: tuple(rows[job][f] for f in ("tier", "is_variant", "is_qualified", "is_recommended"))  # noqa: E731
        self.assertEqual((flags(jid(1)), flags(jid(3))), (("B", 0, 1, 1), ("B", 1, 0, 0)))
        broken = copy.deepcopy(tables)   # the check itself is as strict as before
        broken["fact_job_evaluation"][list(rows).index(jid(3))].update(is_recommended=1)
        self.assertTrue(failed(validate(broken), "job flags agree with the job's state"))

    def test_dismissals_are_exported_with_the_latest_reason(self):
        from careeros.review.dismiss import dismiss
        from tests.test_job_lifecycle import jid, world
        store, ctx = world()
        dismiss(store, jid(1), "pay", "a private note")
        store.clock.advance(days=1)
        dismiss(store, jid(1), "qualifications")
        dismiss(store, jid(7), "location")
        store.conn.execute("UPDATE jobs SET exclude_from_kpi = 1 WHERE job_id = ?", (jid(7),))   # a drill: left out with its dismissal
        tables = build(store, ctx.policy)
        self.assertTrue(all(c["ok"] for c in validate(tables) + reconcile(tables, store, ctx.policy)))
        rows = {r["job_key"]: r for r in tables["fact_job_evaluation"]}
        self.assertNotIn(jid(7), rows)
        latest = store.one("SELECT at FROM dismissals WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (jid(1),))["at"]
        self.assertEqual((rows[jid(1)]["is_dismissed"], rows[jid(1)]["dismissal_reason"], rows[jid(1)]["dismissed_date_key"]),
                         (1, "qualifications", int(latest[:10].replace("-", ""))))
        self.assertEqual(rows[jid(1)]["current_state"], "scored")   # a dismissal is your decision, not a change of the job's state
        others = [r for k, r in rows.items() if k != jid(1)]
        self.assertTrue(others and all((r["is_dismissed"], r["dismissed_date_key"], r["dismissal_reason"]) == (0, None, None) for r in others))
        self.assertIn(rows[jid(1)]["dismissed_date_key"], {d["date_key"] for d in tables["dim_date"]})
        self.assertNotIn("a private note", json.dumps(tables))   # the dismissal's own words are free text: never exported
        from careeros.review.dismiss import REASONS
        documented = next(line for line in DICTIONARY.read_text(encoding="utf-8").splitlines() if line.startswith("| `dismissal_reason` |"))
        self.assertEqual(re.findall(r"`(\w+)`", documented)[1:], list(REASONS))

    def test_a_reinstatement_clears_the_exported_dismissal(self):
        """The latest event decides (ADR 0007, amendment of 2026-11-01): after a reinstatement `is_dismissed` is 0 and its
        date and reason are blank; a later dismissal sets them again. Same columns, so the same contract revision."""
        from careeros.review.dismiss import dismiss, reinstate
        from tests.test_job_lifecycle import jid, world
        store, ctx = world()
        dismiss(store, jid(1), "pay")
        store.clock.advance(days=1)
        reinstate(store, jid(1), "I misread the pay")
        dismiss(store, jid(2), "location")
        store.clock.advance(days=1)
        reinstate(store, jid(2))
        store.clock.advance(days=1)
        dismiss(store, jid(2), "seniority")
        tables = build(store, ctx.policy)
        self.assertEqual([c for c in validate(tables) + reconcile(tables, store, ctx.policy) if not c["ok"]], [])
        rows = {r["job_key"]: r for r in tables["fact_job_evaluation"]}
        self.assertEqual((rows[jid(1)]["is_dismissed"], rows[jid(1)]["dismissed_date_key"], rows[jid(1)]["dismissal_reason"]),
                         (0, None, None))
        self.assertEqual((rows[jid(2)]["is_dismissed"], rows[jid(2)]["dismissal_reason"], rows[jid(2)]["dismissed_date_key"]),
                         (1, "seniority", int(store.clock.now()[:10].replace("-", ""))))
        self.assertNotIn("reinstated", json.dumps(tables))   # a reinstatement is never exported as a reason
        documented = next(line for line in DICTIONARY.read_text(encoding="utf-8").splitlines() if line.startswith("| `is_dismissed` |"))
        self.assertIn("reinstat", documented)

    def test_a_void_application_stays_a_row_but_counts_nowhere(self):
        """ADR 0007, amendment of 2026-11-01: fact_application keeps a void application (state void, never open, no
        stage reached), and it counts nowhere an application counts: has_application, the Applications KPI and the
        rates over it. Same columns, so the same contract revision; the dictionary lists the state."""
        from careeros.review.applications import VOID, application_matrix, record_application, update_application
        from tests.test_job_lifecycle import jid, world
        store, ctx = world()
        app = application_matrix()
        mistaken = record_application(store, app, jid(1), "direct", policy=ctx.policy, key="a1")
        record_application(store, app, jid(6), "direct", policy=ctx.policy, key="a2")   # another role
        update_application(store, app, mistaken, VOID, reason="recorded on the wrong posting", key="v1")
        tables = build(store, ctx.policy)
        self.assertEqual([c for c in validate(tables) + reconcile(tables, store, ctx.policy) if not c["ok"]], [])
        rows = {r["job_key"]: r for r in tables["fact_application"]}
        self.assertEqual(len(tables["fact_application"]), 2)   # the void row is kept
        void = rows[jid(1)]
        self.assertEqual((void["current_state"], void["state_category"], void["is_open"], void["got_answer"],
                          void["reached_interview"]), ("void", "cancelled", 0, 0, 0))
        jobs = {r["job_key"]: r for r in tables["fact_job_evaluation"]}
        self.assertEqual((jobs[jid(1)]["has_application"], jobs[jid(6)]["has_application"]), (0, 1))
        figures = kpis(tables)
        self.assertEqual((figures["applications"], figures["applications_open"]), (1, 1))
        documented = next(line for line in DICTIONARY.read_text(encoding="utf-8").splitlines() if line.startswith("| `current_state` | text | `applied`"))
        self.assertIn("`void`", documented)
        spec = SPEC.read_text(encoding="utf-8")   # the Power BI measure counts the same way
        self.assertIn('Applications = CALCULATE ( COUNTROWS ( fact_application ), KEEPFILTERS ( fact_application[current_state] <> "void" ) )',
                      spec)

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
