# ADR 0009: CareerOS prepares packages and fills forms on request; the person submits

- Status: Accepted (P2–P6 build, 2026-10-21)
- Decided: 2026-10-16 (the owner's assisted-workflow direction, plan v0.2); built from 2026-10-21
- Recorded: 2026-10-21
- Related: ADR 0007 (a person decides; CareerOS does not apply or send), ADR 0008 (one store, duties, audit chain),
  plan features APP-02 to APP-14

## Context

CareerOS finds and judges jobs well enough that the slow part of the search is now applying: retyping the same facts,
answering the same questions, and finding the right resume. The owner chose an assisted workflow (2026-10-16):
- CareerOS prepares applications from approved facts and answers;
- the owner opens the employer's form, asks for assistance, reviews it, and clicks Submit.

Automatic submission stays deferred (ADR 0007).

## Decision

1. **Approved material only.**
   - Facts (`profile/approved_facts.yaml`) and reusable answers (`profile/answers.yaml`) are files the owner writes in
     the vault. CareerOS reads them and never writes them.
   - An answer is reused only for the exact question it was written for, or for a phrasing the owner listed with it.
     A model's judgment that two questions are similar is never enough. A contact detail (a name, email, phone, link or
     location) is bound to its own standard field instead, and fills only that field, where the form labels it so (on
     Greenhouse: the names, email and phone, an unlabelled email or phone field read by its name; link and location
     details are offered in the panel only).
   - An expired answer needs review.
   - Sensitive or voluntary questions (demographics, disability, veteran status, signatures, legal attestations) are
     the owner's to answer on the form, not from the file. Since the 2026-10-22 amendments two checks hold them back:
     each answer declares a kind from a closed list that has no kind for them, and each kind is a closed set of
     question shapes; a wording check is the second line. Each holds back what it recognises; neither is complete (see
     the amendments and the guide's Limits).
2. **Packages are versioned and bound by hash.**
   - A package is the brief, the matched answers, honest gaps, the facts used and the documents (by SHA-256) for one
     posting. Its manifest's SHA-256 identifies the version.
   - A change to an answer, fact, document, question or destination makes a new version. An approval covers only the
     exact version it names (APP-04, APP-07).
3. **Two routes, never a fake label.**
   - A job the owner labelled Apply, or one the standing preparation policy selects, is ready for assistance once
     prepared.
   - A Stretch job's package needs the owner's approval of that version. Several can be approved together as a named
     batch, but a package added later needs its own approval.
   - Standing preparation never writes a label, and Skip, dismissal and an existing application always win (APP-05,
     APP-06).
4. **A handoff is a reservation.**
   - Before any package leaves CareerOS for a form, `submission.authorize` must allow it. That covers eligibility, the
     employer's identity, submission holds and competing submissions.
   - The shared duplicate check must also be clear, and no other session may be open for the role.
   - The session stays open until the owner closes it: submitted (recorded as an application), not submitted, or
     uncertain. An uncertain session blocks a new handoff for the role until it is reconciled. Nothing is ever resent
     automatically (APP-11, APP-12).
   - Closing a session does not withdraw its handoff file. The extension cannot see sessions, so the file can still fill
     the same posting until its own expiry (two hours at most). The owner deletes it from `vault\handoff` to end it
     sooner.
5. **Evidence is named.**
   - An application recorded from a session keeps its evidence: `owner_attested` (the owner says they submitted) or
     `confirmation_observed` (the owner saw the employer's confirmation).
   - A filled form is never evidence of an application.
6. **The browser extension cannot submit** (APP-08 to APP-10).
   - It is Manifest V3, with only `activeTab` and `scripting` permissions: no host permissions, no network and no
     storage of applicant data.
   - It reads a per-session handoff file the owner selects (one package, one session). The file holds only the values
     for that form, expires after two hours, and is bound to its adapter's reviewed origins (two for Greenhouse, which
     serves a posting from both hosts) and one posting path.
   - It shows every value before filling.
   - It fills only fields named by a fixed, reviewed adapter. It never clicks, presses Enter, submits, navigates or
     uploads; the owner attaches files.
   - Tests check its source for those calls.
7. **Destinations are permitted one by one.** Each destination's `fill` capability must be `supervised_only` or
   `allowed` before a handoff is written. Unknown means manual: the owner copies from the package's answer panel. Since
   the 2026-10-22 amendment, `config/site_policy.yaml` (`assistance`) ships every destination at `unknown` and records
   what each was reviewed for; the owner turns one on in the vault overrides, after the installed-extension check.

## Consequences

**Gains:**
- Preparation time drops without giving any code the power to apply.
- Every value that reaches a form traces to an owner-written fact or answer.
- Approval cannot drift onto changed content.

**Costs:**
- The owner maintains `answers.yaml` and approves Stretch packages.
- Each new destination needs a reviewed adapter and an explicit permission.
- The extension's page behaviour is run in Node on a synthetic page (when a Node runtime is available), and its
  installed behaviour is checked once by the owner before filling is turned on; the unit tests check its source and
  its handoff data everywhere.

## Alternatives considered

- **Automatic submission.** Deferred (ADR 0007). It would need its own record, site permission evidence and the
  owner's activation.
- **Similarity matching of questions by a model.** Rejected: a plausible match is not the owner's answer to that
  question.
- **A native-messaging bridge from the extension to CareerOS.** Not built: a per-package file the owner selects is
  enough to prove the mapping, and it grants nothing persistent.
- **Host permissions for every ATS.** Rejected: `activeTab` covers the one page the owner asks to fill.

## Evidence

- Code: `review/answers.py`, `review/packages.py`, `review/assist.py`, `extension/`.
- Tests: `tests/test_packages.py`, `tests/test_assist.py`, `tests/test_extension.py`,
  `tests/test_extension_behaviour.py`.

## Amendments

- **2026-10-22, the independent Codex review of `ac83e00` (Pass 1, BLOCKERS-REMAIN), and a second, adversarial
  Claude review of the first corrections.** The decision stands. Codex's eight findings were each reproduced first;
  the second review found the first corrections incomplete in places (three High findings, all the same gap: the
  sensitive-question check was still a keyword list that common wordings passed). Every finding is covered by a
  regression test, and every one except the routine questions held back as false positives (R11, which fails safe and
  is guarded by the `ROUTINE` list in `tests/test_sensitive_questions.py`) by a planted defect. The rules then held as
  follows (the later amendments below narrow some of them):
  - **Deny by default for sensitive questions.** Each answer declares a `kind` from a closed list (work
    authorization, start date, experience...). There is no kind for consent, attestation, demographics, disability,
    veteran status, signatures, checks, identity documents or salary history, so the file is refused if an answer names
    another kind. A wording check is the second line: an answer is never used when its question, a phrasing, its own
    short text (80 characters or fewer; longer answers were prose and not read, until Codex's Pass 2, below), or the
    form's question or choices read
    as one of those categories, compared in a canonical form (Unicode
    compatibility forms, no invisible characters, plain quotes). The check is conservative, not complete: it is tested
    against a corpus of real-world wordings (`tests/test_sensitive_questions.py`), holds back a few routine questions,
    and says which words held each answer back.
  - **One answer, one id.** Ids are 1 to 64 ASCII letters, digits, `-` and `_`, starting with a letter or digit, and
    two answers can't share one, whatever their case. A package shows or hands over only the exact answer it was
    prepared with, by id and the hash of its text; the fallback panel and every handoff also check the hash of the
    question it was written for.
  - **The fallback panel.** When a posting's questions aren't known, the panel offers only current answers not held
    back, lists those it holds back and why, and both are part of the package version. It no longer promises what the
    wording check can't guarantee; it tells the owner which questions are always theirs.
  - **Checks at the moment of filling.** The extension checks the file, its expiry, the tab in front and its posting
    again when the owner presses Fill. The page function checks them once more against its own copy of the reviewed
    hosts, posting-path shape and field names (CareerOS, the popup and the page hold equal copies, compared by tests),
    so neither a file nor the popup can widen them.
  - **What the handoff is trusted for.** It is unauthenticated vault content, trusted only as far as the Windows
    account is: for its values and the posting it names (which must fit the reviewed posting-path shape). Not for
    which hosts or fields it may fill, and not for how long: a handoff claiming more than two hours (plus five minutes
    for clock differences), or an expiry that isn't a UTC time, is refused.
  - **Choices and fields.** A choice already made is kept whatever its position; only an empty placeholder is replaced.
    A choice is picked by its position, never a disabled one or one of two that read the same. A field name that more
    than one element answers to is left to the owner.
  - **Roles already applied to are never prepared.** Whatever the route or label: every session there would be refused
    by `submission.authorize`, so a package would only look ready. Re-application is the owner's own step outside
    assistance (`careeros applied --reapply`).
  - **A repeated session open or close with the same key** returns the first result, whole; a different request with
    that key (including another explanation) is refused. (A repeated package approval or revocation is acknowledged
    as a replay while that version is still the current one.) A handoff is written before its session is committed,
    so a failed write leaves no session behind. An unreviewed posting address is one of the refusal reasons, not a
    later error.
  - **Filling ships off.** Codex asked for a supervised check of the installed extension before it fills a real form.
    Every destination ships at `unknown`; the owner turns Greenhouse on in the vault overrides with the date of that
    check (docs/assisted_applications.md, section 5), and never beyond what it was reviewed for. That check runs the
    installed extension on a synthetic file; CareerOS's own handoff and session are first used, supervised, on the
    first assisted session (the guide's step 7).
  - **Tests and validation.** The extension's behaviour runs in Node when one is available
    (`tests/extension_harness.cjs`). Its source is held to allow-lists of every name and member it uses.
    `tools/validate.py` says when the behaviour tests were NOT RUN, and fails a `--full` run without them.
  - **A third review (fresh Claude instances again, 2026-10-22) of the second round** found no High in the sessions
    or the extension, and two in the answers: wordings the list still missed, and unquoted YAML values (`Yes` read as
    true). The kind is now checked against the wording of both the owner's question and the form's, so "deny by
    default" is a rule rather than a label: a question that reads as no reusable kind is never answered from the file,
    whatever the wording list misses (the next three reviews found that this check leaked too). A question or answer
    that YAML reads as something other than text (an unquoted `Yes`, `02134` or `2:30`) is refused; other unquoted
    text is read as YAML reads it (a ` #` starts a comment), so the guide says to quote everything. The wording list was widened again and reads a stricter canonical form (HTML entities,
    combining marks, blank letters, look-alikes, trailing punctuation). The documented override date no longer stops
    every command, and must be a real YYYY-MM-DD date, not after today. An interrupt
    after commit keeps the recorded session's handoff; a replay says what became of its session. The extension
    leaves lists of several choices, disabled option groups and choices the page rejects to the owner, and its source
    tests read every string, regular expression and subscript through a small lexer, so a literal can't hide code.
  - **A fourth review (2026-10-22) broke the third's kind check broadly** (56 leaks: a kind's trigger word anywhere in a
    question, "visa" or "years of" or "license", matched sensitive questions too). Each kind is now a closed set of
    whole-question shapes (`KIND_SHAPES`), matched from start to end, with a veto on a named skill or certification that
    is about the person; a contact detail fills only its own standard field, where the form labels it so. Every one
    of that review's 246 questions now fits no kind. With the shapes carrying the main rule, the wording list was
    narrowed to the person ("your medical condition", not "medical history abstraction"), which cut the held-back
    routine questions in the owner's own field from 25 of 48 to 2 at the time (the current figures are in the last
    amendment).
  - **A fifth review (2026-10-22) found 20 more leaks, all through a shape's free parts** (a certificate's name taking
    "DOT medical", an "(if so, list your final rate)" clause, sponsorship tails, a second sentence after a newline),
    status lists as choices, and contact entries in the fallback panel. Free parts are now short names (no clause,
    no sentence), with a wider veto; sponsorship tails are fixed phrases; an authorization answer fills a drop-down
    only when its choices are a yes or a no; a contact detail must be written for its own field. All 20 are held,
    and 52 of a 58-question sample of routine claims-analyst questions still fit a kind.
  - **A sixth review (2026-10-22) found 13 more**, through the names still allowed in a shape ("a background
    clearance", "an FBI clearance", "years of combat experience") and authorization answers carrying a status ("Yes,
    on F-1 OPT"). Besides closing those (real clearance levels only; a wider veto and wording list; authorization
    answers and their choices a plain yes or no), the impact of what remains is narrowed: CareerOS never puts an
    experience, certification, previous-employment, education or motivation answer in a handoff. The owner copies those
    from the panel, reading each one. The guide says plainly that neither check is complete.
  - **A read-only verification of the Codex Pass 2 package (fresh Claude instances, 2026-10-22)** checked each claim
    against the code. Six kinds the extension fills still had free parts (relocation, work location, referral
    source, language, schedule and availability: a relocation reason or destination, a commuting destination or named
    office, a referral name, a language, a time zone), so "Are you willing to relocate for a pregnancy?" could be
    answered and filled. Those parts are closed: only the five named kinds above have a free part
    (and theirs is vetoed), and a test holds every other kind's shapes to fixed words, digits and closed lists of
    languages and time zones. "veterans" joined the wording list. The cost in the owner's own field is now recorded in
    the tests (synthetic samples): of 58 routine claims-analyst application questions, 52 fit a kind; of 48 routine
    claims-operations questions, 32 fit no kind (16 of them only because a word such as member, disability or
    maternity is on the veto) and 3 read as sensitive. Those are held back, so the owner answers them on the form. The
    same check corrected overstatements here and in the guide and strengthened tests and planted defects it found
    weak. It was neither adversarial nor independent; Codex Pass 2 is the independent check.
  - **2026-10-22, the independent Codex review of `4787e2a` (Pass 2, BLOCKERS-REMAIN).** Seven of the eight Pass 1
    findings and the behavioural tests were resolved; the installed-extension check stays the owner's, pending. One
    High remained in F3: an answer's own text was read only when 80 characters or fewer, so a 121-character start date
    carrying "I agree to the privacy policy", and "I agree" padded with invisible characters, were prepared, handed
    over and filled. Now every answer's own text is read, whatever its length, in the canonical form, and the same
    check runs again as each value leaves CareerOS. And an answer over 120 characters (`ANSWER_FILL_CHARS`) is never
    filled: like a named answer, it is prose, and the owner copies it from the panel, reading it. Regression tests
    follow Codex's two cases from the answers file to the page through the real extension scripts, and record the cost
    in routine long answers (8 of 18 held back, for words such as "I agree", "consent forms", "to confirm", "on file"
    and "pay"; 1 copied instead of filled). A read-only check of that correction (fresh Claude instances, a skeptic
    per finding) then found words the checks could not read the way the page shows them: a line break inside a word
    (read as a space, dropped by a text box), look-alike letters outside the fixed list, small capitals, unassigned
    invisible characters, and padding or entities that made the written text far longer than the text as read. So
    the extension is handed only a value of at most 120 characters made of characters the checks read as the page
    shows them: printable ASCII, curly quotes and dashes, and Latin letters with accents (read as the plain letter),
    with no HTML entity (`answers.not_fillable`, `answers.fill_char`); 590 characters in all, every other one
    copied from the panel. A test walks every character of the Basic Multilingual Plane to hold it to that, and a
    seeded test checks that a filled value reads to the checks, character by character, as the page shows it. (A second read-only check found
    that a first, Latin-1 version of the rule still let HTML entities and unfolded letters such as ø, ð, æ and ß read
    differently from the page; the rule above closes that by construction. The wording list also reads each text as
    written, without decoding entities. An owner-authorized re-check then found one letter left: the long s with a
    dot above, which a compatibility fold read as "s" while the page shows it like an "f"; the rule now uses the
    canonical decomposition only, and the character walk compares each letter with the one it is drawn on. Letters
    that look alike within plain ASCII, such as a capital I and a lowercase l, are read as written: a limit of any
    wording check, left to the owner's preview.)
- **2026-10-31, the public snapshot.** The decision stands; what ships changes nothing in it. The extension and the
  tests that read its source (`extension/`, `tests/test_extension.py`, `tests/test_extension_behaviour.py`,
  `tests/extension_harness.cjs`) are part of the public snapshot: the owner decided to publish them with the code
  (they had been allowlisted since 2026-10-21, but no snapshot had been published since). `tools/build_snapshot.py`
  carries the decision in its allowlist; only the operator manual stays private.
