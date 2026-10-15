# CareerOS: business requirements

- Version: 1.0
- Date: 2026-10-13
- Status: describes the system **as built**. Every requirement below is implemented unless its status says otherwise.
- Traceability: `requirements_traceability.md`.

Requirement IDs are stable and are never reused:
- FR = functional
- NFR = non-functional
- DQ = data quality
- SEC = security
- PRIV = privacy
- AUD = auditability
- PERF = performance
- EXC = exception handling
- HR = human review
- AC = acceptance

## 1. Business problem

A job seeker targeting a specialised field (healthcare operations and business systems, including claims and
pharmacy-benefit work) faces an operations problem, not a search problem:

- **BP-1 Volume.** Thousands of postings change daily across employer job boards, alert emails and recommendations.
  Reading them by hand takes hours and misses things.
- **BP-2 Duplication.** The same opening arrives through several channels and locations, and as reposts. Each copy
  costs review time and risks a double application.
- **BP-3 Hidden eligibility.** The facts that decide a job (remote rules and allowed states, degree, licences, years,
  seniority, pay) are buried in free text. Reading them is slow and error-prone.
- **BP-4 Unreliable sources.** Job boards can belong to a different company with the same name. Postings go stale or
  are scams. Pay shown by aggregators is often an estimate.
- **BP-5 Misleading titles.** Relevant work hides behind generic titles ("Operations Specialist", "Analyst II").
- **BP-6 Application conflicts.** Applying twice, or both directly and through an agency, can cost the opportunity.
- **BP-7 Privacy.** Profile, pay floor, applications and correspondence are personal and must stay private.
- **BP-8 No feedback loop.** Without measurement, nobody knows whether the screening agrees with the job seeker's own
  judgement.
- **BP-9 Second job search.** Overnight or part-time work has different rules (schedule, workload, conflict of
  interest) and must not distort the main search.

**Business objective:** conversion over volume. Surface fewer, better-qualified openings with the reasons behind
every decision, so the job seeker spends time applying, not screening.

**Provisional success measures** (instrumented, not yet proven):
- at least 5 extra apply-worthy openings per 30 days from source expansion;
- at least 95% correct audited automatic lead matches, with zero wrong-employer admissions;
- no more than 15 extra review minutes a week.

## 2. Stakeholders

| Stakeholder | Interest | Role in requirements |
|---|---|---|
| Job seeker (owner, product owner) | qualified openings, low effort, privacy | primary user; approves policy, profile and labels |
| Employers / hiring managers | relevant, honest applications | indirect: receive applications prepared from factual data only |
| Job-board providers (Greenhouse, Lever, Ashby, Workable) and employer websites | polite, permitted access | constrain collection (published APIs, budgets, robots.txt) |
| Staffing agencies | representation rights | one-application-per-role rule; clients are never guessed |
| AI assistants (upstream research, engineering) | contributors under governance | outputs are candidates or code reviewed by tests, never authority |
| Portfolio reviewers (future) | understand the system quickly | demo, documentation, traceability |

## 3. User needs

| ID | Need |
|---|---|
| UN-1 | See every relevant opening once, without visiting each site. |
| UN-2 | Know why each job is eligible, rejected or uncertain, in plain language. |
| UN-3 | Trust that a source really belongs to the employer it claims. |
| UN-4 | Not miss good roles because of unusual titles. |
| UN-5 | Never apply twice to the same role or through conflicting channels. |
| UN-6 | Keep personal data private, including from AI tools and public repositories. |
| UN-7 | Spend minutes a day: one review page, one notification. |
| UN-8 | Measure whether the system agrees with my own judgement, and see where it does not. |
| UN-9 | Evaluate overnight or part-time work separately. |
| UN-10 | Stay in control: nothing is applied for or sent on my behalf. |

## 4. Functional requirements

### Ingestion
| ID | Requirement |
|---|---|
| FR-001 | Collect postings from the public listing APIs of Greenhouse, Lever, Ashby and Workable for the boards in the daily scan. |
| FR-002 | Turn job links in alert emails into leads. Sources: saved `.eml` files, or optional Gmail access (OAuth or app password, read-only). Application and recruiter correspondence is skipped. |
| FR-003 | Read job recommendations posted as issues in a private GitHub repository (`Title \| Company \| Location \| Link \| Note`), grade each link, reply with the outcome, and close the issue. |
| FR-004 | Import saved posting pages and plain-text postings, and grade a pasted posting (command line or review page) without fetching its link. |
| FR-005 | Fetch only from sources the site policy allows. Links to other sites (job aggregators, systems that are not read automatically) are listed as leads for manual review. |
| FR-006 | Grade a single posting from a supported job-board link (`add-url`). |

### Normalisation and classification
| ID | Requirement |
|---|---|
| FR-010 | Normalise each posting into structured fields: title, location, remote type, allowed / excluded states, remote scope, country, employment type, pay (any period, annualised), deadline, department, source dates. |
| FR-011 | Classify titles into the job seeker's job families. Generic titles need supporting context. |
| FR-012 | Give title-screen rejects with ambiguous titles a budgeted second review based only on the role's own duties and requirements, and record every decision. |

### Deduplication and opening identity
| ID | Requirement |
|---|---|
| FR-020 | Fingerprint posting content. Separate meaningful changes (pay, remote rules, location, type, deadline, title) from cosmetic edits, and re-evaluate only on meaningful ones. |
| FR-021 | Detect duplicates across sources by requisition. Group the same role in other locations as variants. Keep different requisitions with the same title apart. |
| FR-022 | Record every sighting (scan, alert, recommendation, paste) with its provenance. Link reposts and reopenings to earlier history. |
| FR-023 | Close a posting only after a set number of complete scans without it. Failed or suspicious scans never close postings. |

### Employer identification and source verification
| ID | Requirement |
|---|---|
| FR-030 | Keep a stable record per employer with aliases, former and legal names. Never merge employers on similar names. |
| FR-031 | Discovery (leads, recommendations, curated suggestions, bounded name guesses) produces candidate boards only. |
| FR-032 | Admit a board to the daily scan only when the employer's own website links to it, the board's identity agrees, Board Trust reaches 85 or more, and a validation scan passes; or by an expiring manual approval. |
| FR-033 | Score Board Trust (legitimacy), Career Fit (usefulness) and Board Health (reliability) separately. Career fit sets scan frequency. |
| FR-034 | Re-verify boards on a schedule and on triggers. Pause a board that disappears or changes identity. Detect ATS migrations. Treat provider-wide failures as provider problems. A board scanned before verification existed keeps its history and is marked `verification_required` while proof is unavailable. |
| FR-035 | Record employers whose careers site uses a system CareerOS never reads (e.g. Workday, iCIMS) as manual-only sources. |
| FR-036 | Never probe for a staffing agency's undisclosed client, and never guess boards for excluded or held employers. |

### Requirements, eligibility and decisions
| ID | Requirement |
|---|---|
| FR-040 | Extract requirements with verbatim quotes and classify them as required, preferred or ambiguous. An optional AI reader, off by default, is cross-checked against the rules reader. |
| FR-041 | Apply hard eligibility rules, each with a stated reason and evidence: remote / location and states, work authorisation, degree (with a configured stretch exception), licences, certifications, clearance, years, seniority, pay floor, excluded employers and titles, scam signals. |
| FR-042 | Treat unknown facts as flags, never as matches or rejections. Show per-job eligibility facts (compatible / incompatible / unknown / conflicting) and the one or two unknowns that would decide the job. |
| FR-043 | Hold jobs at employers awaiting a decision by the job seeker: flagged, never rejected, never top tier. |
| FR-044 | Re-evaluate decisions when the policy, profile or rule code changes, keeping earlier decisions as history. |

### Scoring and routing
| ID | Requirement |
|---|---|
| FR-050 | Score eligibility, fit (weighted skill coverage), career value and apply priority, and store each component. |
| FR-051 | Route jobs to tiers. A and B require an eligible state and minimum coverage. Near-miss and archive below that. Conditional and held states are kept separately. |
| FR-052 | Route overnight / part-time work through a separate NIGHT profile: supplemental vs replacement mode, its own pay, schedule reading, workload and conflict-of-interest flags. It never changes primary decisions. |
| FR-053 | Keep a shortlist with a next action for each job. Dismissals record a reason. |

### Review, tracking and output
| ID | Requirement |
|---|---|
| FR-060 | Produce a daily review page: new since the last review, shortlist, needs-review, leads, applications, sources and verification. |
| FR-061 | Record Apply / Stretch / Skip labels and measure the system's agreement with them. |
| FR-062 | Track applications through a workflow. Allow one application per role across variants, duplicates and channels. Nudge for a follow-up after 14 days without a response. |
| FR-063 | Write private application drafts from factual data only (never screening-only adjustments), with honest gaps. |
| FR-070 | Record the evidence behind every decision: gate reasons and flags with quotes, score components, and the board-verification evidence chain. |
| FR-071 | Explain why a board is trusted or not (`careeros sources explain`). |
| FR-072 | Report metrics: discovery, opportunities, verification quality, pilot measures, and a funnel by source. |
| FR-073 | Publish a sanitised index of jobs already seen to the recommendation inbox, so recommendations are not repeated. |
| FR-074 | Explain any job (`careeros explain`): source, first / last seen, employer, verification, duplicate decision, eligibility, fit, career value, routing, status, rejection reason, review reason and actions, from recorded decisions only. A question that does not apply says so and why. |
| FR-075 | Export the data for analysis (`careeros export`): a documented star schema (8 dimensions, 5 facts) as CSV with a manifest; validated before it is written (structure, references, types, ranges, and reconciliation with the application's own figures); byte-identical for the same database. |
| FR-080 | Run daily on a schedule, notify with a summary, and back up the database and settings with an integrity check and retention. |
| FR-081 | Verify the audit trail on demand: the event hash chain, plus per-job and per-application history. |

## 5. Non-functional requirements
| ID | Requirement |
|---|---|
| NFR-001 | Runs locally for one user: Python 3.12, SQLite, no server infrastructure. |
| NFR-002 | One failing item never stops a run: per board, issue, line, email and job. A failed run raises a notification. |
| NFR-003 | Decisions are reproducible: each records the policy, profile and extraction fingerprints. Same inputs give the same decisions. |
| NFR-004 | Changes are accepted only when the full test suite passes and the mutation check catches every deliberate defect. |
| NFR-005 | Polite collection: request budgets, minimum intervals, `Retry-After` honoured, robots.txt honoured on employer websites. |
| NFR-006 | The core runs wherever Python runs. Windows-only features (notifications, credential store, scheduler) are guarded. |
| NFR-007 | Every rejection, flag and score is explained in plain language. |
| NFR-008 | Behaviour is configured in YAML policy with private overrides. Operational settings never trigger re-evaluation. |

## 6. Data quality requirements
| ID | Requirement |
|---|---|
| DQ-001 | Posting text is cleaned of hidden characters and escaped HTML before use. |
| DQ-002 | Tracking parameters never create a new posting identity. Links are canonicalised. |
| DQ-003 | Weaker sources (alerts, recommendations) never overwrite facts from the employer's own board. Conflicts stay visible. |
| DQ-004 | Pay is judged on the full range: units annualised; a range that straddles the floor, geographic tiers or variable pay without a base stated = unknown; aggregator pay never rejects. |
| DQ-005 | "Remote" alone is never read as nationwide. State limits and exclusions are kept. |
| DQ-006 | Postings older than the configured age are flagged as possibly stale, never hidden. |
| DQ-007 | Structured note fields are validated. Bad values are dropped, never stored, and can never change a decision. |
| DQ-008 | A board whose postings stop naming the employer is paused before anything more is ingested (identity drift). |
| DQ-009 | Requirements text that cannot be read reliably is marked low-confidence and sent for human review. |
| DQ-010 | Board links are normalised so each board has one identity, whatever form the link takes: http vs https, letter case, trailing slashes, tracking parameters, embed scripts, posting links, API URLs. A different board name or provider is a different board. The link observed on the employer's page and the canonical board link are both kept. |

## 7. Security requirements
| ID | Requirement |
|---|---|
| SEC-001 | Secrets live only in the operating system's credential store. None are in the repository, which a test enforces. |
| SEC-002 | The review server listens on 127.0.0.1 only and requires a secret token, a Host check and one-time form keys bound to one action and target. |
| SEC-003 | External content (postings, emails, issues, web pages) is untrusted data. It is escaped in output, never executed, and can never change policy, scores or what is fetched. |
| SEC-004 | Employer websites are read only through a safe fetcher: https / port 443, public addresses only, connections pinned to checked addresses, every redirect re-checked, size and type caps, no cookies. |
| SEC-005 | Job-board API clients may only contact the four listing-API hosts, redirects included. |
| SEC-006 | Each component may write only its own tables and columns. State changes go only through workflow matrices. |
| SEC-007 | Email access is read-only: Gmail read-only scope, IMAP opened without marking messages read. Message content is not stored. |
| SEC-008 | Job aggregators and sites the job seeker must not automate are never fetched or treated as employer websites. There is no CAPTCHA bypass. |
| SEC-009 | A manual approval cannot override an employer mismatch, an unsafe address, a contradicting official site or a manual-only system. |
| SEC-010 | In Demo Mode nothing leaves the machine and no private source is read: no real network or name resolution, no credentials, no Gmail or IMAP, no GitHub, no clipboard, no AI key, no publishing, no notifications. A network client created in Demo Mode without the synthetic world fails instead of reaching the internet. |

## 8. Privacy requirements
| ID | Requirement |
|---|---|
| PRIV-001 | Personal data (profile, policy overrides, database, labels, applications, reports, backups) lives in a vault outside the repository, which is enforced. |
| PRIV-002 | The optional AI reader receives public posting text only, never the profile. |
| PRIV-003 | Replies to the recommendation inbox and the published index contain job facts only: no profile, pay floor, scores or tiers. |
| PRIV-004 | Logs redact personal data and keep identifiers. |
| PRIV-005 | Reasons for excluding an employer are never written down. Rejections use a neutral reason. |
| PRIV-006 | Notifications pass their text without writing it to disk or command lines. |
| PRIV-008 | An analytics export of private data is never written inside the code repository; a Demo Mode export never leaves the demo vault and must contain only fictional data. Exports carry decisions, never the profile, pay floor or free text. |
| PRIV-007 | Demo Mode uses synthetic data only, in a throwaway demo vault that may not be, contain or sit inside the private vault or the repository. In Demo Mode the private vault cannot even be located, and only folders carrying the demo marker are ever deleted. |

## 9. Auditability requirements
| ID | Requirement |
|---|---|
| AUD-001 | Every state change follows a workflow matrix and writes a transition plus an audit event. A projection that disagrees with its history fails closed. |
| AUD-002 | The audit events form a hash chain, which `careeros verify` checks. |
| AUD-003 | History tables are append-only, enforced by database triggers. |
| AUD-004 | Repeated commands (a double click) are recorded once (idempotency receipts). |
| AUD-005 | Board verification keeps an append-only evidence chain per run: source and destination, method, result, points. |
| AUD-006 | Test and drill records are excluded from metrics. |

## 10. Performance expectations

These are expectations measured on the live system, not guarantees.

| ID | Expectation |
|---|---|
| PERF-001 | A daily run at a few-thousand-posting scale finishes in minutes (observed: under two minutes). |
| PERF-002 | Daily request ceilings: board discovery at most 60 requests; employer-site pages at most 40; verification and validation at most 40 listing-API requests per run. |
| PERF-003 | The full test suite runs in about a minute. The mutation check takes about 50 minutes on 4 workers. |

## 11. Exception handling
| ID | Requirement |
|---|---|
| EXC-001 | Failures are recorded as exceptions with type, severity, retryability and subject. Data-quality failures on one posting do not stop the batch. |
| EXC-002 | A failed board fetch is recorded as a failed scan (never a closure). Access denied repeatedly disables only that board. A vanished board is paused for re-verification. |
| EXC-003 | When many boards of one provider fail together, the provider is marked degraded and no employer is penalised. |
| EXC-004 | An interrupted run's write lock can be cleared (`careeros unlock`) and the run is marked failed. |
| EXC-005 | After a reported email outage, the next run looks back 7 days instead of 3. |
| EXC-006 | A recommendation issue that cannot be closed is retried, never re-read. An unreadable email file is set aside with its reason. |
| EXC-007 | An employer website that cannot be read (timeout, robots.txt, login) never changes trust. Repeated failures ask for review. Running out of CareerOS's own page budget is never counted as a site failure. |
| EXC-008 | No job can stay parked silently: a job awaiting review is re-checked when the rules, policy or profile change, and a job that cannot be closed with its board is recorded as an exception. |

## 12. Human review requirements
| ID | Requirement |
|---|---|
| HR-001 | The job seeker's labels are the ground truth. The system's agreement with them is measured. |
| HR-002 | Postings whose requirements cannot be read reliably go to a needs-review section. |
| HR-003 | Boards that cannot be verified automatically become review cases with a stated reason. Inconclusive checks of boards already being scanned are summarised, not raised as cases. |
| HR-004 | Manual approvals record a reason, the approver and the previous decision, and they expire. |
| HR-005 | Title rejections and automatic lead matches can be sampled and audited. |
| HR-006 | Nothing is applied for, submitted or sent automatically. The job seeker applies. (Automatic submission is deferred to a later phase, not ruled out: ADR 0007.) |
| HR-007 | Conflict-of-interest and held-employer situations are flagged for the job seeker's decision, never decided by the system. |

## 13. MVP acceptance criteria

**Private system (Phase 1).**

| ID | Criterion | Status |
|---|---|---|
| AC-001 | The daily run collects, screens, scores and reports without manual steps. | Met |
| AC-002 | Every rejection has a reason and evidence; unknowns are flags. | Met (tests) |
| AC-003 | No duplicate applications across variants, duplicates or channels. | Met (tests) |
| AC-004 | At least 50 job-seeker labels with measured agreement (the Phase 1 exit test). | **Not yet** (labels pending) |
| AC-005 | Every change is gated by the full test suite and the mutation check. | Met |

**Portfolio functional MVP.** From `portfolio_v1_plan.md`.

| ID | Criterion | Status |
|---|---|---|
| AC-101 | A demo runs end-to-end (source to output) on synthetic data, offline, with no credentials. | Met: `careeros demo tour` (`docs/demo_mode.md`; `tests/test_demo_mode.py`) |
| AC-102 | The demo is deterministic: identical output on repeat runs. | Met for decisions and output: the same summary, job states and report on every run. Record identifiers (run, review case, event) are random by design. |
| AC-103 | Each demo job can be explained with its evidence. | Met: `careeros explain JOB` and `careeros sources explain` with `CAREEROS_DEMO_MODE=true` |
| AC-104 | The demo covers each decision type: eligible, rejected, unknown pay, duplicate, night route, scam, verified / contradicted / impostor board. | Met except two cases not in the demo world yet: a board contradicted by the employer's own site (the impostor case is Quillon), and an overnight posting that NIGHT accepts (NIGHT routes are shown as conditional) |
| AC-105 | A Power BI-ready export with a data dictionary. | Met: `careeros export`, `analytics_data_dictionary.md`, `powerbi_dashboard_spec.md`, demo sample `samples/analytics_demo/` (`tests/test_analytics.py`), and the report itself: `powerbi/CareerOS.pbip` (the private build also keeps a `.pbix` saved by Power BI Desktop), checked in Power BI Desktop (`tests/test_powerbi.py`). |
| AC-106 | CI runs the tests, the demo and a release-safety scan. | **Partly met**: `.github/workflows/ci.yml` runs `tools/validate.py` (lint, types, configuration, the tests with the network refused, the Demo Mode MVP gate, the release scan, pip-audit), and the same command passes here and in a fresh clone. The workflow has not run on GitHub yet: this repository has no remote |
| AC-107 | A sanitised public snapshot contains no private data, credentials or history. | **Not yet**: strategy, findings and release gate defined (`public_release_checklist.md`, `tools/release_scan.py`); the preparatory refactor (R4) was done on 2026-10-14; the snapshot generator (`tools/build_snapshot.py`) is built; the snapshot, its review and publication follow |
