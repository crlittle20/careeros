"""Structured requirement records (v0.3 §6E)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from careeros.core.fingerprint import fingerprint

REQUIREMENT_TYPES = ("hard_required", "required_with_equivalency", "preferred", "responsibility", "contextual", "ambiguous")
CATEGORIES = ("skill", "experience", "years_experience", "education", "certification", "license", "clearance",
              "work_authorization", "location", "schedule", "travel", "domain")


STRENGTH = {"hard_required": "hard", "required_with_equivalency": "hard", "preferred": "preferred",
            "responsibility": "contextual", "ambiguous": "unknown"}


def requirement_strength(requirement_type: str) -> str:
    """Prompts v2.2 vocabulary for the stored requirement_type. Only 'hard' can ever cause a rejection."""
    return STRENGTH.get(requirement_type, "unknown")


@dataclass(frozen=True)
class Requirement:
    category: str
    name: str
    requirement_type: str
    source_text: str
    confidence: float
    years: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Extraction:
    extractor: str
    requirements: list
    sections: list
    notes: list

    def requirements_fingerprint(self) -> str:
        material = sorted(((r.requirement_type, r.category, r.name, r.years) for r in self.requirements
                           if r.requirement_type != "responsibility"),
                          key=lambda t: (t[0], t[1], t[2], -1.0 if t[3] is None else t[3]))
        return fingerprint(material)
