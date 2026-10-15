"""Data-driven state machines (pattern from Incident Tracker states.json / reference_model.allowed).

A matrix file declares states with their Factory Core category, the initial state, terminal states and the
allowed edges. History checks follow the Incident Tracker consistency invariants.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .clock import parse_utc

CATEGORIES = {"not_started", "active", "blocked", "completed", "cancelled"}


class TransitionRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Matrix:
    workflow: str
    states: dict
    initial_state: str
    terminal: frozenset
    edges: dict  # (from, to) -> requires_reason

    def category(self, state: str) -> str:
        if state not in self.states:
            raise TransitionRejected("unknown_state")
        return self.states[state]

    def check(self, from_state: str, to_state: str, reason_code: str | None = None) -> None:
        if from_state not in self.states or to_state not in self.states:
            raise TransitionRejected("unknown_state")
        if from_state == to_state:
            raise TransitionRejected("self_transition")
        if from_state in self.terminal:
            raise TransitionRejected("terminal_state")
        if (from_state, to_state) not in self.edges:
            raise TransitionRejected("transition_not_allowed")
        if self.edges[(from_state, to_state)] and not (reason_code and reason_code.strip()):
            raise TransitionRejected("reason_required")


def load_matrix(path: Path) -> Matrix:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    states = data["states"]
    if not states or set(states.values()) - CATEGORIES:
        raise ValueError("matrix_categories")
    if data["initial_state"] not in states or any(s not in states for s in data["terminal"]):
        raise ValueError("matrix_states")
    edges = {}
    for edge in data["allowed"]:
        pair = (edge["from"], edge["to"])
        if pair[0] not in states or pair[1] not in states or pair[0] == pair[1] or pair in edges:
            raise ValueError("matrix_edge")
        if pair[0] in data["terminal"]:
            raise ValueError("matrix_terminal_has_exit")
        edges[pair] = bool(edge.get("requires_reason", False))
    return Matrix(data["workflow"], dict(states), data["initial_state"], frozenset(data["terminal"]), edges)


def verify_history(matrix: Matrix, transitions: list[dict], audits: list[dict] | None = None,
                   *, initial_entered_at: str | None = None) -> set[str]:
    """Return invariant violations for one workflow's transition history (empty set = consistent)."""
    errors: set[str] = set()
    state, at = matrix.initial_state, initial_entered_at
    for expected, row in enumerate(sorted(transitions, key=lambda r: r["sequence"]), start=1):
        if row["sequence"] != expected:
            errors.add("sequence_gap")
        if row["from_state"] != state:
            errors.add("broken_chain")
        try:
            matrix.check(row["from_state"], row["to_state"], row.get("reason_code"))
        except TransitionRejected:
            errors.add("illegal_transition")
        if at is not None and parse_utc(row["transitioned_at"]) < parse_utc(at):
            errors.add("time_order")
        if audits is not None:
            matching = [a for a in audits if a.get("transition_id") == row["transition_id"]]
            if len(matching) != 1:
                errors.add("transition_audit_missing_or_duplicate")
            elif (matching[0].get("previous_state") != row["from_state"] or matching[0].get("new_state") != row["to_state"]
                  or matching[0].get("occurred_at") != row["transitioned_at"]):
                errors.add("transition_audit_mismatch")
        state, at = row["to_state"], row["transitioned_at"]
    return errors
