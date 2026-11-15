"""Rules-based requirement extraction. Every record keeps its verbatim source line and a confidence.

Ambiguous qualifications are treated as required (lower confidence), never silently downgraded to preferred.
"""
from __future__ import annotations

import re

from careeros.normalize.posting import contains_term, norm

from .model import Extraction, Requirement

EXTRACTOR_ID = "rules-v16"  # v16 (2026-11-13): v15 with the D149/D150 readings (Codex Pass 2 of the combined
# candidate): every years figure kept through the AI merge, a demanded line saying "years" with no record is to
# verify, ages are never years of experience, Unicode spaces are ASCII spaces. Never deployed before; one new id, so
# every posting is read again exactly once.
# v15 (2026-11-11): v14 with the intake hotfix's final readings (d662c91: the whole-section
# softener rule read sentence by sentence, a posting with no headings one section; D131, D132, D138, D140). Never deployed
# before; one new id, so every posting is read again exactly once.
# v14 (2026-11-10): v13 with the intake hotfix's Codex Pass 1 corrections (each credential
# of a sentence takes its own "helpful" and "not required" reading; d156418). Never deployed before; one new id, so every
# posting is read again exactly once.
# v13 (2026-11-09): the combined candidate (v12 below) joined with the live reader of
# 2026-11-09, which also called itself v11: a certification sentence ending "helpful" is preferred, not
# required-with-equivalency. One new id, so every posting is read again exactly once (not yet deployed).
# With it, the live intake hotfix (rules-v11b, D116): "helpful" makes a certification preferred only when it
# governs that credential ("PMP certification, Agile experience helpful" keeps PMP's section reading).
# v12 (2026-11-04): the "great fit if" family of requirements headings, and an excluded
# phrase only where the skill's word is lowercase (pre-review #10); v11 (2026-11-03): a
# skill word inside an excluded phrase is not that skill ("several facets of the role" is not the Facets system); v10
# (2026-10-15): one record per certification, by sentence; v9: a years figure per area
# Headway's "You'd be a great fit if…" and Unite Us's "You're a great fit for this role if :" are requirements headings
# of seven or more words, which the word cap in _heading refuses (2026-11-04: the Headway posting was parked as
# requirements_unclear after its 11-03 reformat). The whole family, matched as a whole line, before that cap.
_FIT_IF = re.compile(r"^you(?: d| would| ll| will| re| are) (?:be )?an? (?:great|good|strong) fit"
                     r"(?: for (?:this|the) (?:role|position))? if(?: you(?: have)?)?$")

_HEADINGS = [
    ("preferred", ["preferred qualifications", "preferred skills", "preferred experience", "nice to have", "nice to haves",
                   "bonus points", "bonus", "pluses", "preferred", "desired qualifications", "desired skills", "extra credit",
                   "we'd be especially interested if", "especially interested if", "bonus if", "even better if",
                   "it's a plus if", "it would be great if"]),
    ("required", ["required qualifications", "minimum qualifications", "basic qualifications", "required skills",
                  "required experience", "requirements", "what you'll need", "what you will need", "what you need",
                  "must haves", "must have", "you must have", "qualifications required", "job requirements",
                  "you should get in touch if", "what you will bring", "what you'll bring", "what you bring",
                  "you might be a fit if", "you're a great fit if", "you'll be a great fit if", "you will need"]),
    # Checked before "other", so "Who We Are Looking For" is a requirements list, not the company's "Who we are".
    ("ambiguous", ["qualifications", "who you are", "what we're looking for", "what we are looking for", "about you",
                   "skills and experience", "experience", "skills", "you have", "you bring", "your background",
                   "who we are looking for", "who we're looking for", "we are looking for", "we're looking for",
                   "additional qualifications", "additional requirements", "ideal candidate", "the ideal candidate",
                   "what you have", "who we want", "your key strengths"]),
    ("responsibilities", ["responsibilities", "key responsibilities", "what you'll do", "what you will do", "duties",
                          "the role", "your impact", "day to day", "in this role", "job duties", "essential functions",
                          "primary responsibilities", "what you'll be doing", "role overview", "in this role you will",
                          "what you will work on", "what you'll work on", "what you will be working on",
                          "what you'll be working on", "about the role", "about this role", "about the position",
                          "about the job", "about the opportunity", "first 90 days", "first 6 months"]),
    # Company, benefits, hiring-process and legal sections: never requirements, even when they follow the
    # qualifications list (a live scan on 2026-10-12 found these unrecognised in 133 target-family postings).
    ("other", ["benefits", "perks", "about us", "about the company", "who we are", "compensation", "pay range",
               "equal opportunity", "eeo", "our values", "why join", "life at", "location", "salary",
               "success in this role looks like", "success in this role", "job summary", "what we offer", "offers you",
               "what you can expect", "what we provide", "our benefits", "benefits starting", "what makes us different",
               "interview process", "interviewing with", "equal employment opportunity", "cybersecurity awareness notice",
               "fraud and security notice", "other notices", "referral instructions", "pay transparency",
               "what you'll get here", "what you'll be part of", "where you will work"]),
]
_ABOUT = re.compile(r"^about \w+")  # "About Example Health", "About Acme": the company, unless matched above
_YOU_WILL = re.compile(r"\byou(?: ll| will)$")  # "As a Payment Integrity Analyst, you will:" introduces duties
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_YEARS = re.compile(r"(?:(\d+(?:\.\d+)?)|\b(one|two|three|four|five|six|seven|eight|nine|ten)\b)\s*(?:\(\d+\)\s*)?\+?\s*"
                    r"(?:(?:-|–|to)\s*(\d+)\s*)?\+?\s*(?:years?|yrs?)\b", re.I)
# Months count only when the phrase is an experience amount ("18 months of claims experience", "6+ months experience"),
# never a deadline ("within 6 months of hire") or a probation period (2026-10-15).
_MONTHS = re.compile(r"(?<![\w.])(\d+)\s*\+?\s*months?(?:'|’)?\s+(?:of\s+)?[a-z/&\- ]{0,40}?\bexperience\b", re.I)
_NOT_EXPERIENCE = re.compile(r"\b(within|after|first|probation\w*|next|every|for the)\s*$", re.I)
_DEGREES = [("doctorate", r"\b(ph\.?d|doctorate|doctoral)\b"), ("master", r"\b(master'?s|mba|m\.s\.|ms degree)\b"),
            ("bachelor", r"\b(bachelor'?s|ba/bs|bs/ba|b\.s\.|b\.a\.|four[- ]year degree|undergraduate degree|college degree)\b"),
            ("associate", r"\bassociate'?s degree\b"), ("high_school", r"\b(high school|ged)\b")]
_EQUIVALENCY = re.compile(r"or (an )?equivalent|equivalent (combination|experience|work experience)|or related experience|in lieu of", re.I)
_PREFERRED_CUES = re.compile(r"\b(preferred|nice to have|a plus|is a plus|bonus|ideally|desired|desirable|advantageous)\b", re.I)
_REQUIRED_CUES = re.compile(r"\b(required|must|minimum|mandatory|at least)\b", re.I)
_BULLET = re.compile(r"^\s*(?:-|•|\*|·|▪|\d+[.)])\s+")


def _heading(line: str) -> str | None:
    text = norm(line.rstrip().rstrip(":"))
    if not text or len(text) > 70 or _BULLET.match(line):
        return None
    explicit = line.rstrip().endswith(":")
    words = len(text.split())
    if _FIT_IF.match(text):
        return "required"
    if not explicit and words > 6:
        return None
    if explicit and _YOU_WILL.search(text):
        return "responsibilities"
    for section, phrases in _HEADINGS:
        for phrase in phrases:
            p = norm(phrase)
            if text == p or (text.startswith(p) and len(text) <= len(p) + 12):
                return section
            # "Perks & Benefits:", "Experience & Skills:"; short non-colon lines only for non-requirement sections
            if (explicit and words <= 8 or section == "other" and words <= 4) and contains_term(text, p):
                return section
    if _ABOUT.match(text) and words <= 5:
        return "other"
    return None


# An age, never years of experience (D149, the coordinator's age probe): a figure followed by "old", "of age", "or older"
# or "or over" ("21 years old", "18 years of age or older", "18+ yrs old"), or after "age"/"aged" ("aged 21 years",
# "age: 18 years or more")
_AGE_AFTER = re.compile(r"[\s\-]*\+?[\s\-]*(?:old|of\s+age|or\s+older|or\s+over)\b")
_AGE_BEFORE = re.compile(r"(?<![a-z])aged?\b[\s:]*(?:(?:of|at least|over|minimum|min\.?)\s+)*$")


def year_spans(line: str) -> list[tuple[int, int, float, float | None]]:
    """Every years amount on a line as (start, end, years, upper bound or None), in reading order. "3-5 years" is
    (3, 5): the lower bound is the minimum asked for, the upper is kept for the explanation. Months become years
    (18 months = 1.5) only in an experience phrase (2026-10-15)."""
    spans = []
    lowered = line.lower()
    for match in _YEARS.finditer(line):
        if _AGE_AFTER.match(lowered[match.end():]) or _AGE_BEFORE.search(lowered[:match.start()]):
            continue   # an age ("21 years old", "18 years of age or older", "aged 21 years"), never experience (D149)
        value = float(match.group(1)) if match.group(1) else float(_WORD_NUMBERS[match.group(2).lower()])
        upper = float(match.group(3)) if match.group(3) else None
        if 0 < value <= 30:
            spans.append((match.start(), match.end(), value, upper if upper and upper > value else None))
    for match in _MONTHS.finditer(line):
        months = int(match.group(1))
        if 0 < months <= 360 and not _NOT_EXPERIENCE.search(line[:match.start()]):
            spans.append((match.start(), match.end(), round(months / 12, 4), None))
    return sorted(spans)


def _years(line: str) -> float | None:
    values = [span[2] for span in year_spans(line)]
    return min(values) if values else None


def _term_span(lowered: str, words: list[str]) -> tuple[int, int] | None:
    """Where a vocabulary term first appears in the raw line (spaces in the term also match '-' or '/')."""
    found = []
    for word in words:
        pattern = r"[\s\-/]+".join(re.escape(part) for part in norm(word).split())
        match = re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", lowered) if pattern else None
        if match:
            found.append((match.start(), match.end()))
    return min(found) if found else None


def _nearest_years(spans: list, where: tuple[int, int] | None, default: float | None) -> float | None:
    """With two or more different amounts on one line ("2+ years of SQL and 5+ years of claims"), each area takes the
    amount nearest to it, a preceding one on a tie (2026-10-15: the lowest used to apply to every area)."""
    if where is None or len({s[2] for s in spans}) < 2:
        return default

    def distance(span):
        before = span[1] <= where[0]
        gap = where[0] - span[1] if before else max(0, span[0] - where[1])
        return gap, 0 if before else 1
    return min(spans, key=distance)[2]


def _requirement_type(section: str, line: str) -> str:
    if section == "responsibilities":
        return "responsibility"
    if section == "preferred":
        return "ambiguous" if _REQUIRED_CUES.search(line) and not _PREFERRED_CUES.search(line) else "preferred"
    if _PREFERRED_CUES.search(line):
        return "preferred"
    if _EQUIVALENCY.search(line):
        return "required_with_equivalency"
    return "hard_required"


def _confidence(section: str, structured: bool, line: str) -> float:
    base = {"required": 0.9, "preferred": 0.9, "responsibilities": 0.85, "ambiguous": 0.7}.get(section, 0.55)
    if not structured:
        base = 0.55
    if _REQUIRED_CUES.search(line) or _PREFERRED_CUES.search(line) or _EQUIVALENCY.search(line):
        base += 0.05
    return round(min(base, 0.95), 2)


# --- certifications (2026-10-15; careeros/qualify/certifications.py) -----------------------------------------------------
_CERT_WORD = re.compile(r"\b(certifications?|certified|certificates?|credentials?)\b", re.I)
# Wording that mentions certification without asking the candidate for one
_NOT_A_CREDENTIAL = re.compile(
    r"\b(?:certifications?|credentials?)\s*,?\s*(?:etc\b|and the like)|\bbased on\b[^.;]{0,160}\bcertifications?\b|"
    r"\bfactors\b[^.;]{0,160}\bcertifications?\b|\bis certified as\b|\bcertified (?:cop(?:y|ies)|data|mail|letters?|records?|payroll)\b|"
    r"\bcertification of (?:records?|completion|employment|eligibility|insurance|attendance)\b|\bboard[- ]certified\b|"
    r"\b(?:ssl|tls)\s+certificates?\b|\b(?:login|log-in|system|access|user|account|api)\s+credentials?\b|"
    r"\b(?:more|less) than (?:a |any )?(?:one |single )?(?:credentials?|certifications?)\b|"
    r"\bthan any (?:one |single )?(?:credentials?|certifications?)\b", re.I)
# A certification to earn after hire is training, not a requirement
_ATTACHED = re.compile(r"^[\s\-/]*(?:\([^()]*\)[\s\-/]*)?$")   # "PMP certification", "CPC (AAPC) certification"
_ON_THE_JOB = re.compile(r"\b(?:obtain|complete|completing|earn|achieve|pursue|become)\b[^.;]{0,60}\b(?:certifications?|certified)\b", re.I)
_EXAMPLES = re.compile(r"\b(?:e\.\s?g\.?|such as|for example|for instance)(?![a-z])", re.I)
# Any word that softens a requirement (helpful, help as a verb, optional, not required, preferred and its kin), wherever it
# stands: a sentence holding one is read by the plain-form rule below (D126, CR-2026-11-09-INTAKE-HOTFIX Pass 3)
_CERT_SOFT = re.compile(r"\b(?:helpful|help|helps|helping|helped|optional|not (?:a )?(?:required|requirement|mandatory|necessary|needed)|"
                        r"isn'?t (?:required|necessary|needed|mandatory)|no longer (?:required|mandatory|necessary|needed)|preferred|preferably|preference|nice to have|a plus|bonus|ideally|"
                        r"desired|desirable|advantageous)\b", re.I)
# The plain form: once the credentials, their certification words, list words and parentheses holding only an alias or
# issuer are taken out, what is left of the sentence is only a softening phrase ("is also very helpful", "not required")
# or a label ("Preferred:", "A plus:"). Only then is a credential preferred (D126, C1)
# The words allowed before the softening phrase: auxiliaries and a closed list of affirming adverbs, never any "-ly"
# word ("hardly optional", "rarely optional" mean required: not plain; the delta check of c90ee6c, D126)
_PLAIN = re.compile(r"^(?:(?:an?|the)\s+)?(?:(?:is|are|would|will|can|could|should|may|might|be|been|has|have|deemed|found|proven|"
                    r"very|highly|strongly|extremely|especially|particularly|really|definitely|certainly|generally|usually|typically|"
                    r"also|always|often|considered|seen as|viewed as|regarded as)\s+)*"
                    r"(?:helpful|preferred|a plus|desired|nice to have|optional|not required|not mandatory|not necessary|"
                    r"(?:is|are)n'?t required)$|^(?:preferred|a plus|nice to have|desired|optional|helpful)\s*:$", re.I)
_LIST_WORDS = re.compile(r"\b(?:and/or|and|or|as well as|along with)\b|[,/&]|\b(?:e\.\s?g\.?|i\.\s?e\.?|such as)(?![a-z])", re.I)
_CERT_ALTERNATIVE = re.compile(r"\bor (?:similar|another|other relevant|equivalent)\b|\bor formal training\b|"
                               r"\b(?:degree|experience|training|methodology|knowledge)\b[^.;]{0,80}\bor\b[^.;]{0,30}"
                               r"\b(?:certifications?|credentials?|certified)\b|\bor\b[^.;]{0,30}\b(?:experience|degree|training)\b|"
                               r"\b(?:experience|training|degree)\b[^.;]{0,60}\band/or\b|\band/or\b[^.;]{0,60}\b(?:experience|training)\b", re.I)
_CLAUSE_END = re.compile(r"[.;]\s+")
_LIST_GAP = re.compile(r"^[\s,/]*(?:and/or|or)?[\s,/]*$", re.I)
_NOT_A_FAMILY_MENTION = re.compile(r"\s*(?:experience|methodolog|framework|skills?|knowledge|process|principles|practices)", re.I)
_NOT_AN_END = ("e.g", "i.e", "etc", "vs", "approx", "inc", "no")


def clauses(line: str) -> list[str]:
    """A line's sentences ("PMP preferred. Scrum certification desired."), never split after e.g. or i.e."""
    parts, start = [], 0
    for match in _CLAUSE_END.finditer(line):
        if line[start:match.start()].lower().endswith(_NOT_AN_END):
            continue
        parts.append(line[start:match.start() + 1].strip())
        start = match.end()
    parts.append(line[start:].strip())
    return [p for p in parts if p]


def _term_spans(text: str, term: str, case_sensitive: bool = False) -> list[tuple[int, int]]:
    words = (term if case_sensitive else norm(term)).split()
    pattern = r"[\s\-/]+".join(re.escape(w) for w in words)
    flags = 0 if case_sensitive else re.I
    return [m.span() for m in re.finditer(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9+])", text, flags)] if pattern else []


def _sentence_type(text: str, section: str | None) -> str:
    """The section's reading of a sentence naming a certification, in the existing vocabulary."""
    ctype = _requirement_type(section or "ambiguous", text)
    if ctype in ("hard_required", "ambiguous") and _CERT_ALTERNATIVE.search(text):
        return "required_with_equivalency"
    return ctype


def _plain(clause: str, names: list) -> bool:
    """Whether a sentence is the plain form ("PMP, CPC certification is helpful.", "Preferred: PMP certification."):
    with the named certifications (`names`, spans), every certification word, the list words and the parentheses that
    held only those taken out, only a softening phrase or label is left (D126, C1)."""
    text = list(clause)
    bullet = _BULLET.match(clause)   # "- PMP certification a plus."
    for s, e in names + [m.span() for m in _CERT_WORD.finditer(clause)] + ([bullet.span()] if bullet else []):
        text[s:e] = " " * (e - s)
    # a comma after a certification word ends an item, never a list: "PMP certification, CPC optional" is not plain
    word = _CERT_WORD.search(clause)
    rest = _LIST_WORDS.sub(lambda m: m.group() if m.group() == "," and word and m.start() > word.start() else " ", "".join(text))
    rest = re.sub(r"\([\s,/&]*\)", " ", rest)
    rest = re.sub(r"\s+", " ", re.sub(r"[\"'\u2018\u2019\u201c\u201d\u00ae\u2122]", "", rest)).strip().rstrip(".!").strip()
    return bool(_PLAIN.match(rest))


def _cert_type(clause: str, rtype: str, section: str | None, plain: bool) -> str:
    """The requirement type of every certification a sentence names (CR-2026-11-09-INTAKE-HOTFIX Pass 3, D127 B1):
    - no softening word in the sentence: the section's reading, as before (hard under Required);
    - the plain form: preferred;
    - under a Preferred heading: the heading's reading, as before;
    - otherwise "ambiguous" (the gate flags it CONDITIONAL): never preferred, and never a rejection on a demand read
      beside a softening word ("PMP certification is required, optional weekend coverage is helpful" is unclear)."""
    if rtype == "responsibility" or _ON_THE_JOB.search(clause):
        return "responsibility"
    if not _CERT_SOFT.search(clause):
        return _sentence_type(clause, section)
    if plain:
        return "preferred"
    if section == "preferred":
        return _sentence_type(clause, section)
    return "ambiguous"


def _family_terms(clause: str, families: dict, taken: list, upto: int | None = None) -> list[str]:
    """The certification families a sentence names (outside the credentials already found), in reading order."""
    hits = []
    for fam, item in families.items():
        for term in item.get("terms") or []:
            spans = [sp for sp in _term_spans(clause, term, bool(item.get("case_sensitive")))
                     if not any(sp[0] < e and sp[1] > s for s, e in taken) and (upto is None or sp[0] < upto)]
            if spans:
                hits.append((spans[0][0], fam))
                break
    return [fam for _, fam in sorted(hits)]

def certification_mentions(line: str, rtype: str, section: str | None, certs: dict | None) -> list[tuple[str, str]]:
    """(stored name, requirement type) for every certification a line names: a catalog credential ('pmp'), a family
    ('family:scrum') or an issuer ('issuer:aapc') when it names no credential, or 'unspecified certification'.
    Credentials given only as examples ("project management certification (e.g., PRINCE2, PMP)") make one family
    requirement, never hard requirements of their own."""
    certs = certs or {}
    credentials, families, issuers = certs.get("credentials") or {}, certs.get("families") or {}, certs.get("issuers") or {}
    context_only = {norm(a) for a in certs.get("context_only") or []}
    out: list[tuple[str, str]] = []
    for clause in clauses(line):
        excluded = [m.span() for m in _NOT_A_CREDENTIAL.finditer(clause)]
        has_word = any(not any(a <= m.start() < b for a, b in excluded) for m in _CERT_WORD.finditer(clause))
        found = []
        for key, item in credentials.items():
            for alias in item.get("aliases") or []:
                if norm(alias) in context_only and not has_word:
                    continue
                found += [(start, end, key) for start, end in _term_spans(clause, alias)]
        chosen: list[tuple[int, int, str]] = []
        for start, end, key in sorted(found, key=lambda f: (-(f[1] - f[0]), f[0])):   # longest alias first, no overlaps
            if not any(start < e and end > s for s, e, _ in chosen) and key not in {k for _, _, k in chosen}:
                chosen.append((start, end, key))
        chosen.sort()
        if not chosen and not has_word:   # a softening sentence naming none is extract()'s whole-section rule (D138 R2)
            continue
        example = _EXAMPLES.search(clause)
        named = [key for start, _, key in chosen if not (example and example.start() < start)]
        given = [key for start, _, key in chosen if example and example.start() < start]
        taken = [(s, e) for s, e, _ in chosen]

        if named:   # "PMP, Scrum, or SAFe preferred": a family listed with a named credential is a certification too
            listed = sorted((sp, fam) for fam, item in families.items() for term in item.get("terms") or []
                            for sp in _term_spans(clause, term, bool(item.get("case_sensitive")))
                            if not any(sp[0] < e and sp[1] > s for s, e in taken))
            joined = list(taken)
            for (start, end), fam in listed:
                gaps = [clause[e:start] if e <= start else clause[end:s] for s, e in joined]
                if any(_LIST_GAP.match(gap) for gap in gaps) and not _NOT_A_FAMILY_MENTION.match(clause[end:]) \
                        and f"family:{fam}" not in named:
                    named.append(f"family:{fam}")
                    joined.append((start, end))
        if given:
            stated = _family_terms(clause, families, taken, example.start() if example else None)
            common = {(credentials.get(k) or {}).get("family") for k in given}
            family = stated[0] if stated else (common.pop() if len(common) == 1 else None)
            named.append(f"family:{family}" if family else "unspecified certification")
        if not named and has_word:
            named = [f"issuer:{i.lower()}" for i in issuers if _term_spans(clause, i, True)]
            named = named or [f"family:{fam}" for fam in _family_terms(clause, families, taken)] or ["unspecified certification"]
        # Each certification named in the sentence takes the plain-form reading (D126, C1): the sentence is plain only
        # when, with the credentials, the families named as certifications and an issuer standing alone in brackets
        # taken out, a softening phrase is all that is left; any other softening word makes it "ambiguous"
        spans = {key: (start, end) for start, end, key in chosen}
        family_spans = [sp for item in families.values() for term in item.get("terms") or []
                        for sp in _term_spans(clause, term, bool(item.get("case_sensitive")))
                        if not any(sp[0] < e and sp[1] > s for s, e, _ in chosen)]
        # an issuer naming a credential: alone in brackets ("CPC (AAPC) certification") or right beside it
        # ("Microsoft PL-300"); "AAPC preferred" standing alone is another item
        issuer_names = set(issuers) | {str((credentials.get(k) or {}).get("issuer") or "") for _, _, k in chosen} - {""}
        issuer_spans = [sp for i in issuer_names for sp in _term_spans(clause, i, True)
                        if (re.match(r"\s*\)", clause[sp[1]:]) and re.search(r"\(\s*$", clause[:sp[0]]))
                        or any(not clause[sp[1]:s].strip() or not clause[e:sp[0]].strip() for s, e in spans.values())]
        plain = _plain(clause, list(spans.values()) + family_spans + issuer_spans)
        reading = _cert_type(clause, rtype, section, plain)
        own = {name: reading for name in named}
        if _CERT_SOFT.search(clause) and not plain:
            # A certification word not attached to a named credential ("Scrum certification required, CPC helpful")
            # is a certification of its own: kept, unclear like the rest, never lost beside an unclear credential
            for word in _CERT_WORD.finditer(clause):
                if any(a <= word.start() < b for a, b in excluded) or any(
                        e <= word.start() and _ATTACHED.match(clause[e:word.start()]) for _, e, _ in chosen):
                    continue
                lead = clause[:word.start()]
                lead = lead[max(lead.rfind(","), lead.rfind(";")) + 1:]
                fams = _family_terms(lead, families, [])
                found_issuers = [f"issuer:{i.lower()}" for i in issuers if _term_spans(lead, i, True)]
                demanded = f"family:{fams[-1]}" if fams else (found_issuers[-1] if found_issuers else "unspecified certification")
                if demanded not in named:
                    named.append(demanded)
                    own[demanded] = reading
        for name in named:
            if (name, own[name]) not in out:
                out.append((name, own[name]))
    return out


def _area(phrase: str) -> str:
    """'claims experience required' -> 'claims'; 'experience as a business systems analyst' -> 'business systems analyst'."""
    area = re.sub(r"^\s*experience\s+(?:in|with|as|working(?:\s+(?:in|with|on|as))?)\s+(?:an?\s+|the\s+)?", "", phrase)
    area = re.split(r"\b(?:experience|is required|required|is preferred|preferred|is a plus|a plus|desired|strongly)\b", area)[0]
    area = area.strip(" ,.;:-")
    return area or "professional experience"


# 120 characters: an OR-list of roles ("as a Junior Analyst, QA Analyst, Data Analyst, or Operations Associate") must
# reach its last alternative (it was cut at 60 until 2026-10-12)
_AREA = re.compile(r"(?:experience (?:in|with|as|working)|months? (?:of|in)|years (?:of|in))\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})")


_AGE = re.compile(r"[\s\-]*(?:old\b|of age\b|or older\b|or over\b)")   # "18 years old", "21 years of age"
# The words by which a figure's own phrase introduces what it counts: "10+ years with Excel", "9+ years of hands-on
# experience with Excel", "3+ years using SQL" (Codex Pass 1 F3, 2026-11-05)
_OWN_LEAD = re.compile(r"\W*(?:of\s+)?(?:(?:hands[- ]on|professional|direct|relevant|practical|proven|working)\s+)*"
                       r"(?:experience\s+)?(?:working\s+)?(?:with|in|using|on|of)\s+(?:an?\s+|the\s+)?")


def _skill_owns(own: str, aliases: list) -> bool:
    """Whether a figure's own phrase introduces one of these skills: the skill's word comes first after the lead words,
    or after up to two words that describe it ("advanced Excel", "expert-level Excel"; the fix-check of dba7b35), so the
    figure is the skill's. "as a BSA with SQL" introduces a role, and SQL does not own its figure."""
    lead = _OWN_LEAD.match(own)
    rest = norm(own[lead.end():]) if lead else ""
    return any(re.match(rf"(?:[a-z\-]+ ){{0,2}}{re.escape(norm(word))}(?![a-z0-9])", rest)
               for words in aliases for word in words if norm(word))


def _span_area(lowered: str, spans: list, index: int) -> str:
    """The area one years figure on a line asks for: the text after it, up to the next figure."""
    end = spans[index + 1][0] if index + 1 < len(spans) else len(lowered)
    phrase = _AREA.search(lowered[spans[index][0]:end])
    return (re.sub(r"(?:[\s,;:.\-]+(?:and|or|plus|as well as|with|including|at least|of|in))*[\s,;:.\-]*$", "", _area(phrase.group(1)))
            if phrase else "professional experience")


def _capitalized(text: str, words: list) -> bool:
    """Whether a skill's word is written with a capital in `text` ("Facets", "FACETS"): the name of the product."""
    return any(found.group(0) != found.group(0).lower() for word in words if norm(word)
               for found in re.finditer(rf"(?<![a-z0-9]){re.escape(norm(word))}(?![a-z0-9])", text, re.I))


def _without(line: str, normed: str, phrases: list | None, words: list) -> str:
    """The normalized line with each excluded phrase blanked out (policy `skill_alias_exclusions`, 2026-11-03), only where
    the skill's word in it is lowercase (pre-review #10, 2026-11-04): "several facets of the role" is ordinary English,
    while "all Facets modules", "Facets of TriZetto" and "FACETS" name the system."""
    if not phrases:
        return normed
    for phrase in phrases:
        pattern = r"[^a-z0-9+#/]+".join(re.escape(part) for part in norm(phrase).split())
        if pattern:
            line = re.sub(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])",
                          lambda found: found.group(0) if _capitalized(found.group(0), words) else " ", line, flags=re.I)
    return norm(line)


def _line_records(line: str, rtype: str, confidence: float, aliases: dict, domain_terms: dict, section: str | None = None,
                  certs: dict | None = None, exclusions: dict | None = None) -> list[Requirement]:
    lowered, normed = line.lower(), norm(line)
    records: list[Requirement] = []
    spans = year_spans(line)
    years = min((span[2] for span in spans), default=None)

    def add(category, name, yrs=None):
        records.append(Requirement(category, name, rtype, line, confidence, yrs))

    for level, pattern in _DEGREES:
        if re.search(pattern, lowered):
            add("education", level)
            break
    for name, ctype in certification_mentions(line, rtype, section, certs):  # 2026-10-15: one record per certification
        records.append(Requirement("certification", name, ctype, line, confidence))
    if re.search(r"\b(licen[cs]e|licensed|licensure)\b", lowered):
        add("license", line.strip("-• ")[:80].lower())
    if re.search(r"\b(clearance|public trust|ts/sci|top secret)\b", lowered):
        add("clearance", "security clearance")
    if re.search(r"(authorized to work|work authorization|legally authorized|without (visa )?sponsorship|"
                 r"sponsorship|u\.?s\.? citizen|citizenship|green card)", lowered):
        citizen_only = re.search(r"(must be a u\.?s\.? citizen|u\.?s\.? citizenship (is )?required|requires u\.?s\.? citizenship)", lowered) \
            and not re.search(r"(permanent resident|green card|or authorized)", lowered)
        if citizen_only:
            add("work_authorization", "us_citizenship")
        elif "sponsor" in lowered:
            add("work_authorization", "no_sponsorship")
        else:
            add("work_authorization", "us_work_authorization")
    if re.search(r"(must (reside|live|be located)|reside within|commut|located within|local to)", lowered):
        add("location", line.strip("-• ")[:80].lower())
    if re.search(r"\btravel\b", lowered) and re.search(r"\d+\s?%|overnight|frequent", lowered):
        add("travel", line.strip("-• ")[:80].lower())
    if re.search(r"\b(night shift|overnight shift|weekends|on[- ]call|rotating shift|evening shift)\b", lowered):
        add("schedule", line.strip("-• ")[:80].lower())

    exclusions = exclusions or {}   # "several facets of the role": ordinary English, not the Facets system
    skills = [key for key, words in aliases.items() if any(contains_term(_without(line, normed, exclusions.get(key), words), w) for w in words)]
    for skill in skills:
        add("skill", skill, _nearest_years(spans, _term_span(lowered, aliases[skill]), years))
    for domain, words in domain_terms.items():
        if any(contains_term(normed, w) for w in words):
            add("domain", domain, _nearest_years(spans, _term_span(lowered, words), years))
    several = len({span[2] for span in spans}) > 1
    if years is not None and not skills:
        if several:  # "2+ years of claims and 5+ years of management": one record each
            for index, span in enumerate(spans):
                add("years_experience", _span_area(lowered, spans, index), span[2])
        else:
            phrase = _AREA.search(lowered)
            add("years_experience", _area(phrase.group(1)) if phrase else "professional experience", years)
    elif years is not None and several:
        # A figure that no record on the line carries is judged too (Fable review #4, 2026-11-04, D113): in "5+ years as a
        # Business Analyst, including 2+ years with SQL" the skill takes the 2, and the role's 5 used to be dropped. Not
        # a figure whose own phrase introduces a skill on the line ("10+ years with Excel and 2+ years with SQL": the 10
        # is Excel's, though the nearest rule gave Excel the 2) nor an age ("at least 18 years old") (D115). A skill
        # named later in the phrase ("5+ years as a BSA with SQL") does not own the figure: it is kept (Codex Pass 1 F3)
        carried = {r.years for r in records}
        for index, span in enumerate(spans):
            own = lowered[span[1]:spans[index + 1][0] if index + 1 < len(spans) else len(lowered)]
            if span[2] in carried or _AGE.match(lowered[span[1]:]) or _skill_owns(own, [aliases[skill] for skill in skills]):
                continue
            add("years_experience", _span_area(lowered, spans, index), span[2])
    elif rtype != "responsibility" and not records and re.search(r"\bexperience\b", lowered):
        phrase = re.search(r"experience (?:in|with|as|working)\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})", lowered)
        if phrase and _area(phrase.group(1)) != "professional experience":
            add("experience", _area(phrase.group(1)))
    return records


# The sections whose softening lines count for the whole-section rule (None: a posting with no headings at all)
_REQUIREMENT_SECTIONS = (None, "required", "preferred", "ambiguous")


def requirement_lines(description_text: str) -> list[tuple[str, str | None]]:
    """(line, its section) for every line of the posting's requirements sections, their headings included, as extract()
    walks them: Required, Preferred and the unclear ones ("Qualifications"), or, in a posting with no requirements
    heading, every line outside a Responsibilities or company section (D149). The gate reads the posting's own text here,
    never an AI record or quote."""
    lines = [l for l in (description_text or "").split("\n") if l.strip()]
    structured = any(_heading(l) in ("required", "preferred", "ambiguous") for l in lines)
    section: str | None = None
    found: list[tuple[str, str | None]] = []
    for line in lines:
        heading = _heading(line)
        if heading:
            section = heading
            if heading in _REQUIREMENT_SECTIONS:
                found.append((line, heading))
            continue
        if section in _REQUIREMENT_SECTIONS and not (section is None and structured):
            found.append((line, section))
    return found


def demanded_line(line: str, section: str | None) -> bool:
    """Whether a requirements line's demands may reject (not preferred, not a duty), as extract() types it (D149)."""
    return _requirement_type(section or "ambiguous", line) not in ("preferred", "responsibility")


# D149 R-Y2 by the word: "year", "yr" or "decade" in any form, once ages ("18 years old", "21 years of age", "years or
# older"), degree lengths ("4-year degree", "two-year degree", "four year college degree"), pay and other rates ("$90,000
# per year", "a year", "/yr", "annually") and calendar years ("this year", "fiscal year", "year-end") are set aside
# Red team Y4 (D151): months ("60+ months", "mos", "mo.") and "years of experience" abbreviated ("YOE", "y.o.e.") are
# tenure words too; a month as a rate or a deadline ("$7,000 per month", "within 6 months of hire") is set aside
_YEARS_WORD = re.compile(r"(?<![a-z])(?:years?|yrs?|decades?|months?|mos|mo\.|yoe|y\.o\.e\.?)(?![a-z])", re.I)
_NOT_A_TENURE_MONTH = re.compile(r"(?:(?<![a-z])(?:per|an?|each|every|this|next|last)\s+|/\s*)(?:month|mo)(?![a-z])|"
                                 # D155 (4): "for N months" is tenure ("worked for 60 months as a BSA"); only "for the
                                 # first N months" (of a probation or a benefit) is not
                                 r"(?<![a-z])(?:within|after|in|for\s+the\s+first|first|next|every|probation\w*)\s+(?:the\s+)?(?:first\s+)?"
                                 r"(?:\d+|one|two|three|six|twelve)\s*(?:months?|mos?\.?)(?![a-z])", re.I)
_NOT_A_TENURE = re.compile(r"(?<![a-z])(?:years?|yrs?)[\s\-]*(?:old|of\s+age|or\s+older|or\s+over)(?![a-z])|"
                           r"(?<![a-z])aged?\b[\s:]*(?:(?:of|at least|over|minimum|min\.?)\s+)*\w+\+?\s*(?:years?|yrs?)|"
                           r"(?<![a-z0-9])(?:\d+|one|two|three|four|five|six)[\s\-]*(?:year|yr)s?[\s\-]+"
                           r"(?:(?:college|university|undergraduate|bachelor'?s|associate'?s|technical)\s+)*"
                           r"(?:degree|diploma|program|programme)|"
                           r"(?:(?<![a-z])(?:per|an?|each|every|this|next|last|fiscal|calendar|school|plan|academic)\s+|/\s*)"
                           r"(?:year|yr)(?![a-z])(?!\s+of\b)|(?<![a-z])(?:year|yr)[\s\-]+(?:end|round)(?![a-z])", re.I)


def mentions_years(line: str) -> bool:
    """Whether a line speaks of years of anything ("5 or more years", "Eleven years", "a decade of underwriting"), ages
    and degree lengths left out (D149 R-Y2)."""
    return bool(_YEARS_WORD.search(_NOT_A_TENURE_MONTH.sub(" ", _NOT_A_TENURE.sub(" ", line or ""))))


_SUBJECT_LEAD = re.compile(r"^\W*(?:of\s+|in\s+|with\s+|using\s+|on\s+)?(?:(?:hands[- ]on|professional|direct|relevant|"
                           r"practical|proven|working|advanced|expert(?:[- ]level)?|strong|solid|deep|extensive|"
                           r"intermediate|demonstrated)\s+)*(?:experience\s+)?(?:working\s+)?"
                           r"(?:(?:with|in|using|on|of)\s+)?(?:an?\s+|the\s+)?", re.I)


def figure_subjects(line: str, aliases: dict, domain_terms: dict) -> list[tuple[float, float | None, tuple]]:
    """D155 (3): every years figure on a line, in order, with the subject its own phrase opens with: ("skill", name) or
    ("domain", name) when the words right after the figure (past "of", "with", "hands-on experience in" ...) are a
    skill's or a domain's word, else ("other",): a role, a function or anything else ("2+ years in underwriting with
    SQL" opens with underwriting, not SQL). Ages are left out (`year_spans`)."""
    terms = sorted([(norm(word), ("skill", name)) for name, words in aliases.items() for word in [name, *words]
                    if norm(word)] + [(norm(word), ("domain", name)) for name, words in domain_terms.items()
                                      for word in [name, *words] if norm(word)], key=lambda t: len(t[0]), reverse=True)
    spans = year_spans(line or "")
    found = []
    for index, (_start, end, value, upper) in enumerate(spans):
        own = (line or "")[end:spans[index + 1][0] if index + 1 < len(spans) else len(line or "")]
        rest = norm(_SUBJECT_LEAD.sub("", own, count=1))
        subject = next((kind for term, kind in terms if rest == term or rest.startswith(term + " ")), ("other",))
        found.append((value, upper, subject))
    return found


def demand_figures(line: str) -> list[tuple[float, float | None]]:
    """Every years figure on a line that asks for experience: (years, upper bound or None), ages ("at least 18 years
    old") left out (D149 R-Y2)."""
    lowered = (line or "").lower()
    return [(value, upper) for _start, end, value, upper in year_spans(line or "") if not _AGE.match(lowered[end:])]


def extract(description_text: str, policy) -> Extraction:
    aliases = policy.get("skill_aliases", default={}) or {}
    domain_terms = policy.get("domain_terms", default={}) or {}
    certs = policy.get("certifications", default={}) or {}
    exclusions = policy.get("skill_alias_exclusions", default={}) or {}
    lines = [l for l in (description_text or "").split("\n") if l.strip()]
    section, sections = None, []
    for line in lines:
        found = _heading(line)
        if found:
            sections.append(found)
    structured = any(s in ("required", "preferred", "ambiguous") for s in sections)

    records: list[Requirement] = []
    notes: list[str] = []
    part = 0                      # which section a line is in: a new one at every heading (D131 S3b)
    placed: list[int] = []        # the section of each record
    soft_parts: set[int] = set()  # sections holding a softening line that names no other credential
    soft_names: set[str] = set()  # credentials named on a softening line of a requirements section
    for line in lines:
        found = _heading(line)
        if found:
            section = found
            part += 1
            continue
        if section == "other":
            continue
        if section is None and structured:
            continue  # intro text before the first heading
        rtype = _requirement_type(section or "ambiguous", line)
        if section in _REQUIREMENT_SECTIONS:
            # The whole-section rule, sentence by sentence (D138 R2): a softening sentence that names a credential of its
            # own ("CBAP certification is preferred.") softens that credential only; one that names none ("It is
            # generally optional for analysts.") may soften any credential of the section. Checked before a line of a
            # posting with no headings is skipped below, so its softener always counts
            for sentence in clauses(line):
                if _CERT_SOFT.search(sentence):
                    named = {n for n, _ in certification_mentions(sentence, rtype, section, certs) if n != "unspecified certification"}
                    if named:
                        soft_names |= named
                    else:
                        soft_parts.add(part)
        if section is None and not (_YEARS.search(line) or _REQUIRED_CUES.search(line) or _PREFERRED_CUES.search(line)
                                    or re.search(r"\b(degree|experience|certif)\w*", line, re.I)):
            continue
        conf = _confidence(section or "none", structured, line)
        line_records = _line_records(line, rtype, conf, aliases, domain_terms, section, certs, exclusions)
        records.extend(line_records)
        placed.extend([part] * len(line_records))
    # The whole-section rule (CR-2026-11-09-INTAKE-HOTFIX Pass 4, CR-HF-08, D131 S3b): a softening line anywhere in a
    # requirements section (before or after, blank lines between) makes every certification the section demands
    # "ambiguous" (CONDITIONAL); a credential demanded in one place and softened in another is ambiguous too. A heading
    # starts a new section; a softener in a section that is no requirements section (Benefits, About) never counts
    # A line with one certification made unclear has all its required certifications made unclear: an alternative
    # ("PMP or CBAP ... required" beside "CBAP certification preferred") never leaves the other one standing alone
    demanded = ("hard_required", "required_with_equivalency")
    unclear = {r.source_text for r, where in zip(records, placed, strict=True) if r.category == "certification"
               and r.requirement_type in demanded and (where in soft_parts or r.name in soft_names)}
    records = [Requirement(r.category, r.name, "ambiguous", r.source_text, r.confidence, r.years)
               if r.category == "certification" and r.requirement_type in demanded and r.source_text in unclear else r
               for r in records]

    seen, unique = set(), []
    for r in records:
        key = (r.category, r.name, r.requirement_type, r.source_text, r.years)   # two figures on a line are two records
        if key not in seen:
            seen.add(key)
            unique.append(r)
    if not structured:
        notes.append("no_requirement_headings")
    return Extraction(EXTRACTOR_ID, unique, sections, notes)
