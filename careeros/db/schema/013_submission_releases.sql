-- 2026-10-16 submission permission (R2): your releases of a submission hold, one posting and one channel at a time.
--   submission_releases  append-only: a 'release' row lets ONE hold be cleared for ONE posting through ONE channel (and,
--                        for an agency, ONE agency); a 'revoke' row withdraws an earlier release. Each release keeps what
--                        it was granted under (the employer identity and the posting's facts), so a later material change
--                        or an identity correction suspends it. Nothing else in CareerOS writes or reads a release as
--                        permission to qualify a job: eligibility and scores never read this table.
CREATE TABLE submission_releases (
  release_id TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('release', 'revoke')),
  revokes TEXT REFERENCES submission_releases(release_id),
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  hold_id TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('direct', 'agency', 'referral', 'other')),
  agency_norm TEXT NOT NULL DEFAULT '',
  agency TEXT,
  reason TEXT NOT NULL,
  family_key TEXT,
  identity_json TEXT,
  content_fingerprint TEXT,
  requisition_id TEXT,
  canonical_url TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  CHECK ((action = 'revoke') = (revokes IS NOT NULL)),
  CHECK ((channel = 'agency') = (agency_norm != ''))
);
CREATE INDEX submission_releases_job ON submission_releases(job_id, hold_id, channel, agency_norm);
CREATE TRIGGER submission_releases_no_update BEFORE UPDATE ON submission_releases BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER submission_releases_no_delete BEFORE DELETE ON submission_releases BEGIN SELECT RAISE(ABORT, 'append_only'); END;
