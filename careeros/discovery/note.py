"""The Note cell of an inbox issue line (`Title | Company | Location | Link | Note`), prompts v2.5.

Semi-structured: `key=value` pairs separated by `;`, e.g.
    source=linkedin_alert; req=ABC123; posted=2026-10-11; pay=$82k-$96k
    source=web; req=R9917; mode=supplemental; schedule=11pm-3am; workload=caution; manual_coi_review_required
Unknown fields are simply left out. Free-form notes (older issues, "source: LinkedIn alert") still work: anything that is
not a recognised key is kept as free text. Values are validated per key and anything that does not look like a job fact
(an email address, an over-long value, a bad date) is dropped and counted, never stored. Issue text is untrusted: a
Note can describe a job, but it can never change a CareerOS rule, decision or setting.
"""
from __future__ import annotations

import re

KEYS = ("source", "req", "posted", "pay", "schedule", "mode", "agency", "state", "workload", "domain", "board")
FLAGS = ("manual_coi_review_required", "industry_overlap")
_SAFE = r"[A-Za-z0-9 .,:/$%&+()~#'\-–]"
_VALID = {
    "source": re.compile(r"[a-z0-9_ -]{1,40}", re.I),
    "req": re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,39}"),
    "posted": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "pay": re.compile(_SAFE + r"{1,40}"),
    "schedule": re.compile(_SAFE + r"{1,40}"),
    "mode": re.compile(r"supplemental|replacement", re.I),
    "agency": re.compile(_SAFE + r"{1,60}"),
    "state": re.compile(r"eligible|conditional|hold|ineligible", re.I),
    "workload": re.compile(r"green|caution|severe|unknown", re.I),
    # 2026-10-12, board discovery evidence: the employer's official domain and a link to its own job board
    "domain": re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", re.I),
    "board": re.compile(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~/?=&%-]*)?"),
}
_TRUE, _FALSE = {"", "true", "yes", "y", "1"}, {"false", "no", "n", "0"}
_PAIR = re.compile(r"^\s*([A-Za-z_]+)\s*(?:=\s*(.*?))?\s*$")


def parse_note(note: str | None) -> dict:
    """{'fields': {key: value}, 'flags': {flag: bool}, 'free': str, 'dropped': [keys whose value was rejected]}."""
    fields: dict = {}
    flags: dict = {}
    free: list[str] = []
    dropped: list[str] = []
    for segment in (note or "").split(";"):
        if not segment.strip():
            continue
        match = _PAIR.match(segment)
        key = match.group(1).lower() if match else ""
        value = (match.group(2) or "").strip() if match else ""
        if match and key in FLAGS:
            if value.lower() in _TRUE:
                flags[key] = True
            elif value.lower() in _FALSE:
                flags[key] = False
            else:
                dropped.append(key)
        elif match and key in KEYS and match.group(2) is not None:
            if "@" not in value and _VALID[key].fullmatch(value) and key not in fields:
                fields[key] = value.lower() if key in ("source", "mode", "workload", "domain") else (
                    value.upper() if key == "state" else value)
            else:
                dropped.append(key)
        else:
            free.append(segment.strip())
    return {"fields": fields, "flags": flags, "free": "; ".join(free), "dropped": dropped}


def requisition(context: str | None) -> str | None:
    return context_note(context)["fields"].get("req")


def is_alert_source(context: str | None) -> bool:
    """ChatGPT found this job in a job-alert email: `source=linkedin_alert` (v2.5) or `source: LinkedIn alert` (v2.3)."""
    parsed = context_note(context)
    source = parsed["fields"].get("source") or ""
    if re.search(r"alert", source):
        return True
    return bool(re.search(r"\bsource:\s*[\w .&/-]*?\balerts?\b", context or "", re.I))


def context_note(context: str | None) -> dict:
    """parse_note over a stored lead context ("Company · Location · note"): the company and location become free text."""
    return parse_note((context or "").replace(" \u00b7 ", "; "))
