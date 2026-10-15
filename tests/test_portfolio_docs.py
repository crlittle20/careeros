"""Recruiter README, case study and AI-assisted development disclosure (Portfolio Prompt 11, 2026-10-13).

People read these documents to decide whether to trust the project, so they are held to the code:
- each has the sections Prompt 11 asks for, in order;
- every figure they quote for the tests, the planted defects, the Functional MVP gate and the demo world is the current
  one, and the demo figures sit only under a "synthetic" label;
- they hold nothing a public release scan flags (secrets, personal addresses, machine paths), and the README does not
  lean on the private operator manual.
Files, tests, commands and links they name are checked by tests/test_docs.py.
"""
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

from careeros.demo import world
from careeros.paths import ENGINE_ROOT
from tests.test_demo_mode import RECORDED

sys.path.insert(0, str(ENGINE_ROOT / "tools"))
import mutate  # noqa: E402
import release_scan  # noqa: E402

README = ENGINE_ROOT / "README.md"
CASE_STUDY = ENGINE_ROOT / "docs" / "case_study.md"
AI_DEVELOPMENT = ENGINE_ROOT / "docs" / "ai_assisted_development.md"
README_SECTIONS = ["Project summary", "Business problem", "Solution", "Key capabilities", "Architecture", "Workflow",
                   "Employer verification", "Data quality", "Analytics", "Security", "Testing", "Demo Mode", "Tech stack",
                   "Quickstart", "Limitations", "Roadmap"]
CASE_SECTIONS = ["Situation", "Problem", "Requirements", "Analysis", "Design", "Implementation", "Controls", "Testing",
                 "Results", "Lessons", "Next iteration"]
REPORT = ENGINE_ROOT / "docs" / "portfolio_mvp_report.md"
CATEGORIES = ["Business Value", "Architecture", "Functionality", "Data Quality", "Automation", "Analytics", "Security",
              "Testing", "Documentation", "Demoability", "Recruiter Clarity"]
AI_TOPICS = ["Where AI assisted", "What stayed human-controlled", "How generated code was reviewed",
             "How tests validated behavior", "How security-sensitive changes were validated",
             "How architecture decisions were evaluated"]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(body: str) -> str:
    """Whitespace collapsed, so a figure wrapped across lines still matches."""
    return " ".join(body.split())


def headings(path: Path) -> list[str]:
    return [re.sub(r"^\d+\.\s*", "", line[3:]).strip() for line in text(path).splitlines() if line.startswith("## ")]


def section(body: str, heading: str) -> str:
    """From a heading line to the next heading of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    start = body.index(f"\n{heading}\n")
    end = re.search(rf"\n#{{1,{len(level)}}} ", body[start + 1:])
    return body[start: start + 1 + end.start()] if end else body[start:]


class StructureTest(unittest.TestCase):
    def test_the_readme_has_the_sections_in_order(self):
        self.assertEqual(headings(README), README_SECTIONS)

    def test_the_case_study_has_the_sections_in_order(self):
        self.assertEqual(headings(CASE_STUDY), CASE_SECTIONS)

    def test_the_ai_disclosure_answers_each_question_in_order(self):
        found = headings(AI_DEVELOPMENT)
        self.assertEqual([h for h in found if h in AI_TOPICS], AI_TOPICS)

    def test_the_readme_presents_decision_support_not_an_application_bot(self):
        opening = flat(text(README)[:900])
        self.assertTrue("evidence-driven job-search operations and decision-support platform" in opening, "positioning")
        self.assertTrue("It does not apply for jobs" in opening, "no application bot")


class FiguresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tests = unittest.defaultTestLoader.discover(str(ENGINE_ROOT / "tests"), top_level_dir=str(ENGINE_ROOT)).countTestCases()
        cls.gate = json.loads(text(ENGINE_ROOT / "evidence" / "functional_mvp.json"))
        cls.docs = {path.name: flat(text(path)) for path in (README, CASE_STUDY, AI_DEVELOPMENT)}

    def test_the_test_count_is_current(self):
        quoted = {name: re.findall(r"\b(\d+) (?:automated )?tests\b", body) for name, body in self.docs.items()}
        self.assertTrue(f"| {self.tests} automated tests |" in self.docs["README.md"], f"| {self.tests} automated tests |")
        self.assertTrue(f"| Automated tests | {self.tests}, all passing" in self.docs["case_study.md"], f"| Automated tests | {self.tests}, all passing")
        self.assertTrue(f"The suite has {self.tests} tests" in self.docs["ai_assisted_development.md"], f"The suite has {self.tests} tests")
        self.assertEqual({name: set(found) - {str(self.tests)} for name, found in quoted.items()}, {n: set() for n in quoted})

    def test_the_planted_defect_count_is_current(self):
        count = str(len(mutate.MUTATIONS))
        self.assertTrue(f"| {count} planted defects |" in self.docs["README.md"], f"| {count} planted defects |")
        self.assertTrue(f"| Planted defects | {count} defined" in self.docs["case_study.md"], f"| Planted defects | {count} defined")
        self.assertTrue(f"There are {count} such defects" in self.docs["ai_assisted_development.md"], f"There are {count} such defects")
        for name, body in self.docs.items():
            self.assertEqual(set(re.findall(r"\b(\d+) (?:planted|such) defects\b", body)) - {count}, set(), name)

    def test_the_functional_mvp_figures_match_its_evidence(self):
        checks = sum(len(found) for found in self.gate["checks"].values())
        grades = Counter(self.gate["grades"].values())
        for name, body in self.docs.items():
            self.assertEqual(set(re.findall(r"\b(\d+) checks\b", body)) - {str(checks)}, set(), name)
        readme = f"Of {len(self.gate['grades'])}, {grades['PASS']} are PASS and {grades['PARTIAL']} are PARTIAL"
        case_study = f"{checks} checks: no requirement fails; {grades['PASS']} PASS, {grades['PARTIAL']} PARTIAL"
        self.assertTrue(readme in self.docs["README.md"], readme)
        self.assertTrue(case_study in self.docs["case_study.md"], case_study)
        self.assertEqual(grades["FAIL"], 0)    # "no requirement fails" must stay true

    def test_demo_figures_are_current_and_labelled_synthetic(self):
        jobs, boards = RECORDED["jobs"], RECORDED["boards"]
        tiers = RECORDED["tiers"]
        readme_demo = flat(section(text(README), "## Demo Mode"))
        self.assertTrue("**Synthetic results**" in readme_demo, "the README labels its demo figures synthetic")
        for phrase in (f"{len(world.EMPLOYERS)} fictional employers",
                       f"{sum(jobs.values())} jobs, of which {jobs['scored']} scored, {jobs['rejected']} rejected",
                       f"{jobs['duplicate']} duplicate and {jobs['closed']} closed",
                       f"{sum(boards.values())} boards, of which {boards['active']} active, {boards['rejected']} rejected as an impostor and {boards['review_required']} awaiting review"):
            self.assertTrue(phrase in readme_demo, phrase)
        synthetic = flat(section(text(CASE_STUDY), "### SYNTHETIC: Demo Mode results"))
        for phrase in (f"{len(world.EMPLOYERS)} fictional employers, {sum(boards.values())} boards",
                       f"{boards['active']} active, {boards['historical']} historical", f"{boards['manual_only']} manual-only",
                       f"{boards['rejected']} rejected as an impostor, {boards['review_required']} awaiting review",
                       f"{sum(jobs.values())} jobs: {jobs['scored']} scored, {jobs['rejected']} rejected with stated reasons, "
                       f"{jobs['duplicate']} duplicate, {jobs['closed']} closed",
                       f"{tiers['B']} Tier B, {tiers['near_miss']} near-miss, {tiers['archive']} archive",
                       f"{RECORDED['night_routed']} routed to NIGHT", f"{RECORDED['leads']} alert-email leads"):
            self.assertTrue(phrase in synthetic, phrase)
        outside = flat(text(CASE_STUDY).replace(section(text(CASE_STUDY), "### SYNTHETIC: Demo Mode results"), ""))
        self.assertFalse(f"{sum(jobs.values())} jobs" in outside, "demo totals outside the synthetic section")   # demo totals appear only under the synthetic label


    def test_the_portfolio_gate_report_quotes_current_figures(self):
        body = flat(text(REPORT))
        grades = Counter(self.gate["grades"].values())
        for phrase in (f"{self.tests} automated tests pass", f"There are {len(mutate.MUTATIONS)} planted defects",
                       f"{grades['PASS']} PASS, {grades['PARTIAL']} PARTIAL, {grades['FAIL']} FAIL"):
            self.assertTrue(phrase in body, phrase)


class PortfolioGateReportTest(unittest.TestCase):
    """docs/portfolio_mvp_report.md (Portfolio Prompt 12): every category graded, every grade under 90 explained, and a
    verdict that follows the open blockers."""

    def test_every_category_is_graded_and_every_grade_below_90_is_explained(self):
        rows = {m.group(1): (int(m.group(2)), m.group(3).strip())
                for m in re.finditer(r"^\| ([A-Z][A-Za-z ]+?) \| (\d+) \| (.*?)\|$", text(REPORT), re.M)}
        self.assertEqual([c for c in CATEGORIES if c not in rows], [])
        for category in CATEGORIES:
            grade, why = rows[category]
            self.assertTrue(0 <= grade <= 100, category)
            if grade < 90:
                self.assertGreater(len(why), 40, f"{category} is graded {grade} without saying why")

    def test_the_verdict_follows_the_open_blockers(self):
        body = text(REPORT)
        open_blockers = [line for line in section(body, "### BLOCKER").splitlines() if line.startswith("| B") and "| Open" in line]
        self.assertEqual("**Verdict: NOT PASSED.**" in body, bool(open_blockers))
        self.assertEqual("**Verdict: PASSED.**" in body, not open_blockers)
        for label in ("### BLOCKER", "### SHOULD FIX", "### POST-MVP"):
            self.assertIn(f"\n{label}\n", body)


class PublishableTest(unittest.TestCase):
    def test_nothing_a_release_scan_flags(self):
        for path in (README, CASE_STUDY, AI_DEVELOPMENT):
            self.assertEqual(release_scan.scan_text(text(path), []), [], path.name)

    def test_the_readme_stands_without_the_private_operator_manual(self):
        self.assertFalse("operator_manual" in text(README), "the README links the private operator manual")


if __name__ == "__main__":
    unittest.main()
