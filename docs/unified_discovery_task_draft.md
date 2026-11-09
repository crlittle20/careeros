# Complementary CareerOS ChatGPT discovery task — STAGING DRAFT, NOT ACTIVE

**Architecture decision:** CareerOS Windows ingests Gmail job alerts directly through the existing IMAP App Password connector. ChatGPT discovery is **not** a Gmail reader or email checkpoint service in this design. This document supersedes earlier drafts where one ChatGPT task repeatedly pulled Gmail.

## Intent and authority

One future scheduled ChatGPT discovery task can research additional original-employer career pages and permitted ATS postings **outside Gmail**, deduplicate job facts against CareerOS's published sanitized seen index, and submit fact-only GitHub recommendations. CareerOS alone evaluates job fit, score, rank and eligibility. No automatic job applications. This public draft intentionally avoids copying private employer restrictions, compensation thresholds, work schedules or personal details.

**Until expressly approved:** leave the existing PRIMARY AM/PM and NIGHT automations enabled with their current schedules, job-discovery instructions and GitHub publishing rules. Do not create/activate a replacement task or alter any existing task solely because this draft exists.

## Native Gmail is the source for email alerts

- Read only the user's permitted alert sources via the local IMAP reader; keep the App Password exclusively in Windows Credential Manager.
- Retain processed Gmail message identities locally in SQLite; never send raw message IDs, correspondence, message text, credentials or the Gmail extraction checkpoint to GitHub or ChatGPT discovery tasks.
- The local run must report failed/partial Gmail extraction. Empty inbox, outage, truncated search or an unverified posting are distinct states.
- Use the local Gmail source's already-implemented canonical URLs/observations and the sanitized `seen_jobs.json` publication to prevent complementary ChatGPT discovery from rediscovering the same postings.

## External discovery work for the proposed ChatGPT task

1. Read the *latest* private GitHub inbox README, any authorized policy amendments, `seen_jobs.json` (including shards), `intake_status.json` only insofar as applicable, and issue history before making recommendations. Do not infer that a closed issue means a job was ingested.
2. Research only sources explicitly allowed by the existing tasks' policy and site-use rules, prioritizing original employer pages and authorized ATS APIs. **Do not use Gmail** in this replacement producer.
3. Preserve every existing private hard exclusion, employer-family hold, compensation rule, geographic requirement, work-type condition, allowed source list, job-age constraint, and privacy restriction. Unknown compensation or schedule is uncertainty, not a fabricated rejection.
4. Classify each discovered job as PRIMARY, NIGHT, or both using verifiable title, shift, employment and location facts. NIGHT-SUPPLEMENTAL / NIGHT-REPLACEMENT require schedule evidence; suitability grading remains CareerOS's responsibility. SUPERVISOR/leadership is a shared review category, not a third transport profile.
5. Verify a posting is currently accepting applications through an independently trusted original-employer or ATS source before publication, following the existing stricter producer verification rules. An HTTP 200, aggregation page, stale listing, or search snippet does not prove applications remain open.
6. Deduplicate using employer identity plus requisition ID and canonical posting identity. Different requisition IDs with the same title are not automatically duplicates. Check local discovered candidates and the published seen ledger together.
7. **Release A compatibility:** keep separate existing PRIMARY and NIGHT GitHub issue formats, titles, row limits and fact-only Note fields. Exactly one fresh issue per distinct nonempty profile batch, multiple same-day batches allowed, no rewriting an old issue, no splitting a batch to bypass limits. Freeze batch ID and row content before publishing and look up uncertain creations before retrying.
8. Confirm any publication through a GitHub read-back. Report incomplete discovery, uncertain source verification and publication errors in chat, never in private job-fact GitHub issue rows.
9. Preserve existing recovery, permission and safety controls exactly. If a task hits its execution limit, state incomplete coverage instead of claiming the job search was exhaustive. No new data source may be silently authorized.

## Cutover acceptance conditions

- Native IMAP ingest succeeds in the real private Windows checkout, with historical backfill and duplicate protection demonstrated.
- PRIMARY-only, NIGHT-only and dual-eligible Gmail alerts each generate appropriate locally tracked leads and independent eligible assessments when supported sources are trusted.
- ChatGPT's new producer does **not** access Gmail. Its external ATS discoveries do not duplicate the direct Gmail feed.
- Both existing GitHub issue transports remain compatible. All scheduled and manual batches preserve the owner's exact saved private policies, not merely this summary.
- Security audit finds no private email, credentials, hidden personal profile fields or unsupported ATS URLs in any public commit.
- Windows scheduler, local credentials, real-source ingestion, failure recovery and rollback are verified.
- Owner approves the replacement and the precise cadence. Preserve original task definitions for rollback; never disable old tasks first.

## Open release blockers

1. The public GitHub snapshot may not include the newest unpublished Windows development code. Reconcile carefully; don't overwrite it or expose its vault.
2. A long computer outage without a recent successful Gmail checkpoint may exceed the current search window. Time-splitting resolves oversized *searched* windows, not missing historical intervals. Date-watermark and multi-run reconciliation still need actual local integration testing.
3. Source verification prototype is conservative but not fully wired to provider live-application evidence.
4. Real IMAP and scheduled Windows runs must be owner-approved and locally verified before migration.
5. All final ChatGPT task prompts need a full private-policy diff; this is **not** a copy-paste production task.

**No merge, no live task changes, no real Gmail reads, no AI API credits or job applications from this document.**
