"""Years as a role, in a function, or doing an activity (OPP-43, 2026-11-03; your decisions D90-D94).

Until now a years line was met by your best years among everything on it. A domain word on the line ("5+ years in
healthcare billing" also names healthcare), a domain word inside the area ("healthcare analytics"), or an area the reader
could not name ("2+ years as a Business Systems Analyst" was read as "professional experience") let your healthcare or
total years stand in for years in a role you have not held.

A line that names a role ("as a Business Systems Analyst", "in an analyst role"), a function ("in healthcare billing",
"payment integrity experience") or an activity ("leading cross-functional teams") is now met only by years in it: a role
title, function or skill in your profile that names it (`Profile.years_match(..., role_only=True)`), and, on a function or
activity line, a skill the posting names on that line. Your domain years still answer an alternative that is itself a
domain or an industry on a function line ("in PBM, managed care, or health plan environments"); beside a role ("as a
BSA or in a health plan environment") such an alternative is to verify, never a pass (D123 R2). Generic alternatives
("or a related field", "or similar roles") refer back to the named ones and count for nothing. When nothing in your record answers the line and
the old reading would have met it, the gate says so (`years_role_unconfirmed`, a material unknown: CONDITIONAL, verify
before applying): it never rejects on a role your record does not mention (D92). No equivalence is assumed (D93): "PBM
operations" does not answer "pharmacy operations" unless your profile says so.

A line that names only a domain ("3+ years of progressive healthcare experience"), only tools ("5+ years of experience with
SQL and Excel") or generic experience ("3+ years of relevant experience") is judged as before. The reading works on the
posting line itself, so stored requirements, skill coverage and fit are unchanged; only the years judgment moves.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from careeros.extract.rules import year_spans
from careeros.normalize.posting import contains_term, norm

from .profile import GENERIC_EXPERIENCE, Profile, _identity, alternatives

# Words a domain alternative may use besides the policy's domain vocabulary (`domain_terms`, `skill_aliases` for tools),
# and the words that place it ("industry", "setting"). Both are policy (experience.role_years) over these defaults.
DEFAULT_DOMAIN_WORDS = ["insurance", "health insurance", "managed care", "health plans", "health systems", "health system",
                        "healthcare system", "hospitals", "life sciences", "pharmaceutical", "pharmaceuticals", "pharma",
                        "biotech", "biotechnology", "financial services", "finance", "fintech", "saas", "b2b",
                        "technology", "tech", "government", "public sector", "medical", "medical office",
                        "pharmacy benefit management", "pharmacy benefits", "payor", "payors", "payers", "providers",
                        "members", "patients", "health related", "healthcare related", "health care industries",
                        "behavioral health", "outpatient", "inpatient", "digital health"]
DEFAULT_SETTING_WORDS = ["industry", "industries", "space", "setting", "settings", "environment", "environments", "company",
                         "companies", "organization", "organizations", "organisation", "organisations", "sector",
                         "sectors", "field", "fields", "domain", "landscape", "side", "vendor", "vendors", "market",
                         "markets", "lines of business"]
# Qualifiers that name no area ("relevant", "hands-on", "similar") and words that only join one.
GENERIC_WORDS = {"relevant", "related", "professional", "work", "working", "hands", "on", "direct", "directly", "progressive",
                 "progressively", "proven", "prior", "previous", "combined", "overall", "total", "applicable",
                 "commensurate", "demonstrated", "comparable", "similar", "equivalent", "responsible", "closely",
                 "other", "relative", "us", "u", "s", "full", "time", "recent", "experience", "experiences", "year",
                 "years", "solid", "strong", "practical", "relevance", "role", "roles", "position", "positions",
                 "capacity", "function", "functions", "area", "areas"}
STOP = {"a", "an", "the", "of", "in", "and", "or", "with", "as", "for", "to", "at", "within", "across", "both", "either",
        "etc", "including", "any", "all", "such", "e", "g", "i", "and/or", "one", "more"}

_QUALIFIER = (r"(?:hands[- ]on|direct|directly|progressive|progressively|relevant|professional|proven|prior|previous|"
              r"combined|total|overall|applicable|industry|work|demonstrated|responsible|related|full[- ]time|recent|"
              r"solid|strong|substantial|practical|specifically|particularly|primarily|mainly|minimum)")
_LEAD = re.compile(rf"^[\s\W]*(?:of\s+)?(?:{_QUALIFIER}\s+)*(?:experience|experiences)?\s*(?:{_QUALIFIER}\s+)*")
# A role is read only where the area starts, and never past a cut word (pre-review #2, 2026-11-04): "healthcare
# experience, preferably as a business analyst" or "...; experience as a BSA is a must" asks for healthcare years.
# "as a Business Systems Analyst", "experience serving as a ...", "in a similar role as a ...", and without the article
# ("4+ years as Product Owner", Codex Pass 1 F5, 2026-11-05), but not words after "as" that name no role ("as well as",
# "as part of", "as needed")
_BARE_AS = (r"(?<!well as )(?<!such as )(?<!soon as )(?<!known as )"   # the second "as" of "as well as", "such as" ...
            r"(?!(?:an?|the|both)\s)(?!(?:well|part|needed|required|appropriate|applicable|necessary|such|follows?|of|"
            r"per|soon|much|many|long|early|possible|assigned|directed|described|noted|outlined|stated|specified|listed|"
            r"shown|demonstrated|evidenced|indicated|mentioned|defined|in|on|at|with|for|to|is|are|and|or|it|this|that|"
            r"these|those|they|we|you|our|your|their|its|one|any|some|each|evidence|members?|outlined|above|below)\b)"
            r"(?=[a-z])")
_ROLE_AS = re.compile(r"(?:(?:working|serving|acting|employed|functioning)\s+|in (?:an?|the)\s+(?:[a-z\-]+\s+)?"
                      r"(?:role|position|capacity)\s+)?as (?:(?:an?|the|both)\s+|" + _BARE_AS + r")(?P<area>[^;.()]{2,120})")
# "in an analyst role", "in medical billing, payment posting, or a similar RCM role"
_ROLE_NOUN = re.compile(r"(?:(?:working|serving)\s+)?in (?:an?\s+|the\s+)?(?P<area>[a-z0-9 /&,'\-]{2,120}?)\s+"
                        r"(?:role|roles|position|positions|capacity)\b")
_PREP = re.compile(r"^(?:in|within|across|of|with|working (?:in|within|across|for|at))\s+(?:an?\s+|the\s+)?")
_ABILITY = re.compile(r"^(?:ability|abilities) to\s+")
_GERUND = re.compile(r"^[a-z]+ing\b")
_NOT_GERUND = {"including", "during", "bring", "string", "spring", "nothing", "something", "anything", "pricing", "billing",
               "coding", "accounting", "marketing", "engineering", "consulting", "contracting", "auditing", "testing",
               "training", "reporting", "processing", "onboarding", "underwriting", "editing", "mining", "posting",
               "credentialing", "modeling", "modelling", "programming", "nursing", "banking", "staffing", "outsourcing"}
_STARTS_CUT = re.compile(r"^(?:including|such as|e\.?g\.?|i\.?e\.?|preferably|ideally|plus|and|or|with a|[,;:.(]|"
                         r"(?:is |are )?(?:required|preferred|a plus|desired|strongly|necessary|needed|mandatory)\b)")
_CUT = re.compile(r",?\s+(?:preferably|ideally|including|especially|particularly|such as|e\.g\.|i\.e\.|where|who|which|that|"
                  r"plus|whether)\b|,\s*with\b|\s+with (?:a|an|the|strong|proven|demonstrated|demonstrable|direct|hands|"
                  r"heavy|deep|solid|real|focus)\b|[;:()]|\.(?:\s|$)|\s+(?:experience|is required|required|is preferred|"
                  r"preferred|is a plus|a plus|desired|strongly)\b")
_SCOPE_CUT = re.compile(r",?\s+(?:preferably|ideally|where|who|which|that|plus|whether)\b|,\s*with\b|\s+with (?:a|an|the|"
                        r"strong|proven|demonstrated|demonstrable|direct|hands|heavy|deep|solid|real|focus)\b|[;:]|"
                        r"\.(?:\s|$)|\s+(?:is required|required|is preferred|preferred|is a plus|a plus|desired)\b")
_PLACE = re.compile(r"\s+(?:in|within|at|for|on)\s+")   # what follows names the place, not the role or function
_OR_BEFORE = re.compile(r"(?:\bor|\band/or)\s*$")      # "... or in a health plan environment": an alternative, not the place
_OR_AFTER = re.compile(r"\s*,?\s*(?:or|and/or)\s")     # "in an analyst role or in ...": the alternatives go on after the role
# Words between "or" and an "in", "within", "at", "for" or "on" (Fable review #5 and its fix-checks, D113). Words that
# only lead into the place ("or working in", "or a career in", "or, alternatively, in") offer the place as an
# alternative: they read as "or in". Any other words ("or similar in healthcare", "or a business analyst in healthcare",
# "or a long track record of working in ...", "or similar, in ...") may name something of their own, so the line has
# two plausible readings, whatever the place names, and the gate judges both (see `read`). So does any run the reader
# cannot classify (D115): punctuation alone ("or, in healthcare"), a parenthesis ("or similar (e.g. BA) in ..."), an
# aside ("or similar roles preferably in ...", "or a position where you worked in ..."). Only "or in" itself and
# runs of filler words are certain. The run takes any number of words, punctuation and whole parentheses.
_OR_WORDS = re.compile(r"\b(?P<sep>or|and/or)\b(?P<words>(?:[^;:()]|\([^;:()]*\)){0,200}?)\s(?P<prep>in|within|at|for|on)\s+")
_OR_FILLER = {"a", "an", "the", "of", "working", "work", "worked", "employed", "employment", "experience", "experiences",
              "career", "careers", "background", "exposure", "history", "tenure", "time", "having", "been", "serving",
              "spent", "relevant", "prior", "previous", "direct", "professional", "hands-on", "industry",
              "alternatively", "else"}
# A role right after the area's first words is a role in them (Fable review #6, 2026-11-04, D113): "healthcare
# experience as a BSA", "experience in healthcare as a BSA" and "in healthcare in a BSA role" ask for years as a BSA in
# healthcare, as "as a BSA in healthcare" does (D90). Only "as a/an", "working/serving/employed as a/an" and "in a ...
# role" count here: "acting as the primary point of contact" is a duty, not a role. It is a judgement call, so the gate
# also judges the line without it (see `read`).
_ROLE_NEXT = re.compile(r"(?:(?:working|serving|employed|functioning)\s+)?as (?:(?:an?|both)\s|" + _BARE_AS + r")|in (?:an?|the)\s")
_IN_FIELD = re.compile(r"(?:in|within)\s+(?P<field>[^;:()]+?),?\s+(?=" + _ROLE_NEXT.pattern + ")")   # "in healthcare, as"
_NOT_A_ROLE = {"this", "that", "these", "your", "our", "same", "similar", "related", "a", "an", "the", "such"}
# How someone works, not a role they hold ("as a team member", "as an individual contributor", "in a client-facing
# role", "in a leadership role"): after a field, such a clause leaves the field's years asked for (fix-check, D113)
_DUTY_WORDS = {"member", "members", "team", "teams", "contributor", "contributors", "individual", "cross-functional",
               "client-facing", "customer-facing", "patient-facing", "member-facing", "provider-facing", "facing",
               "leadership", "leader", "leaders", "point", "contact", "primary", "key", "liaison", "partner", "part",
               "player", "hands-on", "self-starter", "independent", "independently", "collaborative"}
_TRAILING = r"(?:[\s,;:.\-]+(?:and|or|plus|as well as|with|including|at least|of|in))*[\s,;:.\-]*$"
# D123 (2026-11-09; replaces the D118/D120 readers of alternatives): three build-and-check rounds tried to tell a line's
# real alternatives from harmless "or"s by word lists, and each check found new wordings. The gate no longer decides
# which cue is a real alternative: a role-years line that holds ANY of these cues never rejects on the role's shortfall
# (it is CONDITIONAL years_role_unconfirmed: verify before applying), and nothing but years in the role, or in a role the
# line names that your record confirms in the same words, ever passes it. Over-flagging is accepted ("(Agile or
# Waterfall)", "(1st or 2nd shift)", "in the OR" flag too). The cues, in one place: a word ("or", "and/or", "either",
# "alternatively", "equivalent", "in lieu", "instead", "substitute...", "accept...", "consider...", "qualif...", "counts",
# and "also", "too", "as well", "works", "welcome" for acceptance said other ways: "Product Owner experience works too"),
# "/" between two words, and a parenthesis or bracket holding two or more items ("(BSA, PO)", "(RN/BSN)")
ALTERNATIVE_CUES = re.compile(
    r"\b(?:and/or|or|either|alternatively|equivalent\w*|in lieu|instead|substitut\w*|accept\w*|consider\w*|qualif\w*|counts|"
    r"also|too|as well|works|welcom\w*)\b|[A-Za-z0-9]\s*/\s*[A-Za-z0-9]|[(\[][^()\[\]]*(?:[,/;&|]|\b(?:or|and)\b)[^()\[\]]*[)\]]",
    re.I)
# A line ends in a joiner (", or", "; OR", ", Or", "AND/OR", "or a", "or…", "[OR]", "**or**", "-- or --"), in any case
# and whatever punctuation or markup surrounds it: it may offer the next line as its alternative, so the NEXT line's
# role-years shortfall is CONDITIONAL too (D123 R3)
_JOINER_END = re.compile(r"(?:^|[\W_])(?:or|and/or)(?:\s+(?:an?|the))?[\W_]*$", re.I)
# A line that is only a joiner ("OR", "- Or -", "<p>OR</p>"), opens with one ("- or 5+ years as a Product Owner"), or
# leads in a list of alternatives ("Either:", "One of the following:", "Any of the following:"): every years line of
# its block may be an alternative (D123's check, R3 widened)
_JOINER_LINE = re.compile(r"^[\W_]*(?:or|and/or)(?:[\W_]*$|\s)", re.I)
# A lead-in names "of the following" anywhere in a line without a figure ("Must meet ONE of the following", "at least one
# of the following", "one or more of the following", "Either of the following"), or opens with "either" alone or before a
# colon ("Either:"); with no colon either (the check of 5df563c)
_LEAD_IN = re.compile(r"\b(?:either|any|one|any\s+one|one\s+or\s+more)\s+of\s+the\s+following\b", re.I)
_EITHER = re.compile(r"^[\W_]*either\b", re.I)
# D149 R-Y1 (Codex Pass 2 B and the AI-quote High, 2026-11-12): a joiner or substitution word ANYWHERE in the posting's
# requirements sections, whatever punctuation or markup stands around it ("OR:", "- or -", "(or)", "_or_", "may satisfy
# this requirement", "is accepted", "in lieu of"), read from the posting text itself, never from an AI record or quote.
# With one, no role, function or activity years demand of the posting rejects: its far shortfall is CONDITIONAL
# (years_role_unconfirmed). Over-flagging ("SQL or Python" elsewhere in the list) is the owner's accepted cost.
SECTION_CUES = re.compile(r"(?<![A-Za-z0-9])(?:and/or|or|either|alternatively|instead|in\s+lieu|in\s+place\s+of|substitut\w*|satisf\w*|"
                          r"equivalen\w*|accept\w*|consider\w*|(?:any|one|any\s+one|one\s+or\s+more)\s+of\s+the\s+following)"
                          r"(?![A-Za-z0-9])", re.I)


_SPACED_JOINER = re.compile(r"^[\W_]*(?:o[\W_]+r|a[\W_]*n[\W_]*d[\W_]*/[\W_]*o[\W_]*r)[\W_]*$", re.I)
_SHORT_LEAD_IN = re.compile(r"(?<![A-Za-z0-9])(?:any|one|either)\s+of\s*:", re.I)


# D151 (under D149): the words a demand may hold besides total experience, a domain or a tool and still be plain
_DEMAND_FILLER = {"required", "requirement", "requirements", "requires", "require", "must", "have", "has", "having", "need",
                  "needed", "minimum", "min", "least", "over", "more", "plus", "including", "includes", "exp", "yrs", "yr",
                  "yoe", "using", "use", "knowledge", "proficiency", "proficient", "skills", "skill", "expertise",
                  "advanced", "familiarity", "background", "is", "are", "be", "you", "candidates", "applicants", "will",
                  "should", "month", "months", "mos", "old", "older", "age", "aged", "client", "customer", "acting",
                  "contributor", "cross", "functional"}


_DEMAND_SEPARATOR = re.compile(r"[;,.]|\b(?:and|or|plus)\b")


def demand_segment(text: str, years: float, occurrence: int = 0) -> str | None:
    """A years demand's own words on its posting line, without the figure itself; None when the figure is not on the line
    (D151). They are the words after it up to the next figure, and the words before it: the whole start of the line for
    the first figure ("Product Owner: 10+ years"), and for a later one only those after the last separator before it
    ("SQL: 3+ years; Product Owner: 10+ years"), since the rest belongs to the figure before."""
    low = re.sub(r"[^\S\n]", " ", (text or "").lower())
    spans = year_spans(low)
    for index, (start, end, value, _upper) in enumerate(spans):
        if value != years:
            continue
        if occurrence:
            occurrence -= 1
            continue
        lead = low[:start]
        if index:
            lead = low[spans[index - 1][1]:start]
            cuts = list(_DEMAND_SEPARATOR.finditer(lead))
            lead = lead[cuts[-1].end():] if cuts else ""
        tail = low[end:spans[index + 1][0]] if index + 1 < len(spans) else low[end:]
        if index + 1 < len(spans):   # the words after the last separator lead into the next figure
            cuts = list(_DEMAND_SEPARATOR.finditer(tail))
            tail = tail[:cuts[-1].start()] if cuts else tail
        return lead + " " + tail
    return None


def plain_demand(text: str, years: float, policy, occurrence: int = 0, vocabulary: bool = True) -> bool:
    """D151 (a): whether a years demand names, in its own words on the posting line, only total professional experience,
    a domain or a tool the policy's vocabulary knows ("10+ years of professional experience", "9+ years of PBM
    experience", "10+ years with SQL"). Anything else (a role, a title, a function, an activity, a word the reader cannot
    classify: "Product Owner: 10+ years", "roles such as Product Owner") is not plain: such a demand is never judged on
    years counted for something else. Without `vocabulary`, a domain or a tool is no plain word either: the demand names
    only total professional experience (`total_demand`, D155)."""
    segment = demand_segment(text, years, occurrence)
    if segment is None:
        return False
    rest = f" {norm(segment).replace('/', ' ')} "
    rest = re.sub(r"(?<![a-z])\d[\d+]*(?![a-z])|\+", " ", rest)
    for phrases in _vocabulary(policy) if vocabulary else ():
        for phrase in phrases:
            while f" {phrase} " in rest:
                rest = rest.replace(f" {phrase} ", " ")
    # how someone works ("as a team member", "in a client-facing role") and an age beside the demand name no area
    return not [w for w in rest.split() if w not in STOP and w not in GENERIC_WORDS and w not in _DEMAND_FILLER
                and w not in _DUTY_WORDS and w not in _NOT_A_ROLE]


def total_demand(text: str, years: float, policy, occurrence: int = 0) -> bool:
    """D155 (1): whether a years demand names, in its own words on the posting line, only total professional experience
    ("10+ years of professional experience", "Minimum 10 years of experience"): no domain, tool, role, function or
    activity. Only such a demand may reject on years."""
    return plain_demand(text, years, policy, occurrence, vocabulary=False)


def section_cue(lines) -> str | None:
    """The first cue of an alternative in the posting's requirements lines (`SECTION_CUES`), with the line it is on, or
    None when no line holds one (D149 R-Y1)."""
    for line in lines:
        # full-width letters read as plain ones ("ＯＲ"), and a joiner spelled out letter by letter ("o r") or a short
        # lead-in ("One of:") counts too (red team round 1, the contrived Y3 inputs)
        wide = unicodedata.normalize("NFKC", line or "")
        found = SECTION_CUES.search(wide) or _SPACED_JOINER.match(wide) or _SHORT_LEAD_IN.search(wide)
        if found:
            return f'{found.group(0).strip()}" in "{line.strip()[:120]}'
    return None


@dataclass(frozen=True)
class Reading:
    kind: str                                   # role | function | activity
    area: str                                   # what the line asks years of, as read
    alternatives: tuple[tuple[str, str], ...]   # (alternative, specific | tool | domain); generic ones are dropped
    scope: str = ""                             # the area with its examples: a skill counts only when named here
    place: str = ""                             # where, when the line says ("in healthcare"): never a credit by itself
    doubt: str = ""                             # the judgement call this reading made, when it made one
    others: tuple = ()                          # the other plausible readings then (None: the line as before OPP-43)

    def phrase(self) -> str:
        area = _GENERIC_TAIL.sub("", self.area)   # "business systems analyst, or equivalent": the cue is no part of it
        return {"role": f"as {area}", "activity": area}.get(self.kind, f"in {area}")


# A generic alternative at the end of an area ("..., or equivalent", "or similar"), left out where the area is quoted
_GENERIC_TAIL = re.compile(r",?\s*\b(?:or|and/or)\s+(?:an?\s+)?(?:equivalent|similar|related|comparable)\b.*$")
# Roles joined by "and" in one area ("as a BSA and Product Owner", "as both a BSA and a PO", "BSA, PO and Scrum Master"):
# each is a demand, so the area is met only when every one is (D123's check, 2026-11-09). An "or", "and/or" or "/"
# makes them alternatives instead.
_AND = re.compile(r"\s*(?:,|\band\b|&)\s*")
_NOT_AND = re.compile(r"\bor\b|\band/or\b|/")


def conjoined(area: str, policy) -> list[str]:
    """The roles or functions an area joins by "and" (two or more, each naming something specific), or []."""
    if _NOT_AND.search(area) or not re.search(r"\band\b|&", area):
        return []
    parts = [re.sub(r"^(?:both|either)\s+", "", re.sub(r"^(?:an?|the)\s+", "", p.strip())) for p in _AND.split(area)]
    parts = [re.sub(r"^(?:an?|the)\s+", "", p) for p in parts if p]
    return parts if len(parts) > 1 and all(area_kind(p, policy) == "specific" for p in parts) else []


_VOCABULARY: dict = {}


def _vocabulary(policy) -> tuple[list[str], list[str], list[str]]:
    """(domain phrases, place phrases, tool phrases), longest first, normalized; one per policy."""
    key = getattr(policy, "fingerprint", None) or id(policy)
    if key not in _VOCABULARY:
        cfg = policy.get("experience", "role_years", default={}) or {}
        aliases = policy.get("skill_aliases", default={}) or {}
        domains = [t for terms in (policy.get("domain_terms", default={}) or {}).values() for t in terms]
        domains += cfg.get("domain_words") or DEFAULT_DOMAIN_WORDS
        tools = [w for words in aliases.values() for w in words] + list(aliases)
        settings = cfg.get("setting_words") or DEFAULT_SETTING_WORDS

        def phrases(words) -> list[str]:
            return sorted({norm(w) for w in words if norm(w)}, key=len, reverse=True)
        _VOCABULARY[key] = (phrases(domains), phrases(settings), phrases(tools))
    return _VOCABULARY[key]


def area_kind(text: str, policy) -> str:
    """generic, domain, tool or specific: what one alternative of an area names once its qualifiers ("relevant",
    "hands-on"), domain phrases ("health plan", "PBM"), place words ("industry", "setting") and tools are set aside."""
    if norm(text) in GENERIC_EXPERIENCE:
        return "generic"
    domains, settings, tools = _vocabulary(policy)
    rest, seen = f" {norm(text)} ", set()
    for group, phrases in (("domain", domains), ("place", settings), ("tool", tools)):
        for phrase in phrases:
            if f" {phrase} " in rest:
                rest = rest.replace(f" {phrase} ", " ")
                seen.add(group)
    if [w for w in rest.split() if w not in STOP and w not in GENERIC_WORDS]:
        return "specific"
    return "tool" if "tool" in seen else "domain" if "domain" in seen else "generic"


def _split(area: str) -> list[str]:
    """An area's alternatives: commas, "or" and "and/or" separate them; "/" and "and" never do ("claims/insurance
    operations" and "process improvement and workflow design" are one alternative each)."""
    parts = re.split(r",|\band/or\b|\bor\b", area)
    return [p.strip(" -'") for p in parts if p.strip(" -'")]


def _place_split(area: str) -> tuple[str, str]:
    """(the area, where): the first "in", "within", "at", "for" or "on" names the place, unless "or" stands right before
    it (pre-review #5, 2026-11-04): "as a business analyst or in a health plan environment" offers the environment as an
    alternative (to verify: D123 R2)."""
    for found in _PLACE.finditer(area):
        if not _OR_BEFORE.search(area[:found.start()]):
            return area[:found.start()], area[found.end():]
    return area, ""


def _clean(area: str, place: bool) -> str:
    area = _CUT.split(area, maxsplit=1)[0]
    if place:
        area = _place_split(area)[0]
    # a dangling joiner goes, with its article ("... analyst or a", wrapped: D118)
    return re.sub(r"(?:[\s,;:.\-]+(?:and|or|plus|as well as|with|including|at least|of|in)(?:\s+(?:an?|the))?)*[\s,;:.\-]*$", "",
                  area).strip(" -,'")


def alternative_cue(text: str) -> str | None:
    """The first cue on a line that it may offer an alternative to a role (`ALTERNATIVE_CUES`, D123 R1), as written; None
    when there is none. It does not decide whether the alternative is real: any cue makes a role's shortfall CONDITIONAL."""
    found = ALTERNATIVE_CUES.search(text or "")
    return found.group(0) if found else None


def ends_in_joiner(text: str) -> bool:
    """Whether a posting line ends in a joiner (", or", "; OR", "AND/OR", "or a", "or…"), in any case (D123 R1, R3)."""
    return bool(_JOINER_END.search((text or "").rstrip()))


def opens_alternatives(text: str) -> bool:
    """Whether a posting line is only a joiner, opens with one, or leads in alternatives without a figure of its own
    ("Either:", "One of the following:"): the lines of its block may be alternatives to one another (D123 R3)."""
    text = (text or "").strip()
    return bool(_JOINER_LINE.match(text) or leads_in(text))


def leads_in(text: str) -> bool:
    """Whether a posting line, holding no years figure, leads in a list of alternatives ("Either:", "One of the
    following", "Must meet ONE of the following", "at least one of the following"): the list below it, across blank
    lines, may hold alternatives to one another (D123 R3; the check of 5df563c)."""
    text = (text or "").strip()
    if not text or year_spans(text):
        return False
    alone = _EITHER.match(text) and (text.endswith(":") or not _EITHER.sub("", text).strip(" -*:.•"))
    return bool(_LEAD_IN.search(text) or alone)


def _role_next(rest: str, head: str, policy) -> tuple[str, str] | None:
    """(where a role clause starts, the field before it) when the area starts with a field and a role follows it:
    "<field> experience as a ..." or "in <field> as a ..."; otherwise None (Fable review #6, 2026-11-04). The clause
    must name a role ("in a fast-paced environment" does not, nor "as a team member"), an area that already starts with
    a role keeps it ("as a BSA with healthcare experience as a consultant"), and a field of generic words ("working
    experience as a ...") is no field."""
    if _ROLE_AS.match(head):
        return None
    cut = _CUT.search(rest)
    if cut and cut.group(0).strip() == "experience":
        if _ROLE_NOUN.match(head):   # "in an analyst role with healthcare experience as ...": the role named first
            return None
        field, clause = head, _CUT.split(rest[cut.end():].strip(), maxsplit=1)[0].lstrip(", ")
    elif found := _IN_FIELD.match(head):
        field, clause = found.group("field"), head[found.end():]
    else:
        return None
    role = (_ROLE_AS.match(clause) or _ROLE_NOUN.match(clause)) if _ROLE_NEXT.match(clause) else None
    area = re.sub(r"^(?:an?|the)\s+", "", _clean(role.group("area"), place=True)) if role else ""
    if role is None or not area or _OR_BEFORE.search(field):   # "in healthcare or in an analyst role": not a field
        return None
    if role.re is _ROLE_NOUN and len(area.split()) > 6:   # "in a payer environment demonstrating ... for the position"
        return None
    if not set(area.split()) - STOP - GENERIC_WORDS - _DUTY_WORDS - _NOT_A_ROLE:   # "as a team member": a duty
        return None
    field = re.sub(_TRAILING, "", re.sub(r"^(?:an?|the)\s+", "", field.strip())).strip()
    return clause, "" if area_kind(field, policy) == "generic" else field


def _offer(after: str, policy, guess: bool) -> tuple[str, bool]:
    """`after` with each "or <words> in <place>" read as "or in <place>" where the words only lead into the place ("or
    working in", "or a career in"); with `guess`, also where they may name something of their own ("or similar in
    healthcare", "or similar in claims auditing"). Returns the text and whether such a guess was open (the fix-checks of
    Fable #5, D113)."""
    open_ = False

    def swap(found) -> str:
        nonlocal open_
        run = found.group("words")
        if not run.strip():   # "or in" itself: the alternative (pre-review #5)
            return found.group(0)
        offer = f"{found.group('sep')} {found.group('prep')} "
        words = re.findall(r"[a-z0-9'\-]+", run)
        if words and set(words) <= _OR_FILLER and "(" not in run:
            return offer
        open_ = True   # anything else: two readings, and neither rejects alone (D115)
        return offer if guess else found.group(0)
    return _OR_WORDS.sub(swap, after), open_


def _phrase(text: str, years: float, occurrence: int = 0) -> str:
    """The text that follows this years figure (the figure's `occurrence`-th time on the line, from 0), up to the next
    figure on the line. Whatever else the line offers is not read here: any cue of an alternative on the line makes a
    shortfall CONDITIONAL in the gate (D123 R1)."""
    # any Unicode horizontal space is a space here too (D149 R-Y3; the sanitizer does the same for new postings)
    low = re.sub(r"[^\S\n]", " ", (text or "").rstrip().lower().replace("’", "'"))
    spans = year_spans(low)
    for i, (_start, end, value, _upper) in enumerate(spans):
        if value == years:
            if occurrence:   # the same figure stated again for another demand (D120)
                occurrence -= 1
                continue
            return re.sub(r"^\(s\)", "", low[end:spans[i + 1][0] if i + 1 < len(spans) else len(low)])
    return ""


def read(text: str, years: float | None, policy, occurrence: int = 0) -> Reading | None:
    """What a years line asks years of, when that is a role, a function or an activity. None for a line that names only a
    domain, tools or generic experience, or that cannot be read: the gate then judges it as before. `occurrence` reads
    the figure's later appearance on the line, for a figure stated twice for two demands (D120).

    Two steps are judgement calls: a role read after its field ("healthcare experience as a BSA") and an alternative
    after "or <words> in <domain>" whose words may name something of their own ("or similar in healthcare"). When one is
    taken, the reading says so (`doubt`) and carries the line's other plausible readings (`others`); the gate judges the
    line on each, and where they judge it differently it is CONDITIONAL (`years_role_unconfirmed`), never a rejection
    (D92; the Fable fix-check of ff3c7f7, 2026-11-05, D113; D114). A line is rejected only when every reading rejects it,
    and only when the line holds no cue of an alternative (`alternative_cue`, D123)."""
    if years is None:
        return None
    after = _phrase(text, years, occurrence)
    if not after:
        return None
    first, steps = _read(after, policy, guess=False, after_field=True)
    others, doubt = [], []
    if "field" in steps:
        others.append(_read(after, policy, guess=False, after_field=False)[0])
        doubt.append("a role read after the field it was held in")
    if "offer" in steps:
        others.append(_read(after, policy, guess=True, after_field=True)[0])
        doubt.append('an alternative after "or" that may be the place')
    others = [other for other in others if other != first]
    if not others:
        return first
    if first is None:   # the line as before OPP-43 is one reading, and the guess the other
        first, others = others[0], [None, *others[1:]]
    return replace(first, doubt="; ".join(doubt), others=tuple(others)) if first is not None else None


def _read(after: str, policy, guess: bool, after_field: bool) -> tuple[Reading | None, set]:
    """One reading of the text after a years figure, and the judgement calls it was open to ("field", "offer")."""
    steps = set()
    after, open_ = _offer(after, policy, guess)   # "or working in a health plan" = "or in a health plan"
    if open_:
        steps.add("offer")
    kind, raw, place = "", "", True
    rest = _LEAD.sub("", after, count=1).strip()
    head = _CUT.split(rest, maxsplit=1)[0]   # where the area starts, up to "preferably", "including", ";" ...
    next_role = _role_next(rest, head, policy) if after_field else None   # "healthcare experience as a BSA"
    head, field = next_role or (head, "")
    if field:
        steps.add("field")
    role = _ROLE_AS.match(head) or _ROLE_NOUN.match(head)
    if role:
        raw = role.group("area")
        if role.re is _ROLE_NOUN and _OR_AFTER.match(head[role.end():]):   # "in an analyst role or in a health plan"
            raw += head[role.end():]
        if set(re.sub(r"^(?:an?|the)\s+", "", _clean(raw, place=True)).split()) <= _NOT_A_ROLE:
            role = None   # "in this role", "in a similar role": no role named
        else:
            kind = "role"
    if not role:
        if not rest or _STARTS_CUT.match(rest):
            return None, steps
        gerund = _GERUND.match(rest)
        if _PREP.match(rest):
            kind, raw = "function", _PREP.sub("", rest, count=1)
        elif _ABILITY.match(rest) or (gerund and gerund.group(0) not in _NOT_GERUND):
            kind, raw = "activity", _ABILITY.sub("", rest, count=1)
        elif re.search(r"\bexperiences?\b", rest):
            kind, raw, place = "function", re.split(r"\bexperiences?\b", rest, maxsplit=1)[0], False
        elif re.match(r"^[\s\W]*of\s", after):   # "5+ years of vendor-side ownership of implementations"
            kind, raw = "function", rest
        else:
            return None, steps
    area = re.sub(r"^(?:an?|the)\s+", "", _clean(raw, place=place))
    scope = _SCOPE_CUT.split(raw, maxsplit=1)[0]   # the area with its examples ("such as Excel, SQL")
    where = _place_split(_CUT.split(raw, maxsplit=1)[0])[1].strip() if place else ""
    if kind == "role" and field:   # the field before the role is where it was held, beside any place after it
        where = f"{field}; {where}" if where else field
    if not area:
        return None, steps
    if kind == "activity":   # an activity is one thing: a domain word inside it is only its subject
        if area_kind(area, policy) != "specific":
            return None, steps
        return Reading(kind, area, ((area, "specific"),), scope), steps
    if conjoined(area, policy):   # roles joined by "and": one alternative, met only when every role is
        return Reading(kind, area, ((area, "all"),), scope, where), steps
    alts = tuple((a, area_kind(a, policy)) for a in _split(area))
    alts = tuple((a, k) for a, k in alts if k != "generic")
    if not any(k == "specific" for _, k in alts):
        return None, steps
    return Reading(kind, area, alts, scope, where), steps


def names_domain(record, text: str, policy) -> bool:
    """Whether `text` names the domain of a domain record (by the policy's words for that domain)."""
    terms = (policy.get("domain_terms", default={}) or {}).get(record.name) or [record.name]
    return any(contains_term(norm(text), t) for t in terms)


def demand_records(records: list, text: str, years: float, occurrence: int, policy) -> list:
    """The records of a line that one demand of a figure stated twice names in its own words ("8+ years as a BSA and 8+
    years of healthcare experience": healthcare is the second demand's), so each demand is judged on its own (D120); all
    of them when its words name none."""
    phrase = norm(_phrase(text, years, occurrence))
    aliases = policy.get("skill_aliases", default={}) or {}
    named = [r for r in records if (names_domain(r, phrase, policy) if r.category == "domain" else
                                    any(contains_term(phrase, w) for w in (*aliases.get(r.name, ()), *alternatives(r.name))))]
    return named or records


def line_credit(records: list, reading: Reading, profile: Profile, policy) -> dict | None:
    """The best profile item that answers a line read as a role, function or activity, with its years (None when nothing
    in your record does). Records are the line's years records (one source line, one figure). The credit lists, as
    `unconfirmed`, every alternative your record does not confirm (none of it, or only a partial match): unless the
    credit meets the figure, such an alternative is to be verified, never taken as short (Codex Pass 1 F2, F4)."""
    found, unconfirmed = [], []
    for alternative, kind in reading.alternatives:
        if kind == "all":
            # roles joined by "and" (D123's check): the area as your record names it in its own words, else every role
            # it joins, each confirmed; one your record does not confirm leaves no credit (to verify), and the credit is
            # the weakest role, with all of them (`parts`) for the gate to judge
            whole = profile.years_match(alternative, role_only=True)
            if whole and not whole.get("partial") and _identity(whole["item"]) == _identity(alternative):
                found.append(whole)
                continue
            parts = [(part, profile.years_match(part, role_only=True)) for part in conjoined(alternative, policy)]
            if any(not m or m.get("partial") or m.get("years") is None for _, m in parts):
                unconfirmed += [part for part, m in parts if not m or m.get("partial") or m.get("years") is None]
                continue
            held = [(part, m) for part, m in parts if m]   # every part is confirmed here (the check above)
            weakest = min((m for _, m in held), key=lambda m: m["years"])
            found.append({**weakest, "item": alternative, "parts": tuple((part, m["years"]) for part, m in held)})
            continue
        if kind != "specific" and reading.kind == "role":
            # a domain or a tool a role line offers as an alternative ("as a BSA or in a health plan environment"): years
            # in it are not years in the role, so they never pass it; it is to be verified (D123 R2)
            unconfirmed.append(alternative)
            continue
        if kind in ("specific", "tool"):
            # each listed item on its own: "finance systems, business applications" never matches "business systems
            # analysis" by taking one word from each item
            names = alternatives(alternative)
            # ... and each item confirmed on its own: in "BSA/Product Owner" the BSA never hides the Product Owner
            # (the Fable fix-check of dba7b35, 2026-11-05)
            for name in (names[1:] if len(names) > 2 else names[:1]):
                matches = [m for m in [profile.years_match(name, role_only=True)] if m]
                found += matches
                if all(m.get("partial") or m.get("years") is None for m in matches):   # nothing confirms this item
                    unconfirmed.append(name)
            continue
        elif reading.kind != "activity":   # a domain the line offers as an alternative: your years in that domain
            matches = [m for m in (profile.years_match(r.name) for r in records
                                   if r.category == "domain" and names_domain(r, alternative, policy)) if m]
            match = profile.years_match(alternative)
            if match and match["kind"] == "domain":
                matches.append(match)
            found += matches
        else:
            continue
        if not any(m.get("years") is not None and not m.get("partial") for m in matches):
            unconfirmed.append(alternative)
    if reading.kind != "role":   # a tool the area names on a function or activity line ("data analysis", "such as SQL")
        aliases = policy.get("skill_aliases", default={}) or {}
        named = [r for r in records if r.category == "skill"
                 and any(contains_term(norm(reading.scope), w) for w in aliases.get(r.name, [r.name]))]
        found += [m for m in (profile.years_match(r.name, role_only=True) for r in named) if m]
    found = [m for m in found if m.get("years") is not None]
    # What your record confirms comes first; a partial match is the credit only when nothing confirms any alternative,
    # and then it confirms neither a shortfall nor a pass (D3; the Fable fix-check of dba7b35)
    best = max([m for m in found if not m.get("partial")] or found, key=lambda m: m["years"], default=None)
    return {**best, "unconfirmed": tuple(unconfirmed)} if best else None
