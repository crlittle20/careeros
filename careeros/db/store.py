"""SQLite store with separation of duties enforced in code (pattern from the GrowthOS write guard).

Each pipeline component gets a Writer that may only touch the tables (and, for `jobs`, the columns) it owns.
Workflow state changes only through `Writer.transition`, which writes the authoritative transition row,
the projection and a matching audit event in one transaction and fails closed on any mismatch.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path

from careeros import PROJECT_ID, __version__
from careeros.core.clock import Clock, parse_utc, require_order
from careeros.core.fingerprint import canonical_json, fingerprint, text_fingerprint
from careeros.core.records import CONTRACT_VERSION, validate_audit_event, validate_exception
from careeros.core.transitions import Matrix

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
GENESIS = "0" * 64
# Schema guards (OPP-50, D83). For a schema an older engine may still run on, the oldest such engine's schema. The
# migration that reaches it records the value in the database, in its own transaction (Codex's D190 design review,
# H9), because the older engine cannot know it. 015 rebuilt two tables with their rows and added columns older code
# never reads, and the schema-14 engine ran on it for real (2026-11-03 07:00), so a rollback across it needs no
# restore. A schema not listed here opens only with code that knows it: 016 is not listed (the owner's D191), so an
# engine older than it refuses the database, and a rollback across it is a restore of the backup made before it. 017
# (the P1 build of the D190 design, the owner's D198) is not listed either: an engine without P1 never writes a
# database that holds P1's approvals.
RUNS_ON_OLDER = {15: 14}


def newest_schema() -> int:
    """The newest schema this code can migrate to."""
    return max(int(script.name.split("_", 1)[0]) for script in SCHEMA_DIR.glob("*.sql"))


class SchemaTooNew(RuntimeError):
    """The database is newer than this engine, and not marked safe for it."""


class MigrationPending(RuntimeError):
    """The database needs a migration, and this command may not apply one."""


class TransactionLost(RuntimeError):
    """SQLite ended the whole transaction this work was nested in (it answers a full disk that way), so nothing of it is
    kept and nothing after it may be written as part of it. Raised inside the transaction only: once its outermost
    `Store.tx` frame is left, the caller sees the original error (`original`), as for any failed transaction. Work that
    catches a failure and carries on (one bad posting, one bad card) re-raises this instead (the D190 design, revision 4,
    section 7; found by the gate session's run of the red team's rtv_hr1faults_1 on 74807d3)."""

    def __init__(self, original: BaseException | None):
        self.original = original
        cause = f"{type(original).__name__}: {original}" if original is not None else \
            "a failure inside it was caught and the work carried on"
        super().__init__(f"transaction_lost: SQLite ended the transaction ({cause}); nothing of it was kept")

STATE_COLUMNS = {"current_state", "state_sequence", "entered_state_at"}
JOB_COLUMNS_BY_COMPONENT = {
    "discovery": {"employer_id", "employer_name", "parent", "requisition_id", "title", "title_norm", "family",
                  "location_raw", "remote_type", "remote_states_json", "remote_excluded_states_json", "country", "employment_type", "comp_min",
                  "comp_max", "comp_period", "comp_currency", "posted_at", "last_seen_at", "closed_at", "url",
                  "apply_url", "canonical_url", "description_text", "content_fingerprint", "dedupe_key",
                  "duplicate_of", "repost_of", "variant_of", "exclude_from_kpi", "department", "source_updated_at",
                  "absent_scans", "deadline_at", "remote_scope", "timezone_requirement", "workplace_hint", "board_pay_json",
                  "typed_countries_json", "source_json"},
    "extract": {"extraction_fingerprint", "requirements_fingerprint"},
}
# Board lifecycle columns (2026-10-13 verification). Automatic verification may change them; only an admin action may
# activate a board or record / clear a manual override.
BOARD_LIFECYCLE_COLUMNS = {"entity_id", "career_site_id", "canonical_ats_url", "board_fingerprint", "identity_signals",
                           "verification_status", "provenance", "handshake_status", "identity_check", "validation_status",
                           "board_trust_score", "board_health_score", "board_activity", "last_checked_at", "reverify_at",
                           "quarantined_at", "retired_at", "migrated_to", "paused_reason", "verification_failure",
                           "verify_count", "site_failures"}
BOARD_OVERRIDE_COLUMNS = {"override_reason", "override_by", "override_at", "override_prev_status", "override_expires_at"}
# Column limits on other mutable tables, per component (absent component = all non-state columns).
COLUMN_LIMITS = {
    ("leads", "review"): {"status", "job_id", "status_reason", "status_at"},   # your confirmations and corrections (2026-11-02)
    ("applications", "tracker"): {"notes"},
    ("inbox_issues", "discovery"): {"closed"},
    ("lead_matches", "review"): {"audit", "audited_at"},
    ("alert_notices", "review"): {"state", "state_at", "job_id"},   # your dismissal or addition (part 3 of D192)
    ("label_previews", "review"): {"used_at", "label_id"},   # a preview is used once, by one label (D190, 017)
    ("lead_matches", "discovery"): set(),
    ("title_audit", "review"): {"verdict", "verdict_at"},
    ("board_mappings", "discovery"): {"official_domain", "identity_status", "identity_evidence", "verified_by",
                                      "verified_method", "verified_at", "relevant_roles", "updated_at", "employer_name",
                                      *BOARD_LIFECYCLE_COLUMNS},
    ("board_mappings", "admin"): {"identity_status", "identity_evidence", "verified_by", "verified_method", "verified_at",
                                  "activated_at", "updated_at", "official_domain", "relationship", *BOARD_LIFECYCLE_COLUMNS,
                                  *BOARD_OVERRIDE_COLUMNS},
}
TABLES_BY_COMPONENT = {
    "discovery": {"employers", "jobs", "job_snapshots", "runs", "leads", "alert_messages", "inbox_issues", "board_mappings",
                  "discovery_queue", "discovery_attempts", "lead_matches", "title_reviews", "title_audit", "observations",
                  "opening_links", "job_changes", "source_scans", "employer_entities", "career_sites", "verification_runs",
                  "verification_evidence", "board_reviews", "provider_status", "career_fit_history", "alert_notices",
                  "hold_clearances"},
    "extract": {"requirements", "jobs"},
    "qualify": {"gate_results", "scores", "night_assessments", "night_results"},
    "review": {"labels", "leads", "dismissals", "lead_matches", "title_audit", "alert_notices", "label_previews"},
    "tracker": {"applications"},
    "submission": {"submission_releases"},   # your releases of a submission hold (R2)
    "assist": {"packages", "package_approvals", "assist_sessions", "assist_outcomes"},   # assisted applications (ADR 0009)
    "runner": {"runs"},
    "admin": {"employers", "runs", "meta", "board_mappings", "employer_aliases", "discovery_queue", "employer_entities",
              "board_reviews", "verification_evidence", "verification_runs"},
}
MUTABLE_TABLES = {"employers", "jobs", "runs", "meta", "exceptions", "leads", "applications", "inbox_issues", "board_mappings",
                  "discovery_queue", "lead_matches", "title_audit", "employer_entities", "career_sites", "board_reviews",
                  "provider_status", "alert_notices", "label_previews"}
# The content columns whose text must be in the one canonical form (the D190 design, section 3a: sanitize.canonical),
# beside the column registry above. A write of any other form is refused, so an entry point that misses canonical()
# fails in the tests and the fuzzer instead of storing a second form. URL, id, digest and timestamp columns are not
# listed and never changed; exceptions, job_changes and reasons record, not read, and stay unmarked.
CANONICAL_TEXT = {
    "jobs": {"title", "location_raw", "description_text", "requisition_id", "department", "employer_name", "parent",
             "workplace_hint", "employment_type", "remote_type", "country", "remote_scope", "timezone_requirement",
             "title_norm", "family"},
    "employers": {"name", "parent"},
    "leads": {"title", "title_norm", "context", "sender", "subject"},
    "alert_notices": {"title", "title_norm", "context", "sender", "subject", "suggested_employer"},
    "observations": {"title", "company", "location", "requisition_id", "pay_text"},
}
# JSON columns whose text values are checked the same way (links excluded): the source record, and a sighting's facts
CANONICAL_JSON = (("jobs", "source_json"), ("observations", "facts_json"))


def non_canonical(table: str, row: dict) -> list[str]:
    """The columns of a write whose text is not in the canonical form (empty: the write may go)."""
    from careeros.normalize.sanitize import canonical, canonical_values
    bad = [k for k in sorted(CANONICAL_TEXT.get(table, ())) if isinstance(row.get(k), str) and row[k] != canonical(row[k])]
    for column in (c for t, c in CANONICAL_JSON if t == table and isinstance(row.get(c), str)):
        try:
            value = json.loads(row[column])
        except ValueError:
            continue
        if column == "facts_json" and not (isinstance(value, dict) and value.get("sighting")):
            continue   # only a sighting's facts carry posting content
        if value != canonical_values(value):
            bad.append(column)
    return bad


WORKFLOW_ENTITIES = {  # entity -> (table, id column, event type)
    "job": ("jobs", "job_id", "job_status_changed"),
    "application": ("applications", "application_id", "application_status_changed"),
}


class PermissionDenied(RuntimeError):
    pass


class DataQualityError(RuntimeError):
    pass


class NonCanonicalText(DataQualityError):
    """A write of posting content in another form than sanitize.canonical's: a path that missed it fails loudly
    instead of storing a second form (the D190 design, section 3a)."""


class IdempotencyCollision(RuntimeError):
    pass


def _pending_holds(store: "Store"):
    from careeros.review.packages import _read_pending
    return _read_pending(store)


# what a read transaction reads beside the database before its snapshot starts, by name (Codex H44): the holds kept
# beside the database (the review's pending-holds file). A decision made in the read pairs that reading with a snapshot
# that starts after it, and every path that removes a line does so only after the row or clearance that replaces it has
# committed, so a hold moving from the file into a row is in one view or the other, never in neither
BESIDE = {"pending_holds": _pending_holds}


class Store:
    def __init__(self, path: Path | str, clock: Clock, policy_fingerprint: str | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # The review server is single-threaded (one request at a time) but not the thread that opened the store.
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # A store that is discarded without close() closes its connection then, so Python 3.13+ never warns of an
        # unclosed database from the garbage collector (a warning that can land in a test's captured output).
        self._closer = weakref.finalize(self, self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.clock = clock
        self.policy_fingerprint = policy_fingerprint
        self._depth = 0
        self._reading = False
        self._beside: dict = {}   # what the read in progress read beside the database before its snapshot (BESIDE)
        self.committed = 0   # rows changed by committed transactions: what a caller may tell you was saved

    def close(self) -> None:
        self._closer()   # closes the connection once; later calls do nothing

    # --- schema -------------------------------------------------------------------------------------
    def _meta(self, key: str) -> str | None:
        exists = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone() if exists else None
        return row[0] if row else None

    def schema_version(self) -> int:
        return int(self._meta("schema_version") or 0)

    def check_schema(self) -> int:
        """The database's schema, if this code may use it as it is; SchemaTooNew or MigrationPending otherwise. Writes
        nothing (OPP-50: a command without the writer lock never migrates)."""
        current, newest = self.schema_version(), newest_schema()
        if current > newest:
            oldest = self._meta("oldest_engine_schema")
            if oldest is None or newest < int(oldest):
                raise SchemaTooNew(f"the database is at schema {current}, newer than this engine ({newest}), and not marked "
                                   "safe for it: deploy the newer engine, or restore a backup made before the upgrade")
        elif current < newest:
            raise MigrationPending(f"the database is at schema {current} and this engine needs {newest}: a command that "
                                   "writes applies the migration under the writer lock ('careeros migrate' backs up first)")
        return current

    def migrate(self) -> int:
        current = self.schema_version()
        if current > newest_schema():
            return self.check_schema()   # an older engine on a newer database: allowed only where marked, never migrated
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            number = int(script.name.split("_", 1)[0])
            if number <= current:
                continue
            # the schema it reaches, and the oldest engine that may open it, are written in the migration's own
            # transaction: a stop after its COMMIT never leaves the new tables under the old markers (Codex's D190
            # design review, H9). 016 writes both itself as well.
            markers = "".join(f"\nINSERT OR REPLACE INTO meta(key, value) VALUES ('{key}', '{value}');" for key, value in (
                ("schema_version", number), ("contract_version", CONTRACT_VERSION),
                ("oldest_engine_schema", RUNS_ON_OLDER.get(number, number))))
            try:
                self.conn.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + markers + "\nCOMMIT;")
            except BaseException:
                if self.conn.in_transaction:   # a statement or the COMMIT failed: nothing of the migration stays
                    self.conn.execute("ROLLBACK")
                raise
            current = number
        oldest = str(RUNS_ON_OLDER.get(current, current))   # also for a database migrated before this record existed
        if current and self._meta("oldest_engine_schema") != oldest:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('oldest_engine_schema', ?)", (oldest,))
        return current

    @contextmanager
    def read(self):
        """One read transaction (the D190 design, section 3, "Reads and writes"): every read inside it sees one
        snapshot of the database (BEGIN DEFERRED; in WAL mode one snapshot from its first read to its end), and nothing
        is written (PRAGMA query_only; a tx() inside it is refused). Inside an open transaction, a tx() or another
        read(), it is that transaction: every capture and P1 check runs in one. Before its snapshot starts (its first
        query) it reads what is kept beside the database (BESIDE: the pending-holds file), and every decision in it uses
        that reading (beside), so the file it sees is never newer than the database it is paired with (Codex H44)."""
        if self._depth or self._reading:
            yield
            return
        beside = {name: reader(self) for name, reader in BESIDE.items()}   # before any statement of the transaction
        self.conn.execute("PRAGMA query_only = 1")
        try:
            self.conn.execute("BEGIN DEFERRED")
            self._reading, self._beside = True, beside
            try:
                yield
            finally:
                self._reading, self._beside = False, {}
                if self.conn.in_transaction:
                    self.conn.execute("COMMIT")
        finally:
            self.conn.execute("PRAGMA query_only = 0")

    def beside(self, name: str):
        """What the read transaction in progress read beside the database before its snapshot started (BESIDE), or None
        outside one. A write transaction reads the file when it needs it: its snapshot starts with the write lock, so no
        other connection commits a hold's row while it runs, and the line stays in the file until after that commit."""
        return self._beside.get(name) if self._reading else None

    @contextmanager
    def tx(self):
        """One transaction. Nested, a savepoint: its own work is undone when it fails, so code that catches the failure
        and goes on (one bad row of many) never keeps half of it inside an outer transaction (red team before Codex
        Pass 3: a whole email is now one outer transaction)."""
        if self._reading:
            raise PermissionDenied("write_in_read: a write inside a read transaction (Store.read)")
        if self._depth:
            self._depth += 1
            name = f"careeros_{self._depth}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException as exc:
                if not self.conn.in_transaction:
                    # SQLite ended the whole transaction (a full disk): no savepoint is left to roll back to, and the
                    # work this is nested in must not carry on (the D190 design, revision 4, section 7)
                    if isinstance(exc, TransactionLost):
                        raise
                    raise TransactionLost(exc) from exc
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
                raise
            else:
                if not self.conn.in_transaction:   # lost below, and the failure caught by code in between
                    raise TransactionLost(None)
                self.conn.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        start = self.conn.total_changes
        try:
            yield
            changed = self.conn.total_changes - start
            if not self.conn.in_transaction:   # lost inside, and the failure caught by code in between: never a success
                raise TransactionLost(None)
            self.conn.execute("COMMIT")   # under the rollback below: a COMMIT that fails (disk full) leaves no
            # transaction open on this connection (Codex's D190 design review; the red team's hr1-faults-0)
        except BaseException as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            if isinstance(exc, TransactionLost) and exc.original is not None:
                # outside the lost transaction it is an ordinary failed transaction, with its own cause
                raise exc.original from None
            raise
        else:
            self.committed += changed
        finally:
            self._depth = 0

    def writer(self, component: str) -> "Writer":
        if component not in TABLES_BY_COMPONENT:
            raise PermissionDenied(f"unknown_component:{component}")
        return Writer(self, component)

    # --- reads --------------------------------------------------------------------------------------
    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def verify_event_chain(self) -> list[str]:
        problems, previous = [], GENESIS
        for row in self.all("SELECT * FROM events ORDER BY seq"):
            event = _event_body(dict(row))
            if row["prev_hash"] != previous or row["hash"] != _chain_hash(previous, event):
                problems.append(row["event_id"])
            previous = row["hash"]
        return problems

    def verify_job_consistency(self, matrix: Matrix, job_id: str, entity: str = "job") -> set[str]:
        from careeros.core.transitions import verify_history
        table, id_column, _ = WORKFLOW_ENTITIES[entity]
        job = self.one(f"SELECT * FROM {table} WHERE {id_column} = ?", (job_id,))
        rows = [dict(r) for r in self.all("SELECT * FROM transitions WHERE workflow_id = ? ORDER BY sequence", (job["workflow_id"],))]
        audits = [dict(r) for r in self.all("SELECT * FROM events WHERE workflow_id = ? AND transition_id IS NOT NULL", (job["workflow_id"],))]
        errors = verify_history(matrix, rows, audits, initial_entered_at=job["first_seen_at"])
        last = rows[-1] if rows else None
        expected_state = last["to_state"] if last else matrix.initial_state
        if job["current_state"] != expected_state or job["state_sequence"] != len(rows):
            errors.add("projection_mismatch")
        return errors


def _event_body(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("seq", "prev_hash", "hash")}


def _chain_hash(previous: str, body: dict) -> str:
    return text_fingerprint(previous + canonical_json(body))


class Writer:
    def __init__(self, store: Store, component: str):
        self.store = store
        self.component = component
        self.tables = TABLES_BY_COMPONENT[component]

    # --- table writes -------------------------------------------------------------------------------
    def _need(self, table: str) -> None:
        if table not in self.tables:
            raise PermissionDenied(f"{self.component}_cannot_write:{table}")

    def insert(self, table: str, row: dict) -> None:
        self._need(table)
        if bad := non_canonical(table, row):
            raise NonCanonicalText(f"non_canonical_text: {table}.{', '.join(bad)}")
        if table == "jobs" and self.component != "discovery":
            raise PermissionDenied(f"{self.component}_cannot_create_jobs")
        if table in ("jobs", "applications") and (row.get("state_sequence", 0) != 0):
            raise PermissionDenied("workflow_must_start_at_sequence_0")
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.store.tx():
            self.store.conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))

    def update(self, table: str, values: dict, where: str, params=()) -> int:
        self._need(table)
        if bad := non_canonical(table, values):
            raise NonCanonicalText(f"non_canonical_text: {table}.{', '.join(bad)}")
        if table not in MUTABLE_TABLES:
            raise PermissionDenied(f"immutable_table:{table}")
        if table in ("jobs", "applications") and set(values) & STATE_COLUMNS:
            raise PermissionDenied("state_columns_change_only_through_transition")
        allowed = JOB_COLUMNS_BY_COMPONENT.get(self.component, set()) if table == "jobs" else COLUMN_LIMITS.get((table, self.component))
        if allowed is not None and not set(values) <= allowed:
            raise PermissionDenied(f"{self.component}_cannot_write_columns:{sorted(set(values) - allowed)}")
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.store.tx():
            cursor = self.store.conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}", (*values.values(), *params))
        return cursor.rowcount

    # --- controlled records -------------------------------------------------------------------------
    def event(self, event_type: str, *, outcome: str, event_id: str | None = None, occurred_at: str | None = None,
              entity_type: str | None = None, entity_id: str | None = None, message: str | None = None,
              metadata: dict | None = None, workflow_id: str | None = None, transition_id: str | None = None,
              previous_state: str | None = None, new_state: str | None = None, correlation_id: str | None = None,
              actor_type: str = "system", actor_id: str | None = None, exclude_from_kpi: bool = False) -> str:
        store = self.store
        body = {
            "event_id": event_id or f"evt:{uuid.uuid4().hex}",
            "contract_version": CONTRACT_VERSION,
            "event_type": event_type,
            "occurred_at": occurred_at or store.clock.now(),
            "project_id": PROJECT_ID,
            "project_version": __version__,
            "source": f"careeros.{self.component}",
            "outcome": outcome,
            "actor_type": actor_type, "actor_id": actor_id, "workflow_id": workflow_id,
            "transition_id": transition_id, "correlation_id": correlation_id,
            "entity_type": entity_type, "entity_id": entity_id,
            "previous_state": previous_state, "new_state": new_state, "message": message,
            "metadata_json": canonical_json(metadata or {}),
            "policy_fingerprint": store.policy_fingerprint,
            "exclude_from_kpi": 1 if exclude_from_kpi else 0,
        }
        problems = validate_audit_event({**body, "metadata": metadata or {}})
        if problems:
            raise ValueError(f"invalid_audit_event:{problems}")
        with store.tx():
            last = store.one("SELECT hash, occurred_at FROM events ORDER BY seq DESC LIMIT 1")
            previous = last["hash"] if last else GENESIS
            store.conn.execute(
                f"INSERT INTO events ({', '.join(body)}, prev_hash, hash) VALUES ({', '.join('?' for _ in body)}, ?, ?)",
                (*body.values(), previous, _chain_hash(previous, body)))
        return body["event_id"]

    def exception(self, exception_type: str, severity: str, message: str, *, error_code: str | None = None,
                  entity_type: str | None = None, entity_id: str | None = None, retryable: bool | None = None,
                  correlation_id: str | None = None, metadata: dict | None = None) -> str:
        record = {
            "exception_id": f"exc:{uuid.uuid4().hex}",
            "contract_version": CONTRACT_VERSION,
            "occurred_at": self.store.clock.now(),
            "project_id": PROJECT_ID,
            "source": f"careeros.{self.component}",
            "exception_type": exception_type,
            "severity": severity,
            "message": message[:500],
            "status": "open",
            "correlation_id": correlation_id, "entity_type": entity_type, "entity_id": entity_id,
            "error_code": error_code, "retryable": None if retryable is None else int(retryable),
            "retry_count": 0, "metadata_json": canonical_json(metadata or {}),
        }
        problems = validate_exception(record)
        if problems:
            raise ValueError(f"invalid_exception:{problems}")
        with self.store.tx():
            self.store.conn.execute(
                f"INSERT INTO exceptions ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})", tuple(record.values()))
            self.event("exception_created", outcome="failure", entity_type="exception", entity_id=record["exception_id"],
                       message=record["message"], occurred_at=record["occurred_at"], correlation_id=correlation_id,
                       metadata={"exception_type": exception_type, "severity": severity, "error_code": error_code,
                                 "subject_type": entity_type, "subject_id": entity_id, "new_status": "open"})
        return record["exception_id"]

    def annotate_exception(self, exception_id: str, metadata: dict) -> bool:
        """Add to the metadata of an open problem this component recorded (the P1 upgrade pass stamps an older engine's
        hold with its posting identity and hold_id: the D190 design, section 7). False when it is not open or another
        component recorded it."""
        with self.store.tx():
            row = self.store.one("SELECT metadata_json FROM exceptions WHERE exception_id = ? AND status != 'resolved' "
                                 "AND source = ?", (exception_id, f"careeros.{self.component}"))
            if row is None:
                return False
            try:
                merged = {**(json.loads(row["metadata_json"] or "{}") or {}), **metadata}
            except ValueError:
                merged = dict(metadata)
            self.store.conn.execute("UPDATE exceptions SET metadata_json = ? WHERE exception_id = ?",
                                    (json.dumps(merged, sort_keys=True), exception_id))
        return True

    def resolve_exception(self, exception_id: str, resolution: str) -> bool:
        """Close an open problem that this component recorded, with the reason and an event (2026-11-09: a held-back
        agency digest read in full later). False when it is not open or another component recorded it."""
        if not (resolution or "").strip():
            raise ValueError("resolution_required")
        now = self.store.clock.now()
        with self.store.tx():
            row = self.store.one("SELECT occurred_at FROM exceptions WHERE exception_id = ? AND status != 'resolved' AND source = ?",
                                 (exception_id, f"careeros.{self.component}"))
            if row is None:
                return False
            if parse_utc(now) < parse_utc(row["occurred_at"]):   # a clock set back since: resolved as of its own time (L4-C2)
                now = row["occurred_at"]
            self.store.conn.execute("UPDATE exceptions SET status = 'resolved', resolution = ?, resolved_at = ? WHERE exception_id = ?",
                                    (resolution[:500], now, exception_id))
            self.event("exception_resolved", outcome="success", entity_type="exception", entity_id=exception_id,
                       message=resolution[:500], occurred_at=now, metadata={"new_status": "resolved"})
        return True

    def receipt(self, command: str, key: str, payload: dict, actor_id: str = "system",
                result_ref: str | None = None) -> str:
        """'new' the first time; 'replay' for an identical repeat; IdempotencyCollision for a different payload.
        `result_ref` (an intake's job) is written at insert: receipts are never updated (the D190 design, section 6)."""
        if not key or not key.strip():
            raise ValueError("idempotency_key_required")
        digest = fingerprint({"command": command, "payload": payload})
        with self.store.tx():
            existing = self.store.one("SELECT payload_fingerprint FROM receipts WHERE receipt_key = ?", (key,))
            if existing:
                if existing["payload_fingerprint"] != digest:
                    raise IdempotencyCollision(key)
                return "replay"
            self.store.conn.execute(
                "INSERT INTO receipts(receipt_key, command, actor_id, payload_fingerprint, recorded_at, result_ref) "
                "VALUES (?,?,?,?,?,?)", (key, command, actor_id, digest, self.store.clock.now(), result_ref))
        return "new"

    def transition(self, matrix: Matrix, job_id: str, to_state: str, *, reason_code: str | None = None,
                   notes: str | None = None, trigger_type: str = "system", trigger_id: str | None = None,
                   correlation_id: str | None = None, entity: str = "job") -> dict:
        store = self.store
        table, id_column, event_type = WORKFLOW_ENTITIES[entity]
        with store.tx():
            job = store.one(f"SELECT {id_column}, workflow_id, current_state, state_sequence, entered_state_at, exclude_from_kpi "
                            f"FROM {table} WHERE {id_column} = ?", (job_id,))
            if job is None:
                raise DataQualityError(f"unknown_{entity}")
            last = store.one("SELECT sequence, to_state, transitioned_at FROM transitions WHERE workflow_id = ? "
                             "ORDER BY sequence DESC LIMIT 1", (job["workflow_id"],))
            history_state = last["to_state"] if last else matrix.initial_state
            history_sequence = last["sequence"] if last else 0
            if job["current_state"] != history_state or job["state_sequence"] != history_sequence:
                raise DataQualityError("projection_history_mismatch")  # fail closed; caller records the exception
            matrix.check(job["current_state"], to_state, reason_code)
            now = store.clock.now()
            require_order(last["transitioned_at"] if last else None, now)
            sequence = history_sequence + 1
            row = {
                "transition_id": f"tr:{job_id}:{sequence}", "contract_version": CONTRACT_VERSION, "project_id": PROJECT_ID,
                "workflow_id": job["workflow_id"], "sequence": sequence, "entity_type": entity, "entity_id": job_id,
                "from_state": job["current_state"], "to_state": to_state, "transitioned_at": now,
                "trigger_type": trigger_type, "trigger_id": trigger_id, "reason_code": reason_code,
                "correlation_id": correlation_id, "notes": notes,
            }
            store.conn.execute(f"INSERT INTO transitions ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                               tuple(row.values()))
            store.conn.execute(f"UPDATE {table} SET current_state = ?, state_sequence = ?, entered_state_at = ? WHERE {id_column} = ?",
                               (to_state, sequence, now, job_id))
            self.event(event_type, outcome="success", event_id=f"evt:{job_id}:{sequence}", occurred_at=now,
                       entity_type=entity, entity_id=job_id, workflow_id=job["workflow_id"],
                       transition_id=row["transition_id"], previous_state=row["from_state"], new_state=to_state,
                       correlation_id=correlation_id, actor_type=trigger_type, actor_id=trigger_id,
                       exclude_from_kpi=bool(job["exclude_from_kpi"]),
                       metadata={"reason_code": reason_code, "notes": notes})
        return row


def open_store(path, clock: Clock, policy_fingerprint: str | None = None, *, migrate: bool | None = True) -> Store:
    """migrate=True migrates (a command holding the writer lock); False only checks the schema and refuses if it needs a
    migration or is too new (a read-only command, OPP-50); None does neither (`careeros migrate`, which backs up first)."""
    store = Store(path, clock, policy_fingerprint)
    try:
        if migrate:
            store.migrate()
        elif migrate is False:
            store.check_schema()
    except Exception:
        store.close()
        raise
    return store


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
