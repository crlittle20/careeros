"""Rules-based requirement extraction. Every record keeps its verbatim source line and a confidence.

Ambiguous qualifications are treated as required (lower confidence), never silently downgraded to preferred.
"""
from __future__ import annotations

import re

from careeros.normalize.posting import contains_term, norm

from .model import Extraction, Requirement

EXTRACTOR_ID = "rules-v8"

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
                    r"(?:(?:-|–|to)\s*\d+\s*)?\+?\s*(?:years?|yrs?)\b", re.I)
_DEGREES = [("doctorate", r"\b(ph\.?d|doctorate|doctoral)\b"), ("master", r"\b(master'?s|mba|m\.s\.|ms degree)\b"),
            ("bachelor", r"\b(bachelor'?s|ba/bs|bs/ba|b\.s\.|b\.a\.|four[- ]year degree|undergraduate degree|college degree)\b"),
            ("associate", r"\bassociate'?s degree\b"), ("high_school", r"\b(high school|ged)\b")]
_CERTS = ["pmp", "cbap", "ccba", "ecba", "six sigma", "lean six sigma", "csm", "certified scrum master", "safe agilist",
          "pl-100", "pl-200", "pl-300", "pl-400", "pl-600", "pl-900", "itil", "security+", "cissp", "pharmacy technician",
          "cpht", "rhia", "rhit", "ccs", "cpc"]
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


def _years(line: str) -> float | None:
    values = []
    for match in _YEARS.finditer(line):
        value = float(match.group(1)) if match.group(1) else float(_WORD_NUMBERS[match.group(2).lower()])
        if 0 < value <= 30:
            values.append(value)
    return min(values) if values else None


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


def _area(phrase: str) -> str:
    """'claims experience required' -> 'claims'; 'experience as a business systems analyst' -> 'business systems analyst'."""
    area = re.sub(r"^\s*experience\s+(?:in|with|as|working(?:\s+(?:in|with|on|as))?)\s+(?:an?\s+|the\s+)?", "", phrase)
    area = re.split(r"\b(?:experience|is required|required|is preferred|preferred|is a plus|a plus|desired|strongly)\b", area)[0]
    area = area.strip(" ,.;:-")
    return area or "professional experience"


def _line_records(line: str, rtype: str, confidence: float, aliases: dict, domain_terms: dict) -> list[Requirement]:
    lowered, normed = line.lower(), norm(line)
    records: list[Requirement] = []
    years = _years(line)

    def add(category, name, yrs=None):
        records.append(Requirement(category, name, rtype, line, confidence, yrs))

    for level, pattern in _DEGREES:
        if re.search(pattern, lowered):
            add("education", level)
            break
    if re.search(r"\b(certification|certified|certificate)\b", lowered) or any(contains_term(normed, c) for c in _CERTS):
        named = [c for c in _CERTS if contains_term(normed, c)]
        named = [c for c in named if not any(c != other and contains_term(norm(other), c) for other in named)]  # keep the most specific
        for cert in named or ["unspecified certification"]:
            add("certification", cert)
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
        add("skill", skill, years)
    for domain, words in domain_terms.items():
        if any(contains_term(normed, w) for w in words):
            add("domain", domain, years)
    if years is not None and not skills:
        # 120 characters: an OR-list of roles ("as a Junior Analyst, QA Analyst, Data Analyst, or Operations Associate")
        # must reach its last alternative (it was cut at 60 until 2026-10-12)
        phrase = re.search(r"(?:experience (?:in|with|as|working)|years (?:of|in))\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})", lowered)
        add("years_experience", _area(phrase.group(1)) if phrase else "professional experience", years)
    elif rtype != "responsibility" and not records and re.search(r"\bexperience\b", lowered):
        phrase = re.search(r"experience (?:in|with|as|working)\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})", lowered)
        if phrase and _area(phrase.group(1)) != "professional experience":
            add("experience", _area(phrase.group(1)))
    return records


def extract(description_text: str, policy) -> Extraction:
    aliases = policy.get("skill_aliases", default={}) or {}
    domain_terms = policy.get("domain_terms", default={}) or {}
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
        records.extend(_line_records(line, rtype, conf, aliases, domain_terms))

    seen, unique = set(), []
    for r in records:
        key = (r.category, r.name, r.requirement_type, r.source_text)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    if not structured:
        notes.append("no_requirement_headings")
    return Extraction(EXTRACTOR_ID, unique, sections, notes)
