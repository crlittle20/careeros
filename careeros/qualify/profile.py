"""Candidate profile v0 (vault). Unknown values stay None; they become flags, never assumed facts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from careeros.core.fingerprint import fingerprint
from careeros.core.policy import load_yaml
from careeros.normalize.posting import contains_term as contains
from careeros.normalize.posting import norm

DEGREE_RANK = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}
LEVEL_RANK = {"entry": 1, "mid": 2, "senior": 3, "lead": 4, "manager": 5}
EVIDENCE_WEIGHT = {"direct": 1.0, "strongly_transferable": 0.7, "adjacent": 0.4}


@dataclass
class Profile:
    data: dict
    fingerprint: str

    @property
    def approved(self) -> bool:
        return self.data.get("status") == "approved"

    @property
    def state(self) -> str | None:
        return self.data.get("state")

    def _items(self, key: str) -> list[dict]:
        return [i for i in (self.data.get(key) or []) if isinstance(i, dict) and i.get("name")]

    def skill(self, name: str) -> dict | None:
        """Profile skill matching a canonical skill name, directly or through an approved equivalence."""
        wanted = norm(name)
        for item in self._items("skills"):
            if norm(item["name"]) == wanted:
                return {**item, "via": "direct"}
        for eq in self.data.get("equivalences") or []:
            if norm(eq.get("posting_term", "")) == wanted:
                target = next((i for i in self._items("skills") if norm(i["name"]) == norm(eq.get("counts_as", ""))), None)
                if target:
                    return {**target, "via": "equivalence"}
        return None

    def function(self, phrase: str) -> dict | None:
        """A profile function whose words all appear (by stem) in the posting phrase, e.g. 'business systems
        analysis' matches 'experience as a business systems analyst in healthcare'."""
        wanted = _stems(phrase)
        best, best_score = None, 0.0
        for item in self._items("functions"):
            have = _stems(item["name"])
            if not have:
                continue
            # all words of a short function name, or at least two-thirds of a longer one, must appear in the phrase
            score = len(have & wanted) / len(have)
            needed = 1.0 if len(have) <= 2 else 2 / 3
            if score >= needed and (score, len(have)) > (best_score, len(_stems(best["name"])) if best else 0):
                best, best_score = item, score
        return best

    def domains(self) -> set[str]:
        return {norm(i["name"]) for i in self._items("domains")}

    def degree_rank(self) -> int | None:
        ranks = [DEGREE_RANK.get(e.get("level")) for e in self.data.get("education") or []
                 if isinstance(e, dict) and e.get("completed") is True and e.get("level") in DEGREE_RANK]
        if ranks:
            return max(ranks)
        known = [e for e in self.data.get("education") or [] if isinstance(e, dict) and e.get("level")]
        return 0 if known else None

    def certifications(self) -> set[str]:
        names = set()
        for cert in self.data.get("certifications") or []:
            if isinstance(cert, dict):
                names.add(norm(cert.get("name", "")))
                names.update(norm(m) for m in cert.get("matches") or [])
        return names

    def has_certification(self, name: str) -> bool:
        """'six sigma' is satisfied by 'lean six sigma green belt'; 'pl-200' is not satisfied by 'pl-900'."""
        wanted = norm(name)
        return bool(wanted) and any(wanted == c or contains(c, wanted) for c in self.certifications())

    def licenses(self) -> set[str]:
        return {norm(l.get("name", "")) for l in self.data.get("licenses") or [] if isinstance(l, dict)}

    def years_for(self, name: str) -> float | None:
        """Years used for SCREENING. An item's `screening_years` (set by the user) overrides `years` here only;
        application answers and resumes (Phase 2) must use the factual `years` and real dates."""
        hit = self.skill(name) or self.function(name)
        if hit and _screening_years(hit) is not None:
            return _screening_years(hit)
        # e.g. "3+ years of claims experience" or "medical claims operations" vs the claims domain
        years = [_screening_years(d) for d in self._items("domains")
                 if _screening_years(d) is not None and (norm(d["name"]) == norm(name) or contains(norm(name), norm(d["name"])))]
        if years:
            return max(years)
        # e.g. "2+ years as a Data Analyst": role titles YOU confirmed your years count for (profile `role_titles`).
        # An `exact: true` title only matches that exact wording: "analyst" answers "years as an analyst", never
        # "years as a Data Analyst".
        roles = [_screening_years(r) for r in self._items("role_titles")
                 if _screening_years(r) is not None and (norm(r["name"]) == norm(name)
                                                         or (not r.get("exact") and contains(norm(name), norm(r["name"]))))]
        if roles:
            return max(roles)
        if norm(name) in GENERIC_EXPERIENCE:  # "3+ years of relevant experience": your total professional years
            total = self.data.get("total_professional_years")
            return float(total) if total is not None else None
        return None

    def level_rank(self) -> int | None:
        return LEVEL_RANK.get(self.data.get("current_level"))

    def familiar_with(self, *names: str) -> bool:
        known = {norm(n) for n in self.data.get("familiar_employers") or []}
        return any(norm(n) in known for n in names if n)


# "3+ years of relevant / related / professional / work / hands-on experience" names no specific area.
GENERIC_EXPERIENCE = {"professional experience", "experience", "professional", "relevant", "relevant experience", "related",
                      "related experience", "work", "work experience", "relevant work", "relevant work experience",
                      "related work experience", "professional work experience", "prior", "previous", "industry",
                      "industry experience", "progressive", "progressive experience", "comparable", "similar", "overall",
                      "total", "hands on", "hands on experience", "full time"}

_STOP = {"a", "an", "the", "of", "in", "and", "or", "with", "as", "for", "to", "on", "experience", "years", "year"}


def _screening_years(item: dict) -> float | None:
    value = item.get("screening_years", item.get("years"))
    return float(value) if value is not None else None


def _stems(text: str) -> set[str]:
    return {w[:6] for w in norm(text).split() if w not in _STOP and len(w) > 1}


def load_profile(path: Path) -> Profile:
    data = load_yaml(path)
    return Profile(data=data, fingerprint=fingerprint(data))
