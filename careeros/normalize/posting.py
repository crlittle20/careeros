"""Turn a raw board posting into normalized job fields."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from careeros.core.fingerprint import fingerprint

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
STATE_CODES = set(US_STATES.values())
NON_US = ["canada", "united kingdom", "uk", "england", "ireland", "india", "germany", "france", "spain", "portugal",
          "netherlands", "poland", "mexico", "brazil", "argentina", "colombia", "philippines", "australia",
          "singapore", "japan", "israel", "romania", "ukraine", "serbia", "costa rica", "toronto", "london",
          "bangalore", "bengaluru", "hyderabad", "dublin", "berlin", "lisbon", "vancouver", "montreal",
          "bangladesh", "dhaka", "pakistan", "vietnam", "egypt", "nigeria", "kenya", "south africa", "chile", "peru",
          "uruguay", "sweden", "denmark", "norway", "finland", "switzerland", "austria", "belgium", "italy", "greece",
          "czech", "hungary", "china", "hong kong", "taiwan", "korea", "new zealand", "krakow", "warsaw", "barcelona",
          "madrid", "amsterdam", "paris", "munich", "zurich", "sydney", "melbourne", "tel aviv", "pune", "chennai",
          "noida", "gurgaon", "manila", "cebu", "bogota", "medellin", "sao paulo", "buenos aires", "emea", "apac", "latam"]
_STATE_LIST_CUES = re.compile(
    r"(reside in|located in|living in|based in|hire in|hiring in|following states|eligible states|states:|"
    r"work from|remote in|available in)", re.I)


@dataclass
class RawPosting:
    ats: str
    board_token: str
    external_id: str
    title: str
    location: str = ""
    url: str | None = None
    apply_url: str | None = None
    posted_at: str | None = None
    description_text: str = ""
    requisition_id: str | None = None
    employment_type_hint: str | None = None
    workplace_hint: str | None = None
    comp: dict | None = None
    raw: dict = field(default_factory=dict)
    department: str | None = None        # the board's department / team, when it gives one (2026-10-12)
    updated_at: str | None = None        # the board's own "updated" time, kept apart from publication (2026-10-12)


def norm(text) -> str:
    text = ("" if text is None else str(text)).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9+#/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_term(haystack_norm: str, term: str) -> bool:
    term_norm = norm(term)
    return bool(term_norm) and re.search(rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])", haystack_norm) is not None


# "Senior Analyst, Analytics - Regulatory Reporting" / "Analyst, Quality Assurance": the role noun first, its subject
# after the comma. Also tried as "Senior Analytics Regulatory Reporting Analyst" (2026-10-12 title audit).
_INVERTED = re.compile(r"^((?:senior|sr|lead|principal|staff|junior|jr)\s+)?(analyst|specialist)\s+(.+)$")


def classify_family(title_norm: str, families: dict) -> str | None:
    candidates = [title_norm]
    inverted = _INVERTED.match(title_norm or "")
    if inverted:
        candidates.append(f"{inverted.group(1) or ''}{inverted.group(3)} {inverted.group(2)}")
    for candidate in candidates:
        for name, spec in families.items():
            if any(contains_term(candidate, t) for t in spec.get("title_any", [])):
                return name
    return None


def excluded_term(title_norm: str, terms: list[str]) -> str | None:
    return next((t for t in terms if contains_term(title_norm, t)), None)


# "Manager" in these titles names an individual-contributor role, not a people-management level.
_IC_MANAGER = re.compile(r"\b(product|project|program|account|customer success|client success|case|content|community|"
                         r"implementation|relationship|success|release|change|vendor|portfolio|campaign)\s+manager")


def seniority(title_norm: str, terms: dict) -> str:
    title = _IC_MANAGER.sub(" ", title_norm)
    for level in ("manager", "principal", "senior", "mid", "entry"):
        if any(contains_term(title, t) for t in terms.get(level, [])):
            return level
    return "unspecified"


def targeted_supervisor(title_norm: str, policy) -> bool:
    if not contains_term(title_norm, "supervisor") or contains_term(title_norm, "manager"):
        return False
    terms = policy.get("families", "service_operations_supervision", "title_any", default=[]) or []
    return any(contains_term(title_norm, term) for term in terms)


def relevant_role(title_norm: str, family: str | None, remote_type: str, country: str, policy) -> bool:
    """The one definition of "a role CareerOS is looking for", shared by board discovery and career fit (2026-10-13; they
    used to differ): one of your job families, no excluded title term, not outside the US, remote or not stated,
    and below manager level or a targeted supervisor eligible for scope review."""
    if not family or excluded_term(title_norm, policy.get("title_exclude", default=[]) or []):
        return False
    if country == "non_US" or remote_type not in ("remote", "unknown"):
        return False
    level = seniority(title_norm, policy.get("seniority_terms", default={}) or {})
    return level != "manager" or targeted_supervisor(title_norm, policy)


def detect_remote(location: str, workplace_hint: str | None, text: str) -> str:
    hint = norm(workplace_hint or "")
    if hint in ("remote",):
        return "remote"
    if hint in ("hybrid",):
        return "hybrid"
    if hint in ("onsite", "on site", "in office", "office"):
        return "onsite"
    loc = norm(location)
    if "hybrid" in loc:
        return "hybrid"
    if "remote" in loc:
        return "remote"
    body = norm(text[:4000])
    if re.search(r"\bhybrid\b", body):
        return "hybrid"
    if re.search(r"\b(fully remote|100 remote|remote position|remote role|work remotely|remote first)\b", body):
        return "remote"
    if re.search(r"\b(on site|onsite|in office|in the office)\b", body):
        return "onsite"
    return "unknown"


def _states_in(fragment: str) -> set[str]:
    found = set()
    lowered = fragment.lower()
    for name, code in US_STATES.items():
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered):
            found.add(code)
    for code in re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", fragment):
        if code in STATE_CODES:
            found.add(code)
    return found


_NEGATION = re.compile(r"(not eligible|not able to hire|unable to hire|cannot hire|can't hire|can not hire|do not hire|"
                       r"don't hire|excluding|except|not available|not open to|are not hiring|cannot employ|ineligible)", re.I)
# the residence reader also reads an employer that can't consider people who live there (it only flags, never stores)
_RESIDENCE_NEGATION = re.compile(_NEGATION.pattern[:-1] + r"|(?:unable to|cannot|can't|can not|will not|won't|do not|"
                                 r"does not|are not able to) (?:consider|accept|employ) "
                                 r"(?:candidates|applicants|anyone|individuals|people|residents|those))", re.I)
# Residence limits (2026-11-04): Oscar writes "This is a remote position, open to candidates who reside in: Texas". A
# list above counted only with two or more states, and "live in", "residing in" and "resident of" were never cues, so
# such roles read as open nationwide. A residence cue limits the role to the list of states named right after it, in
# the same sentence ("Arizona; Florida; or Texas" is one list; "... Texas and work Central Time (CT)" ends at Texas).
# "Based in" and "located in" never count here: they usually describe an office. Not a limit: a preference ("the
# ideal candidate will live in", "... are preferred") or a way around it ("previously lived there, or can demonstrate
# ..."); a requirement to move there ("or be willing to relocate") still is. A negation before the cue or before the
# list ("not open to candidates who reside in", "residing in the U.S., excluding Alaska", "must not reside in") makes
# it excluded states; a negation it can't read ("candidates who do not live in Texas must relocate") is neither.
# The read of every stored posting: vault/reports/PAY_AND_STATE_PARSING_COUNTS_2026-11-04.md (and its review).
# A limit needs a requirement on the candidate (the Fable review of 2026-11-04): "must reside in", "open to candidates
# who live in", "only applicants residing in", "we hire people who live in", "residents of Texas only", "... are
# eligible to apply". A sentence that only says where people live is no limit: a pay-transparency or privacy notice
# ("for candidates residing in Colorado, the range is ..."), the members a plan serves, where the team lives.
# FAIL-SAFE (the fix-check of 2026-11-05): each round of this reading found new sentences it misread (work hours "8-5
# CT", "this notice is for applicants residing in California", a negation in the clause before). A limit read wrongly
# rejects a role without a word to you, so what this reader finds never rejects: the gate flags it
# (residence_limit_check, CONDITIONAL: confirm it before applying) with the sentence quoted. The state lists stored
# with the posting come only from the older rules (the location, and the lists above), as before; of those, only the
# location's can reject (D119, 2026-11-09: the lists above, read from the text, flag at the gate; text_state_lists).
# D123 (2026-11-09): three rounds of telling a requirement from "no limit" (a pay notice, the members a plan served, where
# the team lives) each left new wordings passing silently. The gate no longer reads which kind a sentence is: every state
# list read from the description (the older rule's and this reader's lists, text_state_lists) is silent only when it names
# your state and no exclusion or negation word is in its sentence; any other list flags. residence_limits keeps this reader's
# reading of requirements and exclusions, but it no longer decides anything at the gate.
_RESIDENCE_CUE = re.compile(r"\b(?:resid(?:e|es|ing)|li(?:ve|ves|ving))\s+in\b|\bresidents?\s+of\b", re.I)
_PREFERENCE = re.compile(r"\b(?:ideal(?:ly)?|prefer(?:s|red|ably|ence)?|nice to have|a plus)\b", re.I)
_ALTERNATIVE = re.compile(r"\b(?:previously|or (?:can|could) demonstrate|or (?:have|has) (?:lived|resided))\b", re.I)
_WHO = (r"(?:candidates?|applicants?|individuals|people|persons|those|anyone|employees|team members|associates|staff|"
        r"professionals|talent|residents|hires)")
_FILL = r"(?:\s+(?:who|that|be|are|is|currently|permanently|physically|already|also|legally|a|an|only))*"
# the requirement ends right before the cue: "must currently reside in", "open only to candidates who are residents of"
_REQUIRED_BEFORE = re.compile(
    r"(?:\b(?:must|required to|needs? to|have to|has to|will need to|expected to|should)"
    rf"|\b(?:open|limited|restricted)(?:\s+only)?\s+to(?:\s+{_WHO})?|\b(?:available|offered)\s+only\s+to(?:\s+{_WHO})?"
    rf"|\bonly\s+{_WHO}|\b(?:is|are)\s+only\s+for\s+{_WHO}"   # "this notice is for applicants residing in": no limit
    rf"|\b(?:hire|hiring|consider|considering|accept|accepting|employ|employing|recruit|recruiting)(?:\s+only)?\s+{_WHO})"
    rf"{_FILL}\s*:?\s*$", re.I)
# ... or it follows the list: "Residents of Texas only.", "Candidates living in Texas or Florida are eligible to apply."
_REQUIRED_AFTER = re.compile(r"^[\s,]*(?:only\b|(?:are|is)\s+eligible(?:\s+to\s+apply|\s+for\s+(?:this|the)\s+"
                             r"(?:role|position|job|opportunity))?\s*(?:[.;,!]|$)|(?:may|can)\s+apply\b(?!\s+for\b)|"
                             r"will\s+be\s+considered\b)", re.I)
_MUST_NOT = re.compile(rf"\b(?:(?:must|may|shall|can)\s*not|mustn't|can't){_FILL}\s*$", re.I)
_ANY_NEGATION = re.compile(r"\b(?:not|never)\b|n't\b", re.I)
# D123: a time zone written as a code ("the CT or MT time zone", "work 8-5 CT") reads as the state of that code: a list
# that names another state, so it flags (needless for some, never a wrong pass); the time-zone reading that told them
# apart (_zone_code) is gone with the readings it served
# the dots of U.S., D.C., St. and the like don't end a sentence (replaced, same length, so offsets hold), unless a
# capitalised word follows ("... in the U.S. Applicants in Texas ..." is two sentences)
_ABBREVIATION = re.compile(r"\b(?:[A-Z]\.(?=[A-Z]\.?|,|\s+[a-z(\d])|(?:St|Ft|Mt|Inc|Jr|Sr|No|Dr|Corp|Co)\.)")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")   # a semicolon doesn't: "Arizona; Florida; or Texas" is one list
RESIDENCE_WINDOW = 150   # characters after the cue, never past the end of its sentence
RESIDENCE_LIST_START = 60   # the list starts this close to the cue ("reside in: Dallas, Texas")
_LIST_JOIN = re.compile(r"(?:\s|[,;/&:]|\band\b|\bor\b|\bthe\b|\bstates?\b|\bof\b)*", re.I)
# Codex Pass 1, F7 (2026-11-05): a preference, a negation or a time-zone word belongs to its own clause. A clause ends at a
# semicolon, a dash, or a joined "but" / ", while" ...: "Healthcare experience is preferred; applicants must reside in
# Texas" and "We cannot hire interns, but applicants must reside in Texas" both require Texas
_CLAUSE_BREAK = re.compile(r";|\s[-–—]\s|,?\s+but\b|,\s+(?:and|while|whereas|though|although|however)\b", re.I)
# a sentence that opens with a subordinate clause ("While healthcare experience is preferred, applicants must ...")
# starts its main clause after that clause's comma (the Fable fix-check of the Codex corrections, 2026-11-05)
_SUBORDINATE = re.compile(r"\s*(?:while|although|though|whereas|even though)\b[^,;]*,", re.I)
# D123 (2026-11-09): the one closed list of exclusion and negation words. One anywhere in a state list's sentence (and
# in the lead-in line of a bulleted list), or in a remote location field, keeps it from passing silently or rejecting:
# "Candidates who reside in <yours> or <another> are not eligible", "We have paused hiring in ...", "... is on hold",
# "We are restricted from hiring in ...", "... are barred from this role", "... will be disqualified", "We avoid hiring
# in ...", "no longer", "Remote - US (not FL)". A needless flag where the word is about something else is the cost
NEGATION_WORD = re.compile(
    r"\b(?:not|no|never|none|except(?:ions?|ing)?|exclu(?:de|des|ded|ding|sions?)|ineligible|outside|other\s+than|unable|"
    r"cannot|paus(?:e|es|ed|ing)|hold(?:s|ing)?|restrict(?:s|ed|ing|ions?)?|prohibit(?:s|ed|ing|ions?)?|bar(?:s|red|ring)?|"
    r"disqualif(?:y|ies|ied|ying|ication)|avoid(?:s|ed|ing)?|closed|"
    # the check of f1a17e1: "Hiring in <states> is suspended", "... will be declined", "All US states but <yours>", "Non-FL"
    r"suspen(?:d|ds|ded|ding|sion)|declin(?:e|es|ed|ing)|reject(?:s|ed|ing|ion)?|refus(?:e|es|ed|ing|al)|"
    r"(?:all|any|every)\s+(?:[\w.-]+\s+){0,3}?but|"
    rf"non-(?:{'|'.join(sorted(STATE_CODES) + [re.escape(name) for name in US_STATES])})"
    r")\b|n['’]t\b", re.I)
QUOTE_AROUND = 100   # a long sentence's quote keeps this many characters before the cue and after the last state (D123)
_BLANK_LINE = re.compile(r"\n[ \t\r]*\n")   # a blank line: the top of a bulleted list's lead-in lines (D123)
_BULLET = re.compile(r"\s*(?:[-*•·–—]|\d+[.)])?\s*")   # a list line's marker: "- Texas", "• Texas", "1. Texas"


def _clause_start(text: str, left: int, pos: int) -> int:
    """Where the clause holding `pos` starts, no earlier than the sentence's start `left`."""
    starts = [left] + [m.end() for m in _CLAUSE_BREAK.finditer(text, left, pos)]
    opening = _SUBORDINATE.match(text, left)
    if opening and opening.end() <= pos:
        starts.append(opening.end())
    return max(starts)


def _state_spans(fragment: str) -> list[tuple[int, int, str]]:
    """Every state named in the fragment, in order, as (start, end, code); "West Virginia" once, not also Virginia."""
    spans = []
    lowered = fragment.lower()
    for name, code in US_STATES.items():
        spans += [(m.start(), m.end(), code) for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered)]
    spans += [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", fragment)
              if m.group(1) in STATE_CODES]
    out: list[tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if not out or span[0] >= out[-1][1]:
            out.append(span)
    return out


def residence_limits(text: str) -> tuple[list[str], list[str], str | None]:
    """(allowed, excluded, the first sentence quoted): the states a residence sentence limits the role to or excludes.
    Never stored with the posting and never a rejection. Since D123 (2026-11-09) the gate reads the lists themselves
    (text_state_lists), not this reading of which kind a sentence is."""
    allowed, excluded, quotes = _residence_states(text or "")
    return sorted(allowed - excluded), sorted(excluded), (quotes[0] if quotes else None)


def _plain(text: str) -> str:
    """The text with the dots that don't end a sentence (U.S., St.) blanked, same length, so offsets hold."""
    return _ABBREVIATION.sub(lambda m: m.group(0).replace(".", "\x00"), text)


def _residence_lists(text: str, plain: str):
    """Each residence cue with a list of states named right after it, in its own sentence: (the cue, where its sentence
    starts and ends, the window read after the cue, the states' spans in it, the list's states, where the list ends in
    the window). A cue with no state close by is no list."""
    for cue in _RESIDENCE_CUE.finditer(plain):
        left = max([m.end() for m in _SENTENCE_END.finditer(plain, 0, cue.start())] or [0])
        end = _SENTENCE_END.search(plain, cue.end())
        right = end.start() if end else len(plain)
        window = text[cue.end(): min(right, cue.end() + RESIDENCE_WINDOW)]
        spans = _state_spans(window)
        if not spans or spans[0][0] > RESIDENCE_LIST_START:
            continue
        states, last = _joined(window, spans)
        yield cue, left, right, window, spans, states, last


def _joined(fragment: str, spans: list[tuple[int, int, str]]) -> tuple[set[str], int]:
    """(the states, where the list ends): the list that starts at the fragment's first state (`spans`, from
    _state_spans), as long as only list words join its states ("Arizona; Florida; or Texas")."""
    states, last = {spans[0][2]}, spans[0][1]
    for start, stop, code in spans[1:]:
        if _LIST_JOIN.fullmatch(fragment[last:start]) is None:
            break   # the list ended: a later state is about something else
        states.add(code)
        last = stop
    return states, last


def _residence_states(text: str) -> tuple[set[str], set[str], list[str]]:
    """(allowed, excluded, sentences): the list of states named right after each residence cue, in the same sentence,
    when the sentence requires it of the candidate, or excludes it."""
    allowed: set[str] = set()
    excluded: set[str] = set()
    quotes: list[str] = []
    for cue, left, right, window, spans, states, last in _residence_lists(text, _plain(text)):
        tail = _CLAUSE_BREAK.split(window[last:], maxsplit=1)[0]   # the rest of the requirement's clause
        after = re.split(r"[,;]", tail, maxsplit=1)[0]
        clause = _clause_start(text, left, cue.start())   # the requirement's own clause
        if _PREFERENCE.search(text[clause:cue.start()]) or _PREFERENCE.search(after) or \
                _ALTERNATIVE.search(window[:last] + tail):
            continue
        sentence = " ".join(text[left:right].split())[:200]
        # a negation in an earlier clause isn't about the requirement: "Relocation is not available; candidates must
        # reside in Texas" requires Texas (the fix-check of 2026-11-05; "..., but" too, Codex Pass 1 F7); nor is one
        # before a requirement phrase that ends at the cue ("We cannot hire interns and applicants must reside in Texas")
        base = max(clause, cue.start() - 120)
        required = _REQUIRED_BEFORE.search(text[base:cue.start()])
        reach = max(clause, cue.start() - 50, (base + required.start() - 12) if required else 0)
        if _RESIDENCE_NEGATION.search(text[reach: cue.end() + spans[0][0]]):
            excluded |= states
            quotes.append(sentence)
        elif _MUST_NOT.search(text[max(clause, cue.start() - 60):cue.start()]):
            excluded |= states   # "must not reside in", "cannot live in"
            quotes.append(sentence)
        elif _ANY_NEGATION.search(text[max(reach, cue.start() - 25): cue.end() + spans[0][0]]):
            continue   # a negation it can't read ("do not need to live in", "but not California"): neither
        elif _REQUIRED_BEFORE.search(text[max(left, cue.start() - 120):cue.start()]) or _REQUIRED_AFTER.match(window[last:]):
            allowed |= states
            quotes.append(sentence)
    return allowed, excluded, quotes


def remote_state_rules(location: str, text: str, remote_type: str) -> tuple[list[str], list[str]]:
    """(allowed, excluded) states for a remote/hybrid role, only when the posting says so explicitly.
    'Remote - TX, FL' limits to TX/FL; 'New York, NY or Remote' does not (NY is the office alternative);
    'not eligible for hire in: AK, CA' excludes AK/CA rather than limiting to them. A single state after a residence
    cue ('open to candidates who reside in: Texas') is not one of these: text_state_lists reads it, for the gate. Stored
    with the posting; only the location's states reject (D119, D123): the gate reads it with no text."""
    if remote_type not in ("remote", "hybrid"):
        return [], []
    allowed: set[str] = set()
    excluded: set[str] = set()
    loc = location or ""
    marker = re.search(r"(?i)remote", loc)
    if marker:
        allowed |= _states_in(loc[marker.end():])  # only states named after "remote" restrict it
    for _cue, listed, unlisted, _window in _state_lists(text):
        allowed |= listed
        excluded |= unlisted
    return sorted(allowed - excluded), sorted(excluded)


def _state_lists(text: str):
    """The older list rule, cue by cue: (the cue, the states it limits the role to, the states it excludes, the window
    read) for each cue with two or more states (or "states") in the 400 characters after it."""
    for cue in _STATE_LIST_CUES.finditer(text):
        window = text[cue.end(): cue.end() + 400].split("\n\n", 1)[0]
        hits = _states_in(window)
        if not (len(hits) >= 2 or "states" in cue.group(0).lower()):
            continue
        if _NEGATION.search(text[max(0, cue.start() - 50): cue.end()]):
            yield cue, set(), hits, window
        else:
            yield cue, hits, set(), window


def _state_marks(fragment: str) -> list[tuple[int, int]]:
    """(start, end) of every state _states_in finds in the fragment."""
    lowered = fragment.lower()
    marks = [m.span() for name in US_STATES for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered)]
    marks += [m.span() for m in re.finditer(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", fragment) if m.group(1) in STATE_CODES]
    return marks


def text_state_lists(text: str) -> list[tuple[list[str], list[str], str, bool]]:
    """D123 (2026-11-09): every state list read from the description text, in the order of its cues, as (the states the
    flag names, the states the list names, the quote, negated). Two readers: the older list rule (the cues of the stored
    lists: "reside in", "located in", "work from", "following states" ...; two or more states in the 400 characters
    after, or one after a "states" cue; a cue running into a word, "hire In" of "hire Indiana", is read from that word
    on), and the residence reader (a residence cue with a state close after it, in its own sentence, whatever the
    sentence says of it: _residence_lists). Either cue on a line ending in ":" reads the lines of states under it, blank
    lines skipped (_bullets: "residing in the following states:\\n\\n- Texas\\n\\n- Georgia"). The states a list names:
    those lines, or the list that starts at the first state after the cue, in the cue's own sentence or line, while only
    list words join them (_joined): "located in Texas or Georgia and have Florida Medicaid experience" names TX, GA; a
    later state, or one the older rule's window reaches past the sentence, is not named. Negated: an exclusion or
    negation word (NEGATION_WORD) anywhere in the list's sentence, its lead-in line and lines included (_negated). The
    quote holds the cue and the states (_quote). Nothing here is stored (the stored lists stay as the older rule reads
    them) and nothing here rejects: the gate is silent on a list that names your state and is not negated, and flags any
    other (residence_limit_check)."""
    text = text or ""
    plain = _plain(text)
    found = []   # (where the cue starts, the states the flag names, the states the list names, where the list ends)
    for cue in _STATE_LIST_CUES.finditer(text):
        runs_on = cue.group(0)[-2:].lower() == "in" and text[cue.end():cue.end() + 1].isalpha()
        begin = cue.end() - 2 if runs_on else cue.end()
        window = text[begin: begin + 400].split("\n\n", 1)[0]
        taken = _states_in(window)
        bullets = _bullets(text, plain, cue.start(), begin)
        if not (len(taken) >= 2 or (taken and "states" in cue.group(0).lower()) or bullets):
            continue
        if bullets:
            last, named = bullets
        else:
            end = _SENTENCE_END.search(plain, begin)
            sentence = text[begin:end.start() if end else len(text)]
            spans = _state_spans(sentence)
            named, stop = _joined(sentence, spans) if spans else (set(), 0)
            last = begin + stop
        if not named:   # no state in its own sentence: the flag names, and quotes up to, the states the older rule took
            last = begin + max(e for _s, e in _state_marks(window))
        found.append((cue.start(), named or taken, named, last, bool(bullets)))
    for cue, _left, _right, _window, _spans, states, last in _residence_lists(text, plain):
        found.append((cue.start(), states, states, cue.end() + last, False))
    for cue in _RESIDENCE_CUE.finditer(plain):
        if bullets := _bullets(text, plain, cue.start(), cue.end()):
            found.append((cue.start(), bullets[1], bullets[1], bullets[0], True))
    return [(sorted(states), sorted(named), _quote(text, plain, at, last), _negated(text, plain, at, last, lead))
            for at, states, named, last, lead in sorted(found, key=lambda f: f[0])]


def _bullets(text: str, plain: str, at: int, begin: int) -> tuple[int, set[str]] | None:
    """(where the last one ends, their states): the lines of states under a cue (starting at `at`, read from `begin`) whose
    line ends in ":", blank lines skipped, each line only states and list words after an optional marker ("- Texas",
    "Texas, Georgia"); None when the cue's line doesn't end in ":" or no such line follows (D123). The first other line
    ends the list ("- Experience with Florida Medicaid", "Office: Tampa, Florida")."""
    end = _SENTENCE_END.search(plain, begin)
    if not end or end.group() != "\n" or not text[at:end.start()].rstrip().endswith(":"):
        return None
    states: set[str] = set()
    last, pos = 0, end.end()
    while pos < len(text):
        stop = text.find("\n", pos)
        stop = len(text) if stop < 0 else stop
        line = text[pos:stop]
        if line.strip():
            bullet = _BULLET.match(line)
            item = line[bullet.end() if bullet else 0:].rstrip(" .;,\r")
            spans = _state_spans(item)
            gaps = zip([0] + [s[1] for s in spans], [s[0] for s in spans] + [len(item)], strict=True)
            if not spans or any(_LIST_JOIN.fullmatch(item[a:b]) is None for a, b in gaps):
                break
            states |= {code for _s, _e, code in spans}
            last = stop
        pos = stop + 1
    return (last, states) if states else None


def _negated(text: str, plain: str, at: int, last: int, lead: bool = False) -> bool:
    """Whether an exclusion or negation word (NEGATION_WORD) is anywhere in a list's sentence (D123): from the start of
    the sentence or line of its cue (at `at`) to the end of the sentence of its last state or line (ending at `last`). No
    clause is excused: "We cannot hire interns, but applicants must reside in <yours>" flags, needlessly (the cost),
    as "Applicants from the following states are not eligible; this includes those living in <yours>" must. A bulleted
    list (`lead`) also reads the lead-in lines above its cue line, up to the blank line before them ("Not eligible for
    this role:\\nCandidates residing in the following states:\\n- ...", the check of f1a17e1)."""
    left = max([m.end() for m in _SENTENCE_END.finditer(plain, 0, at)] or [0])
    if lead:
        left = max([m.end() for m in _BLANK_LINE.finditer(text, 0, left)] or [0])
    end = _SENTENCE_END.search(plain, last)
    return bool(NEGATION_WORD.search(text, left, end.start() if end else len(text)))


def negated_mentions(text: str) -> set[str]:
    """The states named (by name or code) in any sentence or line of the text that holds an exclusion or negation word
    (D123, the check of f1a17e1): a list naming your state stays silent only when no such sentence names your state
    ("... reside in Florida, Georgia, or Texas. Florida residents are not eligible at this time."; "We hire in FL, GA
    and TX.\\nNote: FL is excluded for this role.")."""
    text = text or ""
    plain = _plain(text)
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(plain)]
    named: set[str] = set()
    for start, stop in zip(starts, starts[1:] + [len(text)], strict=True):
        if NEGATION_WORD.search(text, start, stop):
            named |= _states_in(text[start:stop])
    return named


# D149 (Codex Pass 2, F, R-R2): "Applicants must reside in Florida, Texas; we can hire only in Texas." passed silently,
# the first list read and the later narrowing missed. A STATE STATEMENT is a residence, hiring, eligibility, exclusion
# or "only" cue with a state (or the word "state") in its clause; a cue after a state the statement already named starts another
# ("... reside in Florida or Texas and we can hire only in Texas" is two). The description is silent on residence only
# when it holds no state statement, or exactly one that names your state with no exclusion or negation word (a list's
# reading by text_state_lists decides it when there is one); a second statement, anywhere, flags.
_STATEMENT_CUE = re.compile(r"\b(?:resid\w*|li(?:ve|ves|ving)\s+in|located\s+in|based\s+in|hir(?:e|es|ed|ing)|employ(?:s|ed|ing)?|"
                            r"work(?:ing)?\s+from|remote\s+in|available\s+in|eligib\w*|ineligible|only|exclu\w*|"
                            r"except\w*|restrict\w*|limit\w*|within|must\s+be\s+in)\b", re.I)
_STATE_WORD = re.compile(r"\bstates?\b", re.I)
# ... a statement naming no state that only points back at the one before ("must reside in one of those states") is it
_BACK_REFERENCE = re.compile(r"\b(?:these|those|such|said|above|listed|aforementioned)\s+states?\b", re.I)


@dataclass
class StateStatement:
    quote: str
    states: set
    negated: bool
    cue_line: bool = False   # a line ending in ":" whose lines below are its list (D155)


def state_statements(text: str) -> list[StateStatement]:
    """Every state statement of the description, in order (D149, R-R2)."""
    text = text or ""
    plain = _plain(text)
    cuts = {0, len(text)} | {m.end() for m in _SENTENCE_END.finditer(plain)} | {m.end() for m in re.finditer(";", text)}
    cuts |= {m.end() for m in _CLAUSE_BREAK.finditer(text)}
    bounds = sorted(cuts)
    found = []
    for start, stop in zip(bounds, bounds[1:], strict=False):
        clause = text[start:stop]
        marks = _mentioned(clause, _state_spans(clause))   # a word code standing alone is no state here either (D155)
        heads: list[int] = []   # where each statement's cue starts
        for cue in _STATEMENT_CUE.finditer(clause):
            if not heads or any(heads[-1] <= s < cue.start() for s, _e, _c in marks):
                heads.append(cue.start())
        for i, head in enumerate(heads):
            left = 0 if i == 0 else head
            right = heads[i + 1] if i + 1 < len(heads) else len(clause)
            part = clause[left:right]
            states = {code for s, _e, code in marks if left <= s < right}
            if states or _STATE_WORD.search(_BACK_REFERENCE.sub(" ", part)):
                found.append(StateStatement(" ".join(part.split()), states, bool(NEGATION_WORD.search(part))))
    return found


def home_mention(text: str, home: str) -> str | None:
    """The first sentence or line of the text naming `home` (by name or code) in any way, quoted; None when none does
    (D149, R-R1: a location field naming only other states rejects only then)."""
    text = text or ""
    plain = _plain(text)
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(plain)]
    for start, stop in zip(starts, starts[1:] + [len(text)], strict=True):
        if any(code == home for _s, _e, code in _state_spans(text[start:stop])):
            return " ".join(text[start:stop].split())[:200]
    return None


def location_states(location: str) -> set[str]:
    """Every state a location field names, anywhere in it and in any format ("Remote - TX", "TX (Remote)",
    "US-TX-Remote", "Texas", "Austin, TX"; D151, R4)."""
    return {code for _s, _e, code in _state_spans(location or "")}


def state_clauses(text: str) -> list[StateStatement]:
    """state_clauses' mentions that name a state (see _state_clauses_all)."""
    return [mention for mention in _state_clauses_all(text) if mention.states]


def _state_clauses_all(text: str) -> list[StateStatement]:
    """Every mention of states in the description, in order (D151, R3): one per clause naming a state (a clause ends at
    a sentence end, a semicolon or a clause break such as ", but"), and one more for each later state of the clause that
    no list word joins to the one before ("Florida or Texas (Florida pending approval)" is two). Lines holding only states
    under a line ending in ":" are that line's one mention ("... the following states:\\n- Florida\\n- Texas")."""
    text = text or ""
    plain = _plain(text)
    cuts = {0, len(text)} | {m.end() for m in _SENTENCE_END.finditer(plain)} | {m.end() for m in re.finditer(";", text)}
    cuts |= {m.end() for m in _CLAUSE_BREAK.finditer(text)}
    bounds = sorted(cuts)
    found: list[StateStatement] = []
    under = False   # the clause before (blank ones passed over) ends in ":" or is a line of states under one
    labelled = False   # the line above this clause's line (blank ones passed over) holds a location word (D152)
    for start, stop in zip(bounds, bounds[1:], strict=False):
        clause = text[start:stop]
        if not re.search(r"\w", clause):   # a blank line, or a list line's marker cut off as a clause break ("\n- ")
            continue
        line_at = text.rfind("\n", 0, start) + 1
        if not text[line_at:start].strip(" \t-\u2013\u2014*\u2022"):   # the clause starts its line (a list marker aside)
            above = [line for line in text[:line_at].split("\n") if line.strip()]
            labelled = bool(above and _CODE_LINE_LABEL.search(above[-1]))
        line_end = text.find("\n", stop)
        after = "" if clause.endswith("\n") else text[stop:line_end if line_end != -1 else len(text)]
        spans = _mentioned(clause, _state_spans(clause), text[line_at:start], after, listed=labelled)
        quote, negated = " ".join(clause.split()), bool(NEGATION_WORD.search(clause))
        bare = _BULLET.sub("", clause, count=1)
        marks = _state_spans(bare)
        rest = "".join(" " if any(s <= i < e for s, e, _c in marks) else ch for i, ch in enumerate(bare))
        if under and spans and _LIST_JOIN.fullmatch(rest.rstrip(".")):
            found[-1].states |= {code for _s, _e, code in spans}   # "- Texas" under "... the following states:"
            continue
        under = clause.rstrip().endswith(":")
        if under and not spans:   # the cue line of a bulleted list: its lines are one mention
            found.append(StateStatement(quote, set(), negated, cue_line=True))
            continue
        group: set[str] = set()
        last = None
        for s, e, code in spans:
            if last is not None and _LIST_JOIN.fullmatch(clause[last:s]) is None:
                found.append(StateStatement(quote, group, negated))
                group = set()
            group.add(code)
            last = e
        if group:
            found.append(StateStatement(quote, group, negated))
    return found


# D155 (Codex Pass 3, F7): after a list naming your state, "Eligible states:\n- Northeast region\n- Pacific region"
# passed silently: its lines name no state, so neither reader kept it. A RESIDENCE OR ELIGIBILITY CUE LINE (ending in ":")
# WHOSE LINES BELOW NAME NO STATE is a restriction naming none: residence_limit_check
_UNLISTED_CUE = re.compile(r"\b(?:states?|locations?|locat\w*|resid\w*|li(?:ve|ves|ving)|hire|hiring|based|work[ \t]+from|"
                           r"territor\w*|regions?)\b", re.I)


def unlisted_cues(text: str) -> list[StateStatement]:
    """The residence or eligibility cue lines ending in ":" with no state in the lines below them (D155, F7)."""
    return [m for m in _state_clauses_all(text) if m.cue_line and not m.states and _UNLISTED_CUE.search(m.quote)
            and _STATEMENT_CUE.search(m.quote)]


# ... a state code that is also an everyday word or abbreviation in capitals ("5+ years ...; OR", "IN", "OK", "PA") is
# ignored only as a bare word or connective ("; OR", "OR:", "AND/OR", a line "OR" between two requirements).
# FAIL-CLOSED (D152, the checks of 8fb965d and 7d65a23: "Must be located in OH.", "Location: OH", "Remote (OH)" and
# "Hiring in: MD" passed silently): it is a state mention when, spaces and the marks : ( ) [ ] - , / | and dashes
# between them passed over, on its line, it stands next to
#   - a cue or location word (_CODE_WORDS: "in", "to", "within", "only", "residents", "based", "location", "remote",
#     "hiring", "office", "state", "eligible" ...), before it or after it ("Location: OH", "OH (Remote)", "OH-based"),
#     except after it behind ":" or ";" ("OR: 5+ years ...": a connective's colon);
#   - another state in a list ("FL, OR, WA"), or a capitalised word right before it, a city ("Columbus OH", "Portland,
#     OR"; not for OR, the commonest connective: "Excel OR SQL");
#   - or it is alone on its line (behind a list marker) under a line holding a location word ("Eligible locations:\n- OH",
#     "Location\nOH").
# "MS" before a Microsoft product ("in MS Office", "MS Excel") is no state. A needless flag ("experience in OR",
# "Office ME") is the accepted cost.
_WORD_CODES = {"OR", "IN", "ME", "OK", "HI", "PA", "MD", "CO", "LA", "MA", "AL", "ID", "OH", "MS", "MO", "DE"}
_LOCATION_WORDS = (r"only|residents?|based|locations?|located|remote|hybrid|onsite|on-site|hiring|hire|office|site|"
                   r"states?|region|territory|eligible|eligibility")
_CODE_WORDS = rf"in|to|from|within|of|at|{_LOCATION_WORDS}"
_CODE_MARKS = r"[\s:()\[\],/|.'\"`\-\u2013\u2014\u2018\u2019\u201c\u201d\u00ab\u00bb]*"   # D155 (F7): quotes too
_CODE_PLACE_BEFORE = re.compile(rf"(?<![\w-])(?:{_CODE_WORDS}){_CODE_MARKS}$", re.I)
_CODE_PLACE_AFTER = re.compile(rf"^(?![ \t]*[:;]){_CODE_MARKS}(?:{_CODE_WORDS})(?![\w-])", re.I)
_CODE_CITY_BEFORE = re.compile(r"(?<![\w-])[A-Z][a-z]+[ \t]*,?[ \t]*\(?[ \t]*$")
_CODE_LINE_LABEL = re.compile(rf"(?<![\w-])(?:{_LOCATION_WORDS})(?![\w-])", re.I)
_MS_PRODUCT = re.compile(r"^[ \t]+(?:Office|Excel|Word|Access|Project|Teams|Visio|PowerPoint|Outlook|SQL|Dynamics|365|"
                         r"SharePoint)\b")


def _mentioned(clause: str, spans: list[tuple[int, int, str]], before: str = "", after: str = "",
               listed: bool = False) -> list[tuple[int, int, str]]:
    """The spans of a clause that mention a state (D151, R3): every one but a word code (_WORD_CODES) standing alone.
    `before` and `after` are the rest of the clause's line around it; `listed`: the line above it holds a location word
    (D152)."""
    kept = []
    for i, (s, e, code) in enumerate(spans):
        word = clause[s:e]
        left, right = (before + clause[:s]).split("\n")[-1], (clause[e:] + after).split("\n")[0]
        if word == "MS" and _MS_PRODUCT.match(right):
            continue
        if word in _WORD_CODES:
            joined = (i and _LIST_JOIN.fullmatch(clause[spans[i - 1][1]:s])) or \
                (i + 1 < len(spans) and _LIST_JOIN.fullmatch(clause[e:spans[i + 1][0]]))
            placed = _CODE_PLACE_BEFORE.search(left) or _CODE_PLACE_AFTER.match(right)
            city = code != "OR" and _CODE_CITY_BEFORE.search(left)
            alone = listed and not _BULLET.sub("", left, count=1).strip() and not right.strip(" \t.")
            if not (joined or placed or city or alone or re.search(r"[A-Z][a-z]+,[ \t]*$", left)):
                continue
        kept.append((s, e, code))
    return kept


def _quote(text: str, plain: str, at: int, last: int) -> str:
    """The quote of a list (D123): its cue's sentence (the cue at `at`) to the end of the sentence of its last state
    (ending at `last`), whitespace collapsed; a long sentence keeps QUOTE_AROUND characters before the cue and after
    the last state, so the quote always holds the cue and its states."""
    left = max([m.end() for m in _SENTENCE_END.finditer(plain, 0, at)] or [0])
    end = _SENTENCE_END.search(plain, last)
    right = end.start() if end else len(text)
    if at - left > QUOTE_AROUND:
        left = text.find(" ", at - QUOTE_AROUND, at) + 1 or at - QUOTE_AROUND
    if right - last > QUOTE_AROUND:
        right = max(text.rfind(" ", last, last + QUOTE_AROUND), last)
    return " ".join(text[left:right].split())


def remote_states(location: str, text: str, remote_type: str) -> list[str]:
    """States a remote/hybrid role is limited to; [] = no stated limit."""
    return remote_state_rules(location, text, remote_type)[0]


_US_TERMS = ("united states", "usa", "us", "u s")


def title_country(title: str | None) -> str | None:
    """'non_US' when the title itself names a non-US country, e.g. '(Canada) Implementation Consultant' or
    'Claims Analyst - UK'. A title that also names the US ('US/Canada') is left to the location rules."""
    title = title or ""
    pieces = re.findall(r"[(\[]([^)\]]*)[)\]]", title) + re.split(r"\s[-\u2013\u2014|:,]\s", title)[1:]
    pieces += re.split(r"\s[-\u2013\u2014|:,]\s", title)[:1] if re.match(r"^\s*[(\[]", title) is None and " - " in title else []
    found_non_us = found_us = False
    for piece in pieces:
        p = norm(piece)
        found_us = found_us or any(contains_term(p, t) for t in _US_TERMS)
        found_non_us = found_non_us or any(contains_term(p, c) for c in NON_US)
    return "non_US" if found_non_us and not found_us else None


def detect_country(location: str, text: str, remote_type: str) -> str:
    loc = norm(location)
    if any(contains_term(loc, c) for c in ("united states", "usa", "us", "u s")) or _states_in(location or ""):
        return "US"
    if any(contains_term(loc, c) for c in NON_US):
        return "non_US"
    body = norm(text[:3000])
    if re.search(r"\b(united states|u s based|us based|within the us)\b", body):
        return "US"
    if remote_type == "remote" and not loc.replace("remote", "").strip():
        return "unknown"
    return "unknown"


_NATIONWIDE = re.compile(r"\b(anywhere in the (us|u\.s\.?|united states|usa|country)|all 50 (us |u\.s\. )?states|"
                         r"any (us |u\.s\. )?state|nationwide)\b")
# ...and only in a sentence about where the person may work: "we serve members nationwide" says nothing about it
_NATIONWIDE_CUE = re.compile(r"\b(remote|remotely|work from|working from|hire|hiring|candidates?|employees? (in|from|located)|"
                             r"reside|residing|live in|living in|located in|based in|eligible)\b")
_TIMEZONE = re.compile(r"\b(eastern|central|mountain|pacific|est|cst|mst|pst|edt|cdt|mdt|pdt)\s+(?:standard\s+)?"
                       r"(?:time\s*zone|timezone|time|business hours|working hours|hours)\b", re.I)
_TIMEZONE_CUE = re.compile(r"\b(must|required|requires|need to|needs to|able to|expected to|work|during|within|overlap|based in)\b", re.I)
_ZONES = {"est": "Eastern", "edt": "Eastern", "eastern": "Eastern", "cst": "Central", "cdt": "Central", "central": "Central",
          "mst": "Mountain", "mdt": "Mountain", "mountain": "Mountain", "pst": "Pacific", "pdt": "Pacific", "pacific": "Pacific"}


def remote_scope(remote_type: str, country: str, allowed_states: list, excluded_states: list, text: str = "") -> str:
    """Where a role may be worked from (2026-10-13). 'Remote' alone is never read as nationwide:
    US_nationwide (stated), US_restricted_states, state_specific, US_unspecified (remote in the US, states not stated),
    hybrid, onsite, international, unknown."""
    if country == "non_US":
        return "international"
    if remote_type == "onsite":
        return "onsite"
    if remote_type == "hybrid":
        return "hybrid"
    if remote_type != "remote":
        return "unknown"
    if allowed_states:
        return "state_specific" if len(allowed_states) == 1 else "US_restricted_states"
    if excluded_states:
        return "US_restricted_states"
    body = (text or "")[:6000].lower()
    for match in _NATIONWIDE.finditer(body):
        sentence = body[max(body.rfind(c, 0, match.start()) for c in ".!?\n") + 1: match.end()]
        if _NATIONWIDE_CUE.search(sentence):
            return "US_nationwide"
    return "US_unspecified" if country == "US" else "unknown"


def timezone_requirement(text: str) -> str | None:
    """A required working time zone ('must work Eastern time hours'), or None."""
    for match in _TIMEZONE.finditer((text or "")[:8000]):
        if _TIMEZONE_CUE.search(text[max(0, match.start() - 80): match.start()]):
            return _ZONES[match.group(1).lower()]
    return None


_TITLE_TERMS = {"contract to hire": "contract_to_hire", "temp to perm": "contract_to_hire", "contractor": "contract",
                "contract": "contract", "1099": "contract", "w2": "contract", "w 2": "contract", "c2c": "contract",
                "temporary": "temporary", "temp": "temporary", "seasonal": "temporary", "part time": "part_time"}
_TITLE_FILLER = re.compile(r"\b(\d+\s*(?:months?|mos?|weeks?)|remote|hybrid|onsite|on site|role|position|assignment|opportunity|only|us|usa)\b")


def title_employment(title: str | None) -> str | None:
    """Employment type stated in the title itself, e.g. 'Analyst (Contractor)' or 'Analyst - Contract'. It overrides a
    board's default field. Only a bracketed piece or a trailing segment made of nothing but such a term counts:
    'Provider Contract Analyst' and 'Analyst (Contract Management)' are jobs about contracts."""
    title = title or ""
    pieces = re.findall(r"[(\[]([^)\]]*)[)\]]", title)
    segments = re.split(r"\s[-\u2013\u2014|:,]\s", title)
    if len(segments) > 1:
        pieces.append(segments[-1])
    for piece in pieces:
        for part in re.split(r"[,;|/]|\s[-\u2013\u2014]\s", piece):
            key = " ".join(_TITLE_FILLER.sub(" ", norm(part)).split())
            if key in _TITLE_TERMS:
                return _TITLE_TERMS[key]
    return None


def employment_type(hint: str | None, text: str, title: str | None = None) -> str:
    stated = title_employment(title)
    if stated:
        return stated
    h = norm(hint or "")
    body = norm(text[:6000])
    if "contract to hire" in h or "contract to hire" in body or "temp to perm" in body:
        return "contract_to_hire"
    if h in ("fulltime", "full time", "permanent", "regular"):
        return "full_time"
    if h in ("contract", "contractor", "freelance"):
        return "contract"
    if h in ("temporary", "temp", "seasonal"):
        return "temporary"
    if h in ("parttime", "part time"):
        return "part_time"
    if h in ("intern", "internship"):
        return "internship"
    if re.search(r"\b(w2 contract|contract role|contract position|contract assignment|\d+ month contract|c2c|corp to corp|1099)\b", body):
        return "contract"
    if re.search(r"\bpart time\b", body) and not re.search(r"\bfull time\b", body):
        return "part_time"
    if re.search(r"\b(full time|fte|permanent position|permanent role|permanent opportunity)\b", body):
        return "full_time"
    return "unknown"


# cents after a grouped amount too: Oscar writes "$55,890.00 - $73,355.63" (2026-11-04: 77 postings had no pay or the
# wrong pay for it)
# "K" only as a whole word on the same line: "$60,000\nKey duties" is not 60 million
_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?)(?:[ \t]?([kK])(?![A-Za-z]))?"
_UNIT = r"(?:\s?/\s?(?:hour|hr|year|yr))?"
# the range's own unit: a whole word ("mo" is not the start of "more"), on the range's own line (the review of
# 2026-11-04: a tail that took a line break moved every tier's label onto the next range, and "A day in the life" on
# the next line read as a daily rate), after "/", "per", "a", ", per" or "(paid ...)"
_RANGE = re.compile(_MONEY + _UNIT + r"\s?(?:-|–|—|to)\s?" + _MONEY + r"(?:[ \t]?(?:usd|USD))?"
                    r"(?:[ \t]?(?:/|per[ \t]|an?[ \t]|,[ \t]?(?:paid[ \t])?per[ \t]|\((?:paid[ \t]|per[ \t])?)?[ \t]?"
                    r"(hour|hourly|hr|year|yr|annum|annually|annual|month|mo|monthly|week|wk|weekly|day|daily)(?![a-z]))?",
                    re.I)
_UNIT_PERIOD = {"hour": "hour", "hourly": "hour", "hr": "hour", "year": "year", "yr": "year", "annum": "year",
                "annually": "year", "annual": "year", "month": "month", "mo": "month", "monthly": "month", "week": "week",
                "wk": "week", "weekly": "week", "day": "day", "daily": "day"}


def _amount(number: str, k: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}
_DEADLINE_CUE = re.compile(r"(apply by|application deadline|applications? (?:close|closes|are due|due)|accepting applications "
                           r"(?:until|through)|deadline to apply|posting (?:closes|close date|end date)|close date)\W{0,6}", re.I)


def deadline_of(text: str) -> str | None:
    """An application deadline stated in the posting ("Apply by October 31, 2026", "Application deadline: 10/31/2026"),
    as YYYY-MM-DD; None when no deadline (or no full date) is stated."""
    for cue in _DEADLINE_CUE.finditer(text or ""):
        tail = text[cue.end():cue.end() + 40]
        m = re.match(r"(?:[a-z]+day,?\s+)?([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", tail, re.I)
        if m and m.group(1).lower() in _MONTHS:
            return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", tail) or None
        if m:
            return m.group(0)
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", tail)
        if m:
            return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def pay_quote(text: str | None, high: float | None = None) -> str | None:
    """The posting's own pay line (e.g. 'Pay range: $40,000 - $45,000 per year'), quoted as evidence; None if absent.
    With `high`, the line of the range that tops out there (the tier chosen for your state), else the first."""
    matches = list(_RANGE.finditer(text or ""))
    if not matches:
        return None
    match = next((m for m in matches if high is not None
                  and abs(max(_amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))) - high) < 0.5), matches[0])
    body = text or ""
    start = max((body.rfind(c, 0, match.start()) for c in "\n.;"), default=-1) + 1
    end = min([i for i in (body.find(c, match.end()) for c in "\n;") if i != -1] or [len(body)])
    return " ".join(body[start:end].split())[:200]


# --- pay tiers (2026-11-04) -------------------------------------------------------------------------------------
# Many postings list one range for high-cost states and another for "all other locations" (Oscar, Ramp), or one per
# zone or city (SmithRx, Judi, Omada). The first range is usually the highest: read alone, it overstated the pay that
# applies to you in 53 of the 60 open postings that say which range is yours (vault/reports/
# PAY_AND_STATE_PARSING_COUNTS_2026-11-04.md). Your range is chosen only when the posting says it: one tier names
# your state as a state, or your state is named nowhere and one "all other locations" tier sits beside tiers that each
# name a place. Anything else (zones defined elsewhere, a city of your state, levels) chooses nothing.
TIER_GAP = 700   # characters between the ranges of one pay block
# "excl." is no sentence end ("All states excl. FL" is one label, D120), in any case, nor "exc." or "ex." ("All states
# Excl. FL", "US (ex. FL)"; the second correction of 2026-11-09)
_TIER_BOUNDARY = re.compile(r"\n|;|(?<![A-Z])(?<!\b[Ee][Xx][Cc][Ll])(?<!\b[Ee][Xx][Cc])(?<!\b[Ee][Xx])\.\s+(?=[A-Z#(\-])")
_CATCH_ALL = re.compile(r"\ball other (?:u\.?s\.? )?(?:locations|states|areas|markets|regions|cities|geographies)\b|"
                        r"\bany other (?:location|state)s?\b|\beverywhere else\b|"
                        r"\brest of (?:the )?(?:us|u\.s\.|united states|country)\b", re.I)
# ... and one-line tiers joined by a comma or a bar (the third correction of 2026-11-09, T-2, D120): "California:
# $60,000 - $70,000 per year, Florida: $7,000 - $8,000" is two tiers, as with ". " or ";", when the words between the
# first separator after a range and the next range are a place label ("Florida, Georgia") and ":" or " - " (a comma
# inside an amount, "$60,000", never is one)
_JOINED_TIER = re.compile(r"(?:(?<!\d),(?!\d)|\|)[ \t]*([^|;\n$]{1,60}?)[ \t]*(?::|[ \t][-–—])[ \t]*$")
# a part of the state right before it ("Non-metro Georgia", "Greater Florida"); exclusions are _EXCLUSION's
_PARTIAL_CUE = re.compile(r"\b(?:non[- ]?metro|metro|greater|county|city of)\s*$", re.I)
# ... and, as a city of your state chooses nothing (D120), a region of it, the word right before the state: "South
# Florida", "Northern California" ("Southeast (FL, GA)" is a list of whole states)
_REGION_CUE = re.compile(r"\b(?:(?:north|south)(?:east|west)?|east|west|central|northern|southern|eastern|western|"
                         r"upstate|downstate)[ \t]+$", re.I)
# YOUR STATE'S TIER IS NAMED ONLY BY A PLAIN LABEL (the third correction of 2026-11-09, T-1, D120). Two rounds listed
# exclusion spellings by where they stood ("except" before the state, "excluded" right after it), and each check found
# more ("All states (FL and NY excluded)", "aside from FL", "(no FL)", "(exclusions: FL)", "Florida residents not
# eligible"). An exclusion word anywhere in a tier's label now leaves every state the label names open (partial, so your
# tier is ambiguous): an excluded state can't choose its tier, in either tier order. "Non-exempt" is a pay class.
_EXCLUSION = re.compile(r"\b(?:outside|except(?:ing|ion)?|exclu(?:de|des|ded|ding|sions?|sive)|excl|exc|ex|other than|"
                        r"aside from|apart from|besides|minus|without|w/o|not|no|non(?![- ]?exempt)|ineligible|save|saving|barring|"
                        r"(?:states|locations|everywhere|anywhere) but)\b", re.I)
# "Tampa, Florida" is a city of Florida as "Tampa, FL" is (D120): a city of your state chooses nothing
_CITY_STATE_NAME = re.compile(r"([A-Z][A-Za-z.]+(?:\s[A-Z][A-Za-z.]+)*),\s?(?i:("
                              + "|".join(sorted(US_STATES, key=len, reverse=True)) + r"))(?![A-Za-z])")
# ... and in every other spelling (the second correction of 2026-11-09, D120): a place word right before the state
# ("Tampa Florida", "Tampa (Florida)") or right after it behind a dash or a bracket ("Florida - Tampa only", "Florida
# (Tampa office)"), or a part of the state named after it or with "of" ("Florida Panhandle", "Panhandle of Florida").
# A place word is a capitalised word that is no state, state code, territory or word of a tier label ("Remote US,
# Florida", "Washington DC, Florida", "Florida (Remote)", "Florida residents" are whole states). And since the third
# correction (T-1): an area word right before the state, behind a comma ("Orlando area, FL", "Tampa Bay area,
# Florida"), and the state's possessive ("Florida's Gulf Coast") name a part of it too.
_PLACE_BEFORE = re.compile(r"(?<![\w'’.-])([A-Z][\w'’.-]*)[ \t]*\(?[ \t]*$")
_PLACE_AFTER = re.compile(r"[ \t]*(?:[-–—][ \t]*|\([ \t]*)([A-Z][\w'’.-]*)")
_STATE_PART = (r"(?:panhandle|keys|(?:gulf|space|atlantic|pacific|treasure|emerald|first|sun)?[ \t]*coast|peninsula|"
               r"regions?|areas?|metro(?:politan)?(?:[ \t]+areas?)?|suburbs|count(?:y|ies)|bay|parts?|portions?)")
_PART_AFTER = re.compile(rf"[ \t]+{_STATE_PART}\b", re.I)
_PART_OF = re.compile(rf"\b{_STATE_PART}[ \t]+of[ \t]+(?:the[ \t]+)?(?:state[ \t]+of[ \t]+)?$", re.I)
_PART_BEFORE = re.compile(rf"\b{_STATE_PART}[ \t]*,?[ \t]*$", re.I)
# The words of a plain label (T-1): besides the tier words below, the neutral ones of a whole-state tier ("Tier One
# (Florida)", "Florida (Fully Remote)", "Florida (Home-based)", "Florida (Entire State)", "Tier III (Florida)", "Florida -
# Telecommuting", "Florida (Exempt)", "Florida (Current Employees)", "Florida (Field-based)"); the second correction
# took them for cities and left a whole state's tier open (a flag where 285e72d decided)
_NOT_A_PLACE = {
    "all", "other", "any", "every", "rest", "state", "states", "statewide", "nationwide", "location", "locations", "us",
    "u.s", "usa", "u.s.a", "united", "america", "american", "remote", "hybrid", "onsite", "on-site", "in-office",
    "residents", "resident", "based", "only", "employees", "employee", "candidates", "candidate", "applicants", "workers",
    "staff", "tier", "zone", "level", "geo", "market", "markets", "pay", "range", "ranges", "salary", "base",
    "compensation", "hiring", "including", "includes", "include", "inclusive", "in", "of", "for", "from", "within", "and",
    "or", "the", "non", "high", "low", "cost", "hcol", "mcol", "lcol", "eastern", "central", "mountain", "pacific", "time",
    "district", "columbia", "puerto", "rico", "guam", "territories", "territory", "d.c", "dc", "group", "band", "grade",
    "region", "regions", "area", "areas", "starting", "target", "total", "expected", "minimum", "maximum", "annual",
    "monthly", "weekly", "hourly", "yearly", "full-time", "part-time", "rate", "rates", "wage", "wages", "ote", "usd",
    # a region heading over whole states: "Southeast (FL, GA)", "Gulf Coast (FL, TX)", "New England (MA, NH)"
    "north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest", "northern", "southern",
    "western", "midwest", "midwestern", "mid-atlantic", "atlantic", "gulf", "coast", "coastal", "new", "england",
    "upstate", "downstate", "sunbelt", "telecommute", "telework", "virtual", "wfh", "work", "home",
    "fully", "home-based", "field-based", "telecommuting", "teleworking", "entire", "exempt", "non-exempt", "current",
    "greater", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "ii", "iii", "iv", "vi",
    "vii", "viii", "ix", "first", "second", "third", "fourth", "fifth"}
# a metro whose name is common words counts only when no place word stands right before it ("Tampa Bay area")
_COMMON_METROS = {"bay area"}
# metros that appear as pay tiers: a metro is never the whole state, so one in your state makes the choice ambiguous
METROS = {
    "new york city": "NY", "nyc": "NY", "san francisco bay area": "CA", "san francisco": "CA", "bay area": "CA",
    "sf": "CA", "silicon valley": "CA", "los angeles": "CA", "san diego": "CA", "seattle": "WA", "boston": "MA",
    "cambridge": "MA", "denver": "CO", "boulder": "CO", "chicago": "IL", "austin": "TX", "dallas": "TX",
    "houston": "TX", "san antonio": "TX", "fort worth": "TX", "plano": "TX", "atlanta": "GA", "miami": "FL",
    "philadelphia": "PA", "charlotte": "NC", "phoenix": "AZ", "portland": "OR", "minneapolis": "MN",
    "salt lake city": "UT", "nashville": "TN", "raleigh": "NC",
}


@dataclass
class PayTier:
    match: re.Match
    low: float
    high: float
    unit: str
    end: int = 0                                # where the range's own words end (its trailing spaces dropped)
    lead_at: int = 0                            # where its label's words before it start
    own: int = 0                                # where its own words start: its label, when that names a place (P-2)
    label: str = ""
    states: set = field(default_factory=set)    # named as a state
    partial: set = field(default_factory=set)   # named only through a city, a metro or a qualifier
    catch_all: bool = False


def _tier_places(label: str) -> tuple[set, set, bool]:
    states: set[str] = set()
    partial: set[str] = set()
    rest = label
    for m in re.finditer(r"([A-Z][A-Za-z.]+(?:\s[A-Z][A-Za-z.]+)*),\s?([A-Z]{2})(?![A-Za-z])", label):   # "Denver, CO"
        if m.group(2) in STATE_CODES and m.group(1) not in STATE_CODES:   # "CA, NY, WA" is a list of states
            partial.add(m.group(2))
            rest = rest.replace(m.group(0), " ")
    masked = _place_mask(rest)
    for m in _CITY_STATE_NAME.finditer(rest):   # "Tampa, Florida"; "California, Florida" is a list of states, and so
        if _names_a_place(m.group(1), masked[m.start(1):m.end(1)]):   # is "Washington DC, Florida" (D120)
            partial.add(US_STATES[m.group(2).lower()])
            rest = rest.replace(m.group(0), " ")
    low = rest.lower()
    places = _place_mask(rest)
    for name in sorted(METROS, key=len, reverse=True):
        for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            word = re.search(r"(?<![\w'’.-])([A-Z][\w'’.-]*)[ \t]+$", rest[:m.start()])
            if name in _COMMON_METROS and word and _names_a_place(word.group(1), places[word.start(1):word.end(1)]):
                continue   # "Tampa Bay area" is no San Francisco (T-1)
            partial.add(METROS[name])
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
    for name in sorted(US_STATES, key=len, reverse=True):
        for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            (partial if _qualified(places, m.start(), m.end()) or _part_of_state(rest, places, m.start(), m.end())
             else states).add(US_STATES[name])
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
    for m in re.finditer(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", rest):   # a code under the same qualifiers as a name
        if m.group(1) in STATE_CODES:   # (Codex Pass 1, F6: "All states except FL" is not Florida's tier)
            (partial if _qualified(places, m.start(), m.end()) or _part_of_state(rest, places, m.start(), m.end())
             else states).add(m.group(1))
    if _EXCLUSION.search(label):   # T-1: an exclusion word anywhere leaves every state of the label open
        states, partial = set(), partial | states
    return states, partial, bool(_CATCH_ALL.search(label))


def _names_a_place(words: str, masked: str) -> bool:
    """Whether these words of a label (and `masked`, the same words with every state written as #'s) name a place
    within a state: a capitalised word that is no state, state code, territory or word of a tier label."""
    for m in re.finditer(r"[A-Z][\w'’.-]*", words):
        bare = m.group(0).strip(".'’-").lower()
        if len(bare) > 1 and "#" not in masked[m.start():m.end()] and bare not in _NOT_A_PLACE:
            return True
    return False


def _part_of_state(rest: str, places: str, start: int, end: int) -> bool:
    """A city or region of the state named beside it other than as "City, ST" or "City, Statename" (the second
    correction of 2026-11-09, D120): "Tampa Florida", "Tampa (Florida)", "Florida - Tampa only", "Florida (Tampa
    office)", "Florida Panhandle", "Panhandle of Florida"; and since the third correction (T-1) "Orlando area, FL",
    "Tampa Bay area, Florida", "Florida's Gulf Coast". `rest` is the label, `places` its mask."""
    if _PART_AFTER.match(places, end) or _PART_OF.search(places, 0, start) or _PART_BEFORE.search(places, 0, start):
        return True
    if re.match(r"['’]s\b", rest[end:]):   # "Florida's": a part of it
        return True
    before = _PLACE_BEFORE.search(rest, 0, start)   # in brackets, the state alone: "Tier One (FL, TX)" lists states
    if before and _names_a_place(before.group(1), places[before.start(1):before.end(1)]) and \
            ("(" not in before.group(0) or re.match(r"[ \t]*\)", rest[end:])):
        return True
    after = _PLACE_AFTER.match(rest, end)
    return bool(after and _names_a_place(after.group(1), places[after.start(1):after.end(1)]))


def _place_mask(label: str) -> str:
    """The label in lower case with every state name and code written as #'s, the same length."""
    masked = label.lower()
    for name in sorted(US_STATES, key=len, reverse=True):
        masked = re.sub(rf"(?<![a-z#]){re.escape(name)}(?![a-z])", lambda m: "#" * len(m.group(0)), masked)
    for m in re.finditer(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", label):
        if m.group(1) in STATE_CODES:
            masked = masked[:m.start()] + "##" + masked[m.end():]
    return masked


def _qualified(places: str, start: int, end: int) -> bool:
    """A state named under a qualifier: a region of it ("South Florida", D120) or a part of the state right before it,
    which carries across the list after it ("Greater Georgia and Florida"; the Fable fix-check of the Codex corrections,
    2026-11-05). An exclusion anywhere in the label leaves every state it names open (_tier_places, T-1)."""
    if _REGION_CUE.search(places, 0, start):
        return True
    before = re.sub(r"(?:[\s,/&()#]|\b(?:and|or)\b)+$", "", places[:start])
    return bool(_PARTIAL_CUE.search(before))


def pay_tiers(text: str) -> list[PayTier]:
    """The ranges that sit together with the posting's first range (gaps under TIER_GAP), each with the place its own
    words name: the clause before it back to the previous range or boundary, and the clause after it."""
    text = text or ""
    block: list[PayTier] = []
    for m in _RANGE.finditer(text):
        if block and m.start() - block[-1].end > TIER_GAP:
            break
        low, high = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
        block.append(PayTier(m, min(low, high), max(low, high), (m.group(5) or "").lower(),
                             m.start() + len(m.group(0).rstrip())))
    for i, tier in enumerate(block):
        floor = block[i - 1].end if i else max(0, tier.match.start() - 400)
        before = text[floor:tier.match.start()].rstrip()
        cuts = [c.end() for c in _TIER_BOUNDARY.finditer(before)]
        joined = None if cuts or not i else _JOINED_TIER.search(before)
        if joined and _names_place(joined.group(1)):   # "..., Florida: $7,000 - $8,000" (T-2)
            cuts = [joined.start() + 1]
        lead = before[cuts[-1]:] if cuts else ("" if i else before)   # no boundary since the last range: shared text
        tier.lead_at = floor + cuts[-1] if cuts else (tier.match.start() if i else floor)
        at = _place_at(lead)   # a heading in front of the place ("Monthly pay: California") is the block's
        tier.own = tier.lead_at + at if at >= 0 else tier.match.start()
        ceiling = block[i + 1].match.start() if i + 1 < len(block) else min(len(text), tier.end + 200)
        after = text[tier.end:ceiling]
        stop = _TIER_BOUNDARY.search(after)
        trail = after[:stop.start()] if stop else (after if i + 1 == len(block) else "")
        tier.label = f"{lead.strip()} [*] {trail.strip()}"
        tier.states, tier.partial, tier.catch_all = _tier_places(tier.label)
        if _EXCLUSION.search(_line_above(text, tier.lead_at)):   # D149 (R-P3): "All states, not\nFL:" wraps its exclusion
            tier.states, tier.partial = set(), tier.partial | tier.states
    return block


def _line_above(text: str, at: int) -> str:
    """The non-empty line above the one `at` starts, when `at` starts a line (blank lines passed over); else ""."""
    if text[text.rfind("\n", 0, at) + 1:at].strip():
        return ""
    lines = [line for line in text[:at].split("\n")[:-1] if line.strip()]
    return lines[-1] if lines else ""


def _place_at(words: str) -> int:
    """Where the first place a tier's label words name starts (a state, a metro, a zone or tier, "all other
    locations"); -1 when they name none."""
    low = words.lower()
    marks = [i for i in (_place_mask(words).find("#"),) if i >= 0]
    marks += [m.start() for m in (_ZONE.search(words), _CATCH_ALL.search(words)) if m]
    marks += [m.start() for m in (re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low) for name in METROS) if m]
    return min(marks, default=-1)


def _names_place(words: str) -> bool:
    """Whether a tier's label words name a place: a state, a city or metro, "all other locations", a zone or a tier."""
    states, partial, catch_all = _tier_places(words)
    return bool(states or partial or catch_all or _ZONE.search(words))


def home_pay_tier(text: str, home: str | None) -> tuple[str, PayTier | None, list[PayTier]]:
    """(status, the tier that applies to `home` or None, the block). status: single (one distinct range), named,
    catch_all, ambiguous (your state only partly in a tier, or in two) or unknown."""
    tiers = pay_tiers(text)
    if len({_tier_key(text, t) for t in tiers}) < 2:
        return "single", (tiers[0] if tiers else None), tiers
    if not home or not all(t.states or t.partial or t.catch_all for t in tiers):
        return "unknown", None, tiers   # a range with no place: levels, base and OTE, zones defined elsewhere
    if _BLOCK_EXCLUSION.search(_tier_text(text, tiers)):   # D149 (R-P3): no tier is chosen by its label
        return "ambiguous", None, tiers
    if _ARRANGEMENT.search(_tier_text(text, tiers, BLOCK_BELOW)):   # D155 (F6): nor beside a work arrangement
        return "ambiguous", None, tiers
    if not all(_plain_label(_label_text(text, tiers, i)) for i in range(len(tiers))):   # D151 (P2): a word not plain
        return "ambiguous", None, tiers
    if any(home in t.partial for t in tiers):
        return "ambiguous", None, tiers
    named = [t for t in tiers if home in t.states]
    if len(named) == 1:
        return "named", named[0], tiers
    if named:
        return "ambiguous", None, tiers
    catch = [t for t in tiers if t.catch_all and not (t.states or t.partial)]   # "all other California locations" isn't
    if len(catch) == 1 and all(t.states or t.partial for t in tiers if t is not catch[0]):
        return "catch_all", catch[0], tiers
    return "unknown", None, tiers


_ZONE = re.compile(r"\b(?:zones?|tiers?|regions?|geographic|geographies|locations?|markets?|areas?)\b", re.I)
# D149 (Codex Pass 2, A, R-P1): two ranges are one tier only when their amounts AND their periods are equal and each
# period is settled (one reading): "Zone A: $7,000 - $8,000 per year. Zone B: $7,000 - $8,000 per month." are two
# tiers (they were one, and both gates rejected on the yearly one), and two equal ranges whose period is open stay two
# (read every way at the gate, never on a guessed period).


def _tier_key(text: str, tier: PayTier) -> tuple:
    readings = period_readings(text, tier, [tier])
    return (tier.low, tier.high, next(iter(readings))) if len(readings) == 1 else (id(tier),)


# D149 (Codex Pass 2, E, R-P3): an exclusion was read only inside one tier's label, so a line wrap ("All states
# except\nFL: ...") or a word the label list lacked ("Save for FL") let the excluded state choose its tier. Now an
# exclusion word ANYWHERE in the pay block (from 400 characters before its first range to the end of its last range's
# line, read across line breaks) leaves no tier chosen by its label, by name, code or "all other locations": the
# choice is ambiguous, so ranges on both sides of the floor are pay_tier_unknown.
_BLOCK_EXCLUSION = re.compile(r"\b(?:except\w*|exclu\w*|excl|exc|save|saving|outside|other\s+than|not\s+in|but\s+not|"
                              r"besides|apart\s+from|aside\s+from|minus|ineligible|not\s+eligible|barring)\b|\bex\.", re.I)


def _tier_text(text: str, tiers: list[PayTier], below: int = 0) -> str:
    """The pay block of these tiers for exclusions: 400 characters before the first range to the end of the last
    range's line, and `below` non-empty lines after it (D155), its line breaks read as spaces."""
    end = text.find("\n", tiers[-1].end)
    end = end if end != -1 else len(text)
    below_lines = [line for line in text[end:].split("\n") if line.strip()][:below]
    return " ".join((text[max(0, tiers[0].match.start() - 400): end] + " " + " ".join(below_lines)).split())


# D155 (Codex Pass 3, F6): "Florida (on-site only): $90,000 - $100,000 per year. All other locations: $60,000 -
# $70,000 per year." chose the Florida tier for a remote Florida profile, and so did a tier note on the next line ("Tier
# 1 excludes remote positions. Remote positions use Tier 2."). A WORK ARRANGEMENT OR ELIGIBILITY TERM ANYWHERE IN THE
# PAY BLOCK (400 characters before the first range to eight non-empty lines after the last) CHOOSES NO TIER BY LABEL:
# ranges on both sides of the floor are pay_tier_unknown. Needless where the word is about something else.
_ARRANGEMENT = re.compile(r"\b(?:remote(?:ly)?|on-?site|hybrid|in-office|in[ \t]+office|office|only|eligib\w*|"
                          r"ineligible|exclu\w*|positions?|role[ \t]+types?|work[ \t]+arrangements?|telecommut\w*|"
                          r"work[ \t]+from[ \t]+home|wfh|field[- ]based|home[- ]based)\b", re.I)


# D151 (red team round 1, P2; the owner's allow-style rule under D149): "All states less FL", "sans FL", "not incl. FL",
# "≠ FL" chose the labelled tier for FL, as no exclusion list held the word. A TIER IS CHOSEN BY ITS LABEL ONLY
# WHEN EVERY TIER'S LABEL IS PLAIN: state names and codes, metros, and the words of _LABEL_WORDS (the neutral words of
# a tier label, _NOT_A_PLACE, and a pay sentence's own: "The base pay for this role in the states of ... is:"), with
# digits and plain punctuation. Any other word or symbol means no tier is chosen by label: ranges on both sides of the
# floor are pay_tier_unknown.
_LABEL_WORDS = _NOT_A_PLACE | {
    "for", "this", "role", "position", "job", "opening", "is", "are", "be", "will", "who", "live", "lives", "living",
    "located", "reside", "residing", "per", "year", "hour", "a", "an", "to", "at", "with", "everywhere", "else",
    "anywhere", "country", "transparency", "plus", "paid", "s"}
_LABEL_MARKS = re.compile(r"[\s#\d,:;()/&|*$.'’\[\]+\-–—]*")


def _label_text(text: str, tiers: list[PayTier], i: int) -> str:
    """The words a plain-label check reads for tier i: its line back to the line's start or the previous range (a
    label cut short by an abbreviation's dot, "All states, not incl. FL", is read whole), and its label's words after
    the range."""
    tier = tiers[i]
    start = max(text.rfind("\n", 0, tier.match.start()) + 1, tiers[i - 1].match.end() if i else 0)
    return text[start:tier.match.start()] + " " + tier.label.split("[*]", 1)[-1]


def _plain_label(label: str) -> bool:
    """Whether a tier's label holds only places and the words of _LABEL_WORDS (D151, P2)."""
    masked = _place_mask(label.replace("[*]", " "))
    for name in sorted(METROS, key=len, reverse=True):
        masked = re.sub(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lambda m: "#" * len(m.group(0)), masked)
    rest = re.sub(r"[a-z][a-z'’.-]*", lambda m: " " if m.group(0).strip(".'’-") in _LABEL_WORDS else m.group(0),
                  masked)
    return _LABEL_MARKS.fullmatch(rest) is not None


def tier_periods(text: str, tiers: list[PayTier], placed: bool = False) -> list[str]:
    """Each tier's pay period, read as parse_comp reads it."""
    return [_period(text, t, tiers if placed else [t]) for t in tiers]


def _clause_lead(text: str, start: int) -> str:
    """The words just before the range, in its own clause: "monthly vacation accrual" in the next sentence, a
    "monthly stipend" after a yearly salary, or "40 hours per week, ..., with a base salary of" says nothing about the
    pay's period (Oscar, 2026-11-04: 15 hourly postings were stored as monthly; and the review of the first fix)."""
    plain = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", "\x00"), text[:start])
    left = max([m.end() for m in _SENTENCE_END.finditer(plain)] or [0])
    return re.split(r"[,;(]", text[left:start])[-1][-80:].lower()


# a period read from nearby words, not the range's own unit, must fit the amount: "$60,000 - $70,000" is never weekly
_PERIOD_CEILING = {"hour": 500, "day": 5000, "week": 20000, "month": 60000}
# ... and from below: "$25 - $30, paid weekly" is an hourly rate paid each week, not $30 a week (the fix-check of
# 2026-11-05); a yearly top under 500 is an hourly rate
_PERIOD_FLOOR = {"hour": 0, "day": 100, "week": 500, "month": 1000, "year": 500}
# the words right after the range that its unit pattern doesn't take, always with a word before the unit: "paid
# monthly", ", paid out weekly", "each month", "every week" (", Day Shift" is no period)
_TRAILING_PERIOD = re.compile(r"[ \t]*(?:usd[ \t]*)?,?[ \t]*(?:(?:paid|payable)[ \t]+(?:out[ \t]+)?(?:per[ \t]+|an?[ \t]+|"
                              r"each[ \t]+|every[ \t]+)?|(?:per|an?|each|every)[ \t]+)"
                              r"(hour|hourly|day|daily|week|weekly|month|monthly|year|yearly|annually|annum)(?![a-z])", re.I)


def _fits(period: str, high: float) -> bool:
    """Whether a period read from anything but the range's own unit fits its amount: a $42 top is not a yearly salary
    (under 500 reads as hourly by size), "$60,000 - $70,000" is not hourly, and "$25 - $30" is not a weekly wage."""
    return _PERIOD_FLOOR[period] <= high < _PERIOD_CEILING.get(period, float("inf"))


def _period(text: str, tier: PayTier, siblings: list[PayTier]) -> str:
    """The range's own unit; else the words right after it ("paid monthly"); else, for a place tier, the one unit its
    sibling tiers share ("$75,348 - $98,894 per year ... all other locations: $67,813 - $89,005"); else the words
    before it in its sentence; else its size. Each but the first only when it fits the amount (the Fable review of
    2026-11-04: a sibling's "per year" made "Texas: $38 - $42" a yearly $42, and its "per hour" made a $70,000 catch-all
    hourly)."""
    if tier.unit:
        return _UNIT_PERIOD[tier.unit]
    trailing = _TRAILING_PERIOD.match(text, tier.end)
    if trailing and _fits(_UNIT_PERIOD.get(trailing.group(1).lower(), "year"), tier.high):
        return _UNIT_PERIOD.get(trailing.group(1).lower(), "year")
    shared = {t.unit for t in siblings if t.unit}
    if len(shared) == 1 and len(siblings) > 1 and _fits(_UNIT_PERIOD[next(iter(shared))], tier.high):
        return _UNIT_PERIOD[next(iter(shared))]
    lead = _clause_lead(text, tier.match.start())
    if "per month" in lead or "monthly" in lead:
        period = "month"
    elif "per week" in lead or "weekly" in lead:
        period = "week"
    elif "per day" in lead or "daily rate" in lead:
        period = "day"
    elif "per hour" in lead or "hourly" in lead or tier.high < 500:
        period = "hour"
    else:
        return "year"
    return period if _fits(period, tier.high) else ("hour" if tier.high < 500 else "year")


# FAIL-SAFE (the fix-check of 2026-11-05): every rule above for a period that is not the range's own fitting unit
# misread some real wording ("$42 - $48 Weekly Pay!", "Daily rate $650 - $750, paid weekly", "Salary: $55,000 -
# $59,000 paid monthly", a pay cycle on the next line). _period keeps the best guess for display; the gate asks
# period_readings for every period the posting's words or the amount could mean, and when those fall on both sides
# of your floor it flags pay_period_unknown (CONDITIONAL) instead of passing or rejecting on the guess.
# BLUNT SINCE D123 (the fourth correction of 2026-11-09, D120): three rounds told the pay's period words from another
# item's, a label's or a heading's by ever finer rules, and every check found wording they misread into a wrong
# rejection. Now every period word of the range's pay block is a reading: its own line, up to twelve non-empty lines
# above it and eight below (six and four missed a heading over six label lines; blank lines are passed over, as a posting's HTML puts one between every two lines; a stop
# at a paragraph of prose was tried and turned a monthly range under "Compensation is per month." into a wrong
# rejection); another tier's label and range are blanked out and never read (they are that tier's). Biweekly and
# semi-monthly pay, a bare amount of weekly or monthly size read every way it could be, and the guess itself are
# readings too. A range is decided yearly only when its own explicit yearly unit stands right after it ("per year",
# "/yr", "a year", "annually") and no other period word is in its block ("$12,000 - $14,000 (annual bonus eligible)"
# under "Monthly base:" flags): "Annual base salary: $50,000 - $55,000" beside "Bonuses are paid monthly" may flag (a
# needless CONDITIONAL is accepted; a wrong rejection is not).
# D149 (Codex Pass 2, A): "Compensation, per calendar month:" was read as yearly, as "per" had to stand right before
# the unit. ANY TIME WORD OF THE PAY BLOCK IS A READING: a bare hour, day, week, month or fortnight in any wording
# ("per calendar month", "a full week", "12 months", "(mo.)"), and a pay cycle the reader can't size (a quarter,
# "bimonthly", a pay period, cycle, check or day, a paycheck, an installment, "per" before any other lower-case word:
# "per term", "per assignment") reads as every cycle. A bare "year" adds no reading: an
# experience line ("5+ years") is in most pay blocks, and yearly is the reading of a large amount anyway.
_PERIOD_WORD = re.compile(r"\b(?:(?:per|an?|each|every)\s+(hour|day|week|month|year|annum)|(hourly|daily|weekly|monthly|yearly|"
                          r"annually|annual)|(bi-?weekly|fortnightly|every\s+(?:two|2)\s+weeks)|(semi-?monthly|twice\s+a\s+month)|"
                          r"(per\s+pay\s+period))\b|/[ \t]?(hr|hour|day|wk|week|mo|month|yr|year)\b|"
                          r"\b(?:(hours?|hrs?|days?|weeks?|wks?|months?|mos?|fortnights?|diem)|(quarter(?:s|ly)?|bi-?monthly|"
                          r"pay[ \t]*(?:periods?|cycles?|checks?|days?)|paychecks?|payroll|installments?|cycles?|"
                          # ... and "per" before any other lower-case word: "per term", "per semester", "per assignment"
                          r"per\s+(?!(?:year|annum|annual|calendar|fiscal|full|whole|the|a|an|our|your|its|their|this|"
                          r"that|these|those|company|"
                          r"policy|policies|applicable|state|federal|local|law|laws|cent|se|diem)\b)(?-i:[a-z])[a-z]*))\b",
                          re.I)
_BARE_PERIOD = {"hour": "hour", "hr": "hour", "day": "day", "week": "week", "wk": "week", "month": "month", "mo": "month",
                "fortnight": "biweekly", "diem": "day"}
ANY_CYCLE = {"week", "biweekly", "semimonthly", "month"}
_PERIOD_CEILING_READINGS = {**_PERIOD_CEILING, "biweekly": 40000, "semimonthly": 40000}
# the range's own unit is explicit after "per", "/", "a" or "an" ("$40 - $50 per hour, paid weekly" is hourly); bare or in
# brackets ("$450 - $495 (hourly ...") it settles nothing alone when its sentence names another period
_EXPLICIT_UNIT = re.compile(r"(?:/|\bper\b|\ban?\b)[ \t]*$", re.I)
BLOCK_ABOVE, BLOCK_BELOW = 12, 8   # non-empty lines of a range's pay block above and below its own line


def _pay_block(text: str, tier: PayTier) -> str:
    """The range's pay block as text, another tier's own words (its label from its place on, and its range) blanked
    out: the range's own line and up to BLOCK_ABOVE non-empty lines above it and BLOCK_BELOW below, blank lines and
    another tier's lines passed over."""
    masked = text
    for t in pay_tiers(text):
        if (t.match.start(), t.end) != (tier.match.start(), tier.end):
            masked = masked[:t.own] + re.sub(r"[^\n]", " ", masked[t.own:t.end]) + masked[t.end:]
    lines = masked.split("\n")
    here = text.count("\n", 0, tier.match.start())
    above = [line for line in lines[:here] if line.strip()][-BLOCK_ABOVE:]
    below = [line for line in lines[here + 1:] if line.strip()][:BLOCK_BELOW]
    return " ; ".join(above + [lines[here]] + below)


def _period_words(fragment: str) -> set[str]:
    found: set[str] = set()
    for m in _PERIOD_WORD.finditer(fragment):
        if m.group(1) or m.group(2) or m.group(6):
            found.add(_UNIT_PERIOD.get((m.group(1) or m.group(2) or m.group(6)).lower(), "year"))
        elif m.group(3):
            found.add("biweekly")
        elif m.group(4):
            found.add("semimonthly")
        elif m.group(7):   # D149: a bare time word
            found.add(_BARE_PERIOD[m.group(7).lower().rstrip("s")])
        else:
            found |= ANY_CYCLE   # "per pay period", a quarter, a pay cycle or check: any cycle
    return found


def period_readings(text: str, tier: PayTier, siblings: list[PayTier]) -> set[str]:
    """Every period this range could be paid by: only its own explicit unit when that fits the amount (a yearly one only
    when no other period word is in its pay block); else its own unit, every period named in its pay block, a sibling
    tier's unit, the one its size suggests, and the guess _period stores. A named period counts only under its ceiling
    (a $60,000 range is never weekly); the range's own unit and the guess always count. A place tier is read whatever
    its status (D120): its sibling tiers' unit adds a reading, and never takes away those the tier has alone."""
    found = _readings(text, tier, siblings)
    if len(siblings) > 1:
        found |= _readings(text, tier, [tier])
    return found


def _readings(text: str, tier: PayTier, siblings: list[PayTier]) -> set[str]:
    """period_readings with these siblings."""
    own = _UNIT_PERIOD.get(tier.unit) if tier.unit else None
    rng = tier.match
    explicit = bool(own) and bool(_EXPLICIT_UNIT.search(text[max(rng.end(3), rng.end(4)):rng.start(5)]))
    explicit = explicit or tier.unit in ("annually", "annual")   # "annual bonus" right after it: the bonus's word is a
    # period word of its block, so beside any other period word it decides nothing, and alone it is yearly anyway
    named = _period_words(_pay_block(text, tier))
    if own and explicit and _fits(own, tier.high) and (own != "year" or named <= {"year"}):
        return {own}
    # D155 (Codex Pass 3, F1): a period word of the range's own pay block is kept whatever the amount ("$60,000 -
    # $65,000 per quarter", "Monthly compensation $60,000 - $65,000" were read yearly once a ceiling dropped it); only a
    # sibling tier's unit must fit the amount, as before
    named = {p for p in named if p != "year" or tier.high >= 500}
    shared = {t.unit for t in siblings if t.unit}
    if len(shared) == 1 and len(siblings) > 1:
        unit = _UNIT_PERIOD[next(iter(shared))]
        if unit == "year" and tier.high >= 500 or unit != "year" and tier.high < _PERIOD_CEILING_READINGS[unit]:
            named.add(unit)
    if own and _fits(own, tier.high) and named <= {own}:
        return {own}   # a bare or bracketed unit nothing else contradicts ("$25 - $30 hourly")
    if not named:   # nothing names it: every period its size allows
        named = {"hour"} if tier.high < 500 else {"year"} if tier.high >= 10000 else \
            {p for p in ("day", "week", "month") if tier.high < _PERIOD_CEILING[p]}
    else:
        named.add("hour" if tier.high < 500 else "year")
    found = named | ({own} if own else set()) | {_period(text, tier, siblings)}
    if tier.high < YEARLY_ONLY_BY_UNIT and "year" in found and not _yearly_unit(text, tier):   # D151 (P1)
        found |= {p for p in ANY_CYCLE if tier.high < _PERIOD_CEILING_READINGS[p]}   # never yearly alone
    return found


# D151 (red team round 1, P1; the owner's allow-style rule under D149): "$12,000 - $14,000 p.m." (and "pcm", "/mth",
# "mthly", "every 30 days", "/ m") was read as yearly, as no reader knew the word. A RANGE UNDER $20,000 IS YEARLY ONLY
# BY ITS OWN YEARLY UNIT ("per year", "/yr", "a year", "annually", "annual", "yearly", "per annum", "p.a."; "salary of"
# is none): otherwise every pay cycle is a reading beside the yearly one ("Monthly salary: $5,000 - $6,000" too), so a
# floor between them is pay_period_unknown, never a rejection on a yearly guess. A range of $20,000 or more with no
# unit stays yearly by its size, as before.
YEARLY_ONLY_BY_UNIT = 20000
_YEARLY_AFTER = re.compile(r"[ \t]*(?:usd[ \t]*)?,?[ \t]*\(?[ \t]*(?:yearly|p\.[ \t]?a\.?|per[ \t]+annum)(?![a-z])", re.I)


def _yearly_unit(text: str, tier: PayTier) -> bool:
    """Whether the range's own unit, right after it, is yearly."""
    return _UNIT_PERIOD.get(tier.unit) == "year" or bool(_YEARLY_AFTER.match(text, tier.end))


def pay_period_readings(text: str, high: float | None, home: str | None) -> tuple[float, set[str]] | None:
    """(the range's top as posted, its period readings) for the range parse_comp stored with this top; None when the
    stored pay is not a range of this text (the board's own pay fields)."""
    if high is None:
        return None
    status, tier, tiers = home_pay_tier(text or "", home)
    chosen = tier or (tiers[0] if tiers else None)
    if chosen is None or abs(chosen.high - high) >= 0.5:
        return None
    placed = status in ("named", "catch_all", "ambiguous")
    return chosen.high, period_readings(text or "", chosen, tiers if placed else [chosen])


def parse_comp(text: str, structured: dict | None = None, home: str | None = None) -> dict | None:
    """Pay as {min, max, period, currency}: the board's own fields first; else the posting's text, taking the range
    that applies to `home` (a two-letter state) when the posting says which tier that is, and the first range
    otherwise."""
    if structured and (structured.get("min") or structured.get("max")):
        return structured
    status, tier, tiers = home_pay_tier(text or "", home)
    if not tiers:
        return None
    chosen = tier or tiers[0]
    placed = status in ("named", "catch_all", "ambiguous")   # a place-tier set: its tiers share one unit
    return {"min": chosen.low, "max": chosen.high, "period": _period(text, chosen, tiers if placed else [chosen]),
            "currency": "USD"}


_TRACKING = re.compile(r"^(utm_.*|gh_src|lever-source.*|source|src|ref|referrer|trk.*|mc_.*|fbclid|gclid|_ga)$", re.I)


def canonical_url(url: str | None) -> str | None:
    """Lower-case host, no fragment, tracking parameters dropped; identifying parameters (e.g. gh_jid) kept."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    pairs = sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p.split("=", 1)[0]))
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "&".join(pairs), ""))


def dedupe_key(parent: str, title_norm: str, location: str) -> str:
    core = re.sub(r"\b(req|job|id)\s?#?\s?[a-z0-9\-]+\b", " ", title_norm)
    core = re.sub(r"\s+", " ", core).strip()
    return f"{norm(parent)}|{core}|{norm(location)}"


def content_fingerprint(fields: dict) -> str:
    keys = ("title", "location_raw", "description_text", "employment_type", "remote_type", "comp_min", "comp_max", "comp_period")
    return fingerprint({k: fields.get(k) for k in keys})
