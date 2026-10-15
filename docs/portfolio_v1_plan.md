# CareerOS Portfolio v1.0 plan (Milestone 1: readiness and architecture audit)

- Date: 2026-10-13
- Scope: audit and plan only; nothing was implemented in this milestone.
- Status: the plan of record from Milestone 1. What it says about the system describes 2026-10-13 before Prompt 2.
  The current assessment is `portfolio_mvp_report.md`.
- Baseline: 288 automated tests pass; the audit chain of the live database verifies clean; the last full mutation check
  caught 208 of 208 deliberate defects.

CareerOS is a job-search operations system. It collects job postings from public job boards, alert emails and
recommendations, and:
- normalizes them and removes duplicates;
- verifies that each job board really belongs to the employer;
- applies explicit eligibility rules;
- scores fit and routes each opening;
- records the evidence behind every decision;
- produces a daily review page and metrics.

It runs locally on one machine for one person. Today it is a private working system. This plan describes how to turn
it into a portfolio artifact that a hiring manager can run and understand, without touching any private data.

## 1. Current architecture

```
SOURCES                      PIPELINE (careeros/pipeline.py)                        OUTPUTS
public ATS listing APIs ──┐
  (Greenhouse, Lever,     │   ingest → normalize → dedupe / variants / openings     review page (HTML, local server)
   Ashby, Workable)       ├─► → title screen (+ second review) → requirement        Windows notification
alert emails (.eml, Gmail)│     extraction (rules; AI optional) → eligibility gate  metrics / funnel / eval (CLI)
GitHub-issue inbox        │     → scores + tiers → NIGHT routing → report            private drafts, backups
manual paste / import ────┘
             ▲
board discovery and verification (discovery/board_*.py, safe_fetch, site_inspect)
SQLite store (db/store.py): per-component write permissions, workflow transition matrices, hash-chained audit events,
append-only history tables, idempotency receipts, single-writer lock
```

| Layer | Where | Notes |
|---|---|---|
| Entry point | `careeros/cli.py` (1,400 lines) | 29 commands; daily `run` = discover → Gmail → inbox → discovery → board maintenance → process → report → backup |
| Ingestion | `discovery/boards.py`, `http.py`, `alerts.py`, `gmail.py`, `imap.py`, `github_inbox.py`, `saved.py`, `manual_intake.py` | public ATS APIs through an allow-listed, budgeted client; email and inbox links become leads |
| Normalization | `normalize/posting.py` | remote type, state limits, remote scope, country, pay, employment type, deadline, title family |
| Dedup / identity | `pipeline.py`, `openings.py`, `seen_index.py` | content fingerprints, requisition-aware dedupe, variants, observations, repost / duplicate links |
| Employer and source verification | `discovery/board_discovery.py`, `board_registry.py`, `board_verifier.py`, `safe_fetch.py`, `site_inspect.py`, `source_health.py`, `employer_intel.py` | employer records, official-site handshake, Board Trust / Career Fit / Board Health, quarantine, validation, re-verification, migration, provider outages |
| Requirements | `extract/rules.py`, `extract/ai.py` | rules reader always; AI reader optional (off by default; quotes cross-checked) |
| Eligibility | `qualify/gate.py`, `facts.py`, `title_rescue.py`, `profile.py` | hard rules with stated reasons; unknowns are flags, never matches |
| Scoring / routing | `qualify/scores.py`, `qualify/night.py` | eligibility, fit, career value, tiers A / B / near-miss / archive; separate overnight-work profile with two modes |
| Review / tracking | `review/*.py` | report, local review server, labels, applications, funnel, shortlist, drafts, dismissals, audits |
| Operations | `ops/backup.py`, `ops/notify.py`, `tools/run_daily.cmd` | backups, notifications, Windows Task Scheduler |
| Data | `db/schema/001-012` | 12 forward-only migrations |
| Configuration | `config/policy/defaults.yaml`, `site_policy.yaml`, `workflows/*.json`, `employers.yaml` | private values overridden from the vault |
| Private data | `../vault` (outside the repo; enforced in `paths.py`) | profile, overrides, database, snapshots, reports, logs, backups |
| Quality | `tests/` (21 files, 288 tests), `tools/mutate.py` (210 deliberate defects), `careeros verify` | no CI yet; no git remote |

## 2. Portfolio readiness matrix

Status key: READY = demonstrable as is (with demo data); NEEDS HARDENING = works, but needs changes before a
reviewer sees it; NEEDS BUILDING = missing; PRIVATE ONLY = stays out of the portfolio; POST-MVP = not needed for v1.0.

| Area | Status | Evidence / gap |
|---|---|---|
| ATS ingestion (4 providers) | READY / NEEDS HARDENING | Tested with fake transports. A demo needs offline synthetic boards wired into a demo command. |
| Gmail / IMAP intake | PRIVATE ONLY (code shareable) | The code is generic and tested with fakes. Credentials, queries and mail stay private; the demo uses `.eml` fixtures. |
| GitHub-issue inbox (recommendations) | PRIVATE ONLY (code shareable) | Tied to a private repo and `gh` login; the demo uses a fake runner. |
| Manual import / paste | READY | Synthetic files can be demonstrated. |
| Normalization | READY | Remote scope, state limits, pay units and employment type are covered by tests. |
| Deduplication / fingerprints / variants | READY | Requisition-aware; duplicate prevention is tested. |
| Employer identification and source verification | READY | Official-site handshake, three separate scores, SSRF-safe fetcher, 46 tests. A demo needs a fake employer website. |
| Requirement extraction (rules) | READY | Quotes verbatim evidence. |
| AI requirement reading | POST-MVP (optional) | Needs an API key; off by default; never required for the demo. |
| Eligibility rules | READY / NEEDS HARDENING | Rules are generic, but some code comments, defaults and tests carry the owner's real settings (see §5). |
| Fit / career-value scoring, tiers | READY | Component breakdown stored per score. |
| Day / night routing | READY | Two-mode overnight profile with its own results table. |
| Explainable decision evidence | NEEDS HARDENING | Evidence is stored (gate results, score components, events, verification evidence), but there is no single "why this decision" view per job. |
| Application tracking / labels / funnel | READY (private data) | The demo can show synthetic labels and applications. |
| Review page / server | NEEDS HARDENING | Some wording refers to the owner's personal settings. |
| Scheduling (Task Scheduler) | PRIVATE ONLY | Documented as operations; not part of the demo. |
| Backups / notifications | READY (Windows-specific) | Notifications are Windows-only by design; guarded on other platforms. |
| Observability | READY / NEEDS BUILDING | Events, exceptions, metrics, provider health and `verify` exist. A demo-facing summary is missing. |
| Analytics export / Power BI | NEEDS BUILDING | Metrics are JSON / CLI only; no tabular export or data model. |
| Demo mode + synthetic dataset | NEEDS BUILDING | Pieces exist (synthetic profile, posting fixtures, fakes, `CAREEROS_VAULT`, fixed clock) but no end-to-end demo command. |
| Automated tests | READY | 288 tests; mutation check; fixtures are synthetic except a few real settings (§5). |
| CI quality gates | NEEDS BUILDING | No `.github/`, no remote, no lint or type check configured. |
| Recruiter README / BRD / architecture / data flow / security docs / case study / demo script | NEEDS BUILDING | The current README is a personal operator manual. |
| Setup / reproducibility | NEEDS HARDENING | `pyproject.toml` and a pinned lock file exist; the launcher and docs assume Windows and the owner's folders. |
| Privacy isolation (vault) | READY | The vault must live outside the repo (enforced). |
| Public-release safety | NEEDS BUILDING | The repo content and git history contain private context (§5); no release scanner. |

## 3. Functional MVP gap

The required chain is SOURCE → INGESTION → NORMALIZATION → EMPLOYER IDENTIFICATION → SOURCE VERIFICATION →
DEDUPLICATION → ELIGIBILITY → FIT SCORING → ROUTING → EVIDENCE → OUTPUT, run on synthetic data. Every stage already
exists in code. What is missing is a reviewer-runnable path through them.

1. **Demo Mode:** `careeros demo` (init / run / explain / report / reset):
   - an isolated demo vault and a fixed clock, no network and no credentials;
   - synthetic sources: fake ATS boards, fake employer websites, a couple of alert emails, one pasted posting.
2. **A synthetic demo dataset** designed to exercise every stage:
   - an eligible strong fit, a hard-rule rejection, a pay unknown and a duplicate repost;
   - an overnight-route job, a scam posting;
   - a verified board, a board the employer's site contradicts, and a same-name impostor board.
3. **Per-job decision evidence:** one readable explanation per job covering source, verification, normalization,
   dedupe, rules hit (with quotes), score components and routing.
4. **A deterministic, golden-output end-to-end test** of the demo.

## 4. Portfolio MVP gap
On top of §3:
- **Documentation:** recruiter README, business requirements, architecture, data flow, security and privacy, setup,
  a 3-minute demo script, a case study.
- **Analytics:** a Power BI-ready export (tables plus data dictionary).
- **CI quality gates:** tests, demo run, release-safety scan, lint.
- **A public-release safety review and a sanitized public repository.**

## 5. Privacy risks (most serious first)

1. **Git history.** All eight commits contain the private context below, and the commit author email is a personal
   address. Publishing this repository, even after cleaning the current files, would publish the history. **The
   portfolio repository must be a fresh repository created from a sanitized snapshot, with no history.**
2. **Personal context in tracked files.** About 20 files mention personal settings: employer and agency names, a
   hold rule, an excluded employer, the home state and pay floor values. They include:
   - `README.md`, `config/site_policy.yaml`, `config/policy/defaults.yaml`;
   - `careeros/cli.py`, `qualify/gate.py`, `qualify/night.py`, `review/report.py`, `review/server.py`,
     `discovery/saved.py`, `discovery/alerts.py`, `discovery/safe_fetch.py`, `discovery/board_discovery.py`;
   - schema `008`, and several tests.
3. **Test fixtures with real settings.** Some tests copy the owner's approved settings (pay floor values, an excluded
   employer's name, held employer names) "as synthetic values". They must become invented values.
4. **README operator content.** It holds personal folder paths, workflow details and status notes.
5. **`config/employers.yaml`.** A list of real public employers is not secret, but it reveals the owner's job-search
   targets. Replace it with a demo seed list in the portfolio.
6. **Private integrations.** Gmail credentials (Windows Credential Manager) and the private GitHub inbox repo must never
   be needed by, or referenced from, the demo.
7. **Planning documents outside the repo.** Prompts, status logs and reports contain personal context. They stay
   private and are never copied in.
8. **Vault data.** Already outside the repo by enforcement. The demo must use its own throwaway vault.

Existing safeguards: the vault is outside the repo; secrets are in Windows Credential Manager only; a test fails on any
email address or API key in the repository; published inbox indexes are sanitized.

## 6. Technical risks

| Risk | Impact | Mitigation in this plan |
|---|---|---|
| `cli.py` is a 1,400-line command module | harder to read for reviewers | keep for v1.0; add an architecture map; possible post-MVP split |
| Large policy file (`defaults.yaml`) | rules are hard to see at a glance | business-rules catalogue in the requirements doc |
| Windows-centric operations (launcher, scheduler, notifications, credentials) | reviewers on macOS / Linux | the demo path must be cross-platform; CI on Ubuntu and Windows |
| Live ATS adapters depend on third-party APIs | the demo must not | the demo uses recorded synthetic responses |
| Static-HTML board verification cannot see job lists built with JavaScript | some real boards stay unproven | documented limitation; a post-MVP option is pending a real-world review |
| No lint or type checking | style drift, latent errors | add a linter in CI (dependency decision in M6) |
| Forward-only migrations | rollback is restore-from-backup | documented; backups before migrations |
| Time-dependent behaviour | non-deterministic demo output | fixed clock in demo mode |
| Custom mutation tool | unfamiliar to reviewers | document it; run a subset in CI |

## 7. Recommended milestone sequence
Each milestone ends with tests, fixes, a change report, open issues and a suggested commit message, then stops.

| # | Milestone | Outcome |
|---|---|---|
| M1 | Readiness and architecture audit | this plan |
| M2 | Privacy hardening and public / private boundary | private context moved out of code, config and tests (synthetic values instead); operator manual moved to the private folder; a release-safety scanner (private denylist kept outside the repo, plus email / secret / path patterns) with tests; export-by-snapshot procedure defined |
| M3 | Demo Mode and synthetic dataset | `careeros demo`: offline, credential-free, deterministic, covering all 11 stages; golden end-to-end test |
| M4 | Decision evidence and explainability | `careeros explain JOB` and decision cards on the review page; evidence export |
| M5 | Analytics export (Power BI-ready) | star-schema CSV tables, data dictionary, model relationships, sample measures; schema tests |
| M6 | CI quality gates | workflow: tests (Ubuntu and Windows), demo run, release-safety scan, lint; runs on the future public repository |
| M7 | Portfolio documentation | recruiter README, business requirements and rules, architecture, data flow, security and privacy, testing strategy, AI-assisted development and governance, setup, 3-minute demo, case study |
| M8 | Public-release safety review and sanitized publication | checklist; scanner clean; fresh repository from the snapshot; the owner's explicit approval before anything is published |

M2 comes first so that no later document or demo carries private context.

## 8. Definition of Done

**Functional MVP:** on a fresh clone, with no vault, network or credentials, `careeros demo run` completes and:
- processes the synthetic dataset through all 11 stages deterministically (identical output on repeat runs);
- shows at least one job of each decision type and both routes;
- explains any job with its evidence;
- passes a golden end-to-end test and the full test suite.

**Portfolio MVP:** Functional MVP, plus:
- **Docs present and accurate:** README, business requirements, architecture, data flow, security, setup, 3-minute
  demo, case study.
- **Analytics:** the export loads into Power BI per the data dictionary.
- **CI green:** tests, demo, lint, release scan.
- **Release safety:** the scanner reports zero findings on the snapshot; no private credentials or data are needed.
- **Fresh-reviewer test:** a reviewer can follow the setup on a clean machine in under 15 minutes.

**Every milestone:** all tests pass, no regression in the private system, mutation check still fully caught for the
code it touches, change report written.

## 9. Post-MVP features (do not block v1.0)
- Automatic or browser-based applications.
- AI requirement reading as a default, ML or predictive matching.
- JavaScript rendering, or reading employers' script files, for board verification.
- Employer hiring-velocity forecasting.
- More ATS providers.
- Multi-user, hosted or cloud deployment.
- Splitting `cli.py` into packages.
- Outcome prediction.

## 10. Items that remain private
- **The whole vault:** real profile, policy overrides, database, labels, applications, snapshots, reports, logs,
  backups, drafts.
- **Integrations:** Gmail / IMAP credentials, queries and mail; the GitHub recommendation inbox repo and its issues.
- **Lists and reasons:** excluded and held employer lists and their reasons; curated employer lists.
- **Documents:** resume, prompts, planning and status documents outside the repo.
- **This repository's git history, and the personal commit email.**
- **Machine setup:** the Task Scheduler setup and the owner's folder layout.
