"""Daily local backup of the vault's database and the files you wrote by hand (profile, policy overrides).

Each backup is a dated folder under vault\backups with a consistent SQLite copy (backup API, safe while WAL is in
use), an integrity check of the copy, and a manifest of SHA-256 hashes. Only folders this module created (named by
date and holding its manifest) are ever pruned. This protects against corruption and mistakes, not against losing
the disk: an offsite copy is a separate decision.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

MANIFEST = "careeros-backup.json"
FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def backup(store, paths: dict, *, keep: int = 14) -> Path:
    day = store.clock.now()[:10]
    folder = paths["backups"] / day
    work = paths["backups"] / f".{day}.partial"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    target = sqlite3.connect(work / "careeros.db")
    try:
        store.conn.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        jobs = target.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        target.close()
    if check != "ok":
        raise BackupError(f"backup copy failed its integrity check: {check}")
    files = {"careeros.db": work / "careeros.db"}
    for key in ("profile", "overrides"):
        if paths[key].exists():
            files[paths[key].name] = shutil.copy2(paths[key], work / paths[key].name)
    manifest = {"created_at": store.clock.now(), "integrity_check": check, "jobs": jobs,
                "files": {name: {"sha256": _sha256(Path(p)), "bytes": Path(p).stat().st_size} for name, p in files.items()}}
    (work / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if folder.exists():  # a second backup on the same day replaces the first, only once the new one is complete
        _remove(folder)
    work.rename(folder)
    prune(paths["backups"], keep)
    return folder


def _remove(folder: Path) -> None:
    if not (FOLDER.match(folder.name) and (folder / MANIFEST).is_file()):
        raise BackupError(f"refusing to remove a folder CareerOS did not create: {folder}")
    shutil.rmtree(folder)


def prune(root: Path, keep: int) -> list[Path]:
    ours = sorted(p for p in root.iterdir() if p.is_dir() and FOLDER.match(p.name) and (p / MANIFEST).is_file())
    removed = ours[:max(0, len(ours) - keep)]
    for folder in removed:
        _remove(folder)
    return removed


def verify(folder: Path) -> list[str]:
    """Problems with a backup folder (empty list = the files match the manifest and the database is intact)."""
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    problems = [name for name, meta in manifest["files"].items()
                if not (folder / name).is_file() or _sha256(folder / name) != meta["sha256"]]
    conn = sqlite3.connect(f"file:{folder / 'careeros.db'}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("integrity_check")
    finally:
        conn.close()
    return problems
