"""Shadow scoring evaluator (2026-10-16): read-only snapshot, role identity, preferences, sampling, metrics and the
`careeros eval --scoring` command. All data is synthetic."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from careeros import cli
from careeros.core.clock import FixedClock
from careeros.core.policy import load_policy
from careeros.db import store as store_mod
from careeros.db.readonly import ReadOnlyStore, file_sha256, make_snapshot
from careeros.db.store import PermissionDenied, open_store
from careeros.discovery.boards import fetch_board
from careeros.discovery.http import HttpClient
from careeros.paths import CONFIG_DIR, vault_paths
from careeros.pipeline import Context, decision_fingerprint, ingest, process
from careeros.qualify import rubric
from careeros.qualify.profile import load_profile
from careeros.qualify.rubric import EVIDENCED, UNKNOWN, Assessment, Axis, Item, Section
from careeros.review import role_identity as ri
from careeros.review import sampling, scoring_eval
from careeros.review import shadow_policy as sp
from careeros.review.labels import record_label
from careeros.review.preferences import CONFIRMED, NEGATED, UNCERTAIN, detect, load_preferences, mentions
from careeros.review.scoring_eval import Group, JobEval, baseline_status, group_label

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import Board, gh_job

FIXTURES = Path(__file__).parent / "fixtures"
CFG = rubric.load_config()
EMPLOYER = {"employer_id": "greenhouse:synth", "name": "Synthetic Health", "parent": "Synthetic Health", "ats": "greenhouse",
            "board_token": "synth", "industry": "health insurance", "status": "verified"}
COORDINATION = {"careeros.db-wal", "careeros.db-shm"}   # SQLite's own files: any reader of a WAL database may create them


class SyntheticVault:
    """A throwaway vault whose database went through the real pipeline: 101 and 106 (one requisition in two places,
    scored), 102 (rejected by the gate)."""

    def __init__(self, label_101: bool = True, criteria: bool = True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = vault_paths(self.root)
        self.db, self.reports = self.paths["db"], self.paths["reports"]
        self.clock = FixedClock("2026-10-10T12:00:00.000Z")
        self.policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
        self.profile = load_profile(FIXTURES / "synthetic_profile.yaml")
        store = open_store(self.db, self.clock, self.policy.fingerprint)
        ctx = Context(store, self.policy, self.profile, JOB_MATRIX, self.paths["snapshots"])
        store.writer("admin").insert("employers", EMPLOYER)
        board = Board([gh_job(101, "Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="R-1"),
                       gh_job(106, "Senior Business Systems Analyst - Pharmacy Benefits", postings.GOOD_FIT, req="R-1", location="Austin, TX"),
                       gh_job(102, "Business Analyst", postings.HARD_FAIL, req="R-2", location="Remote")])
        client = HttpClient(transport=httpx.MockTransport(board.handler), min_interval=0, sleep=lambda s: None)
        try:
            ingest(ctx, EMPLOYER, fetch_board(client, "greenhouse", "synth"))
        finally:
            client.close()
        self.clock.advance(minutes=1)
        process(ctx)
        if label_101:
            record_label(store, "greenhouse:synth:101", "apply", None, "k-101")
        store.close()
        if criteria:
            self.paths["adoption_criteria"].parent.mkdir(parents=True, exist_ok=True)
            self.paths["adoption_criteria"].write_text("Adopt v1 only if (synthetic criteria for tests).\n", encoding="utf-8")

    def label(self, job_id: str, label: str, key: str, reason: str | None = None) -> None:
        self.clock.advance(minutes=5)
        store = open_store(self.db, self.clock, self.policy.fingerprint)
        try:
            record_label(store, job_id, label, reason, key)
        finally:
            store.close()

    def run(self, cfg: dict | None = None, **kw) -> dict:
        kw.setdefault("adoption_criteria", self.paths["adoption_criteria"])
        return scoring_eval.run(self.db, self.reports, self.policy, self.profile, cfg or CFG, {}, clock=self.clock, **kw)

    def record(self, report: dict) -> tuple[Path, dict]:
        path = Path(report["calibration_draw"]["record"])
        return path, json.loads(path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.tmp.cleanup()


# --- invariant 10 and 19: isolation ------------------------------------------------------------------------------------
class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.vault = SyntheticVault()

    def tearDown(self):
        self.vault.close()

    def test_snapshot_copies_without_changing_the_source(self):
        before = file_sha256(self.vault.db)
        dest = self.vault.root / "elsewhere" / "snap.db"
        meta = make_snapshot(self.vault.db, dest)
        self.assertEqual(file_sha256(self.vault.db), before)
        self.assertEqual(meta["sha256"], file_sha256(dest))
        self.assertGreaterEqual(meta["schema_version"], 12)
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["snap.db"])   # no WAL side files: opens read-only
        with self.assertRaises(FileExistsError):
            make_snapshot(self.vault.db, dest)

    def test_the_read_only_store_cannot_write(self):
        dest = self.vault.root / "snap.db"
        make_snapshot(self.vault.db, dest)
        before = file_sha256(dest)
        ro = ReadOnlyStore(dest)
        try:
            self.assertEqual(ro.one("SELECT COUNT(*) c FROM jobs")["c"], 3)
            self.assertEqual(ro.one("PRAGMA journal_mode")[0], "delete")
            with self.assertRaises(sqlite3.OperationalError):
                ro.conn.execute("INSERT INTO meta(key, value) VALUES ('evaluator', 'wrote')")
            self.assertEqual(ro.one("PRAGMA query_only")[0], 1)
            ro.conn.execute("PRAGMA query_only = OFF")                          # even then, the file is opened read-only
            with self.assertRaises(sqlite3.OperationalError):
                ro.conn.execute("INSERT INTO meta(key, value) VALUES ('evaluator', 'wrote')")
            with self.assertRaises(PermissionDenied):
                ro.writer("review")
            with self.assertRaises(PermissionDenied):
                ro.tx()
        finally:
            ro.close()
        self.assertEqual(file_sha256(dest), before)

    def test_evaluator_never_builds_a_store_asks_for_a_writer_or_opens_the_network(self):
        before = file_sha256(self.vault.db)
        data_before = {p.name for p in self.vault.db.parent.iterdir()}
        with mock.patch.object(store_mod.Store, "__init__", side_effect=AssertionError("a Store was built")), \
             mock.patch.object(store_mod.Store, "writer", side_effect=AssertionError("a writer was requested")), \
             mock.patch("socket.socket.connect", side_effect=AssertionError("network")):
            report = self.vault.run()
        self.assertEqual(file_sha256(self.vault.db), before)                      # byte-identical database
        new = {p.name for p in self.vault.db.parent.iterdir()} - data_before
        self.assertLessEqual(new, COORDINATION)                                   # nothing of ours in vault/data
        if "careeros.db-wal" in new:
            self.assertEqual((self.vault.db.parent / "careeros.db-wal").stat().st_size, 0)
        self.assertTrue(report["snapshot"]["unchanged_after"])
        self.assertEqual(report["decision_fingerprint"], decision_fingerprint(SimpleNamespace(policy=self.vault.policy)))
        written = sorted(p.relative_to(self.vault.reports).parts[0] for p in self.vault.reports.rglob("*") if p.is_file())
        self.assertEqual(written, ["scoring_eval"])

    def test_a_named_snapshot_stays_byte_identical(self):
        snap = self.vault.root / "frozen.db"
        make_snapshot(self.vault.db, snap)
        before = file_sha256(snap)
        report = self.vault.run(snapshot=snap)
        self.assertEqual(file_sha256(snap), before)
        self.assertEqual((report["snapshot"]["sha256"], report["snapshot"]["named"]), (before, True))
        self.assertTrue(snap.exists())   # a snapshot you name is kept; a temporary one is removed

    def test_cli_runs_without_the_live_store_the_lock_or_a_log_file(self):
        for source, target in (("synthetic_profile.yaml", "profile"), ("synthetic_overrides.yaml", "overrides")):
            self.vault.paths[target].parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / source, self.vault.paths[target])
        before = file_sha256(self.vault.db)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(self.vault.root)}), \
             mock.patch.object(cli, "open_store", side_effect=AssertionError("live store opened")), \
             mock.patch.object(cli, "SingleWriterLock", side_effect=AssertionError("lock taken")), \
             mock.patch.object(store_mod.Store, "__init__", side_effect=AssertionError("a Store was built")), \
             redirect_stdout(out):
            cli.main(["eval", "--scoring", "--draw", "--seed", "cli-seed"])
        text = out.getvalue()
        self.assertIn("Shadow scoring evaluation (read-only", text)
        self.assertNotIn("Synthetic Health", text)                    # the console shows counts, never employers or titles
        self.assertNotIn("Business Systems Analyst", text)
        self.assertEqual(file_sha256(self.vault.db), before)
        self.assertFalse(self.vault.paths["lock"].exists())
        self.assertFalse(self.vault.paths["logs"].exists())
        self.assertTrue(list((self.vault.reports / "calibration").glob("calibration-list-*.md")))


# --- the three approaches, groups and the calibration flow --------------------------------------------------------------
class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.vault = SyntheticVault(label_101=False)

    def tearDown(self):
        self.vault.close()

    def test_one_role_in_two_places_is_one_group_and_every_approach_is_reported(self):
        report = self.vault.run()
        pop = report["population"]
        self.assertEqual((pop["jobs"], pop["judged"], pop["gate_rejected"], pop["role_groups"]), (3, 2, 1, 2))
        self.assertEqual(pop["identity_pairs"].get("confirmed"), 1)          # the same requisition, other location
        rows = {r["job_id"]: r for r in report["jobs"]}
        self.assertEqual(rows["greenhouse:synth:106"]["group"], "greenhouse:synth:101")
        self.assertEqual(rows["greenhouse:synth:102"]["production"], "rejected")
        self.assertEqual({rows["greenhouse:synth:102"][a] for a in ("baseline", "v1")}, {"rejected"})
        self.assertEqual(len(rows["greenhouse:synth:101"]["v1_all"]), 8)      # R0-R3 x D1/D2, all computed
        self.assertIn("v1:R3/D1", report["development"]["metrics"])
        for row in report["jobs"]:
            if row["production"] != "rejected":
                self.assertLessEqual(row["q_low"], row["q_high"])
                self.assertAlmostEqual(row["q_high"] - row["q_low"], 100 - row["coverage"], delta=0.11)

    def test_calibration_list_hides_statuses_and_only_later_labels_enter_estimates(self):
        self.vault.label("greenhouse:synth:102", "skip", "k-102")             # a development label, before the draw
        first = self.vault.run(draw=True, seed="seed-1")
        record = json.loads(Path(first["calibration_draw"]["record"]).read_text(encoding="utf-8"))
        entries = {e["group_id"]: e for e in record["groups"]}
        self.assertEqual(list(entries), ["greenhouse:synth:101"])            # 102 is development data: never drawn
        self.assertEqual((entries["greenhouse:synth:101"]["pi"], entries["greenhouse:synth:101"]["drawn"]), (1.0, True))
        self.assertEqual([r["group_id"] for r in record["relabel"]], ["greenhouse:synth:102"])
        listing = Path(first["calibration_draw"]["list"]).read_text(encoding="utf-8")
        for hidden in ("Tier", "tier", "check_first", "near_miss", "stratum", "recommended", "score", "1.0"):
            self.assertNotIn(hidden, listing.replace("no tier, status or score", ""))
        self.assertIn("greenhouse:synth:106", listing)

        waiting = self.vault.run()["calibration"]
        self.assertEqual((waiting["drawn"], waiting["labelled"], waiting["not_yet_labelled"], waiting["estimates"]), (1, 0, 1, None))
        self.assertEqual(waiting["relabel_check"], {"agree": 0, "disagree": 0, "not_yet_relabelled": 1})   # same-moment label: development

        self.vault.label("greenhouse:synth:106", "apply", "k-106")            # labelled on the list, after the draw
        self.vault.label("greenhouse:synth:102", "skip", "k-102b")            # the relabel check repeats
        cal = self.vault.run()["calibration"]
        self.assertEqual((cal["labelled"], cal["complete"]), (1, True))
        self.assertEqual(cal["relabel_check"], {"agree": 1, "disagree": 0, "not_yet_relabelled": 0})
        est = cal["estimates"]
        for approach in ("production", "baseline", "v1"):
            self.assertEqual(est[approach]["recall"]["estimate"], 1.0, approach)
            self.assertEqual(est[approach]["skip_share_among_recommendations"]["upper_95_if_none"], 0.95)


    def test_a_group_is_drawn_only_once_and_a_list_is_never_overwritten(self):
        self.vault.run(draw=True, seed="seed-1")
        with self.assertRaises(FileExistsError):
            self.vault.run(draw=True, seed="seed-1")
        self.vault.clock.advance(seconds=1)
        again = self.vault.run(draw=True, seed="seed-2")
        record = json.loads(Path(again["calibration_draw"]["record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["groups"], [])

    # --- the draw freezes what it measures (2026-10-17) ---
    def test_a_draw_freezes_each_group_s_predictions_and_the_evaluation_identity(self):
        report = self.vault.run(draw=True, seed="seed-1")
        _, record = self.vault.record(report)
        self.assertEqual(record["version"], 2)
        identity = record["identity"]
        for key in ("evaluator", "rubric_version", "shadow_config_sha256", "primary_configuration", "evaluator_code_sha256",
                    "decision_fingerprint", "profile_fingerprint", "preferences_sha256", "grouping", "sampling_pis",
                    "snapshot_sha256", "adoption_criteria_sha256"):
            self.assertIsNotNone(identity.get(key), key)
        self.assertEqual(identity, report["identity"])
        self.assertEqual(identity["decision_fingerprint"], report["decision_fingerprint"])
        self.assertEqual(identity["adoption_criteria_sha256"], scoring_eval.criteria_sha256(self.vault.paths["adoption_criteria"]))
        self.assertEqual(record["adoption_criteria_text"], "Adopt v1 only if (synthetic criteria for tests).")
        entry = record["groups"][0]
        row = {r["job_id"]: r for r in report["jobs"]}["greenhouse:synth:101"]
        self.assertEqual({a: entry["predictions"][a] for a in ("production", "baseline", "v1")},
                         {a: row[a] for a in ("production", "baseline", "v1")})
        self.assertEqual(sorted(k for k in entry["predictions"] if k.startswith("v1:")), sorted(f"v1:{c}" for c in row["v1_all"]))

    def test_later_estimates_read_the_frozen_predictions_never_today_s_statuses(self):
        report = self.vault.run(draw=True, seed="seed-1")
        path, record = self.vault.record(report)
        record["groups"][0]["predictions"].update(production="rejected", v1="archive")   # what the draw predicted
        path.write_text(json.dumps(record), encoding="utf-8")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")
        est = self.vault.run()["calibration"]["estimates"]
        self.assertEqual(est["production"]["recall"]["estimate"], 0.0)       # frozen: not recommended at the draw
        self.assertEqual(est["production"]["false_hard_rejections"]["estimate"], 1.0)
        self.assertEqual(est["v1"]["recall"]["estimate"], 0.0)
        self.assertEqual(est["baseline"]["recall"]["estimate"], 1.0)         # untouched prediction

    def test_rule_changes_after_a_draw_are_reported_and_leave_the_estimates_alone(self):
        self.vault.run(draw=True, seed="seed-1")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")
        same = self.vault.run()["calibration"]
        self.assertEqual(list(same["changed_since_draw"].values()), [[]])
        changed_cfg = json.loads(json.dumps(CFG))
        changed_cfg["version"] = "rubric-test-changed"
        changed_cfg["primary"]["core"] = "R0"
        later = self.vault.run(cfg=changed_cfg)["calibration"]
        self.assertEqual(list(later["changed_since_draw"].values()),
                         [["primary_configuration", "rubric_version", "shadow_config_sha256"]])
        self.assertEqual(later["estimates"], same["estimates"])

    def test_a_list_without_frozen_predictions_is_named_and_left_out(self):
        folder = self.vault.reports / "calibration"
        folder.mkdir(parents=True)
        old = {"kind": scoring_eval.LIST_KIND, "version": 1, "created_at": "2026-10-10T12:00:30.000Z", "seed": "old",
               "groups": [{"group_id": "greenhouse:synth:101", "members": ["greenhouse:synth:101", "greenhouse:synth:106"],
                           "stratum": "recommended_by_all", "pi": 1.0, "drawn": True}], "relabel": []}
        (folder / "calibration-list-old.json").write_text(json.dumps(old), encoding="utf-8")
        self.vault.label("greenhouse:synth:101", "apply", "k-101")
        cal = self.vault.run()["calibration"]
        self.assertEqual(len(cal["lists_without_frozen_predictions"]), 1)
        self.assertEqual((cal["lists"], cal["drawn"], cal["estimates"]), ([], 0, None))

    def test_a_draw_needs_your_adoption_criteria_written_first(self):
        vault = SyntheticVault(criteria=False)
        try:
            with self.assertRaisesRegex(ValueError, "adoption_criteria_missing"):
                vault.run(draw=True, seed="seed-1")
            vault.paths["adoption_criteria"].parent.mkdir(parents=True, exist_ok=True)
            vault.paths["adoption_criteria"].write_text("  \n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "adoption_criteria_missing"):
                vault.run(draw=True, seed="seed-1")
            self.assertFalse((vault.reports / "calibration").exists())        # refused before anything was written
            report = vault.run()                                               # a plain evaluation never needs them
            self.assertFalse(report["adoption_criteria"]["recorded"])
            vault.paths["adoption_criteria"].write_text("Adopt only if (synthetic).\n", encoding="utf-8")
            drawn = vault.run(draw=True, seed="seed-1")
            self.assertTrue(drawn["adoption_criteria"]["recorded"])
        finally:
            vault.close()

    def test_a_later_list_never_rewrites_an_earlier_one(self):
        """2026-10-17 (fresh-context review): list 2 re-asks about group 101 (a relabel check); that answer belongs to
        list 2, and list 1 keeps your first answer to it."""
        first = self.vault.run(draw=True, seed="seed-1")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")             # answers list 1
        self.vault.clock.advance(seconds=1)
        second = self.vault.run(draw=True, seed="seed-2")
        _, record = self.vault.record(second)
        self.assertEqual([r["group_id"] for r in record["relabel"]], ["greenhouse:synth:101"])
        self.vault.label("greenhouse:synth:101", "skip", "k-101-relabel")      # answers list 2's relabel check
        cal = self.vault.run()["calibration"]
        self.assertEqual((cal["labelled"], cal["conflicting"]), (1, 0))
        self.assertEqual(cal["estimates"]["v1"]["recall"]["estimate"], 1.0)
        self.assertEqual(cal["relabel_check"], {"agree": 0, "disagree": 1, "not_yet_relabelled": 0})
        self.assertTrue(first["calibration_draw"]["drawn"])

    def test_your_first_answer_to_a_list_counts_and_a_later_change_does_not(self):
        """A change after your first answer may have been made after seeing the tiers on the review page: not blind."""
        self.vault.run(draw=True, seed="seed-1")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")
        self.vault.label("greenhouse:synth:106", "skip", "k-106-changed")
        cal = self.vault.run()["calibration"]
        self.assertEqual((cal["labelled"], cal["estimates"]["v1"]["recall"]["estimate"]), (1, 1.0))

    def test_lists_drawn_under_different_rules_are_never_pooled(self):
        self.vault.run(draw=True, seed="seed-1")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")
        folder = self.vault.reports / "calibration"
        path = next(folder.glob("calibration-list-*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        twin = json.loads(json.dumps(record))
        twin["identity"]["rubric_version"] = "an-earlier-rubric"
        twin["groups"][0]["members"] = ["greenhouse:synth:102"]
        (folder / "calibration-list-0-twin.json").write_text(json.dumps(twin), encoding="utf-8")
        self.vault.label("greenhouse:synth:102", "skip", "k-102")
        cal = self.vault.run()["calibration"]
        self.assertEqual((cal["prediction_identities"], cal["estimates"]), (2, None))
        self.assertEqual(sorted(v["labelled"] for v in cal["estimates_by_identity"].values()), [1, 1])

    def test_a_malformed_or_unreadable_list_is_left_out_and_never_stops_the_run(self):
        self.vault.run(draw=True, seed="seed-1")
        folder = self.vault.reports / "calibration"
        record = json.loads(next(folder.glob("calibration-list-*.json")).read_text(encoding="utf-8"))
        broken = json.loads(json.dumps(record))
        del broken["groups"][0]["predictions"]["v1"]
        (folder / "calibration-list-1-broken.json").write_text(json.dumps(broken), encoding="utf-8")
        (folder / "calibration-list-2-garbled.json").write_text("{not json", encoding="utf-8")
        cal = self.vault.run()["calibration"]
        self.assertEqual(len(cal["lists_without_frozen_predictions"]), 2)
        self.assertEqual(len(cal["lists"]), 1)
        self.assertFalse(scoring_eval.frozen({**record, "groups": [{**record["groups"][0], "pi": 0}]}))

    def test_a_later_draw_survives_a_malformed_earlier_list(self):
        """Independent Codex review of f984d15 (2026-10-20): an earlier record whose groups is not a list stopped the next draw."""
        self.vault.run(draw=True, seed="seed-1")
        folder = self.vault.reports / "calibration"
        record = json.loads(next(folder.glob("calibration-list-*.json")).read_text(encoding="utf-8"))
        (folder / "calibration-list-9-bad.json").write_text(json.dumps({**record, "groups": 7}), encoding="utf-8")
        again = self.vault.run(draw=True, seed="seed-2")
        self.assertIn("calibration_draw", again)

    def test_criteria_text_is_normalised_and_a_draw_never_uses_a_named_snapshot(self):
        path = self.vault.paths["adoption_criteria"]
        for invisible in ("\ufeff\r\n", "\u200b", " \n\t"):
            path.write_text(invisible, encoding="utf-8")
            self.assertIsNone(scoring_eval.criteria_text(path), repr(invisible))
        path.write_bytes("Adopt if X\r\n".encode("utf-16"))               # Notepad's "Unicode"
        self.assertEqual(scoring_eval.criteria_text(path), "Adopt if X")
        unix = scoring_eval.criteria_sha256(path)
        path.write_text("Adopt if X\n", encoding="utf-8")
        self.assertEqual(scoring_eval.criteria_sha256(path), unix)          # the same words are the same criteria
        path.write_bytes(b"\xff\xfe\x00\xd8")                             # undecodable: not recorded, never a crash
        self.assertIsNone(scoring_eval.criteria_text(path))
        self.assertFalse(self.vault.run()["adoption_criteria"]["recorded"])
        path.write_text("Adopt if X\n", encoding="utf-8")
        snap = self.vault.root / "old.db"
        make_snapshot(self.vault.db, snap)
        with self.assertRaisesRegex(ValueError, "draw_needs_a_fresh_snapshot"):
            self.vault.run(draw=True, seed="s", snapshot=snap)

    def test_the_summary_never_contradicts_itself(self):
        self.vault.run(draw=True, seed="seed-1")
        self.vault.label("greenhouse:synth:106", "apply", "k-106")
        text = scoring_eval.summary(self.vault.run())
        self.assertIn("Calibration (predictions frozen at each draw): 1 groups drawn, 1 labelled", text)
        self.assertNotIn("no list drawn yet", text)

    def test_free_text_label_reasons_are_collected_in_the_report(self):
        self.vault.label("greenhouse:synth:101", "apply", "k-101", reason="claims work I know well")
        report = self.vault.run()
        rows = {r["job_id"]: r for r in report["jobs"]}
        self.assertEqual(rows["greenhouse:synth:101"]["label_reason"], "claims work I know well")
        self.assertIsNone(rows["greenhouse:synth:102"]["label_reason"])
        self.assertEqual(report["development"]["label_reasons"]["with_reason"], 1)


# --- role identity -------------------------------------------------------------------------------------------------------
TEXT = " ".join(f"duty{i}" for i in range(300)) + " remote within the US"


def posting(n, **kw):
    row = {"job_id": f"gh:x:{n:03d}", "parent": "Example Health", "title_norm": "senior health data analyst", "requisition_id": None,
           "canonical_url": None, "department": "Analytics", "posted_at": "2026-10-01T09:00:00Z", "requirements_fingerprint": "rf1",
           "description_text": TEXT}
    row.update(kw)
    return row


class RoleIdentityTests(unittest.TestCase):
    def test_levels(self):
        a = posting(1)
        city = posting(2, description_text=TEXT.replace(" US", ""))          # one deleted word, another city
        self.assertEqual(ri.identity(a, city, set()), "strong")
        self.assertEqual(ri.identity(a, posting(3, description_text="a different posting entirely"), set()), "possible")
        self.assertEqual(ri.identity(a, posting(4, department="Claims"), set()), "possible")
        self.assertEqual(ri.identity(a, posting(5, posted_at="2026-10-02T09:00:00Z"), set()), "possible")
        self.assertEqual(ri.identity(a, posting(6, requirements_fingerprint=None), set()), "possible")
        self.assertEqual(ri.identity(a, posting(7, parent="Other Health"), set()), "none")
        self.assertEqual(ri.identity(a, posting(14, title_norm="senior claims analyst"), set()), "none")   # same text, other role
        self.assertEqual(ri.identity(posting(8, requisition_id="R-1", title_norm="analyst"), posting(9, requisition_id="R-1"), set()), "confirmed")
        self.assertEqual(ri.identity(posting(10, canonical_url="https://x/1"), posting(11, canonical_url="https://x/1", parent="Y"), set()), "confirmed")
        self.assertEqual(ri.identity(posting(12, title_norm="claims analyst"), posting(13), {frozenset(("gh:x:012", "gh:x:013"))}), "confirmed")

    def test_different_known_requisitions_are_never_the_same_vacancy(self):
        self.assertEqual(ri.identity(posting(1, requisition_id="R-1"), posting(2, requisition_id="R-2"), set()), "possible")
        linked = {frozenset(("gh:x:001", "gh:x:002"))}
        self.assertEqual(ri.identity(posting(1, requisition_id="R-1"), posting(2, requisition_id="R-2"), linked), "possible")

    def test_a_posting_without_a_requisition_never_joins_two_known_vacancies(self):
        """Independent Codex review of f984d15 (2026-10-20): R-1, a posting with no requisition, and R-2 became one group."""
        jobs = [posting(1, requisition_id="R-1"), posting(2), posting(3, requisition_id="R-2")]
        alone = {"gh:x:001": ["gh:x:001"], "gh:x:002": ["gh:x:002"], "gh:x:003": ["gh:x:003"]}
        self.assertEqual(ri.group_roles(jobs, set(), "strong")[0], alone)
        self.assertEqual(ri.group_roles(list(reversed(jobs)), set(), "strong")[0], alone)
        self.assertEqual(ri.group_roles(jobs[:2], set(), "strong")[0], {"gh:x:001": ["gh:x:001", "gh:x:002"]})

    def test_grouping_by_level_is_deterministic(self):
        jobs = [posting(3), posting(1, description_text="unrelated text"), posting(2, description_text=TEXT.replace(" US", ""))]
        groups, counts = ri.group_roles(jobs, set(), "strong")
        self.assertEqual(groups, {"gh:x:001": ["gh:x:001"], "gh:x:002": ["gh:x:002", "gh:x:003"]})
        self.assertEqual(dict(counts), {"possible": 2, "strong": 1})
        self.assertEqual(ri.group_roles(list(reversed(jobs)), set(), "strong")[0], groups)
        self.assertEqual(ri.group_roles(jobs, set(), "possible")[0], {"gh:x:001": ["gh:x:001", "gh:x:002", "gh:x:003"]})
        self.assertEqual(len(ri.group_roles(jobs, set(), "confirmed")[0]), 3)
        self.assertEqual(len(ri.ungrouped(jobs)), 3)
        with self.assertRaises(ValueError):
            ri.group_roles(jobs, set(), "none")

    def test_similarity_threshold(self):
        self.assertTrue(ri.similar(TEXT, TEXT.replace(" US", "")))
        self.assertFalse(ri.similar(TEXT, " ".join(TEXT.split()[:280])))       # 20 words missing: below 0.99
        self.assertFalse(ri.similar("", TEXT))
        self.assertFalse(ri.similar(TEXT, " ".join(reversed(TEXT.split()))))   # the same words in another order


# --- preferences ---------------------------------------------------------------------------------------------------------
PREFS = {"dealbreakers": [{"name": "phone work", "terms": ["inbound calls", "phone work", "call center"]}],
         "avoid": [{"name": "travel", "terms": ["travel"]}], "want": []}


def job_text(text: str, title: str = "Claims Analyst") -> dict:
    return {"title": title, "description_text": text}


class PreferenceTests(unittest.TestCase):
    def states(self, text, title="Claims Analyst", term="inbound calls"):
        return [(m["state"], m["why"]) for m in mentions(job_text(text, title), term)]

    def test_confirmed_only_when_affirmative_in_the_duties_or_title(self):
        self.assertEqual(self.states("Responsibilities:\n- Handle inbound calls from members"), [(CONFIRMED, "affirmative")])
        self.assertEqual(self.states("Requirements:\n- 2 years taking inbound calls"), [(CONFIRMED, "affirmative")])
        self.assertEqual(self.states("Overview", title="Inbound Calls Specialist"), [(CONFIRMED, "affirmative")])
        found = detect(job_text("Responsibilities:\n- Handle inbound calls from members"), PREFS)
        self.assertTrue(found["conflict"])
        self.assertEqual(found["conflicts"][0]["name"], "phone work")

    def test_negations_are_evidence_of_absence(self):
        for text, term in (("Responsibilities:\n- This role has no phone work", "phone work"),
                           ("Responsibilities:\n- This is not a call-center role", "call center"),
                           ("Responsibilities:\n- Work without inbound calls", "inbound calls")):
            self.assertEqual(self.states(text, term=term)[0][0], NEGATED, text)
        found = detect(job_text("Responsibilities:\n- This role has no phone work"), PREFS)
        self.assertEqual((found["conflict"], found["absent"], found["verify"]), (False, ["phone work"], []))

    def test_uncertain_mentions_only_add_a_verify_fact(self):
        cases = {"Responsibilities:\n- You may occasionally take inbound calls": "hedged",
                 "Responsibilities:\n- Cover inbound calls as needed": "hedged",
                 "Benefits:\n- Our inbound calls team supports you": "outside the duties and requirements",
                 "We are a growing company. Our team handles inbound calls.": "outside the duties and requirements",
                 "Responsibilities:\n- Never work weekends and handle the inbound calls queue": "negation scope unclear"}
        for text, why in cases.items():
            self.assertEqual(self.states(text), [(UNCERTAIN, why)], text)
            found = detect(job_text(text), PREFS)
            self.assertFalse(found["conflict"], text)
            self.assertEqual(found["verify"][0]["fact"].split(":")[0], "verify", text)

    def test_not_only_is_not_a_negation(self):
        self.assertEqual(self.states("Responsibilities:\n- Not only handle inbound calls but also chats")[0][0], CONFIRMED)

    def test_avoid_annotates_and_want_does_nothing(self):
        found = detect(job_text("Responsibilities:\n- Travel to client sites monthly"), PREFS)
        self.assertEqual((found["conflict"], [a["name"] for a in found["avoid"]]), (False, ["travel"]))
        self.assertEqual(detect(job_text("Responsibilities:\n- Travel"), {"want": [{"name": "travel", "terms": ["travel"]}]})["avoid"], [])

    def test_the_file_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_preferences(Path(tmp) / "missing.yaml"), {})
            path = Path(tmp) / "preferences.yaml"
            path.write_text("dealbreakers:\n  - phone work\n  - name: weekends\n    terms: [weekend shifts]\n", encoding="utf-8")
            prefs = load_preferences(path)
        self.assertEqual(prefs["dealbreakers"], [{"name": "phone work", "terms": ["phone work"]},
                                                 {"name": "weekends", "terms": ["weekend shifts"]}])
        self.assertEqual((prefs["avoid"], prefs["want"]), ([], []))


# --- sampling and estimation ---------------------------------------------------------------------------------------------
def statuses(production="near_miss", baseline="not_recommended", v1="near_miss", v1_all=None):
    return {"production": production, "baseline": baseline, "v1": v1, "v1_all": v1_all or [v1]}


class SamplingTests(unittest.TestCase):
    def test_first_matching_stratum(self):
        self.assertEqual(sampling.stratum(statuses(baseline="B", v1="check_first", v1_all=["check_first"])), "discordant")
        self.assertEqual(sampling.stratum(statuses("A", "B", "B", ["B"])), "recommended_by_all")
        self.assertEqual(sampling.stratum(statuses(v1="near_miss", v1_all=["near_miss", "stretch_review"])), "check_first_or_stretch")
        self.assertEqual(sampling.stratum(statuses("archive", v1="near_miss")), "near_miss")
        self.assertEqual(sampling.stratum(statuses("archive", v1="archive")), "archive")
        self.assertEqual(sampling.stratum(statuses("rejected", "rejected", "rejected")), "gate_rejected")
        self.assertIsNone(sampling.stratum(statuses("held", "held", "held")))

    def test_a_group_recommended_by_exactly_one_approach_always_has_pi_1(self):
        options = {"production": ("B", "near_miss", "archive"), "baseline": ("B", "not_recommended"),
                   "v1": ("B", "check_first", "near_miss", "archive")}
        for prod, base, v1 in product(*options.values()):
            s = statuses(prod, base, v1, [v1])
            if sum(sampling.recommended(x) for x in (prod, base, v1)) == 1:
                self.assertEqual(CFG["sampling"][sampling.stratum(s)], 1.0, (prod, base, v1))

    def test_draws_are_reproducible_and_near_their_probability(self):
        self.assertEqual([sampling.drawn(f"g{i}", 0.25, "s") for i in range(50)], [sampling.drawn(f"g{i}", 0.25, "s") for i in range(50)])
        self.assertTrue(all(sampling.drawn(f"g{i}", 1.0, "s") for i in range(50)))
        self.assertFalse(any(sampling.drawn(f"g{i}", 0.0, "s") for i in range(50)))
        share = sum(sampling.drawn(f"g{i}", 0.25, "s") for i in range(4000)) / 4000
        self.assertAlmostEqual(share, 0.25, delta=0.03)
        self.assertNotEqual([sampling.drawn(f"g{i}", 0.5, "a") for i in range(40)], [sampling.drawn(f"g{i}", 0.5, "b") for i in range(40)])

    def test_check_first_recovery_is_over_the_wanted_groups_v1_did_not_recommend(self):
        rows = [{"pi": 1.0, "label": "apply", "statuses": {"production": "B", "baseline": "B", "v1": "B"}},
                {"pi": 1.0, "label": "apply", "statuses": {"production": "near_miss", "baseline": "B", "v1": "check_first"}},
                {"pi": 1.0, "label": "stretch", "statuses": {"production": "archive", "baseline": "not_recommended", "v1": "archive"}},
                {"pi": 1.0, "label": "skip", "statuses": {"production": "near_miss", "baseline": "not_recommended", "v1": "check_first"}}]
        v1 = sampling.estimates(rows)["v1"]
        self.assertEqual((v1["check_first_recovery"]["estimate"], v1["check_first_recovery"]["groups_in_denominator"]), (0.5, 2))
        share = v1["check_first_share_of_all_wanted"]
        self.assertEqual((share["estimate"], share["groups_in_denominator"]), (0.333, 3))
        self.assertEqual(v1["check_first_yield"]["estimate"], 0.5)

    def test_only_the_drawn_members_labels_count(self):
        history = {"a": [("2026-10-01T00:00:00Z", "skip"), ("2026-10-03T00:00:00Z", "apply")],
                   "b": [("2026-10-04T00:00:00Z", "skip")], "c": [("2026-10-04T00:00:00Z", "skip")]}
        at = "2026-10-02T00:00:00Z"
        self.assertEqual(scoring_eval.labels_after(history, ["a"], at), "apply")
        self.assertEqual(scoring_eval.labels_after(history, ["a", "b"], at), "conflict")
        self.assertEqual(scoring_eval.labels_until(history, ["a", "c"], at), "skip")
        self.assertIsNone(scoring_eval.labels_after(history, ["d"], at))

    def test_horvitz_thompson_ratio_and_bound(self):
        rows = [{"pi": 0.5, "x": True}, {"pi": 1.0, "x": False}, {"pi": 0.25, "x": True}]
        est = sampling.ht_ratio(rows, lambda r: r["x"], lambda r: True)
        self.assertEqual((est["numerator_ht"], est["denominator_ht"], est["estimate"], est["groups_in_denominator"]), (6.0, 7.0, 0.857, 3))
        self.assertIsNone(sampling.ht_ratio([], lambda r: True, lambda r: True)["estimate"])
        self.assertAlmostEqual(sampling.upper_95_if_none(30), 0.095, places=3)   # about 3/n
        self.assertIsNone(sampling.upper_95_if_none(0))

    def test_estimates_weight_by_inverse_pi_and_read_status_only(self):
        rows = [{"pi": 1.0, "label": "apply", "statuses": {"production": "near_miss", "baseline": "B", "v1": "check_first"}},
                {"pi": 0.1, "label": "apply", "statuses": {"production": "archive", "baseline": "not_recommended", "v1": "archive"}},
                {"pi": 0.05, "label": "stretch", "statuses": {"production": "rejected", "baseline": "rejected", "v1": "rejected"}},
                {"pi": 1.0, "label": "skip", "statuses": {"production": "B", "baseline": "B", "v1": "B"}}]
        est = sampling.estimates(rows)
        self.assertEqual(est["baseline"]["recall"]["estimate"], round(1 / 31, 3))
        self.assertEqual(est["v1"]["recall_with_check_first"]["estimate"], round(1 / 31, 3))
        self.assertEqual(est["v1"]["check_first_yield"]["estimate"], 1.0)
        self.assertEqual(est["production"]["false_hard_rejections"]["estimate"], round(20 / 31, 3))
        self.assertEqual(est["production"]["skip_share_among_recommendations"]["value"], 1.0)
        self.assertEqual(est["production"]["skip_leakage"]["estimate"], 1.0)


# --- labels, baseline, metrics and ordering --------------------------------------------------------------------------------
def assessment(low: float, high: float, facts: int = 0) -> Assessment:
    items = [Item("known", low, EVIDENCED, 1.0), Item("open", high - low, UNKNOWN), Item("missing", 100 - high, EVIDENCED, 0.0)]
    q = Axis([Section("coverage", 100, True, [i for i in items if i.weight > 0])])
    o = Axis([Section("pay", 100, True, [Item("pay", 1.0, EVIDENCED, 0.5)])])
    return Assessment("t", q, o, 5.0, 2.0, "none", None, [], [f"fact {n}" for n in range(facts)])


def job_eval(job_id, v1="near_miss", label=None, production="near_miss", baseline="not_recommended", act="actionable",
             a: Assessment | None = None, family="data_reporting", parent="Example Health") -> JobEval:
    a = a or assessment(50, 70)
    decision = sp.Decision(v1, v1, "3e", facts=list(a.facts_to_resolve))
    return JobEval(job_id, {"job_id": job_id, "family": family, "parent": parent, "title": "Analyst"}, "judged", production, baseline,
                   v1, {"R2/D2": v1}, [("2026-10-01T00:00:00.000Z", label)] if label else [], a, decision, {}, act)


class MetricTests(unittest.TestCase):
    def test_group_label(self):
        self.assertIsNone(group_label([]))
        self.assertEqual(group_label(["skip", "apply"]), "conflict")
        self.assertEqual(group_label(["stretch", "apply"]), "apply")
        self.assertEqual(group_label(["stretch"]), "stretch")
        self.assertEqual(group_label(["skip", "skip"]), "skip")

    def test_baseline_rule(self):
        b = CFG["baseline"]
        self.assertEqual(baseline_status("HOLD", 99, 1.0, "strong_match", False, b), "held")
        self.assertEqual(baseline_status("ELIGIBLE", 92, 0.95, "strong_match", False, b), "A")
        self.assertEqual(baseline_status("ELIGIBLE", 92, 0.95, "small_gap", False, b), "B")
        self.assertEqual(baseline_status("ELIGIBLE", 85, 0.70, None, False, b), "B")          # neutral coverage, gap unknown
        self.assertEqual(baseline_status("ELIGIBLE", 85, 0.75, "medium_gap", False, b), "not_recommended")
        self.assertEqual(baseline_status("ELIGIBLE", 85, 0.75, "small_gap", True, b), "not_recommended")   # degree stretch
        self.assertEqual(baseline_status("CONDITIONAL", 95, 0.95, "strong_match", False, b), "not_recommended")
        self.assertEqual(baseline_status("ELIGIBLE", 79.9, 0.95, "strong_match", False, b), "not_recommended")

    def test_metrics_read_the_status_never_your_decisions(self):
        jobs = [job_eval("j1", "B", "skip", act="declined"), job_eval("j2", "B", "apply"), job_eval("j3", "near_miss", "apply"),
                job_eval("j4", "check_first", "stretch"), job_eval("j5", "B")]
        groups = scoring_eval.build_groups(jobs, ri.ungrouped([j.job for j in jobs]))
        m = scoring_eval.approach_metrics(groups, "v1")
        self.assertEqual((m["labelled"], m["recommended"], m["skips_among_recommended"], m["skip_share_among_recommendations"]), (4, 2, 1, 0.5))
        self.assertEqual((m["wanted"], m["recall_wanted"], m["apply"], m["recall_apply"]), (3, 0.333, 2, 0.5))
        self.assertEqual((m["check_first_labelled"], m["check_first_yield"], m["recall_wanted_with_check_first"]), (1, 1.0, 0.667))
        clean = scoring_eval.approach_metrics(scoring_eval.build_groups(jobs[1:2], ri.ungrouped([jobs[1].job])), "v1")
        self.assertEqual(clean["upper_95_if_none"], 0.95)

    def test_conflicting_groups_are_left_out_unless_a_sensitivity_run_resolves_them(self):
        jobs = [job_eval("j1", "B", "apply"), job_eval("j2", "B", "skip")]
        members = {"j1": ["j1", "j2"]}
        self.assertEqual(scoring_eval.approach_metrics(scoring_eval.build_groups(jobs, members), "v1")["labelled"], 0)
        wanted = scoring_eval.approach_metrics(scoring_eval.build_groups(jobs, members, conflicts="wanted"), "v1")
        skip = scoring_eval.approach_metrics(scoring_eval.build_groups(jobs, members, conflicts="skip"), "v1")
        self.assertEqual((wanted["skips_among_recommended"], wanted["recall_wanted"]), (0, 1.0))
        self.assertEqual(skip["skips_among_recommended"], 1)

    def test_a_group_takes_each_approach_s_best_member_status(self):
        jobs = [job_eval("j1", "check_first", production="archive", baseline="B"), job_eval("j2", "near_miss", production="near_miss")]
        group = scoring_eval.build_groups(jobs, {"j1": ["j1", "j2"]})[0]
        self.assertEqual((group.statuses["v1"], group.statuses["production"], group.statuses["baseline"], group.rep.job_id),
                         ("check_first", "near_miss", "B", "j1"))

    def test_check_first_orders_and_wanted_found_per_review(self):
        jobs = [job_eval("j1", "check_first", "skip", a=assessment(40, 95, facts=4)),
                job_eval("j2", "check_first", "apply", a=assessment(60, 85, facts=1)),
                job_eval("j3", "check_first", "apply", a=assessment(55, 90, facts=2))]
        groups = scoring_eval.build_groups(jobs, ri.ungrouped([j.job for j in jobs]))
        result = scoring_eval.check_first_order(groups, CFG)
        o1 = {r["k"]: r for r in result["O1"]}
        o2 = {r["k"]: r for r in result["O2"]}
        self.assertEqual((o1[1]["wanted_found"], o1[3]["wanted_found"]), (0, 2))    # O1: best case first (the skip)
        self.assertEqual((o2[1]["wanted_found"], o2[3]["per_review"]), (1, 0.667))  # O2: known qualification first
        self.assertEqual(o1[10]["reviewed"], 3)

    def test_opportunity_order_counts_pairs_in_each_queue(self):
        jobs = [job_eval("j1", "B", "apply"), job_eval("j2", "B", "skip"), job_eval("j3", "stretch_review", "stretch")]
        groups = scoring_eval.build_groups(jobs, ri.ungrouped([j.job for j in jobs]))
        result = scoring_eval.opportunity_order(groups, CFG)
        self.assertEqual((result["apply"]["labelled"], result["apply"]["OM1"]["pairs"], result["stretch"]["OM1"]["pairs"]), (2, 1, 0))

    def test_equal_employer_weights_sum_to_one_per_employer(self):
        jobs = [job_eval("j1", "B", "apply", parent="A"), job_eval("j2", "B", "skip", parent="A"), job_eval("j3", "B", "apply", parent="B")]
        groups = scoring_eval.build_groups(jobs, ri.ungrouped([j.job for j in jobs]))
        weights = scoring_eval.employer_weights(groups)
        self.assertEqual(weights, {"j1": 0.5, "j2": 0.5, "j3": 1.0})
        self.assertEqual(scoring_eval.approach_metrics(groups, "v1", weights)["skip_share_among_recommendations"], 0.25)

    def test_avoid_orders_after_the_others_of_the_same_status(self):
        a = assessment(85, 85)
        d = sp.Decision("B", "B", "3b")
        keys = sorted([("avoided", True), ("plain", False)], key=lambda t: sp.order_key("B", a, d, t[0], CFG, avoid=t[1]))
        self.assertEqual([k for k, _ in keys], ["plain", "avoided"])
        self.assertLess(sp.order_key("A", a, d, "z", CFG, avoid=True), sp.order_key("B", a, d, "a", CFG))   # never across statuses

    def test_group_open_when_any_member_is_open(self):
        jobs = [job_eval("j1"), job_eval("j2")]
        jobs[0].job["closed_at"] = "2026-10-05T00:00:00.000Z"
        self.assertTrue(Group("j1", jobs, {}, jobs[0]).open)
        jobs[1].job["closed_at"] = "2026-10-05T00:00:00.000Z"
        self.assertFalse(Group("j1", jobs, {}, jobs[0]).open)


if __name__ == "__main__":
    unittest.main()
