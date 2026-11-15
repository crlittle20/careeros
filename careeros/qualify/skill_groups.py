"""Skills offered as alternatives (OPP-80, 2026-11-03; your decision D100, built as D101 says).

"Tableau or Power BI", "Excel, SQL, Tableau, Power BI, or similar tools" and "one of SQL, Python or R" each ask for one
skill, met by the best one you have, as certifications already are (qualify/certifications.py). Until now each skill
counted on its own, so a posting that accepts Tableau or Power BI cost you Tableau even when you have Power BI.

Two or more skills named on one line, of one requirement type, are alternatives when only commas, "or" or "and/or"
stand between them and an "or" joins them, the list ends in "or ..." ("or similar tools", "or Looker"), or "one of",
"any of" or "either" opens it. "And" never makes alternatives, nor does a slash alone ("SQL/Python" often means both),
and an "or" elsewhere on the line never joins separate skills. A semicolon ends a list, and the opening words count
only right before its first skill ("one of: SQL, Python", never "at least one year with Excel, SQL" or an earlier
sentence; pre-review #8-#9, 2026-11-04). The group counts once, as its best member (your strongest evidence, then the
most important skill; the first named on a tie). A member the posting also asks for on its own, on another line, still
counts there.
"""
from __future__ import annotations

import re
from collections.abc import Callable

_CUE = re.compile(r"\b(?:(?:one|any)(?: or more)? of(?: the following)?|either)\s*:?\s*$")   # right before the list
_TAIL_OR = re.compile(r"^\s*,?\s*(?:or|and/or)\b")


def _spot(name: str, text: str, aliases: dict) -> tuple[int, int] | None:
    """Where a skill is first named on the line (by any of its policy aliases), or None."""
    best = None
    for word in aliases.get(name, [name]):
        found = re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text)
        if found and (best is None or found.start() < best[0]):
            best = (found.start(), found.end())
    return best


def _joiner(between: str) -> str | None:
    rest = re.sub(r"[\s,]+", " ", between).strip()   # a semicolon is not a comma: it ends the list
    if rest in ("or", "and/or", "or a", "or an"):
        return "or"
    return "list" if rest in ("", "/") else None


def groups(requirements: list, policy, types: tuple) -> list[list[str]]:
    """Each set of alternatives, as skill names in the order the line names them."""
    aliases = policy.get("skill_aliases", default={}) or {}
    by_line: dict = {}
    for r in requirements:
        if r.category == "skill" and r.requirement_type in types:
            by_line.setdefault((r.source_text, r.requirement_type), set()).add(r.name)
    found = []
    for (text, _type), names in by_line.items():
        low = (text or "").lower()
        spots = sorted((spot, name) for name in names if (spot := _spot(name, low, aliases)))
        run, joined = spots[:1], False
        for previous, current in zip(spots, spots[1:], strict=False):
            kind = _joiner(low[previous[0][1]:current[0][0]])
            if kind:
                run.append(current)
                joined = joined or kind == "or"
                continue
            found += _close(run, joined, low)
            run, joined = [current], False
        found += _close(run, joined, low)
    return found


def _close(run: list, joined: bool, low: str) -> list[list[str]]:
    if len(run) < 2:
        return []
    alternatives = joined or bool(_TAIL_OR.search(low[run[-1][0][1]:])) or bool(_CUE.search(low[:run[0][0][0]]))
    return [[name for _spot_, name in run]] if alternatives else []


def alternatives(requirements: list, policy, types: tuple, rank: Callable[[str], tuple]) -> tuple[set, dict]:
    """(the skills that count only as a lesser alternative, {best member: the alternatives it stands for}).
    `rank(name)` orders the members: the highest counts for the group."""
    found = groups(requirements, policy, types)
    best_of: dict = {}
    lesser: set = set()
    for members in found:
        best = max(members, key=lambda n: (rank(n), -members.index(n)))
        best_of.setdefault(best, members)
        lesser |= set(members) - {best}
    # a skill also asked for on a line where it is no one's alternative still counts on its own
    lines: dict = {}
    for r in requirements:
        if r.category == "skill" and r.requirement_type in types:
            lines.setdefault(r.name, set()).add((r.source_text, r.requirement_type))
    alone = {name for name, where in lines.items() if len(where) > sum(1 for members in found if name in members)}
    return lesser - alone - set(best_of), best_of
