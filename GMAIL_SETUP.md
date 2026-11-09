# CareerOS Gmail ingestion — single-user Windows setup

**Status:** local-only setup instructions for the existing built-in, read-only Gmail connector. No Gmail credentials or private messages belong in GitHub. Do not configure the user's account from a public GitHub Actions runner or expose OAuth files in a PR.

**Cost:** Google documents standard Gmail API use as available at no additional cost, with usage limits and a daily no-charge threshold. The built-in deterministic parser and SQLite ledger need **no Claude/OpenAI API key or usage credits**. The optional AI requirement reader is a different, separately configured feature. Check Google's current policy before significant commercial scaling: https://developers.google.com/workspace/gmail/api/reference/quota

## Google Cloud — one-time owner setup

1. In https://console.cloud.google.com/ create or choose a Google Cloud project under your own account.
2. Enable **Gmail API** (APIs & Services → Library).
3. Configure Google Auth Platform → **Branding / Audience / Data access**. For a normal personal Gmail account use **External**, not Internal (Internal is only for eligible Workspace organizations).
4. Request only `https://www.googleapis.com/auth/gmail.readonly`. This is a **restricted OAuth scope**: Google authorizes reading the mailbox, not just the job-alert senders. CareerOS's query is an app-level filtering rule, **not** a mailbox-level Google permission boundary. Never request Gmail modify, send, compose, or full-mail scope for this job-alert reader.
5. If testing, add only your own account as a **test user**. Google warns that External apps in **Testing** with Gmail scopes have refresh tokens that expire after **seven days**, requiring reconnection. For ongoing personal-only use, check Google Auth Platform's publishing options and Google's personal-use verification exception before changing status. Do not publish a general-access app or seek restricted-scope production verification just to run a local personal tool.
6. Under **Clients**, create an **OAuth Client ID → Desktop app**, then download the client JSON. Keep that file outside the source checkout, for example in your private CareerOS vault under `secrets/gmail_oauth_client.json`. Do **not** commit/upload the JSON, token, email, or vault contents to GitHub.

Official guides:
- https://developers.google.com/workspace/gmail/api/quickstart/python
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/identity/protocols/oauth2
- https://support.google.com/cloud/answer/13464323

## CareerOS — local Windows setup

Run these PowerShell commands in the **existing CareerOS repository folder** after creating your private vault and installing the project's documented Python 3.12 environment. Replace the example vault location with your actual one, and use the latest private working copy—not an experimental public branch.

```powershell
# Example only. Keep your real vault path PRIVATE, outside the GitHub repository.
$env:CAREEROS_VAULT = "C:\path\to\your\existing\careeros-vault"
Remove-Item Env:CAREEROS_DEMO_MODE -ErrorAction SilentlyContinue

.\.venv-dev\Scripts\python.exe -m careeros gmail status
.\.venv-dev\Scripts\python.exe -m careeros gmail connect --client-file "C:\path\to\your\existing\careeros-vault\secrets\gmail_oauth_client.json"
.\.venv-dev\Scripts\python.exe -m careeros gmail status
.\.venv-dev\Scripts\python.exe -m careeros gmail fetch --days 3
```

The connect command opens a **local browser sign-in** with PKCE and requires an intentional Gmail read-only authorization. The refresh token is stored in **Windows Credential Manager** under `CareerOS/gmail`, not in the GitHub repo. The connector uses the configured `operations.gmail_query` filter; review that private setting before enabling Gmail ingestion. Only add mail from known job-alert senders or a carefully controlled user-managed `CareerOS` Gmail label. Be cautious about broad sender domains and unexpected correspondence.

Each processed message is recorded locally in the SQLite `alert_messages` ledger to prevent repeat processing. The incoming email body is parsed in memory; only extracted leads and limited metadata are stored locally. Supported/verified ATS postings may be fetched and evaluated, while unsupported platforms remain manual leads under source policy. Separate PRIMARY/NIGHT assessment needs validation in the latest private engine before production cutover.

## Running and checking

- `careeros gmail status` checks connection and reports the matched search count and local processed-message count.
- `careeros gmail fetch --days 3` retrieves the configured alert search and processes new matches. For a historical backfill use a larger `--days` value **only after checking volume** and reviewing the query. The default Gmail search is three days.
- `careeros run` includes a Gmail pull **when Gmail is connected**, using the existing CareerOS Windows scheduler. Check the existing scheduled task on the owner's PC before changing any cadence.
- If Gmail API authorization expires or is revoked, `careeros gmail status` should report a reconnect requirement; reauthorize without deleting the local SQLite ledger.
- If more than the safe message-list limit match, the staged connector now **reports an incomplete search** instead of truncating silently. Narrow the Gmail query/date window in private configuration and recover all dates. This is not yet an automatic date-window partitioner.
- If a message cannot be parsed or a row fails during extraction, staged code leaves the message uncommitted for a later retry. This safety hardening has to pass CI and be integrated with the latest private version before activating.
- To revoke the OAuth token and local access: `careeros gmail disconnect`. This does not delete the historical local job and message ledger.

## Release sequencing and remaining limitations

1. Use a **synthetic** Gmail adapter test in CI, never real private emails.
2. On the private Windows version, perform a manual `gmail status` and narrowly scoped `gmail fetch`. Confirm observed leads and no accidental correspondence.
3. Validate that a NIGHT-only alert reaches NIGHT assessment, that duplicate URLs/requisitions remain separate as intended, and that rerunning `gmail fetch` adds no duplicate jobs.
4. Confirm that the Windows daily-run lock and scheduler work and that Gmail token refresh is stable after the consent-screen publishing setting is decided.
5. Only after desktop intake is proven should the three ChatGPT Gmail-search tasks be modified. Keep ChatGPT for *complementary external discovery*, not repeated Gmail parsing; preserve PRIMARY and NIGHT delivery policies and rollback.
6. A long PC outage beyond the configured lookback period can miss older alerts. For now, use a deliberate recovery fetch with a larger day window; true automatic watermark/date partitioning remains a future hardening task.
7. One Gmail query currently has a fixed upper bound of matching message IDs per fetch; the staged change fails visibly on overflow, rather than claiming success.

**Do not enable a paid AI API key, Gmail message sending, application submission, automatic task cutover, or live-mail ingestion in GitHub Actions as part of this setup.**
