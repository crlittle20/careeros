# Security policy

CareerOS is a local, single-user job-search engine. Its security design is documented in
[docs/security_architecture.md](docs/security_architecture.md): assets, trust boundaries, threats with their controls
and tests, secrets, outbound connections and residual risks.

## Reporting a vulnerability

Please report it privately. Use the repository's **Security → Report a vulnerability** form (GitHub private
vulnerability reporting); do not open a public issue. Include:
- what is affected (file, command, version or commit);
- how to reproduce it, with synthetic data only. Never send real personal data, credentials or job-search records;
- what an attacker could gain.

You will get an acknowledgement within 7 days. A fix or an explanation follows as soon as the issue is understood.

## Supported versions

Only the latest commit on the default branch is supported. There are no released versions yet.

## What is in scope

- **Fetching code.** `careeros/discovery/safe_fetch.py` and `careeros/discovery/http.py` must refuse internal addresses
  (SSRF), off-domain or off-allowlist redirects, oversized or non-HTML responses, and DNS-rebinding tricks.
- **Untrusted content changing behaviour.** Postings, emails, issues and web pages are data. Report any way one could
  alter the policy, scores, the hosts contacted, or the local review page (including prompt injection into the optional
  AI reader).
- **The local review server** (`careeros review`): its token, Host check and one-time form keys.
- **Demo Mode isolation** (`careeros/demo/`): any way a demo command could reach a real network, a credential, a
  mailbox, GitHub or the private vault.
- **History integrity**: separation of duties, append-only tables, the audit hash chain.

Out of scope:
- attacks that need the job seeker's own Windows account;
- the job boards' and Google's own services.

## How secrets are handled

- Credentials live only in **Windows Credential Manager** (`CareerOS/gmail`, `CareerOS/gmail-imap`,
  `CareerOS/anthropic`).
- The code never writes them to the repository, the database, reports or logs. Logs are redacted
  (`careeros/core/redact.py`).
- `tests/test_pipeline.py::test_repository_has_no_secrets_or_personal_contact_data` scans the repository's files for
  API keys and personal email addresses on every test run. `tools/release_scan.py --history` also checks every earlier
  version of every file, and the commit metadata.
- `tools/release_scan.py` is the release gate for anything made public. It never prints a matched value
  (`docs/public_release_checklist.md`).
- `.env` files are ignored by git, and CareerOS does not read them. `.env.example` lists the variables that exist.
- Test placeholders that look like credentials are marked `release-scan: fake` on their own line.

## Dependencies

- Direct dependencies are pinned in `pyproject.toml`: PyYAML, httpx, anthropic. Everything else is the Python
  standard library. `requirements.lock` records the engine's installed set.
- The checking tools and the same dependencies are hash-locked in `requirements-dev.lock` (wheels only, sha256 equal
  to PyPI's, every release at least 14 days old). `tools/validate.py`, here and in CI, runs `pip-audit` on both
  locks. YAML is only ever read with `yaml.safe_load`.
- There is no `eval`, `exec`, `pickle` or `shell=True` in the code, and ruff's security rules (`pyproject.toml`) keep
  it that way.
