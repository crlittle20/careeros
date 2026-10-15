"""The whole job lifecycle (2026-10-13): SOURCE -> INGEST -> NORMALIZE -> IDENTIFY EMPLOYER -> VERIFY -> DEDUPLICATE ->
ELIGIBILITY -> FIT -> CAREER VALUE -> ROUTE -> ACTION -> STATUS -> OUTCOME -> ANALYTICS, on synthetic data only.

Every processed job must be able to explain itself (source, dates, employer, verification, duplicate decision,
eligibility, fit, career value, routing, status, rejection / review reasons, actions), repeat processing must change
nothing, and no job may be left in a state nothing moves it out of.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.db.store import Writer
from careeros.discovery import board_registry as reg
from careeros.discovery import employer_intel
from careeros.discovery.board_discovery import relevant_roles
from careeros.discovery.boards import greenhouse_posting
from careeros.normalize.posting import RawPosting, relevant_role
from careeros.pipeline import Context, close_open_jobs, ingest, process
from careeros.review.applications import application_matrix, record_application, update_application
from careeros.review.discovery_metrics import metrics
from careeros.review.explain import FIELDS, explain_job, render
from careeros.review.funnel import funnel
from careeros.review.labels import record_label

from tests.helpers import JOB_MATRIX
from tests.test_v21_policy import OVERNIGHT, policy_with
from tests.test_v25_audit import APPROVED, MID, store_ctx
from tests.test_v26_discovery import CLAIMS, gh_item

LOW_PAY = CLAIMS.replace("$80,000 - $95,000", "$40,000 - $45,000")
BOARD = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health", "ats": "greenhouse",
         "board_token": "synth", "status": "verified", "admitted_via": "legacy"}


def board_postings():
    items = [gh_item(1, "Claims Analyst", CLAIMS, "synth", req="R-1"),                                      # eligible, scored
             gh_item(2, "Claims Analyst", CLAIMS, "synth", req="R-1"),                                      # duplicate of 1
             gh_item(3, "Claims Analyst", CLAIMS, "synth", req="R-1", location="Remote - Florida"),           # variant of 1
             gh_item(4, "Senior Software Engineer", CLAIMS, "synth", req="R-4"),                            # title screen
             gh_item(5, "Claims Operations Specialist", "Claims work. This role is remote.", "synth", req="R-5"),  # too short
             gh_item(6, "Claims Quality Auditor", OVERNIGHT, "synth", req="R-6"),                           # overnight work
             gh_item(7, "Claims Operations Specialist", LOW_PAY, "synth", req="R-7")]                       # below the pay floor
    return [greenhouse_posting("synth", item) for item in items]


def world(policy=APPROVED):
    store, ctx = store_ctx(policy)
    store.writer("admin").insert("employers", BOARD)
    ingest(ctx, dict(store.one("SELECT * FROM employers")), board_postings(), full_board=True)
    reg.sync_legacy(store)
    process(ctx)
    return store, ctx


def jid(n):
    return f"greenhouse:synth:{n}"


def counts(store):
    tables = ("transitions", "gate_results", "scores", "night_results", "observations", "requirements", "events", "job_changes")
    return {t: store.one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in tables} | {
        "states": {r["job_id"]: r["current_state"] for r in store.all("SELECT job_id, current_state FROM jobs")}}


class WorkflowTests(unittest.TestCase):
    def test_every_job_flows_from_source_to_analytics_and_explains_itself(self):
        store, ctx = world()
        states = {r["job_id"]: r["current_state"] for r in store.all("SELECT job_id, current_state FROM jobs")}
        self.assertEqual(states[jid(1)], "scored")
        self.assertEqual(states[jid(2)], "duplicate")
        self.assertEqual(states[jid(4)], "rejected")
        self.assertEqual(states[jid(5)], "needs_review")
        self.assertEqual(states[jid(7)], "rejected")
        # ACTION -> STATUS -> OUTCOME
        record_label(store, jid(1), "apply", "strong claims fit", "label-1")
        app_id = record_application(store, application_matrix(), jid(1), "direct", key="apply-1")
        update_application(store, application_matrix(), app_id, "screening", key="screen-1")
        # every job, whatever happened to it, answers every question
        for job_id in states:
            e = explain_job(store, ctx.policy, job_id)
            self.assertEqual(tuple(e), FIELDS, job_id)
            for field in FIELDS:
                self.assertNotIn(e[field], (None, "", [], {}), f"{job_id}: {field}")
            self.assertIn("Why", render(e, job_id))
        scored = explain_job(store, ctx.policy, jid(1))
        self.assertEqual((scored["eligibility"]["decision"], scored["source"]["board"]), ("eligible", "greenhouse:synth"))
        self.assertIsNotNone(scored["routing"]["tier"])
        self.assertIsInstance(scored["fit"], dict)
        self.assertIsInstance(scored["career_value"], dict)
        self.assertTrue(scored["verification"]["scanned"])
        self.assertEqual(scored["actions"]["applications"][0]["outcome"], "screening")
        self.assertEqual([h["to"] for h in scored["status"]["history"]][:4], ["normalized", "extracted", "eligible", "scored"])
        self.assertTrue(scored["source"]["sightings"])
        duplicate = explain_job(store, ctx.policy, jid(2))
        self.assertEqual((duplicate["duplicate_decision"]["decision"], duplicate["duplicate_decision"]["of"]), ("duplicate", jid(1)))
        self.assertEqual(duplicate["rejection_reason"]["code"], "duplicate")
        self.assertEqual(explain_job(store, ctx.policy, jid(3))["duplicate_decision"]["variant_of"], jid(1))
        titled = explain_job(store, ctx.policy, jid(4))
        self.assertEqual(titled["eligibility"]["stage"], "title screen")
        self.assertIn(titled["rejection_reason"]["code"], ("title_excluded", "no_target_family"))
        self.assertTrue(str(titled["fit"]).startswith("not applicable"))
        review = explain_job(store, ctx.policy, jid(5))
        self.assertEqual((review["review_reason"]["code"], review["eligibility"]["decision"]), ("posting_text_too_short", "needs_review"))
        self.assertIsInstance(explain_job(store, ctx.policy, jid(6))["routing"]["night"], dict)  # assessed for overnight work
        pay = explain_job(store, ctx.policy, jid(7))
        self.assertEqual((pay["rejection_reason"]["code"], pay["eligibility"]["stage"]), ("below_pay_floor", "eligibility rules"))
        self.assertIn("$40,000 - $45,000", pay["rejection_reason"]["quote"])  # the posting's own pay line
        # ANALYTICS
        self.assertEqual(sum(row.get("applied", 0) for row in funnel(store)), 1)
        self.assertIn("verification", metrics(store, ctx.policy, 30))

    def test_explaining_a_job_changes_nothing(self):
        store, ctx = world()
        before = counts(store)
        for row in store.all("SELECT job_id FROM jobs"):
            explain_job(store, ctx.policy, row["job_id"])
        self.assertEqual(counts(store), before)
        with self.assertRaises(ValueError):
            explain_job(store, ctx.policy, "greenhouse:synth:nope")

    def test_the_explain_command_prints_text_and_json(self):
        store, ctx = world()
        with mock.patch("builtins.print") as printed:
            cli.cmd_explain(ctx, None, SimpleNamespace(job_id=jid(1), json=True))
        self.assertEqual(tuple(json.loads(printed.call_args[0][0])), FIELDS)


class IdempotencyTests(unittest.TestCase):
    def test_seeing_and_processing_the_same_postings_again_changes_nothing(self):
        store, ctx = world()
        before = counts(store)
        process(ctx)  # nothing new: no transition, decision, score or event
        self.assertEqual(counts(store), before)
        store.clock.advance(days=1)
        result = ingest(ctx, dict(store.one("SELECT * FROM employers")), board_postings(), full_board=True)
        process(ctx)
        after = counts(store)
        self.assertEqual(after, before)
        self.assertEqual(result.get("new", 0), 0)
        self.assertGreater(result["unchanged"] + result.get("inactive_seen", 0), 0)
        seen = store.one("SELECT last_seen_at, first_seen_at FROM jobs WHERE job_id = ?", (jid(1),))
        self.assertGreater(seen["last_seen_at"], seen["first_seen_at"])  # only the sighting time moved


class StuckJobTests(unittest.TestCase):
    def test_a_job_parked_for_review_is_rechecked_when_the_rules_change(self):
        store, ctx = world()
        self.assertEqual(store.one("SELECT decision FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1",
                                   (jid(5),))["decision"], "needs_review")
        new_policy = policy_with(compensation={"floor_annual": 74000, "floor_hourly": 36, "target_annual": None, "target_hourly": None},
                                 location__remote_only=True, location__home_state="FL", tiers__near_miss__priority=61, night__enabled=True)
        changed = Context(store, new_policy, MID, JOB_MATRIX, None)  # a policy change (different decision fingerprint)
        store.clock.advance(minutes=5)
        process(changed)
        reasons = [r["reason_code"] for r in store.all("SELECT reason_code FROM transitions WHERE entity_id = ? AND from_state = 'needs_review'",
                                                       (jid(5),))]
        self.assertIn("policy_or_rules_changed", reasons)
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (jid(5),))["current_state"], "needs_review")

    def test_a_job_the_title_screen_now_rejects_is_not_sent_back_on_every_run(self):
        """Functional MVP gate (2026-10-13), found on a copy of live data: a job read earlier, then rejected by the title
        screen after a rules and reading-vocabulary change, kept its old reading fingerprint, so every run sent it back
        and the title screen rejected it again."""
        store, ctx = world()
        data = json.loads(json.dumps(ctx.policy.data))
        data["title_exclude"] = data["title_exclude"] + ["claims analyst"]            # a rules change for job 1's title
        data["skill_aliases"] = {**data["skill_aliases"], "claims processing": ["claims processing"]}  # a new vocabulary
        changed = Context(store, type(ctx.policy)(data=data, fingerprint=json.dumps(data, sort_keys=True)), MID, JOB_MATRIX, None)
        store.clock.advance(minutes=5)
        process(changed)
        self.assertEqual(store.one("SELECT reason_code FROM transitions WHERE entity_id = ? ORDER BY sequence DESC LIMIT 1",
                                   (jid(1),))["reason_code"], "title_excluded")
        before = counts(store)
        store.clock.advance(minutes=5)
        process(changed)
        self.assertEqual(counts(store), before)

    def test_too_few_readable_requirements_is_a_recorded_review_decision(self):
        store, ctx = store_ctx()
        blurb = ("About us\nWe are a health plan operations team that keeps pharmacy claims accurate for members across the "
                 "country. Our people care about quality, teamwork and the members we serve every single day. ") * 3
        ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
               [RawPosting("greenhouse", "synth", "8", "Claims Operations Specialist", "Remote - US", description_text=blurb)],
               full_board=False)
        process(ctx)
        self.assertEqual(store.one("SELECT current_state FROM jobs")["current_state"], "needs_review")
        gate = store.one("SELECT decision, flags_json FROM gate_results ORDER BY at DESC, rowid DESC LIMIT 1")
        self.assertEqual((gate["decision"], json.loads(gate["flags_json"])[0]["code"]), ("needs_review", "requirements_unclear"))
        self.assertEqual(explain_job(store, ctx.policy, jid(8))["review_reason"]["code"], "requirements_unclear")

    def test_a_review_parked_before_review_decisions_were_recorded_is_rechecked_once(self):
        store, ctx = store_ctx()
        ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
               [RawPosting("greenhouse", "synth", "9", "Claims Operations Specialist", "Remote - US", description_text=CLAIMS)], full_board=False)
        store.writer("extract").transition(ctx.matrix, jid(9), "needs_review", reason_code="requirements_unclear")  # old-style: no record
        store.clock.advance(minutes=1)
        process(ctx)
        first = [r["reason_code"] for r in store.all("SELECT reason_code FROM transitions WHERE entity_id = ? AND from_state = 'needs_review'",
                                                     (jid(9),))]
        self.assertEqual(first, ["review_rechecked"])
        store.clock.advance(minutes=1)
        before = counts(store)
        process(ctx)
        self.assertEqual(counts(store), before)  # once, not every run

    def test_no_state_lacks_a_way_forward_and_dead_transitions_are_gone(self):
        allowed = {(a["from"], a["to"]) for a in json.load(open(cli.CONFIG_DIR / "workflows" / "job.json"))["allowed"]}
        for dead in (("discovered", "closed"), ("extracted", "needs_review"), ("needs_review", "extracted"), ("normalized", "duplicate")):
            self.assertNotIn(dead, allowed)
        for state in ("normalized", "extracted", "needs_review", "eligible", "scored", "rejected"):
            self.assertIn((state, "closed"), allowed, state)        # every open state can close
            self.assertTrue({(state, "normalized"), (state, "extracted")} & allowed or state in ("normalized",), state)
        self.assertIn(("closed", "normalized"), allowed)            # a closed posting can reopen
        self.assertFalse(any(frm == "duplicate" for frm, _ in allowed))  # duplicate is terminal


class BoardClosureTests(unittest.TestCase):
    def test_rejecting_a_board_closes_its_open_jobs_and_keeps_history(self):
        store, ctx = world()
        open_before = store.one("SELECT COUNT(*) c FROM jobs WHERE employer_id = 'greenhouse:synth' AND current_state NOT IN ('closed','duplicate')")["c"]
        with mock.patch("builtins.print"):
            cli.cmd_sources(ctx, None, SimpleNamespace(action="reject", target="greenhouse:synth", note="wrong company", value=None,
                                                      domain=None))
        self.assertEqual(store.one("SELECT COUNT(*) c FROM jobs WHERE employer_id = 'greenhouse:synth' AND current_state = 'closed'")["c"],
                         open_before)
        self.assertEqual({r["reason_code"] for r in store.all("SELECT reason_code FROM transitions WHERE to_state = 'closed'")}, {"board_retired"})
        self.assertGreater(store.one("SELECT COUNT(*) c FROM gate_results")["c"], 0)  # decisions kept as history

    def test_a_job_that_cannot_close_is_recorded_never_silent(self):
        store, ctx = world()
        real = Writer.transition

        def flaky(self, matrix, job_id, to_state, **kw):
            if job_id == jid(1):
                raise RuntimeError("simulated")
            return real(self, matrix, job_id, to_state, **kw)
        with mock.patch.object(Writer, "transition", flaky):
            closed, failed = close_open_jobs(ctx, "greenhouse:synth", "board_retired")
        self.assertEqual(failed, 1)
        self.assertGreater(closed, 0)
        self.assertTrue(store.one("SELECT 1 FROM exceptions WHERE error_code = 'board_job_close_failed' AND entity_id = ?", (jid(1),)))
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (jid(1),))["current_state"], "scored")


class RelevanceTests(unittest.TestCase):
    def test_discovery_and_career_fit_share_one_definition_of_a_relevant_role(self):
        policy = APPROVED
        self.assertTrue(relevant_role("claims analyst", "pbm_claims_operations", "remote", "US", policy))
        for title, family, remote, country in (("claims operations manager", "pbm_claims_operations", "remote", "US"),
                                               ("claims analyst", None, "remote", "US"),
                                               ("claims analyst", "pbm_claims_operations", "onsite", "US"),
                                               ("claims analyst", "pbm_claims_operations", "remote", "non_US")):
            self.assertFalse(relevant_role(title, family, remote, country, policy), title)
        postings = [RawPosting("greenhouse", "synth", "1", "Claims Analyst", "Remote - US", description_text=CLAIMS),
                    RawPosting("greenhouse", "synth", "2", "Claims Operations Manager", "Remote - US", description_text=CLAIMS)]
        self.assertEqual(relevant_roles(postings, policy), 1)
        store, ctx = store_ctx()
        store.writer("admin").insert("employers", BOARD)
        ingest(ctx, dict(store.one("SELECT * FROM employers")), postings, full_board=True)
        reg.sync_legacy(store)
        entity_id = store.one("SELECT entity_id FROM employers")["entity_id"]
        self.assertEqual(employer_intel.stats(store, entity_id, policy, store.clock.now())["relevant_total"], 1)


if __name__ == "__main__":
    unittest.main()
