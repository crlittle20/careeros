# Testing with planted defects

CareerOS has several layers of tests; the README's [Testing](../README.md#testing) section lists them. This page
explains one of them, planted defects, because its result ("723 of 723 caught") means little without knowing what was
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
| 2026-11-01 | 723 / 723 | this public snapshot | Run inside this public snapshot of the code published on 2026-11-01 (dismissal fields in the export, the testing pages, the snapshot guards). This is the receipt in the repository; [defect_coverage.md](defect_coverage.md) is generated from it. |

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
| pay floor ignored (`careeros/qualify/gate.py`) | A job whose posted maximum is below your pay floor would be scored, and could reach the shortlist, instead of being rejected with the quote that shows why. | `tests.test_demo_mode.DemoTourTest.test_jobs_cover_every_outcome` and 8 more |
| scam block disabled (`careeros/qualify/gate.py`) | A posting with scam indicators (a processing fee, a gift card, a wire transfer) would be scored like any other job. | `tests.test_demo_mode.DemoTourTest.test_jobs_cover_every_outcome` and 5 more |
| projection mismatch not fail-closed (`careeros/db/store.py`) | When a job's stored state and its transition history disagree, the store would keep writing on top of the corrupt record instead of refusing. | `tests.test_core.StoreTests.test_projection_mismatch_fails_closed` |
| private network addresses fetched (`careeros/discovery/safe_fetch.py`) | A careers link that redirects to an address inside your own network would be followed. | `tests.test_board_verification.OperationsTests.test_t20_a_redirect_into_a_private_network_is_blocked` |
| GitHub tokens reach the logs (R2) (`careeros/core/redact.py`) | A GitHub token caught in an error message would be written to the log file in clear. | `tests.test_core.RedactTests.test_github_and_google_token_shapes_are_removed` |
| verified boards join the daily scan without approval (`careeros/discovery/board_verifier.py`) | With automatic activation off, a newly validated job board would be polled every day before you approved it. | `tests.test_board_verification.LifecycleTests.test_without_auto_activation_a_validated_board_waits_for_you` |
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
has neither a test nor a planted defect. 446 of the 723 are caught by exactly one test method (the last column of
[defect_coverage.md](defect_coverage.md)): weaken that test and the defect survives. For 3 more the receipt cannot
tell, because it keeps at most three failure names per defect and one test with subtests can fail more than once.
The run was made on one machine by the project's one owner, and its receipt is committed, not reproduced by anyone
else. And none of this measures
whether the ranking helps: as the README's [Limitations](../README.md#limitations) say, the only measurement is
in-sample, on 50 postings labelled by one person, and no tier rule has been checked on new labels. The planted defects
show that the rules as written are tested; they do not show that the rules are the right ones.
