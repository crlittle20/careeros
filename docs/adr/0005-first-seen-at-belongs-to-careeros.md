# ADR 0005: `first_seen_at` is CareerOS's own date

- Status: Accepted
- Decided: in the design since the first commit (2026-10-12); career fit and analytics rely on it explicitly
  (2026-10-13)
- Recorded: 2026-10-13
- Related: FR-022, NFR-003; `docs/data_lineage.md` section 4

## Context

Employer dates cannot be relied on:
- Some providers publish no posting date.
- Reposts reset the date, and "bumped" postings look new.
- The same job can carry different dates on different providers.
- Alert and recommendation dates belong to the sender, not the employer.

Several features need a date that CareerOS controls and can reproduce:
- "new since your last review";
- career-fit recency and volume;
- metric windows and the analytics' active date;
- which of two duplicates came first.

## Decision

- `jobs.first_seen_at` is **CareerOS's clock** at the moment CareerOS first recorded the job. It is set once, and no
  source ever overwrites it.
- `last_seen_at` and `closed_at` are also CareerOS's: a closure happens only after complete scans miss the job.
- The employer's dates (`posted_at`, `source_updated_at`) are kept beside them as **the employer's claim**. They drive
  freshness and the "may be stale" flag, which is shown, never used to hide a job.

## Consequences

**Gains:**
- Timelines are reproducible and auditable.
- Career fit cannot be inflated by date tricks.
- The analytics export's first-seen date is stable.

**Costs:**
- A job CareerOS finds late (for example on a newly verified board) is "new" to CareerOS even if it is old. Its
  posted date and the stale flag show this.
- Demo runs need a fixed clock to keep their dates repeatable, so the demo uses one.

## Alternatives considered

- **Use the employer's posted date everywhere.** Rejected: missing, resettable and inconsistent.
- **Use the earlier of the two dates.** Rejected: it imports the employer's errors into CareerOS's own timeline.

## Evidence

- Code: `pipeline.ingest` (sets `first_seen_at` once), `employer_intel.py`, `qualify/scores.py: freshness`,
  `review/report.py`.
- Tests: `test_meaningful_versus_cosmetic_and_timestamps`, `test_reposts_and_reopenings_keep_application_history`.
