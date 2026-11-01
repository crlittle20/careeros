"""Submission permission (R2, 2026-10-16): may anything SUBMIT an application for this posting, through this channel?

Kept apart from everything else:
- Eligibility and scoring decide whether a job qualifies. They never read this module, and it never changes a tier.
- Recording an application you already made (`applications.record_application`) is history: it is never refused for a
  hold, only annotated and asked for your explanation, and it never creates a release.
- `authorize` answers BEFORE a submission. Nothing in CareerOS submits applications today (ADR 0007): the review page and
  `careeros applied` only record what you did. A future tool that submits must call `authorize` first and be listed in
  ADAPTERS (tests/test_submission.py fails when code that can send something appears outside its allowlist).

Holds come from policy `submission.holds` (the vault): each names employer families and the channels it covers. Until
that list is set, the gate's `held_parents` (D13) counts as one hold, so there is never a gap. Who the employer is comes
from careeros.normalize.employer_identity, resolved now from the posting's names (with your private agencies, the board
aliases and the names you listed as different organisations): never from a stored score or a stored HOLD state.

A release (`careeros release`) is yours alone: one named hold, one posting, one channel and, for an agency, one agency.
It clears only that hold; authorization still checks eligibility, the employer's identity, every other hold and the
duplicate / competing-submission checks. A release stays valid while it is not revoked, the posting has had no material
change since (a meaningful job_changes row, a new requisition or address) and the employer still resolves to the same
family. A confirmation reason, a label, a draft, a recorded application, a tier, a new posting or a scheduled run never
releases anything, and a release never passes to another city's posting of the same role, a repost or the employer family.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from careeros.core import private_sites
from careeros.db.store import Store
from careeros.normalize.employer_identity import (RESOLVED, EmployerIdentity, in_families, legal_norm, listed_agencies,
                                                  resolve, resolve_job)

from .applications import CHANNELS

LEGACY_HOLD = "gate-held-parents"
BOARD_ALIAS_RELATIONS = ("alias", "legal", "former", "subsidiary")
ADAPTERS: dict = {}   # future submission tools, by name; none exist. Each must call authorize() before sending anything.


class HoldSettingsInvalid(ValueError):
    """`submission.holds` (or the legacy held_parents) is malformed: authorization and releases refuse, recording asks
    for your explanation, and pages show it; nothing is ever read as "no hold"."""


def _names_ok(names) -> bool:
    return isinstance(names, list) and bool(names) and all(isinstance(n, str) and legal_norm(n) for n in names)


def holds(policy) -> list[dict]:
    """The submission holds in force: policy `submission.holds`, or, while that is not set, the gate's held_parents.
    Strict (2026-10-18): a list of mappings, each with an id, a non-empty LIST of names that each normalise to a name,
    and channels "all" or a non-empty list of known channels. Anything else raises HoldSettingsInvalid, so a mistyped
    hold can never hold nothing in silence."""
    configured = policy.get("submission", "holds", default=None)
    if configured is None:
        legacy = policy.get("gate", "held_parents", default=[]) or []
        if not isinstance(legacy, list) or (legacy and not _names_ok(legacy)):
            raise HoldSettingsInvalid(f"submission_hold_invalid: gate.held_parents must be a list of names, not {legacy!r}")
        return [{"id": LEGACY_HOLD, "names": list(legacy), "channels": "all",
                 "reason": "employer hold (D13): the gate's held_parents list, until submission holds are set"}] if legacy else []
    if not isinstance(configured, list):
        raise HoldSettingsInvalid(f"submission_hold_invalid: submission.holds must be a list, not {type(configured).__name__}")
    out, seen = [], set()
    for item in configured:
        if not isinstance(item, dict):
            raise HoldSettingsInvalid(f"submission_hold_invalid: {item!r}")
        channels = item.get("channels", "all")
        hold = {"id": str(item.get("id") or "").strip(), "names": item.get("names"), "channels": channels,
                "reason": str(item.get("reason") or "").strip()}
        channels_ok = channels == "all" or (isinstance(channels, list) and bool(channels)
                                            and all(isinstance(c, str) for c in channels) and set(channels) <= set(CHANNELS))
        if not hold["id"] or hold["id"] in seen or not _names_ok(hold["names"]) or not channels_ok:
            raise HoldSettingsInvalid(f"submission_hold_invalid: {item!r}")
        seen.add(hold["id"])
        out.append(hold)
    return out


def _covers(hold: dict, channel: str) -> bool:
    return hold["channels"] == "all" or channel in hold["channels"]


def _identity_inputs(store: Store, policy) -> dict:
    """The same inputs for a posting and for the hold names: every staffing agency (policy and private), every board
    alias (an alias that points to two employers keeps both, so it can be found ambiguous), and your guards."""
    marks = ",".join("?" * len(BOARD_ALIAS_RELATIONS))
    board: dict[str, list[str]] = {}
    for row in store.all(f"SELECT alias_norm, employer_norm FROM employer_aliases WHERE relation IN ({marks}) "
                         "ORDER BY alias_norm, employer_norm", BOARD_ALIAS_RELATIONS):
        board.setdefault(legal_norm(row["alias_norm"]), []).append(row["employer_norm"])
    return {"agencies": [*listed_agencies(policy), *private_sites.staffing_agencies()], "board_aliases": board,
            "different": policy.get("employer_identity", "different_organisations", default=[]) or []}


def families(policy, **inputs) -> dict:
    """The employer families a hold is matched against: policy `parents`, plus each hold name that names no family as a
    family of its own (2026-10-17). So a hold on "Held Parent" also covers "Held Parent Services" and "HeldParent", by
    the same exact, compact and prefix rules as a listed family, instead of the exact name only."""
    parents = dict(policy.get("parents", default={}) or {})
    for hold in holds(policy):
        for name in hold["names"]:
            if not resolve(name, None, parents, **inputs).known_family:
                parents.setdefault(name, [])
    return parents


def _check_channel(channel: str, agency: str | None) -> str:
    """The normalized agency ('' unless the channel is agency)."""
    if channel not in CHANNELS:
        raise ValueError("unknown_channel")
    name = legal_norm(agency)
    if channel == "agency" and not name:
        raise ValueError("agency_name_required")
    if channel != "agency" and name:
        raise ValueError("agency_only_with_the_agency_channel")
    return name


@dataclass
class HoldState:
    hold_id: str
    reason: str
    channels: object
    releases: list[dict] = field(default_factory=list)    # valid: {release_id, channel, agency_norm, agency, at}
    suspended: list[dict] = field(default_factory=list)   # no longer valid, with the problem

    def released_for(self, channel: str, agency_norm: str = "") -> dict | None:
        return next((r for r in self.releases if r["channel"] == channel and r["agency_norm"] == agency_norm), None)


@dataclass
class SubmissionStatus:
    job_id: str
    identity: EmployerIdentity
    holds: list[HoldState]          # the holds that apply to this posting's employer

    @property
    def identified(self) -> bool:
        return self.identity.status == RESOLVED

    def held_for(self, channel: str, agency: str | None = None) -> list[HoldState]:
        """The applicable holds that cover this channel and have no valid release for it (and this agency)."""
        name = _check_channel(channel, agency)
        hold_list = [h for h in self.holds if _covers({"channels": h.channels}, channel)]
        return [h for h in hold_list if not h.released_for(channel, name)]


def _problem(store: Store, job, release, identity: EmployerIdentity) -> str | None:
    """Why an unrevoked release no longer counts, or None."""
    if identity.status != RESOLVED or identity.key != release["family_key"]:
        return "the employer's identity changed since the release"
    if (job["requisition_id"] or None) != (release["requisition_id"] or None) \
            or (job["canonical_url"] or None) != (release["canonical_url"] or None):
        return "the posting's requisition or address changed since the release"
    # a change recorded after the release counts even when the clock went back in between (Codex review, 2026-10-20): a
    # release keeps how many changes the posting had (identity_json "changes_seen")
    granted = json.loads(release["identity_json"] or "null")
    seen = granted.get("changes_seen") if isinstance(granted, dict) else None
    if not isinstance(seen, int):
        # a release written before change counts were kept could miss a change recorded behind a clock that went back
        # (Codex Pass 2, 2026-10-21); your decision of 2026-10-21: it no longer counts, so the posting is released again
        return "the release predates change tracking; release the posting again"
    change = store.one("SELECT summary FROM job_changes WHERE job_id = ? AND meaningful = 1 AND (at >= ? OR rowid > ?) "
                       "ORDER BY at, rowid LIMIT 1", (job["job_id"], release["at"], seen))
    if change:
        return f"the posting changed materially since the release ({change['summary']})"
    return None


def status(store: Store, policy, job_id: str) -> SubmissionStatus:
    """Who the employer is, which holds apply, and which of your releases still count. Reads policy now; never scores."""
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    inputs = _identity_inputs(store, policy)
    parents = families(policy, **inputs)
    identity = resolve_job(dict(job), policy, parents=parents, **inputs)
    rows = store.all("SELECT * FROM submission_releases WHERE job_id = ? ORDER BY at, rowid", (job_id,))
    revoked = {r["revokes"] for r in rows if r["action"] == "revoke"}
    states = []
    for hold in holds(policy):
        if in_families(identity, hold["names"], parents, **inputs) is not True:
            continue   # not this family; an unresolved identity is handled by `identified`, never as "not held"
        state = HoldState(hold["id"], hold["reason"], hold["channels"])
        for r in rows:
            if r["action"] != "release" or r["hold_id"] != hold["id"] or r["release_id"] in revoked:
                continue
            item = {"release_id": r["release_id"], "channel": r["channel"], "agency_norm": r["agency_norm"], "agency": r["agency"],
                    "at": r["at"]}
            problem = _problem(store, job, r, identity)
            (state.suspended if problem else state.releases).append({**item, **({"problem": problem} if problem else {})})
        states.append(state)
    return SubmissionStatus(job_id, identity, states)


@dataclass
class Authorization:
    job_id: str
    channel: str
    agency: str | None
    reasons: list[dict]
    status: SubmissionStatus

    @property
    def allowed(self) -> bool:
        return not self.reasons


def authorize(store: Store, policy, job_id: str, channel: str, agency: str | None = None, profile=None) -> Authorization:
    """May a submission be made now? Every check must pass: the gate found the job eligible, the posting is open, the
    employer is identified, each hold covering the channel has a valid release, and the duplicate / competing-submission
    check (`applications.preflight`) raises nothing that would block or need your reason."""
    from .applications import preflight
    _check_channel(channel, agency)
    current = status(store, policy, job_id)
    job = store.one("SELECT current_state, closed_at FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    gate = store.one("SELECT decision, policy_fingerprint, profile_fingerprint FROM gate_results WHERE job_id = ? "
                     "ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
    reasons = []
    if gate is None or gate["decision"] != "eligible":
        reasons.append({"code": "not_eligible", "detail": f"the gate's latest decision is {gate['decision'] if gate else 'missing'}"})
    elif not _current(job, gate, policy, profile):
        reasons.append({"code": "eligibility_not_current", "detail": (
            "no current profile was given, so the eligibility decision can't be shown to be current" if profile is None else
            "the eligibility decision predates the current posting, rules or profile: let the daily processing judge it again")})
    if job["current_state"] in ("closed", "duplicate") or job["closed_at"]:
        reasons.append({"code": "posting_not_open", "detail": f"the posting is {job['current_state']}"})
    if not current.identified:
        reasons.append({"code": "employer_identity_unresolved", "detail": f"the hiring employer isn't identified ({current.identity.reason})"})
    for hold in current.held_for(channel, agency):
        reasons.append({"code": "submission_held", "detail": f"{hold.hold_id}: {hold.reason}"})
    check = preflight(store, job_id, channel, agency)
    reasons += [{"code": r["code"], "detail": r["detail"]} for r in check.reasons if r["level"] in ("block", "hold")]
    return Authorization(job_id, channel, agency, reasons, current)


def _current(job, gate, policy, profile) -> bool:
    """The eligible decision was made for this posting as it is now, under today's rules and profile (2026-10-18): the job
    is judged (not sent back for re-judging by a posting edit) and the stored fingerprints match."""
    from types import SimpleNamespace

    from careeros.pipeline import decision_fingerprint
    if job["current_state"] not in ("eligible", "scored"):
        return False
    if gate["policy_fingerprint"] != decision_fingerprint(SimpleNamespace(policy=policy)):   # type: ignore[arg-type]
        return False
    # without your profile nothing is assumed current (independent Codex review of f984d15, 2026-10-20)
    return profile is not None and gate["profile_fingerprint"] == profile.fingerprint


def agency_key(channel: str, agency: str | None) -> str:
    """The normalized agency a release is stored under ('' unless the channel is agency)."""
    return _check_channel(channel, agency)


def display(store: Store, policy, job_id: str) -> str | None:
    """The badge line for a page or report, also when the hold settings are broken (never an exception there)."""
    try:
        return badge(status(store, policy, job_id))
    except HoldSettingsInvalid as exc:
        return f"Submission can't be authorized: the submission hold settings are invalid ({exc}). Fix them in the vault overrides."


def release(store: Store, policy, job_id: str, channel: str, *, hold_id: str, reason: str, agency: str | None = None,
            key: str) -> str:
    """Release ONE hold for ONE posting through ONE channel (and agency). Refused when the employer isn't identified, the
    hold doesn't apply to this posting, or the same release already stands. Returns the release id, or 'replay'."""
    agency_norm = _check_channel(channel, agency)
    reason = (reason or "").strip()[:300]
    if not reason:
        raise ValueError("reason_required")
    writer = store.writer("submission")
    with store.tx():
        payload = {"job_id": job_id, "channel": channel, "agency": agency_norm, "hold_id": hold_id, "reason": reason}
        if writer.receipt("submission_release", key, payload, actor_id="user") == "replay":
            return "replay"
        current = status(store, policy, job_id)
        if not current.identified:
            raise ValueError(f"employer_identity_unresolved: {current.identity.reason}; identify the employer first")
        state = next((h for h in current.holds if h.hold_id == hold_id), None)
        if state is None:
            raise ValueError(f"not_held_by: {hold_id} does not apply to this posting")
        if not _covers({"channels": state.channels}, channel):
            raise ValueError(f"not_held_by: {hold_id} does not cover the {channel} channel")
        if state.released_for(channel, agency_norm):
            raise ValueError("already_released")
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if job is None:
            raise ValueError("unknown_job")
        release_id = f"rel:{uuid.uuid4().hex[:16]}"
        seen = store.one("SELECT COALESCE(MAX(rowid), 0) AS n FROM job_changes WHERE job_id = ?", (job_id,))
        writer.insert("submission_releases", {
            "release_id": release_id, "action": "release", "revokes": None, "job_id": job_id, "hold_id": hold_id,
            "channel": channel, "agency_norm": agency_norm, "agency": (agency or "").strip() or None, "reason": reason,
            "family_key": current.identity.key,
            "identity_json": json.dumps({"evidence": [e.__dict__ for e in current.identity.evidence],
                                         "changes_seen": seen["n"] if seen else 0}, sort_keys=True),
            "content_fingerprint": job["content_fingerprint"], "requisition_id": job["requisition_id"],
            "canonical_url": job["canonical_url"], "at": store.clock.now(), "actor": "user"})
        writer.event("submission_released", outcome="success", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id="user", metadata={"release_id": release_id, "hold_id": hold_id, "channel": channel,
                                                "agency": agency_norm, "reason": reason})
    return release_id


def revoke(store: Store, release_id: str, *, reason: str, key: str) -> str:
    """Withdraw a release. Returns the revocation id, or 'replay'."""
    reason = (reason or "").strip()[:300]
    if not reason:
        raise ValueError("reason_required")
    writer = store.writer("submission")
    with store.tx():
        if writer.receipt("submission_revoke", key, {"release_id": release_id, "reason": reason}, actor_id="user") == "replay":
            return "replay"
        original = store.one("SELECT * FROM submission_releases WHERE release_id = ? AND action = 'release'", (release_id,))
        if original is None:
            raise ValueError("unknown_release")
        if store.one("SELECT 1 FROM submission_releases WHERE revokes = ?", (release_id,)):
            raise ValueError("already_revoked")
        revoke_id = f"rev:{uuid.uuid4().hex[:16]}"
        writer.insert("submission_releases", {
            "release_id": revoke_id, "action": "revoke", "revokes": release_id, "job_id": original["job_id"],
            "hold_id": original["hold_id"], "channel": original["channel"], "agency_norm": original["agency_norm"],
            "agency": original["agency"], "reason": reason, "at": store.clock.now(), "actor": "user"})
        writer.event("submission_release_revoked", outcome="success", entity_type="job", entity_id=original["job_id"],
                     actor_type="human", actor_id="user", metadata={"release_id": release_id, "reason": reason})
    return revoke_id


def badge(current: SubmissionStatus) -> str | None:
    """One line for the review card and the shortlist, or None when nothing is held. Never about qualification."""
    if not current.identified:
        return (f"Submission can't be authorized: the hiring employer isn't identified ({current.identity.reason}). "
                "Preparing is fine.")
    if not current.holds:
        return None
    parts = []
    for h in current.holds:
        released = ", ".join(r["channel"] + (f" ({r['agency']})" if r["agency"] else "") for r in h.releases)
        parts.append(f"{h.hold_id}: {h.reason}" + (f"; released for {released} only" if released else "")
                     + "".join(f"; a release for {r['channel']} is suspended: {r['problem']}" for r in h.suspended))
    return ("Submission held (" + " | ".join(parts) + "). Preparing is fine; submitting needs your release for this posting "
            f"and channel: careeros release {current.job_id} --hold ID --channel ... --reason ...")


def describe(store: Store, policy, job_id: str, agency: str | None = None, profile=None) -> str:
    """The `careeros release JOB_ID` view: identity and its evidence, holds and releases, and authorization per channel
    (with your current profile; without it nothing is shown as allowed)."""
    current = status(store, policy, job_id)
    ident = current.identity
    lines = [f"employer identity: {ident.status}" + (f" -> {ident.family}" if ident.family else "") + f" ({ident.reason})"]
    lines += [f"  {e.field}: {e.value!r} -> {e.rule}" + (f" via {e.via}" if e.via else "") for e in ident.evidence]
    lines.append("holds: " + (", ".join(h.hold_id for h in current.holds) or "none apply"))
    for h in current.holds:
        lines += [f"  {h.hold_id} released for {r['channel']}{' (' + r['agency'] + ')' if r['agency'] else ''} on {r['at'][:10]}"
                  f" [{r['release_id']}]" for r in h.releases]
        lines += [f"  {h.hold_id} release for {r['channel']} SUSPENDED: {r['problem']} [{r['release_id']}]" for r in h.suspended]
    channels = [c for c in CHANNELS if c != "agency"] + (["agency"] if agency else [])
    for channel in channels:
        result = authorize(store, policy, job_id, channel, agency if channel == "agency" else None, profile=profile)
        lines.append(f"submit via {channel}{' (' + agency + ')' if channel == 'agency' and agency else ''}: "
                     + ("allowed" if result.allowed else "not now: " + "; ".join(r["code"] for r in result.reasons)))
    return "\n".join(lines)
