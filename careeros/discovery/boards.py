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


def _get_json(client: HttpClient, url: str):
    """A board API answer with every text value made canonical before any adapter reads it (the D190 design,
    section 3a, point 3): employment metadata, Lever's places and categories, Workable's location, typed countries,
    Ashby's location and pay all read the one form. Links are kept as received."""
    from careeros.normalize.sanitize import canonical_values
    return canonical_values(client.get_json(url))


def fetch_greenhouse(client: HttpClient, token: str) -> list[RawPosting]:
    data = _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}/jobs?content=true")
    return [greenhouse_posting(token, item) for item in data.get("jobs", [])]


# --- Lever -------------------------------------------------------------------------------------------
# a country a provider types as a code, written out in the location field: a code a US state shares ("CA", "IL",
# "IN") is never left where the field could read it as the state (Codex CR-2026-11-15-GATE-FIXES H23, Pass 12; the
# owner's D182). Other codes are written "country XX", which never reads as a US place.
_COUNTRY_NAMES = {
    "AL": "Albania", "AR": "Argentina", "AZ": "Azerbaijan", "CA": "Canada", "CO": "Colombia", "DE": "Germany",
    "GA": "Gabon", "ID": "Indonesia", "IL": "Israel", "IN": "India", "KY": "Cayman Islands", "LA": "Laos",
    "MA": "Morocco", "MD": "Moldova", "ME": "Montenegro", "MN": "Mongolia", "MO": "Macao", "MS": "Montserrat",
    "MT": "Malta", "NC": "New Caledonia", "NE": "Niger", "PA": "Panama", "SC": "Seychelles", "SD": "Sudan",
    "TN": "Tunisia", "VA": "Vatican City", "GB": "United Kingdom", "UK": "United Kingdom", "MX": "Mexico",
    "IE": "Ireland", "FR": "France", "ES": "Spain", "PT": "Portugal", "NL": "Netherlands", "PL": "Poland",
    "PH": "Philippines", "BR": "Brazil", "AU": "Australia", "NZ": "New Zealand", "SG": "Singapore", "JP": "Japan",
    "CN": "China", "CH": "Switzerland", "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland",
    "BE": "Belgium", "AT": "Austria", "IT": "Italy", "ZA": "South Africa", "AE": "United Arab Emirates",
    "CR": "Costa Rica", "EC": "Ecuador", "CL": "Chile", "PE": "Peru", "UA": "Ukraine", "RO": "Romania",
}
_US_TYPED = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}


def typed_country(value) -> str | None:
    """A provider's typed country as the location field shows it: the United States as given; a code spelled out
    ("CA" is "Canada", an unlisted one "country XX"); a name as given; None when there is none."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if text.lower() in _US_TYPED:
        return text   # as given: the plain-US rule reads it
    if len(text) <= 3 and text.isalpha():
        return _COUNTRY_NAMES.get(text.upper(), f"country {text.upper()}")
    return text


def _typed(*values) -> list[str]:
    """Every typed country among `values`, written out (typed_country), each once: what the board says (part 4)."""
    out: list[str] = []
    for value in values:
        country = typed_country(value)
        if country and country.lower() not in (c.lower() for c in out):
            out.append(country)
    return out


def _abroad_typed(*values) -> list[str]:
    """The typed countries among `values` that are not the United States, written out (H23)."""
    return [c for c in (typed_country(v) for v in values) if c and c.lower() not in _US_TYPED]


def _ashby_country(item) -> str | None:
    address = item.get("address") if isinstance(item, dict) else None
    postal = (address or {}).get("postalAddress") if isinstance(address, dict) else None
    return (postal or {}).get("addressCountry") if isinstance(postal, dict) else None


def _all_places(first: str, more) -> str:
    """A posting's primary location and every other one its board lists, joined by "; " (each once), so the location
    field shows every place the board gives, as Workable's already does (the owner's D181: a posting listing "Remote -
    US" and London is not plainly a US posting)."""
    places: list[str] = []
    for place in [first, *(more or [])]:
        text = " ".join(str(place or "").split())
        if text and text.lower() not in (p.lower() for p in places):
            places.append(text)
    return "; ".join(places)


def typed_period(interval) -> str | None:
    """The pay period a board's typed interval names: Lever's "per-month-salary", "per-day-wage", Ashby's "1 MONTH",
    "1 WEEK" and the like, as gate.PERIODS_PER_YEAR keys; None for one it cannot read. A month's, a week's or a day's pay
    is never stored as a year's (the red team before Codex Pass 15, RT-39)."""
    text = re.sub(r"[_\-]+", " ", str(interval or "").lower())
    if "semimonth" in text or "semi month" in text:
        return "semimonthly"
    if "biweek" in text or "bi week" in text or re.search(r"\b2\s+weeks?\b", text):
        return "biweekly"
    for word, period in (("hour", "hour"), ("day", "day"), ("daily", "day"), ("week", "week"), ("month", "month"),
                         ("year", "year"), ("annual", "year")):
        if word in text:
            return period
    return None


def _board_comp(low, high, currency, interval) -> dict | None:
    """A board's typed pay, with its period; none when the period can't be read (the text is read instead)."""
    period = typed_period(interval)
    if period is None or not (low or high):
        return None
    return {"min": low, "max": high, "currency": currency or "USD", "period": period}


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
    comp = _board_comp(salary.get("min"), salary.get("max"), salary.get("currency"), salary.get("interval"))
    return RawPosting(
        ats="lever", board_token=company, external_id=str(item["id"]), title=(item.get("text") or "").strip(),
        location=_all_places(categories.get("location") or "", [*(categories.get("allLocations") or []),
                                                                 *_abroad_typed(item.get("country"))]),
        url=item.get("hostedUrl"),
        apply_url=item.get("applyUrl"),
        posted_at=from_external(item.get("createdAt")), description_text=clean_text("\n".join(s for s in sections if s)),
        employment_type_hint=categories.get("commitment"), workplace_hint=item.get("workplaceType"), comp=comp,
        department=" / ".join(x for x in (categories.get("department"), categories.get("team")) if x) or None,
        updated_at=from_external(item.get("updatedAt")), raw=item, typed_countries=_typed(item.get("country")))


def fetch_lever(client: HttpClient, company: str) -> list[RawPosting]:
    data = _get_json(client, f"https://api.lever.co/v0/postings/{_token(company)}?mode=json")
    if not isinstance(data, list):
        raise FetchError("unexpected_shape")
    return [lever_posting(company, item) for item in data]


# --- Ashby -------------------------------------------------------------------------------------------
def ashby_posting(name: str, item: dict) -> RawPosting:
    comp = None
    for part in ((item.get("compensation") or {}).get("summaryComponents") or []):
        if part.get("compensationType") == "Salary" and (part.get("minValue") or part.get("maxValue")):
            comp = _board_comp(part.get("minValue"), part.get("maxValue"), part.get("currencyCode"), part.get("interval"))
            break
    location = item.get("location") or ""
    if item.get("isRemote") and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    secondary = item.get("secondaryLocations") or []
    location = _all_places(location, [*((s or {}).get("location") if isinstance(s, dict) else s for s in secondary),
                                      *_abroad_typed(_ashby_country(item), *(_ashby_country(s) for s in secondary))])
    return RawPosting(
        ats="ashby", board_token=name, external_id=str(item["id"]), title=(item.get("title") or "").strip(),
        location=location, url=item.get("jobUrl"), apply_url=item.get("applyUrl"),
        posted_at=from_external(item.get("publishedAt")),
        description_text=item.get("descriptionPlain") and clean_text(item["descriptionPlain"]) or html_to_text(item.get("descriptionHtml") or ""),
        employment_type_hint=item.get("employmentType"), workplace_hint=item.get("workplaceType") or ("Remote" if item.get("isRemote") else None),
        comp=comp, department=" / ".join(x for x in (item.get("department"), item.get("team")) if x) or None,
        updated_at=from_external(item.get("updatedAt")), raw=item,
        typed_countries=_typed(_ashby_country(item), *(_ashby_country(s) for s in secondary)))


def _ashby_items(client: HttpClient, name: str) -> list[dict]:
    data = _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{_token(name)}?includeCompensation=true")
    return [item for item in (data or {}).get("jobs", []) if isinstance(item, dict)]


class Listing(list):
    """A board's listing (a list of postings) and, apart, the postings its data still holds unlisted."""
    def __init__(self, listed=(), unlisted=()):
        super().__init__(listed)
        self.unlisted = list(unlisted)


def fetch_ashby(client: HttpClient, name: str) -> list[RawPosting]:
    """The board's listing: unlisted postings (isListed false) are left out, as the board itself leaves them out. They
    are kept apart (`.unlisted`): open at their own link, so a scan never counts one absent, and never admits one from
    the listing either (the red team's gr1-boards-1)."""
    items = _ashby_items(client, name)
    return Listing([ashby_posting(name, i) for i in items if i.get("isListed", True)],
                   [ashby_posting(name, i) for i in items if not i.get("isListed", True)])


# --- Workable ----------------------------------------------------------------------------------------
def _workable_location(item: dict) -> str:
    places = [p for p in (item.get("locations") or []) if isinstance(p, dict) and not p.get("hidden")]
    if not places:
        places = [{"city": item.get("city"), "region": item.get("state"), "country": item.get("country")}]
    # a place's typed country under "country" or "countryCode", else the job's own when that is abroad (the red team
    # after Codex Pass 12, RT2-6)
    job_country = item.get("country") or item.get("countryCode") or item.get("country_code")
    fallback = job_country if _abroad_typed(job_country) else None
    typed = [typed_country(p.get("country") or p.get("countryCode") or p.get("country_code") or fallback) for p in places]
    text = "; ".join(", ".join(x for x in (*(str(p[k]).strip() for k in ("city", "region") if p.get(k)), country) if x)
                     for p, country in zip(places, typed, strict=True))
    # the job's own country abroad is written even when a place names another: the two disagree, and the field must
    # show both (Codex CR-2026-11-15-GATE-FIXES H30, Pass 13). It is left out only when a place's own country is that
    # country, never because its name is inside other words (inside a US state's own name: the red
    # team before Codex Pass 15, RT-30)
    extra = [c for c in _abroad_typed(job_country) if c.lower() not in {(t or "").lower() for t in typed}]
    text = "; ".join(x for x in (text, *extra) if x)
    return f"{text} (Remote)".strip() if item.get("telecommuting") else text


def _workable_typed(item: dict) -> list[str]:
    """The countries a Workable posting types: each place's, and the job's own (part 4)."""
    places = [p for p in (item.get("locations") or []) if isinstance(p, dict) and not p.get("hidden")]
    return _typed(*(p.get("country") or p.get("countryCode") or p.get("country_code") for p in places),
                  item.get("country") or item.get("countryCode") or item.get("country_code"))


def workable_posting(account: str, item: dict) -> RawPosting:
    return RawPosting(
        ats="workable", board_token=account, external_id=str(item["shortcode"]), title=(item.get("title") or "").strip(),
        location=_workable_location(item), url=item.get("url") or item.get("shortlink"), apply_url=item.get("application_url"),
        posted_at=from_external(item.get("published_on") or item.get("created_at")),
        description_text=html_to_text(item.get("description") or ""), requisition_id=item.get("code") or None,
        employment_type_hint=item.get("employment_type") or None, workplace_hint="remote" if item.get("telecommuting") else None,
        department=item.get("department") or None, updated_at=from_external(item.get("updated_at")), raw=item,
        typed_countries=_workable_typed(item))


def fetch_workable(client: HttpClient, account: str) -> list[RawPosting]:
    data = _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{_token(account)}?details=true")
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
        item = _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}/jobs/{_token(job_id)}")
        return greenhouse_posting(token, item)
    if ats == "lever":
        item = _get_json(client, f"https://api.lever.co/v0/postings/{_token(token)}/{_token(job_id)}")
        return lever_posting(token, item)
    if ats == "ashby":   # no single-posting endpoint: read the board and pick the job, an unlisted one too (2026-11-02:
        for item in _ashby_items(client, token):   # a link to an unlisted posting works, but the listing leaves it out)
            if str(item.get("id")) == job_id:
                return ashby_posting(token, item)
        raise FetchError("not_found", 404)
    if ats == "workable":  # no documented single-posting endpoint: read the board and pick the job
        for posting in FETCHERS[ats](client, token):
            if posting.external_id == job_id:
                return posting
        raise FetchError("not_found", 404)
    raise FetchError(f"unsupported_ats:{ats}")
