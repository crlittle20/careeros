# CareerOS: analytics data dictionary

- Version: 1.0 (schema `analytics-1`)
- Date: 2026-10-13
- Code: `careeros/analytics/export.py`
- Command: `careeros export [--out FOLDER]`
- Demo sample: `samples/analytics_demo/` (rebuilt by `tools/build_demo_export.py`)
- Companion: `powerbi_dashboard_spec.md` (the report built on these tables).

## 1. What the export is

`careeros export` turns the CareerOS database into a small **star schema** for Power BI. It writes 13 CSV files and a
`manifest.json`:
- 8 dimensions;
- 5 facts.

The export **only reads** the database. It reuses the existing data model: every column comes from an existing table
or from a rule the application already applies (for example the tier thresholds or pay annualisation). No database
views are added.

```
                     dim_date (role-playing: first seen, decided, scan, applied ...)
                        |
 dim_employer --+       |       +-- dim_role_family
 dim_board -----+-- fact_job_evaluation --+-- dim_location
 dim_ats -------+   (one row per job)     +-- dim_source (discovered via)
                        |
                     dim_job
                        |
     fact_job_observation (one row per sighting)     fact_application (one row per application)
     fact_board_scan (one row per board scan)        fact_verification (one row per verification run)
```

**Where it is written:**
- By default, to `exports\analytics` in the vault.
- With Demo Mode on, that is the demo vault, and the export may not leave it.
- A private export (your real data) is refused inside the code repository.
- The export only replaces a folder that holds nothing but an earlier export.

**Which data:** the manifest's `source` is `demo` for a Demo Mode export (synthetic data only) and `private`
otherwise. Only demo exports are committed (`samples/analytics_demo/`). Private exports stay in your vault.

**Why CSV:** Power BI reads CSV natively. Parquet would need a new dependency (pyarrow) for 13 small tables. Database
views would tie Power BI to a SQLite ODBC driver and to the live database file. Both are recorded as options (section
8).

## 2. Conventions

**Keys.**
- Dimension keys are natural, readable and stable:
  - the job ID (`greenhouse:meridianrx:101`);
  - the board mapping (`lever:bluepeakhealth`);
  - the employer's normalised name (`meridian rx`);
  - the provider (`greenhouse`), the source kind, the role-family code, and a location signature;
  - the date as `YYYYMMDD`.
- Event facts use a sequence number in recording order (`scan_key`, `observation_key`, ...).
- Random record IDs (run, scan, case, application IDs) are never exported.
- As a result, one database always gives byte-identical files.

**The `(none)` member.** Every dimension has a `(none)` row. A fact or dimension reference with nothing to point at
(a pasted job with no board, an alert link to no job) points there. Dimension keys are therefore never empty, and
every relationship is complete.

**Null handling.** A missing *measure* stays empty, never 0. Examples:
- a rejected job has no fit score;
- a posting without pay has no pay range;
- a board never verified has no trust score.

A missing *date* (a job not closed, an application not yet changed) leaves its date key empty. Power BI treats an empty
cell as BLANK, which `AVERAGE` and `SUM` skip, so averages describe only rows that have the value. Counts use the 0/1
flags, which are never empty.

**Types.**

| Type in the tables below | In the CSV | Power BI type |
|---|---|---|
| text, key | text | Text |
| whole number | integer | Whole number |
| decimal | `.` decimal separator, no thousands separator | Decimal number |
| 0/1 | `0` or `1` | Whole number (sum it to count) |
| date | `YYYY-MM-DD` | Date |

**CSV format:** UTF-8 with a byte-order mark (Power BI and Excel then read it as UTF-8), comma-separated, CRLF line
ends, a header row, and values quoted only when needed.

**Dates:** see section 4.

**Left out.** Rows marked `exclude_from_kpi` (drills and test data) are left out, together with their sightings and
applications. This is the same rule as requirement AUD-006.

## 3. Relationships

All relationships are many-to-one, from the fact (many) to the dimension (one), with a single filter direction.

| From (many) | Column | To (one) | Active |
|---|---|---|---|
| fact_job_evaluation | job_key | dim_job | yes |
| fact_job_evaluation | employer_key | dim_employer | yes |
| fact_job_evaluation | board_key | dim_board | yes |
| fact_job_evaluation | ats_key | dim_ats | yes |
| fact_job_evaluation | role_family_key | dim_role_family | yes |
| fact_job_evaluation | location_key | dim_location | yes |
| fact_job_evaluation | discovery_source_key | dim_source | yes |
| fact_job_evaluation | first_seen_date_key | dim_date | yes |
| fact_job_evaluation | decided_date_key, posted_date_key, last_seen_date_key, closed_date_key, label_date_key | dim_date | no (`USERELATIONSHIP`) |
| fact_job_observation | job_key, employer_key, ats_key, source_key | dim_job, dim_employer, dim_ats, dim_source | yes |
| fact_job_observation | observed_date_key | dim_date | yes |
| fact_board_scan | board_key, employer_key, ats_key | dim_board, dim_employer, dim_ats | yes |
| fact_board_scan | scan_date_key | dim_date | yes |
| fact_verification | board_key, employer_key, ats_key | dim_board, dim_employer, dim_ats | yes |
| fact_verification | finished_date_key | dim_date | yes |
| fact_verification | started_date_key | dim_date | no |
| fact_application | job_key, employer_key, ats_key, role_family_key | dim_job, dim_employer, dim_ats, dim_role_family | yes |
| fact_application | applied_date_key | dim_date | yes |
| fact_application | last_change_date_key | dim_date | no |

Two things are deliberately **not** relationships:
- `dim_board` → `dim_employer`. Linking them would make two paths from a fact to `dim_employer`. Each fact carries
  `employer_key` itself instead.
- The self-references in `dim_job` (`duplicate_of_job_key` and similar). They are attributes for drill-through.

## 4. Date logic

- **Every date is a UTC calendar date**, taken from the UTC timestamps the database keeps.
  - A run's records carry the UTC date on which the run happened.
  - Something you record yourself late in a U.S. evening can carry the next UTC date.
- **`dim_date`** has one row per day, with no gaps. It runs from the earliest to the latest date used anywhere in the
  export, so it always covers the data and needs no maintenance.
- **Role-playing dates.** `fact_job_evaluation` has six dates. *First seen* (when CareerOS first recorded the job) is
  the active relationship. The others are reached with `USERELATIONSHIP` in a measure:
  - *posted*: what the employer states;
  - *decided*: the latest eligibility decision;
  - *last seen*;
  - *closed*;
  - *labelled*.
- **`days_to_decision`** and **`days_listed`** are in days, from timestamps, to two decimals. Days listed runs from
  first seen to closed, or to last seen for a job still listed.
- **Snapshot.** `fact_job_evaluation` is a snapshot: each job's state and decision at export time. The event facts
  (sightings, scans, verifications) carry the history.

## 5. Tables

### dim_date

Grain: one row per calendar day. Source: generated (section 4). Demo rows: 9.

| Column | Type | Description |
|---|---|---|
| `date_key` | whole number | `YYYYMMDD`; primary key. |
| `date` | date | The day. Mark the table as a date table on this column. |
| `year` | whole number | Calendar year. |
| `quarter` | whole number | 1-4. |
| `month` | whole number | 1-12; sort `month_name` by it. |
| `month_name` | text | January ... December. |
| `month_start` | date | First day of the month. |
| `iso_week` | whole number | ISO 8601 week number. |
| `week_start` | date | The Monday of the week. |
| `day_of_week` | whole number | 1 = Monday ... 7 = Sunday; sort `day_name` by it. |
| `day_name` | text | Monday ... Sunday. |
| `is_weekend` | 0/1 | Saturday or Sunday. |

### dim_employer

Grain: one row per employer record (`employer_entities`), plus one row per employer name seen only on a pasted or
imported posting, plus `(none)`. Demo rows: 14.

| Column | Type | Description |
|---|---|---|
| `employer_key` | text (key) | The employer's normalised name. `unmatched:<name>` for an employer known only from a posting; `(none)`. |
| `employer_name` | text | Display name. |
| `official_domain` | text | The employer's own website, when known. |
| `domain_status` | text | `verified` or `unverified`: whether that website has been confirmed. |
| `career_fit_score` | decimal | 0-100: how much relevant hiring the employer shows (from board verification). Empty until measured. |
| `career_priority` | text | `A`, `B`, `C`, `dormant` or empty: the scan priority derived from career fit. |
| `boards_total` | whole number | Job boards on record for this employer. |
| `boards_active` | whole number | Of those, boards board verification has admitted (`active`). |
| `is_verified_employer` | 0/1 | At least one active board. |
| `employer_origin` | text | `board` (has a board on record), `no board`, `posting only`, `none`. |
| `first_seen_date_key` | whole number | When the employer record was created. |

### dim_board

Grain: one row per job-board mapping (`board_mappings`), plus `(none)`. Demo rows: 14.

| Column | Type | Description |
|---|---|---|
| `board_key` | text (key) | `provider:token`, for example `lever:bluepeakhealth`. Manual-only systems use their host. |
| `employer_key` | text | The employer the board belongs to (an attribute; see section 3). |
| `ats_key` | text | The provider. |
| `board_token` | text | The board's name at the provider. |
| `collection` | text | `allowed` (CareerOS may read it automatically) or `manual_only`. |
| `verification_status` | text | `active`, `quarantined`, `verification_required`, `review_required`, `rejected`, `historical`, `retired`, `manual_only` ... |
| `provenance` | text | How the board was found: `legacy_unknown` (before verification existed), `discovery`, `official_site`, `job_link` ... |
| `is_legacy` | 0/1 | Found before board verification existed. |
| `handshake_status` | text | Whether the employer's own site links to this board: `confirmed`, `failed`, `unknown`. |
| `identity_check` | text | `confirmed`, `mismatch` (the board names another company) or `unresolved`. |
| `validation_status` | text | Result of the validation scan a new board must pass (`passed`). |
| `board_trust_score` | decimal | 0-100. At least 85 is needed for automatic scanning. |
| `board_health_score` | decimal | 0-100: recent scan success. Outages lower it; trust is never lowered by an outage. |
| `board_activity` | text | `active` (lists jobs), `empty`, `unavailable`. |
| `verify_count` | whole number | Verification runs so far. |
| `is_paused` | 0/1 | Scanning is paused. |
| `paused_reason` | text | Why. |
| `migrated_to_board_key` | text | The board this one moved to (ATS migration); empty otherwise. |
| `created_date_key` | whole number | Board first recorded. |
| `activated_date_key` | whole number | Admitted to the daily scan. |
| `last_checked_date_key` | whole number | Last verification check. |
| `retired_date_key` | whole number | Retired (for example after a migration). |

### dim_ats

Grain: one row per job-board provider or site seen in the data (plus the four supported providers always), plus
`(none)`. Demo rows: 9.

| Column | Type | Description |
|---|---|---|
| `ats_key` | text (key) | Provider code: `greenhouse`, `lever`, `ashby`, `workable`, `workday`, `icims`, `linkedin`, `manual` (pasted or imported) ... |
| `ats_name` | text | Display name. |
| `collection_mode` | text | From `config/site_policy.yaml`: `allowed` (read automatically), `manual` (listed for you only), `blocked`, or `you` (you pasted or imported it). |
| `is_automated` | 0/1 | CareerOS reads this provider by itself. |

### dim_source

Grain: one row per way a posting reaches CareerOS (an observation's `source_kind`), plus `(none)`. Demo rows: 6.

| Column | Type | Description |
|---|---|---|
| `source_key` | text (key) | `ats_scan`, `alert`, `recommendation`, `manual_import`, `human_text`. |
| `source_name` | text | Display name. |
| `channel` | text | `automatic`, `email`, `recommendation`, `you`. |
| `is_automated` | 0/1 | Arrives without you doing anything. |

### dim_role_family

Grain: one row per target role family in the policy (in the policy's order), plus any family found on a job but not
in the policy, plus `(none)` (no target family). Demo rows: 6.

| Column | Type | Description |
|---|---|---|
| `role_family_key` | text (key) | Family code from the policy, for example `pbm_claims_operations`; `(none)`. |
| `family_name` | text | The policy's label, for example "PBM / claims / benefits operations". |
| `is_target` | 0/1 | A family you target (0 only for `(none)`). |
| `sort_order` | whole number | The policy's order; sort `family_name` by it. |

### dim_location

Grain: one row per distinct combination of remote scope, country and state limits, plus `(none)`. Demo rows: 8.

| Column | Type | Description |
|---|---|---|
| `location_key` | text (key) | Remote scope, country, allowed states and excluded states joined by vertical bars, for example `US_restricted_states` + `US` + `AZ,CO,TX` + (none). |
| `remote_scope` | text | `US_nationwide`, `US_restricted_states`, `state_specific`, `US_unspecified`, `hybrid`, `onsite`, `international`, `unknown`. "Remote" alone is never read as nationwide. |
| `scope_label` | text | Display label, for example "Remote: limited US states". |
| `work_arrangement` | text | `Remote`, `Hybrid`, `On-site`, `International`, `Unknown`. |
| `is_remote` | 0/1 | Remote work. |
| `is_us` | 0/1 | In the US. |
| `country` | text | `US`, `non_US` or `unknown`. |
| `allowed_states` | text | States a restricted remote role is open to, comma-separated. |
| `excluded_states` | text | States it is closed to. |
| `sort_order` | whole number | Sort `scope_label` by it (nationwide first). |

### dim_job

Grain: one row per job (`jobs`), plus `(none)`. Descriptive attributes only; the decision is in
`fact_job_evaluation`. Demo rows: 46.

| Column | Type | Description |
|---|---|---|
| `job_key` | text (key) | `provider:board:posting`, or `manual:...` for a pasted posting. |
| `title` | text | The title as posted. |
| `title_norm` | text | Lower-case, trimmed title used for matching. |
| `posted_employer_name` | text | The employer name on the posting. |
| `department` | text | As posted. |
| `requisition_id` | text | The employer's requisition number, when published. |
| `employment_type` | text | `full_time`, `part_time`, `contract`, `unknown` ... |
| `location_raw` | text | The location line as posted. |
| `timezone_requirement` | text | A required working time zone, for example `Eastern`. |
| `pay_min` | decimal | Lowest stated pay, in `pay_period` units. |
| `pay_max` | decimal | Highest stated pay. |
| `pay_period` | text | `year`, `hour`, `day`, `week`, `month`. |
| `pay_currency` | text | Usually `USD`. |
| `annual_pay_min` | decimal | `pay_min` as a yearly amount: hour × 2,080, day × 260, week × 52, month × 12 (the gate's rule). |
| `annual_pay_max` | decimal | `pay_max` as a yearly amount. |
| `is_pay_stated` | 0/1 | The posting states pay. |
| `posted_date` | date | When the employer says it was posted. |
| `deadline_date` | date | Application deadline, when stated. |
| `url` | text | The posting's link. |
| `duplicate_of_job_key` | text | The job this one duplicates (same requisition, link or text). |
| `variant_of_job_key` | text | The job this one is a variant of (same requisition elsewhere). |
| `repost_of_job_key` | text | The earlier, closed posting this one re-posts. |

### fact_job_evaluation

Grain: **one row per job**, describing its current state and latest decision (a snapshot at export time). Source:
`jobs`, plus the latest `gate_results`, `scores`, `night_results` and primary label, the latest transition, and counts
from `observations`, `job_changes`, `opening_links` and `gate_results`. Demo rows: 45.

| Column | Type | Description |
|---|---|---|
| `job_key` | text | → dim_job. |
| `employer_key` | text | → dim_employer. |
| `board_key` | text | → dim_board; `(none)` for a pasted posting. |
| `ats_key` | text | → dim_ats. |
| `role_family_key` | text | → dim_role_family; `(none)` when the title matched no target family. |
| `location_key` | text | → dim_location. |
| `discovery_source_key` | text | → dim_source: how CareerOS first learned of the job. When an alert or recommendation led to a single board read at the same moment, the alert counts. |
| `first_seen_date_key` | whole number | First recorded (active date). |
| `posted_date_key` | whole number | Employer's posting date. |
| `last_seen_date_key` | whole number | Last seen listed. |
| `decided_date_key` | whole number | Latest eligibility decision. |
| `closed_date_key` | whole number | Closed (no longer listed, or its board retired). |
| `current_state` | text | `scored`, `rejected`, `needs_review`, `duplicate`, `closed`, ... (the job workflow). |
| `state_reason` | text | Reason code of the job's latest state change. |
| `rejection_reason` | text | For rejected jobs: the reason code, for example `below_pay_floor`. |
| `rejection_group` | text | For rejected jobs: `title`, `location`, `pay`, `requirements`, `employment type`, `safety`, `employer`, `other`. |
| `decision_stage` | text | Where the decision was taken: `title_screen`, `eligibility_rules`, `deduplication`. |
| `gate_decision` | text | Latest eligibility decision: `eligible`, `rejected`, `needs_review`. |
| `eligibility_state` | text | For scored jobs: `ELIGIBLE`, `CONDITIONAL` (something material is unknown), `HOLD`. |
| `tier` | text | `A`, `B`, `near_miss`, `archive`; empty unless the latest decision produced a score. |
| `is_evaluated` | 0/1 | Has a current decision: scored, rejected or needs review. |
| `is_scored` | 0/1 | Passed eligibility and is scored. |
| `is_qualified` | 0/1 | Scored, tier A, B or near-miss, and not a variant (the application's "qualified openings"). |
| `is_recommended` | 0/1 | Scored, tier A or B. |
| `is_manual_review` | 0/1 | Needs a person's judgement: tier near-miss ("manual review") or state needs_review. |
| `is_rejected` | 0/1 | Rejected. |
| `is_duplicate` | 0/1 | A duplicate of another job (not reviewed again). |
| `is_closed` | 0/1 | Closed. |
| `is_variant` | 0/1 | A variant of another job (same requisition elsewhere). |
| `is_repost` | 0/1 | Re-posts an earlier, closed posting. |
| `linked_openings` | whole number | Links recorded from this job to another posting of the same opening (for example a re-post after an ATS migration). |
| `eligibility_score` | decimal | 0-100. |
| `candidate_fit` | decimal | 0-100: how well you match the role. |
| `career_value` | decimal | 0-100: pay against your target, progression, domain, stability. |
| `freshness` | decimal | 0-100: how recently the job was posted. |
| `apply_priority` | decimal | 0-100: 0.65 × fit + 0.25 × career value + 0.10 × freshness (weights from the policy). |
| `skill_coverage` | decimal | 0-1: weighted share of the required skills you have. |
| `interview_band` | text | `Very High`, `High`, `Moderate`, `Low`. |
| `decisive_unknowns` | whole number | Facts to check before applying (for example pay not stated). |
| `night_decision` | text | NIGHT profile: `eligible` or `rejected`. |
| `night_mode` | text | `supplemental` or `replacement`. |
| `night_state` | text | `ELIGIBLE`, `CONDITIONAL`, `INELIGIBLE`. |
| `label` | text | Your latest label: `apply`, `stretch`, `skip`. |
| `label_date_key` | whole number | When you labelled it. |
| `has_application` | 0/1 | You applied (an application is on record). |
| `sightings` | whole number | Recorded sightings (rows in fact_job_observation). |
| `source_count` | whole number | Distinct sources it was seen through. |
| `meaningful_changes` | whole number | Meaningful posting changes (pay, requirements, location ...); cosmetic edits are not counted. |
| `evaluations` | whole number | Eligibility decisions made over the job's life (re-evaluations included). |
| `days_to_decision` | decimal | First seen → latest decision, in days. |
| `days_listed` | decimal | First seen → closed (or last seen), in days. |

**A note on scores.** The score columns, and `tier`, are filled only while the job's latest decision is the one the
score was made under. A job that was scored and later rejected shows no score. A job that was scored and then closed
keeps its last score, but is not counted as qualified.

### fact_job_observation

Grain: **one row per recorded sighting** of a posting: a new job, a meaningful change, or the same job reached through
another source. Source: `observations`. Demo rows: 50.

| Column | Type | Description |
|---|---|---|
| `observation_key` | whole number | Sequence in recording order; primary key. |
| `job_key` | text | → dim_job; `(none)` for an alert link CareerOS may not read (for example LinkedIn). |
| `employer_key` | text | → dim_employer. |
| `ats_key` | text | → dim_ats: the job's provider, or the alert link's site. |
| `source_key` | text | → dim_source. |
| `evidence_level` | text | `live_ats` (read from the employer's board), `alert`, `human` ... |
| `observed_date_key` | whole number | → dim_date. |
| `is_first_sighting` | 0/1 | The job's first sighting: the moment it was discovered. |
| `is_cross_source` | 0/1 | Seen through a source the job had not been seen through before: a duplicate merged, not reviewed twice. |
| `has_lead` | 0/1 | Came with an alert or recommendation lead. |

### fact_board_scan

Grain: **one row per scan of an employer's board** in a daily run. Source: `source_scans`. Demo rows: 27.

| Column | Type | Description |
|---|---|---|
| `scan_key` | whole number | Sequence; primary key. |
| `board_key` | text | → dim_board. |
| `employer_key` | text | → dim_employer. |
| `ats_key` | text | → dim_ats. |
| `scan_date_key` | whole number | → dim_date. |
| `outcome` | text | `complete`, `suspicious` (far fewer postings than usual: kept, nothing closed), `restricted` (401/403), `failed`, `parse_error`. |
| `is_complete` | 0/1 | The scan read the whole board. |
| `is_failure` | 0/1 | `failed` or `parse_error`. |
| `error_code` | text | For example `http_503`, `invalid_json`, `rate_limited`, `not_found`. |
| `postings` | whole number | Postings listed (complete scans). |
| `requests` | whole number | Requests sent. |
| `jobs_closed` | whole number | Jobs this scan closed (only complete scans close anything). |
| `possibly_absent` | whole number | Jobs missing from this scan but not yet closed. |
| `provider_degraded` | 0/1 | The provider as a whole was failing at the time; not held against the employer. |

### fact_verification

Grain: **one row per board verification run**: an initial check, a re-verification or a validation scan. Source:
`verification_runs` and `verification_evidence`. Demo rows: 14.

| Column | Type | Description |
|---|---|---|
| `verification_key` | whole number | Sequence; primary key. |
| `board_key` | text | → dim_board. |
| `employer_key` | text | → dim_employer. |
| `ats_key` | text | → dim_ats. |
| `kind` | text | `initial`, `reverify`, `validation`. |
| `started_date_key` | whole number | → dim_date (inactive). |
| `finished_date_key` | whole number | → dim_date (active). |
| `outcome` | text | `verified`, `active`, `review_required`, `no_handshake`, `rejected` ... |
| `is_pass` | 0/1 | `verified`, `active` or `validated`. |
| `failure_class` | text | Why it did not pass. |
| `trust_score` | decimal | Board trust at the end of the run. |
| `site_requests` | whole number | Pages read on the employer's own site. |
| `ats_requests` | whole number | Requests to the job-board provider. |
| `evidence_items` | whole number | Evidence records collected. |
| `evidence_points` | decimal | Trust points those records carried. |

### fact_application

Grain: **one row per application you recorded**, with its current status. Source: `applications` and the
application workflow's transitions. Demo rows: 4.

| Column | Type | Description |
|---|---|---|
| `application_key` | whole number | Sequence by application date; primary key. |
| `job_key` | text | → dim_job. |
| `employer_key` | text | → dim_employer. |
| `ats_key` | text | → dim_ats. |
| `role_family_key` | text | → dim_role_family. |
| `channel` | text | `direct`, `agency`, `referral`, `other`. |
| `applied_date_key` | whole number | → dim_date (active). |
| `last_change_date_key` | whole number | → dim_date (inactive): the latest status change. |
| `current_state` | text | `applied`, `acknowledged`, `responded`, `screening`, `interviewing`, `final_interview`, `offer`, `accepted`, `declined`, `rejected`, `withdrawn`, `ghosted`, `closed`. |
| `state_category` | text | `active`, `blocked`, `completed`, `cancelled` (from the application workflow). |
| `is_open` | 0/1 | Active or blocked. |
| `got_answer` | 0/1 | The employer answered at some point (responded, screening, interview, offer or rejection). |
| `reached_screening` | 0/1 | Reached screening or later. |
| `reached_interview` | 0/1 | Reached an interview or later. |
| `reached_offer` | 0/1 | Reached an offer. |
| `is_hired` | 0/1 | Accepted an offer. |
| `days_in_process` | decimal | Applied → latest status change, in days. |

## 6. Measures

The KPIs are defined once, here, in words. `powerbi_dashboard_spec.md` gives the DAX, and
`careeros/analytics/export.py: kpis()` computes the same values. Every export records them in its manifest, and the
dashboard spec quotes the demo values; a test checks those quotes.

| KPI | Definition |
|---|---|
| Jobs discovered | Rows of fact_job_evaluation (every job CareerOS recorded). |
| Distinct openings | Jobs that are neither duplicates nor variants. |
| Jobs evaluated | `is_evaluated` = 1. |
| Jobs qualified | `is_qualified` = 1. Equals the application's "distinct qualified openings". |
| Jobs recommended | `is_recommended` = 1 (tiers A and B). |
| Jobs rejected | `is_rejected` = 1. |
| Manual review rate | Jobs needing manual review ÷ jobs evaluated. |
| Duplicates prevented | Duplicate jobs + linked openings. Equals the application's "duplicate reviews avoided". |
| Cross-source sightings | Sightings of an already-known job through another source (`is_cross_source`): merged, not reviewed twice. |
| Verified employers | Employers with at least one active board. Equals the application's "active employers". |
| Average fit | Average `candidate_fit` of scored jobs. |
| Board verification rate | Active boards ÷ automatic-collection boards that are not historical or retired. |
| New-board verification rate | Of boards found after verification existed, the share quarantined or active. Equals the application's "share verified". |
| Scan reliability | Complete scans ÷ all scans. Equals the application's "scan success rate". |
| NIGHT routed | Jobs whose latest NIGHT decision is eligible. |
| Applications, open applications, interview rate | Rows of fact_application; `is_open`; `reached_interview` ÷ applications. |

## 7. Validation

`careeros export` checks every export before writing it. If any check fails, nothing is written and the command
names the failures. The checks:
- **Structure:**
  - every table has exactly its documented columns, in order;
  - every primary key is present and unique;
  - every reference resolves to a dimension row;
  - every value has its column's type;
  - scores are within 0-100 and skill coverage within 0-1;
  - job flags agree with the job's state;
  - `dim_date` has no gaps.
- **Reconciliation with the application:**
  - each fact has one row per source record;
  - every board is in `dim_board`;
  - jobs by state, remote classifications and boards by status match the database;
  - jobs qualified, duplicates prevented, verified employers, scan reliability and new-board verification rate equal
    `careeros metrics`.
- **Demo exports only:** the only email addresses are `example.com` ones, and every employer website is a fictional
  `example.com` site.

The checks and their results are in `manifest.json`, next to the row count and SHA-256 of every file.
`tests/test_analytics.py` also checks:
- that the validation catches deliberately broken exports;
- that this dictionary matches the code;
- that the committed sample is byte-identical to a fresh demo export.

## 8. Design choices

**Considered and not built**, so as to create only structures that genuinely improve analysis:
- **`fact_employer_activity`.** Daily per-employer activity is fully derivable from `fact_board_scan` and
  `fact_job_evaluation`. A stored copy would only drift.
- **Job status history** (the `transitions` table). Re-evaluations matter for system quality; `evaluations`,
  `days_to_decision` and the event facts cover them. A full transition fact can follow if a flow (Sankey) view is
  wanted.
- **A bridge from jobs to US states.** Restricted roles are few; the states are kept as text on `dim_location`.
- **Parquet and database views.** See section 1. CSV can be swapped for Parquet later without changing the model.

**Not in the export at all:**
- **Your profile, pay floor and policy.** Decisions carry their outcome, not their inputs.
- **Free text** (posting descriptions, notes, email content).
