"""Prompts v2.2 NIGHT: Supplemental vs Replacement modes, states, COI flag, per-mode pay and scoring, separate
leaderboards and labels. Also the title-country pipeline check. Synthetic data only."""
import tempfile
import unittest
from pathlib import Path

import httpx

from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import HttpClient
from careeros.extract.rules import extract
from careeros.pipeline import Context, ingest, night_process, process
from careeros.qualify import night
from careeros.review.labels import agreement, record_label
from careeros.review.report import build_report

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_label_findings import PROFILE
from tests.test_pipeline import Board, gh_job
from tests.test_qualify import POLICY, job
from tests.test_v21_policy import NIGHT_ON, OVERNIGHT, policy_with

PAID = policy_with(night__enabled=True,
                   night__modes__supplemental__compensation={"floor_hourly": 20, "target_hourly": 24},
                   night__modes__replacement__compensation={"floor_annual": 50000, "target_annual": 60000})
EASY_TIERS = policy_with(night__enabled=True, night__tiers={"A": 10, "B": 5, "conditional": 5, "near_miss": 1})
QUIET = OVERNIGHT.replace("health plan operations team", "logistics company").replace("pharmacy claims", "shipping claims")


def assess(title, body, policy=NIGHT_ON, **kw):
    j = job(title, body, **kw)
    return night.assess(j, extract(body, policy).requirements, PROFILE, policy, 60.0)


def codes(out, key="flags"):
    return {f["code"] for f in out[key]}


class ScheduleReadingTests(unittest.TestCase):
    def test_only_schedule_phrases_count(self):
        read = lambda text: night.schedule_of(text, NIGHT_ON)[0]
        self.assertEqual(read("Changing healthcare doesn't happen overnight. We learn from challenges."), "unknown")
        self.assertEqual(read("Facilitate live and asynchronous training sessions."), "unknown")
        self.assertEqual(read("Great culture. No nights or weekends required."), "unknown")
        self.assertEqual(read("Schedule: overnight shift, 11pm to 7am"), "overnight")
        self.assertEqual(read("Hours: 11pm to 7:30am Sunday to Thursday"), "overnight")
        self.assertEqual(read("This is a second shift role"), "evening")
        self.assertEqual(read("Weekend shifts, Saturday and Sunday"), "weekend")
        self.assertEqual(read("Fully flexible schedule"), "flexible")
        self.assertEqual(read("No night shifts or weekend shifts required."), "unknown")  # a negated list
        self.assertEqual(read("You will not be asked to work an overnight shift."), "unknown")
        self.assertEqual(read("No prior experience needed. Night shift role, 4 nights a week."), "overnight")  # new sentence
        self.assertEqual(read("Hours: 8:00 am - 5:00 pm, Monday to Friday"), "daytime")


class ExpandedKeywordTests(unittest.TestCase):
    def read(self, text, title=""):
        return night.schedule_of(text, NIGHT_ON, title=title)[0]

    def test_bare_words_count_in_the_title(self):
        self.assertEqual(self.read("Process claims accurately.", "Overnight Claims Processor"), "overnight")
        self.assertEqual(self.read("Key data accurately.", "3rd Shift Data Entry Specialist"), "overnight")
        self.assertEqual(self.read("Key data accurately.", "Third-Shift Data Entry Specialist"), "overnight")
        self.assertEqual(self.read("Help members.", "Night Auditor"), "overnight")
        self.assertEqual(self.read("Help members.", "After-Hours Support Specialist"), "evening")
        self.assertEqual(self.read("Help members.", "Afterhours Billing Specialist"), "evening")
        self.assertEqual(self.read("Review content.", "Weekend Content Moderator"), "weekend")
        self.assertEqual(self.read("Help members.", "Nightingale Claims Analyst"), "unknown")  # a word boundary, not 'night'

    def test_phrases_and_stated_hours_in_the_posting(self):
        self.assertEqual(self.read("Join our night crew reviewing claims."), "overnight")
        self.assertEqual(self.read("This is a permanent nights position."), "overnight")
        self.assertEqual(self.read("Graveyard shift available."), "overnight")
        self.assertEqual(self.read("Hours: 10:00 PM \u2013 6:30 AM, Sunday to Thursday"), "overnight")
        self.assertEqual(self.read("Shift: 11p-7a"), "overnight")
        self.assertEqual(self.read("Shift: 2300-0730"), "overnight")
        self.assertEqual(self.read("Schedule: 3:00 pm to 11:30 pm"), "evening")
        self.assertEqual(self.read("We cover after-hours support for our clients."), "evening")
        self.assertEqual(self.read("Baylor shift: Saturday and Sunday, 12 hours"), "weekend")
        self.assertEqual(self.read("Schedule: 8:00 AM - 5:00 PM Central"), "daytime")
        self.assertEqual(self.read("Worked here 2014-2018 and 2019-2023."), "unknown")  # years are not hours
        # the role's own night hours outrank a mention of the day shift it hands off to
        self.assertEqual(self.read("Schedule: 11p-7a. The day shift team picks up your queue each morning."), "overnight")

    def test_business_or_training_hours_are_not_proof_of_a_day_job(self):
        self.assertEqual(self.read("Handle tickets during provider and client operational hours (9AM-5PM PST)."), "daytime_likely")
        self.assertEqual(self.read("Standard hours for training: Monday thru Friday, 9:00am - 5:30pm, Eastern"), "daytime_likely")
        self.assertEqual(self.read("Patient Support Business Hours are 6a-6p PST Monday-Friday."), "daytime_likely")
        self.assertEqual(self.read("Your schedule: Monday-Friday 8:00 AM - 5:00 PM."), "daytime")
        likely = assess("Billing Specialist", QUIET.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am",
                                                            "business hours 8:00 AM - 5:00 PM"), employment="part time")
        self.assertEqual((likely["decision"], likely["state"]), ("eligible", "CONDITIONAL"))
        self.assertIn("daytime_hours_likely", codes(likely))

    def test_incidental_extra_hours_are_not_night_work(self):
        self.assertEqual(self.read("Occasional after-hours work may be required."), "unknown")
        self.assertEqual(self.read("Provide after-hours support as needed."), "unknown")
        self.assertEqual(self.read("Monday-Friday 8 AM - 5 PM; occasional after-hours support."), "daytime")

    def test_new_night_friendly_families(self):
        self.assertEqual(night.night_family("content moderator", NIGHT_ON), "content_review")
        self.assertEqual(night.night_family("noc analyst", NIGHT_ON), "workforce_monitoring")
        self.assertEqual(night.night_family("night auditor", NIGHT_ON), "quality_audit")


class ModeTests(unittest.TestCase):
    def test_mode_rule(self):
        self.assertEqual(night.mode_for("overnight", "full_time", NIGHT_ON), "replacement")
        self.assertEqual(night.mode_for("overnight", "contract", NIGHT_ON), "replacement")
        self.assertEqual(night.mode_for("overnight", "part_time", NIGHT_ON), "supplemental")
        self.assertEqual(night.mode_for("evening", "full_time", NIGHT_ON), "supplemental")
        self.assertEqual(night.mode_for("unknown", "full_time", NIGHT_ON), "supplemental")

    def test_coi_review_only_for_supplemental_and_it_makes_the_job_conditional(self):
        # 2026-10-12 split: same industry alone is informational; a concrete reason (an employer you worked for) is not
        part_time = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT, employment="part time")
        self.assertEqual((part_time["mode"], part_time["state"]), ("supplemental", "ELIGIBLE"))
        self.assertIn("night_industry_overlap", codes(part_time))  # pharmacy claims at a health plan: same industry
        self.assertNotIn("night_coi_review", codes(part_time))
        self.assertEqual((part_time["coi"]["industry_overlap"], part_time["coi"]["manual_coi_review_required"]), (True, False))
        familiar = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT, employment="part time", employer="Example Health")
        self.assertEqual(familiar["state"], "CONDITIONAL")
        self.assertIn("night_coi_review", codes(familiar))
        full_time = assess("Claims Quality Auditor", OVERNIGHT, employment="full time", employer="Example Health")
        self.assertEqual(full_time["mode"], "replacement")
        self.assertNotIn("night_coi_review", codes(full_time))
        self.assertIsNone(full_time["coi"])
        quiet = assess("Claims Quality Auditor (Part-Time)", QUIET, employment="part time")
        self.assertNotIn("night_coi_review", codes(quiet))
        self.assertNotIn("night_industry_overlap", codes(quiet))

    def test_unknown_hours_are_conditional_but_an_unset_pay_floor_is_not(self):
        unknown = assess("Claims Quality Auditor (Part-Time)", QUIET.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am", "varies"),
                         employment="part time")
        self.assertEqual(unknown["state"], "CONDITIONAL")
        self.assertIn("schedule_unknown", codes(unknown))
        quiet = assess("Claims Quality Auditor (Part-Time)", QUIET, employment="part time")
        self.assertIn("night_pay_unconfigured", codes(quiet))
        self.assertEqual(quiet["state"], "ELIGIBLE")

    def test_hard_rules(self):
        self.assertIn("daytime_schedule", codes(assess("Claims Quality Auditor", QUIET.replace(
            "overnight shift, Sunday to Thursday, 11pm to 7:30am", "day shift, standard business hours")), "failures"))
        self.assertIn("night_manager_role", codes(assess("Claims Quality Manager", QUIET), "failures"))
        self.assertEqual(assess("Claims Quality Supervisor", QUIET)["decision"], "eligible")
        self.assertIn("no_night_family", codes(assess("Software Developer", QUIET), "failures"))


class PayTests(unittest.TestCase):
    def test_each_mode_uses_its_own_floor_and_aggregator_pay_never_rejects(self):
        paid = QUIET + "\nPay: $15 - $17 per hour"
        supplemental = assess("Claims Quality Auditor (Part-Time)", paid, policy=PAID, employment="part time")
        self.assertEqual(supplemental["mode"], "supplemental")
        self.assertIn("below_night_pay_floor", codes(supplemental, "failures"))
        replacement = assess("Claims Quality Auditor", paid, policy=PAID, employment="full time")
        self.assertEqual(replacement["mode"], "replacement")
        self.assertIn("below_night_pay_floor", codes(replacement, "failures"))  # $34k/yr vs the $50k Replacement floor
        j = dict(job("Claims Quality Auditor (Part-Time)", paid, employment="part time"), url="https://www.indeed.com/viewjob?jk=1")
        aggregator = night.assess(j, extract(paid, PAID).requirements, PROFILE, PAID, 60.0)
        self.assertNotIn("below_night_pay_floor", codes(aggregator, "failures"))
        self.assertIn("pay_untrusted_below_floor", codes(aggregator))


class ScoringTests(unittest.TestCase):
    def test_mode_weights_and_state_gated_tiers(self):
        out = assess("Claims Quality Auditor", QUIET, policy=EASY_TIERS, employment="full time")
        weights = EASY_TIERS.get("night", "modes", "replacement", "weights")
        expected = round(100 * sum(out["dims"][k] * w for k, w in weights.items()) / sum(weights.values()), 1)
        self.assertAlmostEqual(out["priority"], expected, delta=0.2)
        self.assertEqual((out["mode"], out["state"], out["tier"]), ("replacement", "ELIGIBLE", "A"))
        conditional = assess("Claims Quality Auditor (Part-Time)", QUIET.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am",
                                                                                 "varies"), policy=EASY_TIERS, employment="part time")
        self.assertEqual((conditional["state"], conditional["tier"]), ("CONDITIONAL", "near_miss"))  # never A/B while unknowns remain


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-10-12T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, EASY_TIERS.fingerprint)
        self.ctx = Context(self.store, EASY_TIERS, PROFILE, JOB_MATRIX, Path(self.tmp.name))
        self.employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                         "ats": "greenhouse", "board_token": "synth", "industry": "health", "status": "verified"}
        self.store.writer("admin").insert("employers", self.employer)
        self.ingest([gh_job(501, "Claims Quality Auditor", QUIET, req="N-1"),
                     gh_job(502, "Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="P-1")])

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, jobs, **kw):
        client = HttpClient(transport=httpx.MockTransport(Board(jobs).handler), min_interval=0, sleep=lambda s: None)
        ingest(self.ctx, self.employer, fetch_board(client, "greenhouse", "synth"), **kw)
        client.close()

    def test_results_are_separate_idempotent_and_off_by_default(self):
        process(self.ctx)
        row = self.store.one("SELECT mode, state, tier FROM night_results WHERE job_id = 'greenhouse:synth:501'")
        self.assertEqual(row["mode"], "supplemental")  # hours unknown to the board (employment unknown) -> supplemental
        self.assertEqual(sum(night_process(self.ctx).values()), 0)
        self.assertEqual(sum(night_process(Context(self.store, POLICY, PROFILE, JOB_MATRIX, None)).values()), 0)
        self.assertIsNone(self.store.one("SELECT 1 FROM night_assessments"))  # the superseded table is never written

    def test_two_leaderboards_with_night_labels_measured_separately(self):
        process(self.ctx)
        page = build_report(self.store, PROFILE, EASY_TIERS, token="t", form_keys={})
        self.assertIn('id="night-supplemental"', page)
        self.assertIn('id="night-replacement"', page)
        night_part = page[page.index('id="night"'):page.index('Needs review')]
        self.assertIn('action="/label_night', night_part)
        self.assertNotIn('action="/label?', night_part)
        self.assertIn("Skip: schedule doesn't fit", night_part)
        record_label(self.store, "greenhouse:synth:501", "skip", "schedule doesn't fit", key="n1", profile="night")
        self.assertEqual(agreement(self.store)["labelled"], 0)  # NIGHT labels never count toward PRIMARY
        night_stats = agreement(self.store, "night")
        self.assertEqual(night_stats["labelled"], 1)
        self.assertIn("supplemental", night_stats["by_mode"])
        tier = self.store.one("SELECT tier FROM night_results WHERE job_id = 'greenhouse:synth:501' ORDER BY at DESC LIMIT 1")["tier"]
        if tier in ("A", "B"):
            self.assertEqual(night_stats["schedule_fit_false_positive_rate"], 1.0)

    def test_night_only_jobs_stay_out_of_primary_rejection_lists(self):
        # a part-time role that reached CareerOS only as a NIGHT recommendation: PRIMARY rejects it (part-time), but
        # that is not a PRIMARY decision worth auditing or labelling
        self.ingest([gh_job(503, "Claims Specialist (Part-Time)", postings.GOOD_FIT, req="N-3")], source="manual_url",
                    full_board=False)
        self.store.writer("discovery").insert("leads", {
            "lead_id": "lead:n3", "url": "https://job-boards.greenhouse.io/synth/jobs/503", "site": "greenhouse",
            "title": "Claims Specialist (Part-Time)", "title_norm": "claims specialist part time", "first_seen_at": self.clock.now(),
            "status": "ingested", "job_id": "greenhouse:synth:503", "profile": "night"})
        process(self.ctx)
        self.assertEqual(self.store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:synth:503'")["current_state"], "rejected")
        page = build_report(self.store, PROFILE, POLICY, token="t", form_keys={})
        rejected = page[page.index("<h2>Rejected"):]
        self.assertNotIn("Claims Specialist (Part-Time)", rejected)

    def test_a_country_in_the_title_rejects_new_and_stored_postings(self):
        self.ingest([gh_job(504, "(Canada) Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="C-1",
                            location="Remote")], full_board=False)
        process(self.ctx)
        query = "SELECT j.country, t.reason_code FROM jobs j JOIN transitions t ON t.workflow_id = j.workflow_id " \
                "AND t.sequence = j.state_sequence WHERE j.job_id = 'greenhouse:synth:504'"
        self.assertEqual(tuple(self.store.one(query)), ("non_US", "outside_us"))
        self.store.writer("discovery").update("jobs", {"country": "US"}, "job_id = ?", ("greenhouse:synth:504",))  # older code
        self.clock.advance(minutes=5)
        self.assertGreaterEqual(process(self.ctx)["refreshed"], 1)
        self.assertEqual(tuple(self.store.one(query)), ("non_US", "outside_us"))


if __name__ == "__main__":
    unittest.main()
