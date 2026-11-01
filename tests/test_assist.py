"""Assistance sessions, handoffs and form adapters (P2–P5, 2026-10-21; ADR 0009). Synthetic data only."""
import json
import unittest
from datetime import timedelta
from unittest import mock

from careeros.core.clock import parse_utc
from careeros.review import adapters, answers, assist
from careeros.review.applications import application_matrix, record_application
from careeros.review.labels import record_label

from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store

APP = application_matrix()
GREENHOUSE_FILL = {"default": {"fill": "unknown", "upload": "blocked"},
                   "greenhouse.io": {"fill": "supervised_only", "upload": "blocked"}}
GREENHOUSE_JOB = {
    "id": 1, "title": "Claims Analyst",
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}]},
        {"label": "Are you authorized to work in the US?", "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Tracking", "fields": [{"name": "gh_src", "type": "input_hidden"}]},
    ],
    "demographic_questions": {"questions": [{"label": "Gender"}]},
}

# Independent Codex review, Pass 2 of 4787e2a (2026-10-22): its two reproductions (a long start date carrying a consent,
# "I agree" padded with invisible characters), a short answer that is filled, and a long routine one that is copied.
LONG_ANSWERS_LIBRARY = (
    '  - id: start\n    kind: start_date\n    question: "When can you start?"\n'
    '    answer: "I can start two weeks after accepting an offer. I agree to the privacy policy and consent to retention '
    'of my application."\n    approved: 2026-10-01\n'
    '  - id: start-padded\n    kind: start_date\n    question: "When could you start?"\n'
    '    answer: "I agree' + "\\u200b" * 81 + '"\n    approved: 2026-10-01\n'
    '  - id: notice\n    kind: notice_period\n    question: "What is your notice period?"\n    answer: "Two weeks"\n'
    '    approved: 2026-10-01\n'
    '  - id: hours\n    kind: availability\n    question: "Are you available to work full-time?"\n'
    '    answer: "Yes. I can work full-time, Monday through Friday, and I can cover occasional evening or weekend shifts '
    'during month-end close and open enrollment."\n    approved: 2026-10-01\n')
# The read-only check of the Codex Pass 2 correction (2026-10-22): words the checks can't read the way the page will
# show them (a line break inside a word, look-alike letters, an unassigned invisible character, entity padding).
HIDDEN_WORDS_LIBRARY = (
    '  - id: notice-split\n    kind: notice_period\n    question: "What is your current notice period?"\n'
    '    answer: "Two weeks. I a\\ngree to the priva\\ncy policy."\n    approved: 2026-10-01\n'
    '  - id: start-lookalike\n    kind: start_date\n    question: "What is your earliest start date?"\n'
    '    answer: "Now. I a\\u0261ree to the priva\\u03f2y poli\\u03f2y."\n    approved: 2026-10-01\n'
    '  - id: travel-unassigned\n    kind: travel\n    question: "Are you willing to travel?"\n'
    '    answer: "Yes, I ag\\u2065ree to travel"\n    approved: 2026-10-01\n'
    '  - id: relocate-entities\n    kind: relocation\n    question: "Are you willing to relocate?"\n'
    '    answer: "Yes' + "&#8203;" * 20 + '"\n    approved: 2026-10-01\n'
    '  - id: relocate-entity-word\n    kind: relocation\n    question: "Do you need relocation assistance?"\n'
    '    answer: "I agree&ordf; to a background check&ordf;"\n    approved: 2026-10-01\n'
    '  - id: travel-stroke\n    kind: travel\n    question: "Are you willing to travel up to 25%?"\n'
    '    answer: "Yes. I c\\u00f8nsent t\\u00f8 a backgr\\u00f8und check."\n    approved: 2026-10-01\n')
HIDDEN_WORDS_QUESTIONS = [
    {"key": "question", "name": "question_78", "label": "What is your current notice period?", "type": "text"},
    {"key": "question", "name": "question_79", "label": "What is your earliest start date?", "type": "text"},
    {"key": "question", "name": "question_80", "label": "Are you willing to travel?", "type": "text"},
    {"key": "question", "name": "question_81", "label": "Are you willing to relocate?", "type": "text"},
    {"key": "question", "name": "question_82", "label": "Do you need relocation assistance?", "type": "text"},
    {"key": "question", "name": "question_83", "label": "Are you willing to travel up to 25%?", "type": "text"}]
LONG_ANSWER_QUESTIONS = [QUESTIONS[0],
                         {"key": "question", "name": "question_75", "label": "When can you start?", "type": "text"},
                         {"key": "question", "name": "question_74", "label": "When could you start?", "type": "text"},
                         {"key": "question", "name": "question_76", "label": "What is your notice period?", "type": "text"},
                         {"key": "question", "name": "question_77", "label": "Are you available to work full-time?",
                          "type": "text"}]


class Base(unittest.TestCase):
    def setUp(self):
        self.vault = Vault()
        self.store, self.ctx = posting_store()
        record_label(self.store, JOB, "apply", None, "k1")
        self.prepare()

    def tearDown(self):
        self.vault.close()

    def prepare(self, questions=QUESTIONS):
        from careeros.review import packages
        return packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library,
                                vault_root=self.vault.root, questions=questions)

    def open(self, mode="manual", key="o1", questions=QUESTIONS, policy=POLICY):
        return assist.open_session(self.store, policy, MID, JOB, mode, facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=questions, key=key)

    def close(self, session_id, outcome, key="c1", **kw):
        return assist.close_session(self.store, APP, POLICY, session_id, outcome, key=key, **kw)

    def vault_text(self):
        return (self.vault.root / "answers.yaml").read_text(encoding="utf-8")


class SessionTests(Base):
    def test_a_session_reserves_the_role_until_you_close_it(self):
        first = self.open()
        self.assertEqual(assist.session_state(self.store, first["session_id"]), "open")
        with self.assertRaisesRegex(assist.SessionRefused, "session_open"):
            self.open(key="o2")
        self.close(first["session_id"], "not_submitted")
        second = self.open(key="o3")
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_an_uncertain_session_blocks_a_new_one_until_reconciled_and_is_never_resent(self):
        first = self.open()
        self.close(first["session_id"], "uncertain", notes="the page froze after I pressed Submit")
        with self.assertRaisesRegex(assist.SessionRefused, "submission_uncertain"):
            self.open(key="o2")
        out = self.close(first["session_id"], "submitted", key="c2", evidence="confirmation_observed")
        self.assertTrue(out["reconciled"])
        self.assertIsNotNone(out["application_id"])
        with self.assertRaisesRegex(assist.SessionRefused, "already_applied|applied"):
            self.open(key="o3")                                               # the role now has an application

    def test_submitted_records_the_application_with_its_evidence(self):
        session = self.open()
        out = self.close(session["session_id"], "submitted", evidence="confirmation_observed", minutes=12.5, corrections=2)
        event = self.store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded'")
        meta = json.loads(event["metadata_json"])
        self.assertEqual((meta["evidence"], meta["session"]), ("confirmation_observed", session["session_id"]))
        app = self.store.one("SELECT * FROM applications WHERE application_id = ?", (out["application_id"],))
        self.assertEqual(app["channel"], "direct")
        with self.assertRaisesRegex(assist.SessionRefused, "session_closed"):
            self.close(session["session_id"], "not_submitted", key="c2")
        self.assertEqual(assist.report(self.store)["manual"]["median_minutes"], 12.5)

    def test_evidence_belongs_only_to_a_submission(self):
        session = self.open()
        with self.assertRaisesRegex(ValueError, "evidence_only_for_submitted"):
            self.close(session["session_id"], "not_submitted", evidence="owner_attested")
        with self.assertRaisesRegex(ValueError, "unknown_evidence"):
            record_application(self.store, APP, JOB, "direct", policy=POLICY, key="x", evidence="a feeling")

    def test_a_session_needs_a_ready_package_and_authorization(self):
        text = (self.vault.root / "answers.yaml").read_text(encoding="utf-8")
        self.vault.write(text.replace("answer: Avery", "answer: Ava"))                   # an answer changed since preparing
        with self.assertRaisesRegex(assist.SessionRefused, "package stale"):
            self.open()
        self.prepare()
        record_application(self.store, APP, JOB, "direct", policy=POLICY, key="applied-elsewhere")   # you applied outside
        with self.assertRaisesRegex(assist.SessionRefused, "already_applied"):
            self.open(key="o2")

    def test_the_report_labels_a_small_sample(self):
        for i in range(2):
            s = self.open(key=f"o{i}")
            self.close(s["session_id"], "not_submitted", key=f"c{i}", minutes=10 + i)
        report = assist.report(self.store)["manual"]
        self.assertEqual((report["sessions"], report["median_minutes"], report["measured_minutes"]), (2, 10.5, 2))
        self.assertIn("too small", report["sample"])


class AssistedModeTests(Base):
    def test_filling_needs_permission_for_the_destination(self):
        with mock.patch.object(assist, "assistance_policy", return_value={"default": {"fill": "unknown", "upload": "blocked"}}):
            with self.assertRaisesRegex(assist.SessionRefused, "assistance_not_permitted"):
                self.open(mode="assisted")                                     # unknown = manual

    def test_a_handoff_holds_only_the_matched_values_bound_to_the_destination_and_expiring(self):
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted")
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual(data["destination"], {"origins": list(adapters.GREENHOUSE_ORIGINS), "path": "/synth/jobs/1"})
        self.assertEqual([(f["name"], f["value"]) for f in data["fields"]], [("first_name", "Avery"), ("question_1", "Yes")])
        values = json.dumps(data)
        self.assertNotIn("should never be used", values)                      # the sensitive answer never leaves
        self.assertNotIn("Open to discussing", values)                        # nor an expired one
        self.assertTrue(any("Why do you want to work here?" in r for r in data["review"]))
        self.assertEqual([a["kind"] for a in data["attach"]], ["resume"])     # you attach files yourself
        opened = parse_utc(self.store.one("SELECT opened_at FROM assist_sessions")["opened_at"])
        self.assertEqual(parse_utc(data["expires_at"]) - opened, timedelta(hours=assist.HANDOFF_HOURS))

    def test_an_answer_the_form_cant_take_stays_with_you(self):
        self.vault.write(self.vault_text() + "  - id: relocate\n    kind: relocation\n    question: Are you willing to relocate?\n    answer: \"Yes\"\n    approved: 2026-10-01\n")
        questions = [QUESTIONS[0],
                     {"key": "question", "name": "question_8", "label": "Are you willing to relocate?", "type": "select",
                      "options": ["Yes, I am willing", "No"]},                             # your "Yes" isn't a choice
                     {"key": "question", "name": "question_6", "label": "Are you legally authorized to work in the United States?",
                      "type": "multiselect"}]                                              # a field kind never filled
        self.prepare(questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", questions=questions)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual([f["name"] for f in data["fields"]], ["first_name"])
        self.assertTrue(any("option mismatch" in r for r in data["review"]), data["review"])
        self.assertTrue(any("United States" in r for r in data["review"]), data["review"])

    def test_no_adapter_or_unknown_questions_means_manual(self):
        with mock.patch.object(assist, "assistance_policy", return_value={**GREENHOUSE_FILL, "example.com": {"fill": "allowed",
                                                                                                             "upload": "blocked"}}):
            self.prepare(questions=None)
            with self.assertRaisesRegex(assist.SessionRefused, "questions_unknown"):
                self.open(mode="assisted", questions=None)
            self.assertIsNone(assist.adapter_for("careers.example.com"))

    def test_capability_reads_the_nearest_listed_domain(self):
        self.assertEqual(assist.capability("job-boards.greenhouse.io", "fill", GREENHOUSE_FILL), "supervised_only")
        self.assertEqual(assist.capability("evilgreenhouse.io", "fill", GREENHOUSE_FILL), "unknown")
        self.assertEqual(assist.capability("job-boards.greenhouse.io", "upload", GREENHOUSE_FILL), "blocked")
        shipped = assist.assistance_policy()
        self.assertEqual(shipped["default"], {"fill": "unknown", "upload": "blocked"})
        self.assertEqual(assist.capability("job-boards.greenhouse.io", "fill", shipped), "unknown")   # until you turn it on
        self.assertEqual(assist.capability("job-boards.greenhouse.io", "upload", shipped), "blocked")
        self.assertEqual(assist.capability("jobs.lever.co", "fill", shipped), "unknown")


class AdapterTests(unittest.TestCase):
    def test_greenhouse_questions_become_package_questions(self):
        found = adapters.greenhouse_questions(GREENHOUSE_JOB)
        self.assertEqual([(q["key"], q["name"], q["type"]) for q in found],
                         [("first_name", "first_name", "text"), ("resume", "resume", "file"), ("question", "question_1", "select")])
        self.assertEqual(found[2]["options"], ["Yes", "No"])
        self.assertNotIn("Gender", json.dumps(found))                        # demographic questions stay on the form

    def test_only_a_greenhouse_job_reads_greenhouse_questions(self):
        with self.assertRaisesRegex(ValueError, "not_a_greenhouse_job"):
            adapters.fetch_greenhouse_questions(None, "lever:acme:1")


class SecondReviewTests(Base):
    """The second Claude review of the Codex corrections (2026-10-22): sessions, the Greenhouse switch, choices."""

    def test_greenhouse_filling_stays_off_until_you_turn_it_on_after_the_installed_check(self):
        from types import SimpleNamespace

        from careeros.pipeline import decision_fingerprint
        shipped = assist.assistance_policy()
        self.assertEqual(assist.capability("job-boards.greenhouse.io", "fill", shipped), "unknown")
        self.assertEqual(shipped["greenhouse.io"]["reviewed_fill"], "supervised_only")
        self.prepare()
        with self.assertRaisesRegex(assist.SessionRefused, "assistance_not_permitted"):
            self.open(mode="assisted", key="off")                               # as shipped: manual
        on = self._with_assistance({"greenhouse.io": {"fill": "supervised_only", "installed_check": "2026-10-10"}})
        rules = assist.assistance_policy(policy=on)
        self.assertEqual((rules["greenhouse.io"]["fill"], rules["greenhouse.io"]["installed_check"]),
                         ("supervised_only", "2026-10-10"))
        self.assertIn("handoff", self.open(mode="assisted", key="on", policy=on))
        for bad in ({"greenhouse.io": {"fill": "supervised_only"}},                          # no installed check
                    {"greenhouse.io": {"fill": "allowed", "installed_check": "2026-10-23"}},  # beyond what was reviewed
                    {"lever.co": {"fill": "supervised_only", "installed_check": "2026-10-23"}},  # not a listed host
                    {"greenhouse.io": {"fill": "supervised_only", "installed_check": "2026-10-23", "upload": "allowed"}}):
            with self.assertRaisesRegex(ValueError, "assistance_override_invalid", msg=str(bad)):
                assist.assistance_policy(policy=self._with_assistance(bad))
        lowered = assist.assistance_policy(policy=self._with_assistance({"greenhouse.io": {"fill": "blocked"}}))
        self.assertEqual(lowered["greenhouse.io"]["fill"], "blocked")           # lowering needs no date
        self.assertEqual(decision_fingerprint(SimpleNamespace(policy=POLICY)),   # your switch never re-judges a job
                         decision_fingerprint(SimpleNamespace(policy=on)))

    def _with_assistance(self, assistance):
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        data = {**POLICY.data, "assistance": assistance}
        return PolicyBundle(data=data, fingerprint=fingerprint(data))

    def test_a_repeated_open_or_close_returns_the_first_result_whole(self):
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            first = self.open(mode="assisted", key="whole")
            again = self.open(mode="assisted", key="whole")
        self.assertEqual(again, {**first, "replay": True, "state": "open"})
        self.assertTrue((self.vault.root / again["handoff"]).is_file())
        uncertain = self.close(first["session_id"], "uncertain", key="u1")
        closed = self.close(first["session_id"], "submitted", key="s1")
        repeat = self.close(first["session_id"], "submitted", key="s1")
        self.assertEqual(repeat, {**closed, "replay": True})
        self.assertEqual((closed["reconciled"], uncertain["reconciled"]), (True, False))

    def test_a_close_repeated_with_another_explanation_is_refused(self):
        from careeros.db.store import IdempotencyCollision
        session = self.open(key="o-expl")["session_id"]
        self.close(session, "submitted", key="c-expl")
        for change in ({"reapply": "a different reason"}, {"confirm": "a different confirmation"}):
            with self.assertRaises(IdempotencyCollision, msg=str(change)):
                self.close(session, "submitted", key="c-expl", **change)

    def test_a_handoff_that_cant_be_written_leaves_no_session_and_a_retry_works(self):
        from pathlib import Path
        real = Path.write_text

        def failing(path, *args, **kwargs):
            if path.parent.name == "handoff":
                raise OSError(28, "disk full (synthetic)")
            return real(path, *args, **kwargs)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with mock.patch.object(Path, "write_text", failing):
                with self.assertRaises(OSError):
                    self.open(mode="assisted", key="retry")
            self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM assist_sessions")["n"], 0)
            out = self.open(mode="assisted", key="retry")                       # the same key: nothing was recorded
        self.assertTrue((self.vault.root / out["handoff"]).is_file())

    def test_an_unreviewed_posting_address_is_one_of_the_refusal_reasons(self):
        embed = "https://boards.greenhouse.io/embed/job_app?for=synth&token=1"
        self.store.conn.execute("UPDATE jobs SET apply_url = ?, url = ? WHERE job_id = ?", (embed, embed, JOB))
        self.prepare()
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with self.assertRaisesRegex(assist.SessionRefused, "destination_not_reviewed.*use manual mode"):
                self.open(mode="assisted", key="embed")
            self.open(key="manual-first")
            with self.assertRaises(assist.SessionRefused) as refused:
                self.open(mode="assisted", key="embed-2")
        self.assertIn("session_open", str(refused.exception))
        self.assertIn("destination_not_reviewed", str(refused.exception))      # every reason, together

    def test_consent_in_the_forms_choices_or_in_your_answer_never_reaches_a_handoff(self):
        self.vault.write(self.vault_text() + "  - id: confirm\n    kind: experience\n    question: Confirmation\n"
                                             "    answer: I agree\n    approved: 2026-10-01\n"
                                             "  - id: statement\n    kind: experience\n    question: Statement\n"
                                             "    answer: \"Yes\"\n    approved: 2026-10-01\n")
        questions = QUESTIONS + [
            {"key": "question", "name": "question_15", "label": "Confirmation", "type": "text"},
            {"key": "question", "name": "question_16", "label": "Statement", "type": "select",
             "options": ["Yes, I have read and agree to the privacy notice", "No"]},
            {"key": "question", "name": "question_17\n", "label": "Are you authorized to work in the US?", "type": "text"}]
        self.prepare(questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", key="consent", questions=questions)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual([f["name"] for f in data["fields"]], ["first_name", "question_1"])
        review = " | ".join(data["review"])
        self.assertIn("Confirmation (sensitive: your answer confirm: its answer reads as consent", review)
        self.assertIn("Statement (sensitive: a choice on the form reads as consent", review)
        panel = (self.vault.root / out["answers"]).read_text(encoding="utf-8")
        self.assertNotIn("I agree\n", panel)
        self.assertIn("YOU ANSWER THIS (sensitive", panel)



class CommandTests(Base):
    def test_open_and_close_through_the_commands(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_assist
        from careeros.review import packages
        packages.save_questions(self.vault.root, JOB, QUESTIONS, "test", self.store.clock.now())
        paths = {"root": self.vault.root, "approved_facts": self.vault.root / "none.yaml", "answers": self.vault.root / "answers.yaml"}

        def run(action, target=None, **kw):
            args = argparse.Namespace(action=action, target=target, mode=kw.get("mode", "manual"), outcome=kw.get("outcome"),
                                      evidence=kw.get("evidence"), minutes=kw.get("minutes"), corrections=None,
                                      custom_questions=None, login_steps=None, notes=None, reapply=None, confirm=None)
            out = io.StringIO()
            with redirect_stdout(out):
                cmd_assist(self.ctx, paths, args)
            return out.getvalue()

        packages.prepare(self.store, POLICY, MID, JOB, facts=[], library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)   # the command reads facts from the vault, and this vault has none
        opened = run("open", JOB)
        session_id = opened.split()[1]
        self.assertIn("open (manual)", opened)
        self.assertIn("1 session(s)", run("list", JOB))
        closed = run("close", session_id, outcome="submitted", evidence="confirmation_observed", minutes=9)
        self.assertIn("application app:", closed)
        self.assertEqual(json.loads(run("report"))["manual"]["median_minutes"], 9.0)


class CodexBatchReviewTests(Base):
    """Independent Codex review of ac83e00 (2026-10-22): F2 (export checks the answer it was prepared with), F8."""

    def test_the_handoff_refuses_an_answer_other_than_the_one_prepared(self):
        from careeros.review import packages
        package = packages.latest(self.store, JOB)
        manifest = json.loads(package["manifest_json"])
        swapped = {**self.vault.library, "answers": [{**a, "answer": "Someone Else"} if a["id"] == "first-name" else a
                                                     for a in self.vault.library["answers"]]}
        with self.assertRaisesRegex(ValueError, "answer_changed"):
            adapters.handoff("greenhouse", "ses:x", package, manifest, swapped, "2026-10-12T14:00:00.000Z")

    def test_a_repeated_open_or_close_with_the_same_key_returns_the_first_result(self):
        first = self.open(key="same-open")
        again = self.open(key="same-open")
        self.assertEqual((again["replay"], again["session_id"]), (True, first["session_id"]))
        closed = self.close(first["session_id"], "not_submitted", key="same-close")
        repeat = self.close(first["session_id"], "not_submitted", key="same-close")
        self.assertEqual((repeat["replay"], repeat["outcome_id"]), (True, closed["outcome_id"]))
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM assist_outcomes")["n"], 1)
        from careeros.db.store import IdempotencyCollision
        with self.assertRaises(IdempotencyCollision):                          # the same key for something else
            self.close(first["session_id"], "uncertain", key="same-close")

    def test_only_a_reviewed_posting_address_and_field_reach_a_handoff(self):
        """F6: the server side holds the same reviewed hosts, path and field names as the extension."""
        self.assertEqual(adapters.destination("greenhouse", "https://job-boards.greenhouse.io/synth/jobs/1")["path"],
                         "/synth/jobs/1")
        for url in ("https://careers.example.com/synth/jobs/1", "https://job-boards.greenhouse.io/synth/apply",
                    "http://job-boards.greenhouse.io/synth/jobs/1"):
            with self.assertRaisesRegex(ValueError, "destination_not_reviewed", msg=url):
                adapters.destination("greenhouse", url)
        questions = [QUESTIONS[0], {"key": "question", "name": "job_application[answers_attributes][0][text_value]",
                                    "label": "Are you authorized to work in the US?", "type": "text"}]
        self.prepare(questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", questions=questions)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual([f["name"] for f in data["fields"]], ["first_name"])   # an unreviewed field name stays yours


class ThirdReviewTests(Base):
    """The third Claude review of the Codex corrections (2026-10-22): the documented switch, the date, an interrupted
    commit, replays of closed sessions, and the handoff's own re-check."""

    def test_the_documented_switch_loads_from_the_overrides_file(self):
        import tempfile
        from pathlib import Path

        import yaml

        from careeros.core.policy import load_policy
        with tempfile.TemporaryDirectory() as tmp:
            defaults, overrides = Path(tmp) / "defaults.yaml", Path(tmp) / "overrides.yaml"
            defaults.write_text(yaml.safe_dump(POLICY.data), encoding="utf-8")   # the test's rules, as a file
            overrides.write_text("assistance:\n  greenhouse.io: {fill: supervised_only, installed_check: 2026-10-10}\n",
                                 encoding="utf-8")                              # unquoted, exactly as the guide shows
            policy = load_policy(defaults, overrides)
        rules = assist.assistance_policy(policy=policy, today="2026-10-12")
        self.assertEqual((rules["greenhouse.io"]["fill"], rules["greenhouse.io"]["installed_check"]),
                         ("supervised_only", "2026-10-10"))
        self.assertIn("handoff", self.open(mode="assisted", key="switch", policy=policy))

    def test_the_check_date_is_a_real_past_date(self):
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        for bad in ("2026-10-13", "2099-12-31", "2026-W43-5", "20261010", 20261010, True, "2026-02-30"):
            data = {**POLICY.data, "assistance": {"greenhouse.io": {"fill": "supervised_only", "installed_check": bad}}}
            with self.assertRaisesRegex(ValueError, "installed_check", msg=repr(bad)):
                assist.assistance_policy(policy=PolicyBundle(data=data, fingerprint=fingerprint(data)), today="2026-10-12")

    def test_an_interrupt_after_commit_keeps_the_handoff_of_the_recorded_session(self):
        from careeros.db.store import Store
        real = Store.tx

        import contextlib

        @contextlib.contextmanager
        def interrupted(store):
            with real(store):
                yield
            if store._depth == 0:   # the outermost transaction has just committed
                raise KeyboardInterrupt   # as a Ctrl+C arriving right after COMMIT would
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with mock.patch.object(Store, "tx", interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    self.open(mode="assisted", key="interrupted")
        session = self.store.one("SELECT session_id FROM assist_sessions")
        self.assertIsNotNone(session)
        path = self.vault.root / "handoff" / (session["session_id"].replace(":", "_") + ".json")
        self.assertTrue(path.is_file())

    def test_a_handoff_written_in_part_is_removed_when_its_session_is_not_recorded(self):
        from pathlib import Path
        real = Path.write_text

        def partial(path, data, *args, **kwargs):
            if path.parent.name == "handoff":
                real(path, data[:20], *args, **kwargs)
                raise OSError(28, "disk full (synthetic)")
            return real(path, data, *args, **kwargs)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with mock.patch.object(Path, "write_text", partial):
                with self.assertRaises(OSError):
                    self.open(mode="assisted", key="partial")
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM assist_sessions")["n"], 0)
        self.assertEqual(list((self.vault.root / "handoff").glob("*.json")), [])

    def test_a_replay_says_what_became_of_the_session(self):
        first = self.open(key="replayed")
        self.close(first["session_id"], "not_submitted", key="closed")
        again = self.open(key="replayed")
        self.assertEqual((again["replay"], again["session_id"], again["state"]), (True, first["session_id"], "not_submitted"))

    def test_the_handoff_reads_the_forms_choices_again_itself(self):
        """A manifest row that says matched, with a consent among its choices, is still not handed over."""
        from careeros.review import packages
        package = packages.latest(self.store, JOB)
        manifest = json.loads(package["manifest_json"])
        for row in manifest["questions"]:
            if row["name"] == "question_1":
                row["options"] = ["Yes", "Yes, and I agree to the privacy notice"]
        data = adapters.handoff("greenhouse", "ses:x", package, manifest, self.vault.library, "2026-10-12T14:00:00.000Z")
        self.assertEqual([f["name"] for f in data["fields"]], ["first_name"])
        self.assertTrue(any("question_1" not in r and "sensitive: a choice on the form" in r for r in data["review"]))

    def test_a_named_answer_is_copied_from_the_panel_never_filled(self):
        """Sixth Claude review: a skill, credential or employer name can always encode a protected subject, so the
        extension never fills those kinds; you copy them, reading each one."""
        self.vault.write(self.vault_text() + "  - id: claims\n    kind: experience\n"
                         "    question: How many years of experience do you have in claims adjudication?\n"
                         "    answer: \"6\"\n    approved: 2026-10-01\n")
        questions = QUESTIONS + [{"key": "question", "name": "question_9", "type": "text",
                                  "label": "How many years of experience do you have in claims adjudication?"}]
        self.prepare(questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", key="named", questions=questions)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertNotIn("question_9", [f["name"] for f in data["fields"]])
        self.assertTrue(any(r.startswith("copy from the panel: How many years") for r in data["review"]), data["review"])
        panel = (self.vault.root / out["answers"]).read_text(encoding="utf-8")
        self.assertIn("How many years of experience do you have in claims adjudication?\n  6", panel)

    def test_a_standard_field_with_no_label_still_has_its_choices_read(self):
        found = answers.find_field("first_name", self.vault.library, "2026-10-21", "", ["Avery", "I agree to the terms"])
        self.assertEqual(found["status"], "sensitive")


class CheckHandoffToolTests(unittest.TestCase):
    """tools/check_handoff.py, the synthetic handoff for the installed-extension check (third review: its rough edges)."""

    def setUp(self):
        import sys
        from datetime import datetime, timezone

        from careeros.paths import ENGINE_ROOT
        sys.path.insert(0, str(ENGINE_ROOT / "tools"))
        import check_handoff
        self.tool, self.now = check_handoff, datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)

    def test_it_writes_synthetic_values_for_a_reviewed_posting_and_refuses_anything_else(self):
        data = self.tool.make("https://job-boards.greenhouse.io/synth/jobs/1", [("question_3", "textarea", "Synthetic")],
                              self.now)
        self.assertEqual(data["expires_at"], "2026-10-22T10:30:00.000Z")
        self.assertEqual([f["kind"] for f in data["fields"]], ["text", "text", "text", "textarea"])
        for extra in ([("unreviewed_field", "text", "x")], [("question_3", "checkbox", "x")],
                      [("question_3", "text", "")], [("question_3", "text", "x" * 5001)]):
            with self.assertRaises(ValueError, msg=str(extra)):
                self.tool.make("https://job-boards.greenhouse.io/synth/jobs/1", extra, self.now)
        with self.assertRaisesRegex(ValueError, "destination_not_reviewed"):
            self.tool.make("https://careers.example.com/jobs/1", [], self.now)

    def test_a_short_life_can_be_asked_for_to_see_an_expired_file_refused(self):
        url = "https://job-boards.greenhouse.io/synth/jobs/1"
        self.assertEqual(self.tool.make(url, [], self.now, 1)["expires_at"], "2026-10-22T10:01:00.000Z")
        self.assertEqual(self.tool.make(url, [], self.now, 120)["expires_at"], "2026-10-22T12:00:00.000Z")
        for minutes in (0, 121):
            with self.assertRaisesRegex(ValueError, "1 to 120 minutes"):
                self.tool.make(url, [], self.now, minutes)

    def test_a_choice_without_a_value_is_refused_not_dropped(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            code = self.tool.main(["https://job-boards.greenhouse.io/synth/jobs/1", "--select", "question_2"])
        self.assertEqual(code, 1)
        self.assertIn("NAME=VALUE", err.getvalue())


class CodexPass2Tests(Base):
    """Independent Codex review, Pass 2 of 4787e2a (2026-10-22): an answer's own text is read whatever its length, and
    a long answer is never filled."""

    def test_a_long_or_padded_consent_never_reaches_a_handoff_and_a_long_answer_is_copied(self):
        self.vault.write(self.vault_text() + LONG_ANSWERS_LIBRARY)
        self.prepare(questions=LONG_ANSWER_QUESTIONS)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", questions=LONG_ANSWER_QUESTIONS)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual([(f["name"], f["value"]) for f in data["fields"]], [("first_name", "Avery"), ("question_76", "Two weeks")])
        for label in ("When can you start?", "When could you start?"):
            self.assertTrue(any(label in r and "consent, agreement or attestation" in r for r in data["review"]), data["review"])
        self.assertTrue(any(r.startswith("copy from the panel: Are you available to work full-time?") for r in data["review"]),
                        data["review"])
        self.assertNotIn("privacy policy", json.dumps(data["fields"]))

    def test_a_value_the_checks_cant_read_as_the_page_will_is_copied_never_filled(self):
        self.vault.write(self.vault_text() + HIDDEN_WORDS_LIBRARY)
        questions = [QUESTIONS[0], *HIDDEN_WORDS_QUESTIONS]
        self.prepare(questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = self.open(mode="assisted", questions=questions)
        data = json.loads((self.vault.root / out["handoff"]).read_text(encoding="utf-8"))
        self.assertEqual([f["name"] for f in data["fields"]], ["first_name"])
        for question in HIDDEN_WORDS_QUESTIONS:   # copied, or (a word an entity hides, read as written) yours to answer
            self.assertTrue(any(r.startswith((f"copy from the panel: {question['label']} (never filled:",
                                              f"you answer: {question['label']} (sensitive")) for r in data["review"]),
                            (question["label"], data["review"]))
        self.assertTrue(any(r.startswith("you answer: Do you need relocation assistance? (sensitive") for r in data["review"]))


if __name__ == "__main__":
    unittest.main()
