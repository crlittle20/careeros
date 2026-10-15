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


def clean_text(text: str) -> str:
    text = strip_invisible(text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " "))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
