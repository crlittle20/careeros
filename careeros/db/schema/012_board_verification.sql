-- 2026-10-13 board verification: the employer's own website first, the ATS second.
--   employer_entities      one stable record per employer (never keyed by a name or a board token)
--   employer_aliases       rebuilt with two more kinds (former, legal) and an entity link; rows are kept
--   career_sites           the employer-controlled homepage / careers pages a verification actually read
--   board_mappings         + lifecycle (candidate -> quarantined -> active ...), trust / health scores, handshake,
--                            identity check, fingerprint, reverification schedule, migration and manual override
--   verification_runs      one row per verification / reverification / validation scan (append-only)
--   verification_evidence  the machine-readable evidence chain behind every decision (append-only)
--   board_reviews          why a board needs your review, until resolved
--   provider_status        Greenhouse / Lever / Ashby / Workable health as a whole (adapter degradation)
--   career_fit_history     how useful each employer has been over time (append-only)
-- Boards scanned before this migration are marked admitted_via = 'legacy': they keep being scanned while they are
-- re-verified, and are paused only by a concrete failure (see board_registry.scan_gate).

CREATE TABLE employer_entities (
  entity_id TEXT PRIMARY KEY,                       -- "ent:<hex>"
  canonical_name TEXT NOT NULL,
  canonical_norm TEXT NOT NULL,
  official_domain TEXT,
  domain_status TEXT NOT NULL DEFAULT 'unverified' CHECK (domain_status IN ('unverified','verified','failed')),
  domain_verified_at TEXT,
  primary_careers_url TEXT,
  parent_entity_id TEXT REFERENCES employer_entities(entity_id),
  career_fit_score INTEGER,
  career_priority TEXT CHECK (career_priority IN ('A','B','C','dormant')),
  first_seen_at TEXT NOT NULL,
  first_relevant_at TEXT,
  last_relevant_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX employer_entities_norm ON employer_entities(canonical_norm);      -- not unique: a shared name is not one company
CREATE INDEX employer_entities_domain ON employer_entities(official_domain);

CREATE TABLE employer_aliases_v12 (
  alias_norm TEXT NOT NULL,
  employer_norm TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('alias','parent','subsidiary','former','legal')),
  verified_by TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  evidence TEXT,
  entity_id TEXT,
  PRIMARY KEY (alias_norm, employer_norm, relation)
);
INSERT INTO employer_aliases_v12 (alias_norm, employer_norm, relation, verified_by, verified_at, evidence)
  SELECT alias_norm, employer_norm, relation, verified_by, verified_at, evidence FROM employer_aliases;
DROP TABLE employer_aliases;
ALTER TABLE employer_aliases_v12 RENAME TO employer_aliases;

CREATE TABLE career_sites (
  site_id TEXT PRIMARY KEY,                         -- "site:<hex>"
  entity_id TEXT NOT NULL REFERENCES employer_entities(entity_id),
  url TEXT NOT NULL,                                -- as observed
  canonical_url TEXT NOT NULL,
  site_type TEXT NOT NULL CHECK (site_type IN ('homepage','careers_page')),
  found_by TEXT,                                    -- homepage | homepage_link | common_path
  ats_seen TEXT NOT NULL DEFAULT '[]',              -- JSON: boards and systems this page referenced
  content_hash TEXT,
  verified_at TEXT,
  last_checked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','unavailable','changed')),
  UNIQUE (entity_id, canonical_url)
);

ALTER TABLE board_mappings ADD COLUMN entity_id TEXT;
ALTER TABLE board_mappings ADD COLUMN career_site_id TEXT;
ALTER TABLE board_mappings ADD COLUMN canonical_ats_url TEXT;
ALTER TABLE board_mappings ADD COLUMN board_fingerprint TEXT;
ALTER TABLE board_mappings ADD COLUMN identity_signals TEXT;          -- JSON baseline used to notice a board changing hands
ALTER TABLE board_mappings ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'candidate' CHECK (verification_status IN
  ('candidate','quarantined','active','manual_only','review_required','verification_required','unsupported','historical',
   'retired','rejected'));
ALTER TABLE board_mappings ADD COLUMN provenance TEXT NOT NULL DEFAULT 'discovery' CHECK (provenance IN
  ('legacy_unknown','discovery','job_link','human','official_site'));
ALTER TABLE board_mappings ADD COLUMN handshake_status TEXT NOT NULL DEFAULT 'unknown' CHECK (handshake_status IN ('confirmed','failed','unknown'));
ALTER TABLE board_mappings ADD COLUMN identity_check TEXT NOT NULL DEFAULT 'unresolved' CHECK (identity_check IN ('confirmed','unresolved','mismatch'));
ALTER TABLE board_mappings ADD COLUMN validation_status TEXT CHECK (validation_status IN ('pending','passed','failed'));
ALTER TABLE board_mappings ADD COLUMN board_trust_score INTEGER;
ALTER TABLE board_mappings ADD COLUMN board_health_score INTEGER;
ALTER TABLE board_mappings ADD COLUMN board_activity TEXT CHECK (board_activity IN ('active','empty','unavailable','unknown'));
ALTER TABLE board_mappings ADD COLUMN last_checked_at TEXT;
ALTER TABLE board_mappings ADD COLUMN reverify_at TEXT;
ALTER TABLE board_mappings ADD COLUMN quarantined_at TEXT;
ALTER TABLE board_mappings ADD COLUMN retired_at TEXT;
ALTER TABLE board_mappings ADD COLUMN migrated_to TEXT;
ALTER TABLE board_mappings ADD COLUMN paused_reason TEXT;             -- set = automatic ingestion paused, whatever the status
ALTER TABLE board_mappings ADD COLUMN verification_failure TEXT;
ALTER TABLE board_mappings ADD COLUMN verify_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE board_mappings ADD COLUMN site_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE board_mappings ADD COLUMN override_reason TEXT;
ALTER TABLE board_mappings ADD COLUMN override_by TEXT;
ALTER TABLE board_mappings ADD COLUMN override_at TEXT;
ALTER TABLE board_mappings ADD COLUMN override_prev_status TEXT;
ALTER TABLE board_mappings ADD COLUMN override_expires_at TEXT;
CREATE INDEX board_mappings_entity ON board_mappings(entity_id);
CREATE INDEX board_mappings_status ON board_mappings(verification_status);

-- Existing mappings: manual-only stays manual-only; a rejected one stays rejected; a board already in the daily scan
-- becomes a legacy board awaiting re-verification. Nothing is marked verified by this migration.
UPDATE board_mappings SET verification_status = 'manual_only' WHERE collection = 'manual_only';
UPDATE board_mappings SET verification_status = 'rejected' WHERE identity_status = 'rejected' AND collection = 'allowed';
UPDATE board_mappings SET verification_status = 'verification_required', provenance = 'legacy_unknown'
  WHERE activated_at IS NOT NULL AND collection = 'allowed' AND identity_status != 'rejected';

ALTER TABLE employers ADD COLUMN entity_id TEXT;
ALTER TABLE employers ADD COLUMN admitted_via TEXT;                   -- legacy | verified | override
UPDATE employers SET admitted_via = 'legacy';

CREATE TABLE verification_runs (
  run_id TEXT PRIMARY KEY,
  entity_id TEXT,
  mapping_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('initial','reverify','validation')),
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  failure_class TEXT,
  trust_score INTEGER,
  discovery_board TEXT,                             -- pass A: what discovery claimed ("greenhouse:foo"), if anything
  verifier_boards TEXT NOT NULL DEFAULT '[]',       -- pass B: what the employer's own site showed
  comparison TEXT CHECK (comparison IN ('match','mismatch','not_found','no_claim')),
  site_requests INTEGER NOT NULL DEFAULT 0,
  ats_requests INTEGER NOT NULL DEFAULT 0,
  detail TEXT
);
CREATE INDEX verification_runs_mapping ON verification_runs(mapping_id, finished_at);

CREATE TABLE verification_evidence (
  evidence_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  entity_id TEXT,
  mapping_id TEXT,
  evidence_type TEXT NOT NULL,
  source_url TEXT,
  destination_url TEXT,
  method TEXT,
  observed_at TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('pass','fail','info')),
  notes TEXT,
  points INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT
);
CREATE INDEX verification_evidence_mapping ON verification_evidence(mapping_id, observed_at);
CREATE INDEX verification_evidence_run ON verification_evidence(run_id);

CREATE TABLE board_reviews (
  case_id TEXT PRIMARY KEY,
  entity_id TEXT,
  mapping_id TEXT,
  reason_code TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  resolved_at TEXT,
  resolution TEXT
);
CREATE INDEX board_reviews_open ON board_reviews(status, mapping_id);

CREATE TABLE provider_status (
  provider TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('healthy','degraded','unavailable','unknown')),
  since TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE career_fit_history (
  history_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  at TEXT NOT NULL,
  score INTEGER NOT NULL,
  priority TEXT NOT NULL,
  stats_json TEXT NOT NULL
);
CREATE INDEX career_fit_history_entity ON career_fit_history(entity_id, at);

ALTER TABLE source_scans ADD COLUMN provider_degraded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN remote_scope TEXT;
ALTER TABLE jobs ADD COLUMN timezone_requirement TEXT;

CREATE TRIGGER verification_runs_no_update BEFORE UPDATE ON verification_runs BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER verification_runs_no_delete BEFORE DELETE ON verification_runs BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER verification_evidence_no_update BEFORE UPDATE ON verification_evidence BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER verification_evidence_no_delete BEFORE DELETE ON verification_evidence BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER career_fit_history_no_update BEFORE UPDATE ON career_fit_history BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER career_fit_history_no_delete BEFORE DELETE ON career_fit_history BEGIN SELECT RAISE(ABORT, 'append_only'); END;
