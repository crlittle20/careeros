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

    @mock.patch("careeros.discovery.posting_evidence.board_mapping_trusted", return_value=True)
    def test_unified_retains_night_only_leads(self, _trusted_board):
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


    @mock.patch("careeros.discovery.posting_evidence.board_mapping_trusted", return_value=True)
    def test_unified_night_only_posting_reaches_independent_night_assessment(self, _trust):
        import httpx

        from careeros.discovery.http import HttpClient
        from careeros.pipeline import process
        from tests import postings
        from tests.test_pipeline import gh_job

        fixture = PullTests(methodName="test_issue_is_graded_answered_closed_and_never_reread")
        fixture.setUp()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=gh_job(555, "Night Auditor", postings.GOOD_FIT, req="N-555")
            )
        )
        client = HttpClient(transport=transport, min_interval=0, sleep=lambda secs: None)
        try:
            body = (
                "Night Auditor | Synthetic Health | Remote (US) | "
                "https://job-boards.greenhouse.io/synth/jobs/555 | source=test"
            )
            row = issue(number=781, body=body)
            row["title"] = "INTAKE Job recommendations 2026-11-08"
            gh = FakeGh([row])
            with mock.patch.object(inbox, "gh_path", lambda: "gh"), \
                 mock.patch("careeros.qualify.night.enabled", return_value=True):
                counts = _inbox_pull(fixture.ctx, client, REPO, runner=gh)
                self.assertEqual(counts["inbox_issues"], 1)
                process(fixture.ctx)
            job = fixture.store.one(
                "SELECT job_id, title FROM jobs WHERE external_id = '555'"
            )
            self.assertIsNotNone(job)
            self.assertEqual(job["title"], "Night Auditor")
            assessment = fixture.store.one(
                "SELECT decision, family FROM night_results WHERE job_id = ? ORDER BY at DESC LIMIT 1",
                (job["job_id"],),
            )
            self.assertIsNotNone(assessment)
            self.assertEqual(assessment["family"], "quality_audit")  # NIGHT family precedence
        finally:
            client.close()
            fixture.tearDown()

    def test_unified_unverified_ats_remains_review_lead(self):
        fixture = PullTests(methodName="test_issue_is_graded_answered_closed_and_never_reread")
        fixture.setUp()
        try:
            body = (
                "Pharmacy Benefits Systems Analyst | Synthetic Health | Remote (US) | "
                "https://job-boards.greenhouse.io/synth/jobs/555 | source=test"
            )
            row = issue(number=780, body=body)
            row["title"] = "INTAKE Job recommendations 2026-11-08"
            gh = FakeGh([row])
            with mock.patch.object(inbox, "gh_path", lambda: "gh"), \
                 mock.patch("careeros.qualify.night.enabled", return_value=True):
                counts = _inbox_pull(fixture.ctx, fixture.client, REPO, runner=gh)
            self.assertEqual(counts["unified_board_unverified"], 1)
            self.assertEqual(counts["inbox_issues"], 1)
            self.assertEqual(fixture.fetched, [])
            self.assertEqual(
                fixture.store.one("SELECT status FROM leads WHERE site = 'greenhouse'")["status"],
                "new",
            )
            self.assertIsNone(fixture.store.one("SELECT * FROM jobs"))
            comment = next(x[2] for x in gh.calls if x[:2] == ("issue", "comment"))
            self.assertIn("ownership not yet verified", comment)
            self.assertTrue(row.get("closed"))
        finally:
            fixture.tearDown()

    @mock.patch("careeros.discovery.posting_evidence.board_mapping_trusted", return_value=True)
    def test_unified_malformed_row_left_open_then_retried(self, _trusted_board):
        fixture = PullTests(methodName="test_issue_is_graded_answered_closed_and_never_reread")
        fixture.setUp()
        try:
            good = (
                "Pharmacy Benefits Systems Analyst | Synthetic Health | Remote (US) | "
                "https://job-boards.greenhouse.io/synth/jobs/555 | source=test"
            )
            bad = (
                "Claims Data Analyst | Example Company | Remote | "
                "https://[invalid]/jobs/999 | source=test"
            )
            row = issue(number=779, body=good + "\n" + bad)
            row["title"] = "INTAKE Job recommendations 2026-11-08"
            gh = FakeGh([row])
            with mock.patch.object(inbox, "gh_path", lambda: "gh"), \
                 mock.patch("careeros.qualify.night.enabled", return_value=True):
                first = _inbox_pull(fixture.ctx, fixture.client, REPO, runner=gh)
                self.assertEqual(first["inbox_unresolved_rows"], 1)
                self.assertEqual(first["inbox_issues"], 0)
                self.assertFalse(row.get("closed"))
                self.assertEqual(len(fixture.fetched), 1)
                self.assertEqual(
                    fixture.store.one("SELECT COUNT(*) AS c FROM leads")["c"], 1
                )
                # The owner fixes the bad line. Existing good rows must not
                # generate duplicate network fetches or duplicate lead rows.
                row["body"] = good
                second = _inbox_pull(fixture.ctx, fixture.client, REPO, runner=gh)
                self.assertEqual(second["inbox_issues"], 1)
                self.assertEqual(len(fixture.fetched), 1)
                self.assertTrue(row.get("closed"))
                self.assertEqual(
                    fixture.store.one("SELECT COUNT(*) AS c FROM leads")["c"], 1
                )
        finally:
            fixture.tearDown()

if __name__ == "__main__":
    unittest.main()
