"""Prompts v2.1 policy: employer exclusion, severity-based years, domain bonus, and the separate NIGHT profile.
Synthetic data only."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from careeros.cli import _inbox_pull, _record_alert
from careeros.core.clock import FixedClock
from careeros.core.fingerprint import fingerprint
from careeros.core.policy import PolicyBundle
from careeros.db.store import open_store
from careeros.discovery import github_inbox as inbox
from careeros.discovery.alerts import AlertEmail, Link
from careeros.discovery.http import HttpClient
from careeros.extract.rules import extract
from careeros.pipeline import Context
from careeros.qualify.gate import evaluate, excluded_employer
from careeros.qualify.scores import career_value, domain_level, score

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_github_inbox import FakeGh, issue
from tests.test_label_findings import PROFILE
from tests.test_qualify import POLICY, job


def policy_with(**changes) -> PolicyBundle:
    data = copy.deepcopy(POLICY.data)
    for path, value in changes.items():
        node = data
        keys = path.split("__")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
    return PolicyBundle(data=data, fingerprint=fingerprint(data))


EXCLUDING = policy_with(gate__excluded_employers=["Excluded Co"])
NIGHT_ON = policy_with(night__enabled=True)
OVERNIGHT = """About us
We are a health plan operations team.

Responsibilities:
- Audit processed pharmacy claims for accuracy and document findings
- Review claims quality and report errors to the team lead

Requirements:
- 2+ years of claims experience required
- Excel skills required

Schedule: overnight shift, Sunday to Thursday, 11pm to 7:30am. This role is remote. Back office work, no phone queue.
"""


def gate_for(title, body, policy=POLICY, **kw):
    j = job(title, body, **kw)
    return evaluate(j, extract(j["description_text"], policy).requirements, PROFILE, policy)


class ExclusionTests(unittest.TestCase):
    def test_excluded_employers_are_rejected_with_a_neutral_reason(self):
        j = job("Business Systems Analyst", postings.GOOD_FIT, employer="Excluded Co Health Services")
        self.assertEqual(excluded_employer(j, EXCLUDING), "Excluded Co")
        result = evaluate(j, extract(j["description_text"], EXCLUDING).requirements, PROFILE, EXCLUDING)
        failure = next(f for f in result.hard_failures if f["code"] == "employer_excluded")
        self.assertEqual(failure["detail"], "employer is on your exclusion list")
        self.assertIsNone(excluded_employer(job("x", "y", employer="Excludedco Partners"), EXCLUDING))  # whole words only

    def test_alert_and_inbox_leads_from_excluded_employers_are_suppressed(self):
        store = open_store(":memory:", FixedClock(), EXCLUDING.fingerprint)
        ctx = Context(store, EXCLUDING, PROFILE, JOB_MATRIX, None)
        alert = AlertEmail("s", "t", None, [Link("https://www.linkedin.com/jobs/view/4000000001", "linkedin", "Claims Analyst",
                                                 "Excluded Co · Remote", True),
                                            Link("https://www.linkedin.com/jobs/view/4000000002", "linkedin", "Claims Analyst",
                                                 "Other Health · Remote", True)])
        outcomes = []
        counts = _record_alert(ctx, alert, None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["suppressed", "lead"])
        self.assertEqual(counts["lead_suppressed"], 1)
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads")["c"], 1)
        self.assertEqual(inbox.OUTCOME_TEXT["suppressed"], "skipped")  # the reply on GitHub never says why


class YearsRuleTests(unittest.TestCase):
    def test_more_than_four_years_short_on_a_required_number_rejects(self):
        result = gate_for("Business Systems Analyst", "Requirements:\n- 10+ years of SQL experience required\n- Excel skills required\nThis role is remote.")
        self.assertIn("years_far_short", {f["code"] for f in result.hard_failures})

    def test_two_to_four_years_short_is_a_major_penalty_not_a_rejection(self):
        result = gate_for("Business Systems Analyst", "Requirements:\n- 7+ years of SQL experience required\n- Excel skills required\nThis role is remote.")
        self.assertEqual(result.decision, "eligible")
        self.assertIn("years_gap", {f["code"] for f in result.flags})

    def test_an_or_list_is_met_by_your_strongest_alternative(self):
        from careeros.qualify.gate import best_years
        self.assertEqual(best_years(PROFILE, "consulting, healthcare or pharmacy"), 5)  # healthcare domain: 5 years
        self.assertIsNone(best_years(PROFILE, "astrophysics or marine biology"))
        result = gate_for("Business Systems Analyst",
                          "Requirements:\n- 9+ years of experience in consulting, healthcare or pharmacy required\n"
                          "- Excel skills required\nThis role is remote.")
        self.assertNotIn("years_far_short", {f["code"] for f in result.hard_failures})  # 4 short via healthcare, not 9

    def test_a_line_is_judged_on_its_best_record_whatever_order_the_records_arrive_in(self):
        from careeros.extract.model import Requirement
        line = "9+ years of PBM or healthcare experience required"
        reqs = [Requirement("domain", "pbm", "hard_required", line, 0.9, 9.0),          # you have 3: 6 short alone
                Requirement("domain", "healthcare", "hard_required", line, 0.9, 9.0),   # you have 5: 4 short
                Requirement("skill", "excel", "hard_required", "Excel skills required", 0.9, None)]
        result = evaluate(job("Business Systems Analyst", "Requirements:\n- " + line + "\nThis role is remote."), reqs, PROFILE, POLICY)
        self.assertNotIn("years_far_short", {f["code"] for f in result.hard_failures})
        self.assertIn("years_gap", {f["code"] for f in result.flags})

    def test_country_named_in_the_title(self):
        from careeros.normalize.posting import title_country
        self.assertEqual(title_country("(Canada) Software Implementation Consultant - Clinical"), "non_US")
        self.assertEqual(title_country("Claims Analyst - UK"), "non_US")
        self.assertIsNone(title_country("(US) Software Implementation Consultant - Clinical"))
        self.assertIsNone(title_country("Claims Analyst (US/Canada)"))
        self.assertIsNone(title_country("Claims Analyst"))

    def test_preferred_years_only_lower_the_score(self):
        body = ("Requirements:\n- Excel skills required\n- SQL skills required\nPreferred Qualifications:\n"
                "- 12+ years of SQL experience preferred\nThis role is remote.")
        result = gate_for("Business Systems Analyst", body)
        self.assertEqual(result.decision, "eligible")
        self.assertIn("years_preferred_short", {f["code"] for f in result.flags})


class DomainLevelTests(unittest.TestCase):
    """Prompts v2.2: domain relevance is a Career Value component, not a bonus on top of the priority."""

    def test_levels(self):
        self.assertEqual(domain_level(job("Analyst", "Our PBM team works in RxClaim."), POLICY)["level"], "direct")
        self.assertEqual(domain_level(job("Analyst", "We are a Medicare Advantage health plan."), POLICY)["level"], "payer")
        self.assertEqual(domain_level(job("Analyst", "A digital health company."), POLICY)["level"], "healthcare")
        self.assertEqual(domain_level(job("Analyst", "A logistics company."), POLICY)["level"], "none")
        both = job("Analyst", "A PBM serving health plans, Medicare members and their patients in healthcare.")
        self.assertEqual(domain_level(both, POLICY)["level"], "direct")  # the strongest level wins

    def test_domain_counts_once_inside_career_value(self):
        pbm = career_value(job("Claims Analyst", "Our PBM team works in RxClaim."), PROFILE, POLICY)
        other = career_value(job("Claims Analyst", "A logistics company."), PROFILE, POLICY)
        self.assertEqual(pbm["dims"]["domain_relevance"]["value"], 1.0)
        self.assertGreater(pbm["score"], other["score"])
        self.assertNotIn("strategic_value", pbm["dims"])  # the old industry credit is replaced, not stacked
        j = job("Pharmacy Benefits Systems Analyst", postings.GOOD_FIT)
        reqs = extract(j["description_text"], POLICY).requirements
        card = score(j, reqs, PROFILE, evaluate(j, reqs, PROFILE, POLICY), POLICY, "2026-10-10T12:00:00.000Z")
        w = POLICY.get("weights", "priority")
        self.assertEqual(card.apply_priority, round(w["candidate_fit"] * card.candidate_fit + w["career_value"] * card.career_value
                                                    + w["freshness"] * card.freshness, 1))  # no extra bonus term


@mock.patch.object(inbox, "gh_path", lambda: "gh")
class NightInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), min_interval=0, sleep=lambda s: None)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def ctx(self, policy):
        store = open_store(":memory:", FixedClock("2026-10-12T12:00:00.000Z"), policy.fingerprint)
        return Context(store, policy, PROFILE, JOB_MATRIX, Path(self.tmp.name))

    def night_issue(self):
        found = issue(number=9, body="Claims Quality Auditor | Night Co | Remote (US) | https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7c | overnight\n")
        found["title"] = "NIGHT Job recommendations 2026-10-12"
        return found

    def test_night_issues_wait_untouched_while_night_is_off(self):
        ctx, gh = self.ctx(POLICY), FakeGh([self.night_issue()])
        counts = _inbox_pull(ctx, self.client, "owner-x/example-inbox", runner=gh)
        self.assertEqual(counts["inbox_night_waiting"], 1)
        self.assertEqual([c[:2] for c in gh.calls], [("issue", "list")])  # no comment, no close
        self.assertIsNone(ctx.store.one("SELECT 1 FROM inbox_issues"))
        self.assertIsNone(ctx.store.one("SELECT 1 FROM leads"))

    def test_night_issues_become_night_leads_once_night_is_on(self):
        ctx, gh = self.ctx(NIGHT_ON), FakeGh([self.night_issue()])
        _inbox_pull(ctx, self.client, "owner-x/example-inbox", runner=gh)
        lead = ctx.store.one("SELECT profile, family FROM leads")
        self.assertEqual((lead["profile"], lead["family"]), ("night", "quality_audit"))
        self.assertIn(("issue", "close", None), gh.calls)


if __name__ == "__main__":
    unittest.main()
