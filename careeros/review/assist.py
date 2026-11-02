"""Assistance sessions (P2–P5, 2026-10-21; ADR 0009): how a prepared package reaches an employer's form, with you in charge.

A session is a reservation for one role while you apply, in one of three modes:
- `manual`: you copy from the package's answer panel (answers.md);
- `native`: you use the employer's own autofill (e.g. MyGreenhouse), with the package as your reference;
- `assisted`: the CareerOS browser extension fills the fields its fixed adapter knows, after showing you every value.
  It never clicks, submits or uploads.

Opening a session needs, at that moment:
- a package that is ready (careeros/review/packages.py: prepared, current, approved when it must be);
- `submission.authorize` allowed for a direct application (eligibility, the employer's identity, holds, and the shared
  duplicate / competing-submission check);
- no other session open or uncertain anywhere in the role;
- for `assisted`, a destination whose `fill` capability is `supervised_only` or `allowed`, and an adapter for it.
  config/site_policy.yaml (`assistance`) ships every destination at `unknown` (manual). You turn a reviewed one on
  yourself, in your vault overrides, once you have run the installed-extension check on it (see `assistance_policy`).

An assisted session writes a handoff file for the extension. It holds only the values for that form, is bound to the
destination's origin and posting address, and expires after two hours.

You close a session yourself:
- `submitted`: the application is recorded, with its evidence: your statement, or the confirmation you saw;
- `not_submitted`;
- `uncertain`: the page closed or errored around Submit. That blocks a new session for the role until you close it
  again with what really happened.

Nothing is ever resent automatically. Minutes, corrections, custom questions and login steps can be recorded when you
close a session; they are the P3 measurements and the P6 report.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from careeros.core.clock import parse_utc
from careeros.core.policy import load_yaml
from careeros.db.store import Store
from careeros.paths import CONFIG_DIR

from . import packages as pkg

MODES = ("manual", "native", "assisted")
OUTCOMES = ("submitted", "not_submitted", "uncertain")
CAPABILITIES = ("allowed", "supervised_only", "unknown", "blocked")
FILLABLE = ("allowed", "supervised_only")
HANDOFF_HOURS = 2
# Fixed, reviewed form adapters, by the hosts their forms live on (careeros/review/adapters.py builds their fields).
ADAPTER_HOSTS = {"greenhouse": ("job-boards.greenhouse.io", "boards.greenhouse.io")}


class SessionRefused(ValueError):
    pass


LEVELS = ("blocked", "unknown", "supervised_only", "allowed")   # least to most


def assistance_policy(path: Path | None = None, policy=None, today: str | None = None) -> dict:
    """What CareerOS may do on forms, by host: config/site_policy.yaml (`assistance`), then your own switch.

    The shipped file keeps filling at `unknown` (manual) everywhere and records, as `reviewed_fill`, the most a host
    was reviewed for. The Codex review of ac83e00 asked for a supervised check of the installed extension before it
    fills a real form (second Claude review, 2026-10-22), so only you can turn filling on, in your vault overrides
    (policy/overrides.yaml), naming the day you ran that check (docs/assisted_applications.md):

      assistance:
        greenhouse.io: {fill: supervised_only, installed_check: "2026-10-23"}

    Your switch can lower any host, but raise one only up to its `reviewed_fill`, and only with that date: a real
    calendar date written YYYY-MM-DD, quoted or not, and not after `today` when it is given."""
    data = load_yaml(path or CONFIG_DIR / "site_policy.yaml").get("assistance") or {}
    out = {}
    for host, caps in data.items():
        caps = caps or {}
        out[str(host).lower()] = {action: (caps.get(action) if caps.get(action) in CAPABILITIES else "unknown")
                                  for action in ("fill", "upload")}
        if caps.get("reviewed_fill") in CAPABILITIES:
            out[str(host).lower()]["reviewed_fill"] = caps["reviewed_fill"]
    out.setdefault("default", {"fill": "unknown", "upload": "blocked"})
    own = (policy.data.get("assistance") or {}) if policy is not None else {}
    if not isinstance(own, dict):
        raise ValueError("assistance_override_invalid: assistance must map hosts to {fill, installed_check}")
    for host, caps in own.items():
        host, caps = str(host).lower(), caps or {}
        base = out.get(host)
        if base is None or not isinstance(caps, dict) or set(caps) - {"fill", "installed_check"} \
                or caps.get("fill") not in CAPABILITIES:
            raise ValueError(f"assistance_override_invalid: {host}: only a listed host's fill can be set "
                             f"({', '.join(LEVELS)}), with installed_check")
        fill = caps["fill"]
        if LEVELS.index(fill) > LEVELS.index(base["fill"]):
            reviewed = base.get("reviewed_fill", base["fill"])
            if LEVELS.index(fill) > LEVELS.index(reviewed):
                raise ValueError(f"assistance_override_invalid: {host} was reviewed for fill: {reviewed}, not {fill}")
            given = caps.get("installed_check")
            try:
                checked = given if isinstance(given, date) else date.fromisoformat(given) \
                    if isinstance(given, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", given) else None
                if checked is None or (today is not None and checked.isoformat()[:10] > today):
                    raise ValueError("not a date on or before today")
            except ValueError as exc:
                raise ValueError(f"assistance_override_invalid: turning on filling for {host} needs installed_check: the "
                                 "date you ran the installed-extension check (docs/assisted_applications.md)") from exc
            base["installed_check"] = checked.isoformat()[:10]
        base["fill"] = fill
    return out


def capability(host: str, action: str, rules: dict | None = None) -> str:
    """What CareerOS may do on a form at `host`: the listed host or its nearest listed parent domain, else `default`."""
    rules = rules if rules is not None else assistance_policy()
    host = (host or "").lower().rstrip(".")
    parts = host.split(".")
    for i in range(len(parts) - 1):
        hit = rules.get(".".join(parts[i:]))
        if hit:
            return hit[action]
    return rules["default"][action]


def adapter_for(host: str) -> str | None:
    host = (host or "").lower()
    return next((name for name, hosts in ADAPTER_HOSTS.items() if host in hosts), None)


def _latest_outcome(store: Store, session_id: str):
    return store.one("SELECT * FROM assist_outcomes WHERE session_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (session_id,))


def session_state(store: Store, session_id: str) -> str:
    """open, submitted, not_submitted or uncertain: the latest outcome you recorded counts."""
    latest = _latest_outcome(store, session_id)
    return latest["outcome"] if latest else "open"


def role_sessions(store: Store, job_id: str) -> list[dict]:
    """Every session in the role (the job's whole duplicate / variant / same-opening group), with its state."""
    from .applications import role_group_resolved
    group, _ = role_group_resolved(store, job_id)
    marks = ",".join("?" * len(group))
    rows = store.all(f"SELECT * FROM assist_sessions WHERE job_id IN ({marks}) ORDER BY opened_at, rowid", tuple(group))
    return [{**dict(r), "state": session_state(store, r["session_id"])} for r in rows]


def session_for(store: Store, target: str) -> str:
    """The session `careeros assist close` acts on (OPP-77, 2026-11-01): a session id as given, or a job id whose role has
    exactly one session open or uncertain."""
    if store.one("SELECT 1 FROM assist_sessions WHERE session_id = ?", (target,)):
        return target
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (target,)) is None:
        raise ValueError("unknown_session")
    waiting = [s for s in role_sessions(store, target) if s["state"] in ("open", "uncertain")]
    if len(waiting) == 1:
        return waiting[0]["session_id"]
    if not waiting:
        raise ValueError(f"no_open_session: no session is open or uncertain for {target}'s role")
    raise ValueError("several_sessions: " + ", ".join(f"{s['session_id']} ({s['state']})" for s in waiting)
                     + " are open or uncertain for this role: close one by its SESSION_ID")


def open_session(store: Store, policy, profile, job_id: str, mode: str, *, facts: list, library: dict, vault_root: Path,
                 questions: list[dict] | None = None, key: str) -> dict:
    """Reserve the role and hand the package over (see the module docstring). Refused with every reason that applies."""
    if mode not in MODES:
        raise ValueError("unknown_mode")
    writer = store.writer("assist")
    written: Path | None = None
    try:
        with store.tx():   # the same key again returns the first result before any check (Codex review of ac83e00)
            if writer.receipt("assist_open", key, {"job_id": job_id, "mode": mode}, actor_id="user") == "replay":
                first = (_replayed(store, "assist_session_opened", key) or {}).get("result") or {}
                # the first result, and what became of that session since: a closed one is no reservation
                return {"replay": True, **first,
                        **({"state": session_state(store, first["session_id"])} if first.get("session_id") else {})}
            out, data = _open(store, writer, policy, profile, job_id, mode, facts=facts, library=library,
                              vault_root=vault_root, questions=questions, key=key)
            if data is not None:   # written before the session is committed: a file that can't be written refuses it
                written = vault_root / out["handoff"]
                written.parent.mkdir(parents=True, exist_ok=True)
                written.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    except BaseException:
        # a handoff whose session was not recorded must not stay behind; one whose session was (an interrupt after
        # COMMIT) must stay, or the reservation would have no file
        if written is not None and store.one("SELECT 1 FROM assist_sessions WHERE session_id = ?",
                                             (out["session_id"],)) is None:
            written.unlink(missing_ok=True)
        raise
    return out


def _replayed(store: Store, event_type: str, key: str) -> dict | None:
    row = store.one("SELECT metadata_json FROM events WHERE event_type = ? AND json_extract(metadata_json, '$.key') = ? "
                    "ORDER BY seq DESC LIMIT 1", (event_type, key))
    return json.loads(row["metadata_json"]) if row else None


def _open(store: Store, writer, policy, profile, job_id: str, mode: str, *, facts: list, library: dict, vault_root: Path,
          questions: list[dict] | None, key: str) -> tuple[dict, dict | None]:
    """open_session's checks and records, inside its transaction: a refusal rolls back the receipt too. Returns the
    result and the handoff to write. The result is kept in the event, so a repeat with the same key returns it whole
    (second Claude review, 2026-10-22)."""
    from .submission import authorize
    ready = pkg.readiness(store, policy, profile, job_id, facts=facts, library=library, vault_root=vault_root,
                          questions=questions)
    reasons = [] if ready["state"] == "ready" else [f"package {ready['state'].replace('_', ' ')}: " + "; ".join(ready["reasons"])]
    auth = authorize(store, policy, job_id, "direct", profile=profile)
    reasons += [f"{r['code']}: {r['detail']}" for r in auth.reasons]
    for s in role_sessions(store, job_id):
        if s["state"] == "open":
            reasons.append(f"session_open: session {s['session_id']} ({s['mode']}, opened {s['opened_at'][:16]}) is still open "
                           "for this role: close it first")
        elif s["state"] == "uncertain":
            reasons.append(f"submission_uncertain: session {s['session_id']} ended uncertain: close it with what really "
                           "happened before another one")
    package = ready["package"]
    manifest = json.loads(package["manifest_json"]) if package else None
    host = manifest["destination"]["host"] if manifest else ""
    adapter = adapter_for(host)
    if mode == "assisted":
        fill = capability(host, "fill", assistance_policy(policy=policy, today=store.clock.now()[:10]))
        if fill not in FILLABLE:
            reasons.append(f"assistance_not_permitted: filling forms on {host or 'this destination'} is '{fill}' "
                           "(config/site_policy.yaml and your vault overrides: assistance); use manual mode")
        if adapter is None:
            reasons.append(f"no_adapter: no reviewed form adapter for {host or 'this destination'}; use manual mode")
        if manifest is not None and manifest["questions"] is None:
            reasons.append("questions_unknown: this posting's form questions aren't known, so no field can be mapped")
        if adapter is not None and manifest is not None:   # one of the reasons, not a separate error after them
            from .adapters import destination
            try:
                destination(adapter, manifest["destination"]["url"])
            except ValueError as exc:
                reasons.append(f"{exc}; use manual mode")
    if reasons or package is None or manifest is None:
        raise SessionRefused("; ".join(reasons) or "package_missing")
    from .applications import role_group_resolved
    group, _ = role_group_resolved(store, job_id)
    now = store.clock.now()
    expires = (parse_utc(now) + timedelta(hours=HANDOFF_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z") if mode == "assisted" else None
    session_id = f"ses:{uuid.uuid4().hex[:16]}"
    data = None
    if mode == "assisted":   # built before anything is recorded: a handoff that can't be made refuses the session
        from .adapters import handoff
        data = handoff(adapter, session_id, package, manifest, library, expires)
    writer.insert("assist_sessions", {"session_id": session_id, "job_id": job_id, "package_id": package["package_id"],
                                      "manifest_sha256": package["manifest_sha256"], "mode": mode,
                                      "destination": manifest["destination"]["url"], "role_json": json.dumps(sorted(group)),
                                      "opened_at": now, "expires_at": expires, "actor": "user"})
    out = {"session_id": session_id, "mode": mode, "package_id": package["package_id"],
           "answers": (Path(package["folder"]) / "answers.md").as_posix(), "expires_at": expires,
           **({"handoff": f"handoff/{session_id.replace(':', '_')}.json"} if data is not None else {})}
    writer.event("assist_session_opened", outcome="success", entity_type="job", entity_id=job_id, actor_type="human",
                 actor_id="user", metadata={"session_id": session_id, "mode": mode, "package_id": package["package_id"],
                                            "manifest_sha256": package["manifest_sha256"], "adapter": adapter, "key": key,
                                            "result": out})
    return out, data


def close_session(store: Store, matrix, policy, session_id: str, outcome: str, *, key: str, evidence: str | None = None,
                  minutes: float | None = None, corrections: int | None = None, custom_questions: int | None = None,
                  login_steps: int | None = None, notes: str | None = None, reapply: str | None = None,
                  confirm: str | None = None) -> dict:
    """Close a session with what happened, by your account. `submitted` records the application (direct channel) with
    its evidence, through the same checks as `careeros applied`; `uncertain` blocks a new session for the role until a
    later close says what really happened. A session already closed submitted or not submitted can't be closed again."""
    if outcome not in OUTCOMES:
        raise ValueError("unknown_outcome")
    if outcome == "submitted":
        evidence = evidence or "owner_attested"
    elif evidence is not None:
        raise ValueError("evidence_only_for_submitted")
    for name, value in (("minutes", minutes), ("corrections", corrections), ("custom_questions", custom_questions),
                        ("login_steps", login_steps)):
        if value is not None and value < 0:
            raise ValueError(f"{name}_negative")
    writer = store.writer("assist")
    with store.tx():
        payload = {"session_id": session_id, "outcome": outcome, "evidence": evidence or "", "notes": notes or "",
                   "minutes": minutes, "corrections": corrections, "custom_questions": custom_questions,
                   "login_steps": login_steps, "reapply": reapply or "", "confirm": confirm or ""}
        if writer.receipt("assist_close", key, payload, actor_id="user") == "replay":
            first = _replayed(store, "assist_session_closed", key) or {}
            return {"replay": True, "outcome_id": first.get("outcome_id"), "application_id": first.get("application_id"),
                    "reconciled": bool(first.get("reconciles"))}
        session = store.one("SELECT * FROM assist_sessions WHERE session_id = ?", (session_id,))
        if session is None:
            raise ValueError("unknown_session")
        state = session_state(store, session_id)
        if state in ("submitted", "not_submitted"):
            raise SessionRefused(f"session_closed: this session already ended {state.replace('_', ' ')}")
        app_id = None
        if outcome == "submitted":
            from .applications import record_application
            app_id = record_application(store, matrix, session["job_id"], "direct", policy=policy, notes=notes,
                                        key=f"{key}:application", reapply=reapply, confirm=confirm,
                                        evidence=evidence or "owner_attested",
                                        session=session_id)
        outcome_id = f"aso:{uuid.uuid4().hex[:16]}"
        writer.insert("assist_outcomes", {"outcome_id": outcome_id, "session_id": session_id, "outcome": outcome,
                                          "evidence": evidence if outcome == "submitted" else None, "application_id": app_id,
                                          "minutes": minutes, "corrections": corrections, "custom_questions": custom_questions,
                                          "login_steps": login_steps, "notes": (notes or "").strip()[:500] or None,
                                          "at": store.clock.now(), "actor": "user"})
        writer.event("assist_session_closed", outcome="success", entity_type="job", entity_id=session["job_id"],
                     actor_type="human", actor_id="user",
                     metadata={"session_id": session_id, "outcome": outcome, "evidence": evidence, "application_id": app_id,
                               "reconciles": state == "uncertain", "outcome_id": outcome_id, "key": key})
    return {"outcome_id": outcome_id, "application_id": app_id, "reconciled": state == "uncertain"}


MEASURES = ("minutes", "corrections", "custom_questions", "login_steps")


def _host(session) -> str:
    """The destination host the session's package was prepared for (its manifest), else the session's own address."""
    try:
        host = ((json.loads(session["manifest_json"] or "{}").get("destination") or {}).get("host") or "")
    except ValueError:
        host = ""
    return (host or urlsplit(session["destination"] or "").hostname or "unknown").lower()


def report(store: Store) -> dict:
    """Measured assistance (P3 and P6; OPP-04, 2026-11-01): sessions by mode and by the destination host their package was
    prepared for, each with its counts by outcome. Medians are over SUBMITTED sessions only: one not submitted (abandoned)
    or uncertain is counted apart and never in a median. Beside the minutes you reported, the session span: the minutes
    from opening the session to its first close. Each median is over the sessions that recorded the measure; small samples
    are labelled as such."""
    rows = store.all("SELECT s.session_id, s.mode, s.opened_at, s.destination, p.manifest_json FROM assist_sessions s "
                     "LEFT JOIN packages p ON p.package_id = s.package_id")
    groups: dict = {}
    for r in rows:
        latest = _latest_outcome(store, r["session_id"])
        state = latest["outcome"] if latest else "open"
        entry = groups.setdefault((r["mode"], _host(r)), {"sessions": 0, "open": 0, "submitted": 0, "not_submitted": 0,
                                                          "uncertain": 0, "span_minutes": [], **{m: [] for m in MEASURES}})
        entry["sessions"] += 1
        entry[state] += 1
        if state == "submitted":   # medians: submitted sessions only (OPP-04)
            for name in MEASURES:
                if latest[name] is not None:
                    entry[name].append(latest[name])
            first = store.one("SELECT at FROM assist_outcomes WHERE session_id = ? ORDER BY at, rowid LIMIT 1",
                              (r["session_id"],)) or latest   # its first close (an uncertain one, if reconciled later)
            entry["span_minutes"].append((parse_utc(first["at"]) - parse_utc(r["opened_at"])) / timedelta(minutes=1))

    def median(values: list) -> float | None:
        values = sorted(values)
        if not values:
            return None
        mid = len(values) // 2
        return float(values[mid]) if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

    out: dict = {}
    for (mode, host), entry in sorted(groups.items()):
        row = {k: entry[k] for k in ("sessions", "open", "submitted", "not_submitted", "uncertain")}
        out.setdefault(mode, {})[host] = row
        for name in ("minutes", "span_minutes", *MEASURES[1:]):   # the span beside the minutes you reported
            value = median(entry[name])
            row[f"median_{name}"] = round(value, 1) if value is not None else None
            row[f"measured_{name}"] = len(entry[name])
        row["sample"] = "too small to compare (fewer than 5 measured)" if len(entry["minutes"]) < 5 else "measured"
    return out
