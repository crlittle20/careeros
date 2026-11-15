"""Regressions for defects found by the user's first labels (2026-10-11). Synthetic text modeled on the live wording."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from careeros.core.clock import FixedClock
from careeros.core.policy import load_policy
from careeros.db.store import open_store
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import HttpClient
from careeros.extract.rules import extract
from careeros.normalize.posting import (negated_mentions, norm, remote_state_rules, residence_limits, seniority,
                                       text_state_lists)
from careeros.paths import CONFIG_DIR
from careeros.pipeline import Context, ingest, process
from careeros.qualify import gate as gate_mod
from careeros.qualify import night
from careeros.qualify.gate import evaluate, state_for
from careeros.qualify.profile import load_profile

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import Board, gh_job
from tests.test_qualify import job

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PROFILE = load_profile(FIXTURES / "synthetic_profile.yaml")
EXCLUSION = "Pay depends on experience. This role is not eligible for hire in: AK, CA, CO, HI, NY, or WA.  Interview Process: ..."
BASE = postings.GOOD_FIT.split("This role is remote")[0]   # the PRIMARY posting without its own state list
PAY = "\n$90,000 - $100,000 per year."   # above the synthetic floor: pay never decides these
STATE_FAILURES = {"state_excluded", "state_not_eligible"}


def stored_lists(role: dict) -> dict:
    """The posting's state lists as normalize stores them: the location field and the older list rule (unchanged)."""
    role["remote_states"], role["remote_excluded_states"] = remote_state_rules(role["location_raw"], role["description_text"],
                                                                              role["remote_type"])
    return role


def primary_gate(line: str, location: str = "Remote - US") -> tuple[str, list, list]:
    """PRIMARY: the synthetic remote BSA posting with `line`, through gate.evaluate: (state, failures, flags)."""
    role = stored_lists(job("Business Systems Analyst", BASE + "\n" + line + PAY, location=location))
    result = evaluate(role, extract(role["description_text"], POLICY).requirements, PROFILE, POLICY)
    return state_for(result, POLICY), result.hard_failures, result.flags


def night_gate(line: str, location: str = "Remote - US") -> tuple[str, list, list]:
    """NIGHT: the synthetic part-time overnight auditor posting with `line`, through night.assess: (state, failures, flags)."""
    from tests.test_v21_policy import NIGHT_ON   # imported here: those modules import this one
    from tests.test_v22_night import QUIET
    role = stored_lists(job("Claims Quality Auditor (Part-Time)", QUIET + "\n" + line, location=location, employment="part time"))
    out = night.assess(role, extract(role["description_text"], NIGHT_ON).requirements, PROFILE, NIGHT_ON, 60.0)
    return out["state"], out["failures"], out["flags"]


def residence_flags(flags: list) -> list:
    return [f for f in flags if f["code"] == "residence_limit_check"]


class StateRuleTests(unittest.TestCase):
    def test_exclusion_list_is_not_an_allowed_list(self):
        allowed, excluded = remote_state_rules("United States", EXCLUSION, "remote")
        self.assertEqual((allowed, excluded), ([], ["AK", "CA", "CO", "HI", "NY", "WA"]))

    def test_office_alternative_is_not_a_remote_limit(self):
        self.assertEqual(remote_state_rules("New York, NY or Remote", "", "remote"), ([], []))
        self.assertEqual(remote_state_rules("Remote - TX, FL", "", "remote"), (["FL", "TX"], []))
        self.assertEqual(remote_state_rules("Remote - US", "", "remote"), ([], []))

    def test_one_state_after_a_residence_cue_limits_the_role(self):
        """Oscar (2026-11-04): "open to candidates who reside in: Texas" names one state, and read as nationwide."""
        def rules(text):
            return tuple(residence_limits(text)[:2])
        self.assertEqual(rules("This is a remote position, open to candidates who reside in: Texas."), (["TX"], []))
        self.assertEqual(rules("This is a remote position, open to candidates who reside in: Dallas, Texas."), (["TX"], []))
        self.assertEqual(rules("This position covers our West territory and the person must live in California."),
                         (["CA"], []))
        self.assertEqual(rules("- Must reside in Minnesota\n- Bachelor's degree preferred"), (["MN"], []))
        self.assertEqual(rules("Must be a resident of Florida; Spanish preferred."), (["FL"], []))
        self.assertEqual(rules("Remote. Must reside in Texas. We prefer candidates who know Excel."), (["TX"], []))
        self.assertEqual(rules("This role is not open to candidates who reside in California."), ([], ["CA"]))
        self.assertEqual(rules("Fully remote and open to candidates residing in the U.S., excluding Alaska."), ([], ["AK"]))
        self.assertEqual(rules("Open to candidates who reside in: Arizona; Florida; Georgia; or Texas."),
                         (["AZ", "FL", "GA", "TX"], []))
        for text in ("Our company is based in Ohio. This is a remote role.", "We are located in Austin, Texas.",
                     "Associates must live within commuting distance of our New York office.",
                     "Candidates residing in the U.S. are welcome. Our office is in Texas.",
                     "The ideal candidate will live in Houston or Dallas, Texas.",   # a preference, not a limit
                     "You currently live in New Jersey, previously lived there, or can demonstrate knowledge of it."):
            self.assertEqual(rules(text), ([], []), text)
        self.assertEqual(remote_state_rules("Austin, TX", "Must reside in Texas.", "onsite"), ([], []))   # not remote

    def test_a_residence_cue_reads_its_whole_list_and_only_its_own_negation(self):
        """The review of the first fix (2026-11-04): lists, the negation's reach, the U.S., St. Louis, relocation."""
        def rules(text):
            return tuple(residence_limits(text)[:2])
        self.assertEqual(rules("Open to candidates who live in Texas, Florida or Georgia."), (["FL", "GA", "TX"], []))
        self.assertEqual(rules("Applicants must be residents of Arizona, Florida or Georgia."), (["AZ", "FL", "GA"], []))
        self.assertEqual(rules("Fully remote and open to candidates residing in the U.S., excluding Alaska; Hawaii; or "
                               "California."), ([], ["AK", "CA", "HI"]))
        self.assertEqual(rules("Applicants must live in Florida and travel to Georgia monthly."), (["FL"], []))
        self.assertEqual(rules("Must reside in Florida, the role is not eligible for remote work outside the state."),
                         (["FL"], []))   # a negation after the list isn't about it
        self.assertEqual(rules("Not eligible: Alaska.\nMust reside in Florida."), (["FL"], []))
        self.assertEqual(rules("Must reside in Texas or be willing to relocate."), (["TX"], []))   # still a limit
        self.assertEqual(rules("Team members must reside in St. Louis, Missouri and nearby."), (["MO"], []))
        for text in ("Candidates who live in Texas are preferred.",
                     "Candidates residing in the U.S. Applicants in Texas get a bonus.",
                     "Our nurses live in the many different rural and urban communities they serve across the country, "
                     "including Texas."):
            self.assertEqual(rules(text), ([], []), text)

    # The Fable review (2026-11-04): a residence sentence limits the role only when it requires it of the candidate. The
    # kind repeated after the first review, so the whole kind is checked here, one table: requirements, exclusions, and
    # sentences that only say where someone lives (notices, members, the team), unreadable negations and time zones.
    RESIDENCE_KIND = (
        ("Candidates must currently reside in Texas.", ["TX"], []),
        ("Only candidates who reside in Texas will be considered.", ["TX"], []),
        ("We can only hire candidates who live in Texas or Oklahoma.", ["OK", "TX"], []),
        ("This role is only for candidates living in Texas or Oklahoma.", ["OK", "TX"], []),
        ("Residents of Texas only.", ["TX"], []),
        ("Candidates residing in Texas, Florida or Georgia are eligible to apply.", ["FL", "GA", "TX"], []),
        ("Candidates must reside in TX.", ["TX"], []),
        ("Must reside in TX, CT, or NY.", ["CT", "NY", "TX"], []),
        ("Candidates must not reside in California.", [], ["CA"]),
        ("Candidates may not reside in California or New York.", [], ["CA", "NY"]),
        ("Applicants cannot live in California.", [], ["CA"]),
        ("We are unable to consider applicants who reside in California, Colorado or New York.", [], ["CA", "CO", "NY"]),
        ("Unfortunately we cannot consider candidates residing in California.", [], ["CA"]),
        ("In accordance with Colorado law, the salary range for candidates residing in Colorado is $70,000 - $85,000.", [], []),
        ("If you are a resident of California, please review our California Privacy Notice for applicants.", [], []),
        ("Notice to residents of California: we collect personal information under the CCPA.", [], []),
        ("Employees who live in Washington are eligible for paid sick leave under state law.", [], []),
        ("If you live in New York City, the base salary range for this role is $100,000 - $120,000.", [], []),
        ("For residents of Colorado, the pay range for this role is $70,000 - $90,000 per year.", [], []),
        ("Applicants residing in California, Colorado, New York or Washington: the expected salary range is $86,000 - "
         "$96,000.", [], []),
        ("Candidates living in the state of Washington may request the pay range.", [], []),
        ("Applicants must review the pay range for candidates residing in Colorado.", [], []),
        ("You will support Medicaid members who reside in Kentucky and Ohio.", [], []),
        ("You must support members who reside in Kentucky and Ohio.", [], []),
        ("Our health plan serves residents of Texas.", [], []),
        ("We are a remote-first team with employees living in Texas, Colorado and New York.", [], []),
        ("Our distributed team lives in California, Colorado and New York, and we meet twice a year.", [], []),
        ("Our team members reside in St. Louis, Missouri and nearby.", [], []),
        ("Our founder lives in California.", [], []),
        ("Learn how our people live in balance: wellness days, and offices in Austin, Texas.", [], []),
        ("Candidates who do not live in Texas must relocate.", [], []),   # a negation it can't read: neither
        ("You do not need to live in Texas.", [], []),
        ("Candidates must reside in any U.S. state, but not California.", [], []),
        ("We do not offer relocation, so you must live in Texas.", ["TX"], []),   # a negation further back isn't it
        ("Ideally, candidates should reside in Texas.", [], []),   # a requirement softened by a preference: none
        ("Candidates should live in Texas or Florida preferably.", [], []),
        ("Applicants must live in a city with a major airport, for regular travel to client sites in Texas.", [], []),
        ("Candidates must reside in the U.S. Applicants in Texas get a bonus.", [], []),   # "U.S." ended the sentence
        # D123 (2026-11-09): a time zone written as a code reads as that code's state (the time-zone reading is gone): at
        # the gate a list naming another state, so the posting flags, needless but never a wrong pass
        ("Candidates must reside in the Central (CT) or Eastern (ET) time zone.", ["CT"], []),
        ("Must live in the CT or ET time zone and work 8-5 local time.", ["CT"], []),
        # the fix-check (2026-11-05): work hours, and a negation in the clause before the requirement
        ("Must reside in the United States and work 8-5 CT.", ["CT"], []),
        ("Must reside in the U.S. and work CT business hours.", ["CT"], []),
        ("Candidates must reside in the US and work core hours of 9-3 MT.", ["MT"], []),
        ("Relocation assistance is not available; candidates must reside in Texas.", ["TX"], []),
        ("Sponsorship not available; must reside in Texas.", ["TX"], []),
        ("The following privacy notice is for applicants residing in California.", [], []),
        ("Employees residing in California may apply for paid family leave.", [], []),
        ("This role is not open to candidates who reside in California.", [], ["CA"]),
        # Codex Pass 1, F7 (2026-11-05): a preference, a negation or a time-zone word in another clause is not the
        # requirement's
        ("Healthcare experience is preferred; applicants must reside in Texas.", ["TX"], []),
        ("We cannot hire interns, but applicants must reside in Texas.", ["TX"], []),
        ("Applicants must reside in CT; hours are flexible.", ["CT"], []),
        ("Applicants must reside in Connecticut; hours are flexible.", ["CT"], []),
        ("Applicants must reside in Texas, preferably Austin.", ["TX"], []),
        # the Fable fix-check of the Codex corrections (2026-11-05): every guard in the requirement's own clause
        ("Applicants must reside in Texas; previously employed candidates are welcome.", ["TX"], []),
        ("Applicants must reside in Texas - healthcare experience preferred.", ["TX"], []),
        ("Applicants must reside in Texas but healthcare experience is preferred.", ["TX"], []),
        ("Must reside in CT - hours are flexible.", ["CT"], []),
        ("While healthcare experience is preferred, applicants must reside in Texas.", ["TX"], []),
        ("Although we prefer local candidates, applicants must reside in Texas.", ["TX"], []),
        ("Healthcare experience is preferred, and applicants must reside in Texas.", ["TX"], []),
        ("We cannot hire interns and applicants must reside in Texas.", ["TX"], []),
        ("Applicants must reside in Texas, or previously have lived there.", [], []),   # an alternative in its clause
    )

    def test_a_residence_sentence_limits_only_when_it_requires_it(self):
        for text, allowed, excluded in self.RESIDENCE_KIND:
            self.assertEqual(tuple(residence_limits(text)[:2]), (allowed, excluded), text)

    # D123: the rows of RESIDENCE_KIND no reader takes a state list from (no state close after a residence cue in its
    # sentence, and no older list cue with two states): nothing to judge, so silent
    # (D149, R-R2: "Applicants must live in a city with a major airport, for regular travel to client sites in Texas."
    # left this set: a residence cue with a state is a state statement not naming FL, so CONDITIONAL)
    # (D151, R3: "Candidates must reside in the U.S. Applicants in Texas get a bonus." left too: a sentence names a state
    # other than FL, so CONDITIONAL)
    NO_LIST = ()

    def test_every_residence_row_is_conditional_or_eligible_at_the_gate(self):
        """D123 (2026-11-09; D119), the whole kind through the gate, not through residence_limits: each row through
        PRIMARY gate.evaluate and NIGHT night.assess for the FL profile is never INELIGIBLE (a list read from the
        description never rejects). A row naming FL with no exclusion or negation word near it is ELIGIBLE with no
        residence flag; every other row (a requirement, an exclusion, a notice, the members a plan serves, where the team
        lives, a preference, a time zone) is CONDITIONAL with exactly one residence_limit_check quoting its list's
        sentence: no reading of "no limit" is left. Rows no reader takes a list from (NO_LIST) are silent. Until D123 the
        rows read as no limit were silent, and three rounds of checks each found new wordings of that kind passing
        silently while they required another state of the candidate."""
        for text, _allowed, _excluded in self.RESIDENCE_KIND:
            silent = "Florida" in text or text in self.NO_LIST
            for route in (primary_gate, night_gate):
                with self.subTest(text=text, route=route.__name__):
                    state, failures, flags = route(text)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    found = residence_flags(flags)
                    self.assertEqual((state, len(found)), ("ELIGIBLE", 0) if silent else ("CONDITIONAL", 1), flags)
                    if found:
                        self.assertIn(found[0]["source_text"], " ".join(text.split()))

    # D123 (2026-11-09): description lines, in pairs where a pair helps, through PRIMARY and NIGHT for the FL profile:
    # (line, what the one residence_limit_check says the list names, or None: ELIGIBLE, silent). The wordings the D119
    # rounds and their checks found (wf_ab7f136d; corrections\check-residence-1, -2, -r3-1, -r3-2): requirements in any
    # wording, the kinds once read as no limit (a pay notice, the members a plan serves, where the team lives) with and
    # without a requirement after them, negations, time zones
    NEGATED = " beside an exclusion or negation"
    TEXT_LISTS = (
        # your state named, with no exclusion or negation word anywhere in the sentence: silent. A negation in another
        # clause of the sentence counts too (the check of 01cd257: excusing it let "Applicants from the following states
        # are not eligible; this includes those living in <yours> ..." pass silently), so "We cannot hire interns, but
        # applicants must reside in <yours>" flags: needless, the accepted cost; one in another sentence doesn't count
        ("Applicants must reside in Florida or Texas.", None),
        ("Applicants must reside in Texas or Georgia.", "names GA, TX"),
        ("We cannot hire interns, but applicants must reside in Florida or Texas.", "names FL, TX" + NEGATED),
        ("We cannot hire interns, but applicants must reside in Georgia or Texas.", "names GA, TX" + NEGATED),
        ("We cannot hire interns; applicants must reside in Florida or Texas.", "names FL, TX" + NEGATED),
        ("Sponsorship is not available; candidates must be located in Florida or Texas.", "names FL, TX" + NEGATED),
        ("Relocation assistance is not available. Candidates must be located in Florida or Georgia.", None),
        ("Applicants from the following states are not eligible; this includes those living in Florida, Texas and Ohio.",
         "names FL, OH, TX" + NEGATED),
        ("Unfortunately, we cannot hire in the following locations - candidates living in Florida, California or New York.",
         "names CA, FL, NY" + NEGATED),
        ("We are not able to hire everywhere; candidates located in Florida, Texas or Ohio, for example.",
         "names FL, OH, TX" + NEGATED),
        ("Please note: we cannot hire in every state - specifically, candidates located in Florida, Alaska or Hawaii.",
         "names AK, FL, HI" + NEGATED),
        ("This position is not open to residents of certain states; residents of Florida, Alaska and Hawaii are among them.",
         "names AK, FL, HI" + NEGATED),
        ("We are unable to employ people in some states; this includes residents of Florida, Alaska, and Hawaii.",
         "names AK, FL, HI" + NEGATED),
        ("Excluded - candidates who reside in Florida or Texas.", "names FL, TX" + NEGATED),
        ("We cannot hire residents of these states, and those who live in Florida, Texas or Ohio are among them.",
         "names FL, OH, TX" + NEGATED),
        # (D149, R-R2: "This position is open to residents of certain states; residents of Florida, Alaska and Hawaii are
        # among them." left this set: two state statements, so CONDITIONAL; StateRuleTests.D149_SECOND_STATEMENT)
        # the closed list's other exclusion words
        ("We have paused hiring in FL, TX and CA.", "names CA, FL, TX" + NEGATED),
        ("We are restricted from hiring in Florida and Texas.", "names FL, TX" + NEGATED),
        ("We are prohibited from hiring in Florida, Texas or Ohio.", "names FL, OH, TX" + NEGATED),
        ("Hiring in the following states is on hold: FL, TX.", "names FL, TX" + NEGATED),
        ("Candidates located in Florida or Texas are barred from this role due to licensing.", "names FL, TX" + NEGATED),
        ("Applicants who reside in Florida or Texas will be disqualified.", "names FL, TX" + NEGATED),
        ("We avoid hiring in Florida and Texas.", "names FL, TX" + NEGATED),
        ("We are hiring in Florida and Texas.", None),
        ("Hiring in Florida and Texas is closed.", "names FL, TX" + NEGATED),
        ("We are no longer hiring in Florida or Texas.", "names FL, TX" + NEGATED),
        ("Relocation assistance is not available. Candidates must be located in Texas or Georgia.", "names GA, TX"),
        ("For candidates living in Florida, Georgia or Texas, the base salary range is $90,000 - $110,000.", None),
        ("For candidates living in Georgia or Texas, the base salary range is $90,000 - $110,000.", "names GA, TX"),
        ("We hire in the following states: Texas, Georgia, Florida.", None),
        ("We hire in the following states: Texas, Georgia.", "names GA, TX"),
        # the list names only the states joined from its first one, in the cue's sentence or line (or the lines of
        # states under a cue line ending in ":"): a later mention of your state is not the list's (the check of 01cd257)
        ("Candidates must be located in Texas or Georgia. Our headquarters is in Florida.", "names GA, TX"),
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        ("Candidates must be located in Texas or Georgia and have Florida Medicaid experience.", "names GA, TX"),
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        ("Requirements:\n- Must be located in Texas or Georgia\n- Experience with Florida Medicaid", "names GA, TX"),
        ("Eligible states:\nTexas\nGeorgia\nOffice: Tampa, Florida", "names GA, TX"),
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        ("We are hiring in Texas, Georgia and Ohio to support our members in Florida.", "names GA, OH, TX"),
        ("This role is open to candidates in the following states: Texas, Georgia, Ohio\nPreferred: Florida licensure",
         "names GA, OH, TX"),
        ("Location: Must be based in Georgia or Texas\nHeadquarters: Tampa, FL", "names GA, TX"),
        ("Work from Texas or Georgia; travel to Florida quarterly.", "names GA, TX"),
        # a cue line ending in ":" reads the lines of states under it, blank lines skipped (Greenhouse's bullets)
        ("This role is open to candidates residing in the following states:\n\n- Texas\n\n- Georgia", "names GA, TX"),
        ("This role is open to candidates residing in the following states:\n\n- Florida\n\n- Georgia", None),
        ("Candidates must live in:\n\n- Texas\n- Georgia", "names GA, TX"),
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        ("Candidates must reside in:\n- Texas\n- Georgia\n- Experience with Florida Medicaid", "names GA, TX"),
        ("We are unable to hire in the following states:\n\n- Florida\n- California", "names CA, FL" + NEGATED),
        # a requirement naming other states, in wording no reader knows
        ("This position is only available to candidates living in Texas or Georgia.", "names GA, TX"),
        ("This position is only available to candidates living in Florida or Georgia.", None),
        ("We are seeking candidates who reside in Texas or Georgia.", "names GA, TX"),
        ("We only hire individuals in Texas and Georgia.", "names GA, TX"),
        ("We only hire individuals in Florida and Georgia.", None),
        ("We hire Indiana and Ohio residents only.", "names IN, OH"),   # "hire In" of "Indiana": read from the word
        ("We hire Indiana, Illinois and Ohio residents only.", "names IL, IN, OH"),
        # the kinds once read as no limit, with a requirement after them (D119 rounds 2-3) or alone: they flag
        ("Our employees, living in Texas, Colorado and New York, must reside in one of those states.", "names CO, NY, TX"),
        ("Our employees, living in Florida, Colorado and New York, must reside in one of those states.", None),
        ("Our team members reside in Texas and Georgia; new team members must also reside in one of these states.",
         "names GA, TX"),
        ("For candidates living in Texas or Georgia, the pay range is $90,000 - $100,000. This role is only open to residents "
         "of these states.", "names GA, TX"),
        ("For candidates living in Florida or Georgia, the pay range is $90,000 - $100,000. This role is only open to "
         "residents of these states.", None),
        ("You will support members who reside in Kentucky and Ohio. Candidates must reside in one of these states.",
         "names KY, OH"),
        ("You will support members who reside in Kentucky and Florida. Candidates must reside in one of these states.", None),
        ("Our employees reside in Texas or Georgia, and the successful candidate will too.", "names GA, TX"),
        ("You will support members who reside in Kentucky and Ohio. Residence in either state is necessary.", "names KY, OH"),
        ("You will support Medicaid members who reside in Kentucky and Ohio.", "names KY, OH"),
        ("You will support Medicaid members who reside in Kentucky and Florida.", None),
        ("We are a remote-first team with employees living in Texas, Colorado and New York.", "names CO, NY, TX"),
        ("For candidates living in California, Colorado or Washington, the range is $90,000 - $110,000.", "names CA, CO, WA"),
        ("Candidates living in Texas or Oklahoma are preferred.", "names OK, TX"),
        # your state beside an exclusion or negation word; an exclusion list of other states (other states only)
        ("Candidates who reside in Florida or New York are not eligible.", "names FL, NY" + NEGATED),
        ("Applicants living in Florida or Georgia will not be considered.", "names FL, GA" + NEGATED),
        ("Applicants living in Florida or Georgia won't be considered.", "names FL, GA" + NEGATED),
        ("Residents of Florida and New York are not eligible for this role.", "names FL, NY" + NEGATED),
        ("This role is not eligible for hire in: AK, CA, FL, or WA.", "names AK, CA, FL, WA" + NEGATED),
        ("This role is not eligible for hire in: AK, CA, or WA.", "names AK, CA, WA" + NEGATED),
        ("We do not hire in the following states; Florida, Texas.", "names FL, TX" + NEGATED),   # no cue in its clause
        ("Applicants must reside in Florida or Texas; we cannot sponsor visas.", "names FL, TX" + NEGATED),   # the cost
        ("Applicants must reside in Florida or Texas. We cannot sponsor visas.", None),   # another sentence
        # a time zone written as a code
        ("Must work from the CT or MT time zone.", "names CT, MT"),
        ("Candidates must be located in the Central (CT) or Mountain (MT) time zone.", "names CT"),   # ") or Mountain (" ends it
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        # several lists: one flag, the first list that isn't silent
        ("Applicants must reside in Florida or Texas. Our employees live in Texas and Georgia.", "names GA, TX"),
        ("Applicants must reside in Ohio or Texas. Our employees live in Kentucky and Georgia.", "names OH, TX"),
    )

    def test_a_state_list_from_the_description_never_rejects(self):
        """D123 (2026-11-09; D119, D114): a state list read from the description never rejects, in PRIMARY and NIGHT. It
        is silent only when it names your state and no exclusion or negation word is near it; any other list makes the
        posting CONDITIONAL with exactly one residence_limit_check, quoting the first such list's sentence (the quote holds
        the cue and its states). No reading says a list is no limit on you any more: the members a plan serves, where the
        team lives and a pay notice for other states flag too, the accepted cost of a fail-safe rule. Guards the D119
        rounds: each check found a requirement on the candidate (after a no-limit sentence, or in wording the readers
        didn't know) passing silently, ELIGIBLE, where the posting named only other states."""
        for line, where in self.TEXT_LISTS:
            for route in (primary_gate, night_gate):
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    found = residence_flags(flags)
                    if where is None:
                        self.assertEqual((state, found), ("ELIGIBLE", []))
                        continue
                    self.assertEqual((state, len(found)), ("CONDITIONAL", 1), flags)
                    self.assertEqual(found[0]["detail"], f"a list of states in the posting may limit this role ({where}); "
                                                         "you are in FL: confirm it before applying")
                    self.assertIn(found[0]["source_text"], " ".join(line.split()))

    def test_a_long_sentence_is_quoted_around_its_cue(self):
        """D123 (S3): the one flag's quote holds the cue and its states; a long sentence is trimmed around the cue, not
        cut from its start (until D123 the quote was the sentence's first 300 characters, which could drop the states)."""
        lead = ("As a member of our growing analytics organization you will partner with claims, pharmacy, provider network, "
                "finance, compliance and member services leaders to translate business needs into clear requirements, ")
        for route in (primary_gate, night_gate):
            state, _failures, flags = route(lead + "and you must be located in Texas or Georgia.")
            quote = residence_flags(flags)[0]["source_text"]
            self.assertEqual(state, "CONDITIONAL")
            self.assertTrue(quote.endswith("you must be located in Texas or Georgia"), quote)
            self.assertFalse(quote.startswith("As a member"), quote)
            self.assertLessEqual(len(quote), 150)
            state, _failures, flags = route(lead + "and you must be located in Florida or Georgia.")
            self.assertEqual((state, residence_flags(flags)), ("ELIGIBLE", []))

    def test_only_the_location_field_rejects_on_state(self):
        """D123 (2026-11-09; D119) controls, under D151: the posting's location field ("Remote - Texas only") leaving your
        state out is CONDITIONAL with one residence_limit_check quoting the location (until D151 it rejected,
        state_not_eligible); one naming your state never flags on its own, though a description list leaving you out still
        flags. A requirement in the text naming another state is CONDITIONAL with its sentence quoted; naming yours,
        silent."""
        for route in (primary_gate, night_gate):
            for line in ("", "Must work from the CT or MT time zone."):
                state, failures, flags = route(line, "Remote - Texas only")
                self.assertEqual((state, failures), ("CONDITIONAL", []), route.__name__)
                self.assertEqual([(f["detail"], f["source_text"]) for f in residence_flags(flags)],
                                 [("the location names only TX; you are in FL: confirm it before applying",
                                   "Remote - Texas only")])
            state, failures, flags = route("", "Remote - FL, TX")
            self.assertEqual((state, failures, residence_flags(flags)), ("ELIGIBLE", [], []))
            state, failures, flags = route("Applicants must reside in Texas or Georgia.", "Remote - FL, TX")
            self.assertEqual((state, [f["source_text"] for f in residence_flags(flags)]),
                             ("CONDITIONAL", ["Applicants must reside in Texas or Georgia"]))
            state, failures, flags = route("You must reside in one of the following states: Texas, Georgia.")
            self.assertEqual((state, [f["source_text"] for f in residence_flags(flags)]),
                             ("CONDITIONAL", ["You must reside in one of the following states: Texas, Georgia"]))
            state, failures, flags = route("You must reside in one of the following states: Florida, Georgia.")
            self.assertEqual((state, residence_flags(flags)), ("ELIGIBLE", []))

    # D123: a location field naming only another state beside a description list naming FL as allowed (any list: a
    # requirement, a pay notice, the members a plan serves, where the team lives, an office): the two disagree (D114), so
    # CONDITIONAL with one flag quoting the location. (line, location)
    LOCATION_MASKED = (
        ("We serve members living in Florida and Georgia.", "Remote - Texas only"),
        ("You will support members who reside in Florida and Ohio.", "Remote - TX"),
        ("Our headquarters are located in Jacksonville, Florida, with offices in Atlanta, Georgia.", "Remote - Texas"),
        ("Applicants must reside in Florida or Georgia.", "Remote - Texas only"),
        ("Applicants must reside in Florida or Georgia.", "Remote - TX"),
        ("This position is open to candidates who reside in: Florida.", "Remote - Texas"),
        ("This role is remote; we hire in the following states: Texas, Florida, Arizona, Georgia.",
         "Remote - Eden Prairie, MN, US"),   # a saved page: TELECOMMUTE beside the office's address
        ("For candidates living in Florida, Georgia or Texas, the pay range is $90,000 - $110,000.", "Remote - Texas only"),
        ("For candidates living in Florida, Georgia or Texas, the base salary range is $90,000 - $110,000.",
         "Remote - Eden Prairie, MN, US"),
        ("Our employees, living in Florida, Texas and Georgia, work remotely.", "Remote - Eden Prairie, MN, US"),
        ("Our team members reside in Florida and Georgia.", "Remote - Texas"),
    )
    # ... and its pairs: no description list naming FL as allowed (none at all, other states only, FL beside a negation):
    # until D151 the location field rejected here; now CONDITIONAL, quoting the location (line, location)
    LOCATION_REJECTS = (
        ("", "Remote - Texas only"),
        ("", "Remote - TX"),
        ("Must work from the CT or MT time zone.", "Remote - Texas only"),
        ("Applicants must reside in Texas or Georgia.", "Remote - Texas"),
        ("For candidates living in Georgia or Ohio, the base salary range is $90,000 - $110,000.", "Remote - Texas"),
        # (D149, R-R1: "Candidates who reside in Florida or New York are not eligible." beside "Remote - TX" left this
        # set: the description names FL, so CONDITIONAL; StateRuleTests.D149_LOCATION_NAMES_HOME)
        ("We serve members living in Georgia and Ohio.", "Remote - Texas only"),
    )

    def test_the_location_field_holds_unless_the_description_names_your_state(self):
        """D123 (2026-11-09; D119, D114), in PRIMARY and NIGHT: the location field naming only other states with no
        description list naming your state as allowed (LOCATION_REJECTS) rejected until D151; now it is CONDITIONAL with
        one residence_limit_check quoting it. When any description list names
        it (members and team lists count like any other: no reading of "no limit" is left), the two disagree: CONDITIONAL
        with one residence_limit_check quoting the location, never INELIGIBLE (LOCATION_MASKED; until D123 the members
        and team lists rejected here, silent). A location naming your state never rejects. The gate reads the location
        and the text, not the stored union: stored lists holding FL that nothing in the text names (stored by older code)
        no longer lift the location's limit (until D123 they flagged)."""
        named = {"Remote - Eden Prairie, MN, US": "MN"}
        for route in (primary_gate, night_gate):
            for line, location in self.LOCATION_MASKED:
                with self.subTest(line=line, location=location, route=route.__name__):
                    state, failures, flags = route(line, location)
                    self.assertEqual((state, failures), ("CONDITIONAL", []))
                    self.assertEqual([(f["detail"], f["source_text"]) for f in residence_flags(flags)],
                                     [(f"the location names only {named.get(location, 'TX')}, but a list of states in the "
                                       "posting names FL; you are in FL: confirm it before applying", location)])
            for line, location in self.LOCATION_REJECTS:
                with self.subTest(line=line, location=location, route=route.__name__):
                    state, failures, flags = route(line, location)
                    self.assertEqual((state, failures, [f["source_text"] for f in residence_flags(flags)]),
                                     ("CONDITIONAL", [], [location]))
            for location in ("Remote - FL, TX", "Remote - US"):
                state, failures, flags = route(self.LOCATION_MASKED[0][0], location)
                self.assertEqual((state, failures, residence_flags(flags)), ("ELIGIBLE", [], []), location)
        role = job("Business Systems Analyst", BASE + PAY, location="Remote - TX")
        role["remote_states"], role["remote_excluded_states"] = ["FL", "TX"], []
        result = evaluate(role, extract(role["description_text"], POLICY).requirements, PROFILE, POLICY)
        self.assertEqual((state_for(result, POLICY), result.hard_failures), ("CONDITIONAL", []))   # D151: never rejects

    def test_text_state_lists_reads_every_list_with_its_quote(self):
        """D123 (2026-11-09, and the check of 01cd257): text_state_lists reads every list in the description, from the
        older list rule's cues and the residence reader's, as (the states the flag names, the states the list names, the
        quote, negated), in text order. The list names the states joined from its first one in the cue's sentence or
        line, or the lines of states under a cue line ending in ":" (blank lines skipped). Negated: an exclusion or
        negation word anywhere in that sentence, its lead-in line and lines included. A cue running into a word ("hire In"
        of "hire Indiana") is read from that word. Guards a negation in another clause excused ("We cannot hire interns,
        but ..." is negated now), a later mention of a state counted as the list's ("... and have Florida Medicaid
        experience"), bullets after a blank line not read, and a state lost to the cue's word."""
        def lists(text):
            return [tuple(found) for found in text_state_lists(text)]
        tz = "Must work from the CT or MT time zone"
        self.assertEqual(lists(f"Remote. {tz}. Apply now."), [(["CT", "MT"], ["CT", "MT"], tz, False)])
        both = "We cannot hire interns, but applicants must reside in Florida or Texas"
        self.assertEqual(lists(both + "."), [(["FL", "TX"], ["FL", "TX"], both, True)] * 3)   # "hire in", "reside in" twice
        self.assertEqual(lists("We cannot hire Indiana or Ohio residents."),
                         [(["IN", "OH"], ["IN", "OH"], "We cannot hire Indiana or Ohio residents", True)])
        line = "We do not hire in the following states; Florida, Texas"
        self.assertEqual(lists(line + "."), [(["FL", "TX"], ["FL", "TX"], line, True)] * 2)
        line = "Residents of Florida and New York are not eligible for this role"
        self.assertEqual(lists(line + "."), [(["FL", "NY"], ["FL", "NY"], line, True)])
        line = "Candidates living in Texas or Oklahoma are preferred"
        self.assertEqual(lists(line + "."), [(["OK", "TX"], ["OK", "TX"], line, False)] * 2)
        self.assertEqual(lists("Benefits.\nMust reside in one of the following states:\n- Texas\n- Florida\nApply."),
                         [(["FL", "TX"], ["FL", "TX"], "Must reside in one of the following states: - Texas - Florida", False)] * 3)
        self.assertEqual(lists("We cannot hire in the following states:\n\n- Texas\n\n- Florida\n\nApply."),
                         [(["FL", "TX"], ["FL", "TX"], "We cannot hire in the following states: - Texas - Florida", True)] * 2)
        self.assertEqual(lists("Candidates must be located in Texas or Georgia. Our headquarters is in Florida."),
                         [(["GA", "TX"], ["GA", "TX"], "Candidates must be located in Texas or Georgia", False)])
        self.assertEqual(lists("Candidates must be located in Texas or Georgia and have Florida Medicaid experience."),
                         [(["GA", "TX"], ["GA", "TX"], "Candidates must be located in Texas or Georgia and have Florida Medicaid "
                                                       "experience", False)])
        self.assertEqual(lists("Must be located in one of our hubs. We have hubs in Texas and Georgia."),   # none in its own
                         [(["GA", "TX"], [], "Must be located in one of our hubs. We have hubs in Texas and Georgia", False)])
        self.assertEqual(lists("Must reside in Florida or Texas. No relocation."),
                         [(["FL", "TX"], ["FL", "TX"], "Must reside in Florida or Texas", False)] * 2)
        self.assertEqual(lists("Choose from our eligible states. We hire nationwide."), [])   # none named
        self.assertEqual(lists(""), [])

    def test_a_location_field_with_a_negation_flags(self):
        """D123 (the check of 01cd257): a remote location field holding an exclusion or negation word never rejects and
        never passes silently: it is CONDITIONAL with one residence_limit_check quoting it, in PRIMARY and NIGHT. Until
        then "Remote - US, excluding Florida and California" was read as naming FL (ELIGIBLE, silent), and "Remote - US
        (excluding CA)" as limited to CA (INELIGIBLE). The pairs without the word still pass or reject on its states."""
        for route in (primary_gate, night_gate):
            for location in ("Remote - US, excluding Florida and California", "Remote - US (not FL)",
                             "Remote (All states except Florida)", "Remote - FL, TX (FL not eligible)", "Remote - US (excluding CA)",
                             "Remote - US, excluding Texas and California", "Remote - All US states but Florida",
                             "Remote (Non-FL)"):
                with self.subTest(location=location, route=route.__name__):
                    state, failures, flags = route("Applicants must reside in Florida or Texas.", location)
                    self.assertEqual((state, failures), ("CONDITIONAL", []))
                    self.assertEqual([(f["detail"], f["source_text"]) for f in residence_flags(flags)],
                                     [(f"the location field holds an exclusion or negation ({location}); you are in FL: "
                                       "confirm it before applying", location)])
            for location, expected in (("Remote - US, Florida and California", "ELIGIBLE"), ("Remote - FL, TX", "ELIGIBLE"),
                                       ("Remote - All US states", "ELIGIBLE"), ("Remote (FL)", "ELIGIBLE"),
                                       ("Remote - CA", "CONDITIONAL")):   # D151: a location's state never rejects
                state, failures, flags = route("", location)
                self.assertEqual((state, len(residence_flags(flags))), (expected, int(expected != "ELIGIBLE")), location)

    # D123, the check of f1a17e1: (line, the flag's "names ..." text, or None: silent). A negation about your state in
    # another sentence or line, in the lead-in line above a bulleted list, and the words that check added
    ELSEWHERE = "; the posting names FL beside one elsewhere"
    NEGATED_ELSEWHERE = (
        ("This role is open to candidates who reside in Florida, Georgia, or Texas. Florida residents are not eligible at "
         "this time.", "names FL, GA, TX" + ELSEWHERE),
        # (D149, R-R2: the pairs "... reside in Florida, Georgia, or Texas. Texas residents are not eligible at this time."
        # and "We hire in FL, GA and TX.\nNote: TX is excluded for this role." left this set: a second state statement
        # narrows the first, so CONDITIONAL; StateRuleTests.D149_SECOND_STATEMENT)
        ("We hire in FL, GA and TX.\nNote: FL is excluded for this role.", "names FL, GA, TX" + ELSEWHERE),
        ("Applicants must reside in Florida or Texas.", None),
        # (D151, R3: moved to StateRuleTests.D151_SECOND_MENTION: another clause names a state, so CONDITIONAL)
        ("Applicants must reside in Florida or Texas. We do not offer relocation.", None),
        ("Not eligible for this role:\nCandidates residing in the following states:\n- Florida\n- Texas", "names FL, TX" + NEGATED),
        ("Open for this role:\nCandidates residing in the following states:\n- Florida\n- Texas", None),
        ("Not eligible for this role.\n\nCandidates residing in the following states:\n- Florida\n- Texas", None),   # a blank line
        ("Hiring in Florida and Texas is suspended.", "names FL, TX" + NEGATED),
        ("Applications from candidates located in Florida or Texas will be declined.", "names FL, TX" + NEGATED),
        ("Applications from candidates located in Florida or Texas will be rejected.", "names FL, TX" + NEGATED),
        ("We refuse applications from candidates located in Florida or Texas.", "names FL, TX" + NEGATED),
        ("We hire in all states but Florida and Texas.", "names FL, TX" + NEGATED),
        ("We hire in Florida and Texas, but not in Ohio.", "names FL, TX" + NEGATED),
        ("Non-FL candidates located in Texas or Georgia are welcome.", "names GA, TX" + NEGATED),
        ("Candidates located in Florida or Texas are welcome.", None),
    )

    def test_a_negation_about_your_state_anywhere_flags(self):
        """D123, the check of f1a17e1 (a High: these passed silently): a list naming your state stays silent only when no
        sentence or line of the description naming your state holds an exclusion or negation word, and none is in the
        lead-in lines above a bulleted list, up to the blank line before them. The closed list adds suspended, declined,
        rejected, refused, "all ... but" and "non-<state>". Otherwise CONDITIONAL with one residence_limit_check quoting
        the list, in PRIMARY and NIGHT; the pairs (a negation about another state, a mention of your state with no
        negation, a lead-in above a blank line) stay silent."""
        for line, where in self.NEGATED_ELSEWHERE:
            for route in (primary_gate, night_gate):
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    found = residence_flags(flags)
                    if where is None:
                        self.assertEqual((state, found), ("ELIGIBLE", []))
                        continue
                    self.assertEqual((state, len(found)), ("CONDITIONAL", 1), flags)
                    self.assertEqual(found[0]["detail"], f"a list of states in the posting may limit this role ({where}); "
                                                         "you are in FL: confirm it before applying")
                    self.assertIn(found[0]["source_text"], " ".join(line.split()))
        self.assertEqual(negated_mentions("We hire in FL, GA and TX.\nNote: FL is excluded for this role. Ohio is open."), {"FL"})
        self.assertEqual(negated_mentions("Applicants must reside in Florida or Texas. Our Florida office opened."), set())

    def test_a_residence_sentence_never_changes_the_stored_state_lists(self):
        """FAIL-SAFE (the fix-check of 2026-11-05): what the residence reader finds is a flag at the gate, never a
        stored limit; the stored lists come from the location and the older list rules, as before."""
        for text in ("This is a remote position, open to candidates who reside in: Texas.", "Must reside in Minnesota.",
                     "Candidates must not reside in California."):
            self.assertEqual(remote_state_rules("Remote", text, "remote"), ([], []), text)
        self.assertEqual(remote_state_rules("Remote", "You must reside in one of the following states: TX, FL.", "remote"),
                         (["FL", "TX"], []))   # the older list rule still stores it (since D119 a flag at the gate)
        allowed, excluded, sentence = residence_limits("Remote role. Open to candidates who reside in: Texas. Apply now.")
        self.assertEqual((allowed, excluded, sentence), (["TX"], [], "Open to candidates who reside in: Texas"))

    def test_a_pay_transparency_notice_never_rejects_a_remote_role(self):
        """Through the gate: a pay notice for another state in a nationwide posting never rejects the FL profile. Since
        D123 (2026-11-09) it is CONDITIONAL with one residence_limit_check (no reading says a list is no limit on you any
        more: the accepted cost); until D123 it was silent, read as no limit, and the D119 checks found requirements
        after such notices passing silently with it. The same notice naming FL stays silent."""
        text = postings.GOOD_FIT.split("This role is remote")[0]
        reqs = extract(text, POLICY).requirements
        for notice, stored, silent in (
                ("\nFor candidates residing in Colorado, the base salary range is $90,000 - $110,000 per year.", [], False),
                ("\nFor candidates living in California, Colorado or Washington, the base salary range is $90,000 - $110,000.",
                 ["CA", "CO", "WA"], False),   # stored by the older list rule as read (wf_ab7f136d: it rejected)
                ("\nFor candidates living in California, Florida or Washington, the base salary range is $90,000 - $110,000.",
                 ["CA", "FL", "WA"], True)):
            with self.subTest(notice=notice):
                role = job("Business Systems Analyst", text + notice, location="United States")
                role["remote_type"] = "remote"
                role["remote_states"], role["remote_excluded_states"] = remote_state_rules("United States", text + notice, "remote")
                self.assertEqual((role["remote_states"], role["remote_excluded_states"]), (stored, []))
                result = evaluate(role, reqs, PROFILE, POLICY)
                self.assertEqual(result.decision, "eligible")
                self.assertFalse(STATE_FAILURES & {f["code"] for f in result.hard_failures})
                self.assertEqual((state_for(result, POLICY), len(residence_flags(result.flags))),
                                 ("ELIGIBLE", 0) if silent else ("CONDITIONAL", 1))

    def test_gate_fails_a_role_limited_to_one_other_state(self):
        """FAIL-SAFE (2026-11-05): a residence sentence never rejects; it makes the role CONDITIONAL with the sentence
        quoted, and says nothing when your state is the one named. (The name is from before the fail-safe, when such a
        role was rejected; the full run's receipt cites it, so it stays until the next full run.)"""
        from careeros.qualify.facts import eligibility_facts
        from careeros.qualify.gate import state_for
        text = postings.GOOD_FIT.split("This role is remote")[0]
        reqs = extract(text, POLICY).requirements
        for where, flagged in (("Texas", True), ("Florida", False)):   # the synthetic profile is in FL
            line = f"\nThis is a remote position, open to candidates who reside in: {where}."
            role = job("Business Systems Analyst", text + line, location="United States")
            role["remote_type"] = "remote"
            role["remote_states"], role["remote_excluded_states"] = remote_state_rules("United States", text + line, "remote")
            result = evaluate(role, reqs, PROFILE, POLICY)
            self.assertEqual(result.decision, "eligible", where)
            self.assertEqual(result.has_flag("residence_limit_check"), flagged, where)
            if flagged:
                self.assertEqual(state_for(result, POLICY), "CONDITIONAL")
                flag = next(f for f in result.flags if f["code"] == "residence_limit_check")
                self.assertIn("open to candidates who reside in: Texas", flag["source_text"])
                fact = next(f for f in eligibility_facts(role, reqs, result, POLICY) if f["facet"] == "location_state")
                self.assertEqual(fact["status"], "unknown")

    def test_gate_uses_exclusions_correctly(self):
        """D123 (2026-11-09; D119): an exclusion list read from the description text ("not eligible for hire in: ...")
        never rejects. Naming your state or not, it is CONDITIONAL (residence_limit_check) with the sentence quoted: an
        exclusion naming other states only is still a list that doesn't name yours as allowed (until D123 it said nothing:
        the older rule's exclusion reading, a 50-character negation window, was trusted). The gate reads the text, not
        the stored lists: a stored exclusion the text doesn't explain (stored by older code) changes nothing. (Until D119
        naming your state was the hard rejection state_excluded.)"""
        reqs = extract(BASE, POLICY).requirements
        for text, listed in ((EXCLUSION, "AK, CA, CO, HI, NY, WA"), (EXCLUSION.replace("HI, NY", "FL, HI, NY"), "AK, CA, CO, FL, HI, NY, WA")):
            role = stored_lists(job("Business Systems Analyst", BASE + "\n" + text, location="United States", workplace="remote"))
            self.assertEqual(", ".join(role["remote_excluded_states"]), listed)   # stored as read
            for stale in (None, ["FL", "CA"]):   # a stored exclusion from older code that the text doesn't name
                if stale:
                    role["remote_excluded_states"] = stale
                result = evaluate(role, reqs, PROFILE, POLICY)
                self.assertFalse(STATE_FAILURES & {f["code"] for f in result.hard_failures})
                self.assertEqual((result.decision, state_for(result, POLICY)), ("eligible", "CONDITIONAL"))
                flag = residence_flags(result.flags)
                self.assertEqual([f["source_text"] for f in flag], [f"This role is not eligible for hire in: {listed.replace(', WA', ', or WA')}"])
                self.assertIn(f"(names {listed} beside an exclusion or negation); you are in FL", flag[0]["detail"])

    # D149 (Codex Pass 2, F and the residence High, R-R1): a location field naming only other states beside a description
    # that names FL in any way (a list, a negation, an unrelated sentence): CONDITIONAL, one residence_limit_check quoting
    # the location, never INELIGIBLE. (line, location)
    D149_LOCATION_NAMES_HOME = (
        ("Applicants may reside in Florida or Georgia. Florida offices do not have on-site parking.", "Remote - TX"),
        ("Applicants may reside in Florida or Georgia.\nFlorida offices do not have on-site parking.", "Remote - TX"),
        ("Applicants may reside in Florida or Georgia. Our Florida office is not open on weekends.", "Remote - Texas only"),
        ("Applicants may reside in FL or GA. No relocation to Florida is offered.", "Remote - TX"),
        ("Candidates who reside in Florida or New York are not eligible.", "Remote - TX"),
        ("We cannot hire in Florida at this time.", "Remote - TX"),
        ("Our Florida team supports members nationwide.", "Remote - TX"),
    )
    # D149 (Codex Pass 2, F, R-R2): a second state statement, or a lone one not naming FL plainly, flags (line; location
    # "Remote - US")
    D149_SECOND_STATEMENT = (
        "Applicants must reside in Florida, Texas; we can hire only in Texas.",
        "Applicants must reside in Florida, Texas. We can only hire in TX.",
        "Applicants must reside in Florida or Texas and we can hire only in Texas.",
        "Applicants must reside in Florida, Georgia, or Texas.\nCandidates must reside in Texas.",
        "Applicants must reside in Florida or Texas. Texas only.",
        "Applicants must reside in Florida or Texas. Must be located within Texas.",
        "Applicants must reside in Florida or Texas. Must be in TX.",
        "Applicants must reside in Florida or Texas. The role is restricted to Texas.",
        "Applicants must reside in Florida or Texas. We are only able to employ people in approved states.",
        "We can only hire in TX.",
        "We cannot hire in Florida.",
        "This role is open to candidates who reside in Florida, Georgia, or Texas. Texas residents are not eligible at "
        "this time.",
        "We hire in FL, GA and TX.\nNote: TX is excluded for this role.",
        "This position is open to residents of certain states; residents of Florida, Alaska and Hawaii are among them.",
    )
    # ... and its controls: one statement naming FL plainly (a back-reference to it is the same statement): silent
    D149_ONE_STATEMENT = (
        "Applicants must reside in Florida, Texas, or Georgia.",
        "You must reside in one of the following states: Florida, Georgia.",
        "Our employees, living in Florida, Colorado and New York, must reside in one of those states.",
        "We only hire in Florida.",
    )

    def test_d149_a_location_conflict_with_your_state_named_anywhere_is_conditional(self):
        """D149 (Codex Pass 2, F, and the High "the changed residence guard turns an existing conflict into a hard
        rejection"), R-R1, in PRIMARY and NIGHT: "Remote - TX" beside "Applicants may reside in Florida or Georgia." was
        CONDITIONAL, and adding "Florida offices do not have on-site parking." made it INELIGIBLE (state_not_eligible):
        the negation word doubted the list, and with it the only thing holding the rejection back. The location field
        now rejects on state only when the description never names your state; named in any way, CONDITIONAL with one
        residence_limit_check. Controls: a location naming only another state with no mention of FL still rejects, and
        a location naming another country still rejects (outside_us), whatever the description says of FL."""
        for route in (primary_gate, night_gate):
            for line, location in self.D149_LOCATION_NAMES_HOME:
                with self.subTest(line=line, location=location, route=route.__name__):
                    state, failures, flags = route(line, location)
                    self.assertEqual((state, failures), ("CONDITIONAL", []))
                    found = residence_flags(flags)
                    self.assertEqual([f["source_text"] for f in found], [location])
            for line, location in (("", "Remote - TX"), ("Applicants must reside in Texas or Georgia.", "Remote - Texas"),
                                   ("Texas offices do not have on-site parking.", "Remote - TX")):
                with self.subTest(line=line, location=location, route=route.__name__):   # D151: these rejected until then
                    state, failures, flags = route(line, location)
                    self.assertEqual((state, failures, len(residence_flags(flags))), ("CONDITIONAL", [], 1))
            for line in ("", "Applicants may reside in Florida or Georgia."):
                state, failures, flags = route(line, "Remote - Canada")
                self.assertEqual(state, "INELIGIBLE", line)
                self.assertIn("outside_us", [f["code"] for f in failures], line)

    def test_d149_a_second_state_statement_is_conditional(self):
        """D149 (Codex Pass 2, F), R-R2, in PRIMARY and NIGHT: "Applicants must reside in Florida, Texas; we can hire only
        in Texas." passed silently for FL, the narrowing after the first list missed. The description is silent on
        residence only when it holds no state statement (a residence, hiring, eligibility, exclusion or "only" cue with a
        state, or the word "state", in its clause), or exactly one naming FL plainly; any second statement, before or
        after, or a lone one not naming FL plainly, is CONDITIONAL with one residence_limit_check quoting it. Until D149
        "Texas residents are not eligible", "TX is excluded" and "certain states; ... Florida ... among them" after a
        list naming FL were silent (a needless CONDITIONAL now, the accepted cost)."""
        for route in (primary_gate, night_gate):
            for line in self.D149_SECOND_STATEMENT:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    found = residence_flags(flags)
                    self.assertEqual((state, len(found)), ("CONDITIONAL", 1), flags)
                    self.assertIn(found[0]["source_text"], " ".join(line.split()))
            for line in self.D149_ONE_STATEMENT:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertEqual((state, failures, residence_flags(flags)), ("ELIGIBLE", [], []))

    # D151 (red team round 1 on 4199f10, R1, R2, R4): a location field naming another state, in any format, beside any
    # description: CONDITIONAL, one residence_limit_check quoting the location, never INELIGIBLE (line, location)
    D151_LOCATION = (
        ("Applicants may reside in Texas or Fla.", "Remote - TX"),
        ("Applicants may reside in TX, GA or Fl.", "Remote - TX"),
        ("Applicants may reside in Texas or the Sunshine State.", "Remote - TX"),
        ("Applicants may reside in Texas or Tampa / Miami metro areas.", "Remote - TX"),
        ("This role is open to remote employees in all 50 U.S. states.", "Remote - TX"),
        ("Applicants may reside in any U.S. state.", "Remote - TX"),
        ("Applicants may reside in Florida or Georgia.", "TX (Remote)"),
        ("Applicants may reside in Florida or Georgia.", "US-TX-Remote"),
        ("Applicants may reside in Florida or Georgia.", "Remote, Texas; Georgia"),
        ("", "TX (Remote)"), ("", "US-TX-Remote"), ("", "Texas"), ("", "Austin, TX"), ("", "Remote - TX"),
        ("", "Remote - Austin, Texas, United States"),
    )
    # D151 (R3): a description naming states in more than one clause, or in a clause not naming FL plainly, or
    # restricting states with none named: CONDITIONAL (location "Remote - US")
    D151_SECOND_MENTION = (
        "Applicants must reside in Florida or Texas. Florida positions have been filled.",
        "Applicants must reside in Florida or Texas (Florida pending approval).",
        "Applicants must reside in Florida or Texas, but this requisition is for Texas.",
        "Applicants must reside in Florida or Texas. This req: TX residents.",
        "Applicants must reside in Florida or Texas. Our Florida office opened in 2019.",
        "Candidates must be located in Florida or Georgia. Our headquarters is in Texas.",
        "Candidates must be located in Florida or Georgia and have Texas Medicaid experience.",
        "Eligible states:\nFlorida\nGeorgia\nOffice: Austin, Texas",
        "Candidates must reside in:\n- Florida\n- Georgia\n- Experience with Texas Medicaid",
        "Applicants must reside in Florida and work 8-5 CT.",
        "Candidates must reside in the U.S. Applicants in Texas get a bonus.",
        "Applicants may reside in Texas or Fla.",
        "Applicants must reside in approved states.",
        "Applicants must reside in Florida or Texas. Our Portland, OR office is closed.",
    )
    # ... and its controls: exactly one clause names states, as one list naming FL, with no negation: silent
    D151_ONE_MENTION = (
        "Applicants must reside in Florida or Texas.",
        "You must reside in one of the following states: Florida, Georgia.",
        "Candidates residing in the following states:\n- Florida\n- Texas",
        "This role is open to candidates residing in the following states:\n\n- Florida\n\n- Georgia",
        "We hire in FL, GA and TX.",
        "We hire in FL, OR and WA.",
        "Applicants must reside in Florida or Texas. A laptop OR a desktop is provided.",
        "",
    )

    def test_d151_no_state_in_a_location_field_rejects(self):
        """D151 (R1, R2, R4), in PRIMARY and NIGHT. "Remote - TX" rejected (state_not_eligible) beside a description
        naming FL in a form no reader knew ("Fla.", "Fl.", "the Sunshine State", "Tampa / Miami") or not naming it at all
        ("all 50 U.S. states", "any U.S. state"), and "TX (Remote)", "US-TX-Remote", "Texas" and "Austin, TX" were never
        read. A location field naming another state, anywhere and in any format, never rejects: CONDITIONAL with one
        residence_limit_check quoting it. Controls: a location outside the US still rejects (outside_us); a location
        naming FL never flags on its own."""
        for route in (primary_gate, night_gate):
            for line, location in self.D151_LOCATION:
                with self.subTest(line=line, location=location, route=route.__name__):
                    state, failures, flags = route(line, location)
                    self.assertEqual((state, failures), ("CONDITIONAL", []))
                    self.assertEqual([f["source_text"] for f in residence_flags(flags)], [location])
            for line, location in (("", "Remote - Canada"), ("Applicants may reside in Florida or Georgia.", "Remote - Canada"),
                                   ("Applicants may reside in Florida.", "Toronto, ON"), ("", "Remote - Mexico"),
                                   ("Applicants may reside in Florida.", "London, United Kingdom"), ("", "Remote - India")):
                with self.subTest(line=line, location=location, route=route.__name__):
                    state, failures, _flags = route(line, location)
                    self.assertEqual((state, [f["code"] for f in failures]), ("INELIGIBLE", ["outside_us"]))
            for location in ("Remote - FL", "FL (Remote)", "US-FL-Remote", "Florida", "Tampa, FL", "Remote - FL, TX"):
                state, failures, flags = route("", location)   # (a location with no "remote" may flag remote_unknown)
                self.assertEqual((failures, residence_flags(flags)), ([], []), location)

    def test_d151_the_description_is_silent_only_with_one_mention_of_states(self):
        """D151 (R3), in PRIMARY and NIGHT. "Applicants must reside in Florida or Texas. Florida positions have been
        filled.", "... (Florida pending approval)." and "..., but this requisition is for Texas." passed silently: the
        narrowing held no cue. The description is silent on residence only when exactly one clause of it names any state,
        as one list (lines of states under a cue line ending in ":" are its list), naming FL with no exclusion or negation
        word, and nothing else restricts states; any other mention is CONDITIONAL with one residence_limit_check quoting
        it (needless for an office or a Medicaid program: the accepted cost)."""
        for route in (primary_gate, night_gate):
            for line in self.D151_SECOND_MENTION:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    found = residence_flags(flags)
                    self.assertEqual((state, len(found)), ("CONDITIONAL", 1), flags)
                    self.assertIn(" ".join(found[0]["source_text"].split()), " ".join(line.split()))
            for line in self.D151_ONE_MENTION:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertEqual((state, failures, residence_flags(flags)), ("ELIGIBLE", [], []))
    # D152 (the check of 8fb965d): a word code ("OH", "PA", "OR") as the place of a location cue is a state
    D152_WORD_CODE_PLACES = (
        "Must be located in OH.", "Must be located in PA.", "Must be located in ME.", "Must be located in CO.",
        "Candidates must be located in MD.", "We only hire in OH.", "This position is limited to OH.",
        "Must be based in PA.", "Applicants must be in OK.", "Remote within IN only", "OR residents only",
        "Applicants must reside in Florida. Residents of PA are not eligible.",
    )
    D152_WORD_CODE_CONNECTIVES = ("5+ years as a BSA; OR", "OR:", "A laptop OR a desktop is provided.",
                                  "Experience with SQL AND/OR Excel.")

    def test_d152_a_word_code_as_the_place_of_a_cue_is_a_state(self):
        """D152 (the check of 8fb965d, a silent pass): D151's rule for state codes that are everyday capital words read
        "Must be located in OH." (PA, ME, CO, MD, "We only hire in OH.", "limited to OH", "based in PA") as no state at
        all: ELIGIBLE with no flag for FL, where "TX" or "Ohio" flagged. Fail-closed: such a code is a state as the place
        of a cue word right before it ("in", "to", "from", "within", "of", "at", "only") or beside "only", "residents"
        or "based", as well as beside another state or after a city; it is ignored only as a bare word or connective
        ("; OR", "OR:", "AND/OR"). CONDITIONAL with one residence_limit_check in PRIMARY and NIGHT. Controls: the
        connectives stay silent, and for a home of OR, "Must reside in OR." is silent and "Must reside in OH." flags."""
        for route in (primary_gate, night_gate):
            for line in self.D152_WORD_CODE_PLACES:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    self.assertEqual((state, len(residence_flags(flags))), ("CONDITIONAL", 1), flags)
            for line in self.D152_WORD_CODE_CONNECTIVES:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertEqual((failures, residence_flags(flags)), ([], []))

        class HomeOR:   # the synthetic policy with home state OR (the gate reads it through policy.get)
            def get(self, *keys, default=None):
                return "OR" if keys == ("location", "home_state") else POLICY.get(*keys, default=default)

        for line, flagged in (("Must reside in OR.", 0), ("OR residents only", 0), ("Must reside in OH.", 1)):
            role = job("Business Systems Analyst", BASE + "\n" + line + PAY)
            result = gate_mod.GateResult("eligible")
            gate_mod.location_rules(result, role, PROFILE, HomeOR(), remote_only=True)
            self.assertEqual((result.hard_failures, len(residence_flags(result.flags))), ([], flagged), line)
    # D152, round 2 (the check of 7d65a23): a word code beside a location label, marks between them, or alone on a line
    # under one ("{c}" a word code); the probe of 56 templates x 16 codes (896 lines) flags 894, all but "Columbus OR"
    D152_WORD_CODE_LABELS = (
        "Location: {c}", "Location - {c}", "Location — {c}", "Location | {c}", "Location / {c}", "Location ({c})",
        "Location [{c}]", "Location, {c}", "Work location: {c} (remote)", "Remote ({c})", "Remote - {c}", "Remote: {c}",
        "Remote, {c}", "Remote/{c}", "{c} (Remote)", "{c} - Remote", "{c}-based", "{c} based", "{c} only", "{c}-only",
        "Hiring in: {c}", "Hiring: {c}", "Office: {c}", "Office location - {c}", "Site: {c}", "State: {c}", "State | {c}",
        "Eligible states: {c}", "Eligibility: {c} residents", "Region: {c}", "Territory: {c}", "Locations:\n- {c}",
        "Eligible locations:\n\n- {c}", "Location\n{c}", "| State | {c} |", "Hybrid - {c}", "On-site: {c}",
        "Must reside in: {c}", "Location: Remote — {c}", "[{c}] Remote", "{c} | Remote", "Columbus ({c})",
        "Columbus {c}",
    )

    def test_d152_a_word_code_beside_a_location_label_is_a_state(self):
        """D152, round 2 (the check of 7d65a23, a silent pass): "Location: OH", "Work location: PA (remote)", "Remote
        (OH)" and "Hiring in: MD" were ELIGIBLE with no flag for FL, as the word-code rule wanted its cue word right
        before the code. Fail-closed: spaces and the marks : ( ) [ ] - , / | and dashes passed over, a word code next to a
        cue or location word (before it, or after it but not behind ":" or ";"), after a capitalised word (a city; not
        OR), or alone on its line under a line holding a location word is a state: CONDITIONAL with one
        residence_limit_check in PRIMARY and NIGHT, for OH in every template and every word code in "Location: {c}".
        Controls: the connectives stay silent, "MS Office" is no state, and for a home of OR, "Location: OR" and "OR
        (Remote)" are silent while "Location: OH" flags."""
        lines = [t.format(c="OH") for t in self.D152_WORD_CODE_LABELS]
        lines += [f"Location: {code}" for code in ("OR", "IN", "OK", "PA", "MD", "ME", "CO", "MS", "HI", "LA", "MA", "AL",
                                                   "ID", "MO", "DE")]
        lines += ["Work location: PA (remote)", "Hiring in: MD", "Remote (OR)", "OR - Remote"]
        for route in (primary_gate, night_gate):
            for line in lines:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    self.assertEqual((state, len(residence_flags(flags))), ("CONDITIONAL", 1), flags)
            for line in self.D152_WORD_CODE_CONNECTIVES + (
                    "- 5+ years as a Business Systems Analyst\nOR\n- 5+ years as a Product Owner",
                    "- 5+ years as a Business Systems Analyst\n-- or --\n- 5+ years as a Product Owner",
                    "Experience with Excel OR SQL.", "Proficiency in MS Office and MS Excel.", "Is that OK? We think so."):
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertEqual((failures, residence_flags(flags)), ([], []))

        class HomeOR:   # the synthetic policy with home state OR
            def get(self, *keys, default=None):
                return "OR" if keys == ("location", "home_state") else POLICY.get(*keys, default=default)

        for line, flagged in (("Location: OR", 0), ("OR (Remote)", 0), ("Eligible locations:\n- OR", 0),
                              ("Location: OH", 1)):
            role = job("Business Systems Analyst", BASE + "\n" + line + PAY)
            result = gate_mod.GateResult("eligible")
            gate_mod.location_rules(result, role, PROFILE, HomeOR(), remote_only=True)
            self.assertEqual((result.hard_failures, len(residence_flags(result.flags))), ([], flagged), line)
    # D155 (Codex Pass 3, F7): a quoted code, and a residence or eligibility cue line over lines naming no state
    D155_RESIDENCE = (
        "Must be located in “OH”.", 'Must be located in "OH".', "Must be located in 'OH'.",
        "Must be located in «OH».", "Must be located in (“OH”).", "Location: “PA”",
        "Applicants must reside in Florida.\nEligible states:\n- Northeast region\n- Pacific region",
        "Applicants must reside in Florida.\nEligible locations:\n- Pacific Northwest\n- New England",
        "Applicants must reside in Florida.\nMust reside in:\n- Approved states only",
        "Applicants must reside in Florida.\nHiring states:\n\n- See the list on our careers site",
        "Eligible states:\n- Northeast region",
    )
    D155_RESIDENCE_SILENT = (
        "Applicants must reside in Florida.", "Must be located in “FL”.",
        "Eligible states:\n- Florida\n- Georgia", "Benefits eligibility:\n- Medical\n- Dental",
        "Applicants must reside in Florida.\nResponsibilities:\n- Review claims\n- Write SQL",
    )

    def test_d155_a_quoted_code_and_a_cue_line_naming_no_state_are_conditional(self):
        """D155 (Codex Pass 3, F7), in PRIMARY and NIGHT: "Must be located in “OH”." passed silently for FL
        (the quotes kept the code from its cue), and after a list naming FL, "Eligible states:\n- Northeast region\n-
        Pacific region" did too (its lines name no state, so no reader kept the cue). Quotes and other marks around a
        code are passed over, and a residence or eligibility cue line ending in ":" whose lines below name no state is a
        restriction naming none: CONDITIONAL with one residence_limit_check. Controls: a list naming FL, a quoted FL, a
        benefits eligibility list and an unrelated heading stay silent; a location outside the US still rejects."""
        for route in (primary_gate, night_gate):
            for line in self.D155_RESIDENCE:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in failures}, failures)
                    self.assertEqual((state, len(residence_flags(flags))), ("CONDITIONAL", 1), flags)
            for line in self.D155_RESIDENCE_SILENT:
                with self.subTest(line=line, route=route.__name__):
                    state, failures, flags = route(line)
                    self.assertEqual((state, failures, residence_flags(flags)), ("ELIGIBLE", [], []))
            state, failures, _flags = route("Must be located in “OH”.", "Remote - Canada")
            self.assertEqual((state, [f["code"] for f in failures]), ("INELIGIBLE", ["outside_us"]))

class TextStateListEndToEndTests(unittest.TestCase):
    """D123 (2026-11-09; D119), end to end: the lines of StateRuleTests.TEXT_LISTS and the location cases in a PRIMARY and
    a NIGHT posting, through ingest and process (PRIMARY gate, scores, NIGHT) on an in-memory store. The stored state
    lists stay as the older rule reads them; the stored gate and NIGHT results never reject the FL profile on a list from
    the description (wf_ab7f136d: rejected, unscored), and only the location field rejects."""

    def test_text_lists_are_stored_as_read_and_never_reject(self):
        """D123: each TEXT_LISTS line is stored as the older rule reads it (unchanged), and the stored gate result, score
        and NIGHT result are ELIGIBLE with no residence flag where the line names FL with no exclusion or negation near
        it, else CONDITIONAL with one residence_limit_check; never a rejection."""
        from tests.test_v21_policy import NIGHT_ON
        from tests.test_v22_night import QUIET
        clock = FixedClock("2026-10-10T12:00:00.000Z")
        store = open_store(":memory:", clock, NIGHT_ON.fingerprint)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Context(store, NIGHT_ON, PROFILE, JOB_MATRIX, Path(tmp))
            employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                        "ats": "greenhouse", "board_token": "synth", "industry": "health insurance", "status": "verified"}
            store.writer("admin").insert("employers", employer)
            lines = StateRuleTests.TEXT_LISTS
            jobs = [gh_job(600 + i, "Business Systems Analyst", BASE + "\n" + line + PAY, req=f"P-{i}") for i, (line, _) in enumerate(lines)]
            jobs += [gh_job(700 + i, "Claims Quality Auditor (Part-Time)", QUIET + "\n" + line, req=f"N-{i}") for i, (line, _) in enumerate(lines)]
            client = HttpClient(transport=httpx.MockTransport(Board(jobs).handler), min_interval=0, sleep=lambda s: None)
            try:
                ingest(ctx, employer, fetch_board(client, "greenhouse", "synth"))
                clock.advance(minutes=1)
                process(ctx)
            finally:
                client.close()
            for i, (line, where) in enumerate(lines):
                with self.subTest(line=line):
                    row = store.one("SELECT j.current_state, j.location_raw, j.description_text, j.remote_states_json, "
                                    "j.remote_excluded_states_json, g.decision, g.hard_failures_json, g.flags_json FROM jobs j "
                                    "JOIN gate_results g ON g.job_id = j.job_id WHERE j.job_id = ?", (f"greenhouse:synth:{600 + i}",))
                    stored = [json.loads(row["remote_states_json"]), json.loads(row["remote_excluded_states_json"])]
                    self.assertEqual(stored, list(remote_state_rules(row["location_raw"], row["description_text"], "remote")))
                    self.assertEqual((row["decision"], json.loads(row["hard_failures_json"])), ("eligible", []))
                    self.assertEqual(row["current_state"], "scored")
                    flags = residence_flags(json.loads(row["flags_json"]))
                    self.assertEqual(len(flags), 0 if where is None else 1, flags)
                    score = store.one("SELECT state FROM scores WHERE job_id = ?", (f"greenhouse:synth:{600 + i}",))
                    self.assertEqual(score["state"], "ELIGIBLE" if where is None else "CONDITIONAL")
                    night_row = store.one("SELECT decision, state, failures_json FROM night_results WHERE job_id = ?",
                                          (f"greenhouse:synth:{700 + i}",))
                    self.assertFalse(STATE_FAILURES & {f["code"] for f in json.loads(night_row["failures_json"])})
                    self.assertEqual((night_row["decision"], night_row["state"]),
                                     ("eligible", "ELIGIBLE" if where is None else "CONDITIONAL"))
        store.close()

    def test_a_location_limit_holds_end_to_end(self):
        """D123 (2026-11-09; D119), end to end: a location field naming only another state rejects the FL profile in the
        stored gate and NIGHT results, quoting the location, when no description list names FL as allowed
        (StateRuleTests.LOCATION_REJECTS); beside any description list naming FL (LOCATION_MASKED: a requirement, a pay
        notice, and since D123 the members a plan serves and where the team lives, which rejected) the two disagree: it
        is CONDITIONAL and scored, with one residence_limit_check quoting the location."""
        from tests.test_v21_policy import NIGHT_ON
        from tests.test_v22_night import QUIET
        clock = FixedClock("2026-10-10T12:00:00.000Z")
        store = open_store(":memory:", clock, NIGHT_ON.fingerprint)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Context(store, NIGHT_ON, PROFILE, JOB_MATRIX, Path(tmp))
            employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                        "ats": "greenhouse", "board_token": "synth", "industry": "health insurance", "status": "verified"}
            store.writer("admin").insert("employers", employer)
            lines = ([(line, location, False) for line, location in StateRuleTests.LOCATION_MASKED]
                     + [(line, location, True) for line, location in StateRuleTests.LOCATION_REJECTS])
            jobs = [gh_job(800 + i, "Business Systems Analyst", BASE + "\n" + line + PAY, req=f"L-{i}", location=location)
                    for i, (line, location, _rejects) in enumerate(lines)]
            jobs += [gh_job(900 + i, "Claims Quality Auditor (Part-Time)", QUIET + "\n" + line, req=f"M-{i}", location=location)
                     for i, (line, location, _rejects) in enumerate(lines)]
            client = HttpClient(transport=httpx.MockTransport(Board(jobs).handler), min_interval=0, sleep=lambda s: None)
            try:
                ingest(ctx, employer, fetch_board(client, "greenhouse", "synth"))
                clock.advance(minutes=1)
                process(ctx)
            finally:
                client.close()
            for i, (line, location, rejects) in enumerate(lines):
                with self.subTest(line=line):
                    row = store.one("SELECT j.current_state, j.location_raw, j.description_text, j.remote_states_json, "
                                    "j.remote_excluded_states_json, g.decision, g.hard_failures_json, g.flags_json FROM jobs j "
                                    "JOIN gate_results g ON g.job_id = j.job_id WHERE j.job_id = ?", (f"greenhouse:synth:{800 + i}",))
                    self.assertEqual([json.loads(row["remote_states_json"]), json.loads(row["remote_excluded_states_json"])],
                                     list(remote_state_rules(row["location_raw"], row["description_text"], "remote")))   # as read
                    failures = [(f["code"], f["source_text"]) for f in json.loads(row["hard_failures_json"])]
                    flags = [f["source_text"] for f in residence_flags(json.loads(row["flags_json"]))]
                    night_row = store.one("SELECT decision, state, failures_json FROM night_results WHERE job_id = ?",
                                          (f"greenhouse:synth:{900 + i}",))
                    night_failures = {f["code"] for f in json.loads(night_row["failures_json"])}
                    if rejects:   # D151: no longer rejected; CONDITIONAL, the flag quoting the location
                        self.assertEqual((row["decision"], failures, flags), ("eligible", [], [location]))
                        self.assertEqual((night_row["decision"], night_row["state"], night_failures),
                                         ("eligible", "CONDITIONAL", set()))
                        continue
                    self.assertEqual((row["current_state"], row["decision"], failures, flags), ("scored", "eligible", [], [location]))
                    score = store.one("SELECT state FROM scores WHERE job_id = ?", (f"greenhouse:synth:{800 + i}",))
                    self.assertEqual(score["state"], "CONDITIONAL")
                    self.assertEqual((night_row["decision"], night_row["state"], night_failures), ("eligible", "CONDITIONAL", set()))
        store.close()


class SeniorityAndYearsTests(unittest.TestCase):
    def test_individual_contributor_manager_titles(self):
        terms = POLICY.get("seniority_terms")
        self.assertEqual(seniority(norm("Senior Product Manager, PBM Platform"), terms), "senior")
        self.assertEqual(seniority(norm("Project Manager"), terms), "unspecified")
        self.assertEqual(seniority(norm("Manager, Claims Operations"), terms), "manager")

    def test_years_shortfall_is_a_flag_not_a_rejection(self):
        reqs = extract("Requirements:\n- 4+ years of data validation required\n- Bachelor's degree or equivalent", POLICY).requirements
        result = evaluate(job("Data Operations Analyst", "x"), reqs, PROFILE, POLICY)
        self.assertNotIn("years_gap", {f["code"] for f in result.hard_failures})


class ReevaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, PROFILE, JOB_MATRIX, Path(self.tmp.name))
        self.employer = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health",
                         "ats": "greenhouse", "board_token": "synth", "industry": "health", "status": "verified"}
        self.store.writer("admin").insert("employers", self.employer)
        body = postings.GOOD_FIT.split("This role is remote")[0] + "\n" + EXCLUSION
        board = Board([gh_job(301, "Business Systems Analyst", body, req="X1", location="United States")])
        self.http = HttpClient(transport=httpx.MockTransport(board.handler), min_interval=0, sleep=lambda s: None)
        ingest(self.ctx, self.employer, fetch_board(self.http, "greenhouse", "synth"))

    def tearDown(self):
        self.http.close()
        self.tmp.cleanup()

    def test_stale_stored_states_are_refreshed_and_job_reevaluated(self):
        # Simulate a job normalized by the old code, which stored the exclusion list as the allowed list.
        self.store.conn.execute("UPDATE jobs SET remote_states_json = ?, remote_excluded_states_json = '[]', remote_type = 'remote'",
                                (json.dumps(["AK", "CA", "CO", "HI", "NY", "WA"]),))
        process(self.ctx)
        row = self.store.one("SELECT current_state, remote_states_json, remote_excluded_states_json FROM jobs")
        self.assertEqual(row["remote_states_json"], "[]")
        self.assertEqual(row["current_state"], "scored")
        self.assertEqual(self.store.verify_event_chain(), [])

    def test_rules_version_change_reevaluates_existing_decisions(self):
        process(self.ctx)
        before = self.store.one("SELECT COUNT(*) c FROM gate_results")["c"]
        original = gate_mod.RULES_VERSION
        gate_mod.RULES_VERSION = original + "-test"
        try:
            self.clock.advance(minutes=1)
            counts = process(self.ctx)
        finally:
            gate_mod.RULES_VERSION = original
        self.assertGreaterEqual(counts["reevaluated"], 1)
        self.assertGreater(self.store.one("SELECT COUNT(*) c FROM gate_results")["c"], before)


if __name__ == "__main__":
    unittest.main()
