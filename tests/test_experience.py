"""Experience Match (2026-10-15, shadow mode): the 20-point score, its classes and coverage descriptor, the parsing fixes
it depends on, the tier effect it WOULD have, and the guarantee that it changes no tier while enforcement is off.
All data is synthetic."""
import copy
import json
import unittest

from careeros.extract.model import Requirement
from careeros.extract.rules import _line_records, extract, year_spans
from careeros.qualify import experience as exp
from careeros.qualify import night
from careeros.qualify.gate import evaluate
from careeros.qualify.profile import Profile
from careeros.qualify.scores import score
from careeros.review.labels import system_class, three_way

from tests import postings
from tests.helpers import add_job, new_store
from tests.test_qualify import NOW, POLICY, PROFILE, job
from tests.test_v21_policy import policy_with

CFG = exp.settings(POLICY)
ALIASES, DOMAINS = POLICY.get("skill_aliases", default={}), POLICY.get("domain_terms", default={})
BSA_LINE = "5+ years of experience as a business systems analyst in healthcare"
TITLE = "Senior Business Systems Analyst - Pharmacy Benefits"
ENFORCED = policy_with(experience__enforce=True)
ENFORCED_A = policy_with(experience__enforce=True, tiers__A={"priority": 85})
SHADOW_A = policy_with(tiers__A={"priority": 85})
FORBIDDEN = ("exceptional", "minor risk", "likely qualified", "strong candidate", "great fit")


def years(req, have, kind="direct"):
    return exp.score_years(req, have, kind, CFG)


def posting(required_years: int) -> str:
    return postings.GOOD_FIT.replace(BSA_LINE, BSA_LINE.replace("5+", f"{required_years}+"))


def card(text, policy=POLICY, title=TITLE):
    j = job(title, text)
    reqs = extract(j["description_text"], policy).requirements
    gate = evaluate(j, reqs, PROFILE, policy)
    return gate, (score(j, reqs, PROFILE, gate, policy, NOW) if gate.decision == "eligible" else None)


def lines(line, rtype="hard_required"):
    return [(r.category, r.name, r.years) for r in _line_records(line, rtype, 0.9, ALIASES, DOMAINS)]


class ScoreTests(unittest.TestCase):
    def test_the_worked_examples(self):
        for (req, have), (total, gap) in {(5, 4): (17, "small_gap"), (2, 1): (15, "small_gap"), (10, 8): (15, "small_gap"),
                                          (5, 3.5): (14, "medium_gap"), (5, 2): (11, "medium_gap"), (5, 5): (20, "strong_match"),
                                          (5, 7): (20, "strong_match"), (3, 0.3): (8, "large_gap"),
                                          (5, 0.3): (4, "severe_gap")}.items():
            result = years(req, have)
            self.assertEqual((result["score"], result["gap_class"]), (total, gap), f"{have} of {req}")

    def test_one_of_two_scores_below_four_of_five_although_both_are_a_year_short(self):
        self.assertEqual(years(2, 1)["shortfall_months"], years(5, 4)["shortfall_months"])
        self.assertLess(years(2, 1)["score"], years(5, 4)["score"])

    def test_the_parts_are_stored(self):
        result = years(5, 4)
        self.assertEqual((result["absolute_points"], result["coverage_points"], result["relevance_points"]), (8, 5, 4))
        self.assertEqual((result["shortfall_months"], result["coverage_pct"]), (12.0, 80.0))

    def test_coverage_descriptor(self):
        for (req, have), descriptor in {(5, 4): "high_coverage", (2, 1): "moderate_coverage", (5, 2): "moderate_coverage",
                                        (3, 0.3): "very_low_coverage", (5, 5): "full_coverage", (5, 7): "full_coverage",
                                        (5, 3): "substantial_coverage", (5, 1): "low_coverage"}.items():
            self.assertEqual(years(req, have)["coverage_class"], descriptor, f"{have} of {req}")

    def test_coverage_boundaries_are_exact(self):
        self.assertEqual((years(3, 2.4)["coverage_points"], years(3, 2.4)["coverage_class"]), (5, "high_coverage"))  # 80% exactly
        self.assertEqual(years(100, 79.99)["coverage_points"], 4)
        self.assertEqual((years(10, 6)["coverage_points"], years(10, 5.99)["coverage_points"]), (4, 3))
        self.assertEqual((years(10, 4)["coverage_points"], years(10, 3.99)["coverage_points"]), (3, 2))
        self.assertEqual((years(10, 2)["coverage_class"], years(10, 1.99)["coverage_class"]), ("low_coverage", "very_low_coverage"))
        self.assertEqual((years(10, 2)["coverage_points"], years(10, 1.99)["coverage_points"]), (2, 0))

    def test_shortfall_boundaries_are_exact(self):
        for short_months, points in ((0, 10), (6, 9), (7, 8), (12, 8), (13, 6), (24, 6), (25, 4), (36, 4), (37, 2), (48, 2), (49, 0)):
            self.assertEqual(years(30, 30 - short_months / 12)["absolute_points"], points, f"{short_months} months short")
        self.assertEqual(years(1.5, 1)["shortfall_months"], 6.0)  # 18 months against 12: exactly 6, so 9 points
        self.assertEqual(years(1.5, 1)["absolute_points"], 9)

    def test_class_boundaries(self):
        cases = {20: "strong_match", 18: "strong_match", 17: "small_gap", 15: "small_gap", 14: "medium_gap", 11: "medium_gap",
                 10: "large_gap", 7: "large_gap", 6: "severe_gap", 0: "severe_gap"}
        from decimal import Decimal
        for total, gap in cases.items():
            self.assertEqual(exp._band(Decimal(total), CFG["classes"], False, CFG["lowest_class"]), gap)

    def test_relevance_uses_the_evidence_of_the_item_counted(self):
        for kind, total in (("direct", 17), ("strongly_transferable", 16), ("adjacent", 14), ("missing", 14), ("generic", 15)):
            self.assertEqual(years(5, 4, kind)["score"], total, kind)


class ProfileFactsTests(unittest.TestCase):
    PEOPLE = Profile({"functions": [{"name": "claims operations", "years": 3.9, "screening_years": 4, "evidence": "direct"},
                                    {"name": "vendor management", "years": 4, "evidence": "adjacent"},
                                    {"name": "reporting", "years": 2}],
                      "domains": [{"name": "pbm", "years": 3}], "total_professional_years": 7}, "x")

    def test_screening_is_counted_and_the_fact_is_kept_beside_it(self):
        before = copy.deepcopy(self.PEOPLE.data)
        item = exp.score_requirement(Requirement("years_experience", "claims operations", "hard_required", "5+ years", 0.9, 5.0),
                                     self.PEOPLE, CFG)
        self.assertEqual((item["counted_years"], item["screening_years"], item["factual_years"]), (4.0, 4.0, 3.9))
        self.assertEqual(self.PEOPLE.data, before)   # nothing in the profile is rewritten
        text = " ".join(exp.explain({"lines": [item], "decisive_line": 0, "effect": "none", "tier": "B", "hypothetical_tier": "B"}))
        self.assertIn("Factual experience: 3.9 years; screening value used: 4 years.", text)

    def test_adjacent_and_missing_evidence_and_domains(self):
        adjacent = exp.score_requirement(Requirement("years_experience", "vendor management", "hard_required", "", 0.9, 5.0), self.PEOPLE, CFG)
        missing = exp.score_requirement(Requirement("years_experience", "reporting", "hard_required", "", 0.9, 2.0), self.PEOPLE, CFG)
        domain = exp.score_requirement(Requirement("domain", "pbm", "hard_required", "", 0.9, 3.0), self.PEOPLE, CFG)
        generic = exp.score_requirement(Requirement("years_experience", "relevant experience", "hard_required", "", 0.9, 5.0), self.PEOPLE, CFG)
        self.assertEqual((adjacent["evidence"], adjacent["relevance_points"], adjacent["factual_years"]), ("adjacent", 1, 4.0))
        self.assertEqual((missing["evidence"], missing["relevance_points"], missing["factual_years"]), ("missing", 1, 2.0))
        self.assertEqual((domain["evidence"], domain["relevance_points"]), ("direct", 4))   # a domain you hold, as in skill coverage
        self.assertEqual((generic["evidence"], generic["relevance_points"], generic["counted_years"]), ("generic", 2, 7.0))

    def test_years_from_two_items_are_never_added(self):
        item = exp.score_requirement(Requirement("years_experience", "claims operations or vendor management", "hard_required",
                                                 "", 0.9, 8.0), self.PEOPLE, CFG)
        self.assertEqual(item["counted_years"], 4.0)   # the best single item, never 4 + 4

    def test_unknown_years_are_not_scored(self):
        item = exp.score_requirement(Requirement("years_experience", "data engineering", "hard_required", "", 0.9, 3.0), self.PEOPLE, CFG)
        self.assertEqual((item["score"], item["gap_class"]), (None, "unknown"))


class ParsingTests(unittest.TestCase):
    def test_ranges_plus_at_least_minimum_and_words(self):
        self.assertEqual(year_spans("3-5 years of claims experience")[0][2:], (3.0, 5.0))
        self.assertEqual(year_spans("3 to 5 years of claims experience")[0][2:], (3.0, 5.0))
        for line in ("5+ years of claims experience", "at least 5 years of claims experience", "minimum 5 years of claims experience",
                     "at least five (5) years of claims experience"):
            self.assertEqual(year_spans(line)[0][2:], (5.0, None), line)

    def test_months_only_in_an_experience_phrase(self):
        self.assertEqual(lines("18 months of claims processing experience required"),
                         [("domain", "claims", 1.5), ("years_experience", "claims processing", 1.5)])
        self.assertEqual(year_spans("6+ months experience with Excel")[0][2], 0.5)
        for line in ("Obtain certification within 6 months of hire; experience with Excel", "a 6 month probation period",
                     "after 6 months of employment you get experience with our tools"):
            self.assertEqual(year_spans(line), [], line)

    def test_each_area_takes_its_own_figure(self):
        self.assertEqual(lines("2+ years of SQL and 5+ years of claims experience"), [("skill", "sql", 2.0), ("domain", "claims", 5.0)])
        self.assertEqual(lines("SQL (2+ years) and claims (5+ years)"), [("skill", "sql", 2.0), ("domain", "claims", 5.0)])
        self.assertEqual(lines("7+ years of warehouse operations, with 3+ years of multi-site leadership"),
                         [("years_experience", "warehouse operations", 7.0), ("years_experience", "multi-site leadership", 3.0)])
        self.assertEqual(lines("2+ years of claims and 5+ years of people management"),
                         [("domain", "claims", 2.0), ("years_experience", "claims", 2.0), ("years_experience", "people management", 5.0)])

    def test_one_figure_still_applies_to_the_whole_line(self):
        self.assertEqual(lines("5+ years of experience with SQL and Excel"), [("skill", "sql", 5.0), ("skill", "excel", 5.0)])

    def test_the_range_upper_bound_is_kept_for_the_explanation(self):
        reqs = extract("Required Qualifications:\n- 3-5 years of claims experience required\n- SQL", POLICY).requirements
        block = exp.evaluate(reqs, PROFILE, POLICY)
        self.assertEqual((block["lines"][block["decisive_line"]]["required_years"], block["lines"][block["decisive_line"]]["required_upper"]), (3.0, 5.0))
        self.assertIn("Required: 3-5 years of", exp.explain(block)[1])


class LineTests(unittest.TestCase):
    def block(self, *texts, rtype="hard_required"):
        reqs = [r for t in texts for r in _line_records(t, rtype, 0.9, ALIASES, DOMAINS)]
        return exp.evaluate(reqs, PROFILE, POLICY)

    def test_an_or_list_is_judged_on_the_best_alternative(self):
        block = self.block("3+ years of experience as a business systems analyst, consultant, or data engineer")
        line = block["lines"][block["decisive_line"]]
        self.assertEqual((line["structure"], line["counted_years"], line["gap_class"]), ("or", 6.0, "strong_match"))

    def test_an_and_list_is_judged_on_the_strictest_item(self):
        block = self.block("5+ years of experience with SQL and Excel")   # SQL 4, Excel 8
        line = block["lines"][block["decisive_line"]]
        self.assertEqual((line["structure"], line["area"], line["counted_years"]), ("and", "sql", 4.0))
        self.assertEqual(line["gap_class"], "small_gap")

    def test_two_figures_on_one_line_are_two_requirements(self):
        block = self.block("2+ years of SQL and 8+ years of claims experience")   # SQL 4 of 2; claims 4 of 8
        self.assertEqual(sorted((l["area"], l["required_years"]) for l in block["lines"]), [("claims", 8.0), ("sql", 2.0)])
        self.assertEqual(block["lines"][block["decisive_line"]]["area"], "claims")

    def test_the_decisive_line_is_the_strictest_required_line_and_preferred_never_overrides_it(self):
        reqs = (_line_records("5+ years of SQL experience", "hard_required", 0.9, ALIASES, DOMAINS)
                + _line_records("10+ years of claims experience", "preferred", 0.9, ALIASES, DOMAINS))
        block = exp.evaluate(reqs, PROFILE, POLICY)
        self.assertEqual(block["lines"][block["decisive_line"]]["strength"], "hard_required")
        self.assertEqual(block["gap_class"], "small_gap")   # SQL 4 of 5, not the preferred claims 4 of 10

    def test_preferred_lines_are_scored_but_never_have_an_effect(self):
        block = self.block("10+ years of claims experience", rtype="preferred")
        self.assertEqual((block["gap_class"], block["effect"]), ("large_gap", "none"))

    def test_conflicting_figures_for_one_area(self):
        block = self.block("3+ years of claims experience", "5+ years of claims experience required")
        self.assertEqual([(c["area"], c["years"], c["counted"]) for c in block["conflicts"]],
                         [("claims", [3.0, 5.0], 5.0), ("claims", [3.0, 5.0], 5.0)])
        self.assertEqual(block["lines"][block["decisive_line"]]["required_years"], 5.0)
        _gate, card_ = card(postings.GOOD_FIT.replace("- 3+ years of SQL experience required",
                                                      "- 3+ years of SQL experience required\n- 6+ years of SQL experience required"))
        self.assertIn("years_conflicting", {f["code"] for f in _gate.flags})


class EffectTests(unittest.TestCase):
    def test_effects_follow_requirement_strength(self):
        table = {"hard_required": {"strong_match": "none", "small_gap": "none", "medium_gap": "cap_b", "large_gap": "stretch",
                                   "severe_gap": "stretch"},
                 "required_with_equivalency": {"medium_gap": "none", "large_gap": "cap_b", "severe_gap": "stretch"},
                 "ambiguous": {"medium_gap": "none", "large_gap": "cap_b", "severe_gap": "stretch"},
                 "preferred": {"medium_gap": "none", "large_gap": "none", "severe_gap": "none"}}
        for strength, classes in table.items():
            for gap, effect in classes.items():
                line = {"score": 9, "gap_class": gap, "strength": strength, "source_text": "x", "required_years": 3.0}
                self.assertEqual(exp._effect(line, CFG, set()), effect, (strength, gap))

    def test_hypothetical_tiers_never_rise(self):
        self.assertEqual([exp.hypothetical_tier(t, "cap_b") for t in ("A", "B", "near_miss", "archive")], ["B", "B", "near_miss", "archive"])
        self.assertEqual([exp.hypothetical_tier(t, "stretch") for t in ("A", "B", "near_miss", "archive")],
                         ["near_miss", "near_miss", "near_miss", "archive"])
        self.assertEqual(exp.hypothetical_tier("near_miss", "none"), "near_miss")


class ShadowModeTests(unittest.TestCase):
    def test_tier_a_with_a_medium_gap_stays_a_but_would_be_b(self):
        _gate, shadow = card(posting(8), SHADOW_A)
        _gate, enforced = card(posting(8), ENFORCED_A)
        self.assertEqual((shadow.tier, shadow.components["experience"]["effect"], shadow.components["experience"]["hypothetical_tier"]),
                         ("A", "cap_b", "B"))
        self.assertEqual((enforced.tier, enforced.components["stretch"]), ("B", False))

    def test_tier_b_with_a_small_gap_is_unchanged_either_way(self):
        for policy in (POLICY, ENFORCED):
            _gate, result = card(posting(7), policy)   # BSA 6 of 7, healthcare 5 of 7 -> the strictest is small or medium
            self.assertEqual(result.tier, "B")
        _gate, small = card(postings.GOOD_FIT.replace("- 3+ years of SQL experience required", "- 5+ years of SQL experience required"))
        self.assertEqual((small.tier, small.components["experience"]["gap_class"], small.components["experience"]["effect"]),
                         ("B", "small_gap", "none"))

    def test_tier_b_with_a_large_gap_is_only_shown_as_a_future_stretch(self):
        _gate, shadow = card(posting(9))
        _gate, enforced = card(posting(9), ENFORCED)
        self.assertEqual((shadow.tier, shadow.components["stretch"], shadow.components["experience"]["hypothetical_tier"]),
                         ("B", False, "near_miss"))
        self.assertEqual((enforced.tier, enforced.components["stretch"], enforced.components["stretch_reason"]),
                         ("near_miss", True, "experience"))

    def test_a_severe_gap_under_four_years_short_is_a_stretch_never_a_rejection(self):
        gate, result = card(posting(10))   # 4 years short on the best item: the gate's rule does not reject
        self.assertEqual(gate.decision, "eligible")
        self.assertEqual((result.tier, result.components["experience"]["effect"]), ("B", "stretch"))

    def test_more_than_four_years_short_is_still_rejected_and_explained(self):
        for policy in (POLICY, ENFORCED):
            gate, _ = card(posting(11), policy)
            failure = next(f for f in gate.hard_failures if f["code"] == "years_far_short")
            self.assertEqual((gate.decision, failure["required_years"]), ("rejected", 11.0))
            self.assertEqual(failure["experience"]["counted_years"], 6.0)
        text = " ".join(exp.explain(exp.rejection_block(failure)))
        self.assertIn("rejected by the existing years rule", text)

    def test_shadow_mode_never_changes_a_tier(self):
        for text in (postings.GOOD_FIT, posting(7), posting(8), posting(9), posting(10), postings.FLAT):
            for title in (TITLE, "Business Analyst"):
                for default, current in ((POLICY, POLICY), (SHADOW_A, SHADOW_A)):
                    without = copy.deepcopy(default.data)
                    without.pop("experience", None)
                    from careeros.core.policy import PolicyBundle
                    _g, baseline = card(text, PolicyBundle(without, "no-experience"), title)
                    _g, now = card(text, current, title)
                    if baseline is None:
                        self.assertIsNone(now)
                        continue
                    self.assertEqual((now.tier, now.state, now.apply_priority, now.components["stretch"]),
                                     (baseline.tier, baseline.state, baseline.apply_priority, baseline.components["stretch"]))

    def test_unknown_years_keep_the_job_conditional_and_unscored(self):
        _gate, result = card(postings.GOOD_FIT.replace(BSA_LINE, "5+ years of data engineering experience"))
        block = result.components["experience"]
        self.assertEqual(result.state, "CONDITIONAL")
        self.assertNotIn("data engineering", [line["area"] for line in block["lines"] if line["score"] is not None])

    def test_a_degree_equivalency_line_is_not_penalised_twice(self):
        no_degree = Profile({**PROFILE.data, "education": [{"level": "high_school", "completed": True}]}, "no-degree")
        line = "- Bachelor's degree or equivalent: 9 years of business systems analysis experience"
        text = postings.GOOD_FIT.replace("- Bachelor's degree or equivalent work experience", line)
        j = job(TITLE, text)
        reqs = extract(text, POLICY).requirements
        gate = evaluate(j, reqs, no_degree, POLICY)
        self.assertEqual(gate.decision, "eligible")                       # neither path rejects
        self.assertIn("degree_equivalency", {f["code"] for f in gate.flags})
        block = score(j, reqs, no_degree, gate, POLICY, NOW).components["experience"]
        years_line = next(l for l in block["lines"] if l["required_years"] == 9.0)
        self.assertEqual((years_line["strength"], years_line["gap_class"], years_line["effect"]),
                         ("required_with_equivalency", "medium_gap", "none"))   # a hard line with this gap would cap at B
        self.assertEqual(exp._effect({**years_line, "strength": "hard_required"}, CFG, set()), "cap_b")

    def test_high_potential_is_a_note_only(self):
        _gate, result = card(posting(8))
        block = result.components["experience"]
        self.assertTrue(block["high_potential"])
        self.assertEqual(block["basis"], {"skill_coverage": 1.0, "domain_level": "direct"})
        _gate, plain = card(posting(8), policy_with(experience__high_potential={"gap_classes": [], "min_skill_coverage": 0.85,
                                                                                  "domain_levels": ["direct"]}))
        self.assertEqual((plain.tier, plain.apply_priority), (result.tier, result.apply_priority))

    def test_night_results_do_not_depend_on_experience_match(self):
        text = postings.GOOD_FIT + "\nSchedule: overnight shift, 11pm to 7am."
        j = job("Claims Quality Auditor", text)
        results = [night.assess(j, extract(text, p).requirements, PROFILE, policy_with(night__enabled=True, experience__enforce=flag), 60.0)
                   for p, flag in ((POLICY, False), (POLICY, True))]
        self.assertEqual(results[0], results[1])
        self.assertNotIn("experience", json.dumps(results[0]))


class ExplanationTests(unittest.TestCase):
    def test_identical_blocks_give_identical_text_from_stored_numbers(self):
        _gate, result = card(posting(9))
        block = result.components["experience"]
        first, second = exp.explain(block), exp.explain(json.loads(json.dumps(block)))
        self.assertEqual(first, second)
        line = block["lines"][block["decisive_line"]]
        self.assertEqual(first[0], f"Experience {line['score']}/20: large gap, {line['coverage_class'].replace('_', ' ')}.")
        self.assertIn(f'Posting line: "{line["source_text"]}"', first)
        self.assertIn("Current effect: none (shadow mode). Future effect if enforced: stretch (Tier A/B lowered to near-miss), "
                      "tier B would become near_miss.", first)
        self.assertTrue(any(s.startswith("High potential: yes (weighted skill coverage 100%, domain direct)") for s in first))
        self.assertIn("· shadow mode, if enforced: stretch", exp.summary(block))

    def test_no_judgement_words(self):
        for required in (5, 7, 8, 9, 10):
            _gate, result = card(posting(required))
            text = (" ".join(exp.explain(result.components["experience"])) + exp.summary(result.components["experience"])).lower()
            for word in FORBIDDEN:
                self.assertNotIn(word, text)

    def test_nothing_to_score(self):
        self.assertEqual(exp.explain({"decisive_line": None}),
                         ["Experience: no years requirement could be scored (none stated, or your years for it are not set)."])


class ThreeWayTests(unittest.TestCase):
    def setUp(self):
        self.store, _clock = new_store()

    def _scored(self, job_id, tier, label, stretch=False, block=None, state="scored"):
        add_job(self.store, job_id, current_state=state)
        writer = self.store.writer("qualify")
        if state == "scored":
            writer.insert("scores", {"score_id": f"s:{job_id}", "job_id": job_id, "at": NOW, "policy_fingerprint": "p",
                                     "profile_fingerprint": "f", "eligibility": 80, "candidate_fit": 80, "career_value": 80,
                                     "freshness": 80, "apply_priority": 80, "tier": tier, "interview_band": "High",
                                     "components_json": json.dumps({"stretch": stretch, "experience": block}), "state": "ELIGIBLE"})
        self.store.writer("review").insert("labels", {"label_id": f"l:{job_id}", "job_id": job_id, "at": NOW, "label": label,
                                                      "reason": None, "system_tier": tier, "system_decision": state, "profile": "primary"})

    @staticmethod
    def _block(tier, effect, gap):
        after = exp.hypothetical_tier(tier, effect)
        return {"lines": [{"strength": "hard_required", "score": 9, "gap_class": gap}], "decisive_line": 0, "gap_class": gap,
                "effect": effect, "tier": tier, "hypothetical_tier": after}

    def test_the_three_way_matrix_and_what_enforcement_would_change(self):
        self._scored("j1", "B", "stretch", block=self._block("B", "stretch", "large_gap"))
        self._scored("j2", "near_miss", "stretch", stretch=True)
        self._scored("j3", None, "skip", state="rejected")
        self._scored("j4", "A", "apply", block=self._block("A", "none", "strong_match"))
        self._scored("j5", "near_miss", "apply")
        self._scored("j6", "B", "skip")
        result = three_way(self.store, POLICY, None)
        self.assertEqual(result["confusion"]["stretch"]["apply"], 1)
        self.assertEqual(result["confusion"]["stretch"]["stretch"], 1)
        self.assertEqual(result["confusion"]["apply"]["near_miss_unflagged"], 1)   # never counted as stretch
        self.assertEqual(result["agreement"], 0.5)                                 # j2, j3, j4 of 6
        self.assertEqual(result["if_experience_enforced"]["agreement"], 0.667)     # j1 would become a stretch
        self.assertEqual(result["if_experience_enforced"]["moves"], {"apply -> stretch": 1})
        self.assertEqual((result["apply_false_negatives"]["jobs"], result["stretch_misclassified"]["jobs"],
                          result["skip_false_positives"]["jobs"]), (["j5"], ["j1"], ["j6"]))
        self.assertEqual(result["gap_classes"]["labelled"]["stretch"], {"large_gap": 1, "unknown": 1})
        self.assertEqual(result["false_reject_rate"], 0.0)
        self.assertEqual(result["calibration"]["status"], "not_ready")
        self.assertEqual(result["calibration"]["labels_with_years_requirement"], 2)

    def test_calibration_ready_never_switches_enforcement_on(self):
        self._scored("j1", "B", "stretch", block=self._block("B", "stretch", "large_gap"))
        ready = three_way(self.store, policy_with(experience__calibration={"min_labels": 1, "min_years_labels": 1}), None)
        self.assertEqual((ready["calibration"]["status"], ready["calibration"]["enforced"]), ("EXPERIENCE_CALIBRATION_READY", False))

    def test_system_classes(self):
        self.assertEqual([system_class("scored", t, s) for t, s in (("A", False), ("B", True), ("near_miss", True),
                                                                      ("near_miss", False), ("archive", False))],
                         ["apply", "apply", "stretch", "near_miss_unflagged", "skip"])
        self.assertEqual((system_class("rejected", None, False), system_class("needs_review", None, False)), ("skip", "not_judged"))


if __name__ == "__main__":
    unittest.main()
