"""Configuration checks (Portfolio Prompt 10, 2026-10-13): the committed configuration loads, means what it says and
keeps the standing limits, and the dependency pins agree. tools/validate.py runs these as its `config` check.
"""
import re
import tomllib
import unittest
from collections import Counter

from careeros.core.policy import load_policy, load_yaml
from careeros.core.transitions import load_matrix
from careeros.discovery.sources import STATUSES
from careeros.paths import CONFIG_DIR, ENGINE_ROOT

ATS_AUTOMATIC = {"greenhouse", "lever", "ashby", "workable"}     # the four ATSs CareerOS may read automatically
AUTOMATIC = ATS_AUTOMATIC | {"employer_site"}                     # plus employers' own sites, read only to verify a board
NEVER_AUTOMATED = {"linkedin", "indeed", "glassdoor"}
SITE_VALUES = {"allowed", "supervised_only", "manual", "unknown", "blocked"}
ENV_VARIABLES = {"CAREEROS_VAULT", "CAREEROS_DEMO_MODE", "CAREEROS_DEMO_VAULT", "ANTHROPIC_API_KEY"}
DIFFERENT_ON_PURPOSE = {"pydantic", "pydantic-core"}   # requirements.lock's were 4 days old; the dev lock keeps the 14-day rule


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(relative: str) -> dict[str, str]:
    found = {}
    for line in (ENGINE_ROOT / relative).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", line)
        if match:
            found[normalize(match.group(1))] = match.group(2)
    return found


class PolicyAndWorkflowTest(unittest.TestCase):
    def test_the_default_policy_loads_with_a_stable_fingerprint(self):
        first, second = (load_policy(CONFIG_DIR / "policy" / "defaults.yaml") for _ in range(2))
        self.assertTrue(first.data.get("policy_version"))
        self.assertEqual(first.fingerprint, second.fingerprint)
        for section in ("gate", "weights", "tiers", "operations", "board_verification", "scanning"):
            self.assertIsInstance(first.data.get(section), dict, section)

    def test_both_workflows_load_and_are_valid(self):
        for name in ("job", "application"):
            matrix = load_matrix(CONFIG_DIR / "workflows" / f"{name}.json")   # raises on any inconsistency
            self.assertTrue(matrix.edges, name)

    def test_seed_employers_use_only_the_automatic_boards_once_each(self):
        employers = load_yaml(CONFIG_DIR / "employers.yaml")["employers"]
        self.assertTrue(employers)
        for employer in employers:
            self.assertTrue(employer.get("name") and employer.get("token"), employer)
            self.assertIn(employer.get("ats"), ATS_AUTOMATIC, employer["name"])
        repeated = [pair for pair, n in Counter((e["ats"], e["token"]) for e in employers).items() if n > 1]
        self.assertEqual(repeated, [])


class SitePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_yaml(CONFIG_DIR / "site_policy.yaml")

    def test_every_source_status_is_a_known_word(self):
        # careeros.discovery.sources reads an unknown word as "manual", so a misspelt "blocked" would quietly weaken
        self.assertEqual({k: v for k, v in self.policy["sources"].items() if v not in STATUSES}, {})

    def test_only_the_four_ats_and_employer_sites_are_automatic(self):
        self.assertEqual({k for k, v in self.policy["sources"].items() if v == "allowed"}, AUTOMATIC)

    def test_linkedin_indeed_and_glassdoor_are_never_automated(self):
        for source in NEVER_AUTOMATED:
            self.assertIn(self.policy["sources"].get(source), ("manual", "blocked"), source)

    def test_no_site_allows_automated_filling_or_submitting(self):
        for site in self.policy["sites"]:
            for field in ("discovery", "automated_fill", "auto_submit"):
                self.assertIn(site.get(field, "unknown"), SITE_VALUES, (site["site"], field))
            self.assertNotEqual(site.get("automated_fill"), "allowed", site["site"])
            self.assertNotEqual(site.get("auto_submit"), "allowed", site["site"])
            self.assertTrue(site.get("reviewed"), site["site"])


class DependencyPinTest(unittest.TestCase):
    def test_direct_dependencies_are_pinned_alike_everywhere(self):
        declared = tomllib.loads((ENGINE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        runtime, dev = pins("requirements.lock"), pins("requirements-dev.lock")
        for requirement in declared:
            name, _, version = requirement.partition("==")
            self.assertTrue(version, f"{requirement} is not pinned exactly")
            self.assertEqual(runtime.get(normalize(name)), version, f"requirements.lock: {name}")
            self.assertEqual(dev.get(normalize(name)), version, f"requirements-dev.lock: {name}")

    def test_the_runtime_lock_pins_every_package(self):
        lines = [line for line in (ENGINE_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual([line for line in lines if "==" not in line], [])

    def test_the_dev_lock_hash_pins_every_package_and_holds_the_runtime_set(self):
        text = (ENGINE_ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
        entries = re.split(r"\n(?=[A-Za-z0-9])", "\n".join(line for line in text.splitlines() if not line.startswith("#")).strip())
        for entry in entries:
            self.assertRegex(entry, r"^[A-Za-z0-9._-]+==\S+", entry[:60])
            self.assertRegex(entry, r"--hash=sha256:[0-9a-f]{64}", entry[:60])
        dev, runtime = pins("requirements-dev.lock"), pins("requirements.lock")
        self.assertTrue({"ruff", "mypy", "pip-audit"} <= set(dev))
        for name, version in runtime.items():
            self.assertIn(name, dev)
            if name not in DIFFERENT_ON_PURPOSE:
                self.assertEqual(dev[name], version, name)


class EnvironmentExampleTest(unittest.TestCase):
    def test_env_example_lists_only_variables_the_code_reads_and_no_secret(self):
        values = dict(line.split("=", 1) for line in (ENGINE_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.startswith("#"))
        self.assertEqual(set(values), ENV_VARIABLES)
        self.assertEqual(values["ANTHROPIC_API_KEY"], "")
        code = "\n".join(p.read_text(encoding="utf-8") for p in (ENGINE_ROOT / "careeros").rglob("*.py"))
        for name in values:
            self.assertIn(f'"{name}"', code, name)


if __name__ == "__main__":
    unittest.main()
