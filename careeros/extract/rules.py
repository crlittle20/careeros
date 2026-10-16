"""Rules-based requirement extraction. Every record keeps its verbatim source line and a confidence.

Ambiguous qualifications are treated as required (lower confidence), never silently downgraded to preferred.
"""
from __future__ import annotations

import re

from careeros.normalize.posting import contains_term, norm

from .model import Extraction, Requirement

EXTRACTOR_ID = "rules-v10"  # v10 (2026-10-15): one record per certification, by sentence; v9: a years figure per area

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


def year_spans(line: str) -> list[tuple[int, int, float, float | None]]:
    """Every years amount on a line as (start, end, years, upper bound or None), in reading order. "3-5 years" is
    (3, 5): the lower bound is the minimum asked for, the upper is kept for the explanation. Months become years
    (18 months = 1.5) only in an experience phrase (2026-10-15)."""
    spans = []
    for match in _YEARS.finditer(line):
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
_ON_THE_JOB = re.compile(r"\b(?:obtain|complete|completing|earn|achieve|pursue|become)\b[^.;]{0,60}\b(?:certifications?|certified)\b", re.I)
_EXAMPLES = re.compile(r"\b(?:e\.\s?g\.?|such as|for example|for instance)(?![a-z])", re.I)
_CERT_NEGATED = re.compile(r"\b(?:not required|not a requirement|isn'?t required|is not necessary|optional)\b", re.I)
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


def _clause_type(clause: str, rtype: str, section: str | None) -> str:
    """The requirement type of the sentence naming a certification, in the existing vocabulary."""
    if rtype == "responsibility" or _ON_THE_JOB.search(clause):
        return "responsibility"
    if _CERT_NEGATED.search(clause):
        return "preferred"
    ctype = _requirement_type(section or "ambiguous", clause)
    if ctype in ("hard_required", "ambiguous") and _CERT_ALTERNATIVE.search(clause):
        return "required_with_equivalency"
    return ctype



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
        if not chosen and not has_word:
            continue
        ctype = _clause_type(clause, rtype, section)
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
        for name in named:
            if (name, ctype) not in out:
                out.append((name, ctype))
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


def _line_records(line: str, rtype: str, confidence: float, aliases: dict, domain_terms: dict, section: str | None = None,
                  certs: dict | None = None) -> list[Requirement]:
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

    skills = [key for key, words in aliases.items() if any(contains_term(normed, w) for w in words)]
    for skill in skills:
        add("skill", skill, _nearest_years(spans, _term_span(lowered, aliases[skill]), years))
    for domain, words in domain_terms.items():
        if any(contains_term(normed, w) for w in words):
            add("domain", domain, _nearest_years(spans, _term_span(lowered, words), years))
    if years is not None and not skills:
        if len({span[2] for span in spans}) > 1:  # "2+ years of claims and 5+ years of management": one record each
            for index, span in enumerate(spans):
                end = spans[index + 1][0] if index + 1 < len(spans) else len(lowered)
                phrase = _AREA.search(lowered[span[0]:end])
                area = (re.sub(r"(?:[\s,;:.\-]+(?:and|or|plus|as well as|with|including|at least|of|in))*[\s,;:.\-]*$", "", _area(phrase.group(1)))
                        if phrase else "professional experience")
                add("years_experience", area, span[2])
        else:
            phrase = _AREA.search(lowered)
            add("years_experience", _area(phrase.group(1)) if phrase else "professional experience", years)
    elif rtype != "responsibility" and not records and re.search(r"\bexperience\b", lowered):
        phrase = re.search(r"experience (?:in|with|as|working)\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})", lowered)
        if phrase and _area(phrase.group(1)) != "professional experience":
            add("experience", _area(phrase.group(1)))
    return records


def extract(description_text: str, policy) -> Extraction:
    aliases = policy.get("skill_aliases", default={}) or {}
    domain_terms = policy.get("domain_terms", default={}) or {}
    certs = policy.get("certifications", default={}) or {}
    lines = [l for l in (description_text or "").split("\n") if l.strip()]
    section, sections = None, []
    for line in lines:
        found = _heading(line)
        if found:
            sections.append(found)
    structured = any(s in ("required", "preferred", "ambiguous") for s in sections)

    records: list[Requirement] = []
    notes: list[str] = []
    for line in lines:
        found = _heading(line)
        if found:
            section = found
            continue
        if section == "other":
            continue
        if section is None:
            if structured:
                continue  # intro text before the first heading
            if not (_YEARS.search(line) or _REQUIRED_CUES.search(line) or _PREFERRED_CUES.search(line)
                    or re.search(r"\b(degree|experience|certif)\w*", line, re.I)):
                continue
        rtype = _requirement_type(section or "ambiguous", line)
        conf = _confidence(section or "none", structured, line)
        records.extend(_line_records(line, rtype, conf, aliases, domain_terms, section, certs))

    seen, unique = set(), []
    for r in records:
        key = (r.category, r.name, r.requirement_type, r.source_text)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    if not structured:
        notes.append("no_requirement_headings")
    return Extraction(EXTRACTOR_ID, unique, sections, notes)
