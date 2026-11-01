"""Application packages (P2, 2026-10-21; ADR 0009): everything prepared for one posting, versioned and bound by hash.

A package holds:
- the application brief (your approved profile, approved facts quoted word for word, honest gaps);
- the posting's questions matched to your reusable answers (careeros/review/answers.py), and the ones still open;
- the documents, by SHA-256;
- the destination.

Its manifest (canonical JSON, with no timestamps) is hashed. The same inputs give the same version; any change to an
answer, fact, document, question or destination gives a new one.

Routes:
- `apply`: you labelled it Apply.
- `stretch`: you labelled it Stretch.
- `standing`: the standing preparation policy picked it; `preparation` in your vault overrides, off unless you turn it
  on.

Standing preparation never writes a label, and Skip, a dismissal or an application already made always win.

A Stretch package (or every package, with `preparation.approval_required: all`) needs your approval of that exact
version (its manifest hash). Several can be approved together as a named batch; a package prepared later is not part
of the batch. Nothing here submits anything: an assistance session (careeros/review/assist.py) is how a package reaches
a form, and you click Submit.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from careeros.core.fingerprint import fingerprint, text_fingerprint
from careeros.db.store import Store

from . import answers as answer_lib
from .shortlist import _latest_label, brief_lines

ROUTES = ("apply", "stretch", "standing")
SCHEMA = 1
STANDING_DEFAULTS = {"enabled": False, "approval_required": "stretch", "min_eligibility": 80.0, "min_skill_coverage": 0.70,
                     "excluded_gaps": ["medium_gap", "large_gap", "severe_gap"], "allow_degree_stretch": False}


class PackageRefused(ValueError):
    pass


def settings(policy) -> dict:
    data = dict(STANDING_DEFAULTS)
    data.update((policy.data.get("preparation") or {}) if policy is not None else {})
    if data["approval_required"] not in ("stretch", "all"):
        raise ValueError("preparation_invalid: approval_required must be 'stretch' or 'all'")
    return data


def _score(store: Store, job_id: str):
    return store.one("SELECT * FROM scores WHERE job_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (job_id,))


def standing(policy, score) -> tuple[bool, list[str]]:
    """Does the standing preparation policy pick this job? (picked, reasons it doesn't). The rule is absolute, never the
    top N of a batch: ELIGIBLE, eligibility and skill coverage at or above the minimums, no degree stretch, no excluded
    Experience Match gap and no decisive unknown."""
    cfg = settings(policy)
    if not cfg["enabled"]:
        return False, ["standing preparation is off (preparation.enabled in your vault overrides)"]
    if score is None:
        return False, ["not scored"]
    comps = json.loads(score["components_json"] or "{}")
    why = []
    if score["state"] != "ELIGIBLE":
        why.append(f"state {score['state']}, not ELIGIBLE")
    elig = (comps.get("eligibility") or {}).get("score")
    if not isinstance(elig, (int, float)) or elig < cfg["min_eligibility"]:
        why.append(f"eligibility {elig} below {cfg['min_eligibility']:g}")
    coverage = comps.get("skill_coverage")
    if not isinstance(coverage, (int, float)) or coverage < cfg["min_skill_coverage"]:
        why.append(f"skill coverage {coverage} below {cfg['min_skill_coverage']:g}")
    if comps.get("stretch") and not cfg["allow_degree_stretch"]:
        why.append("a stretch (" + str(comps.get("stretch_reason") or "degree") + ")")
    gap = (comps.get("experience") or {}).get("gap_class")
    if gap in cfg["excluded_gaps"]:
        why.append(f"experience gap {gap}")
    if comps.get("decisive_unknowns"):
        why.append("decisive unknowns to check first")
    return not why, why


def _posting_veto(store: Store, job_id: str) -> str | None:
    """Why the posting itself stops preparation, or None: it closed (or is a duplicate), or the gate's latest decision
    isn't eligible. Every session would be refused there by `submission.authorize` (posting_not_open, not_eligible), so
    a package would only look ready, whatever your label or approval (2026-10-27: a closed posting and a role the gate
    now rejects as hybrid both still listed as needing approval). A posting the gate never judged is left to
    `submission.authorize`; one that reopens or is judged eligible again has its route back."""
    job = store.one("SELECT current_state, closed_at FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    if job["current_state"] in ("closed", "duplicate") or job["closed_at"]:
        return f"posting_not_open: the posting is {job['current_state']}" + \
            (f" (closed {job['closed_at'][:10]})" if job["closed_at"] else "")
    gate = store.one("SELECT decision, hard_failures_json FROM gate_results WHERE job_id = ? ORDER BY at DESC, rowid DESC "
                     "LIMIT 1", (job_id,))
    if gate and gate["decision"] != "eligible":
        failures = "; ".join(f"{f.get('code')}: {f.get('detail')}" for f in json.loads(gate["hard_failures_json"] or "[]"))
        return f"not_eligible: the gate's latest decision is {gate['decision']}" + (f" ({failures})" if failures else "")
    return None


def _application_veto(store: Store, job_id: str) -> str | None:
    """Why an application already made stops preparation, or None. Whatever the route, a role you applied to (the
    application in play or ended), may have applied to, or whose duplicate check can't finish is never prepared: every
    session would be refused there by `submission.authorize`, so a package would only look ready. Applying to a role
    again is your own decision, outside assistance: apply yourself and record it with `careeros applied --reapply`
    (independent Codex review of ac83e00, F7, and the second Claude review, 2026-10-22)."""
    from .applications import preflight
    for reason in preflight(store, job_id).reasons:
        if reason["level"] in ("block", "hold"):
            again = " Applying again is your decision: apply yourself and record it with careeros applied --reapply." \
                if reason["code"] == "applied_before" else ""
            return f"{reason['code']}: {reason['detail']}.{again}"
    return None


def route_for(store: Store, policy, job_id: str) -> tuple[str | None, str]:
    """(route, basis) a package may be prepared under now, or (None, why not). Your Skip, a dismissal, a posting that
    closed or that the gate no longer finds eligible, and an application already made always win, and a standing pick
    never becomes a label."""
    if store.one("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) is None:
        raise ValueError("unknown_job")
    if store.one("SELECT 1 FROM dismissals WHERE job_id = ?", (job_id,)):
        return None, "you dismissed it"
    label = _latest_label(store, job_id)
    if label and label["label"] == "skip":
        return None, "you labelled it Skip"
    veto = _posting_veto(store, job_id) or _application_veto(store, job_id)
    if veto:
        return None, veto
    if label and label["label"] in ("apply", "stretch"):
        return label["label"], f"your {label['label']} label" + (f" ({label['reason']})" if label["reason"] else "")
    picked, why = standing(policy, _score(store, job_id))
    if picked:
        return "standing", "the standing preparation policy (no label of yours)"
    return None, "not pursued: label it Apply or Stretch" + ("; standing preparation: " + "; ".join(why) if why else "")


def _documents(library: dict, vault_root: Path) -> tuple[list[dict], list[str]]:
    docs, gaps = [], []
    for kind in answer_lib.DOCUMENT_KINDS:
        rel = library["documents"].get(kind)
        if not rel:
            if kind == "resume":
                gaps.append("no resume named in answers.yaml (documents.resume)")
            continue
        path = vault_root / rel
        if not path.is_file():
            gaps.append(f"{kind} not found in the vault: {rel}")
            continue
        from careeros.core.fingerprint import file_fingerprint
        docs.append({"kind": kind, "path": rel, "sha256": file_fingerprint(path)})
    return docs, gaps


def _question_rows(questions: list[dict] | None, library: dict, documents: list[dict], today: str) -> tuple[list[dict], set]:
    """Each known question with your answer, the document that answers it, or why it's open. With the questions not
    read yet, every answer you could copy (current, and not held back as sensitive) is used, so each is part of the
    version (independent Codex review of ac83e00, 2026-10-22). A form question is read with its choices, which are
    kept in the row so the handoff can read them again."""
    if questions is None:
        return [], {a["id"] for a in library["answers"] if answer_lib.usable(a, today)}
    rows, used = [], set()
    docs = {d["kind"] for d in documents}
    for q in questions:
        key, label = str(q.get("key") or ""), str(q.get("label") or "")
        options = [str(o) for o in q.get("options") or []]
        row = {"key": key, "name": str(q.get("name") or ""), "label": label, "type": q.get("type") or "text",
               "required": bool(q.get("required")), **({"options": options} if options else {})}
        if key in answer_lib.DOCUMENT_KINDS:
            row["status"] = "document" if key in docs else ("missing" if row["required"] else "optional")
        else:
            found = answer_lib.find_field(key, library, today, label, options) if key in answer_lib.STANDARD_FIELDS else \
                answer_lib.find(label, library, today, options)
            row["status"], row["via"] = found["status"], found["via"]
            if found["blocked"]:
                row["blocked"] = found["blocked"]
            if found["answer"] is not None and found["status"] == "matched":
                row["answer_id"] = found["answer"]["id"]
                row["answer_sha256"] = text_fingerprint(found["answer"]["answer"])
                if options and found["answer"]["answer"] not in options:
                    row["status"] = "option_mismatch"   # your answer isn't one of the form's choices: review it
                used.add(found["answer"]["id"])
        rows.append(row)
    return rows, used


def build_manifest(store: Store, policy, profile, job_id: str, *, route: str, basis: str, facts: list, library: dict,
                   vault_root: Path, questions: list[dict] | None, today: str) -> tuple[dict, list[str], list[dict]]:
    """(manifest, brief lines, question rows) for a package, from what is true now. No timestamps: the same inputs
    give the same manifest hash."""
    job = store.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if job is None:
        raise ValueError("unknown_job")
    why = {"apply": "- Prepared because of " + basis, "stretch": "- Prepared because of " + basis +
           ". A Stretch package needs your approval before it is used.",
           "standing": "- Prepared by the standing preparation policy: there is no label of yours on this job."}[route]
    lines, issues = brief_lines(store, profile, job_id, policy, facts, why)
    documents, doc_gaps = _documents(library, vault_root)
    rows, used = _question_rows(questions, library, documents, today)
    gap_rows = [r for r in rows if r.get("status") not in ("matched", "document", "optional")]
    url = job["apply_url"] or job["url"] or ""
    answers_used = [a for a in library["answers"] if a["id"] in used]
    fact_ids = {f["id"] for f in facts}
    manifest = {
        "schema": SCHEMA, "route": route, "basis": basis,
        "job": {"job_id": job_id, "title": job["title"], "employer": job["employer_name"],
                "requisition_id": job["requisition_id"], "canonical_url": job["canonical_url"],
                "content_fingerprint": job["content_fingerprint"]},
        "destination": {"url": url, "host": (urlparse(url).hostname or "").lower()},
        "documents": documents,
        "questions": rows if questions is not None else None,
        # each answer by its text AND the question it was written for, so an edit to either is a new version (second
        # Claude review, 2026-10-22: the panel shows the question next to the answer)
        "answers": [{"id": a["id"], "kind": a["kind"], "approved": a["approved"], "expires": a["expires"],
                     "sha256": text_fingerprint(a["answer"]), "question_sha256": text_fingerprint(a["question"])}
                    for a in answers_used],
        # with the questions not read yet, the panel also lists the answers it holds back and why
        "withheld": [{"id": a["id"], "question_sha256": text_fingerprint(a["question"]),
                      "reason": answer_lib.withheld(a, today)} for a in library["answers"]
                     if questions is None and answer_lib.withheld(a, today)],
        "facts": sorted(({"id": f["id"], "sha256": text_fingerprint(f["text"])} for f in facts), key=lambda f: f["id"]),
        "facts_cited_by_answers": sorted({fid for a in answers_used for fid in a["facts"]}),
        "unknown_facts_cited": sorted({fid for a in answers_used for fid in a["facts"]} - fact_ids),
        "gaps": doc_gaps + [f"question: {r['label'] or r['key']} ({r['status'].replace('_', ' ')}"
                            + (f": {r['blocked']}" if r.get("blocked") else "") + ")" for r in gap_rows],
        "brief_sha256": text_fingerprint("\n".join(lines)),
        "wording_issues": len(issues),
    }
    return manifest, lines, rows


def prepared_answer(by_id: dict, answer_id: str, sha256: str, question_sha256: str | None = None) -> dict:
    """The answer a package was prepared with: its id AND its text hash (and, where the package recorded it, the hash
    of the question it was written for) must match, or nothing is shown or handed over (independent Codex review of
    ac83e00, 2026-10-22: an answer must never stand in for another after matching)."""
    entry = by_id.get(answer_id)
    if entry is None or text_fingerprint(entry["answer"]) != sha256 \
            or (question_sha256 is not None and text_fingerprint(entry["question"]) != question_sha256):
        raise ValueError(f"answer_changed: {answer_id} is not the answer this package was prepared with: prepare it again")
    held = answer_lib.blocked(entry)
    if held:   # the same answer, held back now (the checks were widened, or it was mislabelled)
        raise ValueError(f"answer_held_back: {answer_id} is held back now ({held}): prepare the package again")
    return entry


def _answer_panel(manifest: dict, rows: list[dict], library: dict) -> list[str]:
    """The manual fallback: every value to copy, question by question, and what you still answer yourself."""
    by_id = {a["id"]: a for a in library["answers"]}
    lines = [f"# Answers: {manifest['job']['title']} at {manifest['job']['employer']}", "",
             f"- Destination: {manifest['destination']['url'] or 'not recorded'}",
             "- Copy each value into the employer's form, review the whole form, then submit it yourself.", ""]
    for doc in manifest["documents"]:
        lines.append(f"- Attach your {doc['kind'].replace('_', ' ')}: {doc['path']} (SHA-256 {doc['sha256'][:12]})")
    if manifest["questions"] is None:
        lines += ["", "## Your current answers (this posting's questions are not known yet)", ""]
        for used in manifest["answers"]:
            entry = prepared_answer(by_id, used["id"], used["sha256"], used["question_sha256"])
            lines.append(f"- {entry['question']}\n  {entry['answer']}")
        if manifest["withheld"]:
            lines += ["", "## Not offered", ""]
            lines += [f"- {by_id[w['id']]['question'] if w['id'] in by_id else w['id']}: {w['reason']}"
                      for w in manifest["withheld"]]
    else:
        lines += ["", "## Questions", ""]
        for r in rows:
            label = r["label"] or r["key"]
            if r.get("status") == "matched":
                lines.append(f"- {label}\n  {prepared_answer(by_id, r['answer_id'], r['answer_sha256'])['answer']}")
            elif r.get("status") == "document":
                lines.append(f"- {label}: attach the file above")
            else:
                lines.append(f"- {label}: YOU ANSWER THIS ({r['status'].replace('_', ' ')}"
                             + (f": {r['blocked']}" if r.get("blocked") else "") + ")")
    # an instruction, not an assurance: wording varies, and the checks hold back what they recognise
    lines += ["", "- Consent, attestation, demographic, health, disability, military-service, signature, background, "
                  "criminal, credit and drug-check, identity-document and pay-history questions are always yours to "
                  "answer on the form, whatever this panel shows."]
    if manifest["gaps"]:
        lines += ["", "## Open items", ""] + [f"- {g}" for g in manifest["gaps"]]
    return lines


def questions_path(vault_root: Path, job_id: str) -> Path:
    return vault_root / "packages" / job_id.replace(":", "_") / "questions.json"


def load_questions(vault_root: Path, job_id: str) -> list[dict] | None:
    """The posting's form questions as last read (careeros package questions), or None when never read."""
    path = questions_path(vault_root, job_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["questions"]


def save_questions(vault_root: Path, job_id: str, questions: list[dict], source: str, at: str) -> Path:
    path = questions_path(vault_root, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": source, "read_at": at, "questions": questions}, indent=1) + "\n", encoding="utf-8")
    return path


def latest(store: Store, job_id: str):
    return store.one("SELECT * FROM packages WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,))


def prepare(store: Store, policy, profile, job_id: str, *, facts: list, library: dict, vault_root: Path,
            questions: list[dict] | None = None, actor: str = "user") -> dict:
    """Prepare (or confirm unchanged) the package for a job you pursue or the standing policy picks. Returns
    {package_id, version, new, manifest_sha256, folder, gaps}. Refused for a job you skipped or dismissed, for one
    nobody chose, for a posting that closed or that the gate no longer finds eligible, and for a role with an
    application (open or ended), a possible one, or a duplicate check that can't finish."""
    route, basis = route_for(store, policy, job_id)
    if route is None:
        raise PackageRefused(f"not_prepared: {basis}")
    today = store.clock.now()[:10]
    manifest, brief, rows = build_manifest(store, policy, profile, job_id, route=route, basis=basis, facts=facts,
                                           library=library, vault_root=vault_root, questions=questions, today=today)
    if manifest["unknown_facts_cited"]:
        raise PackageRefused("answer_cites_unknown_fact: " + ", ".join(manifest["unknown_facts_cited"]))
    digest = fingerprint(manifest)
    previous = latest(store, job_id)
    if previous is not None and previous["manifest_sha256"] == digest:
        return {"package_id": previous["package_id"], "version": previous["version"], "new": False, "manifest_sha256": digest,
                "folder": previous["folder"], "gaps": manifest["gaps"], "route": route}
    version = (previous["version"] + 1) if previous is not None else 1
    rel = Path("packages") / job_id.replace(":", "_") / f"v{version}"
    folder = vault_root / rel
    folder.mkdir(parents=True, exist_ok=False)   # a version's folder is written once
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "brief.md").write_text("\n".join(brief) + "\n", encoding="utf-8")
    (folder / "answers.md").write_text("\n".join(_answer_panel(manifest, rows, library)) + "\n", encoding="utf-8")
    package_id = f"pkg:{uuid.uuid4().hex[:16]}"
    writer = store.writer("assist")
    with store.tx():
        writer.insert("packages", {"package_id": package_id, "job_id": job_id, "version": version, "route": route,
                                   "basis": basis, "manifest_sha256": digest, "manifest_json": json.dumps(manifest, sort_keys=True),
                                   "folder": rel.as_posix(), "at": store.clock.now(), "actor": actor})
        writer.event("package_prepared", outcome="success", entity_type="job", entity_id=job_id,
                     actor_type="human" if actor == "user" else "system", actor_id=actor,
                     metadata={"package_id": package_id, "version": version, "route": route, "manifest_sha256": digest,
                               "gaps": len(manifest["gaps"])})
    return {"package_id": package_id, "version": version, "new": True, "manifest_sha256": digest, "folder": rel.as_posix(),
            "gaps": manifest["gaps"], "route": route}


def _approval(store: Store, package) -> dict | None:
    """Your approval of exactly this package version (its manifest hash), unless revoked."""
    rows = store.all("SELECT * FROM package_approvals WHERE package_id = ? ORDER BY at, rowid", (package["package_id"],))
    revoked = {r["revokes"] for r in rows if r["action"] == "revoke"}
    valid = [r for r in rows if r["action"] == "approve" and r["approval_id"] not in revoked
             and r["manifest_sha256"] == package["manifest_sha256"]]
    return dict(valid[-1]) if valid else None


def needs_approval(policy, route: str) -> bool:
    return route == "stretch" or settings(policy)["approval_required"] == "all"


def approve(store: Store, package_id: str, *, reason: str, key: str, batch: str | None = None) -> str:
    """Approve ONE package version for assistance. The approval names its manifest hash, so a later version is not
    covered. `batch` names a set approved together; it never adds packages prepared later."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    package = store.one("SELECT * FROM packages WHERE package_id = ?", (package_id,))
    if package is None:
        raise ValueError("unknown_package")
    newest = latest(store, package["job_id"])
    if newest["package_id"] != package_id:
        raise ValueError(f"superseded: version {newest['version']} is the current package for this job")
    writer = store.writer("assist")
    with store.tx():
        if writer.receipt("package_approve", key, {"package_id": package_id, "batch": batch or "", "reason": reason},
                          actor_id="user") == "replay":
            return "replay"
        approval_id = f"pap:{uuid.uuid4().hex[:16]}"
        writer.insert("package_approvals", {"approval_id": approval_id, "action": "approve", "revokes": None,
                                            "package_id": package_id, "manifest_sha256": package["manifest_sha256"],
                                            "batch": (batch or "").strip() or None, "reason": reason[:300],
                                            "at": store.clock.now(), "actor": "user"})
        writer.event("package_approved", outcome="success", entity_type="job", entity_id=package["job_id"],
                     actor_type="human", actor_id="user",
                     metadata={"package_id": package_id, "approval_id": approval_id, "batch": batch,
                               "manifest_sha256": package["manifest_sha256"]})
    return approval_id


def revoke(store: Store, approval_id: str, *, reason: str, key: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    row = store.one("SELECT * FROM package_approvals WHERE approval_id = ? AND action = 'approve'", (approval_id,))
    if row is None:
        raise ValueError("unknown_approval")
    writer = store.writer("assist")
    with store.tx():
        if writer.receipt("package_revoke", key, {"approval_id": approval_id, "reason": reason}, actor_id="user") == "replay":
            return "replay"
        revoke_id = f"pap:{uuid.uuid4().hex[:16]}"
        writer.insert("package_approvals", {"approval_id": revoke_id, "action": "revoke", "revokes": approval_id,
                                            "package_id": row["package_id"], "manifest_sha256": row["manifest_sha256"],
                                            "batch": row["batch"], "reason": reason[:300], "at": store.clock.now(),
                                            "actor": "user"})
        writer.event("package_approval_revoked", outcome="success", entity_type="package", entity_id=row["package_id"],
                     actor_type="human", actor_id="user", metadata={"approval_id": approval_id, "revoke_id": revoke_id})
    return revoke_id


def standing_candidates(store: Store, policy) -> list[str]:
    """Open, scored jobs the standing preparation policy picks now (no label of yours, no Skip, no dismissal, no
    application in the role), best priority first. Empty while the policy is off."""
    if not settings(policy)["enabled"]:
        return []
    from .applications import preflight
    rows = store.all("SELECT j.job_id FROM jobs j WHERE j.current_state = 'scored' AND NOT EXISTS "
                     "(SELECT 1 FROM labels l WHERE l.job_id = j.job_id AND l.profile = 'primary')")
    out = []
    for r in rows:
        route, _ = route_for(store, policy, r["job_id"])
        if route == "standing" and not preflight(store, r["job_id"]).codes("block"):
            score = _score(store, r["job_id"])
            out.append((-(score["apply_priority"] or 0) if score else 0, r["job_id"]))
    return [job_id for _, job_id in sorted(out)]


def common_gaps(store: Store) -> list[tuple[str, int, str | None]]:
    """Questions your current packages still leave open (missing, expired or not one of the form's choices), most
    frequent first, as (label, count, slot): answering one of these once in answers.yaml covers every package that asks
    it. The slot is the standard field (first_name, email...) or document (resume, cover_letter) the form's own key
    says the question is, filled from the file by `field:` or `documents:` whatever its label; None for any other. A
    label's line breaks and runs of spaces are evened out, so one question is one row on one line."""
    from collections import Counter
    counts: Counter = Counter()
    slots = answer_lib.STANDARD_FIELDS + answer_lib.DOCUMENT_KINDS
    rows = store.all("SELECT manifest_json FROM packages p WHERE version = (SELECT MAX(version) FROM packages q "
                     "WHERE q.job_id = p.job_id)")
    for r in rows:
        for q in json.loads(r["manifest_json"]).get("questions") or []:
            if q.get("status") in ("missing", "expired", "option_mismatch"):
                label = " ".join((q["label"] or "").split()) or q["key"]
                counts[(label, q["key"] if q["key"] in slots else None)] += 1
    return [(label, n, slot) for (label, slot), n in counts.most_common()]


def readiness(store: Store, policy, profile, job_id: str, *, facts: list, library: dict, vault_root: Path,
              questions: list[dict] | None = None) -> dict:
    """Is the job's current package ready for assistance now? {state, package, reasons}. States: not_prepared,
    withdrawn (no route any more: you skipped or dismissed it since, the posting closed or the gate no longer finds it
    eligible, or the role has an application, open or ended, may have one, or its duplicate check can't finish), stale (something it was built from changed: prepare it again),
    needs_approval, ready. Sessions are checked by careeros/review/assist.py."""
    package = latest(store, job_id)
    if package is None:
        return {"state": "not_prepared", "package": None, "reasons": ["no package yet: careeros package prepare"]}
    route, basis = route_for(store, policy, job_id)
    if route is None:
        return {"state": "withdrawn", "package": dict(package), "reasons": [basis]}
    manifest, _, _ = build_manifest(store, policy, profile, job_id, route=route, basis=basis, facts=facts, library=library,
                                    vault_root=vault_root, questions=questions, today=store.clock.now()[:10])
    if fingerprint(manifest) != package["manifest_sha256"]:
        return {"state": "stale", "package": dict(package),
                "reasons": ["what this package was built from changed (an answer, fact, document, question, the route or the "
                            "posting): prepare it again"]}
    if needs_approval(policy, package["route"]) and _approval(store, package) is None:
        return {"state": "needs_approval", "package": dict(package),
                "reasons": [f"approve version {package['version']}: careeros package approve {package['package_id']}"]}
    return {"state": "ready", "package": dict(package), "reasons": [], "approval": _approval(store, package)}
