import unittest
from pathlib import Path

from careeros.core.policy import load_policy
from careeros.extract.rules import extract
from careeros.normalize.posting import (canonical_url, classify_family, detect_country, detect_remote, employment_type,
                                        excluded_term, norm, parse_comp, remote_states)
from careeros.normalize.sanitize import html_to_text
from careeros.paths import CONFIG_DIR
from careeros.qualify.gate import evaluate
from careeros.qualify.profile import load_profile
from careeros.qualify.scores import score

from tests import postings

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PROFILE = load_profile(FIXTURES / "synthetic_profile.yaml")
NOW = "2026-10-10T12:00:00.000Z"


def job(title, text, location="Remote - US", workplace=None, employment=None, posted="2026-10-10T06:00:00.000Z",
        employer="Synthetic Health", parent=None):
    remote = detect_remote(location, workplace, text)
    comp = parse_comp(text) or {}
    return {"title": title, "title_norm": norm(title), "family": classify_family(norm(title), POLICY.get("families")),
            "description_text": text, "location_raw": location, "remote_type": remote,
            "remote_states": remote_states(location, text, remote), "country": detect_country(location, text, remote),
            "employment_type": employment_type(employment, text), "comp_min": comp.get("min"), "comp_max": comp.get("max"),
            "comp_period": comp.get("period"), "posted_at": posted, "employer_name": employer, "parent": parent or employer}


def run(j):
    reqs = extract(j["description_text"], POLICY).requirements
    gate = evaluate(j, reqs, PROFILE, POLICY)
    return reqs, gate


class NormalizeTests(unittest.TestCase):
    def test_greenhouse_escaped_html_and_invisible_chars(self):
        raw = "&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;SQL\u200b&amp;nbsp;skills&lt;/li&gt;&lt;/ul&gt;"
        self.assertEqual(html_to_text(raw), "Requirements\n\n- SQL skills")

    def test_family_and_exclusions(self):
        fam = POLICY.get("families")
        self.assertEqual(classify_family(norm("Sr. Business Systems Analyst"), fam), "healthcare_bsa")
        self.assertEqual(classify_family(norm("Power Platform Business Analyst"), fam), "power_platform_automation")
        self.assertIsNone(classify_family(norm("Software Engineer II"), fam))
        self.assertEqual(excluded_term(norm("Director, Business Systems"), POLICY.get("title_exclude")), "director")

    def test_remote_states_country_comp_type(self):
        self.assertEqual(remote_states("Remote - US", postings.GOOD_FIT, "remote"), ["AZ", "FL", "GA", "TX"])
        self.assertEqual(detect_country("Remote - US", "", "remote"), "US")
        self.assertEqual(detect_country("London, UK", "", "onsite"), "non_US")
        self.assertEqual(parse_comp("Pay: $95,000 - $120,000 per year"), {"min": 95000, "max": 120000, "period": "year", "currency": "USD"})
        self.assertEqual(parse_comp("$40/hr - $50/hr")["period"], "hour")
        self.assertEqual(parse_comp("$45 to $55 per hour")["max"], 55)
        self.assertEqual(parse_comp("$85K–$110K")["min"], 85000)
        self.assertEqual(employment_type(None, "This is a 6 month contract-to-hire role"), "contract_to_hire")
        self.assertEqual(canonical_url("HTTPS://Jobs.Lever.co/acme/123/?lever-source=x#top"), "https://jobs.lever.co/acme/123")
        # Identifying parameters survive; tracking parameters do not (live bug: gh_jid stripped => false duplicates).
        self.assertEqual(canonical_url("https://acme.com/careers/job?gh_jid=81&utm_source=x&gh_src=y"), "https://acme.com/careers/job?gh_jid=81")
        self.assertNotEqual(canonical_url("https://acme.com/careers/job?gh_jid=81"), canonical_url("https://acme.com/careers/job?gh_jid=82"))
        self.assertEqual(detect_country("Dhaka, Bangladesh", "", "onsite"), "non_US")

    def test_requirements_fingerprint_tolerates_missing_years(self):
        from careeros.extract.model import Extraction, Requirement
        ex = Extraction("t", [Requirement("skill", "sql", "hard_required", "a", 0.9, None),
                              Requirement("skill", "sql", "hard_required", "b", 0.9, 3.0)], [], [])
        self.assertEqual(len(ex.requirements_fingerprint()), 64)  # live bug: None vs float comparison crashed

    def test_lever_full_html_preferred_over_truncated_plain_text(self):
        from careeros.discovery.boards import lever_posting
        item = {"id": "x", "text": "Process Analyst", "descriptionPlain": "The Role",
                "description": "<div>The Role</div><div>Requirements:</div><ul><li>3+ years of SQL</li></ul>", "lists": []}
        self.assertIn("3+ years of SQL", lever_posting("acme", item).description_text)


class ExtractTests(unittest.TestCase):
    def test_structured_posting(self):
        ex = extract(postings.GOOD_FIT, POLICY)
        found = {(r.category, r.name, r.requirement_type, r.years) for r in ex.requirements}
        self.assertIn(("skill", "sql", "hard_required", 3.0), found)
        self.assertIn(("education", "bachelor", "required_with_equivalency", None), found)
        self.assertIn(("skill", "rxclaim", "hard_required", None), found)
        self.assertIn(("skill", "power bi", "preferred", None), found)
        self.assertIn(("certification", "cbap", "preferred", None), found)
        self.assertTrue(any(r.requirement_type == "responsibility" for r in ex.requirements))
        for r in ex.requirements:  # every record quotes the posting verbatim
            self.assertIn(r.source_text, postings.GOOD_FIT)

    def test_unstructured_posting_is_low_confidence(self):
        ex = extract(postings.FLAT, POLICY)
        self.assertIn("no_requirement_headings", ex.notes)
        self.assertTrue(ex.requirements and all(r.confidence < 0.7 for r in ex.requirements))

    def test_benefits_section_is_not_a_requirement(self):
        text = "Requirements:\n- 3+ years of SQL\n- Bachelor's degree\nPerks & Benefits:\n- Medical, dental and claims reimbursement"
        found = {(r.category, r.name) for r in extract(text, POLICY).requirements}
        self.assertNotIn(("domain", "claims"), found)  # live bug: benefits heading read as a claims requirement
        self.assertIn(("skill", "sql"), found)

    def test_ambiguous_section_never_becomes_preferred(self):
        ex = extract("Qualifications:\n- Experience with Facets\n- Bachelor's degree", POLICY)
        self.assertTrue(all(r.requirement_type == "hard_required" for r in ex.requirements))


class GateTests(unittest.TestCase):
    def test_good_fit_is_eligible(self):
        _, gate = run(job("Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT))
        self.assertEqual(gate.decision, "eligible", gate.hard_failures)

    def test_hard_failures_each_explained(self):
        _, gate = run(job("Business Analyst", postings.HARD_FAIL, location="Remote"))
        codes = {f["code"] for f in gate.hard_failures}
        self.assertEqual(gate.decision, "rejected")
        self.assertTrue({"clearance_required", "certification_required", "state_not_eligible"} <= codes, codes)
        self.assertTrue(all(f["detail"] for f in gate.hard_failures))

    def test_scam_title_exclusion_location_and_pay_floor(self):
        self.assertIn("scam_signals", {f["code"] for f in run(job("Data Analyst", postings.SCAM))[1].hard_failures})
        self.assertIn("title_excluded", {f["code"] for f in run(job("Director, Business Systems", postings.GOOD_FIT))[1].hard_failures})
        self.assertIn("outside_commute", {f["code"] for f in run(job("Business Analyst", postings.FLAT, location="Dallas, TX", workplace="onsite"))[1].hard_failures})
        low_pay = postings.GOOD_FIT.replace("$95,000 - $120,000", "$55,000 - $70,000")
        self.assertIn("below_pay_floor", {f["code"] for f in run(job("Business Systems Analyst", low_pay))[1].hard_failures})

    def test_unknown_profile_facts_are_flags_not_passes(self):
        from careeros.qualify.profile import Profile
        blank = Profile({"status": "draft", "skills": [], "functions": []}, "x")
        reqs = extract(postings.GOOD_FIT, POLICY).requirements
        gate = evaluate(job("Business Systems Analyst", postings.GOOD_FIT), reqs, blank, POLICY)
        self.assertIn("years_unknown", {f["code"] for f in gate.flags})
        self.assertIn("education_unknown", {f["code"] for f in gate.flags})

    def test_remote_only_policy(self):
        import json as _json
        from careeros.core.fingerprint import fingerprint as _fp
        from careeros.core.policy import PolicyBundle
        data = _json.loads(_json.dumps(POLICY.data))
        data["location"]["remote_only"] = True
        remote_only = PolicyBundle(data, _fp(data))
        reqs = extract(postings.GOOD_FIT, remote_only).requirements
        hybrid = job("Business Systems Analyst", postings.GOOD_FIT, location="Austin, TX", workplace="hybrid")
        self.assertIn("not_remote", {f["code"] for f in evaluate(hybrid, reqs, PROFILE, remote_only).hard_failures})
        unknown = job("Business Systems Analyst", postings.GOOD_FIT, location="Florida")
        unknown["remote_type"] = "unknown"
        result = evaluate(unknown, reqs, PROFILE, remote_only)
        self.assertEqual(result.decision, "eligible")
        self.assertIn("remote_unknown", {f["code"] for f in result.flags})

    def test_certification_matching_is_specific(self):
        from careeros.qualify.profile import Profile
        p = Profile({"certifications": [{"name": "Lean Six Sigma Green Belt", "matches": ["lean six sigma"]}, {"name": "PL-900"}]}, "x")
        self.assertTrue(p.has_certification("six sigma"))
        self.assertTrue(p.has_certification("lean six sigma"))
        self.assertTrue(p.has_certification("pl-900"))
        self.assertFalse(p.has_certification("pl-200"))
        self.assertFalse(p.has_certification("pmp"))
        reqs = extract("Requirements:\n- Lean Six Sigma certification required", POLICY).requirements
        self.assertEqual({r.name for r in reqs if r.category == "certification"}, {"family:lean_six_sigma"})   # 2026-10-15: a family
        from careeros.qualify.certifications import analyse
        record = analyse(reqs, p, POLICY)[0]
        self.assertEqual((record["match_type"], record["satisfied"], record["decision_effect"]), ("family_match", True, "requirement_satisfied"))

    def test_training_only_skills_are_partial_coverage(self):
        from careeros.qualify.profile import Profile
        from careeros.qualify.scores import eligibility
        reqs = extract("Requirements:\n- SQL experience required\n- Excel experience required", POLICY).requirements
        j = job("Business Systems Analyst", "x")
        direct = Profile({"skills": [{"name": "sql", "evidence": "direct"}, {"name": "excel", "evidence": "direct"}]}, "d")
        trained = Profile({"skills": [{"name": "sql", "evidence": "adjacent"}, {"name": "excel", "evidence": "direct"}]}, "t")
        g = evaluate(j, reqs, direct, POLICY)
        self.assertEqual(eligibility(j, reqs, direct, g, POLICY)["dims"]["skills_coverage"]["value"], 1.0)
        self.assertEqual(eligibility(j, reqs, trained, g, POLICY)["dims"]["skills_coverage"]["value"], 0.7)

    def test_manager_roles_rejected_for_mid_level_profile(self):
        from careeros.qualify.profile import Profile
        mid = Profile({**PROFILE.data, "current_level": "mid"}, "mid")
        reqs = extract(postings.GOOD_FIT, POLICY).requirements
        manager = evaluate(job("Manager, Business Systems Analysis", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertIn("seniority_gap", {f["code"] for f in manager.hard_failures})
        senior = evaluate(job("Senior Business Systems Analyst", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertNotIn("seniority_gap", {f["code"] for f in senior.hard_failures})

    def test_function_matching_allows_partial_wording_for_long_names(self):
        from careeros.qualify.profile import Profile
        p = Profile({"functions": [{"name": "pbm operations"}, {"name": "claims adjudication research"}]}, "f")
        self.assertEqual(p.function("experience in PBM operations or pharmacy benefits")["name"], "pbm operations")
        self.assertEqual(p.function("claims adjudication experience")["name"], "claims adjudication research")
        self.assertIsNone(p.function("operations management"))  # short names need every word

    def test_experience_area_names(self):
        from careeros.extract.rules import _area
        self.assertEqual(_area("claims experience required"), "claims")
        self.assertEqual(_area("experience as a business systems analyst in healthcare"), "business systems analyst in healthcare")
        self.assertEqual(_area("experience working with pharmacy benefits is preferred"), "pharmacy benefits")
        self.assertEqual(_area("experience"), "professional experience")

    def test_domain_years_satisfy_domain_year_requirements(self):
        from careeros.qualify.profile import Profile
        p = Profile({"domains": [{"name": "claims", "years": 3.9}], "skills": [], "functions": []}, "d")
        self.assertEqual(p.years_for("claims"), 3.9)
        reqs = extract("Requirements:\n- 3+ years of claims experience required", POLICY).requirements
        gate = evaluate(job("Claims Analyst", "x"), reqs, p, POLICY)
        self.assertFalse({"years_unknown", "years_gap"} & {f["code"] for f in gate.flags + gate.hard_failures})

    def test_screening_years_override_only_screening(self):
        from careeros.qualify.profile import Profile
        p = Profile({"domains": [{"name": "claims", "years": 3.9, "screening_years": 4}], "skills": [], "functions": []}, "s")
        self.assertEqual(p.years_for("claims"), 4.0)
        self.assertEqual(p.data["domains"][0]["years"], 3.9)  # the fact is untouched
        reqs = extract("Requirements:\n- 4+ years of claims experience required", POLICY).requirements
        gate = evaluate(job("Claims Analyst", "x"), reqs, p, POLICY)
        self.assertFalse({"years_slightly_short", "years_gap", "years_unknown"} & {f["code"] for f in gate.flags})

    def test_held_employer_is_flagged_not_rejected(self):
        _, gate = run(job("Business Systems Analyst", postings.GOOD_FIT, employer="Held Parent Co"))
        self.assertEqual(gate.decision, "eligible")
        self.assertIn("held_d13", {f["code"] for f in gate.flags})


class ScoreTests(unittest.TestCase):
    def test_good_fit_ranks_high_and_components_explain(self):
        j = job("Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT)
        reqs, gate = run(j)
        card = score(j, reqs, PROFILE, gate, POLICY, NOW)
        self.assertIn(card.tier, ("A", "B"), card)
        self.assertGreaterEqual(card.eligibility, 85)
        self.assertEqual(card.freshness, 100.0)
        self.assertIn("sql", card.components["eligibility"]["matched_skills"])
        weights = POLICY.get("weights")
        for name in ("eligibility", "candidate_fit", "career_value"):
            self.assertEqual(sum(weights[name].values()), 100)

    def test_unknowns_are_neutral_not_zero(self):
        j = job("Business Systems Analyst", "Responsibilities:\n- Work with teams", posted=None)
        reqs, gate = run(j)
        card = score(j, reqs, PROFILE, gate, POLICY, NOW)
        self.assertEqual(card.freshness, 50.0)
        self.assertTrue(card.components["career_value"]["dims"]["compensation"]["unknown"])

    def test_tiers_require_eligible_state_and_skill_minimum_not_just_priority(self):
        from careeros.qualify.scores import tier_for
        self.assertEqual(tier_for(95, "ELIGIBLE", 0.9, POLICY), "A")
        self.assertEqual(tier_for(95, "CONDITIONAL", 0.9, POLICY), "near_miss")  # listed as Conditional priority
        self.assertEqual(tier_for(95, "HOLD", 1.0, POLICY), "near_miss")
        self.assertEqual(tier_for(95, "ELIGIBLE", 0.5, POLICY), "near_miss")  # your 70% skill minimum
        self.assertEqual(tier_for(88, "ELIGIBLE", 0.8, POLICY), "B")
        self.assertEqual(tier_for(70, "ELIGIBLE", 1.0, POLICY), "archive")

    def test_stale_posting_scores_lower_priority(self):
        fresh = job("Senior Business Systems Analyst", postings.GOOD_FIT)
        stale = job("Senior Business Systems Analyst", postings.GOOD_FIT, posted="2026-08-01T00:00:00.000Z")
        r1, g1 = run(fresh)
        r2, g2 = run(stale)
        self.assertGreater(score(fresh, r1, PROFILE, g1, POLICY, NOW).apply_priority,
                           score(stale, r2, PROFILE, g2, POLICY, NOW).apply_priority)


if __name__ == "__main__":
    unittest.main()
