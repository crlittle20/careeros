"""Alert notices (part 3 of the owner's D192 build; the D190 design, revision 3, section 5). An alert card whose own link
names no posting (a careers page, a search page) is a notice, never a lead: it is stored in its own table
(alert_notices), one row per card occurrence, outside lead resolution and the lead counts. Notices are shown grouped by
the employer each names when that resolves, else by its sender; the tracked postings beside one are computed when shown,
read-only. Your actions: dismiss it, or add the posting by its own link (careeros notices add), each recorded with a
receipt and an event."""
from __future__ import annotations

from careeros.db.store import Store


def groups(store: Store, states: tuple = ("new",), profile: str | None = None) -> list[tuple[str, list]]:
    """[(group, notices)]: by the employer each names when it resolves, else by sender; newest first in each group, the
    groups by their newest notice."""
    marks = ",".join("?" * len(states))
    rows = store.all(f"SELECT * FROM alert_notices WHERE state IN ({marks})" + (" AND profile = ?" if profile else "")
                     + " ORDER BY first_seen_at DESC, notice_id", (*states, *((profile,) if profile else ())))
    grouped: dict[str, list] = {}
    for row in rows:
        label = row["employer_key"] or f"from {row['sender'] or 'an alert'}"
        grouped.setdefault(label, []).append(row)
    return list(grouped.items())


def likely_postings(store: Store, policy, notice, cache: dict | None = None) -> list:
    """Open tracked postings with the notice's title at the employer it names (when that resolves): computed when shown,
    read-only, never linked or matched (advice)."""
    if not notice["employer_key"]:
        return []
    cache = {} if cache is None else cache
    if "inputs" not in cache:
        from careeros.review.submission import _identity_inputs
        cache["inputs"] = _identity_inputs(store, policy)
    from careeros.normalize.employer_identity import RESOLVED, resolve
    parents = policy.get("parents", default={}) or {}
    found = []
    for job in store.all("SELECT job_id, title, employer_name, parent, location_raw FROM jobs WHERE title_norm = ? "
                         "AND current_state NOT IN ('closed', 'duplicate') ORDER BY first_seen_at, job_id", (notice["title_norm"],)):
        who = resolve(job["employer_name"], job["parent"], parents, **cache["inputs"])
        if who.status == RESOLVED and who.key == notice["employer_key"]:
            found.append(job)
    return found


def dismiss_notice(store: Store, notice_id: str, *, key: str) -> str:
    """You dismissed a notice: it leaves the review page and the listing. Idempotent by its key."""
    if store.one("SELECT 1 FROM alert_notices WHERE notice_id = ?", (notice_id,)) is None:
        raise ValueError("unknown_notice")
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("dismiss_notice", key, {"notice_id": notice_id}, actor_id="user") == "replay":
            return "replay"
        writer.update("alert_notices", {"state": "dismissed", "state_at": store.clock.now()},
                      "notice_id = ? AND state = 'new'", (notice_id,))
        writer.event("notice_dismissed", outcome="informational", entity_type="notice", entity_id=notice_id,
                     actor_type="human", actor_id="user")
    return "new"


def mark_added(store: Store, notice_id: str, job_id: str, *, key: str) -> str:
    """You added the notice's posting (careeros notices add): the notice records the job it became."""
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("add_notice", key, {"notice_id": notice_id, "job_id": job_id}, actor_id="user") == "replay":
            return "replay"
        writer.update("alert_notices", {"state": "added", "state_at": store.clock.now(), "job_id": job_id},
                      "notice_id = ?", (notice_id,))
        writer.event("notice_added", outcome="success", entity_type="notice", entity_id=notice_id,
                     actor_type="human", actor_id="user", metadata={"job_id": job_id})
    return "new"
