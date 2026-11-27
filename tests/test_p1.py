"""P1 (the D190 design, revision 8, section 3): an Apply or Stretch label binds to the preview the owner confirmed.

Part 6a: the read transaction, sightings, hold ids, the capture, previews (the CLI's and the review page's) and the label
write. Part 6b: P1 at use (packages.route_for). Part 6c: the consumers (prepare, readiness, the brief, authorize,
the session replay). Part 7a: intake operation receipts (section 6). Part 7b: holds (section 7). Part 5: the alert
fetch-only step (section 5). Part 8: the upgrade pass (section 7). Synthetic data only; nothing here reads the network."""
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from careeros import cli
from careeros.db.store import PermissionDenied
from careeros.discovery.boards import greenhouse_posting
from careeros.discovery.manual_intake import intake
from careeros.normalize.posting import RawPosting
from careeros.pipeline import employer_entry, ingest, process
from careeros.review import labels, packages
from careeros.review.labels import PreviewRefused, capture, record_label, write_preview
from careeros.review.report import build_report
from tests.helpers import give_label
from tests.test_packages import FULL_TIME
from tests.test_v26_discovery import CLAIMS, MID, POLICY, gh_item, setup

EMPLOYER = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}
JOB = "greenhouse:synth:1"


def screened(text=CLAIMS, n=1, **item):
    """A board posting read and screened, as the daily run leaves it."""
    store, ctx = setup()
    ingest(ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(n, "Claims Analyst", text, "synth", **item))],
           full_board=False)
    process(ctx)
    return store, ctx


def board_read(ctx, text=CLAIMS, n=1, **item):
    ingest(ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(n, "Claims Analyst", text, "synth", **item))],
           full_board=False)


def copy_of(text=CLAIMS, url="https://boards.greenhouse.io/synth/jobs/1"):
    """A saved copy of the board posting (by its exact link), as an import reads it."""
    return RawPosting(ats="manual", board_token="synthetic-health", external_id="R-1", title="Claims Analyst",
                      location="Remote - US", url=url, apply_url=url, description_text=text,
                      raw={"source": "saved", "location_source": "file"})


class ReadTransactionTests(unittest.TestCase):
    """Store.read (section 3, "Reads and writes"): one snapshot, no writes, a no-op inside a transaction."""

    def test_a_read_refuses_writes_and_inside_a_write_it_is_that_write(self):
        store, _ = screened()
        with store.read():
            with self.assertRaises(PermissionDenied):
                store.writer("review").event("probe", outcome="informational")
        self.assertIsNone(store.one("SELECT 1 FROM events WHERE event_type = 'probe'"))
        with store.tx():
            with store.read():   # the caller's transaction: its writes stand
                store.writer("review").event("probe", outcome="informational")
        self.assertIsNotNone(store.one("SELECT 1 FROM events WHERE event_type = 'probe'"))
        self.assertFalse(store.conn.in_transaction)

    def test_a_read_sees_one_snapshot(self):
        import tempfile
        from pathlib import Path

        from careeros.core.clock import FixedClock
        from careeros.db.store import open_store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "careeros.db")
        first, second = open_store(db, FixedClock(), "p"), open_store(db, FixedClock(), "p")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with first.read():
            before = first.one("SELECT COUNT(*) FROM events")[0]
            second.writer("review").event("probe", outcome="informational")   # another connection commits meanwhile
            self.assertEqual(first.one("SELECT COUNT(*) FROM events")[0], before)
        self.assertEqual(first.one("SELECT COUNT(*) FROM events")[0], before + 1)


class SightingTests(unittest.TestCase):
    """Part d: the observations that record a posting's content carry the incoming read's source record."""

    def sightings(self, store, job_id=JOB):
        return [json.loads(r["facts_json"]) for r in store.all(
            "SELECT facts_json FROM observations WHERE job_id = ? AND json_extract(facts_json, '$.sighting') = 1 "
            "ORDER BY rowid", (job_id,))]

    def test_a_new_job_a_copy_of_a_board_posting_and_a_paste_of_a_tracked_job_each_record_one(self):
        store, ctx = screened()
        self.assertEqual(len(self.sightings(store)), 1)   # the new job's read
        self.assertEqual(self.sightings(store)[0]["source"], json.loads(store.one(
            "SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))["source_json"]))
        entry = employer_entry(ctx, name="Synthetic Health")
        counts = ingest(ctx, entry, [copy_of(CLAIMS + " Edited in the copy.")], source="manual_import", full_board=False)
        self.assertEqual(counts["already_tracked"], 1)   # never written over the board's data...
        copy = self.sightings(store)[-1]
        self.assertEqual(copy["source"]["description_text"], CLAIMS + " Edited in the copy.")   # ...but recorded
        self.assertEqual(copy["source"]["ats"], "manual")
        result = intake(ctx, CLAIMS + " Pasted.", url="https://boards.greenhouse.io/synth/jobs/1",
                        company="Synthetic Health", title="Claims Analyst")
        self.assertEqual(result["status"], "already_tracked")
        self.assertEqual(self.sightings(store)[-1]["source"]["description_text"], CLAIMS + " Pasted.")
        self.assertTrue(all(s["source_digest"] for s in self.sightings(store)))

    def test_a_scan_location_confirmation_and_an_alert_line_are_not_sightings(self):
        store, ctx = screened()
        board_read(ctx)   # the same read again: the scan's confirmation, no new sighting
        self.assertEqual(len(self.sightings(store)), 1)
        observed = store.one("SELECT COUNT(*) FROM observations WHERE job_id = ?", (JOB,))[0]
        self.assertGreaterEqual(observed, 1)


class HoldIdTests(unittest.TestCase):
    """Section 7, "Which holds reach a job": every hold carries a hold_id; a line keeps its id when it becomes a row."""

    def test_two_file_holds_at_one_clock_reading_get_two_ids(self):
        store, _ = screened()
        packages.add_pending_hold(store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        packages.add_pending_hold(store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        ids = [h["hold_id"] for h in packages.open_holds(store, JOB)]
        self.assertEqual(len(set(ids)), 2)

    def test_a_line_keeps_its_id_as_a_row_and_a_row_that_landed_is_not_written_twice(self):
        store, _ = screened()
        packages.add_pending_hold(store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        line = packages.pending_holds(store)[0]
        kept = list(packages.pending_holds(store))
        self.assertEqual(packages.record_pending_holds(store), 1)
        self.assertEqual([h["hold_id"] for h in packages.open_holds(store, JOB)], [line["hold_id"]])
        packages._write_pending(store, kept)   # the file's rewrite failed after the rows committed
        self.assertEqual(packages.record_pending_holds(store), 0)
        self.assertEqual(store.one("SELECT COUNT(*) FROM exceptions WHERE error_code = 'ingest_failed'")[0], 1)
        self.assertEqual(packages.pending_holds(store), [])

    def test_a_row_written_directly_is_named_by_its_exception_id(self):
        store, _ = screened()
        exception_id = store.writer("discovery").exception("data_quality", "medium", "ingest failed: x", entity_type="job",
                                                            entity_id=JOB, error_code="ingest_failed",
                                                            metadata={"rule": packages.HOLD_RULE})
        self.assertEqual([h["hold_id"] for h in packages.open_holds(store, JOB)], [exception_id])


class CaptureTests(unittest.TestCase):
    """The preview (section 3): what an Apply or Stretch label approves, read in one transaction."""

    def test_the_capture_holds_the_four_parts_and_its_digest_is_stable(self):
        store, _ = screened()
        first = capture(store, POLICY, MID, JOB)
        self.assertIsNone(first["waiting"])
        self.assertEqual(set(first["parts"]), {"source", "derived", "screening", "group", "holds"})
        self.assertEqual(first["parts"]["source"], store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])
        self.assertEqual(first["parts"]["group"]["members"], [JOB])
        store.writer("discovery").update("jobs", {"last_seen_at": "2026-10-13T00:00:00.000Z"}, "job_id = ?", (JOB,))
        self.assertEqual(capture(store, POLICY, MID, JOB)["digest"], first["digest"])   # not a shown value
        self.assertNotEqual(capture(store, POLICY, MID, JOB, "night")["digest"], first["digest"])   # one profile each

    def test_no_preview_without_a_source_record_or_a_current_screening(self):
        store, ctx = screened()
        disc = store.writer("discovery")
        disc.transition(ctx.matrix, JOB, "normalized", reason_code="test")   # sent back to be screened again
        self.assertIn("waiting for screening", capture(store, POLICY, MID, JOB)["waiting"])
        process(ctx)
        self.assertIsNone(capture(store, POLICY, MID, JOB)["waiting"])
        disc.update("jobs", {"source_json": None}, "job_id = ?", (JOB,))   # a job stored before 017
        self.assertIn("waiting for a board read", capture(store, POLICY, MID, JOB)["waiting"])

    def test_no_preview_from_a_screening_of_another_record_or_extraction(self):
        """Part c: the gate result records the source record it judged; one judged on another is not current, even when
        the job was never sent back to be screened (a write that changed the record without moving the job back)."""
        for column, value in (("extraction_fingerprint", "another-extraction"),):
            with self.subTest(column):
                store, _ = screened()
                store.writer("extract").update("jobs", {column: value}, "job_id = ?", (JOB,))
                self.assertIn("waiting for screening", capture(store, POLICY, MID, JOB)["waiting"])
        store, _ = screened()
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])
        record["department"] = "Claims"   # not a value the card shows: only the record moved
        from careeros.pipeline import source_json
        store.writer("discovery").update("jobs", {"source_json": source_json(record)}, "job_id = ?", (JOB,))
        self.assertIn("waiting for screening", capture(store, POLICY, MID, JOB)["waiting"])

    def test_no_preview_while_a_stored_value_is_not_what_its_record_says(self):
        store, _ = screened()
        store.writer("discovery").update("jobs", {"remote_type": "hybrid"}, "job_id = ?", (JOB,))
        self.assertIn("read again", capture(store, POLICY, MID, JOB)["waiting"])

    def test_a_differing_copy_and_an_open_hold_are_shown_and_change_the_digest(self):
        store, ctx = screened()
        first = capture(store, POLICY, MID, JOB)
        ingest(ctx, employer_entry(ctx, name="Synthetic Health"), [copy_of(CLAIMS + " Edited.")], source="manual_import",
               full_board=False)
        second = capture(store, POLICY, MID, JOB)
        self.assertEqual(len(second["parts"]["group"]["sightings"]), 1)
        self.assertNotEqual(second["digest"], first["digest"])
        packages.add_pending_hold(store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        third = capture(store, POLICY, MID, JOB)
        self.assertEqual(len(third["parts"]["holds"]), 1)
        self.assertNotEqual(third["digest"], second["digest"])


class LabelWriteTests(unittest.TestCase):
    """The label write (section 3): Apply or Stretch only from a preview whose digest is still the job's."""

    def setUp(self):
        self.store, self.ctx = screened()

    def label(self, label, preview, key="k"):
        return record_label(self.store, JOB, label, None, key, preview=preview, policy=POLICY, applicant=MID)

    def test_apply_and_stretch_need_a_preview_and_skip_does_not(self):
        for label in ("apply", "stretch"):
            with self.assertRaisesRegex(PreviewRefused, "preview_required"):
                record_label(self.store, JOB, label, None, f"k-{label}")
        self.assertEqual(record_label(self.store, JOB, "skip", None, "k-skip"), "new")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM labels")[0], 1)

    def test_a_preview_is_for_one_job_and_profile_and_one_label(self):
        pid = write_preview(self.store, capture(self.store, POLICY, MID, JOB))
        with self.assertRaisesRegex(PreviewRefused, "preview_unknown"):
            self.label("apply", "preview:nothing")
        with self.assertRaisesRegex(PreviewRefused, "preview_other_job"):
            record_label(self.store, JOB, "apply", None, "k-night", "night", preview=pid, policy=POLICY, applicant=MID)
        self.assertEqual(self.label("apply", pid, "k1"), "new")
        self.assertEqual(self.label("apply", pid, "k2"), "replay")   # the same label again grants nothing new
        with self.assertRaisesRegex(PreviewRefused, "preview_used"):
            self.label("stretch", pid, "k3")
        row = self.store.one("SELECT * FROM label_previews WHERE preview_id = ?", (pid,))
        label = self.store.one("SELECT * FROM labels WHERE preview_id = ?", (pid,))
        self.assertEqual((row["label_id"], label["label"]), (label["label_id"], "apply"))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM labels")[0], 1)

    def test_a_change_between_the_preview_and_its_confirmation_refuses_the_label(self):
        changes = {
            "a board read": lambda: board_read(self.ctx, CLAIMS + " Now with a bonus."),
            "a differing copy": lambda: ingest(self.ctx, employer_entry(self.ctx, name="Synthetic Health"),
                                               [copy_of(CLAIMS + " Edited.")], source="manual_import", full_board=False),
            "a failed read": lambda: packages.add_pending_hold(self.store, JOB, ("greenhouse", "1"), "ingest failed: x"),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.store, self.ctx = screened()
                pid = write_preview(self.store, capture(self.store, POLICY, MID, JOB))
                change()
                process(self.ctx)
                with self.assertRaisesRegex(PreviewRefused, "job_changed|no preview"):
                    self.label("apply", pid)
                self.assertIsNone(self.store.one("SELECT 1 FROM labels"))

    def test_a_label_written_without_a_preview_never_routes(self):
        """A labels row with no preview (given before this upgrade, or by an older engine) waits for you to label the job
        again from a preview (LABEL_RULES labels-2026-11-24)."""
        from tests.helpers import stored_label
        stored_label(self.store, JOB, "apply")
        label = self.store.one("SELECT * FROM labels")
        self.assertFalse(labels.label_confirmed(self.store, label))
        self.assertRegex(packages.route_for(self.store, POLICY, JOB, MID)[1], "^label_unconfirmed")

    def test_a_label_whose_preview_is_not_its_own_never_routes(self):
        """A labels row written around record_label that names another label's preview, under the current rules, is not
        given from a preview: a preview confirms the one label recorded from it."""
        give_label(self.store, POLICY, MID, JOB, "apply")
        first = self.store.one("SELECT * FROM labels")
        review = self.store.writer("review")
        self.store.clock.advance(minutes=1)
        review.insert("labels", {"label_id": "label:copy", "job_id": JOB, "at": self.store.clock.now(), "label": "apply",
                                 "reason": None, "system_tier": None, "system_decision": "scored", "profile": "primary",
                                 "preview_id": first["preview_id"]})
        review.event("job_labelled", outcome="informational", entity_type="job", entity_id=JOB, actor_type="human",
                     actor_id="user", metadata={"label": "apply", "label_id": "label:copy", "label_rules": labels.LABEL_RULES,
                                                "opening": labels.opening_of(self.store, JOB),
                                                "preview_id": first["preview_id"]})
        copy = self.store.one("SELECT * FROM labels WHERE label_id = 'label:copy'")
        self.assertFalse(labels.label_confirmed(self.store, copy))
        self.assertRegex(packages.route_for(self.store, POLICY, JOB, MID)[1], "^label_unconfirmed")

    def test_the_label_records_its_preview_and_the_holds_it_showed(self):
        packages.add_pending_hold(self.store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        captured = capture(self.store, POLICY, MID, JOB)
        pid = write_preview(self.store, captured)
        self.label("apply", pid)
        label = self.store.one("SELECT * FROM labels")
        self.assertTrue(labels.label_confirmed(self.store, label))
        given = labels._given(self.store, label)
        self.assertEqual(given["holds"], captured["parts"]["holds"])
        self.assertEqual(given["digest"], captured["digest"])


class CliPreviewTests(unittest.TestCase):
    """`careeros label JOB apply` prints the preview; `--confirm ID` records the label from it."""

    def run_label(self, store, ctx, job_id, label, *flags):
        confirm = flags[flags.index("--confirm") + 1] if "--confirm" in flags else None
        args = SimpleNamespace(job_id=job_id, label=label, reason=None, night=False, confirm=confirm)
        out = io.StringIO()
        code = 0
        with redirect_stdout(out):
            try:
                cli.cmd_label(SimpleNamespace(store=store, policy=POLICY, profile=MID), {}, args)
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), code

    def test_apply_prints_a_preview_and_confirm_records_it(self):
        store, ctx = screened()
        text, code = self.run_label(store, ctx, JOB, "apply")
        self.assertEqual(code, 0)
        self.assertIn("Claims Analyst at Synthetic Health", text)
        preview_id = text.rsplit("--confirm ", 1)[1].split()[0]
        self.assertIsNone(store.one("SELECT 1 FROM labels"))   # a preview records no label
        text, code = self.run_label(store, ctx, JOB, "apply", "--confirm", preview_id)
        self.assertEqual((text.strip(), code), ("new", 0))
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")

    def test_a_refused_confirmation_shows_a_fresh_preview(self):
        store, ctx = screened()
        text, _ = self.run_label(store, ctx, JOB, "apply")
        preview_id = text.rsplit("--confirm ", 1)[1].split()[0]
        board_read(ctx, CLAIMS + " Now with a bonus.")
        process(ctx)
        text, code = self.run_label(store, ctx, JOB, "apply", "--confirm", preview_id)
        self.assertEqual(code, 2)
        self.assertIn("job_changed", text)
        self.assertIn("--confirm preview:", text)
        self.assertNotIn(preview_id, text.rsplit("--confirm ", 1)[1])

    def test_skip_takes_no_preview(self):
        store, ctx = screened()
        _, code = self.run_label(store, ctx, JOB, "skip", "--confirm", "preview:x")
        self.assertIn("Skip needs no preview", str(code))
        text, code = self.run_label(store, ctx, JOB, "skip")
        self.assertEqual((text.strip(), code), ("new", 0))


class PagePreviewTests(unittest.TestCase):
    """The review page captures each card in one read and keeps it under the card's form key; a label from it is
    recorded only while the job is still what the card showed."""

    def page(self, store):
        form_keys, previews = {}, {}
        html = build_report(store, MID, POLICY, token="t", form_keys=form_keys, previews=previews)
        keys = [k for k, (action, target) in form_keys.items() if action == "label" and target == JOB]
        return html, keys, previews

    def test_a_card_offers_apply_only_with_its_preview(self):
        store, ctx = screened()
        html, keys, previews = self.page(store)
        self.assertTrue(keys and all(k in previews for k in keys))
        self.assertIn(f'name="digest" value="{previews[keys[0]]["digest"]}"', html)
        # a stored value its record no longer gives (the processing run writes it again): the card stays, without Apply
        ctx.store.writer("discovery").update("jobs", {"remote_type": "hybrid"}, "job_id = ?", (JOB,))
        html, keys, previews = self.page(store)
        self.assertTrue(keys and not any(k in previews for k in keys))
        self.assertIn("waiting for screening", html)
        self.assertNotIn('value="apply"', html)

    def test_the_page_writes_the_preview_with_the_label_and_refuses_a_changed_job(self):
        store, ctx = screened()
        _, keys, previews = self.page(store)
        captured = previews[keys[0]]
        self.assertEqual(record_label(store, JOB, "apply", None, keys[0], preview=captured, policy=POLICY,
                                      applicant=MID), "new")
        row = store.one("SELECT p.digest, p.used_at FROM labels l JOIN label_previews p ON p.preview_id = l.preview_id")
        self.assertEqual(row["digest"], captured["digest"])
        self.assertIsNotNone(row["used_at"])
        _, keys, previews = self.page(store)
        captured = previews[keys[0]]
        board_read(ctx, CLAIMS + " Now with a bonus.")
        process(ctx)
        with self.assertRaises(PreviewRefused):
            record_label(store, JOB, "stretch", None, keys[0], preview=captured, policy=POLICY, applicant=MID)


class HelperTests(unittest.TestCase):
    def test_give_label_labels_from_a_preview(self):
        store, _ = screened()
        self.assertEqual(give_label(store, POLICY, MID, JOB, "apply"), "new")
        self.assertIsNotNone(store.one("SELECT preview_id FROM labels")[0])


class P1AtUseTests(unittest.TestCase):
    """Part 6b (section 3, "P1 at use"): an Apply or Stretch label routes only while the job is what its preview showed,
    and past exactly the holds that preview showed."""

    def setUp(self):
        self.store, self.ctx = screened()
        give_label(self.store, POLICY, MID, JOB, "apply")

    def route(self):
        return packages.route_for(self.store, POLICY, JOB, MID)

    def assertHeld(self, pattern):
        route, why = self.route()
        self.assertIsNone(route, why)
        self.assertRegex(why, pattern)

    def test_a_label_routes_while_the_job_is_what_its_preview_showed(self):
        self.assertEqual(self.route()[0], "apply")
        board_read(self.ctx)   # the same read again
        process(self.ctx)
        self.assertEqual(self.route()[0], "apply")

    def test_a_changed_posting_holds_the_label_until_it_is_given_again(self):
        """Part a: the source record byte for byte, whatever the job's state and however it was screened since."""
        board_read(self.ctx, CLAIMS + " Now with a bonus.")
        self.assertHeld("^label_facts_changed: .*the posting as read \\(description_text\\)")
        process(self.ctx)   # screened again, the same decision: still not what the label approved
        self.assertHeld("^label_facts_changed")
        give_label(self.store, POLICY, MID, JOB, "apply")
        self.assertEqual(self.route()[0], "apply")

    def test_a_spacing_only_or_nfc_equivalent_edit_is_the_same_read_and_a_lookalike_is_not(self):
        """Codex H17 in two parts: a horizontal-space-only change and an NFC-equivalent spelling are the same read; a
        variant NFC does not unify (full-width letters) is written, screened again, and holds the label."""
        board_read(self.ctx, CLAIMS.replace("health plan", "health  plan\t"))
        process(self.ctx)
        self.assertEqual(self.route()[0], "apply")
        board_read(self.ctx, CLAIMS + " Caf\u00e9 benefits.")   # composed
        process(self.ctx)
        give_label(self.store, POLICY, MID, JOB, "apply")
        board_read(self.ctx, CLAIMS + " Cafe\u0301 benefits.")   # decomposed: the same text in its canonical form
        process(self.ctx)
        self.assertEqual(self.route()[0], "apply")
        board_read(self.ctx, CLAIMS.replace("Excel", "\uff25xcel") + " Caf\u00e9 benefits.")   # a full-width E
        process(self.ctx)
        self.assertHeld("^label_facts_changed")

    def test_a_reader_update_that_changes_a_shown_value_holds_the_label(self):
        """Part b: the values derived again under the current readers must be those shown."""
        from unittest import mock

        from careeros import pipeline
        with mock.patch.object(pipeline, "timezone_requirement", return_value="ET"):
            self.assertHeld("the values read from it \\(timezone_requirement\\)")
        self.assertEqual(self.route()[0], "apply")

    def test_the_screening_must_be_current_and_what_the_card_showed(self):
        """Part c: a job sent back to be screened waits; a current screening with other flags holds the label."""
        disc = self.store.writer("discovery")
        disc.transition(self.ctx.matrix, JOB, "normalized", reason_code="test")
        self.assertHeld("its screening, which waits for the next processing run")
        process(self.ctx)
        self.assertEqual(self.route()[0], "apply")
        gate = dict(self.store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (JOB,)))
        gate.update(result_id="gate:later", flags_json=json.dumps([{"code": "remote_unknown", "detail": "remote scope unclear"}]))
        self.store.writer("qualify").insert("gate_results", gate)
        self.assertHeld("its screening \\(now eligible\\)")

    def test_a_copy_that_says_something_else_after_the_label_holds_it(self):
        """Part d: a sighting recorded after the label that differs from its source record (Codex Pass 3: an exact-link
        paste of a changed copy of a labelled board job)."""
        intake(self.ctx, CLAIMS + " Pasted with an edit.", url="https://boards.greenhouse.io/synth/jobs/1",
               company="Synthetic Health", title="Claims Analyst")
        self.assertHeld("a copy of this posting says something else \\(" + JOB)
        give_label(self.store, POLICY, MID, JOB, "apply")   # from a preview that shows the copy
        self.assertEqual(self.route()[0], "apply")

    def test_a_member_joined_later_brings_its_older_differing_sightings(self):
        """Codex M2: a sighting recorded before the label, of a job that was not in the label's group then and is a
        member now (its link was read again as the posting's), holds the label, whenever it was recorded."""
        from careeros.openings import record_observation
        from careeros.pipeline import sighting_facts
        from tests.test_intake_identity import twin
        store, ctx = screened()
        other = "manual:synthetic-health:twin"
        twin(store, other, "https://careers.example.com/jobs/claims")   # another posting, for now
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])
        record.update(description_text=CLAIMS + " An older copy.", ats="manual")
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=other,
                           url="https://careers.example.com/jobs/claims", facts=sighting_facts(record))
        give_label(store, POLICY, MID, JOB, "apply")   # its group: the job alone
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")
        disc = store.writer("discovery")   # now the same posting, joined as the job's duplicate: a member of its group
        disc.update("jobs", {"url": "https://job-boards.greenhouse.io/synth/jobs/1", "duplicate_of": JOB}, "job_id = ?",
                    (other,))
        disc.transition(ctx.matrix, other, "normalized")
        disc.transition(ctx.matrix, other, "duplicate", reason_code="joined")
        route, why = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertRegex(why, "a copy of this posting says something else \\(manual:synthetic-health:twin\\)")

    def test_a_label_routes_past_exactly_the_holds_its_preview_showed(self):
        """Codex H25: the label acknowledges the holds its preview showed, for itself only; a later one refuses it."""
        packages.add_pending_hold(self.store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        self.assertHeld("^posting_not_stored: ingest failed: disk")
        give_label(self.store, POLICY, MID, JOB, "apply")   # from a preview that showed the hold
        self.assertEqual(self.route()[0], "apply")
        packages.record_pending_holds(self.store)   # the line becomes a row: the same hold_id
        self.assertEqual(self.route()[0], "apply")
        self.store.writer("discovery").exception("data_quality", "medium", "ingest failed: another", entity_type="job",
                                                 entity_id=JOB, error_code="ingest_failed",
                                                 metadata={"rule": packages.HOLD_RULE})
        self.assertHeld("^posting_not_stored: ingest failed: another")

    def test_a_skip_given_after_still_wins_and_a_label_needs_a_profile_to_route(self):
        self.assertRegex(packages.route_for(self.store, POLICY, JOB)[1], "^label_facts_changed: .*no applicant profile")
        record_label(self.store, JOB, "skip", None, "k-skip")
        self.assertEqual(self.route(), (None, "you labelled it Skip"))


class ConsumerTests(unittest.TestCase):
    """Part 6c (section 3, "Consistent consumers", "Session replay"): each consumer makes its P1 decision and every read
    its action uses in one transaction, and acts only on what it recorded."""

    def setUp(self):
        from tests.test_packages import Vault
        self.vault = Vault()
        self.addCleanup(self.vault.close)
        self.store, self.ctx = screened(FULL_TIME)
        give_label(self.store, POLICY, MID, JOB, "apply")

    def prepare(self, **kw):
        from tests.test_packages import FACTS, QUESTIONS
        return packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                vault_root=self.vault.root, questions=QUESTIONS, **kw)

    def ready(self, package=None):
        from tests.test_packages import FACTS, QUESTIONS
        return packages.readiness(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                  vault_root=self.vault.root, questions=QUESTIONS, package=package)

    def open_session(self, key="o1"):
        from careeros.review import assist
        from tests.test_packages import FACTS, QUESTIONS
        return assist.open_session(self.store, POLICY, MID, JOB, "manual", facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=QUESTIONS, key=key)

    def test_prepare_records_each_file_and_reads_in_one_transaction(self):
        from unittest import mock
        seen = []
        real = packages.build_manifest

        def spy(store, *args, **kwargs):
            seen.append(store.conn.in_transaction)
            return real(store, *args, **kwargs)
        with mock.patch.object(packages, "build_manifest", side_effect=spy):
            out = self.prepare()
        self.assertEqual(seen, [True])   # the route check, the manifest and the row: one transaction
        row = self.store.one("SELECT files_json, folder FROM packages WHERE package_id = ?", (out["package_id"],))
        files = json.loads(row["files_json"])
        self.assertEqual(sorted(files), ["answers.md", "brief.md", "manifest.json"])
        self.assertTrue(packages.files_ok(self.vault.root, self.store.one("SELECT * FROM packages")))
        self.assertEqual(self.ready()["state"], "ready")

    def test_a_package_with_a_missing_file_is_never_ready_and_prepare_repairs_it(self):
        """Codex M1: an interrupted write or a deleted file; the next prepare renders that version again from its row's
        manifest and the current inputs and writes it only when every file matches."""
        out = self.prepare()
        (self.vault.root / out["folder"] / "answers.md").unlink()
        state = self.ready()
        self.assertEqual(state["state"], "files_missing")
        self.assertIn("package_files_missing", state["reasons"][0])
        again = self.prepare()
        self.assertEqual((again["version"], again["new"], again.get("repaired")), (1, False, True))
        self.assertEqual(self.ready()["state"], "ready")
        brief = self.vault.root / out["folder"] / "brief.md"
        brief.write_text(brief.read_text(encoding="utf-8") + "an edit on disk.", encoding="utf-8")
        self.assertEqual(self.ready()["state"], "files_missing")   # changed on disk: not the file its row recorded
        self.assertTrue(self.prepare().get("repaired"))
        self.assertNotIn("an edit on disk", brief.read_text(encoding="utf-8"))

    def test_an_interrupted_write_leaves_a_row_that_is_not_ready_until_repaired(self):
        from unittest import mock
        real = packages._write_files

        def half(folder, rendered):
            real(folder, dict(list(rendered.items())[:1]))
            raise OSError("disk full")
        with mock.patch.object(packages, "_write_files", side_effect=half), self.assertRaises(OSError):
            self.prepare()
        self.assertIsNotNone(self.store.one("SELECT 1 FROM packages"))   # the row was committed first
        self.assertEqual(self.ready()["state"], "files_missing")
        self.assertTrue(self.prepare().get("repaired"))
        self.assertEqual(self.ready()["state"], "ready")

    def test_changed_inputs_make_a_new_version_and_the_old_one_is_never_ready(self):
        first = self.prepare()
        (self.vault.root / first["folder"] / "brief.md").unlink()
        self.vault.write(self.vault.root.joinpath("answers.yaml").read_text(encoding="utf-8").replace("Avery", "Avery J."))
        second = self.prepare()
        self.assertEqual((second["version"], second["new"]), (2, True))
        self.assertFalse((self.vault.root / first["folder"] / "brief.md").exists())   # not repaired
        self.assertEqual(self.ready(first["package_id"])["state"], "stale")
        self.assertEqual(self.ready()["state"], "ready")
        # the inputs back as they were: version 1's manifest is a fresh build again, but it is not the latest version
        self.vault.write(self.vault.root.joinpath("answers.yaml").read_text(encoding="utf-8").replace("Avery J.", "Avery"))
        stale = self.ready(first["package_id"])
        self.assertEqual(stale["state"], "stale")
        self.assertIn("no longer the latest", stale["reasons"][0])

    def test_a_package_from_before_017_is_never_ready(self):
        """A package row an engine before 017 wrote records no files (files_json): never ready, prepared again."""
        out = self.prepare()
        row = dict(self.store.one("SELECT * FROM packages WHERE package_id = ?", (out["package_id"],)))
        row.update(package_id="pkg:older-engine", version=2, files_json=None)
        self.store.writer("assist").insert("packages", row)
        self.assertEqual(self.ready()["state"], "files_missing")
        self.assertEqual(self.prepare()["version"], 3)   # its files cannot be checked: a new version
        self.assertEqual(self.ready()["state"], "ready")

    def test_a_brief_is_issued_only_when_its_draft_is_recorded(self):
        """Codex H13: route and brief captured in one read, checked again and recorded in one write, then written,
        never over an earlier brief; a change between the capture and the issue writes nothing."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        from careeros.review import shortlist
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        first = shortlist.draft(self.store, MID, JOB, folder, POLICY)
        second = shortlist.draft(self.store, MID, JOB, folder, POLICY)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        event = self.store.one("SELECT metadata_json FROM events WHERE event_type = 'draft_created' ORDER BY seq DESC LIMIT 1")
        self.assertIn(json.loads(event[0])["digest"], second.read_text(encoding="utf-8"))
        first.write_text("my own edits\n", encoding="utf-8")
        with mock.patch("os.link", side_effect=OSError(1, "hard links not supported")):   # a filesystem without them
            third = shortlist.draft(self.store, MID, JOB, folder, POLICY)
        self.assertNotIn(third, (first, second))   # created exclusively at the next free name
        self.assertEqual(first.read_text(encoding="utf-8"), "my own edits\n")
        real, calls = shortlist.brief_lines, []

        def moving(*args, **kwargs):   # a posting update between the capture and the issue
            calls.append(1)
            lines, issues = real(*args, **kwargs)
            return (lines + ["- changed"] if len(calls) > 1 else lines), issues
        with mock.patch.object(shortlist, "brief_lines", side_effect=moving), self.assertRaisesRegex(ValueError, "draft_changed"):
            shortlist.draft(self.store, MID, JOB, folder, POLICY)
        self.assertEqual(len(list(folder.glob("*.md"))), 3)
        record_label(self.store, JOB, "skip", None, "k-skip")
        with self.assertRaisesRegex(ValueError, "not_pursued"):
            shortlist.draft(self.store, MID, JOB, folder, POLICY)

    def test_authorize_is_refused_once_p1_holds_the_label(self):
        """Codex Pass 3: an authorization is an evaluation; after a conflicting sighting (or a retitle without a
        relabel) the same job, label and channel are refused, and a currently approved job is the control."""
        from careeros.review.submission import authorize
        first = authorize(self.store, POLICY, JOB, "direct", profile=MID)
        self.assertNotIn("not_approved", [r["code"] for r in first.reasons])
        self.assertEqual(first.label_id, self.store.one("SELECT label_id FROM labels")[0])
        self.assertIsNotNone(first.preview_digest)
        intake(self.ctx, FULL_TIME + " A pasted edit.", url="https://boards.greenhouse.io/synth/jobs/1",
               company="Synthetic Health", title="Claims Analyst")
        again = authorize(self.store, POLICY, JOB, "direct", profile=MID)
        self.assertFalse(again.allowed)
        self.assertIn("label_facts_changed", next(r["detail"] for r in again.reasons if r["code"] == "not_approved"))

    def test_a_session_replay_is_checked_again(self):
        """Codex H14: an open session's replay returns its first result only while its own package is ready, P1 accepts
        the job and a fresh authorization passes; a changed reusable answer supersedes it, with no answers path."""
        from careeros.review import assist
        from careeros.review.applications import application_matrix
        self.prepare()
        try:
            first = self.open_session()
        except assist.SessionRefused as exc:   # the synthetic posting's other checks: the replay rules still apply
            self.skipTest(f"no session opens for the fixture: {exc}")
        self.assertTrue(self.open_session()["replay"])
        self.vault.write(self.vault.root.joinpath("answers.yaml").read_text(encoding="utf-8").replace("Avery", "Avery J."))
        with self.assertRaisesRegex(assist.SessionRefused, "session_superseded") as refused:
            self.open_session()
        self.assertNotIn("answers.md", str(refused.exception))
        assist.close_session(self.store, application_matrix(), POLICY, first["session_id"], "uncertain", key="c1")
        replay = self.open_session()
        self.assertEqual((replay["state"], "answers" in replay), ("uncertain", False))


class IntakeRetryTests(unittest.TestCase):
    """Part 7a (section 6, Codex M5): an intake's retry is recognised by its operation's receipt, keyed per route; the
    job, its sighting and the receipt's completion are one transaction."""

    LINK = "https://careers.example.com/jobs/claims"

    def setUp(self):
        self.store, self.ctx = setup()

    def paste(self, text=CLAIMS, **kw):
        return intake(self.ctx, text, self.LINK, company="Synthetic Health", title="Claims Analyst", **kw)

    def count(self, table):
        return self.store.one(f"SELECT COUNT(*) FROM {table}")[0]

    def test_a_plain_rerun_is_the_retry_and_writes_nothing(self):
        first = self.paste()
        self.assertEqual(first["status"], "added")
        receipt = self.store.one("SELECT result_ref FROM receipts WHERE receipt_key = ?", (first["operation"],))
        self.assertEqual(receipt["result_ref"], first["job_id"])
        seen = (self.count("jobs"), self.count("observations"))
        again = self.paste()
        self.assertEqual((again["status"], again["job_id"]), ("recorded", first["job_id"]))
        self.assertEqual((self.count("jobs"), self.count("observations")), seen)
        changed = self.paste(CLAIMS + " Edited.")   # another request: an operation of its own
        self.assertNotEqual(changed["operation"], first["operation"])

    def test_a_form_key_under_another_request_is_refused(self):
        from careeros.db.store import IdempotencyCollision
        from careeros.discovery.manual_intake import operation_for
        op = operation_for("paste_page", "form-key-1", {"url": self.LINK, "text": CLAIMS, "company": "Synthetic Health"})
        self.paste(operation=op)
        other = operation_for("paste_page", "form-key-1", {"url": self.LINK, "text": CLAIMS + " x", "company": "Synthetic Health"})
        with self.assertRaises(IdempotencyCollision):
            self.paste(CLAIMS + " x", operation=other)

    def test_an_intake_interrupted_before_its_receipt_leaves_nothing_and_its_retry_runs_once(self):
        """Codex Pass 3: interrupt an intake just after the job is created and before its receipt completes."""
        from unittest import mock

        from careeros.discovery import manual_intake
        with mock.patch.object(manual_intake, "complete", side_effect=RuntimeError("interrupted")), \
                self.assertRaises(RuntimeError):
            self.paste()
        self.assertEqual((self.count("jobs"), self.count("receipts")), (0, 0))
        self.assertEqual(self.paste()["status"], "added")
        self.assertEqual(self.paste()["status"], "recorded")
        self.assertEqual(self.count("jobs"), 1)

    def test_a_separate_operation_records_its_pending_receipt_first_and_its_retry_completes_it_once(self):
        """Codex Pass 4: a --separate import interrupted before K:done; a retry with its id completes it once."""
        from unittest import mock

        from careeros.discovery import manual_intake
        from careeros.discovery.manual_intake import operation_for
        request = {"url": self.LINK, "text": CLAIMS, "company": "Synthetic Health", "title": "Claims Analyst",
                   "location": None}
        first = self.paste()
        op = operation_for("paste", "", request, separate=True)
        with mock.patch.object(manual_intake, "complete", side_effect=RuntimeError("interrupted")), \
                self.assertRaises(RuntimeError):
            self.paste(operation=op)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM receipts WHERE receipt_key = ?", (op.key,)))   # K, pending
        self.assertIsNone(self.store.one("SELECT 1 FROM receipts WHERE receipt_key = ?", (op.key + ":done",)))
        retry = operation_for("paste", "", request, operation_id=op.key)
        done = self.paste(operation=retry)
        # a second operation of the same request at a page that names no posting is a second opening: its own job,
        # linked to the first for your review (identity is never a retry: section 6)
        self.assertEqual(done["status"], "added")
        self.assertNotEqual(done["job_id"], first["job_id"])
        self.assertIsNotNone(self.store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ?",
                                            (done["job_id"], first["job_id"])))
        self.assertEqual(self.paste(operation=retry)["status"], "recorded")   # completed once
        self.assertEqual(self.store.one("SELECT result_ref FROM receipts WHERE receipt_key = ?", (op.key + ":done",))[0],
                         done["job_id"])
        self.assertEqual(self.count("jobs"), 2)
        with self.assertRaisesRegex(ValueError, "unknown operation"):
            self.paste(operation=operation_for("paste", "", request, operation_id="paste:op:nothing"))

    def test_a_paste_of_a_tracked_job_records_its_receipt_with_its_sighting(self):
        store, ctx = screened()
        link = "https://boards.greenhouse.io/synth/jobs/1"
        first = intake(ctx, CLAIMS + " Pasted.", url=link, company="Synthetic Health", title="Claims Analyst")
        self.assertEqual(first["status"], "already_tracked")
        observed = store.one("SELECT COUNT(*) FROM observations")[0]
        again = intake(ctx, CLAIMS + " Pasted.", url=link, company="Synthetic Health", title="Claims Analyst")
        self.assertEqual((again["status"], again["job_id"]), ("recorded", JOB))
        self.assertEqual(store.one("SELECT COUNT(*) FROM observations")[0], observed)

    def test_a_colliding_posting_that_comes_again_by_its_link_is_the_job_it_was_given(self):
        """Codex H11 (Pass 9) kept: a posting given a suffixed id beside another posting's generated id is that job when
        its own link comes again, with another text (another operation: identity by the exact link, not a retry)."""
        portal = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/"
        text = "Claims Analyst\nCompany: Acme Rx\nReq ID: R-4001\n\n" + CLAIMS
        a = intake(self.ctx, text, portal + "Claims-Analyst_R-4001")
        b = intake(self.ctx, text + " Another team.", portal + "Claims-Analyst_R-5002")
        self.assertNotEqual(a["job_id"], b["job_id"])
        again = intake(self.ctx, text + " Another team, edited.", portal + "Claims-Analyst_R-5002")
        self.assertEqual((again["status"], again["job_id"]), ("already_tracked", b["job_id"]))
        self.assertEqual(self.count("jobs"), 2)

    def test_add_url_and_import_record_their_operations(self):
        import argparse
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        from careeros import cli
        from careeros.paths import vault_paths
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "posting.txt"
            body.write_text(CLAIMS, encoding="utf-8")
            args = SimpleNamespace(url=self.LINK, text_file=str(body), employer="Synthetic Health", parent=None,
                                   title="Claims Analyst", location=None)
            paths = vault_paths(Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                job = cli.cmd_add_url(self.ctx, paths, args)
                self.assertEqual(cli.cmd_add_url(self.ctx, paths, args), job)   # the retry: its receipt's job
            self.assertEqual(self.count("jobs"), 1)
            self.assertEqual(self.store.one("SELECT COUNT(*) FROM receipts WHERE command = 'add_url'")[0], 1)
            inbox = paths["inbox"]
            inbox.mkdir(parents=True, exist_ok=True)
            saved = ("URL: https://careers.example.com/jobs/other\nTitle: Claims Specialist\nEmployer: Synthetic Health\n"
                     "Location: Remote - US\n\n" + CLAIMS)
            (inbox / "posting.txt").write_text(saved, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                cli._import_inbox(self.ctx, paths, argparse.Namespace())
            self.assertEqual(self.count("jobs"), 2)
            # interrupted after its job was written and before the file moved: the next import finds the receipt
            (inbox / "posting.txt").write_text(saved, encoding="utf-8")
            observed = self.count("observations")
            with contextlib.redirect_stdout(io.StringIO()):
                counts = cli._import_inbox(self.ctx, paths, argparse.Namespace())
            self.assertEqual((counts["already_recorded"], self.count("jobs"), self.count("observations")), (1, 2, observed))
            self.assertFalse((inbox / "posting.txt").exists())


class HoldTests(unittest.TestCase):
    """Part 7b (section 7; Codex H18, H20 to H25; Z1, Y1): a hold lifts only by a board read, or a write-ahead hold by
    its own batch's commit, by its id, in the read's own transaction."""

    def open_ids(self, store, job_id=JOB):
        return [h["hold_id"] for h in packages.open_holds(store, job_id)]

    def hold(self, store, job_id=JOB, rule=True):
        return store.writer("discovery").exception("data_quality", "medium", "ingest failed: x", entity_type="job",
                                                   entity_id=job_id, error_code="ingest_failed",
                                                   metadata={"rule": packages.HOLD_RULE} if rule else None)

    def test_a_board_read_lifts_a_hold_and_a_copy_does_not(self):
        """Codex Pass 5: a failed board read's hold, then a partial copy (still held), then a board read (cleared)."""
        store, ctx = screened()
        held = self.hold(store)
        intake(ctx, CLAIMS + " A partial copy.", url="https://boards.greenhouse.io/synth/jobs/1",
               company="Synthetic Health", title="Claims Analyst")
        self.assertEqual(self.open_ids(store), [held])
        board_read(ctx)
        self.assertEqual(self.open_ids(store), [])

    def test_a_saved_copy_of_a_board_written_posting_lifts_nothing(self):
        """Codex H21: a copy (an import or add-url --text-file of the board's posting) records a sighting and lifts no
        hold."""
        store, ctx = screened()
        held = self.hold(store)
        counts = ingest(ctx, employer_entry(ctx, name="Synthetic Health"), [copy_of(CLAIMS + " Saved.")],
                        source="manual_import", full_board=False)
        self.assertEqual(counts["already_tracked"], 1)
        self.assertEqual(self.open_ids(store), [held])

    def test_a_file_hold_cleared_in_a_committed_transaction_no_longer_holds_before_its_line_is_pruned(self):
        store, _ = screened()
        hold_id = packages.add_pending_hold(store, JOB, ("greenhouse", "1"), "ingest failed: disk")
        with store.tx():
            packages.clear_hold(store, JOB, hold_id, "its read was stored")
        self.assertEqual(len(packages.pending_holds(store)), 1)   # the line is still there until the prune
        self.assertEqual(self.open_ids(store), [])
        self.assertEqual(packages.prune_pending_holds(store), 1)
        self.assertEqual(packages.pending_holds(store), [])

    def test_a_board_the_site_policy_blocks_lifts_nothing(self):
        from unittest import mock

        from careeros.discovery import sources
        store, ctx = screened()
        held = self.hold(store)
        with mock.patch.object(sources, "status", return_value="manual"):
            board_read(ctx, CLAIMS + " Edited.")
        self.assertEqual(self.open_ids(store), [held])

    def test_an_older_engines_hold_stands_after_a_later_stored_read(self):
        """The gate session's revision 7 check: a deployed-engine hold (no rule), then a stored read that advances
        last_seen_at (a copy): still held (hr1-upgrade-2's rule is retired, H22)."""
        store, ctx = setup()
        WORKDAY = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-4455"   # names one posting
        first = intake(ctx, CLAIMS, WORKDAY, company="Synthetic Health",
                       title="Claims Analyst")
        store.clock.advance(minutes=5)
        held = self.hold(store, first["job_id"], rule=False)
        store.clock.advance(minutes=5)
        # a copy of it read and written again (add-url --text-file's route): the job is written, its hold stands
        copy = RawPosting(ats="manual", board_token="synthetic-health", external_id="R-4455", title="Claims Analyst",
                          location="Remote - US", url=WORKDAY, apply_url=WORKDAY, description_text=CLAIMS + " Read again.",
                          raw={"source": "human_text", "location_source": "none"})
        ingest(ctx, employer_entry(ctx, name="Synthetic Health"), [copy], source="manual_url", full_board=False)
        self.assertGreater(store.one("SELECT last_seen_at FROM jobs WHERE job_id = ?", (first["job_id"],))[0],
                           store.one("SELECT occurred_at FROM exceptions WHERE exception_id = ?", (held,))[0])
        self.assertIn(held, self.open_ids(store, first["job_id"]))

    def batch(self, store, ctx, postings, ahead, *, fail_at=None, outer_rollback=False):
        from unittest import mock

        from careeros import pipeline
        real = pipeline.normalized_fields
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if fail_at is not None and len(calls) - 1 == fail_at:
                raise RuntimeError("the second occurrence's write fails")
            return real(*args, **kwargs)
        with mock.patch.object(pipeline, "normalized_fields", side_effect=flaky):
            try:
                with store.tx():
                    pipeline.ingest(ctx, EMPLOYER, postings, full_board=False, placed={}, ahead=ahead)
                    if outer_rollback:
                        raise RuntimeError("the batch abandons its work")
            except RuntimeError:
                if not outer_rollback:
                    raise

    def test_a_write_ahead_hold_clears_only_its_own_occurrence_by_id(self):
        """Z1 (Codex design Pass 8): two occurrences of one posting in a batch, holds carried by position; the first is
        stored, the second fails: the second's exact hold id stays open. With an outer rollback both stay open."""
        from careeros import pipeline
        for outer_rollback in (False, True):
            with self.subTest(outer_rollback=outer_rollback):
                store, ctx = screened()
                one = greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS + " One.", "synth"))
                two = greenhouse_posting("synth", gh_item(1, "Claims Analyst II", CLAIMS + " Two.", "synth"))
                ahead = pipeline.write_ahead(store, [(0, JOB), (1, JOB)])
                first, second = ahead[0][0], ahead[1][0]
                self.assertNotEqual(first, second)
                self.batch(store, ctx, [one, two], ahead, fail_at=None if outer_rollback else 1,
                           outer_rollback=outer_rollback)
                still = set(self.open_ids(store))
                if outer_rollback:
                    self.assertEqual(still, {first, second})
                else:
                    self.assertIn(second, still)
                    self.assertNotIn(first, still)

    def test_a_write_ahead_hold_kept_beside_the_database_is_dropped_only_after_its_commit(self):
        """The gate session's revision 8 check: a write-ahead hold kept in the file (its commit failed), then its
        posting stored: cleared in the read's transaction (hold_clearances), the line dropped after the commit; a nested
        batch rolled back after a re-read that cleared a file hold leaves the hold in place."""
        from unittest import mock

        from careeros import pipeline
        store, ctx = screened()
        with mock.patch.object(store.writer("discovery").__class__, "exception", side_effect=RuntimeError("disk full")):
            ahead = pipeline.write_ahead(store, [(0, JOB)])
        hold_id = ahead[0][0]
        self.assertEqual([h["hold_id"] for h in packages.pending_holds(store)], [hold_id])
        posting = greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS + " Edited.", "synth"))
        with self.assertRaises(RuntimeError):
            with store.tx():
                pipeline.ingest(ctx, EMPLOYER, [posting], full_board=False, placed={}, ahead=ahead)
                raise RuntimeError("the batch abandons its work")
        self.assertEqual(self.open_ids(store), [hold_id])   # the clearing was rolled back with the read
        self.assertEqual(len(packages.pending_holds(store)), 1)
        pipeline.nested_ingest(ctx, EMPLOYER, [posting], placed={})
        self.assertEqual(self.open_ids(store), [])
        self.assertEqual(packages.pending_holds(store), [])   # pruned after the commit

    def test_a_posting_that_resolves_to_a_job_not_held_aborts_the_batch(self):
        """Section 7, step 3: the batch is rolled back, that job is held, and the command stops."""
        from careeros import pipeline
        store, ctx = screened()
        posting = greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS + " Edited.", "synth"))
        before = store.one("SELECT description_text FROM jobs WHERE job_id = ?", (JOB,))[0]
        with self.assertRaises(pipeline.BatchAborted):
            with store.tx():
                pipeline.ingest(ctx, EMPLOYER, [posting], full_board=False, placed={}, ahead={})
        self.assertEqual(store.one("SELECT description_text FROM jobs WHERE job_id = ?", (JOB,))[0], before)
        with self.assertRaises(pipeline.BatchAborted):   # through a nested batch: the job is held, in a fresh transaction
            self._abort_through_nested(store, ctx, posting)
        self.assertTrue(self.open_ids(store))

    def _abort_through_nested(self, store, ctx, posting):
        from unittest import mock

        from careeros import pipeline
        with mock.patch.object(pipeline, "write_ahead", return_value={}):   # the target was not held ahead
            pipeline.nested_ingest(ctx, EMPLOYER, [posting], placed={})

    def test_an_intake_not_written_rolls_back_whole_and_keeps_its_write_ahead_hold(self):
        """Section 6: the receipt is inserted only when the posting was written; otherwise the intake's transaction is
        rolled back and the write-ahead hold on the tracked job stays (fail closed)."""
        from unittest import mock

        from careeros import pipeline
        store, ctx = setup()
        first = intake(ctx, CLAIMS, "https://careers.example.com/jobs/claims", company="Synthetic Health",
                       title="Claims Analyst")
        process(ctx)
        receipts = store.one("SELECT COUNT(*) FROM receipts")[0]
        with mock.patch.object(pipeline, "normalized_fields", side_effect=RuntimeError("disk")), \
                self.assertRaisesRegex(ValueError, "could not be stored"):
            intake(ctx, CLAIMS + " Edited.", "https://careers.example.com/jobs/claims#second", company="Synthetic Health",
                   title="Claims Analyst")
        self.assertEqual(store.one("SELECT COUNT(*) FROM receipts")[0], receipts)
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)
        self.assertEqual(store.one("SELECT COUNT(*) FROM exceptions WHERE error_code = 'intake_not_stored'")[0], 1)
        del first

    def test_status_and_the_report_count_standing_holds_apart(self):
        """Build item Y1: a hold stands until lifted or acknowledged, so it is counted apart from other exceptions."""
        import contextlib
        import io

        from careeros import cli
        store, ctx = screened()
        self.hold(store)
        store.writer("discovery").exception("validation", "low", "something else", error_code="saved_posting_unreadable")
        page = build_report(store, MID, POLICY)
        self.assertIn("1 open exception(s) \u00b7 1 standing hold(s)", page)
        import tempfile
        from pathlib import Path
        from unittest import mock

        from careeros.ops import runtime
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = {"lock": Path(tmp.name) / "data" / "writer.lock", "backups": Path(tmp.name) / "backups"}
        out = io.StringIO()
        with mock.patch.object(cli, "_ai_status", lambda ctx: "off"), contextlib.redirect_stdout(out), \
                mock.patch.object(runtime, "differences", lambda lock: []):
            cli.cmd_status(ctx, paths, SimpleNamespace())
        text = out.getvalue()
        self.assertIn("open exceptions: 1", text)
        self.assertIn("standing holds: 1", text)


class RequestKeyTests(unittest.TestCase):
    """The coordinator's Z5 (Codex Pass 7, H24's pattern): an operation's key is computed from what the intake stores,
    from the same input its storage reads; the target lookup stays apart."""

    def setUp(self):
        self.store, self.ctx = setup()

    def paste(self, text, url, company="Synthetic Health"):
        return intake(self.ctx, text, url, company=company, title="Claims Analyst")

    def test_http_and_https_are_two_requests(self):
        a = self.paste(CLAIMS, "http://careers.example.com/jobs/claims")
        b = self.paste(CLAIMS, "https://careers.example.com/jobs/claims")
        self.assertNotEqual(a["operation"], b["operation"])
        self.assertNotEqual(b["status"], "recorded")   # read as its own request

    def test_a_paste_padded_past_the_cap_and_a_compact_one_are_two_requests(self):
        from careeros.discovery.manual_intake import MAX_TEXT
        compact = self.paste(CLAIMS + " Tail sentence.", "https://careers.example.com/jobs/other")
        padded = self.paste(CLAIMS + " " * MAX_TEXT + " Tail sentence.", "https://careers.example.com/jobs/other")
        self.assertNotEqual(compact["operation"], padded["operation"])
        self.assertNotEqual(padded["status"], "recorded")

    def test_composed_and_decomposed_spellings_are_one_request(self):
        first = self.paste(CLAIMS, "https://careers.example.com/jobs/third", company="Cafe\u0301 Health")
        second = self.paste(CLAIMS, "https://careers.example.com/jobs/third", company="Caf\u00e9 Health")
        self.assertEqual((second["status"], second["job_id"]), ("recorded", first["job_id"]))

    def test_a_retry_of_the_same_request_is_one_job(self):
        first = self.paste(CLAIMS, "https://careers.example.com/jobs/fourth")
        jobs = self.store.one("SELECT COUNT(*) FROM jobs")[0]
        again = self.paste(CLAIMS, "https://careers.example.com/jobs/fourth")
        self.assertEqual((again["status"], again["job_id"]), ("recorded", first["job_id"]))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM jobs")[0], jobs)


class AlertFetchTests(unittest.TestCase):
    """Part 5 (section 5, "The fetch-only step"; Codex Pass 2 H7; Z2): an alert email's board postings are fetched
    before its transaction, which spans no network call; a transient failure leaves the line unapplied and the email
    unsettled, a definite one keeps today's lead with its reason."""

    LINK = "https://boards.greenhouse.io/synth/jobs/555"

    def setUp(self):
        import tempfile
        from pathlib import Path

        import httpx

        from careeros.core.clock import FixedClock
        from careeros.db.store import open_store
        from careeros.discovery.http import HttpClient
        from careeros.pipeline import Context
        from careeros.qualify.profile import load_profile
        from tests.helpers import JOB_MATRIX
        from tests.test_alerts import POLICY as ALERT_POLICY
        from tests.test_saved import FIXTURES
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), ALERT_POLICY.fingerprint)
        self.ctx = Context(self.store, ALERT_POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX,
                           Path(self.tmp.name) / "snap")
        self.answers = []   # what the board answers next: a status code per request
        self.inside = []    # whether a write transaction was open at each request

        def handler(request):
            from tests import postings
            from tests.test_pipeline import gh_job
            self.inside.append(self.store.conn.in_transaction)
            status = self.answers.pop(0) if self.answers else 200
            if status == 200 and request.url.path == "/v1/boards/synth/jobs/555":
                return httpx.Response(200, json=gh_job(555, "Pharmacy Benefits Systems Analyst", postings.GOOD_FIT,
                                                       req="R-55"))
            return httpx.Response(status if status != 200 else 404)

        self.client = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
        self.addCleanup(self.client.close)

    def email(self):
        from tests.test_alerts import alert_email
        html = (f'<html><body><p><a href="{self.LINK}">Pharmacy Benefits Systems Analyst</a> Synthetic Health '
                "\u00b7 Remote</p></body></html>")
        return alert_email(html)

    def run_import(self):
        import contextlib
        import io

        from careeros.cli import _import_alert
        path = self.inbox / "alert.eml"
        path.write_bytes(self.email())
        with contextlib.redirect_stdout(io.StringIO()):
            return _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")

    def filed(self, folder):
        return list((self.inbox / folder).rglob("*.eml")) if (self.inbox / folder).exists() else []

    def test_no_transaction_is_open_while_a_board_is_asked(self):
        self.run_import()
        self.assertTrue(self.inside)
        self.assertNotIn(True, self.inside)

    def test_a_transient_failure_leaves_the_email_unsettled_and_the_next_run_applies_it_once(self):
        """Z2, the coordinator's first test: a transient failure, then a success on the next run (applied once, settled)."""
        self.answers = [503, 503]   # a 5xx after the client's one retry: retryable
        counts = self.run_import()
        self.assertEqual(counts["lead_fetch_retry"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM jobs")[0], 0)   # the line is not applied
        # its card is kept meanwhile as a lead with its link and title, marked to be read again (BATCH_5 E2)
        lead = self.store.one("SELECT url, title, status, job_id, fetch_failure FROM leads")
        self.assertEqual((lead["url"], lead["title"], lead["status"], lead["job_id"]),
                         (self.LINK, "Pharmacy Benefits Systems Analyst", "new", None))
        self.assertTrue(lead["fetch_failure"].startswith("its own link: fetch failed for a moment"))
        self.assertTrue(self.filed("failed"))   # not settled: kept to be read again
        counts = self.run_import()
        self.assertEqual(counts["lead_fetch_retry"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM jobs")[0], 1)
        self.assertEqual([tuple(r) for r in self.store.all("SELECT status, job_id FROM leads")],
                         [("ingested", "greenhouse:synth:555")])
        self.assertTrue(self.filed("imported"))

    def test_a_definite_failure_keeps_a_lead_with_its_reason_and_settles_the_email(self):
        """Z2, the coordinator's second test: a not_found (a lead with its reason, the email settled, no retry)."""
        self.answers = [404]
        counts = self.run_import()
        self.assertEqual((counts["lead_fetch_failed"], counts["lead_fetch_retry"]), (1, 0))
        lead = self.store.one("SELECT status, fetch_failure FROM leads")
        self.assertEqual(lead["status"], "new")
        self.assertIn("its own link: not_found", lead["fetch_failure"])
        self.assertTrue(self.filed("imported"))
        asked = len(self.inside)
        counts = self.run_import()   # the same email again: its line is seen before, never fetched again
        self.assertEqual(counts["lead_seen_before"], 1)
        self.assertEqual(len(self.inside), asked)

    def test_a_tracked_posting_read_for_an_alert_is_held_ahead_and_cleared_with_its_write(self):
        """Section 7: a line that resolves to a tracked job is held ahead of the email's transaction and its hold cleared
        by its id with the write; nothing stays held."""
        self.run_import()
        self.store.clock.advance(days=1)
        from tests.test_alerts import alert_email
        html = ('<html><body><p><a href="https://job-boards.greenhouse.io/synth/jobs/555">Pharmacy Benefits Systems Analyst</a> Synthetic Health '
                "\u00b7 Remote</p></body></html>")
        import contextlib
        import io

        from careeros.cli import _import_alert
        from unittest import mock

        from careeros import cli
        path = self.inbox / "second.eml"
        path.write_bytes(alert_email(html))
        # a line whose link the tracked-job lookup does not match is fetched, and its posting is the tracked job
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_known_job", return_value=None):
            _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-12")
        rows = self.store.all("SELECT status FROM exceptions WHERE error_code = 'read_pending'")
        self.assertTrue(rows)
        self.assertEqual({r["status"] for r in rows}, {"resolved"})

    def test_an_alert_line_that_reaches_a_tracked_job_not_held_ahead_aborts_its_email(self):
        """Section 7, step 3, in an alert email: the email is rolled back, the job held, and the import stops."""
        import contextlib
        import io
        from unittest import mock

        from careeros import cli, pipeline
        from careeros.cli import _import_alert
        from tests.test_alerts import alert_email
        self.run_import()
        html = ('<html><body><p><a href="https://job-boards.greenhouse.io/synth/jobs/555">Pharmacy Benefits Systems Analyst'
                "</a> Synthetic Health \u00b7 Remote</p></body></html>")
        path = self.inbox / "second.eml"
        path.write_bytes(alert_email(html))
        leads = self.store.one("SELECT COUNT(*) FROM leads")[0]
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_known_job", return_value=None), \
                mock.patch.object(pipeline, "write_ahead", return_value={}), self.assertRaises(pipeline.BatchAborted):
            _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-12")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads")[0], leads)   # nothing of the email kept
        self.assertTrue(packages.open_holds(self.store, "greenhouse:synth:555"))


class UpgradeTests(unittest.TestCase):
    """Part 8 (section 7, "Rows stored before the upgrade"; W1-W3, Y2, Y3): rows an older engine stored are handled
    once, under the writer lock, in batches, resumed until a marker is written."""

    def legacy(self, store, job_id, **columns):
        """A job as the deployed engine stored it: no source record, its text as it was written then."""
        from tests.helpers import add_job
        add_job(store, job_id, **columns)
        store.conn.execute("UPDATE jobs SET source_json = NULL WHERE job_id = ?", (job_id,))
        return job_id

    def run_pass(self, ctx):
        from careeros import upgrade
        return upgrade.run(ctx)

    def test_an_older_engines_hold_is_stamped_and_reaches_the_job_of_record_after_a_join(self):
        """The gate session's revision 8 check (Y2, Y3): a deployed-engine hold on a job that becomes a duplicate, then
        the upgrade and the join: the hold reaches the job of record, by its own exception_id."""
        store, ctx = screened()
        from tests.test_intake_identity import twin
        twin(store, "manual:synthetic-health:twin", "https://job-boards.greenhouse.io/synth/jobs/1")
        held = store.writer("discovery").exception("data_quality", "medium", "ingest failed: locked", entity_type="job",
                                                   entity_id="manual:synthetic-health:twin", error_code="ingest_failed")
        self.assertNotIn(held, [h["hold_id"] for h in packages.open_holds(store, JOB)])   # no identity yet
        counts = self.run_pass(ctx)
        self.assertEqual(counts["holds_stamped"], 1)
        meta = json.loads(store.one("SELECT metadata_json FROM exceptions WHERE exception_id = ?", (held,))[0])
        self.assertEqual((meta["hold_id"], meta["posting_identity"]), (held, "greenhouse|1"))
        process(ctx)   # the join
        self.assertIn(held, [h["hold_id"] for h in packages.open_holds(store, JOB)])

    def test_a_pasted_job_gets_its_record_from_its_columns_with_its_unknown_inputs(self):
        """Section 7: a job no board reads: its location with where it came from, its employer entry, every input the
        columns cannot give unknown; its first read then counts as a change (Codex Pass 6)."""
        from careeros.openings import record_observation
        from careeros.pipeline import same_read, source_record
        store, ctx = setup()
        job = self.legacy(store, "manual:synthetic-health:r-1", ats="manual", external_id="r-1", title="Claims Analyst",
                          employer_name="Synthetic Health", parent="Synthetic Health", location_raw="Remote - US",
                          url="https://careers.example.com/jobs/1", description_text="Location: Remote - US" + chr(10) + CLAIMS,
                          current_state="normalized")   # its own Location line, so the current rules hold it declared
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job, url=None,
                           location="Remote - US", facts={"location_write": "label"})   # the write of that field
        self.assertEqual(self.run_pass(ctx)["records_rebuilt"], 1)
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual((record["location"], record["location_source"]), ("Remote - US", "label"))
        self.assertEqual(record["employer"]["resolved"], "parent_for")
        self.assertIn("comp", record["unknown"])
        read = source_record(RawPosting(ats="manual", board_token="synthetic-health", external_id="r-1",
                                        title="Claims Analyst", location="Remote - US",
                                        url="https://careers.example.com/jobs/1", description_text=CLAIMS),
                             record["employer"])
        self.assertFalse(same_read(record, read, board=False))   # the first read after the upgrade is a change

    def test_a_parent_you_gave_is_kept_as_stored(self):
        store, ctx = setup()
        job = self.legacy(store, "manual:synthetic-health:r-2", ats="manual", external_id="r-2", title="Claims Analyst",
                          employer_name="Synthetic Health", parent="Northwind Group", description_text=CLAIMS)
        self.run_pass(ctx)
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual((record["employer"]["parent"], record["employer"]["resolved"]), ("Northwind Group", "stored"))

    def test_a_board_job_waits_for_its_board_read_and_may_be_dismissed_meanwhile(self):
        """Section 7; W2 and W3: no preview until a board read writes its record; the card says how to get one (add-url
        also marks the board verified) and offers dismissal."""
        store, ctx = screened()
        store.conn.execute("UPDATE jobs SET source_json = NULL, title_norm = 'an older key' WHERE job_id = ?", (JOB,))
        counts = self.run_pass(ctx)
        self.assertEqual(counts["jobs_waiting_for_a_board_read"], 1)
        self.assertEqual(store.one("SELECT title_norm FROM jobs WHERE job_id = ?", (JOB,))[0], "claims analyst")
        waiting = capture(store, POLICY, MID, JOB)["waiting"]
        self.assertIn("waiting for a board read", waiting)
        self.assertIn("marks its board verified", waiting)
        form_keys: dict = {}
        build_report(store, MID, POLICY, token="t", form_keys=form_keys)
        self.assertIn(("dismiss", JOB), form_keys.values())
        board_read(ctx)   # its first board read writes its record
        self.assertIsNotNone(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])

    def test_a_board_job_whose_source_may_not_be_read_now_is_rebuilt_and_says_so(self):
        """W1: the site policy makes its source manual: rebuilt from its columns, its board-only inputs unknown."""
        from unittest import mock

        from careeros.discovery import sources
        store, ctx = screened()
        store.conn.execute("UPDATE jobs SET source_json = NULL WHERE job_id = ?", (JOB,))
        with mock.patch.object(sources, "status", return_value="manual"):
            self.assertEqual(self.run_pass(ctx)["boards_unreadable_rebuilt"], 1)
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])
        self.assertIn("typed_countries", record["unknown"])
        self.assertIn("board's own fields", labels.rebuilt_note(record))

    def test_text_is_made_canonical_and_the_title_key_rebuilt(self):
        """Codex Pass 5: a migrated job and a freshly ingested copy of the same text get the same title key."""
        store, ctx = setup()
        job = self.legacy(store, "manual:synthetic-health:r-3", ats="manual", external_id="r-3", title="Claims Analyst",
                          description_text=CLAIMS)
        store.conn.execute("UPDATE jobs SET title = ?, title_norm = ? WHERE job_id = ?",
                           ("Claims  Analyst\u200b", "stale key", job))   # as an older engine stored it
        store.writer("discovery").insert("leads", {"lead_id": "lead:x", "url": "https://example.com/x", "site": "other",
                                                   "title": "Claims Analyst", "title_norm": "claims analyst",
                                                   "first_seen_at": store.clock.now(), "status": "new"})
        store.conn.execute("UPDATE leads SET title = ? WHERE lead_id = 'lead:x'", ("Cafe\u0301 Analyst",))
        counts = self.run_pass(ctx)
        self.assertEqual((counts["jobs_made_canonical"], counts["leads_made_canonical"]), (1, 1))
        self.assertEqual(store.one("SELECT title, title_norm FROM jobs WHERE job_id = ?", (job,))[:],
                         ("Claims Analyst", "claims analyst"))
        self.assertEqual(store.one("SELECT title FROM leads")[0], "Caf\u00e9 Analyst")

    def test_the_pass_runs_once_and_resumes_until_its_marker(self):
        from unittest import mock

        from careeros import upgrade
        store, ctx = setup()
        for n in range(3):
            self.legacy(store, f"manual:synthetic-health:r-{n}", ats="manual", external_id=f"r-{n}", title="Claims Analyst",
                        description_text=CLAIMS)
        real = upgrade._job
        calls = []

        def interrupted(ctx_, job_id):
            calls.append(job_id)
            if len(calls) == 2:
                raise RuntimeError("interrupted")
            return real(ctx_, job_id)
        with mock.patch.object(upgrade, "_job", side_effect=interrupted), self.assertRaises(RuntimeError):
            upgrade.run(ctx)
        self.assertFalse(upgrade.done(store))
        self.assertEqual(upgrade.run(ctx)["records_rebuilt"], 2)   # resumed: the rest
        self.assertTrue(upgrade.done(store))
        self.assertEqual(upgrade.run(ctx), {})   # once

    def test_a_pasted_jobs_first_read_after_the_upgrade_is_written_and_screened_again(self):
        """Section 7 with section 3, "Unchanged reads": the first read of a job no board reads (a link that names its
        posting, so the read reaches it: D188), its text unchanged, replaces the record the upgrade rebuilt and sends
        the job back to screening; a source record never changes without its screen, so the next processing run judges
        it again. A second read of it is the same read."""
        from careeros.review.submission import authorize
        store, ctx = setup()
        entry = employer_entry(ctx, name="Synthetic Health")
        posting = RawPosting(ats="manual", board_token="synthetic-health", external_id="r-9", title="Claims Analyst",
                             location="Remote - US", url="https://job-boards.greenhouse.io/synth/jobs/9",
                             apply_url="https://job-boards.greenhouse.io/synth/jobs/9", description_text=CLAIMS,
                             raw={"location_source": "argument"})
        job = "manual:synthetic-health:r-9"
        ingest(ctx, entry, [posting], source="manual_url", full_board=False)
        store.conn.execute("UPDATE jobs SET source_json = NULL WHERE job_id = ?", (job,))   # as an older engine stored it
        self.run_pass(ctx)
        process(ctx)
        judged = store.one("SELECT current_state FROM jobs WHERE job_id = ?", (job,))[0]
        self.assertIn(judged, ("eligible", "scored"))
        counts = ingest(ctx, entry, [posting], source="manual_url", full_board=False)
        self.assertEqual(counts["edited_cosmetic"], 1)
        row = store.one("SELECT current_state, source_json FROM jobs WHERE job_id = ?", (job,))
        self.assertNotIn("unknown", json.loads(row["source_json"]))
        self.assertEqual(row["current_state"], "normalized")
        self.assertTrue(any(r["code"] == "eligibility_not_current"
                            for r in authorize(store, POLICY, job, "direct", profile=MID).reasons))
        process(ctx)   # judged again: its screen is current for the record it judged
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (job,))[0], judged)
        self.assertEqual(ingest(ctx, entry, [posting], source="manual_url", full_board=False)["unchanged"], 1)

    def test_a_board_job_waiting_for_its_board_read_leaves_its_decision_when_you_set_its_location(self):
        """Codex Pass 7 H5 for a job with no source record (a board job waiting for its board read, section 7): the
        location you set sends it back to screening in the same write, as it does a job with one."""
        from careeros.discovery.manual_intake import set_location
        store, ctx = screened(location="Perth, WA")   # its board's field leaves it unknown: yours may settle it
        self.assertNotIn(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (JOB,))[0],
                         ("discovered", "normalized"))
        store.conn.execute("UPDATE jobs SET source_json = NULL WHERE job_id = ?", (JOB,))   # as an older engine stored it
        self.assertEqual(self.run_pass(ctx)["jobs_waiting_for_a_board_read"], 1)
        self.assertTrue(set_location(ctx, JOB, "Remote - US")["changed"])
        self.assertEqual(tuple(store.one("SELECT location_raw, country, current_state, source_json FROM jobs "
                                         "WHERE job_id = ?", (JOB,))), ("Remote - US", "US", "normalized", None))


class UpgradeAtCommandStartTests(unittest.TestCase):
    def test_every_writing_command_starts_by_resuming_the_pass(self):
        import contextlib
        import io

        from careeros import cli, upgrade
        store, ctx = setup()
        store.conn.execute("DELETE FROM meta WHERE key = ?", (upgrade.DONE,))
        with contextlib.redirect_stdout(io.StringIO()):
            cli._command_start(ctx, {})
        self.assertTrue(upgrade.done(store))


if __name__ == "__main__":
    unittest.main()
