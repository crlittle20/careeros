# ADR 0001: The employer's own website decides which job board is theirs

- Status: Accepted
- Decided: 2026-10-13 (board verification)
- Recorded: 2026-10-13
- Related: FR-031, FR-032, SEC-009; ADR 0002, 0003

## Context

Boards reach CareerOS from many places:
- alert emails;
- research (for example a ChatGPT-produced employer list);
- links in recommendations;
- guessed board names.

None of them proves who owns a board, and two failures showed the risk:
- Same-name employers collide. A board on one provider that carried a followed healthcare employer's short name turned
  out to belong to a mortgage company.
- With automatic activation on, a researched link alone was enough to "verify" identity.

Admitting the wrong board fills the job list with another company's postings. That wastes review time and can lead
to applying in the wrong place.

The one party that reliably knows which board is theirs is the employer, and the employer controls its own domain.

## Decision

A board is admitted to the daily scan only when the employer's official website vouches for it:
1. **The official domain names the employer.** Its homepage is read safely (`safe_fetch`).
2. **Handshake.** The employer's homepage or careers page links to, embeds, or redirects to the board.
3. **Identity.** The board agrees: its own name, the official domain in its postings, or its postings naming the
   employer. A different company's name with nothing else is a mismatch.
4. **Board Trust ≥ 85**, from the evidence alone, with every mandatory check passed.
5. Then quarantine and a validation scan (ADR 0003).

Everything else is a *candidate*:
- search results, aggregator links and alert links;
- research and guessed tokens.

Boards CareerOS already scanned before this decision ("legacy" boards) keep scanning during a grace period, until
they are verified or fail. Nothing becomes verified just because it already existed.

## Consequences

**Gains:**
- Impostor and same-name boards are refused. In the demo, Quillon's linked board names Quillon Logistics: it is
  rejected, and nothing is ingested.
- Every admission is explained by recorded evidence (`careeros sources explain MAPPING`).

**Costs:**
- CareerOS must read employer websites. This reversed an earlier "never read websites" boundary, so it is bounded:
  - `board_verification.fetch_official_sites` must be on (the job seeker switched it on after one supervised check);
  - pages are read only with `safe_fetch`;
  - page budgets apply;
  - robots.txt is honoured.
- A careers page that loads jobs with JavaScript shows no link, so the board goes to review (one followed employer's
  case).
  Whether to read same-domain script files is on the 2026-10-20 review list.
- The official domain must be known first. Boards without one wait in `verification_required`.

## Alternatives considered

- **Trust a matching name or board token.** Rejected: names collide, and tokens are guessable.
- **Trust aggregators and search results.** Rejected: they may *discover* a board, never *verify* it (FR-031).
- **Approve every board by hand.** Rejected as the default: too slow. It remains for the review band (trust 65-84),
  with a reason and an expiry (ADR 0007).

## Evidence

- Code: `careeros/discovery/board_verifier.py`, `board_registry.py: scan_gate`, `safe_fetch.py`.
- Tests: `tests/test_board_verification.py` (`test_t01` to `test_t30`; for example
  `test_t10_a_high_score_without_the_official_handshake_is_never_admitted`).
- Demo: `docs/demo_mode.md` (Quillon, Pinecrest, Harborline).
