"""Codex Pass 16 (BLOCKERS-REMAIN on 2959575, code 2be781c) and the owner's D200: each finding's correction, with tests
that fail on 2959575. Synthetic data only."""
import dataclasses
import json
import unittest
from unittest import mock

from tests.test_v26_discovery import MID, POLICY

JOB = "greenhouse:synth:1"
# the posting's text without its pay sentence: what CareerOS reads as pay comes from the board's own pay fields only
TEXT = """About us
Foo Health is a health plan serving members across the US. Learn more at foohealth.com.

Responsibilities:
- Review claims adjudication outcomes in RxClaim and reconcile payer files
- Investigate eligibility and enrollment discrepancies for members

Requirements:
- 3+ years of claims experience required
- Excel skills required

This role is remote in the US."""
BOARD_PAY = {"min": 80000, "max": 95000, "period": "year", "currency": "USD"}
REGISTRY = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
            "ats": "greenhouse", "board_token": "synth", "status": "verified"}


def board_job(comp=None, text=TEXT):
    """(store, ctx): the board posting read from its registry entry with these pay fields, and screened."""
    from careeros.discovery.boards import greenhouse_posting
    from careeros.pipeline import employer_entry, ingest, process
    from tests.test_v26_discovery import gh_item, setup
    store, ctx = setup()
    store.writer("admin").insert("employers", REGISTRY)
    raw = dataclasses.replace(greenhouse_posting("synth", gh_item(1, "Claims Analyst", text, "synth")),
                              comp=dict(BOARD_PAY if comp is None else comp))
    ingest(ctx, employer_entry(ctx, row=store.one("SELECT * FROM employers")), [raw], full_board=False)
    process(ctx)
    return store, ctx


def surfaces(store, captured):
    """(the CLI preview, the review page's card as text) of a capture."""
    import html
    import re

    from careeros.review import labels
    from careeros.review.report import build_report
    cli = "\n".join(labels.preview_lines(captured, store, store.clock.now()))
    with mock.patch.object(labels, "capture", return_value=captured):
        page = build_report(store, MID, POLICY, token="t", form_keys={})
    start = page.index(f'id="{captured["job_id"]}"')
    card = page[start:page.index("</form>", page.index("approves", start))]
    return cli, html.unescape(re.sub(r"<[^>]+>", " ", card))


def record_of(store) -> dict:
    return json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (JOB,))["source_json"])


def board_copy(store, copy):
    """A board's read of the posting that states `copy` (a sighting: never written over the job of record)."""
    from careeros import pipeline
    from careeros.openings import record_observation
    with store.tx():
        record_observation(store, source_kind="ats_scan", evidence_level="live_ats", job_id=JOB, url=copy["url"],
                           facts={**pipeline.sighting_facts(copy)})


class CopyLeafTests(unittest.TestCase):
    """H42 (High): a differing copy's pay and employer entry were left to their summary lines once those differed, and
    those lines are readings: pay read as "not stated" hid a stored 0-0 per hour, and the entry's name hid its id and how
    it was resolved. Every stored leaf of a differing copy is now shown with both values whole, and every pay line that
    says "not stated" says what is stored (the owner's D200: 0-0 still reads as no pay given)."""

    def test_codex_fixture_a_saved_copy_storing_zero_pay_with_another_employer(self):
        from careeros.pipeline import employer_entry, ingest
        from careeros.review.labels import capture, card_items
        from tests.test_p1 import copy_of
        store, ctx = board_job()
        saved = dataclasses.replace(copy_of(TEXT), comp={"min": 0, "max": 0, "period": "hour", "currency": "USD"})
        ingest(ctx, employer_entry(ctx, name="Synthetic Health Partners"), [saved], source="manual_import",
               full_board=False)
        captured = capture(store, POLICY, MID, JOB)
        self.assertIsNone(captured["waiting"])
        stored = "minimum 0 · maximum 0 · period hour · currency USD"
        expected = [
            # the summary line stays, and says what is stored beside its reading
            f"pay: not stated (stored: {stored}; CareerOS reads 0–0 as no pay given) (this copy) / minimum 80,000 · "
            "maximum 95,000 · per year · USD (the job of record)",
            f"also: pay, as stored: {stored} (this copy) / minimum 80,000 · maximum 95,000 · period year · currency "
            "USD (the job of record)",
            "also: employer employer id: not stated (this copy) / greenhouse:synth (the job of record)",
            "also: employer resolved by: parent_for (this copy) / registry (the job of record)",
            "also: employer name: Synthetic Health Partners (this copy) / Synthetic Health (the job of record)",
            # the location line names its source in words: the stored source is shown too
            "also: location from: file (this copy) / not stated (the job of record)"]
        cli, card = surfaces(store, captured)
        for line in expected:
            with self.subTest(line=line):
                self.assertIn(line, cli)
                self.assertIn(line, card)
        # the digest binds them as the card shows them: the card's lines come from the captured parts alone
        alone = "\n".join(item["text"] for item in card_items(json.loads(json.dumps(captured))))
        for line in expected:
            self.assertIn(line, alone)

    def test_a_differing_employer_id_together_with_a_differing_name(self):
        from careeros.review.labels import capture
        store, _ = board_job()
        record = record_of(store)
        board_copy(store, {**record, "employer": {**record["employer"], "employer_id": "greenhouse:synth-partners",
                                                  "name": "Synthetic Health Partners"}})
        cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
        for line in ("employer: Synthetic Health Partners (part of Synthetic Health) (this copy) / Synthetic Health "
                     "(the job of record)",
                     "also: employer employer id: greenhouse:synth-partners (this copy) / greenhouse:synth (the job of record)",
                     "also: employer name: Synthetic Health Partners (this copy) / Synthetic Health (the job of record)"):
            with self.subTest(line=line):
                self.assertIn(line, cli)
                self.assertIn(line, card)

    def test_a_copy_whose_values_cannot_be_read_shows_each_field_it_states_otherwise(self):
        """No card fact is compared for such a copy, so no field is left to one: each is on its own line."""
        from careeros import pipeline
        from careeros.review.labels import capture
        store, _ = board_job()
        record = record_of(store)
        board_copy(store, {**record, "title": "Senior Claims Analyst", "requisition_id": "R-77"})
        real = pipeline.derive

        def unreadable(ctx, source, job_id):
            if source.get("title") == "Senior Claims Analyst":
                raise ValueError("a copy CareerOS cannot read")
            return real(ctx, source, job_id)
        with mock.patch.object(pipeline, "derive", unreadable):
            cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
        for line in ("also: title: Senior Claims Analyst (this copy) / Claims Analyst (the job of record)",
                     "also: requisition: R-77 (this copy) / not stated (the job of record)"):
            with self.subTest(line=line):
                self.assertIn(line, cli)
                self.assertIn(line, card)

    def test_every_leaf_is_shown_when_it_changes_with_its_summary_line(self):
        """The guard: each leaf of the pay and of the employer entry, a key no renderer names too, changed together
        with one that changes the summary line; the leaf's stored value must be on both surfaces."""
        from careeros.review.labels import _ENTRY_NAMES, _PAY_NAMES, _stated, capture
        pay = {"min": 70000, "max": 99000, "period": "hour", "period_stated": "per shift worked",
               "currency": "CAD", "inferred": ["period"], "a_pay_field_added_later": "paid weekly"}
        names = dict(_PAY_NAMES)
        for leaf, value in pay.items():
            with self.subTest(pay=leaf):
                store, _ = board_job()
                record = record_of(store)
                comp = {**record["comp"], leaf: value, **({"max": 101000} if leaf != "max" else {"min": 60000})}
                board_copy(store, {**record, "comp": comp})
                cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
                said = f"{names.get(leaf, leaf)} {_stated(value)}"
                for surface in (cli, card):
                    self.assertIn("pay: minimum", surface)   # the summary line differs too
                    self.assertIn("also: pay, as stored: ", surface)
                    line = surface.split("also: pay, as stored: ", 1)[1].split(" (this copy) / ", 1)[0]
                    self.assertIn(said, line)
        entry = {"name": "Synthetic Health Partners", "parent": "Synthetic Holdings", "employer_id": "greenhouse:other",
                 "resolved": "argument", "an_entry_field_added_later": "merged 2026"}
        names = dict(_ENTRY_NAMES)
        for leaf, value in entry.items():
            with self.subTest(employer=leaf):
                store, _ = board_job()
                record = record_of(store)
                employer = {**record["employer"], leaf: value, **({"name": "Synthetic Care"} if leaf != "name" else {})}
                board_copy(store, {**record, "employer": employer})
                cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
                line = f"also: employer {names.get(leaf, leaf)}: {value} (this copy) / {_stated(record['employer'].get(leaf))}"
                for surface in (cli, card):
                    self.assertIn("employer: Synthetic", surface)
                    self.assertIn(line, surface)

    def test_the_job_of_records_own_pay_line_says_what_is_stored(self):
        """The owner's D200: a stored 0-0 still reads as pay not stated (screening and digests keep that reading), and
        the line says what is stored, on every surface; the card's own pay summary too."""
        from careeros.review.labels import capture
        from careeros.review.report import _comp
        store, _ = board_job(comp={"min": 0, "max": 0, "period": "hour", "currency": "USD"})
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (JOB,))
        self.assertIsNone(job["comp_min"])
        self.assertIsNone(job["comp_max"])
        note = "stored: minimum 0 · maximum 0 · period hour · currency USD; CareerOS reads 0–0 as no pay given"
        cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
        for surface in (cli, card):
            self.assertIn(f"pay: not stated ({note})", surface)
        self.assertEqual(_comp(job), f"pay not posted ({note})")
        # no stored pay at all: the line says only that
        store, _ = board_job(comp={})
        cli, card = surfaces(store, capture(store, POLICY, MID, JOB))
        for surface in (cli, card):
            self.assertIn("pay: not stated\n" if surface is cli else "pay: not stated ", surface)
        self.assertEqual(_comp(store.one("SELECT * FROM jobs WHERE job_id = ?", (JOB,))), "pay not posted")

    def test_why_stored_pay_reads_as_none(self):
        from careeros.review.labels import stored_pay
        self.assertEqual(stored_pay({"min": 0, "max": 0, "currency": "USD"}),
                         "stored: minimum 0 · maximum 0 · currency USD; CareerOS reads 0–0 as no pay given")
        self.assertEqual(stored_pay({"min": 0, "period": "hour"}),
                         "stored: minimum 0 · period hour; CareerOS reads a minimum and maximum of 0 or none as no pay given")
        self.assertEqual(stored_pay({"period": "hour", "currency": "USD"}),
                         "stored: period hour · currency USD; CareerOS reads a minimum and maximum of 0 or none as no "
                         "pay given")
        for nothing in (None, {}, {"min": None, "currency": ""}, "", []):
            self.assertEqual(stored_pay(nothing), "")
        self.assertEqual(stored_pay("competitive"), "stored: competitive; CareerOS reads no pay from it")



LINK = "https://job-boards.greenhouse.io/synthhealth/jobs/1001"   # a link that names one posting (D184)
HEAD = "Job Title: Claims Analyst\nCompany: Synthetic Health\nLocation: Austin, TX\n\n"


def pastes() -> dict:
    """Codex's four inputs: a posting of exactly MAX_TEXT characters; the same with only blank space after it; the same
    with a sentence past the limit; and another sentence past it (already cut, as the third)."""
    from careeros.discovery.manual_intake import MAX_TEXT
    from tests.test_packages import FULL_TIME
    head = HEAD + FULL_TIME + "\n"
    filler = "".join(f"Note {n}: members are served across the US by the claims team. " for n in range(2000))
    exact = head + filler[:MAX_TEXT - len(head) - 1] + "."
    assert len(exact) == MAX_TEXT
    return {"exact": exact, "blank": exact + "  \n\n  ", "cut": exact + "\nTeam meetings are held on weekday evenings.",
            "cut again": exact + "\nTeam meetings are held on weekend mornings."}


class PasteReceiptTests(unittest.TestCase):
    """H43 (High): a paste's receipt was keyed on its first MAX_TEXT characters alone, so a paste that ran past the limit
    shared the receipt of the same posting ending at it: intake answered "recorded" before reading it, its cut mark
    never reached a sighting, and an Apply label given on the complete preview stayed usable. The receipt now includes
    what the paste's reading cut; two pastes both cut, differing only past the limit, still share one."""

    def setUp(self):
        from tests.test_v26_discovery import setup
        self.store, self.ctx = setup()
        self.texts = pastes()

    def paste(self, name, **kw):
        from careeros.discovery.manual_intake import intake
        return intake(self.ctx, self.texts[name], LINK, **kw)

    def labelled(self):
        """The job the first paste added, screened and labelled Apply from its complete preview: (job id, label)."""
        from careeros.pipeline import process
        from careeros.review.labels import latest_given, p1_held
        from tests.helpers import give_label
        first = self.paste("exact")
        self.assertEqual(first["status"], "added")
        process(self.ctx)
        give_label(self.store, POLICY, MID, first["job_id"], "apply")
        label = latest_given(self.store, first["job_id"], "primary")
        self.assertIsNone(p1_held(self.store, POLICY, MID, first["job_id"], label))
        return first, label

    def latest_sighting(self, job_id) -> dict:
        row = self.store.one("SELECT facts_json FROM observations WHERE job_id = ? AND json_extract(facts_json, "
                             "'$.sighting') = 1 ORDER BY rowid DESC LIMIT 1", (job_id,))
        return json.loads(row["facts_json"])["source"]

    def test_the_request_includes_what_the_reading_cut(self):
        from careeros.discovery.manual_intake import paste_request, parse_pasted
        requests = {name: paste_request(LINK, text) for name, text in self.texts.items()}
        self.assertEqual([parse_pasted(text, LINK)["cut"] for text in self.texts.values()], [[], [], ["text"], ["text"]])
        self.assertEqual(requests["exact"], requests["blank"])
        self.assertNotEqual(requests["exact"], requests["cut"])
        self.assertEqual(requests["cut"], requests["cut again"])   # both already cut: one request (the disclosed reading)
        self.assertNotIn("cut", requests["exact"])   # nothing cut: the request as it was, so its older receipt answers

    def test_codex_four_pastes(self):
        from careeros.review.labels import capture, p1_held, preview_lines
        first, label = self.labelled()
        job = first["job_id"]
        observed = self.store.one("SELECT COUNT(*) FROM observations")[0]
        again = self.paste("blank")
        self.assertEqual((again["status"], again["job_id"], again["operation"]), ("recorded", job, first["operation"]))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM observations")[0], observed)
        cut = self.paste("cut")
        self.assertEqual((cut["status"], cut["job_id"]), ("already_tracked", job))   # read, never answered "recorded"
        self.assertNotEqual(cut["operation"], first["operation"])
        self.assertEqual(self.latest_sighting(job)["cut"], ["text"])
        held = p1_held(self.store, POLICY, MID, job, label)
        self.assertTrue(held and held.startswith("label_facts_changed") and "a copy of this posting says something else"
                        in held, held)
        cli = "\n".join(preview_lines(capture(self.store, POLICY, MID, job), self.store))
        self.assertIn("also: cut to length: text (this copy) / not stated (the job of record)", cli)
        later = self.paste("cut again")
        self.assertEqual((later["status"], later["operation"]), ("recorded", cut["operation"]))

    def test_the_cli_and_a_retry_by_its_operation(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from careeros import cli
        from careeros.paths import vault_paths
        with tempfile.TemporaryDirectory() as tmp:
            def run(name, **kw):
                path = Path(tmp) / "posting.txt"
                path.write_text(self.texts[name], encoding="utf-8")
                args = SimpleNamespace(file=str(path), clipboard=False, url=LINK, company=None, title=None, location=None,
                                       **kw)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.cmd_paste(self.ctx, vault_paths(Path(tmp)), args)
                return out.getvalue()
            said = run("exact", separate=True)
            self.assertIn(": added", said)
            key = said.split("operation ", 1)[1].split(" ", 1)[0]
            job = self.store.one("SELECT job_id FROM jobs")["job_id"]
            self.assertIn(f"{job}: recorded", run("exact", operation=key))   # the same reading: its job
            observed = self.store.one("SELECT COUNT(*) FROM observations")[0]
            with self.assertRaises(SystemExit) as refused:   # another reading under the operation: refused, with why
                run("cut", operation=key)
            self.assertIn(f"Not added: operation {key} was recorded for another request", str(refused.exception))
            self.assertIn("runs past the 60,000-character limit", str(refused.exception))
            self.assertEqual(self.store.one("SELECT COUNT(*) FROM observations")[0], observed)
            self.assertIn(f"{job}: already_tracked", run("cut"))   # the CLI's own key: read once
            self.assertEqual(self.latest_sighting(job)["cut"], ["text"])
            self.assertIn(f"{job}: recorded", run("cut again"))


class PastePageTests(unittest.TestCase):
    """H43 on the review page: a form key resubmitted with another cut state is refused with its reason."""

    def setUp(self):
        from tests.test_review_page import ServerMixin
        self.mixin = type("Page", (ServerMixin, unittest.TestCase), {"runTest": lambda self: None})()
        self.mixin.setUp()
        self.addCleanup(self.mixin.tearDown)

    def test_a_form_resubmitted_with_text_past_the_limit_is_refused_with_its_reason(self):
        page = self.mixin
        texts = pastes()
        key = page.form(page.get(), "paste", "new")
        self.assertEqual(page.post("paste", target="new", key=key, url=LINK, text=texts["exact"], company="").status_code, 303)
        observed = page.count("observations")
        again = page.post("paste", target="new", key=key, url=LINK, text=texts["blank"], company="")
        self.assertEqual(again.status_code, 303)   # the same reading: its job again
        refused = page.post("paste", target="new", key=key, url=LINK, text=texts["cut"], company="")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("this form was already used for another paste", refused.text)
        self.assertIn("runs past the 60,000-character limit", refused.text)
        self.assertEqual(page.count("observations"), observed)
        fresh = page.form(page.get(), "paste", "new")   # a fresh form takes it in, read once
        self.assertEqual(page.post("paste", target="new", key=fresh, url=LINK, text=texts["cut"], company="").status_code, 303)
        self.assertGreater(page.count("observations"), observed)



class HoldTransferTests(unittest.TestCase):
    """H44 (High): a decision made without the writer lock (package list and show through readiness, authorize on its
    own, the review page's preview) paired a database snapshot taken at its first query with a reading of the
    pending-holds file taken later, at P1. A command's start recovering the file (record_pending_holds: the row
    committed, then the line removed) could fall between them, so the hold was in neither view and the package showed
    ready. Each read transaction now reads the file before its snapshot starts (Store.read; packages.pending_snapshot),
    so a hold moving from the file to a row is in one view or the other: the same hold id throughout."""

    HOLD = "hold:moving"

    def setUp(self):
        from careeros.core.clock import FixedClock
        from careeros.db.store import open_store
        from careeros.review import packages
        from tests.test_batch5 import FileStore, hold_line
        self.packages = packages
        self.s = FileStore(self)   # a board posting, screened, labelled Apply from a preview that showed no hold
        self.store = self.s.store
        self.assertEqual(self.s.route()[0], "apply")
        self.s.file.write_text(hold_line(JOB, self.HOLD) + "\n", encoding="utf-8")   # a read of it failed since
        # the command that recovers the file: another connection on the same database
        self.other = open_store(self.store.path, FixedClock("2026-10-12T12:05:00.000Z"), POLICY.fingerprint)
        self.addCleanup(self.other.close)
        self.moved = False

    def recover(self):
        """The other command's start: the hold's row committed, then its line removed from the file."""
        if not self.moved:
            self.moved = True
            self.assertEqual(self.packages.record_pending_holds(self.other), 1)
            self.assertFalse(self.s.file.exists())

    def recovering_before(self, module, name):
        """`module.name` runs only after the recovery: the reader has made its first query, P1 has not run yet."""
        real = getattr(module, name)

        def after_recovery(*args, **kwargs):
            self.recover()
            return real(*args, **kwargs)
        return mock.patch.object(module, name, after_recovery)

    def held_by_the_row(self):
        """After the transfer, a fresh decision is held by the row, under the same hold id."""
        route, basis = self.s.route()
        self.assertIsNone(route)
        self.assertTrue(basis.startswith("posting_not_stored:"), basis)
        self.assertEqual([(h["hold_id"], h["kept"]) for h in self.packages.open_holds(self.store, JOB)],
                         [(self.HOLD, "row")])

    def test_a_read_reads_the_file_before_its_snapshot(self):
        packages = self.packages
        with self.store.read():
            self.store.one("SELECT 1 FROM jobs")   # the snapshot starts
            self.recover()
            self.assertIsNone(self.store.one("SELECT 1 FROM exceptions WHERE json_extract(metadata_json, '$.hold_id') = ?",
                                             (self.HOLD,)))   # the row is not in this snapshot...
            held = packages.open_holds(self.store, JOB, packages.pending_snapshot(self.store))
            self.assertEqual([(h["hold_id"], h["kept"]) for h in held], [(self.HOLD, "file")])   # ...the line is
            route, basis = packages.route_for(self.store, POLICY, JOB, MID)
            self.assertIsNone(route)
            self.assertTrue(basis.startswith("posting_not_stored:"), basis)
        self.held_by_the_row()

    def test_readiness_during_the_transfer_is_withdrawn(self):
        from tests.test_packages import FACTS, QUESTIONS, Vault
        vault = Vault()
        self.addCleanup(vault.close)

        def ready():
            return self.packages.readiness(self.store, POLICY, MID, JOB, facts=FACTS, library=vault.library,
                                           vault_root=vault.root, questions=QUESTIONS)
        self.s.file.unlink()
        self.packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                              questions=QUESTIONS)
        self.assertNotEqual(ready()["state"], "withdrawn")   # the control: prepared, nothing holds it
        from tests.test_batch5 import hold_line
        self.s.file.write_text(hold_line(JOB, self.HOLD) + "\n", encoding="utf-8")
        with self.recovering_before(self.packages, "route_for"):   # after readiness's first query, before P1
            state = ready()
        self.assertTrue(self.moved)
        self.assertEqual(state["state"], "withdrawn")
        self.assertTrue(state["reasons"][0].startswith("posting_not_stored:"), state["reasons"])
        self.held_by_the_row()

    def test_authorize_during_the_transfer_is_not_approved(self):
        from careeros.review.submission import authorize
        with self.recovering_before(self.packages, "route_for"):   # after its status query, before P1
            result = authorize(self.store, POLICY, JOB, "direct", profile=MID)
        self.assertTrue(self.moved)
        self.assertIn("not_approved", [r["code"] for r in result.reasons])
        self.assertTrue(next(r["detail"] for r in result.reasons if r["code"] == "not_approved")
                        .startswith("posting_not_stored:"))
        self.held_by_the_row()

    def test_a_preview_during_the_transfer_shows_the_hold(self):
        from careeros.review import labels
        with self.recovering_before(labels, "_waiting"):   # after the capture's first query, before its holds
            captured = labels.capture(self.store, POLICY, MID, JOB)
        self.assertTrue(self.moved)
        self.assertEqual([h["hold_id"] for h in captured["holds_shown"]], [self.HOLD])
        self.assertEqual(captured["parts"]["holds"], [self.HOLD])
        after = labels.capture(self.store, POLICY, MID, JOB)   # after it: the row, the same id, the same digest
        self.assertEqual(after["parts"]["holds"], [self.HOLD])
        self.assertEqual(after["digest"], captured["digest"])

    def test_a_caller_inside_a_read_that_gives_no_reading_gets_the_reads_own(self):
        """open_holds and pending_file_problem with no reading passed (store_failed's way) take the read's own."""
        packages = self.packages
        with self.store.read():
            self.store.one("SELECT 1 FROM jobs")   # the snapshot starts
            self.recover()
            self.s.file.write_text("not a hold\n", encoding="utf-8")   # a line cut short, written meanwhile
            self.assertEqual([h["hold_id"] for h in packages.open_holds(self.store, JOB)], [self.HOLD])
            self.assertIsNone(packages.pending_file_problem(self.store))
            self.assertTrue(packages.store_failed(self.store, JOB))
        self.assertTrue(packages.pending_file_problem(self.store).startswith("holds_file_damaged"))   # read now

    def test_a_hold_in_both_views_is_one_hold(self):
        """A reading of the file that still has the line, paired with a snapshot that has its row (the moment between
        the recovery's commit and the line's removal): the row stands for it, one hold."""
        from tests.test_batch5 import hold_line
        packages = self.packages
        with self.store.read():
            self.recover()
            self.store.one("SELECT 1 FROM jobs")   # the snapshot starts after the row committed
            self.s.file.write_text(hold_line(JOB, self.HOLD) + "\n", encoding="utf-8")   # the line not yet removed
            held = packages.open_holds(self.store, JOB, packages.pending_snapshot(self.store))
        self.assertEqual([(h["hold_id"], h["kept"]) for h in held], [(self.HOLD, "row")])

    def test_a_write_transaction_reads_the_file_when_it_needs_it(self):
        """Inside tx() the reading is made at P1: the writer holds the write lock, so no recovery commits meanwhile."""
        packages = self.packages
        with self.store.tx():
            route, basis = packages.route_for(self.store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertTrue(basis.startswith("posting_not_stored:"), basis)


if __name__ == "__main__":
    unittest.main()
