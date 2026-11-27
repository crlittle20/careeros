"""Postings you saved yourself (browser "Save page as", or pasted text) dropped into the vault inbox.

Used for sites whose terms forbid automated access (an employer's own careers site, a staffing agency's): you browse, CareerOS
reads only the files you saved. Saved pages are parsed from their schema.org JobPosting data (which career sites embed
for search engines); plain-text files use a small header.

Text file format:
    URL: https://...
    Title: Business Systems Analyst
    Employer: Northgate Health
    Location: Remote (optional)

    <posting text>
"""
from __future__ import annotations

import html as htmllib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from careeros.core.clock import from_external
from careeros.core.fingerprint import text_fingerprint
from careeros.normalize.posting import RawPosting, country_name, norm, posting_link
from careeros.normalize.sanitize import canonical, canonical_values, clean_text, html_to_text, split_newlines

from .manual_intake import _LOCATION, requisition_in

_LD = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
# a tag's attributes in any order, quoted or not: the canonical link and og:url are read whatever their order (the red
# team before Codex Pass 15, RT-08: "href" before "rel", or "content" before "property", dropped the posting's link)
_TAG = lambda name: re.compile(rf"<{name}\b([^>]*)>", re.I)
_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_SAVED_FROM = re.compile(r"<!-- saved from url=\(\d+\)(\S+) -->", re.I)


# the saved-file reader that wrote a location: only this one's is trusted to place a job; a location an older reader
# wrote may be one it cut from several (RT-27, RT-29, fr1-saved-4) and waits for you (the red team's fr1-upgrade-2).
# b: every location label, and a JSON-LD place given by reference (the red team's gr1-saved-2 and gr1-saved-6)
READER = "saved-2026-11-19b"


class SavedPostingError(ValueError):
    pass


def parent_for(name: str, policy) -> str:
    wanted = norm(name)
    for parent, aliases in (policy.get("parents", default={}) or {}).items():
        if wanted == norm(parent) or any(wanted == norm(a) or wanted.startswith(norm(a) + " ") for a in aliases):
            return parent
    return name


def _tag_attr(markup: str, tag: str, key: str, value: str, wanted: str) -> str | None:
    """The `wanted` attribute of the first `tag` whose `key` attribute lists `value`, attributes in any order."""
    for m in _TAG(tag).finditer(markup):
        attrs = {a.lower(): htmllib.unescape(q or s1 or u)
                 for a, q, s1, u in _ATTR.findall(m.group(1))}
        if value in (attrs.get(key) or "").lower().split() and attrs.get(wanted):
            return attrs[wanted].strip()
    return None


def _ld_nodes(markup: str, unread: list | None = None) -> list[dict]:
    """Every object in the page's JSON-LD blocks, @graph members included, in the order they were found. A block is
    read as JSON first: entities in its string values are values ("&quot;" in a description), never decoded before it
    is parsed (the red team before Codex Pass 15, RT-34); a block that is not JSON until its entities are decoded is
    read that way, as before. A block neither reading parses is skipped, and added to `unread` when given."""
    nodes = []
    for block in _LD.findall(markup):
        try:
            data = json.loads(block.strip())
        except ValueError:
            try:
                data = json.loads(htmllib.unescape(block.strip()))
            except ValueError:
                if unread is not None:
                    unread.append(block)
                continue
        # every text value in the canonical form before any reader reads it (the D190 design, section 3a, point 4)
        data = canonical_values(data)
        # walked in the page's own order (a stack reversed a list or an @graph: the red team's fr1-saved-6)
        queue = list(data) if isinstance(data, list) else [data]
        while queue:
            item = queue.pop(0)
            if isinstance(item, dict):
                nodes.append(item)
                if isinstance(item.get("@graph"), list):
                    queue[0:0] = item["@graph"]
            elif isinstance(item, list):
                queue[0:0] = item
    return nodes


def _is_type(item: dict, name: str) -> bool:
    kind = item.get("@type")
    return kind == name or (isinstance(kind, list) and name in kind)


def _every_job_posting(nodes: list[dict]) -> list[dict]:
    """Every JobPosting object anywhere in the page's JSON-LD, each once: a top-level one, an @graph member, or one nested
    in another object (a WebPage's mainEntity, an ItemPage's about): the red team's hr1-saved-3, the owner's D192 build
    part 4. Walked without recursion, in the page's order."""
    found: list[dict] = []
    seen: set[int] = set()
    stack: list = list(reversed(nodes))
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if _is_type(value, "JobPosting"):
                found.append(value)
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))
    return found


def _job_postings(markup: str) -> list[dict]:
    return [n for n in _ld_nodes(markup) if _is_type(n, "JobPosting")]


def _place_text(place) -> str:
    """One jobLocation entry as the location field's words: a Place with a PostalAddress, an address or a Place given as
    text, or a Place's name. Nothing is skipped without its words (the red team before Codex Pass 15, RT-27, RT-33)."""
    if isinstance(place, str):
        return place.strip()
    if not isinstance(place, dict):
        return ""
    address = place.get("address")
    if isinstance(address, list):
        return "; ".join(t for t in (_place_text({"address": a}) for a in address) if t)
    if isinstance(address, str):
        return address.strip()
    if isinstance(address, dict):
        bits = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        bits = [b.get("name") if isinstance(b, dict) else b for b in bits]
        if bits[2]:
            bits[2] = country_name(str(bits[2]))   # "CA" is Canada, never a US state (D179)
        return ", ".join(str(b).strip() for b in bits if b and str(b).strip())
    return str(place.get("name") or "").strip()


def _resolved(value, nodes: list[dict]):
    """A JSON-LD value given by reference ({"@id": ...} and nothing more) replaced by the node of the page that has that
    @id, its own references resolved too (an address given by reference); lists item by item. A reference the page
    does not hold refuses the page: a part of the location field is never dropped (the red team's gr1-saved-2)."""
    if isinstance(value, list):
        return [_resolved(v, nodes) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get("@id") and not set(value) - {"@id", "@type"}:
        found = next((n for n in nodes if n.get("@id") == value["@id"] and set(n) - {"@id", "@type"}), None)
        if found is None:
            raise SavedPostingError(f"the saved page gives part of its location by a reference it does not hold "
                                    f"({str(value['@id'])[:80]}); paste the posting's text with its location instead")
        value = found
    return {k: (_resolved(v, nodes) if k in ("address", "addressCountry", "addressRegion", "addressLocality") else v)
            for k, v in value.items()}


def _location(posting: dict, nodes: list[dict] | None = None) -> str:
    nodes = nodes or []
    places = _resolved(posting.get("jobLocation") or [], nodes)
    places = places if isinstance(places, list) else [places]
    parts = [_place_text(place) for place in places]
    # where a remote applicant may live (schema.org applicantLocationRequirements) is part of the location field: a
    # remote posting open only to applicants in Canada is never read as plainly US by its office's address (RT-27);
    # a place given by reference is read where the page gives it (gr1-saved-2)
    wanted = _resolved(posting.get("applicantLocationRequirements") or [], nodes)
    for place in wanted if isinstance(wanted, list) else [wanted]:
        name = place.get("name") if isinstance(place, dict) else place if isinstance(place, str) else None
        if isinstance(name, str) and name.strip():
            kind = place.get("@type") if isinstance(place, dict) else None
            parts.append(country_name(name.strip()) if kind == "Country" else name.strip())
    location = "; ".join(p for p in parts if p)
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        location = f"Remote{' - ' + location if location else ''}"
    return location


# JSON-LD's unitText, read as stated (BATCH_5 D1: every unit but an hour was read as a year)
# the pay periods the gate annualizes that the engine's period words leave out (qualify.gate.PERIODS_PER_YEAR)
_MORE_UNITS = {"yearly": "year", "biweekly": "biweekly", "fortnightly": "biweekly", "semimonthly": "semimonthly"}


def period_of(unit_text: str | None) -> str | None:
    """A JSON-LD unitText as the period it names, through the engine's own period words (normalize.posting's
    _UNIT_PERIOD and _BARE_PERIOD; BATCH_5 D1, the coordinator on 58126d5): case, underscores, hyphens and a leading
    "PER" do not matter ("PER_HOUR", "Hourly", "BI-WEEKLY"); None for a unit it does not understand."""
    import re

    from careeros.normalize.posting import _BARE_PERIOD, _UNIT_PERIOD
    words = re.sub(r"[\s_\-]+", " ", str(unit_text or "")).strip().lower()
    words = re.sub(r"^(per|an?|each|every) ", "", words)
    if not words:
        return None
    for word in (words, words.replace(" ", "")):
        found = _UNIT_PERIOD.get(word) or _BARE_PERIOD.get(word) or _MORE_UNITS.get(word)
        if found:
            return found
    return None


def _salary(posting: dict) -> dict | None:
    """The page's stated pay. What the page leaves out and the reader fills in for screening (a currency, a period, one
    amount used as both ends) is marked as such under "inferred", never recorded as stated, so a later save that
    states it differently is a change (the D190 design, section 3, "Absent versus stated"; BATCH_5 D1)."""
    salary = posting.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if not isinstance(value, dict):
        return None
    low, high, inferred = value.get("minValue"), value.get("maxValue"), []
    if low is None and high is None and value.get("value") is not None:
        low = high = value.get("value")
        inferred.append("minimum and maximum (one amount stated)")
    try:
        low, high = (float(low) if low is not None else None), (float(high) if high is not None else None)
    except (TypeError, ValueError):
        return None
    if low is None and high is None:
        return None
    stated = str(value.get("unitText") or salary.get("unitText") or "").strip()
    period, unread = period_of(stated), None
    if period is None and stated:
        # a unit the page states that CareerOS does not understand: kept as stated, whole (Codex H38: a 40-character cut
        # made two different units one record), so the record differs from a page that states none and from every
        # other unit; read as a year for screening (BATCH_5 D1)
        period, unread = "year", canonical(stated)
    elif period is None:
        period = "year"
        inferred.append("period")
    currency = salary.get("currency")
    if not currency:
        currency = "USD"
        inferred.append("currency")
    return {"min": low, "max": high, "currency": currency, "period": period, **({"period_stated": unread} if unread else {}),
            **({"inferred": inferred} if inferred else {})}


def parse_saved_html(markup: str, fallback_name: str) -> tuple[RawPosting, str]:
    unread: list = []
    nodes = _ld_nodes(markup, unread)
    if any("jobposting" in block.lower() for block in unread):
        # a job-posting block the reader cannot parse may be the page's own posting: which posting is the page's cannot
        # be told, so none is taken (the red team's gr1-saved-5; fr1-saved-6 for two readable ones)
        raise SavedPostingError("a job-posting block on the saved page cannot be read; paste the posting's text into a "
                                ".txt file instead")
    postings = _every_job_posting(nodes)   # nested ones too (hr1-saved-3)
    if not postings:
        raise SavedPostingError("no JobPosting data in the saved page; save the posting page itself, or paste its text into a .txt file")
    if len(postings) > 1:
        # which of several postings is this page's own cannot be told: never one taken for another (fr1-saved-6)
        raise SavedPostingError(f"the saved page holds {len(postings)} job postings; save the posting's own page, or paste "
                                "its text into a .txt file")
    p = postings[0]
    org = p.get("hiringOrganization") or {}
    org = org[0] if isinstance(org, list) and org else org
    if isinstance(org, dict) and not org.get("name") and org.get("@id"):
        # the employer named by reference to an Organization elsewhere on the page (an @graph node): read there
        org = next((n for n in nodes if n.get("@id") == org["@id"] and n.get("name")), org)
    employer = str((org.get("name") if isinstance(org, dict) else org) or "").strip()
    if not employer:
        # never "Unknown employer": every such page would share one employer, so two employers' openings could become
        # one job and an excluded employer pass the gate (the red team before Codex Pass 15, RT-32)
        raise SavedPostingError("the saved page names no employer; paste its text with --company instead")
    employer = htmllib.unescape(employer)
    url = (_tag_attr(markup, "link", "rel", "canonical", "href") or _tag_attr(markup, "meta", "property", "og:url", "content")
           or (m.group(1) if (m := _SAVED_FROM.search(markup)) else None))
    # the opening's own link, its "#" route kept (Codex H10, Pass 9); a relative JSON-LD link is read against the page's
    # own address, and a link that is still not a full address is kept as written, never dropped: a posting with a
    # link is never matched as one without (D179)
    link = str(p.get("url") or url or "").strip()
    if link and url and not posting_link(link):
        link = urljoin(url, link)
    url = posting_link(link) or link or None
    identifier = p.get("identifier")
    ident = identifier.get("value") if isinstance(identifier, dict) else identifier
    # a requisition as the text routes read one: at least three characters with a digit; a placeholder ("TBD", "N/A")
    # names no posting (RT-16; the red team's fr1-saved-3)
    ident = str(ident).strip() if ident is not None else None
    if ident and not (re.search(r"\d", ident) and 3 <= len(ident) <= 40):
        ident = None
    text = html_to_text(p.get("description") or "")
    workplace = "remote" if str(p.get("jobLocationType", "")).upper() == "TELECOMMUTE" else None
    employment = p.get("employmentType")
    # a list is kept whole: its first item is not what the page states (BATCH_5 D1)
    employment = ", ".join(str(e) for e in employment) if isinstance(employment, list) else employment
    location = _location(p, nodes)
    raw = RawPosting(ats="manual", board_token=re.sub(r"[^a-z0-9]+", "-", norm(employer)).strip("-") or "employer",
                     external_id=(re.sub(r"[^A-Za-z0-9_.\-]+", "-", ident).strip("-.") if ident else
                                  text_fingerprint(url or text or fallback_name)[:16]),
                     title=htmllib.unescape(str(p.get("title") or "")).strip(), location=location, url=url,
                     apply_url=url, posted_at=from_external(p.get("datePosted")), description_text=text,
                     # the page's identifier, else the requisition its text states, as every other route reads one (the
                     # red team's gr1-saved-4)
                     requisition_id=str(ident) if ident else requisition_in(text), employment_type_hint=employment,
                     workplace_hint=workplace,
                     comp=_salary(p), raw={"source_file": fallback_name, "job_posting": p, "reader": READER,
                                           # where the location came from: none when the page states none (BATCH_5 D1)
                                           "location_source": "file" if location else "none"})
    if not raw.title:
        raise SavedPostingError("saved page has no job title")
    return raw, employer


def parse_saved_text(text: str, fallback_name: str) -> tuple[RawPosting, str]:
    # a byte-order mark never hides the first header line (the red team before Codex Pass 15, RT-08)
    # made canonical once, and every content reader (the description, the requisition, the location) reads that one
    # text (the D190 design, section 3a, point 4; Codex's design review Pass 5: the requisition was read from the raw body)
    text = canonical(split_newlines(text).lstrip("\ufeff"))
    header, _, body = text.partition("\n\n")   # Codex CR-2026-11-15-GATE-FIXES H3, Pass 7
    fields, places, other = {}, [], []
    for line in header.split("\n"):
        key, sep, value = line.partition(":")
        name = key.strip().lower()
        if sep and name in ("url", "title", "employer"):
            fields[name] = value.strip()
        elif (labelled := _LOCATION.match(line)) is not None:
            # every line labelled as the location ("Location:", "Work Location:", "Job Location:", "Location - ..."), as
            # a paste reads it, is the location field, in order: one never replaces another (RT-29; the red team's
            # fr1-saved-4)
            if labelled.group(1).strip() not in places:
                places.append(labelled.group(1).strip())
        elif line.strip():
            other.append(line.strip())
    if places:
        fields["location"] = "; ".join(places)
    if other:
        # any other header line ("Job ID: R-4402", "Pay: ...", a first paragraph) is part of the posting's text, never
        # dropped: its requisition is read, and two openings that differ only there stay two (RT-10)
        body = "\n".join(other) + "\n\n" + body
    if not fields.get("title") or not fields.get("employer") or not body.strip():
        raise SavedPostingError("text file needs 'Title:' and 'Employer:' header lines, a blank line, then the posting text")
    # the opening's own link, its "#" route kept (Codex H10, Pass 9); a link that is not a full address is kept as
    # written, never dropped (D179)
    url = posting_link(fields.get("url")) or (fields.get("url") or "").strip() or None
    employer = fields["employer"]
    raw = RawPosting(ats="manual", board_token=re.sub(r"[^a-z0-9]+", "-", norm(employer)).strip("-") or "employer",
                     external_id=text_fingerprint(url or body)[:16], title=fields["title"], location=fields.get("location", ""),
                     url=url, apply_url=url, description_text=clean_text(body), requisition_id=requisition_in(body),
                     raw={"source_file": fallback_name, "text": body, "reader": READER,
                          "location_source": "file" if fields.get("location") else "none"})
    return raw, employer


def parse_saved_file(path: Path) -> tuple[RawPosting, str]:
    content = Path(path).read_text(encoding="utf-8", errors="replace")   # a byte-order mark: parse_saved_text (RT-08)
    if Path(path).suffix.lower() in (".html", ".htm"):
        return parse_saved_html(content, Path(path).name)
    return parse_saved_text(content, Path(path).name)
