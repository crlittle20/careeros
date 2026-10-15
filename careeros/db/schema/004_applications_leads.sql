-- Applications you have made (direct, through an agency, or by referral) and their outcomes, on the same audited
-- workflow machinery as jobs. Leads are job links found in alert emails you saved; CareerOS never opens links on
-- sites whose terms forbid automation, it only lists them for you.

CREATE TABLE applications (
  application_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL UNIQUE,
  job_id TEXT REFERENCES jobs(job_id),
  channel TEXT NOT NULL CHECK (channel IN ('direct','agency','referral','other')),
  agency TEXT,
  applied_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  current_state TEXT NOT NULL,
  state_sequence INTEGER NOT NULL DEFAULT 0,
  entered_state_at TEXT NOT NULL,
  notes TEXT,
  exclude_from_kpi INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX applications_job ON applications(job_id);

CREATE TRIGGER applications_state_guard BEFORE UPDATE OF current_state, state_sequence ON applications
WHEN NEW.state_sequence != OLD.state_sequence + 1
  OR NOT EXISTS (SELECT 1 FROM transitions t WHERE t.workflow_id = NEW.workflow_id AND t.sequence = NEW.state_sequence
                 AND t.from_state = OLD.current_state AND t.to_state = NEW.current_state)
BEGIN SELECT RAISE(ABORT, 'state_change_without_transition'); END;
CREATE TRIGGER applications_no_delete BEFORE DELETE ON applications BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;

CREATE TABLE leads (
  lead_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  site TEXT NOT NULL,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  family TEXT,
  context TEXT,
  sender TEXT,
  subject TEXT,
  received_at TEXT,
  first_seen_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('new','ingested','dismissed')),
  job_id TEXT REFERENCES jobs(job_id)
);
CREATE INDEX leads_status ON leads(status);
