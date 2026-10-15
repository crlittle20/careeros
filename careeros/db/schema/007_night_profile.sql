-- NIGHT / overnight profile (prompts v2.1): a separate, append-only assessment per job that never touches the PRIMARY
-- job workflow, gate results or scores. Leads remember which profile they were recommended for.
CREATE TABLE night_assessments (
  assessment_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('eligible','rejected')),
  tier TEXT CHECK (tier IN ('top','good','archive')),
  priority REAL,
  family TEXT,
  schedule TEXT,
  phone TEXT,
  failures_json TEXT NOT NULL,
  flags_json TEXT NOT NULL,
  components_json TEXT NOT NULL
);
CREATE INDEX night_assessments_job ON night_assessments(job_id, at);
CREATE TRIGGER night_assessments_no_update BEFORE UPDATE ON night_assessments BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER night_assessments_no_delete BEFORE DELETE ON night_assessments BEGIN SELECT RAISE(ABORT, 'append_only'); END;

ALTER TABLE leads ADD COLUMN profile TEXT NOT NULL DEFAULT 'primary' CHECK (profile IN ('primary','night'));
