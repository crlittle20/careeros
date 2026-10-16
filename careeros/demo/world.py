"""The synthetic world Demo Mode runs in (2026-10-13). Every organisation, website, job board and posting here is
FICTIONAL: names are invented, websites live under example.com (a domain reserved for documentation), and nothing here
describes a real person or company. Job boards use the real providers' public API addresses only so that CareerOS's
production code paths are exercised; the demo's network transport answers them from this module and never sends a
request anywhere.

Four simulated days show the lifecycle:
- day 1: legacy boards are scanned, an alert email and a pasted posting are imported, curated employers are discovered,
  every board is checked against its employer's website, new boards enter quarantine, jobs are judged and routed;
- day 2: a pay change and a cosmetic edit, a posting disappears (first miss), Harborline Health's Greenhouse board is
  gone and its website now links an Ashby board (an ATS migration), quarantined boards pass their validation scan;
- day 3: the vanished posting closes after its second miss, newly admitted boards are scanned, the migrated board is
  validated;
- day 4: the migrated board is scanned (its jobs are linked to the old ones as possible reposts).
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

START = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)   # day 1, 13:00 UTC
DAYS = 4


def day_start(day: int) -> str:
    return (START + timedelta(days=day - 1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def site(slug: str) -> str:
    return f"{slug}.example.com"


# --- posting text ----------------------------------------------------------------------------------------------------
NATIONWIDE = "This position is remote and open to candidates in all 50 states."
REMOTE_US = "This role is remote in the US."


def body(about: str, duties: list[str], reqs: list[str], where: str, pay: str | None = None, extra: str = "") -> str:
    parts = [f"About us\n{about}", "Responsibilities:\n" + "\n".join(f"- {d}" for d in duties),
             "Requirements:\n" + "\n".join(f"- {r}" for r in reqs), where]
    if pay:
        parts.append(pay)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


CLAIMS_REQS = ["3+ years of claims, PBM or payer operations experience required", "Advanced Excel skills required",
               "SQL experience preferred"]
BSA_REQS = ["3+ years of business systems analysis in healthcare required", "Requirements gathering and user acceptance testing",
            "SQL experience required", "Jira or similar tracking tools"]
# Experience Match demo (2026-10-15): 8 years asked, the persona has 6 in business systems analysis and 5 in healthcare,
# a medium gap (12 of 20) whose future effect would cap the job at Tier B. Shadow mode: its tier does not change.
BSA_REQS_SENIOR = ["8+ years of business systems analysis in healthcare required", *BSA_REQS[1:],
                   # Certifications demo (2026-10-15): a family the persona holds, a planned credential, one not held
                   "Six Sigma certification required", "PL-300 certification preferred", "PMP a plus"]
DATA_REQS = ["3+ years of healthcare data analysis required", "SQL experience required", "Power BI or Tableau experience required"]


def job(jid, title, location, text, req=None, posted="2026-10-28T09:00:00Z"):
    return {"id": jid, "title": title, "location": location, "text": text, "req": req or f"REQ-{jid}", "posted": posted}


# --- employers --------------------------------------------------------------------------------------------------------
# kind: legacy = a board CareerOS already scanned before verification existed; curated = a suggestion with a website
# and (sometimes) a board link; lead = only seen through an alert email; paste = only seen as pasted text.
EMPLOYERS = [
    {"name": "Meridian Rx", "slug": "meridianrx", "kind": "legacy", "ats": "greenhouse", "token": "meridianrx",
     "story": "PBM. Its website embeds its Greenhouse board: verified; a duplicate, a director role, a poor fit, and a "
              "posting that disappears and closes."},
    {"name": "Bluepeak Health Plan", "slug": "bluepeakhealth", "kind": "legacy", "ats": "lever", "token": "bluepeakhealth",
     "story": "Health plan on Lever: verified; a pay change, a cosmetic edit, hybrid and on-site roles, a state-restricted role."},
    {"name": "Harborline Health", "slug": "harborline", "kind": "legacy", "ats": "greenhouse", "token": "harborline",
     "story": "Moves from Greenhouse to Ashby on day 2: an ATS migration (old board kept as history, new board verified)."},
    {"name": "Cascade Health Data", "slug": "cascadehealthdata", "kind": "legacy", "ats": "lever", "token": "cascadehealthdata",
     "story": "Healthcare data company on Lever: reporting and data roles, a manager role, a poor fit."},
    {"name": "Sterling Claims Services", "slug": "sterlingclaims", "kind": "legacy", "ats": "workable", "token": "sterlingclaims",
     "story": "Workable board with overnight claims work: NIGHT routing."},
    {"name": "Evergreen Health Analytics", "slug": "evergreenhealth", "kind": "legacy", "ats": "greenhouse", "token": "evergreenhealth",
     "story": "A legitimate board with no open jobs: verified, empty, never treated as fake."},
    {"name": "Northwind Benefits", "slug": "northwindbenefits", "kind": "curated", "ats": "ashby", "token": "northwindbenefits",
     "story": "New employer (benefits administration): verified at its own site, quarantined, validated, then scanned."},
    {"name": "Cobalt Claims Technology", "slug": "cobaltclaims", "kind": "curated", "ats": "workable", "token": "cobaltclaims",
     "story": "New employer on Workable: automation and payment-integrity roles."},
    {"name": "Summit Payer Services", "slug": "summitpayer", "kind": "curated", "ats": None,
     "story": "Its careers site uses Workday: manual only, never read automatically."},
    {"name": "Lakeview Pharmacy Solutions", "slug": "lakeviewrx", "kind": "curated", "ats": None,
     "story": "Its careers site uses iCIMS: manual only."},
    {"name": "Quillon Health", "slug": "quillonhealth", "kind": "curated", "ats": "greenhouse", "token": "quillon",
     "story": "A board named like it, linked from its site, actually belongs to Quillon Logistics: identity mismatch, rejected."},
    {"name": "Pinecrest Care Navigation", "slug": "pinecrestcare", "kind": "curated", "ats": "ashby", "token": "pinecrest",
     "story": "Its careers page loads jobs with a script and shows no board link: sent to manual review."},
    {"name": "Atlas Claims Group", "slug": "atlasclaims", "kind": "curated", "ats": "lever", "token": "atlasclaims",
     "story": "The suggested board does not exist: an invalid source, nothing admitted."},
    {"name": "Ridgeway Staffing", "slug": "ridgewaystaffing", "kind": "lead", "ats": None,
     "story": "A staffing agency seen in an alert email: its undisclosed client is never guessed."},
    {"name": "Brightwater Benefits", "slug": "brightwater", "kind": "paste", "ats": None,
     "story": "A pasted posting with scam signals (chat app interview, check deposit): rejected."},
]


def _postings(token: str, day: int) -> list[dict] | None:
    """The postings a board lists on a given day; None = the board does not exist (404)."""
    about = {
        "meridianrx": "Meridian Rx is a pharmacy benefit manager serving employer health plans. Learn more at meridianrx.example.com.",
        "bluepeakhealth": "Bluepeak Health Plan is a regional health plan. Learn more at bluepeakhealth.example.com.",
        "harborline": "Harborline Health builds claims and benefits software for payers. Learn more at harborline.example.com.",
        "harborlinehealth": "Harborline Health builds claims and benefits software for payers. Learn more at harborline.example.com.",
        "cascadehealthdata": "Cascade Health Data turns claims data into reporting for health plans. Learn more at cascadehealthdata.example.com.",
        "sterlingclaims": "Sterling Claims Services processes medical and pharmacy claims for payers. Learn more at sterlingclaims.example.com.",
        "northwindbenefits": "Northwind Benefits administers employee benefits for mid-size employers. Learn more at northwindbenefits.example.com.",
        "cobaltclaims": "Cobalt Claims Technology automates claims operations for payers. Learn more at cobaltclaims.example.com.",
        "quillon": "Quillon Logistics moves freight across North America. Learn more at quillonlogistics.example.com.",
        "pinecrest": "Pinecrest Care Navigation helps members find care. Learn more at pinecrestcare.example.com.",
    }.get(token, "")
    if token == "meridianrx":
        jobs = [job(101, "PBM Operations Analyst", "Remote (United States)",
                    body(about, ["Reconcile pharmacy claims adjudication outcomes in RxClaim", "Investigate pricing and eligibility discrepancies"],
                         CLAIMS_REQS, NATIONWIDE, "Pay range: $78,000 - $92,000 per year"), req="PBM-101"),
                job(102, "PBM Operations Analyst", "Remote (United States)",
                    body(about, ["Reconcile pharmacy claims adjudication outcomes in RxClaim", "Investigate pricing and eligibility discrepancies"],
                         CLAIMS_REQS, NATIONWIDE, "Pay range: $78,000 - $92,000 per year"), req="PBM-101"),   # duplicate of 101
                job(104, "Benefit Configuration Analyst", "Remote - US",
                    body(about, ["Configure pharmacy benefit plans and test them before go-live", "Write requirements for plan changes"],
                         BSA_REQS, REMOTE_US, "Pay range: $38 - $44 per hour")),
                job(105, "Director of Pharmacy Operations", "Remote - US",
                    body(about, ["Lead the pharmacy operations department", "Own the operations budget"],
                         ["10+ years of PBM leadership experience required", "People management of 30+ staff"], REMOTE_US,
                         "Pay range: $180,000 - $220,000 per year")),
                job(106, "Senior Software Engineer", "Remote - US",
                    body(about, ["Build claims processing services in Java"], ["7+ years of software engineering required"], REMOTE_US)),
                job(107, "PBM Client Reporting Analyst", "Remote - US",
                    body(about, ["Build monthly rebate and utilization reports for clients", "Validate claims data used in reports"],
                         DATA_REQS, REMOTE_US)),  # no pay posted: a decisive unknown
                job(109, "Rebate Operations Analyst", "Remote - US",
                    body(about, ["Reconcile manufacturer rebate invoices", "Track rebate disputes"], CLAIMS_REQS, REMOTE_US,
                         "Pay range: $54,000 - $60,000 per year"))]
        if day >= 3:  # a new posting appears mid-demo
            jobs.append(job(108, "Pharmacy Claims Analyst", "Remote (United States)",
                            body(about, ["Research rejected and reversed pharmacy claims", "Trend claim rejections for clients"],
                                 CLAIMS_REQS, NATIONWIDE, "Pay range: $76,000 - $88,000 per year"), posted="2026-11-03T15:00:00Z"))
        if day == 1:  # gone on day 2 and 3: closed after two complete scans without it
            jobs.append(job(103, "Claims Operations Specialist", "Remote - CO, TX, AZ",
                            body(about, ["Resolve rejected pharmacy claims", "Work the claims exception queue"], CLAIMS_REQS,
                                 "This remote role is open to residents of CO, TX and AZ.", "Pay range: $70,000 - $80,000 per year")))
        return jobs
    if token == "bluepeakhealth":
        pay = "Pay range: $72,000 - $84,000 per year" if day == 1 else "Pay range: $76,000 - $88,000 per year"  # a real change
        cosmetic = "" if day == 1 else " Our team values curiosity and care."  # wording only: nothing that matters changes
        jobs = [job("a1f0c3d2-0000-4000-8000-000000000201", "Claims Analyst II", "Remote - US",
                    body(about, ["Audit medical and pharmacy claims for payment accuracy", "Report claims trends to operations leaders"],
                         CLAIMS_REQS, REMOTE_US, pay)),
                job("a1f0c3d2-0000-4000-8000-000000000202", "Payment Integrity Analyst", "Remote - US",
                    body(about, ["Find overpayments and duplicate claims", "Recover payments with providers"], CLAIMS_REQS,
                         REMOTE_US + cosmetic, "Pay range: $75,000 - $90,000 per year")),
                job("a1f0c3d2-0000-4000-8000-000000000203", "Healthcare Business Systems Analyst", "Hybrid - Denver, CO",
                    body(about, ["Gather requirements for the claims platform", "Lead user acceptance testing"], BSA_REQS,
                         "Hybrid: three days a week in our Denver office.", "Pay range: $85,000 - $100,000 per year")),
                job("a1f0c3d2-0000-4000-8000-000000000204", "Reporting Analyst", "Chicago, IL",
                    body(about, ["Build operational dashboards"], DATA_REQS, "This role is on-site in our Chicago office.",
                         "Pay range: $70,000 - $82,000 per year")),
                job("a1f0c3d2-0000-4000-8000-000000000205", "Claims Operations Specialist", "Remote - NY, NJ",
                    body(about, ["Resolve pended claims"], CLAIMS_REQS, "This remote role is open to residents of NY and NJ only.",
                         "Pay range: $72,000 - $80,000 per year")),
                job("a1f0c3d2-0000-4000-8000-000000000206", "Pharmacy Benefit Analyst", "Remote - US",
                    body(about, ["Analyse pharmacy benefit designs", "Test formulary and plan changes"], BSA_REQS_SENIOR, REMOTE_US,
                         "Pay range: $80,000 - $95,000 per year"))]
        if day >= 2:
            jobs.append(job("a1f0c3d2-0000-4000-8000-000000000207", "Benefits Configuration Analyst", "Remote - US",
                            body(about, ["Configure medical benefit plans in the claims system", "Test benefit changes before renewal"],
                                 BSA_REQS, REMOTE_US, "Pay range: $80,000 - $92,000 per year"), posted="2026-11-02T20:00:00Z"))
        return jobs
    if token == "harborline":
        if day >= 2:
            return None  # the employer moved to Ashby and closed this board
        return [job(301, "Implementation Analyst", "Remote - US",
                    body(about, ["Implement claims software for new payer clients", "Configure client workflows"], BSA_REQS, REMOTE_US,
                         "Pay range: $80,000 - $96,000 per year"), req="HL-301"),
                job(302, "Claims Configuration Analyst", "Remote - US",
                    body(about, ["Configure benefit and pricing rules", "Test claims configuration changes"], BSA_REQS, REMOTE_US,
                         "Pay range: $78,000 - $90,000 per year"), req="HL-302"),
                job(303, "Utilization Review Nurse", "Remote - US",
                    body(about, ["Review medical necessity of requests"], ["Active RN license required", "3+ years of clinical experience"],
                         REMOTE_US, "Pay range: $85,000 - $95,000 per year"), req="HL-303")]
    if token == "harborlinehealth":  # the new Ashby board (day 2 onward)
        return [job("b2e1d4c3-0000-4000-8000-000000000311", "Implementation Analyst", "Remote - US",
                    body(about, ["Implement claims software for new payer clients", "Configure client workflows"], BSA_REQS, REMOTE_US,
                         "Pay range: $80,000 - $96,000 per year"), req="HL-301"),
                job("b2e1d4c3-0000-4000-8000-000000000312", "Claims Configuration Analyst", "Remote - US",
                    body(about, ["Configure benefit and pricing rules", "Test claims configuration changes"], BSA_REQS, REMOTE_US,
                         "Pay range: $78,000 - $90,000 per year"), req="HL-302"),
                job("b2e1d4c3-0000-4000-8000-000000000313", "Implementation Consultant", "Remote - US",
                    body(about, ["Lead payer onboarding projects", "Map client processes to the product"], BSA_REQS, REMOTE_US,
                         "Pay range: $90,000 - $105,000 per year"), req="HL-313"),
                job("b2e1d4c3-0000-4000-8000-000000000314", "Benefits Configuration Lead", "Remote - US",
                    body(about, ["Lead benefit configuration for payer clients", "Review configuration test results"], BSA_REQS, REMOTE_US,
                         "Pay range: $95,000 - $110,000 per year"), req="HL-314")]
    if token == "cascadehealthdata":
        return [job("c3f2e5d4-0000-4000-8000-000000000401", "Healthcare Data Analyst", "Remote - US",
                    body(about, ["Analyse medical and pharmacy claims data", "Build Power BI reports for health plan clients"], DATA_REQS,
                         NATIONWIDE, "Pay range: $82,000 - $98,000 per year")),
                job("c3f2e5d4-0000-4000-8000-000000000402", "Data Reporting Analyst", "Remote - US",
                    body(about, ["Automate monthly client reporting", "Validate data quality in claims extracts"], DATA_REQS, REMOTE_US,
                         "Pay range: $74,000 - $86,000 per year")),
                job("c3f2e5d4-0000-4000-8000-000000000403", "Process Improvement Analyst", "Remote - US",
                    body(about, ["Map claims operations processes", "Measure and remove rework"], CLAIMS_REQS, REMOTE_US,
                         "Pay range: $76,000 - $88,000 per year")),
                job("c3f2e5d4-0000-4000-8000-000000000404", "Claims Operations Manager", "Remote - US",
                    body(about, ["Manage a team of 12 claims analysts", "Own operations KPIs"],
                         ["5+ years of people management required", "Claims operations experience required"], REMOTE_US,
                         "Pay range: $110,000 - $130,000 per year")),
                job("c3f2e5d4-0000-4000-8000-000000000405", "Data Engineer", "Remote - US",
                    body(about, ["Build data pipelines in Python and Spark"], ["5+ years of data engineering required"], REMOTE_US)),
                job("c3f2e5d4-0000-4000-8000-000000000406", "Senior Reporting Analyst", "Remote (Anywhere in the US)",
                    body(about, ["Own the claims reporting suite for health plan clients", "Mentor junior analysts"], DATA_REQS, NATIONWIDE,
                         "Pay range: $88,000 - $102,000 per year")),
                job("c3f2e5d4-0000-4000-8000-000000000407", "Claims Data Analyst", "Remote - US",
                    body(about, ["Analyse claims payment trends", "Answer ad hoc data requests from operations"], DATA_REQS,
                         "This role is remote in the US. You must work 8am to 5pm Eastern Time.", "Pay range: $76,000 - $88,000 per year"))]
    if token == "sterlingclaims":
        return [job("SC4001", "Overnight Claims Quality Auditor", "Remote - US",
                    body(about, ["Audit processed pharmacy claims for accuracy and document findings", "Report claims errors to the team lead"],
                         ["2+ years of claims experience required", "Excel skills required"], "This role is remote. Back office work, no phone queue.",
                         "Pay range: $26 - $30 per hour", "Schedule: overnight shift, Sunday to Thursday, 11pm to 7:30am.")),
                job("SC4002", "Claims Operations Associate", "Remote - US",
                    body(about, ["Process medical claims", "Resolve claim edits"], ["1+ year of claims experience required", "Excel skills required"],
                         REMOTE_US, "Pay range: $52,000 - $58,000 per year")),
                job("SC4003", "Payment Integrity Specialist", "Remote - US",
                    body(about, ["Review claims for coordination of benefits issues", "Document recoveries"], CLAIMS_REQS, REMOTE_US,
                         "Pay range: $72,000 - $84,000 per year")),
                job("SC4004", "Senior Claims Analyst", "Remote - US",
                    body(about, ["Adjudicate complex medical claims", "Train examiners on claim edits"], CLAIMS_REQS, REMOTE_US,
                         "Pay range: $36 - $40 per hour"))]
    if token == "evergreenhealth":
        return []  # a real, empty board
    if token == "northwindbenefits":
        return [job("d4a3f6e5-0000-4000-8000-000000000501", "Benefits Operations Analyst", "Remote - US",
                    body(about, ["Reconcile benefit enrollment files with carriers", "Resolve eligibility discrepancies"], CLAIMS_REQS,
                         REMOTE_US, "Pay range: $70,000 - $82,000 per year")),
                job("d4a3f6e5-0000-4000-8000-000000000502", "Benefits Implementation Analyst", "Remote - US",
                    body(about, ["Implement benefit plans for new employer clients", "Build eligibility file feeds"], BSA_REQS, REMOTE_US,
                         "Pay range: $78,000 - $92,000 per year")),
                job("d4a3f6e5-0000-4000-8000-000000000503", "Eligibility Configuration Specialist", "Remote - US",
                    body(about, ["Configure eligibility rules", "Test enrollment changes"], BSA_REQS, REMOTE_US,
                         "Pay range: $72,000 - $84,000 per year")),
                job("d4a3f6e5-0000-4000-8000-000000000504", "Account Executive", "Remote - US",
                    body(about, ["Sell benefits administration to employers"], ["5+ years of B2B sales required"], REMOTE_US)),
                job("d4a3f6e5-0000-4000-8000-000000000505", "Benefits Reporting Analyst", "Remote - US",
                    body(about, ["Report enrollment and premium trends to clients", "Validate carrier feed data"], DATA_REQS, REMOTE_US,
                         "Pay range: $74,000 - $86,000 per year")),
                job("d4a3f6e5-0000-4000-8000-000000000506", "Office Coordinator", "Remote - US",
                    body(about, ["Order supplies and plan team events"], ["Organised and friendly"], REMOTE_US))]
    if token == "cobaltclaims":
        return [job("CC6001", "Automation Analyst", "Remote - US",
                    body(about, ["Automate claims workflows with Power Automate", "Build Power Apps for claims teams"],
                         ["3+ years of process automation experience required", "Power Automate experience required", "SQL experience preferred"],
                         REMOTE_US, "Pay range: $80,000 - $95,000 per year")),
                job("CC6002", "Payment Integrity Analyst", "Remote - US",
                    body(about, ["Model overpayment patterns in claims data", "Work recovery cases with providers"], CLAIMS_REQS, NATIONWIDE,
                         "Pay range: $78,000 - $92,000 per year")),
                job("CC6003", "QA Automation Engineer", "Remote - US",
                    body(about, ["Write automated tests for the claims platform"], ["3+ years of software QA required"], REMOTE_US)),
                job("CC6004", "Claims Automation Specialist", "Remote - US",
                    body(about, ["Build RPA bots for claims intake", "Document automated claims workflows"],
                         ["3+ years of claims or automation experience required", "UiPath or Power Automate experience required"],
                         REMOTE_US, "Pay range: $82,000 - $96,000 per year"))]
    if token == "quillon":
        return [job(901, "Freight Claims Coordinator", "Dallas, TX",
                    body(about, ["Process freight damage claims for shipments"], ["2+ years of logistics experience"], "On-site in Dallas.")),
                job(902, "Dispatch Analyst", "Dallas, TX", body(about, ["Plan truck routes"], ["Logistics experience"], "On-site in Dallas.")),
                job(903, "Warehouse Supervisor", "Dallas, TX", body(about, ["Run the night shift"], ["Supervisory experience"], "On-site."))]
    if token == "pinecrest":
        return [job("e5b4a7f6-0000-4000-8000-000000000701", "Care Navigation Specialist", "Remote - US",
                    body(about, ["Help members find in-network care"], ["Customer service experience"], REMOTE_US)),
                job("e5b4a7f6-0000-4000-8000-000000000702", "Claims Analyst", "Remote - US",
                    body(about, ["Resolve member claim questions"], CLAIMS_REQS, REMOTE_US, "Pay range: $70,000 - $80,000 per year"))]
    return None  # e.g. atlasclaims: no such board


BOARD_NAMES = {"meridianrx": "Meridian Rx", "harborline": "Harborline Health", "evergreenhealth": "Evergreen Health Analytics",
               "quillon": "Quillon Logistics"}   # Greenhouse board names (the identity check reads them)
BOARD_INTROS = {"evergreenhealth": "<p>Evergreen Health Analytics careers. See evergreenhealth.example.com for who we are.</p>"}
WORKABLE_NAMES = {"sterlingclaims": "Sterling Claims Services", "cobaltclaims": "Cobalt Claims Technology"}


def _careers(slug: str, day: int) -> str:
    """The careers page each fictional employer publishes."""
    if slug == "meridianrx":
        return ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=meridianrx"></script>'
                '<a href="https://job-boards.greenhouse.io/meridianrx/jobs/101">PBM Operations Analyst</a>')
    if slug == "bluepeakhealth":
        return ('<a href="https://jobs.lever.co/bluepeakhealth">Open roles</a>'
                '<a href="https://jobs.lever.co/bluepeakhealth/a1f0c3d2-0000-4000-8000-000000000201">Claims Analyst II</a>')
    if slug == "harborline":
        if day == 1:
            return ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=harborline"></script>'
                    '<a href="https://job-boards.greenhouse.io/harborline/jobs/301">Implementation Analyst</a>')
        return ('<a href="https://jobs.ashbyhq.com/harborlinehealth">Open roles</a>'
                '<a href="https://jobs.ashbyhq.com/harborlinehealth/b2e1d4c3-0000-4000-8000-000000000311">Implementation Analyst</a>')
    if slug == "cascadehealthdata":
        return ('<a href="https://jobs.lever.co/cascadehealthdata">Join us</a>'
                '<a href="https://jobs.lever.co/cascadehealthdata/c3f2e5d4-0000-4000-8000-000000000401">Healthcare Data Analyst</a>')
    if slug == "sterlingclaims":
        return ('<a href="https://apply.workable.com/sterlingclaims/">Open positions</a>'
                '<a href="https://apply.workable.com/sterlingclaims/j/SC4001/">Overnight Claims Quality Auditor</a>')
    if slug == "evergreenhealth":
        return ('<iframe src="https://boards.greenhouse.io/embed/job_board?for=evergreenhealth"></iframe>'
                "<p>We have no openings right now.</p>")
    if slug == "northwindbenefits":
        return ('<a href="https://jobs.ashbyhq.com/northwindbenefits">See open roles</a>'
                '<a href="https://jobs.ashbyhq.com/northwindbenefits/d4a3f6e5-0000-4000-8000-000000000501">Benefits Operations Analyst</a>')
    if slug == "cobaltclaims":
        return ('<a href="https://apply.workable.com/cobaltclaims/">Careers</a>'
                '<a href="https://apply.workable.com/cobaltclaims/j/CC6001/">Automation Analyst</a>')
    if slug == "summitpayer":
        return '<a href="https://summitpayer.wd5.myworkdayjobs.com/External">Search jobs</a>'
    if slug == "lakeviewrx":
        return '<a href="https://careers-lakeviewrx.icims.com/jobs">View openings</a>'
    if slug == "quillonhealth":
        return '<script src="https://boards.greenhouse.io/embed/job_board/js?for=quillon"></script>'
    if slug == "pinecrestcare":
        return "<div id='jobs'></div><script src='/static/careers.js'></script><p>Our openings load here.</p>"
    return "<p>We are not hiring right now.</p>"


def _homepage(name: str) -> str:
    return (f"<!doctype html><html><head><title>{html.escape(name)} | Fictional demo company</title></head><body>"
            f'<nav><a href="/careers">Careers</a></nav><p>{html.escape(name)} is a fictional company used in the CareerOS demo.</p>'
            "</body></html>")


# --- provider response shapes ---------------------------------------------------------------------------------------------
def _as_html(text: str) -> str:
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            parts.append(f"<ul><li>{html.escape(line[2:])}</li></ul>")
        elif line:
            parts.append(f"<p>{html.escape(line)}</p>")
    return "".join(parts)


def greenhouse_item(token: str, j: dict) -> dict:
    return {"id": j["id"], "title": j["title"], "location": {"name": j["location"]}, "requisition_id": j["req"],
            "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}", "content": html.escape(_as_html(j["text"])),
            "updated_at": j["posted"], "first_published": j["posted"]}


def lever_item(token: str, j: dict) -> dict:
    workplace = "hybrid" if j["location"].startswith("Hybrid") else "onsite" if "Remote" not in j["location"] else "remote"
    return {"id": j["id"], "text": j["title"], "categories": {"location": j["location"], "commitment": "Full-time"},
            "hostedUrl": f"https://jobs.lever.co/{token}/{j['id']}", "applyUrl": f"https://jobs.lever.co/{token}/{j['id']}/apply",
            "descriptionPlain": j["text"], "createdAt": int(datetime.fromisoformat(j["posted"].replace("Z", "+00:00")).timestamp() * 1000),
            "workplaceType": workplace}


def ashby_item(token: str, j: dict) -> dict:
    return {"id": j["id"], "title": j["title"], "location": j["location"], "isRemote": "Remote" in j["location"],
            "jobUrl": f"https://jobs.ashbyhq.com/{token}/{j['id']}", "applyUrl": f"https://jobs.ashbyhq.com/{token}/{j['id']}/application",
            "descriptionPlain": j["text"], "publishedAt": j["posted"], "isListed": True, "employmentType": "FullTime"}


def workable_item(token: str, j: dict) -> dict:
    return {"shortcode": j["id"], "title": j["title"], "description": _as_html(j["text"]), "url": f"https://apply.workable.com/{token}/j/{j['id']}/",
            "application_url": f"https://apply.workable.com/{token}/j/{j['id']}/apply/", "telecommuting": "Remote" in j["location"],
            "locations": [{"country": "United States"}], "published_on": j["posted"][:10], "code": j["req"], "employment_type": "Full-time"}


class DemoWorld:
    """Answers the demo's network traffic for one simulated day."""

    def __init__(self, day: int = 1):
        self.day = day
        self.requests: list[str] = []
        self.domains = {site(e["slug"]): e for e in EMPLOYERS}

    def knows_host(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.domains)

    # job-board listing APIs
    def ats_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        host, parts = request.url.host, [p for p in request.url.path.split("/") if p]
        if host == "boards-api.greenhouse.io" and len(parts) >= 3:
            token, postings = parts[2], _postings(parts[2], self.day)
            if postings is None:
                return httpx.Response(404, json={"status": 404})
            if len(parts) == 3:
                return httpx.Response(200, json={"name": BOARD_NAMES.get(token, token), "content": BOARD_INTROS.get(token, "")})
            items = [greenhouse_item(token, j) for j in postings]
            if len(parts) == 5:
                found = [i for i in items if str(i["id"]) == parts[4]]
                return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={})
            return httpx.Response(200, json={"jobs": items})
        if host == "api.lever.co" and len(parts) >= 3:
            postings = _postings(parts[2], self.day)
            if postings is None:
                return httpx.Response(404, json=[])
            items = [lever_item(parts[2], j) for j in postings]
            if len(parts) == 4:
                found = [i for i in items if i["id"] == parts[3]]
                return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={})
            return httpx.Response(200, json=items)
        if host == "api.ashbyhq.com" and len(parts) >= 3:
            postings = _postings(parts[2], self.day)
            return httpx.Response(404, json={}) if postings is None else httpx.Response(200, json={"jobs": [ashby_item(parts[2], j) for j in postings]})
        if host == "apply.workable.com" and len(parts) >= 5 and parts[:4] == ["api", "v1", "widget", "accounts"]:
            postings = _postings(parts[4], self.day)
            if postings is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"name": WORKABLE_NAMES.get(parts[4], parts[4]), "description": "",
                                             "jobs": [workable_item(parts[4], j) for j in postings]})
        return httpx.Response(404, json={})

    # fictional employer websites (example.com subdomains)
    def web_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        host, path = request.url.host, request.url.path or "/"
        employer = next((e for d, e in self.domains.items() if host == d or host.endswith("." + d)), None)
        if employer is None or employer["kind"] in ("lead", "paste") or path == "/robots.txt":
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")
        if path == "/":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=_homepage(employer["name"]).encode())
        if path == "/careers":
            page = (f"<!doctype html><html><head><title>Careers at {html.escape(employer['name'])}</title></head><body>"
                    f"{_careers(employer['slug'], self.day)}</body></html>")
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=page.encode())
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")


# --- vault files the demo starts from ---------------------------------------------------------------------------------------
PROFILE = """# DEMO persona: fictional. Not a real person and not your profile.
status: approved
profile_version: 0
state: CO
work_authorization: {us_authorized: true, needs_sponsorship: false}
clearance: none
current_level: senior
total_professional_years: 8
education:
  - {level: bachelor, field: information systems, completed: true}
certifications:   # synthetic: the demo persona is fictional, and so are its credentials
  - {name: Professional Scrum Master I, status: held}
  - {name: Lean Six Sigma Yellow Belt, status: held}
  - {name: PL-300, status: planned}
licenses: []
functions:
  - {name: business systems analysis, years: 6, evidence: direct}
  - {name: requirements gathering, years: 6, evidence: direct}
  - {name: user acceptance testing, years: 5, evidence: direct}
  - {name: data validation, years: 5, evidence: direct}
  - {name: claims operations, years: 4, evidence: direct}
skills:
  - {name: sql, years: 4, evidence: direct}
  - {name: excel, years: 8, evidence: direct}
  - {name: power bi, years: 2, evidence: strongly_transferable}
  - {name: power automate, years: 2, evidence: direct}
  - {name: rxclaim, years: 3, evidence: direct}
  - {name: jira, years: 4, evidence: direct}
  - {name: uat, years: 5, evidence: direct}
  - {name: requirements gathering, years: 6, evidence: direct}
domains:
  - {name: healthcare, years: 5}
  - {name: pbm, years: 3}
  - {name: claims, years: 4}
familiar_employers: []
"""

OVERRIDES = """# DEMO policy: fictional values for the synthetic persona. Never your real settings.
location: {home_state: CO, remote_only: true, commute_cities: []}
compensation: {floor_annual: 70000, floor_hourly: 34, target_annual: 85000, target_hourly: 41}
gate: {excluded_employers: [Grayline Collections], held_parents: []}
extraction: {ai: {enabled: false}}
operations: {notifications: false, publish_seen_index: false}
night:
  enabled: true
  modes:
    supplemental: {compensation: {floor_hourly: 25, target_hourly: 28}}
    replacement: {compensation: {floor_annual: 70000, target_annual: null}}
board_verification: {fetch_official_sites: true, daily_site_requests: 300, max_verifications_per_run: 20,
                     max_reverifications_per_run: 20, max_validations_per_run: 20, ats_request_budget: 200}
source_discovery: {enabled: true, auto_activate: true, daily_request_budget: 200, per_provider_budget: 100, curated_share: 1.0}
scanning: {adaptive: false}
"""

CURATED = {"employers": [
    {"name": e["name"], "domain": site(e["slug"]),
     **({"board": {"greenhouse": f"https://job-boards.greenhouse.io/{e['token']}", "lever": f"https://jobs.lever.co/{e['token']}",
                   "ashby": f"https://jobs.ashbyhq.com/{e['token']}", "workable": f"https://apply.workable.com/{e['token']}/"}[e["ats"]]}
        if e["ats"] else {"board": {"summitpayer": "https://summitpayer.wd5.myworkdayjobs.com/External",
                                    "lakeviewrx": "https://careers-lakeviewrx.icims.com/jobs"}[e["slug"]]}),
     "note": "fictional demo employer"}
    for e in EMPLOYERS if e["kind"] == "curated"]}

ALERT_EMAIL = """From: Demo Job Alerts <alerts@example.com>
To: demo-persona@example.com
Subject: 3 new jobs for "claims analyst"
Date: Mon, 02 Nov 2026 12:30:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Claims Analyst
Ridgeway Staffing · Remote (US)
https://www.linkedin.com/jobs/view/4100000001

PBM Operations Analyst
Meridian Rx · Remote (United States)
https://job-boards.greenhouse.io/meridianrx/jobs/101

Payment Integrity Analyst
Bluepeak Health Plan · Remote - US
https://jobs.lever.co/bluepeakhealth/a1f0c3d2-0000-4000-8000-000000000202
"""

PASTED = """Title: Remote Claims Analyst
Employer: Brightwater Benefits
URL: https://brightwater.example.com/jobs/remote-claims-analyst
Location: Remote

About us
We are hiring remote claims analysts immediately for our benefits clients across the country.

Responsibilities:
- Enter and review claims data from home
- Report to the hiring manager after a short interview on Telegram

Requirements:
- 1+ year of claims or data entry experience required
- Excel skills required

This role is remote in the US. After the interview we will send a check deposit so you can buy your home office
equipment. Pay is $45 per hour. Reply today to secure your spot.
"""


def inbox_files() -> dict[str, str]:
    return {"alert-claims-analyst.eml": ALERT_EMAIL, "pasted-brightwater.txt": PASTED}


def curated_yaml() -> str:
    import yaml
    return "# DEMO curated employers: fictional.\n" + yaml.safe_dump(CURATED, sort_keys=False)


def legacy_boards() -> list[dict]:
    return [e for e in EMPLOYERS if e["kind"] == "legacy"]


def describe() -> str:
    return json.dumps([{k: e[k] for k in ("name", "kind", "story")} for e in EMPLOYERS], indent=1)


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


# --- what the fictional job seeker does (2026-10-13, Portfolio Prompt 7) --------------------------------------------
# Recorded after that day's run through the functions behind `careeros label`, `careeros applied` and
# `careeros app-status`, so the path from discovery to an application outcome has something to show.
BLUEPEAK_JOB = "lever:bluepeakhealth:a1f0c3d2-0000-4000-8000-000000000"
ACTIVITY = {
    3: [("label", BLUEPEAK_JOB + "207", "apply", "benefits configuration is a strong match"),
        ("label", "workable:sterlingclaims:SC4004", "apply", None),
        ("label", "lever:cascadehealthdata:c3f2e5d4-0000-4000-8000-000000000403", "stretch", "process work outside claims"),
        ("label", BLUEPEAK_JOB + "202", "skip", "payment recovery is not my focus"),
        ("apply", BLUEPEAK_JOB + "207", "direct"),
        ("apply", "workable:sterlingclaims:SC4004", "direct")],
    4: [("label", "greenhouse:meridianrx:108", "apply", None),     # both first listed on day 4 (Harborline's new board
        ("label", "ashby:harborlinehealth:b2e1d4c3-0000-4000-8000-000000000314", "apply", None),  # passes validation then)
        ("apply", "ashby:harborlinehealth:b2e1d4c3-0000-4000-8000-000000000314", "referral"),
        ("apply", "greenhouse:meridianrx:108", "direct"),
        ("status", BLUEPEAK_JOB + "207", "screening"),
        ("status", "workable:sterlingclaims:SC4004", "rejected")],
}
