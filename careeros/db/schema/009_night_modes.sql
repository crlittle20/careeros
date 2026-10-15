-- Prompts v2.2 NIGHT modes (2026-10-12). Each assessment is now SUPPLEMENTAL or REPLACEMENT, carries an eligibility
-- state and uses the A / B / near-miss / archive tiers. night_assessments (migration 007) allowed only the earlier
-- tier names, so results go to this new append-only table; the old one was never used live (NIGHT was off) and is
-- left in place untouched.
CREATE TABLE night_results (
  result_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('eligible','rejected')),
  state TEXT NOT NULL CHECK (state IN ('ELIGIBLE','CONDITIONAL','HOLD','INELIGIBLE')),
  mode TEXT CHECK (mode IN ('supplemental','replacement')),
  tier TEXT CHECK (tier IN ('A','B','near_miss','archive')),
  priority REAL,
  family TEXT,
  schedule TEXT,
  phone TEXT,
  failures_json TEXT NOT NULL,
  flags_json TEXT NOT NULL,
  components_json TEXT NOT NULL
);
CREATE INDEX night_results_job ON night_results(job_id, at);
CREATE TRIGGER night_results_no_update BEFORE UPDATE ON night_results BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER night_results_no_delete BEFORE DELETE ON night_results BEGIN SELECT RAISE(ABORT, 'append_only'); END;
