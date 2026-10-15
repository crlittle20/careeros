"""`careeros demo init | run [--all] | status | reset | tour` (2026-10-13).

The demo drives the PRODUCTION commands (`import`, `run`: board scans, discovery, board verification, processing,
report, backup) against a throwaway demo vault and the synthetic world in world.py, one simulated day at a time on a
fixed clock. Everything it writes stays in the demo vault.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from careeros.core.clock import FixedClock, parse_utc
from careeros.paths import vault_paths

from . import sandbox, world

STATE = "demo_state.json"


def paths(root: Path | None = None) -> dict:
    return vault_paths(root or sandbox.demo_vault())


def _state(p: dict) -> dict:
    file = p["root"] / STATE
    return json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}


def _save(p: dict, state: dict) -> None:
    (p["root"] / STATE).write_text(json.dumps(state, indent=1), encoding="utf-8")


def _require(p: dict) -> dict:
    if not (p["root"] / sandbox.MARKER).exists():
        raise SystemExit(f"No demo vault at {p['root']}: run `careeros demo init` first.")
    return _state(p)


def install_world(p: dict | None = None) -> world.DemoWorld:
    """Load the synthetic world at the demo's current day (for any command run while Demo Mode is on)."""
    p = p or paths()
    demo = world.DemoWorld(max(1, _state(p).get("day", 0) or 1))
    sandbox.install(demo)
    return demo


def demo_clock(p: dict | None = None) -> FixedClock:
    """Commands run in Demo Mode act one minute after the last simulated run, so their records follow the demo's time."""
    state = _state(p or paths())
    now = parse_utc(state.get("now") or world.day_start(1)) + timedelta(minutes=1)
    return FixedClock(now.isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def init() -> Path:
    from careeros import cli
    from careeros.db.store import open_store
    sandbox.enable()
    p = paths()
    root = p["root"]
    if root.exists() and any(root.iterdir()):
        if not (root / sandbox.MARKER).exists():
            raise sandbox.DemoModeViolation(f"{root} exists and is not a demo vault: refusing to use it")
        raise SystemExit(f"The demo vault already exists at {root}: run `careeros demo reset` first (or `careeros demo tour`).")
    for key in ("snapshots", "reports", "logs", "inbox", "backups", "drafts"):
        p[key].mkdir(parents=True, exist_ok=True)
    (root / sandbox.MARKER).write_text("CareerOS demo vault: synthetic data only. Safe to delete.\n", encoding="utf-8")
    for target, text in ((p["profile"], world.PROFILE), (p["overrides"], world.OVERRIDES), (p["curated"], world.curated_yaml())):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for name, text in world.inbox_files().items():
        (p["inbox"] / name).write_text(text, encoding="utf-8")
    day0 = (parse_utc(world.day_start(1)) - timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    clock = FixedClock(day0)
    store = open_store(p["db"], clock)
    for e in world.legacy_boards():  # boards a pre-existing CareerOS already scanned (legacy, awaiting verification)
        store.writer("admin").insert("employers", {
            "employer_id": f"{e['ats']}:{e['token']}", "name": e["name"], "parent": e["name"], "ats": e["ats"],
            "board_token": e["token"], "industry": "healthcare", "status": "verified", "verified_at": day0, "admitted_via": "legacy"})
    store.close()
    install_world(p)
    ctx = cli._context(p, clock=FixedClock(day0))
    from careeros.discovery import board_registry as registry
    registry.sync_legacy(ctx.store)
    for e in world.legacy_boards():  # the same command you would use: careeros sources domain MAPPING example.com
        with _quiet():
            cli.cmd_sources(ctx, p, SimpleNamespace(action="domain", target=f"{e['ats']}:{e['token']}", value=world.site(e["slug"]),
                                                     domain=None))
    ctx.store.close()
    _save(p, {"day": 0, "now": day0, "world": "careeros.demo.world"})
    return root


def run(all_days: bool = False) -> list[int]:
    from careeros import cli
    from careeros.core.locks import SingleWriterLock
    sandbox.enable()
    p = paths()
    state = _require(p)
    first = state.get("day", 0) + 1
    if first > world.DAYS:
        print(f"The demo already ran all {world.DAYS} days: `careeros demo reset` then `careeros demo init` to start again.")
        return []
    days = list(range(first, world.DAYS + 1)) if all_days else [first]
    for day in days:
        sandbox.install(world.DemoWorld(day))
        ctx = cli._context(p, clock=FixedClock(world.day_start(day)))
        print(f"\n=== Demo day {day} ({world.day_start(day)[:10]}) ===")
        with SingleWriterLock(p["lock"]):
            args = SimpleNamespace(employer=None, output_file=None)
            if day == 1:
                cli.cmd_import(ctx, p, args)
            cli.cmd_run(ctx, p, args)
            _act(ctx, day)
        ctx.store.close()
        _save(p, {**state, "day": day, "now": world.day_start(day)})
        state = _state(p)
    return days


def _act(ctx, day: int) -> None:
    """The fictional job seeker's labels and applications for the day (world.ACTIVITY), through the production functions.
    Fixed idempotency keys keep the demo repeatable."""
    from careeros.pipeline import process
    from careeros.review.applications import application_matrix, record_application, update_application
    from careeros.review.labels import record_label
    matrix = application_matrix()
    actions = world.ACTIVITY.get(day, [])
    for n, (action, job_id, value, *rest) in enumerate(actions):
        key = f"demo:{day}:{n}"
        if action == "label":
            record_label(ctx.store, job_id, value, rest[0] if rest else None, key=key)
        elif action == "apply":
            record_application(ctx.store, matrix, job_id, value, key=key)
        else:
            app = ctx.store.one("SELECT application_id FROM applications WHERE job_id = ?", (job_id,))
            update_application(ctx.store, matrix, app["application_id"], value, key=key)
    if actions:
        # An application changes what NIGHT may recommend (right to represent). The next morning's run would re-check
        # it; the last simulated day has no next morning, so the same processing runs now and every day ends settled.
        process(ctx)


def reset() -> bool:
    sandbox.enable()
    root = sandbox.demo_vault()
    if not root.exists():
        return False
    if not (root / sandbox.MARKER).exists():
        raise sandbox.DemoModeViolation(f"{root} has no demo marker: refusing to delete it")
    shutil.rmtree(root)
    return True


def summary(store) -> dict:
    """What the demo produced, for `careeros demo status` and the tests."""
    one = lambda sql, *a: store.one(sql, a)["c"]  # noqa: E731
    return {
        "boards": dict(Counter(r["verification_status"] for r in store.all("SELECT verification_status FROM board_mappings"))),
        "jobs": dict(Counter(r["current_state"] for r in store.all("SELECT current_state FROM jobs"))),
        "tiers": dict(Counter(r["tier"] for r in store.all(
            "SELECT s.tier FROM jobs j JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id "
            "ORDER BY at DESC, rowid DESC LIMIT 1) WHERE j.current_state = 'scored'"))),
        "remote_scopes": dict(Counter(r["remote_scope"] for r in store.all("SELECT remote_scope FROM jobs"))),
        "review_cases": dict(Counter(r["reason_code"] for r in store.all("SELECT reason_code FROM board_reviews WHERE status = 'open'"))),
        "rejections": dict(Counter(r["reason_code"] for r in store.all(
            "SELECT reason_code FROM transitions t WHERE entity_type = 'job' AND to_state = 'rejected' AND sequence = "
            "(SELECT MAX(sequence) FROM transitions WHERE entity_id = t.entity_id)"))),
        "board_activity": dict(Counter(r["board_activity"] for r in store.all(
            "SELECT board_activity FROM board_mappings WHERE collection = 'allowed' AND board_activity IS NOT NULL"))),
        "discovery_outcomes": dict(Counter(r["outcome"] for r in store.all("SELECT outcome FROM discovery_attempts"))),
        "lifecycle_events": dict(Counter(r["event_type"] for r in store.all(
            "SELECT event_type FROM events WHERE event_type IN ('board_quarantined','validation_scan_passed','board_active',"
            "'ats_migration_detected','ats_migration_verified','employer_manual_only','identity_mismatch','employer_rejected',"
            "'board_fingerprint_changed','ats_adapter_degraded')"))),
        "leads": one("SELECT COUNT(*) c FROM leads"),
        "changes": {"meaningful": one("SELECT COUNT(*) c FROM job_changes WHERE meaningful = 1"),
                    "cosmetic": one("SELECT COUNT(*) c FROM transitions WHERE reason_code = 'posting_edited_cosmetic'")},
        "night_routed": one("SELECT COUNT(DISTINCT job_id) c FROM night_results WHERE decision = 'eligible'"),
    }


def status() -> dict:
    from careeros import cli
    sandbox.enable()
    p = paths()
    state = _require(p)
    install_world(p)
    ctx = cli._context(p, clock=demo_clock(p))
    result = {"vault": str(p["root"]), "day": state.get("day", 0), "of_days": world.DAYS, **summary(ctx.store)}
    ctx.store.close()
    return result


class _quiet:
    def __enter__(self):
        self._out, sys.stdout = sys.stdout, open(Path(sandbox.demo_vault()) / "logs" / "demo-init.log", "a", encoding="utf-8")

    def __exit__(self, *exc):
        sys.stdout.close()
        sys.stdout = self._out


def main(args) -> None:
    action = args.action
    if action == "reset":
        print("demo vault removed" if reset() else "no demo vault to remove")
    elif action == "init":
        print(f"Demo vault ready at {init()}\nFictional employers and jobs only. Next: careeros demo run --all")
    elif action == "run":
        run(all_days=getattr(args, "all", False))
    elif action == "tour":
        reset()
        init()
        run(all_days=True)
    if action in ("status", "run", "tour", "init"):
        print(json.dumps(status(), indent=1))
        print("\nExplore it with Demo Mode on, e.g.:  set CAREEROS_DEMO_MODE=true  then  careeros sources health | "
              "careeros explain JOB_ID | careeros review")
