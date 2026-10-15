"""Versioned policy: engine defaults merged with private vault overrides; every decision is stamped with the fingerprint."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fingerprint import file_fingerprint, fingerprint


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"policy_not_mapping:{Path(path).name}")
    return data


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class PolicyBundle:
    data: dict
    fingerprint: str
    files: dict = field(default_factory=dict)  # file name -> sha256

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def load_policy(defaults_path: Path, overrides_path: Path | None = None) -> PolicyBundle:
    data = load_yaml(defaults_path)
    files = {Path(defaults_path).name: file_fingerprint(defaults_path)}
    if overrides_path and Path(overrides_path).exists():
        data = deep_merge(data, load_yaml(overrides_path))
        files[Path(overrides_path).name] = file_fingerprint(overrides_path)
    return PolicyBundle(data=data, fingerprint=fingerprint(data), files=files)
