"""Leads you have looked at. Only their status changes here; the alert-email reader (discovery) creates them."""
from __future__ import annotations

from careeros.db.store import Store


def dismiss_lead(store: Store, lead_id: str, *, key: str) -> str:
    if store.one("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)) is None:
        raise ValueError("unknown_lead")
    writer = store.writer("review")
    with store.tx():
        if writer.receipt("dismiss_lead", key, {"lead_id": lead_id}, actor_id="user") == "replay":
            return "replay"
        writer.update("leads", {"status": "dismissed"}, "lead_id = ? AND status = 'new'", (lead_id,))
        writer.event("lead_dismissed", outcome="informational", entity_type="lead", entity_id=lead_id,
                     actor_type="human", actor_id="user")
    return "new"
