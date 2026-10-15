"""Second review for title-screen rejects (2026-10-12, Part 3).

About 97% of scanned postings fail the title screen. Most are right to (engineers, clinicians, sales), but ambiguous
titles ("Operations Specialist", "Member Support Associate", "Quality Coordinator", "Analyst II") can hide relevant
work. This review is deterministic, uses only what the allowed feeds already gave us (department and description), and
never calls a model:

1. prefilter: the title holds an ambiguous role word, and no excluded title term;
2. reject signals win: software QA, licensed clinical work, contact-center call-quality monitoring (NIGHT may still
   consider that work; PRIMARY does not), sales;
3. rescue only with strong evidence in the role's OWN duties and requirements (never the company's "About us" or
   benefits text): a relevant department plus 2 distinct claims/payer/PBM context terms, or 3 without one.

Every decision is recorded (title_reviews) with its rule, evidence and the original rejection reason, so recall gains
can be measured later. Rescues per run are capped (`title_rescue.max_rescues_per_run`); the rest wait for the next run.
"""
from __future__ import annotations

from careeros.normalize.posting import contains_term, norm

RESCUE_VERSION = "rescue-2026-10-12c"   # b: skip what the screen rejects anyway; c: judge duties, not company blurbs


def _cfg(policy) -> dict:
    return policy.get("title_rescue", default={}) or {}


def skipped_title(title_norm: str, policy) -> bool:
    """Titles never worth a rescue for you (business development, partnerships, sales...), matched on the title only."""
    return any(contains_term(title_norm, norm(t)) for t in _cfg(policy).get("skip_title_terms", []))


def prefilter(title_norm: str, policy) -> bool:
    cfg = _cfg(policy)
    return bool(cfg.get("enabled")) and any(contains_term(title_norm, norm(t)) for t in cfg.get("ambiguous_title_terms", []))


def _hits(text: str, terms) -> list[str]:
    return sorted({t for t in terms or [] if contains_term(text, norm(t))})


def _role_text(job: dict, policy) -> str:
    """The posting's own duties and requirements, never the company's "About us" or benefits text: an employer whose
    blurb mentions payers and claims must not make every one of its postings look relevant (live finding, 2026-10-12)."""
    from careeros.extract.rules import _heading
    raw = (job.get("description_text") or "")[:12000]
    kept, section, structured = [], None, False
    for line in raw.split("\n"):
        found = _heading(line)
        if found:
            section, structured = found, True
            continue
        if section in ("required", "preferred", "ambiguous", "responsibilities"):
            kept.append(line)
    return norm(" ".join([job.get("title") or ""] + kept)) if structured else norm(f"{job.get('title') or ''} {raw}")


def review(job: dict, policy) -> dict:
    """{'outcome': 'rescued' | 'kept_rejected', 'family', 'rule', 'evidence'} for one title-screen reject."""
    cfg = _cfg(policy)
    text = norm(f"{job.get('title') or ''} {(job.get('description_text') or '')[:8000]}")
    department = norm(job.get("department") or "")
    for kind, terms in (cfg.get("reject_signals") or {}).items():
        hits = _hits(text, terms)
        if hits:
            return {"outcome": "kept_rejected", "family": None, "rule": f"reject_signal:{kind}", "evidence": ", ".join(hits[:4])}
    context = _hits(_role_text(job, policy), cfg.get("context_terms"))  # duties / requirements only
    dept = _hits(department, cfg.get("department_terms")) if department else []
    if (dept and len(context) >= 2) or len(context) >= 3:  # counted in the role's own duties / requirements
        data_terms = _hits(text, cfg.get("data_terms"))
        family = "data_reporting" if len(data_terms) > len(context) else cfg.get("rescue_family", "pbm_claims_operations")
        evidence = (f"department '{job.get('department')}'; " if dept else "") + "context: " + ", ".join(context[:6])
        return {"outcome": "rescued", "family": family, "rule": "department_and_context" if dept else "strong_context",
                "evidence": evidence}
    return {"outcome": "kept_rejected", "family": None, "rule": "weak_evidence",
            "evidence": f"{len(context)} context terms" + (f"; department '{job.get('department')}'" if job.get("department") else "")}
