"""Low-friction intake for postings you opened yourself (2026-10-12, Part 6).

Paste the posting text (from LinkedIn, Indeed, Workday, an email, anywhere) and its direct application URL. CareerOS:
- extracts what the text states (title, company, location, requisition id, pay, deadline) and nothing else;
- deduplicates against what it already tracks (same link, same requisition at the same company, same title there);
- grades it like any posting, labelled as human-supplied evidence (`human_text`), apart from live ATS data;
- records a manual-source entry when the link is an excluded system (e.g. the employer's Workday board);
- NEVER fetches the URL or any other site: this module takes no HTTP client at all.
Pasted text is untrusted data: it is read for job facts, never followed as instructions.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from careeros.core.fingerprint import text_fingerprint
from careeros.normalize.posting import RawPosting, canonical_url, norm
from careeros.normalize.sanitize import clean_text

from .board_urls import manual_system

MAX_TEXT = 60000
_LABEL = lambda name: re.compile(rf"^\s*(?:{name})\s*[:\-–]\s*(.+?)\s*$", re.I | re.M)
_TITLE = _LABEL(r"job title|position|role|title")
_COMPANY = _LABEL(r"company|employer|organization|hiring company")
_LOCATION = _LABEL(r"location|work location|job location")
_REQ = re.compile(r"\b(?:job|req(?:uisition)?|posting)\s*(?:id|#|number|no\.?)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_.\-]{2,29})", re.I)
_AT = re.compile(r"^(.{3,100}?)\s+(?:at|@)\s+([A-Z][A-Za-z0-9&.,' -]{1,80})$")


def company_from_url(url: str | None) -> str | None:
    """A company name the link itself states: the Workday tenant ("acme.wd5.myworkdayjobs.com" -> "acme")."""
    try:
        host = urlsplit(url or "").netloc.lower()
    except ValueError:
        return None
    if manual_system(url or "") == "workday" and host.count(".") >= 3:
        return host.split(".")[0]
    return None


def parse_pasted(text: str, url: str | None = None, *, company: str | None = None, title: str | None = None,
                 location: str | None = None) -> dict:
    text = clean_text((text or "")[:MAX_TEXT])
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    found_title = title or (m.group(1) if (m := _TITLE.search(text)) else None)
    found_company = company or (m.group(1) if (m := _COMPANY.search(text)) else None)
    if not (found_title and found_company) and lines:
        at = _AT.match(lines[0])
        if at:
            found_title, found_company = found_title or at.group(1), found_company or at.group(2)
    if not found_title and lines and len(lines[0]) <= 100 and not lines[0].endswith("."):
        found_title = lines[0]
    found_company = found_company or company_from_url(url)
    found_location = location or (m.group(1) if (m := _LOCATION.search(text)) else None)
    if not found_location:
        remote = re.search(r"\b(remote(?:\s*[-,(]\s*(?:us|usa|united states)\)?)?|work from home)\b", text, re.I)
        found_location = remote.group(0) if remote else ""
    req = _REQ.search(text)
    return {"title": (found_title or "")[:200], "company": (found_company or "").strip(" ,.")[:100],
            "location": found_location[:120], "requisition_id": req.group(1) if req else None, "text": text,
            "url": url, "canonical": canonical_url(url) if url else None}


def _already(store, parsed: dict) -> str | None:
    from careeros.discovery.seen_index import norm_company, norm_req
    if parsed["canonical"]:
        row = store.one("SELECT job_id FROM jobs WHERE canonical_url = ? OR url = ? LIMIT 1", (parsed["canonical"], parsed["url"]))
        if row:
            return row["job_id"]
    company = norm_company(parsed["company"])
    req = norm_req(parsed["requisition_id"])
    for row in store.all("SELECT job_id, employer_name, parent, requisition_id, title_norm FROM jobs WHERE current_state != 'duplicate'"):
        names = {norm_company(row["employer_name"]), norm_company(row["parent"])}
        if company not in names:
            continue
        if req and norm_req(row["requisition_id"]) == req:
            return row["job_id"]
        if not req and row["title_norm"] == norm(parsed["title"]):
            return row["job_id"]
    return None


def intake(ctx, text: str, url: str | None = None, *, company: str | None = None, title: str | None = None,
           location: str | None = None) -> dict:
    """Grade a posting you pasted. Returns {'job_id', 'status': 'added' | 'already_tracked', 'manual_source'}."""
    from careeros.discovery import board_discovery as bd
    from careeros.discovery.saved import parent_for
    from careeros.openings import record_observation
    from careeros.pipeline import ingest
    parsed = parse_pasted(text, url, company=company, title=title, location=location)
    if not parsed["title"]:
        raise ValueError("no job title found: pass --title")
    if not parsed["company"]:
        raise ValueError("no company found: pass --company")
    if len(parsed["text"]) < 200:
        raise ValueError("too little text to grade: paste the whole posting")
    store = ctx.store
    existing = _already(store, parsed)
    manual = None
    if url and manual_system(url):
        candidate = bd.Candidate(employer_name=parsed["company"], origin="manual", board_url=url, posting_url=url)
        manual = bd._upsert_mapping(store, candidate, manual_system(url), urlsplit(url).netloc.lower(), collection="manual_only",
                                    status="unresolved", evidence=[{"kind": "supplied_board_link", "detail": url[:200]}],
                                    relevant=None, verified_method=None)
    if existing:
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=existing, url=url,
                           canonical=parsed["canonical"], requisition_id=parsed["requisition_id"], title=parsed["title"],
                           company=parsed["company"], location=parsed["location"])
        return {"job_id": existing, "status": "already_tracked", "manual_source": manual}
    slug = re.sub(r"[^a-z0-9]+", "-", parsed["company"].lower()).strip("-")[:60] or "manual"
    external = parsed["requisition_id"] or text_fingerprint(url or parsed["text"])[:16]
    posting = RawPosting(ats="manual", board_token=slug, external_id=external, title=parsed["title"], location=parsed["location"],
                         url=url, apply_url=url, description_text=parsed["text"], requisition_id=parsed["requisition_id"],
                         raw={"source": "human_text", "url": url})
    ingest(ctx, {"employer_id": None, "name": parsed["company"], "parent": parent_for(parsed["company"], ctx.policy)},
           [posting], source="manual_url", full_board=False)
    job_id = f"manual:{slug}:{external}"
    record_observation(store, source_kind="human_text", evidence_level="human", job_id=job_id, url=url, canonical=parsed["canonical"],
                       requisition_id=parsed["requisition_id"], title=parsed["title"], company=parsed["company"],
                       location=parsed["location"])
    return {"job_id": job_id, "status": "added", "manual_source": manual}
