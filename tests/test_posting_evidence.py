"""Offline evidence tests for staged ATS activity verification; no network calls."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from careeros.discovery.posting_evidence import verify_posting

CHECKED_AT = "2026-11-08T15:00:00Z"


class FakeFetchError(Exception):
    def __init__(self, code: str):
        self.code = code


def parse_test_url(url: str):
    if url.startswith("https://manual.invalid"):
        return None
    parts = url.split("/")
    return parts[2], parts[3], parts[4]


def fetch_test_posting(ats: str, token: str, job_id: str):
    return SimpleNamespace(title="Synthetic Operations Analyst", external_id=job_id)


class PostingEvidenceTests(unittest.TestCase):
    def evidence(self, url="https://greenhouse/demo/123", trusted=True, fetcher=fetch_test_posting, **options):
        return verify_posting(
            url,
            checked_at=CHECKED_AT,
            board_trusted=trusted,
            parser=parse_test_url,
            fetcher=fetcher,
            **options,
        )

    def test_found_does_not_mean_active(self):
        result = self.evidence()
        self.assertEqual(result.state, "listed_activity_unknown")

    def test_positive_active_requires_trusted_board(self):
        active = lambda _: True
        self.assertEqual(self.evidence(explicitly_accepting_applications=active).state, "active_verified")
        self.assertEqual(self.evidence(trusted=False, explicitly_accepting_applications=active).state,
                         "listed_board_unverified")

    def test_unrecognized_platform_remains_unverified(self):
        self.assertEqual(self.evidence(url="https://workday/demo/123").state, "needs_manual_verification")
        self.assertEqual(self.evidence(url="https://manual.invalid/a").state, "needs_manual_verification")

    def test_mismatched_posting_identity_is_unverified(self):
        mismatch = lambda *_: SimpleNamespace(title="Synthetic Operations Analyst", external_id="another-id")
        self.assertEqual(self.evidence(fetcher=mismatch).failure_code, "posting_identity_mismatch")

    def test_missing_title_is_unverified(self):
        missing = lambda *args: SimpleNamespace(title="", external_id=args[2])
        self.assertEqual(self.evidence(fetcher=missing).failure_code, "posting_title_missing")

    def test_404_confirmation_requires_trust_and_supported_provider(self):
        missing = lambda *_: (_ for _ in ()).throw(FakeFetchError("not_found"))
        for provider in ("greenhouse", "lever"):
            with self.subTest(provider=provider):
                self.assertEqual(self.evidence(url=f"https://{provider}/demo/123", fetcher=missing).state,
                                 "closed_confirmed")
        for provider in ("ashby", "workable"):
            with self.subTest(provider=provider):
                self.assertEqual(self.evidence(url=f"https://{provider}/demo/123", fetcher=missing).state,
                                 "needs_manual_verification")
        self.assertEqual(self.evidence(trusted=False, fetcher=missing).state,
                         "needs_manual_verification")

    def test_network_and_rate_limit_errors_do_not_close(self):
        for code in ("forbidden", "rate_limited", "timeout", "server_error"):
            with self.subTest(code=code):
                failing = lambda *_: (_ for _ in ()).throw(FakeFetchError(code))
                self.assertEqual(self.evidence(fetcher=failing).state, "needs_manual_verification")

    def test_unknown_application_state_is_not_active(self):
        self.assertEqual(self.evidence(explicitly_accepting_applications=lambda _: None).state,
                         "listed_activity_unknown")

    def test_explicit_application_status_failure_is_inconclusive(self):
        self.assertEqual(self.evidence(explicitly_accepting_applications=lambda _: 1 / 0).state,
                         "needs_manual_verification")

    def test_authoritative_closed_application_state(self):
        self.assertEqual(self.evidence(explicitly_accepting_applications=lambda _: False).state,
                         "closed_confirmed")


if __name__ == "__main__":
    unittest.main()
