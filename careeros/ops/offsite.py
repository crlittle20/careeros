"""The offsite archive (OPP-26, the owner's decision D97, 2026-11-03).

Every daily backup is a folder in vault\\backups, on the same disk as the database: a dead disk, a lost laptop or
ransomware takes them all. After the daily backup, this writes one encrypted archive of the newest verified backup's
database and the vault folders you wrote by hand into a folder you choose (a OneDrive folder), and keeps the newest 7.

- Off until you set `operations.offsite.folder` in your vault overrides and save a passphrase in Windows Credential
  Manager as `CareerOS/offsite`. Keep a second copy of the passphrase outside this PC: without it no archive opens.
- GnuPG, symmetric AES-256 with a SHA-512 key derivation at its highest count (OpenPGP), so any machine with GnuPG can
  decrypt it (`gpg --decrypt`). Git for Windows ships gpg. The passphrase reaches gpg on its standard input, never on a
  command line, in a file or in a log. gpg runs with a private, empty home of its own and never starts an agent.
- Nothing unencrypted is written to that folder. The zip is built in the vault (`backups\\.offsite-work`, removed
  afterwards); the archive is written under a hidden partial name, checked to be an OpenPGP symmetric message, decrypted
  back and compared byte for byte with the zip, and only then renamed into place. A failure leaves no partial file.
- Only CareerOS's own archives (`careeros-offsite-YYYY-MM-DD.zip.gpg`) are ever removed, oldest first.
- The weekly restore drill also decrypts the newest archive in memory and checks every file against its manifest.
- Every check above uses the Credential Manager copy of the passphrase, which is lost with this PC. `careeros offsite
  confirm` asks for the copy you keep off this PC and opens the newest archive with it, as a restore elsewhere would;
  status warns until it has (pre-review #1, D111). Only the date and the archive are recorded, never the passphrase.
- Never in Demo Mode: it would start another program and write outside the demo vault.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from . import backup as backups

CREDENTIAL = "CareerOS/offsite"          # Windows Credential Manager, generic credential
ARCHIVE = re.compile(r"^careeros-offsite-(\d{4}-\d{2}-\d{2})\.zip\.gpg$")
MANIFEST = "careeros-offsite.json"      # inside the archive
RECORD = "offsite.json"                 # beside the dated backup folders (backups.prune only removes those)
CONFIRMED = "offsite-confirmed.json"    # beside it: the last time the copy kept off this PC was tried on an archive
CHANGED = "the archive changed while it was being checked; nothing was recorded"   # D149 R-O1
NOT_MADE = "the archive it opened is not one this PC made with the current stored passphrase"   # D149 red team 6
WORK = ".offsite-work"                  # in vault\backups, each call's own folder `.offsite-work-<unique>`: the
STALE_SECONDS = 24 * 3600               # unencrypted zip lives only there, only during one call (D149 red team 7, 7b)
KEEP = 7
HAND_WRITTEN = ("profile", "policy", "sources", "documents", "drafts", "packages",   # vault folders that can't be re-created,
                "reports/calibration", "reports/powerbi")   # with the write-once calibration lists and your Power BI report (#4)
GPG_CANDIDATES = (r"C:\Program Files\Git\usr\bin\gpg.exe", r"C:\Program Files\GnuPG\bin\gpg.exe",
                  r"C:\Program Files (x86)\GnuPG\bin\gpg.exe")
# The key derivation is stated, so gpg never asks an agent to calibrate it (Git for Windows' gpg can't start one when
# a Windows program runs it, and would report an error after encrypting): SHA-512, iterated and salted, highest count.
CIPHER = ["--cipher-algo", "AES256", "--s2k-mode", "3", "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
          "--compress-algo", "none"]   # the zip is already compressed
OPENPGP_SYMMETRIC = (0x8C, 0x8D, 0x8E, 0x8F, 0xC3)   # the first packet: a symmetric-key encrypted session key (tag 3)


class OffsiteError(RuntimeError):
    pass


def _work(area: Path) -> Path:
    """D149 red team 7, 7b: a private work folder of this call's own, uniquely named in `area` (vault\\backups). No other
    call (a second confirm, the daily run's make or drill) knows it, writes it or removes it; its caller removes it."""
    area.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{WORK}-", dir=area))


def _sweep(area: Path, now: float | None = None) -> None:
    """Work folders a killed call left behind, a day old or more (none lives longer than gpg's 15 minutes, and the
    passphrase prompt comes before one is made): removed, so no unencrypted zip lingers in the vault."""
    now = time.time() if now is None else now
    for path in area.glob(f"{WORK}*") if area.is_dir() else []:
        if (path.name == WORK or path.name.startswith(f"{WORK}-")) and path.is_dir():
            try:
                if now - path.stat().st_mtime >= STALE_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass




def settings(policy) -> tuple[Path | None, int, str | None]:
    """(folder or None when off, archives kept, configured gpg or None)."""
    folder = policy.get("operations", "offsite", "folder")
    keep = max(1, int(policy.get("operations", "offsite", "keep", default=KEEP)))
    return (Path(folder) if folder else None), keep, policy.get("operations", "offsite", "gpg")


def find_gpg(configured: str | None = None) -> str | None:
    """The configured gpg, else gpg on PATH, else the one Git for Windows or Gpg4win installs."""
    for candidate in ([configured] if configured else []) + [shutil.which("gpg"), *GPG_CANDIDATES]:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _gpg(gpg: str, home: Path, args: list[str], passphrase: str, data: bytes = b"") -> subprocess.CompletedProcess:
    """gpg with its own empty home, run inside it (`--homedir .`: Git for Windows' gpg misreads a Windows path given as
    its home), no agent, and the passphrase as the first line of its standard input. `data`, when given, follows that
    line on the same standard input: gpg reads the passphrase from fd 0 byte by byte up to the line break when it starts,
    then the message from the rest (`--decrypt` with no file), so it decrypts exactly those bytes (D155). Windows gives a
    child no other descriptor to pass the passphrase on, and a file or a command line would expose it."""
    command = [gpg, "--batch", "--quiet", "--no-tty", "--no-autostart", "--homedir", ".", "--pinentry-mode", "loopback",
               "--passphrase-fd", "0", "--no-symkey-cache", *args]
    try:
        return subprocess.run(command, input=(passphrase + "\n").encode("utf-8") + data, capture_output=True, cwd=home,
                              timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffsiteError(f"gpg could not run: {type(exc).__name__}") from exc


def _said(done: subprocess.CompletedProcess) -> str:
    lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return f" (gpg: {lines[-1][:200]})" if lines else f" (gpg exit {done.returncode})"


def _check_passphrase(passphrase: str | None) -> str:
    if not passphrase:
        raise OffsiteError(f"no passphrase: save one in Windows Credential Manager as {CREDENTIAL}")
    if "\n" in passphrase or "\r" in passphrase:
        raise OffsiteError("the passphrase has a line break; gpg reads only its first line")
    return passphrase


def _zip(backup_folder: Path, vault: Path, target: Path, at: str) -> dict:
    """The newest backup's database and manifest plus the hand-written vault folders, with a manifest of SHA-256 hashes."""
    files = {"careeros.db": backup_folder / "careeros.db", backups.MANIFEST: backup_folder / backups.MANIFEST}
    for name in HAND_WRITTEN:
        root = vault / name
        if root.is_dir():
            files.update({f"vault/{name}/{p.relative_to(root).as_posix()}": p for p in sorted(root.rglob("*")) if p.is_file()})
    manifest = {"created_at": at, "backup": backup_folder.name,
                "files": {arc: {"sha256": _sha256(p), "bytes": p.stat().st_size} for arc, p in files.items()}}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arc, path in files.items():
            archive.write(path, arc)
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
    return manifest


def make(backup_folder: Path, vault: Path, destination: Path, *, passphrase: str | None, gpg: str | None, at: str,
         keep: int = KEEP) -> dict:
    """Write today's archive of `backup_folder` into `destination` and prune to `keep`. Raises OffsiteError, leaving
    nothing behind in `destination`, when the folder, the passphrase or gpg is missing, the backup does not verify, or
    the archive does not decrypt back to exactly the zip that was encrypted."""
    passphrase = _check_passphrase(passphrase)
    if not gpg:
        raise OffsiteError("gpg not found: install Git for Windows or GnuPG, or set operations.offsite.gpg")
    if not destination.is_dir():
        raise OffsiteError(f"the offsite folder does not exist: {destination}")
    problems = backups.verify(backup_folder)
    if problems:
        raise OffsiteError(f"the backup {backup_folder.name} does not verify: {', '.join(problems)}")
    name = f"careeros-offsite-{at[:10]}.zip.gpg"
    partial = destination / f".{name}.partial"
    _sweep(backup_folder.parent)
    work = _work(backup_folder.parent)
    try:
        (work / "gnupg").mkdir()
        plain = work / "archive.zip"
        manifest = _zip(backup_folder, vault, plain, at)
        partial.unlink(missing_ok=True)
        done = _gpg(gpg, work / "gnupg", ["--yes", "--symmetric", *CIPHER, "--output", str(partial), str(plain)], passphrase)
        if done.returncode != 0 or not partial.is_file():
            raise OffsiteError("gpg did not encrypt the archive" + _said(done))
        with partial.open("rb") as handle:
            first = handle.read(1)
        if not first or first[0] not in OPENPGP_SYMMETRIC:
            raise OffsiteError("the archive is not an OpenPGP symmetric message")
        back = _gpg(gpg, work / "gnupg", ["--decrypt", str(partial)], passphrase)
        if back.returncode != 0 or hashlib.sha256(back.stdout).hexdigest() != _sha256(plain):
            raise OffsiteError("the archive did not decrypt back to the zip that was encrypted" + _said(back))
        size, digest = partial.stat().st_size, _sha256(partial)
        os.replace(partial, destination / name)
    except BaseException:
        partial.unlink(missing_ok=True)   # never a partial archive in the offsite folder
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)   # the unencrypted zip never outlives this call
    removed = prune(destination, keep)
    # The archive's SHA-256 identifies the file itself, not only its dated name: `offsite confirm` binds the key's
    # generation only to this very file (F8-03, D120: a same-named archive restored to an older version or made in
    # another folder must not carry this record's key)
    return {"ok": True, "archive": name, "bytes": size, "files": len(manifest["files"]), "backup": backup_folder.name,
            "removed": [p.name for p in removed], "problems": [], "folder": str(destination), "sha256": digest}


def made_this(record: dict | None, archive: Path, digest: str | None = None) -> bool:
    """True when the attempt `record` made exactly `archive`: the same name and the same SHA-256. Bytes identical to the
    archive the record made were made under the same key generation, wherever they now sit; a same-named archive made
    elsewhere or later, or a file reverted to an older version, has other bytes. A record without the hash (written
    before 2026-11-09) never matches: status then asks again. `digest`, when given, is the SHA-256 of the bytes actually
    checked (the snapshot `confirm` decrypts, D149 R-O1), so the file on disk is not read again."""
    if digest is None:
        digest = _sha256(archive) if archive.is_file() else None
    return bool(record and record.get("ok") and record.get("archive") == archive.name
                and isinstance(record.get("sha256"), str) and digest is not None and record["sha256"] == digest)


def ours(destination: Path) -> list[Path]:
    """CareerOS's own archives in the folder, oldest first (by the date in the name)."""
    return sorted(p for p in destination.iterdir() if p.is_file() and ARCHIVE.match(p.name)) if destination.is_dir() else []


def prune(destination: Path, keep: int) -> list[Path]:
    """Remove CareerOS's oldest archives beyond `keep`; any other file in the folder is never touched."""
    archives = ours(destination)
    removed = archives[:max(0, len(archives) - max(1, keep))]
    for path in removed:
        path.unlink()
    return removed


def drill(destination: Path, *, passphrase: str | None, gpg: str | None, area: Path) -> dict:
    """Read the newest archive back as a restore would: decrypt it in memory, then check every file against the
    archive's manifest and the database's integrity. Nothing is written but gpg's empty home, in a work folder of this
    call's own in `area` (vault\\backups), removed afterwards."""
    archives = ours(destination)
    if not archives:
        return {"ok": False, "archive": None, "problems": ["no archive in the offsite folder"]}
    newest = archives[-1]
    try:
        data = newest.read_bytes()   # D155: read once; gpg decrypts exactly these bytes
    except OSError as exc:
        return {"ok": False, "archive": newest.name, "problems": [f"{type(exc).__name__}: {exc}"]}
    return _read_back(newest, data, passphrase=passphrase, gpg=gpg, area=area)


def _read_back(newest: Path, data: bytes, *, passphrase: str | None, gpg: str | None, area: Path) -> dict:
    """Decrypt `data`, the bytes of the archive `newest` as read once, and check every file against its manifest.
    D155 (Codex CR-2026-11-05-COMBINED Pass 3, F8): the bytes reach gpg on its standard input, after the passphrase
    line; no file of them is written for gpg to read by path, so nothing written or replaced on disk while gpg runs can
    change what it decrypts. gpg's empty home is in a work folder of this call's own in `area`, removed afterwards."""
    try:
        passphrase = _check_passphrase(passphrase)
        if not gpg:
            raise OffsiteError("gpg not found")
        work = _work(area)
        try:
            (work / "gnupg").mkdir()
            done = _gpg(gpg, work / "gnupg", ["--decrypt"], passphrase, data=data)   # D155: the bytes, never a path
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if done.returncode != 0:
            raise OffsiteError("it did not decrypt" + _said(done))
        problems = []
        with zipfile.ZipFile(io.BytesIO(done.stdout)) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            for arc, meta in manifest["files"].items():
                digest = hashlib.sha256()
                with archive.open(arc) as member:
                    for block in iter(lambda: member.read(1 << 20), b""):
                        digest.update(block)
                if digest.hexdigest() != meta["sha256"]:
                    problems.append(arc)
    except (OffsiteError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        return {"ok": False, "archive": newest.name, "problems": [f"{type(exc).__name__}: {exc}"]}
    return {"ok": not problems, "archive": newest.name, "files": len(manifest["files"]), "problems": problems}


def confirm(destination: Path, *, typed: str | None, gpg: str | None, backups_root: Path, at: str) -> dict:
    """Open the newest archive with the passphrase as you keep it off this PC, typed in, and read every file back, as a
    restore on another computer would (pre-review #1, D111). Every other check uses the Credential Manager copy; this one
    never reads it. The outcome is recorded whether or not the archive opened: its date and archive, the folder, and
    `key`, the generation of the stored passphrase that made that archive, from its record (Codex CR-2026-11-05-COMBINED,
    F8: status holds the confirmation only while both are current). Never the passphrase, nor anything that could test
    one.

    D149 R-O1 (Codex Pass 2, G): everything rests on ONE snapshot of the archive's bytes, read once after the prompt: gpg
    opens that snapshot, and the key generation and the SHA-256 recorded are that snapshot's. The archive is hashed again
    just before recording; if it is not the snapshot any more (replaced, deleted, unreadable), nothing is recorded and the
    result says so ("recorded": False). Status holds the confirmation only while the archive still has that SHA-256.

    D149 red team 6: "bound" says whether a key generation was bound; when not, the passphrase opened bytes this PC did
    not make with the current key, and that is no confirmation. Red team 7, 7b: the snapshot copy lives in this call's
    own work folder in `backups_root`; if it went missing or changed around gpg, nothing is recorded ("recorded": False)."""
    archives = ours(destination)
    if not archives:   # gone since the command looked: nothing was tried, so nothing is recorded
        return {"ok": False, "archive": None, "recorded": False, "problems": [CHANGED]}
    newest = archives[-1]
    try:
        snapshot = newest.read_bytes()
    except OSError:
        return {"ok": False, "archive": newest.name, "recorded": False, "problems": [CHANGED]}
    digest = hashlib.sha256(snapshot).hexdigest()
    result = _read_back(newest, snapshot, passphrase=typed, gpg=gpg, area=backups_root)   # D155: these bytes, on stdin
    # the generation that made THESE bytes, from the archive record read after the prompt; any other bytes have none
    made = last(backups_root) or {}
    key = made.get("key") if made_this(made, newest, digest) else None
    try:
        still = _sha256(newest)
    except OSError:
        still = None
    if still != digest:   # the archive changed after the snapshot: what opened is not what the folder holds
        return {**result, "ok": False, "recorded": False, "problems": [CHANGED]}
    entry = {"at": at, "archive": result["archive"], "ok": result["ok"], "folder": str(destination), "key": key}
    entry["sha256"] = digest
    partial = backups_root / f".{CONFIRMED}.partial"
    partial.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    partial.replace(backups_root / CONFIRMED)
    return {**result, "bound": key is not None}


def not_replaced(destination: Path, entry: dict) -> bool:
    """D149 R-O1: False when a file of the name a confirmation opened is in `destination` with other bytes than the
    SHA-256 recorded then (or cannot be read), and for a record without the hash (written before D149). A confirmed
    archive that was pruned or removed keeps it: that does not change which key opens the archives (the key generation
    is checked apart)."""
    name, digest = entry.get("archive"), entry.get("sha256")
    if not (isinstance(name, str) and ARCHIVE.match(name) and isinstance(digest, str)):
        return False
    path = destination / name
    if not path.exists():
        return True   # pruned or removed: no other bytes under that name
    try:
        return _sha256(path) == digest
    except OSError:
        return False


def confirmed(backups_root: Path) -> dict | None:
    """The last try of the copy kept off this PC, or None when there is none or its record cannot be read (status then
    asks for one)."""
    try:
        entry = json.loads((backups_root / CONFIRMED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(entry, dict) and isinstance(entry.get("ok"), bool) and backups._when(entry.get("at"))
            and isinstance(entry.get("archive"), (str, type(None)))):
        return None
    return entry


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _texts(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _well_formed(entry) -> bool:
    """The fields an attempt and its drill write, their times included."""
    if not (isinstance(entry, dict) and isinstance(entry.get("ok"), bool) and backups._when(entry.get("at"))):
        return False
    if entry["ok"] and not (isinstance(entry.get("archive"), str) and _count(entry.get("bytes")) and _count(entry.get("files"))):
        return False
    if not entry["ok"] and not _texts(entry.get("problems")):
        return False
    drill = entry.get("drill")
    return drill is None or (isinstance(drill, dict) and isinstance(drill.get("ok"), bool) and bool(backups._when(drill.get("at")))
                             and _texts(drill.get("problems")) and isinstance(drill.get("archive"), (str, type(None))))


def last(backups_root: Path) -> dict | None:
    """The latest attempt, or None when there is none yet. A record that cannot be read, or does not hold an attempt's
    fields, comes back as {"ok": False, "unreadable": True}, as the restore drill's does (Codex CR-2026-11-03-PYTHON-314,
    F2, found there and applied here)."""
    try:
        entry = json.loads((backups_root / RECORD).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"ok": False, "unreadable": True}
    return entry if _well_formed(entry) else {"ok": False, "unreadable": True}


def record(backups_root: Path, result: dict, at: str) -> None:
    """The latest attempt, kept with the latest drill of the archives (a new attempt keeps the drill's result). Written
    whole or not at all: a new file, then renamed over the old record."""
    previous = last(backups_root) or {}
    entry = {**result, "at": at}
    if "drill" not in entry and previous.get("drill"):
        entry["drill"] = previous["drill"]
    work = backups_root / f".{RECORD}.partial"
    work.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    work.replace(backups_root / RECORD)
