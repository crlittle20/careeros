# CareerOS

**An evidence-driven job-search operations and decision-support platform:** CareerOS discovers openings, verifies their
source, rejects those that fail a stated hard requirement, ranks the rest for a person to review and records the reason
for every decision. It does not apply for jobs; no code in it submits an application or contacts an employer or
recruiter.

![The Executive Overview page of the CareerOS Power BI report, on synthetic demo data](powerbi/screenshots/executive.png)

*Executive Overview page, on Demo Mode data: every number in the screenshots is synthetic.*

**Built by Christopher Little**, product owner and business systems analyst, for their own job search. Chris supplied
or approved the requirements and screening rules, made the product, privacy and release decisions, reviewed the work
by using it, labelled the 50 postings behind the calibration and wrote the private Power BI measures. Claude
(Anthropic) implemented the code, tests and documentation under Chris's direction; Codex (OpenAI) reviewed it
independently; ChatGPT helped design it. Full account: [My role](docs/portfolio_overview.md#my-role) in the portfolio
overview and [AI-assisted development](docs/ai_assisted_development.md). Below, "I" and "the owner" are Chris; "the job
seeker" is Chris on the private system and a fictional person in Demo Mode.

**Results**

- 946 automated tests pass locally with outbound network refused. The public repository's CI runs the same
  validation; this version's CI result comes with its publication.
- 749 planted defects: the latest full run (2026-11-02) caught all 749, each with a failing test
  (`evidence/mutation-receipt.json`).
- Chris labelled 50 real postings (2026-10-16): the hard rules agreed with all 50 (in-sample), the ranking did not;
  ranking effectiveness is not demonstrated ([limitations](#limitations)).

### Project status (2026-11-02)

- First published 2026-10-15 (UTC). This version publishes the code as of 2026-11-02; the code published before it
  was the snapshot of 2026-10-31 (published 2026-11-01: assisted applications with the browser extension, and
  dismissal fields in the analytics export).
- New since then: the review page confirms each action on a page naming the posting, can undo a dismissal and can
  void an application the employer never answered ([ADR 0007](docs/adr/0007-human-approval-at-selected-stages.md),
  amended 2026-11-01); the shortlist leaves out closed postings and shows each job's id and how long it has waited;
  `careeros explain` opens with a one-sentence verdict; the daily run keeps a redacted log, and `careeros status` and
  the saved report show the last completed run and the label backlog.
- The 50 labelled postings behind the calibration and the owner's Power BI report on real data stay private.

### For technical reviewers

- **Try it:** the demo runs offline on synthetic data and needs no credentials: [quickstart](#quickstart), then the
  [reviewer walkthrough](docs/demo_walkthrough.md).
- **Read next:** [case study](docs/case_study.md) · [architecture](docs/architecture.md) ·
  [AI-assisted development](docs/ai_assisted_development.md) · [Demo Mode](docs/demo_mode.md) ·
  [security architecture](docs/security_architecture.md) · [business requirements](docs/business_requirements.md)
- **Chris's own Power BI work:** the [CareerOS Power BI showcase](https://github.com/crlittle20/careeros-powerbi-showcase),
  the measures Chris wrote (Power Query and DAX), rebuilt on this project's synthetic demo export.

## Project summary

| | |
|---|---|
| What | A local, single-user Python application: a daily pipeline, a command line, a local review page and a Power BI report |
| For | One real job search. Designed for healthcare operations and business-systems roles, including claims and pharmacy-benefit work. It runs daily on the owner's machine. |
| Shown publicly through | Demo Mode: the same production code on fictional employers, offline, with no credentials |
| Size | About 20,700 lines of Python in the engine and 15,700 in tests; 43 database tables; 36 commands |
| Documented by | 119 numbered requirements (107 requirements and 12 acceptance criteria), consolidated from the specifications and the built system and traced to code and tests ([business requirements](docs/business_requirements.md), [traceability](docs/requirements_traceability.md)), and 9 architecture decision records ([ADRs](docs/adr/README.md)) |
| Built with | A human-directed, AI-assisted workflow: the owner directed and decided, ChatGPT helped design and challenge, Claude implemented ([how](docs/ai_assisted_development.md)) |
| Status | Phase 1 (discovery and qualification) is in daily private use. The public repository's code is a generated snapshot of the private development repository; its documentation and dependency locks were also updated directly in the public repository on 2026-10-29, and the private repository carries the same corrections ([limitations](#limitations)). |

## Business problem

In a specialised field, job searching is an operations problem more than a search problem:
- **Volume.** Thousands of postings change daily across employer job boards, alert emails and recommendations.
- **Duplication.** One opening arrives through several channels, locations and reposts. Each copy costs time and risks
  a double application, which can break an agency's right to represent.
- **Hidden eligibility.** Remote rules, allowed states, degree, licences, years, seniority and pay are buried in free
  text.
- **Unreliable sources.** A job board can belong to a different company with the same name. Postings go stale or are
  scams. Pay shown by aggregators is often an estimate.
- **Misleading titles.** Relevant work hides behind generic titles such as "Operations Specialist" or "Analyst II".
- **Privacy.** The profile, pay floor and applications are personal.
- **No feedback loop.** Nobody measures whether the screening agrees with the job seeker's own judgement.

## Solution

**Conversion over volume:** surface fewer, better-qualified openings, each with the reasons behind its place, so the
time goes into applying rather than screening. Five principles run through the design:
1. **Evidence for every decision.** Each rejection, flag and score is stored with its reason, the posting text it
   rests on, and fingerprints of the rules and profile in force.
2. **Conservative with sources.** Only four public listing APIs are read automatically, and a job board is admitted
   only when the employer's own website points to it.
3. **Unknown is never a match.** A fact the posting does not state is a flag to check, scored as neutral.
4. **Private by construction.** Personal data lives in a vault outside the repository; secrets live in the operating
   system's credential store.
5. **A person decides.** CareerOS prepares and explains; applying, messaging and anything that reaches outside need a
   person ([ADR 0007](docs/adr/0007-human-approval-at-selected-stages.md)).

## Key capabilities

| Capability | What it does |
|---|---|
| Discovery | Reads the public listing APIs of Greenhouse, Lever, Ashby and Workable. Also reads alert emails (Gmail API, IMAP or saved `.eml` files), a private recommendations inbox, and postings the job seeker pastes or saves. Links to sites that are never automated become leads for a person to open. |
| Employer verification | Admits a board only after the employer's own website links to it and the board agrees about who it belongs to. New boards are quarantined until a validation scan passes. |
| Normalization | Remote scope (nationwide, restricted states, hybrid, onsite, international, unknown), state limits, pay ranges and units, employment type, title family, freshness |
| Deduplication | Requisition-aware identity across channels; sightings, reposts and closures; one application per role across duplicates and agency channels |
| Eligibility | Hard rules with the posting line quoted as evidence. Only a hard requirement can reject; unknowns are flags. |
| Scoring and routing | Fit, career value and freshness give an apply priority and a tier. A separate profile judges overnight and part-time work without touching the main decision. |
| Certifications | Every certification a posting names is recorded: which one, its issuer, how the posting words it, whether the job seeker holds it (held, in progress or planned), and whether the match is exact, related or by family. Only a held credential satisfies a requirement; certifications add no score points. `careeros certifications` shows demand across target jobs ([details](docs/certifications.md)). |
| Submission holds | Permission to submit kept apart from qualification: an employer-level hold, read from policy with the employer resolved through its aliases, is cleared only by your release of one posting and one channel, never by a score, a label or a recorded application. Nothing submits for you ([ADR 0007](docs/adr/0007-human-approval-at-selected-stages.md)). |
| Shadow scoring | A read-only second opinion on the tiers: Qualification and Opportunity scored separately, with unknowns kept visible as a range instead of a neutral guess, a status (Tier A/B, Check First, Stretch review, Near-miss, Archive) and queues. `careeros eval --scoring` compares it with production and a simpler baseline on a read-only snapshot, one role counted once, and draws blind calibration lists for unbiased estimates. It changes no production tier; nothing is adopted without a prospective check ([details](docs/shadow_scoring.md)). |
| Experience gaps | Shadow-mode experience-gap analysis: each years requirement gets a 20-point Experience Match (shortfall, coverage, relevance) and the tier effect it would have. It is shown and measured against the job seeker's labels, and changes no tier until the owner turns it on ([details](docs/experience_match.md)). |
| Explainability | `careeros explain JOB` answers 14 questions about any job; `careeros sources explain` shows why a board is trusted |
| Employer intelligence | Stable employer records with aliases and official domains, three separate board scores, ATS migrations, provider outages, and a Career Fit score that sets how often each employer is scanned |
| Human review and tracking | A local review page, labels (apply, stretch, skip), a shortlist with the next step for each job, private application briefs drafted from the approved profile, application tracking through its own workflow, dismissals with reasons, title-screen audits, a hiring funnel |
| Analytics | A validated star-schema export and a five-page Power BI report |
| Auditability | Workflow matrices, append-only history, a hash-chained audit log and `careeros verify` |
| Demo Mode | The whole system on synthetic data, offline and credential-free |

## Architecture

```mermaid
flowchart LR
    subgraph Sources["Outside sources (untrusted data)"]
        ATS["Job-board listing APIs<br/>Greenhouse · Lever · Ashby · Workable"]
        SITES["Employer websites<br/>(read only to verify boards)"]
        MAIL["Alert emails · recommendations ·<br/>pasted and saved postings"]
    end
    subgraph Engine["CareerOS engine (this repository)"]
        DISC["discovery and<br/>verification"] --> PIPE["pipeline: normalize ·<br/>deduplicate · extract · qualify · score"]
        PIPE --> REV["review: report · review page ·<br/>explain · metrics"]
        PIPE --> AN["analytics export"]
    end
    subgraph Vault["Private vault (outside the repository)"]
        DB[("SQLite store<br/>append-only history,<br/>hash-chained audit log")]
    end
    ATS --> DISC
    SITES --> DISC
    MAIL --> DISC
    Engine <--> DB
    REV --> P["A person decides"]
    AN --> PBI["Power BI report"]
```

- **One local store.** SQLite, with each component limited to the tables (and some columns) it owns, workflow
  matrices for every state change, append-only history enforced by triggers, and idempotency receipts
  ([ADR 0008](docs/adr/0008-local-store-with-enforced-duties-and-audit-chain.md)).
- **Policy as configuration.** Engine defaults in `config/policy/defaults.yaml`, merged with the job seeker's private
  overrides. Every decision stores the fingerprint of the policy and profile it was made under, so a change re-evaluates
  exactly the affected jobs.
- **Same code, two modes.** Private mode uses the vault and real sources; Demo Mode uses a throwaway vault and a
  synthetic world ([ADR 0004](docs/adr/0004-demo-mode-is-isolated.md)).

Details: [architecture](docs/architecture.md), [data lineage](docs/data_lineage.md), [job lifecycle](docs/job_lifecycle.md)
and the [decision records](docs/adr/README.md).

## Workflow

The daily run (`careeros run`, started once a day by Windows Task Scheduler) takes a single-writer lock and:
1. scans every admitted board, within a host allowlist and a request budget, and records each scan;
2. reads new alert emails and recommendations, if connected; links become graded jobs or leads;
3. verifies candidate boards and re-verifies boards that are due;
4. processes only what changed: normalize, deduplicate, read requirements, apply the hard rules, score, route;
5. writes the review page, a sanitised seen-jobs index and a verified backup, and notifies if something is worth a look.

```mermaid
flowchart LR
    S["Source"] --> I["Ingest"] --> N["Normalize"] --> V["Verify employer<br/>and board"] --> D["Deduplicate"]
    D --> E["Eligibility<br/>(hard rules)"] --> F["Score"] --> R["Route<br/>(tier, overnight profile)"]
    R --> X["Evidence"] --> O["Review page"] --> P["Person:<br/>label, apply, dismiss"]
```

A failure in one board, email, issue line or job is recorded as an exception and never stops the run. Each job moves
only through the transitions in `config/workflows/job.json`, and each transition writes its history row, the job's
current state and an audit event in one database transaction.

## Employer verification

A board found anywhere (an alert, research, a guessed name) is only a **candidate**. CareerOS reads the employer's own
website and admits the board only when the employer itself points to it
([ADR 0001](docs/adr/0001-official-employer-sites-are-authoritative.md)):
1. **Handshake.** The employer's homepage and careers page link to, embed or redirect to the board.
2. **Identity.** The board agrees: its own name, the official domain in its postings, or postings that name the
   employer. A board that names another company is rejected as an identity mismatch.
3. **Board Trust at least 85**, with every mandatory check passed. A score never replaces a missing handshake; 65 to 84
   goes to a person.
4. **Quarantine and a validation scan** before the first daily scan
   ([ADR 0003](docs/adr/0003-new-boards-are-quarantined.md)).

Three scores answer three different questions and never mix
([ADR 0002](docs/adr/0002-board-trust-health-and-career-fit-are-separate.md)):

| Score | Question | Built from |
|---|---|---|
| Board Trust | Is this the employer's board? | Verification evidence only |
| Board Health | Is it working? | Recent scan outcomes; provider outages are never held against an employer |
| Career Fit | Is the employer worth scanning often? | CareerOS's own history of relevant roles there: recency, volume, roles that reached a review tier, remote share, breadth |

Systems CareerOS never reads (Workday, iCIMS, SmartRecruiters and others) are recorded as manual-only
([ADR 0006](docs/adr/0006-unsupported-ats-systems-stay-manual.md)). When an employer moves to another provider, the old
board becomes history with its jobs kept and the new one is verified. A manual approval records a reason and expires,
and it can never override an identity mismatch, an unsafe address or a contradicting official site.

## Data quality

- Posting text is cleaned of hidden characters and escaped HTML before use.
- Links are canonicalised, so tracking parameters never create a new posting or board.
- Weaker sources (alerts, recommendations) never overwrite facts from the employer's own board.
- Pay is judged on the full range. Units are annualised. A range that straddles the floor, geographic tiers, or variable
  pay with no stated base is *unknown*, not a pass. Pay shown by aggregators never rejects a job.
- "Remote" alone is never read as nationwide; stated state limits are kept.
- Old postings are flagged as possibly stale, never hidden.
- A scan that suddenly returns far fewer postings is marked suspicious and closes nothing; only complete scans close
  jobs.
- Requirements text that cannot be read reliably goes to a person.
- Rows marked as drills or tests are excluded from every metric, and the analytics export reconciles with the
  application's own metrics before it writes anything.

The full list is DQ-001 to DQ-010 in the [business requirements](docs/business_requirements.md).

## Analytics

`careeros export` writes a star schema of 8 dimensions and 5 facts as 13 CSV files with a manifest. It validates the
structure and reconciles the figures with `careeros metrics` first; if any check fails, nothing is written. A private
export is refused inside the code repository.

The Power BI report (`powerbi/CareerOS.pbip`, with 46 measures) has five pages: Executive Overview, Employer
Intelligence, Job Pipeline, Role Intelligence and System Quality. The committed report, the sample export in
`samples/analytics_demo/` and the screenshots in `powerbi/screenshots/` all use **synthetic Demo Mode data**. Tables,
grain and measures: [data dictionary](docs/analytics_data_dictionary.md) and [dashboard spec](docs/powerbi_dashboard_spec.md).

## Security

| Area | Control |
|---|---|
| Private data | The vault must live outside the repository (enforced). Logs are redacted. |
| Secrets | Windows Credential Manager. The optional AI reader, off by default, also accepts an `ANTHROPIC_API_KEY` environment variable. Never in the repository, database, reports or logs |
| Untrusted content | Postings, emails, issues and web pages are data: escaped, never executed, and unable to change policy, scores or what is fetched |
| Employer websites | An SSRF-safe fetcher: https only, public addresses only, connections pinned to the checked address, every redirect re-checked, robots.txt, size and type caps |
| Job-board clients | Only the four listing-API hosts, redirects included, within request budgets |
| Local review page | 127.0.0.1 only, a secret token, a Host check, one-time form keys |
| Sites never automated | LinkedIn, Indeed, Glassdoor, aggregators and manual-only systems are never fetched. There is no CAPTCHA handling. |
| Acting on someone's behalf | No code applies or contacts an employer or recruiter. The only outward writes are replies and a sanitized index in the owner's own private GitHub inbox. |
| Releases | `tools/release_scan.py` finds secrets and personal data and never prints a matched value |
| Dependencies | Hash-locked development set (wheels only; every release at least 14 days old, except two security fixes recorded in the lock's header) and pip-audit |

Every control in the threat model is tied to a test and to a planted defect that the tests must catch. See
[SECURITY.md](SECURITY.md), [PRIVACY.md](PRIVACY.md) and the [security architecture](docs/security_architecture.md).

## Testing

| Layer | What it shows |
|---|---|
| 946 automated tests | Unit and integration tests on synthetic fixtures, fake transports and fixed clocks, run with outbound network refused (`tools/offline_tests.py`) |
| 749 planted defects | `tools/mutate.py` breaks each safety rule in a scratch copy, and the test suite must then fail. A defect that survives is an untested rule. |
| Functional MVP gate | `tools/mvp_gate.py` runs the real command line in Demo Mode and makes 92 checks against expectations written from the postings and the policy, not read back from the system. Result: no requirement fails. Of 13, 10 are PASS and 3 are PARTIAL, with the gaps listed in the [report](docs/functional_mvp_report.md). |
| Documentation tests | The documents' named files, tests, tables and commands must exist |
| Static checks | ruff (pyflakes, bugbear and security rules), mypy against a recorded baseline of older errors, configuration checks |
| Release and supply chain | The release scan for secrets, and pip-audit of both dependency locks |

How the planted-defect runs work, in plain terms, is in [testing](docs/testing.md).

One command runs it all, the same command the CI workflow (`.github/workflows/ci.yml`) runs:

```
.venv-dev\Scripts\python.exe tools\validate.py            # lint, format, types, config, tests, Demo Mode, security
.venv-dev\Scripts\python.exe tools\validate.py --full     # plus every planted defect (slow: the suite runs once per defect)
```

## Demo Mode

`careeros demo tour` runs four simulated days of the real daily run on 15 fictional employers, their job boards and
their websites (`*.example.com`), plus an alert email, a pasted posting and a fictional job seeker. It runs offline in
a throwaway demo vault. While Demo Mode is on, the private vault cannot even be located, and the real network,
credentials, mail, GitHub, the clipboard, the AI reader and notifications are all refused. The tests prove it under a
"jail" that records any attempt to leave.

**Synthetic results** (fictional data, fixed by the tests): 45 jobs, of which 27 scored, 13 rejected with stated
reasons, 1 duplicate and 4 closed; 13 boards, of which 8 active, 1 rejected as an impostor and 1 awaiting review. These
numbers describe a hand-written world chosen to exercise every rule, not a real job market. See
[Demo Mode](docs/demo_mode.md) and the [reviewer walkthrough](docs/demo_walkthrough.md).

## Tech stack

| Area | Choice |
|---|---|
| Language and runtime | Python 3.12, standard library first |
| Storage | SQLite in WAL mode, 14 forward-only migrations |
| Dependencies | httpx (HTTP), PyYAML (policy), anthropic (an optional AI requirement reader, used only when an API key is stored) |
| Review page | The standard library's HTTP server, bound to 127.0.0.1 |
| Analytics | CSV star schema; Power BI project files (TMSL model and PBIR report) generated by `tools/build_powerbi_project.py` |
| Operations | Windows Task Scheduler, Windows Credential Manager, Windows notifications |
| Quality | unittest, a mutation tool, ruff, mypy, pip-audit, GitHub Actions |

## Quickstart

The hash-locked development set (`requirements-dev.lock`, wheels only) installs on CPython 3.12 on Windows x64 or
Linux x64, and on nothing else yet: Windows 10 and 11 are where the owner runs CareerOS, and the Linux install is
verified by the CI job. On macOS, or with another Python version, the install fails because no hashed wheel matches;
the [reviewer walkthrough](docs/demo_walkthrough.md) says what can be checked without installing.

```
python -m venv .venv-dev
.venv-dev\Scripts\python.exe -m pip install --require-hashes --only-binary :all: --no-deps -r requirements-dev.lock
.venv-dev\Scripts\python.exe -m careeros demo tour
```

The commands are written for Windows. On Linux, use `python3` to create the environment and `.venv-dev/bin/python`
wherever they say `.venv-dev\Scripts\python.exe`.

Then explore the result with Demo Mode on (Command Prompt syntax; in PowerShell use `$env:CAREEROS_DEMO_MODE = "true"`;
on Linux, `export CAREEROS_DEMO_MODE=true` to switch it on and `unset CAREEROS_DEMO_MODE` to switch it off):

```
set CAREEROS_DEMO_MODE=true
.venv-dev\Scripts\python.exe -m careeros explain greenhouse:meridianrx:101
.venv-dev\Scripts\python.exe -m careeros sources health
.venv-dev\Scripts\python.exe -m careeros review          # the review page; Ctrl+C stops it
set CAREEROS_DEMO_MODE=
```

And run every check: `.venv-dev\Scripts\python.exe tools\validate.py --offline` (on Linux,
`.venv-dev/bin/python tools/validate.py --offline`).

The [reviewer walkthrough](docs/demo_walkthrough.md) goes from here through every part of the system in about ten
minutes, with the output to expect. In the documentation, `careeros <command>` means
`.venv-dev\Scripts\python.exe -m careeros <command>`, run from the repository folder.

Using CareerOS for a real search needs a vault folder outside the repository (`CAREEROS_VAULT`), `careeros init`, an
approved profile and policy overrides in the vault, and `careeros run` once a day.

## Limitations

- **Single user, local, Windows.** The full validation also passes on Linux in CI, where that job is informational;
  the daily-run setup (Task Scheduler, Credential Manager, notifications) is Windows-only. The hash-locked install is
  for CPython 3.12 on Windows x64 or Linux x64 only; macOS is unsupported and untested.
- **CI runs only on the public repository.** The private development repository has no remote. The first run
  (2026-10-15) passed on Windows (required) and Linux (informational).
- **Four providers.** Only Greenhouse, Lever, Ashby and Workable are read automatically. Other systems are manual
  by design, and LinkedIn, Indeed and Glassdoor are never automated.
- **Static pages only.** A careers page that builds its job list with JavaScript cannot prove a board; such boards
  wait for a person.
- **Rules-based reading.** Requirements are read by deterministic rules. The AI reader is optional, needs an API key,
  and is switched off in the owner's own setup.
- **Ranking effectiveness not demonstrated (measured 2026-10-16).** I labelled 50 real postings (22 Apply, 11
  Stretch, 17 Skip), and a calibration compared the decisions of the scoring code published on 2026-10-16 with them.
  The hard rules agreed with my 50 labels (in-sample): they rejected 6 labelled jobs, all labelled Skip, and none of
  the 33 Apply or Stretch jobs. The ranking did not: none of the 22 Apply jobs reached the top tiers (A/B), and scored
  Apply and Skip jobs had the same average priority to one decimal place (70.3), although eligibility score and skill
  coverage did separate them. The sample is small, comes from one person, mostly on jobs the review page showed, and
  no tier rule in this repository has been changed in response or checked on new labels
  ([shadow scoring](docs/shadow_scoring.md) is read-only and not adopted). The Phase 1 exit test asks for at least 50
  labels with measured agreement. Not claimed: the coded thresholds were met, but the precision figure rests on one
  top-tier job, the recall figure rests on a private near-miss setting, and nothing in the test counts how many wanted
  jobs reach the top tiers ([calibration](docs/case_study.md#ranking-calibration-identify-measure-improve-validate)).
  The source-expansion targets are provisional.
- **Known gaps.** The Functional MVP gate grades 3 of 13 requirements PARTIAL (5 small gaps, listed in its report).
  Lead matching still relies on an older identity check. Type checking runs against a baseline of 164 older errors.
- **Generated, not mirrored.** The public repository's code is a snapshot generated from an allowlist, with fresh
  history ([release checklist](docs/public_release_checklist.md)). Its documentation and dependency locks were also
  updated directly in the public repository on 2026-10-29; the private development repository carries the same
  corrections. The private development repository's history still holds private settings and is never published.

## Roadmap

**Portfolio release:**
1. Close the remaining Functional MVP gaps, and hash-lock the runtime install.

**Product:**
- Improve the tier rule that the 2026-10-16 calibration found weak, test any change side by side on new labels before
  adopting it, and add to the exit test a measure of how many wanted jobs reach the top tiers; then measure the
  source-expansion pilot.
- Decide how to handle JavaScript careers pages (pending a review of a week of verification results).
- Review the lead-matching path; reduce the type-check baseline; decide whether the Linux CI job becomes required.
- Decide whether to enforce Experience Match once `careeros eval` reports it calibrated (50 labels, 20 of them with a
  years requirement).
- Assisted applications are built ([ADR 0009](docs/adr/0009-assisted-applications.md)): versioned packages prepared
  from the approved profile, and form filling only at the person's request; the person always submits. Filling stays
  off for each destination until its installed-extension check. Next: compare assisted with manual applications
  (`careeros assist report`), and build an adapter for another destination only where that shows time saved.

**Deferred:** automatic applications. CareerOS applies to nothing today. A later phase may let the job seeker promote a
channel to automatic submission after a clean record, under its own decision record
([ADR 0007](docs/adr/0007-human-approval-at-selected-stages.md)).

**Not planned:** hosted or multi-user deployment.
