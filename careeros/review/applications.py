"""Applications you have made and their outcomes. Tracked from day one so response data starts accumulating.

One application per role: the role includes the job's location variants, duplicates, reposts and postings linked as the
same opening. A second application through a different channel (e.g. direct after an agency submitted you) is a
right-to-represent conflict.

Every path that asks "did I already apply?" uses one explainable check, `preflight` (2026-10-16): the review page, the
shortlist, NIGHT's right-to-represent check and recording itself. It looks at the role's whole history (an application
that was rejected or withdrawn still counts: applying again needs a reason), at postings that may be the same role
(linked as a possible duplicate or repost, or the same title at the same employer, e.g. one role posted in several
cities: shown, never held, by the owner's D188 of 2026-11-19) and at whether the duplicate search finished.

Recording is history, not permission (R2, 2026-10-16): what you already did is never refused. A conflict, a possible
duplicate, a submission hold (careeros.review.submission) or an unidentified employer only asks for your explanation and
is kept with the record. Recording never releases a hold; `submission.authorize` decides whether anything may submit.

A record made by mistake is voided, never deleted (ADR 0007, amendment of 2026-11-01): `void`, a state of the
application workflow reachable before anyone at the employer answered (applied, acknowledged, ghosted), with your
reason. The whole history decides, not only the state now: an application that was ever in an employer-answer stage
(responded, screening, interviewing and later) is never voided, even once it is ghosted (`void_refusal`, 2026-11-01).
A void application stays in the history and the applications list, and counts nowhere an application counts:
`preflight` (so every "did I already apply?" check, and applying again needs no reason), the funnel, the follow-up
nudges, `has_application` and the counts of the export, and the daily summary. Each of them asks through COUNTED.

A follow-up you sent is an event, not a state (OPP-21, 2026-11-03): `record_follow_up` adds one to the audit chain and
leaves the application's status and history as they were. Every view asks one rule, `follow_up_due` (QW-11): FR-062's
14 days without a response, counted from the later of entering the waiting state and your latest follow-up. A nudge is
a reminder, never outreach: CareerOS sends nothing.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from careeros.core.clock import format_utc, parse_utc
from careeros.core.transitions import Matrix, TransitionRejected, load_matrix
from careeros.discovery.note import is_alert_source
from careeros.db.store import Store
from careeros.paths import CONFIG_DIR

CHANNELS = ("direct", "agency", "referral", "other")
OPEN_CATEGORIES = ("active", "blocked")   # still in play; a ghosted (blocked) application can revive
CONFIRM_CODES = ("role_group_incomplete", "already_applied", "right_to_represent_conflict",
                 "submission_held", "employer_identity_unresolved", "submission_settings_invalid")   # each needs your explanation to record


class ApplicationConflict(ValueError):
    pass


class SessionOpen(ApplicationConflict):
    """The role has an assistance session still open or ended uncertain (OPP-77, 2026-11-01). If you applied through it,
    closing it records the application with its evidence and measurements, so `careeros applied` and the review page's
    "I applied" refuse and name the exact command (`command`)."""

    def __init__(self, session: dict):
        self.session_id = session["session_id"]
        self.command = f"careeros assist close {self.session_id} --outcome submitted"
        how = "session_open: session {} ({}, opened {}) is still open" if session["state"] == "open" else \
            "submission_uncertain: session {} ({}, opened {}) ended uncertain"
        super().__init__(how.format(self.session_id, session["mode"], session["opened_at"][:16]) +
                         f" for this role. If you applied, record it by closing the session: {self.command}")


class ApplicationHold(ApplicationConflict):
    """Recording needs an explicit acknowledgement: a re-application reason or a confirmation reason."""

    def __init__(self, message: str, check: "Preflight"):
        super().__init__(message)
        self.check = check


def application_matrix() -> Matrix:
    return load_matrix(CONFIG_DIR / "workflows" / "application.json")


def open_states(matrix: Matrix | None = None) -> tuple[str, ...]:
    """Application states still in play, read from the workflow file, so a state such as 'responded' is never missed."""
    matrix = matrix or application_matrix()
    return tuple(state for state, category in matrix.states.items() if category in OPEN_CATEGORIES)


OPEN_STATES = open_states()
VOID = "void"   # recorded by mistake: kept, never counted (2026-11-01)
COUNTED = f"current_state != '{VOID}'"   # SQL: an application that counts (every state but void)


def role_group_resolved(store: Store, job_id: str, limit: int = 200) -> tuple[list[str], bool]:
    """(members, complete): the job plus everything that is the same posting for applying purposes, by the owner's D188
    (2026-11-19, "Exact links only"): its duplicates (one posting at several links), followed in both directions, and
    postings linked as the same opening. A location variant or a repost an older engine recorded is no longer the same
    role (each is linked for your review as a possible duplicate). `complete` is False when the limit stopped the search
    early: a partial group is never treated as clearance."""
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        return [], True
    members, frontier = {job_id}, [job_id]
    while frontier:
        if len(members) >= limit:
            return sorted(members), False
        marks = ",".join("?" * len(frontier))
        rows = store.all(f"SELECT job_id, duplicate_of FROM jobs WHERE job_id IN ({marks}) OR duplicate_of IN ({marks})",
                         tuple(frontier) * 2)
        found = {x for r in rows for x in (r["job_id"], r["duplicate_of"]) if x}
        links = store.all(f"SELECT job_id, other_id FROM opening_links WHERE relation = 'same_opening' "
                          f"AND (job_id IN ({marks}) OR other_id IN ({marks}))", tuple(frontier) * 2)
        found |= {x for r in links for x in (r["job_id"], r["other_id"])}
        frontier = sorted(found - members)
        members |= found
    return sorted(members), True


def role_group(store: Store, job_id: str, limit: int = 200) -> list[str]:
    return role_group_resolved(store, job_id, limit)[0]


@dataclass
class Preflight:
    """One explainable answer to "may I apply / record an application here?". Each reason has a level: block (refused),
    hold (needs your reason: `reapply` for applied_before, `confirm` for the others) or warn (shown, never refused)."""
    job_id: str
    group: list[str]
    complete: bool
    applications: list                       # every application in the role group, newest first, any state but void
    open_applications: list                  # those still in play
    possible: list[str]                      # held until you confirm: always empty since the owner's D188
    distinct: list[str] = field(default_factory=list)   # jobs outside the group that may be the same role and carry an
                                                        # application: shown, never held (D188)
    reasons: list[dict] = field(default_factory=list)

    def add(self, code: str, level: str, detail: str) -> None:
        self.reasons.append({"code": code, "level": level, "detail": detail})

    def codes(self, level: str | None = None) -> list[str]:
        return [r["code"] for r in self.reasons if level is None or r["level"] == level]

    @property
    def decision(self) -> str:
        return "block" if self.codes("block") else "hold" if self.codes("hold") else "clear"


def _describe(app) -> str:
    return f"{app['channel']}{' (' + app['agency'] + ')' if app['agency'] else ''}"


def _possible_matches(store: Store, job, group: list[str]) -> tuple[list[str], list[str]]:
    """(possible, shown): jobs outside the role group that carry an application and may be the same role: linked as a
    possible duplicate or repost, or the same title at the same employer (one role posted in several cities). By the
    owner's D188 (2026-11-19, "Exact links only") nothing but the same posting holds an application, so `possible` (held
    until you confirm) is always empty and every such job is shown, never held."""
    marks = ",".join("?" * len(group))
    relations = "('possible_duplicate', 'possible_repost')"
    linked = {r["id"] for r in store.all(f"SELECT other_id AS id FROM opening_links WHERE job_id IN ({marks}) "
                                         f"AND relation IN {relations}", tuple(group))}
    linked |= {r["id"] for r in store.all(f"SELECT job_id AS id FROM opening_links WHERE other_id IN ({marks}) "
                                          f"AND relation IN {relations}", tuple(group))}
    employer = job["parent"] or job["employer_name"]
    if job["title_norm"] and employer:
        linked |= {r["job_id"] for r in store.all("SELECT job_id FROM jobs WHERE title_norm = ? AND COALESCE(parent, employer_name) = ?",
                                                  (job["title_norm"], employer))}
    shown = [other for other in sorted(linked - set(group))
             if store.one(f"SELECT 1 FROM applications WHERE job_id = ? AND {COUNTED} LIMIT 1", (other,))]
    return [], shown


def preflight(store: Store, job_id: str, channel: str | None = None, agency: str | None = None) -> Preflight:
    """The shared duplicate / right-to-represent check. With no channel it answers for display (any open application
    blocks); with a channel it answers for an application through that channel: another one through the same channel, or
    a different channel (right to represent), needs your explanation to record and stops `submission.authorize`."""
    job = store.one("SELECT job_id, title_norm, employer_name, parent, current_state, requisition_id FROM jobs WHERE job_id = ?",
                    (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    group, complete = role_group_resolved(store, job_id)
    marks = ",".join("?" * len(group))
    apps = store.all(f"SELECT * FROM applications WHERE job_id IN ({marks}) AND {COUNTED} ORDER BY applied_at DESC, rowid DESC",
                     tuple(group))   # a void application (a record made by mistake) is no application
    live = set(OPEN_STATES)
    open_apps = [a for a in apps if a["current_state"] in live]
    possible, distinct = _possible_matches(store, job, group)
    check = Preflight(job_id, group, complete, apps, open_apps, possible, distinct)
    agency = (agency or "").strip() or None
    for app in open_apps:
        when = f"on {app['applied_at'][:10]} via {_describe(app)} (status: {app['current_state'].replace('_', ' ')})"
        if channel is None:
            check.add("applied", "block", f"you applied to this role {when}")
        elif app["channel"] == channel and (app["agency"] or "") == (agency or ""):
            check.add("already_applied", "hold", f"you applied to this role {when}")
        else:
            check.add("right_to_represent_conflict", "hold", f"this role already has an application {when}: applying "
                                                             "through a second channel can disqualify you")
    closed = [a for a in apps if a["current_state"] not in live]
    if closed:
        last = closed[0]
        check.add("applied_before", "hold", f"you applied to this role before: {last['applied_at'][:10]} via {_describe(last)}, "
                                            f"now {last['current_state'].replace('_', ' ')}")
    if distinct:
        check.add("same_title_other_requisition", "warn", "you applied to a posting that may be the same role (another "
                                                          "posting: shown, never held, D188): " + ", ".join(distinct[:3]))
    if not complete:
        check.add("role_group_incomplete", "hold", "too many linked postings to finish the duplicate check: check by hand")
    if job["current_state"] == "closed":
        check.add("listing_closed", "warn", "the posting is closed")
    return check


# ChatGPT tags jobs it found in your Gmail alerts in the issue Note: `source=linkedin_alert` (prompts v2.5) or the
# older free-form "source: LinkedIn alert" (v2.3/v2.4). Both are read by careeros.discovery.note.is_alert_source.


def source_of(store: Store, job) -> tuple[str, str]:
    """(discovery source, profile) for a job: ChatGPT's Gmail-alert finds (its `source: ... alert` Note tag) or its
    web finds via the GitHub inbox, an alert email CareerOS read itself, CareerOS's own board scan, or a manual import;
    NIGHT when it reached CareerOS only as a NIGHT recommendation."""
    lead = store.one("SELECT sender, profile, context FROM leads WHERE job_id = ? ORDER BY first_seen_at LIMIT 1",
                     (job["job_id"],))
    if lead:
        if (lead["sender"] or "").startswith("GitHub inbox"):
            source = "chatgpt_gmail_alert" if is_alert_source(lead["context"]) else "chatgpt_inbox"
        else:
            source = "alert_email"
        return source, lead["profile"] or "primary"
    return ("board_scan" if job["source"] == "ats_api" else "manual_import"), "primary"


def application_context(store: Store, job_id: str) -> dict:
    """What was known when you applied, so outcomes can later be measured by source, family, profile, tier, channel
    and domain (prompts v2.2 conversion analytics). Stored once; never rewritten."""
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    score = store.one("SELECT tier, state, apply_priority, components_json FROM scores WHERE job_id = ? "
                      "ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    source, profile = source_of(store, job)
    domain = (json.loads(score["components_json"]).get("domain") or {}).get("level") if score else None
    night = store.one("SELECT mode FROM night_results WHERE job_id = ? AND decision = 'eligible' ORDER BY at DESC, rowid DESC "
                      "LIMIT 1", (job_id,))
    return {"source": source, "profile": profile, "family": job["family"], "employer": job["employer_name"],
            "tier": score["tier"] if score and job["current_state"] == "scored" else None,
            "state": score["state"] if score else None, "priority": score["apply_priority"] if score else None,
            "domain": domain, "night_mode": night["mode"] if night else None, "employment_type": job["employment_type"]}


EVIDENCE = ("owner_attested", "confirmation_observed")   # your statement; you saw the employer's confirmation (ADR 0009)


def sessions_waiting(store: Store, job_id: str, session: str | None = None) -> list[dict]:
    """Assistance sessions in the role still open or ended uncertain, other than `session` (OPP-77): while there is one,
    "I applied" records nothing and names its `assist close` command. The review page's confirmation checks this before
    it offers Confirm, and recording checks it again (ADR 0007, amendment of 2026-11-01)."""
    from .assist import role_sessions
    return [s for s in role_sessions(store, job_id) if s["state"] in ("open", "uncertain") and s["session_id"] != session]


def record_application(store: Store, matrix: Matrix, job_id: str, channel: str, *, policy, agency: str | None = None,
                       notes: str | None = None, key: str, reapply: str | None = None, confirm: str | None = None,
                       evidence: str = "owner_attested", session: str | None = None) -> str:
    """Record that you applied (your statement: evidence owner_attested, or confirmation_observed when you saw the
    employer's confirmation; `session` names the assistance session it closes). While another session in the role is open
    or uncertain it refuses (SessionOpen) and names the `assist close` command that records it. Never refused for what
    already happened: it
    waits for your reason when you applied to the role before (`reapply`), and for your explanation (`confirm`) when the
    role already has an application through this or another channel, it may duplicate another application, the duplicate
    check could not finish, a submission hold covers it or its employer isn't identified. Holds are read from `policy`
    now, never from a stored score. The record keeps what was held; it never releases a hold."""
    if channel not in CHANNELS:
        raise ValueError("unknown_channel")
    if evidence not in EVIDENCE:
        raise ValueError("unknown_evidence")
    agency = (agency or "").strip() or None
    if channel == "agency" and not agency:
        raise ValueError("agency_name_required")
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        raise ValueError("unknown_job")
    reapply = (reapply or "").strip()[:300] or None
    confirm = (confirm or "").strip()[:300] or None
    writer = store.writer("tracker")
    with store.tx():
        payload = {"job_id": job_id, "channel": channel, "agency": agency or "", "notes": notes or ""}
        payload.update({k: v for k, v in (("reapply", reapply), ("confirm", confirm), ("session", session)) if v})
        if evidence != "owner_attested":
            payload["evidence"] = evidence
        if writer.receipt("record_application", key, payload, actor_id="user") == "replay":
            return "replay"
        waiting = sessions_waiting(store, job_id, session)   # a session in progress is closed instead, never bypassed (OPP-77)
        if waiting:
            raise SessionOpen(waiting[-1])
        check = preflight(store, job_id, channel, agency)
        from .submission import HoldSettingsInvalid, agency_key, status as submission_status
        try:
            held = submission_status(store, policy, job_id)
        except HoldSettingsInvalid as exc:   # broken settings never refuse a record of what you did: they ask why
            held = None
            check.add("submission_settings_invalid", "hold", f"the submission hold settings are invalid ({exc})")
        if held is not None and not held.identified:
            check.add("employer_identity_unresolved", "hold", f"the hiring employer isn't identified ({held.identity.reason})")
        held_now = held.held_for(channel, agency if channel == "agency" else None) if held is not None else []
        for hold in held_now:
            check.add("submission_held", "hold", f"submission is held ({hold.hold_id}: {hold.reason}); recording it never "
                                                 "releases the hold")
        missing = [r for r in check.reasons if (r["code"] == "applied_before" and not reapply)
                   or (r["code"] in CONFIRM_CODES and not confirm)]
        if missing:
            raise ApplicationHold("needs_your_reason: " + "; ".join(r["detail"] for r in missing), check)
        now = store.clock.now()
        app_id = f"app:{uuid.uuid4().hex[:16]}"
        writer.insert("applications", {"application_id": app_id, "workflow_id": f"application:{app_id}", "job_id": job_id,
                                       "channel": channel, "agency": agency, "applied_at": now, "first_seen_at": now,
                                       "current_state": matrix.initial_state, "state_sequence": 0, "entered_state_at": now,
                                       "notes": (notes or "").strip()[:500] or None,
                                       "context_json": json.dumps(application_context(store, job_id))})
        writer.event("application_recorded", outcome="success", entity_type="application", entity_id=app_id,
                     workflow_id=f"application:{app_id}", actor_type="human", actor_id="user",
                     metadata={"job_id": job_id, "channel": channel, "agency": agency,
                               "second_channel": bool(check.open_applications), "evidence": evidence,
                               **({"session": session} if session else {}),
                               "preflight": check.decision, "checks": check.codes(), "reapply_reason": reapply,
                               "confirm_reason": confirm,
                               "submission": {"identity": held.identity.status if held else "settings_invalid",
                                              "family": held.identity.family if held else None,
                                              "held": [h.hold_id for h in held_now], "recorded_while_held": bool(held_now),
                                              "releases": [r["release_id"] for h in (held.holds if held else []) for r in h.releases
                                                           if r["channel"] == channel
                                                           and r["agency_norm"] == agency_key(channel, agency if channel == "agency" else None)]}})
    return app_id


def unanswered_states(matrix: Matrix | None = None) -> frozenset[str]:
    """The states before anyone at the employer answered, read from the workflow (config/workflows/application.json):
    those it lets you void from (applied, acknowledged and ghosted). Every other state is an employer-answer stage or an
    outcome after one (responded, screening, interviewing, final_interview, offer and the rest), so a workflow change is
    never missed here."""
    matrix = matrix or application_matrix()
    return frozenset(frm for (frm, to) in matrix.edges if to == VOID)


def void_refusal(store: Store, matrix: Matrix, app_id: str) -> str | None:
    """Why the application can't be voided, or None when it can: the one rule for the writer (update_application), the
    review page's confirmation and its Void control (Codex CR-2026-11-01-OBS-REVIEW, F2). Void is for a record made by
    mistake, before anyone at the employer answered (ADR 0007, amendment of 2026-11-01): the current state must allow
    it, and the application must never have been in an employer-answer stage. The current state alone is not enough:
    ghosted, and acknowledged after it, are reached again after an answer."""
    app = store.one("SELECT workflow_id, current_state FROM applications WHERE application_id = ?", (app_id,))
    if app is None:
        return "unknown_application"
    if (app["current_state"], VOID) not in matrix.edges:
        return f"transition_not_allowed: an application in state {app['current_state']} can't be voided"
    entered = {r["to_state"] for r in store.all("SELECT to_state FROM transitions WHERE workflow_id = ?", (app["workflow_id"],))}
    answered = sorted(entered - unanswered_states(matrix))
    if answered:   # an employer answered at some point: ghosted or acknowledged now does not make it a mistake
        return ("employer_answered: this application was " + ", ".join(s.replace("_", " ") for s in answered)
                + " before; void is only for a record made before anyone at the employer answered")
    return None


def update_application(store: Store, matrix: Matrix, app_id: str, to_state: str, *, notes: str | None = None, key: str,
                       reason: str | None = None) -> str:
    """Move an application to its next state through the workflow. A move that needs a reason (`void`) takes it in
    `reason`: kept as the transition's reason, checked again by `careeros verify`. Void also follows void_refusal: never
    after an employer answered, whatever the state now."""
    reason = (reason or "").strip()[:300] or None
    writer = store.writer("tracker")
    with store.tx():
        payload = {"app_id": app_id, "to": to_state, "notes": notes or "", **({"reason": reason} if reason else {})}
        if writer.receipt("update_application", key, payload, actor_id="user") == "replay":
            return "replay"
        if to_state == VOID:
            refusal = void_refusal(store, matrix, app_id)
            if refusal:
                raise ValueError(refusal)
        try:
            writer.transition(matrix, app_id, to_state, entity="application", trigger_type="human", trigger_id="user",
                              reason_code=reason, notes=(notes or "").strip()[:300] or None)
        except TransitionRejected as exc:
            raise ValueError(exc.code) from exc
    return "new"


FOLLOW_UP_DAYS = 14                     # FR-062: a follow-up nudge after 14 days without a response
FOLLOWED_UP = "application_followed_up"  # the event of a follow-up you sent (OPP-21)
WAITING = ("applied", "acknowledged")   # still in play, and nobody at the employer has answered yet


@dataclass(frozen=True)
class FollowUp:
    due: bool              # FOLLOW_UP_DAYS or more without a response
    days: int              # whole days since `since`
    since: str             # when the count started: entering the current state, or your latest follow-up if later
    after_follow_up: bool  # the count starts from your follow-up
    due_at: str            # when the nudge starts


def follow_ups(store: Store) -> dict[str, str]:
    """Each application's latest recorded follow-up (its time), by application id."""
    return {r["entity_id"]: r["at"] for r in store.all("SELECT entity_id, MAX(occurred_at) at FROM events WHERE event_type = ? "
                                                       "GROUP BY entity_id", (FOLLOWED_UP,))}


def follow_up_due(app, now: str, followed_up_at: str | None = None) -> FollowUp | None:
    """The one follow-up rule for the review page, the shortlist and `careeros explain` (QW-11, OPP-21, 2026-11-03):
    FR-062's FOLLOW_UP_DAYS without a response, counted from entering the current waiting state (applied or
    acknowledged) or from your latest recorded follow-up, whichever is later. None when no follow-up applies: someone at
    the employer answered, or the application is finished or void. A closed posting does not stop it: the employer may
    still answer the applications it has."""
    if app["current_state"] not in WAITING:
        return None
    entered: str = app["entered_state_at"]
    since = followed_up_at if followed_up_at and parse_utc(followed_up_at) > parse_utc(entered) else entered
    days = int((parse_utc(now) - parse_utc(since)) / timedelta(days=1))
    return FollowUp(days >= FOLLOW_UP_DAYS, days, since, since != entered,
                    format_utc(parse_utc(since) + timedelta(days=FOLLOW_UP_DAYS)))


def record_follow_up(store: Store, app_id: str, *, notes: str | None = None, key: str) -> str:
    """Record that you followed up on an application (OPP-21): an event in the audit chain, never a state, so the
    status and its history stay as they were; the follow-up nudge counts again from it. Only while the application is
    open (void and finished ones are refused). Nothing is sent: this records what you did."""
    notes = (notes or "").strip()[:300] or None
    writer = store.writer("tracker")
    with store.tx():
        if writer.receipt("record_follow_up", key, {"app_id": app_id, "notes": notes or ""}, actor_id="user") == "replay":
            return "replay"
        app = store.one("SELECT workflow_id, job_id, current_state FROM applications WHERE application_id = ?", (app_id,))
        if app is None:
            raise ValueError("unknown_application")
        if app["current_state"] not in OPEN_STATES:
            raise ValueError(f"not_open: the application is {app['current_state'].replace('_', ' ')}; a follow-up is "
                             "recorded only while it is open")
        writer.event(FOLLOWED_UP, outcome="success", entity_type="application", entity_id=app_id,
                     workflow_id=app["workflow_id"], actor_type="human", actor_id="user",
                     metadata={"job_id": app["job_id"], "state": app["current_state"], **({"notes": notes} if notes else {})})
    return "new"


def applications(store: Store) -> list:
    return store.all("""SELECT a.*, j.title, j.employer_name, j.url FROM applications a LEFT JOIN jobs j ON j.job_id = a.job_id
                        ORDER BY a.applied_at DESC""")
