"""Low-friction intake for postings you opened yourself (2026-10-12, Part 6).

Paste the posting text (from LinkedIn, Indeed, Workday, an email, anywhere) and its direct application URL. CareerOS:
- extracts what the text states (title, company, location, requisition id, pay, deadline) and nothing else;
- deduplicates against what it already tracks (same link, same requisition at the same company, same title there);
- grades it like any posting, labelled as human-supplied evidence (`human_text`), apart from live ATS data;
- records a manual-source entry when the link is an excluded system (e.g. the employer's Workday board);
- NEVER fetches the URL or any other site: this module takes no HTTP client at all.
Pasted text is untrusted data: it is read for job facts, never followed as instructions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from careeros.core.fingerprint import text_fingerprint
from careeros.normalize.posting import RawPosting, canonical_url, norm, posting_link
from careeros.normalize.sanitize import canonical, canonical_cut, clean_text

from .board_urls import manual_system

MAX_TEXT = 60000
# a label and its value on one physical line: a blank "Location:" never takes the next line (Codex
# CR-2026-11-15-GATE-FIXES H3, Pass 6: "\s*" crossed the line break, so prose became the location field)
# ...and its separator is a colon, or a dash with a space on each side: "Location-based pay: ..." and "Company-wide
# bonus" are prose, never labels (the red team before Codex Pass 11, D179)
_LABEL = lambda name: re.compile(rf"^[^\S\n]*(?:{name})(?:[^\S\n]*:|[^\S\n]+[\-–](?=[^\S\n]))[^\S\n]*(\S.*?)[^\S\n]*$",
                                 re.I | re.M)
_TITLE = _LABEL(r"job title|title")
# "Position:" and "Role:" lines are often something else ("Position: Full-time"): read only when the first line is no
# title (the red team before Codex Pass 15, RT-26)
_POSITION = _LABEL(r"position|role")
_COMPANY = _LABEL(r"company|employer|organization|hiring company")
# any label that names the location, up to three words before it ("Location", "Work Location", "Other Locations",
# "Additional Locations", "Primary Location", "Location(s)"): every one is the location field, never a hint (the red
# team's gr1-saved-6)
_LOCATION = _LABEL(r"(?:[A-Za-z][\w/&-]*[^\S\n]+){0,3}locations?(?:[^\S\n]*\(s\))?")
# A requisition's label (the red team before Codex Pass 15, RT-09 and RT-11): "Job ID", "Job #", "Job No.", "Job
# Identification", "Job Requisition", "Req", "Req. ID", "Req #", "Requisition", "Requisition ID", "Position ID",
# "Vacancy ID", "Opening ID", "Posting Number". A whole word: "Job Identification" is never "Job Id" + "entification",
# nor "job notifications" "job no" + "tifications".
_REQ_LABEL = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:job|position|vacancy|opening|posting)[^\S\n]*(?:requisition[^\S\n]*)?"
    r"(?:id|identification|#|number|no\.?|ref(?:erence)?)"
    r"|job[^\S\n]+requisition(?:[^\S\n]*(?:id|#|number|no\.?))?"
    r"|req(?:uisition)?\.?(?:[^\S\n]*(?:id|identification|#|number|no\.?))?"
    r")(?![A-Za-z])", re.I)
# ...its value, on the label's own line, after an optional ":" or "#": read whole ("2026/0412", "2026-0412", "REQ 4401"),
# with a digit in it, never cut to a prefix, and never prose ("include the Job ID and title" states none)
_REQ_SEP = re.compile(r"[^\S\n]*(?:[:#][^\S\n]*)?")
_REQ_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-/\u2010-\u2013.]*")
MAX_REQUISITION = 40
# "Claims Analyst at Acme Rx": split at the last " at " that is not "at home" ("Work at Home Claims Specialist" is a title)
_AT = re.compile(r"^(.{3,100})\s+(?:at(?!\s+home\b)|@)\s+([A-Z][A-Za-z0-9&.,' -]{1,80})$", re.I)
# a job site's section heading is never a posting's title
_HEADINGS = {"job details", "about the job", "about this job", "job description", "full job description", "description",
             "overview", "job summary", "summary", "about us", "about the role", "about the company", "position summary",
             "job posting", "posting details", "details", "responsibilities", "qualifications", "apply now", "save job"}


def company_from_url(url: str | None) -> str | None:
    """A company name the link itself states: the Workday tenant ("acme.wd5.myworkdayjobs.com" -> "acme")."""
    try:
        host = urlsplit(url or "").netloc.lower()
    except ValueError:
        return None
    if manual_system(url or "") == "workday" and host.count(".") >= 3:
        return host.split(".")[0]
    return None


def given(value: str | None) -> str | None:
    """A typed argument, or None when it is blank: a value whose canonical form is empty ("", "   ", a zero-width
    space) is not given, as the request reads it (request_of: "" and None are one value). So a blank --location never
    hides the posting's own Location line (the coordinator's probe after Codex Pass 17, D201)."""
    return value if isinstance(value, str) and canonical(value) else None


def company_ends(value: str) -> str:
    """A typed company that fits its limit, as kept: its canonical form with spaces, commas and periods stripped from its
    ends, again until none is left. So it is a function of the request's own value (request_of binds canonical(value)):
    a tab, no-break space, zero-width or invisible character or line break after a final "." no longer stops the strip,
    and "Acme Inc." with a tab after it keeps "Acme Inc", as "Acme Inc." does (the coordinator's reading after Codex
    Pass 17, D201)."""
    kept = canonical(value)
    while (shorter := canonical(kept.strip(" ,."))) != kept:
        kept = shorter
    return kept


def parse_pasted(text: str, url: str | None = None, *, company: str | None = None, title: str | None = None,
                 location: str | None = None) -> dict:
    company, title, location = given(company), given(title), given(location)
    whole_text = text
    text = clean_text((text or "")[:MAX_TEXT])
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    found_title = title or (m.group(1) if (m := _TITLE.search(text)) else None)
    found_company = company or (m.group(1) if (m := _COMPANY.search(text)) else None)
    # what the reader takes by heuristic or infers, never recorded as stated (the D190 design, section 3, "Absent versus
    # stated"; BATCH_5 D1): a later paste that states it differently is a change
    inferred = []
    first = lines[0] if lines else ""
    usable = bool(first) and len(first) <= 100 and not first.endswith(".") and norm(first) not in _HEADINGS \
        and not re.match(r"[^:\n]{1,30}:\s", first)   # a "Company: ..." line is no title
    if not found_company and usable:
        # the employer only from a first line that says "TITLE at EMPLOYER", and only when no Company line names it
        at = _AT.match(first)
        if at:
            inferred += ([] if found_title else ["title"]) + ["company"]
            found_title, found_company = found_title or at.group(1), at.group(2)
    if not found_title and usable:
        found_title = first   # whole: never cut at " at " while a Company line names the employer (RT-26)
        inferred.append("title")
    if not found_title:
        found_title = m.group(1) if (m := _POSITION.search(text)) else None
        inferred += ["title"] if found_title else []
    if not found_company and company_from_url(url):
        found_company = company_from_url(url)   # the Workday tenant in the link's host
        inferred.append("company")
    # the location field: your --location, or the posting's own "Location:" line, never a phrase found in its prose
    # ("Previously advertised as Remote - US" is not where the role is: Codex CR-2026-11-15-GATE-FIXES F1, Pass 5)
    labelled = label_location(text)
    found_location = location or labelled or ""
    source = "argument" if location else "label" if labelled else "none"
    # a value kept shorter than stated is marked cut, never recorded as the whole (Codex H38): the location lines as
    # stated (label_location keeps MAX_LOCATION of them), and the text past MAX_TEXT
    stated_location = location or _location_lines(text) or ""
    cut = [name for name, value, limit in (("title", found_title, 200), ("company", (found_company or "").strip(" ,."), 100),
                                           ("location", stated_location, MAX_LOCATION)) if len(value or "") > limit]
    if whole_text is not None and whole_text[MAX_TEXT:].strip():
        cut.append("text")
    # a typed company cut to its limit is cut as typed (its first 100 characters after its ends' spaces, commas and
    # periods), and the request binds what is kept (D201); one that fits is read from its canonical form (company_ends)
    kept_company = canonical_cut((found_company or "").strip(" ,."), 100)
    if company and "company" not in cut:
        kept_company = company_ends(company)
    # every field it returns in the canonical form, your own arguments from the CLI or the review page included (the
    # D190 design, section 3a, point 8): the company makes the manual id
    return {"title": canonical_cut(found_title, 200), "company": kept_company,
            "location": canonical_cut(found_location, MAX_LOCATION), "location_source": source, "requisition_id": requisition_in(text),
            "text": text, "inferred": sorted(set(inferred)), "cut": cut,
            "url": url, "canonical": canonical_url(url) if url else None}


def _requisition_at(text: str, end: int) -> tuple[str | None, int]:
    """The requisition value after a label ending at `end`, on the label's own line, or None; and where reading goes on
    (after the value, so a value's own letters are never read as another label: "Job ID: REQ 4401")."""
    rest = text[end:end + 120].split("\n", 1)[0]
    skip = m.end() if (m := _REQ_SEP.match(rest)) else 0
    rest = rest[skip:]
    first = _REQ_TOKEN.match(rest)
    if not first:
        return None, end
    value, used = first.group(0), first.end()
    if not any(c.isdigit() for c in value):
        # a short code and its number written apart ("REQ 4401") is read whole; a word is no requisition
        after = rest[first.end():]
        second = _REQ_TOKEN.match(after[1:]) if after[:1] == " " else None
        if not (value.isalpha() and value.isupper() and len(value) <= 5 and second
                and any(c.isdigit() for c in second.group(0))):
            return None, end
        value, used = f"{value} {second.group(0)}", first.end() + 1 + second.end()
    value = value.rstrip(".-/\u2010\u2011\u2012\u2013")   # "Job ID: R-4401." ends a sentence (RT-12)
    # at least three characters: "job #1" and "Req: 3+" are a slogan and a requirement, never a requisition (the red
    # team's fr1-paste-0 and fr1-saved-0); a classification "Job Code" is no requisition label at all
    return (value if 3 <= len(value) <= MAX_REQUISITION else None), end + skip + used


def requisitions_in(text: str | None) -> list[str]:
    """Every requisition the text states, each once (by norm_req), in order."""
    from careeros.discovery.seen_index import norm_req
    found, seen, pos, text = [], set(), 0, text or ""
    while (m := _REQ_LABEL.search(text, pos)) is not None:
        value, pos = _requisition_at(text, m.end())
        pos = max(pos, m.end())
        if value and norm_req(value) not in seen:
            seen.add(norm_req(value))
            found.append(value)
    return found


def requisition_in(text: str | None) -> str | None:
    """The requisition a posting's text states ("Job ID: R-1101", "Req #4001", "Requisition R-8102", "Job No.
    2026/0412"), or None: none stated, or two different ones (which is the posting's is not known). Read whole, by its
    label's own line (the red team before Codex Pass 15, RT-09, RT-11 and RT-12)."""
    found = requisitions_in(text)
    return found[0] if len(found) == 1 else None


MAX_LOCATION = 300   # a location field longer than this is refused (careeros location) or kept whole to that length


def label_location(text: str) -> str | None:
    """The posting's own "Location:" lines, each on its line, or None: every such line, in order, joined with "; ", and
    never cut before the placement rule reads it. A field naming Canada after its 120th character, or on a second
    "Location:" line, is never read as plainly US by its first part (the red team before Codex Pass 15, RT-28, RT-29)."""
    whole = _location_lines(text)
    return whole[:MAX_LOCATION] if whole else None


def _location_lines(text: str) -> str | None:
    """The posting's own "Location:" lines as stated, whole: label_location keeps MAX_LOCATION characters of them, and
    parse_pasted marks the location cut when they are longer (Codex H38)."""
    values = []
    for m in _LOCATION.finditer(text or ""):
        if m.group(1) not in values:
            values.append(m.group(1))
    return "; ".join(values) if values else None


def location_write(store, job_id: str, with_facts: bool = False):
    """The record of the last write of a manual job's location field, or None: the observation written in the same
    transaction as the field, whose facts say where the value came from (`location_write`: argument, label, file or
    none). A later sighting of the posting never counts (Codex CR-2026-11-15-GATE-FIXES H2, Pass 7)."""
    for o in store.all("SELECT location, facts_json FROM observations WHERE job_id = ? ORDER BY observed_at DESC, rowid DESC",
                       (job_id,)):
        facts = json.loads(o["facts_json"] or "{}")
        if "location_write" in facts:
            return {"location": (o["location"] or "").strip(), "source": facts["location_write"],
                    "digest": facts.get("location_digest"), **({"facts": facts} if with_facts else {})}
    return None


# a sentence end inside a value: "Previously advertised as Remote - US. The work ..." (D166, Codex H3 Pass 7)
_SENTENCE_END = re.compile(r"\.\s+\S")


def _squash(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _yours(facts: dict) -> bool:
    """Whether a location write recorded as your argument is yours: `careeros location` (its "via"), or the posting's own
    paste or add with --location (the ingest's record of the posting, which carries its read fields). A re-paste's
    confirmation of D162 builds carries neither."""
    return facts.get("via") == "careeros location" or "remote_type" in facts


def location_declared(store, job: dict) -> bool:
    """Whether a manual job's stored location may place it. Codex CR-2026-11-15-GATE-FIXES H2, Passes 6-7.
    - A posting from a job board: always (its board's location field).
    - A manual job whose location write is recorded (2026-11-17 on): when it was your --location, a saved file's own
      location field as the current saved-file reader wrote it (saved.READER: an older reader could keep one of
      several location lines, RT-27 and RT-29, or drop a "Work Location:" line, fr1-saved-4), or the posting's own
      same-line "Location:" (read again from the stored text by the current rule).
    - A manual job saved before that, with no record: only when it is its own same-line "Location:" and does not run
      past a sentence end (D166). A location its text does not contain came from you or from a saved file's header,
      which an older reader may have cut (the red team's fr1-upgrade-2): it is held like any older row that is not
      provably clean (the owner's D166 Q2). Otherwise it may be a phrase from the prose ("Previously advertised as
      Remote - US"). Either way it places nothing until you set it (careeros location JOB "...", the owner's D180).
    `job` needs job_id, ats, location_raw and description_text."""
    location = (job.get("location_raw") or "").strip()
    if not location or job.get("ats") != "manual":
        return True
    text = job.get("description_text") or ""
    write = location_write(store, job["job_id"], with_facts=True)
    if write is not None and write["source"] in ("scan", "single"):
        # a job you pasted or saved first that a board's own read has since written: its board's location field, whole
        # (pipeline.job_of_record; the red team's batch 3, group A)
        from careeros.pipeline import location_digest
        return write.get("digest") == location_digest(location)
    if write is not None and write["location"] == location:
        if write["source"] == "argument" and not _yours(write["facts"]):
            # a re-paste's confirmation an earlier build recorded (D162, 2026-11-17), which could match another opening
            # at a shared page by its link and title: it places nothing until you set the job's location yourself
            # (careeros location; the red team before Codex Pass 15, RT-36)
            return False
        if write["source"] == "argument":
            return True
        if write["source"] == "file":
            from careeros.discovery.saved import READER
            return write["facts"].get("reader") == READER
        return write["source"] == "label" and label_location(text) == location
    if label_location(text) == location and _pasted_whole(store, job["job_id"], location):
        # an older paste may have been stored with a line-ending control already deleted between a blank "Location:"
        # and the prose, joining them into one line: a value that runs past a sentence end waits for your confirmation
        # (the owner's choice, D166; a shorter joined value is the documented limit)
        return not _SENTENCE_END.search(location)
    return False


def _pasted_whole(store, job_id: str, location: str) -> bool:
    """Whether an older row's location is what a whole input you pasted gave: a paste's sighting (human_text) recorded
    that very location. A paste's text is the whole input, so its own same-line "Location:" lines are re-read whole;
    a saved file's stored text is only its body after the header, whose other location lines an older reader may
    have cut, so a saved row never counts as provably clean (the red team's gr1-saved-1, batch 3 C.3): it waits for
    your careeros location JOB "..."."""
    return any((r["location"] or "").strip() == location for r in store.all(
        "SELECT location FROM observations WHERE job_id = ? AND source_kind = 'human_text'", (job_id,)))


def same_manual_posting(row, *, url: str | None, requisition_id: str | None = None, text: str | None = None,
                        employer: str | None = None, title: str | None = None, location: str | None = None) -> bool:
    """Whether a stored job (`row`: job_id and url at least) is the manual posting being placed (`place_manual`), by
    the owner's D188 (2026-11-19, "Exact links only"): only when the posting's link names one posting (a board's
    posting link, a Workday requisition link, a job site's id) and the stored job is that posting
    (board_discovery.job_identity). Nothing else makes two postings one job: not the same careers-page link, text,
    title, requisition, employer or place, nor a link typed the same. The other inputs are kept for the callers and no
    longer read."""
    from careeros.discovery.board_discovery import job_identity, posting_identity
    mine = posting_identity(url)
    return mine is not None and mine == job_identity(row)


# --- operation receipts (the D190 design, revision 8, section 6; Codex M5) ------------------------------------------
# A retry of an intake is recognised by its operation's receipt, keyed per route, never by the posting's identity: the
# review page's paste by its form key, careeros import by the saved file's bytes, careeros paste and add-url --text-file by
# the request (link, text and arguments). A plain operation's receipt is written with its job in the intake's own
# transaction; a --separate one records a pending receipt K first, and its completion K:done with the job. Receipts are
# append-only: a different request under a key is refused (IdempotencyCollision).
@dataclass(frozen=True)
class Operation:
    command: str
    key: str
    payload: dict
    separate: bool = False
    retry: bool = False   # named by its id (--operation): it must have been recorded


def request_of(stored_url: str | None, stored_text: str, **arguments) -> dict:
    """An intake request as its operation knows it (the D190 design, section 6): what the intake stores, from the same
    input its storage reads (Codex Pass 7, H24: the proof from the same buffer and interpretation as storage, the
    target lookup apart): the link exactly as the route stores it, the text as the route's reader takes it, and your
    arguments in the canonical form, "" and None as one value. So a request whose stored link differs (http and
    https, another tracking parameter) or whose stored text differs (padding past the paste's cap) is another
    request, read as its own; two spellings of one name are one (section 3a, point 8)."""
    return {"url": stored_url or None, "text": stored_text,
            **{k: (canonical(v) or None) if isinstance(v, str) else v for k, v in arguments.items()}}


def paste_request(url: str | None, text: str | None, *, company: str | None = None, title: str | None = None,
                  location: str | None = None) -> dict:
    """A paste's request: its link as intake stores it, its text as parse_pasted takes it (the first MAX_TEXT
    characters, in the canonical form), and what that reading cut (parse_pasted's marks, from the same parse; Codex
    H43). A completed receipt answers only the same request, so a paste that runs past the limit is never answered by
    the receipt of the same posting ending at it: it is read once, and its sighting carries its cut mark to P1. Two
    pastes both cut, differing only past the limit, are one request. A request with nothing cut has no "cut" key, so it
    is the request it was before, and a receipt recorded then still answers it.
    A title, company or location cut to its limit is bound as kept too ("kept": the value parse_pasted keeps, and for
    the location its source): a typed value is cut as typed, before its canonical form, so spacing in its first part
    changes which words are kept while the whole value's canonical form and the cut mark stay the same (Codex H45,
    Pass 17; the owner's D201, "Bind kept values when cut"). A field read from the text is bound by the text already."""
    parsed = parse_pasted(text or "", url, company=company, title=title, location=location)
    cut = parsed["cut"]
    kept = {name: parsed[name] for name in ("title", "company", "location") if name in cut}
    if "location" in cut:
        kept["location_source"] = parsed["location_source"]
    return request_of(url, clean_text((text or "")[:MAX_TEXT]), company=company, title=title, location=location,
                      **({"cut": cut} if cut else {}), **({"kept": kept} if kept else {}))


def operation_for(command: str, route_key: str, payload: dict, *, separate: bool = False,
                  operation_id: str | None = None) -> Operation:
    """The operation an intake request is: by its route's key (`route_key`: a form key, a file's digest, or None for the
    request's own fingerprint); with `separate`, a new operation id; with `operation_id`, that separate operation again."""
    import uuid

    from careeros.core.fingerprint import fingerprint
    if operation_id:
        return Operation(command, operation_id, payload, separate=True, retry=True)
    if separate:
        return Operation(command, f"{command}:op:{uuid.uuid4().hex[:16]}", payload, separate=True)
    return Operation(command, f"{command}:{route_key or fingerprint(payload)[:40]}", payload)


def recorded_job(store, op: Operation) -> tuple[str, str | None]:
    """("done", job) when the operation completed; ("pending", None) when only its pending receipt exists (a separate
    operation interrupted before its job); ("new", None) when it never ran. IdempotencyCollision for another request
    under the key, ValueError for an operation id never recorded."""
    from careeros.core.fingerprint import fingerprint
    from careeros.db.store import IdempotencyCollision
    digest = fingerprint({"command": op.command, "payload": op.payload})
    row = store.one("SELECT payload_fingerprint, result_ref FROM receipts WHERE receipt_key = ?", (op.key,))
    if row is not None and row["payload_fingerprint"] != digest:
        raise IdempotencyCollision(op.key)
    if op.separate:
        done = store.one("SELECT result_ref FROM receipts WHERE receipt_key = ?", (op.key + ":done",))
        if done is not None:
            return "done", done["result_ref"]
        if row is None and op.retry:
            raise ValueError(f"unknown operation {op.key}: no operation with this id was recorded")
        return ("pending", None) if row is not None else ("new", None)
    return ("done", row["result_ref"]) if row is not None else ("new", None)


def begin(store, op: Operation) -> None:
    """A separate operation's pending receipt, committed before any write of the intake (key K, no job)."""
    if op.separate and store.one("SELECT 1 FROM receipts WHERE receipt_key = ?", (op.key,)) is None:
        store.writer("discovery").receipt(op.command, op.key, op.payload, actor_id="user")


def complete(store, op: Operation, job_id: str) -> None:
    """The operation's completion, in the caller's transaction with the job: the receipt K (a plain operation) or
    K:done (a separate one), carrying the job's id."""
    store.writer("discovery").receipt(op.command, op.key + (":done" if op.separate else ""), op.payload,
                                      actor_id="user", result_ref=job_id)


class NotStored(Exception):
    """An intake's posting was not written (refused or failed): its whole transaction rolls back, receipt included, and
    the write-ahead hold stays (the D190 design, sections 6 and 7)."""


def not_stored(store, job_id: str, route: str, reasons=()) -> None:
    """After the rollback: why, recorded again, so the review page names it. A refusal keeps its own code (a
    manual_id_collision); a failed write is recorded as intake_not_stored, never as a hold (the write-ahead hold, on a
    job that existed, already stands)."""
    disc = store.writer("discovery")
    told = False
    for reason in reasons:
        code = reason.get("error_code")
        if code in ("ingest_failed", "read_pending", None):
            continue
        disc.exception("data_quality", "medium", f"{route} not stored: {reason.get('message')}"[:500], entity_type="job",
                       entity_id=reason.get("entity_id") or job_id, error_code=code)
        told = True
    if not told:
        failure = next((r.get("message") for r in reasons if r.get("error_code") == "ingest_failed"), None)
        disc.exception("data_quality", "medium", f"{route} not stored: " + (failure or "the posting could not be written")
                       + "; nothing of it was kept", entity_type="job", entity_id=job_id, error_code="intake_not_stored")


MAX_PLACEMENTS = 64


class PlacementRefused(ValueError):
    """No free id is left beside the other postings that hold this one's generated id: the posting is reported, never
    written over another (Codex CR-2026-11-15-GATE-FIXES H11, Pass 9)."""


def place_manual(store, ats: str, token: str, external: str, *, url: str | None, requisition_id: str | None,
                 text: str, employer: str | None, title: str | None, location: str | None) -> tuple[str, str | None]:
    """(the external id this manual posting is written under, the job id of a different posting it collided with or
    None). The generated id when it is free or holds this same posting; else the id this posting was given before
    (found by its identity, or as the same submission: a retry reuses it); else a new one with a short suffix from its
    link and text, after at most MAX_PLACEMENTS tries (ValueError beyond: the posting is reported, never written over
    another). Two employers' names can make the same token ("A&B Health", "A B Health"), and two openings of one portal
    the same link before its "#" part; a posting never takes over another's job, on any manual route. Codex CR-2026-11-15-GATE-FIXES H8, H10
    and H11, Passes 8-9 (the owner's choice, D172: tracked separately, linked as a possible duplicate)."""
    held_id = f"{ats}:{token}:{external}"
    columns = "job_id, url, requisition_id, description_text, employer_name, title, location_raw"
    same: dict = dict(url=url, requisition_id=requisition_id, text=text, employer=employer, title=title, location=location)
    held = store.one(f"SELECT {columns} FROM jobs WHERE job_id = ?", (held_id,))
    # a retry is recognised by its operation's receipt, never here (the D190 design, section 6; Codex M5): only the
    # same posting by its exact link names a held id
    if held is None or same_manual_posting(held, **same):
        return external, None
    prefix, given = f"{held_id}~", f"{ats}:{token}:"
    for row in store.all(f"SELECT {columns} FROM jobs WHERE substr(job_id, 1, ?) = ? ORDER BY job_id",
                         (len(prefix), prefix)):
        if same_manual_posting(row, **same):
            return row["job_id"][len(given):], held_id
    digest = text_fingerprint(f"{posting_link(url) or url or ''}\n{text or ''}")
    for n in range(MAX_PLACEMENTS):
        candidate = f"{external}~{digest[:8]}" + (f"-{n + 1}" if n else "")
        if not store.one("SELECT 1 FROM jobs WHERE job_id = ?", (f"{given}{candidate}",)):
            return candidate, held_id
    raise PlacementRefused(f"manual_id_collision: no free id after {MAX_PLACEMENTS} tries beside {held_id}")


def set_location(ctx, job_id: str, location: str) -> dict:
    """`careeros location JOB "..."`: you set a pasted or added posting's location by naming the job, never by a match.
    The change and the record of where it came from (your argument) are one transaction, and the job leaves its earlier
    decision in it: the next processing run screens it again. The same value again is recorded as yours, and the next
    processing run re-derives the country from it (and screens the job again when that changes it). A posting from a job
    board keeps its board's location (the owner's D180; it replaces D162's confirmation by re-paste) unless its board's
    field leaves it unknown or places it abroad: then you may settle it, bound to that board field, which a different
    one from the board supersedes (the owner's D192 build, part 4; Codex's D190 design review H6; batch 4's
    hr1-boards-2). The country is recomputed in the same transaction. Returns {'job_id', 'changed'}; ValueError if
    refused."""
    from careeros.openings import record_observation
    store = ctx.store
    location = canonical(location or "")   # stored and recorded in the canonical form (Codex's design review Pass 5)
    if not location or len(location) > MAX_LOCATION:
        raise ValueError(f"give the location, at most {MAX_LOCATION} characters")
    job = store.one("SELECT job_id, ats, url, canonical_url, requisition_id, title, employer_name, current_state, duplicate_of, "
                    "country, location_raw FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError(f"no tracked job {job_id}")
    from careeros.pipeline import board_written
    board = job["ats"] != "manual" or board_written(store, job_id)
    if board and job["country"] == "US" and _active_settle(store, job) is None:
        raise ValueError(f"{job_id} is a job board's posting its board places in the US: it keeps its board's location")
    if job["current_state"] == "duplicate":
        raise ValueError(f"{job_id} is a duplicate of {job['duplicate_of']}: set that job's location")
    if job["current_state"] == "closed":
        raise ValueError(f"{job_id} is closed: it is not screened again")
    with store.tx():
        changed = _confirm_location(ctx, job_id, location)
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job_id, url=job["url"],
                           canonical=job["canonical_url"], requisition_id=job["requisition_id"], title=job["title"],
                           company=job["employer_name"], location=location,
                           facts={"location_source": "argument", "location_write": "argument", "via": "careeros location",
                                  **({"board_field": _board_field(store, job)} if board else {})})
    return {"job_id": job_id, "changed": changed}


def active_settle(store, job_id: str, location_raw: str | None) -> dict | None:
    """The facts of the location you last settled on a board job (careeros location), while it stands: the stored field
    is still the value you gave (a board read that kept it records a write of that same value). None when a different
    field from the board has superseded it, or none was settled (the owner's D192 build, part 4)."""
    for o in store.all("SELECT location, facts_json FROM observations WHERE job_id = ? ORDER BY observed_at DESC, rowid DESC",
                       (job_id,)):
        facts = json.loads(o["facts_json"] or "{}")
        if facts.get("via") == "careeros location":
            if "board_field" in facts and (o["location"] or "").strip() == (location_raw or "").strip():
                return facts
            return None
    return None


def _active_settle(store, job) -> dict | None:
    return active_settle(store, job["job_id"], job["location_raw"])


def _board_field(store, job) -> str:
    """The board's own location field a settled location is bound to: the one you settled first, while it stands, else
    the board's field stored now."""
    settled = _active_settle(store, job)
    return settled["board_field"] if settled is not None else (job["location_raw"] or "")


def _confirm_location(ctx, job_id: str, location: str) -> bool:
    """A location you set (careeros location) replaces the stored one; when that changes it, the job leaves its earlier
    decision in the same transaction and the next processing run screens it again (Codex CR-2026-11-15-GATE-FIXES H2 and
    H5, Passes 6-7). The country and remote scope are recomputed from it in the same transaction, and the board's typed
    countries no longer place it (the owner's D192 build, part 4). Returns whether the stored location changed. The
    caller records the write in the same transaction."""
    from careeros.normalize.posting import detect_remote, placed_country, remote_scope, remote_state_rules
    store = ctx.store
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if (job["location_raw"] or "") == location:
        return False
    if job["source_json"]:
        # the source record takes your location; the field it overrode is its binding, so a later read sending that
        # same field again is no change and a different one is (the D190 design, section 3, "Retained fields"); every
        # derived value comes from the one derivation
        from careeros.pipeline import derive, source_json
        record = json.loads(job["source_json"])
        bindings = dict(record.get("bindings") or {})
        bindings.setdefault("location", record.get("location") or "")
        bindings.setdefault("location_source", record.get("location_source"))
        if record.get("typed_countries"):
            bindings.setdefault("typed_countries", record["typed_countries"])
        record.update(location=location, location_source="argument", typed_countries=[], bindings=bindings)
        fields = derive(ctx, record, job_id)
        values = {k: v for k, v in fields.items() if k != "source_updated_at" and job[k] != v}
        store.writer("discovery").update("jobs", {**values, "source_json": source_json(record)}, "job_id = ?", (job_id,))
        if job["current_state"] not in ("discovered", "normalized", "closed", "duplicate"):
            store.writer("discovery").transition(ctx.matrix, job_id, "normalized", reason_code="derived_fields_refreshed")
        return True
    text = job["description_text"] or ""
    remote = detect_remote(location, None, text)
    allowed, excluded = remote_state_rules(location, text, remote)
    country = placed_country(job["title"], location, text, remote)
    store.writer("discovery").update("jobs", {"location_raw": location, "remote_type": remote,
                                              "remote_states_json": json.dumps(allowed),
                                              "remote_excluded_states_json": json.dumps(excluded), "country": country,
                                              "remote_scope": remote_scope(remote, country, allowed, excluded, text),
                                              "typed_countries_json": None}, "job_id = ?", (job_id,))
    if job["current_state"] not in ("discovered", "normalized", "closed", "duplicate"):
        store.writer("discovery").transition(ctx.matrix, job_id, "normalized", reason_code="derived_fields_refreshed")
    return True


def _same_link_match(store, job_id: str, parsed: dict) -> bool:
    """Whether a paste found its tracked job by the same link (its "#" route included), not only by employer with a
    requisition or title."""
    from careeros.discovery.board_discovery import job_identity, names_one_posting, posting_identity
    row = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        return False
    mine = posting_link(parsed["url"])
    if mine is not None and posting_link(row["url"]) == mine and names_one_posting(parsed["url"]):
        return True
    # another form of the same posting's link (a board's other host, a tracking parameter): the posting its exact link
    # names, as the paste found it (the owner's D188; the hand check's HC1)
    identity = posting_identity(parsed["url"])
    return identity is not None and job_identity(row) == identity


def intake(ctx, text: str, url: str | None = None, *, company: str | None = None, title: str | None = None,
           location: str | None = None, operation: Operation | None = None) -> dict:
    """Grade a posting you pasted. Returns {'job_id', 'status': 'added' | 'already_tracked' | 'recorded',
    'manual_source'}. `operation` is the request's operation (the review page's form key; by default the request's own
    fingerprint, as careeros paste): a retry of a completed one returns its job ('recorded') and writes nothing."""
    from careeros.discovery import board_discovery as bd
    from careeros.openings import record_observation
    company, title, location = given(company), given(title), given(location)   # a blank argument is not given (D201)
    parsed = parse_pasted(text, url, company=company, title=title, location=location)
    if not parsed["title"]:
        raise ValueError("no job title found: pass --title")
    if not parsed["company"]:
        raise ValueError("no company found: pass --company")
    if len(parsed["text"]) < 200:
        raise ValueError("too little text to grade: paste the whole posting")
    store = ctx.store
    op = operation or operation_for("paste", "", paste_request(url, text, company=company, title=title, location=location))
    state, recorded = recorded_job(store, op)
    if state == "done" and recorded:
        return {"job_id": recorded, "status": "recorded", "manual_source": None, "operation": op.key}
    begin(store, op)
    slug = re.sub(r"[^a-z0-9]+", "-", parsed["company"].lower()).strip("-")[:60] or "manual"
    # a requisition is an id only as letters, digits, "_", "." and "-" ("2026/0412" -> "2026-0412", "REQ 4401" -> "REQ-4401")
    req_id = re.sub(r"[^A-Za-z0-9_.\-]+", "-", parsed["requisition_id"]).strip("-.") if parsed["requisition_id"] else ""
    external = req_id or text_fingerprint(url or parsed["text"])[:16]
    from careeros.pipeline import employer_entry, target_of
    entry = employer_entry(ctx, name=parsed["company"])
    posting = RawPosting(ats="manual", board_token=slug, external_id=external, title=parsed["title"], location=parsed["location"],
                         url=url, apply_url=url, description_text=parsed["text"], requisition_id=parsed["requisition_id"],
                         raw={"source": "human_text", "url": url, "location_source": parsed["location_source"],
                              **{mark: parsed[mark] for mark in ("inferred", "cut") if parsed[mark]}})
    # the one resolver (the D190 design, section 7; Codex H20): the job of record of the posting the paste's own link
    # names, else its own id, which may be another posting's (H8, Pass 8; H11, Pass 9): this same posting, at that id or
    # the one it was given before, is that job; a different one gets its own id and is linked to it, and the other job
    # is never touched. A stale duplicate never sends a paste to another posting (the owner's V1)
    posting, target, collided = target_of(store, entry, posting)
    existing = target if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (target,)) else None
    if existing:
        collided = None
    manual = None
    if url and manual_system(url):
        candidate = bd.Candidate(employer_name=parsed["company"], origin="manual", board_url=url, posting_url=url)
        manual = bd._upsert_mapping(store, candidate, manual_system(url), urlsplit(url).netloc.lower(), collection="manual_only",
                                    status="unresolved", evidence=[{"kind": "supplied_board_link", "detail": url[:200]}],
                                    relevant=None, verified_method=None)
    source = {"location_source": parsed["location_source"]}
    if existing:
        # a paste never changes a tracked job's location: you name the job instead (careeros location JOB "..."), so no
        # match can put one posting's location on another (the owner's D180; it replaces D162's re-paste)
        held = store.one("SELECT ats, country FROM jobs WHERE job_id = ?", (existing,))
        note = None
        if location and held is not None and held["ats"] != "manual" and held["country"] == "US":
            note = f"location not changed: {existing} is a job board's posting and keeps its board's location"
        elif location and held is not None and held["ats"] != "manual":
            # its board's field leaves it unknown or abroad: you may settle it by naming the job (part 4 of D192)
            note = (f"location not changed: {existing} is a job board's posting its board leaves unknown or abroad; to "
                    f"settle it, run careeros location {existing} \"{location}\"")
        elif location and _same_link_match(store, existing, parsed):
            note = f"location not changed: to set {existing}'s location, run careeros location {existing} \"{location}\""
        elif location:
            # found by employer and title (or requisition) only: it may be another opening, so no command is offered
            # for it (the red team after Codex Pass 11)
            note = (f"location not changed: this paste matched {existing} by employer and title only, so it may be another "
                    f"opening; paste it with its own link to track it on its own")
        # a paste found already tracked writes nothing to the job: its read is a sighting, which P1 compares with each
        # label (the D190 design, section 3, part d)
        from careeros.pipeline import _stored_again, sighting_facts, source_record, write_ahead
        from careeros.review.packages import prune_pending_holds
        # a nested batch (section 7): the job held ahead of the sighting, cleared by its id with it
        ahead = write_ahead(store, [(0, existing)])
        try:
            with store.tx():   # the sighting, its hold's clearing and the operation's completion: one transaction
                record_observation(store, source_kind="human_text", evidence_level="human", job_id=existing, url=url,
                                   canonical=parsed["canonical"], requisition_id=parsed["requisition_id"],
                                   title=parsed["title"], company=parsed["company"], location=parsed["location"],
                                   facts={**source, **sighting_facts(source_record(posting, entry))})
                _stored_again(store, existing, board=False, hold_id=ahead[0][0] if ahead else None)
                complete(store, op, existing)
        finally:
            prune_pending_holds(store)
        return {"job_id": existing, "status": "already_tracked", "manual_source": manual, "location_confirmed": False,
                "location_changed": False, "location_note": note, "operation": op.key}
    job_id = target
    placed: dict = {}

    def written(counts) -> None:
        """The job, its location's source, the paste's sighting and its receipt: one transaction (H2, Pass 7; the D190
        design, section 6); a posting not written rolls the whole intake back."""
        if placed.get(job_id) != job_id:
            raise NotStored(job_id)
        from careeros.pipeline import sighting_facts, source_record
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job_id, url=url,
                           canonical=parsed["canonical"], requisition_id=parsed["requisition_id"], title=parsed["title"],
                           company=parsed["company"], location=parsed["location"],
                           facts={**source, **sighting_facts(source_record(posting, entry))})
        if collided:
            from careeros.openings import link
            link(store, job_id, collided, "possible_duplicate", f"the same generated id as {collided}: another posting")
        complete(store, op, job_id)

    from careeros.pipeline import nested_ingest
    try:
        nested_ingest(ctx, entry, [posting], placed=placed, after=written)
    except NotStored as exc:   # refused or failed: nothing written, the refusal recorded (H14, Pass 10)
        not_stored(store, job_id, "paste", getattr(exc, "reasons", ()))
        raise ValueError("the posting could not be stored (recorded as an open exception on the review page)") from None
    return {"job_id": job_id, "status": "added", "manual_source": manual, "id_collision": collided,
            "operation": op.key}
