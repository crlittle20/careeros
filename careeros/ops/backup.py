"""Daily local backup of the vault's database and the files you wrote by hand (profile, policy overrides).

Each backup is a dated folder under vault\backups (before a migration, a checkpoint folder named for it) with a
consistent SQLite copy (backup API, safe while WAL is in use), an integrity check of the copy, and a manifest of
SHA-256 hashes. Only folders this module created (named by date and holding its manifest) are ever pruned. This protects against corruption and mistakes, not against losing
the disk: an offsite copy is a separate decision.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime
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


def backup(store, paths: dict, *, keep: int = 14, name: str | None = None) -> Path:
    """The day's backup, replacing an earlier one that day. With `name`, a checkpoint of its own (`careeros migrate`'s,
    taken before it migrates): no later backup replaces it and pruning never removes it (Codex CR-2026-11-03-PYTHON-314,
    N1)."""
    day = name or store.clock.now()[:10]
    folder = paths["backups"] / day
    if name and folder.exists():
        raise BackupError(f"a checkpoint of that name exists already: {folder}")
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


# --- the weekly restore drill (OPP-25, D83) ---------------------------------------------------------------------------
DRILL_RECORD = "restore-drill.json"   # beside the dated folders; prune() only ever removes those
DRILL_EVERY_DAYS = 7


def restore_drill(folder: Path, live_jobs: int) -> dict:
    """Read a backup as a restore would, without restoring anything: its files against the manifest and its integrity
    (verify()), then, opened read-only, the event chain `careeros verify` checks, and its jobs against the live count."""
    from careeros.db.readonly import ReadOnlyStore
    problems = verify(folder)
    copy = ReadOnlyStore(folder / "careeros.db")
    try:
        breaks = copy.verify_event_chain()
        counted = copy.one("SELECT (SELECT COUNT(*) FROM jobs) jobs, (SELECT COUNT(*) FROM events) events")
        jobs, events = (counted["jobs"], counted["events"]) if counted else (0, 0)
    finally:
        copy.close()
    if breaks:
        problems.append(f"event_chain_breaks:{len(breaks)}")
    if jobs > live_jobs or (live_jobs and not jobs):
        problems.append(f"jobs:{jobs}_of_{live_jobs}")
    return {"folder": folder.name, "ok": not problems, "problems": problems, "jobs": jobs, "events": events}


def _when(at) -> datetime | None:
    """A drill record's time (UTC, ISO 8601 with Z), or None when it is not one."""
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return when if when.tzinfo else None


def _well_formed(record) -> bool:
    if not (isinstance(record, dict) and isinstance(record.get("ok"), bool) and isinstance(record.get("folder"), str)
            and _when(record.get("at"))):
        return False
    if record["ok"]:
        return all(isinstance(record.get(k), int) and not isinstance(record.get(k), bool) for k in ("jobs", "events"))
    return isinstance(record.get("problems"), list) and all(isinstance(p, str) for p in record["problems"])


def last_drill(backups: Path) -> dict | None:
    """The last drill's record, or None when there is none yet. A record that cannot be read, or does not hold a drill's
    fields, comes back as {"ok": False, "unreadable": True}: the drill is due and status says why (Codex
    CR-2026-11-03-PYTHON-314, F2)."""
    path = backups / DRILL_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"ok": False, "unreadable": True}
    return record if _well_formed(record) else {"ok": False, "unreadable": True}


def drill_due(backups: Path, now: str) -> bool:
    last = last_drill(backups)
    if not last or not last.get("ok"):
        return True
    when = _when(last["at"])   # never None here: last_drill checked it
    return when is None or (datetime.fromisoformat(now.replace("Z", "+00:00")) - when).days >= DRILL_EVERY_DAYS


def record_drill(backups: Path, result: dict, at: str) -> None:
    """Written whole or not at all: a new file, then renamed over the old record."""
    work = backups / f".{DRILL_RECORD}.partial"
    work.write_text(json.dumps({**result, "at": at}, indent=2), encoding="utf-8")
    work.replace(backups / DRILL_RECORD)
