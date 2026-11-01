# Certification requirements

- Version: 1.0 (2026-10-15)
- Code: `careeros/qualify/certifications.py` (identity, matching, effects, explanations); reading in
  `careeros/extract/rules.py`; the catalog is `certifications:` in `config/policy/defaults.yaml`; demand in
  `careeros/review/certification_demand.py`.
- Tests: `tests/test_certifications.py`.

## What changed

Before, a posting that mentioned a certification CareerOS did not know produced only "a certification is mentioned".
Now every certification a posting names becomes its own record, saying which certification it is, who issues it, how
the posting words it, whether you hold it, how well your credentials match, and what that does to the decision.

## Reading the posting

- **One record per certification**, sentence by sentence, so "PMP preferred. CBAP required." gives a preferred PMP and
  a required CBAP. The source line is kept unchanged with every record.
- **Named credentials** come from a deterministic catalog of aliases: PL-300, PL300, "Microsoft PL-300" and "Power BI
  Data Analyst certification" are all `Microsoft PL-300 Power BI Data Analyst`; PMP, "PMI PMP" and "Project
  Management Professional" are one PMP. Short names that also mean other things (CSM, ITIL, CPC, Green Belt) count
  only in a sentence that says certification, certified, certificate or credential.
- **Families and issuers are kept at the level the posting gives.** "Scrum certification" is the Scrum family with no
  issuer, never a guessed PSM I; "certification through AHIMA and/or AAPC" is an AHIMA or AAPC certification.
- **Examples are not requirements.** "Project management certification (e.g., PRINCE2, PMP)" is one project-management
  family requirement; PMP alone is not required.
- **Unknown certifications are kept as written** (`unspecified certification`, with the source line), never normalised
  into something the posting did not say.
- **Not certifications:** pay-transparency text ("education, certifications, etc."), "certified as a Great Place to
  Work", certified copies, certification of records, certified data, board-certified physicians, TLS certificates,
  login credentials. A certification to earn after hire ("obtain the platform certification within 90 days") is
  on-the-job training, not a requirement. Licences and degrees stay their own requirement types.

## Requirement strength

The existing vocabulary, decided per sentence: `hard_required` ("must hold PMP", "PMP required"), `preferred`
("preferred", "desired", "a plus", "nice to have"; also "certification is not required"), `required_with_equivalency`
("PMP or equivalent", "CRC or similar", "degree or an ASA credential", "experience and/or certification"), `ambiguous`
and `responsibility`. The posting's own words are kept as the record's `wording`.

## Your status and the match

- **Status** (explicit since 2026-10-16): each profile item says `held`, `in_progress`, `planned`, `completed` or
  `expired`. Case, spaces and hyphens don't matter ("In progress" is `in_progress`). An item without a status, or with
  a status not on that list, is `unknown`, never held, and `careeros status` lists it. Two items for one credential with
  different statuses are `conflicting` and never held until you resolve them. Only an explicit `held` satisfies a
  requirement.
- **Kind:** `certification` (the default), `applied_skill` or `course_certificate` (also written "course"). A course
  certificate never satisfies a certification requirement, and a kind not on that list never counts as held
  (2026-10-18). A course named like a certification ("... Professional Certificate") is kept apart from the
  certification itself: they never conflict, and a requirement is matched to the certification first. A brief never
  says you hold a certification on the strength of a course certificate or an unrecognised kind (2026-10-20).
- **Evidence:** `evidence: {source, basis, checked}`, where basis is `self_reported`, `document_inspected` or
  `issuer_verified`. Cards and `careeros explain` show it ("held (self-reported)", "held (certificate checked)"); only
  `issuer_verified` is ever called verified, and a basis not on that list is shown as nothing and listed by
  `careeros status`.
- **Evidence that rests on a credential:** a skill, function or domain may list `credentials: [...]`. When one of them
  is not held, the evidence is kept as recorded but flagged for your review: `careeros status` lists it, and a draft
  marks the line UNDER REVIEW, in its requirements and its talking points, instead of presenting it as confirmed. A
  skill resting on a completed course is not flagged: the course is the training it rests on.
- **Match:** `exact`; `equivalent`, only through an owner-approved pair in the policy's `equivalent` list (empty by
  default); `family_match`, when the posting asks only for a family and you hold any credential of it, or only for
  an issuer and you hold one of THAT issuer's credentials (never another issuer's in the same field);
  `related`, a different credential of the same family, which never satisfies a named one (CAPM is not PMP); `none`;
  or `unknown` for a certification the posting does not name.
- **Alternatives:** "PMP or CBAP", "PMP, CBAP or PSM I" and "one of PMP, CBAP" are satisfied by any one, and judged
  once. Since 2026-10-18 only when they are named in one sentence with nothing but "or", "and/or", "/" or a comma list
  between them: "PMP and CBAP, plus Jira or Azure DevOps" is two requirements.

## Decision effect

| Requirement | Result | Effect |
|---|---|---|
| Hard required, named credential | held (exact or equivalent) | requirement met |
| Hard required, named credential | not held, planned, in progress or only related | rejected (`certification_required`), as CareerOS already did |
| Hard required, a family or issuer | a held member | requirement met |
| Hard required, a family, issuer or unnamed certification | none held | flag to check (`certification_check`), as before |
| Required with an equivalent, or ambiguous | not held | flag to check (`certification_gap` or `certification_check`) |
| Preferred | held | a plus, in the explanation only |
| Preferred | not held | no effect |

Certifications add no points to any score: they appear in no part of Candidate Fit, Career Value or skill coverage.
They affect a decision only through the gate's rejection and flags above.

## Where to see it

- `careeros explain JOB`: a Certifications list, one line per certification, built only from stored fields.
- The review page: one certifications row on each scored card.
- `careeros certifications`: demand across your target jobs. For each certification: how many jobs ask for it, how
  (hard required, with an equivalent, ambiguous, preferred), by month, your status, and how many jobs reject you for
  lacking it; also families, and the credentials you do not hold ordered by demand. It reports observed postings
  only, and it changes nothing about your plans.

The analytics export is unchanged; a certification table in the export and the Power BI report is a possible later
step.

## Demo Mode

The demo persona's certifications are synthetic (a held Scrum and Lean Six Sigma credential, a planned PL-300) and
carry no credential numbers or dates. One demo posting names a family the persona holds, a planned credential and one
it lacks; no demo tier changes.
