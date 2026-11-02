"""Review page (the batch of 2026-11-01): fewer clicks and fewer mistakes per posting. The job id on cards and shortlist
lines (OPP-15); a verdict line in `careeros explain` and on the card (OPP-03); how long each shortlist item has waited
(OPP-08 + OPP-17); the label and "I applied" forms on the shortlist card (OPP-75; the session guard, OPP-77, is in
test_assist.py); a confirmation naming the posting before Dismiss and "I applied" record (OPP-16, ADR 0007 amendment of
2026-11-01). Synthetic data only; no real notification is shown."""
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from careeros import cli
from careeros.pipeline import Context
from careeros.review.explain import _verdict
from careeros.review.labels import record_label
from careeros.review.report import _plain_card, build_report

from tests.helpers import JOB_MATRIX, add_job
from tests.test_label_findings import PROFILE
from tests.test_observability import NOW, StatusMixin, ago, scored
from tests.test_saved import POLICY
from tests.test_v21_policy import policy_with


def card_of(page: str, card_id: str) -> str:
    """One card of the page, from its id to the next card."""
    start = page.index(f'<div class="card" id="{card_id}">')
    end = page.find('<div class="card', start + 1)
    return page[start:end if end != -1 else len(page)]


class JobIdTests(StatusMixin, unittest.TestCase):
    """OPP-15: label, package, assist and app-status all need the id, so cards show it as copyable text (escaped like
    every other value) and `careeros shortlist` prints it on each item's line."""

    def shortlist_cli(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_shortlist(Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None), {}, SimpleNamespace())
        return out.getvalue()

    def test_the_plain_card_the_shortlist_card_and_the_applications_card_show_the_id(self):
        record_label(self.store, scored(self.store, "gh:a&b:1", "B", title="Claims Analyst"), "apply", None, key="k1")
        self.store.writer("tracker").insert("applications", {
            "application_id": "app:<1>", "workflow_id": "application:app:1", "job_id": "gh:a&b:1", "channel": "direct",
            "applied_at": ago(2), "first_seen_at": ago(2), "current_state": "applied", "entered_state_at": ago(2)})
        job = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:a&b:1'")
        card = _plain_card(self.store, job, self.store.clock.now(), None, {})
        self.assertIn('<div class="row"><span><code class="jid">gh:a&amp;b:1</code></span>', card)   # first in its row, escaped
        page = build_report(self.store, PROFILE, POLICY)
        self.assertIn('<div class="row"><span><code class="jid">gh:a&amp;b:1</code></span>', card_of(page, "sl-gh:a&amp;b:1"))
        self.assertIn('<div class="row"><span><code class="jid">app:&lt;1&gt;</code></span>', card_of(page, "app:&lt;1&gt;"))
        self.assertNotIn("app:<1>", page)

    def test_the_shortlist_command_prints_the_id_on_each_item(self):
        self.assertIn("Shortlist is empty", self.shortlist_cli())
        scored(self.store, "gh:a:1", "A", title="Claims Analyst", employer_name="Synth Health")
        record_label(self.store, scored(self.store, "gh:a:2", "near_miss", title="Rebate Analyst"), "stretch", None, key="k1")
        items = [line for line in self.shortlist_cli().splitlines() if line.startswith("- ")]
        self.assertEqual(sorted(items), ["- gh:a:1 | Claims Analyst | Synth Health | open | tier A None",
                                         "- gh:a:2 | Rebate Analyst | Acme Health | open | tier near_miss None"])


def judged(store, job_id: str, tier: str | None, priority: float = 80.0, state: str = "ELIGIBLE", coverage: float | None = 0.8,
           flags: tuple = (), failures: tuple = (), stretch: str | None = None, experience: dict | None = None,
           job_state: str | None = None) -> str:
    """A job with a stored gate result (flags or hard failures by code) and, when `tier` is given, a stored score."""
    add_job(store, job_id, current_state=job_state or ("scored" if tier else "rejected"))
    store.writer("qualify").insert("gate_results", {
        "result_id": f"g:{job_id}", "job_id": job_id, "at": store.clock.now(), "policy_fingerprint": "q",
        "profile_fingerprint": "p", "extraction_fingerprint": "e", "decision": "rejected" if failures else "eligible",
        "hard_failures_json": json.dumps([{"code": c, "detail": f"{c} detail"} for c in failures]),
        "flags_json": json.dumps([{"code": c, "detail": f"{c} detail"} for c in flags])})
    if tier:
        comps = {"eligibility": {"matched_skills": [], "missing_skills": []}, "candidate_fit": {"matched_skills": []},
                 "state": state, "skill_coverage": coverage, "stretch": bool(stretch), "stretch_reason": stretch,
                 **({"experience": experience} if experience else {})}
        store.writer("qualify").insert("scores", {
            "score_id": f"s:{job_id}", "job_id": job_id, "at": store.clock.now(), "profile_fingerprint": "p",
            "policy_fingerprint": "q", "eligibility": 90, "candidate_fit": 80, "career_value": 70, "freshness": 90,
            "apply_priority": priority, "tier": tier, "state": state, "interview_band": "Moderate",
            "components_json": json.dumps(comps)})
    return job_id


class VerdictTests(StatusMixin, unittest.TestCase):
    """OPP-03: one sentence, first in `careeros explain` and on the review card, from the stored gate result and score and
    the policy's current thresholds (Tier A 92, Tier B 85, near-miss 78, skill minimum 70% in the test policy)."""

    def verdict(self, job_id: str, policy=POLICY) -> str:
        return _verdict(self.store, policy, job_id)

    def test_a_rejection_names_every_hard_failure_code(self):
        judged(self.store, "gh:r:1", None, failures=("below_pay_floor", "clearance_required", "below_pay_floor"))
        self.assertEqual(self.verdict("gh:r:1"), "Rejected: below_pay_floor, clearance_required.")
        add_job(self.store, "gh:r:2")   # rejected without a gate result: the recorded reason
        self.store.writer("discovery").transition(JOB_MATRIX, "gh:r:2", "normalized")
        self.store.writer("discovery").transition(JOB_MATRIX, "gh:r:2", "rejected", reason_code="employer_excluded")
        self.assertEqual(self.verdict("gh:r:2"), "Rejected: employer_excluded.")

    def test_tier_a_and_the_priority_short_of_the_next_tier(self):
        judged(self.store, "gh:t:a", "A", priority=94)
        self.assertEqual(self.verdict("gh:t:a"), "Tier A: priority 94 meets the Tier A threshold of 92.")
        judged(self.store, "gh:t:b", "B", priority=88)
        self.assertEqual(self.verdict("gh:t:b"), "Tier B: priority 88 vs 92 for Tier A (4 short).")
        judged(self.store, "gh:t:near", "near_miss", priority=80.7)
        self.assertEqual(self.verdict("gh:t:near"), "Near-miss: priority 80.7 vs 85 for Tier B (4.3 short).")
        judged(self.store, "gh:t:archive", "archive", priority=70, state="CONDITIONAL", flags=("remote_unknown",), coverage=0.2)
        self.assertEqual(self.verdict("gh:t:archive"), "Archive: priority 70 vs 78 for near-miss (8 short).")   # near-miss needs priority only
        self.assertEqual(self.verdict("gh:t:near", policy_with(tiers__B__priority=83)),   # the current thresholds, not the stored ones
                         "Near-miss: priority 80.7 vs 83 for Tier B (2.3 short).")

    def test_the_flag_that_made_it_conditional_or_held(self):
        judged(self.store, "gh:c:1", "near_miss", priority=88, state="CONDITIONAL",
               flags=("pay_not_posted", "remote_unknown", "years_unknown"))
        self.assertEqual(self.verdict("gh:c:1"), "Near-miss: CONDITIONAL on remote_unknown, years_unknown, which keeps a posting "
                                                 "out of Tier A/B (priority 88 would reach Tier B at 85).")
        judged(self.store, "gh:c:2", "near_miss", priority=80, state="CONDITIONAL", flags=("remote_unknown",))
        self.assertEqual(self.verdict("gh:c:2"), "Near-miss: CONDITIONAL on remote_unknown, which keeps a posting out of Tier A/B; "
                                                 "priority 80 vs 85 for Tier B (5 short).")
        judged(self.store, "gh:c:3", "near_miss", priority=95, state="HOLD", flags=("remote_unknown", "held_d13"))
        self.assertEqual(self.verdict("gh:c:3"), "Near-miss: HOLD on held_d13, which keeps a posting out of Tier A/B "
                                                 "(priority 95 would reach Tier A at 92).")

    def test_skill_coverage_below_the_minimum(self):
        judged(self.store, "gh:s:1", "near_miss", priority=88, coverage=0.55)
        self.assertEqual(self.verdict("gh:s:1"), "Near-miss: skill coverage 55% vs the 70% minimum for Tier A/B "
                                                 "(priority 88 would reach Tier B at 85).")
        judged(self.store, "gh:s:2", "near_miss", priority=81, coverage=0.55)
        self.assertEqual(self.verdict("gh:s:2"), "Near-miss: skill coverage 55% vs the 70% minimum for Tier A/B; "
                                                 "priority 81 vs 85 for Tier B (4 short).")

    def test_a_stretch_and_an_enforced_experience_match(self):
        judged(self.store, "gh:x:1", "near_miss", priority=90, stretch="degree")
        self.assertEqual(self.verdict("gh:x:1"), "Near-miss: a stretch (degree required) is shown with the near-misses, never as a "
                                                 "top pick (priority 90 would reach Tier B at 85).")
        judged(self.store, "gh:x:2", "B", priority=94, experience={"enforced": True, "tier": "A", "gap_class": "moderate_gap"})
        self.assertEqual(self.verdict("gh:x:2"), "Tier B: Experience Match (enforced) lowered it from Tier A (moderate gap) "
                                                 "(priority 94 would reach Tier A at 92).")
        judged(self.store, "gh:x:3", "B", priority=88, experience={"enforced": False, "tier": "B", "gap_class": "severe_gap"})
        self.assertEqual(self.verdict("gh:x:3"), "Tier B: priority 88 vs 92 for Tier A (4 short).")   # shadow mode: not a reason

    def test_a_posting_that_was_not_scored(self):
        add_job(self.store, "gh:n:1")
        self.assertEqual(self.verdict("gh:n:1"), "Not scored yet (state discovered).")
        add_job(self.store, "gh:n:2", current_state="duplicate", duplicate_of="gh:n:1")
        self.assertEqual(self.verdict("gh:n:2"), "Duplicate of gh:n:1: judged with that posting, never on its own.")
        add_job(self.store, "gh:n:3")
        self.store.writer("discovery").transition(JOB_MATRIX, "gh:n:3", "normalized")
        self.store.writer("discovery").transition(JOB_MATRIX, "gh:n:3", "needs_review", reason_code="requirements_unclear")
        self.assertEqual(self.verdict("gh:n:3"), "Not scored: parked for your review (requirements_unclear).")

    def test_explain_prints_it_first_and_the_card_shows_it_first(self):
        judged(self.store, "gh:t:<near>", "near_miss", priority=81, coverage=None)   # no coverage: the card has no basis to show
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_explain(Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None), None,
                            SimpleNamespace(job_id="gh:t:<near>", json=False))
        self.assertEqual(out.getvalue().splitlines()[:2], ["Verdict: Near-miss: priority 81 vs 85 for Tier B (4 short).",
                                                           "Why gh:t:<near> is where it is"])
        job = self.store.one("SELECT * FROM jobs WHERE job_id = 'gh:t:<near>'")
        card = _plain_card(self.store, job, self.store.clock.now(), None, {}, policy=POLICY)
        self.assertIn('</h3><div class="verdict">Near-miss: priority 81 vs 85 for Tier B (4 short).</div><div class="row">', card)
        self.assertNotIn('class="verdict"', _plain_card(self.store, job, self.store.clock.now(), None, {}))   # no policy, no thresholds


def label_at(store, job_id: str, label: str, days: float) -> None:
    """Your PRIMARY label, given `days` ago."""
    store.writer("review").insert("labels", {"label_id": f"label:{job_id}:{days}", "job_id": job_id, "at": ago(days),
                                             "label": label, "profile": "primary"})


def package_at(store, job_id: str, version: int, days: float) -> None:
    store.writer("assist").insert("packages", {
        "package_id": f"pkg:{job_id}:{version}", "job_id": job_id, "version": version, "route": "apply", "basis": "label",
        "manifest_sha256": "0" * 64, "manifest_json": "{}", "folder": f"packages/{version}", "at": ago(days), "actor": "user"})


class AgeingTests(StatusMixin, unittest.TestCase):
    """OPP-08 + OPP-17: each shortlist item says how long it has waited, CareerOS's first sighting first, then the label's
    age and the latest package; an unapplied Apply/Stretch item labelled 7 or more days ago asks you to apply or dismiss."""

    def items(self) -> dict:
        from careeros.review.shortlist import shortlist
        return {i["job_id"]: i for i in shortlist(self.store, POLICY)}

    def test_first_seen_label_age_and_the_latest_package(self):
        label_at(self.store, scored(self.store, "gh:p:1", "near_miss", days=9.5), "apply", days=8.2)
        package_at(self.store, "gh:p:1", 1, days=6)
        package_at(self.store, "gh:p:1", 2, days=3)
        scored(self.store, "gh:p:2", "A", days=0.5)   # a Tier A pick: no label, no package
        from careeros.review.shortlist import ageing
        items = self.items()
        self.assertEqual((items["gh:p:1"]["seen_days"], items["gh:p:1"]["label_days"], items["gh:p:1"]["package"]),
                         (9, 8, {"version": 2, "days": 3}))
        self.assertEqual(ageing(items["gh:p:1"]), "first seen 9 days ago; labelled 8 days ago; package v2 made 3 days ago")
        self.assertEqual(ageing(items["gh:p:2"]), "first seen today")
        self.assertEqual(items["gh:p:1"]["next_action"], "apply or dismiss: labelled 8 days ago")
        self.assertTrue(items["gh:p:2"]["next_action"].startswith("apply: "))   # no label: never the label nudge
        page = build_report(self.store, PROFILE, POLICY)
        card = card_of(page, "sl-gh:p:1")
        self.assertIn('<div class="row">first seen 9 days ago; labelled 8 days ago; package v2 made 3 days ago</div>'
                      '<div class="ok"><b>Next:</b> apply or dismiss: labelled 8 days ago</div>', card)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_shortlist(Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None), {}, SimpleNamespace())
        lines = out.getvalue().splitlines()
        at = lines.index(next(line for line in lines if line.startswith("- gh:p:1 ")))
        self.assertEqual(lines[at + 1:at + 3], ["    first seen 9 days ago; labelled 8 days ago; package v2 made 3 days ago",
                                                "    next: apply or dismiss: labelled 8 days ago"])

    def test_apply_or_dismiss_only_for_an_unapplied_pursued_item_labelled_7_or_more_days_ago(self):
        label_at(self.store, scored(self.store, "gh:q:6", "B", days=10), "apply", days=6.9)
        label_at(self.store, scored(self.store, "gh:q:7", "near_miss", days=10), "stretch", days=7)
        label_at(self.store, scored(self.store, "gh:q:old", "near_miss", days=40, title="Rebate Analyst",
                                    title_norm="rebate analyst"), "apply", days=30)
        self.store.writer("tracker").insert("applications", {
            "application_id": "app:q", "workflow_id": "application:app:q", "job_id": "gh:q:old", "channel": "direct",
            "applied_at": ago(2), "first_seen_at": ago(2), "current_state": "applied", "entered_state_at": ago(2)})
        items = self.items()
        self.assertTrue(items["gh:q:6"]["next_action"].startswith("apply: "))
        self.assertEqual(items["gh:q:7"]["next_action"], "apply or dismiss: labelled 7 days ago")
        self.assertEqual(items["gh:q:old"]["next_action"], "wait for a response")   # applied: the application's clock instead

    def test_apply_or_dismiss_never_outranks_a_possible_application_an_incompatibility_or_a_hold(self):
        """Stage 1-2 review (2026-11-01): the label's age comes after every check, so CareerOS never nudges Chris to apply
        to a posting they may already have applied to."""
        from careeros.review.applications import application_matrix, record_application
        from careeros.review.shortlist import next_action
        label_at(self.store, scored(self.store, "gh:r:1", "near_miss", days=12, title="Rebate Analyst", title_norm="rebate analyst"),
                 "apply", days=9)
        scored(self.store, "gh:r:2", "archive", days=12, title="Rebate Analyst", title_norm="rebate analyst")   # another city
        record_application(self.store, application_matrix(), "gh:r:2", "direct", policy=POLICY, key="a1")
        self.assertEqual(self.items()["gh:r:1"]["next_action"], "check: you applied to a similar opening (gh:r:2)")
        base = {"status": "open", "applied": None, "submission": None, "label": "stretch", "label_days": 9, "incompatible": [],
                "unknowns": [], "possibly_applied": [], "apply_url": "https://jobs.example.com/1"}
        cases = {"possibly_applied": (["gh:x:1"], "check: you applied to a similar opening (gh:x:1)"),
                 "incompatible": ([{"detail": "pay below your floor"}], "dismiss it, or record why you would still apply: "
                                                                       "pay below your floor"),
                 "unknowns": ([{"detail": "schedule not stated"}], "verify before applying: schedule not stated"),
                 "submission": ("submission held", "prepare only, don't submit yet: submission held")}
        self.assertEqual(next_action(base), "apply or dismiss: labelled 9 days ago")
        for field, (value, expected) in cases.items():
            with self.subTest(field=field):
                self.assertEqual(next_action({**base, field: value}), expected)


class ShortlistFormTests(StatusMixin, unittest.TestCase):
    """OPP-75: the shortlist card has the label and "I applied" forms beside Dismiss: the plain card's own form helpers, so
    they post to the same endpoints with the server's protections (token, one-time key bound to action and target)."""

    def test_the_shortlist_card_offers_label_i_applied_and_dismiss(self):
        from tests.test_applications import forms
        record_label(self.store, scored(self.store, "gh:f:1", "near_miss"), "stretch", None, key="k1")
        static = card_of(build_report(self.store, PROFILE, POLICY), "sl-gh:f:1")   # no server: the commands instead
        for hint in ("careeros label gh:f:1 apply|stretch|skip", "careeros applied gh:f:1 --channel direct",
                     "careeros dismiss gh:f:1 --reason REASON"):
            self.assertIn(hint, static)
        keys: dict = {}
        card = card_of(build_report(self.store, PROFILE, POLICY, token="t", form_keys=keys), "sl-gh:f:1")
        found = forms(card)
        self.assertEqual([(a, t) for a, t, _ in found], [("label", "gh:f:1"), ("apply", "gh:f:1"), ("dismiss", "gh:f:1")])
        self.assertEqual([keys[k] for _, _, k in found], [("label", "gh:f:1"), ("apply", "gh:f:1"), ("dismiss", "gh:f:1")])
        self.assertIn('<button name="label" value="apply">Apply</button>', card)
        self.assertIn("<button>I applied</button>", card)

    def test_label_and_i_applied_from_the_shortlist_card_through_the_server(self):
        import threading
        from http.server import HTTPServer
        from pathlib import Path

        import httpx

        from careeros.review.server import make_handler
        from tests.test_applications import forms
        record_label(self.store, scored(self.store, "gh:f:2", "near_miss"), "stretch", None, key="k1")
        token = "shortlist-form-token"
        server = HTTPServer(("127.0.0.1", 0), make_handler(self.store, PROFILE, POLICY, token, Path(self.tmp.name) / "writer.lock", {}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            card = lambda: forms(card_of(httpx.get(f"{base}/?t={token}").text, "sl-gh:f:2"))
            label_key = next(k for a, t, k in card() if a == "label")
            reply = httpx.post(f"{base}/label?t={token}", data={"target": "gh:f:2", "key": label_key, "label": "apply", "reason": ""})
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(self.store.one("SELECT label FROM labels WHERE job_id = 'gh:f:2' ORDER BY at DESC, rowid DESC")["label"],
                             "apply")
            apply_key = next(k for a, t, k in card() if a == "apply")
            ask = httpx.post(f"{base}/apply?t={token}", data={"target": "gh:f:2", "key": apply_key, "channel": "direct",
                                                               "agency": "", "from": "sl"})
            confirm_key = next(k for a, t, k in forms(ask.text) if a == "confirm_apply")   # the confirmation first (OPP-16)
            reply = httpx.post(f"{base}/confirm_apply?t={token}", data={"target": "gh:f:2", "key": confirm_key, "channel": "direct",
                                                                         "agency": "", "from": "sl"})
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(self.store.one("SELECT job_id FROM applications")["job_id"], "gh:f:2")
            self.assertEqual([a for a, _, _ in card()], ["label", "dismiss"])   # applied: no second "I applied"
        finally:
            server.shutdown()
            server.server_close()


class ServerMixin(StatusMixin):
    """The review server on a free port, with the test store (synthetic data)."""
    token = "confirmation-token"

    def setUp(self):
        super().setUp()
        import threading
        from http.server import HTTPServer
        from pathlib import Path

        from careeros.review.server import make_handler
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.store, PROFILE, POLICY, self.token,
                                                                Path(self.tmp.name) / "writer.lock", {}))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self) -> str:
        import httpx
        return httpx.get(f"{self.base}/?t={self.token}").text

    def post(self, action: str, **data):
        import httpx
        return httpx.post(f"{self.base}/{action}?t={self.token}", data=data)

    def form(self, html: str, action: str, target: str) -> str:
        from html import unescape

        from tests.test_applications import forms
        return next(k for a, t, k in forms(html) if (a, unescape(t)) == (action, target))

    def count(self, table: str) -> int:
        return self.store.one(f"SELECT COUNT(*) c FROM {table}")["c"]


class ConfirmationTests(ServerMixin, unittest.TestCase):
    """OPP-16 and the owner's decision "Confirmation plus undo" (ADR 0007, amendment of 2026-11-01): Dismiss and "I applied"
    record nothing; they answer with a page naming the posting, and only its Confirm (a new one-time form) records. The
    dismiss reason has no preselected value. After an action the page returns to the card the form came from."""

    def test_dismiss_names_the_posting_and_reason_and_only_confirm_records(self):
        job = scored(self.store, "gh:c:<1>", "B", title="Claims <Analyst>", employer_name="Synth & Co")
        page = self.get()
        card = card_of(page, "sl-gh:c:&lt;1&gt;")
        self.assertIn('<select name="reason" required><option value="">dismiss: choose a reason</option><option value="pay">', card)
        self.assertNotIn("selected", card)   # no reason is preselected
        key = self.form(card, "dismiss", job)
        self.assertEqual(self.post("dismiss", target=job, key=key, reason="", note="").status_code, 400)   # a reason is required
        ask = self.post("dismiss", target=job, key=key, reason="pay", note="below <my> floor", **{"from": "sl"})
        self.assertEqual((ask.status_code, self.count("dismissals")), (200, 0))   # asked, nothing recorded
        for shown in ("Dismiss this posting? Reason: <b>pay</b> (below &lt;my&gt; floor)", "<b>Claims &lt;Analyst&gt;</b>",
                      "Employer: Synth &amp; Co", "Job id: <code>gh:c:&lt;1&gt;</code>",
                      f'<a href="/?t={self.token}#sl-gh:c:%3C1%3E">Cancel</a>', "Content-Security-Policy"):
            self.assertIn(shown, ask.text)
        self.assertNotIn("Claims <Analyst>", ask.text)
        self.assertEqual(self.post("confirm_dismiss", target=job, key=key, reason="pay").status_code, 400)   # the first key can't confirm
        confirm = self.form(ask.text, "confirm_dismiss", job)
        reply = self.post("confirm_dismiss", target=job, key=confirm, reason="pay", note="below <my> floor", **{"from": "sl"})
        self.assertEqual((reply.status_code, reply.headers["location"]), (303, f"/?t={self.token}#sl-gh:c:%3C1%3E"))
        self.post("confirm_dismiss", target=job, key=confirm, reason="pay", note="below <my> floor", **{"from": "sl"})   # double click
        self.assertEqual([(r["job_id"], r["reason_code"], r["reason_text"]) for r in self.store.all("SELECT * FROM dismissals")],
                         [(job, "pay", "below <my> floor")])

    def test_i_applied_names_the_posting_and_channel_and_returns_to_its_card(self):
        record_label(self.store, scored(self.store, "gh:c:2", "B", title="Rebate Analyst"), "apply", None, key="k1")
        page = self.get()
        for origin, card_id in (("card", "gh:c:2"), ("sl", "sl-gh:c:2")):   # the posting's card, then the shortlist card
            key = self.form(card_of(page, card_id), "apply", "gh:c:2")
            self.assertIn(f'<input type="hidden" name="from" value="{origin}">', card_of(page, card_id))
            ask = self.post("apply", target="gh:c:2", key=key, channel="agency", agency="Harbor <Staffing>", **{"from": origin})
            self.assertEqual((ask.status_code, self.count("applications")), (200, 0))
            for shown in ("Record that you applied to this posting <b>via Harbor &lt;Staffing&gt;</b> (agency)?",
                          "<b>Rebate Analyst</b>", "Employer: Acme Health", "Job id: <code>gh:c:2</code>", "Confirm: I applied"):
                self.assertIn(shown, ask.text)
        confirm = self.form(ask.text, "confirm_apply", "gh:c:2")
        reply = self.post("confirm_apply", target="gh:c:2", key=confirm, channel="agency", agency="Harbor <Staffing>", **{"from": "sl"})
        self.assertEqual((reply.status_code, reply.headers["location"]), (303, f"/?t={self.token}#sl-gh:c:2"))
        self.assertEqual([(a["channel"], a["agency"]) for a in self.store.all("SELECT * FROM applications")],
                         [("agency", "Harbor <Staffing>")])
        self.assertEqual(self.post("apply", target="gh:c:2", key=key, channel="agency", agency="").status_code, 400)   # agency named
        from careeros.review.server import anchor_for
        self.assertEqual([anchor_for("gh:c:2", o) for o in ("card", "sl", "night", "x{0}")],
                         ["gh:c:2", "sl-gh:c:2", "night-gh:c:2", "gh:c:2"])

    def test_i_applied_shows_the_assist_close_command_instead_of_confirm_and_recording_checks_again(self):
        record_label(self.store, scored(self.store, "gh:c:3", "B"), "apply", None, key="k1")
        package_at(self.store, "gh:c:3", 1, days=1)

        def session(sid: str) -> None:
            self.store.writer("assist").insert("assist_sessions", {
                "session_id": sid, "job_id": "gh:c:3", "package_id": "pkg:gh:c:3:1", "manifest_sha256": "0" * 64,
                "mode": "manual", "role_json": "[]", "opened_at": self.store.clock.now(), "actor": "user"})
        ask = self.post("apply", target="gh:c:3", key=self.form(self.get(), "apply", "gh:c:3"), channel="direct", agency="")
        confirm = self.form(ask.text, "confirm_apply", "gh:c:3")   # offered: no session yet
        session("ses:first")   # a session opens before Confirm: recording refuses and names the command
        reply = self.post("confirm_apply", target="gh:c:3", key=confirm, channel="direct", agency="")
        self.assertEqual(reply.status_code, 409)
        self.assertIn("careeros assist close ses:first --outcome submitted</p>", reply.text)
        ask = self.post("apply", target="gh:c:3", key=self.form(self.get(), "apply", "gh:c:3"), channel="direct", agency="")
        self.assertEqual(ask.status_code, 409)   # asked while the session is open: the command instead of Confirm
        self.assertIn("<p>Run: <code>careeros assist close ses:first --outcome submitted</code></p>", ask.text)
        self.assertNotIn("confirm_apply", ask.text)
        self.assertEqual(self.count("applications"), 0)

    def test_confirm_records_only_the_choices_its_page_showed(self):
        """Codex CR-2026-11-01-OBS-REVIEW, F3: a confirmation's key is bound on the server to the reason, note or channel
        its page showed. A Confirm carrying other choices (a valid unused key, an altered form) records nothing and shows
        a fresh confirmation of them; the page's own choices still record, once."""
        job = scored(self.store, "gh:c:5", "B", title="Claims Analyst")
        ask = self.post("dismiss", target=job, key=self.form(self.get(), "dismiss", job), reason="pay", note="below floor")
        confirm = self.form(ask.text, "confirm_dismiss", job)
        for altered in ({"reason": "location", "note": "below floor"}, {"reason": "pay", "note": "something else"}):
            reply = self.post("confirm_dismiss", target=job, key=confirm, **altered)
            self.assertEqual((reply.status_code, self.count("dismissals")), (409, 0))
            self.assertIn("Not recorded: the choices differ from the ones the confirmation showed", reply.text)
            self.assertIn(f"Reason: <b>{altered['reason']}</b> ({altered['note']})", reply.text)   # asked again, as sent
        self.assertEqual(self.post("confirm_dismiss", target=job, key=confirm, reason="pay", note="below floor").status_code, 303)
        self.assertEqual([(r["reason_code"], r["reason_text"]) for r in self.store.all("SELECT * FROM dismissals")],
                         [("pay", "below floor")])

        other = scored(self.store, "gh:c:6", "B", title="Rebate Analyst", title_norm="rebate analyst")
        ask = self.post("apply", target=other, key=self.form(self.get(), "apply", other), channel="direct", agency="")
        confirm = self.form(ask.text, "confirm_apply", other)
        for altered in ({"channel": "referral", "agency": ""}, {"channel": "agency", "agency": "Harbor Staffing"}):
            reply = self.post("confirm_apply", target=other, key=confirm, **altered)
            self.assertEqual((reply.status_code, self.count("applications")), (409, 0))
            self.assertIn("Not recorded: the choices differ", reply.text)
        self.assertEqual(self.post("confirm_apply", target=other, key=confirm, channel="direct", agency="").status_code, 303)
        app = self.store.one("SELECT application_id, channel, agency FROM applications")
        self.assertEqual((app["channel"], app["agency"]), ("direct", None))

        ask = self.post("void", target=app["application_id"], key=self.form(self.get(), "void", app["application_id"]),
                        reason="wrong posting")
        confirm = self.form(ask.text, "confirm_void", app["application_id"])
        reply = self.post("confirm_void", target=app["application_id"], key=confirm, reason="another reason")
        self.assertEqual(reply.status_code, 409)
        self.assertIn("Your reason: <b>another reason</b>", reply.text)
        self.assertEqual(self.store.one("SELECT current_state FROM applications")["current_state"], "applied")
        self.assertEqual(self.post("confirm_void", target=app["application_id"], key=confirm, reason="wrong posting").status_code, 303)
        self.assertEqual(self.store.one("SELECT reason_code FROM transitions WHERE entity_id = ?", (app["application_id"],))[0],
                         "wrong posting")



class DismissalUndoTests(ServerMixin, unittest.TestCase):
    """The owner's decision "Confirmation plus undo" (ADR 0007, amendment of 2026-11-01): a dismissal is undone by a later
    event, a reinstatement, never by an edit; the latest event decides for every reader. `careeros dismiss JOB --undo`,
    or Undo in the review page's "Dismissed recently" section (14 days), through the same confirmation."""

    def test_a_reinstatement_is_a_later_event_and_the_latest_event_decides(self):
        import sqlite3

        from careeros.openings import application_status
        from careeros.review import dismiss as dis
        from careeros.review.labels import label_backlog
        from careeros.review.packages import route_for
        from careeros.review.shortlist import shortlisted_job_ids
        job = scored(self.store, "gh:u:1", "B", days=2)
        shown = lambda: (job in shortlisted_job_ids(self.store), (label_backlog(self.store) or {}).get("count", 0),   # noqa: E731
                         route_for(self.store, POLICY, job)[1] == "you dismissed it", dis.is_dismissed(self.store, job),
                         bool(application_status(self.store, job)["dismissed"]))
        self.assertEqual(shown(), (True, 1, False, False, False))
        with self.assertRaisesRegex(ValueError, "not_dismissed"):
            dis.reinstate(self.store, job)   # nothing to undo
        dis.dismiss(self.store, job, "pay")
        self.assertEqual(shown(), (False, 0, True, True, True))
        dis.reinstate(self.store, job, "I misread the pay")
        self.assertEqual(shown(), (True, 1, False, False, False))   # back everywhere
        with self.assertRaisesRegex(ValueError, "not_dismissed"):
            dis.reinstate(self.store, job)
        dis.dismiss(self.store, job, "location")   # a later dismissal dismisses it again
        self.assertEqual(shown(), (False, 0, True, True, True))
        self.assertEqual(dis.current(self.store, job)["reason_code"], "location")
        self.assertEqual([(r["reason_code"], r["reason_text"]) for r in self.store.all("SELECT * FROM dismissals ORDER BY rowid")],
                         [("pay", None), ("reinstated", "I misread the pay"), ("location", None)])   # append-only history
        for sql in ("UPDATE dismissals SET reason_code = 'pay'", "DELETE FROM dismissals"):
            with self.assertRaisesRegex(sqlite3.Error, "append_only"):
                self.store.conn.execute(sql)
        events = [(e["event_type"], json.loads(e["metadata_json"])) for e in self.store.all(
            "SELECT * FROM events WHERE event_type IN ('job_dismissed', 'job_reinstated') ORDER BY seq")]
        first = self.store.one("SELECT dismissal_id FROM dismissals ORDER BY rowid")["dismissal_id"]
        self.assertEqual(events, [("job_dismissed", {"reason": "pay"}),
                                  ("job_reinstated", {"undoes": first, "dismissed_for": "pay"}),
                                  ("job_dismissed", {"reason": "location"})])
        with self.assertRaisesRegex(ValueError, "unknown_reason"):
            dis.dismiss(self.store, job, "reinstated")   # a reinstatement is never a dismissal reason

    def test_every_reader_of_dismissals_asks_for_the_latest_event(self):
        """A query on the dismissals table outside these files would treat a reinstated posting as dismissed: everything
        else asks through careeros/review/dismiss.py (dismissed_sql, is_dismissed, current, recent)."""
        import re

        from careeros.paths import ENGINE_ROOT
        allowed = {"careeros/review/dismiss.py": "the helpers themselves",
                   "careeros/openings.py": "the role group's dismissal in force, through dismissed_sql",
                   "careeros/analytics/export.py": "the latest event per job; a reinstatement exports as not dismissed",
                   "careeros/review/explain.py": "the whole history, reinstatements included"}
        readers = {p.relative_to(ENGINE_ROOT).as_posix() for p in (ENGINE_ROOT / "careeros").rglob("*.py")
                   if re.search(r"\b(?:FROM|JOIN)\s+dismissals\b", p.read_text(encoding="utf-8"), re.I)}
        self.assertEqual(readers, set(allowed))

    def test_dismiss_undo_on_the_command_line(self):
        job = scored(self.store, "gh:u:2", "B")
        ctx = Context(self.store, POLICY, PROFILE, JOB_MATRIX, None, None)

        def run(**kw) -> str:
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_dismiss(ctx, {}, SimpleNamespace(**{"job_id": job, "reason": None, "note": None, "undo": False, **kw}))
            return out.getvalue()
        with self.assertRaisesRegex(SystemExit, "--reason is required"):
            run()
        self.assertEqual(run(reason="pay"), "gh:u:2: dismissed (pay)\n")
        with self.assertRaisesRegex(SystemExit, "--undo takes no --reason"):
            run(undo=True, reason="pay")
        self.assertEqual(run(undo=True, note="changed my mind"),
                         f"gh:u:2: reinstated (the dismissal of {NOW[:10]}, pay, is undone)\n")
        with self.assertRaisesRegex(SystemExit, "not_dismissed"):
            run(undo=True)
        self.assertEqual(self.store.one("SELECT reason_text FROM dismissals WHERE reason_code = 'reinstated'")["reason_text"],
                         "changed my mind")

    def test_dismissed_recently_offers_undo_through_the_confirmation(self):
        from careeros.review import dismiss as dis
        old = scored(self.store, "gh:u:old", "B")
        self.store.writer("review").insert("dismissals", {"dismissal_id": "dis:old", "job_id": old, "reason_code": "pay",
                                                          "at": ago(20), "actor": "user"})   # older than 14 days
        job = scored(self.store, "gh:u:<3>", "B", title="Claims <Analyst>")
        dis.dismiss(self.store, job, "pay", "too <low>")
        page = self.get()
        self.assertIn('<h2 id="dismissed">Dismissed recently (1)</h2>', page)
        self.assertNotIn('id="dis-gh:u:old"', page)
        card = card_of(page, "dis-gh:u:&lt;3&gt;")
        for shown in ('<code class="jid">gh:u:&lt;3&gt;</code>', f"dismissed {NOW[:10]}: pay", "too &lt;low&gt;",
                      "<button>Undo</button>"):
            self.assertIn(shown, card)
        key = self.form(card, "reinstate", job)
        ask = self.post("reinstate", target=job, key=key)
        self.assertEqual((ask.status_code, self.count("dismissals")), (200, 2))   # asked, nothing recorded
        self.assertIn(f"Undo your dismissal of this posting? You dismissed it on {NOW[:10]}: <b>pay</b> (too &lt;low&gt;).",
                      ask.text)
        self.assertIn("<b>Claims &lt;Analyst&gt;</b>", ask.text)
        self.assertEqual(self.post("confirm_reinstate", target=job, key=key).status_code, 400)   # the Undo key can't confirm
        self.assertEqual(self.post("reinstate", target=old, key=self.form(page, "reinstate", job)).status_code, 400)   # bound
        confirm = self.form(ask.text, "confirm_reinstate", job)
        reply = self.post("confirm_reinstate", target=job, key=confirm, note="misread")
        self.assertEqual((reply.status_code, reply.headers["location"]), (303, f"/?t={self.token}#sl-gh:u:%3C3%3E"))
        self.post("confirm_reinstate", target=job, key=confirm, note="misread")   # double click: recorded once
        self.assertEqual(self.count("dismissals"), 3)
        self.assertFalse(dis.is_dismissed(self.store, job))
        page = self.get()
        self.assertNotIn("Dismissed recently", page)
        self.assertIn('id="sl-gh:u:&lt;3&gt;"', page)   # back on the shortlist
        self.assertEqual(self.post("reinstate", target=job, key=key).status_code, 400)   # nothing to undo now
        dis.dismiss(self.store, job, "location")
        static = build_report(self.store, PROFILE, POLICY)   # no server: the command instead
        self.assertIn("undo: <code>careeros dismiss gh:u:&lt;3&gt; --undo</code>", card_of(static, "dis-gh:u:&lt;3&gt;"))

    def test_a_stale_undo_confirmation_never_undoes_a_newer_dismissal(self):
        """Codex CR-2026-11-01-OBS-REVIEW, F3: an Undo confirmation is bound to the dismissal it shows. Dismiss for pay,
        open Undo, dismiss again for location (e.g. on the command line), then Confirm the old page: nothing is
        reinstated, and a fresh confirmation names the newer dismissal, whose own Confirm undoes it. A dismissal undone
        meanwhile on the command line is not undone twice."""
        from careeros.review import dismiss as dis

        def reinstatements() -> list:
            return [json.loads(e["metadata_json"]) for e in self.store.all(
                "SELECT metadata_json FROM events WHERE event_type = 'job_reinstated' ORDER BY seq")]
        job = scored(self.store, "gh:u:5", "B", title="Claims Analyst")
        dis.dismiss(self.store, job, "pay", "old dismissal")
        ask = self.post("reinstate", target=job, key=self.form(self.get(), "reinstate", job))
        self.assertIn("<b>pay</b> (old dismissal)", ask.text)
        stale = self.form(ask.text, "confirm_reinstate", job)
        self.store.clock.advance(seconds=1)
        dis.dismiss(self.store, job, "location", "a newer decision")
        reply = self.post("confirm_reinstate", target=job, key=stale, note="confirmed the old page")
        self.assertEqual(reply.status_code, 409)
        self.assertTrue(dis.is_dismissed(self.store, job))
        self.assertEqual(dis.current(self.store, job)["reason_code"], "location")
        self.assertEqual((reinstatements(), self.count("dismissals")), ([], 2))   # nothing recorded
        self.assertIn("Not recorded: dismissal_changed", reply.text)
        self.assertIn("<b>location</b> (a newer decision)", reply.text)   # a fresh confirmation of the newer one
        self.assertEqual(self.post("confirm_reinstate", target=job, key=stale, note="again").status_code, 409)
        fresh = self.form(reply.text, "confirm_reinstate", job)
        self.assertEqual(self.post("confirm_reinstate", target=job, key=fresh, note="misread").status_code, 303)
        newer = self.store.one("SELECT dismissal_id FROM dismissals WHERE reason_code = 'location'")["dismissal_id"]
        self.assertEqual(reinstatements(), [{"undoes": newer, "dismissed_for": "location"}])
        self.assertFalse(dis.is_dismissed(self.store, job))

        dis.dismiss(self.store, job, "schedule")
        confirm = self.form(self.post("reinstate", target=job, key=self.form(self.get(), "reinstate", job)).text,
                            "confirm_reinstate", job)
        dis.reinstate(self.store, job, "undone on the command line")
        reply = self.post("confirm_reinstate", target=job, key=confirm)
        self.assertEqual(reply.status_code, 400)
        self.assertIn("not_dismissed", reply.text)
        self.assertEqual(len(reinstatements()), 2)   # the command line's only


class VoidApplicationTests(ServerMixin, unittest.TestCase):
    """ADR 0007, amendment of 2026-11-01: "Void (recorded by mistake)" on the applications card needs a reason and goes
    through the same confirmation; a void application stays listed and counts nowhere, so "I applied" is offered again."""

    def test_void_from_the_applications_card_through_the_confirmation(self):
        from careeros.review.applications import application_matrix, record_application
        from careeros.review.shortlist import shortlist
        job = scored(self.store, "gh:v:1", "B", title="Claims <Analyst>")
        record_label(self.store, job, "apply", None, key="k1")
        app_id = record_application(self.store, application_matrix(), job, "direct", policy=POLICY, key="a1")
        page = self.get()
        card = card_of(page, app_id)
        self.assertIn("<button>Void (recorded by mistake)</button>", card)
        self.assertNotIn('<option value="void">', card)   # never an ordinary status change
        self.assertNotIn("I applied", card_of(page, job))   # applied: no "I applied" on the posting's card
        status_key = self.form(card, "status", app_id)
        self.assertEqual(self.post("status", target=app_id, key=status_key, state="void", notes="").status_code, 400)
        key = self.form(card, "void", app_id)
        self.assertEqual(self.post("void", target=app_id, key=key, reason=" ").status_code, 400)   # a reason is required
        ask = self.post("void", target=app_id, key=key, reason="clicked on the wrong <posting>")
        self.assertEqual((ask.status_code, self.store.one("SELECT current_state FROM applications")["current_state"]),
                         (200, "applied"))   # asked, nothing changed
        for shown in ("Your reason: <b>clicked on the wrong &lt;posting&gt;</b>", f"Application: <code>{app_id}</code>",
                      "<b>Claims &lt;Analyst&gt;</b>", "Job id: <code>gh:v:1</code>", "Confirm: void it",
                      f'<a href="/?t={self.token}#{app_id}">Cancel</a>'):
            self.assertIn(shown, ask.text)
        self.assertEqual(self.post("confirm_void", target=app_id, key=key, reason="x").status_code, 400)   # the first key can't
        confirm = self.form(ask.text, "confirm_void", app_id)
        reply = self.post("confirm_void", target=app_id, key=confirm, reason="clicked on the wrong <posting>")
        self.assertEqual((reply.status_code, reply.headers["location"]), (303, f"/?t={self.token}#{app_id}"))
        self.post("confirm_void", target=app_id, key=confirm, reason="clicked on the wrong <posting>")   # double click
        row = self.store.one("SELECT current_state, state_sequence FROM applications")
        self.assertEqual(tuple(row), ("void", 1))
        page = self.get()
        self.assertIn("<h2>Your applications (0; 1 void)</h2>", page)
        card = card_of(page, app_id)
        self.assertIn("status: void (recorded by mistake: clicked on the wrong &lt;posting&gt;)", card)
        self.assertNotIn("<form", card)   # a void is final
        self.assertIn("<button>I applied</button>", card_of(page, job))   # offered again: the void one is no application
        self.assertTrue(next(i for i in shortlist(self.store, POLICY) if i["job_id"] == job)["next_action"].startswith("apply"))

    def test_no_void_control_or_confirmation_once_an_employer_answered(self):
        """Codex CR-2026-11-01-OBS-REVIEW, F2: the Void control, its confirmation and its Confirm all follow the writer's
        rule. After applied -> responded -> ghosted the card offers no Void, a Void form opened before the answer asks
        nothing, and a confirmation opened before it records nothing; applied -> ghosted still offers Void."""
        from careeros.review.applications import application_matrix, record_application, update_application
        matrix = application_matrix()
        answered = record_application(self.store, matrix, scored(self.store, "gh:v:3", "B"), "direct", policy=POLICY, key="a3")
        quiet = record_application(self.store, matrix, scored(self.store, "gh:v:4", "B", title="Rebate Analyst",
                                                                  title_norm="rebate analyst"), "direct", policy=POLICY, key="a4")
        page = self.get()
        void_key = self.form(card_of(page, answered), "void", answered)   # opened while it was still only applied
        ask = self.post("void", target=answered, key=void_key, reason="mistake")
        confirm = self.form(ask.text, "confirm_void", answered)
        for n, state in enumerate(("responded", "ghosted")):   # e.g. `careeros app-status` meanwhile
            update_application(self.store, matrix, answered, state, key=f"s{n}")
        update_application(self.store, matrix, quiet, "ghosted", key="q")
        page = self.get()
        self.assertNotIn("Void (recorded by mistake)", card_of(page, answered))
        self.assertIn("<button>Void (recorded by mistake)</button>", card_of(page, quiet))   # never answered: still offered
        static = build_report(self.store, PROFILE, POLICY, app_matrix=matrix)   # no server: no command either
        self.assertNotIn("recorded by mistake?", card_of(static, answered))
        self.assertIn("recorded by mistake?", card_of(static, quiet))
        refused =self.post("void", target=answered, key=void_key, reason="mistake")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("employer_answered: this application was responded before", refused.text)
        self.assertNotIn("confirm_void", refused.text)
        refused = self.post("confirm_void", target=answered, key=confirm, reason="mistake")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("employer_answered", refused.text)
        self.assertEqual(self.store.one("SELECT current_state FROM applications WHERE application_id = ?", (answered,))[0], "ghosted")

    def test_without_the_server_the_card_names_the_command(self):
        from careeros.review.applications import application_matrix, record_application
        job = scored(self.store, "gh:v:2", "B")
        app_id = record_application(self.store, application_matrix(), job, "direct", policy=POLICY, key="a1")
        static = build_report(self.store, PROFILE, POLICY, app_matrix=application_matrix())
        self.assertIn(f'recorded by mistake? <code>careeros app-status {app_id} void --reason "..."</code>',
                      card_of(static, app_id).replace("&quot;", '"'))


if __name__ == "__main__":
    unittest.main()
