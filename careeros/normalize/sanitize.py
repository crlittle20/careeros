"""Posting text cleaning. Posting text is untrusted data, never instructions (GrowthOS Guard.Text pattern)."""
from __future__ import annotations

import html
import re
import unicodedata

_BLOCK_TAGS = re.compile(r"</?(p|div|br|ul|ol|table|tr|section|article|h[1-6])\b[^>]*>", re.I)
_LIST_ITEM = re.compile(r"<li\b[^>]*>", re.I)
# a real tag only: "<" then a tag name (or "/" and one, a comment, a declaration), never a literal "<" in the text
# ("Travel: <10%", "under < 5 days"): reading from it to the next ">" deleted the lines between (the red team's
# fr1-saved-2)
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>|<!--.*?-->|<![^<>]*>", re.S)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def strip_invisible(text: str) -> str:
    """Drop control and Unicode format characters (zero-width, bidi overrides) except newline and tab."""
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))


def html_to_text(markup: str) -> str:
    if not markup:
        return ""
    text = html.unescape(markup)
    if "&lt;" in markup or "&gt;" in markup:  # Greenhouse returns HTML-escaped HTML
        text = html.unescape(text)
    text = _SCRIPT.sub(" ", text)
    text = _LIST_ITEM.sub("\n- ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return clean_text(text)


# Unicode horizontal whitespace: no-break, ogham, the en/em and other fixed-width spaces, narrow no-break, medium
# mathematical and ideographic spaces, and tab (D149 R-Y3, Codex Pass 2 D: "as&#8239;Product Owner" kept U+202F, so the
# role reader missed the role). Each becomes an ASCII space before any parsing; newlines are untouched.
_HORIZONTAL_SPACE = re.compile("[\t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
# Zero-width separators (zero-width space, word joiner, a no-break space mark inside the text) separate words too:
# "as&#8203;Product Owner" is "as Product Owner", never "asProduct Owner"; the line and paragraph separators U+2028 and
# U+2029 end a line (D149, the follow-up). U+200C and U+200D join letters and are stripped with the other format marks
_ZERO_WIDTH_SPACE = re.compile("[\u200b\u2060\ufeff]")
_LINE_SEPARATOR = re.compile("[\u2028\u2029]")
# the other controls that end a line (vertical tab, form feed, the file/group/record separators, next line) end it
# here too, before strip_invisible drops control characters: deleted, they joined a blank "Location:" to the next
# line of prose (Codex CR-2026-11-15-GATE-FIXES H3, Pass 7)
_LINE_CONTROL = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85]")


def split_newlines(text: str) -> str:
    """Every line ending as "\\n": CRLF, CR, the Unicode line and paragraph separators and the line-ending controls."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _LINE_CONTROL.sub("\n", _LINE_SEPARATOR.sub("\n", text))


def _clean(text: str) -> str:
    text = split_newlines(text)
    text = strip_invisible(_HORIZONTAL_SPACE.sub(" ", _ZERO_WIDTH_SPACE.sub(" ", text)))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def canonical(text: str) -> str:
    """The one form of text that enters CareerOS (the D190 design, section 3a): the cleaner above (line endings as
    "\\n"; runs of spaces and tabs collapsed and each line's ends stripped; zero-width spaces turned into spaces and other
    invisible format characters removed; three or more newlines become two), with Unicode NFC as its LAST step, after
    every deletion and replacement (Codex's D190 design review, Pass 5: with NFC earlier, a removed format character could
    join a letter to a combining mark, and a second cleaning gave other bytes). canonical(canonical(x)) == canonical(x).
    It is what the card shows and what P1 compares: there is no second, tidier form."""
    return unicodedata.normalize("NFC", _clean(text))


clean_text = canonical   # every description route reads the canonical form (point 1)


def canonical_cut(text: str | None, limit: int) -> str:
    """`text` cut to `limit` characters, then made canonical again: a cut can end on a space or split what NFC
    joins."""
    return canonical((text or "")[:limit])


# a key that names a link, or a reference (JSON-LD's @id and @context): its value is never changed
_URL_KEY = re.compile(r"url|link|href|^@id$|^@context$|sameas|image|logo", re.I)
_URL_VALUE = re.compile(r"\s*(https?:|//|mailto:)", re.I)


def canonical_values(value, key: str = ""):
    """A parsed payload with every text value made canonical, before any reader reads it: a board API item at the
    adapter entry, a saved page's JSON-LD, the owner's policy and profile (the D190 design, section 3a, points 3, 4
    and 7). A link is never changed: a value under a key that names one, or a value that is one."""
    if isinstance(value, str):
        return value if _URL_KEY.search(key) or _URL_VALUE.match(value) else canonical(value)
    if isinstance(value, dict):
        return {k: canonical_values(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical_values(v, key) for v in value]
    return value
