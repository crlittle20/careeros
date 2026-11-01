"""Shadow scoring rubric v1 (SCORING_SHADOW_SPEC 2026-10-16): two separate axes, Qualification and Opportunity.

PURE and READ-ONLY: every function here takes values and returns values. Nothing reads or writes the database, the
network or the decision policy's fingerprint, so this module can never change a production tier or re-evaluate a job.
It is used only by the shadow evaluator and its tests.

Each axis is a set of sections; each section a set of items with an evidence state:
  evidenced         known; earns its credit (0..1)
  unknown           not known (e.g. no profile evidence, the posting doesn't say): 0 in the lower bound, 1 in the upper
  confirmed_absent  known gap (you said you don't have it, or a planned / in-progress credential): 0 in both bounds
For an axis with applicable weight A:
  Q_low  = earned / A          (controls Tier A/B)        Q_high = (earned + unknown) / A   (controls Check First)
  C      = known / A           (evidence coverage)        Q_known = earned / known         (display only)
so Q_high - Q_low = 100 - C. These are bounds on the RUBRIC score under the current reading (the requirements CareerOS
extracted, your profile as recorded, these rules); they are not bounds on your true qualification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from careeros.normalize.posting import _IC_MANAGER, contains_term
from careeros.paths import CONFIG_DIR

from . import certifications as certification_mod
from . import experience as experience_mod
from .gate import annualize
from .profile import EVIDENCE_WEIGHT, Profile
from .scores import REQUIRED_TYPES, SENIORITY_RANK, career_value, domain_level, skill_coverage

EVIDENCED, UNKNOWN, ABSENT = "evidenced", "unknown", "confirmed_absent"
LEVELS = ("manager", "principal", "senior", "mid", "entry")
SHADOW_CONFIG = CONFIG_DIR / "shadow_scoring.yaml"


def load_config(path: Path | None = None) -> dict:
    """The shadow configuration: a separate file, never part of the decision policy."""
    return yaml.safe_load((path or SHADOW_CONFIG).read_text(encoding="utf-8"))


@dataclass
class Item:
    name: str
    weight: float
    state: str
    credit: float = 0.0
    detail: dict = field(default_factory=dict)


@dataclass
class Section:
    key: str
    points: float
    applicable: bool
    items: list[Item] = field(default_factory=list)
    reason: str = ""

    def amounts(self) -> tuple[float, float, float, float]:
        """(total, earned, unknown, known) item weight."""
        total = sum(i.weight for i in self.items)
        earned = sum(i.weight * i.credit for i in self.items if i.state == EVIDENCED)
        unknown = sum(i.weight for i in self.items if i.state == UNKNOWN)
        return total, earned, unknown, total - unknown

    def fractions(self) -> tuple[float, float, float]:
        """(low, high, known share) of this section, each 0..1."""
        total, earned, unknown, known = self.amounts()
        if total <= 0:
            return 0.0, 1.0, 0.0
        return earned / total, (earned + unknown) / total, known / total


@dataclass
class Axis:
    sections: list[Section]

    def _sums(self) -> tuple[float, float, float, float]:
        applicable = [s for s in self.sections if s.applicable]
        weight = sum(s.points for s in applicable)
        low = sum(s.points * s.fractions()[0] for s in applicable)
        high = sum(s.points * s.fractions()[1] for s in applicable)
        known = sum(s.points * s.fractions()[2] for s in applicable)
        return weight, low, high, known

    @property
    def applicable_weight(self) -> float:
        return self._sums()[0]

    @property
    def low(self) -> float:
        weight, low, _, _ = self._sums()
        return round(100 * low / weight, 1) if weight else 0.0

    @property
    def high(self) -> float:
        weight, _, high, _ = self._sums()
        return round(100 * high / weight, 1) if weight else 100.0

    @property
    def coverage(self) -> float:
        weight, _, _, known = self._sums()
        return round(100 * known / weight, 1) if weight else 0.0

    @property
    def known(self) -> float | None:
        _, low, _, known = self._sums()
        return round(100 * low / known, 1) if known > 0 else None

    @property
    def mid(self) -> float:
        """Neutral imputation (each unknown counted at half): used ONLY to order jobs, never for a status or a note."""
        return round((self.low + self.high) / 2, 1)

    def section(self, key: str) -> Section:
        return next(s for s in self.sections if s.key == key)


@dataclass
class Assessment:
    version: str
    qualification: Axis
    opportunity: Axis
    listed_weight: float           # importance points of required skills/domains the posting lists
    min_weight: float              # m: fewer than this is too few to judge coverage
    gap: str                       # strong_match .. severe_gap | unresolved | none
    level_reason: str | None       # posting_level_unspecified | posting_level_ambiguous | candidate_level_unset
    core_items: list[dict]
    facts_to_resolve: list[str]
    decisive_unknowns: list[str] = field(default_factory=list)

    @property
    def cov_low(self) -> float:
        return round(100 * self.qualification.section("coverage").fractions()[0], 1)

    @property
    def cov_high(self) -> float:
        return round(100 * self.qualification.section("coverage").fractions()[1], 1)

    @property
    def cov_known(self) -> float:
        return round(100 * self.qualification.section("coverage").fractions()[2], 1)


def posting_level(title_norm: str | None, terms: dict) -> tuple[str | None, str | None]:
    """(level, None) or (None, reason). A title with no level term is UNSPECIFIED and one with terms from two different
    levels ("senior associate") is AMBIGUOUS; both are unknown here (production keeps its own reading)."""
    title = _IC_MANAGER.sub(" ", title_norm or "")
    found = [level for level in LEVELS if any(contains_term(title, t) for t in terms.get(level, []))]
    if not found:
        return None, "posting_level_unspecified"
    if len(found) > 1:
        return None, "posting_level_ambiguous"
    return found[0], None


def _profile_state(name: str, lacks: set[str]) -> str:
    return ABSENT if name.lower() in lacks else UNKNOWN


def _coverage(job: dict, requirements: list, profile: Profile, policy, lacks: set[str]) -> tuple[list[Item], float, float, list[dict]]:
    cov = skill_coverage(job, requirements, profile, policy)
    m = float((policy.get("skill_importance", default={}) or {}).get("min_weight_to_judge", 0) or 0)
    types: dict[tuple[str, str], str] = {}
    for r in requirements:
        if r.requirement_type in REQUIRED_TYPES and r.category in ("skill", "domain"):
            types.setdefault((r.category, r.name), r.requirement_type)
    items, core = [], []
    for i in cov["items"]:
        state = EVIDENCED if i["credit"] > 0 else _profile_state(i["requirement"], lacks)
        items.append(Item(i["requirement"], i["weight"], state, i["credit"] if state == EVIDENCED else 0.0,
                          {"importance": i["importance"], "basis": i["basis"]}))
        if i["importance"] == "core":
            core.append({"name": i["requirement"], "state": state,
                         "requirement_type": types.get((i["category"], i["requirement"]), "hard_required"),
                         "importance_source": "stated" if "named in the job title" in i["basis"] else "inferred"})
    listed = float(cov["weight_total"] or 0)
    if listed < m:
        items.append(Item("required skills the posting does not list", m - listed, UNKNOWN))
    return items, listed, m, core


def _areas(requirements: list, profile: Profile, lacks: set[str]) -> list[Item]:
    names = sorted({r.name for r in requirements if r.requirement_type in REQUIRED_TYPES and r.category in ("experience", "years_experience")})
    if not names:
        return [Item("experience areas the posting does not list", 1.0, UNKNOWN)]
    items = []
    for name in names:
        if profile.function(name) or profile.years_for(name) is not None:
            items.append(Item(name, 1.0, EVIDENCED, 1.0))
        else:
            items.append(Item(name, 1.0, _profile_state(name, lacks)))
    return items


def _duration(requirements: list, profile: Profile, policy, mode: str) -> tuple[Section, str]:
    em = experience_mod.evaluate(requirements, profile, policy)
    if not em["lines"]:
        return Section("duration", 0, False, reason="no years requirement"), "none"
    if em["decisive_line"] is None:
        return Section("duration", 0, True, [Item("years requirement (unresolved)", 1.0, UNKNOWN)]), "unresolved"
    line = em["lines"][em["decisive_line"]]
    if mode == "D1":
        fraction = (line["absolute_points"] + line["coverage_points"]) / 16
    else:
        fraction = line["score"] / 20
    return Section("duration", 0, True, [Item(line["area"], 1.0, EVIDENCED, round(fraction, 4),
                                              {"score": line["score"], "required_years": line["required_years"]})]), em["gap_class"]


def _level(job: dict, profile: Profile, policy) -> tuple[Item, str | None, int | None]:
    posting, reason = posting_level(job.get("title_norm"), policy.get("seniority_terms", default={}) or {})
    mine = profile.level_rank()
    if reason or posting is None:
        return Item("posting level", 1.0, UNKNOWN), reason, None
    rank = SENIORITY_RANK[posting]
    if mine is None:
        return Item("posting level", 1.0, UNKNOWN), "candidate_level_unset", rank
    step = rank - mine
    return Item("posting level", 1.0, EVIDENCED, 1.0 if step <= 0 else 0.7 if step == 1 else 0.4, {"posting": posting}), None, rank


def _preferred(requirements: list, profile: Profile, policy, lacks: set[str]) -> list[Item]:
    items = []
    for name in sorted({r.name for r in requirements if r.category == "skill" and r.requirement_type == "preferred"}):
        hit = profile.skill(name)
        if hit:
            items.append(Item(name, 1.0, EVIDENCED, EVIDENCE_WEIGHT.get(hit.get("evidence") or "adjacent", 0.4)))
        else:
            items.append(Item(name, 1.0, _profile_state(name, lacks)))
    for record in certification_mod.analyse(requirements, profile, policy):
        if record["requirement_type"] != "preferred":
            continue
        if record.get("satisfied"):
            items.append(Item(record["normalized_name"], 1.0, EVIDENCED, 1.0))
        elif record.get("candidate_status") in ("planned", "in_progress"):
            items.append(Item(record["normalized_name"], 1.0, ABSENT))
        else:
            items.append(Item(record["normalized_name"], 1.0, UNKNOWN))
    return items


def _opportunity(job: dict, profile: Profile, policy, weights: dict, posting_rank: int | None) -> Axis:
    value = career_value(job, profile, policy)["dims"]
    comp = policy.get("compensation", default={}) or {}
    target_hourly, floor_hourly = comp.get("target_hourly"), comp.get("floor_hourly")
    target = comp.get("target_annual") or (target_hourly * 2080 if target_hourly else None)
    floor = comp.get("floor_annual") or (floor_hourly * 2080 if floor_hourly else None)
    low = None
    if job.get("comp_min") is not None or job.get("comp_max") is not None:
        low = annualize(job.get("comp_min") or job.get("comp_max"), job.get("comp_period"))
    if low is None:
        pay = Item("pay", 1.0, UNKNOWN, detail={"why": "not posted"})
    else:
        if floor and target and target > floor:
            pay = Item("pay", 1.0, EVIDENCED, max(0.0, min(1.0, (low - floor) / (target - floor))))
        elif target:
            pay = Item("pay", 1.0, EVIDENCED, 1.0 if low >= target else 0.0)
        else:
            pay = Item("pay", 1.0, UNKNOWN, detail={"why": "targets not set"})
    mine = profile.level_rank()
    if posting_rank is None or mine is None:
        progression = Item("progression", 1.0, UNKNOWN)
    else:
        progression = Item("progression", 1.0, EVIDENCED, 1.0 if posting_rank > mine else 0.8 if posting_rank == mine else 0.4)
    remote = job.get("remote_type") or "unknown"
    employment = job.get("employment_type") or "unknown"
    sections = [
        Section("pay", weights["pay"], True, [pay]),
        Section("progression", weights["progression"], True, [progression]),
        Section("domain", weights["domain"], True, [Item("domain", 1.0, EVIDENCED, domain_level(job, policy)["value"])]),
        Section("growth", weights["growth"], True, [Item("growth", 1.0, EVIDENCED, value["skill_growth"]["value"])]),
        Section("arrangement", weights["arrangement"], True,
                [Item("arrangement", 1.0, UNKNOWN) if remote == "unknown" else Item("arrangement", 1.0, EVIDENCED, value["location"]["value"])]),
        Section("terms", weights["terms"], True,
                [Item("terms", 1.0, UNKNOWN) if employment == "unknown" else Item("terms", 1.0, EVIDENCED, value["stability"]["value"])]),
    ]
    return Axis(sections)


def assess(job: dict, requirements: list, profile: Profile, policy, cfg: dict | None = None, *, duration: str = "D2",
           decisive_unknowns: list[str] | None = None) -> Assessment:
    """Both axes for one job, from the requirements CareerOS extracted, your profile and the policy."""
    cfg = cfg or load_config()
    qw, ow = cfg["qualification"], cfg["opportunity"]
    lacks = {str(x).lower() for x in profile.data.get("lacks") or []}
    cov_items, listed, m, core = _coverage(job, requirements, profile, policy, lacks)
    dur, gap = _duration(requirements, profile, policy, duration)
    dur.points = qw["duration"]
    level_item, level_reason, posting_rank = _level(job, profile, policy)
    preferred = _preferred(requirements, profile, policy, lacks)
    sections = [
        Section("coverage", qw["coverage"], True, cov_items),
        Section("areas", qw["areas"], True, _areas(requirements, profile, lacks)),
        dur,
        Section("level", qw["level"], True, [level_item], reason=level_reason or ""),
        Section("preferred", qw["preferred"], bool(preferred), preferred, reason="" if preferred else "none listed"),
    ]
    facts = []
    if listed < m or any(i.state == UNKNOWN and i.name == "required skills the posting does not list" for i in cov_items):
        facts.append("required skills not listed or too few")
    if any(i.state == UNKNOWN and i.detail.get("importance") in ("core", "important") for i in cov_items):
        facts.append("core or important skills without profile evidence")
    if any(i.state == UNKNOWN for i in sections[1].items):
        facts.append("experience areas not confirmed")
    if gap == "unresolved":
        facts.append("years requirement unresolved")
    if level_reason:
        facts.append(level_reason.replace("_", " "))
    return Assessment(version=cfg.get("version", "rubric-v1"), qualification=Axis(sections),
                      opportunity=_opportunity(job, profile, policy, ow, posting_rank), listed_weight=listed, min_weight=m,
                      gap=gap, level_reason=level_reason, core_items=core, facts_to_resolve=facts,
                      decisive_unknowns=list(decisive_unknowns or []))
