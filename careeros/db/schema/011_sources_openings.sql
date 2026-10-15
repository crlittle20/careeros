-- 2026-10-12 source discovery and opportunity upgrades (all additive; existing rows and history are untouched).
--   board_mappings      employer identity <-> ATS board, with evidence, verifier and activation (Part 1, 6, 9)
--   employer_aliases    explicitly verified aliases and parent/subsidiary relationships (Part 1)
--   discovery_queue     employers waiting for discovery, one row per employer, resumable (Part 2, 9)
--   discovery_attempts  every discovery decision with its outcome and request cost (Part 1, 2)
--   lead_matches        lead -> posting matching, separate from identity, with an audit verdict (Part 1, 4)
--   title_reviews       second review of title-screen rejects: rescued or kept rejected, and why (Part 3)
--   title_audit         sampled title rejects with your verdict, to measure false rejections (Part 3)
--   observations        one row per source sighting of an opening, with provenance and evidence level (Part 4, 6)
--   opening_links       same opening / possible duplicate / possible repost relationships (Part 4)
--   dismissals          explicit dismissal reasons (Part 5)
--   job_changes         meaningful vs cosmetic posting changes (Part 7)
--   source_scans        every board scan's outcome, size and request cost (Part 8)

CREATE TABLE board_mappings (
  mapping_id TEXT PRIMARY KEY,                    -- "<ats>:<board token>"
  employer_norm TEXT NOT NULL,
  employer_name TEXT NOT NULL,
  official_domain TEXT,
  ats TEXT NOT NULL,
  board_token TEXT NOT NULL,
  board_url TEXT NOT NULL,
  collection TEXT NOT NULL CHECK (collection IN ('allowed','manual_only')),
  identity_status TEXT NOT NULL CHECK (identity_status IN ('verified','unresolved','rejected')),
  identity_evidence TEXT NOT NULL DEFAULT '[]',  -- JSON list of {kind, detail}
  verified_by TEXT, verified_method TEXT, verified_at TEXT,
  relationship TEXT NOT NULL DEFAULT 'employer' CHECK (relationship IN ('employer','staffing_agency')),
  origin TEXT NOT NULL,
  origin_ref TEXT,
  relevant_roles INTEGER,
  activated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX board_mappings_employer ON board_mappings(employer_norm);

CREATE TABLE employer_aliases (
  alias_norm TEXT NOT NULL,
  employer_norm TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('alias','parent','subsidiary')),
  verified_by TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  evidence TEXT,
  PRIMARY KEY (alias_norm, employer_norm, relation)
);

CREATE TABLE discovery_queue (
  employer_norm TEXT PRIMARY KEY,
  employer_name TEXT NOT NULL,
  official_domain TEXT,
  board_url TEXT,
  origin TEXT NOT NULL,                           -- lead | recommendation | alert | manual | curated | backfill
  priority REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  lead_ids_json TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','deferred','skipped')),
  last_outcome TEXT,
  next_attempt_at TEXT,
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE discovery_attempts (
  attempt_id TEXT PRIMARY KEY,
  employer_norm TEXT NOT NULL,
  ats TEXT,
  slug TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN ('slug_absent','board_empty','identity_unresolved','no_qualified_roles',
                                           'access_restricted','request_failed','verified','skipped_policy',
                                           'budget_exhausted','manual_only')),
  detail TEXT,
  requests INTEGER NOT NULL DEFAULT 0,
  origin TEXT,
  attempted_at TEXT NOT NULL,
  retry_after TEXT
);
CREATE INDEX discovery_attempts_employer ON discovery_attempts(employer_norm, attempted_at);

CREATE TABLE lead_matches (
  match_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL,
  job_id TEXT,
  mapping_id TEXT,
  method TEXT NOT NULL CHECK (method IN ('requisition','canonical_link','title_location','none')),
  decision TEXT NOT NULL CHECK (decision IN ('matched','ambiguous','no_match','posting_closed')),
  candidates_json TEXT NOT NULL DEFAULT '[]',
  decided_at TEXT NOT NULL,
  audit TEXT CHECK (audit IN ('correct','wrong')),
  audited_at TEXT
);
CREATE INDEX lead_matches_lead ON lead_matches(lead_id);

CREATE TABLE title_reviews (
  job_id TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('rescued','kept_rejected')),
  family TEXT,
  rule TEXT,
  evidence TEXT,
  original_reason TEXT,
  PRIMARY KEY (job_id, rules_version)
);

CREATE TABLE title_audit (
  job_id TEXT NOT NULL,
  sampled_at TEXT NOT NULL,
  reason TEXT,
  verdict TEXT CHECK (verdict IN ('should_include','correct_reject')),
  verdict_at TEXT,
  PRIMARY KEY (job_id, sampled_at)
);

CREATE TABLE observations (
  observation_id TEXT PRIMARY KEY,
  job_id TEXT,
  lead_id TEXT,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('ats_scan','recommendation','alert','manual_import','human_text')),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('live_ats','recommendation','alert','human')),
  posting_key TEXT,                               -- namespaced: greenhouse:<board>:<id>, linkedin:<id>, indeed:<jk>, ...
  requisition_id TEXT,
  url TEXT,
  canonical_url TEXT,
  title TEXT,
  company TEXT,
  location TEXT,
  pay_text TEXT,
  source_published_at TEXT,
  source_updated_at TEXT,
  observed_at TEXT NOT NULL,
  facts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX observations_job ON observations(job_id);
CREATE INDEX observations_lead ON observations(lead_id);

CREATE TABLE opening_links (
  job_id TEXT NOT NULL,
  other_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('same_opening','possible_duplicate','possible_repost')),
  evidence TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (job_id, other_id, relation)
);

CREATE TABLE dismissals (
  dismissal_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  reason_code TEXT NOT NULL,
  reason_text TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL
);

CREATE TABLE job_changes (
  change_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  at TEXT NOT NULL,
  meaningful INTEGER NOT NULL CHECK (meaningful IN (0, 1)),
  fields_json TEXT NOT NULL,
  summary TEXT
);
CREATE INDEX job_changes_job ON job_changes(job_id, at);

CREATE TABLE source_scans (
  scan_id TEXT PRIMARY KEY,
  employer_id TEXT NOT NULL,
  at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('complete','suspicious','failed','restricted','parse_error')),
  postings INTEGER,
  requests INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  possibly_absent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX source_scans_employer ON source_scans(employer_id, at);

ALTER TABLE jobs ADD COLUMN department TEXT;
ALTER TABLE jobs ADD COLUMN source_updated_at TEXT;
ALTER TABLE jobs ADD COLUMN absent_scans INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN deadline_at TEXT;
ALTER TABLE employers ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE employers ADD COLUMN last_qualified_at TEXT;
ALTER TABLE employers ADD COLUMN last_complete_scan_at TEXT;
ALTER TABLE employers ADD COLUMN last_posting_count INTEGER;
ALTER TABLE employers ADD COLUMN mapping_id TEXT;
ALTER TABLE employers ADD COLUMN last_evidence_at TEXT;   -- a new lead or recommendation linked this board

-- History tables are append-only.
CREATE TRIGGER discovery_attempts_no_update BEFORE UPDATE ON discovery_attempts BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER discovery_attempts_no_delete BEFORE DELETE ON discovery_attempts BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER dismissals_no_update BEFORE UPDATE ON dismissals BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER dismissals_no_delete BEFORE DELETE ON dismissals BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER job_changes_no_update BEFORE UPDATE ON job_changes BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER job_changes_no_delete BEFORE DELETE ON job_changes BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER source_scans_no_update BEFORE UPDATE ON source_scans BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER source_scans_no_delete BEFORE DELETE ON source_scans BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER title_reviews_no_update BEFORE UPDATE ON title_reviews BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER title_reviews_no_delete BEFORE DELETE ON title_reviews BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER lead_matches_no_delete BEFORE DELETE ON lead_matches BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER board_mappings_no_delete BEFORE DELETE ON board_mappings BEGIN SELECT RAISE(ABORT, 'immutable_table'); END;
