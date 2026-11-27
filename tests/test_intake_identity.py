"""The red team before Codex CR-2026-11-15-GATE-FIXES Pass 11 (2026-11-17; the owner's choices are D179): each test
reproduces what a red-team reviewer found, through the real handlers on a synthetic vault, and checks the correction.
Synthetic data only; nothing here reads the network."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli, pipeline
from careeros.discovery.alerts import AlertEmail, Link
from careeros.discovery.boards import greenhouse_posting
from careeros.discovery.manual_intake import intake, label_location, parse_pasted, same_manual_posting
from careeros.discovery.saved import parse_saved_html, parse_saved_text
from careeros.normalize.posting import RawPosting, detect_country, posting_link
from careeros.paths import vault_paths
from careeros.pipeline import ingest, process
from careeros.review import packages
from tests.helpers import give_label
from tests.test_lead_resolution import lead_row, legacy_board
from tests.test_packages import FULL_TIME, policy
from tests.test_v26_discovery import CLAIMS, add_lead, gh_item, setup

BODY = FULL_TIME.replace("This role is remote in the US. ", "")
ON = policy(enabled=True)
# a board's posting link, which names one posting by itself (the owner's D184: an employer's careers page never does)
WORKDAY = "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-44001"


def twin(store, job_id, url, employer="Synthetic Health", title="Claims Analyst"):
    """A second row for one posting as an older engine wrote one (batch 3 writes none): newer than the job of record,
    so the processing run joins it as that job's duplicate (pipeline.rederive_identity)."""
    from datetime import timedelta

    from careeros.core.clock import parse_utc
    later = (parse_utc(store.clock.now()) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    store.writer("discovery").insert("jobs", {
        "job_id": job_id, "workflow_id": f"job:{job_id}", "source": "manual_url", "ats": "manual",
        "external_id": job_id.rsplit(":", 1)[-1], "first_seen_at": later, "last_seen_at": later, "current_state": "discovered",
        "state_sequence": 0, "entered_state_at": later, "title": title, "title_norm": title.lower(), "url": url,
        "employer_name": employer, "parent": employer, "content_fingerprint": "older-engine"})


def add_url(ctx, tmp, url, body, employer="Acme Rx", title="Claims Analyst", location="Remote - US"):
    text = Path(tmp) / "posting.txt"
    text.write_text(body, encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_add_url(ctx, vault_paths(Path(tmp)), SimpleNamespace(url=url, text_file=str(text), employer=employer, parent=None,
                                                                     title=title, location=location))


def saved_page(employer, title, *, url=None, identifier=None, country=None, locality=None, region=None, body=BODY, canonical=None):
    data = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
            "hiringOrganization": {"@type": "Organization", "name": employer}, "jobLocationType": "TELECOMMUTE",
            "description": "<p>" + body.replace("\n", "</p><p>") + "</p>"}
    if url:
        data["url"] = url
    if identifier:
        data["identifier"] = identifier
    if country or locality:
        data["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": locality,
                                                             "addressRegion": region, "addressCountry": country}}
    head = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return f'<html><head>{head}<script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'


class LabelTests(unittest.TestCase):
    def test_a_prose_line_that_starts_with_a_label_word_is_no_label(self):
        """"Location-based pay: ... US hires" was read as the posting's own Location line (its value "based pay: ..."),
        which placed a Calgary-only paste in the US through standing, prepare and assist; "Company-wide bonus ..."
        became the employer."""
        for line in ("Location-based pay: the range below is for candidates in the United States.",
                     "Location-specific benefits: remote US employees receive a stipend.", "Job location-flexible for US hires"):
            self.assertIsNone(label_location(line), line)
        store, ctx = setup()
        text = ("Claims Analyst\nCompany: Maple Health\n\nThis is a remote role for residents of Ontario, Canada.\n"
                "Location-based pay: the range below is for candidates in the United States.\n\n" + BODY)
        job_id = intake(ctx, text, "https://careers.maple.example/jobs/77")["job_id"]
        process(ctx)
        self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs")), ("", "unknown"))
        self.assertIsNone(packages.route_for(store, ON, job_id)[0])
        self.assertEqual(parse_pasted("Claims Analyst\nCompany-wide bonus plan for every hire.\nCompany: Acme Rx\n" + BODY)["company"],
                         "Acme Rx")
        # the controls: a colon, or a dash with a space on each side
        self.assertEqual(label_location("Location: Remote - US"), "Remote - US")
        self.assertEqual(label_location("Location - Remote, US"), "Remote, US")
        self.assertEqual(label_location("Work Location – Austin, TX"), "Austin, TX")


class CountryTests(unittest.TestCase):
    def test_a_location_field_naming_another_country_or_excluding_the_us_places_nothing(self):
        """A board posting whose location field was "Remote - Canada (US hours)" was placed in the US (any US word won)
        and got a standing package, readiness and an assist session. Since D180 (after Codex Pass 11, H20) a field places a
        posting in the US only when every part of it is plainly a US place or a neutral word."""
        cases = {"Remote - Canada (US hours)": "unknown", "Remote (outside US)": "unknown", "Remote - US or Canada": "unknown",
                 "Remote, Canada - not open to US residents": "unknown", "Remote - US, UK": "unknown",
                 "Remote - Canada, US": "unknown", "Remote": "unknown", "": "unknown",
                 # any word that is not plainly a US place or a neutral word leaves it unknown (D180): your label routes it
                 "Dublin, OH": "unknown", "Paris, TX": "unknown", "Boston, MA (New England)": "unknown",
                 "Remote - US (not hybrid)": "unknown", "Remote - Georgia": "unknown",
                 # Codex Pass 11, H20: another country with US hours, a list, a qualifier
                 "Remote - Ecuador (US hours)": "unknown", "Remote - between US and UAE": "unknown",
                 "Remote - Tbilisi, Georgia (US hours)": "unknown", "Remote - US applicants ineligible": "unknown",
                 # a state code that is also a country's ("IN" India, "CA" Canada) places nothing without "US" after it,
                 # and "PT" alone is Portugal's code too
                 "Pune, IN": "unknown", "Remote - IN": "unknown", "Remote - US, IN": "unknown", "Remote - CA": "unknown",
                 "Hybrid - Chicago, IL": "unknown", "Remote - US (PT)": "unknown", "Remote - US OR Canada": "unknown",
                 "Mumbai, IN | Austin, TX": "unknown",
                 # a city is a city's name alone, after a comma, right before its state: never a list of places, a
                 # place abroad, a qualifier or a code
                 "Ontario / New York": "unknown", "Remote - Tbilisi / TX": "unknown", "Ontario, New York": "unknown",
                 "Ecuador, TX": "unknown", "Remote - Outside, TX": "unknown", "PT, TX": "unknown",
                 "Mexico City, TX": "unknown", "Toronto North, NY": "unknown",   # a place abroad inside a city's name
                 # Codex Pass 12 H22 (the owner's D182): a field that asks, trails off or leaves a bracket open places nothing
                 "Remote - US?": "unknown", "Remote - USA?": "unknown", "Remote - US /": "unknown", "Remote, US;": "unknown",
                 "US ()": "unknown", "Remote (US": "unknown", "Remote - US!": "unknown", "\"Remote - US\"": "unknown",
                 "Remote - U.S.": "US", "Remote - US -": "unknown", "- Remote - US": "unknown",
                 # a large city of a country whose code a state shares, as a city before that code (the generated tests)
                 "Mumbai, IN, US": "unknown", "Calgary, CA, US": "unknown",
                 # the red team after Pass 12: "US" beside a time zone, an empty alternative inside a bracket, two codes
                 # joined by no list separator, a field that trails off, a code joined by "-" or "."
                 "Remote (Time Zone: US)": "unknown", "Remote - EST (US)": "unknown", "Remote (US/EST)": "unknown",
                 "Remote (Pacific Time - US)": "unknown", "Remote - Timezone: USA": "unknown",
                 "Remote (US or) Remote": "unknown", "Remote (/) US": "unknown",
                 "Remote - IN: OK": "unknown", "Remote (IN - OK)": "unknown",
                 "Remote - US...": "unknown", "'Remote - US'": "unknown", "Remote - US based in": "unknown",
                 "Remote - US within the": "unknown", "Remote - US based": "US", "Remote - US-IN": "unknown",
                 "Remote - IN.US": "unknown",
                 # the round-13 hand check: a time zone's database name, and filler words alone beside a place
                 "Remote (US/Arizona)": "unknown", "US/Michigan": "unknown", "Only, TX": "unknown", "Based / US": "unknown",
                 # Codex Pass 13 H29 (the owner's D183): "US" beside a time zone's part, in either order
                 "Remote (US: Time Zone)": "unknown", "Remote - US: Eastern": "unknown", "Remote (US - Pacific time)": "unknown",
                 # the red team after Pass 11 (D181): another script or look-alike letters, "US" as a time zone, a shared
                 # code before ", US" with no city, codes inside a part, filler alone, a squashed place abroad
                 "Remote - US / \u041c\u0415\u0425\u0406\u0421\u041e": "unknown", "Remote - US / \uff2d\uff45\uff58\uff49\uff43\uff4f": "unknown",
                 "Remote - United States, \u6771\u4eac": "unknown", "Remote - US (\U0001F1F2\U0001F1FD)": "unknown",
                 "Texas \u041a\u0438\u0457\u0432": "unknown", "Remote (US Time Zone)": "unknown", "Remote (US/Eastern)": "unknown",
                 "Remote (US Central)": "unknown", "Remote - Any US time zone": "unknown", "CA, US (Remote)": "unknown",
                 "IN, US - Remote": "unknown", "US OR CA (Remote)": "unknown", "Remote - US (CA OK)": "unknown",
                 "Remote OK": "unknown", "Remote (US, In)": "unknown", "Remote - US or Any": "unknown",
                 "Hongkong, NY": "unknown", "Can-ada, FL": "unknown", "Remote - " + "US, " * 60 + "TX": "unknown",
                 # the controls: plain US fields, and plain other countries
                 "Remote - US": "US", "Austin, TX": "US", "Remote - New Mexico": "US", "US Remote": "US", "Remote, USA": "US",
                 "Remote - United States": "US", "Remote - Austin, TX, United States": "US", "Chicago, IL, US": "US",
                 "Indianapolis, IN, United States": "US", "Portland, OR": "US", "Remote - California": "US",
                 "Remote - AZ, CO, TX": "US", "Remote (AZ, CO, TX only)": "US", "Remote: anywhere in the US": "US",
                 "St. Louis, MO, US": "US", "Salt Lake City, UT": "US", "Remote - TX only": "US",
                 "Remote\u00a0-\u00a0US": "US", "Remote - US, OR": "US",
                 "Remote - US - Eastern time zone": "unknown", "Washington, DC": "US",
                 "Remote - Canada": "non_US", "Toronto, ON, Canada": "non_US"}
        for location, want in cases.items():
            self.assertEqual(detect_country(location, "", "remote"), want, location)
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        employer = dict(store.one("SELECT * FROM employers"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME + "\nCandidates must reside in "
                                                                   "Canada.", "synth", location="Remote - Canada (US hours)"))])
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "unknown")
        self.assertEqual(packages.standing_candidates(store, ON), [])
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, ON, None, "greenhouse:synth:1", facts=[], library={}, vault_root=Path("."))

    def test_a_score_made_under_the_earlier_country_rule_is_made_again_by_the_next_processing_run(self):
        """The country rule changed (gate-2026-11-17b, gate-2026-11-18a for D180, gate-2026-11-18b for D182, then
        gate-2026-11-18c for D183, gate-2026-11-19a for D186, gate-2026-11-19b for D188, gate-2026-11-24 for D190): a
        score made under an earlier rule is made again by the next processing run, so the
        screen reads every open job under the current one once after a deploy. Standing preparation, which once acted
        on such a score, is off (D183)."""
        from careeros.qualify import gate as gate_mod
        for earlier in ("gate-2026-11-16a", "gate-2026-11-17a", "gate-2026-11-17b", "gate-2026-11-18a", "gate-2026-11-18b",
                        "gate-2026-11-18c", "gate-2026-11-19a", "gate-2026-11-19b"):
            store, ctx = setup()
            legacy_board(store, name="Synthetic Health", token="synth")
            employer = dict(store.one("SELECT * FROM employers"))
            with mock.patch.object(gate_mod, "RULES_VERSION", earlier):
                ingest(ctx, employer, [greenhouse_posting("synth", gh_item(1, "Claims Analyst", FULL_TIME, "synth"))])
                process(ctx)
            before = store.one("SELECT COUNT(*) FROM scores")[0]
            self.assertEqual(packages.standing_candidates(store, ON), [], earlier)
            process(ctx)                                                      # the current rules: screened again
            self.assertGreater(store.one("SELECT COUNT(*) FROM scores")[0], before, earlier)

    def test_a_saved_page_country_code_is_a_country_never_a_us_state(self):
        """JSON-LD addressCountry "CA" made "Toronto, ON, CA", read as California; "IN", "IL", "DE", "CO" the same."""
        for code, region, locality, want in (("CA", "ON", "Toronto", "Canada"), ("IN", "KA", "Bengaluru", "India"),
                                             ("XY", "QQ", "Somewhere", "country XY (not the United States)"),
                                             ("US", "TX", "Austin", "United States")):
            raw, _ = parse_saved_html(saved_page("Maple Health", "Claims Analyst", country=code, region=region, locality=locality,
                                                 url="https://careers.maple.example/jobs/" + code), "page.html")
            self.assertEqual(raw.location, f"Remote - {locality}, {region}, {want}")
            self.assertEqual(detect_country(raw.location, "", "remote") == "US", code == "US", code)


class LinkTests(unittest.TestCase):
    def test_a_page_anchor_never_names_another_opening(self):
        """"#app" or "#:~:text=..." on the link you added made the same posting a second active job beside the board's,
        both with standing packages; only app routes ("#/opening/R-1101") name an opening (D179)."""
        self.assertEqual(posting_link("https://x.example/jobs/1#app"), "https://x.example/jobs/1")
        self.assertEqual(posting_link("https://x.example/jobs/1#:~:text=Claims"), "https://x.example/jobs/1")
        self.assertEqual(posting_link("https://x.example/p/#/opening/R-1"), "https://x.example/p#/opening/R-1")
        self.assertEqual(posting_link("https://x.example/p/#!/job/7"), "https://x.example/p#!/job/7")
        for anchor in ("#app", "#:~:text=Claims%20Analyst", "#job-description"):
            store, ctx = setup()
            legacy_board(store, name="Acme Rx", token="acmerx")
            item = gh_item(4001, "Claims Analyst", FULL_TIME, "acmerx")
            ingest(ctx, dict(store.one("SELECT * FROM employers")), [greenhouse_posting("acmerx", item)])
            with tempfile.TemporaryDirectory() as tmp:
                add_url(ctx, tmp, item["absolute_url"] + anchor, FULL_TIME)
            process(ctx)
            self.assertNotEqual(store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:acmerx:4001'")[0],
                                "duplicate", anchor)   # the board's job stays the one, the copy a sighting of it
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1, anchor)   # one posting, one record (batch 3)

    def test_two_openings_at_one_shared_link_stay_apart(self):
        """Two openings at one careers page link ("https://acme.example/careers"): B (another title, another
        requisition) overwrote A on add-url and saved files, and B's --location was confirmed onto A on a paste."""
        shared = "https://acme.example/careers"
        calgary = "Job Number: R-1101\n" + BODY + "\nThe work will be performed only in Calgary."
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, shared, calgary, title="Claims Analyst", location="Calgary, Alberta, Canada")
            add_url(ctx, tmp, shared, "Job Number: R-1102\n" + BODY, title="Claims Operations Specialist")
            rows = {r["title"]: dict(r) for r in store.all("SELECT * FROM jobs")}
        self.assertEqual(set(rows), {"Claims Analyst", "Claims Operations Specialist"})
        self.assertNotEqual(rows["Claims Operations Specialist"]["current_state"], "duplicate")   # linked for review only
        a = rows["Claims Analyst"]
        self.assertEqual((a["location_raw"], a["requisition_id"]), ("Calgary, Alberta, Canada", "R-1101"))
        self.assertIn("only in Calgary", a["description_text"])
        # a paste of B at the shared link with --location never confirms onto A
        b = intake(ctx, "Claims Operations Specialist\nCompany: Acme Rx\n\n" + BODY, shared, location="Remote - US")
        self.assertNotEqual(b["job_id"], a["job_id"])
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (a["job_id"],))[0], "Calgary, Alberta, Canada")
        # since the owner's D188 a careers page names no posting: the same link and title there is its own job, linked
        # for your review; only the same submission again (every field the same) is that job
        again = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + calgary, shared)["job_id"]
        self.assertNotEqual(again, a["job_id"])
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate' AND job_id = ?", (again,)))
        # with no requisition on either side, another title alone keeps them apart: never one posting, never made a
        # duplicate (the title rule on its own, D179)
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, shared, BODY + "\nThe work will be performed only in Calgary.", title="Claims Analyst",
                    location="Calgary, AB")
            add_url(ctx, tmp, shared, BODY, title="Claims Operations Specialist")
        rows = {r["title"]: tuple(r) for r in store.all("SELECT title, location_raw, current_state FROM jobs")}
        self.assertEqual(rows["Claims Analyst"][1], "Calgary, AB")
        self.assertNotEqual(rows["Claims Operations Specialist"][2], "duplicate")
        # the same link and title with another requisition stated in the text is another posting, on add-url and in a
        # saved file alike (both now read the requisition)
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, shared, "Job Number: R-2101\n" + BODY, location="Calgary, AB")
            add_url(ctx, tmp, shared, "Job Number: R-2102\n" + BODY)
        self.assertEqual(sorted(r[0] for r in store.all("SELECT requisition_id FROM jobs")), ["R-2101", "R-2102"])
        store, ctx = setup()
        for req, where in (("R-3101", "Calgary, AB"), ("R-3102", "Remote - US")):
            raw, employer = parse_saved_text(f"URL: {shared}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: {where}\n\n"
                                             f"Job Number: {req}\n" + BODY, "p.txt")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual(sorted(r[0] for r in store.all("SELECT location_raw FROM jobs")), ["Calgary, AB", "Remote - US"])

    def test_a_saved_link_that_is_not_a_full_address_is_kept_never_dropped(self):
        """A saved file's link without "https://" (or a page's relative JSON-LD link) was dropped, so two openings fell
        back to the no-link rule and the second overwrote the first."""
        store, ctx = setup()
        for where, link in (("Calgary, Alberta, Canada", "jobs.example.com/apply/calgary-claims"),
                            ("Remote - US", "jobs.example.com/apply/us-claims")):
            raw, employer = parse_saved_text(f"URL: {link}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: {where}\n\n" + BODY,
                                             "posting.txt")
            self.assertEqual(raw.url, link)
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual(sorted(r[0] for r in store.all("SELECT location_raw FROM jobs")), ["Calgary, Alberta, Canada", "Remote - US"])
        raw, _ = parse_saved_html(saved_page("Acme Rx", "Claims Analyst", url="/careers/job/1101",
                                             canonical="https://acme.example/careers/job/1101"), "page.html")
        self.assertEqual(raw.url, "https://acme.example/careers/job/1101")    # read against the page's own address

    def test_the_duplicate_check_reads_every_job_at_the_link(self):
        """With R-1101 and R-1102 tracked at one portal, a saved page of R-1102 was compared with R-1101 only, linked
        to it, and left a second active job for R-1102. Since the owner's D184 a portal's "#" route names no posting:
        there a copy is linked for your review, never made a duplicate. At a board's posting link every job there is
        read, another employer's first: since the owner's D188 a board's posting link is one posting whatever employer is
        typed, so every copy there is the first job's duplicate."""
        portal = "https://jobs.example.com/portal/#/opening/"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, portal + "R-1101", BODY + "\nThe work will be performed only in Calgary.", location="Calgary, AB")
            add_url(ctx, tmp, portal + "R-1102", BODY)
        raw, employer = parse_saved_html(saved_page("Acme Rx", "Claims Analyst", url=portal + "R-1102", identifier="R-1102"), "p.html")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE current_state = 'duplicate'")[0], 0)
        # at a board's posting link: another employer's job there first, then this one's
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, WORKDAY, BODY + "\nAnother team.", employer="Maple Health")
            add_url(ctx, tmp, WORKDAY, BODY)
        first = store.one("SELECT job_id FROM jobs")["job_id"]   # every copy is written into the first (batch 3)
        raw, employer = parse_saved_html(saved_page("Acme Rx", "Claims Analyst", url=WORKDAY), "p.html")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual([r[0] for r in store.all("SELECT job_id FROM jobs")], [first])

    def test_linkless_postings_at_another_location_are_two(self):
        """With no link, the same text, employer and title at another location overwrote the first (D179: the location
        must agree too)."""
        store, ctx = setup()
        for where in ("Calgary, AB, Canada", "Remote - US"):
            raw, employer = parse_saved_text(f"Title: Claims Analyst\nEmployer: Acme Rx\nLocation: {where}\n\n" + BODY, "p.txt")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual(sorted(r[0] for r in store.all("SELECT location_raw FROM jobs")), ["Calgary, AB, Canada", "Remote - US"])

    def test_a_retry_with_a_link_that_is_not_a_full_address_reuses_its_job(self):
        """Retrying add-url with a link typed without "https://" added a new row each time (up to 64). Such a link names
        no posting (the owner's D184; the red team before Codex Pass 15, RT-06), so an edited text there is kept as its
        own job, linked for review, and the first keeps its text."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            for _ in range(3):
                add_url(ctx, tmp, "careers.acme.example/apply/5501", BODY)
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)
            add_url(ctx, tmp, "careers.acme.example/apply/5501", BODY + "\nEdited.")
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 2)
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE description_text LIKE '%Edited.%'")[0], 1)
        self.assertEqual(store.one("SELECT COUNT(*) FROM opening_links WHERE relation = 'possible_duplicate'")[0], 1)


class LeadAndImportTests(unittest.TestCase):
    def test_a_duplicate_found_by_its_text_lends_its_lead_to_its_original(self):
        """After Pass 10, a LinkedIn link you added (a duplicate of a board job by its text) kept its lead on the
        unscored duplicate instead of the job it duplicates. Since the owner's D188 its text makes it no duplicate: it is
        its own job, linked for your review, and its lead (the same LinkedIn job) is linked to it; a real duplicate (the
        same LinkedIn job by another of its links) lends its lead to its original."""
        store, ctx = setup()
        legacy_board(store)
        gh = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        ingest(ctx, dict(store.one("SELECT * FROM employers")), [RawPosting(
            ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst", location="Remote - US",
            url=gh, apply_url=gh, description_text=CLAIMS, raw={})], full_board=False)
        li = "https://www.linkedin.com/jobs/view/4000000777"
        add_lead(store, "lead:li", "Claims Analyst", "Foo Health · Remote", url=li)
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, li, CLAIMS, employer="Foo Health")
            own = store.one("SELECT job_id, current_state FROM jobs WHERE url = ?", (li,))
            self.assertNotEqual(own["current_state"], "duplicate")
            self.assertEqual(lead_row(store, "lead:li")["job_id"], own["job_id"])
            add_lead(store, "lead:li2", "Claims Analyst", "Foo Health · Remote",
                     url="https://www.linkedin.com/jobs/view/4000000778")
            add_url(ctx, tmp, "https://www.linkedin.com/jobs/view/4000000778", CLAIMS, employer="Foo Health")
            add_url(ctx, tmp, "https://www.linkedin.com/jobs/search/?currentJobId=4000000778", CLAIMS + "\nSeen again.",
                    employer="Foo Health")
        # the same LinkedIn job by another of its links is that job, written into it (one posting, one record: batch 3)
        rows = store.all("SELECT job_id, url FROM jobs WHERE url LIKE '%4000000778%'")
        self.assertEqual([r["url"] for r in rows], ["https://www.linkedin.com/jobs/view/4000000778"])
        self.assertEqual(lead_row(store, "lead:li2")["job_id"], rows[0]["job_id"])

    def test_an_alert_board_link_not_stored_keeps_its_lead_open_and_its_email(self):
        """A board link from an alert email whose write failed left its lead "ingested" with no job, and the email was
        filed as imported: the posting was lost for good. add-url with a board link said nothing either."""
        url = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        posting = RawPosting(ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst",
                             location="Remote - US", url=url, apply_url=url, description_text=CLAIMS, raw={"id": 4400001})
        alert = AlertEmail(sender="alerts@example.com", subject="New jobs for you", received_at=None,
                           links=[Link(url=url, site="greenhouse", text="Claims Analyst", context="Foo Health · Remote", job_like=True)])
        from careeros.discovery import alerts
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            paths["inbox"].mkdir(parents=True, exist_ok=True)
            (paths["inbox"] / "alert.eml").write_bytes(b"From: alerts@example.com\r\nSubject: New jobs\r\n\r\nsynthetic\r\n")
            with mock.patch.object(alerts, "parse_eml_bytes", return_value=alert), \
                    mock.patch.object(cli, "fetch_single", return_value=posting), \
                    mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")), \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((paths["inbox"] / "failed").rglob("alert.eml")))
            lead = store.one("SELECT status, job_id, fetch_failure FROM leads")
            self.assertEqual((lead["status"], lead["job_id"]), ("new", None))
            self.assertIn("read but not stored", lead["fetch_failure"])
            with mock.patch.object(cli, "fetch_single", return_value=posting), \
                    mock.patch.object(cli, "HttpClient", return_value=mock.MagicMock()), \
                    mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cli.cmd_add_url(ctx, paths, SimpleNamespace(url=url, text_file=None, employer="Foo Health", parent=None,
                                                            title=None, location=None))

    def test_an_alert_link_matches_a_tracked_job_only_as_that_opening(self):
        """An alert link to portal opening R-1102 (read without its "#" part) was recorded as already tracked on R-1101.
        Since the owner's D184 a portal's link, its "#" route too, names no posting: no alert line joins a job there by
        the link; the lead stays open for you."""
        portal = "https://jobs.example.com/careers#/opening/"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, portal + "R-1101", BODY + "\nThe work will be performed only in Calgary.", location="Calgary, AB")
            add_url(ctx, tmp, portal + "R-1102", BODY)
        self.assertIsNone(cli._known_job(ctx, "https://jobs.example.com/careers"))
        one = store.one("SELECT job_id, url FROM jobs WHERE url LIKE '%R-1101'")
        self.assertIsNone(cli._known_job(ctx, one["url"]))                          # not even its own link (D184)
        with tempfile.TemporaryDirectory() as tmp:                                    # one opening only: still not it
            store, ctx = setup()
            add_url(ctx, tmp, portal + "R-1101", BODY, location="Calgary, AB")
            add_url(ctx, tmp, WORKDAY, BODY)
        self.assertIsNone(cli._known_job(ctx, "https://jobs.example.com/careers"))
        job = store.one("SELECT job_id FROM jobs WHERE url = ?", (WORKDAY,))["job_id"]
        self.assertEqual(cli._known_job(ctx, WORKDAY), job)                          # the control: a board's posting link

    def test_a_malformed_saved_link_never_stops_the_import(self):
        """One saved posting whose URL line was malformed ("http://[...") raised ValueError and stopped every import."""
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            inbox = paths["inbox"]
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "a-bad.txt").write_text("URL: http://[careers.acme.example/jobs/1\nTitle: Claims Analyst\nEmployer: Acme Rx\n\n"
                                             + CLAIMS, encoding="utf-8")
            (inbox / "b-good.txt").write_text("URL: https://careers.acme.example/jobs/2\nTitle: Claims Analyst\nEmployer: Acme Rx\n\n"
                                              + CLAIMS, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((inbox / "failed").rglob("a-bad.txt")))
            self.assertTrue(list((inbox / "imported").rglob("b-good.txt")))
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)


CALGARY = "\nThe work will be performed only in Calgary."
MANUAL = "not pursued: label it Apply or Stretch (standing preparation is off)"   # the owner's D183


def html_mail(*links):
    """A synthetic job-alert email: one paragraph per (link, title, employer)."""
    body = "".join(f'<p><a href="{url}">{title}</a> {employer} \u00b7 Remote</p>' for url, title, employer in links)
    return ("From: jobs@example.com\r\nTo: someone@example.com\r\nSubject: New jobs for you\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n<html><body>" + body + "</body></html>").encode()


class StandingRouteTests(unittest.TestCase):
    def test_no_job_is_prepared_without_your_label(self):
        """The owner's D183, after Codex CR-2026-11-15-GATE-FIXES Passes 12 and 13 (each found new routes into standing
        preparation): standing preparation is off. A pasted, added or saved posting, a board's posting read for an alert
        or add-url, and a board's own scan all wait for your Apply or Stretch label, whatever the policy file says."""
        store, ctx = setup()
        pasted = intake(ctx, "Claims Analyst\nCompany: Acme Rx\nLocation: Remote - US\n\n" + BODY,
                        "https://careers.acme.example/jobs/1")["job_id"]
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, "https://careers.acme.example/jobs/2", BODY, title="Claims Specialist")
        raw, employer = parse_saved_text("URL: https://careers.acme.example/jobs/3\nTitle: Claims Coordinator\nEmployer: Acme Rx\n"
                                         "Location: Remote - US\n\n" + BODY, "p.txt")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        legacy_board(store, name="Synthetic Health", token="synth")
        board = dict(store.one("SELECT * FROM employers WHERE name = 'Synthetic Health'"))
        ingest(ctx, board, [greenhouse_posting("synth", gh_item(7, "Claims Analyst", FULL_TIME, "synth"))], source="manual_url",
               full_board=False)                                          # a board's posting read for an alert or add-url
        ingest(ctx, board, [greenhouse_posting("synth", gh_item(8, "Pharmacy Claims Analyst", FULL_TIME, "synth"))])   # its own scan
        process(ctx)
        rows = [dict(r) for r in store.all("SELECT job_id, country, current_state FROM jobs")]
        self.assertEqual(len(rows), 5)
        self.assertEqual({(r["country"], r["current_state"]) for r in rows}, {("US", "scored")})
        self.assertEqual(packages.standing_candidates(store, ON), [])
        for row in rows:
            self.assertEqual(packages.route_for(store, ON, row["job_id"]), (None, MANUAL))
            with self.assertRaises(packages.PackageRefused):
                packages.prepare(store, ON, None, row["job_id"], facts=[], library={}, vault_root=Path("."))
        give_label(store, ctx.policy, ctx.profile, pasted, "apply", None, "k-apply")                 # your label routes it
        self.assertEqual(packages.route_for(store, ON, pasted, ctx.profile)[0], "apply")   # P1 reads your profile

    def test_you_set_a_location_by_naming_the_job(self):
        """The owner's D180: `careeros location JOB "..."` replaces confirmation by re-paste (Codex Passes 7-11 found a
        different opening confirmed onto a job through every matching rule); a paste never changes a tracked job's
        location."""
        url = WORKDAY   # a link that names one posting: a board's (D184)
        store, ctx = setup()
        job_id = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, url)["job_id"]
        process(ctx)
        self.assertEqual(store.one("SELECT country FROM jobs")["country"], "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.cmd_location(ctx, paths, SimpleNamespace(job_id=job_id, location="Remote - US"))
            self.assertIn("screened again", out.getvalue())
            self.assertEqual(tuple(store.one("SELECT location_raw, country, current_state FROM jobs")), ("Remote - US", "US", "scored"))
            for args, why in ((SimpleNamespace(job_id="manual:acme-rx:none", location="Remote - US"), "no tracked job"),
                              (SimpleNamespace(job_id=job_id, location="   "), "give the location"),
                              (SimpleNamespace(job_id=job_id, location="R" * 301), "at most 300")):
                with self.assertRaises(SystemExit) as refused, contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_location(ctx, paths, args)
                self.assertIn(why, str(refused.exception))
        self.assertEqual(store.one("SELECT location_raw FROM jobs")["location_raw"], "Remote - US")
        # a paste of the same posting with --location records a sighting only, and says how to set it
        again = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, url, location="Remote - Canada")
        self.assertEqual((again["job_id"], again["location_confirmed"], again["location_changed"]), (job_id, False, False))
        self.assertIn(f"careeros location {job_id}", again["location_note"])
        self.assertEqual(store.one("SELECT location_raw FROM jobs")["location_raw"], "Remote - US")
        # a job board's posting keeps its board's location
        legacy_board(store, name="Synthetic Health", token="synth")
        ingest(ctx, dict(store.one("SELECT * FROM employers WHERE name = 'Synthetic Health'")),
               [greenhouse_posting("synth", gh_item(8, "Claims Analyst", FULL_TIME, "synth"))])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as refused, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_location(ctx, vault_paths(Path(tmp)), SimpleNamespace(job_id="greenhouse:synth:8", location="Remote - Canada"))
        self.assertIn("job board", str(refused.exception))
        self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = 'greenhouse:synth:8'")[0], "Remote - US")


class IdentityConflictTests(unittest.TestCase):
    def test_another_employer_at_a_shared_link_is_another_posting(self):
        """Codex CR-2026-11-15-GATE-FIXES H15, Pass 11: a Maple Health paste at Acme Rx's shared careers link, with the
        same title and requisition, confirmed its US location onto Acme Rx's Calgary job; a saved page for "A B Health"
        overwrote "A+B Health"'s job at the same link (the same generated id)."""
        shared = "https://careers.example.com/join"
        store, ctx = setup()
        a = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY + CALGARY, shared,
                   location="Calgary, Alberta, Canada")
        b = intake(ctx, "Claims Analyst\nCompany: Maple Health\n\nJob Number: R-9101\n" + BODY, shared, location="Remote - US")
        self.assertEqual(b["status"], "added")
        self.assertNotEqual(b["job_id"], a["job_id"])
        process(ctx)
        self.assertEqual(tuple(store.one("SELECT employer_name, location_raw, country FROM jobs WHERE job_id = ?", (a["job_id"],))),
                         ("Acme Rx", "Calgary, Alberta, Canada", "non_US"))
        # the control: Acme Rx's own submission again (every field the same) is that job; at a careers page anything
        # less is its own job since the owner's D188
        self.assertEqual(intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY + CALGARY, shared,
                                location="Calgary, Alberta, Canada")["job_id"], a["job_id"])
        store, ctx = setup()
        for employer, (code, region, city) in (("A+B Health", ("CA", "AB", "Calgary")), ("A B Health", ("US", "TX", "Austin")),
                                               ("A+B Health", ("CA", "AB", "Calgary"))):
            raw, name = parse_saved_html(saved_page(employer, "Claims Analyst", url=shared, identifier="R-4001", country=code,
                                                    region=region, locality=city), "page.html")
            ingest(ctx, {"employer_id": None, "name": name, "parent": name}, [raw], source="manual_url", full_board=False)
        rows = {r["employer_name"]: r["location_raw"] for r in store.all("SELECT employer_name, location_raw FROM jobs")}
        self.assertEqual(rows, {"A+B Health": "Remote - Calgary, AB, Canada", "A B Health": "Remote - Austin, TX, United States"})

    def test_a_lead_at_a_shared_link_joins_only_the_opening_it_names(self):
        """Codex CR-2026-11-15-GATE-FIXES H16, Pass 11: with R-8101 and R-8102 tracked at one careers link, a lead for
        R-8102 (its title and requisition) was linked to R-8101 when R-8101 was added again, and an alert for R-8102 was
        recorded as R-8101 when only R-8101 was tracked."""
        shared = "https://careers.example.com/join"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, shared, "Job Number: R-8101\n" + BODY, title="Claims Analyst")
            add_url(ctx, tmp, shared, "Job Number: R-8102\n" + BODY, title="Claims Operations Analyst")
            jobs = {r["requisition_id"]: r["job_id"] for r in store.all("SELECT job_id, requisition_id FROM jobs")}
            add_lead(store, "lead:ops", "Claims Operations Analyst", "Acme Rx \u00b7 Remote \u00b7 req=R-8102", url=shared)
            add_url(ctx, tmp, shared, "Job Number: R-8101\n" + BODY + "\nEdited.", title="Claims Analyst")
            self.assertNotEqual(lead_row(store, "lead:ops")["job_id"], jobs["R-8101"])
            add_url(ctx, tmp, shared, "Job Number: R-8102\n" + BODY + "\nEdited.", title="Claims Operations Analyst")
            # since the owner's D182 (Codex Pass 12 H24, H27), a lead at a page several openings share is linked to none of
        # them by the link: it stays open for you, even for its own opening
        self.assertIsNone(lead_row(store, "lead:ops")["job_id"])
        # an alert names R-8102 at the link while only R-8101 is tracked: never recorded as R-8101
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, shared, "Job Number: R-8101\n" + BODY, title="Claims Analyst")
        one = store.one("SELECT job_id FROM jobs")["job_id"]
        self.assertIsNone(cli._known_job(ctx, shared, "Claims Operations Analyst", "Acme Rx \u00b7 req=R-8102"))
        self.assertIsNone(cli._known_job(ctx, shared, "Claims Analyst", "Acme Rx \u00b7 req=R-8102"))
        self.assertIsNone(cli._known_job(ctx, shared, "Claims Analyst", "Acme Rx \u00b7 Remote"))   # D182: not by the link
        self.assertTrue(one)
        alert = AlertEmail(sender="jobs@example.com", subject="New jobs", received_at=None,
                           links=[Link(url=shared, site="other", text="Claims Operations Analyst",
                                       context="Acme Rx \u00b7 req=R-8102", job_like=True)])
        cli._record_alert(ctx, alert, mock.MagicMock())
        self.assertIsNone(store.one("SELECT 1 FROM leads"))
        self.assertEqual(tuple(store.one("SELECT state, job_id FROM alert_notices")), ("new", None))   # a notice, never a lead (D192 part 3)

    def test_a_requisition_never_hides_another_opening_at_a_shared_link(self):
        """Codex CR-2026-11-15-GATE-FIXES H17, Pass 11: a paste of portal opening B with opening A's requisition (or the
        same requisition under another title at a shared link) was taken for A and never tracked; two board postings at
        one portal under other routes with one requisition made the second a duplicate."""
        portal = "https://careers.example.com/portal#/opening/"
        store, ctx = setup()
        a = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY + CALGARY, portal + "A",
                   location="Calgary, Alberta, Canada")
        b = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY, portal + "B", location="Remote - US")
        self.assertEqual(b["status"], "added")
        self.assertNotEqual(b["job_id"], a["job_id"])
        shared = "https://careers.example.com/join"
        c = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-7101\n" + BODY, shared)
        d = intake(ctx, "Claims Operations Analyst\nCompany: Acme Rx\n\nJob Number: R-7101\n" + BODY, shared)
        self.assertNotEqual(d["job_id"], c["job_id"])
        process(ctx)
        self.assertNotIn("duplicate", [r[0] for r in store.all("SELECT current_state FROM jobs")])
        # the control: the same submission again (every field the same) is that job (D188: a portal's route names none)
        self.assertEqual(intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY, portal + "B",
                                location="Remote - US")["job_id"], b["job_id"])
        # a job board's postings at one portal with other routes and one requisition stay two, linked for your review
        store, ctx = setup()
        legacy_board(store, name="Acme Rx", token="acmerx")
        employer = dict(store.one("SELECT * FROM employers"))

        def board(external, route, title="Claims Analyst", url=None):
            link = url or portal + route
            return RawPosting(ats="greenhouse", board_token="acmerx", external_id=external, title=title, location="Remote - US",
                              url=link, apply_url=link, description_text=FULL_TIME, requisition_id="R-6101", raw={})
        ingest(ctx, employer, [board("1", "A"), board("2", "B"), board("3", "A", title="Claims Operations Analyst")])
        self.assertEqual([r[0] for r in store.all("SELECT current_state FROM jobs ORDER BY job_id")], ["normalized"] * 3)
        self.assertEqual(store.one("SELECT COUNT(*) FROM opening_links WHERE relation = 'possible_duplicate'")[0], 2)
        # another board id is another posting, whatever its link and title (D188): never a duplicate, linked for review
        ingest(ctx, employer, [board("4", "A")], full_board=False)
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:acmerx:4'")[0], "normalized")
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = 'greenhouse:acmerx:4'"))


class AlertRecoveryTests(unittest.TestCase):
    def test_two_openings_of_one_portal_in_an_email_are_two_leads(self):
        """Codex CR-2026-11-15-GATE-FIXES H18, Pass 11: an alert email with portal openings R-401 and R-402 was read as
        one link without its "#" route, so the second opening was lost and the first lost its own address."""
        from careeros.discovery.alerts import parse_eml_bytes
        portal = "https://careers.example.com/portal#/opening/"
        data = html_mail((portal + "R-401", "Claims Analyst", "Acme Rx"), (portal + "R-402", "Claims Operations Analyst", "Acme Rx"),
                         ("https://careers.example.com/about#team", "About us", ""))
        links = parse_eml_bytes(data).links
        self.assertEqual([(lk.url, lk.job_like) for lk in links],
                         [(portal + "R-401", True), (portal + "R-402", True), ("https://careers.example.com/about", False)])
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            paths["inbox"].mkdir(parents=True, exist_ok=True)
            (paths["inbox"] / "alert.eml").write_bytes(data)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((paths["inbox"] / "imported").rglob("alert.eml")))
        self.assertEqual(sorted(r[0] for r in store.all("SELECT url FROM alert_notices")), [portal + "R-401", portal + "R-402"])

    def test_an_alert_line_not_stored_is_read_again_on_the_next_import(self):
        """Codex CR-2026-11-15-GATE-FIXES H19, Pass 11: after a write failure, importing the same email again found the
        lead's id, counted the line as seen before and filed the email as imported: the posting was never stored."""
        from careeros.discovery import alerts
        url = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        posting = RawPosting(ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst",
                             location="Remote - US", url=url, apply_url=url, description_text=CLAIMS, raw={"id": 4400001})
        alert = AlertEmail(sender="alerts@example.com", subject="New jobs for you", received_at=None,
                           links=[Link(url=url, site="greenhouse", text="Claims Analyst", context="Foo Health \u00b7 Remote",
                                       job_like=True)])
        store, ctx = setup()
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            inbox = paths["inbox"]
            inbox.mkdir(parents=True, exist_ok=True)
            data = b"From: alerts@example.com\r\nSubject: New jobs\r\n\r\nsynthetic\r\n"
            (inbox / "alert.eml").write_bytes(data)
            with mock.patch.object(alerts, "parse_eml_bytes", return_value=alert), \
                    mock.patch.object(cli, "fetch_single", return_value=posting), \
                    mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")), \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertTrue(list((inbox / "failed").rglob("alert.eml")))
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 0)
            # the same email again, the fault gone: the line is read again, the job stored and the lead closed on it
            (inbox / "alert.eml").write_bytes(data)
            with mock.patch.object(alerts, "parse_eml_bytes", return_value=alert), \
                    mock.patch.object(cli, "fetch_single", return_value=posting) as fetched, \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertEqual(fetched.call_count, 1)
            self.assertTrue(list((inbox / "imported").rglob("alert.eml")))
        self.assertEqual(store.one("SELECT job_id FROM jobs")[0], "greenhouse:foohealth:4400001")
        lead = store.one("SELECT status, job_id, fetch_failure FROM leads")
        self.assertEqual(tuple(lead), ("ingested", "greenhouse:foohealth:4400001", None))
        self.assertEqual(store.one("SELECT COUNT(*) FROM leads")[0], 1)
        # a third read is a line seen before: never fetched again
        counts = cli._record_alert(ctx, alert, mock.MagicMock())
        self.assertEqual(counts["lead_seen_before"], 1)


class RedTeamAfterPass11Tests(unittest.TestCase):
    """The Claude red team on bfb893d (the D180 build; not a Codex review), each finding paired with its correction (the
    owner's D181: all High and Medium)."""
    SHARED = "https://careers.example.com/join"

    def test_a_long_field_is_read_in_linear_time(self):
        """rt1_6: the field splitter was quadratic on runs of spaces (16,000 spaces: 5 s)."""
        import time
        start = time.perf_counter()
        self.assertEqual(detect_country("Remote" + " " * 20000 + "- US", "", "remote"), "US")
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_titles_agree_only_when_a_stated_level_or_work_arrangement_agrees(self):
        """rt3_3 (the owner's D181): "Claims Analyst I (Remote)" was taken for "Claims Analyst III - Onsite"."""
        from careeros.discovery.board_discovery import titles_agree
        for a, b_, want in (("Claims Analyst I (Remote)", "Claims Analyst III - Onsite", False),
                            ("Claims Analyst (Remote)", "Claims Analyst - Onsite", False),
                            ("Claims Analyst II", "Claims Analyst III", False),
                            ("Claims Analyst", "Claims Analyst II - Remote (US)", True),   # one side states none
                            ("Claims Analyst II - Remote", "claims analyst ii (remote)", True),
                            ("Claims Analyst", "Claims Operations Analyst", False)):
            self.assertEqual(titles_agree(a, b_), want, (a, b_))

    def test_an_alert_or_lead_at_a_shared_link_joins_only_an_opening_whose_employer_title_and_requisition_agree(self):
        """rt4_2/rt3_10 (another employer), rt3_12 (a requisition written "Job ID: ..."), rt3_3 (level and work
        arrangement): each joined the opening tracked at a shared careers link. A link that names one posting still
        joins it whatever the line calls it."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, self.SHARED, "Job Number: R-8101\n" + BODY, title="Claims Analyst III - Onsite")
        one = store.one("SELECT job_id FROM jobs")["job_id"]
        for title, context in (("Claims Analyst III - Onsite", "Maple Health \u00b7 Remote"),
                               ("Claims Analyst III - Onsite", "Acme Rx \u00b7 Job ID: R-8102"),
                               ("Claims Analyst III - Onsite", "Acme Rx \u00b7 Req #R-8102"),
                               ("Claims Analyst I (Remote)", "Acme Rx \u00b7 Remote")):
            self.assertIsNone(cli._known_job(ctx, self.SHARED, title, context), (title, context))
        # since D182 (Codex Pass 12 H24, H27) not even the agreeing line: a shared page joins nothing by its link
        self.assertIsNone(cli._known_job(ctx, self.SHARED, "Claims Analyst III", "Acme Rx \u00b7 Job ID: R-8101"))
        self.assertTrue(one)
        # a stored lead from another employer stays open when the posting is added again
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_lead(store, "lead:maple", "Claims Analyst", "Maple Health \u00b7 Remote", url=self.SHARED)
            add_lead(store, "lead:acme", "Claims Analyst", "Acme Rx \u00b7 Remote", url=self.SHARED)
            add_url(ctx, tmp, self.SHARED, BODY)
        job = store.one("SELECT job_id FROM jobs")["job_id"]
        self.assertEqual((lead_row(store, "lead:maple")["job_id"], lead_row(store, "lead:acme")["job_id"]), (None, None))
        self.assertTrue(job)   # both stay open for you (D182)
        # a link that names one posting (a board's) joins it whatever the line calls it
        store, ctx = setup()
        legacy_board(store, name="Foo Health", token="foohealth")
        gh = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        ingest(ctx, dict(store.one("SELECT * FROM employers")), [RawPosting(
            ats="greenhouse", board_token="foohealth", external_id="4400001", title="Pharmacy Claims Analyst",
            location="Remote - US", url=gh, apply_url=gh, description_text=CLAIMS, raw={})], full_board=False)
        self.assertEqual(cli._known_job(ctx, gh, "Clms Analyst - Pharmacy", "Foo Health Inc \u00b7 Remote"),
                         "greenhouse:foohealth:4400001")

    def test_another_employer_at_a_shared_link_is_never_made_a_duplicate(self):
        """rt4_1/rt3_9: Maple Health's posting at Acme Rx's shared link got its own id but was made a duplicate of Acme
        Rx's Calgary job, never screened, and its lead went to Acme Rx's job."""
        store, ctx = setup()
        add_lead(store, "lead:maple", "Claims Analyst", "Maple Health \u00b7 Remote", url=self.SHARED)
        a = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9101\n" + BODY + CALGARY, self.SHARED,
                   location="Calgary, Alberta, Canada")
        b = intake(ctx, "Claims Analyst\nCompany: Maple Health\n\nJob Number: R-9101\n" + BODY, self.SHARED, location="Remote - US")
        self.assertNotEqual(store.one("SELECT current_state FROM jobs WHERE job_id = ?", (b["job_id"],))[0], "duplicate")
        self.assertNotEqual(lead_row(store, "lead:maple")["job_id"], a["job_id"])
        # Acme Rx's posting again at the shared page: kept and linked for your review, never made a duplicate by the link
        # (the owner's D182, after Codex Pass 12 H24)
        raw, employer = parse_saved_text(f"URL: {self.SHARED}\nTitle: Claims Analyst\nEmployer: Acme Rx\n"
                                         f"Location: Calgary, Alberta, Canada\n\nJob Number: R-9101\n" + BODY + CALGARY, "p.txt")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        copy = store.one("SELECT job_id, current_state, duplicate_of FROM jobs WHERE employer_name = 'Acme Rx' AND job_id != ?",
                         (a["job_id"],))
        self.assertEqual((copy["current_state"], copy["duplicate_of"]), ("normalized", None))
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE job_id = ? AND relation = 'possible_duplicate'",
                                  (copy["job_id"],)))

    def test_a_second_opening_at_a_shared_link_in_another_place_is_its_own_job(self):
        """rt3_5 (the owner's D181): add-url and saved files wrote a second opening (same title, no other requisition)
        over the first at a shared careers link: its location replaced the first's and your Apply label carried over."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, "https://acme.example/careers", BODY)                          # A: Remote - US
            process(ctx)   # screened: a preview is given only then (the D190 design)
            first = store.one("SELECT job_id FROM jobs")["job_id"]
            give_label(store, ctx.policy, ctx.profile, first, "apply", None, "k-apply")
            add_url(ctx, tmp, "https://acme.example/careers", BODY + "\nThis role is open to Ontario residents only.",
                    location="Ontario, Canada")
            rows = {r["job_id"]: r["location_raw"] for r in store.all("SELECT job_id, location_raw FROM jobs")}
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[first], "Remote - US")
            other = next(j for j in rows if j != first)
            self.assertIsNone(packages.route_for(store, ON, other)[0])
            # the same posting again (the same text: a retry) is that job; an edited copy at a page several openings
            # share cannot be told from another opening, so it is its own job, linked for your review (Codex Pass 13
            # H32; the owner's D183)
            add_url(ctx, tmp, "https://acme.example/careers", BODY)
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 2)
            add_url(ctx, tmp, "https://acme.example/careers", BODY + "\nEdited.")
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 3)
        self.assertNotIn("Edited.", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (first,))[0])
        self.assertEqual(packages.route_for(store, ON, first, ctx.profile)[0], "apply")   # P1 reads your profile

    def test_the_board_matcher_links_a_lead_only_to_the_opening_its_link_and_title_name(self):
        """rt3_2: with a board tracking only portal#/opening/R-1, a lead for #/opening/R-2, and one at a shared link under
        another title and requisition, were matched to R-1 by the link without its "#" route."""
        from types import SimpleNamespace

        from careeros.discovery import board_discovery as bd
        from tests.test_v26_discovery import POLICY
        portal = "https://careers.example.com/portal#/opening/"
        posting = SimpleNamespace(url=portal + "R-1", requisition_id=None, external_id="1", title="Claims Analyst",
                                  location="Remote - US", key="greenhouse:acme:1", ats="greenhouse", slug="acme",
                                  remote_type="remote")
        shared = SimpleNamespace(**{**vars(posting), "url": self.SHARED, "requisition_id": "R-8101",
                                    "title": "Claims Analyst III - Onsite"})

        def decide(url, title, req=None, post=posting):
            lead = bd.Candidate(employer_name="Acme Rx", origin="alert", posting_url=url, title=title, location="Remote",
                                requisition_id=req)
            return bd.match_lead(lead, [post], POLICY, "2026-11-18T00:00:00.000Z")["decision"]
        self.assertNotEqual(decide(portal + "R-2", "Claims Operations Analyst"), "matched")
        self.assertNotEqual(decide(self.SHARED, "Claims Operations Analyst", "R-8102", shared), "matched")
        self.assertNotEqual(decide(self.SHARED, "Claims Analyst I (Remote)", None, shared), "matched")
        self.assertNotEqual(decide(self.SHARED, "Claims Analyst III", "R-8102", shared), "matched")   # another requisition
        # a portal's route names no posting (D184): not even its own link matches by the link
        self.assertNotEqual(decide(portal + "R-1", "Clms Analyst"), "matched")
        # at the shared page the agreeing title is only proposed to you, never matched by the link (D182, Pass 12 H24)
        self.assertEqual(decide(self.SHARED, "Claims Analyst III", None, shared), "likely")
        # at a board's posting link (D184) the board's own posting id is the posting (the owner's D188): a "#" part
        # or what the line calls it never names another, and another id is never it
        board = "https://boards.greenhouse.io/acme/jobs/1#/opening/"
        on_board = SimpleNamespace(**{**vars(posting), "url": board + "R-1"})
        self.assertEqual(decide(board + "R-2", "Claims Operations Analyst", None, on_board), "matched")
        self.assertEqual(decide(board + "R-1", "Clms Analyst", None, on_board), "matched")
        named = SimpleNamespace(**{**vars(on_board), "requisition_id": "R-1101"})
        self.assertEqual(decide(board + "R-1", "Claims Analyst", "R-1102", named), "matched")
        self.assertNotEqual(decide("https://boards.greenhouse.io/acme/jobs/2", "Claims Analyst", "R-1101", named), "matched")
        self.assertNotEqual(decide("https://boards.greenhouse.io/acme/jobs/2", "Claims Analyst", "R-1101", named), "likely")

    def test_an_alert_on_a_duplicate_is_recorded_on_its_original(self):
        """rt3_11: an alert for a link you added that became a duplicate of a board job was recorded on the duplicate.
        Since the owner's D188 the same text makes no duplicate (the LinkedIn job is its own, linked for review); the
        same LinkedIn job by another of its links does, and its alert is recorded on the original."""
        store, ctx = setup()
        legacy_board(store)
        gh = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        ingest(ctx, dict(store.one("SELECT * FROM employers")), [RawPosting(
            ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst", location="Remote - US",
            url=gh, apply_url=gh, description_text=CLAIMS, raw={})], full_board=False)
        li = "https://www.linkedin.com/jobs/view/4000000777"
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, li, CLAIMS, employer="Foo Health")
            own = store.one("SELECT job_id, current_state FROM jobs WHERE url = ?", (li,))
            self.assertNotEqual(own["current_state"], "duplicate")
            self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate' AND job_id = ?",
                                      (own["job_id"],)))
            other_form = "https://www.linkedin.com/jobs/search/?currentJobId=4000000777"
            add_url(ctx, tmp, other_form, CLAIMS + "\nSeen again.", employer="Foo Health")
        self.assertIsNone(store.one("SELECT 1 FROM jobs WHERE url LIKE '%currentJobId=4000000777'"))   # written into own
        self.assertIn("Seen again.", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (own["job_id"],))[0])
        self.assertEqual(cli._known_job(ctx, other_form, "Claims Analyst", "Foo Health \u00b7 Remote"), own["job_id"])

    def test_an_alert_job_link_keeps_the_query_that_names_its_opening(self):
        """rt3_1: three openings at ".../jobs/ViewJobDetails?job=101" (102, 103) were read as one link without its query."""
        from careeros.discovery.alerts import parse_eml_bytes
        base = "https://ats.example.com/v4/ats/web.php/jobs/ViewJobDetails?job="
        links = parse_eml_bytes(html_mail(*((f"{base}{n}&clientkey=ACME&utm_source=mail", f"Claims Analyst {n}", f"Employer {n}")
                                            for n in (101, 202, 303)))).links
        self.assertEqual([lk.url for lk in links], [f"https://ats.example.com/v4/ats/web.php/jobs/ViewJobDetails?clientkey=ACME&job={n}"
                                                    for n in (101, 202, 303)])
        # a path naming the posting by a slug keeps its query too, tracking aside: "id" may name the opening (the red
        # team before Codex Pass 15, RT-23: the alert reader keeps what the posting's own link keeps)
        one = parse_eml_bytes(html_mail(("https://careers.example.com/jobs/claims-analyst?id=5&utm_source=x", "Claims Analyst",
                                         "Acme Rx"))).links
        self.assertEqual(one[0].url, "https://careers.example.com/jobs/claims-analyst?id=5")

    def test_openings_on_one_careers_page_in_alerts_are_one_lead_per_title(self):
        """rt3_4: two cards at one careers page in an email were one line, and a later email's other opening there was
        counted as seen before."""
        from careeros.discovery.alerts import parse_eml_bytes
        page = "https://acme.example/careers"
        store, ctx = setup()
        cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Claims Analyst", "Acme Rx"),
                                                         (page, "Claims Operations Analyst", "Acme Rx"))), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Pharmacy Claims Analyst", "Acme Rx"))), mock.MagicMock())
        self.assertEqual(counts["notice_new"], 1)   # a notice, never a lead (D192 part 3)
        self.assertEqual(sorted(r[0] for r in store.all("SELECT title FROM alert_notices")),
                         ["Claims Analyst", "Claims Operations Analyst", "Pharmacy Claims Analyst"])
        again = cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Claims Analyst", "Acme Rx"))), mock.MagicMock())
        self.assertEqual(again["notice_new"], 1)   # another email's card is its own notice: no "seen" by its text
        same = cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Claims Analyst", "Acme Rx"))), mock.MagicMock())
        self.assertEqual(same["notice_seen_before"], 1)                                       # the same email again

    def test_another_posting_on_the_same_site_or_in_another_country_is_never_seen_before(self):
        """rt3_7/rt4_8: LinkedIn job 222 (US) was dropped as seen before because job 111 (Toronto, dismissed) had the same
        employer and title; two same-title openings of one portal were one lead."""
        from careeros.discovery.alerts import parse_eml_bytes
        store, ctx = setup()
        cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.linkedin.com/jobs/view/4000000111", "Claims Analyst",
                                                          "Acme Rx \u00b7 Toronto"))), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.linkedin.com/jobs/view/4000000222", "Claims Analyst",
                                                                   "Acme Rx \u00b7 United States (Remote)"))), mock.MagicMock())
        self.assertEqual(counts["lead_new"], 1)
        portal = "https://careers.example.com/portal#/opening/"
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((portal + "R-401", "Billing Analyst", "Maple Health"),
                                                                  (portal + "R-402", "Billing Analyst", "Maple Health"))),
                                   mock.MagicMock())
        self.assertEqual(store.one("SELECT COUNT(*) FROM alert_notices WHERE title = 'Billing Analyst'")[0], 2)   # a notice, never a lead (D192 part 3)
        # another site's job with the same employer and title is never seen before either (the owner's D188)...
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.indeed.com/viewjob?jk=abc123def", "Claims Analyst",
                                                                   "Acme Rx \u00b7 Remote"))), mock.MagicMock())
        self.assertEqual(counts["lead_new"], 1)
        # ...the control: the same LinkedIn job by another of its links is seen before
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.linkedin.com/jobs/search/?currentJobId=4000000222",
                                                                   "Claims Analyst", "Acme Rx \u00b7 Remote"))), mock.MagicMock())
        self.assertEqual(counts["lead_seen_before"], 1)
        # on another site, a line in another country than the earlier one is another job
        store, ctx = setup()
        cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.linkedin.com/jobs/view/4000000333", "Claims Analyst",
                                                          "Acme Rx \u00b7 Toronto"))), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.indeed.com/viewjob?jk=def456abc", "Claims Analyst",
                                                                   "Acme Rx \u00b7 United States (Remote)"))), mock.MagicMock())
        self.assertEqual(counts["lead_new"], 1)

    def test_one_requisition_under_another_title_and_text_is_another_opening(self):
        """rt3_8: board postings 11 "Claims Analyst" and 12 "Pharmacy Claims Auditor" sharing R-100 made 12 a duplicate."""
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        employer = dict(store.one("SELECT * FROM employers"))
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(11, "Claims Analyst", FULL_TIME, "synth", req="R-100")),
                               greenhouse_posting("synth", gh_item(12, "Pharmacy Claims Auditor", FULL_TIME + "\nAudit pharmacy claims.",
                                                                   "synth", req="R-100"))])
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:synth:12'")[0], "normalized")
        self.assertEqual(store.one("SELECT COUNT(*) FROM opening_links WHERE relation = 'possible_duplicate'")[0], 1)
        # the same text under a retitled copy is another opening too: another title (titles_agree) is linked for review,
        # never a duplicate, whatever the text (the red team before Pass 15, RT-16)
        ingest(ctx, employer, [greenhouse_posting("synth", gh_item(13, "Claims Analyst (copy)", FULL_TIME, "synth", req="R-100"))],
               full_board=False)
        self.assertEqual(store.one("SELECT current_state FROM jobs WHERE job_id = 'greenhouse:synth:13'")[0], "normalized")
        self.assertEqual(store.one("SELECT COUNT(*) FROM opening_links WHERE relation = 'possible_duplicate'")[0], 2)

    def test_lever_and_ashby_postings_show_every_location_their_board_lists(self):
        """rt2_2 (the owner's D181): a Lever posting with allLocations ["Remote - US", "Remote - Canada"] and an Ashby
        posting with a London secondary location read as plainly US and reached standing preparation."""
        from careeros.discovery.boards import ashby_posting, lever_posting
        lever = lever_posting("acme", {"id": "l1", "text": "Claims Analyst", "categories": {
            "location": "Remote - US", "allLocations": ["Remote - US", "Remote - Canada"]}, "descriptionPlain": FULL_TIME})
        ashby = ashby_posting("acme", {"id": "a1", "title": "Claims Analyst", "location": "United States", "isRemote": True,
                                       "secondaryLocations": [{"location": "London, United Kingdom"}], "descriptionPlain": FULL_TIME})
        self.assertEqual(lever.location, "Remote - US; Remote - Canada")
        self.assertEqual(ashby.location, "United States (Remote); London, United Kingdom")
        for posting in (lever, ashby):
            self.assertEqual(detect_country(posting.location, "", "remote"), "unknown", posting.location)
        plain = lever_posting("acme", {"id": "l2", "text": "Claims Analyst", "categories": {      # the control
            "location": "Remote - US", "allLocations": ["Remote - US"]}, "descriptionPlain": FULL_TIME})
        self.assertEqual((plain.location, detect_country(plain.location, "", "remote")), ("Remote - US", "US"))

    def test_a_location_you_set_stays_until_you_set_another(self):
        """rt4_6: after careeros location, an edited copy of the posting added again or imported put the file's location
        back, and your Apply label routed it again. Under the owner's D181 a copy whose location differs (or is missing)
        was its own job; since D188 a copy at the posting's exact link is that posting, its new text written and its
        location (yours) kept until you give another."""
        from careeros.discovery.manual_intake import set_location
        url = WORKDAY   # a link that names one posting: a board's (D184)
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, url, BODY, location=None)
            process(ctx)   # screened: a preview is given only then (the D190 design)
            job = store.one("SELECT job_id FROM jobs")["job_id"]
            give_label(store, ctx.policy, ctx.profile, job, "apply", None, "k-apply")
            set_location(ctx, job, "Calgary, Alberta, Canada")
            add_url(ctx, tmp, url, BODY + "\nEdited.", location=None)
            raw, employer = parse_saved_text(f"URL: {url}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: Remote - US\n\n"
                                             + BODY + "\nEdited again.", "p.txt")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
            process(ctx)
            self.assertEqual(tuple(store.one("SELECT location_raw, country FROM jobs WHERE job_id = ?", (job,))),
                             ("Calgary, Alberta, Canada", "non_US"))
            self.assertIn("Edited again.", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (job,))[0])
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE current_state != 'duplicate'")[0], 1)
            # the control: a location you give again replaces yours
            add_url(ctx, tmp, url, BODY + "\nEdited.", location="Remote - US")
            self.assertEqual(store.one("SELECT location_raw FROM jobs WHERE job_id = ?", (job,))[0], "Remote - US")

    def test_a_paste_offers_careeros_location_only_for_the_same_posting_by_its_link(self):
        """rt4_7/rt4_5: a paste matched by employer and title only (or to a board posting) told you to set that job's
        location from this paste; careeros location on a duplicate said it was screened again; add-url with a board link
        dropped --location silently."""
        from careeros.discovery.manual_intake import set_location
        store, ctx = setup()
        calgary = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY + CALGARY, None, location="Calgary, Alberta, Canada")
        other = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, "https://careers.acme.example/jobs/us-claims-40021",
                       location="Remote - US")
        if other["job_id"] == calgary["job_id"]:
            self.assertNotIn("careeros location", other["location_note"])
            self.assertIn("may be another opening", other["location_note"])
        own_store, own_ctx = setup()                                          # the control: the same posting by its link
        own = intake(own_ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, WORKDAY)   # a board's link (D184)
        same = intake(own_ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, WORKDAY, location="Remote - Canada")
        self.assertEqual(same["job_id"], own["job_id"])
        self.assertIn(f"careeros location {own['job_id']}", same["location_note"])
        legacy_board(store, name="Synthetic Health", token="synth")
        ingest(ctx, dict(store.one("SELECT * FROM employers WHERE name = 'Synthetic Health'")),
               [greenhouse_posting("synth", gh_item(8, "Claims Analyst", FULL_TIME, "synth"))])
        board = intake(ctx, "Claims Analyst\nCompany: Synthetic Health\n\n" + BODY,
                       store.one("SELECT url FROM jobs WHERE job_id = 'greenhouse:synth:8'")[0], location="Remote - Canada")
        self.assertIn("keeps its board's location", board["location_note"])
        # careeros location refuses a duplicate (one LinkedIn job by two of its links: D188) and a closed job
        with tempfile.TemporaryDirectory() as tmp:
            add_url(ctx, tmp, "https://www.linkedin.com/jobs/view/4000000888", FULL_TIME, employer="Synthetic Health")
        # an older engine's second row for it (batch 3 writes none), joined as its duplicate at the processing run
        twin(store, "manual:synthetic-health:twin-888", "https://www.linkedin.com/jobs/search/?currentJobId=4000000888")
        process(ctx)
        dup = store.one("SELECT job_id FROM jobs WHERE current_state = 'duplicate'")["job_id"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            set_location(ctx, dup, "Remote - US")
        # add-url with a board link says --location is not used
        url = "https://boards.greenhouse.io/synth/jobs/9"
        posting = greenhouse_posting("synth", gh_item(9, "Claims Analyst", FULL_TIME, "synth"))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "fetch_single", return_value=posting), \
                mock.patch.object(cli, "HttpClient", return_value=mock.MagicMock()), contextlib.redirect_stdout(out):
            cli.cmd_add_url(ctx, vault_paths(Path(tmp)), SimpleNamespace(url=url, text_file=None, employer="Synthetic Health",
                                                                         parent=None, title=None, location="Calgary, AB"))
        self.assertIn("--location not used", out.getvalue())

    def test_two_links_that_are_not_full_addresses_are_one_posting_only_when_typed_the_same(self):
        """Placing a manual posting (Codex Pass 9 H9; the owner's D179): a link that is not a full address names no
        posting by itself, so two of them are the same posting only when typed the same (a retry), never two others."""
        row = {"url": "jobs.example.com/apply/calgary", "requisition_id": None, "description_text": "A",
               "employer_name": "Acme Rx", "title": "Claims Analyst", "location_raw": "Remote - US"}
        same = dict(requisition_id=None, text="B", employer="Acme Rx", title="Claims Analyst", location="Remote - US")
        self.assertFalse(same_manual_posting(row, url="jobs.example.com/apply/us", **same))
        # since the owner's D188 such a link is never one posting with another, typed alike or not: only the same
        # submission again (every field the same, the link exactly as typed) reuses the job (RT-06)
        for url in ("jobs.example.com/apply/calgary", "Jobs.Example.com/apply/calgary/"):
            self.assertFalse(same_manual_posting(row, url=url, **{**same, "text": "A"}))


class CodexPass12Tests(unittest.TestCase):
    """Codex CR-2026-11-15-GATE-FIXES Pass 12 (H21-H27), each reproduction paired with its correction (the owner's D182:
    fix the three standing routes in place; never auto-merge at shared pages)."""
    SHARED = "https://careers.example.com/join"

    def board(self):
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        return store, ctx, dict(store.one("SELECT * FROM employers WHERE name = 'Synthetic Health'"))

    def test_a_single_link_fetch_is_recorded_as_one_never_as_the_scans(self):
        """H21: a board scan listed the job as "Remote - Ecuador (US hours)"; a single-posting fetch of the same ID
        (add-url, a saved alert) wrote "Remote - US" while the row kept source=ats_api, and standing prepared it.
        Standing preparation is off since D183; the record of where a board posting's location was written stays, and
        nothing is prepared without your label."""
        job = "greenhouse:synth:7"
        store, ctx, board = self.board()
        listed = gh_item(7, "Claims Analyst", FULL_TIME, "synth", location="Remote - Ecuador (US hours)")
        ingest(ctx, board, [greenhouse_posting("synth", listed)])                         # the board's own listing
        self.assertTrue(pipeline.scan_location_current(store, job))
        single = gh_item(7, "Claims Analyst", FULL_TIME, "synth", location="Remote - US")
        ingest(ctx, board, [greenhouse_posting("synth", single)], source="manual_url", full_board=False)   # one link
        process(ctx)
        self.assertEqual(tuple(store.one("SELECT source, country, location_raw FROM jobs")), ("ats_api", "US", "Remote - US"))
        self.assertFalse(pipeline.scan_location_current(store, job))
        self.assertEqual(packages.standing_candidates(store, ON), [])
        with self.assertRaises(packages.PackageRefused):
            packages.prepare(store, ON, None, job, facts=[], library={}, vault_root=Path("."))
        ingest(ctx, board, [greenhouse_posting("synth", single)])       # the board's next scan lists it there: confirmed
        self.assertTrue(pipeline.scan_location_current(store, job))
        # a location written before the record existed (no record) waits for the board's next scan to list it there
        store, ctx, board = self.board()
        with mock.patch.object(pipeline, "_observe_posting"), mock.patch.object(pipeline, "_confirm_scan_location"):
            ingest(ctx, board, [greenhouse_posting("synth", single)])
        self.assertFalse(pipeline.scan_location_current(store, job))
        ingest(ctx, board, [greenhouse_posting("synth", single)])
        self.assertTrue(pipeline.scan_location_current(store, job))

    def test_a_scan_that_cannot_store_the_posting_withdraws_its_scans_record(self):
        """The red team after Pass 12 (RT1-1): the board's next scan listed the job elsewhere but the posting could not
        be stored (a malformed link, a lone surrogate): the earlier scan's location stayed confirmed."""
        job = "greenhouse:synth:7"
        store, ctx, board = self.board()
        single = gh_item(7, "Claims Analyst", FULL_TIME, "synth", location="Remote - US")
        ingest(ctx, board, [greenhouse_posting("synth", single)])
        self.assertTrue(pipeline.scan_location_current(store, job))
        moved = gh_item(7, "Claims Analyst", FULL_TIME, "synth", location="Toronto, Ontario, Canada")
        with mock.patch.object(pipeline, "normalized_fields", side_effect=ValueError("unreadable")):
            counts = ingest(ctx, board, [greenhouse_posting("synth", moved)])
        self.assertEqual(counts["errors"], 1)
        self.assertFalse(pipeline.scan_location_current(store, job))
        ingest(ctx, board, [greenhouse_posting("synth", single)])          # stored again by a scan: confirmed
        self.assertTrue(pipeline.scan_location_current(store, job))

    def test_a_providers_typed_country_is_written_out(self):
        """H23: Workable's typed locations [{region FL, country US}, {country CA}] became "FL, US; CA (Remote)", which
        the plain-US rule read as two states. A typed country is now written out: "CA" is Canada, "IL" Israel."""
        from careeros.discovery.boards import ashby_posting, lever_posting, workable_posting

        def workable(*places):
            return workable_posting("synth", {"shortcode": "W1", "title": "Claims Analyst", "telecommuting": True,
                                              "locations": list(places), "description": "<p>x</p>"})
        for code, name in (("CA", "Canada"), ("IL", "Israel"), ("ZZ", "country ZZ")):
            posting = workable({"region": "FL", "country": "US"}, {"country": code})
            self.assertEqual(posting.location, f"FL, US; {name} (Remote)")
            self.assertEqual(detect_country(posting.location, "", "remote"), "unknown")
        self.assertEqual(detect_country(workable({"city": "Tampa", "region": "FL", "country": "US"}).location, "", "remote"), "US")
        lever = lever_posting("synth", {"id": "L1", "text": "Claims Analyst", "country": "CA",
                                        "categories": {"location": "Remote - US"}, "descriptionPlain": "x"})
        self.assertEqual(lever.location, "Remote - US; Canada")
        self.assertEqual(detect_country(lever.location, "", "remote"), "unknown")
        us = lever_posting("synth", {"id": "L2", "text": "Claims Analyst", "country": "US",
                                     "categories": {"location": "Remote - US"}, "descriptionPlain": "x"})
        self.assertEqual(us.location, "Remote - US")
        ashby = ashby_posting("synth", {"id": "A1", "title": "Claims Analyst", "location": "Remote - US",
                                        "secondaryLocations": [{"location": "Remote",
                                                                "address": {"postalAddress": {"addressCountry": "CA"}}}],
                                        "descriptionPlain": "x"})
        self.assertEqual(detect_country(ashby.location, "", "remote"), "unknown")
        self.assertIn("Canada", ashby.location)
        # the red team after Pass 12 (RT2-6): a typed country under "countryCode", or the job's own when abroad
        self.assertEqual(workable({"city": "Perth", "region": "WA", "countryCode": "AU"}).location, "Perth, WA, Australia (Remote)")
        job = workable_posting("synth", {"shortcode": "W2", "title": "Claims Analyst", "telecommuting": True, "country": "AU",
                                         "locations": [{"region": "WA"}], "description": "<p>x</p>"})
        self.assertEqual(job.location, "WA, Australia (Remote)")   # the job's country with the place that has none
        self.assertEqual(detect_country(job.location, "", "remote"), "unknown")
        us = workable_posting("synth", {"shortcode": "W3", "title": "Claims Analyst", "telecommuting": True, "country": "US",
                                        "locations": [{"region": "WA"}], "description": "<p>x</p>"})
        self.assertEqual((us.location, detect_country(us.location, "", "remote")), ("WA (Remote)", "US"))

    def test_nothing_is_a_terminal_duplicate_at_a_shared_page(self):
        """H24: a Calgary and a US Claims Analyst at one careers page (same requisition) got two ids, but the US one was
        made a terminal duplicate of Calgary, its lead (naming Austin) went to Calgary and its later edit was ignored;
        a common parent masked two employers. At a page several openings share, each is kept and linked for review."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_lead(store, "lead:austin", "Claims Analyst", "Acme Rx \u00b7 Austin, TX", url=self.SHARED)
            add_url(ctx, tmp, self.SHARED, "Job Number: R-8102\n" + BODY + CALGARY, location="Calgary, Alberta, Canada")
            add_url(ctx, tmp, self.SHARED, "Job Number: R-8102\n" + BODY, location="Austin, TX")
            rows = {r["location_raw"]: dict(r) for r in store.all("SELECT job_id, location_raw, current_state FROM jobs")}
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows["Austin, TX"]["current_state"], "duplicate")
            self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate'"))
            self.assertNotEqual(lead_row(store, "lead:austin")["job_id"], rows["Calgary, Alberta, Canada"]["job_id"])
            # its edited copy at the careers page is its own job, linked for review (D188), and the first keeps its text
            add_url(ctx, tmp, self.SHARED, "Job Number: R-8102\n" + BODY + "\nEdited.", location="Austin, TX")
            self.assertNotIn("Edited.", store.one("SELECT description_text FROM jobs WHERE job_id = ?",
                                                  (rows["Austin, TX"]["job_id"],))[0])
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE description_text LIKE '%Edited.%'")[0], 1)
        # two employers under one parent at the same page, title and requisition
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            for employer in ("Acme Rx", "Maple Health"):
                text = Path(tmp) / "posting.txt"
                text.write_text("Job Number: R-8102\n" + BODY, encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_add_url(ctx, vault_paths(Path(tmp)), SimpleNamespace(
                        url=self.SHARED, text_file=str(text), employer=employer, parent="Health Group", title="Claims Analyst",
                        location="Remote - US"))
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs WHERE current_state = 'duplicate'")[0], 0)
            # at a link that names one posting it is that posting whatever employer is typed (the owner's D188)
            store, ctx = setup()
            for employer in ("Acme Rx", "Maple Health"):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_add_url(ctx, vault_paths(Path(tmp)), SimpleNamespace(
                        url=WORKDAY, text_file=str(text), employer=employer,
                        parent="Health Group", title="Claims Analyst", location="Remote - US"))
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 1)   # one posting, one record (batch 3)
        # the control: at a link that names one posting (a board's, D184), the same posting again is that job
        store, ctx = setup()
        url = WORKDAY
        a = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, url, location="Remote - US")
        raw, employer = parse_saved_text(f"URL: {url}\nTitle: Claims Analyst\nEmployer: Acme Rx Inc.\nLocation: Remote - US\n\n"
                                         + BODY + "\nSaved again.", "p.txt")   # another generated id ("acme-rx-inc")
        ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        self.assertEqual([r[0] for r in store.all("SELECT job_id FROM jobs")], [a["job_id"]])
        self.assertIn("Saved again.", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (a["job_id"],))[0])

    def test_a_failed_retry_keeps_the_alert_pending(self):
        """H25: after a write failure, the retry hit a temporary fetch error (HTTP 503): the email was filed as imported
        and the lead lost its retry mark, so the posting was never fetched again."""
        from careeros.discovery import alerts
        from careeros.discovery.http import FetchError
        url = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        posting = RawPosting(ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst",
                             location="Remote - US", url=url, apply_url=url, description_text=CLAIMS, raw={"id": 4400001})
        alert = AlertEmail(sender="alerts@example.com", subject="New jobs for you", received_at=None,
                           links=[Link(url=url, site="greenhouse", text="Claims Analyst", context="Foo Health \u00b7 Remote",
                                       job_like=True)])
        store, ctx = setup()
        data = b"From: alerts@example.com\r\nSubject: New jobs\r\n\r\nsynthetic\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            paths = vault_paths(Path(tmp))
            inbox = paths["inbox"]
            inbox.mkdir(parents=True, exist_ok=True)

            def run(**patches):
                (inbox / "alert.eml").write_bytes(data)
                with mock.patch.object(alerts, "parse_eml_bytes", return_value=alert), \
                        mock.patch.object(cli, "fetch_single", **patches) as fetched, contextlib.redirect_stdout(io.StringIO()):
                    if "side_effect" not in patches:
                        with mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")):
                            cli.cmd_import(ctx, paths, SimpleNamespace())
                    else:
                        cli.cmd_import(ctx, paths, SimpleNamespace())
                return fetched
            run(return_value=posting)                                              # read, not stored
            self.assertTrue(list((inbox / "failed").rglob("alert.eml")))
            run(side_effect=FetchError("http_503"))                                # the retry: a temporary error
            self.assertEqual(len(list((inbox / "failed").rglob("*.eml"))), 2)           # kept in failed again
            self.assertFalse(list((inbox / "imported").rglob("*.eml")))
            self.assertTrue(store.one("SELECT fetch_failure FROM leads")[0].startswith("its own link: read but not stored"))
            (inbox / "alert.eml").write_bytes(data)                                # the next read: fetched and stored
            with mock.patch.object(alerts, "parse_eml_bytes", return_value=alert), \
                    mock.patch.object(cli, "fetch_single", return_value=posting) as fetched, \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_import(ctx, paths, SimpleNamespace())
            self.assertEqual(fetched.call_count, 1)
            self.assertTrue(list((inbox / "imported").rglob("alert.eml")))
        self.assertEqual(tuple(store.one("SELECT status, job_id FROM leads")), ("ingested", "greenhouse:foohealth:4400001"))

    def test_two_openings_at_one_page_with_different_requisitions_are_two_leads(self):
        """H26: two Claims Analyst cards at one careers page (Calgary R-9101, Austin R-9102) were read as one, and the
        Austin card alone in a later email counted as seen before."""
        from careeros.discovery.alerts import parse_eml_bytes
        store, ctx = setup()
        calgary = (self.SHARED, "Claims Analyst", "Acme Rx \u00b7 Calgary, Canada \u00b7 req=R-9101")
        austin = (self.SHARED, "Claims Analyst", "Acme Rx \u00b7 Austin, TX \u00b7 req=R-9102")
        email = parse_eml_bytes(html_mail(calgary, austin))
        self.assertEqual(len(email.links), 2)
        counts = cli._record_alert(ctx, email, mock.MagicMock())
        self.assertEqual(counts["notice_new"], 2)   # a notice, never a lead (D192 part 3)
        store, ctx = setup()
        cli._record_alert(ctx, parse_eml_bytes(html_mail(calgary)), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(austin)), mock.MagicMock())
        self.assertEqual((counts["notice_new"], counts["notice_seen_before"]), (1, 0))
        # the control: the same email again adds nothing
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(austin)), mock.MagicMock())
        self.assertEqual(counts["notice_seen_before"], 1)
        # the same place, another requisition: another opening
        store, ctx = setup()
        other = (self.SHARED, "Claims Analyst", "Acme Rx \u00b7 Austin, TX \u00b7 req=R-9103")
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(austin, other)), mock.MagicMock())
        self.assertEqual(counts["notice_new"], 2)
        # a lead stored by an older version under its link alone hides nothing, not even the same line (Codex's D190
        # design review M4; D192 part 3): each card is a notice of its own
        from careeros.discovery.alerts import lead_id
        store, ctx = setup()
        add_lead(store, lead_id(self.SHARED), "Claims Analyst", "Acme Rx \u00b7 Calgary, Canada \u00b7 req=R-9101 \u00b7 Remote",
                 url=self.SHARED)
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(austin)), mock.MagicMock())
        self.assertEqual((counts["notice_new"], counts["lead_seen_before"]), (1, 0))
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(calgary)), mock.MagicMock())
        self.assertEqual((counts["notice_new"], counts["lead_seen_before"]), (1, 0))

    def test_another_title_at_a_posting_link_is_another_opening(self):
        """The red team's rule before Pass 11 (D179), at a link that names one posting since D182: another title there is
        another opening, linked for review, never made a duplicate (the round-13 hand check found it untested there). A
        board's link since D184. Since the owner's D188 the posting's exact link is the posting whatever its title: the
        posting retitled, one job, its title and text the latest."""
        url = WORKDAY
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, url, BODY, title="Claims Analyst")
            add_url(ctx, tmp, url, BODY + "\nAnother team.", title="Claims Operations Analyst")
        rows = store.all("SELECT title, current_state FROM jobs")
        self.assertEqual([tuple(r) for r in rows], [("Claims Operations Analyst", "normalized")])

    def test_an_alert_link_without_a_route_never_takes_an_opening_with_one(self):
        """The red team's guard before Pass 11 (D179), kept under D182: an alert link that names a posting by its number
        never stands for a tracked opening whose link adds a "#" route to it (two openings of one portal). A board's
        posting link since D184 (an employer's careers page names none, its routes included)."""
        url = "https://boards.greenhouse.io/foohealth/jobs/4400001"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, url + "#/opening/B", BODY)
        job = store.one("SELECT job_id FROM jobs")["job_id"]
        # since the owner's D188 the board's posting id names the posting: a "#" part on its link names no other one
        self.assertEqual(cli._known_job(ctx, url, "Claims Analyst", "Acme Rx · Remote"), job)
        self.assertEqual(cli._known_job(ctx, url + "#/opening/B", "Claims Analyst", "Acme Rx · Remote"), job)
        self.assertIsNone(cli._known_job(ctx, url[:-1] + "2", "Claims Analyst", "Acme Rx · Remote"))   # another id

    def test_a_year_in_a_link_names_no_posting(self):
        """H27: any four digits in a link's last segment ("/careers-2026") made it name one posting, so a Maple Health
        lead for another title there was linked to Acme Rx's job by the link alone."""
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.board_discovery import names_one_posting
        for url, named in (("https://careers.example.com/careers-2026", False), ("https://careers.example.com/careers/2026", False),
                           ("https://careers.example.com/join?page=2026", False), ("https://careers.example.com/join", False),
                           ("https://careers.example.com/join/440012", False), ("https://careers.example.com/join?page=44001", False),
                           # the red team after Pass 12: listing, search and filter pages, page routes, tracking keys
                           ("https://careers.example.com/jobs/location/austin-tx-78701", False),
                           ("https://careers.example.com/jobs/archive/20261118", False),
                           ("https://careers.example.com/jobs/20261118", False),   # a date right after a job route
                           ("https://acme.wd5.myworkdayjobs.com/careers?jobFamilyGroup=91336993fab9", False),
                           ("https://careers.example.com/jobs?postingDate=20261118", False),
                           ("https://careers.example.com/jobs?jobCategoryId=10002", False),
                           ("https://careers.example.com/jobs/search?searchPositionType=10001", False),
                           ("https://careers.example.com/careers/teams/claims-10000", False),
                           ("https://careers.example.com/jobs?jobid=all", False), ("https://careers.example.com/join?requestId=839201", False),
                           ("https://careers.example.com/join?gh_jid=all", False),   # a job site's own key with no id
                           ("https://www.glassdoor.com/Job/austin-claims-analyst-jobs-SRCH_IL.0,6_IC1139761_KO7,21.htm", False),
                           ("https://jobs.example.com/portal/#/jobs", False), ("https://jobs.example.com/portal/#/search?keyword=claims", False),
                           ("https://jobs.example.com/portal/#/home", False), ("https://jobs.example.com/portal/#!/jobs", False),
                           ("https://jobs.example.com/portal/#/jobs/search", False), ("https://jobs.example.com/portal/#/opening/all", False),
                           ("https://careers.example.com/join?trkJobId=10002", False), ("https://careers.example.com/join?utm_jobid=10002", False),
                           # an employer's careers page names no posting, whatever its path, query or route (D184)
                           ("https://jobs.example.com/portal/#/opening/R-1", False), ("https://jobs.example.com/portal/#/opening/B", False),
                           ("https://careers.example.com/jobs/claims-analyst-40021", False),
                           ("https://careers.example.com/jobs/4400001", False), ("https://www.linkedin.com/jobs/view/4000000333", True),
                           ("https://wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Claims-Analyst_R-12345", True),
                           ("https://careers.example.com/join?jobId=10123", False), ("https://www.indeed.com/viewjob?jk=abc123def456", True),
                           ("https://jobs.example.com/portal/#/opening/R-1101", False)):
            self.assertEqual(names_one_posting(url), named, url)
        page = "https://careers.example.com/careers-2026"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, page, BODY)
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Claims Operations Analyst", "Maple Health"))),
                                       mock.MagicMock())
        self.assertEqual(counts["lead_ingested"], 0)
        self.assertEqual(store.one("SELECT state FROM alert_notices")[0], "new")   # a notice, never a lead (D192 part 3)

    def test_a_paste_at_a_shared_page_is_never_matched_by_the_link(self):
        """The red team after Pass 12 (RT3-3): a paste of the Austin opening (Job Number R-9102) at the careers page the
        Calgary one (no requisition) was pasted from was filed "already tracked" as Calgary, and a paste with no
        requisition was offered `careeros location` for Calgary."""
        store, ctx = setup()
        calgary = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY + CALGARY, self.SHARED, location="Calgary, AB")
        austin = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\nJob Number: R-9102\n" + BODY, self.SHARED, location="Austin, TX")
        self.assertEqual(austin["status"], "added")
        self.assertNotEqual(austin["job_id"], calgary["job_id"])
        again = intake(ctx, "Claims Analyst\nCompany: Acme Rx\n\n" + BODY, self.SHARED, location="Austin, TX")
        self.assertNotIn("careeros location", again.get("location_note") or "")

    def test_cards_at_a_shared_page_stay_apart_by_what_they_state(self):
        """The red team after Pass 12: an earlier Austin card hid a Denver card at the same careers page (RT3-4), and
        two openings' cards at a job-like page that names no posting ("/jobs/search", "#/jobs") were one lead (RT3-5);
        a gh_jid on a job slug's query was dropped."""
        from careeros.discovery.alerts import canonical_job_url, parse_eml_bytes
        store, ctx = setup()
        austin = (self.SHARED, "Claims Analyst", "Acme Rx \u00b7 Austin, TX")
        denver = (self.SHARED, "Claims Analyst", "Acme Rx \u00b7 Denver, CO")
        cli._record_alert(ctx, parse_eml_bytes(html_mail(austin)), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(denver)), mock.MagicMock())
        self.assertEqual((counts["notice_new"], counts["notice_seen_before"]), (1, 0))   # a notice, never a lead (D192 part 3)
        for page in ("https://careers.example.com/jobs/search", "https://jobs.example.com/portal/#/jobs"):
            store, ctx = setup()
            cards = ((page, "Claims Analyst", "Acme Rx \u00b7 Calgary, Canada \u00b7 req=R-9101"),
                     (page, "Claims Analyst", "Acme Rx \u00b7 Austin, TX \u00b7 req=R-9102"))
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(*cards)), mock.MagicMock())
            self.assertEqual(counts["notice_new"], 2, page)
        self.assertIn("gh_jid=4400001", canonical_job_url("https://careers.example.com/jobs/open-positions?gh_jid=4400001")[0])


class RedTeamAfterPass12Tests(unittest.TestCase):
    """The Claude red team on 8304ebf (the Pass 12 corrections; not a Codex review), the alert findings each paired with
    its correction (the owner's D182; the run scoped to the Pass 12 blockers)."""
    SHARED = "https://careers.example.com/join"
    URL = "https://boards.greenhouse.io/foohealth/jobs/4400001"

    def posting(self):
        return RawPosting(ats="greenhouse", board_token="foohealth", external_id="4400001", title="Claims Analyst",
                          location="Remote - US", url=self.URL, apply_url=self.URL, description_text=CLAIMS, raw={"id": 4400001})

    def test_alert_cards_keep_everything_they_state(self):
        """RT4-2/3/4: two openings' cards at a shared page were one lead when their place stood in a third part, was
        written "location=", or their requisition was written "Req:"; on two sites, "Job ID:" requisitions were not
        compared; and one opening became several leads through an age badge, another order, or its title linked twice."""
        from careeros.discovery.alerts import parse_eml_bytes
        for one, two in (("Acme Rx \u00b7 Full-time \u00b7 Calgary, Canada", "Acme Rx \u00b7 Full-time \u00b7 Austin, TX"),
                         ("Acme Rx \u00b7 location=Calgary, Canada", "Acme Rx \u00b7 location=Austin, TX"),
                         ("Acme Rx \u00b7 Austin, TX \u00b7 Req: R-9101", "Acme Rx \u00b7 Austin, TX \u00b7 Req: R-9102")):
            store, ctx = setup()
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((self.SHARED, "Claims Analyst", one),
                                                                      (self.SHARED, "Claims Analyst", two))), mock.MagicMock())
            self.assertEqual(counts["notice_new"], 2, (one, two))   # a notice, never a lead (D192 part 3)
        store, ctx = setup()
        cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.linkedin.com/jobs/view/4000000444", "Claims Analyst",
                                                          "Acme Rx \u00b7 Austin, TX \u00b7 Job ID: R-9101"))), mock.MagicMock())
        counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(("https://www.indeed.com/viewjob?jk=abc123def456",
                                                                   "Claims Analyst", "Acme Rx \u00b7 Austin, TX \u00b7 Job ID: R-9102"))),
                                   mock.MagicMock())
        self.assertEqual((counts["lead_new"], counts["lead_seen_before"]), (1, 0))
        # one opening: an age badge, another order of its parts, its title linked twice
        store, ctx = setup()
        for line in ("Posted 2 days ago \u00b7 Acme Rx \u00b7 Austin, TX \u00b7 Job ID: R-9101",
                     "Posted 3 days ago \u00b7 Acme Rx \u00b7 Austin, TX \u00b7 Job ID: R-9101",
                     "Austin, TX \u00b7 Job ID: R-9101 \u00b7 Acme Rx \u00b7 New"):
            cli._record_alert(ctx, parse_eml_bytes(html_mail((self.SHARED, "Claims Analyst", line))), mock.MagicMock())
        # since D192 part 3 every card occurrence is a notice of its own, never merged by its text: the same opening in
        # three emails is three notices, shown together (grouping is display only), and never a lead
        self.assertEqual((store.one("SELECT COUNT(*) FROM alert_notices")[0], store.one("SELECT COUNT(*) FROM leads")[0]), (3, 0))
        html = (f'<p><a href="{self.SHARED}">Claims Analyst</a></p><p><a href="{self.SHARED}">Claims Analyst</a> Acme Rx '
                f'\u00b7 Austin, TX</p>')
        raw = ("From: jobs@example.com\r\nTo: someone@example.com\r\nSubject: New jobs for you\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/html; charset=utf-8\r\n\r\n<html><body>" + html + "</body></html>").encode()
        self.assertEqual(len(parse_eml_bytes(raw).links), 2)   # both occurrences kept (D192 part 3)

    def test_an_inbox_issue_with_a_line_not_stored_stays_open(self):
        """RT4-5: the GitHub inbox recorded and closed an issue whose line was read but not stored."""
        from careeros.discovery import github_inbox as inbox
        from tests.test_github_inbox import FakeGh, issue
        repo = "owner-x/example-inbox"
        body = f"Title | Company | Location | Link | Note\nClaims Analyst | Foo Health | Remote - US | {self.URL} | source=alert\n"
        store, ctx = setup()
        with mock.patch.object(inbox, "gh_path", lambda: "gh"), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "fetch_single", return_value=self.posting()), \
                    mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")):
                cli._inbox_pull(ctx, mock.MagicMock(), repo, runner=FakeGh([issue(7, body)]))
            self.assertIsNone(store.one("SELECT 1 FROM inbox_issues WHERE number = 7"))
            with mock.patch.object(cli, "fetch_single", return_value=self.posting()) as fetched:
                cli._inbox_pull(ctx, mock.MagicMock(), repo, runner=FakeGh([issue(7, body)]))
            self.assertEqual(fetched.call_count, 1)
        self.assertEqual(store.one("SELECT closed FROM inbox_issues WHERE number = 7")[0], 1)
        self.assertEqual(store.one("SELECT job_id FROM leads")[0], "greenhouse:foohealth:4400001")

    def test_an_alert_with_a_line_not_stored_is_read_again_after_its_search_window(self):
        """RT4-6: the Gmail retry of a line read but not stored lasted only while the search listed the email."""
        from careeros.discovery.http import FetchError
        from tests.test_gmail import Scripted
        raw = html_mail((self.URL, "Claims Analyst", "Foo Health"))
        store, ctx = setup()
        with mock.patch.object(cli, "fetch_single", return_value=self.posting()), \
                mock.patch.object(pipeline, "_observe_posting", side_effect=RuntimeError("interrupted")):
            cli._gmail_pull(ctx, mock.MagicMock(), "newer_than:3d", gmail=Scripted(["m1"], {"m1": raw}))
        with mock.patch.object(cli, "fetch_single", side_effect=FetchError("http_503")):
            cli._gmail_pull(ctx, mock.MagicMock(), "newer_than:3d", gmail=Scripted(["m1"], {"m1": raw}))
        self.assertTrue(store.one("SELECT 1 FROM exceptions WHERE error_code = ? AND entity_id = 'm1' AND status != 'resolved'",
                                  (cli.UNSTORED_LINE,)))
        with mock.patch.object(cli, "fetch_single", return_value=self.posting()) as fetched:
            cli._gmail_pull(ctx, mock.MagicMock(), "newer_than:3d", gmail=Scripted([], {"m1": raw}))   # no longer listed
        self.assertEqual(fetched.call_count, 1)
        self.assertEqual(store.one("SELECT job_id FROM leads")[0], "greenhouse:foohealth:4400001")
        self.assertTrue(store.one("SELECT 1 FROM alert_messages WHERE message_id = 'm1'"))
        self.assertFalse(store.one("SELECT 1 FROM exceptions WHERE error_code = ? AND status != 'resolved'", (cli.UNSTORED_LINE,)))


class CodexPass13Tests(unittest.TestCase):
    """Codex CR-2026-11-15-GATE-FIXES Pass 13 (H29-H33), each reproduction paired with its correction (the owner's D183:
    standing preparation off; the identity findings fixed as Codex recommends). H28 (a failed scan write leaving an
    earlier scan's US location in force) acted only through standing preparation, which is off."""
    SHARED = "https://careers.example.com/join"

    def test_a_workable_jobs_own_country_abroad_is_written(self):
        """H30: country=CA at the job and locations=[{region FL, country US}] became "FL, US (Remote)", plainly US."""
        from careeros.discovery.boards import workable_posting
        posting = workable_posting("synth", {"shortcode": "W1", "title": "Claims Analyst", "telecommuting": True, "country": "CA",
                                             "locations": [{"region": "FL", "country": "US"}], "description": "<p>x</p>"})
        self.assertEqual(posting.location, "FL, US; Canada (Remote)")
        self.assertEqual(detect_country(posting.location, "", "remote"), "unknown")
        control = workable_posting("synth", {"shortcode": "W2", "title": "Claims Analyst", "telecommuting": True, "country": "US",
                                             "locations": [{"region": "FL", "country": "US"}], "description": "<p>x</p>"})
        self.assertEqual((control.location, detect_country(control.location, "", "remote")), ("FL, US (Remote)", "US"))

    def test_a_year_or_date_in_a_query_or_app_route_names_no_posting(self):
        """H31: "?job=2026", "?job=20261118" and "#/jobs/2026" named one posting, so a Maple Health lead there joined
        Acme Rx's job by the link."""
        from careeros.discovery.alerts import parse_eml_bytes
        from careeros.discovery.board_discovery import names_one_posting
        for url, named in (("https://careers.example.com/careers?job=2026", False),
                           ("https://careers.example.com/careers?job=20261118", False),
                           ("https://careers.example.com/portal#/jobs/2026", False),
                           ("https://careers.example.com/careers?job=40021", False),   # a careers page (D184)
                           ("https://careers.example.com/portal#/opening/R-1", False),
                           ("https://www.indeed.com/viewjob?jk=abc123def456", True)):
            self.assertEqual(names_one_posting(url), named, url)
        page = "https://careers.example.com/careers?job=2026"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, page, BODY)
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((page, "Claims Operations Analyst", "Maple Health"))),
                                       mock.MagicMock())
        self.assertEqual(counts["lead_ingested"], 0)

    def test_a_second_opening_at_a_shared_page_never_takes_the_first_ones_place(self):
        """H32: two Claims Analyst postings at one careers page (same employer, title and place, no requisition, other
        text) were placed at one generated id: the second replaced the first's text and kept its Apply label."""
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, self.SHARED, BODY + "\nMedicaid enrollment files.")
            process(ctx)   # screened: a preview is given only then (the D190 design)
            first = store.one("SELECT job_id FROM jobs")["job_id"]
            give_label(store, ctx.policy, ctx.profile, first, "apply", None, "k-apply")
            add_url(ctx, tmp, self.SHARED, BODY + "\nPharmacy payment appeals.")
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 2)
            self.assertIn("Medicaid", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (first,))[0])
            other = store.one("SELECT job_id FROM jobs WHERE job_id != ?", (first,))["job_id"]
            self.assertIsNone(packages.route_for(store, ON, other)[0])
            add_url(ctx, tmp, self.SHARED, BODY + "\nMedicaid enrollment files.")    # the control: the same text, a retry
        self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 2)

    def test_the_greenhouse_id_fallback_checks_what_the_line_states(self):
        """H33: a link carrying gh_jid=4400001 and an app route naming R-2222, from another employer and title with
        req=R-2222, was linked to the tracked Greenhouse job by its gh_jid."""
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        ingest(ctx, dict(store.one("SELECT * FROM employers")),
               [greenhouse_posting("synth", gh_item(4400001, "Claims Analyst", FULL_TIME, "synth", req="R-4400"))])
        job = "greenhouse:synth:4400001"
        routed = "https://careers.example.com/portal?gh_jid=4400001#/opening/R-2222"
        self.assertIsNone(cli._known_job(ctx, routed, "Claims Operations Analyst", "Maple Health \u00b7 req=R-2222"))
        self.assertIsNone(cli._known_job(ctx, routed, "Claims Analyst", "Synthetic Health \u00b7 Remote"))   # its route names another
        plain = "https://careers.example.com/portal?gh_jid=4400001"
        self.assertIsNone(cli._known_job(ctx, plain, "Claims Operations Analyst", "Maple Health \u00b7 req=R-2222"))
        self.assertIsNone(cli._known_job(ctx, plain, "Claims Analyst", "Synthetic Health \u00b7 req=R-2222"))
        # since the owner's D188 an employer's careers page names no posting, its gh_jid included: never the job by it
        self.assertIsNone(cli._known_job(ctx, plain, "Claims Analyst", "Synthetic Health \u00b7 Remote"))
        own = "https://job-boards.greenhouse.io/synth/jobs/4400001"   # the control: the board's own posting link
        self.assertEqual(cli._known_job(ctx, own, "Claims Operations Analyst", "Maple Health \u00b7 req=R-2222"), job)


class CodexPass14Tests(unittest.TestCase):
    """Codex CR-2026-11-15-GATE-FIXES Pass 14 (H34-H36), each reproduction paired with its correction (the owner's D184:
    the three fixed, and only a board's posting link or a job site's id names one posting)."""
    SHARED = "https://careers.example.com/join"

    def test_a_handoff_issued_under_standing_preparation_is_withdrawn(self):
        """H34: an assisted session opened under standing preparation before the upgrade (no label) was returned whole
        by its key after it, with its handoff still usable; the session itself stays for you to close."""
        from careeros.review import assist
        from careeros.review.labels import record_label
        from tests.test_assist import GREENHOUSE_FILL
        from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store
        unpause = mock.patch.object(assist, "FILL_PAUSED", False)   # the inert fill code (D191 pauses it)
        unpause.start()
        self.addCleanup(unpause.stop)

        def opened(store, vault, key):
            return assist.open_session(store, POLICY, MID, JOB, "assisted", facts=FACTS, library=vault.library,
                                       vault_root=vault.root, questions=QUESTIONS, key=key)
        vault = Vault()
        self.addCleanup(vault.close)
        store, ctx = posting_store()
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with mock.patch.object(packages, "route_for", return_value=("standing", "the standing rule (before D183)")):
                packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                                 questions=QUESTIONS)
                first = opened(store, vault, "legacy")                       # as the earlier version issued it
            handoff = vault.root / first["handoff"]
            self.assertTrue(handoff.is_file())
            with self.assertRaisesRegex(assist.SessionRefused, "session_withdrawn"):
                opened(store, vault, "legacy")                               # its key again, after the upgrade
        self.assertFalse(handoff.exists())
        self.assertEqual(assist.session_state(store, first["session_id"]), "open")   # yours to close
        # your Apply label later never revives it: its package was prepared under standing preparation
        give_label(store, ctx.policy, ctx.profile, JOB, "apply", None, "k-apply-later")
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL), \
                self.assertRaisesRegex(assist.SessionRefused, "prepared under standing preparation"):
            opened(store, vault, "legacy")
        # the first processing run after the upgrade withdraws it too, without any open
        vault = Vault()
        self.addCleanup(vault.close)
        store, ctx = posting_store()
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            with mock.patch.object(packages, "route_for", return_value=("standing", "the standing rule (before D183)")):
                packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                                 questions=QUESTIONS)
                second = opened(store, vault, "legacy-2")
        with contextlib.redirect_stdout(io.StringIO()):
            counts = cli.cmd_process(ctx, {"root": vault.root}, SimpleNamespace())
        self.assertEqual(counts["handoffs_withdrawn"], 1)
        self.assertFalse((vault.root / second["handoff"]).exists())
        # your Skip after a labelled session withdraws it as well; the control: your Apply label keeps the session (its
        # file goes at any sweep: browser Fill is paused, D191)
        vault = Vault()
        self.addCleanup(vault.close)
        store, ctx = posting_store()
        give_label(store, ctx.policy, ctx.profile, JOB, "apply", None, "k-apply")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root, questions=QUESTIONS)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            third = opened(store, vault, "labelled")
            self.assertEqual(opened(store, vault, "labelled")["session_id"], third["session_id"])   # the control
            record_label(store, JOB, "skip", None, "k-skip")
            with self.assertRaisesRegex(assist.SessionRefused, "session_withdrawn: you labelled it Skip"):
                opened(store, vault, "labelled")
        self.assertFalse((vault.root / third["handoff"]).exists())

    def test_a_guessed_greenhouse_fetch_never_links_a_line_that_states_another_opening(self):
        """H35: after the tracked-job lookup refused a line (another requisition, title or employer, or an app route
        naming another opening), the importer guessed a Greenhouse link from the line's gh_jid, fetched the tracked
        posting and linked the lead to it."""
        from careeros.discovery.alerts import parse_eml_bytes
        store, ctx = setup()
        legacy_board(store, name="Synthetic Health", token="synth")
        item = gh_item(4400001, "Claims Analyst", FULL_TIME, "synth", req="R-4400")
        ingest(ctx, dict(store.one("SELECT * FROM employers")), [greenhouse_posting("synth", item)])
        posting = greenhouse_posting("synth", item)
        lines = (("https://careers.synth.example/portal?gh_jid=4400001#/opening/R-2222", "Claims Operations Analyst",
                  "Maple Health \u00b7 req=R-2222"),
                 ("https://careers.synth.example/portal?gh_jid=4400001", "Claims Analyst", "Synthetic Health \u00b7 req=R-2222"),
                 ("https://careers.synth.example/portal?gh_jid=4400001", "Claims Operations Analyst", "Maple Health"))
        for line in lines:
            store.conn.execute("DELETE FROM alert_notices")
            with mock.patch.object(cli, "fetch_single", return_value=posting) as fetched:
                counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(line)), mock.MagicMock())
            self.assertEqual(counts["lead_ingested"], 0, line)
            self.assertEqual(tuple(store.one("SELECT state, job_id FROM alert_notices")), ("new", None), line)
            self.assertEqual(fetched.call_count, 0, line)   # since D192 part 3 the guess is a suggestion, never fetched
        # since the owner's D188 a careers-page link names no posting, its gh_jid included: a line there that agrees is
        # never joined by the lookup or by a guessed fetch; a guessed posting tracked already is not fetched again
        store.conn.execute("DELETE FROM alert_notices")
        with mock.patch.object(cli, "fetch_single", return_value=posting) as fetched:
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((lines[1][0], "Claims Analyst", "Synthetic Health"))),
                                       mock.MagicMock())
        self.assertEqual((counts["lead_ingested"], counts["notice_new"], fetched.call_count), (0, 1, 0))
        other = greenhouse_posting("synth", gh_item(4400002, "Claims Analyst", FULL_TIME, "synth", req="R-4402"))
        with mock.patch.object(cli, "fetch_single", return_value=other) as fetched:
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(
                ("https://careers.synth.example/portal?gh_jid=4400002", "Claims Analyst", "Synthetic Health \u00b7 req=R-4402"))),
                mock.MagicMock())
        self.assertEqual((counts["lead_ingested"], fetched.call_count), (0, 0))
        notice = store.one("SELECT job_id, suggestion_json FROM alert_notices WHERE url LIKE '%4400002'")
        self.assertIsNone(notice["job_id"])
        self.assertIn("4400002", notice["suggestion_json"])   # the guess, shown on the notice
        # its own board link, whatever requisition the line states: that posting (the exact link, D188)
        store.conn.execute("DELETE FROM leads")
        with mock.patch.object(cli, "fetch_single", return_value=other):
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(
                ("https://job-boards.greenhouse.io/synth/jobs/4400002", "Claims Analyst", "Synthetic Health \u00b7 req=R-9999"))),
                mock.MagicMock())
        self.assertEqual((counts["lead_ingested"], counts["lead_other_posting"]), (1, 0))
        self.assertEqual(store.one("SELECT job_id FROM leads")[0], "greenhouse:synth:4400002")
        # the control: its own board link, fetched (not tracked yet), joins it whatever the line calls it, when no other
        # requisition is stated
        third = greenhouse_posting("synth", gh_item(4400003, "Claims Analyst", FULL_TIME, "synth", req="R-4403"))
        with mock.patch.object(cli, "fetch_single", return_value=third) as fetched:
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail(
                ("https://job-boards.greenhouse.io/synth/jobs/4400003", "Claims Operations Lead", "Synthetic Health"))),
                mock.MagicMock())
        self.assertEqual((counts["lead_ingested"], fetched.call_count), (1, 1))

    def test_only_a_boards_posting_link_or_a_job_sites_id_names_one_posting(self):
        """H36 and the owner's D184: "/jobs/archive_2026" named one posting through the Workday-style suffix rule on any
        host, so a second opening there took the first's place and Apply label; since D184 an employer's careers page
        names no posting whatever its path, query or route."""
        from careeros.discovery.board_discovery import names_one_posting
        for url, named in (("https://careers.example.com/jobs/archive_2026", False),
                           ("https://careers.example.com/jobs/archive_20261118", False),
                           ("https://careers.example.com/jobs/archive-2026", False),
                           ("https://careers.example.com/job/Austin-TX/Claims-Analyst_R-12345", False),   # not Workday's host
                           ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/archive_2026", False),   # a year
                           ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/archive_20261118", False),   # a date
                           ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Claims-Analyst_R-12345#/b", False),
                           ("https://acme.wd5.myworkdayjobs.com/en-US/External/Claims-Analyst_R-12345", False),   # no job route
                           ("https://myworkdayjobs.com.example.net/External/job/Austin/Claims_R-12345", False),
                           ("https://linkedin.com.example.net/jobs/view/4000000333", False),
                           ("https://careers.example.com/jobs?jk=abc123def456", False),
                           ("https://careers.example.com/portal?gh_jid=4400001", False),
                           ("https://careers.example.com/jobs/4400001", False),
                           ("https://boards.greenhouse.io/foohealth/jobs/4400001", True),
                           ("https://jobs.lever.co/acme/0f6d1c2e-1111-4a2b-9c3d-123456789abc", True),
                           ("https://jobs.ashbyhq.com/acme/0f6d1c2e-1111-4a2b-9c3d-123456789abc", True),
                           ("https://apply.workable.com/acme/j/ABC123DEF4/", True),
                           ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Claims-Analyst_JR0012345", True),
                           ("https://ca.indeed.com/viewjob?jk=abc123def456", True),
                           ("https://www.glassdoor.com/job-listing/j?jl=1009876543", True),
                           ("https://www.linkedin.com/jobs/view/claims-analyst-at-acme-4000000333", True)):
            self.assertEqual(names_one_posting(url), named, url)

    def test_a_second_opening_at_an_archive_link_never_takes_the_first_ones_place(self):
        """H36: two Claims Analyst openings at "/jobs/archive_2026" (same place, no requisition, other text): the second,
        added or imported, replaced the first's text and kept its Apply label; a lead from another employer there was
        linked to the tracked job."""
        from careeros.discovery.alerts import parse_eml_bytes
        archive = "https://careers.example.com/jobs/archive_2026"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, archive, BODY + "\nMedicaid enrollment files.")
            process(ctx)   # screened: a preview is given only then (the D190 design)
            first = store.one("SELECT job_id FROM jobs")["job_id"]
            give_label(store, ctx.policy, ctx.profile, first, "apply", None, "k-apply")
            add_url(ctx, tmp, archive, BODY + "\nPharmacy payment appeals.")
            raw, employer = parse_saved_text(f"URL: {archive}\nTitle: Claims Analyst\nEmployer: Acme Rx\nLocation: Remote - US\n\n"
                                             + BODY + "\nPharmacy payment appeals, saved.", "p.txt")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
            self.assertEqual(store.one("SELECT COUNT(*) FROM jobs")[0], 3)
            self.assertIn("Medicaid", store.one("SELECT description_text FROM jobs WHERE job_id = ?", (first,))[0])
            for other in (r[0] for r in store.all("SELECT job_id FROM jobs WHERE job_id != ?", (first,))):
                self.assertIsNone(packages.route_for(store, ON, other)[0])
            counts = cli._record_alert(ctx, parse_eml_bytes(html_mail((archive, "Claims Operations Analyst", "Maple Health"))),
                                       mock.MagicMock())
        self.assertEqual(counts["lead_ingested"], 0)
        self.assertEqual(store.one("SELECT state FROM alert_notices")[0], "new")   # a notice, never a lead (D192 part 3)


class RedTeamBeforePass15Tests(unittest.TestCase):
    """The red team the owner asked for before Codex Pass 15 (D185; run from the coordinating session, not a Codex
    review), each reproduction paired with its correction."""

    def test_a_paste_stating_another_place_is_never_a_sighting_of_a_tracked_job(self):
        """RTC-01: a paste with no link, the same employer, title and text, and "Location: Remote - LATAM" was reported as
        already tracked as a board's Denver posting (matched by employer and title alone)."""
        store, ctx = setup()
        legacy_board(store, name="Acme RX Labs", token="acmerxlabs")
        ingest(ctx, dict(store.one("SELECT * FROM employers")),
               [greenhouse_posting("acmerxlabs", gh_item(905, "Claims Analyst II", BODY, "acmerxlabs", location="Denver, CO"))])
        process(ctx)
        board = "greenhouse:acmerxlabs:905"
        latam = intake(ctx, "Claims Analyst II\nCompany: Acme RX Labs\nLocation: Remote - LATAM\n\n" + BODY, None)
        self.assertEqual(latam["status"], "added")
        self.assertNotEqual(latam["job_id"], board)
        # the controls: a paste of the board's posting without its link is its own job too (its text is not the board's:
        # RTC-04), and the same paste again is its retry, recognised by its receipt (the D190 design, section 6)
        for line in ("Location: Denver, CO\n", ""):
            text = f"Claims Analyst II\nCompany: Acme RX Labs\n{line}\n" + BODY
            first = intake(ctx, text, None)
            self.assertNotEqual(first["job_id"], board, line)
            again = intake(ctx, text, None)
            self.assertEqual((again["job_id"], again["status"]), (first["job_id"], "recorded"), line)

    def test_a_paste_with_another_text_is_never_a_sighting_by_employer_and_title(self):
        """RTC-04: with the same employer and title, a paste whose text is another posting (an Ontario-only role), or
        the first one's text with one line added, was reported as already tracked as the first, at a shared careers page
        and with no link, so the second opening was swallowed and its sighting went to the first."""
        other = ("About the role\nWe are hiring a pharmacy benefits analyst for our Ontario team.\n\nResponsibilities:\n"
                 "- Audit pharmacy claims for Canadian plan members\n- Work with provincial formularies\n\nRequirements:\n"
                 "- 2+ years of pharmacy benefits experience\n\nThis role is open to residents of Ontario, Canada only. "
                 "Pay range: CAD 70,000 - 80,000 per year.")
        for url in ("https://acmerxlabs.example/careers", None):
            for second in (other, BODY + "\nThis role is open to residents of the United Kingdom only."):
                store, ctx = setup()
                first_text = "Claims Analyst II\nCompany: Acme RX Labs\nLocation: US Remote\n\n" + BODY
                a = intake(ctx, first_text, url)
                process(ctx)
                b = intake(ctx, "Claims Analyst II\nCompany: Acme RX Labs\n\n" + second, url)
                self.assertEqual(b["status"], "added", url)
                self.assertNotEqual(b["job_id"], a["job_id"], url)
                # the controls: the same paste again is a sighting; a requisition both state is the same opening
                self.assertEqual(intake(ctx, first_text, url)["job_id"], a["job_id"])
        # one requisition both state is no longer the same opening (the owner's D188): its own job, linked for review
        store, ctx = setup()
        a = intake(ctx, "Claims Analyst II\nCompany: Acme RX Labs\nReq ID: REQ1\n\n" + BODY, None)
        b = intake(ctx, "Claims Analyst II\nCompany: Acme RX Labs\nReq ID: REQ1\n\n" + BODY + "\nEdited.", None)
        self.assertEqual(b["status"], "added")
        self.assertNotEqual(b["job_id"], a["job_id"])
        self.assertTrue(store.one("SELECT 1 FROM opening_links WHERE relation = 'possible_duplicate'"))

    def test_a_stated_link_that_is_not_a_full_address_is_still_a_link(self):
        """RTC-02: a paste stating a Workday link without "https://" was reported as already tracked as another opening
        tracked at a LinkedIn link (same employer, title and text): the link was dropped and the title matched."""
        workday = "northwind.wd5.myworkdayjobs.com/careers/job/Remote/Claims-Specialist_R-44000"
        with tempfile.TemporaryDirectory() as tmp:
            store, ctx = setup()
            add_url(ctx, tmp, "https://www.linkedin.com/jobs/view/4000000001", BODY, employer="Northwind Care",
                    title="Claims Specialist", location=None)
        process(ctx)
        first = store.one("SELECT job_id FROM jobs")["job_id"]
        made = {}
        for url in (workday, "https://" + workday):
            out = intake(ctx, "Claims Specialist\nCompany: Northwind Care\n\n" + BODY, url)
            self.assertNotEqual(out["job_id"], first, url)
            made[url] = out["job_id"]
        # the control: the same typed link again is that posting (its retry, by its receipt: section 6)
        again = intake(ctx, "Claims Specialist\nCompany: Northwind Care\n\n" + BODY, workday)
        self.assertEqual((again["status"], again["job_id"]), ("recorded", made[workday]))

    def test_an_interrupted_prepare_can_be_prepared_again(self):
        """RTC-03: a prepare interrupted after it wrote the version's folder, before its row was committed, refused every
        retry (FileExistsError); the job could never be prepared again without cleaning up by hand."""
        from careeros.db import store as store_mod
        from tests.test_packages import FACTS, JOB, MID, POLICY, QUESTIONS, Vault, posting_store
        vault = Vault()
        self.addCleanup(vault.close)
        store, ctx = posting_store()
        give_label(store, ctx.policy, ctx.profile, JOB, "apply", None, "k-apply")
        original = store_mod.Writer.insert

        def interrupted(writer, table, row):
            if table == "packages":
                raise RuntimeError("interrupted after the folder was written")
            return original(writer, table, row)
        with mock.patch.object(store_mod.Writer, "insert", interrupted), self.assertRaises(RuntimeError):
            packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                             questions=QUESTIONS)
        self.assertEqual(store.one("SELECT COUNT(*) FROM packages")[0], 0)
        out = packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                               questions=QUESTIONS)
        self.assertEqual((out["new"], out["version"]), (True, 1))
        self.assertTrue((vault.root / out["folder"] / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
