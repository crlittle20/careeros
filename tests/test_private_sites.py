"""Private site knowledge (refactor R4, 2026-10-14): what ties CareerOS to the job seeker's own situation lives in the
vault, and the engine applies it exactly as it applied the lists it replaced. The suite reads the synthetic fixture
tests/fixtures/private_sites.yaml (tests/__init__.py), never a real vault.
"""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from careeros.core import private_sites
from careeros.demo import world
from careeros.discovery import sources
from careeros.discovery.alerts import site_of
from careeros.discovery.board_discovery import is_staffing_agency
from careeros.discovery.board_urls import manual_system
from careeros.discovery.safe_fetch import SiteFetcher, SiteFetchError
from careeros.discovery.site_inspect import system_of
from careeros.normalize.posting import US_STATES
from careeros.qualify.facts import pay_fact
from careeros.review.report import _apply_form

from tests.test_saved import POLICY
from tests.test_v21_policy import policy_with

NONE = {private_sites.ENV: ""}
ROOT = Path(__file__).resolve().parents[1]


class LoaderTest(unittest.TestCase):
    def test_the_suite_reads_the_synthetic_fixture(self):
        self.assertTrue(os.environ[private_sites.ENV].endswith(os.path.join("fixtures", "private_sites.yaml")))
        self.assertIn("harborstaffing.com", private_sites.never_fetch())
        self.assertEqual(private_sites.agency_shortcut(), "Harbor Staffing")

    def test_no_file_means_no_additions(self):
        with mock.patch.dict(os.environ, NONE):
            self.assertEqual(private_sites.load(), {})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {private_sites.ENV: str(Path(tmp) / "absent.yaml")}):
            self.assertEqual(private_sites.load(), {})

    def test_demo_mode_never_reads_the_file(self):
        with mock.patch("careeros.demo.sandbox.enabled", return_value=True), \
                mock.patch.object(private_sites, "_load", side_effect=AssertionError("read in Demo Mode")):
            self.assertEqual(private_sites.load(), {})
            self.assertEqual(private_sites.never_fetch(), ())

    def test_a_malformed_file_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private_sites.yaml"
            path.write_text("sources: {somesite: sometimes}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), self.assertRaises(ValueError):
                private_sites.load()

    def test_the_vault_file_lives_in_the_policy_folder(self):
        from careeros.paths import vault_paths
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(vault_paths(Path(tmp))["private_sites"], Path(tmp) / "policy" / "private_sites.yaml")


class ConsumersTest(unittest.TestCase):
    """Each list the engine used to carry is applied from the private file, and nothing is applied without it."""

    def test_private_domains_are_never_fetched(self):
        with self.assertRaises(SiteFetchError) as refused:
            SiteFetcher("careers.northgategroup.com")
        self.assertEqual(refused.exception.failure, "blocked_site")
        with mock.patch.dict(os.environ, NONE):
            SiteFetcher("careers.northgategroup.com").close()   # no private file: an ordinary website

    def test_private_systems_label_leads_and_stay_manual(self):
        self.assertEqual(site_of("careers.northgategroup.com"), "nhg")
        self.assertEqual(manual_system("https://www.harborstaffing.com/job/1"), "harborstaffing")
        self.assertEqual(system_of("https://careers.northgategroup.com/job/2"), "nhg")
        self.assertEqual(sources.status("nhg"), "manual")
        self.assertEqual(sources.status("examplejobs"), "blocked")
        with mock.patch.dict(os.environ, NONE):
            self.assertEqual(site_of("careers.northgategroup.com"), "other")
            self.assertIsNone(manual_system("https://www.harborstaffing.com/job/1"))
            self.assertEqual(sources.status("examplejobs"), "manual")

    def test_a_private_staffing_agency_is_never_probed(self):
        self.assertTrue(is_staffing_agency("Harbor Staffing", POLICY))
        self.assertTrue(is_staffing_agency("Bayview Partners", POLICY))     # only the private list names it
        self.assertTrue(is_staffing_agency("TEKsystems", POLICY))           # the engine's generic list still applies
        with mock.patch.dict(os.environ, NONE):
            self.assertFalse(is_staffing_agency("Harbor", POLICY))
            self.assertFalse(is_staffing_agency("Bayview Partners", POLICY))

    def test_the_review_page_offers_the_private_agency_shortcut_only_when_set(self):
        self.assertIn('value="agency:Harbor Staffing"', _apply_form("gh:x:1", "token", {}))
        with mock.patch.dict(os.environ, NONE):
            page = _apply_form("gh:x:1", "token", {})
        self.assertNotIn('value="agency:', page)
        self.assertIn('value="agency"', page)


class HomeStateTest(unittest.TestCase):
    """The home state is a vault setting (location.home_state, 2026-10-14). Messages read it; shared code, settings, tools
    and documents never name a state in full (apart from the state table and the fictional demo job seeker's state) or
    a U.S. time zone (only the posting reader and the fictional demo postings use those)."""

    def test_messages_name_the_configured_state(self):
        job = {"comp_min": None, "comp_max": None, "description_text": ""}
        self.assertIn("for Florida / remote", pay_fact(job, policy_with(location__home_state="FL"))["detail"])
        self.assertIn("for your state / remote", pay_fact(job, policy_with(location__home_state=None))["detail"])

    def test_shared_files_name_no_state_or_time_zone(self):
        demo = yaml.safe_load(world.OVERRIDES)["location"]["home_state"]
        allowed = {name for name, code in US_STATES.items() if code == demo}
        names = re.compile(r"\b(" + "|".join(map(re.escape, sorted(US_STATES, key=len, reverse=True))) + r")\b", re.I)
        zones = re.compile(r"\b(?:US|U\.S\.) (?:Eastern|Central|Mountain|Pacific)\b|\b(?:Eastern|Central|Mountain|Pacific) [Tt]ime\b")
        found = {}
        paths = [p for d in ("careeros", "config", "tools", "docs") for p in (ROOT / d).rglob("*")] + list(ROOT.glob("*.md"))
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            if (path.suffix not in (".py", ".md", ".yaml", ".yml", ".json", ".sql") or "__pycache__" in path.parts
                    or rel in ("careeros/normalize/posting.py", "docs/operator_manual.md")):
                continue
            text = path.read_text(encoding="utf-8")
            named = {m.group(1).lower() for m in names.finditer(text)} - allowed
            if not rel.startswith("careeros/demo/"):
                named |= {m.group(0) for m in zones.finditer(text)}
            if named:
                found[rel] = sorted(named)
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
