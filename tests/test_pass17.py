"""Codex Pass 17 (BLOCKERS-REMAIN on 2280a59, code e19262e) and the owner's D201: H45's correction, with tests that fail
on 2280a59. Synthetic data only."""
import contextlib
import io
import json
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.test_packages import FULL_TIME
from tests.test_v26_discovery import MID, POLICY

LINK = "https://job-boards.greenhouse.io/synthhealth/jobs/1001"   # a link that names one posting (D184)
TEXT = "Job Title: Claims Analyst\nCompany: Synthetic Health\nLocation: Austin, TX\n\n" + FULL_TIME + "\n"


def words(prefix: str, n: int) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


# each typed field past its limit (title 200, company 100, location MAX_LOCATION 300)
LONG = {"title": "Claims Analyst " + words("Team", 45), "company": "Synthetic Health " + words("Unit", 25),
        "location": "Austin, TX " + words("Site", 70)}
LIMIT = {"title": 200, "company": 100, "location": 300}


def spaced(value: str) -> str:
    """The same words with extra spaces in its first eight gaps (all within its first `limit` characters): one
    canonical form, one cut mark, other words kept (Codex's H45)."""
    return value.replace(" ", "   ", 8)


def latest_sighting(store, job_id) -> dict:
    return store.one("SELECT title, company, location, facts_json FROM observations WHERE job_id = ? AND "
                     "json_extract(facts_json, '$.sighting') = 1 ORDER BY rowid DESC LIMIT 1", (job_id,))


class KeptValueTests(unittest.TestCase):
    """H45 (High): the request bound a typed field's whole value in the canonical form and its cut mark, while
    parse_pasted keeps canonical(value[:limit]), cut before the canonical form. Two pastes of one posting whose typed
    field differs only in spacing within its first `limit` characters had one receipt and kept different words: intake
    answered "recorded" before the sighting, so the changed reading never reached P1. The request now binds what was
    kept when a field is cut (the owner's D201, "Bind kept values when cut")."""

    def test_the_coordinators_probe(self):
        from careeros.discovery.manual_intake import MAX_LOCATION, paste_request, parse_pasted
        body = "Senior Data Analyst\nCompany: Example Corp\n" + ("We analyse revenue data and build reports. " * 10)
        a = " ".join(f"Site{i}" for i in range(80))
        b = a.replace(" ", "    ", 20)
        self.assertTrue(len(a) > MAX_LOCATION and len(b) > MAX_LOCATION)
        pa, pb = parse_pasted(body, None, location=a), parse_pasted(body, None, location=b)
        self.assertNotEqual(pa["location"], pb["location"])   # other words kept
        ra, rb = paste_request(None, body, location=a), paste_request(None, body, location=b)
        self.assertEqual((ra["location"], ra["cut"]), (rb["location"], rb["cut"]))   # one canonical form, one cut mark
        self.assertNotEqual(ra, rb)
        self.assertEqual(ra["kept"], {"location": pa["location"], "location_source": "argument"})

    def test_each_field_kept_is_bound_when_cut(self):
        from careeros.discovery.manual_intake import paste_request, parse_pasted
        from careeros.normalize.sanitize import canonical
        for field, value in LONG.items():
            with self.subTest(field):
                self.assertEqual(canonical(value), canonical(spaced(value)))
                first, second = parse_pasted(TEXT, LINK, **{field: value}), parse_pasted(TEXT, LINK, **{field: spaced(value)})
                self.assertEqual((first["cut"], second["cut"]), ([field], [field]))
                self.assertNotEqual(first[field], second[field])
                request = paste_request(LINK, TEXT, **{field: value})
                self.assertNotEqual(request, paste_request(LINK, TEXT, **{field: spaced(value)}))
                expected = {field: first[field]} | ({"location_source": "argument"} if field == "location" else {})
                self.assertEqual(request["kept"], expected)

    def test_a_second_paste_is_read_and_p1_sees_it(self):
        """Codex's case end to end, for each typed field: the second paste is read again, its sighting carries its kept
        value, and the Apply label given on the first is held."""
        from careeros.discovery.manual_intake import intake, parse_pasted
        from careeros.pipeline import process
        from careeros.review.labels import latest_given, p1_held
        from tests.helpers import give_label
        from tests.test_v26_discovery import setup
        for field, value in LONG.items():
            with self.subTest(field):
                store, ctx = setup()
                first = intake(ctx, TEXT, LINK, **{field: value})
                self.assertEqual(first["status"], "added")
                job = first["job_id"]
                process(ctx)
                give_label(store, POLICY, MID, job, "apply")
                label = latest_given(store, job, "primary")
                self.assertIsNone(p1_held(store, POLICY, MID, job, label))
                second = intake(ctx, TEXT, LINK, **{field: spaced(value)})
                self.assertEqual((second["status"], second["job_id"]), ("already_tracked", job))   # never "recorded"
                self.assertNotEqual(second["operation"], first["operation"])
                kept = parse_pasted(TEXT, LINK, **{field: spaced(value)})[field]
                self.assertEqual(latest_sighting(store, job)[field], kept)
                held = p1_held(store, POLICY, MID, job, label)
                self.assertTrue(held and held.startswith("label_facts_changed"), held)
                again = intake(ctx, TEXT, LINK, **{field: spaced(value)})   # the same reading again: its receipt answers
                self.assertEqual((again["status"], again["operation"]), ("recorded", second["operation"]))

    def test_the_cli_paste_and_a_retry_by_its_operation(self):
        from careeros import cli
        from careeros.discovery.manual_intake import parse_pasted
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import setup
        for field, value in LONG.items():
            with self.subTest(field), tempfile.TemporaryDirectory() as tmp:
                store, ctx = setup()
                path = Path(tmp) / "posting.txt"
                path.write_text(TEXT, encoding="utf-8")

                def run(typed, path=path, field=field, ctx=ctx, **kw):
                    args = SimpleNamespace(**({"file": str(path), "clipboard": False, "url": LINK, "company": None,
                                               "title": None, "location": None, field: typed} | kw))
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        cli.cmd_paste(ctx, vault_paths(Path(tmp)), args)
                    return out.getvalue()
                said = run(value, separate=True)
                self.assertIn(": added", said)
                key = said.split("operation ", 1)[1].split(" ", 1)[0]
                job = store.one("SELECT job_id FROM jobs")["job_id"]
                self.assertIn(f"{job}: recorded", run(value, operation=key))   # the same reading: its job
                observed = store.one("SELECT COUNT(*) FROM observations")[0]
                with self.assertRaises(SystemExit) as refused:   # another kept reading under the operation: refused
                    run(spaced(value), operation=key)
                self.assertIn(f"Not added: operation {key} was recorded for another request", str(refused.exception))
                self.assertIn("as typed, for one that runs past its limit", str(refused.exception))
                self.assertEqual(store.one("SELECT COUNT(*) FROM observations")[0], observed)
                self.assertIn(f"{job}: already_tracked", run(spaced(value)))   # the CLI's own key: read once
                self.assertEqual(latest_sighting(store, job)[field], parse_pasted(TEXT, LINK, **{field: spaced(value)})[field])


class KeptValuePageTests(unittest.TestCase):
    """H45 on the review page (its one typed field, the company): a form key resubmitted with another kept company is
    refused with its reason; a fresh form reads it."""

    def setUp(self):
        from tests.test_review_page import ServerMixin
        self.mixin = type("Page", (ServerMixin, unittest.TestCase), {"runTest": lambda self: None})()
        self.mixin.setUp()
        self.addCleanup(self.mixin.tearDown)

    def test_a_form_resubmitted_with_another_kept_company_is_refused(self):
        from careeros.discovery.manual_intake import parse_pasted
        page = self.mixin
        value = LONG["company"]
        key = page.form(page.get(), "paste", "new")
        self.assertEqual(page.post("paste", target="new", key=key, url=LINK, text=TEXT, company=value).status_code, 303)
        self.assertEqual(page.post("paste", target="new", key=key, url=LINK, text=TEXT, company=value).status_code, 303)
        observed = page.count("observations")
        refused = page.post("paste", target="new", key=key, url=LINK, text=TEXT, company=spaced(value))
        self.assertEqual(refused.status_code, 409)
        self.assertIn("this form was already used for another paste", refused.text)
        self.assertIn("as typed, when it runs past its limit", refused.text)
        self.assertEqual(page.count("observations"), observed)
        fresh = page.form(page.get(), "paste", "new")   # a fresh form takes it in, read once
        self.assertEqual(page.post("paste", target="new", key=fresh, url=LINK, text=TEXT,
                                   company=spaced(value)).status_code, 303)
        self.assertGreater(page.count("observations"), observed)
        row = page.store.one("SELECT company FROM observations WHERE json_extract(facts_json, '$.sighting') = 1 "
                             "ORDER BY rowid DESC LIMIT 1")
        self.assertEqual(row["company"], parse_pasted(TEXT, LINK, company=spaced(value))["company"])


BLANKS = ("", "   ", "\u200b", "\t\r\n", "\u2060")


class BlankFieldTests(unittest.TestCase):
    """A typed title, company or location whose canonical form is empty is not given (the owner's D201). The request
    read "" and None as one value, while parse_pasted kept a blank --location as the location (source "argument") and
    so hid the posting's own Location line, under one receipt (the coordinator's h45_blank_probe)."""

    def test_a_blank_field_reads_as_none_under_the_same_request(self):
        from careeros.discovery.manual_intake import paste_request, parse_pasted
        for field in ("title", "company", "location"):
            none = parse_pasted(TEXT, LINK)
            for blank in BLANKS:
                with self.subTest(field=field, blank=blank):
                    self.assertEqual(paste_request(LINK, TEXT, **{field: blank}), paste_request(LINK, TEXT))
                    read = parse_pasted(TEXT, LINK, **{field: blank})
                    self.assertEqual(read, none)
        self.assertEqual((none["title"], none["company"], none["location"], none["location_source"]),
                         ("Claims Analyst", "Synthetic Health", "Austin, TX", "label"))

    def test_a_blank_location_keeps_the_postings_location_line(self):
        from careeros.discovery.manual_intake import intake, location_write
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, TEXT, LINK, location="   ")
        self.assertEqual(first["status"], "added")
        job = store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (first["job_id"],))
        self.assertEqual(job["location_raw"], "Austin, TX")
        self.assertEqual(location_write(store, first["job_id"])["source"], "label")
        again = intake(ctx, TEXT, LINK)   # no --location: the same request, the same reading
        self.assertEqual((again["status"], again["operation"]), ("recorded", first["operation"]))

    def test_a_blank_location_on_a_tracked_job_is_no_location(self):
        """A paste of a tracked job with a blank --location is a paste without one: no location note (on 2280a59 it
        offered to set the job's location to the blank value)."""
        from careeros.discovery.manual_intake import intake
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, TEXT, LINK)
        again = intake(ctx, TEXT + "\nThe team meets on Mondays.", LINK, location="   ")
        self.assertEqual((again["status"], again["job_id"]), ("already_tracked", first["job_id"]))
        self.assertIsNone(again["location_note"])
        typed = intake(ctx, TEXT + "\nThe team meets on Tuesdays.", LINK, location="Remote - US")
        self.assertIn("location not changed", typed["location_note"])

    def test_the_cli_paste_with_a_blank_location(self):
        from careeros import cli
        from careeros.discovery.manual_intake import location_write
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.txt"
            path.write_text(TEXT, encoding="utf-8")
            args = SimpleNamespace(file=str(path), clipboard=False, url=LINK, company=None, title=None, location="   ")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.cmd_paste(ctx, vault_paths(Path(tmp)), args)
            self.assertIn(": added", out.getvalue())
            self.assertNotIn("location not changed", out.getvalue())
        job = store.one("SELECT job_id, location_raw FROM jobs")
        self.assertEqual(job["location_raw"], "Austin, TX")
        self.assertEqual(location_write(store, job["job_id"])["source"], "label")


ADD_URL = "https://careers.synthhealth.example/jobs/1001"   # no board link: add-url --text-file


class AddUrlTests(unittest.TestCase):
    """add-url --text-file has its own receipt (request_of, the arguments in the canonical form). Its posting was built
    from the raw arguments: a blank --location was stored as your location (source "argument"), a blank --parent as
    the parent "", under the request of no --location and no --parent. Its ingest cuts none of these fields, so the
    request's canonical arguments are what is stored."""

    def add(self, store, ctx, **kw):
        from careeros import cli
        from careeros.paths import vault_paths
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.txt"
            path.write_text(kw.pop("text", "Claims Analyst\n\n" + FULL_TIME + "\n"), encoding="utf-8")
            args = SimpleNamespace(**({"url": ADD_URL, "text_file": str(path), "employer": "Synthetic Health",
                                       "title": "Claims Analyst", "location": None, "parent": None, "separate": False,
                                       "operation": None} | kw))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                job = cli.cmd_add_url(ctx, vault_paths(Path(tmp)), args)
            return job, out.getvalue()

    def stored(self, store, job):
        row = store.one("SELECT location_raw, parent, title, source_json FROM jobs WHERE job_id = ?", (job,))
        source = json.loads(row["source_json"])
        return {"location": row["location_raw"], "parent": row["parent"], "title": row["title"],
                "location_source": source["location_source"], "employer": source["employer"]}

    def test_a_blank_location_or_parent_is_not_given(self):
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        plain, _said = self.add(store, ctx)
        expected = self.stored(store, plain)
        self.assertEqual((expected["location_source"], expected["employer"]["resolved"]), ("none", "parent_for"))
        for location, parent in (("   ", "   "), ("\u200b", "\t"), ("", "")):
            with self.subTest(location=location, parent=parent):
                store, ctx = setup()
                job, _said = self.add(store, ctx, location=location, parent=parent)
                self.assertEqual(job, plain)
                self.assertEqual(self.stored(store, job), expected)
                again, said = self.add(store, ctx)   # no --location, no --parent: the same request, its job
                self.assertEqual(again, job)
                self.assertIn("already recorded", said)

    def test_a_blank_title_or_employer_is_missing(self):
        from tests.test_v26_discovery import setup
        for field in ("title", "employer"):
            with self.subTest(field):
                store, ctx = setup()
                with self.assertRaises(SystemExit) as refused:
                    self.add(store, ctx, **{field: "   "})
                self.assertIn("--text-file FILE --employer NAME --title TITLE", str(refused.exception))
                self.assertIsNone(store.one("SELECT 1 FROM jobs"))

    def test_one_employer_name_is_one_id(self):
        """add-url's manual id comes from the employer's canonical form, as its request binds it: "Cafe Co" typed with
        its accent composed (U+00E9) or combining (U+0301) is one request and one generated id (on 2280a59, caf-co and
        cafe-co: two ids under one receipt)."""
        from tests.test_v26_discovery import setup
        composed, combining = "Caf\u00e9 Co", "Cafe\u0301 Co"
        ids = []
        for employer in (composed, combining):
            store, ctx = setup()
            job, _said = self.add(store, ctx, employer=employer)
            ids.append(job)
        self.assertEqual(ids[0], ids[1])
        self.assertTrue(ids[0].startswith("manual:caf-co:"), ids[0])
        again, said = self.add(store, ctx, employer=composed)   # the other spelling: the same request, its job
        self.assertEqual((again, "already recorded" in said), (ids[1], True))

    def test_its_ingest_cuts_none_of_its_typed_fields(self):
        """A guard: add-url keeps its typed title and location whole in the canonical form, so spacing in a long one
        changes neither its request nor what is stored."""
        from careeros.normalize.sanitize import canonical
        from tests.test_v26_discovery import setup
        title, location = LONG["title"], LONG["location"]
        store, ctx = setup()
        job, _said = self.add(store, ctx, title=title, location=location)
        stored = self.stored(store, job)
        self.assertEqual((stored["title"], stored["location"]), (canonical(title), canonical(location)))
        again, said = self.add(store, ctx, title=spaced(title), location=spaced(location))
        self.assertEqual(again, job)
        self.assertIn("already recorded", said)


# the receipts 2280a59 recorded for these pastes, none cut (scratchpad p17_old_receipts.py, run in a clean worktree of
# e19262e, whose code is 2280a59's): the key and the payload's fingerprint
OLD_RECEIPTS = {
    "plain": ("paste:025b7bb3265181cf39481bcd1e3e71218bc91945",
              "c655c4dec5688ff7d58d154e35dd49b494e51c5d65e298a3e707a0fccfaa591f"),
    "no link": ("paste:27214d4eb445c2cdeaeab70a4bfa072f73791587",
                "79c21917b669fe86e5f8e3d8a8a19f1459a9ad828bfba5a1bffe75a379146c26"),
    "typed": ("paste:86f69b1e509aef2583604d27c9f419e93d96f560",
              "66d088da9e19c4d2db266328c34066f091da4c65c7e421537d5a4b8e8877a938"),
    "at the limits": ("paste:35958f427ef0a91bda071956effd209dba2d6302",
                      "4c8363b7e2309f40ab3ff76bad75e02c0aa4685000faa78e8f13a5ed9230fe09"),
    "padded text": ("paste:025b7bb3265181cf39481bcd1e3e71218bc91945",
                    "c655c4dec5688ff7d58d154e35dd49b494e51c5d65e298a3e707a0fccfaa591f"),
}


def old_cases() -> dict:
    from careeros.discovery.manual_intake import MAX_TEXT
    return {"plain": dict(url=LINK, text=TEXT), "no link": dict(url=None, text=TEXT),
            "typed": dict(url=LINK, text=TEXT, company="Synthetic  Health\t", title="Claims\r\nAnalyst",
                          location=" Austin,  TX "),
            "at the limits": dict(url=LINK, text=TEXT, company="C" * 100, title="T" * 200, location="L" * 300),
            "padded text": dict(url=LINK, text=TEXT + " " * (MAX_TEXT + 10))}


class ReceiptGuardTests(unittest.TestCase):
    """Guards: a paste with nothing cut keeps 2280a59's request byte for byte, so its receipt still answers the retry;
    two already-cut description bodies with the same kept reading still share a receipt (H43's equivalence)."""

    def test_nothing_cut_keeps_its_request(self):
        from careeros.core.fingerprint import fingerprint
        from careeros.discovery.manual_intake import operation_for, paste_request
        for name, case in old_cases().items():
            with self.subTest(name):
                request = paste_request(case["url"], case["text"], company=case.get("company"),
                                        title=case.get("title"), location=case.get("location"))
                self.assertEqual(sorted(request), ["company", "location", "text", "title", "url"])
                op = operation_for("paste", "", request)
                self.assertEqual((op.key, fingerprint({"command": op.command, "payload": op.payload})),
                                 OLD_RECEIPTS[name])

    def test_a_receipt_recorded_by_2280a59_answers_the_retry(self):
        """This code records the very receipt row 2280a59 records (its key and fingerprint, OLD_RECEIPTS), so a row
        2280a59 wrote answers the retry, which writes nothing."""
        from careeros.discovery.manual_intake import intake
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        case = old_cases()["typed"]
        first = intake(ctx, case["text"], case["url"], company=case["company"], title=case["title"],
                       location=case["location"])
        key, digest = OLD_RECEIPTS["typed"]
        row = store.one("SELECT receipt_key, payload_fingerprint, result_ref FROM receipts WHERE receipt_key = ?", (key,))
        self.assertEqual((row["payload_fingerprint"], row["result_ref"]), (digest, first["job_id"]))
        observed = store.one("SELECT COUNT(*) FROM observations")[0]
        again = intake(ctx, case["text"], case["url"], company="Synthetic Health", title="Claims\nAnalyst",
                       location="Austin, TX")
        self.assertEqual((again["status"], again["job_id"], again["operation"]), ("recorded", first["job_id"], key))
        self.assertEqual(store.one("SELECT COUNT(*) FROM observations")[0], observed)

    def test_two_cut_bodies_with_one_kept_reading_share_a_receipt(self):
        from careeros.discovery.manual_intake import paste_request
        from tests.test_pass16 import pastes
        texts = pastes()
        value = LONG["location"]
        one = paste_request(LINK, texts["cut"], location=value)
        self.assertEqual(one["cut"], ["location", "text"])
        self.assertEqual(one, paste_request(LINK, texts["cut again"], location=value))
        self.assertNotEqual(one, paste_request(LINK, texts["cut again"], location=spaced(value)))


# a typed company's end after its final ".": a tab, a no-break space, a zero-width space, a word joiner, a line break
COMPANY_ENDS = ("\t", "\u00a0", "\u200b", "\u2060", "\n")


class CompanyEndsTests(unittest.TestCase):
    """A typed company's ends (the coordinator's choice after Codex Pass 17, D201): parse_pasted stripped spaces, commas
    and periods from the value's ends as typed, before its canonical form, while the request binds canonical(company).
    So a tab, no-break space, zero-width or invisible character or line break after a final "." stopped the strip:
    "Synthetic Health Inc." kept "Synthetic Health Inc" and the same with a tab after it kept "Synthetic Health Inc.",
    under one receipt with nothing cut. A typed company that fits its limit is now read from its canonical form."""

    def test_the_five_variants_keep_one_company(self):
        from careeros.discovery.manual_intake import paste_request, parse_pasted
        plain = parse_pasted(TEXT, LINK, company="Synthetic Health Inc.")
        self.assertEqual(plain["company"], "Synthetic Health Inc")
        for end in COMPANY_ENDS:
            with self.subTest(end=end):
                typed = "Synthetic Health Inc." + end
                self.assertEqual(paste_request(LINK, TEXT, company=typed),
                                 paste_request(LINK, TEXT, company="Synthetic Health Inc."))
                self.assertEqual(parse_pasted(TEXT, LINK, company=typed), plain)

    def test_the_job_stored_is_what_a_retry_keeps(self):
        """The first paste stores what the second would keep, and the second is answered by the first's receipt."""
        from careeros.discovery.manual_intake import intake
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, TEXT, LINK, company="Synthetic Health Inc.\t")
        self.assertEqual(store.one("SELECT employer_name FROM jobs WHERE job_id = ?", (first["job_id"],))["employer_name"],
                         "Synthetic Health Inc")
        again = intake(ctx, TEXT, LINK, company="Synthetic Health Inc.")
        self.assertEqual((again["status"], again["job_id"]), ("recorded", first["job_id"]))

    def test_its_ends_go_until_none_is_left_and_a_cut_company_is_cut_as_before(self):
        from careeros.discovery.manual_intake import company_ends, parse_pasted
        from careeros.normalize.sanitize import canonical_cut
        self.assertEqual(company_ends(" ,Acme , Rx.\u200b ,\t.\n"), "Acme , Rx")   # its middle as it is
        self.assertEqual(company_ends("Acme\nInc.\n,"), "Acme\nInc")
        long = LONG["company"] + "."
        self.assertEqual(parse_pasted(TEXT, LINK, company=long)["company"], canonical_cut(long.strip(" ,."), 100))


# --- Codex's closure check as a property ------------------------------------------------------------------------------
ACCENTED = ("Café", "Résumé", "Société", "Señor")


def bases(field: str) -> list[str]:
    """Typed values of each length class: short, around the field's limit, and twice past it; and each ending in
    punctuation, as a company often does ("Synthetic Health, Inc.")."""
    short = {"title": "Claims Analyst", "company": "Synthetic Health", "location": "Austin, TX"}[field]
    filler = short + " " + " ".join(f"{ACCENTED[i % 4]}{i}" for i in range(120))
    limit = LIMIT[field]
    values = [short, f"{short} {' '.join(ACCENTED)}"] + [filler[:n].rstrip() for n in
                                                           (limit - 3, limit - 1, limit, limit + 1, limit + 4, 2 * limit)]
    return values + [f"{short}, Inc.", f"{short} Inc.,", filler[:limit - 1].rstrip() + "."]


VARIANTS = (
    lambda v: v,
    lambda v: v.replace(" ", "  ", 1),
    lambda v: v.replace(" ", "  ", 5),
    lambda v: v.replace(" ", "   ", 20),
    lambda v: v.replace(" ", "\t", 5),
    lambda v: v.replace(" ", "\r\n", 1),
    lambda v: v.replace(" ", "\n", 1),
    lambda v: v.replace(" ", "\u00a0", 5),
    lambda v: v.replace(" ", "\u200b", 3),
    lambda v: v[:1] + "\u2060" + v[1:],
    lambda v: "  " + v + " \t",
    lambda v: v + "\t",
    lambda v: v + "\u00a0",
    lambda v: v + "\u200b",
    lambda v: v + "\u2060",
    lambda v: v + "\n",
    lambda v: "\u200b" + v,
    lambda v: unicodedata.normalize("NFD", v),
    lambda v: unicodedata.normalize("NFC", v),
)
KEPT = ("title", "company", "location", "location_source", "inferred", "cut")


def closure_violations(fields=("title", "company", "location"), values=None) -> list:
    """Pairs of typed values with one request and another kept reading (Codex's closure check): for each field, every
    value of `values` (by default the blank ones, and each base under each variant), as paste_request and parse_pasted
    read it on TEXT. Returns [(field, a, b)]."""
    from careeros.discovery.manual_intake import paste_request, parse_pasted
    found = []
    for field in fields:
        typed = values(field) if values else [None, *BLANKS] + [f(b) for b in bases(field) for f in VARIANTS]
        seen: dict = {}
        for value in typed:
            request = json.dumps(paste_request(LINK, TEXT, **{field: value}), sort_keys=True)
            read = parse_pasted(TEXT, LINK, **{field: value})
            kept = tuple(json.dumps(read[k]) for k in KEPT)
            if request in seen and seen[request][1] != kept:
                found.append((field, seen[request][0], value))
            seen.setdefault(request, (value, kept))
    return found


class ClosurePropertyTests(unittest.TestCase):
    """Codex's closure check: equal requests mean equal kept title, company, location, location_source, inferred and
    cut, over generated variants of each typed field (spacing, line endings, tabs, invisible characters, NFC and NFD,
    blank values, lengths around each limit)."""

    def test_equal_requests_mean_equal_kept_facts(self):
        found = closure_violations()
        self.assertEqual(found[:5], [])

    def test_the_property_compares_something(self):
        """Many variants share a request (so the check has pairs to compare), cut ones included."""
        from careeros.discovery.manual_intake import paste_request
        for field in LIMIT:
            requests = [json.dumps(paste_request(LINK, TEXT, **{field: f(b)}), sort_keys=True)
                        for b in bases(field) for f in VARIANTS]
            cut = [r for r in requests if '"cut"' in r]
            self.assertLess(len(set(requests)), len(requests), field)
            self.assertLess(len(set(cut)), len(cut), field)


if __name__ == "__main__":
    unittest.main()
