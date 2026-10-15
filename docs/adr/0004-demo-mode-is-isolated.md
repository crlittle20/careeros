# ADR 0004: Demo Mode is isolated from every private source

- Status: Accepted
- Decided: 2026-10-13 (Portfolio Prompt 5)
- Recorded: 2026-10-13
- Related: PRIV-007, SEC-010; `docs/demo_mode.md`

## Context

A portfolio demonstration must run the **real** production code end to end. It runs on the job seeker's own
computer, which also holds:
- the private vault;
- mailbox credentials;
- a signed-in GitHub CLI.

A demo that read the vault, used a credential, published to GitHub or reached a real job board would expose private
data or act in the job seeker's name.

## Decision

Demo Mode (`careeros demo ...`, or `CAREEROS_DEMO_MODE=true`) runs the production commands with these limits:
- **Its own vault only.** The demo vault carries a marker file. It may not be the private vault, contain it, sit
  inside it, or sit inside the repository. In Demo Mode, the private vault cannot even be located, and `reset` deletes
  only marked folders.
- **A synthetic world.** Every job-board client and website reader gets an in-memory transport serving fictional
  employers on `example.com`. Without that world loaded, a client fails; it never falls back to the real network.
  Names outside the world do not resolve.
- **Refused outright:**
  - credentials, Gmail and IMAP;
  - every GitHub CLI call and publishing the seen-jobs index;
  - the clipboard;
  - the AI reader;
  - notifications.

Everything else is the same code: scans, verification, processing, reports, the analytics export.

## Consequences

**Gains:**
- Demos, tests and the committed analytics sample exercise the real logic with no path to private data.
- The tests prove it. A jail records any real network, DNS, socket, program, credential, mailbox or AI-key access,
  and the private vault is fingerprinted before and after.

**Costs:**
- The guards are spread across several modules. Each has a test and a deliberate-defect check.
- Integrations that need a real account are refused, not simulated. A refusal currently prints a traceback (gap G6
  in `functional_mvp_report.md`).

## Alternatives considered

- **A separate demo application.** Rejected: it would not demonstrate the production logic.
- **Mocks inside tests only.** Rejected: a person could not run or explore the demo.
- **Guard only the network.** Rejected: credentials, the vault and outward actions need guarding too.

## Evidence

- Code: `careeros/demo/sandbox.py`, the guards in `discovery/http.py`, `safe_fetch.py`, `core/credentials.py`, `paths.py`,
  `gmail.py`, `imap.py`, `github_inbox.py`, `extract/ai.py`, `ops/notify.py`, `cli.py`.
- Tests: `tests/test_demo_mode.py`, and the privacy checks of the Functional MVP gate (`tools/mvp_gate.py`).
