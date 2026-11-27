"""The owner's D188 (2026-11-19): "Exact links only" and "Re-confirm after upgrade", with the red team's batch 2 and its
addendum on 72edf23 (codex-review-2026-11-15/red-team-coordinator-2026-11-18/BATCH_2.md, BATCH_2_ADDENDUM.md).

Identity: two items are one job (a duplicate, a lead link, "already tracked", "seen before", an application held) only
when both carry the same exact posting link: one board posting (Greenhouse, Lever, Ashby, Workable, Workday) or one job
site's id (LinkedIn, Indeed, Glassdoor). A requisition, a title, a place, a text or a careers-page link only suggests.
Labels: an Apply or Stretch label given before the upgrade waits for you to give it again; any Skip, dismissal, other
label or re-identified posting withdraws the job's open handoffs. Synthetic data only; nothing here reads the network."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from careeros import cli, pipeline
from careeros.discovery import board_discovery as bd
from careeros.discovery.lead_resolution import _match_row, recheck_linked_leads
from careeros.discovery.manual_intake import intake, location_declared
from careeros.normalize.posting import RawPosting
from careeros.pipeline import ingest, process
from careeros.review import assist, packages

from tests.helpers import give_label
from tests.test_assist import GREENHOUSE_FILL
from tests.test_intake_identity import BODY
from tests.test_lead_resolution import lead_row, legacy_board
from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store
from tests.test_v26_discovery import CLAIMS, add_lead, setup

SYNTH = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}


def board_job(external, url, *, title="Claims Analyst", text=CLAIMS, req=None, ats="greenhouse", token="acmerx"):
    """A posting a board's listing gives, with whatever link its API stores."""
    return RawPosting(ats=ats, board_token=token, external_id=str(external), title=title, location="Remote - US", url=url,
                      apply_url=url, description_text=text, requisition_id=req, raw={})


class ExactLinkIdentityTests(unittest.TestCase):
    """Group A of batch 2: impossible by construction."""

    def test_a_board_posting_is_its_board_id_whatever_link_its_api_stores(self):
        """fr1-boards-0: Greenhouse's API can store the employer's page (?gh_jid=) and Workable a short link; two postings
        with such links were one job. A board-scanned job is its own board id."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        page = "https://www.acmerx.example/careers?gh_jid="
        ingest(ctx, employer, [board_job(4400001, page + "4400001"), board_job(4400002, page + "4400002")], full_board=False)
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE current_state = 'duplicate'")[0], 0)
        self.assertEqual(bd.job_identity(store.one("SELECT * FROM jobs WHERE job_id = 'greenhouse:acmerx:4400002'")),
                         ("greenhouse", "4400002"))
        # the posting's own board link names it, so a lead there joins it
        self.assertEqual(cli._known_job(ctx, "https://job-boards.greenhouse.io/acmerx/jobs/4400002"), "greenhouse:acmerx:4400002")
        # a Workable posting is its shortcode, its short link included
        self.assertEqual(bd.job_identity({"ats": "workable", "external_id": "AB12CD34EF", "url": "https://apply.workable.com/j/AB12CD34EF"}),
                         ("workable", "ab12cd34ef"))
        self.assertEqual(bd.posting_identity("https://apply.workable.com/j/AB12CD34EF"), ("workable", "ab12cd34ef"))

    def test_a_link_that_only_contains_a_postings_id_is_not_that_posting(self):
        """The lookup by id reads every candidate's own identity: a careers page whose link holds the digits is another."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        ingest(ctx, employer, [board_job(4400001, "https://job-boards.greenhouse.io/acmerx/jobs/4400001")], full_board=False)
        intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY,
               "https://careers.acmerx.example/jobs/4400001-claims-analyst")
        self.assertEqual(cli._known_job(ctx, "https://job-boards.greenhouse.io/acmerx/jobs/4400001"), "greenhouse:acmerx:4400001")
        self.assertEqual(len(bd.jobs_with_identity(store, ("greenhouse", "4400001"))), 1)

    def test_a_copy_you_add_first_is_never_a_wildcard(self):
        """fr1-identity-0: a LinkedIn or linkless copy added first made two board postings its duplicates."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY, "https://www.linkedin.com/jobs/view/4000000111")
        intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY + "\nAgain.", None)
        ingest(ctx, employer, [board_job(4400001, "https://job-boards.greenhouse.io/acmerx/jobs/4400001", text=BODY, req="R-1101"),
                               board_job(4400002, "https://job-boards.greenhouse.io/acmerx/jobs/4400002", text=BODY, req="R-2202")],
               full_board=False)
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE current_state = 'duplicate'")[0], 0)
        for url in ("https://www.linkedin.com/jobs/view/4000000222", "https://job-boards.greenhouse.io/acmerx/jobs/4400002"):
            out = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY + "\n" + url[-4:], url)
            self.assertNotEqual(out["status"], "added" if "greenhouse" in url else "already_tracked", url)

    def test_a_lead_naming_one_board_posting_is_never_proposed_another(self):
        """RT-19 and fr1-boards-1: a lead at its own Greenhouse link was linked, or proposed, by title to another posting."""
        posting = bd.TrackedPosting("greenhouse:acmerx:4400001", "Claims Analyst", "https://job-boards.greenhouse.io/acmerx/jobs/4400001",
                                    None, "4400001", "Remote - US", "remote")
        lead = bd.Candidate(employer_name="Acme Rx", origin="alert", title="Claims Analyst", location="Remote - US",
                            posting_url="https://boards.greenhouse.io/acmerx/jobs/4400002")
        self.assertEqual(bd.match_lead(lead, [posting], POLICY, "2026-11-19T00:00:00.000Z")["decision"], "no_match")
        same = bd.Candidate(employer_name="Acme Rx", origin="alert", title="Another title", location="Toronto, ON",
                            posting_url="https://boards.greenhouse.io/other/jobs/4400001")   # its own posting, any wording
        self.assertEqual(bd.match_lead(same, [posting], POLICY, "2026-11-19T00:00:00.000Z")["decision"], "matched")
        # an embedded board's posting (its board's API stores the employer's page) is read by its board id too
        embedded = bd.TrackedPosting("greenhouse:acmerx:4400001", "Claims Analyst", "https://www.acmerx.example/careers?gh_jid=4400001",
                                     None, "4400001", "Remote - US", "remote")
        self.assertEqual(bd.match_lead(lead, [embedded], POLICY, "2026-11-19T00:00:00.000Z")["decision"], "no_match")

    def test_another_sites_line_is_never_seen_before_by_its_title_but_an_agency_number_is(self):
        """fr1-identity-1: one earlier job-site lead hid two board postings' lines. An agency's own job number is a
        possible repeat of that agency's card, never "seen" (Codex's D190 design review H8; D192 part 3)."""
        store, ctx = setup()
        add_lead(store, "lead:li", "Claims Analyst", "Acme Rx · Remote · req=R-1101", url="https://www.linkedin.com/jobs/view/4000000111")
        line = SimpleNamespace(url="https://www.indeed.com/viewjob?jk=a1b2c3d4e5f6", text="Claims Analyst",
                               context="Acme Rx · Remote · req=R-1101", site="indeed")
        self.assertFalse(cli._ledger_lead(ctx, line, "claims analyst"))
        store.writer("discovery").insert("leads", {
            "lead_id": "lead:agency:x1", "url": "https://click.agency.example/t/1", "site": "other", "title": "Claims Analyst",
            "title_norm": "claims analyst", "family": "claims_analysis", "context": "Harbor Staffing · Remote · req=04530-0012345678",
            "sender": "agency", "subject": None, "received_at": None, "first_seen_at": store.clock.now(), "status": "new",
            "job_id": None, "profile": "primary"})
        agency_line = SimpleNamespace(url="https://www.linkedin.com/jobs/view/4055555555", text="Claims Analyst",
                                      context="Harbor Staffing · Remote · req=04530-0012345678", site="linkedin")
        self.assertFalse(cli._ledger_lead(ctx, agency_line, "claims analyst"))   # never "seen" (H8)
        self.assertEqual(cli._agency_job(ctx, agency_line), "lead:agency:x1")   # a possible repeat, shown on the lead
        other = SimpleNamespace(**{**vars(agency_line), "context": "Harbor Staffing · Remote · req=04530-0099999999"})
        self.assertIsNone(cli._agency_job(ctx, other))   # another job number


class PasteProposalTests(unittest.TestCase):
    """A posting you paste proposes itself (a likely match) only to a lead of its own employer, never to an agency's lead
    or another employer's, and never to a lead at its own shared page's link (Codex H12, H16), tracking parameters aside
    (the round-17 hand check: these rules were reached by no test once D188 made them proposals)."""

    def _proposed(self, context, url):
        from careeros.discovery.lead_resolution import link_same_posting
        from tests.test_lead_resolution import proposed
        store, ctx = setup()
        add_lead(store, "lead:x", "Claims Analyst", context, url=url)
        job = intake(ctx, "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n" + BODY,
                     "https://careers.foohealth.example/jobs")
        link_same_posting(ctx, job["job_id"], via="pasted")
        return lead_row(store, "lead:x")["job_id"], proposed(store, "lead:x")

    def test_only_its_own_employers_lead_is_proposed(self):
        other_link = "https://www.linkedin.com/jobs/view/4000000901"
        linked, proposal = self._proposed("Foo Health \u00b7 Remote", other_link)
        self.assertIsNone(linked)
        self.assertTrue(proposal)   # the control: the same employer and title
        for context in ("Bar Health \u00b7 Remote", "Foo Health \u00b7 Remote \u00b7 agency=Harbor Staffing"):
            with self.subTest(context=context):
                self.assertEqual(self._proposed(context, other_link), (None, None))

    def test_a_lead_at_the_pages_own_link_is_never_proposed_it(self):
        for url in ("https://careers.foohealth.example/jobs", "https://careers.foohealth.example/jobs?utm_source=alert"):
            with self.subTest(url=url):
                self.assertEqual(self._proposed("Foo Health \u00b7 Remote", url), (None, None))


class RoundSeventeenTests(unittest.TestCase):
    """Rules the round-17 hand check found reached by no test once D188 changed the routes around them."""
    REVIEW = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"

    def _alert(self, ctx, url, title="Claims Analyst", context="Acme Rx \u00b7 Remote"):
        from careeros.discovery.alerts import AlertEmail, Link
        with mock.patch.object(cli, "fetch_single", side_effect=AssertionError("fetched")) as fetched:
            counts = cli._record_alert(ctx, AlertEmail("s", "t", None, [Link(url, "other", title, context, True)]), mock.MagicMock())
        return counts, fetched.call_count

    def test_a_review_link_never_counts_as_an_agency_cards_job_seen_before(self):
        """D127 B3 kept under D188: a link kept for review is never seen before, even naming an agency's job number."""
        store, ctx = setup()
        store.writer("discovery").insert("leads", {
            "lead_id": "lead:agency:x1", "url": "https://click.agency.example/t/1", "site": "other", "title": "Claims Analyst",
            "title_norm": "claims analyst", "family": "claims_analysis", "context": "Harbor Staffing \u00b7 Remote \u00b7 req=04530-0012345678",
            "sender": "agency", "subject": None, "received_at": None, "first_seen_at": store.clock.now(), "status": "new",
            "job_id": None, "profile": "primary"})
        line = SimpleNamespace(url=self.REVIEW, text="Claims Analyst", context="Harbor Staffing \u00b7 Remote \u00b7 req=04530-0012345678",
                               site="linkedin")
        self.assertFalse(cli._ledger_lead(ctx, line, "claims analyst"))

    def test_no_greenhouse_posting_is_guessed_from_a_review_link_an_agency_link_or_past_a_route(self):
        """CR-HF-01, the agency merge and Pass 14 H35: a review link's gh_jid, an agency tracking link's, and one past a "#"
        route are never fetched as a board posting, for a posting not tracked yet too."""
        from tests.test_alerts import AGENCY_TRACKER
        from careeros.discovery.alerts import enroll_agency_hosts
        store, ctx = setup()
        enroll_agency_hosts(store)
        for url in (self.REVIEW + "&gh_jid=4400077", AGENCY_TRACKER + "?token=t9&gh_jid=4400077",
                    "https://xn--scnner-cua.example.net/rd?gh_jid=4400077",   # a host kept local (D153 R5, D156)
                    "https://careers.acmerx.example/portal?gh_jid=4400077#/opening/R-2222"):
            with self.subTest(url=url):
                counts, fetched = self._alert(ctx, url)
                self.assertEqual(fetched, 0)
                self.assertFalse(counts.get("lead_ingested"))
        # since D192 part 3 a careers page's gh_jid is a suggestion on its notice, never fetched, with no route too
        counts, fetched = self._alert(ctx, "https://careers.acmerx.example/portal?gh_jid=4400078")
        self.assertEqual((fetched, counts.get("notice_new")), (0, 1))

    def test_a_duplicate_pasted_again_lends_a_later_lead_to_its_original(self):
        """A lead that arrives after a duplicate exists, linked when the duplicate is added again, joins its original."""
        from careeros.discovery.lead_resolution import link_same_posting
        store, ctx = setup()
        view, search = "https://www.linkedin.com/jobs/view/4000000901", "https://www.linkedin.com/jobs/search/?currentJobId=4000000901"
        original = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY, view)["job_id"]
        copy = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY + "\nAgain.", search)["job_id"]
        self.assertEqual(copy, original)   # one posting: the paste is that job
        # an older engine's second row for it (batch 3 writes none), joined as its duplicate at the processing run
        from tests.test_intake_identity import twin
        twin(store, "manual:acme-rx:copy", search, employer="Acme Rx")
        pipeline.rederive_identity(ctx)
        self.assertEqual(store.one("SELECT duplicate_of FROM jobs WHERE job_id = 'manual:acme-rx:copy'")[0], original)
        add_lead(store, "lead:late", "Claims Analyst", "Acme Rx \u00b7 Remote", url=search)
        link_same_posting(ctx, "manual:acme-rx:copy", via="added")
        self.assertEqual(lead_row(store, "lead:late")["job_id"], original)

    def test_a_placeholder_requisition_suggests_nothing(self):
        """RT-16 under D188: two board postings under other titles, each "TBD", are not even linked for review."""
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        ingest(ctx, employer, [board_job(3, "https://job-boards.greenhouse.io/acmerx/jobs/3", title="Claims Analyst", req="TBD"),
                               board_job(4, "https://job-boards.greenhouse.io/acmerx/jobs/4", title="Billing Specialist", req="TBD",
                                         text=CLAIMS + "\nBilling.")], full_board=False)
        self.assertIsNone(store.one("SELECT 1 FROM opening_links"))
        ingest(ctx, employer, [board_job(5, "https://job-boards.greenhouse.io/acmerx/jobs/5", title="Pricing Analyst", req="R-4401",
                                         text=CLAIMS + "\nPricing."),
                               board_job(6, "https://job-boards.greenhouse.io/acmerx/jobs/6", title="Rebate Analyst", req="R-4401",
                                         text=CLAIMS + "\nRebates.")], full_board=False)
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate'"))   # a real one suggests

    def test_one_workday_requisition_under_two_locations_is_two_postings(self):
        """rtw_fr1upgrade_7 (the coordinator's go): a Workday link's location and title segments are part of its posting."""
        base = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/"
        calgary, us = base + "Calgary-AB/Claims-Analyst_R-12345", base + "US-Remote/Claims-Analyst_R-12345"
        self.assertNotEqual(bd.posting_identity(calgary), bd.posting_identity(us))
        self.assertEqual(bd.posting_identity(calgary),
                         bd.posting_identity("https://acme.wd5.myworkdayjobs.com/careers/job/Calgary-AB/Claims-Analyst_R-12345"))
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY, us)
        add_lead(store, "lead:calgary", "Claims Analyst", "Acme Rx \u00b7 Calgary, AB", url=calgary)
        from careeros.discovery.lead_resolution import link_same_posting
        link_same_posting(ctx, first["job_id"], via="pasted")
        self.assertIsNone(lead_row(store, "lead:calgary")["job_id"])

    def test_a_retry_whose_link_adds_tracking_after_the_route_is_the_same_submission(self):
        """Pass 8 H7 kept: a tracking parameter after "#" is not part of the stored link, so a retry with one is the same."""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from careeros.paths import vault_paths
        portal = "https://jobs.example.com/portal/#/opening/R-1101"
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            text = P(tmp) / "posting.txt"
            text.write_text(BODY, encoding="utf-8")
            for url in (portal, portal + "?utm_source=email"):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_add_url(ctx, vault_paths(P(tmp)), SimpleNamespace(url=url, text_file=str(text), employer="Acme Rx",
                                                                              parent=None, title="Claims Analyst",
                                                                              location="Remote - US"))
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)


class SameSubmissionTests(unittest.TestCase):
    """A retry is the same request made again (RT-06 kept under D188), recognised by its operation's receipt since the
    D190 design (section 6), never two postings matched."""

    def test_a_colliding_posting_that_comes_again_reuses_the_id_it_was_given(self):
        """H11 (Pass 9) kept: a posting given a suffixed id beside another is that job again when it comes again
        unchanged: its retry, by its receipt."""
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: A&B Health\nJob Number: R-4001\n\n" + BODY, None, location="Remote - US")
        text = "Claims Analyst\nCompany: A B Health\nJob Number: R-4001\n\n" + BODY + "\nAnother team."
        second = intake(ctx, text, None, location="Remote - US")
        self.assertNotEqual(second["job_id"], first["job_id"])
        again = intake(ctx, text, None, location="Remote - US")
        self.assertEqual((again["job_id"], again["status"]), (second["job_id"], "recorded"))
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 2)


class LocationKeptTests(unittest.TestCase):
    def test_a_copy_at_the_exact_link_never_writes_over_the_location_you_set(self):
        """Under D188 an edited copy at the posting's exact link is that posting (its text written); your location stays
        until you give another (rt4_6 kept)."""
        from careeros.discovery.manual_intake import set_location
        from careeros.discovery.saved import parse_saved_text
        from tests.test_intake_identity import WORKDAY
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, WORKDAY)
        set_location(ctx, first["job_id"], "Calgary, Alberta, Canada")
        raw, employer = parse_saved_text(f"URL: {WORKDAY}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: Remote - US\n\n"
                                         + BODY + "\nSaved again.", "p.txt")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        process(ctx)
        row = store.one("SELECT location_raw, country, description_text FROM jobs WHERE job_id = ?", (first["job_id"],))
        self.assertEqual((row["location_raw"], row["country"]), ("Calgary, Alberta, Canada", "non_US"))
        self.assertIn("Saved again.", row["description_text"])

    def test_a_saved_file_location_an_older_reader_wrote_waits_for_you(self):
        """The red team's fr1-upgrade-2: a saved file's location the deployed reader cut (one of two Location lines, a
        dropped 'Work Location:') placed the job in the US after the upgrade. Only the current reader's write places it;
        an older row with no record waits for careeros location."""
        from careeros.discovery.saved import READER, parse_saved_text
        store, ctx = setup()
        raw, employer = parse_saved_text("Title: Claims Analyst\nEmployer: Acme Rx\nLocation: Calgary, AB, Canada\n"
                                         "Location: Remote - US\n\n" + BODY, "p.txt")
        self.assertEqual(raw.raw["reader"], READER)
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        job = dict(store.one("SELECT job_id, ats, location_raw, description_text FROM jobs"))
        self.assertTrue(location_declared(store, job))   # the current reader: both lines, read whole
        # what the deployed reader stored: one line of the two, and no record of where it came from
        from tests.helpers import add_job
        add_job(store, "manual:acme-rx:older", ats="manual", location_raw="Remote - US", description_text=BODY)
        older = dict(store.one("SELECT job_id, ats, location_raw, description_text FROM jobs WHERE job_id = 'manual:acme-rx:older'"))
        self.assertFalse(location_declared(store, older))
        # a later candidate's record of a file write without the current reader's mark waits too
        from careeros.openings import record_observation
        with store.tx():
            record_observation(store, source_kind="manual_import", evidence_level="human", job_id=older["job_id"],
                               location="Remote - US", facts={"location_write": "file"})
        self.assertFalse(location_declared(store, older))


class LabelRuleTests(unittest.TestCase):
    """Group B of batch 2 and the addendum's fr1-upgrade-3: a label given before the upgrade waits for you to give it
    again; any Skip, dismissal, other label or re-identified posting withdraws the job's open handoffs."""

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
        handoff = self.vault.root / session["handoff"]
        self.assertTrue(handoff.exists())
        return handoff

    def test_a_label_given_before_the_upgrade_waits_for_you(self):
        store = self.store
        store.writer("review").insert("labels", {"label_id": "label:old", "job_id": JOB, "at": store.clock.now(), "label": "apply",
                                                 "reason": None, "system_tier": None, "system_decision": "scored",
                                                 "profile": "primary"})
        store.writer("review").event("job_labelled", outcome="informational", entity_type="job", entity_id=JOB, actor_type="human",
                                     actor_id="user", metadata={"label": "apply", "label_id": "label:old", "changes_seen": 0})
        route, why = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertTrue(why.startswith("label_unconfirmed:"), why)
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                             questions=QUESTIONS)
        give_label(store, POLICY, MID, JOB, "apply", "still the one", "label-again")
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")

    def test_another_jobs_questions_in_this_folder_are_never_read(self):
        """fr1-paste-1: an older engine's 'R-4401.' folder is the candidate's 'R-4401' on Windows; its questions were read."""
        path = packages.questions_path(self.vault.root, JOB)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"job_id": "manual:acme-rx:R-4401.", "questions": QUESTIONS}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "questions_unreadable"):
            packages.load_questions(self.vault.root, JOB)
        packages.save_questions(self.vault.root, JOB, QUESTIONS, "test", self.store.clock.now())
        self.assertEqual(packages.load_questions(self.vault.root, JOB), QUESTIONS)


class RederivationTests(unittest.TestCase):
    """What an earlier engine stored as one job is undone at every processing run (fr1-faults-1, fr1-upgrade-5/6)."""

    def test_variants_reposts_and_lead_links_an_older_engine_made_are_undone_every_run(self):
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        ingest(ctx, employer, [board_job(n, f"https://job-boards.greenhouse.io/acmerx/jobs/{n}", text=CLAIMS + f"\n{n}.")
                               for n in (4400001, 4400002, 4400009)], full_board=False)
        older = store.writer("discovery")
        for _ in range(2):   # the deployed engine's links, then again after a rollback
            older.update("jobs", {"variant_of": "greenhouse:acmerx:4400001"}, "job_id = 'greenhouse:acmerx:4400002'")
            older.update("jobs", {"repost_of": "greenhouse:acmerx:4400001"}, "job_id = 'greenhouse:acmerx:4400009'")
            counts = pipeline.rederive_identity(ctx)
            self.assertEqual((counts["variants_cleared"], counts["reposts_cleared"]), (1, 1))
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE variant_of IS NOT NULL OR repost_of IS NOT NULL")[0], 0)
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = 'greenhouse:acmerx:4400009' AND relation = 'possible_repost'"))
        # a lead an older engine linked by title, and an agency's lead linked by its link: reopened
        add_lead(store, "lead:title", "Claims Analyst", "Acme Rx · Remote", url="https://www.linkedin.com/jobs/view/4000000111")
        store.writer("discovery").insert("leads", {
            "lead_id": "lead:agency:x1", "url": "https://job-boards.greenhouse.io/acmerx/jobs/4400001", "site": "greenhouse",
            "title": "Claims Analyst", "title_norm": "claims analyst", "family": "claims_analysis", "context": "Harbor Staffing · Remote",
            "sender": "agency", "subject": None, "received_at": None, "first_seen_at": store.clock.now(), "status": "new",
            "job_id": None, "profile": "primary"})
        for lead_id in ("lead:title", "lead:agency:x1"):
            older.update("leads", {"status": "ingested", "job_id": "greenhouse:acmerx:4400001"}, "lead_id = ?", (lead_id,))
            older.insert("lead_matches", _match_row(lead_id, "greenhouse:acmerx:4400001", None, "title_location", "matched",
                                                    ["greenhouse:acmerx:4400001"], store.clock.now(), actor="careeros", reason=None))
        # the agency lead's host is in the private settings (else the recheck waits: the red team's gr1-alerts-2)
        with mock.patch("careeros.discovery.alerts._tracker_hosts", return_value=frozenset({"job-boards.greenhouse.io"})):
            self.assertEqual(recheck_linked_leads(ctx)["leads_reopened"], 2)
        self.assertEqual({lead_row(store, lid)["status"] for lid in ("lead:title", "lead:agency:x1")}, {"new"})


class IndividualFixTests(unittest.TestCase):
    """Group C of batch 2."""

    def test_two_cards_that_differ_only_after_the_cut_are_two_leads(self):
        """fr1-alerts-0: a card cut at 160 characters before its place made two openings one lead."""
        from careeros.discovery.alerts import CONTEXT_CHARS, _line_context
        head = "Acme Rx · " + "A claims role on our pharmacy team reviewing adjudication outcomes and payer files " * 2
        a, b = _line_context(head + "· Calgary, AB"), _line_context(head + "· Remote - US")
        self.assertGreater(len(head), CONTEXT_CHARS)
        self.assertNotEqual(a, b)
        self.assertEqual(_line_context("Acme Rx · Remote"), "Acme Rx · Remote")   # a short card is kept as it is

    def test_a_literal_less_than_sign_never_deletes_the_text(self):
        """fr1-saved-2: '<' in 'pay < $40/hour' deleted everything up to the next '>'."""
        from careeros.normalize.sanitize import html_to_text
        text = html_to_text("<p>Pay < $40/hour for R-1101.</p><p>Team: appeals > grievances</p>")
        self.assertIn("R-1101", text)
        self.assertIn("appeals", text)
        self.assertNotIn("<p>", text)

    def test_a_saved_files_work_location_line_is_its_location(self):
        """fr1-saved-4: 'Work Location:' and 'Job Location:' header lines were dropped."""
        from careeros.discovery.saved import parse_saved_text
        for line in ("Work Location: Calgary, AB, Canada", "Job Location: Calgary, AB, Canada", "Location - Calgary, AB, Canada"):
            raw, _ = parse_saved_text(f"Title: Claims Analyst\nEmployer: Acme Rx\n{line}\n\n" + BODY, "p.txt")
            self.assertIn("Calgary", raw.location, line)

    def test_a_saved_page_with_two_postings_is_refused(self):
        """fr1-saved-6: a page's @graph holding its own posting and another imported the other."""
        from careeros.discovery.saved import SavedPostingError, parse_saved_html
        from tests.test_red_team_d186 import _page, _posting
        page = _page({"@context": "https://schema.org", "@graph": [_posting(), _posting(title="Billing Analyst")]})
        with self.assertRaises(SavedPostingError):
            parse_saved_html(page, "page.html")
        self.assertEqual(parse_saved_html(_page(_posting()), "page.html")[0].title, "Claims Analyst")   # the control

    def test_a_slogan_or_a_short_code_is_no_requisition(self):
        """fr1-paste-0 and fr1-saved-0: 'job #1' and 'Req: 2' were read as requisitions."""
        from careeros.discovery.manual_intake import requisition_in
        self.assertIsNone(requisition_in("Quality is job #1 at Acme."))
        self.assertIsNone(requisition_in("Min. Req: 2 years"))
        self.assertEqual(requisition_in("Job ID: R-4401"), "R-4401")
        self.assertEqual(requisition_in("Req #123"), "123")

    def test_the_title_never_places_a_posting_abroad(self):
        """fr1-boards-3: 'Claims Analyst - Remote (Melbourne, FL)' beside 'Remote - Melbourne, FL' was rejected outside_us."""
        from careeros.normalize.posting import placed_country
        self.assertEqual(placed_country("Claims Analyst - Remote (Melbourne, FL)", "Remote - Melbourne, FL", "", "remote"), "unknown")
        self.assertEqual(placed_country("(Canada) Claims Analyst", "Remote", "", "remote"), "unknown")
        self.assertEqual(placed_country("Claims Analyst - UK", "Remote - US", "", "remote"), "unknown")   # takes the US away
        self.assertEqual(placed_country("Claims Analyst", "Toronto, ON, Canada", "", "onsite"), "non_US")   # the field places

    def test_careeros_location_takes_the_writer_lock(self):
        """fr1-faults-3: `careeros location` wrote the job and ran the pipeline without the single-writer lock."""
        self.assertIn("location", cli.WRITES)


if __name__ == "__main__":
    unittest.main()
