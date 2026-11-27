"""The scoring loop (2026-11-03, D90-D94): years as a role, in a function or doing an activity are met only by years in
it (OPP-43, qualify/role_years.py), and the disagreements between your labels or dismissals and the system (OPP-20).
Synthetic data only: no real person, employer relationship or posting."""
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from careeros import cli
from careeros.extract.rules import _line_records, extract, mentions_years
from careeros.normalize.sanitize import html_to_text
from careeros.qualify import experience as exp
from careeros.qualify import role_years, skill_groups
from careeros.qualify import scores as score_mod
from careeros.qualify.facts import eligibility_facts
from careeros.qualify.gate import GateResult, requirement_rules, state_for
from careeros.qualify.profile import Profile
from careeros.qualify.scores import REQUIRED_TYPES, score
from careeros.review.labels import class_now, disagreements, render_disagreements

from tests.helpers import add_job, new_store
from tests.test_qualify import job
from tests.test_v25_audit import APPROVED, MID, assess, failures, flags, gate, posting

# A synthetic persona: four years of healthcare and PBM operations, a few months under an analyst title (0.3 years,
# counted as 0.5 for screening), no years recorded for leading teams or for billing.
PERSONA = Profile(data={
    **MID.data, "total_professional_years": 4, "equivalences": [],
    "functions": [{"name": "pbm operations", "years": 3.7, "evidence": "direct"},
                  {"name": "claims adjudication research", "years": 3.7, "evidence": "direct"},
                  {"name": "business systems analysis", "years": 0.3, "evidence": "direct"},
                  {"name": "customer service", "years": 5.7, "evidence": "direct"},
                  {"name": "team leadership", "evidence": "adjacent"}],
    "skills": [{"name": "excel", "years": 10, "evidence": "direct"}, {"name": "sql", "years": 3, "evidence": "direct"},
               {"name": "reconciliation", "years": 0.3, "evidence": "direct"},
               {"name": "data validation", "years": 0.3, "evidence": "direct"}],
    "domains": [{"name": "healthcare", "years": 4}, {"name": "pbm", "years": 4}, {"name": "claims", "years": 4}],
    "role_titles": [{"name": "business systems analyst", "years": 0.3, "screening_years": 0.5, "evidence": "direct"},
                    {"name": "analyst", "exact": True, "years": 0.3, "screening_years": 0.5, "evidence": "direct"},
                    {"name": "customer service representative", "years": 3.7, "evidence": "direct"}]}, fingerprint="persona-43")
TITLE = "Business Systems Analyst"


def judged(*required, profile=PERSONA, preferred=()):
    return gate(TITLE, posting(required=list(required), preferred=list(preferred)), profile=profile)


def detail(result, code):
    return next(f["detail"] for f in result.flags + result.hard_failures if f["code"] == code)


def read(line, years):
    return role_years.read(line, years, APPROVED)


# --- OPP-43: reading the line ---------------------------------------------------------------------------------------
class ReadingTests(unittest.TestCase):
    def test_roles(self):
        bsa = read("- 2+ years as a Business Systems Analyst, preferably in insurance or SaaS", 2)
        self.assertEqual((bsa.kind, bsa.area), ("role", "business systems analyst"))
        self.assertEqual(read("- 2+ years of experience working in an analyst role", 2).area, "analyst")
        mixed = read("- 3-5 years of experience in a business analyst, analytics, or data-focused role", 3)
        self.assertEqual((mixed.kind, [a for a, _ in mixed.alternatives]), ("role", ["business analyst", "analytics", "data-focused"]))
        similar = read("- 5 years of experience in healthcare analytics, cost containment, or similar roles in the healthcare "
                       "industry", 5)
        self.assertEqual([a for a, _ in similar.alternatives], ["healthcare analytics", "cost containment"])   # "similar": nothing

    def test_functions_and_activities(self):
        billing = read("A minimum of 5 years of progressively responsible experience in healthcare billing, with demonstrable "
                       "expertise in remittance reconciliation.", 5)
        self.assertEqual((billing.kind, billing.area), ("function", "healthcare billing"))
        self.assertEqual(read("- 3+ years of payment integrity experience at a health plan", 3).area, "payment integrity")
        lead = read("- 5+ years leading cross-functional teams in a matrixed environment", 5)
        self.assertEqual((lead.kind, lead.area), ("activity", "leading cross-functional teams"))
        self.assertEqual(read("- 4+ years of experience analyzing prescription and medical claims data", 4).kind, "activity")

    def test_lines_that_name_no_role_function_or_activity_are_left_alone(self):
        for line, years in (("- 3+ years of progressive healthcare experience", 3),
                            ("- 3-5 years of experience in PBM, managed care, or health plan environments", 3),
                            ("- 1+ years of experience in the pharmacy industry", 1),
                            ("- 5+ years of experience with SQL and Excel", 5),
                            ("- 3+ years of relevant experience", 3),
                            ("- A bachelor's degree or 4+ years of commensurate experience", 4),
                            ("- 2+ years of experience in a similar role", 2),
                            ("- 3+ years of claims experience; in this role you will lead audits", 3),
                            ("- 3+ years working with health plans and providers", 3),
                            ("- 1+ years experience, including proficiency in Google Sheets or Excel", 1)):
            self.assertIsNone(read(line, years), line)

    def test_slash_and_and_never_split_an_alternative(self):
        line = read("- 5+ years of experience in payment operations, financial services, or healthcare claims/insurance operations", 5)
        self.assertIn(("healthcare claims/insurance operations", "specific"), line.alternatives)
        self.assertIn(("financial services", "domain"), line.alternatives)
        # "and" never makes alternatives: the two are one alternative, met only when both are (D123's check: "all")
        self.assertEqual(read("- 2+ years of experience in process improvement and workflow design", 2).alternatives,
                         (("process improvement and workflow design", "all"),))

    def test_such_as_and_as_well_as_name_no_role(self):
        self.assertNotEqual(read("- 2+ years of experience with CRM tools such as a ticketing system", 2).kind, "role")
        self.assertIsNone(read("- 3+ years of claims experience as well as a passion for service", 3))

    def test_a_role_is_read_only_where_the_area_starts(self):
        # pre-review #2 (2026-11-04): a role named after the area, or after a cut word, is a preference or an aside
        for line in ("- 5+ years of healthcare experience, preferably as a business analyst",
                     "- 5+ years of healthcare experience, including time as a Business Analyst",
                     "- 5+ years of healthcare experience, preferably in a business analyst role",
                     "- 5+ years of experience in healthcare or PBM, preferably as an analyst",
                     "- 5+ years of healthcare experience with exposure as a business analyst",
                     "- 5+ years of healthcare experience; experience as a Business Systems Analyst is a must",
                     "- 5+ years of healthcare experience working both independently and as a team member"):
            self.assertIsNone(read(line, 5), line)
        self.assertEqual(read("- 5+ years of experience serving as a business analyst", 5).area, "business analyst")
        self.assertEqual(read("- 2+ years of experience in a similar role as a Business Analyst", 2).area, "business analyst")
        billing = read("- 5+ years of experience in healthcare billing, ideally in an analyst role", 5)
        self.assertEqual((billing.kind, billing.area), ("function", "healthcare billing"))   # never read past "ideally"
        # Where the area starts with "in this role", no role is named (the "; in this role" line above the cut no longer
        # reaches this check, so these lines guard it)
        for line, years in (("- 2+ years in this role", 2), ("- 3+ years in the same role", 3), ("- 3+ years in your role", 3)):
            self.assertNotEqual(getattr(read(line, years), "kind", None), "role", line)

    def test_or_in_offers_an_alternative_and_in_names_the_place(self):
        # pre-review #5 (2026-11-04)
        either = read("- 5+ years as a Business Systems Analyst or in a health plan environment", 5)
        self.assertEqual((either.alternatives[-1], either.place), (("in a health plan environment", "domain"), ""))
        self.assertEqual(read("- 9+ years of experience as a business systems analyst in healthcare", 9).place, "healthcare")

    def test_each_figure_on_a_line_is_read_on_its_own(self):
        line = "- 10+ years of experience in services implementation, including 5+ years of vendor-side ownership of implementations"
        self.assertEqual(read(line, 10).area, "services implementation")
        self.assertEqual(read(line, 5).area, "vendor-side ownership of implementations")


# --- OPP-43: the gate ---------------------------------------------------------------------------------------------
class GateTests(unittest.TestCase):
    def test_years_as_a_role_count_only_years_in_that_role(self):
        result = judged("2+ years as a Business Systems Analyst required")   # read as "professional experience" until now
        self.assertIn("years_slightly_short", flags(result))
        self.assertIn("2+ years as business systems analyst; you have 0.5", detail(result, "years_slightly_short"))

    def test_the_role_title_counts_over_a_smaller_function_of_the_same_name(self):
        result = judged("3+ years as a Business Systems Analyst required")
        # the title (0.5), not the function (0.3); since D151 a role's gap is to verify, never a years_gap
        self.assertIn("(you have 0.5)", detail(result, "years_role_unconfirmed"))

    def test_an_analyst_role_uses_the_analyst_wording_you_confirmed(self):
        self.assertIn("you have 0.5", detail(judged("2+ years of experience working in an analyst role required"),
                                            "years_slightly_short"))

    def test_domain_years_never_stand_in_for_a_function(self):
        result = judged("5+ years of experience in healthcare billing required")
        self.assertIn("years_role_unconfirmed", flags(result))
        self.assertNotIn("years_unknown", flags(result))
        self.assertEqual((result.decision, state_for(result, APPROVED)), ("eligible", "CONDITIONAL"))
        self.assertIn("in healthcare billing", detail(result, "years_role_unconfirmed"))
        self.assertIn("the 4 years counted for it before (a domain, a tool or your total years)",
                      detail(result, "years_role_unconfirmed"))

    def test_an_unconfirmed_role_is_never_a_rejection(self):
        result = judged("9+ years of experience in healthcare billing required")
        self.assertNotIn("years_far_short", failures(result))
        self.assertIn("years_role_unconfirmed", flags(result))

    def test_a_known_small_credit_far_short_is_still_rejected_and_explained(self):
        # D149 R-Y0 (the follow-up; the name is kept for the defect records that cite it): years in a role never reject
        # on their own; far short, they are CONDITIONAL, the flag names the role and your years in it, and Experience
        # Match reads the line as the gate does (until then: years_far_short, with the same Experience Match)
        line = "5+ years as a Business Systems Analyst required"
        result = judged(line)
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertTrue(detail(result, "years_role_unconfirmed").startswith(
            "5+ years as business systems analyst required (you have 0.5); a shortfall in a role"))
        reading, _structure, counted = decisive(f"- {line}")
        self.assertEqual((reading, counted), ("role", 0.5))

    def test_a_domain_the_line_offers_as_an_alternative_still_counts(self):
        result = judged("3+ years of experience in PBM operations, managed care, or health plan environments required")
        self.assertFalse({c for c in flags(result) if c.startswith("years")})   # PBM operations 3.7 or the domain, 4

    def test_a_domain_alone_can_answer_the_line(self):
        result = judged("3+ years of experience in claims auditing or health plan environments required")
        self.assertFalse({c for c in flags(result) if c.startswith("years")})   # nothing names claims auditing; health plans, 4
        # D123 R2 (2026-11-09; until then it was met by your domain years, pre-review #5): a domain offered beside a role
        # is an alternative to verify, never a pass on your domain years
        result = judged("5+ years as a Business Systems Analyst or in a health plan environment required")
        self.assertEqual((state_for(result, APPROVED), years_codes(result)), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertIn("nothing in your record confirms in a health plan environment", detail(result, "years_role_unconfirmed"))

    def test_a_role_figure_beside_a_skill_figure_is_judged(self):
        # Fable review #4 (2026-11-04, D113): the skill takes its own figure, and the role's figure is judged as well. On
        # a skill line such a figure flags and never rejects on its own (the final fix-check, D115): it may be the
        # skill's own figure, handed elsewhere by the nearest rule, or no demand at all
        aliases, domains = APPROVED.get("skill_aliases", default={}), APPROVED.get("domain_terms", default={})

        def records(line):
            return sorted((r.category, r.name, r.years) for r in _line_records(line, "hard_required", 0.9, aliases, domains))
        for line in ("5+ years as a Business Analyst, including 2+ years with SQL",
                     "5+ years as a Business Systems Analyst, including 2+ years with SQL",
                     "2+ years of SQL and 5+ years as a Business Analyst",
                     "5+ years as a Business Analyst; 2+ years of SQL"):
            with self.subTest(line=line):
                self.assertEqual(records(line), [("skill", "sql", 2.0), ("years_experience", "professional experience", 5.0)])
                result = judged(line)
                self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
                self.assertTrue(detail(result, "years_role_unconfirmed").startswith("5+ years as business"))
        # A figure whose own phrase names a skill on the line is that skill's, and an age is no demand: no record
        for line in ("10+ years with Excel and 2+ years with SQL", "9+ years of hands-on experience with Excel and 2+ years with SQL",
                     "Must be at least 18 years old, with 2+ years of SQL", "Applicants must be 21 years or older; 2+ years with SQL"):
            with self.subTest(line=line):
                self.assertNotIn("years_experience", {category for category, _, _ in records(line)})
                # D149 R-Y2: an age is no demand (no flag); a skill's own figure that no record carries (the nearest-figure
                # rule gave Excel the 2, so the 10 was never judged) is to verify, never a silent pass
                self.assertEqual(years_codes(judged(line)), set() if "old" in line else {"years_role_unconfirmed"})
        # Any other figure such a line adds is a flag at most ("live for 12 years")
        self.assertEqual(failures(judged("2+ years of SQL; our platform has been live for 12 years")), set())
        # a figure a domain or a skill already carries gets no second record, and a total figure is judged on total years
        self.assertEqual(records("5+ years in healthcare, including 2+ years with SQL"),
                         [("domain", "healthcare", 5.0), ("skill", "sql", 2.0)])
        self.assertEqual(records("3+ years of SQL and 2+ years of Excel"), [("skill", "excel", 2.0), ("skill", "sql", 3.0)])
        self.assertIn("5+ years of professional experience; you have 4",
                      detail(judged("5+ years of experience, including 2+ years with SQL"), "years_slightly_short"))
        # Figures joined by "or" are alternatives (the fix-check of ff3c7f7, D113): judged once, on the figure you come
        # closest to, with a skill on the line or without one. A figure of another kind than the role (a skill, a
        # function) never meets the role's figure (D123 R2): met (SQL 3, PBM operations 3.7), it flags at most, and
        # enough years in the role still pass
        for line in ("5+ years as a Business Systems Analyst or 3+ years of hands-on SQL",
                     "5+ years as a Business Analyst, or at least 3 years with SQL",
                     "5+ years as a Business Systems Analyst or 3+ years in PBM operations"):
            with self.subTest(line=line):
                self.assertEqual((state_for(judged(line), APPROVED), years_codes(judged(line))),
                                 ("CONDITIONAL", {"years_role_unconfirmed"}))
                self.assertEqual(failures(judged(line)), set())
        self.assertEqual(years_codes(judged("5+ years as a Business Systems Analyst or 3+ years of hands-on SQL",
                                            profile=owner_of([("business systems analyst", 6)]))), set())
        # Far short of both: the line offers an alternative, so the role's shortfall is to verify (D123 R1; it rejected
        # on SQL until then)
        far = judged("9+ years as a Business Systems Analyst or 8+ years of hands-on SQL")
        self.assertEqual((state_for(far, APPROVED), years_codes(far)), ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_figures_offered_as_alternatives(self):
        # The fix-check of f74cdcc (D113): any joiner that only qualifies the next figure, with or without a skill. The
        # SQL 3 met is of another kind than the role, so it flags at most (round 3, INV-1): never a pass, never a rejection
        for line in ("5+ years as a Business Analyst or more than 3 years with SQL",
                     "5+ years as a Business Analyst or, alternatively, 3+ years with SQL",
                     "5+ years as a Business Analyst or min. 3 years with SQL",
                     "5+ years as a Business Analyst / 3+ years with SQL",
                     "5+ years as a Business Analyst and/or 3+ years of SQL"):
            with self.subTest(line=line):
                self.assertEqual((failures(judged(line)), years_codes(judged(line))), (set(), {"years_role_unconfirmed"}))
        # An alternative nothing in your record names is CONDITIONAL, never beaten by one that rejects
        for line in ("5+ years as a Business Systems Analyst or 3+ years in a Scrum Master role",
                     "5+ years as a Business Systems Analyst or 3+ years as a Product Owner",
                     "5+ years as a Business Systems Analyst or 3+ years as a Product Owner with SQL",
                     "5+ years as a Business Analyst or 3+ years as a Product Owner, including 2+ years with SQL"):
            with self.subTest(line=line):
                result = judged(line)
                self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
                self.assertNotIn("years_conflicting", flags(result))   # two figures on one line: alternatives
        # Three figures: the BA's 5 stands alone, and the Data Analyst's 3 or the other's 2 is met by it. Both role figures
        # are kept, however they are named. Without a skill on the line the BA's 5 rejects; beside a skill it flags
        line = "5+ years as a Business Analyst, 3+ years as a Data Analyst or 2+ years with SQL"
        self.assertEqual(sorted(r.years for r in extract(f"Requirements:\n- {line}\n", APPROVED).requirements if r.years),
                         [2.0, 3.0, 5.0])
        self.assertEqual((failures(judged(line)), detail(judged(line), "years_role_unconfirmed")[:30]),
                         (set(), "5+ years as business analyst r"))
        # ... and the line holds an "or", so the BA's far shortfall is to verify, never a rejection (D123 R1)
        three = judged("5+ years as a Business Systems Analyst, 3+ years as a Data Analyst or 2+ years in PBM operations")
        self.assertEqual((failures(three), detail(three, "years_role_unconfirmed")[:45]),
                         (set(), "5+ years as business systems analyst required"))
        # One figure named twice as alternatives is one record: it flags, never rejects on its own (D115)
        for line in ("5+ years as a Business Systems Analyst or 5+ years with SQL",
                     "5+ years as a Business Systems Analyst or 5+ years as a Product Owner",
                     "5+ years as a Business Systems Analyst or 5+ years in PBM operations"):
            with self.subTest(line=line):
                self.assertEqual((state_for(judged(line), APPROVED), failures(judged(line))), ("CONDITIONAL", set()))
        # An "or" a few words before a figure that names no known joiner: the shortfall flags, never rejects alone;
        # an "and" after the "or" ends that, and so does a semicolon
        loose = judged("5+ years as a Business Systems Analyst or similar, 3+ years in PBM operations")
        self.assertEqual((failures(loose), "the line may offer an" in detail(loose, "years_role_unconfirmed")),
                         (set(), True))
        # An "or" anywhere on a role's line is a cue of an alternative (D123 R1): the far shortfall flags; without it, it
        # rejected until D149 R-Y0, and now flags too (a role's years never reject on their own)
        for line in ("3+ years of claims or billing experience and 9+ years as a Business Systems Analyst",
                     "5+ years as a Business Systems Analyst or similar; 3+ years in claims auditing",
                     "3+ years of claims experience and 9+ years as a Business Systems Analyst",
                     "5+ years as a Business Systems Analyst; 3+ years in claims auditing"):
            with self.subTest(line=line):
                self.assertNotIn("years_far_short", failures(judged(line)))
                self.assertIn("years_role_unconfirmed", flags(judged(line)))
        # A second figure under one name, which the duplicate check used to drop, flags but never rejects on its own;
        # the first keeps its rejection
        # (since D151 the second figure's own words, "leads bring", are no plain demand: to verify either way)
        levels = judged("Senior analysts bring 3+ years of experience, and leads bring 9+ years")
        self.assertEqual((failures(levels), state_for(levels, APPROVED)), (set(), "CONDITIONAL"))
        self.assertIn("years_far_short", failures(judged("9+ years overall experience, including 2+ years of management experience")))

    def test_a_role_named_after_the_area_never_rejects(self):
        # pre-review #2 (2026-11-04): healthcare years are asked for; "preferably as a ..." is not the requirement
        result = judged("5+ years of healthcare experience, preferably as a Business Systems Analyst")
        self.assertNotIn("years_far_short", failures(result))
        # D151 (a): the role named beside it makes the line no plain demand: to verify, on the 4 healthcare years
        self.assertIn("(4 years counted for it", detail(result, "years_role_unconfirmed"))
        self.assertNotIn("years_role_unconfirmed", flags(judged("5+ years of healthcare experience acting as the primary point "
                                                                "of contact")))

    def test_the_items_of_a_list_are_matched_one_by_one(self):
        result = judged("5+ years supporting finance organizations, finance systems, business applications, or a related "
                        "area required")
        self.assertIn("years_role_unconfirmed", flags(result))   # never "business systems analysis" from two items' words
        self.assertNotIn("years_far_short", failures(result))
        # nor a pass on it, though that function holds the years (Codex F4: the two items never confirm it)
        analyst = Profile(data={**PERSONA.data, "functions": [{"name": "business systems analysis", "years": 6, "evidence": "direct"}]},
                          fingerprint="items-analyst")
        self.assertIn("years_role_unconfirmed", flags(judged("5+ years supporting finance organizations, finance systems, "
                                                             "business applications, or a related area required", profile=analyst)))
        # each item is matched on its own: the whole list's words are never a credit, not even a partial one
        line = "- 5+ years supporting finance organizations, finance systems, business applications, or a related area required"
        records = _line_records(line, "hard_required", 0.9, ExperienceTests.ALIASES, ExperienceTests.DOMAINS)
        self.assertIsNone(role_years.line_credit([r for r in records if r.years == 5.0], read(line, 5), analyst, APPROVED))

    def test_generic_and_domain_lines_are_judged_as_before(self):
        self.assertFalse({c for c in flags(judged("3+ years of relevant experience required")) if c.startswith("years")})
        self.assertFalse({c for c in flags(judged("3+ years of progressive healthcare experience required")) if c.startswith("years")})

    def test_activities(self):
        self.assertIn("years_role_unconfirmed", flags(judged("5+ years leading cross-functional teams required")))
        self.assertIn("5+ years leading cross-functional teams", detail(judged("5+ years leading cross-functional teams required"),
                                                                      "years_role_unconfirmed"))

    def test_a_tool_the_area_names_counts_on_a_function_line(self):
        result = judged("2+ years of experience using data software such as Excel, SQL or other analytics tools required")
        # Excel's 10 years still credit the function (no shortfall flag), but since D155 (3) a figure whose own phrase
        # opens with something other than a skill ("data software ...") needs a record of its own that is no skill's:
        # to verify (a needless flag here, the cost of the rule)
        self.assertEqual(years_codes(result), {"years_role_unconfirmed"})
        self.assertIn("were not read as any requirement", detail(result, "years_role_unconfirmed"))

    def test_a_tool_named_elsewhere_on_the_line_does_not(self):
        result = judged("5 years of progressively responsible experience in healthcare billing, with demonstrable expertise "
                        "in remittance reconciliation required")
        self.assertIn("years_role_unconfirmed", flags(result))   # never reconciliation's 0.3 years
        self.assertIn("years_role_unconfirmed", flags(judged("5 years of progressively responsible experience in healthcare "
                                                             "billing, with demonstrable expertise in Excel required")))   # nor Excel's 10
        self.assertNotIn("years_far_short", failures(result))

    def test_a_role_line_never_takes_a_skill(self):
        result = judged("2+ years as a data analyst using SQL required")
        self.assertIn("years_role_unconfirmed", flags(result))   # SQL's 3 years are not years as a data analyst

    def test_a_line_unknown_until_now_stays_years_unknown(self):
        result = judged("2+ years in a software implementation role required")
        self.assertIn("years_unknown", flags(result))
        self.assertNotIn("years_role_unconfirmed", flags(result))

    def test_a_function_your_record_names_is_met(self):
        result = judged("3+ years of experience in customer service or provider relations required")
        self.assertFalse({c for c in flags(result) if c.startswith("years")})   # customer service, 5.7

    def test_preferred_lines_only_lower_the_score(self):
        result = judged(preferred=["5+ years of experience in healthcare billing preferred",
                                   "4+ years as a Business Systems Analyst preferred"])
        self.assertNotIn("years_role_unconfirmed", flags(result))
        self.assertIn("4+ years as business systems analyst preferred; you have 0.5", detail(result, "years_preferred_short"))

    def test_night_shares_the_rule(self):
        text = posting(required=["5+ years of experience in healthcare billing required"])
        result = GateResult("eligible")
        requirement_rules(result, extract(text, APPROVED).requirements, PERSONA, APPROVED)
        self.assertIn("years_role_unconfirmed", flags(result))

    def test_the_card_says_which_line_to_verify(self):
        text = posting(required=["5+ years of experience in healthcare billing required"])
        j = job(TITLE, text)
        reqs = extract(text, APPROVED).requirements
        result = gate(TITLE, text, profile=PERSONA)
        facts = {f["facet"]: f for f in eligibility_facts(j, reqs, result, APPROVED)}
        self.assertEqual(facts["qualifications"]["status"], "unknown")
        self.assertIn("healthcare billing", facts["qualifications"]["detail"])
        card = score(j, reqs, PERSONA, result, APPROVED, "2026-10-12T12:00:00.000Z")
        dim = card.components["eligibility"]["dims"]["years_seniority"]
        self.assertTrue(dim["unknown"])


# --- OPP-43: Experience Match reads the line the same way ---------------------------------------------------------------
class ExperienceTests(unittest.TestCase):
    ALIASES, DOMAINS = APPROVED.get("skill_aliases", default={}), APPROVED.get("domain_terms", default={})

    def block(self, line, profile=PERSONA):
        return exp.evaluate(_line_records(line, "hard_required", 0.9, self.ALIASES, self.DOMAINS), profile, APPROVED)

    def test_the_role_is_counted_not_the_domain(self):
        block = self.block("- 3+ years of experience in a business analyst, analytics, or data-focused role")
        line = block["lines"][block["decisive_line"]]
        self.assertEqual((line["area"], line["reading"], line["structure"], line["matched_item"]),
                         ("business analyst, analytics, or data-focused", "role", "or", "function: business systems analysis"))
        self.assertTrue(any(s.startswith("Read as years as a role") for s in exp.explain(block)))

    def test_a_role_in_a_domain_keeps_the_strictest(self):
        senior = Profile(data={**PERSONA.data, "role_titles": [{"name": "business systems analyst", "years": 6}]}, fingerprint="s")
        block = self.block("- 9+ years of experience as a business systems analyst in healthcare", senior)
        line = block["lines"][block["decisive_line"]]
        self.assertEqual((line["structure"], line["counted_years"]), ("and", 4.0))   # healthcare 4 of 9, not the title's 6
        # pre-review #6 (2026-11-04): an "or" inside the place keeps the AND reading, and the place counts as its best
        senior = Profile(data={**senior.data, "domains": [{"name": "healthcare", "years": 4}, {"name": "pbm", "years": 1}]},
                         fingerprint="s2")
        block = self.block("- 9+ years of experience as a business systems analyst in healthcare or PBM", senior)
        line = block["lines"][block["decisive_line"]]
        self.assertEqual((line["structure"], line["counted_years"]), ("and", 4.0))   # not the title's 6, nor PBM's 1

    def test_a_years_rejection_keeps_what_the_gate_judged(self):
        # pre-review #6 (2026-11-04): the role, never the domain the line also names; since D149 R-Y0 (the name is kept
        # for the defect records that cite it) the role's far shortfall is a flag to verify, which names the role and
        # your 0.5 years in it, and Experience Match counts the same (not the 4 healthcare years)
        line = "5+ years as a Business Systems Analyst in healthcare required"
        result = judged(line)
        self.assertEqual(failures(result), set())
        self.assertIn("as business systems analyst required (you have 0.5)", detail(result, "years_role_unconfirmed"))
        block = self.block(f"- {line}")
        found = block["lines"][block["decisive_line"]]
        self.assertEqual((found["reading"], found["counted_years"]), ("role", 0.5))
        # the rejection of a total-years demand still carries the line's Experience Match
        failure = next(f for f in judged("10+ years of professional experience required").hard_failures
                       if f["code"] == "years_far_short")
        self.assertEqual((failure["required_years"], failure["experience"]["counted_years"]), (10.0, 4.0))

    def test_nothing_in_your_record_is_unknown(self):
        block = self.block("- 5+ years of experience in healthcare billing")
        self.assertIsNone(block["decisive_line"])


# --- Fable review of 559212b (2026-11-04, D113): every spelling of one requirement is read and judged the same ---------
# Under D112 rule 1 a finding whose kind repeats is closed for the whole kind: each test below writes one requirement in
# every spelling of its kind and compares each with the reference spelling, reading, gate and Experience Match alike.
def years_codes(result):
    return {c for c in flags(result) | failures(result) if c.startswith("years")}


def decisive(line, profile=PERSONA):
    block = exp.evaluate(_line_records(line, "hard_required", 0.9, ExperienceTests.ALIASES, ExperienceTests.DOMAINS),
                         profile, APPROVED)
    found = block["lines"][block["decisive_line"]] if block["decisive_line"] is not None else {}
    return found.get("reading"), found.get("structure"), found.get("counted_years")


# Three synthetic records for the fail-safe below: the persona (a few months as a BSA, 4 healthcare years), one who meets
# every reading (6 years as a BSA and in each domain), and one who meets none (half a year of each).
SENIOR = Profile(data={**PERSONA.data, "role_titles": [{"name": "business systems analyst", "years": 6, "evidence": "direct"}],
                       "domains": [{"name": n, "years": 6} for n in ("healthcare", "pbm", "claims")]}, fingerprint="d113-senior")
NOVICE = Profile(data={**PERSONA.data, "domains": [{"name": n, "years": 0.5} for n in ("healthcare", "pbm", "claims")]},
                 fingerprint="d113-novice")
FIELDS = ("healthcare", "health plan", "claims", "PBM")
SEPARATORS = (" or", ", or", " and/or")
PLACES = ("in a health plan environment", "within a health plan", "for a health plan", "at a health plan")


class SpellingTests(unittest.TestCase):
    def outcome(self, line, profile=PERSONA):
        result = judged(line, profile=profile)
        return state_for(result, APPROVED), years_codes(result)

    def test_words_that_only_lead_into_the_place_read_as_or_in(self):
        # Fable #5 and its fix-check: "or working in", "or a career in", "or a history of working in" ... = "or in". The
        # place offered is an alternative to verify, never met by domain years (D123 R2; until then it passed on the 4
        # health plan years)
        reference = self.outcome("5+ years as a Business Systems Analyst or in a health plan environment")
        self.assertEqual(reference, ("CONDITIONAL", {"years_role_unconfirmed"}))
        for sep in SEPARATORS:
            for words in ("", " working", " experience", " relevant experience", " work experience", " prior experience working",
                          " a background", " time", " employed", " employment", " a career", " exposure",
                          " a history of working", " having worked", " hands-on experience", " industry experience",
                          ", alternatively,"):
                for place in PLACES:
                    line = f"5+ years as a Business Systems Analyst{sep}{words} {place}"
                    with self.subTest(line=line):
                        reading = read(f"- {line}", 5)
                        self.assertEqual(([k for _, k in reading.alternatives], reading.place, reading.doubt),
                                         (["specific", "domain"], "", ""))
                        self.assertEqual(self.outcome(line), reference)
        # a role named as a noun offers it the same way, to verify (D123 R2); a function's line is met by the domain;
        # claims auditing, which nothing in the record confirms, leaves the 4 health plan years to be verified (Codex F2)
        line = "5+ years in an analyst role or working in a health plan environment"
        self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertIn("health plan environment", detail(judged(line), "years_role_unconfirmed"))
        self.assertIn("you have 4", detail(judged("5+ years of experience in claims auditing or experience in a health plan "
                                                  "environment"), "years_role_unconfirmed"))

    def test_words_after_or_that_may_name_something_never_decide_alone(self):
        # The fix-check of ff3c7f7 (D113): "or similar in healthcare", "or a business analyst in healthcare", "or a stint
        # in ..." can be read with the domain as an alternative or as the place. Both readings are judged: where they
        # differ the line is CONDITIONAL (years_role_unconfirmed), never a rejection and never a plain pass. Since D123
        # (R1) the "or" itself makes a far shortfall CONDITIONAL whatever the readings say, and the domain offered is
        # never met by domain years (R2): only the role's own years pass.
        for sep in SEPARATORS:
            for words in (" similar", " related", " comparable", " other experience", " a similar position", " business analyst",
                          " a stint", " a tour of duty", " a long and demonstrated track record of working", " similar,",
                          " similar exp.", ",", " similar (e.g. BA)", " similar (BA, PO)", " a similar position where you worked",
                          " similar roles preferably", " comparable experience particularly", " (relevant) experience"):
                # any length, punctuation alone, parentheses, asides (fix-checks of 947e709 and da367fb, D115);
                # "or equivalent" makes a line soft: below
                for place in ("in healthcare", "within the healthcare industry", "in a health plan environment"):
                    line = f"5+ years as a Business Systems Analyst{sep}{words} {place}"
                    with self.subTest(line=line):
                        self.assertTrue(read(f"- {line}", 5).doubt)
                        result = judged(line)
                        self.assertEqual((state_for(result, APPROVED), years_codes(result)),
                                         ("CONDITIONAL", {"years_role_unconfirmed"}))
                        self.assertEqual(self.outcome(line, SENIOR), ("ELIGIBLE", set()))
                        self.assertEqual(self.outcome(line, NOVICE), ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_a_role_after_its_field_reads_as_that_role_and_never_decides_alone(self):
        # Fable #6: "<field> experience as a <role>", "experience in <field> as a <role>", "in <field> in a <role> role"
        # ... read as the reference "as a <role> in <field>" (D90). Reading a role there is a judgement call, so the line
        # is also judged as before: where the two differ it is CONDITIONAL, never a rejection (fix-check, D113).
        for field in FIELDS:
            reference = f"5+ years as a Business Systems Analyst in {field}"
            ref_reading = read(f"- {reference}", 5)
            self.assertEqual((ref_reading.kind, ref_reading.doubt, self.outcome(reference)),
                             ("role", "", ("CONDITIONAL", {"years_role_unconfirmed"})))
            # As before, "<field> experience" alone asks for the field's years (4: slightly short), and "in <field> as a
            # BSA" was already read on BSA years (0.5: far short); so the first group is CONDITIONAL, the second rejects
            # on both readings, as the reference does
            conditional = (f"5+ years of {field} experience as a Business Systems Analyst",
                           f"5+ years of experience in {field}, as a Business Systems Analyst",
                           f"5+ years of {field} experience, as a Business Systems Analyst",
                           f"5+ years of {field} industry experience as a Business Systems Analyst",
                           f"5+ years of relevant {field} experience as a Business Systems Analyst",
                           f"5+ years of {field} experience working as a Business Systems Analyst",
                           f"5+ years of {field} experience serving as a Business Systems Analyst",
                           f"5+ years of {field} experience in a Business Systems Analyst role",
                           f"5+ years of experience in {field} in a Business Systems Analyst role",
                           f"5+ years in {field} in a Business Systems Analyst position")
            rejected = (f"5+ years of experience in {field} as a Business Systems Analyst",
                        f"5+ years of experience within {field} as a Business Systems Analyst",
                        f"5+ years of experience in the {field} industry as a Business Systems Analyst",
                        f"5+ years in {field} as a Business Systems Analyst")
            for line in conditional + rejected:
                with self.subTest(line=line):
                    reading = read(f"- {line}", 5)
                    self.assertEqual((reading.kind, reading.area, reading.alternatives),
                                     (ref_reading.kind, ref_reading.area, ref_reading.alternatives))
                    self.assertTrue(reading.place.startswith(field.lower()), reading.place)
                    self.assertTrue(reading.doubt)
                    self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}) if line in conditional
                                     else ("CONDITIONAL", {"years_role_unconfirmed"}))
                    self.assertEqual(self.outcome(line, SENIOR), ("ELIGIBLE", set()))
                    self.assertEqual(self.outcome(line, NOVICE), ("CONDITIONAL", {"years_role_unconfirmed"}))
                    self.assertEqual(decisive(f"- {line}"), decisive(f"- {reference}"))

    def test_the_fail_safe_whatever_follows_and_on_soft_lines(self):
        # The fix-check of 947e709 (D113). A function after "or <words> in" is doubted too: with claims-auditing years
        # the two readings differ (CONDITIONAL); with none, claims auditing is an alternative nothing confirms (Codex F2)
        auditor = Profile(data={**PERSONA.data, "functions": [*PERSONA.data["functions"],
                                                             {"name": "claims auditing", "years": 5, "evidence": "direct"}]},
                          fingerprint="d113-auditor")
        line = "5+ years as a Business Systems Analyst or similar in claims auditing"
        self.assertEqual(self.outcome(line, auditor), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))
        # A soft line ("or equivalent") with a far shortfall on the role is to verify, never a gap flag that passes (D123
        # R1: "equivalent" and "or" are cues); within the gap it flags years_gap as before
        soft = "5+ years as a Business Systems Analyst or equivalent in healthcare"
        self.assertEqual((self.outcome(soft), self.outcome(soft, NOVICE)), (("CONDITIONAL", {"years_role_unconfirmed"}),) * 2)
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst or equivalent", owner_of([("business systems analyst", 2)])),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        # "Read another way" names what the years were for: healthcare's 4, not the first record's (SQL, 3 years)
        result = judged("5+ years of healthcare experience as a Business Systems Analyst; SQL is a must")
        self.assertIn("read another way, 5+ years of healthcare (you have 4)", detail(result, "years_role_unconfirmed"))
        # Where every reading passes, the strictest one's flag is shown: 6 BSA years, 1.5 in healthcare
        held = Profile(data={**PERSONA.data, "role_titles": [{"name": "business systems analyst", "years": 6, "evidence": "direct"}],
                             "domains": [{"name": "healthcare", "years": 1.5}]}, fingerprint="d113-held")
        result = judged("5+ years of healthcare experience as a Business Systems Analyst", profile=held)
        # (since D151 a gap on a line read as a role is to verify, never a years_gap)
        self.assertTrue(detail(result, "years_role_unconfirmed").startswith("5+ years of healthcare required (you have 1.5)"))

    def test_how_someone_works_is_no_role(self):
        # The fix-check: a duty after the field ("as a team member", "in a client-facing role") leaves the field's years
        for field in FIELDS:
            plain = self.outcome(f"5+ years of {field} experience")
            for duty in ("as a team member", "as a member of a cross-functional team", "as an individual contributor",
                         "in a client-facing role", "in a leadership role", "acting as the primary point of contact"):
                line = f"5+ years of {field} experience {duty}"
                with self.subTest(line=line):
                    self.assertIsNone(read(f"- {line}", 5))
                    self.assertEqual(self.outcome(line), plain)

    def test_the_two_kinds_keep_their_bounds(self):
        # A role named first keeps the line: a later "experience as a <role>" is not its field (fix-check)
        for line, area in (("- 5+ years as a Business Systems Analyst with healthcare experience as a consultant",
                            "business systems analyst with healthcare"),
                           ("- 5+ years as a Business Systems Analyst or related experience as a Product Owner",
                            "business systems analyst or related"),
                           ("- 5+ years in an analyst role with healthcare experience as a consultant", "analyst"),
                           ("- 5+ years of experience in a lead role as a Business Analyst", "business analyst")):
            reading = read(line, 5)
            self.assertEqual((reading.kind, reading.area, reading.doubt), ("role", area, ""), line)
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst with healthcare experience as a consultant"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        # A role or a skill after "or" stays what it names; "in" with no "or" before it still names the place
        similar = read("- 5+ years as a Business Systems Analyst or a similar role in healthcare", 5)
        self.assertEqual((similar.alternatives, similar.place), ((("business systems analyst", "specific"),), "healthcare"))
        either = read("- 5+ years of experience in healthcare or in an analyst role", 5)   # an alternative, not the field
        self.assertIn(("healthcare", "domain"), either.alternatives)
        # A guess whose other reading is the line as before keeps both: here claims auditing, and healthcare years
        offered = read("- 5+ years of experience in healthcare or similar in claims auditing", 5)
        self.assertEqual((offered.kind, bool(offered.doubt), offered.others), ("function", True, (None,)))
        # Only "or in" itself and runs of fillers are certain: an aside is a judgement call too (D115)
        self.assertTrue(read("- 5+ years as a Business Systems Analyst or billing, preferably in healthcare", 5).doubt)
        self.assertEqual(read("- 5+ years as a Business Systems Analyst or in healthcare", 5).doubt, "")
        # Only a role right after the field counts: "acting as", a setting, an aside or a long "in a ... position" do not
        for line in ("- 5+ years of healthcare experience acting as the lead analyst",
                     "- 5+ years of healthcare experience in a hospital setting",
                     "- 5+ years of healthcare experience in this role",
                     "- 5+ years of claims experience in a payer environment demonstrating breadth and depth of auditing "
                     "knowledge for the position"):
            self.assertIsNone(read(line, 5), line)
        # A role and the place after it are read as before
        clinical = read("- 3+ years of work experience in a clinical recruitment role in a healthcare industry", 3)
        self.assertEqual((clinical.kind, clinical.area, clinical.doubt), ("role", "clinical recruitment", ""))
        finance = read("- 5+ years of experience in claims or a comparable analyst role in a fast-paced environment", 5)
        self.assertEqual(finance.kind, "role")
        # Generic words before the role are no field; the field loses a trailing "and"; a field and a place are both kept
        working = read("- 3+ years of relevant working experience as a business analyst", 3)
        self.assertEqual((working.kind, working.area, working.place, working.doubt), ("role", "business analyst", "", ""))
        joined = read("- 5+ years of experience in healthcare and as a Business Systems Analyst", 5)
        self.assertEqual((joined.area, joined.place), ("business systems analyst", "healthcare"))
        self.assertEqual(self.outcome("5+ years of experience in healthcare and as a Business Systems Analyst"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # both readings ask for BSA years
        both = read("- 5+ years of healthcare experience as a Business Systems Analyst in PBM", 5)
        self.assertEqual((both.area, both.place), ("business systems analyst", "healthcare; pbm"))


# --- Codex Pass 1 on the combined candidate 0a87d07 (CR-2026-11-05-COMBINED): F2-F5 --------------------------------
# One kind, the fourth time (D112 rule 1): an alternative or a figure the record cannot confirm is never dropped and
# never rejects on its own (D1, D21, D22). Each failing phrase stands beside its paired control.
def owner_of(roles=(), functions=()):
    """The persona with these role titles (and functions) recorded: [(name, years)]."""
    return Profile(data={**PERSONA.data,
                         "role_titles": [{"name": n, "years": y, "evidence": "direct"} for n, y in roles],
                         "functions": [*PERSONA.data["functions"], *({"name": n, "years": y, "evidence": "direct"}
                                                                     for n, y in functions)]},
                   fingerprint=f"codex-p1-{roles}-{functions}")


class CodexPass1Tests(unittest.TestCase):
    def outcome(self, line, profile=PERSONA):
        result = judged(line, profile=profile)
        return state_for(result, APPROVED), years_codes(result)

    def test_f2_an_unconfirmed_alternative_is_never_dropped(self):
        # 0.5 years as a BSA, nothing recorded as a Product Owner: the PO alternative needs verifying, not a rejection
        line = "5+ years as a Business Systems Analyst or a Product Owner"
        self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst or 5+ years as a Product Owner"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # control: the repeated figure
        bsa = ("business systems analyst", 0.5)
        self.assertEqual(self.outcome(line, owner_of([bsa, ("product owner", 0.5)])),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # every alternative known short: "or" (D123 R1)
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst", owner_of([bsa, ("product owner", 0.5)])),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))           # control: no alternative offered
        self.assertEqual(self.outcome(line, owner_of([bsa, ("product owner", 6)])), ("ELIGIBLE", set()))   # one suffices
        # Sibling paths: a function OR-list, a role-noun OR-list, a known alternative only slightly short
        self.assertEqual(self.outcome("5+ years in business systems analysis or payment integrity"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("5+ years in a Business Systems Analyst or Product Owner role"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome(line, owner_of([("business systems analyst", 4)])),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertIn("nothing in your record confirms product owner", detail(judged(line), "years_role_unconfirmed"))
        # A domain alternative nothing in the record names, and one only partly matched, are unconfirmed too
        for other in ("in financial services", "a Customer Service Lead"):
            with self.subTest(other=other):
                self.assertEqual(self.outcome(f"5+ years as a Business Systems Analyst or {other}"),
                                 ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_f3_a_role_figure_beside_a_tool_is_kept(self):
        # The role figure is the role's, though its phrase also names a tool: kept and judged, never silently passed
        for line in ("5+ years as a Business Systems Analyst with SQL, including 2+ years in healthcare",
                     "8+ years as a Business Systems Analyst using SQL; 2+ years in healthcare",
                     "5+ years as a Business Systems Analyst with SQL; 2+ years with Excel",
                     "5+ years as a Business Systems Analyst, including 2+ years with SQL"):   # the control
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))
                figure = float(line.split("+")[0])
                self.assertIn(figure, {r.years for r in extract(f"Requirements:\n- {line}\n", APPROVED).requirements
                                       if r.category == "years_experience"})
        # A skill named after other words is not introduced by the figure's phrase: the figure stays
        line = "5+ years of experience in project coordination using SQL; 2+ years with Excel"
        self.assertIn(5.0, {r.years for r in extract(f"Requirements:\n- {line}\n", APPROVED).requirements
                            if r.category == "years_experience"})
        # Control: a figure the skill's own phrase introduces is the skill's ("10+ years with Excel"), no years_experience
        # record; since D149 R-Y2 the 10 no record carries (Excel took the nearest 2) is to verify, never a silent pass
        self.assertEqual(self.outcome("10+ years with Excel and 2+ years with SQL"), ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_f4_a_role_only_partly_named_is_not_confirmed(self):
        # The "business systems analysis" function only overlaps "Business Intelligence Analyst": no confirmed tenure
        self.assertEqual(self.outcome("5+ years as a Business Intelligence Analyst"), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("5+ years as a Healthcare Data Analyst"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # control: nothing matches
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))           # control: the recorded title itself
        self.assertEqual(self.outcome("5+ years in business systems analysis"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))           # control: the recorded function itself
        # A partial match confirms neither a shortfall nor a pass (D3; the Fable fix-check of dba7b35): it is to verify
        quality = owner_of(functions=[("data quality analysis", 6)])
        self.assertEqual(self.outcome("5+ years as a Data Quality Lead", quality), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("9+ years as a Data Quality Lead", quality), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("5+ years as a Customer Service Lead"), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertIn("(a partial match)", detail(judged("5+ years as a Customer Service Lead"), "years_role_unconfirmed"))
        self.assertEqual(self.outcome("5+ years as a Business Intelligence Analyst or a Customer Service Lead"),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(self.outcome("5+ years in customer service"), ("ELIGIBLE", set()))   # control: the function, 5.7
        # A confirmed alternative that suffices passes, whatever a partial one beside it holds (title 3.7, partial 5.7)
        self.assertEqual(self.outcome("3+ years as a Customer Service Representative or a Customer Service Lead"),
                         ("ELIGIBLE", set()))
        self.assertEqual(self.outcome("11+ years as a Data Quality Analyst", quality),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # control: the function itself, 6 of 11
        # A confirmed title is judged before a partial function with more years: 1 year as a Data Quality Lead
        lead = owner_of([("data quality lead", 1)], [("data quality analysis", 6)])
        self.assertEqual(self.outcome("5+ years as a Data Quality Lead", lead), ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_f2_each_part_of_a_slash_alternative_is_confirmed_on_its_own(self):
        # The Fable fix-check of dba7b35: "BSA/Product Owner" is two alternatives; the BSA part never hides the PO part
        for line in ("5+ years as a Business Systems Analyst/Product Owner", "5+ years as a Product Owner / Business Systems Analyst",
                     "5+ years in a Business Systems Analyst/Product Owner role"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))
                self.assertIn("nothing in your record confirms product owner", detail(judged(line), "years_role_unconfirmed"))
        known = owner_of([("business systems analyst", 0.5), ("product owner", 0.5)])
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst or Product Owner/Scrum Master", known),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # Scrum Master: nothing recorded
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst/Product Owner", known),
                         ("CONDITIONAL", {"years_role_unconfirmed"}))   # both parts known short: "/" (D123 R1)
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst/Product Owner",
                                      owner_of([("business systems analyst", 0.5), ("product owner", 6)])), ("ELIGIBLE", set()))

    def test_the_cheap_lows_of_the_fix_check(self):
        # Preferred years with an unconfirmed alternative still say how short the known one is
        result = judged(preferred=["5+ years as a Business Systems Analyst or a Product Owner"])
        self.assertIn("you have 0.5", detail(result, "years_preferred_short"))
        # Two readings of a line with an unconfirmed alternative quote the known credit, not "nothing names it"
        result = judged("5+ years of healthcare experience as a Business Systems Analyst or a Product Owner")
        self.assertIn("(you have 0.5 as business systems analyst; product owner unconfirmed)", detail(result, "years_role_unconfirmed"))
        # A figure whose phrase introduces a skill after an adjective is the skill's ("10+ years of advanced Excel"); no
        # record carries the 10 (D149 R-Y2), so it is to verify, never a silent pass
        for line in ("10+ years of advanced Excel and 2+ years with SQL", "10+ years of expert-level Excel and 2+ years with SQL"):
            with self.subTest(line=line):
                self.assertNotIn(10.0, {r.years for r in extract(f"Requirements:\n- {line}\n", APPROVED).requirements
                                        if r.category == "years_experience"})
                self.assertEqual(self.outcome(line), ("CONDITIONAL", {"years_role_unconfirmed"}))

    def test_f5_a_role_without_its_article_is_still_a_role(self):
        for article in ("a ", ""):
            with self.subTest(article=article):
                self.assertEqual(self.outcome(f"4+ years as {article}Product Owner"), ("CONDITIONAL", {"years_role_unconfirmed"}))
                self.assertEqual(self.outcome(f"5+ years as {article}Business Systems Analyst"),
                                 ("CONDITIONAL", {"years_role_unconfirmed"}))
        # Sibling: a role without its article after its field is the same judgement call as with it
        self.assertEqual(self.outcome("5+ years of healthcare experience as Business Systems Analyst"),
                         self.outcome("5+ years of healthcare experience as a Business Systems Analyst"))
        # Words after "as" that name no role are not read as one
        for line in ("- 3+ years as part of a claims team", "- 3+ years as needed by the business", "- 3+ years as well as SQL",
                     "- 3+ years of experience in designing workflows as well as standing up procedures"):
            with self.subTest(line=line):
                self.assertNotEqual(getattr(read(line, 3), "kind", None), "role")


# --- The 2026-11-09 correction batch on the integrated candidate 285e72d (D118, D120) --------------------------------
# Acceptance of 285e72d and its verification against live 09d6c75 found the years-as-a-role reading dropping an
# alternative it did not read and then rejecting (D118), and two siblings the candidate introduced (D120): a function
# confirming another role through shared 6-letter stems or reordered words, and one figure stated for two demands
# judging only the first. Each failing phrase stands beside its paired control.
PO_SIX = owner_of([("business systems analyst", 0.5), ("product owner", 6)])
BSA_SIX = owner_of([("business systems analyst", 6)])


def night_years(block, profile=PERSONA):
    """NIGHT's state and years codes for a Requirements block in an overnight posting: NIGHT shares the years rule."""
    body = ("About us\nWe are a logistics company. Our team audits shipping claims overnight for accuracy.\n\nRequirements:\n"
            + block + "\nSchedule: overnight shift, Sunday to Thursday, 11pm to 7:30am. This role is remote. Back office, "
            "no phone queue.\n")
    out = assess("Claims Quality Auditor", body, profile=profile)
    return out["state"], {f["code"] for f in out["failures"] + out["flags"] if f["code"].startswith("years")}


def doer(function, years):
    """A synthetic record with one function and no role title: what a function alone confirms."""
    return Profile(data={**PERSONA.data, "total_professional_years": max(4, years), "role_titles": [],
                         "functions": [{"name": function, "years": years, "evidence": "direct"}]},
                   fingerprint=f"d120-{function}-{years}")


class CorrectionBatchRolesTests(unittest.TestCase):
    def outcome(self, line, profile=PERSONA):
        result = judged(line, profile=profile)
        return state_for(result, APPROVED), years_codes(result)

    def block(self, block, profile=PERSONA):
        """A Requirements block written as is: a bullet across two lines, or two bullets."""
        result = gate(TITLE, posting(extra=f"Requirements:\n{block}\n\nThis role is remote."), profile=profile)
        return state_for(result, APPROVED), years_codes(result)

    def test_d120_a_function_confirms_a_role_only_in_the_same_words(self):
        """D120: the 6-letter stems joined "production management" to "Product Manager" (also account/accounting,
        program/programmer, reordered "administrative assistance"/"Assistant Administrator"), so 0.3 years of a different
        function rejected and 6 passed unverified. Such a match is now partial (Codex F4): CONDITIONAL either way. A
        function's word for the work still names the role that does it (analysis/analyst, processing/processor)."""
        conditional = ("CONDITIONAL", {"years_role_unconfirmed"})
        for function, role in (("production management", "Product Manager"), ("account management", "Accounting Manager"),
                               ("program analysis", "Programmer Analyst"),
                               ("administrative assistance", "Assistant Administrator"),
                               ("claims reporting", "Claims Reporter"), ("pbm operations", "PBM Operator")):
            with self.subTest(function=function, role=role):
                self.assertTrue(doer(function, 0.3).years_match(role, role_only=True)["partial"])
                self.assertEqual(self.outcome(f"5+ years as a {role}", doer(function, 0.3)), conditional)
                self.assertEqual(self.outcome(f"5+ years as a {role}", doer(function, 6)), conditional)
        # Controls: the same words, the role's form of the work, a plural: confirmed, short or long
        for function, role in (("product management", "Product Manager"), ("account management", "Account Manager"),
                               ("programming analysis", "Programmer Analyst"),
                               ("administrative assistance", "Administrative Assistant"),
                               ("claims processing", "Claims Processor"), ("claims review", "Claims Reviewer"),
                               ("business systems analysis", "Business Systems Analyst"),
                               ("business system analysis", "Business Systems Analyst"),
                               ("solution architecture", "Solution Architect"), ("content moderation", "Content Moderator"),
                               ("medical interpretation", "Medical Interpreter"), ("statistics", "Statistician"),
                               ("data architecture", "Data Architect")):
            with self.subTest(function=function, role=role):
                self.assertFalse(doer(function, 0.3).years_match(role, role_only=True).get("partial"))
                self.assertEqual(self.outcome(f"5+ years as a {role}", doer(function, 0.3)), ("CONDITIONAL", {"years_role_unconfirmed"}))
                self.assertEqual(self.outcome(f"5+ years as a {role}", doer(function, 6)), ("ELIGIBLE", set()))
        # A function in the role's own words is found before one that only shares their stems
        both = Profile(data={**doer("production management", 6).data,
                             "functions": [{"name": "production management", "years": 6, "evidence": "direct"},
                                           {"name": "product management", "years": 0.3, "evidence": "direct"}]},
                       fingerprint="d120-both")
        self.assertEqual(self.outcome("5+ years as a Product Manager", both), ("CONDITIONAL", {"years_role_unconfirmed"}))
        # Since role years never reject on their own (D149), the outcome above is the same whichever record is found, so
        # check the record itself (the staged campaign of 2026-11-17 found this defect surviving the whole suite)
        found = both.years_match("Product Manager", role_only=True)
        self.assertEqual((found["item"], found["years"], bool(found.get("partial"))), ("product management", 0.3, False))
        self.assertTrue(doer("claims reporting", 6).years_match("Claims Reporter", role_only=True)["partial"])

    def test_d120_one_figure_stated_for_two_demands_judges_both(self):
        """D120: "8+ years as a BSA and 8+ years of healthcare experience" is one (line, figure) key, and only the role
        was judged, so 2 healthcare years passed (live rejects). Each demand is now judged on the records its own words
        name, and the stricter decides, in the gate and in Experience Match; a figure named twice as alternatives ("or")
        stays one demand (D115)."""
        def owner(bsa, healthcare):
            return Profile(data={**PERSONA.data, "total_professional_years": 10, "functions": [],
                                 "role_titles": [{"name": "business systems analyst", "years": bsa, "evidence": "direct"}],
                                 "domains": [{"name": n, "years": healthcare} for n in ("healthcare", "pbm", "claims")]},
                           fingerprint=f"d120-{bsa}-{healthcare}")
        short_domain, short_role, both = owner(9, 2), owner(0.5, 9), owner(9, 9)
        for line in ("8+ years as a Business Systems Analyst and 8+ years of healthcare experience",
                     "8+ years of experience as a Business Systems Analyst and 8+ years of healthcare experience",
                     "8+ years as a Business Systems Analyst; 8+ years in healthcare",
                     "8+ years of healthcare experience and 8+ years of experience as a Business Systems Analyst"):
            with self.subTest(line=line):
                result = judged(line, profile=short_domain)
                # since D155 (1) a domain far short is to verify, never a rejection; the flag names the stricter demand
                self.assertEqual(state_for(result, APPROVED), "CONDITIONAL")
                self.assertIn("8+ years of healthcare required (you have 2)", detail(result, "years_role_unconfirmed"))
                # D149 R-Y0: the role's far shortfall is to verify, never a rejection; a domain far short beside it still
                # rejects, never hidden behind the role's flag
                self.assertTrue(detail(judged(line, profile=short_role), "years_role_unconfirmed").startswith(
                    "8+ years as business systems analyst required (you have 0.5)"))
                self.assertEqual(state_for(judged(line, profile=short_role), APPROVED), "CONDITIONAL")
                self.assertIn("8+ years of healthcare required (you have 2)",
                              detail(judged(line, profile=owner(0.5, 2)), "years_role_unconfirmed"))
                self.assertEqual(self.outcome(line, both), ("ELIGIBLE", set()))
                block = exp.evaluate(extract(f"Requirements:\n- {line}\n", APPROVED).requirements, short_domain, APPROVED)
                decisive = block["lines"][block["decisive_line"]]
                self.assertEqual((decisive["structure"], decisive["counted_years"]), ("and", 2.0))
        sql = Profile(data={**short_domain.data, "skills": [{"name": "sql", "years": 2, "evidence": "direct"}]},
                      fingerprint="d120-sql")
        self.assertIn("8+ years of sql required (you have 2)",
                      detail(judged("8+ years as a Business Systems Analyst and 8+ years with SQL", profile=sql),
                             "years_role_unconfirmed"))
        # Controls: two figures, or two bullets, were judged on both already; the same figure named twice as
        # alternatives is one demand, met by the role's own years, which flags at most (D115). Since D155 (1) a domain
        # far short is to verify, so each is CONDITIONAL with its own flag
        for result in (judged("8+ years as a Business Systems Analyst and 7+ years of healthcare experience",
                              profile=short_domain),
                       judged("8+ years as a Business Systems Analyst", "8+ years of healthcare experience",
                              profile=short_domain),
                       judged("8+ years as a Business Systems Analyst and 8+ years of healthcare experience",
                              profile=owner(0.5, 2))):
            self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
            self.assertIn("of healthcare required (you have 2)", " ".join(
                f["detail"] for f in result.flags if f["code"] == "years_role_unconfirmed"))
        line = "8+ years as a Business Systems Analyst or 8+ years of healthcare experience"
        self.assertEqual(self.outcome(line, short_domain), ("ELIGIBLE", set()))
        self.assertEqual(self.outcome(line, short_role), ("CONDITIONAL", {"years_role_unconfirmed"}))


# --- D123 (2026-11-09): blunt fail-safe rules for a role line's alternatives -------------------------------------------
# Three build-and-check rounds of D118/D120 tried to tell a role line's real alternatives from harmless "or"s by word
# lists, and each independent check found new wordings, with a High every round. D123 replaces them: any cue of an
# alternative makes a role's shortfall CONDITIONAL (R1); nothing but years in the role, or in a role the line names that
# your record confirms, passes it (R2); posting lines are never merged (R3); two demands of one figure with a cue between
# them flag, never reject, never pass on the other (R4). A needless CONDITIONAL is accepted; a wrong pass or rejection
# from these readings is not.
BSA_SIX_PO_HALF = owner_of([("business systems analyst", 6), ("product owner", 0.5)])
BOTH_HALF = owner_of([("business systems analyst", 0.5), ("product owner", 0.5)])


def graduate(bsa, healthcare):
    """A synthetic record with years as a BSA, healthcare (PBM, claims) years and a completed bachelor's degree, so a
    degree named on the line never decides."""
    return Profile(data={**PERSONA.data, "total_professional_years": 10, "functions": [],
                         "role_titles": [{"name": "business systems analyst", "years": bsa, "evidence": "direct"}],
                         "domains": [{"name": n, "years": healthcare} for n in ("healthcare", "pbm", "claims")],
                         "education": [{"level": "high_school", "completed": True},
                                       {"level": "bachelor", "completed": True}]},
                   fingerprint=f"check-roles-{bsa}-{healthcare}")


def with_sql(profile, years):
    """The record with its SQL years set (synthetic)."""
    return Profile(data={**profile.data, "skills": [{"name": "sql", "years": years, "evidence": "direct"},
                                                    {"name": "excel", "years": 10, "evidence": "direct"}]},
                   fingerprint=f"{profile.fingerprint}-sql{years}")


OFFERED = ("CONDITIONAL", {"years_role_unconfirmed"})
SHORT = ("INELIGIBLE", {"years_far_short"})
MET = ("ELIGIBLE", set())
BSA = "5+ years as a Business Systems Analyst"
# The D118 task inputs, the round-3 lists and the checkers' rows: the role (a BSA's 0.5) is far short in each, and each
# holds a cue of an alternative on its line or at the end of the line before. Blocks are written as posted.
CUED = [
    # N2 and its forms: a role line ending in "or" before words and a figure of another kind (High, round 3)
    ("- 5+ years as a Business Systems Analyst or\n  Product Owner, and 3+ years of SQL", BOTH_HALF),
    ("- 5+ years as a Business Systems Analyst or\n  Product Owner, and 3+ years of SQL", PERSONA),
    (html_to_text("<ul><li>5+ years as a Business Systems Analyst or<br>Product Owner, and 3+ years of SQL</li></ul>"), BOTH_HALF),
    ("- 5+ years as a Business Systems Analyst or a\nProduct Owner, and 3+ years of SQL", PERSONA),
    ("- 5+ years as a Business Systems Analyst in a payer, PBM or\nprovider setting, with 3+ years of SQL", PERSONA),
    ("- 5+ years as a Business Systems Analyst, or\n- 3+ years of SQL", with_sql(PERSONA, 6)),
    ("- 5+ years of Business Systems Analyst experience or 3+ years of SQL", with_sql(PERSONA, 6)),
    ("- 5+ years as a Business Systems Analyst or 3+ years of SQL", with_sql(PERSONA, 6)),
    ("- 5+ years as a Business Systems Analyst AND/OR\n- 3+ years of SQL", with_sql(PERSONA, 6)),
    ("- 5+ years as a Business Systems Analyst, or\n- healthcare experience of 8+ years", graduate(0.5, 9)),
    ("- 8+ years as a Business Systems Analyst or 5+ years of healthcare experience", graduate(0.5, 9)),
    ("- 8+ years as a Business Systems Analyst or 8+ years of healthcare experience", graduate(0.5, 9)),
    # D118: the alternative in parentheses, after ';' or '. ', across a line break, with a Product Owner's 6 or none
    (f"- {BSA} (or Product Owner)", PO_SIX), (f"- {BSA} (or Product Owner)", PERSONA),
    (f"- {BSA} (BSA) or Product Owner (PO)", PO_SIX), (f"- {BSA} (BSA) or Product Owner (PO)", PERSONA),
    (f"- {BSA}; Product Owner experience also accepted", PO_SIX),
    (f"- {BSA}. Product Owner experience is also considered.", PO_SIX),
    (f"- {BSA}, or\n- Product Owner experience", PO_SIX), (f"- {BSA}, or\n- Product Owner experience", PERSONA),
    (f"- {BSA} or a\nProduct Owner", PO_SIX), (f"- {BSA} or a\nProduct Owner", PERSONA),
    (f"- {BSA}, or\n- 5+ years as a Product Owner", BOTH_HALF),
    (f"1. {BSA}, or\n2. 5+ years as a Product Owner", PERSONA),
    # Round 3: acceptance said other ways, qualified, denied (a denial too is CONDITIONAL: verify)
    (f"- {BSA}; we also accept Product Owner experience", PO_SIX),
    (f"- {BSA}; Product Owner experience works too", PO_SIX),
    (f"- {BSA}; Product Owner experience without certification is also accepted", PO_SIX),
    (f"- {BSA}; Product Owner experience is no substitute", PERSONA),
    (f"- {BSA}; neither Product Owner nor Scrum Master experience is accepted", PERSONA),
    (f"- {BSA}; Product Owner experience counts", PO_SIX),
    (f"- {BSA}; Product Owner experience may substitute", PO_SIX),
    (f"- {BSA}; equivalent experience considered", PERSONA),
    (f"- {BSA} in lieu of a degree", PERSONA),
    (f"- {BSA} (BSA/PO)", PO_SIX), (f"- {BSA} / Product Owner", PERSONA),
    (f"- {BSA} or equivalent", PERSONA),
    # A domain or a setting offered beside the role never meets it (healthcare 4 against 5)
    (f"- {BSA} or in a health plan environment", PERSONA),
    (f"- {BSA} or a similar role in healthcare", PERSONA),
    # Over-flagging is accepted: these offer nothing in the role's place, and are CONDITIONAL all the same
    (f"- {BSA} (Agile or Waterfall)", PERSONA), (f"- {BSA} (1st or 2nd shift)", PERSONA), (f"- {BSA} (OR)", PERSONA),
    (f"- {BSA} supporting surgical teams in the OR", PERSONA),
    # Two demands of one figure with a cue between them (R4): the role's rejection flags
    ("- 8+ years as a Business Systems Analyst, or a Bachelor's degree and 8+ years of healthcare experience", graduate(0.5, 9)),
    ("- 8+ years as a Business Systems Analyst in a payer or provider setting and 8+ years of healthcare experience",
     graduate(0.5, 9)),
    ("- 5+ years of business systems analysis experience, or", PERSONA),
]


class D123RolesTests(unittest.TestCase):
    outcome = CorrectionBatchRolesTests.outcome   # one line: PRIMARY's state and years codes
    block = CorrectionBatchRolesTests.block       # a Requirements block written as is

    def test_d123_the_class_never_eligible_far_short_never_ineligible_with_a_cue(self):
        """D123 (the class, PRIMARY and NIGHT): for each line of the table, the role far short and its own years not met,
        the posting is never ELIGIBLE (no figure, domain or line of another kind passes it: R2, R3) and never INELIGIBLE
        (the line holds a cue of an alternative: R1, R4). CONDITIONAL is the answer for every one."""
        self.assertGreaterEqual(len(CUED), 30)
        for block, profile in CUED:
            with self.subTest(block=block, profile=profile.fingerprint):
                primary, night_state = self.block(block, profile)[0], night_years(block + "\n", profile)[0]
                self.assertNotIn(primary, ("ELIGIBLE", "INELIGIBLE"))
                self.assertNotIn(night_state, ("ELIGIBLE", "INELIGIBLE"))
                self.assertEqual((primary, night_state), ("CONDITIONAL", "CONDITIONAL"))

    def test_d123_r1_any_cue_makes_the_shortfall_conditional_and_the_role_met_still_passes(self):
        """D123 R1 (the D118 class): a role-years line with any cue of an alternative never rejects on the role's
        shortfall: years_role_unconfirmed, naming the role's figure, quoting the cue and the line. Enough years in the role
        still pass (the cue never makes a pass harder). The cues are one closed list (`role_years.ALTERNATIVE_CUES`)."""
        for tail in (" (or Product Owner)", "; Product Owner experience also accepted", ", or", "; OR", " or a",
                     "; Product Owner experience works too", " (Agile or Waterfall)", " (BSA, PO)", " (BSA/PO)"):
            line = f"{BSA}{tail}"
            with self.subTest(line=line):
                result = judged(line)
                self.assertEqual((state_for(result, APPROVED), years_codes(result)), OFFERED)
                found = next(f for f in result.flags if f["code"] == "years_role_unconfirmed")
                self.assertTrue(found["detail"].startswith("5+ years as business systems analyst required (you have 0.5)"))
                self.assertIn("the line may offer an alternative", found["detail"])
                self.assertEqual(found["source_text"], f"- {line}")
                self.assertEqual(self.outcome(line, BSA_SIX), MET)
        for text, cue in (("5+ years as a BSA (or PO)", "(or PO)"), ("5+ years as a BSA or PO", "or"),("as a BSA; PO experience is also accepted", "also"),
                          ("as a BSA/PO", "A/P"), ("as a BSA (BSA, PO)", "(BSA, PO)"), ("5+ years as a BSA", None),
                          ("as a BSA (BSA)", None), ("5+ years as a BSA and 5+ years of SQL", None),
                          ("Qualified candidates bring 5+ years as a BSA", "Qualified")):
            with self.subTest(text=text):
                self.assertEqual(role_years.alternative_cue(text), cue)
        for end in (", or", "; OR", ", Or", " AND/OR", " and/or", " or a", "; or"):
            with self.subTest(end=end):
                self.assertTrue(role_years.ends_in_joiner(f"- {BSA}{end}"))
        self.assertFalse(role_years.ends_in_joiner(f"- {BSA} in Oregon"))

    def test_d123_r1_controls_without_a_cue_still_reject(self):
        """D123's controls: with no cue on the line or a joiner before it, a far shortfall rejected years_far_short on
        PRIMARY and NIGHT until D149 R-Y0; a role's years never reject on their own now, so it is CONDITIONAL on both,
        and the role met passes."""
        for line in (BSA, f"{BSA} (BSA)", f"{BSA} (required)", f"{BSA} in healthcare", f"{BSA}; SQL is a must"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line), OFFERED)
                self.assertEqual(night_years(f"- {line}\n"), OFFERED)
                self.assertEqual(self.outcome(line, BSA_SIX), MET)

    def test_d123_r2_a_figure_or_domain_of_another_kind_never_passes_a_role(self):
        """D123 R2: SQL, healthcare or a setting met on the same line (as a figure joined by "or", or as an alternative in
        the area) never passes a role's demand; the role's shortfall is CONDITIONAL. A role the line names that your
        record confirms in the same words passes, and so does the role itself."""
        for line, profile in (("5+ years as a Business Systems Analyst or 3+ years of SQL", with_sql(PERSONA, 6)),
                              ("5+ years of Business Systems Analyst experience or 3+ years of SQL", with_sql(PERSONA, 6)),
                              ("5+ years as a Business Systems Analyst / 3+ years with SQL", with_sql(PERSONA, 6)),
                              ("5+ years as a Business Systems Analyst or 3+ years in PBM operations", PERSONA),
                              ("5+ years as a Business Systems Analyst or in a health plan environment", graduate(0.5, 9)),
                              ("5+ years as a Business Systems Analyst or a similar role in healthcare", graduate(0.5, 9))):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, profile), OFFERED)
                self.assertEqual(night_years(f"- {line}\n", profile), OFFERED)
        for line in ("5+ years as a Business Systems Analyst or 3+ years as a Product Owner",
                     "5+ years as a Business Systems Analyst or Product Owner"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, PO_SIX), MET)   # a role the line names, confirmed in the same words
        self.assertEqual(self.outcome("5+ years as a Business Systems Analyst or 3+ years of SQL", with_sql(BSA_SIX, 0.5)), MET)
        far = judged("9+ years as a Business Systems Analyst or 8+ years of hands-on SQL")   # far short of both: verify
        self.assertEqual((state_for(far, APPROVED), years_codes(far)), OFFERED)
        self.assertIn("9+ years as business systems analyst required", detail(far, "years_role_unconfirmed"))

    def test_d123_r3_posting_lines_are_never_merged(self):
        """D123 R3: each posting line's demands are judged on their own. A joiner at the end of a line, or of the line
        before, only turns that line's would-be rejection into CONDITIONAL; it never passes a line and never removes a
        shortfall from another line (a BSA's 6 with a Product Owner's 0.5 on the bullet after ", or" is a needless
        CONDITIONAL, accepted)."""
        for block in (f"- {BSA}, or\n- 5+ years as a Product Owner", f"- {BSA}; OR\n- 5+ years as a Product Owner",
                      f"1. {BSA}, or\n2. 5+ years as a Product Owner", f"- {BSA} AND/OR\n- Must have 5+ years as a Product Owner",
                      html_to_text(f"<ul><li>{BSA}, or</li><li>5+ years as a Product Owner</li></ul>")):
            with self.subTest(block=block):
                self.assertEqual(self.block(block, BSA_SIX_PO_HALF), OFFERED)   # the second bullet: verify
                self.assertEqual(self.block(block, PO_SIX), OFFERED)            # the first bullet: verify
                self.assertEqual(night_years(block + "\n", BSA_SIX_PO_HALF), OFFERED)
        both = owner_of([("business systems analyst", 6), ("product owner", 6)])
        self.assertEqual(self.block(f"- {BSA}, or\n- 5+ years as a Product Owner", both), MET)
        # A met figure on the next line never passes the role line; the next line keeps its own demand, whose shortfall
        # after the joiner flags too (any figure after a joiner: no wrong rejection of a met first bullet)
        self.assertEqual(self.block(f"- {BSA}, or\n- 3+ years of SQL", with_sql(PERSONA, 6)), OFFERED)
        self.assertEqual(self.block(f"- {BSA}, or\n- 8+ years of SQL", with_sql(BSA_SIX, 0.5)), OFFERED)
        # (a tool far short alone was SHORT; since D155 (1) only total professional years reject)
        self.assertEqual(self.block(f"- {BSA}\n- 8+ years of SQL", with_sql(BSA_SIX, 0.5)), OFFERED)
        # ... and so does a figure on a line that itself ends in a joiner, of whatever kind
        self.assertEqual(self.block(f"- 8+ years of SQL, or\n- {BSA}", with_sql(BSA_SIX, 0.5)), OFFERED)
        self.assertEqual(self.block(f"- 8+ years of SQL\n- {BSA}", with_sql(BSA_SIX, 0.5)), OFFERED)
        # Controls: no joiner, two demands; a blank line is no posting line, so a joiner before it still counts
        self.assertEqual(self.block(f"- {BSA}\n- 5+ years as a Product Owner", BSA_SIX_PO_HALF), OFFERED)
        self.assertEqual(self.block(f"- {BSA}\n- 5+ years as a Product Owner", PO_SIX), OFFERED)
        self.assertEqual(self.block(f"- {BSA}, or\n\n- 5+ years as a Product Owner", BSA_SIX_PO_HALF), OFFERED)
        # Since D151 (c) the requirements-wide cue protects these too; the line's own joiner is still the one the flag
        # names, so the reader of the joiner keeps its own check
        for block, cue in ((f"- 8+ years of SQL, or\n- {BSA}", "or (the line ends in it)"),
                           (f"- {BSA}, or\n- 8+ years of SQL", "or (the line before ends in it)")):
            with self.subTest(block=block):
                result = gate(TITLE, posting(extra=f"Requirements:\n{block}\n\nThis role is remote."),
                              profile=with_sql(BSA_SIX, 0.5))
                self.assertIn(f'("{cue}")', detail(result, "years_role_unconfirmed"))

    def test_d123_r4_two_demands_of_one_figure_with_a_cue_between_flag(self):
        """D123 R4 (D115, D120(b) kept): two demands of one figure joined by "and" are both judged; with any cue between
        them ("or", "/", "either" ...) the stricter demand's rejection is CONDITIONAL, never a rejection and never a pass on
        the other demand; Experience Match scores such a line as AND."""
        for line in ("8+ years as a Business Systems Analyst, or a Bachelor's degree and 8+ years of healthcare experience",
                     "8+ years as a Business Systems Analyst in a payer or provider setting and 8+ years of healthcare experience",
                     "8+ years as a Business Systems Analyst supporting claims or eligibility, 8+ years in healthcare",
                     "8+ years as a Business Systems Analyst (or a similar role with 8+ years in healthcare)"):
            with self.subTest(line=line):
                for profile in (graduate(9, 2), graduate(0.5, 9), graduate(0.5, 2)):
                    self.assertEqual(self.outcome(line, profile), OFFERED)
                self.assertEqual(night_years(f"- {line}\n", graduate(9, 2)), OFFERED)
                self.assertEqual(self.outcome(line, graduate(9, 9)), MET)
                block = exp.evaluate(extract(f"Requirements:\n- {line}\n", APPROVED).requirements, graduate(9, 2), APPROVED)
                decisive = block["lines"][block["decisive_line"]]
                self.assertEqual((decisive["structure"], decisive["counted_years"]), ("and", 2.0))
        self.assertEqual(role_years.alternative_cue(" years as a bsa in a payer or provider setting and "), "or")
        # Controls: "and" alone, both judged and the stricter decides (D120(b)); since D155 (1) a tool or a domain far short
        # is to verify, so the stricter one's flag is shown
        self.assertIn("5+ years of sql required (you have 0.5)", detail(judged(
            "5+ years as a Business Systems Analyst and 5+ years of SQL", profile=with_sql(BSA_SIX, 0.5)), "years_role_unconfirmed"))
        self.assertIn("8+ years of healthcare required (you have 2)", detail(judged(
            "8+ years as a Business Systems Analyst and 8+ years of healthcare experience", profile=graduate(9, 2)),
            "years_role_unconfirmed"))


# --- The independent check of 4393da1 (2026-11-10, D123): roles joined by "and", an alternative on a line of its own,
# joiners in punctuation or markup, a preference beside an unknown demand, a plural role. Each beside its control.
BSA_PO = "5+ years as a Business Systems Analyst"
PO_LINE = "5+ years as a Product Owner"
NO_FUNCTIONS = Profile(data={**owner_of([("business systems analyst", 6)]).data, "functions": []}, fingerprint="d123chk-nofn")


class D123CheckTests(unittest.TestCase):
    outcome = CorrectionBatchRolesTests.outcome   # one line: PRIMARY's state and years codes
    block = CorrectionBatchRolesTests.block       # a Requirements block written as is

    def test_d123_roles_joined_by_and_are_each_a_demand(self):
        """D123's check (High): an area naming two roles joined by "and" passed on the better one (a BSA's 6 with a
        Product Owner's 0.5: ELIGIBLE). Such an area passes only when every role it names is met; one short or unknown
        is CONDITIONAL (years_role_unconfirmed); it rejects only when every role is known and far short."""
        for line in (f"{BSA_PO} and Product Owner", "5+ years as both a Business Systems Analyst and a Product Owner",
                     "5+ years in Business Systems Analyst and Product Owner roles", f"{BSA_PO} & Product Owner",
                     "5+ years of Business Systems Analyst and Product Owner experience"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, BSA_SIX_PO_HALF), OFFERED)
                self.assertEqual(night_years(f"- {line}\n", BSA_SIX_PO_HALF), OFFERED)
                self.assertEqual(self.outcome(line, PO_SIX), OFFERED)
                self.assertEqual(self.outcome(line, owner_of([("business systems analyst", 6), ("product owner", 6)])), MET)
                self.assertEqual(self.outcome(line, BOTH_HALF), OFFERED)   # every role known and far short
        self.assertIn("each must be met", detail(judged(f"{BSA_PO} and Product Owner", profile=BSA_SIX_PO_HALF),
                                                 "years_role_unconfirmed"))
        # Experience Match counts the weakest role, never the better one
        both_met = owner_of([("business systems analyst", 6), ("product owner", 9)])
        block = exp.evaluate(extract(f"Requirements:\n- {BSA_PO} and Product Owner\n", APPROVED).requirements, both_met, APPROVED)
        self.assertEqual(block["lines"][block["decisive_line"]]["counted_years"], 6.0)
        # A role nothing records (Scrum Master), in a pair or a list: to verify, never a pass on the BSA's 6
        for line in (f"{BSA_PO} and Scrum Master", f"{BSA_PO}, Product Owner and Scrum Master"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, BSA_SIX_PO_HALF)[0], "CONDITIONAL")
        # Controls: "and/or" and "or" stay alternatives, met by either
        for line in (f"{BSA_PO} and/or Product Owner", f"{BSA_PO} or Product Owner"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, BSA_SIX_PO_HALF), MET)
        self.assertEqual(role_years.conjoined("business systems analyst, product owner and scrum master", APPROVED),
                         ["business systems analyst", "product owner", "scrum master"])
        self.assertEqual(role_years.conjoined("business systems analyst and/or product owner", APPROVED), [])

    def test_d123_an_alternative_on_a_line_of_its_own(self):
        """D123's check (Medium): an alternative on a line of its own was missed, so the role line rejected: a bullet
        opening with "or", a line that is only a joiner ("OR", "- Or -", HTML <p>OR</p>, in prose), an "Either:" or "One
        of the following:" lead-in. Every years line of that block is CONDITIONAL; a blank line or a heading ends it."""
        for block in (f"- {BSA_PO}\n- or {PO_LINE}", f"- {BSA_PO}\nOR\n- {PO_LINE}", f"- {BSA_PO}\n- Or -\n- {PO_LINE}",
                      f"Either:\n- {BSA_PO}\n- {PO_LINE}", f"One of the following:\n- {BSA_PO}\n- {PO_LINE}",
                      f"{BSA_PO}.\nOR\n{PO_LINE}.", f"Any of the following:\n- {BSA_PO}\n- {PO_LINE}",
                      f"- {BSA_PO}\n- 3+ years of SQL\nOR\n- {PO_LINE}", f"Either:\n{BSA_PO}\n{PO_LINE}",
                      html_to_text(f"<ul><li>{BSA_PO}</li></ul><p>OR</p><ul><li>{PO_LINE}</li></ul>")):
            with self.subTest(block=block):
                self.assertEqual(self.block(block, PO_SIX), OFFERED)
                self.assertEqual(night_years(block + "\n", PO_SIX), OFFERED)
                self.assertEqual(self.block(block, BSA_SIX_PO_HALF), OFFERED)
        for line, opens in (("OR", True), ("- Or -", True), ("**OR**", True), ("- or 5+ years as a PO", True),
                            ("Either:", True), ("One of the following:", True), ("Oregon residents only", False),
                            ("- 5+ years as a BSA; either onsite or remote", False), ("Requirements:", False)):
            with self.subTest(line=line):
                self.assertEqual(role_years.opens_alternatives(line), opens)
        # Controls: two bullets with no joiner still reject. A lead-in in another block of the requirements was a control
        # too until D149 R-Y1: a cue anywhere in the requirements makes a role's far shortfall CONDITIONAL
        self.assertEqual(self.block(f"- {BSA_PO}\n- {PO_LINE}", PO_SIX), OFFERED)
        self.assertEqual(self.block(f"Either:\n- 3+ years of SQL\nAlso required:\n- {BSA_PO}", PO_SIX), OFFERED)
        self.assertEqual(self.block(f"Either:\n- 3+ years of SQL\n\n- {BSA_PO}", PO_SIX), OFFERED)

    def test_d123_a_lead_in_across_blank_lines_or_without_a_colon(self):
        """The check of 5df563c (D123): a lead-in followed by a blank line ("Either:\\n\\n- ...", and the HTML "<p>One of
        the following:</p><ul>...</ul>", which converts so), or one without a colon ("Either of the following", "Must
        meet ONE of the following", "at least one of the following", "one or more of the following", "any of the
        following"), left the role line rejected (live does not reject). The list directly below a lead-in, across blank
        lines, is CONDITIONAL, on both lines."""
        for block in (f"Either:\n\n- {BSA_PO}\n- {PO_LINE}",
                      html_to_text(f"<p>One of the following:</p><ul><li>{BSA_PO}</li><li>{PO_LINE}</li></ul>"),
                      f"Either of the following\n- {BSA_PO}\n- {PO_LINE}", f"Must meet ONE of the following\n- {BSA_PO}\n- {PO_LINE}",
                      f"Candidates need at least one of the following\n\n- {BSA_PO}\n- {PO_LINE}",
                      f"One or more of the following\n- {BSA_PO}\n- {PO_LINE}", f"Any of the following\n\n1. {BSA_PO}\n2. {PO_LINE}"):
            with self.subTest(block=block):
                self.assertEqual(self.block(block, PO_SIX), OFFERED)
                self.assertEqual(night_years(block + "\n", PO_SIX), OFFERED)
                self.assertEqual(self.block(block, BSA_SIX_PO_HALF), OFFERED)
        for line, leads in (("Either of the following", True), ("Must meet ONE of the following", True),
                            ("Either:", True), ("- either onsite or remote", False), ("One of the following: 5+ years", False),
                            ("Our team works with one of the largest health plans", False)):
            with self.subTest(line=line):
                self.assertEqual(role_years.leads_in(line), leads)
        # A lead-in followed by a blank line and an unrelated paragraph leads in nothing, and the list ends at the next
        # blank line; these were controls that rejected until D149 R-Y1: the lead-in is a cue in the requirements, so
        # the role's far shortfall is CONDITIONAL (a needless flag, accepted)
        for block in (f"One of the following:\n\nOur team works remotely across the country.\n- {BSA_PO}",
                      f"Either of the following\n\n- 3+ years of SQL\n\n- {BSA_PO}"):
            with self.subTest(block=block):
                self.assertEqual(self.block(block, PO_SIX), OFFERED)

    def test_d123_joiners_in_punctuation_or_markup(self):
        """D123's check (Medium): a line ending in "or...", "or…", "[OR]", "**or**", "-- or --" was no joiner, so a BSA's 6
        was rejected on the next bullet's Product Owner 0.5. Any punctuation or markup around the "or" is read past."""
        for end in (", or...", ", or…", " [OR]", ", **or**", " -- or --", ", or:", " (or)"):
            with self.subTest(end=end):
                self.assertTrue(role_years.ends_in_joiner(f"- {BSA_PO}{end}"))
                self.assertEqual(self.block(f"- {BSA_PO}{end}\n- {PO_LINE}", BSA_SIX_PO_HALF), OFFERED)
        for end in ("", " (Product Owner)", " for Senior Director"):
            with self.subTest(end=end):
                self.assertFalse(role_years.ends_in_joiner(f"- {BSA_PO}{end}"))

    def test_d123_a_preference_is_flagged_beside_an_unknown_demand(self):
        """D123's check (Medium): on a preferred line with two demands of one figure, the unknown demand (a Product
        Owner nothing records) ranked stricter than the known preferred shortfall, so years_preferred_short was lost
        (live flags it). A preference flags the known shortfall."""
        for block in (f"- {BSA_PO} or, ideally, {PO_LINE}", f"- {BSA_PO} and {PO_LINE}, preferred",
                      f"- {BSA_PO}, ideally with 5+ years as a Scrum Master"):
            with self.subTest(block=block):
                self.assertEqual(self.block(block), ("ELIGIBLE", {"years_preferred_short"}))
        result = gate(TITLE, posting(preferred=[f"{BSA_PO} and {PO_LINE}"]), profile=PERSONA)
        self.assertIn("5+ years as business systems analyst preferred; you have 0.5", detail(result, "years_preferred_short"))
        self.assertEqual(night_years(f"- {BSA_PO} or, ideally, {PO_LINE}\n"), ("ELIGIBLE", {"years_preferred_short"}))

    def test_d123_a_plural_role_names_the_title(self):
        """D123's check (Medium): "5+ years as Business Systems Analysts" did not match the title, so a BSA's 6 was
        rejected on the 0.3-year function (or left unconfirmed without it). A plural names the title."""
        for line in ("5+ years as Business Systems Analysts", "5+ YEARS AS BUSINESS SYSTEMS ANALYSTS"):
            with self.subTest(line=line):
                self.assertEqual(self.outcome(line, BSA_SIX), MET)
                self.assertEqual(self.outcome(line, NO_FUNCTIONS), MET)
                self.assertEqual(night_years(f"- {line}\n", BSA_SIX), MET)
                self.assertEqual(self.outcome(line), OFFERED)   # control: 0.5 as a BSA
        self.assertEqual(PERSONA.years_match("business systems analysts", role_only=True)["kind"], "role_title")

    def test_d123_the_cue_is_no_part_of_the_role_quoted(self):
        """D123's check (Low): the flag quoted the role with its cue ("as business systems analyst, or equivalent"); the
        role is quoted alone, and the cue in its own words."""
        result = judged("8+ years as a Business Systems Analyst, or equivalent", profile=graduate(3, 1))
        found = detail(result, "years_role_unconfirmed")
        self.assertTrue(found.startswith("8+ years as business systems analyst required"), found)
        self.assertIn('("or")', found)
        self.assertEqual(read("- 5+ years as a Business Systems Analyst or similar", 5).phrase(), "as business systems analyst")


# --- D149 (Codex Pass 2 of CR-2026-11-05-COMBINED on 8a3c0c6: F2 B, F3 C, F5 D and the AI-quote High) ----------------
# Structural rules, not wordings: R-Y1 a role, function or activity years demand rejects only when the posting's
# requirements hold no cue of an alternative anywhere (read from the posting text, never an AI quote); R-Y2 a years
# figure on a demanded requirements line that no extracted record carries is to verify; R-Y3 Unicode horizontal spaces
# are ASCII spaces before any parsing. Each reproduction is checked in PRIMARY and NIGHT, beside its controls.
# D151 (red team round 1 of 4199f10, REDTEAM_ROUND1.md Y1-Y4): the exact inputs, run on PRIMARY and NIGHT
RT_Y1 = ["- 10+ years' Product Owner experience", "- 10+ yrs of exp. as PO", "- Product Owner: 10+ years",
         "- Product Owner (10+ years)", "- 10+ years of experience in a Product Owner capacity",
         "- 10+ years of experience serving as Product Owner", "- 10+ years of professional experience as a Product Owner",
         "- 10+ years of professional experience, including as a Product Owner",
         "- Minimum ten (10) years experience as a Product Owner",
         "- 10+ years of overall experience, of which 5 as a Product Owner", "- 10 + years as a Product Owner",
         "- 10+ years’ experience as a Product Owner", "- 10+ years working as a Product Owner",
         "- 10+ years acting as Product Owner", "- At least 10 years as a Product Owner", "- 10+ years as Sr. Product Owner",
         "- 10+ years as a Product Owner/Business Analyst", "- 10+ years of experience in product ownership",
         "- 10+ years of experience, preferably as a Product Owner", "- 10+ years of relevant experience (Product Owner)",
         "- 10+ years of related experience as a Product Owner",
         "- 10+ years of work experience in roles such as Product Owner", "- Product Owner - 10+ years",
         "- Product Owner experience: 10+ years", "- Experience as a Product Owner: 10+ years required",
         "- 10+ years of experience in roles like Product Owner or Scrum Master",
         "- 10+ years of experience in the following role: Product Owner", "- Years as Product Owner: 10+",
         "- 10+ years of experience in a Business Systems Analyst position", "- Experience as a Product Owner: 10+ years",
         "- Product Owner experience required: 10+ years",
         "- Experience as a Business Systems Analyst: 10+ years required",
         "- Experience in claims auditing: 10+ years required", "- Experience as a Product Owner - 10+ years required",
         "- Business Systems Analyst experience: 10+ years", "- Business Systems Analyst: 10+ years",
         "- 10+ years Business Systems Analyst experience", "- 10+ years BSA experience",
         "- Business Systems Analyst (10+ yrs)", "- Business Systems Analyst – minimum 10 years",
         "- Ten-plus (10+) years as a Business Systems Analyst", "- 10+ years of Business Systems Analysis",
         "- Experience in business systems analysis: 10+ years", "- Business systems analysis experience (10+ years)",
         "- Experience leading cross-functional teams: 10+ years", "- Must have 10+ years as Business Systems Analyst",
         "- 10+ years in the role of Business Systems Analyst"]
RT_Y4 = ["- 60+ months as a Business Systems Analyst", "- 10+ YOE as a Business Systems Analyst",
         "- 10+ y.o.e. as a Business Systems Analyst", "- 60 mos as a Business Systems Analyst",
         "- 10 or more years as a Business Systems Analyst", "- Over ten years as a Business Systems Analyst"]
RT_LINE = "- 10+ years as a Business Systems Analyst"
RT_JOINS = ["OR", "- OR: ", "-- or --", "— or —", "Or,", "(alternatively)", "ＯＲ", "o r", "Alternatively, ",
            "In lieu of the above:", "Either of the following is acceptable.", "One of:"]


def rt_ai(quote, years=10, name="business systems analyst", category="years_experience"):
    """A mocked AI years record quoting `quote` (red team Y2)."""
    return {"category": category, "requirement_type": "hard_required", "name": name, "source_text": quote, "years": years,
            "confidence": 0.9}


AI_REQUIRED = ("Required Qualifications:\n- 5+ years as a Business Systems Analyst\nOR\n- 5+ years as a Product Owner\n"
               "- PgMP certification required\n")
NIGHT_TAIL = ("\nSchedule: overnight shift, Sunday to Thursday, 11pm to 7:30am. This role is remote. Back office, "
              "no phone queue.\n")


def ai_years(quote, years, name="business systems analyst"):
    """A synthetic AI record of a years demand, quoting `quote`."""
    return {"category": "years_experience", "requirement_type": "hard_required", "name": name, "source_text": quote,
            "years": years, "confidence": 0.9}


def both_routes(body, profile, raw=None):
    """(PRIMARY state and years codes, NIGHT state and years codes) for a posting body, read by the rules alone or, with
    `raw`, by mocked AI records merged with the rules (careeros.extract.ai.merge)."""
    from careeros.extract.ai import merge
    from careeros.qualify import night
    from careeros.qualify.gate import evaluate

    def requirements(text):
        rules = extract(text, APPROVED)
        if raw is None:
            return rules.requirements
        return merge(raw, text, rules, "ai-d149", APPROVED.get("certifications", default={}) or {}).requirements
    primary_text = posting(extra=body + "\nThis role is remote.")
    primary = evaluate(job(TITLE, primary_text), requirements(primary_text), profile, APPROVED)
    night_text = "About us\nWe are a logistics company. Our team audits shipping claims overnight.\n\n" + body + NIGHT_TAIL
    out = night.assess(job("Claims Quality Auditor", night_text), requirements(night_text), profile, APPROVED, 60.0,
                       now="2026-10-19T12:00:00.000Z")
    return ((state_for(primary, APPROVED), years_codes(primary)),
            (out["state"], {f["code"] for f in out["failures"] + out["flags"] if f["code"].startswith("years")}))


class D149YearsTests(unittest.TestCase):
    def test_d149_r_y1_b_an_alternative_anywhere_in_the_requirements_is_never_a_rejection(self):
        """Codex B: with 0.5 BSA and 6 PO years, "OR:" on the next bullet, and a same-line "Product Owner experience may
        satisfy this requirement", hard-rejected years_far_short on both routes. Siblings: any joiner or substitution
        word, with any punctuation, anywhere in the requirements."""
        bsa = "- 5+ years as a Business Systems Analyst"
        for block in (f"Requirements:\n{bsa}\n- OR: 5+ years as a Product Owner\n",
                      f"Requirements:\n{bsa}; Product Owner experience may satisfy this requirement\n",
                      f"Requirements:\n{bsa}\n-- or --\n- 5+ years as a Product Owner\n",
                      f"Requirements:\n{bsa}\n(or)\n- 5+ years as a Product Owner\n",
                      f"Requirements:\n{bsa}\nAlternatively:\n- 5+ years as a Product Owner\n",
                      f"Requirements:\n{bsa}\n- _or_ 5+ years as a Product Owner\n",
                      f"Requirements:\n{bsa}\n- Product Owner experience satisfies this requirement\n",
                      f"Requirements:\n{bsa}\n- Product Owner experience can be substituted\n",
                      f"Requirements:\n{bsa}\n- 3+ years of SQL\n- Product Owner experience will be accepted instead\n",
                      f"Requirements:\n{bsa}\n\nPreferred qualifications:\n- Jira or Confluence\n"):
            for profile in (PO_SIX, PERSONA):
                with self.subTest(block=block, profile=profile.fingerprint):
                    self.assertEqual(both_routes(block, profile), (OFFERED, OFFERED))
        detail_text = detail(gate(TITLE, posting(extra=f"Requirements:\n{bsa}\n- OR: 5+ years as a Product Owner\n"),
                                  profile=PO_SIX), "years_role_unconfirmed")
        self.assertIn("the requirements may offer an alternative", detail_text)
        self.assertIn('"OR" in "- OR: 5+ years as a Product Owner"', detail_text)
        # Met in the role, it still passes; the cue never makes a pass harder
        self.assertEqual(both_routes(f"Requirements:\n{bsa}; Product Owner experience may satisfy this requirement\n",
                                     BSA_SIX), (MET, MET))
        self.assertEqual(both_routes(f"Requirements:\n{bsa}\n- OR: 5+ years as a Product Owner\n",
                                     owner_of([("business systems analyst", 6), ("product owner", 6)])), (MET, MET))

    def test_d149_r_y1_the_ai_quote_high(self):
        """Codex's third High: a valid AI quote that is not a whole line (the PgMP quote without its "- ") made the gate
        discard the posting's lines, the standalone OR with them, and PRIMARY hard-rejected. Cues are read from the
        posting's own lines, whatever the quotes: rules alone, AI quotes of whole bullets, and quotes without "- "."""
        bullets = [ai_years("- 5+ years as a Business Systems Analyst", 5),
                   ai_years("- 5+ years as a Product Owner", 5, "product owner")]
        bare = [ai_years("5+ years as a Business Systems Analyst", 5), ai_years("5+ years as a Product Owner", 5, "product owner")]
        pgmp = {"category": "certification", "requirement_type": "hard_required", "name": "pgmp certification",
                "confidence": 0.9}
        expected = (("CONDITIONAL", {"years_role_unconfirmed"}), ("CONDITIONAL", {"years_role_unconfirmed"}))
        for raw in (None, bullets + [{**pgmp, "source_text": "- PgMP certification required"}],
                    bullets + [{**pgmp, "source_text": "PgMP certification required"}],
                    bare + [{**pgmp, "source_text": "PgMP certification required"}], bare,
                    [ai_years("Business Systems Analyst", 5)] + bare[1:]):
            with self.subTest(raw=raw):
                self.assertEqual(both_routes(AI_REQUIRED, PO_SIX, raw), expected)
        # The posting's lines are kept for a figure of another kind too: a standalone OR between two bullets the AI quotes
        # without "- " still protects the SQL bullet (8 years of SQL far short; the BSA's 6 meet the other bullet)
        block = "Requirements:\n- 8+ years of SQL\nOR\n- 5+ years as a Business Systems Analyst\n"
        sql = [{"category": "skill", "requirement_type": "hard_required", "name": "sql", "source_text": "8+ years of SQL",
                "years": 8, "confidence": 0.9}, ai_years("5+ years as a Business Systems Analyst", 5)]
        for raw in (None, sql):
            with self.subTest(raw=raw):
                self.assertEqual(both_routes(block, with_sql(BSA_SIX, 0.5), raw), (OFFERED, OFFERED))
        # The same AI quotes on a posting with no cue anywhere: since D149 R-Y0 the role's far shortfall is to verify there
        # too (it rejected in the first D149 commit); a total-years demand with no role still rejects
        plain = "Required Qualifications:\n- 5+ years as a Business Systems Analyst\n- PgMP certification required\n"
        self.assertEqual(both_routes(plain, PO_SIX, [ai_years("5+ years as a Business Systems Analyst", 5),
                                                      {**pgmp, "source_text": "PgMP certification required"}]),
                         (OFFERED, OFFERED))
        total = "Required Qualifications:\n- 10+ years of professional experience\n- PgMP certification required\n"
        self.assertEqual(both_routes(total, PO_SIX, [ai_years("10+ years of professional experience", 10,
                                                              "professional experience"),
                                                     {**pgmp, "source_text": "PgMP certification required"}])[0][0],
                         "INELIGIBLE")

    def test_d149_r_y2_c_a_figure_no_record_carries_is_to_verify(self):
        """Codex C: "5+ years in underwriting with SQL; 2+ years with Excel" kept only the two-year records (SQL took the
        nearest figure and owned the five), and both routes passed silently. Any figure on a demanded requirements line
        that no record carries is now CONDITIONAL (years_role_unconfirmed, quoting the line)."""
        # Siblings: another lead-in, and a skill's own figure the nearest-figure rule gave away (SQL judged on the 2)
        for line in ("5+ years in underwriting with SQL; 2+ years with Excel",
                     "4+ years of experience in underwriting using SQL; 1+ years with Excel",
                     "6+ years in underwriting with SQL and 2+ years with Excel",
                     "3+ years of SQL; 2+ years of Excel", "10+ years with SQL, 2+ years with Excel"):
            with self.subTest(line=line):
                self.assertEqual(both_routes(f"Requirements:\n- {line}\n", PERSONA), (OFFERED, OFFERED))
                result = judged(line)
                found = next(f for f in result.flags if f["code"] == "years_role_unconfirmed")
                self.assertEqual(found["source_text"], f"- {line}")
                self.assertIn("were not read as any requirement", found["detail"])
        # Controls: every figure carried passes; an age is no demand; a preferred line's figure is not material; a
        # plain hard failure elsewhere still rejects
        self.assertEqual(both_routes("Requirements:\n- 3+ years of SQL and 2+ years of Excel\n", PERSONA), (MET, MET))
        self.assertEqual(both_routes("Requirements:\n- Must be at least 18 years old, with 2+ years of SQL\n", PERSONA),
                         (MET, MET))
        self.assertEqual(both_routes("Requirements:\n- 3+ years of SQL\n\nPreferred qualifications:\n"
                                     "- 5+ years in underwriting with SQL; 2+ years with Excel\n", PERSONA), (MET, MET))
        result = judged("5+ years in underwriting with SQL; 2+ years with Excel", "Active Secret clearance required")
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("INELIGIBLE", {"clearance_required"}))

    def test_d149_r_y3_d_a_role_after_any_unicode_space_is_a_role(self):
        """Codex D: "<p>4+ years as&#8239;Product Owner</p>" kept U+202F through the HTML helper, the role was missed,
        and four total years passed both routes silently. Every horizontal space is an ASCII space before parsing."""
        for space in ("&#8239;", "&nbsp;", "&#160;", "&thinsp;", "&#8195;", "&#12288;", "&#8287;"):
            with self.subTest(space=space):
                body = html_to_text(f"<p>Requirements:</p><ul><li>4+ years as{space}Product Owner</li></ul>")
                self.assertEqual(both_routes(body + "\n", PERSONA), (OFFERED, OFFERED))
                body = html_to_text(f"<p>Requirements:</p><ul><li>5+ years as{space}Business Systems Analyst</li></ul>")
                # control: the BSA role, read, far short (CONDITIONAL since R-Y0; missed, total years 4 of 5 would pass)
                self.assertEqual(both_routes(body + "\n", PERSONA), (OFFERED, OFFERED))
        # A stored line that kept such a space (before the sanitizer change) is read the same by the role reader
        self.assertEqual(read("- 4+ years as\u202fProduct Owner", 4).kind, "role")

    def test_d149_r_y0_a_plain_role_demand_far_short_is_to_verify(self):
        """D149 R-Y0 (the follow-up; it replaces "a plain role demand with no cue still rejects"): years in a role, a
        function or an activity never reject on their own, cue or none: CONDITIONAL on PRIMARY and NIGHT; met, it passes.
        Total years (no role named) and a domain far short, and every other hard failure, still reject."""
        block = ("Requirements:\n- 5+ years as a Business Systems Analyst\n- Strong SQL skills\n- Experience with Jira\n\n"
                 "Preferred qualifications:\n- Power BI experience is a plus\n")
        self.assertEqual(both_routes(block, PERSONA), (OFFERED, OFFERED))
        self.assertEqual(both_routes(block, BSA_SIX), (MET, MET))
        for demand in ("5+ years in business systems analysis", "5+ years leading cross-functional teams"):
            with self.subTest(demand=demand):
                self.assertEqual(both_routes(f"Requirements:\n- {demand}\n", owner_of(functions=[
                    ("business systems analysis", 0.3), ("leading cross-functional teams", 0.5)])), (OFFERED, OFFERED))
        # Controls: total professional years with no role, a domain, and another hard failure still reject
        self.assertEqual(both_routes("Requirements:\n- 10+ years of professional experience\n", PERSONA), (SHORT, SHORT))
        self.assertEqual(both_routes("Requirements:\n- 9+ years of healthcare experience\n", PERSONA), (OFFERED, OFFERED))
        result = judged("5+ years as a Business Systems Analyst", "Active Secret clearance required")
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("INELIGIBLE", {"clearance_required"}))

    def test_d149_r_y0_acceptance_in_any_words_on_any_line(self):
        """The siblings the first D149 commit missed: acceptance said in words outside the cue list, on another line, or a
        cue in the Responsibilities section, left the role's far shortfall rejecting. By construction (R-Y0) it is
        CONDITIONAL on both routes."""
        bsa = "- 5+ years as a Business Systems Analyst"
        for block in (f"Requirements:\n{bsa}\n- Product Owner experience works too\n",
                      f"Requirements:\n{bsa}\n- Product Owner experience can replace this\n",
                      f"Requirements:\n{bsa}\n- Product Owner background also fine\n",
                      f"Responsibilities:\n- Work with BAs or POs\n\nRequirements:\n{bsa}\n"):
            with self.subTest(block=block):
                self.assertEqual(both_routes(block, PO_SIX), (OFFERED, OFFERED))

    def test_d149_r_y2_a_years_word_no_record_came_from_is_to_verify(self):
        """R-Y2 by the word (the follow-up): "5 or more years", "Eleven years", "a decade of underwriting" are no figure the
        reader knows, so no years record came from the line and both routes passed silently. A demanded requirements line
        that says year, yr or decade with no years record from it is CONDITIONAL, quoting the line; ages and degree
        lengths are not tenure."""
        for line in ("5 or more years as a Business Systems Analyst", "Eleven years as a Business Systems Analyst",
                     "A decade of underwriting experience", "Several YEARS of claims auditing", "15 yrs in underwriting"):
            with self.subTest(line=line):
                self.assertEqual(both_routes(f"Requirements:\n- {line}\n", PERSONA), (OFFERED, OFFERED))
                found = next(f for f in judged(line).flags if f["code"] == "years_role_unconfirmed")
                self.assertEqual(found["source_text"], f"- {line}")
        for text, says in (("5 or more years", True), ("a decade of underwriting", True), ("Yrs of SQL", True),
                           ("Must be 18 years of age", False), ("at least 21 years old", False), ("21 years or older", False),
                           ("A 4-year degree in finance", False), ("two-year degree or higher", False),
                           ("four year college degree", False), ("Strong SQL skills", False), ("yearly reviews", False),
                           ("Pay range: $78,000 - $92,000 per year", False), ("$45/hr or $90,000 a year", False),
                           ("$90k/yr", False), ("fiscal year-end close", False), ("A year of underwriting experience", True),
                           ("two years in claims", True)):
            with self.subTest(text=text):
                self.assertEqual(mentions_years(text), says)
        # Controls: a degree length and a preferred line are no demand of years; a figure read passes as before
        self.assertEqual(both_routes("Requirements:\n- Two-year degree or higher\n- 3+ years of SQL\n", PERSONA), (MET, MET))
        self.assertEqual(both_routes("Requirements:\n- 3+ years of SQL\nPay range: $78,000 - $92,000 per year\n", PERSONA),
                         (MET, MET))
        self.assertEqual(both_routes("Requirements:\n- 3+ years of SQL\n\nPreferred qualifications:\n"
                                     "- A decade of underwriting experience\n", PERSONA), (MET, MET))

    def test_d149_an_age_is_never_years_of_experience(self):
        """The coordinator's age probe: "at least 21 years old" was a 21-year experience demand, INELIGIBLE years_far_short
        on both routes (on 8a3c0c6 too). A figure followed by "old", "of age", "or older" or "or over", or after "age" or
        "aged", is an age: no years record, no demand, no R-Y2 flag. Control: "21+ years of claims experience" is a
        demand."""
        from careeros.extract.rules import year_spans
        for line in ("Applicants must be at least 21 years old.", "18 years of age or older", "Must be 18+ years old",
                     "aged 18 or over", "Aged 21 years or more", "Must be at least 18 yrs old"):
            with self.subTest(line=line):
                self.assertEqual(year_spans(line), [])
                self.assertFalse(mentions_years(line))
                self.assertEqual(both_routes(f"Requirements:\n- {line}\n", PERSONA), (MET, MET))
        self.assertEqual(year_spans("21+ years of claims experience"), [(0, 9, 21.0, None)])
        # (a domain demand, to verify since D155 (1)); a total-years demand of 21 is still a demand that rejects
        self.assertEqual(both_routes("Requirements:\n- 21+ years of claims experience\n", PERSONA), (OFFERED, OFFERED))
        self.assertEqual(both_routes("Requirements:\n- 21+ years of professional experience\n", PERSONA), (SHORT, SHORT))

    def test_d151_a_y1_a_role_shaped_name_is_never_judged_on_other_years(self):
        """Red team Y1: a role or function the reader does not read ("Product Owner: 10+ years", "Business Systems Analyst
        (10+ yrs)", "roles such as Product Owner") was judged on the record's name ("professional experience"): rejected
        on 4 total years, or ELIGIBLE silently on 12. D151 (a): only a demand whose own words name total experience, a
        domain or a tool may reject or pass on those years; any other is CONDITIONAL, on 4, 12 and 0.5-PO records alike."""
        vet_po = Profile(data={**BOTH_HALF.data, "total_professional_years": 12}, fingerprint="d151-vet-po")
        for line in RT_Y1:
            for profile in (PERSONA, vet_po):
                with self.subTest(line=line, profile=profile.fingerprint):
                    for state, codes in both_routes(f"Requirements:\n{line}\n", profile):   # PRIMARY, NIGHT
                        self.assertEqual(state, "CONDITIONAL")
                        self.assertTrue(codes and codes <= {"years_role_unconfirmed", "years_unknown"}, codes)
        for text, plain in (("- 10+ years of professional experience", True), ("- 10+ years of healthcare experience", True),
                            ("- 9+ years of PBM experience", True), ("- 10+ years of experience with SQL", True),
                            ("- 15+ years of Excel", True), ("- Product Owner: 10+ years", False),
                            ("- 10+ years of work experience in roles such as Product Owner", False),
                            ("- 5+ years of healthcare experience as a team member", True)):
            with self.subTest(text=text):
                figure = float(re.search(r"\d+", text).group(0))
                self.assertEqual(role_years.plain_demand(text, figure, APPROVED), plain)

    def test_d151_b_y2_an_ai_reading_alone_never_rejects(self):
        """Red team Y2: an AI record quoting part of the line ("10+ years", "Business Systems Analyst") replaced the rules'
        record and lost the role: INELIGIBLE, or ELIGIBLE silently on 12 total years. The role is read on the posting line
        the quote comes from (D151 (a)), and only a demand the rules extractor read from the posting line may reject
        (D151 (b))."""
        raws = [[rt_ai("10+ years")], [rt_ai("10+ years", name="professional experience")], [rt_ai("10+ years as a")],
                [rt_ai("years as a Business Systems Analyst")], [rt_ai("Business Systems Analyst")],
                [rt_ai(RT_LINE, name="professional experience")], [rt_ai(RT_LINE[2:], name="business analysis")],
                [rt_ai("10+ years", name="healthcare", category="domain")], [rt_ai(RT_LINE[2:], years=1)],
                [rt_ai(RT_LINE[2:], years=None)]]
        vet12 = Profile(data={**PERSONA.data, "total_professional_years": 12}, fingerprint="d151-vet12")
        for raw in raws:
            for profile in (PERSONA, vet12):
                with self.subTest(raw=raw, profile=profile.fingerprint):
                    self.assertEqual(both_routes(f"Requirements:\n{RT_LINE}\n- Strong SQL skills\n", profile, raw),
                                     (OFFERED, OFFERED))
        # (b) by itself: a plain total-years demand only the AI read (a line in the company section, which the rules
        # reader skips) never rejects; the same demand read by the rules still does
        intro = "10+ years of professional experience\n\nRequirements:\n- Strong SQL skills\n"
        raw = [rt_ai("10+ years of professional experience", name="professional experience")]
        self.assertEqual(both_routes(intro, PERSONA, raw)[0][0], "CONDITIONAL")
        self.assertTrue(role_years.plain_demand("10+ years of professional experience", 10, APPROVED))   # (a) holds
        # (the AI's own record, quoting the line without its "- ", is a demand of its own that only flags)
        rejected = both_routes("Requirements:\n- 10+ years of professional experience\n", PERSONA,
                               [rt_ai("10+ years of professional experience", name="professional experience")])
        for state, codes in rejected:
            self.assertEqual((state, "years_far_short" in codes), ("INELIGIBLE", True))

    def test_d151_c_y3_a_cue_anywhere_protects_every_years_demand(self):
        """Red team Y3: an alternative on the next bullet ("OR:", "Alternatively, ", "In lieu of the above:", "ＯＲ", "o r",
        "One of:") left a domain, total-years or tool demand far short rejecting, though the alternative (BSA 6) is met.
        D151 (c): the requirements-wide cue applies to every years demand."""
        for first in ("- 10+ years of healthcare experience", "- 10+ years of professional experience", "- 10+ years of SQL"):
            for join in RT_JOINS:
                if join.endswith(" ") or (join.endswith(":") and join.startswith("-")):
                    body = f"Requirements:\n{first}\n{join}5+ years as a Business Systems Analyst\n"
                else:
                    body = f"Requirements:\n{first}\n{join}\n- 5+ years as a Business Systems Analyst\n"
                with self.subTest(first=first, join=join):
                    self.assertEqual([state for state, _ in both_routes(body, with_sql(BSA_SIX, 0.5))],
                                     ["CONDITIONAL", "CONDITIONAL"])
        # Controls: total years far short, with no cue anywhere, still reject on both routes; since D155 (1) a domain or a
        # tool far short is to verify
        for line in ("- 10+ years of professional experience", "- 10+ years of experience"):
            with self.subTest(control=line):
                self.assertEqual(both_routes(f"Requirements:\n{line}\n", PERSONA), (SHORT, SHORT))
        for line, profile in (("- 10+ years of healthcare experience", PERSONA), ("- 9+ years of PBM experience", PERSONA),
                              ("- 10+ years of SQL", with_sql(PERSONA, 0.5)), ("- 15+ years of Excel", PERSONA),
                              ("- 10+ years of experience with SQL", with_sql(PERSONA, 0.5))):
            with self.subTest(domain_or_tool=line):
                self.assertEqual(both_routes(f"Requirements:\n{line}\n", profile), (OFFERED, OFFERED))

    def test_d151_y4_months_and_yoe_are_years_words(self):
        """Red team Y4: "60+ months as a BSA" and "10+ YOE as a BSA" were read by nothing and passed silently. Months,
        "mos", "mo.", "YOE" and "y.o.e." are tenure words for R-Y2: CONDITIONAL on both routes; a month as a rate or a
        deadline is not."""
        vet12 = Profile(data={**PERSONA.data, "total_professional_years": 12}, fingerprint="d151-vet12")
        for line in RT_Y4:
            for profile in (PERSONA, vet12):
                with self.subTest(line=line, profile=profile.fingerprint):
                    self.assertEqual(both_routes(f"Requirements:\n{line}\n", profile), (OFFERED, OFFERED))
        for text, says in (("60+ months as a BSA", True), ("10+ YOE", True), ("10 y.o.e.", True), ("60 mos in claims", True),
                           ("$7,000 per month", False), ("certification within 6 months of hire", False),
                           ("$7,000/mo", False)):
            with self.subTest(text=text):
                self.assertEqual(mentions_years(text), says)

    def test_d149_r_y3_a_zero_width_separator_is_a_space(self):
        """The follow-up: "as&#8203;Product Owner" lost its zero-width space as a format mark ("asProduct Owner"), the
        role was missed and four total years passed. Zero-width separators are spaces; U+2028/U+2029 end a line."""
        for mark in ("&#8203;", "&#8288;", "&#65279;"):
            with self.subTest(mark=mark):
                body = html_to_text(f"<p>Requirements:</p><ul><li>4+ years as{mark}Product Owner</li></ul>")
                self.assertEqual(both_routes(body + "\n", PERSONA), (OFFERED, OFFERED))
        self.assertIsNone(role_years.section_cue(["Requirements:", "- 5+ years as a Business Systems Analyst",
                                                  "- Oregon residents only", "- Strong ordering and coordination skills"]))
        for line in ("OR", "- OR: 5+ years", "(or)", "-- or --", "_or_", "Alternatively:", "in lieu of a degree",
                     "may satisfy this", "is accepted", "will be considered", "equivalent experience", "and/or", "Either:",
                     "One of the following", "can substitute", "PO experience in place of BSA experience"):
            with self.subTest(line=line):
                self.assertIsNotNone(role_years.section_cue([line]))


# --- The D152 hand check of integration d7c1559 (2026-11-13): years defects that survived under D150/D151 ---------------
# Each test pins one rule whose effect D151 now mostly hides behind the same CONDITIONAL: by the state where an input still
# tells them apart, else by the flag's own words (which rule made the line CONDITIONAL).
# --- D155 (Codex Pass 3 on adeb243, the owner's blunt fail-safes): F2, F3 and the changed-path Highs 1 and 4 ------------
SQL3 = with_sql(PERSONA, 3)   # synthetic: 3 SQL years, 10 Excel years, 4 total years


def ai_skill(name, quote, years=10, kind="hard_required", category="skill"):
    """A mocked AI years record (Codex's inversions)."""
    return {"category": category, "requirement_type": kind, "name": name, "source_text": quote, "years": years,
            "confidence": 0.9}


class D155YearsTests(unittest.TestCase):
    def test_d155_1_only_total_professional_years_reject(self):
        """Codex F2: "10+ years of SQL" then "Excel is an alternative" (or "Excel can replace SQL") hard-rejected on 3 SQL
        years though the profile holds 10 Excel years. D155 (1): only a demand of total professional years (no domain,
        tool, role or function named) may reject; a domain or a tool far short is CONDITIONAL, whatever follows it."""
        for second in ("Excel is an alternative", "Excel can replace SQL", "Excel may be used in place of SQL",
                       "Strong Excel skills", "Tableau works too"):
            body = f"Required Qualifications:\n- 10+ years of SQL\n- {second}\n"
            with self.subTest(second=second):
                self.assertEqual(both_routes(body, SQL3), (OFFERED, OFFERED))
        for line, profile in (("- 10+ years of healthcare experience", PERSONA), ("- 12+ years of PBM experience", PERSONA),
                              ("- 15+ years of Excel", PERSONA), ("- 10+ years of SQL", SQL3)):
            with self.subTest(line=line):
                self.assertEqual(both_routes(f"Required Qualifications:\n{line}\n", profile), (OFFERED, OFFERED))
                self.assertIn("only total professional years reject on their own (D155)",
                              detail(gate(TITLE, posting(extra=f"Required Qualifications:\n{line}\n\nThis role is remote."),
                                          profile=profile), "years_role_unconfirmed"))
        # Controls: total professional years far short still reject; met, they pass
        for line in ("- 10+ years of professional experience", "- Minimum 10 years of experience", "- 10+ years of experience"):
            with self.subTest(total=line):
                self.assertEqual(both_routes(f"Required Qualifications:\n{line}\n", PERSONA), (SHORT, SHORT))
        self.assertEqual(both_routes("Required Qualifications:\n- 3+ years of professional experience\n", PERSONA), (MET, MET))
        self.assertTrue(role_years.total_demand("- 10+ years of professional experience", 10, APPROVED))
        self.assertFalse(role_years.total_demand("- 10+ years of healthcare experience", 10, APPROVED))
        self.assertFalse(role_years.total_demand("- 10+ years of SQL", 10, APPROVED))

    def test_d155_2_an_ai_record_never_replaces_or_hides_a_rules_record(self):
        """Codex's AI record identity High: an AI hard SQL record quoting the Excel line produced a hard SQL shortfall,
        and an AI Excel record quoting the SQL line a silent pass; an AI contextual SQL record suppressed the hard rules
        record. The merge keeps every rules years record; a line is judged on the rules' own records (whole identity),
        and only flags where the AI reads it otherwise; an AI record never rejects."""
        excel, sql = "- 10+ years of Excel", "- 10+ years of SQL"
        for line, raw in ((excel, [ai_skill("sql", excel[2:])]), (excel, [ai_skill("sql", excel)])):
            with self.subTest(line=line, raw=raw):
                routes = both_routes(f"Required Qualifications:\n{line}\n", SQL3, raw)
                self.assertNotIn("INELIGIBLE", [state for state, _ in routes])   # never a hard SQL shortfall
        for raw in ([ai_skill("excel", sql)], [ai_skill("excel", sql[2:])], [ai_skill("sql", sql, kind="contextual")],
                    [ai_skill("sql", sql[2:], kind="preferred")]):
            with self.subTest(raw=raw):
                self.assertEqual([state for state, _ in both_routes(f"Required Qualifications:\n{sql}\n", SQL3, raw)],
                                 ["CONDITIONAL", "CONDITIONAL"])   # never a silent pass on the AI's Excel
        # The flag says the readings differ, on the rules' line
        result_text = posting(extra=f"Required Qualifications:\n{excel}\n\nThis role is remote.")
        from careeros.extract.ai import merge
        from careeros.qualify.gate import evaluate
        reqs = merge([ai_skill("sql", excel)], result_text, extract(result_text, APPROVED), "ai-d155", {}).requirements
        self.assertIn(("skill", "excel", 10.0), {(r.category, r.name, r.years) for r in reqs})   # the rules' record kept
        found = detail(evaluate(job(TITLE, result_text), reqs, SQL3, APPROVED), "years_role_unconfirmed")
        self.assertIn("the AI read this line as sql", found)
        # Controls: a total-years demand the rules read still rejects whatever the AI adds beside it (hard Excel, or a
        # contextual copy), and the AI's own agreeing record changes nothing
        total = "- 10+ years of professional experience"
        for raw in ([ai_skill("excel", total)], [ai_skill("professional experience", total, kind="contextual",
                                                          category="years_experience")],
                    [ai_skill("professional experience", total, category="years_experience")]):
            with self.subTest(total_raw=raw):
                for state, codes in both_routes(f"Required Qualifications:\n{total}\n", PERSONA, raw):
                    self.assertEqual((state, "years_far_short" in codes), ("INELIGIBLE", True))

    def test_d155_3_each_figure_carried_by_its_own_record(self):
        """Codex F3: "2+ years in underwriting with SQL; 2+ years with Excel": SQL and Excel took the two 2s, so the set of
        amounts looked carried, and the underwriting demand passed silently. Each figure needs a record of its own
        subject (the skill or domain its phrase opens with), else one that is no skill; a record carries one figure."""
        sql6 = with_sql(PERSONA, 6)
        for line in ("2+ years in underwriting with SQL; 2+ years with Excel",
                     "2+ years of actuarial modeling with SQL; 2+ years with Excel",
                     "2+ years in underwriting with SQL and 2+ years of Excel"):
            with self.subTest(line=line):
                self.assertEqual([state for state, _ in both_routes(f"Required Qualifications:\n- {line}\n", sql6)],
                                 ["CONDITIONAL", "CONDITIONAL"])
                self.assertIn("were not read as any requirement", " ".join(
                    f["detail"] for f in judged(line, profile=sql6).flags))
        # Controls: each figure carried by its own subject's record; a role figure judged on the role
        for line in ("2+ years with SQL; 2+ years with Excel", "3+ years of SQL and 2+ years of Excel",
                     "5+ years in healthcare, including 2+ years with SQL"):
            with self.subTest(control=line):
                self.assertEqual(both_routes(f"Required Qualifications:\n- {line}\n", graduate(6, 6) if "health" in line
                                             else sql6)[0], MET)
        self.assertEqual(both_routes("Required Qualifications:\n- 5+ years as a Business Systems Analyst; SQL is a must\n",
                                     BSA_SIX)[0], MET)

    def test_d155_4_for_n_months_is_tenure(self):
        """Codex's months High: "Must have worked for 60 months as a Business Systems Analyst." passed silently (the month
        detector took "for 60 months" as an incidental duration). "for N months" and "N months of" are tenure; only a rate
        or a deadline is not."""
        for line, profile in (("Must have worked for 60 months as a Business Systems Analyst.", PERSONA),
                              ("Must have worked for 60 months as a Business Systems Analyst.", BSA_SIX),
                              ("Worked for 36 months in claims auditing", PERSONA),
                              ("60 months of experience as a Business Systems Analyst", PERSONA)):
            with self.subTest(line=line, profile=profile.fingerprint):
                self.assertEqual([state for state, _ in both_routes(f"Required Qualifications:\n- {line}\n", profile)],
                                 ["CONDITIONAL", "CONDITIONAL"])
        # Control: "N months of experience" is read as years (60 months = 5), so six years as a BSA meet it
        self.assertEqual(both_routes("Required Qualifications:\n- 60 months of experience as a Business Systems Analyst\n",
                                     BSA_SIX), (MET, MET))
        for text, says in (("worked for 60 months", True), ("60 months of experience", True), ("$7,000 per month", False),
                           ("$7,000 a month", False), ("$7,000/mo", False), ("within 6 months of hire", False),
                           ("after 6 months", False), ("for the first 6 months of employment", False)):
            with self.subTest(text=text):
                self.assertEqual(mentions_years(text), says)


# --- The hand check of 489469b (D155): survivors masked once domains and tools never reject ------------------------------
# Only a total-years demand still rejects (D155 (1)), so each test pins its rule on one: a figure stated for a domain and
# again for total years ("9+ years healthcare experience and 9+ years of professional experience"), with a record of 9
# healthcare years and 4 total years.
HC9 = Profile(data={**PERSONA.data, "domains": [{"name": n, "years": 9} for n in ("healthcare", "pbm", "claims")]},
              fingerprint="d155-hc9")


class D155SurvivorTests(unittest.TestCase):
    def test_d155_a_rejection_shows_the_demand_rejected_on(self):
        """D120 (hand check of 489469b): of one figure's two demands, the total-years one (the second) is rejected on; the
        rejection's Experience Match is that demand's (total years, 4), never the first demand's (healthcare, 9)."""
        result = judged("9+ years healthcare experience and 9+ years of professional experience", profile=HC9)
        failure = next(f for f in result.hard_failures if f["code"] == "years_far_short")
        self.assertEqual((state_for(result, APPROVED), failure["detail"]), ("INELIGIBLE", "9+ years of professional required; you have 4"))
        self.assertEqual((failure["experience"]["area"], failure["experience"]["counted_years"]), ("professional", 4.0))

    def test_d155_a_cue_between_two_demands_of_one_figure_never_rejects(self):
        """D120 check and D123 R4 (hand check of 489469b): a cue between the two demands ("(hospital/payer)", no cue word in
        the requirements) may make them alternatives, so the total-years shortfall flags, never rejects; the cue is read
        between the demands, not after the last. Control: without the cue, it rejects."""
        result = judged("9+ years healthcare experience (hospital/payer) and 9+ years of professional experience", profile=HC9)
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertIn("may offer another figure", detail(result, "years_role_unconfirmed"))
        self.assertEqual(failures(judged("9+ years healthcare experience and 9+ years of professional experience",
                                         profile=HC9)), {"years_far_short"})

    def test_d155_an_ai_partial_quote_is_read_on_its_posting_line(self):
        """D151 Y2 (hand check of 489469b): an AI record quoting part of a line ("10+ years" of "- 10+ years as a Business
        Systems Analyst") is read on the posting line it comes from, so the role around it is kept: with 12 total years
        and 0.5 as a BSA, its own demand is the role's, to verify, never met on the 12 total years. Since D155 (2) the
        rules' record of the line is judged beside it, so the posting is CONDITIONAL either way; the AI record's own flag
        shows the reading."""
        from careeros.extract.ai import merge
        from careeros.qualify.gate import evaluate
        vet12 = Profile(data={**PERSONA.data, "total_professional_years": 12}, fingerprint="d155-vet12")
        text = posting(extra="Required Qualifications:\n- 10+ years as a Business Systems Analyst\n- Strong SQL skills\n\n"
                             "This role is remote.")
        raw = [ai_skill("professional experience", "10+ years", category="years_experience")]
        reqs = merge(raw, text, extract(text, APPROVED), "ai-d155y2", {}).requirements
        result = evaluate(job(TITLE, text), reqs, vet12, APPROVED)
        found = [f["detail"] for f in result.flags if f["code"] == "years_role_unconfirmed" and f["source_text"] == "10+ years"]
        self.assertTrue(found and "as business systems analyst" in found[0], result.flags)

    def test_d155_an_ai_partial_quote_never_discards_the_posting_lines(self):
        """D149's AI-quote High (hand check of 489469b): AI records quoting parts of lines never make the gate read the
        requirements' own quotes instead of the posting's lines. Since D155 (2) the rules' records keep the lines in view
        whenever the rules read any, so only a posting the rules read nothing from ("Ten-plus years ...") shows it: the
        standalone OR stays the total-years quote's own cue ("a line after it"), not only the requirements-wide one."""
        from careeros.extract.ai import merge
        from careeros.qualify.gate import evaluate
        text = posting(extra="Required Qualifications:\n- Ten-plus years of professional experience\nOR\n"
                             "- Five-plus years as a Business Systems Analyst\n\nThis role is remote.")
        raw = [ai_skill("professional experience", "Ten-plus years of professional experience", years=10,
                        category="years_experience"),
               ai_skill("business systems analyst", "Five-plus years as a Business Systems Analyst", years=5,
                        category="years_experience")]
        rules_only = extract(text, APPROVED)
        self.assertFalse([r for r in rules_only.requirements if r.years])   # the rules read no figure here
        reqs = merge(raw, text, rules_only, "ai-d155s", {}).requirements
        result = evaluate(job(TITLE, text), reqs, PERSONA, APPROVED)
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        found = next(f["detail"] for f in result.flags if f["code"] == "years_role_unconfirmed"
                     and f["source_text"] == "Ten-plus years of professional experience")
        self.assertIn('("or (a line after it)")', found)


class D152SurvivorTests(unittest.TestCase):
    def block(self, block, profile):
        return gate(TITLE, posting(extra=f"Requirements:\n{block}\n\nThis role is remote."), profile=profile)

    def test_d152_a_line_every_reading_rejects_is_no_split(self):
        """#1059: a role line read two ways (BSA years or healthcare years) where both readings are far short is no
        "the two readings judge it differently" flag: the role's own shortfall is to verify (R-Y0)."""
        found = detail(judged("5+ years in healthcare as a Business Systems Analyst", profile=NOVICE), "years_role_unconfirmed")
        self.assertNotIn("read another way", found)
        self.assertIn("a shortfall in a role, function or activity is never a rejection", found)

    def test_d152_a_soft_line_never_rejects_so_never_splits(self):
        """#1065: a soft line ("At least ..." under Preferred) can never reject, so its two readings (BSA 0.5, healthcare
        4) judge it alike: the flag is the D151 gap flag, never "the two readings judge it differently"."""
        result = gate(TITLE, posting(preferred=["At least 5 years of healthcare experience as a Business Systems Analyst"]),
                      profile=PERSONA)
        found = detail(result, "years_role_unconfirmed")
        self.assertNotIn("read another way", found)
        self.assertIn("never a rejection on its own (D151)", found)

    def test_d152_a_figure_added_beside_a_skill_never_rejects_alone(self):
        """#1075 (D115): on a line that names a skill, a figure only a years_experience record carries ("10+ years of
        experience, including 2+ years with SQL") may be the skill's own: it flags, never rejects on its own."""
        result = judged("10+ years of experience, including 2+ years with SQL")
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertIn("may offer another figure", detail(result, "years_role_unconfirmed"))

    def test_d152_one_figure_named_twice_as_alternatives_never_rejects(self):
        """#1076 (D115): one figure named twice, joined by "/" (no cue word in the requirements), is one demand judged on
        its first phrase, and flags at most."""
        result = judged("5+ years SQL / 5+ years Python", profile=skilled(sql=0.5))
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertIn("may offer another figure", detail(result, "years_role_unconfirmed"))

    def test_d152_an_or_before_an_and_ends_at_the_and(self):
        """#1085 (D113 #503): the "or" in "3+ years of claims or billing experience and 9+ years of healthcare" never
        reaches past the "and". Only an AI reading of a company-section line, outside every requirements cue, tells it
        apart: its 9 years are to verify because only the AI read them (D151 (b)), not as another figure."""
        line = "3+ years of claims or billing experience and 9+ years of healthcare experience"
        raw = [{"category": "years_experience", "requirement_type": "hard_required", "name": "claims", "years": 3,
                "source_text": line, "confidence": 0.9},
               {"category": "domain", "requirement_type": "hard_required", "name": "healthcare", "years": 9,
                "source_text": line, "confidence": 0.9}]
        from careeros.extract.ai import merge
        from careeros.qualify.gate import evaluate
        text = posting(extra=f"{line}\n\nRequirements:\n- Strong SQL skills\n\nThis role is remote.")
        result = evaluate(job(TITLE, text), merge(raw, text, extract(text, APPROVED), "ai-d152", {}).requirements, PERSONA,
                          APPROVED)
        found = detail(result, "years_role_unconfirmed")
        self.assertTrue(found.startswith("9+ years of healthcare required (you have 4)"), found)
        # (since D155 (1) the domain's own reason comes first; never "may offer another figure")
        self.assertIn("it names a domain or a tool", found)
        self.assertNotIn("may offer another figure", found)

    def test_d152_a_role_without_its_article_after_its_field_is_read(self):
        """#1113 (Codex F5): "healthcare experience as Business Systems Analyst" (no article) reads the role after its
        field, as with the article: six years as a BSA in healthcare pass; read as healthcare years only, the role's words
        would make the line to verify."""
        reading = read("- 5+ years of healthcare experience as Business Systems Analyst", 5)
        self.assertEqual((reading.kind, reading.area, reading.place), ("role", "business systems analyst", "healthcare"))
        self.assertEqual(state_for(judged("5+ years of healthcare experience as Business Systems Analyst", profile=SENIOR),
                                   APPROVED), "ELIGIBLE")

    def test_d152_demands_of_one_figure_with_a_cue_between_never_reject(self):
        """#1232, #1244 (D123 R4): "8+ years as a BSA/Product Owner and 8+ years of healthcare": the "/" between the two
        demands (no cue word in the requirements) may make them alternatives, so the healthcare shortfall (2 of 8) flags,
        never rejects; the cue is read between the demands, not after the last."""
        result = judged("8+ years as a Business Systems Analyst/Product Owner and 8+ years of healthcare experience",
                        profile=graduate(9, 2))
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertIn("8+ years of healthcare required (you have 2)", detail(result, "years_role_unconfirmed"))

    def test_d152_a_lead_in_followed_by_prose_leads_in_nothing(self):
        """D123 check 2 (the check of 5df563c): a lead-in followed by a paragraph of prose ("One of the following:", a
        blank line, "Our team works remotely ...") leads in no list, so the bullet after the prose is not cued by it.
        The role line is CONDITIONAL all the same (R-Y0 and the requirements-wide cue); the flag names the
        requirements-wide cue, never "a lead-in above it"."""
        block = ("One of the following:\n\nOur team works remotely across the country.\n"
                 "- 5+ years as a Business Systems Analyst")
        found = next(f["detail"] for f in self.block(block, PO_SIX).flags if f["code"] == "years_role_unconfirmed"
                     and f["source_text"] == "- 5+ years as a Business Systems Analyst")
        self.assertNotIn("a lead-in above it", found)
        self.assertIn('the requirements may offer an alternative ("One of the following"', found)

    def test_d152_a_second_figure_under_one_name_never_rejects_alone(self):
        """#1086 (the fix-check of f74cdcc, D113): a second figure under the same name on one line ("3+ years of
        professional experience; 9+ years of professional experience", "Senior designers bring 3+ ..., Staff designers
        bring 8+ ...") may be an alternative level, not a demand of its own: it flags, never rejects on its own, though its
        words are a plain total (D151 (a) would otherwise allow the rejection)."""
        line = "3+ years of professional experience; 9+ years of professional experience"
        result = judged(line)
        self.assertEqual((state_for(result, APPROVED), failures(result)), ("CONDITIONAL", set()))
        self.assertIn("may offer another figure", detail(result, "years_role_unconfirmed"))
        self.assertEqual(night_years(f"- {line}\n"), ("CONDITIONAL", {"years_role_unconfirmed"}))
        # Control: the higher figure alone is a plain total demand far short, and rejects
        self.assertEqual(self.outcomes("9+ years of professional experience"), ("INELIGIBLE", {"years_far_short"}))

    def outcomes(self, line):
        result = judged(line)
        return state_for(result, APPROVED), years_codes(result)

    def test_d152_the_block_and_lead_in_cues_are_named_in_the_flag(self):
        """#1259, #1260, #1264, #1265 (D123's checks): every role line here is CONDITIONAL by R-Y0 and the
        requirements-wide cue anyway; the flag still says which cue made it so, and that is where the readers differ:
        a heading ends a block (the lead-in above it is no cue of the next section's line), a joiner line cues every line
        of its block, and the list below a lead-in, across a blank line, is cued by it."""
        bsa, po = "5+ years as a Business Systems Analyst", "5+ years as a Product Owner"
        for block, cue in ((f"Either:\n- 3+ years of SQL\nAlso required:\n- {bsa}", 'the requirements may offer an alternative ("Either"'),
                           (f"- {bsa}\n- 3+ years of SQL\nOR\n- {po}", '("or (a line of its block)")'),
                           (f"Either:\n\n- {bsa}\n- {po}", '("one of the following (a lead-in above it)")')):
            with self.subTest(block=block):
                found = next(f["detail"] for f in self.block(block, PO_SIX).flags if f["code"] == "years_role_unconfirmed"
                             and f["source_text"] == f"- {bsa}")
                self.assertIn(cue, found)


# --- The integration hand check of a0a83b2 (2026-11-12): eleven role-years defects survived because the D123 rule made
# their D113/D115/Codex F2-F4 tests' lines CONDITIONAL whatever the defect did. Each rule still decides on lines without
# a role's cue (skills, domains, a preferred section's soft lines) or in a flag's words; each test below pins one.
def skilled(**years):
    """The persona with exactly these skills (synthetic)."""
    return Profile(data={**PERSONA.data, "skills": [{"name": n, "years": y, "evidence": "direct"} for n, y in years.items()]},
                   fingerprint=f"handcheck-{sorted(years.items())}")


def required(line, profile=PERSONA):
    """PRIMARY and NIGHT on one Requirements bullet: (state, years codes) each."""
    result = judged(line, profile=profile)
    return (state_for(result, APPROVED), years_codes(result)), night_years(f"- {line}\n", profile)


class HandCheckA0a83b2Tests(unittest.TestCase):
    def test_a_soft_line_with_two_readings_never_rejects_so_never_splits(self):
        """D113 (hand check #483): a line typed soft ("At least ..." in a preferred section) can never be a rejection, so
        its two readings judge it alike: the gap flag, ELIGIBLE. A hard line with the same words is CONDITIONAL."""
        line = "At least 5 years of healthcare experience as a Business Systems Analyst"
        result = gate(TITLE, posting(preferred=[line]), profile=PERSONA)
        self.assertEqual((state_for(result, APPROVED), years_codes(result)), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(night_years(f"- 3+ years of Excel\n\nPreferred qualifications:\n- {line}\n"), ("CONDITIONAL", {"years_role_unconfirmed"}))
        self.assertEqual(required(line)[0], ("CONDITIONAL", {"years_role_unconfirmed"}))   # control: hard

    def test_one_skill_figure_named_twice_as_alternatives_flags(self):
        """D115 (hand check #494): "5+ years of SQL or 5+ years of Python" is one demand, judged on its first phrase: it
        flags, never rejects on its own. The control without the second phrase rejects."""
        self.assertEqual(required("5+ years of SQL or 5+ years of Python", skilled(sql=0.5)),
                         (("CONDITIONAL", {"years_role_unconfirmed"}),) * 2)
        # (since D151 (c) the "or" is also a cue in the requirements, whose flag is shown first)
        self.assertIn("may offer", detail(judged("5+ years of SQL or 5+ years of Python", profile=skilled(sql=0.5)),
                                          "years_role_unconfirmed"))
        self.assertEqual(required("5+ years of SQL", skilled(sql=0.5)), (("CONDITIONAL", {"years_role_unconfirmed"}),) * 2)

    def test_skill_figures_joined_by_or_are_judged_on_the_best(self):
        """D113 (hand check #497, #499): figures of skills joined by "or" are judged once, on the best alternative: one
        that passes (Excel 10) before one slightly short (SQL 3), one nothing records (CONDITIONAL) before one that
        rejects (SQL 0.5). Controls: each figure alone."""
        line = "5+ years of SQL or 3+ years of Excel"
        self.assertEqual(required(line), (("ELIGIBLE", set()),) * 2)
        self.assertEqual(required("5+ years of SQL"), (("ELIGIBLE", {"years_slightly_short"}),) * 2)
        self.assertEqual(required(line, skilled(sql=0.5)), (("CONDITIONAL", {"years_unknown"}),) * 2)
        self.assertEqual(required(line, skilled(sql=0.5, excel=10)), (("ELIGIBLE", set()),) * 2)

    def test_joiners_that_only_qualify_the_next_skill_figure(self):
        """D113 (hand check #498, #500, #501): "or min.", "/", "or more than", "or, alternatively," before a figure join
        it as an alternative; the second figure is never judged as a demand of its own (no years_unknown, no gap flag
        beside the first). Control: a semicolon makes two demands."""
        for line in ("5+ years of SQL or min. 3 years of Excel", "5+ years of SQL or more than 3 years of Excel",
                     "5+ years of SQL or, alternatively, 3+ years of Excel"):
            with self.subTest(line=line):
                self.assertNotIn("years_unknown", years_codes(judged(line, profile=skilled(sql=0.5))))
        self.assertEqual(years_codes(judged("5+ years as a Business Systems Analyst / 3+ years with SQL",
                                            profile=skilled(sql=0.5, excel=10))), {"years_role_unconfirmed"})
        # the semicolon control: SQL's own gap is judged (since D155 (1) a tool's gap is to verify, in its own words)
        self.assertIn("3+ years of sql required (you have 0.5); it names a domain or a tool", " ".join(
            f["detail"] for f in judged("5+ years as a Business Systems Analyst; 3+ years with SQL",
                                        profile=skilled(sql=0.5, excel=10)).flags))

    def test_an_or_before_an_and_does_not_reach_the_next_figure(self):
        """D113 (hand check #503): in "3+ years of SQL or Excel and 9+ years of healthcare experience" the "or" joins SQL
        and Excel; the "and" ends it, so the 9-year demand is no alternative. Since D151 (c) the "or" is a cue in the
        requirements, so its shortfall is to verify; without the "or" ("SQL and Excel") it still rejects."""
        self.assertEqual(required("3+ years of SQL or Excel and 9+ years of healthcare experience", skilled(sql=0.5))[0][0],
                         "CONDITIONAL")
        # (since D155 (1) a domain far short is to verify; the "and" control is told apart by its flag's words: the
        # domain's own reason, never "may offer another figure")
        found = " ".join(f["detail"] for f in judged("3+ years of SQL and Excel and 9+ years of healthcare experience",
                                                     profile=skilled(sql=0.5)).flags)
        self.assertRegex(found, r"9\+ years of \w+ required \(you have 4\); it names a domain or a tool")
        self.assertNotIn("may offer another figure", found)
        self.assertNotEqual(required("3+ years of SQL or Excel, or 9+ years of healthcare experience", skilled(sql=0.5))[0][0],
                            "INELIGIBLE")   # control: an "or" right before the figure makes it an alternative

    def test_an_unconfirmed_alternative_is_named_in_the_flag(self):
        """Codex F2/F4 and D113 (hand check #521, #522, #529): an alternative nothing confirms (a domain beside a function,
        a role only partly matched) and another reading's unconfirmed alternative are named in the flag; under D123 the
        line is CONDITIONAL either way, so the flag's words are what tells them apart."""
        for line, words in (("5+ years in business systems analysis or financial services",
                             "nothing in your record confirms financial services"),
                            ("5+ years as a Business Systems Analyst or a Customer Service Lead",
                             "nothing in your record confirms customer service lead"),
                            ("5+ years as a Business Systems Analyst or similar in healthcare", "read another way")):
            with self.subTest(line=line):
                self.assertEqual(required(line), (("CONDITIONAL", {"years_role_unconfirmed"}),) * 2)
                self.assertIn(words, detail(judged(line), "years_role_unconfirmed"))
        # control: an alternative the record confirms in full is no flag of its own
        self.assertEqual(required("3+ years as a Customer Service Representative or a Customer Service Lead"),
                         (("ELIGIBLE", set()),) * 2)


# --- A skill word that is ordinary English (2026-11-03, D98) ---------------------------------------------------------
class SkillWordTests(unittest.TestCase):
    def skills(self, line):
        return {r.name for r in extract(f"Requirements:\n- {line}\n", APPROVED).requirements if r.category == "skill"}

    def test_facets_of_the_role_is_not_the_facets_system(self):
        self.assertNotIn("facets", self.skills("This list spans skills relevant to several facets of the role; bring what you have"))
        self.assertNotIn("facets", self.skills("Comfortable with many facets of claims operations"))

    def test_the_facets_system_is_still_read(self):
        self.assertTrue({"facets", "qnxt"} <= self.skills("Experience with Facets or QNXT required"))
        self.assertIn("facets", self.skills("TriZetto Facets configuration experience across all facets of benefits setup"))
        for line in ("Experience with multiple Facets implementations", "Knowledge of all Facets modules",
                     "Facets of TriZetto experience required", "FACETS configuration across all FACETS modules"):
            self.assertIn("facets", self.skills(line), line)   # a capital names the system (pre-review #10, 2026-11-04)

    def test_a_new_exclusion_re_reads_postings(self):
        from careeros.pipeline import Context, _extraction_fingerprint
        from tests.test_v21_policy import policy_with
        posting_row = {"content_fingerprint": "c" * 64}
        before = _extraction_fingerprint(Context(None, policy_with(), PERSONA, None, None), posting_row)
        wider = policy_with(skill_alias_exclusions={"facets": ["facets of", "rare facets"]})
        self.assertNotEqual(_extraction_fingerprint(Context(None, wider, PERSONA, None, None), posting_row), before)


# --- OPP-80: skills offered as alternatives (2026-11-03, D100-D101) ---------------------------------------------------
class SkillAlternativeTests(unittest.TestCase):
    BI = Profile(data={**PERSONA.data, "skills": PERSONA.data["skills"] + [{"name": "power bi", "evidence": "adjacent"}]},
                 fingerprint="bi")

    def reqs(self, *required):
        return extract(posting(required=list(required)), APPROVED).requirements

    def groups(self, *required):
        return skill_groups.groups(self.reqs(*required), APPROVED, REQUIRED_TYPES)

    def test_what_counts_as_alternatives(self):
        self.assertEqual(self.groups("Experience with BI tools such as Tableau or PowerBI"), [["tableau", "power bi"]])
        self.assertEqual(self.groups("Hands-on experience with SQL, Tableau, or PowerBI"), [["sql", "tableau", "power bi"]])
        self.assertEqual(self.groups("Experience using data tools such as Excel, SQL, Tableau, Power BI, or similar tools"),
                         [["excel", "sql", "tableau", "power bi"]])
        self.assertEqual(self.groups("Experience with one of SQL, Python"), [["sql", "python"]])
        self.assertEqual(self.groups("Python required; Jira or Confluence experience"), [["jira", "confluence"]])
        self.assertEqual(self.groups("Proficiency in any of the following: Excel, SQL"), [["excel", "sql"]])
        # pre-review #9 (2026-11-04): a semicolon ends a list
        self.assertEqual(self.groups("Technical skills: Excel; SQL; Tableau or Power BI"), [["tableau", "power bi"]])

    def test_what_does_not(self):
        for line in ("Proficiency in SQL and Excel", "Deep, hands-on expertise in SQL / Python",
                     "Experience with Confluence, Jira, Tableau, Salesforce",
                     # pre-review #8 (2026-11-04): "one", "any" or "either" that does not open the list
                     "At least one year of experience with Excel, SQL and Tableau",
                     "This role is open in any state. Strong Excel, SQL skills required",
                     "Either remote or onsite. Must have Excel, SQL and Python"):
            self.assertEqual(self.groups(line), [], line)

    def test_a_group_counts_once_as_the_best_you_have(self):
        reqs = self.reqs("Experience with BI tools such as Tableau or PowerBI")
        coverage = score_mod.skill_coverage(job(TITLE, ""), reqs, self.BI, APPROVED)
        items = {i["requirement"]: i for i in coverage["items"] if i["category"] == "skill"}
        self.assertEqual(set(items), {"power bi"})
        self.assertEqual(items["power bi"]["alternatives"], ["tableau"])
        self.assertIn("the best of tableau or power bi", items["power bi"]["basis"])
        self.assertEqual((coverage["lesser_alternatives"], coverage["alternatives"]), (["tableau"], {"power bi": ["tableau", "power bi"]}))

    def test_evidence_decides_then_importance(self):
        reqs = self.reqs("Hands-on experience with SQL, Tableau, or PowerBI")
        coverage = score_mod.skill_coverage(job(TITLE, ""), reqs, self.BI, APPROVED)
        self.assertEqual([i["requirement"] for i in coverage["items"] if i["category"] == "skill"], ["sql"])   # direct beats adjacent
        core = score_mod.skill_coverage(job(TITLE, ""), self.reqs("Experience with Facets or Excel"), PERSONA, APPROVED)
        self.assertEqual([i["requirement"] for i in core["items"] if i["category"] == "skill"], ["excel"])   # held beats core
        none = score_mod.skill_coverage(job(TITLE, ""), self.reqs("Experience with Facets or QNXT"), PERSONA, APPROVED)
        self.assertEqual([i["requirement"] for i in none["items"] if i["category"] == "skill"], ["facets"])   # the first, on a tie

    def test_the_card_names_a_missing_group_with_its_alternatives(self):
        text = posting(required=["Experience with Facets or QNXT", "Experience with BI tools such as Tableau or PowerBI"])
        j, reqs = job(TITLE, text), extract(text, APPROVED).requirements
        result = gate(TITLE, text, profile=self.BI)
        elig = score_mod.eligibility(j, reqs, self.BI, result, APPROVED)
        self.assertEqual((elig["missing_skills"], elig["matched_skills"]), (["facets or qnxt"], ["power bi"]))
        fit = score_mod.candidate_fit(j, reqs, self.BI, APPROVED)
        self.assertEqual(fit["dims"]["tools_overlap"]["value"], 0.5)   # one of two requirements, not one of four skills

    def test_a_member_asked_for_on_its_own_elsewhere_still_counts(self):
        reqs = self.reqs("Experience with BI tools such as Tableau or PowerBI", "Tableau dashboard experience")
        coverage = score_mod.skill_coverage(job(TITLE, ""), reqs, self.BI, APPROVED)
        self.assertEqual({i["requirement"] for i in coverage["items"] if i["category"] == "skill"}, {"power bi", "tableau"})


# --- OPP-20: where your labels and the system disagree -------------------------------------------------------------
class DisagreementTests(unittest.TestCase):
    def setUp(self):
        self.store, _clock = new_store()

    def posting(self, job_id, state, tier=None, decision="eligible", stretch=False, label=None, then=("near_miss", "scored"), **kw):
        add_job(self.store, job_id, current_state=state, **kw)
        now = self.store.clock.now()
        self.store.writer("qualify").insert("gate_results", {
            "result_id": f"g:{job_id}", "job_id": job_id, "at": now, "policy_fingerprint": "p", "profile_fingerprint": "q",
            "extraction_fingerprint": "e", "decision": decision,
            "hard_failures_json": json.dumps([{"code": "not_remote"}] if decision == "rejected" else [])})
        if tier:
            self.store.writer("qualify").insert("scores", {
                "score_id": f"s:{job_id}", "job_id": job_id, "at": now, "policy_fingerprint": "p", "profile_fingerprint": "q",
                "eligibility": 80, "candidate_fit": 70, "career_value": 70, "freshness": 80, "apply_priority": 86 if tier == "B" else 70,
                "tier": tier, "interview_band": "Moderate", "state": "ELIGIBLE",
                "components_json": json.dumps({"stretch": stretch, "skill_coverage": 0.8, "state": "ELIGIBLE"})})
        if label:
            for n, value in enumerate(label if isinstance(label, tuple) else (label,)):
                self.store.writer("review").insert("labels", {
                    "label_id": f"l:{job_id}:{n}", "job_id": job_id, "at": f"2026-10-1{n + 2}T12:00:00.000Z", "label": value,
                    "system_tier": then[0], "system_decision": then[1]})

    def dismiss(self, job_id, reason="pay", at="2026-10-20T12:00:00.000Z"):
        self.store.writer("review").insert("dismissals", {"dismissal_id": f"d:{job_id}:{reason}:{at}", "job_id": job_id,
                                                          "reason_code": reason, "at": at, "actor": "user"})

    def result(self):
        policy = APPROVED
        return disagreements(self.store, policy)

    def test_the_classes_and_their_order(self):
        self.posting("gh:x:near", "scored", "near_miss", label="apply")
        self.posting("gh:x:rej", "rejected", decision="rejected", label="apply")
        self.posting("gh:x:arch", "scored", "archive", label="skip")
        self.posting("gh:x:b", "scored", "B", label="stretch")
        self.posting("gh:x:flagged", "scored", "near_miss", stretch=True, label="stretch")
        found = self.result()
        self.assertEqual([d["job_id"] for d in found["disagreements"]], ["gh:x:rej", "gh:x:b", "gh:x:near"])
        self.assertEqual((found["labelled"], found["judged"]), (5, 5))
        self.assertEqual(found["by_class"], {"apply -> near_miss_unflagged": 1, "apply -> skip": 1, "stretch -> apply": 1})
        first = found["disagreements"][0]
        self.assertEqual((first["class_now"], first["class_then"], first["verdict"]), ("skip", "near_miss_unflagged", "Rejected: not_remote."))

    def test_a_closed_posting_is_judged_by_its_last_decision(self):
        self.posting("gh:x:closed-rej", "closed", decision="rejected", label="skip")
        self.posting("gh:x:closed-near", "closed", "near_miss", label="skip")
        self.assertEqual(class_now(self.store, self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:x:closed-rej'")), "skip")
        self.assertEqual([d["job_id"] for d in self.result()["disagreements"]], ["gh:x:closed-near"])

    def test_the_latest_label_counts(self):
        self.posting("gh:x:changed", "scored", "archive", label=("apply", "skip"))
        self.assertEqual(self.result()["disagreements"], [])

    def test_a_dismissal_in_force_disagrees_while_the_system_still_ranks_it(self):
        self.posting("gh:x:dis", "scored", "near_miss")
        self.dismiss("gh:x:dis")
        self.posting("gh:x:back", "scored", "near_miss")
        self.dismiss("gh:x:back")
        self.dismiss("gh:x:back", reason="reinstated", at="2026-10-21T12:00:00.000Z")
        self.posting("gh:x:kept", "scored", "near_miss", stretch=True, label="stretch")   # agrees; reinstated after a dismissal
        self.dismiss("gh:x:kept")
        self.dismiss("gh:x:kept", reason="reinstated", at="2026-10-21T12:00:00.000Z")
        found = self.result()
        self.assertEqual([(d["job_id"], d["label"], d["dismissed"]) for d in found["disagreements"]], [("gh:x:dis", None, "pay")])
        self.assertEqual(found["dismissed"], 1)

    def test_test_rows_are_left_out(self):
        self.posting("gh:x:drill", "scored", "near_miss", label="apply", exclude_from_kpi=1)
        self.assertEqual(self.result()["labelled"], 0)

    def test_the_command_prints_each_posting_with_its_rule(self):
        self.posting("gh:x:rej", "rejected", decision="rejected", label="apply")
        self.posting("gh:x:dis", "scored", "B", label="stretch")
        self.dismiss("gh:x:dis", reason="qualifications")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_labels(SimpleNamespace(store=self.store, policy=APPROVED), None, SimpleNamespace(action="disagreements"))
        text = out.getvalue()
        self.assertIn("2 of 2 judged postings (2 labelled, 1 dismissed)", text)
        self.assertIn("- gh:x:rej", text)
        self.assertIn("you: apply; CareerOS now: rejected or archive; when you labelled it (2026-10-12): near-miss, no stretch flag", text)
        self.assertIn("you: stretch, then dismissed (qualifications); CareerOS now: Tier A/B", text)
        self.assertIn("Rejected: not_remote.", text)
        self.assertEqual(render_disagreements(self.result())[0], text.splitlines()[0])
