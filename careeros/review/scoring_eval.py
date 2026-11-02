"""Shadow scoring evaluator (SCORING_SHADOW_SPEC 2026-10-16, sections 9-12): production tiers, the calibration
review's baseline B0 and rubric v1, side by side on a frozen read-only snapshot, measured against your labels.

READ-ONLY by construction:
- the database is copied with SQLite's backup API (or you name a snapshot) and the copy is opened mode=ro;
- no Store is constructed, no writer is requested and no network call is made;
- the only files written are this evaluator's own report and calibration-list files under vault/reports.
Nothing here changes a production tier, a label, a gate decision or the decision fingerprint. Every metric reads the
STATUS; your labels and preferences decide only actionability.

Your existing labels are a development set: descriptive only, never population estimates and never grounds for adoption.
Population estimates come only from labels given on a calibration list after it was drawn (careeros/review/sampling.py).

A calibration draw FREEZES what it measures (2026-10-17): every approach's status for each drawn group, the drawn
members, and the evaluation identity (evaluator, rubric configuration and code, decision and profile fingerprints,
preferences, grouping, the snapshot and your adoption criteria). Later estimates read those frozen predictions and the
drawn members' own labels, never statuses recomputed under later rules; what changed since the draw is reported. A draw
needs your adoption criteria written down first. Nothing here adopts anything.

Which list a label answers (2026-10-17): a label belongs to the most recent list that held that posting when you gave
it, and a list counts each member's FIRST label that belongs to it. So answering a later list (a relabel check, or a
group drawn again) never rewrites an earlier list's result. Lists are pooled only when their predictions came from the
same rules (the same prediction identity); otherwise each identity is reported on its own.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from careeros.core.clock import Clock
from careeros.db.readonly import ReadOnlyStore, file_sha256, make_snapshot
from careeros.qualify import rubric
from careeros.qualify.gate import GateResult, state_for
from careeros.qualify.profile import Profile

from . import role_identity, sampling, shadow_policy
from .dismiss import dismissed_sql
from .preferences import detect, load_preferences

EVALUATOR_VERSION = "scoring-eval-v1-2026-10-16"
LIST_KIND = "careeros-calibration-list"
LABELS = ("apply", "stretch", "skip")
WANTED = ("apply", "stretch")
DURATIONS = ("D1", "D2")
PROD_ORDER = ("rejected", "held", "archive", "near_miss", "B", "A")
BASE_ORDER = ("rejected", "held", "not_recommended", "B", "A")
V1_ORDER = ("rejected", "held", *shadow_policy.ORDER)


@dataclass
class JobEval:
    job_id: str
    job: dict
    kind: str                                   # judged | rejected
    production: str
    baseline: str
    v1: str                                     # the primary rubric configuration
    v1_all: dict[str, str]                      # "R2/D2" -> status, every configuration
    labels: list[tuple[str, str]] = field(default_factory=list)   # (at, label), oldest first
    assessment: rubric.Assessment | None = None
    decision: shadow_policy.Decision | None = None
    prefs: dict = field(default_factory=dict)
    act: str = "actionable"
    queue: str = ""
    reason: str | None = None                   # the free-text reason you gave with your latest label, if any

    @property
    def label(self) -> str | None:
        return self.labels[-1][1] if self.labels else None


def labels_after(history: dict[str, list[tuple[str, str]]], members: list[str], at: str, until: dict[str, str] | None = None) -> str | None:
    """One group label from each named member's FIRST label given strictly after `at` (a calibration list's creation)
    and, for a member a later list also holds, before that later list was drawn (`until`): your answer to this list.
    Only the members drawn count, never postings that joined the role later."""
    found = []
    for job_id in members:
        stop = (until or {}).get(job_id)
        later = [label for when, label in history.get(job_id, []) if when > at and (stop is None or when <= stop)]
        if later:
            found.append(later[0])
    return group_label(found)


def next_listing(lists: list[dict]) -> dict[str, dict[str, str]]:
    """For each list (by path) and each member it shows (drawn groups and relabel checks; a group considered but not
    drawn was never shown), when the next list showing that member was drawn: labels after that answer the later list."""
    held: dict[str, list[tuple[str, str]]] = {}
    for record in lists:
        for entry in [*(g for g in record["groups"] if g["drawn"]), *record.get("relabel", [])]:
            for job_id in entry["members"]:
                held.setdefault(job_id, []).append((record["created_at"], record["_path"]))
    out: dict[str, dict[str, str]] = {}
    for job_id, times in held.items():
        times.sort()
        for (_, path), (later, _) in zip(times, times[1:], strict=False):
            out.setdefault(path, {})[job_id] = later
    return out


def labels_until(history: dict[str, list[tuple[str, str]]], members: list[str], at: str) -> str | None:
    found = []
    for job_id in members:
        earlier = [label for when, label in history.get(job_id, []) if when <= at]
        if earlier:
            found.append(earlier[-1])
    return group_label(found)


@dataclass
class Group:
    group_id: str
    members: list[JobEval]
    statuses: dict[str, str]                    # production, baseline, v1, and "v1:R0/D1" ... for every configuration
    rep: JobEval                                # the member with the best primary rubric status
    label: str | None = None                    # apply | stretch | skip | conflict | None

    @property
    def open(self) -> bool:
        return any(not m.job.get("closed_at") for m in self.members)


def group_label(labels: list[str]) -> str | None:
    """One label per role group: a Skip next to an Apply or Stretch is a conflict, never resolved silently."""
    kinds = {label for label in labels if label in LABELS}
    if not kinds:
        return None
    if "skip" in kinds and kinds & set(WANTED):
        return "conflict"
    return "apply" if "apply" in kinds else "stretch" if "stretch" in kinds else "skip"


def baseline_status(state: str, eligibility: float, coverage: float, gap: str | None, degree_stretch: bool, b: dict) -> str:
    """B0: ELIGIBLE, not a degree stretch, eligibility and production coverage (its neutral 0.70 included) high enough,
    and an experience gap the calibration review allowed. Stored production values only."""
    if state == "HOLD":
        return "held"
    base = state == "ELIGIBLE" and not degree_stretch
    gap = gap or "unknown"
    if base and eligibility >= b["eligibility_a"] and coverage >= b["coverage_a"] and gap in b["gaps_a"]:
        return "A"
    if base and eligibility >= b["eligibility_b"] and coverage >= b["coverage_b"] and gap in b["gaps_b"]:
        return "B"
    return "not_recommended"


# --- loading the snapshot ---------------------------------------------------------------------------------------------
def _latest(ro: ReadOnlyStore, table: str) -> dict:
    latest: dict = {}
    for row in ro.all(f"SELECT * FROM {table} ORDER BY job_id, at, rowid"):
        latest[row["job_id"]] = row
    return latest


def _label_history(ro: ReadOnlyStore) -> dict[str, list[tuple[str, str]]]:
    history: dict[str, list[tuple[str, str]]] = {}
    for row in ro.all("SELECT job_id, at, label FROM labels WHERE profile = 'primary' ORDER BY at, rowid"):
        history.setdefault(row["job_id"], []).append((row["at"], row["label"]))
    return history


def _label_reasons(ro: ReadOnlyStore) -> dict[str, str | None]:
    """The free-text reason given with each job's latest PRIMARY label (None when you gave none). Collected for the
    report only; it never changes a status or a metric."""
    reasons: dict[str, str | None] = {}
    for row in ro.all("SELECT job_id, reason FROM labels WHERE profile = 'primary' ORDER BY at, rowid"):
        reasons[row["job_id"]] = (row["reason"] or "").strip() or None
    return reasons


# --- the evaluation identity a calibration draw freezes -------------------------------------------------------------------
IDENTITY_MODULES = ("careeros/review/scoring_eval.py", "careeros/review/sampling.py", "careeros/review/shadow_policy.py",
                    "careeros/review/role_identity.py", "careeros/review/preferences.py", "careeros/qualify/rubric.py",
                    # what the predictions also depend on (the rubric's own imports, the job view and requirements it reads)
                    "careeros/qualify/scores.py", "careeros/qualify/experience.py", "careeros/qualify/certifications.py",
                    "careeros/qualify/gate.py", "careeros/qualify/profile.py", "careeros/normalize/posting.py",
                    "careeros/pipeline.py", "careeros/extract/rules.py")
PREDICTION_KEYS = ("evaluator", "rubric_version", "shadow_config_sha256", "primary_configuration", "evaluator_code_sha256",
                   "decision_fingerprint", "profile_fingerprint", "preferences_sha256", "grouping")


def _sha(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def code_sha256() -> str:
    """One hash over the evaluator's own source files (line endings normalised), so a list records the code that
    produced its predictions without running any program."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for rel in IDENTITY_MODULES:
        digest.update(rel.encode("utf-8") + b"\0" + (root / rel).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def evaluation_identity(cfg: dict, policy, profile: Profile, prefs: dict, snapshot_sha256: str, criteria_sha256: str | None) -> dict:
    from careeros.pipeline import decision_fingerprint
    return {"evaluator": EVALUATOR_VERSION, "rubric_version": cfg.get("version"), "shadow_config_sha256": _sha(cfg),
            "primary_configuration": f"{cfg['primary']['core']}/{cfg['primary']['duration']}",
            "evaluator_code_sha256": code_sha256(),
            "decision_fingerprint": decision_fingerprint(SimpleNamespace(policy=policy)),   # type: ignore[arg-type]
            "profile_fingerprint": profile.fingerprint, "preferences_sha256": _sha({k: v for k, v in (prefs or {}).items() if v}),
            "grouping": {"group_at": cfg["evaluation"]["group_at"], "strong_similarity": cfg["evaluation"]["strong_similarity"]},
            "sampling_pis": cfg["sampling"], "snapshot_sha256": snapshot_sha256, "adoption_criteria_sha256": criteria_sha256}


def identity_changes(drawn: dict, current: dict) -> list[str]:
    """What differs between a list's frozen identity and today's (the snapshot always differs, so it is left out)."""
    return sorted(k for k in set(drawn) | set(current) if k != "snapshot_sha256" and drawn.get(k) != current.get(k))


def prediction_identity(identity: dict) -> str:
    """The part of a list's identity that decides its predictions: lists are pooled only when this is the same."""
    return _sha({k: identity.get(k) for k in PREDICTION_KEYS})[:16]


def _linked(ro: ReadOnlyStore) -> set[frozenset]:
    pairs = {frozenset((r["job_id"], r["duplicate_of"])) for r in ro.all("SELECT job_id, duplicate_of FROM jobs WHERE duplicate_of IS NOT NULL")}
    pairs |= {frozenset((r["job_id"], r["other_id"])) for r in ro.all("SELECT job_id, other_id FROM opening_links WHERE relation = 'same_opening'")}
    return {p for p in pairs if len(p) == 2}


def evaluate_jobs(ro: ReadOnlyStore, policy, profile: Profile, cfg: dict, prefs: dict) -> tuple[list[JobEval], Counter]:
    """Every PRIMARY job any approach could judge: scored jobs, gate rejections in your target families, and any
    labelled job the gate judged. Returns the evaluations and counts of what was left out (and why)."""
    from careeros.pipeline import job_view, load_requirements
    gates, scores, history, reasons = _latest(ro, "gate_results"), _latest(ro, "scores"), _label_history(ro), _label_reasons(ro)
    dismissed = {r["job_id"] for r in ro.all(f"SELECT j.job_id FROM jobs j WHERE {dismissed_sql('j.job_id')}")}   # latest event
    primary = cfg["primary"]
    configs = [f"{core}/{d}" for core in shadow_policy.CORE_ALTERNATIVES for d in DURATIONS]
    out: list[JobEval] = []
    left_out: Counter = Counter()
    for row in ro.all("SELECT * FROM jobs WHERE exclude_from_kpi = 0 ORDER BY job_id"):
        job_id, gate_row, labels = row["job_id"], gates.get(row["job_id"]), history.get(row["job_id"], [])
        if gate_row is None or gate_row["decision"] not in ("eligible", "rejected"):
            if labels:
                left_out["labelled_not_judged"] += 1
            continue
        job = job_view(row)
        if gate_row["decision"] == "rejected":
            if row["family"] is None and not labels:
                continue   # screened out on its title: no approach could ever pick it
            out.append(JobEval(job_id, job, "rejected", "rejected", "rejected", "rejected", dict.fromkeys(configs, "rejected"),
                               labels, queue="rejected", reason=reasons.get(job_id)))
            continue
        score = scores.get(job_id)
        if score is None:
            left_out["eligible_not_scored"] += 1
            continue
        gate = GateResult(gate_row["decision"], json.loads(gate_row["hard_failures_json"] or "[]"), json.loads(gate_row["flags_json"] or "[]"))
        state = score["state"] or state_for(gate, policy)
        comps = json.loads(score["components_json"] or "{}")
        degree = gate.has_flag("degree_stretch")
        production = "held" if state == "HOLD" else score["tier"]
        baseline = baseline_status(state, float(score["eligibility"] or 0), float(comps.get("skill_coverage") or 0),
                                   (comps.get("experience") or {}).get("gap_class"), degree, cfg["baseline"])
        requirements = load_requirements(ro, row)  # type: ignore[arg-type]  # the read side of Store is all it uses
        decisive = [f["facet"] for f in comps.get("decisive_unknowns") or []]
        assessments = {d: rubric.assess(job, requirements, profile, policy, cfg, duration=d, decisive_unknowns=decisive) for d in DURATIONS}
        v1_all = {f"{core}/{d}": shadow_policy.decide(assessments[d], state=state, degree_stretch=degree, cfg=cfg, core=core).status
                  for core in shadow_policy.CORE_ALTERNATIVES for d in DURATIONS}
        assessment = assessments[primary["duration"]]
        decision = shadow_policy.decide(assessment, state=state, degree_stretch=degree, cfg=cfg, core=primary["core"])
        found = detect(job, prefs) if prefs else {}
        act = shadow_policy.actionability(labels[-1][1] if labels else None, job_id in dismissed, bool(found.get("conflict")))
        out.append(JobEval(job_id, job, "judged", production, baseline, decision.status, v1_all, labels, assessment, decision,
                           found, act, shadow_policy.queue(decision.status, act), reasons.get(job_id)))
    return out, left_out


# --- role groups --------------------------------------------------------------------------------------------------------
def build_groups(jobs: list[JobEval], members_by_group: dict[str, list[str]], *, conflicts: str = "exclude") -> list[Group]:
    """Group views: the best status per approach and configuration, and one label. `conflicts` decides a conflicting
    group's label for the sensitivity runs: exclude (kept as 'conflict'), wanted or skip."""
    by_id = {j.job_id: j for j in jobs}
    configs = sorted(jobs[0].v1_all) if jobs else []
    groups = []
    for group_id in sorted(members_by_group):
        members = [by_id[m] for m in members_by_group[group_id] if m in by_id]
        if not members:
            continue
        statuses = {"production": max((m.production for m in members), key=PROD_ORDER.index),
                    "baseline": max((m.baseline for m in members), key=BASE_ORDER.index),
                    "v1": max((m.v1 for m in members), key=V1_ORDER.index)}
        for config in configs:
            statuses[f"v1:{config}"] = max((m.v1_all[config] for m in members), key=V1_ORDER.index)
        rep = min(members, key=lambda m: (-V1_ORDER.index(m.v1), m.job_id))
        label = group_label([m.label for m in members if m.label])
        if label == "conflict" and conflicts != "exclude":
            wanted = [m.label for m in members if m.label in WANTED]
            label = "skip" if conflicts == "skip" else ("apply" if "apply" in wanted else "stretch")
        groups.append(Group(group_id, members, statuses, rep, label))
    return groups


# --- metrics on your labels (development set) ---------------------------------------------------------------------------
def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 3) if den else None


def approach_metrics(groups: list[Group], approach: str, weights: dict[str, float] | None = None) -> dict:
    """Counts and rates for one approach over the labelled groups (conflicts left out). With `weights` (equal
    employer weight) the rates are weighted and the counts stay raw."""
    labelled = [g for g in groups if g.label in LABELS]

    def w(g: Group) -> float:
        return weights[g.group_id] if weights else 1.0

    def rec(g: Group) -> bool:
        return sampling.recommended(g.statuses[approach])

    picked = [g for g in labelled if rec(g)]
    wanted = [g for g in labelled if g.label in WANTED]
    applies = [g for g in labelled if g.label == "apply"]
    skips = [g for g in picked if g.label == "skip"]
    out = {
        "labelled": len(labelled), "recommended": len(picked), "skips_among_recommended": len(skips),
        "skip_share_among_recommendations": _ratio(sum(map(w, skips)), sum(map(w, picked))),
        "upper_95_if_none": sampling.upper_95_if_none(len(picked)) if picked and not skips else None,
        "wanted": len(wanted), "recall_wanted": _ratio(sum(w(g) for g in wanted if rec(g)), sum(map(w, wanted))),
        "apply": len(applies), "recall_apply": _ratio(sum(w(g) for g in applies if rec(g)), sum(map(w, applies))),
        "false_hard_rejections": _ratio(sum(w(g) for g in wanted if g.statuses[approach] == "rejected"), sum(map(w, wanted))),
    }
    if approach.startswith("v1"):
        check_first = [g for g in labelled if g.statuses[approach] == "check_first"]
        out["check_first_labelled"] = len(check_first)
        out["check_first_yield"] = _ratio(sum(w(g) for g in check_first if g.label in WANTED), sum(map(w, check_first)))
        out["recall_wanted_with_check_first"] = _ratio(sum(w(g) for g in wanted if rec(g) or g.statuses[approach] == "check_first"),
                                                       sum(map(w, wanted)))
    return out


def all_metrics(groups: list[Group], approaches: list[str], weights: dict[str, float] | None = None) -> dict:
    return {a: approach_metrics(groups, a, weights) for a in approaches}


def discordance(groups: list[Group], first: str, second: str) -> dict:
    """Paired table over every group: who recommends it, and how you labelled the ones only one approach picks."""
    table: Counter = Counter()
    labels: dict[str, Counter] = {"only_" + first: Counter(), "only_" + second: Counter()}
    for g in groups:
        a, b = sampling.recommended(g.statuses[first]), sampling.recommended(g.statuses[second])
        cell = "both" if a and b else "only_" + first if a else "only_" + second if b else "neither"
        table[cell] += 1
        if cell in labels:
            labels[cell][g.label or "unlabelled"] += 1
    return {"groups": dict(table), "labels_of_discordant": {k: dict(v) for k, v in labels.items()}}


def _order(groups: list[Group], cfg: dict, **kw) -> list[Group]:
    def key(g: Group) -> tuple:
        rep = g.rep
        assert rep.assessment is not None and rep.decision is not None
        return shadow_policy.order_key(rep.v1, rep.assessment, rep.decision, rep.job_id, cfg,
                                       avoid=bool(rep.prefs.get("avoid")), **kw)
    return sorted(groups, key=key)


def check_first_order(groups: list[Group], cfg: dict) -> dict:
    """O1 vs O2: wanted jobs found per review, over the labelled Check First groups in each order."""
    queue = [g for g in groups if g.rep.v1 == "check_first" and g.label in LABELS]
    out: dict = {"labelled_check_first_groups": len(queue)}
    for order in ("O1", "O2"):
        ranked = _order(queue, cfg, check_first=order)
        rows = []
        for k in cfg["evaluation"]["review_depths"]:
            top = ranked[:k]
            found = sum(1 for g in top if g.label in WANTED)
            rows.append({"k": k, "reviewed": len(top), "wanted_found": found, "per_review": _ratio(found, len(top))})
        out[order] = rows
    return out


def opportunity_order(groups: list[Group], cfg: dict) -> dict:
    """OM1 vs OM2 inside the Apply and Stretch queues: how often a wanted group comes before a skipped one, and the
    wanted share within the daily budget."""
    budget = int(cfg["evaluation"]["daily_budget"])
    out: dict = {"daily_budget": budget}
    for name, statuses in (("apply", ("A", "B")), ("stretch", ("stretch_review",))):
        queue = [g for g in groups if g.rep.v1 in statuses and g.label in LABELS]
        block: dict = {"labelled": len(queue)}
        for order in ("OM1", "OM2"):
            ranked = _order(queue, cfg, opportunity=order)
            pairs = concordant = 0
            for i, a in enumerate(ranked):
                for b in ranked[i + 1:]:
                    if (a.label in WANTED) != (b.label in WANTED):
                        pairs += 1
                        concordant += a.label in WANTED
            top = ranked[:budget]
            block[order] = {"pairs": pairs, "concordant": concordant, "concordance": _ratio(concordant, pairs),
                            "wanted_within_budget": sum(1 for g in top if g.label in WANTED), "within_budget": len(top)}
        out[name] = block
    return out


def breakdown(groups: list[Group], by: str) -> dict:
    """Per family or per employer: labelled groups and the three approaches' picks, skips among them and recall."""
    buckets: dict[str, list[Group]] = {}
    for g in groups:
        key = (g.rep.job.get("family") if by == "family" else g.rep.job.get("parent")) or "none"
        buckets.setdefault(str(key), []).append(g)
    out = {}
    for key in sorted(buckets):
        rows = buckets[key]
        if not any(g.label in LABELS for g in rows):
            continue
        out[key] = {a: {k: v for k, v in approach_metrics(rows, a).items()
                        if k in ("labelled", "recommended", "skips_among_recommended", "recall_wanted")}
                    for a in ("production", "baseline", "v1")}
    return out


def employer_weights(groups: list[Group]) -> dict[str, float]:
    per: Counter = Counter(str(g.rep.job.get("parent")) for g in groups if g.label in LABELS)
    return {g.group_id: 1 / per[str(g.rep.job.get("parent"))] if g.label in LABELS else 0.0 for g in groups}


# --- calibration lists --------------------------------------------------------------------------------------------------
def read_lists(folder: Path) -> list[dict]:
    """Every calibration list record. A file that can't be read is returned as an unusable record (named in the report),
    never a reason to stop the evaluation."""
    lists = []
    for path in sorted(folder.glob("calibration-list-*.json")) if folder.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            lists.append({"kind": LIST_KIND, "_path": str(path), "_unreadable": f"{type(exc).__name__}", "groups": []})
            continue
        if isinstance(record, dict) and record.get("kind") == LIST_KIND:
            record["_path"] = str(path)
            lists.append(record)
    return lists


def _groups_of(record: dict) -> list:
    """A calibration record's groups; a record whose groups isn't a list has none, never a reason to stop a draw
    (independent Codex review of f984d15, 2026-10-20)."""
    groups = record.get("groups")
    return groups if isinstance(groups, list) else []


def draw_list(groups: list[Group], cfg: dict, seed: str, previous: list[dict], now: str, identity: dict) -> dict:
    """One calibration draw over the groups not drawn before: a Bernoulli(pi) draw per group (seed recorded), plus a
    few groups you already labelled, mixed in to check your labels repeat. The record freezes each group's members and
    every approach's status (its predictions) with the evaluation identity; statuses never appear on the list itself."""
    seen = {m for record in previous for g in _groups_of(record) if _entry_ok(g) for m in g["members"]}
    pis = cfg["sampling"]
    entries, development = [], []
    for g in groups:
        ids = [m.job_id for m in g.members]
        if any(m.labels for m in g.members):
            development.append(g)
            continue
        if not g.open or seen & set(ids):
            continue
        name = sampling.stratum({"production": g.statuses["production"], "baseline": g.statuses["baseline"],
                                 "v1": g.statuses["v1"], "v1_all": [s for k, s in g.statuses.items() if k.startswith("v1:")]})
        if name is None:
            continue
        pi = float(pis[name])
        entries.append({"group_id": g.group_id, "members": ids, "stratum": name, "pi": pi,
                        "drawn": sampling.drawn(g.group_id, pi, seed), "predictions": dict(g.statuses)})
    candidates = sorted(g.group_id for g in development if g.label in LABELS)
    k = min(int(cfg["evaluation"]["relabel_check"]), len(candidates))
    relabel_ids = sorted(random.Random(f"{seed}|relabel").sample(candidates, k))
    relabel = [{"group_id": g.group_id, "members": [m.job_id for m in g.members]} for g in development if g.group_id in relabel_ids]
    return {"kind": LIST_KIND, "version": 2, "created_at": now, "seed": seed, "evaluator": EVALUATOR_VERSION,
            "identity": identity, "pis": pis, "groups": entries, "relabel": relabel}


def _entry_ok(entry) -> bool:
    members = entry.get("members") if isinstance(entry, dict) else None
    return bool(members) and isinstance(members, list) and all(isinstance(m, str) and m for m in members)


def frozen(record: dict) -> bool:
    """A list measured against its own predictions: drawn by this version, with a complete identity, and every group
    well formed (members, a 0 < pi <= 1, a yes/no draw) with every approach's status recorded. Anything else is
    reported and left out, never re-scored under today's rules and never a reason to stop."""
    identity, groups, relabel = record.get("identity"), record.get("groups"), record.get("relabel", [])
    if record.get("version") != 2 or not isinstance(identity, dict) or not all(k in identity for k in PREDICTION_KEYS) \
            or not isinstance(record.get("created_at"), str) or not isinstance(groups, list) or not isinstance(relabel, list):
        return False
    for entry in groups:
        if not _entry_ok(entry) or not isinstance(entry.get("drawn"), bool) \
                or not isinstance(entry.get("pi"), (int, float)) or not 0 < entry["pi"] <= 1:
            return False
        predictions = entry.get("predictions")
        if entry["drawn"] and not (isinstance(predictions, dict) and all(isinstance(predictions.get(a), str) for a in sampling.APPROACHES)):
            return False
    return all(_entry_ok(entry) for entry in relabel)


def list_markdown(record: dict, groups: list[Group]) -> str:
    """The list you label from: titles, employers and links in a shuffled order. No status, tier, stratum or score."""
    by_id = {g.group_id: g for g in groups}
    items = [e for e in record["groups"] if e["drawn"]] + record["relabel"]
    random.Random(f"{record['seed']}|order").shuffle(items)
    lines = [f"# Calibration list {record['created_at'][:10]}", "",
             "Label every role below (apply, stretch or skip) on the review page or with `careeros label JOB_ID LABEL`.",
             "Judge each one on its own. The list deliberately shows no tier, status or score.",
             "A short reason helps later (`--reason \"...\"`, or the reason box on the review page); it is optional.", ""]
    for n, item in enumerate(items, 1):
        g = by_id.get(item["group_id"])
        job = g.rep.job if g else {}
        lines.append(f"{n}. {job.get('title', '?')} - {job.get('employer_name', '?')} ({job.get('location_raw') or 'location not given'})")
        lines.append(f"   {job.get('url') or ''}")
        lines.append(f"   job ids: {', '.join(item['members'])}")
    return "\n".join(lines) + "\n"


def calibration_results(history: dict[str, list[tuple[str, str]]], lists: list[dict], current: dict) -> dict:
    """Population estimates from the labels given on calibration lists after each was drawn, measured against the
    predictions each list froze when it was drawn (never today's statuses), and the relabel check. Only the drawn
    members' labels count. Lists without frozen predictions are named and left out; for the others, what changed in
    the evaluation identity since the draw is reported (the estimates still use the frozen predictions)."""
    rows: dict[str, list[dict]] = {}
    pending = conflicts = 0
    agree = disagree = relabel_pending = 0
    usable = [r for r in lists if frozen(r)]
    until = next_listing(usable)
    for record in usable:
        stop, kind = until.get(record["_path"], {}), prediction_identity(record["identity"])
        for entry in record["groups"]:
            if not entry["drawn"]:
                continue
            label = labels_after(history, entry["members"], record["created_at"], stop)
            if label is None:
                pending += 1
            elif label == "conflict":
                conflicts += 1
            else:
                rows.setdefault(kind, []).append({"pi": entry["pi"], "label": label,
                                                  "statuses": {a: entry["predictions"][a] for a in sampling.APPROACHES}})
        for entry in record.get("relabel", []):
            before = labels_until(history, entry["members"], record["created_at"])
            after = labels_after(history, entry["members"], record["created_at"], stop)
            if after is None:
                relabel_pending += 1
            elif after == before:
                agree += 1
            else:
                disagree += 1
    drawn = sum(1 for r in usable for e in r["groups"] if e["drawn"])
    labelled = sum(len(v) for v in rows.values())
    by_identity = {kind: {"lists": [r["_path"] for r in usable if prediction_identity(r["identity"]) == kind],
                          "labelled": len(found), "estimates": sampling.estimates(found)} for kind, found in sorted(rows.items())}
    kinds = {prediction_identity(r["identity"]) for r in usable}
    return {"lists": [r["_path"] for r in usable], "predictions": "frozen when each list was drawn",
            "lists_without_frozen_predictions": [r["_path"] for r in lists if not frozen(r)],
            "changed_since_draw": {r["_path"]: identity_changes(r["identity"], current) for r in usable},
            "drawn": drawn, "labelled": labelled, "not_yet_labelled": pending,
            "conflicting": conflicts, "complete": drawn > 0 and labelled + conflicts == drawn,
            "prediction_identities": len(kinds),
            # pooled only when every list's predictions came from the same rules; otherwise read estimates_by_identity
            "estimates": sampling.estimates(next(iter(rows.values()))) if len(kinds) == 1 and rows else None,
            "estimates_by_identity": by_identity,
            "relabel_check": {"agree": agree, "disagree": disagree, "not_yet_relabelled": relabel_pending}}


# --- the report ---------------------------------------------------------------------------------------------------------
def job_row(j: JobEval, group_id: str) -> dict:
    a = j.assessment
    row = {"job_id": j.job_id, "group": group_id, "title": j.job.get("title"), "employer": j.job.get("employer_name"),
           "family": j.job.get("family"), "closed": bool(j.job.get("closed_at")), "label": j.label, "label_reason": j.reason,
           "production": j.production, "baseline": j.baseline, "v1": j.v1, "v1_all": j.v1_all, "act": j.act, "queue": j.queue}
    if a is not None and j.decision is not None:
        q = a.qualification
        row.update({"q_low": q.low, "q_high": q.high, "coverage": q.coverage, "q_known": q.known, "cov_low": a.cov_low,
                    "cov_known": a.cov_known, "gap": a.gap, "level_reason": a.level_reason, "o_low": a.opportunity.low,
                    "o_high": a.opportunity.high, "branch": j.decision.branch, "caps": j.decision.caps,
                    "facts_to_resolve": j.decision.facts})
    if j.prefs:
        row["preferences"] = {k: j.prefs[k] for k in ("conflicts", "avoid", "verify", "absent") if j.prefs.get(k)}
    return row


def evaluate(ro: ReadOnlyStore, policy, profile: Profile, cfg: dict, prefs: dict, *, lists: list[dict],
             identity: dict | None = None) -> tuple[dict, list[Group]]:
    jobs, left_out = evaluate_jobs(ro, policy, profile, cfg, prefs)
    labelled = [j for j in jobs if j.label]
    linked = _linked(ro)
    rows = [j.job for j in jobs]
    ev = cfg["evaluation"]
    grouped, pairs = role_identity.group_roles(rows, linked, ev["group_at"], float(ev["strong_similarity"]))
    groups = build_groups(jobs, grouped)
    approaches = ["production", "baseline", "v1"] + sorted(k for k in (groups[0].statuses if groups else {}) if k.startswith("v1:"))
    primary = f"{cfg['primary']['core']}/{cfg['primary']['duration']}"
    group_of = {m: gid for gid, members in grouped.items() for m in members}
    report = {
        "population": {
            "jobs": len(jobs), "judged": sum(j.kind == "judged" for j in jobs), "gate_rejected": sum(j.kind == "rejected" for j in jobs),
            "role_groups": len(groups), "grouped_at": ev["group_at"], "identity_pairs": dict(pairs), "left_out": dict(left_out),
            "status_counts": {a: dict(Counter(g.statuses[a] for g in groups)) for a in ("production", "baseline", "v1")},
            "queues_v1": dict(Counter(j.queue for j in jobs)),
            "open_role_groups": sum(g.open for g in groups),
        },
        "primary_configuration": primary,
        "development": {
            "note": "your existing labels: a development set, descriptive only; never population estimates or grounds for adoption",
            "labelled_groups": sum(g.label in LABELS for g in groups), "conflicting_groups": sum(g.label == "conflict" for g in groups),
            "metrics": all_metrics(groups, approaches),
            "sensitivity": {
                "ungrouped": all_metrics(build_groups(jobs, role_identity.ungrouped(rows)), ["production", "baseline", "v1"]),
                "conflicts_as_wanted": all_metrics(build_groups(jobs, grouped, conflicts="wanted"), ["production", "baseline", "v1"]),
                "conflicts_as_skip": all_metrics(build_groups(jobs, grouped, conflicts="skip"), ["production", "baseline", "v1"]),
                "equal_employer_weight": all_metrics(groups, ["production", "baseline", "v1"], employer_weights(groups)),
            },
            "discordance": {f"{a}_vs_{b}": discordance(groups, a, b)
                            for a, b in (("production", "baseline"), ("production", "v1"), ("baseline", "v1"))},
            "check_first_order": check_first_order(groups, cfg),
            "opportunity_order": opportunity_order(groups, cfg),
            "by_family": breakdown(groups, "family"),
            "by_employer": breakdown(groups, "employer"),
            "label_reasons": {"note": "the free-text reason you gave with your latest label (in each job row); collected only, "
                                      "it changes no status or metric",
                              "labelled_jobs": len(labelled), "with_reason": sum(bool(j.reason) for j in labelled),
                              "with_reason_by_label": dict(Counter(j.label for j in labelled if j.reason))},
        },
        "calibration": calibration_results(_label_history(ro), lists, identity or {}) if lists
        else {"lists": [], "note": "no calibration list drawn yet"},
        "preferences": {"loaded": bool(prefs), "dealbreakers": len(prefs.get("dealbreakers") or []), "avoid": len(prefs.get("avoid") or []),
                        "conflicts": sum(bool(j.prefs.get("conflict")) for j in jobs),
                        "verify_facts": sum(len(j.prefs.get("verify") or []) for j in jobs),
                        "avoid_annotations": sum(bool(j.prefs.get("avoid")) for j in jobs)},
        "jobs": [job_row(j, group_of.get(j.job_id, j.job_id)) for j in jobs],
    }
    return report, groups


_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d\u2060\u00a0"), " ")


def criteria_text(path: Path | None) -> str | None:
    """Your written adoption criteria as text (UTF-8 with or without a BOM, or UTF-16 as Notepad saves "Unicode"), line
    endings and invisible characters normalised; None when the file is missing, unreadable or has no visible text."""
    if path is None or not Path(path).is_file():
        return None
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    text = "\n".join(line.rstrip() for line in text.translate(_INVISIBLE).replace("\r\n", "\n").split("\n")).strip()
    return text or None


def criteria_sha256(path: Path | None) -> str | None:
    """The SHA-256 of your criteria's normalised text (a re-save with other line endings is the same criteria)."""
    text = criteria_text(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def run(db: Path, reports: Path, policy, profile: Profile, cfg: dict, prefs: dict, *, snapshot: Path | None = None,
        draw: bool = False, seed: str | None = None, clock: Clock | None = None, adoption_criteria: Path | None = None) -> dict:
    """Snapshot (unless one is named), evaluate, write the report (and a calibration list when asked). Returns the report.
    A draw needs your adoption criteria written down first (`adoption_criteria`); it refuses before anything is copied."""
    from careeros.pipeline import decision_fingerprint
    criteria = criteria_sha256(adoption_criteria)
    if draw and criteria is None:
        raise ValueError("adoption_criteria_missing: before any calibration draw, write down what results would make you "
                         f"adopt a scoring approach, in {adoption_criteria or 'the adoption criteria file'}")
    if draw and snapshot is not None:
        raise ValueError("draw_needs_a_fresh_snapshot: a calibration list is drawn from a new copy of the live database, "
                         "never from a named snapshot (labels given since it was taken would be missed)")
    clock = clock or Clock()
    now = clock.now()
    stamp = now.replace("-", "").replace(":", "").split(".")[0] + "Z"
    calibration_dir, out_dir = Path(reports) / "calibration", Path(reports) / "scoring_eval"
    with tempfile.TemporaryDirectory(prefix="careeros-eval-") as tmp:
        if snapshot is None:
            meta = make_snapshot(Path(db), Path(tmp) / "snapshot.db")
            path, kept = Path(meta["path"]), False
        else:
            path, kept = Path(snapshot), True
            meta = {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        identity = evaluation_identity(cfg, policy, profile, prefs, meta["sha256"], criteria)
        ro = ReadOnlyStore(path, clock)
        try:
            lists = read_lists(calibration_dir)
            report, groups = evaluate(ro, policy, profile, cfg, prefs, lists=lists, identity=identity)
        finally:
            ro.close()
        unchanged = file_sha256(path) == meta["sha256"]
    report = {"evaluator": EVALUATOR_VERSION, "rubric_version": cfg.get("version"), "generated_at": now,
              "snapshot": {"sha256": meta["sha256"], "bytes": meta["bytes"], "named": kept, "unchanged_after": unchanged,
                           **({"path": meta["path"]} if kept else {})},
              "decision_fingerprint": decision_fingerprint(SimpleNamespace(policy=policy)),   # type: ignore[arg-type]
              "identity": identity,
              "adoption_criteria": {"recorded": criteria is not None, "sha256": criteria,
                                    "note": "written by you before the first draw; nothing is ever adopted automatically"},
              **report}
    if draw:
        seed = seed or secrets.token_hex(8)
        previous = read_lists(calibration_dir)
        record = draw_list(groups, cfg, seed, previous, now, identity)
        record["adoption_criteria_text"] = criteria_text(adoption_criteria)   # kept with the list: write-once, like it
        earlier = sorted({str(h) for r in previous if frozen(r) if (h := r["identity"].get("adoption_criteria_sha256"))})
        calibration_dir.mkdir(parents=True, exist_ok=True)
        base = calibration_dir / f"calibration-list-{stamp}-{re.sub(r'[^A-Za-z0-9_-]', '_', seed)[:40]}"
        if base.with_suffix(".json").exists() or base.with_suffix(".md").exists():
            raise FileExistsError(f"calibration_list_exists:{base.name}")   # never overwrite a list you may be labelling
        base.with_suffix(".json").write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        base.with_suffix(".md").write_text(list_markdown(record, groups), encoding="utf-8")
        report["calibration_draw"] = {"seed": seed, "list": str(base.with_suffix(".md")), "record": str(base.with_suffix(".json")),
                                      "drawn": sum(e["drawn"] for e in record["groups"]), "considered": len(record["groups"]),
                                      "relabel_check": len(record["relabel"]),
                                      "criteria_changed_since_earlier_lists": bool(earlier) and earlier != [criteria]}
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"scoring-eval-{stamp}.json"
    report_path.write_text(json.dumps(report, indent=1, sort_keys=True, default=str), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def summary(report: dict) -> str:
    """A few lines for the console: counts and rates only, no titles or employers (those stay in the report file)."""
    pop, dev = report["population"], report["development"]
    m = dev["metrics"]
    primary = report["primary_configuration"]

    def fmt(value) -> str:
        return "-" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)

    lines = [f"Shadow scoring evaluation (read-only; nothing adopted). Rubric {report['rubric_version']}, primary {primary}.",
             f"Snapshot {report['snapshot']['sha256'][:12]} ({'unchanged' if report['snapshot']['unchanged_after'] else 'CHANGED'}); "
             f"decision fingerprint {report['decision_fingerprint'][:16]}.",
             f"Population: {pop['jobs']} jobs ({pop['judged']} judged, {pop['gate_rejected']} gate-rejected) in "
             f"{pop['role_groups']} role groups ({pop['grouped_at']} identity).",
             f"Development set (your labels, descriptive only): {dev['labelled_groups']} labelled groups, "
             f"{dev['conflicting_groups']} with conflicting labels left out.",
             f"{'':34}{'production':>11}{'baseline':>10}{'v1':>8}"]
    for key, name in (("recommended", "recommended (labelled groups)"), ("skips_among_recommended", "skips among them"),
                      ("skip_share_among_recommendations", "skip share"), ("recall_wanted", "recall, apply + stretch"),
                      ("recall_apply", "recall, apply"), ("false_hard_rejections", "wanted but gate-rejected")):
        lines.append(f"  {name:32}{fmt(m['production'][key]):>11}{fmt(m['baseline'][key]):>10}{fmt(m['v1'][key]):>8}")
    lines.append(f"  v1 Check First: {fmt(m['v1']['check_first_labelled'])} labelled groups, yield {fmt(m['v1']['check_first_yield'])}; "
                 f"recall with Check First {fmt(m['v1']['recall_wanted_with_check_first'])}.")
    cal = report["calibration"]
    if cal.get("lists"):
        changed = sorted({k for keys in cal["changed_since_draw"].values() for k in keys})
        lines.append(f"Calibration (predictions frozen at each draw): {cal['drawn']} groups drawn, {cal['labelled']} labelled, "
                     f"{cal['not_yet_labelled']} waiting." + (f" Changed since a draw: {', '.join(changed)}." if changed else ""))
        if cal.get("prediction_identities", 1) > 1:
            lines.append(f"  Lists come from {cal['prediction_identities']} different rule sets: estimates are reported per rule set.")
    elif cal.get("lists_without_frozen_predictions"):
        lines.append("Calibration: no usable list (draw a new one: --draw).")
    else:
        lines.append("Calibration: no list drawn yet (population estimates need one: --draw).")
    if cal.get("lists_without_frozen_predictions"):
        lines.append(f"Left out (no frozen predictions, or unreadable): {len(cal['lists_without_frozen_predictions'])} list(s).")
    if report.get("calibration_draw", {}).get("criteria_changed_since_earlier_lists"):
        lines.append("WARNING: your adoption criteria changed since earlier lists were drawn.")
    if report.get("calibration_draw"):
        d = report["calibration_draw"]
        lines.append(f"New calibration list: {d['drawn']} of {d['considered']} groups drawn (+{d['relabel_check']} relabel checks), "
                     f"seed {d['seed']}: {d['list']}")
    lines.append("Adoption criteria: " + ("recorded." if report["adoption_criteria"]["recorded"]
                                          else "not written yet (a calibration draw needs them first)."))
    lines.append(f"Report: {report['report_path']}")
    return "\n".join(lines)


def main(paths: dict, args, clock: Clock | None = None) -> None:
    from careeros.core.policy import load_policy
    from careeros.paths import CONFIG_DIR
    from careeros.qualify.profile import load_profile
    policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", paths["overrides"])
    profile = load_profile(paths["profile"])
    cfg = rubric.load_config()
    prefs = load_preferences(paths.get("preferences"))
    try:
        report = run(paths["db"], paths["reports"], policy, profile, cfg, prefs,
                     snapshot=Path(args.snapshot) if getattr(args, "snapshot", None) else None,
                     draw=bool(getattr(args, "draw", False)), seed=getattr(args, "seed", None), clock=clock,
                     adoption_criteria=paths.get("adoption_criteria"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(summary(report))
