"""Regressions for defects found by the user's first labels (2026-10-11). Synthetic text modeled on the live wording."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from careeros.core.clock import FixedClock
from careeros.core.policy import load_policy
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import HttpClient
from careeros.extract.rules import extract
from careeros.normalize.posting import norm, remote_state_rules, seniority
from careeros.paths import CONFIG_DIR
from careeros.pipeline import Context, ingest, process
from careeros.qualify import gate as gate_mod
from careeros.qualify.gate import evaluate
from careeros.qualify.profile import load_profile

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import Board, gh_job
from tests.test_qualify import job

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PROFILE = load_profile(FIXTURES / "synthetic_profile.yaml")
EXCLUSION = "Pay depends on experience. This role is not eligible for hire in: AK, CA, CO, HI, NY, or WA.  Interview Process: ..."


class StateRuleTests(unittest.TestCase):
    def test_exclusion_list_is_not_an_allowed_list(self):
        allowed, excluded = remote_state_rules("United States", EXCLUSION, "remote")
        self.assertEqual((allowed, excluded), ([], ["AK", "CA", "CO", "HI", "NY", "WA"]))

    def test_office_alternative_is_not_a_remote_limit(self):
        self.assertEqual(remote_state_rules("New York, NY or Remote", "", "remote"), ([], []))
        self.assertEqual(remote_state_rules("Remote - TX, FL", "", "remote"), (["FL", "TX"], []))
        self.assertEqual(remote_state_rules("Remote - US", "", "remote"), ([], []))

    def test_gate_uses_exclusions_correctly(self):
        reqs = extract(postings.GOOD_FIT.split("This role is remote")[0], POLICY).requirements
        resident = job("Business Systems Analyst", postings.GOOD_FIT.split("This role is remote")[0] + "\n" + EXCLUSION, location="United States")
        resident["remote_type"], resident["remote_states"], resident["remote_excluded_states"] = "remote", [], ["AK", "CA", "NY"]
        self.assertEqual(evaluate(resident, reqs, PROFILE, POLICY).decision, "eligible")
        resident["remote_excluded_states"] = ["FL", "CA"]
        self.assertIn("state_excluded", {f["code"] for f in evaluate(resident, reqs, PROFILE, POLICY).hard_failures})


class SeniorityAndYearsTests(unittest.TestCase):
    def test_individual_contributor_manager_titles(self):
        terms = POLICY.get("seniority_terms")
        self.assertEqual(seniority(norm("Senior Product Manager, PBM Platform"), terms), "senior")
        self.assertEqual(seniority(norm("Project Manager"), terms), "unspecified")
        self.assertEqual(seniority(norm("Manager, Claims Operations"), terms), "manager")

    def test_years_shortfall_is_a_flag_not_a_rejection(self):
        reqs = extract("Requirements:\n- 4+ years of data validation required\n- Bachelor's degree or equivalent", POLICY).requirements
        result = evaluate(job("Data Operations Analyst", "x"), reqs, PROFILE, POLICY)
        self.assertNotIn("years_gap", {f["code"] for f in result.hard_failures})


class ReevaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, PROFILE, JOB_MATRIX, Path(self.tmp.name))
        self.employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                         "ats": "greenhouse", "board_token": "synth", "industry": "health", "status": "verified"}
        self.store.writer("admin").insert("employers", self.employer)
        body = postings.GOOD_FIT.split("This role is remote")[0] + "\n" + EXCLUSION
        board = Board([gh_job(301, "Business Systems Analyst", body, req="X1", location="United States")])
        self.http = HttpClient(transport=httpx.MockTransport(board.handler), min_interval=0, sleep=lambda s: None)
        ingest(self.ctx, self.employer, fetch_board(self.http, "greenhouse", "synth"))

    def tearDown(self):
        self.http.close()
        self.tmp.cleanup()

    def test_stale_stored_states_are_refreshed_and_job_reevaluated(self):
        # Simulate a job normalized by the old code, which stored the exclusion list as the allowed list.
        self.store.conn.execute("UPDATE jobs SET remote_states_json = ?, remote_excluded_states_json = '[]', remote_type = 'remote'",
                                (json.dumps(["AK", "CA", "CO", "HI", "NY", "WA"]),))
        process(self.ctx)
        row = self.store.one("SELECT current_state, remote_states_json, remote_excluded_states_json FROM jobs")
        self.assertEqual(row["remote_states_json"], "[]")
        self.assertEqual(row["current_state"], "scored")
        self.assertEqual(self.store.verify_event_chain(), [])

    def test_rules_version_change_reevaluates_existing_decisions(self):
        process(self.ctx)
        before = self.store.one("SELECT COUNT(*) c FROM gate_results")["c"]
        original = gate_mod.RULES_VERSION
        gate_mod.RULES_VERSION = original + "-test"
        try:
            self.clock.advance(minutes=1)
            counts = process(self.ctx)
        finally:
            gate_mod.RULES_VERSION = original
        self.assertGreaterEqual(counts["reevaluated"], 1)
        self.assertGreater(self.store.one("SELECT COUNT(*) c FROM gate_results")["c"], before)


if __name__ == "__main__":
    unittest.main()
