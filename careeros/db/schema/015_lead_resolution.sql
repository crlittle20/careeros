-- 2026-11-02 lead resolution (the owner's decision D68; ADR 0003 and ADR 0001, amendments of 2026-11-02).
--   leads          two more states, each with its reason: no_match (the employer's admitted board was scanned completely
--                  and lists nothing matching; matched again on later scans) and closed (nothing matched for the closing
--                  window). status_reason / status_at say why and when the latest state was set; fetch_failure keeps
--                  why a lead's own link could not be read.
--   lead_matches   more decisions: likely (one near-identical posting, a candidate for the owner), awaiting_board (a
--                  posting matches on a board not yet admitted), and the owner's own records, confirmed / rejected (a
--                  likely candidate) and unlinked (a correction); with who decided (actor) and why (reason). Still
--                  append-only: a later decision is a new row, never an edit.
--   employer_entities  where the official-domain candidate came from (domain_origin: lead = only a lead's domain= hint;
--                  curated = only the curated list's or `careeros sources suggest`'s; both checked more strictly; owner =
--                  set by the job seeker) and when (domain_set_at). NULL = as before.
-- leads and lead_matches are rebuilt to widen their CHECK constraints (SQLite cannot alter one); every row is kept as it
-- was, the new columns empty.
CREATE TABLE leads_v15 (
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
  status TEXT NOT NULL CHECK (status IN ('new','ingested','dismissed','no_match','closed')),
  job_id TEXT REFERENCES jobs(job_id),
  profile TEXT NOT NULL DEFAULT 'primary' CHECK (profile IN ('primary','night')),
  status_reason TEXT,
  status_at TEXT,
  fetch_failure TEXT
);
INSERT INTO leads_v15 (lead_id, url, site, title, title_norm, family, context, sender, subject, received_at, first_seen_at,
                       status, job_id, profile)
  SELECT lead_id, url, site, title, title_norm, family, context, sender, subject, received_at, first_seen_at, status, job_id,
         profile FROM leads;
DROP TABLE leads;
ALTER TABLE leads_v15 RENAME TO leads;
CREATE INDEX leads_status ON leads(status);

CREATE TABLE lead_matches_v15 (
  match_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL,
  job_id TEXT,
  mapping_id TEXT,
  method TEXT NOT NULL CHECK (method IN ('requisition','canonical_link','title_location','likely_title','manual_intake','owner',
                                         'none')),
  decision TEXT NOT NULL CHECK (decision IN ('matched','ambiguous','no_match','posting_closed','likely','awaiting_board',
                                             'confirmed','rejected','unlinked')),
  candidates_json TEXT NOT NULL DEFAULT '[]',
  decided_at TEXT NOT NULL,
  audit TEXT CHECK (audit IN ('correct','wrong')),
  audited_at TEXT,
  actor TEXT,
  reason TEXT
);
INSERT INTO lead_matches_v15 (match_id, lead_id, job_id, mapping_id, method, decision, candidates_json, decided_at, audit, audited_at)
  SELECT match_id, lead_id, job_id, mapping_id, method, decision, candidates_json, decided_at, audit, audited_at FROM lead_matches;
DROP TABLE lead_matches;
ALTER TABLE lead_matches_v15 RENAME TO lead_matches;
CREATE INDEX lead_matches_lead ON lead_matches(lead_id);
CREATE TRIGGER lead_matches_no_delete BEFORE DELETE ON lead_matches BEGIN SELECT RAISE(ABORT, 'append_only'); END;

ALTER TABLE employer_entities ADD COLUMN domain_origin TEXT CHECK (domain_origin IS NULL OR domain_origin IN ('lead','curated','owner'));
ALTER TABLE employer_entities ADD COLUMN domain_set_at TEXT;
