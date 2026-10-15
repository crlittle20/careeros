"""Gmail connector: sign-in checks, token handling and the alert pull. Google is simulated; no real account is used."""
import base64
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
from tests.test_alerts import alert_email
from tests.test_saved import POLICY

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT = {"client_id": "synthetic-client.apps.example.com", "client_secret": "synthetic-secret"}  # release-scan: fake (a test placeholder, not a credential)


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

    def test_alerts_become_leads_once_and_messages_are_remembered_not_stored(self):
        google = FakeGoogle()
        counts = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(google))
        self.assertEqual(counts["gmail_messages"], 2)
        self.assertEqual(counts["lead_new"], 4)  # linkedin, indeed, nhg + the unreadable greenhouse link
        self.assertEqual(counts["lead_fetch_failed"], 1)  # the Greenhouse link (simulated 404) is kept as a lead
        self.assertEqual([tuple(r) for r in self.store.all("SELECT message_id, source FROM alert_messages ORDER BY message_id")],
                         [("m1", "gmail"), ("m2", "gmail")])
        again = _gmail_pull(self.ctx, self.boards, "newer_than:3d", gmail=self.reader(google))
        self.assertEqual(again["gmail_messages"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], 5)
        everything = " ".join(str(v) for t in ("leads", "alert_messages", "events") for r in self.store.all(f"SELECT * FROM {t}") for v in r)
        self.assertNotIn("candidate@example.com", everything)  # the recipient never lands in the database

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
