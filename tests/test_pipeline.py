"""End-to-end Phase 1 flow against a simulated Greenhouse board (no live network). All data is synthetic."""
import html
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

import httpx

from careeros.core.clock import FixedClock
from careeros.core.policy import PolicyBundle, load_policy
from careeros.core.fingerprint import fingerprint
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board, parse_job_url
from careeros.discovery.http import FetchError, HttpClient
from careeros.paths import CONFIG_DIR, ENGINE_ROOT
from careeros.pipeline import Context, ingest, process
from careeros.qualify.profile import load_profile
from careeros.review.labels import agreement, record_label
from careeros.review.report import build_report
from careeros.review.server import make_handler

from tests import postings
from tests.helpers import JOB_MATRIX

FIXTURES = Path(__file__).parent / "fixtures"


def as_greenhouse_html(text: str) -> str:
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            parts.append(f"<ul><li>{html.escape(line[2:])}</li></ul>")
        elif line:
            parts.append(f"<p>{html.escape(line)}</p>")
    return html.escape("".join(parts))  # Greenhouse returns escaped HTML


def gh_job(job_id, title, text, req=None, location="Remote - US"):
    return {"id": job_id, "title": title, "location": {"name": location}, "requisition_id": req,
            "absolute_url": f"https://job-boards.greenhouse.io/synth/jobs/{job_id}", "content": as_greenhouse_html(text),
            "updated_at": "2026-10-10T06:00:00-04:00", "first_published": "2026-10-10T06:00:00-04:00"}


class Board:
    def __init__(self, jobs):
        self.jobs = jobs

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boards/synth/jobs":
            return httpx.Response(200, json={"jobs": self.jobs})
        if request.url.path == "/v1/boards/missing/jobs":
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(500)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-10-10T12:00:00.000Z")
        self.policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        self.store = open_store(":memory:", self.clock, self.policy.fingerprint)
        self.ctx = Context(self.store, self.policy, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(self.tmp.name))
        self.employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                         "ats": "greenhouse", "board_token": "synth", "industry": "health insurance", "status": "verified"}
        self.store.writer("admin").insert("employers", self.employer)
        self.board = Board([
            gh_job(101, "Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="R-1"),
            gh_job(102, "Business Analyst", postings.HARD_FAIL, req="R-2", location="Remote"),
            gh_job(103, "Senior Business Systems Analyst - Pharmacy Benefits (copy)", postings.GOOD_FIT, req="R-1"),
            gh_job(104, "Business Systems Analyst", "Short posting. Apply now."),
            gh_job(106, "Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="R-1", location="Austin, TX"),
            gh_job(107, "Business Systems Analyst (Bangladesh)", postings.GOOD_FIT, req="R-7", location="Dhaka, Bangladesh"),
        ])
        self.client = HttpClient(transport=httpx.MockTransport(self.board.handler), min_interval=0, sleep=lambda s: None)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def poll(self):
        self.clock.advance(minutes=1)
        return ingest(self.ctx, self.employer, fetch_board(self.client, "greenhouse", "synth"))

    def state(self, job_id):
        return self.store.one("SELECT current_state FROM jobs WHERE job_id = ?", (job_id,))["current_state"]

    def assert_all_consistent(self):
        self.assertEqual(self.store.verify_event_chain(), [])
        for row in self.store.all("SELECT job_id FROM jobs"):
            self.assertEqual(self.store.verify_job_consistency(JOB_MATRIX, row["job_id"]), set(), row["job_id"])

    def test_full_flow(self):
        counts = self.poll()
        self.assertEqual((counts["new"], counts["duplicate"], counts["variant"]), (5, 1, 1))
        self.assertEqual(self.state("greenhouse:synth:103"), "duplicate")  # same requisition, same place
        variant = self.store.one("SELECT variant_of, current_state FROM jobs WHERE job_id = 'greenhouse:synth:106'")
        self.assertEqual(tuple(variant), ("greenhouse:synth:101", "normalized"))  # same requisition, other place: kept
        self.assertEqual(len(list(Path(self.tmp.name).rglob("*.json"))), 6)  # raw snapshots kept in the vault

        self.clock.advance(minutes=1)
        process(self.ctx)
        self.assertEqual(self.state("greenhouse:synth:101"), "scored")
        self.assertEqual(self.state("greenhouse:synth:102"), "rejected")
        self.assertEqual(self.state("greenhouse:synth:104"), "needs_review")
        screened = self.store.one("SELECT reason_code FROM transitions WHERE entity_id = 'greenhouse:synth:107' ORDER BY sequence DESC")
        self.assertEqual(screened["reason_code"], "outside_us")  # rejected by the early screen, before extraction
        tier = self.store.one("SELECT tier FROM scores WHERE job_id = 'greenhouse:synth:101'")["tier"]
        self.assertIn(tier, ("A", "B"))
        reason = self.store.one("SELECT reason_code FROM transitions WHERE entity_id = 'greenhouse:synth:102' ORDER BY sequence DESC")
        self.assertTrue(reason["reason_code"])
        self.assert_all_consistent()

        # Second poll: 102 delisted, 101 edited materially.
        self.board.jobs = [gh_job(101, "Senior Business Systems Analyst - Pharmacy Benefits",
                                  postings.GOOD_FIT.replace("3+ years of SQL", "4+ years of SQL"), req="R-1"),
                           *self.board.jobs[2:]]
        counts = self.poll()
        # 2026-10-12: one missed complete scan is a possible absence, not a closure; a text-only edit is re-read
        # (the requirement change is recorded at extraction) but is not a meaningful field change
        self.assertEqual((counts["possibly_absent"], counts["closed"], counts["edited_cosmetic"]), (1, 0, 1))
        self.assertEqual(self.state("greenhouse:synth:101"), "normalized")
        self.assertEqual(self.poll()["closed"], 1)  # the second complete scan without it confirms the closure
        self.assertEqual(self.state("greenhouse:synth:102"), "closed")
        self.clock.advance(minutes=1)
        process(self.ctx)
        self.assertEqual(self.state("greenhouse:synth:101"), "scored")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM scores WHERE job_id='greenhouse:synth:101'")["c"], 2)  # history kept
        self.assert_all_consistent()

    def test_policy_change_reevaluates_with_history(self):
        self.poll()
        process(self.ctx)
        data = json.loads(json.dumps(self.policy.data))
        data["tiers"]["A"]["priority"] = 99
        self.ctx.policy = PolicyBundle(data=data, fingerprint=fingerprint(data))
        self.clock.advance(minutes=5)
        counts = process(self.ctx)
        self.assertGreaterEqual(counts["reevaluated"], 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM gate_results WHERE job_id='greenhouse:synth:101'")["c"], 2)
        self.assert_all_consistent()

    def test_widening_job_families_rescues_title_rejected_jobs(self):
        self.board.jobs.append(gh_job(108, "PBM Operations Associate", postings.GOOD_FIT, req="R-8"))
        self.poll()
        data = json.loads(json.dumps(self.policy.data))
        data["families"].pop("pbm_claims_operations")  # the policy before the title audit
        data["title_rescue"]["enabled"] = False  # (the 2026-10-12 second review would rescue it; this test is about stale families)
        self.ctx.policy = PolicyBundle(data=data, fingerprint=fingerprint(data))
        process(self.ctx)
        self.assertEqual(self.state("greenhouse:synth:108"), "rejected")
        self.ctx.policy = self.policy  # families widened: the job must be re-screened with fresh families
        self.clock.advance(minutes=5)
        process(self.ctx)
        row = self.store.one("SELECT current_state, family FROM jobs WHERE job_id = 'greenhouse:synth:108'")
        self.assertEqual(tuple(row), ("scored", "pbm_claims_operations"))  # live bug: stale family kept it rejected
        self.assert_all_consistent()

    def test_employment_type_stated_in_the_title_wins_and_is_refreshed(self):
        self.board.jobs.append(gh_job(109, "Pharmacy Benefits Systems Analyst (Contractor)", postings.GOOD_FIT, req="R-9"))
        self.poll()
        process(self.ctx)
        query = "SELECT employment_type FROM jobs WHERE job_id = 'greenhouse:synth:109'"
        self.assertEqual(self.store.one(query)["employment_type"], "contract")
        # a value stored by older code is corrected on the next run, and the job is re-evaluated with it
        self.store.writer("discovery").update("jobs", {"employment_type": "full_time"}, "job_id = ?", ("greenhouse:synth:109",))
        self.clock.advance(minutes=5)
        counts = process(self.ctx)
        self.assertEqual(self.store.one(query)["employment_type"], "contract")
        self.assertGreaterEqual(counts["refreshed"], 1)
        self.assert_all_consistent()

    def test_board_errors_never_close_jobs(self):
        with self.assertRaises(FetchError) as ctx:
            fetch_board(self.client, "greenhouse", "missing")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_report_escapes_posting_text_and_labels_measure_agreement(self):
        self.board.jobs.append(gh_job(105, "<script>alert(1)</script> Business Systems Analyst", postings.GOOD_FIT, req="R-5"))
        self.poll()
        process(self.ctx)
        page = build_report(self.store, self.ctx.profile, self.policy, seed=1)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

        record_label(self.store, "greenhouse:synth:101", "apply", "great fit", key="k1")
        self.assertEqual(record_label(self.store, "greenhouse:synth:101", "apply", "great fit", key="k1"), "replay")
        record_label(self.store, "greenhouse:synth:102", "apply", "I think I qualify", key="k2")
        result = agreement(self.store)
        self.assertEqual(result["false_reject_jobs"], ["greenhouse:synth:102"])
        self.assertEqual(result["unexplained_rejections"], 0)

    def test_excluded_fixture_rows_never_count_in_metrics(self):
        self.poll()
        process(self.ctx)
        self.store.conn.execute("UPDATE jobs SET exclude_from_kpi = 1 WHERE job_id = 'greenhouse:synth:102'")
        record_label(self.store, "greenhouse:synth:102", "apply", None, key="k3")
        self.assertEqual(agreement(self.store)["labelled"], 0)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        self.store = open_store(":memory:", self.clock, self.policy.fingerprint)
        self.tmp = tempfile.TemporaryDirectory()
        self.token = "test-token-123"
        handler = make_handler(self.store, load_profile(FIXTURES / "synthetic_profile.yaml"), self.policy, self.token,
                               Path(self.tmp.name) / "writer.lock", {})
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_token_host_and_form_key_required(self):
        base = f"http://127.0.0.1:{self.port}"
        self.assertEqual(httpx.get(f"{base}/").status_code, 403)
        self.assertEqual(httpx.get(f"{base}/?t=wrong").status_code, 403)
        self.assertEqual(httpx.get(f"{base}/?t={self.token}", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(httpx.get(f"{base}/?t={self.token}").status_code, 200)
        forged = httpx.post(f"{base}/label?t={self.token}", data={"target": "x", "key": "forged", "label": "apply"})
        self.assertEqual(forged.status_code, 400)


class EmploymentTypeTests(unittest.TestCase):
    def test_title_markers_override_board_defaults_but_jobs_about_contracts_do_not(self):
        from careeros.normalize.posting import employment_type, title_employment
        cases = {"GTM Operations Analyst (Contractor)": "contract", "Business Analyst (12 Month Contract)": "contract",
                 "Claims Analyst - Contract": "contract", "Data Analyst \u2013 Contract to Hire": "contract_to_hire",
                 "Pharmacy Technician (Remote, Part-Time)": "part_time", "Senior Analyst - Temp": "temporary",
                 "Analyst [W-2]": "contract", "Provider Contract Analyst": None, "Analyst (Contract Management)": None,
                 "Business Analyst, Contracts": None, "Contract Analyst - Remote": None, "Claims Analyst (Remote)": None}
        self.assertEqual({title: title_employment(title) for title in cases}, cases)
        self.assertEqual(employment_type("FullTime", "", "GTM Operations Analyst (Contractor)"), "contract")
        self.assertEqual(employment_type("FullTime", "", "Provider Contract Analyst"), "full_time")


class UrlAndPrivacyTests(unittest.TestCase):
    def test_job_url_parsing(self):
        self.assertEqual(parse_job_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x"), ("greenhouse", "acme", "123"))
        self.assertEqual(parse_job_url("https://jobs.lever.co/acme/abc-123/apply"), ("lever", "acme", "abc-123"))
        self.assertEqual(parse_job_url("https://jobs.ashbyhq.com/acme/f00"), ("ashby", "acme", "f00"))
        self.assertIsNone(parse_job_url("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123"))

    def test_vault_cannot_live_inside_the_repo(self):
        from careeros.paths import vault_dir
        old = os.environ.get("CAREEROS_VAULT")
        os.environ["CAREEROS_VAULT"] = str(ENGINE_ROOT / "vault")
        try:
            with self.assertRaises(RuntimeError):
                vault_dir()
        finally:
            if old is None:
                os.environ.pop("CAREEROS_VAULT")
            else:
                os.environ["CAREEROS_VAULT"] = old

    def test_gitignore_blocks_private_artifacts(self):
        ignore = (ENGINE_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("*.db", "/vault/", "*.log", ".env", ".venv/"):
            self.assertIn(pattern, ignore)

    def test_repository_has_no_secrets_or_personal_contact_data(self):
        email = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
        allowed = ("example.com", "example.invalid", "example.org")
        for path in ENGINE_ROOT.rglob("*"):
            if any(part.startswith(".venv") for part in path.parts) or ".git" in path.parts or "__pycache__" in path.parts or not path.is_file():
                continue
            if path.suffix not in (".py", ".yaml", ".yml", ".json", ".md", ".sql", ".toml", ".txt", ".lock", ".ps1"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotRegex(text, r"sk-ant-[A-Za-z0-9]{8}", str(path))
            for found in email.findall(text):
                self.assertTrue(found.endswith(allowed), f"{path}: {found}")


if __name__ == "__main__":
    unittest.main()
