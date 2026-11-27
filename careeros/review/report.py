"""Daily review report (local HTML). All posting-derived text is escaped: postings are untrusted input.

With a token (served by `careeros review`) every card has forms: label, record an application, update an application
status, dismiss an email lead. Each form carries a one-time key bound to (action, target) by the server.
"""
from __future__ import annotations

import contextvars
import functools
import html
import json
import random
import secrets
from collections import Counter
from datetime import timedelta

from careeros.core import private_sites
from careeros.core.clock import parse_utc
from careeros.db.store import Store
from careeros.qualify import certifications, experience
from careeros.review.applications import COUNTED, OPEN_STATES, VOID, follow_up_due, follow_ups, void_refusal
from careeros.review.dismiss import RECENT_DAYS, is_dismissed, recent
from careeros.review.labels import label_backlog

TIER_TITLES = {"A": "Tier A: exceptional", "B": "Tier B: strong apply", "near_miss": "Near-miss: manual review"}
STATE_NOTE = {"CONDITIONAL": "CONDITIONAL: check the flagged unknowns before applying",
              "HOLD": "HOLD: an employer-level eligibility hold (gate.held_parents) until you decide about it"}
STALE_DAYS = 60  # default only: the policy's discovery.max_posting_age_days decides (2026-10-12)
# Jobs that reached CareerOS only as NIGHT recommendations: their PRIMARY verdict is not a PRIMARY decision to audit.
NIGHT_ONLY = ("(j.source = 'manual_url' AND j.job_id IN (SELECT job_id FROM leads WHERE profile = 'night') "
              "AND j.job_id NOT IN (SELECT job_id FROM leads WHERE profile = 'primary' AND job_id IS NOT NULL))")
HIDDEN_REJECTIONS = ("employer_excluded",)  # deliberate exclusions are not audited or labelled
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"   # no script, no request; forms post here only
CSS = """
:root{--bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e2dc;--a:#1f6f43;--b:#2f5d8a;--n:#9a6b16;--r:#a33a2c;--hi:#eef5ee}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--hi:#1c2a1f}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--muted);font-size:13px}.banner{background:#fff4d6;color:#5c4300;padding:10px 12px;border-radius:6px;margin:10px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0}
.card.new{background:var(--hi)}.card h3{margin:0 0 4px;font-size:16px}
.row{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--muted)}
.scores{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.pill{border:1px solid var(--line);border-radius:999px;padding:1px 9px;font-size:12px}
.pill b{font-weight:600}.flag{color:var(--n)}.fail{color:var(--r)}.ok{color:var(--a)}ul{margin:6px 0;padding-left:20px}
form{display:inline-block;margin:4px 8px 0 0}button{font:inherit;font-size:13px;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
input[type=text],select{font:inherit;font-size:13px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);max-width:100%}
input[type=text]{width:200px}.labelled{font-size:12px;color:var(--a)}a{color:var(--b)}
code.jid{user-select:all;font-size:12px}.verdict{font-weight:600;margin:2px 0 4px}
.approves pre{white-space:pre-wrap;font:inherit;font-size:13px;margin:4px 0}
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_link(url: str | None, text: str) -> str:
    if url and url.startswith(("https://", "http://")):
        return f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{esc(text)}</a>'
    return esc(text)


def _days(earlier: str | None, now: str) -> float | None:
    return None if not earlier else (parse_utc(now) - parse_utc(earlier)) / timedelta(days=1)


def _age(posted: str | None, now: str) -> str:
    days = _days(posted, now)
    if days is None:
        return "posted date unknown"
    return "posted today" if days < 1 else f"posted {int(days)}d ago"


def _comp(job) -> str:
    """The pay columns as stated: both ends, their period and currency, never a range made from one end (BATCH_5 C1),
    each amount exactly as stored (Codex H37)."""
    from careeros.review.labels import _amount, stored_pay
    if job["comp_min"] is None and job["comp_max"] is None:
        # what the board's own pay fields store, beside the reading (the owner's D200, Codex H42)
        stored = job["board_pay_json"] if "board_pay_json" in job.keys() else None
        note = stored_pay(json.loads(stored)) if stored else ""
        return "pay not posted" + (f" ({note})" if note else "")
    lo, hi = job["comp_min"], job["comp_max"]
    amount = (f"{_amount(lo)}–{_amount(hi)}" if lo is not None and hi is not None else
              f"up to {_amount(hi)}" if hi is not None else f"from {_amount(lo)}")
    return (f"{job['comp_currency'] or 'currency not stated'} {amount} "
            f"{'per ' + job['comp_period'] if job['comp_period'] else 'period not stated'}")


def _latest(store: Store, table: str, job_id: str):
    return store.one(f"SELECT * FROM {table} WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def _id_code(value: str) -> str:
    """A job or application id as copyable text (OPP-15, 2026-11-01): `label`, `applied`, `package`, `assist` and
    `app-status` all need it. One click selects it whole."""
    return f'<span><code class="jid">{esc(value)}</code></span>'


def _form(action: str, target: str, token: str, form_keys: dict, body: str, key: str | None = None) -> str:
    key = key or secrets.token_hex(12)
    form_keys[key] = (action, target)
    return (f'<form method="post" action="/{action}?t={esc(token)}"><input type="hidden" name="target" value="{esc(target)}">'
            f'<input type="hidden" name="key" value="{key}">{body}</form>')


def _origin(origin: str) -> str:
    """The card a form sits on (`sl` the shortlist card, `night` a NIGHT card, `card` a posting's card), so the server
    returns there after the action (OPP-16, 2026-11-01)."""
    return f'<input type="hidden" name="from" value="{esc(origin)}">'


# The first submit button of a label form: Enter in its reason box submits with the form's first button, which would
# record "Apply". Disabled, it makes Enter record nothing (interface review 2026-11-03, item 1; D88).
NO_ENTER = '<button type="submit" disabled hidden aria-hidden="true" tabindex="-1"></button>'


# the page being built: (your policy, your profile, the previews its label forms keep under their form keys), set by
# build_report, so each card's label form captures what it shows (the D190 design, section 3, "The preview")
_PREVIEWS: contextvars.ContextVar = contextvars.ContextVar("careeros_previews", default=None)
# the page being built: the jobs the shortlist names, and the jobs whose own card it shows, so every shortlisted job's
# card is on the page (its shortlist card links there and offers no label: BATCH_5 C3)
_SHORTLISTED: contextvars.ContextVar = contextvars.ContextVar("careeros_shortlisted", default=None)
_CARDS_SHOWN: contextvars.ContextVar = contextvars.ContextVar("careeros_cards_shown", default=None)


def _preview_buttons(store: Store, job_id: str, who: str, key: str, choices: tuple) -> str:
    """The label buttons of a card, with its preview: Apply and Stretch only where the job offers one (its capture is
    kept in the server's memory under the form key, its digest in the form); Skip always."""
    from careeros.review.labels import capture
    context = _PREVIEWS.get()
    captured = None
    if context is not None:
        policy, profile, previews = context
        captured = capture(store, policy, profile, job_id, who)
        if not captured["waiting"]:
            previews[key] = captured
    offered = [(v, t) for v, t in choices if v not in ("apply", "stretch") or (captured and not captured["waiting"])]
    buttons = "".join(f'<button name="label" value="{v}">{t}</button> ' for v, t in offered)
    if captured is not None and not captured["waiting"]:
        # the card's label offers what this capture binds, every value of it, rendered by the CLI preview's renderer
        # (BATCH_5 C; Codex H37): an Apply or Stretch here approves exactly these items; a text shown whole sits in a
        # fold you open, in the page whole and escaped
        from careeros.review.labels import card_items
        items = card_items(captured, store, store.clock.now())
        shown = ('<div class="approves"><div class="meta">An Apply or Stretch label here approves:</div><ul>'
                 + "".join(f'<li>{esc(item["text"])}'
                           + (f'<details><summary>show it</summary><pre>{esc(item["fold"])}</pre></details>'
                              if "fold" in item else "") + '</li>' for item in items) + '</ul></div>')
        return f'{shown}<input type="hidden" name="digest" value="{esc(captured["digest"])}">{buttons}'
    why = captured["waiting"] if captured is not None else "no preview on this page"
    return f'<span class="meta">Apply and Stretch: {esc(why)}.</span> {buttons}'


def _label_form(store: Store, job_id: str, token: str | None, form_keys: dict, origin: str = "card") -> str:
    """The label buttons. An Apply or Stretch label is given from the preview the card shows (the D190 design,
    section 3): the form carries its digest, and a label clicked after the job changed is refused, never given to
    content the card did not show (the red team's gr1-routes-0, B.2)."""
    if not token:
        return f'<span class="meta">label: <code>careeros label {esc(job_id)} apply|stretch|skip</code></span>'
    key = secrets.token_hex(12)
    buttons = _preview_buttons(store, job_id, "primary", key, (("apply", "Apply"), ("stretch", "Stretch"), ("skip", "Skip")))
    form = _form("label", job_id, token, form_keys,
                 f'{_origin(origin)}{NO_ENTER}'
                 f'<input type="text" name="reason" placeholder="label reason (optional)"> {buttons}', key=key)
    if origin == "card" and "waiting for a board read" in buttons:
        # W2: a job waiting for its board's read may be dismissed meanwhile; a dismissal needs no preview
        form += _dismiss_form(job_id, token, form_keys, origin)
    return form


def _apply_form(job_id: str, token: str | None, form_keys: dict, origin: str = "card") -> str:
    """The "I applied" form: the server first shows a confirmation naming the posting; only its Confirm records (OPP-16)."""
    if not token:
        return f'<span class="meta">applied? <code>careeros applied {esc(job_id)} --channel direct</code></span>'
    shortcut = private_sites.agency_shortcut()   # an agency named in the private settings, if any
    choices = [("direct", "directly"), *([(f"agency:{shortcut}", f"via {shortcut}")] if shortcut else []),
               ("agency", "via another agency"), ("referral", "by referral")]
    options = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in choices)
    return _form("apply", job_id, token, form_keys,
                 f'{_origin(origin)}<select name="channel">{options}</select> '
                 f'<input type="text" name="agency" placeholder="agency name (if other)"> <button>I applied</button>')


def _status_form(app, token: str | None, form_keys: dict, app_matrix) -> str:
    if not token or app_matrix is None or app["current_state"] in app_matrix.terminal:
        return ""
    nexts = sorted(to for (frm, to) in app_matrix.edges if frm == app["current_state"] and to != VOID)   # void: its own control
    options = "".join(f'<option value="{esc(s)}">{esc(s.replace("_", " "))}</option>' for s in nexts)
    # nothing preselected and a choice required: one click or Enter in the note records nothing (D88)
    return _form("status", app["application_id"], token, form_keys,
                 f'<select name="state" required><option value="">choose the new status</option>{options}</select> '
                 f'<input type="text" name="notes" placeholder="note (optional)"> <button>Update status</button>')


def _void_form(store: Store, app, token: str | None, form_keys: dict, app_matrix) -> str:
    """The "Void (recorded by mistake)" control, offered only where the writer allows it: before anyone at the employer
    answered, in the application's whole history (applications.void_refusal). The reason is required, and the server
    first shows a confirmation naming the posting (ADR 0007, amendment of 2026-11-01)."""
    if app_matrix is None or void_refusal(store, app_matrix, app["application_id"]):
        return ""
    if not token:
        return (f'<span class="meta">recorded by mistake? <code>careeros app-status {esc(app["application_id"])} void '
                f'--reason "..."</code></span>')
    return _form("void", app["application_id"], token, form_keys,
                 '<input type="text" name="reason" required maxlength="300" placeholder="why it was recorded by mistake"> '
                 '<button>Void (recorded by mistake)</button>')


def _grouped(rows) -> list:
    """One card per posting group: variants (same requisition elsewhere, same title with other text) collapse together."""
    groups, order = {}, []
    for row in rows:
        key = row["variant_of"] or row["job_id"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [(groups[k][0], len(groups[k]) - 1) for k in order]


def _stale_note(posted: str | None, now: str, stale_days: int) -> str:
    """The posting's age, flagged once it is older than discovery.max_posting_age_days (shown, never hidden)."""
    age = _days(posted, now)
    stale = age is not None and age > stale_days
    return f'<span class="{"flag" if stale else ""}">{esc(_age(posted, now))}{" — may be stale" if stale else ""}</span>'


def _plain_card(store: Store, job, now: str, token: str | None, form_keys: dict, variants: int = 0, note: str = "",
                new: bool = False, stale_days: int = STALE_DAYS, policy=None) -> str:
    if _CARDS_SHOWN.get() is not None:
        _CARDS_SHOWN.get().add(job["job_id"])
    score = _latest(store, "scores", job["job_id"])
    gate = _latest(store, "gate_results", job["job_id"])
    label = _latest(store, "labels", job["job_id"])
    from careeros.openings import application_status   # the same check recording uses (role group, history, holds)
    from .explain import _verdict   # one sentence: why it is in this tier (OPP-03)
    status = application_status(store, job["job_id"])
    app = status["applied"]
    states = json.loads(job["remote_states_json"] or "[]")
    parts = [f'<div class="card{" new" if new else ""}" id="{esc(job["job_id"])}"><h3>{safe_link(job["url"], job["title"])}</h3>',
             *([f'<div class="verdict">{esc(_verdict(store, policy, job["job_id"]))}</div>'] if policy is not None else []),   # OPP-03
             f'<div class="row">{_id_code(job["job_id"])}<span>{esc(job["employer_name"])}</span><span>{esc(job["location_raw"] or "location unknown")}'
             f' · {esc(job["remote_type"])}{" (" + esc(", ".join(states)) + ")" if states else ""}</span>'
             f'<span>{esc(_comp(job))}</span><span>{esc(job["employment_type"])}</span>'
             + _stale_note(job["posted_at"], now, stale_days) +
             f'<span>family: {esc(job["family"])}</span>'
             + (f'<span>+{variants} more posting(s) of this role</span>' if variants else '') + '</div>']
    if note:
        parts.append(f'<div class="row flag">{esc(note)}</div>')
    if score:
        comps = json.loads(score["components_json"])
        pills = [("Priority", score["apply_priority"]), ("Eligibility", score["eligibility"]), ("Fit", score["candidate_fit"]),
                 ("Value", score["career_value"]), ("Freshness", score["freshness"])]
        domain = comps.get("domain") or {}
        state = comps.get("state")
        parts.append('<div class="scores">' + "".join(f'<span class="pill">{n} <b>{v:.0f}</b></span>' for n, v in pills)
                     + f'<span class="pill">Interview chance: <b>{esc(score["interview_band"])}</b></span>'
                     + (f'<span class="pill">domain <b>{esc(domain["level"])}</b></span>' if domain.get("level") else "")
                     + ('<span class="pill flag"><b>Stretch</b>: '
                        + ("experience gap" if comps.get("stretch_reason") == "experience" else "degree required") + '</span>'
                        if comps.get("stretch") else "")
                     + (f'<span class="pill" title="{esc(comps["eligibility"]["dims"]["skills_coverage"]["basis"])}">'
                        f'skills covered (weighted) <b>{comps["skill_coverage"]:.0%}</b></span>'
                        if comps.get("skill_coverage") is not None else "") + '</div>')
        if state in STATE_NOTE:
            parts.append(f'<div class="row flag">{esc(STATE_NOTE[state])}</div>')
        matched = comps["eligibility"]["matched_skills"] + [s for s in comps["candidate_fit"]["matched_skills"] if s not in comps["eligibility"]["matched_skills"]]
        if matched:
            parts.append(f'<div class="row">matches: {esc(", ".join(matched[:10]))}</div>')
        if comps["eligibility"]["missing_skills"]:
            parts.append(f'<div class="row fail">missing required: {esc(", ".join(comps["eligibility"]["missing_skills"]))}</div>')
        if comps.get("certifications"):  # certification requirements, named and matched (2026-10-15)
            parts.append('<div class="row">certifications: ' + esc(" | ".join(certifications.summary(r) for r in comps["certifications"]))
                         + "</div>")
        if (comps.get("experience") or {}).get("decisive_line") is not None:  # Experience Match, shadow mode (2026-10-15)
            parts.append(f'<div class="row" title="{esc(" ".join(experience.explain(comps["experience"])))}">'
                         f'{esc(experience.summary(comps["experience"]))}</div>')
    if gate:
        failures, flags = json.loads(gate["hard_failures_json"]), json.loads(gate["flags_json"])
        items = [f'<li class="fail">{esc(f["detail"])}'
                 + (f' <span class="meta">(your profile: {esc(f["profile_evidence"])}; confidence {f["confidence"]:.2f})</span>'
                    if f.get("profile_evidence") else "") + '</li>' for f in failures] \
            + [f'<li class="flag">{esc(f["detail"])}</li>' for f in flags]   # every flag (BATCH_5 C4)
        if items:
            parts.append("<ul>" + "".join(items) + "</ul>")
    if label:
        parts.append(f'<div class="labelled">your label: {esc(label["label"])}{" — " + esc(label["reason"]) if label["reason"] else ""}</div>')
        from careeros.review.labels import label_confirmed
        if label["label"] in ("apply", "stretch") and label["profile"] == "primary" and not label_confirmed(store, label):
            # the owner's D188: a label given before the upgrade is confirmed once, here, before anything is prepared
            parts.append('<div class="flag">This label was given before the upgrade of 2026-11-19: label the job again below '
                         'to confirm it. Until then nothing is prepared and no session opens for it.</div>')
    if app:
        parts.append(f'<div class="ok">Applied {esc(app["applied_at"][:10])} via {esc(app["channel"])}'
                     f'{" (" + esc(app["agency"]) + ")" if app["agency"] else ""} — status: {esc(app["current_state"].replace("_", " "))}'
                     f'{" (another posting of this role)" if app["job_id"] != job["job_id"] else ""}</div>')
    holds = [c["detail"] for c in status["checks"] if c["level"] == "hold"]
    if holds:
        parts.append(f'<div class="flag">Before applying: {esc("; ".join(holds))}</div>')
    if policy is not None:   # permission to submit (R2): its own line, apart from tier, state and scores
        from .submission import display
        line = display(store, policy, job["job_id"])
        if line:
            parts.append(f'<div class="flag"><b>{esc(line)}</b></div>')
    parts.append(_label_form(store, job["job_id"], token, form_keys))
    if not app or app["current_state"] not in OPEN_STATES:   # a finished application can be followed by a reasoned re-application
        parts.append(_apply_form(job["job_id"], token, form_keys))
    parts.append("</div>")
    return "".join(parts)


def _applications_section(store: Store, now: str, token, form_keys, app_matrix) -> list[str]:
    rows = store.all("""SELECT a.*, j.title, j.employer_name, j.url, j.current_state AS job_state, j.closed_at FROM applications a
                        LEFT JOIN jobs j ON j.job_id = a.job_id ORDER BY a.applied_at DESC""")
    if not rows:
        return []
    counted = [r for r in rows if r["current_state"] != VOID]   # a void one stays listed, and never counts (2026-11-01)
    states = Counter(r["current_state"] for r in counted)
    voided = len(rows) - len(counted)
    out = [f"<h2>Your applications ({len(counted)}{f'; {voided} void' if voided else ''})</h2><div class=\"meta\">" +
           " · ".join(f"{esc(k.replace('_', ' '))}: {v}" for k, v in states.most_common()) + "</div>"]
    followed = follow_ups(store)
    for a in rows:
        due = follow_up_due(a, now, followed.get(a["application_id"]))   # the one rule (QW-11, OPP-21)
        nudge = (f" — no response in {due.days} days{' since your follow-up' if due.after_follow_up else ''}: following up "
                 "is your choice, CareerOS sends nothing (to record one: careeros app-status "
                 f"{a['application_id']} --followed-up)" if due and due.due else "")
        facts = [f"you followed up {followed[a['application_id']][:10]}"] if a["application_id"] in followed else []
        if a["job_state"] == "closed":   # the reminder stays: the employer may still answer (OPP-21)
            facts.append(f"the posting closed {a['closed_at'][:10]}" if a["closed_at"] else "the posting closed")
        if a["current_state"] == VOID:   # why: the reason given when it was voided
            why = store.one("SELECT reason_code FROM transitions WHERE workflow_id = ? AND to_state = ? ORDER BY sequence DESC "
                            "LIMIT 1", (a["workflow_id"], VOID))
            nudge = f" (recorded by mistake: {why['reason_code']})" if why and why["reason_code"] else " (recorded by mistake)"
        out.append(f'<div class="card" id="{esc(a["application_id"])}"><h3>{safe_link(a["url"], a["title"] or a["job_id"])}</h3><div class="row">'
                   f'{_id_code(a["application_id"])}<span>{esc(a["employer_name"])}</span><span>applied {esc(a["applied_at"][:10])} via {esc(a["channel"])}'
                   f'{" (" + esc(a["agency"]) + ")" if a["agency"] else ""}</span>'
                   f'<span class="{"flag" if nudge else "ok"}">status: {esc(a["current_state"].replace("_", " "))}{esc(nudge)}</span></div>'
                   + (f'<div class="row meta">{esc("; ".join(facts))}</div>' if facts else "")
                   + (f'<div class="row">note: {esc(a["notes"])}</div>' if a["notes"] else "")
                   + _status_form(a, token, form_keys, app_matrix) + _void_form(store, a, token, form_keys, app_matrix) + "</div>")
    return out


def _funnel_section(store: Store) -> list[str]:
    from .funnel import funnel
    rows = funnel(store, "source")
    if not rows:
        return []
    head = "".join(f"<th>{h}</th>" for h in ("source", "applied", "responded", "screening", "interviewing", "offer", "hired"))
    body = "".join("<tr>" + "".join(f"<td>{esc(r[k])}</td>" for k in ("source", "applied", "responded", "screening",
                                                                     "interviewing", "offer", "hired")) + "</tr>" for r in rows)
    return ['<h2>Your funnel by source</h2><div class="meta">Automatic confirmations never count as responses. Fewer than '
            '10 applications in a row is an observation, not a trend. More views: <code>careeros funnel --by family</code>.</div>'
            f'<table class="meta">{head}{body}</table>']


def _likely_section(store: Store, token, form_keys, policy) -> list[str]:
    """Likely matches waiting for you (2026-11-02; ADR 0003, amendment of that date): a lead and the one posting at its
    employer with a near-identical title. Never linked until you confirm; "Not this one" keeps the lead open and that job
    is never proposed for it again. A form names the lead and the job it shows, so a stale page records nothing."""
    from .leads import pending_all
    items = pending_all(store, policy)   # only proposals that still qualify (Codex F6)
    if not items:
        return []
    out = [f'<h2 id="likely">Likely matches for your leads ({len(items)})</h2><div class="meta">A lead from an alert or a '
           "recommendation and the one posting at its employer whose title is nearly the same (filler such as remote or I/II "
           "aside), at the same seniority and a compatible location. CareerOS links it only when you confirm. Or: "
           "<code>careeros leads confirm LEAD_ID --job JOB_ID</code>.</div>"]
    for item in items[:40]:
        lead, job, match = item["lead"], item["job"], item["match"]
        target = f"{lead['lead_id']}|{job['job_id']}"
        buttons = (_form("lead_confirm", target, token, form_keys, "<button>Confirm: this is the lead's posting</button>") + " "
                   + _form("lead_reject", target, token, form_keys, "<button>Not this one</button>")) if token else (
            f'<span class="meta"><code>careeros leads confirm {esc(lead["lead_id"])} --job {esc(job["job_id"])}</code> or '
            f'<code>careeros leads reject {esc(lead["lead_id"])} --job {esc(job["job_id"])}</code></span>')
        out.append(f'<div class="card" id="likely-{esc(lead["lead_id"])}"><h3>Lead: {safe_link(lead["url"], lead["title"])}</h3>'
                   f'<div class="row"><span>{esc(lead["site"])}</span><span>{esc(lead["context"] or "")}</span>'
                   f'<span>lead id <code>{esc(lead["lead_id"])}</code></span></div>'
                   f'<div class="row"><span>Likely posting: {safe_link(job["url"], job["title"])}</span><span>{esc(job["employer_name"])}</span>'
                   f'<span>{esc(job["location_raw"] or "location unknown")}</span><span>job id <code>{esc(job["job_id"])}</code></span></div>'
                   f'<div class="meta">{esc(match["reason"] or "")}</div>{buttons}</div>')
    return out


def _lead_card(lead, token, form_keys) -> str:
    ungraded = lead["family"] is None
    dismiss = _form("lead", lead["lead_id"], token, form_keys, '<button name="action" value="dismiss">Dismiss</button>') if token else ""
    state = (f'<div class="row"><span class="flag">no match yet</span><span>{esc(lead["status_reason"])}</span></div>'
             if lead["status"] == "no_match" else
             f'<div class="row"><span>{esc(lead["status_reason"])}</span></div>' if lead["status_reason"] else "")
    failed = (f'<div class="row"><span class="flag">could not be read: {esc(lead["fetch_failure"])}</span></div>'
              if lead["fetch_failure"] else "")
    if "repeat_of" in lead.keys() and lead["repeat_of"]:   # the same agency and job number (H8, part 3 of D192): advice
        failed += (f'<div class="row"><span>possibly the same as lead {esc(lead["repeat_of"])} (the same job number)'
                   '</span></div>')
    family = ('<span>family: not matched</span><span class="flag">Not graded</span>' if ungraded else
              f'<span>family: {esc(lead["family"])}</span>')
    return (f'<div class="card"><h3>{safe_link(lead["url"], lead["title"])}</h3><div class="row">'
            + ('<span class="flag">NIGHT</span>' if lead["profile"] == "night" else '<span>PRIMARY</span>' if ungraded else '')
            + f'<span>{esc(lead["site"])}</span>{family}'
            f'<span>from: {esc(lead["sender"])}</span><span>{esc((lead["received_at"] or lead["first_seen_at"])[:10])}</span></div>'
            + (f'<div class="row">{esc(lead["context"])}</div>' if lead["context"] else "") + state + failed + dismiss + "</div>")


def _notice_card(store: Store, notice, token, form_keys, policy, cache: dict) -> str:
    from .notices import likely_postings
    suggestion = json.loads(notice["suggestion_json"] or "{}").get("greenhouse_posting")
    tracked = likely_postings(store, policy, notice, cache) if policy is not None else []
    dismiss = (_form("notice", notice["notice_id"], token, form_keys, '<button name="action" value="dismiss">Dismiss</button>')
               if token else "")
    return (f'<div class="card"><h3>{safe_link(notice["url"], notice["title"])}</h3><div class="row">'
            + ('<span class="flag">NIGHT</span>' if notice["profile"] == "night" else '')
            + f'<span>{esc(notice["site"])}</span><span>from: {esc(notice["sender"])}</span>'
            f'<span>{esc((notice["received_at"] or notice["first_seen_at"])[:10])}</span></div>'
            + (f'<div class="row">{esc(notice["context"])}</div>' if notice["context"] else "")
            + (f'<div class="row"><span>may be {safe_link(suggestion, suggestion)} (a guess: add it by that link if it is)</span>'
               '</div>' if suggestion else "")
            + "".join(f'<div class="row"><span>a tracked posting with this title: {esc(j["job_id"])} '
                      f'({esc(j["location_raw"] or "location unknown")})</span></div>' for j in tracked[:3])
            + f'<div class="row"><code>careeros notices add {esc(notice["notice_id"])} --url LINK</code> or '
            f'<code>--text-file FILE</code></div>' + dismiss + "</div>")


def _notices_section(store: Store, token, form_keys, policy=None) -> list[str]:
    """Alert cards whose link names no posting (part 3 of the owner's D192 build): never leads, grouped by the employer
    each names, else by sender."""
    from .notices import groups
    grouped = groups(store)
    if not grouped:
        return []
    total, cache = sum(len(rows) for _, rows in grouped), dict[str, object]()
    out = [f'<h2 id="notices">Notices from alerts ({total})</h2><div class="meta">Alert cards whose link names no posting '
           '(a careers page). They are never leads and never matched to a posting: open the page, find the posting, and '
           'add it by its own link, or with its text; or dismiss the notice.</div>']
    for label, rows in grouped:
        out.append(f'<h3>{esc(label)} ({len(rows)})</h3>')
        out.extend(_notice_card(store, n, token, form_keys, policy, cache) for n in rows[:30])
    return out


def _leads_section(store: Store, token, form_keys) -> list[str]:
    leads = store.all("SELECT * FROM leads WHERE status IN ('new','no_match') AND family IS NOT NULL ORDER BY first_seen_at DESC")
    ungraded = store.all("SELECT * FROM leads WHERE status IN ('new','no_match') AND family IS NULL ORDER BY first_seen_at DESC, lead_id")
    out = []
    if leads:
        out.append(f"<h2 id=\"leads\">Leads from alerts and recommendations ({len(leads)})</h2><div class=\"meta\">CareerOS cannot open these sites for you. "
                   "Open the ones worth a look, save the posting page (Ctrl+S, 'Webpage, HTML only') into vault\\inbox, then run "
                   "<code>careeros import</code> to grade it. A lead whose employer's board CareerOS scans is matched again after "
                   "every scan of that board.</div>")
        out.extend(_lead_card(lead, token, form_keys) for lead in leads[:60])
    if ungraded:
        if not leads:
            out.append('<div id="leads"></div>')   # the existing dismiss route returns to this anchor
        out.append(f'<h2 id="leads-role-review">Needs role review / Not graded ({len(ungraded)})</h2>'
                   '<div class="meta">These retained links did not match a configured role family. Review the posting yourself; '
                   'eligibility and qualification have not been assessed. '
                   f'Showing {min(60, len(ungraded))} of {len(ungraded)} open leads needing role review. '
                   'Use <code>careeros leads list</code> to list every open lead.</div>')
        out.extend(_lead_card(lead, token, form_keys) for lead in ungraded[:60])
    return out


def _night_label_form(store: Store, job_id: str, token: str | None, form_keys: dict) -> str:
    if not token:
        return f'<span class="meta">label: <code>careeros label {esc(job_id)} apply|stretch|skip --night</code></span>'
    key = secrets.token_hex(12)
    buttons = _preview_buttons(store, job_id, "night", key, (("apply", "Apply"), ("stretch", "Stretch"), ("skip", "Skip"),
                                                           ("skip_schedule", "Skip: schedule doesn't fit")))
    return _form("label_night", job_id, token, form_keys,
                 f'{NO_ENTER}<input type="text" name="reason" placeholder="label reason (optional)"> {buttons}', key=key)


def _night_card(store: Store, r, now: str, token, form_keys, stale_days: int = STALE_DAYS) -> str:
    app = store.one(f"SELECT * FROM applications WHERE job_id = ? AND {COUNTED} ORDER BY applied_at DESC LIMIT 1", (r["job_id"],))
    label = store.one("SELECT * FROM labels WHERE job_id = ? AND profile = 'night' ORDER BY at DESC, rowid DESC LIMIT 1", (r["job_id"],))
    flags = json.loads(r["n_flags"] or "[]")
    extra = json.loads(r["n_components"] or "{}")
    load = extra.get("workload") or {}
    return (f'<div class="card" id="night-{esc(r["job_id"])}"><h3>{safe_link(r["url"], r["title"])}</h3><div class="row">'
            f'<span>{esc(r["employer_name"])}</span><span>{esc(r["location_raw"] or "location unknown")} · {esc(r["remote_type"])}</span>'
            f'<span>{esc(_comp(r))}</span><span>{esc(r["employment_type"])}</span>'
            + _stale_note(r["posted_at"], now, stale_days) + '</div>'
            f'<div class="scores"><span class="pill">NIGHT priority <b>{r["n_priority"]:.0f}</b></span>'
            f'<span class="pill">{esc(r["n_mode"])}</span><span class="pill">{esc(r["n_state"])}</span>'
            f'<span class="pill">schedule <b>{esc(r["n_schedule"])}</b></span><span class="pill">phone <b>{esc(r["n_phone"])}</b></span>'
            f'<span class="pill">{esc(r["n_family"])}</span>'
            + (f'<span class="pill" title="{esc(load.get("basis") or "")}">workload <b>{esc(load["level"])}</b></span>'
               if load.get("level") else "")
            + '</div>'
            + ("<ul>" + "".join(f'<li class="flag">{esc(f["detail"])}</li>' for f in flags[:6]) + "</ul>" if flags else "")
            + (f'<div class="labelled">your NIGHT label: {esc(label["label"])}{" — " + esc(label["reason"]) if label["reason"] else ""}</div>'
               if label else "")
            + _night_label_form(store, r["job_id"], token, form_keys)
            + (f'<div class="ok">Applied {esc(app["applied_at"][:10])} via {esc(app["channel"])} — status: '
               f'{esc(app["current_state"].replace("_", " "))}</div>' if app else _apply_form(r["job_id"], token, form_keys, "night"))
            + "</div>")


def _night_section(store: Store, now: str, token, form_keys, policy) -> list[str]:
    """NIGHT / overnight, two leaderboards (Supplemental, Replacement): its own rules, states, ranking and labels.
    Never mixed into the PRIMARY lists, and NIGHT labels never count toward PRIMARY acceptance."""
    tiers = (policy.get("night", default={}) or {}).get("tiers") or {}
    conditional_min, near_min = tiers.get("conditional", 82), tiers.get("near_miss", 65)
    rows = store.all("""SELECT j.*, a.tier n_tier, a.priority n_priority, a.schedule n_schedule, a.phone n_phone,
                               a.family n_family, a.flags_json n_flags, a.mode n_mode, a.state n_state,
                               a.components_json n_components
                        FROM jobs j JOIN night_results a ON a.result_id = (SELECT result_id FROM night_results
                             WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE a.decision = 'eligible' AND j.current_state NOT IN ('duplicate','closed')
                        ORDER BY a.priority DESC""")
    out = ['<h2 id="night">NIGHT / overnight</h2><div class="meta">A separate profile with its own rules, states and ranking. '
           'Supplemental = work alongside a daytime job; Replacement = a full-time overnight job that could become the main one. '
           'Labels here measure NIGHT only.</div>']
    for mode, title in (("supplemental", "NIGHT · Supplemental"), ("replacement", "NIGHT · Replacement")):
        mine = [r for r in rows if r["n_mode"] == mode]
        unconfirmed = [
            r for r in mine
            if r["n_state"] == "CONDITIONAL"
            and r["n_schedule"] in ("unknown", "daytime_likely")
            and r["n_priority"] >= near_min
        ]
        unconfirmed_ids = {r["job_id"] for r in unconfirmed}
        groups = [
            ("Tier A", [r for r in mine if r["n_tier"] == "A"]),
            ("Tier B", [r for r in mine if r["n_tier"] == "B"]),
            ("Conditional priority", [
                r for r in mine
                if r["n_state"] == "CONDITIONAL"
                and r["n_priority"] >= conditional_min
                and r["job_id"] not in unconfirmed_ids
            ]),
            ("On hold", [r for r in mine if r["n_state"] == "HOLD" and r["n_priority"] >= near_min]),
            ("Near-miss", [
                r for r in mine
                if r["n_tier"] == "near_miss"
                and r["n_state"] != "HOLD"
                and r["job_id"] not in unconfirmed_ids
                and not (
                    r["n_state"] == "CONDITIONAL"
                    and r["n_priority"] >= conditional_min
                )
            ]),
            ("Unconfirmed hours — check before considering", unconfirmed),
        ]
        shown = sum(len(items) for _, items in groups)
        out.append(f'<h2 id="night-{mode}">{title} ({shown})</h2><div class="meta">'
                   f'{len(mine) - shown} more eligible but ranked below near-miss.</div>')
        for name, items in groups:
            if items:
                out.append(f'<div class="meta"><b>{esc(name)} ({len(items)})</b></div>')
                out.extend(_night_card(store, r, now, token, form_keys, _stale_days(policy)) for r in items[:30])
        if not shown:
            out.append('<div class="meta">none yet</div>')
    return out


def _stale_days(policy) -> int:
    return int(policy.get("discovery", "max_posting_age_days", default=STALE_DAYS) or STALE_DAYS)


STALE_RUN_HOURS = 26   # OPP-06: the daily run is due every 24 hours; two hours' grace before the warning


def run_health(store: Store, completing: tuple[str, str] | None = None) -> tuple[str, str | None]:
    """(line, warning) about the daily run, from the `runs` table (OPP-06, 2026-11-01): when the last run completed and how
    long ago, and a warning when the latest run failed or no run has completed for more than STALE_RUN_HOURS. A vault
    with no run yet gets no warning. The report header and `careeros status` show both.

    `completing` is (run_id, end time) of the daily run writing this report (Codex CR-2026-11-01-OBS-REVIEW, F5): it
    counts as completed then. The run writes its report just before it is marked completed, and a report that cannot be
    written fails the run, so the saved report never shows the previous run's health as its own."""
    now = store.clock.now()
    run_id, ended = completing or (None, None)
    status = "CASE WHEN run_id = :run THEN 'completed' ELSE status END"   # the completing run, as it will be recorded
    done = store.one(f"SELECT started_at, CASE WHEN run_id = :run THEN :ended ELSE ended_at END AS ended_at FROM runs "
                     f"WHERE {status} = 'completed' ORDER BY started_at DESC, rowid DESC LIMIT 1", {"run": run_id, "ended": ended})
    latest = store.one(f"SELECT started_at, {status} AS status FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1",
                       {"run": run_id})
    when = lambda t: f"{t[:16].replace('T', ' ')} UTC"
    age = None
    if done:
        finished = done["ended_at"] or done["started_at"]
        age = (_days(finished, now) or 0) * 24
        line = f"last completed run: {when(finished)} ({int(age)} h ago)"
    else:
        line = "last completed run: none yet"
    warning = None
    if latest and latest["status"] == "failed":
        warning = f"The latest daily run failed (started {when(latest['started_at'])}). Details are in vault\\logs\\daily-run.log."
    elif age is not None and age > STALE_RUN_HOURS:
        warning = f"No daily run has completed for {int(age)} hours. Check Task Scheduler and careeros status."
    return line, warning


HELD_DIGEST_DAYS = 14


def held_digests(store: Store) -> str | None:
    """A warning while an agency job-alert digest held back in the last HELD_DIGEST_DAYS days is still open
    (2026-11-09): its jobs may be missing, so the email itself must be opened. It names no agency. A digest read in
    full later closes its problem; an older one stops showing, but each new failure shows again."""
    now = store.clock.now()
    rows = store.all("SELECT occurred_at FROM exceptions WHERE error_code = 'agency_digest_held' AND status != 'resolved'")
    recent = sum(1 for r in rows if (_days(r["occurred_at"], now) or 0) <= HELD_DIGEST_DAYS)
    if not recent:
        return None
    return (f"{recent} agency job-alert digest(s) from the last {HELD_DIGEST_DAYS} days could not be read in full, so some "
            "of their jobs may be missing here. Open those emails themselves; careeros status lists the problems.")


def build_report(store: Store, profile, policy, *, token: str | None = None, form_keys: dict | None = None,
                 seed: int | None = None, since: str | None = None, app_matrix=None,
                 completing: tuple[str, str] | None = None, previews: dict | None = None) -> str:
    """The review page. `completing`: the daily run writing it, counted as completed in its header (run_health, F5).
    Read in one transaction (Store.read), so every card and its preview come from one snapshot; `previews` receives
    each label form's capture under its form key (the D190 design, section 3), and nothing is written."""
    context = _PREVIEWS.set((policy, profile, {} if previews is None else previews))
    listed, shown = _SHORTLISTED.set(set()), _CARDS_SHOWN.set(set())
    try:
        with store.read():
            return _build_report(store, profile, policy, token=token, form_keys=form_keys, seed=seed, since=since,
                                 app_matrix=app_matrix, completing=completing)
    finally:
        _PREVIEWS.reset(context)
        _SHORTLISTED.reset(listed)
        _CARDS_SHOWN.reset(shown)


def _build_report(store: Store, profile, policy, *, token: str | None, form_keys: dict | None, seed: int | None,
                  since: str | None, app_matrix, completing: tuple[str, str] | None) -> str:
    now = store.clock.now()
    _card = functools.partial(_plain_card, stale_days=_stale_days(policy), policy=policy)
    form_keys = {} if form_keys is None else form_keys
    counts = Counter(r["current_state"] for r in store.all("SELECT current_state FROM jobs"))
    scored = store.all("""SELECT j.*, s.tier, s.apply_priority, s.state FROM jobs j JOIN scores s ON s.score_id =
                          (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                          WHERE j.current_state = 'scored' ORDER BY s.apply_priority DESC""")
    tiers = Counter(r["tier"] for r in scored)
    # standing holds apart from the exceptions that wait to be dealt with (the D190 design, section 7; build item Y1)
    open_exc = store.all("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved' "
                         "AND COALESCE(error_code, '') NOT IN ('ingest_failed', 'read_pending')")[0]["c"]
    holds_open = store.all("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved' "
                           "AND error_code IN ('ingest_failed', 'read_pending')")[0]["c"]
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
           f'<title>CareerOS review</title><style>{CSS}</style></head><body><main>',
           f'<h1>CareerOS daily review</h1><div class="meta">{esc(now[:16].replace("T", " "))} UTC · policy {esc(policy.fingerprint[:10])} · '
           f'profile {esc(profile.fingerprint[:10])} · {open_exc} open exception(s) · {holds_open} standing hold(s)</div>']
    from careeros.review.packages import pending_file_problem
    held_file = pending_file_problem(store)
    if held_file:   # every Apply and Stretch label waits on it (BATCH_5 A)
        out.append(f'<div class="banner"><b>Every Apply and Stretch label waits:</b> {esc(held_file)}</div>')
    last_run, run_warning = run_health(store, completing)
    out.append(f'<div class="meta">{esc(last_run)}</div>')
    if run_warning:
        out.append(f'<div class="banner"><b>{esc(run_warning)}</b></div>')
    held = held_digests(store)
    if held:
        out.append(f'<div class="banner"><b>{esc(held)}</b></div>')
    from careeros.discovery.alerts import agency_setting_problem
    if agency_setting_problem(store):   # names no agency or host here; careeros status has the detail (L3-02)
        out.append('<div class="banner"><b>Alert intake is stopped: a job-alert setting is missing or changed, so no alert '
                   'email, saved email or inbox issue is read and the seen-jobs index is not published. Run careeros status '
                   'for what to restore.</b></div>')
    if not profile.approved:
        out.append('<div class="banner"><b>Profile not approved.</b> Scores are provisional until you fill in and approve '
                   '<code>vault/profile/candidate_profile_v0.yaml</code>. Unknown facts are scored as neutral, never as matches.</div>')
    backlog = label_backlog(store)   # OPP-01: postings close while they wait, so the wait is shown first
    if backlog:
        out.append(f'<div class="meta"><b>{esc(backlog["text"])}</b>: label them in the sections below.</div>')
    out.append('<div class="meta">' + " · ".join(f"{esc(k)}: {v}" for k, v in sorted(counts.items())) +
               " | tiers: " + " · ".join(f"{esc(k)}: {v}" for k, v in sorted(tiers.items())) + "</div>")

    out.extend(_shortlist_section(store, token, form_keys, policy))
    out.extend(_dismissed_section(store, token, form_keys))
    out.extend(_applications_section(store, now, token, form_keys, app_matrix))
    out.extend(_funnel_section(store))
    out.extend(_reconsidered_section(store, since))

    if since:
        fresh = [r for r in scored if r["first_seen_at"] > since]
        out.append(f"<h2>New since your last review ({len(_grouped(fresh))})</h2>"
                   f'<div class="meta">Graded postings first seen after {esc(since[:16].replace("T", " "))} UTC. Unverified Gmail and GitHub alert leads are counted separately below, not in this total.</div>')
        out.extend(_card(store, r, now, token, form_keys, n, new=True) for r, n in _grouped(fresh)[:30])
        if not fresh:
            out.append('<div class="meta">none</div>')

    tiers_cfg = policy.get("tiers", default={}) or {}
    conditional_min = (tiers_cfg.get("conditional") or {}).get("priority", 85)
    near_min = (tiers_cfg.get("near_miss") or {}).get("priority", 60)
    sections = [
        ("Tier A: exceptional", [r for r in scored if r["tier"] == "A"], ""),
        ("Tier B: strong apply", [r for r in scored if r["tier"] == "B"], ""),
        ("Conditional priority", [r for r in scored if r["state"] == "CONDITIONAL" and r["apply_priority"] >= conditional_min],
         "Strong scores, but a material fact is unknown. Resolve the flagged unknowns before applying."),
        ("On hold", [r for r in scored if r["state"] == "HOLD" and r["apply_priority"] >= near_min],
         "Viable, but the employer is on an eligibility hold until you decide about it. Kept for re-evaluation."),
        ("Near-miss: manual review", [r for r in scored if r["tier"] == "near_miss" and r["state"] != "HOLD"
                                      and not (r["state"] == "CONDITIONAL" and r["apply_priority"] >= conditional_min)], ""),
    ]
    for title, rows, note in sections:
        grouped = _grouped(rows)
        out.append(f"<h2>{esc(title)} ({len(grouped)})</h2>" + (f'<div class="meta">{esc(note)}</div>' if note else ""))
        out.extend(_card(store, r, now, token, form_keys, n) for r, n in grouped[:60])
        if not grouped:
            out.append('<div class="meta">none</div>')

    from .shortlist import shortlisted_job_ids
    out.extend(_check_section(store, scored, set(shortlisted_job_ids(store)), near_min))
    out.extend(_likely_section(store, token, form_keys, policy))
    out.extend(_leads_section(store, token, form_keys))
    out.extend(_notices_section(store, token, form_keys, policy))
    out.extend(_paste_form(token, form_keys))
    if (policy.get("night", default={}) or {}).get("enabled"):
        out.extend(_night_section(store, now, token, form_keys, policy))

    review = store.all("SELECT j.*, t.reason_code, t.notes FROM jobs j JOIN transitions t ON t.workflow_id = j.workflow_id "
                       "AND t.sequence = j.state_sequence WHERE j.current_state = 'needs_review' ORDER BY j.first_seen_at DESC")
    out.append(f"<h2>Needs review ({len(review)})</h2>"
               '<div class="meta">CareerOS could not read these postings\' requirements. Open them and label them yourself.</div>')
    out.extend(_card(store, r, now, token, form_keys, note=f"{r['reason_code']}: {r['notes']}") for r in review[:40])

    rejected = store.all("SELECT j.*, t.reason_code FROM jobs j JOIN transitions t ON t.workflow_id = j.workflow_id "
                         f"AND t.sequence = j.state_sequence WHERE j.current_state = 'rejected' AND NOT {NIGHT_ONLY} "
                         "ORDER BY j.first_seen_at DESC")
    reasons = Counter(r["reason_code"] for r in rejected)
    out.append(f"<h2>Rejected ({len(rejected)})</h2><div class=\"meta\">" +
               " · ".join(f"{esc(k)}: {v}" for k, v in reasons.most_common()) + "</div>")
    # Requirement-based rejections are all shown for labeling; the far larger title/location screen is audited by sample.
    screen_codes = ("no_target_family", "title_excluded", "not_remote", "outside_us", "generic_title_no_context")
    by_requirement = [r for r in rejected if r["reason_code"] not in screen_codes + HIDDEN_REJECTIONS]
    if by_requirement:
        out.append(f'<div class="meta">Rejected on stated requirements ({len(by_requirement)}). If one should have passed, label it '
                   'Apply and say which rule misfired.</div>')
        out.extend(_card(store, r, now, token, form_keys, n) for r, n in _grouped(by_requirement)[:40])
    screened = [r for r in rejected if r["reason_code"] in screen_codes]
    sample = random.Random(seed if seed is not None else now[:10]).sample(screened, min(3, len(screened)))
    if sample:
        out.append('<div class="meta">Audit: 3 random postings removed by the title/location screen.</div>')
        out.extend(_card(store, r, now, token, form_keys) for r in sample)
    archived = _grouped([r for r in scored if r["tier"] == "archive"])
    out.append(f'<h2>Archive ({len(archived)})</h2><div class="meta">Eligible but below the near-miss threshold. '
               f'Top {min(20, len(archived))} shown; label any you would apply to (it counts as a false rejection).</div>')
    out.extend(_card(store, r, now, token, form_keys, n) for r, n in archived[:20])
    missing = sorted((_SHORTLISTED.get() or set()) - (_CARDS_SHOWN.get() or set()))
    if missing:   # a shortlisted job whose card a section's cap left out: its card, with what a label approves
        out.append(f'<h2 id="shortlisted-cards">Shortlisted jobs not shown above ({len(missing)})</h2>')
        out.extend(_card(store, row, now, token, form_keys) for job_id in missing
                   if (row := store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))) is not None)
    out.extend(_sources_section(store, policy, now))
    out.append("</main></body></html>")
    return "".join(out)


# --- 2026-10-12: shortlist to action, eligibility questions, reconsiderations, manual intake, sources -----------------
DISMISS_REASONS = ("pay", "location", "remote", "schedule", "seniority", "skills", "qualifications", "domain", "employer",
                   "duplicate", "closed", "applied_elsewhere", "not_interested", "other")


def _dismiss_form(job_id: str, token: str | None, form_keys: dict, origin: str = "sl") -> str:
    """Dismiss: no reason is preselected and one is required; the server first shows a confirmation naming the posting
    and the reason, and only its Confirm records (OPP-16, 2026-11-01)."""
    if not token:
        return f'<span class="meta">dismiss: <code>careeros dismiss {esc(job_id)} --reason REASON</code></span>'
    options = "".join(f'<option value="{r}">{esc(r.replace("_", " "))}</option>' for r in DISMISS_REASONS)
    return _form("dismiss", job_id, token, form_keys,
                 f'{_origin(origin)}<select name="reason" required><option value="">dismiss: choose a reason</option>{options}</select> '
                 '<input type="text" name="note" placeholder="why (optional)"> <button>Dismiss</button>')


def _shortlist_section(store: Store, token, form_keys, policy=None) -> list[str]:
    from .shortlist import ageing, shortlist
    items = shortlist(store, policy)
    out = [f'<h2 id="shortlist">Shortlist ({len(items)})</h2><div class="meta">Your Apply / Stretch labels and Tier A/B picks, '
           'with the evidence, open questions and the next useful action. Drafts: <code>careeros draft JOB_ID</code> '
           '(only after you choose to pursue a job). Nothing is ever submitted for you.</div>']
    for i in items:
        bits = [f'<div class="card" id="sl-{esc(i["job_id"])}"><h3>{safe_link(i["apply_url"], i["title"])}</h3>',
                f'<div class="row">{_id_code(i["job_id"])}<span>{esc(i["employer"])}</span><span>{esc(i["status"])}</span>'
                f'<span>tier {esc(i["tier"] or "-")} · {esc(i["state"] or "-")}</span>'
                + (f'<span>your label: {esc(i["label"])}</span>' if i["label"] else "")
                + ('<span class="flag"><b>Stretch</b></span>' if i["stretch"] else "") + '</div>',
                f'<div class="row">{esc(ageing(i))}</div>',   # the clock: first seen, label age, package (OPP-08 + OPP-17)
                f'<div class="ok"><b>Next:</b> {esc(i["next_action"])}</div>']
        if i.get("submission"):
            bits.append(f'<div class="flag"><b>{esc(i["submission"])}</b></div>')
        if i["fit"]:
            bits.append("<ul>" + "".join(f"<li>{esc(f)}</li>" for f in i["fit"]) + "</ul>")
        if i["incompatible"] or i["unknowns"] or i["conflicts"]:
            bits.append("<ul>" + "".join(f'<li class="fail">{esc(f["facet"])}: {esc(f["detail"])}</li>' for f in i["incompatible"])
                        + "".join(f'<li class="flag">{esc(f["facet"])}: {esc(f["detail"])}</li>' for f in i["unknowns"][:4])
                        + "".join(f'<li class="flag">{esc(c["source"])} said {esc(c["fact"])} = {esc(c["said"])}</li>'
                                  for c in i["conflicts"][:3]) + "</ul>")
        if i["applied"]:
            bits.append(f'<div class="ok">Applied {esc(i["applied"]["applied_at"][:10])} via {esc(i["applied"]["channel"])}: '
                        f'{esc(i["applied"]["current_state"].replace("_", " "))}</div>')
        elif i["possibly_applied"]:
            bits.append(f'<div class="flag">You applied to a similar opening: {esc(", ".join(i["possibly_applied"][:2]))}</div>')
        # no label buttons here: this card does not show everything a label approves, so it links to the card that does
        # (a shortlisted job's card is always on the page; BATCH_5 C3)
        bits.append(f'<div class="meta">To label it, use <a href="#{esc(i["job_id"])}">its card</a>, which shows everything '
                    f'a label approves, or <code>careeros label {esc(i["job_id"])} apply</code>.</div>')
        if _SHORTLISTED.get() is not None:
            _SHORTLISTED.get().add(i["job_id"])
        if not i["applied"] or i["applied"]["current_state"] not in OPEN_STATES:
            bits.append(_apply_form(i["job_id"], token, form_keys, "sl"))
        bits.append(_dismiss_form(i["job_id"], token, form_keys, "sl") + "</div>")
        out.append("".join(bits))
    if not items:
        out.append('<div class="meta">none yet: label jobs Apply or Stretch to build it</div>')
    return out


def _dismissed_section(store: Store, token, form_keys) -> list[str]:
    """Postings you dismissed in the last RECENT_DAYS days and have not reinstated, newest first, each with Undo: a
    reinstatement, through the same confirmation as Dismiss (ADR 0007, amendment of 2026-11-01). Shown only when
    there is one."""
    rows = recent(store)
    if not rows:
        return []
    out = [f'<h2 id="dismissed">Dismissed recently ({len(rows)})</h2><div class="meta">Your dismissals of the last '
           f'{RECENT_DAYS} days. Undo brings a posting back: a later event, and the dismissal stays in its history.</div>']
    for r in rows[:30]:
        undo = (_form("reinstate", r["job_id"], token, form_keys, '<button>Undo</button>') if token
                else f'<span class="meta">undo: <code>careeros dismiss {esc(r["job_id"])} --undo</code></span>')
        out.append(f'<div class="card" id="dis-{esc(r["job_id"])}"><h3>{safe_link(r["url"], r["title"])}</h3><div class="row">'
                   f'{_id_code(r["job_id"])}<span>{esc(r["employer_name"])}</span>'
                   f'<span>dismissed {esc(r["at"][:10])}: {esc(r["reason_code"].replace("_", " "))}</span>'
                   + (f'<span>{esc(r["reason_text"])}</span>' if r["reason_text"] else "") + f'</div>{undo}</div>')
    return out


def _check_section(store: Store, scored, shortlisted: set, near_min: float) -> list[str]:
    rows = []
    for r in scored:
        if r["job_id"] in shortlisted or r["state"] == "INELIGIBLE" or r["apply_priority"] < near_min:
            continue
        score = store.one("SELECT components_json FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (r["job_id"],))
        unknowns = json.loads(score["components_json"]).get("decisive_unknowns") or []
        if 1 <= len(unknowns) <= 2 and not is_dismissed(store, r["job_id"]):   # the latest dismissal event decides
            rows.append((r, unknowns))
    out = [f'<h2 id="check">Check before applying ({len(rows)})</h2><div class="meta">Promising openings held back by one or two '
           'unconfirmed facts. Each line says exactly what to verify.</div>']
    out.extend(f'<div class="card"><h3>{safe_link(r["url"], r["title"])}</h3><div class="row"><span>{esc(r["employer_name"])}</span>'
               f'<span>priority {r["apply_priority"]:.0f}</span></div><ul>'
               + "".join(f'<li class="flag">verify {esc(u["facet"])}: {esc(u["detail"])}</li>' for u in unknowns) + "</ul></div>"
               for r, unknowns in rows[:25])
    if not rows:
        out.append('<div class="meta">none</div>')
    return out


def _reconsidered_section(store: Store, since: str | None) -> list[str]:
    if not since:
        return []
    rows = store.all("""SELECT c.job_id, c.summary, c.at, j.title, j.employer_name, j.url, j.current_state FROM job_changes c
                        JOIN jobs j ON j.job_id = c.job_id WHERE c.meaningful = 1 AND c.at > ? ORDER BY c.at DESC""", (since,))
    out = [f'<h2 id="reconsidered">Reconsidered after a meaningful change ({len(rows)})</h2><div class="meta">Pay, remote / state '
           'eligibility, schedule, employment type, required qualifications, deadline, title or a reopening changed, so these '
           'were judged again. Cosmetic edits are never listed here.</div>']
    out.extend(f'<div class="card"><h3>{safe_link(r["url"], r["title"])}</h3><div class="row"><span>{esc(r["employer_name"])}</span>'
               f'<span>now: {esc(r["current_state"])}</span><span>{esc(r["at"][:10])}</span></div>'
               f'<div class="flag">{esc(r["summary"])}</div></div>' for r in rows[:30])
    if not rows:
        out.append('<div class="meta">none</div>')
    return out


def _paste_form(token: str | None, form_keys: dict) -> list[str]:
    if not token:
        return ['<h2 id="paste">Add a posting you opened yourself</h2><div class="meta">Run <code>careeros review</code> for a paste '
                'box, or <code>careeros paste --url LINK --file posting.txt</code>. CareerOS never opens the link.</div>']
    return ['<h2 id="paste">Add a posting you opened yourself</h2><div class="meta">Paste the whole posting and its direct application '
            'link. CareerOS reads the text only (it never opens the link), grades it and tracks it.</div>',
            _form("paste", "new", token, form_keys,
                  '<input type="url" name="url" placeholder="direct application link" style="width:60%"> '
                  '<input type="text" name="company" placeholder="company (if not in the text)"><br>'
                  '<textarea name="text" rows="8" style="width:100%" placeholder="paste the posting text here"></textarea><br>'
                  '<button>Add and grade</button>')]


def _sources_section(store: Store, policy, now: str) -> list[str]:
    from .discovery_metrics import metrics
    m = metrics(store, policy, 30)
    reviews = store.all("""SELECT r.*, m.board_url, m.employer_name FROM board_reviews r LEFT JOIN board_mappings m ON m.mapping_id = r.mapping_id
                           WHERE r.status = 'open' ORDER BY r.created_at DESC""")
    paused = store.all("SELECT * FROM board_mappings WHERE paused_reason IS NOT NULL AND verification_status NOT IN "
                       "('historical','retired','rejected') ORDER BY updated_at DESC")
    degraded = store.all("SELECT * FROM provider_status WHERE state != 'healthy'")
    manual = store.all("SELECT * FROM board_mappings WHERE collection = 'manual_only' ORDER BY updated_at DESC")
    trouble = store.all("""SELECT e.name, s.outcome, s.error_code, s.at FROM source_scans s JOIN employers e ON e.employer_id = s.employer_id
                           WHERE s.outcome != 'complete' AND s.at >= ? ORDER BY s.at DESC""", (since_days(now, 7),))
    o, e = m["opportunities"], m["employers"]
    out = ['<h2 id="sources">Sources and discovery</h2>',
           f'<div class="meta">Last 30 days: {e["considered"]} employers considered, {e["verified"]} verified boards, '
           f'{o["new_qualified_from_discovery"]} new qualified openings from discovery, {o["lead_enrichment_matches"]} leads '
           f'turned into graded postings (time saved), {o["rescued_titles"]} titles rescued ({o["rescued_and_qualified"]} qualified), '
           f'{o["duplicate_reviews_avoided"]} duplicate reviews avoided. Pilot: {esc(m["pilot"]["status"])}.</div>']
    v = m["verification"]
    states = v["boards_by_status"]
    out.append(f'<div class="meta"><b>Board verification</b> (the employer\'s own site first): {states.get("active", 0)} verified, '
               f'{states.get("verification_required", 0) + states.get("review_required", 0)} awaiting re-verification or review, '
               f'{states.get("quarantined", 0)} in quarantine, {states.get("manual_only", 0)} manual only. '
               f'Explain any board with <code>careeros sources explain MAPPING</code>.</div>')
    unproven = store.all("""SELECT verification_failure f, COUNT(*) c FROM board_mappings WHERE provenance = 'legacy_unknown'
                            AND verification_status != 'active' AND paused_reason IS NULL AND collection = 'allowed' GROUP BY 1""")
    if unproven:
        labels = {None: "not checked yet", "domain_unknown": "website unknown", "no_handshake": "site shows no board link (JavaScript?)",
                  "site_unavailable": "site unreachable", "robots_disallowed": "site asks robots not to read it"}
        out.append(f'<div class="meta"><b>Legacy boards not yet proven by their employer\'s site: {sum(r["c"] for r in unproven)}</b> '
                   '(still scanned; nothing to do unless you want to: <code>careeros sources domain "Name" example.com</code> or '
                   '<code>careeros sources approve MAPPING --reason "..."</code>): '
                   + ", ".join(f'{r["c"]} {esc(labels.get(r["f"], r["f"] or ""))}' for r in unproven) + ".</div>")
    if degraded:
        out.append('<div class="meta"><b>Job-board providers degraded:</b> '
                   + ", ".join(f'{esc(d["provider"])} since {esc(d["since"][:10])}' for d in degraded)
                   + ' (their failures are not held against any employer).</div>')
    if paused:
        out.append(f'<div class="meta"><b>Paused boards ({len(paused)})</b>, not scanned until re-verified:</div><ul>'
                   + "".join(f'<li>{esc(p["employer_name"])}: {safe_link(p["board_url"], p["mapping_id"])}: {esc(p["paused_reason"])}</li>'
                             for p in paused[:15]) + "</ul>")
    if reviews:
        out.append(f'<div class="meta"><b>Boards needing your review ({len(reviews)}):</b> approve with '
                   '<code>careeros sources approve MAPPING --reason "..."</code> (expires), reject with '
                   '<code>careeros sources reject MAPPING</code>, or close with <code>careeros sources resolve CASE</code>.</div><ul>'
                   + "".join(f'<li>{esc(r["employer_name"] or r["entity_id"] or "")} <code>{esc(r["mapping_id"] or "")}</code> '
                             f'[{esc(r["reason_code"])}]: {esc(r["detail"])} <span class="meta">({esc(r["case_id"])})</span></li>'
                             for r in reviews[:15]) + "</ul>")
    if manual:
        out.append(f'<div class="meta"><b>Manual-only sources ({len(manual)}):</b> employer boards on systems CareerOS never reads '
                   'automatically. Check them yourself when you have time.</div><ul>'
                   + "".join(f'<li>{esc(x["employer_name"])}: {esc(x["ats"])} ({esc(x["board_token"])})</li>' for x in manual[:15]) + "</ul>")
    if trouble:
        out.append('<div class="meta"><b>Board scan problems this week</b> (never treated as closures):</div><ul>'
                   + "".join(f'<li>{esc(t["name"])}: {esc(t["outcome"])} {esc(t["error_code"] or "")} ({esc(t["at"][:10])})</li>'
                             for t in trouble[:15]) + "</ul>")
    return out


def since_days(now: str, days: int) -> str:
    from datetime import timedelta
    from careeros.core.clock import parse_utc
    return (parse_utc(now) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def last_review_at(store: Store) -> str | None:
    row = store.one("SELECT MAX(occurred_at) t FROM events WHERE event_type = 'review_opened'")
    return row["t"] if row else None
