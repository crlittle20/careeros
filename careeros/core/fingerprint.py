"""Canonical JSON fingerprints (pattern from GrowthOS command-context.js)."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path


def _dates(value):
    """YAML reads an unquoted 2026-10-23 as a date: fingerprint it as the same ISO text (third Claude review,
    2026-10-22: a date in the vault overrides stopped every command). Anything else still can't be fingerprinted."""
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def canonical_json(value) -> str:
    # allow_nan=False rejects NaN/Infinity; sort_keys makes key order irrelevant.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_dates)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()
