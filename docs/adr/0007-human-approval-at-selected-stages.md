# ADR 0007: A person decides at selected stages, and CareerOS does not apply or send

- Status: Accepted; amended 2026-10-14 (automatic applications are deferred, not rejected)
- Decided: in the design since the first commit (2026-10-12: "It applies to nothing"); expiring board approvals
  2026-10-13
- Recorded: 2026-10-13
- Related: HR-001 to HR-007, SEC-009

## Context

Some mistakes are expensive and hard to undo:
- applying to the wrong job, or applying twice through two channels (agency "right to represent");
- messaging a recruiter;
- admitting an impostor board;
- silently discarding a good job.

Automatic screening and scoring are useful but imperfect; agreement with the job seeker's own labels is measured for
exactly that reason. Work that is safe to automate is different: collecting from admitted boards, applying hard
rules, scoring, and closing jobs that complete scans no longer list.

## Decision

CareerOS **prepares and explains**. A person decides:

| Stage | The person's role | Safeguard |
|---|---|---|
| Applying and messaging | Does it themselves, outside CareerOS | No code exists that submits an application or sends a message to an employer or recruiter |
| Recording an application | `careeros applied`, or the review page | One shared check (`preflight`, 2026-10-16) for the review page, the shortlist, NIGHT and recording: one application per role across variants, duplicates, reposts and same-opening links; a second channel needs an explicit flag; applying again to a role you applied to before (any outcome) needs a reason; a possible duplicate (a linked posting, or the same title at the same employer with no conflicting requisition), an unfinished duplicate search or the employer hold needs a confirmation reason. Your Skip removes a job from the shortlist. Each record is marked as your statement |
| Labels | Apply, stretch or skip | Labels are measured against the system's tiers (the 50-label exit test) |
| Boards in the review band (trust 65-84) and review cases | Approve with a reason, reject, or resolve | An approval expires (90 days). It can never override an identity mismatch, an unsafe address, a contradicting official site or a manual-only system. |
| Near-miss jobs and decisive unknowns | Check before applying | Shown as "manual review" and "check before applying" |
| Dismissals | With a reason | Recorded, never silent |
| Policy and profile | Edits files in the vault; the profile must be approved | A change re-evaluates the affected jobs, with history kept |
| New collection or outward actions | Explicit authorisation (new sources, scheduled tasks, publishing) | Off by default in the engine's policy |

## Consequences

**Gains:**
- Nothing irreversible or reputational happens without a person.
- Disagreements between labels and tiers are visible and measured.

**Costs:**
- A daily review effort: the shortlist and the near-miss band. The report keeps it small and explained.

## Alternatives considered

- **Automatic applications.** Not adopted, and deferred rather than rejected (the owner's decision, 2026-10-14). The
  plan of record allows a later phase in which the job seeker may promote a channel to automatic submission after a
  clean history. That phase would need its own decision record; until then no code submits an application.
- **Auto-approving the review band.** Rejected: that is exactly where impostors sit.
- **Permanent manual overrides.** Rejected: they drift out of date unnoticed, so they expire.

## Evidence

- Code: `review/applications.py`, `review/labels.py`, `review/dismiss.py`, `board_registry.py: approve, expire_overrides`.
- Tests:
  - `test_one_application_per_role_across_variants_and_duplicates`;
  - `test_manual_only_and_hard_failures_cannot_be_approved`;
  - `test_t25_an_expired_manual_override_returns_the_board_to_verification`.

## Amendments

- **2026-10-14.** This record said automatic applications were "rejected outright". That wording was the AI's: it
  wrote this record in Portfolio Prompt 8, and no record shows the owner deciding it. The owner's decision is that
  automatic applications are deferred, as the plan of record allows. The title now describes what CareerOS does
  today. Nothing in the code changed.
- **2026-10-15.** "Send" in this record means to an employer or recruiter. A pre-publication review pointed out that
  CareerOS does write outward in one place: replies on the owner's issues, and a sanitized index, in the owner's own
  private GitHub inbox. The wording now says so. Nothing in the code changed.
- **2026-10-16.** Permission to submit is now a layer of its own, apart from eligibility and scoring
  (`review/submission.py`). Holds are read from policy at the moment of the check; the employer is resolved by
  `normalize/employer_identity.py`; and only the owner's release of one hold for one posting and one channel clears it
  (`careeros release`, table `submission_releases`). Recording an application already made is never refused: conflicts
  and holds ask for the owner's explanation and stay with the record, and recording never releases a hold. Still no code
  submits an application; a future tool must call `authorize`, which also checks eligibility, the employer's identity and
  competing submissions (`test_a_release_never_skips_eligibility_or_competing_submissions`).
- **2026-10-16, later.** The D13 employer left qualification: its jobs are judged like any other (they can reach Tier A/B)
  and are held only for submission. The gate keeps its generic employer-level eligibility hold, now unused; NIGHT's
  same-employer-family check uses the shared family lookup
  (`test_qualification_no_longer_holds_the_employer_but_submission_still_does`).
