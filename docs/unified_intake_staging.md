# Unified discovery and GitHub intake — staging handoff

Status: **unmerged experimental branch**. Do **not** enable neutral INTAKE publication, disable existing jobs, or treat the posting-evidence prototype as production verification yet.

## Scope staged on this branch

- `careeros/discovery/github_inbox.py` recognizes explicit `INTAKE ...` and `TEST INTAKE ...` titles while keeping legacy PRIMARY/NIGHT interpretation.
- `careeros/cli.py` handles neutral issue rows using both PRIMARY and NIGHT title-family detection. An issue stays open when NIGHT is disabled; no silent fallback to PRIMARY.
- Malformed or failed neutral job rows keep the issue open for correction and retry; successfully accepted rows remain idempotent on subsequent pulls.
- Neutral issues only auto-fetch supported ATS postings if `board_mappings` already has an active, independently identity-verified, unpaused employer-to-ATS mapping. Other links become review leads. The legacy intake path is unchanged.
- `careeros/discovery/posting_evidence.py` defines conservative source-evidence states and the strict board-mapping trust predicate. The evidence checker itself makes **no network calls** and is **not yet wired** to full posting-activity verification.
- `tests/test_unified_inbox.py` and `tests/test_posting_evidence.py` cover the staged behavior using fake GitHub/ATS sources.

## Architecture and rollout

**Release A** (safest first): one authenticated, complete discovery producer examines permitted job-alert emails and job boards once. It emits legacy separate `PRIMARY Job recommendations YYYY-MM-DD` and `NIGHT Job recommendations YYYY-MM-DD` issues. The consumer requires no new contract for this phase. The replacement producer needs a real, **private durable checkpoint** that survives interrupted scheduled runs, with frozen batch IDs and idempotent publication. Do not put raw Gmail identifiers, email content, personal settings or checkpoints on GitHub. This branch does **not** implement the checkpoint or replacement task.

**Release B**: a universal `INTAKE` transport, after validating the current private local CareerOS tree. Ingest each posting once; assess PRIMARY and NIGHT independently. The NIGHT processor already records results in `night_results` without overwriting PRIMARY scores. Legacy issues must remain supported. Do not start publishing neutral issues before confirming end-to-end desktop ingestion.

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
