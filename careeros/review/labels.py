"""Your labels (the acceptance data) and agreement metrics against the system's decisions.

PRIMARY and NIGHT are labelled and measured separately (prompts v2.2): a NIGHT label never counts toward PRIMARY
precision or false-reject rate, and vice versa.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import timedelta

from careeros.core.clock import parse_utc, require_order
from careeros.db.store import Store
from careeros.review.dismiss import dismissed_sql

LABELS = ("apply", "stretch", "skip")
LABEL_RULES = "labels-2026-11-24"   # the owner's D188: labels given before these rules are confirmed again
# (b, the red team's batch 3: a label carries the opening it was given for, checked at every use; the D190 design,
# 2026-11-24: an Apply or Stretch label is given from a preview of what it approves)
PROFILES = ("primary", "night")


def label_backlog(store: Store) -> dict | None:
    """Qualified postings that await your label (OPP-01, 2026-11-01): scored, latest tier A, B or near-miss, not a variant
    or duplicate of another posting, no PRIMARY label, not dismissed (the latest dismissal event decides; a reinstated
    posting waits again) and not closed (a closed or duplicate posting is no
    longer 'scored'). The one query behind the report header, `careeros status` and the daily notification, so the three
    always agree. None when nothing waits; else the count, the days the oldest has waited since CareerOS first saw it,
    and the line to show."""
    rows = store.all(f"""SELECT j.first_seen_at FROM jobs j JOIN scores s ON s.score_id =
                          (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state = 'scored' AND s.tier IN ('A','B','near_miss')
                          AND j.variant_of IS NULL AND j.duplicate_of IS NULL
                          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.job_id = j.job_id AND l.profile = 'primary')
                          AND NOT {dismissed_sql('j.job_id')}
                        ORDER BY j.first_seen_at""")
    if not rows:
        return None
    count = len(rows)
    oldest = max(0, int((parse_utc(store.clock.now()) - parse_utc(rows[0]["first_seen_at"])) / timedelta(days=1)))
    text = (f"{count} qualified posting{'s' if count != 1 else ''} await{'s' if count == 1 else ''} your label; "
            f"oldest {oldest} day{'s' if oldest != 1 else ''}")
    return {"count": count, "oldest_days": oldest, "text": text}


def opening_of(store: Store, job_id: str) -> dict | None:
    """The opening a job is now, as a label is checked against it (the red team's batch 3, B.1): the posting it is (its
    job_identity, else its own link), every requisition it states (its board's field and its text), its title, its
    location field and its employer. Its wording is not part of it: a board edits that all the time, and a label is
    for the opening, not for the words."""
    job = store.one("SELECT job_id, ats, external_id, url, requisition_id, title, location_raw, employer_name, "
                    "description_text FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        return None
    from careeros.discovery.board_discovery import job_identity
    from careeros.discovery.manual_intake import requisitions_in
    from careeros.discovery.seen_index import norm_company, norm_req
    from careeros.normalize.posting import norm
    identity = job_identity(job)
    stated = [r.strip() for r in (job["requisition_id"], *requisitions_in(job["description_text"])) if r and r.strip()]
    return {"posting": list(identity) if identity else ["link", job["url"] or ""],
            "requisitions": sorted({norm_req(r) for r in stated} - {"", None}),
            "requisitions_stated": sorted(set(stated)),   # as written, to say what changed; never compared
            "title": norm(job["title"] or ""), "location": " ".join((job["location_raw"] or "").split()),
            "employer": norm_company(job["employer_name"]) or ""}


def opening_digest(opening: dict | None) -> str:
    """A short digest of an opening (opening_of): what the review page's card carries, so a label clicked on a card
    built before the posting changed is refused (B.2)."""
    import json

    from careeros.core.fingerprint import text_fingerprint
    compared = {k: (opening or {}).get(k) for k, _ in _OPENING_PARTS} if opening is not None else None
    return text_fingerprint(json.dumps(compared, sort_keys=True))[:16]


_OPENING_PARTS = (("posting", "posting"), ("requisitions", "requisition"), ("title", "title"), ("location", "location"),
                  ("employer", "employer"))


def opening_change(given: dict, now: dict | None) -> str | None:
    """What differs between the opening a label was given for and the job now, or None."""
    if now is None:
        return "the job is gone"
    show = lambda v: (", ".join(v) if isinstance(v, list) else str(v)) or "none"   # noqa: E731
    shown = {"requisitions": "requisitions_stated"}
    parts = [f"{name}: {show(given.get(shown.get(key, key), given.get(key)))} -> {show(now.get(shown.get(key, key)))}"
             for key, name in _OPENING_PARTS if given.get(key) != now.get(key)]
    return "; ".join(parts) or None


def record_label(store: Store, job_id: str, label: str, reason: str | None, key: str, profile: str = "primary", *,
                 seen: str | None = None, preview: str | dict | None = None, policy=None, applicant=None) -> str:
    """Record your label. An Apply or Stretch label is given from a preview (the D190 design, section 3, "The label
    write"): `preview` is a preview's id (careeros label JOB apply prints one), or the capture the review page showed;
    the job is captured again in the label's own transaction, under `policy` and your `applicant` profile, and the
    label is recorded only when the digest is the preview's (PreviewRefused otherwise). Skip needs none. `seen`, from a
    caller that still sends it, is the digest of the opening its card showed (opening_digest): when the job changed
    since, the label is refused, not recorded (ValueError job_changed; the red team's gr1-routes-0)."""
    if label not in LABELS:
        raise ValueError("unknown_label")
    if profile not in PROFILES:
        raise ValueError("unknown_profile")
    job = store.one("SELECT job_id, current_state FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    writer = store.writer("review")
    return _record_label(store, writer, job_id, label, reason, key, profile, seen, job, preview=preview, policy=policy,
                         applicant=applicant)


def _confirmed_preview(store: Store, job_id: str, label: str, profile: str, preview, policy, applicant) -> dict | str:
    """The preview an Apply or Stretch label is recorded from, checked against a capture made now in the label's own
    transaction: {preview_id or None, digest, parts, page}; "replay" when this preview was already used for this same
    label (a retry grants nothing new). PreviewRefused otherwise."""
    if preview is None:
        raise PreviewRefused("preview_required: an Apply or Stretch label is given from a preview of what it approves: "
                             f"careeros label {job_id} {label} shows one, and --confirm ID records it (or label it on "
                             "the review page)")
    if isinstance(preview, str):
        row = store.one("SELECT * FROM label_previews WHERE preview_id = ?", (preview,))
        if row is None:
            raise PreviewRefused(f"preview_unknown: {preview} is not a preview CareerOS showed")
        if row["job_id"] != job_id or row["profile"] != profile:
            raise PreviewRefused(f"preview_other_job: {preview} is a preview of {row['job_id']} ({row['profile']})")
        if row["used_at"]:
            used = store.one("SELECT label_id, label FROM labels WHERE label_id = ?", (row["label_id"],))
            if used is not None and used["label"] == label:
                latest = store.one("SELECT label_id, label, at FROM labels WHERE job_id = ? AND profile = ? "
                                   "ORDER BY at DESC, rowid DESC LIMIT 1", (job_id, profile))
                if latest is not None and latest["label_id"] != used["label_id"]:
                    # a later label replaced the one this preview gave: it stands, and nothing is recorded (BATCH_5 F1)
                    raise PreviewRefused(f"preview_used: {preview} gave an earlier {label} label; your later "
                                         f"{latest['label']} label ({latest['at'][:16]} UTC) stands. To label it {label} "
                                         f"again, careeros label {job_id} {label} shows a fresh preview")
                return "replay"
            raise PreviewRefused(f"preview_used: {preview} was already used for another label")
        given = {"preview_id": row["preview_id"], "digest": row["digest"], "page": False}
    else:
        if preview.get("job_id") != job_id or preview.get("profile") != profile or not preview.get("digest"):
            raise PreviewRefused("preview_other_job: the card's preview is not this job's")
        given = {"preview_id": None, "digest": preview["digest"], "page": True}
    fresh = capture(store, policy, applicant, job_id, profile)
    if fresh["waiting"]:
        raise PreviewRefused(f"no preview: {fresh['waiting']}")
    if fresh["digest"] != given["digest"]:
        raise PreviewRefused("job_changed: this job is no longer what the preview showed (its posting, the values read "
                             "from it, its screening, a copy of it or a failed read); check the fresh preview, then "
                             "label it from that")
    return {**given, "parts": fresh["parts"]}


def _record_label(store: Store, writer, job_id: str, label: str, reason: str | None, key: str, profile: str,
                  seen: str | None, job, *, preview=None, policy=None, applicant=None) -> str:
    import json
    with store.tx():
        payload = {"job_id": job_id, "label": label, "reason": reason or "", "profile": profile}
        if writer.receipt("label", key, payload if profile != "primary" else {k: v for k, v in payload.items() if k != "profile"},
                          actor_id="user") == "replay":
            return "replay"
        if profile == "night":
            night = store.one("SELECT tier, decision FROM night_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
            system_tier, system_decision = (night["tier"], night["decision"]) if night else (None, None)
        else:
            score = store.one("SELECT tier FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
            system_tier, system_decision = (score["tier"] if score else None), job["current_state"]
        now = store.clock.now()
        before = store.one("SELECT at FROM labels WHERE job_id = ? AND profile = ? ORDER BY at DESC, rowid DESC LIMIT 1",
                           (job_id, profile))
        prior = before["at"] if before else None
        try:
            # the label you give last is the one in force: one stamped before the job's latest label (a clock set back
            # since) would leave the older label the latest, so it is refused, as a dismissal is (the red team before
            # Codex Pass 15, RT-03)
            require_order(prior, now)
        except ValueError:
            raise ValueError(f"out_of_order_label: this computer's clock ({now[:16]} UTC) is before the job's latest label "
                             f"({(prior or '')[:16]} UTC); correct the clock, then label it again") from None
        opening = opening_of(store, job_id)
        if seen is not None and seen != opening_digest(opening):
            raise ValueError("job_changed: this job changed after the page was built (its posting, requisition, title, "
                             "location or employer); reload the page, check it is the opening you mean, then label it")
        given = _confirmed_preview(store, job_id, label, profile, preview, policy, applicant) \
            if label in ("apply", "stretch") else None
        if given == "replay":
            return "replay"
        label_id = f"label:{uuid.uuid4().hex}"
        preview_id = None
        if isinstance(given, dict):
            preview_id = given["preview_id"] or f"preview:{uuid.uuid4().hex}"
            if given["page"]:   # the page's preview lived in its server's memory: written now, with the label
                writer.insert("label_previews", {
                    "preview_id": preview_id, "job_id": job_id, "profile": profile, "digest": given["digest"],
                    "parts_json": json.dumps(given["parts"], sort_keys=True, ensure_ascii=False),
                    "holds_json": json.dumps(given["parts"]["holds"]), "created_at": now})
        sightings = store.one("SELECT COALESCE(MAX(rowid), 0) AS n FROM observations")
        # how many changes the posting had when you labelled it: a change of its identity recorded later suspends the
        # label (identity_change_since; the red team before Codex Pass 15, RT-04 and RT-05)
        counted = store.one("SELECT COALESCE(MAX(rowid), 0) AS n FROM job_changes WHERE job_id = ?", (job_id,))
        counted_seen = counted["n"] if counted else 0
        writer.insert("labels", {"label_id": label_id, "job_id": job_id, "at": now, "label": label,
                                 "reason": (reason or "").strip()[:300] or None, "system_tier": system_tier,
                                 "system_decision": system_decision, "profile": profile, "preview_id": preview_id})
        if preview_id is not None:
            # the preview is used once, by this label; the holds it showed are acknowledged by this label only (H25)
            writer.update("label_previews", {"used_at": now, "label_id": label_id}, "preview_id = ?", (preview_id,))
        writer.event("job_labelled", outcome="informational", entity_type="job", entity_id=job_id, actor_type="human",
                     actor_id="user", metadata={"label": label, "system_state": system_decision, "profile": profile,
                                                "label_id": label_id, "changes_seen": counted_seen,
                                                "label_rules": LABEL_RULES, "opening": opening,
                                                "preview_id": preview_id,
                                                "digest": given["digest"] if isinstance(given, dict) else None,
                                                "sightings_seen": sightings["n"] if sightings else 0})
    return "new"


def _label_event(store: Store, label) -> dict | None:
    import json
    row = store.one("SELECT seq, metadata_json FROM events WHERE event_type = 'job_labelled' AND entity_id = ? "
                    "AND json_extract(metadata_json, '$.label_id') = ? ORDER BY seq DESC LIMIT 1",
                    (label["job_id"], label["label_id"]))
    return {"seq": row["seq"], **json.loads(row["metadata_json"])} if row else None


def label_opening(store: Store, label) -> dict | None:
    """The opening a label was given for (opening_of, recorded with it), or None for a label given before 2026-11-19b."""
    event = _label_event(store, label)
    opening = (event or {}).get("opening")
    return opening if isinstance(opening, dict) else None


def label_confirmed(store: Store, label) -> bool:
    """Whether a labels row was given under the label rules of the owner's D188 (2026-11-19, "Re-confirm after
    upgrade"): an Apply or Stretch label recorded before them, by this engine or an older one, is suspended until you
    give it again on the review page (or with careeros label). Once: a label you give now carries the rules it was given
    under. An older engine cannot write a database this one upgraded (schema 16 refuses it, the owner's D191): a
    rollback is a restore of the backup made before the upgrade."""
    event = _label_event(store, label)
    if event is None or event.get("label_rules") != LABEL_RULES or not isinstance(event.get("opening"), dict):
        return False
    # an Apply or Stretch label is given from a preview of what it approves (the D190 design, section 3)
    return label["label"] not in ("apply", "stretch") or _given(store, label) is not None


# --- the preview (the D190 design, revision 8, section 3) ----------------------------------------------------------
# An Apply or Stretch label approves the posting as its card showed it, in four parts: (a) the job's source record,
# (b) the derived values the card shows, (c) the screening shown, (d) the identity group with its sightings not yet
# acknowledged and the open holds that reach the job. capture() reads them in one transaction; a label is recorded only
# from a preview of them whose digest is still the job's (the label write captures again in its own transaction), and
# P1 (packages.route_for) compares the job with what the label's preview showed at every use.
FACTS_READER = "facts-2026-11-24"
SHOWN_DERIVED = ("comp_min", "comp_max", "comp_period", "comp_currency", "remote_type", "remote_states_json",
                 "remote_excluded_states_json", "deadline_at", "remote_scope", "timezone_requirement", "employment_type")
# the columns the card shows as read: each must be what the one derivation makes of the source record, or the card would
# show something the record does not say (then the job waits for the processing run to write them again)
SHOWN_AS_READ = ("title", "location_raw", "url", "apply_url", "employer_name", "requisition_id", "posted_at",
                 "description_text")


class PreviewRefused(ValueError):
    """An Apply or Stretch label refused for its preview: none given, an unknown one, another job's or profile's, one
    used for another label, or one that no longer shows what the job is. Nothing is recorded."""


def identity_group(store: Store, job_id: str) -> list[str]:
    """The job and every row that is the same posting: its original, the original's duplicates, and every job with its
    identity, a twin before or after it is joined (the members assist.job_group had, the red team's batch 3, condition
    2; the D190 design, part d)."""
    from careeros.discovery.board_discovery import job_identity, jobs_with_identity
    row = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        return [job_id]
    ids = {job_id, *([row["duplicate_of"]] if row["duplicate_of"] else [])}
    ids |= {r["job_id"] for r in jobs_with_identity(store, job_identity(row))}
    marks = ",".join("?" * len(ids))
    ids |= {r["job_id"] for r in store.all(f"SELECT job_id FROM jobs WHERE duplicate_of IN ({marks})", tuple(ids))}
    return sorted(ids)


def screening_current(job, gate, policy, profile) -> bool:
    """Whether the latest gate result judged the job as it is now (the D190 design, part c): the job is screened (not
    sent back to be screened again), and the result was made for its source record, its stored requirement extraction,
    today's rules and your profile. Whatever its decision."""
    from types import SimpleNamespace

    from careeros.pipeline import decision_fingerprint, source_digest
    from careeros.review.packages import SCREENED
    if gate is None or policy is None or profile is None or job["current_state"] not in SCREENED:
        return False
    return (gate["source_digest"] is not None and gate["source_digest"] == source_digest(job["source_json"])
            and gate["extraction_fingerprint"] == (job["extraction_fingerprint"] or "unread")
            and gate["policy_fingerprint"] == decision_fingerprint(SimpleNamespace(policy=policy))   # type: ignore[arg-type]
            and gate["profile_fingerprint"] == profile.fingerprint)


def sighting_differs(record: dict, facts: dict) -> bool:
    """Whether a sighting says something the record does not, by the rule of "Unchanged reads" (pipeline.same_read): a
    board's read counts every input as stated, a copy only what it found. A sighting stored without its source record
    fails closed."""
    from careeros.pipeline import same_read
    read = facts.get("source")
    if not isinstance(read, dict):
        return True
    return not same_read(record, read, board=read.get("ats") not in (None, "manual"))


def _given(store: Store, label) -> dict | None:
    """What a label was given from: its preview's parts and acknowledged holds, and how far the sightings went when it
    was recorded. None for a label given without a preview."""
    import json
    if label is None or "preview_id" not in label.keys() or not label["preview_id"]:
        return None
    preview = store.one("SELECT * FROM label_previews WHERE preview_id = ? AND label_id = ?",
                        (label["preview_id"], label["label_id"]))
    if preview is None:
        return None
    event = _label_event(store, label) or {}
    return {"parts": json.loads(preview["parts_json"]), "holds": json.loads(preview["holds_json"] or "[]"),
            "digest": preview["digest"], "sightings_seen": int(event.get("sightings_seen") or 0)}


def unacknowledged_sightings(store: Store, record: dict, members: list[str], given: dict | None) -> list[dict]:
    """The sightings of the group's members that differ from `record` and that the label `given` did not see: one of a
    member that was in its group recorded after it, and one of a member that was not in its group, whenever recorded
    (Codex M2: a member joined later brings its older sightings). With no label, every differing sighting."""
    import json
    if not members:
        return []
    old = set((given or {}).get("parts", {}).get("group", {}).get("members", [])) if given else set()
    seen = given["sightings_seen"] if given else 0
    marks = ",".join("?" * len(members))
    out = []
    for row in store.all(f"SELECT rowid AS n, observation_id, job_id, facts_json FROM observations WHERE job_id IN ({marks}) "
                         "AND json_extract(facts_json, '$.sighting') = 1 ORDER BY rowid", tuple(members)):
        if given and row["job_id"] in old and row["n"] <= seen:
            continue
        facts = json.loads(row["facts_json"] or "{}")
        if sighting_differs(record, facts):
            out.append({"observation_id": row["observation_id"], "job_id": row["job_id"],
                        "source_digest": facts.get("source_digest")})
    return out


def _member(store: Store, sighting: dict) -> str:
    """Whose copy a sighting is: the joined twin's id, else the job it was read for (BATCH_5 B3)."""
    import json
    row = store.one("SELECT facts_json FROM observations WHERE observation_id = ?", (sighting["observation_id"],))
    return (json.loads(row["facts_json"] or "{}") if row is not None else {}).get("joined_twin") or sighting["job_id"]


def latest_given(store: Store, job_id: str, profile: str):
    """The latest label of the job and profile that was given from a preview, or None."""
    return store.one("SELECT * FROM labels WHERE job_id = ? AND profile = ? AND preview_id IS NOT NULL "
                     "ORDER BY rowid DESC LIMIT 1", (job_id, profile))


def _waiting(store: Store, policy, profile, job) -> str | None:
    """Why the job offers no preview now, or None (the D190 design, section 3, "The label write")."""
    import json

    from careeros.pipeline import Context, board_written, derive
    if not job["source_json"]:
        from careeros.upgrade import board_forbidden
        if board_written(store, job["job_id"]) and board_forbidden(store, job):   # BATCH_5 G2: never "add-url" here
            return ("waiting for the processing run: its board may not be read now (site policy), so its record is "
                    "rebuilt from its stored columns at the next processing run")
        if board_written(store, job["job_id"]):
            # W3: add-url also marks its board verified, for a board you paused or disabled
            return ("waiting for a board read: its board's own record is read at the next scan, or now with careeros "
                    "add-url and its link (which also marks its board verified again, if you paused or disabled it)")
        return "waiting for the upgrade: its source record is written when CareerOS is upgraded"
    gate = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job["job_id"],))
    if not screening_current(job, gate, policy, profile):
        return "waiting for screening: the next processing run screens it as it is now"
    ctx = Context(store, policy, profile, None)   # type: ignore[arg-type]
    derived = derive(ctx, json.loads(job["source_json"]), job["job_id"])
    if any(derived.get(k) != job[k] for k in (*SHOWN_DERIVED, *SHOWN_AS_READ)):
        return "waiting for screening: its values are read again at the next processing run"
    return None


def capture(store: Store, policy, profile, job_id: str, who: str = "primary") -> dict:
    """What an Apply or Stretch label on the job would approve now (the D190 design, section 3, "The preview"), read in
    one transaction: {job_id, profile, waiting, parts, digest}. `profile` is your applicant profile, `who` the label's
    profile (primary or night). `waiting` says why there is no preview (no source record yet, or not screened as it is
    now); then parts and digest are None. The digest covers the job, the label's profile and the parts, never the
    readers' versions."""
    import json

    from careeros.core.fingerprint import fingerprint
    from careeros.review.packages import open_holds, pending_file_problem, pending_snapshot
    if who not in PROFILES:
        raise ValueError("unknown_profile")
    with store.read():
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if job is None:
            raise ValueError("unknown_job")
        out: dict = {"job_id": job_id, "profile": who, "facts_reader": FACTS_READER, "waiting": None, "parts": None,
               "digest": None, "holds_shown": []}
        waiting = _waiting(store, policy, profile, job)
        if waiting:
            return {**out, "waiting": waiting}
        # one reading of the holds kept beside the database: while it cannot all be read, which holds reach the job is
        # not known, so no preview is offered (every label waits on it anyway: p1_held); else the holds shown are
        # that reading's (Codex H40)
        snapshot = pending_snapshot(store)
        problem = pending_file_problem(store, snapshot)
        if problem:
            return {**out, "waiting": problem}
        record = json.loads(job["source_json"])
        gate = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
        if gate is None:   # never after _waiting: a current screening has a gate result
            return {**out, "waiting": "waiting for screening"}
        members = identity_group(store, job_id)
        holds = sorted(open_holds(store, job_id, snapshot), key=lambda h: h["hold_id"])
        from careeros.pipeline import Context
        ctx = Context(store, policy, profile, None)   # type: ignore[arg-type]
        derived = {k: job[k] for k in SHOWN_DERIVED}
        parts = {
            "source": job["source_json"],
            "derived": derived,
            "screening": {"decision": gate["decision"], "hard_failures": json.loads(gate["hard_failures_json"] or "[]"),
                          "flags": json.loads(gate["flags_json"] or "[]")},
            "group": {"members": members,
                      # each differing copy as the card shows it: the digest binds its values (BATCH_5 B3 and C)
                      "sightings": [_copy_of(ctx, job_id, record, derived, s) for s in unacknowledged_sightings(
                          store, record, members, _given(store, latest_given(store, job_id, who)))]},
            "holds": [h["hold_id"] for h in holds],
        }
        return {**out, "parts": parts, "digest": fingerprint({"job_id": job_id, "profile": who, "parts": parts}),
                "holds_shown": holds}


def preview_lines(captured: dict, store: Store | None = None, now: str | None = None) -> list[str]:
    """The card a preview shows in the CLI: what the label approves (approves: card_items, the posting's text printed
    whole), the items the review page's card renders too (BATCH_5 C; Codex H37)."""
    if captured["waiting"]:
        return [f"No preview for {captured['job_id']}: {captured['waiting']}."]
    return [("  " + line).rstrip() if n else line for n, line in enumerate(approves(captured, store, now))]


# where a source record's location came from, as the card says it (the readers' location_source)
LOCATION_FROM = {"argument": "you gave it", "label": "its own Location line", "file": "the saved page's location field",
                 "none": "not stated", "unknown": "unknown (stored before the upgrade)"}


def _amount(value) -> str:
    """A pay amount exactly as stored: thousands marked, never rounded (Codex H37: 24.125 was shown as 24.12)."""
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,}"


def _listed(value) -> str:
    import json
    items = json.loads(value) if isinstance(value, str) and value.startswith("[") else value
    return ", ".join(str(i) for i in items) if items else ""


def stored_pay(comp) -> str:
    """What a record stores as pay where CareerOS reads none from it (the owner's D200, Codex H42): every stored value,
    named, and why it reads as none; "" when it stores none. CareerOS reads a stored 0-0 as "pay not stated" (screening
    and digests keep that reading), and every line that says so says what is stored beside it."""
    from careeros.pipeline import _blank
    if isinstance(comp, dict):
        if all(_blank(v) is None for v in comp.values()):
            return ""
        amounts = [comp.get("min"), comp.get("max")]
        zero = [isinstance(a, (int, float)) and not isinstance(a, bool) and a == 0 for a in amounts]
        why = ("CareerOS reads 0–0 as no pay given" if all(zero) else
               "CareerOS reads a minimum and maximum of 0 or none as no pay given"
               if all(z or _blank(a) is None for z, a in zip(zero, amounts, strict=True)) else "CareerOS reads no pay from it")
        return f"stored: {_named(comp, _PAY_NAMES)}; {why}"
    return "" if _blank(comp) is None else f"stored: {_stated(comp)}; CareerOS reads no pay from it"


def pay_line(derived: dict, record: dict | None = None) -> str:
    """Every pay field the digest binds, as stated: minimum, maximum, period and currency, each "not stated" when it is
    not, never a range made from one end (BATCH_5 C1), each amount exactly as stored (Codex H37); what a reader filled
    in is named (section 3). Pay read as not stated says what the record stores (stored_pay: the owner's D200)."""
    lo, hi, per, cur = derived.get("comp_min"), derived.get("comp_max"), derived.get("comp_period"), derived.get("comp_currency")
    if lo is None and hi is None:
        stored = stored_pay((record or {}).get("comp"))
        return "pay: not stated" + (f" ({stored})" if stored else "")
    comp = (record or {}).get("comp") if isinstance((record or {}).get("comp"), dict) else None
    stated = (comp or {}).get("period_stated")
    bits = [f"minimum {_amount(lo)}" if lo is not None else "no minimum stated",
            f"maximum {_amount(hi)}" if hi is not None else "no maximum stated",
            # a unit the page states that CareerOS does not understand: said as stated, never "not stated" (BATCH_5 D1)
            (f'period stated "{stated}", not understood; read as per {per}' if stated else f"per {per}") if per
            else "period not stated", cur or "currency not stated"]
    filled = (comp or {}).get("inferred") or []
    return "pay: " + " · ".join(bits) + (f" (filled in by CareerOS, not stated by the posting: {', '.join(filled)})"
                                         if filled else "")


def card_facts(record: dict, d: dict, now: str | None = None) -> list[tuple[str, str]]:
    """The facts a card states about one read, as (name, value): what its source record says and what the one
    derivation reads from it. Each differing copy's values are compared with the job of record's by these (BATCH_5 B3,
    C); the card shows every key of the record too (record_items)."""
    employer = record.get("employer") or {}
    who = employer.get("name") or "employer not stated"
    if employer.get("parent") and employer.get("parent") != employer.get("name"):
        who += f" (part of {employer['parent']})"
    source = record.get("location_source")
    deadline = d.get("deadline_at")
    return [("title", record.get("title") or "title not stated"),
            ("employer", who),
            ("posting link", record.get("url") or "none"),
            ("apply link", record.get("apply_url") or "none"),
            ("requisition", record.get("requisition_id") or "none stated"),
            ("posted", record.get("posted_at") or "not stated"),
            ("location", f"{record.get('location') or 'not stated'} (from: "
                         f"{LOCATION_FROM.get(source, source) if source else 'the board' if record.get('ats') not in (None, 'manual') else 'not recorded'})"),
            ("remote", f"{d.get('remote_type') or 'not stated'} · scope: {d.get('remote_scope') or 'not stated'}"),
            ("states it hires in", _listed(d.get("remote_states_json")) or "none named"),
            ("states excluded", _listed(d.get("remote_excluded_states_json")) or "none named"),
            ("time zone", d.get("timezone_requirement") or "none stated"),
            ("deadline", (deadline or "none stated") + (" (already past)" if deadline and now and deadline[:10] < now[:10] else "")),
            ("employment type", d.get("employment_type") or "not stated"),
            ("pay", pay_line(d, record)[len("pay: "):])]


# the card facts that are the posting as read (record_items names each key of the record); the rest are what CareerOS
# reads from it
_READ_FACTS = ("title", "employer", "posting link", "apply link", "requisition", "posted", "location")
# the record keys a card fact shows exactly as stored, a text or nothing (Codex H42): for a copy, such a key whose fact
# differs is left to that fact's line. Every other key is shown with both values whole, the pay and the employer entry
# leaf by leaf, since their facts are readings of them (pay read as not stated; the entry's name and parent only), and
# where the location came from too (its line names the source in words)
_FACT_OF = {"title": "title", "url": "posting link", "apply_url": "apply link", "requisition_id": "requisition",
            "posted_at": "posted", "location": "location"}
# each key of a source record as the card names it, in this order (Codex H37): a key not named here is still shown, by
# its own name, so a new field is never left out
RECORD_NAMES = (("title", "title"), ("employer", "employer entry"), ("ats", "read from"), ("board_token", "board"),
                ("external_id", "posting id"), ("url", "posting link"), ("apply_url", "apply link"),
                ("requisition_id", "requisition"), ("posted_at", "posted"), ("location", "location"),
                ("location_source", "location from"), ("workplace_hint", "workplace, as stated"),
                ("employment_type_hint", "employment type, as stated"), ("department", "department"),
                ("typed_countries", "countries its board types"), ("comp", "pay, as stored"), ("reader", "read by"),
                ("inferred", "read by CareerOS, not stated as such by the posting"), ("cut", "cut to length"),
                ("bindings", "kept from an earlier read; this read sent"),
                ("unknown", "not known (rebuilt from what was stored before the upgrade)"),
                ("description_text", "the posting's text"))
_PAY_NAMES = (("min", "minimum"), ("max", "maximum"), ("period", "period"), ("period_stated", "period as stated"),
              ("currency", "currency"), ("inferred", "filled in by CareerOS, not stated by the posting"))
_ENTRY_NAMES = (("name", "name"), ("parent", "part of"), ("employer_id", "employer id"), ("resolved", "resolved by"))


def _stated(value) -> str:
    """A value of a source record as the card shows it: whole, a number exactly as stored."""
    import json
    if value is None or value == "":
        return "not stated"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return _amount(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
                         for v in value) or "none"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _named(value, names) -> str:
    """A dict of a record (its pay, its employer entry, what it kept) with each key named, in order, then any other."""
    if not isinstance(value, dict):
        return _stated(value)
    known = [k for k, _ in names]
    return " · ".join([f"{name} {_stated(value.get(key))}" for key, name in names if key in value]
                      + [f"{key} {_stated(value[key])}" for key in sorted(value) if key not in known]) or "none"


def record_items(record: dict) -> list[dict]:
    """Every key of a source record, as card items (Codex H37): the posting as read, whole. Its text is a fold the
    review page opens and the CLI prints whole."""
    shown = {key for key, _ in RECORD_NAMES}
    items = []
    for key, name in [*RECORD_NAMES, *((k, k) for k in sorted(record) if k not in shown)]:
        if key not in record or (key == "location_source" and "location" in record):
            continue   # where the location came from is said on its line
        value = record[key]
        if key == "location":   # with where it came from (a board's own field when no reader recorded one)
            items.append({"text": f"location: {dict(card_facts(record, {}))['location']}"})
            continue
        if key == "description_text":
            text = value or ""
            items.append({"text": f"{name}, in full ({len(text):,} characters)" if text else f"{name}: none",
                          **({"fold": text} if text else {})})
            continue
        if key == "comp":
            said = _named(value, _PAY_NAMES) if value else "none"
        elif key == "employer":
            said = _named(value, _ENTRY_NAMES)
        elif key == "bindings" and isinstance(value, dict):
            said = "; ".join(f"{k}: {_stated(v)}" for k, v in sorted(value.items())) or "none"
        else:
            said = _stated(value)
        items.append({"text": f"{name}: {said}"})
    return items


def _screened(entry: dict, what: str) -> str:
    """A gate result's failure or flag, every value it holds."""
    extra = "; ".join(f"{k}: {_stated(v)}" for k, v in sorted(entry.items()) if k != "detail" and v not in (None, ""))
    return f"{what}: {entry.get('detail')}" + (f" ({extra})" if extra else "")


# what kind of read a copy was, as the card says it
COPY_KIND = {"human_text": "pasted", "manual_import": "saved or added", "ats_scan": "a board's read",
             "alert": "an alert's card", "recommendation": "a recommendation"}


def _copy_of(ctx, job_id: str, record: dict, d: dict, sighting: dict) -> dict:
    """A differing sighting as the card shows it, made in the capture so the digest binds what the card shows (BATCH_5
    B3 and C): whose copy it is (the joined twin's id, or the job it was read for), what kind of read and when, each card
    fact that differs with both values, every other input that differs with both values whole, and its text whole
    with the sentences each side states that the other does not (Codex H37: nothing cut)."""
    import json

    from careeros.pipeline import _blank, derive, dropped_marks
    row = ctx.store.one("SELECT source_kind, observed_at, facts_json FROM observations WHERE observation_id = ?",
                        (sighting["observation_id"],))
    facts = json.loads(row["facts_json"] or "{}") if row is not None else {}
    source = facts.get("source")
    copy: dict = source if isinstance(source, dict) else {}
    out = {**sighting, "member": facts.get("joined_twin") or sighting["job_id"],
           "kind": COPY_KIND.get(row["source_kind"], row["source_kind"]) if row is not None else "unknown",
           "at": (row["observed_at"] if row is not None else "")[:10], "differs": [], "other": []}
    if not copy:
        out["other"] = [["its source record", "not recorded", "recorded"]]
        return out
    try:
        theirs_d = {k: v for k, v in derive(ctx, copy, job_id).items() if k in d}
    except Exception:   # noqa: BLE001 - a copy whose values cannot be read: its inputs are shown instead
        theirs_d = None
    if theirs_d is not None:
        own = dict(card_facts(record, d))
        for name, value in card_facts(copy, theirs_d):
            if value != own.get(name):
                out["differs"].append([name, value, own.get(name)])
    covered = {name for name, _theirs, _mine in out["differs"]}
    board = copy.get("ats") not in (None, "manual")   # a board's read states every input; a copy only what it found
    # a mark the copy drops on a field it found: it states that field (Codex Pass 18, D203), so its marks are shown
    dropped = {} if board else dropped_marks(record, copy)
    names = dict(RECORD_NAMES)
    for key in sorted((set(copy) | set(record)) - {"bindings"}):
        theirs, mine = copy.get(key), record.get(key)
        if _FACT_OF.get(key) in covered or (not board and _blank(theirs) is None and key not in dropped) \
                or _blank(theirs) == _blank(mine):
            continue
        if key in dropped:
            out["other"].append([names[key], _stated(theirs or []), _stated(mine)])
        elif key == "description_text":
            out["text"] = {"only_here": _text_differs(theirs or "", mine or ""),
                           "only_there": _text_differs(mine or "", theirs or ""), "whole": theirs or ""}
        elif key == "employer" and isinstance(theirs, dict) and isinstance(mine, dict):
            # every leaf of the entry that differs, whatever its name and parent say: the copy's entry is CareerOS's
            # resolution of it, so each key it has is stated, a null one too (Codex H42: a changed name hid the id)
            known = [sub for sub, _ in _ENTRY_NAMES]
            entry = dict(_ENTRY_NAMES)
            for sub in [*known, *sorted((set(theirs) | set(mine)) - set(known))]:
                if (board or sub in theirs) and _blank(theirs.get(sub)) != _blank(mine.get(sub)):
                    out["other"].append([f"employer {entry.get(sub, sub)}", _stated(theirs.get(sub)), _stated(mine.get(sub))])
        elif key == "comp":
            out["other"].append([names[key], _named(theirs, _PAY_NAMES) if theirs else "none",
                                 _named(mine, _PAY_NAMES) if mine else "none"])
        else:
            out["other"].append([names.get(key, key), _stated(theirs), _stated(mine)])
    return out


def _text_differs(theirs: str, mine: str) -> str:
    """The sentences and lines of a copy's text that the job of record's text does not have, in order."""
    import re

    def pieces(text):
        return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p.strip()]
    own = set(pieces(mine))
    return " ".join(p for p in pieces(theirs) if p not in own) or "the same sentences, arranged otherwise"


def card_items(captured: dict, store: Store | None = None, now: str | None = None) -> list[dict]:
    """What an Apply or Stretch label on the job approves: every value its preview's digest binds, as items ({"text"},
    and a "fold" for a text shown whole). The one renderer of every surface that offers a label (the review page's card
    and the CLI preview; BATCH_5 C; Codex H37): the posting as read, every key of its source record (record_items: its
    text whole, every source field, the pay exactly as stored, the employer entry and how it was resolved, the reader
    and its marks); what CareerOS reads from it (remote type and scope, the states, the time zone, the deadline, the
    employment type, the pay); the screening with every failure and flag; every member of the posting's group and each
    differing copy, every value it states otherwise, whole; and the holds."""
    import json
    parts = captured["parts"]
    record = json.loads(parts["source"])
    d = parts["derived"]
    facts = dict(card_facts(record, d, now))
    items = [{"text": f"{facts['title']} at {facts['employer']} ({captured['job_id']})"},
             {"text": "the posting as read (its source record, every field):"}]
    items += [{**item, "text": "  " + item["text"]} for item in record_items(record)]
    items.append({"text": "what CareerOS reads from it:"})
    items += [{"text": f"  {name}: {value}"} for name, value in card_facts(record, d, now) if name not in _READ_FACTS]
    items.append({"text": f"screening: {parts['screening']['decision']}"})
    items += [{"text": _screened(f, "fails")} for f in parts["screening"]["hard_failures"]]
    items += [{"text": _screened(f, "flag")} for f in parts["screening"]["flags"]]   # every one (BATCH_5 C4)
    others = [m for m in parts["group"]["members"] if m != captured["job_id"]]
    if others:
        items.append({"text": f"the same posting: {', '.join(others)}"})
    for s in parts["group"]["sightings"]:
        if "differs" not in s:   # a capture made before a copy's values were part of it
            whose, says = _copy_says(store, s, record)
            items.append({"text": f"a copy of this posting says something else ({whose}): {says}"})
            continue
        # whose copy, and each value it states otherwise beside the job of record's (BATCH_5 B3 and C), whole (H37)
        whose = (f"{s['member']}, another job of this posting" if s["member"] != captured["job_id"]
                 else "a copy read for this job")
        items.append({"text": f"a copy of this posting says something else ({whose}; {s['kind']}, {s['at']}):"})
        items += [{"text": f"  {name}: {theirs} (this copy) / {mine} (the job of record)"}
                  for name, theirs, mine in s["differs"]]
        items += [{"text": f"  also: {other}"} if isinstance(other, str) else
                  {"text": f"  also: {other[0]}: {other[1]} (this copy) / {other[2]} (the job of record)"}
                  for other in s["other"]]
        text = s.get("text")
        if text:
            items.append({"text": f"  also: its text, where it differs: {text['only_here']}"})
            items.append({"text": f"  also: the job of record's text states, where this copy's does not: {text['only_there']}"})
            items.append({"text": f"  this copy's text, in full ({len(text['whole']):,} characters)", "fold": text["whole"]})
        if not s["differs"] and not s["other"] and not text:
            items.append({"text": "  no stated value differs"})
    if record.get("unknown"):
        items.append({"text": rebuilt_note(record)})
    for h in captured["holds_shown"]:
        items.append({"text": f"a later read failed ({h['at'][:16]} UTC): {h['message']}; CareerOS may not have the "
                              "posting's latest terms (a board read clears it; your label acknowledges it)"})
    return items


def approves(captured: dict, store: Store | None = None, now: str | None = None) -> list[str]:
    """The card's items (card_items) as lines, for the CLI: a fold's text printed whole beneath its line, each of its
    lines indented."""
    lines = []
    for item in card_items(captured, store, now):
        lines.append(item["text"])
        if "fold" in item:
            lines += [("    " + line).rstrip() for line in item["fold"].split("\n")]
    return lines


def _copy_says(store: Store | None, sighting: dict, record: dict) -> tuple[str, str]:
    """(whose copy it is: the joined twin's id, or the job it was read for; what it states where it differs from the
    record, each value whole)."""
    import json
    row = store.one("SELECT facts_json FROM observations WHERE observation_id = ?", (sighting["observation_id"],)) \
        if store is not None else None
    if row is None:
        return sighting["job_id"], f"observation {sighting['observation_id']}"
    facts = json.loads(row["facts_json"] or "{}") or {}
    copy = facts.get("source") or {}
    said = []
    for key in sorted((set(copy) | set(record)) - {"bindings", "employer", "unknown"}):
        if copy.get(key) not in (None, "", [], {}) and copy.get(key) != record.get(key):
            said.append(f"{key} {_stated(copy[key])}")
    mine, theirs = record.get("employer") or {}, copy.get("employer") or {}
    for key in ("name", "parent"):
        if theirs.get(key) and theirs.get(key) != mine.get(key):
            said.append(f"employer {key} {theirs[key]}")
    return facts.get("joined_twin") or sighting["job_id"], "; ".join(said) or "no stated value differs"


def _record_moves(given: dict, now: dict) -> list[str]:
    """The inputs of a source record that differ, named (for the held label's reason)."""
    keys = sorted((set(given) | set(now)) - {"bindings"})
    moved = [k for k in keys if given.get(k) != now.get(k)]
    if given.get("bindings") != now.get("bindings"):
        moved.append("what a kept field is compared with")
    return moved


def p1_held(store: Store, policy, profile, job_id: str, label) -> str | None:
    """Why P1 holds an Apply or Stretch label now, or None (the D190 design, section 3, "P1 at use"): the label routes
    only while (a) the job's source record is byte for byte the one its preview showed, (b) the values derived again
    from it under the current readers, and the job's stored ones, are those shown, (c) the latest gate result is current
    and its decision and flags are those shown, (d) no sighting differs that the label did not see (one of a member of
    its group recorded after it, or of a member that joined since, whenever recorded), and every open hold that reaches
    the job is one the preview showed (Codex H25: the label acknowledges those, for itself only). The caller's
    transaction (route_for reads in one)."""
    import json

    from careeros.pipeline import Context, derive
    from careeros.review.packages import open_holds, pending_file_problem, pending_snapshot
    # one reading of the holds kept beside the database for the whole decision: its health and the holds it keeps come
    # from that one reading, never from two (Codex H40)
    snapshot = pending_snapshot(store)
    problem = pending_file_problem(store, snapshot)
    if problem:   # a hold kept beside the database may reach this job: every label waits (fails closed; BATCH_5 A)
        return f"{problem}. Every Apply and Stretch label waits until then"
    given = _given(store, label)
    if given is None:
        return ("label_unconfirmed: this label was not given from a preview of what it approves; label the job again "
                "from its card or with careeros label JOB apply")
    parts = given["parts"]
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    record = json.loads(parts["source"])
    moved = []
    if job["source_json"] != parts["source"]:   # (a)
        now = json.loads(job["source_json"]) if job["source_json"] else {}
        moved.append("the posting as read (" + (", ".join(_record_moves(record, now)) or "its record") + ")")
    elif profile is None:
        moved.append("no applicant profile was given, so its values and screening cannot be shown current")
    else:
        derived = derive(Context(store, policy, profile, None), record, job_id)   # type: ignore[arg-type]   # (b)
        changed = sorted(k for k in SHOWN_DERIVED if derived.get(k) != parts["derived"][k] or job[k] != parts["derived"][k])
        if changed:
            moved.append("the values read from it (" + ", ".join(changed) + ")")
        gate = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
        if gate is None or not screening_current(job, gate, policy, profile):   # (c)
            moved.append("its screening, which waits for the next processing run")
        elif {"decision": gate["decision"], "hard_failures": json.loads(gate["hard_failures_json"] or "[]"),
              "flags": json.loads(gate["flags_json"] or "[]")} != parts["screening"]:
            moved.append(f"its screening (now {gate['decision']})")
    sightings = unacknowledged_sightings(store, record, identity_group(store, job_id), given)   # (d)
    if sightings:
        moved.append("a copy of this posting says something else (" + ", ".join(sorted({_member(store, s) for s in sightings})) + ")")
    if moved:
        return (f"label_facts_changed: after your {label['label']} label, " + "; ".join(moved) + ". Check the posting, then "
                "label it again from a fresh preview")
    acknowledged = set(given["holds"])
    for hold in open_holds(store, job_id, snapshot):
        if hold["hold_id"] not in acknowledged:
            # a read of the posting failed after the preview, or a hold reached the job since (a twin joined with its
            # own): what it said is not known (B.5; Codex H25)
            return (f"posting_not_stored: {hold['message']} ({hold['at'][:16]} UTC); the label waits until a board read "
                    "stores the posting, or label it again from a preview that shows this")
    return None


def rebuilt_note(record: dict) -> str:
    """What a card says of a record the upgrade rebuilt from stored columns (the D190 design, section 7; W1)."""
    from careeros.discovery.board_discovery import BOARD_ATS
    if record.get("ats") in BOARD_ATS:
        return ("its board's own fields (pay, workplace, countries) could not be read: CareerOS may not read this board "
                "now, so this is what was stored before the upgrade")
    return "rebuilt from what was stored before the upgrade: its next read counts as a change"


def write_preview(store: Store, captured: dict) -> str:
    """Record a preview (careeros label JOB apply): an opaque random id, the job, the profile, the captured parts, their
    digest and the holds it showed. Returns its id."""
    import json
    import secrets
    if captured["waiting"]:
        raise PreviewRefused(f"no preview: {captured['waiting']}")
    preview_id = f"preview:{secrets.token_hex(12)}"
    store.writer("review").insert("label_previews", {
        "preview_id": preview_id, "job_id": captured["job_id"], "profile": captured["profile"],
        "digest": captured["digest"], "parts_json": json.dumps(captured["parts"], sort_keys=True, ensure_ascii=False),
        "holds_json": json.dumps(captured["parts"]["holds"]), "created_at": store.clock.now()})
    return preview_id


def agreement(store: Store, profile: str = "primary") -> dict:
    """Latest label per job (for this profile) vs the system's current view for that profile."""
    if profile == "night":
        return _night_agreement(store)
    rows = store.all("""SELECT l.job_id, l.label, j.current_state,
                          (SELECT tier FROM scores s WHERE s.job_id = l.job_id ORDER BY at DESC, rowid DESC LIMIT 1) AS tier
                        FROM labels l JOIN jobs j ON j.job_id = l.job_id
                        WHERE j.exclude_from_kpi = 0 AND l.profile = 'primary'
                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id AND profile = 'primary'
                                            ORDER BY at DESC, rowid DESC LIMIT 1)""")
    picked = [r for r in rows if r["current_state"] == "scored" and r["tier"] in ("A", "B")]
    wanted = [r for r in rows if r["label"] in ("apply", "stretch")]
    false_rejects = [r for r in wanted if r["current_state"] == "rejected" or (r["current_state"] == "scored" and r["tier"] == "archive")]
    agreed = [r for r in picked if r["label"] in ("apply", "stretch")]
    unexplained = store.one("""SELECT COUNT(*) c FROM jobs j JOIN transitions t ON t.workflow_id = j.workflow_id
                               AND t.sequence = j.state_sequence WHERE j.current_state = 'rejected'
                               AND (t.reason_code IS NULL OR t.reason_code = '')""")["c"]
    return {
        "profile": "primary",
        "labelled": len(rows),
        "system_picks_labelled": len(picked),
        "precision_of_picks": round(len(agreed) / len(picked), 3) if picked else None,
        "you_would_apply": len(wanted),
        "false_rejects": len(false_rejects),
        "false_reject_rate": round(len(false_rejects) / len(wanted), 3) if wanted else None,
        "false_reject_jobs": [r["job_id"] for r in false_rejects],
        "unexplained_rejections": unexplained,
        "targets": {"labelled_at_least": 50, "precision_of_picks_at_least": 0.9, "false_reject_rate_at_most": 0.1},
    }


def _night_agreement(store: Store) -> dict:
    rows = store.all("""SELECT l.job_id, l.label, l.reason, n.decision, n.tier, n.mode FROM labels l
                        JOIN jobs j ON j.job_id = l.job_id
                        LEFT JOIN night_results n ON n.result_id = (SELECT result_id FROM night_results WHERE job_id = l.job_id
                                                                     ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.exclude_from_kpi = 0 AND l.profile = 'night'
                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id AND profile = 'night'
                                            ORDER BY at DESC, rowid DESC LIMIT 1)""")

    def measure(subset: list) -> dict:
        picked = [r for r in subset if r["decision"] == "eligible" and r["tier"] in ("A", "B")]
        wanted = [r for r in subset if r["label"] in ("apply", "stretch")]
        false_rejects = [r for r in wanted if r["decision"] == "rejected" or r["tier"] == "archive"]
        schedule_misfits = [r for r in picked if r["label"] == "skip" and (r["reason"] or "").lower().startswith("schedule")]
        return {"labelled": len(subset), "system_picks_labelled": len(picked),
                "precision_of_picks": round(sum(r["label"] in ("apply", "stretch") for r in picked) / len(picked), 3) if picked else None,
                "false_reject_rate": round(len(false_rejects) / len(wanted), 3) if wanted else None,
                "schedule_fit_false_positive_rate": round(len(schedule_misfits) / len(picked), 3) if picked else None}

    return {"profile": "night", **measure(rows),
            "by_mode": {mode: measure([r for r in rows if r["mode"] == mode]) for mode in ("supplemental", "replacement")},
            "targets": {"labelled_at_least": 50, "precision_of_picks_at_least": 0.9, "false_reject_rate_at_most": 0.1,
                        "schedule_fit_false_positive_rate_at_most": 0.1}}


# --- three-way agreement (2026-10-15): apply, stretch and skip measured separately, for Experience Match calibration -----
SYSTEM_CLASSES = ("apply", "stretch", "near_miss_unflagged", "skip", "not_judged")


def system_class(state: str | None, tier: str | None, stretch: bool) -> str:
    """Tier A/B = apply; a near-miss carrying an explicit stretch flag = stretch; any other near-miss stays
    'near_miss_unflagged' (never counted as stretch); archive or rejected = skip; anything else is not judged."""
    if state == "rejected":
        return "skip"
    if tier in ("A", "B"):
        return "apply"
    if tier == "near_miss":
        return "stretch" if stretch else "near_miss_unflagged"
    if tier == "archive":
        return "skip"
    return "not_judged"


def _experience_for(store: Store, job, score, gate_row, policy, profile) -> tuple[dict | None, bool]:
    """(block, computed): the stored Experience Match, else one computed now from the stored requirements (a job
    rejected before scoring, or scored before 2026-10-15). Computing never writes anything."""
    import json

    from careeros.pipeline import load_requirements
    from careeros.qualify import experience
    from careeros.qualify.gate import GateResult
    comps = json.loads(score["components_json"]) if score else {}
    if comps.get("experience"):
        return comps["experience"], False
    if profile is None or not job["extraction_fingerprint"]:
        return None, False
    gate = GateResult(gate_row["decision"], json.loads(gate_row["hard_failures_json"] or "[]"),
                      json.loads(gate_row["flags_json"] or "[]")) if gate_row else None
    block = experience.evaluate(load_requirements(store, job), profile, policy, gate, score["tier"] if score else None,
                                comps.get("skill_coverage"), (comps.get("domain") or {}).get("level"))
    return block, True


def _hypothetical_class(current: str, block: dict | None, stretch: bool) -> str:
    if not block or current == "not_judged":
        return current
    tier, after = block.get("tier"), block.get("hypothetical_tier")
    if block.get("effect") == "reject_by_existing_gate" or after == "rejected":
        return "skip"
    if after == tier:
        return current
    return "stretch" if block.get("effect") == "stretch" else system_class("scored", after, stretch)


def three_way(store: Store, policy, profile=None) -> dict:
    """Your latest PRIMARY label per job against the system: a 3x3 matrix (plus near-misses without a stretch flag and
    jobs not judged), the misses that matter, the gap classes, and the agreement Experience Match WOULD give if its
    enforcement were switched on. Read only."""
    import json
    from collections import Counter

    from careeros.qualify import experience
    rows = store.all("""SELECT l.job_id, l.label FROM labels l JOIN jobs j ON j.job_id = l.job_id
                        WHERE l.profile = 'primary' AND j.exclude_from_kpi = 0
                          AND l.label_id = (SELECT label_id FROM labels WHERE profile = 'primary' AND job_id = l.job_id
                                            ORDER BY at DESC, rowid DESC LIMIT 1) ORDER BY l.job_id""")
    confusion = {label: dict.fromkeys(SYSTEM_CLASSES, 0) for label in LABELS}
    hypothetical = {label: dict.fromkeys(SYSTEM_CLASSES, 0) for label in LABELS}
    gaps: dict = {label: Counter() for label in LABELS}
    moves: Counter = Counter()
    apply_missed, stretch_missed, skip_picked = [], [], []
    computed = with_years = 0
    for row in rows:
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],))
        if job is None:
            continue
        score = store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (row["job_id"],))
        gate_row = store.one("SELECT * FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (row["job_id"],))
        stretch = bool(json.loads(score["components_json"]).get("stretch")) if score else False
        current = system_class(job["current_state"], score["tier"] if score else None, stretch)
        block, was_computed = _experience_for(store, job, score, gate_row, policy, profile)
        computed += was_computed
        if block and block.get("decisive_line") is not None \
                and block["lines"][block["decisive_line"]]["strength"] in experience.REQUIRED_TYPES:
            with_years += 1
        after = _hypothetical_class(current, block, stretch)
        label = row["label"]
        confusion[label][current] += 1
        hypothetical[label][after] += 1
        gaps[label][(block or {}).get("gap_class", "unknown")] += 1
        if after != current:
            moves[f"{current} -> {after}"] += 1
        if label == "apply" and current not in ("apply", "not_judged"):
            apply_missed.append(row["job_id"])
        if label == "stretch" and current not in ("stretch", "not_judged"):
            stretch_missed.append(row["job_id"])
        if label == "skip" and current == "apply":
            skip_picked.append(row["job_id"])

    def agreement_of(matrix: dict) -> float | None:
        judged = sum(n for label in LABELS for cls, n in matrix[label].items() if cls != "not_judged")
        agreed = sum(matrix[label][label] for label in LABELS)
        return round(agreed / judged, 3) if judged else None

    all_scored: Counter = Counter()
    for score in store.all("""SELECT s.components_json FROM scores s JOIN jobs j ON j.job_id = s.job_id
                               WHERE j.current_state = 'scored' AND s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id
                                                                                   ORDER BY at DESC, rowid DESC LIMIT 1)"""):
        block = json.loads(score["components_json"]).get("experience")
        all_scored[block["gap_class"] if block else "not_computed_yet"] += 1
    cfg = experience.settings(policy)["calibration"]
    ready = len(rows) >= cfg["min_labels"] and with_years >= cfg["min_years_labels"]
    return {
        "profile": "primary",
        "labelled": len(rows),
        "confusion": confusion,
        "agreement": agreement_of(confusion),
        "agreement_note": "exact matches over judged jobs; a near-miss without a stretch flag never counts as agreement",
        "apply_false_negatives": {"count": len(apply_missed), "jobs": apply_missed},
        "stretch_misclassified": {"count": len(stretch_missed), "jobs": stretch_missed},
        "skip_false_positives": {"count": len(skip_picked), "jobs": skip_picked},
        "false_reject_rate": agreement(store)["false_reject_rate"],
        "gap_classes": {"labelled": {label: dict(c) for label, c in gaps.items()}, "all_scored": dict(all_scored)},
        "if_experience_enforced": {"confusion": hypothetical, "agreement": agreement_of(hypothetical), "moves": dict(moves)},
        "experience_computed_for_this_report": computed,
        "calibration": {"labels": len(rows), "labels_with_years_requirement": with_years,
                        "needed": {"labels": cfg["min_labels"], "labels_with_years_requirement": cfg["min_years_labels"]},
                        "status": "EXPERIENCE_CALIBRATION_READY" if ready else "not_ready",
                        "enforced": bool(experience.settings(policy)["enforce"]),
                        "note": "ready only means enough labels to review the thresholds; enforcement stays the owner's decision"},
    }


# --- where your labels and the system disagree (OPP-20, 2026-11-03, D90) ------------------------------------------------
CLASS_WORDS = {"apply": "Tier A/B", "stretch": "near-miss, flagged stretch", "near_miss_unflagged": "near-miss, no stretch flag",
               "skip": "rejected or archive", "not_judged": "not judged"}
# The disagreements that matter most come first: a wanted posting the system turns away, then a skipped one it picks.
_ORDER = {("apply", "skip"): 0, ("stretch", "skip"): 0, ("skip", "apply"): 1, ("skip", "stretch"): 1, ("dismissed", "apply"): 2,
          ("dismissed", "stretch"): 2, ("stretch", "apply"): 3, ("apply", "stretch"): 3, ("dismissed", "near_miss_unflagged"): 4}


def class_now(store: Store, job) -> str:
    """The system's class for a job now (`system_class`). A closed posting is judged by its last decision before it
    closed: rejected when its latest gate result rejected it, else its latest score."""
    import json
    score = store.one("SELECT tier, components_json FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job["job_id"],))
    gate = store.one("SELECT decision FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job["job_id"],))
    state = job["current_state"]
    if state == "closed":
        state = "rejected" if gate is not None and gate["decision"] == "rejected" else "scored"
    stretch = bool(json.loads(score["components_json"]).get("stretch")) if score else False
    return system_class(state, score["tier"] if score and state == "scored" else None, stretch)


def disagreements(store: Store, policy) -> dict:
    """Each posting whose latest PRIMARY label, or whose dismissal in force, disagrees with the system's class now, with
    the rule that fired (the verdict line, OPP-03), the class when you labelled it, and the date. A near-miss without a
    stretch flag never counts as agreement (as in `three_way`); a dismissed posting disagrees while the system still
    ranks it (Tier A/B or near-miss). Read only: nothing is recomputed or written."""
    from careeros.review.dismiss import current
    from careeros.review.explain import _verdict
    rows = store.all(f"""SELECT j.*, l.label, l.at AS labelled_at, l.system_tier, l.system_decision
                         FROM jobs j LEFT JOIN labels l ON l.label_id = (SELECT label_id FROM labels WHERE job_id = j.job_id
                                                                        AND profile = 'primary' ORDER BY at DESC, rowid DESC LIMIT 1)
                         WHERE j.exclude_from_kpi = 0 AND (l.label_id IS NOT NULL OR {dismissed_sql('j.job_id')})
                         ORDER BY j.job_id""")
    found, judged, not_judged, dismissed = [], 0, [], 0
    for row in rows:
        in_force = current(store, row["job_id"])   # the latest dismissal event, unless it was a reinstatement
        dismissed += in_force is not None
        now = class_now(store, row)
        if now == "not_judged":
            not_judged.append(row["job_id"])
            continue
        judged += 1
        label_disagrees = row["label"] is not None and now != row["label"]
        dismissal_disagrees = in_force is not None and now in ("apply", "stretch", "near_miss_unflagged")
        if not (label_disagrees or dismissal_disagrees):
            continue
        kind = row["label"] if label_disagrees else "dismissed"
        found.append({"job_id": row["job_id"], "title": row["title"], "employer": row["employer_name"], "label": row["label"],
                      "labelled_at": (row["labelled_at"] or "")[:10] or None,
                      "class_then": system_class(row["system_decision"], row["system_tier"], False) if row["label"] else None,
                      "class_now": now, "dismissed": in_force["reason_code"] if in_force is not None else None,
                      "verdict": _verdict(store, policy, row["job_id"]),
                      "rank": _ORDER.get((kind, now), 5 if now == "near_miss_unflagged" else 3)})
    found.sort(key=lambda r: (r["rank"], r["labelled_at"] or "", r["job_id"]))
    labelled = sum(1 for r in rows if r["label"] is not None)
    return {"labelled": labelled, "dismissed": dismissed, "judged": judged,
            "not_judged": not_judged, "disagreements": found,
            "by_class": dict(sorted(Counter(f"{r['label'] or 'dismissed'} -> {r['class_now']}" for r in found).items()))}


def render_disagreements(result: dict) -> list[str]:
    """`careeros labels disagreements`: one block per posting, the ones that matter most first."""
    out = [f"Where your labels and dismissals disagree with CareerOS now: {len(result['disagreements'])} of {result['judged']} "
           f"judged postings ({result['labelled']} labelled, {result['dismissed']} dismissed). Read only."]
    if result["not_judged"]:
        out.append(f"Not judged (no decision to compare): {len(result['not_judged'])}.")
    for item in result["disagreements"]:
        yours = f"you: {item['label']}" if item["label"] else "you: dismissed"
        if item["label"] and item["dismissed"]:
            yours += f", then dismissed ({item['dismissed']})"
        elif item["dismissed"]:
            yours += f" ({item['dismissed']})"
        out.append(f"- {item['job_id']}  {item['title'][:60]} | {item['employer'][:40]}")
        then = f"; when you labelled it ({item['labelled_at']}): {CLASS_WORDS[item['class_then']]}" if item["label"] else ""
        out.append(f"    {yours}; CareerOS now: {CLASS_WORDS[item['class_now']]}{then}")
        out.append(f"    {item['verdict']}")
    if result["by_class"]:
        out.append("By class: " + "; ".join(f"{k}: {v}" for k, v in result["by_class"].items()))
    return out
