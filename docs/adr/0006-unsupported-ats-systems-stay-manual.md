# ADR 0006: Unsupported ATS systems stay manual

- Status: Accepted
- Decided: 2026-10-10 (site policy, PLAN.md v0.3); reaffirmed 2026-10-12 ("automatic collection remains limited to
  the four currently allowed ATSs")
- Recorded: 2026-10-13
- Related: FR-005, FR-035, SEC-008

## Context

Many employers use systems other than the four public listing APIs: Workday, iCIMS, SmartRecruiters, Taleo and
others. Their problems:
- most have no public listing API, and scraping their pages is brittle;
- some forbid automated access. SmartRecruiters' API `robots.txt` disallows every agent except LinkedInBot;
- some employers' and staffing agencies' terms of use prohibit scraping;
- LinkedIn, Indeed and Glassdoor must never be automated, and CAPTCHAs must never be bypassed.

Technical ability is not permission.

## Decision

- Automatic collection reads only **Greenhouse, Lever, Ashby and Workable**, through their public listing APIs.
- Every other system becomes a **manual-only** board or a **lead**: CareerOS lists it for a person to check, and never
  fetches it.
- `config/site_policy.yaml` (allowed / manual / blocked; anything unlisted is manual) is checked before every automated
  fetch. `safe_fetch.NEVER_FETCH` keeps these sites out of website reading as well.
- Adding a provider is a reviewed decision: a site-policy entry, an adapter, and tests.

## Consequences

**Gains:**
- Collection is permitted, polite and stable.
- The job seeker's employment and accounts are never put at risk.

**Costs:**
- A coverage gap. Employers on manual systems must be checked by hand; the report's "Manual-only sources" section
  lists them.

## Alternatives considered

- **Scrape Workday or iCIMS pages.** Rejected: terms, robots.txt and brittleness.
- **Third-party aggregator APIs.** Rejected: aggregators may discover a board but never verify one (ADR 0001), and
  their terms vary.

## Evidence

- Code: `config/site_policy.yaml`, `discovery/sources.py`, `discovery/http.py: PROVIDER_HOSTS`, `safe_fetch.py: NEVER_FETCH`,
  `site_inspect.py`.
- Tests:
  - `test_a_source_that_is_not_allowed_is_never_fetched`;
  - `test_t03_search_found_greenhouse_but_the_official_site_uses_workday`;
  - `test_t06_an_icims_careers_site_is_manual_only`.

## Amendments

- **2026-10-14.** One line of the context described the job seeker's own situation. It now states the
  general case. The decision is unchanged.
