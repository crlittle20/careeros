"""Local review server: 127.0.0.1 only, secret token on every request, Host header checked (DNS rebinding),
one-time form keys bound to (action, target), idempotent writes (a double click never records twice), and the
single-writer lock around each write. A confirmation's key is also bound, on the server, to what its page showed
(Codex CR-2026-11-01-OBS-REVIEW, F3): the dismissal an Undo undoes, and the reason, note or channel of Dismiss, "I
applied" and Void. Confirm records exactly that; when the dismissal is no longer the latest event, or the form's choices
differ, nothing is recorded and a fresh confirmation is shown.

Actions: label a job, record an application to it, update an application's status, dismiss a job or an email lead,
add a pasted posting, confirm or reject a likely match for a lead (2026-11-02: the form's target names the lead and the
job its card showed; anything else waiting now records nothing).

Confirmation (OPP-16; ADR 0007, amendment of 2026-11-01, "Confirmation plus undo"): Dismiss, "I applied", Undo of a
dismissal and Void of an application (ASKS) record nothing. They answer with a page naming the posting (its title, employer and job id, and the channel or the
chosen reason) whose Confirm button is a new one-time form for `confirm_<action>`; only that records. Cancel is a link
back. "I applied" offers no Confirm while an assistance session for the role is open or uncertain: it names the
`careeros assist close` command instead, and recording checks again. After an action the page returns to the card the
form came from (`from`: the shortlist card `sl-<job>`, a NIGHT card `night-<job>`, else the posting's card `<job>`).
"""
from __future__ import annotations

import hmac
import secrets
import sys
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from careeros.core.locks import LockError, SingleWriterLock
from careeros.db.store import IdempotencyCollision

from .applications import (CHANNELS, CONFIRM_CODES, VOID, ApplicationConflict, ApplicationHold, SessionOpen,
                           application_matrix, record_application, sessions_waiting, update_application, void_refusal)
from .dismiss import DismissalChanged
from .labels import record_label
from .leads import confirm_likely, dismiss_lead, reject_likely
from .notices import dismiss_notice
from .report import CSP, _form, build_report, esc, last_review_at

MAX_BODY = 4096
MAX_PASTE_BODY = 200_000   # a pasted posting (Part 6)
MAX_FORM_KEYS = 20000
ACTIONS = ("label", "label_night", "apply", "status", "lead", "notice", "dismiss", "paste", "reinstate", "void",
           "confirm_apply", "confirm_dismiss", "confirm_reinstate", "confirm_void",
           "lead_confirm", "lead_reject")   # a likely match for a lead (2026-11-02): the form names the lead and the job shown
ASKS = (   # a confirmation page first; only its Confirm (confirm_<action>) records
    "dismiss",   # Dismiss a posting, with the chosen reason
    "apply",     # "I applied", with the chosen channel
    "reinstate",  # Undo a dismissal ("Dismissed recently")
    "void",      # Void an application recorded by mistake, with the reason
)
ORIGINS = {"sl": "sl-{}", "night": "night-{}"}   # the card a form came from; anything else is the posting's own card


def parse_channel(choice: str, agency_text: str) -> tuple[str, str | None]:
    """The form offers 'direct', 'agency:<name>' (an agency named in the private settings, if any), 'agency' (+ a typed name), 'referral'."""
    if choice.startswith("agency:"):
        return "agency", choice.split(":", 1)[1]
    return choice, (agency_text.strip() or None) if choice == "agency" else None


class ConfirmationChanged(Exception):
    """A Confirm whose choices differ from the ones its page showed (F3): nothing is recorded; a fresh confirmation."""


def choices(kind: str, form: dict) -> dict:
    """What a confirmation page shows for `kind`, canonical: the dismissal's reason and note, the channel and agency of
    "I applied", the reason of a void (F3). Its Confirm must carry the same; an Undo is bound to its dismissal instead."""
    if kind == "dismiss":
        return {"reason": form.get("reason", ""), "note": (form.get("note") or "").strip()}
    if kind == "apply":
        channel, agency = parse_channel(form.get("channel", ""), form.get("agency", ""))
        return {"channel": channel, "agency": agency}
    if kind == "void":
        return {"reason": (form.get("reason") or "").strip()}
    return {}


def anchor_for(target: str, origin: str) -> str:
    """Where the page returns after an action on `target`: the card the form came from."""
    return ORIGINS.get(origin, "{}").format(target)


def small_page(title: str, body: str) -> str:
    """A small page of the review server, under the review page's own Content-Security-Policy."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<title>{esc(title)}</title></head><body>{body}</body></html>')


def posting_lines(job) -> str:
    """The posting a confirmation is about: title, employer and job id, escaped (postings are untrusted input)."""
    return (f'<ul><li>Posting: <b>{esc(job["title"])}</b></li><li>Employer: {esc(job["employer_name"])}</li>'
            f'<li>Job id: <code>{esc(job["job_id"])}</code></li></ul>')


@contextmanager
def _swept(store, policy, profile):
    """Around every action of the review page, under its writer lock: every handoff file in the vault is removed before
    the action and again after it, whatever it did (browser Fill is paused in this release, the owner's D191), and the
    database must still be one this engine may write as it is: a review page left running across an upgrade or a
    restore records nothing (Codex's D190 design review, H9)."""
    from careeros.db.store import MigrationPending, SchemaTooNew

    from .assist import vault_root_of, withdraw_handoffs
    root = vault_root_of(store)
    if root is not None:
        withdraw_handoffs(store, policy, profile, root)
    try:
        store.check_schema()
    except (SchemaTooNew, MigrationPending) as exc:
        raise ValueError(f"the database changed under this review page ({exc}). Stop the review page and start it "
                         "again") from exc
    try:
        yield
    finally:
        if root is not None:
            withdraw_handoffs(store, policy, profile, root)


def make_handler(store, profile, policy, token: str, lock_path, port_holder: dict, *, app_matrix=None, since: str | None = None):
    form_keys: dict = {}
    shown: dict = {}   # a confirmation's form key -> (its choices, the dismissal an Undo undoes): what its page showed (F3)
    previews: dict = {}   # a label form's key -> the capture its card showed (the D190 design, section 3, "The preview")
    app_matrix = app_matrix or application_matrix()

    def _card_preview(key: str, form: dict) -> dict | None:
        """The capture the card behind this form showed, when the form carries its digest; else None (the label is then
        refused for want of a preview, unless it is Skip)."""
        captured = previews.get(key)
        if captured is None or not form.get("digest") or form["digest"] != captured["digest"]:
            return None
        return captured

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
            self.send_header("Content-Security-Policy", CSP)   # every response, error pages included (D88)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _problem(self, status: int, message: str):
            return self._send(status, f'<!doctype html><meta charset="utf-8"><p>{esc(message)}</p>'
                                      f'<p><a href="/?t={esc(token)}">Back to the review page</a></p>')

        def _back(self, target: str, origin: str) -> str:
            return f"/?t={token}#{quote(anchor_for(target, origin), safe=':')}"

        def _confirm_form(self, action: str, target: str, body: str, form: dict, undoes: str | None = None) -> str:
            """A Confirm form whose key is bound, here on the server, to what the page shows (F3)."""
            key = secrets.token_hex(12)
            shown[key] = (choices(action.removeprefix("confirm_"), form), undoes)
            return _form(action, target, token, form_keys, body, key=key)

        def _hold_page(self, target: str, form: dict, exc: ApplicationHold) -> str:
            """The application was not recorded yet: show why and ask for the reason, with a fresh one-time form. You
            confirmed the posting already, so the form records (confirm_apply) once the reasons are given."""
            codes = exc.check.codes()
            fields = (f'<input type="hidden" name="channel" value="{esc(form.get("channel", ""))}">'
                      f'<input type="hidden" name="agency" value="{esc(form.get("agency", ""))}">'
                      f'<input type="hidden" name="from" value="{esc(form.get("from", ""))}">')
            if "applied_before" in codes:
                fields += ('<p><label>Why apply to this role again? <input type="text" name="reapply_reason" required '
                           'maxlength="300"></label></p>')
            if any(c in CONFIRM_CODES for c in codes):
                fields += ('<p><label>Your explanation (kept with the record; it never releases a submission hold): '
                           '<input type="text" name="confirm_reason" required maxlength="300"></label></p>')
            items = "".join(f"<li>{esc(r['detail'])}</li>" for r in exc.check.reasons if r["level"] in ("hold", "warn"))
            job = store.one("SELECT job_id, title, employer_name FROM jobs WHERE job_id = ?", (target,))
            return small_page("Not recorded yet", (posting_lines(job) if job else "")
                        + f'<p>Not recorded yet. This needs your reason:</p><ul>{items}</ul>'
                        + self._confirm_form("confirm_apply", target, fields + "<button>Record it</button>", form)
                        + f'<p><a href="{esc(self._back(target, form.get("from", "")))}">Back to the review page</a></p>')

        def _ask(self, action: str, target: str, form: dict, notice: str = "") -> tuple[int, str]:
            """Dismiss, "I applied", Undo and Void record nothing yet: a page naming the posting, with Confirm (a new
            one-time form for confirm_<action>, carrying the same choices, its key bound to them and, for Undo, to the
            dismissal shown) and Cancel (a link back). `notice` heads a confirmation shown again. ValueError for an
            unknown job or application, or an incomplete form."""
            if action == "void":
                return self._ask_void(target, form, notice)
            job = store.one("SELECT job_id, title, employer_name FROM jobs WHERE job_id = ?", (target,))
            if job is None:
                raise ValueError("unknown_job")
            origin = form.get("from", "")
            back = esc(self._back(target, origin))
            keep = f'<input type="hidden" name="from" value="{esc(origin)}">'
            undoes = None
            if action == "dismiss":
                from .dismiss import REASONS
                reason, note = form.get("reason", ""), (form.get("note") or "").strip()
                if reason not in REASONS:
                    raise ValueError("choose a reason for the dismissal")
                question = (f'<p>Dismiss this posting? Reason: <b>{esc(reason.replace("_", " "))}</b>'
                            + (f' ({esc(note)})' if note else "") + '</p>')
                keep += (f'<input type="hidden" name="reason" value="{esc(reason)}">'
                         f'<input type="hidden" name="note" value="{esc(note)}">')
                button = "Confirm: dismiss it"
            elif action == "reinstate":
                from .dismiss import current
                was = current(store, target)
                if was is None:
                    raise ValueError("not_dismissed: the posting's latest event is not a dismissal")
                undoes = was["dismissal_id"]   # Confirm undoes this dismissal, or nothing (F3)
                question = (f'<p>Undo your dismissal of this posting? You dismissed it on {esc(was["at"][:10])}: '
                            f'<b>{esc(was["reason_code"].replace("_", " "))}</b>'
                            + (f' ({esc(was["reason_text"])})' if was["reason_text"] else "") + '. It returns to the '
                            'shortlist if it qualifies; the dismissal stays in its history.</p>')
                keep += '<p><input type="text" name="note" maxlength="300" placeholder="why undo it (optional)"></p>'
                button = "Confirm: undo the dismissal"
            else:
                channel, agency = parse_channel(form.get("channel", ""), form.get("agency", ""))
                if channel not in CHANNELS:
                    raise ValueError("unknown_channel")
                if channel == "agency" and not agency:
                    raise ValueError("agency_name_required")
                waiting = sessions_waiting(store, target)
                if waiting:   # it is recorded by closing the session (OPP-77): no Confirm, the command instead
                    exc = SessionOpen(waiting[-1])
                    return 409, small_page("Not recorded", posting_lines(job) + f'<p>Not recorded: {esc(exc)}</p>'
                                     f'<p>Run: <code>{esc(exc.command)}</code></p><p><a href="{back}">Back to the review page</a></p>')
                question = (f'<p>Record that you applied to this posting '
                            f'<b>{esc("directly" if channel == "direct" else "via " + (agency or channel))}</b>'
                            f'{" (agency)" if channel == "agency" else ""}?</p>')
                keep += (f'<input type="hidden" name="channel" value="{esc(form.get("channel", ""))}">'
                         f'<input type="hidden" name="agency" value="{esc(form.get("agency", ""))}">')
                button = "Confirm: I applied"
            return 200, small_page("Confirm", f"<h1>Confirm</h1>{notice}{question}{posting_lines(job)}"
                             + self._confirm_form(f"confirm_{action}", target, keep + f"<button>{button}</button>", form, undoes)
                             + f' <a href="{back}">Cancel</a>')

        def _ask_void(self, app_id: str, form: dict, notice: str = "") -> tuple[int, str]:
            """Void (recorded by mistake): the posting and the application named, with the reason you gave."""
            app = store.one("SELECT a.*, j.title, j.employer_name FROM applications a LEFT JOIN jobs j ON j.job_id = a.job_id "
                            "WHERE a.application_id = ?", (app_id,))
            if app is None:
                raise ValueError("unknown_application")
            reason = (form.get("reason") or "").strip()
            if not reason:
                raise ValueError("reason_required: say why it was recorded by mistake")
            refusal = void_refusal(store, app_matrix, app_id)   # the writer's own rule: never after an employer answered
            if refusal:
                raise ValueError(refusal)
            back = esc(f"/?t={token}#{quote(app_id, safe=':')}")
            via = app["channel"] + (f" ({app['agency']})" if app["agency"] else "")
            question = (f'<p>Void this application, recorded by mistake? It stays in the history and no longer counts as an '
                        f'application. Your reason: <b>{esc(reason)}</b></p><ul><li>Application: <code>{esc(app_id)}</code>, '
                        f'recorded {esc(app["applied_at"][:10])} via {esc(via)}, now {esc(app["current_state"].replace("_", " "))}'
                        f'</li></ul>')
            keep = f'<input type="hidden" name="reason" value="{esc(reason)}">'
            return 200, small_page("Confirm", f"<h1>Confirm</h1>{notice}{question}"
                                   + posting_lines({"title": app["title"] or "(posting not on record)",
                                                    "employer_name": app["employer_name"], "job_id": app["job_id"]})
                                   + self._confirm_form("confirm_void", app_id, keep + "<button>Confirm: void it</button>", form)
                                   + f' <a href="{back}">Cancel</a>')

        def _ask_again(self, kind: str, target: str, form: dict, why: Exception) -> tuple[int, str]:
            """Nothing recorded: what Confirm would record now is not what its page showed (F3). A fresh confirmation of
            the posting as it is now, and of the form's choices, with a new key; or the reason none can be offered."""
            notice = f'<p class="banner"><b>Not recorded: {esc(why)}. Check it again; only the new Confirm records.</b></p>'
            try:
                status, page = self._ask(kind, target, form, notice)
            except ValueError as exc:
                return 400, small_page("Not recorded", f"<p>Not recorded: {esc(why)}; {esc(exc)}</p>"
                                       f'<p><a href="/?t={esc(token)}">Back to the review page</a></p>')
            return (409 if status == 200 else status), page

        def do_GET(self):
            self._last_resort(self._get, "Could not build the page")

        def do_POST(self):
            self._last_resort(self._post, "Not recorded")

        def _last_resort(self, handle, title: str):
            """An error nothing else answers gets a page saying so, not a dropped connection (D88). A write that fails
            rolls back what it had not committed, but a form can record in steps (a pasted posting is taken in, then
            processed): "Not recorded" only when the request changed no row (Codex CR-2026-11-03-PYTHON-314, F3)."""
            from careeros.core import private_sites
            self._posting = None   # set by _post: the posting the form is about, once known
            self._before = store.committed   # rows changed by committed transactions; every write is in one (Store.tx)
            try:
                with private_sites.run():   # one reading of the private settings per request, never one for the page's
                    handle()                # whole life: an edit counts from the next request (the red team before Pass 4)
            except Exception:  # noqa: BLE001 - the page says it failed; the console keeps the traceback
                traceback.print_exc(file=sys.stderr)
                message = (self._maybe_saved("something went wrong in CareerOS") if self._changed()
                           else f"{title}: something went wrong in CareerOS.")
                try:
                    self._problem(500, f"{message} The details are in the window that runs the review page.")
                except OSError:   # the browser has gone: nothing more to say
                    pass

        def _changed(self) -> bool:
            """Whether this request committed a change. A write refused and rolled back is not one: its page says
            "Not recorded", truly."""
            return store.committed != self._before

        def _maybe_saved(self, why: str) -> str:
            check = f"Check {self._posting} on the review page" if self._posting else "Check the review page"
            return f"Could not finish: {str(why).rstrip('.')}; some changes may have been saved. {check} before trying again."

        def _refuse(self, status: int, message: str, why) -> None:
            """A refusal: `message` ("Not recorded ...") when the request committed no change; otherwise, as when a pasted
            posting was taken in and its processing then refused, a page that says changes may have been saved, with
            `why` (F3; Codex's Pass 2: the errors handled here, not only the unexpected ones)."""
            self._problem(status, self._maybe_saved(why) if self._changed() else message)

        def _get(self):
            if urlsplit(self.path).path != "/" or not self._authorized():
                return self._send(403, "forbidden")
            page = build_report(store, profile, policy, token=token, form_keys=form_keys, since=since, app_matrix=app_matrix,
                                previews=previews)
            for stale in list(form_keys)[:max(0, len(form_keys) - MAX_FORM_KEYS)]:  # oldest first; bounded memory
                del form_keys[stale]
                shown.pop(stale, None)
                previews.pop(stale, None)
            self._send(200, page)

        def _post(self):
            action = urlsplit(self.path).path.strip("/")
            if action not in ACTIONS or not self._authorized():
                return self._send(403, "forbidden")
            length = int(self.headers.get("Content-Length") or 0)
            if length > (MAX_PASTE_BODY if action == "paste" else MAX_BODY):
                return self._send(413, "too large")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            target, key = form.get("target", ""), form.get("key", "")
            if action != "paste":   # a paste's target is "new": its posting is known once taken in
                self._posting = target.partition("|")[2] or target or None   # a lead form's target is lead|job
            if form_keys.get(key) != (action, target):
                return self._problem(400, "This form is out of date. Reload the review page and try again.")
            if action in ASKS:   # confirmation first: nothing is recorded here (OPP-16)
                try:
                    return self._send(*self._ask(action, target, form))
                except ValueError as exc:
                    return self._problem(400, f"Not recorded: {exc}")
            kind, origin = action.removeprefix("confirm_"), form.get("from", "")
            confirming = kind != action   # confirm_<action>: bound to what its page showed (F3); other forms show nothing
            bound, undoes = shown.get(key, (None, None)) if confirming else (choices(kind, form), None)
            if bound is None:   # a Confirm whose page this server never showed
                return self._problem(400, "This form is out of date. Reload the review page and try again.")
            anchor = target
            try:
                with SingleWriterLock(lock_path), _swept(store, policy, profile):
                    if choices(kind, form) != bound:   # F3: Confirm records what its page showed, or nothing
                        raise ConfirmationChanged("the choices differ from the ones the confirmation showed")
                    if action == "label":   # from the preview its card showed; refused when the job changed since (B.2)
                        record_label(store, target, form.get("label", ""), form.get("reason", ""), key,
                                     preview=_card_preview(key, form), policy=policy, applicant=profile)
                        anchor = anchor_for(target, origin)
                    elif action == "label_night":
                        label, reason = form.get("label", ""), form.get("reason", "")
                        if label == "skip_schedule":  # counts toward the NIGHT schedule-fit false-positive rate
                            label, reason = "skip", "schedule doesn't fit" + (f": {reason}" if reason else "")
                        record_label(store, target, label, reason, key, profile="night",
                                     preview=_card_preview(key, form), policy=policy, applicant=profile)
                        anchor = f"night-{target}"
                    elif kind == "apply":   # confirm_apply: you confirmed the posting and the channel shown
                        record_application(store, app_matrix, target, bound["channel"], policy=policy, agency=bound["agency"],
                                           key=key, reapply=form.get("reapply_reason"), confirm=form.get("confirm_reason"))
                        anchor = anchor_for(target, origin)
                    elif action == "status":
                        if form.get("state") == VOID:   # only through Void, its reason and its confirmation
                            raise ValueError("use Void (recorded by mistake) on the application's card")
                        update_application(store, app_matrix, target, form.get("state", ""), notes=form.get("notes", ""), key=key)
                    elif kind == "void":   # confirm_void: you confirmed the posting, the application and the reason
                        update_application(store, app_matrix, target, VOID, reason=bound["reason"], key=key)
                    elif kind == "dismiss":   # confirm_dismiss: you confirmed the posting and the reason
                        from .dismiss import dismiss
                        dismiss(store, target, bound["reason"], bound["note"], key=key)
                        anchor = anchor_for(target, origin)
                    elif kind == "reinstate":   # confirm_reinstate: a later event undoes the dismissal
                        from .dismiss import reinstate
                        from .shortlist import shortlisted_job_ids
                        reinstate(store, target, form.get("note"), key=key, undoes=undoes)   # the dismissal it showed
                        anchor = f"sl-{target}" if target in shortlisted_job_ids(store) else "shortlist"
                    elif action == "paste":
                        from careeros.core.transitions import load_matrix
                        from careeros.discovery.manual_intake import intake
                        from careeros.paths import CONFIG_DIR
                        from careeros.pipeline import Context, process
                        ctx = Context(store, policy, profile, load_matrix(CONFIG_DIR / "workflows" / "job.json"))
                        from careeros.discovery.alerts import enroll_reading
                        if not enroll_reading(store):   # a pasted tracking link's host is remembered first (Codex Pass 4),
                            raise ValueError(        # or nothing is taken in (D153 R4, Codex Pass 5)
                                "the private settings are missing an agency's tracking host, changed during this request, or "
                                "cannot be read (careeros status names the problem). Nothing was saved; paste again once "
                                "it is fixed.")
                        from careeros.discovery.manual_intake import operation_for, paste_request
                        url = (form.get("url") or "").strip() or None
                        company = (form.get("company") or "").strip() or None
                        # the page's operation is its form key (the D190 design, section 6): a resubmitted form returns
                        # its job, and another request under the key is refused
                        op = operation_for("paste_page", key, paste_request(url, form.get("text", ""), company=company))
                        result = intake(ctx, form.get("text", ""), url, company=company, operation=op)
                        self._posting = result["job_id"]   # taken in: an error after this has saved it (F3)
                        from careeros.discovery.lead_resolution import link_same_posting
                        link_same_posting(ctx, result["job_id"], via="pasted")   # the lead for the same posting (2026-11-02)
                        process(ctx)
                        anchor = result["job_id"]
                    elif action == "lead":
                        dismiss_lead(store, target, key=key)
                        anchor = "leads"
                    elif action == "notice":   # an alert notice you dismiss (part 3 of D192)
                        dismiss_notice(store, target, key=key)
                        anchor = "notices"
                    elif action in ("lead_confirm", "lead_reject"):   # only the likely match the card showed, or nothing
                        lead_id, _, job_id = target.partition("|")
                        if not job_id:
                            raise ValueError("this form names no job")
                        if action == "lead_confirm":
                            confirm_likely(store, lead_id, policy=policy, job_id=job_id, key=key)
                        else:
                            reject_likely(store, lead_id, policy=policy, job_id=job_id, key=key)
                        anchor = "likely"
            # Each refusal says "Not recorded" only while this request committed no change (_refuse; Codex
            # CR-2026-11-03-PYTHON-314, F3, Pass 2): a paste is taken in before it is processed
            except LockError:   # when the lock is taken nothing is written; when it is let go the writes are done
                return self._refuse(409, "A CareerOS run is writing right now. Go back and try again in a moment.",
                                    "CareerOS's writer lock was busy or changed hands")
            except (ConfirmationChanged, DismissalChanged) as exc:   # F3: nothing recorded; confirm what is true now
                if self._changed():
                    return self._problem(409, self._maybe_saved(exc))
                return self._send(*self._ask_again(kind, target, form, exc))
            except ApplicationHold as exc:
                if self._changed():
                    return self._problem(409, self._maybe_saved(exc))
                return self._send(409, self._hold_page(target, form, exc))
            except SessionOpen as exc:   # an assistance session records it when you close it (OPP-77): its command, exactly
                return self._refuse(409, f"Not recorded: {exc}", exc)
            except ApplicationConflict as exc:
                return self._refuse(409, f"Not recorded. {exc}.", exc)
            except IdempotencyCollision:   # a second click on another button of a form already used (D88)
                if action == "paste":   # the same form sent again with another paste (Codex H43): never "recorded"
                    from careeros.discovery.manual_intake import MAX_TEXT
                    why = (f"this form was already used for another paste: its link, its text, its company (as typed, "
                           f"when it runs past its limit), or whether its text runs past the {MAX_TEXT:,}-character "
                           "limit differ")
                    return self._refuse(409, f"Not recorded: {why}. Reload the review page and paste it again.", why)
                return self._refuse(409, "Not recorded: this form was already used for a different choice. Reload the "
                                         "review page and choose again.", "this form was already used for a different choice")
            except ValueError as exc:
                return self._refuse(400, f"Not recorded: {exc}", exc)
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
