-- 2026-11-24 (the owner's D198: the P1 build of the D190 design, revision 8, after Codex's design review cleared it at
-- Pass 8).
--   jobs           source_json: the job's source record, every input normalized_fields reads (each RawPosting field as
--                  read, the raw-payload values its readers read, and the employer entry with how it was resolved), in
--                  the canonical form, retention applied. NULL until a read or the upgrade pass writes it: a job with
--                  none offers no preview (section 3a; section 7, the upgrade).
--   gate_results   source_digest: the digest of the source record the gate judged, so a result is current only for
--                  that record (part c; Codex's design review Pass 6). NULL on a row written before 017: not current.
--   packages       files_json: each rendered file's sha256; a package is ready only when its files match (Codex M1).
--   labels         preview_id: the preview an Apply or Stretch label was given from (labels stay append-only).
--   label_previews one row per preview: the captured parts and their digest, for one job and one profile; used_at and
--                  label_id once a label is recorded from it; holds_json, the hold_ids it captured, which that label
--                  acknowledges, for that label only (Codex H25).
--   hold_clearances a write-ahead hold kept beside the database (its commit failed) is cleared by recording its hold_id
--                  here in the transaction that stores its read; the file line is pruned after the commit (section 7).
--   meta           this schema, and the oldest engine that may open it: 17. An engine without P1 refuses a database
--                  holding P1's approvals (SchemaTooNew); migrate's own rule gives a schema with no RUNS_ON_OLDER entry
--                  itself as its floor.
ALTER TABLE jobs ADD COLUMN source_json TEXT;
ALTER TABLE gate_results ADD COLUMN source_digest TEXT;
ALTER TABLE packages ADD COLUMN files_json TEXT;
ALTER TABLE labels ADD COLUMN preview_id TEXT;
CREATE TABLE label_previews (
  preview_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  profile TEXT NOT NULL CHECK (profile IN ('primary','night')),
  digest TEXT NOT NULL,
  parts_json TEXT NOT NULL,
  holds_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  used_at TEXT,
  label_id TEXT
);
CREATE INDEX label_previews_job ON label_previews(job_id, created_at);
CREATE TABLE hold_clearances (
  hold_id TEXT PRIMARY KEY,
  job_id TEXT,
  cleared_at TEXT NOT NULL
);
INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '17');
INSERT OR REPLACE INTO meta(key, value) VALUES ('oldest_engine_schema', '17');
