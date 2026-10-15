# CareerOS: Demo Mode

- Version: 1.0
- Date: 2026-10-13
- Companion to `business_requirements.md`, `requirements_traceability.md` and `job_lifecycle.md`.
- Code: `careeros/demo/` (`sandbox.py` is the boundary, `world.py` the synthetic data, `runner.py` the commands).
- Tests: `tests/test_demo_mode.py`.

Demo Mode is a safe way to show CareerOS working. It has three properties:
- it runs the **same production commands** as the daily run;
- it uses **synthetic data only**: fictional employers, job boards, employer websites, an alert email, a pasted posting
  and a fictional job seeker;
- it is **offline**: no real network, no credentials, no mail, no GitHub.

Everything it writes goes to a throwaway demo vault, which can be deleted at any time.

## 1. Quick start

```
careeros demo tour
```

`tour` resets the demo vault, sets it up again and runs all four simulated days. It then prints a summary: boards by
verification status, jobs by state, tiers, remote scopes, review cases, rejection reasons, discovery outcomes,
lifecycle events, leads, changes, and NIGHT routing. It takes a few seconds.

To explore the result, switch Demo Mode on and use the normal commands:

```
set CAREEROS_DEMO_MODE=true
careeros sources health
careeros explain greenhouse:meridianrx:101
careeros review
set CAREEROS_DEMO_MODE=
```

Every command run with Demo Mode on prints `[Demo Mode: synthetic data in <demo vault>]` first.

## 2. Commands

| Command | What it does |
|---|---|
| `careeros demo init` | Creates the demo vault with the demo marker file, the fictional profile and policy, the curated employer list, the inbox files, and six pre-existing ("legacy") job boards. It refuses a folder that exists, is not empty and has no demo marker. |
| `careeros demo run` | Runs the next simulated day: on day 1 `careeros import` (the alert email and the pasted posting), then `careeros run` (board scans, discovery, board verification, processing, report, backup). |
| `careeros demo run --all` | Runs every remaining day. |
| `careeros demo status` | Prints the summary for the days run so far. |
| `careeros demo reset` | Deletes the demo vault, but only if it carries the demo marker. |
| `careeros demo tour` | `reset`, `init`, `run --all`, `status`. |

All of them switch Demo Mode on by themselves.

| Setting | Meaning |
|---|---|
| `CAREEROS_DEMO_MODE=true` | Demo Mode for every command (`1`, `yes` and `on` work too). Commands then use the demo vault and the synthetic world at the demo's current day. |
| `CAREEROS_DEMO_VAULT=<folder>` | Where the demo vault lives. Default: `<temp>\careeros-demo-vault`. |

## 3. What Demo Mode guarantees

While Demo Mode is on, the rules below hold. Each is enforced in code and tested in `tests/test_demo_mode.py`. Each is
also covered by a deliberate-defect check in `tools/mutate.py`: the check breaks the rule and confirms that a test
then fails.

| Guarantee | How it is enforced |
|---|---|
| The private vault is never read or written. | Demo Mode works only on the demo vault. `paths.vault_dir()` refuses in Demo Mode, so no code can even locate the private vault. The demo vault may not be the private vault, sit inside it, contain it, or sit inside the code repository. |
| Nothing reaches the real network. | Every job-board client (`discovery/http.py`) and website reader (`discovery/safe_fetch.py`) gets the synthetic world's transport. Synthetic hostnames resolve to one documentation address, and other names do not resolve. A client created in Demo Mode without a synthetic world loaded fails; it never falls back to the internet. |
| No credentials are read. | `WindowsCredentials()` refuses in Demo Mode. |
| Gmail is never read. | `careeros gmail` refuses. The daily run skips mail. The Gmail and IMAP clients refuse to start. |
| GitHub is never contacted. | `careeros inbox` refuses. The daily run skips the recommendations inbox. The seen-jobs index is written locally and never published. Every GitHub CLI call refuses. |
| No other private source is read. | `careeros paste --clipboard` refuses, because the clipboard may hold anything. Pasting from a file or from standard input still works. |
| The AI reader is never used. | `build_reader` returns none in Demo Mode, so no API key is read and no model is called. |
| Nothing pops up. | Notifications are off in Demo Mode. |
| Only demo vaults are deleted. | `reset` deletes a folder only if it carries the `.careeros-demo` marker. |

The tests run the whole tour under a **jail** that records any attempt to:
- send real HTTP;
- resolve a name or open a socket;
- start another program;
- read or write a credential;
- open IMAP;
- read the AI key;
- launch a browser.

The tour runs next to a decoy private vault that holds a unique marker string. The tests then prove:
- the jail recorded nothing;
- every request went to one of the four job-board API hosts or a `*.example.com` site;
- the decoy vault is unchanged (same files, bytes and timestamps);
- the marker string appears nowhere in the demo vault.

## 4. The synthetic world

Fifteen fictional employers. Their websites are `<name>.example.com`, a domain reserved for documentation. The job
boards are fictional boards on the four supported providers, answered from memory.

| Employer | Source | Story |
|---|---|---|
| Meridian Rx | legacy Greenhouse board | PBM. Its site embeds the board, so it is verified. It has a duplicate posting, a director role, a poor fit, a below-floor role, a role that disappears and closes, and a role that appears on day 3. |
| Bluepeak Health Plan | legacy Lever board | Health plan. It has a pay change (meaningful), a wording change (cosmetic), a hybrid role, an onsite role, a role limited to NY/NJ, and a role added on day 2. |
| Harborline Health | legacy Greenhouse, then Ashby | Moves to Ashby on day 2. This is an ATS migration: the old board is kept as history, its jobs close with it, and the new board is verified. |
| Cascade Health Data | legacy Lever board | Reporting and data roles, a manager role, a role that must work Eastern hours, and a poor fit. |
| Sterling Claims Services | legacy Workable board | A full-time overnight claims role, which NIGHT judges as Replacement work and rejects for pay ($26-30/h is below the $70,000 floor). Also a below-floor role and an hourly role. |
| Evergreen Health Analytics | legacy Greenhouse board | A real board with no open jobs. It is verified and stays active as empty, never treated as fake. |
| Northwind Benefits | suggested (curated), Ashby | A new employer. It is verified at its own site, quarantined, validated, then scanned. It has benefits roles and a poor fit. |
| Cobalt Claims Technology | suggested, Workable | A new employer with automation and payment-integrity roles. |
| Summit Payer Services | suggested, Workday | Manual only: never read automatically. |
| Lakeview Pharmacy Solutions | suggested, iCIMS | Manual only. |
| Quillon Health | suggested, Greenhouse | Its site links a board that belongs to Quillon Logistics. This is an identity mismatch: the board is rejected and nothing is ingested. |
| Pinecrest Care Navigation | suggested, Ashby | Its careers page shows no board link, so it goes to manual review. |
| Atlas Claims Group | suggested, Lever | The suggested board does not exist. It is an invalid source: nothing is admitted, and the failed attempt is recorded (`slug_absent`). The report does not show it yet (a gap found by the Functional MVP gate). |
| Ridgeway Staffing | alert email (LinkedIn link) | A staffing agency. The LinkedIn link is listed as a lead and never fetched, and the agency's client is never guessed. |
| Brightwater Benefits | pasted text | A posting with scam signals (a chat-app interview, a check deposit). It is rejected. |

The fictional job seeker is a senior business systems / claims analyst in Colorado: remote only, with a $70,000 floor
and the overnight (NIGHT) profile on. On days 3 and 4 they act, through the functions behind `careeros label`,
`careeros applied` and `careeros app-status` (`world.ACTIVITY`):
- they label 6 jobs (4 apply, 1 stretch, 1 skip);
- they apply to 4;
- by day 4 one application is in screening, one has been rejected, and two are waiting.

This gives the analytics export (`careeros export`) an application pipeline to show.

Totals after four days: **45 jobs**.
- 27 scored.
- 13 rejected, with reasons: title excluded, no target family, not remote, state not eligible, below pay floor, scam
  signals.
- 1 duplicate.
- 4 closed.

Also: 3 alert-email leads, 13 boards, 8 discovery attempts, and 13 jobs routed to NIGHT. These counts are recorded in the
tests (`RECORDED` in `tests/test_demo_mode.py`). A change to the world or to the rules that moves any of them fails a
test until the record and this document are updated together.

## 5. Scenario coverage

| Scenario | Where | What you see |
|---|---|---|
| Supported ATS providers | Greenhouse (Meridian, Evergreen), Lever (Bluepeak, Cascade), Ashby (Northwind, Harborline), Workable (Sterling, Cobalt) | Active boards on all four, scanned daily |
| Manual-only ATS | Summit (Workday), Lakeview (iCIMS) | `manual_only` boards, never read |
| Verified employer | Northwind, Cobalt, Harborline (new board) | Official-site handshake, quarantine, validation scan, then active |
| Invalid employer | Atlas | `slug_absent` discovery outcome; nothing admitted |
| Duplicates | Meridian 101 / 102 (same requisition) | `duplicate` (same_requisition) |
| Updated jobs | Bluepeak, day 2 | One meaningful change (pay) and one cosmetic change (wording) |
| Remote nationwide | Meridian 101 / 108, Cascade, Cobalt | `US_nationwide` |
| Remote, restricted states | Meridian 103 (CO/TX/AZ), Bluepeak (NY/NJ) | `US_restricted_states`; the NY/NJ role is rejected `state_not_eligible` |
| Hybrid / onsite | Bluepeak Denver / Chicago | Rejected `not_remote` |
| PBM roles | Meridian | PBM Operations Analyst, PBM Client Reporting Analyst, Pharmacy Claims Analyst |
| Claims roles | Bluepeak, Sterling, Cascade | Claims Analyst II, Senior Claims Analyst (hourly), Claims Data Analyst |
| Benefits roles | Northwind, Bluepeak | Benefits Operations / Implementation / Reporting Analyst, Benefits Configuration Analyst |
| BSA roles | Bluepeak, Harborline | Pharmacy Benefit Analyst, Claims Configuration Analyst, Healthcare Business Systems Analyst (hybrid: rejected) |
| Reporting roles | Meridian, Cascade, Northwind | PBM Client Reporting Analyst, Senior Reporting Analyst, Data Reporting Analyst |
| Operations roles | Cascade, Northwind | Process Improvement Analyst, Benefits Operations Analyst |
| Configuration roles | Meridian, Harborline, Northwind | Benefit Configuration Analyst, Claims Configuration Analyst, Eligibility Configuration Specialist |
| Implementation roles | Harborline, Northwind | Implementation Analyst / Consultant, Benefits Implementation Analyst |
| Payment integrity | Bluepeak, Cobalt, Sterling | Payment Integrity Analyst / Specialist |
| Automation | Cobalt | Automation Analyst, Claims Automation Specialist |
| Poor fit | Meridian, Cascade, Northwind, Cobalt | Software / data / QA engineer, nurse, account executive, office coordinator: rejected by the title screen |
| Director-level rejection | Meridian | Director of Pharmacy Operations: `title_excluded` |
| Identity mismatch | Quillon | Board rejected, `identity_mismatch` event and review case |
| ATS migration | Harborline | `ats_migration_detected` / `ats_migration_verified`; old board `historical`, its jobs closed `board_retired` |
| Empty board | Evergreen | Active with activity `empty` |
| Quarantine | Northwind, Cobalt, Harborline (new) | `board_quarantined`, then `validation_scan_passed` |
| Manual review | Pinecrest, Quillon, Harborline | `review_required` board and open review cases |
| Below pay floor | Meridian, Sterling | Rejected `below_pay_floor`, with the posting's pay quoted |
| Scam | Brightwater (pasted) | Rejected `scam_signals` |
| Closed job | Meridian 103 | Closed `no_longer_listed` after complete scans without it |
| NIGHT routing | 13 daytime roles with no hours stated; Sterling's overnight auditor | NIGHT section of the report: the 13 are routed to NIGHT Supplemental as conditional; the overnight role is judged as Replacement work and rejected for pay. No overnight role passes NIGHT in this world. |
| Leads never fetched | LinkedIn link in the alert | A lead with status `new`; nothing fetched |

## 6. The same production logic

The demo calls `cli.cmd_import` and `cli.cmd_run`, the functions behind `careeros import` and `careeros run`. The
tests confirm that `run` ran once per day, `import` ran once, and four runs completed.

What differs from a live run, and why:

| Difference | Why |
|---|---|
| HTTP and website reads are answered by the synthetic world, and names resolve to one documentation address. | No real network. The safety checks still run on every request: allowed hosts, https only, redirects, the public-address check on the resolved address, size and type caps. |
| No pause between requests. | The pauses protect real servers; the synthetic world needs none. |
| A fixed clock per simulated day (2026-11-02 to 2026-11-05, 13:00 UTC). | Repeatable dates, and closure and staleness rules that play out in four days. |
| Mail, the GitHub inbox, publishing, notifications and the AI reader are skipped. | They are private sources or outward actions (see section 3). |

## 7. Repeatability

`careeros demo tour` is repeatable. Two tours give:
- the same summary;
- the same jobs in the same states;
- the same last-day report.

Only record identifiers differ. Run IDs, review-case IDs, event IDs and similar are random by design. The same applies
to the database file's byte layout. The test `test_repeating_the_tour_gives_the_same_result` proves this; it masks
those identifiers before comparing.

## 8. Limits

- The world is small and hand-written: 45 jobs, chosen to exercise every rule once or twice. It is not a realistic
  volume or mix.
- Three cases are not in the world yet (see `functional_mvp_report.md`): an overnight role that NIGHT accepts, a
  suggested board contradicted by the employer's own website, and a pasted posting whose location line says only
  "Remote" being placed in the US from its text.
- Employer websites are plain HTML. The demo does not show script-rendered careers pages, beyond the one that goes to
  manual review.
- Commands that only make sense with real accounts are refused rather than simulated: `careeros gmail`,
  `careeros inbox`, and `paste --clipboard`. The import path for alert emails is shown with an `.eml` file instead.
- `careeros review` serves the demo report on 127.0.0.1 as usual. Labels, applications and dismissals you make there
  are written to the demo vault only.
- With Demo Mode on, a command runs one minute after the last simulated run. Day-dependent views (staleness, "new
  since last review") follow the demo's dates, not today's.
