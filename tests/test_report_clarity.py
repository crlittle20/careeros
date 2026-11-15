"""The report distinguishes unconfirmed NIGHT hours from verified shifts. Synthetic rows only."""
import unittest
from unittest import mock

from careeros.review.report import _night_section
from tests.test_qualify import POLICY


class FakeStore:
    def all(self, _query):
        return [
            {"job_id": "unknown", "n_mode": "supplemental", "n_state": "CONDITIONAL",
             "n_schedule": "unknown", "n_tier": "near_miss", "n_priority": 72},
            {"job_id": "known", "n_mode": "supplemental", "n_state": "ELIGIBLE",
             "n_schedule": "evening", "n_tier": "near_miss", "n_priority": 70},
            {"job_id": "unconfirmed_day", "n_mode": "supplemental", "n_state": "CONDITIONAL",
             "n_schedule": "daytime_likely", "n_tier": "near_miss", "n_priority": 68},
        ]


class ReportClarityTests(unittest.TestCase):
    def test_unknown_schedule_leads_are_visibly_separate_and_not_duplicated(self):
        with mock.patch("careeros.review.report._night_card",
                        side_effect=lambda _store, row, *_args: f"<job>{row['job_id']}</job>"):
            output = "".join(_night_section(FakeStore(), "2026-11-09T04:00:00.000Z", None, None, POLICY))
        self.assertIn("Unconfirmed hours", output)
        self.assertIn("Unconfirmed hours — check before considering (2)", output)
        self.assertIn("Near-miss (1)", output)
        for job_id in ("unknown", "known", "unconfirmed_day"):
            self.assertEqual(output.count(f"<job>{job_id}</job>"), 1)


if __name__ == "__main__":
    unittest.main()
