"""Daily review report (local HTML). All posting-derived text is escaped: postings are untrusted input.

With a token (served by `careeros review`) every card has forms: label, record an application, update an application
status, dismiss an email lead. Each form carries a one-time key bound to (action, target) by the server.
"""
from __future__ import annotations

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

TIER_TITLES = {"A": "Tier A: exceptional", "B": "Tier B: strong apply", "near_miss": "Near-miss: manual review"}
STATE_NOTE = {"CONDITIONAL": "CONDITIONAL: check the flagged unknowns before applying",
              "HOLD": "HOLD: employer on hold (D13) until you decide about it"}
STALE_DAYS = 60  # default only: the policy's discovery.max_posting_age_days decides (2026-10-12)
NO_RESPONSE_DAYS = 14
# Jobs that reached CareerOS only as NIGHT recommendations: their PRIMARY verdict is not a PRIMARY decision to audit.
NIGHT_ONLY = ("(j.source = 'manual_url' AND j.job_id IN (SELECT job_id FROM leads WHERE profile = 'night') "
              "AND j.job_id NOT IN (SELECT job_id FROM leads WHERE profile = 'primary' AND job_id IS NOT NULL))")
HIDDEN_REJECTIONS = ("employer_excluded",)  # deliberate exclusions are not audited or labelled
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
    if job["comp_min"] is None and job["comp_max"] is None:
        return "pay not posted"
    unit = "/hr" if job["comp_period"] == "hour" else "/yr"
    lo, hi = job["comp_min"] or job["comp_max"], job["comp_max"] or job["comp_min"]
    return f"${lo:,.0f}–${hi:,.0f}{unit}"


def _latest(store: Store, table: str, job_id: str):
    return store.one(f"SELECT * FROM {table} WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def _form(action: str, target: str, token: str, form_keys: dict, body: str) -> str:
    key = secrets.token_hex(12)
    form_keys[key] = (action, target)
    return (f'<form method="post" action="/{action}?t={esc(token)}"><input type="hidden" name="target" value="{esc(target)}">'
            f'<input type="hidden" name="key" value="{key}">{body}</form>')


def _label_form(job_id: str, token: str | None, form_keys: dict) -> str:
    if not token:
        return f'<span class="meta">label: <code>careeros label {esc(job_id)} apply|stretch|skip</code></span>'
    buttons = "".join(f'<button name="label" value="{v}">{t}</button> ' for v, t in (("apply", "Apply"), ("stretch", "Stretch"), ("skip", "Skip")))
    return _form("label", job_id, token, form_keys, f'<input type="text" name="reason" placeholder="label reason (optional)"> {buttons}')


def _apply_form(job_id: str, token: str | None, form_keys: dict) -> str:
    if not token:
        return f'<span class="meta">applied? <code>careeros applied {esc(job_id)} --channel direct</code></span>'
    shortcut = private_sites.agency_shortcut()   # an agency named in the private settings, if any
    choices = [("direct", "directly"), *([(f"agency:{shortcut}", f"via {shortcut}")] if shortcut else []),
               ("agency", "via another agency"), ("referral", "by referral")]
    options = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in choices)
    return _form("apply", job_id, token, form_keys,
                 f'<select name="channel">{options}</select> <input type="text" name="agency" placeholder="agency name (if other)"> '
                 f'<button>I applied</button>')


def _status_form(app, token: str | None, form_keys: dict, app_matrix) -> str:
    if not token or app_matrix is None or app["current_state"] in app_matrix.terminal:
        return ""
    nexts = sorted(to for (frm, to) in app_matrix.edges if frm == app["current_state"])
    options = "".join(f'<option value="{esc(s)}">{esc(s.replace("_", " "))}</option>' for s in nexts)
    return _form("status", app["application_id"], token, form_keys,
                 f'<select name="state">{options}</select> <input type="text" name="notes" placeholder="note (optional)"> '
                 f'<button>Update status</button>')


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
                new: bool = False, stale_days: int = STALE_DAYS) -> str:
    score = _latest(store, "scores", job["job_id"])
    gate = _latest(store, "gate_results", job["job_id"])
    label = _latest(store, "labels", job["job_id"])
    app = store.one("SELECT * FROM applications WHERE job_id = ? ORDER BY applied_at DESC LIMIT 1", (job["job_id"],))
    states = json.loads(job["remote_states_json"] or "[]")
    parts = [f'<div class="card{" new" if new else ""}" id="{esc(job["job_id"])}"><h3>{safe_link(job["url"], job["title"])}</h3>',
             f'<div class="row"><span>{esc(job["employer_name"])}</span><span>{esc(job["location_raw"] or "location unknown")}'
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
                     + ('<span class="pill flag"><b>Stretch</b>: degree required</span>' if comps.get("stretch") else "")
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
    if gate:
        failures, flags = json.loads(gate["hard_failures_json"]), json.loads(gate["flags_json"])
        items = [f'<li class="fail">{esc(f["detail"])}'
                 + (f' <span class="meta">(your profile: {esc(f["profile_evidence"])}; confidence {f["confidence"]:.2f})</span>'
                    if f.get("profile_evidence") else "") + '</li>' for f in failures] \
            + [f'<li class="flag">{esc(f["detail"])}</li>' for f in flags[:8]]
        if items:
            parts.append("<ul>" + "".join(items) + "</ul>")
    if label:
        parts.append(f'<div class="labelled">your label: {esc(label["label"])}{" — " + esc(label["reason"]) if label["reason"] else ""}</div>')
    if app:
        parts.append(f'<div class="ok">Applied {esc(app["applied_at"][:10])} via {esc(app["channel"])}'
                     f'{" (" + esc(app["agency"]) + ")" if app["agency"] else ""} — status: {esc(app["current_state"].replace("_", " "))}</div>')
    parts.append(_label_form(job["job_id"], token, form_keys))
    if not app:
        parts.append(_apply_form(job["job_id"], token, form_keys))
    parts.append("</div>")
    return "".join(parts)


def _applications_section(store: Store, now: str, token, form_keys, app_matrix) -> list[str]:
    rows = store.all("""SELECT a.*, j.title, j.employer_name, j.url FROM applications a LEFT JOIN jobs j ON j.job_id = a.job_id
                        ORDER BY a.applied_at DESC""")
    if not rows:
        return []
    states = Counter(r["current_state"] for r in rows)
    out = [f"<h2>Your applications ({len(rows)})</h2><div class=\"meta\">" +
           " · ".join(f"{esc(k.replace('_', ' '))}: {v}" for k, v in states.most_common()) + "</div>"]
    for a in rows:
        waiting = _days(a["entered_state_at"], now) or 0
        nudge = (f" — no response in {int(waiting)} days: follow up, or mark it ghosted"
                 if a["current_state"] in ("applied", "acknowledged") and waiting >= NO_RESPONSE_DAYS else "")
        out.append(f'<div class="card" id="{esc(a["application_id"])}"><h3>{safe_link(a["url"], a["title"] or a["job_id"])}</h3><div class="row">'
                   f'<span>{esc(a["employer_name"])}</span><span>applied {esc(a["applied_at"][:10])} via {esc(a["channel"])}'
                   f'{" (" + esc(a["agency"]) + ")" if a["agency"] else ""}</span>'
                   f'<span class="{"flag" if nudge else "ok"}">status: {esc(a["current_state"].replace("_", " "))}{esc(nudge)}</span></div>'
                   + (f'<div class="row">note: {esc(a["notes"])}</div>' if a["notes"] else "")
                   + _status_form(a, token, form_keys, app_matrix) + "</div>")
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


def _leads_section(store: Store, token, form_keys) -> list[str]:
    leads = store.all("SELECT * FROM leads WHERE status = 'new' AND family IS NOT NULL ORDER BY first_seen_at DESC")
    if not leads:
        return []
    out = [f"<h2 id=\"leads\">Leads from alerts and recommendations ({len(leads)})</h2><div class=\"meta\">CareerOS cannot open these sites for you. "
           "Open the ones worth a look, save the posting page (Ctrl+S, 'Webpage, HTML only') into vault\\inbox, then run "
           "<code>careeros import</code> to grade it.</div>"]
    for lead in leads[:60]:
        dismiss = _form("lead", lead["lead_id"], token, form_keys, '<button name="action" value="dismiss">Dismiss</button>') if token else ""
        out.append(f'<div class="card"><h3>{safe_link(lead["url"], lead["title"])}</h3><div class="row">'
                   + ('<span class="flag">NIGHT</span>' if lead["profile"] == "night" else '')
                   + f'<span>{esc(lead["site"])}</span><span>family: {esc(lead["family"])}</span>'
                   f'<span>from: {esc(lead["sender"])}</span><span>{esc((lead["received_at"] or lead["first_seen_at"])[:10])}</span></div>'
                   + (f'<div class="row">{esc(lead["context"])}</div>' if lead["context"] else "") + dismiss + "</div>")
    return out


def _night_label_form(job_id: str, token: str | None, form_keys: dict) -> str:
    if not token:
        return f'<span class="meta">label: <code>careeros label {esc(job_id)} apply|stretch|skip --night</code></span>'
    buttons = "".join(f'<button name="label" value="{v}">{t}</button> ' for v, t in
                      (("apply", "Apply"), ("stretch", "Stretch"), ("skip", "Skip"), ("skip_schedule", "Skip: schedule doesn't fit")))
    return _form("label_night", job_id, token, form_keys, f'<input type="text" name="reason" placeholder="label reason (optional)"> {buttons}')


def _night_card(store: Store, r, now: str, token, form_keys, stale_days: int = STALE_DAYS) -> str:
    app = store.one("SELECT * FROM applications WHERE job_id = ? ORDER BY applied_at DESC LIMIT 1", (r["job_id"],))
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
            + _night_label_form(r["job_id"], token, form_keys)
            + (f'<div class="ok">Applied {esc(app["applied_at"][:10])} via {esc(app["channel"])} — status: '
               f'{esc(app["current_state"].replace("_", " "))}</div>' if app else _apply_form(r["job_id"], token, form_keys))
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
        groups = [
            ("Tier A", [r for r in mine if r["n_tier"] == "A"]),
            ("Tier B", [r for r in mine if r["n_tier"] == "B"]),
            ("Conditional priority", [r for r in mine if r["n_state"] == "CONDITIONAL" and r["n_priority"] >= conditional_min]),
            ("On hold (D13)", [r for r in mine if r["n_state"] == "HOLD" and r["n_priority"] >= near_min]),
            ("Near-miss", [r for r in mine if r["n_tier"] == "near_miss" and r["n_state"] != "HOLD"
                           and not (r["n_state"] == "CONDITIONAL" and r["n_priority"] >= conditional_min)]),
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


def build_report(store: Store, profile, policy, *, token: str | None = None, form_keys: dict | None = None,
                 seed: int | None = None, since: str | None = None, app_matrix=None) -> str:
    now = store.clock.now()
    _card = functools.partial(_plain_card, stale_days=_stale_days(policy))
    form_keys = {} if form_keys is None else form_keys
    counts = Counter(r["current_state"] for r in store.all("SELECT current_state FROM jobs"))
    scored = store.all("""SELECT j.*, s.tier, s.apply_priority, s.state FROM jobs j JOIN scores s ON s.score_id =
                          (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                          WHERE j.current_state = 'scored' ORDER BY s.apply_priority DESC""")
    tiers = Counter(r["tier"] for r in scored)
    open_exc = store.one("SELECT COUNT(*) c FROM exceptions WHERE status != 'resolved'")["c"]
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; form-action \'self\'">'
           f'<title>CareerOS review</title><style>{CSS}</style></head><body><main>',
           f'<h1>CareerOS daily review</h1><div class="meta">{esc(now[:16].replace("T", " "))} UTC · policy {esc(policy.fingerprint[:10])} · '
           f'profile {esc(profile.fingerprint[:10])} · {open_exc} open exception(s)</div>']
    if not profile.approved:
        out.append('<div class="banner"><b>Profile not approved.</b> Scores are provisional until you fill in and approve '
                   '<code>vault/profile/candidate_profile_v0.yaml</code>. Unknown facts are scored as neutral, never as matches.</div>')
    out.append('<div class="meta">' + " · ".join(f"{esc(k)}: {v}" for k, v in sorted(counts.items())) +
               " | tiers: " + " · ".join(f"{esc(k)}: {v}" for k, v in sorted(tiers.items())) + "</div>")

    out.extend(_shortlist_section(store, token, form_keys))
    out.extend(_applications_section(store, now, token, form_keys, app_matrix))
    out.extend(_funnel_section(store))
    out.extend(_reconsidered_section(store, since))

    if since:
        fresh = [r for r in scored if r["first_seen_at"] > since]
        out.append(f"<h2>New since your last review ({len(_grouped(fresh))})</h2>"
                   f'<div class="meta">Found after {esc(since[:16].replace("T", " "))} UTC, best first.</div>')
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
        ("On hold (D13)", [r for r in scored if r["state"] == "HOLD" and r["apply_priority"] >= near_min],
         "Viable, but the employer is on hold until you decide about it. Kept for re-evaluation."),
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
    out.extend(_leads_section(store, token, form_keys))
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
    out.extend(_sources_section(store, policy, now))
    out.append("</main></body></html>")
    return "".join(out)


# --- 2026-10-12: shortlist to action, eligibility questions, reconsiderations, manual intake, sources -----------------
DISMISS_REASONS = ("pay", "location", "remote", "schedule", "seniority", "skills", "qualifications", "domain", "employer",
                   "duplicate", "closed", "applied_elsewhere", "not_interested", "other")


def _dismiss_form(job_id: str, token: str | None, form_keys: dict) -> str:
    if not token:
        return f'<span class="meta">dismiss: <code>careeros dismiss {esc(job_id)} --reason REASON</code></span>'
    options = "".join(f'<option value="{r}">{esc(r.replace("_", " "))}</option>' for r in DISMISS_REASONS)
    return _form("dismiss", job_id, token, form_keys,
                 f'<select name="reason">{options}</select> <input type="text" name="note" placeholder="why (optional)"> '
                 '<button>Dismiss</button>')


def _shortlist_section(store: Store, token, form_keys) -> list[str]:
    from .shortlist import shortlist
    items = shortlist(store)
    out = [f'<h2 id="shortlist">Shortlist ({len(items)})</h2><div class="meta">Your Apply / Stretch labels and Tier A/B picks, '
           'with the evidence, open questions and the next useful action. Drafts: <code>careeros draft JOB_ID</code> '
           '(only after you choose to pursue a job). Nothing is ever submitted for you.</div>']
    for i in items:
        bits = [f'<div class="card" id="sl-{esc(i["job_id"])}"><h3>{safe_link(i["apply_url"], i["title"])}</h3>',
                f'<div class="row"><span>{esc(i["employer"])}</span><span>{esc(i["status"])}</span>'
                f'<span>tier {esc(i["tier"] or "-")} · {esc(i["state"] or "-")}</span>'
                + (f'<span>your label: {esc(i["label"])}</span>' if i["label"] else "")
                + ('<span class="flag"><b>Stretch</b></span>' if i["stretch"] else "") + '</div>',
                f'<div class="ok"><b>Next:</b> {esc(i["next_action"])}</div>']
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
        bits.append(_dismiss_form(i["job_id"], token, form_keys) + "</div>")
        out.append("".join(bits))
    if not items:
        out.append('<div class="meta">none yet: label jobs Apply or Stretch to build it</div>')
    return out


def _check_section(store: Store, scored, shortlisted: set, near_min: float) -> list[str]:
    rows = []
    for r in scored:
        if r["job_id"] in shortlisted or r["state"] == "INELIGIBLE" or r["apply_priority"] < near_min:
            continue
        score = store.one("SELECT components_json FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (r["job_id"],))
        unknowns = json.loads(score["components_json"]).get("decisive_unknowns") or []
        if 1 <= len(unknowns) <= 2 and not store.one("SELECT 1 FROM dismissals WHERE job_id = ?", (r["job_id"],)):
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
