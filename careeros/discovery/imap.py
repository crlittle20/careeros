"""Read job-alert emails from Gmail over IMAP with an app password you created and stored yourself.

The alternative to `gmail.py` (Google Cloud sign-in). Setup: Google account → App passwords → store it with
`cmdkey /generic:CareerOS/gmail-imap /user:<address> /pass`. CareerOS never sees the password outside Windows
Credential Manager, never logs it, opens the mailbox read-only (IMAP EXAMINE) and reads messages with BODY.PEEK, so
nothing is marked read, moved or deleted. The search is the same Gmail query as the API route (Gmail's X-GM-RAW
extension), and message ids are Gmail's own (X-GM-MSGID in hex), so both routes share one "already read" list.

An app password can do more than read (it is a full mail password); the read-only behaviour is this code's
restriction. A dedicated job-alerts Gmail account keeps that exposure small.
"""
from __future__ import annotations

import imaplib
import re
import ssl

from .gmail import GmailError

CREDENTIAL_TARGET = "CareerOS/gmail-imap"
HOST, PORT = "imap.gmail.com", 993
MAX_MESSAGES = 300
MAX_BYTES = 5 * 1024 * 1024
_LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
_META = re.compile(rb"UID (\d+)|X-GM-MSGID (\d+)|RFC822\.SIZE (\d+)")


def _connect():
    from careeros.demo.sandbox import refuse
    refuse("Gmail over IMAP")
    return imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=30)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapGmailClient:
    """Same interface as gmail.GmailClient (search / raw / close / address) so the alert pull works with either."""

    def __init__(self, credentials, connect=_connect):
        entry = credentials.read_entry(CREDENTIAL_TARGET)
        if not entry or not entry[1].strip():
            raise GmailError("not_connected", "no Gmail app password stored as 'CareerOS/gmail-imap'")
        user, password = entry[0].strip(), "".join(entry[1].split())  # Google shows the password in groups of four
        if "@" not in user:
            raise GmailError("imap_user_missing", "store your Gmail address as the credential's user name (/user:...)")
        self.user = user
        self.conn = connect()
        try:
            self.conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            self._logout()
            raise GmailError("imap_login_failed", "Gmail refused the app password (it may have been removed, or IMAP "
                                                  "is off): create a new one and store it again") from exc
        self._select_all_mail()
        self._uids: dict[str, bytes] = {}

    def _logout(self) -> None:
        try:
            self.conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def _select_all_mail(self) -> None:
        """Gmail's 'All Mail' (named per language, found by its \\All flag), read-only; INBOX if it cannot be found."""
        status, lines = self.conn.list()
        mailbox = "INBOX"
        for line in lines if status == "OK" else []:
            match = _LIST_LINE.match(line if isinstance(line, bytes) else b"")
            if match and b"\\All" in match.group("flags").split():
                mailbox = match.group("name").decode("utf-8", "replace")
                break
        status, _ = self.conn.select(mailbox if mailbox.startswith('"') else _quote(mailbox), readonly=True)
        if status != "OK":
            raise GmailError("imap_select_failed", "could not open the mailbox read-only")

    def address(self) -> str:
        return self.user

    def search(self, query: str, limit: int = MAX_MESSAGES) -> list[str]:
        status, data = self.conn.uid("SEARCH", "X-GM-RAW", _quote(query))
        if status != "OK":
            raise GmailError("imap_search_failed", "Gmail search failed")
        uids = (data[0] or b"").split()[-limit:]
        if not uids:
            return []
        status, data = self.conn.uid("FETCH", b",".join(uids).decode(), "(X-GM-MSGID RFC822.SIZE)")
        ids = []
        for item in data if status == "OK" else []:
            line = item[0] if isinstance(item, tuple) else item
            if not isinstance(line, bytes):
                continue
            found = {}
            for uid, msgid, size in _META.findall(line):
                found.update({k: v for k, v in (("uid", uid), ("msgid", msgid), ("size", size)) if v})
            if "uid" in found and "msgid" in found and int(found.get("size", b"0")) <= MAX_BYTES:
                message_id = format(int(found["msgid"]), "x")
                self._uids[message_id] = found["uid"]
                ids.append(message_id)
        return ids

    def raw(self, message_id: str) -> bytes:
        uid = self._uids.get(message_id)
        if uid is None:
            raise GmailError("bad_message_id")
        status, data = self.conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")  # PEEK: never marks the email as read
        for item in data if status == "OK" else []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1]
        raise GmailError("imap_fetch_failed", "could not read an alert email")

    def close(self) -> None:
        self._logout()
