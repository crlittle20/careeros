# Architecture decision records

Each record states one decision: the problem it answers, what was decided, what it costs, and what was rejected.
Records are short and are never rewritten. A changed decision gets a new record that supersedes the old one.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-official-employer-sites-are-authoritative.md) | The employer's own website decides which job board is theirs | Accepted |
| [0002](0002-board-trust-health-and-career-fit-are-separate.md) | Board Trust, Board Health and Career Fit are separate scores | Accepted |
| [0003](0003-new-boards-are-quarantined.md) | New boards are quarantined until a validation scan passes | Accepted |
| [0004](0004-demo-mode-is-isolated.md) | Demo Mode is isolated from every private source | Accepted |
| [0005](0005-first-seen-at-belongs-to-careeros.md) | `first_seen_at` is CareerOS's own date | Accepted |
| [0006](0006-unsupported-ats-systems-stay-manual.md) | Unsupported ATS systems stay manual | Accepted |
| [0007](0007-human-approval-at-selected-stages.md) | A person decides at selected stages, and CareerOS does not apply or send | Accepted (amended 2026-10-14) |
| [0008](0008-local-store-with-enforced-duties-and-audit-chain.md) | One local store, with enforced separation of duties and an audit chain | Accepted |

All eight were written on 2026-10-13. Most describe decisions taken earlier, so each record gives the date the
decision itself was made. A new record copies the structure:
- the header (status, decided, recorded, related);
- Context, Decision, Consequences, Alternatives considered;
- Evidence: the code and tests that hold the decision.
