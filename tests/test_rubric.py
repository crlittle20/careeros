"""Shadow scoring rubric v1 and the shadow recommendation policy (SCORING_SHADOW_SPEC 2026-10-16). Pure functions on
synthetic data: no database, no network, no production tier is touched."""
import math
import random
import unittest
from pathlib import Path

from careeros.core.policy import load_policy
from careeros.extract.model import Requirement as R
from careeros.paths import CONFIG_DIR
from careeros.qualify import rubric
from careeros.qualify.profile import Profile, load_profile
from careeros.qualify.rubric import ABSENT, EVIDENCED, UNKNOWN, Assessment, Axis, Item, Section
from careeros.review import shadow_policy as sp

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PROFILE = load_profile(FIXTURES / "synthetic_profile.yaml")
CFG = rubric.load_config()


def job(title="Software Implementation Consultant", **kw):
    row = {"job_id": "j", "title": title, "title_norm": title.lower(), "description_text": "", "employment_type": "full_time",
           "remote_type": "remote", "comp_min": None, "comp_max": None, "comp_period": None, "family": "process_analyst",
           "employer_name": "Synthetic Health", "parent": "Synthetic Health"}
    row.update(kw)
    return row


def fake(cov=(10, 0, 0), rest=(55, 0, 0), *, gap="strong_match", listed=10.0, core=(), decisive=(), opportunity=None) -> Assessment:
    """An assessment built from item weights: (evidenced at full credit, unknown, confirmed absent) per section."""
    def section(key, points, weights):
        e, u, a = weights
        items = [Item("e", e, EVIDENCED, 1.0)] * (e > 0) + [Item("u", u, UNKNOWN)] * (u > 0) + [Item("a", a, ABSENT)] * (a > 0)
        return Section(key, points, True, list(items))
    qualification = Axis([section("coverage", 45, cov), section("rest", 55, rest)])
    return Assessment("test", qualification, opportunity or Axis([section("pay", 100, (1, 0, 0))]), listed, 2.0, gap, None,
                      list(core), [], list(decisive))


class AxisArithmeticTests(unittest.TestCase):
    def test_partially_known_sections_and_the_three_quantities(self):
        areas = Section("areas", 20, True, [Item("a", 1, EVIDENCED, 1.0), Item("b", 1, UNKNOWN), Item("c", 1, UNKNOWN)])
        level = Section("level", 10, True, [Item("level", 1, EVIDENCED, 0.7)])
        duration = Section("duration", 20, False)          # no years requirement: not applicable, out of the denominator
        axis = Axis([areas, level, duration])
        self.assertEqual(axis.applicable_weight, 30)
        self.assertEqual((axis.low, axis.high, axis.coverage), (45.6, 90.0, 55.6))   # (6.67+7)/30, (20+7)/30, (6.67+10)/30
        self.assertEqual(axis.known, 82.0)                                            # 13.67 / 16.67

    def test_nothing_known_shows_no_known_only_score(self):
        axis = Axis([Section("coverage", 45, True, [Item("unlisted", 2, UNKNOWN)])])
        self.assertEqual((axis.low, axis.high, axis.coverage, axis.known), (0.0, 100.0, 0.0, None))

    def test_invariants_hold_for_any_item_states(self):
        rng = random.Random(7)
        for _ in range(300):
            sections = []
            for key, points in (("coverage", 45), ("areas", 20), ("duration", 20), ("level", 10), ("preferred", 5)):
                items = [Item(f"i{n}", rng.choice((0.5, 1, 2, 4)), rng.choice((EVIDENCED, UNKNOWN, ABSENT)), rng.choice((0.4, 0.7, 1.0)))
                         for n in range(rng.randint(1, 4))]
                sections.append(Section(key, points, key in ("coverage", "areas") or rng.random() < 0.7, items))
            axis = Axis(sections)
            for value in (axis.low, axis.high, axis.coverage):
                self.assertTrue(0 <= value <= 100)
            self.assertLessEqual(axis.low, axis.high)
            self.assertLessEqual(axis.low, axis.coverage + 0.05)
            self.assertTrue(math.isclose(axis.high - axis.low, 100 - axis.coverage, abs_tol=0.15))
            self.assertGreaterEqual(axis.applicable_weight, 65)

    def test_resolving_evidence_narrows_the_bounds_monotonically(self):
        def axis(state, credit=0.0):
            return Axis([Section("coverage", 45, True, [Item("a", 2, EVIDENCED, 1.0), Item("b", 2, state, credit)]),
                         Section("areas", 20, True, [Item("c", 1, EVIDENCED, 1.0)])])
        before = axis(UNKNOWN)
        for credit in (0.4, 0.7, 1.0):   # unknown -> evidenced: the lower bound never falls, the upper never rises
            after = axis(EVIDENCED, credit)
            self.assertGreaterEqual(after.low, before.low)
            self.assertLessEqual(after.high, before.high)
            self.assertTrue(before.low <= after.low <= before.high)        # the resolved score stays inside the earlier range
        absent = axis(ABSENT)                                              # unknown -> confirmed absent
        self.assertEqual(absent.low, before.low)
        self.assertLess(absent.high, before.high)


class AssessTests(unittest.TestCase):
    def test_a_posting_with_no_listed_skills_is_unknown_coverage_not_a_neutral_70(self):
        a = rubric.assess(job(), [R("experience", "business systems analysis", "hard_required", "business systems analysis", 0.9)],
                          PROFILE, POLICY, CFG)
        q = a.qualification
        self.assertEqual((q.applicable_weight, q.low, q.high, q.coverage, q.known), (75, 26.7, 100.0, 26.7, 100.0))  # spec example
        self.assertEqual((a.cov_low, a.listed_weight, a.min_weight, a.gap), (0.0, 0.0, 2.0, "none"))
        self.assertEqual(sp.decide(a, state="ELIGIBLE", degree_stretch=False, cfg=CFG).status, "check_first")

    def test_a_thin_skill_list_leaves_the_unlisted_remainder_unknown(self):
        a = rubric.assess(job(), [R("skill", "jira", "hard_required", "Jira required", 0.9)], PROFILE, POLICY, CFG)
        self.assertEqual((a.listed_weight, a.min_weight), (1.0, 2.0))                     # 'supporting' = 1 point, under m = 2
        self.assertEqual((a.cov_low, a.cov_high, a.cov_known), (50.0, 100.0, 50.0))       # not 100: the rest is unknown
        self.assertIn("required skills not listed or too few", a.facts_to_resolve)

    def test_everything_unknown_still_stays_visible(self):
        reqs = [R("experience", "clinical informatics", "hard_required", "clinical informatics", 0.9),
                R("experience", "epic implementation", "hard_required", "epic implementation", 0.9),
                R("years_experience", "clinical informatics", "hard_required", "2+ years of clinical informatics experience", 0.9, years=2.0)]
        a = rubric.assess(job(), reqs, PROFILE, POLICY, CFG)
        q = a.qualification
        self.assertEqual((q.applicable_weight, q.low, q.high, q.coverage, q.known, a.gap), (95, 0.0, 100.0, 0.0, None, "unresolved"))
        self.assertEqual(sp.decide(a, state="CONDITIONAL", degree_stretch=False, cfg=CFG).status, "check_first")

    def test_posting_level_unspecified_or_ambiguous_is_unknown_and_separate_from_your_level(self):
        terms = POLICY.get("seniority_terms")
        self.assertEqual(rubric.posting_level("business analyst", terms), (None, "posting_level_unspecified"))
        self.assertEqual(rubric.posting_level("senior associate", terms), (None, "posting_level_ambiguous"))
        self.assertEqual(rubric.posting_level("senior business analyst", terms), ("senior", None))
        self.assertEqual(rubric.assess(job("Senior Associate"), [], PROFILE, POLICY, CFG).level_reason, "posting_level_ambiguous")
        unset = Profile(data={**PROFILE.data, "current_level": None}, fingerprint="u")
        self.assertEqual(rubric.assess(job("Senior Analyst"), [], unset, POLICY, CFG).level_reason, "candidate_level_unset")
        lower = rubric.assess(job("Junior Analyst"), [], PROFILE, POLICY, CFG)      # below your level: you can do it
        self.assertEqual(lower.qualification.section("level").items[0].credit, 1.0)
        self.assertEqual(lower.opportunity.section("progression").items[0].credit, 0.4)  # but it doesn't move you forward

    def test_planted_credentials_and_declared_gaps_are_confirmed_absent(self):
        lacking = Profile(data={**PROFILE.data, "lacks": ["Salesforce"]}, fingerprint="l")
        reqs = [R("skill", "salesforce", "hard_required", "Salesforce required", 0.9), R("skill", "sql", "hard_required", "SQL required", 0.9)]
        states = {i.name: i.state for i in rubric.assess(job(), reqs, lacking, POLICY, CFG).qualification.section("coverage").items}
        self.assertEqual((states["salesforce"], states["sql"]), (ABSENT, EVIDENCED))
        states = {i.name: i.state for i in rubric.assess(job(), reqs, PROFILE, POLICY, CFG).qualification.section("coverage").items}
        self.assertEqual(states["salesforce"], UNKNOWN)                     # not declared: merely no profile evidence

    def test_duration_alternatives_differ_only_in_the_relevance_points(self):
        reqs = [R("years_experience", "business systems analysis", "hard_required", "8+ years of business systems analysis", 0.9, years=8.0)]
        d1 = rubric.assess(job(), reqs, PROFILE, POLICY, CFG, duration="D1").qualification.section("duration").items[0]
        d2 = rubric.assess(job(), reqs, PROFILE, POLICY, CFG, duration="D2").qualification.section("duration").items[0]
        self.assertNotEqual(d1.credit, d2.credit)
        self.assertEqual(d2.credit, round(d2.detail["score"] / 20, 4))

    def test_unknown_pay_never_triggers_the_lower_desirability_note(self):
        a = rubric.assess(job(), [], PROFILE, POLICY, CFG)
        self.assertEqual(a.opportunity.section("pay").items[0].state, UNKNOWN)
        self.assertFalse(sp.lower_desirability(a, CFG))
        self.assertEqual(a.opportunity.mid, round((a.opportunity.low + a.opportunity.high) / 2, 1))
        wide = fake(opportunity=Axis([Section("pay", 30, True, [Item("pay", 1, UNKNOWN)]),
                                      Section("other", 70, True, [Item("other", 1, EVIDENCED, 0.3)])]))
        self.assertEqual((wide.opportunity.low, wide.opportunity.mid, wide.opportunity.high), (21.0, 36.0, 51.0))
        self.assertFalse(sp.lower_desirability(wide, CFG))                                 # the note uses O_high, never O_mid

    def test_the_shadow_configuration_is_not_part_of_the_decision_policy(self):
        self.assertEqual(rubric.SHADOW_CONFIG, CONFIG_DIR / "shadow_scoring.yaml")
        for key in ("qualification", "opportunity", "thresholds", "primary"):
            self.assertNotIn(key, POLICY.data)


class DecisionTableTests(unittest.TestCase):
    def status(self, a, state="ELIGIBLE", stretch=False, core="R2"):
        return sp.decide(a, state=state, degree_stretch=stretch, cfg=CFG, core=core).status

    def test_tiers_need_eligible_known_coverage_and_a_resolved_duration(self):
        self.assertEqual(self.status(fake()), "A")
        self.assertEqual(self.status(fake(cov=(8, 2, 0), gap="small_gap")), "B")           # CovL 80; A needs CovC 100
        for state in ("CONDITIONAL",):
            self.assertEqual(self.status(fake(), state), "check_first")                    # never falls into A/B
        self.assertEqual(self.status(fake(), "HOLD"), "held")
        self.assertEqual(self.status(fake(), "INELIGIBLE"), "rejected")
        self.assertEqual(self.status(fake(gap="unresolved")), "stretch_review")           # fully known otherwise: nothing to check
        self.assertEqual(self.status(fake(rest=(45, 10, 0), gap="unresolved")), "check_first")
        self.assertEqual(self.status(fake(listed=1.5)), "stretch_review")                 # too few listed skills: never A/B

    def test_a_fully_known_job_failing_the_coverage_guard_is_a_stretch_not_check_first(self):
        self.assertEqual(self.status(fake(cov=(6, 0, 4))), "stretch_review")              # Q 82, CovL 60, C 100
        self.assertEqual(self.status(fake(cov=(6, 4, 0))), "check_first")                 # same, but the gap is unknown

    def test_sparse_postings_stay_visible_but_never_reach_a_tier(self):
        sparse = fake(cov=(0, 2, 0), listed=0.0, rest=(10, 45, 0))
        self.assertEqual(self.status(sparse), "check_first")
        self.assertEqual(self.status(fake(cov=(0, 2, 0), listed=0.0, rest=(0, 10, 45))), "near_miss")  # Q_high 55
        self.assertEqual(self.status(fake(cov=(0, 2, 0), listed=0.0, rest=(0, 2, 53))), "archive")     # Q_high 47: weak at best

    def test_caps_for_stretch_and_experience_gaps_only_lower(self):
        self.assertEqual(self.status(fake(), stretch=True), "stretch_review")
        self.assertEqual(self.status(fake(gap="medium_gap")), "stretch_review")
        self.assertEqual(self.status(fake(gap="large_gap")), "stretch_review")
        self.assertEqual(self.status(fake(gap="severe_gap")), "near_miss")
        weak = fake(cov=(1, 0, 9), rest=(10, 0, 45))
        self.assertEqual(self.status(weak, stretch=True), "archive")                       # a cap never raises

    def test_core_alternatives_route_as_specified_and_are_monotonic(self):
        core = [{"name": "rxclaim", "state": UNKNOWN, "requirement_type": "hard_required", "importance_source": "inferred"}]
        x3 = fake(cov=(12, 4, 0), core=core)                                               # Q_low 88.8: B numerically
        self.assertEqual([self.status(x3, core=c) for c in sp.CORE_ALTERNATIVES], ["B", "B", "check_first", "near_miss"])
        absent = [{**core[0], "state": ABSENT}]
        x3b = fake(cov=(12, 0, 4), core=absent)
        self.assertEqual([self.status(x3b, core=c) for c in sp.CORE_ALTERNATIVES], ["B", "stretch_review", "stretch_review", "near_miss"])
        rng = random.Random(3)
        for _ in range(200):
            a = fake(cov=(rng.randint(0, 9), rng.randint(0, 3), rng.randint(0, 3)), rest=(rng.randint(0, 55), rng.randint(0, 20), rng.randint(0, 20)),
                     gap=rng.choice(("strong_match", "small_gap", "none", "unresolved")),
                     core=[{"name": "x", "state": rng.choice((UNKNOWN, ABSENT, EVIDENCED)),
                            "requirement_type": rng.choice(("hard_required", "ambiguous")), "importance_source": "inferred"}])
            ranks = [sp.rank(self.status(a, core=c)) for c in sp.CORE_ALTERNATIVES]
            self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_untolerated_decisive_unknowns_keep_a_job_out_of_tier_a(self):
        self.assertEqual(self.status(fake(decisive=["pay"])), "A")                         # unposted pay is tolerated
        self.assertEqual(self.status(fake(decisive=["schedule"])), "B")


class QueueTests(unittest.TestCase):
    def test_your_decisions_set_actionability_without_changing_the_status(self):
        self.assertEqual(sp.actionability("skip", False, False), "declined")
        self.assertEqual(sp.actionability(None, True, False), "declined")
        self.assertEqual(sp.actionability("apply", False, True), "preference_conflict")
        self.assertEqual(sp.actionability("stretch", False, False), "actionable")
        self.assertEqual(sp.queue("B", "declined"), "declined")
        self.assertEqual(sp.queue("B", "preference_conflict"), "preference_conflict")
        self.assertEqual(sp.queue("archive", "preference_conflict"), "hidden")
        self.assertEqual(sp.queue("check_first", "actionable"), "check_first")
        self.assertEqual(sp.queue("held", "declined"), "held")

    def test_ordering_alternatives(self):
        high = fake(cov=(0, 2, 0), listed=0.0, rest=(30, 25, 0))          # Q_low 30, Q_high 100
        low = fake(cov=(0, 2, 0), listed=0.0, rest=(10, 45, 0))           # Q_low 10, Q_high 100
        d = sp.Decision("check_first", "check_first", "3c", facts=["a"])
        many = sp.Decision("check_first", "check_first", "3c", facts=["a", "b", "c"])
        o2 = sorted([("x", low, d), ("y", high, many)], key=lambda t: sp.order_key("check_first", t[1], t[2], t[0], CFG, check_first="O2"))
        self.assertEqual([t[0] for t in o2], ["y", "x"])                  # higher known qualification first
        o1 = sorted([("x", low, d), ("y", high, many)], key=lambda t: sp.order_key("check_first", t[1], t[2], t[0], CFG, check_first="O1"))
        self.assertEqual([t[0] for t in o1], ["y", "x"])                  # equal Q_high: higher coverage first
        note = fake(opportunity=Axis([Section("pay", 100, True, [Item("p", 1, EVIDENCED, 0.2)])]))
        plain = fake()
        b = sp.Decision("B", "B", "3b")
        keys = sorted([("noted", note, b), ("plain", plain, b)], key=lambda t: sp.order_key("B", t[1], t[2], t[0], CFG))
        self.assertEqual([t[0] for t in keys], ["plain", "noted"])        # lower desirability orders last within a tier


if __name__ == "__main__":
    unittest.main()
