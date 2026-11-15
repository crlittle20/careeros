# ADR 0003: New boards are quarantined until a validation scan passes

- Status: Accepted; amended 2026-11-02 (leads resolve only through admitted boards)
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

## Amendments

- **2026-11-02, the owner's decision D68** (the lead-resolution batch). The quarantine is unchanged. What changes is how a
  lead (a job seen only in an alert or a recommendation, for example on LinkedIn or Indeed) reaches its posting:
  - **Only through admitted boards.** A lead is linked to a posting only when the posting is on a board the daily scan
    admits (active, or a legacy board in its grace) and the lead's employer and the board's employer resolve to the same
    employer through `employer_identity.resolve()`, the check submission and NIGHT already use. Discovery's own evidence
    about a candidate board (its name, a domain in its content, a supplied link) no longer grades a lead or ingests its
    posting: a posting that matches there is recorded as waiting for the board (`awaiting_board`), and the lead is
    linked once the board is admitted and scanned. The same rules apply when an alert or a recommendation arrives: its
    line is linked at once only to a matching job on an admitted board of the same employer, and otherwise stays an open
    lead (made precise after Codex review CR-2026-11-02-LEAD-RESOLUTION, finding F2).
  - **Matched again.** After every scan of a board, and when a board is admitted, the open leads for that employer are
    matched again against its postings, each lead with its own title, link, requisition and location. A lead whose job
    is scanned later is linked then.
  - **What links by itself, and what the owner confirms.** The same requisition, the same link, or the same title (same
    seniority, compatible location) links automatically. A likely match (one posting at the employer with a
    near-identical title, filler such as "remote" or I/II aside, the same seniority and a compatible location) is
    recorded as a candidate for the owner to confirm or reject (`careeros leads confirm|reject` naming its job, or the
    review page; a command or a page that names another job than the one waiting records nothing, Codex review F5);
    it is never linked by itself. A proposal is shown, and can be confirmed or rejected, only while it still qualifies:
    the employers still match and, for a proposal made after a scan, its board is still in the daily scan (checked in
    the same transaction as the confirmation; Codex review, F6). Two or more candidates stay ambiguous.
  - **Lead states.** A lead whose employer's admitted board was scanned completely without a match is `no_match` (and is
    matched again on later scans); one with no match after 45 days is `closed`, but only when every admitted board of
    its employer was read completely in that scan run (Codex review, F3: one board's scan says nothing about the
    others, whatever the order they are read in). Each keeps its reason, and so does a
    failed fetch of a lead's link. Pasting or adding the same posting links its lead, when no other open posting of the
    employer could be the lead too (Codex review, F4: the same link, or a requisition only that posting carries, picks it
    out; otherwise the ambiguity stays). An owner's correction is a later
    record with an event in the audit chain (ADR 0008), never an edit of history (`careeros leads unlink`).
  - **The domain route.** When a lead carries `domain=` and no board is known for its employer, the official-site check
    (ADR 0001) runs for that employer to find the board its careers page links to; a board waiting for an unknown
    domain is checked again as soon as a lead supplies one, instead of after 30 days. Whatever it finds enters
    quarantine and a validation scan like any new board. A lead's domain is only a hint (ADR 0001, amendment of the
    same date).
  - **Unchanged.** New boards stay quarantined until a validation scan passes; nothing is guessed into the daily scan;
    CareerOS never fetches, crawls or logs into LinkedIn, Indeed or Glassdoor.
  - **Evidence.** Code: `discovery/lead_resolution.py`, `board_discovery.py: match_lead`, `review/leads.py`. Tests:
    `tests/test_lead_resolution.py`, for example `test_a_board_the_scan_does_not_admit_never_resolves_a_lead`,
    `test_a_near_identical_title_is_a_likely_match_linked_only_when_you_confirm` and
    `test_a_lead_whose_job_is_scanned_later_is_linked_after_that_scan`.
