-- Prompts v2.5 (2026-10-12): the Gmail status line of each inbox issue ("Gmail status: ok / partial / unavailable"),
-- so CareerOS can tell ChatGPT's next run to widen its Gmail lookback after an outage. Only the status word is kept:
-- never a Gmail message, ID, sender or address.
ALTER TABLE inbox_issues ADD COLUMN gmail_status TEXT CHECK (gmail_status IN ('ok','partial','unavailable'));
ALTER TABLE inbox_issues ADD COLUMN profile TEXT;
