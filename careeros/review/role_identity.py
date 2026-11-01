"""Role identity for the shadow evaluator (SCORING_SHADOW_SPEC 2026-10-16, section 9). PURE: job rows in, groups out.

The unit of evaluation is the role, not the posting: one opening listed in two cities is one decision for you, so it
must count once. Identity levels between two postings, strongest first:
  confirmed  the same requisition at the same employer, the same canonical URL, or a link CareerOS made on that kind
             of evidence (a duplicate, or postings linked as the same opening)
  strong     same employer, title, department, posting date and requirements fingerprint, with posting text
             similarity >= 0.99 (the cross-city pairs no dedupe key catches)
  possible   same employer and title only
  none
Two postings with different known requisitions are separate vacancies: never more than possible.
The primary analysis groups at strong or above; the ungrouped run is a sensitivity check.
"""
from __future__ import annotations

import difflib
from collections import Counter
from itertools import combinations

from careeros.normalize.posting import norm

LEVELS = ("none", "possible", "strong", "confirmed")
STRONG_SIMILARITY = 0.99


def similar(a: str | None, b: str | None, threshold: float = STRONG_SIMILARITY) -> bool:
    """Word-level similarity of two posting texts at or above `threshold`."""
    ta, tb = norm(a).split(), norm(b).split()
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return matcher.real_quick_ratio() >= threshold and matcher.quick_ratio() >= threshold and matcher.ratio() >= threshold


def identity(a: dict, b: dict, linked: set[frozenset], threshold: float = STRONG_SIMILARITY) -> str:
    """The identity level of two postings. `linked` holds pairs CareerOS already joined on confirmed evidence."""
    same_employer = norm(a.get("parent")) == norm(b.get("parent"))
    req_a, req_b = (a.get("requisition_id") or "").strip(), (b.get("requisition_id") or "").strip()
    same_title = (a.get("title_norm") or "") == (b.get("title_norm") or "")
    if same_employer and req_a and req_b and req_a != req_b:
        return "possible" if same_title else "none"
    if frozenset((a["job_id"], b["job_id"])) in linked:
        return "confirmed"
    if same_employer and req_a and req_a == req_b:
        return "confirmed"
    if a.get("canonical_url") and a.get("canonical_url") == b.get("canonical_url"):
        return "confirmed"
    if not (same_employer and same_title):
        return "none"
    if ((a.get("department") or "") == (b.get("department") or "")
            and (a.get("posted_at") or "")[:10] == (b.get("posted_at") or "")[:10]
            and a.get("requirements_fingerprint") and a.get("requirements_fingerprint") == b.get("requirements_fingerprint")
            and similar(a.get("description_text"), b.get("description_text"), threshold)):
        return "strong"
    return "possible"


def _candidate_pairs(jobs: list[dict], linked: set[frozenset]) -> set[tuple[str, str]]:
    """Pairs worth judging: same employer and title, same employer and requisition, same canonical URL, or linked."""
    buckets: dict[tuple, list[str]] = {}
    for job in jobs:
        employer = norm(job.get("parent"))
        buckets.setdefault(("title", employer, job.get("title_norm") or ""), []).append(job["job_id"])
        if (job.get("requisition_id") or "").strip():
            buckets.setdefault(("req", employer, job["requisition_id"].strip()), []).append(job["job_id"])
        if job.get("canonical_url"):
            buckets.setdefault(("url", job["canonical_url"]), []).append(job["job_id"])
    ids = {job["job_id"] for job in jobs}
    pairs = {tuple(sorted(p)) for members in buckets.values() for p in combinations(sorted(members), 2)}
    pairs |= {tuple(sorted(p)) for p in linked if len(p) == 2 and p <= ids}
    return pairs  # type: ignore[return-value]


def group_roles(jobs: list[dict], linked: set[frozenset], at: str = "strong",
                threshold: float = STRONG_SIMILARITY) -> tuple[dict[str, list[str]], Counter]:
    """({group id: sorted member ids}, pair counts per identity level). A group joins every pair at level `at` or
    stronger; its id is its smallest job id. Deterministic for the same input."""
    if at not in LEVELS[1:]:
        raise ValueError(f"unknown identity level: {at}")
    by_id = {job["job_id"]: job for job in jobs}
    counts: Counter = Counter()
    joined = []
    for a, b in sorted(_candidate_pairs(jobs, linked)):
        level = identity(by_id[a], by_id[b], linked, threshold)
        counts[level] += 1
        if LEVELS.index(level) >= LEVELS.index(at):
            joined.append((a, b))
    # A group never holds two different known requisitions of one employer, and a posting without a requisition that
    # would join two such groups stays on its own (independent Codex review of f984d15, 2026-10-20: R-1, a posting with
    # no requisition and R-2 became one group through the middle one).
    alone: set[str] = set()
    while True:
        root, bridges = _join(by_id, joined, alone)
        if not bridges:
            break
        alone |= bridges
    groups: dict[str, list[str]] = {}
    for job_id in sorted(by_id):
        groups.setdefault(root[job_id], []).append(job_id)
    return {min(members): members for members in groups.values()}, counts


def _requisitions(job: dict) -> dict[str, set[str]]:
    """{employer: {its known requisition}} for one posting; empty when it has none."""
    req = (job.get("requisition_id") or "").strip()
    return {norm(job.get("parent")): {req}} if req else {}


def _join(by_id: dict[str, dict], joined: list[tuple[str, str]], alone: set[str]) -> tuple[dict[str, str], set[str]]:
    """One grouping pass: (each posting's group root, the postings without a requisition that would join two groups
    holding different known requisitions of one employer). Postings in `alone` join nothing."""
    parent = {job_id: job_id for job_id in by_id}
    reqs = {job_id: _requisitions(job) for job_id, job in by_id.items()}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def compatible(ra: str, rb: str) -> bool:
        return all(len(reqs[ra][e] | reqs[rb][e]) <= 1 for e in reqs[ra].keys() & reqs[rb].keys())

    for a, b in joined:
        ra, rb = find(a), find(b)
        if a not in alone and b not in alone and ra != rb and compatible(ra, rb):
            keep, drop = min(ra, rb), max(ra, rb)
            parent[drop] = keep
            for employer, found in reqs[drop].items():
                reqs[keep].setdefault(employer, set()).update(found)
    bridges = {j for a, b in joined for j, n in ((a, b), (b, a)) if j not in alone and n not in alone
               and not _requisitions(by_id[j]) and find(j) != find(n) and not compatible(find(j), find(n))}
    return {job_id: find(job_id) for job_id in by_id}, bridges


def ungrouped(jobs: list[dict]) -> dict[str, list[str]]:
    return {job["job_id"]: [job["job_id"]] for job in sorted(jobs, key=lambda j: j["job_id"])}
