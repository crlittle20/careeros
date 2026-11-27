"""The coordinating session's wide red team on f56ed53 before Codex Pass 15 (the owner's D185 and D186, 2026-11-19):
batch 1, RT-01 to RT-34 (codex-review-2026-11-15/red-team-coordinator-2026-11-18/BATCH_1.md). Each test fails on
f56ed53 and passes on its correction. Synthetic data only."""
import json
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.discovery.boards import greenhouse_posting
from careeros.pipeline import ingest, process
from careeros.review import assist, packages
from careeros.review.labels import record_label

from tests.test_assist import APP, GREENHOUSE_FILL
from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store
from tests.test_v26_discovery import CLAIMS, gh_item
from tests.helpers import give_label

SYNTH = {"employer_id": None, "name": "Synthetic Health", "parent": "Synthetic Health"}


class LabelAndHandoffTests(unittest.TestCase):
    """RT-01 to RT-05: a handoff never outlives its session or its label, and a label stands for the opening it was
    given for."""

    def setUp(self):
        self.vault = Vault()
        self.store, self.ctx = posting_store()
        self.fill = mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL)
        self.fill.start()
        # the inert browser-Fill code is tested with the pause off (D191 pauses it in this release)
        self.unpause = mock.patch.object(assist, "FILL_PAUSED", False)
        self.unpause.start()
        self.addCleanup(self.unpause.stop)

    def tearDown(self):
        self.fill.stop()
        self.vault.close()

    def _open(self, key="o1", job=JOB):
        give_label(self.store, POLICY, MID, job, "apply", None, f"label-{key}")
        packages.prepare(self.store, POLICY, MID, job, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        return assist.open_session(self.store, POLICY, MID, job, "assisted", facts=FACTS, library=self.vault.library,
                                   vault_root=self.vault.root, questions=QUESTIONS, key=key)

    def _process(self):
        with mock.patch("builtins.print"):
            return cli.cmd_process(self.ctx, {"root": self.vault.root}, SimpleNamespace())

    def test_a_closed_sessions_handoff_is_withdrawn_and_never_replayed(self):
        """RT-01: a session closed not submitted keeps no handoff after a Skip, and its key never returns one."""
        first = self._open()
        handoff = self.vault.root / first["handoff"]
        assist.close_session(self.store, APP, POLICY, first["session_id"], "not_submitted", key="c1")
        record_label(self.store, JOB, "skip", "not for me", "label-skip")
        handoff.write_text("{}", encoding="utf-8")   # one an older engine left: the processing run's sweep removes it
        self.assertEqual(self._process().get("handoffs_withdrawn"), 1)
        self.assertFalse(handoff.exists())
        replay = assist.open_session(self.store, POLICY, MID, JOB, "assisted", facts=FACTS, library=self.vault.library,
                                     vault_root=self.vault.root, questions=QUESTIONS, key="o1")
        self.assertTrue(replay["replay"])
        self.assertNotIn("handoff", replay)
        self.assertEqual(replay["state"], "not_submitted")

    def test_closing_a_session_removes_its_handoff(self):
        """RT-01: the extension can't load the handoff of a session you closed."""
        first = self._open()
        handoff = self.vault.root / first["handoff"]
        assist.close_session(self.store, APP, POLICY, first["session_id"], "not_submitted", key="c1",
                             vault_root=self.vault.root)
        self.assertFalse(handoff.exists())

    def test_a_handoff_with_no_recorded_session_is_withdrawn(self):
        """RT-02: an open stopped between writing its handoff and recording its session leaves no loadable file."""
        first = self._open()
        handoff = self.vault.root / first["handoff"]
        orphan = handoff.with_name(f"ses_{uuid.uuid4().hex[:16]}.json")
        orphan.write_bytes(handoff.read_bytes())
        record_label(self.store, JOB, "skip", "not for me", "label-skip")
        self._process()
        self.assertFalse(orphan.exists())

    def test_a_label_given_behind_a_clock_set_back_is_refused(self):
        """RT-03: the label you give last is the label in force, never an older one stamped later."""
        give_label(self.store, POLICY, MID, JOB, "apply", None, "label-apply")
        self.store.clock.advance(minutes=-2)
        with self.assertRaisesRegex(ValueError, "out_of_order_label"):
            record_label(self.store, JOB, "skip", "not for me", "label-skip")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM labels WHERE job_id = ?", (JOB,))["n"], 1)

    def test_a_board_posting_reidentified_after_the_label_waits_for_a_new_one(self):
        """RT-05: the same text under another requisition is another opening for the label."""
        store, ctx = self.store, self.ctx
        text = CLAIMS + "\n\nThis opening supports the appeals team."
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(7, "Claims Analyst", text, "synth", req="R-1101"))],
               full_board=False)
        process(ctx)
        job = "greenhouse:synth:7"
        give_label(store, POLICY, MID, job, "apply", None, "label-7")
        self.assertEqual(packages.route_for(store, POLICY, job, MID)[0], "apply")
        store.clock.advance(minutes=5)
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(7, "Claims Analyst", text, "synth", req="R-2202"))],
               full_board=False)
        route, basis = packages.route_for(store, POLICY, job, MID)
        self.assertIsNone(route)
        self.assertIn("label_suspended", basis)
        process(ctx)   # screened as it is now: a fresh preview is given only then (the D190 design)
        give_label(store, POLICY, MID, job, "apply", "checked: the new requisition", "label-7b")
        self.assertEqual(packages.route_for(store, POLICY, job, MID)[0], "apply")

    def test_an_older_engines_overwrite_suspends_the_label_given_before_it(self):
        """RT-04: a label written by an older engine (no change count) is suspended by a title change recorded after it.
        Since the owner's D188 such a label waits for you to confirm it anyway; once confirmed, a title change suspends
        it again."""
        store = self.store
        store.writer("review").insert("labels", {"label_id": "label:old", "job_id": JOB, "at": store.clock.now(),
                                                 "label": "apply", "reason": None, "system_tier": None,
                                                 "system_decision": "scored", "profile": "primary"})
        store.writer("review").event("job_labelled", outcome="informational", entity_type="job", entity_id=JOB,
                                     actor_type="human", actor_id="user", metadata={"label": "apply"})
        route, basis = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertIn("label_unconfirmed", basis)   # given before the upgrade: confirm it (D188)
        give_label(store, POLICY, MID, JOB, "apply", None, "label-confirmed")
        self.assertEqual(packages.route_for(store, POLICY, JOB, MID)[0], "apply")
        store.clock.advance(minutes=5)
        from careeros.openings import record_change
        with store.tx():   # the posting's title changed (batch 3: compared with the opening the label was given for)
            store.writer("discovery").update("jobs", {"title": "Pharmacy Benefits Specialist",
                                                      "title_norm": "pharmacy benefits specialist"}, "job_id = ?", (JOB,))
            record_change(store, JOB, [{"field": "title", "what": "title", "before": "Claims Analyst",
                                        "after": "Pharmacy Benefits Specialist"}])
        route, basis = packages.route_for(store, POLICY, JOB, MID)
        self.assertIsNone(route)
        self.assertIn("label_suspended", basis)

    def test_new_text_under_another_requisition_suspends_the_label_even_behind_a_clock_set_back(self):
        """RT-04, RT-05: an edit that also moved the requisition was recorded without it, and a change recorded behind a
        clock set back after the label was read as older than the label."""
        store, ctx = self.store, self.ctx
        text = CLAIMS + "\n\nThis opening supports the appeals team."
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(8, "Claims Analyst", text, "synth", req="R-1101"))],
               full_board=False)
        process(ctx)
        job = "greenhouse:synth:8"
        give_label(store, POLICY, MID, job, "apply", None, "label-8")
        store.clock.advance(minutes=5)
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(8, "Claims Analyst", text + " And grievances.", "synth",
                                                               req="R-3303"))], full_board=False)
        route, basis = packages.route_for(store, POLICY, job, MID)
        self.assertIsNone(route)
        self.assertIn("requisition: R-1101 -> R-3303", basis)
        # the same text under another requisition, recorded behind a clock set back after a new label
        process(ctx)   # screened as it is now: a fresh preview is given only then (the D190 design)
        store.clock.advance(minutes=60)   # the label, an hour after that screening
        give_label(store, POLICY, MID, job, "apply", "checked", "label-8b")
        store.clock.advance(minutes=-30)
        ingest(ctx, SYNTH, [greenhouse_posting("synth", gh_item(8, "Claims Analyst", text + " And grievances.", "synth",
                                                               req="R-4404"))], full_board=False)
        route, basis = packages.route_for(store, POLICY, job, MID)
        self.assertIsNone(route)
        self.assertIn("R-3303 -> R-4404", basis)

    def test_a_skip_withdraws_the_handoff_at_once(self):
        """RT-01: `careeros label JOB skip` withdraws an open session's handoff then, not at the next processing run."""
        first = self._open()
        handoff = self.vault.root / first["handoff"]
        with mock.patch("builtins.print"):
            cli.cmd_label(self.ctx, {"root": self.vault.root},
                          SimpleNamespace(job_id=JOB, label="skip", reason="not for me", night=False))
        self.assertFalse(handoff.exists())

    def test_one_jobs_unreadable_questions_never_end_the_package_list(self):
        """RT-40: one empty questions.json ended `careeros package list` for every package."""
        give_label(self.store, POLICY, MID, JOB, "apply", None, "label-q")
        packages.prepare(self.store, POLICY, MID, JOB, facts=FACTS, library=self.vault.library, vault_root=self.vault.root,
                         questions=QUESTIONS)
        path = packages.questions_path(self.vault.root, JOB)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        with mock.patch.object(cli, "_package_inputs", return_value=(FACTS, self.vault.library)), \
                mock.patch("builtins.print") as printed:
            cli.cmd_package(self.ctx, {"root": self.vault.root}, SimpleNamespace(action="list"))
        said = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("questions unreadable", said)
        self.assertIn("1 package(s)", said)

    def test_two_job_ids_one_letters_case_apart_get_two_package_folders(self):
        """RT-12: Windows reads "greenhouse_Synth_1" and "greenhouse_synth_1" as one folder. Since the owner's D188 those
        two are one Greenhouse posting (its id), so two pastes one letter's case apart stand in for them."""
        from careeros.normalize.posting import RawPosting
        store, ctx = self.store, self.ctx
        for external, more in (("R-1101a", ""), ("R-1101A", "\n\nAnother team.")):
            ingest(ctx, SYNTH, [RawPosting(ats="manual", board_token="synthetic-health", external_id=external, title="Claims Analyst",
                                           location="Remote - US", url=None, apply_url=None, description_text=CLAIMS + more,
                                           raw={"source": "human_text"})], source="manual_url", full_board=False)
        process(ctx)
        folders = []
        for job in ("manual:synthetic-health:R-1101a", "manual:synthetic-health:R-1101A"):
            give_label(store, POLICY, MID, job, "apply", None, f"label-{job}")
            folders.append(packages.prepare(store, POLICY, MID, job, facts=FACTS, library=self.vault.library,
                                            vault_root=self.vault.root, questions=QUESTIONS)["folder"].lower())
        self.assertNotEqual(folders[0], folders[1])


def _page(data: dict, head: str = "") -> str:
    return f'<html><head>{head}<script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'


def _posting(**more) -> dict:
    return {"@context": "https://schema.org", "@type": "JobPosting", "title": "Claims Analyst",
            "hiringOrganization": {"@type": "Organization", "name": "Acme Rx"},
            "description": "<p>" + CLAIMS.replace("\n", "</p><p>") + "</p>", **more}


class SavedFileAndPlacementTests(unittest.TestCase):
    """RT-08, RT-10, RT-27 to RT-34: what a saved file or a paste states is read whole before anything decides on it."""

    def test_a_byte_order_mark_never_hides_the_saved_link(self):
        """RT-08: a saved .txt starting with a UTF-8 byte-order mark lost its first line, the URL."""
        import tempfile
        from pathlib import Path
        from careeros.discovery.saved import parse_saved_file
        link = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-44001"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting.txt"
            path.write_bytes(b"\xef\xbb\xbf" + f"URL: {link}\nTitle: Claims Analyst\nEmployer: Acme Rx\n\n{CLAIMS}".encode())
            posting, _ = parse_saved_file(path)
        self.assertEqual(posting.url, link)

    def test_the_canonical_link_is_read_whatever_the_attribute_order(self):
        """RT-08: <link href=... rel="canonical"> and <meta content=... property="og:url"> were not read."""
        from careeros.discovery.saved import parse_saved_html
        link = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_R-44001"
        for head in (f'<link href="{link}" rel="canonical">', f"<meta content='{link}' property='og:url'>"):
            posting, _ = parse_saved_html(_page(_posting(), head), "p.html")
            self.assertEqual(posting.url, link, head)

    def test_a_saved_text_headers_other_lines_are_part_of_the_posting(self):
        """RT-10: "Job ID: R-4402" in a saved .txt header was dropped, so two openings merged as one."""
        from careeros.discovery.saved import parse_saved_text
        a, _ = parse_saved_text(f"Title: Claims Analyst\nEmployer: Acme Rx\nJob ID: R-4401\n\n{CLAIMS}", "a.txt")
        b, _ = parse_saved_text(f"Title: Claims Analyst\nEmployer: Acme Rx\nJob ID: R-4402\n\n{CLAIMS}", "b.txt")
        self.assertEqual((a.requisition_id, b.requisition_id), ("R-4401", "R-4402"))
        self.assertNotEqual(a.description_text, b.description_text)

    def test_every_location_line_is_the_location_field(self):
        """RT-29: of two "Location:" lines a saved .txt kept the last and a paste the first, so line order decided."""
        from careeros.discovery.manual_intake import parse_pasted
        from careeros.discovery.saved import parse_saved_text
        from careeros.normalize.posting import detect_country
        for first, second in (("Calgary, AB, Canada", "Remote - US"), ("Remote - US", "Remote - Canada (Ontario)")):
            saved, _ = parse_saved_text(f"Title: Claims Analyst\nEmployer: Acme Rx\nLocation: {first}\nLocation: {second}\n\n"
                                        f"{CLAIMS}", "a.txt")
            pasted = parse_pasted(f"Claims Analyst\nCompany: Acme Rx\nLocation: {first}\nLocation: {second}\n\n{CLAIMS}")
            for location in (saved.location, pasted["location"]):
                self.assertEqual(location, f"{first}; {second}")
                self.assertEqual(detect_country(location, "", "remote"), "unknown")

    def test_a_long_location_line_is_read_whole(self):
        """RT-28: a pasted Location line cut at 120 characters lost the part naming Canada and read as plainly US."""
        from careeros.discovery.manual_intake import label_location, parse_pasted
        from careeros.normalize.posting import detect_country
        field = ("Remote - US: Arizona, Colorado, Florida, Idaho, Indiana, Kansas, Kentucky, Michigan, Missouri, Nevada, "
                 "Ohio, Texas, Utah; Remote - Canada (Ontario)")
        pasted = parse_pasted(f"Claims Analyst\nCompany: Acme Rx\nLocation: {field}\n\n{CLAIMS}")
        self.assertEqual((pasted["location"], label_location(pasted["text"])), (field, field))
        self.assertEqual(detect_country(pasted["location"], "", "remote"), "unknown")

    def test_where_a_remote_applicant_may_live_is_part_of_the_location(self):
        """RT-27: applicantLocationRequirements (Canada only) and an address given as text were dropped: placed in the US."""
        from careeros.discovery.saved import parse_saved_html
        from careeros.normalize.posting import detect_country
        miami = {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Miami", "addressRegion": "FL",
                                               "addressCountry": "US"}}
        for more in ({"jobLocation": miami, "applicantLocationRequirements": {"@type": "Country", "name": "Canada"}},
                     {"jobLocation": [miami, {"@type": "Place", "address": "Toronto, ON, Canada"}]}):
            posting, _ = parse_saved_html(_page(_posting(jobLocationType="TELECOMMUTE", **more)), "p.html")
            self.assertIn("Canada", posting.location)
            self.assertNotEqual(detect_country(posting.location, "", "remote"), "US")

    def test_a_job_location_given_as_text_never_stops_the_import(self):
        """RT-33: jobLocation as Text raised AttributeError and stopped every import; any unreadable page goes to failed."""
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from careeros.discovery import saved
        from careeros.discovery.saved import parse_saved_html
        from careeros.paths import vault_paths
        from tests.test_v26_discovery import setup
        posting, _ = parse_saved_html(_page(_posting(jobLocation="Toronto, ON, Canada")), "p.html")
        self.assertEqual(posting.location, "Toronto, ON, Canada")
        store, ctx = setup()
        real = saved.parse_saved_file

        def odd(path):
            if path.name.startswith("a-"):
                raise AttributeError("'str' object has no attribute 'get'")
            return real(path)
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            paths["inbox"].mkdir(parents=True, exist_ok=True)
            (paths["inbox"] / "a-odd.html").write_text(_page(_posting()), encoding="utf-8")
            (paths["inbox"] / "b-good.txt").write_text(f"URL: https://careers.acme.example/jobs/2\nTitle: Claims Analyst\n"
                                                       f"Employer: Acme Rx\n\n{CLAIMS}", encoding="utf-8")
            with mock.patch.object(saved, "parse_saved_file", odd), contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((paths["inbox"] / "failed").rglob("a-odd.html")))
            self.assertTrue(list((paths["inbox"] / "imported").rglob("b-good.txt")))

    def test_json_ld_is_parsed_before_its_entities_are_read(self):
        """RT-34: '&quot;' inside a JSON-LD string made the page unreadable."""
        from careeros.discovery.saved import parse_saved_html
        page = _page(_posting()).replace("Claims Analyst", "Claims Analyst &quot;members first&quot;", 1)
        posting, _ = parse_saved_html(page, "p.html")
        self.assertIn("members first", posting.title)

    def test_the_employer_is_read_by_reference_or_the_page_is_refused(self):
        """RT-32: hiringOrganization {"@id": ...} imported as 'Unknown employer', merging employers and passing the gate."""
        from careeros.discovery.saved import SavedPostingError, parse_saved_html
        graph = {"@context": "https://schema.org", "@graph": [
            {"@type": "Organization", "@id": "#org", "name": "Northgate Health"},
            {**_posting(), "hiringOrganization": {"@id": "#org"}}]}
        _, employer = parse_saved_html(_page(graph), "p.html")
        self.assertEqual(employer, "Northgate Health")
        with self.assertRaises(SavedPostingError):
            parse_saved_html(_page({**_posting(), "hiringOrganization": {"@id": "#elsewhere"}}), "p.html")

    def test_a_workable_jobs_country_abroad_is_written_beside_a_us_state_holding_its_name(self):
        """RT-30: country MX with 'Albuquerque, New Mexico' (or IN with 'Indianapolis, Indiana') read plainly US."""
        from careeros.discovery.boards import workable_posting
        from careeros.normalize.posting import detect_country
        for country, city, region in (("MX", "Albuquerque", "New Mexico"), ("IN", "Indianapolis", "Indiana")):
            posting = workable_posting("synth", {"shortcode": "W1", "title": "Claims Analyst", "telecommuting": True,
                                                 "country": country, "description": "<p>x</p>",
                                                 "locations": [{"city": city, "region": region, "country": "US"}]})
            self.assertEqual(detect_country(posting.location, "", "remote"), "unknown", posting.location)

    def test_the_title_never_places_a_posting_abroad_by_a_us_region_and_only_takes_the_us_away(self):
        """RT-31: '- New Mexico', '- New England' and '- London Market' titles at 'Remote - US' were rejected outside_us."""
        from careeros.normalize.posting import detect_country, placed_country
        self.assertEqual(placed_country("Claims Analyst - New Mexico", "Remote - US", "", "remote"), "US")
        self.assertEqual(placed_country("Claims Analyst - New England", "Remote - US", "", "remote"), "US")
        self.assertEqual(placed_country("Claims Analyst - London Market", "Remote - US", "", "remote"), "unknown")
        self.assertNotEqual(detect_country("Remote - New England", "", "remote"), "non_US")
        # the title never sets non_US at all (the red team's fr1-boards-3: "Melbourne, FL" titles): the location question
        self.assertEqual(placed_country("Claims Analyst - UK", "Remote", "", "remote"), "unknown")
        self.assertEqual(placed_country("Claims Analyst", "London, UK", "", "onsite"), "non_US")   # the field still does


def _tracked(key, title, url, *, req=None, location="Remote - US", remote="remote", employer="Acme Rx", parent=None):
    import dataclasses
    from careeros.discovery.board_discovery import TrackedPosting
    posting = TrackedPosting(key, title, url, req, key.rsplit(":", 1)[-1], location, remote)
    try:
        return dataclasses.replace(posting, employer=employer, parent=parent or employer)
    except TypeError:   # an engine before the employer field (f56ed53, for the pairing check)
        return posting


def _candidate(title="Claims Analyst", *, url=None, req=None, location="Remote - US", employer="Acme Rx"):
    from careeros.discovery.board_discovery import Candidate
    return Candidate(employer_name=employer, origin="alert", posting_url=url, requisition_id=req, title=title,
                     location=location)


def _decision(candidate, postings) -> str:
    from careeros.discovery.board_discovery import match_lead
    return match_lead(candidate, postings, POLICY, "2026-10-12T12:00:00.000Z")["decision"]


WD = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Analyst_"


class LinkAndRequisitionTests(unittest.TestCase):
    """RT-06, RT-07, RT-09, RT-11, RT-12: a link names a posting only by itself, and a requisition is read whole."""

    def _add(self, url, body, title="Claims Analyst"):
        import tempfile
        from tests.test_intake_identity import add_url
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, url, body, title=title)
            first = store.one("SELECT job_id, description_text FROM jobs")
            add_url(ctx, tmp, url, body + "\nThis opening supports pharmacy payment appeals.", title=title)
        return store, first

    def test_a_link_that_is_not_a_full_address_is_a_shared_page(self):
        """RT-06: at a careers link written without its scheme, or as mailto:, a second opening overwrote the first."""
        from tests.test_intake_identity import BODY
        for url in ("careers.acmerx.example/join", "mailto:careers@example.com", "/careers/join"):
            store, first = self._add(url, BODY)
            self.assertEqual(store.one("SELECT COUNT(*) n FROM jobs")["n"], 2, url)
            self.assertEqual(store.one("SELECT description_text FROM jobs WHERE job_id = ?", (first["job_id"],))[0],
                             first["description_text"], url)

    def test_a_job_sites_id_after_the_hash_names_no_posting(self):
        """RT-07: a LinkedIn or Indeed id after "#" named one posting, but the link kept for identity dropped it."""
        from careeros.discovery.board_discovery import names_one_posting
        self.assertFalse(names_one_posting("https://www.linkedin.com/jobs/search/#currentJobId=4000000111"))
        self.assertFalse(names_one_posting("https://www.indeed.com/jobs?q=claims#vjk=0a1b2c3d4e5f6a7b"))
        self.assertTrue(names_one_posting("https://www.linkedin.com/jobs/search/?currentJobId=4000000111"))   # control

    def test_a_requisition_is_read_whole_by_its_label_or_not_at_all(self):
        """RT-09, RT-11, RT-12: prose, a word's tail, a prefix, the next line or a sentence's period were read."""
        from careeros.discovery.manual_intake import requisition_in
        for text, value in (("email us and include the Job ID and title of the role", None),
                            ("Sign up for job notifications", None), ("quote the job number shown below", None),
                            ("Job Identification: 2400123", "2400123"), ("Job No. 2026/0412", "2026/0412"),
                            ("Requisition ID: 2026–0412", "2026–0412"), ("Job ID: REQ 4401", "REQ 4401"),
                            ("Job ID:\nResponsibilities", None), ("Job ID: R-4401.", "R-4401"),
                            ("Req: R-8102", "R-8102"), ("Requisition R-8102", "R-8102"), ("Req. ID: R-8102", "R-8102"),
                            ("Job Requisition: R-8102", "R-8102"), ("Position ID: R-8102", "R-8102"),
                            ("Vacancy ID: R-8102", "R-8102"), ("Job ID: R-1101 and Job ID: R-1102", None),
                            ("Job ID:\n4402 claims", None), ("quote the job number when 2 calls fail", None)):
            self.assertEqual(requisition_in(text), value, text)

    def test_a_footer_naming_the_job_id_never_makes_two_openings_one(self):
        """RT-09: "include the Job ID and title" read as the requisition "and", so a second opening overwrote the first."""
        from tests.test_intake_identity import BODY
        store, _ = self._add("https://careers.acmerx.example/join",
                             BODY + "\nNeed an accommodation? Email us and include the Job ID and title of the role.")
        self.assertEqual(store.one("SELECT COUNT(*) n FROM jobs")["n"], 2)

    def test_a_card_cut_before_its_requisition_keeps_it(self):
        """RT-11: a card's text was cut at 160 characters before its requisition, so two openings became one lead."""
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.board_discovery import lead_requisition
        teaser = ("A role on our claims team reviewing appeals, reconciling payments and answering member questions "
                  "for our members across every plan we administer, with a team that values careful work")
        url = "https://careers.acmehealth.example/jobs"
        body = "".join(f'<p><a href="{url}">Claims Analyst</a> Acme Health · Remote · {teaser} · '
                       f"Job ID: {req}</p>" for req in ("R-8101", "R-8102"))
        mail = ("From: jobs@example.com\r\nTo: someone@example.com\r\nSubject: New jobs\r\nMIME-Version: 1.0\r\n"
                "Content-Type: text/html; charset=utf-8\r\n\r\n<html><body>" + body + "</body></html>").encode()
        links = parse_eml_bytes(mail).links
        self.assertEqual(sorted(lead_requisition(lk.context) for lk in links), ["R-8101", "R-8102"])

    def test_two_job_ids_never_share_a_package_folder(self):
        """RT-12: "R-4401." and "R-4401" were one folder on Windows, so one of the two jobs could never be prepared."""
        windows = lambda name: name.rstrip(". ").lower()   # how Windows reads a folder name
        dotted, plain = packages.job_folder("manual:acme-rx:R-4401."), packages.job_folder("manual:acme-rx:R-4401")
        self.assertEqual(windows(dotted), dotted.lower())   # a name Windows keeps as it is
        self.assertNotEqual(windows(dotted), windows(plain))
        self.assertEqual(packages.job_folder("greenhouse:synth:1"), "greenhouse_synth_1")   # unchanged for a plain id


class DuplicateTests(unittest.TestCase):
    """RT-13 to RT-18: two postings are never one job, and one opening's application never holds another's package."""

    def _board(self, name="Acme Rx", token="acmerx", parent=None):
        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        employer = dict(legacy_board(store, name=name, token=token))
        if parent:
            employer["parent"] = parent
        return store, ctx, employer

    def _state(self, store, job_id):
        return store.one("SELECT current_state FROM jobs WHERE job_id = ?", (job_id,))[0]

    def test_a_title_word_that_starts_with_job_req_or_id_is_kept(self):
        """RT-13: dedupe_key removed 'Job Coach', 'Identity', 'Idaho', so distinct titles were one key."""
        from careeros.normalize.posting import dedupe_key
        self.assertNotEqual(dedupe_key("acme", "job coach", "remote"), dedupe_key("acme", "job developer", "remote"))
        self.assertNotEqual(dedupe_key("acme", "claims analyst idaho", "remote"), dedupe_key("acme", "claims analyst", "remote"))
        self.assertEqual(dedupe_key("acme", "claims analyst req 4401", "remote"), dedupe_key("acme", "claims analyst", "remote"))

    def test_two_board_postings_with_one_text_are_two_jobs(self):
        """RT-14: Greenhouse 4400001 and 4400002 with the same title, place and text: the second became a duplicate."""
        store, ctx, employer = self._board()
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(n, "Claims Analyst", CLAIMS, "acmerx"))
                               for n in (4400001, 4400002)], full_board=False)
        self.assertNotEqual(self._state(store, "greenhouse:acmerx:4400002"), "duplicate")

    def test_two_employers_under_one_parent_are_never_one_job(self):
        """RT-15: two employers' postings under a common parent, same title, place and text: one became a duplicate."""
        from careeros.normalize.posting import RawPosting
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        for token, name, n in (("acmetx", "Acme Health Texas", 5500001), ("acmefl", "Acme Health Florida", 5500002)):
            ingest(ctx, {"employer_id": None, "name": name, "parent": "Acme Holdings"},
                   [greenhouse_posting(token, gh_item(n, "Claims Analyst", CLAIMS, token))], full_board=False)
        self.assertNotEqual(self._state(store, "greenhouse:acmefl:5500002"), "duplicate")
        # ...and the same posting text pasted for each with no link: only the employer tells them apart
        store, ctx = setup()
        for token, name in (("acmetx", "Acme Health Texas"), ("acmefl", "Acme Health Florida")):
            ingest(ctx, {"employer_id": None, "name": name, "parent": "Acme Holdings"},
                   [RawPosting(ats="manual", board_token=token, external_id="p1", title="Claims Analyst", location="Remote - US",
                               url=None, apply_url=None, description_text=CLAIMS, raw={})], full_board=False)
        self.assertNotEqual(self._state(store, "manual:acmefl:p1"), "duplicate")

    def test_a_board_link_with_another_app_route_is_linked_for_review_never_a_duplicate(self):
        """The round-16 hand check: since D184 only a board's posting link reaches the same-link duplicate rule, so its "#"
        route check was untested. Since the owner's D188 the board's posting id is the posting: a "#" part names no other
        one, so the second is the first's duplicate; another posting id stays its own job, linked for review."""
        from careeros.normalize.posting import RawPosting
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        link = "https://job-boards.greenhouse.io/acmerx/jobs/4400001"
        ingest(ctx, {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"},
               [RawPosting(ats="manual", board_token="acme-rx", external_id=f"r{n}", title="Claims Analyst",
                           location="Remote - US", url=f"{link}#/opening/{n}", apply_url=None,
                           description_text=CLAIMS + f"\n\nOpening {n}.", raw={}) for n in (1, 2)], full_board=False)
        # one posting, one record (the red team's batch 3): the second is written into the first, no row of its own
        self.assertIsNone(store.one("SELECT 1 FROM jobs WHERE job_id = 'manual:acme-rx:r2'"))
        self.assertIn("Opening 2.", store.one("SELECT description_text FROM jobs WHERE job_id = 'manual:acme-rx:r1'")[0])
        ingest(ctx, {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"},
               [RawPosting(ats="manual", board_token="acme-rx", external_id="r3", title="Claims Analyst", location="Remote - US",
                           url=link[:-1] + "2", apply_url=None, description_text=CLAIMS, raw={})], full_board=False)
        self.assertNotEqual(self._state(store, "manual:acme-rx:r3"), "duplicate")
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = 'manual:acme-rx:r3' AND relation = 'possible_duplicate'"))

    def test_another_level_or_a_placeholder_requisition_is_another_opening(self):
        """RT-16: 'Claims Analyst II' and 'III' under one requisition (or 'TBD') became a terminal duplicate."""
        store, ctx, employer = self._board()
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(1, "Claims Analyst II", CLAIMS, "acmerx", req="R-2207")),
                               greenhouse_posting("acmerx", gh_item(2, "Claims Analyst III", CLAIMS, "acmerx", req="R-2207")),
                               greenhouse_posting("acmerx", gh_item(3, "Claims Analyst", CLAIMS, "acmerx", req="TBD")),
                               greenhouse_posting("acmerx", gh_item(4, "Claims Analyst", CLAIMS + "\n\nMedicare duties.",
                                                                    "acmerx", req="TBD"))], full_board=False)
        self.assertNotEqual(self._state(store, "greenhouse:acmerx:2"), "duplicate")
        self.assertNotEqual(self._state(store, "greenhouse:acmerx:4"), "duplicate")

    def test_a_new_requisition_is_never_a_repost_of_a_closed_one(self):
        """RT-17: a new opening with its own requisition became repost_of a closed one by title and place alone."""
        store, ctx, employer = self._board()
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(1, "Claims Analyst", CLAIMS, "acmerx", req="R-1101"))])
        for _ in range(2):
            ingest(ctx, employer, [])
        self.assertEqual(self._state(store, "greenhouse:acmerx:1"), "closed")
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(9, "Claims Analyst", CLAIMS, "acmerx", req="R-2207"))])
        self.assertIsNone(store.one("SELECT repost_of FROM jobs WHERE job_id = 'greenhouse:acmerx:9'")[0])

    def test_an_application_to_one_opening_at_a_shared_page_never_holds_another_title(self):
        """RT-18: an application to 'Billing Specialist' held a package for 'Claims Analyst' at the same careers page."""
        import tempfile
        from careeros.review.applications import application_matrix, preflight, record_application
        from tests.test_intake_identity import BODY, add_url
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, "https://www.acmerx.example/careers", BODY, title="Billing Specialist")
            add_url(ctx, tmp, "https://www.acmerx.example/careers", BODY + "\nClaims work.", title="Claims Analyst")
        jobs = {r["title"]: r["job_id"] for r in store.all("SELECT job_id, title FROM jobs")}
        record_application(store, application_matrix(), jobs["Billing Specialist"], "direct", policy=POLICY, key="a1")
        self.assertNotIn("possible_match_applied", preflight(store, jobs["Claims Analyst"]).codes("hold"))


class LeadMatchingTests(unittest.TestCase):
    """RT-19 to RT-25: a lead joins only the posting it is, by its own link, requisition, title, place and employer."""

    def test_a_leads_own_posting_link_wins_over_the_title(self):
        """RT-19: a lead at Workday R-20412 was linked by its title to the posting at R-20388."""
        self.assertNotEqual(_decision(_candidate(url=WD + "R-20412"), [_tracked("m:1", "Claims Analyst", WD + "R-20388")]),
                            "matched")
        self.assertEqual(_decision(_candidate(url=WD + "R-20412"), [_tracked("m:1", "Claims Analyst", WD + "R-20412")]),
                         "matched")   # the control: its own posting

    def test_two_lines_at_places_that_differ_are_two_leads(self):
        """RT-20: a later line in 'Remote - US' was dropped as seen before behind an earlier one in 'Calgary, AB'."""
        from tests.test_v26_discovery import add_lead, setup
        store, ctx = setup()
        add_lead(store, "lead:1", "Claims Analyst", "Acme Rx · Calgary, AB", url="https://www.indeed.com/viewjob?jk=a1b2c3d4e5f6")
        line = SimpleNamespace(url="https://www.linkedin.com/jobs/view/4012345678", text="Claims Analyst",
                               context="Acme Rx · Remote - US")
        self.assertFalse(cli._ledger_lead(ctx, line, "claims analyst"))
        same = SimpleNamespace(url="https://www.indeed.com/rc/clk?jk=a1b2c3d4e5f6", text=line.text, context="Acme Rx · Remote - US")
        self.assertTrue(cli._ledger_lead(ctx, same, "claims analyst"))   # the control: the same job by its link (D188)

    def test_a_place_that_cannot_be_the_postings_is_another_opening(self):
        """RT-21: 'Calgary, AB' joined 'Remote - US', and 'Dallas, TX' joined 'Austin, TX'."""
        from careeros.discovery.board_discovery import places_compatible
        self.assertFalse(places_compatible("Calgary, AB", "unknown", "Remote - US", "remote"))
        self.assertFalse(places_compatible("Dallas, TX", "unknown", "Austin, TX", "onsite"))
        self.assertFalse(places_compatible("Buffalo, NY", "unknown", "New York, NY", "onsite"))
        self.assertFalse(places_compatible("Portland, ME", "unknown", "Portland, OR", "onsite"))   # one city name, two states
        self.assertTrue(places_compatible("Chicago, IL", "unknown", "Remote - US", "remote"))   # a US city, US remote

    def test_a_requisition_alone_never_joins_another_place_or_title(self):
        """RT-22: a Toronto line, or a 'Claims Supervisor' line, joined the Austin 'Claims Analyst' by its requisition."""
        posting = _tracked("gh:1", "Claims Analyst", "https://job-boards.greenhouse.io/acmerx/jobs/4400001", req="R-1101",
                           location="Austin, TX", remote="onsite")
        for candidate in (_candidate(req="R-1101", location="Toronto, ON"),
                          _candidate("Claims Supervisor", req="R-1101", location="Austin, TX"),
                          _candidate("Billing Analyst", req="R-1101", location="Austin, TX")):
            self.assertNotIn(_decision(candidate, [posting]), ("matched", "likely"))
        # the control: proposed to you, never linked by the requisition alone (the owner's D188)
        self.assertEqual(_decision(_candidate(req="R-1101", location="Austin, TX"), [posting]), "likely")

    def test_the_query_naming_an_opening_is_kept_in_the_alert_link(self):
        """RT-23: '?ashby_jid=' on a job path was dropped, so two openings became one lead."""
        from careeros.discovery.alerts import parse_eml_bytes
        from tests.test_intake_identity import html_mail
        base = "https://www.acmehealth.example/careers/jobs/open-roles?ashby_jid="
        links = parse_eml_bytes(html_mail((base + "a1b2", "Claims Analyst", "Acme Health"),
                                          (base + "c3d4", "Claims Analyst", "Acme Health"))).links
        self.assertEqual(len({lk.url for lk in links}), 2)

    def test_a_sister_company_is_never_the_leads_employer(self):
        """RT-24: an Aetna line joined Signify Health's posting through their common parent."""
        signify = _tracked("gh:1", "Claims Analyst", None, employer="Signify Health", parent="CVS Health")
        self.assertNotIn(_decision(_candidate(employer="Aetna"), [signify]), ("matched", "likely"))
        self.assertEqual(_decision(_candidate(employer="Signify Health"), [signify]), "likely")   # control (D188: proposed)

    def test_a_link_an_older_engine_made_is_checked_again(self):
        """RT-25: a lead an older engine linked to another opening at a shared page stayed linked after the upgrade."""
        from careeros.discovery.lead_resolution import recheck_linked_leads
        from tests.test_v26_discovery import add_lead
        store, ctx = posting_store()
        writer = store.writer("discovery")
        own = store.one("SELECT url FROM jobs WHERE job_id = ?", (JOB,))["url"]
        for lead_id, title, context, url in (
                ("lead:other", "Reimbursement Analyst", "Synthetic Health · Remote - US", "https://careers.synth.example/jobs"),
                ("lead:same", "Claims Analyst", "Synthetic Health · Remote - US", "https://careers.synth.example/jobs"),
                ("lead:yours", "Pharmacy Analyst", "Synthetic Health · Remote - US", "https://careers.synth.example/jobs"),
                ("lead:own", "Specialist", "", own)):   # its own posting's link, and no company to match by
            add_lead(store, lead_id, title, context, url=url)
            writer.update("leads", {"status": "ingested", "job_id": JOB}, "lead_id = ?", (lead_id,))
        from careeros.discovery.lead_resolution import _match_row
        writer.insert("lead_matches", _match_row("lead:yours", JOB, None, "owner", "confirmed", [JOB], store.clock.now(),
                                                 actor="user", reason="you confirmed it"))
        self.assertEqual(recheck_linked_leads(ctx)["leads_reopened"], 2)   # by the owner's D188 the same title keeps none
        status = {r["lead_id"]: r["status"] for r in store.all("SELECT lead_id, status FROM leads")}
        self.assertEqual(status, {"lead:other": "new", "lead:same": "new", "lead:yours": "ingested", "lead:own": "ingested"})
        writer.update("leads", {"status": "ingested", "job_id": JOB}, "lead_id = ?", ("lead:other",))
        # every run, never once: a link an older engine makes after a rollback is checked again (the red team's
        # fr1-faults-1 and fr1-alerts-1)
        self.assertEqual(recheck_linked_leads(ctx)["leads_reopened"], 1)
        self.assertEqual(store.one("SELECT status FROM leads WHERE lead_id = 'lead:other'")[0], "new")


class PasteTitleTests(unittest.TestCase):
    def test_a_pastes_title_is_its_own_line(self):
        """RT-26: 'Work at Home ...' was cut at ' at ', and a 'Position:' line or a site's heading taken as the title."""
        from careeros.discovery.manual_intake import parse_pasted
        body = "\n\n" + CLAIMS
        for text, title in (("Work at Home Claims Specialist\nCompany: Acme Rx", "Work at Home Claims Specialist"),
                            ("Claims Specialist - Work at Home - Medicare\nCompany: Acme Rx",
                             "Claims Specialist - Work at Home - Medicare"),
                            ("Claims Specialist\nPosition: Full-time\nCompany: Acme Rx", "Claims Specialist"),
                            ("Job details\nClaims Analyst\nCompany: Acme Rx", ""),
                            ("Claims Analyst at Acme Rx", "Claims Analyst"),
                            ("Claims Analyst at Acme Rx\nCompany: Acme Rx", "Claims Analyst at Acme Rx")):
            self.assertEqual(parse_pasted(text + body)["title"], title, text)
        self.assertEqual(parse_pasted("Work at Home Claims Specialist" + body)["company"], "")


class HandCheck16Tests(unittest.TestCase):
    """The round-16 hand check's survivors: older rules whose tests the corrections routed elsewhere (D184 sends only a
    board's posting link through the link rules; RTC-01 and RTC-04 refuse a match by employer and title earlier)."""

    def test_a_paste_at_a_board_link_under_another_title_is_another_posting(self):
        """A paste at a tracked job's own board link, under another title, was taken for it once the title check went.
        Since the owner's D188 the posting's exact link is the posting whatever title a paste gives it: that job."""
        from careeros.discovery.manual_intake import intake
        from tests.test_intake_identity import BODY, WORKDAY
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, WORKDAY)
        other = intake(ctx, "Pharmacy Benefits Specialist\nCompany: Acme Rx\n\n" + BODY + "\nPharmacy appeals.", WORKDAY)
        self.assertEqual((other["job_id"], other["status"]), (first["job_id"], "already_tracked"))
        another = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, WORKDAY.replace("R-44001", "R-44002"))
        self.assertEqual(another["status"], "added")   # another requisition link is another posting

    def test_the_duplicate_check_reads_every_job_at_a_board_link(self):
        """A third posting at one board link, the same as the second there, was compared with the first only. Since the
        owner's D188 every posting at one board posting link is that posting, "#" parts aside, and since the red team's
        batch 3 every read of it is written into the first job: one record."""
        from careeros.normalize.posting import RawPosting
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        link = "https://job-boards.greenhouse.io/acmerx/jobs/4400001"
        employer = {"employer_id": None, "name": "Acme Rx", "parent": "Acme Rx"}
        posting = lambda n, route: RawPosting(ats="manual", board_token="acme-rx", external_id=f"r{n}", title="Claims Analyst",
                                              location="Remote - US", url=f"{link}#/opening/{route}", apply_url=None,
                                              description_text=CLAIMS + f"\n\nOpening {route}.", raw={})
        ingest(ctx, employer, [posting(1, 1), posting(2, 2)], full_board=False)
        ingest(ctx, employer, [posting(3, 2)], full_board=False)
        self.assertEqual([r[0] for r in store.all("SELECT job_id FROM jobs")], ["manual:acme-rx:r1"])

    def test_a_paste_found_by_employer_and_title_never_offers_careeros_location(self):
        """A paste with --location found only by employer and title (no link, the same text and place) said to set that
        job's location from it."""
        from careeros.discovery.manual_intake import intake
        from tests.test_intake_identity import BODY
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, None, location="Remote - US")
        again = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, None, location="Remote - US")
        # the same paste again is its retry, by its receipt (the D190 design, section 6): it offers nothing
        self.assertEqual((again["job_id"], again["status"]), (first["job_id"], "recorded"))
        self.assertNotIn("careeros location", again.get("location_note") or "")

    def test_a_paste_at_another_typed_link_is_another_posting_whatever_its_text(self):
        """RTC-02's typed-link conflict: a paste with the same text at another link that is not a full address was taken
        for the tracked job once the conflict was dropped (RTC-04's text rule passed it)."""
        from careeros.discovery.manual_intake import intake
        from tests.test_intake_identity import BODY
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        first = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, "careers.acme.example/apply/5501")
        other = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, "careers.acme.example/apply/9999")
        self.assertNotEqual(other["job_id"], first["job_id"])


class AddendumTests(unittest.TestCase):
    """RT-36 to RT-41 (BATCH_1_ADDENDUM.md)."""

    def test_a_duplicate_an_older_engine_made_returns_to_screening_once(self):
        """RT-35: a posting the deployed engine made a duplicate (another level under one requisition) stayed one after
        the upgrade: its own alerts joined the other posting and its Apply label could never prepare."""
        from careeros import pipeline
        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import setup
        store, ctx = setup()
        employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(1, "Claims Analyst II", CLAIMS, "acmerx", req="R-2207"))],
               full_board=False)
        def older(job_id):   # the deployed engine's rule: one requisition at the employer made a duplicate
            with store.tx():
                store.writer("discovery").update("jobs", {"duplicate_of": "greenhouse:acmerx:1"}, "job_id = ?", (job_id,))
                store.writer("discovery").transition(ctx.matrix, job_id, "duplicate", reason_code="same_requisition")
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(2, "Claims Analyst III", CLAIMS + "\n\nSenior work.",
                                                                   "acmerx", req="R-2207"))], full_board=False)
        older("greenhouse:acmerx:2")
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(3, "Claims Analyst II", CLAIMS, "acmerx", req="R-2207"))],
               full_board=False)   # the same requisition, title, place and text under another board id: its own job (D188)
        state = lambda n: store.one("SELECT current_state, duplicate_of FROM jobs WHERE job_id = ?", (f"greenhouse:acmerx:{n}",))
        self.assertEqual(tuple(state(2)), ("duplicate", "greenhouse:acmerx:1"))
        for _ in range(2):   # the original leaves the board: a duplicate of it is judged against it all the same
            ingest(ctx, employer, [])
        self.assertEqual(state(1)["current_state"], "closed")
        counts = pipeline.process(ctx)
        self.assertEqual(counts["duplicates_rederived"], 1)
        self.assertNotEqual(state(2)["current_state"], "duplicate")
        self.assertIsNone(state(2)["duplicate_of"])
        self.assertNotEqual(state(3)["current_state"], "duplicate")
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = 'greenhouse:acmerx:2' AND "
                                  "other_id = 'greenhouse:acmerx:1' AND relation = 'possible_duplicate'"))
        ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(4, "Claims Analyst III", CLAIMS + "\n\nLead work.",
                                                                   "acmerx", req="R-2207"))], full_board=False)
        older("greenhouse:acmerx:4")   # made later, by an older engine after a rollback
        # every run, never once (the red team's fr1-faults-1): a duplicate an older engine makes after the upgrade (a
        # rollback) is re-derived too
        self.assertEqual(pipeline.rederive_identity(ctx)["duplicates_rederived"], 1)
        self.assertNotEqual(state(4)["current_state"], "duplicate")

    def test_an_earlier_builds_repaste_confirmation_never_places_the_job(self):
        """RT-36: a D162 re-paste confirmation, which could match another opening, was trusted as your --location."""
        from careeros.discovery.manual_intake import location_declared
        from careeros.openings import record_observation
        from tests.test_v26_discovery import setup
        store, _ = setup()
        job = {"job_id": "manual:acme-health:e1", "ats": "manual", "location_raw": "Remote - US",
               "description_text": "Location: Calgary, AB\nThis position is based in our Calgary office."}
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job["job_id"],
                           location="Remote - US", facts={"location_source": "argument", "location_write": "argument"})
        self.assertFalse(location_declared(store, job))
        record_observation(store, source_kind="human_text", evidence_level="human", job_id=job["job_id"],
                           location="Remote - US", facts={"location_source": "argument", "location_write": "argument",
                                                          "via": "careeros location"})
        self.assertTrue(location_declared(store, job))   # the control: careeros location

    def test_an_application_to_one_board_posting_never_holds_another_with_its_title(self):
        """RT-37: an application to Greenhouse 4400001 refused the Apply label of 4400002, which only shares its title."""
        from careeros.review.applications import application_matrix, preflight, record_application
        from tests.test_lead_resolution import legacy_board
        from tests.test_v26_discovery import setup
        for location in ("Austin, TX", "Remote - US"):
            store, ctx = setup()
            employer = dict(legacy_board(store, name="Acme Rx", token="acmerx"))
            ingest(ctx, employer, [greenhouse_posting("acmerx", gh_item(4400001, "Claims Analyst", CLAIMS, "acmerx")),
                                   greenhouse_posting("acmerx", gh_item(4400002, "Claims Analyst",
                                                                        CLAIMS + "\n\nPharmacy claims.", "acmerx",
                                                                        location=location))], full_board=False)
            record_application(store, application_matrix(), "greenhouse:acmerx:4400001", "direct", policy=POLICY, key="a1")
            reasons = preflight(store, "greenhouse:acmerx:4400002").reasons
            self.assertFalse([r for r in reasons if r["level"] in ("hold", "block")], (location, reasons))

    def test_a_word_about_other_arrangements_never_makes_a_remote_posting_hybrid(self):
        """RT-38: 'fully remote ... we offer remote, hybrid and in-office roles' read as hybrid and was rejected."""
        from careeros.normalize.posting import detect_remote
        text = "This is a fully remote position. We offer remote, hybrid and in-office roles across the company."
        self.assertEqual(detect_remote("United States", None, text), "unknown")
        self.assertEqual(detect_remote("United States", None, "This is a hybrid role, three days a week."), "hybrid")

    def test_a_boards_monthly_weekly_or_daily_pay_keeps_its_period(self):
        """RT-39: Lever's and Ashby's monthly, weekly or daily pay was stored as yearly, below the pay floor."""
        from careeros.discovery.boards import ashby_posting, lever_posting
        for interval, period in (("per-month-salary", "month"), ("per-week-salary", "week"), ("per-day-wage", "day"),
                                 ("per-hour-wage", "hour"), ("per-year-salary", "year")):
            lever = lever_posting("acme", {"id": "l1", "text": "Claims Analyst", "descriptionPlain": CLAIMS,
                                           "salaryRange": {"min": 6700, "max": 7900, "currency": "USD", "interval": interval}})
            self.assertEqual(lever.comp["period"], period, interval)
        for interval, period in (("1 MONTH", "month"), ("1 WEEK", "week")):
            ashby = ashby_posting("acme", {"id": "a1", "title": "Claims Analyst", "descriptionPlain": CLAIMS,
                                           "compensation": {"summaryComponents": [{"compensationType": "Salary",
                                                                                   "minValue": 6700, "maxValue": 7900,
                                                                                   "currencyCode": "USD", "interval": interval}]}})
            self.assertEqual(ashby.comp["period"], period, interval)
        odd = lever_posting("acme", {"id": "l2", "text": "Claims Analyst", "descriptionPlain": CLAIMS,
                                     "salaryRange": {"min": 6700, "max": 7900, "interval": "one-time"}})
        self.assertIsNone(odd.comp)   # a period it can't read: the text is read instead

    def test_an_interrupted_questions_write_leaves_the_earlier_file_and_one_unreadable_file_names_its_job(self):
        """RT-40: an interrupted `careeros package questions` left questions.json empty and every package command
        refused with a bare JSON error."""
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        packages.save_questions(root, JOB, QUESTIONS, "test", "2026-10-12T12:00:00.000Z")
        real = Path.write_text

        def stop(self, *a, **k):
            real(self, "", encoding="utf-8")
            raise KeyboardInterrupt
        with mock.patch.object(Path, "write_text", stop), self.assertRaises(KeyboardInterrupt):
            packages.save_questions(root, JOB, QUESTIONS[:1], "test", "2026-10-12T13:00:00.000Z")
        self.assertEqual(len(packages.load_questions(root, JOB)), len(QUESTIONS))   # the earlier file, whole
        packages.questions_path(root, JOB).write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, f"questions_unreadable: {JOB}"):
            packages.load_questions(root, JOB)

    def test_a_lock_whose_process_id_another_program_now_uses_is_stale(self):
        """RT-41: a run interrupted holding the writer lock, its id reused by another process, blocked every writer and
        `careeros unlock` refused."""
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from careeros.core import locks
        path = Path(tempfile.mkdtemp()) / "writer.lock"
        mine = locks.process_start(os.getpid())
        path.write_text(json.dumps({"pid": os.getpid(), "nonce": "x", "started": (mine or 0) + 1}), encoding="utf-8")
        self.assertEqual(locks.hold(path)[1], "stale")              # this id, another start: not that writer
        with self.assertRaisesRegex(locks.LockError, "stale_lock_held"):
            locks.SingleWriterLock(path).acquire()
        self.assertTrue(locks.force_unlock(path))
        # a lock written before start times were kept, its id now another program's
        other = subprocess.Popen(["ping", "-n", "30", "127.0.0.1"] if sys.platform == "win32" else ["sleep", "30"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            path.write_text(json.dumps({"pid": other.pid, "nonce": "y"}), encoding="utf-8")
            self.assertEqual(locks.hold(path)[1], "stale")
        finally:
            other.kill()
            other.wait()
        # the control: a lock this process takes is its own, and live
        path.unlink()
        held = locks.SingleWriterLock(path).acquire()
        self.assertIsNone(locks.hold(path))
        held.release()


if __name__ == "__main__":
    unittest.main()
