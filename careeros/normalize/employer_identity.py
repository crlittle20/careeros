"""Employer identity (R1, 2026-10-16): which employer family a posting belongs to, how sure that is, and why.

PURE. Used by submission permission (R2, careeros/review/submission.py) and the NIGHT familiar-employer check (R3); no
gate, score or discovery step calls it.

A posting names its employer in up to two places: `employer_name` (as posted) and `parent` (stored at intake: it may
be missing, may be a copy of the employer name, and is never recomputed when the `parents` aliases change). Each name
is matched against the employer families in policy `parents` (a family name and its aliases) after normalisation
(accents and full-width letters folded, case, punctuation, legal suffixes such as "Inc." or "LLC", a leading "The"):
  different_organisation  a name you listed as a DIFFERENT organisation that only looks like a family member (a union
                          whose name starts with an alias): never a family match
  exact                   equals the family name or an alias
  compact                 equals one with the spaces removed ("NorthgateInsight, Inc." = "northgate insight")
  board_alias             a board alias (the employer_aliases table) that points to a name that matches
  prefix                  starts with an alias followed by more words ("Northgate Rx Services"); longest alias first
  agency                  a staffing agency: it names the recruiter, not the hiring employer
  confidential            a confidential or undisclosed client ("Confidential", "Undisclosed Fortune 500")
  agency_signal           a name with "staffing", "recruiting" or "talent" in it that matches nothing above: a signal
                          that the poster may be an agency, NOT proof (the owner's rule, 2026-10-18); the hiring
                          employer needs review. To confirm such a name is the employer, list it in `parents`.
  own                     anything else: the employer is its own family
  ambiguous               the name matches two families through one alias, or a family and a guard or an agency
                          equally well: a configuration contradiction, never settled by sort order
When a guard, a family and an agency all match (2026-10-17), the MOST SPECIFIC wins: a whole-name match (exact, compact,
board alias) beats a prefix, and a longer matched name beats a shorter one. So a guard longer than the alias it shadows
("Northgate Care Workers" over "Northgate") wins, a family alias longer than a short guard wins, and a listed agency
whose name starts with a family alias stays an agency. A tie is ambiguous.
The result is one of:
  resolved     one family (a stored parent naming a known family wins over an employer name that matches nothing);
               with no family match, the stored parent's own name, else the employer's
  conflicting  the names point to different employers, e.g. a stale parent, or a name is ambiguous: never treated as
               either one
  unresolved   no usable name, a confidential client (unless a stored parent names a known family), an agency
               posting that does not name its client, or an agency-signal name (unless a stored parent names a known
               family, or the employer name is an ordinary name under an agency-signal parent)
Evidence records which name matched what, by which rule and alias, in a fixed order: the same posting always gets the
same answer. Nothing here decides what a match means (a hold, a release): callers do, and an unresolved or conflicting
identity must never be read as "not held".
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .posting import contains_term, norm

RESOLVED, UNRESOLVED, CONFLICTING = "resolved", "unresolved", "conflicting"
FAMILY_RULES = ("exact", "compact", "board_alias", "prefix")
FIELDS = ("employer_name", "parent")
_LEGAL = re.compile(r"\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc|lp|llp|holdings)\b")
AGENCY_SIGNAL_TERMS = ("staffing", "recruiting", "talent")   # the owner's list (2026-10-18): review, not proof
CONFIDENTIAL_TERMS = ("confidential", "undisclosed", "anonymous", "withheld", "not disclosed", "unnamed", "stealth",
                      "our client", "a client", "client of")


def legal_norm(name: str | None) -> str:
    """Lowercase, punctuation and legal suffixes removed, and a leading 'the': 'The Northgate Rx, Inc.' -> 'northgate rx'.
    Accents and full-width letters are folded first ('Nórdica' = 'Nordica'), never dropped."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c))
    text = re.sub(r"\s+", " ", _LEGAL.sub(" ", norm(folded))).strip()
    return text[4:] if text.startswith("the ") else "" if text == "the" else text


@dataclass(frozen=True)
class Match:
    field: str           # employer_name | parent
    value: str           # as stored
    normalized: str
    rule: str            # one of the rules in the module docstring, or "empty"
    family: str | None   # the family it points to (exact / compact / board_alias / prefix only)
    via: str | None      # the alias, board alias, agency or guard that matched


@dataclass(frozen=True)
class EmployerIdentity:
    status: str                  # resolved | unresolved | conflicting
    family: str | None           # a family name from policy, or the employer's own name; None unless resolved
    known_family: bool           # True when `family` is one of the policy families
    reason: str | None           # why it is unresolved or conflicting, or which name decided it
    evidence: tuple[Match, ...]

    @property
    def key(self) -> str | None:
        return legal_norm(self.family) if self.family else None


def _aliases(parents: dict) -> list[tuple[str, str, str]]:
    """(normalized alias, family, alias as written), longest first, then alphabetical: a deterministic match order."""
    found = set()
    for family, aliases in (parents or {}).items():
        for alias in [family, *(aliases or [])]:
            key = legal_norm(alias)
            if key:
                found.add((key, str(family), str(alias)))
    return sorted(found, key=lambda a: (-len(a[0]), a[0], a[1], a[2]))   # every field: no tie left to set order


def _starts(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + " ")


def starts_with_name(name: str, prefix: str) -> bool:
    """`name` is `prefix` or starts with it followed by more words, also when its first words are written together
    ("NorthgateCare Workers East" starts with "northgate care workers"). Both normalised with legal_norm."""
    if not prefix:
        return False
    if _starts(name, prefix):
        return True
    words, glued = name.split(), prefix.replace(" ", "")
    return any("".join(words[:i]) == glued for i in range(1, len(words) + 1))


def _size(key: str) -> int:
    return len(key.replace(" ", ""))


def _longest(name: str, keys: Iterable[str], *, glued: bool = False) -> str | None:
    """The most specific key the name starts with (word boundary; first words written together too when `glued`)."""
    test = starts_with_name if glued else _starts
    found = sorted((k for k in set(keys) if k and test(name, k)), key=lambda k: (-_size(k), k))
    return found[0] if found else None


def match_name(field: str, value: str | None, parents: dict, *, agencies: Iterable[str] = (),
               board_aliases: dict[str, str | list[str]] | None = None, different: Iterable[str] = ()) -> Match:
    """How one stored name matches. A guard (different organisation), a family and an agency can all match one name:
    the most specific wins, and a tie, or one alias shared by two families, is ambiguous (see the module docstring)."""
    raw = (value or "").strip()
    name = legal_norm(raw)
    if not name:
        return Match(field, raw, name, "empty", None, None)
    agencies, different = tuple(agencies), tuple(different)
    aliases = _aliases(parents)
    compact = name.replace(" ", "")
    whole = _size(name) + 1   # a whole-name match is more specific than any prefix of the name
    family: tuple[int, str, list[tuple[str, str]]] | None = None   # (specificity, rule, [(family, alias as written)])
    exact = [(f, w) for a, f, w in aliases if a == name]
    squeezed = [(f, w) for a, f, w in aliases if a.replace(" ", "") == compact]
    if exact:
        family = (whole, "exact", exact)
    elif squeezed:
        family = (whole, "compact", squeezed)
    else:
        targets = (board_aliases or {}).get(name)
        if targets:
            inner = [match_name(field, t, parents, agencies=agencies, different=different)
                     for t in ([targets] if isinstance(targets, str) else list(targets))]
            found = sorted({(m.family, f"{name} -> {m.via}") for m in inner if m.family})
            # every target counts (independent Codex review of f984d15, 2026-10-20): an alias to a known agency, a
            # guarded name or a confidential client keeps that classification, and one beside a family is ambiguous
            other = sorted({(m.rule, f"{name} -> {m.via}") for m in inner if not m.family and m.rule not in ("own", "empty")})
            if len({f for f, _ in found}) > 1:
                return Match(field, raw, name, "ambiguous", None, "board alias names two families")
            if (found and other) or len({r for r, _ in other}) > 1:
                return Match(field, raw, name, "ambiguous", None,
                             "board alias names " + " and ".join(sorted({"a family" for _ in found} | {r for r, _ in other})))
            if other:
                return Match(field, raw, name, other[0][0], None, other[0][1])
            if found:
                family = (whole, "board_alias", [found[0]])
        if family is None:
            key = _longest(name, (a for a, _, _ in aliases))
            if key:
                family = (_size(key), "prefix", [(f, w) for a, f, w in aliases if a == key])
    guard = _longest(name, (legal_norm(g) for g in different), glued=True)
    agency = _longest(name, (legal_norm(a) for a in agencies))
    candidates = [c for c in ((family[0], family[1]) if family else None,
                              (whole if guard == name else _size(guard), "different_organisation") if guard else None,
                              (whole if agency == name else _size(agency), "agency") if agency else None) if c]
    if candidates:
        best = max(size for size, _ in candidates)
        winners = [rule for size, rule in candidates if size == best]
        if len(winners) > 1:
            return Match(field, raw, name, "ambiguous", None, " = ".join(sorted(winners)))
        if winners[0] == "different_organisation":
            return Match(field, raw, name, "different_organisation", None, guard)
        if winners[0] == "agency":
            return Match(field, raw, name, "agency", None, agency)
        assert family is not None
        families = sorted({f for f, _ in family[2]})
        if len(families) > 1:
            return Match(field, raw, name, "ambiguous", None, f"one alias in two families: {', '.join(families)}")
        return Match(field, raw, name, family[1], families[0], sorted(w for _, w in family[2])[0])
    for term in CONFIDENTIAL_TERMS:
        if contains_term(name, term):
            return Match(field, raw, name, "confidential", None, term)
    for term in AGENCY_SIGNAL_TERMS:
        if contains_term(name, term):
            return Match(field, raw, name, "agency_signal", None, term)
    return Match(field, raw, name, "own", None, None)


def resolve(employer_name: str | None, parent: str | None, parents: dict, *, agencies: Iterable[str] = (),
            board_aliases: dict | None = None, different: Iterable[str] = ()) -> EmployerIdentity:
    """The employer identity of one posting from its two stored names."""
    agencies, different = tuple(agencies), tuple(different)
    employer, stored = (match_name(f, v, parents, agencies=agencies, board_aliases=board_aliases, different=different)
                        for f, v in zip(FIELDS, (employer_name, parent), strict=True))
    evidence = (employer, stored)
    if "ambiguous" in (employer.rule, stored.rule):
        return EmployerIdentity(CONFLICTING, None, False, "ambiguous_name", evidence)
    families = sorted({m.family for m in evidence if m.family})
    if len(families) > 1:
        return EmployerIdentity(CONFLICTING, None, False, "names_point_to_different_families", evidence)
    if families:
        # A subsidiary name the aliases don't list, under a parent naming the family, is that family. The reverse (a
        # family name under a parent naming some other employer) is a contradiction; so is a stored parent pointing a
        # name you listed as a different organisation at the family (an older prefix match left it there).
        if employer.family and stored.rule == "own":
            return EmployerIdentity(CONFLICTING, None, False, "parent_names_a_different_employer", evidence)
        if stored.family and employer.rule == "different_organisation":
            return EmployerIdentity(CONFLICTING, None, False, "employer_name_is_a_different_organisation", evidence)
        decided = employer if employer.family else stored
        return EmployerIdentity(RESOLVED, families[0], True, f"{decided.field}_{decided.rule}", evidence)
    if "confidential" in (employer.rule, stored.rule):
        return EmployerIdentity(UNRESOLVED, None, False, "confidential_client", evidence)
    if employer.rule == "agency_signal" or (stored.rule == "agency_signal" and employer.rule != "own"):
        # an agency-signal poster: a stored parent of its own may be the agency's holding company, never proof of the
        # client; an ordinary employer name under an agency-signal parent is that employer
        return EmployerIdentity(UNRESOLVED, None, False, "possible_agency_name_needs_review", evidence)
    if employer.rule == "agency" and stored.rule == "own":
        # a listed agency's own stored parent is likelier its holding company than the client (Codex review, 2026-10-20)
        return EmployerIdentity(UNRESOLVED, None, False, "agency_client_not_named", evidence)
    if stored.rule == "own":
        return EmployerIdentity(RESOLVED, stored.value, False, "parent_own_name", evidence)
    if employer.rule == "own":
        return EmployerIdentity(RESOLVED, employer.value, False, "employer_name_own_name", evidence)
    if "agency" in (employer.rule, stored.rule):
        return EmployerIdentity(UNRESOLVED, None, False, "agency_client_not_named", evidence)
    if "different_organisation" in (employer.rule, stored.rule):
        named = employer if employer.rule == "different_organisation" else stored
        return EmployerIdentity(RESOLVED, named.value, False, f"{named.field}_different_organisation", evidence)
    return EmployerIdentity(UNRESOLVED, None, False, "no_employer_name", evidence)


def resolve_job(job: dict, policy, *, agencies: Iterable[str] = (), board_aliases: dict | None = None,
                different: Iterable[str] = (), parents: dict | None = None) -> EmployerIdentity:
    """`resolve` with the families from policy `parents` (or the `parents` given) and the staffing agencies from policy
    `source_discovery`, plus any the caller adds (the private ones in the vault). Reads nothing else."""
    return resolve(job.get("employer_name"), job.get("parent"),
                   parents if parents is not None else policy.get("parents", default={}) or {},
                   agencies=[*listed_agencies(policy), *agencies], board_aliases=board_aliases, different=different)


def listed_agencies(policy) -> list[str]:
    return list((policy.get("source_discovery", default={}) or {}).get("staffing_agencies") or [])


def in_families(identity: EmployerIdentity, names: Iterable[str], parents: dict, **kw) -> bool | None:
    """Whether a resolved identity belongs to the family of any of `names` (each name resolved the same way). None when
    the identity is unresolved or conflicting: the caller must never read that as False."""
    if identity.status != RESOLVED:
        return None
    keys = set()
    for name in names:
        found = resolve(name, None, parents, **kw)
        if found.status == RESOLVED and found.key:
            keys.add(found.key)
    return identity.key in keys
