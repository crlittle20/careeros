# CareerOS: AI-assisted development

- Version: 1.1
- Date: 2026-10-13
- Companions: `case_study.md`, `functional_mvp_report.md`, `security_architecture.md`, `adr/README.md`.
- Basis: the session records of the development work, the repository's history, and the owner's own account. Where a
  point could not be established from those records, it is left out or marked as open (section 7).

## Summary

CareerOS was built through a human-directed, AI-assisted workflow, with three distinct roles:
- **The owner** is the job seeker, acting as product owner.
  - Originated the concept and defined the operating problem and goals.
  - Supplied or approved the business rules and requirements, and made the decisions put to them for approval.
  - Authorized commits, and controlled release decisions and outward-facing actions.
- **ChatGPT**, by OpenAI, was the design and analysis partner. It produced the policy prompt series, the 10-part
  source-expansion specification, the 42-section board-verification specification and the Phase 1 audit. It also helped
  expand requirements, critique designs and find missed opportunities.
- **Claude**, by Anthropic, working through Claude Code, was primarily the implementation agent.
  - Wrote nearly all of the code and automated tests, and drafted most of the documentation.
  - Ran the automated checks, reported each milestone's results and proposed the commit messages.
  - Every commit carries a `Co-Authored-By` trailer that names it.

AI-generated work was not accepted automatically. The sections below describe how it was checked, and who did what.

### In the owner's words

> CareerOS was developed through a human-directed, AI-assisted workflow.
>
> I originated the project concept, defined the problem I wanted it to solve, established operating goals and
> acceptance criteria, and made or approved the product and workflow decisions that determined how the system should
> behave. In several cases I chose a different direction from the AI recommendation, corrected results that did not
> match my requirements, or required additional safeguards before work could proceed.
>
> ChatGPT served as a design and analysis partner. It helped expand requirements and produced the policy prompt
> series, source-expansion specification, board-verification specification, and Phase 1 audit.
>
> Claude served primarily as the implementation agent, translating requirements and decisions into code, tests,
> documentation, and repository changes. Claude also proposed some technical and architecture choices for my approval.
>
> I did not perform line-by-line human review of the code. I evaluated work through requirements and architecture
> review, targeted inspection, reported automated test results, functional and integration testing, and corrections
> when observed behavior did not match the intended design. I approved changes before they were committed.

## 1. Where AI assisted

| Activity | AI contribution | Owner contribution |
|---|---|---|
| Requirements and specifications | ChatGPT, as design and analysis partner, produced the policy prompt series, the 10-part source-expansion specification, the 42-section board-verification specification and the Phase 1 audit. Claude reconciled these with the repository and wrote the requirements documentation (`business_requirements.md`). | Originated the concept and defined the operating problem and goals. Supplied or approved the requirements, resolved open questions, and decided whether proposed changes became part of CareerOS. |
| Architecture and workflow design | ChatGPT challenged and expanded design ideas. Claude proposed implementation approaches and recorded technical decisions and their alternatives (`adr/`). | Made the decisions explicitly put to them for approval, and accepted, rejected or redirected the other proposed designs through the milestone process (section 6). |
| Code and tests | Claude did the primary implementation work, including the automated tests. | Evaluated whether the resulting behaviour matched the requirements, and sent work back when testing or observed behaviour showed it did not (section 3). |
| Documentation | Claude drafted most of the repository documentation. ChatGPT contributed requirements and design material that the documentation draws on. | Approved the documentation milestones, and supplied the first-person account above. |
| Research | ChatGPT supplied candidate employers, job boards and research notes. Claude checked the candidates independently before they were added to CareerOS; in the first batch, one board link in eight was wrong and was left out. | Decided what research to request, and whether its results should be incorporated. |
| Report checking | A Claude session operated Power BI Desktop to open, refresh and capture the generated report. | Authorized the desktop control. |
| Reports and milestone summaries | Claude drafted each milestone report: what changed, test results, unresolved items and a proposed commit message. | Reviewed each milestone's outcome and authorized its commit. |

## 2. What stayed human-controlled

The owner's role was not limited to accepting generated code. The owner controlled the following.

- **Project direction and priorities.**
  - Originated the concept; quality over volume was a stated principle from the first description of the project.
  - Chose which specifications and milestones to build, and in what order.
- **Factual job-search and profile data.**
  - Supplied the profile facts, the pay floor and the location rule, and decided which job titles their experience
    counts for.
  - CareerOS uses the profile only once it is marked approved. When an AI-drafted profile included a detail the owner
    did not want, the owner had it removed.
  - A screening-only experience setting the owner chose is kept apart from the factual years.
- **Acceptance criteria.** The owner set the conditions for accepting work:
  - the milestone rule (tests, regressions fixed, a written report, unresolved issues, then stop);
  - on 2026-10-12, a commit held until the planted-defect run had finished;
  - the conditions of the Functional MVP and Portfolio MVP gates.
- **Decisions put to the owner, including overrides of AI recommendations.** Confirmed examples:
  - **A skill minimum for Tiers A and B.** The owner required one, where the AI had recommended no separate minimum.
  - **Tier 2 experience titles.** The AI had added only Tier 1; the owner required Tier 2.
  - **Repository publication.** The AI recommended a private copy for CI; the owner chose to wait for a sanitized
    snapshot.
  - **Development tools.** The owner required that they be verified safe before installation.
- **Corrections after observed behaviour.**
  - The owner ran a live test of the recommendation hand-off and brought back the results. The problems they
    exposed were fixed at the owner's go-ahead.
  - The owner supplied the Phase 1 audit and asked for its problems to be fixed.
  - When the Gmail sign-in setup did not work, the owner asked for another way, and a read-only alternative was built.
- **External and security-sensitive actions.** In the sessions reviewed, publishing, pushing, sending, scheduling and
  downloading required the owner's approval, and new collection features were turned on at the owner's instruction.
- **Release and risk decisions.** The public-release strategy, and any open finding to be accepted rather than fixed,
  are the owner's call.

**What was delegated.** Not every decision was the owner's.
- Lower-level technical choices were made by Claude and accepted or sent back through milestone approval. Examples:
  module and test design, tooling configuration, and how settings are stored.
- Once, the owner handed over a night's design decisions with the instruction to approve the recommended options and
  keep a log. Sixteen decisions were logged for review.

## 3. How generated code was reviewed

CareerOS did not rely on line-by-line human code review. Claude did the primary implementation. Review combined
automated checks, which Claude ran, with the owner's evaluation of the results and of the system's behaviour.

**The milestone process:**
1. Claude implemented the requested change.
2. The relevant automated tests and validation checks were run.
3. Regressions were corrected.
4. Claude reported the changes, the test results and the unresolved findings.
5. The owner evaluated the milestone and decided whether it needed correction or could be approved.

The milestone instructions limited new scope to what a milestone needed, and required each report to state exactly
what changed.

**How the owner evaluated work:**
- requirements review, and architecture review where a milestone raised architecture questions;
- targeted inspection;
- review of the automated test results each milestone reported;
- direct functional testing of the running system: running its commands, labelling jobs on the review page, and
  reporting failures;
- integration validation, such as live tests of the recommendation hand-off from ChatGPT through a private repository
  into CareerOS;
- asking for corrections when observed behaviour did not match the intended design.

Targeted inspection does not mean that every file or line was read. The automated checks below are designed to catch
errors without anyone having to trust the code's author.

**Automated checks run by Claude.** Each milestone ran the test suite; planted-defect checks were run for the controls
a milestone touched. Since 2026-10-13, `tools/validate.py` also runs:
- ruff, including bugbear and security rules;
- mypy, which fails on any type error not in the recorded baseline;
- configuration checks;
- documentation tests that fail when a document names a file, test, table or command that does not exist.

**A second model, as a challenger rather than an authority.** ChatGPT's specifications and audits were reconciled with
the code rather than carried out unchecked, and its research results were checked before use. In the other direction,
Claude raised conflicts with standing rules as decisions for the owner. It flagged that reading employers' websites
would reverse an earlier boundary, and that pushing this repository would put personal settings on GitHub.

**Self-review is labelled as such.** The Functional MVP and Portfolio MVP reports state that the AI that implemented
the system also ran those assessments, which is not an independent review. They list what offsets that (section 4). A
review by a person or a tool outside this chain is still recommended.

## 4. How tests validated behavior

- **Tests come with the change.** Each milestone that changed system behaviour or documentation added or updated tests
  for the change. The suite has 419 tests on synthetic fixtures, fake transports and fixed clocks, and
  `tools/validate.py` runs it with outbound network refused.
- **Planted defects check the tests themselves.** `tools/mutate.py` deliberately breaks each safety rule, one at a
  time, in a scratch copy, and the suite must fail; a defect that survives is a rule with no real test. There are 284
  such defects. A full run on 2026-10-14 caught 283 of them. The one that survived, a private staffing agency probed for a
  board, had been reported caught in an earlier re-check for a reason that could not be found; its test was
  strengthened, and the final full run caught all 284. Spot checks made between 2026-10-13 and that re-check proved nothing: the tool's scratch copies lacked files some
  tests need, so every defect looked caught. The tool now refuses to run unless an unmutated copy passes.
- **Expectations do not come from the system.** The Functional MVP gate derives its 92 checks from the postings and the
  policy. For example, it recomputes tiers from the thresholds rather than reading the stored tier.
- **Proof that a gate can fail.** Both fixes prompted by the Functional MVP gate were proven by running the gate against
  the code from before each fix: it graded the affected requirement FAIL.
- **Real data, safely.** A rehearsal on a copy of the live database found a defect that no synthetic test had: the same
  jobs were re-evaluated on every run after a vocabulary change.
- **Golden records.** Demo Mode's totals are fixed in the tests, and two tours must give the same result.
- **Wrong expectations are recorded.** Two of the gate's own expectations were wrong on its first run. The report says
  so. The documentation that misled them was corrected, and the scenarios they assumed became listed gaps rather than
  silent passes.

**What the tests do not show.** They check specified behaviour, mostly on synthetic data. They do not measure whether
CareerOS's recommendations agree with the owner's own judgement. That is the Phase 1 exit test, which needs at least 50
of the owner's labels and has not been reached.

## 5. How security-sensitive changes were validated

Claude implemented and tested the mechanisms below. The owner set or approved the requirements behind several of
them: Demo Mode isolation, never printing secret values, no credentials in CI, and no publication before a sanitized
snapshot. The records do not show the owner inspecting the implementations themselves.

- **Threat to control to test.** `security_architecture.md` ties each threat to its control, code and test. Every
  control also has a planted defect that the tests must catch.
- **Boundaries are tests, not intentions.** Tests enforce:
  - the four-host allowlist for job boards;
  - the never-fetch list;
  - "only four providers are automatic" and "no site may auto-submit" (`tests/test_config.py`);
  - the vault living outside the repository;
  - Demo Mode's isolation, under a jail that records any attempt to reach the network, a credential or the private
    vault.
- **Probing, not assuming.** The website fetcher was probed with private, loopback, link-local, cloud-metadata, CGNAT
  and IPv4-mapped addresses. All were refused.
- **Secrets stay out of output.** The release scan reports a finding's type and file, never its value; the same rule
  applied to the AI's own output while it reviewed the repository. Logs are redacted. Since 2026-10-13 that includes
  GitHub and Google token shapes, and the job-board and Gmail clients ignore proxy settings in the environment.
- **Outward switches default to off.** New collection and employer-website reading are off in the engine's defaults.
  They were turned on in the private settings at the owner's instruction.
- **AI output is untrusted input.**
  - A researched board link cannot verify a board on its own, a safeguard Claude proposed and the owner approved.
  - Recommendations arriving from an AI assistant are data that is graded, never obeyed.
  - The optional AI requirement reader must quote the posting word for word, and the rules cross-check it.
- **Supply chain.** The owner's requirement was to install the development tools only if they were verified safe.
  Claude set the technical criteria and applied them:
  - ruff, mypy and pip-audit were confirmed as their official projects;
  - every package, dependencies included, had to be a wheel, with its SHA-256 equal to PyPI's, not yanked, and
    released at least 14 days earlier.

  The first attempt was rejected because ten packages were too new; so was the second, because of the newest pydantic.
  Older versions were pinned, and the result is hash-locked in `requirements-dev.lock` and audited with pip-audit.
- **CI.** The CI workflow uses no secrets, gets a read-only token that is not kept after checkout, and pins each action
  to a full commit. It has not run yet.

## 6. How architecture decisions were evaluated

- **Decision records.** Eight ADRs (`adr/`), written by Claude, each state the context, the decision, its
  consequences, the alternatives considered and the evidence. A documentation test fails if one loses a section or
  drops out of the index.

**Decisions the owner made explicitly:**
- **Tier rules.** The owner chose to require a skill-coverage minimum for Tiers A and B, even though the AI recommended
  using the tier states without one. The value of about 70% came from the AI's proposed option.
- **Tier 2 experience titles.** The owner required them after the AI had added only Tier 1.
- **Repository publication.** The owner delayed it until a sanitized snapshot exists, rather than the private CI copy
  the AI recommended.
- **Reading employers' websites.** The owner chose to allow it after one supervised check.

**AI-proposed technical decisions accepted with the milestone:**
- **Public release strategy.** Three strategies were compared, and a generated snapshot with fresh history was
  recommended over cleaning up this repository (`public_release_checklist.md`).
- **Formatting.** `ruff format` was not adopted, because it would rewrite 92 of 130 files.
- **Type checking.** It started from a recorded baseline rather than a big-bang fix.
- **Linux CI.** The Linux job was made informational until it has been seen to pass. It passed on its first run
  (2026-10-15) and stays informational for now.

**Decisions delegated to Claude.** Besides everyday technical choices, one night's sixteen design decisions were
delegated explicitly, under the owner's instruction to approve the recommended options, and logged for review
(section 2).

**Reversals are surfaced, not slipped in.** The board-verification specification required reading employers' own
websites. That reversed an earlier rule that CareerOS must not become a general website crawler.
- Claude raised the conflict with the owner.
- The feature shipped switched off, behind a page budget and robots.txt.
- The owner chose to allow it after one supervised check.

**Traceability.** Each requirement maps to code and tests (`requirements_traceability.md`), and documentation tests keep
the documents true to the code.

## 7. Limits of this approach

- **Shared blind spots.** The same assistant wrote much of the code, its tests and the gate that assesses it. Planted
  defects, independent expectations and live-data rehearsals reduce this, but do not remove it.
- **Privacy slipped early.** During fast early iteration, personal settings entered code comments, defaults and test
  fixtures. The repository test of that time checked only email addresses and API keys. A release scan with a private
  denylist found them, and on 2026-10-14 they were moved into the vault; the tests now use fictional values.
- **Known gaps:** a type-check baseline of 164 older errors; three PARTIAL items in the Functional MVP gate; Linux is
  checked only by the informational CI job.
- **Human inspection was targeted, not line by line.** See section 3.
- **Provenance.** Git history names Claude as co-author on every commit, but it does not record who originated each
  idea. This account rests on the session records and the owner's statement. Points those records do not settle are
  left out rather than assumed.

## 8. AI inside the product

This is separate from how CareerOS was built:
- **The optional requirement reader** (Anthropic API) sees public posting text only, never the profile. It must quote
  the posting word for word, it is cross-checked by the rules, and it falls back to them. It needs an API key, is
  switched off in the owner's own setup, and is never used in Demo Mode.
- **Recommendations** from a scheduled AI task arrive as issues in a private repository. They are untrusted data:
  graded like any other link, and never able to change a decision or the policy.
- **Research suggestions** are candidates. A researched link alone cannot admit a board. Admission needs the
  employer's own website, or, in the review band, a person's approval that expires.
