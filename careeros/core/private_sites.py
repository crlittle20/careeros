"""Private site knowledge (refactor R4, 2026-10-14): sites, systems and agencies tied to the job seeker's own situation.

They live in the vault (`policy/private_sites.yaml`), never in this repository, so the shared code holds no personal
specifics. Each list here ADDS to the engine's generic ones:
- never_fetch:       domains never fetched and never treated as an employer's official website;
- manual_systems:    {domain: label} for application systems a link is listed under, never read automatically;
- sources:           {label: allowed | manual | blocked} site-policy statuses (anything unlisted is manual anyway);
- staffing_agencies: agencies whose hiring clients are never guessed, so their boards are never probed;
- agency_shortcut:   an agency offered as a one-click choice when recording an application on the review page;
- sites:             reviewed notes on those sites (documentation only; never read by the code).

Nothing is read in Demo Mode, and an absent file means no additions. CAREEROS_PRIVATE_SITES names another file, or,
when set but empty, no file at all (the test suite sets it so it never depends on a real vault).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from careeros.core.policy import load_yaml

ENV = "CAREEROS_PRIVATE_SITES"
STATUSES = ("allowed", "manual", "blocked")


def _path() -> Path | None:
    configured = os.environ.get(ENV)
    if configured is not None:
        return Path(configured) if configured.strip() else None
    from careeros.paths import vault_paths
    return vault_paths()["private_sites"]


@lru_cache(maxsize=8)
def _load(path: str) -> dict:
    data = load_yaml(Path(path))
    lists = {key: [str(v).strip().lower() for v in data.get(key) or []] for key in ("never_fetch", "staffing_agencies")}
    systems = data.get("manual_systems") or {}
    sources = data.get("sources") or {}
    if not isinstance(systems, dict) or not isinstance(sources, dict):
        raise ValueError("private_sites_invalid: manual_systems and sources must be mappings")
    if any(str(v).strip().lower() not in STATUSES for v in sources.values()):
        raise ValueError("private_sites_invalid: a source status is not allowed, manual or blocked")
    shortcut = data.get("agency_shortcut")
    return {**lists,
            "manual_systems": tuple((str(d).strip().lower(), str(label).strip().lower()) for d, label in systems.items()),
            "sources": {str(k).strip().lower(): str(v).strip().lower() for k, v in sources.items()},
            "agency_shortcut": str(shortcut).strip() if shortcut else None}


def load() -> dict:
    """The private additions, or {} in Demo Mode or when there is no file."""
    from careeros.demo.sandbox import enabled as demo_mode
    if demo_mode():  # Demo Mode never reads the vault
        return {}
    path = _path()
    return _load(str(path)) if path is not None and path.exists() else {}


def never_fetch() -> tuple[str, ...]:
    return tuple(load().get("never_fetch", ()))


def manual_systems() -> tuple[tuple[str, str], ...]:
    return tuple(load().get("manual_systems", ()))


def site_statuses() -> dict[str, str]:
    return dict(load().get("sources", {}))


def staffing_agencies() -> list[str]:
    return list(load().get("staffing_agencies", []))


def agency_shortcut() -> str | None:
    return load().get("agency_shortcut")
