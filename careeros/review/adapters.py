"""Fixed, reviewed form adapters (P4/P5, 2026-10-21; ADR 0009).

An adapter knows one ATS's application form well enough to say which named field takes which of your answers. Two
things come from it:
- the posting's questions, read from the ATS's public job-board data (the same allowed source discovery uses);
- the handoff file the browser extension reads.

The handoff holds only the values for that form's fillable fields, plus what you must do yourself. Only text fields,
text areas and plain drop-down lists are filled. Files, multi-selects, custom widgets and every demographic or
compliance question stay with you.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import answers as answer_lib

STANDARD = {"first_name", "last_name", "preferred_name", "email", "phone"}
KINDS = {"input_text": "text", "textarea": "textarea", "multi_value_single_select": "select", "input_file": "file",
         "multi_value_multi_select": "multiselect"}


def greenhouse_questions(job_json: dict) -> list[dict]:
    """The posting's application questions from Greenhouse's public job-board API (a job fetched with questions=true),
    as package questions: {key, name, label, type, required, options}. Demographic and compliance sections are left
    out: you answer those on the form. Hidden fields are skipped."""
    out = []
    for q in job_json.get("questions") or []:
        fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
        if not fields:
            continue
        files = [f for f in fields if f.get("type") == "input_file"]
        field = files[0] if files else fields[0]   # "Resume/CV" offers a file and a text box: the file is the question
        name = str(field.get("name") or "")
        kind = KINDS.get(field.get("type"), "other")
        key = name if name in STANDARD | set(answer_lib.DOCUMENT_KINDS) else "question"
        options = [str(v.get("label")) for v in field.get("values") or [] if v.get("label") is not None]
        out.append({"key": key, "name": name, "label": str(q.get("label") or name), "type": kind,
                    "required": bool(q.get("required")), **({"options": options} if options else {})})
    return out


GREENHOUSE_ORIGINS = ("https://boards.greenhouse.io", "https://job-boards.greenhouse.io")
# The reviewed shape of a Greenhouse-hosted posting and of the fields the extension may fill. extension/popup.js and
# the page function in extension/fill.js hold the same three values, and tests/test_extension.py checks they stay
# equal (independent Codex review of ac83e00 and the second Claude review, 2026-10-22: neither a handoff file nor the
# popup may grant another destination or field).
GREENHOUSE_PATH = r"^/[A-Za-z0-9_-]{1,100}/jobs/[0-9]{1,20}$"
GREENHOUSE_FIELD = r"^(?:first_name|last_name|preferred_name|email|phone|question_[0-9]{1,20})$"


def fetch_greenhouse_questions(client, job_id: str) -> list[dict]:
    """A Greenhouse posting's questions, from the public job-board API (source policy: greenhouse must be allowed)."""
    from careeros.discovery.boards import _token
    from careeros.discovery.sources import require_allowed
    require_allowed("greenhouse")
    parts = job_id.split(":")
    if len(parts) != 3 or parts[0] != "greenhouse":
        raise ValueError("not_a_greenhouse_job")
    data = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_token(parts[1])}/jobs/{_token(parts[2])}?questions=true")
    return greenhouse_questions(data)


def destination(adapter: str, url: str) -> dict:
    """Where the extension may fill: the origins the adapter's forms live on and the posting's own path. Greenhouse
    serves one posting from two hosts (the older address redirects to the newer one)."""
    parts = urlparse(url or "")
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    path = parts.path.rstrip("/")
    # fullmatch: '$' alone would also accept a final newline, which the extension refuses (second Claude review)
    if adapter != "greenhouse" or origin not in GREENHOUSE_ORIGINS or not re.fullmatch(GREENHOUSE_PATH, path):
        raise ValueError(f"destination_not_reviewed: {origin}{path} is not a reviewed {adapter} posting address")
    return {"origins": list(GREENHOUSE_ORIGINS), "path": path}


def handoff(adapter: str | None, session_id: str, package, manifest: dict, library: dict, expires_at: str | None) -> dict:
    """The per-session file the extension reads: only the values for fields this adapter fills, bound to the destination
    and expiring. Everything else is listed for you to do on the form."""
    if adapter != "greenhouse":
        raise ValueError(f"no_adapter: {adapter}")
    from .packages import prepared_answer
    place = destination(adapter, manifest["destination"]["url"])
    by_id = {a["id"]: a for a in library["answers"]}
    question_sha = {a["id"]: a.get("question_sha256") for a in manifest["answers"]}
    fields, review = [], []
    for row in manifest["questions"] or []:
        label = row["label"] or row["key"]
        if row["type"] in ("file",):
            review.append(f"attach: {label}")
            continue
        # read again here, with the form's choices: the last check before a value leaves CareerOS
        blocked = answer_lib.form_blocked(label, row.get("options"))
        if row.get("status") != "matched" or row["type"] not in ("text", "textarea", "select") \
                or not re.fullmatch(GREENHOUSE_FIELD, row.get("name") or "") or blocked:
            why = row.get("blocked") or blocked
            status = "sensitive" if blocked and not row.get("blocked") else row.get("status", row["type"])
            review.append(f"you answer: {label} ({status.replace('_', ' ')}" + (f": {why}" if why else "") + ")")
            continue
        entry = prepared_answer(by_id, row["answer_id"], row["answer_sha256"], question_sha.get(row["answer_id"]))
        if entry["kind"] in answer_lib.NAMED_KINDS:   # a named skill, credential or employer: you copy it, reading it
            review.append(f"copy from the panel: {label} (CareerOS never fills a {entry['kind'].replace('_', ' ')} answer)")
            continue
        why = answer_lib.not_fillable(entry["answer"])   # prose, or characters the checks can't read as the page will
        if why:
            review.append(f"copy from the panel: {label} (never filled: {why})")
            continue
        fields.append({"name": row["name"], "label": label, "kind": row["type"], "value": entry["answer"]})
    review.append("you answer: every consent, attestation, demographic, health, disability, military-service, "
                  "signature, background, criminal, credit and drug-check, identity-document and pay-history question "
                  "on the form, whatever was filled")
    return {"careeros_handoff": 1, "adapter": adapter, "session_id": session_id, "package_id": package["package_id"],
            "manifest_sha256": package["manifest_sha256"], "expires_at": expires_at,
            "destination": place,
            "job": {"title": manifest["job"]["title"], "employer": manifest["job"]["employer"]},
            "fields": fields, "review": review,
            "attach": [{"kind": d["kind"], "path": d["path"], "sha256": d["sha256"]} for d in manifest["documents"]]}
