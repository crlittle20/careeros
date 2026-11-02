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
| Recording an application | `careeros applied`, or the review page | One shared check (`preflight`, 2026-10-16) for the review page, the shortlist, NIGHT and recording: one application per role across variants, duplicates, reposts and same-opening links; a second channel needs an explicit flag; applying again to a role you applied to before (any outcome) needs a reason; a possible duplicate (a linked posting, or the same title at the same employer with no conflicting requisition), an unfinished duplicate search or the employer hold needs a confirmation reason. Your Skip removes a job from the shortlist. Each record is marked as your statement. On the review page a confirmation naming the posting comes first; a record made by mistake is voided with a reason, never deleted (2026-11-01) |
| Labels | Apply, stretch or skip | Labels are measured against the system's tiers (the 50-label exit test) |
| Boards in the review band (trust 65-84) and review cases | Approve with a reason, reject, or resolve | An approval expires (90 days). It can never override an identity mismatch, an unsafe address, a contradicting official site or a manual-only system. |
| Near-miss jobs and decisive unknowns | Check before applying | Shown as "manual review" and "check before applying" |
| Dismissals | With a reason | Recorded, never silent. On the review page a confirmation naming the posting comes first; a later reinstatement undoes one, and the latest event decides (2026-11-01) |
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
- **2026-11-01, the owner's decision "Confirmation plus undo"** (the observability and review-page batch, item 10). The
  stages a person decides do not change: how a decision is confirmed on the review page, and how a mistaken one is
  corrected, do. Records stay append-only: a correction is a later record, never an update or a deletion.
  - **What a person confirms.** Dismiss and "I applied" on the review page (a posting's card, the shortlist card, the
    NIGHT card) first show a confirmation page naming the posting: its title, employer and job id, with the channel
    for "I applied" and the chosen reason for Dismiss. The dismiss reason has no preselected value; one must be chosen.
    Only Confirm records; Cancel records nothing. While an assistance session for the role is open or uncertain,
    "I applied" shows the `careeros assist close ...` command instead of Confirm (ADR 0009), and recording checks again.
    The command line (`careeros dismiss`, `careeros applied`) names the job explicitly and asks nothing more.
  - **Reinstating a dismissal.** A dismissal is undone by a later event, a reinstatement (`careeros dismiss JOB_ID
    --undo`, or Undo in the review page's "Dismissed recently" section, through the same confirmation). It is a new
    row in the append-only dismissals table; the latest event decides. Everything that reads dismissals (the
    shortlist, package routing, the report, the label backlog, the daily summary and the analytics export) treats a
    reinstated posting as not dismissed, and a later dismissal dismisses it again. The earlier dismissal stays in the
    history (`careeros explain`).
  - **Void applications.** An application recorded by mistake is voided: `void`, a state of the application workflow,
    reachable only from the states no person at the employer has answered (`applied`, `acknowledged`, `ghosted`),
    with a required reason (`careeros app-status APP void --reason "..."`, or "Void (recorded by mistake)" on the
    application's card, through the same confirmation). A void application stays in the audit trail, the
    applications list and the export's application rows. It leaves the funnel, the follow-up nudges,
    `has_application`, the application counts and every "did I already apply?" check, so applying to the role
    afterwards needs no re-application reason. A void is final.
    *Note, 2026-11-01 (Codex CR-2026-11-01-OBS-REVIEW, F2):* "no person at the employer has answered" means never,
    in the application's whole history, not only in its state now: an application that was ever responded,
    screening, interviewing, final_interview or offer is never voided, even after it becomes `ghosted` (or
    `acknowledged` after that). The writer, the confirmation and the Void control apply that one rule
    (`void_refusal`); the states before an answer are read from the workflow's own void edges.
  - **Unchanged.** Every record is the person's own statement; a reinstatement or a void is never made automatically;
    nothing is deleted; no code submits an application.
