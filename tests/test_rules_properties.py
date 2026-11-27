"""Generated property tests for the two rules the owner's D180 rests on (after Codex CR-2026-11-15-GATE-FIXES Pass 11).

1. The plain-US rule: a location field places a posting in the US only when every part of it is plainly a US place or a
   neutral word. Thousands of generated fields, built from parts of known kinds and joined every way a field is written.
2. The identity rule: a stored posting is never taken for an incoming one that states something it contradicts (another
   employer, title, requisition, or link with its "#" route), on every manual route and for leads.
3. The invariant Codex Pass 12 is asked about: only a job board's own scan with a plainly US location field is picked by
   standing preparation; every other route and field waits for your label.

The generator is seeded (the same cases every run), so a failure names its case and can be replayed."""
import contextlib
import io
import random
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from careeros import cli
from careeros.discovery.boards import greenhouse_posting
from careeros.discovery.board_discovery import job_identity, posting_identity
from careeros.discovery.manual_intake import intake, same_manual_posting
from careeros.discovery.saved import parse_saved_text
from careeros.normalize.posting import _field_parts, _time_only, _us_only, detect_country, norm
from careeros.paths import vault_paths
from careeros.pipeline import ingest, process
from careeros.review import packages
from tests.helpers import give_label
from tests.test_lead_resolution import legacy_board
from tests.test_packages import FULL_TIME, policy
from tests.test_v26_discovery import MID, gh_item, setup

SEED = 20261118
ON = policy(enabled=True)
BODY = FULL_TIME.replace("This role is remote in the US. ", "")

# parts that are plainly a US place
US_PARTS = ["US", "USA", "U.S.", "United States", "United States of America", "Texas", "New York", "California", "Ohio",
            "TX", "NY", "WA", "OH", "FL", "Austin, TX", "Portland, OR", "New York, NY", "Chicago, IL, US", "Denver, CO, USA",
            "Indianapolis, IN, United States", "Washington, DC"]
# words that say how or when, never where
NEUTRAL = ["Remote", "Hybrid", "Onsite", "Work from home", "Fully remote", "WFH", "Telecommute", "Eastern time zone",
           "Pacific time", "Any time zone", "Remote only"]
# parts that are not plainly a US place: other countries and their cities, regions, a city with a code a country
# shares, codes that are no state's, and words that qualify a place (hours, exclusions, alternatives)
FOREIGN = ["Canada", "UK", "Ecuador", "UAE", "Toronto, ON", "Berlin, DE", "Pune, IN", "EMEA", "LATAM", "Dublin", "Mexico",
           "Georgia", "Portugal", "Philippines", "Manila", "Kenya", "Ontario", "Tbilisi, Georgia", "PT", "ET", "UK", "Mumbai, IN",
           "Remote - Canada (US hours)", "Bogota, CO", "Remote - Ontario",
           # the red team after Pass 11: other scripts and look-alike letters, flags, squashed names, filler alone
           "\u041c\u0415\u0425\u0406\u0421\u041e", "\uff2d\uff45\uff58\uff49\uff43\uff4f", "\u6771\u4eac", "\U0001F1F2\U0001F1FD",
           "\u041a\u0430\u043d\u0430\u0434\u0430", "Hongkong", "Can-ada", "In", "Any", "us, in"]
# a code a country shares, alone: placed only in a list of states with no city ("AZ, CO, TX") or before ", US"
SHARED = ["IN", "CA", "DE", "CO", "IL", "GA", "PA", "MA"]
QUALIFIERS = ["US hours", "not US", "outside US", "excluding US", "US applicants ineligible", "between US and UAE",
              "except Canada", "US time zones preferred", "worldwide", "global", "anywhere", "international", "EU",
              "not hybrid", "US or Canada", "US/Canada", "Americas", "North America",
              # "US" as a time zone, codes joined inside a part ("US OR CA", "CA OK"), a shared code before ", US"
              "US Time Zone", "US/Eastern", "US Central", "Any US time zone", "US OR CA", "CA OK", "IN OK"]
SEPS = [" - ", ", ", " / ", "; ", " | ", " or ", " and ", " – ", " • ", " & ", ": "]


def field(rng: random.Random, parts: list[str]) -> str:
    """Parts joined by separators, a part sometimes in brackets ("Remote (Austin, TX)"): brackets are always closed
    (an open one places nothing since Codex Pass 12 H22)."""
    out = parts[0]
    for part in parts[1:]:
        out += f" ({part})" if rng.random() < 0.15 else rng.choice(SEPS) + part
    return out


def spoiled(rng: random.Random, location: str) -> str:
    """A field made to ask, trail off or leave a bracket open (Codex CR-2026-11-15-GATE-FIXES H22, Pass 12)."""
    sep = rng.choice(SEPS).strip()
    if " " not in location:
        location = "Remote - " + location   # so every spoiling below changes it
    return rng.choice([location + "?", location + " ?", location + "!", f'"{location}"', f"{location} {sep}",
                       f"{sep} {location}", location + " ()", "(" + location, location + ")", location + " []",
                       location.replace(" ", "? ", 1), f"{location} {sep} {sep} Remote"])


class PlainUsPlaceTests(unittest.TestCase):
    def test_a_field_with_any_part_that_is_not_plainly_a_us_place_never_places_it_in_the_us(self):
        """Safety: one part from another country, a code shared with one, or a qualifier, anywhere in the field, among
        any number of US places and neutral words, joined any way: never US."""
        rng = random.Random(SEED)
        for case in range(4000):
            parts = [rng.choice(US_PARTS + NEUTRAL) for _ in range(rng.randint(0, 3))]
            parts.insert(rng.randint(0, len(parts)), rng.choice(FOREIGN + QUALIFIERS))
            location = field(rng, parts)
            self.assertNotEqual(detect_country(location, "", "remote"), "US", f"case {case}: {location!r}")

    def test_a_field_that_asks_trails_off_or_leaves_a_bracket_open_never_places_it(self):
        """H22 (Codex Pass 12): "Remote - US?" read as a US placement. A plainly US field made to ask, end in a
        separator, or open a bracket is never US."""
        rng = random.Random(SEED + 12)
        for case in range(3000):
            parts = [rng.choice(US_PARTS + NEUTRAL) for _ in range(rng.randint(1, 3))]
            location = spoiled(rng, field(rng, parts))
            self.assertNotEqual(detect_country(location, "", "remote"), "US", f"case {case}: {location!r}")

    def test_a_code_a_country_shares_places_nothing_beside_a_city_or_alone(self):
        """A shared code ("IN": India or Indiana), with US words, neutral words and a city, but no list of states by
        their codes: US only when ", US" (or "USA", "United States") follows it ("Chicago, IL, US"). "Mumbai, IN | Austin,
        TX" is two offices; "Remote - US, CA" is two countries."""
        rng = random.Random(SEED + 5)
        words = ["US", "USA", "United States", "Texas", "New York", "California"] + NEUTRAL
        cities = ["Austin, TX", "Portland, OR", "New York, NY", "Mumbai, IN", "Toronto, CA", "Chicago, IL, US"]
        followed = re.compile(r"\b(?:" + "|".join(SHARED) + r"), (?:US|USA|U\.S\.|United States)\b")
        for case in range(4000):
            parts = [rng.choice(words) for _ in range(rng.randint(0, 3))]
            if rng.random() < 0.5:
                parts.insert(rng.randint(0, len(parts)), rng.choice(cities))
            parts.insert(rng.randint(0, len(parts)), rng.choice(SHARED))
            location = field(rng, parts)
            if detect_country(location, "", "remote") == "US":
                self.assertRegex(location, followed, f"case {case}: {location!r}")

    def test_a_field_of_us_places_and_neutral_words_places_it_in_the_us(self):
        """The control: only US places and neutral words, with at least one US place, joined any way: US."""
        rng = random.Random(SEED + 1)
        for case in range(3000):
            parts = [rng.choice(US_PARTS + NEUTRAL) for _ in range(rng.randint(0, 3))]
            parts.insert(rng.randint(0, len(parts)), rng.choice(US_PARTS))
            location = field(rng, parts)
            if re.search(r"(?:US|U\.S\.|USA) ?/ ?(?:Eastern|Central|Mountain|Pacific)", location):
                continue   # "US/Eastern" is a time zone's name, never a place: unknown by design
            split, _ = _field_parts(" ".join(location.split()))
            if any((_us_only(a) and _time_only(b)) or (_time_only(a) and _us_only(b)) for a, b in zip(split, split[1:], strict=False)):
                continue   # a time zone beside "US", either order, reads it as the zone (the red team after Codex Pass 12,
                # RT2-1; Codex Pass 13 H29): unknown by design
            self.assertEqual(detect_country(location, "", "remote"), "US", f"case {case}: {location!r}")

    def test_neutral_words_alone_and_arbitrary_text_never_place_it(self):
        """No US place, no US placement: neutral words alone, empty fields, and random text of letters and punctuation
        (which never raises)."""
        rng = random.Random(SEED + 2)
        for case in range(1000):
            location = field(rng, [rng.choice(NEUTRAL) for _ in range(rng.randint(1, 3))])
            self.assertEqual(detect_country(location, "", "remote"), "unknown", f"case {case}: {location!r}")
        alphabet = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ,;:/|()[]-\u2013\u2022&.'#\u00e9\u0430\u200b"
        for case in range(3000):
            location = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            got = detect_country(location, "", "remote")
            self.assertIn(got, ("US", "non_US", "unknown"))
            if got == "US":   # random text placed in the US must hold a US place word or code
                self.assertTrue(any(w in norm(location).split() for w in ("us", "usa", "u", "states")) or
                                any(len(w) == 2 and w.isupper() for w in location.replace(",", " ").split()) or
                                any(name in norm(location) for name in ("texas", "ohio", "york", "california")),
                                f"case {case}: {location!r}")


def _row(url, title, req, employer, text="Body.", location="Remote - US"):
    return {"job_id": "manual:acme-rx:p1", "url": url, "title": title, "title_norm": norm(title) if title else None,
            "requisition_id": req, "employer_name": employer, "parent": employer, "description_text": text,
            "location_raw": location}


class IdentityRuleTests(unittest.TestCase):
    """The owner's D188 (2026-11-19, "Exact links only") as a property over every combination of link, title,
    requisition, employer and text: two postings are one job only when both links name one posting and it is the same
    posting (a board's posting link, a Workday requisition link, a job site's id, in any of its link forms)."""
    LINKS = ["https://careers.example.com/join", "https://careers.example.com/portal#/opening/A",
             "https://careers.example.com/jobs/7", "jobs.example.com/apply/7", None,
             "https://job-boards.greenhouse.io/acme/jobs/4400001", "https://boards.greenhouse.io/other/jobs/4400001",
             "https://job-boards.greenhouse.io/acme/jobs/4400002", "https://www.linkedin.com/jobs/view/4000000111",
             "https://www.linkedin.com/jobs/search/?currentJobId=4000000111",
             "https://acmerx.wd5.myworkdayjobs.com/careers/job/Remote/Claims_R-44001"]
    TITLES = ["Claims Analyst", "Claims Operations Analyst"]
    REQS = ["R-1", "R-2", None]
    EMPLOYERS = ["Acme Rx", "Maple Health", "A+B Health", "A B Health"]
    TEXTS = ["Body.", "Another body."]

    def cases(self):
        rng = random.Random(SEED + 3)
        for _ in range(3000):
            yield (_row(rng.choice(self.LINKS), rng.choice(self.TITLES), rng.choice(self.REQS), rng.choice(self.EMPLOYERS),
                        text=rng.choice(self.TEXTS)),
                   dict(url=rng.choice(self.LINKS), title=rng.choice(self.TITLES), requisition_id=rng.choice(self.REQS),
                        employer=rng.choice(self.EMPLOYERS), text=rng.choice(self.TEXTS)))

    def test_two_postings_are_one_job_only_by_one_exact_posting_link(self):
        """Whatever the title, requisition, employer and text say, and whatever a careers-page link or no link says."""
        for case, (row, new) in enumerate(self.cases()):
            mine = posting_identity(new["url"])
            want = mine is not None and mine == job_identity(row)
            self.assertEqual(same_manual_posting(row, location="Remote - US", **new), want, f"case {case}: {row} vs {new}")

    def test_one_posting_in_another_link_form_is_it(self):
        """The control: a board's posting under another board name, and a job site's id in another link form."""
        for stored, given in (("https://job-boards.greenhouse.io/acme/jobs/4400001", "https://boards.greenhouse.io/other/jobs/4400001"),
                              ("https://www.linkedin.com/jobs/view/4000000111",
                               "https://www.linkedin.com/jobs/search/?currentJobId=4000000111")):
            self.assertTrue(same_manual_posting(_row(stored, "A", None, "Acme Rx"), url=given))
            self.assertFalse(same_manual_posting(_row("https://careers.example.com/join", "A", None, "Acme Rx"),
                                                 url="https://careers.example.com/join"))   # a careers page names none


class StandingInvariantTests(unittest.TestCase):
    ROUTES = ("scan", "alert_or_add_url", "paste", "add_url", "saved_file")

    def place(self, route: str, location: str, n: int):
        """One posting through one route; returns (store, job id)."""
        store, ctx = setup()
        if route in ("scan", "alert_or_add_url"):
            legacy_board(store, name="Synthetic Health", token="synth")
            item = gh_item(n, "Claims Analyst", FULL_TIME, "synth", location=location)
            ingest(ctx, dict(store.one("SELECT * FROM employers")), [greenhouse_posting("synth", item)],
                   **({} if route == "scan" else {"source": "manual_url", "full_board": False}))
        elif route == "paste":
            intake(ctx, f"Claims Analyst\nCompany: Acme Rx\nLocation: {location}\n\n" + BODY, f"https://careers.acme.example/jobs/{n}")
        elif route == "add_url":
            with tempfile.TemporaryDirectory() as tmp:
                body = Path(tmp) / "posting.txt"
                body.write_text(BODY, encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_add_url(ctx, vault_paths(Path(tmp)), SimpleNamespace(
                        url=f"https://careers.acme.example/jobs/{n}", text_file=str(body), employer="Acme Rx", parent=None,
                        title="Claims Analyst", location=location))
        else:
            raw, employer = parse_saved_text(f"URL: https://careers.acme.example/jobs/{n}\nTitle: Claims Analyst\nEmployer: Acme Rx\n"
                                             f"Location: {location}\n\n" + BODY, "p.txt")
            ingest(ctx, {"employer_id": None, "name": employer, "parent": employer}, [raw], source="manual_url", full_board=False)
        process(ctx)
        return store, store.one("SELECT job_id FROM jobs")["job_id"]

    def test_no_route_is_picked_without_your_label(self):
        """The invariant Codex Passes 12 and 13 were asked about, over generated fields and every route: since the owner's
        D183 standing preparation is off, so no job is picked, prepared or routed without your Apply or Stretch label,
        whatever its route or field; your label routes it."""
        rng = random.Random(SEED + 4)
        remote_us = ["Remote - US", "US Remote", "Remote, United States", "Remote - USA", "Remote (US)"]
        for case in range(40):
            route = self.ROUTES[case % len(self.ROUTES)]
            kind = rng.choice(("us", "foreign", "qualifier"))
            parts = ["Remote", rng.choice(US_PARTS)] if kind == "us" else ["Remote", rng.choice(US_PARTS),
                                                                          rng.choice(FOREIGN if kind == "foreign" else QUALIFIERS)]
            rng.shuffle(parts)
            location = rng.choice(remote_us) if kind == "us" and case % 2 else field(rng, parts)
            store, job_id = self.place(route, location, 100 + case)
            label = f"case {case}: {route} {location!r}"
            self.assertEqual(packages.standing_candidates(store, ON), [], label)
            self.assertIsNone(packages.route_for(store, ON, job_id)[0], label)
            with self.assertRaises(packages.PackageRefused, msg=label):
                packages.prepare(store, ON, None, job_id, facts=[], library={}, vault_root=Path("."))
            if case < 5:   # the control: your label routes it
                give_label(store, ON, MID, job_id, "apply", None, f"k-{case}")
                self.assertEqual(packages.route_for(store, ON, job_id, MID)[0], "apply", label)

if __name__ == "__main__":
    unittest.main()
