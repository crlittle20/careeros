# CareerOS: reviewer walkthrough

- Version: 1.0
- Date: 2026-10-13
- Companions: `README.md` (Quickstart), `demo_mode.md`, `portfolio_mvp_report.md`.

This walkthrough shows each part of CareerOS working, in Demo Mode, in about ten minutes. Everything you see is
**synthetic**: fictional employers on `example.com`, a fictional job seeker, and four simulated days. Nothing reaches
the internet, and no credential is needed.

**Before you start** (Windows, Python 3.12), set up once from the repository folder, as in the README:

```
python -m venv .venv-dev
.venv-dev\Scripts\python.exe -m pip install --require-hashes --only-binary :all: --no-deps -r requirements-dev.lock
```

In this document, `careeros <command>` means `.venv-dev\Scripts\python.exe -m careeros <command>`, run from the
repository folder.
After the tour, switch Demo Mode on for the other commands:
- Command Prompt: `set CAREEROS_DEMO_MODE=true`
- PowerShell: `$env:CAREEROS_DEMO_MODE = "true"`

The output excerpts below are checked against the real commands by `tests/test_demo_mode.py`, so they stay true.

## The 3-minute demo

| # | Command | What it shows |
|---|---|---|
| 1 | `careeros demo tour` | Four simulated days of the real daily run, in seconds |
| 2 | `careeros sources list` | Every job board and its verification status, including an impostor that was rejected |
| 3 | `careeros sources explain greenhouse:quillon` | Why that board was rejected: the employer's site links to it, but the board names another company |
| 4 | `careeros explain greenhouse:meridianrx:102` | A duplicate caught by its requisition number |
| 5 | `careeros explain greenhouse:meridianrx:101` | Every decision behind one job, with its evidence |
| 6 | `careeros review` | The page a person works from (Ctrl+C stops it) |

## The full walkthrough

### 1. Understand the business problem

Read the README's **Business problem** and **Solution**, then sections 1 and 2 of `case_study.md`. The formal version is
section 1 of `business_requirements.md`.

### 2. Understand the architecture

Read the README's **Architecture** and **Workflow**, then `architecture.md`, which has diagrams of the components, the
daily run, the job lifecycle, verification and Demo Mode. Each major choice has a decision record in `adr/`.

### 3. Install the portfolio build

Run the two setup commands above. They install 51 hash-checked packages into `.venv-dev` and leave the rest of your
system alone. Expect a minute or two.

### 4. Initialize Demo Mode

`careeros demo tour` runs `careeros demo init` for you. `init` creates a throwaway demo vault (by default in your temp
folder) with a marker file, a fictional profile and policy, and six existing job boards. `careeros demo reset` deletes
the vault again, but only a folder that carries the marker.

### 5. Process synthetic jobs

The tour runs four days of `careeros run`: board scans, an alert email, a pasted posting, board discovery and
verification, then processing. Afterwards, `careeros status` shows the totals:

```
jobs: scored=27, rejected=13, closed=4, duplicate=1
NIGHT profile: on; supplemental/archive=3, supplemental/near_miss=10
```

### 6. Observe employer verification

`careeros sources list` shows each board's status. Among them:

```
greenhouse:harborline   historical        Harborline Health
greenhouse:quillon      rejected          Quillon Health
ashby:pinecrest         review_required   Pinecrest Care Navigation
```

Harborline moved to another provider, so its old board is history. Quillon is the impostor. Pinecrest's careers page
shows no board link, so a person decides. `careeros sources explain greenhouse:quillon` prints the evidence chain:

```
status=rejected
identity=mismatch
board names another company: Quillon Logistics
```

`careeros sources reviews` lists the open review cases, and `careeros sources health` shows trust, health and Career Fit
side by side.

### 7. Observe duplicate prevention

`careeros explain greenhouse:meridianrx:102`:

```
decision: duplicate
of: greenhouse:meridianrx:101
reason: same_requisition
```

### 8. Observe scoring

`careeros explain greenhouse:meridianrx:101` shows the **ELIGIBILITY** facts, each with the posting text it rests on.
**FIT** and **CAREER VALUE** show every scoring component with its basis, and **ROUTING** shows the result:

```
tier: near_miss
apply_priority: 80.7
```

The job's **SOURCE** names the alert email that brought it in first; its sightings list every channel that saw it.

### 9. Observe routing

`careeros shortlist` lists the jobs worth applying to, each with its tier and next step, for example:

```
Pharmacy Claims Analyst | Meridian Rx | open | tier B ELIGIBLE
```

The `careeros status` line in step 5 shows the separate overnight and part-time profile (NIGHT). `careeros review` shows
both routes on one page: Tier B, near-miss, "check before applying", and the NIGHT section.

### 10. Inspect evidence

- `careeros explain greenhouse:meridianrx:101 --json`: the same 14 answers, as data.
- `careeros verify` checks the hash-chained audit log and every job's history:

  ```
  "event_chain_breaks": []
  ```

- Each day's HTML report and verified backup are in the demo vault's `reports` and `backups` folders.

### 11. Export analytics

`careeros export` writes 13 CSV files and a manifest into the demo vault, after its own checks:

```
all 78 checks passed
```

To see the Power BI report:
- without Power BI: the five screenshots in `powerbi/screenshots/`;
- with Power BI Desktop: open `powerbi/CareerOS.pbip`, set
  **Transform data → Manage parameters → ExportFolder** to this repository's `samples\analytics_demo` folder, and
  refresh.

### 12. Run tests

```
.venv-dev\Scripts\python.exe tools\validate.py --offline
```

It runs lint, formatting rules, type checks, configuration checks, every test (with the network refused), the
Functional MVP gate and the release scan, and ends with `Validation: PASS (7 checks)`. It takes a few minutes.

### 13. Understand privacy controls

Read the README's **Security**, then `PRIVACY.md`. Section 3 of `demo_mode.md` lists what Demo Mode refuses. Try one:
`careeros gmail status` with Demo Mode on ends with

```
Refused: Gmail is not available in Demo Mode
```

**When you are done:** run `careeros demo reset`, and switch Demo Mode off (`set CAREEROS_DEMO_MODE=` or
`Remove-Item Env:CAREEROS_DEMO_MODE`).
