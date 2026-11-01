"""Work time for NIGHT Supplemental work (R5, 2026-10-16): your day job and a posting's shift as DATED intervals in real
time zones, so overlap, hours and rest are measured on actual dates, across daylight-saving changes.

Inputs
- Your day job: policy night.day_job {start, end, days, timezone} (your local zone, an IANA name).
- The shift: its clock times, the days it states (`days_reading`) and the time zone it states (`zone_reading`). A zone
  is RESOLVED only when it is written out ("Eastern Time", "Central", "Pacific", "ET", "CT", "PT"), or an abbreviation
  or "Mountain" comes with confirming evidence (the same region written out in the posting, or Arizona / Denver for
  Mountain time). An abbreviation alone (EST, CST, MST, PST, EDT ...) is AMBIGUOUS: its civil-time and fixed-offset
  readings are both measured. With no zone at all, every continental US zone is measured.
- Dates: 14 days from the assessment date, plus the week around each of the next two daylight-saving changes in your
  zone. Durations are elapsed time between real instants (10 PM to 6 AM is 9 hours on the night clocks fall back).

Measures, each reported on its own (none derived from another):
  overlap_minutes   the most minutes in one day when the shift and the day job are both on the clock
  duty spans        runs of work separated by gaps shorter than duty_gap_hours (6, a hypothesis like every threshold
                    here): the hours worked, the elapsed hours, and the breaks inside (never counted as rest)
  sleep             the off-duty blocks before and after each duty span that contains the shift (the shortest of each)
  weekly_hours      the most hours worked in one calendar week (Monday to Sunday, in your zone)
A result is CONFIRMED only when the shift's zone is resolved, its days are stated, your day job's zone is set and the
time-zone data is available. When several zones are possible each one is measured and the worst is reported. An
unconfirmed result is never 'green' and never rejects a job by itself: it asks you to check.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONTINENTAL = ("America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles")
_CIVIL = {"eastern": "America/New_York", "central": "America/Chicago", "pacific": "America/Los_Angeles"}
_MOUNTAIN = ("America/Denver", "America/Phoenix")
_ABBREVIATIONS = {"est": ("America/New_York", "Etc/GMT+5"), "edt": ("America/New_York", "Etc/GMT+4"),
                  "cst": ("America/Chicago", "Etc/GMT+6"), "cdt": ("America/Chicago", "Etc/GMT+5"),
                  "mst": ("America/Denver", "America/Phoenix"), "mdt": ("America/Denver", "Etc/GMT+6"),
                  "pst": ("America/Los_Angeles", "Etc/GMT+8"), "pdt": ("America/Los_Angeles", "Etc/GMT+7")}
_REGION_OF = {"est": "eastern", "edt": "eastern", "cst": "central", "cdt": "central", "mst": "mountain", "mdt": "mountain",
              "pst": "pacific", "pdt": "pacific"}
_ABBREVIATIONS_OF = {(region, kind): f"{region[0]}{kind[0]}t" for region in ("eastern", "central", "mountain", "pacific")
                     for kind in ("standard", "daylight")}
_REGIONS = ("eastern", "central", "mountain", "pacific")
_TWO_LETTER = {"et": "America/New_York", "ct": "America/Chicago", "mt": "America/Denver", "pt": "America/Los_Angeles"}
# a zone named next to the hours: "<region> [standard|daylight] time", an abbreviation, or a bare region word that is not
# followed by another word ("(Central)", but never "Central Florida" or "Central Region")
_ZONE_TOKEN = re.compile(r"(eastern|central|mountain|pacific)(?:\s+(standard|daylight))?\s+time\b|"
                         r"\b(est|edt|cst|cdt|mst|mdt|pst|pdt|et|ct|mt|pt)\b|(eastern|central|mountain|pacific)(?=\s*(?:[),.;:\]/]|$))")
_SEPARATOR = re.compile(r"\s*(?:/|,|&|\bor\b|\band\b)\s*")
_BEFORE = re.compile(r"\b(eastern|central|mountain|pacific)(?:\s+(standard|daylight))?\s+time(?:\s+hours)?[\s:,(]{0,3}$")
_PLAIN = re.compile(r"\b(eastern|central|mountain|pacific)\s+time\b")   # written out plainly: corroboration
_ARIZONA = re.compile(r"\b(arizona|phoenix)\b|,\s*az\b")
_DENVER = re.compile(r"\b(denver|colorado)\b|,\s*co\b")
_DAY = r"(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)\.?"
_DAY_RANGE = re.compile(r"\b" + _DAY + r"\s*(?:-|–|—|to|through|thru)\s*" + _DAY + r"(?![a-z])")
_DAY_LIST = re.compile(r"\b" + _DAY + r"(?:\s*(?:,|and|&|/)\s*" + _DAY + r")+(?![a-z])")
_WEEKDAYS = re.compile(r"\bweekdays\b")
_WEEKENDS = re.compile(r"\bweekends?\b")
# weekend work stated as the days of the shift; never "no weekends", "some weekends" or a schedule label (2026-10-18)
_WEEKEND_ONLY = re.compile(r"\b(?:weekends?\s+only|only\s+(?:on\s+)?weekends|weekend\s+(?:shifts?|schedule|hours)|"
                           r"(?:saturdays?|sat)\s*(?:and|&|-|to|through|/)\s*(?:sundays?|sun))\b")
_NEGATED = re.compile(r"\b(?:no|not|never|without|free|off|except)\b[^.;\n]{0,20}$")
_SENTENCE_END = re.compile(r"[.;!?\n](?!\d)")


def _day(word: str) -> str:
    return word.rstrip(".")[:3]


def _sentence(text: str, span: tuple[int, int]) -> tuple[int, int]:
    """Where the sentence holding `span` starts and ends."""
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, span[0])]
    after = _SENTENCE_END.search(text, span[1])
    return (starts[-1] if starts else 0), (after.start() if after else len(text))


def _token(match) -> tuple[str, str | None, bool]:
    """(what was written, its 'standard'/'daylight' word, whether it is a bare region word without 'time')."""
    if match.group(1):
        return match.group(1), match.group(2), False
    return (match.group(3) or match.group(4)), None, bool(match.group(4))


def _named_after(text: str) -> list[tuple[str, str | None, bool]]:
    """Every zone named right after the hours: "PT", "ET/PT", "(Central)", "Eastern Time or Central Time"."""
    out: list[tuple[str, str | None, bool]] = []
    lead = re.match(r"[\s(\[,:-]*", text)
    pos = lead.end() if lead else 0
    while True:
        found = _ZONE_TOKEN.match(text, pos)
        if not found:
            return out
        out.append(_token(found))
        pos = found.end()
        separator = _SEPARATOR.match(text, pos)
        if not separator:
            return out
        pos = separator.end()


def _one(word: str, standard: str | None, text: str, where: str, bare: bool = False) -> tuple[str, list[str], str]:
    """(basis, zones, evidence) for one zone written next to the hours. `text` is the shift's own sentence: a zone
    written anywhere else in the posting never corroborates it (independent Codex review of f984d15, 2026-10-20)."""
    if word in _REGIONS and standard:   # "Eastern Standard Time": the clock it names may be the fixed offset
        word = _ABBREVIATIONS_OF[(word, standard)]
    if word == "mdt":   # Mountain daylight time is Denver's summer clock; Arizona never keeps it (2026-10-20)
        if _DENVER.search(where) or _DENVER.search(text) or "mountain" in {m.group(1) for m in _PLAIN.finditer(text)}:
            return "resolved", ["America/Denver"], f"stated: {word}, with Denver or Mountain time written out"
        return "ambiguous", list(_ABBREVIATIONS[word]), f"stated: {word} (an abbreviation alone)"
    if word in ("mountain", "mt", "mst"):
        if _ARIZONA.search(where) or _ARIZONA.search(text):
            return "resolved", ["America/Phoenix"], f"stated: {word}, with Arizona"
        if _DENVER.search(where) or _DENVER.search(text):
            return "resolved", ["America/Denver"], f"stated: {word}, with Denver"
        zones = list(_MOUNTAIN) if word != "mst" else list(_ABBREVIATIONS["mst"])
        return "ambiguous", zones, f"stated: {word} (Mountain or Arizona time)"
    if word in _ABBREVIATIONS:
        region = _REGION_OF[word]
        if region in _CIVIL and region in {m.group(1) for m in _PLAIN.finditer(text)}:
            return "resolved", [_CIVIL[region]], f"stated: {word}, with {region} time written out"
        return "ambiguous", list(_ABBREVIATIONS[word]), f"stated: {word} (an abbreviation alone)"
    if word in _TWO_LETTER:   # "PT" can be part-time, "CT" a state: never resolved alone (2026-10-18)
        named = _TWO_LETTER[word]
        return "ambiguous", [named, *(z for z in CONTINENTAL if z != named)], f"stated: {word} (a two-letter abbreviation alone)"
    if word in _REGIONS and bare:
        named = _CIVIL[word]
        return "ambiguous", [named, *(z for z in CONTINENTAL if z != named)], f"stated: {word} (a region word without 'time')"
    return "resolved", [_CIVIL[word]], f"stated: {word} time"


def zone_reading(text: str, span: tuple[int, int] | None, location: str | None = None) -> dict:
    """{basis: resolved | ambiguous | unstated, zones, evidence} for a shift whose time range sits at `span` in the
    lower-cased posting `text`. Since 2026-10-18 (fresh-context review) only "<region> time" written next to the hours,
    or in their own sentence, resolves a zone; a bare region word or a two-letter abbreviation is ambiguous; several zones
    listed together ("ET/PT") are all measured; "Eastern Standard Time" reads like "EST"; a zone mentioned elsewhere in
    the posting never resolves the shift's."""
    text, where = (text or "").lower(), (location or "").lower()
    if span:
        lo, hi = _sentence(text, span)
        named = _named_after(text[span[1]:span[1] + 48])
        before = _BEFORE.search(text[max(0, span[0] - 40):span[0]])
        if before and not named:
            named = [(before.group(1), before.group(2), False)]
        if named:
            readings = [_one(word, standard, text[lo:hi], where, bare) for word, standard, bare in named]
            zones = list(dict.fromkeys(z for _, found, _ in readings for z in found))
            if len(readings) == 1 or (all(b == "resolved" for b, _, _ in readings) and len(zones) == 1):
                return {"basis": readings[0][0], "zones": readings[0][1], "evidence": readings[0][2]}
            return {"basis": "ambiguous", "zones": zones, "evidence": "stated: " + " / ".join(w for w, _, _ in named)}
        regions = sorted({m.group(1) for m in _PLAIN.finditer(text[lo:hi])} - {"mountain"})
        if len(regions) == 1:
            return {"basis": "resolved", "zones": [_CIVIL[regions[0]]], "evidence": f"the shift's sentence names {regions[0]} time"}
    elsewhere = sorted({m.group(1) for m in _PLAIN.finditer(text)})
    note = f" ({', '.join(elsewhere)} time mentioned elsewhere in the posting)" if elsewhere else ""
    return {"basis": "unstated", "zones": list(CONTINENTAL), "evidence": "no time zone stated with the hours" + note}


def combine(loads: list[dict]) -> dict:
    """Several stated ranges in one posting (independent Codex review of f984d15, 2026-10-20): each is measured, every
    measure is reported at its worst, and the result is never confirmed, since the posting doesn't say which applies."""
    measured = [load for load in loads if load.get("readings")]
    if not measured:
        return loads[0]

    def worst_level(load: dict) -> int:
        return max(LEVELS.index(r["level"]) for r in load["readings"])

    worst = max(measured, key=lambda load: (worst_level(load), load["overlap_minutes"], load["combined_hours"], load["span_hours"]))
    least = lambda key: min((load[key] for load in measured if load.get(key) is not None), default=None)
    out = dict(worst)
    out.update(overlap_minutes=max(load["overlap_minutes"] for load in measured),
               combined_hours=max(load["combined_hours"] for load in measured),
               span_hours=max(load["span_hours"] for load in measured), weekly_hours=max(load["weekly_hours"] for load in measured),
               sleep_before_hours=least("sleep_before_hours"), sleep_after_hours=least("sleep_after_hours"), confirmed=False,
               readings=[r for load in measured for r in load["readings"]], ranges=[load["basis"] for load in measured])
    level = LEVELS[worst_level(worst)]
    out["level"] = level if level != "green" else "unconfirmed"
    out["basis"] = (f"{len(measured)} stated ranges, each measured: " + " | ".join(load["basis"] for load in measured)
                    + ". Unconfirmed: the posting doesn't say which range applies")
    return out


def _day_sets(segment: str) -> list[tuple[list[str], str]]:
    found = []
    for m in _DAY_RANGE.finditer(segment):
        first, last = DAYS.index(_day(m.group(1))), DAYS.index(_day(m.group(2)))
        found.append(([DAYS[(first + i) % 7] for i in range((last - first) % 7 + 1)], m.group(0).strip()))
    for m in _DAY_LIST.finditer(segment):
        named = [_day(d.group(1)) for d in re.finditer(_DAY, m.group(0))]
        found.append(([d for d in DAYS if d in named], m.group(0).strip()))
    if _WEEKDAYS.search(segment):
        found.append((list(DAYS[:5]), "weekdays"))
    for m in _WEEKEND_ONLY.finditer(segment):
        if not _NEGATED.search(segment[max(0, m.start() - 25):m.start()]):
            found.append((["sat", "sun"], m.group(0).strip()))
    return found


def days_reading(text: str, span: tuple[int, int] | None, schedule: str) -> tuple[list[str] | None, str]:
    """(days, evidence) the shift is worked; None when the posting doesn't say clearly. Read from the shift's own
    sentence first, then near its time range (2026-10-18); days that disagree, "some weekends", "no weekends" or a
    schedule label alone are not stated days."""
    text = (text or "").lower()
    segments = []
    if span:
        lo, hi = _sentence(text, span)
        segments = [text[lo:hi], text[max(0, span[0] - 160):span[1] + 160]]
    else:
        segments = [text]
    for segment in segments:
        sets = _day_sets(segment)
        distinct = {tuple(days) for days, _ in sets}
        if len(distinct) == 1:
            return list(sets[0][0]), f"stated: {sets[0][1]}"
        if len(distinct) > 1:
            return None, "unclear: " + "; ".join(evidence for _, evidence in sets[:3])
    if any(_WEEKENDS.search(segment) for segment in segments):
        return None, "weekends mentioned, but not as the shift's days"
    return None, "not stated"


def _clock(value) -> int | None:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or "").strip())   # never "24:00" or "8:75"
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def horizon(reference: date, zones, days: int = 14) -> list[date]:
    """`days` dates from `reference`, plus the week around each of the next two daylight-saving changes in EVERY zone
    given (2026-10-18: your zone and the shift's), so a home without daylight saving still sees both seasons."""
    dates = {reference + timedelta(days=i) for i in range(days)}
    for zone in ([zones] if isinstance(zones, ZoneInfo) else zones):
        noon = lambda d, z=zone: datetime.combine(d, time(12), tzinfo=z).utcoffset()
        found, d = 0, reference
        while found < 2 and d < reference + timedelta(days=400):
            if noon(d) != noon(d + timedelta(days=1)):
                dates |= {d + timedelta(days=i) for i in range(-3, 5)}
                found += 1
            d += timedelta(days=1)
    return sorted(dates)


def intervals(start: int, end: int, days: list[str], zone: ZoneInfo, dates: list[date]) -> list[tuple[datetime, datetime]]:
    """Real UTC instants of a schedule worked `start`-`end` (minutes after midnight, local) on `days` of `dates`."""
    out = []
    for d in dates:
        if DAYS[d.weekday()] not in days:
            continue
        first = datetime.combine(d, time(start // 60, start % 60), tzinfo=zone)
        last_day = d if end > start else d + timedelta(days=1)
        # an end in the repeated hour of a fall-back night is the later one (fold=1): the longer, worse case
        last = datetime.combine(last_day, time(end // 60 % 24, end % 60), tzinfo=zone).replace(fold=1)
        out.append((first.astimezone(timezone.utc), last.astimezone(timezone.utc)))
    return out


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 2)


def measure(day_job: list, shift: list, home: ZoneInfo, limits: dict) -> dict:
    """The measures and level for one reading: `day_job` and `shift` are lists of (start, end) UTC instants."""
    gap = timedelta(hours=limits["duty_gap_hours"])
    overlap: dict = {}
    for a in day_job:
        for b in shift:
            both = min(a[1], b[1]) - max(a[0], b[0])
            if both > timedelta(0):
                key = a[0].astimezone(home).date()
                overlap[key] = overlap.get(key, 0) + int(both.total_seconds() // 60)
    work = sorted([(s, e, "day_job") for s, e in day_job] + [(s, e, "shift") for s, e in shift])
    spans: list[dict] = []
    for s, e, source in work:
        if spans and s - spans[-1]["end"] < gap:
            span = spans[-1]
            if s > span["end"]:
                span["breaks"].append(_hours(s - span["end"]))
            span["end"] = max(span["end"], e)
        else:
            span = {"start": s, "end": e, "worked": timedelta(0), "breaks": [], "shift": False}
            spans.append(span)
        span["worked"] += e - s
        span["shift"] = span["shift"] or source == "shift"
    worst = None
    before, after = [], []
    for i, span in enumerate(spans):
        if not span["shift"]:
            continue
        if worst is None or (span["worked"], span["end"] - span["start"]) > (worst["worked"], worst["end"] - worst["start"]):
            worst = span
        if i > 0:
            before.append(_hours(span["start"] - spans[i - 1]["end"]))
        if i + 1 < len(spans):
            after.append(_hours(spans[i + 1]["start"] - span["end"]))
    weekly: dict = {}
    for s, e, _ in work:
        week = s.astimezone(home).isocalendar()[:2]
        weekly[week] = weekly.get(week, timedelta(0)) + (e - s)
    result = {"overlap_minutes": max(overlap.values(), default=0),
              "combined_hours": _hours(worst["worked"]) if worst else 0.0,
              "span_hours": _hours(worst["end"] - worst["start"]) if worst else 0.0,
              "breaks_hours": worst["breaks"] if worst else [],
              "sleep_before_hours": min(before) if before else None, "sleep_after_hours": min(after) if after else None,
              "weekly_hours": _hours(max(weekly.values(), default=timedelta(0)))}
    sleep = [h for h in (result["sleep_before_hours"], result["sleep_after_hours"]) if h is not None]
    if result["overlap_minutes"] or result["combined_hours"] >= limits["severe_total_hours"] \
            or result["span_hours"] > limits["severe_span_hours"] or (sleep and min(sleep) < limits["severe_min_rest_hours"]):
        result["level"] = "severe"
    elif result["combined_hours"] > limits["caution_total_hours"] or (sleep and min(sleep) < limits["caution_min_rest_hours"]):
        result["level"] = "caution"
    else:
        result["level"] = "green"
    return result


LEVELS = ("green", "caution", "severe")
LIMITS = {"caution_total_hours": 12, "severe_total_hours": 14, "caution_min_rest_hours": 8, "severe_min_rest_hours": 6,
          "duty_gap_hours": 6, "severe_span_hours": 24}


def assess(day_job: dict, shift: tuple[int, int] | None, days: list[str] | None, zone: dict, reference: date,
           limits: dict | None = None, hours_direct: bool = True) -> dict:
    """Workload for one posting against your day job. `zone` is a zone_reading; `days` None means not stated (Monday to
    Friday is then measured, as an assumption). `hours_direct` False: the hours may describe the business rather than
    the shift, so the result can't be confirmed."""
    limits = {**LIMITS, **(limits or {})}
    day_start, day_end = _clock(day_job.get("start")), _clock(day_job.get("end"))
    if day_start is None or day_end is None:
        return {"level": "unknown", "confirmed": False, "basis": "your day-job hours are not set (night.day_job in vault overrides)",
                "overlap_minutes": 0}
    if shift is None:
        return {"level": "unknown", "confirmed": False, "basis": "the posting does not state its hours", "overlap_minutes": 0}
    try:
        home_name = day_job.get("timezone")
        # a region name only ("America/Chicago"): an abbreviation, Etc/ or fixed offset never confirms (2026-10-18)
        home_ok = bool(home_name) and "/" in str(home_name) and not str(home_name).startswith("Etc/")
        home = ZoneInfo(str(home_name)) if home_ok else None
        candidates = [(name, ZoneInfo(name)) for name in zone["zones"]]
    except ZoneInfoNotFoundError:
        return {"level": "unknown", "confirmed": False, "basis": "time-zone data is not available (install tzdata)",
                "overlap_minutes": 0, "time_basis": "no_zone_data"}
    days_stated = days is not None
    shift_days: list[str] = days if days is not None else list(DAYS[:5])
    readings = []
    for name, tz in candidates:
        mine = home or tz   # your zone unset: the same zone is assumed, and the result can't be confirmed
        dates = horizon(reference, [mine, tz])
        reading = measure(intervals(day_start, day_end, list(day_job.get("days") or DAYS[:5]), mine, dates),
                          intervals(shift[0], shift[1], shift_days, tz, dates), mine, limits)
        readings.append({"zone": name, **reading})
    worst = max(readings, key=lambda r: (LEVELS.index(r["level"]), r["overlap_minutes"], r["combined_hours"], r["span_hours"]))
    confirmed = zone["basis"] == "resolved" and days_stated and home is not None and hours_direct
    # each measure at its worst across the readings (2026-10-18): the most overlap and hours, the least sleep
    least = lambda key: min((r[key] for r in readings if r[key] is not None), default=None)
    out = {"overlap_minutes": max(r["overlap_minutes"] for r in readings), "combined_hours": max(r["combined_hours"] for r in readings),
           "span_hours": max(r["span_hours"] for r in readings), "breaks_hours": worst["breaks_hours"],
           "sleep_before_hours": least("sleep_before_hours"), "sleep_after_hours": least("sleep_after_hours"),
           "weekly_hours": max(r["weekly_hours"] for r in readings)}
    out.update(level=worst["level"] if confirmed or worst["level"] != "green" else "unconfirmed", confirmed=confirmed,
               time_basis=zone["basis"], zones=zone["zones"], zone_evidence=zone["evidence"],
               days=shift_days, days_basis="stated" if days_stated else "assumed Monday to Friday (not stated)",
               readings=[{k: r[k] for k in ("zone", "level", "overlap_minutes", "combined_hours", "span_hours")} for r in readings])
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    reasons = [r for r, bad in (("the shift's time zone isn't resolved", zone["basis"] != "resolved"),
                                ("its days aren't stated", not days_stated),
                                ("the hours may describe the business, not the shift", not hours_direct),
                                ("your day job's time zone isn't set as a region name (e.g. America/Chicago)", home is None)) if bad]
    out["basis"] = (f"shift {fmt(shift[0])}-{fmt(shift[1])} ({zone['evidence']}), {', '.join(shift_days)}"
                    f"{'' if days_stated else ' assumed'}; with your day job: up to {out['overlap_minutes']} min overlap a day, "
                    f"{out['combined_hours']:g} h worked in a {out['span_hours']:g} h duty span"
                    + (f" (breaks {', '.join(f'{b:g} h' for b in out['breaks_hours'])})" if out["breaks_hours"] else "")
                    + (f", {out['sleep_before_hours']:g} h off before" if out["sleep_before_hours"] is not None else "")
                    + (f", {out['sleep_after_hours']:g} h off after" if out["sleep_after_hours"] is not None else "")
                    + f", up to {out['weekly_hours']:g} h a week"
                    + (f". Unconfirmed: {'; '.join(reasons)}" if reasons else ""))
    return out
