"""Local review server: 127.0.0.1 only, secret token on every request, Host header checked (DNS rebinding),
one-time form keys bound to (action, target), idempotent writes (a double click never records twice), and the
single-writer lock around each write.

Actions: label a job, record an application to it, update an application's status, dismiss an email lead.
"""
from __future__ import annotations

import hmac
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from careeros.core.locks import LockError, SingleWriterLock

from .applications import ApplicationConflict, application_matrix, record_application, update_application
from .labels import record_label
from .leads import dismiss_lead
from .report import build_report, esc, last_review_at

MAX_BODY = 4096
MAX_PASTE_BODY = 200_000   # a pasted posting (Part 6)
MAX_FORM_KEYS = 20000
ACTIONS = ("label", "label_night", "apply", "status", "lead", "dismiss", "paste")


def parse_channel(choice: str, agency_text: str) -> tuple[str, str | None]:
    """The form offers 'direct', 'agency:<name>' (an agency named in the private settings, if any), 'agency' (+ a typed name), 'referral'."""
    if choice.startswith("agency:"):
        return "agency", choice.split(":", 1)[1]
    return choice, (agency_text.strip() or None) if choice == "agency" else None


def make_handler(store, profile, policy, token: str, lock_path, port_holder: dict, *, app_matrix=None, since: str | None = None):
    form_keys: dict = {}
    app_matrix = app_matrix or application_matrix()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # keep request lines (with the token) out of the console
            pass

        def _authorized(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            query = parse_qs(urlsplit(self.path).query)
            supplied = (query.get("t") or [""])[0]
            return host in ("127.0.0.1", "localhost") and hmac.compare_digest(supplied, token)

        def _send(self, status: int, body: str, extra: dict | None = None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _problem(self, status: int, message: str):
            return self._send(status, f'<!doctype html><meta charset="utf-8"><p>{esc(message)}</p>'
                                      f'<p><a href="/?t={esc(token)}">Back to the review page</a></p>')

        def do_GET(self):
            if urlsplit(self.path).path != "/" or not self._authorized():
                return self._send(403, "forbidden")
            page = build_report(store, profile, policy, token=token, form_keys=form_keys, since=since, app_matrix=app_matrix)
            for stale in list(form_keys)[:max(0, len(form_keys) - MAX_FORM_KEYS)]:  # oldest first; bounded memory
                del form_keys[stale]
            self._send(200, page)

        def do_POST(self):
            action = urlsplit(self.path).path.strip("/")
            if action not in ACTIONS or not self._authorized():
                return self._send(403, "forbidden")
            length = int(self.headers.get("Content-Length") or 0)
            if length > (MAX_PASTE_BODY if action == "paste" else MAX_BODY):
                return self._send(413, "too large")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            target, key = form.get("target", ""), form.get("key", "")
            if form_keys.get(key) != (action, target):
                return self._problem(400, "This form is out of date. Reload the review page and try again.")
            anchor = target
            try:
                with SingleWriterLock(lock_path):
                    if action == "label":
                        record_label(store, target, form.get("label", ""), form.get("reason", ""), key)
                    elif action == "label_night":
                        label, reason = form.get("label", ""), form.get("reason", "")
                        if label == "skip_schedule":  # counts toward the NIGHT schedule-fit false-positive rate
                            label, reason = "skip", "schedule doesn't fit" + (f": {reason}" if reason else "")
                        record_label(store, target, label, reason, key, profile="night")
                        anchor = f"night-{target}"
                    elif action == "apply":
                        channel, agency = parse_channel(form.get("channel", ""), form.get("agency", ""))
                        record_application(store, app_matrix, target, channel, agency=agency, key=key)
                    elif action == "status":
                        update_application(store, app_matrix, target, form.get("state", ""), notes=form.get("notes", ""), key=key)
                    elif action == "dismiss":
                        from .dismiss import dismiss
                        dismiss(store, target, form.get("reason", ""), form.get("note", ""))
                        anchor = "shortlist"
                    elif action == "paste":
                        from careeros.core.transitions import load_matrix
                        from careeros.discovery.manual_intake import intake
                        from careeros.paths import CONFIG_DIR
                        from careeros.pipeline import Context, process
                        ctx = Context(store, policy, profile, load_matrix(CONFIG_DIR / "workflows" / "job.json"))
                        result = intake(ctx, form.get("text", ""), (form.get("url") or "").strip() or None,
                                        company=(form.get("company") or "").strip() or None)
                        process(ctx)
                        anchor = result["job_id"]
                    else:
                        dismiss_lead(store, target, key=key)
                        anchor = "leads"
            except LockError:
                return self._problem(409, "A CareerOS run is writing right now. Go back and try again in a moment.")
            except ApplicationConflict as exc:
                return self._problem(409, f"Not recorded. {exc}. If you really did apply twice, record it with "
                                          f"'careeros applied {target} --channel ... --second-channel'.")
            except ValueError as exc:
                return self._problem(400, f"Not recorded: {exc}")
            self._send(303, "", {"Location": f"/?t={token}#{quote(anchor, safe=':')}"})

    return Handler


def serve(store, profile, policy, lock_path, port: int = 8790, open_browser: bool = True) -> None:
    since = last_review_at(store)
    try:
        with SingleWriterLock(lock_path):
            store.writer("review").event("review_opened", outcome="informational", entity_type="review", entity_id="daily",
                                         actor_type="human", actor_id="user", metadata={"previous": since})
    except LockError:
        print("Note: a run is in progress, so this visit is not recorded as a review.")
    token = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", port), make_handler(store, profile, policy, token, lock_path, {}, since=since))
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"Review page: {url}\nPress Ctrl+C to stop.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
