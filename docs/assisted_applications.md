# Assisted applications

CareerOS prepares each application from what you approved and can fill the form you opened. **You always submit.**
The design and its limits are in [ADR 0009](adr/0009-assisted-applications.md). This page is the how-to.

## 1. Write your answers once

Create `profile/answers.yaml` in your vault. CareerOS reads it and never writes it.

```yaml
documents:
  resume: documents/resume.pdf          # a file inside the vault
answers:
  - id: first-name
    question: First Name
    answer: "Avery"
    field: first_name                   # standard fields: first_name, last_name, preferred_name, email, phone, ...
    approved: 2026-10-21
  - id: work-authorization
    kind: work_authorization            # what the question is about (the list is below)
    question: "Are you legally authorized to work in the United States?"
    phrasings: ["Are you authorized to work in the US?"]
    answer: "Yes"                       # quote every answer: an unquoted Yes, No, 02134 or 2:30 is refused
    approved: 2026-10-21
    expires: 2027-04-21                 # optional: after this, it needs your review
```

How answers are reused:
- An answer is used only for its exact question, or for a phrasing you listed. Case, spacing, look-alike quotes and
  dashes, fullwidth forms, HTML entities, a leading `*` or `-`, a quote at either end and a trailing ?, ., :, ! or *
  don't matter; other punctuation (a trailing `;` or `,`, quotes inside the question) must match.
- A question that is merely similar is never answered for you.
- Each answer needs its own `id`: 1 to 64 letters, digits, `-` and `_`, starting with a letter or digit. The file is
  refused if two answers share one, whatever their case. Quote an id made of digits: YAML reads `017` as the number
  15.
- Quote each question and answer. YAML would read an unquoted `Yes` as true, `02134` as 1116 and `2:30` as 150, so
  the file is refused rather than guessing. Unquoted text is read the way YAML reads it: a ` #` starts a comment
  and drops the rest without a warning, so quote anything with punctuation.
- An answer can name the approved facts it rests on (`facts: [id]`); a package refuses an id that isn't one of your
  approved facts.

**Kinds.** Each answer names its `kind`, from this closed list (a contact detail names its `field` instead):
`work_authorization`, `sponsorship`, `start_date`, `notice_period`, `availability`, `relocation`, `work_location`,
`travel`, `schedule`, `salary_expectation`, `experience`, `previous_employment`, `motivation`, `certification`,
`education`, `language`, `referral_source`, `link`. The file is refused if an answer names any other kind.

Each kind is a closed set of question shapes, matched against the whole question: a `work_authorization` answer is
used only for "Are you legally authorized to work in the United States?" and its like, a `salary_expectation` answer
only for "What are your salary expectations?" and its like, an `experience` answer only for "How many years of
experience do you have with ...?" and its like (and never where the skill named is about you: drugs, health, military
service, family). Your own question and the form's question must each be one of those shapes; an answer whose
question isn't is held back. This is the main rule, and it is deny by default: a question that isn't one of the
shapes is never answered from the file, whatever its words. Only the kinds whose shapes name something
(`experience`, `previous_employment`, `motivation`, `certification`, `education`: a skill, a company, a certification,
a school) have a free part, and it is a short name, not a clause or a second sentence. Every other kind's shapes are
fixed words, numbers and closed lists (the languages and time zones they know): "Are you willing to relocate for this
role?" is a `relocation` question, "Are you willing to relocate to Nashville?" isn't, so you answer that one on the
form. A work-authorization or sponsorship answer is a plain Yes or No, and fills a drop-down only when its choices are
a plain yes and no: a status is yours to give on the form. `careeros package gaps` lists the questions your packages
leave unanswered, expired or answered with something that isn't one of the form's choices, each with the kind it reads
as, if any; a standard field or document (known by the form's own key) says the `field:` or `documents.` entry that
covers it. A contact detail is written for its own standard field (its question is "First Name", "Email" and the
like) and fills only that field, where the form labels it so (a field with no label is read by its name, so an
unlabelled email or phone field is filled too).

**Questions you always answer on the form.** There is no kind for these, so they don't belong in the file:
- consent, agreement or attestation ("Do you consent to...", "I confirm the information is true", privacy notices,
  terms, arbitration, permission to contact employers or references, keeping your data on file, text messages);
- demographics, citizenship and national origin;
- health, disability and military service (including vaccination and veteran status);
- signatures, typed names and initials;
- background, criminal, credit, driving and drug checks, and exclusion lists;
- identity documents;
- pay history (what you earn or earned, as opposed to what you expect).

As a second line, CareerOS also reads the wording. An answer is never used when its question, a phrasing, its own
text (whatever its length), or the form's question or choices read as one of those categories. It reads text the
way a pasted web label is evened out: HTML entities, no-break spaces, invisible formatting characters, blank letters,
accents, a fixed list of common look-alike letters, curly quotes, fullwidth letters and trailing punctuation don't
hide a word. Other characters can (a line break inside a word, a letter such as ø, an HTML entity), so an answer
that holds one is never filled: you copy it from the panel. Neither rule is complete, because wording
varies: each holds back what it recognises, but a sensitive question that fits a shape and avoids the wording list
can still be answered. Some routine questions are held back too (for example "Do you agree to work weekend hours?",
a professional license number, or experience with member or disability claims). `careeros package gaps` names the
words that held back an answer in your file, so you can reword a false alarm; an answer held back on one form because
of that form's question or choices is shown by `careeros package prepare` and in the package's `answers.md`.

## 2. Prepare a package

| Command | What it does |
|---|---|
| `careeros package questions JOB` | Reads a Greenhouse posting's form questions from its public job-board API |
| `careeros package prepare JOB` | Prepares the package (brief, answers matched to the questions, open items, documents by SHA-256) in `vault\packages` |
| `careeros package show JOB` | Says whether it is ready, stale, waiting for approval or withdrawn |
| `careeros package approve PACKAGE_ID --reason "..."` | Approves a Stretch package (that exact version) |
| `careeros package list` | The queue: every package and its state now |
| `careeros package gaps` | The questions your packages leave unanswered, expired or not one of the form's choices, most often first, each with the kind it reads as (or none: yours on each form), or for a standard field or document the `field:` or `documents.` entry that covers it, and the answers your file holds back and why |
| `careeros package prepare --standing` | Prepares every job the standing preparation policy picks (off unless you turn it on) |

Which packages are prepared, and when they need you:
- **Routes.** A package is prepared for a job you labelled Apply or Stretch, or one the standing policy picks. Skip, a
  dismissal or an application already made always win. A dismissal you undid (`careeros dismiss JOB --undo`) no longer
  counts: the latest event decides (2026-11-01).
- **Closed or no longer eligible postings.** A posting that closed, or that the gate's latest decision no longer finds
  eligible (the posting, the rules or your profile changed), is never prepared, whatever its label or approval: every
  session there would be refused. Its package shows as withdrawn with the reason, and comes back if the posting reopens
  or is judged eligible again.
- **Roles you applied to.** A role you applied to (the application in play or ended), may have applied to, or whose
  duplicate check can't finish is never prepared, whatever its label: every session there would be refused. Applying
  again is your own decision: apply yourself and record it with `careeros applied --reapply "<why>"`.
- **Approval.** A Stretch package needs your approval of that exact version. An Apply package doesn't, unless you set
  `preparation.approval_required: all` in your vault overrides.
- **Changes.** If an answer (or the question it was written for), fact, document, question or the posting changes, the
  package becomes stale. Prepare it again; an earlier approval doesn't cover the new version.
- **Standing preparation.** This is `preparation` in your vault overrides. It picks only by an absolute rule: ELIGIBLE,
  eligibility at least 80, skill coverage at least 0.70, no degree stretch, no medium, large or severe experience gap,
  and nothing to check first. It never writes a label.

## 3. Apply

1. **Open a session.** Run `careeros assist open JOB`.
   - It checks that the package is ready, that submission is authorized (eligibility, the employer's identity, holds)
     and that the role has no application or open session.
   - It then reserves the role. If anything is wrong, it lists every reason at once.
2. **Fill the form in one of three modes:**
   - `--mode manual` (the default): copy each value from the package's `answers.md`.
   - `--mode native`: use the employer's own autofill (for example MyGreenhouse), with `answers.md` as your reference.
   - `--mode assisted`: on a destination you turned filling on for (section 5), CareerOS writes a handoff file in
     `vault\handoff` for the browser extension.
3. **Review the whole form, attach your files, and submit it yourself.**
4. **Close the session** with what happened:
   - `careeros assist close SESSION --outcome submitted`, adding `--evidence confirmation_observed` if you saw the
     employer's confirmation. This records the application. `careeros assist close JOB ...` works too when the role
     has exactly one session open or uncertain.
   - While a session is open or uncertain, `careeros applied` and the review page's "I applied" refuse and print the
     exact `careeros assist close SESSION --outcome submitted` command, so the application keeps the session's evidence
     and measurements. On the review page the confirmation step shows that command instead of Confirm, and recording
     checks again (2026-11-01).
   - A session closed as submitted by mistake leaves an application you can void (`careeros app-status APP void
     --reason "..."`, while no one at the employer has ever answered). The void application stays in the history and no
     longer stops preparation or a new session for the role (2026-11-01).
   - `--outcome not_submitted`.
   - `--outcome uncertain` if the page closed or errored around Submit. That blocks a new session for the role until
     you close it again with what really happened.
   - Add `--minutes`, `--corrections`, `--custom-questions` and `--login-steps` to measure the work.
     `careeros assist report` compares the modes, by destination.

## 4. The browser extension (CareerOS Assist)

**Installing it** (Chrome or Edge):
1. Open `chrome://extensions` and turn on Developer mode.
2. Choose **Load unpacked** and pick the engine's `extension` folder.

The `extension` folder ships in the public snapshot with the rest of the code (`tools/build_snapshot.py`): the owner
decided to publish it on 2026-10-31. Filling stays off for each destination until its installed-extension check.

The manifest is `extension/manifest.json`. It asks for two permissions only:
- `activeTab`: the tab you clicked the icon on;
- `scripting`: to fill that tab once you press the button.

**Using it:**
1. Open the employer's application form.
2. Click the CareerOS Assist icon and choose the handoff file from `vault\handoff`.
3. The popup checks the file:
   - it is a file CareerOS writes (its session, job, review list and attachments);
   - it isn't expired, and doesn't claim more than the two hours CareerOS gives it (plus five minutes for clock
     differences);
   - this tab is the posting it names;
   - every field in it is one the adapter fills.
4. The popup shows every value. Press **Fill these fields**.
5. At that moment it checks everything again: the file's contents as loaded, its expiry, and that this tab, still in
   front, is still that posting. Then the page itself checks the posting and the expiry once more before anything is written, against its
   own copy of the reviewed sites.

**What it fills, and what it leaves to you:**
- It fills text fields and text areas that are empty.
- CareerOS never hands it an experience, certification, previous-employment, education or motivation answer: the
  kinds whose questions name something (a skill, a certificate, a company, a school). A name can always carry
  something personal, so you copy those from the package's `answers.md`, reading each one. The popup lists them under
  "copy from the panel". The same goes for any answer over 120 characters, and any that holds an HTML entity or a
  character other than plain keyboard characters, curly quotes and dashes, and accented Latin letters (a line break,
  an invisible character, a letter such as ø or ß, a symbol such as €): you copy those too, and the popup says which
  character it was.
- It fills plain drop-down lists only where nothing is chosen yet: an empty placeholder such as "Select..." or
  "--". A choice already made, even the first option, is kept. It picks the choice itself, by its position, never a
  disabled one, and never one of two choices that read the same.
- It leaves everything else to you: values you typed, files, custom drop-downs, a field name that more than one
  element on the page answers to, demographic and consent questions, and Submit.
- It reports what it filled, what was already right, what it couldn't find and what it left for you.

**What it trusts.** The handoff is a file in your vault. Nothing signs it, so it is trusted only as far as your
Windows account is. The extension doesn't take from it where it may fill: the hosts, the shape of a posting address
and the field names reviewed for each adapter are fixed in the extension, and a file can't add others. Nor does it
take how long it may be used: a file that claims more than two hours (plus five minutes for clock differences) is
refused. It does take the values and the posting the file names (which must fit that shape), as CareerOS wrote
them.

## 5. Before the first real form: the installed-extension check

Every destination ships with filling off (`unknown` in `config/site_policy.yaml`, `assistance`), so assisted mode
refuses until you turn it on. The tests run the extension in Node on a synthetic page; this check runs it installed,
in your browser, on a real Greenhouse posting, with synthetic values and nothing submitted:

1. Pick a public posting on `job-boards.greenhouse.io` with a drop-down question, and copy its address.
2. From the engine folder, run `.venv\Scripts\python.exe tools\check_handoff.py <address> --select
   question_<id>=<choice>` (in a development checkout, `.venv-dev`), with a field name and choice from that form. It writes `careeros-test-handoff.json` (in your temporary folder; it prints the
   path) with synthetic values only (a first name, a last name, an `example.invalid` email and that choice), expiring
   in 30 minutes.
3. Open the posting, click the CareerOS Assist icon and choose that file. Check the preview, press **Fill these
   fields**, and compare what the extension reports with the form: each value in its own field, nothing else changed.
4. Check that it refuses what it should:
   - reload the posting, pick a different choice in that drop-down yourself, then load the file and press Fill: your
     choice must stay ("already chosen");
   - open another posting and choose the same file: the popup must refuse it (it names a different posting);
   - write a file with `--minutes 1`, load it on the posting at once, leave the popup open until that minute has
     passed, then press Fill: it must refuse (expired).
5. Close the tabs **without submitting**. Delete the test files.
6. If it all held, turn filling on for that host in your vault overrides (`policy/overrides.yaml`), with the date in
   quotes (an older engine can still read the file after a rollback):

   ```yaml
   assistance:
     greenhouse.io: {fill: supervised_only, installed_check: "2026-10-23"}   # the day you ran it, YYYY-MM-DD
   ```

   CareerOS can't see the check itself: the date is your own record that you ran it. This check covers the installed
   extension with a synthetic file; CareerOS's own handoff and session are first used on your first assisted session
   (step 7). A host can't be turned on beyond
   what it was reviewed for (`reviewed_fill`), or without the date. Setting `fill: unknown` or `blocked` turns it off
   again. This switch never changes an eligibility decision.
7. Treat your first assisted session as the last part of the check: compare the preview with the form closely, and
   if anything is off, close the session with `--outcome not_submitted` and turn filling off again.

Where filling may happen once it's on:
- **Greenhouse-hosted forms** (`greenhouse.io`) can be `supervised_only`: you ask, watch and submit.
- **Everything else** is unknown, which means manual.
- **Forms embedded on an employer's own site** are other addresses, so they stay manual.

## 6. Measuring before expanding (plan P3 and P6)

Do your next 5–10 applications in `manual` or `native` mode and record the minutes and corrections when you close
each session. Then try `assisted` on Greenhouse forms. `careeros assist report` groups the sessions by mode and by the
destination host their package was prepared for (2026-11-01). Its medians are over submitted sessions only; sessions
not submitted (abandoned) or uncertain are counted apart. Beside the minutes you recorded it shows the session span,
from opening the session to its first close. It marks fewer than five measured sessions as too small to compare.
Another destination's adapter is worth building only when the report shows time saved there.

## Limits

- Letters that look alike in some fonts (a capital I and a lowercase l, a zero and a capital O) are read as written,
  so a sensitive word spelled with the wrong one is not recognised. Read every value in the popup before you press
  Fill.
- Every answer's own text is read in full, so some of your own prose is held back for an ordinary phrase. In a
  synthetic sample of 18 routine long answers, 8 were, for words such as "I agree", "consent forms", "to confirm",
  "on file", "pay" or "reasonable accommodations". `careeros package gaps` names the words: reword the answer, or
  answer that question on the form yourself.
- The extension's behaviour is run for real, on a synthetic page with fake Chrome APIs, by
  `tests/test_extension_behaviour.py`. That test needs a Node.js runtime (`CAREEROS_NODE`, or `node` on the path), and
  is skipped without one; `tools/validate.py` then says the behaviour tests were NOT RUN, and a `--full` run fails.
- Its permissions, its source (allow-lists of every name, member, string, regular expression and subscript it uses,
  found by a small JavaScript lexer) and its handoff format are checked
  everywhere (`tests/test_extension.py`).
- Installed use is checked by you, once, before filling is turned on (section 5). Check the first real use closely too.
- Greenhouse is the only adapter. Its field names were checked read-only on one public posting. A field the page
  doesn't have is reported as not found and left alone.
- Neither the kinds nor the wording check is complete: each holds back what it recognises. Codex's review and five
  rounds of adversarial review by fresh Claude instances each found questions that slipped past the version before;
  the known ones are regression tests, but a new wording can still slip by. That is why only the named kinds have free
  parts and are never filled for you, why every value is shown before you press Fill, and why you read the whole form
  before you submit.
- The rules cost some routine questions in your own field. On synthetic samples kept in the tests: of 58 routine
  claims-analyst application questions, 52 fit a kind; of 48 routine claims-operations questions, 32 fit no kind (16
  only because a word such as member, disability or maternity is about the person elsewhere) and 3 read as
  sensitive. Those are yours to answer on the form.
- A handoff file stays usable until it expires (two hours at most), even after you close its session. Delete it from
  `vault\handoff` if you want it gone sooner.
- Nothing here submits an application, creates an account, signs in, solves a CAPTCHA or uploads a file.
