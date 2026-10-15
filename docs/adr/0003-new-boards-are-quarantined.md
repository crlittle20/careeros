# ADR 0003: New boards are quarantined until a validation scan passes

- Status: Accepted
- Decided: 2026-10-13 (board verification)
- Recorded: 2026-10-13
- Related: FR-032; ADR 0001, 0008

## Context

A board can pass verification and still be wrong for CareerOS:
- a token that resolves to the wrong board;
- an API answering in an unexpected shape;
- identity signals that only a full scan contradicts.

CareerOS's history is append-only (ADR 0008). Postings ingested from a bad board cannot be cleaned away later: they
would stay in the history, the metrics and the analytics as rejected or closed jobs.

## Decision

A newly verified board enters **`quarantined`**. One validation scan must then pass before the board becomes
`active` and its postings are ingested:
- the board answers with well-formed postings (an empty board is valid);
- the identity is checked again on the full postings.

If validation fails, the board goes to `review_required` and a review case opens.

A legacy board that its own employer's site verifies goes straight to `active`, because it was already being scanned.

## Consequences

**Gains:**
- Nothing enters the history from a board that has not been read cleanly once.
- Validation failures surface as review cases, not as bad jobs.

**Costs:**
- A new board's jobs appear two runs after verification, because each daily run scans boards before it does board
  upkeep. In the demo, Northwind is verified on day 1, passes validation on day 2, and its jobs are first ingested on
  day 3.

## Alternatives considered

- **Ingest immediately and clean up afterwards.** Rejected: the history is append-only by design, and cleaning would
  hide what happened.
- **Validate by hand.** Rejected: unnecessary work. The automatic scan answers the question.

## Evidence

- Code: `board_verifier.py` (the validation step and `_validation_failed`), `board_registry.py`.
- Tests:
  - `test_t13_*`, `test_t14_*`, `test_t15_*` in `tests/test_board_verification.py`;
  - `test_a_legacy_board_verified_at_its_own_site_becomes_active_without_quarantine`.
- Demo: the `board_quarantined` and `validation_scan_passed` events (`docs/demo_mode.md`).
