"""STAGING PROTOTYPE: conservative, read-only job-posting activity evidence.

Does not perform HTTP requests or change job state. The caller must supply a
policy-approved parser, provider fetcher, and separately verified board trust.
Not yet wired into production ingestion, eligibility, or publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

SUPPORTED = frozenset({"greenhouse", "lever", "ashby", "workable"})


@dataclass(frozen=True)
class PostingEvidence:
    source_url: str
    ats: str | None
    job_id: str | None
    state: str
    checked_at: str
    board_trusted: bool
    title: str | None = None
    failure_code: str | None = None


def verify_posting(
    url: str,
    *,
    checked_at: str,
    board_trusted: bool,
    parser: Callable[[str], tuple[str, str, str] | None],
    fetcher: Callable[[str, str, str], Any],
    explicitly_accepting_applications: Callable[[Any], bool | None] | None = None,
) -> PostingEvidence:
    """Return posting evidence, not permission to publish or recommend.

    Board trust must come from CareerOS's employer-site handshake and registry;
    a submitted link, an HTTP 200, and an aggregator are insufficient evidence.
    An unavailable or inconclusive provider is never treated as closed.
    """
    try:
        parsed = parser(url)
    except (TypeError, ValueError):
        parsed = None
    if not parsed or parsed[0] not in SUPPORTED:
        return PostingEvidence(
            url, None, None, "needs_manual_verification", checked_at, board_trusted,
            failure_code="unsupported_or_invalid_url",
        )
    ats, token, job_id = parsed
    if not token or not job_id:
        return PostingEvidence(
            url, ats, job_id or None, "needs_manual_verification", checked_at, board_trusted,
            failure_code="missing_provider_identity",
        )
    try:
        posting = fetcher(ats, token, job_id)
    except Exception as exc:
        code = getattr(exc, "code", "unknown_fetch_error")
        if code == "not_found" and ats in ("greenhouse", "lever") and board_trusted:
            return PostingEvidence(
                url, ats, job_id, "closed_confirmed", checked_at, board_trusted,
                failure_code="authoritative_posting_not_found",
            )
        return PostingEvidence(
            url, ats, job_id, "needs_manual_verification", checked_at, board_trusted,
            failure_code=str(code)[:80],
        )

    if posting is None or str(getattr(posting, "external_id", "")) != str(job_id):
        return PostingEvidence(
            url, ats, job_id, "needs_manual_verification", checked_at, board_trusted,
            failure_code="posting_identity_mismatch",
        )
    title = str(getattr(posting, "title", "") or "").strip()
    if not title:
        return PostingEvidence(
            url, ats, job_id, "needs_manual_verification", checked_at, board_trusted,
            failure_code="posting_title_missing",
        )
    if not board_trusted:
        return PostingEvidence(url, ats, job_id, "listed_board_unverified", checked_at, False, title=title)
    try:
        active = explicitly_accepting_applications(posting) if explicitly_accepting_applications else None
    except Exception:
        return PostingEvidence(
            url, ats, job_id, "needs_manual_verification", checked_at, True, title=title,
            failure_code="application_status_unreadable",
        )
    if active is True:
        return PostingEvidence(url, ats, job_id, "active_verified", checked_at, True, title=title)
    if active is False:
        return PostingEvidence(
            url, ats, job_id, "closed_confirmed", checked_at, True, title=title,
            failure_code="provider_reports_applications_closed",
        )
    return PostingEvidence(url, ats, job_id, "listed_activity_unknown", checked_at, True, title=title)
