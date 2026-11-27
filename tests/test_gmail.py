"""Gmail connector: sign-in checks, token handling and the alert pull. Google is simulated; no real account is used."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from careeros.cli import _gmail_pull
from careeros.core.clock import FixedClock
from careeros.core.credentials import MemoryCredentials
from careeros.db.store import open_store
from careeros.discovery import gmail
from careeros.discovery.http import HttpClient
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile

from tests.helpers import JOB_MATRIX
from tests.test_alerts import AG_1, AG_2, alert_email, agency_card, agency_component, agency_email
from tests.test_saved import POLICY

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT = {"client_id": "synthetic-client.apps.example.com", "client_secret": "synthetic-secret"}  # release-scan: fake (a test placeholder, not a credential)


def legacy_lead(store, url, title="Claims Analyst", context="Synthetic Employer Inc · Remote"):
    """An ordinary lead at a link that names no posting, as stored before the owner's D192 (such a card is a notice now,
    never a lead: part 3); leads stored before then stay as they were."""
    from urllib.parse import urlsplit

    from careeros.discovery.alerts import lead_id, site_of
    from careeros.normalize.posting import classify_family, norm
    store.writer("discovery").insert("leads", {
        "lead_id": lead_id(url), "url": url, "site": site_of(urlsplit(url).netloc), "title": title, "title_norm": norm(title),
        "family": classify_family(norm(title), POLICY.get("families", default={})), "context": context,
        "sender": "LinkedIn Job Alerts", "first_seen_at": store.clock.now(), "status": "new", "profile": "primary"})


class FakeGoogle:
    def __init__(self, scope=gmail.SCOPE, refresh_ok=True):
        self.scope, self.refresh_ok, self.calls = scope, refresh_ok, []
        self.messages = {"m1": alert_email(), "m2": alert_email()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["one-time-code"] and form["code_verifier"][0]
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1", "scope": self.scope})
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "a2", "scope": self.scope})
        if request.headers.get("Authorization") not in ("Bearer a1", "Bearer a2"):
            return httpx.Response(401)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "candidate@example.com"})
        if request.url.path.endswith("/messages"):
            if request.url.params.get("pageToken") == "p2":
                return httpx.Response(200, json={"messages": [{"id": "m2"}]})
            return httpx.Response(200, json={"messages": [{"id": "m1"}], "nextPageToken": "p2"})
        message_id = request.url.path.rsplit("/", 1)[-1]
        raw = base64.urlsafe_b64encode(self.messages[message_id]).decode().rstrip("=")
        return httpx.Response(200, json={"id": message_id, "raw": raw})


class ListedGoogle(FakeGoogle):
    """Lists exactly its own messages, in one page, whatever the query (the query is kept in `queries`)."""

    def __init__(self, messages=None, listed=None, **kwargs):
        super().__init__(**kwargs)
        self.messages = dict(messages or {})
        self.listed = listed   # the ids the search lists; None: every message
        self.queries: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages") and request.headers.get("Authorization") in ("Bearer a1", "Bearer a2"):
            self.queries.append(request.url.params.get("q"))
            ids = self.messages if self.listed is None else self.listed
            return httpx.Response(200, json={"messages": [{"id": m} for m in ids]})
        message_id = request.url.path.rsplit("/", 1)[-1]
        if "/messages/" in request.url.path and message_id not in self.messages:
            return httpx.Response(404)
        return super().handler(request)


class PagedGoogle(FakeGoogle):
    """Lists `count` small alert emails page by page, as the Gmail API does (maxResults, nextPageToken)."""

    def __init__(self, count, **kwargs):
        super().__init__(**kwargs)
        self.messages = {f"p{n:04d}": alert_email("<p>No jobs today.</p>") for n in range(count)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages") and request.headers.get("Authorization") in ("Bearer a1", "Bearer a2"):
            start, size = int(request.url.params.get("pageToken") or 0), int(request.url.params.get("maxResults"))
            ids = list(self.messages)[start:start + size]
            body = {"messages": [{"id": m} for m in ids]}
            if start + size < len(self.messages):
                body["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=body)
        return super().handler(request)


class Scripted:
    """A Gmail reader with set answers: `listed` for the search, and per id the raw bytes or an exception to raise."""

    def __init__(self, listed, raws):
        self.listed, self.raws = list(listed), dict(raws)

    def search(self, query, limit=300):
        return list(self.listed)

    def raw(self, message_id):
        value = self.raws[message_id]
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        pass


def browser(answer_state=None, extra=None):
    """Simulates you approving access: Google redirects your browser to the local listener."""
    def open_browser(url):
        query = parse_qs(urlsplit(url).query)
        assert query["scope"] == [gmail.SCOPE] and query["code_challenge_method"] == ["S256"]
        params = {"code": "one-time-code", "state": answer_state or query["state"][0], **(extra or {})}
        httpx.get(query["redirect_uri"][0], params=params)
    return open_browser


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.creds = MemoryCredentials()

    def connect(self, google, **kw):
        http = httpx.Client(transport=httpx.MockTransport(google.handler))
        return gmail.connect(CLIENT, self.creds, http=http, timeout=10, **kw)

    def test_connect_stores_only_in_the_credential_store(self):
        self.assertEqual(self.connect(FakeGoogle(), open_browser=browser()), "candidate@example.com")
        stored = json.loads(self.creds.read(gmail.CREDENTIAL_TARGET))
        self.assertEqual(stored["refresh_token"], "r1")

    def test_a_forged_answer_is_refused_and_nothing_saved(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            self.connect(FakeGoogle(), open_browser=browser(answer_state="forged"))
        self.assertEqual(ctx.exception.code, "state_mismatch")
        self.assertIsNone(self.creds.read(gmail.CREDENTIAL_TARGET))

    def test_read_only_scope_must_be_granted(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            self.connect(FakeGoogle(scope="openid"), open_browser=browser())
        self.assertEqual(ctx.exception.code, "scope_not_granted")
        self.assertIsNone(self.creds.read(gmail.CREDENTIAL_TARGET))

    def test_declined_consent(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            self.connect(FakeGoogle(), open_browser=browser(extra={"error": "access_denied"}))
        self.assertEqual(ctx.exception.code, "denied")

    def test_client_file_must_be_a_desktop_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text(json.dumps({"web": CLIENT}), encoding="utf-8")
            with self.assertRaises(gmail.GmailError) as ctx:
                gmail.load_client(path)
            self.assertEqual(ctx.exception.code, "client_file_wrong_type")
            path.write_text(json.dumps({"installed": CLIENT}), encoding="utf-8")
            self.assertEqual(gmail.load_client(path), CLIENT)


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(self.tmp.name))
        self.creds = MemoryCredentials()
        self.creds.write(gmail.CREDENTIAL_TARGET, json.dumps({**CLIENT, "refresh_token": "r1"}))
        self.board_requests = []
        self.boards = HttpClient(transport=httpx.MockTransport(lambda r: self.board_requests.append(str(r.url)) or httpx.Response(404)),
                                 min_interval=0, sleep=lambda s: None)

    def tearDown(self):
        self.boards.close()
        self.tmp.cleanup()

    def reader(self, google):
        return gmail.GmailClient(self.creds, http=httpx.Client(transport=httpx.MockTransport(google.handler)))

    def after_a_fix(self, version="after-a-fix"):
        """A CareerOS update that changes how email is read: every held digest is read once more."""
        import careeros.cli as cli
        return unittest.mock.patch.object(cli, "_reader_fingerprint", return_value=version)

    def test_alerts_become_leads_once_and_messages_are_remembered_not_stored(self):
        google = FakeGoogle()
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(google))
        self.assertEqual(counts["gmail_messages"], 2)
        self.assertEqual(counts["lead_new"], 3)  # linkedin, indeed + the unreadable greenhouse link
        self.assertEqual(counts["notice_new"], 2)   # nhg's careers page, in each email: a notice, never a lead (D192 part 3)
        self.assertEqual(counts["lead_fetch_failed"], 1)  # the Greenhouse link (simulated 404) is kept as a lead
        self.assertEqual([tuple(r) for r in self.store.all("SELECT message_id, source FROM alert_messages ORDER BY message_id")],
                         [("m1", "gmail"), ("m2", "gmail")])
        again = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(google))
        self.assertEqual(again["gmail_messages"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 4)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM alert_notices")["c"], 2)
        everything = " ".join(str(v) for t in ("leads", "alert_messages", "events") for r in self.store.all(f"SELECT * FROM {t}") for v in r)
        self.assertNotIn("candidate@example.com", everything)  # the recipient never lands in the database

    def test_agency_retry_retains_leads_without_fetching_or_exporting_trackers(self):
        from careeros.discovery.board_discovery import candidate_from_lead
        from careeros.discovery.seen_index import build

        google = FakeGoogle()
        incomplete = agency_card("Claims Analyst", AG_1) + agency_component(18, "<b>Claims Analyst</b>")
        google.messages = {"m1": agency_email(incomplete), "m2": agency_email(subject="Your application was received", bulk=False)}
        result = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((result["lead_new"], result["gmail_digests_deferred"], result["correspondence_skipped"]), (1, 1, 1))
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])
        google.messages["m1"] = agency_email()
        with self.after_a_fix():
            result = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((result["lead_new"], result["lead_seen_before"], result["gmail_messages"]), (1, 1, 1))
        self.assertEqual({r["url"] for r in self.store.all("SELECT url FROM leads")}, {AG_1, AG_2})
        self.assertTrue(all(candidate_from_lead(r, POLICY) is None for r in self.store.all("SELECT * FROM leads")))
        self.assertFalse(self.board_requests)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM jobs")[0], 0)
        self.assertEqual(build(self.store), [])

    def test_agency_observation_failure_leaves_digest_eligible_for_retry(self):
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": agency_email(subject="Interview invitation", bulk=False)}
        with unittest.mock.patch("careeros.openings.record_observation", side_effect=RuntimeError("synthetic write failure")):
            failed = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((failed["lead_errors"], failed["gmail_digests_deferred"]), (2, 1))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads")[0], 0)
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])
        retried = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((retried["lead_new"], retried["gmail_messages"]), (2, 1))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM observations")[0], 2)
        self.assertFalse(self.board_requests)

    def test_a_held_digest_is_reported_once_shown_for_two_weeks_and_closed_when_read_in_full(self):
        from careeros.review.report import held_digests
        google = FakeGoogle()
        incomplete = agency_card("Claims Analyst", AG_1) + agency_component(18, "<b>Claims Analyst</b>")
        google.messages = {"m1": agency_email(incomplete), "m2": agency_email(subject="Your application was received", bulk=False)}
        for _ in range(2):  # the retry that still fails adds no second problem
            _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        problems = self.store.all("SELECT entity_type, entity_id, status, message FROM exceptions WHERE error_code = 'agency_digest_held'")
        self.assertEqual([(r["entity_type"], r["entity_id"], r["status"]) for r in problems], [("gmail_message", "m1", "open")])
        warning = held_digests(self.store)
        self.assertIn("1 agency job-alert digest(s)", warning)
        from careeros.review.report import build_report
        self.assertIn(warning, build_report(self.store, self.ctx.profile, POLICY))
        self.assertNotIn("Harbor", warning + problems[0]["message"])  # no notice names the agency
        self.store.clock.advance(days=15)
        self.assertIsNone(held_digests(self.store))  # an old one stops showing; a new failure shows again
        self.store.clock.advance(days=-15)
        google.messages["m1"] = agency_email()
        with self.after_a_fix():
            _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual(self.store.one("SELECT status FROM exceptions WHERE error_code = 'agency_digest_held'")["status"], "resolved")
        self.assertIsNone(held_digests(self.store))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events WHERE event_type = 'exception_resolved'")[0], 1)

    def test_a_blocked_agency_site_stores_none_of_its_cards(self):
        from careeros.discovery import sources
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": alert_email()}
        status = sources.status
        with unittest.mock.patch.object(sources, "status", side_effect=lambda s: "blocked" if s == "harborstaffing" else status(s)):
            counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual(counts["lead_suppressed"], 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads WHERE site = 'harborstaffing'")[0], 0)

    def test_agency_mail_without_a_readable_card_is_held_and_retried_never_read_as_done(self):
        google = FakeGoogle()
        changed_layout = f'<strong>Claims Analyst</strong><p>Remote</p><a href="{AG_1}">View job</a>'   # Codex CR-AD-01
        google.messages = {"m1": agency_email(changed_layout), "m2": alert_email()}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual(counts["gmail_digests_deferred"], 1)
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])
        problem = self.store.one("SELECT entity_id, message FROM exceptions WHERE error_code = 'agency_digest_held'")
        self.assertEqual(problem["entity_id"], "m1")
        self.assertIn("a job card could not be read", problem["message"])
        google.messages["m1"] = agency_email(agency_card("Claims Analyst", AG_1))   # after a fix, the same message is read
        with self.after_a_fix():
            again = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((again["lead_new"], again["gmail_messages"]), (1, 1))

    def test_a_job_number_in_a_cards_prose_never_merges_two_jobs(self):
        google = FakeGoogle()
        prose = "For a different opening, see Job #OTHER9999."   # Codex CR-AD-03
        google.messages = {"m1": agency_email(agency_card("Claims Quality Analyst", AG_1, description=prose)),
                           "m2": agency_email(agency_card("Benefits Analyst", AG_2, description=prose), subject="More jobs")}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual(counts["lead_seen_before"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads WHERE site = 'harborstaffing'")[0], 2)

    def test_the_same_private_link_never_becomes_an_ordinary_lead_in_either_order(self):
        import os
        from careeros.cli import _record_alert
        from careeros.core import private_sites
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.board_discovery import candidate_from_lead, lead_facts
        from careeros.discovery.seen_index import build, files
        private_link = "https://click.email.harborstaffing.com/job/synthetic-private-token"   # Codex CR-AD-06
        plain = f'<a href="{private_link}">Claims Analyst</a><br>Harbor Staffing · Remote'
        card = agency_email(agency_card("Claims Analyst", private_link))
        # agency card first, then the same link as an ordinary alert while the setting is off: no second row
        _record_alert(self.ctx, parse_eml_bytes(card), self.boards)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            again = _record_alert(self.ctx, parse_eml_bytes(alert_email(plain)), self.boards)
            # its link names no posting while the setting is off: a notice, never an ordinary lead (D192 part 3)
            self.assertEqual((again["lead_new"], again["notice_new"]), (0, 1))
            self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads WHERE url = ?", (private_link,))[0], 1)
        # an ordinary copy stored before D192 (setting off), then the agency card: the earlier copy is kept local
        other = private_link.replace("synthetic-private-token", "synthetic-other-token")
        legacy_lead(self.store, other, context="Harbor Staffing · Remote")
        later = _record_alert(self.ctx, parse_eml_bytes(agency_email(agency_card("Claims Analyst", other))), self.boards)
        self.assertEqual((later["lead_new"], later["lead_seen_before"]), (0, 1))
        ordinary = self.store.one("SELECT * FROM leads WHERE url = ? AND lead_id NOT LIKE 'lead:agency:%'", (other,))
        payload = json.dumps(files(build(self.store)))   # the setting in place: both rows of each link stay local
        self.assertNotIn("synthetic-private-token", payload)
        self.assertNotIn("synthetic-other-token", payload)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), \
                unittest.mock.patch("careeros.discovery.alerts.agency_setting_problem", return_value=None):
            payload = json.dumps(files(build(self.store)))   # behind the fail-closed rule, the marks alone still hold
            self.assertNotIn("synthetic-private-token", payload)
            self.assertNotIn("synthetic-other-token", payload)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            with self.assertRaisesRegex(ValueError, "agency_setting_missing"):   # the setting gone: no export at all (D125)
                build(self.store)
            self.assertIsNone(lead_facts(self.store, ordinary["lead_id"], POLICY))
            self.assertIsNotNone(candidate_from_lead(ordinary, POLICY))   # only the marked twin keeps it out now

    def test_alert_intake_stops_while_a_stored_agency_leads_setting_is_missing(self):
        import os
        from careeros.core import private_sites
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        stored = self.store.one("SELECT COUNT(*) FROM leads")[0]
        google.messages = {"m1": alert_email(), "m2": alert_email()}
        moved = Path(self.tmp.name) / "moved.yaml"
        moved.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                         "email.harborstaffing.com, tracker_host: links.harborstaffing.com}\n", encoding="utf-8")
        for setting in ("", str(moved)):   # removed, or changed to another host (D125, fail closed)
            with self.subTest(setting=setting), unittest.mock.patch.dict(os.environ, {private_sites.ENV: setting}):
                with self.assertRaises(gmail.GmailError) as raised:
                    _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(FakeGoogle()))
                self.assertIn("agency_setting_missing", str(raised.exception))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads")[0], stored)

    def test_queued_discovery_skips_leads_that_turned_out_to_be_agency_links_and_stops_without_the_setting(self):
        import os
        from careeros.cli import _record_alert
        from careeros.core import private_sites
        from careeros.discovery import discovery_queue as dq
        from careeros.discovery.alerts import parse_eml_bytes
        link = "https://click.email.harborstaffing.com/job/synthetic-queued-token"   # Codex Pass 3 CR-AD-02
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # no setting yet: an ordinary lead, queued
            legacy_lead(self.store, link)   # as stored before D192 (such a card is a notice now: part 3)
            dq.build_backfill(self.ctx, 30)
            self.assertEqual([row["employer"] for row in dq.plan(self.ctx)], ["Synthetic Employer Inc"])
        _record_alert(self.ctx, parse_eml_bytes(agency_email(agency_card("Claims Analyst", link))), self.boards)   # its twin
        self.assertEqual(dq.plan(self.ctx), [])   # the queued row's only lead is now an agency link: not probed
        self.assertEqual(dq.run(self.ctx, client=None)["agency_lead_skipped"], 1)
        other = '<a href="https://www.linkedin.com/comm/jobs/view/4066666666/">Reporting Analyst</a><br>Other Employer Inc · Remote'
        _record_alert(self.ctx, parse_eml_bytes(alert_email(other)), self.boards)   # an unrelated, ordinary lead
        dq.build_backfill(self.ctx, 30)
        self.assertEqual([row["employer"] for row in dq.plan(self.ctx)], ["Other Employer Inc"])
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # the setting gone again: nothing at all
            self.assertEqual(dq.build_backfill(self.ctx, 30), {"agency_setting_missing": 1})
            self.assertEqual(dq.plan(self.ctx), [])
            self.assertEqual(dq.run(self.ctx, client=None).get("agency_setting_missing"), 1)

    def test_a_host_once_set_up_stays_required_before_any_lead_is_stored(self):
        import os
        from careeros.core import private_sites
        google = FakeGoogle()
        google.messages = {"m1": alert_email(), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))   # set up: enrolled, no agency mail yet
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads WHERE lead_id LIKE 'lead:agency:%'")[0], 0)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # CR-AD-V2-02: then turned off
            with self.assertRaises(gmail.GmailError) as raised:
                _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(FakeGoogle()))
            self.assertIn("agency_setting_missing", str(raised.exception))

    def test_a_missing_agency_entry_is_named_as_such_by_every_intake_route(self):
        import os
        from careeros.cli import _import_alert, _inbox_pull
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir()
        saved = inbox / "alert.eml"
        saved.write_bytes(alert_email())
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), contextlib.redirect_stdout(out), \
                unittest.mock.patch("careeros.discovery.github_inbox.open_issues") as issues:   # L3F-03
            self.assertEqual(_import_alert(self.ctx, saved, self.boards, inbox, "2026-10-11")["alert_config_error"], 1)
            self.assertEqual(_inbox_pull(self.ctx, self.boards, "synthetic/recommendations"), {"inbox_config_error": 1})
        issues.assert_not_called()
        self.assertTrue(saved.exists())
        self.assertIn("restore the agency entry, then import again", out.getvalue())
        codes = [r["error_code"] for r in self.store.all("SELECT error_code FROM exceptions ORDER BY occurred_at, rowid")]
        self.assertEqual(codes, ["agency_setting_missing", "agency_setting_missing"])

    def test_the_agencys_tracking_links_outside_its_digest_envelope_are_held_not_read(self):
        google = FakeGoogle()
        google.messages = {"m1": agency_email(bulk=False), "m2": alert_email()}   # CR-AD-V2-06
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual(counts["gmail_digests_deferred"], 1)
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])

    def test_automatic_matching_uses_no_lead_while_an_agency_setting_is_missing(self):
        import os
        from careeros.cli import _record_alert
        from careeros.core import private_sites
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.lead_resolution import Resolver
        link = "https://click.email.harborstaffing.com/job/synthetic-private-token?qs=synthetic"   # CR-AD-V2-04
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            _record_alert(self.ctx, parse_eml_bytes(alert_email(f'<a href="{link}">Claims Analyst</a><br>Synthetic Employer Inc · Remote')),
                          self.boards)   # a normalised ordinary copy, stored before the setting existed
        _record_alert(self.ctx, parse_eml_bytes(agency_email(agency_card("Claims Analyst", link))), self.boards)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            self.assertEqual(Resolver(self.ctx).open_leads(), {})

    def test_a_refused_run_never_enrols_and_a_host_once_set_up_stays_required(self):
        import argparse
        import os
        from careeros.cli import cmd_agency_hosts
        from careeros.core import private_sites
        from careeros.discovery.alerts import enrolled_hosts

        def entry(host):
            path = Path(self.tmp.name) / f"{host}.yaml"
            path.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                            f"email.harborstaffing.com, tracker_host: {host}}}\n", encoding="utf-8")
            return str(path)

        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up with the fixture's host
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: entry("click.typo.example")}):   # L3-01
            with self.assertRaises(gmail.GmailError):
                _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))
        self.assertNotIn("click.typo.example", enrolled_hosts(self.store))   # the refused run enrolled nothing
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # the working entry restored: it runs
        typo = entry("click.typo.example")   # L3F-01: a typo next to the real host, enrolled by a passing run
        both = Path(self.tmp.name) / "both.yaml"
        both.write_text(Path(entry("click.email.harborstaffing.com")).read_text(encoding="utf-8").replace(
            "}\n", ", former_hosts: [click.typo.example]}\n"), encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(both)}):
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))
        self.assertIn("click.typo.example", enrolled_hosts(self.store))
        with self.assertRaises(gmail.GmailError) as raised:   # the typo removed from the entry: stopped, and named
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))
        from careeros.discovery.alerts import agency_setting_problem
        self.assertNotIn("click.typo.example", str(raised.exception))   # run output names no host (P4-01)
        self.assertIn("click.typo.example", agency_setting_problem(self.store, name_hosts=True))   # careeros status does
        self.assertEqual(raised.exception.code, "agency_setting_missing")
        for env in ({private_sites.ENV: str(both)}, {}):   # D153 R6: --forget is turned off, configured or not
            with unittest.mock.patch.dict(os.environ, env), self.assertRaises(SystemExit) as refused:
                cmd_agency_hosts(self.ctx, {}, argparse.Namespace(forget="click.typo.example"))
            self.assertIn("turned off", str(refused.exception))
        self.assertIn("click.typo.example", enrolled_hosts(self.store))   # still required: intake stays stopped
        with self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(both)}):   # listed again (former_hosts): it runs
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))
        self.assertTrue(typo)

    def test_a_host_used_by_a_stored_lead_cannot_be_forgotten(self):
        import argparse
        import os
        from careeros.cli import cmd_agency_hosts
        from careeros.core import private_sites
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(SystemExit) as refused:
            cmd_agency_hosts(self.ctx, {}, argparse.Namespace(forget="click.email.harborstaffing.com"))
        self.assertIn("turned off", str(refused.exception))   # D153 R6

    def test_after_a_stop_the_pull_looks_back_over_the_missed_days(self):
        import os
        from careeros.core import private_sites
        queries = []

        class Google(FakeGoogle):
            def handler(self, request):
                if request.url.path.endswith("/messages"):
                    queries.append(request.url.params.get("q"))
                return super().handler(request)

        google = Google()
        google.messages = {"m1": agency_email(), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "newer_than:3d from:x", gmail=self.reader(google))   # set up and enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d from:x", gmail=self.reader(Google()))   # stopped (L4-02)
        self.store.clock.advance(days=5)
        queries.clear()
        _gmail_pull(self.ctx, self.boards, "newer_than:3d from:x", gmail=self.reader(Google()))   # restored
        self.assertEqual(queries[0], "newer_than:7d from:x")
        queries.clear()
        _gmail_pull(self.ctx, self.boards, "newer_than:3d from:x", gmail=self.reader(Google()))   # caught up: usual window
        self.assertEqual(queries[0], "newer_than:3d from:x")

    def test_the_setting_is_checked_again_for_each_message(self):
        import contextlib
        import os
        from careeros.core import private_sites
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": agency_email(agency_card("Billing Specialist", AG_2), subject="More")}
        real, entries = private_sites.pinned, []
        patch = unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""})

        @contextlib.contextmanager
        def removed_before_the_second_email():   # the settings file goes away between the messages (L4-01)
            if private_sites._PINNED.get() is None:
                entries.append(1)
                if len(entries) == 2:
                    patch.start()
            with real() as revision:
                yield revision

        try:
            with unittest.mock.patch.object(private_sites, "pinned", removed_before_the_second_email), \
                    self.assertRaises(gmail.GmailError) as raised:
                _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        finally:
            patch.stop()
        self.assertEqual(raised.exception.code, "private_sites_changed")   # the pull's reading no longer holds (D141)
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m1"])   # m2 never marked
        self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])   # kept for the catch-up

    def settings_file(self, host="click.email.harborstaffing.com", layout="c5d892ac0ea84766"):
        path = Path(self.tmp.name) / "private_sites.yaml"
        path.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                        f"email.harborstaffing.com, tracker_host: {host}, layout: {layout}}}\n", encoding="utf-8")
        return path

    def test_settings_rewritten_in_place_while_an_email_is_read_commit_nothing_of_it(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        from careeros.discovery import seen_index
        path = self.settings_file()
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up: enrolled
            for change in ("entry removed", "host changed"):   # CR-AD-V2-07: the same path, rewritten during the read
                with self.subTest(change=change):
                    path = self.settings_file()
                    other = "https://click.other-agency.example/job/synthetic-other"
                    google = ListedGoogle({"m3": agency_email(), "m4": alert_email(
                        f'<a href="{other}">Claims Analyst</a><br>Synthetic Employer Inc · Remote')})
                    original = cli._record_alert

                    def rewrite(*args, original=original, change=change, path=path, **kwargs):
                        result = original(*args, **kwargs)
                        if change == "entry removed":
                            path.write_text("sources: {}\n", encoding="utf-8")
                        else:
                            self.settings_file(host="click.other-agency.example")
                        return result

                    with unittest.mock.patch.object(cli, "_record_alert", side_effect=rewrite), \
                            self.assertRaises(gmail.GmailError) as raised:
                        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
                    self.assertEqual(raised.exception.code, "private_sites_changed")
                    self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id IN ('m3', 'm4')"))
                    with self.assertRaises(gmail.GmailError) as again:   # the next pull reads the file as it is now
                        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
                    self.assertEqual(again.exception.code, "agency_setting_missing")
                    with self.assertRaisesRegex(ValueError, "agency_setting_missing"):
                        seen_index.build(self.store)
                    self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url LIKE '%other-agency%'"))

    def test_a_same_size_rewrite_with_its_time_set_back_is_read_by_every_check(self):
        import os
        from careeros.core import private_sites
        from careeros.discovery.alerts import agency_setting_problem
        path = self.settings_file()
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up: enrolled
            before = private_sites.revision()
            self.assertEqual(private_sites.agency_digests()[0].tracker_host, "click.email.harborstaffing.com")
            stat = path.stat()
            self.settings_file(host="click.email.harborstaffinh.com")   # the same size
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertIn("click.email.harborstaffing.com", agency_setting_problem(self.store, name_hosts=True))   # the content
            self.assertNotEqual(private_sites.revision(), before)   # the checks read the content (CR-AD-V2-07)
            self.assertEqual(private_sites.agency_digests()[0].tracker_host, "click.email.harborstaffinh.com")

    def test_a_recognised_digest_is_read_whatever_its_subject_by_the_gmail_route(self):
        google = FakeGoogle()
        google.messages = {"m1": agency_email(subject="Your next steps: new jobs this week"),   # L1-01
                           "m2": agency_email(agency_card("Billing Specialist", AG_2), subject="Your next steps", bulk=False)}
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"], counts.get("correspondence_skipped", 0)), (2, 1))

    def test_an_unreadable_email_from_the_agency_is_held_not_marked_read(self):
        google = ListedGoogle()
        unreadable = ("From: Job Alerts <" + "@".join(("synthetic", "email.harborstaffing.com")) + ">\r\n"
                      "Subject: Your jobs\r\nContent-Type: application/octet-stream\r\n\r\nxx").encode()
        charset = ("From: Job Alerts <" + "@".join(("synthetic", "email.harborstaffing.com")) + ">\r\n"
                   "Subject: Your jobs\r\nContent-Type: text/html; charset=x-no-such-charset\r\n\r\n<p>hi</p>").encode()
        google.messages = {"m1": unreadable, "m2": alert_email(), "m3": charset}   # L1-06
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        self.assertEqual(counts["gmail_digests_deferred"], 2)
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])
        held = " | ".join(r["message"] for r in self.store.all(
            "SELECT message FROM exceptions WHERE error_code = 'agency_digest_held' ORDER BY entity_id"))
        self.assertIn("an attachment or another part besides its text", held)   # m1, held first (D153 R8)
        self.assertIn("it could not be read as an email", held)   # m3: one body part, in a charset nothing reads (L1-06)

    def test_a_stop_is_shown_on_the_review_page_and_notified(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        from careeros.review.report import build_report
        google = FakeGoogle()
        google.messages = {"m1": agency_email(), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # L3-02
            page = build_report(self.store, self.ctx.profile, POLICY)
            self.assertIn("Alert intake is stopped", page)
            banner = page[page.index("Alert intake is stopped"):].split("</div>")[0]   # the banner never names the agency
            self.assertNotIn("harbor", banner.lower())
            with unittest.mock.patch.object(cli, "_gmail_method", return_value="api"), \
                    unittest.mock.patch("careeros.core.credentials.WindowsCredentials"), \
                    unittest.mock.patch.object(cli, "_gmail_pull", side_effect=gmail.GmailError("agency_setting_missing", "stopped")), \
                    unittest.mock.patch("careeros.ops.notify.toast") as toast:
                cli._daily_gmail(self.ctx)
            toast.assert_called_once()
            self.assertNotIn("harbor", " ".join(toast.call_args.args).lower())
            from types import SimpleNamespace
            from careeros.ops import runtime
            out = io.StringIO()
            paths = {"lock": Path(self.tmp.name) / "data" / "writer.lock", "backups": Path(self.tmp.name) / "backups"}
            with unittest.mock.patch.object(cli, "_ai_status", lambda ctx: "off"), contextlib.redirect_stdout(out), \
                    unittest.mock.patch.object(runtime, "differences", lambda lock: []):
                cli.cmd_status(self.ctx, paths, SimpleNamespace())
            self.assertIn("ALERT INTAKE STOPPED: agency_setting_missing", out.getvalue())
        self.assertNotIn("Alert intake is stopped", build_report(self.store, self.ctx.profile, POLICY))

    def test_an_agency_link_in_an_inbox_line_becomes_a_local_agency_lead(self):
        from careeros.cli import _record_alert
        from careeros.discovery.github_inbox import issue_alert
        issue = {"number": 7, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                 "body": f"Claims Analyst | Harbor Staffing | Remote | {AG_1} | source=alert"}   # L4-03
        alert = issue_alert(issue)
        self.assertEqual([(link.site, link.job_like) for link in alert.links], [("harborstaffing", True)])
        counts = _record_alert(self.ctx, alert, self.boards)
        self.assertEqual(counts["lead_new"], 1)
        self.assertTrue(self.store.one("SELECT lead_id FROM leads")["lead_id"].startswith("lead:agency:"))

    def test_an_agency_lead_stored_under_another_spelling_of_its_host_does_not_stop_intake(self):
        from careeros.cli import _record_alert
        from careeros.discovery.alerts import agency_setting_problem
        from careeros.discovery.github_inbox import issue_alert
        for n, link in enumerate(("https://click.email.harborstaffing.com./job/synthetic-dot",   # CR-AD-V2-03
                                  "https://click.email%2Eharborstaffing.com/job/synthetic-escape"), 1):
            issue = {"number": n, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                     "body": f"Claims Analyst | Harbor Staffing | Remote | {link} | source=alert"}
            self.assertEqual(_record_alert(self.ctx, issue_alert(issue), self.boards)["lead_new"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM leads WHERE lead_id LIKE 'lead:agency:%'")[0], 2)
        self.assertIsNone(agency_setting_problem(self.store))   # the same host: the setting is not missing

    def test_an_inbox_link_carrying_a_host_python_cannot_read_stays_local(self):
        """Codex Pass 5 (D153 R5, CR-AD-V2-02 to -04, -08): a scanner wrapper around a host with a middle dot, which a
        browser takes to the agency's enrolled xn-- host, became an ordinary lead and reached the shared index whole."""
        import contextlib
        import io
        from types import SimpleNamespace
        from urllib.parse import quote
        from careeros.cli import _record_alert, cmd_agency_hosts
        from careeros.discovery import seen_index
        from careeros.discovery.alerts import agency_lead_id, agency_setting_problem
        from careeros.discovery.github_inbox import issue_alert
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        wrapped = "https://scanner.example.net/jobs/" + quote("https://a\u00b7b.example.com/job/SYNTH-PERSONAL", safe="")
        issue = {"number": 9, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                 "body": f"Claims Analyst | Synthetic Employer | Remote | {wrapped} | source=alert"}
        alert = issue_alert(issue)
        self.assertEqual([link.site for link in alert.links], ["invalid"])   # an inbox line it cannot read: reported
        _record_alert(self.ctx, alert, self.boards)
        self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url = ?", (wrapped,)))   # never a lead (Codex Pass 5)
        stored = agency_lead_id(wrapped)   # one stored before (as an agency link): local, and it names no required host
        self.store.writer("discovery").insert("leads", {
            "lead_id": stored, "url": wrapped, "site": "other", "title": "Claims Analyst", "title_norm": "claims analyst",
            "family": "pbm_claims_operations", "first_seen_at": self.store.clock.now(), "status": "new"})
        self.store.conn.execute("INSERT INTO leads (lead_id, url, site, title, title_norm, family, first_seen_at, status) "
                                "VALUES ('lead:ordinary', ?, 'other', 'Claims Analyst', 'claims analyst', "
                                "'pbm_claims_operations', '2026-10-11T12:00:00Z', 'new')", (wrapped,))
        self.assertNotIn("SYNTH-PERSONAL", json.dumps(seen_index.build(self.store)))   # not even an unmarked copy
        self.assertIsNone(agency_setting_problem(self.store))   # it names no required host (CR-AD-V2-08)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_hosts(self.ctx, {}, SimpleNamespace(forget=None))
        self.assertIn("1 stored agency link(s) carry a host CareerOS cannot read", out.getvalue())

    def test_a_problem_is_resolved_even_when_the_clock_was_set_back(self):
        writer = self.store.writer("discovery")
        problem = writer.exception("integration", "medium", "held", error_code="agency_digest_held")
        self.store.clock.advance(days=-1)   # L4-C2
        self.assertTrue(writer.resolve_exception(problem, "read in full"))
        row = self.store.one("SELECT status, resolved_at, occurred_at FROM exceptions WHERE exception_id = ?", (problem,))
        self.assertEqual((row["status"], row["resolved_at"]), ("resolved", row["occurred_at"]))

    def test_a_tracking_link_hidden_from_python_by_a_backslash_is_never_an_ordinary_lead(self):
        link = "https://click.email.harborstaffing.com\\@ordinary.example/job/synthetic-ambiguous-authority"   # CR-AD-V2-03
        google = ListedGoogle({"m1": alert_email(f'<a href="{link}">Claims Analyst</a><br>Synthetic Employer Inc · Remote')})
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        self.assertEqual((counts.get("lead_new", 0), counts["gmail_digests_deferred"]), (0, 1))   # held, never a lead
        later = ListedGoogle({"m2": alert_email('<a href="https://www.linkedin.com/jobs/view/4012345690">Claims Analyst</a>'
                                                "<br>Synthetic Employer Inc · Remote")})
        self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(later))["lead_new"], 1)   # not "seen before"

    def test_a_wrapped_agency_link_from_the_inbox_requires_only_its_tracking_host(self):
        import os
        from urllib.parse import quote
        from careeros.cli import _record_alert
        from careeros.core import private_sites
        from careeros.discovery.alerts import agency_setting_problem
        from careeros.discovery.github_inbox import issue_alert
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        wrapped = "https://safelinks.example.net/?url=" + quote(AG_1, safe="")
        issue = {"number": 8, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                 "body": f"Claims Analyst | Harbor Staffing | Remote | {wrapped} | source=alert"}
        self.assertEqual(_record_alert(self.ctx, issue_alert(issue), self.boards)["lead_new"], 1)
        self.assertTrue(self.store.one("SELECT 1 FROM leads WHERE lead_id LIKE 'lead:agency:%' AND url = ?", (wrapped,)))
        self.assertIsNone(agency_setting_problem(self.store))   # CR-AD-V2-08: the wrapper's own host is never required
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # intake keeps running
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            problem = agency_setting_problem(self.store, name_hosts=True)
        self.assertIn("click.email.harborstaffing.com", problem)
        self.assertNotIn("safelinks", problem)

    def test_a_host_an_earlier_ordinary_lead_or_job_uses_is_never_forgotten(self):
        import argparse
        import os
        from careeros.cli import cmd_agency_hosts
        from careeros.core import private_sites
        from careeros.discovery import seen_index
        from tests.helpers import add_job
        link = "https://click.email.harborstaffing.com/job/synthetic-before-setup"
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # before the agency was set up: ordinary
            legacy_lead(self.store, link)   # as stored before D192 (such a card is a notice now: part 3)
        self.assertTrue(self.store.one("SELECT 1 FROM leads WHERE url = ? AND lead_id NOT LIKE 'lead:agency:%'", (link,)))
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up with the fixture: enrolled
        self.assertNotIn("synthetic-before-setup", json.dumps(seen_index.build(self.store)))
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # the entry removed: stopped (CR-AD-V2-09)
            with self.assertRaises(SystemExit) as refused:
                cmd_agency_hosts(self.ctx, {}, argparse.Namespace(forget="click.email.harborstaffing.com"))
            self.assertIn("turned off", str(refused.exception))   # D153 R6
            with self.assertRaisesRegex(ValueError, "agency_setting_missing"):
                seen_index.build(self.store)
        self.store.conn.execute("DELETE FROM leads WHERE url = ?", (link,))   # a job with that link counts the same
        add_job(self.store, "gh:synthetic:9", url=link)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(SystemExit):
            cmd_agency_hosts(self.ctx, {}, argparse.Namespace(forget="click.email.harborstaffing.com"))

    def test_a_digest_held_during_the_catch_up_is_read_again_by_its_id_after_it(self):
        import os
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(FakeGoogle()))   # stopped
        self.store.clock.advance(days=8)
        changed = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>')   # a changed layout
        first = ListedGoogle({"m5": changed})
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(first))
        self.assertEqual((first.queries, counts["gmail_digests_deferred"]), (["newer_than:10d"], 1))
        later = ListedGoogle({"m5": changed}, listed=[])   # outside the usual window now
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(later))
        self.assertEqual((later.queries, counts["gmail_held_unchanged"]), (["newer_than:3d"], 1))   # nothing changed: not read
        self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm5'"))
        readable = ListedGoogle({"m5": agency_email()}, listed=[])   # after a fix it reads in full: read again by its id
        with self.after_a_fix():
            self.assertEqual(_gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(readable))["gmail_messages"], 1)
        self.assertTrue(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm5'"))
        self.assertIsNone(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'agency_digest_held' AND status != 'resolved'"))

    def test_a_held_digest_deleted_from_gmail_is_closed(self):
        changed = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>')
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(ListedGoogle({"m5": changed})))
        with self.after_a_fix():   # read again after a change, and gone: a 404
            counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(ListedGoogle({}, listed=[])))
        self.assertEqual(counts["gmail_held_gone"], 1)
        self.assertEqual(self.store.one("SELECT value FROM meta WHERE key = 'agency_held_try:m5'")["value"], "")
        row = self.store.one("SELECT status, resolution FROM exceptions WHERE error_code = 'agency_digest_held'")
        self.assertEqual((row["status"], row["resolution"]), ("resolved", "the held email is no longer in Gmail"))

    def test_a_search_cut_off_is_reported_and_the_catch_up_lists_past_the_usual_cap(self):
        import os
        from careeros.core import private_sites
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(PagedGoogle(301)))
        self.assertEqual((counts["gmail_messages"], counts["gmail_search_cut_off"]), (300, 1))   # CR-AD-V2-10
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'gmail_search_cut_off' AND status != 'resolved'"))
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(PagedGoogle(301)))   # stopped
        self.store.clock.advance(days=5)
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(PagedGoogle(301)))   # the catch-up
        self.assertEqual((counts["gmail_messages"], counts.get("gmail_search_cut_off", 0)), (1, 0))
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM alert_messages")[0], 301)

    def test_a_held_digest_is_fetched_again_only_after_a_change_that_could_read_it_differently(self):
        import os
        from careeros.core import private_sites
        path = self.settings_file()
        changed = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>')
        google = ListedGoogle({"m5": changed})

        def fetched():
            return sum(1 for _, where in google.calls if where.endswith("/messages/m5"))

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))["gmail_digests_deferred"], 1)
            for _ in range(2):   # the email never changes: neither does its reading, so it is not fetched again
                self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))["gmail_held_unchanged"], 1)
            self.assertEqual(fetched(), 1)
            self.settings_file(layout="0123456789abcdef")   # the agency's entry changed (e.g. a new layout recorded)
            self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))["gmail_digests_deferred"], 1)
            self.assertEqual(fetched(), 2)
            with self.after_a_fix():   # a CareerOS update: read once more, then not again
                self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))["gmail_digests_deferred"], 1)
                self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))["gmail_held_unchanged"], 1)
            self.assertEqual(fetched(), 3)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM exceptions WHERE error_code = 'agency_digest_held'")[0], 1)

    def test_a_held_digest_can_be_listed_and_dismissed(self):
        import argparse
        import contextlib
        from careeros.cli import cmd_agency_held
        changed = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>')
        google = ListedGoogle({"m5": changed, "m6": changed.replace(b"synthetic-2", b"synthetic-3")})
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_held(self.ctx, {}, argparse.Namespace(dismiss=None))
        listed = out.getvalue().splitlines()
        self.assertEqual(len(listed), 2)
        self.assertNotIn("harbor", out.getvalue().lower())
        with self.assertRaises(SystemExit):   # "exc:" begins every id: more than one matches
            cmd_agency_held(self.ctx, {}, argparse.Namespace(dismiss="exc:"))
        first = listed[0].split()[0]
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_agency_held(self.ctx, {}, argparse.Namespace(dismiss=first[:12]))
        row = self.store.one("SELECT status, resolution, entity_id FROM exceptions WHERE exception_id = ?", (first,))
        self.assertEqual((row["status"], row["resolution"]), ("resolved", "dismissed by you after a look at the email"))
        self.assertTrue(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = ?", (row["entity_id"],)))
        with self.after_a_fix():   # even after a change, a dismissed email is never read again
            counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        self.assertEqual(counts["gmail_digests_deferred"], 1)   # only the other one

    def changed(self):
        return agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>')   # held: its layout

    def open_holds(self):
        return [r[0] for r in self.store.all("SELECT entity_id FROM exceptions WHERE error_code = 'agency_digest_held' "
                                             "AND status != 'resolved'")]

    def test_a_failure_to_fetch_stops_the_pull_and_never_settles_or_receipts_anything(self):
        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5"], {"m5": self.changed()}))
        self.assertEqual(self.open_holds(), ["m5"])
        for fault in (httpx.ReadTimeout("synthetic"), OSError("synthetic reset")):   # red team I3-01
            with self.subTest(fault=type(fault).__name__), self.after_a_fix(), self.assertRaises(gmail.GmailError) as stopped:
                _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {"m5": fault}))
            self.assertEqual(stopped.exception.code, "fetch_failed")
            self.assertEqual(self.open_holds(), ["m5"])   # never "read in full"
        with self.assertRaises(gmail.GmailError):   # a listed digest: no receipt either
            _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m6"], {"m6": httpx.ConnectError("synthetic")}))
        self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id IN ('m5', 'm6')"))
        counts = _gmail_pull(self.ctx, self.boards, "q",
                             gmail=Scripted(["m6"], {"m6": agency_email(agency_card("Claims Analyst", AG_2))}))
        self.assertEqual((counts["gmail_messages"], counts["lead_new"]), (1, 1))   # the network back: read in full

    def test_a_held_digest_stays_held_unless_it_is_read_in_full(self):
        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5"], {"m5": self.changed()}))
        cases = {"read as correspondence": agency_email(subject="Application received: Claims Analyst"),   # FU-03
                 "not in the only mailbox IMAP could open": gmail.GmailError("message_not_reachable")}   # FU-02
        for name, answer in cases.items():
            with self.subTest(name), self.after_a_fix(name):
                counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {"m5": answer}))
            self.assertEqual(self.open_holds(), ["m5"])
            self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm5'"))
        self.assertEqual(counts["gmail_held_not_reachable"], 1)

    def test_a_held_digest_with_a_row_that_failed_to_save_is_read_again_by_the_next_pull(self):
        partial = agency_email(agency_card("Claims Analyst", AG_1) + agency_component(18, "<b>Claims Analyst</b>"))
        with unittest.mock.patch("careeros.openings.record_observation", side_effect=RuntimeError("synthetic write failure")):
            failed = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5"], {"m5": partial}))
        self.assertEqual((failed["lead_errors"], failed["gmail_digests_deferred"]), (1, 1))
        retried = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {"m5": partial}))   # Codex Pass 3, CR-AD-V2-10
        self.assertEqual((retried.get("gmail_held_unchanged", 0), retried["lead_new"]), (0, 1))
        self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {}))["gmail_held_unchanged"], 1)
        self.assertEqual(self.open_holds(), ["m5"])   # still read only in part: held, now with every card it could read

    def test_a_held_digest_imap_could_not_reach_is_fetched_again_once_it_can(self):
        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5"], {"m5": self.changed()}))

        def inbox_only():
            reader = Scripted([], {"m5": gmail.GmailError("message_not_reachable")})
            reader.all_mail = False
            return reader

        with self.after_a_fix():
            self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=inbox_only())["gmail_held_not_reachable"], 1)
            self.assertEqual(_gmail_pull(self.ctx, self.boards, "q", gmail=inbox_only())["gmail_held_unchanged"], 1)
            counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {"m5": agency_email()}))   # All Mail shown now
        self.assertEqual(counts["gmail_messages"], 1)   # Codex Pass 3, CR-AD-V2-10: read in full once it can be reached
        self.assertEqual(self.open_holds(), [])

    def test_a_search_with_a_date_of_its_own_keeps_the_catch_up_until_a_search_can_cover_it(self):
        import os
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(FakeGoogle()))   # stopped
        self.store.clock.advance(days=8)
        dated = ListedGoogle({})
        counts = _gmail_pull(self.ctx, self.boards, "after:2026/10/18 label:jobs", gmail=self.reader(dated))
        self.assertEqual((dated.queries, counts["gmail_catch_up_blocked"]), (["after:2026/10/18 label:jobs"], 1))
        self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])   # Pass 3
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'gmail_catch_up_blocked'"))
        months = ListedGoogle({})   # a window of a month covers the ten days missed: searched as it is
        _gmail_pull(self.ctx, self.boards, "newer_than:1m label:jobs", gmail=self.reader(months))
        self.assertEqual(months.queries, ["newer_than:1m label:jobs"])
        self.assertFalse(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_one_held_digest_gmail_will_not_serve_never_blocks_the_others(self):
        failing: dict = {}
        faults = (gmail.GmailError("http_400"), gmail.GmailError("http_403"), ValueError("synthetic undecodable message"))
        for n, fault in enumerate(faults):   # http_403: one message's own refusal (Codex Pass 4)
            first, second = f"m{n}a", f"m{n}b"
            with self.subTest(fault=getattr(fault, "code", type(fault).__name__)):
                _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([first, second], {first: self.changed(),
                                                                                         second: self.changed()}))
                readable = agency_email(agency_card("Billing Specialist", f"{AG_2}-{n}"))
                failing[first] = fault   # still held, and still refused, from the earlier rounds too
                with self.after_a_fix(f"fix-{n}"):
                    counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {**failing, second: readable}))
                self.assertEqual((counts["gmail_held_fetch_failed"], counts["gmail_messages"]), (len(failing), 1))   # Pass 3
                self.assertIn(first, self.open_holds())
                self.assertNotIn(second, self.open_holds())
                self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = ?", (first,)))
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'gmail_held_fetch_failed'"))
        for code in ("http_503", "http_429", "reconnect_needed"):   # Gmail itself failing still stops the pull
            with self.subTest(code), self.after_a_fix(code), self.assertRaises(gmail.GmailError):
                _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {"m0a": gmail.GmailError(code),
                                                                            "m1a": gmail.GmailError(code)}))

    def test_interview_or_application_mail_from_the_digest_sender_is_never_read_by_the_gmail_route(self):
        from tests.test_alerts import agency_mail, digest_html
        card = agency_card("Claims Analyst", AG_1)
        words = "<p>Thank you for your application. Please confirm your interview availability.</p>"
        google = ListedGoogle({"m1": agency_mail(digest_html(body=card), subject="Your interview availability"),
                               "m2": agency_mail(digest_html(body=card + words), subject="Next steps")})
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))   # Codex Pass 3, CR-AD-V2-11
        self.assertEqual((counts["correspondence_skipped"], counts["gmail_digests_deferred"], counts.get("lead_new", 0)),
                         (1, 1, 0))
        self.assertEqual(self.open_holds(), ["m2"])

    def test_a_second_agency_added_during_a_pull_waits_for_the_next_run_and_then_stays_required(self):
        import os
        from careeros.core import private_sites
        from careeros.discovery.alerts import enrolled_hosts
        path = self.settings_file()
        both = path.read_text(encoding="utf-8") + ("  - {name: Other Agency, site: otheragency, sender_domain: "
                                                   "email.other-agency.example, tracker_host: click.other-agency.example}\n")
        mail = alert_email('<a href="https://click.other-agency.example/job/synthetic-7">Claims Analyst</a>')

        class Installing(Scripted):
            def search(inner, query, limit=300):
                path.write_text(both, encoding="utf-8")   # a valid second agency, once the pull has begun
                return super().search(query, limit)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            with self.assertRaises(gmail.GmailError) as raised:   # Codex Pass 3, CR-AD-V2-02: never read by this run
                _gmail_pull(self.ctx, self.boards, "q", gmail=Installing(["m7"], {"m7": mail}))
            self.assertEqual(raised.exception.code, "private_sites_changed")
            self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages"))
            self.assertNotIn("click.other-agency.example", enrolled_hosts(self.store))
            _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m7"], {"m7": mail}))   # the next run: enrolled first
            self.assertIn("click.other-agency.example", enrolled_hosts(self.store))
            self.settings_file()   # the second entry removed again
            with self.assertRaises(gmail.GmailError) as raised:
                _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {}))
        self.assertEqual(raised.exception.code, "agency_setting_missing")

    def test_every_writing_command_enrols_the_hosts_of_its_reading_before_it_uses_them(self):
        import os
        from careeros.cli import _enroll_reading
        from careeros.core import private_sites
        from careeros.discovery.alerts import enrolled_hosts
        path = self.settings_file(host="click.other-agency.example")
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), private_sites.run():
            _enroll_reading(self.store)   # D141: whatever the command (an export, a publication, matching)
        self.assertEqual(enrolled_hosts(self.store), {"click.other-agency.example"})
        self.settings_file()   # that host gone: a stop, and the new host is not enrolled while it lasts (L3-01)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), private_sites.run():
            _enroll_reading(self.store)
        self.assertEqual(enrolled_hosts(self.store), {"click.other-agency.example"})

    def test_a_writing_command_enrols_the_hosts_of_its_reading_before_its_own_work(self):
        import argparse
        import os
        from types import SimpleNamespace
        import careeros.cli as cli
        from careeros.core import private_sites
        from careeros.discovery.alerts import enrolled_hosts
        from careeros.paths import vault_paths
        path = self.settings_file(host="click.other-agency.example")
        store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), POLICY.fingerprint)
        seen = []

        def process(ctx, paths, args):
            seen.append((sorted(enrolled_hosts(ctx.store)), private_sites._RUN.get() is not None))

        try:
            with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                    unittest.mock.patch.object(cli, "vault_paths", lambda: vault_paths(Path(self.tmp.name) / "vault")), \
                    unittest.mock.patch.object(cli, "_context", lambda *a, **k: SimpleNamespace(store=store, policy=POLICY, ai=None)), \
                    unittest.mock.patch.object(cli, "cmd_process", process):
                cli._dispatch(argparse.Namespace(command="process", output_file=None))
        finally:
            for handler in list(cli.log.handlers):   # release the temporary log file
                handler.close()
                cli.log.removeHandler(handler)
        self.assertEqual(seen, [(["click.other-agency.example"], True)])   # D141: one reading, its hosts enrolled first

    def test_no_board_is_asked_about_an_email_after_the_settings_changed_part_way(self):
        import contextlib
        import os
        from careeros.core import private_sites
        google = Scripted(["m1", "m2"], {"m1": agency_email(), "m2": alert_email()})   # m2 links a readable board
        real, entries = private_sites.pinned, []
        removed = unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""})

        @contextlib.contextmanager
        def removed_before_the_second_email():
            if private_sites._PINNED.get() is None:
                entries.append(1)
                if len(entries) == 2:
                    removed.start()
            with real() as revision:
                yield revision

        try:
            with unittest.mock.patch.object(private_sites, "pinned", removed_before_the_second_email), \
                    self.assertRaises(gmail.GmailError):
                _gmail_pull(self.ctx, self.boards, "q", gmail=google)
        finally:
            if len(entries) >= 2:
                removed.stop()
        self.assertEqual(self.board_requests, [])   # checked at the email's start: nothing of it is fetched (L4-01, D141)

    def test_a_settings_file_that_cannot_be_read_keeps_the_catch_up(self):
        import os
        from careeros.core import private_sites
        path = self.settings_file()
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # set up: enrolled
            path.write_text("agency_digests: [\n", encoding="utf-8")   # broken by hand (the red team before Pass 4)
            for _ in range(2):
                with self.assertRaises(gmail.GmailError) as raised:
                    _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))
                self.assertEqual(raised.exception.code, "private_sites_invalid")
                self.store.clock.advance(days=4)
            self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])
            self.settings_file()   # fixed
            fixed = ListedGoogle({})
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(fixed))
        self.assertEqual(fixed.queries, ["newer_than:10d"])   # the days of the broken file are read too

    def test_a_pull_gmail_itself_stops_keeps_the_catch_up(self):
        class Refusing(Scripted):
            def search(inner, query, limit=300):
                raise gmail.GmailError("reconnect_needed", "synthetic")

        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))
        with self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Refusing([], {}))   # Google asks you to reconnect
        self.store.clock.advance(days=8)
        later = ListedGoogle({})
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(later))
        self.assertEqual(later.queries, ["newer_than:10d"])   # the red team before Pass 4: none of those days lost
        with self.assertRaises(gmail.GmailError):   # Gmail never set up: no catch-up to keep
            _gmail_pull(self.ctx, self.boards, "q", credentials=MemoryCredentials())
        self.assertFalse(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_a_pull_the_network_stops_keeps_the_catch_up_too(self):
        class Unreachable(ListedGoogle):
            def handler(inner, request):
                if request.url.path.endswith("/messages"):
                    raise httpx.ConnectError("synthetic: no network", request=request)
                return super().handler(request)

        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))
        with self.assertRaises(httpx.ConnectError):   # Codex Pass 4, CR-AD-V2-10
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(Unreachable({})))
        self.store.clock.advance(days=8)
        later = ListedGoogle({})
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(later))
        self.assertEqual(later.queries, ["newer_than:10d"])   # none of those days lost

    def test_a_listed_email_gmail_refuses_the_first_time_keeps_the_catch_up(self):
        """Codex Pass 5 (D153 R9, CR-AD-V2-10): with no stop before, a listed email Gmail refused (403) left no catch-up,
        so once the usual window passed it the email was never asked for again."""
        class Refusing(ListedGoogle):
            def handler(inner, request):
                if request.url.path.endswith("/messages/m1"):
                    return httpx.Response(403)
                return super().handler(request)

        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # set up, no stop before
        self.assertFalse((self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'") or {"value": ""})["value"])
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(Refusing({"m1": alert_email()})))
        self.assertEqual(counts["gmail_message_fetch_failed"], 1)
        self.store.clock.advance(days=8)
        later = ListedGoogle({})
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(later))
        self.assertEqual(later.queries, ["newer_than:10d"])   # the refused email's day is searched again

    def test_one_email_gmail_refuses_with_403_never_blocks_the_others(self):
        class Refusing(ListedGoogle):
            def handler(inner, request):
                if request.url.path.endswith("/messages/m1"):
                    return httpx.Response(403)
                return super().handler(request)

        google = Refusing({"m1": alert_email(), "m2": alert_email()})
        for _ in range(2):   # Codex Pass 4, CR-AD-V2-10: on every pull, while Gmail refuses that one email
            counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(google))
            self.assertEqual(counts["gmail_message_fetch_failed"], 1)
        self.assertEqual([r["message_id"] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])
        with self.assertRaises(gmail.GmailError):   # the account's own refusal stops at the search
            _gmail_pull(self.ctx, self.boards, "newer_than:3d",
                        gmail=self.reader(type("Search403", (ListedGoogle,), {"handler": lambda inner, request: httpx.Response(403)
                                                                              if request.url.path.endswith("/messages")
                                                                              else ListedGoogle.handler(inner, request)})({})))

    def test_a_held_digest_the_search_still_lists_never_blocks_the_others_when_gmail_refuses_it(self):
        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5", "m6"], {"m5": self.changed(), "m6": self.changed()}))
        readable = agency_email(agency_card("Billing Specialist", f"{AG_2}-6"))
        with self.after_a_fix("listed"):
            counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m5"], {"m5": gmail.GmailError("http_400"),
                                                                                     "m6": readable}))
        self.assertEqual((counts["gmail_held_fetch_failed"], counts["gmail_messages"]), (1, 1))   # the red team before Pass 4
        self.assertEqual(self.open_holds(), ["m5"])

    def test_a_listed_email_gmail_refuses_is_reported_and_the_others_read(self):
        import os
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # a stop
        refused = Scripted(["n1", "n2"], {"n1": gmail.GmailError("imap_fetch_failed"), "n2": alert_email()})
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=refused)
        self.assertEqual((counts["gmail_message_fetch_failed"], counts["gmail_messages"]), (1, 1))   # the red team
        self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'n1'"))
        self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])   # still waiting
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'gmail_message_fetch_failed'"))
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted(["n1"], {"n1": alert_email()}))
        self.assertFalse(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_a_change_anywhere_in_careeros_or_python_reads_held_digests_again(self):
        import careeros.cli as cli

        def fingerprint():
            cli._READER.clear()
            return cli._reader_fingerprint()

        before = fingerprint()
        real = Path.read_bytes

        def fixed(path, *args):
            data = real(path, *args)
            return data + b"# a fix" if path.name == "sanitize.py" else data

        with unittest.mock.patch.object(Path, "read_bytes", fixed):   # the text cleaning the reader uses (the red team)
            self.assertNotEqual(fingerprint(), before)
        with unittest.mock.patch.object(cli.sys, "version", "3.99.0 (synthetic)"):   # Python's own parsers
            self.assertNotEqual(fingerprint(), before)
        self.assertEqual(fingerprint(), before)

    def test_a_held_digest_another_gmail_account_cannot_find_stays_held(self):
        class Account(Scripted):
            def __init__(inner, address, listed, raws):
                super().__init__(listed, raws)
                inner.mail = address

            def address(inner):
                return inner.mail

        a, b = "synthetic-a@example.invalid", "synthetic-b@example.invalid"
        _gmail_pull(self.ctx, self.boards, "q", gmail=Account(a, ["m5"], {"m5": self.changed()}))   # held, account a
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Account(b, [], {"m5": gmail.GmailError("message_gone")}))
        self.assertEqual((counts["gmail_held_not_reachable"], self.open_holds()), (1, ["m5"]))   # another account: not gone
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Account(a, [], {"m5": agency_email()}))   # account a again
        self.assertEqual((counts["gmail_messages"], self.open_holds()), (1, []))
        _gmail_pull(self.ctx, self.boards, "q", gmail=Account(a, ["m7"], {"m7": self.changed()}))
        with self.after_a_fix("gone"):
            counts = _gmail_pull(self.ctx, self.boards, "q", gmail=Account(a, [], {"m7": gmail.GmailError("message_gone")}))
        self.assertEqual((counts["gmail_held_gone"], self.open_holds()), (1, []))   # the same account: deleted, closed

    def test_a_search_with_two_windows_or_a_negated_one_keeps_the_catch_up(self):
        import os
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=Scripted([], {}))   # a stop
        self.store.clock.advance(days=8)
        for query in ("newer_than:30d label:jobs newer_than:3d", "label:jobs -newer_than:1d"):   # the red team, R-8
            with self.subTest(query=query):
                counts = _gmail_pull(self.ctx, self.boards, query, gmail=Scripted([], {}))
                self.assertEqual(counts["gmail_catch_up_blocked"], 1)
                self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_the_review_page_reads_the_settings_for_each_request_not_once_for_its_life(self):
        import argparse
        from types import SimpleNamespace
        import careeros.cli as cli
        from careeros.core import private_sites
        from careeros.paths import vault_paths
        store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), POLICY.fingerprint)
        seen = []
        try:
            with unittest.mock.patch.object(cli, "vault_paths", lambda: vault_paths(Path(self.tmp.name) / "vault")), \
                    unittest.mock.patch.object(cli, "_context", lambda *a, **k: SimpleNamespace(store=store, policy=POLICY,
                                                                                                 ai=None)), \
                    unittest.mock.patch.object(cli, "cmd_review", lambda ctx, paths, args: seen.append(private_sites._RUN.get())):
                cli._dispatch(argparse.Namespace(command="review", output_file=None))
        finally:
            for handler in list(cli.log.handlers):   # release the temporary log file
                handler.close()
                cli.log.removeHandler(handler)
        self.assertEqual(seen, [None])   # the page makes one reading per request (the red team before Pass 4)

    def test_a_change_just_before_an_emails_receipt_commits_nothing_of_it(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        path = self.settings_file()
        other = "https://click.other-agency.example/job/synthetic-other"
        real = cli._gmail_message

        def then_changed(*args, **kwargs):
            links = real(*args, **kwargs)
            self.settings_file(host="click.other-agency.example")   # read, and changed before its receipt (Codex Pass 3)
            return links

        mail = alert_email(f'<a href="{other}">Claims Analyst</a><br>Synthetic Employer Inc · Remote')
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(cli, "_gmail_message", then_changed), self.assertRaises(gmail.GmailError) as raised:
            _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m7"], {"m7": mail}))
        self.assertEqual(raised.exception.code, "private_sites_changed")
        self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url LIKE '%other-agency%'"))
        self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm7'"))
        self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])   # caught up later

    def test_after_a_stop_a_search_with_no_window_lists_past_the_usual_cap(self):
        import os
        from careeros.core import private_sites
        _gmail_pull(self.ctx, self.boards, "label:jobs", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}), self.assertRaises(gmail.GmailError):
            _gmail_pull(self.ctx, self.boards, "label:jobs", gmail=self.reader(FakeGoogle()))   # stopped
        counts = _gmail_pull(self.ctx, self.boards, "label:jobs", gmail=self.reader(PagedGoogle(301)))
        self.assertEqual((counts["gmail_messages"], counts.get("gmail_search_cut_off", 0)), (301, 0))   # Codex Pass 3
        self.assertFalse(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_replies_left_from_an_earlier_run_stop_once_the_settings_change(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        path = self.settings_file()
        for number in (7, 8):
            self.store.writer("discovery").insert("inbox_issues", {
                "repo": "synthetic/recommendations", "number": number, "processed_at": self.store.clock.now(), "links": 0,
                "reply": "CareerOS read this issue", "closed": 0, "gmail_status": None, "profile": "primary"})
        sent = []

        def close(repo, number, reply, runner, before_write=None):
            before_write()   # as close_with_reply does: the check comes before the comment (Codex Pass 4)
            sent.append(number)
            self.settings_file(host="click.other-agency.example")   # changed after the first reply (D141)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch("careeros.discovery.github_inbox.open_issues", return_value=[]), \
                unittest.mock.patch("careeros.discovery.github_inbox.close_with_reply", side_effect=close):
            counts = cli._inbox_pull(self.ctx, self.boards, "synthetic/recommendations")
        self.assertEqual((sent, counts["inbox_config_error"]), ([7], 1))

    def test_no_reply_is_sent_on_an_issue_once_the_settings_changed_after_it_was_recorded(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        path = self.settings_file()
        issue = {"number": 9, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                 "body": "Claims Analyst | Synthetic Employer Inc | Remote | https://www.linkedin.com/jobs/view/4012345671 | source=alert"}
        real_tx, depth = self.store.tx, []

        @contextlib.contextmanager
        def then_changed():
            depth.append(1)
            try:
                with real_tx():
                    yield
            finally:
                depth.pop()
            recorded = self.store.one("SELECT 1 FROM inbox_issues WHERE number = 9")
            if not depth and recorded:   # the issue's own transaction committed, then the file changed before the reply
                self.settings_file(host="click.other-agency.example")   # (Codex Pass 3, CR-AD-V2-07)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch("careeros.discovery.github_inbox.open_issues", return_value=[issue]), \
                unittest.mock.patch("careeros.discovery.github_inbox.close_with_reply") as close, \
                unittest.mock.patch.object(self.store, "tx", then_changed):
            counts = cli._inbox_pull(self.ctx, self.boards, "synthetic/recommendations")
        self.assertEqual(counts["inbox_config_error"], 1)
        close.assert_not_called()
        self.assertEqual(self.store.one("SELECT closed FROM inbox_issues WHERE number = 9")["closed"], 0)
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch("careeros.discovery.github_inbox.open_issues", return_value=[]), \
                unittest.mock.patch("careeros.discovery.github_inbox.close_with_reply") as close:
            cli._inbox_pull(self.ctx, self.boards, "synthetic/recommendations")   # the next run sends it
        close.assert_called_once()

    def test_an_index_whose_settings_change_after_it_was_built_is_neither_written_nor_published(self):
        import os
        import subprocess
        import careeros.cli as cli
        from careeros.core import private_sites
        from careeros.discovery import seen_index
        from tests.helpers import add_job
        from tests.test_v21_policy import policy_with
        path = self.settings_file()
        older = "https://jobs.other-agency.example/job/4012"   # an older ordinary link on a host made private later
        add_job(self.store, "manual:synthetic:4012", url=older, canonical_url=older, title="Claims Analyst",
                title_norm="claims analyst", employer_name="Synthetic Employer Inc")
        ctx = Context(self.store, policy_with(operations__github_inbox="owner-x/example-inbox"), self.ctx.profile, JOB_MATRIX,
                      Path(self.tmp.name))
        sent = []

        def runner(argv, **kwargs):
            sent.append((argv, kwargs.get("input") or ""))
            if "PUT" in argv:
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            changed()   # after GitHub was asked what is there, before the upload
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")

        real_files = seen_index.files
        for when in ("while the index is written out", "just before a file is sent"):
            with self.subTest(when):
                self.settings_file()
                sent.clear()
                reports = Path(self.tmp.name) / when.replace(" ", "-")
                changed = (lambda: None) if when.startswith("while") else (lambda: self.settings_file(host="jobs.other-agency.example"))

                def files(*args, when=when, **kwargs):
                    out = real_files(*args, **kwargs)
                    if when.startswith("while"):
                        self.settings_file(host="jobs.other-agency.example")   # Codex Pass 3, CR-AD-V2-07
                    return out

                with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                        unittest.mock.patch.object(seen_index, "files", files), \
                        unittest.mock.patch("careeros.discovery.github_inbox.gh_path", return_value="gh"), \
                        unittest.mock.patch.object(cli, "_replace_folder", wraps=cli._replace_folder) as staged:
                    counts = cli._publish_index(ctx, {"reports": reports}, runner=runner)
                self.assertEqual(counts, {"index_errors": 1})
                self.assertFalse(any("PUT" in argv for argv, _ in sent))   # nothing published
                if when.startswith("while"):   # nor the local copy replaced, nor even staged on disk (Pass 5)
                    self.assertEqual(list(reports.rglob("*.json")), [])
                    staged.assert_not_called()
                else:
                    staged.assert_called_once()
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'private_sites_changed'"))

    def test_the_local_index_is_staged_and_an_earlier_copy_stays_on_a_refusal(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        folder = Path(self.tmp.name) / "inbox-index"
        cli._replace_folder(folder, {"a.json": "1", "sub/b.json": "2"}, lambda: None)

        def refuse():
            raise private_sites.SettingsChanged("private_sites_changed: synthetic")

        with self.assertRaises(private_sites.SettingsChanged):
            cli._replace_folder(folder, {"a.json": "new"}, refuse)
        self.assertEqual(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.json")), ["a.json", "sub/b.json"])
        self.assertEqual((folder / "a.json").read_text(encoding="utf-8"), "1")
        self.assertEqual(sorted(p.name for p in folder.parent.iterdir()), ["inbox-index"])   # nothing staged is left
        cli._replace_folder(folder, {"a.json": "3"}, lambda: None)
        self.assertEqual([p.relative_to(folder).as_posix() for p in folder.rglob("*.json")], ["a.json"])   # replaced whole
        path, reports = self.settings_file(), Path(self.tmp.name) / "reports"
        cli._replace_folder(reports / "inbox-index", {"earlier.json": "{}"}, lambda: None)
        real = cli._replace_folder

        def changed_while_written(folder, files, check):   # Codex Pass 4: the settings changed while the copy was written
            def then_check():
                self.settings_file(host="jobs.other-agency.example")
                check()
            real(folder, files, then_check)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(cli, "_replace_folder", changed_while_written):
            self.assertEqual(cli._publish_index(self.ctx, {"reports": reports}), {"index_errors": 1})
        self.assertEqual([p.name for p in (reports / "inbox-index").iterdir()], ["earlier.json"])

    def test_a_swap_stopped_between_its_renames_keeps_the_earlier_copy(self):
        """Codex Pass 5 (D153 R3): a reader holding a staged file made the second rename fail, leaving the earlier copy
        only beside the folder; the next run, refused, removed it, leaving no copy at all."""
        import careeros.cli as cli
        from careeros.core import private_sites
        folder = Path(self.tmp.name) / "r3" / "inbox-index"
        cli._replace_folder(folder, {"a.json": "1"}, lambda: None)
        real_rename = Path.rename

        def locked(path, target):
            if path.name.endswith(".writing"):
                raise PermissionError(13, "synthetic: a reader holds a staged file")
            return real_rename(path, target)

        with unittest.mock.patch.object(Path, "rename", locked), self.assertRaises(PermissionError):
            cli._replace_folder(folder, {"a.json": "2"}, lambda: None)
        self.assertEqual((folder / "a.json").read_text(encoding="utf-8"), "1")   # put back at once

        def refuse():
            raise private_sites.SettingsChanged("private_sites_changed: synthetic")

        folder.rename(folder.with_name("inbox-index.old"))   # a swap an earlier version left half-done
        with self.assertRaises(private_sites.SettingsChanged):
            cli._replace_folder(folder, {"a.json": "3"}, refuse)
        self.assertEqual((folder / "a.json").read_text(encoding="utf-8"), "1")   # its earlier copy back first, then kept
        self.assertEqual(sorted(p.name for p in folder.parent.iterdir()), ["inbox-index"])

    def test_an_export_stopped_between_its_renames_keeps_the_earlier_export(self):
        import os
        from careeros.analytics import export
        from careeros.core import private_sites
        path, out = self.settings_file(), Path(self.tmp.name) / "r3" / "analytics"
        out.parent.mkdir()
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            export.export(self.store, POLICY, out, source="private")
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        real_rename = Path.rename

        def locked(source, target):
            if source.name.endswith(".writing"):
                raise PermissionError(13, "synthetic: a reader holds a staged file")
            return real_rename(source, target)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(Path, "rename", locked), self.assertRaises(PermissionError):
            export.export(self.store, POLICY, out, source="private")
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)   # put back at once
        out.rename(out.with_name("analytics.old"))   # an export an earlier version left half-done
        real = private_sites.check

        def changed_then_check():
            self.settings_file(host="click.other-agency.example")
            real()

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(private_sites, "check", changed_then_check), self.assertRaises(export.ExportError):
            export.export(self.store, POLICY, out, source="private")
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)   # back first, then kept
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["analytics"])
        other = Path(self.tmp.name) / "r3b" / "analytics"   # a folder of yours named like the earlier copy: never moved
        mine = other.with_name("analytics.old")
        mine.mkdir(parents=True)
        (mine / "notes.txt").write_text("mine", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), self.assertRaises(export.ExportError):
            export.export(self.store, POLICY, other, source="private")
        self.assertEqual(((mine / "notes.txt").read_text(encoding="utf-8"), other.exists()), ("mine", False))

    def test_an_export_refused_at_its_last_check_keeps_the_earlier_export(self):
        import os
        from careeros.analytics import export
        from careeros.core import private_sites
        path, out = self.settings_file(), Path(self.tmp.name) / "analytics"
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}):
            export.export(self.store, POLICY, out, source="private")
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        real = private_sites.check

        def changed_then_check():   # Codex Pass 4: changed while the earlier export was set aside
            self.settings_file(host="click.other-agency.example")
            real()

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(private_sites, "check", changed_then_check), self.assertRaises(export.ExportError):
            export.export(self.store, POLICY, out, source="private")
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir() if p.name.startswith("analytics")), ["analytics"])
        mine = out.with_name("analytics.old")   # a folder of yours beside it is never removed
        mine.mkdir()
        (mine / "notes.txt").write_text("mine", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(self.settings_file())}), \
                self.assertRaises(export.ExportError):
            export.export(self.store, POLICY, out, source="private")
        self.assertEqual((mine / "notes.txt").read_text(encoding="utf-8"), "mine")
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)

    def test_an_email_whose_settings_change_while_it_is_read_leaves_nothing_behind(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        path = self.settings_file()
        other = "https://click.other-agency.example/job/synthetic-other"
        real = cli._record_alert
        for after in ("the replacement host", "a broken file"):   # red team S2-02, I3-03
            with self.subTest(after):
                path = self.settings_file()

                def rewrite(*args, after=after, path=path, **kwargs):
                    result = real(*args, **kwargs)
                    if after == "a broken file":
                        path.write_text("agency_digests: [\n", encoding="utf-8")
                    else:
                        self.settings_file(host="click.other-agency.example")
                    return result

                mail = alert_email(f'<a href="{other}">Claims Analyst</a><br>Synthetic Employer Inc · Remote')
                with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                        unittest.mock.patch.object(cli, "_record_alert", rewrite), self.assertRaises(gmail.GmailError) as raised:
                    _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m7"], {"m7": mail}))
                self.assertEqual(raised.exception.code, "private_sites_changed")
                self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url LIKE '%other-agency%'"))   # rolled back
                self.assertIsNone(self.store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm7'"))

    def test_a_settings_file_unreadable_when_an_email_begins_is_a_reported_stop(self):
        import contextlib
        from careeros.core import private_sites

        @contextlib.contextmanager
        def unreadable():
            raise ValueError("private_sites_invalid: the file is not valid YAML near line 2")
            yield

        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m1"], {"m1": alert_email()}))
        with unittest.mock.patch.object(private_sites, "pinned", unreadable), self.assertRaises(gmail.GmailError) as raised:
            _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted(["m2"], {"m2": alert_email()}))   # red team S2-03
        self.assertEqual(raised.exception.code, "private_sites_invalid")

    def test_the_analytics_export_keeps_no_tracking_link(self):
        import os
        from careeros.analytics.export import build
        from careeros.core import private_sites
        from tests.helpers import add_job
        add_job(self.store, "manual:synthetic:1", url=AG_1)   # a pasted agency link (red team P4-02)
        add_job(self.store, "gh:synthetic:2", url="https://job-boards.greenhouse.io/synth/jobs/2")
        urls = {row["job_key"]: row["url"] for row in build(self.store, POLICY)["dim_job"]}
        self.assertEqual((urls["manual:synthetic:1"], urls["gh:synthetic:2"]), (None, "https://job-boards.greenhouse.io/synth/jobs/2"))
        _gmail_pull(self.ctx, self.boards, "q", gmail=Scripted([], {}))   # set up: enrolled
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # a stop: no link at all
            self.assertIsNone({row["job_key"]: row["url"] for row in build(self.store, POLICY)["dim_job"]}["gh:synthetic:2"])

    def test_an_analytics_export_whose_settings_change_while_it_is_built_writes_nothing(self):
        import os
        from careeros.analytics import export
        from careeros.core import private_sites
        path = self.settings_file()
        out = Path(self.tmp.name) / "analytics"
        real = export.build

        def rewrite(*args):
            tables = real(*args)
            self.settings_file(host="click.other-agency.example")   # D141: changed while the export was built
            return tables

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(export, "build", rewrite), self.assertRaises(export.ExportError) as refused:
            export.export(self.store, POLICY, out, source="private")
        self.assertTrue(str(refused.exception).startswith("private_sites_changed"))
        self.assertFalse(out.exists())
        self.assertFalse(out.with_name("analytics.writing").exists())

    def test_bulk_application_mail_from_the_agencys_sender_is_never_read_by_the_gmail_route(self):
        google = ListedGoogle({"m1": agency_email(subject="Application received: Claims Analyst")})   # CR-AD-V2-11
        counts = _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(google))
        self.assertEqual((counts["correspondence_skipped"], counts.get("lead_new", 0)), (1, 0))

    def test_a_stop_holds_the_whole_discovery_queue_curated_employers_included(self):
        import os
        from careeros.core import private_sites
        from careeros.discovery import discovery_queue as dq
        _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle()))   # set up: enrolled
        stop = unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""})
        probe = unittest.mock.patch("careeros.discovery.board_discovery.discover", side_effect=AssertionError("probed"))
        with stop, probe:   # nothing due, and still the run says it is stopped (D125)
            self.assertEqual(dq.run(self.ctx, client=None), {"requests": 0, "agency_setting_missing": 1})
        curated = Path(self.tmp.name) / "curated.yaml"
        curated.write_text("employers:\n  - {name: Curated Health, domain: curatedhealth.example}\n", encoding="utf-8")
        self.assertEqual(dq.load_curated(self.ctx, curated)["added"], 1)
        self.assertEqual([row["employer"] for row in dq.plan(self.ctx)], ["Curated Health"])   # no stop: planned
        with stop, probe:   # the whole queue stops, curated employers included (L3-03)
            self.assertEqual(dq.plan(self.ctx), [])
            self.assertEqual(dq.run(self.ctx, client=None).get("agency_setting_missing"), 1)

    def test_discovery_stops_before_the_next_probe_when_an_agency_setting_goes_missing_mid_run(self):
        from careeros.cli import _record_alert
        from careeros.discovery import alerts
        from careeros.discovery import discovery_queue as dq
        from careeros.discovery.alerts import parse_eml_bytes
        other = '<a href="https://www.linkedin.com/comm/jobs/view/4066666661/">Reporting Analyst</a><br>Other Employer Inc · Remote'
        _record_alert(self.ctx, parse_eml_bytes(alert_email(other)), self.boards)
        dq.build_backfill(self.ctx, 30)
        self.assertEqual(len(dq.plan(self.ctx)), 1)
        state, real_due = {"problem": None}, dq._due

        def due(store, now):
            rows = real_due(store, now)
            state["problem"] = "agency_setting_missing: synthetic"   # the entry goes missing once the run has started
            return rows

        with unittest.mock.patch.object(dq, "_due", due), \
                unittest.mock.patch.object(alerts, "agency_setting_problem", lambda store: state["problem"]), \
                unittest.mock.patch("careeros.discovery.board_discovery.discover", side_effect=AssertionError("probed")):
            stats = dq.run(self.ctx, client=None)
        self.assertEqual(stats.get("agency_setting_missing"), 1)   # CR-AD-V2-12: checked before every probe

    def test_an_export_during_which_the_settings_change_publishes_nothing(self):
        import os
        from careeros.core import private_sites
        from careeros.discovery import seen_index
        path = self.settings_file()
        real = seen_index._observations

        def rewrite(store):
            self.settings_file(host="click.other-agency.example")   # CR-AD-V2-07: changed while the export reads
            return real(store)

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch.object(seen_index, "_observations", rewrite), \
                self.assertRaises(private_sites.SettingsChanged):
            seen_index.build(self.store)

    def test_an_inbox_issue_read_while_the_settings_change_stays_open(self):
        import os
        import careeros.cli as cli
        from careeros.core import private_sites
        path = self.settings_file()
        issue = {"number": 9, "title": "Jobs", "author": {"login": "chatgpt"}, "createdAt": "2026-10-11T12:00:00Z",
                 "body": "Claims Analyst | Synthetic Employer Inc | Remote | https://www.linkedin.com/jobs/view/4012345671 | source=alert"}
        real = cli._record_alert

        def rewrite(*args, **kwargs):
            result = real(*args, **kwargs)
            path.write_text("sources: {}\n", encoding="utf-8")   # CR-AD-V2-07: changed while the issue is read
            return result

        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: str(path)}), \
                unittest.mock.patch("careeros.discovery.github_inbox.open_issues", return_value=[issue]), \
                unittest.mock.patch("careeros.discovery.github_inbox.close_with_reply") as close, \
                unittest.mock.patch.object(cli, "_record_alert", rewrite):
            counts = cli._inbox_pull(self.ctx, self.boards, "synthetic/recommendations")
        self.assertEqual(counts["inbox_config_error"], 1)
        close.assert_not_called()
        self.assertIsNone(self.store.one("SELECT 1 FROM inbox_issues"))
        self.assertIsNone(self.store.one("SELECT 1 FROM leads"))   # nothing of the issue kept (one transaction)

    def test_the_same_job_number_is_one_lead_whichever_site_arrived_first(self):
        google = FakeGoogle()
        linkedin = ('<a href="https://www.linkedin.com/comm/jobs/view/4055555555/">Claims Analyst</a>'
                    "<br>Harbor Staffing · Remote · req=04530-0012345678")   # Codex CR-AD-03, the reverse order
        google.messages = {"m1": alert_email(linkedin),
                           "m2": agency_email(agency_card("Claims Analyst", AG_1, description="Job ID: 04530-0012345678"))}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        # both kept, the later one a possible repeat of the earlier: never "seen", never a closed twin (Codex's D190
        # design review H8; D192 part 3)
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))
        public = self.store.one("SELECT lead_id FROM leads WHERE site = 'linkedin'")[0]
        agency = self.store.one("SELECT * FROM leads WHERE lead_id LIKE 'lead:agency:%'")
        self.assertEqual((agency["status"], agency["url"], agency["repeat_of"]), ("new", AG_1, public))

    def test_a_mixed_digest_is_held_not_recorded_as_read(self):
        google = FakeGoogle()
        mixed = agency_card("Claims Analyst", AG_1) + f'<strong>Benefits Analyst</strong><a href="{AG_2}">View job</a>'
        google.messages = {"m1": agency_email(mixed), "m2": alert_email()}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"] >= 1, counts["gmail_digests_deferred"]), (True, 1))
        self.assertEqual([r[0] for r in self.store.all("SELECT message_id FROM alert_messages")], ["m2"])

    def test_stored_agency_leads_stay_local_whatever_the_private_setting_says_later(self):
        import os
        from careeros.core import private_sites
        from careeros.discovery.board_discovery import candidate_from_lead
        from careeros.discovery.seen_index import build, files
        private_link = "https://click.email.harborstaffing.com/job/synthetic-private-token"   # Codex CR-AD-02
        google = FakeGoogle()
        google.messages = {"m1": agency_email(agency_card("Claims Analyst", private_link)), "m2": alert_email()}
        _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        lead = self.store.one("SELECT * FROM leads WHERE url = ?", (private_link,))
        self.assertTrue(lead["lead_id"].startswith("lead:agency:"))
        moved = Path(self.tmp.name) / "moved.yaml"   # the agency now under another tracking host
        moved.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                         "email.harborstaffing.com, tracker_host: links.harborstaffing.com}\n", encoding="utf-8")
        for setting in (str(Path(self.tmp.name) / "absent.yaml"), "", str(moved)):
            with self.subTest(setting=setting), unittest.mock.patch.dict(os.environ, {private_sites.ENV: setting}):
                with self.assertRaisesRegex(ValueError, "agency_setting_missing"):   # fail closed: no export at all (D125)
                    files(build(self.store))
                self.assertIsNone(candidate_from_lead(self.store.one("SELECT * FROM leads WHERE url = ?", (private_link,)), POLICY))
        self.assertNotIn("synthetic-private-token", json.dumps(files(build(self.store))))   # restored: exported without it
        from careeros.cli import _record_alert
        from careeros.discovery.alerts import parse_eml_bytes
        linkedin = '<a href="https://www.linkedin.com/comm/jobs/view/4055555555/">Claims Analyst</a><br>Harbor Staffing · Remote'
        with unittest.mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # the setting gone: no title-only suppression
            later = _record_alert(self.ctx, parse_eml_bytes(alert_email(linkedin)), self.boards)
        self.assertEqual((later["lead_new"], later["lead_seen_before"]), (1, 0))

    def test_an_agency_card_never_hides_another_sites_lead_on_its_title(self):
        google = FakeGoogle()
        linkedin = '<a href="https://www.linkedin.com/comm/jobs/view/4055555555/">Claims Analyst</a><br>Harbor Staffing · Remote'
        google.messages = {"m1": agency_email(agency_card("Claims Analyst", AG_1)), "m2": alert_email(linkedin)}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))
        self.assertEqual(sorted(r["site"] for r in self.store.all("SELECT site FROM leads")), ["harborstaffing", "linkedin"])

    def test_another_sites_lead_never_hides_an_agency_card_on_its_title(self):
        google = FakeGoogle()
        linkedin = '<a href="https://www.linkedin.com/comm/jobs/view/4055555555/">Claims Analyst</a><br>Harbor Staffing · Remote'
        google.messages = {"m1": alert_email(linkedin), "m2": agency_email(agency_card("Claims Analyst", AG_1))}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))
        self.assertEqual(sorted(r["site"] for r in self.store.all("SELECT site FROM leads")), ["harborstaffing", "linkedin"])

    def test_agency_cards_with_the_same_job_number_are_one_lead(self):
        google = FakeGoogle()
        job = "Job ID: 04530-0012345678"
        google.messages = {"m1": agency_email(agency_card("Claims Analyst", AG_1, description=job)),
                           "m2": agency_email(agency_card("Claims Analyst", AG_2, description=job), subject="More jobs")}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))   # a possible repeat (H8, D192 part 3)
        first = self.store.one("SELECT lead_id FROM leads WHERE url = ?", (AG_1,))[0]
        self.assertEqual(self.store.one("SELECT repeat_of FROM leads WHERE url = ?", (AG_2,))[0], first)

    def test_another_sites_lead_with_the_agency_cards_job_number_is_the_same_job(self):
        google = FakeGoogle()
        linkedin = ('<a href="https://www.linkedin.com/comm/jobs/view/4055555555/">Claims Analyst</a>'
                    "<br>Harbor Staffing · Remote · req=04530-0012345678")
        google.messages = {"m1": agency_email(agency_card("Claims Analyst", AG_1, description="Job ID: 04530-0012345678")),
                           "m2": alert_email(linkedin)}
        counts = _gmail_pull(self.ctx, self.boards, "synthetic query", gmail=self.reader(google))
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (2, 0))   # a possible repeat (H8, D192 part 3)
        agency = self.store.one("SELECT lead_id FROM leads WHERE url = ?", (AG_1,))[0]
        self.assertEqual(self.store.one("SELECT repeat_of FROM leads WHERE site = 'linkedin'")[0], agency)

    def test_expired_access_asks_you_to_reconnect(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            _gmail_pull(self.ctx, self.boards, "q", gmail=self.reader(FakeGoogle(refresh_ok=False)))
        self.assertEqual(ctx.exception.code, "reconnect_needed")

    def test_not_connected(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            gmail.GmailClient(MemoryCredentials())
        self.assertEqual(ctx.exception.code, "not_connected")

    def test_disconnect_revokes_and_forgets(self):
        revoked = []
        http = httpx.Client(transport=httpx.MockTransport(lambda r: revoked.append(r.url.path) or httpx.Response(200)))
        self.assertTrue(gmail.disconnect(self.creds, http=http))
        self.assertEqual(revoked, ["/revoke"])
        self.assertIsNone(self.creds.read(gmail.CREDENTIAL_TARGET))
        self.assertFalse(gmail.disconnect(self.creds, http=http))



class EnvironmentTrustTests(unittest.TestCase):
    """Proxy variables and .netrc in the environment never steer CareerOS's job-board or Gmail requests (release finding
    R1, 2026-10-13); the employer-website reader already ignored them."""

    def test_the_job_board_client_ignores_the_environment(self):
        board = HttpClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.assertIs(board.client.trust_env, False)

    def test_the_gmail_clients_ignore_the_environment(self):
        creds = MemoryCredentials()
        creds.write(gmail.CREDENTIAL_TARGET, json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s"}))
        client = gmail.GmailClient(creds)
        try:
            self.assertIs(client.http.trust_env, False)
        finally:
            client.close()
        made = []

        class Recorder(httpx.Client):
            def __init__(self, **kwargs):
                made.append(kwargs)
                super().__init__(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)
        with unittest.mock.patch.object(gmail.httpx, "Client", Recorder):
            self.assertTrue(gmail.disconnect(creds))
        self.assertEqual([kwargs.get("trust_env") for kwargs in made], [False])

if __name__ == "__main__":
    unittest.main()
