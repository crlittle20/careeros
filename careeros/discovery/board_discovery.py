"""Verified employer-board discovery (2026-10-12, Parts 1, 2 and 9).

Turns recommendations, alert leads, manual leads and curated suggestions into employer -> board mappings on the four
allowed ATSs (Greenhouse, Lever, Ashby, Workable), using only their documented public listing endpoints.

Two separate decisions:
- EMPLOYER IDENTITY: is this board really this employer's? Evidence, strongest first:
    supplied_board_link    a recommendation, alert or person supplied a link on this exact board;
    official_domain        the employer's official domain appears in the board's own content or postings;
    board_name             the board's own display name is the employer (or a verified alias);
    description_name       the employer's full name appears in the board's posting texts.
  Verified = one strong kind (supplied link / official domain), or both name kinds. A matching job TITLE never
  counts. Anything less stays "unresolved" for you to confirm (`careeros sources verify`).
- JOB MATCHING: which posting on the board is the lead's job? Requisition id, then exact link, then title +
  location + seniority. Several candidates = "ambiguous" (reviewable), none = "no_match", and an old lead with no
  match = "posting_closed" (the board is still worth keeping).

Boundaries: discovery never opens LinkedIn / Indeed / Glassdoor / Workday / SmartRecruiters, sites the job seeker must not automate, or any
lead's original link, and never crawls websites. A link to an excluded system (e.g. Workday) becomes a manual-only
record. Every request goes through a client restricted to the four API hosts (redirects included) with a global and
per-provider budget; Retry-After is honoured. Staffing agencies are recorded as such and their clients are never
guessed. Excluded and held employers are skipped.

2026-10-13: discovery only ever produces CANDIDATES. Admission needs board verification against the employer's own
website (board_verifier: official-site handshake, quarantine, validation scan) or your expiring approval.
2026-11-02 (ADR 0003, amendment): the identity evidence above no longer grades a lead either. A posting that matches a
lead on a board discovery reads is recorded as `awaiting_board` and never ingested; the lead is linked by
lead_resolution once the board is admitted and scanned (the employer resolved by employer_identity, not by this evidence).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from careeros.core import private_sites
from careeros.core.clock import parse_utc
from careeros.normalize.posting import classify_family, contains_term, detect_country, detect_remote, norm

from .board_urls import BOARD_HOSTS, board_url, manual_system, parse_board_url
from .http import PROVIDER_HOSTS, FetchError, HttpClient, RequestBudget
from .seen_index import norm_company, norm_req

OUTCOME_RETRY_DAYS = {"slug_absent": 60, "board_empty": 14, "identity_unresolved": 30, "no_qualified_roles": 30,
                      "access_restricted": 90, "request_failed": 1, "budget_exhausted": 0, "verified": 30,
                      "manual_only": 365, "skipped_policy": 365}
_GENERIC_NAMES = {"health", "healthcare", "care", "group", "solutions", "services", "partners", "global", "united", "national",
                  "american", "clear", "simple", "one", "first", "prime", "plus", "pro", "the", "company", "consulting",
                  "systems", "technologies", "tech", "labs", "digital", "data", "medical", "pharmacy", "benefits", "insurance"}


def cfg(policy) -> dict:
    return {"enabled": False, "auto_activate": False, "slug_guessing": True, "max_slug_variants": 2, "daily_request_budget": 60,
            "per_provider_budget": 20, "curated_share": 0.2, "lookback_days": 90, "closed_after_days": 45,
            "retry_days": {}, "staffing_agencies": [], "skip_employers": [], **(policy.get("source_discovery", default={}) or {})}


def retry_days(policy, outcome: str) -> float:
    return float({**OUTCOME_RETRY_DAYS, **(cfg(policy).get("retry_days") or {})}.get(outcome, 30))


@dataclass
class Candidate:
    employer_name: str
    origin: str                                   # lead | recommendation | alert | manual | curated | backfill
    official_domain: str | None = None
    board_url: str | None = None                  # an explicit link on a board (posting or board root)
    posting_url: str | None = None
    requisition_id: str | None = None
    title: str | None = None
    location: str | None = None
    lead_ids: list = field(default_factory=list)
    origin_ref: str | None = None
    relationship: str = "employer"
    first_seen_at: str | None = None
    evidence: list = field(default_factory=list)

    @property
    def employer_norm(self) -> str:
        return norm_company(self.employer_name)


# --- building candidates ---------------------------------------------------------------------------------
def is_staffing_agency(name: str, policy) -> bool:
    text = norm(name)
    agencies = [*cfg(policy)["staffing_agencies"], *private_sites.staffing_agencies()]   # private ones from the vault
    return any(contains_term(text, norm(a)) for a in agencies) or bool(
        re.search(r"\b(staffing|recruiting|recruitment|talent solutions)\b", text))


def candidate_from_lead(lead, policy, local_urls: frozenset[str] = frozenset()) -> Candidate | None:
    """A lead (alert or recommendation) as a discovery candidate: its company, any board / domain hints in its Note,
    and the job facts needed for matching later. None when the lead names no company."""
    from careeros.discovery.alerts import is_agency_lead
    from careeros.discovery.note import context_note

    if is_agency_lead(lead, local_urls):  # an agency card or its link: its client is never guessed (CR-AD-02, CR-AD-06)
        return None
    context = lead["context"] or ""
    company = re.sub(r"\s+", " ", context.split(" · ")[0]).strip() if context else ""
    if not company or len(company) > 100 or re.search(r"[@=<>]|https?:", company):
        return None
    note = context_note(context)["fields"]
    parts = context.split(" · ")
    sender = lead["sender"] or ""
    origin = "recommendation" if sender.startswith("GitHub inbox") else "alert"
    hint = note.get("board") or (lead["url"] if parse_board_url(lead["url"] or "") or manual_system(lead["url"] or "") else None)
    candidate = Candidate(employer_name=company, origin=origin, official_domain=note.get("domain"), board_url=hint,
                          posting_url=lead["url"], requisition_id=lead_requisition(context), title=lead["title"],
                          location=parts[1] if len(parts) > 1 else None, lead_ids=[lead["lead_id"]],
                          origin_ref=lead["lead_id"], first_seen_at=lead["first_seen_at"])
    if note.get("agency") or is_staffing_agency(company, policy):
        candidate.relationship = "staffing_agency"
    return candidate


def lead_facts(store, lead_id: str, policy) -> Candidate | None:
    """One lead's own job facts (title, link, requisition, location, first seen) for matching. Leads at one employer
    share a discovery queue row and a board, never one another's facts (gap 2, 2026-11-02). None for a lead that is gone
    or names no company."""
    from careeros.discovery.alerts import local_only_urls
    row = store.one("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    return candidate_from_lead(row, policy, local_only_urls(store)) if row is not None else None


def slug_variants(name: str, limit: int = 2) -> list[str]:
    """A few distinctive full-name board names: "Foo Health, Inc." -> foohealth, foo-health. Never shortened names, and
    nothing for a name too generic to identify one company ("Health Partners", "Clear")."""
    words = norm_company(name).split()
    words = [w for w in words if w != "the"]
    if not words or (len(words) == 1 and (words[0] in _GENERIC_NAMES or len(words[0]) < 5)):
        return []
    if all(w in _GENERIC_NAMES for w in words):
        return []
    variants = ["".join(words)]
    if len(words) > 1:
        variants.append("-".join(words))
    return [v for v in variants if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", v)][:max(1, limit)]


# --- identity -----------------------------------------------------------------------------------------------
def _aliases(store, employer_norm: str) -> set[str]:
    names = {employer_norm}
    for row in store.all("SELECT alias_norm, employer_norm FROM employer_aliases WHERE alias_norm = ? OR employer_norm = ?",
                         (employer_norm, employer_norm)):
        names |= {row["alias_norm"], row["employer_norm"]}
    return {n for n in names if n}


def identity(store, candidate: Candidate, ats: str, token: str, board_name: str | None, board_text: str,
             postings: list) -> tuple[str, list[dict]]:
    """('verified' | 'unresolved', evidence) for one board. Title matches are deliberately not evidence."""
    evidence = []
    if candidate.board_url and parse_board_url(candidate.board_url) == (ats, token):
        evidence.append({"kind": "supplied_board_link", "detail": candidate.board_url[:200]})
    domain = (candidate.official_domain or "").lower().removeprefix("www.")
    if domain:
        texts = [board_text] + [f"{p.description_text} {p.apply_url or ''} {p.url or ''}" for p in postings[:40]]
        if any(domain in (t or "").lower() for t in texts):
            evidence.append({"kind": "official_domain", "detail": domain})
    names = _aliases(store, candidate.employer_norm)
    if board_name and norm_company(board_name) in names:
        evidence.append({"kind": "board_name", "detail": board_name[:100]})
    full = [n for n in names if len(n.split()) >= 2 or len(n) >= 8]
    mentions = sum(1 for p in postings[:40] if any(contains_term(norm(p.description_text), n) for n in full))
    if mentions >= 2:
        evidence.append({"kind": "description_name", "detail": f"named in {mentions} posting texts"})
    kinds = {e["kind"] for e in evidence}
    verified = bool(kinds & {"supplied_board_link", "official_domain"}) or {"board_name", "description_name"} <= kinds
    return ("verified" if verified else "unresolved"), evidence


def relevant_roles(postings: list, policy) -> int:
    from careeros.normalize.posting import relevant_role
    families, count = policy.get("families", default={}), 0
    for p in postings:
        title = norm(p.title)
        remote = detect_remote(p.location, p.workplace_hint, p.description_text)
        if relevant_role(title, classify_family(title, families), remote, detect_country(p.location, p.description_text, remote), policy):
            count += 1
    return count


# --- job matching (separate from identity) ---------------------------------------------------------------------
@dataclass(frozen=True)
class TrackedPosting:
    """A job CareerOS tracks, as the lead matcher reads it (lead_resolution, 2026-11-02). `key` is its job id, and is what
    a match names; a posting read from a board during discovery is named by its own id on that board."""
    key: str
    title: str
    url: str | None
    requisition_id: str | None
    external_id: str | None
    location: str
    remote_type: str
    employer: str | None = None   # its employer's own name and parent: a lead naming another company is never this
    parent: str | None = None     # posting (RT-24)


def _key(p) -> str:
    return getattr(p, "key", None) or p.external_id


def _remote(p) -> str:
    known = getattr(p, "remote_type", None)
    return known or detect_remote(p.location, getattr(p, "workplace_hint", None), getattr(p, "description_text", None) or "")


def _same_link(a: str | None, b: str | None) -> bool:
    from careeros.normalize.posting import canonical_url
    if not (a and b):
        return False
    return a.rstrip("/") == b.rstrip("/") or (canonical_url(a) is not None and canonical_url(a) == canonical_url(b))


# Filler a near-identical title may differ by (2026-11-02, the owner's decision D68: "filler such as remote, I/II aside"):
# where the work is done, and a level written as I-IV or 1-4. Everything else in the title must be the same words.
_FILLER_PHRASES = re.compile(r"\b(work from home|wfh|remote|hybrid|on site|onsite|in office|virtual|telecommute|telework|"
                             r"nationwide|anywhere|united states|usa|us|level (?:i{1,3}|iv|[1-4]))\b")
_LEVEL = {"i", "ii", "iii", "iv", "1", "2", "3", "4"}


def near_title(title: str | None) -> str:
    """The title with its filler removed ("Claims Analyst II - Remote (US)" -> "claims analyst")."""
    words = _FILLER_PHRASES.sub(" ", norm(title or "")).split()
    return " ".join(w for w in words if w not in _LEVEL)


_ARRANGEMENTS = (("work from home", "remote"), ("wfh", "remote"), ("telecommute", "remote"), ("telework", "remote"),
                 ("virtual", "remote"), ("remote", "remote"), ("hybrid", "hybrid"), ("on site", "onsite"), ("onsite", "onsite"),
                 ("in office", "onsite"))


def _title_marks(title: str | None) -> tuple[set[str], set[str]]:
    """(levels, work arrangements) a title states: "Claims Analyst III - Onsite" -> ({"iii"}, {"onsite"})."""
    words = norm(title or "")
    levels = {w for w in words.split() if w in _LEVEL}
    arrangements = {kind for phrase, kind in _ARRANGEMENTS if re.search(rf"\b{phrase}\b", words)}
    return levels, arrangements


def titles_agree(a: str | None, b: str | None) -> bool:
    """Whether two titles name the same opening for an automatic link: the same words with filler aside (near_title),
    and no stated level or work arrangement that differs; one side not stating it never conflicts (the owner's D181:
    "Claims Analyst I (Remote)" is not "Claims Analyst III - Onsite"). The likely-match list (D68) keeps near_title."""
    if near_title(a) != near_title(b):
        return False
    (levels_a, ways_a), (levels_b, ways_b) = _title_marks(a), _title_marks(b)
    return not ((levels_a and levels_b and levels_a != levels_b) or (ways_a and ways_b and ways_a != ways_b))


# the query keys that carry one posting's id (exact names: "jobCategoryId", "requestId" or "postingDate" are none): kept
# in a link's reading (alerts._reading), so two openings of one page stay two leads; they never name a posting by
# themselves (names_one_posting)
_ID_KEYS = {"jk", "jl", "gh_jid", "jobid", "job_id", "job", "jid", "currentjobid", "reqid", "req_id", "req", "requisitionid",
            "requisition_id", "requisition", "postingid", "posting_id", "posting", "openingid", "opening_id", "opening",
            "positionid", "position_id", "vacancyid", "vacancy_id"}


_DATE = re.compile(r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")


def _job_number(text: str) -> bool:
    """Whether `text` carries a job number: a run of four digits or more that is no year (1900-2099) and no date
    (20261118)."""
    return any(not re.fullmatch(r"(?:19|20)\d\d", run) and not _DATE.fullmatch(run) for run in re.findall(r"\d{4,}", text or ""))


# the job sites whose own job id names one posting, by their own host or a subdomain of it ("ca.indeed.com"; never
# "linkedin.com.example.net"): alerts reads the id (_ID_FIELDS, "/jobs/view/N")
_JOB_SITES = (("linkedin.com", "linkedin"), ("indeed.com", "indeed"), ("glassdoor.com", "glassdoor"))


def _workday_posting(url: str) -> bool:
    """A Workday posting link: a host under myworkdayjobs.com, a "job" route, and a requisition ending the path whose
    number is no year and no date ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Claims-Analyst_R-12345";
    never ".../job/archive_2026"), with no "#" part."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host.endswith(".myworkdayjobs.com") or parts.fragment:
        return False
    segments = [s.lower() for s in parts.path.split("/") if s]
    match = re.search(r"_[a-z]{0,4}-?(\d{4,})$", segments[-1]) if len(segments) > 1 else None
    return match is not None and "job" in segments[:-1] and _job_number(match.group(1))


def _job_site_posting(url: str) -> bool:
    """A job site's link to one job by its own id ("https://www.linkedin.com/jobs/view/4000000333",
    "https://www.indeed.com/viewjob?jk=abc123def456"), on that site's own host."""
    from careeros.discovery.alerts import canonical_job_url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    site = next((name for domain, name in _JOB_SITES if host == domain or host.endswith("." + domain)), None)
    # by an id in its path or query only: one after "#" is a page's state, and the link kept for the posting drops it,
    # so every job shown on that page would share it (the red team before Codex Pass 15, RT-07)
    reading = canonical_job_url(parts._replace(fragment="").geturl()) if site else None
    return reading is not None and reading[1] == site and reading[2]


BOARD_ATS = ("greenhouse", "lever", "ashby", "workable")


def _workable_short(url: str) -> str | None:
    """The posting a Workable short link names ("https://apply.workable.com/j/ABC123DEF4"), or None."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if (parts.hostname or "").lower() == "apply.workable.com" and len(segments) == 2 and segments[0] == "j" \
            and not parts.fragment:
        return segments[1].lower()
    return None


def posting_identity(url: str | None) -> tuple[str, str] | None:
    """(kind, id) of the one posting a link names by itself (names_one_posting), or None: ("greenhouse", "4400001"),
    ("workable", "abc123def4"), ("workday", "<host>/<location>/<title>_r-20412": the path after "job"), ("linkedin", "<its job link>"). A board's posting id is
    the board's own (Greenhouse's, Lever's, Ashby's and Workable's ids name one posting whatever board name the link
    carries), so a board-scanned job and a link to it agree (job_identity). Two links of one kind with different ids
    are two postings (the red team before Codex Pass 15, RT-19)."""
    if not url or not names_one_posting(url):
        return None
    url = str(url)
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return None   # a javascript:// or ftp:// form of a board's link names no posting (the hand check's HC3)
    from careeros.discovery.boards import parse_job_url
    parsed = parse_job_url(url)
    if parsed:
        return parsed[0], parsed[2].lower()
    short = _workable_short(url)
    if short:
        return "workable", short
    parts = urlsplit(url)
    if _workday_posting(url):
        # the tenant, then the path after "job": its location and its title with the requisition. One requisition under
        # two location segments is two postings (the red team's rtw_fr1upgrade_7; the coordinator's go, 2026-11-19): an
        # exact link only ever splits; the locale and the career site's name are left out (one posting's link forms)
        segments = [s.lower() for s in parts.path.split("/") if s]
        after = segments[segments.index("job") + 1:] if "job" in segments else segments[-1:]
        return "workday", f"{(parts.hostname or '').lower()}/{'/'.join(after)}"
    from careeros.discovery.alerts import canonical_job_url
    reading = canonical_job_url(parts._replace(fragment="").geturl())
    exact_id = re.search(r"/jobs/view/(?:[^/]*-)?[0-9]{6,}/?$", parts.path, re.I)
    if reading and reading[1] == "linkedin" and "/jobs/view/" in parts.path.lower() and not exact_id:
        return None   # more after the id's digits ("/jobs/view/4000000001x") names no posting (the hand check's HC3)
    return (reading[1], reading[0]) if reading else None


def job_identity(job) -> tuple[str, str] | None:
    """The one posting a stored job (or a posting read from a board) is, by the owner's D188 (2026-11-19, "Exact links
    only"): a board-scanned job's own board id ((ats, external id): never the link its board's API stores, which can be
    an employer's page with ?gh_jid= or a short link, the red team's fr1-boards-0), else the posting its own link names
    (posting_identity). None when nothing names one posting: such a job is one job with no other, whatever its text,
    title, requisition or place say. `job` is a jobs row, a dict, a RawPosting or a TrackedPosting."""
    def get(name):
        if isinstance(job, dict) or hasattr(job, "keys"):
            return job[name] if name in job.keys() else None
        return getattr(job, name, None)
    ats = get("ats") or (str(get("key") or get("job_id") or "").split(":", 1)[0] or None)
    external = get("external_id")
    if ats in BOARD_ATS and external:
        return ats, str(external).lower()
    return posting_identity(get("url"))


def jobs_with_identity(store, identity: tuple | None, *, exclude: str | None = None, where: str = "1 = 1",
                       params: tuple = ()) -> list:
    """Stored jobs (whole rows) that are the posting `identity` names (job_identity), oldest first."""
    if identity is None:
        return []
    found = re.findall(r"[A-Za-z0-9-]+", identity[1])
    token = (found[-1] if found else identity[1]).lower()
    marks = ",".join("?" * len(BOARD_ATS))
    rows = store.all(f"SELECT * FROM jobs WHERE job_id != ? AND ({where}) AND ((ats IN ({marks}) AND lower(external_id) = ?) "
                     f"OR lower(url) LIKE ?) ORDER BY first_seen_at, job_id",
                     (exclude or "", *params, *BOARD_ATS, token, f"%{token}%"))
    return [r for r in rows if job_identity(r) == identity]


def same_posting(a, b) -> bool:
    """Whether two jobs (or postings) are one posting by D188: both have an identity, and it is the same."""
    mine = job_identity(a)
    return mine is not None and mine == job_identity(b)


def names_one_posting(url: str | None) -> bool:
    """Whether a link names one posting by itself. Only a supported board's posting link does (Greenhouse, Lever, Ashby
    and Workable as boards.parse_job_url reads them, and a Workday posting link), or a job site's link to one job by its
    own id (LinkedIn, Indeed, Glassdoor). Any other link is treated as a page several openings share, an employer's
    careers page above all, whatever its path, query or "#" route says ("/jobs/4400001", "?jobId=10123",
    "#/opening/R-1101", "/jobs/archive_2026", "/join"): nothing is merged, linked or made a duplicate there by the link
    alone (the owner's D184, after Codex CR-2026-11-15-GATE-FIXES H27, H31 and H36, Passes 12 to 14, which each found a
    careers-page link pattern read as one posting)."""
    from careeros.discovery.alerts import kept_for_review
    from careeros.discovery.boards import parse_job_url
    if not url or kept_for_review(url):
        return False   # a search or listing page a job site's link names (the red team after Codex Pass 12, RT3-1)
    return bool(parse_job_url(url)) or bool(_workable_short(url)) or _workday_posting(url) or _job_site_posting(url)


def lead_requisition(context: str | None) -> str | None:
    """The requisition a lead's line states: its Note's req= field, or written in its own words ("Job ID: R-8102",
    "Req #R-8102"; the red team after Codex Pass 11)."""
    from careeros.discovery.manual_intake import requisition_in
    from careeros.discovery.note import context_note
    return context_note(context)["fields"].get("req") or requisition_in(context)


def _place_words(location: str) -> set[str]:
    return {w for w in norm(location).split() if len(w) >= 3 and w not in ("remote", "hybrid", "onsite", "united", "states", "usa")}


def places_compatible(lead: str | None, lead_remote: str, place: str | None, place_remote: str) -> bool:
    """Whether two location fields can be one opening's (location_compatible). An unstated place is compatible with any.
    Not compatible: remote and on-site or hybrid; two countries that differ, or a named place that cannot be read as the
    other's country ("Calgary, AB" beside "Remote - US"); two places with states in common only by state ("Dallas, TX"
    and "Austin, TX"), or with no state or city word in common. The red team before Codex Pass 15, RT-20 and RT-21: a
    remote side no longer makes any place compatible."""
    from careeros.normalize.posting import _states_in
    lead, place = lead or "", place or ""
    if {lead_remote, place_remote} in ({"remote", "onsite"}, {"remote", "hybrid"}):
        return False
    words, other = _place_words(lead), _place_words(place)
    lead_states, place_states = _states_in(lead), _states_in(place)
    named, named_other = bool(words or lead_states), bool(other or place_states)
    a, b = _match_country(lead, lead_remote, lead_states), _match_country(place, place_remote, place_states)
    if a != b and not (a == "unknown" and not named) and not (b == "unknown" and not named_other):
        return False
    if not (named and named_other):
        return True
    if lead_states and place_states and not lead_states & place_states:
        return False
    from careeros.normalize.posting import US_STATES
    codes = lead_states | place_states
    states = {w for name, code in US_STATES.items() if code in codes for w in norm(name).split()}
    cities, other_cities = _city_words(lead, states), _city_words(place, states)
    return not (cities and other_cities) or bool(cities & other_cities)


def _city_words(location: str, state_words: set) -> set[str]:
    """The words of the places' cities: before the first comma of each place ("Albany, NY" -> albany), else the place's
    words other than a state's (a city named like its state is never another city there by the state's name)."""
    out: set[str] = set()
    for place in re.split(r"[;|]", location or ""):
        head, comma, _ = place.partition(",")
        out |= _place_words(head) if comma else (_place_words(place) - state_words)
    return out


def _match_country(location: str, remote: str, states: set) -> str:
    """The country a location reads as for matching: its placement (detect_country), and a place naming a US state and no
    place abroad ("Chicago, IL", "Denver, CO") as the US: what places a posting stays strict (D180), but a line in a US
    city is not another country's (a place naming no state, such as "Calgary, AB", stays unknown)."""
    from careeros.normalize.posting import NON_US
    country = detect_country(location, "", remote)
    if country == "unknown" and states and not any(contains_term(norm(location), c) for c in NON_US):
        return "US"
    return country


def location_compatible(lead_location: str | None, posting) -> bool:
    """Whether a lead's location (as an alert showed it) can be the posting's (places_compatible)."""
    lead = "" if "=" in (lead_location or "") else (lead_location or "")   # a Note field in that place is no location
    return places_compatible(lead, detect_remote(lead, None, ""), posting.location or "", _remote(posting))


def match_lead(candidate: Candidate, postings: list, policy, now: str) -> dict:
    """Which of `postings` is the lead's job (`candidate` holds that one lead's facts). By the owner's D188 (2026-11-19,
    "Exact links only") only one step links a lead by itself:
      canonical_link  the lead's own link names one posting, and a posting is that posting (job_identity): matched.
    Every other step is a suggestion you confirm or reject, never a link (likely), or several (ambiguous):
      requisition     the same requisition id, a place that can be the lead's and the same title;
      title_location  the same title, the same seniority, a compatible location, and no different known requisition;
      likely_title    a near-identical title (filler such as remote or I/II aside) with the same seniority, a compatible
                      location and no different known requisition.
    none = no_match, or posting_closed for a lead older than closed_after_days. Postings may be a board's (discovery) or
    tracked jobs (TrackedPosting)."""
    from careeros.discovery.alerts import kept_for_review
    from careeros.normalize.posting import seniority
    if candidate.posting_url and kept_for_review(candidate.posting_url):
        # A lead kept for review (no single job ID) is never matched automatically, not even by its byte-identical URL:
        # only you dismiss it (CR-2026-11-09-INTAKE-HOTFIX Pass 6, CR-HF-01, the owner's D140 F2), checked first
        return {"decision": "no_match", "method": "review_link", "posting": None, "candidates": []}
    # A posting tracked at a review link is never a candidate, by any strategy (requisition, link, title, location): the
    # exclusion comes before the candidate set is built (the owner's D142 R-b, Codex Pass 7)
    postings = [p for p in postings if not (p.url and kept_for_review(p.url))]
    # a lead naming one company is never another company's posting, a sister company under one parent included (RT-24)
    own = posting_identity(candidate.posting_url)
    from careeros.discovery.seen_index import norm_company
    company = norm_company(candidate.employer_name)
    postings = [p for p in postings
                if not (company and (names := {norm_company(getattr(p, "employer", None)),
                                               norm_company(getattr(p, "parent", None))} - {""}) and company not in names)]
    if own is not None:
        # the one automatic link (D188): the lead's own link names the posting, and so does the posting's identity (a
        # board-scanned job by its own board id, whatever link its board's API stores: the red team's fr1-boards-1)
        hits = [p for p in postings if job_identity(p) == own]
        if len(hits) == 1:
            return {"decision": "matched", "method": "canonical_link", "posting": hits[0], "candidates": [_key(hits[0])]}
    # a lead whose own link names one posting is never another posting of the same kind (another Greenhouse job, another
    # Workday requisition, another LinkedIn id) by any suggestion (the red team before Codex Pass 15, RT-19), read by the
    # posting's own identity (its board id: the red team's fr1-boards-1)
    postings = [p for p in postings if not (own and (theirs := job_identity(p)) and theirs[0] == own[0] and theirs != own)]
    req = norm_req(candidate.requisition_id)
    title = norm(candidate.title or "")
    terms = policy.get("seniority_terms", default={})
    level = seniority(title, terms)
    if req:
        # the same requisition, a place that can be the lead's and the same title (filler aside) at the same seniority:
        # a suggestion only (RT-22; D188)
        hits = [p for p in postings if (norm_req(p.requisition_id) == req or norm_req(p.external_id) == req)
                and location_compatible(candidate.location, p) and seniority(norm(p.title), terms) == level
                and (not title or near_title(p.title) == near_title(candidate.title))]
        if len(hits) == 1:
            return {"decision": "likely", "method": "requisition", "posting": hits[0], "candidates": [_key(hits[0])]}
        if hits:
            return {"decision": "ambiguous", "method": "requisition", "posting": None, "candidates": [_key(p) for p in hits][:10]}
    # a different known requisition is a different vacancy; a place that cannot be the lead's is another posting
    fits = [p for p in postings if not (req and norm_req(p.requisition_id) and norm_req(p.requisition_id) != req)
            and seniority(norm(p.title), terms) == level and location_compatible(candidate.location, p)]
    same = [p for p in fits if title and norm(p.title) == title]
    if len(same) == 1:   # a suggestion you confirm, never a link by itself (D188)
        return {"decision": "likely", "method": "title_location", "posting": same[0], "candidates": [_key(same[0])]}
    if same:
        return {"decision": "ambiguous", "method": "title_location", "posting": None, "candidates": [_key(p) for p in same][:10]}
    near = near_title(candidate.title)
    if len(near.split()) >= 2:
        likely = [p for p in fits if near_title(p.title) == near]
        if len(likely) == 1:
            return {"decision": "likely", "method": "likely_title", "posting": likely[0], "candidates": [_key(likely[0])]}
        if likely:
            return {"decision": "ambiguous", "method": "likely_title", "posting": None, "candidates": [_key(p) for p in likely][:10]}
    old = candidate.first_seen_at and (parse_utc(now) - parse_utc(candidate.first_seen_at)) / timedelta(days=1) > cfg(policy)["closed_after_days"]
    return {"decision": "posting_closed" if old else "no_match", "method": "none", "posting": None, "candidates": []}


# --- running one candidate ---------------------------------------------------------------------------------------
def _recent(store, employer_norm: str, ats: str | None, slug: str | None, policy, now: str):
    row = store.one("SELECT * FROM discovery_attempts WHERE employer_norm = ? AND COALESCE(ats,'') = ? AND COALESCE(slug,'') = ? "
                    "ORDER BY attempted_at DESC, rowid DESC LIMIT 1", (employer_norm, ats or "", slug or ""))
    if not row:
        return None
    if row["retry_after"]:  # the server said when to come back: that replaces the outcome's default window
        return row if row["retry_after"] > now else None
    age = (parse_utc(now) - parse_utc(row["attempted_at"])) / timedelta(days=1)
    return row if age < retry_days(policy, row["outcome"]) else None


def _attempt(store, candidate: Candidate, outcome: str, *, ats=None, slug=None, detail=None, requests=0, retry_after=None):
    store.writer("discovery").insert("discovery_attempts", {
        "attempt_id": f"att:{uuid.uuid4().hex[:16]}", "employer_norm": candidate.employer_norm, "ats": ats, "slug": slug,
        "outcome": outcome, "detail": (detail or "")[:300] or None, "requests": requests, "origin": candidate.origin,
        "attempted_at": store.clock.now(), "retry_after": retry_after})


def _upsert_mapping(store, candidate: Candidate, ats: str, token: str, *, collection: str, status: str, evidence: list,
                    relevant: int | None, verified_method: str | None) -> str:
    mapping_id = f"{ats}:{token}"
    now = store.clock.now()
    existing = store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,))
    writer = store.writer("discovery")
    from . import board_registry as registry
    # a lead's domain= is only a hint: it never merges employer records (ADR 0001, amendment of 2026-11-02)
    entity_id = registry.ensure_entity_for(store, candidate.employer_name, domain=candidate.official_domain, origin=candidate.origin)
    if existing:
        merged = {json.dumps(e, sort_keys=True) for e in json.loads(existing["identity_evidence"] or "[]")} | \
                 {json.dumps(e, sort_keys=True) for e in evidence}
        keep_verified = existing["identity_status"] == "verified"  # never downgrade a verified mapping on weaker news
        values = {"identity_evidence": json.dumps([json.loads(e) for e in sorted(merged)]), "relevant_roles": relevant,
                  "updated_at": now}
        if not keep_verified and status == "verified":
            values.update(identity_status="verified", verified_by="careeros", verified_method=verified_method, verified_at=now)
        if candidate.official_domain and not existing["official_domain"]:
            values["official_domain"] = candidate.official_domain
        if not existing["entity_id"]:
            values["entity_id"] = entity_id
        writer.update("board_mappings", values, "mapping_id = ?", (mapping_id,))
        return mapping_id
    writer.insert("board_mappings", {
        "mapping_id": mapping_id, "employer_norm": candidate.employer_norm, "employer_name": candidate.employer_name[:100],
        "official_domain": candidate.official_domain, "ats": ats, "board_token": token, "board_url": board_url(ats, token),
        "collection": collection, "identity_status": status, "identity_evidence": json.dumps(evidence),
        "verified_by": "careeros" if status == "verified" else None, "verified_method": verified_method if status == "verified" else None,
        "verified_at": now if status == "verified" else None, "relationship": candidate.relationship, "origin": candidate.origin,
        "origin_ref": candidate.origin_ref, "relevant_roles": relevant, "activated_at": None, "created_at": now, "updated_at": now,
        "entity_id": entity_id, "provenance": "discovery",
        "verification_status": "manual_only" if collection == "manual_only" else "candidate"})
    return mapping_id


def _fetch_board(client: HttpClient, ats: str, token: str) -> tuple[list, str | None, str]:
    """(postings, board display name, board text) using only the documented public listing endpoints."""
    from .boards import FETCHERS, _token
    name, text = None, ""
    if ats == "greenhouse":
        meta = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_token(token)}")
        name, text = (meta or {}).get("name"), (meta or {}).get("content") or ""
    if ats == "workable":
        data = client.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{_token(token)}?details=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FetchError("unexpected_shape")
        from .boards import workable_posting
        postings = [workable_posting(token, item) for item in data["jobs"] if item.get("shortcode")]
        return postings, data.get("name"), f"{data.get('description') or ''} {data.get('url') or data.get('website') or ''}"
    return FETCHERS[ats](client, token), name, text


def discovery_client(policy, budget: RequestBudget, transport=None, sleep=None) -> HttpClient:
    kwargs = {"transport": transport} if transport else {}
    if sleep:
        kwargs["sleep"] = sleep
    return HttpClient(min_interval=0.6, allowed_hosts=set(PROVIDER_HOSTS), budget=budget, **kwargs)


def discover(ctx, candidate: Candidate, client: HttpClient, *, now: str | None = None) -> dict:
    """Run discovery for one candidate. Returns {'outcome', 'mappings': [...], 'requests', 'matches': n}."""
    from careeros.discovery import sources
    from careeros.qualify.gate import excluded_employer
    store, policy = ctx.store, ctx.policy
    now = now or store.clock.now()
    start = client.requests
    result = {"outcome": None, "mappings": [], "requests": 0, "matches": 0}

    skipped = [*(policy.get("gate", "held_parents", default=[]) or []), *cfg(policy)["skip_employers"]]
    held = {norm_company(p) for p in skipped}   # held, or employers whose sites stay manual (source_discovery.skip_employers)
    if excluded_employer({"employer_name": candidate.employer_name}, policy) or candidate.employer_norm in held:
        _attempt(store, candidate, "skipped_policy", detail="excluded, held or skipped employer")
        result["outcome"] = "skipped_policy"
        return result
    if candidate.relationship == "staffing_agency":
        _attempt(store, candidate, "skipped_policy", detail="staffing agency: the hiring client is not disclosed, never guessed")
        result["outcome"] = "skipped_policy"
        return result

    manual = manual_system(candidate.board_url or "")
    if manual:  # Workday / SmartRecruiters / ... : a useful manual-source record, never automatic collection
        host = urlsplit(candidate.board_url).netloc.lower()
        mapping = _upsert_mapping(store, candidate, manual, host, collection="manual_only", status="unresolved",
                                  evidence=[{"kind": "supplied_board_link", "detail": candidate.board_url[:200]}],
                                  relevant=None, verified_method=None)
        _attempt(store, candidate, "manual_only", ats=manual, slug=host, detail="excluded system: open it yourself")
        result.update(outcome="manual_only", mappings=[mapping])
        return result

    explicit = parse_board_url(candidate.board_url or "")
    if explicit:
        targets = [explicit]
    elif cfg(policy)["slug_guessing"]:
        targets = [(ats, slug) for slug in slug_variants(candidate.employer_name, int(cfg(policy)["max_slug_variants"]))
                   for ats in BOARD_HOSTS if sources.status(ats) == "allowed"]
        if not targets:
            _attempt(store, candidate, "skipped_policy", detail="name too generic to guess a board safely")
            result["outcome"] = "skipped_policy"
            return result
    else:
        targets = []
    outcomes = []
    for ats, token in targets:
        if sources.status(ats) != "allowed":
            if explicit:
                _attempt(store, candidate, "skipped_policy", ats=ats, slug=token, detail=f"{ats} is not read automatically")
            continue
        if _recent(store, candidate.employer_norm, ats, token, policy, now):
            outcomes.append("cached")
            continue
        before = client.requests
        try:
            postings, name, text = _fetch_board(client, ats, token)
        except FetchError as exc:
            if exc.code == "budget_exhausted":
                _attempt(store, candidate, "budget_exhausted", ats=ats, slug=token, requests=client.requests - before)
                outcomes.append("budget_exhausted")
                break
            outcome = {"not_found": "slug_absent", "access_restricted": "access_restricted"}.get(exc.code, "request_failed")
            retry_at = None
            if exc.retry_after:
                retry_at = (parse_utc(now) + timedelta(seconds=exc.retry_after)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            _attempt(store, candidate, outcome, ats=ats, slug=token, detail=exc.code, requests=client.requests - before,
                     retry_after=retry_at)
            outcomes.append(outcome)
            continue
        if not postings:
            _attempt(store, candidate, "board_empty", ats=ats, slug=token, requests=client.requests - before)
            outcomes.append("board_empty")
            continue
        status, evidence = identity(store, candidate, ats, token, name, text, postings)
        relevant = relevant_roles(postings, policy)
        method = ",".join(sorted({e["kind"] for e in evidence})) or None
        mapping = _upsert_mapping(store, candidate, ats, token, collection="allowed", status=status, evidence=evidence,
                                  relevant=relevant, verified_method=method)
        result["mappings"].append(mapping)
        verified = store.one("SELECT identity_status FROM board_mappings WHERE mapping_id = ?", (mapping,))["identity_status"] == "verified"
        outcome = ("verified" if relevant else "no_qualified_roles") if verified else "identity_unresolved"
        _attempt(store, candidate, outcome, ats=ats, slug=token, requests=client.requests - before,
                 detail=f"{len(postings)} postings, {relevant} relevant; evidence: {method or 'none'}")
        outcomes.append(outcome)
        for lead_id in candidate.lead_ids:   # each lead with its own job facts, never the group's (gap 2, 2026-11-02)
            facts = lead_facts(store, lead_id, policy)
            if facts is not None:
                result["matches"] += _record_match(ctx, facts, lead_id, mapping, postings, now)
        # a candidate only: admission to the daily scan is board_verifier's decision (the employer's own site first)
        if verified:
            break  # one verified board per employer is enough
    order = ["verified", "no_qualified_roles", "identity_unresolved", "manual_only", "board_empty", "access_restricted",
             "request_failed", "budget_exhausted", "slug_absent", "cached"]
    result["outcome"] = next((o for o in order if o in outcomes), "skipped_policy")   # nothing probed is never "absent" (F5)
    result["requests"] = client.requests - start
    return result


def probed_absent(store, url: str | None):
    """The last probe of exactly the board this link names, when it found no such board: (canonical board address,
    when it was probed); None when the link names no allowed board, or that board was never probed, or it was found.
    A guessed name, a blocked system or an unrecognised site never counts (Codex CR-2026-11-15-GATE-FIXES F5)."""
    target = parse_board_url(url or "")
    if not target or not _SAFE_TOKEN.fullmatch(target[1]):   # a decoded "?" or "&" in a token is never printed (Pass 2)
        return None
    last = store.one("SELECT outcome, attempted_at FROM discovery_attempts WHERE ats = ? AND lower(slug) = lower(?) "
                     "ORDER BY attempted_at DESC LIMIT 1", target)
    if not last or last["outcome"] != "slug_absent":
        return None
    from .board_registry import mapping_for_board
    found = mapping_for_board(store, *target)
    if found and (found["verification_status"] in ("active", "quarantined")
                  or max(found["verified_at"] or "", found["activated_at"] or "") > last["attempted_at"]):
        return None   # verified since the probe found nothing: the old 404 no longer stands (Codex Pass 2, F5)
    return board_url(*target), last["attempted_at"]


_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def readable_board(url: str | None) -> bool:
    """A link discovery would probe: a board on a system read automatically, named by a plain board name (letters, digits,
    ".", "_", "-"). The same checks probed_absent applies, so a link accepted here can later be shown missing and
    corrected (Codex CR-2026-11-15-GATE-FIXES F5, Pass 3)."""
    from careeros.discovery import sources
    target = parse_board_url(url or "")
    return bool(target and _SAFE_TOKEN.fullmatch(target[1]) and sources.status(target[0]) == "allowed")


def absent_suggestions(store) -> list[tuple[str, str, str]]:
    """(employer, board address, when probed) for each suggested board a probe found does not exist (careeros sources list)."""
    return [(row["employer_name"], *found) for row in
            store.all("SELECT employer_name, board_url FROM discovery_queue WHERE board_url IS NOT NULL ORDER BY employer_name")
            if (found := probed_absent(store, row["board_url"]))]


def _record_match(ctx, facts: Candidate, lead_id: str, mapping_id: str, postings: list, now: str) -> int:
    """Job matching, kept apart from identity. The board discovery just read is a candidate, never admitted (ADR 0003), so
    a match here never grades the lead or ingests its posting, whatever the board's identity evidence (ADR 0003,
    amendment of 2026-11-02): it is recorded as `awaiting_board`, and the lead is linked by lead_resolution once the
    board is admitted and scanned. Several candidates stay ambiguous; none is no_match (posting_closed for an old lead).
    The lead's own state is left as it is: only an admitted board's scan changes it. Returns 1 for a match found."""
    match = match_lead(facts, postings, ctx.policy, now)
    found = match["decision"] in ("matched", "likely")   # a likely match is confirmed by you once the board is admitted
    decision = "awaiting_board" if found else match["decision"]
    reason = ("a posting on this candidate board matches; the lead is linked once the board is admitted and scanned" if found
              else None)
    ctx.store.writer("discovery").insert("lead_matches", {
        "match_id": f"lm:{uuid.uuid4().hex[:16]}", "lead_id": lead_id, "job_id": None, "mapping_id": mapping_id,
        "method": match["method"], "decision": decision, "candidates_json": json.dumps(match["candidates"]), "decided_at": now,
        "audit": None, "audited_at": None, "actor": "careeros", "reason": reason})
    return int(found)


def activate(store, mapping_id: str, *, actor: str = "user") -> str:
    """Put a board into the daily scan. Only a board that passed verification against the employer's own site AND its
    validation scan (or one you approved, after its validation scan) can be activated; manual-only systems never."""
    from . import board_registry as registry
    mapping = registry.mapping(store, mapping_id)
    if not mapping:
        raise ValueError("unknown_mapping")
    if mapping["collection"] != "allowed":
        raise ValueError("manual_only: this board is on a system CareerOS never reads automatically")
    if not (mapping["verification_status"] == "active" or
            (mapping["verification_status"] == "quarantined" and mapping["validation_status"] == "passed")):
        raise ValueError(f"not verified ({mapping['verification_status']}): the employer's own site must link to this board "
                         f"(careeros sources check {mapping_id}) or you approve it (careeros sources approve {mapping_id} --reason ...)")
    with store.tx():   # active and admitted (which completes a migration to it) together (Codex CR-2026-11-15-GATE-FIXES F4)
        if mapping["verification_status"] == "quarantined":
            registry.update_board(store, mapping_id, {"verification_status": "active"}, component="admin")
        return registry.admit(store, mapping_id, via="override" if mapping["override_reason"] else "verified", actor=actor)


def verify(store, mapping_id: str, *, domain: str | None = None, note: str | None = None, actor: str = "user",
           days: float = 90) -> str:
    """A person vouches for a board: an expiring manual approval (board_registry.approve)."""
    from . import board_registry as registry
    return registry.approve(store, mapping_id, reason=note or f"confirmed by {actor}", days=days, actor=actor, domain=domain)


def reject(store, mapping_id: str, *, note: str | None = None, actor: str = "user") -> None:
    mapping = store.one("SELECT * FROM board_mappings WHERE mapping_id = ?", (mapping_id,))
    if not mapping:
        raise ValueError("unknown_mapping")
    evidence = json.loads(mapping["identity_evidence"] or "[]") + [{"kind": "human_rejected", "detail": (note or actor)[:200]}]
    store.writer("admin").update("board_mappings", {"identity_status": "rejected", "identity_evidence": json.dumps(evidence),
                                                    "verification_status": "rejected", "verification_failure": "known_unrelated",
                                                    "paused_reason": f"rejected by {actor}", "updated_at": store.clock.now()},
                                 "mapping_id = ?", (mapping_id,))


def add_alias(store, alias: str, employer: str, relation: str = "alias", *, actor: str = "user", evidence: str | None = None) -> None:
    if relation not in ("alias", "parent", "subsidiary", "former", "legal"):
        raise ValueError("relation must be alias, parent, subsidiary, former or legal")
    row = store.one("SELECT entity_id FROM employer_entities WHERE canonical_norm = ? ORDER BY created_at LIMIT 1", (norm_company(employer),))
    store.writer("admin").insert("employer_aliases", {"alias_norm": norm_company(alias), "employer_norm": norm_company(employer),
                                                      "relation": relation, "verified_by": actor, "verified_at": store.clock.now(),
                                                      "evidence": (evidence or "")[:200] or None,
                                                      "entity_id": row["entity_id"] if row else None})
