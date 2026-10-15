"""Saved job-alert emails become leads; readable job-board links are graded. Synthetic emails only, no network."""
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

import httpx

from careeros.cli import _import_alert
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery.alerts import canonical_job_url, parse_eml
from careeros.discovery.http import HttpClient
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import gh_job
from tests.test_saved import POLICY

FIXTURES = Path(__file__).parent / "fixtures"
LI = "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D%3D&refId=xyz&lipi=urn&midToken=t"

ALERT_HTML = f"""<html><head><style>.a{{color:red}} Business Analyst</style></head><body>
<table><tr><td><a href="{LI}"><img src="logo.png"></a></td>
<td><a href="{LI}&trk=title">Claims Business Analyst</a><br>Synthetic Health · United States (Remote)</td></tr>
<tr><td><a href="{LI}&trk=button">View job</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099999999/?trackingId=q">Registered Nurse</a><br>Synth Hospital · Dallas, TX</td></tr>
<tr><td><a href="https://click.example.net/r?url=https%3A%2F%2Fjob-boards.greenhouse.io%2Fsynth%2Fjobs%2F555%3Fgh_src%3Dmail">Pharmacy Benefits Systems Analyst</a> Synthetic Health · Remote</td></tr>
<tr><td><a href="https://www.indeed.com/rc/clk/dl?jk=0a1b2c3d4e5f6a7b&from=ja&tk=zz">PBM Operations Analyst</a> Payer Co · Remote</td></tr>
<tr><td><a href="https://careers.northgategroup.com/job/123456/claims-analyst-remote/">Claims Analyst</a> Northgate · Remote</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/psettings/email-unsubscribe?x=1">Unsubscribe</a>
<a href="mailto:someone@example.com">Contact</a> <a href="javascript:alert(1)">Business Analyst</a></td></tr>
</table></body></html>"""


def alert_email(html_body=ALERT_HTML, text_body=None) -> bytes:
    message = EmailMessage()
    message["From"] = "LinkedIn Job Alerts <jobalerts@example.com>"
    message["To"] = "candidate@example.com"
    message["Subject"] = "Claims Business Analyst: Synthetic Health and 4 more"
    message["Date"] = "Sat, 10 Oct 2026 08:15:00 -0500"
    message.set_content(text_body or "plain part")
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return bytes(message)


class LinkTests(unittest.TestCase):
    def test_tracking_is_removed_and_job_links_recognised(self):
        self.assertEqual(canonical_job_url(LI), ("https://www.linkedin.com/jobs/view/4012345678", "linkedin", True))
        self.assertEqual(canonical_job_url("https://www.linkedin.com/jobs/search/?currentJobId=4011111111&keywords=x")[0],
                         "https://www.linkedin.com/jobs/view/4011111111")
        self.assertEqual(canonical_job_url("https://www.indeed.com/rc/clk/dl?jk=0a1b2c3d4e5f6a7b&from=ja"),
                         ("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "indeed", True))
        self.assertEqual(canonical_job_url("https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=1009"),
                         ("https://www.glassdoor.com/job-listing/j?jl=1009", "glassdoor", True))
        self.assertEqual(canonical_job_url("https://t.example/r?url=https%3A%2F%2Fjobs.lever.co%2Facme%2Fabc-1%3Flever-source%3Dx"),
                         ("https://jobs.lever.co/acme/abc-1", "lever", True))
        self.assertEqual(canonical_job_url("https://apply.workable.com/acme/j/94F9E30E22/"),
                         ("https://apply.workable.com/acme/j/94F9E30E22", "workable", True))
        self.assertFalse(canonical_job_url("https://www.linkedin.com/comm/psettings/email-unsubscribe?x=1")[2])
        self.assertIsNone(canonical_job_url("javascript:alert(1)"))
        self.assertIsNone(canonical_job_url("mailto:a@example.com"))


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: bytes, name="alert.eml") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_one_link_per_job_with_its_title_and_the_text_beside_it(self):
        alert = parse_eml(self.write(alert_email()))
        self.assertEqual(alert.sender, "LinkedIn Job Alerts <jobalerts@example.com>")
        self.assertEqual(alert.received_at, "2026-10-10T13:15:00.000Z")
        links = {l.url: l for l in alert.links}
        first = links["https://www.linkedin.com/jobs/view/4012345678"]
        self.assertEqual((first.text, first.site, first.job_like), ("Claims Business Analyst", "linkedin", True))
        self.assertIn("Synthetic Health · United States (Remote)", first.context)
        self.assertEqual(links["https://job-boards.greenhouse.io/synth/jobs/555"].text, "Pharmacy Benefits Systems Analyst")
        self.assertNotIn("javascript:alert(1)", links)
        self.assertNotIn("color:red", " ".join(l.text + l.context for l in alert.links))

    def test_plain_text_alerts_use_the_line_before_the_link(self):
        body = "New jobs for you\n\nPBM Claims Analyst\nPayer Co - Remote\nhttps://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b\n"
        alert = parse_eml(self.write(alert_email(html_body=None, text_body=body)))
        self.assertEqual([(l.url, l.text, l.context) for l in alert.links],
                         [("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "PBM Claims Analyst", "Payer Co - Remote")])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(self.tmp.name) / "snap")
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            if request.url.path == "/v1/boards/synth/jobs/555":
                return httpx.Response(200, json=gh_job(555, "Pharmacy Benefits Systems Analyst", postings.GOOD_FIT, req="R-55"))
            return httpx.Response(404)

        self.client = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def run_import(self, name="alert.eml", data=None):
        path = self.inbox / name
        path.write_bytes(data or alert_email())
        return _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")

    def test_readable_boards_are_graded_and_other_sites_are_only_listed(self):
        counts = self.run_import()
        # only the Greenhouse link was fetched; LinkedIn, Indeed and NHG were never contacted
        self.assertEqual(self.requests, ["https://boards-api.greenhouse.io/v1/boards/synth/jobs/555"])
        leads = {r["site"]: r for r in self.store.all("SELECT * FROM leads WHERE family IS NOT NULL")}
        self.assertEqual(sorted(leads), ["greenhouse", "indeed", "linkedin", "nhg"])
        self.assertEqual((leads["greenhouse"]["status"], leads["greenhouse"]["job_id"]), ("ingested", "greenhouse:synth:555"))
        self.assertEqual(self.store.one("SELECT title FROM jobs WHERE job_id = 'greenhouse:synth:555'")["title"],
                         "Pharmacy Benefits Systems Analyst")
        self.assertEqual((leads["linkedin"]["title"], leads["linkedin"]["status"]), ("Claims Business Analyst", "new"))
        nurse = self.store.one("SELECT * FROM leads WHERE title = 'Registered Nurse'")
        self.assertEqual((nurse["family"], nurse["status"]), (None, "new"))  # kept for audit, hidden from the review page
        self.assertEqual((counts["lead_ingested"], counts["lead_new"], counts["lead_other_family"]), (1, 3, 1))
        columns = {r["name"] for r in self.store.all("PRAGMA table_info(leads)")}
        self.assertNotIn("candidate@example.com", " ".join(str(v) for r in self.store.all("SELECT * FROM leads") for v in r))
        self.assertEqual(columns, {"lead_id", "url", "site", "title", "title_norm", "family", "context", "sender", "subject",
                                   "received_at", "first_seen_at", "status", "job_id", "profile"})
        self.assertEqual(list(self.inbox.glob("*.eml")), [])
        self.assertEqual(len(list((self.inbox / "imported").rglob("*.eml"))), 1)

    def test_the_same_alert_twice_adds_nothing(self):
        self.run_import()
        before = self.store.one("SELECT COUNT(*) c FROM leads")["c"]
        counts = self.run_import("again.eml")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], before)
        self.assertGreaterEqual(counts["lead_seen_before"], 4)

    def test_unreadable_email_is_set_aside_with_an_exception(self):
        counts = self.run_import("bad.eml", b"From: x\nContent-Type: application/pdf\n\n%PDF-1.4")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(len(list((self.inbox / "failed").rglob("*.eml"))), 1)
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "alert_email_unreadable")


if __name__ == "__main__":
    unittest.main()
