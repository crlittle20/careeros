"""Codex Pass 18 (BLOCKERS-REMAIN on 9ff1731, code 07cc6e4) and the owner's D203: D1's comparison of a copy with the
record's marks ("Absent versus stated, on a copy"), with tests that fail on 9ff1731. Synthetic data only."""
import contextlib
import io
import itertools
import json
import unittest

from tests.test_packages import FULL_TIME
from tests.test_v26_discovery import MID, POLICY

WORKDAY = "https://foohealth.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-20417"   # Codex's own case
HEURISTIC = "Claims Analyst\n\n" + FULL_TIME   # a title by heuristic, the company from the link's host: both inferred
STATED = "Job Title: Claims Analyst\nCompany: foohealth\n\n" + FULL_TIME   # the same words stated in the text
LINK = "https://job-boards.greenhouse.io/synthhealth/jobs/1001"   # a link that names one posting (D184)
TEXT = "Job Title: Claims Analyst\nCompany: Synthetic Health\n\n" + FULL_TIME + "\n"
LONG_TITLE = "Claims Analyst " + " ".join(f"Team{i}" for i in range(45))        # past its limit of 200: cut
LONG_LOCATION = "Austin, TX " + " ".join(f"Site{i}" for i in range(70))         # past MAX_LOCATION (300): cut
DROPPED_INFERRED = ("also: read by CareerOS, not stated as such by the posting: none (this copy) / company, title (the job "
                    "of record)")


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def labelled(text, url, label="apply", **typed):
    """A posting pasted, processed and labelled from a preview: (store, ctx, job, the label)."""
    from careeros.discovery.manual_intake import intake
    from careeros.pipeline import process
    from careeros.review.labels import latest_given
    from tests.helpers import give_label
    from tests.test_v26_discovery import setup
    store, ctx = setup()
    first = quiet(intake, ctx, text, url, **typed)
    assert first["status"] == "added", first
    job = first["job_id"]
    quiet(process, ctx)
    give_label(store, POLICY, MID, job, label)
    return store, ctx, job, latest_given(store, job, "primary")


def record_of(store, job) -> dict:
    return json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])


def sighting_of(store, job) -> dict:
    """The latest sighting's source record."""
    row = store.one("SELECT facts_json FROM observations WHERE job_id = ? AND json_extract(facts_json, '$.sighting') = 1 "
                    "ORDER BY rowid DESC LIMIT 1", (job,))
    return json.loads(row["facts_json"])["source"]


class DecisionTests(unittest.TestCase):
    """D1 (potential High, Codex Pass 18): a copy's source record omits an empty mark list and the copy comparison skips
    an absent incoming value, so a copy that states, in the same words, a value the record marked inferred or cut read
    as unchanged when that was the record's last mark, and P1 never saw it. A copy that finds a field the record marks,
    and does not mark it, is now a change (the owner's D203, "Fix it, then Pass 19"). The same words are stated as your
    arguments over the same text: stated in the text, they change the text, which 9ff1731 already holds (a guard
    below)."""

    def assertHeld(self, store, job, label):
        from careeros.review.labels import p1_held
        held = p1_held(store, POLICY, MID, job, label)
        self.assertTrue(held and held.startswith("label_facts_changed") and "a copy of this posting says something else"
                        in held, held)

    def test_a_copy_that_states_what_the_record_inferred_holds_the_label(self):
        from careeros.discovery.manual_intake import intake
        from careeros.review.labels import latest_given, p1_held
        from tests.helpers import give_label
        for label in ("apply", "stretch"):
            with self.subTest(label):
                store, ctx, job, given = labelled(HEURISTIC, WORKDAY, label)
                record = record_of(store, job)
                self.assertEqual(record["inferred"], ["company", "title"])
                self.assertIsNone(p1_held(store, POLICY, MID, job, given))
                again = quiet(intake, ctx, HEURISTIC, WORKDAY, title="Claims Analyst", company="foohealth")
                self.assertEqual((again["status"], again["job_id"]), ("already_tracked", job))
                copy = sighting_of(store, job)
                self.assertEqual(copy, {k: v for k, v in record.items() if k != "inferred"})   # only its mark dropped
                self.assertHeld(store, job, given)
                give_label(store, POLICY, MID, job, label)   # again, from a fresh preview that shows the copy
                self.assertIsNone(p1_held(store, POLICY, MID, job, latest_given(store, job, "primary")))

    def test_a_copy_that_types_the_kept_title_holds_the_label(self):
        from careeros.discovery.manual_intake import intake
        store, ctx, job, given = labelled(TEXT, LINK, title=LONG_TITLE)
        record = record_of(store, job)
        kept = record["title"]
        self.assertEqual(record["cut"], ["title"])
        self.assertLessEqual(len(kept), 200)
        again = quiet(intake, ctx, TEXT, LINK, title=kept)   # exactly the kept title: it fits, and is not cut
        self.assertEqual((again["status"], again["job_id"]), ("already_tracked", job))
        self.assertEqual(sighting_of(store, job), {k: v for k, v in record.items() if k != "cut"})
        self.assertHeld(store, job, given)

    def test_a_mark_on_a_kept_location_compares_as_the_read_its_binding_holds(self):
        """A retained field: your location set by naming the job is the record's, the location it overrode its binding,
        and the read's cut mark stays the record's (retention substitutes the value, never the marks). The mark
        describes the read its binding holds: a copy sending that read again with its mark is unchanged, and one
        sending the same words not cut is a change."""
        from careeros.discovery.manual_intake import intake, operation_for, paste_request, set_location
        from careeros.pipeline import process
        from careeros.review.labels import latest_given, p1_held
        from tests.helpers import give_label
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        job = quiet(intake, ctx, TEXT, LINK, location=LONG_LOCATION)["job_id"]
        kept = record_of(store, job)["location"]
        quiet(set_location, ctx, job, "Austin, TX")
        quiet(process, ctx)
        give_label(store, POLICY, MID, job, "apply")
        given = latest_given(store, job, "primary")
        record = record_of(store, job)
        self.assertEqual((record["location"], record["cut"], record["bindings"]["location"],
                          record["bindings"]["location_source"]), ("Austin, TX", ["location"], kept, "argument"))
        same = operation_for("paste", "", paste_request(LINK, TEXT, location=LONG_LOCATION), separate=True)
        self.assertEqual(quiet(intake, ctx, TEXT, LINK, location=LONG_LOCATION, operation=same)["status"], "already_tracked")
        self.assertEqual(sighting_of(store, job)["cut"], ["location"])
        self.assertIsNone(p1_held(store, POLICY, MID, job, given))   # the read its binding holds, with its mark
        self.assertLessEqual(len(kept), 300)
        quiet(intake, ctx, TEXT, LINK, location=kept)   # the same words, not cut
        self.assertNotIn("cut", sighting_of(store, job))
        self.assertHeld(store, job, given)

    def test_the_fresh_preview_names_the_dropped_mark(self):
        from careeros.discovery.manual_intake import intake
        from careeros.review.labels import capture, preview_lines
        store, ctx, job, _given = labelled(HEURISTIC, WORKDAY)
        quiet(intake, ctx, HEURISTIC, WORKDAY, title="Claims Analyst", company="foohealth")
        shown = preview_lines(capture(store, POLICY, MID, job), store)
        self.assertIn("a copy of this posting says something else (a copy read for this job; pasted,",
                      " ".join(shown))
        self.assertIn(DROPPED_INFERRED, [line.strip() for line in shown])
        self.assertNotIn("no stated value differs", [line.strip() for line in shown])


class GuardTests(unittest.TestCase):
    """What 9ff1731 holds, held still (they pass on 9ff1731 too)."""

    def held(self, store, job, label):
        from careeros.review.labels import p1_held
        return p1_held(store, POLICY, MID, job, label)

    def test_a_copy_that_gains_a_mark_holds_the_label(self):
        from careeros.discovery.manual_intake import intake
        store, ctx, job, given = labelled(HEURISTIC, WORKDAY, title="Claims Analyst", company="foohealth")
        self.assertNotIn("inferred", record_of(store, job))
        quiet(intake, ctx, HEURISTIC, WORKDAY)
        self.assertEqual(sighting_of(store, job)["inferred"], ["company", "title"])
        self.assertRegex(self.held(store, job, given) or "", "^label_facts_changed: .*a copy of this posting says")

    def test_a_copy_that_drops_one_of_two_marks_holds_the_label(self):
        from careeros.discovery.manual_intake import intake
        store, ctx, job, given = labelled(HEURISTIC, WORKDAY)
        quiet(intake, ctx, HEURISTIC, WORKDAY, title="Claims Analyst")
        self.assertEqual(sighting_of(store, job)["inferred"], ["company"])
        self.assertRegex(self.held(store, job, given) or "", "^label_facts_changed: .*a copy of this posting says")

    def test_a_copy_whose_text_states_them_holds_the_label(self):
        """The same words stated in the text (the brief's own wording): its text differs, a change on 9ff1731 too."""
        from careeros.discovery.manual_intake import intake
        store, ctx, job, given = labelled(HEURISTIC, WORKDAY)
        quiet(intake, ctx, STATED, WORKDAY)
        self.assertNotIn("inferred", sighting_of(store, job))
        self.assertRegex(self.held(store, job, given) or "", "^label_facts_changed: .*a copy of this posting says")

    def test_an_unchanged_reading_and_an_equivalent_retry_keep_the_label(self):
        from careeros.discovery.manual_intake import intake, operation_for, paste_request
        for text, url, typed in ((HEURISTIC, WORKDAY, {}), (TEXT, LINK, {"title": LONG_TITLE})):
            with self.subTest(url):
                store, ctx, job, given = labelled(text, url, **typed)
                separate = operation_for("paste", "", paste_request(url, text, **typed), separate=True)
                self.assertEqual(quiet(intake, ctx, text, url, operation=separate, **typed)["status"], "already_tracked")
                self.assertEqual(sighting_of(store, job), record_of(store, job))   # its sighting: the same read, marks too
                self.assertIsNone(self.held(store, job, given))
                observed = store.one("SELECT COUNT(*) FROM observations")[0]
                retry = operation_for("paste", "", paste_request(url, text, **typed), operation_id=separate.key)
                self.assertEqual(quiet(intake, ctx, text, url, operation=retry, **typed)["status"], "recorded")
                self.assertEqual(quiet(intake, ctx, text, url, **typed)["status"], "recorded")   # the first's receipt
                self.assertEqual(store.one("SELECT COUNT(*) FROM observations")[0], observed)
                self.assertIsNone(self.held(store, job, given))

    def test_a_board_copys_card_shows_its_marks_as_before(self):
        """A board's read states every input: its card line for a mark the record has and it does not is as before."""
        from careeros.openings import record_observation
        from careeros.pipeline import sighting_facts
        from careeros.review.labels import capture, preview_lines
        store, _ctx, job, _given = labelled(HEURISTIC, WORKDAY)
        board = {k: v for k, v in record_of(store, job).items() if k != "inferred"} | {"ats": "workday"}
        record_observation(store, source_kind="ats_scan", evidence_level="live_ats", job_id=job, url=WORKDAY,
                           facts=sighting_facts(board))
        shown = preview_lines(capture(store, POLICY, MID, job), store)
        self.assertIn("also: read by CareerOS, not stated as such by the posting: not stated (this copy) / company, title "
                      "(the job of record)", [line.strip() for line in shown])


def old_same_read(stored, incoming, *, board):
    """9ff1731's same_read, whole (inlined for the tighter-only property)."""
    from careeros.pipeline import _blank
    if not stored or stored.get("unknown"):
        return False
    bindings = stored.get("bindings") or {}
    for key in (set(stored) | set(incoming)) - {"bindings"}:
        if key == "employer":
            mine, theirs = stored.get(key) or {}, incoming.get(key) or {}
            if any(_blank(mine.get(k)) != _blank(theirs.get(k)) for k in ("employer_id", "name", "parent")):
                return False
            continue
        if not board and _blank(incoming.get(key)) is None:
            continue
        expected = bindings[key] if key in bindings else stored.get(key)
        if _blank(expected) != _blank(incoming.get(key)):
            return False
    return True


BASE = {"title": "Claims Analyst", "ats": "manual", "location": "Austin, TX", "location_source": "argument",
        "description_text": "Claims work. " * 20, "url": WORKDAY,
        "employer": {"employer_id": None, "name": "foohealth", "parent": "foohealth", "resolved": "parent_for"}}


def with_marks(record: dict, inferred=(), cut=()) -> dict:
    return {**record, **({"inferred": list(inferred)} if inferred else {}), **({"cut": list(cut)} if cut else {})}


class ComparisonTests(unittest.TestCase):
    """The comparison itself: each mark's name, the fields a copy did not find, board reads, legacy records."""

    def test_the_coordinators_probe(self):
        from careeros.pipeline import same_read
        cases = [(with_marks(BASE, ["company", "title"]), dict(BASE)),
                 (with_marks(BASE, ["company", "title"]), with_marks(BASE, ["company"])),
                 (dict(BASE), with_marks(BASE, ["company", "title"])),
                 (with_marks(BASE, cut=["title"]), dict(BASE))]
        self.assertEqual([same_read(s, i, board=False) for s, i in cases], [False] * 4)
        self.assertEqual([old_same_read(s, i, board=False) for s, i in cases], [True, False, False, True])   # 9ff1731

    def test_each_marks_name_qualifies_its_field(self):
        from careeros.pipeline import marked_value
        self.assertEqual([marked_value(BASE, n) for n in ("company", "title", "location", "text")],
                         ["foohealth", "Claims Analyst", "Austin, TX", BASE["description_text"]])
        self.assertEqual(marked_value({"employer": {"name": None, "parent": "foohealth"}}, "company"), None)
        self.assertEqual(marked_value({"employer": None}, "company"), None)
        self.assertEqual(marked_value({"requisition_id": "R-1"}, "requisition_id"), "R-1")   # any other name: its key
        self.assertEqual((marked_value(BASE, ["title"]), marked_value(BASE, None)), (None, None))

    def test_each_mark_a_copy_drops_on_a_field_it_found(self):
        from careeros.pipeline import dropped_marks, same_read
        stored = with_marks(BASE, ["company", "title"], ["title", "location", "text"])
        self.assertEqual(dropped_marks(stored, dict(BASE)), {"inferred": ["company", "title"],
                                                             "cut": ["title", "location", "text"]})
        self.assertEqual(dropped_marks(stored, stored), {})
        for name, key in (("text", "description_text"), ("location", "location"), ("title", "title")):
            with self.subTest(name):
                one = with_marks(BASE, cut=[name])
                self.assertFalse(same_read(one, dict(BASE), board=False))
                self.assertTrue(same_read(one, {**BASE, key: None}, board=False))   # not found: says nothing
        self.assertEqual(dropped_marks(with_marks(BASE, ["title"]), {**BASE, "inferred": "title"}), {"inferred": ["title"]})
        self.assertEqual(dropped_marks({**BASE, "inferred": "title"}, dict(BASE)), {})   # not a list: no names
        self.assertTrue(same_read({**BASE, "inferred": [["title"]]}, dict(BASE), board=False))   # no name, as before
        self.assertEqual(dropped_marks(with_marks(BASE, ["company"]), {**BASE, "employer": None}), {})

    def test_a_copy_that_does_not_find_a_marked_field_compares_as_before(self):
        from careeros.pipeline import same_read
        stored = with_marks(BASE, ["title"], ["location"])
        for incoming in ({**BASE, "title": None, "location": None}, with_marks({**BASE, "title": None}, cut=["location"]),
                         with_marks({**BASE, "title": ""}, cut=["location"]),
                         with_marks({**BASE, "title": None, "location": None}, ["title"])):
            with self.subTest(incoming):
                self.assertTrue(same_read(stored, incoming, board=False))
                self.assertTrue(old_same_read(stored, incoming, board=False))
        # one that does not find a marked field and keeps another field's mark: today's whole-list comparison, kept
        stored = with_marks(BASE, ["company", "title"])
        incoming = with_marks({**BASE, "title": None}, ["company"])
        self.assertEqual((same_read(stored, incoming, board=False), old_same_read(stored, incoming, board=False)),
                         (False, False))

    def test_a_mark_on_a_bound_field(self):
        from careeros.pipeline import same_read
        stored = {**with_marks(BASE, cut=["location"]), "location": "Remote - US",
                  "bindings": {"location": "Austin, TX", "location_source": "argument"}}
        self.assertTrue(same_read(stored, with_marks(BASE, cut=["location"]), board=False))   # the bound read, its mark
        self.assertFalse(same_read(stored, dict(BASE), board=False))   # the same words, not cut
        self.assertTrue(same_read(stored, {**BASE, "location": None}, board=False))   # no location: says nothing
        self.assertTrue(old_same_read(stored, dict(BASE), board=False))   # 9ff1731

    def test_legacy_and_rebuilt_records(self):
        from careeros.pipeline import same_read
        self.assertTrue(same_read(dict(BASE), dict(BASE), board=False))   # no marks: read as stated
        self.assertTrue(same_read(dict(BASE), {**BASE, "title": None}, board=False))
        rebuilt = {**with_marks(BASE, ["title"]), "unknown": ["comp"]}
        for incoming in (dict(BASE), rebuilt, with_marks(BASE, ["title"])):
            self.assertFalse(same_read(rebuilt, incoming, board=False))
            self.assertFalse(same_read(rebuilt, incoming, board=True))

    def test_board_reads_compare_as_before(self):
        from careeros.pipeline import same_read
        stored = with_marks(BASE, ["company", "title"], ["location"])
        for incoming in (dict(BASE), {**BASE, "title": None}, with_marks(BASE, ["company"]), stored):
            self.assertEqual(same_read(stored, incoming, board=True), old_same_read(stored, incoming, board=True))


def grid():
    """Every pair of mark lists (inferred of company and title; cut of title, company, location and text), with each
    field found or not by the copy, its location bound or not, and the bound read sent or the record's own."""
    def subsets(names):
        return [list(c) for r in range(len(names) + 1) for c in itertools.combinations(names, r)]
    inferred, cut = subsets(("company", "title")), subsets(("title", "company", "location", "text"))
    marks = list(itertools.product(inferred, cut))
    for (si, sc), (ii, ic) in itertools.product(marks, marks):
        for title, company, text, location, bound in itertools.product((True, False), (True, False), (True, False),
                                                                       ("none", "record", "bound"), (False, True)):
            stored = with_marks(BASE, si, sc)
            if bound:
                stored = {**stored, "location": "Remote - US", "bindings": {"location": "Austin, TX"}}
            incoming = with_marks({**BASE, "title": BASE["title"] if title else None,
                                   "employer": BASE["employer"] if company else {**BASE["employer"], "name": None},
                                   "description_text": BASE["description_text"] if text else None,
                                   "location": {"none": None, "record": stored["location"], "bound": "Austin, TX"}[location]},
                                  ii, ic)
            yield stored, incoming


class TighterOnlyPropertyTests(unittest.TestCase):
    """The owner's D203: nothing 9ff1731 counts as a change may become unchanged. Over the grid, the new comparison's
    "unchanged" implies 9ff1731's; on a copy it differs from 9ff1731 exactly where the record marks a field the copy
    found and does not mark (an independent statement of the rule); a board read compares as on 9ff1731."""

    def test_unchanged_implies_unchanged_on_9ff1731(self):
        from careeros.pipeline import same_read
        found = {"title": lambda r: r["title"], "company": lambda r: r["employer"]["name"],
                 "location": lambda r: r["location"], "text": lambda r: r["description_text"]}
        pairs = tightened = 0
        for stored, incoming in grid():
            pairs += 1
            new, old = same_read(stored, incoming, board=False), old_same_read(stored, incoming, board=False)
            dropped = any(name not in incoming.get(mark, []) and found[name](incoming)
                          for mark in ("inferred", "cut") for name in stored.get(mark, []))
            if new and not old:
                self.fail(f"looser: {stored} / {incoming}")
            self.assertEqual(new, old and not dropped, (stored, incoming))
            tightened += old and not new
            self.assertEqual(same_read(stored, incoming, board=True), old_same_read(stored, incoming, board=True))
        self.assertEqual(pairs, 64 * 64 * 48)
        self.assertGreater(tightened, 0)


if __name__ == "__main__":
    unittest.main()
