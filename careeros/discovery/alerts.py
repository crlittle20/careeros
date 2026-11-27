"""Job-alert emails you saved (.eml) become leads.

Links to the public job boards CareerOS may read (Greenhouse, Lever, Ashby, Workable) are fetched and graded like
any other posting. Every other link (LinkedIn, Indeed, Glassdoor, Workday,
SmartRecruiters, ...) is only listed for you: CareerOS never opens those sites. Email content is untrusted input; the
raw email is not stored, only the job link, its title, the text next to it and the alert's sender and subject.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import email
import email.policy
import hashlib
import html as html_text
import quopri
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import idna   # httpx's own dependency, pinned in requirements.lock: a browser's IDNA tables

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


class MalformedLink(ValueError):
    """A link that is not a readable URL (urlsplit refuses it). Only this is skipped as malformed; any other error,
    such as an invalid private-sites file, is raised (2026-11-09 hotfix: 09d6c75 caught every ValueError and so hid
    a broken configuration as quietly dropped links)."""


@dataclass
class Link:
    url: str
    site: str
    text: str = ""
    context: str = ""
    job_like: bool = False
    # the card's occurrence key: its email's key and its position there (part 3 of the owner's D192 build; Codex's D190
    # design review H7): what a notice is stored under, so the same email read again adds nothing
    occurrence: str = ""

    def __post_init__(self):
        # the card's text and context in the canonical form (the D190 design, section 3a, point 5); the link as given
        from careeros.normalize.sanitize import canonical
        self.text = canonical(self.text or "")
        self.context = canonical(self.context or "")


@dataclass
class AlertEmail:
    sender: str
    subject: str
    received_at: str | None
    links: list[Link] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # links left out, by reason: navigation, malformed (counted)
    # An agency digest (private_sites `agency_digests`) only: "complete" (every job card read) or "partial" (a card could
    # not be read, or none was found: the email is held back for a retry and you are told). Mail from the digest
    # sender with no card CareerOS recognises is never taken as a newsletter: a changed layout would look the same
    # (Codex CR-2026-11-09-AGENCY-DIGESTS, CR-AD-01).
    digest_status: str | None = None
    digest_reason: str | None = None   # why a digest is "partial": "cards", "layout", "no layout recorded" or "plain text"
                                       # ("correspondence": a recognised digest that reads like application mail)
    message_key: str = ""   # the email's key: "gmail:<id>" for one pulled from Gmail, else "eml:<its bytes' digest>"

    def __post_init__(self):
        # the sender and subject in the canonical form (the D190 design, section 3a, point 5)
        from careeros.normalize.sanitize import canonical
        self.sender = canonical(self.sender) if isinstance(self.sender, str) else self.sender
        self.subject = canonical(self.subject) if isinstance(self.subject, str) else self.subject


# --- links ---------------------------------------------------------------------------------------------
def _split(url: str):
    try:
        return urlsplit(url)
    except ValueError as exc:  # e.g. a broken IPv6 host
        raise MalformedLink(str(exc)) from exc


# Search, browse and collection pages of LinkedIn, Indeed and Glassdoor: the headers and "see all" links of an alert
# digest ("Your job alert for Data Analyst"), never one posting. Only links of these shapes are skipped as navigation.
# Any other link of those sites without a job ID (a sponsored or tracking link, a new format) stays a link for you to
# open, as before 09d6c75, which skipped every link without a known ID and so lost real jobs (2026-11-09 hotfix).
_NAVIGATION = {
    "linkedin": re.compile(r"^(?:/comm)?/jobs(?:/(?:search|collections|alerts)(?:[/-][^?#]*)?)?/?$", re.I),
    "indeed": re.compile(r"^(?:/m)?/jobs/?$|^/(?:m/)?[^/]*-jobs\.html$|^/cmp/[^/]+/jobs/?$", re.I),
    "glassdoor": re.compile(r"^/Jobs?/[^/]+\.htm$", re.I),
}


def is_navigation(site: str, path: str) -> bool:
    """A job-search, browse or collection page of a big job site, with no job named in it."""
    pattern = _NAVIGATION.get(site)
    return bool(pattern and pattern.match(path or "/"))


def _tracker_hosts() -> frozenset[str]:
    """Hosts of the agencies' personal click-tracking links (private_sites `agency_digests`, with any former hosts). A
    broken private-sites file raises here, loudly: it is never read as "no trackers"."""
    return frozenset(host for digest in private_sites.agency_digests() for host in (digest.tracker_host, *digest.former_hosts))


_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})   # ideographic and full-width full stops


# Characters a browser drops from a host name (IDNA mapping: soft hyphen, zero-width and joiner marks, variation
# selectors, byte-order mark and other default-ignorable code points)
_IGNORABLE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
                        "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
_WEB = re.compile(r"(https?):[\\/]*", re.I)


_URL_IGNORED = re.compile(r"[\t\n\r]")   # a browser removes these from a link wherever they are (WHATWG URL)


_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"))


def _element(tag: str, attrs) -> str:
    """An element as the layout records it around a link: its tag and every attribute with its value (a link's own
    destination excepted, as it names the job), a style without its spaces."""
    def value(name, text):
        return re.sub(r"\s+", "", (text or "").lower()) if name == "style" else (text or "")
    return tag + "[" + ";".join(sorted(f"{name}={value(name, text)}" for name, text in attrs if name != "href")) + "]"


_ESCAPE_ROUNDS = 10   # percent-escape decoding goes on until nothing changes, at most this often (red team before Pass 4)
_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def _uts46(char: str) -> str:
    """One character as a browser's IDNA mapping (UTS 46) reads it in a host name, from the idna package's tables,
    which follow newer Unicode versions than Python's own (an outlined letter of Unicode 16, red team before Pass 4). A
    character IDNA refuses is kept as it is."""
    try:
        return idna.uts46_remap(char, std3_rules=False, transitional=False)
    except (idna.IDNAError, ValueError):
        return char


def _fold(text: str) -> str:
    """Text as a host comparison sees it: tabs and line breaks dropped as a browser drops them from a link, before and
    after each round of percent-escape decoding, rounds repeated until nothing changes (an escaped tab inside a wrapped
    link, Codex Pass 3; wrappers nested four deep, the red team before Pass 4), compatibility forms folded (NFKC),
    then every other character mapped as a browser's IDNA maps it and the whole put in lower case, ideographic and full-width dots read as dots,
    invisible characters dropped, backslashes read as slashes (L1-03, L1-04, CR-AD-V2-03). Case is folded as a browser
    folds a host (UTS 46, not transitional), never Python's casefold, which reads "ß" as "ss" (Codex Pass 4)."""
    for _ in range(_ESCAPE_ROUNDS):
        decoded = unquote(_URL_IGNORED.sub("", text))
        if decoded == text:
            break
        text = decoded
    text = _URL_IGNORED.sub("", text)
    text = unicodedata.normalize("NFKC", text).translate(_DOTS)
    text = _NON_ASCII.sub(lambda match: _uts46(match.group()), text).lower()
    return _IGNORABLE.sub("", text).replace("\\", "/")


def _browser_url(url: str | None) -> str:
    """A web link as a browser reads the part that decides its host (WHATWG URL): spaces and control characters at the
    ends dropped, tabs and line breaks removed, backslashes read as slashes, and any number of slashes (or none) after
    http(s): taken as the start of the host. Python's urlsplit does none of this, so a backslash could put the two on
    different hosts (CR-AD-V2-03, Pass 2)."""
    text = re.sub(r"[\t\n\r]", "", (url or "").strip("".join(map(chr, range(0x21)))))
    match = _WEB.match(text)
    if not match:
        return text
    return f"{match.group(1).lower()}://" + text[match.end():].replace("\\", "/")


def _spellings(trackers: frozenset[str]) -> frozenset[str]:
    """Each tracking host as written in the settings (plain ASCII, an international name as its xn-- form) and, for an
    international name, as a browser shows it too ("fuß.example.com" for "xn--fu-hia.example.com", Codex Pass 4)."""
    out = set(trackers)
    for host in trackers:
        if "xn--" in host:
            try:
                out.add(idna.decode(host))
            except (idna.IDNAError, UnicodeError, ValueError):
                pass
    return frozenset(out)


def _mentions_tracker(url: str | None, trackers: frozenset[str]) -> bool:
    """Whether a link carries a tracking link inside it: a mail scanner's or a search engine's redirect wrapper with the
    tracking link in a parameter or a path, escaped up to three times (L1-03)."""
    text = _fold(url or "")
    return any(re.search(r"(?<![a-z0-9-])" + re.escape(host) + r"\.?(?![a-z0-9.-])", text) for host in _spellings(trackers))


def _text_mentions_tracker(text: str, trackers: frozenset[str]) -> bool:
    """Whether any text (an email's HTML or plain part, a link) names a tracking host anywhere, in any case, escaped,
    wrapped or split by invisible characters, a tab or a line break inside a link included (a browser drops them; Codex
    Pass 3), and also with each tab or line break read as a space, so a host at the start of a line is never joined to
    the word before it: the evidence that mail is the agency's (CR-AD-V2-06, Pass 2)."""
    unescaped, spellings = html_text.unescape(text or ""), _spellings(trackers)
    for folded in (_fold(unescaped), _fold(_URL_IGNORED.sub(" ", unescaped))):
        if any(re.search(r"(?<![a-z0-9-])" + re.escape(host) + r"\.?(?![a-z0-9.-])", folded) for host in spellings):
            return True
    return False


_EMBEDDED = re.compile(r"(?:https?:/*|//)([^/?#\s]*)")   # a host after a scheme or "//", in the link or inside it
_TOKEN = re.compile(r"[^\s/?#&=@:;,'\"<>()\[\]|^]+")      # a run of characters a host name could be


def unreadable_host(url: str | None) -> bool:
    """Whether a link, or a link carried inside it (a scanner's or a search engine's wrapper, escaped any number of
    times), has a host CareerOS cannot read as a browser does: a character outside ASCII once a browser's own mapping is
    applied, or an xn-- label (Codex Pass 5: a host with a middle dot, U+00B7, that a browser accepts and Python's IDNA
    refuses).
    Such a link is a tracking link: kept local, never written to the shared index, an export or GitHub, and never
    matched (D153 R5). An ASCII host reads the same to both, so only these are judged."""
    if not url:
        return False
    text = _fold(_browser_url(url))
    hosts = [m.group(1).rsplit("@", 1)[-1] for m in _EMBEDDED.finditer(text)]
    hosts += [t for t in _TOKEN.findall(text) if "." in t.strip(".")]
    for host in hosts:
        host = re.sub(r":[0-9]*$", "", host).strip(".")
        if host and (not host.isascii() or re.search(r"(?:^|\.)xn--", host)):
            return True
    return False


def _norm_host(url: str | None) -> str | None:
    """A link's host as a browser would reach it (CR-AD-V2-03): the link read as a browser reads it (_browser_url), lower
    case, percent-escapes decoded, compatibility forms folded, no trailing dot, international names in their ASCII form.
    None for a link without a readable host."""
    try:
        host = urlsplit(_browser_url(url)).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = _fold(host).strip().rstrip(".")
    if not host.isascii():   # an international name in its xn-- form, as a browser sends it (non-transitional UTS 46)
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except (idna.IDNAError, UnicodeError, ValueError):
            pass
    return host or None
# The fields a big job site names a job in, aliases included (query and "#" fragment alike)
_ID_FIELDS = {"linkedin": ("currentJobId", "jobId"), "indeed": ("jk", "vjk"), "glassdoor": ("jobListingId", "jl")}
_ID_FORMS = {"linkedin": re.compile(r"[0-9]+"), "indeed": re.compile(r"[0-9a-f]{8,}"), "glassdoor": re.compile(r"[0-9]+")}


def _id_values(site: str, parts) -> list[str]:
    """Every non-empty value of every job-ID field of the link, in the query and the fragment, in order. Field names
    are read in any capitals ("CurrentJobId", "JK"): CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-01 (D126)."""
    found = []
    names = {name.lower() for name in _ID_FIELDS.get(site, ())}
    for part in (parts.query, parts.fragment):
        for key, values in parse_qs(part).items():
            if key.strip().lower() in names:
                found += [v.strip().lower() if site == "indeed" else v.strip() for v in values]
    return found


def _one_job_id(site: str, parts) -> str | None:
    """The one job a link names: every value of every ID field is checked before any is chosen, so an unreadable value
    never hides a readable one ("?currentJobId=unavailable&jobId=9123456"), and two different readable IDs name no
    single job (kept for review, never chosen by their order). CR-2026-11-09-INTAKE-HOTFIX CR-HF-01."""
    ids = list(dict.fromkeys(v for v in _id_values(site, parts) if _ID_FORMS[site].fullmatch(v)))
    return ids[0] if len(ids) == 1 else None


def names_a_job_id(site: str, url: str) -> bool:
    """Whether a link carries a value in one of its site's job-ID fields: it then names a job (or tries to), so it is
    never skipped as a search page; when the ID can't be read as one job, the link is kept for review."""
    return site in _ID_FIELDS and bool(_id_values(site, _split(url)))


def _inner(url: str) -> str | None:
    """The destination a redirect wrapper carries in a query parameter, or None."""
    query = parse_qs(_split(url).query)
    return next((unquote(v[0]) for k, v in query.items() if k.lower() in ("url", "u", "dest", "destination", "target", "redirect")
                 and v and unquote(v[0]).startswith(("https://", "http://"))), None)


def _unwrap(url: str) -> str:
    """Follow redirect wrappers that carry the destination in a query parameter (no network access)."""
    trackers = _tracker_hosts()
    for _ in range(3):
        if _norm_host(url) in trackers:  # an agency's tracking link is kept whole: its query is not a destination
            break
        inner = _inner(url)
        if not inner:
            return url
        url = inner
    return url


def _layers(url: str, limit: int = 12) -> list[str]:
    """The link and every link it wraps, outermost first, to the fully unwrapped one (bounded; no network access)."""
    layers = [url]
    while len(layers) < limit:
        inner = _inner(layers[-1])
        if not inner or inner in layers:
            break
        layers.append(inner)
    return layers


_WRAPPER_FIELDS = ("url", "u", "dest", "destination", "target", "redirect")
_INVISIBLE = re.compile("[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _normal(text: str) -> str:
    """NFKC, without zero-width and other invisible characters ("１１１" is "111", "current\u200bJobId" is "currentJobId")."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


def _walk(url: str, depth: int = 12, most: int = 64) -> tuple[list[str], bool]:
    """(layers, complete): the link (as received and normalized) and every link any of its wrapper fields carries, in the
    query or after the "#", each value read decoded once and decoded twice, outermost first, plus _layers' chain; at most
    `depth` layers deep and `most` links in all (no network access). `complete` is False when the walk stopped at a
    bound with links left to read, or met a wrapper value that is no readable absolute link (a token, another scheme;
    "//host/..." is read as https): the owner's D151 rule 2 and D155 rule a, the caller then treats the link as a review
    link, never as a definite one. A wrapped link that cannot be parsed raises MalformedLink: a review link too."""
    found: list[str] = []
    queue: list[tuple[str, int]] = []
    complete = True

    def add(link: str, level: int) -> None:
        nonlocal complete
        if link in found:
            return
        if len(found) >= most or level > depth:
            complete = False
            return
        found.append(link)
        queue.append((link, level))

    add(url, 1)
    add(_normal(url), 1)
    for level, layer in enumerate(_layers(url, depth), 1):
        add(layer, level)
    while queue:
        layer, level = queue.pop(0)
        parts = _split(layer)
        for part in (parts.query, parts.fragment):
            for key, values in parse_qs(part).items():
                if _normal(key).strip().lower() not in _WRAPPER_FIELDS:
                    continue
                for value in values:   # every value is a destination reading (the owner's D155 rule a)
                    readable = False
                    for inner in (value, unquote(value)):
                        inner = _normal(inner).strip()
                        if inner.startswith("//"):
                            inner = "https:" + inner   # a scheme-relative link, read as https (D155 rule a)
                        scheme, sep, rest = inner.partition("://")
                        if sep and scheme.lower() in ("http", "https"):
                            readable = True
                            add(f"{scheme.lower()}://{rest}", level + 1)
                    if not readable:
                        complete = False   # a wrapped link the reader cannot follow (a token, another scheme)
    return found, complete


def _every_layer(url: str) -> list[str]:
    """_walk's layers (D149 R-I1)."""
    return _walk(url)[0]


def _jobs_named(layers: list[str]) -> set[str]:
    """Every job the layers name, by any reading (the owner's D151 rule 1): on LinkedIn, Indeed and Glassdoor the job
    in the path (/jobs/view/N) and every readable value of every job-ID field (query and "#"), on other sites the link's
    own job reading (a board posting, a job page)."""
    jobs: set[str] = set()
    for layer in layers:
        parts = _split(layer)
        site = site_of(parts.netloc)
        if site in _ID_FIELDS:
            # an unreadable value ("unavailable", letters) is a reading that disagrees with any other (D151 limit 2 closed)
            jobs |= {f"{site}:{v}" if _ID_FORMS[site].fullmatch(v) else f"{site}:unreadable:{v}" for v in _id_values(site, parts)}
            match = re.search(r"/jobs/view/(?:[^/]*-)?(\d{6,})", parts.path, re.I) if site == "linkedin" else None
            if match:
                jobs.add(f"linkedin:{match.group(1)}")
        else:
            reading = _reading(layer)
            if reading is not None and reading[2]:
                jobs.add(reading[0])
    return jobs


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


def is_agency_tracking_url(url: str | None) -> bool:
    """A personal click-tracking link from an agency job-alert digest: a local link for you to open, never fetched, never
    an external posting identifier and never exported."""
    trackers = _tracker_hosts()  # outside the try: a broken private-sites file is an error, never "not a tracker"
    if not url:
        return False
    if unreadable_host(url):   # a host CareerOS cannot read as a browser does: local, whatever the settings (D153 R5)
        return True
    return bool(trackers) and (_norm_host(url) in trackers or _mentions_tracker(url, trackers))


def canonical_job_url(raw: str) -> tuple[str, str, bool] | None:
    """(url, site, looks_like_a_job_link) with tracking removed; None for links that are not web links."""
    url = _unwrap(raw.strip())
    if not url[:8].lower().startswith(("https://", "http://")) or is_local_link(url):
        return None
    return _reading(url)


def is_local_link(url: str | None) -> bool:
    """A link CareerOS keeps local: an agency's tracking link (is_agency_tracking_url, which includes a host CareerOS
    cannot read as a browser does, D153 R5), or a link whose host a browser may fold onto another (M1-01).
    canonical_job_url never reads such a link as a lead. A review link the combined code keeps under a link other than its
    canonical form (the owner's D149 R-I1: a layer or the href; D155 rule b: the outer link) may be one: the owner's D156
    keeps it as a review lead, and alert intake asks this to store it as a local lead (the agency mark), so it is never
    exported, published or matched."""
    if not url or is_agency_tracking_url(url):
        return bool(url)
    try:
        shown = _fold(urlsplit(_browser_url(url)).hostname or "")
    except ValueError:
        shown = ""
    if not shown.isascii():   # a host a browser may fold onto another, whatever IDNA makes of it (M1-01): never a lead
        return True
    return False


def _reading(url: str) -> tuple[str, str, bool] | None:
    """canonical_job_url's reading of one link as it stands, without unwrapping it."""
    if not url.startswith(("https://", "http://")):
        return None
    parts = _split(url)
    host, path = parts.netloc.lower(), parts.path
    site = site_of(host)
    if site == "linkedin":
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d{6,})", path, re.I)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}", site, True
        job_id = _one_job_id(site, parts)  # "/jobs/search/#currentJobId=..." names the job after the "#"
        return (f"https://www.linkedin.com/jobs/view/{job_id}", site, True) if job_id else (url, site, False)
    if site == "indeed":
        jk = _one_job_id(site, parts)
        return (f"https://www.indeed.com/viewjob?jk={jk}", site, True) if jk else (url, site, False)
    if site == "glassdoor":
        listing = _one_job_id(site, parts)
        return (f"https://www.glassdoor.com/job-listing/j?jl={listing}", site, True) if listing else (url, site, False)
    if parse_job_url(url):
        return f"https://{host}{path}".rstrip("/"), site, True
    job_path = bool(re.search(r"/(job|jobs|job-detail|careers?/job|requisition)s?/[^/]+", path, re.I))
    # an app route after "#" names the opening ("/portal#/opening/R-401"): kept, so two openings of one portal stay two
    # leads; a page anchor or a text link is dropped (Codex CR-2026-11-15-GATE-FIXES H18, Pass 11; D179/D180)
    route = parts.fragment.rstrip("/") if parts.fragment.startswith(("/", "!/")) else ""
    query = parts.query
    if job_path and query:
        # a job path's query stays whole, tracking aside, as the posting's own link keeps it (normalize.posting_link):
        # the parameter naming the opening is never dropped, whatever its name ("ashby_jid", "id", "pid", "jobReqId"), so
        # two openings of one page stay two leads (the red team after Codex Pass 11, RT3-5; before Pass 15, RT-23)
        from careeros.normalize.posting import _TRACKING
        query = "&".join(sorted(p for p in query.split("&") if p and not _TRACKING.match(p.split("=", 1)[0])))
    return (f"https://{host}{path}" + (f"?{query}" if query else "") + (f"#{route}" if route else ""),
            site, job_path or bool(route))


# --- parsing -------------------------------------------------------------------------------------------
_CARD_BLOCKS = ("p", "div", "tr", "td", "th", "li", "ul", "ol", "table", "tbody", "thead", "section", "article")


class _Anchors(HTMLParser):
    """Each link with its text and its card's other words (its context): the words after it, up to the next link. A
    card's own parts stay its own (the red team's gr1-alerts-1): a link that follows another with no block started
    between them (the employer's name linked, a Save link) is in the same card, so the words after it are the earlier
    link's context too; and the words of a block that began after the previous link and is still open when the next
    link starts (the employer and the place written above the title, in the title's own cell) are the next link's,
    never the previous card's. Words in a block that began and ended between two links stay the earlier link's."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[list] = []  # [href, text, context]
        self._open: list | None = None
        self._lead = ""   # the open link's lead-in: its card's words before it, in a block begun after the last link
        self._tails: list[list] = []   # the links of the card being read: the words that follow are their context
        self._blocks: list[list] = []   # open blocks: [tag, began after the last link, its words since then]
        self._block_since_link = False
        self._joined = False
        self._skip = 0

    def _emit(self, text: str) -> None:
        for tail in self._tails:
            if len(tail[2]) < CONTEXT_CHARS * 10:   # read whole before any cut (RT-11)
                tail[2] += text
        for block in self._blocks:
            if block[1]:
                block[2] += text

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):   # a <head> never hides a link: browsers ignore it in the body (CR-AD-V2-01)
            self._skip += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            # the innermost block that began after the previous link, is still open and has words: the new card's
            lead_in = next((b[2] for b in reversed(self._blocks) if b[1] and b[2].strip(" ·\n\t\r")), "")
            if lead_in:
                for tail in self._tails:
                    if tail[2].endswith(lead_in):
                        tail[2] = tail[2][:-len(lead_in)]
            self._joined = not self._block_since_link and bool(self._tails)
            if not self._joined:
                self._tails = []
            self._open = [href, "", lead_in + " · " if lead_in else ""]
            self._lead = lead_in
        elif tag == "br" or tag in _CARD_BLOCKS:
            self._emit(" · ")
            if tag in _CARD_BLOCKS and self._open is None:
                self._blocks.append([tag, True, ""])
                self._block_since_link = True

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._open is not None:
            words = re.sub(r"\s+", " ", self._open[1]).strip()
            parts = [p.strip() for p in re.split(r"\s*·\s*", self._lead) if p.strip()]
            if words and GENERIC_TEXT.match(words) and parts and not GENERIC_TEXT.match(parts[0]):
                # a card whose only link is a button ("View Job", "Apply now"): its title is the card's own text read
                # before the button, in its own block (its lead-in), and the rest of that text stays its context; a
                # button after a linked title has no lead-in of its own and stays untitled (BATCH_5 F3)
                self._open[1], self._open[2] = parts[0], self._open[2].replace(parts[0], "", 1)
            if self._joined:
                self._emit(" · " + self._open[1])
            self.found.append(self._open)
            self._tails = [*self._tails, self._open]
            self._open = None
            for block in self._blocks:   # every block open now began before this link ended
                block[1], block[2] = False, ""
            self._block_since_link = False
        elif tag in _CARD_BLOCKS:
            for i in range(len(self._blocks) - 1, -1, -1):
                if self._blocks[i][0] == tag:
                    del self._blocks[i:]
                    break

    def handle_data(self, data):
        if self._skip:
            return
        if self._open is not None:
            self._open[1] += data
        else:
            self._emit(data)


def _tidy(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", clean_text(text or "")).strip()
    text = re.sub(r"(\s*·\s*)+", " · ", text).strip(" ·")
    return text[:limit]


def _line_context(text: str) -> str:
    """A card's text after its link, tidied and cut to CONTEXT_CHARS. What the cut drops is never lost to the card's
    identity: a requisition it states is written at the end ("... · Job ID: R-8102", RT-11), and when anything else is
    cut (a place, the pay), a digest of the whole card is too ("... · card 3f9a1c2e"), so two cards that differ only
    after the cut never become one lead (the red team's fr1-alerts-0)."""
    import hashlib
    from careeros.discovery.manual_intake import requisition_in, requisitions_in
    whole = _tidy(text, 10 * CONTEXT_CHARS)
    if len(whole) <= CONTEXT_CHARS:
        return whole
    stated = requisitions_in(whole)
    tail = (" · Job ID: " + (requisition_in(whole) or "unclear (" + ", ".join(stated) + ")")
            if stated and requisitions_in(whole[:CONTEXT_CHARS]) != stated else "")
    tail += " · card " + hashlib.sha256(whole.encode("utf-8")).hexdigest()[:8]
    return (whole[:max(0, CONTEXT_CHARS - len(tail))].rstrip(" ·") + tail)[:CONTEXT_CHARS * 2]


# An "&" in an href that starts a query parameter ("&currentJobId=9123456") is the parameter, never an HTML entity: the
# parser would read "&curren" as "¤" and lose the job ID. Such an "&" is escaped first; "&amp;" and real entities
# ("&quot;", "&#38;") are left alone (CR-2026-11-09-INTAKE-HOTFIX Pass 3, D126 C4)
_HREF = re.compile(r"""(\bhref\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_PARAMETER_AMP = re.compile(r"&(?=[A-Za-z_][A-Za-z0-9_.\-\[\]]*=)")


def _escape_href_ampersands(markup: str) -> str:
    return _HREF.sub(lambda m: m.group(1) + _PARAMETER_AMP.sub("&amp;", m.group(2)), markup)


# Facts a card may state beside its title, both optional (Codex CR-AD-03, CR-AD-04). Pay: an amount or a range that
# pay wording introduces ("Pay: $25 - $30") or that is a card line of its own ("$25/hr"); any other amount, with or
# without a period ("budgets of $500,000", "vendor bill rates of $85/hour"), is not pay. Job number: only a card line that is a labelled identifier and
# nothing else ("Job ID: 04530-0012345678"), and only when one such line is on the card; a number in the description
# ("for a different opening, see Job #...") is never the card's identity, which then stays its exact link.
_AMOUNT = r"\$\s?\d[\d,]*(?:\.\d{1,2})?\s?[kK]?(?:\s*(?:-|\u2013|to)\s*\$?\s?\d[\d,]*(?:\.\d{1,2})?\s?[kK]?)?"
_PERIOD = r"\s*(?:/|per)\s*(?:hr|hour|wk|week|yr|year|annum)\b"
_PAY_WORDS = r"\b(?:pay(?:\s+rate|\s+range)?|salary(?:\s+range)?|compensation|wages?|hourly\s+rate|base\s+pay)\s*(?:is\s+|of\s+)?[:\-\u2013]?\s*"
_CARD_PAY = re.compile(rf"{_PAY_WORDS}({_AMOUNT}(?:{_PERIOD})?)", re.I)   # a period alone is not pay (CR-AD-04, Pass 2)
_CARD_PAY_LINE = re.compile(rf"(?:{_PAY_WORDS})?({_AMOUNT}(?:{_PERIOD})?)\.?", re.I)
_CARD_JOB_LINE = re.compile(r"(?:job\s*(?:id|#|no\.?|number)|req(?:uisition)?\s*(?:id|#|no\.?|number)?)\s*[:#]?\s*"
                            r"((?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,30})\.?", re.I)
_NOTE_VALUE = re.compile(r"[A-Za-z0-9 .,:/$%&+()~#'\-\u2013]{1,40}")   # what the Note reader accepts as a pay value


def _card_text(text: str, limit: int) -> str:
    """A card line as plain text: the characters the Note reader treats as field marks (; = and the separator dot)
    become spaces, so card text can describe a job but never forge a req=, pay= or agency= field."""
    return re.sub(r"\s+", " ", re.sub(r"[;=\u00b7]", " ", _tidy(text, 400))).strip()[:limit]


def _card_context(name: str, details: list[str]) -> str:
    """The lead's context line, "Agency · location", plus the card's pay and job number as Note fields when it states
    them. The first detail line is the location; a long one (a description) is not shown."""
    texts = [_card_text(text, 400) for text in details]
    parts = [name]
    jobs = {m.group(1) for t in texts for m in [_CARD_JOB_LINE.fullmatch(t)] if m}
    if texts and texts[0] and len(texts[0]) <= 80 and not (_CARD_JOB_LINE.fullmatch(texts[0]) or _CARD_PAY_LINE.fullmatch(texts[0])):
        parts.append(texts[0])
    fields = []
    pay = next((m.group(1) for t in texts for m in [_CARD_PAY_LINE.fullmatch(t)] if m), None) or next(
        (m.group(1) for t in texts for m in [_CARD_PAY.search(t)] if m), None)
    if pay and _NOTE_VALUE.fullmatch(pay.strip()):
        fields.append(f"pay={pay.strip()}")
    if len(jobs) == 1:  # two different job-number lines: no identity rather than the wrong one
        fields.append(f"req={jobs.pop()}")
    return " \u00b7 ".join(parts + (["; ".join(fields)] if fields else []))


_RAW_TEXT = ("script", "style")   # text to browsers and to Python's parser alike, up to their own end tag
_TEXT_ONLY = ("title", "textarea", "xmp", "iframe", "noembed", "noframes", "noscript")   # text (or nothing) to browsers
_TAG_NAME = re.compile(r"[A-Za-z][^\t\n\f\r />]*")
_ATTR_NAME = re.compile(r"[a-z_:][a-z0-9_:.-]*")
_COMMENT_END = re.compile(r"--!?>")   # a browser ends a comment at "-->" or "--!>", whichever comes first
_SPACE = "\t\n\f\r "
_CLICK_ATTRS = ("href", "action", "formaction", "xlink:href")
# Elements that make a link or a form, or show text a browser and Python's parser read differently: a digest that has
# one is held, never read around (CR-AD-V2-01, Pass 2). Real digests have none (2026-11-11, structure check).
_UNREAD_ELEMENTS = frozenset(("script", "area", "map", "form", "button", "input", "select", "textarea", "iframe", "frame",
                              "frameset", "object", "embed", "applet", "template", "xmp", "plaintext", "noembed", "noframes",
                              "noscript", "svg", "math", "base", "portal", "link"))
# CSS that can hide a link's text or show other text in its place (Codex Pass 3, CR-AD-V2-01), matched in declarations
# read as a browser reads them (_css_plain, no spaces): hidden, invisible, zero or tiny size, moved away, reordered, or
# drawn by the style sheet (generated content, an image). A link that has any of it, or a style sheet rule that can
# reach one, holds the digest: the reader would read text a browser does not show. Real digests have none (2026-11-11,
# count check of the two saved digests).
_RELABEL = re.compile(
    r"(?:^|;|\{)(?:display:none|visibility:(?:hidden|collapse)|opacity:0(?:\.0+)?(?=;|!|\}|$)|"
    r"font-size:(?:0(?:\.0+)?(?:[a-z%]+)?|[0-3](?:\.\d+)?(?:px|pt))(?=;|!|\}|$)|"
    r"(?:max-)?(?:height|width):0(?:\.0+)?(?:[a-z%]+)?(?=;|!|\}|$)|line-height:0(?:\.0+)?(?:[a-z%]+)?(?=;|!|\}|$)|"
    r"mso-hide:all|color:transparent|-webkit-text-fill-color:transparent|text-indent:-|clip(?:-path)?:|"
    r"position:(?:absolute|fixed)|transform:|unicode-bidi:|direction:rtl|letter-spacing:-|content:)"
    r"|(?:url|image-set|-webkit-image-set|element)\(")
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n\r\f]?|\\(.)", re.S)


def _css_text(css: str) -> str:
    """CSS as a browser reads it before matching: comments and the "<!--" "-->" an email wraps it in dropped, escapes
    decoded ("cont\\65nt" is "content"), lower case."""
    css = re.sub(r"/\*.*?(?:\*/|$)", "", css, flags=re.S).replace("<!--", " ").replace("-->", " ")

    def char(match):
        if match.group(1):
            code = int(match.group(1), 16)
            return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"
        return match.group(2)
    return _CSS_ESCAPE.sub(char, css).lower()


def _css_plain(css: str) -> str:
    """CSS declarations without spaces, for _RELABEL."""
    return re.sub(r"\s+", "", _css_text(css))


def _reaches(selector: str, tags: set[str], classes: set[str], ids: set[str]) -> bool:
    """Whether a CSS selector can apply to a link or to something inside one: its last compound (the element it
    styles) names no tag, class or id that no link part has. Pseudo-classes and attributes only narrow it, so they count
    as able to match."""
    subject = re.split(r"[\s>+~]+", selector.strip())[-1] if selector.strip() else ""
    subject = re.sub(r"\[[^\]]*\]?|::?[-a-z0-9]+(?:\([^)]*\)?)?", "", subject)
    tag = re.match(r"[a-z][a-z0-9-]*|\*", subject)
    if tag and tag.group(0) != "*" and tag.group(0) not in tags:
        return False
    return (all(c in classes for c in re.findall(r"\.([^.#:\[\s]+)", subject))
            and all(i in ids for i in re.findall(r"#([^.#:\[\s]+)", subject)))


def _read_tag(markup: str, i: int) -> tuple[int, list[tuple[str, str | None]]]:
    """The attributes of a tag whose name ends at `i`, read as a browser tokenizes them, and the index just after its
    ">" (len(markup) when it never ends): a quoted value may hold ">", the first character of a name is always part of
    it, and a repeated attribute keeps its first value."""
    n, seen = len(markup), set()
    attrs: list[tuple[str, str | None]] = []
    while i < n:
        c = markup[i]
        if c == ">":
            return i + 1, attrs
        if c in _SPACE or c == "/":
            i += 1
            continue
        start, i = i, i + 1
        while i < n and markup[i] not in _SPACE + "/>=":
            i += 1
        name, j, value = markup[start:i].lower(), i, None
        while j < n and markup[j] in _SPACE:
            j += 1
        if j < n and markup[j] == "=":
            j += 1
            while j < n and markup[j] in _SPACE:
                j += 1
            if j < n and markup[j] in "\"'":
                close = markup.find(markup[j], j + 1)
                value = markup[j + 1:close if close >= 0 else n]
                j = n if close < 0 else close + 1
            else:
                k = j
                while j < n and markup[j] not in _SPACE + ">":
                    j += 1
                value = markup[k:j]
            i = j
        if name not in seen:
            seen.add(name)
            attrs.append((name, value))
    return n, attrs


def _tag_text(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    """A start tag written plainly, with the attributes a browser read (names it cannot hold dropped, values quoted)."""
    parts = [f' {name}' if value is None else f' {name}="{value.replace(chr(34), "&quot;")}"'
             for name, value in attrs if _ATTR_NAME.fullmatch(name)]
    return f"<{tag}{''.join(parts)}>"


def _browser_markup(markup: str) -> tuple[str, int]:
    """Markup rewritten so Python's HTML parser reads it as a browser does, and the number of clickable attributes the
    browser-style reading found (CR-AD-V2-01, Pass 2). A comment ends where a browser ends it ("-->" or "--!>", and
    "<!-->" or "<!--->" alone) and is left empty; "<![CDATA[", "<?", other "<!..." and "</" before anything but a letter
    are bogus comments that end at the next ">" and are dropped; every tag is written plainly with the attributes a
    browser reads (an end tag without any); script and style keep their text up to their own end tag; and the elements
    a browser shows only as text, or not at all (title, textarea, ...), keep their content as escaped text. What a
    browser shows as a link is then what the card reader sees."""
    out: list[str] = []
    clickables, i, n = 0, 0, len(markup)
    while i < n:
        j = markup.find("<", i)
        if j < 0:
            out.append(markup[i:])
            break
        out.append(markup[i:j])
        i = j
        if markup.startswith("<!--", i):
            k = i + 4
            if markup.startswith(">", k):
                i = k + 1
            elif markup.startswith("->", k):
                i = k + 2
            else:
                end = _COMMENT_END.search(markup, k)   # one search: linear, however many comments (M1-02)
                i = n if end is None else end.end()
            out.append("<!---->")
            continue
        if markup[i:i + 9].lower() == "<!doctype":
            k = markup.find(">", i)
            i = n if k < 0 else k + 1
            out.append("<!DOCTYPE html>")
            continue
        if markup.startswith(("<!", "<?"), i):
            k = markup.find(">", i + 2)
            i = n if k < 0 else k + 1
            continue
        if markup.startswith("</", i):
            name = _TAG_NAME.match(markup, i + 2)
            if name is None:
                k = markup.find(">", i + 2)
                i = n if k < 0 else k + 1
                continue
            i = _read_tag(markup, name.end())[0]
            out.append(f"</{name.group(0).lower()}>")
            continue
        name = _TAG_NAME.match(markup, i + 1)
        if name is None:
            out.append("&lt;")   # a "<" that starts no tag is text
            i += 1
            continue
        tag = name.group(0).lower()
        k, attrs = _read_tag(markup, name.end())
        clickables += sum(1 for attr, _ in attrs if attr in _CLICK_ATTRS)
        out.append(_tag_text(tag, attrs))
        i = k
        if tag in _RAW_TEXT or tag in _TEXT_ONLY:
            end = re.compile(r"</" + re.escape(tag) + r"(?=[\t\n\f\r />])", re.I).search(markup, k)
            body = markup[k:end.start() if end else n]
            out.append(body if tag in _RAW_TEXT else body.replace("<", "&lt;"))
            out.append(f"</{tag}>")
            i = _read_tag(markup, end.start() + 2 + len(tag))[0] if end else n
        elif tag == "plaintext":
            out.append(markup[k:].replace("<", "&lt;"))
            break
    return "".join(out), clickables


class _AgencyCards(_Anchors):
    """Read an agency digest's job cards: each title cell and the Apply Now button of the same card, with the card's
    detail lines between them. Strict on purpose: a card without the expected shape is never guessed at, and the
    digest is then "partial". Links are never followed."""

    def __init__(self, digest: private_sites.AgencyDigest):
        super().__init__()
        self.digest = digest
        self.details: list[str] = []
        self.layout_items: list[str] = []   # every link that is not an Apply Now button, described (layout_fingerprint)
        # the same links' visible text, label and alt, kept apart for `careeros agency-layout`: never parsed back out of
        # the described form, where an alt or attribute holding "|c=" or "|alt=" was misread (Codex
        # CR-2026-11-15-GATE-FIXES F7). The fingerprint's inputs are unchanged.
        self.link_labels: list[tuple[str, str, str]] = []
        self.abandoned = 0   # links opened and never closed: their text and destination cannot be judged (CR-AD-V2-01)
        self.unread_elements = 0   # elements in _UNREAD_ELEMENTS (Pass 2)
        self.clickables = 0        # attributes in _CLICK_ATTRS on every tag the parser saw (Pass 2)
        self.raw_clickables: int | None = None   # the same attributes counted by _browser_markup (_read_cards)
        self.cell_text: list[str] = []   # the text of each open table cell (innermost last)
        self.cell_links: list[int] = []  # how many non-button links each open cell holds
        self._link_meta: dict = {}
        self.cells: list[int] = []
        self.tables: list[bool] = []
        self.scope_depth: int | None = None
        self.components = self.detail_cells = 0
        self.heading: list[str] | None = None
        self.title = ""
        self.gap = 0
        self.heading_count = self.button_count = 0
        self.cards: list[Link] = []
        self.css: list[str] = []   # every style element's text (Codex Pass 3)
        self.styles: list[str] = []   # every style attribute
        self.relabels = 0   # links, or parts of one, hidden or drawn over by their own style or a hidden attribute
        self.link_tags: set[str] = set()   # the tag names, classes and ids of links and of what is inside them
        self.link_classes: set[str] = set()
        self.link_ids: set[str] = set()
        self._in_style = 0
        self.outside: list[str] = []
        self.shapes: set[tuple] = set()   # every element's tag with its attribute names (the structure fingerprint)
        self.open_elements: list[tuple[str, str]] = []   # the elements open now: tag, as _element writes it (innermost last)
        self.chains: set[str] = set()   # every job button with what is around it and inside it (Codex Pass 4)
        self._chain = ""   # the open link with the elements around it
        self.card_details: list[str] = []   # every card's detail lines, read for correspondence too (Codex Pass 4)
        self.card_titles: list[str] = []    # every card's title, read for correspondence too (D153 R10)
        self.alternate_pictures = 0   # pictures a browser may draw from another source (srcset, picture, source; D153 R7)

    def handle_starttag(self, tag, attrs):
        if tag == "a" and self._open is not None:
            self.abandoned += 1   # a new link opens inside one that never closed, wherever it is (CR-AD-V2-01, Pass 2)
        self.clickables += len({name for name, _ in attrs if name in _CLICK_ATTRS})
        self.unread_elements += int(tag in _UNREAD_ELEMENTS)
        values = dict(attrs)
        self.alternate_pictures += int(tag in ("picture", "source") or (tag == "img" and "srcset" in values))
        self.styles.append(values.get("style") or "")
        self.shapes.add((tag, tuple(sorted(name for name, _ in attrs))))
        if tag == "a":   # what is around a link: a card moved under a hidden element changes it (Codex Pass 4)
            self._chain = ">".join([written for _, written in self.open_elements] + [_element(tag, attrs)])
        if tag not in _VOID:
            self.open_elements.append((tag, _element(tag, attrs)))
        if tag == "a" or self._open is not None:   # what a browser shows as the link's label (Codex Pass 3)
            self.link_tags.add(tag)
            self.link_classes.update((values.get("class") or "").lower().split())
            self.link_ids.update((values.get("id") or "").lower().split())
            if "hidden" in values or _RELABEL.search(";" + _css_plain(values.get("style") or "")):
                self.relabels += 1
        if tag == "style":
            self._in_style += 1
            self.css.append("")
        super().handle_starttag(tag, attrs)
        if self._skip:
            return
        if tag == "a":
            self._link_meta = {"aria": _tidy(dict(attrs).get("aria-label") or "", 120).casefold(), "img": False, "alt": "",
                               "src": ""}
        elif tag == "img" and self._open is not None:
            self._link_meta["img"] = True   # the picture's whole address: a swapped image changes it (Codex Pass 4)
            self._link_meta["src"] += (dict(attrs).get("src") or "").strip() + " "
            self._link_meta["alt"] = (self._link_meta["alt"] + " " + _tidy(dict(attrs).get("alt") or "", 120)).strip().casefold()
        if tag == "table":
            component = "stylingblock-content-wrapper" in (dict(attrs).get("class") or "").split()
            if self.title and self.scope_depth == len(self.tables):
                if component:
                    self.components += 1
                else:
                    self.title = ""
            self.tables.append(component)
        elif tag == "td":
            style = dict(attrs).get("style") or ""
            size = re.search(r"(?:^|;)\s*font-size\s*:\s*(14|18)px\s*(?:;|$)", style, re.I)
            font = int(size[1]) if size else 0
            self.cells.append(font)
            self.cell_text.append("")
            self.cell_links.append(0)
            if font == 18:
                self.title = ""  # even an empty title cell starts another card
            elif font == 14 and self.title:
                self.detail_cells += 1
                self.details.append("")
        elif tag == "b":
            self.title = ""
            self.scope_depth = next((i for i in range(len(self.tables) - 1, 0, -1) if self.tables[i]), None)
            self.heading = [] if self.scope_depth is not None and self.cells and self.cells[-1] == 18 else None
            self.heading_count += int(self.heading is not None)

    def handle_data(self, data):
        super().handle_data(data)
        if self._in_style and self.css:
            self.css[-1] += data
        if not self._skip and self.cell_text:
            self.cell_text[-1] += data
        if not self._skip and self.heading is not None:
            self.heading.append(data)
        elif not self._skip and self.title and self._open is None:
            self.gap += len(data.strip())
            if self.details:
                self.details[-1] += data
        elif not self._skip:
            self.outside.append(data)   # text that is no card's title or detail line (Codex Pass 3, CR-AD-V2-11)

    def handle_endtag(self, tag):
        for i in range(len(self.open_elements) - 1, -1, -1):   # the element closed, and any left open inside it
            if self.open_elements[i][0] == tag:
                del self.open_elements[i:]
                break
        if tag == "style":
            self._in_style = max(0, self._in_style - 1)
        if tag in ("td", "th", "tr", "table", "tbody", "thead", "tfoot", "caption") and self._open is not None:
            self.abandoned += 1   # a browser closes a link at the end of its cell: what follows is not its text
        if not self._skip:
            if tag == "b" and self.heading is not None:
                self.title = _tidy("".join(self.heading), 200)
                self.card_titles.append(self.title)
                self.gap = self.components = self.detail_cells = 0
                self.details = []
                self.heading = None
            elif tag == "td" and self.cells:
                self.cells.pop()
                text, links = self.cell_text.pop(), self.cell_links.pop()
                if links:   # a cell holding links outside the cards: its whole text is part of the layout (L1-02)
                    self.layout_items.append(f"cell={_tidy(text, 2000).casefold()}")
            elif tag == "table" and self.tables:
                if self.scope_depth == len(self.tables):
                    self.title = ""
                    self.heading = self.scope_depth = None
                self.tables.pop()
            elif tag == "a" and self._open is not None:
                url, label, _ = self._open
                text = _tidy(label, 120).rstrip(" \u2192")
                if text.casefold() != "apply now":   # header, navigation and footer links: the layout (CR-AD-01, D125)
                    meta = self._link_meta
                    self.layout_items.append(f"t={text.casefold()}|a={meta.get('aria', '')}|i={int(meta.get('img', False))}"
                                             f"|src={meta.get('src', '').strip()}|alt={meta.get('alt', '')}|c={self._chain}")
                    self.link_labels.append((text.casefold(), meta.get("aria", ""), meta.get("alt", "")))
                    if self.cell_links:
                        self.cell_links[-1] += 1
                if text.casefold() == "apply now":
                    self.button_count += 1
                    meta = self._link_meta
                    self.chains.add(f"{self._chain}|a={meta.get('aria', '')}|src={meta.get('src', '').strip()}"
                                    f"|alt={meta.get('alt', '')}")
                    # The digest uses adjacent sibling components: heading, two details, and button.
                    if (self.title and not GENERIC_TEXT.fullmatch(self.title) and 0 < self.gap <= 500
                            and self.components == self.detail_cells == 3 and self.cells and self.cells[-1] == 14 and self._valid_link(url)):
                        self.cards.append(Link(url, self.digest.site, self.title,
                                               _card_context(self.digest.name, self.details[:-1]), job_like=True))
                        self.card_details.extend(self.details)
                self.title = ""  # an unrelated or invalid link cannot carry a heading into another card
        super().handle_endtag(tag)

    def _valid_link(self, url: str) -> bool:
        """https on exactly the agency's tracking host, with a path or a query to name the job (marketing mail often
        links to https://host/?qs=...), no fragment, bounded and free of spaces and control characters."""
        if len(url) > 4096 or re.search(r"[\s\x00-\x1f]", url):
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return (parts.scheme == "https" and parts.netloc.lower() == self.digest.tracker_host
                and bool(parts.path.strip("/") or parts.query) and not parts.fragment)

    def layout(self) -> str:
        """The layout fingerprint: the links that are not job buttons, in order, each with every element around it and
        its own attributes (a link's destination excepted) and its images' whole addresses; every distinct way a job
        button sits and looks; and the digest's structure as recorded from a real one (red team before Pass 4): its style
        sheets, every distinct inline style, and every distinct element with its attribute names (Codex Pass 4: where
        each style is, not only which styles exist). A banned-style list never covers every way a browser hides or draws text; any style
        or element the recorded template does not have changes the fingerprint, so the digest is held."""
        structure = ["css=" + re.sub(r"\s+", " ", _css_text("".join(self.css))).strip(),
                     "styles=" + "|".join(sorted({re.sub(r"\s+", "", style.lower()) for style in self.styles if style})),
                     "shapes=" + "|".join(f"{tag}:{','.join(names)}" for tag, names in sorted(self.shapes)),
                     "buttons=" + " || ".join(sorted(self.chains))]
        return layout_fingerprint(self.layout_items + structure)

    def close(self):
        super().close()
        if self._open is not None:
            self.abandoned += 1   # a link still open at the end of the email
            self._open = None

    def reason(self) -> str | None:
        """None when every card was read and the layout is the recorded one; otherwise why the digest is held."""
        if self.abandoned:
            return "links"
        if (self.unread_elements or self.raw_clickables != self.clickables or self.relabelled() or self.alternate_pictures
                or self.neighbour_css()):
            return "markup"   # an element or a link a browser could show that the reader cannot read as it does
        if not (self.cards and len(self.cards) == self.heading_count == self.button_count):
            return "cards"
        if not self.digest.layout:
            return "no layout recorded"
        return None if self.layout() == self.digest.layout else "layout"

    def relabelled(self) -> bool:
        """Whether the style of the digest can hide a link's text or show other text in its place (Codex Pass 3): a
        link or a part of one hidden or drawn over by its own style, generated content or a font or style sheet loaded
        from elsewhere anywhere, or a style sheet rule that can reach a link with such a declaration."""
        if self.relabels:
            return True
        sheet = _css_text("\n".join(self.css))
        flat = re.sub(r"\s+", "", sheet)
        if re.search(r"@(?:import|font-face)|(?:^|[;{])content:", flat) or any(
                re.search(r"(?:^|;)content:", _css_plain(style)) for style in self.styles):
            return True
        for rule in re.finditer(r"([^{}]*)\{([^{}]*)\}", sheet):
            if _RELABEL.search(";" + re.sub(r"\s+", "", rule.group(2))) and any(
                    _reaches(selector, self.link_tags, self.link_classes, self.link_ids)
                    for selector in rule.group(1).split(",")):
                return True
        return False

    def neighbour_css(self) -> bool:
        """Whether a style sheet selects an element by its neighbours (a sibling combinator, + or ~) or by what it holds
        (:has): moving a class between neighbouring elements can then hide a card without changing any element's own
        description, which the layout records (Codex Pass 5, D153 R7). The one exception is a selector that only starts
        with "u +" or "u ~", the way email templates detect Gmail; the two real digests have three such and no other."""
        sheet = _css_text("\n".join(self.css))
        for head in re.findall(r"([^{}]*)\{", sheet):
            if head.strip().startswith("@"):
                continue
            for selector in head.split(","):
                selector = re.sub(r"\[[^\]]*\]?", "", selector)   # attribute selectors ([href~=...]) say nothing of neighbours
                if ":has(" in selector:
                    return True
                selector = re.sub(r":nth-[a-z-]+\([^)]*\)", "", selector)   # an+b arguments
                selector = re.sub(r"^\s*u\s*[+~]", "", selector, count=1)   # Gmail's detection
                if re.search(r"[+~]", selector):
                    return True
        return False

    def status(self) -> str:
        return "complete" if self.reason() is None else "partial"


def _read_cards(digest: private_sites.AgencyDigest, markup: str) -> _AgencyCards:
    """A digest's cards, read from its markup as a browser would show it (_browser_markup). The clickable attributes the
    browser-style reading counted must equal those the parser saw: a link the parser did not see as one holds the digest."""
    shown, clickables = _browser_markup(markup)
    cards = _AgencyCards(digest)
    cards.feed(shown)
    cards.close()
    cards.raw_clickables = clickables
    return cards


def layout_fingerprint(items: list[str]) -> str:
    """The fingerprint of a digest's links that are not job buttons, in order: each link's visible text, accessible
    label, whether it is an image and the image's alt text; then the digest's structure (_AgencyCards.layout). The same
    template gives the same fingerprint; a link added, removed or relabelled (a new layout, a job behind an image or a
    label), or a style or element the template does not have, changes it. Only this hash is stored in the private
    settings, so no agency wording enters the shared code (D125)."""
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:16]


_LIST_LINK = re.compile(r"(?:see|view) (?:all|more) jobs|search jobs|more jobs|jobs? for you|\d+ new jobs?", re.I)


def agency_layout_report(data: bytes) -> dict | None:
    """For `careeros agency-layout`: what the reader sees in one saved digest, as counts and the layout fingerprint
    only (no text or link). None when the email is not an agency digest under the current settings."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    digest = _agency_digest(message)
    if digest is None:
        return None
    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        return {"agency": digest.name, "cards": 0, "headings": 0, "buttons": 0, "layout": None, "recorded": digest.layout or None,
                "status": "partial", "reason": "plain text"}
    cards = _read_cards(digest, html_part.get_content())

    def job_like(labels):   # a link outside the cards whose text, label or alt reads like a job, other than "see all jobs"
        # the link's own text, aria label and alt only: never the element chain's attributes (a table's
        # role="presentation" made every link "job-like", 2026-11-15), and never parsed out of the fingerprint's form (F7)
        return any(t and not _LIST_LINK.fullmatch(t) and re.search(r"\b(apply|job|jobs|position|role|opening)\b", t)
                   for t in labels)
    return {"agency": digest.name, "cards": len(cards.cards), "headings": cards.heading_count, "buttons": cards.button_count,
            "layout": cards.layout(), "recorded": digest.layout or None, "status": cards.status(), "reason": cards.reason(),
            "job_like_links": sum(1 for labels in cards.link_labels if job_like(labels))}


def _named_like_an_agency(message) -> bool:
    """Bulk mail (List-Unsubscribe) whose sender's display name contains a configured agency's name, from an address the
    entry does not list: the agency's digest after a move to another mail service (L4F-02)."""
    if not message["List-Unsubscribe"]:
        return False
    display, _ = parseaddr(str(message["From"] or ""))
    display = re.sub(r"\s+", " ", display).casefold()
    return any(digest.name.casefold() in display for digest in private_sites.agency_digests() if digest.name)


def raw_mentions_agency(data: bytes) -> bool:
    """Whether raw mail that could not be read names a configured agency's sender domain or tracking host: such mail is
    held for you, never recorded as read (L1-06). Read as the bytes are and as a reader would see them: quoted-printable
    line breaks joined, character references and escapes decoded, tabs and line breaks inside a name dropped (Codex
    Pass 3)."""
    lowered = data.lower()
    folded = _fold(html_text.unescape(re.sub(r"=\r?\n", "", data.decode("latin-1"))))
    return any(needle.lower().encode() in lowered or _fold(needle) in folded for d in private_sites.agency_digests()
               for needle in (d.sender_domain, d.tracker_host, *d.former_hosts, d.name) if needle)


def agency_for_url(url: str | None) -> private_sites.AgencyDigest | None:
    """The agency whose tracking host a link is on (or carries), if any: for links that reach CareerOS another way, such
    as a GitHub inbox line (L4-03)."""
    for digest in private_sites.agency_digests():
        hosts = frozenset((digest.tracker_host, *digest.former_hosts))
        if _norm_host(url) in hosts or _mentions_tracker(url, hosts):
            return digest
    return None


def _tracker_evidence(message) -> bool:
    """Whether an email names a configured agency tracking host anywhere a reader could follow it: in any text part,
    HTML and plain alike (a mail client may show either), in any link or text, any case, escaped or wrapped
    (CR-AD-V2-06, Pass 2: the HTML part alone was checked, and plain-text links only in lower case), with character
    references decoded and tabs and line breaks dropped from links as a browser drops them (Codex Pass 3)."""
    trackers = _tracker_hosts()
    if not trackers:
        return False
    return any(_text_mentions_tracker(text, trackers) for text in _texts(message))


def _texts(message, depth: int = 0):
    """The text of every part of an email, attached messages included, also one sent with a transfer encoding Python's
    parser leaves undecoded (M1-05), and every other part read as text (Codex Pass 4)."""
    for part in message.walk():
        if part.get_content_maintype() == "message" and depth < 3:
            encoding = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
            inner = part.get_payload()
            if encoding in ("base64", "quoted-printable"):   # Python leaves this undecoded: decode it here
                text = inner[0].get_payload() if isinstance(inner, list) and inner else inner
                if isinstance(text, str):
                    try:
                        data = (base64.b64decode(text) if encoding == "base64"
                                else quopri.decodestring(text.encode("latin-1")))
                    except (binascii.Error, ValueError):
                        data = b""
                    if data:
                        yield from _texts(email.message_from_bytes(data, policy=email.policy.default), depth + 1)
            elif not part.is_multipart():
                decoded = part.get_payload(decode=True)
                if isinstance(decoded, bytes) and decoded:
                    yield from _texts(email.message_from_bytes(decoded, policy=email.policy.default), depth + 1)
            continue
        if part.is_multipart():
            continue
        if part.get_content_maintype() != "text":   # a part of any other type is searched as text too, whatever its
            data = part.get_payload(decode=True)    # name: an HTML page sent as application/octet-stream (Codex Pass 4)
            if isinstance(data, bytes) and data:
                wide = data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                yield data.decode("utf-16" if wide else "utf-8", "replace")
            continue
        try:
            text = part.get_content()
        except (LookupError, UnicodeError, ValueError, KeyError, AssertionError):
            text = (part.get_payload(decode=True) or b"").decode("latin-1")
        if isinstance(text, str):
            yield text


def _agency_digest(message) -> private_sites.AgencyDigest | None:
    """The agency this email is a digest from, if any: bulk mail (List-Unsubscribe) from an address at the agency's
    sender domain, read from the whole From header. Which mail it is decides alone; whether its cards can be read is
    the card reader's question, so a plain-text copy or a new tracking host is held, never read as an ordinary alert
    (CR-AD-01, Pass 2)."""
    digests = private_sites.agency_digests()
    if not digests or not message["List-Unsubscribe"]:
        return None
    _, address = parseaddr(str(message["From"] or ""))
    address = address.lower()
    return next((d for d in digests if address.endswith("@" + d.sender_domain)), None)


def _links_from_html(markup: str) -> list[tuple[str, str, str]]:
    parser = _Anchors()
    parser.feed(_escape_href_ampersands(markup))
    parser.close()
    return [(href, _tidy(text, 200), _line_context(context)) for href, text, context in parser.found]


def _links_from_text(body: str) -> list[tuple[str, str, str]]:
    """Plain-text alerts list each job as a block: title line, then company/location lines, then the link."""
    found, block = [], []
    for line in body.splitlines():
        urls = re.findall(r"https?://[^\s<>\")]+", line, re.I)
        label = _tidy(re.sub(r"https?://\S+", "", line, flags=re.I), 200)
        if not urls:
            block = block + [label] if label else []
            continue
        title = label or (block[0] if block else "")
        context = _line_context(" · ".join(block[1:] if not label else block))
        found.extend((url, title, context) for url in urls)
        block = []
    return found


def _extra_parts(message) -> bool:
    """Whether an email has any part besides its body (at most one text/plain and one text/html part, neither marked as
    an attachment): an attachment of any type, an attached email, a calendar file, a picture. Such mail is held for you,
    never read for jobs and never given a receipt (D153 R8). The two real digests have one part, text/html."""
    leaves = []
    for part in message.walk():
        if part.get_content_maintype() == "message":
            return True
        if not part.is_multipart():
            leaves.append(part)
    types = [part.get_content_type() for part in leaves]
    return (any(t not in ("text/plain", "text/html") for t in types) or types.count("text/plain") > 1
            or types.count("text/html") > 1 or any(part.get_content_disposition() == "attachment" for part in leaves))


def parse_eml(path: Path) -> AlertEmail:
    if path.stat().st_size > MAX_EML_BYTES:
        raise AlertEmailError("email is larger than 5 MB")
    return parse_eml_bytes(path.read_bytes())


def parse_eml_bytes(data: bytes, message_key: str | None = None) -> AlertEmail:
    """The email's job lines, every card occurrence kept, each keyed by `message_key` (the Gmail message id, given by the
    Gmail pull; else the bytes' digest) and its position, never merged with another by its link, title or stated
    identity (part 3 of the owner's D192 build; Codex's D190 design review H7)."""
    if len(data) > MAX_EML_BYTES:
        raise AlertEmailError("email is larger than 5 MB")
    key = message_key or "eml:" + hashlib.sha256(data).hexdigest()[:24]
    message = email.message_from_bytes(data, policy=email.policy.default)
    try:
        received = format_utc(parsedate_to_datetime(message["Date"])) if message["Date"] else None
    except (TypeError, ValueError):
        received = None
    sender = _tidy(str(message["From"] or ""), 120)
    subject = _tidy(str(message["Subject"] or ""), 200)
    if _extra_parts(message):   # an attachment, an attached email or any other part a reader could open: held unread,
        # no receipt, whatever its body (D153 R8, Codex Pass 5: a calendar file folding a host across lines, a PDF
        # writing it in hex)
        return AlertEmail(sender, subject, received, [], digest_status="partial", digest_reason="attachment")
    html_part = message.get_body(preferencelist=("html",))
    text_part = message.get_body(preferencelist=("plain",))
    if html_part is None and text_part is None:
        raise AlertEmailError("no readable body in the email")
    markup = html_part.get_content() if html_part is not None else None
    digest = _agency_digest(message)
    if digest is None and (_tracker_evidence(message) or _named_like_an_agency(message)):
        # The agency's tracking links in mail that is not its digest as recorded (another sender, no bulk header): held,
        # never read as an ordinary alert and marked done with its jobs lost (CR-AD-V2-06).
        return AlertEmail(sender, subject, received, [], digest_status="partial", digest_reason="envelope")
    if digest is not None:
        if markup is None:   # a plain-text digest: its cards cannot be read, so it is held
            return AlertEmail(sender, subject, received, [], digest_status="partial", digest_reason="plain text")
        if [part.get_content_type() for part in message.walk() if not part.is_multipart()] != ["text/html"]:
            # anything beside its one HTML part, which real digests never have (a plain-text or AMP copy, a second HTML
            # part, an attachment): a mail client may show it instead, so it is held (M1-04, Codex Pass 3, the red team
            # before Pass 4)
            return AlertEmail(sender, subject, received, [], digest_status="partial", digest_reason="markup")
        cards = _read_cards(digest, markup)
        if reads_like_correspondence(" ".join(cards.outside)) or explicit_correspondence(
                " ".join(cards.card_titles + cards.card_details)):   # the whole card, its title included (D153 R10)
            # the digest's own words outside its cards are about an application, an interview, an assessment, an offer
            # or a submittal: no card is read, and it is held for you, never read for jobs (Codex Pass 3, CR-AD-V2-11)
            return AlertEmail(sender, subject, received, [], digest_status="partial", digest_reason="correspondence")
        for position, card in enumerate(cards.cards):
            card.occurrence = f"{key}#card{position}"
        return AlertEmail(sender, subject, received, cards.cards, digest_status=cards.status(), digest_reason=cards.reason(),
                          message_key=key)

    raw_links = _links_from_html(markup) if markup is not None else _links_from_text(text_part.get_content())
    links: list[Link] = []
    skipped: dict[str, int] = {}
    for position, (href, text, context) in enumerate(raw_links):
        try:
            canonical = canonical_job_url(href)
        except MalformedLink:  # one unreadable link never discards the rest of the digest; it is counted
            # unless it is a review link whose own outer link reads: kept under that outer link (the owner's D155 rule b)
            outer, canonical = href.strip(), None
            try:
                if outer.startswith(("https://", "http://")) and kept_for_review(outer):
                    canonical = outer, site_of(_split(outer).netloc), True
            except MalformedLink:
                canonical = None
            if canonical is None:
                skipped["malformed"] = skipped.get("malformed", 0) + 1
                continue
        if canonical is None:
            continue
        url, site, job_like = canonical
        # The header and "see all" links of a LinkedIn, Indeed or Glassdoor digest point to job-search pages: never
        # a posting, so never a lead, even when their text names a job family. Counted, not silent. A link that names a
        # job ID is no search page, even when the ID can't be read as one job: it is kept for review (CR-HF-01)
        if not job_like and is_navigation(site, _split(url).path) and not names_a_job_id(site, url):
            if not names_any_job(href):   # dropped only when no layer names any job (D151 rule 3)
                skipped["navigation"] = skipped.get("navigation", 0) + 1
                continue
        url = stored_url(href, url)   # a review href stays a review lead, whatever its kept URL reads as (D149 R-I1)
        site = site_of(_split(url).netloc)   # the site of the stored link, not of its unwrapped destination (D151 rule 4)
        meaningful = text if text and not GENERIC_TEXT.match(text) else ""
        # every occurrence is its own line, under its position: none is merged with another by its link, title or stated
        # identity (Codex's D190 design review H7; part 3 of the owner's D192 build). One posting linked twice is "seen"
        # at its second line by its exact link; a careers page's lines are notices, one per occurrence
        links.append(Link(url=url, site=site, job_like=job_like, text=meaningful, context=context if meaningful else "",
                          occurrence=f"{key}#{position}"))
    return AlertEmail(sender, subject, received, links, skipped=skipped, message_key=key)


# the parts of an alert line that change from one email to the next for the same opening: its age, "New", "Easy Apply"
# and the like (the red team after Codex Pass 12, RT4-4)
_VOLATILE = re.compile(r"(?:(?:re)?posted\b.*|new|just posted|today|yesterday|easy apply|actively (?:hiring|recruiting)|"
                       r"promoted|be an early applicant|(?:over )?\d+\+? applicants?|"
                       # an age says "ago": "6 months" or "40 hours" alone is a contract's length or the weekly hours,
                       # part of which opening it is (the red team's gr1-alerts-1)
                       r"\d+\+? (?:minutes?|hours?|days?|weeks?|months?) ago)", re.I)


def stated_identity(context: str | None) -> str:
    """What an alert line says about which opening it is, beside its title: every part of it (the employer, the place,
    the pay, a requisition however it is written) but the ones that change from email to email (its age, "New"),
    tidied and in the order of their text, and any requisition it states read as one. At a page several openings
    share, two lines that state anything different are two leads, never one, and the same line again is one (Codex
    CR-2026-11-15-GATE-FIXES H26, Pass 12; the owner's D182; the red team after Pass 12, RT4-3 and RT4-4)."""
    from careeros.discovery.board_discovery import lead_requisition
    from careeros.discovery.seen_index import norm_req
    parts = {" ".join(s.split()).lower() for s in (context or "").split(" \u00b7 ")}
    kept = sorted(p for p in parts if p and not _VOLATILE.fullmatch(p))
    return "|".join([*kept, norm_req(lead_requisition(context)) or ""])


_HOST_PREFIXES = {"www", "careers", "jobs", "job", "boards", "apply", "work", "join"}


def greenhouse_candidate(url: str) -> str | None:
    """A company careers page that embeds a Greenhouse posting (`?gh_jid=123`) usually uses its domain name as the
    Greenhouse board name. Returns that public Greenhouse link to try, or None. Wrong guesses just return not-found."""
    trackers = _tracker_hosts()
    parts = urlsplit(url)
    if _norm_host(url) in trackers:
        return None
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
    r"received your application|viewed your (application|profile)|"
    r"interview (invitation|request|confirmation|scheduled|availability)|"
    r"confirm (your|the) (interview|availability)|"
    r"(schedule|scheduling) (an|your) interview|interview with|invitation to interview|(online|skills?) assessment|"
    r"complete (the|your|an) assessment|assessment (invitation|link|request)|offer letter|job offer|next steps|"
    r"not (moving|move) forward|unfortunately|message from|wants to connect)\b", re.I)


_SPACES = re.compile(r"[\s\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000]+")   # every space, the no-break ones included


def _words(text: str | None) -> str:
    """Text with every run of spaces, line breaks and no-break spaces read as one space, as a reader sees it (the red
    team before Pass 4: "your&nbsp;application" or a line break inside a phrase)."""
    return _SPACES.sub(" ", text or "").strip()


def is_correspondence(subject: str | None) -> bool:
    """True for application, interview, offer or recruiter mail: never processed, even from an alert sender."""
    return bool(_CORRESPONDENCE.search(_words(subject)))


_AMBIGUOUS = re.compile(r"\b(next steps|message from)\b", re.I)


def explicit_correspondence(subject: str | None) -> bool:
    """Correspondence named by more than an ambiguous phrase: application, interview, assessment, offer or decision
    words. "Next steps" and "message from" alone are ambiguous (an agency's digest may use them, L1-01), so only they
    yield to a recognised digest; the others never do, whoever sends them (CR-AD-V2-11)."""
    return bool(_CORRESPONDENCE.search(_AMBIGUOUS.sub(" ", _words(subject))))


# Words a digest's own text outside its job cards never needs: they belong to application, interview, assessment, offer
# and submittal mail. A phrase list never covers every wording (the red team before Pass 4), so a recognised digest
# with any of these words outside its cards is held unread for you. The two real digests checked have none (2026-11-11).
_DIGEST_CORRESPONDENCE = re.compile(r"\b(?:interview\w*|applications?|applied|applicant|assessments?|offers?|"
                                    r"submi(?:t|ts|tted|ttal|ssion)|shortlist\w*|hiring manager|phone screen\w*|"
                                    r"screening call|skills? test)\b", re.I)


def reads_like_correspondence(text: str | None) -> bool:
    """Whether a recognised digest's own words outside its cards read like application, interview, assessment or offer
    mail (Codex Pass 3, CR-AD-V2-11): any such word, or any explicit correspondence phrase."""
    words = _words(text)
    return bool(_DIGEST_CORRESPONDENCE.search(words)) or explicit_correspondence(words)


def lead_id(url: str) -> str:
    return "lead:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def stored_url(href: str, url: str) -> str:
    """The URL a lead from the link `href` (as received) is stored and matched under, `url` being its canonical form.

    The owner's D149 R-I1: the review classification is made on the href as received, before any wrapper layer is
    discarded, and it travels with the lead. When the href is a review link, the lead keeps the first of these that is
    itself a review link: the canonical form, a layer it wraps (outermost first), the href itself. So every later
    kept_for_review() check, in intake, lead resolution and the board matcher, sees a review lead, by construction.
    Otherwise the canonical form is kept, as before."""
    href = href.strip()
    if not kept_for_review(href):
        return url
    try:
        layers = _every_layer(href)[1:]
    except MalformedLink:
        layers = []
    return next((link for link in (url, *layers) if kept_for_review(link)), href)


def names_any_job(href: str) -> bool:
    """Whether the href as received names a job anywhere: a job-ID field with a value (readable or not), or a job reading
    of any layer, or a layer the reader could not finish (D149 R-I1, the owner's D151 rule 3). Only a link naming no job
    may be dropped as a search page."""
    try:
        layers, complete = _walk(href.strip())
        return (not complete or any(r is not None and r[2] for r in map(_reading, layers))
                or any(names_a_job_id(site_of(_split(layer).netloc), layer) for layer in layers))
    except MalformedLink:
        return True


def kept_for_review(url: str) -> bool:
    """A lead kept only for review: a LinkedIn, Indeed or Glassdoor link that names no single job (an unreadable or
    conflicting job ID, or none). It never makes a later link to a definite job count as seen before
    (CR-2026-11-09-INTAKE-HOTFIX Pass 2, CR-HF-01, D126).

    Any reading decides (the owner's D142 R-a, Codex Pass 7): the link as stored (the parser's bounded unwrap), every
    link it wraps and the fully unwrapped one, and canonical_job_url's own reading. If any of them is such a link, it is
    a review link; only a link every reading agrees is definite may be matched or ingested. Derived from the URL alone.

    Fail closed by construction (the owner's D151, under D149): after NFKC and removing invisible characters, every job
    the link names anywhere (the path, every job-ID field, every wrapped link at every layer) must be one and the same
    job, and the walk must finish within its bounds; otherwise it is a review link."""
    try:
        layers, complete = _walk((url or "").strip())
        readings = [_reading(layer) for layer in layers] + [canonical_job_url(url or "")]
        jobs = _jobs_named(layers)
    except MalformedLink:
        return True
    if not complete:   # a bound hit or a link it cannot read: never definite (the owner's D151 rule 2)
        return True
    if len(jobs) > 1:   # the readings name more than one job (the owner's D151 rule 1)
        return True
    return any(r is not None and r[1] in _ID_FIELDS and not r[2] for r in readings)


AGENCY_LEAD = "lead:agency:"


def agency_lead_id(url: str) -> str:
    """The lead_id of an agency digest card: its prefix marks the row for good as a personal tracking link that stays
    local, whatever the private settings say later (Codex CR-AD-02)."""
    return AGENCY_LEAD + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def agency_setting_problem(store, *, name_hosts: bool = False) -> str | None:
    """Fail closed (D125): when a stored agency lead's tracking host is not in the current private settings (the entry
    turned off, removed or changed), CareerOS cannot tell that agency's links from ordinary ones. Alert intake, the
    seen-jobs export and lead-based board discovery then stop, with this message, until the entry is restored. Inside
    a run, a file changed since the run read it stops them too, until the next run reads it (D141)."""
    try:
        private_sites.refresh()   # outside a run, the file as it is now; inside one, a change is a stop (CR-AD-V2-07)
    except private_sites.SettingsChanged as exc:
        return str(exc)
    hosts = _tracker_hosts()
    enrolled = enrolled_hosts(store)
    known = frozenset(hosts | enrolled)
    stored: set[str | None] = set()
    for r in store.all("SELECT DISTINCT url FROM leads WHERE lead_id LIKE ?", (AGENCY_LEAD + "%",)):
        stored |= lead_tracker_hosts(r["url"], known)
    missing = sorted(h for h in (enrolled | stored) if h is not None and h not in hosts)
    if None in stored:
        missing.append("(unreadable)")
    if not missing:
        return None
    which = (f"the tracking host(s) {', '.join(missing)}" if name_hosts   # run output names none (P4-01): it can be pasted
             else "an agency tracking host (careeros status and careeros agency-hosts name it)")
    return (f"agency_setting_missing: agency_digests (policy\\private_sites.yaml) is missing {which}, set up before or "
            "used by stored agency leads. Restore the entry (or list an old host under former_hosts): a host once set "
            "up stays private for good. Alert intake, the seen-jobs export and board discovery stay stopped until then.")


def lead_tracker_hosts(url: str | None, known: frozenset[str]) -> set[str | None]:
    """The tracking hosts a stored agency lead depends on: its own host when it is one CareerOS knows (configured or set
    up before), else the known hosts its link carries inside a wrapper, else its own host (unknown, so the check fails
    closed). A wrapper's own host is never required (CR-AD-V2-08). A link with a host CareerOS cannot read (D153 R5)
    names no required host: it stays local whatever the settings say, and careeros agency-hosts counts it."""
    host = _norm_host(url)
    if host in known:
        return {host}
    carried: set[str | None] = {h for h in known if _mentions_tracker(url, frozenset((h,)))}
    if not carried and unreadable_host(url):
        return set()
    return carried or {host}


def links_using_host(store, host: str) -> int:
    """How many stored links (every lead, every job, marked or not) are on this tracking host or carry it inside a
    wrapper. A host any of them uses is never forgotten, so no earlier ordinary copy can reach the export once the host
    is no longer listed (CR-AD-V2-09)."""
    one = frozenset((host,))
    rows = store.all("SELECT url FROM leads UNION ALL SELECT url FROM jobs UNION ALL SELECT canonical_url FROM jobs")
    return sum(1 for r in rows if r[0] and (_norm_host(r[0]) == host or _mentions_tracker(r[0], one)))


ENROLLED = "agency_enrolled:"   # meta keys: every tracking host CareerOS has been set up with (CR-AD-V2-02)


def enroll_agency_hosts(store) -> None:
    """Remember every configured tracking host for good, before any intake: a host once set up stays required, so
    turning its entry off later fails closed even before its first lead is stored."""
    known = {r["key"]: r["value"] for r in store.all("SELECT key, value FROM meta WHERE key LIKE ?", (ENROLLED + "%",))}
    writer = store.writer("admin")
    for host in sorted(_tracker_hosts()):
        key = ENROLLED + host
        if key not in known:
            try:
                writer.insert("meta", {"key": key, "value": store.clock.now()})
            except sqlite3.IntegrityError:   # another run enrolled it at the same moment (L3-04)
                pass
        elif known[key].startswith(FORGOTTEN):
            writer.update("meta", {"value": store.clock.now()}, "key = ?", (key,))


def enroll_reading(store) -> bool:
    """Every tracking host of the reading in force is enrolled before it is used, whatever the caller (a command, the
    review page's paste box; CR-AD-V2-02, D141, Codex Pass 4). A reading the stop check refuses enrols nothing (L3-01);
    a file that cannot be read is left to the callers that read it, which stop on it. Whether the hosts were enrolled:
    the paste box takes nothing in when they were not (D153 R4)."""
    try:
        if agency_setting_problem(store) is None:
            enroll_agency_hosts(store)
            return True
    except ValueError:
        pass
    return False


FORGOTTEN = "forgotten "   # a meta value: the owner removed this host with careeros agency-hosts --forget


def enrolled_hosts(store) -> set[str]:
    return {r["key"][len(ENROLLED):] for r in store.all("SELECT key, value FROM meta WHERE key LIKE ?", (ENROLLED + "%",))
            if not r["value"].startswith(FORGOTTEN)}


class _Everything(frozenset):
    """Every link is local: what local_only_urls gives while the private settings are missing an agency (fail closed)."""

    def __contains__(self, item) -> bool:
        return True


class _LocalUrls(frozenset):
    """The links of the stored agency cards, with the tracking hosts of the same reading of the private settings as the
    stop check that produced them (`hosts`): every lead is then classified under that one reading, never a later one
    (red team before Codex Pass 3, S2F-01 to -03)."""
    hosts: frozenset = frozenset()


def local_only_urls(store) -> frozenset[str]:
    """The links of every stored agency card (lead_id marked lead:agency:). Any other row with one of these links is
    the same personal tracking link and stays local too, whatever the private settings say (CR-AD-06). While an
    enrolled or stored agency host is missing from the settings, every lead counts as local, so no lead feeds export,
    discovery or automatic matching (CR-AD-V2-04). The check and the tracking hosts that is_agency_lead then uses come
    from one pinned reading; a change while it is read counts every lead as local."""
    try:
        with private_sites.pinned():
            if agency_setting_problem(store):
                return _Everything()
            local = _LocalUrls(r["url"] for r in store.all("SELECT url FROM leads WHERE lead_id LIKE ?", (AGENCY_LEAD + "%",)))
            local.hosts = _tracker_hosts()
    except private_sites.SettingsChanged:
        return _Everything()
    return local


def is_agency_lead(lead, local_urls: frozenset[str] = frozenset()) -> bool:
    """A stored lead from an agency digest: by its lead_id's mark, by a link that a marked row has (`local_urls`, from
    local_only_urls), or by its link's host while the settings list it. Such a lead is never exported, never offered
    for board discovery or automatic matching, and never matched to another lead on its title."""
    hosts = getattr(local_urls, "hosts", None)   # the reading of the stop check that made local_urls, when it has one
    url = lead["url"]
    tracking = (bool(url) and (unreadable_host(url) or _norm_host(url) in hosts or _mentions_tracker(url, hosts))
                if hosts is not None else is_agency_tracking_url(url))
    return (str(lead["lead_id"] or "").startswith(AGENCY_LEAD) or (lead["url"] or "") in local_urls
            or tracking)
