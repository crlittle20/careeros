"""Contextual preference detection for the shadow evaluator (SCORING_SHADOW_SPEC 2026-10-16, section 5.1). PURE.

Your preferences live in an optional file, vault/profile/preferences.yaml. Without it nothing is detected:

  dealbreakers:          # a CONFIRMED mention sets the job aside as a preference conflict (you may still override)
    - name: inbound phone work
      terms: [inbound calls, member calls, call center]
  avoid:                 # a confirmed mention only annotates the job, ordered after the others of the same status
    - name: overnight shifts
      terms: [overnight shift]
  want: []               # recorded, no effect in v1

Each mention is judged in its context:
  confirmed  affirmative, in the title or a duties / requirements section
  negated    "no phone work", "not a call-center role", "without direct reports": evidence the trait is absent
  uncertain  hedged ("may occasionally"), outside the duty sections (benefits, company or EEO text, text before the
             first heading, a posting with no headings), or a negation too far from the phrase to be sure
An uncertain dealbreaker never changes anything beyond a visible "verify: <phrase>" fact. None of this changes a status
or a production gate, and detection precision is measured on a labelled sample before a conflict is relied on.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from careeros.extract.rules import _heading
from careeros.normalize.posting import norm

CONFIRMED, NEGATED, UNCERTAIN = "confirmed", "negated", "uncertain"
KINDS = ("dealbreakers", "avoid", "want")
DUTY_SECTIONS = ("responsibilities", "required", "preferred", "ambiguous")
NEGATORS = {"no", "not", "never", "without", "non", "zero", "none"}
NOT_NEGATING = {("not", "only"), ("not", "just"), ("not", "limited")}   # "not only answer calls but ..."
NEGATION_WINDOW = 3      # words right before the phrase
HEDGE_WORDS = {"may", "might", "occasionally", "occasional", "possibly", "potentially", "could", "periodically", "rarely",
               "sometimes", "some"}
HEDGE_PHRASES = ("as needed", "if needed", "from time to time", "on occasion", "at times")
HEDGE_BEFORE, HEDGE_AFTER = 6, 3
_CLAUSES = re.compile(r"[.;:!?]+")


def load_preferences(path: Path | None) -> dict:
    """{kind: [{name, terms}]}; empty when the file is missing."""
    if path is None or not Path(path).exists():
        return {}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = {}
    for kind in KINDS:
        items = []
        for item in data.get(kind) or []:
            if isinstance(item, str):
                item = {"name": item}
            terms = [str(t) for t in item.get("terms") or [item["name"]] if norm(t)]
            items.append({"name": str(item["name"]), "terms": terms})
        out[kind] = items
    return out


def _negated_before(tokens: list[str], start: int) -> tuple[bool, bool]:
    """(negated right before the phrase, a negation earlier in the clause)."""
    window = range(max(0, start - NEGATION_WINDOW), start)
    near = any(tokens[i] in NEGATORS and (tokens[i], tokens[i + 1] if i + 1 < len(tokens) else "") not in NOT_NEGATING
               for i in window)
    earlier = any(t in NEGATORS for t in tokens[:max(0, start - NEGATION_WINDOW)])
    return near, earlier


def _hedged(tokens: list[str], start: int, end: int) -> bool:
    around = tokens[max(0, start - HEDGE_BEFORE):start] + tokens[end:end + HEDGE_AFTER]
    text = f" {' '.join(around)} "
    return bool(HEDGE_WORDS & set(around)) or any(f" {h} " in text for h in HEDGE_PHRASES)


def judge(clause: str, term: str, context: str) -> list[dict]:
    """Every mention of `term` in one clause, each with its state. `context` is 'title', a section name or 'none'."""
    tokens, wanted = norm(clause).split(), norm(term).split()
    found = []
    for start in range(len(tokens) - len(wanted) + 1):
        if tokens[start:start + len(wanted)] != wanted:
            continue
        end = start + len(wanted)
        near, earlier = _negated_before(tokens, start)
        if near:
            state, why = NEGATED, "negated"
        elif earlier:
            state, why = UNCERTAIN, "negation scope unclear"
        elif _hedged(tokens, start, end):
            state, why = UNCERTAIN, "hedged"
        elif context != "title" and context not in DUTY_SECTIONS:
            state, why = UNCERTAIN, "outside the duties and requirements"
        else:
            state, why = CONFIRMED, "affirmative"
        found.append({"state": state, "why": why, "context": context, "phrase": " ".join(tokens[max(0, start - 3):end + 3])})
    return found


def mentions(job: dict, term: str) -> list[dict]:
    """Every mention of `term` in the job's title and posting, judged in context."""
    out = []
    for clause in _CLAUSES.split(job.get("title") or ""):
        out += judge(clause, term, "title")
    lines = [line for line in (job.get("description_text") or "").split("\n") if line.strip()]
    section = None
    for line in lines:
        found = _heading(line)
        if found:
            section = found
            continue
        for clause in _CLAUSES.split(line):
            out += judge(clause, term, section or "none")
    return out


def detect(job: dict, prefs: dict) -> dict:
    """{conflict, conflicts, avoid, verify, absent} for one job. Only CONFIRMED dealbreaker mentions conflict."""
    result: dict = {"conflict": False, "conflicts": [], "avoid": [], "verify": [], "absent": []}
    for kind in ("dealbreakers", "avoid"):
        for pref in prefs.get(kind) or []:
            found = [m for term in pref["terms"] for m in mentions(job, term)]
            states = {m["state"] for m in found}
            if CONFIRMED in states:
                hit = next(m for m in found if m["state"] == CONFIRMED)
                result["conflicts" if kind == "dealbreakers" else "avoid"].append({"name": pref["name"], "phrase": hit["phrase"],
                                                                                   "context": hit["context"]})
            elif UNCERTAIN in states and kind == "dealbreakers":
                hit = next(m for m in found if m["state"] == UNCERTAIN)
                result["verify"].append({"name": pref["name"], "fact": f"verify: {hit['phrase']}", "why": hit["why"]})
            elif NEGATED in states:
                result["absent"].append(pref["name"])
    result["conflict"] = bool(result["conflicts"])
    return result
