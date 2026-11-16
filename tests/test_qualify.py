import unittest
from pathlib import Path

from careeros.core.policy import load_policy
from careeros.extract.rules import extract
from careeros.normalize.posting import (canonical_url, classify_family, detect_country, detect_remote, employment_type,
                                        excluded_term, home_pay_tier, norm, parse_comp, remote_states)
from careeros.normalize.sanitize import html_to_text
from careeros.paths import CONFIG_DIR
from careeros.qualify.gate import evaluate
from careeros.qualify.profile import load_profile
from careeros.qualify.scores import score

from tests import postings

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PROFILE = load_profile(FIXTURES / "synthetic_profile.yaml")
NOW = "2026-10-10T12:00:00.000Z"


def job(title, text, location="Remote - US", workplace=None, employment=None, posted="2026-10-10T06:00:00.000Z",
        employer="Synthetic Health", parent=None):
    remote = detect_remote(location, workplace, text)
    comp = parse_comp(text) or {}
    return {"title": title, "title_norm": norm(title), "family": classify_family(norm(title), POLICY.get("families")),
            "description_text": text, "location_raw": location, "remote_type": remote,
            "remote_states": remote_states(location, text, remote), "country": detect_country(location, text, remote),
            "employment_type": employment_type(employment, text), "comp_min": comp.get("min"), "comp_max": comp.get("max"),
            "comp_period": comp.get("period"), "posted_at": posted, "employer_name": employer, "parent": parent or employer}


def run(j):
    reqs = extract(j["description_text"], POLICY).requirements
    gate = evaluate(j, reqs, PROFILE, POLICY)
    return reqs, gate


OSCAR_TIERS = ("Pay Transparency: The base pay for this role in the states of California, Connecticut, New Jersey, New "
               "York, and Washington is: $86,000.00 - $96,000.00 per year. The base pay for this role in all other "
               "locations is: $70,000.00 - $78,000.00 per year. You are also eligible for monthly vacation accrual.")


# D155 (F6): "You are also eligible for monthly vacation accrual." holds an eligibility term, so the full Oscar wording
# chooses no tier by label (pay_tier_unknown when it straddles the floor); the tier tests read it without that sentence
OSCAR_PLAIN = OSCAR_TIERS.replace(" You are also eligible for monthly vacation accrual.", "")


class PayTests(unittest.TestCase):
    """2026-11-04: cents, the range's own period, and the tier for your state
    (vault/reports/PAY_AND_STATE_PARSING_COUNTS_2026-11-04.md). The synthetic policy: home FL, floor 80,000."""

    def comp(self, text, home=None):
        got = parse_comp(text, None, home)
        return got and (round(got["min"], 2), round(got["max"], 2), got["period"])

    def test_cents_and_the_ranges_own_period(self):
        self.assertEqual(self.comp("The base pay for this role is: $55,890.00 - $73,355.63 per year."),
                         (55890, 73355.63, "year"))
        self.assertEqual(self.comp("The base pay for this role is: $32.60 - $42.79 per hour. You are also eligible for "
                                   "monthly vacation accrual."), (32.6, 42.79, "hour"))
        self.assertEqual(self.comp("Pay Transparency: The hourly rate will be $33.04-$43.37. You are also eligible for "
                                   "monthly vacation accrual."), (33.04, 43.37, "hour"))
        self.assertEqual(self.comp("Base salary $80,000 - $100,000, plus a monthly stipend."), (80000, 100000, "year"))
        self.assertEqual(self.comp("The range is $60,000 - $80,000 more or less."), (60000, 80000, "year"))
        self.assertEqual(self.comp("$40 - $50 an hour"), (40, 50, "hour"))
        self.assertEqual(self.comp("Monthly salary: $5,000 - $6,000."), (5000, 6000, "month"))

    def test_the_pay_tier_for_your_state(self):
        self.assertEqual(self.comp(OSCAR_PLAIN, "FL"), (70000, 78000, "year"))   # all other locations
        self.assertEqual(self.comp(OSCAR_PLAIN, "NY"), (86000, 96000, "year"))   # named
        self.assertEqual(self.comp(OSCAR_TIERS), (86000, 96000, "year"))         # no home state: the first range
        self.assertEqual(self.comp("NYC or SF: $120,000 - $150,000. All other locations: $100,000 - $125,000 per year.",
                                   "FL"), (100000, 125000, "year"))
        self.assertEqual(self.comp("Florida: $70,000 - $80,000 per year; California: $90,000 - $100,000 per year.", "FL"),
                         (70000, 80000, "year"))
        # the posting doesn't say which tier is yours: the first range, as before
        for text, status in (("Zone A Pay Range $62,200 - $73,200 USD. Zone B Pay Range $57,000 - $67,100 USD.", "unknown"),
                             ("New York, NY: $106,972 - $155,000. Denver, CO: $98,000 - $142,000.", "unknown"),
                             ("Miami, FL: $90,000 - $100,000. All other locations: $80,000 - $90,000.", "ambiguous")):
            self.assertEqual(home_pay_tier(text, "FL")[0], status, text)
            self.assertEqual(self.comp(text, "FL"), self.comp(text), text)

    def gate(self, pay):
        j = job("Business Systems Analyst", postings.GOOD_FIT.split("This role is remote")[0] + "\n" + pay)
        comp = parse_comp(j["description_text"], None, "FL") or {}
        j.update(comp_min=comp.get("min"), comp_max=comp.get("max"), comp_period=comp.get("period"))
        gate = run(j)[1]
        return {f["code"] for f in gate.hard_failures}, {f["code"] for f in gate.flags}

    def test_the_gate_reads_your_tier_and_never_decides_on_an_unknown_one(self):
        failures, _ = self.gate(OSCAR_PLAIN)   # your tier, 78,000, is under the floor; the first range is not
        self.assertIn("below_pay_floor", failures)
        for pay in ("Zone 1: $85,000 - $95,000 per year. Zone 2: $70,000 - $78,000 per year.",   # first range clears it
                    "Zone 1: $70,000 - $78,000 per year. Zone 2: $85,000 - $95,000 per year."):  # first range doesn't
            failures, flags = self.gate(pay)
            self.assertNotIn("below_pay_floor", failures, pay)
            self.assertIn("pay_tier_unknown", flags, pay)
        failures, flags = self.gate("Zone 1: $60,000 - $70,000 per year. Zone 2: $65,000 - $75,000 per year.")
        self.assertIn("below_pay_floor", failures)   # every tier under the floor
        self.assertNotIn("pay_tier_unknown", flags)
        failures, flags = self.gate("Level I: $70,000 - $78,000 per year. Level II: $85,000 - $95,000 per year.")
        self.assertNotIn("below_pay_floor", failures)   # D123: levels on both sides of the floor flag too, never the
        self.assertIn("pay_tier_unknown", flags)        # first range (a needless CONDITIONAL accepted)

    # The review of the first fix (2026-11-04): Greenhouse's pay widget, short period windows, the quote, the state.
    GH_CATCH = ("California, New York and Washington Pay Range\n\n$85,000 — $95,000 USD\n\n"
                "All other US locations Pay Range\n\n$65,000 — $75,000 USD\n\nBenefits")
    GH_NAMED = ("California, New York and Washington Pay Range\n\n$85,000 — $95,000 USD\n\n"
                "Florida Pay Range\n\n$65,000 — $75,000 USD\n\nBenefits")
    GH_OTHER_FIRST = ("All other US locations Pay Range\n\n$65,000 — $75,000 USD\n\n"
                      "California, New York and Washington Pay Range\n\n$85,000 — $95,000 USD\n\n"
                      "The base pay offered may vary depending on location.")

    def test_a_range_ends_on_its_own_line(self):
        for text, status in ((self.GH_CATCH, "catch_all"), (self.GH_NAMED, "named"), (self.GH_OTHER_FIRST, "catch_all"),
                             ("CA, NY and WA: $85,000 - $95,000\nAll other US locations: $65,000 - $75,000", "catch_all")):
            self.assertEqual(home_pay_tier(text, "FL")[0], status, text)
            self.assertEqual(self.comp(text, "FL"), (65000, 75000, "year"), text)
        self.assertEqual(self.comp("Pay range: $50,000 - $60,000\nKey duties include"), (50000, 60000, "year"))
        self.assertEqual(self.comp("Salary: $60,000 - $70,000\nA day in the life of this role"), (60000, 70000, "year"))

    def test_the_period_comes_from_the_ranges_own_words(self):
        self.assertEqual(self.comp("The rate is $300 - $400, per day."), (300, 400, "day"))
        self.assertEqual(self.comp("A stipend of $1,000 - $1,500 (paid monthly)."), (1000, 1500, "month"))
        self.assertEqual(self.comp("Unlike our monthly stipend programs, this role pays $25 - $30."), (25, 30, "hour"))
        self.assertEqual(self.comp("This full-time role works 40 hours per week with a base salary range of $60,000 - "
                                   "$70,000."), (60000, 70000, "year"))   # a yearly amount is never weekly

    def test_a_period_not_the_ranges_own_unit_must_fit_its_amount(self):
        """The Fable review (2026-11-04): a sibling tier's unit made "Texas: $38 - $42" a yearly $42 (rejected) and a
        $70,000 catch-all hourly (passed); "paid monthly" after a range read as yearly. Every period that is not the
        range's own unit must fit the amount: the words right after it, a sibling's unit, the words before it."""
        cases = (
            ("Texas: $38 - $42. California: $150,000 - $180,000 per year.", "TX", (38, 42, "hour")),
            ("California: $150,000 - $180,000 per year. All other locations: $38 - $42.", "TX", (38, 42, "hour")),
            ("NYC: $40 - $50 per hour. All other locations: $60,000 - $70,000.", "TX", (60000, 70000, "year")),
            ("California: $75,348 - $98,894 per year. All other locations: $67,813 - $89,005.", "TX",
             (67813, 89005, "year")),   # the sibling's unit, where it fits (Oscar)
            ("Compensation: $6,500 - $7,500 paid monthly.", "FL", (6500, 7500, "month")),
            ("$6,500 - $7,500, paid monthly.", "FL", (6500, 7500, "month")),
            ("$6,500 - $7,500 each month.", "FL", (6500, 7500, "month")),
            ("$1,500 - $1,700 paid weekly.", "FL", (1500, 1700, "week")),
            ("$1,500 - $1,700 every week.", "FL", (1500, 1700, "week")),
            ("Pay: $60,000 - $70,000 paid weekly.", "FL", (60000, 70000, "year")),   # doesn't fit: its size decides
            ("$80,000 - $90,000, plus a monthly stipend.", "FL", (80000, 90000, "year")),
            ("$38 - $42\nA day in the life", "FL", (38, 42, "hour")),   # the next line is not the range's words
            # the fix-check (2026-11-05): a pay cycle after an hourly range is not its period; nor is a shift's name
            ("Pay: $25 - $30, paid weekly.", "FL", (25, 30, "hour")),
            ("Pay Rate: $42 - $48 Paid Weekly!", "FL", (42, 48, "hour")),
            ("$40 - $45 paid daily.", "FL", (40, 45, "hour")),
            ("$25 - $30 paid monthly.", "FL", (25, 30, "hour")),
            ("Weekly pay: $25 - $30.", "FL", (25, 30, "hour")),
            ("$18 - $22, Day Shift.", "FL", (18, 22, "hour")),
            ("$150 - $200, Day Shift.", "FL", (150, 200, "hour")),
            ("$300 - $400 paid daily.", "FL", (300, 400, "day")),
            # a period word in the next sentence, or in an earlier clause, is not this range's (a monthly-sized amount)
            ("Salary: $45,000 - $55,000. You also receive a monthly wellness stipend.", "FL", (45000, 55000, "year")),
            ("This role offers a monthly stipend, and a base salary of $45,000 - $55,000.", "FL", (45000, 55000, "year")),
        )
        for text, home, expected in cases:
            self.assertEqual(self.comp(text, home), expected, text)
        failures, _ = self.gate("Florida: $38 - $42. California: $150,000 - $180,000 per year.")
        self.assertNotIn("below_pay_floor", failures)   # $42 an hour clears the 80,000 floor; a yearly $42 doesn't
        failures, _ = self.gate("NYC: $60 - $70 per hour. All other locations: $60,000 - $70,000.")
        self.assertIn("below_pay_floor", failures)   # $70,000 a year is under it; hourly, it wasn't
        failures, _ = self.gate("Pay Rate: $42 - $48 Paid Weekly!")
        self.assertNotIn("below_pay_floor", failures)   # $48 an hour, paid weekly, clears it; $48 a week doesn't

    def test_the_gate_quotes_your_tier_and_a_straddle_is_conditional(self):
        from careeros.qualify.facts import pay_fact
        from careeros.qualify.gate import state_for
        j = job("Business Systems Analyst", postings.GOOD_FIT.split("This role is remote")[0] + "\n" + OSCAR_PLAIN)
        comp = parse_comp(j["description_text"], None, "FL")
        j.update(comp_min=comp["min"], comp_max=comp["max"], comp_period=comp["period"])
        failure = next(f for f in run(j)[1].hard_failures if f["code"] == "below_pay_floor")
        self.assertIn("$70,000.00 - $78,000.00", failure["source_text"])   # the tier that decided, not the first range
        straddle = job("Business Systems Analyst", postings.GOOD_FIT.split("This role is remote")[0]
                       + "\nZone 1: $70,000 - $78,000 per year. Zone 2: $85,000 - $95,000 per year.")
        gate = run(straddle)[1]
        self.assertEqual(state_for(gate, POLICY), "CONDITIONAL")
        self.assertEqual(pay_fact(straddle, POLICY)["status"], "unknown")
        board = dict(straddle, comp_min=70000.0, comp_max=76000.0, comp_period="year")   # the board's own pay fields
        failures = {f["code"] for f in run(board)[1].hard_failures}
        self.assertIn("below_pay_floor", failures)   # not these tiers: the board's pay decides, as before

    def test_night_pay_never_decides_on_an_unknown_tier(self):
        from careeros.qualify import night
        from careeros.qualify.gate import GateResult
        tiers = {"description_text": "Zone 1: $70,000 - $78,000 per year. Zone 2: $85,000 - $95,000 per year.",
                 "comp_min": 70000.0, "comp_max": 78000.0, "comp_period": "year"}
        result = GateResult("eligible")
        night._pay(result, tiers, "replacement", {"floor_annual": 80000}, "FL")
        self.assertEqual(([f["code"] for f in result.hard_failures], [f["code"] for f in result.flags]),
                         ([], ["pay_tier_unknown"]))
        result = GateResult("eligible")
        night._pay(result, dict(tiers, description_text="Pay: $70,000 - $78,000 per year."), "replacement",
                   {"floor_annual": 80000}, "FL")
        self.assertEqual([f["code"] for f in result.hard_failures], ["below_night_pay_floor"])
        weekly = {"description_text": "Pay Rate: $42 - $48 Weekly Pay!", "comp_min": 42.0, "comp_max": 48.0,
                  "comp_period": "week"}   # as stored: the range's own word, which doesn't fit $48
        result = GateResult("eligible")
        night._pay(result, weekly, "replacement", {"floor_annual": 80000}, "FL")
        self.assertEqual(([f["code"] for f in result.hard_failures], [f["code"] for f in result.flags]),
                         ([], ["pay_period_unknown"]))

    # FAIL-SAFE (the fix-check of 2026-11-05): each rule for a period that is not the range's own fitting unit misread
    # some real wording, so a period the posting leaves open never decides the floor: across it, pay_period_unknown.
    PERIOD_OPEN = ("Pay Rate: $42 - $48 Weekly Pay!", "Daily rate $650 - $750, paid weekly.",
                   "Salary: $55,000 - $59,000 paid monthly.", "Weekly pay of $450 - $495.", "Pay: $1,500 - $1,700\nPaid weekly",
                   "Hourly rate: $120 - $150, paid daily.",
                   # the fix-check of the fail-safe (2026-11-05): the readings cast a wider net than the guess
                   "$38 - $42 per year", "Pay: $1,500 - $1,700\n\nPaid weekly", "- Pay: $1,500 - $1,700\n- Paid weekly",
                   "Weekly pay, $450 - $495.", "Weekly pay\n$450 - $495", "$3,500 - $4,000",
                   "$3,200 - $3,500 paid biweekly, about $90,000 a year.",
                   "Florida: $9,000 - $9,500. California: $10,000 - $12,000 per month.",
                   "Pay: $12,000 - $14,000\nPaid monthly",   # only the next line says monthly
                   # the last fix-check (2026-11-05): a bracketed unit, later lines, the cycles, the guess
                   "Weekly earnings of $450 - $495 (hourly $11.25 - $12.37)", "Pay: $12,000 - $14,000\n\nPay is monthly",
                   "Weekly earnings of $450 - $495 (hourly)",   # one range: a bracketed unit is no explicit one
                   "Pay: $12,000 - $14,000\n\nMonthly", "Pay: $12,000 - $14,000\n\nPaid monthly",
                   "- Pay: $12,000 - $14,000\n- Paid monthly", "$9,000 - $9,500",
                   "$4,000 - $4,500 paid semi-monthly, about $100,000 a year.",
                   "$4,000 - $4,500 per pay period, about $100,000 a year.",
                   "$3,200 - $3,500 every two weeks, about $90,000 a year.",
                   "$3,200 - $3,500 fortnightly, about $90,000 a year.")

    def test_a_pay_period_the_posting_leaves_open_never_decides_the_floor(self):
        from careeros.qualify.facts import pay_fact
        from careeros.qualify.gate import state_for
        for pay in self.PERIOD_OPEN:
            failures, flags = self.gate(pay)
            self.assertNotIn("below_pay_floor", failures, pay)
            # since D123 two ranges on both sides of the floor ("$450 - $495 (hourly $11.25 - $12.37)") flag the tier
            self.assertTrue({"pay_period_unknown", "pay_tier_unknown"} & flags, pay)
        for pay, below in (("Base salary $60,000 - $70,000.", True), ("$85,000 - $95,000 per year.", False),
                           ("$40 - $45 per hour", False),   # D151: "Monthly salary: $5,000 - $6,000." now flags
                           ("$1,500 - $1,700 per week", False),
                           ("$40 - $50 per hour, paid weekly", False),   # an explicit unit settles it; the cycle doesn't
                           ("Pay range: $85,000 - $95,000\nBenefits\nMedical, dental and vision", False)):
            # (D155, F1: "This full-time role works 40 hours per week with a base salary range of $60,000 - $70,000."
            # left: a week word of the pay block is a reading whatever the amount; PayTests.test_d155_...)
            failures, flags = self.gate(pay)
            self.assertEqual("below_pay_floor" in failures, below, pay)
            self.assertNotIn("pay_period_unknown", flags, pay)
        j = job("Business Systems Analyst", postings.GOOD_FIT.split("This role is remote")[0] + "\n" + self.PERIOD_OPEN[0])
        comp = parse_comp(j["description_text"], None, "FL")
        j.update(comp_min=comp["min"], comp_max=comp["max"], comp_period=comp["period"])
        self.assertEqual(state_for(run(j)[1], POLICY), "CONDITIONAL")
        self.assertEqual(pay_fact(j, POLICY)["status"], "unknown")
        board = dict(j, comp_min=42.0, comp_max=50.0, comp_period="week")   # the board's own pay fields: they decide
        self.assertIn("below_pay_floor", {f["code"] for f in run(board)[1].hard_failures})

    def test_codex_pass1_pay_tiers_and_headings_never_decide_on_a_guess(self):
        """Codex Pass 1 (CR-2026-11-05-COMBINED), F1 and F6, with their controls. F1: an unknown tier's other periods
        (the second zone, paid monthly) and a pay heading above a label ("Monthly compensation" / "Salary range:") are
        readings too. F6: "except FL" is not Florida's tier, as "except Florida" isn't."""
        open_cases = ("Zone 1: $60,000 - $70,000 per year.\nZone 2: $7,000 - $8,000.\nPaid monthly.",
                      "Monthly compensation\nSalary Range:\n$12,000 - $14,000",
                      "Monthly compensation:\nSalary range: $12,000 - $14,000",
                      "Monthly compensation\n$12,000 - $14,000",   # the control that already flagged
                      "All states except FL: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per year.",
                      "Outside FL: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per year.",
                      "All states except Florida: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 "
                      "per year.",
                      # the Fable fix-check (2026-11-05): a qualifier carries across the list after it
                      "All states except CA, FL: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per year.",
                      "All states except CA and FL: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 "
                      "per year.",
                      "All states except California, Florida: $90,000 - $100,000 per year. All other locations: $60,000 - "
                      "$70,000 per year.",
                      "All states except (FL): $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per year.",
                      "FL excluded: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per year.")
        for pay in open_cases:
            failures, flags = self.gate(pay)
            self.assertNotIn("below_pay_floor", failures, pay)
            self.assertTrue({"pay_tier_unknown", "pay_period_unknown"} & flags, pay)
        self.assertNotEqual(home_pay_tier(open_cases[4], "FL")[0], "named")
        for pay, below in (("Zone 1: $60,000 - $70,000 per year.\nZone 2: $65,000 - $75,000 per year.", True),
                           ("Salary range:\n$85,000 - $95,000", False),
                           ("Texas: $90,000 - $100,000 per year. Florida: $60,000 - $70,000 per year.", True)):
            failures, flags = self.gate(pay)   # every reading on one side, or your tier named: it decides, no flag
            self.assertEqual("below_pay_floor" in failures, below, pay)
            self.assertFalse({"pay_tier_unknown", "pay_period_unknown"} & flags, pay)

    # The correction batch of 2026-11-09 (D120): the pay and F6 siblings the acceptance of the integrated candidate
    # found. Each is judged by PRIMARY (the gate) and by NIGHT (its replacement floor, 80,000), which share the readings.
    base_requirements = None

    def judged(self, pay):
        """[(failures, flags) in PRIMARY, (failures, flags) in NIGHT] for a pay text, as stored for the FL home. The
        posting's requirements are read once (no pay rule depends on them; reading them is most of a gate run)."""
        from careeros.qualify import night
        from careeros.qualify.gate import GateResult
        base = postings.GOOD_FIT.split("This role is remote")[0]
        if PayTests.base_requirements is None:
            PayTests.base_requirements = extract(base, POLICY).requirements
        j = job("Business Systems Analyst", base + "\n" + pay)
        comp = parse_comp(j["description_text"], None, "FL") or {}
        j.update(comp_min=comp.get("min"), comp_max=comp.get("max"), comp_period=comp.get("period"))
        primary, result = evaluate(j, PayTests.base_requirements, PROFILE, POLICY), GateResult("eligible")
        night._pay(result, j, "replacement", {"floor_annual": 80000}, "FL")
        return [({f["code"] for f in gate.hard_failures}, {f["code"] for f in gate.flags}) for gate in (primary, result)]

    def assert_open(self, pay):
        """Never a pay rejection, and flagged: the posting is CONDITIONAL in PRIMARY and NIGHT (D114)."""
        for failures, flags in self.judged(pay):
            self.assertFalse({"below_pay_floor", "below_night_pay_floor"} & failures, pay)
            self.assertTrue({"pay_tier_unknown", "pay_period_unknown"} & flags, pay)

    def assert_decided(self, pay, below):
        for failures, flags in self.judged(pay):
            self.assertEqual(bool({"below_pay_floor", "below_night_pay_floor"} & failures), below, pay)
            self.assertFalse({"pay_tier_unknown", "pay_period_unknown"} & flags, pay)

    def test_d120_the_pay_block_gives_its_period_and_only_the_pays(self):
        """D120: a period word in another sentence of the range's own line ("Salary range: $12,000 - $14,000. Paid
        monthly.") or in a heading beyond the old two-line read ("Monthly compensation" over "Salary Range:" over "USD",
        or "Monthly compensation (US$)") was never read, and PRIMARY and NIGHT rejected a yearly $14,000. The range is
        now read with its pay block. Only the pay's period words count, above or below it: a meeting's, a stipend's, paid
        leave's, or one beside an amount of another item never makes a yearly salary under the floor a flag (the
        F1-04 regression of the old two-line read), and an explicit unit ("per year", "/yr", "annually") still decides."""
        for pay in ("Salary range: $12,000 - $14,000. Paid monthly.", "Pay is monthly. Salary range: $12,000 - $14,000.",
                    "Monthly compensation. Salary range: $12,000 - $14,000",
                    "Compensation: $12,000 - $14,000 USD. This position is paid on a monthly basis.",
                    "Salary range: $12,000 - $14,000. Monthly.",
                    "Monthly compensation\nSalary Range:\nUSD\n$12,000 - $14,000",
                    "Monthly compensation (US$)\nSalary range: $12,000 - $14,000",
                    "Monthly compensation\nThe range below reflects the base pay for candidates in all US states:\n"
                    "$12,000 - $14,000",
                    "Monthly\nSalary Range:\nUSD\n$12,000 - $14,000", "Monthly compensation\nHiring range:\n$12,000 - $14,000",
                    "Hiring range (monthly)\n$12,000 - $14,000", "USD / Monthly\n$12,000 - $14,000",
                    "Monthly (USD)\nSalary range:\n$12,000 - $14,000",
                    "## Monthly Compensation\n\nSalary Range:\n\n$12,000 - $14,000 USD",
                    "Pay: $12,000 - $14,000\nWeekly direct deposit",
                    # the third correction (P-1): beside a range with no explicit unit or yearly label of its own,
                    # another item's period on its line or the lines beside it is a plausible reading too
                    "Schedule: weekly rotation\nSalary range:\n$15,000 - $18,000",
                    "- Monthly paid time off\n\nSalary range:\n$50,000 - $55,000",
                    "Salary range: $50,000 - $55,000. A $500 bonus is paid monthly.",
                    "Salary: $45,000 - $55,000\nMonthly wellness stipend included."):
            self.assert_open(pay)
        # D151 (P1): "Salary range: $12,000 - $14,000" (no period anywhere, under $20,000) left: it now flags
        # (D155, F1: "Monthly compensation\nSalary range: $60,000 - $70,000 per year" left: the monthly word is a reading
        # whatever the amount; PayTests.test_d155_...)
        # D123 (the fourth correction): every period word of the pay block is a reading, another item's too, and an
        # explicit yearly unit decides only when no other period word is in the block (a needless CONDITIONAL accepted)
        for pay in ("Annual base salary: $50,000 - $55,000. We hold monthly all-hands meetings.",
                    "We hold monthly all-hands meetings.\nSalary range:\n$50,000 - $55,000",
                    "Schedule: weekly rotation\nAnnual salary range: $15,000 - $18,000",
                    "Monthly stipend: $500\nSalary range: $50,000 - $55,000",
                    "Salary range: $30,000 - $35,000 annually. Paid bi-weekly.",
                    "Monthly compensation\nSalary Range:\nUSD\n$12,000 - $14,000/yr"):
            self.assert_open(pay)

    def test_d120_a_place_tier_is_read_every_way_whatever_its_status(self):
        """D120: a place tier is read every way it could be paid whatever its status (named, catch-all or ambiguous),
        as an unknown one (zones) already was, and with the period sentence on its line. "California: $60,000 -
        $70,000 per year. Florida: $7,000 - $8,000. Paid monthly." took California's "per year" for Florida's tier and
        rejected a yearly $8,000 in PRIMARY and NIGHT; the tier your state is in now straddles the floor and flags."""
        for pay, status in (("California: $60,000 - $70,000 per year. Florida: $7,000 - $8,000. Paid monthly.", "named"),
                            ("CA: $60,000 - $70,000 per year. FL: $7,000 - $8,000. Paid monthly.", "named"),
                            ("California: $60,000 - $70,000 per year. All other locations: $7,000 - $8,000. Paid monthly.",
                             "catch_all"),
                            ("Florida: $7,000 - $8,000. California: $60,000 - $70,000 per year. Paid monthly.", "named"),
                            ("All other locations: $7,000 - $8,000. California: $60,000 - $70,000 per year. Paid monthly.",
                             "catch_all"),
                            ("Miami, FL: $60,000 - $70,000 per year. All other locations: $7,000 - $8,000. Paid monthly.",
                             "ambiguous"),
                            ("California: $60,000 - $70,000 per year. Florida: $7,000 - $8,000.", "named"),
                            ("California: $12,000 - $14,000. Florida: $10,000 - $12,000. Paid monthly.", "named"),
                            ("California: $60,000 - $70,000 per year. Florida: $3,600 - $4,000. Paid semi-monthly.",
                             "named")):
            self.assertEqual(home_pay_tier(pay, "FL")[0], status, pay)
            self.assert_open(pay)
        # D123: an explicit yearly unit beside another period word ("Paid monthly") no longer decides alone
        self.assert_open("California: $60,000 - $70,000 per year. Florida: $50,000 - $55,000 per year. Paid monthly.")
        for pay, below in (("California: $60,000 - $70,000 per year. Florida: $50,000 - $55,000.", True),
                           ("California: $60,000 - $70,000 per year. Florida: $7,000 - $8,000 per month.", False),
                           ("Texas: $60,000 - $70,000 per year. Florida: $90,000 - $100,000 per year.", False)):
            self.assert_decided(pay, below)

    def test_d120_an_excluded_city_or_region_state_never_chooses_its_tier(self):
        """D120 (F6 parity): words between an exclusion and the state ("Outside the state of FL", "All states except
        for FL", "Not including Florida", "All states excluding residents of Florida"), "Non-FL", "excl. FL", and a
        city or region of the state written out ("Tampa, Florida", "South Florida") made the excluded tier Florida's:
        a wrong pass or a wrong rejection, by tier order and polarity. They now mark Florida partial, as "All states
        except FL" and "Tampa, FL" already did: in either order and either polarity the posting is CONDITIONAL with
        pay_tier_unknown in PRIMARY and NIGHT. Whole states still choose ("California, Florida", "Southeast (FL, GA)")."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("Outside the state of FL", "All states except for FL", "All states not including FL",
                      "Not including Florida", "All states excluding residents of Florida",
                      "All states with the exception of FL", "All states except the following: FL, NY", "Non-FL locations",
                      "All states (not FL)", "All states excl. FL", "Tampa, Florida", "Orlando, Florida", "Tampa, FL",
                      "South Florida", "Non-metro Georgia and Florida"):
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.",
                            f"All other locations: {other}. {label}: {mine}."):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        for pay, below in ((f"Florida: {high}. All other locations: {low}.", False),
                           (f"All other locations: {low}. Florida residents: {high}.", False),
                           (f"California, Florida: {low}. All other locations: {high}.", True),
                           (f"Southeast (FL, GA): {low}. All other locations: {high}.", True),
                           (f"South Carolina and Florida: {low}. All other locations: {high}.", True)):
            self.assert_decided(pay, below)
        # D149 (R-P3): an exclusion word anywhere in the pay block chooses no tier: "All states except TX" holds FL too,
        # so the "all other locations" reading this row was decided by (a rejection) was a guess
        self.assert_open(f"All states except TX: {high}. All other locations: {low}.")
        # the third correction (T-1): an exclusion word anywhere in its label leaves your state's tier open
        self.assert_open(f"Florida and all states except CA: {high}. California: {low}.")

    # The second correction of the batch (2026-11-09, D120): the independent check of the first (check-pay-1).
    def test_d120_the_lines_beside_a_range_give_its_period_but_never_another_items(self):
        """D120, second correction. The first read only the pay's period words on the lines beside a range (one beside a
        pay noun, or alone), so a period beside any other word, on the line after the range ("per month (before
        taxes)", "Monthly, plus bonus", "The range above is monthly.") or in the heading right above it ("Monthly Base",
        "Monthly (gross)", "Monthly OTE"), or a heading over a level or location line ("Monthly compensation" over
        "Remote, US"), was left out and a monthly $14,000 rejected as yearly in PRIMARY and NIGHT: 285e72d flagged them
        (D114). Those lines now give every period word but another item's. And another item's period (a stipend, a
        bonus, a commission, paid leave, a meeting) is never the pay's anywhere near the range, also when "paid" stands
        beside it ("A wellness stipend is paid monthly.") or its clause hangs on the item behind a comma or a bracket
        ("Wellness stipend (paid monthly)", "Bonuses, paid monthly"): a yearly salary under the floor still rejects
        (F1-04). The pay's own period beside an item stays the pay's: the item in a later clause, no item right before
        the comma ("Bonus eligible, paid monthly"), or the pay named too ("Base salary + bonus (monthly)")."""
        for pay in ("Salary range:\n$12,000 - $14,000\nper month (before taxes)", "Salary range:\n$12,000 - $14,000\nMonthly, plus bonus",
                    "Salary range:\n$12,000 - $14,000\n\nper month (before taxes)",   # across a blank line
                    "Salary range:\n$12,000 - $14,000\n(monthly, DOE)", "Compensation: $12,000 - $14,000\nper month before taxes",
                    "Salary range:\n$12,000 - $14,000\nThe range above is monthly.",
                    "Salary range:\n$12,000 - $14,000\nper month + benefits", "Salary range:\n$12,000 - $14,000\nMonthly, bonus eligible",
                    "Monthly Base\n$12,000 - $14,000",
                    "Monthly (gross)\n$12,000 - $14,000", "Per month, before taxes\n$12,000 - $14,000",
                    "Monthly OTE\n$12,000 - $14,000", "Monthly compensation\nRemote, US\n$12,000 - $14,000",
                    "Compensation\nMonthly\nLevel II\n$12,000 - $14,000",
                    "Salary and bonus are paid monthly.\nSalary range:\n$12,000 - $14,000",
                    "Salary range:\n$12,000 - $14,000\nPaid monthly via direct deposit",
                    "Bonus eligible, paid monthly\nSalary range:\n$12,000 - $14,000",
                    "Base salary + bonus (monthly)\n$12,000 - $14,000",
                    # the third correction (P-1): with no explicit unit or yearly label of its own, a range reads
                    # another item's period on its line or the lines beside it too
                    "Salary range: $50,000 - $55,000. Phone and internet reimbursement paid monthly.",
                    "Salary range: $50,000 - $55,000. Paid time off accrues monthly.",
                    "Monthly all-hands\nSalary range:\n$50,000 - $55,000", "Monthly 401(k) match\nSalary range:\n$50,000 - $55,000",
                    "Salary range:\n$50,000 - $55,000\nMonthly all-hands and quarterly offsites",
                    "- Monthly paid days off\nSalary range:\n$50,000 - $55,000",
                    "One paid volunteer day each month\nSalary range:\n$50,000 - $55,000",
                    "Salary range:\n$50,000 - $55,000\n$500 internet stipend, paid monthly",
                    "Salary range: $50,000 - $55,000. We add a $500 home-office budget. It is paid monthly."):
            self.assert_open(pay)
        # D123 (the fourth correction): the item rule is gone; another item's period beside a yearly salary under the
        # floor is a reading too (a needless CONDITIONAL accepted), and a yearly unit a monthly reading can't fit decides
        for pay in ("Annual base salary: $50,000 - $55,000. A wellness stipend is paid monthly.",
                    "Annual base salary: $50,000 - $55,000. Bonuses are paid monthly.",
                    "Monthly all-hands\nAnnual base salary: $50,000 - $55,000",
                    "Annual salary range:\n$50,000 - $55,000\n$500 internet stipend, paid monthly",
                    "Our contractors are paid monthly.\nAbout the role\nSalary range:\n$50,000 - $55,000"):
            self.assert_open(pay)
        # (D155, F1: "Monthly Base\n$60,000 - $70,000 per year" and "Salary range:\n$60,000 - $70,000/yr\nper month (before
        # taxes)" left: a period word of the block is a reading whatever the amount; PayTests.test_d155_...)

    def test_d120_the_pay_block_reads_its_own_line_and_beyond_the_lines_beside_a_range(self):
        """D120, second correction. With the two lines above and below a range read again, most of the first
        correction's cases are read twice; these are the pay block's alone. A heading beyond those lines, over "Hiring
        range:", "Base pay" or "USD" ("Hiring range (monthly)", "USD / Monthly", "Monthly (USD)"), and a pay-cycle
        sentence on the range's own line ("Weekly direct deposit.") give the range its period: CONDITIONAL
        pay_period_unknown in PRIMARY and NIGHT, as a monthly $14,000 (or a weekly $480) is above the floor and a yearly
        one under it. On the range's own line a period beside no pay word ("You will reconcile accounts monthly.") or
        after a comma ("competitive pay, monthly learning days") is not the pay's, and a paid-leave line ("Paid time
        off") speaks of no pay, so it ends the block as 285e72d's two-line read did: a yearly salary under the floor
        still rejects (F1-04)."""
        for pay in ("Monthly compensation\nHiring range:\nUSD\n$12,000 - $14,000", "Hiring range (monthly)\nBase pay\nUSD\n$12,000 - $14,000",
                    "USD / Monthly\nSalary range:\nUSD\n$12,000 - $14,000", "Monthly (USD)\nSalary range:\nUSD\n$12,000 - $14,000",
                    "Salary range: $400 - $480. Weekly direct deposit.",
                    # beyond a line the read above stops at, the pay block still crosses a "range" label to the heading
                    "Monthly compensation\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "Monthly\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "USD / Monthly\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "Monthly (USD)\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "Pay is monthly\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "Hiring range (monthly)\nThe hiring range for this role:\nUSD\n$12,000 - $14,000",
                    "Weekly direct deposit\nThe hiring range for this role:\nUSD\n$400 - $480",
                    # the third correction (P-1): every period word of the line of a range with no unit or yearly label
                    "We offer competitive pay, monthly learning days and more. Salary range: $50,000 - $55,000."):
            self.assert_open(pay)
        self.assert_decided("Annual base salary:\nUSD\n$50,000 - $55,000", True)   # no other period word: yearly
        for pay in ("Annual base salary: $50,000 - $55,000. You will reconcile accounts monthly.",   # D123: a reading
                    "Monthly compensation\nPaid time off\nSalary range:\n$50,000 - $55,000"):
            self.assert_open(pay)

    def test_d120_a_sibling_tiers_own_period_is_its_own(self):
        """D120, second correction. The first walked the pay block through its other ranges and read their period words
        too, so another tier's own period ("Florida: $50,000 - $55,000. California monthly salary: $9,000 - $9,500.",
        either order, or zones) made a clearly yearly tier under the floor a flag in PRIMARY and NIGHT; 285e72d rejected
        it. A sentence or line holding another range of the block now gives that range its words, except a heading in
        front of its place ("Monthly pay: California ..."), which is the block's; a period heading or sentence the
        tiers share is still read for each."""
        for pay in ("Florida: $50,000 - $55,000. California monthly salary: $9,000 - $9,500.",
                    "California monthly salary: $9,000 - $9,500. Florida: $50,000 - $55,000.",
                    "Florida: $50,000 - $55,000.\nCalifornia (paid monthly): $9,000 - $9,500.",
                    "California monthly salary: $13,000 - $14,000. Florida: $50,000 - $55,000."):
            self.assert_decided(pay, True)
        for pay in ("Monthly pay: California $13,000 - $14,000. Florida $12,000 - $13,000.",
                    "Monthly pay by location: California $13,000 - $14,000. Florida: $12,000 - $13,000.",
                    "Pay is monthly. California: $13,000 - $14,000. Florida: $12,000 - $13,000.",
                    "Monthly Base\nCalifornia: $13,000 - $14,000\nFlorida: $12,000 - $13,000",
                    "Florida: $12,000 - $13,000\nCalifornia: $60,000 - $70,000 per year\nTexas: $65,000 - $75,000 per year\n"
                    "Paid monthly"):
            self.assert_open(pay)

    def test_d120_every_exclusion_city_and_region_spelling_leaves_the_tier_open(self):
        """D120 (F6 parity), second correction. "excludes" was no exclusion, "Excl." (capitalised), "ex.", "exc.",
        "minus", "without" and "w/o" were none either, and a city or region of the state written other than "City,
        Statename" ("Tampa Florida", "Tampa (Florida)", "Florida - Tampa only", "Florida (Tampa office)", "Florida
        Panhandle", "Panhandle of Florida", "Tampa FL") named the whole state: the excluded or city tier became
        Florida's, a wrong pass or a wrong rejection by tier order and polarity. Each now marks Florida partial: in either
        order and polarity the posting is CONDITIONAL with pay_tier_unknown in PRIMARY and NIGHT. Whole states still
        choose, also when the first "City, Statename" read took them for a city ("Washington DC, Florida", "Remote US,
        Florida", "District of Columbia, Florida", "Puerto Rico, Florida") and under a region or tier heading over a
        bracketed list ("Southeast (FL, GA)", "Gulf Coast (FL, TX)", "Tier One (FL, TX)")."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("All states (excludes FL)", "Excludes FL", "All states, excludes Florida", "All states Excl. FL",
                      "US (ex. FL)", "All states exc. FL", "All states minus FL", "All states w/o FL",
                      "All states without Florida", "Tampa Florida", "Tampa (Florida)", "Florida - Tampa only",
                      "Florida (Tampa office)", "Florida Panhandle", "Panhandle of Florida", "Tampa FL", "FL Panhandle"):
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.",
                            f"All other locations: {other}. {label}: {mine}."):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        # (D155, F6: "Remote US, Florida" and "Florida (Remote)" left: a work arrangement chooses no tier)
        for label, below in (("Washington DC, Florida", True),
                             ("District of Columbia, Florida", False), ("Puerto Rico, Florida", True),
                             ("Southeast (FL, GA)", True), ("Gulf Coast (FL, TX)", False),
                             ("Florida residents", True), ("Florida-based", False),
                             ("Florida (FL)", True), ("Tier One (FL, TX)", True)):
            mine, other = (low, high) if below else (high, low)
            for pay in (f"{label}: {mine}. All other locations: {other}.", f"All other locations: {other}. {label}: {mine}."):
                self.assertEqual(home_pay_tier(pay, "FL")[0], "named", pay)
                self.assert_decided(pay, below)

    # The third correction of the batch (2026-11-09, D120): the second independent check (check-pay-2), closed by
    # invariants instead of more phrasings.
    def test_d120_an_item_rule_never_removes_a_plausible_pay_period(self):
        """D120 (F1, P-1). The second correction gave a period word to another item (benefits, a bonus, overtime, a
        401(k), commission) wherever the item was named in its clause, so "Paid monthly with benefits" under a bare
        "$12,000 - $14,000" lost its "monthly" and PRIMARY and NIGHT rejected a yearly $14,000; 285e72d flagged it. The
        item rule now sets a word aside only beside a range with an explicit unit of its own ("per year", "/yr",
        "annually") or an explicit yearly label ("Annual base salary:", "Annual salary range:"); otherwise every period
        word of its line and the lines beside it is a reading: CONDITIONAL pay_period_unknown. Since D123 (the fourth
        correction) the item rule is gone: beside any range every period word of its pay block is a reading."""
        for pay in ("Salary range:\n$12,000 - $14,000\nPaid monthly with benefits",
                    "Salary range:\n$12,000 - $14,000\nPaid monthly with an annual bonus",
                    "Salary range:\n$12,000 - $14,000\nPaid monthly and eligible for overtime",
                    "Salary range:\n$12,000 - $14,000\nMonthly with bonus",
                    "Salary range:\n$12,000 - $14,000\nPer month before taxes and benefits",
                    "Monthly base excluding bonus\n$12,000 - $14,000", "Monthly OTE including commission\n$12,000 - $14,000",
                    "Salary range:\n$12,000 - $14,000\nPaid monthly via ACH deposit",
                    "Salary range: $12,000 - $14,000\nWe offer great benefits. Paid monthly.",
                    "Monthly rate before overtime\n$12,000 - $14,000",
                    "Salary range: $12,000 - $14,000\nThis role is paid monthly and is bonus eligible.",
                    "Paid monthly with benefits\nSalary range: $12,000 - $14,000",
                    "Base salary: $12,000 - $14,000\nMonthly before bonuses",
                    "Salary range: $12,000 - $14,000. Paid monthly with benefits.",
                    "Salary range: $12,000 - $14,000. Paid monthly and eligible for a bonus.",
                    "Salary range: $12,000 - $14,000\nMonthly, plus a $500 sign-on bonus",
                    "Annual bonus eligible\nSalary range:\n$12,000 - $14,000\nPaid monthly with benefits",   # no yearly label
                    "Monthly stipend. Salary range: $12,000 - $14,000.",
                    # a yearly label anchors the item rule only: a pay cycle that is no item's is still a reading, also
                    # beside an item in a later clause, after words that are no item, or with the pay named too
                    "Annual base salary: $12,000 - $14,000. Paid monthly.",
                    "Annual salary range:\n$12,000 - $14,000\nper month + benefits",
                    "Annual salary range:\n$12,000 - $14,000\nMonthly, bonus eligible",
                    "Annual salary range:\n$12,000 - $14,000\nMonthly, plus bonus",
                    "Annual salary range:\n$12,000 - $14,000\nPaid monthly via direct deposit",
                    "Bonus eligible, paid monthly\nAnnual salary range: $12,000 - $14,000",
                    "Base salary + bonus (monthly)\nAnnual salary range: $12,000 - $14,000",
                    "Salary and bonus are paid monthly.\nAnnual salary range: $12,000 - $14,000"):
            self.assert_open(pay)
        # D123 (the fourth correction): the stipend, bonus and meeting guard is dropped with the item rule; beside
        # another period word even an explicit yearly salary under the floor flags (a needless CONDITIONAL accepted)
        for pay in ("Annual base salary: $50,000 - $55,000\nPaid monthly with benefits",
                    "Salary range: $50,000 - $55,000 per year\nMonthly team lunches",
                    "Salary range: $50,000 - $55,000/yr. A wellness stipend is paid monthly.",
                    "We make a monthly deposit to your HSA.\nAnnual base salary: $50,000 - $55,000"):
            self.assert_open(pay)

    def test_d120_another_tiers_label_is_its_own(self):
        """D120 (F1, P-2). The second correction's read above a range took up to two lines past the lines of the
        block's other ranges, so another tier's label ("California (monthly):", "California monthly salary:", "Zone B
        (monthly):", "California - paid monthly") gave its period to Florida's clearly yearly $55,000, and the read
        below took a label line ("California (paid monthly):") before its range: a flag in PRIMARY and NIGHT where
        285e72d rejected. Another tier's label and range lines are now passed over, never read, and a range's own label
        ("Florida (annual)", "Florida - paid yearly") is read as its own; zones on both sides of the floor with no home
        tier stay pay_tier_unknown, and a period heading over the tiers is still the block's."""
        for pay in ("California (monthly)\n$9,000 - $9,500\nFlorida (annual)\n$50,000 - $55,000",
                    "California (monthly):\n$9,000 - $9,500\nFlorida:\n$50,000 - $55,000",
                    "California (paid monthly):\n$9,000 - $9,500\nFlorida:\n$50,000 - $55,000",
                    "Florida:\n$50,000 - $55,000\nCalifornia (paid monthly):\n$9,000 - $9,500",
                    "California monthly salary:\n$9,000 - $9,500\nFlorida:\n$50,000 - $55,000",
                    "California monthly salary:\n$9,000 - $9,500\nFlorida salary:\n$50,000 - $55,000",
                    "Florida\n$50,000 - $55,000\nCalifornia - monthly\n$9,000 - $9,500",
                    "California (monthly)\n$9,000 - $9,500\n\nFlorida\n$50,000 - $55,000",
                    "California - paid monthly\n$9,000 - $9,500\nFlorida - paid yearly\n$50,000 - $55,000"):
            self.assert_decided(pay, True)
        for pay in ("Zone B (monthly):\n$9,000 - $9,500\nZone A:\n$50,000 - $55,000",   # no home tier: the zones straddle
                    "Monthly pay\nCalifornia:\n$13,000 - $14,000\nFlorida:\n$12,000 - $13,000",
                    "Florida: $12,000 - $13,000\nCalifornia: $13,000 - $14,000\nMonthly, plus benefits",
                    "Monthly compensation\nCalifornia: $60,000 - $65,000\nFlorida: $50,000 - $55,000"):
            self.assert_open(pay)
        self.assertIn("pay_tier_unknown", self.judged("Zone B (monthly):\n$9,000 - $9,500\nZone A:\n$50,000 - $55,000")[0][1])

    def test_d120_the_read_above_reaches_the_blocks_heading(self):
        """D120 (F1, P-3). The read above a range stopped after two lines, so a "Monthly compensation" heading over
        "Salary Range:" and "Remote, US" (or "Entry level", "Level II", "Remote (US)", "USD") was never read and PRIMARY
        and NIGHT rejected a yearly $14,000. It now goes on across short location, level, currency and label lines to
        the block's heading, also across the blank line a posting's HTML puts between every two lines: CONDITIONAL
        pay_period_unknown. Since D123 (the fourth correction) the read is blunt: up to six non-empty lines above,
        whatever they say."""
        for pay in ("Monthly compensation\nSalary Range:\nRemote, US\n$12,000 - $14,000",
                    "Monthly compensation\nRemote, US\nLevel II\n$12,000 - $14,000",
                    "Monthly compensation\nSalary Range:\nRemote (US)\nUSD\n$12,000-$14,000",
                    "Monthly compensation\nEntry level\nRemote, US\n$12,000 - $14,000",
                    "Monthly pay\nLocation: Remote\nLevel II\nUSD\n$12,000 - $14,000",
                    "Monthly compensation\n\nSalary Range:\n\nRemote, US\n\n$12,000 - $14,000",
                    "Monthly compensation\n\nRemote, US\n$12,000 - $14,000"):
            self.assert_open(pay)
        # (D155, F1: "Monthly compensation\nSalary Range:\nRemote, US\n$60,000 - $70,000/yr" left, now CONDITIONAL)
        # D123: up to twelve lines above and eight below are read whatever they say, a meeting's or a stipend's period
        # word too (the fifth correction: six and four missed a heading over six label lines and a pay cycle five lines
        # down, which ab62b46 flagged)
        for pay in ("Weekly team lunches\nMonthly pay:\n$1,500 - $1,700",
                    "We hold monthly all-hands meetings.\nSalary range:\nRemote, US\n$50,000 - $55,000",
                    "Monthly newsletter\nAbout the role\nSalary range:\n$50,000 - $55,000",
                    "Monthly compensation\nSalary Range:\nRemote, US\nUSD\nLevel II\nEntry level\nFull-time\n$12,000 - $14,000",
                    "Monthly compensation\n\nSalary Range:\n\nRemote, US\n\nUSD\n\nLevel II\n\nEntry level\n\nFull-time\n\n"
                    "$12,000 - $14,000",
                    "Salary range:\n$12,000 - $14,000\nUSD\nBase pay only\nThe pay range varies by level.\n"
                    "Pay ranges are reviewed often.\nPay is monthly."):
            self.assert_open(pay)
        # ... but no further: a period word thirteen non-empty lines up, or nine down, is no reading
        above, below = "\n".join(f"Line {i}" for i in range(11)), "\n".join(f"Line {i}" for i in range(8))
        self.assert_decided(f"Paid monthly\n{above}\nSalary range:\n$50,000 - $55,000", True)
        self.assert_decided(f"Salary range:\n$50,000 - $55,000\n{below}\nPaid monthly", True)

    def test_d120_only_a_plain_label_names_your_tier(self):
        """D120 (F6, T-1). Exclusions were listed by where they stood, so "All states (FL and NY excluded)", "aside
        from FL", "(no FL)", "(exclusions: FL)" and "Florida residents not eligible" named Florida's tier; "Orlando
        area, FL", "Tampa Bay area, Florida" (a California metro besides) and "Florida's Gulf Coast" named the whole
        state: a wrong pass or rejection by tier order and polarity. And the second correction took neutral words of
        a whole-state label ("Tier One (Florida)", "Florida (Fully Remote)", "Florida - Telecommuting") for cities and
        left a decided tier open. Your state's tier is now named only by a plain label: an exclusion word anywhere in
        it, an area word or the state's possessive leaves it open (pay_tier_unknown in PRIMARY and NIGHT); the neutral
        words decide as on 285e72d."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("All states (FL and NY excluded)", "All states (CA, FL, NY excluded)", "All states (FL residents excluded)",
                      "All states, Florida residents not eligible", "All states aside from FL", "All states (no FL)",
                      "All states (exclusions: FL)", "Orlando area, FL", "Tampa Bay area, Florida", "Florida's Gulf Coast",
                      "Florida (excluding Tampa)", "All states other than FL", "Florida residents ineligible",
                      "Greater Georgia and Florida"):   # a part of a state carries across the list after it
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.", f"All other locations: {other}. {label}: {mine}."):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        # (D155, F6: "Florida (Fully Remote)", "Florida (Home-based)", "Fully Remote, Florida", "Florida -
        # Telecommuting" and "Florida (Field-based)" left: a work arrangement chooses no tier; PayTests.test_d155_...)
        for label in ("Tier One (Florida)", "Florida (Entire State)", "Tier III (Florida)", "Florida (Exempt)",
                      "Florida (Current Employees)", "Florida (Non-exempt)"):
            for below in (True, False):
                mine, other = (low, high) if below else (high, low)
                for pay in (f"{label}: {mine}. All other locations: {other}.", f"All other locations: {other}. {label}: {mine}."):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "named", pay)
                    self.assert_decided(pay, below)
        # "Tampa Bay area" is no San Francisco: a California home's tier is the catch-all
        # D151 (P2): "Tampa" is no word of a plain label, so no tier is chosen by label (until D151: catch_all)
        self.assertEqual(home_pay_tier(f"Tampa Bay area, Florida: {high}. All other locations: {low}.", "CA")[0], "ambiguous")

    def test_d120_one_line_tiers_joined_by_commas_are_tiers(self):
        """D120 (F1 and F6, T-2). Tiers on one line joined by a comma were one label ("California: $60,000 - $70,000
        per year, Florida: $7,000 - $8,000, paid monthly."), so Florida's tier had no place and the first range
        decided: California's yearly $70,000 rejected a monthly $8,000, and "California: $90,000 - $100,000 per year,
        Florida: $60,000 - $70,000 per year." passed on California's range. A comma or a bar before a place label and
        ":" or " - " and a range now splits them as ". " and ";" do; a comma inside an amount, or before words that
        name no place ("OTE:"), never does."""
        for pay, below in (("California: $90,000-$100,000 per year, Florida: $60,000-$70,000 per year.", True),
                           ("California: $60,000-$70,000 per year, Florida: $90,000-$100,000 per year.", False),
                           ("California: $90,000-$100,000 per year, all other locations: $60,000-$70,000 per year.", True),
                           ("California: $90,000-$100,000 per year | Florida: $60,000-$70,000 per year", True),
                           ("Florida - $90,000-$100,000 per year, California - $60,000-$70,000 per year.", False),
                           ("California: $90,000-$100,000 per year, Florida, Georgia: $60,000-$70,000 per year.", True)):
            self.assert_decided(pay, below)
        # D123: two ranges on both sides of the floor and no tier for your state (base and OTE here) flag
        self.assert_open("Base salary: $60,000 - $70,000 per year, OTE: $90,000 - $100,000 per year.")
        for pay in ("California: $60,000-$70,000 per year, Florida: $7,000-$8,000. Paid monthly.",
                    "California: $60,000-$70,000 per year, Florida: $7,000-$8,000, paid monthly.",
                    "California - $60,000-$70,000 per year, Florida - $7,000-$8,000, paid monthly."):
            self.assert_open(pay)
        self.assertEqual(home_pay_tier("California: $90,000-$100,000 per year, Florida: $60,000-$70,000 per year.", "FL")[0],
                         "named")

    # The fourth correction of the batch (2026-11-10, D120 under D123: blunt fail-safe rules): the third check of the
    # pay readings (check-pay-r3) found wrong rejections again, two of them introduced by the third correction.
    def test_d120_d123_a_range_is_yearly_only_by_its_own_unit_with_no_other_period_near(self):
        """D120 (F1) under D123. The third correction let a yearly word that was no unit of the range decide it: "annual"
        of an annual bonus right after it ("$12,000 - $14,000 (annual bonus eligible)", "... annually adjusted for
        inflation"), or a yearly heading or label above it ("Annual raises", "Annual planning cycle", "Salary range,
        reviewed annually:") set the item rule on, and a heading beyond a line that was no label ("Tampa, FL",
        "Business Systems Analyst") or a period word far from the range was never read: PRIMARY and NIGHT rejected a
        monthly $14,000 as a yearly one. The item rule and the label anchoring are gone. A range is decided yearly only
        by an explicit yearly unit right after it ("per year", "/yr", "annually.") with no other period word in its pay
        block (its line, six lines above, four below); else every period word there is a reading: CONDITIONAL
        pay_period_unknown, never a rejection. A clearly yearly range under the floor with no other period word near it
        still rejects (F1-04), and an explicit monthly unit still decides."""
        for pay in ("Monthly base: $12,000 - $14,000 (annual bonus eligible)",
                    "Salary range: $12,000 - $14,000 annual bonus eligible. Paid monthly.",
                    "Salary range (with annual merit increases): $12,000 - $14,000. Paid monthly with benefits.",
                    "Annual raises and promotions\nSalary range:\n$12,000 - $14,000\nPaid monthly with benefits",
                    "Annual planning cycle\nSalary range:\n$12,000 - $14,000\nPaid monthly with benefits",
                    "Annual Enrollment\n\nSalary range:\n\n$12,000 - $14,000\n\nPaid monthly with benefits",
                    "Monthly salary range: $12,000 - $14,000 (annual bonus not included)",
                    "Monthly base pay\n$12,000 - $14,000 annual bonus target 10%",
                    "Salary range: $12,000 - $14,000 (annual cost of living adjustments). Paid monthly with benefits.",
                    "Monthly salary: $8,000 - $9,000 (annual bonus eligible)", "Monthly pay range: $8,000 - $9,000 annual incentive plan",
                    "Monthly salary\n\n$8,000 - $9,000 (annual bonus eligible)",
                    "Monthly base: $8,000 - $9,000 annually adjusted for inflation",
                    "Paid monthly. Salary range: $8,000 - $9,000 annual bonus",
                    "Annual merit cycle\nPay range\n$12,000 - $14,000\nPaid monthly with benefits",
                    "Salary range, reviewed annually: $12,000 - $14,000. Paid monthly with benefits.",
                    "Monthly compensation\nTampa, FL\nSalary Range:\n$12,000 - $14,000",
                    "Monthly compensation\n\nHybrid - Orlando\n\nSalary Range:\n\n$12,000 - $14,000",
                    "Pay (monthly)\nClaims Department\nSalary Range:\n$12,000 - $14,000",
                    "Salary range: $12,000 - $14,000. Location: remote. Team: claims. Paid per month.",
                    "Salary range:\n$12,000 - $14,000\nLocation: Remote\nTeam: Claims\nAll salaries are paid per month.",
                    "Per month\n\nSenior Business Systems Analyst\n\nSalary Range:\n\n$12,000 - $14,000",
                    "Monthly compensation\nBusiness Systems Analyst\nSalary Range:\n$12,000 - $14,000",
                    "Compensation is per month.\n\nThe range below reflects base pay.\n\nSalary range:\n\n$12,000 - $14,000",
                    "Salary range: $12,000 - $14,000\nPaid at $1,000/mo installments"):   # "/mo" is a period word too
            self.assert_open(pay)
        for pay, below in (("$50,000 - $55,000 per year", True), ("Salary range: $60,000 - $70,000/yr", True),
                           ("Salary range: $50,000 - $55,000 annually.", True), ("$50,000 - $55,000 a year", True),
                           ("Annual base salary: $50,000 - $55,000", True),
                           ("$50,000 - $55,000 annual base salary", True),
                           ("Salary range: $8,000 - $9,000 per month (annual bonus eligible)", False),
                           ("Salary: $10,000 - $12,000/mo before taxes", False)):
            self.assert_decided(pay, below)

    def test_d120_d123_unnamed_tiers_on_both_sides_of_the_floor_flag(self):
        """D120 (F6) under D123. Two or more ranges decided by the first one whenever a range's label named no place: a
        "Remote" tier ("California: ... Remote (US): ..."), a label after or around the range ("$90,000 - $100,000
        (CA), $60,000 - $70,000 (FL)", "... in California, ... in Florida"), tiers joined with no colon ("California
        $90,000-$100,000 | Florida $60,000-$70,000", "CA: ... / FL: ..."), and an excluded tier beside a no-place one
        ("All states except Florida: ... Remote: ..."): a wrong pass or rejection in PRIMARY and NIGHT, as on live.
        When the text holds two ranges or more that are no one range repeated, no tier names your state plainly and
        none is "all other locations" beside named ones, and the ranges fall on both sides of the floor, it is
        pay_tier_unknown; levels and roles flag too (a needless CONDITIONAL accepted). On one side of the floor the
        ranges decide, and a plainly named tier for your state, or the "all other locations" one, decides."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for pay in (f"California: {high}. Remote (US): {low}.", f"California: {high}. Remote: {low}.",
                    f"California: {high}, Remote, US: {low}", f"California, New York (on-site): {high}. Remote (all US): {low}.",
                    f"New York City: {high}. Remote: {low}.", f"On-site (California): {high}. Remote (US): {low}.",
                    f"California: {high}. Remote US: {low}.", f"California: {high}. US Remote: {low}.",
                    "California $90,000 - $100,000, Florida $60,000 - $70,000 per year",
                    "$90,000 - $100,000 (CA), $60,000 - $70,000 (FL)",
                    "California $90,000-$100,000 | Florida $60,000-$70,000", "CA: $90,000 - $100,000 / FL: $60,000 - $70,000",
                    "$90,000 - $100,000 (California) and $60,000 - $70,000 (Florida)",
                    "$90,000 - $100,000 in California, $60,000 - $70,000 in Florida",
                    f"All states except Florida: {low}. Remote: {high}.", f"All states except Florida: {high}. Remote: {low}.",
                    f"Outside Florida: {high}. Remote (US): {low}.", f"Outside Florida: {low}. Remote (US): {high}.",
                    f"All states (FL excluded): {high}. Contractors: {low}.",
                    f"Level I: {low}. Level II: {high}."):
            for failures, flags in self.judged(pay):
                self.assertFalse({"below_pay_floor", "below_night_pay_floor"} & failures, pay)
                self.assertIn("pay_tier_unknown", flags, pay)
        # (D155, F6: "New York: ... Remote - Florida: ..." and "California: ... Remote (all other states): ..." left: a
        # work arrangement chooses no tier, so they straddle the floor: pay_tier_unknown)
        for pay, below in (("Remote (US): $60,000 - $70,000 per year. Contractors: $65,000 - $75,000 per year.", True),
                           ("Remote (US): $90,000 - $100,000 per year. Contractors: $95,000 - $105,000 per year.", False),
                           ("$60,000 - $70,000 per year. Also: $60,000 - $70,000 per year.", True)):   # one range repeated
            self.assert_decided(pay, below)

    # D149 (Codex Pass 2 of CR-2026-11-05-COMBINED, findings F1 and F6, reproductions A and E): structural rules
    def test_d149_equal_amounts_with_unequal_periods_are_two_tiers(self):
        """D149 (A, R-P1). "Zone A: $7,000 - $8,000 per year. Zone B: $7,000 - $8,000 per month." was one tier (equal
        amounts), and PRIMARY and NIGHT rejected on the yearly one. Two ranges are one tier only when their amounts and
        their settled periods are equal: here two tiers, and as the posting doesn't say which is yours, pay_tier_unknown
        (CONDITIONAL). Named California and Florida, the Florida tier (monthly, $96,000 a year) decides: no rejection.
        Controls: a plainly named yearly tier for your state under the floor still rejects; one range repeated with
        its period stays one tier and decides."""
        for pay in ("Zone A: $7,000 - $8,000 per year. Zone B: $7,000 - $8,000 per month.",
                    "Zone A: $7,000 - $8,000 per month. Zone B: $7,000 - $8,000 per year.",
                    "Zone A: $7,000 - $8,000 per year.\nZone B: $7,000 - $8,000 per month.",
                    "Level I: $7,000 - $8,000 annually. Level II: $7,000 - $8,000 monthly.",
                    "Zone A: $7,000 - $8,000 per year. Zone B: $7,000 - $8,000 per week.",
                    "Zone A: $7,000 - $8,000 per year. Zone B: $7,000 - $8,000."):   # B's period unsettled
            self.assertNotEqual(home_pay_tier(pay, "FL")[0], "single", pay)
            self.assert_open(pay)
        for pay, below in (("California: $7,000 - $8,000 per year. Florida: $7,000 - $8,000 per month.", False),
                           ("Florida: $7,000 - $8,000 per month. California: $7,000 - $8,000 per year.", False),
                           ("Florida: $7,000 - $8,000 per year. California: $7,000 - $8,000 per month.", True),
                           ("$60,000 - $70,000 per year. Also: $60,000 - $70,000 per year.", True)):
            self.assert_decided(pay, below)

    def test_d149_a_period_word_the_reader_cant_size_is_never_a_yearly_guess(self):
        """D149 (A, R-P2). "Compensation, per calendar month: $12,000 - $14,000" was read as yearly ("per" had to stand
        right before the unit) and PRIMARY and NIGHT rejected it, where "per month" flagged. Any time word of the pay
        block is now a reading (a bare hour, day, week, month or fortnight in any wording), and a pay cycle the reader
        can't size (a quarter, "bimonthly", a pay period, cycle, check or day, a paycheck, payroll, an installment)
        reads as every cycle: pay_period_unknown, never an annual guess. Controls: a plainly yearly range under the
        floor, alone or with a bare "year" (an experience line), still rejects."""
        for pay in ("Compensation, per calendar month: $12,000 - $14,000",
                    "Compensation (calendar month): $12,000 - $14,000",
                    "Base pay, each full month: $12,000 - $14,000",
                    "Compensation, per pay period: $12,000 - $14,000",
                    "Compensation, per payroll cycle: $12,000 - $14,000",
                    "Pay per paycheck: $3,000 - $3,400",
                    "Compensation per fortnight: $3,000 - $3,400",
                    "Quarterly base: $12,000 - $14,000",
                    "Bimonthly salary: $3,000 - $3,400",
                    "Salary range: $12,000 - $14,000, paid in 12 equal installments",
                    "Pay: $12,000 - $14,000 for each 4-week period",
                    "Rate: $3,000 - $3,400 per calendar week",
                    "Compensation, per term: $12,000 - $14,000", "Pay per semester: $12,000 - $14,000",
                    "Compensation per assignment: $12,000 - $14,000", "Compensation (mo.): $12,000 - $14,000"):
            self.assert_open(pay)
        for pay in ("$50,000 - $55,000 per year", "Annual base salary: $50,000 - $55,000",
                    "Compensation, per calendar year: $50,000 - $55,000",
                    "Salary range: $50,000 - $55,000 per year, set per company policy",
                    "Requires 5+ years of claims experience.\nSalary range: $50,000 - $55,000 per year"):
            self.assert_decided(pay, True)

    def test_d149_an_exclusion_anywhere_in_the_pay_block_chooses_no_tier(self):
        """D149 (E, R-P3). "All states except\nFL: $90,000 - $100,000 per year. All other locations: $60,000 - $70,000
        per year." chose the high tier for FL: the line break cut the exclusion from the label, and "Save for FL" was no
        exclusion word: a silent pass in PRIMARY and NIGHT. An exclusion word anywhere in the pay block (from 400
        characters before its first range to the end of its last range's line, across line breaks) now leaves no tier
        chosen by its label: pay_tier_unknown, in either tier order and polarity. Controls: a tier plainly naming your
        state with no exclusion near it still decides, either side of the floor."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("All states except\nFL", "All states except\n\nFL", "Save for FL", "All states save for FL",
                      "All states saving FL", "All states excluding\nFlorida", "All states other than FL",
                      "All states other than\nFL", "All locations, with the exception of\nFL", "All states apart from\nFL",
                      "US (besides\nFL)", "All states but not\nFL", "Everywhere except TX", "All states except TX",
                      "All states, not\nFL", "All states (no\nFL)", "All states without\nFL"):   # the label's own words
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.",
                            f"All other locations: {other}.\n{label}: {mine}."):
                    with self.subTest(pay=pay):
                        self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                        self.assert_open(pay)
        for pay, below in ((f"Florida: {high}. All other locations: {low}.", False),
                           (f"Texas: {high}.\nFlorida: {low}.", True),
                           (f"Texas: {low}.\nFlorida: {high}.", False)):
            self.assert_decided(pay, below)

    # D151 (red team round 1 on 4199f10, P1 and P2; the owner's allow-style rules under D149)
    def test_d151_a_range_under_20000_is_yearly_only_by_its_own_yearly_unit(self):
        """D151 (P1). "Salary: $12,000 - $14,000 p.m." (and "pcm", "/mth", "mthly", "every 30 days", "/ m") was read as
        yearly and rejected in PRIMARY and NIGHT: no reader knew the word. A range under $20,000 is yearly only by its own
        yearly unit ("per year", "/yr", "a year", "annually", "annual", "yearly", "per annum", "p.a."; "salary of" is
        none); otherwise every pay cycle is a reading beside the yearly one: pay_period_unknown, never a rejection on a
        yearly guess (until D151 a bare $14,000, or a monthly heading over $6,000, decided). Controls: the same range with
        its own yearly unit, a plainly yearly-sized bare range, and an explicit monthly unit still decide."""
        for pay in ("Salary: $12,000 - $14,000 p.m.", "Salary: $12,000 - $14,000 pcm", "Salary: $12,000 - $14,000/mth",
                    "Salary: $12,000 - $14,000 mthly", "Salary: $12,000 - $14,000 every 30 days",
                    "Salary: $12,000 - $14,000 / m", "Salary range: $12,000 - $14,000", "Salary of $12,000 - $14,000",
                    "Compensation: $12,000 - $14,000 per head", "Salary: $8,000 - $9,500 gross",
                    "Monthly salary: $5,000 - $6,000.", "Zone A: $50,000 - $55,000. Zone B monthly salary: $4,000 - $4,500.",
                    "Zone B monthly salary:\n$4,000 - $4,500\nZone A:\n$50,000 - $55,000",
                    "Zone B (monthly):\n$4,000 - $4,500\nZone A:\n$50,000 - $55,000",
                    # $20,000 or more: the period words of the block still give the readings (no D151 rule needed)
                    "Salary: $30,000 - $35,000, paid semi-monthly", "Compensation per pay period: $30,000 - $35,000",
                    "Compensation, per calendar month: $30,000 - $35,000", "Compensation per term: $30,000 - $35,000"):
            self.assert_open(pay)
        for pay, below in (("Salary: $12,000 - $14,000 per year", True), ("Salary: $12,000 - $14,000 annually", True),
                           ("Salary: $12,000 - $14,000/yr", True), ("Salary: $12,000 - $14,000 a year", True),
                           ("Salary: $12,000 - $14,000 yearly", True), ("Salary: $12,000 - $14,000 p.a.", True),
                           ("Salary: $12,000 - $14,000 per annum", True), ("Salary range: $50,000 - $55,000", True),
                           ("$50,000 - $55,000 per year", True), ("Salary range: $60,000 - $70,000 annually", True),
                           ("Base salary: $50,000 - $55,000/yr", True), ("Annual salary: $60,000 - $70,000", True),
                           ("$60,000 - $70,000 per annum", True), ("Salary: $5,000 - $6,000 per month", True),
                           ("Salary: $12,000 - $14,000 per month", False)):
            self.assert_decided(pay, below)

    def test_d151_a_tier_is_chosen_by_its_label_only_when_every_label_is_plain(self):
        """D151 (P2). "All states less FL", "sans FL", "not incl. FL", "\u2260 FL" chose the labelled tier for FL (no
        exclusion list held the word): a wrong pass or rejection in PRIMARY and NIGHT. A tier is chosen by its label only
        when every tier's label holds only places and the words of a plain label (_LABEL_WORDS); any other word or symbol
        chooses none: pay_tier_unknown when the ranges straddle the floor, either polarity and order. Controls: plain
        labels ("Florida residents", "Remote - Florida", "Tier One (FL, TX)", the Oscar sentence) still decide."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("All states less FL", "All states sans FL", "All states, not incl. FL", "All states \u2260 FL",
                      "All states bar FL", "All states w/o FL", "All states lacking FL", "Florida (all cities)",
                      "Premium (FL, TX)", "All states (ex-FL)", "All states \u2216 FL", "All states but FL"):
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.",
                            f"All other locations: {other}. {label}: {mine}."):
                    with self.subTest(pay=pay):
                        self.assertNotIn(home_pay_tier(pay, "FL")[0], ("named", "catch_all"), pay)
                        self.assert_open(pay)
        # an exclusion above plain labels, beyond the line above them: the pay block's exclusion rule (D149 R-P3) holds
        for mine, other in ((high, low), (low, high)):
            self.assert_open(f"Florida residents are excluded from these ranges.\n\nPay ranges:\nTexas: {other}\n"
                             f"All other locations: {mine}")
        for label, below in (("Florida residents", True), ("Florida - Statewide", False), ("Tier One (FL, TX)", True),
                             ("Florida", False)):
            mine, other = (low, high) if below else (high, low)
            self.assert_decided(f"{label}: {mine}. All other locations: {other}.", below)
        self.assert_decided(OSCAR_PLAIN, True)
    # D152 (the delta hand check of d7c1559): pay readings that P1 (D151) masked for ranges under $20,000, and tier
    # spellings the plain-label rule lets through, each paired with the planted defect it catches
    def test_d152_readings_and_labels_the_d151_rules_do_not_mask(self):
        """D152. Under D151 a range under $20,000 with no yearly unit flags whatever its block says, and a tier label
        with a word no plain label holds chooses nothing, so five planted defects of the older readings had no test left.
        Each input here still turns on that older reading: a period word after a blank line ("Paid monthly"),
        "fortnightly" and "every two weeks" (biweekly) beside a range of $20,000 or more; a bare daily-sized amount
        ($600 - $900, read per day too); a region or part of the state before a state code ("South FL", "Greater FL",
        plain label words, so only the qualifier keeps the tier open); and an exclusion on a long line right above the
        first tier, beyond the pay block's 400 characters ("Florida residents excluded ..."). Each is CONDITIONAL in
        PRIMARY and NIGHT."""
        for pay in ("Salary range: $30,000 - $35,000\n\nPaid monthly.",   # the line after a blank line
                    "Salary range:\n- $30,000 - $35,000\n- Paid monthly",   # the next list item
                    "Salary: $30,000 - $35,000, paid fortnightly", "Salary: $30,000 - $35,000, paid every two weeks",
                    "Rate: $600 - $900"):   # a daily-sized amount: per day it clears the floor
            self.assert_open(pay)
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("South FL", "Greater FL", "Northern FL"):
            for mine, other in ((high, low), (low, high)):
                pay = f"{label}: {mine}. All other locations: {other}."
                self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                self.assert_open(pay)


    def test_d152_every_exclusion_label_word_still_counts(self):
        """D152 (the exclusion-label family of the hand check). Under D149 and D151 an exclusion word in a tier's label
        is also read by the pay block's exclusion rule and the plain-label rule, so the label's own exclusion list
        (_EXCLUSION) and the abbreviations a tier label is not cut at ("excl.", "ex.", "Excl.") had no test left. Two
        inputs still turn on them, in PRIMARY and NIGHT: an exclusion on a long line right above the first tier, beyond
        the pay block's 400 characters (read by the label rule's line above only), one per word of the list; and a tier
        label holding a period word before the abbreviation ("All locations monthly excl. FL"): cut there, the tier's
        own words would start at the state and hand "monthly" to the other tier, dropping its daily reading."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        filler = " Our team values collaboration, growth and plain language in every document we write." * 5
        for lead in ("Florida residents excluded from the ranges below.", "The ranges below exclude Florida.",
                     "This list excludes Florida.", "With the exception of Florida, the ranges below apply.",
                     "All ranges below excl Florida.", "All ranges below exc Florida.", "All ranges below ex Florida.",
                     "Non-Florida residents only for the ranges below.", "Florida is not part of the ranges below.",
                     "All ranges below minus Florida.", "All ranges below without Florida.", "All ranges below w/o Florida.",
                     "Aside from Florida, these ranges apply.", "Ranges below: no Florida.", "Exclusions: Florida.",
                     "Florida residents ineligible for the ranges below."):
            for mine, other in ((high, low), (low, high)):
                pay = f"{lead}{filler}\nFlorida: {mine}\nAll other locations: {other}"
                with self.subTest(pay=pay[:60]):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        for abbreviation in ("excl.", "Excl.", "ex.", "Ex.", "exc."):
            with self.subTest(abbreviation=abbreviation):
                self.assert_open(f"Zone A: $600 - $900\nAll locations monthly {abbreviation} FL: $1,000 - $1,200")


    def test_d152_every_place_qualifier_of_a_label_still_counts(self):
        """D152 (the place-qualifier family of the hand check). Under D151 a tier is chosen by its label only when the
        label holds plain words, so a city before or after the state ("Tampa, Florida", "Florida (Tampa office)") or a
        part of the state ("Florida Panhandle") was already left open by the plain-label rule, and the readers that tell
        a part or a city of a state from the whole state had no test left. They still decide a label of plain words
        only: a part of the state named by a plain word ("Florida Region", "Coast of Florida", "Coast, FL", "FL Coast"),
        and a city named by a word that is a plain-label word but no tier word ("Opening, Florida", "Opening (Florida)",
        "Florida - Opening"): CONDITIONAL in PRIMARY and NIGHT. Controls: "Opening (FL, TX)" and "Role (Florida, Texas)"
        list whole states in brackets and still decide. Two readings too: a pay heading in front of the first tier's
        place ("Monthly pay: California ...") is read by the other tiers (ranges of $20,000 or more, so the D151 rule
        for small ranges doesn't mask it), and a named tier is read alone as well as in its siblings' unit (a daily-sized
        Florida tier beside a yearly California one)."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("Opening, Florida", "Role, Florida",                    # a city, state name written out
                      "FL Region", "FL Coast", "Opening FL",                       # a code beside a part or a city
                      "Florida Region", "Florida Coast",                            # a part after the state
                      "Region of Florida", "Coast of the state of Florida",         # a part of the state
                      "Coast, FL", "Region, Florida", "Coast FL",                   # a part before the state
                      "Opening Florida", "Opening (Florida)", "Opening (Florida)",   # a city before the state
                      "Florida - Opening", "Florida (Opening)"):                   # a city after the state
            for mine, other in ((high, low), (low, high)):
                pay = f"{label}: {mine}. All other locations: {other}."
                with self.subTest(pay=pay):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        for label in ("Opening (FL, TX)", "Role (Florida, Texas)"):   # whole states in brackets: they decide
            for below in (True, False):
                mine, other = (low, high) if below else (high, low)
                pay = f"{label}: {mine}. All other locations: {other}."
                self.assertEqual(home_pay_tier(pay, "FL")[0], "named", pay)
                self.assert_decided(pay, below)
        self.assert_open("Monthly pay: California $30,000 - $35,000. Florida $25,000 - $28,000.")
        self.assert_open("California: $60,000 - $70,000 per year. Florida: $600 - $900.")
        # a bay area of another state named by a plain-label word is no San Francisco: a California home's tier is
        # the catch-all
        self.assertEqual(home_pay_tier(f"Opening Bay area, Florida: {high}. All other locations: {low}.", "CA")[0],
                         "catch_all")

    def test_d152_the_pay_block_reach_holds_for_ranges_of_20000_or_more(self):
        """D152. The pay block reads twelve non-empty lines above a range and eight below (D123); its tests used ranges
        under $20,000, which D151 flags whatever their block says. With ranges of $20,000 or more a period word three or
        five lines below, or a heading seven lines above over six label lines, still makes the posting CONDITIONAL in
        PRIMARY and NIGHT."""
        for pay in ("Salary range: $30,000 - $35,000\nLocation: Remote\nTeam: Claims\nPaid monthly.",
                    "Salary range: $30,000 - $35,000\nLocation: Remote\nTeam: Claims\nLevel: II\nStatus: Full-time\n"
                    "Paid monthly.",
                    "Monthly compensation\nLocation: Remote\nTeam: Claims\nLevel: II\nStatus: Full-time\n"
                    "Department: Operations\nSalary Range:\n$30,000 - $35,000"):
            self.assert_open(pay)

    # D155 (Codex Pass 3 of CR-2026-11-05-COMBINED, F1 and F6): blunt fail-safes, no further Codex pass
    def test_d155_an_explicit_cycle_word_is_a_reading_whatever_the_amount(self):
        """D155 (F1). "Compensation: $60,000 - $65,000 per quarter" and "Monthly compensation $60,000 - $65,000"
        hard-rejected in PRIMARY and NIGHT: an amount ceiling (no monthly pay of $65,000) dropped the explicit cycle word
        and left a yearly guess. A period word of the range's own pay block is kept whatever the amount (only a sibling
        tier's unit must fit it): pay_period_unknown. Controls: a plainly yearly single range under the floor still
        rejects, and an explicit unit right after the range ("per hour, paid weekly") still decides."""
        for pay in ("Compensation: $60,000 - $65,000 per quarter", "Monthly compensation $60,000 - $65,000",
                    "Compensation: $50,000 - $55,000 per quarter", "Quarterly pay: $60,000 - $65,000",
                    "Salary: $60,000 - $65,000 paid every pay period", "Compensation: $60,000 - $65,000 per month",
                    "Weekly salary: $60,000 - $65,000", "Monthly Base\n$60,000 - $70,000 per year",
                    "Monthly compensation\nSalary range: $60,000 - $70,000 per year",
                    "Salary range:\n$60,000 - $70,000/yr\nper month (before taxes)",
                    "Monthly compensation\nSalary Range:\nRemote, US\n$60,000 - $70,000/yr",
                    "This full-time role works 40 hours per week with a base salary range of $60,000 - $70,000."):
            self.assert_open(pay)
        for pay, below in (("$50,000 - $55,000 per year", True), ("Salary range: $60,000 - $70,000 annually", True),
                           ("Base salary: $50,000 - $55,000/yr", True), ("Annual salary: $60,000 - $70,000", True),
                           ("$40 - $50 per hour, paid weekly", False), ("$90,000 - $100,000 per year", False)):
            self.assert_decided(pay, below)

    def test_d155_a_work_arrangement_in_the_pay_block_chooses_no_tier(self):
        """D155 (F6). "Florida (on-site only): $90,000 - $100,000 per year. All other locations: $60,000 - $70,000 per
        year." chose the Florida tier for a remote Florida profile, and a tier note on the next line ("Tier 1 excludes
        remote positions. Remote positions use Tier 2.") was never read. A work arrangement or eligibility term anywhere
        in the pay block (400 characters before the first range to eight non-empty lines after the last: remote,
        on-site, hybrid, in-office, office, only, eligible, excludes, positions, role type ...) chooses no tier by label:
        pay_tier_unknown in PRIMARY and NIGHT, either polarity. The full Oscar wording ("You are also eligible for
        monthly vacation accrual.") now flags too (needless). Controls: a plain home-state tier still decides."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for label in ("Florida (on-site only)", "Florida (onsite)", "Florida (hybrid)", "Florida (in-office)",
                      "Florida office", "Florida only", "Florida (remote eligible)", "Remote - Florida", "Florida (Remote)",
                      "Remote US, Florida", "Florida (Fully Remote)", "Florida (Home-based)", "Fully Remote, Florida",
                      "Florida - Telecommuting", "Florida (Field-based)"):
            for mine, other in ((high, low), (low, high)):
                for pay in (f"{label}: {mine}. All other locations: {other}.",
                            f"All other locations: {other}.\n{label}: {mine}."):
                    with self.subTest(pay=pay):
                        self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                        self.assert_open(pay)
        for note in ("Tier 1 excludes remote positions. Remote positions use Tier 2.", "Role type: on-site.",
                     "These ranges apply to office-based roles.", "Only hybrid employees are eligible for Tier 1."):
            for mine, other in ((high, low), (low, high)):
                pay = f"Tier 1 (Florida): {mine}. Tier 2 (all other locations): {other}.\n{note}"
                with self.subTest(pay=pay):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)
        self.assert_open(f"New York: {high}. Remote - Florida: {low}.")
        self.assert_open(f"California: {high}. Remote (all other states): {low}.")
        self.assert_open(OSCAR_TIERS)
        for pay, below in ((f"Florida: {low}. All other locations: {high}.", True),
                           (f"Florida: {high}. All other locations: {low}.", False),
                           (f"Texas: {high}.\nFlorida: {low}.", True), (OSCAR_PLAIN, True)):
            self.assert_decided(pay, below)

    def test_d155_an_exclusion_word_beyond_the_labels_still_chooses_no_tier(self):
        """D155 hand check of 489469b. The pay block's exclusion rule (D149 R-P3) had no test left: its inputs held
        "excluded", which the D155 arrangement rule (F6) reads too. An exclusion word with no arrangement or eligibility
        term, two lines above plain tier labels (beyond the line-above rule), still leaves no tier chosen by its label:
        pay_tier_unknown in PRIMARY and NIGHT, either polarity. Each lead holds one exclusion word of _BLOCK_EXCLUSION."""
        high, low = "$90,000 - $100,000 per year", "$60,000 - $70,000 per year"
        for lead in ("Florida is outside these ranges.", "Except for Florida, the ranges below apply.",
                     "The ranges below apply to all states other than Florida.", "Besides Florida, these ranges apply.",
                     "Apart from Florida, these ranges apply.", "Aside from Florida, these ranges apply.",
                     "These ranges apply to all states, save Florida.", "These ranges apply to all states but not Florida.",
                     "These ranges apply to all states minus Florida.", "These ranges apply barring Florida.",
                     "These ranges are not in effect for Florida.", "These ranges excl. Florida."):
            for mine, other in ((high, low), (low, high)):
                pay = f"{lead}\n\nPay ranges:\nTexas: {other}\nAll other locations: {mine}"
                with self.subTest(pay=pay):
                    self.assertEqual(home_pay_tier(pay, "FL")[0], "ambiguous", pay)
                    self.assert_open(pay)

class HeadingTests(unittest.TestCase):
    """2026-11-04: Headway's reformatted posting was parked as requirements_unclear because its requirements heading,
    "You'd be a great fit if…", was seven words and not on the list, and everything after "About the Team & Role" was
    read as company text."""
    HEADWAY_STYLE = ("Synth Health is building the most meaningful experience of your career.\n"
                     "About the Team & Role\n- You will lead our outsourced support partners\n"
                     "You Will\n- Run weekly reviews with each vendor\n"
                     "You’d be a great fit if…\n- You've worked in contact center or CX operations for 4+ years\n"
                     "- Strong written communication\n"
                     "COMPREHENSIVE BENEFITS:\n- Medical, dental and vision\n")
    UNITE_STYLE = ("What you'll do:\n- Lead customer implementations\n"
                   "You're a great fit for this role if :\n- 3+ years of experience in software implementation\n"
                   "- Excel experience\n")

    def material(self, text):
        result = extract(text, POLICY)
        return result, {(r.category, r.years) for r in result.requirements if r.requirement_type != "responsibility"}

    def test_the_great_fit_family_is_a_requirements_heading(self):
        result, found = self.material(self.HEADWAY_STYLE)
        self.assertIn("required", result.sections)
        self.assertIn(("years_experience", 4.0), found)
        result, found = self.material(self.UNITE_STYLE)
        self.assertIn(("years_experience", 3.0), found)


class NormalizeTests(unittest.TestCase):
    def test_greenhouse_escaped_html_and_invisible_chars(self):
        raw = "&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;SQL\u200b&amp;nbsp;skills&lt;/li&gt;&lt;/ul&gt;"
        self.assertEqual(html_to_text(raw), "Requirements\n\n- SQL skills")

    def test_d149_unicode_horizontal_spaces_become_ascii_spaces(self):
        """D149 R-Y3 (Codex Pass 2 D): "as&#8239;Product Owner" kept U+202F through the HTML helper. Every Unicode
        horizontal space (and tab) is an ASCII space before any parsing; newlines are kept."""
        for space in ("&#8239;", "&#160;", "&nbsp;", "&thinsp;", "&#8200;", "&#8287;", "&#12288;", "&#5760;", " ", "\t"):
            with self.subTest(space=space):
                self.assertEqual(html_to_text(f"<p>Requirements</p><p>4+ years as{space}Product Owner</p>"),
                                 "Requirements\n\n4+ years as Product Owner")
        from careeros.normalize.sanitize import clean_text
        self.assertEqual(clean_text("a\u202fb\u3000c\nd\u00a0\u2009e"), "a b c\nd e")
        # the follow-up: zero-width separators are spaces, U+2028/U+2029 end a line, the joiners U+200C/U+200D go
        self.assertEqual(clean_text("as\u200bProduct\u2060Owner\ufeffnow\u2028next\u2029last a\u200db\u200cc"),
                         "as Product Owner now\nnext\nlast abc")
        self.assertEqual(html_to_text("<p>4+ years as&#8203;Product Owner</p>"), "4+ years as Product Owner")

    def test_family_and_exclusions(self):
        fam = POLICY.get("families")
        self.assertEqual(classify_family(norm("Sr. Business Systems Analyst"), fam), "healthcare_bsa")
        self.assertEqual(classify_family(norm("Power Platform Business Analyst"), fam), "power_platform_automation")
        self.assertIsNone(classify_family(norm("Software Engineer II"), fam))
        self.assertEqual(excluded_term(norm("Director, Business Systems"), POLICY.get("title_exclude")), "director")

    def test_targeted_supervisors_are_discoverable_in_both_title_orders(self):
        from careeros.normalize.posting import relevant_role

        for title in ("Supervisor, Contact Center Operations", "Claims Operations Supervisor",
                      "Provider Data Operations Supervisor", "Supervisor, Insurance/Billing/Collection",
                      "QA Supervisor"):
            with self.subTest(title=title):
                normalized = norm(title)
                family = classify_family(normalized, POLICY.get("families"))
                self.assertTrue(relevant_role(normalized, family, "remote", "US", POLICY))
                self.assertFalse(relevant_role(normalized, family, "hybrid", "US", POLICY))
                self.assertFalse(relevant_role(normalized, family, "remote", "non_US", POLICY))
        for title in ("Claims Operations Manager", "Software QA Engineer", "Software QA Supervisor", "Retail Operations Supervisor"):
            with self.subTest(title=title):
                normalized = norm(title)
                family = classify_family(normalized, POLICY.get("families"))
                self.assertFalse(relevant_role(normalized, family, "remote", "US", POLICY))

    def test_remote_states_country_comp_type(self):
        self.assertEqual(remote_states("Remote - US", postings.GOOD_FIT, "remote"), ["AZ", "FL", "GA", "TX"])
        self.assertEqual(detect_country("Remote - US", "", "remote"), "US")
        self.assertEqual(detect_country("London, UK", "", "onsite"), "non_US")
        self.assertEqual(parse_comp("Pay: $95,000 - $120,000 per year"), {"min": 95000, "max": 120000, "period": "year", "currency": "USD"})
        self.assertEqual(parse_comp("$40/hr - $50/hr")["period"], "hour")
        self.assertEqual(parse_comp("$45 to $55 per hour")["max"], 55)
        self.assertEqual(parse_comp("$85K–$110K")["min"], 85000)
        self.assertEqual(employment_type(None, "This is a 6 month contract-to-hire role"), "contract_to_hire")
        self.assertEqual(canonical_url("HTTPS://Jobs.Lever.co/acme/123/?lever-source=x#top"), "https://jobs.lever.co/acme/123")
        # Identifying parameters survive; tracking parameters do not (live bug: gh_jid stripped => false duplicates).
        self.assertEqual(canonical_url("https://acme.com/careers/job?gh_jid=81&utm_source=x&gh_src=y"), "https://acme.com/careers/job?gh_jid=81")
        self.assertNotEqual(canonical_url("https://acme.com/careers/job?gh_jid=81"), canonical_url("https://acme.com/careers/job?gh_jid=82"))
        self.assertEqual(detect_country("Dhaka, Bangladesh", "", "onsite"), "non_US")

    def test_a_remote_posting_that_says_it_is_remote_in_the_us_is_placed_in_the_us(self):
        # the demo's pasted posting said only "Location: Remote" and, in its text, "This role is remote in the US" (2026-11-15)
        self.assertEqual(detect_country("Remote", "Pay is good. This role is remote in the US.", "remote"), "US")
        self.assertEqual(detect_country("Remote", "You may work remotely in the U.S. from any state.", "remote"), "US")
        self.assertEqual(detect_country("Remote", "Candidates based in the USA are welcome.", "remote"), "US")
        # "us" as a word on its own is not a country: "join us", "contact us in the morning"
        self.assertEqual(detect_country("Remote", "Join us! Contact us in the morning.", "remote"), "unknown")
        self.assertEqual(detect_country("Remote", "Tell us about the tools you use.", "remote"), "unknown")
        # a stated foreign location still wins over the body
        self.assertEqual(detect_country("London, UK", "This role is remote in the US.", "onsite"), "non_US")

    def test_requirements_fingerprint_tolerates_missing_years(self):
        from careeros.extract.model import Extraction, Requirement
        ex = Extraction("t", [Requirement("skill", "sql", "hard_required", "a", 0.9, None),
                              Requirement("skill", "sql", "hard_required", "b", 0.9, 3.0)], [], [])
        self.assertEqual(len(ex.requirements_fingerprint()), 64)  # live bug: None vs float comparison crashed

    def test_lever_full_html_preferred_over_truncated_plain_text(self):
        from careeros.discovery.boards import lever_posting
        item = {"id": "x", "text": "Process Analyst", "descriptionPlain": "The Role",
                "description": "<div>The Role</div><div>Requirements:</div><ul><li>3+ years of SQL</li></ul>", "lists": []}
        self.assertIn("3+ years of SQL", lever_posting("acme", item).description_text)


class ExtractTests(unittest.TestCase):
    def test_structured_posting(self):
        ex = extract(postings.GOOD_FIT, POLICY)
        found = {(r.category, r.name, r.requirement_type, r.years) for r in ex.requirements}
        self.assertIn(("skill", "sql", "hard_required", 3.0), found)
        self.assertIn(("education", "bachelor", "required_with_equivalency", None), found)
        self.assertIn(("skill", "rxclaim", "hard_required", None), found)
        self.assertIn(("skill", "power bi", "preferred", None), found)
        self.assertIn(("certification", "cbap", "preferred", None), found)
        self.assertTrue(any(r.requirement_type == "responsibility" for r in ex.requirements))
        for r in ex.requirements:  # every record quotes the posting verbatim
            self.assertIn(r.source_text, postings.GOOD_FIT)

    def test_unstructured_posting_is_low_confidence(self):
        ex = extract(postings.FLAT, POLICY)
        self.assertIn("no_requirement_headings", ex.notes)
        self.assertTrue(ex.requirements and all(r.confidence < 0.7 for r in ex.requirements))

    def test_benefits_section_is_not_a_requirement(self):
        text = "Requirements:\n- 3+ years of SQL\n- Bachelor's degree\nPerks & Benefits:\n- Medical, dental and claims reimbursement"
        found = {(r.category, r.name) for r in extract(text, POLICY).requirements}
        self.assertNotIn(("domain", "claims"), found)  # live bug: benefits heading read as a claims requirement
        self.assertIn(("skill", "sql"), found)

    def test_ambiguous_section_never_becomes_preferred(self):
        ex = extract("Qualifications:\n- Experience with Facets\n- Bachelor's degree", POLICY)
        self.assertTrue(all(r.requirement_type == "hard_required" for r in ex.requirements))


class GateTests(unittest.TestCase):
    def test_good_fit_is_eligible(self):
        _, gate = run(job("Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT))
        self.assertEqual(gate.decision, "eligible", gate.hard_failures)

    def test_hard_failures_each_explained(self):
        _, gate = run(job("Business Analyst", postings.HARD_FAIL, location="Remote"))
        codes = {f["code"] for f in gate.hard_failures}
        self.assertEqual(gate.decision, "rejected")
        self.assertIn("clearance_required", codes, codes)
        self.assertNotIn("certification_required", codes)   # D140 F1: a certification never rejects on its own
        self.assertIn("certification_check", {f["code"] for f in gate.flags})
        # D119 (2026-11-09): "must reside in one of the following states" in the text (location "Remote") never rejects;
        # it flags, with the sentence quoted. Only a location field naming other states rejects (test_label_findings).
        self.assertNotIn("state_not_eligible", codes)
        self.assertIn("one of the following states: Virginia, Maryland, District of Columbia",
                      next(f["source_text"] for f in gate.flags if f["code"] == "residence_limit_check"))
        self.assertTrue(all(f["detail"] for f in gate.hard_failures))

    def test_scam_title_exclusion_location_and_pay_floor(self):
        self.assertIn("scam_signals", {f["code"] for f in run(job("Data Analyst", postings.SCAM))[1].hard_failures})
        self.assertIn("title_excluded", {f["code"] for f in run(job("Director, Business Systems", postings.GOOD_FIT))[1].hard_failures})
        self.assertIn("outside_commute", {f["code"] for f in run(job("Business Analyst", postings.FLAT, location="Dallas, TX", workplace="onsite"))[1].hard_failures})
        low_pay = postings.GOOD_FIT.replace("$95,000 - $120,000", "$55,000 - $70,000")
        self.assertIn("below_pay_floor", {f["code"] for f in run(job("Business Systems Analyst", low_pay))[1].hard_failures})

    def test_unknown_profile_facts_are_flags_not_passes(self):
        from careeros.qualify.profile import Profile
        blank = Profile({"status": "draft", "skills": [], "functions": []}, "x")
        reqs = extract(postings.GOOD_FIT, POLICY).requirements
        gate = evaluate(job("Business Systems Analyst", postings.GOOD_FIT), reqs, blank, POLICY)
        self.assertIn("years_unknown", {f["code"] for f in gate.flags})
        self.assertIn("education_unknown", {f["code"] for f in gate.flags})

    def test_remote_only_policy(self):
        import json as _json
        from careeros.core.fingerprint import fingerprint as _fp
        from careeros.core.policy import PolicyBundle
        data = _json.loads(_json.dumps(POLICY.data))
        data["location"]["remote_only"] = True
        remote_only = PolicyBundle(data, _fp(data))
        reqs = extract(postings.GOOD_FIT, remote_only).requirements
        hybrid = job("Business Systems Analyst", postings.GOOD_FIT, location="Austin, TX", workplace="hybrid")
        self.assertIn("not_remote", {f["code"] for f in evaluate(hybrid, reqs, PROFILE, remote_only).hard_failures})
        unknown = job("Business Systems Analyst", postings.GOOD_FIT, location="Florida")
        unknown["remote_type"] = "unknown"
        result = evaluate(unknown, reqs, PROFILE, remote_only)
        self.assertEqual(result.decision, "eligible")
        self.assertIn("remote_unknown", {f["code"] for f in result.flags})

    def test_certification_matching_is_specific(self):
        from careeros.qualify.profile import Profile
        p = Profile({"certifications": [{"name": "Lean Six Sigma Green Belt", "matches": ["lean six sigma"], "status": "held"},
                                        {"name": "PL-900", "status": "held"}]}, "x")
        self.assertTrue(p.has_certification("six sigma"))
        self.assertTrue(p.has_certification("lean six sigma"))
        self.assertTrue(p.has_certification("pl-900"))
        self.assertFalse(p.has_certification("pl-200"))
        self.assertFalse(p.has_certification("pmp"))
        reqs = extract("Requirements:\n- Lean Six Sigma certification required", POLICY).requirements
        self.assertEqual({r.name for r in reqs if r.category == "certification"}, {"family:lean_six_sigma"})   # 2026-10-15: a family
        from careeros.qualify.certifications import analyse
        record = analyse(reqs, p, POLICY)[0]
        self.assertEqual((record["match_type"], record["satisfied"], record["decision_effect"]), ("family_match", True, "requirement_satisfied"))

    def test_training_only_skills_are_partial_coverage(self):
        from careeros.qualify.profile import Profile
        from careeros.qualify.scores import eligibility
        reqs = extract("Requirements:\n- SQL experience required\n- Excel experience required", POLICY).requirements
        j = job("Business Systems Analyst", "x")
        direct = Profile({"skills": [{"name": "sql", "evidence": "direct"}, {"name": "excel", "evidence": "direct"}]}, "d")
        trained = Profile({"skills": [{"name": "sql", "evidence": "adjacent"}, {"name": "excel", "evidence": "direct"}]}, "t")
        g = evaluate(j, reqs, direct, POLICY)
        self.assertEqual(eligibility(j, reqs, direct, g, POLICY)["dims"]["skills_coverage"]["value"], 1.0)
        self.assertEqual(eligibility(j, reqs, trained, g, POLICY)["dims"]["skills_coverage"]["value"], 0.7)

    def test_targeted_supervisor_is_conditional_but_manager_remains_rejected(self):
        from careeros.qualify.profile import Profile
        from careeros.qualify.gate import state_for

        mid = Profile({**PROFILE.data, "current_level": "mid"}, "mid")
        reqs = extract(postings.GOOD_FIT, POLICY).requirements
        supervisor = evaluate(job("Supervisor, Contact Center Operations", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertNotIn("seniority_gap", {f["code"] for f in supervisor.hard_failures})
        self.assertIn("supervisor_scope_review", {f["code"] for f in supervisor.flags})
        self.assertEqual(state_for(supervisor, POLICY), "CONDITIONAL")
        manager = evaluate(job("Manager, Contact Center Operations", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertIn("seniority_gap", {f["code"] for f in manager.hard_failures})
        self.assertNotIn("supervisor_scope_review", {f["code"] for f in manager.flags})
        senior = Profile({**PROFILE.data, "current_level": "senior"}, "senior")
        j = job("Provider Data Operations Supervisor", postings.GOOD_FIT)
        self.assertEqual(j["family"], "pbm_claims_operations")
        self.assertEqual(state_for(evaluate(j, reqs, senior, POLICY), POLICY), "CONDITIONAL")

        # D119 (2026-11-09): only a state the location field names rejects, so the limit is the location's here; D123: not
        # beside a description list naming your state (GOOD_FIT's own list names FL: the two disagree, a flag), so the
        # posting's text here is the one without its list
        blocked = dict(j, remote_states=["CA"], location_raw="Remote - CA", comp_max=10000, comp_period="year",
                       description_text=postings.GOOD_FIT.split("This role is remote")[0])
        result = evaluate(blocked, reqs, mid, POLICY)
        # D151: a location field's state never rejects (a flag); the pay floor still does
        self.assertEqual({f["code"] for f in result.hard_failures} & {"state_not_eligible", "below_pay_floor"},
                         {"below_pay_floor"})
        self.assertIn("residence_limit_check", {f["code"] for f in result.flags})

    def test_generic_qa_analyst_requires_relevant_context(self):
        cases = (
            ("Selenium, Playwright and software regression testing. " * 6, True),
            # D126 (the intake hotfix's Pass 2, CR-HF-04): with a testing tool and one weak or one strong health-plan word
            # the posting is flagged for review (generic_title_unverified), never rejected
            ("Selenium, Playwright and software regression testing of enrollment screens. " * 6, False),
            ("Selenium, Playwright and software regression testing of claims screens. " * 6, False),
            ("Manual and exploratory software regression testing for our mobile app. " * 6, True),
            ("Claims quality review and call monitoring in a healthcare contact center", False),
        )
        for description, should_reject in cases:
            with self.subTest(description=description):
                j = job("QA Analyst", description, location="Remote")
                self.assertEqual(j["family"], "data_reporting")
                _reqs, gate = run(j)
                codes = {f["code"] for f in gate.hard_failures}
                self.assertEqual("generic_title_no_context" in codes, should_reject)

    def test_manager_roles_rejected_for_mid_level_profile(self):
        from careeros.qualify.profile import Profile
        mid = Profile({**PROFILE.data, "current_level": "mid"}, "mid")
        reqs = extract(postings.GOOD_FIT, POLICY).requirements
        manager = evaluate(job("Manager, Business Systems Analysis", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertIn("seniority_gap", {f["code"] for f in manager.hard_failures})
        senior = evaluate(job("Senior Business Systems Analyst", postings.GOOD_FIT), reqs, mid, POLICY)
        self.assertNotIn("seniority_gap", {f["code"] for f in senior.hard_failures})

    def test_function_matching_allows_partial_wording_for_long_names(self):
        from careeros.qualify.profile import Profile
        p = Profile({"functions": [{"name": "pbm operations"}, {"name": "claims adjudication research"}]}, "f")
        self.assertEqual(p.function("experience in PBM operations or pharmacy benefits")["name"], "pbm operations")
        self.assertEqual(p.function("claims adjudication experience")["name"], "claims adjudication research")
        self.assertIsNone(p.function("operations management"))  # short names need every word

    def test_experience_area_names(self):
        from careeros.extract.rules import _area
        self.assertEqual(_area("claims experience required"), "claims")
        self.assertEqual(_area("experience as a business systems analyst in healthcare"), "business systems analyst in healthcare")
        self.assertEqual(_area("experience working with pharmacy benefits is preferred"), "pharmacy benefits")
        self.assertEqual(_area("experience"), "professional experience")

    def test_domain_years_satisfy_domain_year_requirements(self):
        from careeros.qualify.profile import Profile
        p = Profile({"domains": [{"name": "claims", "years": 3.9}], "skills": [], "functions": []}, "d")
        self.assertEqual(p.years_for("claims"), 3.9)
        reqs = extract("Requirements:\n- 3+ years of claims experience required", POLICY).requirements
        gate = evaluate(job("Claims Analyst", "x"), reqs, p, POLICY)
        self.assertFalse({"years_unknown", "years_gap"} & {f["code"] for f in gate.flags + gate.hard_failures})

    def test_screening_years_override_only_screening(self):
        from careeros.qualify.profile import Profile
        p = Profile({"domains": [{"name": "claims", "years": 3.9, "screening_years": 4}], "skills": [], "functions": []}, "s")
        self.assertEqual(p.years_for("claims"), 4.0)
        self.assertEqual(p.data["domains"][0]["years"], 3.9)  # the fact is untouched
        reqs = extract("Requirements:\n- 4+ years of claims experience required", POLICY).requirements
        gate = evaluate(job("Claims Analyst", "x"), reqs, p, POLICY)
        self.assertFalse({"years_slightly_short", "years_gap", "years_unknown"} & {f["code"] for f in gate.flags})

    def test_held_employer_is_flagged_not_rejected(self):
        _, gate = run(job("Business Systems Analyst", postings.GOOD_FIT, employer="Held Parent Co"))
        self.assertEqual(gate.decision, "eligible")
        self.assertIn("held_d13", {f["code"] for f in gate.flags})


class ScoreTests(unittest.TestCase):
    def test_good_fit_ranks_high_and_components_explain(self):
        j = job("Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT)
        reqs, gate = run(j)
        card = score(j, reqs, PROFILE, gate, POLICY, NOW)
        self.assertIn(card.tier, ("A", "B"), card)
        self.assertGreaterEqual(card.eligibility, 85)
        self.assertEqual(card.freshness, 100.0)
        self.assertIn("sql", card.components["eligibility"]["matched_skills"])
        weights = POLICY.get("weights")
        for name in ("eligibility", "candidate_fit", "career_value"):
            self.assertEqual(sum(weights[name].values()), 100)

    def test_unknowns_are_neutral_not_zero(self):
        j = job("Business Systems Analyst", "Responsibilities:\n- Work with teams", posted=None)
        reqs, gate = run(j)
        card = score(j, reqs, PROFILE, gate, POLICY, NOW)
        self.assertEqual(card.freshness, 50.0)
        self.assertTrue(card.components["career_value"]["dims"]["compensation"]["unknown"])

    def test_tiers_require_eligible_state_and_skill_minimum_not_just_priority(self):
        from careeros.qualify.scores import tier_for
        self.assertEqual(tier_for(95, "ELIGIBLE", 0.9, POLICY), "A")
        self.assertEqual(tier_for(95, "CONDITIONAL", 0.9, POLICY), "near_miss")  # listed as Conditional priority
        self.assertEqual(tier_for(95, "HOLD", 1.0, POLICY), "near_miss")
        self.assertEqual(tier_for(95, "ELIGIBLE", 0.5, POLICY), "near_miss")  # your 70% skill minimum
        self.assertEqual(tier_for(88, "ELIGIBLE", 0.8, POLICY), "B")
        self.assertEqual(tier_for(70, "ELIGIBLE", 1.0, POLICY), "archive")

    def test_stale_posting_scores_lower_priority(self):
        fresh = job("Senior Business Systems Analyst", postings.GOOD_FIT)
        stale = job("Senior Business Systems Analyst", postings.GOOD_FIT, posted="2026-08-01T00:00:00.000Z")
        r1, g1 = run(fresh)
        r2, g2 = run(stale)
        self.assertGreater(score(fresh, r1, PROFILE, g1, POLICY, NOW).apply_priority,
                           score(stale, r2, PROFILE, g2, POLICY, NOW).apply_priority)


if __name__ == "__main__":
    unittest.main()
