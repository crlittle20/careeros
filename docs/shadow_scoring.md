# Shadow scoring: rubric v1 (read-only, not adopted)

- Status: **shadow only.** Nothing here changes a production tier, a gate decision, the decision fingerprint or any
  stored row. Production tiers still come from `tier_for` in `careeros/qualify/scores.py`.
- Added: 2026-10-16, in two parts: the rubric and the decision policy, then the comparison harness that runs them over
  a frozen read-only snapshot of the database and measures them against your labels (`careeros eval --scoring`).
- Code: `careeros/qualify/rubric.py` (the two axes), `careeros/review/shadow_policy.py` (status, caps, actionability,
  queues, ordering), `careeros/review/scoring_eval.py` (the harness), `careeros/review/role_identity.py`,
  `careeros/review/preferences.py`, `careeros/review/sampling.py`, `careeros/db/readonly.py`.
- Settings: `config/shadow_scoring.yaml`, a separate file that the decision policy never loads. Every number in it is a
  hypothesis to validate on new labels.
- Tests: `tests/test_rubric.py`, `tests/test_scoring_eval.py`.

## Why

A calibration review on 2026-10-16 found that the blended Apply Priority doesn't separate the jobs you'd apply to from
the jobs you'd skip: their average Priority was identical. Your eligibility (how well you meet a job's requirements)
and weighted skill coverage separated them clearly. So the shadow design scores **Qualification** and **Opportunity**
separately, and never lets a desirable job make up for weak qualification.

## Two axes

**Qualification** (100 points): required coverage 45, experience areas 20, duration 20, level 10, preferred
qualifications 5.

**Opportunity** (100 points): pay above your floor 30, progression 20, your stated domain priority 20, growth 10, work
arrangement 10, employment terms 10.

Each section is made of items, and each item has a state:

| State | Meaning | Lower bound | Upper bound |
|---|---|---|---|
| evidenced | your profile shows it | its credit (direct 1.0, strongly transferable 0.7, adjacent 0.4) | the same |
| unknown | no profile evidence, or the posting doesn't say | 0 | 1 |
| confirmed absent | you declared you don't have it, or a credential is only planned or in progress | 0 | 0 |

From these come four numbers on 0–100:
- **Q_low:** what's known to be met. It decides Tier A/B.
- **Q_high:** the best case. It decides Check First and the archive line.
- **C:** how much of the score rests on known facts.
- **Q_known:** performance on the known part. Display only.

Always Q_high − Q_low = 100 − C. These are bounds on the **rubric** score under the current reading of the posting and
your profile, not bounds on your true qualification.

**Sections with nothing to judge:**
- A posting that lists no required skills, or too few (under 2 importance points), gets an unknown remainder, never
  the neutral 0.70 that production uses.
- A posting with no years requirement makes duration not applicable.
- A title with no level term, or with terms from two different levels, makes level unknown.

## Status, actionability and queues

**Status:**
1. A gate rejection stays **rejected**, and the employer hold stays **held**.
2. **Tier A** needs every one of these:
   - state ELIGIBLE;
   - Q_low ≥ 90;
   - known coverage ≥ 90 and all required skills known;
   - a resolved duration (strong match or no requirement);
   - no untolerated decisive unknowns.
3. **Tier B** needs: state ELIGIBLE; Q_low ≥ 80; known coverage ≥ 70; enough listed skills; and an experience gap that
   is strong, small or not applicable.
4. **Check First** when the best case could reach B and something is still unknown.
5. Otherwise **Stretch review**, **Near-miss** or **Archive**, by Q_low and Q_high.

**Caps** can only lower a status:
- degree stretch → Stretch review;
- medium or large gap → Stretch review;
- severe gap → Near-miss;
- the core-skill alternatives below.

**Core-skill alternatives**, all computed side by side:

| Alternative | Rule |
|---|---|
| R0 | diagnostic only |
| R1 | a hard-required core skill you've declared you don't have → Stretch review |
| R2 | R1, plus one with no profile evidence → Check First |
| R3 | any core skill unknown or absent → Near-miss |

**Actionability** is separate from status. Your Skip or a dismissal declines the job, and a confirmed dealbreaker
preference sets it aside. The status itself is unchanged; evaluation always measures the status.

**Ordering inside a queue:**
- **Apply queue:** A before B, then known qualification.
- **Check First:** by the best case (O1) or by known qualification and the fewest facts to resolve (O2).
- **Opportunity:** orders jobs only. Its neutral midpoint is used just for ordering, and the lower-desirability note uses
  the upper bound, so unknown pay never triggers it.

## The comparison harness: `careeros eval --scoring`

It compares three approaches on the same jobs:

| Approach | What it is |
|---|---|
| Production | today's stored tiers (`tier_for`) |
| Baseline B0 | the calibration review's rule on stored values: ELIGIBLE, not a degree stretch, eligibility ≥ 80, production coverage ≥ 0.70 (its neutral 0.70 included) and an experience gap of strong, small or unknown; A also needs eligibility ≥ 90, coverage ≥ 0.90 and a strong match |
| Rubric v1 | this design, primary configuration first, with all eight R0–R3 × D1/D2 configurations alongside |

**Read-only by construction:**
- It copies the database with SQLite's backup API into a temporary file (or uses a copy you name with `--snapshot`),
  opens the copy read-only and deletes it afterwards.
- It never opens the live store, never takes the writer lock and never writes a log; it makes no network call.
- It writes only its report (in the vault's reports folder, under `scoring_eval`) and, with `--draw`, a calibration list
  (under `calibration`). Nothing in the vault's data folder changes. SQLite itself may create its usual empty side files
  next to the database when none exist, as any reader does.
- The report records the snapshot's SHA-256, checks it is unchanged afterwards, and records the decision fingerprint
  and the full evaluation identity (below).

**Role groups.** One opening listed in two cities is one decision, so it counts once:
- **Confirmed:** the same requisition or canonical URL, or a duplicate link.
- **Strong:** same employer, title, department, posting date and requirements, with posting text ≥ 0.99 similar.
- **Possible:** same employer and title only.
- Two different known requisitions are never one vacancy, not even through a third posting without a requisition:
  that posting stays in a group of its own (2026-10-20).
- The primary analysis groups at strong; the ungrouped run is a sensitivity check.
- A group with both a Skip and an Apply or Stretch is left out, and two sensitivity runs count it each way.

**Two kinds of labels:**
- **Your existing labels** are a development set: descriptive only, never population estimates, never grounds for
  adoption.
- **Calibration labels** are the ones you give on a calibration list after it was drawn. Only they enter population
  estimates.

**Calibration lists (`--draw`, about once a week):**
- Before the first draw, write down what results would make you adopt a scoring approach, in the vault's
  `profile\adoption_criteria.md` (UTF-8 or Notepad's "Unicode"). `--draw` refuses while that file is missing, unreadable
  or has no visible text, and each list keeps a copy of the text and its SHA-256 (taken over the normalised text, so a
  re-save with other line endings is the same criteria). A draw under criteria that differ from earlier lists' prints a
  warning. Results on your existing labels never count. Nothing is ever adopted automatically.
- A list is always drawn from a fresh copy of the live database: `--draw` with `--snapshot` is refused, because labels
  given since that snapshot would be missed.
- **A draw freezes what it will be measured against** (2026-10-17). Each list's record keeps:
  - every drawn group's members and every approach's status at the draw (production, baseline, the primary rubric
    configuration and all eight alternatives): its predictions;
  - the evaluation identity: evaluator version, rubric version, a hash of the shadow configuration and of the
    evaluator's source files, the primary configuration, the decision and profile fingerprints, a hash of your
    preferences, the grouping settings, the inclusion probabilities, the snapshot's SHA-256 and the adoption criteria's
    SHA-256.

  Later estimates read those frozen predictions and only the drawn members' labels, never statuses recomputed under
  later rules or postings that joined the role afterwards. The report names what changed in the identity since each
  draw. A list without frozen predictions, or one that is malformed or unreadable, is named and left out; it never stops
  the evaluation.
- **Which list a label answers:** the most recent list that showed that posting when you gave the label (a drawn group
  or a relabel check; a group considered but not drawn was never shown). A list counts each member's first label that
  answers it, so a later list never rewrites an earlier one. A change after your first answer to a list is not counted
  either: it may have been made after seeing the tiers on the review page, so it is no longer blind.
- **Pooling:** lists are pooled only when their predictions came from the same rules (evaluator, rubric, configuration,
  code, decision and profile fingerprints, preferences, grouping). Otherwise `estimates` is empty and
  `estimates_by_identity` reports each rule set on its own.
- Every role group not drawn before gets one inclusion probability from the first stratum it matches:

  | Stratum | Probability |
  |---|---|
  | the approaches disagree on recommending it | 1 |
  | recommended by all | 1 |
  | any rubric configuration gives Check First or Stretch review | 1 |
  | best status near-miss | 0.25 |
  | archive under every approach | 0.10 |
  | rejected by the gate (your target job families) | 0.05 |

- The draw is reproducible from its recorded seed. Held groups, closed roles and groups you already labelled are never
  drawn.
- A few groups you already labelled are mixed in, to check your labels repeat.
- The list shows titles, employers and links in a shuffled order: no tier, status or score. Don't open its record file
  (the one ending in .json) while labelling.

**What the report measures** (always the status, never your decisions):
- per approach: recommendations among your labelled groups, Skips among them, recall of Apply + Stretch and of Apply,
  and wanted jobs the gate rejected;
- for the rubric: Check First yield and recall including Check First;
- paired discordance tables between the approaches;
- wanted jobs found per review in Check First (O1 vs O2), and within-queue order by Opportunity (OM1 vs OM2);
- breakdowns per job family and employer, and an equal-employer-weight run;
- once a calibration list has labels: Horvitz–Thompson estimates of Skip leakage, recall, Check First recovery and yield
  and false hard rejections, each with its counts; and the relabel check. All of them use the predictions frozen at
  each draw.
  - **Check First recovery** = wanted groups in Check First ÷ wanted groups the rubric did not recommend: of the wanted
    jobs v1 would not have put in front of you as A or B, the share Check First brings back.
  - The share of *all* wanted groups that sit in Check First is a different quantity, reported as
    `check_first_share_of_all_wanted`.
- the free-text reason you gave with each job's latest label (in its job row), with counts. They are collected only and
  change no status or metric. Structured reason codes need a database change and are a separate step.

**Preferences (optional).** A `preferences.yaml` in the vault's profile folder lists dealbreakers, things to avoid and
wants. A dealbreaker counts only when a mention is confirmed (affirmative, in the title, duties or requirements). A
negated mention ("no phone work") is evidence the trait is absent. An uncertain one (hedged, in benefits or company text,
or a negation too far away) only adds a "verify" fact. Without the file nothing is detected.

## What it is not

- Not adopted: adopting it would be an explicit, recorded decision after a prospective check on new labels.
- Not a learning system: no weight is fitted from labels.
- Not a change to the gate, to Experience Match enforcement (still off) or to any site policy.
