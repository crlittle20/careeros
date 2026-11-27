"""The owner's D192 build (2026-11-20): the parts of Codex's design review of D190 (rev 2) and the owner's D191 that
were resolved before rev 3, each part its own commit. Part 1 (browser Fill paused) is tested in
tests/test_batch3.py FillPausedTests. Synthetic data only; nothing here reads the network."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from careeros.core.clock import FixedClock
from careeros.db import store as store_mod
from careeros.db.store import SCHEMA_DIR, SchemaTooNew, newest_schema, open_store
from careeros.discovery.boards import ashby_posting
from careeros.pipeline import board_pay_json, ingest

from tests.helpers import give_label, stored_label
from tests.test_packages import JOB, MID, POLICY, posting_store
from tests.test_review_page import ServerMixin
from tests.test_v26_discovery import CLAIMS, setup


def meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


class UpgradeFloorTests(unittest.TestCase):
    """Part 2 (H9): migration 016 records schema 16 and the floor 16 inside its own transaction; no engine older than it
    opens the database; a rollback across it is a restore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "careeros.db"

    def at_15(self):
        con = sqlite3.connect(self.db)
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) <= 15:
                con.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
        con.executescript("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '15');"
                          "INSERT OR REPLACE INTO meta(key, value) VALUES ('oldest_engine_schema', '14');")
        con.close()

    def test_016_writes_its_markers_inside_its_own_script(self):
        self.assertGreaterEqual(newest_schema(), 16)
        self.assertNotIn(16, store_mod.RUNS_ON_OLDER)
        self.at_15()
        con = sqlite3.connect(self.db)
        script = (SCHEMA_DIR / "016_upgrade_floor.sql").read_text(encoding="utf-8")
        con.executescript("BEGIN;\n" + script + "\nCOMMIT;")   # the script alone, as an older migrate ran it
        self.assertEqual((meta(con, "schema_version"), meta(con, "oldest_engine_schema")), ("16", "16"))
        columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        self.assertLessEqual({"workplace_hint", "board_pay_json"}, columns)
        con.close()

    def test_a_migration_that_stops_leaves_the_old_schema_whole_and_no_transaction_open(self):
        self.at_15()
        store = open_store(self.db, FixedClock(), POLICY.fingerprint, migrate=None)
        self.addCleanup(store.close)
        real = Path.read_text

        def broken(path, *a, **kw):   # the script fails half-way, after its first statement
            body = real(path, *a, **kw)
            return body + "\nINSERT INTO no_such_table VALUES (1);" if path.name.startswith("016") else body
        with mock.patch.object(Path, "read_text", broken), self.assertRaises(sqlite3.OperationalError):
            store.migrate()
        self.assertFalse(store.conn.in_transaction)
        self.assertEqual((store.schema_version(), store._meta("oldest_engine_schema")), (15, "14"))
        self.assertNotIn("workplace_hint", {r[1] for r in store.conn.execute("PRAGMA table_info(jobs)")})
        self.assertEqual(store.migrate(), newest_schema())
        self.assertEqual(store._meta("oldest_engine_schema"), str(newest_schema()))

    def test_each_migration_records_its_schema_with_its_own_commit(self):
        """A chain stopped after a migration committed (014 to 016, stopped in 016) leaves the schema that migration
        reached, so the next run never applies it twice."""
        con = sqlite3.connect(self.db)
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) <= 14:
                con.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
        con.executescript("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '14');")
        con.close()
        store = open_store(self.db, FixedClock(), POLICY.fingerprint, migrate=None)
        self.addCleanup(store.close)
        real = Path.read_text

        def broken(path, *a, **kw):
            body = real(path, *a, **kw)
            return body + "\nINSERT INTO no_such_table VALUES (1);" if path.name.startswith("016") else body
        with mock.patch.object(Path, "read_text", broken), self.assertRaises(sqlite3.OperationalError):
            store.migrate()
        self.assertEqual((store.schema_version(), store._meta("oldest_engine_schema")), (15, "14"))
        self.assertEqual(store.migrate(), newest_schema())

    def test_the_engine_before_016_refuses_the_database(self):
        open_store(self.db, FixedClock(), POLICY.fingerprint).close()   # this engine: its newest schema
        with mock.patch.object(store_mod, "newest_schema", return_value=15):   # the deployed engine (dda868e)
            for mode in (True, False):
                with self.subTest(migrate=mode), self.assertRaisesRegex(SchemaTooNew, "restore a backup"):
                    open_store(self.db, FixedClock(), POLICY.fingerprint, migrate=mode).close()

    def test_016_removes_the_retired_marks(self):
        self.at_15()
        con = sqlite3.connect(self.db)
        con.executescript("INSERT INTO meta(key, value) VALUES ('writes_mark', '{}');"
                          "INSERT INTO meta(key, value) VALUES ('labels_reconfirm_seq', '3');"
                          "INSERT INTO meta(key, value) VALUES ('handoff_owed:job-1', 'x');")
        con.close()
        store = open_store(self.db, FixedClock(), POLICY.fingerprint)
        self.addCleanup(store.close)
        self.assertEqual(store.all("SELECT key FROM meta WHERE key IN ('writes_mark', 'labels_reconfirm_seq') "
                                   "OR key LIKE 'handoff_owed:%'"), [])


class _FailingCommit:
    """A connection whose next COMMIT fails, as a full disk makes it fail."""

    def __init__(self, conn):
        self._conn, self.failed = conn, False

    def execute(self, sql, *args):
        if sql == "COMMIT" and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class CommitPathTests(unittest.TestCase):
    """Part 2 (the red team's hr1-faults-0): a COMMIT that fails rolls back and re-raises; the connection is left in no
    transaction and the next one works."""

    @staticmethod
    def event(store):
        store.writer("review").event("review_opened", outcome="informational", entity_type="review", entity_id="probe",
                                     actor_type="human", actor_id="user", metadata={})

    def test_a_failed_commit_leaves_no_transaction_open(self):
        store, _ = setup()
        real = store.conn
        store.conn = _FailingCommit(real)
        before = store.committed
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk is full"):
            with store.tx():
                self.event(store)
        self.assertFalse(real.in_transaction)
        self.assertEqual(store.committed, before)
        self.assertIsNone(store.one("SELECT 1 FROM events WHERE entity_id = 'probe'"))
        with store.tx():   # the next transaction begins and commits
            self.event(store)
        self.assertIsNotNone(store.one("SELECT 1 FROM events WHERE entity_id = 'probe'"))


class ReviewPageSchemaTests(unittest.TestCase):
    """Part 2 (H9): a review page left running across an upgrade or a restore records nothing."""

    def test_an_action_on_a_database_this_engine_may_not_write_is_refused(self):
        from careeros.review.server import _swept
        store, _ = posting_store()
        labels = store.one("SELECT COUNT(*) FROM labels")[0]
        for version, error in ((str(newest_schema() + 1), "newer than this engine"),
                               (str(newest_schema() - 1), "needs")):
            with self.subTest(schema=version):
                for key in ("schema_version", "oldest_engine_schema"):   # a newer one not marked for this engine
                    store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, version))
                with self.assertRaisesRegex(ValueError, "the database changed under this review page.*" + error):
                    with _swept(store, POLICY, MID):
                        give_label(store, POLICY, MID, JOB, "apply", None, f"late-{version}")
        self.assertEqual(store.one("SELECT COUNT(*) FROM labels")[0], labels)
        for key in ("schema_version", "oldest_engine_schema"):
            store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(newest_schema())))
        with _swept(store, POLICY, MID):   # the control
            give_label(store, POLICY, MID, JOB, "apply", None, "now")
        self.assertEqual(store.one("SELECT COUNT(*) FROM labels")[0], labels + 1)

    def test_no_engine_check_reads_another_engines_writes_any_more(self):
        for name in ("check_foreign_writes", "foreign_write_mark", "foreign_writes_pending", "WATCHED_TABLES"):
            self.assertFalse(hasattr(store_mod, name), name)   # B.6 retired (the owner's D191)


class BoardFactsTests(unittest.TestCase):
    """Part 2: migration 016 stores the board's own workplace hint and pay fields with the job, as the board gives them."""
    EMPLOYER = {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"}

    def item(self, **extra):
        return {"id": "a1", "title": "Claims Analyst", "location": "Remote - US", "jobUrl": "https://jobs.ashbyhq.com/acmerx/a1",
                "applyUrl": "https://jobs.ashbyhq.com/acmerx/a1/application", "descriptionPlain": CLAIMS,
                "publishedAt": "2026-10-10T00:00:00Z", **extra}

    def stored(self, store):
        return store.one("SELECT workplace_hint, board_pay_json FROM jobs")

    def test_the_boards_own_hint_and_pay_are_stored_and_kept_current(self):
        store, ctx = setup()
        ingest(ctx, self.EMPLOYER, [ashby_posting("acmerx", self.item())], full_board=False)
        self.assertEqual(tuple(self.stored(store)), (None, None))   # the board gives neither
        pay = {"compensation": {"summaryComponents": [{"compensationType": "Salary", "minValue": 60000, "maxValue": 70000,
                                                       "currencyCode": "USD", "interval": "1 YEAR"}]}}
        ingest(ctx, self.EMPLOYER, [ashby_posting("acmerx", self.item(workplaceType="Remote", **pay))], full_board=False)
        hint, board_pay = self.stored(store)
        self.assertEqual(hint, "Remote")
        self.assertEqual(json.loads(board_pay), {"currency": "USD", "max": 70000, "min": 60000, "period": "year"})
        ingest(ctx, self.EMPLOYER, [ashby_posting("acmerx", self.item(workplaceType="Hybrid", **pay))], full_board=False)
        self.assertEqual(self.stored(store)[0], "Hybrid")
        store.conn.execute("UPDATE jobs SET workplace_hint = NULL, board_pay_json = NULL")   # stored before 016
        ingest(ctx, self.EMPLOYER, [ashby_posting("acmerx", self.item(workplaceType="Hybrid", **pay))], full_board=False)
        self.assertEqual(self.stored(store)[0], "Hybrid")   # an unchanged read fills the board's own fields
        self.assertIsNotNone(self.stored(store)[1])
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)

    def test_board_pay_json_is_canonical_and_none_when_the_board_gives_none(self):
        self.assertIsNone(board_pay_json(None))
        self.assertIsNone(board_pay_json({"min": None, "max": None}))
        self.assertEqual(board_pay_json({"period": "year", "min": 1, "max": 2, "currency": "USD"}),
                         '{"currency": "USD", "max": 2, "min": 1, "period": "year"}')


class FailedReadHoldTests(unittest.TestCase):
    """Part 5 (batch 4's hr1-routes-0, the B.5 hold part; hr1-upgrade-2): a read that could not be stored holds the label
    of the job that is that posting, whatever failed; an older engine's failure superseded by its own write holds
    nothing."""
    LINK = "https://job-boards.greenhouse.io/synth/jobs/1"

    def pasted_then_board(self):
        from careeros.discovery.manual_intake import intake
        from tests.test_batch3 import board_read
        from tests.test_intake_identity import BODY
        from tests.test_lead_resolution import legacy_board
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        mine = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\nJob ID: R-1101\n" + BODY,
                      self.LINK)["job_id"]
        board_read(ctx, employer, full_board=True)
        return store, ctx, employer, mine, board_read

    def test_a_read_whose_job_of_record_cannot_be_found_holds_that_jobs_label(self):
        """ATTACK: the job-of-record lookup failed; the hold went to the generated id and the pasted job's label routed;
        a complete scan counted the job absent."""
        from careeros import pipeline
        from careeros.review import packages
        store, ctx, employer, mine, board_read = self.pasted_then_board()
        self.assertIsNone(packages.store_failed(store, mine))
        with mock.patch.object(pipeline, "job_of_record", side_effect=sqlite3.OperationalError("disk I/O error")):
            counts = board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202", full_board=True)
        self.assertEqual(counts["errors"], 1)
        self.assertRegex(packages.store_failed(store, mine) or "", "^ingest failed: OperationalError: disk I/O error")
        row = store.one("SELECT absent_scans, closed_at, requisition_id FROM jobs WHERE job_id = ?", (mine,))
        self.assertEqual((row["absent_scans"] or 0, row["closed_at"], row["requisition_id"]), (0, None, "R-1101"))
        board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202", full_board=True)   # stored now
        self.assertIsNone(packages.store_failed(store, mine))
        self.assertEqual(store.one("SELECT requisition_id FROM jobs WHERE job_id = ?", (mine,))[0], "R-2202")

    def test_a_hold_the_database_refuses_is_kept_beside_it_and_recorded_at_the_next_command(self):
        """VARIANT B: the posting's write failed and so did the record that would hold its label."""
        from careeros import cli, pipeline
        from careeros.db.store import Writer
        from careeros.review import packages
        store, ctx, employer, mine, board_read = self.pasted_then_board()
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(pipeline, "normalized_fields", side_effect=full), \
                mock.patch.object(Writer, "exception", side_effect=full):
            counts = board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202")
        self.assertEqual(counts["errors"], 1)
        self.assertIn("not yet recorded", packages.store_failed(store, mine) or "")
        self.assertIsNone(store.one("SELECT 1 FROM exceptions WHERE error_code = 'ingest_failed'"))
        with mock.patch("builtins.print"):
            cli._command_start(ctx, None)   # the next command records it
        self.assertEqual(packages.pending_holds(store), [])
        reason = packages.store_failed(store, mine) or ""
        self.assertTrue(reason.startswith("ingest failed: OperationalError: database or disk is full"), reason)
        self.assertNotIn("not yet recorded", reason)
        board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202")   # a later read is stored
        self.assertIsNone(packages.store_failed(store, mine))

    def test_a_stored_read_clears_the_hold_kept_beside_the_database(self):
        from careeros.review import packages
        store, ctx, employer, mine, board_read = self.pasted_then_board()
        packages.add_pending_hold(store, "greenhouse:synth:1", ("greenhouse", "1"), "ingest failed: disk full")
        self.assertIsNotNone(packages.store_failed(store, mine))   # by the posting it is, under another id
        board_read(ctx, employer)
        self.assertEqual(packages.pending_holds(store), [])
        self.assertIsNone(packages.store_failed(store, mine))

    def test_a_hold_kept_beside_a_database_file_survives_the_process(self):
        from careeros.review import packages
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "careeros.db"
            store = open_store(db, FixedClock(), POLICY.fingerprint)
            packages.add_pending_hold(store, "manual:x:1", ("greenhouse", "1"), "ingest failed: disk full")
            store.close()
            store = open_store(db, FixedClock(), POLICY.fingerprint)
            self.assertEqual([h["job_id"] for h in packages.pending_holds(store)], ["manual:x:1"])
            self.assertEqual(packages.record_pending_holds(store), 1)
            self.assertFalse(Path(str(db) + ".pending-holds.jsonl").exists())
            self.assertEqual(store.one("SELECT entity_id FROM exceptions WHERE error_code = 'ingest_failed'")[0], "manual:x:1")
            store.close()

    def test_an_older_engines_failure_stands_until_a_board_read_lifts_it(self):
        """hr1-upgrade-2's rule (an older engine's failure is superseded once a later read of the job was stored) is
        retired (Codex H22; the D190 design, section 7): it stands until a board read of the posting is stored."""
        from careeros.discovery.boards import greenhouse_posting
        from careeros.pipeline import ingest
        from careeros.review import packages
        from tests.test_v26_discovery import gh_item
        store, ctx = posting_store()
        store.writer("discovery").exception("data_quality", "medium", "ingest failed: OperationalError: database is locked",
                                            entity_type="job", entity_id=JOB, error_code="ingest_failed")   # as dda868e
        self.assertIsNotNone(packages.store_failed(store, JOB))   # held, though the job was stored at or after it
        ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
               [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))], full_board=False)
        self.assertIsNone(packages.store_failed(store, JOB))   # the board's own read lifts it


class AlertDuplicateTests(unittest.TestCase):
    """Part 5 (batch 4's hr1-alerts-3): an alert line at a posting's own link is linked to that posting's job, never
    through an older engine's duplicate to another posting."""

    @staticmethod
    def duplicate(store, ctx, job_id, why):
        with store.tx():
            store.writer("discovery").update("jobs", {"duplicate_of": "greenhouse:acmerx:4400001"}, "job_id = ?", (job_id,))
            store.writer("discovery").transition(ctx.matrix, job_id, "duplicate", reason_code=why)

    def test_an_older_engines_duplicate_of_another_posting_is_not_followed(self):
        from careeros import cli
        from careeros.discovery.boards import greenhouse_posting
        from tests.test_v26_discovery import gh_item
        store, ctx = setup()
        employer = {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"}
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(n, title, CLAIMS, "acmerx", req="R-1101"))
                               for n, title in ((4400001, "Claims Analyst"), (4400002, "Senior Claims Analyst"))],
               full_board=False)
        self.duplicate(store, ctx, "greenhouse:acmerx:4400002", "same_requisition")   # as the deployed engine did
        link = "https://job-boards.greenhouse.io/acmerx/jobs/4400002"
        self.assertEqual(cli._known_job(ctx, link), "greenhouse:acmerx:4400002")
        # the control: a duplicate that is its original's posting (a twin at the same link) is followed
        from tests.test_intake_identity import twin
        own = "https://job-boards.greenhouse.io/acmerx/jobs/4400001"
        twin(store, "manual:acme-rx:twin", own)
        self.duplicate(store, ctx, "manual:acme-rx:twin", "same_posting")
        self.assertEqual(cli._known_job(ctx, own), "greenhouse:acmerx:4400001")


class ShortlistRefreshTests(unittest.TestCase):
    """Part 5 (batch 4's hr1-identity-1 and hr1-boards-0): the shortlist refresh reads a posting by its current
    identity: the board its job now belongs to, else its own link; never a token taken from the job's id."""

    def refresh(self, ctx, job_id, posting=None, error=None):
        from careeros import cli
        asked = []

        def fetch(client, ats, token, external):
            asked.append((ats, token, external))
            if error is not None:
                raise error
            return posting
        with mock.patch("careeros.review.shortlist.shortlisted_job_ids", return_value=[job_id]), \
                mock.patch.object(cli, "fetch_single", fetch):
            counts = cli._refresh_shortlisted(ctx, None, ctx.store.clock.now())
        return asked, counts

    def test_a_renamed_boards_posting_is_read_through_its_current_board(self):
        from careeros.discovery.boards import greenhouse_posting
        from careeros.discovery.http import FetchError
        from tests.test_batch3 import board_read
        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import gh_item
        store, ctx = setup()
        old = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        new = dict(legacy_board(store, name="Acme Rx", token="acmehealth"))
        board_read(ctx, old, token="acmerx")
        board_read(ctx, new, token="acmehealth")
        asked, counts = self.refresh(ctx, "greenhouse:acmerx:1",
                                     posting=greenhouse_posting("acmehealth", gh_item(1, "Claims Analyst", CLAIMS, "acmehealth")))
        self.assertEqual(asked, [("greenhouse", "acmehealth", "1")])
        self.assertEqual(counts["shortlist_still_open"], 1)
        asked, counts = self.refresh(ctx, "greenhouse:acmerx:1", error=FetchError("not_found"))   # gone from its board
        self.assertEqual((asked, counts["shortlist_closed"]), ([("greenhouse", "acmehealth", "1")], 1))

    def test_package_questions_reads_the_posting_by_its_current_identity(self):
        from careeros import cli
        from tests.test_batch3 import board_read
        from tests.test_lead_resolution import legacy_board
        store, ctx = setup()
        old = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        new = dict(legacy_board(store, name="Acme Rx", token="acmehealth"))
        board_read(ctx, old, token="acmerx")
        board_read(ctx, new, token="acmehealth")
        asked = []

        class Client:
            def get_json(self, url):
                asked.append(url)
                return {"questions": []}
        args = mock.Mock(action="questions", target="greenhouse:acmerx:1", standing=False)
        with tempfile.TemporaryDirectory() as tmp, mock.patch("careeros.discovery.http.HttpClient", Client), \
                mock.patch("builtins.print"):
            cli.cmd_package(ctx, {"root": Path(tmp)}, args)
        self.assertEqual(asked, ["https://boards-api.greenhouse.io/v1/boards/acmehealth/jobs/1?questions=true"])

    def test_a_job_you_pasted_first_is_refreshed_by_its_posting(self):
        from careeros.discovery.http import FetchError
        from careeros.discovery.manual_intake import intake
        from tests.test_intake_identity import BODY
        store, ctx = setup()
        mine = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\nJob ID: R-1101\n" + BODY,
                      "https://job-boards.greenhouse.io/synth/jobs/1")["job_id"]
        asked, counts = self.refresh(ctx, mine, error=FetchError("not_found"))
        self.assertEqual((asked, counts["shortlist_closed"]), ([("greenhouse", "synth", "1")], 1))
        # the control: a job whose link names no posting is never read by a guess
        other = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + BODY,
                       "https://careers.example.com/join")["job_id"]
        self.assertEqual(self.refresh(ctx, other, error=FetchError("not_found"))[0], [])


class PlacementTests(unittest.TestCase):
    """Part 4 (screening quality, advice under D191; Codex's D190 design review H2 and H6; batch 4's hr1-boards-2 and
    hr1-saved-3): typed countries per adapter, the ambiguous-place list, the owner settling a board job's location, the
    country recomputed with every location write, and a saved page whose own posting cannot be told apart."""

    def test_each_adapter_keeps_the_countries_its_board_types(self):
        from careeros.discovery.boards import ashby_posting, greenhouse_posting, lever_posting, workable_posting
        from tests.test_v26_discovery import gh_item
        lever = lever_posting("acme", {"id": "l1", "text": "Claims Analyst", "country": "AU",
                                       "categories": {"location": "Perth, WA"}, "descriptionPlain": CLAIMS})
        ashby = ashby_posting("acme", {"id": "a1", "title": "Claims Analyst", "location": "Toronto",
                                       "address": {"postalAddress": {"addressCountry": "Canada"}}, "descriptionPlain": CLAIMS})
        workable = workable_posting("acme", {"shortcode": "w1", "title": "Claims Analyst", "country": "US",
                                             "locations": [{"city": "Austin", "region": "TX", "country": "US"},
                                                           {"city": "Toronto", "region": "ON", "countryCode": "CA"}]})
        self.assertEqual(lever.typed_countries, ["Australia"])
        self.assertEqual(ashby.typed_countries, ["Canada"])
        self.assertEqual(workable.typed_countries, ["US", "Canada"])
        self.assertEqual(greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth")).typed_countries, [])

    def test_a_typed_country_abroad_wins_and_disagreeing_ones_leave_it_unknown(self):
        from careeros.normalize.posting import placed_country
        for location, typed, country in (("Remote", ["Canada"], "non_US"), ("Austin, TX", ["Australia"], "non_US"),
                                         ("Austin, TX", ["US"], "US"), ("Remote", ["US"], "unknown"),
                                         ("Austin, TX; Toronto, ON", ["US", "Canada"], "unknown"),
                                         ("Austin, TX", None, "US")):
            with self.subTest(location=location, typed=typed):
                self.assertEqual(placed_country("Claims Analyst", location, "", "remote", typed), country)

    def test_an_ambiguous_place_lowers_a_us_reading_to_unknown(self):
        from careeros.normalize.posting import placed_country
        for location, country in (("Perth, WA", "unknown"), ("Vancouver, WA", "unknown"), ("Melbourne, FL", "unknown"),
                                  ("Perth, WA, Australia", "non_US"), ("Perth, WA; Austin, TX", "unknown"),
                                  ("Seattle, WA", "US"), ("Orlando, FL", "US"), ("Toronto, ON", "non_US")):
            with self.subTest(location=location):
                self.assertEqual(placed_country("Claims Analyst", location, "", "remote"), country)

    def test_the_typed_countries_are_kept_with_the_job_and_read_again(self):
        from careeros.discovery.boards import lever_posting
        from careeros.normalize.posting import placed_country
        from careeros.pipeline import refresh_derived_fields
        store, ctx = setup()
        ingest(ctx, {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"},
               [lever_posting("acme", {"id": "l1", "text": "Claims Analyst", "country": "CA",
                                       "categories": {"location": "Austin, TX"}, "descriptionPlain": CLAIMS})], full_board=False)
        row = store.one("SELECT country, typed_countries_json, location_raw FROM jobs")
        self.assertEqual((row["country"], json.loads(row["typed_countries_json"])), ("non_US", ["Canada"]))
        # the field alone ("Austin, TX; Canada") reads unknown: only the typed country places it abroad
        self.assertEqual(placed_country("Claims Analyst", row["location_raw"], CLAIMS, "remote"), "unknown")
        refresh_derived_fields(ctx)   # a processing run reads the stored field and the typed countries together
        self.assertEqual(store.one("SELECT country FROM jobs")[0], "non_US")

    def test_an_older_row_gets_its_typed_countries_at_the_first_read_after_the_upgrade(self):
        from careeros.discovery.boards import lever_posting
        store, ctx = setup()
        employer = {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"}
        posting = lever_posting("acme", {"id": "l1", "text": "Claims Analyst", "country": "CA",
                                         "categories": {"location": "Remote"}, "descriptionPlain": CLAIMS})
        ingest(ctx, employer, [posting], full_board=False)
        store.conn.execute("UPDATE jobs SET typed_countries_json = NULL, country = 'unknown'")   # as stored before 016
        ingest(ctx, employer, [posting], full_board=False)   # the same posting, unchanged
        self.assertEqual(tuple(store.one("SELECT country, typed_countries_json FROM jobs")), ("non_US", '["Canada"]'))

    def board_job(self, location):
        from careeros.discovery.boards import greenhouse_posting
        from tests.test_v26_discovery import gh_item
        store, ctx = setup()
        employer = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}

        def read(where, text=CLAIMS):
            ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", text, "synth", location=where))],
                   full_board=False)
        read(location)
        return store, ctx, read

    def test_the_owner_settles_a_board_job_its_field_leaves_unknown(self):
        from careeros.discovery.manual_intake import set_location
        store, ctx, read = self.board_job("Perth, WA")
        job = "greenhouse:synth:1"
        self.assertEqual(store.one("SELECT country FROM jobs")[0], "unknown")
        self.assertTrue(set_location(ctx, job, "Remote - US")["changed"])
        row = store.one("SELECT location_raw, country, remote_scope FROM jobs")
        self.assertEqual((row["location_raw"], row["country"]), ("Remote - US", "US"))   # at once, no processing run
        for edit in ("Edited.", "Edited twice."):   # the board's field you settled: yours stays, read after read
            read("Perth, WA", CLAIMS + "\n" + edit)
            self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs")), ("Remote - US", "US"))
        read("Perth, WA")   # an unchanged read too
        self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs")), ("Remote - US", "US"))
        set_location(ctx, job, "Austin, TX")   # you may settle it again while yours stands
        read("Perth, WA", CLAIMS + "\nEdited again.")
        self.assertEqual(store.one("SELECT location_raw FROM jobs")[0], "Austin, TX")
        read("Perth, WA, Australia", CLAIMS + "\nEdited.")   # another field from the board supersedes yours
        self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs")), ("Perth, WA, Australia", "non_US"))
        set_location(ctx, job, "Remote - US")   # settled again: bound to the board's field now
        read("Perth, WA, Australia", CLAIMS + "\nOnce more.")
        self.assertEqual(store.one("SELECT location_raw FROM jobs")[0], "Remote - US")

    def test_a_paste_with_a_location_points_to_settling_a_board_job_its_field_leaves_unknown(self):
        from careeros.discovery.manual_intake import intake
        from tests.test_intake_identity import BODY
        store, ctx, _ = self.board_job("Perth, WA")
        link = store.one("SELECT url FROM jobs")[0]
        note = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + BODY, link, location="Remote - US")["location_note"]
        self.assertIn('leaves unknown or abroad; to settle it, run careeros location greenhouse:synth:1 "Remote - US"', note)
        self.assertEqual(store.one("SELECT location_raw FROM jobs")[0], "Perth, WA")   # a paste changes nothing

    def test_a_board_job_its_board_places_in_the_us_keeps_its_location(self):
        from careeros.discovery.manual_intake import set_location
        store, ctx, _ = self.board_job("Austin, TX")
        with self.assertRaisesRegex(ValueError, "places in the US"):
            set_location(ctx, "greenhouse:synth:1", "Toronto, ON")

    def test_a_location_you_set_recomputes_the_country_in_its_own_transaction(self):
        from careeros.discovery.manual_intake import intake, set_location
        from tests.test_intake_identity import BODY
        store, ctx = setup()
        job = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + BODY,
                     "https://careers.example.com/join")["job_id"]
        set_location(ctx, job, "Toronto, Ontario, Canada")
        self.assertEqual(store.one("SELECT country FROM jobs WHERE job_id = ?", (job,))[0], "non_US")

    def page(self, *blocks):
        return ('<html><head><link rel="canonical" href="https://careers.acme.example/jobs/1">'
                + "".join('<script type="application/ld+json">' + json.dumps(b) + "</script>" for b in blocks)
                + "</head><body></body></html>")

    def posting(self, title, place):
        return {"@type": "JobPosting", "title": title, "description": "<p>" + CLAIMS + "</p>",
                "hiringOrganization": {"@type": "Organization", "name": "Acme Rx"},
                "jobLocation": {"@type": "Place", "address": {"addressLocality": place, "addressCountry": "CA"}}}

    def test_a_saved_page_whose_own_posting_is_nested_beside_another_is_refused(self):
        from careeros.discovery import saved
        own = {"@context": "https://schema.org", "@type": "WebPage",
               "mainEntity": self.posting("Claims Analyst", "Toronto")}
        featured = {"@context": "https://schema.org", **self.posting("Billing Specialist", "Ottawa")}
        with self.assertRaisesRegex(saved.SavedPostingError, "2 job postings"):
            saved.parse_saved_html(self.page(own, featured), "p.html")
        raw, _ = saved.parse_saved_html(self.page(own), "p.html")   # its own posting alone, nested: read
        self.assertEqual(raw.title, "Claims Analyst")


class AlertCardTests(unittest.TestCase):
    """Part 3 (the D190 design, revision 3, section 5; Codex's D190 design review H7's parser part, H8, M3, M4): every card
    occurrence is kept and keyed; a card whose own link names no posting is a notice, never a lead; an agency job
    number repeated in either order is a possible repeat, never "seen" and never closed; no older text-based association
    hides a new card; a careers page's Greenhouse id is a suggestion, never fetched. Held for rev 3: the receipt and the
    fetch-only transaction step."""
    CAREERS = "https://careers.acme.example/jobs"
    LINKEDIN = "https://www.linkedin.com/jobs/view/4012345671"

    def setUp(self):
        import httpx

        from careeros.core.credentials import MemoryCredentials
        from careeros.discovery import gmail
        from careeros.discovery.http import HttpClient
        from careeros.pipeline import Context
        from careeros.qualify.profile import load_profile
        from tests.helpers import JOB_MATRIX
        from tests.test_gmail import CLIENT
        from tests.test_saved import POLICY as SAVED_POLICY
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), SAVED_POLICY.fingerprint)
        self.ctx = Context(self.store, SAVED_POLICY, load_profile(Path(__file__).parent / "fixtures" / "synthetic_profile.yaml"),
                           JOB_MATRIX, Path(self.tmp.name))
        self.creds = MemoryCredentials()
        self.creds.write(gmail.CREDENTIAL_TARGET, json.dumps({**CLIENT, "refresh_token": "r1"}))
        self.requests = []
        self.boards = HttpClient(transport=httpx.MockTransport(lambda r: self.requests.append(str(r.url)) or httpx.Response(404)),
                                 min_interval=0, sleep=lambda s: None)
        self.addCleanup(self.boards.close)

    def html(self):
        return (f'<table><tr><td><a href="{self.CAREERS}">Claims Analyst</a><br>Acme Rx · Remote</td></tr>'
                f'<tr><td><a href="{self.CAREERS}">Claims Analyst</a><br>Acme Rx · Remote</td></tr>'   # one card linked twice
                f'<tr><td><a href="{self.CAREERS}">Senior Claims Analyst</a><br>Acme Rx · Remote</td></tr>'
                f'<tr><td><a href="{self.LINKEDIN}">Claims Analyst</a><br>Synthetic Health · Remote</td></tr>'
                f'<tr><td><a href="{self.LINKEDIN}">Claims Analyst</a><br>Synthetic Health · Remote</td></tr></table>')

    def record(self, data, key=None):
        from careeros.cli import _record_alert
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_alerts import alert_email
        alert = parse_eml_bytes(alert_email(data) if isinstance(data, str) else data, message_key=key)
        outcomes = []
        return alert, _record_alert(self.ctx, alert, self.boards, outcomes), outcomes

    def test_every_card_occurrence_is_kept_and_keyed_by_its_email_and_position(self):
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_alerts import alert_email
        data = alert_email(self.html())
        alert = parse_eml_bytes(data)
        self.assertEqual([link.url for link in alert.links], [self.CAREERS] * 3 + [self.LINKEDIN] * 2)
        self.assertEqual(len({link.occurrence for link in alert.links}), 5)   # never merged by link, title or identity
        self.assertTrue(all(link.occurrence.startswith(alert.message_key + "#") for link in alert.links))
        self.assertTrue(alert.message_key.startswith("eml:"))
        self.assertEqual(parse_eml_bytes(data).message_key, alert.message_key)   # the same bytes, the same keys
        self.assertEqual(parse_eml_bytes(data, message_key="gmail:m1").links[0].occurrence, "gmail:m1#0")
        # an agency digest's cards too, one key each, the same job linked twice included
        from tests.test_alerts import AG_1, agency_card, agency_email
        digest = parse_eml_bytes(agency_email(agency_card("Claims Analyst", AG_1) + agency_card("Claims Analyst", AG_1)),
                                 message_key="gmail:m2")
        self.assertEqual([card.occurrence for card in digest.links], ["gmail:m2#card0", "gmail:m2#card1"])

    def test_a_card_whose_link_names_no_posting_is_a_notice_never_a_lead(self):
        from tests.test_alerts import alert_email
        data = alert_email(self.html())
        _, counts, outcomes = self.record(data)
        self.assertEqual([o for _, o in outcomes], ["notice"] * 3 + ["lead", "seen_before"])
        self.assertEqual([r["url"] for r in self.store.all("SELECT url FROM leads")], [self.LINKEDIN])
        notices = self.store.all("SELECT title, state, suggested_employer FROM alert_notices ORDER BY notice_id")
        self.assertEqual([(n["title"], n["state"], n["suggested_employer"]) for n in notices],
                         [("Claims Analyst", "new", "Acme Rx")] * 2 + [("Senior Claims Analyst", "new", "Acme Rx")])
        _, again, _ = self.record(data)   # the same email read again adds nothing
        self.assertEqual((again["notice_seen_before"], again["notice_new"]), (3, 0))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM alert_notices")[0], 3)

    def test_no_older_lead_for_the_same_page_and_title_hides_a_new_card(self):
        """Codex M4: a lead stored before D190 for the careers page with the same title and stated identity."""
        from careeros.discovery.alerts import lead_id
        self.store.writer("discovery").insert("leads", {
            "lead_id": lead_id(self.CAREERS), "url": self.CAREERS, "site": "other", "title": "Claims Analyst",
            "title_norm": "claims analyst", "context": "Acme Rx · Remote", "first_seen_at": self.store.clock.now(),
            "status": "new", "profile": "primary"})
        _, counts, _ = self.record(f'<a href="{self.CAREERS}">Claims Analyst</a><br>Acme Rx · Remote')
        self.assertEqual(counts["notice_new"], 1)

    def test_a_careers_page_greenhouse_id_is_a_suggestion_never_fetched(self):
        link = "https://careers.acme.example/jobs?gh_jid=4400001"
        self.record(f'<a href="{link}">Claims Analyst</a><br>Acme Rx · Remote')
        notice = self.store.one("SELECT suggestion_json FROM alert_notices")
        self.assertIn("4400001", json.loads(notice["suggestion_json"])["greenhouse_posting"])
        self.assertEqual(self.requests, [])   # nothing fetched
        self.assertIsNone(self.store.one("SELECT 1 FROM jobs"))

    def test_a_routed_careers_page_greenhouse_id_is_never_suggested(self):
        """Codex Pass 14 H35: a '#' route names another opening than the gh_jid."""
        self.record('<a href="https://careers.acme.example/portal?gh_jid=4400001#/opening/R-2222">Claims Analyst</a>'
                    '<br>Acme Rx · Remote')
        self.assertEqual(self.store.one("SELECT suggestion_json FROM alert_notices")[0], "{}")

    def test_a_review_link_that_reads_as_a_board_posting_stays_a_lead_never_fetched(self):
        """D140 F2 and D142 R-b: a link kept for review is a lead, never a notice, and never fetched, whatever board
        posting it also reads as (here a Greenhouse link wrapping a LinkedIn job)."""
        link = "https://job-boards.greenhouse.io/synth/jobs/555?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4012345671"
        _, counts, outcomes = self.record(f'<a href="{link}">Claims Analyst</a><br>Synthetic Health · Remote')
        self.assertEqual(([o for _, o in outcomes], self.requests), (["lead"], []))
        self.assertEqual(tuple(self.store.one("SELECT status, job_id FROM leads")), ("new", None))
        self.assertIsNone(self.store.one("SELECT 1 FROM alert_notices"))

    def test_a_night_alerts_notice_is_a_night_notice(self):
        from careeros.cli import _record_alert
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_alerts import alert_email
        alert = parse_eml_bytes(alert_email(f'<a href="{self.CAREERS}">Claims Analyst</a><br>Acme Rx · Remote'))
        counts = _record_alert(self.ctx, alert, self.boards, [], profile="night")
        self.assertEqual((counts["notice_new"], self.store.one("SELECT profile FROM alert_notices")[0]), (1, "night"))

    def test_notices_are_shown_grouped_and_dismissed_or_added(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from careeros import cli
        from careeros.review import notices
        from careeros.review.report import _notices_section
        self.record(self.html())
        self.record('<a href="https://careers.other.example/open">Claims Analyst</a>', key="eml:other")
        groups = notices.groups(self.store)
        self.assertEqual(sorted(len(rows) for _, rows in groups), [1, 3])
        page = "".join(_notices_section(self.store, "t", {}, self.ctx.policy))
        self.assertIn("Notices from alerts (4)", page)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_notices(self.ctx, {}, SimpleNamespace(action="list", target=None, all=False))
        self.assertIn("4 notice(s)", out.getvalue())
        first = self.store.one("SELECT notice_id FROM alert_notices ORDER BY notice_id")[0]
        notices.dismiss_notice(self.store, first, key="k1")
        self.assertEqual(self.store.one("SELECT state FROM alert_notices WHERE notice_id = ?", (first,))[0], "dismissed")
        self.assertEqual(sum(len(rows) for _, rows in notices.groups(self.store)), 3)
        with tempfile.TemporaryDirectory() as tmp:   # add the posting by its own text, at the notice's link
            text = Path(tmp) / "posting.txt"
            text.write_text("Claims Analyst\n" + CLAIMS, encoding="utf-8")
            second = self.store.one("SELECT notice_id FROM alert_notices WHERE state = 'new' ORDER BY notice_id")[0]
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_notices(self.ctx, {"root": Path(tmp)}, SimpleNamespace(
                    action="add", target=second, all=False, url=None, text_file=str(text), employer=None, title=None,
                    location=None))
        row = self.store.one("SELECT state, job_id FROM alert_notices WHERE notice_id = ?", (second,))
        self.assertEqual(row["state"], "added")
        self.assertIsNotNone(self.store.one("SELECT 1 FROM jobs WHERE job_id = ?", (row["job_id"],)))
        self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url LIKE 'https://careers.%'"))   # never a lead
        notices.dismiss_notice(self.store, second, key="k3")   # a notice already added is never dismissed
        self.assertEqual(self.store.one("SELECT state FROM alert_notices WHERE notice_id = ?", (second,))[0], "added")
        # the tracked postings beside a notice are the named employer's only, with its title (computed, read-only)
        self.record('<a href="https://careers.northwind.example/jobs">Claims Analyst</a><br>Northwind Rx · Remote',
                    key="eml:nw")
        acme = self.store.one("SELECT * FROM alert_notices WHERE notice_id = ?", (first,))
        north = self.store.one("SELECT * FROM alert_notices WHERE suggested_employer = 'Northwind Rx'")
        self.assertEqual(([j["job_id"] for j in notices.likely_postings(self.store, self.ctx.policy, acme)],
                          notices.likely_postings(self.store, self.ctx.policy, north)), ([row["job_id"]], []))

    def gmail(self, messages):
        from careeros.cli import _gmail_pull
        from careeros.discovery import gmail
        from tests.test_gmail import FakeGoogle
        google = FakeGoogle()
        google.messages = messages
        reader = gmail.GmailClient(self.creds, http=__import__("httpx").Client(transport=__import__("httpx").MockTransport(google.handler)))
        return _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=reader)

    def test_an_agency_job_number_repeated_in_either_order_is_a_possible_repeat(self):
        """Codex H8: the reverse order marked the agency card seen and kept it as a closed twin."""
        from tests.test_alerts import AG_1, agency_card, agency_email, alert_email
        linkedin = (f'<a href="{self.LINKEDIN}">Claims Analyst</a>'
                    "<br>Harbor Staffing · Remote · req=04530-0012345678")
        card = agency_email(agency_card("Claims Analyst", AG_1, description="Job ID: 04530-0012345678"))
        counts = self.gmail({"m1": alert_email(linkedin), "m2": card})
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))
        rows = {r["site"]: r for r in self.store.all("SELECT * FROM leads")}
        self.assertEqual({r["status"] for r in rows.values()}, {"new"})   # no closed twin
        agency = next(r for r in rows.values() if r["lead_id"].startswith("lead:agency:"))
        public = next(r for r in rows.values() if not r["lead_id"].startswith("lead:agency:"))
        self.assertEqual((agency["repeat_of"], public["repeat_of"]), (public["lead_id"], None))

    def test_the_other_order_too(self):
        from tests.test_alerts import AG_1, agency_card, agency_email, alert_email
        linkedin = (f'<a href="{self.LINKEDIN}">Claims Analyst</a>'
                    "<br>Harbor Staffing · Remote · req=04530-0012345678")
        card = agency_email(agency_card("Claims Analyst", AG_1, description="Job ID: 04530-0012345678"))
        counts = self.gmail({"m1": card, "m2": alert_email(linkedin)})
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))
        agency = self.store.one("SELECT lead_id FROM leads WHERE lead_id LIKE 'lead:agency:%'")[0]
        public = self.store.one("SELECT repeat_of FROM leads WHERE lead_id NOT LIKE 'lead:agency:%'")[0]
        self.assertEqual(public, agency)
        # shown beside the lead, on the listing and on the review page (advice only)
        import contextlib
        import io
        from types import SimpleNamespace

        from careeros import cli
        from careeros.review.report import _leads_section
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_leads(self.ctx, {}, SimpleNamespace(action="list", all=False, target=None))
        self.assertIn(f"possibly the same as {agency} (the same job number)", out.getvalue())
        self.assertIn(f"possibly the same as lead {agency}", "".join(_leads_section(self.store, None, {})))

    def test_two_other_lines_with_one_requisition_are_no_possible_repeat(self):
        """D188: only an agency card's job number counts, never another site's line with the same requisition."""
        from tests.test_alerts import alert_email
        lines = "".join(f'<tr><td><a href="https://www.linkedin.com/jobs/view/40123456{n}">Claims Analyst</a>'
                        "<br>Harbor Staffing · Remote · req=04530-0012345678</td></tr>" for n in (71, 72))
        counts = self.gmail({"m1": alert_email(f"<table>{lines}</table>")})
        self.assertEqual(counts["lead_new"], 2)
        self.assertEqual([r[0] for r in self.store.all("SELECT repeat_of FROM leads")], [None, None])


class NoticeReviewPageTests(ServerMixin, unittest.TestCase):
    """Part 3: the review page shows the notices and dismisses one through the server (its schema checked per POST)."""

    def test_the_page_shows_a_notice_and_dismisses_it(self):
        self.store.writer("discovery").insert("alert_notices", {
            "notice_id": "eml:synthetic#0", "message_key": "eml:synthetic", "position": "0",
            "url": "https://careers.acme.example/jobs", "site": "other", "title": "Claims Analyst", "title_norm": "claims analyst",
            "context": "Acme Rx · Remote", "sender": "alerts@example.com", "first_seen_at": self.store.clock.now(),
            "state": "new"})
        page = self.get()
        self.assertIn('id="notices">Notices from alerts (1)', page)
        self.post("notice", target="eml:synthetic#0", key=self.form(page, "notice", "eml:synthetic#0"))
        self.assertEqual(self.store.one("SELECT state FROM alert_notices")[0], "dismissed")
        self.assertNotIn('id="notices"', self.get())


class _FullDisk:
    """A connection whose next statement that `fires` meets a REAL full disk: the database is capped at its size and the
    statement's longest text is padded, so SQLite raises SQLITE_FULL and handles it as it really does, ending the whole
    transaction (the red team's rtv_hr1faults_1 on 74807d3)."""

    def __init__(self, conn, fires):
        self._conn, self._fires, self.fired = conn, fires, []

    def execute(self, sql, params=()):
        if not self.fired and self._fires(sql, params):
            self.fired.append(sql[:40])
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            self._conn.execute(f"PRAGMA max_page_count = {pages}")
            values = list(params)
            longest = max((i for i, v in enumerate(values) if isinstance(v, str)), key=lambda i: len(values[i]))
            values[longest] += " " * 60000
            try:
                return self._conn.execute(sql, tuple(values))
            finally:
                self._conn.execute("PRAGMA max_page_count = 1073741823")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TransactionLostTests(unittest.TestCase):
    """The D190 design, revision 4, section 7 (the owner's go-ahead, 2026-11-23; found by the red team's rtv_hr1faults_1 on
    74807d3): when SQLite answers a full disk by ending the whole transaction, nested work raises TransactionLost instead
    of "no such savepoint", nothing carries on writing outside the transaction, and the lost posting's label is held."""

    @staticmethod
    def event(store, entity_id):
        store.writer("review").event("review_opened", outcome="informational", entity_type="review", entity_id=entity_id,
                                     actor_type="human", actor_id="user", metadata={})

    @staticmethod
    def kept(store, entity_id):
        return store.one("SELECT 1 FROM events WHERE entity_id = ?", (entity_id,)) is not None

    def full_on(self, store, fires):
        disk = _FullDisk(store.conn, fires)
        store.conn = disk
        self.addCleanup(setattr, store, "conn", disk._conn)
        return disk

    def test_a_nested_failure_that_ends_the_transaction_is_lost_inside_and_its_cause_outside(self):
        store, _ = setup()
        disk = self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO events") and "second" in params)
        inside = []
        with self.assertRaisesRegex(sqlite3.OperationalError, "^database or disk is full$"):
            with store.tx():
                self.event(store, "first")
                try:
                    with store.tx():
                        self.event(store, "second")
                except store_mod.TransactionLost as lost:
                    inside.append(lost)
                    raise
        self.assertEqual(len(disk.fired), 1)
        self.assertEqual(len(inside), 1)
        self.assertRegex(str(inside[0].original), "disk is full")   # never "no such savepoint"
        self.assertFalse(store.conn.in_transaction)
        self.assertFalse(self.kept(store, "first") or self.kept(store, "second"))
        with store.tx():   # the next transaction begins and commits
            self.event(store, "third")
        self.assertTrue(self.kept(store, "third"))

    def test_a_loss_caught_by_code_in_between_is_never_a_success(self):
        store, _ = setup()
        self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO events") and "second" in params)
        with self.assertRaises(store_mod.TransactionLost) as nested:   # caught inside a nested frame: found at its end
            with store.tx():
                self.event(store, "first")
                with store.tx():
                    try:
                        self.event(store, "second")
                    except store_mod.TransactionLost:
                        pass
        self.assertIsNone(nested.exception.original)
        self.assertFalse(self.kept(store, "first"))
        self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO events") and "fifth" in params)
        with self.assertRaises(store_mod.TransactionLost):   # caught at the top: never committed as if whole
            with store.tx():
                self.event(store, "fourth")
                try:
                    self.event(store, "fifth")
                except store_mod.TransactionLost:
                    pass
        self.assertFalse(self.kept(store, "fourth"))
        self.assertFalse(store.conn.in_transaction)

    def test_a_statement_failure_that_leaves_the_transaction_open_undoes_only_its_own_work(self):
        """CONTROL: a failure SQLite answers with the statement alone (the transaction stays open)."""
        store, _ = setup()
        with store.tx():
            self.event(store, "first")
            with self.assertRaises(sqlite3.IntegrityError):
                with store.tx():
                    store.conn.execute("INSERT INTO meta(key, value) VALUES ('probe-lost', '1')")
                    store.conn.execute("INSERT INTO meta(key, value) VALUES ('probe-lost', '2')")
            self.assertTrue(store.conn.in_transaction)
        self.assertTrue(self.kept(store, "first"))
        self.assertIsNone(store.one("SELECT 1 FROM meta WHERE key = 'probe-lost'"))

    def test_a_scan_posting_whose_transaction_is_lost_holds_its_label_with_the_cause_and_the_next_one_is_stored(self):
        """rtv_hr1faults_1, window_real: the hold said "no such savepoint: careeros_2". A scan's postings are each their
        own transaction, so one lost posting never stops the run."""
        from careeros.discovery.boards import greenhouse_posting
        from careeros.review import packages
        from tests.test_v26_discovery import gh_item
        store, ctx, employer, mine, board_read = FailedReadHoldTests().pasted_then_board()
        disk = self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO job_changes"))
        counts = ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Senior Claims Analyst", CLAIMS, "synth",
                                                                            req="R-2202")),
                                        greenhouse_posting("synth", gh_item(2, "Appeals Analyst", CLAIMS, "synth"))])
        self.assertEqual(len(disk.fired), 1)
        self.assertEqual((counts["errors"], counts["new"]), (1, 1))
        reason = packages.store_failed(store, mine) or ""
        self.assertTrue(reason.startswith("ingest failed: OperationalError: database or disk is full"), reason)
        self.assertEqual(store.one("SELECT requisition_id FROM jobs WHERE job_id = ?", (mine,))[0], "R-1101")
        self.assertIsNotNone(store.one("SELECT 1 FROM jobs WHERE job_id = 'greenhouse:synth:2'"))
        board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202")   # stored next time: the hold is resolved
        self.assertIsNone(packages.store_failed(store, mine))

    def test_an_ingest_nested_in_an_alert_email_stops_the_email_and_holds_the_posting(self):
        """Nested work (an alert email's one transaction): the failed posting was counted and the email carried on
        writing with no transaction open."""
        from careeros.review import packages
        store, ctx, employer, mine, board_read = FailedReadHoldTests().pasted_then_board()
        disk = self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO job_changes"))
        with self.assertRaisesRegex(sqlite3.OperationalError, "^database or disk is full$"):
            with store.tx():   # as cmd_import, the Gmail pull and the inbox read one email
                self.event(store, "email-before")
                board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202")
                self.event(store, "email-after")
        self.assertEqual(len(disk.fired), 1)
        self.assertFalse(store.conn.in_transaction)
        self.assertFalse(self.kept(store, "email-before") or self.kept(store, "email-after"))
        reason = packages.store_failed(store, mine) or ""   # written in a fresh transaction before stopping
        self.assertTrue(reason.startswith("ingest failed: OperationalError: database or disk is full"), reason)
        self.assertEqual(store.one("SELECT requisition_id FROM jobs WHERE job_id = ?", (mine,))[0], "R-1101")

    def test_a_card_whose_email_transaction_is_lost_stops_the_email_and_is_not_a_line_error(self):
        """The per-card handler counted the loss as one bad line and wrote the other cards with no transaction open."""
        import httpx

        from careeros.cli import _record_alert
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.http import HttpClient
        from careeros.pipeline import Context
        from careeros.qualify.profile import load_profile
        from tests.helpers import JOB_MATRIX
        from tests.test_alerts import alert_email
        from tests.test_saved import POLICY as SAVED_POLICY
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), SAVED_POLICY.fingerprint)
        ctx = Context(store, SAVED_POLICY, load_profile(Path(__file__).parent / "fixtures" / "synthetic_profile.yaml"),
                      JOB_MATRIX, Path(tmp.name))
        boards = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), min_interval=0, sleep=lambda s: None)
        self.addCleanup(boards.close)
        careers = "https://careers.acme.example/jobs"
        alert = parse_eml_bytes(alert_email(
            f'<table><tr><td><a href="{careers}">Claims Analyst</a><br>Acme Rx · Remote</td></tr>'
            f'<tr><td><a href="{careers}">Senior Claims Analyst</a><br>Acme Rx · Remote</td></tr></table>'),
            message_key="eml:lost")
        seen = []
        disk = self.full_on(store, lambda sql, params: sql.startswith("INSERT INTO alert_notices")
                            and seen.append(1) is None and len(seen) == 2)
        with self.assertRaisesRegex(sqlite3.OperationalError, "^database or disk is full$"):
            with store.tx():
                _record_alert(ctx, alert, boards)
        self.assertEqual(len(disk.fired), 1)
        self.assertIsNone(store.one("SELECT 1 FROM alert_notices"))   # the first card's notice went with the email
        self.assertIsNone(store.one("SELECT 1 FROM exceptions WHERE error_code = 'alert_line_failed'"))
        with store.tx():   # read again next time: every card kept
            counts = _record_alert(ctx, alert, boards)
        self.assertEqual((counts["notice_new"], counts["lead_errors"]), (2, 0), counts)
        self.assertEqual(store.one("SELECT COUNT(*) FROM alert_notices")[0], 2)


class RoundNineteenTests(unittest.TestCase):
    """The round-19 hand check (D192, at 74807d3): defects no test caught once part 3 retired the Greenhouse guess and
    the alert cards' old routes, and once part 1 left every sweep to remove every handoff file."""

    @staticmethod
    def _agency_host():
        from urllib.parse import urlsplit

        from tests.test_alerts import AGENCY_TRACKER
        return urlsplit(AGENCY_TRACKER).hostname

    def _alert(self, ctx, url, posting=None, title="Claims Analyst", context="Synthetic Health \u00b7 Remote"):
        from careeros import cli
        from careeros.discovery.alerts import AlertEmail, Link
        fetch = (mock.patch.object(cli, "fetch_single", return_value=posting) if posting is not None
                 else mock.patch.object(cli, "fetch_single", side_effect=AssertionError("fetched")))
        with fetch as fetched:
            counts = cli._record_alert(ctx, AlertEmail("s", "t", None, [Link(url, "other", title, context, True)]),
                                       mock.MagicMock())
        return counts, fetched.call_count

    def test_an_agency_cards_link_is_seen_before_while_the_setting_is_off_and_never_stored_again(self):
        """CR-AD-06 after part 3: an agency card's link that names a board posting, or wraps a link kept for review, is
        no notice while the agency setting is off; the stored agency card makes it seen before, so the personal link
        is never stored again as an ordinary lead, or fetched."""
        import os
        from urllib.parse import quote

        from careeros.core import private_sites
        from careeros.discovery.alerts import enroll_agency_hosts, is_local_link, kept_for_review
        from careeros.discovery.board_discovery import names_one_posting
        from tests.test_alerts import AGENCY_TRACKER
        store, ctx = setup()
        enroll_agency_hosts(store)
        review = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"
        for url in (f"https://job-boards.greenhouse.io/synth/jobs/4400091?src={self._agency_host()}",
                    AGENCY_TRACKER + "?url=" + quote(review, safe="")):
            with self.subTest(url=url):
                self.assertTrue(is_local_link(url))   # the setting in place: an agency card
                counts, fetched = self._alert(ctx, url)
                self.assertEqual((counts["lead_new"], fetched), (1, 0))
                with mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # the setting gone
                    self.assertFalse(is_local_link(url))
                    self.assertTrue(names_one_posting(url) or kept_for_review(url))   # so never a notice
                    counts, fetched = self._alert(ctx, url)
                    self.assertEqual((counts["lead_seen_before"], counts["lead_new"], counts["notice_new"], fetched),
                                     (1, 0, 0, 0))
                self.assertEqual([r["lead_id"][:12] for r in store.all("SELECT lead_id FROM leads WHERE url = ?", (url,))],
                                 ["lead:agency:"])

    def test_an_agency_card_naming_a_board_posting_is_never_matched_or_fetched(self):
        """The agency merge after part 3: a board posting's link carrying an agency's tracking host is an agency card,
        kept local: never matched to the tracked posting it names, and never fetched, tracked or not."""
        from careeros.discovery.alerts import enroll_agency_hosts, is_local_link
        from careeros.discovery.boards import greenhouse_posting

        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import gh_item
        store, ctx = setup()
        enroll_agency_hosts(store)
        legacy_board(store, name="Synthetic Health", token="synth")
        ingest(ctx, dict(store.one("SELECT * FROM employers")),
               [greenhouse_posting("synth", gh_item(4400092, "Claims Analyst", CLAIMS, "synth"))])
        self.assertTrue(store.one("SELECT 1 FROM jobs WHERE job_id = 'greenhouse:synth:4400092'"))
        for number in (4400092, 4400093):   # tracked, then not tracked
            url = f"https://job-boards.greenhouse.io/synth/jobs/{number}?src={self._agency_host()}"
            with self.subTest(url=url):
                self.assertTrue(is_local_link(url))
                counts, fetched = self._alert(ctx, url)
                self.assertEqual((counts["lead_new"], counts["lead_ingested"], counts["lead_errors"], fetched), (1, 0, 0, 0))
                self.assertEqual(tuple(store.one("SELECT status, job_id FROM leads WHERE url = ?", (url,))), ("new", None))

    def test_a_fetch_by_a_lines_own_link_answered_with_another_posting_keeps_the_lead_open(self):
        """Pass 14 H35 after part 3: a line is fetched only by its own link, and a board can answer it with another
        posting (fetch_single never compares the id it gets with the one it asked for). That posting is graded as any
        posting; the line's lead stays open, linked to nothing."""
        from careeros.discovery.boards import greenhouse_posting

        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import gh_item
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        url = "https://job-boards.greenhouse.io/synth/jobs/4400094"
        counts, fetched = self._alert(ctx, url, greenhouse_posting("synth", gh_item(4400095, "Claims Analyst", CLAIMS, "synth")))
        self.assertEqual((fetched, counts["lead_other_posting"], counts["lead_ingested"]), (1, 1, 0))
        self.assertEqual(tuple(store.one("SELECT status, job_id FROM leads WHERE url = ?", (url,))), ("new", None))
        self.assertTrue(store.one("SELECT 1 FROM jobs WHERE job_id = 'greenhouse:synth:4400095'"))
        # the control: the posting its link names joins it
        url = "https://job-boards.greenhouse.io/synth/jobs/4400096"
        counts, fetched = self._alert(ctx, url, greenhouse_posting("synth", gh_item(4400096, "Claims Analyst", CLAIMS, "synth")))
        self.assertEqual((fetched, counts["lead_other_posting"], counts["lead_ingested"]), (1, 0, 1))
        self.assertEqual(store.one("SELECT job_id FROM leads WHERE url = ?", (url,))["job_id"], "greenhouse:synth:4400096")

    def test_the_twin_carrying_your_decision_is_the_job_of_record_though_it_is_the_newer(self):
        """Batch 3 condition 4: when only the newer twin carries your label, it is the job of record and the older one
        becomes its duplicate (the oldest is the record only when several carry the same decisions)."""
        from careeros import pipeline

        from tests.test_batch3 import LINK, SYNTH, board_read, jobs
        from tests.test_intake_identity import twin
        store, ctx = setup()
        board_read(ctx, SYNTH)
        pipeline.process(ctx)
        newer = "manual:synthetic-health:twin"
        twin(store, newer, LINK)
        stored_label(store, newer, "apply", None, "l1")   # an older engine's row and its label: no preview before the upgrade
        self.assertEqual(pipeline.rederive_identity(ctx)["twins_joined"], 1)
        rows = jobs(store)
        self.assertEqual((rows[newer]["duplicate_of"], rows[JOB]["duplicate_of"]), (None, newer))

    def test_a_command_refused_at_the_writer_lock_removes_every_handoff_file(self):
        """Batch 3 condition 1 under D191, through the command itself: refused at a lock another command holds, or one
        left behind, a command still removes every handoff file, makes no database and leaves the lock."""
        import contextlib
        import io
        import os

        from careeros import cli
        from tests.test_observability import stale_lock
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            for pid in (os.getpid(), 999999):   # a command running now; a lock left behind
                with self.subTest(pid=pid):
                    lock = stale_lock(vault / "data" / "writer.lock", pid=pid)
                    stray = vault / "handoff" / "ses_stray.json"
                    stray.parent.mkdir(parents=True, exist_ok=True)
                    stray.write_text("{}", encoding="utf-8")
                    out = io.StringIO()
                    try:
                        with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(vault)}), contextlib.redirect_stdout(out):
                            with self.assertRaises(SystemExit) as stop:
                                cli.main(["process"])
                    finally:
                        for handler in list(cli.log.handlers):   # release the temporary log file
                            handler.close()
                            cli.log.removeHandler(handler)
                    self.assertEqual(stop.exception.code, 1)
                    self.assertIn("CareerOS is busy", out.getvalue())
                    self.assertFalse(stray.exists())
                    self.assertTrue(lock.exists())
                    self.assertFalse((vault / "data" / "careeros.db").exists())


if __name__ == "__main__":
    unittest.main()
