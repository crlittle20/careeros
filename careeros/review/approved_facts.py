"""Approved facts (R6, 2026-10-16): statements about your work that prepared text must quote word for word.

They live in their own vault file (vault/profile/approved_facts.yaml), never in the shared code and never in the
profile, so they never change a decision or re-judge a job; only drafts read them.

  - id: a short name
    text: the exact wording to use (a full sentence)
    applies_to: [profile items the fact supports, e.g. a function name]
    superseded: [figures no longer used, e.g. an older count]
    source: where the wording comes from and when you confirmed it

`check(text, facts)` reads prepared text for the ways a figure gets distorted: turned into a daily rate, made a
guarantee, made into ownership the fact doesn't state, or an old figure used again. It reports; it never rewrites.

Since 2026-10-18 (fresh-context review): wider word lists ("5,000/day", "each shift", "responsible for", "managing",
"spearheaded", "ensured"...), spelled-out and space-grouped figures ("five thousand", "5 000"), percentages, every
approved quote taken out before sentences are split (so a quote's own "approx." or a second fact's figure is never
reported), words right beside a quote read as part of it, years and phone-like numbers ignored, and field types checked.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_FIGURE = re.compile(r"(?<![\d.\-])(?:\d{1,3}(?:[,\u00a0\u202f ]\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:k\b|thousand\b|million\b|%))?(?![\d\-])",
                     re.I)
_SMALL = {w: n for n, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * n for n, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALE = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}
_WORDS = re.compile(r"\b(?:(?:a|an|" + "|".join([*_SMALL, *_TENS]) + r")[\s-]+)+(?:(?:hundred|thousand|million|and|"
                    + "|".join([*_SMALL, *_TENS]) + r")[\s-]*)*(?:hundred|thousand|million)\b", re.I)
_ABBREVIATIONS = {"approx", "e.g", "i.e", "inc", "vs", "etc", "no", "co", "corp", "ltd", "mr", "ms", "mrs", "dr", "jr", "sr", "st"}
_MARK = "\u2063FACT{}\u2063"
DISTORTIONS = {
    "a daily rate": re.compile(r"\b(?:per|a|each|every)\s+(?:day|shift|work\s?day|business\s+day|working\s+day)\b|\bdaily\b|"
                               r"/\s*(?:day|shift)\b", re.I),
    "a guarantee": re.compile(r"\b(?:guarantee(?:s|d|ing)?|always|consistently|every\s+time|without\s+fail|ensur(?:e|ed|es|ing))\b",
                              re.I),
    "ownership the fact doesn't state": re.compile(
        r"\b(?:owned|owner\s+of|led|leading|managed|managing|oversaw|overseeing|(?:was\s+)?(?:solely\s+)?responsible\s+for|"
        r"single-handedly|directed|directing|ran|headed|spearheaded)\b", re.I),
}
_NEAR = 40   # characters on either side of a quote that are read as part of it


def load(path: Path | None) -> list[dict]:
    """The approved facts; [] when the file is missing. Each must have an id and a text; applies_to and superseded must
    be lists (a single string would be read letter by letter)."""
    if path is None or not Path(path).exists():
        return []
    facts = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(facts, list):
        raise ValueError("approved_fact_invalid: the file must be a list of facts")
    out = []
    for fact in facts:
        if not isinstance(fact, dict) or not str(fact.get("id") or "").strip() or not str(fact.get("text") or "").strip():
            raise ValueError(f"approved_fact_invalid: {fact!r}")
        applies, old = fact.get("applies_to") or [], fact.get("superseded") or []
        if not isinstance(applies, list) or not all(isinstance(a, str) for a in applies) \
                or not isinstance(old, list) or not all(isinstance(s, (str, int, float)) for s in old):
            raise ValueError(f"approved_fact_invalid: applies_to and superseded must be lists ({fact.get('id')})")
        out.append({"id": str(fact["id"]).strip(), "text": str(fact["text"]).strip(),
                    "applies_to": [a.strip().lower() for a in applies if a.strip()],
                    "superseded": [str(s) for s in old], "source": str(fact.get("source") or "")})
    return out


def _spelled(text: str) -> list[float]:
    """'five thousand' = 5000, 'one hundred twenty thousand' = 120000, 'a hundred thousand' = 100000."""
    out = []
    for m in _WORDS.finditer(text):
        total, current = 0, 0
        for word in re.split(r"[\s-]+", m.group(0).lower()):
            if word in ("a", "an"):
                current = max(current, 1)
            elif word in _SMALL:
                current += _SMALL[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word in ("thousand", "million"):
                total += max(current, 1) * _SCALE[word]
                current = 0
        out.append(float(total + current))
    return out


def _numbers(text: str) -> list[tuple[float, bool]]:
    """(value, is_percent) for every figure: '5,000', '5 000', '5k', '5 thousand', 'five thousand' and '98%'."""
    out = []
    for m in _FIGURE.finditer(text):
        raw = re.sub(r"[\s,\u00a0\u202f]", "", m.group(0)).lower()
        percent = raw.endswith("%")
        raw = raw.rstrip("%")
        unit = 1_000 if raw.endswith(("k", "thousand")) else 1_000_000 if raw.endswith("million") else 1
        number = re.match(r"\d+(?:\.\d+)?", raw)
        if not number:
            continue
        value = float(number.group(0)) * unit
        if unit == 1 and not percent and "." not in number.group(0) and 1900 <= value <= 2100 and len(number.group(0)) == 4:
            continue   # a year, not a count
        out.append((value, percent))
    return out + [(v, False) for v in _spelled(text)]


def _figures(text: str) -> set[float]:
    """Every number in the text as a value: "5,000", "5k", "5 thousand" and "five thousand" are the same figure."""
    return {v for v, _ in _numbers(text)}


def _key_figures(fact_text: str) -> set[float]:
    """The figures that make a fact: counts of 100 or more, and any percentage ("98%")."""
    return {v for v, percent in _numbers(fact_text) if v >= 100 or percent}


def _sentences(text: str) -> list[str]:
    """Sentences, split on '.', '!' or '?' followed by a space (never after an abbreviation such as 'approx.'), and on a
    line break only where a new item starts (a bullet, a heading or a blank line); a wrapped line stays one sentence."""
    text = re.sub(r"\n(?!\s*(?:[-*#]|\d+[.)]|\n))", " ", text or "")
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s)|\n+", text):
        if m.group(0) == ".":
            before = re.search(r"([A-Za-z.]+)\.$", text[start:m.end()])
            if before and before.group(1).lower().rstrip(".") in _ABBREVIATIONS:
                continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def check(text: str, facts: list[dict]) -> list[dict]:
    """Issues in prepared `text`: a sentence that uses an approved fact's figure, or sits right beside its exact quote,
    and turns it into a daily rate, a guarantee or ownership; or a superseded figure used at all. The exact quotes
    themselves are fine, wherever they appear: every one is taken out before anything else is read."""
    marked = text or ""
    for n, fact in enumerate(facts):
        marked = marked.replace(fact["text"], f" {_MARK.format(n)} ")
    issues = []
    sentences = _sentences(marked)
    for i, whole in enumerate(sentences):
        plain = re.sub("\u2063FACT\\d+\u2063", " ", whole)
        # a qualifier left alone in a short next sentence ("Approx. 5,000 tickets. Daily.") is read with this one
        # (independent Codex review of f984d15, 2026-10-20)
        after = re.sub("\u2063FACT\\d+\u2063", " ", sentences[i + 1]) if i + 1 < len(sentences) else ""
        tail = after if after and len(after.split()) <= 3 and not _figures(after) else ""
        for n, fact in enumerate(facts):
            figures = _key_figures(fact["text"])
            mark = _MARK.format(n)
            near = ""
            for m in re.finditer(re.escape(mark), whole):
                near += " " + re.sub("\u2063FACT\\d+\u2063", " ", whole[max(0, m.start() - _NEAR):m.end() + _NEAR])
            if figures & _figures(plain) or near:
                scope = (plain if figures & _figures(plain) else near) + " " + tail
                for kind, pattern in DISTORTIONS.items():
                    found = pattern.search(scope)
                    if found:
                        issues.append({"fact": fact["id"], "kind": kind, "words": found.group(0),
                                       "sentence": (plain.strip() + (" " + tail if tail else ""))[:200]})
            for old in fact["superseded"]:
                if _figures(old) & _figures(plain):
                    issues.append({"fact": fact["id"], "kind": "a superseded figure", "words": old, "sentence": plain.strip()[:200]})
    return issues
