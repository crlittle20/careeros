"""The red team's batch 3 on the D188 candidate (codex-review-2026-11-15/red-team-coordinator-2026-11-18/BATCH_3.md).

A. One posting, one record: every read of a posting (its board's listing, a fetch of its own link, a saved copy) is
   written into its job of record, so a re-identification, a retitle, a location and a closure reach the job that
   carries your label, whichever route read the posting first.
B. A label is checked at every use against the opening it was given for; a stale review card records nothing; a posting
   read but not stored holds the label; the label written last is in force. (A write by another engine after a
   rollback, B.6, is retired with schema 16's floor, the owner's D191: an older engine refuses the database.)
C. Placement: every location label is the location field; a JSON-LD place given by reference is read or the page is
   refused; an older saved row is held; a wide region that excludes a country is not placed abroad.
D. Individual fixes: a saved page's requisition from its text; an unreadable posting block refuses the page; alert
   card parsing; lead recheck held while agency settings are incomplete; older likely proposals re-derived; the lock.
Synthetic data only; nothing here reads the network."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli, pipeline
from careeros.core import locks
from careeros.discovery import board_discovery as bd
from careeros.discovery import boards, saved
from careeros.discovery.boards import ashby_posting, greenhouse_posting
from careeros.discovery.manual_intake import intake, label_location, location_declared
from careeros.normalize.posting import detect_country
from careeros.pipeline import ingest
from careeros.review import assist, packages
from careeros.review.applications import application_matrix
from careeros.review.labels import capture, record_label

from tests.test_assist import GREENHOUSE_FILL
from tests.test_intake_identity import BODY, twin
from tests.test_lead_resolution import legacy_board
from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store
from tests.test_v26_discovery import CLAIMS, gh_item, setup
from tests.helpers import give_label, stored_label

SYNTH = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}
LINK = "https://job-boards.greenhouse.io/synth/jobs/1"


def board_read(ctx, employer, title="Claims Analyst", req="R-1101", text=CLAIMS, full_board=False, token="synth", **kw):
    return ingest(ctx, employer, [greenhouse_posting(token, gh_item(1, title, text, token, req=req, **kw))],
                  full_board=full_board)


def jobs(store):
    return {r["job_id"]: dict(r) for r in store.all("SELECT job_id, current_state, duplicate_of, requisition_id, title, "
                                                    "closed_at, employer_id FROM jobs")}


class OneRecordTests(unittest.TestCase):
    """Group A: impossible by construction."""

    def test_a_board_read_of_a_posting_you_pasted_first_is_written_into_your_job(self):
        """gr1-paste-0, gr1-identity-0, gr1-saved-0: the board's later read was a duplicate whose reads went nowhere."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        mine = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\nJob ID: R-1101\n" + BODY,
                      LINK)["job_id"]
        board_read(ctx, employer, full_board=True)
        self.assertEqual(set(jobs(store)), {mine})   # no second row: the board's read is your job's
        board_read(ctx, employer, title="Senior Claims Analyst", req="R-2202", full_board=True)
        self.assertEqual((jobs(store)[mine]["requisition_id"], jobs(store)[mine]["title"]), ("R-2202", "Senior Claims Analyst"))
        # it keeps its own link, so it stays that posting, and the board's absence closes it
        self.assertEqual(bd.job_identity(store.one("SELECT * FROM jobs WHERE job_id = ?", (mine,))), ("greenhouse", "1"))
        for _ in range(2):
            ingest(ctx, employer, [], full_board=True)
        self.assertEqual(jobs(store)[mine]["current_state"], "closed")
        # a copy you save of it now is a sighting: the board's data stays
        self.assertTrue(pipeline.board_written(store, mine))

    def test_a_renamed_boards_listing_reaches_the_job_of_record_and_its_absence_closes_it(self):
        """gr1-boards-0: the new board's row was a duplicate, never re-identified or closed."""
        store, ctx = setup()
        old = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        new = dict(legacy_board(store, name="Acme Rx", token="acmehealth"))
        board_read(ctx, old, full_board=True, token="acmerx")
        board_read(ctx, new, title="Claims Analyst II", req="R-2202", full_board=True, token="acmehealth")
        self.assertEqual(set(jobs(store)), {"greenhouse:acmerx:1"})
        self.assertEqual(jobs(store)["greenhouse:acmerx:1"]["requisition_id"], "R-2202")
        self.assertEqual(jobs(store)["greenhouse:acmerx:1"]["employer_id"], "greenhouse:acmehealth")
        for _ in range(2):
            ingest(ctx, new, [], full_board=True)
        self.assertEqual(jobs(store)["greenhouse:acmerx:1"]["current_state"], "closed")

    def test_a_posting_first_read_by_its_own_link_is_closed_by_its_boards_absence(self):
        """gr1-boards-3: source manual_url kept it out of the closing loop."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))],
               source="manual_url", full_board=False)
        for _ in range(2):
            ingest(ctx, employer, [], full_board=True)
        self.assertEqual(jobs(store)["greenhouse:synth:1"]["current_state"], "closed")

    def test_an_unlisted_ashby_posting_stays_open_and_is_never_admitted_from_the_listing(self):
        """gr1-boards-1: the listing left it out, so two scans closed it for good."""
        def item(pid, listed):
            return {"id": pid, "title": "Claims Analyst", "location": "Remote - US", "isRemote": True, "isListed": listed,
                    "descriptionPlain": CLAIMS, "jobUrl": f"https://jobs.ashbyhq.com/acmerx/{pid}"}
        client = SimpleNamespace(get_json=lambda url: {"jobs": [item("a1", True), item("a2", False), item("a3", False)]})
        listing = boards.fetch_ashby(client, "acmerx")
        self.assertEqual([p.external_id for p in listing], ["a1"])
        self.assertEqual([p.external_id for p in listing.unlisted], ["a2", "a3"])
        store, ctx = setup()
        employer = {"employer_id": "ashby:acmerx", "name": "Acme Rx", "parent": "Acme Rx", "ats": "ashby"}
        store.writer("admin").insert("employers", {**employer, "board_token": "acmerx", "status": "verified"})
        ingest(ctx, employer, [ashby_posting("acmerx", item("a2", False))], source="manual_url", full_board=False)
        for _ in range(3):
            ingest(ctx, employer, [*listing, *listing.unlisted], full_board=True)
        self.assertEqual(jobs(store)["ashby:acmerx:a2"]["current_state"] not in ("closed",), True)
        self.assertNotIn("ashby:acmerx:a3", jobs(store))   # never admitted from the listing

    def test_a_closed_posting_read_open_at_its_own_link_reopens(self):
        """gr1-boards-1, the call recorded in FIXES_PASS14.md: a single fetch at its own link that reads it open."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        board_read(ctx, employer, full_board=True)
        for _ in range(2):
            ingest(ctx, employer, [], full_board=True)
        self.assertEqual(jobs(store)[JOB]["current_state"], "closed")
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))],
               source="manual_url", full_board=False)
        self.assertNotEqual(jobs(store)[JOB]["current_state"], "closed")

    def test_same_posting_twins_are_joined_at_every_run(self):
        """gr1-identity-3: a copy written while the board job was closed stayed a second open job."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        board_read(ctx, employer)
        twin(store, "manual:synthetic-health:twin", LINK)   # an older engine's twin: the same link, its own row
        counts = pipeline.rederive_identity(ctx)
        self.assertEqual(counts["twins_joined"], 1)
        self.assertEqual(jobs(store)["manual:synthetic-health:twin"]["duplicate_of"], JOB)

    def test_the_shortlist_refresh_writes_what_it_reads(self):
        """gr1-faults-1: the refresh fetched the re-identified posting and kept only last_seen_at."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Synthetic Health", token="synth"))
        board_read(ctx, employer)
        posting = greenhouse_posting("synth", gh_item(1, "Senior Claims Analyst", CLAIMS, "synth", req="R-2202"))
        with mock.patch.object(cli, "fetch_single", return_value=posting), \
                mock.patch("careeros.review.shortlist.shortlisted_job_ids", return_value=[JOB]):
            counts = cli._refresh_shortlisted(ctx, None, "2026-10-13T12:00:00.000Z")
        self.assertEqual(counts["shortlist_still_open"], 1)
        self.assertEqual(jobs(store)[JOB]["requisition_id"], "R-2202")


class LabelAtUseTests(unittest.TestCase):
    """Group B."""

    def setUp(self):
        self.vault = Vault()
        self.store, self.ctx = posting_store()
        for patch in (mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL),
                      mock.patch.object(assist, "vault_root_of", return_value=self.vault.root),
                      mock.patch.object(assist, "FILL_PAUSED", False)):   # the inert fill code (D191 pauses it)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.vault.close)

    def _open(self, key="o1"):
        give_label(self.store, POLICY, MID, JOB, "apply", None, f"label-{key}")
        packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        session = assist.open_session(self.store, POLICY, MID, JOB, "assisted", facts=FACTS, library=self.vault.library,
                                      vault_root=self.vault.root, questions=QUESTIONS, key=key)
        return self.vault.root / session["handoff"]

    def route(self):
        return packages.route_for(self.store, POLICY, JOB, MID)

    def test_a_requisition_moved_through_a_blank_suspends_the_label(self):
        """gr1-identity-1: R-1101 -> none -> R-2202 was two harmless-looking job_changes rows."""
        board_read(self.ctx, SYNTH, req="R-1101")
        pipeline.process(self.ctx)   # screened as it is now: a preview is given only then
        give_label(self.store, POLICY, MID, JOB, "apply", None, "l1")
        board_read(self.ctx, SYNTH, req=None)
        board_read(self.ctx, SYNTH, req="R-2202")
        route, why = self.route()
        self.assertIsNone(route)
        self.assertIn("requisition", why)
        board_read(self.ctx, SYNTH, req="R-1101")   # back to the opening you labelled: it stands again, once screened
        self.assertIsNone(self.route()[0])          # (P1: the screening waits for the processing run)
        pipeline.process(self.ctx)
        self.assertEqual(self.route()[0], "apply")

    def test_a_stale_review_card_records_nothing(self):
        """gr1-routes-0: Apply on a card built before the posting changed confirmed the new opening. The card's preview
        (the D190 design, section 3) no longer shows what the job is: refused."""
        shown = capture(self.store, POLICY, MID, JOB)
        board_read(self.ctx, SYNTH, title="Claims Analyst - Workers Compensation", req="R-2202")
        pipeline.process(self.ctx)   # the changed posting is screened again before any preview
        with self.assertRaisesRegex(ValueError, "job_changed"):
            record_label(self.store, JOB, "apply", None, "card-1", preview=shown, policy=POLICY, applicant=MID)
        self.assertIsNone(self.store.one("SELECT 1 FROM labels WHERE job_id = ?", (JOB,)))
        give_label(self.store, POLICY, MID, JOB, "apply", None, "card-2")   # from a fresh preview
        self.assertEqual(self.route()[0], "apply")

    def test_a_posting_read_but_not_stored_holds_the_label_until_it_is(self):
        """gr1-faults-0: the failed write left the old opening's label routing and its handoff loadable."""
        self._open()
        with mock.patch("careeros.openings.record_change", side_effect=RuntimeError("database or disk is full")):
            counts = board_read(self.ctx, SYNTH, title="Senior Claims Analyst", req="R-2202")
        self.assertEqual(counts["errors"], 1)
        route, why = self.route()
        self.assertIsNone(route)
        self.assertTrue(why.startswith("posting_not_stored:"), why)
        board_read(self.ctx, SYNTH, req=None)   # stored again, the same opening: the hold is resolved
        self.assertEqual(self.route()[0], "apply")

    def test_the_label_written_last_is_in_force_whatever_the_clock(self):
        """gr1-upgrade-1: an older engine's Skip behind a clock step lost to an earlier Apply."""
        give_label(self.store, POLICY, MID, JOB, "apply", None, "l1")
        self.store.writer("review").insert("labels", {"label_id": "label:old-engine", "job_id": JOB, "at": "2026-10-12T11:30:00.000Z",
                                                      "label": "skip", "reason": None, "system_tier": None,
                                                      "system_decision": "scored", "profile": "primary"})
        self.assertEqual(self.route(), (None, "you labelled it Skip"))

class PlacementTests(unittest.TestCase):
    """Group C."""

    def test_every_location_label_is_the_location_field(self):
        """gr1-saved-6: 'Other Locations:', 'Additional Locations:' and 'Locations:' were hints."""
        for label in ("Other Locations", "Additional Locations", "Locations", "Primary Location", "Location(s)"):
            with self.subTest(label=label):
                self.assertEqual(label_location(f"Location: Miami, FL\n{label}: Toronto, ON, Canada"),
                                 "Miami, FL; Toronto, ON, Canada")
        self.assertEqual(label_location("Location: Miami, FL\nLocation-based pay: $30"), "Miami, FL")

    def test_a_jsonld_place_given_by_reference_is_read_or_the_page_refused(self):
        """gr1-saved-2: a place, an address or a country given by @id was dropped."""
        def page(graph):
            return ('<html><head><link rel="canonical" href="https://careers.acme.example/jobs/1">'
                    '<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@graph": graph})
                    + "</script></head><body></body></html>")
        posting = {"@type": "JobPosting", "title": "Claims Analyst", "description": "<p>" + CLAIMS + "</p>",
                   "hiringOrganization": {"@type": "Organization", "name": "Acme Rx"},
                   "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Miami", "addressRegion": "FL",
                                                                  "addressCountry": "US"}}, {"@id": "#toronto"}]}
        toronto = {"@id": "#toronto", "@type": "Place", "address": {"@id": "#toronto-address"}}
        address = {"@id": "#toronto-address", "@type": "PostalAddress", "addressLocality": "Toronto", "addressRegion": "ON",
                   "addressCountry": "CA"}
        raw, _ = saved.parse_saved_html(page([posting, toronto, address]), "p.html")
        self.assertIn("Toronto", raw.location)
        with self.assertRaisesRegex(saved.SavedPostingError, "reference"):
            saved.parse_saved_html(page([posting, toronto]), "p.html")

    def test_an_older_saved_row_is_held_and_an_older_paste_is_not(self):
        """gr1-saved-1: a saved .txt body repeating the kept US line made the cut location look clean."""
        store, _ = setup()
        job = {"job_id": "manual:acme:1", "ats": "manual", "location_raw": "Remote - US",
               "description_text": "Location: Remote - US\n" + CLAIMS}
        self.assertFalse(location_declared(store, job))
        twin(store, "manual:acme:1", None, employer="Acme")
        from careeros.openings import record_observation
        record_observation(store, source_kind="human_text", evidence_level="human", job_id="manual:acme:1",
                           location="Remote - US", facts={})
        self.assertTrue(location_declared(store, job))

    def test_a_wide_region_that_excludes_a_country_is_not_placed_abroad(self):
        """gr1-boards-2."""
        for field in ("Remote - Worldwide (excluding India)", "Remote - North America (excl. Mexico)",
                      "Remote (Global) - not available in Canada"):
            with self.subTest(field=field):
                self.assertEqual(detect_country(field, "", "remote"), "unknown")
        self.assertEqual(detect_country("Remote - India", "", "remote"), "non_US")
        self.assertEqual(detect_country("Toronto, ON (excluding downtown)", "", "onsite"), "non_US")


class IndividualTests(unittest.TestCase):
    """Group D."""

    def test_a_saved_pages_requisition_is_read_from_its_text(self):
        """gr1-saved-4."""
        markup = ('<html><head><link rel="canonical" href="https://job-boards.greenhouse.io/acmerx/jobs/4400001">'
                  '<script type="application/ld+json">' + json.dumps({
                      "@type": "JobPosting", "title": "Claims Analyst", "hiringOrganization": {"name": "Acme Rx"},
                      "description": "<p>Job ID: R-1101</p><p>" + CLAIMS + "</p>"}) + "</script></head></html>")
        raw, _ = saved.parse_saved_html(markup, "p.html")
        self.assertEqual(raw.requisition_id, "R-1101")

    def test_an_unreadable_posting_block_refuses_the_page(self):
        """gr1-saved-5: the page's own block was skipped and another posting imported under its link."""
        other = json.dumps({"@type": "JobPosting", "title": "Billing Specialist", "hiringOrganization": {"name": "Acme Rx"},
                            "description": "<p>" + CLAIMS + "</p>"})
        markup = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Claims Analyst",\n'
                  '"description": "line one\nline two"}</script><script type="application/ld+json">' + other
                  + "</script></head></html>")
        with self.assertRaisesRegex(saved.SavedPostingError, "cannot be read"):
            saved.parse_saved_html(markup, "p.html")

    def test_an_alert_cards_own_parts_stay_its_own(self):
        """gr1-alerts-1: a contract length, a linked employer and details above the title were lost or misplaced."""
        from careeros.discovery.alerts import _links_from_html, stated_identity
        page, home = "https://careers.acme.example/jobs", "https://www.acme.example/"

        def table(cards):
            return "<html><body><table>" + "".join(f"<tr><td>{c}</td></tr>" for c in cards) + "</table></body></html>"
        layouts = (lambda d: f'<a href="{page}">Claims Analyst</a><br><a href="{home}">Acme</a><br>{d}',
                   lambda d: f'Acme<br>{d}<br><a href="{page}">Claims Analyst</a>',
                   lambda d: f'<a href="{page}">Claims Analyst</a><br>Acme<br>{d}')
        for card in layouts:
            found = [(text, stated_identity(context)) for href, text, context in _links_from_html(
                table([card("Calgary, AB"), card("Austin, TX")])) if href == page]
            self.assertEqual(found, [("Claims Analyst", "acme|calgary, ab|"), ("Claims Analyst", "acme|austin, tx|")])
        self.assertNotEqual(stated_identity("Acme · Contract · 6 months"), stated_identity("Acme · Contract · 12 months"))
        self.assertEqual(stated_identity("Acme · 3 days ago"), stated_identity("Acme · 5 days ago"))

    def test_the_lead_recheck_waits_while_an_agency_entry_is_missing(self):
        """gr1-alerts-2: every lead linked by its own exact link was reopened."""
        from careeros.discovery.lead_resolution import recheck_linked_leads
        store, ctx = setup()
        with mock.patch("careeros.discovery.alerts.agency_setting_problem", return_value="agency_setting_missing"):
            self.assertEqual(recheck_linked_leads(ctx), {"leads_recheck_held": 1})

    def test_a_lock_never_fully_taken_is_removed_and_an_unreadable_one_unlocks(self):
        """gr1-faults-3: a failed write of the lock's body left an empty lock that held every writer."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "writer.lock"
            with mock.patch.object(locks.json, "dump", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    locks.SingleWriterLock(path).acquire()
            self.assertFalse(path.exists())
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(locks.LockError, "lock_being_written"):
                locks.force_unlock(path)
            old = time.time() - 2 * locks.UNREADABLE_AFTER_SECONDS
            os.utime(path, (old, old))
            self.assertTrue(locks.force_unlock(path))
            self.assertFalse(path.exists())


class ConditionTests(unittest.TestCase):
    """The coordinator's batch 3 conditions on b08670e: every writing handler withdraws (by construction), twins whose
    decisions differ are held apart, the sweep fails closed (with or without the writer lock), a reopened posting is
    screened again before any route, and the rollback rule's records."""

    def setUp(self):
        self.vault = Vault()
        self.addCleanup(self.vault.close)
        for patch in (mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL),
                      mock.patch.object(assist, "vault_root_of", return_value=self.vault.root),
                      mock.patch.object(assist, "FILL_PAUSED", False)):   # the inert fill code (D191 pauses it)
            patch.start()
            self.addCleanup(patch.stop)

    def _session(self, store, job, key="o1"):
        give_label(store, POLICY, MID, job, "apply", None, f"label-{key}")
        packages.prepare(store, POLICY, MID, job, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        out = assist.open_session(store, POLICY, MID, job, "assisted", facts=FACTS, library=self.vault.library,
                                  vault_root=self.vault.root, questions=QUESTIONS, key=key)
        handoff = self.vault.root / out["handoff"]
        self.assertTrue(handoff.exists())
        return out["session_id"], handoff

    def _manual(self):
        store, ctx = setup()
        job = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\nLocation: Remote - US\n\n" + CLAIMS, LINK)["job_id"]
        pipeline.process(ctx)
        return store, ctx, job

    def test_twins_with_different_decisions_are_held_apart_and_suspended(self):
        """Condition 4: a join never picks between your decisions."""
        store, ctx = setup()
        board_read(ctx, SYNTH)
        pipeline.process(ctx)
        twin(store, "manual:synthetic-health:twin", LINK)
        pipeline.process(ctx)   # no decisions on the twin: joined
        self.assertEqual(jobs(store)["manual:synthetic-health:twin"]["duplicate_of"], JOB)
        store, ctx = setup()
        board_read(ctx, SYNTH)
        pipeline.process(ctx)
        give_label(store, POLICY, MID, JOB, "apply", None, "l1")
        twin(store, "manual:synthetic-health:twin", LINK)
        record_label(store, "manual:synthetic-health:twin", "skip", None, "l2")
        counts = pipeline.rederive_identity(ctx)
        self.assertEqual(counts["twins_held"], 1)
        self.assertIsNone(jobs(store)["manual:synthetic-health:twin"]["duplicate_of"])
        route, why = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertTrue(why.startswith("same_posting_twins:"), why)
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate'"))
        # you make them agree: they are one job, the one carrying your decision. The twin is an older engine's row,
        # with no source record until the upgrade, so its Apply is a stored label (it never routes by itself)
        stored_label(store, "manual:synthetic-health:twin", "apply", None, "l3")
        self.assertEqual(pipeline.rederive_identity(ctx)["twins_joined"], 1)

    def test_a_reopened_posting_is_screened_again_before_any_route(self):
        """Condition 5: a closed posting read open at its own link routes only after the next processing run."""
        store, ctx = posting_store()
        give_label(store, POLICY, MID, JOB, "apply", None, "l1")
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")
        with store.tx():
            store.writer("discovery").update("jobs", {"closed_at": store.clock.now()}, "job_id = ?", (JOB,))
            store.writer("discovery").transition(ctx.matrix, JOB, "closed", reason_code="no_longer_listed")
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))],
               source="manual_url", full_board=False)
        route, why = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertTrue(why.startswith("posting_not_screened:"), why)
        # a screen that leaves it in needs_review is a screen: the usual rules decide from there (the owner's D166 Q3)
        with store.tx():
            store.writer("extract").transition(ctx.matrix, JOB, "needs_review", reason_code="requirements_unclear")
        self.assertFalse(packages.route_for(store, POLICY, JOB, MID)[1].startswith("posting_not_screened"))
        pipeline.process(ctx)
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")

    def test_a_copy_that_states_no_location_keeps_the_stored_one(self):
        """The coordinator's fuzzer seed 68 (the call recorded with batch 3)."""
        from careeros.normalize.posting import RawPosting
        store, ctx, job = self._manual()
        ingest(ctx, SYNTH, [RawPosting(ats="manual", board_token="synthetic-health", external_id="again",
                                       title="Claims Analyst", location="", url=LINK, apply_url=LINK,
                                       description_text=CLAIMS + "\nAgain.", raw={"location_source": "none"})],
               source="manual_url", full_board=False)
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job,))[0], "Remote - US")


class RoundEighteenTests(unittest.TestCase):
    """Rules the round-18 hand check found reached by no test once batch 3 changed the routes around them."""

    def test_a_link_naming_a_duplicate_names_its_original(self):
        """An older engine's second row for a posting, joined as its duplicate: a line at its link is the original."""
        store, ctx = posting_store()
        twin(store, "manual:synthetic-health:twin", "https://boards.greenhouse.io/synth/jobs/1")
        pipeline.rederive_identity(ctx)
        self.assertEqual(jobs(store)["manual:synthetic-health:twin"]["duplicate_of"], JOB)
        self.assertEqual(cli._known_job(ctx, "https://boards.greenhouse.io/synth/jobs/1"), JOB)

    def test_a_real_duplicate_is_left_alone_at_every_run(self):
        """D187 under batch 3: a duplicate that is its original's posting is never re-opened and joined again."""
        store, ctx = posting_store()
        twin(store, "manual:synthetic-health:twin", LINK)
        pipeline.process(ctx)
        before = store.one("SELECT current_state, state_sequence FROM jobs WHERE job_id = 'manual:synthetic-health:twin'")
        for _ in range(2):
            pipeline.process(ctx)
        self.assertEqual(tuple(store.one("SELECT current_state, state_sequence FROM jobs "
                                         "WHERE job_id = 'manual:synthetic-health:twin'")), tuple(before))
        self.assertEqual(before["current_state"], "duplicate")

    def test_a_handoff_with_no_session_goes_at_a_sweep_that_owes_nothing(self):
        """RT-02 under batch 3: the sweep itself removes a file with no session, not only a debt's withdrawal."""
        vault = Vault()
        self.addCleanup(vault.close)
        store, ctx = posting_store()
        folder = vault.root / "handoff"
        folder.mkdir(exist_ok=True)
        orphan = folder / "ses_00000000000000aa.json"
        orphan.write_text("{}", encoding="utf-8")
        assist.withdraw_handoffs(store, POLICY, MID, vault.root)
        self.assertFalse(orphan.exists())


class RoundEighteenMoreTests(unittest.TestCase):
    """The round-18 hand check's other survivors: batch-3 rules each reached by no test of its own."""

    def setUp(self):
        self.vault = Vault()
        self.addCleanup(self.vault.close)
        for patch in (mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL),
                      mock.patch.object(assist, "vault_root_of", return_value=self.vault.root),
                      mock.patch.object(assist, "FILL_PAUSED", False)):   # the inert fill code (D191 pauses it)
            patch.start()
            self.addCleanup(patch.stop)

    def _session(self, store):
        give_label(store, POLICY, MID, JOB, "apply", None, "label-o1")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        out = assist.open_session(store, POLICY, MID, JOB, "assisted", facts=FACTS, library=self.vault.library,
                                  vault_root=self.vault.root, questions=QUESTIONS, key="o1")
        return self.vault.root / out["handoff"]

    def test_a_renamed_boards_unchanged_listing_moves_the_job_to_it(self):
        """gr1-boards-0: the same content under a renamed board moves the job to that board, whose absence closes it."""
        store, ctx = setup()
        old = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        new = dict(legacy_board(store, name="Acme Rx", token="acmehealth"))
        board_read(ctx, old, full_board=True, token="acmerx")
        board_read(ctx, new, full_board=True, token="acmehealth")   # the same title, place and text
        self.assertEqual(jobs(store)["greenhouse:acmerx:1"]["employer_id"], "greenhouse:acmehealth")
        for _ in range(2):
            ingest(ctx, new, [], full_board=True)
        self.assertEqual(jobs(store)["greenhouse:acmerx:1"]["current_state"], "closed")

    def test_a_requisition_only_the_text_states_is_part_of_the_opening(self):
        """gr1-saved-4: a board posting with no requisition field whose text moves 'Job ID: R-1101' to R-2202."""
        store, ctx = setup()
        board_read(ctx, SYNTH, req=None, text=CLAIMS + "\n\nJob ID: R-1101")
        pipeline.process(ctx)
        give_label(store, POLICY, MID, JOB, "apply", None, "l1")
        board_read(ctx, SYNTH, req=None, text=CLAIMS + "\n\nJob ID: R-2202")
        route, why = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertIn("R-1101 -> R-2202", why)

    def test_add_urls_board_branch_sweeps(self):
        """gr1-saved-3: careeros add-url <board link> sweeps however it ends, an unchanged read included."""
        import contextlib
        import io

        from careeros.paths import vault_paths
        store, ctx = posting_store()
        orphan = self.vault.root / "handoff" / "ses_00000000000000bb.json"
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_text("{}", encoding="utf-8")
        posting = greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth"))
        with mock.patch.object(cli, "fetch_single", return_value=posting), \
                mock.patch.object(cli, "HttpClient", return_value=mock.MagicMock()), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_add_url(ctx, vault_paths(self.vault.root), SimpleNamespace(
                url=LINK, text_file=None, employer="Synthetic Health", parent=None, title=None, location=None))
        self.assertFalse(orphan.exists())

    def test_an_older_engines_likely_match_the_rules_now_forbid_is_never_shown(self):
        """gr1-alerts-3: a lead whose own link names one LinkedIn job, proposed another by an older engine."""
        from careeros.discovery.lead_resolution import _match_row
        from careeros.review.leads import pending
        from tests.test_v26_discovery import add_lead
        store, ctx = setup()
        job = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY,
                     "https://www.linkedin.com/jobs/view/4012300111")["job_id"]
        add_lead(store, "lead:own", "Claims Analyst", "Acme Rx · Remote", url="https://www.linkedin.com/jobs/view/4012300222")
        store.writer("discovery").insert("lead_matches", _match_row(
            "lead:own", job, None, "title_location", "likely", [job], store.clock.now(), actor="careeros",
            reason="an older engine's proposal"))
        self.assertIsNone(pending(store, "lead:own", POLICY))

    def test_a_dismissal_written_later_wins_whatever_the_clock(self):
        """Call 6: an older engine's dismissal written after a reinstatement, behind a clock step, is in force."""
        from careeros.review.dismiss import dismiss, is_dismissed, reinstate
        store, ctx = posting_store()
        dismiss(store, JOB, "pay")
        store.clock.advance(minutes=10)
        reinstate(store, JOB)
        store.writer("review").insert("dismissals", {"dismissal_id": "dis:older-engine", "job_id": JOB, "reason_code": "pay",
                                                     "reason_text": None, "at": "2026-10-12T11:30:00.000Z", "actor": "user"})
        self.assertTrue(is_dismissed(store, JOB))
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID), (None, "you dismissed it"))


class FillPausedTests(unittest.TestCase):
    """Part 1 of the owner's D192 build: browser Fill is paused in this release (D191, after Codex's D190 design review
    H1). Assisted mode is refused in code, a replay of an earlier assisted open hands no handoff over, and every sweep
    removes every handoff file with no database read."""

    def setUp(self):
        self.vault = Vault()
        self.addCleanup(self.vault.close)
        self.store, self.ctx = posting_store()
        give_label(self.store, POLICY, MID, JOB, "apply", None, "label-1")
        packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)

    def _open(self, mode, key):
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            return assist.open_session(self.store, POLICY, MID, JOB, mode, facts=FACTS, library=self.vault.library,
                                       vault_root=self.vault.root, questions=QUESTIONS, key=key)

    def _stray(self, name="ses_00000000000000ff.json"):
        folder = self.vault.root / "handoff"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text("{}", encoding="utf-8")
        return folder / name

    def test_assisted_mode_is_refused_whatever_the_site_policy_says(self):
        self.assertTrue(assist.FILL_PAUSED)
        with self.assertRaisesRegex(assist.SessionRefused, "assisted_paused"):
            self._open("assisted", "o1")
        self.assertIsNone(self.store.one("SELECT 1 FROM assist_sessions"))
        self.assertFalse((self.vault.root / "handoff").is_dir() and any((self.vault.root / "handoff").iterdir()))
        self.assertEqual(self._open("manual", "o1")["mode"], "manual")   # the refusal kept no receipt for the key

    def test_a_replay_of_an_earlier_assisted_open_hands_no_handoff_over(self):
        with mock.patch.object(assist, "FILL_PAUSED", False):   # opened before the upgrade
            first = self._open("assisted", "o1")
        self.assertIn("handoff", first)
        again = self._open("assisted", "o1")
        self.assertTrue(again["replay"])
        self.assertNotIn("handoff", again)
        self.assertIn("paused", again["handoff_withdrawn"])
        self.assertFalse((self.vault.root / first["handoff"]).exists())
        assist.close_session(self.store, application_matrix(), POLICY, first["session_id"], "not_submitted", key="c1")

    def test_every_sweep_removes_every_handoff_file(self):
        from careeros.review.server import _swept
        with mock.patch.object(assist, "FILL_PAUSED", False):
            opened = self.vault.root / self._open("assisted", "o1")["handoff"]
        stray = self._stray()
        self.assertEqual(assist.withdraw_handoffs(self.store, POLICY, MID, self.vault.root), 2)   # a live session's too
        self.assertFalse(opened.exists() or stray.exists())
        stray = self._stray()
        self.assertEqual(cli._sweep_unlocked({"root": self.vault.root}), 1)   # no database read: refused at the lock
        stray = self._stray()
        with mock.patch.object(assist, "vault_root_of", return_value=self.vault.root), _swept(self.store, POLICY, MID):
            pass   # around every review-page action
        self.assertFalse(stray.exists())
        stray = self._stray()
        import contextlib
        import io

        from careeros.paths import vault_paths
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_dismiss(self.ctx, vault_paths(self.vault.root), SimpleNamespace(job_id=JOB, reason="pay", note=None,
                                                                                     undo=False))
        self.assertFalse(stray.exists())


if __name__ == "__main__":
    unittest.main()
