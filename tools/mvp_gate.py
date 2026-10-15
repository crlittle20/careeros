"""Functional MVP gate (Portfolio Prompt 6, 2026-10-13): CareerOS assessed end to end, in Demo Mode only.

    .venv\\Scripts\\python.exe tools\\mvp_gate.py

The assessment drives the real command line (`careeros demo ...`, then ordinary commands with Demo Mode on) on a
fresh, throwaway demo vault. Throughout, a jail records any attempt to send real HTTP, resolve a name, open a socket,
start another program, read a credential, open a mailbox, read the AI key or open a browser.

Expectations are written here from the synthetic postings (careeros/demo/world.py) and the demo policy, never read
back from the system's own decisions. Tiers and priorities are recomputed from the policy's thresholds and weights,
and normalised fields are compared with the posting text.

Grades:
- PASS: every check holds;
- PARTIAL: the stage works, but a supporting check does not;
- FAIL: a core check does not hold.

The result is printed and written to evidence/functional_mvp.json (or --receipt PATH). The private vault is only read (sizes,
timestamps, hashes of files up to 20 MB) to prove the demo never touched it.
"""
from __future__ import annotations

import hashlib
import imaplib
import io
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from careeros import cli  # noqa: E402
from careeros import paths as layout  # noqa: E402
from careeros.core.credentials import WindowsCredentials  # noqa: E402
from careeros.core.locks import SingleWriterLock  # noqa: E402
from careeros.core.policy import load_policy  # noqa: E402
from careeros.demo import sandbox, world  # noqa: E402
from careeros.discovery.http import PROVIDER_HOSTS  # noqa: E402
from careeros.extract import ai  # noqa: E402
from careeros.review.explain import FIELDS  # noqa: E402

ITEMS = ["fresh demo initialization", "synthetic ingestion", "normalization", "employer verification", "duplicate prevention",
         "eligibility", "scoring", "routing", "evidence generation", "output", "failure handling", "repeat processing",
         "privacy isolation"]
ATS_OF = {"meridianrx": "greenhouse", "bluepeakhealth": "lever", "harborline": "greenhouse", "harborlinehealth": "ashby",
          "cascadehealthdata": "lever", "sterlingclaims": "workable", "evergreenhealth": "greenhouse", "northwindbenefits": "ashby",
          "cobaltclaims": "workable", "quillon": "greenhouse", "pinecrest": "ashby", "atlasclaims": "lever"}
ADMITTED = ["meridianrx", "bluepeakhealth", "harborline", "harborlinehealth", "cascadehealthdata", "sterlingclaims", "evergreenhealth",
            "northwindbenefits", "cobaltclaims"]
NOT_ADMITTED = ["quillon", "pinecrest", "atlasclaims"]


def jid(token: str, posting) -> str:
    return f"{ATS_OF[token]}:{token}:{posting}"


BLUEPEAK = "a1f0c3d2-0000-4000-8000-000000000"
CASCADE = "c3f2e5d4-0000-4000-8000-000000000"
HARBOR = "b2e1d4c3-0000-4000-8000-000000000"


class Gate:
    def __init__(self):
        self.checks = {item: [] for item in ITEMS}

    def check(self, item: str, label: str, ok: bool, evidence="", core: bool = True) -> bool:
        self.checks[item].append({"check": label, "ok": bool(ok), "core": core, "evidence": str(evidence)[:400]})
        return ok

    def grade(self, item: str) -> str:
        failed = [c for c in self.checks[item] if not c["ok"]]
        if any(c["core"] for c in failed):
            return "FAIL"
        return "PARTIAL" if failed else "PASS"


class Jail:
    """Records (and refuses) every way out of the sandbox."""
    EXITS = [(httpx.HTTPTransport, "handle_request", "real HTTP"), (socket, "getaddrinfo", "DNS"),
             (socket, "create_connection", "socket"), (socket.socket, "connect", "socket"),
             (subprocess, "Popen", "another program"), (imaplib, "IMAP4_SSL", "IMAP"), (webbrowser, "open", "browser"),
             (WindowsCredentials, "read_entry", "credential store"), (WindowsCredentials, "write", "credential store"),
             (ai, "read_api_key", "AI key")]

    def __init__(self):
        self.attempts, self.hosts, self._stack = [], [], ExitStack()

    def __enter__(self):
        for owner, name, what in self.EXITS:
            self._stack.enter_context(mock.patch.object(owner, name, self._refuse(what)))
        for name in ("ats_handler", "web_handler"):
            self._stack.enter_context(mock.patch.object(world.DemoWorld, name, self._spy(getattr(world.DemoWorld, name))))
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def _refuse(self, what):
        def refuse(*args, **kwargs):
            self.attempts.append(what)
            raise OSError(f"jail: {what}")
        return refuse

    def _spy(self, handler):
        def record(world_self, request):
            self.hosts.append(request.url.host)
            return handler(world_self, request)
        return record


def run(*argv) -> tuple[int, str]:
    """One `careeros ...` command, in this process: (exit code, everything it printed). 99 = an uncaught exception."""
    out, code = io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(out):
        try:
            cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if isinstance(exc.code, str):
                out.write(exc.code)
        except Exception as exc:  # noqa: BLE001 - recorded as a finding
            code = 99
            out.write(f"\nUNCAUGHT {type(exc).__name__}: {exc}")
    return code, out.getvalue()


def rows(vault: Path, sql: str, *args) -> list[dict]:
    con = sqlite3.connect(vault / "data" / "careeros.db")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def count(vault: Path, table: str) -> int:
    return rows(vault, f"SELECT COUNT(*) c FROM {table}")[0]["c"]


def latest(vault: Path, table: str) -> dict:
    """job_id -> the latest row of a per-job decision table (gate_results, scores)."""
    return {r["job_id"]: r for r in rows(vault, f"SELECT * FROM {table} t WHERE rowid = (SELECT rowid FROM {table} "
                                                 f"WHERE job_id = t.job_id ORDER BY at DESC, rowid DESC LIMIT 1)")}


def last_reason(vault: Path) -> dict:
    return {r["job_id"]: r for r in rows(vault, "SELECT j.job_id, j.title, j.employer_name, j.current_state, t.reason_code, t.notes "
                                                "FROM jobs j LEFT JOIN transitions t ON t.entity_id = j.job_id AND t.sequence = "
                                                "(SELECT MAX(sequence) FROM transitions WHERE entity_id = j.job_id)")}


def fingerprint(root: Path) -> dict:
    """Every file under root: size and modification time, plus a hash for files up to 20 MB."""
    result = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            entry = [st.st_size, st.st_mtime_ns]
            if st.st_size <= 20_000_000:
                entry.append(hashlib.sha256(f.read_bytes()).hexdigest())
            result[str(f.relative_to(root))] = entry
    return result


def world_postings(token: str) -> set:
    found = set()
    for day in range(1, world.DAYS + 1):
        found |= {j["id"] for j in (world._postings(token, day) or [])}
    return found


# --- the assessment ------------------------------------------------------------------------------------------------
def assess(gate: Gate, jail: Jail, demo: Path, private: Path, private_before: dict, scratch: Path) -> None:
    policy = None

    # 1. FRESH DEMO INITIALIZATION
    item = "fresh demo initialization"
    code, out = run("demo", "reset")
    gate.check(item, "reset on a machine with no demo vault removes nothing", code == 0 and "no demo vault" in out, out.strip())
    code, out = run("demo", "init")
    gate.check(item, "init succeeds", code == 0 and demo.exists(), out.strip()[:200])
    files = [demo / sandbox.MARKER, demo / "profile" / "candidate_profile_v0.yaml", demo / "policy" / "overrides.yaml",
             demo / "sources" / "curated_employers.yaml", demo / "inbox" / "alert-claims-analyst.eml",
             demo / "inbox" / "pasted-brightwater.txt", demo / "data" / "careeros.db"]
    gate.check(item, "init writes the marker, fictional profile and policy, curated list, inbox files and database",
               all(f.exists() for f in files), [f.name for f in files if not f.exists()] or "all present")
    legacy = rows(demo, "SELECT employer_id FROM employers WHERE admitted_via = 'legacy'")
    gate.check(item, "init seeds the six pre-existing (legacy) boards and no jobs", len(legacy) == 6 and count(demo, "jobs") == 0,
               f"legacy boards={len(legacy)} jobs={count(demo, 'jobs')}")
    profile = (demo / "profile" / "candidate_profile_v0.yaml").read_text(encoding="utf-8")
    gate.check(item, "the profile is the fictional demo persona", profile.startswith("# DEMO persona: fictional"), profile[:60])
    code, out = run("demo", "status")
    gate.check(item, "status after init reports day 0", code == 0 and '"day": 0' in out, core=False, evidence=out[:120])
    marker_before = (demo / sandbox.MARKER).stat().st_mtime_ns
    code, out = run("demo", "init")
    gate.check(item, "a second init is refused and leaves the vault as it is",
               code != 0 and "already exists" in out and (demo / sandbox.MARKER).stat().st_mtime_ns == marker_before, out.strip()[-160:])
    foreign = scratch / "not-a-demo"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("someone's folder", encoding="utf-8")
    with mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(foreign)}):
        code, out = run("demo", "init")
        code2, out2 = run("demo", "reset")
    gate.check(item, "init and reset refuse a folder that is not a demo vault, and leave it untouched",
               code != 0 and code2 != 0 and (foreign / "keep.txt").read_text(encoding="utf-8") == "someone's folder"
               and len(list(foreign.iterdir())) == 1, (out.strip()[-120:], out2.strip()[-120:]))

    # run the four simulated days
    started = time.monotonic()
    code, out = run("demo", "run", "--all")
    took = time.monotonic() - started
    days_ok = all(f"=== Demo day {d} " in out for d in range(1, world.DAYS + 1))
    gate.check("synthetic ingestion", "`careeros demo run --all` runs the four simulated days", code == 0 and days_ok,
               f"exit {code}, {took:.1f}s")
    runs = rows(demo, "SELECT status, started_at FROM runs ORDER BY started_at")
    gate.check("synthetic ingestion", "each day is a completed production run (`careeros run`)",
               [r["status"] for r in runs] == ["completed"] * world.DAYS and [r["started_at"] for r in runs] ==
               [world.day_start(d) for d in range(1, world.DAYS + 1)], runs)
    policy = load_policy(layout.CONFIG_DIR / "policy" / "defaults.yaml", demo / "policy" / "overrides.yaml")

    # 2. SYNTHETIC INGESTION
    item = "synthetic ingestion"
    jobs = {r["job_id"]: r for r in rows(demo, "SELECT * FROM jobs")}
    expected = {jid(t, p) for t in ADMITTED for p in world_postings(t)}
    board_jobs = {j for j, r in jobs.items() if not j.startswith("manual:")}
    gate.check(item, "every posting listed by an admitted board on any day became exactly one job",
               board_jobs == expected, {"missing": sorted(expected - board_jobs), "unexpected": sorted(board_jobs - expected)})
    gate.check(item, "all four supported providers were ingested",
               {r["ats"] for r in jobs.values() if r["source"] == "ats_api"} >= {"greenhouse", "lever", "ashby", "workable"},
               sorted({r["ats"] for r in jobs.values()}))
    leads = rows(demo, "SELECT site, status, job_id FROM leads")
    gate.check(item, "the alert email produced three leads: two board links graded as jobs, the LinkedIn link kept as a lead",
               sorted((r["site"], r["status"]) for r in leads) == [("greenhouse", "ingested"), ("lever", "ingested"), ("linkedin", "new")],
               leads)
    pasted = [r for j, r in jobs.items() if j.startswith("manual:")]
    gate.check(item, "the pasted posting became one job", len(pasted) == 1 and pasted[0]["employer_name"] == "Brightwater Benefits",
               [p["job_id"] for p in pasted])
    kinds = {r["source_kind"] for r in rows(demo, "SELECT DISTINCT source_kind FROM observations")}
    gate.check(item, "every sighting keeps its source (board scan, alert, manual import)", kinds == {"ats_scan", "alert", "manual_import"},
               sorted(kinds))
    gate.check(item, "boards that were not admitted produced no jobs",
               not [j for j in jobs if any(f":{t}:" in j for t in NOT_ADMITTED)], NOT_ADMITTED)
    gate.check(item, "the total is within the 40-60 synthetic jobs asked for", 40 <= len(jobs) <= 60, len(jobs), core=False)

    # 3. NORMALIZATION (expected values read off the posting text)
    item = "normalization"
    expect = {
        jid("meridianrx", 101): {"remote_scope": "US_nationwide", "comp_min": 78000, "comp_max": 92000, "comp_period": "year"},
        jid("meridianrx", 103): {"remote_scope": "US_restricted_states", "remote_states_json": ["AZ", "CO", "TX"], "comp_min": 70000, "comp_max": 80000},
        jid("meridianrx", 107): {"comp_min": None, "comp_max": None},
        jid("bluepeakhealth", BLUEPEAK + "201"): {"comp_min": 76000, "comp_max": 88000, "comp_period": "year"},   # after the day-2 change
        jid("bluepeakhealth", BLUEPEAK + "203"): {"remote_scope": "hybrid"},
        jid("bluepeakhealth", BLUEPEAK + "204"): {"remote_scope": "onsite"},
        jid("bluepeakhealth", BLUEPEAK + "205"): {"remote_scope": "US_restricted_states", "remote_states_json": ["NJ", "NY"]},
        jid("sterlingclaims", "SC4001"): {"comp_min": 26, "comp_max": 30, "comp_period": "hour"},
        jid("sterlingclaims", "SC4004"): {"comp_min": 36, "comp_max": 40, "comp_period": "hour"},
        jid("cascadehealthdata", CASCADE + "406"): {"remote_scope": "US_nationwide", "comp_min": 88000, "comp_max": 102000},
        jid("cascadehealthdata", CASCADE + "407"): {"timezone_requirement": "Eastern"},
    }
    wrong = {}
    for job_id, fields in expect.items():
        row = jobs.get(job_id) or {}
        for field, value in fields.items():
            got = row.get(field)
            if field == "remote_states_json":
                got = sorted(json.loads(got or "[]"))
            if got != value:
                wrong[f"{job_id}.{field}"] = (got, value)
    gate.check(item, "remote scope, state limits, pay range and unit, and time zone match the posting text", not wrong, wrong or f"{len(expect)} postings")
    incomplete = [j for j, r in jobs.items() if not (r["title_norm"] and r["remote_scope"] and r["country"] and r["content_fingerprint"])]
    gate.check(item, "every job has a normalised title, remote scope, country and content fingerprint", not incomplete, incomplete)
    gate.check(item, "every job is placed in the US", {r["country"] for r in jobs.values()} == {"US"}, {r["country"] for r in jobs.values()},
               core=False)
    untitled = [j for j, r in jobs.items() if r["title_norm"] != r["title_norm"].strip().lower()]
    gate.check(item, "normalised titles are lower case and trimmed", not untitled, untitled, core=False)

    # 4. EMPLOYER VERIFICATION
    item = "employer verification"
    boards = {r["mapping_id"]: r for r in rows(demo, "SELECT * FROM board_mappings")}
    status = {m: r["verification_status"] for m, r in boards.items()}
    want = {"greenhouse:meridianrx": "active", "lever:bluepeakhealth": "active", "lever:cascadehealthdata": "active",
            "workable:sterlingclaims": "active", "greenhouse:evergreenhealth": "active", "ashby:northwindbenefits": "active",
            "workable:cobaltclaims": "active", "ashby:harborlinehealth": "active", "greenhouse:harborline": "historical",
            "greenhouse:quillon": "rejected", "ashby:pinecrest": "review_required",
            "workday:summitpayer.wd5.myworkdayjobs.com": "manual_only", "icims:careers-lakeviewrx.icims.com": "manual_only"}
    gate.check(item, "each board ends in the status its story calls for", status == want,
               {m: (status.get(m), w) for m, w in want.items() if status.get(m) != w} or "13 boards as expected")
    auto = policy.get("board_verification", "auto_trust")
    weak = {m: (r["handshake_status"], r["identity_check"], r["board_trust_score"]) for m, r in boards.items()
            if r["verification_status"] == "active" and not (r["handshake_status"] == "confirmed" and r["identity_check"] == "confirmed"
                                                              and (r["board_trust_score"] or 0) >= auto)}
    gate.check(item, f"every active board has the official-site handshake, a confirmed identity and trust >= {auto}", not weak, weak)
    events = {(r["entity_id"], r["event_type"]) for r in rows(demo, "SELECT entity_id, event_type FROM events")}
    new = ["ashby:northwindbenefits", "workable:cobaltclaims", "ashby:harborlinehealth"]
    gate.check(item, "new boards pass through quarantine and a validation scan before they are scanned",
               all((m, "board_quarantined") in events and (m, "validation_scan_passed") in events for m in new), new)
    gate.check(item, "the board naming another company is an identity mismatch and is rejected",
               boards["greenhouse:quillon"]["identity_check"] == "mismatch" and ("greenhouse:quillon", "identity_mismatch") in events,
               boards["greenhouse:quillon"]["identity_check"])
    attempts = {r["slug"]: r["outcome"] for r in rows(demo, "SELECT slug, outcome FROM discovery_attempts")}
    cases = [(r["mapping_id"], r["reason_code"]) for r in rows(demo, "SELECT * FROM board_reviews WHERE status = 'open'")]
    # run 1 expected a review case for Atlas (docs/demo_mode.md said so); the only board_not_found case is the retired
    # Harborline board's. The invalid suggestion is recorded as a discovery attempt and nothing is admitted.
    gate.check(item, "the suggested board that does not exist is recorded as absent and nothing is admitted",
               attempts.get("atlasclaims") == "slug_absent" and not [m for m in boards if "atlasclaims" in m], attempts.get("atlasclaims"))
    report = (demo / "reports" / f"report-{world.day_start(world.DAYS)[:10]}.html").read_text(encoding="utf-8")
    code, listed = run("sources", "list")
    gate.check(item, "you can see that a suggested board does not exist (report or `careeros sources list`)",
               "Atlas" in report or "atlasclaims" in listed, "only in the discovery_attempts table", core=False)
    stale = [c for c in cases if c[0] == "greenhouse:harborline"]
    gate.check(item, "a migration that verification has completed leaves no open review case", not stale, stale, core=False)
    gate.check(item, "the ATS migration keeps the old board as history and verifies the new one",
               ("ashby:harborlinehealth", "ats_migration_verified") in events or any(e == "ats_migration_verified" for _, e in events),
               boards["greenhouse:harborline"]["migrated_to"])
    gate.check(item, "the real but empty board stays active and is recorded as empty",
               boards["greenhouse:evergreenhealth"]["board_activity"] == "empty", boards["greenhouse:evergreenhealth"]["board_activity"])
    manual_hosts = [h for h in jail.hosts if "myworkdayjobs" in h or "icims" in h]
    gate.check(item, "manual-only systems (Workday, iCIMS) are never requested", not manual_hosts, manual_hosts)
    gate.check(item, "the staffing agency's client is never guessed", attempts.get("ridgewaystaffing") in (None, "skipped_policy")
               and not [h for h in jail.hosts if "ridgeway" in h and "example.com" not in h], attempts.get("ridgewaystaffing"))
    gate.check(item, "a board contradicted by the employer's own website is shown", False,
               "not in the demo world (covered by unit tests test_t03 / test_t06, not demonstrated)", core=False)

    # 5. DUPLICATE PREVENTION
    item = "duplicate prevention"
    gate.check(item, "two postings of one requisition: the second is a duplicate of the first",
               jobs[jid("meridianrx", 102)]["current_state"] == "duplicate" and jobs[jid("meridianrx", 102)]["duplicate_of"] == jid("meridianrx", 101),
               (jobs[jid("meridianrx", 102)]["current_state"], jobs[jid("meridianrx", 102)]["duplicate_of"]))
    for job_id in (jid("meridianrx", 101), jid("bluepeakhealth", BLUEPEAK + "202")):
        seen = {r["source_kind"] for r in rows(demo, "SELECT source_kind FROM observations WHERE job_id = ?", job_id)}
        gate.check(item, f"the same job from an alert email and a board scan is one job ({job_id.split(':')[1]})",
                   seen == {"alert", "ats_scan"} and sum(1 for j in jobs if j == job_id) == 1, sorted(seen))
    gate.check(item, "after the ATS migration the re-posted roles are linked to the old ones, not new openings",
               jobs[jid("harborlinehealth", HARBOR + "311")]["repost_of"] == jid("harborline", 301)
               and jobs[jid("harborlinehealth", HARBOR + "312")]["repost_of"] == jid("harborline", 302),
               (jobs[jid("harborlinehealth", HARBOR + "311")]["repost_of"], jobs[jid("harborlinehealth", HARBOR + "312")]["repost_of"]))
    same_title = [r for r in jobs.values() if r["title"] == "Payment Integrity Analyst"]
    gate.check(item, "the same title at two employers stays two jobs", len(same_title) == 2 and len({r["employer_name"] for r in same_title}) == 2,
               [r["job_id"] for r in same_title], core=False)

    # 6. ELIGIBILITY
    item = "eligibility"
    last = last_reason(demo)
    gates = latest(demo, "gate_results")
    by_title = {}
    for r in last.values():
        by_title.setdefault((r["title"], r["employer_name"]), set()).add((r["current_state"], r["reason_code"]))
    decisions = [("Director of Pharmacy Operations", "Meridian Rx", ("rejected", "title_excluded")),
                 ("Senior Software Engineer", "Meridian Rx", ("rejected", "title_excluded")),
                 ("Data Engineer", "Cascade Health Data", ("rejected", "title_excluded")),
                 ("Account Executive", "Northwind Benefits", ("rejected", "title_excluded")),
                 ("Office Coordinator", "Northwind Benefits", ("rejected", "no_target_family")),
                 ("QA Automation Engineer", "Cobalt Claims Technology", ("rejected", "no_target_family")),
                 ("Healthcare Business Systems Analyst", "Bluepeak Health Plan", ("rejected", "not_remote")),
                 ("Reporting Analyst", "Bluepeak Health Plan", ("rejected", "not_remote")),
                 ("Claims Operations Specialist", "Bluepeak Health Plan", ("rejected", "state_not_eligible")),
                 ("Rebate Operations Analyst", "Meridian Rx", ("rejected", "below_pay_floor")),
                 ("Claims Operations Associate", "Sterling Claims Services", ("rejected", "below_pay_floor")),
                 ("Overnight Claims Quality Auditor", "Sterling Claims Services", ("rejected", "below_pay_floor")),
                 ("Remote Claims Analyst", "Brightwater Benefits", ("rejected", "scam_signals"))]
    off = {f"{t} @ {e}": sorted(by_title.get((t, e), set())) for t, e, want in decisions if want not in by_title.get((t, e), set())}
    gate.check(item, "each planted rule violation is rejected for the right reason (13 postings)", not off, off or "all 13")
    floor_y, floor_h = policy.get("compensation", "floor_annual"), policy.get("compensation", "floor_hourly")
    home = policy.get("location", "home_state")
    excluded = [t.lower() for t in policy.get("title_exclude")]
    breaches = {}
    for job_id, r in jobs.items():
        if r["current_state"] != "scored":
            continue
        why = []
        if r["remote_scope"] in ("hybrid", "onsite"):
            why.append("not remote")
        if r["remote_scope"] == "US_restricted_states" and home not in json.loads(r["remote_states_json"] or "[]"):
            why.append("state")
        if r["comp_max"] is not None and r["comp_max"] < (floor_h if r["comp_period"] == "hour" else floor_y):
            why.append("pay")
        if any(re.search(rf"\b{re.escape(t)}\b", r["title"].lower()) for t in excluded):
            why.append("title")
        if not r["family"]:
            why.append("family")
        if why:
            breaches[job_id] = why
    gate.check(item, f"no scored job breaks a hard rule (remote only, {home} allowed, ${floor_y:,} / ${floor_h}/h floor, title exclusions, "
                     "a target family)", not breaches, breaches or f"{sum(r['current_state'] == 'scored' for r in jobs.values())} scored jobs")
    unexplained = [j for j, r in last.items() if r["current_state"] == "rejected" and not (r["reason_code"] and (r["notes"] or
                   json.loads((gates.get(j) or {}).get("hard_failures_json") or "[]")))]
    gate.check(item, "every rejection records a reason code and a plain-language explanation", not unexplained, unexplained)
    pay = [j for j, g in gates.items() if g["decision"] == "rejected" and "below_pay_floor" in g["hard_failures_json"]]
    unquoted = [j for j in pay if not any(f.get("source_text") for f in json.loads(gates[j]["hard_failures_json"]))]
    gate.check(item, "pay-floor rejections quote the posting's pay text", pay and not unquoted, {"pay rejections": len(pay), "unquoted": unquoted})
    unknown = jobs[jid("meridianrx", 107)]
    score107 = latest(demo, "scores").get(unknown["job_id"]) or {}
    facts = json.loads(score107.get("components_json") or "{}").get("facts", [])
    gate.check(item, "a posting with no pay is not rejected; pay is shown as an unknown to check",
               unknown["current_state"] == "scored" and any(f["facet"] == "pay" and f["status"] == "unknown" for f in facts),
               [(f["facet"], f["status"]) for f in facts if f["facet"] == "pay"])

    # 7. SCORING
    item = "scoring"
    scores = latest(demo, "scores")
    scored = [j for j, r in jobs.items() if r["current_state"] == "scored"]
    gate.check(item, "every scored job has a score", all(j in scores for j in scored), [j for j in scored if j not in scores])
    w = policy.get("weights", "priority")
    bad_range, bad_priority, bad_fp, no_basis = [], [], [], []
    for j in scored:
        s = scores[j]
        if not all(0 <= s[k] <= 100 for k in ("eligibility", "candidate_fit", "career_value", "freshness", "apply_priority")):
            bad_range.append(j)
        recomputed = w["candidate_fit"] * s["candidate_fit"] + w["career_value"] * s["career_value"] + w["freshness"] * s["freshness"]
        if abs(recomputed - s["apply_priority"]) > 0.06:
            bad_priority.append((j, s["apply_priority"], round(recomputed, 2)))
        g = gates.get(j) or {}
        if not (s["policy_fingerprint"] and s["profile_fingerprint"] == g.get("profile_fingerprint")
                and s["policy_fingerprint"] == g.get("policy_fingerprint")):
            bad_fp.append(j)
        comp = json.loads(s["components_json"])
        dims = [d for part in ("eligibility", "candidate_fit", "career_value") for d in comp[part]["dims"].values()]
        if not dims or not all(d.get("basis") for d in dims):
            no_basis.append(j)
    gate.check(item, "all scores are within 0-100", not bad_range, bad_range)
    gate.check(item, f"apply priority = {w['candidate_fit']} fit + {w['career_value']} career value + {w['freshness']} freshness (recomputed)",
               not bad_priority, bad_priority or f"{len(scored)} jobs")
    gate.check(item, "each score was made under the same policy and profile as the job's eligibility decision", not bad_fp, bad_fp)
    gate.check(item, "every score component states its basis in words", not no_basis, no_basis)
    gate.check(item, "scores separate the jobs (not all the same)", len({scores[j]["apply_priority"] for j in scored}) > len(scored) // 2,
               len({scores[j]["apply_priority"] for j in scored}), core=False)

    # 8. ROUTING
    item = "routing"
    t = policy.get("tiers")
    wrong_tier = []
    for j in scored:
        s = scores[j]
        comp = json.loads(s["components_json"])
        strong = s["state"] == "ELIGIBLE" and comp["skill_coverage"] >= t["skill_coverage_min"]
        p = s["apply_priority"]
        tier = ("A" if strong and p >= t["A"]["priority"] else "B" if strong and p >= t["B"]["priority"]
                else "near_miss" if p >= t["near_miss"]["priority"] else "archive")
        if comp.get("stretch") and tier in ("A", "B"):
            tier = "near_miss"
        if tier != s["tier"]:
            wrong_tier.append((j, s["tier"], tier))
    gate.check(item, f"every tier matches the policy thresholds (A {t['A']['priority']}, B {t['B']['priority']} with ELIGIBLE and "
                     f"{t['skill_coverage_min']:.0%} skills, near-miss {t['near_miss']['priority']})", not wrong_tier,
               wrong_tier or {k: sum(scores[j]["tier"] == k for j in scored) for k in ("A", "B", "near_miss", "archive")})
    shown = [j for j in scored if scores[j]["tier"] != "archive"]
    gate.check(item, "every A / B / near-miss job is on the final report", all(j in report for j in shown), [j for j in shown if j not in report])
    night = {r["job_id"]: r for r in rows(demo, "SELECT * FROM night_results n WHERE rowid = (SELECT rowid FROM night_results "
                                                "WHERE job_id = n.job_id ORDER BY at DESC, rowid DESC LIMIT 1)")}
    overnight = jid("sterlingclaims", "SC4001")
    # run 1 expected NIGHT to accept this posting (docs/demo_mode.md said so). By the policy it cannot: a full-time
    # overnight job is a NIGHT Replacement job, judged on the annual floor, and $30/h x 2,080 h = $62,400 < $70,000.
    replacement = policy.get("night", "modes", "replacement", "compensation", "floor_annual")
    n = night.get(overnight) or {}
    gate.check(item, f"NIGHT judges the full-time overnight posting as Replacement work against its own ${replacement:,} floor, "
                     "apart from PRIMARY", n.get("mode") == "replacement" and n.get("schedule") == "overnight" and n.get("decision") == "rejected"
               and 30 * 2080 < replacement and last[overnight]["reason_code"] == "below_pay_floor",
               (n.get("mode"), n.get("schedule"), n.get("decision"), last[overnight]["reason_code"]))
    routed = [j for j, r in night.items() if r["decision"] == "eligible"]
    gate.check(item, "NIGHT routes jobs to its own section (CONDITIONAL when the hours are not stated)",
               routed and all(night[j]["state"] in ("ELIGIBLE", "CONDITIONAL") for j in routed) and "NIGHT · Supplemental" in report,
               f"{len(routed)} routed: " + str(sorted({night[j]['state'] for j in routed})))
    gate.check(item, "an overnight posting that NIGHT accepts is shown", any(night[j]["schedule"] == "overnight" for j in routed),
               "no overnight posting in the demo world passes NIGHT", core=False)
    gate.check(item, "rejected, duplicate and closed jobs are never routed to a tier",
               not [j for j, r in jobs.items() if r["current_state"] != "scored" and j in scores and scores[j]["at"] > (gates.get(j) or {}).get("at", "")],
               "no tier after a rejection", core=False)

    # 9. EVIDENCE GENERATION
    item = "evidence generation"
    incomplete, broken = {}, []
    for job_id in sorted(jobs):
        code, out = run("explain", job_id, "--json")
        try:
            doc = json.loads(out[out.index("{"):])
        except ValueError:
            broken.append(job_id)
            continue
        missing = [f for f in FIELDS if doc.get(f) in (None, "", [], {})]
        if code != 0 or missing:
            incomplete[job_id] = missing
    gate.check(item, f"`careeros explain JOB --json` answers all {len(FIELDS)} questions for every job ({len(jobs)})",
               not incomplete and not broken, {"incomplete": incomplete, "unreadable": broken} if incomplete or broken else f"{len(jobs)} jobs")
    unexplained_boards = []
    for mapping in sorted(boards):
        code, out = run("sources", "explain", mapping)
        if code != 0 or f"{mapping}" not in out or "status=" not in out:
            unexplained_boards.append(mapping)
    gate.check(item, f"`careeros sources explain` explains every board ({len(boards)})", not unexplained_boards, unexplained_boards)
    code, out = run("verify")
    try:
        verdict = json.loads(out[out.index("{"):])
    except ValueError:
        verdict = None
    gate.check(item, "`careeros verify`: the hash-chained audit log and every job's history are consistent",
               code == 0 and verdict == {"event_chain_breaks": [], "inconsistent_jobs": {}, "inconsistent_applications": {}}, verdict or out[-200:])
    unprinted = [r["result_id"] for r in rows(demo, "SELECT * FROM gate_results")
                 if not (r["policy_fingerprint"] and r["profile_fingerprint"] and r["extraction_fingerprint"])]
    gate.check(item, "every eligibility decision records the policy, profile and extraction it was made under", not unprinted, unprinted)

    # 10. OUTPUT
    item = "output"
    reports = [demo / "reports" / f"report-{world.day_start(d)[:10]}.html" for d in range(1, world.DAYS + 1)]
    gate.check(item, "a review report for each simulated day", all(r.exists() and r.stat().st_size > 10_000 for r in reports),
               [r.name for r in reports if not r.exists()])
    sections = ["Tier A", "Tier B", "Near-miss", "Check before applying", "Leads from alerts", "NIGHT", "Rejected", "Archive", "Sources and discovery"]
    gate.check(item, "the report has the tier, check-first, leads, NIGHT, rejected, archive and sources sections",
               all(s in report for s in sections), [s for s in sections if s not in report])
    bad_backup = []
    for d in range(1, world.DAYS + 1):
        folder = demo / "backups" / world.day_start(d)[:10]
        try:
            manifest = json.loads((folder / "careeros-backup.json").read_text(encoding="utf-8"))
            ok = manifest["integrity_check"] == "ok" and manifest["files"]["careeros.db"]["sha256"] == hashlib.sha256(
                (folder / "careeros.db").read_bytes()).hexdigest()
        except (OSError, KeyError, ValueError):
            ok = False
        if not ok:
            bad_backup.append(folder.name)
    gate.check(item, "a verified backup for each day (integrity check, SHA-256 manifest)", not bad_backup, bad_backup)
    active = sum(1 for r in boards.values() if r["verification_status"] == "active")
    review = sum(1 for r in boards.values() if r["verification_status"] == "review_required")
    figures = re.search(r"(\d+) verified boards", report), re.search(r"\(the employer's own site first\): (\d+) verified", report)
    gate.check(item, "the report's verified-board figures agree with the board records (only admitted boards count as verified)",
               all(figures) and int(figures[0].group(1)) == active == int(figures[1].group(1)),
               {"30-day line": figures[0] and figures[0].group(1), "status line": figures[1] and figures[1].group(1), "active boards": active})
    code, out = run("metrics")
    try:
        employers = json.loads(out[out.index("{"):])["employers"]
    except (ValueError, KeyError):
        employers = {}
    gate.check(item, "`careeros metrics` counts verified and awaiting-review boards from the verification records",
               employers.get("verified") == active and employers.get("awaiting_your_verification") == review,
               {"metrics": {k: employers.get(k) for k in ("verified", "awaiting_your_verification")}, "records": (active, review)})
    index = demo / "reports" / "inbox-index" / "seen_jobs.json"
    gate.check(item, "the seen-jobs index is written locally", index.exists() and json.loads(index.read_text(encoding="utf-8")) is not None,
               index.name, core=False)
    code, out = run("status")
    counted = dict(re.findall(r"(\w+)=(\d+)", next((line for line in out.splitlines() if line.startswith("jobs:")), "")))
    actual = {}
    for r in jobs.values():
        actual[r["current_state"]] = actual.get(r["current_state"], 0) + 1
    gate.check(item, "`careeros status` reports the same job counts as the database", {k: int(v) for k, v in counted.items()} == actual,
               (counted, actual))
    code, out = run("demo", "status")
    gate.check(item, "`careeros demo status` summarises the demo", code == 0 and '"day": 4' in out, core=False, evidence=out[:80])

    # 11. REPEAT PROCESSING (before any fault injection)
    item = "repeat processing"
    tables = ["jobs", "transitions", "gate_results", "scores", "leads", "night_results", "job_changes"]
    before = {tb: count(demo, tb) for tb in tables}
    code, out = run("process")
    gate.check(item, "processing again changes nothing", code == 0 and {tb: count(demo, tb) for tb in tables} == before,
               {tb: count(demo, tb) - before[tb] for tb in tables})
    code, out = run("import")
    gate.check(item, "importing the same alert email and pasted posting again adds nothing",
               code == 0 and {tb: count(demo, tb) for tb in tables} == before, {tb: count(demo, tb) - before[tb] for tb in tables})
    states_before = {j: r["current_state"] for j, r in jobs.items()}
    code, out = run("run")
    after = {r["job_id"]: r["current_state"] for r in rows(demo, "SELECT job_id, current_state FROM jobs")}
    gate.check(item, "a whole extra daily run over unchanged boards adds no job and changes no decision",
               code == 0 and after == states_before and {tb: count(demo, tb) for tb in tables} == before,
               {tb: count(demo, tb) - before[tb] for tb in tables})
    second = scratch / "second-demo"
    with mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(second)}):
        code, out = run("demo", "tour")
    pick = "SELECT j.job_id, j.current_state, s.tier, s.apply_priority FROM jobs j LEFT JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)"
    first_run = {tuple(r.values()) for r in rows(demo, pick)}
    second_run = {tuple(r.values()) for r in rows(second, pick)}
    gate.check(item, "a second fresh demo gives the same jobs, states, tiers and priorities", code == 0 and first_run == second_run,
               {"only first": len(first_run - second_run), "only second": len(second_run - first_run)})
    # Added after run 2, from a live-data rehearsal: change the rules (exclude "pharmacy" titles) and the reading
    # vocabulary together, re-process, then process once more. The second pass must change nothing.
    overrides = demo / "policy" / "overrides.yaml"
    original_overrides = overrides.read_text(encoding="utf-8")
    overrides.write_text(original_overrides + "title_exclude: " + json.dumps(list(policy.get("title_exclude")) + ["pharmacy"])
                         + '\nskill_aliases: {"claims processing": ["claims processing"]}\n', encoding="utf-8")
    run("process")
    changed = last_reason(demo).get(jid("meridianrx", 108)) or {}
    middle = {tb: count(demo, tb) for tb in tables}
    code, out = run("process")
    gate.check(item, "after a rules and vocabulary change, the re-evaluation settles: a second pass changes nothing",
               changed.get("reason_code") == "title_excluded" and code == 0 and {tb: count(demo, tb) for tb in tables} == middle,
               {"Pharmacy Claims Analyst": changed.get("reason_code"), "second pass": {tb: count(demo, tb) - middle[tb] for tb in tables}})
    overrides.write_text(original_overrides, encoding="utf-8")
    run("process")   # back to the demo's own policy for the checks that follow

    # 12. FAILURE HANDLING
    item = "failure handling"
    trust_before = {r["mapping_id"]: (r["board_trust_score"], r["verification_status"]) for r in rows(demo, "SELECT * FROM board_mappings")}
    states_before = {r["job_id"]: r["current_state"] for r in rows(demo, "SELECT job_id, current_state FROM jobs")}
    exceptions_before = count(demo, "exceptions")
    original = world.DemoWorld.ats_handler   # the jail's spy, which wraps the world's handler

    def faulty(world_self, request):
        url = str(request.url)
        if "meridianrx" in url:
            return httpx.Response(503)
        if "bluepeakhealth" in url:
            return httpx.Response(200, content=b"{not json")
        if "cascadehealthdata" in url:
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        if "northwindbenefits" in url:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return original(world_self, request)
    started = time.monotonic()
    with mock.patch.object(world.DemoWorld, "ats_handler", faulty):
        code, out = run("run")
    took = time.monotonic() - started
    last_run = rows(demo, "SELECT status FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1")[0]["status"]
    gate.check(item, "a run with four failing boards (HTTP 503, invalid JSON, timeout, 1-hour rate limit) completes",
               code == 0 and last_run == "completed", f"exit {code}, run {last_run}, {took:.1f}s")
    scans = {r["employer_id"]: r for r in rows(demo, "SELECT * FROM source_scans s WHERE rowid = (SELECT MAX(rowid) FROM source_scans "
                                                    "WHERE employer_id = s.employer_id)")}
    failing = {"greenhouse:meridianrx": "http_503", "lever:bluepeakhealth": "invalid_json",
               "lever:cascadehealthdata": "network:ConnectTimeout", "ashby:northwindbenefits": "rate_limited"}
    gate.check(item, "each failure is recorded against its board with its own error code",
               all((scans.get(b) or {}).get("error_code") == code_ for b, code_ in failing.items()),
               {b: (scans.get(b) or {}).get("error_code") for b in failing})
    gate.check(item, "the healthy boards in the same run are still scanned completely",
               all((scans.get(b) or {}).get("outcome") == "complete" for b in ("workable:sterlingclaims", "workable:cobaltclaims", "ashby:harborlinehealth")),
               {b: (scans.get(b) or {}).get("outcome") for b in ("workable:sterlingclaims", "workable:cobaltclaims", "ashby:harborlinehealth")})
    now_states = {r["job_id"]: r["current_state"] for r in rows(demo, "SELECT job_id, current_state FROM jobs")}
    gate.check(item, "a failed scan never closes or changes a job", now_states == states_before,
               {j: (states_before.get(j), s) for j, s in now_states.items() if states_before.get(j) != s})
    trust_now = {m: (r["board_trust_score"], r["verification_status"]) for m, r in
                 {r["mapping_id"]: r for r in rows(demo, "SELECT * FROM board_mappings")}.items()}
    gate.check(item, "an outage lowers board health, never trust or verification", trust_now == trust_before,
               {m: (trust_before[m], trust_now[m]) for m in trust_now if trust_now[m] != trust_before.get(m)})
    gate.check(item, "every failure is logged as an exception for follow-up", count(demo, "exceptions") - exceptions_before >= len(failing),
               count(demo, "exceptions") - exceptions_before)
    gate.check(item, "a 1-hour Retry-After is reported, not slept through", took < 30, f"{took:.1f}s", core=False)
    (demo / "inbox" / "broken.eml").write_bytes(b"\x00\xff\xfe not an email \x80")
    (demo / "inbox" / "empty.txt").write_text("", encoding="utf-8")
    code, out = run("import")
    gate.check(item, "an unreadable email and an empty pasted file are reported and skipped", code == 0 and "empty.txt" in out, out.strip()[-200:])
    with SingleWriterLock(demo / "data" / "writer.lock"):
        code, out = run("run")
    gate.check(item, "a second writer is refused while one is running", code == 1 and "busy" in out, out.strip()[-120:])
    code, out = run("gmail", "status")
    gate.check(item, "a refused integration in Demo Mode ends with a one-line message", code != 0 and "Traceback" not in out
               and "UNCAUGHT" not in out, out.strip().splitlines()[-1] if out.strip() else "", core=False)

    # DUPLICATE APPLICATIONS (after the repeat checks: it changes the demo)
    item = "duplicate prevention"
    apps_before = count(demo, "applications")   # the demo's job seeker has applied to a few jobs already
    code1, out1 = run("applied", jid("meridianrx", 101), "--channel", "direct")
    code2, out2 = run("applied", jid("meridianrx", 102), "--channel", "agency", "--agency", "Fictional Staffing")
    code3, out3 = run("applied", jid("meridianrx", 101), "--channel", "direct")
    apps = count(demo, "applications")
    gate.check(item, "one application per role: applying to its duplicate or applying twice is refused",
               code1 == 0 and code2 != 0 and code3 != 0 and apps == apps_before + 1,
               (out2.strip()[-120:], out3.strip()[-120:], f"applications {apps_before} -> {apps}"))

    # 13. PRIVACY ISOLATION
    item = "privacy isolation"
    for args, what in ((("inbox",), "GitHub inbox"), (("paste", "--clipboard"), "clipboard")):
        code, out = run(*args)
        gate.check(item, f"the {what} is refused in Demo Mode", code != 0 and "not available in Demo Mode" in out, out.strip()[-120:])
    gate.check(item, "Gmail is refused in Demo Mode", "not available in Demo Mode" in run("gmail", "status")[1], "gmail status")
    gate.check(item, "nothing tried to reach the real network, another program, a credential, a mailbox or the AI key",
               not jail.attempts, jail.attempts or f"{len(jail.hosts)} requests, all answered by the synthetic world")
    outside = sorted({h for h in jail.hosts if h not in PROVIDER_HOSTS and not h.endswith(".example.com")})
    gate.check(item, "every request went to one of the four job-board APIs or a fictional *.example.com site", not outside,
               outside or sorted({h for h in jail.hosts if h in PROVIDER_HOSTS}))
    gate.check(item, "the private vault (a decoy on a machine without one) is untouched: sizes, timestamps, hashes", fingerprint(private) == private_before,
               f"{len(private_before)} files compared")
    gate.check(item, "the demo vault is outside the private vault and the code, and carries the demo marker",
               (demo / sandbox.MARKER).exists() and not demo.resolve().is_relative_to(private.resolve())
               and not demo.resolve().is_relative_to(layout.ENGINE_ROOT), str(demo))
    addresses = set()
    for f in demo.rglob("*"):
        if f.is_file() and "backups" not in f.parts:
            addresses |= set(re.findall(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", f.read_bytes()))
    foreign_addresses = sorted(a.decode(errors="replace") for a in addresses if not a.endswith(b"example.com"))
    gate.check(item, "the only email addresses anywhere in the demo are fictional example.com ones", not foreign_addresses,
               foreign_addresses or sorted(a.decode() for a in addresses))
    with mock.patch.dict(os.environ, {sandbox.ENV: "true"}):
        try:
            layout.vault_dir()
            located = True
        except sandbox.DemoModeViolation:
            located = False
    gate.check(item, "in Demo Mode the private vault cannot even be located", not located, "paths.vault_dir() refuses")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Functional MVP gate (Demo Mode only)")
    parser.add_argument("--receipt", type=Path, default=ROOT / "evidence" / "functional_mvp.json",
                        help="where to write the result (tools/validate.py uses a temporary file)")
    args = parser.parse_args(argv)
    os.environ.pop(sandbox.ENV, None)
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    except OSError:
        commit, dirty = "unknown", None
    scratch = Path(tempfile.mkdtemp(prefix="careeros-mvp-gate-"))
    demo = scratch / "demo-vault"
    private = layout.vault_dir()
    decoy = not private.exists()
    if decoy:  # no private vault on this machine: compare a decoy instead
        private = scratch / "private-vault"
        (private / "data").mkdir(parents=True)
        (private / "data" / "careeros.db").write_bytes(b"decoy")
    print(f"Functional MVP gate: commit {commit}{' (uncommitted changes)' if dirty else ''}; demo vault {demo}")
    print(f"Fingerprinting the private vault at {private} ...")
    private_before = fingerprint(private)
    gate = Gate()
    env = {sandbox.VAULT_ENV: str(demo)}
    if decoy:
        env["CAREEROS_VAULT"] = str(private)
    started = time.monotonic()
    try:
        with mock.patch.dict(os.environ, env), Jail() as jail:
            assess(gate, jail, demo, private, private_before, scratch)
    finally:
        for handler in list(cli.log.handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(scratch):
                handler.close()
                cli.log.removeHandler(handler)
        sandbox.install(None)
        shutil.rmtree(scratch, ignore_errors=True)
    took = time.monotonic() - started

    grades = {item: gate.grade(item) for item in ITEMS}
    print()
    for item in ITEMS:
        print(f"{grades[item]:8} {item}")
        for c in gate.checks[item]:
            mark = "ok  " if c["ok"] else ("FAIL" if c["core"] else "gap ")
            print(f"           {mark} {c['check']}" + ("" if c["ok"] else f"\n                  -> {c['evidence']}"))
    overall = "FAIL" if "FAIL" in grades.values() else "PARTIAL" if "PARTIAL" in grades.values() else "PASS"
    checks = sum(len(v) for v in gate.checks.values())
    print(f"\nFunctional MVP: {overall} ({sum(g == 'PASS' for g in grades.values())} PASS, "
          f"{sum(g == 'PARTIAL' for g in grades.values())} PARTIAL, {sum(g == 'FAIL' for g in grades.values())} FAIL; "
          f"{checks} checks in {took:.0f}s)")
    places = [(scratch, "<scratch>"), (private, "<private vault>"), (Path.home(), "<home>")]

    def scrub(text: str) -> str:  # the evidence file names no local folders (it may be published with the code)
        for place, label in places:
            for path in (str(place), str(place).split(":", 1)[-1]):   # with and without the drive letter
                for form in (path, path.replace("\\", "\\\\")):
                    text = text.replace(form, label)
        return text
    checks = {item: [{**c, "evidence": scrub(c["evidence"])} for c in found] for item, found in gate.checks.items()}
    receipt = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": commit, "uncommitted_changes": dirty,
               "private_vault": "decoy" if decoy else "the real private vault (read only)", "seconds": round(took),
               "overall": overall, "grades": grades, "checks": checks}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    return 0 if overall != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
