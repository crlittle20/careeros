"""Certification requirements (2026-10-15): which certification a posting names, who issues it, how the posting words
the requirement, whether you hold it, how well your credentials match, and what that does to the decision.

The posting is read by the rules extractor (`careeros/extract/rules.py`), which stores one requirement record per
certification it finds: a credential from the policy's catalog (`pmp`), a family (`family:scrum`) or an issuer
(`issuer:aapc`) when the posting names no specific credential, or `unspecified certification`. Its requirement type is
the existing vocabulary (hard_required, required_with_equivalency, ambiguous, preferred, responsibility).

Matching against your profile:
  candidate_status  held | in_progress | planned | not_held | unknown   (a profile item without a status is held)
  match_type        exact | equivalent | family_match | related | none | unknown
Only a HELD credential satisfies a requirement: exactly, through an owner-approved equivalence (`equivalent` in the
policy), or, when the posting asks only for a family or an issuer, through any held credential of that family. A
different credential of the same family is `related` and never satisfies a named credential. A line that offers
alternatives ("PMP or CBAP") is satisfied by any one of them.

Decision effects follow the rules CareerOS already had: a named credential on a hard-required line that you do not
hold rejects the job (`certification_required`); everything else is a flag to check or nothing. A held preferred
credential is a `positive_signal` for the explanation only: certifications add no points to any score.
"""
from __future__ import annotations

import re

from careeros.extract.model import requirement_strength
from careeros.normalize.posting import contains_term, norm

from .profile import Profile

STATUSES = ("held", "in_progress", "planned")
SATISFYING = ("exact", "equivalent", "family_match")
REQUIRED = ("hard_required", "required_with_equivalency", "ambiguous")
_OR_LINE = re.compile(r"\bor\b|\band/or\b|\b(?:one|any|either) of\b", re.I)
_CUES = re.compile(r"\b(must|required|minimum|mandatory|at least|strongly desired|strongly preferred|preferred|desired|"
                   r"desirable|a plus|nice to have|ideally|bonus|not required|optional|or equivalent|or similar)\b", re.I)


def catalog(policy) -> dict:
    data = policy.get("certifications", default={}) or {}
    return {"credentials": data.get("credentials") or {}, "families": data.get("families") or {},
            "issuers": data.get("issuers") or {}, "equivalent": data.get("equivalent") or [],
            "context_only": data.get("context_only") or []}


def describe(name: str, policy) -> dict:
    """What a stored certification name stands for: a catalog credential, a family, an issuer, or unknown."""
    cat = catalog(policy)
    if name in cat["credentials"]:
        item = cat["credentials"][name]
        return {"key": name, "kind": "credential", "name": item["name"], "issuer": item.get("issuer"), "family": item.get("family")}
    if name.startswith("family:") and name[7:] in cat["families"]:
        fam = name[7:]
        item = cat["families"][fam]
        return {"key": name, "kind": "family", "name": item["name"], "issuer": item.get("issuer"), "family": fam}
    issuers = {i.lower(): (i, fam) for i, fam in cat["issuers"].items()}
    if name.startswith("issuer:") and name[7:] in issuers:
        issuer, fam = issuers[name[7:]]
        return {"key": name, "kind": "issuer", "name": f"{issuer} certification", "issuer": issuer, "family": fam}
    key = credential_key(name, policy)   # a free-text name, e.g. from the AI reader
    if key:
        return describe(key, policy)
    family = next((fam for fam, item in cat["families"].items()
                   if any(contains_term(norm(name), term) for term in item.get("terms") or [])), None)
    if family:
        return describe(f"family:{family}", policy)
    if name == "unspecified certification":
        return {"key": name, "kind": "unspecified", "name": "Certification (not named)", "issuer": None, "family": None}
    return {"key": name, "kind": "unknown", "name": name, "issuer": None, "family": None}


def credential_key(text: str, policy) -> str | None:
    """The catalog credential a free text names (the longest alias that appears in it), or None."""
    normed = norm(text)
    best = None
    for key, item in catalog(policy)["credentials"].items():
        for alias in item.get("aliases") or []:
            if contains_term(normed, alias) and (best is None or len(norm(alias)) > best[1]):
                best = (key, len(norm(alias)))
    return best[0] if best else None


def profile_credentials(profile: Profile, policy) -> list[dict]:
    """Your profile's certifications with their catalog identity and status (held unless the item says otherwise)."""
    cat = catalog(policy)["credentials"]
    out = []
    for cert in profile.data.get("certifications") or []:
        if not isinstance(cert, dict) or not cert.get("name"):
            continue
        key = credential_key(cert["name"], policy) or next(
            (k for m in cert.get("matches") or [] if (k := credential_key(m, policy))), None)
        status = cert.get("status", "held")
        item = cat.get(key) or {}
        out.append({"name": cert["name"], "key": key, "family": item.get("family"), "issuer": item.get("issuer"),
                    "status": status if status in STATUSES else "unknown"})
    return out


def _best(items: list) -> dict | None:
    order = {"held": 0, "in_progress": 1, "planned": 2, "unknown": 3}
    return min(items, key=lambda i: order.get(i["status"], 9)) if items else None


def _equivalents(key: str, policy) -> set:
    return {b if a == key else a for a, b in (tuple(pair) for pair in catalog(policy)["equivalent"] if len(pair) == 2)
            if key in (a, b)}


def match(described: dict, mine: list, policy) -> dict:
    """Candidate status, match type and whether the requirement is satisfied, for one described certification."""
    kind = described["kind"]
    if kind in ("unspecified", "unknown"):
        return {"candidate_status": "unknown", "match_type": "unknown", "matched_credential": None, "satisfied": None}
    if kind == "credential":
        found = _best([c for c in mine if c["key"] == described["key"]])
        how = "exact"
        if not found:
            found, how = _best([c for c in mine if c["key"] in _equivalents(described["key"], policy)]), "equivalent"
        if not found:
            found, how = _best([c for c in mine if c["family"] and c["family"] == described["family"]]), "related"
    else:  # a family or an issuer: any credential of that family (or from that issuer) answers it
        found = _best([c for c in mine if (c["family"] and c["family"] == described["family"])
                       or (kind == "issuer" and c["issuer"] == described["issuer"])])
        how = "family_match"
    if not found:
        return {"candidate_status": "not_held", "match_type": "none", "matched_credential": None, "satisfied": False}
    status = found["status"] if how != "related" else "not_held"
    satisfied = how in SATISFYING and found["status"] == "held"
    return {"candidate_status": found["status"] if how != "related" else status, "match_type": how,
            "matched_credential": found["name"], "matched_status": found["status"], "satisfied": satisfied}


def _clause(text: str, described: dict, policy) -> str:
    """The sentence of the posting line that mentions this certification (for its wording)."""
    from careeros.extract.rules import clauses
    cat = catalog(policy)
    words = (cat["credentials"].get(described["key"], {}).get("aliases") or []) + \
        (cat["families"].get(described["family"] or "", {}).get("terms") or []) + [described["issuer"] or ""]
    for part in clauses(text or ""):
        if any(w and contains_term(norm(part), w) for w in words):
            return part
    return text or ""


def analyse(requirements: list, profile: Profile, policy) -> list[dict]:
    """One record per certification requirement: identity, wording, your status, the match and the decision effect."""
    mine = profile_credentials(profile, policy)
    records = []
    for index, r in enumerate(requirements):
        if r.category != "certification":
            continue
        described = describe(r.name, policy)
        clause = _clause(r.source_text, described, policy)
        cue = _CUES.search(clause)
        records.append({"index": index, "raw_text": (r.source_text or "")[:300], "mention": clause[:200],
                        "normalized_name": described["name"], "certification_key": described["key"], "kind": described["kind"],
                        "issuer": described["issuer"], "cert_family": described["family"],
                        "requirement_type": r.requirement_type, "requirement_strength": requirement_strength(r.requirement_type),
                        "wording": cue.group(0).lower() if cue else None, **match(described, mine, policy)})
    _alternatives(records)
    for record in records:
        record["decision_effect"] = _effect(record)
    return records


def _alternatives(records: list) -> None:
    """Certifications offered as alternatives on one line ("PMP or CBAP"): any one held satisfies them all."""
    groups: dict = {}
    for record in records:
        if _OR_LINE.search(record["raw_text"]):
            groups.setdefault((record["raw_text"], record["requirement_type"]), []).append(record)
    for members in groups.values():
        if len(members) < 2:
            continue
        names = [m["normalized_name"] for m in members]
        winner = next((m for m in members if m["satisfied"]), None)
        for m in members:
            m["alternatives"] = [n for n in names if n != m["normalized_name"]]
            if winner and m is not winner:
                m["satisfied_by_alternative"] = winner["normalized_name"]


def _effect(record: dict) -> str:
    rtype = record["requirement_type"]
    satisfied = record["satisfied"] or bool(record.get("satisfied_by_alternative"))
    if rtype not in REQUIRED and rtype != "preferred":
        return "not_a_requirement"           # a duty or on-the-job training ("obtain the certification within 90 days")
    if rtype == "preferred":
        return "positive_signal" if satisfied else "none"
    if satisfied:
        return "requirement_satisfied"
    if rtype == "hard_required" and record["kind"] == "credential":
        return "hard_failure"
    return "check_required"


def gate_groups(records: list) -> list[list[dict]]:
    """Records grouped as the gate judges them: one group per set of alternatives, otherwise one record each."""
    groups: dict = {}
    for record in records:
        key = (record["raw_text"], record["requirement_type"]) if record.get("alternatives") else ("single", record["index"])
        groups.setdefault(key, []).append(record)
    return list(groups.values())


def status_note(record: dict) -> str:
    """'(yours is planned, not counted as held)' or '(you hold X: related, not counted)', for gate details."""
    if record["match_type"] == "related" and record.get("matched_credential"):
        return f" (you hold {record['matched_credential']}: related, not counted)" if record.get("matched_status") == "held" \
            else f" (your related {record['matched_credential']} is {record.get('matched_status')})"
    if record["candidate_status"] in ("planned", "in_progress"):
        return f" (yours is {record['candidate_status'].replace('_', ' ')}, not counted as held)"
    return ""


STATUS_WORDS = {"held": "held", "in_progress": "in progress (not counted as held)", "planned": "planned (not counted as held)",
                "not_held": "not held", "unknown": "unknown"}
MATCH_WORDS = {"exact": "exact match", "equivalent": "equivalent (owner-approved)", "family_match": "same family",
               "related": "related credential only", "none": "no match", "unknown": "cannot be matched (not named)"}
EFFECT_WORDS = {"requirement_satisfied": "requirement met", "hard_failure": "rejects the job (hard requirement not held)",
                "check_required": "check before applying", "positive_signal": "a plus you have (no score effect)",
                "none": "no effect", "not_a_requirement": "on-the-job, not a requirement"}


def summary(record: dict) -> str:
    """One short item for a review card: 'PMP (preferred): not held'."""
    status = STATUS_WORDS.get(record["candidate_status"], record["candidate_status"]).split(" (")[0]
    if record["match_type"] in ("family_match", "related", "equivalent") and record.get("matched_credential"):
        status += f", {MATCH_WORDS[record['match_type']]}"
    return f"{record['normalized_name']} ({record['requirement_type'].replace('_', ' ')}): {status}"


def explain(records: list) -> list[str]:
    """Deterministic lines from the stored records, one per certification."""
    lines = []
    for r in records:
        parts = [r["normalized_name"]]
        if r["issuer"]:
            parts.append(f"issuer {r['issuer']}")
        parts.append(r["requirement_type"].replace("_", " ") + (f" (\"{r['wording']}\")" if r.get("wording") else ""))
        status = STATUS_WORDS.get(r["candidate_status"], r["candidate_status"])
        if r.get("matched_credential") and r["match_type"] in ("related", "family_match", "equivalent"):
            status += f": you hold {r['matched_credential']}" if r.get("matched_status") == "held" else \
                f": your {r['matched_credential']} is {STATUS_WORDS.get(r.get('matched_status'), r.get('matched_status'))}"
        parts += [status, MATCH_WORDS.get(r["match_type"], r["match_type"])]
        if r.get("satisfied_by_alternative"):
            parts.append(f"met by the alternative {r['satisfied_by_alternative']}")
        parts.append(f"effect: {EFFECT_WORDS.get(r['decision_effect'], r['decision_effect'])}")
        lines.append(" · ".join(parts) + f" · from: \"{r['mention']}\"")
    return lines or ["Certifications: none mentioned."]
