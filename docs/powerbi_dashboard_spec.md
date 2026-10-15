# CareerOS: Power BI dashboard specification

- Version: 1.0
- Date: 2026-10-13
- Data: the analytics export (`careeros export`; tables in `analytics_data_dictionary.md`).
- Build and check it against the demo export, `samples/analytics_demo/`, which is synthetic. The KPI values in
  section 3 are what that export measures; a test keeps them true.

## 1. Purpose

One report answers five questions about the job search:

| Page | Question | Main reader |
|---|---|---|
| Executive overview | Is CareerOS finding enough of the right jobs, and is the search converting? | The job seeker; a portfolio reviewer |
| Employer intelligence | Which employers are worth watching, and which sources can be trusted? | The job seeker |
| Job pipeline | Where are jobs now, why were they decided that way, and what is next? | The job seeker |
| Role intelligence | Which role families and work arrangements produce qualified jobs, and at what pay? | The job seeker |
| System quality | Is the machine working: verification, scanning, duplicates, review load? | The builder; a technical reviewer |

## 2. Model setup

**Load.** Create a parameter `ExportFolder` (text). Set it to the demo sample folder (`...\engine\samples\analytics_demo`)
to build the report, or to your private export (`...\vault\exports\analytics`) to use it. Then create this function:

```m
// LoadTable("fact_job_evaluation"): one export file with its headers; empty cells become null so numbers type cleanly
(name as text) as table =>
let
    Source = Csv.Document(File.Contents(ExportFolder & "\" & name & ".csv"),
                          [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Nulls = Table.ReplaceValue(Headers, "", null, Replacer.ReplaceValue, Table.ColumnNames(Headers))
in
    Nulls
```

Add one query per table, each calling `LoadTable`. Then set the column types listed in the data dictionary:
- keys and text → Text;
- whole numbers, date keys and 0/1 flags → Whole Number;
- decimals → Decimal Number;
- dates → Date.

The empty-to-null step matters: an empty cell otherwise fails to convert to a number.

**Relationships.**
- Create the relationships in section 3 of the data dictionary: all many-to-one, single direction.
- Mark the inactive date relationships as inactive.
- Do not relate `dim_board` to `dim_employer`.

**Housekeeping.**
- Mark `dim_date` as the date table on `date`, and turn off auto date/time.
- Sort `month_name` by `month`, `day_name` by `day_of_week`, `family_name` by `sort_order` and `scope_label` by
  `sort_order`.
- Hide every key column and every 0/1 flag from report view; measures replace them.
- Keep all measures in an empty table named `_Measures`.

**Refresh.** Run `careeros export`, then refresh the report. The export folder is replaced as a whole, and only after
every check passes, so a refresh never sees a half-written export.

**Privacy.** A report on your private export contains your real search data. Keep that `.pbix` in the vault, and
never publish it to the web. Build portfolio screenshots from the demo export only.

## 3. Measures

### KPI catalogue (checked against the demo export)

| Measure | KPI key | Demo value |
|---|---|---|
| Jobs Discovered | `jobs_discovered` | 45 |
| Distinct Openings | `distinct_openings` | 44 |
| Jobs Evaluated | `jobs_evaluated` | 40 |
| Jobs Qualified | `jobs_qualified` | 18 |
| Jobs Recommended | `jobs_recommended` | 5 |
| Jobs Rejected | `jobs_rejected` | 13 |
| Jobs Manual Review | `jobs_manual_review` | 13 |
| Manual Review Rate | `manual_review_rate` | 32.5% |
| Duplicates Prevented | `duplicates_prevented` | 3 |
| Cross-Source Sightings | `cross_source_sightings` | 2 |
| Verified Employers | `verified_employers` | 8 |
| Average Fit | `average_fit` | 84.8 |
| Board Verification Rate | `board_verification_rate` | 80.0% |
| New-Board Verification Rate | `new_board_verification_rate` | 60.0% |
| Scan Reliability | `scan_reliability` | 96.3% |
| NIGHT Routed | `night_routed` | 13 |
| Applications | `applications` | 4 |
| Open Applications | `applications_open` | 3 |
| Interview Rate | `interview_rate` | 0.0% |

Each KPI's definition is in data dictionary section 6. The same values are in every export's `manifest.json`
(`kpis`), so a report built on a private export can be checked the same way.

### DAX: the KPIs

```dax
Jobs Discovered = COUNTROWS ( fact_job_evaluation )

Distinct Openings =
CALCULATE ( [Jobs Discovered], fact_job_evaluation[is_duplicate] = 0, fact_job_evaluation[is_variant] = 0 )

Jobs Evaluated = SUM ( fact_job_evaluation[is_evaluated] )
Jobs Qualified = SUM ( fact_job_evaluation[is_qualified] )
Jobs Recommended = SUM ( fact_job_evaluation[is_recommended] )
Jobs Rejected = SUM ( fact_job_evaluation[is_rejected] )
Jobs Manual Review = SUM ( fact_job_evaluation[is_manual_review] )
Manual Review Rate = DIVIDE ( [Jobs Manual Review], [Jobs Evaluated] )

Duplicates Prevented =
SUM ( fact_job_evaluation[is_duplicate] ) + SUM ( fact_job_evaluation[linked_openings] )

Cross-Source Sightings = SUM ( fact_job_observation[is_cross_source] )

Verified Employers = CALCULATE ( COUNTROWS ( dim_employer ), dim_employer[is_verified_employer] = 1 )

Average Fit =
CALCULATE ( AVERAGE ( fact_job_evaluation[candidate_fit] ), fact_job_evaluation[is_scored] = 1 )

Board Verification Rate =
VAR Judged =
    CALCULATETABLE (
        dim_board,
        dim_board[collection] = "allowed",
        NOT dim_board[verification_status] IN { "historical", "retired" }
    )
RETURN
    DIVIDE ( COUNTROWS ( FILTER ( Judged, dim_board[verification_status] = "active" ) ), COUNTROWS ( Judged ) )

New-Board Verification Rate =
VAR NewBoards = CALCULATETABLE ( dim_board, dim_board[is_legacy] = 0, dim_board[collection] = "allowed" )
RETURN
    DIVIDE (
        COUNTROWS ( FILTER ( NewBoards, dim_board[verification_status] IN { "quarantined", "active" } ) ),
        COUNTROWS ( NewBoards )
    )

Scan Reliability = DIVIDE ( SUM ( fact_board_scan[is_complete] ), COUNTROWS ( fact_board_scan ) )

NIGHT Routed = CALCULATE ( [Jobs Discovered], fact_job_evaluation[night_decision] = "eligible" )

Applications = COUNTROWS ( fact_application )
Open Applications = SUM ( fact_application[is_open] )
Interview Rate = DIVIDE ( SUM ( fact_application[reached_interview] ), [Applications] )
```

### DAX: supporting measures

```dax
Qualified Rate = DIVIDE ( [Jobs Qualified], [Jobs Evaluated] )
Rejection Rate = DIVIDE ( [Jobs Rejected], [Jobs Evaluated] )
Recommended Rate = DIVIDE ( [Jobs Recommended], [Jobs Evaluated] )

-- the role-playing dates (data dictionary, section 4)
Jobs Decided =
CALCULATE ( [Jobs Evaluated], USERELATIONSHIP ( fact_job_evaluation[decided_date_key], dim_date[date_key] ) )
Jobs Closed =
CALCULATE ( SUM ( fact_job_evaluation[is_closed] ),
            USERELATIONSHIP ( fact_job_evaluation[closed_date_key], dim_date[date_key] ) )
Jobs Labelled =
CALCULATE ( COUNTROWS ( fact_job_evaluation ), NOT ISBLANK ( fact_job_evaluation[label] ),
            USERELATIONSHIP ( fact_job_evaluation[label_date_key], dim_date[date_key] ) )

Average Days to Decision = AVERAGE ( fact_job_evaluation[days_to_decision] )
Average Days Listed = AVERAGE ( fact_job_evaluation[days_listed] )

Pay Stated Rate =
DIVIDE ( CALCULATE ( [Jobs Discovered], dim_job[is_pay_stated] = 1 ), [Jobs Discovered] )
Average Top of Pay Range (annual) =
CALCULATE ( AVERAGEX ( fact_job_evaluation, RELATED ( dim_job[annual_pay_max] ) ), fact_job_evaluation[is_scored] = 1 )

Labelled Apply = CALCULATE ( [Jobs Discovered], fact_job_evaluation[label] = "apply" )
Applied = SUM ( fact_job_evaluation[has_application] )
Recommended Then Applied =
CALCULATE ( [Applied], fact_job_evaluation[is_recommended] = 1 )

Response Rate = DIVIDE ( SUM ( fact_application[got_answer] ), [Applications] )
Screening Rate = DIVIDE ( SUM ( fact_application[reached_screening] ), [Applications] )
Offer Rate = DIVIDE ( SUM ( fact_application[reached_offer] ), [Applications] )

Average Board Trust = AVERAGE ( dim_board[board_trust_score] )
Scan Failures = SUM ( fact_board_scan[is_failure] )
Postings Read = SUM ( fact_board_scan[postings] )
Verification Runs = COUNTROWS ( fact_verification )
Verification Pass Rate = DIVIDE ( SUM ( fact_verification[is_pass] ), [Verification Runs] )

Remote Nationwide Share =
DIVIDE ( CALCULATE ( [Jobs Discovered], dim_location[remote_scope] = "US_nationwide" ), [Jobs Discovered] )
```

### DAX: helpers used by the report's visuals

```dax
-- counts boards without the (none) member, so tables of boards leave it out
Boards = CALCULATE ( COUNTROWS ( dim_board ), dim_board[board_key] <> "(none)" )
Average Career Fit = AVERAGE ( dim_employer[career_fit_score] )
Shortlist Jobs =
CALCULATE ( COUNTROWS ( fact_job_evaluation ),
            fact_job_evaluation[is_recommended] = 1 || fact_job_evaluation[is_manual_review] = 1 )

-- by-reason charts: jobs that were not rejected (and scans that did not fail) have no reason; leave that empty
-- category out without changing any total
Jobs Rejected by Reason =
IF ( HASONEVALUE ( fact_job_evaluation[rejection_group] ) && ISBLANK ( SELECTEDVALUE ( fact_job_evaluation[rejection_group] ) ),
     BLANK (), [Jobs Rejected] )
Scan Failures by Error =
IF ( HASONEVALUE ( fact_board_scan[error_code] ) && ISBLANK ( SELECTEDVALUE ( fact_board_scan[error_code] ) ),
     BLANK (), [Scan Failures] )
```

**Formatting:** counts as whole numbers; rates as percentages with one decimal; fit, value and priority with one
decimal; pay as currency with no decimals.

## 4. Pages

Every page shares one slicer bar:
- date (`dim_date[date]`, as a range);
- role family;
- work arrangement;
- provider.

One colour per tier, used on every page:
- A: dark teal;
- B: teal;
- near-miss: amber;
- archive: grey;
- rejected: muted red.

Use the colour-blind-safe palette of your theme, and always show labels next to colour.

### Page 1: EXECUTIVE OVERVIEW

**Purpose:** the state of the search at a glance.

- **KPI cards (top row):** Jobs Discovered, Jobs Qualified, Jobs Recommended, Jobs Rejected, Duplicates Prevented,
  Verified Employers, Average Fit, Manual Review Rate. Each card has a subtitle comparing it with the previous
  period, using `dim_date`.
- **Funnel**, one bar per stage: Jobs Discovered → Distinct Openings → Jobs Evaluated → Jobs Qualified → Jobs
  Recommended → Labelled Apply → Applied (`[Applied]`) → reached screening → reached interview.
- **Line chart: discovery over time.** Jobs Discovered by `dim_date[date]` (first seen), with Jobs Qualified as a
  second line.
- **Bar chart: why jobs are rejected.** Jobs Rejected by `fact_job_evaluation[rejection_group]`, sorted descending;
  the tooltip shows `rejection_reason`.
- **Donut: where qualified jobs come from.** Jobs Qualified by `dim_source[source_name]` (discovered via).
- **Interaction:** selecting a funnel stage or a rejection group filters the rest of the page.

### Page 2: EMPLOYER INTELLIGENCE

**Purpose:** which employers deserve attention, and how far each source can be trusted.

- **Table, one row per employer** (`dim_employer[employer_name]`), with:
  - Jobs Discovered, Jobs Qualified, Jobs Recommended and Average Fit;
  - `career_fit_score`, `career_priority`, `boards_active`;
  - Scan Reliability.

  Conditional formatting: data bars on Jobs Qualified, and an icon on `is_verified_employer`. Sorted by Jobs
  Qualified.
- **Scatter: employer value.** One point per employer: x = `career_fit_score`, y = Jobs Qualified, size = Jobs
  Discovered, colour = `career_priority`. Employers top-right are the ones to watch.
- **Matrix: board verification by provider.** Rows `dim_ats[ats_name]`, columns `dim_board[verification_status]`,
  values = count of boards. It shows at a glance how many boards are active, waiting for review, rejected (impostors)
  or manual-only.
- **Bar chart:** Verified Employers against employers without an active board (`dim_employer[employer_origin]`).
- **Drill-through** from an employer to the job pipeline page, filtered to that employer.

### Page 3: JOB PIPELINE

**Purpose:** where every job stands, why, and what to do next.

- **Stacked column by day:**
  - Jobs Discovered (first seen), Jobs Decided and Jobs Closed per `dim_date[date]`;
  - the three measures use their own date relationships (section 3);
  - this shows flow in and out of the search.
- **Matrix: decisions.** Rows `decision_stage` → `gate_decision` → `rejection_reason`; values Jobs Discovered. It
  shows how much the title screen decides compared with the full eligibility rules.
- **Table: the shortlist.** Jobs where Jobs Recommended or Jobs Manual Review is 1, with:
  - title (`dim_job`), employer, tier, `apply_priority`, `candidate_fit`;
  - `decisive_unknowns` (what to check first);
  - `label`, `has_application`.

  Sorted by `apply_priority` descending.
- **Bar chart: applications by status** (`fact_application[current_state]`), with Response Rate, Screening Rate and
  Interview Rate cards beside it.
- **Cards:** Average Days to Decision, Average Days Listed.
- **Job detail (drill-through page)**, from any job:
  - every `dim_job` attribute, the scores and `rejection_reason`;
  - the sightings (`fact_job_observation`: source, date, first sighting or cross-source).

  This is the report's version of `careeros explain`.

### Page 4: ROLE INTELLIGENCE

**Purpose:** which kinds of roles are worth the effort.

- **Table, one row per role family** (`dim_role_family[family_name]`), with Jobs Discovered, Jobs Qualified, Qualified
  Rate, Average Fit, Average Top of Pay Range (annual) and Pay Stated Rate. "No target family" is kept as its own row,
  so screened-out volume stays visible.
- **100% stacked bar: remote classifications.** Jobs Discovered by `dim_location[scope_label]` for each role family.
  It shows how often "remote" means anywhere in the US, limited states, or remote with no states stated.
- **Matrix: rejection reasons by family.** Rows family, columns `rejection_group`, values Jobs Rejected.
- **Column chart: pay.** Average Top of Pay Range (annual) by family, with a constant line at your floor. Add the line
  as a what-if parameter; the export does not carry your pay floor.
- **Bar chart: NIGHT.** NIGHT Routed by `night_mode`.

### Page 5: SYSTEM QUALITY

**Purpose:** evidence that the pipeline is reliable. This is the page for a technical reviewer.

- **KPI cards:** Board Verification Rate, New-Board Verification Rate, Scan Reliability, Verification Pass Rate,
  Average Board Trust, Duplicates Prevented, Cross-Source Sightings, Manual Review Rate.
- **Line chart: scan reliability over time.** Scan Reliability by `dim_date[date]` (scan date), with Scan Failures as
  columns.
- **Bar chart: scan failures by error code** (`fact_board_scan[error_code]`), coloured by `provider_degraded`.
- **Matrix: verification runs.** Rows `fact_verification[kind]`, columns `outcome`, values Verification Runs; the
  tooltip shows `failure_class`.
- **Table: board health.** One row per board: `board_trust_score`, `board_health_score`, `board_activity`,
  `verification_status`, `is_paused`, `paused_reason`. Conditional formatting: health below 80 in amber, paused in
  red.
- **Cards:** Average Days to Decision, and Jobs Evaluated against Jobs Discovered (how much of the intake gets a
  decision).

## 5. Acceptance

The report is ready when all of these hold:
1. On the demo export, every card in section 3 shows the demo value listed there.
2. Every relationship is many-to-one with single direction, and no visual shows a "(Blank)" category caused by a
   missing key. Every key has a `(none)` member.
3. Changing `ExportFolder` to a private export and refreshing works without edits.
4. Every visual has a title that states its question, and colour is never the only carrier of meaning.
5. The private `.pbix` is not shared or published.

## 6. The built report

The report described here exists as a **Power BI Project** in `powerbi/`:
- `CareerOS.pbip`: open it in Power BI Desktop (2.158 or later), then **Home → Refresh**;
- `CareerOS.pbix`: the same report saved by Power BI Desktop, with the demo data loaded (private build only: it stores
  a local folder path, so the public copy leaves it out);
- `screenshots/`: one image per page, taken on the demo export.

Every file of the project is **generated** by `tools/build_powerbi_project.py`:
- the model's tables, types, relationships, sort orders and date table come from `careeros/analytics/export.py`;
- the measures are the DAX in section 3.

Change the generator, never the files, and rebuild:

```
.venv\Scripts\python.exe tools\build_powerbi_project.py --folder "<full path to samples\analytics_demo>"
```

`tests/test_powerbi.py` checks four things:
- the committed project is exactly what the generator builds;
- every field a visual uses, and every column a measure or relationship names, exists;
- every `RELATED` has a relationship to follow;
- the measures are exactly those listed in this document.

The project was opened in Power BI Desktop 2.158 on 2026-10-13. It refreshed on the demo export, every card on every
page showed the value in section 3, and no visual showed an error.

**On another computer.** The project remembers the folder it was built with. Open `CareerOS.pbip`, set **Transform data → Manage parameters → ExportFolder** to the clone's
`samples\analytics_demo` folder, and refresh.

**Your own data.** In Power BI Desktop, go to **Transform data → Manage parameters → ExportFolder**, enter your vault's
`exports\analytics`, and refresh. A report holding your own data is private: save it in your vault, not in this
repository.
