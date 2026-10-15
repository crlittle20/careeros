-- Prompts v2.2 (2026-10-12).
-- scores.state: ELIGIBLE / CONDITIONAL (material unknowns to check first) / HOLD (an employer-level hold such as D13).
--   Ineligible jobs are never scored. Older rows have NULL (scored before states existed).
-- applications.context_json: where the job came from and how CareerOS rated it when you applied, so hiring outcomes
--   can be measured by source, family, profile, tier, channel and domain.
-- labels.profile: PRIMARY and NIGHT acceptance are measured separately.
ALTER TABLE scores ADD COLUMN state TEXT CHECK (state IS NULL OR state IN ('ELIGIBLE','CONDITIONAL','HOLD'));
ALTER TABLE applications ADD COLUMN context_json TEXT;
ALTER TABLE labels ADD COLUMN profile TEXT NOT NULL DEFAULT 'primary' CHECK (profile IN ('primary','night'));
