"""Daily backup and the after-run notification. Synthetic data only; no real notification is shown."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.ops.backup import MANIFEST, BackupError, backup, prune, verify
from careeros.ops.notify import summary, toast

from tests.helpers import add_job


def score(store, job_id, tier, priority=80.0):
    store.writer("qualify").insert("scores", {
        "score_id": f"s:{job_id}", "job_id": job_id, "at": store.clock.now(), "profile_fingerprint": "p", "policy_fingerprint": "q",
        "eligibility": 90, "candidate_fit": priority, "career_value": 70, "freshness": 90,
        "apply_priority": priority, "tier": tier, "interview_band": "Moderate", "components_json": "{}"})


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.clock = FixedClock("2026-10-11T07:00:00.000Z")
        self.paths = {"backups": root / "backups", "profile": root / "profile.yaml", "overrides": root / "overrides.yaml"}
        self.paths["profile"].write_text("status: approved\n", encoding="utf-8")
        self.paths["overrides"].write_text("compensation: {}\n", encoding="utf-8")
        self.store = open_store(root / "careeros.db", self.clock, "test-policy")
        add_job(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_backup_is_complete_verified_and_replaces_the_same_day(self):
        folder = backup(self.store, self.paths)
        self.assertEqual(folder.name, "2026-10-11")
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual((manifest["integrity_check"], manifest["jobs"]), ("ok", 1))
        self.assertEqual(sorted(manifest["files"]), ["careeros.db", "overrides.yaml", "profile.yaml"])
        self.assertEqual(verify(folder), [])
        add_job(self.store, "gh:acme:2")
        again = backup(self.store, self.paths)
        self.assertEqual(json.loads((again / MANIFEST).read_text(encoding="utf-8"))["jobs"], 2)
        (folder / "profile.yaml").write_text("tampered\n", encoding="utf-8")
        self.assertEqual(verify(folder), ["profile.yaml"])

    def test_pruning_keeps_the_newest_and_never_touches_other_folders(self):
        for day in range(1, 6):
            self.clock = FixedClock(f"2026-10-0{day}T07:00:00.000Z")
            self.store.clock = self.clock
            backup(self.store, self.paths, keep=3)
        (self.paths["backups"] / "pre-migration-004").mkdir()
        (self.paths["backups"] / "2026-09-01").mkdir()  # looks like ours but has no manifest
        prune(self.paths["backups"], 1)
        self.assertEqual(sorted(p.name for p in self.paths["backups"].iterdir()), ["2026-09-01", "2026-10-05", "pre-migration-004"])
        with self.assertRaises(BackupError):
            from careeros.ops.backup import _remove
            _remove(self.paths["backups"] / "pre-migration-004")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock("2026-10-11T07:00:00.000Z")
        self.store = open_store(":memory:", self.clock, "test-policy")
        self.started = self.clock.now()

    def test_summary_counts_only_new_jobs_worth_a_look_and_relevant_leads(self):
        add_job(self.store, "gh:old:1", current_state="scored")
        score(self.store, "gh:old:1", "A")
        self.assertIsNone(summary(self.store, "2026-10-11T07:00:00.001Z"))
        self.clock.advance(minutes=5)
        for n, tier in enumerate(("A", "near_miss", "archive", "B", "B", "B")):
            add_job(self.store, f"gh:new:{n}", current_state="scored", title=f"Claims Analyst {n}", employer_name="Synth <Health>")
            score(self.store, f"gh:new:{n}", tier, priority=90 - n)
        add_job(self.store, "gh:new:v", current_state="scored", variant_of="gh:new:0")
        score(self.store, "gh:new:v", "A")
        self.store.writer("discovery").insert("leads", {"lead_id": "l1", "url": "https://x.example/1", "site": "linkedin",
                                                         "title": "Claims Analyst", "title_norm": "claims analyst",
                                                         "family": "pbm_claims_operations", "first_seen_at": self.clock.now(), "status": "new"})
        title, body = summary(self.store, "2026-10-11T07:01:00.000Z")
        self.assertEqual(title, "CareerOS: 5 new jobs worth a look and 1 new email lead")
        self.assertTrue(body.startswith("Claims Analyst 0 (Synth <Health>); Claims Analyst 1"))
        self.assertTrue(body.endswith("+2 more"))

    def test_toast_passes_text_only_through_the_environment(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw["env"]))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        hostile = "'; Remove-Item -Recurse $HOME; '"
        import sys
        if sys.platform == "win32":
            self.assertTrue(toast(hostile, "<b>body</b>", runner=runner))
            argv, env = calls[0]
            self.assertNotIn(hostile, " ".join(argv))
            self.assertEqual((env["CAREEROS_TOAST_TITLE"], env["CAREEROS_TOAST_BODY"]), (hostile, "<b>body</b>"))
            self.assertFalse(toast("t", "b", runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"boom")))
            self.assertFalse(toast("t", "b", runner=lambda *a, **k: (_ for _ in ()).throw(OSError("no powershell"))))


class DecisionFingerprintTests(unittest.TestCase):
    def test_operational_settings_never_re_evaluate_jobs(self):
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        from careeros.pipeline import Context, decision_fingerprint
        from tests.test_saved import POLICY

        def ctx(data):
            return Context(None, PolicyBundle(data=data, fingerprint=fingerprint(data)), None, None, None)

        base = decision_fingerprint(ctx(POLICY.data))
        quieter = {**POLICY.data, "operations": {**POLICY.data["operations"], "notifications": False, "backup_keep": 3}}
        self.assertEqual(decision_fingerprint(ctx(quieter)), base)
        stricter = {**POLICY.data, "gate": {**POLICY.data["gate"], "max_seniority_gap": 1}}
        self.assertNotEqual(decision_fingerprint(ctx(stricter)), base)

    def test_night_settings_re_check_night_only(self):
        from careeros.core.fingerprint import fingerprint
        from careeros.core.policy import PolicyBundle
        from careeros.core.clock import FixedClock
        from careeros.db.store import open_store
        from careeros.pipeline import Context, _night_fingerprint, decision_fingerprint
        from tests.test_saved import POLICY
        store = open_store(":memory:", FixedClock(), "p")

        def ctx(data):
            return Context(store, PolicyBundle(data=data, fingerprint=fingerprint(data)), None, None, None)

        class P:  # minimal profile stand-in
            fingerprint = "p"

        job = {"job_id": "gh:none:1", "content_fingerprint": "c", "employment_type": "full_time", "remote_type": "remote", "remote_states_json": "[]",
               "remote_excluded_states_json": "[]", "country": "US", "comp_max": None}
        base, paid = ctx(POLICY.data), ctx({**POLICY.data, "night": {**POLICY.data["night"], "enabled": True}})
        base.profile = paid.profile = P()
        self.assertEqual(decision_fingerprint(base), decision_fingerprint(paid))  # PRIMARY is not re-evaluated
        self.assertNotEqual(_night_fingerprint(base, job), _night_fingerprint(paid, job))  # NIGHT is


if __name__ == "__main__":
    unittest.main()


class RunFailureNoticeTests(unittest.TestCase):
    def test_a_failed_daily_run_shows_a_notification(self):
        import os
        from unittest import mock
        from careeros import cli
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "profile").mkdir(parents=True)
            (vault / "policy").mkdir()
            (vault / "profile" / "candidate_profile_v0.yaml").write_text('a: {b: "x "y" z"}\n', encoding="utf-8")  # broken YAML
            (vault / "policy" / "overrides.yaml").write_text("{}\n", encoding="utf-8")
            shown = []
            with mock.patch.dict(os.environ, {"CAREEROS_VAULT": str(vault)}), \
                    mock.patch("careeros.ops.notify.toast", lambda title, body, **kw: shown.append(title) or True):
                with self.assertRaises(Exception):  # noqa: B017 - any failure: the point is the notification below
                    cli.main(["run"])
            for handler in list(cli.log.handlers):  # release the temp log file
                handler.close()
                cli.log.removeHandler(handler)
        self.assertEqual(shown, ["CareerOS: daily run failed"])
