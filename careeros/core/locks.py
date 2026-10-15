"""Single-writer lock (pattern from the GrowthOS synthetic journal).

A lock left by a crashed process is an operator hold, never silently cleared.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import sys
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
                pid = int(owner.get("pid", 0))
            except (ValueError, OSError):
                raise LockError("lock_unreadable: inspect and remove the lock file manually") from None
            if pid_alive(pid):
                raise LockError("writer_already_running") from None
            raise LockError("stale_lock_held: confirm no CareerOS run is active, then run `careeros unlock`") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "nonce": self.nonce}, handle)
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


def force_unlock(path: Path) -> bool:
    """Operator action: remove a stale lock only if its process is gone."""
    path = Path(path)
    if not path.exists():
        return False
    owner = json.loads(path.read_text(encoding="utf-8"))
    if pid_alive(int(owner.get("pid", 0))):
        raise LockError("writer_still_running")
    path.unlink()
    return True
