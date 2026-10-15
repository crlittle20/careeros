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


def relevant_role(title_norm: str, family: str | None, remote_type: str, country: str, policy) -> bool:
    """The one definition of "a role CareerOS is looking for", shared by board discovery and career fit (2026-10-13; they
    used to differ): one of your job families, no excluded title term, not outside the US, remote or not stated, and
    below manager level."""
    if not family or excluded_term(title_norm, policy.get("title_exclude", default=[]) or []):
        return False
    if country == "non_US" or remote_type not in ("remote", "unknown"):
        return False
    return seniority(title_norm, policy.get("seniority_terms", default={}) or {}) != "manager"


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


def remote_state_rules(location: str, text: str, remote_type: str) -> tuple[list[str], list[str]]:
    """(allowed, excluded) states for a remote/hybrid role, only when the posting says so explicitly.
    'Remote - TX, FL' limits to TX/FL; 'New York, NY or Remote' does not (NY is the office alternative);
    'not eligible for hire in: AK, CA' excludes AK/CA rather than limiting to them."""
    if remote_type not in ("remote", "hybrid"):
        return [], []
    allowed: set[str] = set()
    excluded: set[str] = set()
    loc = location or ""
    marker = re.search(r"(?i)remote", loc)
    if marker:
        allowed |= _states_in(loc[marker.end():])  # only states named after "remote" restrict it
    for cue in _STATE_LIST_CUES.finditer(text):
        window = text[cue.end(): cue.end() + 400].split("\n\n", 1)[0]
        hits = _states_in(window)
        if not (len(hits) >= 2 or "states" in cue.group(0).lower()):
            continue
        if _NEGATION.search(text[max(0, cue.start() - 50): cue.end()]):
            excluded |= hits
        else:
            allowed |= hits
    return sorted(allowed - excluded), sorted(excluded)


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


_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?"
_UNIT = r"(?:\s?/\s?(?:hour|hr|year|yr))?"
_RANGE = re.compile(_MONEY + _UNIT + r"\s?(?:-|–|—|to)\s?" + _MONEY + r"(?:\s?(?:usd|USD))?\s?(?:/|per\s)?\s?(hour|hr|year|yr|annum|annually|annual|month|mo|monthly|week|wk|weekly|day|daily)?", re.I)


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


def pay_quote(text: str | None) -> str | None:
    """The posting's own pay line (e.g. 'Pay range: $40,000 - $45,000 per year'), quoted as evidence; None if absent."""
    match = _RANGE.search(text or "")
    if not match:
        return None
    start = max((text.rfind(c, 0, match.start()) for c in "\n.;"), default=-1) + 1
    end = min([i for i in (text.find(c, match.end()) for c in "\n;") if i != -1] or [len(text)])
    return " ".join(text[start:end].split())[:200]


def parse_comp(text: str, structured: dict | None = None) -> dict | None:
    if structured and (structured.get("min") or structured.get("max")):
        return structured
    match = _RANGE.search(text or "")
    if not match:
        return None
    low, high = _amount(match.group(1), match.group(2)), _amount(match.group(3), match.group(4))
    unit = (match.group(5) or "").lower()
    context = (text[max(0, match.start() - 60): match.end() + 60]).lower()
    if unit in ("month", "mo", "monthly") or "per month" in context or "monthly" in context:
        period = "month"
    elif unit in ("week", "wk", "weekly") or "per week" in context or "weekly" in context:
        period = "week"
    elif unit in ("day", "daily") or "per day" in context or "daily rate" in context:
        period = "day"
    elif unit in ("hour", "hr") or "per hour" in context or "hourly" in context or high < 500:
        period = "hour"
    else:
        period = "year"
    if low > high:
        low, high = high, low
    return {"min": low, "max": high, "period": period, "currency": "USD"}


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
