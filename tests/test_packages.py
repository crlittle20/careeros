"""Application packages and reusable answers (P2, 2026-10-21; ADR 0009). Synthetic data only."""
import json
import tempfile
import unittest
from pathlib import Path

from careeros.core.fingerprint import fingerprint
from careeros.core.policy import PolicyBundle, deep_merge
from careeros.discovery.boards import greenhouse_posting
from careeros.pipeline import ingest, process
from careeros.review import answers, packages
from careeros.review.dismiss import dismiss
from careeros.review.labels import record_label

from tests.test_v26_discovery import CLAIMS, MID, POLICY, gh_item, setup

JOB = "greenhouse:synth:1"
FULL_TIME = CLAIMS + "\n\nThis is a full-time position."
LIBRARY = """
documents:
  resume: documents/resume.pdf
answers:
  - id: first-name
    question: First Name
    answer: Avery
    field: first_name
    approved: 2026-10-01
  - id: work-authorization
    kind: work_authorization
    question: "Are you legally authorized to work in the United States?"
    phrasings: ["Are you authorized to work in the US?"]
    answer: "Yes"
    approved: 2026-10-01
  - id: old-salary
    kind: salary_expectation
    question: What are your salary expectations?
    answer: Open to discussing a fair range.
    approved: 2026-01-01
    expires: 2026-06-30
  - id: gender
    kind: experience
    question: Gender
    answer: should never be used
    approved: 2026-10-01
"""
QUESTIONS = [{"key": "first_name", "name": "first_name", "label": "First Name", "type": "text", "required": True},
             {"key": "resume", "name": "resume", "label": "Resume/CV", "type": "file", "required": True},
             {"key": "question", "name": "question_1", "label": "Are you authorized to work in the US?", "type": "select",
              "required": True, "options": ["Yes", "No"]},
             {"key": "question", "name": "question_2", "label": "What are your salary expectations?", "type": "text"},
             {"key": "question", "name": "question_3", "label": "Gender", "type": "select", "options": ["Prefer not to say"]},
             {"key": "question", "name": "question_4", "label": "Why do you want to work here?", "type": "textarea"}]
FACTS = [{"id": "volume", "text": "Reviewed about 5,000 claims a month.", "applies_to": [], "superseded": [], "source": "t"}]


def policy(**preparation) -> PolicyBundle:
    data = deep_merge(POLICY.data, {"preparation": preparation}) if preparation else POLICY.data
    return PolicyBundle(data=data, fingerprint=fingerprint(data))


class Vault:
    """A temporary vault with an answers file and a resume."""

    def __init__(self, text=LIBRARY):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "documents").mkdir()
        (self.root / "documents" / "resume.pdf").write_bytes(b"%PDF synthetic resume v1")
        self.write(text)

    def write(self, text):
        (self.root / "answers.yaml").write_text(text, encoding="utf-8")
        self.library = answers.load(self.root / "answers.yaml")

    def close(self):
        self._tmp.cleanup()


def posting_store(text=CLAIMS):
    store, ctx = setup()
    ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
           [greenhouse_posting("synth", gh_item(1, "Claims Analyst", text, "synth"))], full_board=False)
    process(ctx)
    return store, ctx


class AnswerLibraryTests(unittest.TestCase):
    def setUp(self):
        self.vault = Vault()
        self.lib = self.vault.library

    def tearDown(self):
        self.vault.close()

    def test_an_answer_is_reused_only_for_its_question_or_a_listed_phrasing(self):
        exact = answers.find("are you legally authorized to work in the united states", self.lib, "2026-10-21")
        self.assertEqual((exact["status"], exact["via"], exact["answer"]["id"]), ("matched", "question", "work-authorization"))
        listed = answers.find("Are you authorized to work in the US?*", self.lib, "2026-10-21")
        self.assertEqual((listed["status"], listed["via"]), ("matched", "phrasing"))
        similar = answers.find("Are you authorized to work in the U.S.?", self.lib, "2026-10-21")   # close, but not yours
        self.assertEqual(similar["status"], "missing")

    def test_an_expired_answer_needs_review_and_sensitive_questions_are_never_answered(self):
        self.assertEqual(answers.find("What are your salary expectations?", self.lib, "2026-10-21")["status"], "expired")
        self.assertEqual(answers.find("What are your salary expectations?", self.lib, "2026-06-30")["status"], "matched")
        for question in ("Gender", "Are you a protected veteran?", "Do you have a disability?", "I certify that the above is true",
                         "What is your race/ethnicity?"):
            self.assertEqual(answers.find(question, self.lib, "2026-10-21")["status"], "sensitive", question)

    def test_the_file_is_refused_when_it_cant_be_read_exactly(self):
        bad = {"no approval date": "answers:\n  - {id: a, kind: experience, question: Q, answer: A}\n",
               "unknown field": "answers:\n  - {id: a, question: Q, answer: A, field: shoe_size, approved: 2026-10-01}\n",
               "two answers, one question": "answers:\n  - {id: a, kind: experience, question: Q, answer: A, approved: 2026-10-01}\n"
                                            "  - {id: b, kind: experience, question: 'q?', answer: B, approved: 2026-10-01}\n",
               "a document outside the vault": "documents: {resume: ../resume.pdf}\nanswers: []\n",
               "phrasings not a list": "answers:\n  - {id: a, kind: experience, question: Q, answer: A, phrasings: Q2, "
                                       "approved: 2026-10-01}\n"}
        for name, text in bad.items():
            with self.assertRaises(ValueError, msg=name):
                self.vault.write(text)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.vault = Vault()
        self.store, self.ctx = posting_store()

    def tearDown(self):
        self.vault.close()

    def prepare(self, pol=POLICY, questions=QUESTIONS, facts=FACTS):
        return packages.prepare(self.store, pol, MID, JOB, facts=facts, library=self.vault.library, vault_root=self.vault.root,
                                questions=questions)

    def ready(self, pol=POLICY, questions=QUESTIONS, facts=FACTS):
        return packages.readiness(self.store, pol, MID, JOB, facts=facts, library=self.vault.library,
                                  vault_root=self.vault.root, questions=questions)

    def test_nothing_is_prepared_for_a_job_nobody_chose_and_skip_or_dismissal_always_win(self):
        with self.assertRaisesRegex(packages.PackageRefused, "not pursued"):
            self.prepare()
        record_label(self.store, JOB, "skip", None, "k-skip")
        with self.assertRaisesRegex(packages.PackageRefused, "Skip"):
            self.prepare()
        record_label(self.store, JOB, "apply", None, "k-apply")
        self.assertTrue(self.prepare()["new"])
        dismiss(self.store, JOB, "other", "changed my mind")
        self.assertEqual(self.ready()["state"], "withdrawn")

    def test_the_same_inputs_give_the_same_version_and_any_change_a_new_one(self):
        record_label(self.store, JOB, "apply", None, "k1")
        first = self.prepare()
        self.assertEqual((first["new"], first["version"]), (True, 1))
        self.assertEqual(self.prepare()["new"], False)                  # nothing changed: no new version
        folder = self.vault.root / first["folder"]
        self.assertEqual({p.name for p in folder.iterdir()}, {"manifest.json", "brief.md", "answers.md"})
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        statuses = {q["name"]: q["status"] for q in manifest["questions"]}
        self.assertEqual(statuses, {"first_name": "matched", "resume": "document", "question_1": "matched",
                                    "question_2": "expired", "question_3": "sensitive", "question_4": "missing"})
        self.assertNotIn("should never be used", (folder / "answers.md").read_text(encoding="utf-8"))
        (self.vault.root / "documents" / "resume.pdf").write_bytes(b"%PDF synthetic resume v2")   # a different resume
        self.assertEqual(self.ready()["state"], "stale")
        self.assertEqual(self.prepare()["version"], 2)

    def test_an_answer_that_isnt_one_of_the_forms_choices_is_an_open_item(self):
        record_label(self.store, JOB, "apply", None, "k1")
        self.vault.write(LIBRARY + "  - id: relocate\n    kind: relocation\n    question: Are you willing to relocate?\n    answer: \"Yes\"\n    approved: 2026-10-01\n")
        questions = [{"key": "question", "name": "question_8", "label": "Are you willing to relocate?", "type": "select",
                      "options": ["Yes, I am willing", "No"]}]
        out = self.prepare(questions=questions)
        self.assertTrue(any("option mismatch" in g for g in out["gaps"]), out["gaps"])
        # an authorization question's choices must be a yes or a no (sixth Claude review): a status list stays yours
        out = self.prepare(questions=[{**QUESTIONS[2], "options": ["Yes, on F-1 OPT", "No"]}])
        self.assertTrue(any("aren't a yes or a no" in g for g in out["gaps"]), out["gaps"])

    def test_a_stretch_package_needs_approval_of_its_exact_version(self):
        record_label(self.store, JOB, "stretch", "worth a try", "k1")
        first = self.prepare()
        self.assertEqual(self.ready()["state"], "needs_approval")
        approval = packages.approve(self.store, first["package_id"], reason="reviewed every answer", batch="week-43", key="a1")
        self.assertEqual(self.ready()["state"], "ready")
        self.vault.write(LIBRARY.replace("answer: Avery", "answer: Avery J."))   # an answer changed after approval
        self.assertEqual(self.ready()["state"], "stale")
        second = self.prepare()
        self.assertEqual(self.ready()["state"], "needs_approval")            # the approval named version 1's hash
        with self.assertRaisesRegex(ValueError, "superseded"):
            packages.approve(self.store, first["package_id"], reason="old one", key="a2")
        packages.approve(self.store, second["package_id"], reason="reviewed again", key="a3")
        self.assertEqual(self.ready()["state"], "ready")
        self.assertTrue(approval)

    def test_a_revoked_approval_no_longer_counts(self):
        record_label(self.store, JOB, "stretch", None, "k1")
        approval = packages.approve(self.store, self.prepare()["package_id"], reason="ok", key="a1")
        packages.revoke(self.store, approval, reason="second thoughts", key="r1")
        self.assertEqual(self.ready()["state"], "needs_approval")

    def test_a_posting_that_closes_or_that_the_gate_now_rejects_withdraws_its_package(self):
        # 2026-10-27: a closed posting and a role the gate now rejects as hybrid still listed as needing approval, though
        # every session would be refused there (posting_not_open, not_eligible)
        def post(location):
            self.store.clock.advance(days=1)
            ingest(self.ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
                   [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth", location=location))],
                   full_board=False)
            process(self.ctx)

        record_label(self.store, JOB, "stretch", "worth a try", "k1")
        packages.approve(self.store, self.prepare()["package_id"], reason="reviewed every answer", key="a1")
        self.assertEqual(self.ready()["state"], "ready")
        post("Boston, MA (Hybrid, 3 days in office)")
        ready = self.ready()
        self.assertEqual(ready["state"], "withdrawn")
        self.assertRegex(ready["reasons"][0], "^not_eligible: the gate's latest decision is rejected .*not_remote")
        with self.assertRaisesRegex(packages.PackageRefused, "not_eligible"):
            self.prepare()
        post("Remote - US")                       # judged eligible again: the route, and that version's approval, return
        self.assertEqual(self.ready()["state"], "ready")
        with self.store.tx():
            self.store.writer("discovery").update("jobs", {"closed_at": self.store.clock.now()}, "job_id = ?", (JOB,))
            self.store.writer("discovery").transition(self.ctx.matrix, JOB, "closed", reason_code="posting_removed")
        ready = self.ready()
        self.assertEqual(ready["state"], "withdrawn")
        self.assertRegex(ready["reasons"][0], r"^posting_not_open: the posting is closed \(closed \d{4}-\d\d-\d\d\)")
        with self.assertRaisesRegex(packages.PackageRefused, "posting_not_open"):
            self.prepare()

    def test_an_apply_package_needs_no_approval_unless_you_ask_for_all(self):
        record_label(self.store, JOB, "apply", None, "k1")
        self.prepare()
        self.assertEqual(self.ready()["state"], "ready")
        self.assertEqual(self.ready(pol=policy(approval_required="all"))["state"], "needs_approval")

    def test_standing_preparation_is_off_by_default_and_never_writes_a_label(self):
        store, _ = posting_store(FULL_TIME)
        self.store = store
        with self.assertRaisesRegex(packages.PackageRefused, "standing preparation is off"):
            self.prepare()
        on = policy(enabled=True)
        out = self.prepare(pol=on)
        self.assertEqual(out["route"], "standing")
        self.assertIsNone(store.one("SELECT 1 FROM labels"))                  # no invented label
        record_label(store, JOB, "skip", None, "k-skip")                       # your Skip wins over the policy
        self.assertEqual(self.ready(pol=on)["state"], "withdrawn")

    def test_the_standing_rule_is_absolute_and_holds_back_decisive_unknowns(self):
        on = policy(enabled=True)
        score = self.store.one("SELECT * FROM scores ORDER BY at DESC LIMIT 1")
        picked, why = packages.standing(on, score)                            # employment type unknown in CLAIMS
        self.assertFalse(picked)
        self.assertIn("decisive unknowns to check first", why)
        store, _ = posting_store(FULL_TIME)
        score = store.one("SELECT * FROM scores ORDER BY at DESC LIMIT 1")
        self.assertEqual(packages.standing(on, score), (True, []))
        elig = json.loads(score["components_json"])["eligibility"]["score"]
        picked, why = packages.standing(policy(enabled=True, min_eligibility=elig + 0.1), score)   # just above: not picked
        self.assertFalse(picked)
        self.assertTrue(any(w.startswith("eligibility") for w in why), why)

    def test_an_answer_resting_on_an_unknown_fact_is_refused(self):
        record_label(self.store, JOB, "apply", None, "k1")
        self.vault.write(LIBRARY.replace("    approved: 2026-10-01\n  - id: work-authorization",
                                         "    facts: [no-such-fact]\n    approved: 2026-10-01\n  - id: work-authorization", 1))
        with self.assertRaisesRegex(packages.PackageRefused, "answer_cites_unknown_fact"):
            self.prepare()

    def test_questions_not_yet_read_leave_every_answer_in_the_panel(self):
        record_label(self.store, JOB, "apply", None, "k1")
        out = self.prepare(questions=None)
        panel = (self.vault.root / out["folder"] / "answers.md").read_text(encoding="utf-8")
        self.assertIn("this posting's questions are not known yet", panel)
        self.assertIn("Are you legally authorized to work in the United States?", panel)

    def test_a_missing_resume_is_an_open_item(self):
        record_label(self.store, JOB, "apply", None, "k1")
        (self.vault.root / "documents" / "resume.pdf").unlink()
        self.assertIn("resume not found in the vault: documents/resume.pdf", self.prepare()["gaps"])


class SecondReviewTests(unittest.TestCase):
    """The second Claude review of the Codex corrections (2026-10-22): deny by default, the panel's version, F7."""

    def setUp(self):
        self.vault = Vault()

    def tearDown(self):
        self.vault.close()

    def test_every_answer_declares_a_kind_from_the_closed_list(self):
        for kind in ("demographics", "consent", "attestation", "signature", "contact", ""):
            text = LIBRARY.replace("kind: work_authorization", f"kind: {kind}" if kind else "")
            with self.assertRaisesRegex(ValueError, "needs a kind", msg=kind):
                self.vault.write(text)
        self.vault.write(LIBRARY.replace("    field: first_name\n", "    field: first_name\n    kind: contact\n"))
        self.assertEqual(self.vault.library["answers"][0]["kind"], "contact")   # a contact detail: its field is enough
        self.assertEqual(set(answers.KINDS) & {"consent", "demographics", "disability", "veteran", "signature",
                                               "background_check", "identity_document", "salary_history"}, set())

    def test_an_id_is_plain_letters_digits_dashes_and_underscores(self):
        for odd in ('"first-name\u200b"', '"\ufb01rst-name"', '"first\u2010name"', "1.0", '"first name"'):
            with self.assertRaisesRegex(ValueError, "answer_invalid", msg=odd):
                self.vault.write(LIBRARY.replace("id: gender", f"id: {odd}"))
        with self.assertRaisesRegex(ValueError, "two answers have the id"):
            self.vault.write(LIBRARY.replace("id: gender", "id: FIRST-NAME"))

    def test_an_answer_that_is_itself_a_consent_is_never_used(self):
        self.vault.write(LIBRARY + "  - id: confirm\n    kind: experience\n    question: Confirmation\n"
                                   "    answer: I agree\n    approved: 2026-10-01\n")
        found = answers.find("Confirmation", self.vault.library, "2026-10-21")
        self.assertEqual(found["status"], "sensitive")
        self.assertIn("its answer reads as consent, agreement or attestation ('i agree')", found["blocked"])
        found = answers.find("Are you authorized to work in the US?", self.vault.library, "2026-10-21",
                             ["Yes, I agree to the privacy notice", "No"])
        self.assertEqual(found["status"], "sensitive")
        self.assertIn("a choice on the form", found["blocked"])

    def test_the_fallback_panel_says_which_answers_it_holds_back_and_why(self):
        store, _ = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        out = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                               vault_root=self.vault.root, questions=None)
        panel = (self.vault.root / out["folder"] / "answers.md").read_text(encoding="utf-8")
        self.assertIn("## Not offered", panel)
        self.assertIn("- Gender: held back: its question reads as demographics", panel)
        self.assertIn("- What are your salary expectations?: expired 2026-06-30", panel)
        self.assertNotIn("should never be used", panel)
        self.assertNotIn("Not listed: expired answers and anything about consent", panel)   # no assurance it can't keep
        self.assertIn("always yours to answer on the form, whatever this panel shows", panel)

    def test_editing_only_the_question_an_answer_was_written_for_makes_a_new_version(self):
        """F4, second review: the panel shows the question beside the answer, so the question is versioned too. Each edit
        keeps the answer usable (verification of the Pass 2 package, 2026-10-22), so only the question's hash tells the
        versions apart."""
        store, _ = posting_store()
        record_label(store, JOB, "stretch", None, "k1")
        first = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                 vault_root=self.vault.root, questions=None)
        packages.approve(store, first["package_id"], reason="reviewed", key="a1")
        self.vault.write(LIBRARY.replace('question: "Are you legally authorized to work in the United States?"',
                                         'question: "Are you eligible to work in the United States?"'))
        ready = packages.readiness(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=None)
        self.assertEqual(ready["state"], "stale")
        again = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                 vault_root=self.vault.root, questions=None)
        self.assertEqual((again["new"], again["version"]), (True, 2))
        offered = [a["id"] for a in json.loads(packages.latest(store, JOB)["manifest_json"])["answers"]]
        self.assertIn("work-authorization", offered)                              # still offered: only its question moved
        self.vault.write(LIBRARY.replace("question: First Name", "question: Legal First Name"))   # questions known too
        record_label(store, JOB, "apply", None, "k2")
        known = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                 vault_root=self.vault.root, questions=QUESTIONS)
        rows = {r["name"]: r for r in json.loads(packages.latest(store, JOB)["manifest_json"])["questions"]}
        self.assertEqual(rows["first_name"]["status"], "matched")                 # still filled: only its question moved
        self.vault.write(LIBRARY)
        self.assertEqual(packages.readiness(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                            vault_root=self.vault.root, questions=QUESTIONS)["state"], "stale")
        self.assertTrue(known["new"])

    def test_a_role_you_applied_to_is_never_reported_ready(self):
        """F7, second review: a label given while the application was open, or after it ended, doesn't make a
        package look ready that every session would refuse."""
        from careeros.review.applications import application_matrix, record_application, update_application
        store, _ = posting_store(FULL_TIME)
        record_label(store, JOB, "apply", None, "k-before")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        store.clock.advance(seconds=60)
        app = record_application(store, application_matrix(), JOB, "direct", policy=POLICY, key="applied")
        store.clock.advance(seconds=60)
        record_label(store, JOB, "apply", "calibration label on the review page", "k-during")
        store.clock.advance(seconds=60)
        update_application(store, application_matrix(), app, "rejected", key="rejected")
        ready = packages.readiness(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=QUESTIONS)
        self.assertEqual(ready["state"], "withdrawn")
        self.assertIn("applied_before", ready["reasons"][0])



class QueueTests(unittest.TestCase):
    """P6 (2026-10-21): the standing batch, the questions left open most often, and the commands end to end."""

    def setUp(self):
        self.vault = Vault()

    def tearDown(self):
        self.vault.close()

    def test_the_standing_batch_picks_only_unlabelled_unapplied_jobs_and_only_when_on(self):
        store, _ = posting_store(FULL_TIME)
        self.assertEqual(packages.standing_candidates(store, POLICY), [])               # off
        on = policy(enabled=True)
        self.assertEqual(packages.standing_candidates(store, on), [JOB])
        record_label(store, JOB, "apply", None, "k1")                                   # yours now: not the policy's
        self.assertEqual(packages.standing_candidates(store, on), [])

    def test_common_gaps_counts_each_open_question_across_packages(self):
        store, _ = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        self.assertEqual(sorted(packages.common_gaps(store)), [("What are your salary expectations?", 1, None),
                                                               ("Why do you want to work here?", 1, None)])

    def test_package_gaps_names_the_entry_that_covers_a_standard_field_or_document(self):
        """Live run, 2026-10-26: with no answers.yaml yet, gaps told you to answer First Name, Email, Resume/CV and the
        like on each form. The form's own key makes them standard fields and documents the file fills once."""
        import argparse
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_package
        self.vault.write("")                                                       # no documents, no answers
        store, ctx = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        questions = [{"key": "first_name", "name": "first_name", "label": "First Name", "type": "text", "required": True},
                     {"key": "preferred_name", "name": "preferred_name", "label": "Preferred First Name", "type": "text"},
                     {"key": "email", "name": "email", "label": "Email", "type": "text", "required": True},
                     {"key": "phone", "name": "phone", "label": "Phone", "type": "text"},
                     {"key": "resume", "name": "resume", "label": "Resume/CV", "type": "file", "required": True},
                     {"key": "cover_letter", "name": "cover_letter", "label": "Cover Letter", "type": "file",
                      "required": True},
                     {"key": "question", "name": "question_5", "label": "Email", "type": "text"},
                     {"key": "question", "name": "question_6", "label": "Will you require visa sponsorship?",
                      "type": "text"},
                     {"key": "question", "name": "question_7", "label": "Why Synthetic Health?", "type": "textarea"}]
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=questions)
        gaps = packages.common_gaps(store)
        self.assertIn(("Email", 1, "email"), gaps)
        self.assertIn(("Email", 1, None), gaps)                    # the form's own question: its label is not its key
        self.assertIn(("Resume/CV", 1, "resume"), gaps)
        paths = {"root": self.vault.root, "approved_facts": self.vault.root / "none.yaml",
                 "answers": self.vault.root / "answers.yaml"}
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_package(ctx, paths, argparse.Namespace(action="gaps", target=None, standing=False))
        lines = out.getvalue().splitlines()
        for line in ("  1  First Name  (standard field: answers.yaml covers it with field: first_name)",
                     "  1  Preferred First Name  (standard field: answers.yaml covers it with field: preferred_name)",
                     "  1  Email  (standard field: answers.yaml covers it with field: email)",
                     "  1  Phone  (standard field: answers.yaml covers it with field: phone)",
                     "  1  Resume/CV  (document: answers.yaml covers it with documents.resume)",
                     "  1  Cover Letter  (document: answers.yaml covers it with documents.cover_letter)",
                     "  1  Email  (no reusable kind: answer it on each form)",
                     "  1  Will you require visa sponsorship?  (kind: sponsorship)",
                     "  1  Why Synthetic Health?  (no reusable kind: answer it on each form)"):
            self.assertIn(line, lines)
        self.assertEqual(len([line for line in lines if line.startswith("  1  ")]), 9, lines)

    def test_package_gaps_reads_a_label_with_stray_spacing_as_one_question(self):
        """Live run, 2026-10-27: a form label ending in a line break put its note on the next line, and the same question
        with and without the stray spacing would have counted as two. Its spacing is evened out first."""
        import argparse
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_package
        store, ctx = setup()
        ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
               [greenhouse_posting("synth", gh_item(1, "Claims Analyst", CLAIMS, "synth")),
                greenhouse_posting("synth", gh_item(2, "Payment Integrity Analyst", FULL_TIME, "synth"))], full_board=False)
        process(ctx)
        for n, label in ((1, "Do any of your relatives work at Synthetic Health?\n"),
                         (2, " Do any of your relatives work at\tSynthetic  Health? ")):
            record_label(store, f"greenhouse:synth:{n}", "apply", None, f"k{n}")
            packages.prepare(store, POLICY, MID, f"greenhouse:synth:{n}", facts=FACTS, library=self.vault.library,
                             vault_root=self.vault.root,
                             questions=[{"key": "question", "name": "question_1", "label": label, "type": "text"}])
        self.assertEqual(packages.common_gaps(store), [("Do any of your relatives work at Synthetic Health?", 2, None)])
        paths = {"root": self.vault.root, "approved_facts": self.vault.root / "none.yaml",
                 "answers": self.vault.root / "answers.yaml"}
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_package(ctx, paths, argparse.Namespace(action="gaps", target=None, standing=False))
        self.assertEqual(out.getvalue().splitlines()[0],
                         "  2  Do any of your relatives work at Synthetic Health?  (no reusable kind: answer it on each form)")

    def test_package_gaps_prints_every_label_when_its_output_is_redirected(self):
        """Live run, 2026-10-26: piped, the command wrote the ANSI code page, and a form label with a non-breaking hyphen
        stopped it part-way ('charmap' codec can't encode). The command line writes UTF-8 instead."""
        import io
        from unittest import mock

        from careeros import cli
        from careeros.paths import vault_paths
        store, ctx = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        label = "Are you a current Synthetic\u2011Health employee?"
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS + [{"key": "question", "name": "question_9", "label": label, "type": "text"}])
        out, err = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(out, encoding="cp1252")              # what a redirected Windows console gets
        stderr = io.TextIOWrapper(err, encoding="cp1252", errors="backslashreplace")
        with mock.patch("careeros.cli.vault_paths", return_value=vault_paths(self.vault.root)), \
                mock.patch("careeros.cli._setup_logging"), mock.patch("careeros.cli._context", return_value=ctx), \
                mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            cli.main(["package", "gaps"])
            stdout.flush()
        self.assertEqual((stdout.encoding, stderr.encoding), ("utf-8", "utf-8"))
        self.assertIn(f"  1  {label}  (no reusable kind: answer it on each form)".encode("utf-8"), out.getvalue())
        self.assertIn(b"The rest stay yours on each form.", out.getvalue())       # the list ran to its end

    def test_the_commands_prepare_show_list_and_report_gaps(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_package
        store, ctx = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        paths = {"root": self.vault.root, "approved_facts": self.vault.root / "none.yaml", "answers": self.vault.root / "answers.yaml"}
        packages.save_questions(self.vault.root, JOB, QUESTIONS, "test", store.clock.now())

        def run(action, target=None, **kw):
            out = io.StringIO()
            with redirect_stdout(out):
                cmd_package(ctx, paths, argparse.Namespace(action=action, target=target, reason=kw.get("reason"),
                                                           batch=None, standing=kw.get("standing", False)))
            return out.getvalue()

        self.assertIn("prepared", run("prepare", JOB))
        self.assertIn(f"{JOB}: ready", run("show", JOB))
        self.assertIn("ready", run("list"))
        self.assertIn("Why do you want to work here?", run("gaps"))
        self.assertIn("(it is off)", run("prepare", standing=True))


class CodexBatchReviewTests(unittest.TestCase):
    """Independent Codex review of ac83e00 (2026-10-22): F2, F3, F4, F7."""

    def setUp(self):
        self.vault = Vault()

    def tearDown(self):
        self.vault.close()

    def test_two_answers_with_one_id_are_refused(self):
        """F2: a second answer with the first's id would be swapped in after matching."""
        for other in ("work-authorization", "gender"):
            with self.assertRaisesRegex(ValueError, "answer_invalid: two answers have the id first-name"):
                self.vault.write(LIBRARY.replace(f"id: {other}", "id: first-name"))

    def test_consent_and_attestation_questions_beyond_the_first_person_are_never_answered(self):
        """F3: not only first-person wording (the full corpus is tests/test_sensitive_questions.py)."""
        for question in ("Do you consent to the processing of your personal data?",
                         "Consent to background check", "By checking this box you agree to the privacy policy",
                         "Please acknowledge that the information provided is accurate",
                         "Do you authorize us to contact your previous employers?",
                         "Electronic signature", "Have you ever been convicted of a felony?",
                         "Will you submit to a drug screen?"):
            self.assertTrue(answers.sensitive(question), question)
        for question in ("Are you legally authorized to work in the United States?", "First Name",
                         "What are your salary expectations?", "Why do you want to work here?"):
            self.assertFalse(answers.sensitive(question), question)

    def test_an_answer_written_for_a_sensitive_question_is_never_used_even_through_a_field(self):
        """F3: the entry's own question counts too, not only the form's wording."""
        self.vault.write(LIBRARY + '  - id: privacy\n    kind: experience\n'
                                   '    question: "Do you consent to the processing of your personal data?"\n'
                                   '    phrasings: ["Personal data"]\n    answer: "Yes"\n    approved: 2026-10-01\n')
        self.assertEqual(answers.find("Personal data", self.vault.library, "2026-10-21")["status"], "sensitive")
        self.vault.write(LIBRARY.replace("question: First Name", "question: Gender identity (first name field)"))
        self.assertEqual(answers.find_field("first_name", self.vault.library, "2026-10-21")["status"], "sensitive")

    def test_the_fallback_panel_holds_only_current_answers_and_they_are_part_of_the_version(self):
        """F4: questions not read yet: no expired or sensitive answer to copy, and an edit makes a new version."""
        store, _ = posting_store()
        record_label(store, JOB, "stretch", None, "k1")
        first = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                                 questions=None)
        panel = (self.vault.root / first["folder"] / "answers.md").read_text(encoding="utf-8")
        self.assertNotIn("should never be used", panel)
        self.assertNotIn("Open to discussing a fair range.", panel)
        self.assertIn("Avery", panel)
        packages.approve(store, first["package_id"], reason="reviewed", key="a1")
        self.vault.write(LIBRARY.replace("answer: Avery", "answer: Ava"))
        ready = packages.readiness(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=None)
        self.assertEqual(ready["state"], "stale")
        self.assertTrue(packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                         vault_root=self.vault.root, questions=None)["new"])

    def test_an_application_already_made_stops_standing_preparation_and_an_unchosen_route(self):
        """F7: an open or ended application vetoes the standing route and a label alike. Second Claude review: a label
        given after it doesn't re-open preparation either (every session there would be refused): applying again is
        your own step, outside assistance."""
        from careeros.review.applications import application_matrix, record_application, update_application
        store, _ = posting_store(FULL_TIME)
        on = policy(enabled=True)
        app = record_application(store, application_matrix(), JOB, "direct", policy=on, key="applied")
        self.assertIsNone(packages.route_for(store, on, JOB)[0])
        self.assertEqual(packages.standing_candidates(store, on), [])
        record_label(store, JOB, "apply", None, "k-apply")
        self.assertIsNone(packages.route_for(store, on, JOB)[0])                  # open application: not again
        store.clock.advance(seconds=60)
        update_application(store, application_matrix(), app, "withdrawn", key="withdraw")
        record_label(store, JOB, "skip", None, "k-skip")
        store.clock.advance(seconds=60)
        record_label(store, JOB, "apply", "applying again after withdrawing", "k-again")
        route, why = packages.route_for(store, on, JOB)
        self.assertIsNone(route)                                                 # not even a label given after it
        self.assertIn("careeros applied --reapply", why)
        store3, _ = posting_store(FULL_TIME)
        record_label(store3, JOB, "apply", None, "k-before")                    # your label from before you applied
        store3.clock.advance(seconds=60)
        app3 = record_application(store3, application_matrix(), JOB, "direct", policy=on, key="applied3")
        update_application(store3, application_matrix(), app3, "withdrawn", key="withdraw3")
        self.assertIsNone(packages.route_for(store3, on, JOB)[0])                 # nor a label from before you applied
        store2, _ = posting_store(FULL_TIME)
        app2 = record_application(store2, application_matrix(), JOB, "direct", policy=on, key="applied2")
        update_application(store2, application_matrix(), app2, "withdrawn", key="withdraw2")
        self.assertIsNone(packages.route_for(store2, on, JOB)[0])                 # standing never re-applies
        self.assertEqual(packages.standing_candidates(store2, on), [])


class ThirdReviewTests(unittest.TestCase):
    """The third Claude review of the Codex corrections (2026-10-22): the kind is checked against the wording, more
    sensitive wordings, quoted YAML values, ids, and what the tools tell you."""

    def setUp(self):
        self.vault = Vault()

    def tearDown(self):
        self.vault.close()

    def entry(self, entry_id, kind, question, answer="Yes", phrasings=None):
        extra = f"    phrasings: {phrasings!r}\n" if phrasings else ""
        return (f"  - id: {entry_id}\n    kind: {kind}\n    question: {question!r}\n{extra}    answer: {answer!r}\n"
                f"    approved: 2026-10-01\n")

    def test_an_answer_is_used_only_where_the_question_reads_as_its_kind(self):
        """Deny by default: a sensitive question that the wording check misses still reads as no reusable kind."""
        self.vault.write(LIBRARY + self.entry("lift", "experience", "Are you able to lift 50 pounds?")
                         + self.entry("usp", "work_authorization", "Are you a U.S. person?"))
        for question in ("Are you able to lift 50 pounds?", "Are you a U.S. person?"):
            self.assertFalse(answers.sensitive(question), question)            # the wording check doesn't hold them
            found = answers.find(question, self.vault.library, "2026-10-21")
            self.assertEqual(found["status"], "sensitive", question)           # their own kinds do
            self.assertIn("doesn't read as", found["blocked"])
        self.assertEqual(answers.kinds_for("Are you legally authorized to work in the United States?"), ["work_authorization"])
        self.assertEqual(answers.kinds_for("Do you have a valid driver's license?"), ["certification"])
        self.assertEqual(answers.kinds_for("Current annual salary"), [])
        # the form's question must read as the kind too, not only the owner's
        self.vault.write(LIBRARY.replace('phrasings: ["Are you authorized to work in the US?"]',
                                         'phrasings: ["Are you authorized to work in the US?", "Citizenship status Q3"]'))
        self.assertEqual(answers.find("Citizenship status Q3", self.vault.library, "2026-10-21")["status"], "sensitive")
        self.vault.write(LIBRARY.replace('phrasings: ["Are you authorized to work in the US?"]',
                                         'phrasings: ["Are you authorized to work in the US?", "Question 3"]'))
        self.assertEqual(answers.find("Question 3", self.vault.library, "2026-10-21")["status"], "kind_mismatch")

    def test_a_phrasing_that_reads_as_sensitive_holds_the_answer_back(self):
        self.vault.write(LIBRARY.replace('phrasings: ["Are you authorized to work in the US?"]',
                                         'phrasings: ["Are you authorized to work in the US?", '
                                         '"Do you consent to a background check?"]'))
        found = answers.find("Are you legally authorized to work in the United States?", self.vault.library, "2026-10-21")
        self.assertEqual(found["status"], "sensitive")
        self.assertIn("a phrasing reads as consent", found["blocked"])

    def test_wordings_the_third_review_found_are_never_answered(self):
        """Its end-to-end case: each with a kind the file allows; none reaches the panel or a question row."""
        cases = [("salary_expectation", "Current annual salary"), ("previous_employment", "Salary at your last position"),
                 ("availability", "Are you fully vaccinated against COVID-19?"),
                 ("experience", "Have you ever been excluded from participation in Medicare?"),
                 ("experience", "Have you ever been cited for a DUI?"), ("certification", "Arbitration Agreement"),
                 ("certification", "Applicant Certification and Agreement"),
                 ("experience", "To the best of my knowledge, the information above is correct"),
                 ("link", "Candidate Privacy"), ("work_location", "Where were you born?"),
                 ("experience", "How do you identify?"), ("experience", "Age?"), ("certification", "Passport:"),
                 ("experience", "Please enter your full name to sign"), ("notice_period", "CCPA Notice"),
                 ("experience", "Have you had any moving violations in the past 3 years?"),
                 ("work_location", "Permanent resident card number"), ("language", "What is your first language?"),
                 ("link", "Do you allow us to share your profile with other hiring teams?")]
        text = LIBRARY + "".join(self.entry(f"e{i}", kind, question) for i, (kind, question) in enumerate(cases))
        self.vault.write(text)
        for i, (_, question) in enumerate(cases):
            self.assertEqual(answers.find(question, self.vault.library, "2026-10-21")["status"], "sensitive", question)
            self.assertFalse(answers.usable(self.vault.library["answers"][4 + i], "2026-10-21"), question)
        consent = answers.find("Candidate Privacy", self.vault.library, "2026-10-21", ["Accept", "Decline"])
        self.assertEqual(consent["status"], "sensitive")

    def test_an_unquoted_value_yaml_would_change_is_refused(self):
        for value in ("Yes", "No", "02134", "2:30", "[English, Spanish]", "true", "3.0"):
            text = ("answers:\n  - id: relocate\n    kind: relocation\n    question: Are you willing to relocate?\n"
                    f"    answer: {value}\n    approved: 2026-10-01\n")
            with self.assertRaisesRegex(ValueError, "put it in quotes", msg=value):
                self.vault.write(text)
        self.vault.write("answers:\n  - id: relocate\n    kind: relocation\n    question: Are you willing to relocate?\n"
                         "    answer: \"Yes\"\n    approved: 2026-10-01\n")
        self.assertEqual(self.vault.library["answers"][0]["answer"], "Yes")

    def test_an_id_must_be_plain_ascii(self):
        with self.assertRaisesRegex(ValueError, "an id may use only"):
            self.vault.write(LIBRARY.replace("id: gender", 'id: "\u212aey"'))   # the Kelvin sign lowercases to k

    def test_an_answer_held_back_after_preparation_says_so(self):
        by_id = {a["id"]: a for a in self.vault.library["answers"]}
        from careeros.core.fingerprint import text_fingerprint
        gender = by_id["gender"]
        with self.assertRaisesRegex(ValueError, "answer_held_back: gender is held back now"):
            packages.prepared_answer(by_id, "gender", text_fingerprint(gender["answer"]), text_fingerprint(gender["question"]))
        first = by_id["first-name"]
        with self.assertRaisesRegex(ValueError, "answer_changed"):                 # the question it was written for moved
            packages.prepared_answer(by_id, "first-name", text_fingerprint(first["answer"]), text_fingerprint("Last Name"))

    def test_package_gaps_says_which_questions_have_a_reusable_kind(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_package
        store, ctx = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        questions = QUESTIONS + [{"key": "question", "name": "question_9", "label": "Will you require visa sponsorship?",
                                  "type": "text"},
                                 {"key": "question", "name": "question_10", "label": "Do you have a record?", "type": "text"}]
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=questions)
        paths = {"root": self.vault.root, "approved_facts": self.vault.root / "none.yaml",
                 "answers": self.vault.root / "answers.yaml"}
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_package(ctx, paths, argparse.Namespace(action="gaps", target=None, standing=False))
        text = out.getvalue()
        self.assertIn("Will you require visa sponsorship?  (kind: sponsorship)", text)
        self.assertIn("Why do you want to work here?  (kind: motivation)", text)
        self.assertIn("gender: held back: its question reads as demographics", text)
        self.assertNotIn("Do you have a record?  (kind", text)                   # held back: never a gap to fill


class VerificationTests(unittest.TestCase):
    """The read-only verification of the Codex Pass 2 package (2026-10-22)."""

    def setUp(self):
        self.vault = Vault()

    def tearDown(self):
        self.vault.close()

    def prepare(self, store, on):
        return packages.prepare(store, on, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                                questions=QUESTIONS)

    def test_an_application_elsewhere_in_the_role_or_a_check_that_cant_finish_stops_preparation(self):
        """F7: the veto is the shared preflight, so an application on another posting of the same role, one that may be
        the same role, or a duplicate check that can't finish withdraws a prepared package and refuses a new one."""
        from unittest import mock

        from careeros.openings import link
        from careeros.review.applications import application_matrix, record_application
        from tests.helpers import add_job
        on = policy(enabled=True)
        for n, (relation, code) in enumerate((("same_opening", "applied"), ("possible_duplicate", "possible_match_applied"))):
            store, _ = posting_store(FULL_TIME)
            self.vault.close()
            self.vault = Vault()                                                  # each store prepares its own v1
            record_label(store, JOB, "apply", None, f"k{n}")
            self.prepare(store, on)
            other = add_job(store, f"greenhouse:synth:9{n}", employer_name="Synthetic Health", parent="Synthetic Health",
                            title="Claims Specialist", title_norm="claims specialist")
            link(store, other, JOB, relation, "synthetic")
            record_application(store, application_matrix(), other, "direct", policy=on, key=f"a{n}")
            route, why = packages.route_for(store, on, JOB)
            self.assertIsNone(route)
            self.assertTrue(why.startswith(code + ":"), why)
            ready = packages.readiness(store, on, MID, JOB, facts=FACTS, library=self.vault.library,
                                       vault_root=self.vault.root, questions=QUESTIONS)
            self.assertEqual(ready["state"], "withdrawn")
            with self.assertRaisesRegex(packages.PackageRefused, code):
                self.prepare(store, on)
        store, _ = posting_store(FULL_TIME)
        record_label(store, JOB, "apply", None, "k-group")
        with mock.patch("careeros.review.applications.role_group_resolved", return_value=([JOB], False)):
            route, why = packages.route_for(store, on, JOB)
            self.assertIsNone(route)
            self.assertTrue(why.startswith("role_group_incomplete:"), why)
            with self.assertRaisesRegex(packages.PackageRefused, "role_group_incomplete"):
                self.prepare(store, on)


if __name__ == "__main__":
    unittest.main()
