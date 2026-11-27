# CareerOS: Functional MVP report

- Author: Claude, the AI coding assistant that implemented CareerOS for its owner, Christopher Little. This is
  Claude's self-assessment, not an independent review, and "I" in this report means Claude (note added 2026-10-29).
- Version: 1.0
- Date: 2026-10-13
- Prompt: Portfolio Prompt 6, Functional MVP gate.
- Assessed: commit `4a27420` plus the two fixes in section 4.
- Method: Demo Mode only, using `tools/mvp_gate.py`. The evidence is in `evidence/functional_mvp.json`.
- Companion to `demo_mode.md`, `job_lifecycle.md` and `requirements_traceability.md`.
- Updated: 2026-10-13, at the Portfolio MVP gate (Prompt 12): G6 is fixed, and the gate graded 10 PASS,
  3 PARTIAL, 0 FAIL. Updated 2026-11-15: G1 to G5 are closed, and the gate grades 13 PASS, 0 PARTIAL, 0 FAIL
  (section 8). Revised 2026-11-16 after Codex's review of those changes (section 8): G1 now closes by showing the
  text's US sentence as a hint with the location question; the text never sets the country. The sections below record the
  assessment as it was made.

**Latest result (November 26):** 13 PASS, 0 PARTIAL, 0 FAIL across 95 synthetic checks. See [the dated updates](#8-later-changes). This is the build assistant’s assessment, not independent release approval. The independent review of the screening rules and the fixes that followed (Codex, nineteen passes) found no unresolved blocker at its last pass; those fixes are in this snapshot.

## 1. Original October assessment

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
- **Eligibility:** the 28 scored jobs are checked against the hard rules directly: remote only, Colorado allowed,
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

The table records the assessment of 2026-10-13, run on the private computer: its figures, and its check that the
real private vault was unchanged, come from that run. `evidence/functional_mvp.json` is regenerated each time the gate
runs, so it holds the latest run's checks. The public copy runs it with a decoy vault (`"private_vault": "decoy"`), so
its privacy check compares the decoy, never the real vault. The demo world has grown since, and the run of 2026-11-14
recorded 12 planted rejections, 28 scored jobs with 23 distinct priorities, tiers of 5 B, 15 near-miss and 8 archive,
and 14 jobs routed by NIGHT (CONDITIONAL). Since 2026-11-19 (the owner's D188, "Exact links only": only one exact
posting link makes two postings one job) the gate's duplicate checks expect the second posting of one requisition to be
kept and linked to the first for review, the roles re-posted after the ATS migration to be linked to the old ones as
possible reposts, and an application through a second channel to be refused; the demo then has 29 scored jobs and no
duplicate. The headline evidence of 2026-10-13:

| Requirement | Grade | What was shown |
|---|---|---|
| Fresh demo initialization | PASS | Reset finds nothing to remove. `init` writes the marker, the fictional profile and policy, the curated list, the inbox files and the database, with the 6 legacy boards and 0 jobs. A second `init` is refused and changes nothing. `init` and `reset` refuse a folder that is not a demo vault and leave it untouched. |
| Synthetic ingestion | PASS | Four simulated days run as four completed production runs, in a few seconds. Every posting any admitted board listed on any day became exactly one job: 44 board jobs plus 1 pasted, none missing, none unexpected. All four providers were read. The alert email produced 3 leads: 2 board links graded as jobs, and LinkedIn kept as a lead, never fetched. Sightings keep their source (scan, alert, import). Boards not admitted produced no jobs. |
| Normalization | PARTIAL | 11 postings match their text: nationwide, state-restricted (AZ/CO/TX; NJ/NY), hybrid, onsite, annual and hourly pay, the day-2 pay change, missing pay, and Eastern hours. Every job has a normalised title, remote scope, country and fingerprint. **Gap:** the pasted posting says "Location: Remote", and its text says "remote in the US", but its country is `unknown`. |
| Employer verification | PARTIAL | All 13 boards end in the expected status. Every active board has the official-site handshake, a confirmed identity and trust of at least 85. The three new boards passed quarantine and a validation scan. The impostor board (Quillon) is an identity mismatch and is rejected. The non-existent board (Atlas) is recorded as absent, and nothing is admitted. The migration keeps the old board as history and verifies the new one. The empty board stays active. Workday and iCIMS are never requested. The agency's client is never guessed. **Gaps:** the Atlas outcome is invisible to you; two Harborline review cases stay open after the migration completed; no board in the world is contradicted by its employer's own site. |
| Duplicate prevention | PASS | The second posting of one requisition is a duplicate. The same job seen in an alert email and a board scan is one job (twice over). Roles re-posted after the ATS migration are linked to the old ones. The same title at two employers stays two jobs. Applying to a duplicate, or applying twice, is refused. |
| Eligibility | PASS | All 13 planted violations are rejected for the right reason: director and other excluded titles, no target family, hybrid, onsite, a state outside the limit, below the pay floor, scam. No scored job breaks a hard rule. Every rejection has a reason and an explanation. All 3 pay rejections quote the posting. The posting with no pay is scored, with pay shown as an unknown to check. |
| Scoring | PASS | All 28 scored jobs have scores between 0 and 100. Each apply priority equals 0.65 fit + 0.25 career value + 0.10 freshness. Each score was made under the same policy and profile as its eligibility decision. Every component states its basis. The 27 jobs have 23 distinct priorities. |
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
  - Resolved 2026-11-02 (lead resolution, ADR 0003 amendment): such a match is only recorded as `awaiting_board`, and
    a lead is linked to a job only through a board in the daily scan.

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

**2026-11-15, the five gaps closed** (at the owner's request: "close the 5 gaps to get a full pass"). Each check
that recorded a gap now tests the behaviour, and the checks that were informational are now core:
- G1: a posting whose text says it is remote in the US ("remote in the US", "based in the USA") is placed in the US;
  "us" counts only after "in the", so "join us" never does. The pasted posting is now in the US.
- G2: `careeros sources list` lists suggested boards that do not exist (Atlas), under their own heading.
- G3: once the board an employer moved to is verified and in the daily scan, the old board's review cases about the
  move (`board_not_found`, `migration_detected`, `possible_migration`) are closed, whether the board was activated
  automatically or by `careeros sources activate`. Harborline's two cases now close.
- G4: the demo world has Granite Benefit Partners, whose suggested Greenhouse board is real but whose own careers
  site uses Workday. The board is rejected (`official_site_contradicts`) and shown as rejected by `sources list`.
- G5: Sterling has a part-time overnight auditor at $28-30/h. NIGHT accepts it as Supplemental work (eligible,
  overnight); the main profile rejects it as part-time work.
The demo now has 16 employers, 15 boards and 46 jobs; the recorded totals, the documents and the analytics sample
were updated with it. The gate: 92 checks, 13 PASS, 0 PARTIAL, 0 FAIL (`evidence/functional_mvp.json`).

**2026-11-16, after Codex's review** (CR-2026-11-15-GATE-FIXES, Pass 1: one High, five Medium, two Low findings). The
grades above are what the synthetic demo shows; the review found that the first versions of G1 to G3 went wider than
the demo, and that some checks could pass with the output missing. Changed:
- G1 is narrower. The older rule, which placed any text naming "United States" (or "US-based", "within the US"), was
  removed at the owner's request. After Codex's Pass 2 found wordings that still placed a Calgary opening ("Not open to
  remote United States applicants", "This team provides remote U.S. support", "For a US-based remote role, see our
  other openings"), the owner chose strict sentences: the text places a posting only by a sentence that opens with
  the role ("This role is remote in the US") or its candidates ("Candidates must reside in the United States"), or by
  the posting's own location line ("Location: Remote, United States"). Codex's Pass 3 then found conflicts the rule
  missed: one past the first 3,000 characters, a second location line ("Work Location: Calgary"), candidates or work
  said to be in Calgary, "ineligible", and an unrelated "not" beside the Calgary sentence. The owner chose a whole-text
  veto: the whole description is read first, and another country named anywhere, a location line or location field
  naming somewhere else, or a clause that puts the role, its work or its candidates somewhere other than the US leaves
  the country open, as does a negation in the placing sentence. Codex's Pass 4 found wordings that still beat it
  ("performed only in Calgary", "remote in the US or Calgary", "Applicants must be Calgary residents", a denial in a
  separate sentence). The owner chose to make the text a hint only: the stored country comes from the posting's
  location field alone, and when the text has a US sentence with no conflict found, the location question quotes it
  ("the text says the role is in the US, but the posting's location field does not") for you to check. The question
  stays open. The demo's pasted posting is the case: its country is unknown and its question quotes "This role is
  remote in the US". A job stored under an earlier rule is re-derived by the next processing pass and re-screened, and
  a rescan whose country changes does the same. Codex's Pass 5 found three routes that still skipped the question: a
  state list in the text cleared it for a home state in the list; a paste with no location line took its location
  from a phrase in its prose ("Previously advertised as Remote - US"); and right after an upgrade, before the first
  processing run, standing preparation and assistance acted on scores made under the old rule. Now a state list from
  the text never clears the question of a job its location field does not place; a paste takes its location only from
  its own "Location:" line or the one you give; and standing preparation (with the packages and assistance built on
  it) picks only a job whose location field places it in the US and whose score was made under the current rules (a
  new rule version marks every older score until the job is re-screened). Codex's Pass 6 confirmed that hold and found
  two more routes: a blank "Location:" label took the next line of prose as the location, and a paste saved by the
  older parser kept the location it read from its prose, so one re-screen made it eligible again (pasting it again
  did not correct it). Now a label's value must be on the label's own line; a paste records where its location came
  from; and a paste saved earlier whose location is neither its own "Location:" line nor one you gave keeps that
  location but is not placed by it: its location question asks you to confirm it, and pasting the posting again with
  `--location` replaces the stored location and has the job re-screened. Codex's Pass 7 found four more routes. A
  vertical tab, form feed or next-line control after a blank "Location:" was deleted by the text cleaner before the
  label was read, so the label took the prose again. A plain re-paste of a posting added with `add-url --location`
  turned its US placement into a question, because any paste sighting was taken as how the job got its location; and
  a paste whose sighting was lost (written apart from the job) read as one added another way. A paste of a different
  opening of the same employer and title, with another link, confirmed its `--location` onto a rejected Calgary
  posting. An interrupted confirmation left the old score, which `package prepare` and readiness still used, and a
  score made by the Pass 6 candidate routed a standing package before the first processing run. Now every line-ending
  control (vertical tab, form feed, next line, the separators) ends the line before anything is stripped, so a
  label's value is on its own line in the text as you pasted it. Where a manual job's location came from (your
  `--location`, the posting's own "Location:" line, a saved file's own location field, or none) is recorded with the
  location, in the same transaction; a later sighting is recorded without changing it. Only the same posting confirms
  a location: pasted again with its own link (or, when neither has a link, the same text) and `--location`. A match
  on employer and title alone changes nothing and says so, and a job board posting keeps its board's location.
  Codex's Pass 8 found two collisions that still let one opening stand in for another: two openings of one portal
  whose links differ only after "#" counted as the same posting (the link was compared without that part), and two
  employers whose names make the same generated job ID ("A&B Health", "A B Health") let the second paste overwrite the
  first job and its history. Now the same posting means the same link including its part after "#" (tracking
  parameters inside or outside it aside), or the same text when neither has a link, and never two different
  requisition numbers. A new posting whose generated ID another posting already holds gets its own ID (a short suffix
  from its link or text) and is linked to the other as a possible duplicate; the existing job is never touched. Both
  follow the owner's choices (D172). Codex's Pass 9 found where that still fell short. Two links that are not full
  addresses ("jobs.example.com/apply/calgary-claims" and ".../us-claims") both failed to normalize and counted as the
  same posting. `add-url --text-file` and the saved-file import dropped the part after "#" before storing the link
  and deriving the ID, and the ID guard let the same employer's second opening overwrite the first. Retrying a
  colliding saved import added a new row each time, without the possible-duplicate link, until it never finished, and
  the import linked leads to the generated ID rather than the job written. Now a link confirms a location only when
  both the paste's link and the job's are full http(s) links naming the same posting; otherwise the result says the
  link is not a full web address. Every manual route (a paste, `add-url --text-file`, a saved text file or page) keeps
  the opening's own link, the part after "#" included, and writes an ID only when the job holding it is this same
  posting (by link, requisition and text), whatever the employer; a different posting gets its own ID and the
  possible-duplicate link. A posting that comes again finds the ID it was given before; the search for a free ID
  stops after 64 tries, and beyond that the posting is reported as an exception and not written; and the import and
  `add-url` link leads to the job actually written. Codex's Pass 10 found three last gaps. The older duplicate check
  compared links without their part after "#", so a second portal opening kept as its own row was still made a
  duplicate of the first, its later edits were ignored, and lead linking, which follows a duplicate to its original,
  linked its lead to the first opening. With no link on either side, the same description counted as the same posting
  even from another employer or under another title, so one saved page overwrote another employer's job. And a
  refused import (no free ID) or a failed write still filed the input as imported and linked its lead to the job
  holding the ID. Now the duplicate check makes two postings with the same link a duplicate only when their part
  after "#" agrees too; otherwise they are linked for your review as possible duplicates. On the manual routes a lead
  is linked by its link only to the same opening, a lead whose link names another opening of the same portal is
  never matched to this one by title, and a duplicate stands for its original only when it is the same opening. With
  no link on either side, the same posting needs the same text, the same employer and the same title; anything else
  gets its own ID, linked as a possible duplicate. A lead is linked and a file filed as imported only after a
  committed write; a refused or failed file goes to the inbox's failed folder with the reason as an open exception, and
  `add-url` and a paste report "could not be stored" (the owner's choices, D178). Before Codex's Pass 11, a Claude
  red team (four independent reviewers, each required to reproduce what it reported on a synthetic vault) found more
  in this area and in older code; every finding was corrected under the owner's choices (D179). A location field that
  names the US and also another country or region ("Remote - Canada (US hours)", "US or Canada"), or that excludes the
  US ("outside US", "not open to US residents"), no longer places a posting in the US: the country stays unknown and
  the location question opens; US places whose names a foreign city shares ("Dublin, OH", "Paris, TX") and state names
  that contain a country's name stay in the US. A saved page's country code is read as the country ("CA" is Canada, never
  a US state). A label needs a colon or a dash with a space on each side, so "Location-based pay: ..." and
  "Company-wide ..." are prose. Only an app route after "#" ("#/opening/R-1101", "#!/job/7") names an opening; a page
  anchor ("#app") or a text link ("#:~:text=") is ignored. Two openings at one shared link (a careers page) are the
  same posting only under the same title and with no other requisition; `add-url` and saved text files now read the
  requisition the text states. A saved link that is not a full address is kept (a relative page link is read against
  the page's own address), so such postings never fall back to the no-link rule; a retry with the same incomplete
  link finds its own job. With no link, the same posting also needs the same location. The duplicate check reads every
  job at a link and prefers the same opening; a duplicate found by its text at another link lends its lead to its
  original. An alert's board link that was read but not stored keeps its lead open with the reason, and its email
  goes to the failed folder (a Gmail email is read again at the next pull); `add-url` with a board link says "Not
  added"; an alert link matches a tracked job only as that same opening; and one saved file with a malformed link no
  longer stops the import. Pasting a second opening that matches a tracked one only by employer and title is still
  answered "already tracked" (the owner's D166 choice). The rule version changed again (gate-2026-11-17b). The
  change takes the job out of its earlier decision in the same transaction. A job waiting to be re-screened gets no
  standing package, whether batch selection, a direct `package prepare` or readiness asks; by the owner's choice, a
  job you labelled Apply or Stretch keeps its route, and assistance refuses it until its eligibility is current. The
  rule version changed again, so every older score waits for a re-screen. A manual job saved before this rule has no
  record: its stored location places it when it is its own same-line "Location:" that does not run past a sentence
  end (the same exception as before, narrowed), or when those words do not appear in its text at all (an older parser
  only copied words from the text, so a location the text does not contain came from you or the file); otherwise it
  waits for your confirmation. A paste stored before this fix whose text had a line-ending control between a blank
  label and the next line was stored with the control already removed, joining the two lines. When the joined value
  runs past a sentence end it waits for your confirmation (a value like "St. Louis, MO" asks once too); a shorter
  joined value, with no sentence end, still reads as a same-line label. That is the documented limit; how many such
  pastes exist was not measured, because the live vault was not read. These are the tested routes; the rule has not
  been measured on real postings.
  Codex's Pass 11 confirmed three of those corrections and found six more gaps (H15 to H20). With links on both sides,
  a posting from another employer at a shared careers link, with the same title and requisition, stood in for the
  tracked one (its location confirmed onto it, or a saved page overwriting it). A lead, or an alert line, at a shared
  link joined the opening there even when its title or requisition named another. A requisition number matched before
  the link rule ran, so a second portal opening (another "#" route) or another title at a shared link was taken for
  the first; a board's second posting at one portal was made a duplicate. An alert email read a portal link without
  its "#" route, so two openings became one lead. An alert line that was read but not stored was counted as seen
  before when the email was read again, and the email was filed as done. And location fields such as "Remote - Ecuador
  (US hours)" and "Remote - between US and UAE" were placed in the US and reached standing preparation and assistance.
  Each text rule had been beaten in turn, so the owner chose a structural fix (D180). Standing preparation (batch
  selection, `package prepare`, readiness) picks only a posting from a job board's own scan; a posting you paste, add
  or save, or one read for an alert or `add-url`, waits for your Apply or Stretch label, whatever its location says.
  The board's location field places a posting in the US only when every part of it is plainly a US place or a
  neutral word: "US", "United States", a state's name or code, a city written "City, ST" (its name alone, after a
  comma, right before its state, never a place abroad or a word such as "outside"), "Remote", "Hybrid", a US time
  zone. A state code that a country shares ("IN", "CA", "DE", "IL", "GA" and others) counts only in a list of states
  with no city ("Remote - AZ, CO, TX") or before ", US" ("Chicago, IL, US"). Anything left over ("Ecuador", "hours",
  "between", "ineligible", "not") leaves the country unknown and the location question open. A paste never changes a
  tracked job's location: you set it by naming the job, `careeros location JOB "..."`, and the job is screened again
  (a job board's posting keeps its board's location). Everything a posting states must agree before one stands in
  for another: its link with the "#" route, title, requisition and employer, on a paste, a saved file, a lead and an
  alert; a missing value never conflicts. At a shared link a requisition no longer hides another route or title (they
  are linked for review). An alert keeps a portal link's "#" route, and an alert line not stored is tried again, under
  the same lead, the next time the email is read. The rule version changed again (gate-2026-11-18a). Generated
  property tests check the location rule over about 15,000 generated fields, the identity rule over 6,000 generated
  pairs, and the standing route over 60 generated postings across every route. The rule is stricter: a US town named
  like a place abroad ("Dublin, OH", "Lebanon, PA"), a city with a shared code and no ", US" ("Chicago, IL"), or a
  field in a format the rule does not know is a question for you; and a place abroad that no list names, written as a
  city right before a US state ("Tbilisi, TX"), still reads as a US city. How many real postings this moves was not
  measured.
  A second Claude red team, on the D180 build (`bfb893d`; four reviewers, each reproducing what it reported; not a
  Codex review), found about 16 High and 10 Medium problems; every one was corrected under the owner's choices (D181:
  all High and Medium; at a shared link the location must agree too; Lever and Ashby list every location; at a shared
  link a stated level or work arrangement must agree). The location field is read only as plain ASCII text, its spaces
  collapsed, at most 200 characters: a part in another script, look-alike letters or a flag ("Remote - US / Канада")
  had vanished and left the field US. "US" before a time word ("US Time Zone", "US Timezone") and a time zone's name
  ("US/Eastern") are time zones, never places. A state code a country shares counts only in a list of state codes with
  one no country shares, every part a code, "US" or a work word ("Remote - AZ, CO, TX"; never "US OR CA" or "CA OK"),
  or as "City, ST, US" (never "CA, US"). A state's code inside a part counts only after a city's name ("Austin TX";
  never "Remote OK"); a part of filler words alone ("In", "Any") is a leftover; a place abroad written as one word or
  hyphenated ("Hongkong", "Can-ada") is still a place abroad; and the splitter no longer takes quadratic time on runs of
  spaces. Lever and Ashby postings now show every location their board lists ("Remote - US; Remote - Canada"), as
  Workable's already did, so the rule sees them all. The duplicate check compares the employer at a shared link (another
  employer's posting there is another opening), and one requisition under another title and other text is another
  opening. Lead matching (for a paste, an alert, and the board matcher) never joins a lead stating another
  requisition, written as "req=" or in its own words ("Job ID: R-8102"); at a link that names no single posting (a
  careers page, a portal's front), the employer must agree and so must the title, with a stated level or work
  arrangement; a link that names one posting (a board's posting link, a job site's job id, an app route, a long
  number) joins it whatever the line calls it. An alert on a duplicate is recorded on its original. An alert job link
  keeps the query that names its opening ("/jobs/ViewJobDetails?job=101"); openings on one careers page in alerts are
  one lead per title; another posting on the same site, or a line in another country, is never counted as seen before
  on its title alone. On add-url and saved files, a second opening at a shared link with the same title is the same
  posting only at the same location too, so a location you set is never replaced by a copy's. Standing preparation holds
  a score made under an earlier profile, and your Skip or dismissal of any posting of the role (a variant, duplicate or
  repost) holds the others. A paste offers `careeros location` only for the same posting by its link; `careeros
  location` refuses a duplicate or a closed job; and add-url with a board link says `--location` is not used. The demo's
  two city fields are now written "Denver, CO, US" and "Chicago, IL, US". Each red-team script was re-run on the fixed
  tree; the two that still exit 1 are the owner's D166 paste choice and the documented fail-closed formats
  (a ZIP code after the state, "Austin, TX 78701"); a third's remaining difference is the target-family filter.
  Codex's twelfth pass (on `f44d270`) found three routes into standing preparation and four identity gaps; the owner's
  D182: fix the standing routes in place, and never merge at a page several openings share. A board posting's
  location now records where it was written, by the board's listing scan or by a single-link fetch (add-url, an
  alert, a lead, a saved board link); standing preparation needs a location the scan wrote or confirmed, and the
  board's next scan that lists the posting there confirms it (a location written before 2026-11-18 waits for that scan
  too). A field with a question mark, a quote or another sign, an alternative left empty by a separator at its end or
  beside another, or an open or empty bracket places nothing ("Remote - US?", "Remote - US /", "Remote (US"). A
  provider's typed country is written out in the location field (Workable, Lever, Ashby: "CA" is "Canada", never a
  state). At a link that names no single posting, nothing is made a duplicate, linked or matched by the link: each
  posting is kept, screened and linked for your review, and a lead or alert there stays open (this replaces D181's
  title and employer rule at such links). A link names one posting only in a known form: a board's posting link, a
  job site's id key, an app route after "#", or a job number after a job route in the path or in a query whose key
  names a job; a year is no job number ("/careers-2026"). Two employers named at one posting's link are two, even
  under one parent. An alert card at a shared page keeps its stated employer, place and requisition, so two openings'
  cards are two leads. An alert line read but not stored stays pending, and so does its email, when its retry fetch
  fails too. The large cities of the countries whose codes a state shares are places abroad in a city's slot
  ("Mumbai, IN, US" is unknown).
  A Claude red team on those corrections only (`8304ebf`; four reviewers, one per fix; the owner asked to run only on
  the blockers) reproduced 19 problems, all corrected under D182. A link names one posting only in a strict form: a
  job number (four digits or more, no year or date) right after a job route, a job site's exact id key with an id
  ("?jobId=10123", never "?jobid=all" or "?jobCategoryId="), an app route that names an opening ("#/opening/R-1",
  never "#/jobs" or "#/search"), or a Workday requisition; a job site's search page, a listing or filter page and a
  tracking key never do. A paste matches a tracked job by its link only when the link names one posting. An alert
  card's identity is everything its line states except its age ("Posted 2 days ago", "New"), in any order, so two
  openings' cards stay two leads and one opening stays one; an earlier card at a shared page hides only the same card.
  In the location field, "US" beside a time zone's part ("Time Zone: US", "EST (US)", "US/EST") is no place, an empty
  alternative inside a bracket or two state codes joined by a colon or dash ("IN: OK") place nothing, and a part that
  trails off ("US based in", "US...") places nothing; Workable's typed country is read from "countryCode" and from the
  job when a location has none. A listing scan that reads a posting but cannot store it withdraws the earlier scan's
  confirmation. An alert line read but not stored keeps its GitHub issue open, and its Gmail email is read again by
  its id until the posting is stored, beyond the search window.
  Codex's thirteenth pass (on `c1a93dd`) found three more inputs that reached standing preparation (a failed write of
  the scan's record leaving an earlier scan's US location in force; "Remote (US: Time Zone)"; a Workable job's own
  country dropped when a place names another) and three identity gaps. The owner turned standing preparation off
  (D183): `route_for` routes only your Apply or Stretch label, the standing batch picks nothing, and `package prepare
  --standing` prepares nothing, whatever `preparation.enabled` says; a package prepared under it before is withdrawn.
  The record of where a board posting's location was written stays. "US" beside a time zone's part, in either order,
  places nothing ("US" followed by a time zone's part is now a question too); a Workable job's own country abroad is
  written even when a place names another; a generic job-id query key needs a job number (never a year or a date),
  and a numeric id in an app route too; at a page several openings share, placement takes a posting for the earlier
  one only for the same text or one stated requisition; and the Greenhouse-id fallback skips a link with an app route
  and checks the line's title, employer and requisition.
  Codex's fourteenth pass (on `eb41423`) resolved H28 to H30, H23 and H20, and found three more High gaps: an
  assisted session opened under standing preparation before the change was still returned whole by its request key,
  its handoff usable (H34); after the tracked-job lookup refused an alert line, the importer guessed a Greenhouse
  link from its gh_jid, fetched the tracked posting and linked the lead anyway (H35); and "/jobs/archive_2026" named
  one posting through the Workday-style suffix rule on any host, so a second opening there took the first one's
  place and Apply label (H36). The owner's D184: a handoff is withdrawn (its file removed at the next processing run
  or session open, and its key refused) when its package was prepared under standing preparation or its job lost
  its route; no Greenhouse link is guessed for a link with a "#" route, and a fetched posting must agree with the
  line (by a guessed link: its title, employer and requisition; by its own link: no other requisition) before the
  lead joins it; and only a job board's posting link (Greenhouse, Lever, Ashby and Workable as the scanner reads them,
  a Workday link on myworkdayjobs.com ending in its requisition) or a job site's link to one job by its own id
  (LinkedIn, Indeed, Glassdoor) names one posting. Any employer careers page is shared, whatever its path, query or
  route, which ends the link-pattern findings (H27, H31, H36).
  Before Codex's fifteenth pass the owner had an extra red team run from a separate coordinating session (D185; not a
  Codex review). Its first three findings were corrected: a paste found by its employer and title alone was taken for
  a tracked job stating another location (now another opening); a stated link without "https://" was dropped, so the
  title decided (now compared as typed); and a package preparation interrupted after writing its folder could never be
  retried (the folder with no record is now written again). A fourth: a paste with another text was taken for a tracked
  job by employer and title (now another opening). Its wide rounds then confirmed 41 more items (D186, all fixed
  before Pass 15): handoffs that outlived a closed session or a Skip, a label stamped behind a clock set back, labels kept
  after a posting's identity changed; link forms that skipped the shared-page rule; requisitions misread from prose or
  cut short; distinct postings made one job by their text or a common parent; leads joined by title, requisition or a
  family name against their own link, place or employer, and links an older engine made; Location lines, applicant
  locations and a Workable job's country dropped before the placement rule read them; saved pages that stopped an
  import or lost their employer; duplicates and lead links an older engine made (checked again once after the upgrade);
  a board's monthly pay read as yearly; and a writer lock whose process id another program reused. The rules version moved to `gate-2026-11-19a`.
- G2 lists a board when the last probe of exactly that board found nothing and it has not been verified since. The
  date is the probe's, and the address is built from the board's own name. A link discovery would probe (a plain
  board name on a system read automatically) replaces a missing one and is checked again; a careers page, a mangled
  board name or a blocked system does not.
- G3 closes cases for a board retired by its move to the new board, of the same employer, once. The tested ways into
  the daily scan complete it (the validation scan, `sources activate`, `sources approve`, and a move to a board already
  scanned), each in one transaction, and each verification run completes a move left incomplete.
- The gate checks that every board job is placed by its location field, that the pasted posting stays unplaced with
  its hint, and that no wording in the text places a posting, in the parser and through the ingest step that stores
  the country. It checks Atlas's row under its heading, the two named Harborline records with their
  link and completion event, and the overnight job's card in the NIGHT section. The gate now makes 95 checks: 13 PASS,
  0 PARTIAL, 0 FAIL.
- Status: Codex's Pass 2 found F1, F4, F5, F6 and F8 partly resolved; its Pass 3 found F4 and F6 resolved and F1,
  F5 and F8 still partly resolved; its Pass 4 found F5 resolved and F1 and F8 still partly resolved; its Pass 5 found
  F1 and F8 still partly resolved and one new High (H1, scores under the old rule before the first re-screen); its
  Pass 6 found H1 resolved, F1 and F8 still partly resolved, and two new Highs (H2, an older paste's location read
  from its prose; H3, a blank location label); its Pass 7 found F1, H2, H3 and F8 partly resolved and three new Highs
  (H4, a confirmation written onto a different posting; H5, an interrupted confirmation; H6, scores made before the
  provenance rule); its Pass 8 found F1, H2, H3, H5 and H6 resolved, H4 and F8 partly resolved, and two new Highs
  (H7, a portal link that differs only after "#"; H8, a generated job ID shared by two employers); its Pass 9 found
  H7 resolved, H4, H8 and F8 partly resolved, and three new Highs (H9, links that are not full addresses; H10, the
  file routes dropping the "#" part; H11, retried colliding imports); its Pass 10 found H9 resolved, H4, H8, H10,
  H11 and F8 partly resolved, and three new Highs (H12, the duplicate check and lead linking ignoring the "#" part;
  H13, linkless pages merging across employers; H14, refused imports filed as imported); a Claude red team then found
  and the corrections above closed further routes in this area and in older code (D179). Codex's passes 11 to 18
  each found further gaps, corrected as described above and in the README's
  [project status](../README.md#project-status-2026-11-26); its nineteenth pass found no new Critical or High problem.

**2026-11-26, after Codex's nineteenth pass.** Codex's nineteenth pass (CR-2026-11-15-GATE-FIXES) found no
unresolved blocker and no new Critical or High problem; like every pass, its recommendation is advisory, not
release approval. On the reviewed code (`67bbb76`) the gate grades 13 PASS, 0 PARTIAL, 0 FAIL across 95 checks
(`evidence/functional_mvp.json`). This snapshot publishes that code.
