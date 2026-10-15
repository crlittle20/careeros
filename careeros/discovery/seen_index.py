"""The seen-jobs index (prompts v2.5): a sanitized, ChatGPT-readable list of the jobs CareerOS has already seen,
published to the private inbox repository as `seen_jobs.json` so ChatGPT can skip them before spending effort.

JOB FACTS ONLY. Each record holds: fingerprint, normalized_company, requisition_id, normalized_title,
normalized_location, canonical_url, urls, first_seen_date, last_seen_date, source. Never: your name, profile, resume,
application answers, Apply/Stretch/Skip labels, eligibility decisions or scores, rejection reasons, recruiter mail,
email addresses, Gmail IDs, pay history or notes. `check_record` enforces that before anything is published.

The local database stays the authoritative ledger; this file is rebuilt from it on every run, deterministically, so
publishing is idempotent (an unchanged index is never re-committed).

One fingerprint per requisition, across LinkedIn, Indeed, employer and recruiter URLs: observations (tracked jobs and
inbox/alert leads) are merged when they share a job, a cleaned URL, the same company + requisition id, or the same
company + title with no conflicting requisition ids. A different requisition id is never merged, even under the same
title. Tracking parameters never create a new fingerprint: only identifying query parameters survive cleaning.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from careeros.normalize.posting import norm

from .alerts import canonical_job_url
from .note import context_note

SCHEMA = "careeros.seen_jobs/1"
FIELDS = ("fingerprint", "normalized_company", "requisition_id", "normalized_title", "normalized_location",
          "canonical_url", "urls", "first_seen_date", "last_seen_date", "source")
# Query parameters that identify a posting; every other parameter (utm_*, trk, refId, trackingId, src, ...) is dropped.
ID_PARAMS = {"gh_jid", "jk", "vjk", "jl", "joblistingid", "currentjobid", "jobid", "job_id", "jobreqid", "reqid",
             "requisitionid", "req", "id", "pid", "postingid", "job"}
MAX_URLS = 8
_SUFFIXES = re.compile(r"\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc|lp|llp|holdings)\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def clean_url(raw: str | None) -> tuple[str, str] | None:
    """(cleaned job URL, site) with tracking removed, or None when the link does not look like a job posting."""
    try:
        canonical = canonical_job_url(raw or "")
    except ValueError:
        return None
    if canonical is None:
        return None
    url, site, job_like = canonical
    parts = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() in ID_PARAMS)
    if not (job_like or query):  # a careers search page carrying ?gh_jid= still identifies one posting
        return None
    host = parts.netloc.lower()
    return f"https://{host}{parts.path.rstrip('/')}" + (f"?{urlencode(query)}" if query else ""), site


def norm_company(name: str | None) -> str:
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", norm(name or ""))).strip()


def norm_req(req: str | None) -> str | None:
    value = re.sub(r"[^A-Za-z0-9]", "", req or "").upper()
    return value or None


def _location(raw: str | None) -> str | None:
    text = norm(raw or "")
    if not text:
        return None
    if "remote" in text.split():
        return "remote" + (" us" if re.search(r"\b(us|usa|united states)\b", text) else "")
    return text if len(text) <= 60 else text[:60].rsplit(" ", 1)[0]  # whole words only


def _observations(store) -> list[dict]:
    obs = []
    night_family = {r["job_id"] for r in store.all("SELECT DISTINCT job_id FROM night_results WHERE family IS NOT NULL")}
    for job in store.all("SELECT job_id, employer_name, parent, requisition_id, title_norm, family, location_raw, url, "
                         "canonical_url, ats, first_seen_at, last_seen_at FROM jobs"):
        if not (job["family"] or job["job_id"] in night_family):
            continue  # outside every target family: never something ChatGPT would recommend
        cleaned = clean_url(job["canonical_url"] or job["url"])
        obs.append({"key": f"job:{job['job_id']}", "job": job["job_id"], "company": norm_company(job["employer_name"] or job["parent"]),
                    "req": norm_req(job["requisition_id"]), "title": job["title_norm"], "location": _location(job["location_raw"]),
                    "url": cleaned[0] if cleaned else None, "first": job["first_seen_at"], "last": job["last_seen_at"],
                    "source": job["ats"] if job["ats"] != "manual" else (cleaned[1] if cleaned else "employer")})
    for lead in store.all("SELECT lead_id, url, site, title_norm, context, job_id, first_seen_at FROM leads"):
        note = context_note(lead["context"])
        company = norm_company((lead["context"] or "").split(" · ")[0]) if lead["context"] else ""
        cleaned = clean_url(lead["url"])
        source = note["fields"].get("source") or (cleaned[1] if cleaned else lead["site"]) or "other"
        obs.append({"key": f"lead:{lead['lead_id']}", "job": lead["job_id"], "company": company,
                    "req": norm_req(note["fields"].get("req")), "title": lead["title_norm"], "location": None,
                    "url": cleaned[0] if cleaned else None, "first": lead["first_seen_at"], "last": lead["first_seen_at"],
                    "source": re.sub(r"[^a-z0-9_]", "_", source.lower())[:30]})
    return obs


class _Groups:
    def __init__(self, items: list[dict]):
        self.parent = list(range(len(items)))
        self.reqs = [{i["req"]} - {None} for i in items]

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int, *, guard: bool = False) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if guard and self.reqs[ra] and self.reqs[rb] and self.reqs[ra] != self.reqs[rb]:
            return  # two different requisitions under the same title: distinct jobs
        self.parent[rb] = ra
        self.reqs[ra] |= self.reqs[rb]


def _merge(obs: list[dict]) -> list[list[dict]]:
    groups = _Groups(obs)
    first: dict = {}

    def link(key, i, guard=False):
        if key in first:
            groups.union(first[key], i, guard=guard)
        else:
            first[key] = i

    for i, o in enumerate(obs):  # the same job, the same cleaned URL, the same company + requisition
        if o["job"]:
            link(("job", o["job"]), i)
        if o["url"]:
            link(("url", o["url"]), i)
        if o["req"] and o["company"]:
            link(("req", o["company"], o["req"]), i)
    for i, o in enumerate(obs):  # then the same company + title, unless their requisition ids differ
        if o["company"] and o["title"]:
            link(("title", o["company"], o["title"]), i, guard=True)
    out: dict = {}
    for i, o in enumerate(obs):
        out.setdefault(groups.find(i), []).append(o)
    return list(out.values())


def _record(group: list[dict]) -> dict:
    group = sorted(group, key=lambda o: (o["first"] or "", o["key"]))
    reqs = sorted({o["req"] for o in group if o["req"]})
    company = next((o["company"] for o in group if o["company"]), "")
    title = next((o["title"] for o in group if o["title"]), "")
    urls = sorted({o["url"] for o in group if o["url"]})
    aggregators = ("linkedin.com", "indeed.com", "glassdoor.")
    employer_urls = [u for u in urls if not any(a in u for a in aggregators)]
    basis = f"req|{company}|{reqs[0]}" if len(reqs) == 1 else f"title|{company}|{title}|{','.join(reqs)}"
    record = {
        "fingerprint": hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16],
        "normalized_company": company or None,
        "requisition_id": reqs[0] if len(reqs) == 1 else None,
        "normalized_title": title or None,
        "normalized_location": next((o["location"] for o in group if o["location"]), None),
        "canonical_url": (employer_urls or urls or [None])[0],
        "urls": urls[:MAX_URLS],
        "first_seen_date": min([d for d in ((o["first"] or "")[:10] for o in group) if d] or [None]),
        "last_seen_date": max([d for d in ((o["last"] or o["first"] or "")[:10] for o in group) if d] or [None]),
        "source": group[0]["source"],
    }
    return {k: v for k, v in record.items() if v not in (None, [], "")}


def check_record(record: dict) -> None:
    """Fail closed before publishing anything that is not a plain job fact."""
    unknown = set(record) - set(FIELDS)
    if unknown:
        raise ValueError(f"seen index: unexpected fields {sorted(unknown)}")
    text = json.dumps(record)
    if _EMAIL.search(text):
        raise ValueError("seen index: an email address-like value")
    for url in [record.get("canonical_url")] + list(record.get("urls") or []):
        if url and any(k.lower() not in ID_PARAMS for k, _ in parse_qsl(urlsplit(url).query)):
            raise ValueError("seen index: a URL with a non-identifying query parameter")


def build(store) -> list[dict]:
    """The index records, sorted by fingerprint: the same database always gives exactly the same output."""
    records = [_record(g) for g in _merge(_observations(store))]
    records = [r for r in records if r.get("normalized_title") and (r.get("normalized_company") or r.get("canonical_url"))]
    out = sorted(records, key=lambda r: r["fingerprint"])
    fingerprints = [r["fingerprint"] for r in out]
    if len(set(fingerprints)) != len(fingerprints):  # the merge failed to join one requisition: publish nothing
        raise ValueError("seen index: two records share a fingerprint")
    for r in out:
        check_record(r)
    return out


def files(records: list[dict], max_per_file: int = 1500) -> dict[str, str]:
    """{repo path: content}. One `seen_jobs.json` while small; past `max_per_file` records it becomes a manifest of 16
    shards (`seen_jobs/0.json` ... `seen_jobs/f.json`, by the first fingerprint character), each well under GitHub's
    1 MB contents-API limit. Content is stable JSON (sorted keys, no timestamps) so an unchanged index is byte-identical."""
    dump = lambda data: json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if len(records) <= max_per_file:
        return {"seen_jobs.json": dump({"schema": SCHEMA, "sharded": False, "record_count": len(records), "records": records})}
    shards = {f"{c:x}": [] for c in range(16)}
    for r in records:
        shards[r["fingerprint"][0]].append(r)
    out = {"seen_jobs.json": dump({"schema": SCHEMA, "sharded": True, "record_count": len(records),
                                   "shards": [f"seen_jobs/{k}.json" for k in sorted(shards)]})}
    for key, items in shards.items():
        out[f"seen_jobs/{key}.json"] = dump({"schema": SCHEMA, "shard": key, "records": items})
    return out


def intake_status(store) -> dict:
    """The Gmail-intake status ChatGPT's next run should use (prompts v2.5), from the "Gmail status:" lines of earlier
    inbox issues. Only dates and status words: no Gmail data of any kind."""
    reports = store.all("SELECT processed_at, gmail_status FROM inbox_issues WHERE gmail_status IS NOT NULL "
                        "ORDER BY processed_at, number")
    last = reports[-1] if reports else None
    last_ok = next((r for r in reversed(reports) if r["gmail_status"] == "ok"), None)
    last_bad = next((r for r in reversed(reports) if r["gmail_status"] != "ok"), None)
    recovering = bool(last and last["gmail_status"] != "ok")
    return {"schema": "careeros.intake_status/1",
            "gmail": {"last_reported_status": last["gmail_status"] if last else None,
                      "last_reported_date": last["processed_at"][:10] if last else None,
                      "last_ok_date": last_ok["processed_at"][:10] if last_ok else None,
                      "last_failure_date": last_bad["processed_at"][:10] if last_bad else None,
                      "normal_lookback_days": 3, "recovery_lookback_days": 7,
                      "next_run_lookback_days": 7 if recovering else 3}}


def intake_file(store) -> dict[str, str]:
    return {"intake_status.json": json.dumps(intake_status(store), indent=1, sort_keys=True) + "\n"}
