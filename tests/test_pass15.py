"""Codex Pass 15 (BLOCKERS-REMAIN on a1bff5e, code 579bd31) and the owner's D199: each finding's correction, with tests
that fail on 579bd31. Synthetic data only."""
import builtins
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from careeros.review import packages
from tests.test_batch5 import JOB, FileStore, hold_line
from tests.test_v26_discovery import MID, POLICY


def _pending(s, holds=("hold:one", "hold:two"), damaged=("not json",)) -> bytes:
    """A pending-holds file with a damaged line and these holds (all for JOB), its bytes as written."""
    s.file.write_text("".join(line + "\n" for line in damaged) + "".join(hold_line(JOB, h) + "\n" for h in holds),
                      encoding="utf-8")
    return s.file.read_bytes()


def _clear(s, hold_id):
    with s.store.tx():
        packages.clear_hold(s.store, JOB, hold_id, "a board read of the posting was stored")


def _leftovers(s):
    return sorted(p.name for p in s.file.parent.iterdir() if p.name.startswith(s.file.name) and p != s.file)


class PendingReplaceTests(unittest.TestCase):
    """H41 (High): the pending-holds file is replaced whole, never truncated in place: a stop or a full disk during a
    rewrite left an empty file or a valid prefix, and both read as clean."""

    def test_a_failure_while_writing_the_replacement_leaves_the_file_as_it_was(self):
        s = FileStore(self)
        before = _pending(s)
        _clear(s, "hold:one")
        real = builtins.open

        def disk_full(file, mode="r", *a, **kw):
            handle = real(file, mode, *a, **kw)
            if "w" in mode and str(file).startswith(str(s.file)):
                handle.close()   # opened, so truncated, then the disk is full at the first write
                raise OSError(28, "No space left on device")
            return handle
        with mock.patch.object(builtins, "open", disk_full), mock.patch.object(io, "open", disk_full):
            with self.assertRaises(OSError):
                packages.prune_pending_holds(s.store)
        self.assertEqual(s.file.read_bytes(), before)   # every hold and the damaged line
        self.assertEqual(_leftovers(s), [])
        self.assertEqual({h["hold_id"] for h in packages.pending_holds(s.store)}, {"hold:one", "hold:two"})
        self.assertTrue(packages.pending_file_problem(s.store).startswith("holds_file_damaged:"))

    def test_a_failure_at_the_replace_leaves_the_file_as_it_was(self):
        """A Windows sharing error at os.replace: the original stays, the replacement goes."""
        s = FileStore(self)
        before = _pending(s)
        _clear(s, "hold:one")
        with mock.patch.object(os, "replace", side_effect=PermissionError(32, "The process cannot access the file")):
            with self.assertRaises(PermissionError):
                packages.prune_pending_holds(s.store)
        self.assertEqual(s.file.read_bytes(), before)
        self.assertEqual(_leftovers(s), [])
        # the next try prunes it
        self.assertEqual(packages.prune_pending_holds(s.store), 1)
        self.assertEqual(s.file.read_text(encoding="utf-8"), "not json\n" + hold_line(JOB, "hold:two") + "\n")

    def test_the_replacement_is_synced_before_it_replaces_the_file(self):
        s = FileStore(self)
        _pending(s)
        _clear(s, "hold:one")
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(os, "replace", side_effect=lambda a, b: (calls.append(("replace", Path(b).name)),
                                                                           real_replace(a, b))[1]):
            self.assertEqual(packages.prune_pending_holds(s.store), 1)
        self.assertEqual(calls, ["fsync", ("replace", s.file.name)])

    def test_a_record_at_the_command_start_that_cannot_replace_the_file_records_each_hold_once(self):
        """The rows land, the rewrite fails: the next start finds each line's row and writes none twice."""
        s = FileStore(self)
        _pending(s, holds=("hold:one",))
        with mock.patch.object(os, "replace", side_effect=PermissionError(32, "The process cannot access the file")):
            with self.assertRaises(PermissionError):
                packages.record_pending_holds(s.store)
        self.assertEqual(len(packages.pending_holds(s.store)), 1)   # the line is still there
        self.assertEqual(packages.record_pending_holds(s.store), 0)   # its row landed: pruned, never recorded twice
        self.assertEqual(s.store.one("SELECT COUNT(*) AS n FROM exceptions WHERE json_extract(metadata_json, "
                                     "'$.hold_id') = 'hold:one'")["n"], 1)
        self.assertEqual(s.file.read_text(encoding="utf-8"), "not json\n")

    def test_a_successful_prune_keeps_exactly_the_uncleared_lines(self):
        s = FileStore(self)
        _pending(s, holds=("hold:one", "hold:two", "hold:three"), damaged=("not json", "{\"torn"))
        _clear(s, "hold:two")
        self.assertEqual(packages.prune_pending_holds(s.store), 1)
        self.assertEqual(s.file.read_text(encoding="utf-8"),
                         "not json\n{\"torn\n" + hold_line(JOB, "hold:one") + "\n" + hold_line(JOB, "hold:three") + "\n")
        self.assertEqual(_leftovers(s), [])

    def test_a_replacement_left_behind_changes_nothing(self):
        s = FileStore(self)
        before = _pending(s, holds=("hold:one",), damaged=())
        stale = s.file.with_name(s.file.name + packages.REPLACEMENT + "0" * 32)
        stale.write_text(hold_line(JOB, "hold:stale") + "\n{\n", encoding="utf-8")   # a stop before the replace
        self.assertEqual([h["hold_id"] for h in packages.pending_holds(s.store)], ["hold:one"])
        self.assertIsNone(packages.pending_file_problem(s.store))
        self.assertEqual(packages.record_pending_holds(s.store), 1)
        self.assertFalse(s.file.exists())   # every hold recorded, no damaged line: the file goes
        self.assertTrue(stale.exists())     # never read, never counted
        self.assertNotEqual(before, b"")


class _Reader:
    """Path.read_text for the pending-holds file, counted: the first `good` readings succeed, then each fails with a
    sharing error (None: every reading succeeds)."""

    def __init__(self, good=None):
        self.good, self.count = good, 0
        real = Path.read_text

        def read(path, *a, **kw):   # a plain function: patched onto Path, it is bound as a method
            if str(path).endswith(".pending-holds.jsonl"):
                self.count += 1
                if self.good is not None and self.count > self.good:
                    raise PermissionError(32, "The process cannot access the file")
            return real(path, *a, **kw)
        self.read = read


class PendingReadingTests(unittest.TestCase):
    """H40 (High): P1 read the pending-holds file once for its health and again for its holds; an error on the second
    reading counted as no holds, so a label could route while a hold kept beside the database reached its job."""

    def setUp(self):
        from careeros.review.labels import capture
        self.s = FileStore(self)   # JOB read, screened and labelled Apply from a preview that showed no hold
        self.capture = lambda: capture(self.s.store, POLICY, MID, JOB)
        self.s.file.write_text(hold_line(JOB, "hold:later") + "\n", encoding="utf-8")   # a read failed since

    def test_a_reading_that_fails_after_the_health_check_never_lets_the_label_route(self):
        reader = _Reader(good=1)
        with mock.patch.object(Path, "read_text", reader.read):
            route, why = self.s.route()
        self.assertIsNone(route)
        self.assertTrue(why.startswith("posting_not_stored:"), why)   # the hold, from the one reading
        self.assertEqual(reader.count, 1)

    def test_a_decision_reads_the_file_once(self):
        """So a change between two readings cannot split the file's health from the holds it keeps."""
        for decide in (self.s.route, self.capture):
            reader = _Reader()
            with mock.patch.object(Path, "read_text", reader.read):
                decide()
            self.assertEqual(reader.count, 1, decide)

    def test_a_capture_that_cannot_read_the_file_offers_no_preview_and_says_why(self):
        with mock.patch.object(Path, "read_text", _Reader(good=0).read):
            captured = self.capture()
            route, why = self.s.route()
        self.assertIsNone(captured["digest"])
        self.assertTrue(captured["waiting"].startswith("holds_file_unreadable:"), captured["waiting"])
        self.assertIsNone(route)
        self.assertTrue(why.startswith("holds_file_unreadable:"), why)
        captured = self.capture()   # read again: the hold is shown, and a label from this preview acknowledges it
        self.assertEqual([h["hold_id"] for h in captured["holds_shown"]], ["hold:later"])

    def test_a_damaged_file_offers_no_preview(self):
        self.s.file.write_text("{\n" + hold_line(JOB, "hold:later") + "\n", encoding="utf-8")
        captured = self.capture()
        self.assertTrue(captured["waiting"].startswith("holds_file_damaged:"), captured["waiting"])

    def surfaces(self):
        """(the CLI preview, the review page's card) of JOB, as they are now."""
        from careeros.review.labels import preview_lines
        from careeros.review.report import build_report
        cli = "\n".join(preview_lines(self.capture(), self.s.store))
        page = build_report(self.s.store, MID, POLICY, token="t", form_keys={})
        start = page.index(f'id="{JOB}"')
        return cli, page[start:page.index("</form>", start)]

    def test_every_surface_says_why_there_is_no_preview_and_what_to_do(self):
        """The coordinator's conditions for this reading (2026-11-25): the reason and what to do, never a generic
        "waiting for screening"."""
        with mock.patch.object(Path, "read_text", _Reader(good=0).read):
            unreadable = self.surfaces()
        self.s.file.write_text("{\n" + hold_line(JOB, "hold:later") + "\n", encoding="utf-8")
        damaged = self.surfaces()
        for surfaces, reason, advice in ((unreadable, "holds_file_unreadable", "close any program that has it open"),
                                         (damaged, "holds_file_damaged", "check each one, then delete it from the file")):
            for surface in surfaces:
                self.assertIn(reason, surface)
                self.assertIn(advice, surface)
                self.assertIn(str(self.s.file), surface)
                self.assertNotIn("waiting for screening", surface)
        self.assertNotIn('value="apply"', damaged[1])   # the card offers no Apply or Stretch
        self.assertIn('value="skip"', damaged[1])

    def test_skip_and_dismissal_still_work_while_the_file_cannot_be_read(self):
        from careeros.review import dismiss
        from careeros.review.labels import record_label
        with mock.patch.object(Path, "read_text", _Reader(good=0).read):
            self.assertEqual(record_label(self.s.store, JOB, "skip", None, "skip-while-unreadable"), "new")
            dismiss.dismiss(self.s.store, JOB, "location", None, key="dismiss-while-unreadable")
        self.assertEqual(self.s.store.one("SELECT label FROM labels WHERE job_id = ? ORDER BY at DESC, rowid DESC",
                                          (JOB,))["label"], "skip")
        self.assertTrue(dismiss.is_dismissed(self.s.store, JOB))

    def test_a_fresh_preview_is_offered_once_the_file_reads_again(self):
        from careeros.review.labels import record_label, write_preview
        with mock.patch.object(Path, "read_text", _Reader(good=0).read):
            self.assertIsNotNone(self.capture()["waiting"])
        captured = self.capture()
        self.assertIsNone(captured["waiting"])
        self.assertIsNotNone(captured["digest"])
        self.assertEqual([h["hold_id"] for h in captured["holds_shown"]], ["hold:later"])
        self.s.store.clock.advance(minutes=1)
        preview = write_preview(self.s.store, captured)
        self.assertEqual(record_label(self.s.store, JOB, "apply", None, "apply-again", preview=preview, policy=POLICY,
                                      applicant=MID), "new")
        self.assertEqual(self.s.route()[0], "apply")   # the label acknowledges the hold its preview showed

    def test_only_a_file_confirmed_missing_holds_nothing(self):
        """A check that says no file while the file is there (Path.exists swallows an error) is not a missing file."""
        with mock.patch.object(Path, "exists", lambda path, *a, **kw: False):
            route, why = self.s.route()
            captured = self.capture()
        self.assertIsNone(route)
        self.assertTrue(why.startswith("posting_not_stored:"), why)
        self.assertEqual([h["hold_id"] for h in captured["holds_shown"]], ["hold:later"])
        self.s.file.unlink()
        self.assertEqual(self.s.route()[0], "apply")   # gone: nothing held

    def test_an_error_that_is_not_a_missing_file_makes_it_unreadable(self):
        real = Path.read_text

        def is_a_folder(path, *a, **kw):
            if str(path).endswith(".pending-holds.jsonl"):
                raise IsADirectoryError(21, "Is a directory")
            return real(path, *a, **kw)
        with mock.patch.object(Path, "read_text", is_a_folder):
            snapshot = packages.pending_snapshot(self.s.store)
            route, why = self.s.route()
        self.assertEqual(snapshot[0], [])
        self.assertTrue(snapshot[2].startswith("IsADirectoryError"), snapshot)
        self.assertIsNone(route)
        self.assertTrue(why.startswith("holds_file_unreadable:"), why)


class BoardClearanceTests(unittest.TestCase):
    """H39 (High): a board read cleared every hold that reaches its job (open_holds), Z8's every-job holds included,
    though it says nothing of the failed paste behind one: a routine scan of any posting removed the warning before you
    saw it. A board read now clears a hold only on a positive match with its own posting."""

    def setUp(self):
        from careeros import pipeline
        from tests.test_v26_discovery import setup
        self.store, self.ctx = setup()
        self.pipeline = pipeline
        self.read(1)
        self.read(2, title="Claims Specialist")
        pipeline.process(self.ctx)
        from tests.helpers import give_label
        self.give = lambda job: give_label(self.store, POLICY, MID, job, "apply")
        self.give(JOB)
        self.give("greenhouse:synth:2")

    def read(self, n, text=None, title="Claims Analyst", **kw):
        from careeros.discovery.boards import greenhouse_posting
        from tests.test_batch5 import EMPLOYER
        from tests.test_packages import FULL_TIME
        from tests.test_v26_discovery import gh_item
        kw.setdefault("full_board", False)
        self.store.clock.advance(minutes=1)
        return self.pipeline.ingest(self.ctx, EMPLOYER, [greenhouse_posting("synth", gh_item(n, title, text or FULL_TIME,
                                                                                         "synth"))], **kw)

    def hold(self, entity_id):
        return self.store.writer("discovery").exception("data_quality", "medium", "ingest failed: disk full",
                                                        entity_type="job", entity_id=entity_id, error_code="ingest_failed")

    def status(self, hold_id):
        return self.store.one("SELECT status FROM exceptions WHERE exception_id = ?", (hold_id,))["status"]

    def every_job_hold(self):
        """An older engine's failed paste, on a generated id that names no posting and no job (Z8)."""
        from careeros import upgrade
        held = self.hold("manual:synthetic-health:r-404")
        self.assertEqual(upgrade.run(self.ctx)["holds_reaching_every_job"], 1)
        return held

    def test_the_same_every_job_hold_survives_board_reads_scans_and_refreshes_of_other_postings(self):
        from tests.test_packages import FULL_TIME
        held = self.every_job_hold()
        self.read(1)                                                  # an unchanged board read
        self.read(1, text=FULL_TIME + "\nBenefits start on day one.")   # a changed one, written
        self.read(1, full_board=True)                                 # a complete scan of the board
        self.read(2, title="Claims Specialist", source="manual_url")  # the shortlist refresh's single-posting read
        self.pipeline.process(self.ctx)                               # the processing run
        self.assertEqual(self.status(held), "open")
        for job in (JOB, "greenhouse:synth:2"):
            self.assertIn(held, [h["hold_id"] for h in packages.open_holds(self.store, job)])
            self.assertNotIn(held, [h["hold_id"] for h in packages.posting_holds(self.store, job)])
        self.assertEqual([r["exception_id"] for r in packages.every_job_holds(self.store)], [held])

    def test_p1_still_refuses_every_label_that_did_not_acknowledge_it(self):
        held = self.every_job_hold()
        self.read(1, full_board=True)
        self.read(2, title="Claims Specialist", full_board=True)
        self.pipeline.process(self.ctx)
        for job in (JOB, "greenhouse:synth:2"):
            route, why = packages.route_for(self.store, POLICY, job, MID)
            self.assertIsNone(route, job)
            self.assertTrue(why.startswith("posting_not_stored:"), why)
        self.give(JOB)   # from a preview that shows it: acknowledged for this label only (H25)
        self.assertEqual(packages.route_for(self.store, POLICY, JOB, MID)[0], "apply")
        self.assertIsNone(packages.route_for(self.store, POLICY, "greenhouse:synth:2", MID)[0])
        self.read(1, full_board=True)
        self.assertEqual(self.status(held), "open")

    def test_a_job_with_exactly_its_id_is_the_only_one_it_reaches_and_another_read_leaves_it(self):
        from tests.helpers import add_job
        held = self.every_job_hold()
        add_job(self.store, "manual:synthetic-health:r-404", ats="manual", external_id="r-404")
        self.assertNotIn(held, [h["hold_id"] for h in packages.open_holds(self.store, JOB)])
        self.assertEqual([h["hold_id"] for h in packages.posting_holds(self.store, "manual:synthetic-health:r-404")],
                         [held])
        self.read(1, full_board=True)
        self.assertEqual(self.status(held), "open")

    def test_a_legacy_board_hold_is_cleared_only_by_a_read_of_the_posting_its_id_names(self):
        """B1's board form: the upgrade reads the posting from the id ("greenhouse:synth:7" names posting 7)."""
        from careeros import upgrade
        held = self.hold("greenhouse:synth:7")
        upgrade.run(self.ctx)
        self.read(1, full_board=True)
        self.read(2, title="Claims Specialist")
        self.assertEqual(self.status(held), "open")
        self.read(7, title="Claims Coordinator")   # its own posting: its job is created, and its read stored
        self.assertEqual(self.status(held), "resolved")

    def test_a_file_line_is_cleared_only_by_a_read_of_its_own_posting(self):
        """A hold kept beside the database (a store in memory keeps it in this process)."""
        mine = packages.add_pending_hold(self.store, JOB, ("greenhouse", "1"), "ingest failed: disk full")
        other = packages.add_pending_hold(self.store, "greenhouse:synth:9", ("greenhouse", "9"), "ingest failed: disk full")
        self.read(2, title="Claims Specialist", full_board=True)
        self.assertEqual(packages.cleared_holds(self.store), set())
        self.read(1, text=None)
        self.assertEqual(packages.cleared_holds(self.store), {mine})
        self.assertNotIn(other, packages.cleared_holds(self.store))


# two pay units CareerOS does not understand, the same for their first 40 characters (Codex's were 48 and 70)
UNIT_A = "per shift on site, weekdays, paid in arrears each month"
UNIT_B = UNIT_A + ", evening coverage paid at time and a half"


class StatedUnitTests(unittest.TestCase):
    """H38 (High): the saved-page reader kept 40 characters of a pay unit it does not understand, so two pages stating
    different pay terms gave one source record, one card and one digest. A value any reader keeps shorter than stated
    is now kept whole or marked cut."""

    def read(self, unit, value=None):
        """(the source record, the CLI preview) of a saved page with this pay unit, in a store of its own."""
        from careeros import pipeline
        from careeros.discovery.saved import parse_saved_html
        from careeros.review.labels import capture, preview_lines
        from tests.test_batch5 import ld_page, pay
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        raw, employer = parse_saved_html(ld_page(pay(unit, **(value or {"minValue": 40, "maxValue": 50}))), "page.html")
        entry = pipeline.employer_entry(ctx, name=employer)
        pipeline.ingest(ctx, entry, [raw], source="manual_url", full_board=False)
        pipeline.process(ctx)
        job = store.one("SELECT job_id, source_json FROM jobs")
        return job["source_json"], "\n".join(preview_lines(capture(store, POLICY, MID, job["job_id"]), store))

    def test_a_unit_not_understood_is_kept_whole(self):
        from careeros.discovery.saved import _salary
        from tests.test_batch5 import pay
        self.assertEqual(UNIT_A[:40], UNIT_B[:40])
        for unit in (UNIT_A, UNIT_B, "per shift"):
            with self.subTest(unit=unit):
                self.assertEqual(_salary({"baseSalary": pay(unit, minValue=40, maxValue=50)})["period_stated"], unit)

    def test_every_unit_gives_its_own_record_and_card_but_two_forms_of_one_unit(self):
        units = {"short": "per shift", "long": UNIT_A, "longer": UNIT_B, "absent": None, "hourly": "HOURLY",
                 "per hour": "PER_HOUR", "monthly": "MONTHLY"}
        read = {name: self.read(unit) for name, unit in units.items()}
        self.assertIn(f'period stated "{UNIT_B}"', read["longer"][1])   # the card says it whole
        self.assertIn(f'period stated "{UNIT_A}"', read["long"][1])
        for a in units:
            for b in units:
                if a < b:
                    with self.subTest(pair=(a, b)):
                        same = {a, b} == {"hourly", "per hour"}   # two spellings of one unit: one read
                        self.assertEqual(read[a][0] == read[b][0], same)
                        self.assertEqual(read[a][1] == read[b][1], same)

    def test_a_pasted_location_longer_than_its_limit_is_marked_cut(self):
        from careeros.discovery.manual_intake import MAX_LOCATION, parse_pasted
        from tests.test_packages import FULL_TIME
        places = "; ".join(f"Office {n}, Springfield, IL" for n in range(20))
        parsed = parse_pasted(f"Job Title: Claims Analyst\nCompany: Foo Health\nLocation: {places}\n\n" + FULL_TIME)
        self.assertGreater(len(places), MAX_LOCATION)
        self.assertEqual(len(parsed["location"]), MAX_LOCATION)
        self.assertEqual(parsed["cut"], ["location"])
        short = parse_pasted("Job Title: Claims Analyst\nCompany: Foo Health\nLocation: Austin, TX\n\n" + FULL_TIME)
        self.assertEqual(short["cut"], [])

    def test_a_pasted_text_longer_than_its_limit_is_marked_cut(self):
        from careeros.discovery.manual_intake import MAX_TEXT, parse_pasted
        from tests.test_packages import FULL_TIME
        head = "Job Title: Claims Analyst\nCompany: Foo Health\n\n" + FULL_TIME + "\n"
        padded = head + "x" * (MAX_TEXT - len(head))
        self.assertEqual(parse_pasted(padded)["cut"], [])
        self.assertEqual(parse_pasted(padded + " \n  \n")["cut"], [])   # only blank space past the limit: nothing lost
        self.assertEqual(parse_pasted(padded + "\nTravel is 50% of the role.")["cut"], ["text"])


def _leaves(value) -> list[str]:
    """Every text and number a value holds, as the card must show it (a number exactly as stored)."""
    if isinstance(value, dict):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    if isinstance(value, float):
        return [f"{value:,}"]
    if isinstance(value, str):
        return [line for line in value.split("\n") if line.strip()]
    return []


class FullCardTests(unittest.TestCase):
    """H37 (High): the card left out the posting's text, its department, its typed countries and other source fields,
    cut a differing copy's text at 400 characters and other fields at 160, and rounded pay to two decimals: two postings
    differing by one sentence had identical previews and different digests. Every label surface now shows everything
    the approval covers, from one renderer."""

    def setUp(self):
        from tests.test_p1 import screened
        from tests.test_packages import FULL_TIME
        self.full_time = FULL_TIME
        self.store, self.ctx = screened(FULL_TIME)

    def surfaces(self, captured, store=None):
        """(the CLI preview, the review page's card as text, the card's HTML) of a capture."""
        import html
        import re

        from careeros.review import labels
        from careeros.review.report import build_report
        store = store or self.store
        cli = "\n".join(labels.preview_lines(captured, store, store.clock.now()))
        with mock.patch.object(labels, "capture", return_value=captured):
            page = build_report(store, MID, POLICY, token="t", form_keys={})
        start = page.index(f'id="{captured["job_id"]}"')
        card = page[start:page.index("</form>", page.index("approves", start))]
        return cli, html.unescape(re.sub(r"<[^>]+>", " ", card)), card

    def test_every_key_of_the_source_record_is_on_every_label_surface(self):
        """Structural: the record's keys are the source-record test's list (every RawPosting field but the declared
        bookkeeping, the raw inputs, the employer entry), the marks, the bindings and the rebuild's unknown list, and a
        key no renderer names; each is set to a value of its own, and each value must be on both surfaces."""
        import json
        from dataclasses import fields

        from careeros import pipeline
        from careeros.normalize.posting import RawPosting
        from careeros.review.labels import SHOWN_DERIVED, capture
        keys = ({f.name for f in fields(RawPosting)} - set(pipeline.SOURCE_BOOKKEEPING)) | set(pipeline.RAW_INPUTS) \
            | {"employer", *pipeline.MARKS, "bindings", "unknown", "a_field_added_later"}
        lists = {"typed_countries", "inferred", "cut", "unknown"}
        record = {key: [f"value-of-{key}"] if key in lists else f"value-of-{key}" for key in keys}
        record.update(
            comp={"min": 24.125, "max": 31.0625, "period": "value-of-comp-period", "currency": "value-of-comp-currency",
                  "period_stated": "value-of-comp-period_stated", "inferred": ["value-of-comp-inferred"],
                  "a_pay_field_added_later": "value-of-comp-a_pay_field_added_later"},
            employer={k: f"value-of-employer-{k}" for k in pipeline.ENTRY_KEYS},
            bindings={"location": "value-of-bindings-location"},
            description_text="value-of-description_text, its first line\n\nits last line: <b>Tuesday</b> & more")
        captured = capture(self.store, POLICY, MID, JOB)
        parts = json.loads(json.dumps(captured["parts"]))
        parts["source"] = pipeline.source_json(record)
        parts["derived"] = {k: f"value-of-{k}" for k in SHOWN_DERIVED} | {
            "comp_min": 1234.5678, "comp_max": 98765.4321, "remote_states_json": '["value-of-remote_states_json"]',
            "remote_excluded_states_json": '["value-of-remote_excluded_states_json"]'}
        parts["screening"] = {"decision": "value-of-decision",
                              "hard_failures": [{"code": "value-of-fail-code", "detail": "value-of-fail-detail",
                                                 "source_text": "value-of-fail-source_text",
                                                 "profile_evidence": "value-of-fail-profile_evidence",
                                                 "confidence": 0.875, "strength": "value-of-fail-strength"}],
                              "flags": [{"code": "value-of-flag-code", "detail": "value-of-flag-detail",
                                         "source_text": "value-of-flag-source_text"}]}
        captured = {**captured, "parts": parts}
        cli, card, raw = self.surfaces(captured)
        expected = _leaves(record) + _leaves({k: v for k, v in parts["derived"].items() if not k.endswith("_json")}) \
            + ["value-of-remote_states_json", "value-of-remote_excluded_states_json"] + _leaves(parts["screening"])
        for value in expected:
            with self.subTest(value=value):
                self.assertIn(value, cli)
                self.assertIn(value, card)
        self.assertIn("&lt;b&gt;Tuesday&lt;/b&gt; &amp; more", raw)   # in the page whole, escaped, in a fold
        self.assertIn("<details>", raw)
        self.assertIn("    its last line: <b>Tuesday</b> & more", cli)   # the CLI prints it whole

    def test_two_postings_that_differ_by_one_sentence_have_two_cards(self):
        """Codex's pair: their CLI previews were identical, their digests different."""
        from careeros.review.labels import capture
        from tests.test_p1 import screened
        seen = {}
        for day in ("Tuesday", "Thursday"):
            store, _ = screened(self.full_time + f"\nTeam meetings are on {day} afternoons.")
            captured = capture(store, POLICY, MID, JOB)
            cli, card, _raw = self.surfaces(captured, store)
            self.assertIn(f"Team meetings are on {day} afternoons.", cli)
            self.assertIn(f"Team meetings are on {day} afternoons.", card)
            seen[day] = (cli, card, captured["digest"])
        for n in range(3):
            self.assertNotEqual(seen["Tuesday"][n], seen["Thursday"][n])

    def test_a_long_differing_copy_is_shown_whole(self):
        import contextlib

        from careeros import pipeline
        from careeros.discovery.manual_intake import intake
        from careeros.review.labels import capture
        tail = " ".join(f"Point {n} of the onboarding plan is covered in week {n}." for n in range(1, 40))
        text = ("Claims Analyst\nCompany: Synthetic Health\n\n" + self.full_time + "\n" + tail
                + "\nOn-call weekends are required twice a month.")
        with contextlib.redirect_stdout(io.StringIO()):
            intake(self.ctx, text, "https://job-boards.greenhouse.io/synth/jobs/1")
        pipeline.process(self.ctx)
        cli, card, _raw = self.surfaces(capture(self.store, POLICY, MID, JOB))
        differs = cli.split("also: its text, where it differs: ", 1)[1].split("\n", 1)[0]
        self.assertGreater(len(differs), 400)
        self.assertTrue(differs.endswith("On-call weekends are required twice a month."), differs[-80:])
        for line in (tail, "On-call weekends are required twice a month."):
            self.assertIn("    " + line + "\n", cli + "\n")   # the copy's text, whole
            self.assertIn(line, card)

    def test_every_field_a_copy_states_otherwise_is_shown_whole(self):
        """A copy whose department, typed countries and workplace differ, each longer than the 160 characters the card
        kept, and whose text differs past 400."""
        import json

        from careeros import pipeline
        from careeros.openings import record_observation
        from careeros.review.labels import capture
        record = json.loads(self.store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))[0])
        copy = {**record, "department": "Claims Operations, " + "Pharmacy Benefit Reconciliation Team, " * 6 + "East",
                "typed_countries": [f"country {n} of a long list of the places it names" for n in range(6)],
                "workplace_hint": "hybrid: " + "two days a week in the office, " * 7 + "the rest remote",
                "description_text": record["description_text"] + "\n" + "Shifts rotate weekly. " * 30 + "Weekends too.",
                # another registry entry of the same name: the card's employer fact is the same, the entry is not
                "employer": {**record["employer"], "employer_id": "emp:another-entry-of-the-same-name"}}
        with self.store.tx():
            record_observation(self.store, source_kind="ats_scan", evidence_level="live_ats", job_id=JOB, url=record["url"],
                               facts={**pipeline.sighting_facts(copy)})
        cli, card, _raw = self.surfaces(capture(self.store, POLICY, MID, JOB))
        for name, value in (("department", copy["department"]), ("workplace, as stated", copy["workplace_hint"]),
                            ("countries its board types", ", ".join(copy["typed_countries"]))):
            self.assertGreater(len(value), 160)
            line = f"also: {name}: {value} (this copy) / "
            self.assertIn(line, cli)
            self.assertIn(line, card)
        self.assertIn("Weekends too.", cli.split("also: its text, where it differs: ", 1)[1].split("\n", 1)[0])
        line = "also: employer employer id: emp:another-entry-of-the-same-name (this copy) / "
        self.assertIn(line, cli)
        self.assertIn(line, card)

    def test_pay_is_shown_exactly_as_stored(self):
        import json

        from careeros.review.labels import capture, pay_line
        from careeros.review.report import _comp
        derived = {"comp_min": 24.125, "comp_max": 30.0, "comp_period": "hour", "comp_currency": "USD"}
        self.assertEqual(pay_line(derived), "pay: minimum 24.125 · maximum 30 · per hour · USD")
        self.assertEqual(_comp(derived), "USD 24.125–30 per hour")
        captured = capture(self.store, POLICY, MID, JOB)
        parts = json.loads(json.dumps(captured["parts"]))
        parts["derived"].update(derived)
        cli, card, _raw = self.surfaces({**captured, "parts": parts})
        for surface in (cli, card):
            self.assertIn("minimum 24.125 · maximum 30 · per hour · USD", surface)
            self.assertNotIn("24.12 ", surface)


if __name__ == "__main__":
    unittest.main()
