# Privacy

CareerOS runs on the job seeker's own computer for the job seeker alone. There is no CareerOS server, account,
telemetry or analytics service. This page says what personal data it handles, where that data lives, and what ever
leaves the machine.

## What it holds

| Data | Source | Where |
|---|---|---|
| Profile: skills, years, education, location, work authorisation | Written by the job seeker | `vault\profile\` |
| Policy: pay floor, target states, excluded employers, settings | Written by the job seeker | `vault\policy\overrides.yaml` |
| Jobs seen, decisions, scores, reasons | Public job postings plus CareerOS's own rules | `vault\data\careeros.db` |
| Labels, applications, dismissals, notes | The job seeker | `vault\data\careeros.db` |
| Alert-email leads: links, titles, the text beside each link, sender and subject | The job seeker's mailbox, read-only | `vault\data\careeros.db` (message bodies are never stored) |
| Reports, logs, backups, analytics exports | Built from the above | `vault\reports`, `logs` (redacted), `backups`, `exports` |

The vault sits **outside the code repository**, and CareerOS refuses to run with a vault inside it. Nothing from the
vault is ever written into the repository. Since 2026-10-14 the job seeker's own settings (employer families, sites
never to automate, staffing agencies, pay figures) live only in the vault (`policy/overrides.yaml`, `policy/private_sites.yaml`),
and the tests use fictional values. Earlier versions in the private development repository's Git history still
hold them, which is why the public repository is a fresh snapshot with its own history
([docs/public_release_checklist.md](docs/public_release_checklist.md)).

## What leaves the machine

| Destination | What is sent | When |
|---|---|---|
| Public job-board APIs (Greenhouse, Lever, Ashby, Workable) | Requests for public listings. Nothing about the job seeker. | Daily run |
| Employer websites | Requests for public pages, to verify boards | Daily run, when board verification is on |
| Gmail | Read-only queries for alert senders | Daily run, once connected |
| The job seeker's private GitHub repository | Replies to recommendation issues and the seen-jobs index. **Job facts only**: company, title, location, links, dates. Never the profile, pay floor, labels, scores or rejection reasons. | Daily run, when configured |
| Anthropic API (optional; only with a stored API key) | Public posting text only. Never the profile. | Only if AI reading is enabled |

Nothing else is sent. No application, message or email is ever sent on the job seeker's behalf.

## Demonstrations and public material

- **Demo Mode** uses a fictional job seeker, fictional employers on `example.com`, and a throwaway demo vault. In Demo
  Mode, the private vault, credentials, mailboxes, GitHub and the network are all refused
  ([docs/demo_mode.md](docs/demo_mode.md)).
- The committed analytics sample (`samples/analytics_demo/`) and Power BI report (`powerbi/`) contain demo data only.
- Anything published goes through [docs/public_release_checklist.md](docs/public_release_checklist.md). Private Git
  history is never published.

## Keeping and deleting

- History is kept on purpose: decisions are explainable and auditable. Delete everything by deleting the vault
  folder, including its 14 most recent daily backups.
- Disconnect Gmail with `careeros gmail disconnect` (it removes the stored credential). Remove other credentials in
  Windows Credential Manager.
- Reports and exports built on private data (including a Power BI report pointed at a private export) are private.
  Keep them in the vault and do not share them.
