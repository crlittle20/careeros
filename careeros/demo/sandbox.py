"""Demo Mode boundary (2026-10-13).

Demo Mode is switched on with CAREEROS_DEMO_MODE=true (or by any `careeros demo ...` command). While it is on:
- the vault is a throwaway DEMO vault (CAREEROS_DEMO_VAULT, default <temp>/careeros-demo-vault), never the private one,
  never inside the code repository, and only ever deleted when it carries the demo marker file;
- every HTTP client and website reader gets a synthetic transport serving fictional job boards and employer sites. A
  client created while Demo Mode is on but no synthetic world is loaded fails instead of reaching the internet;
- credentials are never read (the credential store refuses), and Gmail, IMAP, the GitHub recommendation inbox,
  publishing and notifications are skipped or refused;
- the requirement-reading AI is never used.

The production business logic is otherwise unchanged: the demo runs the same `careeros` commands.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV = "CAREEROS_DEMO_MODE"
VAULT_ENV = "CAREEROS_DEMO_VAULT"
MARKER = ".careeros-demo"
DEMO_ADDRESS = "93.184.216.34"   # the address documentation domains like example.com use; nothing is ever sent to it
_WORLD = None


class DemoModeViolation(RuntimeError):
    """Something tried to leave the demo sandbox (real network, credentials, private vault)."""


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def enable() -> None:
    os.environ[ENV] = "true"


def demo_vault() -> Path:
    from careeros.paths import ENGINE_ROOT
    vault = Path(os.environ.get(VAULT_ENV) or Path(tempfile.gettempdir()) / "careeros-demo-vault").resolve()
    private = Path(os.environ.get("CAREEROS_VAULT") or ENGINE_ROOT.parent / "vault").resolve()
    if vault == private or private in vault.parents or vault in private.parents:
        raise DemoModeViolation(f"the demo vault must be separate from the private vault ({private})")
    if vault == ENGINE_ROOT or ENGINE_ROOT in vault.parents:
        raise DemoModeViolation("the demo vault must live outside the code repository")
    return vault


def install(world) -> None:
    """Load the synthetic world whose job boards and websites the demo's network traffic is answered from."""
    global _WORLD
    _WORLD = world


def world():
    return _WORLD


def transport(kind: str):
    """The transport an HTTP client or website reader must use: None outside Demo Mode (the normal network); in Demo
    Mode, the synthetic world's transport, or a refusal when no world is loaded (never the real network)."""
    if not enabled():
        return None
    if _WORLD is None:
        raise DemoModeViolation("Demo Mode is on but no synthetic world is loaded: refusing to use the real network")
    import httpx
    return httpx.MockTransport(_WORLD.ats_handler if kind == "ats" else _WORLD.web_handler)


def resolver():
    """Demo Mode name resolution: synthetic sites resolve to one documentation address; nothing else resolves."""
    if not enabled():
        return None

    def resolve(host: str) -> list[str]:
        from careeros.discovery.safe_fetch import SiteFetchError
        if _WORLD is not None and _WORLD.knows_host(host):
            return [DEMO_ADDRESS]
        raise SiteFetchError("dns_failure", "not part of the demo world", host)
    return resolve


def refuse(what: str) -> None:
    """Raise when Demo Mode is on: for integrations that must never run in a demo (Gmail, GitHub, credentials...)."""
    if enabled():
        raise DemoModeViolation(f"{what} is not available in Demo Mode")
