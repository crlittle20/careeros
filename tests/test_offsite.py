"""The offsite archive (OPP-26, the owner's decision D97, 2026-11-03): an encrypted copy of the newest backup and the
vault folders you wrote by hand, in a folder you choose. Synthetic data and a test passphrase only. The tests run the
real gpg (Git for Windows' on Windows, the system's elsewhere) and are skipped without one."""
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from careeros import cli
from careeros.core.clock import FixedClock
from careeros.core.credentials import MemoryCredentials
from careeros.core.policy import load_policy
from careeros.db.store import open_store
from careeros.ops import backup as backup_mod
from careeros.ops import offsite
from careeros.paths import CONFIG_DIR, vault_paths
from tests.helpers import add_job

PASS = "synthetic-test-passphrase"
MARKER = "SYNTHETIC-OFFSITE-MARKER"
TODAY = "careeros-offsite-2026-11-04.zip.gpg"


class Base(unittest.TestCase):
    def setUp(self):
        self.gpg = offsite.find_gpg()
        if not self.gpg:
            self.skipTest("no gpg on this machine")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = vault_paths(root / "vault")
        self.store = open_store(self.paths["db"], FixedClock("2026-11-04T12:00:00.000Z"), "test-policy")
        add_job(self.store)
        for name, text in (("profile/candidate_profile_v0.yaml", f"status: approved  # {MARKER}\n"),
                           ("policy/overrides.yaml", "compensation: {}\n"), ("documents/resume.txt", f"{MARKER} resume\n"),
                           ("snapshots/posting.html", "<html>re-creatable</html>"), ("logs/careeros.log", "a log line\n")):
            path = self.paths["root"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.dest = root / "OneDrive" / "CareerOS-offsite"
        self.dest.mkdir(parents=True)
        self.folder = backup_mod.backup(self.store, self.paths)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, **kw) -> dict:
        args = {"passphrase": PASS, "gpg": self.gpg, "at": self.store.clock.now(), **kw}
        return offsite.make(self.folder, self.paths["root"], self.dest, **args)

    def gpg_run(self, args: list[str], passphrase: str = PASS) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as home:
            return offsite._gpg(self.gpg, Path(home), args, passphrase)

    def work_left(self) -> list[str]:
        """Work folders left in vault\\backups: the shared one of old, or any call's own (D149 red team 7, 7b)."""
        return sorted(p.name for p in self.paths["backups"].iterdir() if p.name.startswith(offsite.WORK))

    def assert_left_nothing(self):
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), [])
        self.assertEqual(self.work_left(), [])


class ArchiveTests(Base):
    def test_an_archive_is_encrypted_complete_and_only_it_reaches_the_folder(self):
        result = self.make()
        self.assertEqual((result["ok"], result["archive"], result["removed"]), (True, TODAY, []))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), [TODAY])   # no partial file, no plaintext
        self.assertEqual(self.work_left(), [])       # the unencrypted zip is gone
        data = (self.dest / TODAY).read_bytes()
        self.assertIn(data[0], offsite.OPENPGP_SYMMETRIC)
        for plain in (MARKER.encode(), b"PK\x03\x04", b"SQLite format 3"):
            self.assertNotIn(plain, data)
        packets = self.gpg_run(["--list-packets", str(self.dest / TODAY)]).stdout.decode()
        self.assertIn("cipher 9", packets)                         # AES-256
        self.assertIn("s2k 3, hash 10", packets)                   # iterated and salted, SHA-512
        self.assertIn("count 65011712", packets)
        self.assertNotEqual(self.gpg_run(["--decrypt", str(self.dest / TODAY)], "not the passphrase").returncode, 0)
        done = self.gpg_run(["--decrypt", str(self.dest / TODAY)])
        self.assertEqual(done.returncode, 0)
        with zipfile.ZipFile(io.BytesIO(done.stdout)) as archive:
            manifest = json.loads(archive.read(offsite.MANIFEST))
            self.assertEqual(sorted(manifest["files"]), ["careeros-backup.json", "careeros.db", "vault/documents/resume.txt",
                                                         "vault/policy/overrides.yaml", "vault/profile/candidate_profile_v0.yaml"])
            self.assertEqual(archive.read("careeros.db"), (self.folder / "careeros.db").read_bytes())
            self.assertIn(MARKER, archive.read("vault/documents/resume.txt").decode())
        self.assertEqual((manifest["backup"], result["files"]), ("2026-11-04", 5))

    def test_the_calibration_lists_and_the_power_bi_folder_are_kept_and_daily_reports_are_not(self):
        """Pre-review #4 (D111): a calibration list is written once and cannot be drawn again, and your Power BI report is
        your own work; the daily HTML reports CareerOS makes again are left out."""
        for name in ("reports/calibration/calibration-2026-11-04.json", "reports/powerbi/CareerOS.pbix",
                     "reports/report-2026-11-04.html"):
            path = self.paths["root"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{MARKER} {name}", encoding="utf-8")
        result = self.make()
        done = self.gpg_run(["--decrypt", str(self.dest / TODAY)])
        with zipfile.ZipFile(io.BytesIO(done.stdout)) as archive:
            names = set(json.loads(archive.read(offsite.MANIFEST))["files"])
            self.assertEqual(archive.read("vault/reports/powerbi/CareerOS.pbix").decode(), f"{MARKER} reports/powerbi/CareerOS.pbix")
        self.assertLessEqual({"vault/reports/calibration/calibration-2026-11-04.json", "vault/reports/powerbi/CareerOS.pbix"}, names)
        self.assertNotIn("vault/reports/report-2026-11-04.html", names)
        self.assertEqual(result["files"], 7)

    def test_the_passphrase_never_reaches_a_command_line(self):
        seen = []
        real = subprocess.run

        def spy(command, **kw):
            seen.append((command, kw.get("input")))
            return real(command, **kw)
        with mock.patch.object(offsite.subprocess, "run", spy):
            self.make()
        self.assertEqual(len(seen), 2)   # encrypt, then decrypt back
        for command, given in seen:
            self.assertFalse(any(PASS in part for part in command), command)
            self.assertEqual(given, f"{PASS}\n".encode())

    def test_nothing_is_left_in_the_folder_when_anything_fails(self):
        fail = lambda code, err=b"gpg: boom": subprocess.CompletedProcess([], code, b"", err)  # noqa: E731
        real = offsite._gpg

        def plaintext_copier(gpg, home, args, passphrase):   # a "gpg" that copies its input unencrypted
            if "--symmetric" in args:
                Path(args[args.index("--output") + 1]).write_bytes(Path(args[-1]).read_bytes())
                return fail(0, b"")
            return subprocess.CompletedProcess([], 0, Path(self.dest / f".{TODAY}.partial").read_bytes(), b"")

        def wrong_back(gpg, home, args, passphrase):          # encrypts, but decrypts back to other bytes
            if "--symmetric" in args:
                return real(gpg, home, args, passphrase)
            return subprocess.CompletedProcess([], 0, b"other bytes", b"")

        def disk_full(gpg, home, args, passphrase):           # half an archive, then no space
            Path(args[args.index("--output") + 1]).write_bytes(b"\x8c\x0d half")
            return fail(2, b"gpg: error writing to output: No space left on device")
        cases = {"no passphrase": ({"passphrase": None}, None, "no passphrase"),
                 "a line break": ({"passphrase": "two\nlines"}, None, "line break"),
                 "no gpg": ({"gpg": None}, None, "gpg not found"),
                 "gpg fails": ({}, lambda *a: fail(2), "did not encrypt"),
                 "a plaintext copy": ({}, plaintext_copier, "not an OpenPGP symmetric message"),
                 "a wrong round trip": ({}, wrong_back, "did not decrypt back"),
                 "a full disk": ({}, disk_full, "No space left on device")}
        for name, (kw, runner, says) in cases.items():
            with self.subTest(name), mock.patch.object(offsite, "_gpg", runner or real):
                with self.assertRaisesRegex(offsite.OffsiteError, says):
                    self.make(**kw)
                self.assert_left_nothing()
        with mock.patch.object(backup_mod, "verify", lambda folder: ["careeros.db"]), \
                self.assertRaisesRegex(offsite.OffsiteError, "does not verify: careeros.db"):
            self.make()
        self.assert_left_nothing()
        with self.assertRaisesRegex(offsite.OffsiteError, "does not exist"):
            offsite.make(self.folder, self.paths["root"], self.dest / "missing", passphrase=PASS, gpg=self.gpg,
                         at=self.store.clock.now())

    def test_pruning_keeps_the_newest_and_touches_only_careeros_archives(self):
        for day in range(20, 29):
            (self.dest / f"careeros-offsite-2026-10-{day}.zip.gpg").write_bytes(b"\x8c old")
        others = ["notes.txt", "careeros-offsite-latest.zip.gpg", "careeros-offsite-2026-10-01.zip", "backup.zip.gpg"]
        for name in others:
            (self.dest / name).write_bytes(b"not ours")
        (self.dest / "careeros-offsite-2026-01-01.zip.gpg").mkdir()   # a folder with an archive's name
        result = self.make(keep=7)
        self.assertEqual(result["removed"], [f"careeros-offsite-2026-10-{day}.zip.gpg" for day in range(20, 23)])
        self.assertEqual([p.name for p in offsite.ours(self.dest)],
                         [f"careeros-offsite-2026-10-{day}.zip.gpg" for day in range(23, 29)] + [TODAY])
        for name in [*others, "careeros-offsite-2026-01-01.zip.gpg"]:
            self.assertTrue((self.dest / name).exists(), name)
        self.assertEqual(offsite.prune(self.dest, 0),   # never fewer than one
                         [self.dest / f"careeros-offsite-2026-10-{day}.zip.gpg" for day in range(23, 29)])
        self.assertEqual([p.name for p in offsite.ours(self.dest)], [TODAY])


class DrillTests(Base):
    def drill(self, passphrase=PASS) -> dict:
        return offsite.drill(self.dest, passphrase=passphrase, gpg=self.gpg, area=self.paths["backups"])

    def test_the_drill_reads_the_newest_archive_back_and_catches_damage(self):
        self.assertEqual(self.drill()["problems"], ["no archive in the offsite folder"])
        self.make()
        self.assertEqual(self.drill(), {"ok": True, "archive": TODAY, "files": 5, "problems": []})
        self.assertEqual(self.work_left(), [])
        self.assertFalse(self.drill("not the passphrase")["ok"])
        data = bytearray((self.dest / TODAY).read_bytes())
        data[len(data) // 2] ^= 0xFF   # one byte damaged, as a bad sync might
        (self.dest / TODAY).write_bytes(bytes(data))
        damaged = self.drill()
        self.assertFalse(damaged["ok"])
        self.assertTrue(damaged["problems"][0].startswith("OffsiteError: it did not decrypt"), damaged["problems"])

    def test_the_drill_checks_every_file_against_the_manifest(self):
        """An archive that decrypts but whose file is not the one its manifest describes."""
        plain = self.paths["root"] / "forged.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("careeros.db", b"not the database")
            archive.writestr(offsite.MANIFEST, json.dumps({"files": {"careeros.db": {"sha256": "0" * 64, "bytes": 3}}}))
        done = self.gpg_run(["--symmetric", *offsite.CIPHER, "--output", str(self.dest / TODAY), str(plain)])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.drill()["problems"], ["careeros.db"])


class DailyRunTests(Base):
    def policy(self, folder: Path | None):
        overrides = self.paths["root"] / "policy" / "overrides.yaml"
        overrides.write_text("operations:\n  offsite:\n    folder: " + (json.dumps(folder.as_posix()) if folder else "null") + "\n",
                             encoding="utf-8")
        return load_policy(CONFIG_DIR / "policy" / "defaults.yaml", overrides)

    def run_offsite(self, folder, credentials):
        ctx = cli.Context(self.store, self.policy(folder), None, None, None, None)
        shown: list[str] = []
        with mock.patch("careeros.ops.notify.problem", lambda title, body: shown.append(title)), redirect_stdout(io.StringIO()) as out:
            result = cli._offsite(ctx, self.paths, self.folder, credentials)
        return result, shown, out.getvalue(), ctx

    def test_the_daily_run_writes_records_drills_weekly_and_status_says_so(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        result, shown, out, ctx = self.run_offsite(self.dest, credentials)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["drill"]["ok"], shown), (True, []))
        self.assertIn(f"Offsite archive: {TODAY}", out)
        self.assertIn(f"Offsite drill: {TODAY} decrypted and read back whole (5 files)", out)
        line = cli._offsite_line(ctx.policy, self.paths, self.store.clock.now())
        self.assertTrue(line.startswith(f"offsite archive: {TODAY} ok ("), line)
        self.assertTrue(line.endswith("; drill 2026-11-04 ok"), line)
        again, _, out, _ = self.run_offsite(self.dest, credentials)   # the same day: replaced, not drilled again
        self.assertTrue(again["ok"])
        self.assertNotIn("drill", again)
        self.assertNotIn("Offsite drill", out)
        self.assertEqual(offsite.last(self.paths["backups"])["drill"]["at"], "2026-11-04T12:00:00.000Z")   # kept
        self.assertEqual([p.name for p in self.dest.iterdir()], [TODAY])
        self.assertTrue(cli._offsite_line(ctx.policy, self.paths, "2026-11-08T12:00:00.000Z")
                        .startswith("!! offsite archive: the newest (2026-11-04) is 4 days old"))

    def test_the_drill_runs_again_a_week_later_and_the_day_after_a_failed_one(self):
        """Fable review of 559212b, finding 11: the weekly cadence itself, both ways it could stop, and status saying so
        when the drill is overdue (as it does of the restore drill)."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        drilled = lambda result: result.get("drill", {}).get("at")
        first, _, _, ctx = self.run_offsite(self.dest, credentials)
        self.assertEqual(drilled(first), "2026-11-04T12:00:00.000Z")
        self.store.clock.advance(days=6)
        self.assertIsNone(drilled(self.run_offsite(self.dest, credentials)[0]))        # 6 days: not yet
        self.store.clock.advance(days=1)
        self.assertEqual(drilled(self.run_offsite(self.dest, credentials)[0]), "2026-11-11T12:00:00.000Z")   # a week
        self.store.clock.advance(days=7)
        failed = {"ok": False, "archive": "careeros-offsite-2026-11-18.zip.gpg", "problems": ["careeros.db"]}
        with mock.patch.object(offsite, "drill", lambda *a, **kw: dict(failed)):
            self.assertFalse(self.run_offsite(self.dest, credentials)[0]["drill"]["ok"])
        self.store.clock.advance(days=1)
        retried = self.run_offsite(self.dest, credentials)[0]                          # the next day, not a week later
        self.assertEqual((drilled(retried), retried["drill"]["ok"]), ("2026-11-19T12:00:00.000Z", True))
        entry = offsite.last(self.paths["backups"])
        stale = {**entry, "drill": {**entry["drill"], "at": "2026-11-10T12:00:00.000Z"}}   # a fresh archive, an old drill
        offsite.record(self.paths["backups"], stale, entry["at"])
        self.assertEqual(cli._offsite_line(ctx.policy, self.paths, "2026-11-19T13:00:00.000Z"),
                         "!! offsite drill: the last one (2026-11-10) is 9 days old; the daily run makes one weekly")
        self.assertTrue(cli._offsite_line(ctx.policy, self.paths, "2026-11-18T13:00:00.000Z").endswith("; drill 2026-11-10 ok"))

    def test_a_failure_is_recorded_notified_and_shown_and_the_run_goes_on(self):
        result, shown, out, ctx = self.run_offsite(self.dest, MemoryCredentials())   # no passphrase saved
        self.assertFalse(result["ok"])
        self.assertEqual(shown, ["CareerOS: offsite archive failed"])
        self.assertIn("! offsite archive failed: OffsiteError: no passphrase", out)
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "offsite_failed")
        self.assertTrue(cli._offsite_line(ctx.policy, self.paths, self.store.clock.now())
                        .startswith("!! offsite archive failed 2026-11-04: OffsiteError: no passphrase"))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_a_failed_drill_is_recorded_notified_and_shown(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        with mock.patch.object(offsite, "drill", lambda *a, **kw: {"ok": False, "archive": TODAY, "problems": ["careeros.db"]}):
            result, shown, out, ctx = self.run_offsite(self.dest, credentials)
        self.assertTrue(result["ok"])
        self.assertEqual(shown, ["CareerOS: offsite drill failed"])
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "offsite_drill_failed")
        self.assertEqual(cli._offsite_line(ctx.policy, self.paths, self.store.clock.now()),
                         f"!! offsite drill failed 2026-11-04 on {TODAY}: careeros.db")

    def test_a_record_that_cannot_be_saved_is_recorded_and_notified(self):
        """F2 of Codex CR-2026-11-03-PYTHON-314, found in the restore drill's bookkeeping and applied here."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        ctx = cli.Context(self.store, self.policy(self.dest), None, None, None, None)
        shown: list[tuple[str, str]] = []
        with mock.patch.object(offsite, "record", side_effect=PermissionError("read-only folder")), \
                mock.patch("careeros.ops.notify.problem", lambda title, body: shown.append((title, body))), \
                redirect_stdout(io.StringIO()) as out, self.assertLogs(cli.log, "ERROR"):
            result = cli._offsite(ctx, self.paths, self.folder, credentials)
        self.assertFalse(result["ok"])
        self.assertEqual(shown, [("CareerOS: offsite archive failed",
                                  "Its record could not be saved, so status cannot show it. Details: careeros status")])
        self.assertIn("result not saved (PermissionError: read-only folder)", out.getvalue())
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "offsite_failed")

    def test_off_until_a_folder_is_set_and_never_in_demo_mode(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        result, shown, out, ctx = self.run_offsite(None, credentials)
        self.assertEqual((result, out), (None, ""))
        self.assertTrue(cli._offsite_line(ctx.policy, self.paths, self.store.clock.now()).startswith("offsite archive: off ("))
        with mock.patch("careeros.demo.sandbox.enabled", lambda: True):
            self.assertEqual(self.run_offsite(self.dest, credentials)[0], None)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertIsNone(offsite.last(self.paths["backups"]))

    def test_backup_offsite_on_the_command_line(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        ctx = cli.Context(self.store, self.policy(self.dest), None, None, None, None)
        with mock.patch("careeros.core.credentials.WindowsCredentials", lambda: credentials), redirect_stdout(io.StringIO()) as out:
            cli.cmd_backup(ctx, self.paths, SimpleNamespace(offsite=True))
        self.assertIn("(verified)", out.getvalue())
        self.assertIn(f"Offsite archive: {TODAY}", out.getvalue())
        with mock.patch("careeros.core.credentials.WindowsCredentials", MemoryCredentials), \
                mock.patch("careeros.ops.notify.problem", lambda title, body: None), redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as failed:   # no passphrase saved: a failure, with exit code 2
            cli.cmd_backup(ctx, self.paths, SimpleNamespace(offsite=True))
        self.assertEqual(failed.exception.code, 2)
        off = cli.Context(self.store, self.policy(None), None, None, None, None)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "Offsite archive: off"):
            cli.cmd_backup(off, self.paths, SimpleNamespace(offsite=True))
        with redirect_stdout(io.StringIO()):
            cli.cmd_backup(off, self.paths, SimpleNamespace())   # without --offsite: the backup alone, as before


class ConfirmTests(Base):
    """Pre-review #1 (D111): every other check uses the Credential Manager copy of the passphrase, which is lost with the
    PC. `careeros offsite confirm` opens the newest archive with the copy you keep off it, typed in."""
    policy = DailyRunTests.policy
    run_offsite = DailyRunTests.run_offsite

    def confirm(self, ctx, typed: str) -> tuple[str, int | str | None]:
        with mock.patch("getpass.getpass", lambda prompt: typed), redirect_stdout(io.StringIO()) as out:
            try:
                cli.cmd_offsite(ctx, self.paths, SimpleNamespace(action="confirm"))
            except SystemExit as stop:
                return out.getvalue(), stop.code
        return out.getvalue(), None

    def test_status_warns_until_the_copy_kept_off_this_pc_opens_the_newest_archive(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        line = lambda: cli._passphrase_line(ctx.policy, self.paths, credentials)
        self.assertEqual(line(), "!! offsite passphrase: the copy you keep off this PC has never opened an archive; "
                                 "run careeros offsite confirm")
        out, code = self.confirm(ctx, "synthetic-test-passphrasf")   # one character wrong, as a typo at setup would be
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} did not open with it"), code)
        self.assertEqual(line(), f"!! offsite passphrase: the copy you keep off this PC did not open {TODAY} on 2026-11-04; "
                                 "run careeros offsite confirm")
        out, code = self.confirm(ctx, PASS)
        self.assertIsNone(code)
        self.assertIn(f"Confirmed: the copy you keep off this PC opened {TODAY}, and all 5 files read back whole", out)
        self.assertEqual(line(), f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04")
        record = (self.paths["backups"] / offsite.CONFIRMED).read_text(encoding="utf-8")
        self.assertNotIn(PASS, record)                                   # the date and the archive only
        self.assertEqual(json.loads(record), {"at": "2026-11-04T12:00:00.000Z", "archive": TODAY, "ok": True,
                                              "folder": str(self.dest), "key": credentials.written(offsite.CREDENTIAL),
                                              "sha256": offsite._sha256(self.dest / TODAY)})   # D149: the bytes it opened
        self.assertEqual(self.work_left(), [])
        (self.paths["backups"] / offsite.CONFIRMED).write_text('{"ok": true}', encoding="utf-8")   # a damaged record
        self.assertTrue(line().startswith("!! offsite passphrase: the copy you keep off this PC has never opened an archive"))

    def test_off_without_a_folder_and_refused_before_the_first_archive(self):
        off = cli.Context(self.store, self.policy(None), None, None, None, None)
        self.assertIsNone(cli._passphrase_line(off.policy, self.paths, MemoryCredentials()))
        self.assertTrue(str(self.confirm(off, PASS)[1]).startswith("Offsite archive: off"))
        on = cli.Context(self.store, self.policy(self.dest), None, None, None, None)
        self.assertEqual(self.confirm(on, PASS)[1], "No offsite archive yet: run careeros backup --offsite first")
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))
        with mock.patch("careeros.demo.sandbox.enabled", lambda: True):
            self.assertTrue(str(self.confirm(on, PASS)[1]).startswith("Offsite archive: off"))

    def test_a_new_stored_passphrase_or_folder_asks_for_the_kept_copy_again(self):
        """Codex CR-2026-11-05-COMBINED, F8: a confirmation holds only for the key generation and the folder it was made
        under. Saving the passphrase again (a new generation, even the same day) or moving the folder asks for the kept
        copy again; archives made under the confirmed key, the same day or later, keep it."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        line = lambda policy=ctx.policy: cli._passphrase_line(policy, self.paths, credentials)
        self.assertIsNone(self.confirm(ctx, PASS)[1])
        ok = f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04"
        self.assertEqual(line(), ok)
        self.store.clock.advance(days=1)
        self.run_offsite(self.dest, credentials)                    # control: the same key, the next day
        self.assertEqual(line(), ok)
        self.run_offsite(self.dest, credentials)                    # control: the same key, the same day (11-05 remade;
        self.assertEqual(line(), ok)                                # the confirmed 11-04 file is untouched, D149 R-O1)
        changed = (f"!! offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04, but the stored "
                   "passphrase or the offsite folder has changed since; run careeros offsite confirm")
        credentials.write(offsite.CREDENTIAL, "another-synthetic-passphrase")   # a new key, as the probe's
        self.assertEqual(line(), changed)
        self.run_offsite(self.dest, credentials)                    # the same day: the archive is now the new key's
        self.assertEqual(line(), changed)
        self.assertIsNone(self.confirm(ctx, "another-synthetic-passphrase")[1])
        self.assertTrue(line().startswith("offsite passphrase: the copy you keep off this PC opened "), line())
        other = self.dest.parent / "elsewhere"
        other.mkdir()
        self.assertEqual(line(self.policy(other)).split(" on ")[-1].split(",")[0], "2026-11-05")
        self.assertTrue(line(self.policy(other)).startswith("!! offsite passphrase:"), line(self.policy(other)))

    def test_a_confirmation_binds_the_key_that_made_the_archive_it_opened(self):
        """F8: the passphrase saved anew after the newest archive was made; the kept copy (the old key) still opens that
        archive, but the next archives use the new key, so status asks again."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        credentials.write(offsite.CREDENTIAL, "another-synthetic-passphrase")
        self.assertIsNone(self.confirm(ctx, PASS)[1])
        self.assertTrue(cli._passphrase_line(ctx.policy, self.paths, credentials).startswith("!! offsite passphrase:"))

    def test_a_confirmation_binds_only_the_very_file_the_record_made(self):
        """F8-03 (D120): the confirm-time key came from the newest record whenever its archive NAME matched. A same-day
        archive of the same name made in another folder (S1), or the same folder's file reverted to an older version
        (S2), then lent the new key to old-key evidence and status said ok. Now the record must be of the very file the
        drill opens: its folder and SHA-256 too. Control: the plain case still binds and status stays ok."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)                 # X: today's archive under key A
        other = self.dest.parent / "elsewhere"
        other.mkdir()
        credentials.write(offsite.CREDENTIAL, "another-synthetic-passphrase")   # key B
        self.run_offsite(other, credentials)                                    # Y: today's archive, same name, key B
        code = self.confirm(ctx, PASS)[1]                                      # back on X: kept A opens X's A archive,
        self.assertTrue(str(code).startswith(f"Not confirmed: the copy you keep off this PC opened {TODAY}, but "
                                             f"{offsite.NOT_MADE}"), code)   # but Y's record names other bytes (red team 6)
        self.assertTrue(cli._passphrase_line(ctx.policy, self.paths, credentials).startswith("!! offsite passphrase:"))

        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)
        older = (self.dest / TODAY).read_bytes()                                # S2: the same folder, key A's file
        credentials.write(offsite.CREDENTIAL, "another-synthetic-passphrase")
        self.run_offsite(self.dest, credentials)                                # the same day, key B's file
        (self.dest / TODAY).write_bytes(older)                                  # a sync or recycle-bin restore
        self.assertTrue(str(self.confirm(ctx, PASS)[1]).startswith("Not confirmed: the copy you keep off this PC opened "))
        self.assertTrue(cli._passphrase_line(ctx.policy, self.paths, credentials).startswith("!! offsite passphrase:"))

        self.run_offsite(self.dest, credentials)                                # control: key B's own file, confirmed
        self.assertIsNone(self.confirm(ctx, "another-synthetic-passphrase")[1])
        self.assertTrue(cli._passphrase_line(ctx.policy, self.paths, credentials).startswith("offsite passphrase: "))

    def test_a_passphrase_saved_while_an_archive_is_made_is_never_recorded_as_that_archives_key(self):
        """F8-03 (D120, the checker's Low): the run read the secret, then the key's generation. A passphrase saved between
        the two reads recorded an old-key archive as the new generation. The generation is read first now, so such an
        archive is recorded under the older generation and status asks again."""
        class SavedDuringTheRun(MemoryCredentials):
            def read(self, target):
                secret = super().read(target)
                if target == offsite.CREDENTIAL and self.saved[target] == 1:
                    self.write(target, "another-synthetic-passphrase")   # saved anew just after the secret was read
                return secret
        credentials = SavedDuringTheRun()
        credentials.write(offsite.CREDENTIAL, PASS)
        result = self.run_offsite(self.dest, credentials)[0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(offsite.last(self.paths["backups"])["key"], "1")       # the generation that made the archive


class D149SnapshotTests(Base):
    """D149 R-O1 (Codex CR-2026-11-05-COMBINED Pass 2, F8 reproduction G): the kept-passphrase confirmation rests on ONE
    snapshot of the archive's bytes, read after the prompt. gpg opens that snapshot; the key generation and the SHA-256
    recorded are that snapshot's; the archive is hashed again just before recording and nothing is recorded if it
    changed; status drops the confirmation when a file of the confirmed name has other bytes (a pruned or removed
    archive keeps it: that does not change which key opens the archives)."""
    policy = DailyRunTests.policy
    run_offsite = DailyRunTests.run_offsite
    OTHER = "another-synthetic-passphrase"

    def confirm(self, ctx, typed: str, at_prompt=None) -> tuple[str, int | str | None]:
        def prompt(text):
            if at_prompt:
                at_prompt()
            return typed
        with mock.patch("getpass.getpass", prompt), redirect_stdout(io.StringIO()) as out:
            try:
                cli.cmd_offsite(ctx, self.paths, SimpleNamespace(action="confirm"))
            except SystemExit as stop:
                self.assertNotIn(typed, str(stop.code))       # never a passphrase in what is said
                return out.getvalue(), stop.code
        self.assertNotIn(typed, out.getvalue())
        return out.getvalue(), None

    def around_decrypt(self, before=None, after=None):
        """The archive changes on disk while confirm's gpg runs: `before` just before it decrypts, `after` just after."""
        real = offsite._gpg

        def wrapped(gpg, home, args, passphrase, data=b""):
            if "--decrypt" in args and before:
                before()
            done = real(gpg, home, args, passphrase, data)
            if "--decrypt" in args and after:
                after()
            return done
        return mock.patch.object(offsite, "_gpg", wrapped)

    def line(self, ctx, credentials) -> str:
        return cli._passphrase_line(ctx.policy, self.paths, credentials)

    @staticmethod
    def not_made(archive: str) -> str:
        return (f"!! offsite passphrase: the copy you keep off this PC opened {archive} on 2026-11-04, but {offsite.NOT_MADE}; "
                "run careeros backup --offsite, then careeros offsite confirm")

    def around_gpg(self, before=None, after=None, calls=None):
        """`before(home, args)` just before gpg decrypts, `after(home, args)` just after; `home` is gpg's empty home in
        the call's own work folder. Only the first decrypt of the call is hooked, so a make or drill run from a hook is
        not hooked again. `calls` collects every decrypt's arguments and what reached gpg's standard input."""
        real = offsite._gpg
        state = {"busy": False}

        def wrapped(gpg, home, args, passphrase, data=b""):
            mine = "--decrypt" in args and not state["busy"]
            if "--decrypt" in args and calls is not None:
                calls.append((list(args), data, sorted(p.name for p in Path(home).parent.iterdir())))
            if mine and before:
                state["busy"] = True
                try:
                    before(Path(home), args)
                finally:
                    state["busy"] = False
            done = real(gpg, home, args, passphrase, data)
            if mine and after:
                after(Path(home), args)
            return done
        return mock.patch.object(offsite, "_gpg", wrapped)

    def write(self, data: bytes):
        return lambda: (self.dest / TODAY).write_bytes(data)

    def two_generations(self):
        """Today's archive made under key A (PASS), then remade under key B the same day: A's bytes, B's bytes."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        a_bytes = (self.dest / TODAY).read_bytes()
        credentials.write(offsite.CREDENTIAL, self.OTHER)
        self.run_offsite(self.dest, credentials)
        return a_bytes, (self.dest / TODAY).read_bytes(), ctx, credentials

    def test_codex_g_a_kept_passphrase_proven_on_swapped_bytes_is_never_bound_to_the_restored_archive(self):
        """Codex's exact sequence: B (the current key's archive) is in the folder; A's archive replaces it at the prompt;
        A is entered; B is restored afterwards (a later sync). Kept A cannot open B: status must not report confirmation."""
        a_bytes, b_bytes, ctx, credentials = self.two_generations()
        out, code = self.confirm(ctx, PASS)                                      # stable B: kept A does not open it
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} did not open with it"), code)
        out, code = self.confirm(ctx, PASS, at_prompt=self.write(a_bytes))      # A's ciphertext swapped in at the prompt
        self.assertTrue(str(code).startswith(f"Not confirmed: the copy you keep off this PC opened {TODAY}, but "
                                             f"{offsite.NOT_MADE}"), code)   # red team 6: never "Confirmed"
        self.assertNotIn("Confirmed", out)
        self.write(b_bytes)()                                                     # B restored, as a later sync would
        self.assertEqual(self.line(ctx, credentials), self.not_made(TODAY))       # the real reason, not "changed"
        self.assertNotEqual(self.gpg_run(["--decrypt", str(self.dest / TODAY)]).returncode, 0)   # kept A really can't
        recorded = json.loads((self.paths["backups"] / offsite.CONFIRMED).read_text(encoding="utf-8"))
        self.assertNotIn(PASS, json.dumps(recorded))
        self.assertIsNone(recorded["key"])                                       # A's bytes were not made by key B
        self.assertEqual(recorded["sha256"], hashlib.sha256(a_bytes).hexdigest())   # the bytes that actually opened

    def test_an_archive_replaced_after_the_snapshot_is_never_recorded(self):
        """Sibling: B's bytes land while gpg checks the snapshot of A (after it, before the record): refused, unrecorded."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, self.OTHER)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        b_bytes = (self.dest / TODAY).read_bytes()
        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)                                  # A, key PASS, is the newest and recorded
        with self.around_decrypt(after=self.write(b_bytes)):
            out, code = self.confirm(ctx, PASS)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} changed while it was being checked"), code)
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))               # nothing recorded
        self.assertTrue(self.line(ctx, credentials).startswith("!! offsite passphrase:"))
        self.assertEqual(self.work_left(), [])         # the snapshot copy is gone with it

    def test_an_archive_deleted_after_the_snapshot_is_never_recorded(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        with self.around_decrypt(after=lambda: (self.dest / TODAY).unlink()):
            out, code = self.confirm(ctx, PASS)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} changed while it was being checked"), code)
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))
        self.assertTrue(self.line(ctx, credentials).startswith("!! offsite passphrase:"))

    def test_gpg_opens_the_snapshot_not_whatever_the_file_holds_meanwhile(self):
        """Sibling: the file flickers to B's bytes during gpg and back to A's (a sync): the snapshot of A is what opens and
        what is recorded, and A is on disk again when it is recorded, so the confirmation holds."""
        _, b_bytes, ctx, credentials = self.two_generations()
        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)                                  # A again: recorded, the current key
        a_bytes = (self.dest / TODAY).read_bytes()
        with self.around_decrypt(before=self.write(b_bytes), after=self.write(a_bytes)):
            out, code = self.confirm(ctx, PASS)
        self.assertIsNone(code)
        self.assertIn(f"Confirmed: the copy you keep off this PC opened {TODAY}", out)
        self.assertEqual(self.line(ctx, credentials),
                         f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04")

    def test_status_drops_a_confirmation_only_when_its_archive_has_other_bytes(self):
        """Sibling: a file of the confirmed name with other bytes, even of the same key generation (remade the same day,
        or another copy synced over it), asks again (a needless ask is the accepted cost). The confirmed archive pruned or
        removed keeps the confirmation: the key generation, checked apart, says which key opens the archives. A record
        without the hash (before D149) never holds."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        first = (self.dest / TODAY).read_bytes()
        self.run_offsite(self.dest, credentials)                                  # the same generation, other bytes
        second = (self.dest / TODAY).read_bytes()
        self.assertNotEqual(first, second)
        self.assertIsNone(self.confirm(ctx, PASS)[1])                             # confirmed on `second`
        ok = f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04"
        self.assertEqual(self.line(ctx, credentials), ok)
        replaced = (f"!! offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04, but that archive "
                    "has been replaced since; run careeros offsite confirm")
        self.write(first)()                                                       # same generation, other bytes
        self.assertEqual(self.line(ctx, credentials), replaced)
        self.write(second)()
        self.assertEqual(self.line(ctx, credentials), ok)                         # the very bytes again
        self.run_offsite(self.dest, credentials)                                  # remade the same day
        self.assertEqual(self.line(ctx, credentials), replaced)
        self.assertIsNone(self.confirm(ctx, PASS)[1])
        confirmed_bytes = (self.dest / TODAY).read_bytes()
        self.store.clock.advance(days=1)
        self.run_offsite(self.dest, credentials)                                  # a newer archive, the same key
        (self.dest / TODAY).unlink()                                              # the confirmed one pruned or removed
        self.assertEqual(self.line(ctx, credentials), ok)                         # removal keeps it
        newer_key = MemoryCredentials()
        newer_key.write(offsite.CREDENTIAL, PASS)
        newer_key.write(offsite.CREDENTIAL, PASS)                                 # saved anew: another generation
        self.assertTrue(self.line(ctx, newer_key).startswith("!! offsite passphrase:"))   # a new key still asks
        self.write(first)()                                                       # the name back, with other bytes
        self.assertEqual(self.line(ctx, credentials), replaced)
        self.write(confirmed_bytes)()
        self.assertEqual(self.line(ctx, credentials), ok)
        record = self.paths["backups"] / offsite.CONFIRMED                        # a record from before D149: no hash
        entry = json.loads(record.read_text(encoding="utf-8"))
        entry.pop("sha256")
        record.write_text(json.dumps(entry), encoding="utf-8")
        self.assertEqual(self.line(ctx, credentials), replaced)
        (self.dest / TODAY).unlink()
        self.assertEqual(self.line(ctx, credentials), replaced)                   # without a hash, removal does not help

    # D149 red team round 1, findings 6, 7 and 7b (REDTEAM_ROUND1_intake_offsite.md, inputs O1, O3, O9, O12)

    def test_rt6_o3_a_newer_name_holding_foreign_bytes_is_not_confirmed(self):
        """O3: a newer-dated name holding A's bytes appears at the prompt and kept A is typed: it opens, but no key
        generation is bound, so the command says Not confirmed (non-zero) and status gives that same reason."""
        a_bytes, _, ctx, credentials = self.two_generations()                   # current key: B
        newer = self.dest / "careeros-offsite-2026-11-05.zip.gpg"
        out, code = self.confirm(ctx, PASS, at_prompt=lambda: newer.write_bytes(a_bytes))
        self.assertTrue(str(code).startswith(f"Not confirmed: the copy you keep off this PC opened {newer.name}, but "
                                             f"{offsite.NOT_MADE}"), code)
        self.assertNotIn("Confirmed", out)
        newer.unlink()                                                            # then gone (pruned-like)
        self.assertEqual(self.line(ctx, credentials), self.not_made(newer.name))

    def test_rt7_o9_a_foreign_archive_written_over_the_file_during_gpg_is_never_recorded(self):
        """O9, since D155 with no staged copy: a foreign archive that the typed (wrong) copy opens is written over the
        archive while gpg runs. gpg decrypts the bytes read before, which that copy cannot open, and the re-hash before
        recording refuses: nothing is recorded and the command says to try again; never a confirmation."""
        _, b_bytes, ctx, credentials = self.two_generations()                   # b_bytes: key OTHER's archive
        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)                                  # current key PASS: OTHER can't open it
        with self.around_gpg(before=lambda home, args: self.write(b_bytes)()):
            out, code = self.confirm(ctx, self.OTHER)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} changed while it was being checked"), code)
        self.assertTrue(str(code).endswith("Run careeros offsite confirm again."), code)
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))
        self.assertTrue(self.line(ctx, credentials).startswith("!! offsite passphrase:"))
        self.assertEqual(self.work_left(), [])

    def test_rt7b_o12_the_archive_removed_while_gpg_runs_says_try_again_not_did_not_open(self):
        """O12, since D155 with no staged copy: the archive is removed while gpg runs and the CORRECT kept copy is typed:
        try again, nothing recorded, no advice to re-save the stored passphrase."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        with self.around_gpg(before=lambda home, args: (self.dest / TODAY).unlink()):
            out, code = self.confirm(ctx, PASS)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} changed while it was being checked"), code)
        self.assertNotIn("did not open", str(code))
        self.assertNotIn("Credential Manager", str(code))
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))

    def test_rt7_the_archive_changed_after_gpg_read_it_is_never_recorded(self):
        """Sibling: the archive changes after gpg decrypted the bytes (the re-hash before recording): nothing is recorded."""
        _, b_bytes, ctx, credentials = self.two_generations()
        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)
        with self.around_gpg(after=lambda home, args: self.write(b_bytes)()):
            out, code = self.confirm(ctx, PASS)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} changed while it was being checked"), code)
        self.assertIsNone(offsite.confirmed(self.paths["backups"]))

    def test_rt7b_a_daily_make_and_drill_during_confirm_never_touch_its_work_folder(self):
        """Sibling (the real-world O12): the daily run's drill and a make (into another folder) run while confirm's gpg
        decrypts: each uses its own work folder, so the confirmation still stands, and none is left."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        other = self.dest.parent / "elsewhere"
        other.mkdir()

        def daily_run(home, args):
            self.assertTrue(offsite.drill(self.dest, passphrase=PASS, gpg=self.gpg, area=self.paths["backups"])["ok"])
            offsite.make(self.folder, self.paths["root"], other, passphrase=PASS, gpg=self.gpg, at=self.store.clock.now())
            self.assertTrue(home.is_dir())                                        # confirm's own folder is untouched
        with self.around_gpg(before=daily_run):
            out, code = self.confirm(ctx, PASS)
        self.assertIsNone(code, code)
        self.assertIn(f"Confirmed: the copy you keep off this PC opened {TODAY}", out)
        self.assertEqual(self.line(ctx, credentials), f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04")
        self.assertEqual(self.work_left(), [])

    def test_rt7b_each_call_has_its_own_work_folder_and_stale_ones_are_swept(self):
        """Two confirms run gpg in two different work folders of their own, inside vault\\backups. A make
        removes work folders a killed call left a day ago or more (with the old shared name too), never a fresh one."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        seen: list[Path] = []
        for _ in range(2):
            with self.around_gpg(before=lambda home, args: seen.append(home)):
                self.assertIsNone(self.confirm(ctx, PASS)[1])
        self.assertNotEqual(seen[0].parent, seen[1].parent)
        for home in seen:
            self.assertEqual(home.parent.parent, self.paths["backups"])
            self.assertTrue(home.parent.name.startswith(offsite.WORK + "-"), home)
        backups = self.paths["backups"]
        old = time.time() - offsite.STALE_SECONDS - 60
        for name in (offsite.WORK, offsite.WORK + "-killed"):
            (backups / name).mkdir()
            (backups / name / "archive.zip").write_bytes(b"synthetic")
            os.utime(backups / name, (old, old))
        (backups / (offsite.WORK + "-running")).mkdir()                           # fresh: another call's, in use
        self.make()
        self.assertEqual(self.work_left(), [offsite.WORK + "-running"])

    # D155 (Codex CR-2026-11-05-COMBINED Pass 3, F8; the owner's D155): gpg decrypts the bytes read once, from stdin

    def test_d155_codex_p3_a_file_replaced_during_gpg_and_restored_never_changes_what_it_decrypts(self):
        """Codex's Pass 3 race, in process: B (the current key's archive) is in the folder and kept A is typed. While gpg
        runs, a writer replaces every file it could read (the archive, and any staged copy named on its command line)
        with A's archive, then restores B before the hashes are taken again. gpg must have decrypted B's bytes, which A
        cannot open: "did not open" is recorded and status says so; never B confirmed."""
        a_bytes, b_bytes, ctx, credentials = self.two_generations()
        swapped: list[Path] = []

        def replace(home, args):
            targets = [self.dest / TODAY] + [Path(a) for a in args if not a.startswith("-") and Path(a).is_file()]
            for path in targets:
                path.write_bytes(a_bytes)
                swapped.append(path)

        def restore(home, args):
            for path in swapped:
                path.write_bytes(b_bytes)
        with self.around_gpg(before=replace, after=restore):
            out, code = self.confirm(ctx, PASS)
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} did not open with it"), code)
        self.assertEqual(self.line(ctx, credentials), f"!! offsite passphrase: the copy you keep off this PC did not open "
                                                      f"{TODAY} on 2026-11-04; run careeros offsite confirm")
        self.assertFalse(offsite.confirmed(self.paths["backups"])["ok"])
        self.assertNotEqual(self.gpg_run(["--decrypt", str(self.dest / TODAY)]).returncode, 0)   # kept A really can't

    def test_d155_confirm_and_the_drill_give_gpg_the_bytes_on_stdin_and_name_no_file(self):
        """Structural: every decrypt gets the archive's bytes after the passphrase line on its standard input, its
        arguments name no file, the passphrase is in no argument, and the work folder holds gpg's home only."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        archive = (self.dest / TODAY).read_bytes()
        calls: list = []
        with self.around_gpg(calls=calls):
            self.assertIsNone(self.confirm(ctx, PASS)[1])
            self.assertTrue(offsite.drill(self.dest, passphrase=PASS, gpg=self.gpg, area=self.paths["backups"])["ok"])
        self.assertEqual(len(calls), 2)
        for args, data, in_work in calls:
            self.assertEqual(args, ["--decrypt"])
            self.assertEqual(data, archive)
            self.assertEqual(in_work, ["gnupg"])
            self.assertFalse(any(PASS in a for a in args))

    def test_d155_the_drill_decrypts_the_bytes_it_read_whatever_the_file_holds_while_gpg_runs(self):
        """The weekly drill too: damaged bytes written over the archive while gpg runs, then the archive put back, do
        not reach gpg: the drill reads back what it read."""
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        self.run_offsite(self.dest, credentials)
        good = (self.dest / TODAY).read_bytes()
        damaged = bytearray(good)
        damaged[len(damaged) // 2] ^= 0xFF
        with self.around_gpg(before=lambda home, args: self.write(bytes(damaged))(), after=lambda home, args: self.write(good)()):
            result = offsite.drill(self.dest, passphrase=PASS, gpg=self.gpg, area=self.paths["backups"])
        self.assertTrue(result["ok"], result)

    def test_control_the_stable_correct_case_still_confirms_and_a_wrong_passphrase_still_fails(self):
        credentials = MemoryCredentials()
        credentials.write(offsite.CREDENTIAL, PASS)
        _, _, _, ctx = self.run_offsite(self.dest, credentials)
        out, code = self.confirm(ctx, "synthetic-test-passphrasf")
        self.assertTrue(str(code).startswith(f"Not confirmed: {TODAY} did not open with it"), code)
        self.assertFalse(offsite.confirmed(self.paths["backups"])["ok"])          # a plain failure is still recorded
        out, code = self.confirm(ctx, PASS)
        self.assertIsNone(code)
        self.assertIn(f"Confirmed: the copy you keep off this PC opened {TODAY}, and all 5 files read back whole", out)
        entry = offsite.confirmed(self.paths["backups"])
        self.assertEqual((entry["ok"], entry["key"], entry["sha256"]),
                         (True, credentials.written(offsite.CREDENTIAL), offsite._sha256(self.dest / TODAY)))
        self.assertEqual(self.line(ctx, credentials),
                         f"offsite passphrase: the copy you keep off this PC opened {TODAY} on 2026-11-04")
        self.assertEqual(self.work_left(), [])


MANUAL = Path(__file__).resolve().parents[1] / "docs" / "operator_manual.md"


class ManualTests(unittest.TestCase):
    @unittest.skipUnless(MANUAL.is_file(), "the private operator manual is not in the public export")
    def test_the_restore_writes_the_zip_with_output_and_the_setup_confirms_your_own_copy(self):
        """Pre-review #3 and #1 (D111): Windows PowerShell rewrites what a program prints as text, so `> restore.zip`
        gives a zip that does not open, while gpg's --output writes the bytes. The setup ends with your own copy."""
        text = MANUAL.read_text(encoding="utf-8")
        section = text[text.index("### Offsite archive"):text.index("Schema migrations (2026-11-03")]
        self.assertIn("`gpg --output restore.zip --decrypt careeros-offsite-<date>.zip.gpg`", section)
        self.assertNotRegex(section.replace("never `> restore.zip`", ""), r"> ?restore\.zip")
        self.assertIn("5. Run `careeros offsite confirm`", section)
        self.assertIn("`reports\\calibration`", section)


class RecordTests(unittest.TestCase):
    """The offsite record read back and the daily run's housekeeping (F2 of Codex CR-2026-11-03-PYTHON-314, found in the
    restore drill's and applied here). No gpg needed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = vault_paths(Path(self.tmp.name) / "vault")
        self.paths["backups"].mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def policy(self):
        overrides = self.paths["root"] / "policy" / "overrides.yaml"
        overrides.parent.mkdir(parents=True, exist_ok=True)
        folder = json.dumps((Path(self.tmp.name) / "offsite").as_posix())
        overrides.write_text(f"operations:\n  offsite:\n    folder: {folder}\n", encoding="utf-8")
        return load_policy(CONFIG_DIR / "policy" / "defaults.yaml", overrides)

    def test_a_damaged_record_is_never_trusted_and_status_says_so(self):
        good = {"ok": True, "archive": TODAY, "bytes": 10, "files": 5, "problems": [], "at": "2026-11-04T12:00:00.000Z"}
        for text in ('{"ok": true, "at": "not-a-timestamp"}', json.dumps({**good, "bytes": "10"}),
                     json.dumps({**good, "drill": {"ok": True, "at": "yesterday", "archive": TODAY, "problems": []}}),
                     json.dumps({"ok": False, "at": good["at"], "problems": "gpg"}), "[1]", "{not json"):
            with self.subTest(record=text):
                (self.paths["backups"] / offsite.RECORD).write_text(text, encoding="utf-8")
                self.assertEqual(offsite.last(self.paths["backups"]), {"ok": False, "unreadable": True})
                self.assertEqual(cli._offsite_line(self.policy(), self.paths, good["at"]),
                                 "!! offsite archive: its record (vault\\backups\\offsite.json) cannot be read; the next daily "
                                 "run writes a new one")
        (self.paths["backups"] / offsite.RECORD).write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(offsite.last(self.paths["backups"]), good)

    def test_the_offsite_archives_own_trouble_never_fails_the_run(self):
        store = open_store(self.paths["db"], FixedClock("2026-11-04T12:00:00.000Z"), "test-policy")
        try:
            ctx = cli.Context(store, self.policy(), None, None, None, None)
            with mock.patch.object(cli, "_offsite", side_effect=ValueError("bad record")), \
                    mock.patch.object(cli, "_weekly_drill", lambda *a: None), redirect_stdout(io.StringIO()) as out, \
                    self.assertLogs(cli.log, "ERROR"):
                cli._after_run(ctx, self.paths, "2026-11-04T00:00:00.000Z", notify=False)
            self.assertIn("offsite archive could not finish: ValueError: bad record", out.getvalue())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
