# CareerOS native Gmail ingestion — IMAP App Password (Windows)

**Chosen architecture:** CareerOS reads only job-alert messages from Gmail through Gmail IMAP with an **App Password**, without a Google Cloud project, OAuth client, or paid AI API credits. The connector uses IMAP `EXAMINE` (read-only mailbox selection) and `BODY.PEEK[]` so it does not mark messages read or move/delete them. Do not modify the live desktop setup or scheduled tasks until this staged branch is reconciled with the latest private Windows code.

## Cost and security

- **Gmail IMAP:** No Gmail API credits or Google Cloud project needed for ordinary personal access. CareerOS's existing deterministic email parser and SQLite ledger need **no OpenAI or Claude API credits**.
- **Important security trade-off:** A Google App Password is a powerful account credential and is **not** scoped to read-only. CareerOS's *code* operates read-only, but anyone who steals the password may have broader mail access. A dedicated job-alert mailbox is safer than connecting a primary personal mailbox. Google recommends Sign in with Google when feasible; we are retaining the owner's intentional IMAP choice for this personal local workflow.
- Google App Passwords require **2-Step Verification**. They're not available for all accounts (e.g., some organization policies, Advanced Protection or security-key-only setups), and changing the Google Account password typically revokes existing App Passwords.
- The App Password belongs in **Windows Credential Manager**, at target `CareerOS/gmail-imap`, not in GitHub, the database, logs, task prompts, plaintext files or this chat.
- The Gmail search is limited by private `operations.gmail_query` to permitted alert senders and a designated label. Subject exclusions + a second application/recruiter-correspondence check prevent those messages from becoming job leads. **These are software filters**, not a Google-enforced mailbox access boundary.
- Official Google guides: https://support.google.com/accounts/answer/185833 and https://support.google.com/mail/answer/7126229

## Local IMAP setup — owner action, not a CI step

1. Review the private `operations.gmail_query` and the job-alert providers included. Avoid broad searches that unintentionally include private conversations.
2. Sign into your Google Account and, if eligible, create an App Password at https://myaccount.google.com/apppasswords . You may need to enable 2-Step Verification first.
3. On **your own Windows PC**, open a terminal within the existing CareerOS working copy. Store the App Password in **Windows Credential Manager** under generic target `CareerOS/gmail-imap`, with your Gmail address as the username. The existing implementation supports `cmdkey /generic:CareerOS/gmail-imap /user:<gmail address> /pass`; this prompts locally for the password (do not type it in this chat or put it in command history). Never paste the App Password into GitHub, logs, scripts or screenshots.
4. Keep `CAREEROS_DEMO_MODE` off during a deliberately approved *live* test (Demo Mode should refuse real Gmail reads). Verify the **existing** private repo and vault are backed up before using any experimental changes.
5. From the existing CareerOS folder with the private vault environment configured, run these commands:

```powershell
.\.venv-dev\Scripts\python.exe -m careeros gmail status
# After validating your working version and source-filtering rules:
.\.venv-dev\Scripts\python.exe -m careeros gmail fetch --days 3
```

These commands never require `careeros gmail connect`: that command belongs to the **alternative OAuth** route and is deliberately not the selected setup. If both an OAuth token and IMAP credential are present, current code prefers OAuth; revoke the old OAuth token only after checking whether it is used elsewhere.

## Local workflow, recovery, and validation

- `careeros run` already calls Gmail intake when an account is connected. Successful message identifiers are stored locally in SQLite (`alert_messages`) to suppress repeats; raw messages are not saved as job records.
- The staged Gmail changes **do not mark malformed or partially processed email complete**. Subsequent pulls can retry failed links while preserving previously stored leads.
- The staged IMAP reader **refuses** to silently discard older results when the search exceeds its match limit, when metadata is incomplete, or when an alert exceeds the safe raw-email parsing limit. Search overflow initiates bounded time-window partitioning (up to 60 days) through the same authorized query filters. Dense one-minute windows still fail visibly for manual recovery; no partial result is falsely marked complete.
- The `--days` option can support an owner-approved historical backfill, provided the source search is narrowly scoped. A long outage beyond the lookback or a search with more than the supported message cap needs explicit recovery; **do not assume a green daily run proves every job alert was captured**.
- Staged Gmail ingestion always recognizes both PRIMARY and NIGHT title families and applies the strict approved-employer-board trust gate. NIGHT-only manual leads remain available even when NIGHT assessment is disabled, but are never silently scored as PRIMARY. When enabled in private policy, the independent NIGHT processor assesses ingested postings. A stored manual lead is not an assessed, active, or apply-worthy job.
- All simulated tests run without account credentials, Gmail network access or expensive AI calls.
- Before production, reconcile unpublished Windows changes, test exact source policies, real IMAP read-only behavior, duplicate requisition handling, NIGHT-only/dual-family routing, supported ATS trust and the Windows scheduler. Keep the existing ChatGPT tasks active until a validated parallel comparison and owner-approved cutover.
- **Nothing in this staging branch is deployed to the desktop, merged to main or connected to your actual mailbox.**

## Disconnection / incident response

- If a laptop or App Password is compromised, remove that App Password in your Google Account and delete the `CareerOS/gmail-imap` Windows Credential Manager entry. The local lead/message ledger remains until separately removed.
- `careeros gmail disconnect` removes the configured Gmail credential; note that revoking an IMAP App Password **must also be performed in Google's account settings**.

OAuth is supported by the legacy code but is not the preferred path for this deployment.
