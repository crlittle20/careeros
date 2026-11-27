"""Lead resolution (the owner's decision D68, 2026-11-02; ADR 0003 and ADR 0001, amendments of that date).

A lead is a job seen only in an alert or a recommendation (LinkedIn, Indeed, any site CareerOS never reads). It reaches
its posting only through a board the daily scan admits, each lead with its own facts; a likely match waits for the
owner. Synthetic data only: invented employers, boards, postings and leads; every board answer comes from a local fake
and every web page from a fake web, so no test touches the network.
"""
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import timedelta
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx

from careeros import cli
from careeros.core.clock import FixedClock, parse_utc
from careeros.db.store import SCHEMA_DIR, Store, newest_schema
from careeros.discovery import board_discovery as bd
from careeros.discovery import board_registry as reg
from careeros.discovery import board_verifier as bv
from careeros.discovery import discovery_queue as dq
from careeros.discovery import lead_resolution as lr
from careeros.discovery.boards import greenhouse_posting
from careeros.pipeline import ingest
from careeros.review import leads as review_leads
from careeros.review.server import make_handler

from tests import test_board_verification as tbv
from tests.test_applications import forms
from tests.test_v25_audit import MID
from tests.test_v26_discovery import CLAIMS, POLICY, FakeATS, add_lead, client_for, gh_item, setup


def leads_command(ctx, action="list", target=None, **extra) -> str:
    """`careeros leads ...` as typed: its printed output (SystemExit for a refusal)."""
    args = SimpleNamespace(**{"action": action, "target": target, "job": None, "reason": None, "note": None, "all": False, **extra})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_leads(ctx, None, args)
    return out.getvalue()


def days_ago(store, days: float) -> str:
    return (parse_utc(store.clock.now()) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def legacy_board(store, name="Foo Health", token="foohealth", **extra):
    """A board the daily scan admits: scanned before verification existed (legacy), so in its grace."""
    store.writer("admin").insert("employers", {"employer_id": f"greenhouse:{token}", "name": name, "parent": name, "ats": "greenhouse",
                                               "board_token": token, "status": "verified", "admitted_via": "legacy", **extra})
    reg.sync_legacy(store)
    return store.one("SELECT * FROM employers WHERE employer_id = ?", (f"greenhouse:{token}",))


def lead_row(store, lead_id):
    return store.one("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))


def decisions(store, lead_id):
    return [r["decision"] for r in store.all("SELECT decision FROM lead_matches WHERE lead_id = ? ORDER BY decided_at, rowid", (lead_id,))]


def proposed(store, lead_id):
    """The job proposed to you as the lead's likely match (its latest decision is `likely`), or None. Since the owner's
    D188 (2026-11-19, "Exact links only") a title or a requisition only proposes a posting; the lead's own exact posting
    link alone links it."""
    row = store.one("SELECT decision, job_id FROM lead_matches WHERE lead_id = ? ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                    (lead_id,))
    return row["job_id"] if row is not None and row["decision"] == "likely" else None


# --- item 1: each lead is matched with its own title, link, requisition and location --------------------------------------
class OwnFactsTests(unittest.TestCase):
    def test_two_differently_titled_leads_at_one_employer_each_match_their_own_posting(self):
        """Gap 2: the discovery queue kept the first lead's facts and matched every later lead at the employer with them,
        so a second lead was linked to the first lead's job."""
        store, ctx = setup()
        fake = FakeATS()
        fake.greenhouse["foohealth"] = {"name": "Foo Health", "content": "Careers at foohealth.com", "jobs": [
            gh_item(41, "Claims Analyst", CLAIMS, "foohealth", req="C-41"),
            gh_item(42, "Eligibility Specialist", CLAIMS, "foohealth", req="E-42")]}
        add_lead(store, "lead:a", "Claims Analyst", "Foo Health · Remote · domain=foohealth.com",
                 url="https://www.linkedin.com/jobs/view/4000000041")
        add_lead(store, "lead:b", "Eligibility Specialist", "Foo Health · Remote",
                 url="https://www.indeed.com/viewjob?jk=00000000000000b2")
        self.assertEqual(dq.build_backfill(ctx, 90)["added"], 1)   # one employer, one queue row, two leads
        row = store.one("SELECT * FROM discovery_queue")
        self.assertEqual(sorted(json.loads(row["lead_ids_json"])), ["lead:a", "lead:b"])
        evidence = json.loads(row["evidence_json"])
        self.assertEqual({k: evidence.get(k) for k in ("title", "posting_url", "requisition_id", "location")},
                         {"title": None, "posting_url": None, "requisition_id": None, "location": None})
        client = client_for(fake)
        dq.run(ctx, client=client)
        client.close()
        matches = {r["lead_id"]: r for r in store.all("SELECT * FROM lead_matches")}
        self.assertEqual(json.loads(matches["lead:a"]["candidates_json"]), ["41"])
        self.assertEqual(json.loads(matches["lead:b"]["candidates_json"]), ["42"])
        self.assertEqual({lid: m["method"] for lid, m in matches.items()}, {"lead:a": "title_location", "lead:b": "title_location"})
        # discovery grades nothing (item 3): each posting waits for its board
        self.assertEqual({lid: m["decision"] for lid, m in matches.items()}, {"lead:a": "awaiting_board", "lead:b": "awaiting_board"})
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads WHERE job_id IS NOT NULL")["c"], 0)
        reg.approve(store, "greenhouse:foohealth", reason="its careers page links it")
        client = client_for(fake)
        self.assertEqual(bv.validate_board(ctx, "greenhouse:foohealth", client), "active")
        client.close()
        tbv.scan(ctx, fake.greenhouse["foohealth"]["jobs"])   # admitted and scanned: each lead proposed its own posting (D188)
        self.assertEqual({lid: proposed(store, lid) for lid in ("lead:a", "lead:b")},
                         {"lead:a": "greenhouse:foohealth:41", "lead:b": "greenhouse:foohealth:42"})
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads WHERE job_id IS NOT NULL")["c"], 0)

    def test_an_old_queue_row_holding_a_first_leads_facts_never_lends_them_to_another_lead(self):
        """A queue row written before 2026-11-02 still carries its first lead's title and link in evidence_json."""
        store, ctx = setup()
        fake = FakeATS()
        fake.greenhouse["foohealth"] = {"name": "Foo Health", "content": "Careers at foohealth.com", "jobs": [
            gh_item(51, "Claims Analyst", CLAIMS, "foohealth")]}
        add_lead(store, "lead:c", "Eligibility Specialist", "Foo Health · Remote · domain=foohealth.com")
        now = store.clock.now()
        store.writer("discovery").insert("discovery_queue", {
            "employer_norm": "foo health", "employer_name": "Foo Health", "official_domain": "foohealth.com", "board_url": None,
            "origin": "backfill", "priority": 9, "lead_ids_json": json.dumps(["lead:c"]), "state": "pending", "last_outcome": None,
            "next_attempt_at": None, "added_at": now, "updated_at": now,
            "evidence_json": json.dumps({"domain": "foohealth.com", "title": "Claims Analyst",
                                         "posting_url": "https://job-boards.greenhouse.io/foohealth/jobs/51"})})
        client = client_for(fake)
        dq.run(ctx, client=client)
        client.close()
        match = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:c'")
        self.assertEqual((match["decision"], match["job_id"]), ("no_match", None))   # its own title matches nothing there
        self.assertIsNone(store.one("SELECT job_id FROM leads WHERE lead_id = 'lead:c'")["job_id"])


# --- item 2: matched again after every scan, and when a board is admitted ------------------------------------------------------
class RematchTests(unittest.TestCase):
    def test_a_lead_whose_job_is_scanned_later_is_linked_after_that_scan(self):
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:later", "Eligibility Specialist", "Foo Health · Remote (US)")
        tbv.scan(ctx, [gh_item(61, "Claims Analyst", CLAIMS, "foohealth")])
        lead = lead_row(store, "lead:later")
        self.assertEqual((lead["status"], lead["job_id"]), ("no_match", None))   # a complete scan, nothing matching yet
        self.assertIn("no open posting at Foo Health matches", lead["status_reason"])
        from careeros.review.report import build_report
        self.assertIn("no match yet", build_report(store, MID, ctx.policy))   # still listed with the leads, with its reason
        tbv.scan(ctx, [gh_item(61, "Claims Analyst", CLAIMS, "foohealth")])
        self.assertEqual(decisions(store, "lead:later"), ["no_match"])   # the same decision is not written again
        store.clock.advance(days=2)
        counts = tbv.scan(ctx, [gh_item(61, "Claims Analyst", CLAIMS, "foohealth"),
                                gh_item(62, "Eligibility Specialist", CLAIMS, "foohealth")])
        lead = lead_row(store, "lead:later")
        self.assertEqual((lead["status"], lead["job_id"]), ("new", None))   # proposed to you, never linked by its title (D188)
        self.assertEqual(counts["leads_likely"], 1)
        self.assertEqual(decisions(store, "lead:later"), ["no_match", "likely"])
        match = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:later' AND decision = 'likely'")
        self.assertEqual((match["method"], match["job_id"], match["actor"]), ("title_location", "greenhouse:foohealth:62", "careeros"))
        self.assertFalse(store.one("SELECT 1 FROM events WHERE event_type = 'lead_linked' AND entity_id = 'lead:later'"))
        self.assertEqual(store.verify_event_chain(), [])

    def test_each_open_lead_is_matched_with_its_own_facts_and_only_at_its_own_employer(self):
        store, ctx = setup()
        legacy_board(store)
        legacy_board(store, "Bar Health", "barhealth")
        add_lead(store, "lead:req", "Claims Analyst", "Foo Health, Inc. · Remote · req=C-71")   # a legal suffix: the same employer
        add_lead(store, "lead:title", "Eligibility Specialist", "Foo Health · Remote")
        add_lead(store, "lead:other", "Payment Integrity Analyst", "Bar Health · Remote")
        add_lead(store, "lead:agency", "Claims Analyst", "Insight Global · Remote")   # an agency: its client is never guessed
        add_lead(store, "lead:secret", "Claims Analyst", "Confidential · Remote")
        add_lead(store, "lead:unknown", "Claims Analyst", "Zeta Corp · Remote")
        add_lead(store, "lead:via", "Claims Analyst", "Foo Health · Remote · agency=Harbor Staffing")   # an agency's posting
        add_lead(store, "lead:link", "Clms Analyst - Pharmacy", "Foo Health · Remote",   # the board's own link, tracking aside
                 url="https://job-boards.greenhouse.io/foohealth/jobs/73?utm_source=alert")
        tbv.scan(ctx, lambda token: [gh_item(71, "Claims Analyst", CLAIMS, token, req="C-71"),
                                     gh_item(72, "Eligibility Specialist", CLAIMS, token, req="E-72"),
                                     gh_item(73, "Pharmacy Claims Analyst", CLAIMS, token)] if token == "foohealth" else
                 [gh_item(81, "Claims Analyst", CLAIMS, token, req="C-71")])
        linked = {r["lead_id"]: (r["status"], r["job_id"], proposed(store, r["lead_id"])) for r in store.all("SELECT * FROM leads")}
        self.assertEqual(linked, {"lead:req": ("new", None, "greenhouse:foohealth:71"),   # proposed only (D188)
                                  "lead:title": ("new", None, "greenhouse:foohealth:72"),
                                  "lead:other": ("no_match", None, None), "lead:agency": ("new", None, None),
                                  "lead:secret": ("new", None, None), "lead:unknown": ("new", None, None), "lead:via": ("new", None, None),
                                  "lead:link": ("ingested", "greenhouse:foohealth:73", None)})   # its own link: linked
        methods = {r["lead_id"]: (r["decision"], r["method"])
                   for r in store.all("SELECT lead_id, decision, method FROM lead_matches WHERE decision IN ('matched', 'likely')")}
        self.assertEqual(methods, {"lead:req": ("likely", "requisition"), "lead:title": ("likely", "title_location"),
                                   "lead:link": ("matched", "canonical_link")})

    def test_a_resolver_kept_across_a_scan_stops_matching_when_an_agency_setting_goes_missing(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.discovery.alerts import ENROLLED
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:req", "Claims Analyst", "Foo Health · Remote · req=C-71", url="https://job-boards.greenhouse.io/foohealth/jobs/71")   # its own link
        kept = lr.Resolver(ctx)
        self.assertIn("lead:req", kept.open_leads())   # read before the stop
        missing = ENROLLED + "click.other-agency.example"   # a host set up by another run, not in these settings: a stop
        store.writer("admin").insert("meta", {"key": missing, "value": store.clock.now()})   # the settings file unchanged
        tbv.scan(ctx, [gh_item(71, "Claims Analyst", CLAIMS, "foohealth", req="C-71")])
        self.assertEqual(dict(kept.rematch(store.one("SELECT * FROM employers"), complete=True, why="a kept resolver")), {})
        self.assertEqual((lead_row(store, "lead:req")["status"], lead_row(store, "lead:req")["job_id"]), ("new", None))
        with mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # the agency's entry missing as well (CR-AD-V2-12)
            self.assertEqual(dict(kept.rematch(store.one("SELECT * FROM employers"), complete=True, why="still stopped")), {})
        store.conn.execute("DELETE FROM meta WHERE key = ?", (missing,))   # the stop resolved
        restored = kept.rematch(store.one("SELECT * FROM employers"), complete=True, why="the entry restored")
        self.assertEqual(restored["leads_linked"], 1)

    def test_a_stop_found_part_way_through_a_batch_leaves_the_rest_of_it_unmatched(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.discovery.alerts import enroll_agency_hosts
        for within in ("outside a run", "inside a run"):   # the file read as it is now, or the run's reading kept
            with self.subTest(within):
                store, ctx = setup()
                legacy_board(store)
                add_lead(store, "lead:a", "Claims Analyst", "Foo Health · Remote · req=C-71", url="https://job-boards.greenhouse.io/foohealth/jobs/71")   # their own links
                add_lead(store, "lead:b", "Eligibility Specialist", "Foo Health · Remote · req=E-72", url="https://job-boards.greenhouse.io/foohealth/jobs/72")
                enroll_agency_hosts(store)   # the suite's synthetic agency, set up
                real_link, linked, removed = lr.link, [], mock.patch.dict(os.environ, {private_sites.ENV: ""})

                def link_then_removed(*args, linked=linked, removed=removed, real_link=real_link, **kwargs):
                    real_link(*args, **kwargs)
                    linked.append(args[1])
                    if len(linked) == 1:
                        removed.start()   # the agency's entry removed right after the first link (Codex Pass 3, CR-AD-V2-12)

                run = private_sites.run() if within.startswith("inside") else contextlib.nullcontext()
                ended = (self.assertRaises(tbv.cli.CommandStopped) if within.startswith("inside")   # D153 R2: the run ends
                         else contextlib.nullcontext())
                try:
                    with run, mock.patch.object(lr, "link", link_then_removed), ended:
                        tbv.scan(ctx, [gh_item(71, "Claims Analyst", CLAIMS, "foohealth", req="C-71"),
                                       gh_item(72, "Eligibility Specialist", CLAIMS, "foohealth", req="E-72")])
                finally:
                    if linked:
                        removed.stop()
                self.assertEqual(len(linked), 1)
                statuses = sorted(lead_row(store, lead)["status"] for lead in ("lead:a", "lead:b"))
                self.assertEqual(statuses, ["ingested", "new"])   # the second lead is never matched once the stop is found

    def test_a_pasted_posting_links_no_more_leads_once_the_settings_change_part_way(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.discovery.alerts import enroll_agency_hosts
        from careeros.discovery.manual_intake import intake
        store, ctx = setup()
        add_lead(store, "lead:a", "Claims Analyst", "Foo Health · Remote", url="https://www.linkedin.com/jobs/view/4000000901")
        add_lead(store, "lead:b", "Claims Analyst", "Foo Health · Remote", url="https://www.indeed.com/viewjob?jk=00000000000902")
        enroll_agency_hosts(store)   # the suite's synthetic agency, set up
        real_link, linked, removed = lr.link, [], mock.patch.dict(os.environ, {private_sites.ENV: ""})

        def link_then_removed(*args, **kwargs):
            done = real_link(*args, **kwargs)
            linked.append(args[1])
            if len(linked) == 1:
                removed.start()   # the agency's entry removed right after the first link (the red team before Pass 4)
            return done

        try:
            with private_sites.run():
                job = intake(ctx, "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n" + CLAIMS,
                             "https://www.linkedin.com/jobs/view/4000000901")   # lead:a's own posting (D188)
                with mock.patch.object(lr, "link", link_then_removed):
                    found = lr.link_same_posting(ctx, job["job_id"], via="pasted")
        finally:
            if linked:
                removed.stop()
        self.assertEqual((len(linked), found["leads_stopped"]), (1, 1))

    def test_no_lead_domain_is_used_once_the_settings_change_part_way(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.discovery.alerts import enroll_agency_hosts
        store, ctx = setup()
        add_lead(store, "lead:a", "Claims Analyst", "Alpha Health · Remote · domain=alphahealth.example")
        add_lead(store, "lead:b", "Claims Analyst", "Beta Health · Remote · domain=betahealth.example")
        enroll_agency_hosts(store)
        real_hint, hinted, removed = reg.apply_domain_hint, [], mock.patch.dict(os.environ, {private_sites.ENV: ""})

        def hint_then_removed(*args, **kwargs):
            done = real_hint(*args, **kwargs)
            hinted.append(args[1])
            if len(hinted) == 1:
                removed.start()   # the red team before Pass 4
            return done

        try:
            with mock.patch.object(reg, "apply_domain_hint", hint_then_removed):
                counts = lr.lead_domains(ctx, store.clock.now())["counts"]
        finally:
            if hinted:
                removed.stop()
        self.assertEqual((len(hinted), counts["lead_domain_stopped"]), (1, 1))

    def test_a_board_the_scan_does_not_admit_never_resolves_a_lead(self):
        store, ctx = setup()
        row = cli._add_employer(ctx, "Baz Care", "greenhouse", "bazcare")   # e.g. from a pasted job link: a candidate
        reg.sync_legacy(store)
        ingest(ctx, row, [greenhouse_posting("bazcare", gh_item(91, "Claims Analyst", CLAIMS, "bazcare", req="B-91"))],
               source="manual_url", full_board=False)
        add_lead(store, "lead:baz", "Claims Analyst", "Baz Care · Remote · req=B-91")
        employer = store.one("SELECT * FROM employers")
        self.assertFalse(reg.scan_gate(store, employer, ctx.policy)[0])
        self.assertEqual(lr.after_scan(ctx, employer, complete=True), {})
        self.assertEqual(lr.after_admission(ctx, "greenhouse:bazcare"), {})
        self.assertEqual((lead_row(store, "lead:baz")["status"], decisions(store, "lead:baz")), ("new", []))

    def test_an_incomplete_scan_never_sets_no_match_or_closed(self):
        store, ctx = setup()
        legacy_board(store, last_posting_count=20)
        add_lead(store, "lead:old", "Eligibility Specialist", "Foo Health · Remote", first_seen=days_ago(store, 60))
        tbv.scan(ctx, [gh_item(61, "Claims Analyst", CLAIMS, "foohealth")])   # 20 -> 1 posting: suspicious
        self.assertEqual((lead_row(store, "lead:old")["status"], decisions(store, "lead:old")), ("new", []))
        tbv.scan(ctx, [gh_item(60 + n, f"Claims Analyst {n}", CLAIMS, "foohealth") for n in range(10)])   # 10 of 20: complete
        lead = lead_row(store, "lead:old")
        self.assertEqual((lead["status"], decisions(store, "lead:old")), ("closed", ["posting_closed"]))
        self.assertIn("likely closed", lead["status_reason"])
        tbv.scan(ctx, [gh_item(61, "Claims Analyst", CLAIMS, "foohealth"), gh_item(62, "Eligibility Specialist", CLAIMS, "foohealth")])
        self.assertEqual(lead_row(store, "lead:old")["status"], "closed")   # closed: no longer matched automatically

    def test_when_a_board_is_admitted_its_employers_open_leads_are_matched_against_the_jobs_held(self):
        store, ctx = tbv.setup()
        _web, boards = tbv.example_world()
        held = tbv.gh_job(101, "Claims Analyst", tbv.EX_TEXT, "examplehealth")
        row = cli._add_employer(ctx, tbv.EX, "greenhouse", "examplehealth")   # a job added from its link before admission
        reg.sync_legacy(store)
        ingest(ctx, row, [greenhouse_posting("examplehealth", held)], source="manual_url", full_board=False)
        add_lead(store, "lead:ex", "Claims Analyst", f"{tbv.EX} · Remote · req=R-101")
        mapping_id = store.one("SELECT mapping_id FROM board_mappings")["mapping_id"]
        self.assertEqual(lr.after_admission(ctx, mapping_id), {})   # not admitted yet: nothing
        reg.update_board(store, mapping_id, {"verification_status": "quarantined", "quarantined_at": days_ago(store, 1),
                                             "validation_status": "pending"})
        client = boards.client()
        self.assertEqual(bv.validate_board(ctx, mapping_id, client), "active")
        client.close()
        lead = lead_row(store, "lead:ex")
        self.assertEqual((lead["status"], lead["job_id"], proposed(store, "lead:ex")), ("new", None, "greenhouse:examplehealth:101"))
        self.assertIn("admitted", lead["status_reason"])

    def test_when_you_activate_or_approve_a_board_its_employers_open_leads_are_matched(self):
        from tests.test_v21_policy import policy_with
        for action in ("activate", "approve"):
            with self.subTest(action=action):
                store, ctx = tbv.setup(policy_with(board_verification__fetch_official_sites=True, source_discovery__auto_activate=False))
                _web, boards = tbv.example_world()
                row = cli._add_employer(ctx, tbv.EX, "greenhouse", "examplehealth")
                reg.sync_legacy(store)
                ingest(ctx, row, [greenhouse_posting("examplehealth", tbv.gh_job(101, "Claims Analyst", tbv.EX_TEXT, "examplehealth"))],
                       source="manual_url", full_board=False)
                add_lead(store, "lead:on", "Claims Analyst", f"{tbv.EX} · Remote · req=R-101")
                mapping_id = store.one("SELECT mapping_id FROM board_mappings")["mapping_id"]
                reg.update_board(store, mapping_id, {"verification_status": "quarantined", "quarantined_at": days_ago(store, 1),
                                                     "validation_status": "pending"})
                client = boards.client()
                self.assertEqual(bv.validate_board(ctx, mapping_id, client), "validated")   # waits for you to activate it
                client.close()
                self.assertIsNone(lead_row(store, "lead:on")["job_id"])
                args = SimpleNamespace(action=action, target=mapping_id, reason="its careers page links it", note=None, days=None,
                                       domain=None)
                if action == "approve":   # your approval of a board that already passed validation admits it at once
                    reg.update_board(store, mapping_id, {"verification_status": "review_required"})
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_sources(ctx, None, args)
                self.assertEqual(proposed(store, "lead:on"), "greenhouse:examplehealth:101")   # proposed to you (D188)


# --- item 4: a likely match is a candidate you confirm or reject, never linked by itself -----------------------------------------
class LikelyMatchTests(unittest.TestCase):
    def test_the_matcher_reads_filler_seniority_location_and_requisitions(self):
        near = bd.near_title
        self.assertEqual(near("Claims Analyst II - Remote (US)"), "claims analyst")
        self.assertEqual(near("Work From Home Claims Analyst, Level 2"), "claims analyst")
        self.assertEqual(near("Home Health Claims Analyst"), "home health claims analyst")   # only the phrase is filler
        post = lambda loc, remote="unknown": bd.TrackedPosting("j", "t", None, None, None, loc, remote)
        compatible = bd.location_compatible
        self.assertTrue(compatible("Remote (US)", post("United States", "remote")))
        self.assertTrue(compatible("", post("Austin, TX", "onsite")))                  # unknown on one side
        self.assertTrue(compatible("source=linkedin_alert", post("Austin, TX", "onsite")))   # a Note field is no location
        self.assertTrue(compatible("Chicago, IL", post("Remote - US", "remote")))
        self.assertTrue(compatible("Austin, Texas", post("Austin, TX", "onsite")))
        self.assertFalse(compatible("Remote (US)", post("Austin, TX", "onsite")))
        self.assertFalse(compatible("Remote", post("Denver, CO (Hybrid)", "hybrid")))
        self.assertFalse(compatible("Chicago, IL", post("Austin, TX", "onsite")))
        self.assertFalse(compatible("Toronto, Canada", post("Remote - United States", "remote")))
        facts = bd.Candidate(employer_name="Foo Health", origin="alert", title="Claims Analyst", requisition_id="R-1", location="Remote")
        other_req = bd.TrackedPosting("j2", "Claims Analyst", None, "R-2", "2", "Remote - US", "remote")
        self.assertEqual(bd.match_lead(facts, [other_req], POLICY, "2026-11-02T12:00:00.000Z")["decision"], "no_match")   # another vacancy

    def test_a_near_identical_title_is_a_likely_match_linked_only_when_you_confirm(self):
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:l", "Claims Analyst II (Remote)", "Foo Health · Remote (US)")
        counts = tbv.scan(ctx, [gh_item(201, "Claims Analyst II", CLAIMS, "foohealth"), gh_item(202, "Eligibility Specialist", CLAIMS, "foohealth")])
        self.assertEqual(counts["leads_likely"], 1)
        lead = lead_row(store, "lead:l")
        self.assertEqual((lead["status"], lead["job_id"]), ("new", None))   # never linked by itself
        self.assertIn("likely match", lead["status_reason"])
        match = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:l'")
        self.assertEqual((match["decision"], match["method"], match["job_id"], match["actor"]),
                         ("likely", "likely_title", "greenhouse:foohealth:201", "careeros"))
        tbv.scan(ctx, [gh_item(201, "Claims Analyst II", CLAIMS, "foohealth"), gh_item(202, "Eligibility Specialist", CLAIMS, "foohealth")])
        self.assertEqual(decisions(store, "lead:l"), ["likely"])   # proposed once
        listing = leads_command(ctx)
        self.assertIn("LIKELY MATCH: greenhouse:foohealth:201", listing)
        self.assertIn("careeros leads confirm lead:l --job greenhouse:foohealth:201", listing)
        with self.assertRaises(SystemExit) as refused:   # only the job the waiting match names
            leads_command(ctx, "confirm", "lead:l", job="greenhouse:foohealth:202")
        self.assertIn("not_the_likely_match", str(refused.exception))
        leads_command(ctx, "confirm", "lead:l", job="greenhouse:foohealth:201", note="the same posting on the employer's site")
        lead = lead_row(store, "lead:l")
        self.assertEqual((lead["status"], lead["job_id"]), ("ingested", "greenhouse:foohealth:201"))
        confirmed = store.one("SELECT * FROM lead_matches WHERE decision = 'confirmed'")
        self.assertEqual((confirmed["method"], confirmed["actor"], confirmed["job_id"]), ("owner", "user", "greenhouse:foohealth:201"))
        self.assertIn("the same posting on the employer's site", confirmed["reason"])
        event = store.one("SELECT * FROM events WHERE event_type = 'lead_linked' AND entity_id = 'lead:l'")
        self.assertEqual((event["actor_type"], event["actor_id"]), ("human", "user"))
        with self.assertRaises(ValueError):
            review_leads.confirm_likely(store, "lead:l", policy=ctx.policy, job_id="greenhouse:foohealth:201", key="k-again")   # nothing waits any more
        self.assertEqual(store.verify_event_chain(), [])

    def test_a_rejected_likely_match_is_never_proposed_or_linked_for_that_lead_again(self):
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:r", "Remote Claims Analyst", "Foo Health · Remote")
        items = [gh_item(211, "Claims Analyst - US", CLAIMS, "foohealth")]
        tbv.scan(ctx, items)
        self.assertEqual(review_leads.reject_likely(store, "lead:r", policy=ctx.policy, job_id="greenhouse:foohealth:211", reason="another team", key="k-r"),
                         "new")
        self.assertEqual(review_leads.reject_likely(store, "lead:r", policy=ctx.policy, job_id="greenhouse:foohealth:211", reason="another team", key="k-r"),
                         "replay")
        self.assertIsNone(review_leads.pending(store, "lead:r", ctx.policy))
        tbv.scan(ctx, items)
        tbv.scan(ctx, items)
        self.assertEqual(decisions(store, "lead:r"), ["likely", "rejected", "no_match"])
        self.assertEqual((lead_row(store, "lead:r")["status"], lead_row(store, "lead:r")["job_id"]), ("no_match", None))
        self.assertTrue(store.one("SELECT 1 FROM events WHERE event_type = 'lead_match_rejected' AND actor_type = 'human'"))

    def test_two_near_identical_postings_stay_ambiguous_and_other_differences_are_no_match(self):
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:two", "Remote Claims Analyst", "Foo Health · Remote")
        add_lead(store, "lead:senior", "Claims Specialist", "Foo Health · Remote")
        add_lead(store, "lead:place", "Payment Integrity Analyst", "Foo Health · Remote (US)")
        add_lead(store, "lead:city", "Appeals Coordinator", "Foo Health · Chicago, IL")
        add_lead(store, "lead:level", "Appeals Analyst I", "Foo Health · Remote")   # entry level; the posting is mid level
        tbv.scan(ctx, [gh_item(221, "Claims Analyst - US", CLAIMS, "foohealth"), gh_item(222, "Claims Analyst (Work from home)", CLAIMS, "foohealth"),
                       gh_item(223, "Senior Claims Specialist", CLAIMS, "foohealth"),
                       gh_item(224, "Payment Integrity Analyst", CLAIMS.replace("remote in the US", "on site in Austin"), "foohealth",
                               location="Austin, TX"),
                       gh_item(225, "Appeals Coordinator", CLAIMS.replace("remote in the US", "on site in Austin"), "foohealth",
                               location="Austin, TX"),
                       gh_item(226, "Appeals Analyst II", CLAIMS, "foohealth")])
        two = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:two'")
        self.assertEqual((two["decision"], two["method"], sorted(json.loads(two["candidates_json"]))),
                         ("ambiguous", "likely_title", ["greenhouse:foohealth:221", "greenhouse:foohealth:222"]))
        self.assertEqual({lid: lead_row(store, lid)["status"] for lid in ("lead:two", "lead:senior", "lead:place", "lead:city", "lead:level")},
                         {"lead:two": "new", "lead:senior": "no_match", "lead:place": "no_match", "lead:city": "no_match",
                          "lead:level": "no_match"})
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads WHERE job_id IS NOT NULL")["c"], 0)

    def test_the_review_page_confirms_only_the_likely_match_its_card_showed(self):
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:p", "Claims Analyst II (Remote)", "Foo Health · Remote (US)")
        add_lead(store, "lead:q", "Remote Claims Specialist", "Foo Health · Remote (US)")
        tbv.scan(ctx, [gh_item(231, "Claims Analyst II", CLAIMS, "foohealth"), gh_item(232, "Claims Specialist - US", CLAIMS, "foohealth")])
        token = "lead-test-token"
        with tempfile.TemporaryDirectory() as tmp:
            server = HTTPServer(("127.0.0.1", 0), make_handler(store, MID, POLICY, token, Path(tmp) / "writer.lock", {}))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                page = httpx.get(f"{base}/?t={token}").text
                self.assertIn("Likely matches for your leads (2)", page)
                found = {(a, t): k for a, t, k in forms(page) if a in ("lead_confirm", "lead_reject")}
                self.assertEqual(set(found), {("lead_confirm", "lead:p|greenhouse:foohealth:231"), ("lead_reject", "lead:p|greenhouse:foohealth:231"),
                                              ("lead_confirm", "lead:q|greenhouse:foohealth:232"), ("lead_reject", "lead:q|greenhouse:foohealth:232")})
                reply = httpx.post(f"{base}/lead_confirm?t={token}", data={"target": "lead:p|greenhouse:foohealth:231",
                                                                            "key": found[("lead_confirm", "lead:p|greenhouse:foohealth:231")]})
                self.assertEqual(reply.status_code, 303)
                self.assertEqual(lead_row(store, "lead:p")["job_id"], "greenhouse:foohealth:231")
                review_leads.reject_likely(store, "lead:q", policy=ctx.policy, job_id="greenhouse:foohealth:232", key="cli-reject")   # decided elsewhere: the card is stale,
                tbv.scan(ctx, [gh_item(231, "Claims Analyst II", CLAIMS, "foohealth"),   # and another likely match now waits
                               gh_item(232, "Claims Specialist - US", CLAIMS, "foohealth"),
                               gh_item(233, "Claims Specialist (Remote)", CLAIMS, "foohealth")])
                self.assertEqual(review_leads.pending(store, "lead:q", ctx.policy)["job_id"], "greenhouse:foohealth:233")
                stale = httpx.post(f"{base}/lead_confirm?t={token}", data={"target": "lead:q|greenhouse:foohealth:232",
                                                                            "key": found[("lead_confirm", "lead:q|greenhouse:foohealth:232")]})
                self.assertEqual(stale.status_code, 400)
                self.assertIsNone(lead_row(store, "lead:q")["job_id"])
                forged = httpx.post(f"{base}/lead_confirm?t={token}", data={"target": "lead:q|greenhouse:foohealth:231",
                                                                             "key": found[("lead_reject", "lead:q|greenhouse:foohealth:232")]})
                self.assertEqual(forged.status_code, 400)   # a key is bound to its action and to the lead and job it showed
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(decisions(store, "lead:q"), ["likely", "rejected", "likely"])   # the new one still waits for you


# --- item 5: a failed fetch keeps its reason, the same posting pasted or added links its lead, unlisted Ashby postings ---------------
class IntakeAndFetchTests(unittest.TestCase):
    ASHBY_LISTED = "aaaaaaaa-1111-4111-8111-000000000001"
    ASHBY_UNLISTED = "bbbbbbbb-2222-4222-8222-000000000002"

    def test_an_unlisted_ashby_posting_is_read_by_its_link_but_never_listed(self):
        from careeros.discovery.alerts import AlertEmail, Link
        from careeros.discovery.boards import fetch_board, fetch_single
        from careeros.discovery.http import FetchError
        store, ctx = tbv.setup()
        boards = tbv.FakeBoards()
        hidden = {**tbv.ashby_job(self.ASHBY_UNLISTED, "Claims Analyst", tbv.EX_TEXT, "foohealth"), "isListed": False}
        boards.ashby["foohealth"] = [tbv.ashby_job(self.ASHBY_LISTED, "Eligibility Analyst", tbv.EX_TEXT, "foohealth"), hidden]
        client = boards.client()
        try:
            self.assertEqual([p.external_id for p in fetch_board(client, "ashby", "foohealth")], [self.ASHBY_LISTED])
            self.assertEqual(fetch_single(client, "ashby", "foohealth", self.ASHBY_UNLISTED).title, "Claims Analyst")
            with self.assertRaises(FetchError) as missing:
                fetch_single(client, "ashby", "foohealth", "cccccccc-3333-4333-8333-000000000003")
            self.assertEqual(missing.exception.code, "not_found")
            url = f"https://jobs.ashbyhq.com/foohealth/{self.ASHBY_UNLISTED}"
            alert = AlertEmail("GitHub inbox (owner-x)", "jobs", None, [Link(url, "ashby", "Claims Analyst", "Foo Health · Remote", True)])
            counts = cli._record_alert(ctx, alert, client)
        finally:
            client.close()
        self.assertEqual(counts["lead_ingested"], 1)
        self.assertEqual(store.one("SELECT status, job_id FROM leads")["job_id"], f"ashby:foohealth:{self.ASHBY_UNLISTED}")

    def test_a_lead_whose_link_cannot_be_read_keeps_the_reason(self):
        from careeros.discovery.alerts import AlertEmail, Link
        from careeros.review.report import build_report
        store, ctx = tbv.setup()
        boards = tbv.FakeBoards()   # no such Lever posting: 404
        url = "https://jobs.lever.co/foohealth/dddddddd-4444-4444-8444-000000000004"
        alert = AlertEmail("GitHub inbox (owner-x)", "jobs", None, [Link(url, "lever", "Claims Analyst", "Foo Health · Remote", True)])
        client = boards.client()
        try:
            counts = cli._record_alert(ctx, alert, client)
        finally:
            client.close()
        self.assertEqual(counts["lead_fetch_failed"], 1)
        lead = store.one("SELECT * FROM leads")
        self.assertEqual((lead["status"], lead["job_id"]), ("new", None))
        self.assertEqual(lead["fetch_failure"], f"its own link: not_found ({store.clock.now()[:10]})")
        self.assertIn("its own link: not_found", leads_command(ctx))
        self.assertIn("could not be read: its own link: not_found", build_report(store, MID, ctx.policy))

    def test_pasting_or_adding_the_same_posting_links_its_lead(self):
        from careeros.paths import vault_paths
        store, ctx = setup()
        linkedin = "https://www.linkedin.com/jobs/view/4000000301"
        add_lead(store, "lead:link", "Claims Analyst", "Foo Health · Remote", url=linkedin)
        add_lead(store, "lead:req", "Eligibility Specialist", "Foo Health · Remote · req=E-302",
                 url="https://www.indeed.com/viewjob?jk=0000000000000302")
        add_lead(store, "lead:bar", "Eligibility Specialist", "Bar Health · Remote", url="https://www.indeed.com/viewjob?jk=bar")
        add_lead(store, "lead:gone", "Claims Analyst", "Foo Health · Remote", url=linkedin + "?trk=x")
        store.writer("review").update("leads", {"status": "dismissed"}, "lead_id = 'lead:gone'")
        add_lead(store, "lead:added", "Payment Integrity Analyst", "Foo Health · Remote")
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            pasted = Path(tmp) / "posting.txt"
            pasted.write_text("Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n" + CLAIMS, encoding="utf-8")
            args = SimpleNamespace(file=str(pasted), clipboard=False, url=linkedin, company=None, title=None, location=None)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_paste(ctx, paths, args)
            pasted.write_text("Eligibility Specialist\nCompany: Foo Health\nLocation: Remote - US\nJob ID: E-302\n\n" + CLAIMS,
                              encoding="utf-8")
            args.url = "https://foohealth.example/careers/302"
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_paste(ctx, paths, args)
            item = gh_item(303, "Payment Integrity Analyst", CLAIMS, "foohealth")
            with contextlib.redirect_stdout(io.StringIO()), \
                    unittest.mock.patch.object(cli, "fetch_single", lambda c, ats, token, job: greenhouse_posting(token, item)):
                cli.cmd_add_url(ctx, paths, SimpleNamespace(url="https://job-boards.greenhouse.io/foohealth/jobs/303", text_file=None,
                                                            employer="Foo Health", parent=None, title=None, location=None))
        rows = {r["lead_id"]: r for r in store.all("SELECT * FROM leads")}
        self.assertEqual({lid: (r["status"], r["job_id"] is not None) for lid, r in rows.items()},
                         {"lead:link": ("ingested", True), "lead:req": ("new", False), "lead:bar": ("new", False),
                          "lead:gone": ("dismissed", False), "lead:added": ("new", False)})   # D188: only the same link links
        self.assertEqual(proposed(store, "lead:added"), "greenhouse:foohealth:303")   # its title proposes the posting
        self.assertTrue(proposed(store, "lead:req"))   # and its requisition the pasted one
        self.assertIn("the same link", rows["lead:link"]["status_reason"])
        self.assertIn("the same requisition", rows["lead:req"]["status_reason"])
        self.assertEqual({r["method"] for r in store.all("SELECT method FROM lead_matches WHERE decision = 'matched'")}, {"manual_intake"})

    def test_a_colliding_saved_import_retried_reuses_its_own_job_and_links_its_lead(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 9, H11: re-importing a saved page that collided with another employer's
        generated id made a new suffixed row each time, then looped forever on the ninth; the new rows had no possible
        duplicate link, and the CLI linked leads to the generated id, not the job written."""
        import json as _json
        from unittest import mock

        from careeros.discovery import manual_intake
        from careeros.discovery.saved import parse_saved_html
        from careeros.paths import vault_paths

        def page(employer, url, body=CLAIMS):
            data = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Claims Analyst", "identifier": "R-4001",
                    "hiringOrganization": {"@type": "Organization", "name": employer}, "url": url,
                    "description": "<p>" + body.replace("\n", "</p><p>") + "</p>"}
            return '<html><head><script type="application/ld+json">' + _json.dumps(data) + "</script></head></html>"
        store, ctx = setup()
        # a board's posting link: an employer's careers page names no posting, so no lead links there by its link (D184)
        second = "https://abhealth.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-40011"
        add_lead(store, "lead:second", "Claims Analyst", "A B Health · Remote", url=second)
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            paths["inbox"].mkdir(parents=True, exist_ok=True)
            (paths["inbox"] / "first.html").write_text(page("A+B Health", "https://careers.ab-health.example/jobs/4001"),
                                                       encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            for attempt in range(10):                                  # the same second page, again and again
                (paths["inbox"] / f"second-{attempt}.html").write_text(page("A B Health", second), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_import(ctx, paths, SimpleNamespace())
        ids = [r["job_id"] for r in store.all("SELECT job_id FROM jobs ORDER BY first_seen_at, job_id")]
        self.assertEqual(len(ids), 2)                                  # one row per posting, however often it comes
        held, own = "manual:a-b-health:R-4001", next(i for i in ids if i != "manual:a-b-health:R-4001")
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND other_id = ? AND relation = 'possible_duplicate'",
                                  (own, held)))
        self.assertEqual(lead_row(store, "lead:second")["job_id"], own)   # the lead links the job actually written
        # the search for a free id is bounded: beyond it the posting is reported, never written over another
        with mock.patch.object(manual_intake, "MAX_PLACEMENTS", 0):
            raw, employer = parse_saved_html(page("A B Health", "https://jobs.a-b-health.example/postings/third"), "y.html")
            counts = ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url",
                            full_board=False)
        self.assertEqual((counts["errors"], counts["new"]), (1, 0))
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM jobs")["n"], 2)
        self.assertTrue(store.one("SELECT 1 FROM exceptions WHERE error_code = 'manual_id_collision'"))
        self.assertEqual(store.one("SELECT url FROM jobs WHERE job_id = ?", (held,))["url"], "https://careers.ab-health.example/jobs/4001")

    def test_a_second_portal_opening_gets_its_own_lead_and_a_refused_import_is_kept_in_failed(self):
        """Codex CR-2026-11-15-GATE-FIXES Pass 10: H12, a second portal opening (".../#/opening/R-1102") was made a
        duplicate of the first and its lead linked to the first; H14, a refused import (no free id) or a failed write
        was still filed as imported and its lead linked to the job holding the id."""
        from unittest import mock

        from careeros import pipeline
        from careeros.discovery import manual_intake
        from careeros.paths import vault_paths
        portal = "https://jobs.example.com/portal/#/opening/"
        store, ctx = setup()
        add_lead(store, "lead:1102", "Claims Analyst", "Acme Rx · Remote", url=portal + "R-1102")
        add_lead(store, "lead:1103", "Claims Analyst", "Acme Rx · Remote", url=portal + "R-1103")
        add_lead(store, "lead:1104", "Claims Analyst", "Acme Rx · Remote", url=portal + "R-1104")
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            text = Path(tmp) / "posting.txt"
            for opening, body in (("R-1101", CLAIMS + "\nThe work will be performed only in Calgary."), ("R-1102", CLAIMS)):
                text.write_text(body, encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_add_url(ctx, paths, SimpleNamespace(url=portal + opening, text_file=str(text), employer="Acme Rx",
                                                                parent=None, title="Claims Analyst", location="Remote - US"))
            first, second = (store.one("SELECT * FROM jobs WHERE url = ?", (portal.replace("/#", "#") + o,)) for o in ("R-1101", "R-1102"))
            self.assertNotEqual(second["current_state"], "duplicate")
            # never R-1101's job; since the owner's D184 a portal's link names no posting, so the lead stays open for you
            # (a lead linked by a board's posting link: the test above)
            self.assertIsNone(lead_row(store, "lead:1102")["job_id"])
            # a refused import: no free id beside the held one; the file is kept in failed, its lead stays open
            paths["inbox"].mkdir(parents=True, exist_ok=True)
            (paths["inbox"] / "refused.txt").write_text(
                f"URL: {portal}R-1103\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: Remote - US\n\n" + CLAIMS, encoding="utf-8")
            refused = manual_intake.PlacementRefused("manual_id_collision: no free id")   # what place_manual raises
            with mock.patch.object(manual_intake, "place_manual", side_effect=refused):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((paths["inbox"] / "failed").rglob("refused.txt")))
            self.assertFalse(list((paths["inbox"] / "imported").rglob("refused.txt")) if (paths["inbox"] / "imported").exists() else [])
            self.assertEqual(lead_row(store, "lead:1103")["status"], "new")
            self.assertTrue(store.one("SELECT 1 FROM exceptions WHERE error_code = 'manual_id_collision'"))
            # a failed write (rolled back): the same
            (paths["inbox"] / "broken.txt").write_text(
                f"URL: {portal}R-1104\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: Remote - US\n\n" + CLAIMS, encoding="utf-8")
            with mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((paths["inbox"] / "failed").rglob("broken.txt")))
            self.assertEqual(lead_row(store, "lead:1104")["status"], "new")
            self.assertIsNone(store.one("SELECT 1 FROM jobs WHERE url LIKE ?", ("%R-1104",)))
            # add-url refuses the same way: nothing written, no lead linked
            text.write_text(CLAIMS, encoding="utf-8")
            with mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cli.cmd_add_url(ctx, paths, SimpleNamespace(url=portal + "R-1104", text_file=str(text), employer="Acme Rx",
                                                            parent=None, title="Claims Analyst", location="Remote - US"))
            self.assertEqual(lead_row(store, "lead:1104")["status"], "new")
            # and a paste: refused with a reason, never reported as added
            from careeros.discovery.manual_intake import intake
            with mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(ValueError, "could not be stored"):
                    intake(ctx, "Claims Analyst\nCompany: Other Health\n\n" + CLAIMS, "https://careers.other.example/jobs/77")
            self.assertIsNone(store.one("SELECT 1 FROM jobs WHERE url = 'https://careers.other.example/jobs/77'"))

    def test_the_review_pages_paste_box_links_the_lead_for_the_same_posting(self):
        store, ctx = setup()
        linkedin = "https://www.linkedin.com/jobs/view/4000000501"
        add_lead(store, "lead:box", "Claims Analyst", "Foo Health · Remote", url=linkedin)
        token = "paste-test-token"
        with tempfile.TemporaryDirectory() as tmp:
            server = HTTPServer(("127.0.0.1", 0), make_handler(store, MID, POLICY, token, Path(tmp) / "writer.lock", {}))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                key = next(k for a, _t, k in forms(httpx.get(f"{base}/?t={token}").text) if a == "paste")
                reply = httpx.post(f"{base}/paste?t={token}", data={"target": "new", "key": key, "url": linkedin, "company": "",
                                                                    "text": "Claims Analyst\nCompany: Foo Health\nLocation: Remote - US\n\n" + CLAIMS})
                self.assertEqual(reply.status_code, 303)
            finally:
                server.shutdown()
                server.server_close()
        lead = lead_row(store, "lead:box")
        self.assertEqual((lead["status"], lead["job_id"] is not None), ("ingested", True))


# --- item 6: a guessed Lever or Ashby board's match waits for the official-site check, never "ambiguous" ------------------------
class GuessedBoardTests(unittest.TestCase):
    def test_a_guessed_lever_board_without_a_company_name_waits_for_the_employers_site_then_the_lead_links(self):
        """Lever's and Ashby's listings carry no company name, so discovery's own identity check never passes for a guessed
        board, and a correct same-title match used to be recorded "ambiguous" (gap 5). Now the match waits for the board,
        and the employer's own site decides (ADR 0001); after admission and a scan the lead is linked."""
        from careeros.discovery.boards import lever_posting
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        uid = "eeeeeeee-5555-4555-8555-000000000005"
        boards.lever["examplehealth"] = [tbv.lever_job(uid, "Claims Analyst", tbv.EX_TEXT, "examplehealth")]
        web.employer_site("examplehealth.com", tbv.EX, '<a href="https://jobs.lever.co/examplehealth">Open roles</a>')
        add_lead(store, "lead:lv", "Claims Analyst", f"{tbv.EX} · Remote (US)")
        client = boards.client()
        result = bd.discover(ctx, bd.candidate_from_lead(lead_row(store, "lead:lv"), tbv.POLICY), client)
        client.close()
        self.assertEqual(result["outcome"], "identity_unresolved")   # no company name in a Lever listing
        match = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:lv'")
        self.assertEqual((match["decision"], match["method"], json.loads(match["candidates_json"])),
                         ("awaiting_board", "title_location", [uid]))   # not "ambiguous"
        self.assertEqual((lead_row(store, "lead:lv")["status"], store.one("SELECT COUNT(*) c FROM jobs")["c"]), ("new", 0))
        with contextlib.redirect_stdout(io.StringIO()):   # you give the employer's website (careeros sources domain)
            cli.cmd_sources(ctx, None, SimpleNamespace(action="domain", target="lever:examplehealth", value="examplehealth.com",
                                                       domain=None, note=None))
        counts = bv.maintain(ctx, site_factory=web.factory(), ats_client=boards.client())
        self.assertEqual(counts["verification_initial_verified"], 1)   # the employer's own site links the board
        self.assertEqual(reg.mapping(store, "lever:examplehealth")["verification_status"], "quarantined")
        store.clock.advance(days=1)
        counts = bv.maintain(ctx, site_factory=web.factory(), ats_client=boards.client())
        self.assertEqual(counts["validation_active"], 1)
        self.assertIsNone(lead_row(store, "lead:lv")["job_id"])   # admitted; its postings come with the first scan
        with unittest.mock.patch.object(cli, "fetch_board", lambda c, ats, token: [lever_posting(token, i) for i in boards.lever[token]]), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_discover(ctx, None, SimpleNamespace(employer=None))
        lead = lead_row(store, "lead:lv")
        self.assertEqual((lead["status"], lead["job_id"], proposed(store, "lead:lv")), ("new", None, f"lever:examplehealth:{uid}"))
        self.assertEqual(decisions(store, "lead:lv"), ["awaiting_board", "likely"])   # proposed to you by its title (D188)


# --- item 7 (Part C): a lead's domain= finds the board at the employer's own site; it is only a hint ------------------------------
UNRELATED_UID = "ffffffff-6666-4666-8666-000000000006"


def unrelated_world(web, boards, *, home_title: str):
    """unrelated.com: another company's site that names Example Health in its title (a partner page, say), with a careers
    page linking its own nameless Lever board and one posting there; the postings point back to unrelated.com."""
    web.page("https://unrelated.com/", tbv.html(home_title, '<a href="/careers">Careers</a><p>We move freight.</p>'))
    web.page("https://unrelated.com/careers", tbv.html("Careers", '<a href="https://jobs.lever.co/unrelatedcorp">Jobs</a>'
                                                       f'<a href="https://jobs.lever.co/unrelatedcorp/{UNRELATED_UID}">Claims Analyst</a>'))
    boards.lever["unrelatedcorp"] = [tbv.lever_job(UNRELATED_UID, "Claims Analyst", tbv.OTHER_TEXT + " Apply at unrelated.com.",
                                                   "unrelatedcorp")]


class DomainRouteTests(unittest.TestCase):
    def upkeep(self, ctx, web, boards):
        return bv.maintain(ctx, site_factory=web.factory(), ats_client=boards.client())

    def test_no_site_is_checked_for_a_leads_hint_once_the_settings_change_part_way(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.discovery.alerts import enroll_agency_hosts
        store, ctx = tbv.setup()
        web, boards = tbv.example_world()
        add_lead(store, "lead:d", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        enroll_agency_hosts(store)   # the suite's synthetic agency, set up
        real, removed = lr.lead_domains, mock.patch.dict(os.environ, {private_sites.ENV: ""})

        def hints_then_removed(*args, **kwargs):
            found = real(*args, **kwargs)
            removed.start()   # the agency's entry removed once the hints are read (the red team before Pass 4)
            return found

        try:
            with mock.patch.object(lr, "lead_domains", hints_then_removed):
                counts = self.upkeep(ctx, web, boards)
        finally:
            removed.stop()
        self.assertEqual((counts["lead_domain_check"], counts["verification_lead_domain_stopped"]), (1, 1))
        self.assertNotIn("verification_lead_domain_verified", counts)

    def test_a_lead_with_a_domain_and_no_board_known_finds_the_board_at_the_employers_own_site(self):
        store, ctx = tbv.setup()
        web, boards = tbv.example_world()
        add_lead(store, "lead:d", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        counts = self.upkeep(ctx, web, boards)
        self.assertEqual((counts["lead_domain_set"], counts["lead_domain_check"], counts["verification_lead_domain_verified"]), (1, 1, 1))
        ent = store.one("SELECT * FROM employer_entities")
        self.assertEqual((ent["official_domain"], ent["domain_origin"]), ("examplehealth.com", "lead"))
        self.assertEqual(reg.mapping(store, "greenhouse:examplehealth")["verification_status"], "quarantined")   # ADR 0003
        self.assertEqual(self.upkeep(ctx, web, boards)["lead_domain_check"], 0)   # a board is known now: not checked again,
        store.clock.advance(days=31)
        counts = self.upkeep(ctx, web, boards)
        self.assertEqual((counts["lead_domain_check"], counts["validation_active"]), (0, 1))   # not even after 30 days
        with contextlib.redirect_stdout(io.StringIO()):
            tbv.scan(ctx, boards.greenhouse["examplehealth"]["jobs"])
        self.assertEqual(proposed(store, "lead:d"), "greenhouse:examplehealth:101")   # proposed to you (D188)

    def test_a_board_waiting_for_an_unknown_domain_is_checked_again_as_soon_as_a_lead_supplies_it(self):
        store, ctx = tbv.setup()
        quiet = tbv.EX_TEXT.replace(" Learn more at examplehealth.com.", "")   # the board names no website of its own
        web, boards = tbv.example_world(jobs=[tbv.gh_job(101, "Claims Analyst", quiet, "examplehealth")])
        boards.greenhouse["examplehealth"]["content"] = ""
        _entity_id, mapping_id = tbv.claim(store, tbv.EX, None, "greenhouse", "examplehealth")
        self.assertEqual(self.upkeep(ctx, web, boards)["verification_initial_domain_unknown"], 1)
        waiting = reg.mapping(store, mapping_id)
        self.assertEqual(waiting["verification_failure"], "domain_unknown")
        self.assertGreater(waiting["reverify_at"], store.clock.now())   # it would wait 30 days
        add_lead(store, "lead:w", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        counts = self.upkeep(ctx, web, boards)   # the same day
        self.assertEqual((counts["lead_domain_retry"], counts["verification_initial_verified"]), (1, 1))
        self.assertEqual(reg.mapping(store, mapping_id)["verification_status"], "quarantined")
        self.assertIsNone(store.one("SELECT 1 FROM board_reviews WHERE reason_code = 'domain_unknown' AND status = 'open'"))

    def test_a_board_a_lead_supplied_the_domain_for_goes_first_among_the_first_checks(self):
        from tests.test_v21_policy import policy_with
        store, ctx = tbv.setup(policy_with(board_verification__fetch_official_sites=True, source_discovery__auto_activate=True,
                                           board_verification__max_verifications_per_run=1))
        quiet = tbv.EX_TEXT.replace(" Learn more at examplehealth.com.", "")
        web, boards = tbv.example_world(jobs=[tbv.gh_job(101, "Claims Analyst", quiet, "examplehealth")])
        boards.greenhouse["examplehealth"]["content"] = ""
        _entity_id, mapping_id = tbv.claim(store, tbv.EX, None, "greenhouse", "examplehealth")
        self.upkeep(ctx, web, boards)   # domain unknown
        _other, busier = tbv.claim(store, "Zeta Health", None, "greenhouse", "zetahealth")
        reg.update_board(store, busier, {"relevant_roles": 5})   # would come first otherwise
        add_lead(store, "lead:first", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        self.upkeep(ctx, web, boards)
        self.assertEqual(reg.mapping(store, mapping_id)["verification_status"], "quarantined")
        self.assertIsNone(store.one("SELECT 1 FROM verification_runs WHERE mapping_id = ?", (busier,)))   # it waits its turn

    def test_an_employer_whose_site_shows_no_board_is_checked_again_only_after_30_days(self):
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        web.employer_site("examplehealth.com", tbv.EX, "<p>No openings right now.</p>")
        add_lead(store, "lead:n", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        self.assertEqual(self.upkeep(ctx, web, boards)["lead_domain_check"], 1)
        self.assertEqual(self.upkeep(ctx, web, boards)["lead_domain_check"], 0)
        store.clock.advance(days=31)
        self.assertEqual(self.upkeep(ctx, web, boards)["lead_domain_check"], 1)

    def test_a_lead_domain_named_only_in_a_homepages_text_does_not_verify(self):
        """The right employer, but its homepage names it only in its text: a hint needs the title or the site name."""
        store, ctx = tbv.setup()
        web, boards = tbv.example_world()
        web.page("https://examplehealth.com/", tbv.html("Home", f'<nav><a href="/careers">Careers</a></nav><p>{tbv.EX} helps plan '
                                                                 "members every day.</p>"))
        add_lead(store, "lead:t", "Claims Analyst", f"{tbv.EX} · Remote · domain=examplehealth.com")
        self.upkeep(ctx, web, boards)
        self.assertNotIn(reg.mapping(store, "greenhouse:examplehealth")["verification_status"], ("quarantined", "active"))
        self.assertTrue(store.one("SELECT 1 FROM verification_evidence WHERE method = 'names:text'"))

    def test_a_wrong_domain_can_only_fail_to_verify_never_verify_another_companys_board(self):
        """A lead's domain= points at another company, whose site even names the employer in its title. The other company's
        nameless Lever board carries that domain: for a hint that is no identity, so nothing is admitted."""
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        unrelated_world(web, boards, home_title=f"Unrelated Corp | partner of {tbv.EX}")
        add_lead(store, "lead:x", "Claims Analyst", f"{tbv.EX} · Remote · domain=unrelated.com")
        counts = self.upkeep(ctx, web, boards)
        self.assertEqual(counts["lead_domain_check"], 1)
        m = reg.mapping(store, "lever:unrelatedcorp")
        self.assertEqual((m["verification_status"], m["identity_check"]), ("review_required", "unresolved"))
        self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
        self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)
        self.assertEqual(lead_row(store, "lead:x")["status"], "new")

    def test_a_domain_you_set_is_checked_as_before(self):
        """The same site with the domain set by you (careeros sources domain): you vouch for the website, so the domain on the
        board counts as identity, as it always did (ADR 0001)."""
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        unrelated_world(web, boards, home_title=f"Unrelated Corp | partner of {tbv.EX}")
        entity_id = reg.ensure_entity(store, tbv.EX)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_sources(ctx, None, SimpleNamespace(action="domain", target=tbv.EX, value="unrelated.com", domain=None, note=None))
        self.assertEqual(reg.entity(store, entity_id)["domain_origin"], "owner")
        tbv.verify(ctx, web, boards, entity_id)
        self.assertEqual(reg.mapping(store, "lever:unrelatedcorp")["verification_status"], "quarantined")

    def test_a_leads_domain_never_merges_employer_records_or_creates_an_alias(self):
        store, ctx = setup()
        bar = reg.ensure_entity(store, "Bar Corp", domain="bar.com")
        add_lead(store, "lead:m", "Claims Analyst", "Foo Health · Remote · domain=bar.com")
        hints = lr.lead_domains(ctx, store.clock.now())
        self.assertEqual(hints["counts"]["lead_domain_conflict"], 1)
        foo = store.one("SELECT * FROM employer_entities WHERE canonical_norm = 'foo health'")
        self.assertNotEqual(foo["entity_id"], bar)
        self.assertIsNone(foo["official_domain"])
        self.assertEqual(reg.ensure_entity_for(store, "Foo Health", domain="bar.com", origin="recommendation"), foo["entity_id"])
        self.assertEqual(store.one("SELECT COUNT(*) c FROM employer_aliases")["c"], 0)
        reg.ensure_entity(store, "Baz Care", domain="bazcare.com")
        add_lead(store, "lead:k", "Claims Analyst", "Baz Care · Remote · domain=bazcare-other.com")
        self.assertEqual(lr.lead_domains(ctx, store.clock.now())["counts"]["lead_domain_kept"], 1)   # its website stands
        self.assertEqual(store.one("SELECT official_domain FROM employer_entities WHERE canonical_norm = 'baz care'")["official_domain"],
                         "bazcare.com")


# --- item 11: the curated list's and `careeros sources suggest`'s domains are hints too (the owner's answer, 2026-11-02) ------------
class CuratedDomainTests(unittest.TestCase):
    def test_a_curated_or_suggested_domain_never_merges_employer_records_or_creates_an_alias(self):
        store, ctx = setup()
        bar = reg.ensure_entity(store, "Bar Corp", domain="bar.com")
        fake = FakeATS()
        for token, name in (("foohealth", "Foo Health"), ("bazcare", "Baz Care"), ("quxhealth", "Qux Health")):
            fake.greenhouse[token] = {"name": name, "content": "", "jobs": [gh_item(1, "Claims Analyst", CLAIMS, token)]}
        with tempfile.TemporaryDirectory() as tmp:
            curated = Path(tmp) / "curated_employers.yaml"
            curated.write_text("employers:\n"
                               "  - {name: Foo Health, domain: bar.com, board: 'https://job-boards.greenhouse.io/foohealth'}\n"
                               "  - {name: Qux Health, domain: quxhealth.com, board: 'https://job-boards.greenhouse.io/quxhealth'}\n",
                               encoding="utf-8")
            self.assertEqual(dq.load_curated(ctx, curated)["added"], 2)
        with contextlib.redirect_stdout(io.StringIO()):   # careeros sources suggest "Baz Care" --domain bar.com --board ...
            cli.cmd_sources(ctx, None, SimpleNamespace(action="suggest", target="Baz Care", domain="bar.com", note=None,
                                                       board="https://job-boards.greenhouse.io/bazcare"))
        client = client_for(fake)
        dq.run(ctx, client=client)
        client.close()
        domains = {r["canonical_norm"]: (r["official_domain"], r["domain_origin"]) for r in store.all("SELECT * FROM employer_entities")}
        self.assertEqual(domains, {"bar": ("bar.com", None), "foo health": (None, None), "baz care": (None, None),
                                   "qux health": ("quxhealth.com", "curated")})
        self.assertEqual(store.one("SELECT COUNT(*) c FROM employer_aliases")["c"], 0)
        self.assertEqual(reg.entity(store, bar)["canonical_norm"], "bar")   # "Corp" is a legal suffix
        cases = store.all("SELECT * FROM board_reviews WHERE reason_code = 'domain_conflict' AND status = 'open'")
        self.assertEqual(len(cases), 2)   # the list's author sees why the website was not used
        self.assertTrue(all("bar.com" in c["detail"] for c in cases))

    def test_a_wrong_curated_domain_can_only_fail_to_verify(self):
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        unrelated_world(web, boards, home_title=f"Unrelated Corp | partner of {tbv.EX}")
        entity_id = reg.ensure_entity_for(store, tbv.EX, domain="unrelated.com", origin="curated")
        self.assertEqual(reg.entity(store, entity_id)["domain_origin"], "curated")
        tbv.verify(ctx, web, boards, entity_id)
        self.assertNotIn(reg.mapping(store, "lever:unrelatedcorp")["verification_status"], ("quarantined", "active"))
        self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))

    def test_a_curated_domain_named_only_in_a_homepages_text_does_not_verify(self):
        store, ctx = tbv.setup()
        web, boards = tbv.example_world()
        web.page("https://examplehealth.com/", tbv.html("Home", f'<nav><a href="/careers">Careers</a></nav><p>{tbv.EX} helps plan '
                                                                 "members every day.</p>"))
        entity_id = reg.ensure_entity_for(store, tbv.EX, domain="examplehealth.com", origin="curated")
        tbv.verify(ctx, web, boards, entity_id)
        self.assertNotIn(reg.mapping(store, "greenhouse:examplehealth")["verification_status"], ("quarantined", "active"))

    def test_a_website_an_employer_record_already_had_keeps_the_check_it_had(self):
        """No data rewrite: a website set before 2026-11-02 (domain_origin empty) is checked as it always was."""
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        unrelated_world(web, boards, home_title=f"Unrelated Corp | partner of {tbv.EX}")
        entity_id = reg.ensure_entity(store, tbv.EX, domain="unrelated.com")
        self.assertIsNone(reg.entity(store, entity_id)["domain_origin"])
        tbv.verify(ctx, web, boards, entity_id)
        self.assertEqual(reg.mapping(store, "lever:unrelatedcorp")["verification_status"], "quarantined")


# --- Codex CR-2026-11-02-LEAD-RESOLUTION F1: a wrong hinted website never verifies another employer's board ---------------------
CONTRACTOR_LEVER = ("aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002")


def customer_world(web, boards, ats: str, *, mentions: bool = True, home_title="Unrelated Corp | implementation partner of Example Health",
                   home_meta=""):
    """Codex's reproduction: the hint points at a contractor's own site, which names Example Health only as its partner and
    links the contractor's own board; two postings mention Example Health as the contractor's customer. home_meta goes in
    the homepage's head (Pass 2: a declared site name)."""
    if ats == "greenhouse":
        careers = ('<a href="https://job-boards.greenhouse.io/unrelatedcorp">Jobs</a>'
                   '<a href="https://job-boards.greenhouse.io/unrelatedcorp/jobs/101">Claims Analyst</a>')
    else:
        careers = (f'<a href="https://jobs.lever.co/unrelatedcorp">Jobs</a>'
                   f'<a href="https://jobs.lever.co/unrelatedcorp/{CONTRACTOR_LEVER[0]}">Claims Analyst</a>')
    home = tbv.html(home_title, '<nav><a href="/careers">Careers</a></nav><p>We build claims systems.</p>')
    web.page("https://contractor.example.com/", home.replace("</head>", f"{home_meta}</head>"))
    web.page("https://contractor.example.com/careers", tbv.html("Careers", careers))
    text = ("Unrelated Corp is hiring for its own team. We implement systems for Example Health, our customer. This role is "
            "employed by Unrelated Corp, not Example Health. Learn more at contractor.example.com." if mentions else
            "Unrelated Corp is hiring for its own team. Learn more at contractor.example.com.")
    if ats == "greenhouse":
        boards.greenhouse["unrelatedcorp"] = {"name": "Unrelated Corp", "content": "Learn more at contractor.example.com.", "jobs": [
            tbv.gh_job(101, "Claims Analyst", text, "unrelatedcorp"), tbv.gh_job(102, "Implementation Analyst", text, "unrelatedcorp")]}
    else:
        boards.lever["unrelatedcorp"] = [tbv.lever_job(uid, "Claims Analyst", text, "unrelatedcorp") for uid in CONTRACTOR_LEVER]


def site_name_tag(key: str, value: str) -> str:
    """A site-name declaration as pages write it: og:site_name in a property attribute, application-name in a name one."""
    return f'<meta property="{key}" content="{value}">' if key == "og:site_name" else f'<meta name="{key}" content="{value}">'


class HintedIdentityTests(unittest.TestCase):
    def check(self, ats: str, origin: str, home_html=None, **world):
        store, ctx = tbv.setup()   # official-site checks on, auto_activate on
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        customer_world(web, boards, ats, **world)
        if home_html is not None:   # the contractor's homepage exactly as given (Codex Pass 6: one ending mid-title)
            web.page("https://contractor.example.com/", home_html)
        entity_id = reg.ensure_entity_for(store, tbv.EX, domain="contractor.example.com", origin=origin)
        tbv.verify(ctx, web, boards, entity_id)
        mapping_id = f"{ats}:unrelatedcorp"
        client = boards.client()
        validated = bv.validate_board(ctx, mapping_id, client)   # even a validation scan with auto_activate never admits it
        client.close()
        return store, reg.mapping(store, mapping_id), validated

    def test_a_board_named_for_another_company_is_rejected_whatever_its_postings_mention(self):
        for origin in ("lead", "curated"):
            with self.subTest(origin=origin):
                store, m, validated = self.check("greenhouse", origin)
                self.assertEqual((m["identity_check"], m["verification_status"], validated), ("mismatch", "rejected", "skipped"))
                self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_nameless_board_on_a_site_that_only_mentions_the_employer_waits_for_your_review(self):
        for origin in ("lead", "curated"):
            with self.subTest(origin=origin):
                store, m, validated = self.check("lever", origin)
                self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_nameless_board_on_the_employers_own_branded_site_still_verifies(self):
        """The control: a hint that IS the employer's site (it presents itself as Example Health) still finds its nameless
        board, through the official-site handshake, without any posting having to name it."""
        store, ctx = tbv.setup()
        web, boards = tbv.FakeWeb(), tbv.FakeBoards()
        web.employer_site("examplehealth.com", tbv.EX, '<a href="https://jobs.lever.co/examplehealth">Jobs</a>'
                                                       f'<a href="https://jobs.lever.co/examplehealth/{CONTRACTOR_LEVER[0]}">Claims</a>')
        boards.lever["examplehealth"] = [tbv.lever_job(CONTRACTOR_LEVER[0], "Claims Analyst", "Join the claims team.", "examplehealth")]
        entity_id = reg.ensure_entity_for(store, tbv.EX, domain="examplehealth.com", origin="lead")
        tbv.verify(ctx, web, boards, entity_id)
        m = reg.mapping(store, "lever:examplehealth")
        self.assertEqual((m["identity_check"], m["verification_status"]), ("confirmed", "quarantined"))
        self.assertEqual(store.one("SELECT notes FROM verification_evidence WHERE evidence_type = 'identity'")["notes"], "employer_site_brand")

    def test_the_employers_own_site_declaring_no_site_name_sends_its_nameless_board_to_your_review(self):
        """The cost of Pass 7: a hint that IS the employer's site, titled "Example Health | Pharmacy benefits" but declaring
        no site name (og:site_name, application-name), no longer verifies its nameless board by its title. The board waits for
        your review (careeros sources approve), from a lead's or the curated list's hint; never quarantined or active."""
        for origin in ("lead", "curated"):
            with self.subTest(origin=origin):
                store, ctx = tbv.setup()
                web, boards = tbv.FakeWeb(), tbv.FakeBoards()
                web.employer_site("examplehealth.com", tbv.EX, '<a href="https://jobs.lever.co/examplehealth">Jobs</a>'
                                                               f'<a href="https://jobs.lever.co/examplehealth/{CONTRACTOR_LEVER[0]}">Claims</a>',
                                  site_name=False)
                boards.lever["examplehealth"] = [tbv.lever_job(CONTRACTOR_LEVER[0], "Claims Analyst", "Join the claims team.", "examplehealth")]
                entity_id = reg.ensure_entity_for(store, tbv.EX, domain="examplehealth.com", origin=origin)
                tbv.verify(ctx, web, boards, entity_id)
                self.assertNotIn(reg.mapping(store, "lever:examplehealth")["verification_status"], ("quarantined", "active"))
                self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))

    def test_the_site_brand_is_the_sites_own_name_not_a_mention(self):
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        page = lambda title, meta="": inspect(f"<html><head><title>{title}</title>{meta}</head><body></body></html>",
                                              "https://x.example.com/", "x.example.com")
        # Pass 7: a title never brands a hinted site, however it reads
        self.assertFalse(bv.branded_as(page("Example Health | Pharmacy benefits"), names))
        self.assertFalse(bv.branded_as(page("Example Health, Inc. - Home"), names))
        self.assertTrue(bv.branded_as(page("Home", '<meta property="og:site_name" content="Example Health">'), names))
        self.assertFalse(bv.branded_as(page("Unrelated Corp | implementation partner of Example Health"), names))
        self.assertFalse(bv.branded_as(page("Example Health implementation services | Unrelated Corp"), names))
        self.assertFalse(bv.branded_as(page("Home"), names))
        # Pass 2: a declared site name settles it, and every declared one must be the employer's; a blank one is no declaration
        unrelated = '<meta property="og:site_name" content="Unrelated Corp">'
        self.assertFalse(bv.branded_as(page("Example Health | customer story", unrelated), names))
        self.assertFalse(bv.branded_as(page("Example Health", unrelated), names))
        self.assertFalse(bv.branded_as(page("Example Health", '<meta property="og:site_name" content="Example Health">'
                                                             '<meta name="application-name" content="Unrelated Corp">'), names))
        self.assertTrue(bv.branded_as(page("Welcome | Plan members first", '<meta property="og:site_name" content="Example Health, Inc.">'), names))
        self.assertFalse(bv.branded_as(page("Example Health | Home", '<meta property="og:site_name" content="  ">'), names))

    def test_a_title_naming_the_employer_never_overrides_the_site_name_the_page_declares(self):
        """Codex Pass 2 (the F1 residual): the contractor's homepage is titled "Example Health | customer story" but declares
        itself Unrelated Corp (og:site_name). Its nameless board waits for your review, from a lead's or the curated list's
        hint, whether or not its postings mention Example Health; never quarantined or active, no employer admitted."""
        for origin in ("lead", "curated"):
            for mentions in (True, False):
                with self.subTest(origin=origin, mentions=mentions):
                    store, m, validated = self.check("lever", origin, mentions=mentions, home_title="Example Health | customer story",
                                                     home_meta='<meta property="og:site_name" content="Unrelated Corp">')
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_the_site_name_the_employer_declares_verifies_its_nameless_board_whatever_its_title_leads_with(self):
        """The Pass 2 control: the employer's own site declaring its name (og:site_name "Example Health, Inc.") under a title
        that leads with something else still finds its nameless board through the official-site handshake; so does one that
        repeats its declarations (Pass 3: every one is its own name, a blank one is no declaration)."""
        for meta in ('<meta property="og:site_name" content="Example Health, Inc.">',
                     '<meta property="og:site_name" content="Example Health"><meta property="og:site_name" content="">'
                     '<meta name="application-name" content="Example Health, Inc.">'):
            with self.subTest(meta=meta):
                store, ctx = tbv.setup()
                web, boards = tbv.FakeWeb(), tbv.FakeBoards()
                home = tbv.html("Welcome | Plan members first", f'<nav><a href="/careers">Careers</a></nav><p>{tbv.EX} helps plan members every day.</p>')
                web.page("https://examplehealth.com/", home.replace("</head>", f"{meta}</head>"))
                web.page("https://examplehealth.com/careers", tbv.html("Careers", '<a href="https://jobs.lever.co/examplehealth">Jobs</a>'
                                                                                 f'<a href="https://jobs.lever.co/examplehealth/{CONTRACTOR_LEVER[0]}">Claims</a>'))
                boards.lever["examplehealth"] = [tbv.lever_job(CONTRACTOR_LEVER[0], "Claims Analyst", "Join the claims team.", "examplehealth")]
                entity_id = reg.ensure_entity_for(store, tbv.EX, domain="examplehealth.com", origin="curated")
                tbv.verify(ctx, web, boards, entity_id)
                m = reg.mapping(store, "lever:examplehealth")
                self.assertEqual((m["identity_check"], m["verification_status"]), ("confirmed", "quarantined"))
                self.assertEqual(store.one("SELECT notes FROM verification_evidence WHERE evidence_type = 'identity'")["notes"], "employer_site_brand")

    def test_every_site_name_a_page_declares_counts_repeated_tags_included(self):
        """Codex Pass 3 (the F1 residual): the page reader kept only the last tag of each key, so a later og:site_name or
        application-name, blank or the employer's, erased an earlier "Unrelated Corp". Every declaration counts, in either
        order, and a tag declares through either its property or its name attribute (once per tag)."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        page = lambda meta: inspect(f"<html><head><title>Example Health | customer story</title>{meta}</head><body></body></html>",
                                    "https://x.example.com/", "x.example.com")
        for key in ("og:site_name", "application-name"):
            for later in ("", "Example Health"):
                for pair in (("Unrelated Corp", later), (later, "Unrelated Corp")):
                    with self.subTest(key=key, order=pair):
                        self.assertFalse(bv.branded_as(page(site_name_tag(key, pair[0]) + site_name_tag(key, pair[1])), names))
        inspection = page('<meta property="og:description" name="application-name" content="Unrelated Corp">'
                          '<meta property="og:site_name" name="application-name" content="Example Health">')
        self.assertEqual(inspection.site_names, ["Unrelated Corp", "Example Health"])
        self.assertFalse(bv.branded_as(inspection, names))
        padded = page(site_name_tag("og:site_name", " " * 300 + "Unrelated Corp") + site_name_tag("og:site_name", "Example Health"))
        self.assertEqual(padded.site_names, ["Unrelated Corp", "Example Health"])   # trimmed before the length limit
        self.assertFalse(bv.branded_as(padded, names))
        self.assertTrue(bv.branded_as(page(site_name_tag("og:site_name", "Example Health") + site_name_tag("og:site_name", "")
                                           + site_name_tag("application-name", "Example Health, Inc.")), names))

    def test_a_repeated_site_name_tag_never_erases_another_companys_declaration(self):
        """Codex Pass 3 (the F1 residual), end to end: the contractor's homepage, titled "Example Health | customer story",
        declares itself Unrelated Corp and repeats the tag, blank or as Example Health, in either order, for either key. No
        posting mentions Example Health. Its nameless board waits for your review from a lead's or the curated list's hint
        (careeros sources suggest --domain uses the curated origin); never quarantined or active, no employer admitted."""
        for origin in ("lead", "curated"):
            for key in ("og:site_name", "application-name"):
                for later in ("", "Example Health"):
                    for pair in (("Unrelated Corp", later), (later, "Unrelated Corp")):
                        with self.subTest(origin=origin, key=key, order=pair):
                            store, m, validated = self.check("lever", origin, mentions=False, home_title="Example Health | customer story",
                                                             home_meta=site_name_tag(key, pair[0]) + site_name_tag(key, pair[1]))
                            self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                            self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                            self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_site_name_or_title_is_compared_whole_never_clipped(self):
        """Codex Pass 4 (the F1 residual): the page reader clipped each declared site name, and the title, to 300
        characters before the check, so "Example Health", 310 spaces or dots, then "| Unrelated Corp" read as "Example
        Health". Both are read and compared whole; a name with only trailing padding is still the employer's."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        page = lambda title, meta="": inspect(f"<html><head><title>{title}</title>{meta}</head><body></body></html>",
                                              "https://x.example.com/", "x.example.com")
        for pad in (" " * 310, "." * 310):
            declared = f"Example Health{pad}| Unrelated Corp"
            for key in ("og:site_name", "application-name"):
                with self.subTest(pad=pad[0], key=key):
                    inspection = page("Example Health | customer story", site_name_tag(key, declared))
                    self.assertEqual(inspection.site_names, [declared])
                    self.assertFalse(bv.branded_as(inspection, names))
            with self.subTest(pad=pad[0], key="title"):
                inspection = page(f"Example Health{pad}Unrelated Corp")
                self.assertEqual(inspection.title, f"Example Health{pad}Unrelated Corp")
                self.assertFalse(bv.branded_as(inspection, names))
        self.assertTrue(bv.branded_as(page("Home", site_name_tag("og:site_name", "Example Health" + " " * 310)), names))
        self.assertEqual(page("Example Health" + " " * 310).title, "Example Health")   # read whole (a title no longer brands, Pass 7)

    def test_an_oversized_site_name_or_title_never_reads_as_the_employers(self):
        """Codex Pass 4 (the F1 residual), end to end: the contractor's homepage declares "Example Health", 310 spaces, then
        "| Unrelated Corp" (340 characters) under a customer-story title, for either key, or declares nothing under a title
        that long. No posting mentions Example Health. Clipped, it read as Example Health and the contractor's nameless board
        was verified and made active; now it waits for your review from a lead's or the curated list's hint, never
        quarantined or active, no employer admitted."""
        long_name = "Example Health" + " " * 310 + "| Unrelated Corp"
        worlds = {key: {"home_title": "Example Health | customer story", "home_meta": site_name_tag(key, long_name)}
                  for key in ("og:site_name", "application-name")}
        worlds["title"] = {"home_title": "Example Health" + " " * 310 + "Unrelated Corp"}
        for origin in ("lead", "curated"):
            for case, world in worlds.items():
                with self.subTest(origin=origin, case=case):
                    store, m, validated = self.check("lever", origin, mentions=False, **world)
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_page_sized_title_or_site_name_is_compared_in_linear_time(self):
        """Codex Pass 5 (an availability note on the whole-value change): splitting a whole title took quadratic time on a
        long run of whitespace, about 5 seconds for 32,000 spaces and hours at the page limit (safe_fetch.MAX_BYTES). Since
        Pass 7 the title is never split for a hinted site at all; reading and checking a page-sized title or site name stays
        fast."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        page = lambda title, meta="": inspect(f"<html><head><title>{title}</title>{meta}</head><body></body></html>",
                                              "https://x.example.com/", "x.example.com")
        long_run = "Example Health" + " " * 100_000 + "Unrelated Corp"
        for case, inspection in (("title", page(long_run)), ("site name", page("Home", site_name_tag("og:site_name", long_run)))):
            with self.subTest(case=case):
                started = time.perf_counter()
                self.assertFalse(bv.branded_as(inspection, names))
                self.assertLess(time.perf_counter() - started, 5)
        self.assertTrue(bv.branded_as(page("Home", site_name_tag("og:site_name", "Example Health" + " " * 100_000)), names))

    # --- Pass 6 (a pre-review sweep of F1 before Codex's pass) --------------------------------------------------------------
    def test_a_page_the_reader_did_not_read_whole_never_brands_a_hinted_site(self):
        """A parse error, or a quirk of Python's HTML parser that a browser does not share ("</script/>" ending no script),
        hid the contractor's "Unrelated Corp" site name from the reader, which then saw only an "Example Health" one. A page
        the reader could not parse is never branded (Pass 6), and the declarations it read must be exactly those in the
        page's source (Pass 7); otherwise a hinted site waits for your review. Since Pass 9 the reader reads tags as a
        browser does, so "</script/>" ends the script and the contractor's name is read; where the reader is stricter than
        a browser (a <select>), the source still finds what it skipped."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        employer, unrelated = site_name_tag("og:site_name", "Example Health"), site_name_tag("og:site_name", "Unrelated Corp")
        title = "<title>Home</title>"
        page = lambda head, body="<p>Welcome</p>": inspect(f"<html><head>{head}</head><body>{body}</body></html>",
                                                            "https://x.example.com/", "x.example.com")
        from careeros.discovery import site_inspect
        with unittest.mock.patch.object(site_inspect._Collector, "parse_marked_section", side_effect=AssertionError("parser error")):
            errors = {"a parse error": page(title + employer + "<![x]>" + unrelated),
                      "a parse error, nothing after it": page(title + employer + "<![x]>")}
        hidden = {
            **errors,
            "an entity-encoded key in a select": page(title + employer, '<select><meta property="og&#58;site_name" content="Unrelated Corp"></select>'),
            "an unclosed comment at the end": page(title + employer + "<!-- " + unrelated, body=""),
        }
        for case, inspection in hidden.items():
            with self.subTest(case=case):
                self.assertEqual(inspection.site_names, ["Example Health"])
                self.assertFalse(bv.branded_as(inspection, names))
        self.assertFalse(hidden["a parse error, nothing after it"].read_whole)
        self.assertEqual(hidden["an entity-encoded key in a select"].source_site_names, ["Example Health", "Unrelated Corp"])
        ended = page(title + employer + "<script>var a = 1;</script/>" + unrelated)   # as in a browser (Pass 9)
        self.assertEqual(ended.site_names, ["Example Health", "Unrelated Corp"])
        self.assertFalse(bv.branded_as(ended, names))
        bad_link = '<link rel="canonical" href="https://[host]/">'   # a malformed URL skips that link, not the rest of the page
        self.assertEqual(page(title + bad_link + unrelated).site_names, ["Unrelated Corp"])
        self.assertFalse(bv.branded_as(page(title + bad_link + unrelated), names))
        for control in (page(title + bad_link + employer), page(title + employer)):
            self.assertTrue(control.read_whole)
            self.assertTrue(bv.branded_as(control, names))

    def test_a_comment_ends_where_a_browser_ends_it(self):
        """Python 3.12.10 ends a comment at "--" + optional whitespace + ">": "<!-->" and "<!--->" (whole empty comments in
        HTML5) and "--!>" (which ends one) left a comment open over the contractor's "Unrelated Corp" site name, and "-- >"
        ended one early, showing a commented-out "Example Health" site name a browser never shows. Comments now end as in
        HTML5."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        unrelated = site_name_tag("og:site_name", "Unrelated Corp")
        page = lambda head: inspect(f"<html><head>{head}</head><body><p>Welcome</p></body></html>", "https://x.example.com/", "x.example.com")
        for opener in ("<!-->", "<!--->", "<!-- x --!>"):
            with self.subTest(opener=opener):
                inspection = page("<title>Example Health | customer story</title>" + opener + unrelated + "<!-- analytics -->")
                self.assertEqual((inspection.site_names, inspection.read_whole), (["Unrelated Corp"], True))
                self.assertFalse(bv.branded_as(inspection, names))
        kept = page('<title>Unrelated Corp</title><!-- old: -- > <meta property="og:site_name" content="Example Health"> -->')
        self.assertEqual(kept.site_names, [])
        self.assertFalse(bv.branded_as(kept, names))
        self.assertTrue(bv.branded_as(page("<title>Home</title><!-- a -- b --><!---->" + site_name_tag("og:site_name", "Example Health")), names))

    def test_the_title_is_the_documents_first_title_never_an_svgs(self):
        """An SVG's <title> (a customer's logo labelled "Example Health"), or a <title> written in a textarea, became the
        page's title when the head's was empty or missing, and a later one was appended. The title is the document's
        first <title> outside such elements, as in a browser; a stray end tag ("</math>") or "<iframe/>" (which a browser
        leaves open) never ends that exclusion (Codex Pass 6). Since Pass 7 a title never brands a hinted site; it is still
        what the mentions check reads."""
        from careeros.discovery.site_inspect import inspect
        page = lambda head, body: inspect(f"<html><head>{head}</head><body>{body}</body></html>", "https://x.example.com/", "x.example.com")
        svg = "<svg><title>Example Health</title></svg><h1>Unrelated Corp</h1>"
        for head, body, expected in (("<title></title>", svg, ""), ("", svg, ""),
                                     ("", "<textarea><title>Example Health</title></textarea>", ""),
                                     ("<title>Unrelated Corp</title>", svg, "Unrelated Corp"),
                                     ("<title>Unrelated Corp</title>", "<title>Example Health</title>", "Unrelated Corp"),
                                     ("", "<svg></math><title>Example Health</title></svg>", ""),
                                     ("", "<svg></template><title>Example Health</title></svg>", ""),
                                     ("", "<iframe/><title>Example Health</title></iframe>", ""),
                                     ("", "<template/><title>Example Health</title></template>", "")):
            with self.subTest(head=head, body=body):
                self.assertEqual(page(head, body).title, expected)
        control = page("<title>Example Health | Home</title>", "<svg/><svg><title>Unrelated Corp logo</title></svg>")
        self.assertEqual(control.title, "Example Health | Home")
        self.assertEqual(page("", "<svg/><title>Example Health</title>").title, "Example Health")   # "/>" closes an SVG

    def test_only_the_documents_own_site_name_tags_declare_and_they_must_match_its_source(self):
        """Codex Pass 6 and the Pass 7 change. An unfinished title ("<title>Example Health<Unrelated", the rest unread) or an
        SVG title after a stray "</math>" read as Example Health and branded the contractor's site through the title
        fallback, which is gone. And only the document's own markup declares a site name: a site-name tag in a textarea or
        an SVG, after "<iframe/>", "<template/>" or "<script/>" (which a browser leaves open), after a stray end tag inside
        one, or in a comment, is no declaration, and the reader's declarations must equal those in the source (a tag the
        source scan cannot read counts as one). Each such page waits for your review."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        read = lambda html: inspect(html, "https://x.example.com/", "x.example.com")
        for html in ("<html><head><title>Example Health<Unrelated", "<html><head><title>Example Health</title",
                     "<html><head></head><body><svg></math><title>Example Health</title></svg></body></html>",
                     "<html><head></head><body><iframe/><title>Example Health</title></iframe></body></html>"):
            with self.subTest(html=html):
                self.assertFalse(bv.branded_as(read(html), names))
        employer = site_name_tag("og:site_name", "Example Health")
        page = lambda body: read(f"<html><head><title>Unrelated Corp | Home</title></head><body>{body}</body></html>")
        for case, body in (("in a textarea", f"<textarea>{employer}</textarea>"), ("in an SVG", f"<svg>{employer}</svg>"),
                           ("after a stray end tag in an SVG", f"<svg></math>{employer}</svg>"),
                           ("after a stray end tag in a textarea", f"<textarea></template>{employer}</textarea>"),
                           ("after <iframe/>", f"<iframe/>{employer}</iframe>"), ("after <template/>", f"<template/>{employer}</template>"),
                           ("after <script/>", f"<script/>{employer}</script>"), ("in a comment", f"<!-- {employer} -->")):
            with self.subTest(case=case):
                inspection = page(body)
                self.assertEqual((inspection.site_names, inspection.source_site_names), ([], ["Example Health"]))
                self.assertFalse(bv.branded_as(inspection, names))
        unreadable = page(employer + '<!-- <meta property="og:site_name" content="Un>related"> -->')
        self.assertEqual(unreadable.site_names, ["Example Health"])
        self.assertEqual(len(unreadable.source_site_names), 2)
        self.assertFalse(bv.branded_as(unreadable, names))
        control = page(employer + "<svg><title>Unrelated Corp logo</title></svg>")   # the document's own declaration decides
        self.assertEqual((control.site_names, control.source_site_names), (["Example Health"], ["Example Health"]))
        self.assertTrue(bv.branded_as(control, names))

    def test_raw_text_ends_only_at_its_own_end_tag_as_in_a_browser(self):
        """Codex Pass 7: a site-name tag written inside the title, after "<plaintext></plaintext>", or inside a textarea that
        "</template>" seemed to close, was read as the document's declaration, and the source scan agreed. In a browser,
        everything after a raw-text element's start tag (title, textarea, iframe, noscript, ...) is text up to that element's
        own end tag, and plaintext runs to the end of the page; a tag Python's parser reports inside it is text."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        read = lambda html: inspect(html, "https://x.example.com/", "x.example.com")
        for key in ("og:site_name", "application-name"):
            tag = site_name_tag(key, "Example Health")
            for case, html in (("in the title", f"<html><head><title>{tag}</title></head><body></body></html>"),
                               ("after <title/>", f"<html><head><title/>{tag}</title></head></html>"),
                               ("after a stray end tag in the title", f"<html><head><title></math>{tag}</title></head></html>"),
                               ("in an unclosed title", f"<html><head><title>Home{tag}"),
                               ("after <plaintext></plaintext>", f"<html><body><plaintext></plaintext>{tag}</body></html>"),
                               ("in a textarea an outer end tag seemed to close", f"<html><body><template><textarea></template>{tag}</body></html>"),
                               ("in raw text inside raw text", f"<html><body><template><textarea><xmp></xmp></template>{tag}</body></html>")):
                with self.subTest(key=key, case=case):
                    inspection = read(html)
                    self.assertEqual((inspection.site_names, inspection.source_site_names), ([], ["Example Health"]))
                    self.assertFalse(bv.branded_as(inspection, names))
        employer = site_name_tag("og:site_name", "Example Health")
        for html in (f"<html><head><title>Home</title>{employer}</head></html>",
                     f"<html><body><textarea>x</textarea>{employer}</body></html>",
                     f"<html><body><template><textarea></textarea></template>{employer}</body></html>",
                     f"<html><body><iframe><b>x</b></iframe>{employer}</body></html>"):
            with self.subTest(control=html):   # after each element's own end tag the document's markup resumes
                self.assertTrue(bv.branded_as(read(html), names))
        self.assertEqual(read("<html><head><title>Example <b>Health</b></title></head></html>").title, "Example <b>Health</b>")

    def test_a_site_name_tag_in_raw_text_never_admits_another_companys_board(self):
        """End to end, Codex Pass 7, from a lead's or the curated list's hint: the contractor's homepage carries an "Example
        Health" site-name tag inside its title, after "<plaintext></plaintext>", or inside a textarea that "</template>"
        seemed to close. No posting mentions Example Health. The contractor's nameless board waits for your review; never
        quarantined or active, no employer admitted."""
        tag = site_name_tag("og:site_name", "Example Health")
        nav = '<nav><a href="/careers">Careers</a></nav>'
        homes = {"in the title": f"<html><head><title>{tag}</title></head><body>{nav}</body></html>",
                 "after <plaintext></plaintext>": f"<html><head><title>Unrelated Corp | Home</title></head><body>{nav}<plaintext></plaintext>{tag}</body></html>",
                 "in a textarea an outer end tag seemed to close": f"<html><head><title>Unrelated Corp | Home</title></head><body>{nav}<template><textarea></template>{tag}</body></html>"}
        for origin in ("lead", "curated"):
            for case, home in homes.items():
                with self.subTest(origin=origin, case=case):
                    store, m, validated = self.check("lever", origin, home_html=home, mentions=False)
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_the_reader_never_brands_a_page_a_browsers_reading_would_not(self):
        """A browser comparison before Codex's Pass 8: CareerOS's page reader against Chromium's HTML5 parser on 43,000
        generated pages, with site-name tags among the constructs where Python 3.12.10's html.parser and HTML5 disagree.
        tests/fixtures/browser_site_names.json keeps, with the browser's reading of each, all 286 pages where CareerOS
        branded the site as Example Health and the browser's reading did not (before the fixes: a <frameset>, "<![" marked
        sections, unterminated references such as "&nbspHealth", a noscript's end tag), and 600 more. Pass 9 added Codex
        Pass 8's 80 pages and the 1,103 pages of a tokenizer round that c65bc93 branded and the browser did not, read both
        ways (JavaScript's trim, and HTML whitespace). On none may the reader brand a site the browser's reading would not."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        data = json.loads((Path(__file__).parent / "fixtures" / "browser_site_names.json").read_text(encoding="utf-8"))
        self.assertEqual((len(data["cases"]), sum(c["found_as"] == "A" for c in data["cases"])), (2069, 1469))
        brands = lambda seen: bool(seen) and all(bv.named_exactly(names, s) for s in seen)
        wrong = [c["html"][:160] for c in data["cases"]
                 if bv.branded_as(inspect(c["html"], "https://x.example.com/", "x.example.com"), names)
                 and not (brands(c["browser"]) and brands(c.get("browser_html", c["browser"])))]
        self.assertEqual(wrong, [])

    def test_four_parser_differences_the_browser_comparison_found(self):
        """Each of the four causes, with controls: a <frameset> (a browser can discard the body's tags for one); "<![" read
        as HTML5 does (a bogus comment to the first ">", a CDATA section only inside SVG or MathML); an unterminated
        reference in a site-name tag ("Example&nbspHealth", which Python decodes and a browser does not); and a noscript's
        end tag, which closes nothing while a raw-text or template element opened in it is still open with scripts off."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        employer = site_name_tag("og:site_name", "Example Health")
        read = lambda html: inspect(html, "https://x.example.com/", "x.example.com")
        for html in (f"<html><head>{employer}</head><frameset></frameset></html>", f"<frameset>{employer}",
                     f"<html><head></head><body><p>Welcome</p>{employer}<FRAMESET rows='*'>"):
            with self.subTest(frameset=html):
                self.assertFalse(read(html).read_whole)
                self.assertFalse(bv.branded_as(read(html), names))
        marked = read(f"<![endif]--><xmp>{employer}")
        self.assertEqual((marked.site_names, marked.source_site_names), ([], ["Example Health"]))
        self.assertFalse(bv.branded_as(marked, names))
        inside_svg = read(f"<svg><![CDATA[x></svg>]]>{employer}</svg>")
        self.assertEqual(inside_svg.site_names, [])
        for html in (f"<![CDATA[x]]>{employer}", f"<![ CDATA[x]]>{employer}", f"<svg><![CDATA[</svg>]]></svg>{employer}"):
            with self.subTest(marked_control=html):
                self.assertTrue(read(html).read_whole)
                self.assertTrue(bv.branded_as(read(html), names))
        unterminated = read('<meta property="og:site_name" content="Example&nbspHealth">')
        self.assertEqual((unterminated.site_names, unterminated.source_site_names), (["Example&nbspHealth"], ["Example&nbspHealth"]))
        self.assertFalse(bv.branded_as(unterminated, names))
        self.assertFalse(bv.branded_as(read('<meta property="og:site_name" content="Example&nbsp=Health">'), names))   # "=" too
        for value in ("Example&nbsp;Health", "Example&nbsp Health", "Example&#32Health"):
            with self.subTest(reference_control=value):
                self.assertTrue(bv.branded_as(read(f'<meta property="og:site_name" content="{value}">'), names))
        for html in (f"<noscript/><template></noscript/>{employer}", f"<noscript><iframe/></noscript/>{employer}",
                     f"<noscript><textarea></noscript>{employer}</textarea>",
                     f"<noscript><template><iframe></iframe></noscript>{employer}"):
            with self.subTest(noscript=html):
                self.assertEqual(read(html).site_names, [])
                self.assertFalse(bv.branded_as(read(html), names))
        gtm = read(f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X" height="0"></iframe></noscript>{employer}')
        self.assertTrue(bv.branded_as(gtm, names))   # the usual tag-manager snippet still lets the page declare after it

    # --- Pass 9 (Codex Pass 8: tags read by Python's tokenizer, not a browser's) --------------------------------------------
    def test_tags_are_read_as_a_browser_tokenizes_them(self):
        """Codex Pass 8. Python 3.12.10's parser split tags on whitespace a browser does not (U+000B, U+00A0 and 22 more),
        so 'property="og:site_name"[U+000B]content=...' had a content attribute a browser never reads, and "</title[U+000B]>"
        ended a title a browser keeps open; and it ended a script at its first "</script>", where a browser reads
        "<script><!--<script></script>" as one double-escaped section. The same tokenizer also took "</ title>" for an end
        tag, "==" for "=", NUL for the end of a tag name, and "</script>" written with a long s for "</script>". The reader now reads start tags,
        end tags, attribute values and raw text as HTML5 does, and the source scan reads each <meta> the same way."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        read = lambda html: inspect(html, "https://x.example.com/", "x.example.com")
        employer = site_name_tag("og:site_name", "Example Health")
        spaces = "\x0b\x1c\x1d\x1e\x1f\x85\xa0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000"
        self.assertEqual(len(spaces), 24)
        for space in spaces:
            for form, html in (("attributes", f'<meta property="og:site_name"{space}content="Example Health">'),
                               ("title", f"<title>Unrelated Corp</title{space}>{employer}"),
                               ("textarea", f"<textarea>Unrelated Corp</textarea{space}>{employer}")):
                with self.subTest(space=f"U+{ord(space):04X}", form=form):
                    self.assertFalse(bv.branded_as(read(html), names))
        for key in ("og:site_name", "application-name"):
            for script in ("<script><!--<script></script>{}</script>", "<SCRIPT><!--<ScRiPt></sCrIpT>{}</script>",
                           "<script><!--<script\n></script>{}</script>", '<script type="application/json"><!--<script></script>{}</script>'):
                with self.subTest(key=key, script=script):
                    html = f"<html><head>{script.format(site_name_tag(key, 'Example Health'))}</head><body></body></html>"
                    self.assertEqual(read(html).site_names, [])
                    self.assertFalse(bv.branded_as(read(html), names))
        for case, html in (("whitespace after </", f"<title>Unrelated Corp</ title>{employer}"),
                           ("whitespace after </ in a script", f"<script></ script>{employer}</script>"),
                           ("whitespace after </ in a style", f"<style></ style>{employer}</style>"),
                           ("whitespace after </ in a template", f"<template></ template>{employer}"),
                           ("a long s", f"<script></\u017fcript>{employer}</script>"),
                           ("NUL in a tag name", f"<div\x00 a=\"{employer.replace(chr(34), chr(39))}\">"),
                           ("==", '<meta property="og:site_name" content=="Example Health">'),
                           ("== moving where a tag ends", f'<div a=="x y="z>"{employer}">'),
                           ("an end tag's quoted attribute", f'<title>Unrelated Corp</title a="{employer}">'),
                           ("an end tag running past a quoted >", f'<title>Unrelated Corp</title a=">"{employer}">'),
                           ("a key only another reading trims", f'{employer}<meta property="og:site_name\xa0" content="Unrelated Corp">'),
                           ("a content only another reading trims", f'{employer}<meta property="og:site_name" content="\x1c">')):
            with self.subTest(case=case):
                self.assertFalse(bv.branded_as(read(html), names))
        for html in (f"<script>x</script >{employer}", f"<script>x</SCRIPT>{employer}", f"<script>x</script/>{employer}",
                     f"<script><!-- x --></script>{employer}", f"<script><!--<script></script>--></script>{employer}",
                     f"<script><!--<script></script></script>{employer}", f"<style><!--</style>{employer}",
                     f"<title>Home</title >{employer}", f"<textarea>x</TEXTAREA>{employer}",
                     '<meta property = "og:site_name" content = "Example Health">', "<meta property=og:site_name content='Example Health'>",
                     '<META PROPERTY="OG:SITE_NAME" CONTENT="Example Health"/>', '<meta property="og:site_name" content="Example&nbsp;Health">',
                     '<meta\tproperty="og:site_name"\ncontent="Example Health">'):
            with self.subTest(control=html):
                self.assertTrue(bv.branded_as(read(html), names))

    def test_a_page_a_browser_tokenizes_differently_never_admits_another_companys_board(self):
        """End to end, Codex Pass 8, from a lead's or the curated list's hint: the contractor's homepage carries an "Example
        Health" site name that only Python's tokenizer read (after U+000B or U+00A0, after "</title[U+00A0]>", in a
        double-escaped script, after "</ title>" or "=="). No posting mentions Example Health. The contractor's nameless
        board waits for your review; never quarantined or active, no employer admitted."""
        tag = site_name_tag("og:site_name", "Example Health")
        nav = '<nav><a href="/careers">Careers</a></nav>'
        head = lambda inner: f"<html><head>{inner}</head><body>{nav}</body></html>"
        homes = {"U+000B between attributes": head('<meta property="og:site_name"\x0bcontent="Example Health">'),
                 "U+00A0 in a title's end tag": head(f"<title>Unrelated Corp</title\xa0>{tag}"),
                 "a double-escaped script": head(f"<script><!--<script></script>{tag}</script>"),
                 "whitespace after </": head(f"<title>Unrelated Corp</ title>{tag}"),
                 "==": head('<meta property="og:site_name" content=="Example Health">')}
        for origin in ("lead", "curated"):
            for case, home in homes.items():
                with self.subTest(origin=origin, case=case):
                    store, m, validated = self.check("lever", origin, home_html=home, mentions=False)
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_the_source_scan_stays_linear_when_tags_run_into_one_another(self):
        """The source scan reads each "<meta" as a browser would read a tag beginning there, so tags left open (a quoted
        value never closed, no ">") can run into one another. A budget keeps it linear: a page that exhausts it gets an
        unreadable declaration, and waits for your review."""
        from careeros.discovery.site_inspect import UNREADABLE, inspect, source_site_names
        employer = site_name_tag("og:site_name", "Example Health")
        for case, html in (("an open quote", '<meta a="' + "<meta x " * 4_000), ("no >", "<meta a " * 4_000 + employer),
                           ("open quotes", "<meta a='" * 4_000), ("slashes", "<meta /" * 4_000 + employer)):
            with self.subTest(case=case):
                started = time.perf_counter()
                inspection = inspect(html, "https://x.example.com/", "x.example.com")
                self.assertLess(time.perf_counter() - started, 2)
                self.assertFalse(bv.branded_as(inspection, {"example health"}))
        self.assertEqual(source_site_names("<meta a " * 3_000 + employer)[-1], UNREADABLE)
        self.assertEqual(source_site_names(employer * 3), ["Example Health"] * 3)
        for html in ("<p>&#" + "9" * 5_000 + ";</p>" + employer, "<p>&#" + "0" * 5_000 + "65;</p>" + employer):
            with self.subTest(reference=html[:12]):   # Python's unescape raised on a reference over 4,300 digits
                self.assertTrue(bv.branded_as(inspect(html, "https://x.example.com/", "x.example.com"), {"example health"}))

    def test_the_source_scan_counts_every_character_a_read_scans(self):
        """Codex Pass 9: the budget counted a read only as it went, so a "<meta" read that ran to the end of the page (a
        value with no whitespace or ">" after it, or whitespace to the end) or reached its ">" through a long run of
        whitespace cost nothing, and a page of them took time growing with the square of its size (288 KB of "<meta/a=x":
        14 s). Each read now spends what it scanned, and such a page waits for your review."""
        from careeros.discovery.safe_fetch import MAX_BYTES
        from careeros.discovery.site_inspect import UNREADABLE, inspect, source_site_names
        employer = site_name_tag("og:site_name", "Example Health")
        small = {"values to the end": "<meta/a=x" * 3_000, "whitespace to the end": "<meta/" * 200 + " " * 200_000,
                 "whitespace before a >": "<meta/" * 200 + " " * 200_000 + ">"}
        for case, shape in small.items():
            with self.subTest(case=case):
                self.assertEqual(source_site_names(employer + shape), ["Example Health", UNREADABLE])
                self.assertFalse(bv.branded_as(inspect(employer + shape, "https://x.example.com/", "x.example.com"), {"example health"}))
        spaces = " " * (MAX_BYTES - 7_000)
        large = {"values to the end": "<meta/a=x" * 34_000, "whitespace to the end": "<meta/" * 1_000 + spaces,
                 "whitespace before a >": "<meta/" * 1_000 + spaces + ">"}
        for case, shape in large.items():
            with self.subTest(case=case, size=len(shape)):
                started = time.perf_counter()
                inspect(employer + shape, "https://x.example.com/", "x.example.com")
                self.assertLess(time.perf_counter() - started, 2)

    def test_a_repeated_attribute_counts_its_first_value_as_in_a_browser(self):
        """The reader kept the last of a repeated attribute, a browser the first: content="Unrelated Corp" followed by
        content="Example Health" read as Example Health, and a second property attribute made the tag vanish."""
        from careeros.discovery.site_inspect import inspect
        names = {"example health"}
        page = lambda meta: inspect(f"<html><head><title>Example Health | customer story</title>{meta}</head></html>",
                                    "https://x.example.com/", "x.example.com")
        for meta in ('<meta property="og:site_name" content="Unrelated Corp" content="Example Health">',
                     '<meta property="og:site_name" property="og:description" content="Unrelated Corp">'):
            with self.subTest(meta=meta):
                self.assertEqual(page(meta).site_names, ["Unrelated Corp"])
                self.assertFalse(bv.branded_as(page(meta), names))
        self.assertTrue(bv.branded_as(page('<meta property="og:site_name" content="Example Health" content="Unrelated Corp">'), names))

    def test_a_page_sized_malformed_page_is_read_in_linear_time(self):
        """Python 3.12.10's close() rescans an unclosed comment, end tag or declaration at the end of a page in quadratic
        time (CVE-2025-6069): a 1.5 MB page of "<!--" took about 9 minutes. The reader no longer calls it; what is left
        unread at the end is dropped, as HTML5 does at the end of a file."""
        from careeros.discovery.site_inspect import inspect
        for case, body in (("unclosed comments", "<!--" * 50_000), ("unclosed end tags", "</" * 200_000),
                           ("unclosed declarations", "<?" * 200_000),
                           ("deep SVGs and stray end tags (Pass 7)", "<svg>" * 40_000 + "</math>" * 40_000)):
            with self.subTest(case=case):
                started = time.perf_counter()
                inspection = inspect(body, "https://x.example.com/", "x.example.com")
                self.assertLess(time.perf_counter() - started, 2)
                self.assertFalse(bv.branded_as(inspection, {"example health"}))

    def test_the_reader_reads_a_page_the_same_on_every_python_version(self):
        """Python's HTML parser changed after 3.12.10, the version the engine runs. From 3.12.12 its parse_html_declaration
        reads "<![" itself, as a CDATA section anywhere, and its close() reads the end of a page differently. The reader
        reads "<![" itself and never calls close(), so it reads a page the same on every version; the public CI's Linux
        job runs a later one (Python 3.14 review, 2026-11-03)."""
        from html.parser import HTMLParser

        from careeros.discovery import site_inspect
        names = {"example health"}
        employer, unrelated = site_name_tag("og:site_name", "Example Health"), site_name_tag("og:site_name", "Unrelated Corp")
        read = lambda html: site_inspect.inspect(html, "https://x.example.com/", "x.example.com")
        stock_declaration = HTMLParser.parse_html_declaration

        def declaration(parser, i):
            if parser.rawdata.startswith("<![", i):
                raise AssertionError("Python's own parse_html_declaration read a marked section")
            return stock_declaration(parser, i)

        with unittest.mock.patch.object(HTMLParser, "parse_html_declaration", declaration), \
                unittest.mock.patch.object(site_inspect._Collector, "close", side_effect=AssertionError("close() called")):
            marked = read(f"{employer}<![CDATA[ a > {unrelated} ]]>")   # a browser: a comment to the first ">", then the tag
            open_at_the_end = read(f"<title>Home</title>{employer}<!-- {unrelated}")
            doctype = read(f"<!DOCTYPE html>{employer}")
        self.assertEqual(marked.site_names, ["Example Health", "Unrelated Corp"])
        self.assertFalse(bv.branded_as(marked, names))
        for inspection in (open_at_the_end, doctype):
            self.assertTrue(inspection.read_whole)
            self.assertEqual(inspection.site_names, ["Example Health"])
        self.assertFalse(bv.branded_as(open_at_the_end, names))
        self.assertTrue(bv.branded_as(doctype, names))

    def test_a_page_read_in_part_or_an_svg_title_never_admits_another_companys_board(self):
        """End to end, from a lead's or the curated list's hint: the contractor's homepage puts its "Unrelated Corp" site
        name after a marked section, "<!-->" or "</script/>", repeats an attribute, has a malformed link before it, or has an
        empty title and an SVG logo titled "Example Health". No posting mentions Example Health. The contractor's nameless
        board waits for your review; never quarantined or active, no employer admitted."""
        unrelated = site_name_tag("og:site_name", "Unrelated Corp")
        story = "Example Health | customer story"
        worlds = {"a marked section": (story, "<![ CDATA[x]]>" + unrelated),
                  "<!-->": (story, "<!-->" + unrelated + "<!-- analytics -->"),
                  "</script/>": (story, "<script>var a = 1;</script/>" + unrelated),
                  "a repeated attribute": (story, '<meta property="og:site_name" content="Unrelated Corp" content="Example Health">'),
                  "a malformed link": (story, '<link rel="canonical" href="https://[host]/">' + unrelated),
                  "an SVG title": ("", "<svg><title>Example Health</title></svg>")}
        for origin in ("lead", "curated"):
            for case, (home_title, home_meta) in worlds.items():
                with self.subTest(origin=origin, case=case):
                    store, m, validated = self.check("lever", origin, mentions=False, home_title=home_title, home_meta=home_meta)
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_title_or_a_site_name_tag_a_browser_does_not_see_never_admits_another_companys_board(self):
        """End to end, Codex Pass 6 and the Pass 7 change, from a lead's or the curated list's hint: the contractor's homepage
        ends mid-title ("<title>Example Health<Unrelated"), has an SVG title after a stray "</math>", or carries an "Example
        Health" site-name tag only in a textarea or after "<iframe/>", under its own title "Unrelated Corp | Home". No posting
        mentions Example Health. The contractor's nameless board waits for your review; never quarantined or active, no
        employer admitted."""
        employer = site_name_tag("og:site_name", "Example Health")
        nav = '<nav><a href="/careers">Careers</a></nav>'
        homes = {"an unfinished title": "<html><head><title>Example Health<Unrelated",
                 "an SVG title after a stray end tag": f"<html><head></head><body>{nav}<svg></math><title>Example Health</title></svg></body></html>",
                 "a site-name tag in a textarea": f"<html><head><title>Unrelated Corp | Home</title></head><body>{nav}<textarea>{employer}</textarea></body></html>",
                 "a site-name tag after <iframe/>": f"<html><head><title>Unrelated Corp | Home</title></head><body>{nav}<iframe/>{employer}</iframe></body></html>"}
        for origin in ("lead", "curated"):
            for case, home in homes.items():
                with self.subTest(origin=origin, case=case):
                    store, m, validated = self.check("lever", origin, home_html=home, mentions=False)
                    self.assertEqual((m["identity_check"], m["verification_status"], validated), ("unresolved", "review_required", "skipped"))
                    self.assertIsNone(store.one("SELECT 1 FROM board_mappings WHERE verification_status IN ('quarantined','active')"))
                    self.assertEqual(store.one("SELECT COUNT(*) c FROM employers")["c"], 0)

    def test_a_board_whose_name_only_contains_the_employers_waits_for_your_review(self):
        """Under strict identity a named board was the employer's when its name merely contained the employer's: the
        employer's own site linking "Example Health Foundation" or its credit union's board made that board active. Only a
        board named exactly for the employer (legal suffixes aside) is confirmed; a longer name waits for your review."""
        names = {"example health"}
        for board, expected in (("Example Health", "confirmed"), ("Example Health, Inc.", "confirmed"),
                                ("Example Health Foundation", "unresolved"), ("Example Health Federal Credit Union", "unresolved"),
                                ("Unrelated Corp", "mismatch")):
            for branded in (True, False):
                with self.subTest(board=board, branded=branded):
                    self.assertEqual(bv.strict_identity(names, board, site_branded=branded)[0], expected)
        for board, expected in (("Example Health Foundation", ("unresolved", "review_required", "skipped")),
                                ("Example Health, Inc.", ("confirmed", "active", "active"))):
            with self.subTest(board=board, end_to_end=True):
                store, ctx = tbv.setup()
                web, boards = tbv.FakeWeb(), tbv.FakeBoards()
                web.employer_site("examplehealth.com", tbv.EX, '<a href="https://job-boards.greenhouse.io/exhealthgroup">Jobs</a>'
                                                               '<a href="https://job-boards.greenhouse.io/exhealthgroup/jobs/301">Claims</a>')
                boards.greenhouse["exhealthgroup"] = {"name": board, "content": "Learn more at examplehealth.com.", "jobs": [
                    tbv.gh_job(301, "Claims Analyst", "Join the claims team.", "exhealthgroup")]}
                entity_id = reg.ensure_entity_for(store, tbv.EX, domain="examplehealth.com", origin="lead")
                tbv.verify(ctx, web, boards, entity_id)
                client = boards.client()
                validated = bv.validate_board(ctx, "greenhouse:exhealthgroup", client)
                client.close()
                m = reg.mapping(store, "greenhouse:exhealthgroup")
                self.assertEqual((m["identity_check"], m["verification_status"], validated), expected)


# --- Codex F2: an alert's arrival follows the same matcher, admission and identity rules ------------------------------------------
class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"no client access expected: {name}")


class AlertArrivalTests(unittest.TestCase):
    def arrive(self, *, lead_employer="Foo Health", job_employer="Foo Health", admitted=True, onsite=False):
        """Codex's probe: one tracked Claims Analyst job, then an aggregator alert line for a Claims Analyst at the lead's
        employer, Remote (US). The line is never fetched (the client refuses)."""
        from careeros.discovery.alerts import AlertEmail, Link
        store, ctx = setup()
        if admitted:
            board = legacy_board(store, job_employer, "foo-a")
        else:
            board = cli._add_employer(ctx, job_employer, "greenhouse", "foo-a")   # a candidate the scan does not admit
            reg.sync_legacy(store)
        text = CLAIMS.replace("This role is remote in the US.", "This role is on site in Austin, Texas.") if onsite else CLAIMS
        item = gh_item(601, "Claims Analyst", text, "foo-a", location="Austin, TX" if onsite else "Remote - US")
        ingest(ctx, dict(board), [greenhouse_posting("foo-a", item)], source="ats_api" if admitted else "manual_url", full_board=False)
        link = Link("https://www.linkedin.com/jobs/view/9999999601", "linkedin", "Claims Analyst", f"{lead_employer} · Remote (US)", True)
        outcomes = []
        cli._record_alert(ctx, AlertEmail("GitHub inbox (synthetic)", "jobs", None, [link]), NoNetwork(), outcomes)
        row = store.one("SELECT lead_id, status, job_id FROM leads")
        arrived = (row["status"], row["job_id"])
        lr.after_scan(ctx, store.one("SELECT * FROM employers"), complete=False)   # the next scan's lead resolution
        return (*arrived, proposed(store, row["lead_id"])), [o for _, o in outcomes]

    def test_an_alert_line_is_linked_on_arrival_only_by_the_lead_resolution_rules(self):
        # the control: admitted, same employer; never linked by its title, on arrival or after, but proposed (D188)
        self.assertEqual(self.arrive()[0], ("new", None, "greenhouse:foo-a:601"))
        for case in ({"onsite": True}, {"admitted": False}, {"job_employer": "Foo Health Services"}):
            with self.subTest(**case):
                row, outcomes = self.arrive(**case)
                self.assertEqual((row, outcomes), (("new", None, None), ["lead"]))   # open for lead resolution, nothing proposed


# --- Codex F3: one board's complete scan never closes a lead for the whole employer --------------------------------------------
class CoverageTests(unittest.TestCase):
    def two_boards(self, first_matches: bool):
        """Two admitted boards of one employer; the daily scan reads them in name order ("Foo Health" first)."""
        store, ctx = setup()
        legacy_board(store, "Foo Health", "foo-a")
        legacy_board(store, "Foo Health, Inc.", "foo-b")
        add_lead(store, "lead:old", "Eligibility Specialist", "Foo Health · Remote", first_seen=days_ago(store, 60))
        matching, unrelated = ("foo-a", "foo-b") if first_matches else ("foo-b", "foo-a")
        jobs = {matching: [gh_item(502, "Eligibility Specialist", CLAIMS, matching)], unrelated: [gh_item(501, "Claims Analyst", CLAIMS, unrelated)]}
        return store, ctx, jobs, matching

    def test_both_board_orders_link_the_lead(self):
        for first_matches in (False, True):   # Codex's two orders: the unrelated board first, then the matching one first
            with self.subTest(first_matches=first_matches):
                store, ctx, jobs, matching = self.two_boards(first_matches)
                tbv.scan(ctx, jobs.__getitem__)
                lead = lead_row(store, "lead:old")
                self.assertEqual((lead["status"], lead["job_id"], proposed(store, "lead:old")),
                                 ("new", None, f"greenhouse:{matching}:502"))   # proposed to you (D188)

    def test_a_board_not_read_in_this_scan_keeps_the_lead_open_and_full_coverage_closes_it(self):
        from careeros.discovery.http import FetchError
        store, ctx, jobs, matching = self.two_boards(False)
        tbv.scan(ctx, lambda token: FetchError("network:ConnectError", retryable=True) if token == matching else jobs[token])
        lead = lead_row(store, "lead:old")
        self.assertEqual(lead["status"], "no_match")   # the other board failed: not evidence about the whole employer
        self.assertIn("not every admitted board", lead["status_reason"])
        store.clock.advance(days=1)
        tbv.scan(ctx, lambda token: [gh_item(501, "Claims Analyst", CLAIMS, token)])   # both read completely; nothing matches
        self.assertEqual(lead_row(store, "lead:old")["status"], "closed")


# --- Codex F4: a posting you add keeps an ambiguity it does not resolve ------------------------------------------------------------
class ManualIntakeAmbiguityTests(unittest.TestCase):
    def test_adding_one_of_two_same_title_postings_does_not_link_a_lead_either_could_be(self):
        store, ctx = setup()
        board = legacy_board(store, "Foo Health", "foo-a")
        items = [gh_item(701, "Claims Analyst", CLAIMS, "foo-a", req="R-701"), gh_item(702, "Claims Analyst", CLAIMS, "foo-a", req="R-702")]
        ingest(ctx, dict(board), [greenhouse_posting("foo-a", item) for item in items], full_board=True)
        add_lead(store, "lead:either", "Claims Analyst", "Foo Health · Remote")
        self.assertEqual(lr.after_scan(ctx, board, complete=True)["leads_ambiguous"], 1)   # Codex: both are candidates
        add_lead(store, "lead:named", "Claims Analyst", "Foo Health · Remote · req=R-701",   # arrives after that scan
                 url="https://www.indeed.com/viewjob?jk=0000000000000701")
        added = lr.link_same_posting(ctx, "greenhouse:foo-a:701", via="added")
        self.assertEqual(added["leads_likely"], 1)   # only the lead whose requisition picks out 701, proposed (D188)
        self.assertEqual({r["lead_id"]: (r["status"], r["job_id"], proposed(store, r["lead_id"])) for r in store.all("SELECT * FROM leads")},
                         {"lead:either": ("new", None, None), "lead:named": ("new", None, "greenhouse:foo-a:701")})
        self.assertEqual(decisions(store, "lead:either"), ["ambiguous"])


# --- Codex F5 and F6: a confirmation is bound to the job you saw, and to a proposal that is still eligible -------------------------
class StaleProposalTests(unittest.TestCase):
    def proposal(self):
        """Codex's probe: a lead and one likely posting (231) at an admitted board."""
        store, ctx = setup()
        legacy_board(store)
        add_lead(store, "lead:probe", "Remote Claims Specialist", "Foo Health · Remote (US)")
        self.items = [gh_item(231, "Claims Specialist - US", CLAIMS, "foohealth")]
        tbv.scan(ctx, self.items)
        self.assertEqual(review_leads.pending(store, "lead:probe", ctx.policy)["job_id"], "greenhouse:foohealth:231")
        return store, ctx

    def test_a_printed_command_acts_only_on_the_job_it_names(self):
        store, ctx = self.proposal()
        listing = leads_command(ctx)
        confirm_a = "careeros leads confirm lead:probe --job greenhouse:foohealth:231"
        reject_a = "careeros leads reject lead:probe --job greenhouse:foohealth:231"
        self.assertIn(confirm_a, listing)
        self.assertIn(reject_a, listing)
        review_leads.reject_likely(store, "lead:probe", policy=ctx.policy, job_id="greenhouse:foohealth:231", key="reject-original")   # then B replaces A
        store.clock.advance(seconds=1)
        tbv.scan(ctx, self.items + [gh_item(232, "Claims Specialist (Remote)", CLAIMS, "foohealth")])
        self.assertEqual(review_leads.pending(store, "lead:probe", ctx.policy)["job_id"], "greenhouse:foohealth:232")
        for action, job in (("confirm", None), ("confirm", "greenhouse:foohealth:231"), ("reject", None),
                            ("reject", "greenhouse:foohealth:231")):
            with self.subTest(action=action, job=job), self.assertRaises(SystemExit) as refused:
                leads_command(ctx, action, "lead:probe", job=job)
            self.assertIn("job_required" if job is None else "not_the_likely_match", str(refused.exception))
        self.assertEqual((lead_row(store, "lead:probe")["job_id"], review_leads.pending(store, "lead:probe", ctx.policy)["job_id"]),
                         (None, "greenhouse:foohealth:232"))   # B still waits for you, untouched
        with self.assertRaises(ValueError):
            review_leads.confirm_likely(store, "lead:probe", policy=ctx.policy, key="no-job")   # the shared writer, as the web uses it

    def test_a_proposal_whose_board_left_the_daily_scan_is_hidden_and_cannot_be_confirmed(self):
        """Codex's F6 probe: the card is rendered, then the board is paused (an identity contradiction); its old Confirm,
        and the command, record nothing, and the proposal is no longer shown."""
        store, ctx = self.proposal()
        token, target = "lead-f6-token", "lead:probe|greenhouse:foohealth:231"
        with tempfile.TemporaryDirectory() as tmp:
            server = HTTPServer(("127.0.0.1", 0), make_handler(store, MID, POLICY, token, Path(tmp) / "writer.lock", {}))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                key = next(k for a, t, k in forms(httpx.get(f"{base}/?t={token}").text) if a == "lead_confirm" and t == target)
                reg.pause(store, store.one("SELECT mapping_id FROM board_mappings")["mapping_id"], "independent identity contradiction",
                          status="verification_required", failure="identity_mismatch")
                self.assertFalse(reg.scan_gate(store, store.one("SELECT * FROM employers"), ctx.policy, store.clock.now())[0])
                reply = httpx.post(f"{base}/lead_confirm?t={token}", data={"target": target, "key": key})
                self.assertEqual(reply.status_code, 400)
                self.assertNotIn("Likely matches for your leads", httpx.get(f"{base}/?t={token}").text)
            finally:
                server.shutdown()
                server.server_close()
        with self.assertRaises(SystemExit) as refused:
            leads_command(ctx, "confirm", "lead:probe", job="greenhouse:foohealth:231")
        self.assertIn("no_likely_match", str(refused.exception))
        self.assertNotIn("LIKELY MATCH", leads_command(ctx))
        self.assertEqual((lead_row(store, "lead:probe")["status"], lead_row(store, "lead:probe")["job_id"]), ("new", None))
        self.assertEqual(decisions(store, "lead:probe"), ["likely"])   # nothing recorded

    def test_a_proposal_whose_employer_no_longer_matches_the_lead_is_hidden(self):
        store, ctx = self.proposal()
        store.writer("discovery").update("jobs", {"employer_name": "Foo Health Services", "parent": "Foo Health Services"},
                                         "job_id = 'greenhouse:foohealth:231'")   # the posting now names another employer
        self.assertIsNone(review_leads.pending(store, "lead:probe", ctx.policy))
        with self.assertRaises(ValueError):
            review_leads.confirm_likely(store, "lead:probe", policy=ctx.policy, job_id="greenhouse:foohealth:231", key="k-other")
        self.assertIsNone(lead_row(store, "lead:probe")["job_id"])


# --- item 8: the correction of a lead linked to another lead's job, by an audited owner command ----------------------------------
class CorrectionTests(unittest.TestCase):
    def test_unlink_corrects_a_lead_linked_to_another_leads_job_with_an_audit_event_and_the_rematch_finds_its_own(self):
        """The live defect of gap 2: discovery matched a second lead with the first lead's facts and linked it to the first
        lead's job. The correction is yours (careeros leads unlink, with a reason); the build never applies it."""
        store, ctx = setup()
        legacy_board(store)
        items = [gh_item(401, "Claims Analyst", CLAIMS, "foohealth"), gh_item(402, "Eligibility Specialist", CLAIMS, "foohealth")]
        tbv.scan(ctx, items)
        add_lead(store, "lead:first", "Claims Analyst", "Foo Health · Remote")
        add_lead(store, "lead:second", "Eligibility Specialist", "Foo Health · Remote")
        old = store.writer("discovery")   # the state the old discovery path left: both leads on the first lead's job
        for lead_id in ("lead:first", "lead:second"):
            old.update("leads", {"status": "ingested", "job_id": "greenhouse:foohealth:401"}, "lead_id = ?", (lead_id,))
            old.insert("lead_matches", {"match_id": f"lm:old-{lead_id}", "lead_id": lead_id, "job_id": "greenhouse:foohealth:401",
                                        "mapping_id": "greenhouse:foohealth", "method": "canonical_link", "decision": "matched",
                                        "candidates_json": '["401"]', "decided_at": store.clock.now()})
        with self.assertRaises(SystemExit) as refused:
            leads_command(ctx, "unlink", "lead:second")   # a reason is required
        self.assertIn("reason_required", str(refused.exception))
        store.clock.advance(minutes=5)
        out = leads_command(ctx, "unlink", "lead:second", reason="D68: linked to another lead's job (gap 2)")
        self.assertIn("unlinked from greenhouse:foohealth:401", out)
        second = lead_row(store, "lead:second")
        self.assertEqual((second["status"], second["job_id"]), ("new", None))
        self.assertIn("D68: linked to another lead's job", second["status_reason"])
        row = store.one("SELECT * FROM lead_matches WHERE lead_id = 'lead:second' AND decision = 'unlinked'")
        self.assertEqual((row["method"], row["actor"], row["job_id"]), ("owner", "user", "greenhouse:foohealth:401"))
        self.assertTrue(store.one("SELECT 1 FROM lead_matches WHERE match_id = 'lm:old-lead:second'"))   # the history is kept
        event = store.one("SELECT * FROM events WHERE event_type = 'lead_unlinked'")
        self.assertEqual((event["entity_id"], event["actor_type"], json.loads(event["metadata_json"])["job_id"]),
                         ("lead:second", "human", "greenhouse:foohealth:401"))
        self.assertEqual(store.verify_event_chain(), [])
        self.assertEqual(lead_row(store, "lead:first")["job_id"], "greenhouse:foohealth:401")   # the other lead is untouched
        with self.assertRaises(ValueError) as twice:
            review_leads.unlink_lead(store, "lead:second", reason="again", key="k-u2")
        self.assertIn("not_linked", str(twice.exception))
        retitled = gh_item(401, "Eligibility Specialist", CLAIMS, "foohealth")   # even when the job you unlinked matches its title
        tbv.scan(ctx, [retitled, items[1]])   # the next scan matches it with its own facts, never back to that job
        second = lead_row(store, "lead:second")
        self.assertEqual((second["status"], second["job_id"], proposed(store, "lead:second")),
                         ("new", None, "greenhouse:foohealth:402"))   # proposed to you (D188)
        self.assertEqual(json.loads(store.one("SELECT candidates_json FROM lead_matches WHERE lead_id = 'lead:second' "
                                              "AND decision = 'likely' AND actor = 'careeros'")["candidates_json"]),
                         ["greenhouse:foohealth:402"])

    def test_an_unlink_is_recorded_once_for_one_key(self):
        store, ctx = setup()
        legacy_board(store)
        tbv.scan(ctx, [gh_item(411, "Claims Analyst", CLAIMS, "foohealth")])
        add_lead(store, "lead:u", "Claims Analyst", "Foo Health · Remote", url="https://job-boards.greenhouse.io/foohealth/jobs/411")   # its own link
        tbv.scan(ctx, [gh_item(411, "Claims Analyst", CLAIMS, "foohealth")])
        self.assertEqual(review_leads.unlink_lead(store, "lead:u", reason="not this one", key="k-once"), "new")
        self.assertEqual(review_leads.unlink_lead(store, "lead:u", reason="not this one", key="k-once"), "replay")
        self.assertEqual(store.one("SELECT COUNT(*) c FROM events WHERE event_type = 'lead_unlinked'")["c"], 1)


# --- schema 15: lead states, lead-match decisions, the domain's origin ----------------------------------------------------------
class SchemaTests(unittest.TestCase):
    def test_a_schema_14_database_migrates_forward_keeping_every_lead_and_match(self):
        store = Store(":memory:", FixedClock("2026-11-02T12:00:00.000Z"))
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            if int(script.name.split("_", 1)[0]) <= 14:
                store.conn.executescript("BEGIN;\n" + script.read_text(encoding="utf-8") + "\nCOMMIT;")
        store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '14')")
        now = store.clock.now()
        store.conn.execute("INSERT INTO jobs (job_id, workflow_id, employer_name, parent, source, ats, external_id, title, title_norm, first_seen_at, "
                           "last_seen_at, content_fingerprint, current_state, state_sequence, entered_state_at) VALUES "
                           "('lever:synth:1', 'job:lever:synth:1', 'Synth Co', 'Synth Co', 'manual_url', 'lever', '1', 'Claims Analyst', 'claims analyst', ?, ?, "
                           "'c', 'discovered', 0, ?)", (now, now, now))
        for lead_id, status, job_id, profile in (("lead:1", "new", None, "primary"), ("lead:2", "ingested", "lever:synth:1", "night"),
                                                 ("lead:3", "dismissed", None, "primary")):
            store.conn.execute("INSERT INTO leads (lead_id, url, site, title, title_norm, family, context, sender, subject, received_at, "
                               "first_seen_at, status, job_id, profile) VALUES (?, 'https://www.linkedin.com/jobs/view/1', 'linkedin', "
                               "'Claims Analyst', 'claims analyst', 'claims_analysis', 'Synth Co · Remote', 'GitHub inbox', NULL, NULL, "
                               "?, ?, ?, ?)", (lead_id, now, status, job_id, profile))
        store.conn.execute("INSERT INTO lead_matches (match_id, lead_id, job_id, mapping_id, method, decision, candidates_json, decided_at, "
                           "audit, audited_at) VALUES ('lm:1', 'lead:2', 'lever:synth:1', 'lever:synth', 'canonical_link', 'matched', "
                           "'[\"1\"]', ?, 'correct', ?)", (now, now))
        before = {t: [tuple(r) for r in store.conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("leads", "lead_matches")}
        self.assertEqual(store.migrate(), newest_schema())   # through 015, and the migrations after it
        after_leads = [tuple(r)[:14] for r in store.conn.execute("SELECT * FROM leads ORDER BY 1")]
        after_matches = [tuple(r)[:10] for r in store.conn.execute("SELECT * FROM lead_matches ORDER BY 1")]
        self.assertEqual((after_leads, after_matches), (before["leads"], before["lead_matches"]))   # every row and value kept
        self.assertEqual(store.one("SELECT COUNT(*) c FROM leads WHERE status_reason IS NOT NULL OR fetch_failure IS NOT NULL")["c"], 0)
        store.conn.execute("UPDATE leads SET status = 'no_match', status_reason = 'r', status_at = ? WHERE lead_id = 'lead:1'", (now,))
        store.conn.execute("UPDATE leads SET status = 'closed' WHERE lead_id = 'lead:3'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE leads SET status = 'linked' WHERE lead_id = 'lead:1'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE leads SET job_id = 'nowhere' WHERE lead_id = 'lead:1'")   # the job reference is kept
        store.conn.execute("INSERT INTO lead_matches (match_id, lead_id, method, decision, decided_at, actor, reason) VALUES "
                           "('lm:2', 'lead:1', 'likely_title', 'likely', ?, 'careeros', 'one near-identical title')", (now,))
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO lead_matches (match_id, lead_id, method, decision, decided_at) VALUES "
                               "('lm:3', 'lead:1', 'guess', 'matched', ?)", (now,))
        with self.assertRaises(sqlite3.DatabaseError):   # still append-only
            store.conn.execute("DELETE FROM lead_matches WHERE match_id = 'lm:1'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO employer_entities (entity_id, canonical_name, canonical_norm, created_at, updated_at, "
                               "domain_origin) VALUES ('ent:1', 'Synth Co', 'synth co', ?, ?, 'search')", (now, now))
        self.assertEqual(store.migrate(), newest_schema())   # once only

    def test_your_review_actions_change_a_leads_state_and_link_only(self):
        """Separation of duties (ADR 0008): the review component may set a lead's state, reason and link (your
        confirmations and corrections), never what its source said; lead_matches rows are never edited by discovery."""
        from careeros.db.store import PermissionDenied
        store, _ctx = setup()
        add_lead(store, "lead:s", "Claims Analyst", "Foo Health · Remote")
        store.writer("review").update("leads", {"status_reason": "checked", "status_at": store.clock.now()}, "lead_id = 'lead:s'")
        for column in ("url", "title", "context", "fetch_failure", "first_seen_at", "sender"):
            with self.assertRaises(PermissionDenied):
                store.writer("review").update("leads", {column: "x"}, "lead_id = 'lead:s'")
        for writer, column in (("review", "decision"), ("review", "reason"), ("discovery", "reason"), ("discovery", "audit")):
            with self.assertRaises(PermissionDenied):
                store.writer(writer).update("lead_matches", {column: "x"}, "1 = 1")


if __name__ == "__main__":
    unittest.main()
