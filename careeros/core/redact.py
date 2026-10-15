"""Redaction for logs and error text: personal data and secrets never reach log files."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED_TOKEN]"),
    # GitHub tokens, and Google OAuth tokens and client secrets (release finding R2, 2026-10-13)
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?<![\w/])1//[A-Za-z0-9._\-]{10,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{10,}"), "[REDACTED_SECRET]"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"), "[REDACTED_PHONE]"),
    # Long bare numbers (account/ID numbers); not ones embedded in identifiers such as "gh:board:4012345678".
    (re.compile(r"(?<![\w:/\-])\d{9,}(?!\w)"), "[REDACTED_NUMBER]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True
