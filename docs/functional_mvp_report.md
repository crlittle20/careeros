# CareerOS: Functional MVP report

- Author: Claude, the AI coding assistant that implemented CareerOS for its owner, Christopher Little. This is
  Claude's self-assessment, not an independent review, and "I" in this report means Claude (note added 2026-10-29).
- Version: 1.0
- Date: 2026-10-13
- Prompt: Portfolio Prompt 6, Functional MVP gate.
- Assessed: commit `4a27420` plus the two fixes in section 4.
- Method: Demo Mode only, using `tools/mvp_gate.py`. The evidence is in `evidence/functional_mvp.json`.
- Companion to `demo_mode.md`, `job_lifecycle.md` and `requirements_traceability.md`.
- Updated: 2026-10-13, at the Portfolio MVP gate (Prompt 12): G6 is fixed, and the gate now grades 10 PASS,
  3 PARTIAL, 0 FAIL (section 8). The sections below record the assessment as it was made.

## 1. Verdict

**The Functional MVP gate passes: no requirement fails.** Of the 13 requirements, 9 are PASS and 4 are PARTIAL. A
PARTIAL means the stage works but a supporting check does not hold.

| # | Pipeline stage | Requirement | Grade |
|---|---|---|---|
| 1 | (setup) | Fresh demo initialization | **PASS** |
| 2 | SOURCE, INGEST | Synthetic ingestion | **PASS** |
| 3 | NORMALIZE | Normalization | PARTIAL |
| 4 | VERIFY | Employer verification | PARTIAL |
| 5 | DEDUPLICATE | Duplicate prevention | **PASS** |
| 6 | ELIGIBILITY | Eligibility | **PASS** |
| 7 | SCORE | Scoring | **PASS** |
| 8 | ROUTE | Routing | PARTIAL |
| 9 | EVIDENCE | Evidence generation | **PASS** |
| 10 | OUTPUT | Output | **PASS** |
| 11 | (operations) | Failure handling | PARTIAL |
| 12 | (operations) | Repeat processing | **PASS** |
| 13 | (operations) | Privacy isolation | **PASS** |

The assessment found two core defects, and both are fixed (section 4):
- the report counted boards that verification had rejected as "verified";
- processing kept re-evaluating some jobs on every run.

Section 5 lists the four PARTIAL gaps. The prompt allows fixing only failing requirements, so none was fixed here.
Whether to close them before Prompt 7 is the Product Owner's decision.

## 2. How the assessment works

`tools/mvp_gate.py` drives the real command line, in this process, on a fresh throwaway demo vault:
1. `careeros demo reset`, then `init`, then `run --all`;
2. ordinary commands with Demo Mode on: `explain`, `sources explain`, `verify`, `status`, `metrics`, `process`,
   `import`, `run`, `applied`, `gmail`, `inbox` and `paste`.

The assessment then reads the demo database and files and runs **92 checks**. Each check is either:
- **core**: a failure grades the requirement FAIL;
- **supporting**: a failure grades it PARTIAL.

The full run takes about 30 seconds:

```
.venv\Scripts\python.exe tools\mvp_gate.py
```

**Independent expectations.** The checks do not read the system's own decisions back as the answer:
- **Normalization:** compared with the posting text. For example, "Remote - NY, NJ" must be restricted to {NJ, NY},
  and "$36 - $40 per hour" must be 36-40 per hour.
- **Ingestion:** compared with every posting the synthetic boards list on any of the four days.
- **Tiers and priorities:** recomputed from the policy's thresholds (92 / 85 / 78, with a 70% skill minimum) and
  weights (0.65 / 0.25 / 0.10).
- **Eligibility:** the 27 scored jobs are checked against the hard rules directly: remote only, Colorado allowed,
  the $70,000 and $34/h floors, title exclusions, and a target family.

**Isolation.** Everything runs under a jail that records any attempt to:
- send real HTTP;
- resolve a name or open a socket;
- start a program;
- read or write a credential;
- open IMAP;
- read the AI key;
- open a browser.

The **real private vault** is fingerprinted before and after the run: every file, by size and timestamp, plus a hash
for each file up to 20 MB. It is only read, never written. On a machine without a private vault, a decoy is used
instead.

**Faults.** Failures are injected into the synthetic world only:
- HTTP 503;
- invalid JSON;
- a timeout;
- a one-hour `Retry-After`;
- an unreadable email and an empty pasted file;
- a held writer lock.

**Independence, stated plainly.** The same AI (Claude) designed, built and assessed CareerOS. This is not an
independent review in the strict sense. Four things offset that:
- the expectations above;
- the jail and the vault fingerprint;
- a proof that the gate can fail: run against the code before each fix, it grades the affected requirement FAIL;
- this report records every place where my expectation turned out to be wrong.

An independent reviewer (for example ChatGPT, reading this report and the evidence file) is still recommended.

## 3. Results by requirement

Every check, with its evidence, is in `evidence/functional_mvp.json`. The headline evidence:

| Requirement | Grade | What was shown |
|---|---|---|
| Fresh demo initialization | PASS | Reset finds nothing to remove. `init` writes the marker, the fictional profile and policy, the curated list, the inbox files and the database, with the 6 legacy boards and 0 jobs. A second `init` is refused and changes nothing. `init` and `reset` refuse a folder that is not a demo vault and leave it untouched. |
| Synthetic ingestion | PASS | Four simulated days run as four completed production runs, in a few seconds. Every posting any admitted board listed on any day became exactly one job: 44 board jobs plus 1 pasted, none missing, none unexpected. All four providers were read. The alert email produced 3 leads: 2 board links graded as jobs, and LinkedIn kept as a lead, never fetched. Sightings keep their source (scan, alert, import). Boards not admitted produced no jobs. |
| Normalization | PARTIAL | 11 postings match their text: nationwide, state-restricted (AZ/CO/TX; NJ/NY), hybrid, onsite, annual and hourly pay, the day-2 pay change, missing pay, and Eastern hours. Every job has a normalised title, remote scope, country and fingerprint. **Gap:** the pasted posting says "Location: Remote", and its text says "remote in the US", but its country is `unknown`. |
| Employer verification | PARTIAL | All 13 boards end in the expected status. Every active board has the official-site handshake, a confirmed identity and trust of at least 85. The three new boards passed quarantine and a validation scan. The impostor board (Quillon) is an identity mismatch and is rejected. The non-existent board (Atlas) is recorded as absent, and nothing is admitted. The migration keeps the old board as history and verifies the new one. The empty board stays active. Workday and iCIMS are never requested. The agency's client is never guessed. **Gaps:** the Atlas outcome is invisible to you; two Harborline review cases stay open after the migration completed; no board in the world is contradicted by its employer's own site. |
| Duplicate prevention | PASS | The second posting of one requisition is a duplicate. The same job seen in an alert email and a board scan is one job (twice over). Roles re-posted after the ATS migration are linked to the old ones. The same title at two employers stays two jobs. Applying to a duplicate, or applying twice, is refused. |
| Eligibility | PASS | All 13 planted violations are rejected for the right reason: director and other excluded titles, no target family, hybrid, onsite, a state outside the limit, below the pay floor, scam. No scored job breaks a hard rule. Every rejection has a reason and an explanation. All 3 pay rejections quote the posting. The posting with no pay is scored, with pay shown as an unknown to check. |
| Scoring | PASS | All 27 scored jobs have scores between 0 and 100. Each apply priority equals 0.65 fit + 0.25 career value + 0.10 freshness. Each score was made under the same policy and profile as its eligibility decision. Every component states its basis. The 27 jobs have 23 distinct priorities. |
| Routing | PARTIAL | Every tier matches the recomputed threshold: 5 B, 13 near-miss, 9 archive. Every A, B and near-miss job is on the report. NIGHT routes 13 jobs to its own section, as conditional because their hours are not stated. NIGHT judges the full-time overnight posting as Replacement work against its own $70,000 floor, separately from the main (PRIMARY) decision. **Gap:** no overnight posting in the demo world is accepted by NIGHT. |
| Evidence generation | PASS | `careeros explain JOB --json` answers all 14 questions for all 45 jobs. `careeros sources explain` explains all 13 boards. `careeros verify` finds the hash chain and every history consistent. Every eligibility decision records the policy, profile and extraction fingerprints. |
| Output | PASS | There is a report for each day, with every section, and a verified backup for each day (integrity check and SHA-256 manifest). The report's verified-board figures (8) match the board records, after fix 1. `careeros metrics` counts 8 verified and 1 awaiting review, matching the records. `careeros status` matches the database. The seen-jobs index is written locally. |
| Failure handling | PARTIAL | A run with four failing boards completes in 4.4 s. Each failure is recorded against its board with its own code, and the healthy boards are scanned completely. No job is closed or changed. Health drops; trust and verification do not. Each failure is logged as an exception. The one-hour `Retry-After` is reported, not slept through. A broken email and an empty paste are reported and skipped. A second writer is refused. **Gap:** a refusal in Demo Mode (`gmail`, `inbox`, `paste --clipboard`, `demo init` on a foreign folder) prints a Python traceback instead of one line. |
| Repeat processing | PASS | Processing again, re-importing and a whole extra run change nothing. A second fresh demo gives the same jobs, states, tiers and priorities. After a rules and vocabulary change, re-evaluation settles, and a second pass changes nothing (after fix 2). |
| Privacy isolation | PASS | Gmail, the GitHub inbox and the clipboard are refused. The jail recorded nothing: there were 182 requests, all answered by the synthetic world, all to the four job-board API hosts or fictional `*.example.com` sites. The real private vault is unchanged (every file). The demo vault sits outside it and outside the code, and carries the marker. The only email addresses anywhere in the demo are `@example.com`. In Demo Mode the private vault cannot even be located. |

## 4. Run history and fixes

**Run 1 (85 checks): 9 PASS, 2 PARTIAL, 2 FAIL.** Both FAILs were my own wrong expectations. Both came from
`demo_mode.md`, which was wrong on these points and is now corrected:
- *"The non-existent board gets a review case."* The only `board_not_found` case belongs to Harborline's retired
  board. The Atlas suggestion is recorded only as a discovery attempt (`slug_absent`). The check now tests what the
  system guarantees: the board is recorded as absent and nothing is admitted. That nobody can see this became a
  supporting gap.
- *"NIGHT accepts the overnight posting."* By the policy it cannot. A full-time overnight job is NIGHT Replacement
  work, judged on the $70,000 annual floor, and $30/h × 2,080 h = $62,400. The check now verifies that judgement,
  and the missing accepted overnight role became a supporting gap.

**Fix 1: the report and `careeros metrics` called rejected boards "verified" (OUTPUT, core).** I read the report as a
user would, and its Sources section said "11 verified boards" next to "8 verified".
- **Cause:** the 30-day figure counted the older discovery identity flag. Board verification later rejects some of
  those boards: here the impostor board (Quillon), the board awaiting review (Pinecrest), and the retired board.
  "Awaiting your verification" used the same old flag and said 0 while one board awaited review.
- **Fix:** `review/discovery_metrics.py` now counts boards that verification admitted (`active`) and boards awaiting
  your review (`review_required`).
- **Checks added:** two core output checks in the gate, a test
  (`test_demo_mode::test_reported_verified_boards_are_the_boards_verification_admitted`) and a planted-bug check.
- **Proof:** run on a copy with the old code, the gate grades output **FAIL**.

**Run 2 (91 checks): 9 PASS, 4 PARTIAL, 0 FAIL.**

**Fix 2: the same jobs were re-evaluated on every run (repeat processing, core, NFR-003).** This was found outside the
demo, while rehearsing Prompt 4 on a private copy of the live database:
- The first pass re-evaluated every job (a few thousand) in under two minutes with no errors. Every pay rejection quoted the posting, and every
  job explains itself.
- But every later pass moved the same 10 jobs again.
- **Cause:** a job read once and later rejected by the title screen kept its old reading fingerprint. Each time the
  reading vocabulary changed, the job was sent back as "vocabulary changed", and the title screen rejected it again.
  The decisions never changed, but history grew by about 20 transitions per run.
- **Fix:** `pipeline._reevaluate` no longer re-sends a job whose latest decision came from the title screen, because
  that decision never used the reading.
- **Checks added:** a test (`test_job_lifecycle::test_a_job_the_title_screen_now_rejects_is_not_sent_back_on_every_run`),
  a planted-bug check, and a Demo Mode check in the gate (change the title exclusions and the vocabulary, process,
  then process again).
- **Proof:** on a copy with the old code, the gate grades repeat processing **FAIL**. On the live-data copy, later
  passes now move nothing.
- **Timing:** the defect was in the live engine and would have started with the next daily run, the first run on the
  new rules.

**Run 3, final (92 checks): 9 PASS, 4 PARTIAL, 0 FAIL, in about 30 s.** The suite has 329 tests, all passing. The two
new planted-bug checks are caught; the full mutation run is reported with the commit.

## 5. Open gaps (PARTIAL)

None is a failure. Each is small.

| # | Requirement | Gap | Effect | Suggested fix |
|---|---|---|---|---|
| G1 | Normalization | A posting whose location line says only "Remote" is not placed in the US from its text ("remote in the US"). | Such jobs show the country as unknown (a flag to check) instead of US remote. | Read the country and remote scope from the posting text when the location line is bare. This is a rule change, so jobs are re-evaluated once. |
| G2 | Employer verification | A suggested board that does not exist is recorded only in the database. | You never learn that a researched suggestion was wrong. | List failed suggestions in the report's Sources section. |
| G3 | Employer verification | After a completed ATS migration, its two review cases stay open, still saying "the new board is being verified". | Review noise. | Close both cases when the migration is verified. |
| G4 | Employer verification | The demo has no board contradicted by the employer's own website. Unit tests cover it, but the demo does not show it. | AC-104 is not fully demonstrated. | Add one fictional employer to the demo world. |
| G5 | Routing | The demo has no overnight posting that NIGHT accepts. | D7 and AC-104 are only partly demonstrated. | Add a part-time overnight posting above the NIGHT Supplemental floor to the demo world. |
| G6 | Failure handling | Refusals in Demo Mode print a Python traceback. | It looks like a crash, though nothing failed. | **Fixed 2026-10-13:** `cli.main` prints one line (`Refused: ...`) and exits with code 2. |

G4 and G5 change the demo's recorded totals, so the `RECORDED` summary and `demo_mode.md` would be updated with them.

## 6. Risk outside the demo (not graded)

- **Lead matching uses the older identity flag.** `board_discovery._record_match` grades a lead's posting from a board
  that the older discovery identity check marked verified. That check is not the official-site verification. For
  example, a same-name company's board could still have one matching lead posting graded.
  - The demo cannot reach this path.
  - I recommend a review of it before you rely on lead enrichment.

## 7. Limits of this assessment

- Demo Mode only, as the prompt requires. The synthetic world has 45 jobs; it is not a realistic volume or mix.
  Fix 2 was found on live data precisely because the demo had no job in that situation.
- The private-vault check compares files over 20 MB by size and timestamp, not by content.
- The checks are as good as their expectations. Two of mine were wrong in run 1, as section 4 records.
- This is not an independent review (section 2).

## 8. Later changes

**2026-10-13, Portfolio MVP gate (Prompt 12).** G6 was fixed: a Demo Mode refusal now ends with one line,
`Refused: Gmail is not available in Demo Mode`, and exit code 2, instead of a traceback. The fix is checked by
`test_demo_mode::test_commands_with_demo_mode_on_use_the_demo_vault` and a planted defect. The gate was run again on
the same 92 checks: 10 PASS, 3 PARTIAL, 0 FAIL. Failure handling is now PASS, and `evidence/functional_mvp.json` was
regenerated. G1 to G5 remain open; `portfolio_mvp_report.md` explains why each was left.
