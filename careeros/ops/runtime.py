"""What the engine runs on, compared with what it was reviewed on (OPP-55, D83).

`careeros status` names the Python running the engine and checks every pin in requirements.lock against the version
installed. After a venv is rebuilt (the 2026-11-03 Python 3.14 batch swapped the whole runtime), a pinned package
that is missing, or installed at a version the lock does not name, shows here instead of going unnoticed. It checks
versions only: not where a package came from (pip checks the hashes when it installs from the lock), and not packages
the lock does not name (Codex CR-2026-11-03-PYTHON-314, N2).
"""
from __future__ import annotations

import platform
import re
from importlib import metadata
from pathlib import Path

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def locked(lock_path: Path) -> dict[str, str]:
    """The `name==version` pins of a lock file, hash lines and comments aside."""
    pins = {}
    for line in Path(lock_path).read_text(encoding="utf-8").splitlines():
        match = PIN.match(line.strip())
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def differences(lock_path: Path) -> list[str]:
    """One entry per pin the environment does not match: "name installed X (lock Y)" or "name missing (lock Y)"."""
    found = []
    for name, version in sorted(locked(lock_path).items(), key=lambda item: item[0].lower()):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            found.append(f"{name} missing (lock {version})")
            continue
        if installed != version:
            found.append(f"{name} installed {installed} (lock {version})")
    return found


def summary(lock_path: Path) -> tuple[str, list[str]]:
    """The status line, and the differences (empty when every pin matches)."""
    pins, found = locked(lock_path), differences(lock_path)
    head = f"runtime: Python {platform.python_version()}; "
    if not found:
        return head + f"all {len(pins)} packages as in {Path(lock_path).name}", []
    return head + f"{len(found)} of {len(pins)} packages differ from {Path(lock_path).name}", found
