"""Application tracker, email leads on the review page, new-since-last-review and stale flags. Synthetic data only."""
import argparse
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx

from careeros.cli import cmd_applied, cmd_import
from careeros.core.clock import FixedClock
from careeros.db.store import PermissionDenied, open_store
from careeros.openings import application_status, link
from careeros.pipeline import Context, _has_application
from careeros.qualify.profile import load_profile
from careeros.review.applications import (OPEN_STATES, ApplicationHold, application_matrix, preflight,
                                          record_application, role_group, role_group_resolved, update_application)
from careeros.review.leads import dismiss_lead
from careeros.review.report import build_report, last_review_at
from careeros.review.server import make_handler, parse_channel

from tests.helpers import JOB_MATRIX, add_job, new_store
from tests.test_saved import POLICY, saved_page

FIXTURES = Path(__file__).parent / "fixtures"
APP = application_matrix()
FORM = re.compile(r'<form method="post" action="/(\w+)\?t=[^"]*"><input type="hidden" name="target" value="([^"]+)">'
                  r'<input type="hidden" name="key" value="([^"]+)">')


def forms(page: str) -> list[tuple[str, str, str]]:
    return FORM.findall(page)


class ApplicationLogicTests(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = new_store()
        add_job(self.store, "gh:acme:1")
        add_job(self.store, "gh:acme:2", variant_of="gh:acme:1")  # same requisition, other location
        add_job(self.store, "gh:acme:3", duplicate_of="gh:acme:1")
        add_job(self.store, "gh:acme:4", repost_of="gh:acme:2")  # the same role listed again later
        add_job(self.store, "gh:acme:9", title="Claims Analyst", title_norm="claims analyst")

    def record(self, job="gh:acme:1", channel="direct", key="k1", **kw):
        return record_application(self.store, APP, job, channel, policy=POLICY, key=key, **kw)

    def test_record_once_and_replay_a_double_click(self):
        app_id = self.record()
        self.assertEqual(self.record(), "replay")
        rows = self.store.all("SELECT * FROM applications")
        self.assertEqual([(r["application_id"], r["current_state"], r["state_sequence"]) for r in rows], [(app_id, "applied", 0)])
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM events WHERE event_type = 'application_recorded'")["c"], 1)

    def test_one_application_per_role_across_variants_and_duplicates(self):
        self.assertEqual(role_group(self.store, "gh:acme:2"), ["gh:acme:1", "gh:acme:2", "gh:acme:3", "gh:acme:4"])
        self.assertEqual(role_group(self.store, "gh:acme:9"), ["gh:acme:9"])
        self.record("gh:acme:4", "agency", key="k0", agency="Harbor Staffing")
        with self.assertRaisesRegex(ApplicationHold, "second channel can disqualify you"):  # agency sent you to the repost
            self.record()
        update_application(self.store, APP, self.store.one("SELECT application_id FROM applications")["application_id"],
                           "withdrawn", key="w0")
        with self.assertRaisesRegex(ApplicationHold, "applied to this role before"):  # the withdrawn one still counts
            self.record()
        self.record(reapply="the agency submission was withdrawn; applying directly")
        with self.assertRaisesRegex(ApplicationHold, "you applied to this role on") as caught:   # recorded twice by mistake?
            self.record("gh:acme:2", key="k2")
        self.assertEqual(caught.exception.check.codes("hold"), ["already_applied", "applied_before"])
        with self.assertRaisesRegex(ApplicationHold, "second channel can disqualify you"):   # never refused: explain it
            self.record("gh:acme:3", "agency", key="k3", agency="Harbor Staffing")
        self.record("gh:acme:3", "agency", key="k4", agency="Harbor Staffing", reapply="the agency re-submitted me",
                    confirm="I really was submitted through both")
        self.record("gh:acme:9", key="k5")  # a different role is unaffected
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 4)  # incl. the withdrawn repost application

    def test_every_finished_application_holds_a_new_one_until_you_give_a_reason(self):
        paths = {"rejected": ["rejected"], "withdrawn": ["withdrawn"], "closed": ["closed"],
                 "declined": ["screening", "interviewing", "offer", "declined"],
                 "accepted": ["screening", "interviewing", "offer", "accepted"]}
        for final, steps in paths.items():
            with self.subTest(final=final):
                store, _ = new_store()
                add_job(store, "gh:acme:1")
                add_job(store, "gh:acme:2", variant_of="gh:acme:1")
                app_id = record_application(store, APP, "gh:acme:1", "direct", policy=POLICY, key="a1")
                for n, state in enumerate(steps):
                    update_application(store, APP, app_id, state, key=f"s{n}")
                with self.assertRaisesRegex(ApplicationHold, "applied to this role before") as caught:
                    record_application(store, APP, "gh:acme:2", "direct", policy=POLICY, key="a2")
                self.assertEqual(caught.exception.check.codes("hold"), ["applied_before"])
                self.assertEqual(store.one("SELECT COUNT(*) c FROM applications")["c"], 1)  # nothing recorded
                new = record_application(store, APP, "gh:acme:2", "direct", policy=POLICY, key="a3", reapply="reposted six months later")
                meta = json.loads(store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded' "
                                            "AND entity_id = ?", (new,))["metadata_json"])
                self.assertEqual((meta["reapply_reason"], meta["evidence"], meta["preflight"]),
                                 ("reposted six months later", "owner_attested", "hold"))

    def test_a_responded_application_is_still_in_play_everywhere(self):
        self.assertIn("responded", OPEN_STATES)  # read from the workflow file, not a hand-written list
        app_id = self.record()
        update_application(self.store, APP, app_id, "responded", key="r1")
        with self.assertRaisesRegex(ApplicationHold, "second channel can disqualify you"):
            self.record("gh:acme:2", "agency", key="k2", agency="Harbor Staffing")
        with self.assertRaisesRegex(ApplicationHold, "you applied to this role on"):
            self.record("gh:acme:3", key="k3")
        self.assertTrue(_has_application(SimpleNamespace(store=self.store), "gh:acme:4"))  # NIGHT's right-to-represent check
        self.assertFalse(_has_application(SimpleNamespace(store=self.store), "gh:acme:9"))
        self.assertEqual(application_status(self.store, "gh:acme:4")["applied"]["current_state"], "responded")

    def test_a_void_application_needs_a_reason_stays_in_the_history_and_counts_nowhere(self):
        """ADR 0007, amendment of 2026-11-01: an application recorded by mistake is voided with a reason, never deleted;
        it no longer counts for "did I already apply?" (NIGHT, the card, recording: applying again needs no reason), the
        funnel or a possible duplicate elsewhere."""
        from careeros.review.applications import VOID
        from careeros.review.funnel import funnel
        app_id = self.record()
        with self.assertRaisesRegex(ValueError, "reason_required"):
            update_application(self.store, APP, app_id, VOID, key="v0")
        self.assertEqual(update_application(self.store, APP, app_id, VOID, key="v1", reason="clicked I applied on the wrong posting"),
                         "new")
        self.assertEqual(update_application(self.store, APP, app_id, VOID, key="v1", reason="clicked I applied on the wrong posting"),
                         "replay")
        row = self.store.one("SELECT * FROM applications WHERE application_id = ?", (app_id,))
        self.assertEqual((row["current_state"], row["state_sequence"]), (VOID, 1))   # kept, with its history
        self.assertEqual(self.store.one("SELECT reason_code FROM transitions WHERE entity_id = ?", (app_id,))["reason_code"],
                         "clicked I applied on the wrong posting")
        self.assertEqual(self.store.verify_job_consistency(APP, app_id, entity="application"), set())
        with self.assertRaisesRegex(ValueError, "terminal_state"):
            update_application(self.store, APP, app_id, "applied", key="v2")   # a void is final
        self.assertEqual((preflight(self.store, "gh:acme:2").applications, preflight(self.store, "gh:acme:2").codes()), ([], []))
        self.assertIsNone(application_status(self.store, "gh:acme:3")["applied"])
        self.assertFalse(_has_application(SimpleNamespace(store=self.store), "gh:acme:4"))   # NIGHT's right-to-represent check
        self.assertEqual(funnel(self.store, "channel"), [])   # no application in the funnel
        add_job(self.store, "gh:other:9", title="Claims Analyst", title_norm="claims analyst")   # same title, same employer
        void_elsewhere = self.record("gh:acme:9", key="k9")
        update_application(self.store, APP, void_elsewhere, VOID, key="v9", reason="wrong posting")
        self.assertEqual(preflight(self.store, "gh:other:9").possible, [])   # a void application is no possible duplicate
        new = self.record("gh:acme:2", key="k2")   # applying again after a void needs no re-application reason
        meta = json.loads(self.store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded' "
                                         "AND entity_id = ?", (new,))["metadata_json"])
        self.assertEqual((meta["preflight"], meta["checks"], meta["reapply_reason"]), ("clear", [], None))
        self.assertEqual([r["applied"] for r in funnel(self.store, "channel")], [1])
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 3)   # nothing deleted

    def test_void_is_reachable_only_before_anyone_at_the_employer_answered(self):
        from careeros.review.applications import VOID
        voidable = {"applied": [], "acknowledged": ["acknowledged"], "ghosted": ["ghosted"]}
        answered = {"responded": ["responded"], "screening": ["screening"], "rejected": ["rejected"], "withdrawn": ["withdrawn"]}
        for state, steps in {**voidable, **answered}.items():
            with self.subTest(state=state):
                store, _ = new_store()
                add_job(store, "gh:acme:1")
                app_id = record_application(store, APP, "gh:acme:1", "direct", policy=POLICY, key="a1")
                for n, step in enumerate(steps):
                    update_application(store, APP, app_id, step, key=f"s{n}")
                if state in voidable:
                    update_application(store, APP, app_id, VOID, key="v", reason="recorded by mistake")
                    self.assertEqual(store.one("SELECT current_state FROM applications")["current_state"], VOID)
                else:
                    with self.assertRaisesRegex(ValueError, "transition_not_allowed|terminal_state"):
                        update_application(store, APP, app_id, VOID, key="v", reason="recorded by mistake")
        self.assertNotIn(VOID, OPEN_STATES)
        self.assertEqual(APP.category(VOID), "cancelled")

    def test_void_is_refused_once_an_employer_answered_whatever_the_state_now(self):
        """Codex CR-2026-11-01-OBS-REVIEW, F2: the whole history decides. An application that was ever in an
        employer-answer stage is never voided, even once ghosted (and acknowledged after that): it keeps counting
        everywhere an application counts. One that never was can still be voided, and then carries no answer flag."""
        from careeros.analytics.export import build
        from careeros.review.applications import VOID
        refused = {"responded, ghosted": ["responded", "ghosted"],
                   "interviewing, ghosted, acknowledged": ["interviewing", "ghosted", "acknowledged"],
                   "acknowledged, screening, ghosted": ["acknowledged", "screening", "ghosted"],
                   "final interview, ghosted, responded, ghosted": ["interviewing", "final_interview", "ghosted",
                                                                    "responded", "ghosted"]}
        allowed = {"applied": [], "ghosted": ["ghosted"], "acknowledged, ghosted": ["acknowledged", "ghosted"],
                   "ghosted, acknowledged": ["ghosted", "acknowledged"]}
        for name, steps in {**refused, **allowed}.items():
            with self.subTest(path=name):
                store, clock = new_store()
                add_job(store, "gh:acme:1")
                add_job(store, "gh:acme:2", variant_of="gh:acme:1")
                app_id = record_application(store, APP, "gh:acme:1", "agency", agency="Synthetic Agency", policy=POLICY, key="a1")
                for n, step in enumerate(steps):
                    clock.advance(seconds=1)
                    update_application(store, APP, app_id, step, key=f"s{n}")
                if name in refused:
                    with self.assertRaisesRegex(ValueError, r"^employer_answered: this application was \w"):
                        update_application(store, APP, app_id, VOID, key="v", reason="recorded by mistake")
                    row = store.one("SELECT current_state, state_sequence FROM applications")
                    self.assertEqual(tuple(row), (steps[-1], len(steps)))   # nothing changed
                    self.assertEqual(preflight(store, "gh:acme:2", "direct").codes(), ["right_to_represent_conflict"])
                    self.assertTrue(_has_application(SimpleNamespace(store=store), "gh:acme:2"))   # NIGHT still sees it
                else:
                    update_application(store, APP, app_id, VOID, key="v", reason="recorded by mistake")
                    self.assertEqual(store.one("SELECT current_state FROM applications")["current_state"], VOID)
                    row = build(store, POLICY)["fact_application"][0]   # a void row never carries an answer flag
                    self.assertEqual([row[c] for c in ("got_answer", "reached_screening", "reached_interview",
                                                       "reached_offer", "is_hired")], [0, 0, 0, 0, 0])

    def test_the_states_before_an_answer_are_read_from_the_workflow(self):
        """F2: the states void is allowed from are the workflow's own (its void edges), and none of them is a stage the
        funnel or the export counts as an answer."""
        from careeros.analytics.export import ANSWERED
        from careeros.review.applications import VOID, unanswered_states
        from careeros.review.funnel import REACHED
        before = unanswered_states(APP)
        self.assertEqual(before, {frm for (frm, to) in APP.edges if to == VOID})
        self.assertEqual(before, {"applied", "acknowledged", "ghosted"})
        self.assertEqual(before & (ANSWERED | REACHED["responded"]), set())
        self.assertEqual(ANSWERED - set(APP.states), set())   # every answer stage is a workflow state outside `before`

    def test_app_status_void_on_the_command_line_needs_a_reason(self):
        import io
        from contextlib import redirect_stdout

        from careeros.cli import cmd_app_status
        app_id = self.record()
        ctx = SimpleNamespace(store=self.store)
        with self.assertRaisesRegex(SystemExit, "reason_required"):
            cmd_app_status(ctx, {}, SimpleNamespace(application_id=app_id, state="void", notes=None, reason=None))
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_app_status(ctx, {}, SimpleNamespace(application_id=app_id, state="void", notes=None, reason="wrong posting"))
        self.assertEqual(out.getvalue(), f"{app_id} -> void\n")

    def test_a_possible_duplicate_or_the_same_title_elsewhere_holds_until_confirmed(self):
        add_job(self.store, "gh:acme:20", title="Pricing Analyst", title_norm="pricing analyst", location_raw="Boston")
        add_job(self.store, "gh:acme:21", title="Pricing Analyst", title_norm="pricing analyst", location_raw="New York")
        add_job(self.store, "gh:acme:30", title="Claims Specialist", title_norm="claims specialist")
        add_job(self.store, "gh:acme:31", title="Claims Specialist II", title_norm="claims specialist ii")
        add_job(self.store, "gh:acme:40", title="Data Analyst", title_norm="data analyst")
        add_job(self.store, "gh:acme:41", title="Reporting Analyst", title_norm="reporting analyst")
        link(self.store, "gh:acme:31", "gh:acme:30", "possible_duplicate", "similar text")
        link(self.store, "gh:acme:41", "gh:acme:40", "same_opening", "one vacancy")
        for applied, other in (("gh:acme:20", "gh:acme:21"), ("gh:acme:30", "gh:acme:31")):
            self.record(applied, key=f"a{applied}")
            self.assertEqual(application_status(self.store, other)["possibly_applied"], [applied])  # the card says so too
            with self.assertRaisesRegex(ApplicationHold, "may be the same role"):
                self.record(other, key=f"b{other}")
            self.record(other, key=f"c{other}", confirm="different team and requisition per the recruiter")
        self.record("gh:acme:40", key="d1")
        with self.assertRaisesRegex(ApplicationHold, "you applied to this role on"):  # a same-opening link is the same role
            self.record("gh:acme:41", key="d2")

    def test_same_title_with_a_different_requisition_is_shown_not_held(self):
        add_job(self.store, "gh:acme:50", title="Pricing Analyst", title_norm="pricing analyst", requisition_id="R-1")
        add_job(self.store, "gh:acme:51", title="Pricing Analyst", title_norm="pricing analyst", requisition_id="R-2")
        self.record("gh:acme:50", key="r1")
        check = preflight(self.store, "gh:acme:51", "direct")
        self.assertEqual((check.decision, check.codes("warn")), ("clear", ["same_title_other_requisition"]))
        self.record("gh:acme:51", key="r2")

    def test_the_cli_says_how_to_give_a_reason(self):
        update_application(self.store, APP, self.record(), "rejected", key="s1")
        args = argparse.Namespace(job_id="gh:acme:2", channel="direct", agency=None, notes=None, reapply=None, confirm=None)
        ctx = SimpleNamespace(store=self.store, policy=POLICY)
        with self.assertRaises(SystemExit) as caught:
            cmd_applied(ctx, {}, args)
        self.assertIn("--reapply", str(caught.exception.code))
        cmd_applied(ctx, {}, argparse.Namespace(**{**vars(args), "reapply": "reposted with a new manager"}))
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 2)

    def test_an_unfinished_duplicate_search_is_never_clearance(self):
        add_job(self.store, "gh:big:0", title="Ops Analyst", title_norm="ops analyst")
        for n in range(1, 205):
            add_job(self.store, f"gh:big:{n}", title="Ops Analyst", title_norm="ops analyst", variant_of="gh:big:0")
        members, complete = role_group_resolved(self.store, "gh:big:7")
        self.assertFalse(complete)
        with self.assertRaisesRegex(ApplicationHold, "check by hand"):
            self.record("gh:big:7", key="e1")
        self.assertTrue(_has_application(SimpleNamespace(store=self.store), "gh:big:7"))  # NIGHT treats it as a conflict
        self.record("gh:big:7", key="e2", confirm="checked the employer portal: no earlier application")

    def test_a_submission_hold_needs_your_explanation_and_comes_from_policy_not_a_stored_score(self):
        add_job(self.store, "gh:ng:1", employer_name="NorthgateRx", parent="NorthgateRx")   # a held family, by its alias
        with self.assertRaisesRegex(ApplicationHold, "submission is held") as caught:
            self.record("gh:ng:1", key="h1")
        self.assertEqual(caught.exception.check.codes("hold"), ["submission_held"])
        app_id = self.record("gh:ng:1", key="h2", confirm="applied before the hold existed")
        meta = json.loads(self.store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded' AND entity_id = ?",
                                         (app_id,))["metadata_json"])
        self.assertEqual((meta["submission"]["held"], meta["submission"]["recorded_while_held"]), (["gate-held-parents"], True))
        self.store.writer("qualify").insert("scores", {
            "score_id": "s:hold", "job_id": "gh:acme:9", "at": self.clock.now(), "policy_fingerprint": "p",
            "profile_fingerprint": "p", "tier": "near_miss", "interview_band": "Low", "components_json": "{}", "state": "HOLD"})
        self.record("gh:acme:9", key="h3")   # a stale stored HOLD state means nothing: holds are read from policy
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM submission_releases")["c"], 0)   # recording never releases

    def test_input_validation(self):
        with self.assertRaisesRegex(ValueError, "unknown_channel"):
            self.record(channel="carrier pigeon")
        with self.assertRaisesRegex(ValueError, "agency_name_required"):
            self.record(channel="agency", agency="  ")
        with self.assertRaisesRegex(ValueError, "unknown_job"):
            self.record("gh:nope:1")
        self.assertEqual(parse_channel("agency:Harbor Staffing", "ignored"), ("agency", "Harbor Staffing"))
        self.assertEqual(parse_channel("agency", " TEKsystems "), ("agency", "TEKsystems"))
        self.assertEqual(parse_channel("direct", "typed by mistake"), ("direct", None))

    def test_status_follows_the_matrix_and_history_stays_consistent(self):
        app_id = self.record()
        with self.assertRaisesRegex(ValueError, "transition_not_allowed"):
            update_application(self.store, APP, app_id, "offer", key="s0")
        for n, state in enumerate(("screening", "interviewing", "offer", "accepted")):
            self.clock.advance(days=1)
            update_application(self.store, APP, app_id, state, notes=f"step {n}", key=f"s{n + 1}")
        self.assertEqual(update_application(self.store, APP, app_id, "accepted", notes="step 3", key="s4"), "replay")
        with self.assertRaisesRegex(ValueError, "terminal_state"):
            update_application(self.store, APP, app_id, "withdrawn", key="s9")
        row = self.store.one("SELECT current_state, state_sequence FROM applications WHERE application_id = ?", (app_id,))
        self.assertEqual(tuple(row), ("accepted", 4))
        self.assertEqual(self.store.verify_job_consistency(APP, app_id, entity="application"), set())
        self.assertEqual(self.store.verify_event_chain(), [])

    def test_state_and_rows_cannot_be_changed_around_the_workflow(self):
        app_id = self.record()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute("UPDATE applications SET current_state = 'offer', state_sequence = 1 WHERE application_id = ?", (app_id,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute("DELETE FROM applications")
        with self.assertRaises(PermissionDenied):
            self.store.writer("review").update("applications", {"notes": "x"}, "application_id = ?", (app_id,))
        with self.assertRaises(PermissionDenied):
            self.store.writer("tracker").update("applications", {"channel": "referral"}, "application_id = ?", (app_id,))
        with self.assertRaises(PermissionDenied):
            self.store.writer("tracker").update("applications", {"current_state": "offer"}, "application_id = ?", (app_id,))
        self.store.writer("tracker").update("applications", {"notes": "recruiter: Pat"}, "application_id = ?", (app_id,))


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {"inbox": root / "inbox", "reports": root / "reports"}
        self.paths["inbox"].mkdir()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.profile = load_profile(FIXTURES / "synthetic_profile.yaml")
        self.ctx = Context(self.store, POLICY, self.profile, JOB_MATRIX, root / "snapshots")
        (self.paths["inbox"] / "a.html").write_text(saved_page(org="Synthetic Health"), encoding="utf-8")
        cmd_import(self.ctx, self.paths, argparse.Namespace())
        self.job_id = self.store.one("SELECT job_id FROM jobs WHERE current_state = 'scored'")["job_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, **kw):
        return build_report(self.store, self.profile, POLICY, app_matrix=APP, **kw)

    def test_applications_section_offers_only_allowed_next_states_and_nudges_after_14_days(self):
        app_id = record_application(self.store, APP, self.job_id, "agency", agency="Harbor Staffing", policy=POLICY, key="k1")
        page = build_report(self.store, self.profile, POLICY, token="t", form_keys={}, app_matrix=APP)
        self.assertIn("Your applications (1)", page)
        self.assertIn("via agency (Harbor Staffing)", page)
        self.assertIn('<option value="screening">', page)
        self.assertNotIn('<option value="offer">', page)
        self.assertNotIn("no response in", page)
        self.clock.advance(days=15)
        self.assertIn("no response in 15 days", self.page())
        update_application(self.store, APP, app_id, "screening", key="s1")
        self.assertNotIn("no response in", self.page())

    def test_new_since_last_review_and_stale_flag(self):
        self.assertNotIn("New since your last review", self.page())
        self.assertIn("New since your last review (1)", self.page(since="2026-10-01T00:00:00.000Z"))
        self.assertIn("New since your last review (0)", self.page(since="2026-10-11T13:00:00.000Z"))
        self.assertNotIn("may be stale", self.page())
        self.clock.advance(days=70)
        self.assertIn("may be stale", self.page())

    def test_last_review_is_read_from_the_event_log(self):
        self.assertIsNone(last_review_at(self.store))
        self.store.writer("review").event("review_opened", outcome="informational", entity_type="review", entity_id="daily",
                                          actor_type="human", actor_id="user")
        self.assertEqual(last_review_at(self.store), "2026-10-11T12:00:00.000Z")

    def test_review_page_forms_record_applications_status_and_lead_dismissals(self):
        self.store.writer("discovery").insert("leads", {
            "lead_id": "lead:1", "url": "https://www.linkedin.com/jobs/view/1", "site": "linkedin", "title": "Claims Analyst",
            "title_norm": "claims analyst", "family": "pbm_claims_operations", "first_seen_at": self.clock.now(), "status": "new"})
        token = "tracker-test-token"
        server = HTTPServer(("127.0.0.1", 0), make_handler(self.store, self.profile, POLICY, token,
                                                          Path(self.tmp.name) / "writer.lock", {}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            found = forms(httpx.get(f"{base}/?t={token}").text)
            label_key = next(k for a, t, k in found if a == "label")
            apply_target, apply_key = next((t, k) for a, t, k in found if a == "apply")
            # a key is bound to its action: a label form's key cannot record an application
            self.assertEqual(httpx.post(f"{base}/apply?t={token}", data={"target": apply_target, "key": label_key,
                                                                          "channel": "direct"}).status_code, 400)
            form = {"target": apply_target, "key": apply_key, "channel": "agency:Harbor Staffing", "agency": ""}
            ask = httpx.post(f"{base}/apply?t={token}", data=form)   # a confirmation naming the posting first (OPP-16)
            self.assertEqual(ask.status_code, 200)
            self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 0)
            confirm_key = next(k for a, t, k in forms(ask.text) if (a, t) == ("confirm_apply", apply_target))
            form = {"target": apply_target, "key": confirm_key, "channel": "agency:Harbor Staffing", "agency": "", "from": "card"}
            self.assertEqual(httpx.post(f"{base}/confirm_apply?t={token}", data=form).status_code, 303)
            self.assertEqual(httpx.post(f"{base}/confirm_apply?t={token}", data=form).status_code, 303)  # double click
            apps = self.store.all("SELECT * FROM applications")
            self.assertEqual([(a["job_id"], a["channel"], a["agency"]) for a in apps], [(self.job_id, "agency", "Harbor Staffing")])

            found = forms(httpx.get(f"{base}/?t={token}").text)
            self.assertFalse([1 for a, t, k in found if a == "apply" and t == self.job_id])  # already applied: no button
            status_target, status_key = next((t, k) for a, t, k in found if a == "status")
            reply = httpx.post(f"{base}/status?t={token}", data={"target": status_target, "key": status_key, "state": "screening"})
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(self.store.one("SELECT current_state FROM applications")["current_state"], "screening")

            lead_target, lead_key = next((t, k) for a, t, k in found if a == "lead")
            self.assertEqual(httpx.post(f"{base}/lead?t={token}", data={"target": lead_target, "key": lead_key}).status_code, 303)
            self.assertEqual(self.store.one("SELECT status FROM leads")["status"], "dismissed")
            self.assertNotIn("Leads from your alert emails", httpx.get(f"{base}/?t={token}").text)
            self.assertEqual(httpx.post(f"{base}/nope?t={token}", data={}).status_code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_the_review_page_asks_for_a_reason_before_recording_a_re_application(self):
        app_id = record_application(self.store, APP, self.job_id, "direct", policy=POLICY, key="k1")
        update_application(self.store, APP, app_id, "rejected", key="s1")
        token = "hold-test-token"
        server = HTTPServer(("127.0.0.1", 0), make_handler(self.store, self.profile, POLICY, token,
                                                          Path(self.tmp.name) / "writer.lock", {}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            page = httpx.get(f"{base}/?t={token}").text
            self.assertIn("Before applying: you applied to this role before", page)
            target, key = next((t, k) for a, t, k in forms(page) if a == "apply" and t == self.job_id)  # offered again
            ask = httpx.post(f"{base}/apply?t={token}", data={"target": target, "key": key, "channel": "direct", "agency": ""})
            self.assertEqual(ask.status_code, 200)   # the confirmation naming the posting (OPP-16)
            key = next(k for a, t, k in forms(ask.text) if a == "confirm_apply")
            reply = httpx.post(f"{base}/confirm_apply?t={token}", data={"target": target, "key": key, "channel": "direct", "agency": ""})
            self.assertEqual(reply.status_code, 409)
            self.assertIn("Not recorded yet", reply.text)
            self.assertIn('name="reapply_reason"', reply.text)
            self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 1)
            target, key = next((t, k) for a, t, k in forms(reply.text) if a == "confirm_apply")
            reply = httpx.post(f"{base}/confirm_apply?t={token}", data={"target": target, "key": key, "channel": "direct", "agency": "",
                                                                         "reapply_reason": "new hiring manager; reposted"})
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_dismiss_lead_is_idempotent(self):
        self.store.writer("discovery").insert("leads", {
            "lead_id": "lead:2", "url": "https://example.com/j", "site": "other", "title": "Claims Analyst",
            "title_norm": "claims analyst", "first_seen_at": self.clock.now(), "status": "new"})
        self.assertEqual(dismiss_lead(self.store, "lead:2", key="d1"), "new")
        self.assertEqual(dismiss_lead(self.store, "lead:2", key="d1"), "replay")
        with self.assertRaisesRegex(ValueError, "unknown_lead"):
            dismiss_lead(self.store, "lead:404", key="d2")


if __name__ == "__main__":
    unittest.main()
