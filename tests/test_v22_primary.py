"""Prompts v2.2, PRIMARY side: eligibility states, evidence-backed rejections, pay trust, generic titles, source
status, cross-source duplicates and hiring-outcome tracking. Synthetic data only."""
import json
import unittest
from unittest import mock

import httpx

from careeros.cli import _record_alert
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery import sources
from careeros.discovery.alerts import AlertEmail, Link
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import FetchError, HttpClient
from careeros.extract.model import requirement_strength
from careeros.extract.rules import extract
from careeros.pipeline import Context
from careeros.qualify.gate import GateResult, evaluate, state_for
from careeros.review.applications import application_matrix, record_application, update_application
from careeros.review.funnel import funnel
from careeros.review.report import build_report

from tests import postings
from tests.helpers import JOB_MATRIX, add_job
from tests.test_label_findings import PROFILE
from tests.test_qualify import POLICY, job

APP = application_matrix()
LOGISTICS = ("About us\nWe move freight across North America with a modern fleet and a friendly team.\n\nWhat you will do:\n"
             "- Build dashboards for the fleet team\n- Present weekly numbers to leadership\n\nRequirements:\n"
             "- Excel skills required\n- Strong presentation skills required\nThis role is remote.\n")


def gate(title, text, **kw):
    j = job(title, text, **kw)
    return evaluate(j, extract(j["description_text"], POLICY).requirements, PROFILE, POLICY)


class StateTests(unittest.TestCase):
    def test_state_from_failures_and_flags(self):
        flag = lambda *codes: GateResult("eligible", [], [{"code": c, "detail": c, "source_text": None} for c in codes])
        self.assertEqual(state_for(GateResult("rejected", [{"code": "x"}], []), POLICY), "INELIGIBLE")
        self.assertEqual(state_for(flag("held_d13", "remote_unknown"), POLICY), "HOLD")
        self.assertEqual(state_for(flag("remote_unknown"), POLICY), "CONDITIONAL")
        self.assertEqual(state_for(flag("years_slightly_short", "degree_equivalency"), POLICY), "ELIGIBLE")
        self.assertEqual(state_for(flag(), POLICY), "ELIGIBLE")  # unposted pay is not a flag at all

    def test_conditional_and_hold_have_their_own_sections_and_never_reach_tier_a(self):
        store = open_store(":memory:", FixedClock(), "p")
        for job_id, state, title in (("gh:a:1", "CONDITIONAL", "Claims Analyst One"), ("gh:a:2", "HOLD", "Claims Analyst Two")):
            add_job(store, job_id, current_state="scored", title=title, title_norm=title.lower())
            store.writer("qualify").insert("scores", {
                "score_id": f"s:{job_id}", "job_id": job_id, "at": store.clock.now(), "policy_fingerprint": "p",
                "profile_fingerprint": "q", "eligibility": 95, "candidate_fit": 95, "career_value": 90, "freshness": 90,
                "apply_priority": 93, "tier": "near_miss", "interview_band": "High", "state": state,
                "components_json": json.dumps({"eligibility": {"matched_skills": [], "missing_skills": []},
                                               "candidate_fit": {"matched_skills": []}, "state": state})})
        page = build_report(store, PROFILE, POLICY)
        conditional = page[page.index("Conditional priority"):page.index("On hold")]
        hold = page[page.index("On hold"):page.index("Near-miss")]
        self.assertIn("Claims Analyst One", conditional)
        self.assertIn("Claims Analyst Two", hold)
        self.assertIn("Tier A: exceptional (0)", page)


class EvidenceTests(unittest.TestCase):
    def test_a_rejection_records_posting_and_profile_evidence_with_confidence_and_strength(self):
        result = gate("Business Systems Analyst", "Requirements:\n- Master's degree required\n- Excel skills required\nThis role is remote.")
        failure = next(f for f in result.hard_failures if f["code"] == "degree_required")
        self.assertIn("Master", failure["source_text"])
        self.assertTrue(failure["profile_evidence"].startswith("completed:"))
        self.assertGreater(failure["confidence"], 0)
        self.assertEqual(failure["strength"], "hard")

    def test_strength_vocabulary(self):
        self.assertEqual([requirement_strength(t) for t in ("hard_required", "required_with_equivalency", "preferred",
                                                            "responsibility", "ambiguous", "???")],
                         ["hard", "hard", "preferred", "contextual", "unknown", "unknown"])


class PayTrustTests(unittest.TestCase):
    def test_aggregator_pay_is_an_estimate_not_a_rejection(self):
        text = postings.GOOD_FIT
        low = {"comp_min": 40000, "comp_max": 50000, "comp_period": "year"}
        employer = dict(job("Business Systems Analyst", text), url="https://boards.greenhouse.io/x/jobs/1", **low)
        aggregator = dict(job("Business Systems Analyst", text), url="https://www.linkedin.com/jobs/view/1", **low)
        reqs = extract(text, POLICY).requirements
        self.assertIn("below_pay_floor", {f["code"] for f in evaluate(employer, reqs, PROFILE, POLICY).hard_failures})
        result = evaluate(aggregator, reqs, PROFILE, POLICY)
        self.assertNotIn("below_pay_floor", {f["code"] for f in result.hard_failures})
        self.assertIn("pay_untrusted_below_floor", {f["code"] for f in result.flags})


class GenericTitleTests(unittest.TestCase):
    def test_generic_titles_need_context(self):
        self.assertIn("generic_title_no_context", {f["code"] for f in gate("Data Analyst", LOGISTICS).hard_failures})
        self.assertNotIn("generic_title_no_context",
                         {f["code"] for f in gate("Data Analyst", LOGISTICS.replace("fleet team", "claims team")).hard_failures})
        self.assertNotIn("generic_title_no_context", {f["code"] for f in gate("Data Analyst, Pharmacy", LOGISTICS).hard_failures})
        stub = gate("Data Analyst", "Apply now.")
        self.assertIn("generic_title_unverified", {f["code"] for f in stub.flags})


class SourceStatusTests(unittest.TestCase):
    def test_a_source_that_is_not_allowed_is_never_fetched(self):
        calls = []
        client = HttpClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={"jobs": []})),
                            min_interval=0, sleep=lambda s: None)
        with mock.patch.object(sources, "statuses", lambda: {"greenhouse": "manual"}):
            with self.assertRaises(FetchError) as ctx:
                fetch_board(client, "greenhouse", "synth")
            self.assertEqual(ctx.exception.code, "source_manual:greenhouse")
            store = open_store(":memory:", FixedClock(), POLICY.fingerprint)
            outcomes = []
            alert = AlertEmail("s", "t", None, [Link("https://job-boards.greenhouse.io/synth/jobs/9", "greenhouse",
                                                     "Claims Analyst", "Synth Health · Remote", True)])
            _record_alert(Context(store, POLICY, PROFILE, JOB_MATRIX, None), alert, client, outcomes)
            self.assertEqual([o for _, o in outcomes], ["lead"])
        self.assertEqual(calls, [])
        self.assertEqual(sources.status("linkedin"), "manual")
        self.assertEqual(sources.status("never-heard-of-it"), "manual")
        client.close()


class LedgerTests(unittest.TestCase):
    def test_same_job_through_another_site_is_recognised(self):
        store = open_store(":memory:", FixedClock(), POLICY.fingerprint)
        ctx = Context(store, POLICY, PROFILE, JOB_MATRIX, None)
        add_job(store, "gh:synth:1", title="Claims Analyst", title_norm="claims analyst", employer_name="Synthetic Health",
                parent="Synthetic Health")
        outcomes = []
        alert = AlertEmail("s", "t", None, [
            Link("https://www.linkedin.com/jobs/view/4000000011", "linkedin", "Claims Analyst", "Synthetic Health · Remote", True),
            Link("https://www.linkedin.com/jobs/view/4000000012", "linkedin", "Payer Operations Analyst", "Other Co · Remote", True),
            Link("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a70", "indeed", "Payer Operations Analyst", "Other Co · Remote", True),
            Link("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a71", "indeed", "Claims Analyst", "Different Co · Remote", True)])
        _record_alert(ctx, alert, None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["already_tracked", "lead", "seen_before", "lead"])


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = open_store(":memory:", FixedClock(), POLICY.fingerprint), None
        add_job(self.store, "gh:synth:1", current_state="scored", source="ats_api")
        add_job(self.store, "gh:synth:2", title="Claims Analyst", title_norm="claims analyst")
        self.store.writer("discovery").insert("leads", {
            "lead_id": "lead:1", "url": "https://x.example/2", "site": "other", "title": "Claims Analyst",
            "title_norm": "claims analyst", "first_seen_at": self.store.clock.now(), "status": "ingested",
            "job_id": "gh:synth:2", "sender": "GitHub inbox (owner-x)", "profile": "primary"})

    def test_chatgpt_gmail_alert_finds_are_their_own_source(self):
        from careeros.review.applications import source_of
        add_job(self.store, "gh:synth:3", title="Payer Operations Analyst", title_norm="payer operations analyst")
        self.store.writer("discovery").insert("leads", {
            "lead_id": "lead:3", "url": "https://x.example/3", "site": "other", "title": "Payer Operations Analyst",
            "title_norm": "payer operations analyst", "first_seen_at": self.store.clock.now(), "status": "ingested",
            "job_id": "gh:synth:3", "sender": "GitHub inbox (owner-x)", "profile": "primary",
            "context": "Example Health · Remote (US) · source: LinkedIn alert"})
        job = lambda job_id: self.store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        self.assertEqual(source_of(self.store, job("gh:synth:3")), ("chatgpt_gmail_alert", "primary"))
        self.assertEqual(source_of(self.store, job("gh:synth:2")), ("chatgpt_inbox", "primary"))  # no alert tag: web find
        self.assertEqual(source_of(self.store, job("gh:synth:1")), ("board_scan", "primary"))

    def test_context_is_captured_and_confirmations_never_count_as_responses(self):
        board = record_application(self.store, APP, "gh:synth:1", "direct", key="a1")
        inbox = record_application(self.store, APP, "gh:synth:2", "agency", agency="Harbor Staffing", key="a2")
        ctx = json.loads(self.store.one("SELECT context_json FROM applications WHERE application_id = ?", (inbox,))["context_json"])
        self.assertEqual((ctx["source"], ctx["profile"]), ("chatgpt_inbox", "primary"))
        update_application(self.store, APP, board, "acknowledged", key="u1")  # an automatic confirmation only
        for n, state in enumerate(("responded", "screening", "interviewing")):
            update_application(self.store, APP, inbox, state, key=f"u{n + 2}")
        rows = {r["source"]: r for r in funnel(self.store, "source")}
        self.assertEqual((rows["board_scan"]["applied"], rows["board_scan"]["responded"]), (1, 0))
        self.assertEqual((rows["chatgpt_inbox"]["responded"], rows["chatgpt_inbox"]["interviewing"]), (1, 1))
        self.assertTrue(rows["chatgpt_inbox"]["small_sample"])
        channel = {r["channel"]: r for r in funnel(self.store, "channel")}
        self.assertEqual(sorted(channel), ["agency", "direct"])
        update_application(self.store, APP, board, "closed", key="u9")  # the posting closed: a negative outcome
        self.assertEqual(self.store.verify_job_consistency(APP, board, entity="application"), set())


if __name__ == "__main__":
    unittest.main()
