# Experience Match: shadow-mode experience-gap analysis

- Version: 1.0 (2026-10-15)
- Status: **shadow mode**. Experience Match is computed, stored, shown and measured against the owner's labels, but it
  changes no tier until the owner turns it on (`experience.enforce` in `config/policy/defaults.yaml`, `false`).
- Code: `careeros/qualify/experience.py`; parsing in `careeros/extract/rules.py`; evaluation in `careeros/review/labels.py`.
- Tests: `tests/test_experience.py`.

## Why

Before this, a years gap smaller than four years never reached the tier: it only lowered the Eligibility score, which
does not feed Apply Priority. A gap of more than four years on a hard-required line rejected the job outright. And a gap
was measured in absolute years only, so "1 of 2 years" and "4 of 5 years" were the same "slightly short". Experience
Match grades every gap, relative and absolute, and says what it would do.

## The score (20 points per years requirement)

| Part | Points | Bands |
|---|---:|---|
| Absolute shortfall (required minus counted, in months) | 0-10 | meets 10; up to 6 months 9; 7-12 months 8; 13-24 months 6; 25-36 months 4; 37-48 months 2; more 0 |
| Coverage (counted / required, capped at 100%) | 0-6 | 100% 6; 80-99% 5; 60-79% 4; 40-59% 3; 20-39% 2; under 20% 0 |
| Relevance (evidence of the one profile item counted) | 0-4 | direct 4; strongly transferable 3; total years for "relevant experience" 2; adjacent or not recorded 1 |

| Score | Class | | Coverage | Descriptor |
|---|---|---|---|---|
| 18-20 | strong_match | | 100% | full_coverage |
| 15-17 | small_gap | | 80-99% | high_coverage |
| 11-14 | medium_gap | | 60-79% | substantial_coverage |
| 7-10 | large_gap | | 40-59% | moderate_coverage |
| 0-6 | severe_gap | | 20-39% / under 20% | low_coverage / very_low_coverage |

Examples (direct evidence): 4 of 5 years scores 17, small gap, high coverage; 1 of 2 scores 15, small gap, moderate
coverage; 2 of 5 scores 11, medium gap; 0.3 of 3 scores 8, large gap. Months are exact decimals, so 4 of 5 is exactly
80% and a boundary such as 12 months short never drifts with floating point.

Every band, class and effect is policy (`experience:` in `config/policy/defaults.yaml`), part of the decision
fingerprint, so changing it re-checks every job.

## Facts stay facts

- The years **counted** are the screening value the profile sets (`screening_years`, else `years`), as the gate already
  does. The **factual** `years` is stored beside it and shown whenever the two differ.
- Years from different profile items are never added together, and adjacent experience is never turned into direct
  experience: it only lowers the relevance points.
- A domain the profile holds counts as direct evidence, as it already does in skill coverage.
- Recency is not scored: the profile has no dates per item, and Experience Match does not infer them.

## Reading the requirement

- **Ranges:** "3-5 years" asks for 3; the 5 is kept for the explanation.
- **Plus and minimum wording:** "5+ years", "at least 5 years", "minimum 5 years" and "five (5) years" ask for 5.
- **Months:** "18 months of claims processing experience" is 1.5 years. Months count only in an experience phrase,
  never in a deadline ("within 6 months of hire") or a probation period.
- **Two figures on one line:** "2+ years of SQL and 5+ years of claims" gives SQL 2 and claims 5 (the lowest figure
  used to apply to both).
- **OR and AND:** a line that offers alternatives ("or", "/", "one of") is judged on your best alternative; any other
  line naming several items is judged on the strictest one.
- **Conflicting figures:** two hard-required lines asking different years for one area are flagged
  `years_conflicting`, and the higher figure counts.
- **The decisive line** is the required line with the strictest future effect, then the lowest score. A preferred line
  is scored and shown but never decides the result while a required line can be scored.

## What it would do (the future effect)

| Class | Hard required | Required with equivalency, or ambiguous | Preferred |
|---|---|---|---|
| strong_match, small_gap | none | none | none |
| medium_gap | cap at Tier B | none | none |
| large_gap | stretch (Tier A/B to near-miss) | cap at Tier B | none |
| severe_gap | stretch | stretch | none |

- The hard rule is unchanged: a hard-required line more than `gate.years_major_gap` (4) years short is rejected by the
  gate, whatever the score. The rejection records the line's Experience Match, so the explanation shows why.
- In shadow mode the tier is never changed. With enforcement on, a cap or stretch is applied the way the degree
  STRETCH already is, and a tier is never raised.
- **High potential** is a note only: a medium or large gap where weighted skill coverage is at least 85% and the
  domain is direct. It changes no score and no tier.

## Where to see it

- `careeros explain JOB`: the experience sentences under eligibility, built only from stored fields.
- The review page: one Experience line on each scored card.
- `careeros eval`: a three-way comparison with the owner's labels (Tier A/B = apply; a near-miss with an explicit
  stretch flag = stretch; archive or rejected = skip; other near-misses are reported separately, never counted as
  stretch), the gap classes, and what enforcement would change.

## Calibration before enforcement

- `careeros eval` reports `EXPERIENCE_CALIBRATION_READY` once there are at least 50 PRIMARY labels, of which at least
  20 carry a years requirement. Nothing switches enforcement on: that is the owner's decision.
- A cutoff changes only when at least 3 labelled jobs in the same cell disagree with CareerOS and the change does not
  lower agreement elsewhere. Each change is recorded as an owner decision. The scoring never adjusts itself.

## Known limits

- The gate still judges a line on your best item, while Experience Match judges an AND-line on the strictest item, so
  the two can differ on a line such as "5+ years with SQL and Excel". The gate was left unchanged on purpose: making it
  strict would add rejections that calibration has not justified.
- "Bachelor's degree or 9 years of experience" is read as a hard degree requirement: the parser recognises equivalency
  wording such as "or equivalent" and "in lieu of", not "or N years". It is recorded for a separate decision.
- "N years of equivalent experience" names no area the profile can answer, so its years stay unknown.
