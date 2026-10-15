-- The same requisition posted in several locations, or the same title with different text, is a variant
-- (evaluated separately, applied to at most once per requisition later), not a duplicate.
ALTER TABLE jobs ADD COLUMN variant_of TEXT REFERENCES jobs(job_id);
CREATE INDEX jobs_variant ON jobs(variant_of);
