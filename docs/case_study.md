# CareerOS: case study

- Version: 1.1
- Date: 2026-10-29 (first version 2026-10-13; ranking calibration and dependency audit added)
- Companions: `README.md` (overview), `architecture.md`, `business_requirements.md`, `functional_mvp_report.md`,
  `ai_assisted_development.md`.

**About the numbers.** Every figure in this case study is one of three kinds, and each is labelled:
- **Engineering evidence:** measured in this repository (tests, planted defects, gates, scans).
- **SYNTHETIC:** a result of Demo Mode on fictional employers and postings. It shows that the logic works, not how a
  real job market behaves.
- **Observed on the private system:** timings already recorded in the requirements and gate reports, and one
  calibration against the job seeker's labels, reported as aggregate figures only (counts, means and ranges), never a
  label, job, employer or pay figure. They are observations, not benchmarks.

No outcome of the job search itself (applications, interviews, offers) is reported here.

## 1. Situation

One job seeker was looking for roles in a specialised field: healthcare operations and business systems, including
claims and pharmacy-benefit (PBM) work. Relevant openings were spread across many employers' own job boards,
daily alert emails from job sites, and recommendations from an AI research assistant. The search had to run
every day, alongside everything else, and it had to stay private.

## 2. Problem

Finding postings was easy. Working through them was not:
- **Too much to read.** A few thousand postings were tracked, and they changed daily. Most were irrelevant, but deciding
  that took reading each one.
- **The deciding facts were buried.** Remote rules, allowed states, degree requirements, years, seniority and pay sat in
  free text, often contradicting the title or the location line.
- **One opening, many copies.** The same role arrived from a board, an alert and a recommendation, sometimes as a
  repost or in several locations. Applying twice, or both directly and through an agency, can cost the opportunity.
- **Sources could not be taken at face value.** In live use, a board on one provider carrying a target employer's short
  name turned out to belong to an unrelated company. Aggregator pay is often an estimate. Some postings are scams.
- **Good roles hid behind generic titles,** and a pure title filter either missed them or let noise through.
- **No feedback.** Nothing measured whether any screening agreed with the job seeker's own judgement.

The goal was **conversion over volume**: fewer, better-qualified openings, each with its reasons, so the time goes
into applying rather than screening.

## 3. Requirements

The requirements are written as 113 numbered statements in `business_requirements.md` and traced to code and tests in
`requirements_traceability.md`. The ones that shaped everything else:

| Requirement | Why it is non-negotiable |
|---|---|
| Nothing is applied for, submitted or sent automatically (HR-006) | A wrong or duplicate application is reputational and cannot be undone |
| Every rejection, flag and score is explained in plain language (NFR-007), and unknown facts are flags, never matches | The job seeker must be able to trust, and correct, every decision |
| Decisions are reproducible: each records the policy, profile and extraction fingerprints (NFR-003) | A rule change must re-evaluate exactly the jobs it affects, and history must show why a decision changed |
| Personal data lives in a vault outside the repository (PRIV-001); secrets only in the credential store (SEC-001) | Privacy by construction, not by discipline |
| Polite, permitted collection only (NFR-005, SEC-005, SEC-008) | Published listing APIs, budgets, robots.txt; some sites are never automated |
| One failing item never stops a run (NFR-002) | A daily system has to survive bad data and outages |
| History is append-only and the audit log hash-chained (AUD-002, AUD-003) | Decisions can be checked after the fact |

## 4. Analysis

Working through real postings and real failures produced the findings that drove the design:
- **"Unknown" is the most common answer.** Many postings do not state pay, hours or eligible states. Treating a missing
  fact as a pass hides risk; treating it as a fail loses good roles. So unknown became a first-class value: a flag to
  check, scored as neutral, and surfaced as "check before applying".
- **"Remote" means several different things.** Remote anywhere in the U.S., remote in a few states, remote with a
  required time zone, or remote with onsite days. So every job carries a remote scope with eight values, and "Remote"
  alone is never read as nationwide.
- **Pay needs the whole range.** A range that straddles the floor, geographic pay tiers, or bonus pay with no stated
  base decides nothing, so these are unknown rather than a pass or a fail.
- **Finding a board is not proving it.** Alerts, research and guessed board names are good at discovering boards and
  bad at proving who owns them. Only the employer's own website can do that.
- **Three questions were being mixed.** "Is this board the employer's?", "is it working?" and "is the employer worth
  scanning often?" have different evidence. Mixing them would let a busy board look trustworthy, or an outage look like
  fraud.
- **Titles mislead in both directions.** A second, evidence-based look at the duties of ambiguous titles recovers
  relevant roles; a matching title is never accepted as evidence of who an employer is.
- **The job seeker's labels are the ground truth.** The system's tiers can only be judged against them, so labels and
  agreement measurement were built in from Phase 1.

## 5. Design

Eight architecture decision records (`docs/adr/`) capture the main choices, with the alternatives that were rejected:

| Decision | Rejected alternatives |
|---|---|
| The employer's own website decides which board is theirs (ADR 0001) | Trusting a matching name or board token; trusting aggregators and search results; approving every board by hand |
| Board Trust, Board Health and Career Fit are separate scores (ADR 0002) | One composite score; dropping boards with no relevant roles |
| New boards are quarantined until a validation scan passes (ADR 0003) | Ingesting immediately and cleaning up afterwards; validating by hand |
| Demo Mode is isolated from every private source (ADR 0004) | A separate demo application; mocks inside tests only; guarding only the network |
| `first_seen_at` is CareerOS's own date (ADR 0005) | The employer's posted date everywhere; the earlier of the two dates |
| Unsupported ATS systems stay manual (ADR 0006) | Scraping Workday or iCIMS pages; third-party aggregator APIs |
| A person decides at selected stages, and CareerOS does not apply or send (ADR 0007) | Auto-approving the review band; permanent manual overrides. Automatic applications are deferred to a later phase, not rejected. |
| One local store with enforced separation of duties and an audit chain (ADR 0008) | A server database; plain JSON or CSV files; an ORM |

The resulting architecture is a single Python process over one SQLite file. Each component may write only the tables
it owns. Every state change goes through a workflow matrix and writes history, the new state and an audit event in one
transaction. A daily run collects, verifies and processes only what changed, then hands a person a short, explained
list. The details are in `architecture.md`.

## 6. Implementation

- **Stack:** Python 3.12 and the standard library, SQLite, httpx and PyYAML. The anthropic SDK backs an optional
  requirement reader that must quote the posting word for word and falls back to the rules.
- **Size:** about 13,400 lines of engine code and 6,800 lines of tests, 38 tables, 12 forward-only migrations and 33
  commands.
- **Order of work:**
  1. Phase 1: board scanning, normalisation, the eligibility gate, scoring and the review page.
  2. Phase 1.5: application tracking, alert emails and a recommendations inbox.
  3. Source expansion: discovery from leads, title rescue, opening identity across sources, eligibility facts.
  4. Board verification against employers' own websites.
  5. A portfolio sequence: requirements and traceability, explainability, Demo Mode, a Functional MVP gate, the
     analytics export and Power BI report, architecture documents, a security and privacy review, CI, and this
     documentation.
- **How it was built:** through a human-directed, AI-assisted workflow. The owner set the direction and acceptance
  criteria, supplied or approved the requirements and decisions, and approved changes before they were committed;
  ChatGPT served as a design and analysis partner; Claude implemented. Every milestone ended with tests and a written
  report. `ai_assisted_development.md` explains what the AI did, what stayed with the person, and how the work was
  checked.

## 7. Controls

| Family | Controls |
|---|---|
| Decision quality | Hard rules with quoted evidence; only a hard requirement can reject; unknowns are flags; fingerprints on every decision; re-evaluation when rules change, with history kept |
| Source trust | The official-site handshake; identity agreement; a Board Trust threshold of 85; quarantine and a validation scan; expiring manual approvals that cannot override a mismatch |
| Data quality | DQ-001 to DQ-010: canonical links, cleaned text, full-range pay, remote scope, staleness flags, suspicious-scan protection, identity-drift pauses, reconciliation of the analytics export |
| Security | An SSRF-safe fetcher, a four-host allowlist, a never-fetch list, a local review page with a token, untrusted content as data, secrets only in the credential store |
| Auditability | Per-component write permissions, workflow matrices that fail closed, append-only history, a hash-chained audit log, idempotency receipts, `careeros verify` |
| Human control | Labels as ground truth, review cases, expiring approvals, dismissals with reasons, and no code that applies or contacts employers or recruiters |
| Release and supply chain | A release scan that never prints a value; a hash-locked development set; pip-audit; a CI workflow with no secrets and a read-only token |

Each security control is listed in `security_architecture.md` with the code that enforces it and the test that
proves it.

## 8. Testing

Tests had to show more than "the code runs". Five layers each answer a different question:

| Layer | Question it answers |
|---|---|
| Unit and integration tests, on synthetic fixtures, fake transports and fixed clocks, with the network refused | Does each rule behave as specified? |
| Planted defects (`tools/mutate.py`) | Would the tests notice if a safety rule were broken? Each defect breaks one rule in a scratch copy, and the suite must fail. |
| The Functional MVP gate (`tools/mvp_gate.py`) | Does the whole chain work end to end? Its expectations are written from the postings and the policy, not read back from the system, and it runs inside a jail that records any attempt to reach the network, a credential or the private vault. |
| Rehearsals on a copy of the live database | Does it behave on real volume and history, which no synthetic world fully reproduces? |
| Documentation and configuration tests | Do the documents name only real files, tests, tables and commands? Does the configuration keep its standing limits (only four automatic providers, no site allowed to auto-submit)? |

`tools/validate.py` runs lint, type checks, configuration checks, the tests, the gate and the security scans in one
command, the same command as the CI workflow.

## 9. Results

### Engineering evidence (this repository)

| Measure | Result |
|---|---|
| Automated tests | 485, all passing; repeated full runs on 2026-10-13, including a fresh clone, showed no flaky test |
| Planted defects | 305 defined. A full run on 2026-10-14, when 284 were defined, caught all but one; the survivor exposed a weak test, which was strengthened (below), and the next full run caught all 284. The last full run (2026-10-15, after Experience Match and certifications) caught all 305 (`evidence/mutation-receipt.json`). |
| Functional MVP gate | 92 checks: no requirement fails; 10 PASS, 3 PARTIAL (5 small gaps, listed in `functional_mvp_report.md`) |
| Release scan | No secret in the files or the Git history. Anyone can repeat the public repository's scan (`python tools/release_scan.py --history`); the private repository's result is reported, not inspectable. |
| Dependency audit | At release, the first CI run's pip-audit (2026-10-15) found no known vulnerabilities in either lock. Advisories published afterwards affected three pinned packages (certifi, filelock and msgpack), which were raised to fixed versions, with every pin checked against PyPI and OSV advisory data, and a local pip-audit of both locks, on 2026-10-29. After the update, that pip-audit found no known vulnerabilities. Two of the fixed releases were under 14 days old; the lock's header records them as exceptions. |

### Defects the controls caught

These were real defects, each found by a control rather than by chance:

| Defect | Found by | Effect if missed |
|---|---|---|
| The report counted boards that verification had rejected (including an impostor) as "verified" | The Functional MVP gate, reading the report as a user would | A misleading trust figure |
| The same jobs were re-evaluated on every run after a vocabulary change | A rehearsal on a copy of the live database | History growing by about 20 transitions a run, starting with the next morning's run |
| A job-level fact in the analytics model had no link to its job dimension | Building and checking the Power BI report in Power BI Desktop | Measures that look up job details failed and the shortlist table broke; fixed at the root, in the export's list of references |
| Two of the gate's own expectations were wrong | The gate's first run, recorded in its report | Both expectations were corrected in the documentation, and the missing scenarios became listed gaps |
| A refusal in Demo Mode printed a Python traceback | The Functional MVP gate (gap G6), fixed at the Portfolio MVP gate | A reviewer would read a crash where nothing had failed |
| The planted-defect tool's scratch copies lacked files that some tests need, so every defect looked caught | Building the public snapshot generator: running the unmutated suite in a copy | Spot checks from 2026-10-13 on proved nothing. The tool now refuses to run unless an unmutated copy passes. The next full run still found one weak test (a private staffing agency probed for a board): it passed whatever the private list said, because the fictional agency's name contained the word "staffing". It now uses a second agency whose name does not. |
| Ten development-tool packages were less than 14 days old | The supply-chain rule applied before installing | Older, established versions were pinned instead |

### SYNTHETIC: Demo Mode results

Fictional employers and postings, four simulated days, fixed by the tests:

| Measure | SYNTHETIC result |
|---|---|
| World | 15 fictional employers, 13 boards, an alert email and a pasted posting |
| Boards | 8 active, 1 historical after an ATS migration, 2 manual-only, 1 rejected as an impostor, 1 awaiting review |
| Jobs | 45 jobs: 27 scored, 13 rejected with stated reasons, 1 duplicate, 4 closed |
| Tiers of the scored jobs | 5 Tier B, 13 near-miss, 9 archive |
| Other routes | 13 routed to NIGHT, the separate overnight and part-time profile; 3 alert-email leads |
| Isolation | 182 requests, all answered by the synthetic world; nothing reached the network or the private vault |

These numbers describe a small hand-written world built to exercise every rule. They say nothing about a real job
market.

### Observed on the private system

- Private rehearsals at a few-thousand-posting scale finished in under two minutes: a daily run (PERF-001), and a
  re-evaluation of every job after the rule changes of 2026-10-13, with no errors and every job able to explain
  itself. These observations are functional evidence, not performance benchmarks.

### Ranking calibration: identify, measure, improve, validate

Observed on the private system on 2026-10-16, using the scoring code published here (commit `8ad32cf`) with the job
seeker's private profile and settings. It reports aggregate figures only (counts, means and ranges), never a label,
job, employer or pay figure.

| Step | Status | Where it stands |
|---|---|---|
| Identify | Done | The question: do the hard rules and the tiers agree with the job seeker's own Apply, Stretch and Skip labels (HR-001)? |
| Measure | Done, once | The latest label on each of 50 jobs (22 Apply, 11 Stretch, 17 Skip), compared read-only with the decisions stored after every job was re-judged under those rules |
| Improve | Not done | No rule, weight or threshold in this repository has changed in response |
| Validate | Not done | No change has been checked on labels given after it was chosen |

**What was measured:**
- **The hard rules agreed with the job seeker's 50 labels (in-sample).** They rejected 6 of the labelled jobs, all
  labelled Skip, each for a stated hard requirement. None of the 33 Apply or Stretch jobs was rejected.
- **The tiers did not.** 1 of the 57 jobs scored on the private system at the time (labelled or not) reached Tier A/B;
  the only top-tier job was one the job seeker had marked as wanted. None of the 22 Apply jobs did: 20 were
  near-misses and 2 had closed. Every scored Apply job's priority was below 77, under the Tier B line of 85; across
  all 57 scored jobs the median was 70.0 and the highest 85.3.
- **Priority did not separate wanted from unwanted.** Over the jobs still scored (the 20 open Apply jobs, and the Skip
  jobs the hard rules had not rejected), mean priority was 70.3 for Apply and 70.3 for Skip, to one decimal place. The
  requirement-match measures did separate them: mean eligibility score 86.0 against 67.7, and mean skill coverage 0.89
  against 0.64. Priority is 0.65 × candidate fit + 0.25 × career value + 0.10 × freshness; the eligibility score is
  not part of it. Career value averaged slightly higher for the Skip jobs (84.6) than for the Apply jobs (81.9).
- **The near-miss band rests on a private setting**, so these tier results cannot be reproduced from the public
  defaults alone.
- **The exit test is not claimed.** As coded, it asks for at least 50 labels, at least 90% of Tier A/B picks wanted
  (the precision figure), and at most 10% of wanted jobs rejected or archived (the recall figure). Not claimed: the
  coded thresholds were met, but the precision figure rests on one top-tier job, the recall figure rests on a private
  near-miss setting, and nothing in the test counts how many wanted jobs reach the top tiers.

**How much it can bear:**
- 50 labels is a small sample. Three pairs of postings are probably one role advertised in several cities, so it may
  be 47 roles.
- One person gave every label, mostly on jobs the review page showed, so a wanted job that CareerOS rejected or
  archived would rarely have been labelled.
- No label records a reason, so a Skip cannot be split into "not qualified" and "not wanted".
- Labels are judgements, not outcomes. No interview or offer is used.

**Not done: improve.** The calibration's leading hypothesis is to decide the top tiers on requirement match rather
than on priority: an eligible job with an eligibility score of at least 80, skill coverage of at least 0.70, no
medium or larger experience gap and no degree stretch. On the same 50 labels that rule would pick 12 of the 20 scored
Apply jobs and no Skip job, against none of either today. It was chosen on the labels it was scored on, so it is a
hypothesis, not a result. The configuration-only alternative, lowering the Tier B line to a priority of 70, would have
let 3 Skip jobs in. Nothing in this repository has changed in response.

**Not done: validate.** No change has been measured on labels given after it was chosen, so nothing is validated.
Private development is working on that check; it is not part of this repository.

### Not yet measured

- **Whether a changed tier rule agrees better with the job seeker.** The calibration above measured the published
  rules once; no change has been made, and nothing has been measured on labels given after it.
- **The source-expansion pilot's targets** (more apply-worthy openings, audited match accuracy, review time). They are
  instrumented, but provisional.
- **Job-search outcomes.** They are private and are not reported.

## 10. Lessons

1. **Evidence beats scores.** A trust score that could outweigh a missing handshake would have admitted exactly the
   same-name boards the design exists to stop. Mandatory evidence first, scores second.
2. **Unknown deserves its own value.** Most of the hard cases were missing facts, not wrong ones.
3. **Keep different questions in different numbers.** Separating legitimacy, health and usefulness made outages,
   impostors and quiet employers each handle cleanly.
4. **Tests written by the same author share blind spots.** Planted defects, expectations derived independently, and
   rehearsals on real data each found something the unit tests had not.
5. **Test what must never happen.** No code applies or contacts employers or recruiters, the tests run with the network refused, and Demo Mode is
   checked under a jail. Absence can be tested.
6. **Privacy has to be designed in at the start.** Early on, personal settings found their way into code comments,
   defaults and test fixtures. Taking them out later is a refactor of its own, and it is why the public version must be
   generated from an allowlist rather than cleaned up.
7. **AI assistance needs gates, not trust.** Several of the defects above were introduced or missed in AI-written work
   and then caught by the controls. `ai_assisted_development.md` describes those gates.
8. **A score has to be checked against the person it serves.** Priority averaged the same for the jobs the job seeker
   wanted and the ones they would skip. Only a comparison with labels showed it, and only new labels can show whether
   a fix works.

## 11. Next iteration

1. Close the five remaining Functional MVP gaps, including a demo employer whose own website contradicts a board and an overnight
   role that the overnight profile accepts.
2. Improve the tier rule that the calibration found weak, test any change side by side on new labels before adopting
   it, and add to the exit test a measure of how many wanted jobs reach the top tiers; then measure the
   source-expansion pilot.
3. Decide how to handle careers pages that build their job lists with JavaScript, after a week of verification results.
4. Review the lead-matching path that still uses an older identity check, and reduce the type-check baseline.
