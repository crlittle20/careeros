# Single CareerOS Discovery Automation — staged policy draft (NOT ACTIVE)

**Staging only.** This is an implementation contract for release A, not a replacement automation prompt ready to paste unchanged. Before activation, diff the FULL saved PRIMARY AM / PRIMARY PM / NIGHT automation rules, the latest private inbox README, and the user-authorized amendments. Do not delete restrictions just because they are not repeated in this concise document.

## Intent and source ownership

One ChatGPT discovery **task** obtains permitted fresh job facts once, deduplicates once across *both* profiles, and publishes distinct batches through the existing private `crlittle20/careeros-inbox` transport. CareerOS remains the only qualification/scoring/ranking authority. The task MUST NOT apply for jobs, compute candidate scores or send application materials. Release A retains **separate existing PRIMARY and NIGHT issues**. Neutral `INTAKE` issues are for release B only, after consumer integration tests.

**Only publisher changes:** The three existing tasks remain active until this new task is validated and the owner approves cutover; the new task must not be enabled concurrently with unrestricted delivery, which would cause duplicate job handoffs.

## One extraction, two routing outputs

1. Fetch the newest `README.md`, `intake_status.json`, full or sharded `seen_jobs` index and all relevant repo issue history before processing. Respect current GitHub read pagination. Cache/aggregate issue history within the run instead of re-fetching it for each candidate. Read issue comments where required to check the full delivery/ingestion history; a closed issue is not proof the job was ingested. `SUPERVISOR` is a shared review category, not a third issue transport.
2. Search only the Gmail job-alert providers, subjects and allowed fields already authorized. Never read private recruiter conversations, individual application acknowledgments, interview invitations or other correspondence. Process all discovered permitted messages, all eligible email bodies and every identifiable posting card. **25-job issue caps are not raw email-card extraction limits.** Count and report incomplete units accurately.
3. Use one shared collection of job identities, titles, employers, requisition IDs, schedule statements, pay evidence, location and URLs. Do not invent a requisition ID, shift, salary, active posting status or employer trust.
4. Route each job into PRIMARY, NIGHT or both based on **objective factual rules**. PRIMARY covers roles that satisfy the user's privately configured country, state, remote-work and job-family rules; never disclose private state or role preferences in public code or docs. NIGHT covers jobs meeting the separately authorized location and schedule profile, including uncertain schedules for conditional review under existing private policy. Classify NIGHT-SUPPLEMENTAL versus NIGHT-REPLACEMENT only from affirmative schedule + employment evidence; otherwise identify uncertainty, not certainty. Candidate fit/tier and final decision belong solely to CareerOS.
5. Preserve **all** privately configured employer/application restrictions, including named excluded employers, existing employer-family holds, recruiter/RTR conflicts, excluded job categories, and privacy rules. Never publish these private values or their reasons. Unverified home-state eligibility, unknown work hours and unposted pay are not evidence to invent a rejection.
6. Preserve salary restrictions precisely, using the original PRIVATE profile-specific hourly and annual thresholds without copying those private values into this public document. Third-party salary estimates cannot hard-reject. Salary not stated is not a rejection. Keep the other original NIGHT/PRIMARY work-type, scope and compensation rules untouched.
7. Release A keeps the task-side **verified-active** prepublication requirement. Confirm by trusted original employer or authorized ATS evidence, not a listing card, search snippet, stale page or aggregator. Unsupported sources remain unresolved unless separately verified. Moving this requirement into CareerOS needs a second explicitly authorized, tested protocol revision.
8. Before publishing, deduplicate across same employer + requisition ID, canonical posting identity, and company/title only when requisitions do not conflict. Do not collapse distinct requisitions with the same title. Use the complete local run results and authoritative published seen index. Jobs eligible for both tracks may be represented in both profile issues only as allowed by the existing policy and within profile-specific quotas; document the shared identity to avoid repeating upstream work.

## Transport: preserve the existing GitHub issue contract

- Exactly one **fresh** issue per *distinct, nonempty* profile batch; multiple same-day distinct issues allowed; do not append to or rewrite old issues; do not split a batch to dodge limits.
- PRIMARY title: `PRIMARY Job recommendations YYYY-MM-DD`.
- NIGHT title: `NIGHT Job recommendations YYYY-MM-DD`.
- Body starts with the approved immutable `<!-- careeros-batch-id: ... -->` comment and has only the established `Title | Company | Location | Link | Note` rows, with every job row among the first 200 lines, plus exactly one `Gmail status: ok|partial|unavailable` line. Each row uses the established allowed source/mode/schedule/pay/requisition note fields. No private score, identity, compensation floor explanation, raw email, message ID, sender address, personal profile or task diagnostics in the repo.
- Enforce the existing PRIMARY maximum of **25** at the previously authorized delivery boundary and the actual NIGHT limit from its policy. Do not assume NIGHT has the same quota or that multiple issues permit unlimited jobs.
- Freeze each batch ID, list of rows, target profile and issue title before the write. If creation returns uncertainty, search GitHub for the frozen batch ID and exact contents *before* retrying; never duplicate an uncertain issue. Read the newly created issue back, verify title, batch marker, row count and content. Report only the verified published count. A published issue does **not** imply CareerOS consumed or approved it.

## Execution-limit and Gmail recovery

- Normal run: existing permitted 3-day Gmail lookback; when intake status is partial/unavailable or a previous extract incomplete: use the existing 7-day recovery rules. A lookback overlaps prior runs intentionally; dedupe every repeat.
- Within a run, track discovered message/card counts, completed extractions, remaining messages/cards, and failure type. Only set `Gmail status: ok` if *all* required providers/queries/pages and messages/cards have been processed; otherwise `partial` or `unavailable` as appropriate.
- **Critical unsolved requirement:** a Cloud ChatGPT scheduled task must demonstrate an authorized durable **private** checkpoint capable of preserving message/card progress between runs. The desktop SQLite ledger does not automatically become available to ChatGPT cloud tasks. Do not pretend GitHub public job facts or issue history are a private Gmail checkpoint. No raw Gmail identifiers in the repo. If durability is unavailable, fail honestly, preserve 7-day recovery, and defer activation until this is addressed or the owner explicitly approves a bounded reduced-coverage design.
- Minimize repeated reads: one shared Gmail discovery, one shared employer/ATS lookup, one shared history/index snapshot per run, separate routing/issue publication only. Execution budget is not authorization to silently skip messages.
- Always report the checked window, full/partial/unavailable status, number of extracted cards, deduped/discarded count, unresolved source verification, distinct PRIMARY and NIGHT issues, frozen batch IDs, and anything left for recovery **in the chat response only**, not in the private GitHub job-fact issue.

## Scheduling caveat

One **task** does not necessarily mean one **run**. The current automations cover NIGHT 6:30 AM, PRIMARY 8:30 AM and PRIMARY PM 8 PM America/Chicago. A single daily run will reduce coverage/freshness. Decide the new cadence with the owner; ensure tasks can actually represent it and that the desktop CareerOS consumption window still makes sense. Do not modify schedules until explicitly authorized.

## Mandatory acceptance tests

1. Same Gmail alert contains a PRIMARY-only, NIGHT-only and dual-eligible job: one discovery, correct routing and no lost cards.
2. Distinct requisitions sharing title survive dedup.
3. A failed or interrupted Gmail extraction emits `partial`, retains precise work-left accounting and recovers via allowed lookback.
4. One GitHub create succeeds but reply is lost: frozen batch ID prevents duplicate issue.
5. Open/closed issue history, comments and sharded seen indexes are read once correctly and do not silently block new jobs.
6. All employer, work-country/home-state and posting-active checks remain enforced.
7. NIGHT supplemental and replacement classification is factual and never substitutes for CareerOS scoring.
8. Never process recruiter correspondence or expose private mail/individual candidate information to GitHub.
9. The desktop reports consumer receipt and separate PRIMARY/NIGHT assessment after a real supported-source intake.
10. Run one test with zero jobs, one with more than 25 extracted candidates, one with unavailable Gmail, and one with overloaded GitHub history.

Do **not** activate as-is. Preserve all existing task policies as source-of-truth and obtain a final full-policy diff and owner-controlled cutover.
