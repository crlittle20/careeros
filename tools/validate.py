"""One validation command, the same one CI runs (Portfolio Prompt 10, 2026-10-13).

    .venv-dev\\Scripts\\python.exe tools\\validate.py              # every check CI runs
    .venv-dev\\Scripts\\python.exe tools\\validate.py --offline    # all but pip-audit, the one check that needs the internet
    .venv-dev\\Scripts\\python.exe tools\\validate.py --only lint,types
    .venv-dev\\Scripts\\python.exe tools\\validate.py --full       # also every planted defect (tools/mutate.py; slow)

Set up once: `python -m venv .venv-dev`, then install requirements-dev.lock (the command is at the top of that file).

Checks, in order (all run; the exit code is 1 if any fails):
  lint       ruff: pyflakes, bugbear, pylint errors and bandit security rules (pyproject.toml)
  format     ruff's layout rules: trailing whitespace, final newline, tabs, mixed indentation, line length
  types      mypy; fails on any error not listed in tools/mypy_baseline.txt (the list may only shrink)
  config     tests/test_config.py and tests/test_ci.py: policy, workflows, site policy, dependency pins, .env.example,
             the CI workflow and this tool
  tests      every test, with outbound network refused (tools/offline_tests.py)
  demo       tools/mvp_gate.py in Demo Mode on a throwaway vault; fails on FAIL, or on any item graded lower than in
             evidence/functional_mvp.json
  security   tools/release_scan.py (any secret fails; values are never printed) and pip-audit of both locks
  mutations  (--full only) tools/mutate.py: every planted defect must be caught

Every check runs apart from your private data: CAREEROS_VAULT points at an empty temporary folder, and the Demo Mode
variables, ANTHROPIC_API_KEY and GitHub tokens are removed. No check needs a credential or reaches a job board.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "mypy_baseline.txt"
RECEIPT = ROOT / "evidence" / "functional_mvp.json"
LOCKS = ("requirements.lock", "requirements-dev.lock")
REMOVED_ENV = ("CAREEROS_DEMO_MODE", "CAREEROS_DEMO_VAULT", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
FORMAT_RULES = "W,E101,E501"
STEPS = ("lint", "format", "types", "config", "tests", "demo", "security", "mutations")
GRADE_ORDER = {"FAIL": 0, "PARTIAL": 1, "PASS": 2}
INSTALL_HINT = "the dev tools are missing: install requirements-dev.lock into .venv-dev (the command is in that file)"


@dataclass
class Outcome:
    ok: bool
    detail: str
    output: str = ""          # shown only when the check fails


def run(cmd: list[str], env: dict[str, str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def missing(*modules: str) -> str | None:
    gone = [m for m in modules if importlib.util.find_spec(m) is None]
    return f"{', '.join(gone)}: {INSTALL_HINT}" if gone else None


def found_count(output: str) -> int:
    match = re.search(r"Found (\d+) error", output)
    return int(match.group(1)) if match else 0


# --- types: mypy against its baseline -----------------------------------------------------------------------------
def type_errors(output: str) -> Counter[str]:
    """mypy errors as 'path: message  [code]', without line numbers, so edits elsewhere in a file change nothing."""
    errors: Counter[str] = Counter()
    for line in output.splitlines():
        match = re.match(r"^(.+?\.py):\d+(?::\d+)?: error: (.+)$", line.strip())
        if match:
            errors[f"{match.group(1).replace(chr(92), '/')}: {match.group(2).strip()}"] += 1
    return errors


def read_baseline(text: str) -> Counter[str]:
    return Counter(line for line in text.splitlines() if line.strip() and not line.startswith("#"))


def compare_types(baseline: Counter[str], found: Counter[str]) -> tuple[list[str], int]:
    """(errors not in the baseline, number of baseline errors now fixed)."""
    return sorted((found - baseline).elements()), sum((baseline - found).values())


def baseline_text(found: Counter[str]) -> str:
    return ("# mypy errors present on 2026-10-13 (Portfolio Prompt 10), without line numbers. tools/validate.py fails on any\n"
            "# error not listed here. Fix them over time; never add to this list to make a new error pass.\n"
            "# Rewrite after fixing some: .venv-dev\\Scripts\\python.exe tools\\validate.py --only types --update-mypy-baseline\n"
            + "".join(f"{line}\n" for line in sorted(found.elements())))


# --- demo: the MVP gate against the committed receipt -------------------------------------------------------------
def grade_drops(committed: dict[str, str], now: dict[str, str]) -> list[str]:
    """Items graded lower now than in the committed receipt (an item that disappeared counts as a drop)."""
    return [f"{item}: {grade} -> {now.get(item, 'missing')}" for item, grade in committed.items()
            if GRADE_ORDER.get(now.get(item, "FAIL"), 0) < GRADE_ORDER.get(grade, 0)]


# --- the checks ---------------------------------------------------------------------------------------------------
def check_lint(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    if gone := missing("ruff"):
        return Outcome(False, gone)
    done = run([sys.executable, "-m", "ruff", "check", "--no-cache", "--ignore", FORMAT_RULES, "--output-format", "concise", "."], env)
    return Outcome(done.returncode == 0, "no findings" if done.returncode == 0 else f"{found_count(done.stdout)} findings", done.stdout + done.stderr)


def check_format(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    if gone := missing("ruff"):
        return Outcome(False, gone)
    done = run([sys.executable, "-m", "ruff", "check", "--no-cache", "--select", FORMAT_RULES, "--output-format", "concise", "."], env)
    return Outcome(done.returncode == 0, "no findings" if done.returncode == 0 else f"{found_count(done.stdout)} findings", done.stdout + done.stderr)


def check_types(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    if gone := missing("mypy"):
        return Outcome(False, gone)
    cache = Path(tempfile.gettempdir()) / "careeros-mypy-cache"     # outside the repository
    done = run([sys.executable, "-m", "mypy", "--cache-dir", str(cache), "--no-error-summary", "--no-pretty"], env)
    found = type_errors(done.stdout)
    if done.returncode not in (0, 1) or (done.returncode == 1 and not found):
        return Outcome(False, f"mypy could not run (exit {done.returncode})", done.stdout + done.stderr)
    if args.update_mypy_baseline:
        BASELINE.write_text(baseline_text(found), encoding="utf-8")
        return Outcome(True, f"baseline rewritten: {sum(found.values())} errors")
    new, fixed = compare_types(read_baseline(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else Counter(), found)
    detail = f"{sum(found.values())} errors, all in the baseline" if not new else f"{len(new)} new errors (not in tools/mypy_baseline.txt)"
    if fixed:
        detail += f"; {fixed} baseline errors fixed (shrink it with --update-mypy-baseline)"
    return Outcome(not new, detail, "\n".join(new))


def check_config(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    done = run([sys.executable, "-m", "unittest", "tests.test_config", "tests.test_ci"], env)
    ran = re.search(r"Ran (\d+) tests?", done.stderr)
    return Outcome(done.returncode == 0, f"{ran.group(1) if ran else '?'} checks", done.stderr[-6000:])


def check_tests(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    done = run([sys.executable, str(ROOT / "tools" / "offline_tests.py")], env)
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", done.stderr)
    status = (re.findall(r"^(OK.*|FAILED.*)$", done.stderr, re.M) or ["no result"])[-1]
    detail = f"{ran.group(1)} tests, {status}" if ran else status
    failures = done.stderr[done.stderr.find("=" * 70):] if "=" * 70 in done.stderr else done.stderr[-6000:]
    return Outcome(done.returncode == 0, detail + ", network refused", failures)


def check_demo(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    receipt = scratch / "functional_mvp.json"
    done = run([sys.executable, str(ROOT / "tools" / "mvp_gate.py"), "--receipt", str(receipt)], env)
    if not receipt.exists():
        return Outcome(False, f"the MVP gate wrote no result (exit {done.returncode})", (done.stdout + done.stderr)[-6000:])
    result = json.loads(receipt.read_text(encoding="utf-8"))
    committed = json.loads(RECEIPT.read_text(encoding="utf-8"))["grades"] if RECEIPT.exists() else {}
    drops = grade_drops(committed, result["grades"])
    counts = Counter(result["grades"].values())
    checks = sum(len(v) for v in result["checks"].values())
    detail = f"{result['overall']} ({counts['PASS']} PASS, {counts['PARTIAL']} PARTIAL, {counts['FAIL']} FAIL; {checks} checks)"
    if drops:
        detail += f"; lower than evidence/functional_mvp.json: {'; '.join(drops)}"
    return Outcome(result["overall"] != "FAIL" and not drops, detail, done.stdout[-6000:])


def check_security(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    done = run([sys.executable, str(ROOT / "tools" / "release_scan.py"), "--json"], env)
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from release_scan import SECRET_TYPES
        findings = json.loads(done.stdout)["findings"]
    except (ValueError, KeyError, ImportError) as exc:
        return Outcome(False, f"release scan did not run: {type(exc).__name__}", done.stderr[-4000:])
    secrets = sorted(k for k in findings if k in SECRET_TYPES)
    others = sorted(k for k in findings if k not in SECRET_TYPES)
    parts = [f"secrets: {', '.join(secrets)} (see tools/release_scan.py output)" if secrets else "no secrets",
             f"{len(others)} other kinds for the public-release checklist" if others else "nothing else found"]
    ok, output = not secrets, ""
    if args.offline:
        parts.append("pip-audit skipped (--offline)")
    elif gone := missing("pip_audit"):
        ok, output = False, gone
        parts.append("pip-audit missing")
    else:
        for lock in LOCKS:
            audit = run([sys.executable, "-m", "pip_audit", "--disable-pip", "--no-deps", "--progress-spinner", "off", "-r", lock], env, timeout=600)
            pins = sum(1 for line in (ROOT / lock).read_text(encoding="utf-8").splitlines() if re.match(r"^[A-Za-z0-9]", line))
            if audit.returncode == 0:
                parts.append(f"{lock}: {pins} pins, no known vulnerabilities")
            else:
                ok = False
                parts.append(f"{lock}: pip-audit failed (exit {audit.returncode}; --offline skips it when PyPI cannot be reached)")
                output += audit.stdout + audit.stderr
    return Outcome(ok, "; ".join(parts), output)


def check_mutations(env: dict[str, str], scratch: Path, args: argparse.Namespace) -> Outcome:
    done = run([sys.executable, str(ROOT / "tools" / "mutate.py"), "--workers", str(args.workers)], env, timeout=4 * 3600)
    caught = re.search(r"(\d+)/(\d+) mutations caught", done.stdout)
    survived = [line for line in done.stdout.splitlines() if line.startswith(("SURVIVED", "ANCHOR"))]
    return Outcome(done.returncode == 0, caught.group(0) if caught else f"no result (exit {done.returncode})", "\n".join(survived) or done.stdout[-4000:])


CHECKS: dict[str, Callable[[dict[str, str], Path, argparse.Namespace], Outcome]] = {
    "lint": check_lint, "format": check_format, "types": check_types, "config": check_config, "tests": check_tests,
    "demo": check_demo, "security": check_security, "mutations": check_mutations}


def isolated_env(scratch: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in REMOVED_ENV}
    env.update(CAREEROS_VAULT=str(scratch / "no-private-vault"), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def describe_commit() -> str:
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "commit unknown"
    return f"commit {commit or 'unknown'}{' with uncommitted changes' if dirty else ''}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareerOS validation: the same checks as CI")
    parser.add_argument("--only", help=f"comma-separated checks to run ({', '.join(STEPS)})")
    parser.add_argument("--offline", action="store_true", help="skip pip-audit, the only check that needs the internet")
    parser.add_argument("--full", action="store_true", help="also run every planted defect (tools/mutate.py)")
    parser.add_argument("--workers", type=int, default=4, help="parallel mutation runs with --full (default 4)")
    parser.add_argument("--update-mypy-baseline", action="store_true", help="rewrite tools/mypy_baseline.txt from this run")
    args = parser.parse_args(argv)
    chosen = [s.strip() for s in args.only.split(",")] if args.only else [s for s in STEPS if s != "mutations" or args.full]
    unknown = [s for s in chosen if s not in CHECKS]
    if unknown:
        parser.error(f"unknown checks: {', '.join(unknown)} (choose from {', '.join(STEPS)})")
    if sys.version_info[:2] != (3, 12):
        print(f"note: CareerOS targets Python 3.12; this is {platform.python_version()}")
    print(f"CareerOS validation: {describe_commit()}; Python {platform.python_version()} on {platform.system()}", flush=True)
    scratch = Path(tempfile.mkdtemp(prefix="careeros-validate-"))
    results: dict[str, Outcome] = {}
    try:
        env = isolated_env(scratch)
        for name in chosen:
            started = time.monotonic()
            try:
                outcome = CHECKS[name](env, scratch, args)
            except subprocess.TimeoutExpired as exc:
                outcome = Outcome(False, f"timed out after {exc.timeout:.0f}s")
            results[name] = outcome
            print(f"  {'PASS' if outcome.ok else 'FAIL'}  {name:<10}{time.monotonic() - started:7.1f}s  {outcome.detail}", flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    failed = [name for name, outcome in results.items() if not outcome.ok]
    for name in failed:
        print(f"\n--- {name} ---\n{results[name].output.strip() or '(no output)'}")
    print(f"\nValidation: {'FAIL (' + ', '.join(failed) + ')' if failed else 'PASS'} ({len(results)} checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
