"""Read job-alert emails from your Gmail (read-only) and hand them to the same reader as saved .eml files.

Setup is yours: you create a Google Cloud "Desktop app" OAuth client and approve access in your own browser
(`careeros gmail connect`). CareerOS asks only for gmail.readonly, searches only the alert senders in
policy `operations.gmail_query`, and keeps only job links, titles, the text beside them and the alert's sender and
subject. The refresh token is stored in Windows Credential Manager as 'CareerOS/gmail'; nothing secret is written to
disk, the database or logs. Google's read-only scope covers the whole mailbox: the sender filter is CareerOS's own
restriction, not Google's.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDENTIAL_TARGET = "CareerOS/gmail"
MAX_MESSAGES = 300


class GmailError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def load_client(path: Path) -> dict:
    """The OAuth client file you downloaded from Google Cloud (type: Desktop app)."""
    if not path.is_file():
        raise GmailError("client_file_missing", f"OAuth client file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    installed = data.get("installed")
    if not isinstance(installed, dict) or not installed.get("client_id") or not installed.get("client_secret"):
        raise GmailError("client_file_wrong_type", "the client file must be for a 'Desktop app' OAuth client")
    return {"client_id": installed["client_id"], "client_secret": installed["client_secret"]}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def connect(client: dict, credentials, *, open_browser, http: httpx.Client | None = None, timeout: float = 300) -> str:
    """Browser sign-in on a one-shot 127.0.0.1 listener (state + PKCE checked). Returns the connected address."""
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    received: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # the URL carries the one-time code
            pass

        def do_GET(self):
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if urlsplit(self.path).path == "/" and ("code" in query or "error" in query):
                received.update(query)
                body, status = "CareerOS: Gmail sign-in finished. You can close this tab.", 200
            else:
                body, status = "Not found", 404
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()), "code_challenge_method": "S256"})
    waited = 0.0
    try:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        while not received and waited < timeout:
            server.handle_request()
            waited += server.timeout
    finally:
        server.server_close()
    if not received:
        raise GmailError("timeout", "no answer from Google sign-in; run `careeros gmail connect` again")
    if received.get("error"):
        raise GmailError("denied", f"Google sign-in was not completed ({received['error']})")
    if not hmac.compare_digest(received.get("state", ""), state):
        raise GmailError("state_mismatch", "sign-in answer did not match this request; nothing was saved")
    own = http is None
    http = http or httpx.Client(timeout=30, trust_env=False)
    try:
        response = http.post(TOKEN_URL, data={"code": received.get("code", ""), "client_id": client["client_id"],
                                              "client_secret": client["client_secret"], "redirect_uri": redirect,
                                              "grant_type": "authorization_code", "code_verifier": verifier})
        if response.status_code != 200:
            raise GmailError("token_exchange_failed", f"Google refused the sign-in code (HTTP {response.status_code})")
        token = response.json()
        granted = set((token.get("scope") or "").split())
        if SCOPE not in granted:
            raise GmailError("scope_not_granted", "read-only Gmail access was not granted; nothing was saved")
        if not token.get("refresh_token"):
            raise GmailError("no_refresh_token", "Google did not return a long-lived token; remove CareerOS's access "
                                                 "in your Google Account and connect again")
        credentials.write(CREDENTIAL_TARGET, json.dumps({**client, "refresh_token": token["refresh_token"]}))
        return GmailClient(credentials, http=http).address()
    finally:
        if own:
            http.close()


def disconnect(credentials, http: httpx.Client | None = None) -> bool:
    """Revoke the token at Google (best effort) and remove it from Credential Manager."""
    stored = credentials.read(CREDENTIAL_TARGET)
    if not stored:
        return False
    own = http is None
    http = http or httpx.Client(timeout=30, trust_env=False)
    try:
        http.post("https://oauth2.googleapis.com/revoke", data={"token": json.loads(stored)["refresh_token"]})
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    finally:
        if own:
            http.close()
    return credentials.delete(CREDENTIAL_TARGET)


class GmailClient:
    def __init__(self, credentials, http: httpx.Client | None = None):
        from careeros.demo.sandbox import refuse
        refuse("Gmail")
        stored = credentials.read(CREDENTIAL_TARGET)
        if not stored:
            raise GmailError("not_connected", "Gmail is not connected (run `careeros gmail connect`)")
        self._creds = json.loads(stored)
        self._own = http is None
        self.http = http or httpx.Client(timeout=30, trust_env=False)
        self._access: str | None = None

    def close(self) -> None:
        if self._own:
            self.http.close()

    def _token(self) -> str:
        if self._access is None:
            response = self.http.post(TOKEN_URL, data={"client_id": self._creds["client_id"], "client_secret": self._creds["client_secret"],
                                                       "refresh_token": self._creds["refresh_token"], "grant_type": "refresh_token"})
            if response.status_code in (400, 401) and "invalid_grant" in response.text:
                raise GmailError("reconnect_needed", "Gmail access expired or was removed: run `careeros gmail connect` again")
            if response.status_code != 200:
                raise GmailError("token_refresh_failed", f"Google token refresh failed (HTTP {response.status_code})")
            self._access = response.json()["access_token"]
        return self._access

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self.http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self._token()}"})
        if response.status_code == 401:
            raise GmailError("reconnect_needed", "Gmail access expired: run `careeros gmail connect` again")
        if response.status_code != 200:
            raise GmailError(f"http_{response.status_code}", f"Gmail API error (HTTP {response.status_code})")
        return response.json()

    def address(self) -> str:
        return self._get("/profile").get("emailAddress", "")

    def search(self, query: str, limit: int = MAX_MESSAGES) -> list[str]:
        ids, page = [], None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(ids))}
            if page:
                params["pageToken"] = page
            data = self._get("/messages", params)
            ids.extend(m["id"] for m in data.get("messages", []))
            page = data.get("nextPageToken")
            if not page:
                break
        return ids

    def raw(self, message_id: str) -> bytes:
        if not message_id.isalnum():
            raise GmailError("bad_message_id")
        data = self._get(f"/messages/{message_id}", {"format": "raw"})
        return base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
