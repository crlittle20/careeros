# ADR 0002: Board Trust, Board Health and Career Fit are separate scores

- Status: Accepted
- Decided: 2026-10-13 (board verification)
- Recorded: 2026-10-13
- Related: FR-033; ADR 0001

## Context

Three different questions get asked about a board:
- Is it the employer's? (legitimacy)
- Is it working? (operations)
- Is the employer worth watching closely? (usefulness)

One combined status would mix them:
- a provider outage would look like an identity problem;
- a legitimate employer with no openings this month would look fake;
- an employer that hires a lot would look more "trustworthy".

Each of those leads to a wrong action, such as dropping a real employer or keeping a bad board.

## Decision

Three scores, computed from different evidence, and none feeds another:

| Score | Question | Evidence | Used for |
|---|---|---|---|
| Board Trust (0-100) | Is this the employer's board? | Verification evidence only: handshake 40, identity 20, second link 15, board function 10, domain 10, agreement 5 | Admission (≥ 85 plus every mandatory check) |
| Board Health (0-100) | Is it working? | The last 10 scans outside provider outages | Operations: the report, alerts |
| Career Fit (0-100) | Worth scanning often? | Relevant roles seen there, by recency, volume, quality, remote share and breadth | Scan cadence (daily, every 3 days, weekly, monthly) |

## Consequences

**Gains:**
- An outage lowers health, never trust. In the Functional MVP failure test, four boards failed and none lost trust.
- A real but empty board stays active (Evergreen, in the demo).
- Scan effort goes where relevant roles appear, and no board is ever disabled for lack of hiring.

**Costs:**
- Three numbers to explain. The report and `careeros sources health` show each one with its reason.

## Alternatives considered

- **One composite score.** Rejected: it hides which question failed, and it lets usefulness compensate for doubtful
  ownership.
- **Drop boards with no relevant roles.** Rejected: hiring is seasonal, so cadence slows instead.

## Evidence

- Code: `board_verifier.py` (trust), `source_health.py` (health), `employer_intel.py` (career fit).
- Tests:
  - `test_t09_no_relevant_roles_lowers_career_fit_never_trust`;
  - `test_t26_a_temporarily_unavailable_board_loses_health_not_trust`;
  - `test_t30_repeated_relevant_hiring_raises_career_fit_and_scan_priority_not_trust`.
