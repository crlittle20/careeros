"""Job recommendations posted as issues in a private GitHub repository (e.g. by a ChatGPT scheduled task).

Each open issue is read once: its job links go through the same path as alert-email links (readable boards graded,
every other site a lead, never opened), then CareerOS comments one line per job and closes the issue. Uses the
GitHub CLI already signed in on this PC. Issue text is untrusted data: only titles, companies, locations, notes and
links are taken from it, and nothing about you (profile, pay, scores, rejection reasons) is written back.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

from careeros.core.clock import from_external

from .alerts import AlertEmail, Link, canonical_job_url

GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")
MAX_ISSUES = 20
MAX_LINES = 200
REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"https?://[^\s|<>\"\)\]]+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
OUTCOME_TEXT = {
    "graded": "graded",
    "already_tracked": "already tracked by CareerOS",
    "lead": "lead: CareerOS does not open this site, so it is listed for the owner to open",
    "outside_target_families": "outside target families",
    "seen_before": "seen before",
    "unreadable_kept_as_lead": "could not read the posting (kept as a lead)",
    "no_title": "skipped: no job title on the line",
    "not_a_job_link": "skipped: not a job posting link",
    "suppressed": "skipped",
    "malformed_row": "skipped: this line could not be read (malformed); the rest of the issue was processed",
    "row_error": "flagged: CareerOS hit an error on this line; it was skipped and logged, the rest was processed",
}
NOTE_MAX = 300


class InboxError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def gh_path() -> str | None:
    return shutil.which("gh") or (str(GH_FALLBACK) if GH_FALLBACK.exists() else None)


def _gh(args: list[str], runner=subprocess.run, stdin: str | None = None) -> str:
    from careeros.demo.sandbox import refuse
    refuse("GitHub (gh)")  # every GitHub call goes through here; Demo Mode never makes one
    exe = gh_path()
    if exe is None and runner is subprocess.run:  # an injected runner (tests) needs no installed CLI
        raise InboxError("gh_missing", "GitHub CLI (gh) is not installed")
    result = runner([exe or "gh", *args], input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise InboxError("gh_failed", f"gh {args[0]} {args[1]} failed: {(result.stderr or '').strip()[:300]}")
    return result.stdout or ""


def check_repo(repo: str) -> str:
    if not repo or not REPO.match(repo):
        raise InboxError("bad_repo", f"not an owner/name repository: {repo!r}")
    return repo


def open_issues(repo: str, runner=subprocess.run) -> list[dict]:
    out = _gh(["issue", "list", "-R", check_repo(repo), "--state", "open", "--limit", str(MAX_ISSUES),
               "--json", "number,title,body,author,createdAt"], runner)
    data = json.loads(out or "[]")
    return [i for i in data if isinstance(i, dict) and isinstance(i.get("number"), int)]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]+", "", text or "")).strip(" -–—:·|")


def _parse_line(line: str) -> dict | None:
    markdown = _MD_LINK.search(line)
    plain = _URL.search(line)
    if not (markdown or plain):
        return None
    url = (markdown.group(2) if markdown else plain.group(0)).rstrip(".,;")
    title = company = location = note = ""
    if "|" in line:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        at = next(i for i, c in enumerate(cells) if _URL.search(c))
        before, after = [_clean(c) for c in cells[:at]], [_clean(c) for c in cells[at + 1:]]
        if at == 0 and markdown:  # "[Title](link) | Company | Location | Note"
            title, rest = _clean(markdown.group(1)), after
            company, location, note = (rest + ["", "", ""])[:2] + [" ".join(rest[2:])]
        else:
            title, company, location = (before + ["", "", ""])[:3]
            note = " ".join(x for x in after if x)
    elif markdown:
        title = _clean(markdown.group(1))
        note = _clean(_MD_LINK.sub(" ", line))
    else:
        title = _clean(line[:plain.start()])
        note = _clean(line[plain.end():])
    if title.lower() in ("title", "job title", "role"):
        return None  # a header row that happens to contain a link
    return {"title": title[:200], "company": company[:100], "location": location[:100], "url": url, "note": note[:NOTE_MAX]}


def parse_lines(body: str) -> list[dict]:
    """Rows of {title, company, location, url, note}. Pipe-separated lines, markdown tables and [title](link) bullets.
    A line that cannot be read becomes a row marked `malformed`; it never stops the other lines (prompts v2.5)."""
    rows = []
    for raw in (body or "").splitlines()[:MAX_LINES]:
        line = _BULLET.sub("", raw.strip())
        try:
            row = _parse_line(line)
        except Exception:  # noqa: BLE001 - untrusted text: one bad line is reported, never fatal
            row = {"title": _clean(line)[:80], "company": "", "location": "", "url": "", "note": "", "malformed": True}
        if row:
            rows.append(row)
    return rows


def issue_alert(issue: dict) -> AlertEmail:
    """The issue as an alert: the same lead/grading path as alert emails. A line whose link cannot be read becomes a
    link on the site "invalid", which is reported as malformed and never stored or opened."""
    links = []
    for row in parse_lines(issue.get("body") or ""):
        try:
            canonical = None if row.get("malformed") else canonical_job_url(row["url"])
        except ValueError:  # e.g. a broken IPv6 host: urlsplit refuses it
            canonical = None
        if canonical is None:
            links.append(Link(url=row["url"][:200], site="invalid", text=row["title"], context="", job_like=False))
            continue
        url, site, _ = canonical
        context = " · ".join(x for x in (row["company"], row["location"], row["note"]) if x)
        links.append(Link(url=url, site=site, text=row["title"], context=context[:NOTE_MAX + 210], job_like=True))
    author = ((issue.get("author") or {}).get("login") or "unknown")[:60]
    return AlertEmail(sender=f"GitHub inbox ({author})", subject=f"#{issue['number']} {issue.get('title') or ''}"[:200],
                      received_at=from_external(issue.get("createdAt")), links=links)


_NIGHT_TITLE = re.compile(r"^\s*(?:test\s+)?night\b", re.I)


def issue_profile(issue: dict) -> str:
    """'night' for issues titled "NIGHT ..." (or "TEST NIGHT ..."); everything else is PRIMARY."""
    return "night" if _NIGHT_TITLE.match(issue.get("title") or "") else "primary"


# The Gmail line every scheduled issue carries (prompts v2.5): "Gmail status: ok" / "unavailable" / "partial". The
# older wording "Gmail alert intake unavailable for this run" is also recognised. Only this status is kept: never a
# Gmail message, ID, sender or address.
_GMAIL_LINE = re.compile(r"^\W*gmail(?:[ _-]?alert)?(?:[ _-]?intake)?[ _-]?status\W*[:=]\W*(ok|available|unavailable|failed|"
                         r"partial|skipped|not connected|disconnected)\b", re.I | re.M)
_GMAIL_FAILED = re.compile(r"gmail alert intake (?:was )?unavailable", re.I)


def gmail_status(body: str) -> str | None:
    """'ok', 'partial' or 'unavailable' when the issue reports how ChatGPT's Gmail alert intake went; None if silent."""
    match = _GMAIL_LINE.search(body or "")
    if match:
        word = match.group(1).lower()
        return "ok" if word in ("ok", "available") else ("partial" if word == "partial" else "unavailable")
    return "unavailable" if _GMAIL_FAILED.search(body or "") else None


def reply_text(day: str, outcomes: list, gmail: str | None = None) -> str:
    counts: dict[str, int] = {}
    lines = []
    for link, outcome in outcomes[:100]:
        counts[outcome] = counts.get(outcome, 0) + 1
        lines.append(f"- {OUTCOME_TEXT.get(outcome, outcome)}: {link.text or link.url} ({link.site})")
    summary = ", ".join(f"{n} {OUTCOME_TEXT.get(k, k).split(':')[0]}" for k, n in counts.items()) or "no job links found"
    note = ""
    if gmail in ("unavailable", "partial"):
        note = (f"\n\nGmail status recorded: {gmail}. intake_status.json now asks the next run to use a 7-day Gmail "
                "lookback.")
    elif gmail == "ok":
        note = "\n\nGmail status recorded: ok."
    return f"CareerOS read this issue on {day}: {summary}.{note}\n\n" + "\n".join(lines)


def close_with_reply(repo: str, number: int, reply: str, runner=subprocess.run) -> None:
    _gh(["issue", "comment", str(number), "-R", check_repo(repo), "--body-file", "-"], runner, stdin=reply)
    _gh(["issue", "close", str(number), "-R", repo], runner)


def publish_files(repo: str, files: dict[str, str], message: str, runner=subprocess.run) -> list[str]:
    """Create or update files in the inbox repository through the GitHub contents API. A file whose content is already
    identical is left alone (no commit), so publishing the same index twice changes nothing. Returns the changed paths."""
    changed = []
    for path, content in sorted(files.items()):
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", path) or ".." in path:
            raise InboxError("bad_path", f"refusing to write {path!r}")
        sha = None
        try:
            current = json.loads(_gh(["api", f"repos/{check_repo(repo)}/contents/{path}"], runner) or "{}")
            sha = current.get("sha")
            if sha and base64.b64decode(current.get("content") or "").decode("utf-8") == content:
                continue
        except InboxError as exc:
            if "404" not in str(exc) and "Not Found" not in str(exc):
                raise
        body = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        _gh(["api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"], runner, stdin=json.dumps(body))
        changed.append(path)
    return changed
