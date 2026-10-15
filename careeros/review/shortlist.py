"""Shortlist to action (2026-10-12, Part 10).

The shortlist = openings you labelled Apply or Stretch, plus Tier A/B picks, minus those you dismissed or that closed.
For each one it shows: evidence-backed reasons it fits, confirmed incompatibilities and open questions (eligibility
facts), listing status, the direct application link, whether you already applied or dismissed it, and the one next
useful action.

Drafts (`careeros draft <job>`) exist only after you chose to pursue an opening (an Apply / Stretch label). A draft is
an application brief built from your approved profile: the FACTUAL years and resume sources only (never the
screening-only rounding), the posting's requirements matched to that evidence, and honest gaps. It never invents
experience or credentials, and nothing is ever submitted.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from careeros.core.clock import parse_utc
from careeros.db.store import Store


def _latest_label(store: Store, job_id: str):
    return store.one("SELECT * FROM labels WHERE job_id = ? AND profile = 'primary' ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def shortlisted_job_ids(store: Store) -> list[str]:
    rows = store.all("""SELECT j.job_id FROM jobs j
                        LEFT JOIN scores s ON s.score_id = (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state NOT IN ('duplicate')
                          AND (s.tier IN ('A','B') OR (SELECT label FROM labels l WHERE l.job_id = j.job_id AND l.profile = 'primary'
                                                        ORDER BY at DESC, rowid DESC LIMIT 1) IN ('apply','stretch'))
                          AND NOT EXISTS (SELECT 1 FROM dismissals d WHERE d.job_id = j.job_id)
                        ORDER BY s.apply_priority DESC""")
    return [r["job_id"] for r in rows]


def listing_status(job) -> str:
    if job["current_state"] == "closed":
        return "closed"
    if (job["absent_scans"] or 0) > 0:
        return f"possibly removed (missing from {job['absent_scans']} recent scan{'s' if job['absent_scans'] != 1 else ''})"
    return "open"


def fit_reasons(components: dict) -> list[str]:
    out = []
    coverage = (components.get("eligibility") or {}).get("coverage") or {}
    core = [i for i in coverage.get("items", []) if i["importance"] == "core" and i["credit"] > 0]
    if core:
        out.append("core requirements you have: " + ", ".join(f"{i['requirement']} ({i['evidence']})" for i in core[:4]))
    if components.get("skill_coverage") is not None:
        out.append(f"weighted skill coverage {components['skill_coverage']:.0%}")
    domain = (components.get("domain") or {})
    if domain.get("level") in ("direct", "payer", "healthcare"):
        out.append(f"domain: {domain['level']} ({', '.join(domain.get('matched', [])[:3])})")
    return out


def next_action(item: dict) -> str:
    if item["status"] == "closed":
        return "none: the posting is closed"
    if item["applied"]:
        app = item["applied"]
        if app["current_state"] in ("applied", "acknowledged"):
            days = item.get("days_since_applied") or 0
            return "follow up with the recruiter" if days >= 7 else "wait for a response"
        return f"track the {app['current_state'].replace('_', ' ')} stage"
    if item["incompatible"]:
        return "dismiss it, or record why you would still apply: " + item["incompatible"][0]["detail"]
    if item["unknowns"]:
        return "verify before applying: " + item["unknowns"][0]["detail"]
    if item["possibly_applied"]:
        return "check: you applied to a similar opening (" + ", ".join(item["possibly_applied"][:2]) + ")"
    return "apply: " + (item["apply_url"] or "open the posting")


def shortlist(store: Store) -> list[dict]:
    from careeros.openings import application_status, conflicts
    now = store.clock.now()
    out = []
    for job_id in shortlisted_job_ids(store):
        job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        score = store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))
        comps = json.loads(score["components_json"]) if score else {}
        facts = comps.get("facts") or []
        status = application_status(store, job_id)
        item = {"job_id": job_id, "title": job["title"], "employer": job["employer_name"], "tier": score["tier"] if score else None,
                "state": score["state"] if score else None, "priority": score["apply_priority"] if score else None,
                "label": (_latest_label(store, job_id) or {"label": None})["label"], "status": listing_status(job),
                "apply_url": job["apply_url"] or job["url"], "fit": fit_reasons(comps),
                "incompatible": [f for f in facts if f["status"] == "incompatible"],
                "unknowns": [f for f in facts if f["status"] in ("unknown", "conflicting")],
                "conflicts": conflicts(store, job), "applied": status["applied"], "possibly_applied": status["possibly_applied"],
                "dismissed": status["dismissed"], "stretch": bool(comps.get("stretch"))}
        if status["applied"]:
            item["days_since_applied"] = (parse_utc(now) - parse_utc(status["applied"]["applied_at"])) / timedelta(days=1)
        item["next_action"] = next_action(item)
        out.append(item)
    return out


# --- drafts --------------------------------------------------------------------------------------------------
def draft(store: Store, profile, job_id: str, folder: Path) -> Path:
    """An application brief for an opening you chose to pursue, from approved profile facts only."""
    from careeros.pipeline import load_requirements
    label = _latest_label(store, job_id)
    if not label or label["label"] not in ("apply", "stretch"):
        raise ValueError("not_pursued: label it Apply or Stretch first (drafts are only for openings you chose to pursue)")
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    requirements = [r for r in load_requirements(store, job) if r.requirement_type != "responsibility"]
    items = {"skill": profile._items("skills"), "function": profile._items("functions"), "domain": profile._items("domains")}

    def evidence_for(name: str) -> dict | None:
        hit = profile.skill(name) or profile.function(name)
        if hit:
            return hit
        return next((d for d in items["domain"] if d["name"].lower() == name.lower()), None)

    def factual(item: dict) -> str:
        years = item.get("years")  # the FACT, never screening_years
        return f"{years:g} years" if isinstance(years, (int, float)) else "years not recorded"

    lines = [f"# Application brief: {job['title']} at {job['employer_name']}", "",
             f"- Link: {job['apply_url'] or job['url'] or 'not recorded'}",
             f"- Your label: {label['label']}" + (f" ({label['reason']})" if label["reason"] else ""),
             "- Built from your approved profile only. Years are the factual numbers from your resume (no screening rounding).",
             "  Edit freely; nothing here is submitted anywhere.", "", "## Requirements and your evidence", ""]
    gaps = []
    seen = set()
    for r in requirements:
        key = (r.category, r.name)
        if key in seen or r.category in ("work_authorization", "location"):
            continue
        seen.add(key)
        item = evidence_for(r.name) if r.category in ("skill", "domain", "experience", "years_experience") else None
        if item:
            source = item.get("source") or "your profile"
            lines.append(f"- **{r.name}** ({r.requirement_type.replace('_', ' ')}): {factual(item)}, {item.get('evidence', 'direct')}"
                         f" evidence. Source: {source}")
        else:
            gaps.append(r)
    lines += ["", "## Honest gaps (do not claim these)", ""]
    lines += [f"- {r.name} ({r.requirement_type.replace('_', ' ')}): \"{(r.source_text or '').strip()[:140]}\"" for r in gaps[:15]] or ["- none found"]
    lines += ["", "## Talking points (from your resume facts)", ""]
    for kind in ("function", "skill"):
        for item in items[kind]:
            if item.get("evidence") == "direct" and item.get("source"):
                lines.append(f"- {item['name']}: {factual(item)} ({item['source']})")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{store.clock.now()[:10]}-{job_id.replace(':', '_')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store.writer("review").event("draft_created", outcome="success", entity_type="job", entity_id=job_id,
                                 actor_type="human", actor_id="user", metadata={"path": path.name})
    return path
