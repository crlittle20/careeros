"""Temporary folders that are really removed (2026-11-02, item 10 of the lead-resolution batch).

The test suite and the tools leaked thousands of careeros-* folders into the temporary folder (about 1 GB per full
planted-defect run): they removed them with shutil.rmtree(..., ignore_errors=True), which on Windows gives up silently
on a read-only file (git's objects in the release-scan test's repository) or a file still open (an SQLite database
whose connection was not yet collected). `remove_tree` makes read-only files writable, collects garbage first so an
unreferenced connection lets its file go, retries briefly for a file another process holds, and raises when the folder
is still there, so a leak is seen instead of piling up.
"""
from __future__ import annotations

import gc
import os
import shutil
import stat
import time
from pathlib import Path


def _writable(func, target, _exc) -> None:
    os.chmod(target, stat.S_IWRITE)
    func(target)


def remove_tree(path, *, attempts: int = 5) -> None:
    """Remove `path` and everything in it; nothing to do when it is already gone. OSError when it cannot be removed."""
    folder = Path(path)
    for attempt in range(attempts):
        if not folder.exists():
            return
        gc.collect()
        try:
            shutil.rmtree(folder, onexc=_writable)
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    if folder.exists():
        raise OSError(f"temporary folder not removed: {folder}")


def remove_tree_or_say(path) -> bool:
    """For a tool's `finally`: remove the folder, or say on stderr that it is left (and where), never hiding the tool's
    own result. True when removed."""
    try:
        remove_tree(path)
        return True
    except OSError as exc:
        import sys
        print(f"note: {exc}", file=sys.stderr)
        return False
