# Unified discovery and GitHub intake — staging handoff

Status: **unmerged experimental branch**. Do **not** enable neutral INTAKE publication, disable existing jobs, or treat the posting-evidence prototype as production verification yet.

## Scope staged on this branch

- `careeros/discovery/github_inbox.py` recognizes explicit `INTAKE ...` and `TEST INTAKE ...` titles while keeping legacy PRIMARY/NIGHT interpretation.
- `careeros/cli.py` handles neutral issue rows using both PRIMARY and NIGHT title-family detection. An issue stays open when NIGHT is disabled; no silent fallback to PRIMARY.
- Malformed or failed neutral job rows keep the issue open for correction and retry; successfully accepted rows remain idempotent on subsequent pulls.
- Neutral issues only auto-fetch supported ATS postings if `board_mappings` already has an active, independently identity-verified, unpaused employer-to-ATS mapping. Other links become review leads. The legacy intake path is unchanged.
- `careeros/discovery/posting_evidence.py` defines conservative source-evidence states and the strict board-mapping trust predicate. The evidence checker itself makes **no network calls** and is **not yet wired** to full posting-activity verification.
- `tests/test_unified_inbox.py` and `tests/test_posting_evidence.py` cover the staged behavior using fake GitHub/ATS sources.
- `careeros/discovery/imap.py`, `careeros/discovery/gmail_recovery.py`, and the native Gmail intake now fail closed on overflow, missing metadata, oversized messages and incomplete rows; search overflow can split into bounded time windows while preserving the configured job-alert filters.
- Native Gmail intake retains PRIMARY, NIGHT-only, and dual-family leads, even if NIGHT assessment is disabled. Unknown employer-to-ATS ownership remains a review lead, never a verified automatic fetch. Tests are synthetic; no real mailbox was read.

## Architecture and rollout

**Release A** (owner-selected): CareerOS on the local Windows machine ingests permitted Gmail job alerts directly through read-only IMAP using a locally stored App Password and private SQLite processed-message ledger. The ChatGPT discovery producer stops re-reading Gmail after a shadow comparison and continues only *complementary external* employer/ATS research, emitting separate legacy `PRIMARY Job recommendations YYYY-MM-DD` and `NIGHT Job recommendations YYYY-MM-DD` GitHub issues. The consumer requires no replacement issue contract for this phase. Keep ChatGPT's existing tasks unchanged until local IMAP credentials, job-alert coverage, native profile evaluation, on-disk checkpoint behavior and the new non-Gmail producer have all been validated. No Gmail IDs or content may be moved to the public repository or GitHub issues.

**Release B**: a universal `INTAKE` transport, after validating the current private local CareerOS tree. Ingest each posting once; assess PRIMARY and NIGHT independently. The NIGHT processor already records results in `night_results` without overwriting PRIMARY scores. Legacy issues must remain supported. Do not start publishing neutral issues before confirming end-to-end desktop ingestion.

## November 9 synthetic report-quality fixes (staging only)

- Mail parser discards LinkedIn/Indeed/Glassdoor search-page and newsletter-header links, but retains real individual job links including LinkedIn search URLs carrying a specific job ID. Existing stored leads are **not automatically deleted**.
- Focused contact-center, claims, and service-operation supervisor titles can now be recognized as target families. When a supervisor title exceeds the current-level threshold, it receives a `supervisor_scope_review` **CONDITIONAL** flag; manager roles still trigger the original hard seniority gate. Nothing is automatically approved for submission.
- Generic `QA Analyst` and call-quality roles can be recognized. Generic QA titles require relevant operational/healthcare evidence; generic software QA must not pass the context gate by title alone.
- Qualifications stating a certification is "helpful" are marked `preferred`, not mandatory, with extractor revision `rules-v11`. Explicit required certifications and equivalencies preserve their established treatment.
- NIGHT report explicitly separates unknown-hour conditional jobs from scheduled near-misses; hours not stated remain **unknown**, never confirmed overnight work. The fresh-results heading counts **graded postings**, not unverified email leads.
- All tests use synthetic employers/job postings and emails; no private report or mailbox was committed to GitHub.

These code changes are **not retroactive** to the already imported Gmail-message ledger. Existing ungraded leads still require a controlled local review/reconciliation after the public branch is merged into the owner's private engine. Never delete that ledger or disable the existing tasks to force a backfill. The daily-run failure banner must be investigated separately using the private Windows log and scheduler output, not inferred from a successful Gmail fetch.

## Important cautions before activation

1. The main code snapshot's `config/policy/defaults.yaml` defaults to `night.enabled: false`; only the private local override can establish actual state. Neutral issues stay open while disabled.
2. `_record_alert` can record manual-site leads, but unsupported sources are not live-verified or automatically graded. Keep those as leads, not approved recommendations.
3. `_ingest_board_link` currently sets an employer to `verified` after a supported ATS posting fetch. That is not equivalent to the official employer-to-ATS board trust handshake. Reconcile with the board-verifier before handing publication verification to CareerOS.
4. `night.screen` still rejects managers and roles outside the NIGHT families. Preserve leadership candidates for manual role review without weakening eligibility grading without explicit policy authorization.
5. The public code may lag the owner's current private development tree. Compare commits and review changes before merging anything from this branch into the private implementation.
6. An ATS HTTP success proves the endpoint returned data; it does not alone prove employer ownership, region eligibility, or that applications are accepted. The new evidence prototype intentionally needs a separate positive activity signal.
7. Current GitHub inbox limits include 20 open issues per fetch and the first 200 issue-body lines. Keep publication batches compatible until the consumer is deliberately upgraded.
8. Maintain existing privacy, employer exclusions, compensation controls, no automatic applications, and verified-live publication rules during Release A. Any change moving verification downstream requires explicit policy revision and completed tests.

## Minimum integration checks

Run:

```sh
python -m unittest tests.test_github_inbox tests.test_unified_inbox tests.test_posting_evidence -v
python -m unittest discover -s tests -v
python tools/validate.py
```

Also simulate: one job eligible in both profiles, NIGHT-only title outside PRIMARY families, manager/leadership review, distinct requisition IDs under the same title, 404/429/timeout/provider mismatch, untrusted board identity, interrupted extraction, duplicate publication and uncertain GitHub issue creation. Validate a genuine desktop processing receipt before switching from legacy issue types.

**No production activation:** existing scheduled automations, mailbox, private GitHub recommendation issues, Windows task schedule, and local CareerOS database are unchanged by commits on this branch.
