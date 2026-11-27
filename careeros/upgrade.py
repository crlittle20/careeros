"""The P1 upgrade pass (the D190 design, revision 8, section 7, "Rows stored before the upgrade"): rows an older engine
stored are handled once, under the writer lock, from the first command after migration 017, in batches; a meta key
written with the last batch marks the pass done, and until then each command resumes it. Every step is idempotent.

- Every open hold an older engine recorded (an ingest failure with no posting identity) is stamped with its job's
  posting identity and its hold_id (its own exception_id: build item Y3), so it reaches the job of record after a join.
- Every job's text, and the text of leads and alert notices, is made canonical, and a job's title key is rebuilt from
  its canonical title (Codex Pass 5).
- A job no board reads (pasted, saved, an alert's, add-url --text-file), and a job a board's read wrote whose board
  CareerOS may not read now (build item W1: by the posting's boards, not the job's ats, and at every processing run, not
  only here: BATCH_5 G2), gets its source record from its stored columns: its location with where it came from, and
  its employer entry (a stored parent equal to parent_for's under the current policy is resolved by parent_for, any
  other is kept as stored). An input the columns cannot give is recorded as unknown, never as stated or absent, so the
  job's first read counts as a change (Codex Pass 6). Its derived values are rebuilt from that record.
- Any other board job keeps its stored values and gets its source record at its first board read; until then it offers
  no preview ("waiting for a board read").
"""
from __future__ import annotations

import json
from collections import Counter

from careeros.normalize.sanitize import canonical

DONE = "p1_upgrade_done"
BATCH = 200
# the inputs a job's stored columns cannot give (the D190 design, section 7): the board's structured pay, its
# employment-type and workplace hints, its typed countries, the untruncated department, and what a retention was bound to
CLOCK_BEHIND = "jobs_waiting_for_the_clock"
UNKNOWN = ("bindings", "comp", "department", "employment_type_hint", "typed_countries", "workplace_hint")


def done(store) -> bool:
    row = store.one("SELECT value FROM meta WHERE key = ?", (DONE,))
    return row is not None and row["value"] == "1"


def run(ctx) -> Counter:
    """The pass, resumed until its marker is written. Returns what it did."""
    store, counts = ctx.store, Counter[str]()
    if done(store):
        return counts
    counts["holds_stamped"] += _stamp_holds(store, counts)
    counts["leads_made_canonical"] += _canonical_rows(store, "leads", "lead_id")
    counts["notices_made_canonical"] += _canonical_rows(store, "alert_notices", "notice_id")
    last = ""
    while True:
        batch = [r["job_id"] for r in store.all("SELECT job_id FROM jobs WHERE source_json IS NULL AND job_id > ? "
                                                "ORDER BY job_id LIMIT ?", (last, BATCH))]
        if not batch:
            break
        for job_id in batch:
            try:
                with store.tx():   # each job's canonical text, title key and (where it gets one) source record: one transaction
                    counts.update(_job(ctx, job_id))
            except ValueError as exc:
                if str(exc) != "out_of_order_event":
                    raise
                # the clock is behind the job's last state change (set back, or corrected after running ahead): its
                # rebuild is rolled back and read again by a later command, the others go on, and no command is refused
                # (BATCH_5 G1). Until then the job offers no preview ("waiting for the upgrade")
                counts[CLOCK_BEHIND] += 1
        last = batch[-1]
    if counts[CLOCK_BEHIND]:
        return +counts   # not done: the marker waits for every job
    with store.tx():
        writer = store.writer("admin")
        if store.one("SELECT 1 FROM meta WHERE key = ?", (DONE,)):
            writer.update("meta", {"value": "1"}, "key = ?", (DONE,))
        else:
            writer.insert("meta", {"key": DONE, "value": "1"})
    return +counts


def _stamp_holds(store, counts: Counter | None = None) -> int:
    from careeros.discovery.board_discovery import job_identity
    from careeros.review.packages import EVERY_JOB, identity_key
    writer, stamped, counts = store.writer("discovery"), 0, counts if counts is not None else Counter()
    for row in store.all("SELECT exception_id, entity_id, metadata_json FROM exceptions WHERE error_code = 'ingest_failed' "
                         "AND entity_type = 'job' AND status != 'resolved'"):
        try:
            meta = json.loads(row["metadata_json"] or "{}") or {}
        except ValueError:
            meta = {}
        if meta.get("posting_identity") or meta.get("rule") or meta.get("stamped"):
            continue   # recorded by this engine, or stamped by an earlier run of this pass: it carries its identity
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (row["entity_id"],))
        # a read whose new job's insert failed held the generated id, so no job has it: its posting is read from the id
        # itself (a board's posting id names the posting; BATCH_5 B1)
        identity = job_identity(job) if job is not None else identity_of_id(row["entity_id"])
        # an id that names no link (a pasted, saved or file-added posting's) and no job: which posting it was cannot be
        # read, so the hold reaches every job until a job with exactly that id is created (Z8)
        every = job is None and identity is None
        if writer.annotate_exception(row["exception_id"], {
                "posting_identity": identity_key(identity), "hold_id": row["exception_id"], "stamped": "p1_upgrade",
                **({"reaches": EVERY_JOB} if every else {})}):
            stamped += 1
            counts["holds_reaching_every_job"] += int(every)
    return stamped


def identity_of_id(job_id: str | None):
    """The posting a generated job id names by itself: a board's id ("greenhouse:acme:555"), or None (a manual id names
    no link)."""
    from careeros.discovery.board_discovery import BOARD_ATS, job_identity
    parts = (job_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] not in BOARD_ATS:
        return None
    return job_identity({"ats": parts[0], "external_id": parts[2], "url": None, "job_id": job_id})


def _canonical_rows(store, table: str, key: str) -> int:
    from careeros.db.store import CANONICAL_TEXT
    columns = sorted(CANONICAL_TEXT[table])
    writer, changed = store.writer("discovery"), 0
    for row in store.all(f"SELECT {key}, {', '.join(columns)} FROM {table}"):
        values = {c: canonical(row[c]) for c in columns if isinstance(row[c], str) and row[c] != canonical(row[c])}
        if values:
            with store.tx():
                writer.update(table, values, f"{key} = ?", (row[key],))
            changed += 1
    return changed


def record_from_columns(ctx, job) -> dict:
    """A job's source record rebuilt from its stored columns (the D190 design, section 7): what they state, where its
    location came from (the latest record of it: a manual job's observations), and its employer entry; every input they
    cannot give is listed as unknown."""
    from careeros.normalize.posting import RawPosting
    from careeros.pipeline import employer_entry, source_record
    store = ctx.store
    parts = job["job_id"].split(":", 2)
    facts: dict = {}
    for o in store.all("SELECT facts_json FROM observations WHERE job_id = ? ORDER BY observed_at DESC, rowid DESC",
                       (job["job_id"],)):
        found = json.loads(o["facts_json"] or "{}")
        if found.get("location_write") in ("argument", "label", "file", "none"):
            facts = found
            break
    source = facts.get("location_write")
    if (job["ats"] or "manual") == "manual" and (job["location_raw"] or "").strip():
        # where the location came from, as the current rules hold it: a location they do not hold as declared (no
        # location write, an older reader's, a D162 re-paste's confirmation) is unknown and places nothing, as for a fresh
        # intake of the same row (the D190 design, section 3a; BATCH_5 D2)
        from careeros.discovery.manual_intake import label_location, location_declared
        if not location_declared(store, dict(job)):
            source = "unknown"
        elif source is None:
            source = "label" if label_location(job["description_text"] or "") == job["location_raw"] else "unknown"
        elif source == "label" and label_location(job["description_text"] or "") != job["location_raw"]:
            # an older paste's label write, and a location since written by a board's read (a W1 rebuild: BATCH_5 G2):
            # its Location line is not where this location came from
            source = "unknown"
    p = RawPosting(ats=job["ats"] or "manual", board_token=parts[1] if len(parts) == 3 else "",
                   external_id=job["external_id"] or (parts[2] if len(parts) == 3 else job["job_id"]),
                   title=job["title"] or "", location=job["location_raw"] or "", url=job["url"], apply_url=job["apply_url"],
                   posted_at=job["posted_at"], description_text=job["description_text"] or "",
                   requisition_id=job["requisition_id"],
                   raw={"location_source": source, "reader": facts.get("reader")})
    name, parent = job["employer_name"], job["parent"]
    entry = {"employer_id": job["employer_id"], "name": name, "parent": parent, "resolved": "stored"}
    if name:
        guessed = employer_entry(ctx, name=name)
        if guessed["parent"] == canonical(parent or ""):
            entry = {**guessed, "employer_id": job["employer_id"]}   # the policy's parent: resolved again at each run
    record = source_record(p, entry)
    record["unknown"] = list(UNKNOWN)
    return record


def posting_boards(store, job) -> set:
    """The boards whose reads write a job's posting: its own ats (a board's job), the board its own link names (a job
    you pasted, added or saved first, whose posting a board's read then wrote: D188), and the board of each board read
    written into it (its ats_scan observations' posting keys). BATCH_5 G2: never the job's ats alone."""
    from careeros.discovery.board_discovery import BOARD_ATS, job_identity
    boards = {job["ats"]} if job["ats"] in BOARD_ATS else set()
    identity = job_identity(job)
    if identity is not None and identity[0] in BOARD_ATS:
        boards.add(identity[0])
    for row in store.all("SELECT DISTINCT posting_key FROM observations WHERE job_id = ? AND source_kind = 'ats_scan'",
                         (job["job_id"],)):
        board = (row["posting_key"] or "").split(":", 1)[0]
        if board in BOARD_ATS:
            boards.add(board)
    return boards


def board_forbidden(store, job) -> bool:
    """W1: whether every board whose read a job would wait for may not be read now under the site policy (a manual or
    blocked source), so no board read can ever come: the job is rebuilt from its columns like a job no board reads."""
    from careeros.discovery import sources
    boards = posting_boards(store, job)
    return bool(boards) and all(sources.status(board) != "allowed" for board in boards)


def rebuild_forbidden(ctx) -> Counter:
    """W1 at every processing run, not only at the upgrade (BATCH_5 G2): a job that waits for a board read the site
    policy forbids (its board made manual or blocked since) is rebuilt from its columns. A job the clock would put out of
    order is left for a later run, as in the upgrade (G1)."""
    from careeros.pipeline import board_written
    store, counts = ctx.store, Counter[str]()
    for row in store.all("SELECT * FROM jobs WHERE source_json IS NULL ORDER BY job_id"):
        if not (board_written(store, row["job_id"]) and board_forbidden(store, row)):
            continue
        try:
            with store.tx():
                counts.update(_job(ctx, row["job_id"]))
        except ValueError as exc:
            if str(exc) != "out_of_order_event":
                raise
            counts[CLOCK_BEHIND] += 1
    return +counts


def _job(ctx, job_id: str) -> Counter:
    from careeros.normalize.posting import norm
    from careeros.pipeline import board_written, derive, source_json
    store, counts = ctx.store, Counter[str]()
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None or job["source_json"]:
        return counts
    disc = store.writer("discovery")
    from careeros.db.store import CANONICAL_TEXT
    text = {c: canonical(job[c]) for c in sorted(CANONICAL_TEXT["jobs"]) if isinstance(job[c], str) and job[c] != canonical(job[c])}
    if job["title"]:
        title_norm = norm(canonical(job["title"]))
        if title_norm != job["title_norm"]:
            text["title_norm"] = title_norm
    if text:
        disc.update("jobs", text, "job_id = ?", (job_id,))
        counts["jobs_made_canonical"] += 1
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    board = board_written(store, job_id)
    unreadable = board and board_forbidden(store, job)   # by the posting's boards, never the job's ats (BATCH_5 G2)
    if board and not unreadable:
        counts["jobs_waiting_for_a_board_read"] += 1   # its record comes with its first board read
        return counts
    record = record_from_columns(ctx, job)
    fields = derive(ctx, record, job_id)
    values = {k: v for k, v in fields.items() if k != "source_updated_at" and job[k] != v}
    values["source_json"] = source_json(record)
    disc.update("jobs", values, "job_id = ?", (job_id,))
    if job["current_state"] not in ("discovered", "normalized", "closed", "duplicate"):
        disc.transition(ctx.matrix, job_id, "normalized", reason_code="source_record_rebuilt")
    counts["boards_unreadable_rebuilt" if unreadable else "records_rebuilt"] += 1
    return counts
