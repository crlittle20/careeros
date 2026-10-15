"""Postings you saved yourself (browser "Save page as", or pasted text) dropped into the vault inbox.

Used for sites whose terms forbid automated access (an employer's own careers site, a staffing agency's): you browse, CareerOS
reads only the files you saved. Saved pages are parsed from their schema.org JobPosting data (which career sites embed
for search engines); plain-text files use a small header.

Text file format:
    URL: https://...
    Title: Business Systems Analyst
    Employer: Northgate Health
    Location: Remote (optional)

    <posting text>
"""
from __future__ import annotations

import html as htmllib
import json
import re
from pathlib import Path

from careeros.core.clock import from_external
from careeros.core.fingerprint import text_fingerprint
from careeros.normalize.posting import RawPosting, canonical_url, norm
from careeros.normalize.sanitize import clean_text, html_to_text

_LD = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_CANONICAL = re.compile(r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", re.I)
_OG_URL = re.compile(r"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"']([^\"']+)", re.I)
_SAVED_FROM = re.compile(r"<!-- saved from url=\(\d+\)(\S+) -->", re.I)


class SavedPostingError(ValueError):
    pass


def parent_for(name: str, policy) -> str:
    wanted = norm(name)
    for parent, aliases in (policy.get("parents", default={}) or {}).items():
        if wanted == norm(parent) or any(wanted == norm(a) or wanted.startswith(norm(a) + " ") for a in aliases):
            return parent
    return name


def _job_postings(markup: str) -> list[dict]:
    found = []
    for block in _LD.findall(markup):
        try:
            data = json.loads(htmllib.unescape(block.strip()))
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("@type") == "JobPosting" or (isinstance(item.get("@type"), list) and "JobPosting" in item["@type"]):
                    found.append(item)
                stack.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [])
            elif isinstance(item, list):
                stack.extend(item)
    return found


def _location(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    parts = []
    for place in places:
        address = (place or {}).get("address") or {}
        if isinstance(address, dict):
            bits = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
            bits = [b.get("name") if isinstance(b, dict) else b for b in bits]
            parts.append(", ".join(str(b) for b in bits if b))
    location = "; ".join(p for p in parts if p)
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        location = f"Remote{' - ' + location if location else ''}"
    return location


def _salary(posting: dict) -> dict | None:
    salary = posting.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if not isinstance(value, dict):
        return None
    low, high = value.get("minValue") or value.get("value"), value.get("maxValue") or value.get("value")
    try:
        low, high = (float(low) if low is not None else None), (float(high) if high is not None else None)
    except (TypeError, ValueError):
        return None
    if low is None and high is None:
        return None
    unit = str(value.get("unitText") or "").upper()
    return {"min": low, "max": high, "currency": salary.get("currency") or "USD", "period": "hour" if unit == "HOUR" else "year"}


def parse_saved_html(markup: str, fallback_name: str) -> tuple[RawPosting, str]:
    postings = _job_postings(markup)
    if not postings:
        raise SavedPostingError("no JobPosting data in the saved page; save the posting page itself, or paste its text into a .txt file")
    p = postings[0]
    org = p.get("hiringOrganization") or {}
    employer = (org.get("name") if isinstance(org, dict) else str(org)) or "Unknown employer"
    url = None
    for pattern in (_CANONICAL, _OG_URL, _SAVED_FROM):
        match = pattern.search(markup)
        if match:
            url = match.group(1)
            break
    url = canonical_url(p.get("url") or url)
    identifier = p.get("identifier")
    ident = identifier.get("value") if isinstance(identifier, dict) else identifier
    text = html_to_text(p.get("description") or "")
    workplace = "remote" if str(p.get("jobLocationType", "")).upper() == "TELECOMMUTE" else None
    employment = p.get("employmentType")
    employment = employment[0] if isinstance(employment, list) and employment else employment
    raw = RawPosting(ats="manual", board_token=re.sub(r"[^a-z0-9]+", "-", norm(employer)).strip("-") or "employer",
                     external_id=str(ident or text_fingerprint(url or text or fallback_name)[:16]),
                     title=htmllib.unescape(str(p.get("title") or "")).strip(), location=_location(p), url=url, apply_url=url,
                     posted_at=from_external(p.get("datePosted")), description_text=text,
                     requisition_id=str(ident) if ident else None, employment_type_hint=employment, workplace_hint=workplace,
                     comp=_salary(p), raw={"source_file": fallback_name, "job_posting": p})
    if not raw.title:
        raise SavedPostingError("saved page has no job title")
    return raw, employer


def parse_saved_text(text: str, fallback_name: str) -> tuple[RawPosting, str]:
    header, _, body = text.replace("\r\n", "\n").partition("\n\n")
    fields = {}
    for line in header.split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in ("url", "title", "employer", "location"):
            fields[key.strip().lower()] = value.strip()
    if not fields.get("title") or not fields.get("employer") or not body.strip():
        raise SavedPostingError("text file needs 'Title:' and 'Employer:' header lines, a blank line, then the posting text")
    url = canonical_url(fields.get("url"))
    employer = fields["employer"]
    raw = RawPosting(ats="manual", board_token=re.sub(r"[^a-z0-9]+", "-", norm(employer)).strip("-") or "employer",
                     external_id=text_fingerprint(url or body)[:16], title=fields["title"], location=fields.get("location", ""),
                     url=url, apply_url=url, description_text=clean_text(body), raw={"source_file": fallback_name, "text": body})
    return raw, employer


def parse_saved_file(path: Path) -> tuple[RawPosting, str]:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    if Path(path).suffix.lower() in (".html", ".htm"):
        return parse_saved_html(content, Path(path).name)
    return parse_saved_text(content, Path(path).name)
