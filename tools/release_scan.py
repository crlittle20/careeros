"""Public-release scan (Portfolio Prompt 9, 2026-10-13): what in this repository must not reach a public repository.

    .venv\\Scripts\\python.exe tools\\release_scan.py [--history] [--denylist FILE] [--root DIR] [--json]

It NEVER prints a matched value. A finding is a TYPE, a file (or, with --history, a path in an earlier commit), and
line numbers. Personal terms the scan cannot guess (your name, user names, the employers in your own search, your pay
figures) come from a denylist file that lives outside the repository. The default is the vault's
`release_denylist.txt`, one term per line, with `#` for comments. Its entries are reported by number, never by text.

Exit code: 2 if a secret-type finding exists; 1 if any other finding exists; 0 if clean. Use it as a release gate
(docs/public_release_checklist.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRETS = {   # type -> pattern; any match is a release blocker
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{8,}",
    "OpenAI-style API key": r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}",
    "Google OAuth client secret": r"\bGOCSPX-[A-Za-z0-9_-]{10,}",
    "Google OAuth refresh token": r"\b1//0[A-Za-z0-9_-]{20,}",
    "Google OAuth access token": r"\bya29\.[A-Za-z0-9_-]{20,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Slack token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "Private key": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    "JSON web token": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "Credential in code or config": r"(?i)\b(?:password|passwd|secret|api_key|apikey|client_secret|refresh_token|access_token|auth_token)"
                                    r"\b[\"']?\s*[:=]\s*[\"'][^\"'\s{}<>$%]{8,}[\"']",
    "Cookie value": r"(?i)\b(?:set-)?cookie:\s*[\w.-]+=[^\s;\"']{8,}",
}
PERSONAL = {
    "Email address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "Phone number": r"(?<![\d.-])(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\d.-])",
    # C:\Users\name, C:/Users/name, /c/Users/name (Git Bash), or /Users/name and /home/name not inside a URL path
    "Machine path with a user name": r"(?i)(?:\b[A-Z]:[\\/]{1,2}|(?<![\w/.:-])/[a-z]/|(?<![\w/.:-])/)(?:Users|home)[\\/]{1,2}"
                                     r"(?!you\b|<|%|\$|USERNAME\b|Public\b|Default\b)[A-Za-z0-9._-]+",
}
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid", "anthropic.com")
FILE_TYPES = {  # type -> extensions; every such file needs a person's look before release
    "Database or backup file": (".db", ".sqlite", ".sqlite3", ".abf", ".bak", ".dump"),
    "Image or screenshot": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"),
    "Power BI file (contains data)": (".pbix",),
    "Email file": (".eml", ".msg"),
    "Document (resume, letter?)": (".pdf", ".docx", ".doc", ".xlsx", ".pptx"),
    "Environment file": (".env",),
}
SECRET_TYPES = set(SECRETS)


def default_denylist() -> Path | None:
    vault = Path(os.environ.get("CAREEROS_VAULT") or ROOT.parent / "vault")
    path = vault / "release_denylist.txt"
    return path if path.exists() else None


def load_denylist(path: Path | None) -> list[re.Pattern]:
    if not path:
        return []
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
    return [re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])") for t in terms]


def scan_text(text: str, deny: list[re.Pattern]) -> list[tuple[str, int]]:
    """(type, line number) for every finding in one file. Values are never returned."""
    found = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if "release-scan: fake" not in line:   # a test placeholder, marked on its own line so a reviewer sees why
            for kind, pattern in SECRETS.items():
                if re.search(pattern, line):
                    found.append((kind, number))
        for match in re.finditer(PERSONAL["Email address"], line):
            if not match.group(0).lower().endswith(SAFE_EMAIL_DOMAINS):
                found.append(("Email address", number))
        if any(not re.search(r"\b555[ .-]\d{4}\b", m.group(0)) for m in re.finditer(PERSONAL["Phone number"], line)):
            found.append(("Phone number", number))   # the 555 exchange is fictional
        if re.search(PERSONAL["Machine path with a user name"], line):
            found.append(("Machine path with a user name", number))
        for index, term in enumerate(deny, 1):
            if term.search(line):
                found.append((f"Denylisted term #{index}", number))
    return found


def file_type(path: str) -> str | None:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "Environment file"
    return next((kind for kind, exts in FILE_TYPES.items() if lower.endswith(exts) and kind != "Environment file"), None)


def git(*args: str, root: Path, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=True).stdout


def tracked(root: Path) -> dict[str, bytes]:
    """The files a release would hold: Git's view (tracked, plus untracked files that are not ignored). Outside a Git
    work tree, such as a scratch copy made by tools/mutate.py, every file except Git's folder, virtual environments and
    caches: a walk sees at least as much as Git would."""
    try:
        names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", root=root).split(b"\0")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
                if path.is_file() and not any(part in (".git", "__pycache__") or part.startswith(".venv")
                                              for part in path.relative_to(root).parts)}
    out = {}
    for name in filter(None, names):
        path = root / name.decode("utf-8")
        if path.is_file():
            out[name.decode("utf-8")] = path.read_bytes()
    return out


def historical(root: Path, current: dict[str, bytes]) -> dict[str, bytes]:
    """Every blob in any commit whose content is not in today's tree, by the first path it appeared under."""
    blobs: dict[str, str] = {}
    for commit in git("rev-list", "--all", root=root).decode().split():
        for entry in git("ls-tree", "-r", "-z", commit, root=root).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            sha = meta.split()[2].decode()
            blobs.setdefault(sha, f"{path.decode('utf-8')} @ {commit[:7]}")
    # today's versions, as git stores them (the working tree may differ only in line endings)
    now = {line.split()[1].decode() for line in git("ls-files", "-s", root=root).splitlines() if line}
    out = {}
    for sha, where in blobs.items():
        if sha not in now:
            out[where] = git("cat-file", "blob", sha, root=root)
    return out


def commit_identities(root: Path) -> list[tuple[str, int]]:
    """Personal data in commit metadata (authors, committers, trailers): type and number of commits, never the value."""
    log = git("log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", root=root).decode("utf-8", "replace")
    counts: dict[str, int] = defaultdict(int)
    for record in filter(str.strip, log.split("\x1e")):
        fields = record.strip().split("\x00")
        emails = [e for e in (fields[2], fields[4]) if e and not e.lower().endswith(SAFE_EMAIL_DOMAINS + ("users.noreply.github.com",))]
        if emails:
            counts["Email address in commit author or committer"] += 1
        if any(n.strip() for n in (fields[1], fields[3])):
            counts["Name in commit author or committer"] += 1
    return sorted(counts.items())


def scan(root: Path, deny: list[re.Pattern], history: bool) -> dict:
    current = tracked(root)
    sources = {"tree": current}
    if history:
        sources["history"] = historical(root, current)
    findings: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for origin, files in sources.items():
        for path, content in files.items():
            where = path if origin == "tree" else f"history: {path}"
            kind = file_type(path)
            if kind:
                findings[kind][where] = []
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if path.endswith("tools/release_scan.py") or path.endswith("tests/test_release_scan.py"):
                continue   # its own detection patterns and planted test values
            for found_kind, line in scan_text(text, deny):
                findings[found_kind][where].append(line)
    result = {"files_scanned": len(current), "history_blobs_scanned": len(sources.get("history", {})),
              "findings": {k: {f: sorted(set(v)) for f, v in files.items()} for k, files in sorted(findings.items())}}
    if history:
        result["commit_metadata"] = dict(commit_identities(root))
    return result


def report(result: dict) -> str:
    out = [f"Scanned {result['files_scanned']} files" + (f" and {result['history_blobs_scanned']} historical file versions"
                                                          if result["history_blobs_scanned"] else "")]
    for kind, files in result["findings"].items():
        marker = "SECRET " if kind in SECRET_TYPES else ""
        out.append(f"\n{marker}{kind}: {len(files)} file(s)")
        for path, lines in sorted(files.items()):
            shown = ", ".join(map(str, lines[:12])) + (f" (+{len(lines) - 12} more)" if len(lines) > 12 else "")
            out.append(f"  {path}" + (f"  lines {shown}" if lines else ""))
    for kind, n in result.get("commit_metadata", {}).items():
        out.append(f"\n{kind}: {n} commit(s)")
    if not result["findings"] and not result.get("commit_metadata"):
        out.append("No findings.")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Public-release scan; never prints a matched value.")
    parser.add_argument("--root", default=str(ROOT), help="repository to scan (default: this one)")
    parser.add_argument("--history", action="store_true", help="also scan every earlier version of every file, and commit metadata")
    parser.add_argument("--denylist", help="file of private terms, one per line (default: the vault's release_denylist.txt)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    deny_path = Path(args.denylist) if args.denylist else default_denylist()
    result = scan(Path(args.root), load_denylist(deny_path), args.history)
    result["denylist"] = f"{len(load_denylist(deny_path))} terms" if deny_path else "none (personal terms not checked)"
    print(json.dumps(result, indent=1) if args.json else report(result) + f"\nDenylist: {result['denylist']}")
    if any(k in SECRET_TYPES for k in result["findings"]):
        return 2
    return 1 if result["findings"] or result.get("commit_metadata") else 0


if __name__ == "__main__":
    sys.exit(main())
