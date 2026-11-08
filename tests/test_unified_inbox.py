"""Staged neutral intake tests. No live Gmail, GitHub, ATS, or secrets are used."""
import unittest
from unittest import mock

from careeros.cli import _inbox_pull
from careeros.discovery import github_inbox as inbox
from tests.test_github_inbox import FakeGh, PullTests, REPO, issue


class NeutralIntakeTests(unittest.TestCase):
    def test_legacy_titles_remain_unchanged(self):
        cases = {
            "PRIMARY Job recommendations 2026-11-08": "primary",
            "NIGHT Job recommendations 2026-11-08": "night",
            "TEST NIGHT Job recommendations 2026-11-08": "night",
            "Job recommendations 2026-11-08": "primary",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(inbox.issue_profile({"title": title}), expected)

    def test_explicit_neutral_prefix_only(self):
        self.assertEqual(inbox.issue_profile({"title": "INTAKE Job recommendations 2026-11-08"}), "unified")
        self.assertEqual(inbox.issue_profile({"title": "TEST INTAKE Job recommendations 2026-11-08"}), "unified")
        self.assertEqual(inbox.issue_profile({"title": "NIGHT and INTAKE 2026-11-08"}), "night")
        self.assertEqual(inbox.issue_profile({"title": "Some INTAKE ideas"}), "primary")

    def test_unified_issue_waits_when_night_disabled(self):
        fixture = PullTests(methodName="test_issue_is_graded_answered_closed_and_never_reread")
        fixture.setUp()
        try:
            row = issue(number=777, body=(
                "Pharmacy Benefits Systems Analyst | Synthetic Health | Remote (US) | "
                "https://job-boards.greenhouse.io/synth/jobs/555 | source=test"
            ))
            row["title"] = "INTAKE Job recommendations 2026-11-08"
            gh = FakeGh([row])
            with mock.patch.object(inbox, "gh_path", lambda: "gh"), \
                 mock.patch("careeros.qualify.night.enabled", return_value=False):
                counts = _inbox_pull(fixture.ctx, fixture.client, REPO, runner=gh)
            self.assertEqual(counts["inbox_night_waiting"], 1)
            self.assertEqual(counts["inbox_issues"], 0)
            self.assertFalse(fixture.fetched)
            self.assertFalse(row.get("closed"))
        finally:
            fixture.tearDown()

    def test_unified_retains_night_only_leads(self):
        fixture = PullTests(methodName="test_issue_is_graded_answered_closed_and_never_reread")
        fixture.setUp()
        try:
            body = (
                "Pharmacy Benefits Systems Analyst | Synthetic Health | Remote (US) | "
                "https://job-boards.greenhouse.io/synth/jobs/555 | source=test\n"
                "Night Auditor | Sample Audit | Remote (US) | "
                "https://www.linkedin.com/jobs/view/4012345678 | source=test\n"
            )
            row = issue(number=778, body=body)
            row["title"] = "INTAKE Job recommendations 2026-11-08"
            gh = FakeGh([row])
            with mock.patch.object(inbox, "gh_path", lambda: "gh"), \
                 mock.patch("careeros.qualify.night.enabled", return_value=True):
                counts = _inbox_pull(fixture.ctx, fixture.client, REPO, runner=gh)
            self.assertEqual(counts["inbox_issues"], 1)
            self.assertEqual(
                fixture.store.one("SELECT profile FROM leads WHERE site = 'greenhouse'")["profile"],
                "primary",
            )
            self.assertEqual(
                fixture.store.one("SELECT profile FROM leads WHERE site = 'linkedin'")["profile"],
                "night",
            )
            self.assertEqual(len(fixture.fetched), 1)
            self.assertTrue(row.get("closed"))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
