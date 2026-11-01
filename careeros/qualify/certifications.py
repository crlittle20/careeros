"""Certification requirements (2026-10-15): which certification a posting names, who issues it, how the posting words
the requirement, whether you hold it, how well your credentials match, and what that does to the decision.

The posting is read by the rules extractor (`careeros/extract/rules.py`), which stores one requirement record per
certification it finds: a credential from the policy's catalog (`pmp`), a family (`family:scrum`) or an issuer
(`issuer:aapc`) when the posting names no specific credential, or `unspecified certification`. Its requirement type is
the existing vocabulary (hard_required, required_with_equivalency, ambiguous, preferred, responsibility).

Matching against your profile (2026-10-16: every status is explicit):
  candidate_status  held | in_progress | planned | completed | conflicting | not_held | unknown
                    (a profile item without a status is UNKNOWN, never held; two profile items for one credential with
                    different statuses are CONFLICTING and never held, until you resolve them)
  kind              certification | applied_skill | course_certificate (a completed course never satisfies a requirement)
  evidence basis    self_reported | document_inspected | issuer_verified (shown; only issuer_verified is "verified")
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

STATUSES = ("held", "in_progress", "planned", "completed", "expired")
KINDS = ("certification", "applied_skill", "course_certificate")
COUNTING_KINDS = ("certification", "applied_skill")   # a course certificate, or a kind not recognised, never counts as held
_KIND_WORDS = {"course": "course_certificate", "course_certificate": "course_certificate", "certificate_course": "course_certificate"}


def _word(value) -> str:
    """'In progress', 'in-progress' and 'IN_PROGRESS' are one word: 'in_progress'."""
    return re.sub(r"[\s\-]+", "_", str(value).strip().lower()) if value is not None else ""


def normal_status(value) -> str:
    word = _word(value)
    return word if word in STATUSES else "unknown"


def normal_kind(value) -> str:
    """The recognised kind; anything else is 'unknown' and never counts as held (2026-10-18; it was read as a
    certification, so 'course certificate' written with a space satisfied a requirement)."""
    word = _word(value if value is not None else "certification")
    word = _KIND_WORDS.get(word, word)
    return word if word in KINDS else "unknown"
BASES = ("self_reported", "document_inspected", "issuer_verified")
BASIS_WORDS = {"self_reported": "self-reported", "document_inspected": "certificate checked", "issuer_verified": "verified with the issuer"}
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
    """Your profile's certifications with their catalog identity, explicit status, kind and evidence basis. A missing or
    unrecognised status is 'unknown'; profile items for one credential that disagree are all 'conflicting'."""
    cat = catalog(policy)["credentials"]
    out = []
    for cert in profile.data.get("certifications") or []:
        if not isinstance(cert, dict) or not cert.get("name"):
            continue
        key = credential_key(cert["name"], policy) or next(
            (k for m in cert.get("matches") or [] if (k := credential_key(m, policy))), None)
        item = cat.get(key) or {}
        basis = (cert.get("evidence") or {}).get("basis") if isinstance(cert.get("evidence"), dict) else None
        out.append({"name": cert["name"], "key": key, "family": item.get("family"), "issuer": item.get("issuer"),
                    "status": normal_status(cert.get("status")), "kind": normal_kind(cert.get("kind")),
                    "basis": basis if basis in BASES else None})   # an unrecognised basis is never shown as verified
    # one credential: the catalog credential (else the name as written) AND whether it is a course, so a course named like
    # a certification ("... Professional Certificate") never collides with the certification itself (2026-10-18)
    identity = lambda c: (c["key"] or norm(c["name"]), c["kind"] == "course_certificate")
    for ident in {identity(c) for c in out}:
        same = [c for c in out if identity(c) == ident]
        if len({c["status"] for c in same}) > 1:
            for c in same:
                c["status"] = "conflicting"
    return out


def credential_status(profile: Profile, policy, name: str) -> str:
    """Your status for a credential named in free text (a skill's `credentials`): held | in_progress | planned |
    completed | conflicting | unknown, or 'absent' when your profile doesn't list it."""
    key = credential_key(name, policy)
    mine = [c for c in profile_credentials(profile, policy) if (key and c["key"] == key) or norm(c["name"]) == norm(name)]
    if not mine:
        return "absent"
    counting = [c for c in mine if c["kind"] in COUNTING_KINDS]
    found = {c["status"] for c in (counting or mine)}   # a course of the same name never overrides the credential itself
    return found.pop() if len(found) == 1 else "conflicting"


def holds(profile: Profile, policy, name: str) -> bool:
    """You hold the credential itself: a certification or applied skill recorded as held, with no counting record of it
    saying otherwise. A course certificate or an unrecognised kind never does, as in the gate (Codex review, 2026-10-20)."""
    key = credential_key(name, policy)
    mine = [c for c in profile_credentials(profile, policy)
            if c["kind"] in COUNTING_KINDS and ((key and c["key"] == key) or norm(c["name"]) == norm(name))]
    return bool(mine) and {c["status"] for c in mine} == {"held"}


def evidence_review(profile: Profile, policy) -> list[dict]:
    """Skill, function and domain evidence that rests on a credential you don't currently hold (the item lists it in
    `credentials`). The evidence stays as recorded, never erased; it is flagged so nothing presents it as currently
    confirmed until you review it."""
    out = []
    for key in ("skills", "functions", "domains"):
        for item in profile._items(key):
            for name in item.get("credentials") or []:
                status = credential_status(profile, policy, name)
                if status == "completed" and _course_named(profile, policy, name):
                    continue   # a completed course is the training the skill rests on, not a contradicted claim
                if status != "held":
                    out.append({"item": item.get("name"), "kind": key[:-1], "credential": name, "status": status})
    return out


def _course_named(profile: Profile, policy, name: str) -> bool:
    key = credential_key(name, policy)
    return any(c["kind"] == "course_certificate" for c in profile_credentials(profile, policy)
               if (key and c["key"] == key) or norm(c["name"]) == norm(name))


def profile_warnings(profile: Profile, policy) -> list[str]:
    """Profile certification items that can't count as held as written: no status, an unrecognised status or kind, or
    two items for one credential that disagree."""
    raw = [c for c in profile.data.get("certifications") or [] if isinstance(c, dict) and c.get("name")]
    out = []
    for cert, parsed in zip(raw, profile_credentials(profile, policy), strict=True):
        if cert.get("status") is None:
            out.append(f"{cert['name']}: no status recorded (counted as unknown, not held)")
        elif normal_status(cert.get("status")) == "unknown":
            out.append(f"{cert['name']}: unrecognised status {cert.get('status')!r} (counted as unknown)")
        elif parsed["status"] == "conflicting":
            out.append(f"{cert['name']}: recorded more than once with different statuses (not counted as held)")
        if parsed["kind"] == "unknown":
            out.append(f"{cert['name']}: unrecognised kind {cert.get('kind')!r} (never counted as held)")
        elif parsed["kind"] == "course_certificate" and parsed["status"] == "held":
            out.append(f"{cert['name']}: a course certificate marked held: a course never counts as a held certification "
                       "(use status: completed)")
        basis = (cert.get("evidence") or {}).get("basis") if isinstance(cert.get("evidence"), dict) else None
        if basis is not None and basis not in BASES:
            out.append(f"{cert['name']}: unrecognised evidence basis {basis!r} (shown as self-reported; use one of {', '.join(BASES)})")
    return out


def held_names(profile: Profile, policy) -> list[str]:
    """The credentials that count as held: an explicit 'held' status, and never a course certificate."""
    return [c["name"] for c in profile_credentials(profile, policy) if c["status"] == "held" and c["kind"] in COUNTING_KINDS]


def _best(items: list) -> dict | None:
    order = {"held": 0, "in_progress": 1, "planned": 2, "completed": 3, "expired": 4, "conflicting": 5, "unknown": 6}
    # a credential that can count is preferred to a course or an unknown kind with the same status (2026-10-18)
    return min(items, key=lambda i: (i["kind"] not in COUNTING_KINDS, order.get(i["status"], 9))) if items else None


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
    elif kind == "issuer":   # "certification through AAPC": only that issuer's credentials answer it (2026-10-18)
        found, how = _best([c for c in mine if c["issuer"] and c["issuer"] == described["issuer"]]), "family_match"
    else:  # a family: any credential of that family answers it
        found = _best([c for c in mine if c["family"] and c["family"] == described["family"]])
        how = "family_match"
    if not found:
        return {"candidate_status": "not_held", "match_type": "none", "matched_credential": None, "satisfied": False}
    status = found["status"] if how != "related" else "not_held"
    satisfied = how in SATISFYING and found["status"] == "held" and found["kind"] in COUNTING_KINDS
    return {"candidate_status": found["status"] if how != "related" else status, "match_type": how,
            "matched_credential": found["name"], "matched_status": found["status"], "matched_basis": found.get("basis"),
            "matched_kind": found["kind"], "satisfied": satisfied}


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
    _alternatives(records, policy)
    for record in records:
        record["decision_effect"] = _effect(record)
    return records


def _spot(record: dict, policy) -> tuple[int, int] | None:
    """Where this certification is named in its sentence (the earliest of its aliases, family terms and issuer)."""
    cat = catalog(policy)
    words = [*(cat["credentials"].get(record["certification_key"] or "", {}).get("aliases") or []),
             *(cat["families"].get(record["cert_family"] or "", {}).get("terms") or []), record["issuer"] or "",
             record["normalized_name"] or ""]
    text, best = record["mention"].lower(), None
    for word in {w.lower() for w in words if w}:
        found = re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text)
        if found and (best is None or found.start() < best[0]):
            best = (found.start(), found.end())
    return best


_LIST_CUE = re.compile(r"\b(?:one|any|either)(?: of)?\b", re.I)


def _separator(text: str) -> str:
    rest = re.sub(r"[\s,;]+", " ", text).strip().lower()
    return "or" if rest in ("or", "and/or", "/", "or a", "or an") else "comma" if not rest else "other"


def _alternatives(records: list, policy) -> None:
    """Certifications offered as alternatives ("PMP or CBAP", "PMP, CBAP or PSM", "one of PMP, CBAP"): any one held
    satisfies them all. Only when they are named in ONE sentence and only "or" (or "and/or", "/", a comma list that ends
    in "or", or a list opened by "one of" / "any of" / "either") stands between them (2026-10-18): an "or" elsewhere on the
    line ("... plus Jira or Azure DevOps") never turns separate requirements into alternatives."""
    by_clause: dict = {}
    for record in records:
        spot = _spot(record, policy)
        if spot is not None:
            by_clause.setdefault((record["mention"], record["requirement_type"]), []).append((spot, record))
    groups = []
    for (mention, _rtype), found in by_clause.items():
        found.sort(key=lambda x: (x[0][0], x[1]["index"]))
        cue = bool(_LIST_CUE.search(mention[:found[0][0][0]]))   # "one of ..." before the first one
        run, joined = [found[0]], False
        for previous, current in zip(found, found[1:], strict=False):
            kind = _separator(mention[previous[0][1]:current[0][0]])
            if kind in ("or", "comma"):
                run.append(current)
                joined = joined or kind == "or"
                continue
            if len(run) > 1 and (joined or cue):
                groups.append([r for _, r in run])
            run, joined, cue = [current], False, False
        if len(run) > 1 and (joined or cue):
            groups.append([r for _, r in run])
    for members in groups:
        names = [m["normalized_name"] for m in members]
        winner = next((m for m in members if m["satisfied"]), None)
        for m in members:
            m["alternatives"] = [n for n in names if n != m["normalized_name"]]
            m["alt_group"] = members[0]["index"]
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
        key = ("alternatives", record["alt_group"]) if record.get("alt_group") is not None else ("single", record["index"])
        groups.setdefault(key, []).append(record)
    return list(groups.values())


def status_note(record: dict) -> str:
    """'(yours is planned, not counted as held)' or '(you hold X: related, not counted)', for gate details."""
    if record["match_type"] == "related" and record.get("matched_credential"):
        return f" (you hold {record['matched_credential']}: related, not counted)" if record.get("matched_status") == "held" \
            else f" (your related {record['matched_credential']} is {record.get('matched_status')})"
    if record.get("matched_kind") == "course_certificate" and record.get("matched_credential"):
        return f" (yours, {record['matched_credential']}, is a course certificate: never counted as held)"
    short = {"planned": "planned", "in_progress": "in progress", "completed": "a completed course", "expired": "expired",
             "conflicting": "recorded with conflicting statuses", "unknown": "without a recorded status"}
    if record["candidate_status"] in short and record.get("matched_credential"):
        return f" (yours is {short[record['candidate_status']]}, not counted as held)"
    return ""


STATUS_WORDS = {"held": "held", "in_progress": "in progress (not counted as held)", "planned": "planned (not counted as held)",
                "completed": "a completed course, not a certification (not counted as held)",
                "expired": "expired (not counted as held)",
                "conflicting": "recorded twice with different statuses (not counted as held until you resolve it)",
                "not_held": "not held", "unknown": "status not recorded (not counted as held)"}
MATCH_WORDS = {"exact": "exact match", "equivalent": "equivalent (owner-approved)", "family_match": "same family",
               "related": "related credential only", "none": "no match", "unknown": "cannot be matched (not named)"}
EFFECT_WORDS = {"requirement_satisfied": "requirement met", "hard_failure": "rejects the job (hard requirement not held)",
                "check_required": "check before applying", "positive_signal": "a plus you have (no score effect)",
                "none": "no effect", "not_a_requirement": "on-the-job, not a requirement"}


def summary(record: dict) -> str:
    """One short item for a review card: 'PMP (preferred): not held'."""
    status = STATUS_WORDS.get(record["candidate_status"], record["candidate_status"]).split(" (")[0]
    if record.get("matched_kind") in ("course_certificate", "unknown"):
        status = "a course or unrecognised kind, not counted as held"
    elif record["candidate_status"] == "held" and record.get("matched_basis"):
        status += f" ({BASIS_WORDS.get(record['matched_basis'], record['matched_basis'])})"
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
        if r.get("matched_kind") in ("course_certificate", "unknown"):
            status = f"{r['matched_credential']} is a course or an unrecognised kind (never counted as held)"
        elif r["candidate_status"] == "held" and r.get("matched_basis"):
            status += f" ({BASIS_WORDS.get(r['matched_basis'], r['matched_basis'])})"
        if r.get("matched_credential") and r["match_type"] in ("related", "family_match", "equivalent"):
            status += f": you hold {r['matched_credential']}" if r.get("matched_status") == "held" else \
                f": your {r['matched_credential']} is {STATUS_WORDS.get(r.get('matched_status'), r.get('matched_status'))}"
        parts += [status, MATCH_WORDS.get(r["match_type"], r["match_type"])]
        if r.get("satisfied_by_alternative"):
            parts.append(f"met by the alternative {r['satisfied_by_alternative']}")
        parts.append(f"effect: {EFFECT_WORDS.get(r['decision_effect'], r['decision_effect'])}")
        lines.append(" · ".join(parts) + f" · from: \"{r['mention']}\"")
    return lines or ["Certifications: none mentioned."]
