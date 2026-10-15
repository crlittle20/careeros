"""Application tracker, email leads on the review page, new-since-last-review and stale flags. Synthetic data only."""
import argparse
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

import httpx

from careeros.cli import cmd_import
from careeros.core.clock import FixedClock
from careeros.db.store import PermissionDenied, open_store
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile
from careeros.review.applications import (ApplicationConflict, application_matrix, record_application, role_group,
                                          update_application)
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
        return record_application(self.store, APP, job, channel, key=key, **kw)

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
        with self.assertRaisesRegex(ApplicationConflict, "right_to_represent_conflict"):  # agency sent you to the repost
            self.record()
        update_application(self.store, APP, self.store.one("SELECT application_id FROM applications")["application_id"],
                           "withdrawn", key="w0")
        self.record()
        with self.assertRaisesRegex(ApplicationConflict, "already_applied"):
            self.record("gh:acme:2", key="k2")
        with self.assertRaisesRegex(ApplicationConflict, "right_to_represent_conflict"):
            self.record("gh:acme:3", "agency", key="k3", agency="Harbor Staffing")
        self.record("gh:acme:3", "agency", key="k4", agency="Harbor Staffing", allow_second_channel=True)
        self.record("gh:acme:9", key="k5")  # a different role is unaffected
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 4)  # incl. the withdrawn repost application

    def test_a_closed_application_does_not_block_reapplying(self):
        app_id = self.record()
        update_application(self.store, APP, app_id, "rejected", key="s1")
        self.record("gh:acme:2", key="k2")

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
        app_id = record_application(self.store, APP, self.job_id, "agency", agency="Harbor Staffing", key="k1")
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
            self.assertEqual(httpx.post(f"{base}/apply?t={token}", data=form).status_code, 303)
            self.assertEqual(httpx.post(f"{base}/apply?t={token}", data=form).status_code, 303)  # double click
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
