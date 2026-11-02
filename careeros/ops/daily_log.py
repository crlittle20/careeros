"""The scheduled run's log file, daily-run.log (OPP-52 and OPP-70, 2026-11-01).

The scheduled task starts `pythonw -m careeros run --output-file ...vault\\logs\\daily-run.log`: there is no window, so
what is printed, and any traceback, must land in that file. `python -m careeros` points stdout and stderr at it before
it imports the engine (careeros/__main__.py), so a broken install, a failed import or a bad vault path leaves a trace
instead of failing in silence every day; `careeros.cli` does it itself when CareerOS was started another way.

Everything written there goes through careeros/core/redact.py, as careeros.log does: this is the file pasted into an
AI session when a run fails. This module needs the standard library and that one module only, so it loads when the
engine does not.
"""
from __future__ import annotations

import atexit
import io
import sys
from datetime import datetime, timezone
from typing import TextIO, cast

from careeros.core.redact import redact

_current: tuple[str, TextIO] | None = None   # the file stdout and stderr point at, and its stream


class RedactingWriter(io.TextIOBase):
    """A text stream that writes whole lines, each redacted, to `handle` and flushes it after each (line-buffered, as
    before). A line is held until it ends, so a secret is never redacted in two halves.

    flush() never writes what is held (Codex CR-2026-11-01-OBS-REVIEW, F1): `print(..., end="", flush=True)` flushes in
    the middle of a line, and two halves redacted apart would put the whole secret in the file. The unfinished last line
    is written, redacted, only by finish(), once nothing more can be written to it: close() calls it, and redirect()
    arranges it at the process's exit, a normal one or an exception's."""
    encoding = "utf-8"   # the log file's, as redirect() opens it

    def __init__(self, handle: TextIO):
        super().__init__()
        self._handle = handle
        self._held = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._held += text
        if "\n" in self._held:
            lines, self._held = self._held.rsplit("\n", 1)
            self._handle.write("".join(redact(line) + "\n" for line in lines.split("\n")))
            self._handle.flush()
        return len(text)

    def flush(self) -> None:
        self._handle.flush()   # what is held stays held: the rest of its line may still come

    def finish(self) -> None:
        """Write the unfinished last line, redacted, when no write can continue it (the process's exit, or close())."""
        if self._held and not self.closed:
            self._handle.write(redact(self._held))
            self._held = ""
            self._handle.flush()

    def close(self) -> None:
        if not self.closed:
            self.finish()
            atexit.unregister(self.finish)
            super().close()   # flushes first
            self._handle.close()


def requested(argv: list[str]) -> str | None:
    """The log file a `run` command line asks for ('--output-file FILE' or '--output-file=FILE'), or None."""
    if argv[:1] != ["run"]:
        return None
    for i, arg in enumerate(argv):
        if arg == "--output-file" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--output-file="):
            return arg.split("=", 1)[1]
    return None


def redirect(path: str) -> None:
    """Point stdout and stderr at the log file, appended and redacted. Once: when they already point at it (cli.py after
    __main__.py), nothing changes, so a run's entry has one header and one stream. At the process's exit (atexit, which
    also runs after an uncaught exception's traceback) the stream writes its unfinished last line, redacted."""
    global _current
    if _current and _current[0] == path and sys.stdout is _current[1] and sys.stderr is _current[1]:
        return
    stream = cast(TextIO, RedactingWriter(open(path, "a", encoding="utf-8", buffering=1)))   # lives as long as the process
    atexit.register(cast(RedactingWriter, stream).finish)   # its unfinished last line, redacted, at exit (normal or not)
    sys.stdout = sys.stderr = stream
    _current = (path, stream)


def load_failed() -> None:
    """Before the traceback of an engine that failed to load: a dated line, as every run's entry starts with one."""
    if _current:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        print(f"=== {now} careeros: the engine failed to load ===")
