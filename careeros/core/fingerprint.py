"""Canonical JSON fingerprints (pattern from GrowthOS command-context.js)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json(value) -> str:
    # allow_nan=False rejects NaN/Infinity; sort_keys makes key order irrelevant.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


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
