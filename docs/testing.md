# Testing with planted defects

CareerOS has several layers of tests; the README's [Testing](../README.md#testing) section lists them. This page
explains one of them, planted defects, because its result ("1066 of 1066 caught") means little without knowing what was
planted, what counts as caught, and what the run cannot show. The full list, one table per source file with the tests
that caught each defect, is generated from the run's receipt: [defect_coverage.md](defect_coverage.md).

## What a planted defect is

A planted defect is a deliberate, small break of one safety rule: the pay floor ignored, the scam block switched off,
a redirect followed off the employer's domain. `tools/mutate.py` holds the list. For each defect it records the file,
the exact text to replace, the replacement and a short label. The tool copies the repository, puts one defect into the
copy, runs the whole test suite there and records which tests failed. The copy is then thrown away; the code itself is
never changed.

Why the project uses them: a passing suite proves only that the tests pass. It cannot say whether the tests would
notice if a rule were broken. A planted defect asks exactly that question. When no test fails, the defect "survives",
and the rule has no real test; the fix is a new or stronger test, never a change to the defect. CareerOS's rules exist
to keep a job seeker from a bad outcome (pay below the floor, a scam posting, a site that must never be automated, a
token in a log file, a second application to the same role), so the project asks the question of each one.

What counts as caught: a defect is caught only when a real test failed or errored with the whole suite loaded. A test
module that failed to import, a suite that ran fewer tests than the unmutated one, a timeout or a broken run never
counts. Before the first defect goes in, the tool runs the unmutated copy and refuses to start unless it passes.
Every validation (`tools/validate.py`) also checks that each defect's text is still found once in the code, so a
defect cannot stop applying without anyone noticing.

## Run history

Each row is a full run: every defect, the whole suite once per defect. The evidence commit holds the run's receipt
(`evidence/mutation-receipt.json`) or records the result in the documents; `git log` shows them. Dates are the
evidence commit's; where none was committed, the date the documents gave. Runs on candidates that proved not final
were stopped part-way (one was voided by standby) and count for nothing.

| Date | Caught / planted | Evidence commit | Note |
|---|---|---|---|
| 2026-10-14 | 283 / 284 | none committed | The first full run after the scratch-copy fix (below). The survivor was a weak test: a private staffing agency probed for a board passed whatever the private list said, because the fictional agency's name contained the word "staffing". The test now uses a second agency whose name does not. |
| 2026-10-14 | 284 / 284 | e0d39b6 | The re-run after that test was strengthened. |
| 2026-10-14 | 284 / 284 | 0be7729 | The same result for e0d39b6, made in the public copy. |
| 2026-10-15 | 284 / 284 | f76332b | The result for 2b11899. |
| 2026-10-15 | 305 / 305 | 0e24124 | After Experience Match and certifications. |
| 2026-10-17 | 438 / 443 | none committed | 5 defects whose code had changed never ran. Since then every validation checks each defect's text. |
| 2026-10-18 | 493 / 512 | none committed | The first run with each result classified: 4 timed out while the laptop was suspended, 8 were hidden by demo fixtures that ran in setUpClass, 2 defect definitions did not import, 2 survivors exposed missing test cases and 1 had become equivalent (retired). The fixes are commit 99ab7a3. |
| 2026-10-19 | 511 / 511 | f984d15 | After those fixes. |
| not dated in the record | 525 / 526 | none committed | After an independent Codex review found six more defects, each fixed with a regression test and a planted defect. The survivor was one test gap, fixed. |
| 2026-10-20 | 526 / 526 | 2e133ed | After that fix. |
| 2026-10-21 | 526 / 526 | 456aed6 | On the certifi candidate. |
| 2026-10-22 | 555 / 555 | ac83e00 | The assisted-applications batch. A second Codex pass had found two more defects (fixed, with tests), and assisted applications added their own planted defects. |
| 2026-10-22 | 662 / 662 | 4787e2a | The corrected batch: Codex's review of it, and five reviews and two read-only checks of its corrections, added more defects. |
| 2026-10-23 | 714 / 714 | 0c907ec | Run on 8ef6547. Codex's second pass found one more gap (fixed, with three read-only checks of the correction); three reviews of the run tool, which now resumes a stopped or slept-through run carrying over only clean catches on the identical tree and environment, added their own defects. |
| 2026-10-25 | 723 / 723 | 23f8272 | Run on 911cef3. Codex's third pass confirmed the gap closed and found one in the run tool (a resumed run with every result carried over could certify an edit made after its checks); fixed, with a fresh check of the fix, and confirmed by Codex's fourth pass. |
| 2026-10-26 | 723 / 723 | da7001b | Run on 9139a51. |
| 2026-10-27 | 723 / 723 | 3a16a05 | Run on 98a8d62. |
| 2026-10-27 | 723 / 723 | 7b64924 | Run on d6b0d1f. |
| 2026-10-28 | 723 / 723 | f75a2b1 | Run on 569a668. |
| 2026-10-29 | 723 / 723 | 845fbdc | Run on 0d1be18. |
| 2026-11-01 | 723 / 723 | 8329914 | Run on 5e1fc97, the code published on 2026-11-01 (dismissal fields in the export, the testing pages, the snapshot guards). |
| 2026-11-02 | 749 / 749 | 076d753 | Run on d11b24d, the observability and review-page batch after Codex's two passes: the batch added 22 planted defects and the corrections of Codex's first pass four more. Stopped cleanly once and resumed with eight workers (60 catches carried over); the eight defects the machine slept through ran again. |
| 2026-11-04 | 906 / 906 | 2fdfe4d | Run on fd7b16c, the Python 3.14 and resilience batch: CPython 3.14.10 and Node v24.22.1; the reader fix, the closed stores, the alert test and the resilience items added planted defects, 749 -> 906. |
| 2026-11-04 | 1006 / 1007 | f30a638 | Run on d561171, the merged candidate: the measurement batch, the scoring loop and the Python 3.14 batch with its Codex corrections, under CPython 3.14.10 and Node v24.22.1; their planted defects, 906 -> 1007. One survived, "long OR-lists of roles cut at 60 characters": a rule change (role years read from the posting's own line) had moved the check its only test relied on, and no behaviour changed. The test was fixed (05ed645) and, by the owner's decision, checked by hand instead of a new full run: on c096174 the suite fails with that defect planted and passes without it; on d561171 it passed with it, as in the run (`evidence/mutation-spot-check.json`). |
| 2026-11-05 | 1066 / 1066 | the commit that adds this row | Run on 559212b, the combined candidate (D113): the measurement batch, the scoring loop, the Python 3.14 batch, the pre-review corrections (D111) and the full-name, residence and pay batch, under CPython 3.14.10 and Node v24.22.1; 1066 planted defects (1007 -> 1066), 8 workers, 7 h 58 min for the planted defects, started once scope was frozen and never restarted (D112). This is the receipt in the repository; [defect_coverage.md](defect_coverage.md) is generated from it. |

After that run, a pre-review's corrections changed the code again (the owner's decision D111, 2026-11-04). In place of
a new full run, 272 of the 1030 defects were planted by hand on ea1dfc1, each with the whole suite and the tool's own
classification: every defect in a file changed since f30a638, every one added or re-anchored since, and every one
whose catches in the receipt above all came from a test module changed since. All were caught, one only after its
test was fixed: the defect that reads "in this role" as a role named "this" survived, because the years reader's
correction had moved its test line out of reach; planted again on f0ef4da, the merge of that test fix, it is caught
(`evidence/mutation-hand-check.json`). The full run on 559212b, the table's last row, then planted every defect
again, with the full-name batch's.

After that run, the corrections that followed a Fable review (the owner's decisions D114 and D115, 2026-11-05) changed
the code again. In place of a new full run (D112 rule 3), 423 of the 1157 defects were planted by hand on fbc22aa,
each with the whole suite and the tool's own classification: every defect in a file changed since 559212b, every one
added or re-anchored since, and every one whose catches in the receipt above all came from a test module changed
since. All were caught (`evidence/mutation-hand-check-fbc22aa.json`). The receipt keeps the full run's own result.

After Codex's first review of the combined candidate (CR-2026-11-05-COMBINED), its corrections, the merge of live
main and the intake hotfix changed the code again. In place of a new full run, 813 of the 1406 defects were
planted by hand on ece4cb1, chosen the same way since d1a4834, on Python 3.14.10. 812 were caught. The one
left depends on the Python version: 3.14's HTML parser no longer reads "&currentJobId=" in a link as an entity, so
the escape it removes changes nothing there; planted again on 3.12.10, the runtime a rollback returns to, it was
caught (`evidence/mutation-hand-check-ece4cb1.json`). On a0a83b2 eleven role-years defects had survived: D123 had made their old tests' lines
CONDITIONAL whatever the defect did; ece4cb1 gives each a paired test.

The intake hotfix's last fail-safe (D142: a link is a review link if any reading of it says so, and nothing matches a job tracked at one) was merged in as dd8c8d8. Its 206 defects, chosen the same way since 3b0de58, were planted by hand on Python 3.14.10: 205 caught, and the Python-version-dependent one caught on 3.12.10 (`evidence/mutation-hand-check-dd8c8d8.json`).

The fail-safe fixes for Codex's second review of the combined candidate (D149-D151: an uncertain reading is to verify, never a rejection or a silent pass) were checked on 8f896fa. Its 727 defects, chosen the same way since 8a3c0c6, were planted by hand on Python 3.14.10, each first against the exact tests that caught it in the full run, then the test modules most likely to catch it, and the whole suite only when those didn't (D152; each row records its stage: 376 modules, 347 receipt tests, 3 whole suite): 726 caught, and the Python-version-dependent one caught on 3.12.10 (`evidence/mutation-hand-check-8f896fa.json`).

The fail-safe fixes for Codex's third and last review of the combined candidate (D155: the same rule for the readings that review still found, and an AI reader's quote never replaces the rules' own reading of another requirement), with those for its second review, were checked on 3b741f6. Its 762 defects, chosen the same way since 8a3c0c6, were planted by hand on Python 3.14.10, each first against the exact tests that caught it in the full run, then the test modules most likely to catch it, and the whole suite only when those didn't (D152; each row records its stage: 387 modules, 370 receipt tests, 4 whole suite): 761 caught, and the Python-version-dependent one caught on 3.12.10 (`evidence/mutation-hand-check-3b741f6.json`). The owner closed that review's findings with these fixes and this check, without a further independent review.

The agency-digest batch (54fdb33, its own hand checks outside this repository) was merged onto the deployed combined candidate (3c1fda0) and checked on 7c78b77. Its 496 defects (the 9 new or re-anchored at the merge, and every defect in the six source files both sides had changed) were planted by hand the same way, on Python 3.14.10 (D152): 490 caught at once (483 by the tests of an earlier receipt, 5 by the merge's own tests, 2 by the modules). Of the six left, two guards no test reached got one each (AgencyMergeHandCheckTests: an agency card whose tracking link carries gh_jid is never matched to that Greenhouse job; a posting whose canonical link is a review link never links the lead at its own link, a gap older than the merge); three depend on the Python version (3.14's html.parser and Unicode 16 tables do on their own what the code guards against) and were caught on 3.12.10, the runtime a rollback returns to; and the export one was caught by its own test in a git checkout, the only place that test can run (`evidence/mutation-hand-check-7c78b77.json`).

The owner's D156 then replaced the hold proposed at the merge: a review link the combined code keeps under a link the agency batch keeps local is kept as a review lead, stored as a local lead (never exported, published or matched). It was checked on 370a4fe: the 2 defects new since 60a404d and every defect in the three source files it changed, 411, were planted the same way on Python 3.14.10: 407 caught there, the three that depend on the Python version caught on 3.12.10, and the export one by its own test in a git checkout (`evidence/mutation-hand-check-370a4fe.json`).

The Functional MVP gate's five gaps were then closed (2026-11-15: a posting's country read from "remote in the US", suggested boards that do not exist listed, a completed migration's review cases closed, and two demo scenarios). It was checked on 3e0dddf: the 5 defects new since d7b02b1 and every defect in the six source files it changed, 468, were planted the same way on Python 3.14.10: 418 caught by the tests of an earlier receipt, 45 by the related modules, 4 by the whole suite, and the export one by its own test in a git checkout (`evidence/mutation-hand-check-3e0dddf.json`).

Receipts committed before 2026-10-14 (95 to 259 defects, every one reported caught) are void: the tool's scratch
copies lacked files some tests need, so every defect looked caught. The tool now refuses to run unless an unmutated
copy passes.

## Ten examples

Each row is one planted defect from the receipt: its label and file, what would go wrong for a job seeker if the
defect were real, and a test that failed when it was planted, with how many more failure or error records the suite
counted (a record is not a test: one test with subtests can fail more than once; the receipt keeps up to three names
per defect, and the generated page shows them).

| Planted defect (file) | If it were real | A test that caught it |
|---|---|---|
| pay floor ignored (`careeros/qualify/gate.py`) | A job whose posted maximum is below your pay floor would be scored, and could reach the shortlist, instead of being rejected with the quote that shows why. | `tests.test_qualify.PayTests.test_the_gate_quotes_your_tier_and_a_straddle_is_conditional` and 10 more |
| scam block disabled (`careeros/qualify/gate.py`) | A posting with scam indicators (a processing fee, a gift card, a wire transfer) would be scored like any other job. | `tests.test_demo_mode.DemoTourTest.test_jobs_cover_every_outcome` and 5 more |
| projection mismatch not fail-closed (`careeros/db/store.py`) | When a job's stored state and its transition history disagree, the store would keep writing on top of the corrupt record instead of refusing. | `tests.test_core.StoreTests.test_projection_mismatch_fails_closed` |
| private network addresses fetched (`careeros/discovery/safe_fetch.py`) | A careers link that redirects to an address inside your own network would be followed. | `tests.test_board_verification.OperationsTests.test_t20_a_redirect_into_a_private_network_is_blocked` |
| GitHub tokens reach the logs (R2) (`careeros/core/redact.py`) | A GitHub token caught in an error message would be written to the log file in clear. | `tests.test_core.RedactTests.test_github_and_google_token_shapes_are_removed` |
| verified boards join the daily scan without approval (`careeros/discovery/board_verifier.py`) | With automatic activation off, a newly validated job board would be polled every day before you approved it. | `tests.test_board_verification.LifecycleTests.test_without_auto_activation_a_validated_board_waits_for_you` and 2 more |
| LinkedIn made an automatic source (`config/site_policy.yaml`) | The daily run would read LinkedIn automatically, which the project never allows. | `tests.test_config.SitePolicyTest.test_linkedin_indeed_and_glassdoor_are_never_automated` and 3 more |
| duplicate application not detected (`careeros/review/applications.py`) | A second application to a role you already applied to would be recorded without a hold or a question. | `tests.test_applications.ApplicationLogicTests.test_a_possible_duplicate_or_the_same_title_elsewhere_holds_until_confirmed` and 4 more |
| submission: a closed posting authorized (`careeros/review/submission.py`) | An application could be authorized for a posting that has already closed. | `tests.test_submission.AuthorizationTests.test_a_release_never_skips_eligibility_or_competing_submissions` |
| Demo Mode without a synthetic world uses the real network (`careeros/demo/sandbox.py`) | A reviewer's demo could reach the internet instead of failing when no synthetic world is loaded. | `tests.test_demo_mode.DemoBoundaryTest.test_without_a_synthetic_world_clients_fail_instead_of_reaching_the_internet` |

## How to run it

From the engine folder, with the development environment installed (the command is at the top of
`requirements-dev.lock`):

```
.venv-dev\Scripts\python.exe tools\validate.py --full
```

`--full` runs every check CI runs (lint, format, types, configuration, the tests with the network refused, the
Functional MVP gate in Demo Mode, the release scan and pip-audit) and then every planted defect. The suite runs once
per defect, so the last check takes hours; `--workers N` runs that many copies at once (the default is 4). A run the
machine slept through, or that was stopped, resumes with the same command on the same tree; `tools\mutate.py --fresh`
starts from zero. The run writes `evidence/mutation-receipt.json`. Then:

```
.venv-dev\Scripts\python.exe tools\defect_coverage.py            # rewrite docs/defect_coverage.md from the receipt
.venv-dev\Scripts\python.exe tools\defect_coverage.py --check    # the tests run this: the page must match the receipt
```

## What it does not prove

A caught defect proves that some test notices that one break, made in that one place; it does not prove the rule is
right, or that a different break of the same rule would be noticed. The defects were written by the same people and
tools that wrote the code and the tests, so they cover the rules their authors thought of; a rule nobody thought of
has neither a test nor a planted defect. 628 of the 1066 are caught by exactly one test method (the last column of
[defect_coverage.md](defect_coverage.md)): weaken that test and the defect survives. For 22 more the receipt cannot
tell, because it keeps at most three failure names per defect and one test with subtests can fail more than once.
The run was made on one machine by the project's one owner, and its receipt is committed, not reproduced by anyone
else. And none of this measures
whether the ranking helps: as the README's [Limitations](../README.md#limitations) say, the only measurement is
in-sample, on 50 postings labelled by one person, and no tier rule has been checked on new labels. The planted defects
show that the rules as written are tested; they do not show that the rules are the right ones.
