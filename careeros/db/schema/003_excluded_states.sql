-- Remote postings sometimes list states they cannot hire in ("not eligible for hire in: AK, CA ..."), the opposite
-- of an allowed-states list. Stored separately so an exclusion list is never read as the only eligible states.
ALTER TABLE jobs ADD COLUMN remote_excluded_states_json TEXT NOT NULL DEFAULT '[]';
