"""SQLite store with separation of duties enforced in code (pattern from the GrowthOS write guard).

Each pipeline component gets a Writer that may only touch the tables (and, for `jobs`, the columns) it owns.
Workflow state changes only through `Writer.transition`, which writes the authoritative transition row,
the projection and a matching audit event in one transaction and fails closed on any mismatch.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from careeros import PROJECT_ID, __version__
from careeros.core.clock import Clock, require_order
from careeros.core.fingerprint import canonical_json, fingerprint, text_fingerprint
from careeros.core.records import CONTRACT_VERSION, validate_audit_event, validate_exception
from careeros.core.transitions import Matrix

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
GENESIS = "0" * 64

STATE_COLUMNS = {"current_state", "state_sequence", "entered_state_at"}
JOB_COLUMNS_BY_COMPONENT = {
    "discovery": {"employer_id", "employer_name", "parent", "requisition_id", "title", "title_norm", "family",
                  "location_raw", "remote_type", "remote_states_json", "remote_excluded_states_json", "country", "employment_type", "comp_min",
                  "comp_max", "comp_period", "comp_currency", "posted_at", "last_seen_at", "closed_at", "url",
                  "apply_url", "canonical_url", "description_text", "content_fingerprint", "dedupe_key",
                  "duplicate_of", "repost_of", "variant_of", "exclude_from_kpi", "department", "source_updated_at",
                  "absent_scans", "deadline_at", "remote_scope", "timezone_requirement"},
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
    ("leads", "review"): {"status"},
    ("applications", "tracker"): {"notes"},
    ("inbox_issues", "discovery"): {"closed"},
    ("lead_matches", "review"): {"audit", "audited_at"},
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
                  "verification_evidence", "board_reviews", "provider_status", "career_fit_history"},
    "extract": {"requirements", "jobs"},
    "qualify": {"gate_results", "scores", "night_assessments", "night_results"},
    "review": {"labels", "leads", "dismissals", "lead_matches", "title_audit"},
    "tracker": {"applications"},
    "runner": {"runs"},
    "admin": {"employers", "runs", "meta", "board_mappings", "employer_aliases", "discovery_queue", "employer_entities",
              "board_reviews", "verification_evidence", "verification_runs"},
}
MUTABLE_TABLES = {"employers", "jobs", "runs", "meta", "exceptions", "leads", "applications", "inbox_issues", "board_mappings",
                  "discovery_queue", "lead_matches", "title_audit", "employer_entities", "career_sites", "board_reviews",
                  "provider_status"}
WORKFLOW_ENTITIES = {  # entity -> (table, id column, event type)
    "job": ("jobs", "job_id", "job_status_changed"),
    "application": ("applications", "application_id", "application_status_changed"),
}


class PermissionDenied(RuntimeError):
    pass


class DataQualityError(RuntimeError):
    pass


class IdempotencyCollision(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path | str, clock: Clock, policy_fingerprint: str | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # The review server is single-threaded (one request at a time) but not the thread that opened the store.
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.clock = clock
        self.policy_fingerprint = policy_fingerprint
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    # --- schema -------------------------------------------------------------------------------------
    def migrate(self) -> int:
        exists = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        current = int(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) if exists else 0
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            number = int(script.name.split("_", 1)[0])
            if number <= current:
                continue
            self.conn.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(number),))
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('contract_version', ?)", (CONTRACT_VERSION,))
            current = number
        return current

    @contextmanager
    def tx(self):
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
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

    def receipt(self, command: str, key: str, payload: dict, actor_id: str = "system") -> str:
        """'new' the first time; 'replay' for an identical repeat; IdempotencyCollision for a different payload."""
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
                "INSERT INTO receipts(receipt_key, command, actor_id, payload_fingerprint, recorded_at) VALUES (?,?,?,?,?)",
                (key, command, actor_id, digest, self.store.clock.now()))
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


def open_store(path, clock: Clock, policy_fingerprint: str | None = None) -> Store:
    store = Store(path, clock, policy_fingerprint)
    store.migrate()
    return store


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
