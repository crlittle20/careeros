# ADR 0008: One local store, with enforced separation of duties and an audit chain

- Status: Accepted; amended 2026-11-03 (schema guards)
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

## Amendments

- **2026-11-03, schema guards** (OPP-50 of the 2026-10-31 opportunity audit; the owner's decision D83). That day a
  command run from another checkout migrated the live database before its deploy, and the older engine then ran on
  the newer schema, correctly. So:
  - a migration says whether older engines may run on the schema it reaches (`RUNS_ON_OLDER` in
    `careeros/db/store.py`; 015 says schema-14 engines may), and records the answer in the database
    (`meta.oldest_engine_schema`), because the older engine cannot know it;
  - an engine refuses a database newer than itself unless that record names it (`SchemaTooNew`), so a rollback
    across a migration needs no restore where the migration allows it, and is refused where it does not;
  - only a command that holds the writer lock migrates; one without it refuses a pending migration in one line
    (`MigrationPending`), and `careeros migrate` applies it after a verified backup;
  - `careeros verify` also runs SQLite's `quick_check` and `foreign_key_check`.
- **2026-11-03, an encrypted offsite archive** (OPP-26; the owner's decision D97). The store stays local, but its
  backups shared its disk. After the daily backup, an archive of the newest backup and the hand-written vault folders
  is encrypted with GnuPG (symmetric AES-256, SHA-512 key derivation; any GnuPG can open it) into a folder the owner
  chooses, such as a OneDrive folder. Nothing unencrypted reaches that folder: each archive is decrypted back and
  compared before it is named, and a weekly drill decrypts the newest one and checks its manifest. The passphrase is
  in Windows Credential Manager, reaches gpg on its standard input only, and is kept by the owner outside the PC.
  Off until set up; never in Demo Mode. Rejected: Python's `cryptography` (a new compiled dependency for the same
  result) and a copy by hand to a USB drive (it depends on remembering).

  *Note, 2026-11-04 (Codex CR-2026-11-03-PYTHON-314, F1 and N1):* `careeros unlock` was the one writer outside that
  rule. It removed a stale lock, then opened the database (and so migrated it) and marked interrupted runs failed
  without a lock of its own, while another command could already hold the lock it had just freed. It now takes the
  writer lock first, and when another command was quicker it changes nothing and says to run it again. And the
  backup `careeros migrate` takes is a checkpoint folder of its own, which the same day's daily backup no longer
  replaces.

## Evidence

- Code: `careeros/db/store.py` (`TABLES_BY_COMPONENT`, `COLUMN_LIMITS`, `Writer.transition`), `db/schema/*.sql`,
  `core/locks.py`, `core/policy.py`.
- Tests (schema guards: `tests/test_resilience.py`; the offsite archive: `tests/test_offsite.py`):
  - `test_separation_of_duties`, `test_state_columns_only_via_transition`, `test_history_tables_are_immutable`;
  - `test_event_chain_detects_tampering`, `test_projection_mismatch_fails_closed`, `test_idempotency_receipts`;
  - `test_second_writer_refused_and_stale_lock_held`.
