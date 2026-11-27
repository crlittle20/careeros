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

Codex's review of those changes (CR-2026-11-15-GATE-FIXES, Pass 1) found one High, five Medium and two Low problems; fixes followed on 2026-11-16 with the owner's follow-up (the older rule that placed any posting naming "United States" narrowed too). It was checked on f59f6c7: the 24 defects new or re-anchored since 1f5a760 and every defect in the eight source files it changed, 704, were planted the same way on Python 3.14.10, in two rounds (the second against the final code for the two files changed after the first): 668 caught by the tests of an earlier receipt, 19 by the related modules, 13 by the whole suite, the three that depend on the Python version on 3.12.10, and the export one by its own test in a git checkout. One new defect first survived (a board back in use with a stale migration link): another guard also stopped it, so its test was reworked to reach the guard on its own (`evidence/mutation-hand-check-f59f6c7.json`).

Codex's second pass found three of those resolved and five partly resolved (the US placement among them); the owner chose strict sentences for the US placement, and corrections followed the same day. They were checked on e14b63b: every defect new or re-anchored since f8b976b and every defect in the five source files changed, 266, planted the same way on Python 3.14.10: 236 caught by the tests of an earlier receipt, 24 by the related modules and 6 by the whole suite. Two planted defects were themselves wrong at first (one changed nothing, one did not load); corrected, one of them survived, so a test was added for it ("U.S." before a capital letter ends its sentence), and both were planted again and caught (`evidence/mutation-hand-check-e14b63b.json`).

Codex's third pass found two more resolved and three still partly resolved (the US placement among them), and a regression of e14b63b: the new sentence negation took the state-list reader's name, so some excluded states were read as the only ones allowed. The owner chose a whole-text veto for the US placement, and corrections followed the same day. They were checked on b81c92b: every defect new or re-anchored since 103dc16 and every defect in the three source files changed, 223, planted the same way on Python 3.14.10: 208 caught by the tests of an earlier receipt and 15 by the related modules. One new defect first survived ('all 50 states' read as somewhere else): its control sentence never reached the rule, so two controls that do were added (953b604) and it was planted again and caught (`evidence/mutation-hand-check-953b604.json`).

Codex's fourth pass found the link replacement resolved and the US placement still open: new wordings beat every text rule. The owner chose to make the text a hint only (the location field alone sets the country). It was checked on 1cabaa5: every defect new or re-anchored since 6e53cf2 and every defect in the three source files changed, 230, planted the same way on Python 3.14.10. Two survived, masked because two functions each checked for the placing sentence; 6c62676 keeps one check, and the five defects it touched were planted again and caught: 220 caught by the tests of an earlier receipt and 10 by the related modules (`evidence/mutation-hand-check-6c62676.json`).

Codex's fifth pass found the hint-only rule holding and the US placement still open through three routes (a state list in the text, a paste's location taken from its prose, scores made under the old rules), plus a new High: standing preparation acting on old scores before the first re-screen. The owner chose to fix all three and the High. The corrections (739bf0f) were checked on that commit: every defect new or re-anchored since aec37d1 and every defect in the five source files changed, 190, planted the same way on Python 3.14.10. All were caught at the first planting: 158 by the tests of an earlier receipt, 6 by the related modules and 26 by the whole suite (`evidence/mutation-hand-check-739bf0f.json`).

Codex's sixth pass found the hold on old scores resolved and two new routes to the US placement: a blank "Location:" label took the next prose line as the location, and a paste saved by the older parser kept a location read from its prose, which one re-screen made eligible again. The corrections (e1e2fbb: a label's value on its own line; a pasted location places a job only when it is its own Location line or the one you give) were checked on that commit: every defect new or re-anchored since 9b0502e and every defect in the three source files changed, 60, planted the same way on Python 3.14.10. All were caught at the first planting: 53 by the tests of an earlier receipt and 7 by the related modules (`evidence/mutation-hand-check-e1e2fbb.json`).

Codex's seventh pass found four more routes: a vertical tab, form feed or next-line control after a blank "Location:" was deleted before the label was read; a later sighting changed how a job got its location, and a paste whose sighting was lost read as added another way; a different opening's paste confirmed its location onto a tracked posting; and an interrupted confirmation, or a score made before the provenance rule, still routed a standing package. The corrections (d2439c7: every line-ending control ends a line; a manual job's location source recorded with it in one transaction; confirmation by the same posting only; the standing route waits for the re-screen; a new rule version) were checked on that commit: every defect new or re-anchored since 52f73ae and every defect in the eight source files changed, 450, planted the same way on Python 3.14.10. All were caught at the first planting: 429 by the tests of an earlier receipt, 20 by the related modules, and the private-export defect, whose test needs a real git checkout, by hand in a clean git worktree (`evidence/mutation-hand-check-d2439c7.json`).

Codex's eighth pass found two collisions that let one opening stand in for another: two openings of one portal whose links differ only after "#", and two employers whose names make the same generated job ID. The corrections (b936f2a: the same posting keeps the link's part after "#" and never has a different requisition; a new posting at another's ID gets its own) were checked on that commit: every defect new or re-anchored since 0de0514 and every defect in the three source files changed, 300, planted the same way on Python 3.14.10. All were caught at the first planting: 291 by the tests of an earlier receipt, 8 by the related modules, and the private-export defect by hand in a clean git worktree (`evidence/mutation-hand-check-b936f2a.json`). Before the commit, planting the new defects one at a time found two guards without a test of their own (the part after "#" when no requisition is stated, and the same posting found only by its ID); both got tests.

Codex's ninth pass found where the identity rule still fell short: two links that are not full addresses, the file routes dropping the part after "#", and retried colliding imports growing rows until one never finished. The corrections (bdfa862: a full link on both sides; every manual route keeps the opening's own link and writes an ID only when it holds this same posting; a posting coming again reuses its ID; a bounded search; leads linked to the job written) were checked on that commit: every defect new or re-anchored since df34e7b and every defect in the five source files changed, 492, planted the same way on Python 3.14.10. All were caught at the first planting: 482 by the tests of an earlier receipt, 9 by the related modules, and the private-export defect by hand in a clean git worktree (`evidence/mutation-hand-check-bdfa862.json`). The clean validation passed on Python 3.14.10 and 3.12.10.

Codex's tenth pass and a Claude red team before the eleventh found the last gaps in this area and a few in older code: the duplicate check and lead linking ignoring the part after "#", linkless pages merging across employers, refused imports filed as imported, prose read as a Location label, page anchors read as openings, two openings at one shared link merging, a location field naming another country besides the US, and saved pages' country codes. The corrections (83497d5, with 2dac7af correcting two planted defects that no longer loaded) were checked on that commit: every defect new or re-anchored since a2fea80 and every defect in the seven source files changed, 669, planted the same way on Python 3.14.10. All were caught: 632 by the tests of an earlier receipt, 35 by the related modules, one by the whole suite, and the private-export defect by hand in a clean git worktree (`evidence/mutation-hand-check-2dac7af.json`). Planting each new defect alone before the commit found two guards without a test of their own (the title rule at a shared link), which got one, and two planted defects that no longer loaded were corrected.

Codex's eleventh pass found six more routes, each a text or identity rule beaten by a new case: another employer's posting at a shared link taken for the tracked one, a lead or an alert joined to another opening at that link, a requisition hiding another opening, two portal routes in one email read as one, an alert line not stored and never read again, and a location field naming another country besides the US. The owner chose a structural fix over the next rule (D180): standing preparation only for a job board's own scan whose location field is plainly a US place, a location set by naming the job (`careeros location`), a plain-US-place rule for that field, and generated property tests for both rules (`tests/test_rules_properties.py`). A Claude red team on that build (bfb893d; four subagents, each reproducing what it found; not a Codex review) found about 16 High and 10 Medium problems, none of them a route into standing preparation from anything but a scan; all were corrected (7153435, the owner's D181). The corrections were checked on that commit: every defect new or re-anchored since 7178f1c and every defect in the changed source files, 948, planted the same way on Python 3.14.10. In the stages, 866 were caught by the tests of an earlier receipt and 73 by the related modules. Nine survived the whole suite. Six were caught when planted again: two review-link rules after their tests gained a second site (bf3cb32), three guards that depend on the Python version on 3.12.10, and the private-export defect by hand in a clean git worktree. The other three were in code no route ran any more after D180 (rules for a location confirmed by a re-paste, and an employer check made twice) or had no test of their own (a place abroad inside a city's name, "Mexico City, TX"); d90931c removed the first two and added the test, and every defect in that function and its caller was planted again on it. All 948 were caught in the end (`evidence/mutation-hand-check-05ab20a.json`).

Codex's twelfth pass found the invariant broken through three routes (a single-link fetch kept a scan's authority, "Remote - US?" read as a US place, typed country codes read as state codes) and four identity gaps at shared links. The owner chose (D182) to fix those routes in place and never to merge at a page several openings share, then a red team and the next pass, and asked to run only on the blockers. The corrections (8304ebf, with tests in 7f1c460) were attacked by a third Claude red team, four reviewers, one per fix, which reproduced 19 more problems; all were corrected in b971342. Each new or re-anchored defect was planted alone before its commit (42 for D182, 41 for the red team's corrections, all caught in the end). The hand check was scoped as the owner asked: every defect new or re-anchored since f44d270 and every defect whose anchor lies in a function the corrections changed, 210 (two more were retired, below), planted the same way on Python 3.14.10. Round 13 planted 212 on b971342: 203 were caught by the tests that caught them before and 2 by the related modules. Seven survived the whole suite: five were guards no test reached any more since D182 sends shared links away early, and got a test each; two were checks wholly held by newer ones, which d2caeea removed (their defects retired). Every defect in the functions d2caeea changed was planted again on it. All were caught in the end (`evidence/mutation-hand-check-d2caeea.json`).

Codex's thirteenth pass found three more inputs that reached standing preparation and three identity gaps. The owner turned standing preparation off (D183) and had the identity findings fixed as Codex recommended, then the next pass, scoped to the blockers, with no red team. The corrections (5a285cf) removed the standing route with its rule and holds, so 10 planted defects retired with them; 10 new and 6 re-anchored defects were each planted alone before the commit, all caught. The hand check, scoped as before to the new or re-anchored defects and those in the functions the corrections changed, planted 51 on Python 3.14.10. In the stages, 37 were caught by the tests that caught them before and 12 by the related modules; two survived the whole suite, guards the newer H30 and H31 checks mostly cover, and 0da9e3e gave each a test, after which both were caught. (`evidence/mutation-hand-check-0da9e3e.json`).

Codex's fourteenth pass found three more gaps: an assisted handoff issued under standing preparation before the change, still returned by its key; a guessed Greenhouse fetch that linked a lead the conflict check had refused; and a careers archive link read as one posting. The owner had all three fixed and chose that only a job board's posting link or a job site's id names one posting (D184). The corrections (f56ed53) removed the link-pattern rule, so 7 planted defects retired with it; 17 new and 2 re-anchored defects were each planted alone before the commit. The hand check, scoped as before, planted 53 on Python 3.14.10 and caught 52. The one it missed, an agency card's guard, looked caught only because a documentation-count test failed on that commit whatever was planted (the whole-suite baseline had been taken on trust); its test was strengthened in the next commit, and the next round planted it again (`evidence/mutation-hand-check-f56ed53.json`).

Before Codex's fifteenth pass the owner had a red team run from a separate coordinating session (D185; not a Codex review): a rule fuzzer and attacker and refuter agents in two rounds, 4 and 41 confirmed items. The owner had every one fixed (D186) and chose that a duplicate an older engine made, which the current rules would not make, returns to screening once (D187, the job workflow's one new transition). The corrections (72edf23) added 53 tests (the 52 for the findings each fail on f56ed53) and 82 new and 24 re-anchored planted defects (those for the wide red team's items each planted alone before the commit). The hand check, scoped the same way to the defects new or re-anchored since f56ed53 and those in the functions the corrections changed, with round 15's survivor, planted 236 on Python 3.14.10, its whole-suite baseline taken in the copies. By the stage that caught each in the end, 114 were caught by the tests that caught them before (from every earlier receipt), 86 by the red team's tests, 33 by the related modules, 2 by the whole suite and 1 on CPython 3.12.10. Seven survived the first planting: one depends on the Python version (the one caught on 3.12.10), and six whose tests the corrections had routed elsewhere were each given a test (f3882b4), after which all were caught; two rows a restart interrupted were planted again and caught, 236 of 236 in the end (`evidence/mutation-hand-check-f3882b4.json`).

A second red-team round on the corrected candidate (72edf23) confirmed 31 more items, most of them ways around the matching rules. Rather than patch each one, the owner chose that only one exact posting link makes two items one job (a requisition, a title, a place or a careers page only suggests), and that every Apply or Stretch label given before the upgrade waits to be given again (D188). The build (9b5021d) retired 47 planted defects with the rules it removed, re-anchored 47 and added 28; it added 22 tests (tests/test_d188.py) and changed the older tests whose expectations were the old matching. The hand check took every defect new or re-anchored since 69ab30b, every defect in a function the build changed, and every defect whose catching tests the build changed, 246 in all, planted on Python 3.14.10 with the whole-suite baseline taken in the copies. By the stage that caught each in the end, 135 were caught by the tests that caught them before (from every earlier receipt) and 111 by the related modules, D188's tests among them. 21 survived the first planting: 10 were equivalent under D188 or planted into code it had made dead, which the next commit (fb1e600) removed, and they are retired, each with why; 11 were rules no test reached once D188 made them proposals, and each was given a test, after which all were caught, one only after its test was made stronger (90707df): 246 of 246 in the end (`evidence/mutation-hand-check-90707df.json`).

The label gate (D190: an Apply or Stretch label approves the posting as its preview showed it, and nothing is
prepared for it once the posting, its values, its screening, a copy of it or a failed read differ) was built on
c48bd9e in eight parts and checked by hand on f88715b. A red team then ran against f88715b from the coordinating
session (BATCH_5: not a Codex review): 9 High and 7 Medium findings and 4 Lows, each fixed with a test that fails on
f88715b, then the coordinator's follow-ups (a legacy hold that names no posting reaches every job, Z8; each differing
copy shown with both values; every form of a saved page's pay unit; a closed retry's wording). The end-of-series hand
check (`evidence/mutation-hand-check-579bd31.json`, with `pass1-fixes\hand-check-batch5.log` outside this
repository) planted every defect in a function the series changed, at three points of it, 296 in all: 291 caught. The
5 others also survive the whole suite at f88715b, so they are older than the series: four in ingest's branch for an
unchanged read of a job no board reads, which no board read reaches since the gate's build, and one behind a paste
match that D188 no longer makes. They are kept, not retired, for a decision.

Codex's fifteenth pass on that build found five more gaps (D199): the card did not show everything an approval
covers; a saved page's unrecognised pay unit was cut; a board read of one posting could clear a hold that reaches
every job; P1 read the pending-holds file twice for one decision; and the file was rewritten in place. Each was fixed
with tests that fail on 579bd31 (tests/test_pass15.py), and the card now shows every field of the posting from one
renderer. The hand check of that round (`evidence/mutation-hand-check-2be781c.json`, with
`pass1-fixes\hand-check-pass15.log` outside this repository) planted every defect in a function the fixes changed, 93
in all: 92 caught. The one other blanks the apply link in the facts a differing copy is compared by; the card shows
the link in its record section, and a copy's differing link on a line of its own, so it no longer changes what any
surface shows. It is kept, not retired, for a decision.

Codex's sixteenth pass on that build found three more gaps (D200): a differing copy's stored pay and employer entry
were left to their summary lines, which are readings (a stored 0–0 per hour showed only "pay: not stated"); a paste
running past the 60,000-character limit shared the receipt of the same posting ending at it, so its cut mark never
reached a sighting; and a decision made without the writer lock paired a database snapshot with a later reading of the
pending-holds file, so a hold recovery moved between them was in neither. Each was fixed with tests that fail on
2959575 (tests/test_pass16.py): a copy's every differing leaf is shown whole, a paste's request includes its cut marks,
and a read transaction reads the file before its snapshot starts. The hand check of that round
(`evidence/mutation-hand-check-e19262e.json`, with `pass1-fixes\hand-check-pass16.log` outside this repository) planted
every defect in a function the fixes changed, 72 in all: 72 caught.

Codex's seventeenth pass on that build found one more (D201, H45): a paste's request bound a typed title, company or
location as the canonical form of its whole value, while the reader keeps the canonical form of its first 200, 100 or
300 characters as typed. So when one ran past its limit, spacing in its first part changed which words were kept under
the same receipt, and the changed reading never reached a sighting. The request now binds what was kept of a field cut
at its limit, a blank typed field counts as not given, a typed company that fits is read from its canonical form, and
add-url makes its job id from the employer's canonical form (tests/test_pass17.py, whose tests fail on 2280a59, among
them Codex's closure check as a property over generated variants of each field). The hand check of that round
(`evidence/mutation-hand-check-07cc6e4.json`, with `pass1-fixes\hand-check-pass17.log` outside this repository) planted
every defect in a function the fixes changed, 68 in all: 68 caught.

Codex's eighteenth pass on that build found one more (D203, D1): a copy's source record leaves out an empty list of
what its reader inferred or cut, and the comparison of a copy skips what the copy did not find, those lists included.
So a copy that stated, in the same words, a value the stored record marked inferred or cut read as unchanged when that
was the record's last mark, and its sighting never reached the label check. A copy that finds a field the record
marks, and does not mark it, now counts as a change, on top of the whole comparison, and the card names the mark it
drops (tests/test_pass18.py, whose tests fail on 9ff1731, among them a property that the new comparison's "unchanged"
implies the old one's over every pair of mark lists and found fields). The hand check of that round
(`evidence/mutation-hand-check-67bbb76.json`, with `pass1-fixes\hand-check-pass18.log` outside this repository) planted
every defect in a function the fix changed, 30 in all: 30 caught.

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

A batch's own check plants only the defects it can affect, with `tools/hand_check.py` (efficiency review of
2026-11-16; it replaces the per-batch scripts the hand checks above used):

```
.venv-dev\Scripts\python.exe tools\hand_check.py %TEMP%\hc-batch --base <the batch's base commit> --workers 2
```

`--base` chooses every defect new or re-anchored since that commit and every defect in a file changed since it;
`--files`, `--labels` and `--all` choose others. Each defect runs first against the tests that caught it in the
receipts in `evidence/`, then the test modules that caught defects in its file, and the whole suite only when
those miss (D152). A catch counts only by `tools/mutate.py`'s rule, at every stage, and an early-stage catch also
needs the whole suite to still load. The unmutated copy must pass the whole suite, and each smaller set of tests is
used only if it passes on the unmutated copy too: a set that fails without the defect is skipped, so a test that fails
outside its usual company never makes a catch. Each copy is checked against the starting tree before every defect,
and no receipt is written if the tree or the environment changed. A stopped run resumes with the same command. It writes
its receipt (OUT with `.json` added) in the shape of the `evidence/mutation-hand-check-*.json` receipts. `--all` runs every defect this way,
as a staged campaign; that is not the release evidence: the full run above stays the standard unless the owner
adopts staged campaigns.

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
