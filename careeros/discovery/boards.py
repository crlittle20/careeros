"""Adapters for public ATS job-board APIs (read-only, intended for listing jobs).

Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Lever:      https://api.lever.co/v0/postings/{company}?mode=json
Ashby:      https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
Workable:   https://apply.workable.com/api/v1/widget/accounts/{account}?details=true  (the public careers-page widget)

SmartRecruiters is deliberately absent: api.smartrecruiters.com/robots.txt disallows every client except LinkedInBot,
so its postings are handled like other manual sites (save the page, `careeros import`).
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from careeros.core.clock import from_external
from careeros.normalize.posting import RawPosting
from careeros.normalize.sanitize import clean_text, html_to_text

from .http import FetchError, HttpClient


def _token(value: str) -> str:
    token = (value or "").strip()
    if not token or "/" in token or ".." in token:
        raise FetchError("invalid_board_token")
    return quote(token, safe="-_.")


# --- Greenhouse --------------------------------------------------------------------------------------
def _greenhouse_employment(item: dict) -> str | None:
    """Employment type from Greenhouse custom fields, when the employer fills one in."""
    for field in item.get("metadata") or []:
        if isinstance(field, dict) and re.search(r"employment|job type|worker type|time type", str(field.get("name", "")), re.I):
            value = field.get("value")
            return ", ".join(map(str, value)) if isinstance(value, list) else (str(value) if value else None)
    return None


def greenhouse_posting(token: str, item: dict) -> RawPosting:
    return RawPosting(employment_type_hint=_greenhouse_employment(item),
        ats="greenhouse", board_token=token, external_id=str(item["id"]), title=(item.get("title") or "").strip(),
        location=((item.get("location") or {}).get("name") or "").strip(), url=item.get("absolute_url"),
        apply_url=item.get("absolute_url"), posted_at=from_external(item.get("first_published") or item.get("updated_at")),
        description_text=html_to_text(item.get("content") or ""), requisition_id=item.get("requisition_id"),
        department=", ".join(d.get("name", "") for d in item.get("departments") or [] if isinstance(d, dict) and d.get("name")) or None,
        updated_at=from_external(item.get("updated_at")), raw=item)


def fetch_greenhouse(client: HttpClient, token: str) -> list[RawPosting]:
    data = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}/jobs?content=true")
    return [greenhouse_posting(token, item) for item in data.get("jobs", [])]


# --- Lever -------------------------------------------------------------------------------------------
def lever_posting(company: str, item: dict) -> RawPosting:
    # Some Lever boards put only the opening paragraph in descriptionPlain; the HTML description holds the full text.
    from_html = html_to_text(item.get("description") or "")
    plain = item.get("descriptionPlain") or ""
    sections = [from_html if len(from_html) >= len(plain) else plain]
    for block in item.get("lists") or []:
        sections.append(f"{(block.get('text') or '').strip()}:")
        sections.append(html_to_text(block.get("content") or ""))
    sections.append(item.get("additionalPlain") or html_to_text(item.get("additional") or ""))
    categories = item.get("categories") or {}
    salary = item.get("salaryRange") or {}
    comp = None
    if salary.get("min") or salary.get("max"):
        interval = (salary.get("interval") or "").lower()
        comp = {"min": salary.get("min"), "max": salary.get("max"), "currency": salary.get("currency") or "USD",
                "period": "hour" if "hour" in interval else "year"}
    return RawPosting(
        ats="lever", board_token=company, external_id=str(item["id"]), title=(item.get("text") or "").strip(),
        location=(categories.get("location") or "").strip(), url=item.get("hostedUrl"), apply_url=item.get("applyUrl"),
        posted_at=from_external(item.get("createdAt")), description_text=clean_text("\n".join(s for s in sections if s)),
        employment_type_hint=categories.get("commitment"), workplace_hint=item.get("workplaceType"), comp=comp,
        department=" / ".join(x for x in (categories.get("department"), categories.get("team")) if x) or None,
        updated_at=from_external(item.get("updatedAt")), raw=item)


def fetch_lever(client: HttpClient, company: str) -> list[RawPosting]:
    data = client.get_json(f"https://api.lever.co/v0/postings/{_token(company)}?mode=json")
    if not isinstance(data, list):
        raise FetchError("unexpected_shape")
    return [lever_posting(company, item) for item in data]


# --- Ashby -------------------------------------------------------------------------------------------
def ashby_posting(name: str, item: dict) -> RawPosting:
    comp = None
    for part in ((item.get("compensation") or {}).get("summaryComponents") or []):
        if part.get("compensationType") == "Salary" and (part.get("minValue") or part.get("maxValue")):
            interval = (part.get("interval") or "").lower()
            comp = {"min": part.get("minValue"), "max": part.get("maxValue"), "currency": part.get("currencyCode") or "USD",
                    "period": "hour" if "hour" in interval else "year"}
            break
    location = item.get("location") or ""
    if item.get("isRemote") and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    return RawPosting(
        ats="ashby", board_token=name, external_id=str(item["id"]), title=(item.get("title") or "").strip(),
        location=location, url=item.get("jobUrl"), apply_url=item.get("applyUrl"),
        posted_at=from_external(item.get("publishedAt")),
        description_text=item.get("descriptionPlain") and clean_text(item["descriptionPlain"]) or html_to_text(item.get("descriptionHtml") or ""),
        employment_type_hint=item.get("employmentType"), workplace_hint=item.get("workplaceType") or ("Remote" if item.get("isRemote") else None),
        comp=comp, department=" / ".join(x for x in (item.get("department"), item.get("team")) if x) or None,
        updated_at=from_external(item.get("updatedAt")), raw=item)


def fetch_ashby(client: HttpClient, name: str) -> list[RawPosting]:
    data = client.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{_token(name)}?includeCompensation=true")
    return [ashby_posting(name, item) for item in data.get("jobs", []) if item.get("isListed", True)]


# --- Workable ----------------------------------------------------------------------------------------
def _workable_location(item: dict) -> str:
    places = [p for p in (item.get("locations") or []) if isinstance(p, dict) and not p.get("hidden")]
    if not places:
        places = [{"city": item.get("city"), "region": item.get("state"), "country": item.get("country")}]
    text = "; ".join(", ".join(str(p[k]).strip() for k in ("city", "region", "country") if p.get(k) and str(p[k]).strip())
                     for p in places)
    return f"{text} (Remote)".strip() if item.get("telecommuting") else text


def workable_posting(account: str, item: dict) -> RawPosting:
    return RawPosting(
        ats="workable", board_token=account, external_id=str(item["shortcode"]), title=(item.get("title") or "").strip(),
        location=_workable_location(item), url=item.get("url") or item.get("shortlink"), apply_url=item.get("application_url"),
        posted_at=from_external(item.get("published_on") or item.get("created_at")),
        description_text=html_to_text(item.get("description") or ""), requisition_id=item.get("code") or None,
        employment_type_hint=item.get("employment_type") or None, workplace_hint="remote" if item.get("telecommuting") else None,
        department=item.get("department") or None, updated_at=from_external(item.get("updated_at")), raw=item)


def fetch_workable(client: HttpClient, account: str) -> list[RawPosting]:
    data = client.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{_token(account)}?details=true")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise FetchError("unexpected_shape")
    return [workable_posting(account, item) for item in data["jobs"] if item.get("shortcode")]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "workable": fetch_workable}


def fetch_board(client: HttpClient, ats: str, token: str) -> list[RawPosting]:
    from .sources import require_allowed
    require_allowed(ats)
    if ats not in FETCHERS:
        raise FetchError(f"unsupported_ats:{ats}")
    return FETCHERS[ats](client, token)


# --- Manual URLs -------------------------------------------------------------------------------------
def parse_job_url(url: str) -> tuple[str, str, str] | None:
    """(ats, board_token, job_id) for known hosted ATS job URLs; None for anything else."""
    parts = urlsplit(url.strip())
    host, segments = parts.netloc.lower(), [s for s in parts.path.split("/") if s]
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and len(segments) >= 3 and segments[1] == "jobs":
        return "greenhouse", segments[0], segments[2]
    if host == "jobs.lever.co" and len(segments) >= 2:
        return "lever", segments[0], segments[1]
    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        return "ashby", segments[0], segments[1]
    if host == "apply.workable.com" and len(segments) >= 3 and segments[0] != "j" and segments[1] == "j":
        return "workable", segments[0], segments[2]
    return None


def fetch_single(client: HttpClient, ats: str, token: str, job_id: str) -> RawPosting:
    from .sources import require_allowed
    require_allowed(ats)
    if ats == "greenhouse":
        item = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}/jobs/{_token(job_id)}")
        return greenhouse_posting(token, item)
    if ats == "lever":
        item = client.get_json(f"https://api.lever.co/v0/postings/{_token(token)}/{_token(job_id)}")
        return lever_posting(token, item)
    if ats in ("ashby", "workable"):  # no documented single-posting endpoint: read the board and pick the job
        for posting in FETCHERS[ats](client, token):
            if posting.external_id == job_id:
                return posting
        raise FetchError("not_found", 404)
    raise FetchError(f"unsupported_ats:{ats}")
