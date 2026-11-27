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

from tests.helpers import give_label
from tests.test_v26_discovery import CLAIMS, MID, POLICY, gh_item, setup

OFF = "not pursued: label it Apply or Stretch (standing preparation is off)"   # the owner's D183

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
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k-apply")
        self.assertTrue(self.prepare()["new"])
        dismiss(self.store, JOB, "other", "changed my mind")
        self.assertEqual(self.ready()["state"], "withdrawn")

    def test_the_same_inputs_give_the_same_version_and_any_change_a_new_one(self):
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
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
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
        self.vault.write(LIBRARY + "  - id: relocate\n    kind: relocation\n    question: Are you willing to relocate?\n    answer: \"Yes\"\n    approved: 2026-10-01\n")
        questions = [{"key": "question", "name": "question_8", "label": "Are you willing to relocate?", "type": "select",
                      "options": ["Yes, I am willing", "No"]}]
        out = self.prepare(questions=questions)
        self.assertTrue(any("option mismatch" in g for g in out["gaps"]), out["gaps"])
        # an authorization question's choices must be a yes or a no (sixth Claude review): a status list stays yours
        out = self.prepare(questions=[{**QUESTIONS[2], "options": ["Yes, on F-1 OPT", "No"]}])
        self.assertTrue(any("aren't a yes or a no" in g for g in out["gaps"]), out["gaps"])

    def test_a_stretch_package_needs_approval_of_its_exact_version(self):
        give_label(self.store, POLICY, MID, JOB, "stretch", "worth a try", "k1")
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
        give_label(self.store, POLICY, MID, JOB, "stretch", None, "k1")
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

        give_label(self.store, POLICY, MID, JOB, "stretch", "worth a try", "k1")
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
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
        self.prepare()
        self.assertEqual(self.ready()["state"], "ready")
        self.assertEqual(self.ready(pol=policy(approval_required="all"))["state"], "needs_approval")

    def test_standing_preparation_is_off_by_default_and_never_writes_a_label(self):
        """Off by default, and since the owner's D183 (after Codex CR-2026-11-15-GATE-FIXES Passes 12 and 13, each of
        which found new routes into it) always: no package is prepared without your Apply or Stretch label, even with
        preparation.enabled on, and nothing writes a label."""
        store, _ = posting_store(FULL_TIME)
        self.store = store
        for pol in (POLICY, policy(enabled=True)):
            with self.assertRaisesRegex(packages.PackageRefused, "standing preparation is off"):
                self.prepare(pol=pol)
            self.assertEqual(packages.standing_candidates(store, pol), [])
        self.assertIsNone(store.one("SELECT 1 FROM labels"))                  # no invented label
        give_label(store, POLICY, MID, JOB, "apply", None, "k-apply")
        self.assertEqual(self.prepare(pol=policy(enabled=True))["route"], "apply")

    def test_an_answer_resting_on_an_unknown_fact_is_refused(self):
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
        self.vault.write(LIBRARY.replace("    approved: 2026-10-01\n  - id: work-authorization",
                                         "    facts: [no-such-fact]\n    approved: 2026-10-01\n  - id: work-authorization", 1))
        with self.assertRaisesRegex(packages.PackageRefused, "answer_cites_unknown_fact"):
            self.prepare()

    def test_questions_not_yet_read_leave_every_answer_in_the_panel(self):
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
        out = self.prepare(questions=None)
        panel = (self.vault.root / out["folder"] / "answers.md").read_text(encoding="utf-8")
        self.assertIn("this posting's questions are not known yet", panel)
        self.assertIn("Are you legally authorized to work in the United States?", panel)

    def test_a_full_name_answer_is_on_the_panel_of_a_form_not_yet_read(self):
        """Tremendous (Ashby, 2026-11-04): the form asked for the whole name and the panel had only its two halves."""
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
        self.vault.write(LIBRARY + "  - id: full-name\n    question: Full Name\n    answer: Avery Synth\n"
                                   "    field: full_name\n    approved: 2026-10-01\n")
        out = self.prepare(questions=None)
        panel = (self.vault.root / out["folder"] / "answers.md").read_text(encoding="utf-8")
        self.assertIn("- Full Name\n  Avery Synth", panel)
        self.assertIn("- First Name\n  Avery", panel)

    def test_a_missing_resume_is_an_open_item(self):
        give_label(self.store, POLICY, MID, JOB, "apply", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "stretch", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k2")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k-before")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        store.clock.advance(seconds=60)
        app = record_application(store, application_matrix(), JOB, "direct", policy=POLICY, key="applied")
        store.clock.advance(seconds=60)
        give_label(store, POLICY, MID, JOB, "apply", "calibration label on the review page", "k-during")
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

    def test_the_standing_rule_is_absolute_and_holds_back_decisive_unknowns(self):
        """The standing rule is absolute, and since D183 it picks nothing: an eligible job and one with decisive unknowns
        to check first alike wait for your label, whatever the policy says."""
        store, _ = posting_store(FULL_TIME)
        unknowns, _ = posting_store()                                                   # CLAIMS: employment type unknown
        for pol in (POLICY, policy(enabled=True)):
            self.assertEqual(packages.standing_candidates(store, pol), [])           # off (D183)
            self.assertEqual(packages.route_for(unknowns, pol, JOB), (None, OFF))
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
        self.assertEqual(packages.route_for(store, policy(enabled=True), JOB, MID)[0], "apply")

    def test_the_location_field_alone_places_a_posting_and_a_score_under_earlier_rules_is_made_again(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 5: F1 (a state list or a US sentence in the text never clears the location
        question) and H1 (a score made under earlier rules is made again by the next processing run). Standing
        preparation, which once acted on both, is off (D183)."""
        from unittest import mock

        from careeros.qualify import gate as gate_mod
        on = policy(enabled=True)

        def store_with(location, text=FULL_TIME):
            store, ctx = setup()
            ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
                   [greenhouse_posting("synth", gh_item(1, "Claims Analyst", text, "synth", location=location))], full_board=False)
            process(ctx)
            return store, ctx
        store, _ = store_with("Remote - US")
        self.assertEqual(store.one("SELECT country FROM jobs WHERE job_id = ?", (JOB,))["country"], "US")   # the field places it
        self.assertEqual(packages.standing_candidates(store, on), [])
        # the location field says only "Remote": the text's US sentence is a hint, the question stays
        store, _ = store_with("Remote")
        self.assertEqual(store.one("SELECT country FROM jobs WHERE job_id = ?", (JOB,))["country"], "unknown")
        self.assertEqual(packages.route_for(store, on, JOB), (None, OFF))
        # a state list in the text (the home state among them) beside a restriction elsewhere: still a question
        restricted = FULL_TIME + "\nOur US division hires in the following states: Florida, Georgia, Texas, Arizona.\n" \
                                 "The work will be performed only in Calgary, Canada."
        store, _ = store_with("Remote", restricted)
        self.assertEqual(store.one("SELECT country FROM jobs WHERE job_id = ?", (JOB,))["country"], "unknown")
        # a score made under earlier rules (an upgrade, before the first processing run): made again by the next run
        with mock.patch.object(gate_mod, "RULES_VERSION", "gate-older"):
            store, ctx = store_with("Remote - US")
        before = store.one("SELECT COUNT(*) FROM scores")[0]
        process(ctx)
        self.assertGreater(store.one("SELECT COUNT(*) FROM scores")[0], before)

    def test_a_paste_saved_by_an_older_parser_routes_no_standing_package_until_you_confirm_its_location(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 6, H2: after the rule-version bump, one re-screen gave a paste the older
        parser had placed by its prose ("Previously advertised as Remote - US") a current score, and standing preparation,
        package prepare and assist open acted on it. Since D180 a pasted job is never picked by standing preparation."""
        from careeros.discovery.manual_intake import set_location
        from careeros.normalize.posting import RawPosting
        from careeros.openings import record_observation
        on = policy(enabled=True)
        url = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-4458"
        text = "Claims Analyst\nCompany: Acme Rx\n\n" + FULL_TIME.replace("This role is remote in the US. ", "") + \
            "\nPreviously advertised as Remote - US. The work will be performed only in Calgary."
        store, ctx = setup()
        job_id = "manual:acme-rx:R-4458"
        ingest(ctx, {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"},    # as the parser before 2026-11-16 saved it
               [RawPosting(ats="manual", board_token="acme-rx", external_id="R-4458", title="Claims Analyst", location="Remote - US",
                           url=url, apply_url=url, description_text=text, requisition_id="R-4458",
                           raw={"source": "human_text", "url": url})], source="manual_url", full_board=False)
        store.conn.execute("UPDATE jobs SET source_json = NULL")   # a row stored before 017: no source record
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job_id, url=url,
                           title="Claims Analyst", company="Acme Rx", location="Remote - US")
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "unknown")     # the prose location places nothing
        self.assertEqual(packages.standing_candidates(store, on), [])
        self.assertIsNone(packages.route_for(store, on, job_id)[0])
        set_location(ctx, job_id, "Remote - US")          # you set it by naming the job (D180): placed at the next run
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "US")
        self.assertEqual(packages.standing_candidates(store, on), [])                     # still yours to label (D180)
        self.assertEqual(packages.route_for(store, on, job_id), (None, OFF))
        # a row stored before 017 offers no preview, so no Apply label, until the upgrade pass gives it its source
        # record (the D190 design, section 7)
        from careeros.review.labels import capture
        self.assertIn("waiting for the upgrade", capture(store, POLICY, MID, job_id)["waiting"])

    # --- Codex CR-2026-11-15-GATE-FIXES Pass 7 (H3-H6) ------------------------------------------------------------------
    PROSE = "Previously advertised as Remote - US. The work will be performed only in Calgary."
    BODY = FULL_TIME.replace("This role is remote in the US. ", "")

    def paste(self, ctx, tmp, text, url, location=None):
        """`careeros paste --file` as you would run it: the handler reads the file, processes and reports."""
        import contextlib
        import io
        from types import SimpleNamespace

        from careeros import cli
        from careeros.paths import vault_paths
        path = Path(tmp) / "posting.txt"
        path.write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_paste(ctx, vault_paths(Path(tmp)), SimpleNamespace(file=str(path), clipboard=False, url=url, company=None,
                                                                       title=None, location=location))
        return out.getvalue()

    def held_everywhere(self, store, on, job_id, why=None):
        """Nothing acts on the job: standing selection, the route, package prepare and assist open."""
        from careeros.review import assist
        self.assertEqual(packages.standing_candidates(store, on), [])
        route, basis = packages.route_for(store, on, job_id)
        self.assertIsNone(route, basis)   # standing preparation is off (D183): no label, no route, whatever else holds it
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, on, MID, job_id, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                             questions=QUESTIONS)
        with self.assertRaises(assist.SessionRefused):
            assist.open_session(store, on, MID, job_id, "manual", facts=FACTS, library=self.vault.library,
                                vault_root=self.vault.root, questions=QUESTIONS, key=f"o-{job_id}")
        return basis

    def test_a_line_ending_control_never_joins_a_blank_location_label_to_the_prose(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 7, H3: "Location:" followed by a vertical tab, form feed or next-line
        control, then the prose: the sanitizer deleted the control, the label took the prose, and standing preparation,
        package prepare and assist open acted on it (also through `paste --file`)."""
        on = policy(enabled=True)
        for n, sep in enumerate(("\x0b", "\x0c", "\x85", "\x1c", "\x1d", "\x1e")):
            with tempfile.TemporaryDirectory() as tmp:
                store, ctx = setup()
                url = f"https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-47{n:02d}"
                self.paste(ctx, tmp, "Claims Analyst\nCompany: Acme Rx\nLocation:" + sep + self.PROSE + "\n\n" + self.BODY, url)
                row = store.one("SELECT job_id, location_raw, country FROM jobs")
                self.assertEqual((row["location_raw"], row["country"]), ("", "unknown"), repr(sep))
                self.held_everywhere(store, on, row["job_id"])
        with tempfile.TemporaryDirectory() as tmp:   # the control: a value on the label's own line places it
            store, ctx = setup()
            self.paste(ctx, tmp, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\x0c" + self.PROSE + "\n\n" + self.BODY,
                       "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-4799")
            self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs")), ("Remote - US", "US"))

    def test_a_location_confirmation_changes_only_the_same_posting(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 7, H4: a paste of a different opening (same employer and title, another
        link) with --location "Remote - US" was written onto a rejected Calgary posting, which was then prepared. Since
        D180 a paste never changes a tracked job's location; you name the job (careeros location JOB "...")."""
        from careeros.discovery.manual_intake import intake, set_location
        on = policy(enabled=True)
        store, ctx = setup()
        calgary = "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY + "\nThe work will be performed only in Calgary."
        a_url = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Calgary/Claims_R-10001"
        a = intake(ctx, calgary, a_url, location="Calgary, Alberta, Canada")
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "non_US")
        b = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY, "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-10002",
                   location="Remote - US")
        self.assertNotEqual(b["job_id"], a["job_id"])                       # another link: another posting (D180)
        process(ctx)
        row = store.one("SELECT location_raw, country FROM jobs WHERE job_id = ?", (a["job_id"],))
        self.assertEqual((row["location_raw"], row["country"]), ("Calgary, Alberta, Canada", "non_US"))
        self.held_everywhere(store, on, a["job_id"])
        # a paste of A itself, by its own link, records the sighting and changes nothing
        again = intake(ctx, calgary, a_url + "?src=email", location="Remote - US")
        self.assertEqual((again["job_id"], again["location_confirmed"]), (a["job_id"], False))
        self.assertIn("careeros location", again["location_note"])
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Calgary, Alberta, Canada")
        # the control: naming the job sets it
        self.assertTrue(set_location(ctx, a["job_id"], "Remote - US")["changed"])
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Remote - US")
        # a posting from a job board keeps its board's location
        store, ctx = setup()
        ingest(ctx, {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"},
               [greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME, "synth", location="Remote - US"))],
               full_board=False)
        with self.assertRaisesRegex(ValueError, "job board"):
            set_location(ctx, "greenhouse:synth:1", "Remote - Canada")
        board = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + self.BODY, store.one("SELECT url FROM jobs")["url"],
                       location="Remote - Canada")
        self.assertEqual((board["status"], board["location_confirmed"]), ("already_tracked", False))
        self.assertEqual(store.one("SELECT location_raw FROM jobs")["location_raw"], "Remote - US")

    def test_a_portal_link_that_differs_after_the_hash_or_a_different_requisition_is_another_posting(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 8, H7: two openings of one portal ("/portal/#/opening/R-1101" and
        "...R-1102") reduce to the same canonical link, so B's --location was written onto rejected Calgary opening A,
        which was then prepared and opened."""
        from careeros.discovery.manual_intake import intake
        on = policy(enabled=True)
        store, ctx = setup()
        portal = "https://jobs.example.com/portal/#/opening/"
        calgary = "Claims Analyst\nCompany: Acme Rx\nJob Number: R-1101\n\n" + self.BODY + "\nThe work will be performed only in Calgary."
        a = intake(ctx, calgary, portal + "R-1101", location="Calgary, Alberta, Canada")
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "non_US")
        b = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nJob Number: R-1102\n\n" + self.BODY, portal + "R-1102",
                   location="Remote - US")
        # R-1102 is its own posting (the red team before Pass 11, D179): added as its own job, never written onto R-1101
        self.assertEqual(b["status"], "added")
        self.assertNotEqual(b["job_id"], a["job_id"])
        process(ctx)
        row = store.one("SELECT location_raw, country, url, requisition_id FROM jobs WHERE job_id = ?", (a["job_id"],))
        self.assertEqual(tuple(row), ("Calgary, Alberta, Canada", "non_US", portal + "R-1101", "R-1101"))
        self.assertIsNone(packages.route_for(store, on, a["job_id"])[0])
        self.assertNotIn(a["job_id"], packages.standing_candidates(store, on))
        # the same link with a different requisition in its text is another posting too: never confirmed onto R-1101
        other = intake(ctx, calgary.replace("R-1101", "R-1199"), portal + "R-1101", location="Remote - US")
        self.assertNotEqual(other["job_id"], a["job_id"])
        self.assertFalse(other.get("location_confirmed", False))
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Calgary, Alberta, Canada")
        # and a link that differs only after "#" is another posting even when neither paste states a requisition
        store, ctx = setup()
        plain = calgary.replace("Job Number: R-1101\n", "")
        first = intake(ctx, plain, portal + "R-1101", location="Calgary, Alberta, Canada")
        second = intake(ctx, plain, portal + "R-1102", location="Remote - US")
        self.assertNotEqual(second["job_id"], first["job_id"])                 # another route: another posting (D180)
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (first["job_id"],))[0], "Calgary, Alberta, Canada")
        # since the owner's D188 a portal's route names no posting: even a tracking-only variant of the opening's link is
        # its own job, linked for review, and the first keeps its location; only the same submission again is that job
        for variant in ("https://jobs.example.com/portal/?utm_source=email#/opening/R-1101",
                        portal + "R-1101?utm_campaign=x", portal + "R-1101/"):
            self.assertNotEqual(intake(ctx, plain, variant)["job_id"], first["job_id"], variant)
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (first["job_id"],))[0], "Calgary, Alberta, Canada")
        self.assertEqual(intake(ctx, plain, portal + "R-1101", location="Calgary, Alberta, Canada")["job_id"], first["job_id"])

    def test_a_new_paste_never_takes_over_another_posting_at_the_same_generated_id(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 8, H8: "A&B Health" and "A B Health" make the same id token; with the same
        requisition, the second paste reported "added" while it overwrote the first employer's job, which then got a
        ready standing package and an assistance session."""
        from careeros.discovery.manual_intake import intake
        from careeros.normalize.posting import RawPosting
        on = policy(enabled=True)
        store, ctx = setup()
        calgary = "Claims Analyst\nCompany: A&B Health\nJob Number: R-4001\n\n" + self.BODY + "\nThe work will be performed only in Calgary."
        a = intake(ctx, calgary, "https://careers.ab-health.example/jobs/4001", location="Calgary, Alberta, Canada")
        process(ctx)
        first = dict(store.one("SELECT * FROM jobs WHERE job_id = ?", (a["job_id"],)))
        self.assertEqual(a["job_id"], "manual:a-b-health:R-4001")
        b = intake(ctx, "Claims Analyst\nCompany: A B Health\nJob Number: R-4001\n\n" + self.BODY,
                   "https://jobs.a-b-health.example/postings/4001", location="Remote - US")
        self.assertEqual((b["status"], b["id_collision"]), ("added", a["job_id"]))
        self.assertNotEqual(b["job_id"], a["job_id"])
        self.assertTrue(b["job_id"].startswith("manual:a-b-health:R-4001~"))
        process(ctx)
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM jobs")["n"], 2)
        kept = dict(store.one("SELECT * FROM jobs WHERE job_id = ?", (a["job_id"],)))
        for column in ("employer_name", "url", "description_text", "location_raw", "country"):
            self.assertEqual(kept[column], first[column], column)               # the first job is never touched
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ? AND relation = 'possible_duplicate'",
                                  (b["job_id"], a["job_id"])))
        self.assertIsNone(packages.route_for(store, on, a["job_id"])[0])          # the Calgary job: still no route
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, on, MID, a["job_id"], facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                             questions=QUESTIONS)
        self.assertEqual(store.one("SELECT country FROM jobs WHERE job_id = ?", (b["job_id"],))[0], "US")   # tracked on its own
        # since the owner's D188 a careers page names no posting: the same posting pasted again under another form of
        # that link is its own job, linked for your review, and the first is never touched
        again = intake(ctx, calgary, "https://careers.ab-health.example/jobs/4001?utm_source=x")
        self.assertEqual(again["status"], "added")
        self.assertNotEqual(again["job_id"], a["job_id"])
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Calgary, Alberta, Canada")
        # with no link, the same text under another employer's name is another posting (Codex Pass 10, H13; D178): its
        # own job, linked to the first, which keeps its employer
        text = "Claims Analyst\nJob Number: R-4002\n\n" + self.BODY
        mine = intake(ctx, text, None, company="A&B Health")
        other = intake(ctx, text, None, company="A B Health")
        self.assertEqual((other["status"], other["id_collision"]), ("added", mine["job_id"]))
        self.assertNotEqual(other["job_id"], mine["job_id"])
        self.assertEqual(store.one("SELECT employer_name FROM jobs WHERE job_id = ?", (mine["job_id"],))["employer_name"], "A&B Health")
        # every other manual path too (a saved file, add-url): another employer's posting at the same id gets its own
        ingest(ctx, {"employer_id": None, "name": "A B Health", "parent": "A B Health"},
               [RawPosting(ats="manual", board_token="a-b-health", external_id="R-4001", title="Claims Analyst",
                           location="Remote - US", url="https://jobs.a-b-health.example/saved/4001",
                           apply_url="https://jobs.a-b-health.example/saved/4001", description_text=self.BODY + "\nSaved.",
                           requisition_id="R-4001", raw={"source_file": "saved.txt", "location_source": "file"})],
               source="manual_url", full_board=False)
        # the two openings, the re-paste under another link (D188), the two R-4002 pastes and the saved file
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM jobs")["n"], 6)
        self.assertEqual(dict(store.one("SELECT employer_name, url FROM jobs WHERE job_id = ?", (a["job_id"],))),
                         {"employer_name": first["employer_name"], "url": first["url"]})

    def saved_page(self, identifier, url, employer="Acme Rx", body=None):
        """A saved posting page (JSON-LD), as the inbox import reads it."""
        import json as _json
        page = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Claims Analyst",
                "hiringOrganization": {"@type": "Organization", "name": employer}, "identifier": identifier, "url": url,
                "description": "<p>" + (body or self.BODY).replace("\n", "</p><p>") + "</p>"}
        return '<html><head><script type="application/ld+json">' + _json.dumps(page) + "</script></head><body></body></html>"

    def test_two_links_that_are_not_full_addresses_never_confirm_a_location(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 9, H9: "jobs.example.com/apply/calgary-claims" and ".../us-claims" both
        failed to normalize, and None == None confirmed the Calgary job's location from the US paste. Since D180 no
        paste changes a tracked job's location."""
        from careeros.discovery.manual_intake import intake
        on = policy(enabled=True)
        store, ctx = setup()
        calgary = "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY + "\nThe work will be performed only in Calgary."
        a = intake(ctx, calgary, "jobs.example.com/apply/calgary-claims", location="Calgary, Alberta, Canada")
        process(ctx)
        b = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY, "jobs.example.com/apply/us-claims",
                   location="Remote - US")
        self.assertFalse(b.get("location_confirmed"))
        self.assertNotEqual(b["job_id"], a["job_id"])   # two typed links are two postings (the red team before Pass 15, RTC-02)
        process(ctx)
        row = store.one("SELECT location_raw, country, url FROM jobs WHERE job_id = ?", (a["job_id"],))
        self.assertEqual(tuple(row), ("Calgary, Alberta, Canada", "non_US", "jobs.example.com/apply/calgary-claims"))
        self.held_everywhere(store, on, a["job_id"])
        # the same text and typed link stating another place: its own job (D181, RTC-01); the Calgary job keeps its place
        self.assertFalse(intake(ctx, calgary, "jobs.example.com/apply/calgary-claims", location="Remote - US").get("location_confirmed"))
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Calgary, Alberta, Canada")

    def test_a_file_based_opening_never_overwrites_another_at_the_same_generated_id(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 9, H10: `add-url --text-file` and saved-text import dropped the "#" part
        before the stored link and the generated id, and the id guard accepted an occupied id for the same employer: the
        US opening R-1102 overwrote the Calgary opening R-1101 (text, location, link), which was then prepared."""
        import contextlib
        import io
        from types import SimpleNamespace

        from careeros import cli
        from careeros.discovery.saved import parse_saved_html
        from careeros.paths import vault_paths
        on = policy(enabled=True)
        portal = "https://jobs.example.com/portal/#/opening/"
        calgary = self.BODY + "\nThe work will be performed only in Calgary."
        for route in ("add-url", "import"):
            with tempfile.TemporaryDirectory() as tmp:
                store, ctx = setup()
                paths = vault_paths(Path(tmp))
                for opening, location, body in (("R-1101", "Calgary, Alberta, Canada", calgary), ("R-1102", "Remote - US", self.BODY)):
                    with contextlib.redirect_stdout(io.StringIO()):
                        if route == "add-url":
                            text = Path(tmp) / "posting.txt"
                            text.write_text(body, encoding="utf-8")
                            cli.cmd_add_url(ctx, paths, SimpleNamespace(url=portal + opening, text_file=str(text), employer="Acme Rx",
                                                                        parent=None, title="Claims Analyst", location=location))
                        else:
                            paths["inbox"].mkdir(parents=True, exist_ok=True)
                            (paths["inbox"] / f"{opening}.txt").write_text(
                                f"URL: {portal + opening}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: {location}\n\n{body}",
                                encoding="utf-8")
                            cli.cmd_import(ctx, paths, SimpleNamespace())
                process(ctx)
                stored = portal.replace("/#", "#")                              # the opening's own link, normalized
                rows = {r["url"]: dict(r) for r in store.all("SELECT * FROM jobs")}
                self.assertEqual(set(rows), {stored + "R-1101", stored + "R-1102"}, route)   # each opening keeps its link
                first = rows[stored + "R-1101"]
                self.assertEqual((first["location_raw"], first["country"]), ("Calgary, Alberta, Canada", "non_US"), route)
                self.assertIn("only in Calgary", first["description_text"])
                self.assertIsNone(packages.route_for(store, on, first["job_id"])[0], route)
                self.assertNotIn(first["job_id"], packages.standing_candidates(store, on))
                # Codex Pass 10, H12: R-1102 is its own active job, not a duplicate of R-1101 (linked for your review)
                second = rows[stored + "R-1102"]
                self.assertNotEqual(second["current_state"], "duplicate", route)
                self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate' AND "
                                          "((job_id = ? AND other_id = ?) OR (job_id = ? AND other_id = ?))",
                                          (second["job_id"], first["job_id"], first["job_id"], second["job_id"])), route)
        # a held id with the same employer is written only when it is the same posting: two saved pages sharing an
        # identifier (R-2001) but naming different openings; the second gets its own id, linked to the first
        store, ctx = setup()
        for url, body in ((portal + "A", calgary), (portal + "B", self.BODY)):
            raw, employer = parse_saved_html(self.saved_page("R-2001", url, body=body), "page.html")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        stored = portal.replace("/#", "#")
        rows = {r["url"]: r["job_id"] for r in store.all("SELECT job_id, url FROM jobs")}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[stored + "A"], "manual:acme-rx:R-2001")
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ? AND relation = 'possible_duplicate'",
                                  (rows[stored + "B"], rows[stored + "A"])))
        self.assertIn("only in Calgary", store.one("SELECT description_text FROM jobs WHERE job_id = 'manual:acme-rx:R-2001'")[0])
        # Codex Pass 10, H12: the second opening is not made a duplicate of the first (only linked)
        self.assertNotEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (rows[stored + "B"],))[0], "duplicate")
        # since the owner's D188 a portal's "#" route names no posting: an edited page saved again there is its own job,
        # linked for your review, and each job keeps its text. The same page saved again is that job only as its
        # operation's retry (careeros import, by the file's bytes: the D190 design, section 6, tests/test_p1.py);
        # read again with no receipt, at a route that names no posting, it is its own job, linked for review
        raw, employer = parse_saved_html(self.saved_page("R-2001", portal + "B", body=self.BODY + "\nEdited."), "page.html")
        counts = ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url",
                        full_board=False)
        self.assertEqual((counts["new"], store.one("SELECT COUNT(*) AS n FROM jobs")["n"]), (1, 3))
        self.assertNotIn("Edited.", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (rows[stored + "B"],))[0])
        raw, employer = parse_saved_html(self.saved_page("R-2001", portal + "A", body=calgary), "page.html")
        counts = ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url",
                        full_board=False)
        self.assertEqual((counts["new"], store.one("SELECT COUNT(*) AS n FROM jobs")["n"]), (1, 4))

    def test_linkless_saved_pages_are_one_posting_only_with_the_same_employer_and_title(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 10, H13: two saved pages with no link, identifier R-4001 and the same
        description, one "Claims Analyst" for "A+B Health" in Canada and one "Claims Operations Analyst" for
        "A B Health" in the US, were taken for one posting: the second overwrote the first's employer, title and
        location, which then entered standing routing."""
        import json as _json

        from careeros.discovery.saved import parse_saved_html
        on = policy(enabled=True)

        def page(employer, title, country):
            data = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "identifier": "R-4001",
                    "hiringOrganization": {"@type": "Organization", "name": employer}, "jobLocationType": "TELECOMMUTE",
                    "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": country}},
                    "description": "<p>" + self.BODY.replace("\n", "</p><p>") + "</p>"}
            return '<html><head><script type="application/ld+json">' + _json.dumps(data) + "</script></head></html>"
        store, ctx = setup()
        for employer, title, country in (("A+B Health", "Claims Analyst", "Canada"), ("A B Health", "Claims Operations Analyst", "US")):
            raw, name = parse_saved_html(page(employer, title, country), "page.html")
            ingest(ctx, {"employer_id": None, "name": name, "parent": name}, [raw], source="manual_url", full_board=False)
        process(ctx)
        rows = {r["job_id"]: dict(r) for r in store.all("SELECT * FROM jobs")}
        self.assertEqual(len(rows), 2)
        first = rows["manual:a-b-health:R-4001"]
        self.assertEqual((first["employer_name"], first["title"], first["location_raw"]), ("A+B Health", "Claims Analyst", "Remote - Canada"))
        self.assertNotEqual(first["country"], "US")
        self.assertIsNone(packages.route_for(store, on, first["job_id"])[0])
        other = next(j for j in rows if j != first["job_id"])
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ? AND relation = 'possible_duplicate'",
                                  (other, first["job_id"])))
        # the control: the same employer, title and text with no link is the same posting only as its operation's
        # retry (careeros import's receipt: the D190 design, section 6); read again with no receipt, its own job
        raw, name = parse_saved_html(page("A+B Health", "Claims Analyst", "Canada"), "page.html")
        counts = ingest(ctx, {"employer_id": None, "name": name, "parent": name}, [raw], source="manual_url", full_board=False)
        self.assertEqual((counts["new"], store.one("SELECT COUNT(*) AS n FROM jobs")["n"]), (1, 3))
        # the same employer and text under another title is another posting too
        raw, name = parse_saved_html(page("A+B Health", "Claims Supervisor", "Canada"), "page.html")
        counts = ingest(ctx, {"employer_id": None, "name": name, "parent": name}, [raw], source="manual_url", full_board=False)
        self.assertEqual((counts["new"], store.one("SELECT title FROM jobs WHERE job_id = 'manual:a-b-health:R-4001'")[0]),
                         (1, "Claims Analyst"))

    def test_an_interrupted_location_confirmation_never_leaves_a_package_ready(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 7, H5: a confirmation interrupted before processing left the new location
        with the old country and score; package prepare and readiness used the old score. Since D180 you set a location
        by naming the job (set_location), and only a job board's own postings are picked by standing preparation."""
        from unittest import mock

        from careeros import openings
        from careeros.discovery.manual_intake import intake, set_location
        from careeros.review import assist
        on = policy(enabled=True)
        store, ctx = setup()
        url = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-4801"
        job_id = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY, url, location="Remote - US")["job_id"]
        process(ctx)
        # a failure while recording the location you set undoes the change with it
        with mock.patch.object(openings, "record_observation", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                set_location(ctx, job_id, "Remote - Canada")
        self.assertEqual(tuple(store.one("SELECT location_raw, current_state FROM jobs")), ("Remote - US", "scored"))
        give_label(store, POLICY, MID, job_id, "apply", None, "k-apply")   # from a preview of the screened job
        # set, before processing: the job waits for its re-screen; your Apply label keeps its route (D166) and
        # assistance refuses it until its eligibility is current again
        self.assertTrue(set_location(ctx, job_id, "Remote - Canada")["changed"])
        # the country is recomputed in the same write (part 4 of the owner's D192 build); the screen waits for processing
        self.assertEqual(tuple(store.one("SELECT location_raw, country, current_state FROM jobs")), ("Remote - Canada", "non_US", "normalized"))
        # your label was given from a preview of the job before the change: it no longer routes, so nothing is prepared
        # and no session opens (the D190 design, P1; before it the label kept its route and assistance refused)
        self.assertIsNone(packages.route_for(store, on, job_id, MID)[0])
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, on, MID, job_id, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                             questions=QUESTIONS)
        with self.assertRaises(assist.SessionRefused):
            assist.open_session(store, on, MID, job_id, "manual", facts=FACTS, library=self.vault.library,
                                vault_root=self.vault.root, questions=QUESTIONS, key="o-label")
        process(ctx)                                                       # the clean retry
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "non_US")
        self.assertIsNone(packages.route_for(store, on, job_id)[0])
        # a job board's posting that changed since it was scored: nothing is ready without your label (D183)
        store, ctx = setup()
        employer = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME, "synth"))], full_board=False)
        process(ctx)
        self.assertEqual(packages.standing_candidates(store, on), [])
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME + "\nNew duties.", "synth"))],
               full_board=False)
        self.assertEqual(store.one("SELECT current_state FROM jobs")["current_state"], "normalized")
        self.assertEqual(packages.route_for(store, on, JOB), (None, OFF))
        state = packages.readiness(store, on, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                                   questions=QUESTIONS)
        self.assertIn(state["state"], ("not_prepared", "withdrawn"))

    def test_a_score_made_before_the_provenance_rule_routes_nothing_before_processing(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 7, H6: a score made by the Pass 6 candidate (same decision fingerprint, no
        provenance rule) routed a standing package for an older parser's prose location before the first processing run.
        Since D180 a pasted job is never picked by standing preparation, whatever its score."""
        from unittest import mock

        from careeros.discovery import manual_intake
        from careeros.normalize.posting import RawPosting
        from careeros.openings import record_observation
        from careeros.qualify import gate as gate_mod
        on = policy(enabled=True)
        url = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-4802"
        text = "Claims Analyst\nCompany: Acme Rx\n\n" + self.BODY + "\n" + self.PROSE
        job_id = "manual:acme-rx:R-4802"
        for rules in ("gate-2026-11-16a", gate_mod.RULES_VERSION):
            store, ctx = setup()
            ingest(ctx, {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"},   # as the older parser saved it
                   [RawPosting(ats="manual", board_token="acme-rx", external_id="R-4802", title="Claims Analyst",
                               location="Remote - US", url=url, apply_url=url, description_text=text, requisition_id="R-4802",
                               raw={"source": "human_text", "url": url})], source="manual_url", full_board=False)
            store.conn.execute("UPDATE jobs SET source_json = NULL")   # a row stored before 017: no source record
            record_observation(store, source_kind="human_text", evidence_level="human", job_id=job_id, url=url,
                               title="Claims Analyst", company="Acme Rx", location="Remote - US")
            with mock.patch.object(gate_mod, "RULES_VERSION", rules), \
                    mock.patch.object(manual_intake, "location_declared", return_value=True):   # no provenance rule
                process(ctx)
            self.assertEqual(store.one("SELECT country FROM jobs")["country"], "US")
            self.held_everywhere(store, on, job_id)
            process(ctx)
            self.assertEqual(store.one("SELECT country FROM jobs")["country"], "unknown")
            self.held_everywhere(store, on, job_id)

    def test_common_gaps_counts_each_open_question_across_packages(self):
        store, _ = posting_store()
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
            give_label(store, POLICY, MID, f"greenhouse:synth:{n}", "apply", None, f"k{n}")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
        self.assertIn("standing preparation is off", run("prepare", standing=True))


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
        give_label(store, POLICY, MID, JOB, "stretch", None, "k1")
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k-apply")
        self.assertIsNone(packages.route_for(store, on, JOB)[0])                  # open application: not again
        store.clock.advance(seconds=60)
        update_application(store, application_matrix(), app, "withdrawn", key="withdraw")
        record_label(store, JOB, "skip", None, "k-skip")
        store.clock.advance(seconds=60)
        give_label(store, POLICY, MID, JOB, "apply", "applying again after withdrawing", "k-again")
        route, why = packages.route_for(store, on, JOB)
        self.assertIsNone(route)                                                 # not even a label given after it
        self.assertIn("careeros applied --reapply", why)
        store3, _ = posting_store(FULL_TIME)
        give_label(store3, POLICY, MID, JOB, "apply", None, "k-before")                    # your label from before you applied
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
        give_label(store, POLICY, MID, JOB, "apply", None, "k1")
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
        """F7: the veto is the shared preflight, so an application on another posting of the same role, or a duplicate
        check that can't finish, withdraws a prepared package and refuses a new one. One that may be the same role is
        shown, never held (the owner's D188)."""
        from unittest import mock

        from careeros.openings import link
        from careeros.review.applications import application_matrix, record_application
        from tests.helpers import add_job, give_label
        on = policy(enabled=True)
        for n, (relation, code) in enumerate((("same_opening", "applied"), ("possible_duplicate", None))):
            store, _ = posting_store(FULL_TIME)
            self.vault.close()
            self.vault = Vault()                                                  # each store prepares its own v1
            give_label(store, POLICY, MID, JOB, "apply", None, f"k{n}")
            self.prepare(store, on)
            other = add_job(store, f"greenhouse:synth:9{n}", employer_name="Synthetic Health", parent="Synthetic Health",
                            title="Claims Specialist", title_norm="claims specialist")
            link(store, other, JOB, relation, "synthetic")
            record_application(store, application_matrix(), other, "direct", policy=on, key=f"a{n}")
            route, why = packages.route_for(store, on, JOB, MID)
            if code is None:   # a possible duplicate is shown, never held
                self.assertEqual(route, "apply")
                continue
            self.assertIsNone(route)
            self.assertTrue(why.startswith(code + ":"), why)
            ready = packages.readiness(store, on, MID, JOB, facts=FACTS, library=self.vault.library,
                                       vault_root=self.vault.root, questions=QUESTIONS)
            self.assertEqual(ready["state"], "withdrawn")
            with self.assertRaisesRegex(packages.PackageRefused, code):
                self.prepare(store, on)
        store, _ = posting_store(FULL_TIME)
        give_label(store, POLICY, MID, JOB, "apply", None, "k-group")
        with mock.patch("careeros.review.applications.role_group_resolved", return_value=([JOB], False)):
            route, why = packages.route_for(store, on, JOB)
            self.assertIsNone(route)
            self.assertTrue(why.startswith("role_group_incomplete:"), why)
            with self.assertRaisesRegex(packages.PackageRefused, "role_group_incomplete"):
                self.prepare(store, on)


if __name__ == "__main__":
    unittest.main()
