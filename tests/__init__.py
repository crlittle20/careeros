"""The test suite never reads a real vault's private site knowledge: careeros/core/private_sites.py reads the synthetic
fixture instead (refactor R4, 2026-10-14).

Every careeros-* temporary folder made in this test process is noted (item 10 of the lead-resolution batch, 2026-11-02),
so the suite's last module (tests/test_zz_temporary_folders.py) can fail when one is left behind."""
import os
import tempfile
from pathlib import Path

os.environ["CAREEROS_PRIVATE_SITES"] = str(Path(__file__).parent / "fixtures" / "private_sites.yaml")

MADE: list[str] = []   # careeros-* folders made by tempfile.mkdtemp (and TemporaryDirectory) in this process
_mkdtemp = tempfile.mkdtemp


def _noted_mkdtemp(suffix=None, prefix=None, dir=None):  # noqa: A002 - tempfile's own parameter name
    path = _mkdtemp(suffix, prefix, dir)
    if str(prefix or "").startswith("careeros-"):
        MADE.append(path)
    return path


if getattr(tempfile.mkdtemp, "__name__", "") != "_noted_mkdtemp":   # once, however often the package is imported
    tempfile.mkdtemp = _noted_mkdtemp

# The tests change the private settings file part-way through runs to test the checks that refuse such a change; the
# lock that keeps it from changing during a run on Windows (D147) has tests of its own, which turn it back on.
from careeros.core import private_sites as _private_sites  # noqa: E402

_private_sites.LOCK_RUNS = False
