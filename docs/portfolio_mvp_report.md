# CareerOS: Portfolio MVP report

- Version: 1.0
- Date: 2026-10-13
- Prompt: Portfolio Prompt 12, Portfolio MVP gate.
- Assessed: commit `e5db6df`, plus the fixes in section 6.
- Definition: the Portfolio MVP in `portfolio_v1_plan.md` (section 8), and the 13 things Prompt 12 says a fresh reviewer
  must be able to do.
- Companions: `functional_mvp_report.md`, `demo_walkthrough.md`, `public_release_checklist.md`.
- Updated: 2026-10-15. B1 is resolved (refactor R4 and two later privacy passes). B2 has one step left: the first
  green CI run on the public repository.

## 1. Verdict

**Verdict: NOT PASSED.** At the gate (2026-10-13) the build was ready but could not reach a reviewer. Since then B1 is
resolved, and B2 has one step left: the gate passes with the first green CI run on the public repository.

- **What works:** a fresh reviewer can do all 13 things Prompt 12 lists. I followed the README in a clean clone, with no
  vault, no credentials and no network, and every step worked.
- **What blocked the gate:** two release blockers (section 5):
  - **B1.** 25 files the public snapshot would ship still held personal settings;
  - **B2.** no public snapshot existed, so no reviewer could obtain the build, and CI had never run.

  At the gate both were planned release work (the preparatory refactor and the snapshot in
  `public_release_checklist.md`), and both needed the owner's decisions, so they were not fixed during the gate.
- **What was fixed during the gate:** nine SHOULD FIX issues, including the missing reviewer walkthrough, a demo refusal
  that printed a traceback, two security hardening findings and three inaccurate statements in the documents
  (section 6).

| Definition of Done item | Status |
|---|---|
| Functional MVP: the demo runs on a fresh clone, offline and credential-free, deterministic, all stages, explainable, golden test | **Met.** The Functional MVP gate grades 10 PASS, 3 PARTIAL, 0 FAIL |
| Documents present and accurate: README, requirements, architecture, data flow, security, setup, 3-minute demo, case study | **Met** after this gate added the walkthrough (`demo_walkthrough.md`) and corrected three statements |
| Analytics: the export loads into Power BI as the data dictionary describes | **Met.** Checked in Power BI Desktop on 2026-10-13 (`powerbi_dashboard_spec.md`, section 6) |
| CI green: tests, demo, lint, release scan | **Not met at the gate.** The workflow and its command exist and pass locally; CI first runs on the public repository (B2) |
| Release safety: the scan of the snapshot finds nothing, and no private data or credentials are needed | **Not met at the gate:** no credentials were needed, but 25 shippable files held denylisted terms (B1). **Met since:** the scan of the snapshot finds only the GitHub username the owner chose for `LICENSE`, and five screenshots checked by eye. |
| Fresh-reviewer test: setup on a clean machine in under 15 minutes | **Partly shown.** A clean clone on this computer took about 5½ minutes of machine time, from clone to a passing validation. A person on a different machine has not tried it yet. |

**Independence.** The same AI that built CareerOS made this assessment, so it is not independent. To reduce the bias:
- I worked as a reviewer would: a clean clone, only the README and the documents, outputs recorded;
- every claim the documents make about the demo is now checked against the real commands by a test;
- each grade below 90 says why.

A walkthrough by the owner, or by another person or model, on a different computer is the missing independent check.

## 2. The 13 reviewer tasks

The method: clone the repository into an empty folder, with no vault beside it. Create `.venv-dev` from
`requirements-dev.lock`, hash-checked; the wheels came from the copies verified on 2026-10-13, so the install did not
download anything. Then follow the README and `demo_walkthrough.md`. All 13 tasks succeeded.

| # | Task | How | Evidence |
|---|---|---|---|
| 1 | Understand the business problem | README **Business problem** and **Solution**; `case_study.md` sections 1-2; `business_requirements.md` | The positioning and problem are on the first screen of the README |
| 2 | Understand the architecture | README **Architecture** and **Workflow** (two diagrams); `architecture.md`; eight ADRs | Diagram blocks and links are checked by `test_docs` |
| 3 | Install the portfolio build | The README's two setup commands | 51 packages installed, hash-checked, in 96 seconds |
| 4 | Initialize Demo Mode | `careeros demo tour` (runs `init`) | Demo vault created with its marker; the tour finished in 9 seconds |
| 5 | Process synthetic jobs | The tour's four days of `careeros run` | `careeros status`: `jobs: scored=27, rejected=13, closed=4, duplicate=1` |
| 6 | Observe employer verification | `careeros sources list`, `careeros sources explain greenhouse:quillon`, `careeros sources reviews` | 13 boards with their status; the impostor's chain shows the handshake passed and identity failed (`board names another company`) |
| 7 | Observe duplicate prevention | `careeros explain greenhouse:meridianrx:102` | `decision: duplicate`, `of: greenhouse:meridianrx:101`, `reason: same_requisition` |
| 8 | Observe scoring | `careeros explain greenhouse:meridianrx:101` | Every fit and career-value component with its basis; `apply_priority: 80.7` |
| 9 | Observe routing | `careeros shortlist`, `careeros status`, `careeros review` | Tier B and near-miss jobs with next steps; 13 jobs routed to the overnight profile |
| 10 | Inspect evidence | `careeros explain ... --json`, `careeros verify`, the daily reports | 14 answers per job; `"event_chain_breaks": []` |
| 11 | Export analytics | `careeros export`; the Power BI files | 13 CSV files and a manifest; `all 78 checks passed` |
| 12 | Run tests | `tools\validate.py --offline` | `Validation: PASS (7 checks)` in about 3 minutes |
| 13 | Understand privacy controls | README **Security**, `PRIVACY.md`, `SECURITY.md`, `demo_mode.md` section 3 | `careeros gmail status` in Demo Mode now ends with `Refused: Gmail is not available in Demo Mode` |

The review page (`careeros review`) was also opened in Demo Mode. Its sections render on synthetic data, and a request
without its token is refused (HTTP 403).

**Friction found on the way** (all fixed, section 6):
- there was no guided walkthrough;
- a refusal printed a traceback;
- the documentation writes `careeros <command>`, which is not installed as a command;
- the Power BI project would not refresh on another computer without changing a parameter, and nothing said so.

## 3. What was reviewed

| Area | Verdict | Notes |
|---|---|---|
| README | Good | The 16 required sections; claims checked by `test_portfolio_docs` |
| Requirements and traceability | Good | 113 numbered requirements; tests cited in the traceability are checked to exist |
| Architecture | Good | Diagrams, eight ADRs with alternatives; `portfolio_v1_plan.md` now says it is a historical plan |
| Demo Mode | Good | Offline, deterministic, isolated under a jail; three demo scenarios still missing (G4, G5) |
| Analytics | Good | Validated and reconciled export; five-page report checked in Desktop; cosmetic issues remain |
| Security | Good | Threat model with tests and planted defects; R1 and R2 fixed here; personal settings still in code (B1) |
| CI | Written; first runs on the public repository | No secrets, a read-only token, actions pinned to commits (B2) |
| Tests | Strong | Tests, planted defects, the gate, the network refused, documentation tests, fresh-clone runs |
| Quickstart | Good | Works as written on Windows; other platforms unverified |
| Case study | Good | Every figure labelled as engineering evidence, SYNTHETIC or observed; no outcomes claimed |

## 4. Grades

| Category | Grade | Why below 90 |
|---|---|---|
| Business Value | 80 | The problem and the objective are clear, and the system runs daily on a real search. But its value is argued, not yet measured: the Phase 1 exit test (at least 50 labels with measured agreement) has not been reached, and the pilot's targets are provisional. |
| Architecture | 88 | Strong separation of duties, an audit chain, policy fingerprints, demo isolation and recorded decisions. But `cli.py` is a 1,400-line module that mixes commands with orchestration, operations are tied to Windows, and lead matching still uses an older identity check that board verification has replaced. |
| Functionality | 88 | All 13 reviewer tasks work, and the gate grades 10 of 13 requirements PASS. Five gaps remain: a bare "Remote" is not placed in the U.S. (G1); a failed suggestion is invisible (G2); stale review cases after a migration (G3); and two scenarios missing from the demo (G4, G5). |
| Data Quality | 87 | DQ-001 to DQ-010 are implemented and tested, and the export reconciles with the application. But G1 leaves some jobs' country unknown, lead matching relies on the older identity check, and the type-check baseline includes arithmetic on values that may be empty in the scoring modules. |
| Automation | 82 | The daily run, its failure isolation and one-command validation are automated. At the gate, CI had never run, Linux was unverified, the full planted-defect run was manual and slow, and the snapshot generator did not exist yet (built 2026-10-14). |
| Analytics | 86 | A validated star schema, a reconciled export, a five-page report with 46 measures, a byte-stable demo sample, and tests that hold the model to the export. But some chart subtitles show raw field names, one table scrolls sideways, the report needs Power BI Desktop on Windows, and it has only ever shown synthetic data publicly. |
| Security | 86 | Every control in the threat model has a test and a planted defect; the website fetcher was probed; the release scan never prints values; the supply chain is checked; CI has least privilege. R1 and R2 were fixed here. At the gate, personal settings remained in shippable code (B1, since resolved); the runtime install is not hash-locked (R3), and the lead-matching identity check is a residual risk. |
| Testing | 92 | |
| Documentation | 91 | |
| Demoability | 87 | Offline, credential-free, a nine-second tour, a walkthrough with checked output, and a review page. But only Windows is verified, a reviewer must install Python, the Power BI report needs Desktop, and `explain` prints dense JSON fragments that are hard to scan. |
| Recruiter Clarity | 84 | The positioning, problem, case study and AI disclosure are clear and honest. But the README is long and assumes some domain vocabulary (PBM, ATS, tiers, NIGHT). There is no short visual demo, a non-technical reviewer cannot try anything without Windows and Python, and nothing is public yet. |

## 5. Issues

### BLOCKER

| ID | Issue | Evidence | Path to resolution | Status |
|---|---|---|---|---|
| B1 | Personal settings were still in code, configuration and tests | With the private denylist, 25 files the snapshot would ship held denylisted terms: 8 in `careeros/`, 2 in `config/`, 12 in `tests/`, plus `tools/mutate.py`, the mutation receipt and the Power BI model. No secret was found anywhere. | The preparatory refactor in `public_release_checklist.md`, section 1 | **Resolved 2026-10-14** (R4): the entries live in the vault (`policy/private_sites.yaml`, `overrides.yaml`) and the tests use fictional values. Since then the mutation receipt has been rebuilt without the term, and the snapshot gives the Power BI model a neutral folder. |
| B2 | No public snapshot exists, so no reviewer can obtain the build, and CI has never run | No remote; the snapshot generator is not built; CI is meant to run first on the snapshot (the owner's decision, 2026-10-13) | After B1: build the snapshot generator, generate the snapshot, review every file, publish with the owner's approval, and see CI green on Windows and Linux | Open, one step left: the generator is built (`tools/build_snapshot.py`, 2026-10-14) and the snapshot passes its release checks. It is published only with the owner's approval, and B2 closes with its first green CI run on the public repository |

### SHOULD FIX

| ID | Issue | Status |
|---|---|---|
| S1 | No reviewer walkthrough or 3-minute demo, although the Definition of Done requires one | **Fixed:** `demo_walkthrough.md`, with its output excerpts checked by `test_demo_mode::test_the_reviewer_walkthrough_shows_what_the_commands_print` |
| S2 | A refusal in Demo Mode printed a Python traceback (G6) | **Fixed:** one line and exit code 2; the gate's failure-handling item is now PASS |
| S3 | The job-board and Gmail clients honoured proxy variables from the environment (R1) | **Fixed:** `trust_env=False`; tests and planted defects |
| S4 | Log redaction missed GitHub and Google token shapes (R2) | **Fixed:** five token shapes are redacted; test and planted defects |
| S5 | `PRIVACY.md` said nothing personal is written into the repository, and that the AI reader is off by default | **Fixed:** both corrected (B1 is stated openly) |
| S6 | The release checklist also called the AI reader "off by default" | **Fixed** |
| S7 | Opening the Power BI project on another computer needs a parameter change that only the private-data instructions mentioned | **Fixed:** `powerbi_dashboard_spec.md` section 6 and the walkthrough |
| S8 | The documentation writes `careeros <command>`, which a reviewer does not have as a command | **Fixed:** the README and the walkthrough explain `python -m careeros <command>` |
| S9 | `portfolio_v1_plan.md` reads as current ("no CI yet", "the README is an operator manual") | **Fixed:** a status line marks it as the Milestone 1 plan |
| S10 | G1: a posting whose location says only "Remote" is not placed in the U.S. from its text | Open: a rule change that re-evaluates every live job on the next run, so the owner should choose when |
| S11 | G2 and G3: a failed suggestion is invisible, and review cases stay open after a completed migration (visible in `careeros sources reviews`) | Open: it changes the report and the review-case lifecycle of the live system; best done in a focused milestone with G4 and G5 |
| S12 | G4 and G5: the demo shows no board contradicted by its employer's own site, and no overnight job that the overnight profile accepts | Open: it changes the demo world, its recorded totals, the committed analytics sample and the Power BI files, and re-saving the `.pbix` and screenshots needs Power BI Desktop |
| S13 | R3: the runtime install (`requirements.lock`) is not hash-locked, and its pydantic is newer than 14 days | Open: from 2026-10-23 pydantic 2.14.0 meets the 14-day rule; the owner decides whether to hash-lock the engine's own environment |
| S14 | Lead matching relies on the older identity check (`board_discovery._record_match`) | Open: a correctness review outside the demo, already recorded in `functional_mvp_report.md` section 6 |
| S15 | Power BI cosmetics: some chart subtitles show raw field names, and one table scrolls sideways | Open: with S12, since both need Power BI Desktop |
| S16 | `careeros explain` prints dense JSON fragments, and its SOURCE line names only the first channel | Open: presentation only; the walkthrough explains how to read it |

### POST-MVP

- Reading careers pages that build their job list with JavaScript (a decision due at the 2026-10-20 review).
- More ATS providers; AI requirement reading as a default.
- Splitting `cli.py`; working down the type-check baseline (164 older errors).
- macOS support, after Linux is verified by CI.
- A short screen recording of the walkthrough, for reviewers who will not install anything.

Deferred: automatic applications, to a later phase the owner would open (ADR 0007, amended 2026-10-14). Not planned:
hosted or multi-user deployment.

## 6. Fixed during this gate

| Change | Files | Checked by |
|---|---|---|
| Reviewer walkthrough, with output excerpts held to the real commands | `docs/demo_walkthrough.md`, `README.md` | `test_demo_mode::test_the_reviewer_walkthrough_shows_what_the_commands_print`, `test_docs` |
| A Demo Mode refusal is one line and exit code 2 | `careeros/cli.py` | `test_demo_mode::test_commands_with_demo_mode_on_use_the_demo_vault`, the gate, a planted defect |
| The job-board and Gmail clients ignore proxy variables | `careeros/discovery/http.py`, `careeros/discovery/gmail.py` | `test_gmail::test_the_job_board_client_ignores_the_environment`, `test_gmail::test_the_gmail_clients_ignore_the_environment`, three planted defects |
| GitHub and Google token shapes are redacted | `careeros/core/redact.py` | `test_core::test_github_and_google_token_shapes_are_removed`, two planted defects |
| Accuracy: privacy, the AI reader, Power BI on another computer, the old plan | `PRIVACY.md`, `docs/public_release_checklist.md`, `docs/powerbi_dashboard_spec.md`, `docs/portfolio_v1_plan.md` | Reading, and `test_docs` |
| The AI-assisted development disclosure now quotes the owner's own account of the process (given 2026-10-13), and the case study and README follow it | `docs/ai_assisted_development.md`, `docs/case_study.md`, `README.md` | `test_portfolio_docs` |
| Gate evidence regenerated after the G6 fix: 10 PASS, 3 PARTIAL, 0 FAIL | `evidence/functional_mvp.json`, `docs/functional_mvp_report.md` (section 8) | `test_portfolio_docs`, `tools/validate.py` |

After these changes and the refactor of 2026-10-14: 419 automated tests pass. There are 284 planted defects. The spot checks reported at this gate proved
nothing at the time: the tool's scratch copies lacked files some tests need, so every defect looked caught. After the
fix (2026-10-14), a full run caught 283 of them. The survivor exposed a weak test, which was strengthened; the final
full run caught all 284 (`evidence/mutation-receipt.json`). `tools/validate.py`
passes all seven checks.

## 7. Scope freeze

From here, nothing is added just because it would be interesting. Before v1.0, a post-MVP item is worth adding only if
it materially improves recruiter comprehension, demonstration quality, target-role relevance, security, credibility or
measurable business value. Measured that way, the work in order:
1. **B1, then B2**: nothing else matters to a reviewer until the build can be published safely.
2. **The Phase 1 exit test and its agreement measure** (business value, credibility): no code, just labels.
3. **S11, S12 and S15 as one milestone** (demonstration quality): the missing demo scenarios, the stale review cases
   and the report's cosmetics, with the Power BI files re-saved once.
4. **A short screen recording** (recruiter comprehension).
5. **S10, S13 and S14** (data quality, security) when the owner chooses.
