"""Posting text cleaning. Posting text is untrusted data, never instructions (GrowthOS Guard.Text pattern)."""
from __future__ import annotations

import html
import re
import unicodedata

_BLOCK_TAGS = re.compile(r"</?(p|div|br|ul|ol|table|tr|section|article|h[1-6])\b[^>]*>", re.I)
_LIST_ITEM = re.compile(r"<li\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
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


def clean_text(text: str) -> str:
    text = _LINE_SEPARATOR.sub("\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = strip_invisible(_HORIZONTAL_SPACE.sub(" ", _ZERO_WIDTH_SPACE.sub(" ", text)))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
