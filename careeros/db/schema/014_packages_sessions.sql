-- 2026-10-21 assisted applications (P2, ADR 0009). All four tables are append-only.
--   packages           one row per package version for a posting: what was prepared (manifest), for which route (your
--                      Apply or Stretch label, or the standing preparation policy) and its SHA-256. A change to any
--                      answer, fact, document, question or destination is a new version.
--   package_approvals  your approval of ONE package version (bound to its manifest hash), or a revoke of one.
--   assist_sessions    a reservation while you apply: one package, one role, one mode (manual, the employer's native
--                      autofill, or CareerOS assistance). It never submits anything.
--   assist_outcomes    how a session ended, by your account: submitted (with its evidence), not submitted, or
--                      uncertain. The latest one counts; an uncertain session blocks a new one for the role until a
--                      later outcome reconciles it. Measurements (minutes, corrections...) are kept for P3/P6.
CREATE TABLE packages (
  package_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  version INTEGER NOT NULL CHECK (version >= 1),
  route TEXT NOT NULL CHECK (route IN ('apply', 'stretch', 'standing')),
  basis TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  folder TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  UNIQUE (job_id, version)
);
CREATE TABLE package_approvals (
  approval_id TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('approve', 'revoke')),
  revokes TEXT REFERENCES package_approvals(approval_id),
  package_id TEXT NOT NULL REFERENCES packages(package_id),
  manifest_sha256 TEXT NOT NULL,
  batch TEXT,
  reason TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  CHECK ((action = 'revoke') = (revokes IS NOT NULL))
);
CREATE TABLE assist_sessions (
  session_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  package_id TEXT NOT NULL REFERENCES packages(package_id),
  manifest_sha256 TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('manual', 'native', 'assisted')),
  destination TEXT,
  role_json TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  expires_at TEXT,
  actor TEXT NOT NULL
);
CREATE TABLE assist_outcomes (
  outcome_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES assist_sessions(session_id),
  outcome TEXT NOT NULL CHECK (outcome IN ('submitted', 'not_submitted', 'uncertain')),
  evidence TEXT CHECK (evidence IN ('owner_attested', 'confirmation_observed')),
  application_id TEXT REFERENCES applications(application_id),
  minutes REAL CHECK (minutes IS NULL OR minutes >= 0),
  corrections INTEGER CHECK (corrections IS NULL OR corrections >= 0),
  custom_questions INTEGER CHECK (custom_questions IS NULL OR custom_questions >= 0),
  login_steps INTEGER CHECK (login_steps IS NULL OR login_steps >= 0),
  notes TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  CHECK ((outcome = 'submitted') = (evidence IS NOT NULL)),
  CHECK (outcome = 'submitted' OR application_id IS NULL)
);
CREATE INDEX packages_job ON packages(job_id, version);
CREATE INDEX package_approvals_package ON package_approvals(package_id);
CREATE INDEX assist_sessions_job ON assist_sessions(job_id);
CREATE INDEX assist_outcomes_session ON assist_outcomes(session_id);
CREATE TRIGGER packages_no_update BEFORE UPDATE ON packages BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER packages_no_delete BEFORE DELETE ON packages BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER package_approvals_no_update BEFORE UPDATE ON package_approvals BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER package_approvals_no_delete BEFORE DELETE ON package_approvals BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER assist_sessions_no_update BEFORE UPDATE ON assist_sessions BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER assist_sessions_no_delete BEFORE DELETE ON assist_sessions BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER assist_outcomes_no_update BEFORE UPDATE ON assist_outcomes BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER assist_outcomes_no_delete BEFORE DELETE ON assist_outcomes BEGIN SELECT RAISE(ABORT, 'append_only'); END;
