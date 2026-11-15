# ADR 0001: The employer's own website decides which job board is theirs

- Status: Accepted; amended 2026-11-02 (a lead's domain is a hint)
- Decided: 2026-10-13 (board verification)
- Recorded: 2026-10-13
- Related: FR-031, FR-032, SEC-009; ADR 0002, 0003

## Context

Boards reach CareerOS from many places:
- alert emails;
- research (for example a ChatGPT-produced employer list);
- links in recommendations;
- guessed board names.

None of them proves who owns a board, and two failures showed the risk:
- Same-name employers collide. A board on one provider that carried a followed healthcare employer's short name turned
  out to belong to a mortgage company.
- With automatic activation on, a researched link alone was enough to "verify" identity.

Admitting the wrong board fills the job list with another company's postings. That wastes review time and can lead
to applying in the wrong place.

The one party that reliably knows which board is theirs is the employer, and the employer controls its own domain.

## Decision

A board is admitted to the daily scan only when the employer's official website vouches for it:
1. **The official domain names the employer.** Its homepage is read safely (`safe_fetch`).
2. **Handshake.** The employer's homepage or careers page links to, embeds, or redirects to the board.
3. **Identity.** The board agrees: its own name, the official domain in its postings, or its postings naming the
   employer. A different company's name with nothing else is a mismatch.
4. **Board Trust ≥ 85**, from the evidence alone, with every mandatory check passed.
5. Then quarantine and a validation scan (ADR 0003).

Everything else is a *candidate*:
- search results, aggregator links and alert links;
- research and guessed tokens.

Boards CareerOS already scanned before this decision ("legacy" boards) keep scanning during a grace period, until
they are verified or fail. Nothing becomes verified just because it already existed.

## Consequences

**Gains:**
- Impostor and same-name boards are refused. In the demo, Quillon's linked board names Quillon Logistics: it is
  rejected, and nothing is ingested.
- Every admission is explained by recorded evidence (`careeros sources explain MAPPING`).

**Costs:**
- CareerOS must read employer websites. This reversed an earlier "never read websites" boundary, so it is bounded:
  - `board_verification.fetch_official_sites` must be on (the job seeker switched it on after one supervised check);
  - pages are read only with `safe_fetch`;
  - page budgets apply;
  - robots.txt is honoured.
- A careers page that loads jobs with JavaScript shows no link, so the board goes to review (one followed employer's
  case).
  Whether to read same-domain script files is on the 2026-10-20 review list.
- The official domain must be known first. Boards without one wait in `verification_required`.

## Alternatives considered

- **Trust a matching name or board token.** Rejected: names collide, and tokens are guessable.
- **Trust aggregators and search results.** Rejected: they may *discover* a board, never *verify* it (FR-031).
- **Approve every board by hand.** Rejected as the default: too slow. It remains for the review band (trust 65-84),
  with a reason and an expiry (ADR 0007).

## Evidence

- Code: `careeros/discovery/board_verifier.py`, `board_registry.py: scan_gate`, `safe_fetch.py`.
- Tests: `tests/test_board_verification.py` (`test_t01` to `test_t30`; for example
  `test_t10_a_high_score_without_the_official_handshake_is_never_admitted`).
- Demo: `docs/demo_mode.md` (Quillon, Pinecrest, Harborline).

## Amendments

- **2026-11-02, a lead's `domain=` is a hint, never an authority** (the owner's decision D68, Part C of the
  lead-resolution batch; ADR 0003, amendment of the same date). A domain that only a lead supplied (for example from
  ChatGPT's look-up of the employer) becomes the employer's official-domain candidate only when the employer record has
  none and no other employer record uses it. It never merges two employer records and never creates an alias. The
  official-site check decides, as before, and more strictly for such a domain (as made precise after Codex review
  CR-2026-11-02-LEAD-RESOLUTION, finding F1, the same day):
  - the homepage must declare itself the employer: it declares a site name (`og:site_name`, `application-name`), and
    every one it declares is the employer's name, repeated tags included (Pass 3 of the same review), each compared whole
    (Pass 4). Its title never counts (Pass 7, the job seeker's choice: Python's HTML parser reads some titles differently
    from a browser, and a customer story's title leads with the customer). Only the document's own markup declares: a
    site-name tag in a textarea, an SVG, a comment, or after `<iframe/>` or `<script/>` does not. Inside a raw-text
    element (title, textarea, iframe, noscript, ...) everything up to that element's own end tag is text, as in a browser,
    and plaintext runs to the end of the page (Pass 8). A comparison of the reader with Chromium's HTML5 parser on 43,000
    generated pages, before Pass 8, closed four more differences: a page with a `<frameset>` is never branded; `<![` is
    read as HTML5 reads it; a site-name tag with an unterminated reference (`Example&nbspHealth`) never counts as the
    employer's; and inside a `noscript` a declaration counts only where the readings with scripts on and off agree.
    `tests/fixtures/browser_site_names.json` keeps the browser's reading of 886 of those pages as a regression check.
    Since Pass 9 the reader tokenizes as HTML5 does, not as Python 3.12.10's parser does: start tags, end tags and
    attribute values (only HTML whitespace separates, `==` is not `=`, references decode as in an attribute), and raw
    text to the end tag a browser accepts, a script's escaped sections included; the source check reads each `<meta>`
    the same way. A site-name key or content that only another reading accepts (other whitespace trimmed, other case
    folding) never counts as the employer's, nor does a site-name tag inside a `<select>`. The regression fixture grew
    to 2,069 pages. The source check charges each read every character it scanned, to the end of the page when the
    page ends inside the tag (a Codex Pass 9 note), so a page of tags left open costs linear time, and a page that
    spends the budget waits for the job seeker's review. The declarations the
    reader found must be exactly those in the page's source, read tag by tag, and the reader must have parsed the page
    without an error (Passes 6 and 7). A mention ("implementation partner of ...", or anywhere in its text) does not
    count. An employer's own site that declares no site name sends its nameless board to the job seeker's review;
  - a board whose own name is another company's is rejected before anything else;
  - postings that mention the employer, and the domain in question appearing on the board, never count as identity (a
    contractor's postings name its customers);
  - a board named exactly for the employer (legal suffixes aside) is confirmed; one whose name only contains it ("Example
    Health Foundation") waits for the job seeker's review (Pass 6); a nameless board (Lever, Ashby) is accepted only
    through that branded site linking it, and otherwise waits for the job seeker's review.

  A wrong domain can therefore only fail to verify; it never verifies another company's board for the employer. A
  domain the job seeker sets (`careeros sources domain`) is checked as before. Evidence: `board_registry.py:
  apply_domain_hint`, `board_verifier.py: verify_employer`;
  `test_lead_resolution::test_a_wrong_domain_can_only_fail_to_verify_never_verify_another_companys_board`,
  `test_lead_resolution::test_a_lead_domain_named_only_in_a_homepages_text_does_not_verify`,
  `test_lead_resolution::test_a_leads_domain_never_merges_employer_records_or_creates_an_alias`,
  `test_lead_resolution::test_a_board_named_for_another_company_is_rejected_whatever_its_postings_mention`,
  `test_lead_resolution::test_a_nameless_board_on_a_site_that_only_mentions_the_employer_waits_for_your_review`.
- **2026-11-02, later: the curated list's and `careeros sources suggest --domain`'s domains are hints too** (the owner's
  answer of the same day; item 11 of the lead-resolution batch). They are handled exactly like a lead's: they never merge
  employer records or create an alias, become the official-domain candidate only when the record has none and no other
  record uses it (`domain_origin` `curated`), and are checked by the stricter rules above. A curated website that is
  already another employer's is not used and is raised as a review case (`domain_conflict`), so the list's author sees
  it; a lead's domain that conflicts the same way is also not used but only counted (`lead_domain_conflict` in the daily
  upkeep's counts), with no review case (the clarification Codex asked for in review CR-2026-11-02-LEAD-RESOLUTION,
  D5). Unchanged: a website set with `careeros sources domain` is checked as before, and a website an employer record
  already had is not rewritten and keeps the check it had. Evidence: `board_registry.py: ensure_entity_for`;
  `test_lead_resolution::test_a_curated_or_suggested_domain_never_merges_employer_records_or_creates_an_alias`,
  `test_lead_resolution::test_a_wrong_curated_domain_can_only_fail_to_verify`,
  `test_lead_resolution::test_a_website_an_employer_record_already_had_keeps_the_check_it_had`.
