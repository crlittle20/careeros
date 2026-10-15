"""Factory Core 0.2.0 AuditEvent and ExceptionRecord shapes (contracts adopted as-is)."""
from __future__ import annotations

import re

from .clock import parse_utc

CONTRACT_VERSION = "0.2.0"
OUTCOMES = {"success", "failure", "partial", "informational"}
ACTOR_TYPES = {"human", "system", "agent", "external"}
EXCEPTION_TYPES = {"validation", "business_rule", "integration", "system", "security", "data_quality"}
SEVERITIES = {"low", "medium", "high", "critical"}
# Core allows accepted/ignored, but those need a recorded owner decision; CareerOS Phase 1 does not support them.
EXCEPTION_STATUSES = {"open", "investigating", "resolved"}
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")

AUDIT_REQUIRED = ("event_id", "event_type", "occurred_at", "project_id", "project_version", "source", "outcome")
EXCEPTION_REQUIRED = ("exception_id", "occurred_at", "project_id", "source", "exception_type", "severity", "message", "status")


def _time_ok(value) -> bool:
    try:
        parse_utc(value)
        return True
    except ValueError:
        return False


def validate_audit_event(event: dict) -> list[str]:
    errors = [f"missing:{k}" for k in AUDIT_REQUIRED if not event.get(k)]
    if event.get("contract_version") != CONTRACT_VERSION:
        errors.append("contract_version")
    if event.get("event_type") and not _SNAKE.match(event["event_type"]):
        errors.append("event_type_format")
    if event.get("outcome") and event["outcome"] not in OUTCOMES:
        errors.append("outcome")
    if event.get("actor_type") and event["actor_type"] not in ACTOR_TYPES:
        errors.append("actor_type")
    if event.get("occurred_at") and not _time_ok(event["occurred_at"]):
        errors.append("occurred_at")
    if not isinstance(event.get("metadata", {}), dict):
        errors.append("metadata")
    return errors


def validate_exception(record: dict) -> list[str]:
    errors = [f"missing:{k}" for k in EXCEPTION_REQUIRED if not record.get(k)]
    if record.get("contract_version") != CONTRACT_VERSION:
        errors.append("contract_version")
    if record.get("exception_type") not in EXCEPTION_TYPES:
        errors.append("exception_type")
    if record.get("severity") not in SEVERITIES:
        errors.append("severity")
    if record.get("status") not in EXCEPTION_STATUSES:
        errors.append("status")
    if record.get("occurred_at") and not _time_ok(record["occurred_at"]):
        errors.append("occurred_at")
    if record.get("retry_count", 0) < 0:
        errors.append("retry_count")
    if record.get("status") == "resolved":
        if not (record.get("resolution") or "").strip() or not record.get("resolved_at"):
            errors.append("resolution_required")
        elif parse_utc(record["resolved_at"]) < parse_utc(record["occurred_at"]):
            errors.append("resolved_before_occurred")
    return errors
