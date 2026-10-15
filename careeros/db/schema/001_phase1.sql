-- CareerOS Phase 1 schema. History tables are append-only (enforced by triggers).

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Factory Core 0.2.0 AuditEvent, hash-chained for tamper evidence.
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  contract_version TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  project_id TEXT NOT NULL,
  project_version TEXT NOT NULL,
  source TEXT NOT NULL,
  outcome TEXT NOT NULL,
  actor_type TEXT, actor_id TEXT, workflow_id TEXT, transition_id TEXT, correlation_id TEXT,
  entity_type TEXT, entity_id TEXT, previous_state TEXT, new_state TEXT, message TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  policy_fingerprint TEXT,
  exclude_from_kpi INTEGER NOT NULL DEFAULT 0,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE
);
CREATE INDEX events_entity ON events(entity_type, entity_id);

-- Factory Core 0.2.0 ExceptionRecord (current snapshot; changes are also logged as events).
CREATE TABLE exceptions (
  exception_id TEXT PRIMARY KEY,
  contract_version TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source TEXT NOT NULL,
  exception_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','investigating','resolved')),
  correlation_id TEXT, entity_type TEXT, entity_id TEXT, error_code TEXT,
  retryable INTEGER, retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  root_cause TEXT, resolution TEXT, resolved_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

-- Idempotency receipts: one key, one payload fingerprint.
CREATE TABLE receipts (
  receipt_key TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  payload_fingerprint TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  result_ref TEXT
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
  counts_json TEXT NOT NULL DEFAULT '{}',
  policy_fingerprint TEXT
);

CREATE TABLE employers (
  employer_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent TEXT NOT NULL,
  business_unit TEXT,
  ats TEXT NOT NULL,
  board_token TEXT NOT NULL,
  industry TEXT,
  status TEXT NOT NULL CHECK (status IN ('candidate','verified','invalid','disabled')),
  verified_at TEXT,
  last_polled_at TEXT,
  notes TEXT,
  UNIQUE (ats, board_token)
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL UNIQUE,
  employer_id TEXT REFERENCES employers(employer_id),
  employer_name TEXT NOT NULL,
  parent TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('ats_api','manual_url','fixture')),
  ats TEXT NOT NULL,
  external_id TEXT NOT NULL,
  requisition_id TEXT,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  family TEXT,
  location_raw TEXT,
  remote_type TEXT CHECK (remote_type IN ('remote','hybrid','onsite','unknown')),
  remote_states_json TEXT NOT NULL DEFAULT '[]',
  country TEXT,
  employment_type TEXT,
  comp_min REAL, comp_max REAL, comp_period TEXT, comp_currency TEXT,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  closed_at TEXT,
  url TEXT, apply_url TEXT, canonical_url TEXT,
  description_text TEXT NOT NULL DEFAULT '',
  content_fingerprint TEXT NOT NULL,
  requirements_fingerprint TEXT,
  extraction_fingerprint TEXT,
  dedupe_key TEXT,
  duplicate_of TEXT REFERENCES jobs(job_id),
  repost_of TEXT REFERENCES jobs(job_id),
  current_state TEXT NOT NULL,
  state_sequence INTEGER NOT NULL DEFAULT 0,
  entered_state_at TEXT NOT NULL,
  exclude_from_kpi INTEGER NOT NULL DEFAULT 0,
  UNIQUE (ats, employer_id, external_id)
);
CREATE INDEX jobs_state ON jobs(current_state);
CREATE INDEX jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX jobs_req ON jobs(employer_id, requisition_id);
CREATE INDEX jobs_canonical ON jobs(canonical_url);

CREATE TABLE job_snapshots (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  content_fingerprint TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  raw_path TEXT,
  PRIMARY KEY (job_id, content_fingerprint)
);

-- Factory Core 0.2.0 WorkflowTransition. Authoritative history; jobs.current_state is the projection.
CREATE TABLE transitions (
  transition_id TEXT NOT NULL UNIQUE,
  contract_version TEXT NOT NULL,
  project_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence >= 1),
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  transitioned_at TEXT NOT NULL,
  trigger_type TEXT NOT NULL CHECK (trigger_type IN ('human','system','agent','external')),
  trigger_id TEXT, reason_code TEXT, correlation_id TEXT, notes TEXT,
  PRIMARY KEY (workflow_id, sequence)
);

CREATE TABLE requirements (
  req_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  extraction_fingerprint TEXT NOT NULL,
  extractor TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  requirement_type TEXT NOT NULL CHECK (requirement_type IN
    ('hard_required','required_with_equivalency','preferred','responsibility','contextual','ambiguous')),
  years REAL,
  source_text TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  created_at TEXT NOT NULL
);
CREATE INDEX requirements_job ON requirements(job_id, extraction_fingerprint);

CREATE TABLE gate_results (
  result_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  policy_fingerprint TEXT NOT NULL,
  profile_fingerprint TEXT NOT NULL,
  extraction_fingerprint TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('eligible','rejected','needs_review')),
  hard_failures_json TEXT NOT NULL DEFAULT '[]',
  flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX gate_job ON gate_results(job_id, at);

CREATE TABLE scores (
  score_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  policy_fingerprint TEXT NOT NULL,
  profile_fingerprint TEXT NOT NULL,
  eligibility REAL, candidate_fit REAL, career_value REAL, freshness REAL, apply_priority REAL,
  tier TEXT NOT NULL CHECK (tier IN ('A','B','near_miss','archive')),
  interview_band TEXT NOT NULL CHECK (interview_band IN ('Very High','High','Moderate','Low')),
  components_json TEXT NOT NULL
);
CREATE INDEX scores_job ON scores(job_id, at);

CREATE TABLE labels (
  label_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  label TEXT NOT NULL CHECK (label IN ('apply','stretch','skip')),
  reason TEXT,
  system_tier TEXT,
  system_decision TEXT
);
CREATE INDEX labels_job ON labels(job_id, at);

-- Append-only enforcement.
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER transitions_no_update BEFORE UPDATE ON transitions BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER transitions_no_delete BEFORE DELETE ON transitions BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER requirements_no_update BEFORE UPDATE ON requirements BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER requirements_no_delete BEFORE DELETE ON requirements BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER gate_no_update BEFORE UPDATE ON gate_results BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER gate_no_delete BEFORE DELETE ON gate_results BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER scores_no_update BEFORE UPDATE ON scores BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER scores_no_delete BEFORE DELETE ON scores BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER labels_no_update BEFORE UPDATE ON labels BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER labels_no_delete BEFORE DELETE ON labels BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON job_snapshots BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON job_snapshots BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;

-- Contiguous transition sequence per workflow.
CREATE TRIGGER transitions_contiguous BEFORE INSERT ON transitions
WHEN NEW.sequence != COALESCE((SELECT MAX(sequence) FROM transitions WHERE workflow_id = NEW.workflow_id), 0) + 1
BEGIN SELECT RAISE(ABORT, 'sequence_not_contiguous'); END;

-- Workflow state columns change only together with a matching transition row.
CREATE TRIGGER jobs_state_guard BEFORE UPDATE OF current_state, state_sequence ON jobs
WHEN NEW.state_sequence != OLD.state_sequence + 1
  OR NOT EXISTS (SELECT 1 FROM transitions t WHERE t.workflow_id = NEW.workflow_id AND t.sequence = NEW.state_sequence
                 AND t.from_state = OLD.current_state AND t.to_state = NEW.current_state)
BEGIN SELECT RAISE(ABORT, 'state_change_without_transition'); END;
