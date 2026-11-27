"""The red team's round p1 on the built D190 P1 gate (BATCH_5, on f88715b): each finding's correction, with a test that
fails on f88715b. Synthetic data only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from careeros import pipeline
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery.boards import greenhouse_posting
from careeros.review import packages
from careeros.review.submission import authorize
from tests.helpers import JOB_MATRIX, give_label
from tests.test_packages import FULL_TIME
from tests.test_v26_discovery import MID, POLICY, gh_item

JOB = "greenhouse:synth:1"
EMPLOYER = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}


class FileStore:
    """A store on a database file (the pending-holds file lives beside it), with a board posting read, screened and
    labelled Apply from a preview."""

    def __init__(self, case: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "careeros.db")
        self.store = open_store(db, FixedClock("2026-10-12T12:00:00.000Z"), POLICY.fingerprint)
        case.addCleanup(self.store.close)
        self.ctx = pipeline.Context(self.store, POLICY, MID, JOB_MATRIX, None)
        self.file = Path(db + ".pending-holds.jsonl")
        self.read(FULL_TIME)
        pipeline.process(self.ctx)
        give_label(self.store, POLICY, MID, JOB, "apply")

    def read(self, text, n=1):
        return pipeline.ingest(self.ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(n, "Claims Analyst", text, "synth"))],
                               full_board=False)

    def route(self, job=JOB):
        return packages.route_for(self.store, POLICY, job, MID)


def hold_line(job_id, hold_id="hold:abc", identity="greenhouse|1"):
    return json.dumps({"job_id": job_id, "identity": identity, "message": "ingest failed: disk full",
                       "at": "2026-10-12T12:00:00.000Z", "hold_id": hold_id}, sort_keys=True)


class PendingFileTests(unittest.TestCase):
    """A (High): the pending-holds file fails closed. A file that cannot be read, or a line in it that is not a hold,
    holds every Apply and Stretch label until it is read cleanly; an append never joins an earlier line."""

    def test_an_unreadable_file_holds_every_label_until_it_reads_again(self):
        """A1: an OSError on read (a sharing violation) made the file a placeholder that reached no job."""
        s = FileStore(self)
        self.assertEqual(s.route()[0], "apply")
        s.file.write_text(hold_line("greenhouse:other:9", identity="greenhouse|9") + "\n", encoding="utf-8")   # another job's
        real = Path.read_text

        def locked(path, *a, **kw):
            if str(path).endswith(".pending-holds.jsonl"):
                raise PermissionError(32, "The process cannot access the file")
            return real(path, *a, **kw)
        with mock.patch.object(Path, "read_text", locked):
            self.assertEqual(packages.record_pending_holds(s.store), 0)
            route, why = s.route()
            self.assertIsNone(route)
            self.assertTrue(why.startswith("holds_file_unreadable:"), why)
            self.assertIn(str(s.file), why)
            self.assertTrue(any(r["code"] == "not_approved"
                                for r in authorize(s.store, POLICY, JOB, "direct", profile=MID).reasons))
        self.assertTrue(s.file.exists())   # left as it was
        self.assertEqual(packages.record_pending_holds(s.store), 1)   # read cleanly: recorded
        self.assertEqual(s.route()[0], "apply")   # that hold is another job's

    def test_a_torn_line_never_swallows_the_next_hold_and_holds_every_label_while_it_stands(self):
        """A2: an append cut short left a line with no newline; the next hold was glued onto it and lost."""
        s = FileStore(self)
        s.file.write_text(hold_line(JOB, "hold:torn")[:40], encoding="utf-8")   # cut short: no newline
        kept = packages.add_pending_hold(s.store, JOB, ("greenhouse", "1"), "ingest failed: disk full")
        self.assertEqual([h["hold_id"] for h in packages.pending_holds(s.store)], [kept])
        route, why = s.route()
        self.assertIsNone(route)
        self.assertTrue(why.startswith("holds_file_damaged:"), why)
        self.assertEqual(packages.record_pending_holds(s.store), 1)   # the hold is recorded; the torn line stays
        self.assertEqual(s.file.read_text(encoding="utf-8"), hold_line(JOB, "hold:torn")[:40] + "\n")
        self.assertTrue(s.route()[1].startswith("holds_file_damaged:"))
        s.file.unlink()   # you check the line and delete it
        route, why = s.route()
        self.assertIsNone(route)
        self.assertTrue(why.startswith("posting_not_stored:"), why)   # the recorded hold holds this label

    def test_a_line_that_is_not_a_hold_is_kept_by_every_rewrite(self):
        s = FileStore(self)
        s.file.write_text("not json\n" + hold_line(JOB, "hold:one") + "\n", encoding="utf-8")
        with s.store.tx():
            packages.clear_hold(s.store, JOB, "hold:one", "a board read of the posting was stored")
        self.assertEqual(packages.prune_pending_holds(s.store), 1)
        self.assertEqual(s.file.read_text(encoding="utf-8"), "not json\n")
        self.assertTrue(packages.pending_file_problem(s.store).startswith("holds_file_damaged:"))

    def test_the_review_page_and_status_name_the_file(self):
        import contextlib
        import io

        from careeros import cli
        from careeros.review.report import build_report
        s = FileStore(self)
        s.file.write_text("{\n", encoding="utf-8")
        html = build_report(s.store, MID, POLICY, token="t", form_keys={})
        self.assertIn("Every Apply and Stretch label waits", html)
        self.assertIn("holds_file_damaged", html)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._command_start(s.ctx, {})
        self.assertIn("holds_file_damaged", out.getvalue())


class FailedReadReachTests(unittest.TestCase):
    """B (High): a failed read must keep reaching the labelled job, and a read that lifts a hold must be stored where
    P1 compares it."""

    def setUp(self):
        from tests.test_v26_discovery import setup
        self.store, self.ctx = setup()

    def board_read(self, n=1, text=FULL_TIME, **kw):
        return pipeline.ingest(self.ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(n, "Claims Analyst", text, "synth"))],
                               **kw)

    def hold(self, entity_id, metadata=None):
        return self.store.writer("discovery").exception("data_quality", "medium", "ingest failed: disk full",
                                                        entity_type="job", entity_id=entity_id, error_code="ingest_failed",
                                                        metadata=metadata)

    def test_a_legacy_hold_on_a_job_never_created_reaches_the_posting_by_its_id(self):
        """B1: the deployed engine held the generated id when a new posting's insert failed; the upgrade stamped it
        with no identity, so it reached nothing."""
        import contextlib
        import io

        from careeros import upgrade
        from careeros.discovery.manual_intake import intake
        with contextlib.redirect_stdout(io.StringIO()):
            pasted = intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + FULL_TIME,
                            "https://job-boards.greenhouse.io/synth/jobs/7")["job_id"]
        held = self.hold("greenhouse:synth:7")   # no job has this id: its insert failed
        self.assertEqual(upgrade._stamp_holds(self.store), 1)
        meta = json.loads(self.store.one("SELECT metadata_json FROM exceptions WHERE exception_id = ?", (held,))[0])
        self.assertEqual(meta["posting_identity"], "greenhouse|7")
        self.assertIn(held, [h["hold_id"] for h in packages.open_holds(self.store, pasted)])
        self.assertIsNone(upgrade.identity_of_id("manual:synthetic-health:R-1"))   # a manual id names no link

    def test_a_board_read_goes_to_the_twin_that_carries_your_decisions(self):
        """B2: target_of wrote a read into the oldest twin while rederive_identity made the twin with your label the
        job of record; one rule now serves both."""
        from tests.helpers import stored_label
        from tests.test_intake_identity import twin
        self.board_read()
        twin(self.store, "manual:synthetic-health:twin", "https://job-boards.greenhouse.io/synth/jobs/1")
        stored_label(self.store, "manual:synthetic-health:twin", "apply")
        read = greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME, "synth"))
        self.assertEqual(pipeline.target_of(self.store, EMPLOYER, read)[1], "manual:synthetic-health:twin")
        pipeline.process(self.ctx)   # the join picks the same job
        self.assertEqual(self.store.one("SELECT duplicate_of FROM jobs WHERE job_id = ?", (JOB,))[0],
                         "manual:synthetic-health:twin")
        self.assertEqual(pipeline.target_of(self.store, EMPLOYER, read)[1], "manual:synthetic-health:twin")

    def test_twins_whose_decisions_differ_send_a_read_to_the_oldest(self):
        """B2: twins your decisions keep apart are never joined (their labels wait); a read goes to the oldest, never
        to whichever twin was decided first."""
        from tests.helpers import stored_label
        from tests.test_intake_identity import twin
        self.board_read()
        twin(self.store, "manual:synthetic-health:a", "https://job-boards.greenhouse.io/synth/jobs/1")
        self.store.clock.advance(hours=2)
        twin(self.store, "manual:synthetic-health:b", "https://job-boards.greenhouse.io/synth/jobs/1")
        stored_label(self.store, "manual:synthetic-health:a", "apply")
        stored_label(self.store, "manual:synthetic-health:b", "skip")
        read = greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME, "synth"))
        members = [JOB, "manual:synthetic-health:a", "manual:synthetic-health:b"]
        # one rule (the coordinator's B2 test): job_of_record, target_of and rederive_identity's record_of pick the same job
        self.assertEqual({pipeline.job_of_record(self.store, m, read) for m in members}, {JOB})
        self.assertEqual(pipeline.record_of(self.store, members), JOB)
        self.assertEqual(pipeline.target_of(self.store, EMPLOYER, read)[1], JOB)
        counts = pipeline.process(self.ctx)   # never joined
        self.assertEqual(counts["twins_held"], 1)
        self.assertEqual([r[0] for r in self.store.all("SELECT duplicate_of FROM jobs ORDER BY job_id")], [None] * 3)
        self.assertEqual({pipeline.job_of_record(self.store, m, read) for m in members}, {JOB})
        self.assertEqual(pipeline.target_of(self.store, EMPLOYER, read)[1], JOB)

    def test_a_joined_twin_whose_record_differs_is_a_sighting_of_the_job_of_record(self):
        """B3: the upgrade gave an older engine's twin a source record and the join recorded no sighting, so part (d)
        never showed what that copy says."""
        from careeros import upgrade
        from tests.test_intake_identity import twin
        self.board_read()
        pipeline.process(self.ctx)
        twin(self.store, "manual:synthetic-health:twin", "https://job-boards.greenhouse.io/synth/jobs/1")
        self.store.conn.execute("UPDATE jobs SET description_text = ?, location_raw = 'Denver, CO' WHERE job_id = ?",
                                ("This role is on-site in Denver, CO.", "manual:synthetic-health:twin"))
        upgrade.run(self.ctx) if not upgrade.done(self.store) else upgrade._job(self.ctx, "manual:synthetic-health:twin")
        self.assertIsNotNone(self.store.one("SELECT source_json FROM jobs WHERE job_id = 'manual:synthetic-health:twin'")[0])
        pipeline.process(self.ctx)
        rows = [json.loads(r["facts_json"]) for r in self.store.all("SELECT facts_json FROM observations WHERE job_id = ?",
                                                                   (JOB,))]
        copies = [f for f in rows if f.get("joined_twin") == "manual:synthetic-health:twin"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0]["sighting"])
        self.assertIn("on-site", copies[0]["source"]["description_text"])
        from careeros.review.labels import capture, preview_lines   # the card says what that copy states (C2)
        shown = " ".join(preview_lines(capture(self.store, POLICY, MID, JOB), self.store))
        self.assertIn("a copy of this posting says something else (manual:synthetic-health:twin, another job of this "
                      "posting", shown)
        self.assertIn("on-site in Denver, CO", shown)

    def twin_card(self, *, with_store: bool):
        """The upgrade path (rtw_p1use_3 case A): an older engine's twin row, on-site in Denver at $40,000, rebuilt by
        record_from_columns and joined by rederive_identity to the board job (remote in the US, $80,000-95,000)."""
        from careeros import upgrade
        from careeros.review.labels import capture, preview_lines
        from tests.test_intake_identity import twin
        self.board_read()
        pipeline.process(self.ctx)
        twin(self.store, "manual:synthetic-health:twin", "https://job-boards.greenhouse.io/synth/jobs/1")
        self.store.conn.execute("UPDATE jobs SET description_text = ?, location_raw = 'Denver, CO' WHERE job_id = ?",
                                ("This role is on-site in Denver, CO, five days a week. Pay range: $40,000 - $45,000 per "
                                 "year.",
                                 "manual:synthetic-health:twin"))
        upgrade.run(self.ctx) if not upgrade.done(self.store) else upgrade._job(self.ctx, "manual:synthetic-health:twin")
        pipeline.process(self.ctx)
        captured = capture(self.store, POLICY, MID, JOB)
        return captured, preview_lines(captured, self.store if with_store else None)

    def test_the_card_names_the_twin_and_shows_both_values_of_each_differing_fact(self):
        """BATCH_5 B3 + C (the coordinator on 58126d5): on the upgrade path the card named the record's own id and an
        observation id, and showed none of the twin's values; the card now shows them from the capture itself, on
        every surface (no store needed)."""
        for with_store in (True, False):
            _captured, lines = self.twin_card(with_store=with_store)
            self.setUp()
            shown = "\n".join(lines)
            self.assertIn("a copy of this posting says something else (manual:synthetic-health:twin, another job of this "
                          "posting", shown)
            self.assertNotIn("observation obs:", shown)
            self.assertIn("location: Denver, CO", shown)
            self.assertIn("(this copy) / Remote - US (from: the board) (the job of record)", shown)
            self.assertIn("remote: onsite", shown)
            self.assertIn("pay: minimum 40,000 · maximum 45,000 · per year · USD (this copy) / minimum 80,000 · maximum "
                          "95,000 · per year · USD (the job of record)", shown)

    def test_a_twin_joined_after_the_label_holds_it_by_the_twins_id(self):
        """B3: a label given before an older engine's twin is joined is held by the copy the twin brings, and the reason
        names the twin, not the job of record."""
        from careeros import upgrade
        from tests.test_intake_identity import twin
        self.board_read()
        pipeline.process(self.ctx)
        give_label(self.store, POLICY, MID, JOB, "apply")
        self.assertEqual(packages.route_for(self.store, POLICY, JOB, MID)[0], "apply")
        twin(self.store, "manual:synthetic-health:twin", "https://job-boards.greenhouse.io/synth/jobs/1")
        self.store.conn.execute("UPDATE jobs SET description_text = ?, location_raw = 'Denver, CO' WHERE job_id = ?",
                                ("This role is on-site in Denver, CO.", "manual:synthetic-health:twin"))
        upgrade.run(self.ctx) if not upgrade.done(self.store) else upgrade._job(self.ctx, "manual:synthetic-health:twin")
        pipeline.process(self.ctx)
        route, why = packages.route_for(self.store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertIn("a copy of this posting says something else (manual:synthetic-health:twin)", why)

    def test_the_review_card_shows_the_twin_values_too(self):
        """C: the review page's card renders the same lines (one renderer)."""
        from careeros.review.report import build_report
        captured, _lines = self.twin_card(with_store=True)
        page = build_report(self.store, MID, POLICY, token="t", form_keys={})
        self.assertIn("Denver, CO", page)
        self.assertIn("(this copy) / Remote - US", page)

    def test_a_pasted_copy_is_shown_with_both_values_and_a_later_differing_copy_holds_the_label(self):
        """B3 + C, the paste path: a copy pasted under this engine is shown as read for this job, with both values; a
        label given from that card routes; a later differing copy holds it (label_facts_changed)."""
        import contextlib
        import io

        from careeros.discovery.manual_intake import intake
        from careeros.review.labels import capture, preview_lines
        self.board_read()
        pipeline.process(self.ctx)
        link = "https://job-boards.greenhouse.io/synth/jobs/1"
        with contextlib.redirect_stdout(io.StringIO()):
            intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Denver, CO\n\n" + FULL_TIME.replace(
                "This role is remote in the US.", "This role is on-site in Denver, CO."), link)
        shown = "\n".join(preview_lines(capture(self.store, POLICY, MID, JOB)))
        self.assertIn("a copy of this posting says something else (a copy read for this job; pasted,", shown)
        self.assertIn("location: Denver, CO", shown)
        # of a long text, the sentences the job of record's text lacks, never its first 160 characters; then the whole
        # text, uncut (Codex Pass 15, H37)
        self.assertRegex(shown, r"also: its text, where it differs: [^\n]*This role is on-site in Denver, CO\.")
        differs = shown.split("also: its text, where it differs: ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("About us", differs)
        self.assertIn("this copy's text, in full", shown)
        self.assertIn("    About us\n", shown)
        self.assertIn("(this copy) / Remote - US (from: the board) (the job of record)", shown)
        give_label(self.store, POLICY, MID, JOB, "apply")
        self.assertEqual(packages.route_for(self.store, POLICY, JOB, MID)[0], "apply")
        self.store.clock.advance(minutes=5)
        with contextlib.redirect_stdout(io.StringIO()):
            intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Austin, TX\n\n" + FULL_TIME.replace(
                "This role is remote in the US.", "This role is on-site in Austin, TX."), link + "?gh_src=linkedin")
        route, why = packages.route_for(self.store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertIn("label_facts_changed", why)

    def test_a_legacy_hold_naming_no_posting_reaches_every_job_until_a_job_has_its_id(self):
        """B1's manual case (Z8): a hold on a generated id that names no link, whose job was never created, reached
        nothing. It now reaches every job, says so on every card, holds every label it has not been acknowledged by, and
        narrows to the job with exactly its id once there is one."""
        from collections import Counter

        from careeros import upgrade
        from tests.helpers import add_job
        self.board_read()
        pipeline.process(self.ctx)
        give_label(self.store, POLICY, MID, JOB, "apply")
        self.assertEqual(packages.route_for(self.store, POLICY, JOB, MID)[0], "apply")
        held = self.hold("manual:synthetic-health:r-404")   # a paste whose new job's insert failed
        counts = upgrade.run(self.ctx)
        self.assertEqual((counts["holds_stamped"], counts["holds_reaching_every_job"]), (1, 1))
        reached = {h["hold_id"]: h["message"] for h in packages.open_holds(self.store, JOB)}
        self.assertIn(held, reached)
        self.assertIn("CareerOS cannot tell which posting it was", reached[held])
        self.assertIsNone(packages.route_for(self.store, POLICY, JOB, MID)[0])   # the label given before it is held
        self.assertEqual([r["exception_id"] for r in packages.every_job_holds(self.store)], [held])
        self.store.clock.advance(minutes=1)
        give_label(self.store, POLICY, MID, JOB, "apply")   # from a preview that shows it: acknowledged (H25)
        self.assertEqual(packages.route_for(self.store, POLICY, JOB, MID)[0], "apply")
        self.assertEqual(upgrade._stamp_holds(self.store, Counter()), 0)   # stamped once
        add_job(self.store, "manual:synthetic-health:r-404", ats="manual", external_id="r-404")
        self.assertNotIn(held, [h["hold_id"] for h in packages.open_holds(self.store, JOB)])
        self.assertEqual([(h["hold_id"], h["message"]) for h in packages.open_holds(self.store, "manual:synthetic-health:r-404")],
                         [(held, "ingest failed: disk full")])
        self.assertEqual(packages.every_job_holds(self.store), [])

    def test_the_upgrade_report_and_status_list_a_hold_that_reaches_every_job(self):
        """Z8: listed in the upgrade report (command start) and in careeros status."""
        import contextlib
        import io
        from types import SimpleNamespace

        from careeros import cli
        from careeros.paths import vault_paths
        self.hold("manual:synthetic-health:r-404")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = vault_paths(Path(tmp.name))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._command_start(self.ctx, paths)
            cli.cmd_status(self.ctx, paths, SimpleNamespace())
        said = out.getvalue()
        self.assertIn("1 failed read(s) an older engine recorded name no posting", said)
        self.assertIn("holds that reach every job: 1", said)

    def test_a_read_that_stores_nothing_lifts_no_hold(self):
        """B4: a partial scan's read of a closed job wrote only last_seen_at, yet lifted every hold on its posting."""
        self.board_read()
        pipeline.process(self.ctx)
        disc = self.store.writer("discovery")
        with self.store.tx():
            disc.update("jobs", {"closed_at": self.store.clock.now()}, "job_id = ?", (JOB,))
            disc.transition(self.ctx.matrix, JOB, "closed", reason_code="posting_removed")
        held = self.hold(JOB, {"posting_identity": "greenhouse|1", "rule": packages.HOLD_RULE})
        counts = self.board_read(text=FULL_TIME + "\nNow on-site.", full_board=False)
        self.assertEqual(counts["inactive_seen"], 1)
        self.assertEqual(self.store.one("SELECT status FROM exceptions WHERE exception_id = ?", (held,))[0], "open")
        self.board_read(text=FULL_TIME + "\nNow on-site.", full_board=True)   # a complete scan reopens and stores it
        self.assertEqual(self.store.one("SELECT status FROM exceptions WHERE exception_id = ?", (held,))[0], "resolved")


class LabelSurfaceTests(unittest.TestCase):
    """C (High): every surface that offers a label renders the capture its digest binds, every value of it (one
    renderer: labels.approves), and a surface that cannot (the shortlist card) offers no label."""

    def setUp(self):
        from tests.test_p1 import screened
        self.store, self.ctx = screened(FULL_TIME)

    def rich(self):
        """A real capture, with every bound value set to something a card could get wrong."""
        from careeros.review.labels import capture
        captured = capture(self.store, POLICY, MID, JOB)
        from careeros.pipeline import source_json
        parts = json.loads(json.dumps(captured["parts"]))
        record = json.loads(parts["source"])
        record["apply_url"] = record["url"] + "/apply-here"   # a destination other than the posting link
        parts["source"] = source_json(record)
        parts["derived"].update(comp_min=7000.0, comp_max=8000.0, comp_period="month", comp_currency="CAD",
                                remote_type="remote", remote_states_json='["FL", "TX"]',
                                remote_excluded_states_json='["NV"]', deadline_at="2026-10-01",
                                remote_scope="us_states", timezone_requirement="Pacific", employment_type="contract")
        parts["screening"]["flags"] = [{"code": f"c{n}", "detail": f"flag number {n} of eleven"} for n in range(11)]
        holds = [{"hold_id": "hold:x", "at": "2026-10-12T12:00:00.000Z", "kept": "row",
                  "message": "ingest failed: disk full"}]
        return {**captured, "parts": parts, "holds_shown": holds}

    def expected(self, captured):
        parts = captured["parts"]
        record = json.loads(parts["source"])
        d = parts["derived"]
        values = ["7,000", "8,000", "per month", "CAD", d["remote_type"], "FL, TX", "NV", d["deadline_at"],
                  d["remote_scope"], d["timezone_requirement"], d["employment_type"], record["title"], record["location"],
                  record["url"], record["apply_url"], (record.get("employer") or {})["name"], "(from: the board)",
                  "ingest failed: disk full"]
        return values + [f["detail"] for f in parts["screening"]["flags"]]

    def test_every_bound_value_is_on_every_label_surface(self):
        """C1, C2, C4: the CLI preview and the review page's card render the same capture, every value of it."""
        import html
        import re

        from careeros.review import labels
        from careeros.review.labels import preview_lines
        from careeros.review.report import build_report
        captured = self.rich()
        cli_text = "\n".join(preview_lines(captured, self.store, self.store.clock.now()))
        with mock.patch.object(labels, "capture", return_value=captured):
            page = build_report(self.store, MID, POLICY, token="t", form_keys={})
        start = page.index(f'id="{JOB}"')
        card = html.unescape(re.sub(r"<[^>]+>", " ", page[start:page.index("</form>", page.index("approves", start))]))
        for value in self.expected(captured):
            with self.subTest(value=value):
                self.assertIn(value, cli_text)
                self.assertIn(value, card)
        self.assertIn("(already past)", cli_text)   # the deadline before today

    def test_the_card_lists_every_screening_flag(self):
        """C4: the card listed only the first eight of the gate's flags."""
        from careeros.review.report import build_report
        latest = dict(self.store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC", (JOB,)))
        flags = [{"code": f"c{n}", "detail": f"flag number {n} of eleven"} for n in range(11)]
        self.store.clock.advance(seconds=1)
        self.store.writer("qualify").insert("gate_results", {**latest, "result_id": "gate:eleven", "at": self.store.clock.now(),
                                                             "flags_json": json.dumps(flags)})
        page = build_report(self.store, MID, POLICY, token="t", form_keys={})
        for f in flags:
            self.assertIn(f'<li class="flag">{f["detail"]}</li>', page)

    def test_pay_is_never_a_range_made_from_one_end(self):
        """C1: "up to X" was shown as "$X–$X", a CAD or monthly range as "$…/yr", and the CLI said "pay not stated"."""
        from careeros.review.labels import pay_line
        from careeros.review.report import _comp
        only_max = {"comp_min": None, "comp_max": 120000.0, "comp_period": "year", "comp_currency": "USD"}
        self.assertEqual(pay_line(only_max), "pay: no minimum stated · maximum 120,000 · per year · USD")
        self.assertEqual(_comp(only_max), "USD up to 120,000 per year")
        self.assertEqual(_comp({"comp_min": 7000.0, "comp_max": 8000.0, "comp_period": "month", "comp_currency": "CAD"}),
                         "CAD 7,000–8,000 per month")
        self.assertEqual(_comp({"comp_min": 2400.0, "comp_max": None, "comp_period": None, "comp_currency": None}),
                         "currency not stated from 2,400 period not stated")

    def test_the_shortlist_card_offers_no_label_and_its_job_card_is_always_on_the_page(self):
        """C3: the shortlist card offered Apply with no posting facts, and the job's own card could be cut by a cap."""
        from careeros.review.report import build_report
        give_label(self.store, POLICY, MID, JOB, "apply")
        with mock.patch("careeros.review.report._grouped", return_value=[]):   # every section's cards cut
            page = build_report(self.store, MID, POLICY, token="t", form_keys={})
        start = page.index(f'id="sl-{JOB}"')
        shortlist_card = page[start:page.index("<h2", start)]
        self.assertNotIn('name="label"', shortlist_card)
        self.assertIn(f'href="#{JOB}"', shortlist_card)
        self.assertIn("Shortlisted jobs not shown above", page)
        self.assertIn(f'id="{JOB}"', page)


def ld_page(salary=None, employment="FULL_TIME", location=True, identifier="R-20417"):
    """A saved posting page with its JSON-LD JobPosting, as a browser saves one."""
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Claims Analyst",
               "hiringOrganization": {"@type": "Organization", "name": "Foo Health"},
               "url": "https://foohealth.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-20417",
               "identifier": {"@type": "PropertyValue", "value": identifier}, "datePosted": "2026-10-10",
               "description": "<p>" + FULL_TIME.replace("\n", "<br>") + "</p>", "employmentType": employment}
    if location:
        posting["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Austin",
                                                                 "addressRegion": "TX", "addressCountry": "US"}}
    if salary is not None:
        posting["baseSalary"] = salary
    return f'<html><head><script type="application/ld+json">{json.dumps(posting)}</script></head><body></body></html>'


def pay(unit=None, currency="USD", **value):
    return {"@type": "MonetaryAmount", **({"currency": currency} if currency else {}),
            "value": {"@type": "QuantitativeValue", **value, **({"unitText": unit} if unit else {})}}


class ReaderMarkTests(unittest.TestCase):
    """D1 (High): a value a reader fills in, infers or cuts is marked as such, never recorded as stated, so a later read
    that states it differently is a change (the D190 design, section 3, "Absent versus stated")."""

    def setUp(self):
        from tests.test_v26_discovery import setup
        self.store, self.ctx = setup()

    def saved(self, markup):
        from careeros.discovery.saved import parse_saved_html
        from careeros.pipeline import employer_entry
        raw, employer = parse_saved_html(markup, "page.html")
        return pipeline.ingest(self.ctx, employer_entry(self.ctx, name=employer), [raw], source="manual_url",
                               full_board=False), raw

    def test_the_saved_page_pay_reader_reads_every_unit(self):
        from careeros.discovery.saved import _salary
        for unit, period in (("HOUR", "hour"), ("DAY", "day"), ("WEEK", "week"), ("MONTH", "month"), ("YEAR", "year")):
            with self.subTest(unit=unit):
                comp = _salary({"baseSalary": pay(unit, minValue=10, maxValue=20)})
                self.assertEqual((comp["period"], comp.get("inferred")), (period, None))

    def test_each_form_of_a_unit_is_read_as_its_period(self):
        """The D1 residual (the coordinator on 58126d5): any unitText but the five bare words was read as no unit."""
        from careeros.discovery.saved import _salary
        forms = {"year": ("ANNUAL", "YEARLY", "PER_YEAR", "per annum", "Annually"),
                 "hour": ("HOURLY", "PER_HOUR", "Hourly", "hr"), "month": ("MONTHLY", "PER_MONTH"),
                 "week": ("WEEKLY", "PER WEEK"), "day": ("DAILY", "PER_DAY"),
                 "biweekly": ("BIWEEKLY", "BI-WEEKLY", "FORTNIGHTLY"), "semimonthly": ("SEMIMONTHLY", "SEMI_MONTHLY")}
        for period, units in forms.items():
            for unit in units:
                with self.subTest(unit=unit):
                    comp = _salary({"baseSalary": pay(unit, minValue=40, maxValue=50)})
                    self.assertEqual((comp["period"], comp.get("inferred"), comp.get("period_stated")), (period, None, None))

    def test_a_unit_not_understood_is_kept_as_stated_and_differs_from_none(self):
        from careeros.discovery.saved import _salary
        none = _salary({"baseSalary": pay(None, minValue=40, maxValue=50)})
        quarter = _salary({"baseSalary": pay("QUARTER", minValue=40, maxValue=50)})
        semester = _salary({"baseSalary": pay("SEMESTER", minValue=40, maxValue=50)})
        self.assertEqual((quarter["period"], quarter["period_stated"], quarter.get("inferred")), ("year", "QUARTER", None))
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in (none, quarter, semester)}), 3)

    def test_a_resave_that_changes_the_unit_is_a_change(self):
        """A page saved as HOURLY and saved again as MONTHLY, or with a unit CareerOS does not understand, is a change."""
        _, first = self.saved(ld_page(pay("HOURLY", minValue=40, maxValue=50)))
        self.assertEqual(first.comp["period"], "hour")
        for unit in ("MONTHLY", "QUARTER", None):
            with self.subTest(unit=unit):
                again, _ = self.saved(ld_page(pay(unit, minValue=40, maxValue=50)))
                self.assertEqual(again["unchanged"], 0)

    def test_the_card_says_what_unit_was_stated_and_how_it_was_read(self):
        from careeros.review.labels import pay_line
        derived = {"comp_min": 40.0, "comp_max": 50.0, "comp_period": "year", "comp_currency": "USD"}
        line = pay_line(derived, {"comp": {"min": 40.0, "max": 50.0, "period": "year", "currency": "USD",
                                           "period_stated": "QUARTER"}})
        self.assertIn('period stated "QUARTER", not understood; read as per year', line)
        self.assertNotIn("not stated", line)

    def test_a_monthly_page_and_a_yearly_page_are_two_reads(self):
        """D1 as found: 7000-8000 USD per MONTH was read as a year; a later save saying per YEAR was unchanged."""
        first, raw = self.saved(ld_page(pay("MONTH", minValue=7000, maxValue=8000)))
        self.assertEqual((first["new"], raw.comp["period"]), (1, "month"))
        again, _ = self.saved(ld_page(pay("YEAR", minValue=7000, maxValue=8000)))
        self.assertEqual(again["unchanged"], 0)
        self.assertEqual(again["changed"] + again["edited_cosmetic"] + again["pay_reread"], 1)

    def test_what_the_pay_reader_fills_in_is_marked_and_a_page_that_states_it_is_a_change(self):
        _, raw = self.saved(ld_page(pay(None, currency=None, minValue=60000, maxValue=70000)))
        self.assertEqual(sorted(raw.comp["inferred"]), ["currency", "period"])
        self.assertEqual((raw.comp["currency"], raw.comp["period"]), ("USD", "year"))   # for screening, as before
        job = self.store.one("SELECT job_id, source_json FROM jobs")
        self.assertEqual(sorted(json.loads(job["source_json"])["comp"]["inferred"]), ["currency", "period"])
        stated, _ = self.saved(ld_page(pay("YEAR", currency="CAD", minValue=60000, maxValue=70000)))
        self.assertEqual(stated["unchanged"], 0)
        from careeros.review.labels import pay_line
        self.assertIn("filled in by CareerOS, not stated by the posting: period, currency",
                      pay_line({"comp_min": 1.0, "comp_max": 2.0, "comp_period": "year", "comp_currency": "USD"},
                               json.loads(job["source_json"])))

    def test_one_amount_is_marked_as_both_ends(self):
        from careeros.discovery.saved import _salary
        comp = _salary({"baseSalary": pay("YEAR", value=120000)})
        self.assertEqual((comp["min"], comp["max"]), (120000.0, 120000.0))
        self.assertEqual(comp["inferred"], ["minimum and maximum (one amount stated)"])

    def test_a_page_with_no_location_records_none_as_its_source(self):
        from careeros.discovery.saved import parse_saved_html, parse_saved_text
        raw, _ = parse_saved_html(ld_page(location=False), "page.html")
        self.assertEqual(raw.raw["location_source"], "none")
        raw, _ = parse_saved_html(ld_page(), "page.html")
        self.assertEqual(raw.raw["location_source"], "file")
        text, _ = parse_saved_text("Title: Claims Analyst\nEmployer: Foo Health\n\n" + FULL_TIME, "posting.txt")
        self.assertEqual(text.raw["location_source"], "none")

    def test_a_list_of_employment_types_is_kept_whole(self):
        from careeros.discovery.saved import parse_saved_html
        raw, _ = parse_saved_html(ld_page(employment=["FULL_TIME", "CONTRACTOR"]), "page.html")
        self.assertEqual(raw.employment_type_hint, "FULL_TIME, CONTRACTOR")

    def test_what_a_paste_infers_or_cuts_is_marked_and_a_paste_that_states_it_is_a_change(self):
        """The company from a Workday link's host, a title read by heuristic, and a value cut to length."""
        import contextlib
        import io

        from careeros.discovery.manual_intake import intake, parse_pasted
        link = "https://foohealth.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-20417"
        parsed = parse_pasted("Claims Analyst\n\n" + FULL_TIME, link)
        self.assertEqual((parsed["company"], parsed["inferred"]), ("foohealth", ["company", "title"]))
        self.assertEqual(parse_pasted("Job Title: " + "T" * 210 + "\nCompany: Foo Health\n\n" + FULL_TIME)["cut"],
                         ["title"])
        with contextlib.redirect_stdout(io.StringIO()):
            job = intake(self.ctx, "Claims Analyst\n\n" + FULL_TIME, link)["job_id"]
        record = json.loads(self.store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual(record["inferred"], ["company", "title"])
        from careeros.review.labels import capture, preview_lines   # the card names them
        pipeline.process(self.ctx)
        shown = " ".join(preview_lines(capture(self.store, POLICY, MID, job), self.store))
        self.assertIn("read by CareerOS, not stated as such by the posting: company, title", shown)
        stated = parse_pasted("Job Title: Claims Analyst\nCompany: foohealth\n\n" + FULL_TIME, link)
        self.assertEqual(stated["inferred"], [])   # the same words, stated: no mark, so the record differs
        # and so does the comparison (Codex Pass 18, D203): the same words stated as your arguments, over the same text,
        # make a copy whose sighting differs from the record, though its only difference is the mark it drops
        from careeros.review.labels import sighting_differs
        with contextlib.redirect_stdout(io.StringIO()):
            again = intake(self.ctx, "Claims Analyst\n\n" + FULL_TIME, link, title="Claims Analyst", company="foohealth")
        self.assertEqual((again["status"], again["job_id"]), ("already_tracked", job))
        record = json.loads(self.store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        facts = json.loads(self.store.one("SELECT facts_json FROM observations WHERE job_id = ? AND json_extract(facts_json, "
                                          "'$.sighting') = 1 ORDER BY rowid DESC LIMIT 1", (job,))[0])
        self.assertEqual(facts["source"], {k: v for k, v in record.items() if k != "inferred"})
        self.assertTrue(sighting_differs(record, facts))


class RebuiltLocationTests(unittest.TestCase):
    """D2 (Medium): a rebuilt record whose location provenance is unknown places nothing, and a copy that states no
    location keeps the stored provenance."""

    def test_a_rebuilt_location_with_no_record_of_where_it_came_from_places_nothing(self):
        from careeros import upgrade
        from careeros.pipeline import location_places, posting_from_record
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        from tests.helpers import add_job
        add_job(store, "manual:foo-health:1", ats="manual", external_id="1", title="Claims Analyst",
                employer_name="Foo Health", location_raw="Remote - US",
                description_text="Previously advertised as Remote - US. " + FULL_TIME, current_state="normalized")
        store.conn.execute("UPDATE jobs SET source_json = NULL")
        upgrade._job(ctx, "manual:foo-health:1")
        record = json.loads(store.one("SELECT source_json FROM jobs")[0])
        self.assertEqual(record["location_source"], "unknown")
        self.assertFalse(location_places(posting_from_record(record)))

    def rebuilt_source(self, description, observation):
        from careeros import upgrade
        from careeros.openings import record_observation
        from tests.helpers import add_job
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        add_job(store, "manual:foo-health:2", ats="manual", external_id="2", title="Claims Analyst",
                employer_name="Foo Health", location_raw="Remote - US", description_text=description,
                current_state="normalized")
        record_observation(store, job_id="manual:foo-health:2", url=None, location="Remote - US", **observation)
        store.conn.execute("UPDATE jobs SET source_json = NULL")
        upgrade._job(ctx, "manual:foo-health:2")
        return json.loads(store.one("SELECT source_json FROM jobs")[0])["location_source"]

    def test_a_rebuilt_re_paste_confirmation_places_nothing(self):
        """A write recorded as your argument that is not yours (a D162 re-paste's confirmation) is unknown when rebuilt."""
        self.assertEqual(self.rebuilt_source(FULL_TIME, {"source_kind": "human_text", "evidence_level": "human",
                                                         "facts": {"location_write": "argument"}}), "unknown")

    def test_a_rebuilt_older_paste_of_its_own_location_line_is_that_label(self):
        """An older paste with no location write whose own Location line the paste recorded whole: declared, a label."""
        self.assertEqual(self.rebuilt_source("Location: Remote - US" + chr(10) + FULL_TIME,
                                             {"source_kind": "human_text", "evidence_level": "human"}), "label")

    def test_a_copy_that_states_no_location_keeps_where_the_stored_one_came_from(self):
        from careeros.pipeline import _keep_your_location
        from careeros.normalize.posting import RawPosting
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        import contextlib
        import io

        from careeros.discovery.manual_intake import intake
        link = "https://foohealth.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-20417"
        with contextlib.redirect_stdout(io.StringIO()):
            job = intake(self.ctx_of(store, ctx), "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n" + FULL_TIME,
                         link)["job_id"]
        copy = RawPosting(ats="manual", board_token="foo-health", external_id="x", title="Claims Analyst", location="",
                          url=link, description_text=FULL_TIME, raw={"location_source": "none"})
        kept = _keep_your_location(store, job, copy)
        self.assertEqual((kept.location, kept.raw["location_source"]), ("Remote - US", "label"))

    @staticmethod
    def ctx_of(store, ctx):
        return ctx


BOARD_LINK = "https://boards.greenhouse.io/synth/jobs/555"
OTHER_LINK = "https://www.linkedin.com/comm/jobs/view/4011111111/"


class GmailReader:
    """A Gmail reader with set answers, for one account (a message it does not hold is answered 404)."""

    def __init__(self, listed, raws):
        self.listed, self.raws = list(listed), dict(raws)

    def address(self):
        return "candidate@example.com"

    def search(self, query, limit=300):
        return list(self.listed)

    def raw(self, message_id):
        from careeros.discovery.gmail import GmailError
        if message_id not in self.raws:
            raise GmailError("http_404", "Gmail API error (HTTP 404)")
        return self.raws[message_id]

    def close(self):
        pass


class AlertLineTests(unittest.TestCase):
    """E (Medium): one alert line's board fault is that line's failure (E1), and a line whose fetch failed for a moment
    keeps a lead with its card's link and title until it is read (E2)."""

    def setUp(self):
        import httpx

        from careeros.discovery.http import HttpClient
        from careeros.qualify.profile import load_profile
        from tests.test_alerts import POLICY as ALERT_POLICY
        from tests.test_saved import FIXTURES
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), ALERT_POLICY.fingerprint)
        self.ctx = pipeline.Context(self.store, ALERT_POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX,
                                    Path(self.tmp.name) / "snap")
        self.answer = "ok"   # how the board answers posting 555: ok, 503, redirect_loop or odd_shape

        def handler(request):
            from tests import postings
            from tests.test_pipeline import gh_job
            if request.url.path != "/v1/boards/synth/jobs/555":
                return httpx.Response(404)
            if self.answer == "503":
                return httpx.Response(503)
            if self.answer == "redirect_loop":
                return httpx.Response(302, headers={"location": str(request.url)})
            if self.answer == "odd_shape":
                return httpx.Response(200, json="not a posting")
            return httpx.Response(200, json=gh_job(555, "Pharmacy Benefits Systems Analyst", postings.GOOD_FIT, req="R-55"))

        self.client = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
        self.addCleanup(self.client.close)

    @staticmethod
    def email():
        from tests.test_alerts import alert_email
        dot = chr(0xB7)
        return alert_email("<html><body><table>"
                           f'<tr><td><a href="{BOARD_LINK}">Pharmacy Benefits Systems Analyst</a> Synthetic Health {dot} '
                           "Remote</td></tr>"
                           f'<tr><td><a href="{OTHER_LINK}">Claims Business Analyst</a> Other Payer Co {dot} Remote</td></tr>'
                           "</table></body></html>")

    def run_import(self, name="alert.eml"):
        import contextlib
        import io

        from careeros.cli import _import_alert
        path = self.inbox / name
        path.write_bytes(self.email())
        with contextlib.redirect_stdout(io.StringIO()):
            return _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")

    def pull(self, reader):
        import contextlib
        import io

        from careeros.cli import _gmail_pull
        with contextlib.redirect_stdout(io.StringIO()):
            return _gmail_pull(self.ctx, self.client, "newer_than:3d", gmail=reader)

    def board_lead(self):
        return self.store.one("SELECT url, title, status, job_id, fetch_failure FROM leads WHERE url = ?", (BOARD_LINK,))

    def other_lead(self):
        return self.store.one("SELECT 1 FROM leads WHERE url LIKE '%linkedin%'")

    def filed(self, folder):
        return list((self.inbox / folder).rglob("*.eml")) if (self.inbox / folder).exists() else []

    def test_a_redirect_loop_in_the_fetch_only_step_is_that_lines_definite_failure(self):
        """E1: httpx.TooManyRedirects escaped the import; the .eml stayed .importing and the other card was lost."""
        self.answer = "redirect_loop"
        counts = self.run_import()
        self.assertEqual((counts["lead_fetch_failed"], counts["lead_fetch_retry"]), (1, 0))
        lead = self.board_lead()
        self.assertEqual((lead["status"], lead["job_id"]), ("new", None))
        self.assertIn("its own link: unreadable_answer:TooManyRedirects", lead["fetch_failure"])
        self.assertTrue(self.other_lead())   # the rest of the email is applied
        self.assertTrue(self.filed("imported"))
        self.assertFalse(list(self.inbox.glob("*.importing")))

    def test_an_answer_of_an_unexpected_shape_is_a_definite_failure_and_the_gmail_pull_goes_on(self):
        """E1: a parser error (AttributeError) on an odd answer escaped the Gmail pull and the daily run's Gmail step."""
        self.answer = "odd_shape"
        counts = self.pull(GmailReader(["m1"], {"m1": self.email()}))
        self.assertEqual(counts["lead_fetch_failed"], 1)
        self.assertIn("unreadable_answer:", self.board_lead()["fetch_failure"])
        self.assertTrue(self.other_lead())
        self.assertTrue(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm1'"))   # settled: its receipt

    def test_a_transient_kind_raised_in_the_fetch_only_step_is_read_again(self):
        """E1: a timeout or connection error that reaches the step (not the client's FetchError) is a retry (Z2)."""
        import httpx
        with mock.patch("careeros.cli.fetch_single", side_effect=httpx.ConnectError("refused")):
            counts = self.run_import()
        self.assertEqual(counts["lead_fetch_retry"], 1)
        self.assertTrue(self.board_lead()["fetch_failure"].startswith("its own link: fetch failed for a moment"))
        self.assertTrue(self.other_lead())
        self.assertTrue(self.filed("failed"))   # unsettled: imported again to read the line

    def test_each_kind_of_line_failure(self):
        import httpx

        from careeros.cli import _line_failure
        request = httpx.Request("GET", "https://boards-api.greenhouse.io/v1/boards/synth/jobs/555")

        def status(code):
            return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))
        kinds = {"timeout": httpx.ReadTimeout("t"), "network": httpx.ConnectError("c"), "protocol": httpx.RemoteProtocolError("p"),
                 "5xx": status(502), "429": status(429), "4xx": status(410), "redirects": httpx.TooManyRedirects("r"),
                 "decoding": httpx.DecodingError("d"), "parser": KeyError("id")}
        self.assertEqual({k: _line_failure(e).retryable for k, e in kinds.items()},
                         {"timeout": True, "network": True, "protocol": True, "5xx": True, "429": True, "4xx": False,
                          "redirects": False, "decoding": False, "parser": False})

    def test_a_transient_line_keeps_its_lead_when_its_email_is_not_served_again(self):
        """E2: under Z2 the line's only record was its email's retry; a 404 by id closed it and the card was lost."""
        self.answer = "503"
        self.pull(GmailReader(["m1"], {"m1": self.email()}))
        lead = self.board_lead()
        self.assertEqual((lead["url"], lead["title"], lead["status"], lead["job_id"]),
                         (BOARD_LINK, "Pharmacy Benefits Systems Analyst", "new", None))
        self.assertTrue(lead["fetch_failure"].startswith("its own link: fetch failed for a moment (http_503"))
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'alert_line_unstored' AND status = 'open'"))
        self.store.clock.advance(days=1)
        self.answer = "ok"
        counts = self.pull(GmailReader([], {}))   # m1 read again by its id: no longer in Gmail
        self.assertEqual(counts["gmail_held_gone"], 1)
        self.assertFalse(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'alert_line_unstored' "
                                        "AND status != 'resolved'"))
        self.assertEqual(self.board_lead()["status"], "new")   # closing the retry never removes the card's only trace

    def test_a_kept_lead_says_it_will_not_be_retried_once_its_email_is_gone(self):
        """E2 wording (the coordinator on 58126d5): after the email's retry closed, the lead kept "will retry" for good."""
        self.answer = "503"
        self.pull(GmailReader(["m1"], {"m1": self.email()}))
        self.assertTrue(self.board_lead()["fetch_failure"].endswith("; will retry"))
        counts = self.pull(GmailReader([], {}))   # m1 read again by its id: no longer in Gmail
        self.assertEqual((counts["gmail_held_gone"], counts["leads_no_longer_retried"]), (1, 1))
        lead = self.board_lead()
        self.assertEqual(lead["status"], "new")
        self.assertTrue(lead["fetch_failure"].startswith("its own link: fetch failed for a moment (http_503"))
        self.assertTrue(lead["fetch_failure"].endswith("; will not be retried: the email is no longer read; open its link"))

    def test_an_open_retry_record_gathers_every_pass_kept_leads(self):
        """E2 wording: a later pass's kept leads join the open record, so its closing reaches them all."""
        from careeros.cli import _hold_unstored
        _hold_unstored(self.ctx, "m9", ["lead:a"])
        _hold_unstored(self.ctx, "m9", ["lead:b"])
        rows = self.store.all("SELECT metadata_json FROM exceptions WHERE error_code = 'alert_line_unstored'")
        self.assertEqual([json.loads(r["metadata_json"])["retry_leads"] for r in rows], [["lead:a", "lead:b"]])

    def test_a_lead_kept_for_a_moment_is_read_again_never_seen_before(self):
        """E2: the kept lead is the line's retry: the next reading applies it and updates the same lead."""
        self.answer = "503"
        self.run_import("first.eml")
        self.answer = "ok"
        counts = self.run_import("second.eml")
        self.assertEqual((counts["lead_seen_before"], counts["lead_ingested"]), (1, 1))   # the other card: seen before
        self.assertEqual([tuple(r) for r in self.store.all("SELECT status, job_id, fetch_failure FROM leads WHERE url = ?",
                                                           (BOARD_LINK,))],
                         [("ingested", "greenhouse:synth:555", None)])

    def test_a_second_failure_for_a_moment_updates_the_same_lead(self):
        """E2: the kept lead is marked again, never written twice (the line is not an alert_line_failed problem)."""
        self.answer = "503"
        self.run_import("first.eml")
        self.store.clock.advance(days=1)
        counts = self.run_import("second.eml")
        self.assertEqual((counts["lead_fetch_retry"], counts["lead_errors"]), (1, 0))
        self.assertIn("2026-10-12", self.board_lead()["fetch_failure"])

    def test_a_card_outside_the_families_leaves_a_kept_lead_to_its_retry(self):
        """E2 (the fuzzer's seed 100081 on 51a8406): another email's card for the same link, under a title in no target
        family, read the kept lead again without fetching it and cleared its mark; the first email's retry then found it
        "seen before", so the posting was never read."""
        import contextlib
        import io

        from careeros.cli import _import_alert
        from tests.test_alerts import alert_email
        self.answer = "503"
        self.run_import("first.eml")
        other = self.inbox / "other.eml"
        other.write_bytes(alert_email(f'<html><body><p><a href="{BOARD_LINK}">Office Coordinator</a> Synthetic Health '
                                      f"{chr(0xB7)} Remote</p></body></html>"))
        with contextlib.redirect_stdout(io.StringIO()):
            counts = _import_alert(self.ctx, other, self.client, self.inbox, "2026-10-11")
        self.assertEqual(counts["lead_seen_before"], 1)
        self.assertTrue(self.board_lead()["fetch_failure"].startswith("its own link: fetch failed for a moment"))
        self.answer = "ok"
        self.run_import("first.eml")   # the first email, kept in failed, imported again
        lead = self.board_lead()
        self.assertEqual((lead["status"], lead["job_id"], lead["fetch_failure"]), ("ingested", "greenhouse:synth:555", None))

    def test_a_line_not_fetched_ahead_keeps_a_lead_to_read_again(self):
        """E2: a line the fetch-only step did not fetch (its planning pass failed) keeps its card's lead meanwhile."""
        from careeros import cli
        with mock.patch.object(cli, "_prefetch_alert", return_value={"fetched": {}, "ahead": {}}):
            counts = self.run_import()
        self.assertEqual(counts["lead_fetch_retry"], 1)
        self.assertIn("fetch failed for a moment (not fetched yet", self.board_lead()["fetch_failure"])
        self.assertTrue(self.filed("failed"))

    def test_a_lead_read_but_not_stored_keeps_its_own_mark_through_a_failure_for_a_moment(self):
        """E2 with H25: a lead read but not stored stays pending under its own mark when a retry fails for a moment."""
        from careeros.discovery.alerts import lead_id
        from careeros.normalize.posting import norm
        mark = "its own link: read but not stored (2026-10-10)"
        self.store.writer("discovery").insert("leads", {
            "lead_id": lead_id(BOARD_LINK), "url": BOARD_LINK, "site": "greenhouse",
            "title": "Pharmacy Benefits Systems Analyst", "title_norm": norm("Pharmacy Benefits Systems Analyst"),
            "family": None, "context": None, "sender": None, "subject": None, "received_at": None,
            "first_seen_at": "2026-10-10T12:00:00.000Z", "status": "new", "job_id": None, "profile": "primary",
            "fetch_failure": mark})
        self.answer = "503"
        counts = self.run_import()
        self.assertEqual(counts["lead_fetch_retry"], 1)
        self.assertEqual(self.board_lead()["fetch_failure"], mark)

    def test_the_retry_record_says_where_each_kind_of_line_is(self):
        """E2: "The lead is on the review page meanwhile" was false for a line with no lead."""
        from careeros.cli import _hold_unstored
        _hold_unstored(self.ctx, "m1")
        message = self.store.one("SELECT message FROM exceptions WHERE error_code = 'alert_line_unstored'")["message"]
        self.assertNotIn("The lead is on the review page meanwhile.", message)
        self.assertIn("not read for a moment, is a lead on the review page", message)
        self.assertIn("alert_line_failed", message)


class UpgradeRobustnessTests(unittest.TestCase):
    """G (Medium): the upgrade's transitions never refuse a command when the clock is behind (G1), and W1 is decided by
    the posting's boards, at every processing run (G2)."""

    LINK = "https://job-boards.greenhouse.io/synth/jobs/555"

    @staticmethod
    def quiet(fn, *a, **kw):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **kw)

    def paste(self, store, ctx, link="https://careers.example.com/claims", text=None):
        from careeros.discovery.manual_intake import intake
        from tests.test_v26_discovery import CLAIMS
        return self.quiet(intake, ctx, text or CLAIMS + "\n\nThis is a full-time position.", link,
                          company="Synthetic Health", title="Claims Analyst")["job_id"]

    @staticmethod
    def as_deployed(store, job):
        store.conn.execute("UPDATE jobs SET source_json = NULL WHERE job_id = ?", (job,))   # stored by an older engine

    def test_a_clock_behind_a_jobs_last_state_change_leaves_that_job_and_refuses_nothing(self):
        """G1: the upgrade's transition raised out_of_order_event out of every writing command's start."""
        from careeros import upgrade
        from careeros.review.labels import capture
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        ahead = self.paste(store, ctx)
        self.quiet(pipeline.process, ctx)   # screened while the clock ran ahead
        other = self.paste(store, ctx, link="https://careers.example.com/claims-2")   # not screened: no transition
        for job in (ahead, other):
            self.as_deployed(store, job)
        store.clock.advance(hours=-2)   # the clock corrected
        counts = upgrade.run(ctx)
        self.assertEqual((counts[upgrade.CLOCK_BEHIND], counts["records_rebuilt"]), (1, 1))
        self.assertFalse(upgrade.done(store))   # the marker waits for every job
        self.assertIsNone(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (ahead,))[0])
        self.assertIsNotNone(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (other,))[0])
        self.assertIn("waiting for the upgrade", capture(store, POLICY, MID, ahead)["waiting"])
        store.clock.advance(hours=3)   # past the job's last state change
        self.assertEqual(upgrade.run(ctx)["records_rebuilt"], 1)
        self.assertTrue(upgrade.done(store))

    def test_command_start_reports_the_jobs_left_for_the_clock(self):
        """G1: every writing command starts, and says why a job is not rebuilt yet."""
        import contextlib
        import io

        from careeros import cli
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        job = self.paste(store, ctx)
        self.quiet(pipeline.process, ctx)
        self.as_deployed(store, job)
        store.clock.advance(hours=-2)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._command_start(ctx, vault_paths(Path(tmp.name)))
        self.assertIn("1 job(s) not rebuilt: this computer's clock", out.getvalue())

    def board_written_paste(self):
        """A job you pasted by its exact link that a board's read then wrote (D188), as an older engine left it."""
        from tests.test_v26_discovery import CLAIMS, setup
        store, ctx = setup()
        job = self.paste(store, ctx, link=self.LINK)
        pipeline.ingest(ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(555, "Claims Analyst", CLAIMS, "synth"))],
                        full_board=False)
        self.assertEqual(store.one("SELECT ats FROM jobs WHERE job_id = ?", (job,))[0], "manual")
        self.assertTrue(pipeline.board_written(store, job))
        self.as_deployed(store, job)
        return store, ctx, job

    def test_a_board_written_paste_whose_board_may_not_be_read_now_is_rebuilt(self):
        """G2: W1 read the job's ats ("manual"), so the job waited for a board read the site policy forbids, for good."""
        from careeros import upgrade
        from careeros.discovery import sources
        from careeros.review.labels import capture
        store, ctx, job = self.board_written_paste()
        with mock.patch.object(sources, "status", return_value="manual"):
            self.assertEqual(upgrade.run(ctx)["boards_unreadable_rebuilt"], 1)
            self.quiet(pipeline.process, ctx)
            self.assertIsNone(capture(store, POLICY, MID, job)["waiting"])

    def test_a_job_whose_board_is_made_manual_after_the_upgrade_is_rebuilt_at_the_next_processing_run(self):
        """G2, the second form: a job still waiting after the upgrade whose source is blocked later."""
        from careeros import upgrade
        from careeros.discovery import sources
        from careeros.review.labels import capture
        store, ctx, job = self.board_written_paste()
        self.assertEqual(upgrade.run(ctx)["jobs_waiting_for_a_board_read"], 1)   # its board may be read: it waits
        self.assertTrue(upgrade.done(store))
        with mock.patch.object(sources, "status", return_value="blocked"):
            waiting = capture(store, POLICY, MID, job)["waiting"]
            self.assertIn("rebuilt from its stored columns at the next processing run", waiting)
            self.assertNotIn("add-url", waiting)   # which the site policy refuses
            self.assertEqual(self.quiet(pipeline.process, ctx)["boards_unreadable_rebuilt"], 1)
            self.assertIsNotNone(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])

    def test_a_processing_run_leaves_a_w1_rebuild_the_clock_would_put_out_of_order(self):
        """G1 and G2: the run's W1 rebuild is left for a later run when the clock is behind; the run goes on."""
        from careeros import upgrade
        from careeros.discovery import sources
        store, ctx, job = self.board_written_paste()
        upgrade.run(ctx)   # its board may be read: it waits
        self.quiet(pipeline.process, ctx)   # screened while the clock ran ahead
        store.clock.advance(hours=-2)
        with mock.patch.object(sources, "status", return_value="manual"):
            counts = self.quiet(pipeline.process, ctx)
            self.assertEqual(counts[upgrade.CLOCK_BEHIND], 1)
            self.assertIsNone(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
            store.clock.advance(hours=3)
            self.assertEqual(self.quiet(pipeline.process, ctx)["boards_unreadable_rebuilt"], 1)

    def test_the_boards_of_a_posting(self):
        """G2: its own ats, the board its link names, and the board of every board read written into it."""
        from careeros import upgrade
        from careeros.discovery import sources
        from careeros.openings import record_observation
        from tests.test_v26_discovery import setup
        bare, bare_ctx = setup()
        pasted = self.paste(bare, bare_ctx, link=self.LINK)   # no board read yet: the board its link names
        self.assertEqual(upgrade.posting_boards(bare, bare.one("SELECT * FROM jobs WHERE job_id = ?", (pasted,))),
                         {"greenhouse"})
        store, ctx, job = self.board_written_paste()
        row = store.one("SELECT * FROM jobs WHERE job_id = ?", (job,))
        self.assertEqual(upgrade.posting_boards(store, row), {"greenhouse"})
        record_observation(store, source_kind="ats_scan", evidence_level="live_ats", job_id=job, key="lever:synth:abc")
        self.assertEqual(upgrade.posting_boards(store, row), {"greenhouse", "lever"})
        with mock.patch.object(sources, "status", side_effect=lambda board: "allowed" if board == "lever" else "manual"):
            self.assertFalse(upgrade.board_forbidden(store, row))   # one of its boards may be read
        with mock.patch.object(sources, "status", return_value="manual"):
            self.assertTrue(upgrade.board_forbidden(store, row))

    def test_a_rebuilt_location_its_paste_line_does_not_give_places_nothing(self):
        """G2 with D2: an older paste's Location line, and a location a board's read wrote since: not that line's."""
        from careeros import upgrade
        from careeros.discovery import sources
        from tests.test_v26_discovery import CLAIMS, setup
        store, ctx = setup()
        job = self.paste(store, ctx, link=self.LINK, text="Location: Remote - US" + chr(10) + CLAIMS)
        pipeline.ingest(ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(555, "Claims Analyst", CLAIMS, "synth",
                                                                            location="Remote - Canada"))], full_board=False)
        self.as_deployed(store, job)
        with mock.patch.object(sources, "status", return_value="manual"):
            upgrade.run(ctx)
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual((record["location"], record["location_source"]), ("Remote - Canada", "unknown"))


class LowTests(unittest.TestCase):
    """F (Low) and the hand check's pre-existing HC1-HC3: wording, a re-screen that flapped, a button-only card, a
    paste's note, add-url's answer, and the link forms that name a posting."""

    def test_a_used_preview_confirmed_again_after_a_skip_says_the_skip_stands(self):
        """F1: it printed "replay", as if the Apply stood."""
        from careeros.review.labels import PreviewRefused, record_label
        s = FileStore(self)   # labelled Apply from a preview
        preview = s.store.one("SELECT preview_id FROM labels WHERE job_id = ? AND label = 'apply'", (JOB,))["preview_id"]
        record_label(s.store, JOB, "skip", None, key="k-skip")
        s.store.clock.advance(minutes=1)
        with self.assertRaises(PreviewRefused) as refused:
            record_label(s.store, JOB, "apply", None, key="k-again", preview=preview, policy=POLICY, applicant=MID)
        self.assertIn("your later skip label", str(refused.exception))
        self.assertIn("stands", str(refused.exception))
        self.assertEqual(s.store.one("SELECT label FROM labels WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1",
                                     (JOB,))["label"], "skip")

    def test_a_used_preview_confirmed_again_while_its_label_stands_is_a_replay(self):
        """F1's guard: a retry of the label still in force grants nothing new and says so."""
        from careeros.review.labels import record_label
        s = FileStore(self)
        preview = s.store.one("SELECT preview_id FROM labels WHERE job_id = ? AND label = 'apply'", (JOB,))["preview_id"]
        self.assertEqual(record_label(s.store, JOB, "apply", None, key="k-again", preview=preview, policy=POLICY,
                                      applicant=MID), "replay")

    def test_a_field_read_as_null_and_then_blank_never_rescreens_the_job(self):
        """F2: the same read wrote the incoming column (None over ""), and the next processing run wrote it back."""
        from tests.test_v26_discovery import CLAIMS, setup
        store, ctx = setup()

        def scan(requisition):
            item = gh_item(1, "Claims Analyst", CLAIMS, "synth", req=requisition)
            return pipeline.ingest(ctx, EMPLOYER, [greenhouse_posting("synth", item)], full_board=False)
        scan("")
        pipeline.process(ctx)
        give_label(store, POLICY, MID, JOB, "apply")
        for requisition in (None, "", None):
            counts = scan(requisition)
            self.assertEqual(counts["unchanged"], 1)
            self.assertNotEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (JOB,))[0], "normalized")
            self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")

    def test_a_button_only_card_takes_its_title_from_its_own_text(self):
        """F3: the card was dropped as no_title, with no count, and its email filed as imported."""
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_alerts import alert_email
        dot, link = chr(0xB7), "https://acme.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-1"
        alert = parse_eml_bytes(alert_email(
            f"<html><body><table><tr><td><b>Claims Analyst</b><br>Acme Health {dot} Remote<br>"
            f'<a href="{link}">View Job</a></td></tr></table></body></html>'))
        self.assertEqual([(x.text, x.context.split(f" {dot} ")[0]) for x in alert.links], [("Claims Analyst", "Acme Health")])

    def test_a_button_after_a_linked_title_stays_untitled(self):
        """F3's guards: a link with its own title keeps it, whatever words stand above it in its cell; a button in a card
        whose title is linked has no lead-in of its own, so it never takes the employer's name, or an earlier card's
        title, as a title."""
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_alerts import alert_email
        dot, workday = chr(0xB7), "https://acme.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-1"
        alert = parse_eml_bytes(alert_email(
            "<html><body><table>"
            f'<tr><td><b>Claims Analyst</b><br>Acme Health {dot} Remote<br><a href="{workday}">View Job</a></td></tr>'
            f"<tr><td>Beta Health {dot} Remote<br>"
            f'<a href="https://boards.greenhouse.io/synth/jobs/1">Claims Specialist</a><br>Beta Health {dot} Remote<br>'
            '<a href="https://boards.greenhouse.io/synth/jobs/1#app">Apply now</a></td></tr></table></body></html>'))
        self.assertEqual([x.text for x in alert.links], ["Claims Analyst", "Claims Specialist", ""])

    def test_a_paste_through_another_form_of_the_posting_link_offers_the_location_command(self):
        """HC1: the note said "matched by employer and title only, so it may be another opening", though D188 found the
        job by the posting its exact link names."""
        import contextlib
        import io

        from careeros.discovery.manual_intake import intake
        from tests.test_v26_discovery import CLAIMS, setup
        store, ctx = setup()
        with contextlib.redirect_stdout(io.StringIO()):
            first = intake(ctx, CLAIMS, "https://job-boards.greenhouse.io/synth/jobs/555", company="Synthetic Health",
                           title="Claims Analyst")["job_id"]
            res = intake(ctx, CLAIMS, "https://boards.greenhouse.io/synth/jobs/555?gh_src=linkedin",
                         company="Synthetic Health", title="Claims Analyst", location="Remote - US")
        self.assertEqual((res["job_id"], res["status"]), (first, "already_tracked"))
        self.assertNotIn("by employer and title only", res["location_note"])
        self.assertIn(f"run careeros location {first}", res["location_note"])

    def test_add_url_for_a_posting_that_cannot_be_read_ends_with_one_line(self):
        """HC2: a FetchError traceback."""
        from types import SimpleNamespace

        from careeros import cli
        from careeros.discovery.http import FetchError
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = SimpleNamespace(url="https://boards.greenhouse.io/synth/jobs/555", text_file=None, employer=None, title=None,
                               location=None, parent=None, separate=False, operation=None)
        with mock.patch.object(cli, "_ingest_board_link", side_effect=FetchError("not_found", 404)), \
                self.assertRaises(SystemExit) as stopped:
            cli.cmd_add_url(ctx, vault_paths(Path(tmp.name)), args)
        self.assertEqual(str(stopped.exception),
                         "Not added: https://boards.greenhouse.io/synth/jobs/555 could not be read: not_found")

    def test_only_an_exact_http_link_names_a_posting(self):
        """HC3: a javascript:// or ftp:// form, or a LinkedIn id with more after its digits, named the posting."""
        from careeros.discovery.board_discovery import posting_identity
        self.assertEqual(posting_identity("https://boards.greenhouse.io/synth/jobs/1"), ("greenhouse", "1"))
        self.assertEqual(posting_identity("HTTP://boards.greenhouse.io/synth/jobs/1"), ("greenhouse", "1"))
        self.assertIsNone(posting_identity("javascript://boards.greenhouse.io/synth/jobs/1"))
        self.assertIsNone(posting_identity("ftp://boards.greenhouse.io/synth/jobs/1"))
        view = "https://www.linkedin.com/jobs/view/"
        self.assertEqual(posting_identity(view + "4000000001/"), ("linkedin", view + "4000000001"))
        self.assertEqual(posting_identity(view + "claims-analyst-at-foo-4000000001"), ("linkedin", view + "4000000001"))
        self.assertIsNone(posting_identity(view + "4000000001x"))


if __name__ == "__main__":
    unittest.main()
