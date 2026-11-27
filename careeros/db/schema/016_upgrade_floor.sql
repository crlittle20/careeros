-- 2026-11-20 (the owner's D191 and D192, after Codex's design review of D190, H9).
--   jobs           the board's own workplace hint (workplace_hint: "Remote", "Hybrid", "On-site", as the board gives it)
--                  and its structured pay (board_pay_json: the board's own pay fields as it gives them, canonical JSON),
--                  stored with the job as the board gives them. NULL = the board gives none, or no read since 016.
--                  typed_countries_json: the countries the board types for the posting and its places, written out
--                  (a JSON list); placement reads them with the location field (part 4 of the owner's D192 build).
--   alert_notices  one row per alert card whose own link names no posting (a careers page, a search page): a notice,
--                  never a lead (D190 rule 3; the D190 design, revision 3, section 5; part 3 of the owner's D192 build).
--                  notice_id is the card's occurrence key (its email's key and its position), so reading the same email
--                  again adds nothing and no other card is ever merged into it. employer_key: the employer the card
--                  names, when it resolves (employer_identity), by which notices are shown grouped, else by sender.
--                  suggestion_json: advice only (the Greenhouse posting a careers page's gh_jid may name; never
--                  fetched). state: new, dismissed (by you) or added (you added the posting: job_id). Outside the leads
--                  table, lead resolution and the lead counts by construction.
--   leads          repeat_of: an earlier lead with the same agency and job number, shown as a possible repeat in either
--                  arrival order (advice: never "seen", never closed; Codex's D190 design review H8).
--   meta           this schema, and the oldest engine that may open it: 16. An engine older than 016 refuses this
--                  database (SchemaTooNew), so a rollback across 016 is a restore of the backup made before it.
--                  Written here, inside this migration's own transaction, so a stop after its COMMIT never leaves the
--                  new schema under the old markers. Batch 3's writes-mark keys (B.6, retired) and any handoff owed
--                  (retired with browser Fill paused) are removed.
ALTER TABLE jobs ADD COLUMN workplace_hint TEXT;
ALTER TABLE jobs ADD COLUMN board_pay_json TEXT;
ALTER TABLE jobs ADD COLUMN typed_countries_json TEXT;
CREATE TABLE alert_notices (
  notice_id TEXT PRIMARY KEY,
  message_key TEXT NOT NULL,
  position TEXT NOT NULL,
  url TEXT NOT NULL,
  site TEXT NOT NULL,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  family TEXT,
  context TEXT,
  sender TEXT,
  subject TEXT,
  received_at TEXT,
  suggested_employer TEXT,
  employer_key TEXT,
  suggestion_json TEXT NOT NULL DEFAULT '{}',
  profile TEXT NOT NULL DEFAULT 'primary' CHECK (profile IN ('primary','night')),
  first_seen_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','dismissed','added')),
  state_at TEXT,
  job_id TEXT
);
CREATE INDEX alert_notices_state ON alert_notices(state);
ALTER TABLE leads ADD COLUMN repeat_of TEXT;
DELETE FROM meta WHERE key IN ('writes_mark', 'labels_reconfirm_seq', 'foreign_writes_checked_at') OR key LIKE 'handoff_owed:%';
INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '16');
INSERT OR REPLACE INTO meta(key, value) VALUES ('oldest_engine_schema', '16');
