"""Recognising job-board URLs without visiting them (2026-10-12)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from careeros.core import private_sites

# Public boards on the four allowed ATSs: their human-facing hosts.
BOARD_HOSTS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
    "workable": ("apply.workable.com",),
}
# Employer application systems CareerOS never reads automatically: a link to one becomes a manual-source record.
# (LinkedIn / Indeed / Glassdoor are aggregators, not an employer's own board, so they are not listed here.)
MANUAL_SYSTEMS = (("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"), ("smartrecruiters.com", "smartrecruiters"),
                  ("icims.com", "icims"), ("taleo.net", "taleo"), ("successfactors.com", "successfactors"),
                  ("ultipro.com", "ukg"), ("ukg.net", "ukg"), ("paylocity.com", "paylocity"),
                  ("jobvite.com", "jobvite"), ("bamboohr.com", "bamboohr"), ("oraclecloud.com", "oracle"))
_NOT_TOKENS = {"embed", "jobs", "j", "api", "v1", "careers", "apply", "job-board", "posting-api"}


def parse_board_url(url: str) -> tuple[str, str] | None:
    """(ats, board token) for a link to a board or a posting on one of the four allowed ATSs; None otherwise."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = parts.netloc.lower()
    segments = [s for s in parts.path.split("/") if s]
    for ats, hosts in BOARD_HOSTS.items():
        if host in hosts:
            if ats == "greenhouse" and segments[:1] == ["embed"]:
                token = (parse_qs(parts.query).get("for") or [""])[0]
                return (ats, token) if token else None
            if segments and segments[0].lower() not in _NOT_TOKENS:
                return ats, segments[0]
    return None


def board_url(ats: str, token: str) -> str:
    return f"https://{BOARD_HOSTS[ats][-1]}/{token}" if ats in BOARD_HOSTS else token


def manual_system(url: str) -> str | None:
    """The name of an excluded application system a link points to (workday, smartrecruiters, ...), or None."""
    try:
        host = urlsplit((url or "").strip()).netloc.lower()
    except ValueError:
        return None
    return next((name for marker, name in MANUAL_SYSTEMS + private_sites.manual_systems() if host == marker or host.endswith("." + marker)), None)
