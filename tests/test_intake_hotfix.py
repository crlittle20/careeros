"""The intake hotfix of 2026-11-09 (D116): the confirmed findings of the independent review of the 09d6c75 deploy, each
with its failure and its control. Synthetic emails, postings and policies only; no network, no vault."""
import contextlib
import io
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import httpx

from careeros.cli import _gmail_pull, _import_alert
from careeros.core.clock import FixedClock
from careeros.core.policy import PolicyBundle
from careeros.db.store import open_store
from careeros.discovery import gmail
from careeros.discovery.alerts import canonical_job_url, kept_for_review, parse_eml_bytes
from careeros.discovery.http import HttpClient
from careeros.extract.ai import merge
from careeros.extract.rules import extract
from careeros.normalize.posting import norm, relevant_role, targeted_supervisor
from careeros.pipeline import Context
from careeros.qualify.gate import GateResult, evaluate, requirement_rules, state_for
from careeros.qualify.night import night_state
from careeros.qualify.profile import load_profile
from careeros.review.report import _night_section

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_alerts import alert_email
from tests.test_certifications import CATALOG, judged, mentions, person, posting_with
from tests.test_certifications import TITLE as CERT_TITLE
from tests.test_qualify import POLICY, job, run
from tests.test_saved import POLICY as SAVED_POLICY

FIXTURES = Path(__file__).parent / "fixtures"
TITLE = "Healthcare Reporting Analyst"   # a target-family title: before 09d6c75 such a card was always kept as a lead

# Links to real jobs without a job ID CareerOS recognises: sponsored, tracking and redirect links, and formats it does
# not know. 09d6c75 skipped them all as "navigation" and lost the jobs (review High, alerts.py:208).
NO_KNOWN_ID = (
    "https://www.indeed.com/pagead/clk/dl?mo=r&ad=-6NYlbfkN0Dsynthetic&from=ja",
    "https://www.indeed.com/pagead/clk?mo=r&ad=-6NYlbfkN0Dsynthetic2",
    "https://cts.indeed.com/v3/H4sIAAAAAAAAAsynthetic",
    "https://engage.indeed.com/f/a/synthetic~~/AAAAAQA~/RgRsynthetic",
    "https://apply.indeed.com/indeedapply/xpc?v=5&synthetic=1",
    "https://www.glassdoor.com/partner/jobListing.htm?pos=101&ao=synthetic",
    "https://www.glassdoor.com/job-listing/reporting-analyst-synthetic-health-JV_KO0,17_KE18,34.htm",
    "https://click.glassdoor.com/cl/synthetic?x=1",
    "https://www.linkedin.com/e/v2?e=synthetic&a=jobs",
    "https://www.linkedin.com/slink?code=synthetic",
)
# Search, browse and collection pages: the headers and "see all" links of a digest, never one posting.
SEARCH_PAGES = (
    "https://www.linkedin.com/jobs/search/?keywords=Healthcare%20Reporting%20Analyst",
    "https://www.linkedin.com/comm/jobs/search?keywords=analyst&f_TPR=r86400",
    "https://www.linkedin.com/jobs/collections/recommended/",
    "https://www.linkedin.com/jobs/",
    "https://www.indeed.com/jobs?q=healthcare+reporting+analyst&l=remote",
    "https://www.indeed.com/q-healthcare-reporting-analyst-l-remote-jobs.html",
    "https://www.indeed.com/m/jobs?q=reporting+analyst",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=reporting%20analyst",
    "https://www.glassdoor.com/Job/remote-reporting-analyst-jobs-SRCH_IL.0,6_IS11047_KO7,24.htm",
)


def readings(reqs):
    """The certification readings (name, requirement type) of extracted or merged requirements."""
    return {(r.name, r.requirement_type) for r in reqs if r.category == "certification"}


def hard_verify(gate):
    """The "verify the certification" flags of a credential read as required that you do not hold: since the owner's
    D140 F1 a certification never rejects on its own (no certification hard failure, ever)."""
    assert not [f for f in gate.hard_failures if f["code"].startswith("certification")], gate.hard_failures
    return [f for f in gate.flags if f["code"] == "certification_check" and f.get("hard_reading")]


def digest(*urls, title=TITLE) -> bytes:
    """One card per link, each from its own synthetic employer (one employer's same title would be one job)."""
    return alert_email(html_body="".join(f'<p><a href="{u}">{title}</a> Synthetic Employer {i} - Remote</p>'
                                         for i, u in enumerate(urls)))


class AlertImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.store = open_store(":memory:", FixedClock("2026-11-09T12:00:00.000Z"), SAVED_POLICY.fingerprint)
        self.ctx = Context(self.store, SAVED_POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX,
                           Path(self.tmp.name) / "snap")
        self.client = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), min_interval=0,
                                 sleep=lambda s: None)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def run_import(self, data: bytes, name="alert.eml"):
        path = self.inbox / name
        path.write_bytes(data)
        return _import_alert(self.ctx, path, self.client, self.inbox, "2026-11-09")


class JobLinksWithoutAKnownIdTests(AlertImport):
    def test_a_job_link_without_a_known_id_stays_a_lead(self):
        alert = parse_eml_bytes(digest(*NO_KNOWN_ID))
        self.assertEqual(len(alert.links), len(NO_KNOWN_ID), [l.url for l in alert.links])
        self.assertEqual(alert.skipped, {})
        counts = self.run_import(digest(*NO_KNOWN_ID))
        self.assertEqual(counts["lead_new"], len(NO_KNOWN_ID))
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE family IS NOT NULL")["c"], len(NO_KNOWN_ID))

    def test_job_ids_in_other_spellings_are_recognised(self):
        view = "https://www.linkedin.com/jobs/view/4488484632"
        for url in ("https://www.linkedin.com/jobs/view/?jobId=4488484632",
                    "https://www.linkedin.com/jobs/search/#currentJobId=4488484632",
                    "https://www.linkedin.com/Jobs/View/4488484632"):
            with self.subTest(url=url):
                self.assertEqual(canonical_job_url(url), (view, "linkedin", True))
        self.assertEqual(canonical_job_url("https://www.indeed.com/rc/clk?jk=0A1B2C3D4E5F6A7B&from=ja"),
                         ("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "indeed", True))

    def test_search_pages_are_skipped_and_counted_never_leads(self):
        alert = parse_eml_bytes(digest(*SEARCH_PAGES))
        self.assertEqual(alert.links, [])
        self.assertEqual(alert.skipped, {"navigation": len(SEARCH_PAGES)})
        counts = self.run_import(digest(*SEARCH_PAGES))
        self.assertEqual(counts["link_navigation_skipped"], len(SEARCH_PAGES))
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 0)

    def test_a_search_page_naming_a_job_is_that_job(self):
        alert = parse_eml_bytes(digest("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4488484632"))
        self.assertEqual([(l.url, l.job_like) for l in alert.links], [("https://www.linkedin.com/jobs/view/4488484632", True)])


class MalformedLinkTests(AlertImport):
    def test_an_unreadable_link_is_counted_and_the_rest_of_the_digest_kept(self):
        data = digest("https://[invalid", "https://www.indeed.com/viewjob?jk=3074d70a34bee345")
        alert = parse_eml_bytes(data)
        self.assertEqual([l.url for l in alert.links], ["https://www.indeed.com/viewjob?jk=3074d70a34bee345"])
        self.assertEqual(alert.skipped, {"malformed": 1})
        self.assertEqual(self.run_import(data)["link_malformed_skipped"], 1)

    def broken_private_sites(self) -> str:
        path = Path(self.tmp.name) / "private_sites.yaml"
        path.write_text("manual_systems:\n  - not-a-mapping.example\n", encoding="utf-8")  # must be a mapping
        return str(path)

    def test_a_broken_private_sites_file_stops_the_import_and_keeps_the_email(self):
        data = digest("https://careers.synthetic-health.example/job/123/reporting-analyst/")
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.broken_private_sites()}):
            with self.assertRaises(ValueError):   # never swallowed as a "malformed link" (review Medium, alerts.py:198)
                parse_eml_bytes(data)
            counts = self.run_import(data)
        self.assertEqual(counts["alert_config_error"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["alert.eml"])   # left for a re-run
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "private_sites_invalid")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 0)

    def test_a_broken_private_sites_file_stops_the_gmail_pull_before_any_message_is_marked(self):
        reader = mock.Mock()
        reader.search.return_value = ["m1"]
        reader.raw.return_value = digest("https://careers.synthetic-health.example/job/123/reporting-analyst/")
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.broken_private_sites()}):
            with self.assertRaises(gmail.GmailError) as ctx:
                _gmail_pull(self.ctx, self.client, "q", gmail=reader)
        self.assertEqual(ctx.exception.code, "private_sites_invalid")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM alert_messages")["c"], 0)
        reader.close.assert_called_once()


class HelpfulCertificationTests(unittest.TestCase):
    MIXED = ("PMP certification, Agile experience helpful.", "PMP certification with Agile experience helpful.",
             "PMP certification - Agile experience helpful.", "PMP certification, Agile experience is helpful.")

    def test_helpful_counts_only_when_it_describes_the_credential(self):
        for line in self.MIXED:   # D126: "helpful" outside the credential's own clause makes it unclear, never preferred
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "ambiguous")])
        for line in ("PMP certification is helpful.", "PMP certification helpful.", "PMP helpful."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "preferred")])

    def test_a_required_credential_beside_a_helpful_item_still_decides(self):
        for line in self.MIXED:   # D126: never a silent pass; flagged for review (CONDITIONAL) instead of rejected
            with self.subTest(line=line):
                gate, _reqs, _job = judged(person(), line)
                self.assertNotIn("certification_required", {f["code"] for f in gate.hard_failures})
                self.assertIn("certification_check", {f["code"] for f in gate.flags})
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
        gate, _reqs, _job = judged(person(), "PMP certification is helpful.")
        self.assertNotIn("certification_required", {f["code"] for f in gate.hard_failures})

    def ai(self, text, quote, name="pmp", kind="hard_required"):
        raw = [{"category": "certification", "name": name, "requirement_type": kind, "source_text": quote,
                "confidence": 0.95}]
        return {(r.name, r.requirement_type) for r in merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
                if r.category == "certification"}

    def test_the_ai_merge_takes_the_rules_reading_of_a_helpful_sentence(self):
        line = "PMP certification is helpful."
        text = f"Required Qualifications:\n- {line}\n- Experience with Facets required."
        # D131 S1d: an AI hard reading of a credential the rules hold only as preferred adds it to verify
        self.assertEqual(self.ai(text, "PMP certification"), {("pmp", "preferred"), ("pmp", "ambiguous")})
        self.assertEqual(self.ai(text, line), {("pmp", "preferred"), ("pmp", "ambiguous")})
        both = "PMP certification is helpful. Experience with Facets required."
        text = f"Required Qualifications:\n- {both}"
        self.assertEqual(self.ai(text, both), {("pmp", "preferred"), ("pmp", "ambiguous")})   # the whole line around it
        self.assertEqual(self.ai(text, both, kind="preferred"), {("pmp", "preferred")})   # an AI preferred record adds nothing

    def test_the_ai_merge_never_weakens_a_credential_the_rules_read_as_required(self):
        text = "Required Qualifications:\n- PMP certification, Agile experience helpful."   # rules: unclear (D126)
        self.assertEqual(self.ai(text, "PMP certification, Agile experience helpful."), {("pmp", "ambiguous")})
        text = "Required Qualifications:\n- PMP certification required.\n- PMP certification is helpful."
        # D131 S1: certifications are the rules' alone; S3b: PMP demanded in one line and called helpful in another is unclear
        self.assertEqual(self.ai(text, "PMP certification is helpful."), {("pmp", "ambiguous"), ("pmp", "preferred")})


ENROLLMENT_AUDIT = ("You will audit 834 enrollment files and eligibility records for accuracy, reconcile enrollment "
                    "rosters against the source files, document every discrepancy and work with the enrollment team "
                    "on corrections. Experience with eligibility and enrollment processes required. ") * 2
SOFTWARE_QA = ("You will run validation of configuration changes before each software release, write test plans, "
               "automate regression suites and keep defect reporting dashboards current for the engineering team. "
               "Experience with test automation and release validation required. ") * 2


class QaTitleTests(unittest.TestCase):
    def codes(self, title, text):
        j = job(title, text, location="Remote")
        return {f["code"] for f in run(j)[1].hard_failures}

    def test_health_plan_operations_words_are_context_for_qa_titles(self):
        for title in ("QA Auditor", "Eligibility QA Auditor", "Enrollment QA Analyst", "QA Analyst"):
            with self.subTest(title=title):
                self.assertNotIn("generic_title_no_context", self.codes(title, ENROLLMENT_AUDIT))

    def test_qa_titles_are_still_judged_on_their_own_list(self):
        """Software QA text full of general words ("validation", "configuration", "reporting") passes the broad list
        but never the QA list (the review: nothing caught the QA list being dropped). With no context word at all it is
        rejected, whether or not it names a testing tool ("test automation"); with one weak word beside the tool it is
        flagged for review (Pass 2, CR-HF-04, D126)."""
        no_tool = SOFTWARE_QA.replace("test automation", "regression testing")
        self.assertIn("generic_title_no_context", self.codes("QA Analyst", no_tool))
        self.assertIn("generic_title_no_context", self.codes("QA Analyst", SOFTWARE_QA))
        with_word = SOFTWARE_QA + "You will also test the enrollment screens. "
        self.assertEqual(self.codes("QA Analyst", with_word), set())
        self.assertEqual(state_for(run(job("QA Analyst", with_word, location="Remote"))[1], POLICY), "CONDITIONAL")
        self.assertNotIn("generic_title_no_context", self.codes("Reporting Analyst", SOFTWARE_QA))


class SupervisorAndReportTests(unittest.TestCase):
    def test_a_supervisor_title_that_also_says_manager_is_a_manager(self):
        self.assertTrue(targeted_supervisor(norm("Claims Supervisor"), POLICY))
        self.assertFalse(targeted_supervisor(norm("Claims Supervisor / Manager"), POLICY))
        family = job("Claims Supervisor / Manager", "text")["family"]
        self.assertFalse(relevant_role(norm("Claims Supervisor / Manager"), family, "remote", "US", POLICY))

    def test_the_supervisor_scope_review_is_conditional_whatever_a_policy_list_says(self):
        policy = PolicyBundle({**POLICY.data, "eligibility_states": {"material_unknowns": []}}, "synthetic")
        result = GateResult("eligible")
        result.flag("supervisor_scope_review", "verify scope")
        self.assertEqual(state_for(result, policy), "CONDITIONAL")

    def test_a_high_priority_unconfirmed_night_row_is_listed_once(self):
        class Store:
            def all(self, _query):
                return [{"job_id": "unconfirmed", "n_mode": "supplemental", "n_state": "CONDITIONAL",
                         "n_schedule": "unknown", "n_tier": "near_miss", "n_priority": 99}]
        with mock.patch("careeros.review.report._night_card",
                        side_effect=lambda _store, row, *_args: f"<job>{row['job_id']}</job>"):
            output = "".join(_night_section(Store(), "2026-11-09T04:00:00.000Z", None, None, POLICY))
        self.assertEqual(output.count("<job>unconfirmed</job>"), 1)
        self.assertIn("Unconfirmed hours — check before considering (1)", output)


class CodexPass1AlertIdTests(AlertImport):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 1, CR-HF-01 (High): the first non-empty ID field won before it was checked, so a
    malformed currentJobId hid a valid jobId or "#currentJobId" and the job was skipped as a search page."""
    VIEW = "https://www.linkedin.com/jobs/view/9123456"

    def test_every_id_field_is_read_before_one_is_chosen(self):
        for url in ("https://www.linkedin.com/jobs/search/?currentJobId=unavailable&jobId=9123456",
                    "https://www.linkedin.com/jobs/search/?currentJobId=unavailable#currentJobId=9123456",
                    "https://www.linkedin.com/jobs/search/?currentJobId=&jobId=9123456",
                    "https://www.linkedin.com/jobs/search/?currentJobId=9123456&jobId=9123456",
                    "https://www.linkedin.com/jobs/search/?jobId=9123456"):   # the control, as before
            with self.subTest(url=url):
                self.assertEqual(canonical_job_url(url), (self.VIEW, "linkedin", True))
        self.assertEqual(canonical_job_url("https://www.indeed.com/jobs?q=analyst&jk=zzz&vjk=ABCDEF0123456789"),
                         ("https://www.indeed.com/viewjob?jk=abcdef0123456789", "indeed", True))
        self.assertEqual(canonical_job_url("https://www.glassdoor.com/Job/x.htm?jl=bad&jobListingId=1009876543"),
                         ("https://www.glassdoor.com/job-listing/j?jl=1009876543", "glassdoor", True))

    def test_two_different_job_ids_name_no_single_job_and_are_kept_for_review(self):
        url = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"
        self.assertFalse(canonical_job_url(url)[2])   # never chosen by their order
        alert = parse_eml_bytes(digest(url))
        self.assertEqual(([l.url for l in alert.links], alert.skipped), ([url], {}))
        self.assertEqual(self.run_import(digest(url))["lead_new"], 1)

    def test_an_unreadable_job_id_on_a_search_page_is_kept_for_review_and_a_bare_search_page_skipped(self):
        kept = "https://www.linkedin.com/jobs/search/?currentJobId=unavailable"
        alert = parse_eml_bytes(digest(kept, "https://www.linkedin.com/jobs/search/?keywords=analyst&currentJobId="))
        self.assertEqual(([l.url for l in alert.links], alert.skipped), ([kept], {"navigation": 1}))

    def test_a_digest_of_mixed_links_keeps_every_job_and_counts_the_rest(self):
        # since the owner's D151 (under D149) an unreadable ID value disagrees with the readable one: the link is kept for
        # review as received, no longer read as the job 9123456
        review = "https://www.linkedin.com/jobs/search/?currentJobId=unavailable&jobId=9123456"
        data = digest(review, "https://[invalid", "https://www.linkedin.com/jobs/search/?keywords=analyst")
        alert = parse_eml_bytes(data)
        self.assertEqual(([l.url for l in alert.links], alert.skipped), ([review], {"malformed": 1, "navigation": 1}))
        counts = self.run_import(data)
        self.assertEqual((counts["link_malformed_skipped"], counts["link_navigation_skipped"]), (1, 1))
        reader = mock.Mock()
        reader.search.return_value = ["m1"]
        reader.raw.return_value = data
        counts = _gmail_pull(self.ctx, self.client, "q", gmail=reader)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM alert_messages")["c"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE url = ?", (review,))["c"], 1)


class CodexPass1PrivateSitesTests(AlertImport):
    """CR-HF-02 (Medium): a YAML syntax error in the private-sites file escaped both alert preflights as a PyYAML error."""
    BROKEN = {"syntax": "sources: [unterminated", "not a mapping": "- a\n- b\n", "a list as a number": "never_fetch: 5\n"}

    def write(self, body: str) -> str:
        path = Path(self.tmp.name) / "private_sites.yaml"
        path.write_text(body, encoding="utf-8")
        return str(path)

    # What each kind of broken file is called, so the owner knows what to fix (the hand check of 32b9706: with only the
    # prefix asserted, the loader's own handling of each kind could go and the outer catch-all hid it)
    SAYS = {"syntax": "is not valid YAML", "not a mapping": "must be a mapping", "a list as a number": "must be lists",
            "a folder": "the file cannot be read ("}

    def test_every_broken_file_is_the_one_error_the_preflights_handle(self):
        from careeros.core import private_sites
        for name, body in [*self.BROKEN.items(), ("a folder", None)]:
            with self.subTest(name=name), mock.patch.dict(os.environ, {
                    "CAREEROS_PRIVATE_SITES": self.tmp.name if body is None else self.write(body)}):
                with self.assertRaises(ValueError) as ctx:
                    private_sites.load()
                self.assertTrue(str(ctx.exception).startswith("private_sites_invalid"), str(ctx.exception))
                self.assertIn(self.SAYS[name], str(ctx.exception))
                self.assertNotIn("unterminated", str(ctx.exception))   # never quotes the private file

    def test_a_file_that_is_not_utf8_says_it_cannot_be_read(self):
        from careeros.core import private_sites
        path = Path(self.tmp.name) / "private_sites.yaml"
        path.write_bytes(b"sources: {caf\xe9: allowed}\n")   # saved in another encoding: its own reason, not "a mapping"
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": str(path)}), self.assertRaises(ValueError) as ctx:
            private_sites.load()
        self.assertTrue(str(ctx.exception).startswith("private_sites_invalid: the file cannot be read"), str(ctx.exception))

    def test_a_syntax_error_keeps_the_email_stops_gmail_and_the_daily_run_goes_on(self):
        data = digest("https://careers.synthetic-health.example/job/123/reporting-analyst/")
        reader = mock.Mock()
        reader.search.return_value = ["m1"]
        reader.raw.return_value = data
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write(self.BROKEN["syntax"])}):
            counts = self.run_import(data)
            with mock.patch("careeros.core.credentials.WindowsCredentials"), \
                    mock.patch("careeros.demo.sandbox.enabled", return_value=False), \
                    mock.patch("careeros.cli._gmail_method", return_value="imap"), \
                    mock.patch("careeros.cli._gmail_reader", return_value=reader):
                from careeros.cli import _daily_gmail
                daily = _daily_gmail(self.ctx)
        self.assertEqual(counts["alert_config_error"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["alert.eml"])
        self.assertEqual(daily, {"gmail_errors": 1})
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM alert_messages")["c"], 0)
        codes = [r["error_code"] for r in self.store.all("SELECT error_code FROM exceptions")]
        self.assertEqual(codes, ["private_sites_invalid", "private_sites_invalid"])
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write("sources: {}\n")}):   # corrected: retried
            self.assertEqual(self.run_import(data)["lead_new"], 1)

    def test_a_broken_file_leaves_every_github_inbox_issue_open(self):
        """The fix-check of 84a47b1, N1: the GitHub recommendations inbox swallowed the loader's error per link and
        closed the issue with its jobs lost."""
        from careeros.cli import _inbox_pull
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write(self.BROKEN["syntax"])}), \
                mock.patch("careeros.discovery.github_inbox.open_issues") as issues, \
                mock.patch("careeros.discovery.github_inbox.close_with_reply") as close:
            counts = _inbox_pull(self.ctx, self.client, "synthetic/recommendations")
        issues.assert_not_called()
        close.assert_not_called()
        self.assertEqual(counts, {"inbox_config_error": 1})
        self.assertEqual([r["error_code"] for r in self.store.all("SELECT error_code FROM exceptions")], ["private_sites_invalid"])


class CodexPass1CertificationTests(unittest.TestCase):
    """CR-HF-03 (High): "helpful" was decided once for every credential of a sentence, and the AI merge decided it again
    without the credential's place, so a demanded credential passed and an alias-only helpful one was rejected."""

    def test_each_credential_of_a_sentence_takes_its_own_reading(self):
        # D126 C1: a softening word outside the plain form makes the credential unclear (CONDITIONAL), never preferred; a credential
        # demanded in its own part of the sentence, with no softening word there, stays required
        for line, expected in (
                # D127: a demand beside a softening word is unclear (CONDITIONAL), never a rejection
                ("PMP certification is a prerequisite and CPC certification is helpful.", [("pmp", "ambiguous"), ("cpc", "ambiguous")]),
                ("PMP certification required and CPC helpful.", [("pmp", "ambiguous"), ("cpc", "ambiguous")]),
                ("PMP certification is valued and CPC certification is helpful.", [("pmp", "ambiguous"), ("cpc", "ambiguous")]),
                ("You must hold a CPC certification and PMP is helpful.", [("cpc", "ambiguous"), ("pmp", "ambiguous")]),
                ("Must have strong SQL skills, and CPC certification is helpful.", [("cpc", "ambiguous")]),
                ("PMP and CPC certifications are helpful.", [("pmp", "preferred"), ("cpc", "preferred")]),
                ("Familiarity with sprint planning, PMP helpful.", [("pmp", "ambiguous")])):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), expected)

    def test_a_sentence_level_certification_beside_a_helpful_item_keeps_the_section(self):
        self.assertEqual(mentions("Professional certification, SQL experience helpful."),   # D126: unclear, for review
                         [("unspecified certification", "ambiguous")])
        self.assertEqual(mentions("Professional certification is helpful."), [("unspecified certification", "ambiguous")])   # D126 C1
        self.assertEqual(mentions("Coding certification is helpful."), [("family:medical_coding", "preferred")])   # the plain form

    def test_a_separately_demanded_credential_still_decides(self):
        line = "PMP certification is a prerequisite and CPC certification is helpful."   # D127: a demand beside a softening word is unclear (CONDITIONAL), never a rejection
        for profile in (person(), person({"name": "PMP", "status": "held"})):
            gate, _reqs, _job = judged(profile, line)
            self.assertFalse(hard_verify(gate))
            self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
        gate, _reqs, _job = judged(person(), "PMP certification is a prerequisite.")   # no softening word: required
        self.assertTrue(hard_verify(gate))   # read as required: verify, never a rejection (D140 F1)
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")

    def ai(self, text, quote, name="pmp", kind="hard_required"):
        return HelpfulCertificationTests.ai(self, text, quote, name, kind=kind)

    def test_the_ai_merge_keeps_the_rules_reading_of_each_credential(self):
        text = "Required Qualifications:\n- PMP certification is helpful.\n- SQL experience required."   # the plain form
        for quote in ("PMP", "PMP certification is helpful."):   # D131 S1d: an AI hard reading of a credential the rules hold only as preferred adds it to verify
            with self.subTest(quote=quote):
                self.assertEqual(self.ai(text, quote), {("pmp", "preferred"), ("pmp", "ambiguous")})
        text = "Required Qualifications:\n- Familiarity with sprint planning, PMP helpful.\n- SQL experience required."
        for quote in ("PMP", "Familiarity with sprint planning, PMP helpful."):   # D126 C1/C2: not plain, so unclear
            with self.subTest(quote=quote):
                self.assertEqual(self.ai(text, quote), {("pmp", "ambiguous")})
        text = ("Required Qualifications:\n- PMP certification is a prerequisite and CPC certification is helpful.\n"
                "- SQL experience required.")
        for quote in ("PMP certification is a prerequisite",   # D127: the rules read both as unclear; the AI never
                      "PMP certification is a prerequisite and CPC certification is helpful."):   # settles it either way
            with self.subTest(quote=quote):
                self.assertEqual(self.ai(text, quote), {("pmp", "ambiguous"), ("cpc", "ambiguous")})
        self.assertEqual(self.ai(text, "CPC certification is helpful", name="cpc"), {("cpc", "ambiguous"), ("pmp", "ambiguous")})

    # The fix-check of 84a47b1 (Claude's own check before the delta Pass 2)
    def test_not_required_and_optional_count_only_in_their_own_part(self):
        """P1 and N2: "not required" made every credential of the sentence preferred, and the merge then downgraded a
        correctly read hard PMP."""
        # the delta check of c90ee6c: "but helpful" names no other item, so it may soften PMP too: PMP is unclear
        self.assertEqual(mentions("PMP certification is required and CPC is not required but helpful."),
                         [("pmp", "ambiguous"), ("cpc", "ambiguous")])
        for line in ("PMP certification is required, CPC certification is optional.",
                     "PMP certification is mandatory and CPC certification is not required."):
            with self.subTest(line=line):   # D127: a demand beside a softening word is unclear (CONDITIONAL), never a rejection
                self.assertEqual(mentions(line), [("pmp", "ambiguous"), ("cpc", "ambiguous")])
                text = f"Required Qualifications:\n- {line}\n- SQL experience required."
                self.assertEqual(self.ai(text, "PMP certification is"), {("pmp", "ambiguous"), ("cpc", "ambiguous")})
        self.assertEqual(mentions("PMP and CPC certifications are not required."), [("pmp", "preferred"), ("cpc", "preferred")])

    def test_a_demanded_certification_beside_a_helpful_credential_is_kept(self):
        """N3: a demanded certification that is no catalog credential vanished with the helpful one beside it. It is
        kept, unclear like the rest of its softened sentence (D127)."""
        for line, demanded in (("Scrum certification required, CPC helpful.", "family:scrum"),
                               ("Professional certification required, CPC helpful.", "unspecified certification"),
                               ("AAPC certification required, PMP helpful.", "issuer:aapc")):
            with self.subTest(line=line):
                self.assertIn((demanded, "ambiguous"), mentions(line))
        self.assertEqual(mentions("PMP certification is helpful."), [("pmp", "preferred")])   # nothing added

    def test_helpful_in_other_words_and_lists_of_helpful_credentials_stay_preferred(self):
        """N4: "can be helpful", "is generally helpful" and lists ("PMP, CPC certification helpful") read as required."""
        for line in ("CPC certification can be helpful.", "CPC certification should be helpful.",
                     "CPC certification is certainly helpful.", "CPC certification is seen as helpful.",
                     "CPC certification has proven helpful.", "A CPC certification is always helpful."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("cpc", "preferred")])
        for line in ("PMP, CPC certification helpful.", "PMP certification as well as CPC helpful.",
                     "PMP along with CPC certification helpful."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "preferred"), ("cpc", "preferred")])
        for line in ("PMP certification (required) and CPC certification are helpful.", "Required: PMP certification helpful."):
            with self.subTest(line=line):   # a demand and "helpful" in one clause: unclear, never preferred (D126)
                self.assertIn(("pmp", "ambiguous"), mentions(line))

    def test_an_unspecified_rules_record_never_downgrades_a_demanded_credential(self):
        text = ("Required Qualifications:\n- Epic Resolute certification required, Tapestry experience helpful. "
                "Professional certification is helpful.")
        merged = self.ai(text, "Epic Resolute certification required, Tapestry experience helpful.", name="epic resolute")
        # D131 S1: certifications are the rules' alone; the AI's certification records are dropped: the merged certifications are exactly the rules', none of them preferred by the AI
        rules = {(r.name, r.requirement_type) for r in extract(text, POLICY).requirements if r.category == "certification"}
        # D131 S1b: the rules' records unchanged; the credential only the AI found comes in as "verify" (ambiguous)
        self.assertEqual(merged, rules | {("epic resolute", "ambiguous")})
        self.assertNotIn(("epic resolute", "preferred"), merged)


SOFTWARE_DEVICE_QA = ("You will build Selenium and Playwright test automation for our device management product, verify "
                      "device enrollment flows, reproduce UI defects and run end-to-end testing before each release. ") * 3
FIXTURE_QA = ("You will write pytest and Postman suites and maintain test fixtures numbered 834, 835 and 837 for our "
              "billing microservices, and keep the regression dashboard green for the engineering team. ") * 3
FINANCE_QA = ("You will automate reconciliation test cases in Cypress and Selenium for our accounting software, reproduce "
              "ledger defects and keep the release regression suite current for the engineering team. ") * 3
# A genuine enrollment-file audit that also names one testing tool and no strong word: kept in review, never rejected
# (the fix-check of 84a47b1, N5)
EDI_QA_WITH_A_TOOL = ENROLLMENT_AUDIT + "Experience with Postman or automated testing of EDI transactions is a plus. "
HEALTH_PLAN_QA = ("You will audit 834 enrollment files for our health plan members, reconcile eligibility against the "
                  "payer source files and document each discrepancy for the operations team. ") * 3


class CodexPass1QaContextTests(unittest.TestCase):
    """CR-HF-04 (Medium): one weak word ("device enrollment") admitted a software test-automation QA posting."""

    def judged(self, title, text):
        result = run(job(title, text, location="Remote"))[1]
        return {f["code"] for f in result.hard_failures}, {f["code"]: f["detail"] for f in result.flags}

    def test_weak_words_beside_software_testing_tools_are_never_passed(self):
        """Pass 2 (D126): with a testing tool, some health-plan word and fewer than two strong ones, flagged, never
        passed or rejected (on d156418 these were rejected generic_title_no_context). With no such word at all, the
        posting is software QA and rejected, as live does."""
        self.assertIn("generic_title_no_context", self.judged("QA Analyst", SOFTWARE_DEVICE_QA.replace("enrollment", "pairing"))[0])
        for text in (SOFTWARE_DEVICE_QA, FIXTURE_QA, FINANCE_QA):
            with self.subTest(text=text[:60]):
                failures, flags = self.judged("QA Analyst", text)
                self.assertEqual(failures, set())
                self.assertIn("generic_title_unverified", flags)
                self.assertEqual(state_for(run(job("QA Analyst", text, location="Remote"))[1], POLICY), "CONDITIONAL")

    def test_weak_words_alone_flag_a_qa_title_for_review_never_pass_it_silently(self):
        for title, text in (("QA Analyst", ENROLLMENT_AUDIT), ("Enrollment QA Analyst", ENROLLMENT_AUDIT),
                            ("QA Analyst", EDI_QA_WITH_A_TOOL), ("Eligibility QA Auditor", EDI_QA_WITH_A_TOOL),
                            ("QA Analyst", FIXTURE_QA.replace("pytest and Postman", "pytest"))):
            with self.subTest(title=title, text=text[-70:]):
                failures, flags = self.judged(title, text)
                self.assertEqual(failures, set())
                self.assertIn("also fit software or finance work", flags.get("generic_title_unverified", ""))
                self.assertEqual(state_for(run(job(title, text, location="Remote"))[1], POLICY), "CONDITIONAL")

    def test_genuine_health_plan_operations_stay_in_scope(self):
        for text in (HEALTH_PLAN_QA, HEALTH_PLAN_QA + "You will also maintain our Selenium regression suite. "):
            with self.subTest(text=text[-60:]):
                self.assertEqual(self.judged("QA Analyst", text), (set(), {}))


# --- Codex's Pass 2 of d156418 (BLOCKERS-REMAIN) and the owner's fail-safe rules (D126) -------------------------------
REVIEW = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"   # two IDs: kept for review
DEFINITE = "https://www.linkedin.com/jobs/view/2222222"


class CodexPass2AlertIdTests(AlertImport):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-01 (High), D126 rule H1: job-ID field names are read in any capitals, in
    the query and after "#"; a link kept for review (an unreadable or conflicting ID) never makes a later definite link
    to a job count as seen before, in the saved-email import and the Gmail pull alike."""
    VIEW = "https://www.linkedin.com/jobs/view/9123456"

    def fresh(self):
        self.store = open_store(":memory:", FixedClock("2026-11-09T12:00:00.000Z"), SAVED_POLICY.fingerprint)
        self.ctx = Context(self.store, SAVED_POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX,
                           Path(self.tmp.name) / "snap")

    def test_job_id_fields_are_read_in_any_capitals(self):
        for url in ("https://www.linkedin.com/jobs/search/?CurrentJobId=9123456",   # Codex's input
                    "https://www.linkedin.com/jobs/search/?CURRENTJOBID=9123456",
                    "https://www.linkedin.com/jobs/search/?keywords=analyst&jobid=9123456",
                    "https://www.linkedin.com/jobs/search/#CurrentJobId=9123456",
                    "https://www.linkedin.com/jobs/collections/recommended/?currentjobid=9123456"):
            with self.subTest(url=url):
                self.assertEqual(canonical_job_url(url), (self.VIEW, "linkedin", True))
                alert = parse_eml_bytes(digest(url))
                self.assertEqual(([l.url for l in alert.links], alert.skipped), ([self.VIEW], {}))
        both = "https://www.linkedin.com/jobs/search/?CurrentJobId=unavailable&JobId=9123456"   # both fields read (D151: review)
        self.assertEqual([l.url for l in parse_eml_bytes(digest(both)).links], [both])
        self.assertTrue(kept_for_review(both))
        self.assertEqual(canonical_job_url("https://www.indeed.com/jobs?q=analyst&JK=ABCDEF0123456789"),
                         ("https://www.indeed.com/viewjob?jk=abcdef0123456789", "indeed", True))
        self.assertEqual(canonical_job_url("https://www.indeed.com/jobs?q=analyst&Vjk=ABCDEF0123456789"),
                         ("https://www.indeed.com/viewjob?jk=abcdef0123456789", "indeed", True))
        self.assertEqual(canonical_job_url("https://www.glassdoor.com/Job/x.htm?JobListingID=1009876543"),
                         ("https://www.glassdoor.com/job-listing/j?jl=1009876543", "glassdoor", True))
        self.assertEqual(canonical_job_url("https://www.glassdoor.com/Job/x.htm?JL=1009876543"),
                         ("https://www.glassdoor.com/job-listing/j?jl=1009876543", "glassdoor", True))

    def test_controls_in_other_capitals_keep_their_reading(self):
        conflicting = "https://www.linkedin.com/jobs/search/?CurrentJobId=1111111&JOBID=2222222"
        self.assertFalse(canonical_job_url(conflicting)[2])   # two IDs in any capitals: still no single job
        alert = parse_eml_bytes(digest(conflicting, "https://www.linkedin.com/jobs/search/?keywords=analyst&CurrentJobId="))
        self.assertEqual(([l.url for l in alert.links], alert.skipped), ([conflicting], {"navigation": 1}))

    def import_both(self, first: str, second: str, via: str) -> Counter:
        """Two emails, one link each, the same title from the same employer; the second's counts."""
        if via == "saved":
            self.run_import(digest(first), "first.eml")
            return self.run_import(digest(second), "second.eml")
        reader = mock.Mock()
        reader.search.return_value = ["m1", "m2"]
        reader.raw.side_effect = {"m1": digest(first), "m2": digest(second)}.get
        return _gmail_pull(self.ctx, self.client, "q", gmail=reader)

    def test_a_link_kept_for_review_never_hides_a_later_definite_link(self):
        for review in (REVIEW,   # Codex's input: conflicting IDs, then the canonical link to one of them
                       "https://www.linkedin.com/jobs/search/?currentJobId=unavailable",
                       "https://www.linkedin.com/jobs/search/?CurrentJobId=1111111&JOBID=2222222",
                       "https://www.linkedin.com/slink?code=synthetic"):
            for via in ("saved", "gmail"):
                with self.subTest(review=review, via=via):
                    self.fresh()
                    self.import_both(review, DEFINITE, via)
                    self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE url = ?", (DEFINITE,))["c"], 1)
                    self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE url = ?", (review,))["c"], 1)

    def test_a_definite_link_first_is_kept_whatever_follows(self):
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                self.fresh()
                self.import_both(DEFINITE, REVIEW, via)
                self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE url = ?", (DEFINITE,))["c"], 1)
                self.fresh()
                self.import_both(DEFINITE, DEFINITE.replace("view/2222222", "view/2222222/"), via)   # the same job again
                self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 1)

    def test_a_definite_lead_still_hides_a_later_copy_of_itself(self):
        """The control: two definite links to one job from one employer are one lead, as before."""
        other = "https://www.linkedin.com/jobs/search/?currentJobId=2222222"
        counts = self.import_both(DEFINITE, other, "saved")
        self.assertEqual(counts["lead_seen_before"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 1)
        for via in ("saved", "gmail"):   # the same job through another site: one lead, by employer and title
            with self.subTest(via=via):
                self.fresh()
                counts = self.import_both(DEFINITE, "https://www.indeed.com/viewjob?jk=abcdef0123456789", via)
                self.assertEqual(counts["lead_seen_before"], 1)
                self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 1)


class CodexPass2PrivateSitesTests(AlertImport):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-02 (Medium), D126 rule H2: every failure while locating or loading the
    private-sites file (nesting thousands deep, a refused path check, memory) is the one handled error, never quoting
    the file, and all three preflights hold: the email kept, Gmail unread, the GitHub inbox issues left open."""
    DEEP = "sources: " + "[" * 3000 + "]" * 3000 + "\n"   # Codex's input: 3,000 nested flow sequences

    NAMES = ("nested 3000 deep", "a mapping nested 3000 deep", "exists() refused", "locating fails", "locating recurses",
             "out of memory")

    def write(self, body: str, name: str = "private_sites.yaml") -> str:
        path = Path(self.tmp.name) / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def assert_handled(self):
        from careeros.core import private_sites
        with self.assertRaises(ValueError) as ctx:
            private_sites.load()
        self.assertTrue(str(ctx.exception).startswith("private_sites_invalid"), str(ctx.exception))
        self.assertNotIn("synthetic-secret", str(ctx.exception))   # never quotes the file or the failure's text

    def broken(self, name: str) -> contextlib.ExitStack:
        """Each failure Codex reproduced, and siblings, beside an otherwise readable file."""
        stack = contextlib.ExitStack()
        good = self.write("sources: {}\n", "good.yaml")
        files = {"nested 3000 deep": self.DEEP, "a mapping nested 3000 deep": "sources: " + "{a: " * 3000 + "b" + "}" * 3000 + "\n"}
        stack.enter_context(mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write(files[name], f"{len(files[name])}.yaml")
                                                         if name in files else good}))
        refused = mock.Mock(**{"exists.side_effect": PermissionError("synthetic-secret")})
        patches = {"exists() refused": mock.patch("careeros.core.private_sites._path", return_value=refused),
                   "locating fails": mock.patch("careeros.core.private_sites._path", side_effect=OSError("synthetic-secret")),
                   "locating recurses": mock.patch("careeros.core.private_sites._path", side_effect=RecursionError("synthetic-secret")),
                   # the settings reader parses the bytes it read itself (CR-AD-V2-07), so memory runs out in yaml.safe_load
                   "out of memory": mock.patch("careeros.core.private_sites.yaml.safe_load", side_effect=MemoryError("synthetic-secret"))}
        if name in patches:
            stack.enter_context(patches[name])
        stack.enter_context(mock.patch.dict("careeros.core.private_sites._CACHE", clear=True))   # read, never a cached reading
        return stack

    def test_every_load_failure_is_the_one_handled_error(self):
        for name in self.NAMES:
            with self.subTest(name=name), self.broken(name):
                self.assert_handled()

    def test_the_error_says_what_is_wrong(self):
        """The diagnostic names the kind of failure (and the line of a syntax error), never the file's content."""
        from careeros.core import private_sites
        for name, body, words in (("deep", self.DEEP, "nested too deeply"), ("syntax", "sources: [unterminated", "near line 1"),
                                  ("shape", "sources: [a, b]\n", "must be mappings")):
            with self.subTest(name=name), mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write(body, f"{name}.yaml")}):
                with self.assertRaises(ValueError) as ctx:
                    private_sites.load()
                self.assertIn(words, str(ctx.exception))
                self.assertNotIn("unterminated", str(ctx.exception))

    def test_the_three_preflights_hold_on_each_failure(self):
        from careeros.cli import _inbox_pull
        data = digest("https://careers.synthetic-health.example/job/123/reporting-analyst/")
        for index, name in enumerate(("nested 3000 deep", "exists() refused", "out of memory")):
            with self.subTest(name=name), self.broken(name):
                self.assertEqual(self.run_import(data, f"alert-{index}.eml")["alert_config_error"], 1)
                reader = mock.Mock()
                with self.assertRaises(gmail.GmailError) as ctx:
                    _gmail_pull(self.ctx, self.client, "q", gmail=reader)
                self.assertEqual(ctx.exception.code, "private_sites_invalid")
                reader.search.assert_not_called()
                with mock.patch("careeros.discovery.github_inbox.open_issues") as issues:
                    self.assertEqual(_inbox_pull(self.ctx, self.client, "synthetic/recommendations"), {"inbox_config_error": 1})
                issues.assert_not_called()
        self.assertEqual(len(list(self.inbox.glob("*.eml"))), 3)   # every email kept for a re-run
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM alert_messages")["c"], 0)

    def test_a_good_file_and_no_file_still_load(self):
        from careeros.core import private_sites
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": self.write("never_fetch: [synthetic.example]\n")}):
            self.assertEqual(private_sites.load()["never_fetch"], ["synthetic.example"])
        with mock.patch.dict(os.environ, {"CAREEROS_PRIVATE_SITES": str(Path(self.tmp.name) / "absent.yaml")}):
            self.assertEqual(private_sites.load(), {})


def merged_gate(profile, line, quote, name="pmp"):
    """The gate on a posting with `line` under Required Qualifications, after the AI merge of one hard record."""
    text = posting_with(line)
    raw = [{"category": "certification", "name": name, "requirement_type": "hard_required", "source_text": quote,
            "confidence": 0.95}]
    reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
    return evaluate(job(CERT_TITLE, text), reqs, profile, POLICY), reqs


def night_state_of(profile, reqs):
    """NIGHT's reading of the same requirements: the shared requirement rules and NIGHT's state."""
    result = GateResult("eligible")
    requirement_rules(result, reqs, profile, POLICY)
    return night_state(result, POLICY)


class CodexPass2CertificationTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-03 (High) and CR-HF-05 (High), D126 rule H3: a credential is preferred
    only when "helpful", "optional", "not required" or "preferred" lies plainly in its own clause; any other softening
    word in the sentence makes it "ambiguous" (CONDITIONAL), never preferred. The AI merge weakens an AI hard record only
    when the rules read the same credential (catalog key or alias) as preferred in the AI's sentence; any other
    disagreement is ambiguous, never preferred and never a rejection; without the catalog it weakens nothing."""
    CR_HF_05 = "PMP certification is required, optional weekend coverage is helpful."
    LEAK = "PMP certification is mandatory and CPC or equivalent certification is helpful."
    HELPFUL = "Familiarity with sprint planning, PMP helpful."

    def codes(self, gate):
        return {f["code"] for f in gate.hard_failures}, {f["code"] for f in gate.flags}

    def test_a_demand_in_the_credentials_own_clause_holds_whatever_follows(self):
        """D127 (Codex's Pass 3): a demand beside any softening word is unclear (CONDITIONAL), never a silent pass and
        never a rejection; the same demand with no softening word stays required."""
        for line, expected in ((self.CR_HF_05, [("pmp", "ambiguous")]),   # Codex's CR-HF-05 input
                               (self.LEAK, [("pmp", "ambiguous"), ("cpc", "ambiguous"), ("unspecified certification", "ambiguous")]),
                               ("PMP certification is required, CPC preferred.", [("pmp", "ambiguous"), ("cpc", "ambiguous")]),
                               ("PMP certification required, Agile experience a plus.", [("pmp", "ambiguous")]),
                               ("Bachelor's degree preferred, PMP certification required.", [("pmp", "ambiguous")]),
                               ("PMP certification is mandatory and CPC or equivalent experience is helpful.",
                                [("pmp", "ambiguous"), ("cpc", "ambiguous")])):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), expected)
                gate, reqs, _job = judged(person(), line)
                self.assertEqual(self.codes(gate)[0], set())
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
        gate, _reqs, _job = judged(person(), "PMP certification is required.")   # the control: no softening word
        self.assertTrue(hard_verify(gate))   # read as required: verify, never a rejection (D140 F1)
        line = "Scrum certification is required, optional weekend coverage is helpful."   # a family: checked, never passed
        self.assertEqual(mentions(line), [("family:scrum", "ambiguous")])
        gate, _reqs, _job = judged(person(), line)
        self.assertIn("certification_check", self.codes(gate)[1])
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")

    def test_the_ai_merge_never_weakens_a_credential_demanded_in_its_own_clause(self):
        """CR-HF-05: the AI's verbatim hard quote admitted a profile without PMP on d156418. Under D127 the rules read
        the line as unclear and the AI never settles it: CONDITIONAL in PRIMARY and NIGHT, never ELIGIBLE."""
        for line, quote in ((self.CR_HF_05, "PMP certification is required"), (self.CR_HF_05, self.CR_HF_05),
                            (self.LEAK, "PMP certification is mandatory"), (self.LEAK, self.LEAK)):
            for name in ("pmp", "pmp certification", "project management professional"):
                with self.subTest(line=line, quote=quote, name=name):
                    gate, reqs = merged_gate(person(), line, quote, name)
                    self.assertEqual(self.codes(gate)[0], set())
                    self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                    self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
        gate, _reqs = merged_gate(person({"name": "PMP", "status": "held"}), self.CR_HF_05, "PMP certification is required")
        self.assertNotIn("certification_required", self.codes(gate)[0])   # the control: PMP held

    def test_an_ai_name_or_alias_of_a_helpful_credential_is_matched(self):
        """Codex's input: the AI named "pmp certification" and quoted "PMP", and the helpful posting was rejected. Since
        D131 S1 the AI's certification records are dropped: the rules' reading stands (preferred on the plain form,
        unclear on Codex's line), never rejected."""
        plain = "PMP certification is helpful."
        for name in ("pmp", "pmp certification", "project management professional", "PMP Certification", "PMP\u00ae"):
            for line, quote, expected in ((plain, "PMP", {"preferred", "ambiguous"}), (plain, plain, {"preferred", "ambiguous"}),   # S1d
                                          (self.HELPFUL, "PMP", {"ambiguous"}), (self.HELPFUL, self.HELPFUL, {"ambiguous"}),
                                          (self.HELPFUL, "PMP helpful", {"ambiguous"})):
                with self.subTest(name=name, line=line, quote=quote):
                    gate, reqs = merged_gate(person(), line, quote, name)
                    self.assertNotIn("certification_required", self.codes(gate)[0])
                    self.assertEqual({r.requirement_type for r in reqs if r.category == "certification" and "cbap" not in r.name},
                                     expected)

    def test_any_other_disagreement_is_ambiguous_never_preferred_or_rejected(self):
        # D131 S1c: an AI certification counts as read by the rules only on its own line. On another line than the
        # rules' helpful PMP it is to verify (CONDITIONAL); in the same line it adds nothing (the rules' reading stands)
        for text, expected in (("PMP helpful.\n- Lead the PMO and its weekly portfolio review.", {("pmp", "preferred"), ("pmp", "ambiguous")}),
                               ("PMP helpful. Lead the PMO and its weekly portfolio review.", {("pmp", "preferred"), ("pmp", "ambiguous")})):   # S1d
            self.assertIn(("pmp", "preferred"), {(r.name, r.requirement_type) for r in extract(posting_with(text), POLICY).requirements})
            quote = "Lead the PMO and its weekly portfolio review."   # AI: a hard PMP here
            gate, reqs = merged_gate(person(), text, quote, "pmp certification")
            self.assertEqual({(r.name, r.requirement_type) for r in reqs if r.category == "certification" and r.name != "cbap"}, expected)
            self.assertNotIn("certification_required", self.codes(gate)[0])
            self.assertIn("certification_check", self.codes(gate)[1])
        line = "PMP certification, Agile experience helpful."   # rules: unclear; AI: hard
        gate, reqs = merged_gate(person(), line, line)
        self.assertEqual({r.requirement_type for r in reqs if r.name == "pmp"}, {"ambiguous"})
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")

    def test_without_the_catalog_the_merge_weakens_nothing(self):
        """Without the catalog only the same name is the same credential; their readings differ, so it is unclear."""
        gate, reqs = merged_gate(person(), self.HELPFUL, "PMP", "pmp")
        self.assertEqual({(r.name, r.requirement_type) for r in reqs if r.name == "pmp"}, {("pmp", "ambiguous")})

    def test_a_softening_word_outside_the_own_clause_is_ambiguous(self):
        for line, expected in (("PMP certification, weekend coverage optional.", [("pmp", "ambiguous")]),
                               ("PMP certification with Agile experience preferred.", [("pmp", "ambiguous")]),
                               ("PMP certification - SQL experience is helpful.", [("pmp", "ambiguous")]),
                               ("Professional certification, Agile experience a plus.",
                                [("family:agile", "ambiguous"), ("unspecified certification", "ambiguous")]),
                               ("Scrum certification, weekend coverage optional.", [("family:scrum", "ambiguous")]),
                               ("PMP is helpful but required for this role.", [("pmp", "ambiguous")]),
                               ("SQL experience required plus PMP helpful.", [("pmp", "ambiguous")]),
                               ("Bonus if you hold a PMP certification.", [("pmp", "ambiguous")]),
                               ("PMP certification will make onboarding helpful.", [("pmp", "ambiguous")]),
                               ("Certification required, though credentials from other bodies are optional.",
                                [("unspecified certification", "ambiguous")])):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), expected)
                gate, _reqs, _job = judged(person(), line)
                self.assertNotIn("certification_required", self.codes(gate)[0])
                self.assertIn("certification_check", self.codes(gate)[1])
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")

    def test_plain_softening_words_stay_preferred(self):
        for line, expected in (("PMP certification is optional.", [("pmp", "preferred")]),
                               ("Preferred: PMP certification.", [("pmp", "preferred")]),
                               ("A plus: PMP certification.", [("pmp", "preferred")]),
                               ("PMP certification is not mandatory.", [("pmp", "preferred")]),
                               ("Project management certification (e.g., PRINCE2, PMP) preferred.", [("family:project_management", "preferred")]),
                               ("PMP, CPC certification helpful.", [("pmp", "preferred"), ("cpc", "preferred")]),
                               ("CPC (AAPC) certification preferred.", [("cpc", "preferred")]),
                               ("Microsoft PL-300 preferred.", [("pl-300", "preferred")]),
                               ("A CPC certification is always helpful.", [("cpc", "preferred")])):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), expected)
                gate, _reqs, _job = judged(person(), line)
                self.assertNotIn("certification_check", self.codes(gate)[1])
                self.assertNotIn("certification_required", self.codes(gate)[0])


DEVICE_QA_PERKS = SOFTWARE_DEVICE_QA + "Company benefits include paid time off. "   # Codex's CR-HF-04 input
AUDIT_WITH_TOOLS = ENROLLMENT_AUDIT + "Experience with Postman, pytest, Selenium, and Playwright is a plus. "   # Codex's
NO_TOOL_QA = SOFTWARE_QA.replace("test automation", "regression testing")


class CodexPass2QaContextTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-04 (Medium), D126 rule H4: a QA posting that names a software-testing
    tool passes only with two different strong health-plan words; with some health-plan word but fewer it is
    CONDITIONAL generic_title_unverified, never passed; with no strong or weak word at all it is rejected, as live does.
    Perk words ("benefits", "insurance") are weak, never strong. With no tool: a strong word passes, a weak word flags,
    no context word rejects."""

    def outcome(self, title, text):
        result = run(job(title, text, location="Remote"))[1]
        codes = {f["code"] for f in result.hard_failures} & {"generic_title_no_context"}
        return ("rejected" if codes else "flagged" if "generic_title_unverified" in {f["code"] for f in result.flags}
                else "passed")

    def test_testing_tools_need_two_strong_health_plan_words(self):
        for title, text, expected in (
                ("QA Analyst", DEVICE_QA_PERKS, "flagged"),                       # Codex: was ELIGIBLE on d156418
                ("Eligibility QA Auditor", AUDIT_WITH_TOOLS, "flagged"),          # Codex: was INELIGIBLE on d156418
                ("QA Analyst", AUDIT_WITH_TOOLS, "flagged"),
                ("QA Analyst", SOFTWARE_DEVICE_QA + "We are a healthcare company. ", "flagged"),   # one strong word
                ("QA Analyst", SOFTWARE_DEVICE_QA + "Our benefits include health insurance. ", "flagged"),
                ("QA Analyst", SOFTWARE_DEVICE_QA + "You will test our provider data screens. ", "flagged"),   # one phrase
                ("Claims QA Analyst", SOFTWARE_DEVICE_QA, "flagged"),             # a title word is one strong word
                ("QA Analyst", SOFTWARE_DEVICE_QA, "flagged"),                    # Pass 1's input: never passed
                ("QA Analyst", SOFTWARE_DEVICE_QA.replace("enrollment", "pairing"), "rejected"),   # no word at all
                ("QA Analyst", NO_TOOL_QA + "Our Cypress suites cover the payment screens. ", "rejected"),
                ("QA Analyst", AUDIT_WITH_TOOLS + "Our health plan audits each payer file. ", "passed"),
                ("QA Analyst", HEALTH_PLAN_QA + "You will also maintain our Selenium regression suite. ", "passed")):
            with self.subTest(title=title, text=text[-70:]):
                self.assertEqual(self.outcome(title, text), expected)
                if expected == "flagged":
                    self.assertEqual(state_for(run(job(title, text, location="Remote"))[1], POLICY), "CONDITIONAL")

    def test_without_a_tool_strong_passes_weak_flags_nothing_rejects(self):
        for title, text, expected in (
                ("QA Analyst", NO_TOOL_QA + "Company benefits include paid time off. ", "flagged"),   # perks: weak
                ("QA Analyst", NO_TOOL_QA + "Our insurance products serve small firms. ", "flagged"),
                ("QA Analyst", NO_TOOL_QA + "You will review claims for accuracy. ", "passed"),
                ("QA Analyst", ENROLLMENT_AUDIT, "flagged"),
                ("QA Analyst", NO_TOOL_QA, "rejected"),
                ("Reporting Analyst", SOFTWARE_QA, "passed")):                   # not a QA title: the broad list
            with self.subTest(title=title, text=text[-70:]):
                self.assertEqual(self.outcome(title, text), expected)


# --- The pre-Pass-3 check of 94e42ea (check-pass3) and D126 applied more bluntly (C1-C4) ------------------------------
# Sentences the check found read as preferred, so a profile without the credential passed silently (its problems 1-5)
SILENT_ON_94E42EA = (
    ("pmp", "PMP certification (not optional)."), ("pmp", "PMP certification not optional."),
    ("pmp", "PMP certification: never optional."), ("pmp", "PMP certification · Agile experience helpful."),
    ("pmp", "PMP certification | Agile experience helpful."), ("pmp", "PMP certification / Agile experience helpful."),
    ("pmp", "PMP certification and SQL experience helpful."), ("pmp", "PMP certification (Agile experience helpful)."),
    ("pmp", "PMP certification [Agile experience helpful]."), ("pmp", "PMP certification • Agile experience helpful."),
    ("pmp", "PMP certification: Agile experience helpful."), ("pmp", "PMP certification–Agile experience helpful."),
    ("pmp", "PMP certification—optional weekend coverage is helpful."), ("pmp", "PMP certification (weekend coverage optional)."),
    ("pmp", "PMP certification helps."), ("pmp", "PMP certification to help lead projects."),
    ("cpc", "Active CPC certification to help members with coding questions."),
    ("pmp", "PMP certification from a preferred training provider."), ("cpc", "CPC certification (AAPC preferred)."),
    ("cpc", "Medical coding certification (CPC preferred)."), ("cpc", "CPC certification (bonus eligible)."),
    ("cpc", "CPC certification (Epic experience a plus)."), ("cpc", "CPC certification (ideally with 2 years of coding)."),
    ("cpc", "Current CPC certification in the desired specialty area."), ("pmp", "1) PMP certification 2) Agile experience helpful."),
    ("pmp", "PMP certification, CPC optional."), ("pmp", "PMP certification desired for leads, required for analysts."),
    ("pmp", "PMP certification is not required for interns."),
    (None, "Certification required, certification in Agile helpful."),
    (None, "Professional certification required, Scrum certification helpful."),
)
PLAIN_FORMS = (
    ("PMP certification is helpful.", [("pmp", "preferred")]), ("PMP helpful.", [("pmp", "preferred")]),
    ("PMP certification a plus.", [("pmp", "preferred")]), ("Preferred: PMP certification.", [("pmp", "preferred")]),
    ("A plus: CPC certification.", [("cpc", "preferred")]), ("PMP certification is not required.", [("pmp", "preferred")]),
    ("PMP certification not mandatory.", [("pmp", "preferred")]), ("PMP certification would be a plus.", [("pmp", "preferred")]),
    ("PMP, CPC or CCS certification is optional.", [("pmp", "preferred"), ("cpc", "preferred"), ("ccs", "preferred")]),
    ("Project Management Professional (PMI) certification desired.", [("pmp", "preferred")]),
)


class PrePass3CertificationTests(unittest.TestCase):
    """The pre-Pass-3 check of 94e42ea, problems 1-5 (D126 rule C1): a credential is preferred only in the plain form;
    any other sentence holding a softening word ("help" as a verb, a softener about another item in brackets, "AAPC
    preferred", "a preferred training provider", "(not optional)", a softener after "·", "|", "•", ":", an unspaced dash
    or "1) ... 2) ...") makes its certifications unclear (CONDITIONAL), never preferred, in the rules and after an AI hard
    record; a credential demanded in its own part of the sentence stays required."""

    def test_no_check_input_passes_silently_without_the_credential(self):
        for key, line in SILENT_ON_94E42EA:
            with self.subTest(line=line):
                self.assertNotIn("preferred", {t for _n, t in mentions(line)})
                gate, reqs, _job = judged(person(), line)
                self.assertNotEqual(state_for(gate, POLICY), "ELIGIBLE")
                self.assertNotEqual(night_state_of(person(), reqs), "ELIGIBLE")
                if key:   # the AI reads the whole line as a hard requirement of the credential
                    gate, reqs = merged_gate(person(), line, line, key)
                    self.assertNotEqual(state_for(gate, POLICY), "ELIGIBLE")

    def test_the_plain_form_stays_preferred(self):
        for line, expected in PLAIN_FORMS:
            with self.subTest(line=line):
                self.assertEqual(mentions(line), expected)
                gate, _reqs, _job = judged(person(), line)
                self.assertFalse({"certification_check", "certification_required"} & ({f["code"] for f in gate.flags}
                                                                                      | {f["code"] for f in gate.hard_failures}))

    def test_a_demand_beside_a_family_named_as_experience_stands(self):
        """A family named only as experience ("Agile experience a plus") is not where the demanded certification stands."""
        self.assertIn(("family:agile", "ambiguous"), mentions("Agile experience a plus, professional certification required."))

    def test_a_softened_part_is_never_read_as_a_demand(self):
        """"not required" holds the word "required": a part of the sentence that also softens is unclear, never hard."""
        for line in ("PMP certification is not required for interns.", "PMP certification not mandatory but strongly encouraged."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "ambiguous")])

    def test_a_sentence_without_a_softening_word_keeps_the_section_reading(self):
        for line in ("PMP certification.", "PMP certification required.", "Project Management Professional credential."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "hard_required")])
                gate, _reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")   # D140 F1: verify, never a rejection
                self.assertTrue(hard_verify(gate))

    def test_a_preferred_heading_keeps_its_reading(self):
        for line in ("PMP certification, Agile experience helpful.", "PMP certification to help lead projects."):
            with self.subTest(line=line):
                text = postings.GOOD_FIT.replace("- CBAP certification preferred\n", f"- {line}\n")
                self.assertIn(("pmp", "preferred"), {(r.name, r.requirement_type) for r in extract(text, POLICY).requirements})

    def test_codex_inputs_keep_their_outcomes(self):
        """CR-HF-05 and CR-HF-03's leak: under D127 a demand beside a softening word is unclear (CONDITIONAL), never a
        pass. CR-HF-03's sprint-planning line is not the plain form: unclear, never rejected."""
        for line, quote in ((CodexPass2CertificationTests.CR_HF_05, "PMP certification is required"),
                            (CodexPass2CertificationTests.LEAK, "PMP certification is mandatory")):
            with self.subTest(line=line):
                gate, _reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                gate, _reqs = merged_gate(person(), line, quote, "pmp certification")
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
        gate, _reqs = merged_gate(person(), CodexPass2CertificationTests.HELPFUL, "PMP", "pmp certification")
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")


class DeltaCheckC90ee6cTests(unittest.TestCase):
    """The delta check of c90ee6c (check-pass3/probes/r2_certs.py; D126): 1) only affirming adverbs belong to the plain
    form ("hardly optional" means required); 2) the demand exception holds only when the softened part is about another
    item ("required for team leads, optional for analysts" is unclear); 3) a "Preferred:" / "Optional ...:" label's colon
    is no part boundary, so a label sentence holding a demand is unclear. An AI preferred reading never overrides an
    unclear rules reading."""

    def assert_unclear(self, line, key="pmp"):
        self.assertEqual(mentions(line), [(key, "ambiguous")])
        gate, reqs, _job = judged(person(), line)
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
        self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
        for kind in ("hard_required", "preferred"):   # an AI record either way never settles it
            text = posting_with(line)
            raw = [{"category": "certification", "name": key, "requirement_type": kind, "source_text": line, "confidence": 0.95}]
            reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
            self.assertEqual(state_for(evaluate(job(CERT_TITLE, text), reqs, person(), POLICY), POLICY), "CONDITIONAL")

    def test_a_negating_adverb_is_no_plain_form(self):
        for line in ("PMP certification is hardly optional.", "PMP certification is rarely optional.",
                     "PMP certification is seldom optional.", "PMP certification is only occasionally optional."):
            with self.subTest(line=line):
                self.assert_unclear(line)

    def test_affirming_adverbs_keep_the_plain_form(self):
        for line in ("PMP certification is very helpful.", "PMP certification is strongly preferred.",
                     "PMP certification is generally considered helpful."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "preferred")])

    def test_a_softener_about_the_same_credential_voids_the_demand(self):
        for line, key in (("PMP certification is required for team leads, optional for analysts.", "pmp"),
                          ("CPC certification required within 12 months of hire, optional at application.", "cpc"),
                          ("PMP certification required and helpful.", "pmp")):
            with self.subTest(line=line):
                self.assert_unclear(line, key)

    def test_a_softener_about_another_item_keeps_the_demand(self):
        """Superseded by D127 (Codex's Pass 3): a demand beside a softening word about another item is unclear too."""
        for line in (CodexPass2CertificationTests.CR_HF_05, "Must hold PMP certification, Agile a plus.",
                     "PMP certification required, CPC certification helpful."):
            with self.subTest(line=line):
                self.assertIn(("pmp", "ambiguous"), mentions(line))
                gate, _reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")

    def test_a_label_sentence_holding_a_demand_is_unclear(self):
        for line in ("Preferred: PMP certification required.", "Preferred: must hold PMP certification.",
                     "Preferred (required for leads): PMP certification.", "Optional for analysts: PMP certification."):
            with self.subTest(line=line):
                self.assert_unclear(line)

    def test_a_demand_label_and_a_plain_label_keep_their_readings(self):
        self.assertEqual(mentions("Required: PMP certification."), [("pmp", "hard_required")])
        gate, _reqs, _job = judged(person(), "Required: PMP certification.")
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")   # D140 F1: verify, never a rejection
        self.assertTrue(hard_verify(gate))
        self.assertEqual(mentions("Preferred: PMP certification."), [("pmp", "preferred")])

    def test_a_negation_alone_is_never_a_demand(self):
        """The hand check of 65492ca: a line whose only softening words are "isn't needed", "isn't mandatory" or "no longer
        required" is unclear (CONDITIONAL), never a rejection; the control, "is needed", stays required."""
        for line in ("PMP certification isn't needed.", "PMP certification is no longer required.",
                     "PMP certification isn't mandatory.", "PMP certification is no longer mandatory for analysts."):
            with self.subTest(line=line):
                self.assertEqual(mentions(line), [("pmp", "ambiguous")])
                gate, reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
        gate, reqs, _job = judged(person(), "PMP certification is needed.")
        self.assertEqual((state_for(gate, POLICY), night_state_of(person(), reqs)), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1
        self.assertTrue(hard_verify(gate))

    def test_a_negation_that_softens_is_never_a_demand(self):
        for line in ("PMP certification isn't needed, CPC certification is helpful.",
                     "PMP certification is no longer required, CPC certification is helpful."):
            with self.subTest(line=line):
                self.assertNotIn(("pmp", "hard_required"), mentions(line))


class PrePass3MergeIdentityTests(unittest.TestCase):
    """The pre-Pass-3 check, problem 8 (D126 rule C2): the AI merge matches a certification by its catalog key, so an AI
    name such as "pmp certification", "PMP®" or "project management professional" beside the rules' "pmp" is one
    requirement: one certification_required, never two."""

    def test_one_credential_is_judged_once(self):
        for name in ("pmp certification", "PMP®", "project management professional", "pmi-pmp"):
            with self.subTest(name=name):
                gate, reqs = merged_gate(person(), "PMP certification required.", "PMP certification required.", name)
                self.assertEqual(len(hard_verify(gate)), 1)   # one "verify" for the credential (D140 F1)

    def test_a_different_credential_is_still_judged(self):
        """The control: an AI record of another credential (CAPM) beside the rules' PMP is judged on its own."""
        text = posting_with("PMP certification required.", "CAPM certification required.")   # each standalone (D138 R1)
        raw = [ai_record("capm", "hard_required", "CAPM certification required.")]
        gate = evaluate(job(CERT_TITLE, text), merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements, person(), POLICY)
        self.assertEqual(len(hard_verify(gate)), 2)   # one "verify" each (D140 F1)


class PrePass3QaStubTests(unittest.TestCase):
    """The pre-Pass-3 check, problem 6 (D126 rule C3): a QA posting whose text is under 200 characters is "unverified"
    (CONDITIONAL), as live does, before the testing-tool rule."""

    def test_a_stub_qa_posting_naming_a_tool_is_unverified(self):
        for text in ("Selenium and Playwright regression testing for our app.", "Postman API checks."):
            with self.subTest(text=text):
                result = run(job("QA Analyst", text, location="Remote"))[1]
                self.assertEqual({f["code"] for f in result.hard_failures} & {"generic_title_no_context"}, set())
                self.assertIn("generic_title_unverified", {f["code"] for f in result.flags})
                self.assertEqual(state_for(result, POLICY), "CONDITIONAL")

    def test_a_full_posting_naming_a_tool_and_no_word_is_rejected(self):
        """The control: past 200 characters the tool rule applies (no context word at all: rejected)."""
        result = run(job("QA Analyst", "Selenium and Playwright regression testing for our app. " * 5, location="Remote"))[1]
        self.assertIn("generic_title_no_context", {f["code"] for f in result.hard_failures})


class PrePass3HrefAmpersandTests(AlertImport):
    """The pre-Pass-3 check, problem 9 (D126 rule C4): an "&" in an href that starts a query parameter is never read as
    an HTML entity ("&currentJobId=" lost its ID to "&curren"); "&amp;" forms keep working."""
    VIEW = "https://www.linkedin.com/jobs/view/9123456"

    def test_a_raw_ampersand_before_a_parameter_keeps_the_job_id(self):
        for url in ("https://www.linkedin.com/jobs/search/?keywords=analyst&currentJobId=9123456",
                    "https://www.linkedin.com/comm/jobs/search?trk=x&currentJobId=9123456",
                    "https://www.linkedin.com/jobs/search/?keywords=analyst&CurrentJobId=9123456"):
            with self.subTest(url=url):
                self.assertEqual([l.url for l in parse_eml_bytes(digest(url)).links], [self.VIEW])

    def test_escaped_ampersands_and_entities_still_read(self):
        self.assertEqual([l.url for l in parse_eml_bytes(digest(
            "https://www.linkedin.com/jobs/search/?keywords=analyst&amp;currentJobId=9123456")).links], [self.VIEW])
        data = alert_email(html_body="<p><a href='https://www.indeed.com/viewjob?from=ja&amp;jk=3074d70a34bee345'>"
                                     "Claims &amp; Reporting Analyst</a> Synthetic Employer - Remote</p>")
        link = parse_eml_bytes(data).links[0]
        self.assertEqual((link.url, link.text), ("https://www.indeed.com/viewjob?jk=3074d70a34bee345", "Claims & Reporting Analyst"))


# --- Codex's Pass 3 of 6d8875d (BLOCKERS-REMAIN) and the owner's D127 (bluntest certification rule) --------------------
CR_HF_06 = "PMP certification, Agile experience helpful."
CR_HF_07 = "PMP certification is required."
CR_HF_08 = "PMP certification is required for team leads and generally optional for analysts."


def merged_states(line, raw, profile=None):
    """PRIMARY and NIGHT states for a posting with `line` under Required Qualifications after the AI merge of `raw`."""
    profile = profile or person()
    text = posting_with(line)
    reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
    return state_for(evaluate(job(CERT_TITLE, text), reqs, profile, POLICY), POLICY), night_state_of(profile, reqs), reqs


def ai_record(name, kind, quote, category="certification"):
    return {"category": category, "name": name, "requirement_type": kind, "source_text": quote, "confidence": 0.95}


class CodexPass3AlertOrderTests(AlertImport):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 3, CR-HF-01 residual (D127 B3): a definite link first, then a review link (two IDs)
    with the same title and employer: the review link was discarded as seen before. A review link is never lost: it is
    kept as its own review lead; only the very same link is dropped."""
    DIRECT = "https://www.linkedin.com/jobs/view/1111111"
    BOTH = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"

    def test_a_review_link_after_a_definite_one_is_kept(self):
        for review in (self.BOTH, "https://www.linkedin.com/jobs/search/?currentJobId=unavailable",
                       "https://www.linkedin.com/slink?code=synthetic"):
            for via in ("saved", "gmail"):
                with self.subTest(review=review, via=via):
                    CodexPass2AlertIdTests.fresh(self)
                    counts = CodexPass2AlertIdTests.import_both(self, self.DIRECT, review, via)
                    self.assertEqual(counts["lead_seen_before"], 0)
                    for url in (self.DIRECT, review):
                        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads WHERE url = ?", (url,))["c"], 1)

    def test_the_very_same_review_link_is_dropped(self):
        """The control: the same review link twice is one lead, as before."""
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                CodexPass2AlertIdTests.fresh(self)
                counts = CodexPass2AlertIdTests.import_both(self, self.BOTH, self.BOTH, via)
                self.assertEqual(counts["lead_seen_before"], 1)
                self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 1)


class CodexPass3CertificationRuleTests(unittest.TestCase):
    """Codex's Pass 3, CR-HF-05 residual and CR-HF-08 (High), D127 B1: a credential is preferred only in the plain form;
    any other sentence with a softening word makes it unclear, with no demand exception, so a role-specific optional
    clause never rejects and an explicit demand beside a softener never passes: CONDITIONAL in PRIMARY and NIGHT."""

    def test_a_role_specific_optional_clause_is_conditional(self):
        for line in (CR_HF_08, "PMP certification is required for team leads and optional for analysts.",
                     "PMP certification is required for team leads but usually optional for analysts.",
                     "PMP certification is required for team leads; it is generally optional for analysts.",
                     CodexPass2CertificationTests.CR_HF_05):
            with self.subTest(line=line):
                gate, reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
                for kind in ("hard_required", "required_with_equivalency", "preferred", "responsibility"):
                    primary, night, _reqs = merged_states(line, [ai_record("pmp certification", kind, line)])
                    self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"), kind)

    def test_without_a_softening_word_the_demand_stands(self):
        """The control: the same demand with no softening word stays required: a profile without PMP is to verify (D140 F1:
        a certification never rejects on its own)."""
        for line in ("PMP certification is required for team leads and analysts.", CR_HF_07):
            with self.subTest(line=line):
                gate, reqs, _job = judged(person(), line)
                self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
                self.assertEqual(night_state_of(person(), reqs), "CONDITIONAL")
                self.assertIn(("pmp", "hard_required"), readings(reqs))
                self.assertTrue(hard_verify(gate))


class CodexPass3MergeTests(unittest.TestCase):
    """Codex's Pass 3, CR-HF-06 and CR-HF-07 (High), D127 B2: the AI merge never weakens and never drops a rules record.
    A rules certification the AI leaves out is kept as it is; where the two read one credential (catalog identity)
    differently, the merged record is the rules' when it is hard, otherwise "ambiguous"."""

    def test_a_rules_certification_the_ai_leaves_out_is_kept(self):
        """CR-HF-06: the AI returned an unrelated skill and the rules' unclear PMP vanished (ELIGIBLE)."""
        skill = ai_record("sql", "hard_required", "3+ years of SQL experience required", category="skill")
        for line, expected in ((CR_HF_06, ("CONDITIONAL", "CONDITIONAL")), (CR_HF_07, ("CONDITIONAL", "CONDITIONAL")),
                               ("PMP or equivalent experience required.", None)):
            with self.subTest(line=line):
                primary, night, reqs = merged_states(line, [skill])
                kept = {(r.name, r.requirement_type) for r in reqs if r.category == "certification"}
                self.assertEqual(kept - {("cbap", "preferred")}, {(r.name, r.requirement_type) for r in
                                                                 extract(posting_with(line), POLICY).requirements
                                                                 if r.category == "certification"} - {("cbap", "preferred")})
                if expected:
                    self.assertEqual((primary, night), expected)

    def test_a_weaker_ai_reading_never_replaces_a_hard_rule(self):
        """CR-HF-07: an AI record named "pmp certification" typed required_with_equivalency or responsibility beside the
        rules' hard PMP gave ELIGIBLE."""
        for kind in ("required_with_equivalency", "responsibility", "preferred", "contextual", "ambiguous", "hard_required"):
            for name in ("pmp certification", "pmp", "project management professional"):
                with self.subTest(kind=kind, name=name):
                    primary, night, reqs = merged_states(CR_HF_07, [ai_record(name, kind, CR_HF_07)])
                    self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
                    self.assertIn(("pmp", "hard_required"), readings(reqs))   # the rules' hard reading kept
        primary, _night, _reqs = merged_states(CR_HF_07, [ai_record("pmp certification", "responsibility", CR_HF_07)],
                                               person({"name": "PMP", "status": "held"}))
        self.assertEqual(primary, "ELIGIBLE")   # the control: PMP held

    def test_any_other_disagreement_is_unclear(self):
        """D131 S1d: an AI preferred record beside the rules' unclear PMP stays CONDITIONAL; an AI hard or equivalency
        record beside the rules' plain-form preferred PMP adds it to verify (CONDITIONAL), never a rejection."""
        for line, kind, expected in ((CR_HF_06, "preferred", "CONDITIONAL"), ("PMP certification is helpful.", "hard_required", "CONDITIONAL"),
                                     ("PMP certification is helpful.", "required_with_equivalency", "CONDITIONAL")):
            with self.subTest(line=line, kind=kind):
                primary, night, reqs = merged_states(line, [ai_record("pmp certification", kind, line)])
                self.assertEqual((primary, night), (expected, expected))
        primary, _night, _reqs = merged_states("PMP certification is helpful.",   # the control: the readings agree
                                               [ai_record("pmp certification", "preferred", "PMP certification is helpful.")])
        self.assertEqual(primary, "ELIGIBLE")

    def test_one_credential_is_one_record(self):
        """The delta check of 741103e: two AI records of one credential (one agreeing, one an alias read as preferred)
        beside the rules' hard PMP gave two certification_required failures; one record per credential, the strongest."""
        text = posting_with(CR_HF_07)
        for raw in ([ai_record("pmp", "hard_required", CR_HF_07), ai_record("pmp certification", "preferred", "PMP certification")],
                    [ai_record("pmp certification", "preferred", "PMP certification"), ai_record("pmp", "hard_required", CR_HF_07)],
                    [ai_record("pmp", "hard_required", CR_HF_07), ai_record("PMP®", "hard_required", CR_HF_07)]):
            with self.subTest(raw=[(r["name"], r["requirement_type"]) for r in raw]):
                reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
                gate = evaluate(job(CERT_TITLE, text), reqs, person(), POLICY)
                self.assertEqual(len(hard_verify(gate)), 1)   # one "verify" for the credential (D140 F1)
        # with no rules record of it, a credential only the AI found comes in once, as "verify", never hard (D131 S1b)
        quote = "Experience leading cross-functional projects."
        text = posting_with(quote)
        for kinds in (("preferred", "hard_required"), ("hard_required", "preferred")):
            with self.subTest(kinds=kinds):
                raw = [ai_record("pmp", kinds[0], quote), ai_record("pmp certification", kinds[1], quote)]
                reqs = [r for r in merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements if r.category == "certification"]
                self.assertEqual([r.requirement_type for r in reqs if "cbap" not in r.name], ["ambiguous"])
        # the control: two different credentials are still two requirements
        text = posting_with("PMP certification is required.", "CAPM certification is required.")   # each on its own line
        raw = [ai_record("pmp", "hard_required", "PMP certification is required."),
               ai_record("capm", "hard_required", "CAPM certification is required.")]
        gate = evaluate(job(CERT_TITLE, text), merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements,
                        person(), POLICY)
        self.assertEqual(len(hard_verify(gate)), 2)   # one "verify" each (D140 F1)

    def test_an_ai_credential_on_an_unclear_unnamed_certification_is_unclear(self):
        """The AI names a credential (CPC) where the rules read an unnamed certification as unclear: never a rejection,
        whether the AI quotes the whole sentence or a part of it."""
        line = "Experience or certification in coding/billing is helpful."
        for quote in (line, "certification in coding/billing"):
            with self.subTest(quote=quote):
                primary, night, _reqs = merged_states(line, [ai_record("cpc", "hard_required", quote)])
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))

    def test_through_the_ai_reader(self):
        """The production path: AIReader with the catalog from the policy, a stubbed response omitting PMP (CR-HF-06)."""
        from careeros.extract.ai import AIReader
        from tests.test_ai import FakeClient, response
        text = posting_with(CR_HF_06)
        reader = AIReader(FakeClient(response([ai_record("sql", "hard_required", "3+ years of SQL experience required",
                                                         category="skill")])), "claude-opus-5-5", certs=CATALOG)
        reqs = reader.extract(CERT_TITLE, text, extract(text, POLICY))[0].requirements
        self.assertIn(("pmp", "ambiguous"), {(r.name, r.requirement_type) for r in reqs})
        self.assertEqual(state_for(evaluate(job(CERT_TITLE, text), reqs, person(), POLICY), POLICY), "CONDITIONAL")


class CodexPass3CodexInputsTests(unittest.TestCase):
    """Codex's Pass 3 evidence lines under D127: each gives the rule's answer in PRIMARY and NIGHT."""

    def test_cr_hf_03_lines(self):
        for line, quote, name, expected in ((CodexPass2CertificationTests.LEAK, "PMP certification is mandatory", "pmp", "CONDITIONAL"),
                                            (CodexPass2CertificationTests.HELPFUL, "PMP", "pmp certification", "CONDITIONAL"),
                                            ("PMP is preferred.", "PMP", "pmp certification", "CONDITIONAL")):   # D131 S1d
            with self.subTest(line=line):
                primary, night, _reqs = merged_states(line, [ai_record(name, "hard_required", quote)])
                self.assertEqual((primary, night), (expected, expected))
        self.assertEqual(mentions("PMP is preferred."), [("pmp", "preferred")])   # the plain form, by the rules alone

    def test_cr_hf_05_line_with_an_equivalency_record(self):
        line = CodexPass2CertificationTests.CR_HF_05
        primary, night, _reqs = merged_states(line, [ai_record("pmp certification", "required_with_equivalency", line)])
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))


QA_TITLE_TOOL = ("You will test releases of our cloud platform for a leading software provider, write test plans, track "
                 "defects to closure and report release readiness to the engineering team every sprint. ") * 2


class CodexPass3QaTitleToolTests(unittest.TestCase):
    """Codex's Pass 3, CR-HF-04 residual (D127 B4): a testing tool named in the title ("QA Analyst - Selenium") counts
    as one named in the posting, so one strong word ("provider") no longer passes it."""

    def outcome(self, title, text):
        return CodexPass2QaContextTests.outcome(self, title, text)

    def test_a_tool_in_the_title_counts(self):
        for title, text in (("QA Analyst - Selenium", QA_TITLE_TOOL), ("QA Analyst - Selenium", QA_TITLE_TOOL + "Use Selenium daily. "),
                            ("QA Analyst (Playwright)", QA_TITLE_TOOL), ("QA Auditor - Postman", QA_TITLE_TOOL)):
            with self.subTest(title=title, text=text[-30:]):
                self.assertEqual(self.outcome(title, text), "flagged")
                self.assertEqual(state_for(run(job(title, text, location="Remote"))[1], POLICY), "CONDITIONAL")

    def test_controls(self):
        """Without a tool anywhere one strong word still passes; with two strong words a title tool passes too."""
        self.assertEqual(self.outcome("QA Analyst", QA_TITLE_TOOL), "passed")
        self.assertEqual(self.outcome("QA Analyst - Selenium", QA_TITLE_TOOL + "Our health plan audits each payer file. "), "passed")


def lines_states(lines, raw, held=()):
    """PRIMARY and NIGHT states, and the certification records, for a posting with `lines` under Required Qualifications
    after the AI merge of `raw`, for a profile holding `held`."""
    profile = person(*[{"name": h, "status": "held"} for h in held])
    text = posting_with(*lines)
    reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
    return (state_for(evaluate(job(CERT_TITLE, text), reqs, profile, POLICY), POLICY), night_state_of(profile, reqs),
            [(r.name, r.requirement_type) for r in reqs if r.category == "certification"])


class CodexPass4CertificationTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 4 (D131): S1, CR-HF-09 (High): certifications are the rules' alone; the AI merge
    drops every AI certification record and keeps the rules' exactly, so no clause or alternative group is erased.
    S2, CR-HF-10 (High): an unclear certification is its own requirement, never part of an alternative group. S3,
    CR-HF-08 residual: a softening line right after a line demanding a certification makes it unclear."""
    ALT, LEAD = "PMP or CBAP certification is required.", "PMP certification is required to lead client delivery."
    CAPM = "PMP or CAPM certification is required."

    def test_cr_hf_09_distinct_clauses_and_groups_survive_the_merge(self):
        pmp_alt, pmp_lead = ai_record("pmp", "hard_required", self.ALT), ai_record("pmp", "hard_required", self.LEAD)
        cbap = ai_record("cbap", "hard_required", self.ALT)
        # the separate PMP clause stands with the rules alone; with the AI's records, CBAP is only in a group, so the AI
        # and the rules disagree and every demand is to verify (D138 R1): CONDITIONAL, never a pass
        for raw in ([pmp_alt, cbap, pmp_lead], [pmp_lead, pmp_alt, cbap], []):   # D140 F1: verify, never a rejection
            with self.subTest(raw=[(r["name"], r["source_text"][:20]) for r in raw]):
                primary, night, certs = lines_states([self.ALT, self.LEAD], raw, held=("CBAP",))
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))
                self.assertIn(("pmp", "hard_required" if not raw else "ambiguous"), certs)
        four = [pmp_alt, cbap, ai_record("pmp", "hard_required", self.CAPM), ai_record("capm", "hard_required", self.CAPM)]
        for raw, expected in ((four, "CONDITIONAL"), (list(reversed(four)), "CONDITIONAL"), ([], "ELIGIBLE")):
            with self.subTest(raw=len(raw)):   # PMP meets both groups; S1d: the AI's grouped records are to verify
                primary, night, _certs = lines_states([self.ALT, self.CAPM], raw, held=("PMP",))
                self.assertEqual((primary, night), (expected, expected))

    def test_the_ai_never_adds_changes_or_drops_a_certification(self):
        """The AI never changes or drops a rules certification record; a credential the rules recorded nowhere comes in
        only as its own "verify" (ambiguous) record (D131 S1b)."""
        for line in (CR_HF_06, CR_HF_07, "PMP certification is helpful.", self.ALT):
            text = posting_with(line)
            rules = [r for r in extract(text, POLICY).requirements if r.category == "certification"]
            names = {r.name for r in rules}
            for raw in ([], [ai_record("pmp certification", "preferred", line)], [ai_record("capm", "hard_required", line)],
                        [ai_record("pmp", "responsibility", line), ai_record("cbap", "hard_required", line)]):
                with self.subTest(line=line, raw=[(r["name"], r["requirement_type"]) for r in raw]):
                    merged = [r for r in merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements if r.category == "certification"]
                    # each rules record kept, in place; a demand turns unclear only on a line where the AI named a
                    # credential the rules did not read there (D131 S1c), never anything else
                    self.assertEqual([(r.name, r.source_text) for r in merged[:len(rules)]], [(r.name, r.source_text) for r in rules])
                    for got, had in zip(merged, rules, strict=False):
                        self.assertIn(got.requirement_type, (had.requirement_type, "ambiguous"))
                    extra = merged[len(rules):]
                    self.assertTrue(all(r.requirement_type == "ambiguous" for r in extra), extra)   # S1d: only "verify" records added
                    self.assertTrue(names)

    def test_other_categories_are_merged_as_before(self):
        """The control: a degree and a skill from the AI are kept or reconciled exactly as on live 09d6c75."""
        text = posting_with(CR_HF_07)
        raw = [ai_record("bachelor", "hard_required", "Bachelor's degree or equivalent work experience", category="education"),
               ai_record("sql", "preferred", "3+ years of SQL experience required", category="skill")]
        reqs = merge(raw, text, extract(text, POLICY), "synthetic", CATALOG).requirements
        names = {(r.category, r.name, r.requirement_type) for r in reqs}
        self.assertIn(("education", "bachelor", "hard_required"), names)
        rules_sql = [r for r in extract(text, POLICY).requirements if r.category == "skill" and r.name == "sql"]
        if rules_sql and rules_sql[0].requirement_type == "hard_required":   # a preferred AI reading of a hard rule: unclear
            self.assertIn(("skill", "sql", "ambiguous"), names)

    def test_cr_hf_10_an_unclear_credential_never_joins_an_alternative_group(self):
        line = "Must hold PMP, or CBAP is preferred."
        for raw in ([], [ai_record("pmp", "hard_required", line), ai_record("cbap", "preferred", line)]):
            with self.subTest(ai=bool(raw)):
                primary, night, _certs = lines_states([line], raw, held=("CBAP",))
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(lines_states(["Must hold PMP, but CBAP is preferred."], [], held=("CBAP",))[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_plain_alternatives_still_form_a_group(self):
        """The control: "PMP or CBAP certification is required." with CBAP held is met; with neither, to verify (D140 F1)."""
        self.assertEqual(lines_states([self.ALT], [], held=("CBAP",))[:2], ("ELIGIBLE", "ELIGIBLE"))
        self.assertEqual(lines_states([self.ALT], [])[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_cr_hf_08_a_softening_line_after_a_demand(self):
        lines = ["PMP certification is required for team leads.", "It is generally optional for analysts."]
        for raw in ([], [ai_record("pmp", "hard_required", lines[0])]):
            with self.subTest(ai=bool(raw)):
                self.assertEqual(lines_states(lines, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(lines_states([lines[0], "Optional for analysts."], [])[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_a_line_after_a_demand_that_is_no_loose_softener_leaves_it(self):
        """The controls: a line that softens nothing, a softening line that names another credential, or a softening line
        under the next heading leaves the demand required (a blank line between no longer separates: D131 S3b)."""
        for second in ("SQL experience required.", "CBAP certification is helpful."):
            with self.subTest(second=second):
                primary, night, certs = lines_states(["PMP certification is required for team leads.", second], [])
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
                self.assertIn(("pmp", "hard_required"), certs)   # the demand stays required
        for between, expected in (("\nPreferred Qualifications:\n", "hard_required"), ("\n\n", "ambiguous")):
            with self.subTest(between=between):
                text = f"Required Qualifications:\n- PMP certification is required for team leads.{between}- It is generally optional for analysts."
                self.assertIn(("pmp", expected), {(r.name, r.requirement_type) for r in extract(text, POLICY).requirements})


def text_states(text, raw=(), held=()):
    """PRIMARY and NIGHT states for a whole posting `text` after the AI merge of `raw`, for a profile holding `held`."""
    profile = person(*[{"name": h, "status": "held"} for h in held])
    reqs = merge(list(raw), text, extract(text, POLICY), "synthetic", CATALOG).requirements
    return state_for(evaluate(job(CERT_TITLE, text), reqs, profile, POLICY), POLICY), night_state_of(profile, reqs), reqs


class Round6AiOnlyCertificationTests(unittest.TestCase):
    """The independent check of 785a850 (round 5; D131 S1b): with the AI reader on, a certification only the AI found was
    dropped and the posting passed. It now goes in as its own "verify" record (ambiguous: CONDITIONAL unless held),
    never hard, never preferred, never in an alternative group, never changing a rules record."""
    INPUTS = (("CPC required.", "cpc"), ("Active CSM required.", "csm"), ("Must be a P.M.P. holder.", "pmp"),
              ("CHDA required.", "chda"))

    def test_the_checkers_inputs_are_conditional_with_an_ai_hard_record(self):
        for line, name in self.INPUTS:
            with self.subTest(line=line):
                text = posting_with(line)
                primary, night, reqs = text_states(text, [ai_record(name, "hard_required", line)])
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))
                added = [r for r in reqs if r.category == "certification" and "CBAP" not in r.source_text]
                self.assertEqual([r.requirement_type for r in added], ["ambiguous"])

    def test_a_held_ai_only_credential_is_met(self):
        """The gate treats a held ambiguous credential as met: PMP held and "Must be a P.M.P. holder." passes."""
        line = "Must be a P.M.P. holder."
        primary, night, _reqs = text_states(posting_with(line), [ai_record("pmp", "hard_required", line)], held=("PMP",))
        self.assertEqual((primary, night), ("ELIGIBLE", "ELIGIBLE"))

    def test_a_credential_the_rules_recorded_gets_nothing_added(self):
        """The control: the AI's alias of a credential the rules recorded adds no record (one certification_required)."""
        for name in ("pmp certification", "project management professional", "PMP®"):
            with self.subTest(name=name):
                text = posting_with(CR_HF_07)
                primary, night, reqs = text_states(text, [ai_record(name, "hard_required", CR_HF_07)])
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
                self.assertIn(("pmp", "hard_required"), readings(reqs))
                self.assertEqual([r.name for r in reqs if r.category == "certification" and "CBAP" not in r.source_text], ["pmp"])

    def test_an_ai_only_credential_never_joins_a_group_or_changes_one(self):
        """With "PMP or CBAP certification is required." and CBAP held, an AI-only CAPM stays its own unclear record: the
        group is met, CAPM is to verify (CONDITIONAL), and the rules' records are unchanged."""
        line = "PMP or CBAP certification is required."
        text = posting_with(line)
        primary, night, reqs = text_states(text, [ai_record("capm", "hard_required", line)], held=("CBAP",))
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(text_states(text, [], held=("CBAP",))[:2], ("ELIGIBLE", "ELIGIBLE"))   # the control: no AI record


class Round7ByLineTests(unittest.TestCase):
    """The checker's round 6 (D131 S1c): S1b matched an AI certification against the rules posting-wide, so a credential
    the rules read only as helpful on one line hid an AI hard record of it on another line (a silent pass), and an
    AI-only alternative on a rules group's line left the rules' credential to reject alone. An AI certification now
    counts as read by the rules only on its own line: otherwise it is added as "verify" (ambiguous), and every
    certification the rules demand on that line becomes ambiguous too."""

    @staticmethod
    def preferred_also(required_line, preferred_line):
        return posting_with(required_line).replace("- Power BI experience is a plus", f"- Power BI experience is a plus\n- {preferred_line}")

    def test_a_credential_read_as_helpful_elsewhere_never_hides_an_ai_demand(self):
        for text, name, quote in ((self.preferred_also("Must be a P.M.P. holder.", "PMP certification is a plus"), "pmp", "Must be a P.M.P. holder."),
                                  (self.preferred_also("CPC required.", "CPC certification a plus"), "cpc", "CPC required."),
                                  (posting_with("CPC certification is helpful.", "CPC required."), "cpc", "CPC required.")):
            with self.subTest(quote=quote, text=text[-120:]):
                self.assertEqual(text_states(text)[:2], ("ELIGIBLE", "ELIGIBLE"))   # the rules alone see no demand
                self.assertEqual(text_states(text, [ai_record(name, "hard_required", quote)])[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_an_ai_only_alternative_makes_the_rules_group_unclear(self):
        line = "PMP or PgMP certification is required."
        raw = [ai_record("pmp", "hard_required", line), ai_record("pgmp", "hard_required", line)]
        text = posting_with(line)
        for held in ((), ("PgMP",)):
            with self.subTest(held=held):
                self.assertEqual(text_states(text, raw, held)[:2], ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(text_states(text, raw, ("PMP",))[:2], ("CONDITIONAL", "CONDITIONAL"))   # PgMP still to verify

    def test_controls(self):
        """An AI alias quoting the rules' hard PMP line adds nothing; the rules' hard PMP alone stays INELIGIBLE; a plain
        group with matching AI records stays a group (met by CBAP)."""
        text = posting_with(CR_HF_07)
        primary, night, reqs = text_states(text, [ai_record("pmp certification", "hard_required", CR_HF_07)])
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(reqs))   # the AI alias changes nothing
        self.assertEqual(text_states(text)[:2], ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(text_states(text)[2]))
        line = "PMP or CBAP certification is required."
        raw = [ai_record("pmp", "hard_required", line), ai_record("cbap", "hard_required", line)]
        # S1d: both are only in a group, so the AI's hard records add them to verify: CONDITIONAL, never ELIGIBLE or
        # INELIGIBLE for a CBAP holder (a needless CONDITIONAL, accepted); the rules alone are unchanged
        self.assertEqual(text_states(posting_with(line), raw, ("CBAP",))[:2], ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(text_states(posting_with(line), [], ("CBAP",))[:2], ("ELIGIBLE", "ELIGIBLE"))

    def test_a_quote_on_no_single_line_is_still_to_verify(self):
        """A quote that spans two lines is found on no line: added as "verify", fail-safe."""
        text = posting_with("Certification:", "CHDA required.")
        quote = "Certification:\n- CHDA required."
        self.assertIn(_squash_for_test(quote), _squash_for_test(text))
        primary, night, reqs = text_states(text, [ai_record("chda", "hard_required", quote)])
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))
        self.assertIn(("chda", "ambiguous"), {(r.name, r.requirement_type) for r in reqs if r.category == "certification"})


class Round8StrengthTests(unittest.TestCase):
    """The checker's round 7 (D131 S1d): S1c's line matching let a short AI quote ("CPC") or a helpful sentence on the
    demand's own line hide an AI hard record (silent passes). The rule is now quote-free and strength-based, by catalog
    key (else normalized name): (1) a standalone rules hard record of the credential (the only certification of its
    line) adds nothing; (2) otherwise an AI hard, equivalency or ambiguous record adds it to verify (ambiguous), even
    beside a rules preferred, unclear or grouped record; (3) an AI preferred, contextual or responsibility record adds it
    only when the rules recorded nothing of it."""

    @staticmethod
    def preferred_also(required_lines, preferred_line):
        return posting_with(*required_lines).replace("- Power BI experience is a plus", f"- Power BI experience is a plus\n- {preferred_line}")

    def test_the_checkers_inputs_are_conditional(self):
        for text, name, quote in ((posting_with("CPC certification is helpful.", "CPC required."), "cpc", "CPC"),
                                  (self.preferred_also(["Must be a P.M.P. holder."], "PMP certification is a plus"), "pmp", "PMP"),
                                  (posting_with("CPC certification a plus. CPC required."), "cpc", "CPC required."),
                                  (posting_with("PMP certification is a plus. Must be a P.M.P. holder."), "pmp", "Must be a P.M.P. holder.")):
            with self.subTest(quote=quote, text=text[-90:]):
                self.assertEqual(text_states(text, [ai_record(name, "hard_required", quote)])[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_a_group_and_a_missed_demand(self):
        """"PMP or CBAP certification is required." plus a rules-missed "Must be a P.M.P. holder to lead delivery." with
        an AI hard PMP: a CBAP holder is CONDITIONAL, never passed (PMP is only in a group, never standalone)."""
        text = posting_with("PMP or CBAP certification is required.", "Must be a P.M.P. holder to lead delivery.")
        raw = [ai_record("pmp", "hard_required", "Must be a P.M.P. holder to lead delivery.")]
        self.assertEqual(text_states(text, raw, ("CBAP",))[:2], ("CONDITIONAL", "CONDITIONAL"))
        self.assertEqual(text_states(text, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))   # D138 R1: every demand to verify

    def test_controls(self):
        text = posting_with(CR_HF_07)   # a standalone rules hard PMP: an AI alias adds nothing
        primary, night, reqs = text_states(text, [ai_record("pmp certification", "hard_required", "PMP")])
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(reqs))
        self.assertEqual([r.name for r in reqs if r.category == "certification" and "CBAP" not in r.source_text], ["pmp"])
        helpful = posting_with("PMP certification is helpful.")   # held: the added "verify" record is met
        self.assertEqual(text_states(helpful, [ai_record("pmp", "hard_required", "PMP")], ("PMP",))[:2], ("ELIGIBLE", "ELIGIBLE"))

    def test_a_weak_ai_reading_adds_only_what_the_rules_never_recorded(self):
        helpful = posting_with("PMP certification is helpful.")
        for kind in ("preferred", "contextual", "responsibility"):
            with self.subTest(kind=kind):
                self.assertEqual(text_states(helpful, [ai_record("pmp", kind, "PMP")])[:2], ("ELIGIBLE", "ELIGIBLE"))
                missed = posting_with("CHDA required.")
                _p, _n, reqs = text_states(missed, [ai_record("chda", kind, "CHDA required.")])
                self.assertIn(("chda", "ambiguous"), {(r.name, r.requirement_type) for r in reqs})


def _squash_for_test(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", text or "").strip().lower()


class Round6WholeSectionTests(unittest.TestCase):
    """The owner's "Whole-section rule" (D131 S3b; the checker's round 5 residuals of CR-HF-08): a softening line anywhere
    in a requirements section (before or after, a blank or another line between) makes every certification the section
    demands unclear (CONDITIONAL); a credential demanded and softened anywhere in the requirement sections is unclear. A
    heading separates sections; a softener outside them (Benefits) never counts; a softening line naming another
    credential softens only that one."""
    DEMAND = "PMP certification is required for team leads."
    SOFT = "It is generally optional for analysts."

    def required(self, *lines, between="\n") -> str:
        return postings.GOOD_FIT.replace("- Experience with RxClaim", between.join(f"- {l}" for l in lines) + "\n- Experience with RxClaim")

    def test_the_checkers_residual_inputs_are_conditional(self):
        for label, text in (("blank line between", self.required(self.DEMAND, self.SOFT, between="\n\n")),
                            ("another line between", self.required(self.DEMAND, "SQL reporting experience.", self.SOFT)),
                            ("softener first", self.required(self.SOFT, self.DEMAND)),
                            ("the same credential", self.required("PMP certification is required.", "PMP certification is optional for analysts."))):
            for raw in ([], [ai_record("pmp", "hard_required", self.DEMAND if "team leads" in text else "PMP certification is required.")]):
                with self.subTest(label=label, ai=bool(raw)):
                    self.assertEqual(text_states(text, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_controls_stay_required(self):
        heading = postings.GOOD_FIT.replace("- Experience with RxClaim", f"- {self.DEMAND}\nNice to have:\n- {self.SOFT}\n- Experience with RxClaim")
        benefits = self.required(self.DEMAND) + "\n\nBenefits:\n- Optional dental coverage is a plus."
        duties = self.required(self.DEMAND).replace("- Write SQL to validate data",   # a duty naming PMP never softens the demand
                                                     "- Write SQL to validate data\n- Help analysts prepare for the PMP exam")
        for label, text in (("a heading between", heading), ("a softener only in Benefits", benefits),
                            ("a softener only in Responsibilities", duties),
                            ("no softener", self.required(self.DEMAND)),
                            ("another credential called helpful", self.required(self.DEMAND, "CBAP certification is helpful."))):
            with self.subTest(label=label):
                primary, night, reqs = text_states(text)
                self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
                self.assertIn(("pmp", "hard_required"), readings(reqs))   # the demand stays required

    def test_plain_alternatives_with_no_softener_still_group(self):
        text = posting_with("PMP or CBAP certification is required.")
        self.assertEqual(text_states(text, held=("CBAP",))[:2], ("ELIGIBLE", "ELIGIBLE"))

    def test_an_alternative_softened_elsewhere_never_leaves_its_partner_to_reject(self):
        """With "CBAP certification preferred" elsewhere in the posting, "PMP or CBAP certification is required." is
        unclear as a whole: a CBAP holder is CONDITIONAL, never rejected on PMP alone."""
        text = self.required("PMP or CBAP certification is required.")   # GOOD_FIT also says "CBAP certification preferred"
        self.assertEqual({(r.name, r.requirement_type) for r in extract(text, POLICY).requirements if r.category == "certification"
                          and "PMP or CBAP" in r.source_text}, {("pmp", "ambiguous"), ("cbap", "ambiguous")})
        self.assertEqual(text_states(text, held=("CBAP",))[:2], ("CONDITIONAL", "CONDITIONAL"))


class CodexPass4ReviewLinkTrackedJobTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 4, CR-HF-01 residual (D131 S4): with an admitted Foo Health board holding one
    Claims Analyst posting in Remote US, a LinkedIn search link carrying two job IDs, with the same title, employer and
    location, was absorbed into the tracked job by title (lead_ingested) and hidden from the review page. A link kept
    for review is never matched by title, employer or location: it stays an open lead the review page shows, through
    saved email and Gmail, and a later scan of the board never links it either."""
    REVIEW = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222"
    DEFINITE = "https://www.linkedin.com/jobs/view/3333333"

    def setUp(self):
        from tests import test_board_verification as tbv
        from tests.test_lead_resolution import legacy_board
        self.tbv = tbv
        self.store, self.ctx = tbv.setup()
        legacy_board(self.store)
        tbv.scan(self.ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])   # the tracked job
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name)
        self.client = tbv.FakeBoards().client()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def email(self, url) -> bytes:
        return alert_email(html_body=f'<p><a href="{url}">Claims Analyst</a> Foo Health · Remote (US)</p>')

    def lead(self, url):
        return self.store.one("SELECT status, job_id FROM leads WHERE url = ?", (url,))

    def test_a_review_link_is_never_absorbed_into_a_tracked_job(self):
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                if via == "gmail":   # a fresh store, board and tracked job for the second route
                    self.tearDown()
                    self.setUp()
                data = self.email(self.REVIEW)
                if via == "saved":
                    path = self.inbox / "alert.eml"
                    path.write_bytes(data)
                    counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-11-11")
                else:
                    reader = mock.Mock()
                    reader.search.return_value = ["m1"]
                    reader.raw.return_value = data
                    counts = _gmail_pull(self.ctx, self.client, "q", gmail=reader)
                self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
                self.assertEqual((self.lead(self.REVIEW)["status"], self.lead(self.REVIEW)["job_id"]), ("new", None))
                self.tbv.scan(self.ctx, [self.tbv.gh_job(71, "Claims Analyst", self.tbv.EX_TEXT, "foohealth")])   # rescanned
                self.assertIsNone(self.lead(self.REVIEW)["job_id"])
                self.assertIn(self.lead(self.REVIEW)["status"], ("new", "no_match"))   # shown on the review page

    def test_a_definite_link_to_the_same_title_is_still_matched(self):
        """The control: a definite link with the same title, employer and location is the tracked job, as before."""
        path = self.inbox / "alert.eml"
        path.write_bytes(self.email(self.DEFINITE))
        counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-11-11")
        self.assertEqual(counts["lead_ingested"], 1)
        self.assertEqual(self.lead(self.DEFINITE)["job_id"], "greenhouse:foohealth:71")


class CodexPass5AgreementTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 5, CR-HF-11 and CR-HF-12 (High), D138 R1: with the AI reader on, the AI and the
    rules must agree on certifications posting-wide. An AI certification agrees when the rules hold it as a standalone
    hard record (or the AI reads it only as preferred, contextual or a responsibility and the rules recorded it). Any
    disagreement turns every rules demand of the posting unclear and adds the AI's credential to verify, never grouped."""

    def test_cr_hf_11_a_catalog_alternative_the_rules_missed(self):
        line = "PMP or CPC required."
        raw = [ai_record("pmp", "hard_required", line), ai_record("cpc", "hard_required", line)]
        for held in (("CPC",), ()):
            with self.subTest(held=held):
                self.assertEqual(text_states(posting_with(line), raw, held)[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_cr_hf_12_a_quote_across_a_line_break(self):
        text = posting_with("PMP or PgMP certification is").replace("- PMP or PgMP certification is", "- PMP or PgMP certification is\nrequired.")
        raw = [ai_record("pmp", "hard_required", "PMP or PgMP certification is required."),
               ai_record("pgmp", "hard_required", "PMP or PgMP certification is required.")]
        for held in (("PgMP",), ()):
            with self.subTest(held=held):
                self.assertEqual(text_states(text, raw, held)[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_agreement_changes_nothing(self):
        """The controls: an AI alias of a standalone rules hard PMP, and an AI preferred reading of a credential the rules
        recorded, leave the rules' outcome as it is."""
        text = posting_with(CR_HF_07)
        primary, night, reqs = text_states(text, [ai_record("pmp certification", "hard_required", CR_HF_07)])
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(reqs))   # the AI alias changes nothing
        helpful = posting_with("PMP certification is helpful.", "SQL reporting experience.")
        for raw in ([ai_record("pmp", "preferred", "PMP certification is helpful.")], []):
            with self.subTest(ai=bool(raw)):
                self.assertEqual(text_states(helpful, raw)[:2], ("ELIGIBLE", "ELIGIBLE"))

    def test_a_disagreement_never_rejects_on_its_own(self):
        """An AI hard record of a credential the rules hold only as preferred turns the posting's demands unclear: a
        separate standalone PMP demand is CONDITIONAL, never a rejection, and the rules alone still reject."""
        text = posting_with(CR_HF_07, "CBAP certification is preferred.")
        raw = [ai_record("pmp", "hard_required", CR_HF_07), ai_record("cbap", "hard_required", "CBAP certification is preferred.")]
        self.assertEqual(text_states(text, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))
        self.assertIn(("pmp", "ambiguous"), readings(text_states(text, raw)[2]))   # the disagreement turns the demand unclear
        self.assertEqual(text_states(text)[:2], ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(text_states(text)[2]))


class CodexPass5SectionTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 5, CR-HF-08 residual (D138 R2): a line softens its whole section unless every
    softening sentence on it names a credential of its own; a posting with no headings is one requirements section, and
    its softening sentences count even when the extractor skips the line otherwise."""
    DEMAND = "PMP certification is required for team leads."

    def test_an_unnamed_softener_beside_a_named_one(self):
        text = posting_with(self.DEMAND, "It is generally optional for analysts. CBAP certification is preferred.")
        self.assertIn(("pmp", "ambiguous"), readings(extract(text, POLICY).requirements))   # the reading, beside the state
        for raw in ([], [ai_record("pmp", "hard_required", self.DEMAND)]):
            with self.subTest(ai=bool(raw)):
                self.assertEqual(text_states(text, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))

    def test_a_posting_with_no_headings(self):
        text = ("Synthetic Health is hiring a claims analyst for our pharmacy benefit team. " * 3 + f"\n{self.DEMAND}\n"
                "It is generally optional for analysts.\n")
        self.assertIn(("pmp", "ambiguous"), {(r.name, r.requirement_type) for r in extract(text, POLICY).requirements})
        for raw in ([], [ai_record("pmp", "hard_required", self.DEMAND)]):
            with self.subTest(ai=bool(raw)):
                self.assertEqual(text_states(text, raw)[:2], ("CONDITIONAL", "CONDITIONAL"))
        plain = text.replace("It is generally optional for analysts.\n", "")   # the control: no softener anywhere
        self.assertEqual(text_states(plain)[:2], ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(text_states(plain)[2]))

    def test_a_named_softener_alone_softens_only_its_credential(self):
        primary, night, reqs = text_states(posting_with(self.DEMAND, "CBAP certification is preferred."))
        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))   # D140 F1: verify, never a rejection
        self.assertIn(("pmp", "hard_required"), readings(reqs))   # the named softener leaves PMP required


class CodexPass5ReviewLinkTests(CodexPass4ReviewLinkTrackedJobTests):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 5, CR-HF-01 residual (D138 R3): a link kept for review matches a tracked job or
    a lead only by its byte-identical URL (query and fragment included), checked before every other step: no board hint
    (gh_jid), no normalized link, no requisition. Codex's two inputs stay open leads through saved email and Gmail."""
    GH = "https://www.linkedin.com/jobs/search/?currentJobId=1111111&jobId=2222222&gh_jid=71"
    TRACKED = "https://www.linkedin.com/jobs/search/?currentJobId=1111111"
    FRAGMENT = TRACKED + "#jobId=2222222"

    def setUp(self):
        super().setUp()
        self.client.close()
        self.boards = self.tbv.FakeBoards()   # its requests show whether a review link's board hint is fetched
        self.client = self.boards.client()

    def import_via(self, url, via):
        data = self.email(url)
        if via == "saved":
            path = self.inbox / f"alert-{via}.eml"
            path.write_bytes(data)
            return _import_alert(self.ctx, path, self.client, self.inbox, "2026-11-11")
        reader = mock.Mock()
        reader.search.return_value = [f"m-{url[-6:]}"]
        reader.raw.return_value = data
        return _gmail_pull(self.ctx, self.client, "q", gmail=reader)

    def track_at(self, url):
        self.store.writer("discovery").update("jobs", {"url": url}, "job_id = ?", ("greenhouse:foohealth:71",))

    def test_codexs_inputs_stay_open_review_leads(self):
        for url, tracked in ((self.GH, None), (self.FRAGMENT, self.TRACKED)):
            for via in ("saved", "gmail"):
                with self.subTest(url=url, via=via):
                    self.tearDown()
                    self.setUp()
                    if tracked:
                        self.track_at(tracked)
                    counts = self.import_via(url, via)
                    self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
                    self.assertEqual(self.boards.requests, [])   # no board hint (gh_jid) fetched for a review link
                    self.assertEqual((self.lead(url)["status"], self.lead(url)["job_id"]), ("new", None))

    def test_a_byte_identical_url_still_matches(self):
        """D140 F2: even the review URL tracked byte for byte stays an open lead (only you dismiss it); the control: the same
        review URL twice is one lead."""
        self.track_at(self.FRAGMENT)
        counts = self.import_via(self.FRAGMENT, "saved")
        self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
        self.assertEqual((self.lead(self.FRAGMENT)["status"], self.lead(self.FRAGMENT)["job_id"]), ("new", None))
        self.tearDown()
        self.setUp()
        self.import_via(self.GH, "saved")
        self.assertEqual(self.import_via(self.GH, "saved")["lead_seen_before"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 1)

    def test_a_tracked_jobs_normalized_link_is_not_its_url(self):
        """Round 10: a review link equal only to a tracked job's normalized (canonical) link, not to its own URL, stays an
        open lead through saved email and Gmail."""
        canonical = "https://www.linkedin.com/jobs/search?currentJobId=1111111&jobId=2222222"
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                self.tearDown()
                self.setUp()
                self.store.writer("discovery").update("jobs", {"url": canonical.replace("search?", "search/?"), "canonical_url": canonical},
                                                      "job_id = ?", ("greenhouse:foohealth:71",))
                self.assertEqual(self.import_via(canonical, via)["lead_new"], 1)
                self.assertEqual((self.lead(canonical)["status"], self.lead(canonical)["job_id"]), ("new", None))

    def test_match_lead_guard_comes_first(self):
        from types import SimpleNamespace

        from careeros.discovery import board_discovery as bd
        posting = SimpleNamespace(url=self.TRACKED, requisition_id="R-9", external_id="71", title="Claims Analyst",
                                  location="Remote", key="greenhouse:foohealth:71", ats="greenhouse", slug="foohealth")
        for url, decision in ((self.FRAGMENT, "no_match"), (self.TRACKED + "&gh_jid=71&jobId=2", "no_match"),
                              (self.TRACKED, "matched")):   # the control: a definite link matches as before
            with self.subTest(url=url):
                candidate = bd.Candidate(employer_name="Foo Health", origin="alert", posting_url=url, requisition_id="R-9",
                                         title="Claims Analyst", location="Remote")
                self.assertEqual(bd.match_lead(candidate, [posting], POLICY, "2026-11-11T00:00:00.000Z")["decision"], decision)
        same = bd.Candidate(employer_name="Foo Health", origin="alert", posting_url=self.FRAGMENT, title="Claims Analyst")
        self.assertEqual(bd.match_lead(same, [SimpleNamespace(**{**vars(posting), "url": self.FRAGMENT})], POLICY,
                                       "2026-11-11T00:00:00.000Z")["decision"], "no_match")   # D140 F2: not even byte for byte


class Round10ManualIntakeReviewLeadTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX round 10, CR-HF-01 (D138 R3 carried to manual intake): pasting, adding or importing a
    posting linked an open review lead whose link only normalized to the posting's (lead_resolution.link_same_posting's
    _same_link), so it left the review page. A lead kept for review is linked only by its byte-identical URL, checked
    first; a definite lead keeps its normal matching."""
    TRACK = "https://www.linkedin.com/jobs/search?currentJobId=1111111&jobId=2222222"   # stored as given by every route
    SEARCH = "https://www.linkedin.com/jobs/search/?currentJobId=1111111"
    VIEW = "https://www.linkedin.com/jobs/view/3333333"
    TEXT = "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n"

    def run_route(self, route, lead_url, posting_url, context="Foo Health · Remote"):
        from types import SimpleNamespace

        from careeros import cli
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import CLAIMS, add_lead, setup
        store, ctx = setup()
        add_lead(store, "lead:review", "Claims Analyst", context, url=lead_url)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            paths = vault_paths(Path(tmp))
            text = Path(tmp) / "posting.txt"
            if route == "paste":
                text.write_text(self.TEXT + CLAIMS, encoding="utf-8")
                cli.cmd_paste(ctx, paths, SimpleNamespace(file=str(text), clipboard=False, url=posting_url, company=None,
                                                          title=None, location=None))
            elif route == "add-url":
                text.write_text(CLAIMS, encoding="utf-8")
                cli.cmd_add_url(ctx, paths, SimpleNamespace(url=posting_url, text_file=str(text), employer="Foo Health",
                                                            parent=None, title="Claims Analyst", location="Remote - US"))
            else:
                paths["inbox"].mkdir(parents=True, exist_ok=True)
                (paths["inbox"] / "posting.txt").write_text(
                    f"URL: {posting_url}\nTitle: Claims Analyst\nEmployer: Foo Health\nLocation: Remote - US\n\n" + CLAIMS,
                    encoding="utf-8")
                cli.cmd_import(ctx, paths, SimpleNamespace())
        self.assertEqual(store.one("SELECT COUNT(*) c FROM jobs")["c"], 1)   # the posting itself went in
        lead = store.one("SELECT status, job_id FROM leads WHERE lead_id = 'lead:review'")
        return lead["status"], lead["job_id"] is not None

    def test_a_review_lead_stays_open(self):
        cases = [("codex #jobId", self.SEARCH + "#jobId=2222222", self.SEARCH)] + [
            (name, url, self.TRACK) for name, url in (
                ("trailing slash", self.TRACK.replace("search?", "search/?")),
                ("host case", self.TRACK.replace("www.linkedin.com", "WWW.LinkedIn.com")),
                ("http", self.TRACK.replace("https:", "http:")), ("utm", self.TRACK + "&utm_source=x"),
                ("trk", self.TRACK + "&trk=abc"), ("lever-source", self.TRACK + "&lever-source=LinkedIn"),
                ("fragment added", self.TRACK + "#x"))]
        for route in ("paste", "add-url", "import"):
            for name, lead_url, posting_url in cases:
                with self.subTest(route=route, case=name):
                    self.assertTrue(kept_for_review(lead_url))
                    self.assertEqual(self.run_route(route, lead_url, posting_url), ("new", False))

    def test_a_definite_lead_of_another_employer_or_an_agency_is_never_linked(self):
        """A posting you paste, add or import never links a definite lead at another employer, or an agency's lead, by
        its title (planted defect #784; its old test's lead, jk=bar, has been a review link since round 9). The control:
        the same employer and title is linked."""
        posting = "https://foohealth.example/careers/claims-analyst"   # a different link: only the title can match
        for route in ("paste", "add-url", "import"):
            for name, context, expected in (("another employer", "Bar Health · Remote", ("new", False)),
                                            ("an agency's lead", "Foo Health · Remote · agency=yes", ("new", False)),
                                            ("control: the same employer", "Foo Health · Remote", ("ingested", True))):
                with self.subTest(route=route, case=name):
                    self.assertFalse(kept_for_review(self.VIEW))
                    self.assertEqual(self.run_route(route, self.VIEW, posting, context), expected)

    def test_the_controls_still_link(self):
        """A definite lead with a trailing slash or a utm variant is linked as before; a byte-identical review URL is not
        (D140 F2: a review lead is never closed automatically, only dismissed)."""
        for route in ("paste", "add-url", "import"):
            for name, lead_url, posting_url, expected in (("byte-identical review URL", self.TRACK, self.TRACK, ("new", False)),
                                                          ("definite, trailing slash", self.VIEW + "/", self.VIEW, ("ingested", True)),
                                                          ("definite, utm", self.VIEW + "?utm_source=x", self.VIEW, ("ingested", True))):
                with self.subTest(route=route, case=name):
                    self.assertEqual(self.run_route(route, lead_url, posting_url), expected)


class Round11CertificationVerifyTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 6 (the owner's D140 F1): a certification requirement the profile does not meet is
    "verify the certification" (CONDITIONAL), never INELIGIBLE, in PRIMARY and NIGHT, with the rules alone and with the
    AI reader, whatever the wording. Every Codex Pass 6 input is CONDITIONAL for a profile without the credential."""
    DEMAND = "PMP certification is required for team leads."
    WRAPPED_PGMP = "PMP or PgMP certification is required."

    def inputs(self):
        pgmp = posting_with("PMP or PgMP certification is").replace("- PMP or PgMP certification is",
                                                                    "- PMP or PgMP certification is\nrequired.")
        cbap = posting_with("PMP or").replace("- PMP or", "- PMP or\nCBAP certification is required.")
        both = "PMP or CBAP certification is required."
        return (
            ("Reno", posting_with(self.DEMAND, "It is generally optional for analysts based in Reno. CBAP certification is preferred."),
             [ai_record("pmp", "hard_required", self.DEMAND)], ()),
            ("!", posting_with(self.DEMAND, "It is generally optional for analysts! CBAP certification is preferred."),
             [ai_record("pmp", "hard_required", self.DEMAND)], ()),
            ("who hold a CBAP certification", posting_with(self.DEMAND, "It is generally optional for analysts who hold a CBAP certification."),
             [ai_record("pmp", "hard_required", self.DEMAND)], ("CBAP",)),
            ("analysts are exempt", posting_with("PMP certification is required for team leads; analysts are exempt. CBAP certification is preferred."),
             [ai_record("cbap", "preferred", "CBAP certification is preferred.")], ()),
            ("a preferred family beside PMP or CPC", posting_with("PMP or CPC required. Scrum certification is preferred."),
             [ai_record("scrum", "preferred", "Scrum certification is preferred.")], ("CPC",)),
            ("an AI-only PMP", posting_with("PMP or CPC required."), [ai_record("pmp", "hard_required", "PMP or CPC required.")], ("CPC",)),
            ("a compound 'pmp or cpc'", posting_with("PMP or CPC required."),
             [ai_record("pmp or cpc", "hard_required", "PMP or CPC required.")], ("CPC",)),
            ("wrapped, an AI-only PMP", pgmp, [ai_record("pmp", "hard_required", self.WRAPPED_PGMP)], ("PgMP",)),
            ("wrapped, a compound 'pmp or pgmp'", pgmp, [ai_record("pmp or pgmp", "hard_required", self.WRAPPED_PGMP)], ("PgMP",)),
            ("wrapped 'PMP or' / 'CBAP'", cbap, [ai_record("pmp", "hard_required", both), ai_record("cbap", "hard_required", both)], ("CBAP",)),
        )

    def test_codexs_pass6_inputs_are_conditional(self):
        for label, text, raw, held in self.inputs():
            for ai in (False, True):
                for profile in (held, ()):   # Codex's profile, and one holding nothing
                    with self.subTest(case=label, ai=ai, held=profile):
                        primary, night, reqs = text_states(text, raw if ai else (), profile)
                        self.assertEqual((primary, night), ("CONDITIONAL", "CONDITIONAL"))

    def test_a_plain_demand_is_verify_and_a_held_one_is_met(self):
        text = posting_with(CR_HF_07)
        gate, reqs, _job = judged(person(), CR_HF_07)
        self.assertEqual((state_for(gate, POLICY), night_state_of(person(), reqs)), ("CONDITIONAL", "CONDITIONAL"))
        flag = hard_verify(gate)[0]
        self.assertTrue(flag["detail"].startswith("verify the certification: "), flag["detail"])
        self.assertEqual(text_states(text, held=("PMP",))[:2], ("ELIGIBLE", "ELIGIBLE"))
        for kind, expected in (("required_with_equivalency", "ELIGIBLE"), ("ambiguous", "CONDITIONAL")):   # never a rejection
            reqs = [r if r.category != "certification" else type(r)(r.category, r.name, kind, r.source_text, r.confidence, r.years)
                    for r in extract(text, POLICY).requirements]
            gate = evaluate(job(CERT_TITLE, text), reqs, person(), POLICY)
            with self.subTest(kind=kind):
                self.assertEqual((state_for(gate, POLICY), night_state_of(person(), reqs)), (expected, expected))
                flag = next(f for f in gate.flags if f["code"].startswith("certification_"))
                self.assertEqual(flag["code"], "certification_gap" if kind == "required_with_equivalency" else "certification_check")
                self.assertEqual("unclear whether required" in flag["detail"], kind == "ambiguous", flag["detail"])   # the reading shown
        self.assertEqual(text_states(posting_with("PMP certification is helpful.", "SQL reporting experience."))[:2],
                         ("ELIGIBLE", "ELIGIBLE"))   # a plainly preferred certification stays a silent note

    def test_an_equivalency_stays_a_note(self):
        """The owner's answer on D140 (2026-11-11): a named credential required "or equivalent experience" that you do not
        hold is a note (certification_gap, not material: ELIGIBLE), as on 4480b90, in PRIMARY and NIGHT, rules alone. The
        controls: a hard demand without PMP is CONDITIONAL; with PMP held there is no flag."""
        line = "PMP certification or equivalent experience required."
        gate, reqs, _job = judged(person(), line)
        self.assertEqual(readings(reqs), {("pmp", "required_with_equivalency")})
        self.assertEqual((state_for(gate, POLICY), night_state_of(person(), reqs)), ("ELIGIBLE", "ELIGIBLE"))
        self.assertEqual([f["code"] for f in gate.flags if f["code"].startswith("certification")], ["certification_gap"])
        gate, reqs, _job = judged(person(), CR_HF_07)
        self.assertEqual((state_for(gate, POLICY), night_state_of(person(), reqs)), ("CONDITIONAL", "CONDITIONAL"))
        held = person({"name": "PMP", "status": "held"})
        for text in (line, CR_HF_07):
            with self.subTest(held=text):
                gate, reqs, _job = judged(held, text)
                self.assertEqual((state_for(gate, POLICY), night_state_of(held, reqs)), ("ELIGIBLE", "ELIGIBLE"))
                self.assertFalse([f for f in gate.flags if f["code"].startswith("certification")])

    def test_another_reason_still_rejects(self):
        """The control: a posting outside the US is still INELIGIBLE in PRIMARY and NIGHT, beside its certification."""
        from careeros.qualify import night
        from tests.test_v22_night import NIGHT_ON
        text = posting_with(CR_HF_07)
        gate = evaluate(job(CERT_TITLE, text, location="London, UK"), extract(text, POLICY).requirements, person(), POLICY)
        self.assertEqual(state_for(gate, POLICY), "INELIGIBLE")
        self.assertEqual({f["code"] for f in gate.hard_failures}, {"outside_us"})
        out = night.assess(job("Billing Specialist", text, location="London, UK"), extract(text, NIGHT_ON).requirements, person(),
                           NIGHT_ON, 60.0)
        self.assertEqual(out["state"], "INELIGIBLE")
        self.assertIn("outside_us", {f["code"] for f in out["failures"]})
        self.assertFalse([f for f in out["failures"] if f["code"].startswith("certification")])


class Round11ReviewLeadNeverClosedTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 6, CR-HF-01 (the owner's D140 F2): a lead kept for review is never closed, linked or
    ingested automatically: not by intake, a rescan, matching, lead resolution or your own paste, add or import (the CLI
    and the review page's paste box, its canonical-dedupe return and a form with no link), not even by its byte-identical
    URL. Only your dismiss closes it. Codex's reproduction A, with B and F links."""
    B = "https://www.linkedin.com/jobs/search?currentJobId=1111111&jobId=2222222"
    F = B + "#jobId=3333333"
    UNAVAILABLE = "https://www.linkedin.com/jobs/search?currentJobId=unavailable"
    TEXT = "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n"

    def fresh(self, lead_url):
        from tests.test_v26_discovery import add_lead, setup
        store, ctx = setup()
        add_lead(store, "lead:review", "Claims Analyst", "Foo Health · Remote", url=lead_url)
        return store, ctx

    def submit(self, ctx, route, url, tmp):
        from types import SimpleNamespace

        from careeros import cli
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import CLAIMS
        paths = vault_paths(Path(tmp))
        text = Path(tmp) / "posting.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            if route == "paste":
                text.write_text(self.TEXT + CLAIMS, encoding="utf-8")
                cli.cmd_paste(ctx, paths, SimpleNamespace(file=str(text), clipboard=False, url=url, company=None, title=None,
                                                          location=None))
            elif route == "add-url":
                text.write_text(CLAIMS, encoding="utf-8")
                cli.cmd_add_url(ctx, paths, SimpleNamespace(url=url, text_file=str(text), employer="Foo Health", parent=None,
                                                            title="Claims Analyst", location="Remote - US"))
            else:
                paths["inbox"].mkdir(parents=True, exist_ok=True)
                (paths["inbox"] / "posting.txt").write_text(
                    f"URL: {url}\nTitle: Claims Analyst\nEmployer: Foo Health\nLocation: Remote - US\n\n" + CLAIMS, encoding="utf-8")
                cli.cmd_import(ctx, paths, SimpleNamespace())

    def lead(self, store):
        row = store.one("SELECT status, job_id FROM leads WHERE lead_id = 'lead:review'")
        return row["status"], row["job_id"]

    def test_codexs_reproduction_a_through_add_import_and_paste(self):
        for lead_url, posted in ((self.B, self.F), (self.F, self.F), (self.B, self.B), (self.UNAVAILABLE, self.UNAVAILABLE + "#jobId=3333333")):
            for route in ("add-url", "import", "paste"):
                with self.subTest(lead=lead_url, posted=posted, route=route), tempfile.TemporaryDirectory() as tmp:
                    store, ctx = self.fresh(lead_url)
                    self.submit(ctx, route, posted, tmp)
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM jobs")["c"], 1)   # the posting itself went in
                    self.assertEqual(self.lead(store), ("new", None))

    def test_paste_returning_an_existing_job(self):
        """Pasting F where a job at B already exists returns the B job (canonical dedupe): the B lead stays open."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = self.fresh(self.B)
            self.submit(ctx, "paste", self.B, tmp)
            self.submit(ctx, "paste", self.F, tmp)
            self.assertEqual(self.lead(store), ("new", None))

    def test_the_review_pages_paste_box_with_and_without_a_link(self):
        import threading
        from http.server import HTTPServer

        from careeros.review.server import make_handler
        from tests.test_applications import forms
        from tests.test_v25_audit import MID
        from tests.test_v26_discovery import CLAIMS
        from tests.test_v26_discovery import POLICY as DISCOVERY_POLICY
        for url in (self.F, self.B, ""):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                store, _ctx = self.fresh(self.B)
                token = "paste-test-token"
                server = HTTPServer(("127.0.0.1", 0), make_handler(store, MID, DISCOVERY_POLICY, token, Path(tmp) / "writer.lock", {}))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{server.server_address[1]}"
                try:
                    key = next(k for a, _t, k in forms(httpx.get(f"{base}/?t={token}").text) if a == "paste")
                    reply = httpx.post(f"{base}/paste?t={token}", data={"target": "new", "key": key, "url": url, "company": "",
                                                                        "text": self.TEXT + CLAIMS})
                    self.assertEqual(reply.status_code, 303)
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertEqual(store.one("SELECT COUNT(*) c FROM jobs")["c"], 1)
                self.assertEqual(self.lead(store), ("new", None))

    def test_the_board_matcher_with_a_review_link_on_either_side(self):
        """Codex: a tracked posting stored at the unavailable-ID link B and a definite candidate F (B plus a fragment), a
        different title and no requisition, gave matched/canonical_link. Either operand a review link: no_match."""
        from types import SimpleNamespace

        from careeros.discovery import board_discovery as bd
        self.assertTrue(kept_for_review(self.UNAVAILABLE + "#jobId=3333333"))   # since D151 a review link itself
        definite = "https://www.linkedin.com/jobs/search?jobId=3333333"
        self.assertFalse(kept_for_review(definite))
        tracked = SimpleNamespace(url=self.UNAVAILABLE, requisition_id=None, external_id="x1", title="Payment Integrity Analyst",
                                  location="Remote", key="manual:foo:1", ats="manual", slug="foo", remote_type="remote")
        candidate = bd.Candidate(employer_name="Foo Health", origin="alert", posting_url=definite, title="Claims Analyst", location="Remote")
        self.assertEqual(bd.match_lead(candidate, [tracked], POLICY, "2026-11-11T00:00:00.000Z")["decision"], "no_match")
        review = bd.Candidate(employer_name="Foo Health", origin="alert", posting_url=self.B, title="Claims Analyst", location="Remote")
        same = SimpleNamespace(**{**vars(tracked), "url": self.B, "title": "Claims Analyst"})
        self.assertEqual(bd.match_lead(review, [same], POLICY, "2026-11-11T00:00:00.000Z")["decision"], "no_match")
        plain = SimpleNamespace(**{**vars(tracked), "url": "https://www.linkedin.com/jobs/view/3333333"})   # the control
        candidate.posting_url = "https://www.linkedin.com/jobs/view/3333333/"
        self.assertEqual(bd.match_lead(candidate, [plain], POLICY, "2026-11-11T00:00:00.000Z")["decision"], "matched")

    def test_rescans_never_close_it_and_your_dismiss_does(self):
        """A rescan of the employer's board, at once and after the 45 days that close an unmatched lead, leaves a review lead
        open with no decision recorded; your dismiss closes it."""
        from careeros.review.leads import dismiss_lead
        from tests import test_board_verification as tbv
        from tests.test_lead_resolution import legacy_board
        store, ctx = tbv.setup()
        legacy_board(store)
        from tests.test_v26_discovery import add_lead
        add_lead(store, "lead:review", "Claims Analyst", "Foo Health · Remote", url=self.B)
        for days in (0, 60):
            with self.subTest(days=days):
                store.clock.advance(days=days)
                tbv.scan(ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])
                self.assertEqual(self.lead(store), ("new", None))
                self.assertEqual(store.one("SELECT COUNT(*) c FROM lead_matches WHERE lead_id = 'lead:review'")["c"], 0)
        dismiss_lead(store, "lead:review", key="dismiss-review-lead")
        self.assertEqual(self.lead(store)[0], "dismissed")


def wrapped_review(gh_jid=True):
    """Codex's Pass 7 F2 link: the conflicting LinkedIn search link (with gh_jid=71, or without), plus an encoded `url`
    field pointing to a definite LinkedIn job, wrapped three times by tracking redirects."""
    from urllib.parse import quote, urlencode
    url = "https://www.linkedin.com/jobs/search?currentJobId=1111111&jobId=2222222" + ("&gh_jid=71" if gh_jid else "")
    url += "&" + urlencode({"url": "https://www.linkedin.com/jobs/view/3333333"})
    for n in (1, 2, 3):
        url = f"https://tracking-{n}.example/rd?" + urlencode({"url": quote(url, safe="")})
    return url


class Round12AnyReadingTests(unittest.TestCase):
    """CR-2026-11-09-INTAKE-HOTFIX Pass 7, CR-HF-01 (the owner's D142, fail-safe R-a and R-b): a link is a review link if
    any reading of it says so (the parser's bounded unwrap, every wrapped layer, the fully unwrapped link); and if either
    the lead's link or the posting's is a review link, no strategy matches them. Codex's exact reproductions leave the
    lead open; definite-to-definite matching is unchanged."""
    B = "https://www.linkedin.com/jobs/search?currentJobId=unavailable"
    F = "https://www.linkedin.com/jobs/search?jobId=3333333"   # a definite lead (Codex's B#jobId=3333333 is review since D151)
    TEXT = "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n"

    def store(self, tracked=True):
        from tests import test_board_verification as tbv
        from tests.test_lead_resolution import legacy_board
        store, ctx = tbv.setup()
        if tracked:
            legacy_board(store)
            tbv.scan(ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])
        return store, ctx

    def alert(self, ctx, url, via, employer):
        import html
        data = alert_email(html_body=f'<p><a href="{html.escape(url)}">Claims Analyst</a> {employer} · Remote (US)</p>')
        from tests import test_board_verification as tbv
        client = tbv.FakeBoards().client()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            try:
                if via == "saved":
                    path = Path(tmp) / "alert.eml"
                    path.write_bytes(data)
                    return _import_alert(ctx, path, client, Path(tmp), "2026-11-12")
                reader = mock.Mock()
                reader.search.return_value = ["m1"]
                reader.raw.return_value = data
                return _gmail_pull(ctx, client, "q", gmail=reader)
            finally:
                client.close()

    def leads(self, store):
        return [(r["status"], r["job_id"]) for r in store.all("SELECT status, job_id FROM leads")]

    def test_the_wrapped_link_is_a_review_link_by_any_reading(self):
        url = wrapped_review()
        stored = parse_eml_bytes(alert_email(html_body=f'<p><a href="{url}">Claims Analyst</a> Unrelated Co</p>')).links[0]
        self.assertFalse(stored.job_like)   # the parser's bounded unwrap: a review link
        self.assertTrue(canonical_job_url(stored.url)[2])   # unwrapped once more, a definite job: one reading of several
        self.assertTrue(kept_for_review(stored.url))
        self.assertTrue(kept_for_review(url))
        self.assertFalse(kept_for_review("https://tracking-1.example/rd?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F3333333"))

    def test_codexs_wrapped_alert_with_gh_jid_stays_open(self):
        """Greenhouse 71 tracked; the triple-wrapped alert with gh_jid=71 and unrelated employer context."""
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                store, ctx = self.store()
                counts = self.alert(ctx, wrapped_review(), via, "Unrelated Employer")
                self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
                self.assertEqual(self.leads(store), [("new", None)])

    def test_codexs_wrapped_alert_without_gh_jid_through_admission_and_a_scan(self):
        from tests import test_board_verification as tbv
        from tests.test_lead_resolution import legacy_board
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                store, ctx = self.store(tracked=False)
                self.alert(ctx, wrapped_review(gh_jid=False), via, "Foo Health")
                self.assertEqual(self.leads(store), [("new", None)])
                legacy_board(store)   # its board admitted, then scanned: the same title and location
                tbv.scan(ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])
                self.assertEqual(self.leads(store), [("new", None)])

    def test_a_posting_tracked_at_a_review_link_never_links_a_definite_lead(self):
        """Codex: B (currentJobId=unavailable) on the tracked job, F = B#jobId=3333333 on a definite lead, through add-url,
        import and paste."""
        self.assertTrue(kept_for_review(self.B))
        self.assertFalse(kept_for_review(self.F))
        self.assertTrue(kept_for_review(self.B + "#jobId=3333333"))   # Codex's F: an unreadable value disagrees (D151)
        for route in ("add-url", "import", "paste"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                store, ctx = Round11ReviewLeadNeverClosedTests.fresh(self, self.F)
                Round11ReviewLeadNeverClosedTests.submit(self, ctx, route, self.B, tmp)
                self.assertEqual(store.one("SELECT COUNT(*) c FROM jobs")["c"], 1)
                self.assertEqual(self.leads(store), [("new", None)])

    def test_the_board_matcher_by_requisition_and_by_title(self):
        from types import SimpleNamespace

        from careeros.discovery import board_discovery as bd
        tracked = SimpleNamespace(url=self.B, requisition_id="R-9", external_id="x1", title="Claims Analyst", location="Remote",
                                  key="manual:foo:1", ats="manual", slug="foo", remote_type="remote")
        definite = SimpleNamespace(**{**vars(tracked), "url": "https://www.linkedin.com/jobs/view/4444444"})
        for name, fields in (("requisition", {"requisition_id": "R-9"}), ("title", {})):
            candidate = bd.Candidate(employer_name="Foo Health", origin="alert", posting_url=self.F, title="Claims Analyst",
                                     location="Remote", **fields)
            with self.subTest(by=name):
                self.assertEqual(bd.match_lead(candidate, [tracked], POLICY, "2026-11-12T00:00:00.000Z")["decision"], "no_match")
                # the control: the same posting at a definite link matches as before
                self.assertEqual(bd.match_lead(candidate, [definite], POLICY, "2026-11-12T00:00:00.000Z")["decision"], "matched")

    def test_the_alert_path_never_matches_a_job_tracked_at_a_review_link(self):
        """R-b on cli's alert path: a definite careers-page link with gh_jid=71, where Greenhouse 71 is tracked at a review
        link, is not matched to it (the board hint or the job's link alike); the lead stays open."""
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                store, ctx = self.store()
                store.writer("discovery").update("jobs", {"url": self.B, "canonical_url": self.B}, "job_id = ?",
                                                 ("greenhouse:foohealth:71",))
                counts = self.alert(ctx, "https://careers.foohealth.example/jobs?gh_jid=71", via, "Foo Health")
                self.assertEqual(counts["lead_ingested"], 0)
                self.assertEqual(self.leads(store), [("new", None)])

    def test_controls(self):
        """A definite alert link to a tracked job still links it; the owner's dismiss closes a review lead."""
        from careeros.review.leads import dismiss_lead
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                store, ctx = self.store()
                counts = self.alert(ctx, "https://job-boards.greenhouse.io/foohealth/jobs/71", via, "Foo Health")
                self.assertEqual(counts["lead_ingested"], 1)
        store, ctx = self.store()
        self.alert(ctx, wrapped_review(), "saved", "Unrelated Employer")
        lead_id = store.one("SELECT lead_id FROM leads")["lead_id"]
        dismiss_lead(store, lead_id, key="dismiss-wrapped")
        self.assertEqual(self.leads(store), [("dismissed", None)])


GH71 = "https://job-boards.greenhouse.io/foohealth/jobs/71"   # the tracked synthetic Greenhouse job
CONFLICTING = "https://www.linkedin.com/jobs/search?currentJobId=1111111&jobId=2222222"


def carrying(outer: str, inner: str, field: str = "url") -> str:
    """`outer` with a redirect field naming `inner` (URL-encoded), as a wrapper or a tracking link carries it."""
    from urllib.parse import urlencode
    return outer + ("&" if "?" in outer else "?") + urlencode({field: inner})


class D149IntakeHelpers:
    """Saved-email, Gmail and GitHub-inbox runs over a store with Greenhouse 71 tracked (D149, D151)."""
    CODEX = carrying(CONFLICTING, GH71)

    def run_alert(self, url, via, employer, tracked=True):
        store, ctx = Round12AnyReadingTests.store(self, tracked)
        counts = Round12AnyReadingTests.alert(self, ctx, url, via, employer)
        return store, ctx, counts

    def assert_open_review_lead(self, url, employer="Unrelated Employer", tracked=True):
        from tests import test_board_verification as tbv
        from tests.test_lead_resolution import legacy_board
        for via in ("saved", "gmail"):
            with self.subTest(via=via, employer=employer):
                store, ctx, counts = self.run_alert(url, via, employer, tracked)
                self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
                self.assertEqual(Round12AnyReadingTests.leads(self, store), [("new", None)])
                stored = store.one("SELECT url FROM leads")["url"]
                self.assertTrue(kept_for_review(stored), stored)   # the flag travels with the stored lead
                if not tracked:
                    legacy_board(store)   # the employer's board admitted later, then scanned with the same title
                with contextlib.redirect_stdout(io.StringIO()):   # rescanned: lead resolution runs again
                    tbv.scan(ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])
                self.assertEqual(Round12AnyReadingTests.leads(self, store), [("new", None)])

    def inbox_leads(self, href, employer="Foo Health"):
        """[(status, job_id, site)] after a GitHub inbox line carrying `href`, and a rescan of the board."""
        from careeros.cli import _record_alert
        from careeros.discovery import github_inbox
        from tests import test_board_verification as tbv
        store, ctx = Round12AnyReadingTests.store(self)
        alert = github_inbox.issue_alert({"number": 5, "title": "Job recommendations", "author": {"login": "x"},
                                          "createdAt": "2026-11-13T10:00:00Z",
                                          "body": f"Claims Analyst | {employer} | Remote (US) | {href} | synthetic\n"})
        client = tbv.FakeBoards().client()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _record_alert(ctx, alert, client)
                tbv.scan(ctx, [tbv.gh_job(71, "Claims Analyst", tbv.EX_TEXT, "foohealth")])
        finally:
            client.close()
        return [(r["status"], r["job_id"], r["site"]) for r in store.all("SELECT status, job_id, site FROM leads")]


class D149ReviewHrefSurvivesUnwrapTests(D149IntakeHelpers, unittest.TestCase):
    """Codex Pass 2 of CR-2026-11-05-COMBINED, the first changed-path High (the owner's D149 R-I1): a LinkedIn search link
    with conflicting currentJobId=1111111&jobId=2222222 and a `url=` naming Greenhouse 71 exactly was classified as a
    review link, but the mail parser kept only Greenhouse 71, so saved-email and Gmail intake ingested it against the
    tracked job. The classification is now made on the href as received and travels with the lead: a lead from a review
    href is a review lead everywhere downstream, whatever its kept URL reads as."""
    def test_codexs_reproduction_stays_an_open_review_lead(self):
        """Saved email and Gmail, with an unrelated employer context and with the tracked job's own employer."""
        self.assertTrue(kept_for_review(self.CODEX))
        self.assertEqual(canonical_job_url(self.CODEX)[0], GH71)   # its kept reading is the definite Greenhouse job
        for employer in ("Unrelated Employer", "Foo Health"):
            self.assert_open_review_lead(self.CODEX, employer)

    def test_the_review_lead_is_not_linked_once_its_board_is_admitted(self):
        self.assert_open_review_lead(self.CODEX, "Foo Health", tracked=False)

    def test_the_parser_keeps_a_review_url(self):
        for href in (self.CODEX, carrying("https://scanner-1.example/r", self.CODEX, "u")):
            with self.subTest(href=href):
                link = parse_eml_bytes(digest(href)).links[0]
                self.assertTrue(kept_for_review(link.url))
                self.assertNotEqual(link.url, GH71)

    def test_siblings_other_wrapper_fields_two_layers_and_inner_conflicts(self):
        from urllib.parse import quote
        siblings = {f"linkedin {field}=": carrying(CONFLICTING, GH71, field) for field in ("u", "dest", "redirect", "target")}
        siblings["scanner u= over Codex's link"] = carrying("https://scanner-1.example/r", self.CODEX, "u")
        siblings["two scanner layers"] = carrying("https://scanner-2.example/r",
                                                  carrying("https://scanner-1.example/r", self.CODEX, "dest"), "redirect")
        siblings["conflicting Indeed ids in the inner layer"] = carrying(
            "https://scanner-1.example/r", carrying("https://www.indeed.com/viewjob?jk=aaaaaaaa11&vjk=bbbbbbbb22", GH71), "u")
        siblings["conflicting Glassdoor ids in the inner layer"] = carrying(
            "https://scanner-1.example/r", carrying("https://www.glassdoor.com/partner/jobListing.htm?jl=111&jobListingId=222",
                                                    GH71, "dest"), "u")
        siblings["conflicting ids after the '#'"] = ("https://www.linkedin.com/jobs/search?url=" + quote(GH71, safe="")
                                                     + "#currentJobId=1111111&jobId=2222222")
        siblings["unreadable id"] = carrying("https://www.linkedin.com/jobs/search?currentJobId=unavailable", GH71)
        siblings["LinkedIn search page naming no id"] = carrying("https://www.linkedin.com/jobs/search?keywords=claims", GH71)
        siblings["LinkedIn redirect"] = carrying("https://www.linkedin.com/redir/redirect", GH71)
        for name, href in siblings.items():
            with self.subTest(sibling=name):
                self.assertTrue(kept_for_review(href))
                self.assert_open_review_lead(href, "Foo Health")

    def test_a_search_page_inside_a_conflicting_link_is_kept_not_skipped(self):
        """The kept reading is a search page, but the href as received names job IDs: kept for review, never skipped."""
        href = carrying(CONFLICTING, "https://www.linkedin.com/jobs/search?keywords=claims")
        alert = parse_eml_bytes(digest(href, title="Claims Analyst"))
        self.assertEqual(alert.skipped.get("navigation", 0), 0)
        self.assertEqual(len(alert.links), 1)
        self.assertTrue(kept_for_review(alert.links[0].url))

    def test_the_github_inbox_keeps_the_review_url(self):
        from careeros.cli import _record_alert
        from careeros.discovery import github_inbox
        from tests import test_board_verification as tbv
        store, ctx = Round12AnyReadingTests.store(self)
        alert = github_inbox.issue_alert({"number": 3, "title": "Job recommendations", "author": {"login": "x"},
                                          "createdAt": "2026-11-12T10:00:00Z",
                                          "body": f"Claims Analyst | Foo Health | Remote (US) | {self.CODEX} | synthetic\n"})
        self.assertTrue(kept_for_review(alert.links[0].url))
        client = tbv.FakeBoards().client()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                counts = _record_alert(ctx, alert, client)
        finally:
            client.close()
        self.assertEqual(counts["lead_ingested"], 0)
        self.assertEqual(Round12AnyReadingTests.leads(self, store), [("new", None)])

    def test_codexs_review_lead_is_closed_only_by_your_dismiss(self):
        from careeros.review.leads import dismiss_lead
        store, _ctx, _counts = self.run_alert(self.CODEX, "saved", "Foo Health")
        self.assertEqual(Round12AnyReadingTests.leads(self, store), [("new", None)])
        dismiss_lead(store, store.one("SELECT lead_id FROM leads")["lead_id"], key="dismiss-d149")
        self.assertEqual(Round12AnyReadingTests.leads(self, store), [("dismissed", None)])

    def test_control_a_plain_scanner_wrapper_over_the_definite_job_still_matches(self):
        for wrapper in (carrying("https://scanner-1.example/r", GH71, "u"),
                        carrying("https://scanner-2.example/r", carrying("https://scanner-1.example/r", GH71), "dest")):
            for via in ("saved", "gmail"):
                with self.subTest(wrapper=wrapper, via=via):
                    self.assertFalse(kept_for_review(wrapper))
                    store, _ctx, counts = self.run_alert(wrapper, via, "Foo Health")
                    self.assertEqual(counts["lead_ingested"], 1)
                    self.assertEqual(Round12AnyReadingTests.leads(self, store), [("ingested", "greenhouse:foohealth:71")])
                    self.assertEqual(store.one("SELECT url FROM leads")["url"], GH71)   # stored canonical, as before

    def test_control_the_direct_conflicting_link_stays_open(self):
        self.assert_open_review_lead(CONFLICTING, "Foo Health")
        self.assert_open_review_lead(CONFLICTING, "Unrelated Employer")

def _e(url: str) -> str:
    from urllib.parse import quote
    return quote(url, safe="")


# The red team's inputs against da2c3a7 (REDTEAM_ROUND1_intake_offsite.md, findings 1-5), Greenhouse 71 tracked
RT = {
    "I1 budget": "https://scanner-1.example/r?u=" + _e(GH71) + "".join(f"&u={_e(f'https://junk{i}.example/x')}" for i in range(70))
                 + "&dest=" + _e(CONFLICTING),
    "I2 view path vs currentJobId": "https://www.linkedin.com/jobs/view/1111111?currentJobId=2222222&url=" + _e(GH71),
    "I3 view path vs #currentJobId": "https://www.linkedin.com/jobs/view/1111111?url=" + _e(GH71) + "#currentJobId=2222222",
    "I4 definite LinkedIn carrying GH71": "https://www.linkedin.com/jobs/view/3333333?url=" + _e(GH71),
    "I5 fullwidth digits": "https://www.linkedin.com/jobs/search?currentJobId=１１１１１１１&jobId=2222222&url="
                           + _e(GH71),
    "I6 zero-width field name": "https://www.linkedin.com/jobs/search?current\u200bJobId=1111111&jobId=2222222&url=" + _e(GH71),
    "I12 search page beside GH71": "https://scanner-1.example/r?u=" + _e("https://www.linkedin.com/jobs/search?keywords=claims")
                                   + "&dest=" + _e(GH71),
    "X1 view path vs currentJobId, no wrapper": "https://www.linkedin.com/jobs/view/1111111?currentJobId=2222222",
    "X2 definite Indeed carrying GH71": "https://www.indeed.com/viewjob?jk=aaaaaaaa11&url=" + _e(GH71),
}


class D149D151FailClosedTests(D149IntakeHelpers, unittest.TestCase):
    """The red team's round 1 on the D149 intake fix (findings 1-5) and the owner's D151, applied under D149: the review
    classification fails closed by construction. (1) After NFKC and removing invisible characters, every job the href
    names anywhere (the path, every job-ID field, every wrapped link at every layer) must be one and the same job; (2) a
    bound hit (12 layers, 64 links) or a wrapped link the reader cannot follow makes it a review link; (3) a href naming
    any job is never dropped as a search page; (4) the lead's site is its stored link's."""

    def test_finding_1_and_1b_a_path_id_against_a_job_id_field(self):
        for name in ("I2 view path vs currentJobId", "I3 view path vs #currentJobId", "X1 view path vs currentJobId, no wrapper"):
            for employer in ("Foo Health", "Unrelated Employer"):
                with self.subTest(input=name):
                    self.assertTrue(kept_for_review(RT[name]))
                    self.assert_open_review_lead(RT[name], employer)

    def test_finding_1_board_admitted_later(self):
        self.assert_open_review_lead(RT["I2 view path vs currentJobId"], "Foo Health", tracked=False)

    def test_finding_2_a_bound_hit_is_a_review_link(self):
        self.assertTrue(kept_for_review(RT["I1 budget"]))
        for employer in ("Foo Health", "Unrelated Employer"):
            self.assert_open_review_lead(RT["I1 budget"], employer)
        deep = GH71
        for n in range(12):   # 12 wrappers over GH71: 13 layers, one past the depth bound
            deep = carrying(f"https://scanner-{n}.example/r", deep, "u")
        self.assertTrue(kept_for_review(deep))
        self.assert_open_review_lead(deep, "Foo Health")
        shallow = GH71
        for n in range(11):   # 11 wrappers: 12 layers, within the bound, read to the end: definite
            shallow = carrying(f"https://scanner-{n}.example/r", shallow, "u")
        self.assertFalse(kept_for_review(shallow))
        # a wrapped link the reader cannot follow (another scheme) is never read as definite
        unreadable = carrying(carrying("https://scanner-1.example/r", GH71, "u"), "ftp://files.example/job/1", "dest")
        self.assertTrue(kept_for_review(unreadable))
        self.assert_open_review_lead(unreadable, "Foo Health")

    def test_finding_3_unicode_forms_and_two_definite_jobs(self):
        """I5/I6: NFKC and invisible characters removed, the two IDs conflict again. I4/X2: the outer link names one job
        and the wrapped link another: a review link (the owner's accepted cost for offsite-apply wrappers)."""
        for name in ("I5 fullwidth digits", "I6 zero-width field name"):
            for employer in ("Foo Health", "Unrelated Employer"):
                with self.subTest(input=name):
                    self.assertTrue(kept_for_review(RT[name]))
                    self.assert_open_review_lead(RT[name], employer)
        for name in ("I4 definite LinkedIn carrying GH71", "X2 definite Indeed carrying GH71"):
            with self.subTest(input=name):
                self.assertTrue(kept_for_review(RT[name]))
                self.assert_open_review_lead(RT[name], "Unrelated Employer")

    def test_finding_3_unicode_forms_without_a_wrapper(self):
        """With no wrapped job beside it, only normalization shows the conflict: one ID in fullwidth digits, or a field
        name with a zero-width character, would otherwise be skipped as unreadable and the other ID taken as the job."""
        for href in ("https://www.linkedin.com/jobs/search?currentJobId=１１１１１１１&jobId=2222222",
                     "https://www.linkedin.com/jobs/search?current\u200bJobId=1111111&jobId=2222222",
                     "https://www.indeed.com/viewjob?jk=aaaaaaaa11&v\u2060jk=bbbbbbbb22"):
            with self.subTest(href=href):
                self.assertTrue(kept_for_review(href))
                self.assert_open_review_lead(href, "Foo Health")

    def test_two_scanner_layers_over_the_bare_conflicting_link(self):
        """Each layer encoded once: read only over-decoded, the outer scanner's inner link loses jobId to the scanner and
        reads as the one job 1111111; read decoded once, both IDs are seen."""
        href = carrying("https://scanner-2.example/r", carrying("https://scanner-1.example/r", CONFLICTING, "dest"), "u")
        self.assertTrue(kept_for_review(href))
        self.assert_open_review_lead(href, "Foo Health")

    def test_finding_4_a_review_href_is_never_dropped_as_a_search_page(self):
        href = RT["I12 search page beside GH71"]
        self.assertTrue(kept_for_review(href))
        alert = parse_eml_bytes(digest(href, title="Claims Analyst"))
        self.assertEqual((alert.skipped, len(alert.links)), ({}, 1))
        self.assert_open_review_lead(href, "Foo Health")
        self.assertEqual(self.inbox_leads(href), [("new", None, "linkedin")])   # the GitHub inbox agrees
        # the control: a search page naming no job anywhere is still skipped as a digest header
        header = parse_eml_bytes(digest("https://www.linkedin.com/jobs/search/?keywords=Claims%20Analyst", title="Claims Analyst"))
        self.assertEqual((header.skipped, header.links), ({"navigation": 1}, []))

    def test_finding_5_the_site_is_the_stored_links(self):
        expected = {self.CODEX: "linkedin", RT["I2 view path vs currentJobId"]: "linkedin",
                    RT["X2 definite Indeed carrying GH71"]: "indeed",
                    carrying("https://scanner-1.example/r", GH71, "u"): "greenhouse"}
        for href, site in expected.items():
            with self.subTest(href=href):
                link = parse_eml_bytes(digest(href, title="Claims Analyst")).links[0]
                self.assertEqual(link.site, site)
                store, _ctx, _counts = self.run_alert(href, "saved", "Foo Health")
                self.assertEqual(store.one("SELECT site FROM leads")["site"], site)
        self.assertEqual(self.inbox_leads(self.CODEX), [("new", None, "linkedin")])

    def test_the_github_inbox_table_lines(self):
        for name in ("I2 view path vs currentJobId", "X1 view path vs currentJobId, no wrapper", "I5 fullwidth digits"):
            with self.subTest(input=name):
                self.assertEqual(self.inbox_leads(RT[name]), [("new", None, "linkedin")])

    def test_an_unreadable_job_id_value_disagrees(self):
        """D151, limit 2 closed: a job-ID field the code knows, present with a value that is not a readable job ID
        ("unavailable", letters, mixed), is a reading that disagrees: a review link, even beside one readable ID."""
        hrefs = ("https://www.linkedin.com/jobs/search?currentJobId=unavailable&jobId=9123456",
                 carrying("https://www.linkedin.com/jobs/search?currentJobId=unavailable", GH71),
                 carrying("https://scanner-1.example/r", "https://www.linkedin.com/jobs/view/9123456?jobId=abc123", "u"),
                 "https://www.indeed.com/viewjob?jk=aaaaaaaa11&vjk=not-a-key",
                 "https://www.glassdoor.com/partner/jobListing.htm?jl=111&jobListingId=x111")
        for href in hrefs:
            with self.subTest(href=href):
                self.assertTrue(kept_for_review(href))
                for employer in ("Foo Health", "Unrelated Employer"):
                    self.assert_open_review_lead(href, employer)
        self.assertFalse(kept_for_review("https://www.linkedin.com/jobs/view/9123456"))   # the control: one readable ID

    def test_controls(self):
        """A plain scanner wrapper over the definite Greenhouse 71 still ingests against it; a definite LinkedIn view link
        with no conflicting reading still title-matches the tracked job; on both routes."""
        for via in ("saved", "gmail"):
            with self.subTest(via=via):
                store, _ctx, counts = self.run_alert(carrying("https://scanner-1.example/r", GH71, "u"), via, "Foo Health")
                self.assertEqual((counts["lead_ingested"], Round12AnyReadingTests.leads(self, store)),
                                 (1, [("ingested", "greenhouse:foohealth:71")]))
                store, _ctx, counts = self.run_alert("https://www.linkedin.com/jobs/view/3333333", via, "Foo Health")
                self.assertEqual((counts["lead_ingested"], Round12AnyReadingTests.leads(self, store)),
                                 (1, [("ingested", "greenhouse:foohealth:71")]))
        self.assertFalse(kept_for_review("https://www.linkedin.com/jobs/view/3333333?currentJobId=3333333"))   # one job, twice

def _query(base: str, *pairs) -> str:
    """`base` + "?" + the URL-encoded (field, value) pairs, a field possibly repeated."""
    from urllib.parse import urlencode
    return base + "?" + urlencode(list(pairs))


# Codex Pass 3 of CR-2026-11-05-COMBINED on adeb243, the changed-path Highs 2 and 3 (H1 partially resolved)
CODEX_P3_SCHEME_RELATIVE = _query("https://scanner.example.net/rd", ("url", GH71), ("redirect", "//www.linkedin.com/jobs/view/3333333"))
CODEX_P3_MALFORMED = CONFLICTING + "&" + _query("", ("url", "https://[invalid"))[1:]


class D149D155Pass3Tests(D149IntakeHelpers, unittest.TestCase):
    """The owner's D155 (blunt fail-safes after Codex Pass 3): (a) every value of every wrapper field is a destination
    reading; one that is no readable absolute link (a token, another scheme; "//host/..." is read as https) or cannot be
    parsed makes the href a review link; (b) a review href whose own outer link reads is kept as a review lead under that
    link, even when a link it wraps cannot be parsed: never skipped as malformed, and Gmail records the link."""

    def test_codexs_scheme_relative_second_redirect(self):
        self.assertTrue(kept_for_review(CODEX_P3_SCHEME_RELATIVE))
        for employer in ("Foo Health", "Unrelated Employer"):
            self.assert_open_review_lead(CODEX_P3_SCHEME_RELATIVE, employer)

    def test_codexs_ambiguous_href_with_a_malformed_destination(self):
        self.assertTrue(kept_for_review(CODEX_P3_MALFORMED))
        alert = parse_eml_bytes(digest(CODEX_P3_MALFORMED, title="Claims Analyst"))
        self.assertEqual(([(l.url, l.site) for l in alert.links], alert.skipped), ([(CODEX_P3_MALFORMED, "linkedin")], {}))
        for employer in ("Foo Health", "Unrelated Employer"):
            self.assert_open_review_lead(CODEX_P3_MALFORMED, employer)
        store, _ctx, counts = self.run_alert(CODEX_P3_MALFORMED, "gmail", "Foo Health")
        self.assertEqual(counts["link_malformed_skipped"], 0)
        self.assertEqual(store.one("SELECT links FROM alert_messages")["links"], 1)   # the receipt records the link
        self.assertEqual(self.inbox_leads(CODEX_P3_MALFORMED), [("new", None, "linkedin")])   # the GitHub inbox agrees

    def test_siblings(self):
        siblings = {
            "two redirect values, the second scheme-relative": _query(
                "https://scanner.example.net/rd", ("redirect", GH71), ("redirect", "//www.linkedin.com/jobs/view/3333333")),
            "two redirect values, both absolute": _query(
                "https://scanner.example.net/rd", ("redirect", GH71), ("redirect", "https://www.linkedin.com/jobs/view/3333333")),
            "'//' in url=, an ambiguous LinkedIn link": _query("https://scanner.example.net/rd", ("url", "//" + CONFLICTING[8:])),
            "'https://[invalid' in redirect= beside GH71": _query(
                "https://scanner.example.net/rd", ("url", GH71), ("redirect", "https://[invalid")),
            "a token beside GH71": _query("https://scanner.example.net/rd", ("url", GH71), ("u", "abc123")),
            "another scheme beside GH71": _query("https://scanner.example.net/rd", ("url", GH71), ("dest", "ftp://files.example/x")),
        }
        for name, href in siblings.items():
            with self.subTest(sibling=name):
                self.assertTrue(kept_for_review(href))
                self.assert_open_review_lead(href, "Foo Health")

    def test_a_malformed_outer_link_is_still_skipped_as_malformed(self):
        """The control of rule (b): only a review href whose own outer link reads is kept; an unreadable outer link is
        counted as malformed, as before."""
        alert = parse_eml_bytes(digest("https://[invalid?currentJobId=1111111&jobId=2222222", title="Claims Analyst"))
        self.assertEqual((alert.links, alert.skipped), ([], {"malformed": 1}))

    def test_controls(self):
        """A single definite scanner wrapper still links Greenhouse 71; the same one-value redirect, scheme-relative,
        still links it; a plain LinkedIn view link stays definite and title-matches; on saved email and Gmail."""
        for href in (carrying("https://scanner-1.example/r", GH71, "u"),
                     _query("https://scanner.example.net/rd", ("redirect", "//job-boards.greenhouse.io/foohealth/jobs/71")),
                     "https://www.linkedin.com/jobs/view/3333333"):
            self.assertFalse(kept_for_review(href))
            for via in ("saved", "gmail"):
                with self.subTest(href=href, via=via):
                    store, _ctx, counts = self.run_alert(href, via, "Foo Health")
                    self.assertEqual((counts["lead_ingested"], Round12AnyReadingTests.leads(self, store)),
                                     (1, [("ingested", "greenhouse:foohealth:71")]))


class AgencyMergeLocalLinkTests(D149IntakeHelpers, unittest.TestCase):
    """The agency-digest batch merged onto the combined candidate (2026-11-14) and the owner's D156. The combined code
    keeps a review link under a link other than its canonical form: a layer or the href itself (the owner's D149 R-I1),
    or the outer link when a link it wraps cannot be parsed (D155 rule b). That link may be one the agency batch keeps
    local: a host CareerOS cannot read as a browser does (D153 R5), one a browser may fold onto another (M1-01), or an
    agency's tracking link. D156: it is kept as a review lead, stored as a local lead (the agency mark), so it is never
    exported, published or matched; the email is read and recorded as usual. The same links on an ordinary host are
    ordinary review leads."""

    LOCAL = {"a non-ASCII wrapper host": "https://sc\u00e4nner.example.net/rd", "an xn-- wrapper host": "https://xn--scnner-cua.example.net/rd"}

    @staticmethod
    def hrefs(wrapper: str) -> dict[str, str]:
        return {"D149 R-I1: the href kept": _query(wrapper, ("url", GH71), ("redirect", "//www.linkedin.com/jobs/view/3333333")),
                "D155 rule b: the outer link kept": _query(wrapper, ("url", "https://[invalid"))}

    @staticmethod
    def stored(store) -> list[tuple]:
        """(status, job_id, url, the agency mark, is_agency_lead) per lead: the mark and is_agency_lead keep a lead out of
        the seen-jobs export, publication, board discovery and matching."""
        from careeros.discovery.alerts import AGENCY_LEAD, is_agency_lead, local_only_urls
        local = local_only_urls(store)
        return [(r["status"], r["job_id"], r["url"], str(r["lead_id"]).startswith(AGENCY_LEAD), is_agency_lead(r, local))
                for r in store.all("SELECT * FROM leads")]

    def test_a_review_link_under_a_local_link_is_kept_as_a_local_review_lead(self):
        from careeros.discovery.alerts import is_local_link
        for host, wrapper in self.LOCAL.items():
            for case, href in self.hrefs(wrapper).items():
                with self.subTest(host=host, case=case):
                    self.assertTrue(kept_for_review(href) and is_local_link(href))
                    alert = parse_eml_bytes(digest(href, title="Claims Analyst"))
                    self.assertEqual((alert.digest_status, [link.url for link in alert.links]), (None, [href]))
                    for via in ("saved", "gmail"):
                        store, _ctx, counts = self.run_alert(href, via, "Foo Health")
                        self.assertEqual((counts["lead_new"], counts["lead_ingested"]), (1, 0), via)
                        self.assertEqual(self.stored(store), [("new", None, href, True, True)], via)
                        if via == "gmail":   # read in full and recorded, never held
                            self.assertEqual(store.one("SELECT links FROM alert_messages")["links"], 1)

    def test_the_github_inbox_keeps_the_line_as_a_local_review_lead(self):
        from careeros.cli import _record_alert
        from careeros.discovery import github_inbox
        from tests import test_board_verification as tbv
        for host, wrapper in self.LOCAL.items():
            for case, href in self.hrefs(wrapper).items():
                with self.subTest(host=host, case=case):
                    alert = github_inbox.issue_alert({"number": 5, "title": "Job recommendations", "author": {"login": "x"},
                                                      "createdAt": "2026-11-13T10:00:00Z",
                                                      "body": f"Claims Analyst | Foo Health | Remote (US) | {href} | synthetic\n"})
                    self.assertEqual([link.url for link in alert.links], [href])
                    store, ctx = Round12AnyReadingTests.store(self)
                    client = tbv.FakeBoards().client()
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            _record_alert(ctx, alert, client)
                    finally:
                        client.close()
                    self.assertEqual(self.stored(store), [("new", None, href, True, True)])

    def test_controls_on_an_ordinary_host(self):
        for case, href in self.hrefs("https://scanner.example.net/rd").items():
            with self.subTest(case=case):
                alert = parse_eml_bytes(digest(href, title="Claims Analyst"))
                self.assertEqual((alert.digest_status, [link.url for link in alert.links]), (None, [href]))
                self.assert_open_review_lead(href, "Foo Health")
                store, _ctx, _counts = self.run_alert(href, "saved", "Foo Health")
                self.assertEqual(self.stored(store), [("new", None, href, False, False)])   # an ordinary review lead
                self.assertEqual(self.inbox_leads(href), [("new", None, "other")])


class AgencyMergeHandCheckTests(D149IntakeHelpers, unittest.TestCase):
    """Tests the hand check of the agency batch's merge asked for (2026-11-14): two guards the merged code reaches only
    on these paths."""

    def test_an_agency_card_whose_tracking_link_carries_gh_jid_is_never_matched_to_the_greenhouse_job(self):
        """An agency's tracking link is kept whole: its query is never a destination (CR-AD-02), so a card whose link
        happens to carry gh_jid=71 is not matched to the tracked Greenhouse 71, and nothing is fetched for it."""
        from careeros.cli import _record_alert
        from careeros.discovery.alerts import AlertEmail, Link
        from tests import test_board_verification as tbv
        from tests.test_alerts import AG_1
        store, ctx = Round12AnyReadingTests.store(self)
        card = Link(url=AG_1 + "&gh_jid=71", site="harborstaffing", text="Claims Analyst", context="Harbor Staffing · Remote",
                    job_like=True)
        alert = AlertEmail("Job Alerts", "Your jobs", None, [card], digest_status="complete")
        client = tbv.FakeBoards().client()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                counts = _record_alert(ctx, alert, client)
        finally:
            client.close()
        self.assertEqual((counts["lead_ingested"], counts["lead_new"]), (0, 1))
        self.assertEqual(Round12AnyReadingTests.leads(self, store), [("new", None)])

    def test_a_posting_whose_canonical_link_is_a_review_link_never_links_the_lead_at_its_own_link(self):
        """The owner's D142 R-b checks both of a posting's stored links. The board matcher refuses a posting tracked at a
        review link on its own, so only the same-link route reaches this check: a posting whose link is definite but
        whose canonical link is a review link never links the lead at that link. The control: both links definite."""
        from careeros.discovery.lead_resolution import link_same_posting
        F, B = Round12AnyReadingTests.F, Round12AnyReadingTests.B
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = Round11ReviewLeadNeverClosedTests.fresh(self, F)
            Round11ReviewLeadNeverClosedTests.submit(self, ctx, "add-url", B, tmp)
        job_id = store.one("SELECT job_id FROM jobs")["job_id"]
        store.writer("discovery").update("jobs", {"url": F, "canonical_url": B}, "job_id = ?", (job_id,))
        self.assertEqual(link_same_posting(ctx, job_id, via="added")["leads_linked"], 0)
        self.assertEqual(Round11ReviewLeadNeverClosedTests.lead(self, store), ("new", None))
        store.writer("discovery").update("jobs", {"canonical_url": F}, "job_id = ?", (job_id,))
        self.assertEqual(link_same_posting(ctx, job_id, via="added")["leads_linked"], 1)


if __name__ == "__main__":
    unittest.main()
