"""Reading an employer-controlled page as data (2026-10-13).

Finds, in one page's HTML, every place it points somewhere: links, buttons with link targets, frames, scripts,
canonical links, meta refreshes, form targets, data-* attributes, and job-board URLs written into inline scripts or
JSON (how most careers pages embed a board). Each target is classified:
- a board on one of the four supported systems (Greenhouse, Lever, Ashby, Workable), with its exact board token;
- an individual posting on such a board;
- a known system CareerOS never reads automatically (Workday, iCIMS, ... and other recognised ATSs): manual only;
- another site's job pages (unrecognised system).
Nothing is executed or followed here, and page text never changes what CareerOS does: an instruction written into a
page is just more text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from careeros.core import private_sites
from careeros.normalize.posting import contains_term, norm

from .board_urls import BOARD_HOSTS, MANUAL_SYSTEMS, parse_board_url

MAX_TARGETS = 3000
CAREERS_WORDS = ("careers", "career", "jobs", "job openings", "open positions", "open roles", "join us", "join our team",
                 "work with us", "work at", "we re hiring", "we are hiring", "opportunities", "openings", "hiring")
CAREERS_PATH = re.compile(r"/(careers?|jobs|join(-us)?|work-with-us|work-at[\w-]*|opportunities|openings|positions|hiring)(/|$|\.)", re.I)
COMMON_PATHS = ("/careers", "/jobs", "/company/careers", "/about/careers", "/join-us", "/work-with-us", "/careers/openings",
                "/company/jobs")
# Recognised application systems CareerOS does not support: their employers are useful, but manual only.
OTHER_SYSTEMS = (("workforcenow.adp.com", "adp"), ("recruiting.adp.com", "adp"), ("myjobs.adp.com", "adp"),
                 ("applytojob.com", "jazzhr"), ("jazzhr.com", "jazzhr"), ("recruitee.com", "recruitee"), ("breezy.hr", "breezy"),
                 ("teamtailor.com", "teamtailor"), ("jobs.personio.", "personio"), ("ats.rippling.com", "rippling"),
                 ("app.dover.com", "dover"), ("jobs.gem.com", "gem"), ("pinpointhq.com", "pinpoint"), ("comeet.com", "comeet"),
                 ("eightfold.ai", "eightfold"), ("avature.net", "avature"), ("paycomonline.net", "paycom"),
                 ("dayforcehcm.com", "dayforce"), ("hire.withgoogle.com", "google_hire"), ("zohorecruit.", "zoho_recruit"),
                 ("freshteam.com", "freshteam"), ("applicantpro.com", "applicantpro"), ("isolvedhire.com", "isolved"),
                 ("hrmdirect.com", "hrmdirect"), ("trinethire.com", "trinet"), ("phenompeople.com", "phenom"),
                 ("clearcompany.com", "clearcompany"), ("hirebridge.com", "hirebridge"), ("careers-page.com", "careers_page"),
                 ("polymer.co", "polymer"), ("workday.com", "workday"))
# Pages whose links are never a job system (social networks, aggregators, analytics).
_IGNORED_HOSTS = ("linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "tiktok.com",
                  "glassdoor.com", "indeed.com", "ziprecruiter.com", "google.com", "googletagmanager.com", "doubleclick.net",
                  "builtin.com", "wellfound.com", "angel.co", "crunchbase.com", "github.com", "medium.com", "apple.com")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_API_BOARDS = (  # board URLs written as API calls in embed scripts
    (re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9._-]+)", re.I), "greenhouse"),
    (re.compile(r"https?://api\.lever\.co/v0/postings/([A-Za-z0-9._-]+)", re.I), "lever"),
    (re.compile(r"https?://api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)", re.I), "ashby"),
    (re.compile(r"https?://apply\.workable\.com/api/v[0-9]+/widget/accounts/([A-Za-z0-9._-]+)", re.I), "workable"),
)
_INLINE_URL = re.compile(r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s\"'<>\\)\]}]*)?", re.I)
_SKIP_TEXT = {"script", "style", "noscript", "template", "svg"}


def valid_token(token: str | None) -> bool:
    return bool(token) and bool(_TOKEN.match(token))


def board_of(url: str) -> tuple[str, str] | None:
    """(ats, token) for a URL on a supported board (board page, posting, embed or listing API); None otherwise."""
    for pattern, ats in _API_BOARDS:
        match = pattern.match(url or "")
        if match and valid_token(match.group(1)):
            return ats, match.group(1)
    parsed = parse_board_url(url or "")
    return parsed if parsed and valid_token(parsed[1]) else None


def job_of(url: str) -> tuple[str, str, str] | None:
    """(ats, token, posting id) for a link to one posting on a supported board."""
    parts = urlsplit(url or "")
    host, segments = parts.netloc.lower(), [s for s in parts.path.split("/") if s]
    if host in BOARD_HOSTS["greenhouse"]:
        if len(segments) >= 3 and segments[1] == "jobs" and valid_token(segments[0]):
            return "greenhouse", segments[0], segments[2]
        query = parse_qs(parts.query)
        if segments[:2] == ["embed", "job_app"] and query.get("for") and query.get("token"):
            return "greenhouse", query["for"][0], query["token"][0]
    if host in BOARD_HOSTS["lever"] and len(segments) >= 2 and valid_token(segments[0]) and re.fullmatch(r"[0-9a-f-]{20,}", segments[1]):
        return "lever", segments[0], segments[1]
    if host in BOARD_HOSTS["ashby"] and len(segments) >= 2 and valid_token(segments[0]) and re.fullmatch(r"[0-9a-f-]{20,}", segments[1]):
        return "ashby", segments[0], segments[1]
    if host in BOARD_HOSTS["workable"] and len(segments) >= 3 and segments[1] == "j" and valid_token(segments[0]):
        return "workable", segments[0], segments[2]
    return None


def system_of(url: str) -> str | None:
    """Name of a recognised system CareerOS never reads automatically (Workday, iCIMS, ADP, ...), or None."""
    host = urlsplit(url or "").netloc.lower()
    for marker, name in MANUAL_SYSTEMS + private_sites.manual_systems() + OTHER_SYSTEMS:
        if marker in host:
            return name
    return None


@dataclass
class Target:
    url: str
    how: str                     # link | frame | script | canonical | refresh | form | data | inline
    text: str = ""


class _Collector(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.targets, self.title, self.meta, self._text = base, [], "", {}, []
        self._open_link, self._skip, self._in_title = None, 0, False

    def _add(self, raw: str | None, how: str, text: str = "") -> None:
        raw = (raw or "").strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")) or len(self.targets) >= MAX_TARGETS:
            return
        self.targets.append(Target(urljoin(self.base, raw), how, text))

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TEXT:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("a", "area"):
            self._open_link = [a.get("href"), a.get("aria-label", "") + " " + a.get("title", "")]
        elif tag in ("iframe", "frame"):
            self._add(a.get("src"), "frame")
        elif tag == "script":
            self._add(a.get("src"), "script")
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self._add(a.get("href"), "canonical")
        elif tag == "meta":
            if a.get("http-equiv", "").lower() == "refresh":
                match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", a.get("content", ""), re.I)
                if match:
                    self._add(match.group(1), "refresh")
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:site_name", "application-name", "og:title", "twitter:site", "author", "copyright"):
                self.meta[key] = a.get("content", "")[:300]
        elif tag == "form":
            self._add(a.get("action"), "form")
        elif tag == "button":
            for key in ("formaction", "data-href", "data-url"):
                self._add(a.get(key), "data")
        for key, value in a.items():
            if key.startswith("data-") and value.startswith(("http://", "https://", "//")):
                self._add(value, "data")

    def handle_endtag(self, tag):
        if tag in _SKIP_TEXT and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("a", "area") and self._open_link:
            href, text = self._open_link
            self._add(href, "link", re.sub(r"\s+", " ", text).strip()[:120])
            self._open_link = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._open_link is not None:
            self._open_link[1] += " " + data
        if not self._skip and len(self._text) < 4000:
            self._text.append(data)


@dataclass
class Inspection:
    url: str
    title: str = ""
    meta: dict = field(default_factory=dict)
    text: str = ""
    targets: list = field(default_factory=list)
    boards: dict = field(default_factory=dict)       # "ats:token(lower)" -> {"ats", "token", "how", "url"}
    jobs: list = field(default_factory=list)         # (ats, token, posting id, url)
    systems: dict = field(default_factory=dict)      # system name -> first URL (manual only)
    offsite_jobs: list = field(default_factory=list)  # job-like links on unrecognised outside hosts
    careers_links: list = field(default_factory=list)


def board_key(ats: str, token: str) -> str:
    return f"{ats}:{token.lower()}"


def inspect(html: str, base_url: str, site_domain: str) -> Inspection:
    collector = _Collector(base_url)
    try:
        collector.feed(html or "")
        collector.close()
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed so far
        pass
    unescaped = unescape((html or "").replace("\\/", "/"))
    targets = list(collector.targets)
    seen = {t.url for t in targets}
    for match in _INLINE_URL.finditer(unescaped):  # board URLs built by inline scripts / JSON
        if len(targets) >= MAX_TARGETS:
            break
        url = match.group(0).rstrip(".,;")
        if url not in seen and (board_of(url) or system_of(url)):
            seen.add(url)
            targets.append(Target(url, "inline"))
    result = Inspection(base_url, collector.title.strip()[:300], collector.meta, " ".join(collector._text)[:20000], targets)
    jobs_seen = set()
    for t in targets:
        host = urlsplit(t.url).netloc.lower()
        job = job_of(t.url)
        if job and board_key(job[0], job[1]) + f":{job[2]}" not in jobs_seen:
            jobs_seen.add(board_key(job[0], job[1]) + f":{job[2]}")
            result.jobs.append((*job, t.url))
        board = board_of(t.url)
        if board:
            key = board_key(*board)
            if key not in result.boards or t.how in ("frame", "link", "refresh"):
                result.boards[key] = {"ats": board[0], "token": board[1], "how": t.how, "url": t.url}
            continue
        system = system_of(t.url)
        if system:
            result.systems.setdefault(system, t.url)
            continue
        same_site = host == site_domain or host.endswith("." + site_domain)
        careerish = CAREERS_PATH.search(urlsplit(t.url).path or "") or any(contains_term(norm(t.text), w) for w in CAREERS_WORDS)
        if same_site and t.how in ("link", "refresh", "data") and careerish and t.url not in result.careers_links:
            result.careers_links.append(t.url)
        elif (not same_site and t.how == "link" and careerish and host and not host.endswith(_IGNORED_HOSTS)
              and t.url not in result.offsite_jobs):
            result.offsite_jobs.append(t.url)
    return result


def names_employer(inspection: Inspection, names: set[str]) -> str | None:
    """Where the page names the employer (title / site-name metadata / visible text), or None. Names shorter than 4
    characters only count in the title or site name."""
    places = (("title", inspection.title), ("site_name", " ".join(inspection.meta.values())), ("text", inspection.text))
    for place, text in places:
        text_norm = norm(text)
        for name in names:
            if not name or (len(name) < 4 and place == "text"):
                continue
            if contains_term(text_norm, name) or (" " in name and contains_term(text_norm.replace(" ", ""), name.replace(" ", ""))):
                return place
            joined = name.replace(" ", "")
            if len(joined) >= 6 and joined in text_norm.replace(" ", ""):
                return place
    return None
