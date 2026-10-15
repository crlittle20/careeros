"""AI requirement reading with a fake Anthropic client: no network, no key, synthetic data only."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from careeros.core.clock import FixedClock
from careeros.core.fingerprint import fingerprint
from careeros.core.policy import PolicyBundle, load_policy
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import HttpClient
from careeros.extract import rules
from careeros.extract.ai import FALLBACK_BETA, AIReader, AIUnavailable, merge
from careeros.paths import CONFIG_DIR
from careeros.pipeline import Context, ingest, process
from careeros.qualify.profile import load_profile

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import Board, gh_job

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")

GOOD_RECORDS = [
    {"category": "skill", "name": "sql", "requirement_type": "hard_required", "years": 3,
     "source_text": "3+ years of SQL experience required", "confidence": 0.95},
    {"category": "skill", "name": "rxclaim", "requirement_type": "hard_required", "years": None,
     "source_text": "Experience with RxClaim or another PBM adjudication platform", "confidence": 0.9},
    {"category": "education", "name": "bachelor", "requirement_type": "required_with_equivalency", "years": None,
     "source_text": "Bachelor's degree or equivalent work experience", "confidence": 0.9},
    {"category": "skill", "name": "power bi", "requirement_type": "preferred", "years": None,
     "source_text": "Power BI experience is a plus", "confidence": 0.9},
]


def response(records=None, stop_reason="end_turn"):
    text = json.dumps({"requirements": records if records is not None else GOOD_RECORDS})
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)], model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0), _request_id="req_test")


class FakeClient:
    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class ReaderTests(unittest.TestCase):
    def test_request_shape_and_only_public_posting_is_sent(self):
        client = FakeClient(response())
        AIReader(client, "claude-opus-5-5").read("Business Systems Analyst", postings.GOOD_FIT)
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual((call["betas"], call["fallbacks"]), ([FALLBACK_BETA], "default"))
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(call["output_config"]["effort"], "medium")
        self.assertEqual(call["messages"], [{"role": "user", "content":
                         f"Job title: Business Systems Analyst\n\nPosting text:\n<posting>\n{postings.GOOD_FIT}\n</posting>"}])

    def test_unquoted_records_dropped_and_values_sanitized(self):
        rules_ex = rules.extract(postings.GOOD_FIT, POLICY)
        raw = GOOD_RECORDS + [
            {"category": "certification", "name": "pmp", "requirement_type": "hard_required", "years": None,
             "source_text": "PMP certification required", "confidence": 0.9},          # not in the posting: invented
            {"category": "planet", "name": "x", "requirement_type": "hard_required", "years": None,
             "source_text": "Write SQL to validate data", "confidence": 0.9},          # invalid category
            {"category": "skill", "name": "sql", "requirement_type": "responsibility", "years": 99,
             "source_text": "Write SQL to validate data", "confidence": 7},            # out-of-range values clamped
        ]
        merged = merge(raw, postings.GOOD_FIT, rules_ex, "ai:test")
        names = {(r.category, r.name) for r in merged.requirements}
        self.assertNotIn(("certification", "pmp"), names)
        self.assertIn("ai_dropped_unquoted:2", merged.notes)
        clamped = next(r for r in merged.requirements if r.requirement_type == "responsibility")
        self.assertEqual((clamped.years, clamped.confidence), (None, 1.0))

    def test_rules_cross_check_and_gate_critical_records_kept(self):
        rules_ex = rules.extract("Requirements:\n- SQL experience required\n- Bachelor's degree required", POLICY)
        raw = [{"category": "skill", "name": "sql", "requirement_type": "preferred", "years": None,
                "source_text": "SQL experience required", "confidence": 0.9}]
        merged = merge(raw, "Requirements:\n- SQL experience required\n- Bachelor's degree required", rules_ex, "ai:test")
        found = {(r.category, r.name): r.requirement_type for r in merged.requirements}
        self.assertEqual(found[("skill", "sql")], "ambiguous")          # never silently downgraded to preferred
        self.assertEqual(found[("education", "bachelor")], "hard_required")  # AI omitted it; rules record kept

    def test_refusal_truncation_and_bad_json_raise(self):
        for bad in (response(stop_reason="refusal"), response(stop_reason="max_tokens"),
                    SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")])):
            with self.assertRaises(AIUnavailable):
                AIReader(FakeClient(bad), "m").read("t", "x")


class PipelineAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-10-10T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                         "ats": "greenhouse", "board_token": "synth", "industry": "health insurance", "status": "verified"}
        self.store.writer("admin").insert("employers", self.employer)
        board = Board([gh_job(201, "Business Systems Analyst", postings.GOOD_FIT, req="A1"),
                       gh_job(202, "Senior Business Systems Analyst", postings.GOOD_FIT.replace("5+ years", "4+ years"), req="A2")])
        self.http = HttpClient(transport=httpx.MockTransport(board.handler), min_interval=0, sleep=lambda s: None)
        self.postings = fetch_board(self.http, "greenhouse", "synth")

    def tearDown(self):
        self.http.close()
        self.tmp.cleanup()

    def context(self, client, policy=POLICY):
        return Context(self.store, policy, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(self.tmp.name),
                       AIReader(client, "claude-opus-5-5"))

    def extractor(self, job_id):
        return self.store.one("SELECT r.extractor FROM requirements r JOIN jobs j ON j.job_id = r.job_id "
                              "AND r.extraction_fingerprint = j.extraction_fingerprint WHERE j.job_id = ? LIMIT 1", (job_id,))["extractor"]

    def test_failure_falls_back_to_rules_then_retries_with_ai(self):
        failing = FakeClient(AIUnavailable("refusal"))
        ctx = self.context(failing)
        ingest(ctx, self.employer, self.postings)
        process(ctx)
        self.assertEqual(self.extractor("greenhouse:synth:201"), rules.EXTRACTOR_ID)
        self.assertGreaterEqual(self.store.one("SELECT COUNT(*) c FROM exceptions WHERE error_code LIKE 'ai_%'")["c"], 1)

        self.clock.advance(minutes=5)
        ctx.ai = AIReader(FakeClient(response()), "claude-opus-5-5")
        process(ctx)  # the stale rules-only extraction is detected and re-read by AI
        self.assertTrue(self.extractor("greenhouse:synth:201").startswith("ai:"))
        self.assertEqual(self.store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:synth:201'")["current_state"], "scored")
        self.assertEqual(self.store.verify_event_chain(), [])

    def test_daily_cap_limits_calls(self):
        data = json.loads(json.dumps(POLICY.data))
        data["extraction"]["ai"]["max_calls_per_day"] = 1
        capped = PolicyBundle(data, fingerprint(data))
        client = FakeClient(response())
        ctx = self.context(client, capped)
        ingest(ctx, self.employer, self.postings)
        process(ctx)
        self.assertEqual(len(client.calls), 1)
        extractors = {self.extractor(j) for j in ("greenhouse:synth:201", "greenhouse:synth:202")}
        self.assertEqual(len(extractors), 2)  # one AI-read, one rules-read (retried by AI on a later day)


if __name__ == "__main__":
    unittest.main()
