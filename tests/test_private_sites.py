"""Private site knowledge (refactor R4, 2026-10-14): what ties CareerOS to the job seeker's own situation lives in the
vault, and the engine applies it exactly as it applied the lists it replaced. The suite reads the synthetic fixture
tests/fixtures/private_sites.yaml (tests/__init__.py), never a real vault.
"""
import os
import re
import sys
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

    def test_agency_digests_are_read_and_checked(self):
        self.assertEqual(private_sites.agency_digests(), (private_sites.AgencyDigest(
            "Harbor Staffing", "harborstaffing", "email.harborstaffing.com", "click.email.harborstaffing.com", "c5d892ac0ea84766"),))
        bad = ("agency_digests: {name: A}\n",
               "agency_digests:\n  - {name: A, site: a, sender_domain: mail.a.example}\n",
               "agency_digests:\n  - {site: a, sender_domain: mail.a.example, tracker_host: t.a.example}\n",   # no name
               "agency_digests:\n  - {name: A, site: a, sender_domain: mail.a.example, tracker_host: https://t.a.example/x}\n",
               "agency_digests:\n  - {name: A, site: a, sender_domain: mail.a.example, tracker_host: t.a.example, layout: abc}\n")
        for text in bad:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "private_sites.yaml"
                path.write_text(text, encoding="utf-8")
                with mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                        self.assertRaisesRegex(ValueError, "private_sites_invalid"):
                    private_sites.agency_digests()
        with mock.patch.dict(os.environ, NONE):
            self.assertEqual(private_sites.agency_digests(), ())

    def test_the_vault_file_lives_in_the_policy_folder(self):
        from careeros.paths import vault_paths
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(vault_paths(Path(tmp))["private_sites"], Path(tmp) / "policy" / "private_sites.yaml")


class RunReadingTest(unittest.TestCase):
    """One reading of the private settings per run (Codex Pass 3, D141): edits take effect at the next run, and a run
    whose file changed commits and sends nothing more."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private_sites.yaml"
        self.path.write_text("never_fetch: [first.example]\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {private_sites.ENV: str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_run_keeps_its_first_reading_and_refuses_once_the_file_changed(self):
        with private_sites.run():
            self.assertEqual(private_sites.never_fetch(), ("first.example",))
            before = private_sites.revision()
            private_sites.check()   # unchanged: nothing to refuse
            self.path.write_text("never_fetch: [second.example]\n", encoding="utf-8")
            self.assertEqual(private_sites.never_fetch(), ("first.example",))   # never read part-way through the run
            self.assertEqual(private_sites.revision(), before)
            with private_sites.run():   # a nested run keeps the outer one
                self.assertEqual(private_sites.never_fetch(), ("first.example",))
            with self.assertRaises(private_sites.SettingsChanged):
                private_sites.check()
            with self.assertRaises(private_sites.SettingsChanged):
                with private_sites.pinned():   # a unit inside the run uses its reading and is refused at its end
                    self.assertEqual(private_sites.never_fetch(), ("first.example",))
        self.assertEqual(private_sites.never_fetch(), ("second.example",))   # the next run reads it

    def test_a_file_unreadable_when_the_run_first_reads_it_stays_so_for_the_run(self):
        self.path.write_text("never_fetch: [\n", encoding="utf-8")
        with private_sites.run():
            with self.assertRaises(ValueError):
                private_sites.load()
            self.path.write_text("never_fetch: [fixed.example]\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                private_sites.never_fetch()
        self.assertEqual(private_sites.never_fetch(), ("fixed.example",))

    def test_a_file_removed_or_broken_during_a_run_is_a_change(self):
        for change in ("removed", "broken"):
            with self.subTest(change), private_sites.run():
                self.path.write_text("never_fetch: [first.example]\n", encoding="utf-8")
                private_sites.load()
                if change == "removed":
                    self.path.unlink()
                else:
                    self.path.write_text("never_fetch: [\n", encoding="utf-8")
                with self.assertRaises(private_sites.SettingsChanged) as raised:
                    private_sites.check()
                self.assertTrue(str(raised.exception).startswith("private_sites_changed"))

    def test_demo_mode_never_reads_the_file_in_a_run_either(self):
        from careeros.demo import sandbox
        with mock.patch.dict(os.environ, {sandbox.ENV: "true"}), \
                mock.patch.object(private_sites, "_path", side_effect=AssertionError("the vault was read")):
            with private_sites.run():
                private_sites.refresh()
                private_sites.check()
                with private_sites.pinned() as revision:
                    self.assertEqual((revision, private_sites.load()), (private_sites.ABSENT, {}))

    def test_a_change_once_seen_stops_the_run_even_after_the_file_is_restored(self):
        start = self.path.read_bytes()
        with private_sites.run():
            private_sites.load()
            self.assertFalse(private_sites.stopped())
            self.path.write_text("never_fetch: [second.example]\n", encoding="utf-8")
            with self.assertRaises(private_sites.SettingsChanged):
                private_sites.check()
            self.path.write_bytes(start)   # the starting bytes again: the run stays stopped (Codex Pass 4)
            self.assertTrue(private_sites.stopped())
            with self.assertRaises(private_sites.SettingsChanged):
                private_sites.check()
            with self.assertRaises(private_sites.SettingsChanged):
                with private_sites.pinned():
                    pass
        with private_sites.run():   # the next run starts afresh
            private_sites.check()
            self.assertFalse(private_sites.stopped())

    def test_outside_a_run_every_read_sees_the_file_as_it_is(self):
        self.assertEqual(private_sites.never_fetch(), ("first.example",))
        private_sites.check()   # no reading to compare: nothing to do
        self.path.write_text("never_fetch: [second.example]\n", encoding="utf-8")
        private_sites.refresh()
        self.assertEqual(private_sites.never_fetch(), ("second.example",))


@unittest.skipUnless(sys.platform == "win32", "the file is held for a run on Windows only (D147)")
class RunLockTest(unittest.TestCase):
    """D147: on Windows a run holds the settings file so that no program can write, replace or delete it while the run
    lasts; a file another program is writing when the run starts is not read at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private_sites.yaml"
        self.path.write_text("never_fetch: [first.example]\n", encoding="utf-8")
        for patch in (mock.patch.dict(os.environ, {private_sites.ENV: str(self.path)}),
                      mock.patch.object(private_sites, "LOCK_RUNS", True), mock.patch.object(private_sites, "_BUSY_WAIT", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_no_program_can_write_replace_or_delete_the_file_during_a_run(self):
        edited = self.path.with_name("edited.yaml")
        edited.write_text("never_fetch: [second.example]\n", encoding="utf-8")
        with private_sites.run():
            self.assertEqual(private_sites.never_fetch(), ("first.example",))
            with self.assertRaises(PermissionError):
                self.path.write_text("never_fetch: [second.example]\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                os.replace(edited, self.path)   # an editor's save by replacing the file
            with self.assertRaises(PermissionError):
                self.path.unlink()
            self.assertEqual(self.path.read_text(encoding="utf-8"), "never_fetch: [first.example]\n")   # still readable
            private_sites.check()
        os.replace(edited, self.path)   # free again once the run ends: the next run reads the edit
        self.assertEqual(private_sites.never_fetch(), ("second.example",))

    def test_a_file_another_program_is_writing_is_not_read_by_the_run(self):
        with open(self.path, "a", encoding="utf-8"):   # an editor saving it this very moment
            with private_sites.run():
                with self.assertRaises(ValueError) as raised:
                    private_sites.never_fetch()
        self.assertTrue(str(raised.exception).startswith("private_sites_invalid"))
        with private_sites.run():
            self.assertEqual(private_sites.never_fetch(), ("first.example",))

    def test_a_missing_file_is_made_empty_and_held_so_none_can_be_created_during_a_run(self):
        """Codex Pass 5 (D153 R1): a run that started without the file held nothing, so a file created part-way through
        it went unseen by the run's own hold. The run now makes the file, empty, under the same hold."""
        edited = self.path.with_name("edited.yaml")
        edited.write_text("never_fetch: [second.example]\n", encoding="utf-8")
        self.path.unlink()
        with private_sites.run():
            self.assertEqual(private_sites.never_fetch(), ())   # empty: no private sites, as no file means
            self.assertEqual(self.path.read_bytes(), b"")
            with self.assertRaises(PermissionError):
                self.path.write_text("never_fetch: [second.example]\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                os.replace(edited, self.path)   # an editor's save, or a file made by another program
            with self.assertRaises(PermissionError):
                self.path.unlink()
            private_sites.check()
        os.replace(edited, self.path)   # free once the run ends: the next run reads it
        self.assertEqual(private_sites.never_fetch(), ("second.example",))

    def test_a_missing_folder_is_made_too(self):
        nested = Path(self.tmp.name) / "policy" / "private_sites.yaml"
        with mock.patch.dict(os.environ, {private_sites.ENV: str(nested)}), private_sites.run():
            self.assertEqual(private_sites.never_fetch(), ())
            self.assertEqual(nested.read_bytes(), b"")
            with self.assertRaises(PermissionError):
                nested.write_text("never_fetch: [second.example]\n", encoding="utf-8")


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
    a U.S. time zone (only the posting readers and the fictional demo postings use those)."""

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
                    or rel in ("careeros/normalize/posting.py", "careeros/qualify/worktime.py",   # posting readers
                               "docs/operator_manual.md")):
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
