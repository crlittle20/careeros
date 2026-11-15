"""Private site knowledge (refactor R4, 2026-10-14): sites, systems and agencies tied to the job seeker's own situation.

They live in the vault (`policy/private_sites.yaml`), never in this repository, so the shared code holds no personal
specifics. Each list here ADDS to the engine's generic ones:
- never_fetch:       domains never fetched and never treated as an employer's official website;
- manual_systems:    {domain: label} for application systems a link is listed under, never read automatically;
- sources:           {label: allowed | manual | blocked} site-policy statuses (anything unlisted is manual anyway);
- staffing_agencies: agencies whose hiring clients are never guessed, so their boards are never probed;
- agency_shortcut:   an agency offered as a one-click choice when recording an application on the review page;
- agency_digests:    job-alert digest emails from an agency, read card by card from the email alone (never by visiting
                     the agency's site): each entry names the agency, its site label, the sender's email domain and
                     the host of the personal click-tracking links on its Apply buttons (2026-11-09). Leads
                     already stored keep their own lasting agency mark (alerts.AGENCY_LEAD), so they stay local
                     whatever this list says later. To stop reading a digest, set its site to blocked under `sources`;
- sites:             reviewed notes on those sites (documentation only; never read by the code).

Nothing is read in Demo Mode, and an absent file means no additions. CAREEROS_PRIVATE_SITES names another file, or,
when set but empty, no file at all (the test suite sets it so it never depends on a real vault).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import NamedTuple

import yaml

from careeros.core.policy import load_yaml

ENV = "CAREEROS_PRIVATE_SITES"
STATUSES = ("allowed", "manual", "blocked")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class AgencyDigest(NamedTuple):
    """One agency's job-alert digest: `name` is shown as the employer line of its leads, `site` is its site label (its
    `sources` status applies), mail must come from an address at `sender_domain`, and an Apply button must link to
    https://`tracker_host`/... (the personal tracking link, never fetched). `layout` is the fingerprint of the digest's
    links that are not job buttons (header, navigation, footer), from `careeros agency-layout` on a saved digest: a
    digest is read in full only when its layout matches it, so a changed template is held, never guessed at."""
    name: str
    site: str
    sender_domain: str
    tracker_host: str
    layout: str = ""
    former_hosts: tuple[str, ...] = ()   # tracking hosts the agency used before: their stored links stay local


def _agency_digests(data) -> tuple[AgencyDigest, ...]:
    if data is None:
        return ()
    if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
        raise ValueError("private_sites_invalid: agency_digests must be a list of mappings")
    digests = []
    for entry in data:
        former = entry.get("former_hosts") or []
        if not isinstance(former, list) or not all(_HOST.fullmatch(str(h).strip().lower()) for h in former):
            raise ValueError("private_sites_invalid: an agency digest's former_hosts must be a list of plain host names")
        values = {key: str(entry.get(key) or "").strip() for key in AgencyDigest._fields if key != "former_hosts"}
        if not all(values[key] for key in ("name", "site", "sender_domain", "tracker_host")):
            raise ValueError("private_sites_invalid: each agency digest needs name, site, sender_domain and tracker_host")
        if values["layout"] and not re.fullmatch(r"[0-9a-f]{16}", values["layout"].lower()):
            raise ValueError("private_sites_invalid: an agency digest's layout must be the 16-character fingerprint "
                             "that careeros agency-layout prints")
        values["layout"] = values["layout"].lower()
        for key in ("site", "sender_domain", "tracker_host"):
            values[key] = values[key].lower()
        if not (_HOST.fullmatch(values["sender_domain"]) and _HOST.fullmatch(values["tracker_host"])):
            raise ValueError("private_sites_invalid: an agency digest's sender_domain and tracker_host must be plain host names")
        digests.append(AgencyDigest(**values, former_hosts=tuple(str(h).strip().lower() for h in former)))
    return tuple(digests)


def _path() -> Path | None:
    configured = os.environ.get(ENV)
    if configured is not None:
        return Path(configured) if configured.strip() else None
    from careeros.paths import vault_paths
    return vault_paths()["private_sites"]


def _read(path: str, raw: bytes | None = None) -> dict:
    """The file's mapping (from `raw`, the bytes already read, when given). Every way it can fail to read (unreadable,
    not UTF-8, not YAML, not a mapping) is the one error the callers handle, "private_sites_invalid", naming where, never
    quoting the file (CR-2026-11-09-INTAKE-HOTFIX CR-HF-02: a YAML syntax error escaped both alert preflights)."""
    try:
        if raw is None:
            return load_yaml(Path(path))
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("not a mapping")
        return data
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        raise ValueError(f"private_sites_invalid: the file is not valid YAML{where}") from None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"private_sites_invalid: the file cannot be read ({type(exc).__name__})") from None
    except (RecursionError, MemoryError) as exc:   # nested thousands deep, or too large (Pass 2, CR-HF-02, D126)
        raise ValueError(f"private_sites_invalid: the file is nested too deeply or too large ({type(exc).__name__})") from None
    except ValueError:   # load_yaml's own: the file is not a mapping
        raise ValueError("private_sites_invalid: the file must be a mapping of the keys above") from None


def _additions(data: dict) -> dict:
    if any(not isinstance(data.get(key) or [], list) for key in ("never_fetch", "staffing_agencies")):
        raise ValueError("private_sites_invalid: never_fetch and staffing_agencies must be lists")
    lists = {key: [str(v).strip().lower() for v in data.get(key) or []] for key in ("never_fetch", "staffing_agencies")}
    systems = data.get("manual_systems") or {}
    sources = data.get("sources") or {}
    if not isinstance(systems, dict) or not isinstance(sources, dict):
        raise ValueError("private_sites_invalid: manual_systems and sources must be mappings")
    if any(str(v).strip().lower() not in STATUSES for v in sources.values()):
        raise ValueError("private_sites_invalid: a source status is not allowed, manual or blocked")
    shortcut = data.get("agency_shortcut")
    return {**lists,
            "manual_systems": tuple((str(d).strip().lower(), str(label).strip().lower()) for d, label in systems.items()),
            "sources": {str(k).strip().lower(): str(v).strip().lower() for k, v in sources.items()},
            "agency_shortcut": str(shortcut).strip() if shortcut else None,
            "agency_digests": _agency_digests(data.get("agency_digests"))}


# Each file's last reading: its stat (times, size, file id), the sha256 of its bytes and its additions. A reading is
# reused only while the stat is unchanged; a changed stat reads the bytes again, and a changed content parses them again,
# so a rewrite in place or an atomic replacement is seen at once, never only after a restart (CR-AD-V2-07).
_CACHE: dict[str, tuple[tuple, str, dict]] = {}
# One revision pinned for a unit of work (one email, one export): every read inside it sees the same additions.
_PINNED: ContextVar[tuple[str, dict] | None] = ContextVar("careeros_private_sites_pinned", default=None)
ABSENT = "absent"   # the revision when there is no file (or none in Demo Mode)


def _snapshot(path: str, verify: bool = False) -> tuple[str, dict]:
    """(revision, additions) of the file. `verify` reads and hashes the content even when the stat looks unchanged: the
    fail-closed checks before intake, export and discovery use it, so a same-size rewrite within one clock tick is seen
    too."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == key and not verify:
            return cached[1], cached[2]
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        _CACHE.pop(path, None)
        return ABSENT, {}
    except OSError as exc:
        raise ValueError(f"private_sites_invalid: the file cannot be read ({type(exc).__name__})") from None
    revision = hashlib.sha256(raw).hexdigest()
    data = cached[2] if cached is not None and cached[1] == revision else _additions(_read(path, raw))
    _CACHE[path] = (key, revision, data)
    return revision, data


def _load(path: str) -> dict:
    return _snapshot(path)[1]


def _now_reading() -> tuple[str, dict]:
    """(revision, additions) of the file as it is now, its content always read and hashed. Demo Mode reads nothing."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():
        return ABSENT, {}
    path = _path()
    return _snapshot(str(path), verify=True) if path is not None else (ABSENT, {})


# One reading for a whole run (Codex Pass 3, D141): the first read inside run() is kept, as is a failure to read, and
# every later read of the run returns it. A change of the file during the run is never read by it: the run's checks
# (check(), the end of pinned()) refuse to commit or send anything once the file differs, and the next run reads it.
# On Windows the run also holds the file open so that no other program can write, replace or delete it while the run
# lasts (D147): an edit then cannot land part-way through a run at all, and the checks stay as the second line. A
# missing file is made, empty, under the same hold (D153 R1): an empty file means no private sites, as a missing one
# does, and no file can then be created part-way through a run either (Codex Pass 5).
LOCK_RUNS = sys.platform == "win32"   # the test suite turns it off: it changes the file part-way to test the checks
_BUSY_TRIES, _BUSY_WAIT = 5, 0.2   # an editor saving the file this very moment: tried again shortly, then refused


class _Run:
    """One run's reading (taken at its first use, or the failure to read it), the handle that keeps the file from
    changing while the run lasts, and the change once one is seen: from then on the run stays stopped, whatever the
    file holds later, its starting bytes included (Codex Pass 4)."""
    __slots__ = ("reading", "changed", "handle")

    def __init__(self):
        self.reading = None
        self.changed: str | None = None
        self.handle = None


_RUN: ContextVar[_Run | None] = ContextVar("careeros_private_sites_run", default=None)


def _run_reading(run: _Run) -> tuple[str, dict]:
    if run.reading is None:
        try:
            run.reading = _now_reading()
        except ValueError as exc:
            run.reading = exc
    if isinstance(run.reading, ValueError):
        raise ValueError(str(run.reading))
    return run.reading


def _hold_open(path: str):
    """A Windows handle on the settings file that lets other programs read it but never write, replace, rename or
    delete it (D147). A missing file (and its folder) is made, empty, and held the same way (D153 R1): no run is ever
    without the hold. ValueError ("private_sites_invalid") when another program has it open for writing even after a
    short wait, or it cannot be opened or made at all: nothing of the run is read then."""
    import ctypes
    import time
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    error = 0
    for attempt in range(_BUSY_TRIES):
        handle = create(path, 0x80000000, 0x1, None, 4, 0x80, None)   # GENERIC_READ, shared for reading only, OPEN_ALWAYS
        if handle not in (None, ctypes.c_void_p(-1).value):          # (a missing file is made empty, already held)
            return handle
        error = ctypes.get_last_error()
        if error == 3:   # no folder for it yet: made, and the file made in it on the next try
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                break
            continue
        if error not in (32, 33) or attempt == _BUSY_TRIES - 1:
            break
        time.sleep(_BUSY_WAIT)
    if error in (32, 33):
        raise ValueError("private_sites_invalid: the private settings file is open for writing in another program (an "
                         "editor saving it?); this run reads none of it, and the next one tries again")
    raise ValueError(f"private_sites_invalid: the private settings file cannot be opened and held for this run (Windows "
                     f"error {error}); this run reads none of it, and the next one tries again")


def _release(handle) -> None:
    import ctypes
    from ctypes import wintypes
    close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close(handle)


def stopped() -> bool:
    """Whether this run has seen its settings file change: then it does nothing more that a change could affect."""
    run = _RUN.get()
    return run is not None and run.changed is not None


def _current() -> tuple[str, dict] | None:
    """The reading in force: a pinned unit's, or the run's; None outside both."""
    pinned_now = _PINNED.get()
    if pinned_now is not None:
        return pinned_now
    holder = _RUN.get()
    return _run_reading(holder) if holder is not None else None


def load() -> dict:
    """The private additions, or {} in Demo Mode or when there is no file. Inside a run or pinned(), its reading. Every
    failure while locating or loading the file (a path check refused, an unreadable or broken file, nesting too deep) is
    the one error the callers handle, ValueError("private_sites_invalid: ..."), naming its kind, never quoting the file
    (CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-02, D126: a PermissionError from Path.exists() and a RecursionError escaped
    the preflights). The reading in force (_current) fails only as private_sites_invalid."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():  # Demo Mode never reads the vault
        return {}
    current = _current()
    if current is not None:
        return current[1]
    try:
        path = _path()
        return _load(str(path)) if path is not None and path.exists() else {}
    except Exception as exc:  # noqa: BLE001 - every failure here is the one handled configuration error
        if isinstance(exc, ValueError) and str(exc).startswith("private_sites_invalid"):
            raise
        raise ValueError(f"private_sites_invalid: the file cannot be located or read ({type(exc).__name__})") from None


def revision() -> str:
    """The current revision of the private settings, read from the file's content: its sha256, or ABSENT. Inside a run
    or pinned(), its reading's. Raises private_sites_invalid like load()."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():
        return ABSENT
    current = _current()
    return current[0] if current is not None else _now_reading()[0]


class SettingsChanged(ValueError):
    """The private settings changed during a run: nothing more of it is committed or sent (CR-AD-V2-07, D141)."""


def _verify(expected: str) -> None:
    """SettingsChanged unless the file's content, read now, is still the reading `expected`."""
    try:
        now = _now_reading()[0]
    except ValueError as exc:   # changed into a file that cannot be read: still a change, never a crash (I3-03, S2-03)
        raise SettingsChanged(f"private_sites_changed: the private settings file changed during this run and cannot be "
                              f"read now ({str(exc).split(': ', 1)[-1]}); nothing more of the run was committed or sent, "
                              "and the next run reads it") from None
    if now != expected:
        raise SettingsChanged("private_sites_changed: the private settings file changed during this run; nothing more of "
                              "the run was committed or sent, and the next run reads it")


def _verify_run(expected: str) -> None:
    """_verify, and inside a run the change is kept: every later check of the run refuses too, even once the file holds
    the run's starting bytes again (Codex Pass 4)."""
    run = _RUN.get()
    if run is not None and run.changed is not None:
        raise SettingsChanged(run.changed)
    try:
        _verify(expected)
    except SettingsChanged as exc:
        if run is not None:
            run.changed = str(exc)
        raise


def check() -> None:
    """Inside a run or pinned(): the file's content is read now and, if it is no longer the reading in force (changed,
    removed or unreadable), SettingsChanged ("private_sites_changed") is raised, so the caller commits or sends nothing
    more; inside a run, once a change was seen, every later check refuses too. Called before every commit of alert
    intake, every automatic match or probe, every write of the seen-jobs index or the analytics export, and every
    publication or reply. Outside both, there is no reading to compare."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():
        return
    current = _current()
    if current is not None:
        _verify_run(current[0])


def refresh() -> None:
    """Outside a run: read the file's content again now, so the next reads see any change made since. Inside one: the
    run keeps its reading, and a change raises SettingsChanged (check())."""
    if _current() is None:
        revision()
    else:
        check()


@contextmanager
def run():
    """One reading of the private settings for a whole run (a command, a pull, an import, an export, a publication):
    edits take effect at the next run, never part-way through one (D141). On Windows the file is held for the run so
    that no edit can land meanwhile: an editor saving it then is refused and saves again after (D147); a missing file is
    made empty and held too, so none can be created meanwhile either (D153 R1). Nested use keeps the outer run."""
    if _RUN.get() is not None or _PINNED.get() is not None:
        yield
        return
    from careeros.demo.sandbox import enabled as demo_mode
    run = _Run()
    path = None if demo_mode() or not LOCK_RUNS else _path()
    if path is not None:
        try:
            run.handle = _hold_open(str(path))   # held before the first read: the reading is what the run keeps
        except ValueError as exc:
            run.reading = exc
    token = _RUN.set(run)
    try:
        yield
    finally:
        _RUN.reset(token)
        if run.handle is not None:
            _release(run.handle)


@contextmanager
def pinned():
    """One revision of the private settings for a unit of work (one email, one export). Every read inside it sees the
    same additions; when the unit ends, the file's content is read again and, if it changed, SettingsChanged is raised
    ("private_sites_changed"), so the caller commits nothing of the unit (no receipt, no move, no publish). Inside a run
    the unit uses the run's reading; outside one it pins the reading it starts with. Nested use keeps the outer one."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():   # Demo Mode never reads the vault
        yield ABSENT
        return
    if _PINNED.get() is not None:
        yield _PINNED.get()[0]
        return
    holder = _RUN.get()
    current = _run_reading(holder) if holder is not None else _now_reading()
    token = _PINNED.set(current)
    try:
        yield current[0]
    finally:
        _PINNED.reset(token)
    _verify_run(current[0])


def never_fetch() -> tuple[str, ...]:
    return tuple(load().get("never_fetch", ()))


def manual_systems() -> tuple[tuple[str, str], ...]:
    return tuple(load().get("manual_systems", ()))


def site_statuses() -> dict[str, str]:
    return dict(load().get("sources", {}))


def staffing_agencies() -> list[str]:
    return list(load().get("staffing_agencies", []))


def agency_shortcut() -> str | None:
    return load().get("agency_shortcut")


def agency_digests() -> tuple[AgencyDigest, ...]:
    return tuple(load().get("agency_digests", ()))
