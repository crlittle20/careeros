"""Job-alert emails you saved (.eml) become leads.

Links to the public job boards CareerOS may read (Greenhouse, Lever, Ashby, Workable) are fetched and graded like
any other posting. Every other link (LinkedIn, Indeed, Glassdoor, Workday,
SmartRecruiters, ...) is only listed for you: CareerOS never opens those sites. Email content is untrusted input; the
raw email is not stored, only the job link, its title, the text next to it and the alert's sender and subject.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from careeros.core import private_sites
from careeros.core.clock import format_utc
from careeros.normalize.sanitize import clean_text

from .boards import parse_job_url

MAX_EML_BYTES = 5 * 1024 * 1024
CONTEXT_CHARS = 160
GENERIC_TEXT = re.compile(r"^(view( job| details)?|apply( now)?|see (all|more)( jobs)?|easy apply|learn more|more jobs|"
                          r"unsubscribe|manage (alerts|settings)|search jobs|view all jobs|save|share|jobs? for you|"
                          r"\d+ new jobs?|new|promoted|actively recruiting)$", re.I)


class AlertEmailError(ValueError):
    pass


@dataclass
class Link:
    url: str
    site: str
    text: str = ""
    context: str = ""
    job_like: bool = False


@dataclass
class AlertEmail:
    sender: str
    subject: str
    received_at: str | None
    links: list[Link] = field(default_factory=list)


# --- links ---------------------------------------------------------------------------------------------
def _unwrap(url: str) -> str:
    """Follow redirect wrappers that carry the destination in a query parameter (no network access)."""
    for _ in range(3):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        inner = next((unquote(v[0]) for k, v in query.items() if k.lower() in ("url", "u", "dest", "destination", "target", "redirect")
                      and v and unquote(v[0]).startswith(("https://", "http://"))), None)
        if not inner:
            return url
        url = inner
    return url


def site_of(host: str) -> str:
    host = host.lower()
    for marker, site in (("linkedin.com", "linkedin"), ("indeed.com", "indeed"), ("glassdoor.", "glassdoor"),
                         ("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
                         ("workable.com", "workable"), ("smartrecruiters.com", "smartrecruiters"),
                         ("myworkdayjobs.com", "workday"),
                         ("ziprecruiter.com", "ziprecruiter"), ("dice.com", "dice"), ("usajobs.gov", "usajobs")):
        if marker in host:
            return site
    for marker, site in private_sites.manual_systems():  # systems tied to the job seeker's situation (vault)
        if marker in host:
            return site
    return "other"


def canonical_job_url(raw: str) -> tuple[str, str, bool] | None:
    """(url, site, looks_like_a_job_link) with tracking removed; None for links that are not web links."""
    url = _unwrap(raw.strip())
    if not url.startswith(("https://", "http://")):
        return None
    parts = urlsplit(url)
    host, path, query = parts.netloc.lower(), parts.path, parse_qs(parts.query)
    site = site_of(host)
    if site == "linkedin":
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d{6,})", path)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}", site, True
        job_id = (query.get("currentJobId") or [""])[0]
        return (f"https://www.linkedin.com/jobs/view/{job_id}", site, True) if job_id.isdigit() else (url, site, False)
    if site == "indeed":
        jk = (query.get("jk") or query.get("vjk") or [""])[0]
        if re.fullmatch(r"[0-9a-f]{8,}", jk):
            return f"https://www.indeed.com/viewjob?jk={jk}", site, True
        return url, site, False
    if site == "glassdoor":
        listing = (query.get("jobListingId") or query.get("jl") or [""])[0]
        return (f"https://www.glassdoor.com/job-listing/j?jl={listing}", site, True) if listing.isdigit() else (url, site, False)
    if parse_job_url(url):
        return f"https://{host}{path}".rstrip("/"), site, True
    job_path = bool(re.search(r"/(job|jobs|job-detail|careers?/job|requisition)s?/[^/]+", path, re.I))
    return f"https://{host}{path}" + (f"?{parts.query}" if parts.query and not job_path else ""), site, job_path


# --- parsing -------------------------------------------------------------------------------------------
class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[list] = []  # [href, text, context]
        self._open: list | None = None
        self._tail: list | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._open = [href, "", ""]
            self._tail = None
        elif tag in ("br", "p", "div", "tr", "td", "li") and self._tail is not None:
            self._tail[2] += " · "

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._open is not None:
            self.found.append(self._open)
            self._tail, self._open = self._open, None

    def handle_data(self, data):
        if self._skip:
            return
        if self._open is not None:
            self._open[1] += data
        elif self._tail is not None and len(self._tail[2]) < CONTEXT_CHARS * 2:
            self._tail[2] += data


def _tidy(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", clean_text(text or "")).strip()
    text = re.sub(r"(\s*·\s*)+", " · ", text).strip(" ·")
    return text[:limit]


def _links_from_html(markup: str) -> list[tuple[str, str, str]]:
    parser = _Anchors()
    parser.feed(markup)
    parser.close()
    return [(href, _tidy(text, 200), _tidy(context, CONTEXT_CHARS)) for href, text, context in parser.found]


def _links_from_text(body: str) -> list[tuple[str, str, str]]:
    """Plain-text alerts list each job as a block: title line, then company/location lines, then the link."""
    found, block = [], []
    for line in body.splitlines():
        urls = re.findall(r"https?://[^\s<>\")]+", line)
        label = _tidy(re.sub(r"https?://\S+", "", line), 200)
        if not urls:
            block = block + [label] if label else []
            continue
        title = label or (block[0] if block else "")
        context = _tidy(" · ".join(block[1:] if not label else block), CONTEXT_CHARS)
        found.extend((url, title, context) for url in urls)
        block = []
    return found


def parse_eml(path: Path) -> AlertEmail:
    if path.stat().st_size > MAX_EML_BYTES:
        raise AlertEmailError("email is larger than 5 MB")
    return parse_eml_bytes(path.read_bytes())


def parse_eml_bytes(data: bytes) -> AlertEmail:
    if len(data) > MAX_EML_BYTES:
        raise AlertEmailError("email is larger than 5 MB")
    message = email.message_from_bytes(data, policy=email.policy.default)
    try:
        received = format_utc(parsedate_to_datetime(message["Date"])) if message["Date"] else None
    except (TypeError, ValueError):
        received = None
    html_part = message.get_body(preferencelist=("html",))
    text_part = message.get_body(preferencelist=("plain",))
    if html_part is None and text_part is None:
        raise AlertEmailError("no readable body in the email")
    raw_links = _links_from_html(html_part.get_content()) if html_part is not None else _links_from_text(text_part.get_content())
    by_url: dict[str, Link] = {}
    for href, text, context in raw_links:
        canonical = canonical_job_url(href)
        if canonical is None:
            continue
        url, site, job_like = canonical
        meaningful = text if text and not GENERIC_TEXT.match(text) else ""
        link = by_url.setdefault(url, Link(url=url, site=site, job_like=job_like))
        if meaningful and not link.text:
            link.text, link.context = meaningful, context
    return AlertEmail(sender=_tidy(str(message["From"] or ""), 120), subject=_tidy(str(message["Subject"] or ""), 200),
                      received_at=received, links=list(by_url.values()))


_HOST_PREFIXES = {"www", "careers", "jobs", "job", "boards", "apply", "work", "join"}


def greenhouse_candidate(url: str) -> str | None:
    """A company careers page that embeds a Greenhouse posting (`?gh_jid=123`) usually uses its domain name as the
    Greenhouse board name. Returns that public Greenhouse link to try, or None. Wrong guesses just return not-found."""
    parts = urlsplit(url)
    job_id = (parse_qs(parts.query).get("gh_jid") or [""])[0]
    if not job_id.isdigit():
        return None
    labels = [l for l in parts.netloc.lower().split(":")[0].split(".") if l]
    while labels and labels[0] in _HOST_PREFIXES:
        labels = labels[1:]
    if len(labels) < 2:
        return None
    name = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") else labels[-2]
    return f"https://job-boards.greenhouse.io/{name}/jobs/{job_id}" if re.fullmatch(r"[a-z0-9-]+", name) else None


# LinkedIn (and others) send job alerts AND application updates from the same address (jobs-noreply@...), so the
# sender alone is not enough. Application, interview, assessment, offer and recruiter mail is never read for jobs.
_CORRESPONDENCE = re.compile(
    r"\b(your application|application (was )?(sent|received|submitted|viewed)|applied (to|for)|you applied|"
    r"thank(s| you) for (applying|your (application|interest))|application (status|update)|regarding your application|"
    r"received your application|viewed your (application|profile)|interview (invitation|request|confirmation|scheduled)|"
    r"(schedule|scheduling) (an|your) interview|interview with|invitation to interview|(online|skills?) assessment|"
    r"complete (the|your|an) assessment|assessment (invitation|link|request)|offer letter|job offer|next steps|"
    r"not (moving|move) forward|unfortunately|message from|wants to connect)\b", re.I)


def is_correspondence(subject: str | None) -> bool:
    """True for application, interview, offer or recruiter mail: never processed, even from an alert sender."""
    return bool(_CORRESPONDENCE.search(subject or ""))


def lead_id(url: str) -> str:
    return "lead:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
