"""Shared intake visibility: synthetic links only, in-memory stores, no network or real vault access."""
import contextlib
import html
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from careeros import cli
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery.alerts import AlertEmail, Link, lead_id
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile
from careeros.review.report import _leads_section, build_report
from careeros.review.server import make_handler

from tests.helpers import JOB_MATRIX
from tests.test_applications import forms
from tests.test_saved import POLICY


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"no client access expected: {name}")


class SharedIntakeVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock("2026-11-06T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.addCleanup(self.store.close)
        self.profile = load_profile(Path(__file__).parent / "fixtures" / "synthetic_profile.yaml")
        self.ctx = Context(self.store, POLICY, self.profile, JOB_MATRIX, None)

    def arrive(self, ident=1, *, title="Service Desk Supervisor", profile="primary", url=None):
        link = Link(url or f"https://www.linkedin.com/jobs/view/900000{ident:04d}", "linkedin", title,
                    f"Synthetic Support {ident} · Remote (US)", True)
        outcomes = []
        counts = cli._record_alert(self.ctx, AlertEmail("GitHub inbox (synthetic)", "jobs", None, [link]),
                                   NoNetwork(), outcomes, profile=profile)
        self.assertEqual(counts["lead_errors"], 0)
        return lead_id(link.url), counts, [outcome for _, outcome in outcomes]

    def seed(self, ident, *, family=None, status="new", profile="primary", **extra):
        row = {"lead_id": f"lead:{ident}", "url": f"https://example.invalid/jobs/{ident}", "site": "synthetic",
               "title": f"Synthetic role {ident}", "title_norm": f"synthetic role {ident}", "family": family,
               "context": "Synthetic Support · Remote (US)", "sender": "synthetic alert", "subject": None,
               "received_at": None, "first_seen_at": self.clock.now(), "status": status, "job_id": None,
               "profile": profile, **extra}
        self.store.writer("discovery").insert("leads", row)
        self.clock.advance(seconds=1)
        return row

    def section(self, token=None, form_keys=None):
        return "".join(_leads_section(self.store, token, {} if form_keys is None else form_keys))

    def test_supervisor_uses_existing_intake_and_is_visible_without_being_graded(self):
        lid, counts, outcomes = self.arrive()
        lead = self.store.one("SELECT * FROM leads WHERE lead_id = ?", (lid,))
        self.assertEqual((lead["family"], lead["status"], lead["profile"], lead["job_id"]), (None, "new", "primary", None))
        self.assertEqual((counts["lead_other_family"], outcomes), (1, ["outside_target_families"]))
        keys = {}
        page = build_report(self.store, self.profile, POLICY, token="synthetic-token", form_keys=keys)
        self.assertIn("Needs role review / Not graded (1)", page)
        self.assertIn("Service Desk Supervisor", page)
        self.assertIn("eligibility and qualification have not been assessed", page)
        section = self.section(token="synthetic-token")
        self.assertIn('<span>PRIMARY</span>', section)
        self.assertIn('<span class="flag">Not graded</span>', section)
        self.assertIn('<div id="leads"></div>', section)   # the existing dismiss redirect remains usable
        self.assertEqual({(action, target) for action, target, _ in forms(section)}, {("lead", lid)})
        for table in ("jobs", "scores", "gate_results", "night_results"):
            self.assertEqual(self.store.one(f"SELECT COUNT(*) c FROM {table}")["c"], 0)

    def test_known_lead_keeps_its_section_and_family(self):
        known_id, counts, outcomes = self.arrive(title="Claims Analyst")
        self.arrive(2)
        known = self.store.one("SELECT * FROM leads WHERE lead_id = ?", (known_id,))
        self.assertIsNotNone(known["family"])
        self.assertEqual((counts["lead_new"], outcomes), (1, ["lead"]))
        known_section, unknown_section = self.section().split('<h2 id="leads-role-review">', 1)
        self.assertIn('id="leads">Leads from alerts and recommendations (1)', known_section)
        self.assertIn("Claims Analyst", known_section)
        self.assertIn(f'family: {known["family"]}', known_section)
        self.assertNotIn("Service Desk Supervisor", known_section)
        self.assertNotIn("Not graded", known_section)
        self.assertIn("Service Desk Supervisor", unknown_section)
        self.assertNotIn("Claims Analyst", unknown_section)

    def test_targeted_supervisor_is_a_recognized_but_ungraded_alert_lead(self):
        lid, first, outcomes = self.arrive(title="Customer Service Supervisor")
        _, replay, replay_outcomes = self.arrive(title="Customer Service Supervisor")
        row = self.store.one("SELECT family, status, job_id FROM leads WHERE lead_id = ?", (lid,))
        self.assertEqual(tuple(row), ("service_operations_supervision", "new", None))
        self.assertEqual((first["lead_new"], outcomes), (1, ["lead"]))
        self.assertEqual((replay["lead_seen_before"], replay_outcomes), (1, ["seen_before"]))
        self.assertIn("Leads from alerts and recommendations (1)", self.section())
        for table in ("jobs", "scores", "gate_results", "night_results"):
            self.assertEqual(self.store.one(f"SELECT COUNT(*) c FROM {table}")["c"], 0)

    def test_only_open_leads_appear_in_either_section(self):
        for family in (None, "healthcare_bsa"):
            for status in ("new", "no_match", "closed", "dismissed", "ingested"):
                self.seed(f"{family}-{status}", family=family, status=status, status_reason=f"reason {status}")
        page = self.section()
        self.assertIn("Leads from alerts and recommendations (2)", page)
        self.assertIn("Needs role review / Not graded (2)", page)
        for family in (None, "healthcare_bsa"):
            for status in ("new", "no_match", "closed", "dismissed", "ingested"):
                title = f"Synthetic role {family}-{status}"
                if status in ("new", "no_match"):
                    self.assertIn(title, page)
                else:
                    self.assertNotIn(title, page)
        self.assertEqual(page.count("no match yet"), 2)
        self.assertEqual(page.count("reason no_match"), 2)

    def test_untrusted_fields_are_escaped_and_unsafe_urls_are_not_links(self):
        values = {"title": '<script>alert("title")</script>', "context": '<img src=x onerror="context">',
                  "site": '<site title="site">', "sender": '<sender title="sender">',
                  "status_reason": '<reason title="reason">', "fetch_failure": '<failure title="failure">',
                  "url": 'https://example.invalid/jobs/1?x=" onmouseover="bad&tag=<url>'}
        self.seed("untrusted", status="no_match", **values)
        self.seed("unsafe", title="Unsafe URL lead", url="javascript:alert(1)")
        page = self.section(token='token"<untrusted>')
        for name, value in values.items():
            self.assertIn(html.escape(value, quote=True), page, name)
            self.assertNotIn(value, page, name)
        self.assertNotIn('href="javascript:', page)
        self.assertNotIn("<script>", page)
        self.assertIn("Unsafe URL lead", page)
        self.assertIn('t=token&quot;&lt;untrusted&gt;', page)

    def test_unknown_overflow_cannot_displace_known_leads_and_all_are_listable(self):
        for number in range(61):
            self.seed(f"known-{number:03d}", family="healthcare_bsa")
        for number in range(65):
            self.seed(f"unknown-{number:03d}")
        keys = {}
        page = self.section(token="synthetic-token", form_keys=keys)
        known, unknown = page.split('<h2 id="leads-role-review">', 1)
        self.assertEqual(known.count('<div class="card">'), 60)
        self.assertEqual(unknown.count('<div class="card">'), 60)
        self.assertIn("Leads from alerts and recommendations (61)", known)
        self.assertIn("Needs role review / Not graded (65)", unknown)
        self.assertIn("Showing 60 of 65 open leads needing role review", unknown)
        self.assertIn("<code>careeros leads list</code>", unknown)
        self.assertIn("Synthetic role known-001", known)
        self.assertNotIn("Synthetic role known-000", known)
        self.assertIn("Synthetic role unknown-005", unknown)
        self.assertNotIn("Synthetic role unknown-004", unknown)
        self.assertEqual(len(keys), 120)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.cmd_leads(self.ctx, None, SimpleNamespace(action="list", all=False))
        self.assertIn("Synthetic role unknown-000", output.getvalue())
        self.assertIn("126 lead(s)", output.getvalue())

    def test_duplicate_supervisor_intake_adds_no_score_or_observation(self):
        for profile in ("primary", "night"):
            with self.subTest(profile=profile):
                number = 11 if profile == "primary" else 12
                # Even a fetchable board URL stays a retained, ungraded lead when no family matches.
                url = f"https://job-boards.greenhouse.io/synthetic/jobs/{number}"
                lid, first, _ = self.arrive(number, title="Service Desk Supervisor", profile=profile, url=url)
                _, replay, outcomes = self.arrive(number, title="Service Desk Supervisor", profile=profile, url=url)
                self.assertEqual(first["lead_other_family"], 1)
                self.assertEqual((replay["lead_seen_before"], outcomes), (1, ["seen_before"]))
                row = self.store.one("SELECT family, profile, job_id FROM leads WHERE lead_id = ?", (lid,))
                self.assertEqual(tuple(row), (None, profile, None))
                self.assertEqual(self.store.one("SELECT COUNT(*) c FROM observations WHERE lead_id = ?", (lid,))["c"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 2)
        for table in ("jobs", "scores", "gate_results", "night_results"):
            self.assertEqual(self.store.one(f"SELECT COUNT(*) c FROM {table}")["c"], 0)
        self.assertIn('<span>PRIMARY</span>', self.section())
        self.assertIn('<span class="flag">NIGHT</span>', self.section())

    def test_dismiss_uses_existing_bound_form_and_no_submission_route(self):
        target, _, _ = self.arrive()
        other, _, _ = self.arrive(2, profile="night")
        self.assertEqual(forms(self.section()), [])   # a saved/static report offers no mutation form
        with tempfile.TemporaryDirectory(prefix="careeros-shared-intake-") as tmp:
            handler = object.__new__(make_handler(self.store, self.profile, POLICY, "synthetic-token", Path(tmp) / "writer.lock", {}))
            replies = []
            handler._send = lambda status, body, extra=None: replies.append((status, body, extra))

            def request(action=None, *, token="synthetic-token", **data):
                handler.path = f"/{action or ''}?t={token}"
                body = urlencode(data).encode("utf-8")
                handler.headers = {"Host": "127.0.0.1", "Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler.do_GET() if action is None else handler.do_POST()
                return replies[-1]

            status, page, _ = request()
            self.assertEqual(status, 200)
            key = next(key for action, shown_target, key in forms(page) if (action, shown_target) == ("lead", target))
            self.assertEqual(request("lead", target=target, key=key, token="wrong")[0], 403)
            self.assertEqual(request("lead", target=other, key=key)[0], 400)
            self.assertEqual(request("apply", target=target, key=key, channel="direct")[0], 400)
            self.assertEqual(self.store.one("SELECT status FROM leads WHERE lead_id = ?", (target,))["status"], "new")
            self.assertEqual(request("lead", target=target, key=key)[0], 303)
            self.assertEqual(request("lead", target=target, key=key)[0], 303)   # double click is idempotent
            self.assertEqual(self.store.one("SELECT status FROM leads WHERE lead_id = ?", (target,))["status"], "dismissed")
            self.assertEqual(self.store.one("SELECT status FROM leads WHERE lead_id = ?", (other,))["status"], "new")
            self.assertEqual(self.store.one("SELECT COUNT(*) c FROM events WHERE event_type = 'lead_dismissed'")["c"], 1)
            self.assertNotIn("Synthetic Support 1", self.section())
            self.assertIn("Synthetic Support 2", self.section())
            self.assertEqual(self.store.one("SELECT COUNT(*) c FROM applications")["c"], 0)


if __name__ == "__main__":
    unittest.main()
