"""Frozen, read-only database copies for the shadow evaluator (SCORING_SHADOW_SPEC 2026-10-16, section 12).

`make_snapshot` copies the live database with SQLite's online backup API, reading the source through a mode=ro
connection: a consistent copy even while a run is writing, without taking the writer lock or touching the source.
`ReadOnlyStore` opens a copy as mode=ro with query_only on. It has the read methods the readers use (`one`, `all`) and
no way to write: `writer()` and `tx()` refuse, and SQLite itself rejects any write statement.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from careeros.core.clock import Clock

from .store import PermissionDenied


def _ro_uri(path: Path) -> str:
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_snapshot(source: Path, dest: Path) -> dict:
    """Copy `source` into a new file `dest` and return {path, sha256, bytes, schema_version}. The copy uses a plain
    rollback journal, so it opens read-only without the WAL side files."""
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"snapshot_exists:{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(_ro_uri(source), uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
        src.close()
    return {"path": str(dest), "sha256": file_sha256(dest), "bytes": dest.stat().st_size,
            "schema_version": schema_version(dest)}


def schema_version(path: Path) -> int | None:
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else None
    finally:
        con.close()


class ReadOnlyStore:
    """The read side of Store over a snapshot. There is no writer: the evaluator can't record anything by mistake."""

    def __init__(self, path: Path | str, clock: Clock | None = None):
        self.path = str(path)
        self.conn = sqlite3.connect(_ro_uri(Path(path)), uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.clock = clock or Clock()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def writer(self, component: str):
        raise PermissionDenied(f"read_only_snapshot:{component}")

    def tx(self):
        raise PermissionDenied("read_only_snapshot")

    def close(self) -> None:
        self.conn.close()
