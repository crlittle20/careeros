"""docs/defect_coverage.md from evidence/mutation-receipt.json: every planted defect, its result and the tests that caught it.

    .venv-dev\\Scripts\\python.exe tools\\defect_coverage.py            # rewrite docs/defect_coverage.md from the receipt
    .venv-dev\\Scripts\\python.exe tools\\defect_coverage.py --check    # exit 1 when the committed page is not what the receipt gives

Read-only over the receipt that tools/mutate.py writes; this tool runs no test. The page depends on the receipt alone
(no generation time, nothing from the environment), so writing it twice gives the same bytes, and --check compares the
committed page with what it would write. tests/test_portfolio_docs.py runs that check. The plain-language account of
planted defects, the run history and worked examples are in docs/testing.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "evidence" / "mutation-receipt.json"
PAGE = ROOT / "docs" / "defect_coverage.md"
SHOWN = 3                  # test names per defect; the receipt itself keeps at most three (tools/mutate.py, suite())
MARKUP = "\\|<>`*[]"       # characters in a defect's label that Markdown would read as markup inside a table cell
WORDS = {1: "one", 2: "two", 3: "three"}


def load(path: Path = RECEIPT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def failed_tests(entry: dict) -> int:
    """How many failure and error records the suite counted under this defect, never fewer than the names kept. A record is
    not a test: one test method with subtests can fail more than once (Codex review CR-2026-10-31, F1)."""
    return max((entry.get("failures") or 0) + (entry.get("errors") or 0), len(entry.get("failing") or []))


def methods(entry: dict) -> list[str]:
    """The distinct test methods among the failure names the receipt kept, in the receipt's order."""
    return list(dict.fromkeys(entry.get("failing") or []))


def complete(entry: dict) -> bool:
    """Whether the receipt kept a name for every failure and error record (it keeps at most three)."""
    return failed_tests(entry) == len(entry.get("failing") or [])


def single_method(entry: dict) -> str:
    """'yes' when every record names one and the same test method; 'unknown' when the kept names are one method but the
    receipt dropped some records, so another method may have failed too; '' otherwise."""
    if entry.get("result") != "CAUGHT" or len(methods(entry)) != 1:
        return ""
    return "yes" if complete(entry) else "unknown"


def single_counts(results: list[dict]) -> tuple[int, int]:
    """(defects caught by exactly one test method, defects where the receipt cannot tell)."""
    marks = Counter(single_method(e) for e in results)
    return marks["yes"], marks["unknown"]


def cell(label: str) -> str:
    return "".join(f"\\{c}" if c in MARKUP else c for c in label)


def cite(name: str) -> str:
    """A test as the documents cite it, module::test; the receipt's full name (tests.module.Class.test) also holds the class."""
    parts = name.split(".")
    module = next((p for p in parts if p.startswith("test_")), parts[0])
    return f"{module}::{parts[-1]}" if len(parts) > 1 else name


def catching(entry: dict) -> str:
    """The distinct tests among the kept names, up to SHOWN, and the suite's own record count when it says more."""
    names = methods(entry)
    if not names:
        return "none"
    shown = ", ".join(f"`{cite(name)}`" for name in names[:SHOWN])
    records = failed_tests(entry)
    return shown + (f" ({records} failure/error records)" if records != len(names[:SHOWN]) else "")


def environment(receipt: dict) -> str:
    parts = []
    if receipt.get("python"):
        parts.append(f"Python {receipt['python']}")
    if receipt.get("platform"):
        parts.append(str(receipt["platform"]))
    if receipt.get("node_version"):
        parts.append(f"Node.js {receipt['node_version']}")
    if receipt.get("workers"):
        parts.append(f"{receipt['workers']} workers")
    return ", ".join(parts)


def run_rows(receipt: dict, results: list[dict], single: tuple[int, int]) -> list[tuple[str, str]]:
    """The header table: only what this receipt records (older receipts hold less)."""
    by_result = receipt.get("by_result") or dict(Counter(e.get("result", "?") for e in results))
    rows = [("Run finished", str(receipt.get("at", "not recorded")))]
    if receipt.get("commit"):
        rows.append(("Commit", f"`{receipt['commit']}`"))
    if receipt.get("tree"):
        rows.append(("Source tree", f"`{receipt['tree']}` (SHA-256 over every file of the tree the run accepted)"))
    rows.append(("Planted defects", str(receipt.get("total", len(results)))))
    rows.append(("Caught", f"{receipt.get('caught', by_result.get('CAUGHT', 0))} ("
                 + ", ".join(f"{k} {v}" for k, v in sorted(by_result.items())) + ")"))
    rows.append(("Source files with a planted defect", str(len({e["file"] for e in results}))))
    exact, unknown = single
    rows.append(("Caught by exactly one test method", f"{exact} (a single point of detection), counted where the receipt kept a name "
                 f"for every failure record; {unknown} more cannot be told, because the receipt dropped some of their records"))
    baseline = receipt.get("baseline") or {}
    if baseline.get("ran"):
        rows.append(("Unmutated suite", f"{baseline['ran']} tests ran in {baseline.get('seconds', '?')} s before the first defect went in"))
    if environment(receipt):
        rows.append(("Environment", environment(receipt)))
    if receipt.get("carried_over"):
        rows.append(("Carried over", f"{receipt['carried_over']} results from an earlier stopped run on the same tree and environment"))
    return rows


def render(receipt: dict) -> str:
    results = list(receipt.get("results") or [])
    by_file: dict[str, list[dict]] = {}
    for entry in results:
        by_file.setdefault(entry["file"], []).append(entry)
    single = single_counts(results)
    lines = [
        "# Planted-defect coverage",
        "",
        "Generated by `tools/defect_coverage.py` from `evidence/mutation-receipt.json`; do not edit by hand (`--check` fails when",
        "this page is out of date). `tools/mutate.py` breaks one safety rule at a time in a scratch copy of the engine and runs the",
        "whole test suite; a defect is CAUGHT only when a real test failed or errored with the whole suite loaded. What a planted",
        "defect is, the run history and ten worked examples are in [testing.md](testing.md).",
        "",
        "## The run",
        "",
        "| | |",
        "|---|---|",
        *[f"| {name} | {value} |" for name, value in run_rows(receipt, results, single)],
        "",
        "One table per source file, in path order; the defects keep the receipt's order. \"Catching tests\" lists the distinct tests",
        f"among the failure names the receipt keeps (at most {WORDS.get(SHOWN, str(SHOWN))} per defect), written module::test as the other",
        "documents cite them; \"(N failure/error records)\" is the suite's own count when it differs from the names shown. A record",
        "is not a test: one test method with subtests can fail more than once. \"One test only\" is \"yes\" when every record names",
        "the same test method (weaken that test and the defect would survive), and \"unknown\" when the kept names are one method",
        "but the receipt dropped some records, so another method may have failed too.",
        "",
    ]
    for file in sorted(by_file):
        entries = by_file[file]
        caught = sum(1 for e in entries if e.get("result") == "CAUGHT")
        lines += [f"## `{file}`", "",
                  f"{len(entries)} planted defect{'s' if len(entries) != 1 else ''}, {caught} caught.", "",
                  "| Planted defect | Result | Catching tests | One test only |", "|---|---|---|---|"]
        for entry in entries:
            lines.append(f"| {cell(entry['mutation'])} | {entry.get('result', '?')} | {catching(entry)} | {single_method(entry)} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write docs/defect_coverage.md from the planted-defect receipt")
    parser.add_argument("--receipt", type=Path, default=RECEIPT, help="the receipt tools/mutate.py wrote (default: evidence/mutation-receipt.json)")
    parser.add_argument("--out", type=Path, default=PAGE, help="the page to write or check (default: docs/defect_coverage.md)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when the page differs from what the receipt gives")
    args = parser.parse_args(argv)
    receipt = load(args.receipt)
    page = render(receipt)
    summary = f"{receipt.get('caught', '?')} of {receipt.get('total', '?')} planted defects caught in {len({e['file'] for e in receipt.get('results', [])})} files"
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != page:
            print(f"{args.out.name} is {'missing' if current is None else 'out of date'}: run tools/defect_coverage.py to rewrite it from {args.receipt.name}")
            return 1
        print(f"{args.out.name} matches {args.receipt.name}: {summary}")
        return 0
    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        out.write(page)
    print(f"Wrote {args.out.name}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
