"""NIGHT work time on dated intervals (R5, 2026-10-16): overlap, duty spans, breaks, sleep opportunity and weekly hours
in real time zones across daylight-saving changes; zones and days read from the posting; unconfirmed never green."""
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from careeros.pipeline import decision_fingerprint
from careeros.qualify import night
from careeros.qualify import worktime as wt

from tests.test_v21_policy import policy_with

HOME = ZoneInfo("America/New_York")   # a fictional home zone
DAY_JOB = {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"], "timezone": "America/New_York"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
REFERENCE = date(2026, 10, 19)           # a Monday; the horizon also reaches 2026-11-01 and 2027-03-14
QUIET = [date(2026, 11, 16) + timedelta(days=i) for i in range(14)]   # two weeks with no clock change


def resolved(zone):
    return {"basis": "resolved", "zones": [zone], "evidence": "stated"}


def quiet(shift, days, zone="America/New_York"):
    """The measures over two weeks without a clock change, both schedules in their own zones."""
    return wt.measure(wt.intervals(8 * 60, 17 * 60, WEEKDAYS, HOME, QUIET),
                      wt.intervals(shift[0], shift[1], days, ZoneInfo(zone), QUIET), HOME, wt.LIMITS)


class ExampleTests(unittest.TestCase):
    """The worked examples approved for the plan: overlap, combined hours and rest reported separately."""

    def test_six_to_ten_pm_supplemental(self):
        m = quiet((18 * 60, 22 * 60), WEEKDAYS)
        self.assertEqual((m["overlap_minutes"], m["combined_hours"], m["span_hours"], m["breaks_hours"]), (0, 13.0, 14.0, [1.0]))
        self.assertEqual((m["sleep_before_hours"], m["sleep_after_hours"], m["weekly_hours"], m["level"]), (10.0, 10.0, 65.0, "caution"))

    def test_friday_overnight(self):
        m = quiet((22 * 60, 6 * 60), ["fri"])
        self.assertEqual((m["overlap_minutes"], m["combined_hours"], m["span_hours"], m["breaks_hours"]), (0, 17.0, 22.0, [5.0]))
        self.assertEqual((m["sleep_before_hours"], m["sleep_after_hours"], m["weekly_hours"], m["level"]), (15.0, 50.0, 53.0, "severe"))

    def test_sunday_overnight_is_no_longer_green(self):
        m = quiet((22 * 60, 6 * 60), ["sun"])
        self.assertEqual((m["combined_hours"], m["span_hours"], m["breaks_hours"], m["sleep_after_hours"], m["level"]),
                         (17.0, 19.0, [2.0], 15.0, "severe"))

    def test_a_shift_an_hour_west_leaves_no_six_hour_block_all_week(self):
        m = quiet((21 * 60, 5 * 60), WEEKDAYS, "America/Chicago")    # 10 PM to 6 AM in your zone
        self.assertGreater(m["span_hours"], 24)
        self.assertTrue(all(b < 6 for b in m["breaks_hours"]))          # short gaps are breaks, never rest
        self.assertEqual(m["level"], "severe")

    def test_a_pacific_afternoon_overlaps_your_day_job(self):
        m = quiet((13 * 60, 21 * 60), WEEKDAYS, "America/Los_Angeles")   # 4 PM to midnight in your zone
        self.assertEqual((m["overlap_minutes"], m["level"]), (60, "severe"))

    def test_arizona_moves_against_your_zone_with_the_seasons(self):
        summer = [date(2026, 10, 19) + timedelta(days=i) for i in range(7)]   # your zone on daylight time
        winter = [date(2026, 11, 16) + timedelta(days=i) for i in range(7)]
        span = {}
        for name, dates in (("summer", summer), ("winter", winter)):
            span[name] = wt.measure(wt.intervals(8 * 60, 17 * 60, WEEKDAYS, HOME, dates),
                                    wt.intervals(18 * 60, 22 * 60, WEEKDAYS, ZoneInfo("America/Phoenix"), dates), HOME,
                                    wt.LIMITS)["span_hours"]
        self.assertEqual(span, {"summer": 17.0, "winter": 16.0})


class DaylightSavingTests(unittest.TestCase):
    def test_elapsed_hours_on_the_change_nights(self):
        every = list(wt.DAYS)
        lengths = {d: (lambda i: (i[0][1] - i[0][0]).total_seconds() / 3600)(wt.intervals(22 * 60, 6 * 60, every, HOME, [d]))
                   for d in (date(2026, 10, 31), date(2027, 3, 13), date(2026, 11, 7))}
        self.assertEqual(lengths, {date(2026, 10, 31): 9.0, date(2027, 3, 13): 7.0, date(2026, 11, 7): 8.0})

    def test_the_horizon_reaches_both_changes(self):
        dates = wt.horizon(REFERENCE, HOME)
        self.assertIn(date(2026, 11, 1), dates)
        self.assertIn(date(2027, 3, 14), dates)
        friday = wt.assess(DAY_JOB, (22 * 60, 6 * 60), ["fri"], resolved("America/New_York"), REFERENCE)
        self.assertEqual(friday["sleep_after_hours"], 49.0)   # the spring-forward weekend is an hour shorter


class ReadingTests(unittest.TestCase):
    def read(self, text, location=None):
        low = text.lower()
        found = night.shift_reading(low)
        span = found[1] if found else None
        return wt.zone_reading(low, span, location), wt.days_reading(low, span, "evening")[0]

    def test_zones(self):
        cases = {"Schedule: 10pm-6am EST, Monday to Friday.": ("ambiguous", ["America/New_York", "Etc/GMT+5"]),
                 "Schedule: 10pm-6am EST. Our team works Eastern Time.": ("ambiguous", ["America/New_York", "Etc/GMT+5"]),
                 "Schedule: 10pm-6am EST (Eastern Time), Monday to Friday.": ("resolved", ["America/New_York"]),
                 "Shift 6pm-10pm (Mountain), weekdays.": ("ambiguous", ["America/Denver", "America/Phoenix"]),
                 "Shift: 6pm to 10pm Central Time, Monday-Friday": ("resolved", ["America/Chicago"]),
                 "Shift: 6pm to 10pm PDT": ("ambiguous", ["America/Los_Angeles", "Etc/GMT+7"]),
                 "Eastern time: 10pm-6am, Sun-Thu": ("resolved", ["America/New_York"]),
                 "Evening shift 6pm-10pm.": ("unstated", list(wt.CONTINENTAL))}
        for text, (basis, zones) in cases.items():
            reading, _days = self.read(text)
            self.assertEqual((reading["basis"], reading["zones"]), (basis, zones), text)
        self.assertEqual(self.read("Shift 6pm-10pm MST, weekdays.", "Phoenix, AZ")[0]["zones"], ["America/Phoenix"])
        self.assertEqual(self.read("Shift 6pm-10pm MST, weekdays.")[0]["basis"], "ambiguous")

    def test_days(self):
        self.assertEqual(self.read("Overnight shift, Sunday to Thursday, 11pm to 7:30am")[1], ["sun", "mon", "tue", "wed", "thu"])
        self.assertEqual(self.read("Shift: 6pm-10pm, Fri-Sun")[1], ["fri", "sat", "sun"])
        self.assertEqual(self.read("Shift: 6pm-10pm, Saturday and Sunday")[1], ["sat", "sun"])
        self.assertIsNone(self.read("Evening shift 6pm-10pm.")[1])


class ConfirmationTests(unittest.TestCase):
    def test_unconfirmed_results_are_never_green(self):
        unstated = {"basis": "unstated", "zones": list(wt.CONTINENTAL), "evidence": "none"}
        load = wt.assess(DAY_JOB, (18 * 60, 20 * 60), WEEKDAYS, unstated, REFERENCE)
        self.assertEqual((load["level"], load["confirmed"]), ("unconfirmed", False))
        self.assertEqual(wt.assess({**DAY_JOB, "timezone": None}, (18 * 60, 20 * 60), WEEKDAYS, resolved("America/New_York"),
                                   REFERENCE)["level"], "unconfirmed")   # your zone isn't set
        assumed = wt.assess(DAY_JOB, (18 * 60, 20 * 60), None, resolved("America/New_York"), REFERENCE)
        self.assertEqual((assumed["level"], assumed["days_basis"]), ("unconfirmed", "assumed Monday to Friday (not stated)"))
        self.assertEqual(wt.assess(DAY_JOB, (18 * 60, 20 * 60), WEEKDAYS, resolved("America/New_York"), REFERENCE)["level"], "green")

    def test_every_possible_zone_is_measured_and_the_worst_is_reported(self):
        unstated = {"basis": "unstated", "zones": list(wt.CONTINENTAL), "evidence": "none"}
        load = wt.assess(DAY_JOB, (13 * 60, 21 * 60), WEEKDAYS, unstated, REFERENCE)
        self.assertEqual(len(load["readings"]), 5)
        self.assertEqual(load["overlap_minutes"], max(r["overlap_minutes"] for r in load["readings"]))
        self.assertEqual((load["level"], load["confirmed"]), ("severe", False))
        overnight = wt.assess(DAY_JOB, (22 * 60, 6 * 60), WEEKDAYS, unstated, REFERENCE)
        self.assertEqual(overnight["overlap_minutes"], 60)   # read as Pacific time it runs into your morning: the worst counts
        # the worst reading is not always the first: "2pm-6pm Pacific or Eastern" is a caution in Pacific time and severe in
        # Eastern time, where it overlaps your day job (the case above put New York first, so readings[0] survived)
        either = {"basis": "ambiguous", "zones": ["America/Los_Angeles", "America/New_York"], "evidence": "Pacific or Eastern"}
        listed = wt.assess(DAY_JOB, (14 * 60, 18 * 60), WEEKDAYS, either, REFERENCE)
        self.assertEqual([(r["zone"], r["level"]) for r in listed["readings"]],
                         [("America/Los_Angeles", "caution"), ("America/New_York", "severe")])
        self.assertEqual((listed["level"], listed["overlap_minutes"]), ("severe", 180))

    def test_without_zone_data_nothing_is_measured(self):
        with mock.patch.object(wt, "ZoneInfo", side_effect=wt.ZoneInfoNotFoundError("no data")):
            load = wt.assess(DAY_JOB, (18 * 60, 20 * 60), WEEKDAYS, resolved("America/New_York"), REFERENCE)
        self.assertEqual((load["level"], load["confirmed"], load["time_basis"]), ("unknown", False, "no_zone_data"))

    def test_night_settings_never_change_primary_decisions(self):
        base = policy_with(night__enabled=True)
        moved = policy_with(night__enabled=True, night__day_job={**DAY_JOB}, night__workload={"duty_gap_hours": 7})
        self.assertEqual(decision_fingerprint(SimpleNamespace(policy=base)), decision_fingerprint(SimpleNamespace(policy=moved)))


class CodexReviewTests(unittest.TestCase):
    """Independent Codex review of f984d15 (2026-10-20)."""
    NOW = "2026-10-19T12:00:00Z"

    @property
    def day_job(self):
        from tests.test_v25_audit import WITH_DAY_JOB
        return WITH_DAY_JOB

    def zone(self, text):
        low = text.lower()
        found = night.shift_reading(low)
        return wt.zone_reading(low, found[1] if found else None)

    def test_every_stated_range_is_assessed_not_only_the_preferred_one(self):
        text = "Shifts rotate between Monday-Friday 6pm-10pm Central Time and Monday-Friday 9am-5pm Central Time."
        load = night.workload(text, "evening", self.day_job, now=self.NOW)
        self.assertEqual(load["overlap_minutes"], 390)   # 9am-5pm Central is 10:00-18:00 against your 08:00-16:30 Eastern
        self.assertFalse(load["confirmed"])              # which range applies isn't known: never confirmed from one of them
        self.assertEqual(len(load["ranges"]), 2)
        single = night.workload("Shift Monday-Friday 6pm-10pm Central Time.", "evening", self.day_job, now=self.NOW)
        self.assertEqual((single["overlap_minutes"], single["confirmed"]), (0, True))

    def test_mountain_daylight_time_is_read_never_raised(self):
        cases = {"Shift Monday-Friday 6pm-10pm MDT (Mountain Time).": ("resolved", ["America/Denver"]),
                 "Shift Monday-Friday 6pm-10pm MDT. Coverage follows Mountain Time.": ("ambiguous", ["America/Denver", "Etc/GMT+6"]),
                 "Shift Monday-Friday 6pm-10pm Mountain Daylight Time.": ("ambiguous", ["America/Denver", "Etc/GMT+6"])}
        for text, expected in cases.items():
            found = self.zone(text)
            self.assertEqual((found["basis"], found["zones"]), expected, text)
            night.workload(text, "evening", self.day_job, now=self.NOW)   # never a KeyError

    def test_a_zone_written_elsewhere_never_corroborates_an_abbreviation(self):
        self.assertEqual(self.zone("Shift Monday-Friday 3pm-9pm EST. Our customers use Eastern Time.")["basis"], "ambiguous")
        self.assertEqual(self.zone("Shift Monday-Friday 3pm-9pm EST, Eastern Time.")["zones"], ["America/New_York"])

    # Independent Codex Pass 2 (2026-10-21): two schedules with the same clock hours but other days or zones were
    # de-duplicated by their hours alone, so the order of the sentences decided whether the weekday overlap was seen.
    SAME_HOURS = ("Weekend schedule: Saturday and Sunday, 3pm-9pm Pacific Time.",
                  "Weekday schedule: Monday-Friday, 3pm-9pm Eastern Time.")

    def test_the_same_hours_on_other_days_or_zones_are_each_assessed(self):
        for text in (" ".join(self.SAME_HOURS), " ".join(reversed(self.SAME_HOURS))):
            load = night.workload(text, "evening", self.day_job, now=self.NOW)
            self.assertEqual(load["overlap_minutes"], 90, text)   # 3pm-9pm Eastern, Monday-Friday, against 08:00-16:30 Eastern
            self.assertFalse(load["confirmed"], text)             # never confirmed from one of several schedules
            self.assertEqual(len(load["ranges"]), 2, text)

    def test_the_same_hours_never_pass_supplemental_without_the_overlap_review(self):
        from tests.test_v21_policy import OVERNIGHT
        from tests.test_v25_audit import assess
        for schedule in (" ".join(self.SAME_HOURS), " ".join(reversed(self.SAME_HOURS))):
            body = OVERNIGHT.replace("Schedule: overnight shift, Sunday to Thursday, 11pm to 7:30am.", "Evening shift. " + schedule)
            result = assess("Claims Quality Auditor (Part-Time)", body, policy=self.day_job, employment="part time")
            self.assertEqual(result["mode"], "supplemental", schedule)
            self.assertIn("possible_day_job_overlap", {f["code"] for f in result["flags"]}, schedule)
            self.assertNotEqual(result["state"], "ELIGIBLE", schedule)

if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """Fresh-context Claude review of 4486b82 (2026-10-18). Fictional day job 08:00-16:30 America/New_York."""
    NOW = "2026-10-19T12:00:00.000Z"

    def load(self, text, schedule="evening", policy=None):
        from tests.test_v25_audit import WITH_DAY_JOB
        return night.workload(text, schedule, policy or WITH_DAY_JOB, now=self.NOW)

    def days(self, text):
        low = text.lower()
        found = night._ranges(low, every=True)
        span = (found[0][4], found[0][5]) if found else None
        return wt.days_reading(low, span, "evening")[0]

    def test_weekend_words_are_not_the_shift_s_days_unless_they_say_so(self):
        for text in ("Evening shift, 1pm to 9pm Central Time, no weekends.", "Some weekends required. Evening shift 1pm to 9pm Central Time.",
                     "Evening shift 1pm to 9pm Central Time, no weekend shifts."):
            load = self.load(text)
            self.assertEqual((self.days(text), load["confirmed"]), (None, False), text)
            self.assertNotEqual(load["level"], "green", text)
            self.assertGreater(load["overlap_minutes"], 0, text)
        self.assertEqual(self.days("Our support center is open Monday to Friday. Weekend shift: 1pm to 9pm Eastern Time."), ["sat", "sun"])
        self.assertIsNone(self.days("Shift: 6pm-10pm Eastern Time, Mon-Fri or Sat-Sun."))   # two answers: not stated
        labelled = self.load("Shift 1pm-9pm Eastern Time.", "weekend")                              # a label alone states no days
        self.assertEqual((labelled["days_basis"], labelled["confirmed"]), ("assumed Monday to Friday (not stated)", False))

    def test_every_stated_range_is_measured(self):
        nine_to_five = self.load("Flexible schedule. Hours: 9am to 5pm Eastern Time, Monday to Friday.", "flexible")
        self.assertEqual((nine_to_five["confirmed"], nine_to_five["overlap_minutes"] > 0), (True, True))
        for text in ("Evening shift, 12pm to 8:30pm Eastern Time, Monday to Friday.", "Shift: 7pm to 3am Eastern Time, Monday to Friday."):
            self.assertNotEqual(self.load(text)["level"], "unknown", text)
        office = self.load("Our office hours are 9am to 5pm Eastern Time, Monday to Friday.", "unknown")
        self.assertEqual((office["confirmed"], office["level"] in ("severe", "unconfirmed")), (False, True))
        self.assertIn("may describe the business", office["basis"])

    def test_a_home_without_daylight_saving_sees_both_seasons(self):
        phoenix = {"start": "08:00", "end": "16:00", "days": WEEKDAYS, "timezone": "America/Phoenix"}
        winter = wt.assess(phoenix, (18 * 60, 22 * 60), WEEKDAYS, resolved("America/New_York"), date(2026, 12, 1))
        self.assertEqual(winter["overlap_minutes"], 60)   # in summer 6pm Eastern is 3pm in Phoenix: always counted now

    def test_zones_come_only_from_words_that_name_one(self):
        def zone(text):
            low = text.lower()
            found = night._ranges(low, every=True)
            return wt.zone_reading(low, (found[0][4], found[0][5]) if found else None)
        self.assertEqual(zone("Shift 4pm-9pm, Central Florida team, Monday to Friday.")["basis"], "unstated")
        self.assertEqual(zone("Shift 3pm-9pm (Central Region), weekdays.")["basis"], "unstated")
        pt = zone("Shift: 3pm-9pm PT, weekdays.")
        self.assertEqual((pt["basis"], "America/New_York" in pt["zones"]), ("ambiguous", True))
        both = zone("Shift: 3pm-9pm ET/PT, weekdays.")
        self.assertEqual((both["basis"], set(both["zones"]) >= {"America/New_York", "America/Los_Angeles"}), ("ambiguous", True))
        listed = zone("Shift: 3pm-9pm Eastern Time or Pacific Time, weekdays.")
        self.assertEqual((listed["basis"], listed["zones"]), ("ambiguous", ["America/New_York", "America/Los_Angeles"]))
        self.assertEqual(zone("We support customers on Pacific Time. Shift: 3pm-9pm, weekdays.")["basis"], "unstated")
        self.assertEqual(zone("Shift: 3:30pm-9pm Eastern Standard Time, weekdays.")["basis"], "ambiguous")
        self.assertEqual(zone("Shift 6pm-10pm (Central), weekdays.")["basis"], "ambiguous")
        self.assertEqual(zone("Shift: 6pm to 10pm Central Time, Monday-Friday")["zones"], ["America/Chicago"])

    def test_clock_home_zone_fold_and_worst_measures(self):
        self.assertEqual([wt._clock(v) for v in ("24:00", "8:75", "23:59", " 08:00 ")], [None, None, 23 * 60 + 59, 8 * 60])
        est = wt.assess({**DAY_JOB, "timezone": "EST"}, (18 * 60, 20 * 60), WEEKDAYS, resolved("America/New_York"), REFERENCE)
        self.assertEqual((est["confirmed"], est["level"]), (False, "unconfirmed"))
        first, last = wt.intervals(17 * 60, 90, list(wt.DAYS), HOME, [date(2026, 10, 31)])[0]
        self.assertEqual((last - first).total_seconds() / 3600, 9.5)   # the fall-back hour is worked twice
        unstated = {"basis": "unstated", "zones": list(wt.CONTINENTAL), "evidence": "none"}
        load = wt.assess(DAY_JOB, (22 * 60, 5 * 60), WEEKDAYS, unstated, REFERENCE)
        each = [wt.assess(DAY_JOB, (22 * 60, 5 * 60), WEEKDAYS, resolved(z), REFERENCE) for z in wt.CONTINENTAL]
        self.assertEqual(load["sleep_after_hours"], min(e["sleep_after_hours"] for e in each))
        self.assertEqual(load["sleep_before_hours"], min(e["sleep_before_hours"] for e in each))

    def test_a_possible_overlap_is_always_material(self):
        from careeros.qualify.gate import GateResult
        from tests.test_v25_audit import WITH_DAY_JOB
        result = GateResult("eligible")
        result.flag("possible_day_job_overlap", "may overlap", "")
        bare = policy_with(night__material_unknowns_extra=[])
        self.assertEqual(night.night_state(result, bare), "CONDITIONAL")
        self.assertEqual(night.night_state(result, WITH_DAY_JOB), "CONDITIONAL")
