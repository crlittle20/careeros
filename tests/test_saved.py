"""Saved-page import and the review page's labeling path. Synthetic data only."""
import argparse
import html
import json
import re
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

import httpx

from careeros.cli import cmd_import
from careeros.core.clock import FixedClock
from careeros.core.policy import load_policy
from careeros.db.store import open_store
from careeros.discovery.saved import SavedPostingError, parent_for, parse_saved_html, parse_saved_text
from careeros.paths import CONFIG_DIR
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile
from careeros.review.server import make_handler

from tests import postings
from tests.helpers import JOB_MATRIX

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")


def saved_page(title="Business Systems Analyst - Northgate Rx", org="Northgate", remote=True, ident="2399999"):
    body = "".join(f"<p>{html.escape(line)}</p>" for line in postings.GOOD_FIT.split("\n") if line.strip())
    data = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "identifier": {"@type": "PropertyValue", "value": ident},
            "hiringOrganization": {"@type": "Organization", "name": org}, "datePosted": "2026-10-10",
            "employmentType": "FULL_TIME", "description": body,
            "jobLocation": {"@type": "Place", "address": {"addressLocality": "Eden Prairie", "addressRegion": "MN", "addressCountry": "US"}},
            "baseSalary": {"@type": "MonetaryAmount", "currency": "USD", "value": {"@type": "QuantitativeValue", "minValue": 71200, "maxValue": 127200, "unitText": "YEAR"}}}
    if remote:
        data["jobLocationType"] = "TELECOMMUTE"
    return (f'<html><head><link rel="canonical" href="https://careers.example.com/job/{ident}">'
            f'<script type="application/ld+json">{json.dumps(data)}</script></head><body>page</body></html>')


class SavedParsingTests(unittest.TestCase):
    def test_job_posting_data_is_read_from_a_saved_page(self):
        posting, employer = parse_saved_html(saved_page(), "x.html")
        self.assertEqual((posting.title, employer, posting.workplace_hint), ("Business Systems Analyst - Northgate Rx", "Northgate", "remote"))
        self.assertEqual(posting.comp, {"min": 71200.0, "max": 127200.0, "currency": "USD", "period": "year"})
        self.assertEqual(posting.requisition_id, "2399999")
        self.assertIn("3+ years of SQL experience required", posting.description_text)
        self.assertTrue(posting.location.startswith("Remote"))

    def test_parent_companies(self):
        self.assertEqual(parent_for("Northgate", POLICY), "Northgate Group")
        self.assertEqual(parent_for("Northgatecare", POLICY), "Northgate Group")
        self.assertEqual(parent_for("Caremark", POLICY), "CVS Health")
        self.assertEqual(parent_for("Synthetic Health", POLICY), "Synthetic Health")

    def test_text_format_and_errors(self):
        posting, employer = parse_saved_text("URL: https://example.com/j/1\nTitle: Claims Analyst\nEmployer: Northgate\n\n" + postings.GOOD_FIT, "a.txt")
        self.assertEqual((posting.title, employer), ("Claims Analyst", "Northgate"))
        with self.assertRaises(SavedPostingError):
            parse_saved_text("just some text", "b.txt")
        with self.assertRaises(SavedPostingError):
            parse_saved_html("<html>no structured data</html>", "c.html")


class ImportAndLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {"inbox": root / "inbox", "reports": root / "reports"}
        self.paths["inbox"].mkdir()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, root / "snapshots")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_flags_held_parent_and_moves_files(self):
        (self.paths["inbox"] / "northgate.html").write_text(saved_page(org="Northgate"), encoding="utf-8")
        (self.paths["inbox"] / "broken.html").write_text("<html>nothing</html>", encoding="utf-8")
        counts = cmd_import(self.ctx, self.paths, argparse.Namespace())
        self.assertEqual((counts["new"], counts["failed"]), (1, 1))
        job = self.store.one("SELECT * FROM jobs WHERE ats = 'manual'")
        self.assertEqual((job["parent"], job["remote_type"], job["current_state"]), ("Northgate Group", "remote", "scored"))
        gate = self.store.one("SELECT flags_json FROM gate_results WHERE job_id = ? ORDER BY at DESC LIMIT 1", (job["job_id"],))
        self.assertIn("held_d13", gate["flags_json"])  # Northgate resolves to Northgate Group, which is held
        self.assertEqual(list(self.paths["inbox"].glob("*.html")), [])  # nothing left behind, nothing deleted
        self.assertEqual(len(list((self.paths["inbox"] / "imported").rglob("*.html"))), 1)
        self.assertEqual(len(list((self.paths["inbox"] / "failed").rglob("*.html"))), 1)

    def test_review_page_labels_through_the_browser_form(self):
        (self.paths["inbox"] / "northgate.html").write_text(saved_page(), encoding="utf-8")
        cmd_import(self.ctx, self.paths, argparse.Namespace())
        token = "label-test-token"
        server = HTTPServer(("127.0.0.1", 0), make_handler(self.store, self.ctx.profile, POLICY, token,
                                                          Path(self.tmp.name) / "writer.lock", {}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            page = httpx.get(f"{base}/?t={token}").text
            job_id = re.search(r'name="target" value="([^"]+)"', page).group(1)
            key = re.search(r'name="key" value="([^"]+)"', page).group(1)
            form = {"target": job_id, "key": key, "label": "apply", "reason": "strong PBM fit"}
            first = httpx.post(f"{base}/label?t={token}", data=form)
            self.assertEqual(first.status_code, 303)
            self.assertEqual(httpx.post(f"{base}/label?t={token}", data=form).status_code, 303)  # double click: no duplicate
            labels = self.store.all("SELECT label, reason FROM labels WHERE job_id = ?", (html.unescape(job_id),))
            self.assertEqual([tuple(l) for l in labels], [("apply", "strong PBM fit")])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
