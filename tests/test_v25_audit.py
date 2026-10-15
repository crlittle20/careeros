"""Prompts v2.5 / 2026-10-12 Phase 1 audit: the seen-jobs index and requisition fingerprints, "Principal" as a seniority
signal, weighted skill coverage, NIGHT workload and the two COI levels, structured Notes, Gmail outage recovery,
partial-failure handling, the completed outcome funnel, and the acceptance regression list (problem #13).
Synthetic data only: no real person, employer relationship or posting."""
import base64
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.cli import _inbox_pull, _record_alert
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery import github_inbox as inbox
from careeros.discovery import seen_index
from careeros.discovery.alerts import AlertEmail, Link
from careeros.discovery.note import context_note, is_alert_source, parse_note
from careeros.extract.rules import extract
from careeros.pipeline import Context, night_process
from careeros.qualify import night
from careeros.qualify.gate import evaluate, state_for
from careeros.qualify.profile import Profile
from careeros.qualify.scores import skill_coverage
from careeros.review.applications import application_matrix, record_application, source_of, update_application
from careeros.review.funnel import funnel
from careeros.review.labels import agreement, record_label

from tests.helpers import JOB_MATRIX, add_job
from tests.test_github_inbox import FakeGh, issue
from tests.test_label_findings import PROFILE
from tests.test_qualify import job
from tests.test_v21_policy import OVERNIGHT, policy_with

# A complete, fictional settings set: remote US only, FL, a $74,000 / $36 floor, one excluded employer, one held
# employer family (D13), near-miss 62, NIGHT on with a $29/hour Supplemental and a $74,000 Replacement floor.
APPROVED = policy_with(
    compensation={"floor_annual": 74000, "floor_hourly": 36, "target_annual": None, "target_hourly": None},
    location__remote_only=True, location__commute_cities=[], location__home_state="FL",
    gate__held_parents=["Northgate Group", "Northgate", "Bridgewell claims", "Northgatecare"],
    gate__excluded_employers=["Brambleton"], tiers__near_miss__priority=62,
    night__enabled=True,
    night__modes__supplemental__compensation={"floor_hourly": 29, "target_hourly": None},
    night__modes__replacement__compensation={"floor_annual": 74000, "target_annual": None})
WITH_DAY_JOB = policy_with(night__enabled=True, night__day_job={"start": "08:00", "end": "16:30", "days": ["mon", "tue", "wed", "thu", "fri"]},
                           night__modes__supplemental__compensation={"floor_hourly": 29, "target_hourly": None},
                           night__modes__replacement__compensation={"floor_annual": 74000, "target_annual": None})
# A synthetic mid-level persona with about four years and no completed bachelor's degree.
MID = Profile(data={**PROFILE.data, "current_level": "mid", "total_professional_years": 4,
                    "education": [{"level": "high_school", "completed": True}, {"level": "bachelor", "completed": False}]},
              fingerprint="mid")


def posting(required=(), preferred=(), duties=(), extra="This role is remote."):
    text = ("About us\nWe are a health plan operations team working on pharmacy claims for members across the country. "
            "Our analysts keep claims accurate, reconcile payments and report on quality to the business every week.\n\n")
    if duties:
        text += "Responsibilities:\n" + "".join(f"- {d}\n" for d in duties) + "\n"
    if required:
        text += "Requirements:\n" + "".join(f"- {r}\n" for r in required) + "\n"
    if preferred:
        text += "Preferred qualifications:\n" + "".join(f"- {p}\n" for p in preferred) + "\n"
    return text + extra + "\n"


def gate(title, text, policy=APPROVED, profile=MID, **kw):
    j = job(title, text, **kw)
    return evaluate(j, extract(j["description_text"], policy).requirements, profile, policy)


def failures(result):
    return {f["code"] for f in result.hard_failures}


def flags(result):
    return {f["code"] for f in result.flags}


def assess(title, body, policy=APPROVED, profile=MID, **kw):
    j = job(title, body, **kw)
    return night.assess(j, extract(body, policy).requirements, profile, policy, 60.0)


def store_ctx(policy=APPROVED, profile=MID):
    store = open_store(":memory:", FixedClock("2026-10-12T12:00:00.000Z"), policy.fingerprint)
    return store, Context(store, policy, profile, JOB_MATRIX, None)


def lead(store, lead_id, url, title, context, job_id=None, profile="primary"):
    store.writer("discovery").insert("leads", {
        "lead_id": lead_id, "url": url, "site": "other", "title": title, "title_norm": title.lower(),
        "first_seen_at": store.clock.now(), "status": "ingested" if job_id else "new", "job_id": job_id,
        "sender": "GitHub inbox (owner-x)", "profile": profile, "context": context})


# --- #3 Principal ------------------------------------------------------------------------------------------------
class PrincipalTests(unittest.TestCase):
    def test_principal_with_modest_individual_contributor_requirements_is_evaluated_normally(self):
        result = gate("Principal Operations Analyst", posting(
            required=["5+ years of claims experience required", "Excel skills required"],
            duties=["Analyze claims operations data and recommend fixes"]))
        self.assertNotIn("title_excluded", failures(result))
        self.assertNotIn("principal_strategic_scope", failures(result))
        self.assertNotIn("seniority_gap", failures(result))
        self.assertEqual(result.decision, "eligible")
        self.assertIn("years_slightly_short", flags(result))  # 5 required, 4 held: a penalty, not a rejection

    def test_principal_with_ten_years_and_strategic_architecture_ownership_is_ineligible(self):
        result = gate("Principal Business Systems Analyst", posting(
            required=["10+ years of claims experience required", "Excel skills required"],
            duties=["Own the architecture strategy and set the strategy for enterprise architecture across the company"]))
        self.assertEqual(state_for(result, APPROVED), "INELIGIBLE")
        self.assertTrue({"years_far_short", "principal_strategic_scope"} <= failures(result))
        self.assertNotIn("title_excluded", failures(result))

    def test_people_leadership_alone_rejects_a_principal_title_and_one_strategy_term_only_flags_it(self):
        lead_role = gate("Principal Business Systems Analyst", posting(required=["Excel skills required"],
                                                                        duties=["Manage a team of six analysts with direct reports"]))
        self.assertIn("principal_strategic_scope", failures(lead_role))
        one_term = gate("Principal Business Systems Analyst", posting(required=["Excel skills required"],
                                                                       duties=["Contribute to the enterprise architecture review"]))
        self.assertNotIn("principal_strategic_scope", failures(one_term))
        self.assertIn("principal_scope_check", flags(one_term))

    def test_director_vp_head_and_chief_titles_stay_automatic_rejections(self):
        for title in ("Director, Claims Operations", "AVP Claims Operations", "VP of Pharmacy Operations",
                      "SVP Business Systems", "Head of Claims Analytics", "Chief Operations Analyst"):
            self.assertIn("title_excluded", failures(gate(title, posting(required=["Excel skills required"]))), title)


# --- #4 Weighted skill coverage ------------------------------------------------------------------------------------
CLAIMS_PROFILE = Profile(data={**PROFILE.data, "skills": [
    {"name": "rxclaim", "years": 3, "evidence": "direct"}, {"name": "reconciliation", "years": 3, "evidence": "direct"},
    {"name": "excel", "years": 4, "evidence": "direct"}, {"name": "data validation", "years": 3, "evidence": "direct"}],
    "equivalences": []}, fingerprint="claims")
CLAIMS_POSTING = posting(required=[
    "3+ years of PBM claims experience required", "Hands-on RxClaim experience required", "Claims reconciliation experience required",
    "Advanced Excel required", "Source of truth validation for claims data", "Experience with Jira, Tableau, Power BI and Python",
    "Bachelor's degree required", "Must be authorized to work in the US", "Active pharmacy technician license required",
    "Public trust clearance required"])


class WeightedCoverageTests(unittest.TestCase):
    def coverage(self, title="Pharmacy Claims Analyst", text=CLAIMS_POSTING, profile=CLAIMS_PROFILE):
        j = job(title, text)
        return skill_coverage(j, extract(text, APPROVED).requirements, profile, APPROVED)

    def test_core_matches_are_not_sunk_by_missing_peripheral_tools(self):
        cov = self.coverage()
        items = {i["requirement"]: i for i in cov["items"]}
        self.assertGreaterEqual(cov["weighted_coverage"], 0.7)
        skills = [i for i in cov["items"] if i["category"] == "skill"]
        self.assertLess(sum(1 for i in skills if i["credit"]) / len(skills), 0.7)  # the old naive count would fail it
        self.assertEqual((items["rxclaim"]["importance"], items["rxclaim"]["evidence"]), ("core", "direct"))
        self.assertEqual((items["jira"]["importance"], items["jira"]["credit"]), ("supporting", 0.0))
        self.assertEqual(items["excel"]["importance"], "important")
        self.assertEqual(items["pbm"]["category"], "domain")
        for item in cov["items"]:  # auditable: every item keeps its weight, reason, evidence and source line
            self.assertTrue({"weight", "basis", "evidence", "credit", "source_text"} <= set(item))

    def test_a_missing_core_skill_costs_more_than_a_missing_tool(self):
        without_core = Profile(data={**CLAIMS_PROFILE.data, "skills": [s for s in CLAIMS_PROFILE.data["skills"] if s["name"] != "rxclaim"]},
                               fingerprint="x")
        with_tool = Profile(data={**CLAIMS_PROFILE.data, "skills": CLAIMS_PROFILE.data["skills"] + [
            {"name": "jira", "years": 1, "evidence": "direct"}]}, fingerprint="y")
        base = self.coverage()["weighted_coverage"]
        self.assertGreater(base - self.coverage(profile=without_core)["weighted_coverage"],
                           self.coverage(profile=with_tool)["weighted_coverage"] - base)

    def test_education_authorization_clearance_and_licences_are_never_skills(self):
        categories = {i["category"] for i in self.coverage()["items"]}
        self.assertEqual(categories, {"skill", "domain"})
        names = {i["requirement"] for i in self.coverage()["items"]}
        self.assertFalse(names & {"bachelor", "us_work_authorization", "security clearance"})

    def test_title_and_duties_promote_importance(self):
        text = posting(required=["Experience with Tableau and Jira required"], duties=["Maintain Jira boards for the claims team"])
        items = {i["requirement"]: i for i in self.coverage("Tableau Reporting Analyst", text)["items"]}
        self.assertEqual((items["tableau"]["importance"], items["tableau"]["basis"]), ("core", "named in the job title"))
        self.assertEqual(items["jira"]["importance"], "important")  # supporting + a stated duty

    def test_the_score_stores_the_weighted_coverage_and_uses_it_for_the_tier_gate(self):
        from careeros.qualify.scores import score
        j = job("Pharmacy Claims Analyst", CLAIMS_POSTING)
        reqs = extract(CLAIMS_POSTING, APPROVED).requirements
        card = score(j, reqs, CLAIMS_PROFILE, evaluate(j, reqs, CLAIMS_PROFILE, APPROVED), APPROVED, "2026-10-12T12:00:00.000Z")
        self.assertEqual(card.components["skill_coverage"], card.components["eligibility"]["coverage"]["weighted_coverage"])
        self.assertEqual(card.components["eligibility"]["coverage"]["method"], "weighted-v1")


# --- #5 Workload and #6 COI ------------------------------------------------------------------------------------------
class WorkloadTests(unittest.TestCase):
    def test_levels(self):
        level = lambda schedule, shift, policy=WITH_DAY_JOB: night.workload(schedule, shift, policy)["level"]
        self.assertEqual(level("overnight", (23 * 60, 7 * 60)), "severe")   # 8:00-4:30 day job + 11pm-7am
        self.assertEqual(level("overnight", (23 * 60, 3 * 60)), "caution")  # + 11pm-3am
        self.assertEqual(level("evening", (18 * 60, 20 * 60)), "green")
        self.assertEqual(level("weekend", None), "green")
        self.assertEqual(level("unknown", None), "unknown")
        self.assertEqual(level("overnight", (23 * 60, 7 * 60), APPROVED), "unknown")  # day-job hours not set

    def test_workload_is_informational_but_overlapping_hours_make_supplemental_incompatible(self):
        stacked = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT, policy=WITH_DAY_JOB, employment="part time")
        self.assertEqual((stacked["mode"], stacked["workload"]["level"]), ("supplemental", "severe"))
        self.assertNotIn("overlaps_day_job", {f["code"] for f in stacked["failures"]})
        self.assertEqual(stacked["decision"], "eligible")  # severe is information for you, not a rejection
        overlap = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am",
                                                                                 "evening shift, 1pm to 9pm"),
                         policy=WITH_DAY_JOB, employment="part time")
        self.assertIn("overlaps_day_job", {f["code"] for f in overlap["failures"]})
        self.assertIn("its hours overlap your day job", overlap["coi"]["reasons"])
        replacement = assess("Claims Quality Auditor", OVERNIGHT, policy=WITH_DAY_JOB, employment="full time")
        self.assertEqual((replacement["mode"], replacement["workload"]), ("replacement", None))


class CoiTests(unittest.TestCase):
    def test_concrete_reasons_require_manual_review(self):
        competitor = policy_with(night__enabled=True, night__coi={**APPROVED.get("night", "coi"), "competitors": ["Rival Rx"]})
        quiet = OVERNIGHT.replace("health plan operations team", "logistics company").replace("pharmacy claims", "shipping claims")
        rival = assess("Claims Quality Auditor (Part-Time)", quiet, policy=competitor, employment="part time", employer="Rival Rx")
        self.assertEqual(rival["coi"]["reasons"], ["a direct competitor of your daytime employer"])
        self.assertEqual(rival["state"], "CONDITIONAL")
        exclusive = assess("Claims Quality Auditor (Part-Time)", quiet + "\nNo outside employment is permitted.", employment="part time")
        self.assertTrue(exclusive["coi"]["manual_coi_review_required"])
        j = job("Claims Quality Auditor (Part-Time)", quiet, employment="part time")
        applied = night.assess(j, extract(quiet, APPROVED).requirements, MID, APPROVED, 60.0, application_conflict=True)
        self.assertIn("this role already has an application through another channel (right to represent)", applied["coi"]["reasons"])
        plain = assess("Claims Quality Auditor (Part-Time)", quiet, employment="part time")
        self.assertEqual((plain["coi"]["manual_coi_review_required"], plain["coi"]["industry_overlap"]), (False, False))


# --- #7 Notes ------------------------------------------------------------------------------------------------------
class NoteTests(unittest.TestCase):
    def test_structured_fields_and_flags(self):
        n = parse_note("source=linkedin_alert; req=ABC123; posted=2026-10-11; pay=$82k-$96k")
        self.assertEqual(n["fields"], {"source": "linkedin_alert", "req": "ABC123", "posted": "2026-10-11", "pay": "$82k-$96k"})
        n = parse_note("source=web; req=R9917; mode=supplemental; schedule=11pm-3am; workload=caution; manual_coi_review_required; "
                       "industry_overlap=false; agency=Harbor Staffing; state=conditional")
        self.assertEqual(n["fields"]["schedule"], "11pm-3am")
        self.assertEqual(n["fields"]["state"], "CONDITIONAL")
        self.assertEqual(n["flags"], {"manual_coi_review_required": True, "industry_overlap": False})

    def test_bad_values_are_dropped_never_stored_and_free_text_still_works(self):
        n = parse_note("posted=yesterday; agency=someone@example.com; state=APPROVED; req=" + "X" * 60 + "; workload=fine")
        self.assertEqual(n["fields"], {})
        self.assertEqual(sorted(n["dropped"]), ["agency", "posted", "req", "state", "workload"])
        old = parse_note("source: LinkedIn alert")
        self.assertEqual((old["fields"], old["free"]), ({}, "source: LinkedIn alert"))
        self.assertTrue(is_alert_source("Example Health · Remote · source: LinkedIn alert"))
        self.assertTrue(is_alert_source("Example Health · Remote · source=indeed_alert; req=7"))
        self.assertFalse(is_alert_source("Example Health · Remote · source=web"))
        self.assertEqual(context_note("Example Health · Remote (US) · source=web; req=R-1")["fields"]["req"], "R-1")

    def test_issue_lines_carry_structured_notes_and_a_note_can_never_change_a_decision(self):
        body = ("Title | Company | Location | Link | Note\n"
                "Registered Nurse | Synth Hospital | Remote | https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7c | "
                "source=web; state=ELIGIBLE; req=N1\n")
        alert = inbox.issue_alert(issue(body=body))
        self.assertIn("state=ELIGIBLE", alert.links[0].context)
        store, ctx = store_ctx()
        outcomes = []
        _record_alert(ctx, alert, None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["outside_target_families"])

    def test_source_of_reads_the_structured_alert_source(self):
        store, _ = store_ctx()
        add_job(store, "gh:synth:1", title="Claims Analyst", title_norm="claims analyst")
        lead(store, "lead:1", "https://x.example/1", "Claims Analyst", "Example · Remote · source=linkedin_alert; req=1", job_id="gh:synth:1")
        self.assertEqual(source_of(store, store.one("SELECT * FROM jobs WHERE job_id = 'gh:synth:1'"))[0], "chatgpt_gmail_alert")


# --- #2 Seen-jobs index and requisition fingerprints -----------------------------------------------------------------
class SeenIndexTests(unittest.TestCase):
    def setUp(self):
        self.store, self.ctx = store_ctx()
        add_job(self.store, "gh:synth:100", title="Claims Analyst", title_norm="claims analyst", family="claims_analysis",
                employer_name="Synthetic Health, Inc.", requisition_id="R-100", location_raw="Remote - US",
                url="https://job-boards.greenhouse.io/synth/jobs/100?gh_src=abc&utm_source=newsletter",
                canonical_url="https://job-boards.greenhouse.io/synth/jobs/100")
        add_job(self.store, "gh:synth:200", title="Claims Analyst", title_norm="claims analyst", family="claims_analysis",
                employer_name="Synthetic Health", requisition_id="R200", url="https://job-boards.greenhouse.io/synth/jobs/200")
        add_job(self.store, "gh:synth:300", title="Software Engineer", title_norm="software engineer", family=None,
                employer_name="Synthetic Health", url="https://job-boards.greenhouse.io/synth/jobs/300")
        lead(self.store, "lead:li", "https://www.linkedin.com/jobs/view/4000000100?trk=public&refId=abc", "Claims Analyst",
             "Synthetic Health · Remote · source=linkedin_alert; req=r100")
        lead(self.store, "lead:in", "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a70&from=serp&tk=1x", "Claims Analyst",
             "Synthetic Health LLC · Remote (US) · req=R-100")

    def test_one_fingerprint_per_requisition_across_sites_and_a_new_requisition_stays_distinct(self):
        records = seen_index.build(self.store)
        by_req = {r.get("requisition_id"): r for r in records}
        self.assertEqual(sorted(by_req), ["R100", "R200"])  # the engineer is outside every family
        first = by_req["R100"]
        self.assertEqual(first["urls"], ["https://job-boards.greenhouse.io/synth/jobs/100", "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a70",
                                         "https://www.linkedin.com/jobs/view/4000000100"])
        self.assertEqual(first["canonical_url"], "https://job-boards.greenhouse.io/synth/jobs/100")  # the employer link wins
        self.assertEqual((first["normalized_company"], first["normalized_title"], first["normalized_location"]),
                         ("synthetic health", "claims analyst", "remote us"))
        self.assertNotEqual(first["fingerprint"], by_req["R200"]["fingerprint"])
        # the same requisition shown under another title on another site is still the same job
        lead(self.store, "lead:li3", "https://www.linkedin.com/jobs/view/4000000777", "Claims Analyst II (Remote)",
             "Synthetic Health · Remote · source=linkedin_alert; req=R-100")
        records = seen_index.build(self.store)
        self.assertEqual(sorted(r.get("requisition_id") for r in records), ["R100", "R200"])
        self.assertIn("https://www.linkedin.com/jobs/view/4000000777",
                      next(r for r in records if r.get("requisition_id") == "R100")["urls"])

    def test_tracking_parameters_never_create_a_new_fingerprint(self):
        clean = lambda u: seen_index.clean_url(u)[0]
        self.assertEqual(clean("https://www.linkedin.com/jobs/view/123456789/?trk=abc&refId=x"), "https://www.linkedin.com/jobs/view/123456789")
        self.assertEqual(clean("https://careers.example.com/jobs/claims-analyst?utm_source=x&id=5&src=mail"),
                         "https://careers.example.com/jobs/claims-analyst")
        self.assertEqual(clean("https://careers.example.com/search?utm_campaign=y&gh_jid=77&gh_src=z"),
                         "https://careers.example.com/search?gh_jid=77")
        self.assertIsNone(seen_index.clean_url("https://careers.example.com/about?utm_campaign=y"))  # not a posting
        before = seen_index.build(self.store)
        lead(self.store, "lead:li2", "https://www.linkedin.com/jobs/view/4000000100?trk=other&trackingId=zzz", "Claims Analyst",
             "Synthetic Health · Remote · source=linkedin_alert")
        self.assertEqual([r["fingerprint"] for r in seen_index.build(self.store)], [r["fingerprint"] for r in before])

    def test_job_facts_only(self):
        record_label(self.store, "gh:synth:100", "skip", "private reason: pay too low for me", "k1")
        text = json.dumps(seen_index.build(self.store))
        for private in ("skip", "private reason", "pay too low", "@", "gmail", "score", "eligib", "label"):
            self.assertNotIn(private, text.lower())
        for record in seen_index.build(self.store):
            self.assertTrue(set(record) <= set(seen_index.FIELDS))
        with self.assertRaises(ValueError):
            seen_index.check_record({"fingerprint": "x", "notes": "mine"})
        with self.assertRaises(ValueError):
            seen_index.check_record({"fingerprint": "x", "normalized_company": "me@example.com"})
        with self.assertRaises(ValueError):
            seen_index.check_record({"fingerprint": "x", "urls": ["https://x.example/jobs/1?token=secret"]})

    def test_rebuilds_are_identical_and_publishing_is_idempotent(self):
        files = seen_index.files(seen_index.build(self.store))
        self.assertEqual(files, seen_index.files(seen_index.build(self.store)))
        repo_files, puts = {}, []

        def gh(argv, input=None, **kw):
            args = argv[1:]
            path = args[-1].split("/contents/", 1)[1] if args[:1] == ["api"] and "/contents/" in args[-1] else args[-3].split("/contents/", 1)[1]
            if "-X" in args:
                body = json.loads(input)
                repo_files[path] = base64.b64decode(body["content"]).decode("utf-8")
                puts.append(path)
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            if path not in repo_files:
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
            content = base64.b64encode(repo_files[path].encode("utf-8")).decode("ascii")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"sha": "s1", "content": content}), "")

        self.assertEqual(inbox.publish_files("owner-x/example-inbox", files, "m", gh), ["seen_jobs.json"])
        self.assertEqual(inbox.publish_files("owner-x/example-inbox", files, "m", gh), [])  # unchanged: no commit
        self.assertEqual(puts, ["seen_jobs.json"])
        with self.assertRaises(inbox.InboxError):
            inbox.publish_files("owner-x/example-inbox", {"../x": "y"}, "m", gh)

    def test_large_indexes_are_sharded(self):
        records = seen_index.build(self.store)
        files = seen_index.files(records, max_per_file=1)
        manifest = json.loads(files["seen_jobs.json"])
        self.assertTrue(manifest["sharded"])
        self.assertEqual(len(manifest["shards"]), 16)
        shard_records = [r for name in manifest["shards"] for r in json.loads(files[name])["records"]]
        self.assertEqual(sorted(r["fingerprint"] for r in shard_records), sorted(r["fingerprint"] for r in records))

    def test_publishing_writes_a_local_copy_and_a_github_failure_never_stops_the_run(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            paths = {"reports": Path(tmp)}
            local_only = policy_with(operations__github_inbox=None)
            ctx = Context(self.store, local_only, MID, JOB_MATRIX, None)
            self.assertEqual(cli._publish_index(ctx, paths), {"index_records": 2})
            self.assertTrue((Path(tmp) / "inbox-index" / "seen_jobs.json").is_file())
            self.assertTrue((Path(tmp) / "inbox-index" / "intake_status.json").is_file())
            down = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 503 (service unavailable)")
            with_repo = Context(self.store, policy_with(operations__github_inbox="owner-x/example-inbox"), MID, JOB_MATRIX, None)
            self.assertEqual(cli._publish_index(with_repo, paths, runner=down), {"index_errors": 1})
            self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE message LIKE 'seen-jobs index not published%'"))

    def test_inbox_dedupe_is_requisition_aware(self):
        alert = AlertEmail("s", "t", None, [
            Link("https://www.linkedin.com/jobs/view/4000000201", "linkedin", "Claims Analyst", "Synthetic Health · Remote · req=R200", True),
            Link("https://www.linkedin.com/jobs/view/4000000301", "linkedin", "Claims Analyst", "Synthetic Health · Remote · req=R999", True),
            Link("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a99", "indeed", "Claims Analyst", "Synthetic Health · Remote · req=R999", True)])
        outcomes = []
        _record_alert(self.ctx, alert, None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["already_tracked", "lead", "seen_before"])


# --- #8 Gmail outage recovery ----------------------------------------------------------------------------------------
class GmailRecoveryTests(unittest.TestCase):
    def test_status_line(self):
        self.assertEqual(inbox.gmail_status("picks\nGmail status: unavailable\n"), "unavailable")
        self.assertEqual(inbox.gmail_status("- **Gmail status:** partial"), "partial")
        self.assertEqual(inbox.gmail_status("Gmail status: ok"), "ok")
        self.assertEqual(inbox.gmail_status("Gmail alert intake unavailable for this run"), "unavailable")
        self.assertIsNone(inbox.gmail_status("no status line here"))

    def test_a_reported_outage_asks_the_next_run_for_a_seven_day_lookback_without_any_gmail_data(self):
        store, ctx = store_ctx()
        self.assertEqual(seen_index.intake_status(store)["gmail"]["next_run_lookback_days"], 3)
        fake = FakeGh([issue(7, "Gmail status: unavailable\nNo jobs today.")])
        _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)
        status = seen_index.intake_status(store)["gmail"]
        self.assertEqual((status["last_reported_status"], status["next_run_lookback_days"]), ("unavailable", 7))
        self.assertIn("7-day Gmail lookback", fake.calls[1][2])  # said in the reply too
        fake.issues.append(issue(8, "Gmail status: ok\nNo jobs today."))
        _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)
        self.assertEqual(seen_index.intake_status(store)["gmail"]["next_run_lookback_days"], 3)
        text = seen_index.intake_file(store)["intake_status.json"]
        self.assertEqual(set(json.loads(text)), {"schema", "gmail"})
        self.assertNotIn("@", text)


# --- #9 Partial failures -----------------------------------------------------------------------------------------------
class PartialFailureTests(unittest.TestCase):
    def test_one_malformed_github_row_does_not_kill_the_batch(self):
        body = ("Title | Company | Location | Link | Note\n"
                "Claims Analyst | Acme | Remote | https://[broken/jobs/1 | x\n"
                "Claims Analyst | Payer Co | Remote (US) | https://www.linkedin.com/jobs/view/4012345678 | source=web\n")
        store, ctx = store_ctx()
        fake = FakeGh([issue(7, body)])
        _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)
        reply = fake.calls[1][2]
        self.assertIn("CareerOS read this issue on 2026-10-12: 1 skipped, 1 lead.", reply)
        self.assertIn("could not be read (malformed)", reply)
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads")["c"], 1)
        self.assertTrue(fake.issues[0].get("closed"))

    def test_an_issue_is_read_once_even_while_its_close_keeps_failing(self):
        store, ctx = store_ctx()
        fake = FakeGh([issue(7, "Claims Analyst | Payer Co | Remote | https://www.linkedin.com/jobs/view/4012345670 | x")],
                      fail_close_times=3)
        first = _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)
        self.assertEqual((first["inbox_issues"], first["inbox_close_failed"]), (1, 1))
        again = _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)  # still open on GitHub: listed again
        self.assertEqual((again["inbox_issues"], again["inbox_issue_errors"], again["inbox_close_failed"]), (0, 0, 1))
        self.assertEqual(store.one("SELECT COUNT(*) c FROM inbox_issues")["c"], 1)
        self.assertEqual(sum(1 for c in fake.calls if c[:2] == ("issue", "comment")), 2)  # the stored reply, retried

    def test_an_error_on_one_line_is_flagged_and_the_rest_carry_on(self):
        store, ctx = store_ctx()
        real = cli._known_job
        boom = lambda c, url: (_ for _ in ()).throw(KeyError("odd")) if url.endswith("401") else real(c, url)
        alert = AlertEmail("s", "t", None, [
            Link("https://www.linkedin.com/jobs/view/4000000401", "linkedin", "Claims Analyst", "A · Remote", True),
            Link("https://www.linkedin.com/jobs/view/4000000402", "linkedin", "Claims Analyst", "B · Remote", True)])
        outcomes = []
        with mock.patch.object(cli, "_known_job", boom):
            counts = _record_alert(ctx, alert, None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["row_error", "lead"])
        self.assertEqual(counts["lead_errors"], 1)
        self.assertTrue(store.one("SELECT 1 FROM exceptions WHERE error_code = 'alert_line_failed'"))

    def test_one_failing_issue_leaves_the_others_processed(self):
        store, ctx = store_ctx()
        real = inbox.issue_alert
        fake = FakeGh([issue(7), issue(8, "Claims Analyst | Payer Co | Remote | https://www.linkedin.com/jobs/view/4012345679 | x")])
        with mock.patch.object(inbox, "issue_alert", lambda i: (_ for _ in ()).throw(ValueError("bad")) if i["number"] == 7 else real(i)):
            counts = _inbox_pull(ctx, None, "owner-x/example-inbox", runner=fake)
        self.assertEqual((counts["inbox_issue_errors"], counts["inbox_issues"]), (1, 1))
        self.assertEqual([i.get("closed", False) for i in fake.issues], [False, True])  # #7 stays open for a retry

    def test_one_employer_board_failing_unexpectedly_never_stops_the_scan(self):
        store, ctx = store_ctx()
        cli._add_employer(ctx, "Bad Board", "greenhouse", "bad", admitted_via="legacy")
        cli._add_employer(ctx, "Good Board", "greenhouse", "good", admitted_via="legacy")
        calls = []

        def fetch(client, ats, token):
            calls.append(token)
            if token == "bad":
                raise KeyError("jobs")
            return []
        with mock.patch.object(cli, "fetch_board", fetch):
            counts = cli.cmd_discover(ctx, None, SimpleNamespace(employer=None))
        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(counts["board_errors"], 1)

    def test_one_failing_night_assessment_never_stops_the_others(self):
        store, ctx = store_ctx()
        for n in (1, 2):
            add_job(store, f"gh:synth:{n}", current_state="scored", title="Claims Quality Auditor", title_norm="claims quality auditor",
                    description_text=OVERNIGHT, remote_type="remote", country="US", employment_type="part_time")
        real = night.screen
        with mock.patch.object(night, "screen", lambda j, p, pol: (_ for _ in ()).throw(KeyError("x")) if j["job_id"].endswith("1")
                               else real(j, p, pol)):
            counts = night_process(ctx)
        self.assertEqual(counts["night_errors"], 1)
        self.assertEqual(store.one("SELECT COUNT(*) c FROM night_results")["c"], 1)


# --- Found in the first live hand-off test (issue #2, 2026-10-12) -----------------------------------------------------
class HandoffFindingsTests(unittest.TestCase):
    LEVER_STYLE = ("The Business Analyst provides analytical and process support to the operations team.\n"
                   "What You Will Work On:\n- Lead requirements gathering with stakeholders\n"
                   "- Build dashboards and reports in Excel\n"
                   "Who We Are Looking For:\n- 2-4 years of experience as a Data Analyst or Operations Associate\n"
                   "- Strong data analysis skills\n"
                   "Additional Qualifications\n- Deep Excel experience\n- P&L experience\n"
                   "What You Can Expect:\n- Comprehensive medical, dental and vision insurance for every member\n"
                   "About Synth Health\nOur leaders bring 15+ years of claims experience to every member.\n"
                   "Interviewing with Synth\n- A short skills assessment; 12+ years of history behind our process\n"
                   "Who We Are\nWe are a friendly company. Must have 10+ years of experience at sea.\n")

    def test_lever_style_headings_are_read(self):
        result = extract(self.LEVER_STYLE, APPROVED)
        self.assertEqual(result.sections, ["responsibilities", "ambiguous", "ambiguous", "other", "other", "other", "other"])
        required = [r for r in result.requirements if r.requirement_type != "responsibility"]
        self.assertIn(("years_experience", 2.0), {(r.category, r.years) for r in required})
        self.assertIn("excel", {r.name for r in required if r.category == "skill"})
        # company, benefits and interview-process text after the qualifications is never read as a requirement
        self.assertFalse({10.0, 12.0, 15.0} & {r.years for r in result.requirements})
        self.assertNotIn("healthcare", {r.name for r in required if r.category == "domain"})
        self.assertIn("excel", {r.name for r in result.requirements if r.requirement_type == "responsibility"})

    def test_a_new_board_found_through_the_inbox_takes_the_company_name_and_parent(self):
        import httpx
        from careeros.discovery.http import HttpClient
        from tests import postings
        from tests.test_pipeline import gh_job
        board = lambda request: (httpx.Response(200, json=gh_job(42, "Pharmacy Claims Analyst", postings.GOOD_FIT, req="R-42"))
                                 if request.url.path == "/v1/boards/northgaterxjobs/jobs/42" else httpx.Response(404))
        client = HttpClient(transport=httpx.MockTransport(board), min_interval=0, sleep=lambda s: None)
        store, ctx = store_ctx()
        try:
            _record_alert(ctx, AlertEmail("s", "t", None, [
                Link("https://job-boards.greenhouse.io/northgaterxjobs/jobs/42", "greenhouse", "Pharmacy Claims Analyst",
                     "Northgate Rx · Remote (US) · source=web; req=R-42", True)]), client)
        finally:
            client.close()
        employer = store.one("SELECT name, parent FROM employers WHERE board_token = 'northgaterxjobs'")
        self.assertEqual((employer["name"], employer["parent"]), ("Northgate Rx", "Northgate Group"))  # the D13 hold applies
        self.assertIsNone(cli._company_name("someone@example.com · Remote"))
        self.assertIsNone(cli._company_name("source=web; req=1"))

    def test_application_and_recruiter_mail_is_never_read_for_jobs(self):
        from careeros.discovery.alerts import is_correspondence
        for subject in ("Your application was sent to Example Health", "Thank you for applying to Example Rx",
                        "Example Health viewed your application", "Interview invitation: Claims Analyst",
                        "Please complete your assessment", "Unfortunately, we will not be moving forward",
                        "Your job offer from Example Health"):
            self.assertTrue(is_correspondence(subject), subject)
        for subject in ('"business analyst": Avalere Health - Business Analyst and more', "30+ new jobs for claims analyst",
                        "Example Health is hiring a Risk Assessment Analyst", "New jobs similar to Claims Analyst"):
            self.assertFalse(is_correspondence(subject), subject)
        self.assertIn('-subject:("your application"', APPROVED.get("operations", "gmail_query"))

    def test_gmail_skips_correspondence_from_an_alert_sender(self):
        store, ctx = store_ctx()
        eml = lambda subject: ("From: Job Alerts <jobs-noreply@example.com>\r\nTo: x@example.com\r\nSubject: " + subject +
                               "\r\nContent-Type: text/plain\r\n\r\nClaims Analyst\r\nhttps://www.linkedin.com/jobs/view/4000000888\r\n").encode()

        class FakeGmail:
            mails = {"m1": eml("Your application was sent to Example Health"), "m2": eml("New jobs similar to Claims Analyst")}

            def search(self, query):
                return list(self.mails)

            def raw(self, message_id):
                return self.mails[message_id]

            def close(self):
                pass

        counts = cli._gmail_pull(ctx, None, "q", gmail=FakeGmail())
        self.assertEqual((counts["correspondence_skipped"], counts["gmail_messages"]), (1, 2))
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads")["c"], 1)  # only the alert's job
        self.assertEqual(store.one("SELECT links FROM alert_messages WHERE message_id = 'm1'")["links"], 0)


# --- Missed-opportunity follow-ups (approved 2026-10-12) ----------------------------------------------------------------
class FollowUpTests(unittest.TestCase):
    def test_inverted_titles_and_adjacent_families(self):
        from careeros.normalize.posting import classify_family, norm
        fam = lambda title: classify_family(norm(title), APPROVED.get("families"))
        self.assertEqual(fam("Senior Analyst, Analytics - Regulatory Reporting"), "data_reporting")
        self.assertEqual(fam("Analyst, Quality Assurance"), "data_reporting")
        self.assertIsNone(fam("Analyst II"))
        self.assertIsNone(fam("Specialist, Brand Marketing"))
        for title in ("Senior Specialist, Provider Data Operations (US Healthcare Insurance)", "Payer Integration Specialist",
                      "Product Owner - Claims", "Senior Analyst, SIU Investigator", "Risk Adjustment Auditor"):
            self.assertEqual(fam(title), "pbm_claims_operations", title)
        self.assertEqual(fam("Enterprise Implementation Manager"), "process_analyst")
        ic = gate("Enterprise Implementation Manager", posting(required=["Excel skills required"]))
        self.assertNotIn("seniority_gap", failures(ic))  # an individual-contributor manager title, not people management

    def test_coverage_is_not_judged_on_generic_items_alone(self):
        thin = posting(required=["Agile experience required", "Strong documentation skills required"])
        j = job("Business Systems Analyst", thin)
        from careeros.qualify.scores import eligibility
        reqs = extract(thin, APPROVED).requirements
        dim = eligibility(j, reqs, CLAIMS_PROFILE, evaluate(j, reqs, CLAIMS_PROFILE, APPROVED), APPROVED)["dims"]["skills_coverage"]
        self.assertEqual((dim["value"], dim["unknown"]), (0.7, True))
        self.assertIn("too few to judge", dim["basis"])
        full = eligibility(job("Pharmacy Claims Analyst", CLAIMS_POSTING), extract(CLAIMS_POSTING, APPROVED).requirements,
                           CLAIMS_PROFILE, evaluate(job("Pharmacy Claims Analyst", CLAIMS_POSTING),
                                                    extract(CLAIMS_POSTING, APPROVED).requirements, CLAIMS_PROFILE, APPROVED),
                           APPROVED)["dims"]["skills_coverage"]
        self.assertFalse(full["unknown"])

    def test_role_titles_you_confirm_answer_years_as_a_role(self):
        text = posting(required=["2-4 years of experience as a Junior Analyst, QA Analyst, Data Analyst, or Operations Associate"])
        self.assertIn("years_unknown", flags(gate("Business Systems Analyst", text)))
        confirmed = Profile(data={**MID.data, "role_titles": [{"name": "data analyst", "screening_years": 4}]}, fingerprint="r")
        result = gate("Business Systems Analyst", text, profile=confirmed)
        self.assertNotIn("years_unknown", flags(result))
        self.assertEqual(state_for(result, APPROVED), "ELIGIBLE")

    def test_the_stale_flag_follows_the_policy_setting(self):
        from careeros.review.report import build_report
        store, _ = store_ctx()
        add_job(store, "gh:synth:9", current_state="scored", title="Claims Analyst", title_norm="claims analyst",
                posted_at="2026-09-22T12:00:00.000Z", employment_type="full_time", remote_type="remote")
        store.writer("qualify").insert("scores", {
            "score_id": "s9", "job_id": "gh:synth:9", "at": store.clock.now(), "policy_fingerprint": "p", "profile_fingerprint": "q",
            "eligibility": 90, "candidate_fit": 90, "career_value": 90, "freshness": 30, "apply_priority": 70, "tier": "near_miss",
            "interview_band": "High", "state": "ELIGIBLE",
            "components_json": json.dumps({"eligibility": {"matched_skills": [], "missing_skills": [],
                                                           "dims": {"skills_coverage": {"basis": "x"}}},
                                           "candidate_fit": {"matched_skills": []}, "state": "ELIGIBLE"})})
        self.assertNotIn("may be stale", build_report(store, MID, APPROVED))  # 20 days old, limit 60
        strict = policy_with(discovery__max_posting_age_days=14)
        self.assertIn("may be stale", build_report(store, MID, strict))


# --- Your decisions of 2026-10-12 (evening) ------------------------------------------------------------------------------
class DecisionTests(unittest.TestCase):
    PBM_PM = posting(required=["Bachelor's degree required", "Experience with RxClaim and PBM pharmacy benefit operations"],
                     duties=["Own the PBM platform roadmap with engineering and operations"])

    def test_pbm_product_roles_are_a_stretch_instead_of_a_degree_rejection(self):
        from careeros.qualify.scores import score
        result = gate("Senior Product Manager, PBM Platform", self.PBM_PM)
        self.assertNotIn("degree_required", failures(result))
        self.assertIn("degree_stretch", flags(result))
        easy = policy_with(tiers__A__priority=1, tiers__B__priority=1, tiers__skill_coverage_min=0.0)
        j = job("Senior Product Manager, PBM Platform", self.PBM_PM)
        reqs = extract(self.PBM_PM, easy).requirements
        card = score(j, reqs, MID, evaluate(j, reqs, MID, easy), easy, "2026-10-12T12:00:00.000Z")
        self.assertEqual((card.tier, card.components["stretch"]), ("near_miss", True))  # never Tier A/B
        # the exception is narrow: not product roles outside PBM, and not PBM roles that are not product roles
        logistics = self.PBM_PM.replace("RxClaim and PBM pharmacy benefit", "freight").replace("PBM platform", "fleet platform")
        logistics = logistics.replace("pharmacy claims", "shipping")
        self.assertIn("degree_required", failures(gate("Senior Product Manager, Fleet Platform", logistics)))
        self.assertIn("degree_required", failures(gate("PBM Claims Analyst", self.PBM_PM)))

    def test_a_long_or_list_of_roles_reaches_its_last_alternative(self):
        line = "2-4 years of experience as a Junior Analyst, QA Analyst, Data Analyst, or Operations Associate; demonstrated skill"
        text = posting(required=[line])
        self.assertIn("years_unknown", flags(gate("Business Analyst, Appraisal Operations", text)))
        ops = Profile(data={**MID.data, "role_titles": [{"name": "operations associate", "years": 3.7, "screening_years": 4}]},
                      fingerprint="ops")
        self.assertNotIn("years_unknown", flags(gate("Business Analyst, Appraisal Operations", text, profile=ops)))

    def test_exact_role_titles_never_spill_into_specific_titles(self):
        generic = Profile(data={**MID.data, "role_titles": [
            {"name": "operations analyst", "years": 3.7, "screening_years": 4},
            {"name": "analyst", "years": 3.7, "screening_years": 4, "exact": True},
            {"name": "analyst role", "years": 3.7, "screening_years": 4, "exact": True}]}, fingerprint="g")
        self.assertEqual(generic.years_for("analyst"), 4.0)
        self.assertEqual(generic.years_for("analyst role"), 4.0)
        self.assertEqual(generic.years_for("senior operations analyst"), 4.0)
        self.assertIsNone(generic.years_for("data analyst role"))  # not credited as data-analyst years
        self.assertIsNone(generic.years_for("qa analyst"))
        self.assertIn("years_unknown", flags(gate("Business Systems Analyst", posting(
            required=["3+ years of experience as a Data Analyst required"]), profile=generic)))
        self.assertNotIn("years_unknown", flags(gate("Business Systems Analyst", posting(
            required=["3+ years of experience in an analyst role required"]), profile=generic)))

    def test_generic_years_use_your_total_professional_years(self):
        self.assertNotIn("years_unknown", flags(gate("Business Systems Analyst", posting(
            required=["3+ years of relevant experience required"]))))
        self.assertIn("years_gap", flags(gate("Business Systems Analyst", posting(
            required=["7+ years of professional experience required"]))))  # 4 held: 3 short
        self.assertIn("years_far_short", failures(gate("Business Systems Analyst", posting(
            required=["9+ years of hands-on experience required"]))))


# --- #13 Acceptance regression list --------------------------------------------------------------------------------------
class AcceptanceTests(unittest.TestCase):
    def test_bachelors(self):
        self.assertIn("degree_required", failures(gate("Business Systems Analyst", posting(required=["Bachelor's degree required"]))))
        preferred = gate("Business Systems Analyst", posting(required=["Excel skills required"], preferred=["Bachelor's degree"]))
        self.assertNotIn("degree_required", failures(preferred))
        self.assertEqual(state_for(preferred, APPROVED), "ELIGIBLE")
        equivalent = gate("Business Systems Analyst", posting(required=["Bachelor's degree or equivalent experience"]))
        self.assertNotIn("degree_required", failures(equivalent))
        self.assertIn("degree_equivalency", flags(equivalent))

    def test_pay(self):
        unposted = gate("Business Systems Analyst", posting(required=["Excel skills required"]))
        self.assertFalse({"below_pay_floor", "pay_untrusted_below_floor"} & (failures(unposted) | flags(unposted)))
        self.assertEqual(state_for(unposted, APPROVED), "ELIGIBLE")
        low = posting(required=["Excel skills required"], extra="This role is remote.\nPay: $25 - $30 per hour")
        j = dict(job("Business Systems Analyst", low), url="https://www.linkedin.com/jobs/view/4000000999")
        aggregator = evaluate(j, extract(low, APPROVED).requirements, MID, APPROVED)
        self.assertNotIn("below_pay_floor", failures(aggregator))
        self.assertIn("pay_untrusted_below_floor", flags(aggregator))
        j = dict(job("Business Systems Analyst", low), url="https://job-boards.greenhouse.io/synth/jobs/9")
        self.assertIn("below_pay_floor", failures(evaluate(j, extract(low, APPROVED).requirements, MID, APPROVED)))

    def test_experience(self):
        far = gate("Business Systems Analyst", posting(required=["9+ years of claims experience required"]))
        self.assertIn("years_far_short", failures(far))  # 9 required, 4 held: more than 4 short
        major = gate("Business Systems Analyst", posting(required=["8+ years of claims experience required"]))
        self.assertEqual((failures(major), "years_gap" in flags(major)), (set(), True))  # 4 short: major penalty only
        preferred = gate("Business Systems Analyst", posting(required=["Excel skills required"],
                                                             preferred=["9+ years of claims experience"]))
        self.assertNotIn("years_far_short", failures(preferred))
        self.assertIn("years_preferred_short", flags(preferred))

    def test_preferred_and_contextual_requirements_never_become_hard_rejections(self):
        result = gate("Business Systems Analyst", posting(
            required=["Excel skills required"],
            preferred=["Master's degree", "PMP certification", "10+ years of claims experience", "Security clearance"],
            duties=["Work with licensed pharmacists on claims questions", "Support the team during audits"]))
        self.assertEqual(failures(result), set())

    def test_night_schedule_mode_and_pay(self):
        unknown = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am",
                                                                                 "varies"), employment="part time")
        self.assertEqual(unknown["state"], "CONDITIONAL")
        daytime = assess("Claims Quality Auditor", OVERNIGHT.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am",
                                                                     "day shift, 8am to 5pm"))
        self.assertIn("daytime_schedule", {f["code"] for f in daytime["failures"]})
        self.assertEqual(assess("Claims Quality Auditor", OVERNIGHT, employment="full time")["mode"], "replacement")
        evening = OVERNIGHT.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am", "evening shift, 5pm to 10pm")
        self.assertEqual(assess("Claims Quality Auditor", evening, employment="full time")["mode"], "supplemental")
        weekend = OVERNIGHT.replace("overnight shift, Sunday to Thursday, 11pm to 7:30am", "weekend shifts, Saturday and Sunday")
        self.assertEqual(assess("Claims Quality Auditor", weekend, employment="full time")["mode"], "supplemental")
        self.assertEqual(assess("Claims Quality Auditor (Part-Time)", OVERNIGHT, employment="part time")["mode"], "supplemental")
        cheap_supp = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT + "\nPay: $25 - $28 per hour", employment="part time")
        self.assertIn("below_night_pay_floor", {f["code"] for f in cheap_supp["failures"]})
        fine_supp = assess("Claims Quality Auditor (Part-Time)", OVERNIGHT + "\nPay: $31 - $33 per hour", employment="part time")
        self.assertNotIn("below_night_pay_floor", {f["code"] for f in fine_supp["failures"]})
        cheap_repl = assess("Claims Quality Auditor", OVERNIGHT + "\nPay: $31 - $33 per hour", employment="full time")
        self.assertEqual(cheap_repl["mode"], "replacement")
        self.assertIn("below_night_pay_floor", {f["code"] for f in cheap_repl["failures"]})  # under the $74,000 Replacement floor

    def test_employers(self):
        self.assertIn("employer_excluded", failures(gate("Business Systems Analyst", posting(required=["Excel skills required"]),
                                                         employer="Brambleton Business Services")))
        store, ctx = store_ctx()
        outcomes = []
        _record_alert(ctx, AlertEmail("s", "t", None, [Link("https://www.linkedin.com/jobs/view/4000000555", "linkedin",
                                                            "Claims Analyst", "Brambleton · Remote", True)]), None, outcomes)
        self.assertEqual([o for _, o in outcomes], ["suppressed"])
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads")["c"], 0)
        held = gate("Business Systems Analyst", posting(required=["Excel skills required"]), employer="Northgate", parent="Northgate Group")
        self.assertEqual(state_for(held, APPROVED), "HOLD")

    def test_acceptance_targets(self):
        store, _ = store_ctx()
        self.assertEqual(agreement(store)["targets"], {"labelled_at_least": 50, "precision_of_picks_at_least": 0.9,
                                                       "false_reject_rate_at_most": 0.1})
        self.assertEqual(agreement(store, "night")["targets"]["schedule_fit_false_positive_rate_at_most"], 0.1)


# --- #14 Outcome funnel ------------------------------------------------------------------------------------------------
class FunnelTests(unittest.TestCase):
    def test_recommended_to_hired_with_negatives_and_new_dimensions(self):
        store, _ = store_ctx()
        matrix = application_matrix()
        for n, tier in ((1, "near_miss"), (2, "B"), (3, "archive")):
            job_id = add_job(store, f"gh:synth:{n}", current_state="scored", employment_type="contract")
            store.writer("qualify").insert("scores", {
                "score_id": f"s{n}", "job_id": job_id, "at": store.clock.now(), "policy_fingerprint": "p", "profile_fingerprint": "q",
                "eligibility": 90, "candidate_fit": 90, "career_value": 90, "freshness": 90, "apply_priority": 90, "tier": tier,
                "interview_band": "High", "state": "ELIGIBLE", "components_json": json.dumps({"domain": {"level": "direct"}})})
        record_label(store, "gh:synth:1", "apply", None, "l1")
        record_label(store, "gh:synth:2", "skip", None, "l2")
        a1 = record_application(store, matrix, "gh:synth:1", "agency", agency="Synthetic Staffing", key="k1")
        a3 = record_application(store, matrix, "gh:synth:3", "direct", key="k3")
        update_application(store, matrix, a1, "acknowledged", key="u1")
        update_application(store, matrix, a3, "acknowledged", key="u2")
        update_application(store, matrix, a3, "rejected", key="u3")
        row = funnel(store, "employment_type")[0]
        self.assertEqual((row["employment_type"], row["recommended"], row["approved"], row["applied"], row["responded"], row["rejected"]),
                         ("contract", 2, 1, 2, 0, 1))  # confirmations are not responses
        self.assertEqual({r["channel"] for r in funnel(store, "channel")}, {"agency", "direct", "not applied"})
        self.assertIn("night_mode", {d for d in __import__("careeros.review.funnel", fromlist=["x"]).DIMENSIONS})


if __name__ == "__main__":
    unittest.main()
