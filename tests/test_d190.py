"""The P1 build: the D190 design, revision 8 (Codex's design review cleared it at Pass 8), one part per commit.

Part 1, the canonical form (section 3a): one function, sanitize.canonical (the text cleaner with Unicode NFC as its last
step), applied where text enters CareerOS, and a writer that refuses any other form in the content columns.
Synthetic data only; nothing here reads the network."""
import json
import random
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.core.clock import FixedClock
from careeros.db import store as store_mod
from careeros.db.store import (SCHEMA_DIR, MigrationPending, NonCanonicalText, PermissionDenied, SchemaTooNew,
                               newest_schema, open_store)
from careeros.discovery import boards
from careeros.discovery.alerts import AlertEmail, Link
from careeros.discovery.boards import greenhouse_posting
from careeros.discovery.manual_intake import intake, set_location
from careeros.discovery.saved import parse_saved_html, parse_saved_text
from careeros.normalize.posting import POSTING_TEXT_FIELDS, RawPosting
from careeros.normalize.sanitize import canonical, canonical_cut, clean_text, html_to_text
from careeros import pipeline
from careeros.openings import record_observation
from careeros.pipeline import employer_entry, ingest, normalized_fields
from careeros.qualify import gate as gate_mod

from tests.test_lead_resolution import legacy_board
from tests.test_v26_discovery import CLAIMS, gh_item, setup

E = "Cafe\u0301"           # a decomposed accent
COMPOSED = "Caf\u00e9"     # the same letter, composed
POOL = ["a", "e", "A", " ", "\t", "\n", "\r", "\u0301", "\u0300", "\u0327", "\u200b", "\u200c", "\u200d", "\u2060",
        "\ufeff", "\u00a0", "\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\u202f", "\u3000", "\u2000", "\u2001", "\u00e9",
        "\u0345", "\u1100", "\u1161", "\u11a8", "\uac00", "\u0f73", "\u0344", "\u212b", "\u2126", "\u0958", "\x00",
        "\x1f", "\x7f", "\u061c", "\u180e", "\U0001d15e", "\U000e0001", "x", "<", ">", "&"]
BODY = (f"We review claims for a health plan at {E} Health. You will audit claims, check coding and work with providers "
        "on appeals. Requirements: 3 years of claims experience; knowledge of CPT and ICD-10 coding; strong Excel skills. "
        "Bachelor's degree preferred. Remote within the US.")


class _Client:
    """A board API that answers every request with one payload (the adapters read nothing else of the client)."""

    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url):
        return self.payload


class CanonicalFormTests(unittest.TestCase):
    """Section 3a: the canonical form, where it is applied, and the writer that enforces it."""

    def test_canonical_is_idempotent_and_nfc_with_nfc_as_its_last_step(self):
        # Codex's design review, Pass 5: a format character removed between a letter and a combining mark joins them
        self.assertEqual(canonical("e\u200d\u0301"), "\u00e9")
        rng = random.Random(190)
        for _ in range(4000):
            text = "".join(rng.choice(POOL) for _ in range(rng.randint(0, 14)))
            once = canonical(text)
            self.assertEqual(canonical(once), once, repr(text))
            self.assertTrue(unicodedata.is_normalized("NFC", once), repr(text))

    def test_every_description_route_reads_the_canonical_form(self):
        self.assertIs(clean_text, canonical)   # point 1
        self.assertEqual(html_to_text(f"<p>{E}  team</p>"), f"{COMPOSED} team")

    def test_a_posting_is_canonical_when_it_is_made(self):
        fields = ("title", "location", "description_text", "requisition_id", "employment_type_hint", "workplace_hint",
                  "department")
        self.assertEqual(set(POSTING_TEXT_FIELDS), set(fields))   # every text field, named here, not read from the code
        values = {name: f"  {E}\u00a0 {name}\u200b " for name in fields}
        url = f"https://job-boards.greenhouse.io/synth/jobs/1?q={E}  x"
        p = RawPosting(ats="greenhouse", board_token="synth", external_id="1", url=url, apply_url=url,
                       typed_countries=[f"{E}  land"], **values)
        for name in fields:   # point 2, the description included
            self.assertEqual(getattr(p, name), f"{COMPOSED} {name}", name)
        self.assertEqual(p.typed_countries, [f"{COMPOSED} land"])
        self.assertEqual((p.url, p.apply_url), (url, url))   # links as received

    def test_a_board_item_is_canonical_before_the_adapter_reads_it(self):
        """Point 3: Lever's place reader joins the posting's places, so one place written two ways was listed twice."""
        link = f"https://jobs.lever.co/synth/l1?ref={E}"
        item = {"id": "l1", "text": "Claims Analyst", "descriptionPlain": BODY, "hostedUrl": link,
                "applyUrl": "https://jobs.lever.co/synth/l1/apply",
                "categories": {"location": f"{E} City", "allLocations": [f"{COMPOSED} City"]}}
        self.assertEqual(boards.lever_posting("synth", item).location, f"{COMPOSED} City; {COMPOSED} City")  # unread
        p = boards.fetch_board(_Client([item]), "lever", "synth")[0]
        self.assertEqual((p.location, p.url), (f"{COMPOSED} City", link))
        single = boards.fetch_single(_Client(item), "lever", "synth", "l1")
        self.assertEqual((single.location, single.url), (f"{COMPOSED} City", link))

    def test_a_saved_pages_json_ld_is_canonical_before_its_readers_read_it(self):
        """Point 4: a format character inside "TELECOMMUTE" hid that the posting is remote."""
        ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Claims Analyst",
              "description": f"<p>{BODY}</p>", "hiringOrganization": {"name": "Synthetic Health"},
              "jobLocationType": "TELE\u200dCOMMUTE",
              "jobLocation": {"@type": "Place", "address": {"addressRegion": "TX", "addressCountry": "US"}}}
        posting, _ = parse_saved_html(f'<script type="application/ld+json">{json.dumps(ld)}</script>', "claims.html")
        self.assertEqual((posting.workplace_hint, posting.location), ("remote", "Remote - TX, United States"))

    def test_a_saved_text_files_readers_read_its_canonical_text(self):
        """Point 4 (Codex's design review Pass 5): the requisition was read from the raw body."""
        posting, _ = parse_saved_text("Title: Claims Analyst\nEmployer: Synthetic Health\n\nJob ID: R-\u200d4402\n" + BODY,
                                      "claims.txt")
        self.assertEqual(posting.requisition_id, "R-4402")

    def test_an_alert_card_and_its_email_are_canonical(self):
        link = Link("https://www.linkedin.com/jobs/view/4000000901", "linkedin", f"Claims  Analyst {E}",
                    f"{E}  Health \u00b7 Remote ", True)
        alert = AlertEmail(f"Jobs\u00a0 {E}", f"New  jobs {E} ", None, [link], message_key="eml:x")
        self.assertEqual((link.text, link.context), (f"Claims Analyst {COMPOSED}", f"{COMPOSED} Health \u00b7 Remote"))
        self.assertEqual((alert.sender, alert.subject), (f"Jobs {COMPOSED}", f"New jobs {COMPOSED}"))
        store, ctx = setup()
        counts = cli._record_alert(ctx, alert, mock.MagicMock())
        self.assertEqual((counts["lead_new"], counts["lead_errors"]), (1, 0))
        self.assertEqual(store.one("SELECT title FROM leads")[0], f"Claims Analyst {COMPOSED}")

    def test_employer_names_and_parents_are_canonical(self):
        """Point 6: where normalized_fields takes them, and at the registry write."""
        store, ctx = setup()
        p = RawPosting(ats="manual", board_token="t", external_id="1", title="Claims Analyst", description_text=BODY)
        fields = normalized_fields(ctx, {"employer_id": None, "name": f"{E}  Health", "parent": f"{E}  Group"}, p)
        self.assertEqual((fields["employer_name"], fields["parent"]), (f"{COMPOSED} Health", f"{COMPOSED} Group"))
        row = cli._add_employer(ctx, f"{E}  Health", "greenhouse", "cafe", parent=f"{E}  Group")
        self.assertEqual((row["name"], row["parent"]), (f"{COMPOSED} Health", f"{COMPOSED} Group"))
        stored = store.one("SELECT name, parent FROM employers WHERE employer_id = 'greenhouse:cafe'")
        self.assertEqual(tuple(stored), (f"{COMPOSED} Health", f"{COMPOSED} Group"))

    def test_the_policy_and_the_profile_are_canonical_when_loaded(self):
        """Point 7: a term you typed decomposed matches posting text typed composed; a policy already in the canonical
        form keeps its fingerprint."""
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import load_policy, load_yaml
        from careeros.qualify.profile import load_profile
        with tempfile.TemporaryDirectory() as tmp:
            typed = Path(tmp) / "typed.yaml"
            typed.write_text(f"title_exclude:\n  - \"{E}  manager\"\nfamilies:\n  claims: [\"{E} analyst\"]\n", encoding="utf-8")
            policy = load_policy(typed)
            self.assertEqual(policy.get("title_exclude"), [f"{COMPOSED} manager"])
            self.assertEqual(policy.get("families", "claims"), [f"{COMPOSED} analyst"])
            plain = Path(tmp) / "plain.yaml"
            plain.write_text("title_exclude:\n  - director\n", encoding="utf-8")
            self.assertEqual(load_policy(plain).fingerprint, fingerprint(load_yaml(plain)))
            profile = Path(tmp) / "profile.yaml"
            profile.write_text(f"state: TX\nskills:\n  - name: \"{E}  coding\"\n", encoding="utf-8")
            self.assertEqual(load_profile(profile).data["skills"][0]["name"], f"{COMPOSED} coding")

    def test_your_arguments_are_canonical_before_they_make_an_id(self):
        """Point 8: the manual id comes from your --employer or the paste's company, so two spellings made two jobs."""
        args = SimpleNamespace(employer=f"{E}  Health", title=f"Claims  {E}", location=None, note="as  typed")
        cli._canonical_args(args)
        self.assertEqual((args.employer, args.title, args.note), (f"{COMPOSED} Health", f"Claims {COMPOSED}", "as  typed"))
        store, ctx = setup()
        text = "Claims Analyst\nLocation: Remote - US\n\n" + BODY
        first = intake(ctx, text, None, company=f"{E} Health")
        second = intake(ctx, text, None, company=f"{COMPOSED} Health")
        self.assertEqual((first["job_id"], second["job_id"], second["status"]),
                         (first["job_id"], first["job_id"], "recorded"))   # one request: its retry (section 6)

    def test_careeros_location_stores_the_canonical_form(self):
        """Point 8 (Codex's design review Pass 5: set_location stored decomposed text)."""
        store, ctx = setup()
        job = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + BODY, None)["job_id"]
        set_location(ctx, job, f"{E} City,\u00a0 TX\u200b")
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job,))[0], f"{COMPOSED} City, TX")

    def test_the_writer_refuses_any_other_form(self):
        """A path that misses canonical() fails loudly instead of storing a second form; links are never checked."""
        store, ctx = setup()
        now = store.clock.now()
        row = {"job_id": "manual:x:1", "workflow_id": "job:manual:x:1", "source": "fixture", "ats": "manual",
               "employer_name": "Synthetic Health", "parent": "Synthetic Health", "external_id": "1", "title": f"{E} Analyst", "title_norm": "cafe analyst", "first_seen_at": now,
               "last_seen_at": now, "content_fingerprint": "c" * 64, "current_state": "discovered", "state_sequence": 0,
               "entered_state_at": now}
        writer = store.writer("discovery")
        with self.assertRaisesRegex(NonCanonicalText, r"jobs\.title"):
            writer.insert("jobs", row)
        writer.insert("jobs", {**row, "title": f"{COMPOSED} Analyst", "url": f"https://x.example/{E}  y"})
        with self.assertRaisesRegex(NonCanonicalText, r"jobs\.location_raw"):
            writer.update("jobs", {"location_raw": "Austin,  TX"}, "job_id = ?", ("manual:x:1",))
        writer.update("jobs", {"location_raw": "Austin, TX", "apply_url": f"https://x.example/{E}"}, "job_id = ?",
                      ("manual:x:1",))
        # a sighting's facts carry posting content and are checked; other facts are not
        with self.assertRaisesRegex(NonCanonicalText, r"observations\.facts_json"):
            record_observation(store, source_kind="human_text", evidence_level="human",
                               facts={"sighting": True, "title": f"{E} Analyst", "url": f"https://x.example/{E}"})
        record_observation(store, source_kind="human_text", evidence_level="human",
                           facts={"sighting": True, "title": f"{COMPOSED} Analyst", "url": f"https://x.example/{E}"})
        record_observation(store, source_kind="human_text", evidence_level="human", facts={"via": "as  typed"})

    def test_a_cut_value_is_made_canonical_again(self):
        """A value cut to its column's length can end on a space: it is made canonical again, never refused."""
        self.assertEqual(canonical_cut("a" * 199 + " b", 200), "a" * 199)
        store, ctx = setup()
        record_observation(store, source_kind="human_text", evidence_level="human", title="t" * 199 + " x",
                           company="c" * 119 + " y", location="l" * 499 + " z")
        row = store.one("SELECT title, company, location FROM observations")
        self.assertEqual(tuple(row), ("t" * 199, "c" * 119, "l" * 499))
        p = RawPosting(ats="manual", board_token="t", external_id="1", title="Claims Analyst", description_text=BODY,
                       department="d" * 199 + " e", workplace_hint="w" * 99 + " v")
        fields = normalized_fields(ctx, None, p)
        self.assertEqual((fields["department"], fields["workplace_hint"]), ("d" * 199, "w" * 99))

    def test_a_pastes_company_cut_to_its_length_is_canonical(self):
        """Every field a paste's reader returns is in the canonical form (section 3a, point 8): a Company line longer
        than its 100 characters, cut on a space, is made canonical again before it makes the manual id."""
        from careeros.discovery.manual_intake import parse_pasted
        parsed = parse_pasted("Claims Analyst\nCompany: " + "C" * 99 + " Holdings\n\n" + BODY)
        self.assertEqual(parsed["company"], "C" * 99)
        for key in ("title", "company", "location"):
            self.assertEqual(parsed[key], canonical(parsed[key]))

    def test_an_nfc_equivalent_read_is_unchanged(self):
        """Codex's design review Pass 4, H17 (i): the same posting with one accent written two ways is one read."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        first = ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, f"Claims Analyst {COMPOSED}",
                                                                           CLAIMS + f" {COMPOSED} team.", "synth"))])
        again = ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, f"Claims Analyst {E}",
                                                                           CLAIMS + f" {E} team.", "synth"))])
        self.assertEqual((first["new"], again["unchanged"], again["changed"]), (1, 1, 0))


class Migration017Tests(unittest.TestCase):
    """Part 2 (section 7): migration 017 adds P1's columns and tables and raises the floor to 17, so an engine without
    P1 never writes a database that holds P1's approvals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "careeros.db"

    def at_16(self):
        con = sqlite3.connect(self.db)
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) <= 16:
                con.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
        con.close()   # 016 writes schema 16 and its floor itself

    def test_017_writes_its_markers_and_its_tables_inside_its_own_script(self):
        self.assertEqual(newest_schema(), 17)
        self.assertNotIn(17, store_mod.RUNS_ON_OLDER)
        self.at_16()
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        con.executescript("BEGIN;\n" + (SCHEMA_DIR / "017_p1_approval.sql").read_text(encoding="utf-8") + "\nCOMMIT;")
        markers = dict(con.execute("SELECT key, value FROM meta WHERE key IN ('schema_version', 'oldest_engine_schema')"))
        self.assertEqual(markers, {"schema_version": "17", "oldest_engine_schema": "17"})

        def columns(table):
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        self.assertIn("source_json", columns("jobs"))
        self.assertIn("source_digest", columns("gate_results"))
        self.assertIn("files_json", columns("packages"))
        self.assertIn("preview_id", columns("labels"))
        self.assertLessEqual({"preview_id", "job_id", "profile", "digest", "parts_json", "holds_json", "created_at",
                              "used_at", "label_id"}, columns("label_previews"))
        self.assertLessEqual({"hold_id", "job_id", "cleared_at"}, columns("hold_clearances"))

    def test_an_engine_without_p1_refuses_a_p1_database(self):
        open_store(self.db, FixedClock(), "test").close()   # this engine: 017
        for older in (16, 15):   # the D192 build; the deployed engine (dda868e)
            with mock.patch.object(store_mod, "newest_schema", return_value=older):
                for mode in (True, False):
                    with self.subTest(older=older, migrate=mode), self.assertRaisesRegex(SchemaTooNew, "restore a backup"):
                        open_store(self.db, FixedClock(), "test", migrate=mode).close()

    def test_a_database_left_at_16_is_migrated_by_this_engine(self):
        """A stop between 016 and 017 leaves schema 16 under the floor 16, which this engine migrates."""
        self.at_16()
        with self.assertRaises(MigrationPending):
            open_store(self.db, FixedClock(), "test", migrate=False).close()
        store = open_store(self.db, FixedClock(), "test")
        self.addCleanup(store.close)
        self.assertEqual((store.schema_version(), store._meta("oldest_engine_schema")), (17, "17"))

    def test_who_may_write_the_new_tables(self):
        """A preview is written by the review component and, once written, only its use is recorded; a hold's
        clearance is discovery's; the source record is a discovery column of the job."""
        store, ctx = setup()
        now = store.clock.now()
        review = store.writer("review")
        review.insert("label_previews", {"preview_id": "pv:1", "job_id": "j", "profile": "primary", "digest": "d",
                                         "parts_json": "{}", "created_at": now})
        review.update("label_previews", {"used_at": now, "label_id": "lbl:1"}, "preview_id = ?", ("pv:1",))
        with self.assertRaises(PermissionDenied):
            review.update("label_previews", {"digest": "other"}, "preview_id = ?", ("pv:1",))
        with self.assertRaises(PermissionDenied):
            store.writer("discovery").insert("label_previews", {"preview_id": "pv:2", "job_id": "j", "profile": "primary",
                                                                "digest": "d", "parts_json": "{}", "created_at": now})
        store.writer("discovery").insert("hold_clearances", {"hold_id": "hold:1", "job_id": "j", "cleared_at": now})
        with self.assertRaises(PermissionDenied):
            review.insert("hold_clearances", {"hold_id": "hold:2", "job_id": "j", "cleared_at": now})
        self.assertIn("source_json", store_mod.JOB_COLUMNS_BY_COMPONENT["discovery"])


class SourceRecordTests(unittest.TestCase):
    """Part 3a (section 3a): the source record holds every input normalized_fields reads, and every job write stores it
    with the employer entry the one resolver gave."""

    DECLARED = ("raw", "updated_at")   # the bookkeeping inputs, reviewed here: never read from the code

    def test_the_record_holds_every_posting_field_but_the_declared_bookkeeping(self):
        from dataclasses import fields as dataclass_fields
        self.assertEqual(pipeline.SOURCE_BOOKKEEPING, self.DECLARED)
        p = RawPosting(ats="greenhouse", board_token="synth", external_id="1", title="Claims Analyst")
        record = pipeline.source_record(p, None)
        posting = {f.name for f in dataclass_fields(RawPosting)} - set(self.DECLARED)
        self.assertEqual(set(record), posting | {"location_source", "reader", "employer"})
        self.assertEqual(set(record["employer"]), {"employer_id", "name", "parent", "resolved"})

    def test_the_raw_keys_the_engine_reads_are_inputs_or_declared_bookkeeping(self):
        """Found from the code's syntax tree: a runtime recorder loses track where _keep_your_location copies the
        payload into a plain dict."""
        import ast
        read = set()
        for path in Path(pipeline.__file__).parent.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                target = None
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "raw":
                    target = node.slice
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                      and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "raw" and node.args):
                    target = node.args[0]
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    read.add(target.value)
        self.assertEqual(read, {"location_source", "reader", "isListed"})   # today's, reviewed here
        self.assertEqual((set(pipeline.RAW_INPUTS), set(pipeline.RAW_BOOKKEEPING)),
                         ({"location_source", "reader"}, {"isListed"}))

    def test_a_bookkeeping_input_changes_neither_the_jobs_record_nor_its_screen(self):
        store, ctx = setup()
        base = dict(ats="greenhouse", board_token="synth", external_id="1", title="Claims Analyst",
                    location="Remote - US", description_text=CLAIMS)
        entry = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health", "resolved": "argument"}

        def outcome(p):
            fields = normalized_fields(ctx, entry, p)
            fields.pop("source_updated_at")   # the bookkeeping value itself, kept for explain and the export
            result = gate_mod.evaluate(pipeline.job_view({"job_id": "j", **fields}), [], ctx.profile, ctx.policy)
            return fields, (result.decision, result.hard_failures, result.flags)
        reference = outcome(RawPosting(**base, raw={"location_source": "none"}, updated_at="2026-10-10T00:00:00Z"))
        for variant in (RawPosting(**base, raw={"location_source": "none", "isListed": False, "other": 1},
                                   updated_at="2026-10-10T00:00:00Z"),
                        RawPosting(**base, raw={"location_source": "none"}, updated_at="2026-11-11T11:11:11Z")):
            self.assertEqual(outcome(variant), reference)

    def test_the_one_resolver_of_an_employer_entry(self):
        from careeros.discovery.saved import parent_for
        store, ctx = setup()
        self.assertEqual(employer_entry(ctx, row={"employer_id": "greenhouse:synth", "name": f"{E}  Health", "parent": None}),
                         {"employer_id": "greenhouse:synth", "name": f"{COMPOSED} Health", "parent": f"{COMPOSED} Health",
                          "resolved": "registry"})
        self.assertEqual(employer_entry(ctx, name="Synthetic Health", parent="Synth Group"),
                         {"employer_id": None, "name": "Synthetic Health", "parent": "Synth Group", "resolved": "argument"})
        self.assertEqual(employer_entry(ctx, name="Synthetic Health"),
                         {"employer_id": None, "name": "Synthetic Health",
                          "parent": parent_for("Synthetic Health", ctx.policy), "resolved": "parent_for"})
        self.assertEqual(employer_entry(ctx)["resolved"], "none")
        job = {"source_json": None, "employer_id": None, "employer_name": "Synthetic Health", "parent": "Synth Group"}
        self.assertEqual(employer_entry(ctx, job=job)["resolved"], "stored")

    def test_every_job_write_stores_its_source_record_as_read(self):
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth", department="D" * 250))])
        job = store.one("SELECT source_json, department FROM jobs")
        record = json.loads(job["source_json"])
        self.assertEqual((record["department"], len(job["department"])), ("D" * 250, 200))   # as read; the column cuts
        self.assertEqual((record["title"], record["employer"]["resolved"]), ("Claims Analyst", "registry"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Senior Claims Analyst", CLAIMS, "synth"))])
        self.assertEqual(json.loads(store.one("SELECT source_json FROM jobs")[0])["title"], "Senior Claims Analyst")
        store.conn.execute("UPDATE jobs SET source_json = NULL")   # a job stored before 017
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Senior Claims Analyst", CLAIMS, "synth"))])
        self.assertIsNotNone(store.one("SELECT source_json FROM jobs")[0])

    def test_a_paste_and_a_text_file_record_how_their_entry_was_resolved(self):
        import contextlib
        import io

        from careeros.paths import vault_paths
        store, ctx = setup()
        job = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + BODY, None)["job_id"]
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual((record["employer"]["resolved"], record["location_source"]), ("parent_for", "none"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.txt"
            path.write_text(BODY + " The team is growing.", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                added = cli.cmd_add_url(ctx, vault_paths(Path(tmp) / "vault"), SimpleNamespace(
                    url="https://careers.example.com/jobs/claims-2", text_file=str(path), employer="Synthetic Health",
                    title="Claims Analyst", location=None, parent="Synth Group"))
        record = json.loads(store.one("SELECT source_json FROM jobs WHERE job_id = ?", (added,))[0])
        self.assertEqual(record["employer"], {"employer_id": None, "name": "Synthetic Health", "parent": "Synth Group",
                                              "resolved": "argument"})

    def test_a_read_with_no_entry_keeps_the_jobs_entry_and_a_pasted_job_keeps_its_own_link(self):
        """The shortlist refresh passes no entry when no registry row names the job's board: the board token was written
        as the employer. A board read written into a job you pasted first keeps that job's own link (_KEPT_LINK), in the
        record as in the columns."""
        store, ctx = setup()
        pasted = "https://boards.greenhouse.io/synth/jobs/1?gh_src=mail"
        job = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + CLAIMS, pasted)["job_id"]
        ingest(ctx, None, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))],
               source="manual_url", full_board=False)
        row = store.one("SELECT employer_name, url, source_json FROM jobs WHERE job_id = ?", (job,))
        record = json.loads(row["source_json"])
        self.assertEqual((row["employer_name"], record["employer"]["name"]), ("Synthetic Health", "Synthetic Health"))
        self.assertEqual((row["url"], record["url"], record["apply_url"]), (pasted, pasted, pasted))


class SameReadTests(unittest.TestCase):
    """Part 3b (section 3, "Unchanged reads" and "Retained fields"): one predicate decides a board read's unchanged
    branch, and anything else a board sends is written and screened again."""

    def setUp(self):
        self.store, self.ctx = setup()
        self.employer = dict(legacy_board(self.store, name="Synthetic Health", token="synth"))

    def read(self, item, employer=None, **kw):
        return ingest(self.ctx, employer or self.employer, [greenhouse_posting("synth", item)], full_board=False, **kw)

    def job(self):
        return self.store.one("SELECT * FROM jobs")

    def test_null_and_blank_are_one_value_and_a_board_clearing_a_field_is_a_change(self):
        self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", req="R-1101"))
        self.assertEqual(self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", req="R-1101"))["unchanged"], 1)
        self.assertEqual(self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", req=None))["unchanged"], 0)
        self.assertIsNone(self.job()["requisition_id"])
        self.assertEqual(self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", req=""))["unchanged"], 1)

    def test_a_department_change_is_written_and_screened_again(self):
        """Today's unchanged branch wrote it silently, and the title rescue reads it."""
        self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", department="Claims"))
        pipeline.process(self.ctx)
        self.assertEqual(self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", department="Payment Integrity"))["unchanged"], 0)
        job = self.job()
        self.assertEqual((job["department"], json.loads(job["source_json"])["department"], job["current_state"]),
                         ("Payment Integrity", "Payment Integrity", "normalized"))

    def test_a_parent_only_change_is_another_read(self):
        """Codex H19: only the employer's parent changed, the read was unchanged, and the old screen stood."""
        item = gh_item(1, "Claims Analyst", CLAIMS, "synth")
        self.read(item)
        pipeline.process(self.ctx)
        self.assertEqual(self.read(item)["unchanged"], 1)   # the control: the same parent
        employer_id = self.employer["employer_id"]
        self.store.conn.execute("UPDATE employers SET parent = 'Other Holdings' WHERE employer_id = ?", (employer_id,))
        row = dict(self.store.one("SELECT * FROM employers WHERE employer_id = ?", (employer_id,)))
        self.assertEqual(self.read(item, employer=row)["unchanged"], 0)
        job = self.job()
        self.assertEqual((job["parent"], json.loads(job["source_json"])["employer"]["parent"], job["current_state"]),
                         ("Other Holdings", "Other Holdings", "normalized"))

    def test_a_destination_only_change_is_written(self):
        """Today's unchanged branch never wrote apply_url."""
        def posting(apply_url):
            return RawPosting(ats="greenhouse", board_token="synth", external_id="1", title="Claims Analyst",
                              location="Remote - US", url="https://job-boards.greenhouse.io/synth/jobs/1",
                              apply_url=apply_url, description_text=CLAIMS)
        ingest(self.ctx, self.employer, [posting("https://job-boards.greenhouse.io/synth/jobs/1#app")], full_board=False)
        counts = ingest(self.ctx, self.employer, [posting("https://apply.example.com/synth/1")], full_board=False)
        self.assertEqual((counts["unchanged"], self.job()["apply_url"]), (0, "https://apply.example.com/synth/1"))

    def test_a_reader_update_on_the_same_read_refreshes_derived_values_only(self):
        item = gh_item(1, "Claims Analyst", CLAIMS, "synth")
        self.read(item)
        pipeline.process(self.ctx)
        job = self.job()
        record, edits = job["source_json"], self.store.one("SELECT COUNT(*) FROM job_changes")[0]
        self.store.writer("discovery").update("jobs", {"remote_scope": "older reader"}, "job_id = ?", (job["job_id"],))
        self.assertEqual(self.read(item)["unchanged"], 1)
        job = self.job()
        self.assertNotEqual(job["remote_scope"], "older reader")
        self.assertEqual((job["source_json"], job["current_state"]), (record, "normalized"))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM job_changes")[0], edits)   # not an employer's edit

    def test_a_settled_board_location_is_compared_with_the_field_you_settled(self):
        abroad = gh_item(1, "Claims Analyst", CLAIMS, "synth", location="Toronto, ON")
        self.read(abroad)
        set_location(self.ctx, self.job()["job_id"], "Remote - US")
        self.assertEqual(self.read(abroad)["unchanged"], 1)   # the board still sends what you settled
        self.assertEqual(self.job()["location_raw"], "Remote - US")
        self.assertEqual(self.read(gh_item(1, "Claims Analyst", CLAIMS, "synth", location="Vancouver, BC"))["unchanged"], 0)
        self.assertEqual(self.job()["location_raw"], "Vancouver, BC")   # the board changed it: you should see it

    def test_a_pasted_jobs_own_link_is_compared_with_the_board_link_it_was_bound_to(self):
        pasted = "https://boards.greenhouse.io/synth/jobs/1?gh_src=mail"
        job = intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + CLAIMS, pasted)["job_id"]
        item = gh_item(1, "Claims Analyst", CLAIMS, "synth")
        self.read(item)   # its first board read, written into your job
        record = json.loads(self.store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job,))[0])
        self.assertEqual((record["url"], record["bindings"]["url"]), (pasted, item["absolute_url"]))
        self.assertEqual(self.read(item)["unchanged"], 1)
        moved = {**item, "absolute_url": "https://job-boards.greenhouse.io/synth/jobs/1?ref=moved"}
        self.assertEqual(self.read(moved)["unchanged"], 0)

    def test_a_spacing_only_edit_and_a_read_with_no_entry_are_the_same_read(self):
        item = gh_item(1, "Claims Analyst", CLAIMS, "synth")
        self.read(item)
        before = self.job()["source_json"]
        self.assertEqual(self.read(gh_item(1, "Claims  Analyst ", CLAIMS.replace(". ", ".   "), "synth"))["unchanged"], 1)
        counts = ingest(self.ctx, None, [greenhouse_posting("synth", item)], source="manual_url", full_board=False)
        self.assertEqual((counts["unchanged"], self.job()["source_json"]), (1, before))


class OneDerivationTests(unittest.TestCase):
    """Part 3c (section 3a, "One derivation"; section 3, "Unchanged reads"): every derived value comes from
    normalized_fields over the source record, and each processing run resolves a known employer entry again."""

    def setUp(self):
        self.store, self.ctx = setup()
        self.employer = dict(legacy_board(self.store, name="Synthetic Health", token="synth"))

    def record(self, job_id):
        return json.loads(self.store.one("SELECT source_json FROM jobs WHERE job_id = ?", (job_id,))[0])

    def assert_derived(self, job_id):
        job = self.store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        derived = pipeline.derive(self.ctx, self.record(job_id), job_id)
        self.assertEqual({k: job[k] for k in derived if k != "source_updated_at"},
                         {k: v for k, v in derived.items() if k != "source_updated_at"})

    def reasons(self, job_id):
        return [r[0] for r in self.store.all("SELECT reason_code FROM transitions WHERE entity_id = ? ORDER BY sequence",
                                             (job_id,))]

    def test_the_processing_run_derives_again_through_the_one_derivation(self):
        ingest(self.ctx, self.employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))])
        job_id = self.store.one("SELECT job_id FROM jobs")[0]
        pipeline.process(self.ctx)
        self.store.writer("discovery").update("jobs", {"country": "non_US", "remote_scope": "older reader",
                                                       "employment_type": "part_time"}, "job_id = ?", (job_id,))
        self.assertEqual(pipeline.refresh_derived_fields(self.ctx), 1)
        self.assert_derived(job_id)
        self.assertEqual(self.reasons(job_id)[-1], "derived_fields_refreshed")
        self.assertEqual(pipeline.refresh_derived_fields(self.ctx), 0)   # nothing left to derive

    def test_your_location_write_records_what_it_overrode(self):
        ingest(self.ctx, self.employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth",
                                                                             location="Toronto, ON"))])
        job_id = self.store.one("SELECT job_id FROM jobs")[0]
        set_location(self.ctx, job_id, "Remote - US")
        record = self.record(job_id)
        self.assertEqual((record["location"], record["location_source"], record["typed_countries"]), ("Remote - US", "argument", []))
        self.assertEqual(record["bindings"]["location"], "Toronto, ON")
        self.assert_derived(job_id)
        self.assertEqual(self.store.one("SELECT country FROM jobs WHERE job_id = ?", (job_id,))[0], "US")
        set_location(self.ctx, job_id, "Remote - United States")   # a second write keeps the field the first overrode
        self.assertEqual(self.record(job_id)["bindings"]["location"], "Toronto, ON")

    def test_a_copys_location_places_the_job_only_as_declared(self):
        """D190 rule 2, applied to the read: an older saved-file reader's location places nothing; yours does."""
        from careeros.discovery.saved import READER
        base = pipeline.source_record(RawPosting(ats="manual", board_token="synthetic-health", external_id="1",
                                                 title="Claims Analyst", location="Remote - US", description_text=CLAIMS),
                                      {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health",
                                       "resolved": "parent_for"})
        cases = (({"location_source": "file", "reader": "saved-older"}, "unknown"), ({"location_source": "file", "reader": READER}, "US"),
                 ({"location_source": "label"}, "unknown"),   # its text has no such Location line
                 ({"location_source": "argument"}, "US"))
        for raw, country in cases:
            with self.subTest(raw=raw):
                fields = pipeline.derive(self.ctx, {**base, "reader": None, **raw})
                self.assertEqual((fields["location_raw"], fields["country"]), ("Remote - US", country))

    def test_the_rescued_family_is_derived_from_the_title_rescues_decision(self):
        from careeros.qualify.title_rescue import RESCUE_VERSION
        ingest(self.ctx, self.employer, [greenhouse_posting("synth", gh_item(1, "Operations Associate", CLAIMS, "synth"))])
        job_id = self.store.one("SELECT job_id FROM jobs")[0]
        self.assertIsNone(pipeline.derive(self.ctx, self.record(job_id), job_id)["family"])
        self.store.writer("discovery").insert("title_reviews", {
            "job_id": job_id, "rules_version": RESCUE_VERSION, "reviewed_at": self.store.clock.now(), "outcome": "rescued",
            "family": "claims_analysis", "rule": "synthetic", "evidence": "synthetic", "original_reason": "no_target_family"})
        self.assertEqual(pipeline.derive(self.ctx, self.record(job_id), job_id)["family"], "claims_analysis")
        self.assertIsNone(pipeline.derive(self.ctx, self.record(job_id))["family"])   # no job: no rescue to read
        pipeline.refresh_derived_fields(self.ctx)
        self.assertEqual(self.store.one("SELECT family FROM jobs WHERE job_id = ?", (job_id,))[0], "claims_analysis")

    def test_a_policy_parents_change_reaches_a_pasted_job_at_the_processing_run(self):
        """Codex H19 for a job no one reads again: the parent comes from the policy, and the policy changed."""
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        job_id = intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + CLAIMS, None)["job_id"]
        pipeline.process(self.ctx)
        self.assertNotIn("employer_reresolved", self.reasons(job_id))
        data = {**self.ctx.policy.data, "parents": {"Synthetic Holdings": ["Synthetic Health"]}}
        ctx = pipeline.Context(self.store, PolicyBundle(data=data, fingerprint=fingerprint(data)), self.ctx.profile,
                               self.ctx.matrix)
        counts = pipeline.process(ctx)
        self.assertEqual(counts["entries_reresolved"], 1)
        self.assertEqual((self.record(job_id)["employer"]["parent"],
                          self.store.one("SELECT parent FROM jobs WHERE job_id = ?", (job_id,))[0]),
                         ("Synthetic Holdings", "Synthetic Holdings"))
        self.assertIn("employer_reresolved", self.reasons(job_id))
        self.assertNotIn("entries_reresolved", pipeline.process(ctx))   # the control: the same entry again

    def test_a_registry_change_reaches_a_board_job_and_your_parent_never_moves(self):
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        ingest(self.ctx, self.employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))])
        board = self.store.one("SELECT job_id FROM jobs")[0]
        ingest(self.ctx, employer_entry(self.ctx, name="Acme Health", parent="Acme Group"),
               [RawPosting(ats="manual", board_token="acme-health", external_id="2", title="Claims Analyst",
                           description_text=CLAIMS, raw={"location_source": "none"})], source="manual_url", full_board=False)
        yours = "manual:acme-health:2"
        pipeline.process(self.ctx)
        self.store.conn.execute("UPDATE employers SET parent = 'Synthetic Holdings' WHERE employer_id = ?",
                                (self.employer["employer_id"],))
        data = {**self.ctx.policy.data, "parents": {"Other Group": ["Acme Health"]}}
        ctx = pipeline.Context(self.store, PolicyBundle(data=data, fingerprint=fingerprint(data)), self.ctx.profile,
                               self.ctx.matrix)
        self.assertEqual(pipeline.process(ctx)["entries_reresolved"], 1)
        self.assertEqual(self.record(board)["employer"]["parent"], "Synthetic Holdings")
        self.assertEqual(self.record(yours)["employer"]["parent"], "Acme Group")   # your --parent stays yours


class ScreeningCurrencyTests(unittest.TestCase):
    """Part 3d (part c; Codex's design review Pass 6; the gate session's C9, C10, X1): each screening records the source
    record it judged, and a screening is current only for that record and the extraction it read."""

    def test_every_screening_records_the_source_record_it_judged(self):
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        postings = [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth")),     # the gate's decision
                    greenhouse_posting("synth", gh_item(2, "Claims Director", CLAIMS, "synth")),    # the title screen's
                    greenhouse_posting("synth", gh_item(3, "Claims Analyst", "Short text.", "synth"))]   # a review decision
        ingest(ctx, employer, postings)
        pipeline.process(ctx)
        for number in (1, 2, 3):
            job = store.one("SELECT job_id, source_json FROM jobs WHERE job_id = ?", (f"greenhouse:synth:{number}",))
            gate = store.one("SELECT source_digest FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC",
                             (job["job_id"],))
            self.assertEqual(gate["source_digest"], pipeline.source_digest(job["source_json"]), number)
            self.assertIsNotNone(gate["source_digest"])

    def test_a_screening_is_current_only_for_the_record_and_the_extraction_it_judged(self):
        from careeros.review.submission import _current
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))])
        pipeline.process(ctx)
        job_id = "greenhouse:synth:1"

        def current():
            job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            gate = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC", (job_id,))
            return _current(job, gate, ctx.policy, ctx.profile)
        self.assertIn(store.one("SELECT current_state FROM jobs")[0], ("eligible", "scored"))
        self.assertTrue(current())
        stored = store.one("SELECT source_json, extraction_fingerprint FROM jobs")
        # a write that changed the record but not the state: the binding is in the data
        store.conn.execute("UPDATE jobs SET source_json = ?", (stored["source_json"].replace("Claims Analyst", "Claims Lead"),))
        self.assertFalse(current())
        store.conn.execute("UPDATE jobs SET source_json = ?", (stored["source_json"],))
        self.assertTrue(current())
        store.conn.execute("UPDATE jobs SET extraction_fingerprint = 'another extraction'")   # requirements read again
        self.assertFalse(current())
        store.conn.execute("UPDATE jobs SET extraction_fingerprint = ?", (stored["extraction_fingerprint"],))
        self.assertTrue(current())
        latest = dict(store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC", (job_id,)))
        store.clock.advance(seconds=1)
        store.writer("qualify").insert("gate_results", {**latest, "result_id": "gate:older-engine", "at": store.clock.now(),
                                                        "source_digest": None})   # as an engine before 017 wrote it
        self.assertFalse(current())


class TargetTests(unittest.TestCase):
    """Part 4 (section 7; Codex H20): target_of is the only function that maps an incoming read to a job."""

    def setUp(self):
        self.store, self.ctx = setup()
        self.employer = dict(legacy_board(self.store, name="Synthetic Health", token="synth"))
        self.entry = employer_entry(self.ctx, name="Synthetic Health")

    def paste(self, url, text=CLAIMS):
        return intake(self.ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + text, url)

    def copy(self, url, external="R-1", text=CLAIMS):
        return RawPosting(ats="manual", board_token="synthetic-health", external_id=external, title="Claims Analyst",
                          location="Remote - US", url=url, apply_url=url, description_text=text,
                          raw={"location_source": "argument"})

    def board(self, number):
        return greenhouse_posting("synth", gh_item(number, "Claims Analyst", CLAIMS, "synth"))

    def test_every_route_resolves_one_posting_to_one_job(self):
        """Codex H20: the paste's exact-link lookup and ingest's placement could name different jobs for one posting."""
        link = "https://boards.greenhouse.io/synth/jobs/1?gh_src=mail"
        job = self.paste(link)["job_id"]
        self.assertEqual(pipeline.target_of(self.store, self.employer, self.board(1))[1], job)
        self.assertEqual(pipeline.target_of(self.store, self.entry, self.copy(link, external="other"))[1], job)
        self.assertEqual(self.paste(link, text=CLAIMS + " Edited since.")["job_id"], job)
        ingest(self.ctx, self.employer, [self.board(1)], full_board=False)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM jobs")[0], 1)

    def test_the_resolver_writes_nothing(self):
        self.paste("https://boards.greenhouse.io/synth/jobs/1?gh_src=mail")

        def counts():
            return {t: self.store.one(f"SELECT COUNT(*) FROM {t}")[0]
                    for t in ("jobs", "observations", "events", "exceptions", "opening_links")}
        before = counts()
        for read in (self.board(1), self.board(2), self.copy("https://careers.example.com/jobs/x", external="R-9")):
            pipeline.target_of(self.store, self.entry if read.ats == "manual" else self.employer, read)
        self.assertEqual(counts(), before)

    def test_another_posting_at_the_same_generated_id_gets_its_own(self):
        ingest(self.ctx, self.entry, [self.copy("https://careers.example.com/jobs/a")], source="manual_url", full_board=False)
        placed, target, collided = pipeline.target_of(
            self.store, self.entry, self.copy("https://careers.example.com/jobs/b", text=CLAIMS + " Another opening."))
        self.assertEqual(collided, "manual:synthetic-health:R-1")
        self.assertTrue(target.startswith("manual:synthetic-health:R-1~"), target)
        self.assertEqual(f"manual:synthetic-health:{placed.external_id}", target)

    def test_a_stale_duplicate_never_sends_a_read_to_another_posting(self):
        """The owner's choice (V1, D198): every job with the posting's identity is a duplicate whose original is another
        posting (an older engine's rule, until the processing run clears it): the read gets its own job."""
        from tests.test_intake_identity import twin
        original = self.paste("https://boards.greenhouse.io/synth/jobs/9")["job_id"]
        link = "https://boards.greenhouse.io/synth/jobs/7"
        twin(self.store, "manual:synthetic-health:stale", link)
        disc = self.store.writer("discovery")
        disc.update("jobs", {"duplicate_of": original}, "job_id = ?", ("manual:synthetic-health:stale",))
        disc.transition(self.ctx.matrix, "manual:synthetic-health:stale", "normalized")
        disc.transition(self.ctx.matrix, "manual:synthetic-health:stale", "duplicate", reason_code="older_engine_rule")
        self.assertEqual(pipeline.target_of(self.store, self.employer, self.board(7))[1], "greenhouse:synth:7")
        result = self.paste(link)
        self.assertEqual(result["status"], "added")
        self.assertNotIn(result["job_id"], (original, "manual:synthetic-health:stale"))

    def test_ingest_writes_the_job_the_resolver_names(self):
        self.paste("https://boards.greenhouse.io/synth/jobs/1?gh_src=mail")
        ingest(self.ctx, self.entry, [self.copy("https://careers.example.com/jobs/a")], source="manual_url", full_board=False)
        reads = [(self.employer, self.board(1)), (self.employer, self.board(2)),
                 (self.entry, self.copy("https://careers.example.com/jobs/b", text=CLAIMS + " Another opening."))]
        for employer, read in reads:
            with self.subTest(read=read.url):
                target = pipeline.target_of(self.store, employer, read)[1]
                placed: dict = {}
                ingest(self.ctx, employer, [read], source="manual_url", full_board=False, placed=placed)
                self.assertEqual(placed[f"{read.ats}:{read.board_token}:{read.external_id}"], target)


if __name__ == "__main__":
    unittest.main()
