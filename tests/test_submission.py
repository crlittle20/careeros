"""Submission permission (R2, 2026-10-16): holds read from policy, identity resolved now, per-posting-and-channel releases,
authorization that never skips eligibility or competing-submission checks, and recording that never releases anything.
Synthetic names only: the fictional Northgate Group family mirrors a real one (tests/fixtures/synthetic_overrides.yaml)."""
import argparse
import io
import json
import re
import sqlite3
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from careeros.cli import cmd_release
from careeros.core.fingerprint import fingerprint
from careeros.core.policy import PolicyBundle, deep_merge, load_policy
from careeros.paths import CONFIG_DIR, ENGINE_ROOT
from careeros.pipeline import Context, _night_fingerprint, decision_fingerprint
from careeros.qualify import night
from careeros.qualify.gate import GateResult, held_rule, state_for
from careeros.qualify.profile import Profile, load_profile
from careeros.qualify.scores import tier_for
from careeros.review import submission
from careeros.review.applications import ApplicationHold, application_matrix, record_application
from careeros.review.labels import record_label
from careeros.review.report import _plain_card
from careeros.review.shortlist import shortlist

from tests.helpers import JOB_MATRIX, add_job, new_store

FIXTURES = Path(__file__).parent / "fixtures"
BASE = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")   # gate.held_parents only
HOLD = {"id": "ng-hold", "names": ["Northgate Group", "Northgate", "Bridgewell Claims", "NorthgateCare"], "channels": "all",
        "reason": "agreement terms not verified"}
APP = application_matrix()


def policy_with(extra: dict) -> PolicyBundle:
    data = deep_merge(BASE.data, extra)
    return PolicyBundle(data=data, fingerprint=fingerprint(data))


POLICY = policy_with({"submission": {"holds": [HOLD], "representation": {"other_agencies_allowed": True}},
                      "employer_identity": {"different_organisations": ["Northgate Care Workers"]}})
NO_HOLDS = policy_with({"submission": {"holds": []}})
CURRENT = SimpleNamespace(fingerprint="p")   # the profile the fixture's gate decisions were made with


def gate(store, job_id, decision="eligible", policy=None):
    """A gate decision made under `policy`'s rules (by default the fixture's; submission settings never change it)."""
    from types import SimpleNamespace

    from careeros.pipeline import decision_fingerprint
    store.clock.advance(seconds=1)
    store.writer("qualify").insert("gate_results", {"result_id": f"g:{uuid.uuid4().hex}", "job_id": job_id, "at": store.clock.now(),
                                                    "policy_fingerprint": decision_fingerprint(SimpleNamespace(policy=policy or POLICY)),
                                                    "profile_fingerprint": "p", "extraction_fingerprint": "e", "decision": decision})


def score(store, job_id, state, tier):
    store.writer("qualify").insert("scores", {"score_id": f"s:{uuid.uuid4().hex}", "job_id": job_id, "at": store.clock.now(),
                                              "policy_fingerprint": "p", "profile_fingerprint": "p", "tier": tier,
                                              "interview_band": "Low", "components_json": "{}", "state": state})


def codes(result) -> list[str]:
    return [r["code"] for r in result.reasons]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = new_store()
        add_job(self.store, "gh:ng:1", employer_name="NorthgateRx", parent="NorthgateRx", requisition_id="R-1", current_state="scored")
        add_job(self.store, "gh:ng:2", employer_name="NorthgateRx", parent="NorthgateRx", requisition_id="R-1", current_state="scored",
                variant_of="gh:ng:1", location_raw="Another City")
        add_job(self.store, "gh:ng:3", employer_name="Bridgewell Claims", parent="Northgate Group", title="Claims Analyst",
                title_norm="claims analyst", current_state="scored")
        add_job(self.store, "gh:acme:1", current_state="scored", title="Data Analyst", title_norm="data analyst")
        for job_id in ("gh:ng:1", "gh:ng:2", "gh:ng:3", "gh:acme:1"):
            gate(self.store, job_id)

    def release(self, job_id="gh:ng:1", channel="direct", key=None, **kw):
        kw.setdefault("hold_id", "ng-hold")
        kw.setdefault("reason", "checked: direct applications allowed for this opening")
        return submission.release(self.store, POLICY, job_id, channel, key=key or uuid.uuid4().hex, **kw)

    def allowed(self, job_id="gh:ng:1", channel="direct", agency=None, policy=POLICY):
        return submission.authorize(self.store, policy, job_id, channel, agency, profile=CURRENT)


class HoldSourceTests(Fixture):
    def test_configured_holds_or_else_the_gate_list(self):
        self.assertEqual([h["id"] for h in submission.holds(POLICY)], ["ng-hold"])
        legacy = submission.holds(BASE)
        self.assertEqual((legacy[0]["id"], legacy[0]["names"]), (submission.LEGACY_HOLD, ["Held Parent Co", "Northgate Group"]))
        self.assertEqual(submission.holds(NO_HOLDS), [])   # set to none on purpose: no fallback
        for bad in ({"names": ["X"]}, {"id": "x", "names": []}, {"id": "x", "names": ["X"], "channels": ["fax"]}):
            with self.assertRaises(ValueError):
                submission.holds(policy_with({"submission": {"holds": [bad]}}))
        with self.assertRaises(ValueError):
            submission.holds(policy_with({"submission": {"holds": [HOLD, HOLD]}}))

    def test_held_through_the_family_never_through_a_stored_score(self):
        self.assertEqual([h.hold_id for h in submission.status(self.store, POLICY, "gh:ng:1").holds], ["ng-hold"])   # by alias
        self.assertEqual([h.hold_id for h in submission.status(self.store, POLICY, "gh:ng:3").holds], ["ng-hold"])   # by parent
        score(self.store, "gh:ng:1", "ELIGIBLE", "A")
        self.assertTrue(submission.status(self.store, POLICY, "gh:ng:1").holds)            # Tier A changes nothing
        score(self.store, "gh:acme:1", "HOLD", "near_miss")
        self.assertEqual(submission.status(self.store, POLICY, "gh:acme:1").holds, [])     # a stale HOLD state means nothing
        add_job(self.store, "gh:union:1", employer_name="Northgate Care Workers East", parent="Northgate Care Workers East")
        self.assertEqual(submission.status(self.store, POLICY, "gh:union:1").holds, [])    # a listed different organisation

    def test_an_agency_signal_name_is_never_authorized_until_the_employer_is_known(self):
        add_job(self.store, "gh:hl:1", employer_name="Harborline Talent", parent="Harborline Talent", current_state="scored")
        gate(self.store, "gh:hl:1")
        self.assertIn("employer_identity_unresolved", codes(self.allowed("gh:hl:1")))
        self.assertIn("employer_identity_unresolved", codes(self.allowed("gh:hl:1", policy=NO_HOLDS)))
        with self.assertRaisesRegex(ValueError, "employer_identity_unresolved"):
            self.release("gh:hl:1")

    def test_a_hold_name_that_names_no_family_is_matched_like_one(self):
        """2026-10-17: a hold on a name outside `parents` covers its legal, compact and longer forms, not the exact name only."""
        own = policy_with({"submission": {"holds": [{"id": "own-hold", "names": ["Held Parent"], "channels": "all", "reason": "r"}]}})
        for n, name in enumerate(("Held Parent Services", "HeldParent", "Held Parent, Inc.")):
            add_job(self.store, f"gh:hp:{n}", employer_name=name, parent=name)
            self.assertEqual([h.hold_id for h in submission.status(self.store, own, f"gh:hp:{n}").holds], ["own-hold"], name)
        add_job(self.store, "gh:hp:x", employer_name="Heldparentage Labs", parent="Heldparentage Labs")
        self.assertEqual(submission.status(self.store, own, "gh:hp:x").holds, [])


class ReviewFixTests(Fixture):
    """Fresh-context Claude review of 4486b82 (2026-10-18): F1 strict holds, F2 current eligibility, F4 the shortlist's
    next action, F5 broken settings never refuse a record, F9 recording metadata per agency."""

    def test_a_mistyped_hold_never_holds_nothing_in_silence(self):
        for bad in ({"id": "x", "names": "Northgate Group"}, {"id": "x", "names": ["Northgate Group"], "channels": []},
                    {"id": "x", "names": ["Inc."]}, {"id": "x", "names": [7]}):
            broken = policy_with({"submission": {"holds": [bad]}})
            with self.assertRaises(submission.HoldSettingsInvalid, msg=repr(bad)):
                self.allowed(policy=broken)
            with self.assertRaises(submission.HoldSettingsInvalid):
                self.release(policy=broken) if False else submission.release(self.store, broken, "gh:ng:1", "direct",
                                                                              hold_id="x", reason="r", key=uuid.uuid4().hex)
        for bad in ({"ng-hold": HOLD}, ["Northgate Group"]):
            with self.assertRaises(submission.HoldSettingsInvalid):
                submission.holds(policy_with({"submission": {"holds": bad}}))
        self.assertIn("settings are invalid", submission.display(self.store, policy_with({"submission": {"holds": ["x"]}}), "gh:ng:1"))

    def test_broken_hold_settings_never_refuse_a_record_of_what_you_did(self):
        broken = policy_with({"submission": {"holds": [{"id": "x", "names": "Northgate Group"}]}})
        with self.assertRaisesRegex(ApplicationHold, "settings are invalid"):
            record_application(self.store, APP, "gh:ng:1", "direct", policy=broken, key="k-broken")
        app_id = record_application(self.store, APP, "gh:ng:1", "direct", policy=broken, key="k-broken-2",
                                    confirm="applied on the employer site; the hold settings were being fixed")
        self.assertTrue(app_id.startswith("app:"))

    def test_eligibility_must_be_current(self):
        add_job(self.store, "gh:acme:8", current_state="normalized", title="Data Analyst", title_norm="data analyst")
        gate(self.store, "gh:acme:8")                 # an eligible row from before a posting edit sent it back
        self.assertIn("eligibility_not_current", codes(self.allowed("gh:acme:8", policy=NO_HOLDS)))
        add_job(self.store, "gh:acme:9", current_state="scored", title="Data Analyst", title_norm="data analyst")
        gate(self.store, "gh:acme:9", policy=policy_with({"gate": {"held_parents": ["Somebody Else"]}}))   # older rules
        self.assertIn("eligibility_not_current", codes(self.allowed("gh:acme:9", policy=NO_HOLDS)))
        gate(self.store, "gh:acme:9")
        self.assertEqual(codes(self.allowed("gh:acme:9", policy=NO_HOLDS)), [])
        changed = load_profile(FIXTURES / "synthetic_profile.yaml")
        self.assertIn("eligibility_not_current",
                      codes(submission.authorize(self.store, NO_HOLDS, "gh:acme:9", "direct", profile=changed)))   # "p" is not its fingerprint

    def test_a_change_at_the_same_moment_as_the_release_suspends_it(self):
        from careeros.openings import record_change
        self.release()
        record_change(self.store, "gh:ng:1", [], extra="pay changed")          # same clock reading as the release
        self.assertIn("submission_held", codes(self.allowed()))

    def test_recording_lists_only_this_agency_s_releases(self):
        self.release(channel="agency", agency="Harbor Staffing")
        record_application(self.store, APP, "gh:ng:1", "agency", agency="Bayview Staffing", policy=POLICY, key="k-bay",
                           confirm="Bayview submitted me; Harbor's release is not mine to use")
        event = self.store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded' ORDER BY seq DESC LIMIT 1")
        self.assertEqual(json.loads(event["metadata_json"])["submission"]["releases"], [])

    def test_a_hold_on_a_guarded_name_never_lets_its_longer_forms_through(self):
        guarded = policy_with({"submission": {"holds": [{"id": "union", "names": ["Northgate Care Workers"], "channels": "all",
                                                         "reason": "r"}]},
                               "employer_identity": {"different_organisations": ["Northgate Care Workers"]}})
        add_job(self.store, "gh:u:1", employer_name="Northgate Care Workers East", parent="Northgate Care Workers East",
                current_state="scored")
        gate(self.store, "gh:u:1")
        self.assertFalse(submission.authorize(self.store, guarded, "gh:u:1", "direct", profile=CURRENT).allowed)

    def test_the_shortlist_says_prepare_only_while_held(self):
        from careeros.review.shortlist import next_action
        item = {"status": "scored", "applied": None, "incompatible": [], "unknowns": [], "possibly_applied": [], "apply_url": "u",
                "submission": "Submission held (ng-hold): agreement terms not verified"}
        self.assertTrue(next_action(item).startswith("prepare only, don't submit yet"))
        self.assertTrue(next_action({**item, "submission": None}).startswith("apply: "))


class AuthorizationTests(Fixture):
    def test_every_channel_is_held(self):
        for channel, agency in (("direct", None), ("agency", "Harbor Staffing"), ("referral", None), ("other", None)):
            result = self.allowed(channel=channel, agency=agency)
            self.assertEqual((result.allowed, codes(result)), (False, ["submission_held"]), channel)
        self.assertTrue(self.allowed("gh:acme:1").allowed)   # an employer no hold covers

    def test_a_release_clears_one_hold_for_one_posting_and_one_channel(self):
        release_id = self.release()
        self.assertTrue(self.allowed().allowed)
        self.assertEqual(codes(self.allowed(channel="referral")), ["submission_held"])
        self.assertEqual(codes(self.allowed(channel="agency", agency="Harbor Staffing")), ["submission_held"])
        self.assertEqual(codes(self.allowed("gh:ng:2")), ["submission_held"])   # the same role in another city
        self.assertEqual(codes(self.allowed("gh:ng:3")), ["submission_held"])   # the same family
        submission.revoke(self.store, release_id, reason="changed my mind", key="rv1")
        self.assertEqual(codes(self.allowed()), ["submission_held"])

    def test_an_agency_release_is_for_that_agency(self):
        self.release(channel="agency", agency="Harbor Staffing, Inc.")
        self.assertTrue(self.allowed(channel="agency", agency="harbor staffing").allowed)
        self.assertEqual(codes(self.allowed(channel="agency", agency="Bayview Partners")), ["submission_held"])
        self.assertEqual(codes(self.allowed()), ["submission_held"])

    def test_release_refusals_and_replays(self):
        cases = {"not_held_by": dict(hold_id="other-hold"), "reason_required": dict(reason="  "),
                 "agency_name_required": dict(channel="agency"), "agency_only_with": dict(agency="Harbor Staffing")}
        for message, kw in cases.items():
            with self.assertRaisesRegex(ValueError, message):
                self.release(**kw)
        with self.assertRaisesRegex(ValueError, "not_held_by"):
            self.release("gh:acme:1")
        first = self.release(key="same")
        self.assertEqual(self.release(key="same"), "replay")
        with self.assertRaisesRegex(ValueError, "already_released"):
            self.release()
        add_job(self.store, "gh:conf:1", employer_name="Confidential Healthcare Client", parent="Confidential Healthcare Client")
        with self.assertRaisesRegex(ValueError, "employer_identity_unresolved"):
            self.release("gh:conf:1")
        with self.assertRaisesRegex(ValueError, "unknown_release"):
            submission.revoke(self.store, "rel:nope", reason="x", key="r1")
        submission.revoke(self.store, first, reason="x", key="r2")
        with self.assertRaisesRegex(ValueError, "already_revoked"):
            submission.revoke(self.store, first, reason="x", key="r3")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM submission_releases")["c"], 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
            self.store.conn.execute("UPDATE submission_releases SET reason = 'edited'")

    def test_a_material_change_or_a_new_requisition_suspends_a_release(self):
        self.release()
        self.clock.advance(minutes=1)
        self.store.writer("discovery").insert("job_changes", {"change_id": "c0", "job_id": "gh:ng:1", "at": self.clock.now(),
                                                              "meaningful": 0, "fields_json": "[]", "summary": "cosmetic edit only"})
        self.assertTrue(self.allowed().allowed)
        self.store.writer("discovery").insert("job_changes", {"change_id": "c1", "job_id": "gh:ng:1", "at": self.clock.now(),
                                                              "meaningful": 1, "fields_json": "[]", "summary": "pay: 30 -> 25"})
        self.assertEqual(codes(self.allowed()), ["submission_held"])
        suspended = submission.status(self.store, POLICY, "gh:ng:1").holds[0].suspended
        self.assertIn("changed materially", suspended[0]["problem"])
        self.release("gh:ng:3")
        self.store.writer("discovery").update("jobs", {"requisition_id": "R-9"}, "job_id = ?", ("gh:ng:3",))
        self.assertEqual(codes(self.allowed("gh:ng:3")), ["submission_held"])

    def test_an_identity_correction_suspends_and_an_unresolved_identity_never_authorizes(self):
        self.release()
        self.store.writer("discovery").update("jobs", {"parent": "CVS Health"}, "job_id = ?", ("gh:ng:1",))
        self.assertEqual(codes(self.allowed()), ["employer_identity_unresolved"])   # conflicting names: never either one
        for n, (employer, parent) in enumerate((("Confidential", "Confidential"), ("TEKsystems", "TEKsystems"), ("", ""))):
            add_job(self.store, f"gh:unknown:{n}", employer_name=employer, parent=parent, current_state="scored")
            gate(self.store, f"gh:unknown:{n}")
            self.assertEqual(codes(self.allowed(f"gh:unknown:{n}", policy=NO_HOLDS)), ["employer_identity_unresolved"], employer)

    def test_a_release_is_for_the_employer_it_was_granted_under(self):
        two = policy_with({"submission": {"holds": [{"id": "two", "names": ["Northgate Group", "Held Parent Co"], "channels": "all",
                                                       "reason": "both"}]}})
        add_job(self.store, "gh:hp:1", employer_name="Brightpath Analytics", parent="Held Parent Co", current_state="scored")
        gate(self.store, "gh:hp:1")
        submission.release(self.store, two, "gh:hp:1", "direct", hold_id="two", reason="checked", key="t1")
        self.assertTrue(submission.authorize(self.store, two, "gh:hp:1", "direct", profile=CURRENT).allowed)
        self.store.writer("discovery").update("jobs", {"parent": "Northgate Group"}, "job_id = ?", ("gh:hp:1",))   # corrected
        after = submission.authorize(self.store, two, "gh:hp:1", "direct", profile=CURRENT)
        self.assertEqual(codes(after), ["submission_held"])        # still held by the same hold, but not for this employer
        self.assertIn("identity changed", after.status.holds[0].suspended[0]["problem"])

    def test_a_release_never_skips_eligibility_or_competing_submissions(self):
        self.release()
        gate(self.store, "gh:ng:1", "rejected")
        self.assertEqual(codes(self.allowed()), ["not_eligible"])
        gate(self.store, "gh:ng:1", "eligible")
        add_job(self.store, "gh:ng:9", employer_name="Northgate", parent="Northgate", current_state="closed",
                closed_at="2026-10-10T00:00:00.000Z")
        self.assertIn("posting_not_open", codes(self.allowed("gh:ng:9")))
        add_job(self.store, "gh:acme:2", title="Pricing Analyst", title_norm="pricing analyst")
        self.assertEqual(codes(self.allowed("gh:acme:2")), ["not_eligible"])   # never judged by the gate
        record_application(self.store, APP, "gh:ng:1", "agency", policy=POLICY, agency="Harbor Staffing", key="a1",
                           confirm="submitted by the agency before the hold")
        self.assertEqual(codes(self.allowed()), ["right_to_represent_conflict"])   # other agencies allowed, not twice
        self.assertIn("already_applied", codes(self.allowed(channel="agency", agency="Harbor Staffing")))


class RecordingTests(Fixture):
    def test_recording_is_history_it_never_releases_or_authorizes(self):
        with self.assertRaisesRegex(ApplicationHold, "submission is held"):
            record_application(self.store, APP, "gh:ng:1", "direct", policy=POLICY, key="r1")
        app_id = record_application(self.store, APP, "gh:ng:1", "direct", policy=POLICY, key="r2", confirm="I applied myself")
        self.assertEqual(record_application(self.store, APP, "gh:ng:1", "direct", policy=POLICY, key="r2", confirm="I applied myself"),
                         "replay")
        meta = json.loads(self.store.one("SELECT metadata_json FROM events WHERE event_type = 'application_recorded' AND entity_id = ?",
                                         (app_id,))["metadata_json"])
        self.assertEqual(meta["submission"], {"identity": "resolved", "family": "Northgate Group", "held": ["ng-hold"],
                                              "recorded_while_held": True, "releases": []})
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM submission_releases")["c"], 0)
        self.assertIn("submission_held", codes(self.allowed("gh:ng:3")))   # nothing about the family changed

    def test_an_unidentified_employer_needs_an_explanation_to_record(self):
        add_job(self.store, "gh:conf:1", employer_name="Confidential", parent="Confidential")
        with self.assertRaisesRegex(ApplicationHold, "isn't identified") as caught:
            record_application(self.store, APP, "gh:conf:1", "direct", policy=NO_HOLDS, key="c1")
        self.assertEqual(caught.exception.check.codes("hold"), ["employer_identity_unresolved"])
        record_application(self.store, APP, "gh:conf:1", "direct", policy=NO_HOLDS, key="c2", confirm="the recruiter named the client")


class FingerprintTests(Fixture):
    def test_submission_settings_and_releases_never_re_judge_a_job(self):
        """Submission settings and releases never re-judge PRIMARY; representation settings and releases never re-judge
        NIGHT. The hold list does re-judge NIGHT only (2026-10-18): held employers count as familiar for its conflict check."""
        held = policy_with({"submission": {"holds": [HOLD], "representation": {"other_agencies_allowed": True}}})
        self.assertEqual(decision_fingerprint(SimpleNamespace(policy=BASE)), decision_fingerprint(SimpleNamespace(policy=held)))
        profile = load_profile(FIXTURES / "synthetic_profile.yaml")
        job = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:ng:1'")
        represented = policy_with({"submission": {"holds": [HOLD], "representation": {"other_agencies_allowed": False}}})
        night = [_night_fingerprint(Context(self.store, p, profile, JOB_MATRIX), job) for p in (held, represented)]
        self.release()
        night.append(_night_fingerprint(Context(self.store, held, profile, JOB_MATRIX), job))
        self.assertEqual(len(set(night)), 1)
        self.assertNotEqual(*(_night_fingerprint(Context(self.store, p, profile, JOB_MATRIX), job) for p in (NO_HOLDS, held)))

    def test_identity_settings_re_judge_night_only(self):
        """employer_identity is read by NIGHT's conflict check (R3), so it is in NIGHT's fingerprint, never PRIMARY's."""
        guard = policy_with({"employer_identity": {"different_organisations": ["Northgate Care Workers"]}})
        self.assertEqual(decision_fingerprint(SimpleNamespace(policy=BASE)), decision_fingerprint(SimpleNamespace(policy=guard)))
        profile = load_profile(FIXTURES / "synthetic_profile.yaml")
        job = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:ng:1'")
        self.assertNotEqual(*(_night_fingerprint(Context(self.store, p, profile, JOB_MATRIX), job) for p in (BASE, guard)))


class D13MovedTests(Fixture):
    """R3: the held employer is judged like any other and held only for submission; generic eligibility holds remain."""
    MOVED = policy_with({"gate": {"held_parents": []}, "submission": {"holds": [HOLD]}})

    def test_qualification_no_longer_holds_the_employer_but_submission_still_does(self):
        job = dict(self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:ng:3'"))   # parent: Northgate Group
        moved, generic = GateResult("eligible"), GateResult("eligible")
        held_rule(moved, job, self.MOVED)
        held_rule(generic, job, BASE)                       # the fixture still lists the family in gate.held_parents
        self.assertEqual((moved.flags, state_for(moved, self.MOVED)), ([], "ELIGIBLE"))
        self.assertEqual(state_for(generic, BASE), "HOLD")   # generic eligibility holds still work
        self.assertEqual(tier_for(95, "ELIGIBLE", 1.0, self.MOVED), "A")
        gate(self.store, "gh:ng:3", policy=self.MOVED)        # judged again under the rules that moved the hold
        self.assertEqual(codes(self.allowed("gh:ng:3", policy=self.MOVED)), ["submission_held"])

    def test_a_held_employer_counts_as_familiar_for_night(self):
        """2026-10-18: the employers your submission holds name are employers you are placed with."""
        base = load_profile(FIXTURES / "synthetic_profile.yaml")
        nobody = Profile(data={**base.data, "familiar_employers": []}, fingerprint="n")
        job = {"employer_name": "Bridgewell Claims", "parent": "Bridgewell Claims"}
        self.assertTrue(night.familiar_family(job, nobody, POLICY))
        self.assertFalse(night.familiar_family(job, nobody, NO_HOLDS))
        self.assertIn("same employer family as one you have worked for or are placed with",
                      night.coi_review({**job, "description_text": ""}, nobody, POLICY)["reasons"])

    def test_night_finds_a_familiar_family_through_its_aliases(self):
        base = load_profile(FIXTURES / "synthetic_profile.yaml")
        profile = Profile(data={**base.data, "familiar_employers": ["Northgate"]}, fingerprint="p")
        guard = policy_with({"employer_identity": {"different_organisations": ["Northgate Care Workers"]}})
        def familiar(employer, parent=None, policy=guard):
            return night.familiar_family({"employer_name": employer, "parent": parent or employer}, profile, policy)
        self.assertTrue(familiar("NorthgateRx"))                       # alias of the familiar family; not an exact name
        self.assertTrue(familiar("Brightpath Analytics", "Northgate Group"))
        self.assertFalse(familiar("Northgate Care Workers East"))      # a listed different organisation
        self.assertFalse(familiar("Acme Health"))
        self.assertFalse(familiar("Confidential"))
        self.assertTrue(night.familiar_family({"employer_name": "Example Health", "parent": None}, base, guard))   # exact, as before
        self.assertTrue(familiar("Northgate Rx", "Southwind Logistics"))   # the names disagree (a stale parent): still a prompt
        agency = Profile(data={**base.data, "familiar_employers": ["Brightpath Staffing"]}, fingerprint="q")
        for posted in ("Brightpath Staffing, LLC", "Brightpath Staffing Midwest"):
            self.assertTrue(night.familiar_family({"employer_name": posted, "parent": None}, agency, guard), posted)
        self.assertIn("same employer family as one you have worked for or are placed with",
                      night.coi_review({"employer_name": "NorthgateRx", "parent": "NorthgateRx", "description_text": ""},
                                       profile, guard)["reasons"])


class DisplayTests(Fixture):
    def test_the_card_and_the_shortlist_show_the_hold_apart_from_the_qualification(self):
        job = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:ng:1'")
        card = _plain_card(self.store, job, self.clock.now(), None, {}, policy=POLICY)
        self.assertIn("Submission held (ng-hold: agreement terms not verified)", card)
        self.assertIn("careeros release gh:ng:1 --hold ID", card)
        self.assertNotIn("Submission held", _plain_card(self.store, job, self.clock.now(), None, {}))   # no policy, no claim
        other = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:acme:1'")
        self.assertNotIn("Submission", _plain_card(self.store, other, self.clock.now(), None, {}, policy=POLICY))
        record_label(self.store, "gh:ng:1", "apply", None, "l1")
        items = {i["job_id"]: i for i in shortlist(self.store, POLICY)}
        self.assertIn("Submission held", items["gh:ng:1"]["submission"])
        self.release()
        self.assertIn("released for direct only", submission.badge(submission.status(self.store, POLICY, "gh:ng:1")))

    def test_the_release_command(self):
        def run(**kw):
            args = argparse.Namespace(**{"job_id": "gh:ng:1", "hold": None, "channel": None, "agency": None, "reason": None,
                                         "revoke": None, **kw})
            out = io.StringIO()
            with redirect_stdout(out):
                cmd_release(SimpleNamespace(store=self.store, policy=POLICY, profile=CURRENT), {}, args)
            return out.getvalue()
        shown = run()
        self.assertIn("employer identity: resolved -> Northgate Group", shown)
        self.assertIn("submit via direct: not now: submission_held", shown)
        released = run(hold="ng-hold", channel="direct", reason="checked the agreement for this opening")
        self.assertIn("submit via direct: allowed", released)
        release_id = self.store.one("SELECT release_id FROM submission_releases")["release_id"]
        self.assertIn(f"revoked {release_id}", run(job_id=None, revoke=release_id, reason="not yet"))
        with self.assertRaises(SystemExit):
            run(hold="ng-hold", reason="no channel given")


class NoUnregisteredSubmitterTests(unittest.TestCase):
    ALLOWED = {"careeros/discovery/gmail.py": "Google sign-in token requests",
               "careeros/discovery/github_inbox.py": "comments and the seen index in your own private inbox repository",
               "careeros/cli.py": "opens your browser for the Gmail sign-in",
               "careeros/review/server.py": "opens the local review page in your browser"}
    SENDS = re.compile(r'\.post\(|\.put\(|\.patch\(|\bsmtplib\b|\bsendmail\b|\bwebbrowser\b|\burlopen\(|"comment"|"-X", "PUT"|'
                       r'^\s*(?:import|from)\s+requests\b', re.M)

    def test_nothing_can_submit_an_application(self):
        """Code that can send something exists only where listed; none of it submits an application. A submission tool
        would have to be added to submission.ADAPTERS and call authorize() first."""
        self.assertEqual(submission.ADAPTERS, {})
        found = sorted(p.relative_to(ENGINE_ROOT).as_posix() for p in (ENGINE_ROOT / "careeros").rglob("*.py")
                       if self.SENDS.search(p.read_text(encoding="utf-8")))
        self.assertEqual(found, sorted(self.ALLOWED))


class CodexReviewTests(Fixture):
    """Independent Codex review of f984d15 (2026-10-20): each of these allowed a submission it could not justify."""

    def test_a_board_alias_to_a_known_agency_never_authorizes(self):
        agency = policy_with({"submission": {"holds": []}, "source_discovery": {"staffing_agencies": ["Harborline Staffing"]}})
        self.store.writer("admin").insert("employer_aliases", {"alias_norm": "hs", "employer_norm": "harborline staffing",
                                                               "relation": "alias", "verified_by": "you",
                                                               "verified_at": self.store.clock.now()})
        add_job(self.store, "gh:hs:1", employer_name="HS", parent="HS", current_state="scored", title="Data Analyst",
                title_norm="data analyst")
        gate(self.store, "gh:hs:1", policy=agency)
        result = submission.authorize(self.store, agency, "gh:hs:1", "direct", profile=CURRENT)
        self.assertIn("employer_identity_unresolved", codes(result))
        self.assertEqual(result.status.identity.reason, "agency_client_not_named")

    def test_authorization_needs_the_current_profile(self):
        add_job(self.store, "gh:acme:7", current_state="scored", title="Data Analyst", title_norm="data analyst")
        gate(self.store, "gh:acme:7")
        self.assertEqual(codes(submission.authorize(self.store, NO_HOLDS, "gh:acme:7", "direct", profile=CURRENT)), [])
        self.assertIn("eligibility_not_current", codes(submission.authorize(self.store, NO_HOLDS, "gh:acme:7", "direct")))

    def test_a_change_recorded_after_the_release_suspends_it_even_when_the_clock_went_back(self):
        from careeros.openings import record_change
        self.release()
        self.assertNotIn("submission_held", codes(self.allowed()))
        self.clock.advance(seconds=-5)                        # the system clock moved back after the release
        record_change(self.store, "gh:ng:1", [], extra="pay changed")
        self.assertIn("submission_held", codes(self.allowed()))

    def test_a_change_at_the_same_moment_as_the_release_suspends_it(self):
        """A change recorded at the release's own clock reading, even just before it, may not have been seen."""
        from careeros.openings import record_change
        record_change(self.store, "gh:ng:1", [], extra="pay changed")          # same clock reading as the release
        self.release()
        self.assertIn("submission_held", codes(self.allowed()))

    def test_an_older_release_without_a_change_count_must_be_given_again(self):
        """A release written before 2026-10-20 keeps no change count, so a change recorded behind a clock that went back
        could slip past it (independent Codex Pass 2, 2026-10-21). Your decision of 2026-10-21: it no longer counts."""
        key = submission.status(self.store, POLICY, "gh:ng:1").identity.key
        job = self.store.one("SELECT requisition_id, canonical_url FROM jobs WHERE job_id = ?", ("gh:ng:1",))
        self.store.writer("submission").insert("submission_releases", {
            "release_id": "rel:older", "action": "release", "revokes": None, "job_id": "gh:ng:1", "hold_id": "ng-hold",
            "channel": "direct", "agency_norm": "", "agency": None, "reason": "released before change counts were kept",
            "family_key": key, "identity_json": "[]", "content_fingerprint": None, "requisition_id": job["requisition_id"],
            "canonical_url": job["canonical_url"], "at": self.store.clock.now(), "actor": "user"})
        self.assertIn("submission_held", codes(self.allowed()))               # no change at all: it still doesn't count
        suspended = submission.status(self.store, POLICY, "gh:ng:1").holds[0].suspended
        self.assertEqual([s["problem"] for s in suspended], ["the release predates change tracking; release the posting again"])
        self.release()                                                          # a fresh release keeps the change count
        self.assertNotIn("submission_held", codes(self.allowed()))

if __name__ == "__main__":
    unittest.main()
