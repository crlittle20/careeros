"""Source automation status (prompts v2.2): allowed / manual / blocked, read from config/site_policy.yaml.

Checked before every automated fetch. `allowed` = CareerOS may read and grade the source automatically; `manual` =
links are listed for you only; `blocked` = never accessed. Anything not listed is manual. Review the file whenever a
site's terms, robots.txt or API policy changes.
"""
from __future__ import annotations

from functools import lru_cache

from careeros.core import private_sites
from careeros.core.policy import load_yaml
from careeros.paths import CONFIG_DIR

from .http import FetchError

SITE_POLICY = CONFIG_DIR / "site_policy.yaml"
STATUSES = ("allowed", "manual", "blocked")


@lru_cache(maxsize=1)
def statuses() -> dict:
    data = load_yaml(SITE_POLICY).get("sources") or {}
    return {str(k).lower(): (v if v in STATUSES else "manual") for k, v in data.items()}


def status(source: str | None) -> str:
    key = (source or "").lower()
    return private_sites.site_statuses().get(key) or statuses().get(key, "manual")


def require_allowed(source: str) -> None:
    current = status(source)
    if current != "allowed":
        raise FetchError(f"source_{current}:{source}")
