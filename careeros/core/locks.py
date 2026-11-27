"""Single-writer lock (pattern from the GrowthOS synthetic journal).

A lock left by a crashed process is an operator hold, never silently cleared.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path


class LockError(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Never os.kill() on Windows: any signal other than CTRL events terminates the process.
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied => the process exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start(pid: int) -> int | None:
    """When process `pid` started, as the system counts it (Windows: its creation time; Linux: its start in clock ticks
    since boot), or None when that can't be read. With the id, it names one process: an id Windows or Linux gives to a
    later process never passes for the writer that took the lock (the red team before Codex Pass 15, RT-41)."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, ended, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel),
                                            ctypes.byref(user)):
                return None
            return int(created.value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def process_image(pid: int) -> str | None:
    """The file name of process `pid`'s program ("python.exe"), or None when it can't be read."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value).name
        finally:
            kernel32.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def owner_alive(owner: dict) -> bool:
    """Whether the process a lock names still runs: its id is alive and, when the lock recorded when it started, it is
    that same process. A lock written before start times were recorded counts as gone when its id now runs a program
    that is no Python (CareerOS runs under Python), the one sign left of a reused id."""
    pid = int(owner.get("pid", 0))
    if not pid_alive(pid):
        return False
    started = owner.get("started")
    if isinstance(started, int):
        now = process_start(pid)
        return now is None or now == started
    image = process_image(pid)
    return image is None or "python" in image.lower()


class SingleWriterLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.nonce = secrets.token_hex(16)
        self.held = False

    def acquire(self) -> "SingleWriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = json.loads(self.path.read_text(encoding="utf-8"))
                int(owner.get("pid", 0))
            except (ValueError, OSError, AttributeError):
                raise LockError("lock_unreadable: inspect and remove the lock file manually") from None
            if owner_alive(owner):
                raise LockError("writer_already_running") from None
            raise LockError("stale_lock_held: confirm no CareerOS run is active, then run `careeros unlock`") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "nonce": self.nonce, "started": process_start(os.getpid())}, handle)
        except BaseException:
            # a lock that was never fully taken (a full disk) is removed by the command that failed to take it, never
            # left empty to hold every later writer (the red team's gr1-faults-3)
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self.held = True
        return self

    def release(self) -> None:
        if not self.held:
            return
        owner = json.loads(self.path.read_text(encoding="utf-8"))
        if owner.get("nonce") != self.nonce:
            raise LockError("lock_owner_changed")
        self.path.unlink()
        self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def hold(path: Path) -> tuple[str, str] | None:
    """(since, why) when the lock holds every later writer until you act (OPP-05, 2026-11-01): 'stale' when its process
    is gone, the test `acquire` uses (cleared with `careeros unlock`), or 'unreadable' (inspect it by hand). `since` is the
    lock file's time, UTC. None when there is no lock or its process still runs. Only reads: no lock is removed here."""
    path = Path(path)
    try:
        taken = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return None
    since = taken.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        int(owner.get("pid", 0))
    except FileNotFoundError:   # released while it was read
        return None
    except (ValueError, OSError, AttributeError):
        return since, "unreadable"
    return None if owner_alive(owner) else (since, "stale")


UNREADABLE_AFTER_SECONDS = 60   # a writer writes its lock's body right after creating it: an empty one this old is left


def force_unlock(path: Path) -> bool:
    """Operator action: remove a stale lock only if its process is gone. A lock that cannot be read (empty: a kill or a
    full disk between creating it and writing who holds it) names no process: it is removed once it is older than
    UNREADABLE_AFTER_SECONDS, which no writer takes to write it (the red team's gr1-faults-3). LockError otherwise,
    never a traceback."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        int(owner.get("pid", 0))
    except FileNotFoundError:
        return False
    except (ValueError, OSError, AttributeError):
        try:
            age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        if age < UNREADABLE_AFTER_SECONDS:
            raise LockError("lock_being_written") from None
        path.unlink()
        return True
    if owner_alive(owner):
        raise LockError("writer_still_running")
    path.unlink()
    return True
