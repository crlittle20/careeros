"""Hand check (efficiency review of 2026-11-16, item 3): one repository tool in place of the per-batch copies.

It plants a chosen set of the defects listed in tools/mutate.py, one at a time, in scratch copies of this tree as it
is (uncommitted changes included). For each defect it runs first the tests most likely to catch it (D152's stages), and
the whole suite only when those miss.

Choosing the defects (combine them; give at least one of --base, --files, --labels or --all):
    --base REV     every defect that REV's tools/mutate.py doesn't list (new or re-anchored since REV), and every defect
                   in a file changed since REV, committed or not
    --files A,B    every defect in these files
    --labels FILE  only the defects whose label contains one of FILE's lines (alone: exactly those defects)
    --all          every defect: a staged full campaign. This is NOT the release evidence standard. tools/mutate.py's
                   full run stays that until the owner adopts staged campaigns (efficiency review, item 2).

The stages, for each defect:
    prior    the tests that caught this same defect in earlier receipts (evidence/mutation-receipt.json and
             evidence/mutation-hand-check*.json, plus any --prior files)
    modules  the test modules that caught any defect in the same file in those receipts, plus --modules
    suite    the whole suite (only for a defect the earlier stages missed)

CAUGHT follows tools/mutate.py's rule at every stage:
- a real test failed or errored;
- every named test loaded;
- the run counted at least as many tests as the same names count in the unmutated tree;
- in the prior and modules stages, the whole suite still loads with all its tests, as a full run requires.
A failure of a --flaky test alone is never a catch: the defect goes on to the next stage. TIMEOUT, LOAD_ERROR,
INFRA_ERROR, ANCHOR_NOT_FOUND and SLEPT are never caught. Before any defect, the unmutated copy must pass the whole
suite. A selection of tests (a stage's names) never names a test twice: a test whose class or module is named too is
left out, since a test run twice in one process can fail on its own leftovers. Before a prior or modules stage counts
for a defect, the same selection must pass on the unmutated copy (each selection is checked once): a selection that
fails without the defect is skipped, so a test that fails only outside its usual company never makes a catch.

Each worker keeps one scratch copy. Before every defect, it hashes the copy against the tree the check started on and
rebuilds the copy if a test changed it, so every defect runs on exactly that tree. At the end it checks the tree and
the environment (Python, its packages, Node.js) again; if either changed, no receipt is written. If the machine sleeps
during a defect, that defect runs again (up to SLEEP_ROUNDS times).

Results go to OUT.jsonl as they finish. Running the same command again carries over the defects already CAUGHT on the
same tree in the same environment. At the end OUT.json is written, in the shape of evidence/mutation-hand-check-*.json.
The tests run below normal priority. To stop a run, create careeros-hand-check.stop in the temp folder; the same
command resumes it.

    .venv-dev\\Scripts\\python.exe tools\\hand_check.py OUT --base 9b0502e [--modules tests.test_x] [--workers 2]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate  # noqa: E402

STOP_FILE = "careeros-hand-check.stop"   # in the temp folder; a run stops before its next defect and resumes later
PRIOR_PER_DEFECT = 12                    # at most this many earlier catching tests run first for one defect
SLEEP_TOLERANCE = mutate.SLEEP_TOLERANCE
SLEEP_ROUNDS = mutate.SLEEP_ROUNDS
STAGES = ("prior", "modules", "suite")

_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]*)\)", re.M)
_COUNT = (
    "import json, sys, unittest\n"
    "loader = unittest.TestLoader()\n"
    "counts = {}\n"
    "for name in json.loads(sys.stdin.read()):\n"
    "    loader.errors.clear()\n"
    "    try:\n"
    "        found = loader.loadTestsFromName(name)\n"
    "        counts[name] = None if loader.errors else found.countTestCases()\n"
    "    except Exception:\n"
    "        counts[name] = None\n"
    "print(json.dumps(counts))\n"
)
_RUN = (   # unittest's own command line, with the test names read from stdin: any number of them fits
    "import json, sys, unittest\n"
    "unittest.main(module=None, argv=['unittest', *json.loads(sys.stdin.read())])\n"
)
_LOADS = (
    "import unittest\n"
    "loader = unittest.TestLoader()\n"
    "found = loader.discover('tests', top_level_dir='.')\n"
    "print(len(loader.errors), found.countTestCases())\n"
)


class Stopped(Exception):
    """The run stopped before its receipt; the defects already finished stay in OUT.jsonl."""


class TreeChanged(Stopped):
    """The tree the check started on changed: a copy could not be made to match it."""


class EnvironmentChanged(Stopped):
    """Python, its packages or Node.js changed during the run."""


class StopRequested(Stopped):
    """STOP_FILE appeared."""


# --- choosing the defects --------------------------------------------------------------------------------------------

def listed(source: str) -> list[tuple[str, str, str, str]]:
    """The MUTATIONS list of a tools/mutate.py text (an older commit's, read with git show)."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "MUTATIONS" for target in node.targets):
            return [tuple(item) for item in ast.literal_eval(node.value)]
    raise ValueError("no MUTATIONS list in that text")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def changed_files(root: Path, base: str) -> set[str]:
    """Files that differ from `base` in this working tree: committed, staged, unstaged and untracked."""
    diff = git(root, "diff", "--name-only", base).splitlines()
    untracked = git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return {line.strip() for line in [*diff, *untracked] if line.strip()}


def choose(mutations: list, base_list: list | None = None, changed: set[str] | frozenset[str] = frozenset(),
           files: set[str] | frozenset[str] = frozenset(), labels: list[str] | None = None,
           everything: bool = False) -> list[tuple[int, str]]:
    """(index, kind) for every chosen defect. kind is "new" for a defect `base_list` doesn't have, else "shared"."""
    before = {tuple(item) for item in base_list or ()}
    selectors = everything or base_list is not None or bool(files)
    picked = []
    for index, item in enumerate(mutations):
        new = base_list is not None and tuple(item) not in before
        hit = everything or new or item[0] in changed or item[0] in files
        if labels is not None:
            match = any(word in item[3] for word in labels)
            hit = (hit and match) if selectors else match
        if hit:
            picked.append((index, "new" if new else "shared"))
    return picked


# --- what earlier receipts say ---------------------------------------------------------------------------------------

def default_receipts(root: Path) -> list[Path]:
    """The hand-check receipts in evidence/ (newest name first), then the last full run's receipt."""
    evidence = root / "evidence"
    found = sorted(evidence.glob("mutation-hand-check*.json"), reverse=True)
    full = evidence / "mutation-receipt.json"
    return [*found, *([full] if full.exists() else [])]


def module_of(name: str) -> str:
    """tests.test_x.Class.test_y -> tests.test_x"""
    return ".".join(name.split(".")[:2])


def prior_catches(receipts: list[dict]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """By defect label: the tests that caught it. By file: the test modules that caught any defect in it. Earlier
    receipts in the list come first."""
    by_label: dict[str, list[str]] = {}
    by_file: dict[str, list[str]] = {}
    for receipt in receipts:
        for row in receipt.get("results") or []:
            if not isinstance(row, dict) or row.get("result") != "CAUGHT":
                continue
            names = [n for n in row.get("failing") or [] if isinstance(n, str) and re.fullmatch(r"tests\.\w+(?:\.\w+)*", n)]
            label, file = row.get("mutation"), row.get("file")
            if isinstance(label, str):
                known = by_label.setdefault(label, [])
                known += [n for n in names if n not in known]
            if isinstance(file, str):
                modules = by_file.setdefault(file, [])
                modules += [m for m in dict.fromkeys(module_of(n) for n in names) if m not in modules]
    return by_label, by_file


def tidy(names: list[str]) -> list[str]:
    """The names once each, in order, without a test whose class or module is also named: unittest would run it twice,
    and a test run twice in one process can fail on its own leftovers (the first --all campaign, 2026-11-17)."""
    unique = list(dict.fromkeys(names))
    named = set(unique)
    return [n for n in unique if not any(".".join(n.split(".")[:i]) in named for i in range(1, n.count(".") + 1))]


def stage_names(label: str, file: str, by_label: dict, by_file: dict, extra_modules: list[str], loadable: set[str]) -> dict:
    """The names each stage runs for one defect; a name that no longer loads is left out (the suite stage still runs)."""
    prior = tidy([n for n in by_label.get(label, []) if n in loadable])[:PRIOR_PER_DEFECT]
    modules = tidy([m for m in [*by_file.get(file, []), *extra_modules] if m in loadable])
    return {"prior": prior, "modules": modules, "suite": None}


# --- running tests ---------------------------------------------------------------------------------------------------

def _low_priority() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)}
    return {"preexec_fn": lambda: os.nice(5)}


def summary(text: str, exit_code: int | None, seconds: float, flaky: frozenset[str] = frozenset()) -> dict:
    """unittest's own summary, read as tools/mutate.py reads it, plus whether only --flaky tests failed."""
    ran = re.search(r"^Ran (\d+) tests? in", text, re.M)
    counts = {k: int(v) for k, v in re.findall(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", text)}
    failing = _FAILED.findall(text)
    load = [where for _, where in failing if "_FailedTest" in where]
    named = [where for _, where in failing if "_FailedTest" not in where]
    real = [where for where in named if where not in flaky and where.rsplit(".", 1)[-1] not in flaky]
    return {"exit": exit_code, "timed_out": False, "seconds": seconds, "ran": int(ran.group(1)) if ran else None,
            "failures": counts.get("failures", 0), "errors": counts.get("errors", 0), "skipped": counts.get("skipped", 0),
            "load_errors": len(load), "failing": real[:3], "flaky_only": bool(named) and not real}


def run_tests(folder: Path, names: list[str] | None, flaky: frozenset[str] = frozenset(),
              timeout: float = mutate.MUTATION_TIMEOUT) -> dict:
    """Run the named tests (None: the whole suite) in `folder`, below normal priority. Never raises on a timeout. The
    names go in on stdin: a whole campaign's names are far longer than a Windows command line may be (32,767 characters)."""
    command = [sys.executable, "-B", "-c", _RUN]
    arguments = json.dumps(names or ["discover", "-s", "tests", "-t", "."])
    started = time.monotonic()
    try:
        done = subprocess.run(command, cwd=folder, input=arguments, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, **_low_priority())
    except subprocess.TimeoutExpired:
        return {"exit": None, "timed_out": True, "seconds": round(time.monotonic() - started, 1)}
    return summary(done.stderr + done.stdout, done.returncode, round(time.monotonic() - started, 1), flaky)


def verdict(outcome: dict, expected_ran: int | None) -> str:
    """mutate.classify, except that a failure of --flaky tests alone is never a catch."""
    if outcome.get("flaky_only") and not outcome.get("load_errors") and not outcome.get("timed_out"):
        return "SURVIVED"
    return mutate.classify(outcome, expected_ran)


def passes(outcome: dict) -> bool:
    """An unmutated run is healthy: it passed, or only --flaky tests failed (and everything loaded)."""
    return not outcome.get("timed_out") and outcome.get("ran") is not None and not outcome.get("load_errors") \
        and (outcome["exit"] == 0 or bool(outcome.get("flaky_only")))


def suite_loads(folder: Path, suite_ran: int) -> bool:
    """True when every test module of the whole suite still imports in `folder` and it holds as many tests as the
    unmutated suite ran. A full run counts a catch only with the whole suite loaded, so a stage that runs fewer tests
    checks this too: a defect that breaks an import elsewhere is never a catch."""
    try:
        done = subprocess.run([sys.executable, "-B", "-c", _LOADS], cwd=folder, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600, **_low_priority())
        errors, count = (int(x) for x in done.stdout.strip().splitlines()[-1].split())
    except (subprocess.SubprocessError, ValueError, IndexError):
        return False
    return done.returncode == 0 and errors == 0 and count == suite_ran


def test_counts(folder: Path, names: list[str]) -> dict[str, int | None]:
    """How many tests each name loads in `folder` (None: it doesn't load)."""
    if not names:
        return {}
    done = subprocess.run([sys.executable, "-B", "-c", _COUNT], cwd=folder, input=json.dumps(names), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


# --- scratch copies --------------------------------------------------------------------------------------------------

def make_copy(root: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns(*mutate.COPY_IGNORE))


def ready_copy(root: Path, copy: Path, tree: str) -> bool:
    """True when `copy` is exactly `tree`; a copy a test changed is rebuilt once. False: the tree itself changed."""
    if mutate.tree_digest(copy) == tree:
        return True
    make_copy(root, copy)
    return mutate.tree_digest(copy) == tree


class CleanSelections:
    """Whether each selection of tests passes on the unmutated tree, run once per selection and remembered."""

    def __init__(self, flaky: frozenset[str]):
        self.flaky, self.known, self.lock = flaky, {}, threading.Lock()

    def passes(self, copy: Path, names: list[str]) -> bool:
        key = tuple(names)
        with self.lock:
            if key in self.known:
                return self.known[key]
        clean = passes(run_tests(copy, names, self.flaky))
        with self.lock:
            self.known[key] = clean
        return clean

    def failing(self) -> list[list[str]]:
        return [list(key) for key, clean in self.known.items() if not clean]


def check_one(job: dict, copy: Path, root: Path, tree: str, flaky: frozenset[str], expected: dict, suite_ran: int,
              clean: CleanSelections | None = None) -> dict:
    """Plant one defect in `copy`, run its stages until one catches it, and put the file back byte for byte. A prior or
    modules selection that fails on the unmutated copy is skipped (`clean`)."""
    clean = clean or CleanSelections(flaky)
    if not ready_copy(root, copy, tree):
        raise TreeChanged(job["mutation"])
    usable = {stage: bool(names) and clean.passes(copy, names) for stage, names in job["stages"].items() if stage != "suite"}
    if not ready_copy(root, copy, tree):   # the unmutated selections may have left files in the copy
        raise TreeChanged(job["mutation"])
    path = copy / job["file"]
    original = path.read_bytes()
    text = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")   # as tools/mutate.py reads it
    if text != job["source"]:
        raise TreeChanged(job["mutation"])
    stages: list[dict] = []
    result, outcome, scope = "INFRA_ERROR", {}, None
    path.write_bytes(text.replace(job["old"], job["new"]).encode("utf-8"))
    try:
        for stage in STAGES:
            names = job["stages"][stage]
            if stage != "suite" and not names:
                continue
            if stage != "suite" and not usable[stage]:
                stages.append({"stage": stage, "tests": len(names), "result": "SKIPPED: fails unmutated"})
                continue
            outcome = run_tests(copy, names, flaky)
            want = suite_ran if stage == "suite" else sum(expected[n] or 0 for n in names)
            result, scope = verdict(outcome, want), stage
            if result == "CAUGHT" and stage != "suite" and not suite_loads(copy, suite_ran):
                result = "LOAD_ERROR"   # caught by a few tests, but the whole suite no longer loads: not proof
            stages.append({"stage": stage, "tests": len(names) if names else "all", "result": result,
                           "ran": outcome.get("ran"), "seconds": outcome.get("seconds")})
            if result == "CAUGHT":
                break
    finally:
        path.write_bytes(original)
    if path.read_bytes() != original:
        raise TreeChanged(job["mutation"])
    return {"mutation": job["mutation"], "file": job["file"], "kind": job["kind"], "result": result, "scope": scope,
            **{k: outcome.get(k) for k in ("ran", "failures", "errors", "failing", "seconds")}, "stages": stages}


# --- the run ---------------------------------------------------------------------------------------------------------

def carried_over(path: Path, key: dict, mutations: list) -> dict[int, dict]:
    """The defects an earlier run with this OUT already CAUGHT on this tree, in this environment, without a sleep."""
    kept: dict[int, dict] = {}
    if not path.exists():
        return kept
    for line in path.read_bytes().splitlines():
        try:
            entry = json.loads(line)
            index, record = entry["index"], entry["result"]
            same = entry.get("tree") == key["tree"] and entry.get("env") == key["env"]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not (same and isinstance(index, int) and 0 <= index < len(mutations) and isinstance(record, dict)):
            continue
        if record.get("mutation") == mutations[index][3] and record.get("result") == "CAUGHT" and not record.get("slept"):
            kept[index] = {**record, "carried_over": True}
        else:
            kept.pop(index, None)
    return kept


def describe(args: argparse.Namespace) -> str:
    parts = []
    if args.all:
        parts.append("every defect (a staged full campaign)")
    if args.base:
        parts.append(f"every defect new or re-anchored since {args.base}, and every defect in a file changed since it")
    if args.files:
        parts.append(f"every defect in {args.files}")
    if args.labels:
        parts.append(f"only defects whose label contains a line of {Path(args.labels).name}" if (args.all or args.base or args.files)
                     else f"the defects whose label contains a line of {Path(args.labels).name}")
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareerOS hand check: chosen planted defects, tests most likely to catch them first")
    parser.add_argument("out", help="OUT: writes OUT.jsonl as it goes and OUT.json at the end")
    parser.add_argument("--base", help="a commit: defects new since it, and defects in files changed since it")
    parser.add_argument("--files", default="", help="comma-separated source files: every defect in them")
    parser.add_argument("--labels", help="a file of label substrings, one a line")
    parser.add_argument("--all", action="store_true", help="every defect (a staged full campaign; not the release standard)")
    parser.add_argument("--modules", default="", help="comma-separated test modules added to every defect's modules stage")
    parser.add_argument("--prior", action="append", default=[], help="another receipt whose catches run first (repeatable)")
    parser.add_argument("--flaky", action="append", default=[], help="a test (name or full id) whose failure alone is never a catch")
    parser.add_argument("--workers", type=int, default=2, help="defects planted at once, each worker in its own copy")
    parser.add_argument("--what", default="", help="one sentence for the receipt: what this hand check is for")
    args = parser.parse_args(argv)
    if not (args.base or args.files or args.labels or args.all):
        parser.error("choose the defects: --base, --files, --labels or --all")
    root = mutate.ROOT
    mutations = list(mutate.MUTATIONS)
    flaky = frozenset(args.flaky)
    started = time.time()
    jsonl, final = Path(args.out + ".jsonl"), Path(args.out + ".json")
    if jsonl.resolve().is_relative_to(root.resolve()):
        print("OUT must be outside the tree: results written into it would change the tree the check runs on. Copy the"
              " receipt into evidence/ afterwards.")
        return 2

    env = mutate.environment()
    files = mutate.tree_files(root)
    tree = mutate.tree_digest(files=files)
    drift = mutate.defect_list_drift(files)
    if drift:
        print(f"{drift}: run again once nothing is editing it")
        return 2
    key = mutate.run_key(tree, env)
    base_list = listed(git(root, "show", f"{args.base}:tools/mutate.py")) if args.base else None
    changed = changed_files(root, args.base) if args.base else set()
    labels = [line.strip() for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()] if args.labels else None
    wanted = {f.strip() for f in args.files.split(",") if f.strip()}
    picked = choose(mutations, base_list, changed, wanted, labels, args.all)
    if not picked:
        print("no defect matches that choice")
        return 2

    receipts = []
    for path in [*default_receipts(root), *map(Path, args.prior)]:
        try:
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"receipt {path.name} not read ({type(exc).__name__}); its catches don't run first")
    by_label, by_file = prior_catches(receipts)
    extra = [m.strip() for m in args.modules.split(",") if m.strip()]

    work = Path(tempfile.mkdtemp(prefix="careeros-hc-"))
    try:
        copies = [work / f"w{n}" for n in range(max(1, args.workers))]
        for copy in copies:
            make_copy(root, copy)
            if mutate.tree_digest(copy) != tree:
                print("the tree changed while the copies were made: run again once nothing is editing it")
                return 2
        wanted_names = sorted({*(n for i, _ in picked for n in by_label.get(mutations[i][3], [])),
                               *(m for i, _ in picked for m in by_file.get(mutations[i][0], [])), *extra})
        counts = test_counts(copies[0], wanted_names)
        loadable = {name for name, count in counts.items() if count}
        gone = [name for name in wanted_names if name not in loadable]
        if gone:
            print(f"{len(gone)} test names from earlier receipts no longer load and are left out (for example {gone[0]})")
        jobs, results = [], {}
        for index, kind in picked:
            relative, old, new, label = mutations[index]
            source_path = root / relative
            source = source_path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") if source_path.exists() else ""
            if source.count(old) != 1:
                results[index] = {"mutation": label, "file": relative, "kind": kind, "result": "ANCHOR_NOT_FOUND"}
                continue
            jobs.append({"index": index, "kind": kind, "file": relative, "old": old, "new": new, "mutation": label,
                         "source": source, "stages": stage_names(label, relative, by_label, by_file, extra, loadable)})

        print(f"{len(jobs)} defects to plant ({sum(j['kind'] == 'new' for j in jobs)} new), {len(results)} not found; "
              f"{len(copies)} workers; stop with {Path(tempfile.gettempdir()) / STOP_FILE}", flush=True)
        baseline = {"suite": run_tests(copies[0], None, flaky)}   # each smaller selection is checked before its first use
        print("baseline: " + "; ".join(f"{k} ran {v.get('ran')} exit {v.get('exit')}" for k, v in baseline.items()), flush=True)
        if not all(passes(v) for v in baseline.values()):
            print("the unmutated suite fails in a copy: nothing planted (fix the tree, or name a known-flaky test with --flaky)")
            return 2
        suite_ran = baseline["suite"]["ran"]

        with mutate.only_run(jsonl):
            code = _run(args, root, tree, key, env, mutations, jobs, results, copies, flaky, counts, suite_ran, baseline,
                        jsonl, final, files, started)
        if code == 0:
            jsonl.with_suffix(".lock").unlink(missing_ok=True)   # an empty file; only the lock held on it mattered
        return code
    except mutate.RunInProgress as exc:
        print(f"another hand check writes {jsonl.name} ({exc}); let it finish, or stop it first")
        return 2
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(args, root, tree, key, env, mutations, jobs, results, copies, flaky, counts, suite_ran, baseline, jsonl, final,
         files, started) -> int:
    stop = Path(tempfile.gettempdir()) / STOP_FILE
    try:
        if stop.stat().st_mtime < started - 2:
            stop.unlink()   # left from before this run: meant for an earlier one
    except FileNotFoundError:
        pass
    kept = carried_over(jsonl, key, mutations)
    for job in jobs:
        if job["index"] in kept:
            results[job["index"]] = kept[job["index"]]
    todo = [job for job in jobs if job["index"] not in kept]
    if kept:
        print(f"{len(kept)} defects already CAUGHT on this tree carried over from {jsonl.name}", flush=True)
    free, lock, halt = list(copies), threading.Lock(), threading.Event()
    clean = CleanSelections(flaky)
    done_count = [0]

    def one(job: dict) -> None:
        if halt.is_set():
            return
        if stop.exists():
            halt.set()
            raise StopRequested(job["mutation"])
        with lock:
            copy = free.pop()
        try:
            before = mutate.asleep_seconds()
            record = check_one(job, copy, root, tree, flaky, counts, suite_ran, clean)
            slept = round(mutate.asleep_seconds() - before, 1)
        except Stopped:
            halt.set()
            raise
        finally:
            with lock:
                free.append(copy)
        if mutate.run_key(tree, mutate.environment())["env"] != key["env"]:
            halt.set()
            raise EnvironmentChanged(job["mutation"])
        record["at"] = mutate._now()
        if slept > SLEEP_TOLERANCE:
            record["slept"] = slept
        with lock:
            results[job["index"]] = record
            mutate.append_progress(jsonl, {"index": job["index"], **key, "result": record})
            done_count[0] += 1
            print(f"{done_count[0]:>4}/{len(todo)} {record['result']:<8} {job['kind']:<6} {record.get('scope') or '-':<7} | {job['mutation']}"
                  + (f"  (slept {record['slept']} s)" if "slept" in record else ""), flush=True)

    try:
        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            for future in [pool.submit(one, job) for job in todo]:
                future.result()
            for round_ in range(1, SLEEP_ROUNDS + 1):
                again = [job for job in jobs if results.get(job["index"], {}).get("slept")]
                if not again:
                    break
                print(f"running again {len(again)} defects the machine slept through (round {round_} of {SLEEP_ROUNDS})", flush=True)
                for future in [pool.submit(one, job) for job in again]:
                    future.result()
    except StopRequested:
        stop.unlink(missing_ok=True)
        print(f"\nstopped on request; run the same command again to carry on ({jsonl.name})")
        return 3
    except TreeChanged as exc:
        print(f"\nthe tree changed during the run (first seen with: {exc}); stopped. Put back exactly what the run started on"
              " and run it again: what finished carries over.")
        return 2
    except EnvironmentChanged as exc:
        print(f"\nPython, its packages or Node.js changed during the run (first seen with: {exc}); stopped.")
        return 2
    for job in jobs:
        if results[job["index"]].get("slept"):
            results[job["index"]]["result"] = "SLEPT"

    now = mutate.tree_files(root)
    if mutate.tree_digest(files=now) != tree or mutate.run_key(tree, mutate.environment())["env"] != key["env"]:
        print("\nthe tree or the environment is not what the run started with ("
              + ("; ".join(mutate.tree_changes(files, now)[:10]) or "the environment changed") + "): no receipt written")
        return 2
    ordered = [results[i] for i in sorted(results)]
    caught = sum(r["result"] == "CAUGHT" for r in ordered)
    try:
        rev = git(root, "rev-parse", "HEAD").strip()
        dirty = bool(git(root, "status", "--porcelain").strip())
        base = git(root, "rev-parse", args.base).strip() if args.base else None
    except (OSError, subprocess.CalledProcessError):
        rev, dirty, base = None, None, args.base
    receipt = {
        "tool": "tools/hand_check.py", "at": mutate._now(),
        "what": args.what or "a hand check of chosen planted defects, the tests most likely to catch each first",
        "scope": describe(args), "base": base, "rev": rev, "uncommitted_changes": dirty, "tree": tree,
        "python": sys.version.split()[0], "platform": sys.platform, "node_version": env.get("node_version"),
        "packages": env.get("packages"), "workers": len(copies), "flaky": sorted(flaky),
        "defined": len(mutations), "checked": len(ordered), "caught": caught,
        "by_result": dict(sorted(Counter(r["result"] for r in ordered).items())),
        "by_scope": dict(sorted(Counter(r.get("scope") or "-" for r in ordered if r["result"] == "CAUGHT").items())),
        "carried_over": sum(bool(r.get("carried_over")) for r in ordered),
        "selections_failing_unmutated": clean.failing(),
        "baseline": {k: {f: v.get(f) for f in ("ran", "skipped", "seconds")} for k, v in baseline.items()},
        "mutation_timeout_seconds": mutate.MUTATION_TIMEOUT, "results": ordered,
    }
    final.write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    missed = [r for r in ordered if r["result"] != "CAUGHT"]
    if missed:
        print("\nnot caught:\n" + "\n".join(f"{r['result']:<16} {r['kind']:<6} {r['mutation']} ({r['file']})" for r in missed))
    print(f"\n{caught}/{len(ordered)} caught ({', '.join(f'{k} {v}' for k, v in receipt['by_scope'].items())}) -> {final}")
    if caught == len(ordered):
        jsonl.unlink(missing_ok=True)   # finished: nothing left to resume
    return 0 if caught == len(ordered) else 1


if __name__ == "__main__":
    sys.exit(main())
