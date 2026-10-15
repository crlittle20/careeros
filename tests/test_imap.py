"""Gmail over IMAP with an app password: read-only mailbox, PEEK fetches, shared message ids. Gmail is simulated."""
import imaplib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from careeros.cli import _gmail_method, _gmail_pull, _gmail_reader
from careeros.core.clock import FixedClock
from careeros.core.credentials import MemoryCredentials
from careeros.db.store import open_store
from careeros.discovery import gmail, imap
from careeros.discovery.http import HttpClient
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile

from tests.helpers import JOB_MATRIX
from tests.test_alerts import alert_email
from tests.test_saved import POLICY

FIXTURES = Path(__file__).parent / "fixtures"
MSGIDS = {b"11": 1278455344230334865, b"12": 1278455344230334866}


class FakeImap:
    """Answers like imaplib does for the few commands CareerOS uses, and records what it was asked."""

    def __init__(self, password="abcdefghijklmnop", all_mail='"[Gmail]/All Mail"'):  # release-scan: fake (a test placeholder, not a credential)
        self.password, self.all_mail, self.calls, self.selected = password, all_mail, [], None

    def login(self, user, password):
        self.calls.append(("login", user))
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Success"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" ' + self.all_mail.encode(),
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"11 12"]
        if args[1] == "(X-GM-MSGID RFC822.SIZE)":
            return "OK", [f"{n} (UID {uid.decode()} X-GM-MSGID {MSGIDS[uid]} RFC822.SIZE 2048)".encode()
                          for n, uid in enumerate(MSGIDS, 1)]
        if args[1] == "(BODY.PEEK[])":
            return "OK", [(f"1 (UID {args[0]} BODY[] {{2048}}".encode(), alert_email()), b")"]
        return "NO", [b"unexpected"]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


def stored(user="jobs@example.com", password="abcd efgh ijkl mnop"):
    creds = MemoryCredentials()
    creds.write(imap.CREDENTIAL_TARGET, password, user=user)
    return creds


class ImapTests(unittest.TestCase):
    def test_mailbox_is_opened_read_only_and_messages_are_never_marked_read(self):
        fake = FakeImap()
        client = imap.ImapGmailClient(stored(), connect=lambda: fake)
        self.assertEqual(fake.selected, ('"[Gmail]/All Mail"', True))  # IMAP EXAMINE: no flags change, nothing moves
        ids = client.search("newer_than:3d from:(jobalerts-noreply)")
        self.assertEqual(ids, [format(MSGIDS[b"11"], "x"), format(MSGIDS[b"12"], "x")])  # same ids as the Gmail API
        self.assertIn(("SEARCH", "X-GM-RAW", '"newer_than:3d from:(jobalerts-noreply)"'), fake.calls)  # the policy query
        self.assertTrue(client.raw(ids[0]).startswith(b"From:") or b"Subject:" in client.raw(ids[0]))
        fetches = [c for c in fake.calls if c[0] == "FETCH"]
        self.assertTrue(all("BODY[]" not in c[2] or "PEEK" in c[2] for c in fetches))
        client.close()
        self.assertEqual(fake.calls[0], ("login", "jobs@example.com"))  # spaces removed from the password
        self.assertEqual(fake.calls[-1], ("logout",))

    def test_localised_all_mail_and_wrong_password(self):
        fake = FakeImap(all_mail='"[Google Mail]/All Mail"')
        imap.ImapGmailClient(stored(), connect=lambda: fake)
        self.assertEqual(fake.selected, ('"[Google Mail]/All Mail"', True))
        with self.assertRaises(gmail.GmailError) as ctx:
            imap.ImapGmailClient(stored(password="wrong wrong wrong"), connect=FakeImap)
        self.assertEqual(ctx.exception.code, "imap_login_failed")

    def test_missing_or_incomplete_credentials(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            imap.ImapGmailClient(MemoryCredentials(), connect=FakeImap)
        self.assertEqual(ctx.exception.code, "not_connected")
        with self.assertRaises(gmail.GmailError) as ctx:
            imap.ImapGmailClient(stored(user="CareerOS"), connect=FakeImap)
        self.assertEqual(ctx.exception.code, "imap_user_missing")

    def test_method_choice_prefers_google_sign_in(self):
        creds = stored()
        self.assertEqual(_gmail_method(creds), "imap")
        creds.write(gmail.CREDENTIAL_TARGET, json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
        self.assertEqual(_gmail_method(creds), "api")
        self.assertIsNone(_gmail_method(MemoryCredentials()))
        self.assertIsNone(_gmail_reader(MemoryCredentials()))


class ImapPullTests(unittest.TestCase):
    def test_imap_alerts_flow_into_leads_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = open_store(":memory:", FixedClock("2026-10-12T12:00:00.000Z"), POLICY.fingerprint)
        ctx = Context(store, POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(tmp.name))
        boards = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), min_interval=0, sleep=lambda s: None)
        self.addCleanup(boards.close)
        counts = _gmail_pull(ctx, boards, "q", gmail=imap.ImapGmailClient(stored(), connect=FakeImap))
        self.assertEqual(counts["gmail_messages"], 2)
        self.assertGreaterEqual(counts["lead_new"], 3)
        again = _gmail_pull(ctx, boards, "q", gmail=imap.ImapGmailClient(stored(), connect=FakeImap))
        self.assertEqual(again["gmail_messages"], 0)


if __name__ == "__main__":
    unittest.main()
