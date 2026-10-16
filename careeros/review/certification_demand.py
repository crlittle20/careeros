"""Certification demand across your target jobs (2026-10-15): which certifications relevant postings ask for, how
(required, preferred, ...), how often over time, and your status for each. Read only, from observed jobs: nothing is
estimated, and nothing changes your plans. The `not_held_by_demand` list is information for your own decisions."""
from __future__ import annotations

from collections import Counter

from careeros.db.store import Store
from careeros.qualify import certifications


def demand(store: Store, policy, profile) -> dict:
    from careeros.pipeline import load_requirements
    jobs = store.all("""SELECT * FROM jobs WHERE family IS NOT NULL AND extraction_fingerprint IS NOT NULL
                        AND current_state != 'duplicate' AND exclude_from_kpi = 0 ORDER BY first_seen_at""")
    by_cert: dict = {}
    families: Counter = Counter()
    totals: Counter = Counter()
    for job in jobs:
        records = [r for r in certifications.analyse(load_requirements(store, job), profile, policy)
                   if r["decision_effect"] != "not_a_requirement"]
        if not records:
            continue
        totals["jobs_with_certification"] += 1
        totals["jobs_where_candidate_holds_one"] += any(r["satisfied"] for r in records)
        totals["jobs_where_candidate_lacks_a_required_one"] += any(r["decision_effect"] == "hard_failure" for r in records)
        totals["jobs_where_a_planned_certification_appears"] += any(r["candidate_status"] in ("planned", "in_progress") for r in records)
        month = (job["first_seen_at"] or "")[:7]
        seen: set = set()
        for r in records:
            name = r["normalized_name"]
            if r["cert_family"]:
                families[r["cert_family"]] += name not in seen
            entry = by_cert.setdefault(name, {"certification": name, "issuer": r["issuer"], "family": r["cert_family"],
                                              "kind": r["kind"], "your_status": r["candidate_status"], "jobs": 0,
                                              "hard_required": 0, "required_with_equivalency": 0, "ambiguous": 0,
                                              "preferred": 0, "jobs_where_you_lack_it_as_required": 0, "by_month": Counter()})
            if name in seen:
                continue
            seen.add(name)
            entry["jobs"] += 1
            entry[r["requirement_type"]] = entry.get(r["requirement_type"], 0) + 1
            entry["jobs_where_you_lack_it_as_required"] += r["decision_effect"] == "hard_failure"
            entry["by_month"][month] += 1
    rows = sorted(by_cert.values(), key=lambda e: (-e["jobs"], e["certification"]))
    for row in rows:
        row["by_month"] = dict(sorted(row["by_month"].items()))
    not_held = [{"certification": r["certification"], "issuer": r["issuer"], "jobs": r["jobs"], "required": r["hard_required"],
                 "preferred": r["preferred"], "your_status": r["your_status"]}
                for r in rows if r["kind"] == "credential" and r["your_status"] != "held"]
    return {"relevant_jobs": len(jobs), **{k: totals[k] for k in ("jobs_with_certification", "jobs_where_candidate_holds_one",
                                                                     "jobs_where_candidate_lacks_a_required_one",
                                                                     "jobs_where_a_planned_certification_appears")},
            "certifications": rows, "families": dict(families.most_common()), "not_held_by_demand": not_held,
            "note": "observed postings only; responsibilities and on-the-job training are not counted as requirements"}
