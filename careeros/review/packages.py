"""Application packages (P2, 2026-10-21; ADR 0009): everything prepared for one posting, versioned and bound by hash.

A package holds:
- the application brief (your approved profile, approved facts quoted word for word, honest gaps);
- the posting's questions matched to your reusable answers (careeros/review/answers.py), and the ones still open;
- the documents, by SHA-256;
- the destination.

Its manifest (canonical JSON, with no timestamps) is hashed. The same inputs give the same version; any change to an
answer, fact, document, question or destination gives a new one.

Routes:
- `apply`: you labelled it Apply.
- `stretch`: you labelled it Stretch.
- `standing`: only a package prepared before 2026-11-18 under the standing preparation policy, which is now off: no
  job is prepared, and no assistance opens, without your Apply or Stretch label (the owner's D183, after Codex
  CR-2026-11-15-GATE-FIXES Pass 13). Such a package is withdrawn, never resumed.

Skip, a dismissal or an application already made always win.

A Stretch package (or every package, with `preparation.approval_required: all`) needs your approval of that exact
version (its manifest hash). Several can be approved together as a named batch; a package prepared later is not part
of the batch. Nothing here submits anything: an assistance session (careeros/review/assist.py) is how a package reaches
a form, and you click Submit.
"""
from __future__ import annotations

import json
import uuid
import weakref
from pathlib import Path
from urllib.parse import urlparse

from careeros.core.fingerprint import fingerprint, text_fingerprint
from careeros.db.store import Store

from . import answers as answer_lib
from .dismiss import is_dismissed
from .shortlist import _latest_label, brief_lines

ROUTES = ("apply", "stretch", "standing")
SCHEMA = 1
STANDING_DEFAULTS = {"enabled": False, "approval_required": "stretch", "min_eligibility": 80.0, "min_skill_coverage": 0.70,
                     "excluded_gaps": ["medium_gap", "large_gap", "severe_gap"], "allow_degree_stretch": False}


class PackageRefused(ValueError):
    pass


def settings(policy) -> dict:
    data = dict(STANDING_DEFAULTS)
    data.update((policy.data.get("preparation") or {}) if policy is not None else {})
    if data["approval_required"] not in ("stretch", "all"):
        raise ValueError("preparation_invalid: approval_required must be 'stretch' or 'all'")
    return data


def _score(store: Store, job_id: str):
    return store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def _posting_veto(store: Store, job_id: str) -> str | None:
    """Why the posting itself stops preparation, or None: it closed (or is a duplicate), or the gate's latest decision
    isn't eligible. Every session would be refused there by `submission.authorize` (posting_not_open, not_eligible), so
    a package would only look ready, whatever your label or approval (2026-10-27: a closed posting and a role the gate
    now rejects as hybrid both still listed as needing approval). A posting the gate never judged is left to
    `submission.authorize`; one that reopens or is judged eligible again has its route back."""
    job = store.one("SELECT current_state, closed_at FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    if job["current_state"] in ("closed", "duplicate") or job["closed_at"]:
        return f"posting_not_open: the posting is {job['current_state']}" + \
            (f" (closed {job['closed_at'][:10]})" if job["closed_at"] else "")
    gate = store.one("SELECT decision, hard_failures_json FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC "
                     "LIMIT 1", (job_id,))
    if gate and gate["decision"] != "eligible":
        failures = "; ".join(f"{f.get('code')}: {f.get('detail')}" for f in json.loads(gate["hard_failures_json"] or "[]"))
        return f"not_eligible: the gate's latest decision is {gate['decision']}" + (f" ({failures})" if failures else "")
    return None


def _application_veto(store: Store, job_id: str) -> str | None:
    """Why an application already made stops preparation, or None. Whatever the route, a role you applied to (the
    application in play or ended), may have applied to, or whose duplicate check can't finish is never prepared: every
    session would be refused there by `submission.authorize`, so a package would only look ready. Applying to a role
    again is your own decision, outside assistance: apply yourself and record it with `careeros applied --reapply`
    (independent Codex review of ac83e00, F7, and the second Claude review, 2026-10-22)."""
    from .applications import preflight
    for reason in preflight(store, job_id).reasons:
        if reason["level"] in ("block", "hold"):
            again = " Applying again is your decision: apply yourself and record it with careeros applied --reapply." \
                if reason["code"] == "applied_before" else ""
            return f"{reason['code']}: {reason['detail']}.{again}"
    return None


# The ingest failures this engine records carry this rule (pipeline._hold_failed_read): each holds until a later read
# of the posting is stored. One an older engine recorded, which never resolved its own (batch 4's hr1-upgrade-2), holds
# only while no read of the job was stored after it.
HOLD_RULE = "b5-2026-11-20"


# A failed read an older engine recorded on a generated id that names no link (a pasted, saved or file-added posting's
# "manual:<slug>:<fingerprint>") whose job was never created: which posting it was cannot be read, so it reaches every
# job, until a job with exactly that id is created (Z8, the coordinator's choice for BATCH_5 B1, built on the owner's OK)
EVERY_JOB = "every_job"


def every_job_message(at: str | None) -> str:
    return (f"a posting you added on {(at or '')[:10] or 'an earlier day'} (pasted, saved or added from a file) could not "
            "be stored, and CareerOS cannot tell which posting it was")


def identity_key(identity) -> str | None:
    """A posting identity (board_discovery.job_identity) as one string, for a hold's record."""
    return f"{identity[0]}|{identity[1]}" if identity else None


def failed_reads(store: Store, job_id: str, *, reach: bool = True) -> list:
    """The open holds recorded for the job, or for the posting it is (one recorded under another id when which job the
    posting is could not be read: the red team's hr1-routes-0), newest first: ingest failures and write-ahead holds
    (read_pending, the D190 design, section 7). With `reach`, also those that reach every job (Z8); without it, only
    the posting's own (posting_holds; Codex H39)."""
    from careeros.discovery.board_discovery import job_identity
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    key = identity_key(job_identity(job)) if job is not None else None
    return store.all("SELECT exception_id, entity_id, message, occurred_at, metadata_json FROM exceptions "
                     "WHERE entity_type = 'job' AND error_code IN ('ingest_failed', 'read_pending') AND status != 'resolved' "
                     "AND (entity_id = ? OR (? IS NOT NULL AND json_extract(metadata_json, '$.posting_identity') = ?) "
                     # Z8: a hold whose posting cannot be read reaches every job while no job has its id
                     "OR (? AND json_extract(metadata_json, '$.reaches') = ? AND NOT EXISTS "
                     "(SELECT 1 FROM jobs WHERE jobs.job_id = exceptions.entity_id))) "
                     "ORDER BY occurred_at DESC, rowid DESC", (job_id, key, key, int(reach), EVERY_JOB))


def posting_holds(store: Store, job_id: str, snapshot: tuple | None = None) -> list[dict]:
    """The open holds of the job's own posting, by a positive match only: a row recorded for the job's id or for its
    posting identity, a line of the pending-holds file for either. Never a hold that reaches the job only because its
    own posting cannot be read (Z8's every_job): a board read of this posting says nothing of that one. What a board
    read of the posting may clear (pipeline._stored_again; Codex H39); open_holds is what holds a label."""
    return open_holds(store, job_id, snapshot, reach=False)


def open_holds(store: Store, job_id: str, snapshot: tuple | None = None, *, reach: bool = True) -> list[dict]:
    """Every open hold that reaches the job (the D190 design, section 7): an ingest failure recorded for it or for the
    posting it is, and a hold kept beside the database because even its record could not be written (hr1-routes-0),
    each by its hold_id, with what it says. Kept beside the database first, then recorded. A hold lifts only by a
    board read of the posting or, for a write-ahead hold, its own batch's commit (section 7): an older engine's failure
    is no longer skipped once a later read of the job is stored (Codex H22; batch 4's hr1-upgrade-2 rule retired),
    and a line of the pending-holds file cleared by a committed transaction (hold_clearances) no longer counts.
    `snapshot`: the decision's own reading of the file (pending_snapshot; Codex H40), else pending_snapshot's now: the
    read transaction's own reading inside one (Codex H44), else the file as it is.
    `reach=False`: the posting's own holds only, never one that reaches every job (posting_holds; Codex H39)."""
    from careeros.discovery.board_discovery import job_identity
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    key = identity_key(job_identity(job)) if job is not None else None
    # a line a committed clearance cleared, or whose hold a row already carries, is settled: the row stands for it (a
    # reading made before the row's commit, paired with a snapshot after it, sees both: one hold; Codex H44)
    cleared = settled_lines(store)
    holds = [{"hold_id": hold_id_of(hold), "at": str(hold.get("at") or ""), "kept": "file",
              "message": str(hold.get("message") or "ingest failed")}
             for hold in (snapshot if snapshot is not None else pending_snapshot(store))[0]
             if (hold.get("job_id") == job_id or (key is not None and hold.get("identity") == key))
             and hold_id_of(hold) not in cleared]
    for row in failed_reads(store, job_id, reach=reach):
        message = row["message"]
        if row["entity_id"] != job_id and _reaches_every_job(row):   # Z8: said as what it is, on every card
            message = every_job_message(row["occurred_at"])
        holds.append({"hold_id": row_hold_id(row), "at": row["occurred_at"], "kept": "row", "message": message})
    return holds


def _reaches_every_job(row) -> bool:
    try:
        return (json.loads(row["metadata_json"] or "{}") or {}).get("reaches") == EVERY_JOB
    except ValueError:
        return False


def every_job_holds(store: Store) -> list:
    """The open holds that reach every job (Z8): for the upgrade report and careeros status."""
    return store.all("SELECT exception_id, entity_id, occurred_at FROM exceptions WHERE error_code = 'ingest_failed' "
                     "AND status != 'resolved' AND json_extract(metadata_json, '$.reaches') = ? AND NOT EXISTS "
                     "(SELECT 1 FROM jobs WHERE jobs.job_id = exceptions.entity_id) ORDER BY occurred_at", (EVERY_JOB,))


def cleared_holds(store: Store) -> set[str]:
    """The hold_ids of file lines a committed transaction cleared (hold_clearances, migration 017)."""
    return {r["hold_id"] for r in store.all("SELECT hold_id FROM hold_clearances")}


def settled_lines(store: Store) -> set[str]:
    """The hold_ids of file lines that no longer count as lines: cleared by a committed transaction, or recorded as a row
    (open or resolved: the row is the hold from then on). prune_pending_holds removes these lines; open_holds skips them."""
    return cleared_holds(store) | {r["hold_id"] for r in store.all(
        "SELECT json_extract(metadata_json, '$.hold_id') AS hold_id FROM exceptions WHERE error_code IN "
        "('ingest_failed', 'read_pending') AND json_extract(metadata_json, '$.hold_id') IS NOT NULL")}


def clear_hold(store: Store, job_id: str, hold_id: str, why: str) -> None:
    """Clear one hold by its id, in the caller's transaction: a row is resolved; a line of the pending-holds file is
    recorded as cleared (hold_clearances), and the line is pruned once that transaction commits (prune_pending_holds),
    so a rollback leaves it in place (the D190 design, section 7)."""
    row = store.one("SELECT exception_id FROM exceptions WHERE error_code IN ('ingest_failed', 'read_pending') "
                    "AND status != 'resolved' AND (exception_id = ? OR json_extract(metadata_json, '$.hold_id') = ?)",
                    (hold_id, hold_id))
    if row is not None:
        store.writer("discovery").resolve_exception(row["exception_id"], why)
    elif any(hold_id_of(h) == hold_id for h in pending_holds(store)) \
            and store.one("SELECT 1 FROM hold_clearances WHERE hold_id = ?", (hold_id,)) is None:
        store.writer("discovery").insert("hold_clearances", {"hold_id": hold_id, "job_id": job_id,
                                                           "cleared_at": store.clock.now()})


def prune_pending_holds(store: Store) -> int:
    """Drop the lines of the pending-holds file that committed transactions cleared or recorded as rows: after a
    batch's outermost commit, and at each command's start. Never inside an open transaction (a rollback would not
    restore the file). Returns how many lines it dropped."""
    if store.conn.in_transaction:
        return 0
    holds, damaged, unreadable = _read_pending(store)
    if not holds or unreadable:
        return 0
    done = settled_lines(store)
    kept = [h for h in holds if hold_id_of(h) not in done]
    if len(kept) != len(holds):
        _write_pending(store, kept, damaged)
    return len(holds) - len(kept)


def store_failed(store: Store, job_id: str) -> str | None:
    """Why the job's latest read could not be stored, or None: the first of its open holds (open_holds), which a later
    write of the job that succeeds resolves (pipeline.ingest; the red team's gr1-faults-0)."""
    for hold in open_holds(store, job_id):
        return f"{hold['message']} ({hold['at'][:16]} UTC" + (", not yet recorded)" if hold["kept"] == "file" else ")")
    return None


# holds kept beside the database when the database refused them (pipeline._hold_failed_read), until the next command's
# start records them (record_pending_holds): a file next to the database file, or this process's memory for a store
# held in memory
_PENDING: weakref.WeakKeyDictionary[Store, list] = weakref.WeakKeyDictionary()


def _pending_file(store: Store) -> Path | None:
    return None if store.path == ":memory:" else Path(store.path + ".pending-holds.jsonl")


def _read_pending(store: Store) -> tuple[list[dict], list[str], str | None]:
    """(the holds kept beside the database, the file's lines that are not holds, why the file cannot be read or None).
    A line that is not a hold (a write cut short) is kept as it is, never dropped (BATCH_5 A2). Only a file confirmed
    missing holds nothing: any other error reading it makes it unreadable (Codex H40)."""
    path = _pending_file(store)
    if path is None:
        return list(_PENDING.get(store, [])), [], None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as exc:
        return [], [], f"{type(exc).__name__}: {exc}"[:200]
    holds, damaged = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            hold = json.loads(line)
        except ValueError:
            hold = None
        if isinstance(hold, dict) and hold.get("job_id"):
            holds.append(hold)
        else:
            damaged.append(line)
    return holds, damaged, None


def pending_holds(store: Store) -> list[dict]:
    """The holds kept beside the database that can be read (pending_file_problem says when that is not all of them)."""
    return _read_pending(store)[0]


def pending_snapshot(store: Store) -> tuple[list[dict], list[str], str | None]:
    """One reading of the holds kept beside the database: (holds, damaged lines, why it cannot be read or None). A
    decision (a capture, P1) takes one and gives it to both pending_file_problem and open_holds, so the file's health and
    which holds it keeps are read from the same reading; a change or an error between two readings can never split
    them (Codex H40). Inside a read transaction it is the reading made before the transaction's snapshot started
    (Store.read; Codex H44): a hold a command's start moves from the file into a row meanwhile is in one view or the
    other. Inside a write transaction, or none, the file is read now."""
    taken = store.beside("pending_holds")
    return taken if taken is not None else _read_pending(store)


def pending_file_problem(store: Store, snapshot: tuple | None = None) -> str | None:
    """Why the holds kept beside the database cannot all be read now, or None. While there is one, every Apply and
    Stretch label is held (p1_held) and no preview is offered (labels.capture): the file fails closed (BATCH_5 A1, A2).
    `snapshot`: the decision's own reading (pending_snapshot), else pending_snapshot's now: the read transaction's own
    reading inside one (Codex H44), else the file as it is."""
    path = _pending_file(store)
    _, damaged, unreadable = snapshot if snapshot is not None else pending_snapshot(store)
    if unreadable:
        return (f"holds_file_unreadable: the holds kept beside the database ({path}) cannot be read ({unreadable}); "
                "close any program that has it open, and the next command records its holds")
    if damaged:
        return (f"holds_file_damaged: {len(damaged)} line(s) of {path} are not holds (a write cut short): check each "
                "one, then delete it from the file; the next command records the rest")
    return None


def hold_id_of(hold: dict) -> str:
    """A hold's id (the D190 design, section 7): a row's exception_id, or the id a line of the pending-holds file
    carries, which the row it becomes keeps. A line written without one (before 017) is named by its own content."""
    if hold.get("hold_id"):
        return str(hold["hold_id"])
    return "hold:line:" + text_fingerprint(json.dumps(hold, sort_keys=True))[:24]


def row_hold_id(row) -> str:
    """The hold_id of an ingest failure row: the id its line carried when it was kept beside the database first, else
    its own exception_id (stable: only its status and resolution change afterwards)."""
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        meta = {}
    return str(meta.get("hold_id") or row["exception_id"])


def add_pending_hold(store: Store, job_id: str, identity, message: str) -> str:
    """Keep a hold the database refused beside it. Raises when that fails too: the command stops, never silent. Each
    line carries a random hold_id: two holds never share one, even under a fixed clock (section 7)."""
    hold = {"job_id": job_id, "identity": identity_key(identity), "message": message[:300], "at": store.clock.now(),
            "hold_id": f"hold:{uuid.uuid4().hex}"}
    path = _pending_file(store)
    if path is None:
        _PENDING.setdefault(store, []).append(hold)
        return str(hold["hold_id"])
    # an append never joins an earlier line: a line cut short (no newline) gets one first (BATCH_5 A2)
    lead = "" if _ends_a_line(path) else "\n"
    with path.open("a", encoding="utf-8") as out:
        out.write(lead + json.dumps(hold, sort_keys=True) + "\n")
    return str(hold["hold_id"])


def _ends_a_line(path: Path) -> bool:
    """Whether the file is empty, missing or ends with a newline (when it cannot be read, assume it does not)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                return True
            handle.seek(-1, 2)
            return handle.read(1) == b"\n"
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _write_pending(store: Store, holds: list[dict], damaged: list[str] | tuple = ()) -> None:
    """Rewrite the pending-holds file with these holds, keeping every line that is not a hold as it was: replaced whole,
    never truncated in place (Codex H41)."""
    path = _pending_file(store)
    if path is None:
        _PENDING[store] = list(holds)
        return
    if not holds and not damaged:
        path.unlink(missing_ok=True)
        return
    _replace_whole(path, "".join(line + "\n" for line in damaged) + "".join(json.dumps(h, sort_keys=True) + "\n"
                                                                            for h in holds))


# the name a replacement of the pending-holds file is written under before it replaces the file: the reader reads only
# the file itself, so a replacement left behind by a stop changes nothing (Codex H41)
REPLACEMENT = ".replacement-"


def _replace_whole(path: Path, text: str) -> None:
    """Write `text` over `path` whole or not at all (Codex H41): the complete replacement is written to a new file in the
    same folder, flushed and synced to disk, then put in the file's place in one step (os.replace). On any failure (a
    full disk, a stop, a Windows sharing error at the replace) the file stays as it was, every hold and every damaged
    line in it, and the replacement is removed; a hold whose row landed meanwhile is then recorded once at the next
    command's start (record_pending_holds)."""
    import os
    temp = path.with_name(f"{path.name}{REPLACEMENT}{uuid.uuid4().hex}")
    try:
        with open(temp, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass   # a replacement that cannot be removed is ignored: the reader reads only the file itself
        raise


def record_pending_holds(store: Store) -> int:
    """At a command's start: every hold kept beside the database is recorded in it, then removed from beside it."""
    prune_pending_holds(store)   # lines cleared, or recorded, by transactions that committed
    holds, damaged, unreadable = _read_pending(store)
    if not holds or unreadable:
        return 0   # an unreadable file stays as it is: every label waits on it (pending_file_problem)
    disc = store.writer("discovery")
    recorded = 0
    with store.tx():
        for hold in holds:
            hold_id = hold_id_of(hold)
            if store.one("SELECT 1 FROM exceptions WHERE error_code = 'ingest_failed' "
                         "AND json_extract(metadata_json, '$.hold_id') = ?", (hold_id,)):
                continue   # its row landed while the file's rewrite failed: one row, never two (section 7)
            disc.exception("data_quality", "medium", str(hold.get("message") or "ingest failed")[:500], entity_type="job",
                           entity_id=hold["job_id"],
                           error_code="read_pending" if str(hold.get("message", "")).startswith("read_pending")
                           else "ingest_failed",
                           metadata={"posting_identity": hold.get("identity"), "rule": HOLD_RULE,
                                     "kept_beside_the_database_at": hold.get("at"), "hold_id": hold_id})
            recorded += 1
    _write_pending(store, [], damaged)   # a line that is not a hold stays, and holds every label, until you remove it
    return recorded


# the states a screen leaves a job in: what route_for then reads follows the usual rules (a job in needs_review routes on
# your label, as before: the owner's D166 Q3); only a reopened job no screen has read since is refused
SCREENED = ("needs_review", "eligible", "scored", "rejected")


def _reopened_unscreened(store: Store, job_id: str) -> bool:
    """Whether the job reopened (closed -> normalized) after a screen last left it in a screened state (SCREENED)."""
    job = store.one("SELECT workflow_id, current_state FROM jobs WHERE job_id = ?", (job_id,))
    if job is None or job["current_state"] in SCREENED:
        return False
    marks = ",".join("?" * len(SCREENED))
    row = store.one(f"SELECT COALESCE(MAX(sequence), 0) AS s FROM transitions WHERE workflow_id = ? "
                    f"AND to_state IN ({marks})", (job["workflow_id"], *SCREENED))
    return store.one("SELECT 1 FROM transitions WHERE workflow_id = ? AND from_state = 'closed' AND sequence > ? LIMIT 1",
                     (job["workflow_id"], row["s"] if row is not None else 0)) is not None


def route_for(store: Store, policy, job_id: str, profile=None) -> tuple[str | None, str]:
    """(route, basis) a package may be prepared under now, or (None, why not): P1, the only safety decision (the D190
    design, section 2). Your Skip, a dismissal (while it is the latest event: a reinstatement undoes it, 2026-11-01), a
    posting that closed or that the gate no longer finds eligible, and an application already made always win, and a
    standing pick never becomes a label. Read in one transaction: the caller's, else its own (Store.read)."""
    with store.read():
        return _route_for(store, policy, job_id, profile)


def _route_for(store: Store, policy, job_id: str, profile) -> tuple[str | None, str]:
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        raise ValueError("unknown_job")
    if is_dismissed(store, job_id):
        return None, "you dismissed it"
    label = _latest_label(store, job_id)
    if label and label["label"] == "skip":
        return None, "you labelled it Skip"
    veto = _posting_veto(store, job_id) or _application_veto(store, job_id)
    if veto:
        return None, veto
    if _reopened_unscreened(store, job_id):
        # closed, then read open again, and not screened since: the gate's decision from before the closure is not this
        # posting's now; the next processing run screens it again, its location and its being open included (the
        # coordinator's batch 3 condition 5). A posting that only changed keeps your label's route until then (the
        # owner's D166 Q3), its label checked against its opening (B.1) and assistance against its eligibility
        return None, "posting_not_screened: it reopened and waits for the next processing run to screen it again"
    from careeros.pipeline import same_posting_twins
    twins = same_posting_twins(store, job_id)
    if twins:
        # one posting stored as two jobs (an older engine's, or held apart because your decisions on them differ):
        # nothing is prepared for either until they are one (the coordinator's batch 3 condition 4)
        return None, (f"same_posting_twins: {job_id} and {', '.join(twins)} are one posting by its exact link; give them "
                      "the same label (or dismiss the same way) and the next processing run makes them one job")
    if label and label["label"] in ("apply", "stretch"):
        from .labels import label_confirmed, label_opening, opening_change, opening_of, p1_held
        if not label_confirmed(store, label):
            return None, (f"label_unconfirmed: your {label['label']} label was not given from a preview of what it "
                          "approves (given before the upgrade of 2026-11-24, or before another CareerOS engine last "
                          "wrote this database: a rollback); confirm it by labelling the job again on the review page "
                          f"(careeros label JOB {label['label']}) (the owner's D188; the D190 design)")
        moved = opening_change(label_opening(store, label) or {}, opening_of(store, job_id))
        if moved:
            # a label stands for the opening it was given for, compared at every use whatever wrote the change and
            # through any number of steps (the red team before Codex Pass 15, RT-04 and RT-05; batch 3, B.1)
            return None, (f"label_suspended: the posting's identity changed after your {label['label']} label ({moved}); "
                          "check it is the opening you meant, then label it again")
        held = p1_held(store, policy, profile, job_id, label)
        if held:
            # P1 (the D190 design, section 3): the job is no longer what the label's preview showed, or a read of it
            # failed that the preview did not show (the red team's gr1-faults-0, B.5; Codex H25)
            return None, held
        return label["label"], f"your {label['label']} label" + (f" ({label['reason']})" if label["reason"] else "")
    # standing preparation is off (the owner's D183: Codex Passes 12 and 13 each found new routes into it): a package is
    # prepared, and assistance opens, only for a job you labelled Apply or Stretch
    return None, "not pursued: label it Apply or Stretch (standing preparation is off)"


def _documents(library: dict, vault_root: Path) -> tuple[list[dict], list[str]]:
    docs, gaps = [], []
    for kind in answer_lib.DOCUMENT_KINDS:
        rel = library["documents"].get(kind)
        if not rel:
            if kind == "resume":
                gaps.append("no resume named in answers.yaml (documents.resume)")
            continue
        path = vault_root / rel
        if not path.is_file():
            gaps.append(f"{kind} not found in the vault: {rel}")
            continue
        from careeros.core.fingerprint import file_fingerprint
        docs.append({"kind": kind, "path": rel, "sha256": file_fingerprint(path)})
    return docs, gaps


def _question_rows(questions: list[dict] | None, library: dict, documents: list[dict], today: str) -> tuple[list[dict], set]:
    """Each known question with your answer, the document that answers it, or why it's open. With the questions not
    read yet, every answer you could copy (current, and not held back as sensitive) is used, so each is part of the
    version (independent Codex review of ac83e00, 2026-10-22). A form question is read with its choices, which are
    kept in the row so the handoff can read them again."""
    if questions is None:
        return [], {a["id"] for a in library["answers"] if answer_lib.usable(a, today)}
    rows, used = [], set()
    docs = {d["kind"] for d in documents}
    for q in questions:
        key, label = str(q.get("key") or ""), str(q.get("label") or "")
        options = [str(o) for o in q.get("options") or []]
        row = {"key": key, "name": str(q.get("name") or ""), "label": label, "type": q.get("type") or "text",
               "required": bool(q.get("required")), **({"options": options} if options else {})}
        if key in answer_lib.DOCUMENT_KINDS:
            row["status"] = "document" if key in docs else ("missing" if row["required"] else "optional")
        else:
            found = answer_lib.find_field(key, library, today, label, options) if key in answer_lib.STANDARD_FIELDS else \
                answer_lib.find(label, library, today, options)
            row["status"], row["via"] = found["status"], found["via"]
            if found["blocked"]:
                row["blocked"] = found["blocked"]
            if found["answer"] is not None and found["status"] == "matched":
                row["answer_id"] = found["answer"]["id"]
                row["answer_sha256"] = text_fingerprint(found["answer"]["answer"])
                if options and found["answer"]["answer"] not in options:
                    row["status"] = "option_mismatch"   # your answer isn't one of the form's choices: review it
                used.add(found["answer"]["id"])
        rows.append(row)
    return rows, used


def build_manifest(store: Store, policy, profile, job_id: str, *, route: str, basis: str, facts: list, library: dict,
                   vault_root: Path, questions: list[dict] | None, today: str) -> tuple[dict, list[str], list[dict]]:
    """(manifest, brief lines, question rows) for a package, from what is true now. No timestamps: the same inputs
    give the same manifest hash."""
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    why = {"apply": "- Prepared because of " + basis, "stretch": "- Prepared because of " + basis +
           ". A Stretch package needs your approval before it is used.",
           "standing": "- Prepared by the standing preparation policy: there is no label of yours on this job."}[route]
    lines, issues = brief_lines(store, profile, job_id, policy, facts, why)
    documents, doc_gaps = _documents(library, vault_root)
    rows, used = _question_rows(questions, library, documents, today)
    gap_rows = [r for r in rows if r.get("status") not in ("matched", "document", "optional")]
    url = job["apply_url"] or job["url"] or ""
    answers_used = [a for a in library["answers"] if a["id"] in used]
    fact_ids = {f["id"] for f in facts}
    manifest = {
        "schema": SCHEMA, "route": route, "basis": basis,
        "job": {"job_id": job_id, "title": job["title"], "employer": job["employer_name"],
                "requisition_id": job["requisition_id"], "canonical_url": job["canonical_url"],
                "content_fingerprint": job["content_fingerprint"]},
        "destination": {"url": url, "host": (urlparse(url).hostname or "").lower()},
        "documents": documents,
        "questions": rows if questions is not None else None,
        # each answer by its text AND the question it was written for, so an edit to either is a new version (second
        # Claude review, 2026-10-22: the panel shows the question next to the answer)
        "answers": [{"id": a["id"], "kind": a["kind"], "approved": a["approved"], "expires": a["expires"],
                     "sha256": text_fingerprint(a["answer"]), "question_sha256": text_fingerprint(a["question"])}
                    for a in answers_used],
        # with the questions not read yet, the panel also lists the answers it holds back and why
        "withheld": [{"id": a["id"], "question_sha256": text_fingerprint(a["question"]),
                      "reason": answer_lib.withheld(a, today)} for a in library["answers"]
                     if questions is None and answer_lib.withheld(a, today)],
        "facts": sorted(({"id": f["id"], "sha256": text_fingerprint(f["text"])} for f in facts), key=lambda f: f["id"]),
        "facts_cited_by_answers": sorted({fid for a in answers_used for fid in a["facts"]}),
        "unknown_facts_cited": sorted({fid for a in answers_used for fid in a["facts"]} - fact_ids),
        "gaps": doc_gaps + [f"question: {r['label'] or r['key']} ({r['status'].replace('_', ' ')}"
                            + (f": {r['blocked']}" if r.get("blocked") else "") + ")" for r in gap_rows],
        "brief_sha256": text_fingerprint("\n".join(lines)),
        "wording_issues": len(issues),
    }
    return manifest, lines, rows


def prepared_answer(by_id: dict, answer_id: str, sha256: str, question_sha256: str | None = None) -> dict:
    """The answer a package was prepared with: its id AND its text hash (and, where the package recorded it, the hash
    of the question it was written for) must match, or nothing is shown or handed over (independent Codex review of
    ac83e00, 2026-10-22: an answer must never stand in for another after matching)."""
    entry = by_id.get(answer_id)
    if entry is None or text_fingerprint(entry["answer"]) != sha256 \
            or (question_sha256 is not None and text_fingerprint(entry["question"]) != question_sha256):
        raise ValueError(f"answer_changed: {answer_id} is not the answer this package was prepared with: prepare it again")
    held = answer_lib.blocked(entry)
    if held:   # the same answer, held back now (the checks were widened, or it was mislabelled)
        raise ValueError(f"answer_held_back: {answer_id} is held back now ({held}): prepare the package again")
    return entry


def _answer_panel(manifest: dict, rows: list[dict], library: dict) -> list[str]:
    """The manual fallback: every value to copy, question by question, and what you still answer yourself."""
    by_id = {a["id"]: a for a in library["answers"]}
    lines = [f"# Answers: {manifest['job']['title']} at {manifest['job']['employer']}", "",
             f"- Destination: {manifest['destination']['url'] or 'not recorded'}",
             "- Copy each value into the employer's form, review the whole form, then submit it yourself.", ""]
    for doc in manifest["documents"]:
        lines.append(f"- Attach your {doc['kind'].replace('_', ' ')}: {doc['path']} (SHA-256 {doc['sha256'][:12]})")
    if manifest["questions"] is None:
        lines += ["", "## Your current answers (this posting's questions are not known yet)", ""]
        for used in manifest["answers"]:
            entry = prepared_answer(by_id, used["id"], used["sha256"], used["question_sha256"])
            lines.append(f"- {entry['question']}\n  {entry['answer']}")
        if manifest["withheld"]:
            lines += ["", "## Not offered", ""]
            lines += [f"- {by_id[w['id']]['question'] if w['id'] in by_id else w['id']}: {w['reason']}"
                      for w in manifest["withheld"]]
    else:
        lines += ["", "## Questions", ""]
        for r in rows:
            label = r["label"] or r["key"]
            if r.get("status") == "matched":
                lines.append(f"- {label}\n  {prepared_answer(by_id, r['answer_id'], r['answer_sha256'])['answer']}")
            elif r.get("status") == "document":
                lines.append(f"- {label}: attach the file above")
            else:
                lines.append(f"- {label}: YOU ANSWER THIS ({r['status'].replace('_', ' ')}"
                             + (f": {r['blocked']}" if r.get("blocked") else "") + ")")
    # an instruction, not an assurance: wording varies, and the checks hold back what they recognise
    lines += ["", "- Consent, attestation, demographic, health, disability, military-service, signature, background, "
                  "criminal, credit and drug-check, identity-document and pay-history questions are always yours to "
                  "answer on the form, whatever this panel shows."]
    if manifest["gaps"]:
        lines += ["", "## Open items", ""] + [f"- {g}" for g in manifest["gaps"]]
    return lines


def job_folder(job_id: str) -> str:
    """A job's folder name under packages/: its id with ":" as "_". An id that is not a safe folder name as it stands (a
    character Windows reads differently, a trailing dot or space, which Windows drops) gets a short suffix from the id,
    so two jobs never share a folder (the red team before Codex Pass 15, RT-12: "R-4401." and "R-4401" did)."""
    import hashlib
    import re
    base = job_id.replace(":", "_")
    safe = re.sub(r"[^A-Za-z0-9_.~-]", "_", base).rstrip(". ")
    return base if safe == base else f"{safe}~{hashlib.sha256(job_id.encode()).hexdigest()[:8]}"


def questions_path(vault_root: Path, job_id: str) -> Path:
    return vault_root / "packages" / job_folder(job_id) / "questions.json"


def load_questions(vault_root: Path, job_id: str) -> list[dict] | None:
    """The posting's form questions as last read (careeros package questions), or None when never read."""
    path = questions_path(vault_root, job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = data["questions"]
        if not isinstance(questions, list):
            raise ValueError("no list of questions")
        if data.get("job_id") not in (None, job_id):
            # another job's file in this folder (an older engine's "R-4401." beside "R-4401", which Windows reads as one
            # folder: the red team's fr1-paste-1): never read as this job's
            raise ValueError("another job's questions")
    except (ValueError, KeyError, TypeError) as exc:
        # named for its own job, never a bare JSON error that ends every package command (the red team before Codex
        # Pass 15, RT-40)
        raise ValueError(f"questions_unreadable: {job_id}'s saved form questions ({path.name} in its package folder) "
                         f"can't be read ({type(exc).__name__}); read them again with careeros package questions "
                         f"{job_id}, or remove that file to prepare without them") from None
    return questions


def save_questions(vault_root: Path, job_id: str, questions: list[dict], source: str, at: str) -> Path:
    path = questions_path(vault_root, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # written whole to a file beside it, then put in its place: an interrupted write leaves the earlier file (RT-40)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps({"job_id": job_id, "source": source, "read_at": at, "questions": questions}, indent=1) + "\n",
                       encoding="utf-8")
    partial.replace(path)
    return path


def latest(store: Store, job_id: str):
    return store.one("SELECT * FROM packages WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,))


def prepare(store: Store, policy, profile, job_id: str, *, facts: list, library: dict, vault_root: Path,
            questions: list[dict] | None = None, actor: str = "user") -> dict:
    """Prepare (or confirm unchanged) the package for a job you labelled Apply or Stretch. Returns
    {package_id, version, new, manifest_sha256, folder, gaps}. Refused for a job you skipped or dismissed, for one
    nobody chose, for a posting that closed or that the gate no longer finds eligible, and for a role with an
    application (open or ended), a possible one, or a duplicate check that can't finish."""
    writer = store.writer("assist")
    with store.tx():   # the route check, every read the package uses and its row: one transaction (Codex H13, H16)
        route, basis = route_for(store, policy, job_id, profile)
        if route is None:
            raise PackageRefused(f"not_prepared: {basis}")
        today = store.clock.now()[:10]
        manifest, brief, rows = build_manifest(store, policy, profile, job_id, route=route, basis=basis, facts=facts,
                                               library=library, vault_root=vault_root, questions=questions, today=today)
        if manifest["unknown_facts_cited"]:
            raise PackageRefused("answer_cites_unknown_fact: " + ", ".join(manifest["unknown_facts_cited"]))
        digest = fingerprint(manifest)
        rendered = render_files(manifest, brief, rows, library)
        sums = {name: text_fingerprint(body) for name, body in rendered.items()}
        previous = latest(store, job_id)
        if previous is not None and previous["manifest_sha256"] == digest:
            if files_ok(vault_root, previous):
                return {"package_id": previous["package_id"], "version": previous["version"], "new": False,
                        "manifest_sha256": digest, "folder": previous["folder"], "gaps": manifest["gaps"], "route": route}
            if _stored_files(previous) == sums:
                # repair (Codex M1): the version's files are missing or changed on disk, and rendering it again from
                # the current inputs gives exactly the files its row recorded: they are written again, after the commit
                repaired = previous
            else:
                repaired = None   # its inputs changed since: not repaired; a new current version below
            if repaired is not None:
                writer.event("package_repaired", outcome="success", entity_type="job", entity_id=job_id,
                             metadata={"package_id": previous["package_id"], "version": previous["version"]})
        else:
            repaired = None
        if repaired is None:
            new = _new_version(store, writer, job_id, previous, route, basis, digest, manifest, sums, actor)
    if repaired is not None:
        _write_files(vault_root / repaired["folder"], rendered)
        return {"package_id": repaired["package_id"], "version": repaired["version"], "new": False, "repaired": True,
                "manifest_sha256": digest, "folder": repaired["folder"], "gaps": manifest["gaps"], "route": route}
    # the files are written only after the row commits; an interruption leaves a row whose files fail files_json,
    # which readiness refuses and the next prepare repairs
    _write_files(vault_root / new["folder"], rendered)
    return {**new, "gaps": manifest["gaps"], "route": route}


def render_files(manifest: dict, brief: list[str], rows: list[dict], library: dict) -> dict[str, str]:
    """A package's three files, rendered in memory (manifest.json, brief.md, answers.md)."""
    return {"manifest.json": json.dumps(manifest, indent=1, sort_keys=True) + "\n",
            "brief.md": "\n".join(brief) + "\n",
            "answers.md": "\n".join(_answer_panel(manifest, rows, library)) + "\n"}


def _stored_files(package) -> dict | None:
    try:
        stored = json.loads(package["files_json"]) if package["files_json"] else None
    except ValueError:
        return None
    return stored if isinstance(stored, dict) else None


def files_ok(vault_root: Path, package) -> bool:
    """Whether every file of the package is on disk as its row recorded it (files_json, migration 017). A package
    prepared before 017 recorded none: never ready, prepared again (Codex M1)."""
    stored = _stored_files(package)
    if not stored:
        return False
    folder = Path(vault_root) / package["folder"]
    for name, digest in stored.items():
        try:
            if text_fingerprint((folder / name).read_bytes().decode("utf-8")) != digest:
                return False
        except (OSError, UnicodeDecodeError):
            return False
    return True


def _write_files(folder: Path, rendered: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in rendered.items():
        with (folder / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)


def _new_version(store: Store, writer, job_id: str, previous, route: str, basis: str, digest: str, manifest: dict,
                 sums: dict, actor: str) -> dict:
    """The next version's row, with each file's sha256 (files_json), in the caller's transaction."""
    version = (previous["version"] + 1) if previous is not None else 1
    rel = Path("packages") / job_folder(job_id) / f"v{version}"

    def windows(path: str) -> str:   # a folder as Windows reads it: no case, no trailing dot or space in any part
        return "/".join(part.rstrip(". ").lower() for part in path.split("/"))
    mine = windows((Path("packages") / job_folder(job_id)).as_posix())
    if any(windows(Path(r["folder"]).parent.as_posix()) == mine
           for r in store.all("SELECT folder FROM packages WHERE job_id != ?", (job_id,))):
        # another job's packages already live in this folder as Windows reads it (two ids one letter's case apart, or an
        # older engine's "R-4401." beside "R-4401": the red team's fr1-paste-1): this job's get a folder of their own,
        # never another's (RT-12)
        import hashlib
        rel = Path("packages") / f"{job_folder(job_id)}~{hashlib.sha256(job_id.encode()).hexdigest()[:8]}" / f"v{version}"
    # a version's files are written after its row commits; a folder an older engine left without a row (its files were
    # written before the row) is written over by the version that takes its number (the red team before Codex Pass 15,
    # RTC-03)
    package_id = f"pkg:{uuid.uuid4().hex[:16]}"
    writer.insert("packages", {"package_id": package_id, "job_id": job_id, "version": version, "route": route,
                               "basis": basis, "manifest_sha256": digest, "manifest_json": json.dumps(manifest, sort_keys=True),
                               "folder": rel.as_posix(), "at": store.clock.now(), "actor": actor,
                               "files_json": json.dumps(sums, sort_keys=True)})
    writer.event("package_prepared", outcome="success", entity_type="job", entity_id=job_id,
                 actor_type="human" if actor == "user" else "system", actor_id=actor,
                 metadata={"package_id": package_id, "version": version, "route": route, "manifest_sha256": digest,
                           "gaps": len(manifest["gaps"])})
    return {"package_id": package_id, "version": version, "new": True, "manifest_sha256": digest, "folder": rel.as_posix()}


def _approval(store: Store, package) -> dict | None:
    """Your approval of exactly this package version (its manifest hash), unless revoked."""
    rows = store.all("SELECT * FROM package_approvals WHERE package_id = ? ORDER BY at, rowid", (package["package_id"],))
    revoked = {r["revokes"] for r in rows if r["action"] == "revoke"}
    valid = [r for r in rows if r["action"] == "approve" and r["approval_id"] not in revoked
             and r["manifest_sha256"] == package["manifest_sha256"]]
    return dict(valid[-1]) if valid else None


def needs_approval(policy, route: str) -> bool:
    return route == "stretch" or settings(policy)["approval_required"] == "all"


def approve(store: Store, package_id: str, *, reason: str, key: str, batch: str | None = None) -> str:
    """Approve ONE package version for assistance. The approval names its manifest hash, so a later version is not
    covered. `batch` names a set approved together; it never adds packages prepared later."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    package = store.one("SELECT * FROM packages WHERE package_id = ?", (package_id,))
    if package is None:
        raise ValueError("unknown_package")
    newest = latest(store, package["job_id"])
    if newest["package_id"] != package_id:
        raise ValueError(f"superseded: version {newest['version']} is the current package for this job")
    writer = store.writer("assist")
    with store.tx():
        if writer.receipt("package_approve", key, {"package_id": package_id, "batch": batch or "", "reason": reason},
                          actor_id="user") == "replay":
            return "replay"
        approval_id = f"pap:{uuid.uuid4().hex[:16]}"
        writer.insert("package_approvals", {"approval_id": approval_id, "action": "approve", "revokes": None,
                                            "package_id": package_id, "manifest_sha256": package["manifest_sha256"],
                                            "batch": (batch or "").strip() or None, "reason": reason[:300],
                                            "at": store.clock.now(), "actor": "user"})
        writer.event("package_approved", outcome="success", entity_type="job", entity_id=package["job_id"],
                     actor_type="human", actor_id="user",
                     metadata={"package_id": package_id, "approval_id": approval_id, "batch": batch,
                               "manifest_sha256": package["manifest_sha256"]})
    return approval_id


def revoke(store: Store, approval_id: str, *, reason: str, key: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    row = store.one("SELECT * FROM package_approvals WHERE approval_id = ? AND action = 'approve'", (approval_id,))
    if row is None:
        raise ValueError("unknown_approval")
    writer = store.writer("assist")
    with store.tx():
        if writer.receipt("package_revoke", key, {"approval_id": approval_id, "reason": reason}, actor_id="user") == "replay":
            return "replay"
        revoke_id = f"pap:{uuid.uuid4().hex[:16]}"
        writer.insert("package_approvals", {"approval_id": revoke_id, "action": "revoke", "revokes": approval_id,
                                            "package_id": row["package_id"], "manifest_sha256": row["manifest_sha256"],
                                            "batch": row["batch"], "reason": reason[:300], "at": store.clock.now(),
                                            "actor": "user"})
        writer.event("package_approval_revoked", outcome="success", entity_type="package", entity_id=row["package_id"],
                     actor_type="human", actor_id="user", metadata={"approval_id": approval_id, "revoke_id": revoke_id})
    return revoke_id


def standing_candidates(store: Store, policy, profile=None) -> list[str]:
    """None: standing preparation is off (the owner's D183, after Codex CR-2026-11-15-GATE-FIXES Pass 13). No job is
    prepared without your Apply or Stretch label, whatever the policy file says."""
    return []


def common_gaps(store: Store) -> list[tuple[str, int, str | None]]:
    """Questions your current packages still leave open (missing, expired or not one of the form's choices), most
    frequent first, as (label, count, slot): answering one of these once in answers.yaml covers every package that asks
    it. The slot is the standard field (first_name, email...) or document (resume, cover_letter) the form's own key
    says the question is, filled from the file by `field:` or `documents:` whatever its label; None for any other. A
    label's line breaks and runs of spaces are evened out, so one question is one row on one line."""
    from collections import Counter
    counts: Counter = Counter()
    slots = answer_lib.STANDARD_FIELDS + answer_lib.DOCUMENT_KINDS
    rows = store.all("SELECT manifest_json FROM packages p WHERE version = (SELECT MAX(version) FROM packages q "
                     "WHERE q.job_id = p.job_id)")
    for r in rows:
        for q in json.loads(r["manifest_json"]).get("questions") or []:
            if q.get("status") in ("missing", "expired", "option_mismatch"):
                label = " ".join((q["label"] or "").split()) or q["key"]
                counts[(label, q["key"] if q["key"] in slots else None)] += 1
    return [(label, n, slot) for (label, slot), n in counts.most_common()]


def readiness(store: Store, policy, profile, job_id: str, *, facts: list, library: dict, vault_root: Path,
              questions: list[dict] | None = None, package: str | None = None) -> dict:
    """Is the job's current package (or the package `package` names) ready for assistance now? {state, package,
    reasons}. States: not_prepared, withdrawn (no route any more: P1 holds your label, you skipped or dismissed it since,
    the posting closed or the gate no longer finds it eligible, or the role has an application, open or ended, may have
    one, or its duplicate check can't finish), stale (something it was built from changed, or a later version exists:
    prepare it again), files_missing (a file is missing or not what its row recorded: package_files_missing, repaired by
    careeros package prepare), needs_approval, ready. The route check, the manifest and the files are read in one
    transaction: the caller's, else its own (the D190 design, section 3). Sessions are checked by
    careeros/review/assist.py."""
    with store.read():
        return _readiness(store, policy, profile, job_id, facts=facts, library=library, vault_root=vault_root,
                          questions=questions, package_id=package)


def _readiness(store: Store, policy, profile, job_id: str, *, facts: list, library: dict, vault_root: Path,
               questions: list[dict] | None, package_id: str | None) -> dict:
    current = latest(store, job_id)
    package = current if package_id is None else store.one("SELECT * FROM packages WHERE package_id = ? AND job_id = ?",
                                                         (package_id, job_id))
    if package is None:
        return {"state": "not_prepared", "package": None, "reasons": ["no package yet: careeros package prepare"]}
    route, basis = route_for(store, policy, job_id, profile)
    if route is None:
        return {"state": "withdrawn", "package": dict(package), "reasons": [basis]}
    manifest, _, _ = build_manifest(store, policy, profile, job_id, route=route, basis=basis, facts=facts, library=library,
                                    vault_root=vault_root, questions=questions, today=store.clock.now()[:10])
    if fingerprint(manifest) != package["manifest_sha256"]:
        return {"state": "stale", "package": dict(package),
                "reasons": ["what this package was built from changed (an answer, fact, document, question, the route or the "
                            "posting): prepare it again"]}
    if current is None or current["package_id"] != package["package_id"]:
        return {"state": "stale", "package": dict(package),
                "reasons": [f"version {package['version']} is no longer the latest: prepare it again"]}
    if not files_ok(vault_root, package):
        # never ready without its files as its row recorded them (Codex M1)
        return {"state": "files_missing", "package": dict(package),
                "reasons": [f"package_files_missing: version {package['version']}'s files are missing or changed; "
                            "careeros package prepare writes them again"]}
    if needs_approval(policy, package["route"]) and _approval(store, package) is None:
        return {"state": "needs_approval", "package": dict(package),
                "reasons": [f"approve version {package['version']}: careeros package approve {package['package_id']}"]}
    return {"state": "ready", "package": dict(package), "reasons": [], "approval": _approval(store, package)}
