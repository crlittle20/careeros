"""Filesystem layout. The private vault must never sit inside the engine repository."""
from __future__ import annotations

import os
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ENGINE_ROOT / "config"


def vault_dir() -> Path:
    from careeros.demo.sandbox import refuse
    refuse("the private vault")  # Demo Mode works only on its own demo vault (careeros.demo.sandbox.demo_vault)
    vault = Path(os.environ.get("CAREEROS_VAULT", ENGINE_ROOT.parent / "vault")).resolve()
    if vault == ENGINE_ROOT or ENGINE_ROOT in vault.parents:
        raise RuntimeError("vault_inside_engine_repo: the vault must live outside the git repository")
    return vault


def vault_paths(vault: Path | None = None) -> dict[str, Path]:
    root = vault or vault_dir()
    return {
        "root": root,
        "db": root / "data" / "careeros.db",
        "lock": root / "data" / "writer.lock",
        "snapshots": root / "snapshots",
        "reports": root / "reports",
        "logs": root / "logs",
        "inbox": root / "inbox",
        "backups": root / "backups",
        "profile": root / "profile" / "candidate_profile_v0.yaml",
        "overrides": root / "policy" / "overrides.yaml",
        "private_sites": root / "policy" / "private_sites.yaml",   # sites, systems and agencies tied to your situation (R4)
        "curated": root / "sources" / "curated_employers.yaml",   # employers you found independently (Part 9)
        "drafts": root / "drafts",                                 # application briefs (Part 10)
    }
