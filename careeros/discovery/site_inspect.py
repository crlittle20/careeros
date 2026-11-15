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
from collections import Counter
from html.entities import html5 as _ENTITIES
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
SITE_NAME_KEYS = {"og:site_name", "application-name"}   # where a page declares its own name (identity for a hinted domain)
# Raw-text elements: in a browser everything after the start tag is text up to that element's own end tag (plaintext: to
# the end of the page), so a tag inside one is text, end tags included (Codex F1, Pass 8).
_RAW_TEXT = {"title", "textarea", "xmp", "iframe", "noembed", "noframes", "noscript", "plaintext", "script", "style"}
# Elements whose content is not the document's own markup as a browser builds it: raw text, and an SVG's, a MathML's or a
# template's content. A <title> or site-name tag inside one never counts (Codex F1, Passes 6 to 8). Nor inside a <select>,
# whose content some browsers drop (Codex Pass 8 left that unconfirmed; excluding it fails safe).
_RAW_CONTEXT = _RAW_TEXT | {"svg", "math", "template", "select"}
_FOREIGN = {"svg", "math"}   # "/>" closes these; in HTML it closes no other _RAW_CONTEXT element ("<iframe/>" stays open)
_COMMENT_END = re.compile(r"--!?>")   # where an HTML5 comment ends (_Collector.parse_comment)
# A browser comparison before Codex's Pass 8 (CareerOS's reader against Chromium's HTML5 parser on 16,000 generated pages):
_FRAMESET = re.compile(r"<frameset[\s/>]", re.I)   # a frameset can discard the body's tags in a browser (inspect)
AMBIGUOUS = "\x00ambiguous site name"   # a site name that two readings of the page read differently: never the employer's
UNREADABLE = "\x00unreadable site-name tag"   # the source scan gave up on a pathological page (source_site_names)
# Tags as HTML5 tokenizes them (_read_tag), not as Python 3.12.10's html.parser does (Codex F1, Pass 8: it also split on
# U+000B, U+00A0 and other whitespace, accepted whitespace after "</", read "==" as "=", ended a tag name at NUL, matched
# "</script>" in any Unicode case and ended a script at its first "</script>"). Only these five characters separate:
_WS = "\t\n\f\r "   # (a browser reads a carriage return as a line feed before it tokenizes)
_ANY_SPACE = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a" \
             "\u2028\u2029\u202f\u205f\u3000\ufeff"   # whitespace to Python's str.strip or JavaScript's trim
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")   # HTML5 lower-cases ASCII only
_NAME_END = re.compile(r"[\t\n\f\r />]")
_ATTRIBUTE_END = re.compile(r"[\t\n\f\r /=>]")
_VALUE_END = re.compile(r"[\t\n\f\r >]")
_NOT_WS = re.compile(r"[^\t\n\f\r ]")
_DELIMITERS = frozenset(_WS + "/>")   # what ends a tag's name
_LETTERS = re.compile(r"[A-Za-z]*")
_DASH_OR_LT = re.compile(r"[-<]")
_META_OPEN = re.compile(r"<[Mm][Ee][Tt][Aa][\t\n\f\r />]")   # where a <meta> tag can begin (source_site_names)
_REFERENCE = re.compile(r"&(?:#(?:[xX]([0-9A-Fa-f]+)|([0-9]+));?|([A-Za-z0-9]+;?))")
_C1 = dict(zip((0x80, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8E, 0x91, 0x92, 0x93, 0x94, 0x95,
                0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9E, 0x9F),
               "\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022"
               "\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178", strict=True))   # HTML5's reading of &#128; to &#159;
# A numeric reference with many digits: Python's unescape raises on one over 4,300 digits, leading zeros included (its
# integer conversion limit), which stopped inspect() (found while timing Pass 9's reader). inspect() first rewrites each
# to the same character, U+FFFD beyond U+10FFFF as in HTML5 (_short_reference).
_LONG_REFERENCE = re.compile(r"&#(?:[xX]([0-9A-Fa-f]{8,})|([0-9]{8,}));?")


def _short_reference(match: re.Match) -> str:
    hexadecimal, decimal = match.groups()
    digits = (hexadecimal or decimal).lstrip("0")
    if len(digits) > (6 if hexadecimal else 7):
        return "&#xFFFD;"
    return f"&#x{int(digits or '0', 16 if hexadecimal else 10):X};"


def _clean(name: str) -> str:
    """A tag or attribute name as HTML5 reads it: ASCII letters lower-cased, NUL read as U+FFFD."""
    return name.translate(_ASCII_LOWER).replace("\x00", "\ufffd")


def _reference(match: re.Match, raw: str) -> str:
    hexadecimal, decimal, name = match.groups()
    if name is None:
        digits = (hexadecimal or decimal).lstrip("0")
        if len(digits) > (6 if hexadecimal else 7):   # beyond U+10FFFF (and never a huge int() conversion)
            return "\ufffd"
        code = int(digits or "0", 16 if hexadecimal else 10)
        if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return "\ufffd"
        return _C1.get(code) or chr(code)   # a control character or noncharacter is kept, as in HTML5
    for end in range(min(len(name), 32), 0, -1):   # the longest name in the table (at most 32 characters)
        if name[:end] in _ENTITIES:
            following = name[end:end + 1] or raw[match.end():match.end() + 1]
            if not name[:end].endswith(";") and (following == "=" or following.isascii() and following.isalnum()):
                return match.group()   # in an attribute, "&nbspHealth" or "&copy=" stays as written
            return _ENTITIES[name[:end]] + name[end:]
    return match.group()


def _attribute_value(raw: str) -> str:
    """An attribute value with its character references decoded as HTML5 decodes them in an attribute (Python's unescape
    decodes "&nbspHealth" and drops "&#1;"), NUL read as U+FFFD."""
    if "&" in raw:
        raw = _REFERENCE.sub(lambda match: _reference(match, raw), raw)
    return raw.replace("\x00", "\ufffd")


def _read_tag(text: str, i: int):
    """A start or end tag as HTML5 tokenizes it, from its name (text[i], an ASCII letter) to its ">": (name, [(attribute,
    value)], whether it ends "/>", the index after it). Only HTML whitespace separates; a quoted value runs to its closing
    quote, past any ">"; after "=" the value begins, even with another "=". None when the page ends inside the tag: a
    browser drops it. Each search begins where the last one stopped, so a read scans no further than the index it
    returns, or the end of the page when it returns None (source_site_names counts on it)."""
    found = _NAME_END.search(text, i)
    if not found:
        return None
    name, i = _clean(text[i:found.start()]), found.start()
    attributes: list[tuple[str, str]] = []
    while True:
        found = _NOT_WS.search(text, i)
        if not found:
            return None
        i = found.start()
        if text[i] == ">":
            return name, attributes, False, i + 1
        if text[i] == "/":
            if text.startswith("/>", i):
                return name, attributes, True, i + 2
            i += 1   # a "/" anywhere else is ignored
            continue
        found = _ATTRIBUTE_END.search(text, i + 1)   # a name may begin with "="
        if not found:
            return None
        key = _clean(text[i:found.start()])
        found = _NOT_WS.search(text, found.start())
        if not found:
            return None
        i = found.start()
        if text[i] != "=":
            attributes.append((key, ""))
            continue
        found = _NOT_WS.search(text, i + 1)
        if not found:
            return None
        i = found.start()
        if text[i] in "\"'":
            end = text.find(text[i], i + 1)
            if end < 0:
                return None
            attributes.append((key, _attribute_value(text[i + 1:end])))
            i = end + 1
        elif text[i] == ">":
            attributes.append((key, ""))
            return name, attributes, False, i + 1
        else:
            found = _VALUE_END.search(text, i)
            if not found:
                return None
            attributes.append((key, _attribute_value(text[i:found.start()])))
            i = found.start()


def _end_tag_at(text: str, i: int, name: str) -> bool:
    """text[i:] begins the end tag of raw text called name as HTML5 reads it: "</", the name in any ASCII case, then
    whitespace, "/" or ">"."""
    after = i + 2 + len(name)
    return text.startswith("</", i) and text[i + 2:after].translate(_ASCII_LOWER) == name and text[after:after + 1] in _DELIMITERS


def _letters(text: str, i: int) -> tuple[str, int]:
    """The ASCII letters at text[i] onward, lower-cased, and the index after them."""
    found = _LETTERS.match(text, i)
    end = found.end() if found else i
    return text[i:end].translate(_ASCII_LOWER), end


def _script_end(text: str, i: int) -> int | None:
    """Where a script's content ends, as HTML5 reads script data from text[i]: at its first "</script" (and whitespace,
    "/" or ">"), except that after "<!--" a "<script" begins a double-escaped section that "</script" only leaves (Codex
    F1, Pass 8: "<script><!--<script></script><meta ...></script>" ends at the second). None: it runs to the end."""
    data, escaped, escaped_dash, escaped_dashes, double, double_dash, double_dashes = range(7)
    state, size = data, len(text)
    while True:
        if state == data:
            i = text.find("<", i)
            if i < 0:
                return None
            if _end_tag_at(text, i, "script"):
                return i
            state, i = (escaped_dashes, i + 4) if text.startswith("<!--", i) else (data, i + 1)
            continue
        if state in (escaped, double):
            found = _DASH_OR_LT.search(text, i)
            if not found:
                return None
            i = found.start()
            if text[i] == "-":
                state, i = (escaped_dash if state == escaped else double_dash), i + 1
                continue
        elif i >= size:
            return None
        elif text[i] == "-":
            state, i = (escaped_dashes if state in (escaped_dash, escaped_dashes) else double_dashes), i + 1
            continue
        elif text[i] == ">" and state in (escaped_dashes, double_dashes):
            state, i = data, i + 1
            continue
        elif text[i] != "<":
            state, i = (escaped if state in (escaped_dash, escaped_dashes) else double), i + 1
            continue
        # text[i] is "<" in an escaped or double-escaped state
        if state in (escaped, escaped_dash, escaped_dashes):
            if _end_tag_at(text, i, "script"):
                return i
            word, end = _letters(text, i + 1)
            if end >= size:
                return None
            entering = word == "script" and text[end] in _DELIMITERS
            state, i = (double, end + 1) if entering else (escaped, i + 1)
        else:
            if text.startswith("</", i):
                word, end = _letters(text, i + 2)
                if end >= size:
                    return None
                if word == "script" and text[end] in _DELIMITERS:
                    state, i = escaped, end + 1
                    continue
            state, i = double, i + 1


def _raw_text_end(text: str, start: int, name: str) -> tuple[int, int] | None:
    """(where the raw text begun at text[start] ends, the index after its end tag), as HTML5 reads it; None when it runs
    to the end of the page (plaintext always, or no end tag a browser accepts)."""
    if name == "plaintext":
        return None
    if name == "script":
        i = _script_end(text, start)
    else:
        i = text.find("</", start)
        while i >= 0 and not _end_tag_at(text, i, name):
            i = text.find("</", i + 2)
    if i is None or i < 0:
        return None
    tag = _read_tag(text, i + 2)
    return None if tag is None else (i, tag[3])


def _declaration(attributes: dict[str, str]) -> str | None:
    """The site name a <meta> tag declares, or None. As a browser reads it: the property or name, without HTML whitespace
    and lower-cased in ASCII, is og:site_name or application-name, and the content is not blank in HTML whitespace. A key
    or content only another reading accepts (other whitespace trimmed, as Python's strip or JavaScript's trim does, or
    other case folding) is AMBIGUOUS, never the employer's (Pass 9)."""
    keys = (attributes.get("property", ""), attributes.get("name", ""))
    content = attributes.get("content", "")
    if not content.strip(_WS):
        return None
    if {key.strip(_WS).translate(_ASCII_LOWER) for key in keys} & SITE_NAME_KEYS:
        return content.strip() if content.strip(_ANY_SPACE) else AMBIGUOUS
    return AMBIGUOUS if {key.strip(_ANY_SPACE).casefold() for key in keys} & SITE_NAME_KEYS else None


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
        self.site_names: list[str] = []   # every site name declared, repeats included (meta keeps only the last of each key)
        self._open_link, self._skip, self._in_title = None, 0, False
        self._title_seen = False   # the first document <title> only
        self._raw_context: list[str] = []   # the open _RAW_CONTEXT elements, innermost last
        self._raw_open: Counter[str] = Counter()   # how many of each are open: a stray end tag is found out at once (linear)
        self._raw_text_at: int | None = None   # where in _raw_context the outermost open raw-text element is (Pass 8)
        # Inside a <noscript>, the same stack as a browser with scripts off (a crawler) builds it, where noscript content is
        # markup: its end tag closes nothing while a raw-text, template or foreign element opened in it is still open
        # (browser comparison before Codex's Pass 8). A declaration then counts only where both readings agree.
        self._ns_inner: list[str] = []
        self._ns_open: Counter[str] = Counter()
        self._ns_raw_at: int | None = None

    def parse_starttag(self, i):
        """A start tag as HTML5 reads it (_read_tag), then, when it begins raw text (a title, textarea, style, script and
        the like), that text up to the end tag a browser ends it at (_raw_text_end), so nothing in it is ever markup
        (Codex F1, Pass 8)."""
        rawdata = self.rawdata
        tag = _read_tag(rawdata, i + 1)
        if tag is None:
            return -1   # the page ends inside the tag: a browser drops it, and inspect() drops what is left unread
        name, attrs, closing, end = tag
        self.lasttag = name
        if closing:
            self.handle_startendtag(name, attrs)
        else:
            self.handle_starttag(name, attrs)
        if not self._raw_text_began(name):
            return end
        found = _raw_text_end(rawdata, end, name)
        content = rawdata[end:found[0] if found else len(rawdata)]
        if content:
            self.handle_data(unescape(content) if name in ("title", "textarea") else content)
        if not found:
            return len(rawdata)   # raw text to the end of the page
        self.handle_endtag(name)
        return found[1]

    def parse_endtag(self, i):
        """An end tag as HTML5 reads it: "</" and an ASCII letter begin one, which runs to its ">" (_read_tag); "</>" is
        nothing; "</" and anything else begin a bogus comment to the first ">" (Codex F1, Pass 8: Python 3.12.10 also took
        "</ title>" for an end tag)."""
        rawdata = self.rawdata
        following = rawdata[i + 2:i + 3]
        if not following:
            return -1
        if following == ">":
            return i + 3
        if not (following.isascii() and following.isalpha()):
            return self.parse_bogus_comment(i)
        tag = _read_tag(rawdata, i + 2)
        if tag is None:
            return -1
        self.handle_endtag(tag[0])
        return tag[3]

    def _raw_text_began(self, tag: str) -> bool:
        """The start tag just handled began raw text the reader reads to its end at once: the outermost raw text (a
        noscript's content excepted, read both ways below), or raw text opened in a noscript, read scripts off."""
        if self._ns_raw_at is not None and self._ns_raw_at == len(self._ns_inner) - 1 and self._ns_inner[-1] == tag:
            return True
        return tag != "noscript" and self._raw_text_at == len(self._raw_context) - 1 and self._raw_context[-1:] == [tag]

    def _noscript_start(self, tag: str) -> None:
        if self._ns_raw_at is not None:   # raw text inside the noscript: a tag is text there
            return
        if tag in _RAW_TEXT:
            self._ns_raw_at = len(self._ns_inner)
        self._ns_inner.append(tag)
        self._ns_open[tag] += 1

    def _noscript_end(self, tag: str) -> None:
        if self._ns_raw_at is not None:
            if tag == self._ns_inner[self._ns_raw_at]:
                while len(self._ns_inner) > self._ns_raw_at:
                    self._ns_open[self._ns_inner.pop()] -= 1
                self._ns_raw_at = None
        elif self._ns_open[tag]:
            while True:
                top = self._ns_inner.pop()
                self._ns_open[top] -= 1
                if top == tag:
                    break

    def parse_comment(self, i, report=1):
        """A comment ends as in HTML5 (Codex F1, Pass 6): "<!-->" and "<!--->" are whole empty comments, and a comment ends
        at the first "-->" or "--!>". Python 3.12.10 ends one at "--" + optional whitespace + ">", so "<!-->" hid the tags
        after it and "-- >" showed tags a browser keeps commented out."""
        rawdata = self.rawdata
        for empty in ("<!-->", "<!--->"):
            if rawdata.startswith(empty, i):
                return i + len(empty)
        match = _COMMENT_END.search(rawdata, i + 4)
        if not match:
            return -1   # unclosed: feed() waits, and inspect() drops what is left unread at the end
        if report:
            self.handle_comment(rawdata[i + 4:match.start()])
        return match.end()

    def parse_marked_section(self, i, report=1):
        """"<![" as HTML5 reads it: inside SVG or MathML a CDATA section ending at "]]>", anywhere else a bogus comment
        ending at the first ">". Python 3.12.10 ran some to the next "]>" or "]]>", hiding tags a browser sees (an "<xmp>"
        after "<![endif]-->"), and raised on others (browser comparison before Codex's Pass 8)."""
        rawdata = self.rawdata
        if (self._raw_open["svg"] or self._raw_open["math"]) and rawdata.startswith("<![CDATA[", i):
            end = rawdata.find("]]>", i + 9)
            return -1 if end < 0 else end + 3
        end = rawdata.find(">", i + 3)
        return -1 if end < 0 else end + 1

    def parse_html_declaration(self, i):
        """"<![" goes to parse_marked_section on every Python version. From 3.12.12 (gh-135661) CPython's own method reads
        it without calling parse_marked_section, as a CDATA section anywhere, which a browser reads only inside SVG or
        MathML; the public CI's Linux job runs such a version (Python 3.14 review, 2026-11-03)."""
        if self.rawdata.startswith("<![", i):
            return self.parse_marked_section(i)
        return super().parse_html_declaration(i)

    def _add(self, raw: str | None, how: str, text: str = "") -> None:
        raw = (raw or "").strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")) or len(self.targets) >= MAX_TARGETS:
            return
        try:
            url = urljoin(self.base, raw)
        except ValueError:   # a malformed URL ("https://[host]/") skips that target, never the rest of the page (Codex F1, Pass 6)
            return
        self.targets.append(Target(url, how, text))

    def handle_starttag(self, tag, attrs):
        a: dict[str, str] = {}
        for k, v in attrs:   # the first of a repeated attribute counts, as in browsers (Codex F1, Pass 6)
            a.setdefault(k, v)
        if tag in _SKIP_TEXT:
            self._skip += 1
        if tag == "title":
            if not self._title_seen and not self._raw_context:   # the document's title is its first, never an SVG's (Pass 6)
                self._title_seen = self._in_title = True
        if tag in _RAW_CONTEXT:
            if self._raw_text_at is not None and self._raw_context[self._raw_text_at] == "noscript":
                self._noscript_start(tag)
            if tag in _RAW_TEXT and self._raw_text_at is None:   # raw text begins: nothing until its end tag is markup (Pass 8)
                self._raw_text_at = len(self._raw_context)
            self._raw_context.append(tag)
            self._raw_open[tag] += 1
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
            declared = _declaration(a)   # whole (Pass 4): a clipped name could read as the employer's
            # Pass 7: a tag inside a _RAW_CONTEXT element (a textarea, an SVG, after "<script/>") is no declaration of the document
            if declared is not None and not self._raw_context:   # Pass 3: a later tag never erases an earlier one
                self.site_names.append(declared)
        elif tag == "form":
            self._add(a.get("action"), "form")
        elif tag == "button":
            for key in ("formaction", "data-href", "data-url"):
                self._add(a.get(key), "data")
        for key, value in a.items():
            if key.startswith("data-") and value.startswith(("http://", "https://", "//")):
                self._add(value, "data")

    def handle_startendtag(self, tag, attrs):
        """"/>" closes only a void or a foreign element: in HTML "<iframe/>", "<template/>" or "<script/>" leaves the element
        open, as in a browser (Codex F1, Pass 7)."""
        self.handle_starttag(tag, attrs)
        if tag not in _RAW_CONTEXT or tag in _FOREIGN:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP_TEXT and self._skip:
            self._skip -= 1
        if self._raw_text_at is not None:   # in raw text only the end tag of the element that began it counts (Codex F1, Pass 8)
            opener = self._raw_context[self._raw_text_at]
            if opener == "noscript" and self._ns_inner:   # scripts off, an element opened in the noscript is still open
                self._noscript_end(tag)
            elif tag == opener:
                while len(self._raw_context) > self._raw_text_at:
                    self._raw_open[self._raw_context.pop()] -= 1
                self._raw_text_at = None
        elif self._raw_open[tag]:   # only its own end tag closes it; a stray "</math>" never does (Codex F1, Pass 7)
            while True:   # each open element is popped once: linear however deep the nesting
                top = self._raw_context.pop()
                self._raw_open[top] -= 1
                if top == tag:
                    break
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
    title: str = ""                                  # the document's first <title>, whole (Pass 4); mentions read its first 300
    meta: dict = field(default_factory=dict)
    text: str = ""
    targets: list = field(default_factory=list)
    boards: dict = field(default_factory=dict)       # "ats:token(lower)" -> {"ats", "token", "how", "url"}
    jobs: list = field(default_factory=list)         # (ats, token, posting id, url)
    systems: dict = field(default_factory=dict)      # system name -> first URL (manual only)
    offsite_jobs: list = field(default_factory=list)  # job-like links on unrecognised outside hosts
    careers_links: list = field(default_factory=list)
    site_names: list = field(default_factory=list)   # every nonblank site name the page declares (SITE_NAME_KEYS), repeats included, whole
    source_site_names: list = field(default_factory=list)   # the same, read from every <meta> tag in the source (Codex F1, Pass 7)
    read_whole: bool = False   # parsed without an error (Codex F1, Pass 6), and no <frameset> in the source (browser comparison)


def board_key(ats: str, token: str) -> str:
    return f"{ats}:{token.lower()}"


def source_site_names(html: str) -> list[str]:
    """Every site name a <meta> tag declares anywhere in the page's source, read straight from the text: in the document,
    a comment, a script or a textarea alike (Codex F1, Pass 7). Each "<meta" is read as a browser reads a tag beginning
    there (_read_tag, as in the reader since Pass 9). branded_as needs this list to equal what the reader declared, so no
    quirk of the reader can hide another company's declaration. Linear in the page: each read spends a budget on every
    character it scanned, to the end of the page when the page ends inside the tag (Codex Pass 9: such a read cost
    nothing, so a page of them took time growing with the square of its size), and a page that exhausts the budget ends
    the list with an unreadable declaration."""
    names: list[str] = []
    budget = 2 * len(html) + 100_000
    for match in _META_OPEN.finditer(html):
        start = match.start() + 1
        tag = _read_tag(html, start)
        budget -= (len(html) if tag is None else tag[3]) - start
        if budget < 0:
            return names + [UNREADABLE]
        if tag is None:
            continue   # the page ends inside it: a browser drops it
        a: dict[str, str] = {}
        for k, v in tag[1]:
            a.setdefault(k, v)
        declared = _declaration(a)
        if declared is not None:
            names.append(declared)
    return names


def inspect(html: str, base_url: str, site_domain: str) -> Inspection:
    html = _LONG_REFERENCE.sub(_short_reference, html or "")   # the same page to HTML5, and never a ValueError in unescape
    collector = _Collector(base_url)
    complete = True
    try:
        # No close(): what feed() leaves unread at the end (an unclosed comment, tag or declaration) is dropped, as HTML5
        # does at the end of a file. Python 3.12.10's close() rescans it in quadratic time (CVE-2025-6069: minutes for a
        # page under safe_fetch.MAX_BYTES; Codex F1, Pass 6). Later versions fixed that; the reader still never calls
        # close(), whose reading of the end differs from one Python version to the next.
        collector.feed(html or "")
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed so far, but never brands a hinted site
        complete = False
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
    result = Inspection(base_url, collector.title.strip(), collector.meta, " ".join(collector._text)[:20000], targets,
                        site_names=list(collector.site_names), source_site_names=source_site_names(html or ""),
                        read_whole=complete and not _FRAMESET.search(html or ""))
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
    places = (("title", inspection.title[:300]), ("site_name", " ".join(inspection.meta.values())), ("text", inspection.text))
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
