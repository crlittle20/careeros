# ADR 0008: One local store, with enforced separation of duties and an audit chain

- Status: Accepted
- Decided: in the design since the first commit (2026-10-12)
- Recorded: 2026-10-13
- Related: NFR-001, NFR-003, SEC-006, AUD-001 to AUD-004

## Context

CareerOS serves one person, on one computer, with private data. It still has to:
- explain any decision after the fact;
- prove its history was not altered;
- reprocess the same input without side effects;
- survive one failing component without corrupting the rest.

A server database would add infrastructure and a network surface without adding value.

## Decision

- **One SQLite file** in the vault (WAL mode), changed only through numbered migrations (`db/schema/001-012`).
- **Single writer.** One process writes at a time, and a lock left by a crash is an operator hold, never cleared
  silently.
- **Separation of duties in code.** Each component writes only its own tables and columns. For example:
  - `qualify` cannot write jobs;
  - `discovery` cannot write scores;
  - only `admin` activates boards or records overrides.
- **Workflow matrices.** A state changes only by an allowed transition, which writes the history row, the projection
  and an audit event in one transaction. A mismatch fails closed.
- **Append-only history**, enforced by triggers, and a **hash-chained** event log (`careeros verify`).
- **Idempotency receipts** for human actions and replays. **Fingerprints** on every decision, so a rules change
  re-evaluates exactly the affected jobs.

## Consequences

**Gains:**
- `careeros explain` and `careeros verify` work from the store alone.
- Repeat processing changes nothing.
- A backup is one consistent file copy, with an integrity check.

**Costs:**
- One machine, one writer.
- Schema changes need migrations.
- Append-only history means mistakes are corrected by new rows, never by edits. ADR 0003 exists partly for that
  reason.

## Alternatives considered

- **A server database.** Rejected: infrastructure and exposure for one user.
- **Plain JSON or CSV files.** Rejected: no transactions, constraints or triggers.
- **An ORM.** Rejected: it would hide the write guards that make the separation of duties checkable.

## Evidence

- Code: `careeros/db/store.py` (`TABLES_BY_COMPONENT`, `COLUMN_LIMITS`, `Writer.transition`), `db/schema/*.sql`,
  `core/locks.py`, `core/policy.py`.
- Tests:
  - `test_separation_of_duties`, `test_state_columns_only_via_transition`, `test_history_tables_are_immutable`;
  - `test_event_chain_detects_tampering`, `test_projection_mismatch_fails_closed`, `test_idempotency_receipts`;
  - `test_second_writer_refused_and_stale_lock_held`.
