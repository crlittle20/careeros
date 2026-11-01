"""The public snapshot generator (release blocker B2, 2026-10-14): only allowlisted files leave, never the private ones,
and the fixed replacements are applied. See tools/build_snapshot.py and docs/public_release_checklist.md.
"""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from careeros.paths import ENGINE_ROOT

sys.path.insert(0, str(ENGINE_ROOT / "tools"))
import build_snapshot  # noqa: E402


def in_git_work_tree() -> bool:
    try:
        return subprocess.run(["git", "-C", str(ENGINE_ROOT), "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True).stdout.strip() == "true"
    except OSError:
        return False


class NeutralFolderTest(unittest.TestCase):
    def test_only_the_export_folder_changes(self):
        original = (ENGINE_ROOT / build_snapshot.MODEL).read_text(encoding="utf-8")
        changed = build_snapshot.neutral_folder(original)
        model = json.loads(changed)
        expression = next(e for e in model["model"]["expressions"] if e["name"] == "ExportFolder")["expression"]
        self.assertTrue(expression.startswith(f'"{build_snapshot.NEUTRAL_FOLDER}" meta'))
        self.assertEqual(changed.count("\n"), original.count("\n"))   # the rest of the file is untouched
        self.assertEqual(build_snapshot.neutral_folder(changed), changed)


class AllowlistCoverageTest(unittest.TestCase):
    def test_every_configuration_file_reaches_the_public_copy(self):
        """2026-10-18: the shadow scoring configuration was missing from the allowlist, so the public copy could not load
        its evaluator. Every file the code reads under config/ must be allowlisted (none of them is private: personal
        settings live in the vault)."""
        allowed = lambda name: any(name == a or name.startswith(a + "/") for a in build_snapshot.ALLOWLIST)
        names = sorted(p.relative_to(ENGINE_ROOT).as_posix() for p in (ENGINE_ROOT / "config").rglob("*") if p.is_file())
        self.assertTrue(names)
        self.assertEqual([n for n in names if not allowed(n)], [])


@unittest.skipUnless(in_git_work_tree(), "needs this repository's Git history (absent in a scratch copy)")
class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name) / "snapshot"
        cls.out.mkdir()
        cls.written = build_snapshot.export("HEAD", cls.out)
        build_snapshot.replace(cls.out, "example-user")

    def test_only_allowlisted_files_leave_and_never_the_private_ones(self):
        allowed = lambda name: any(name == a or name.startswith(a + "/") for a in build_snapshot.ALLOWLIST)
        self.assertTrue(self.written)
        self.assertEqual([n for n in self.written if not allowed(n)], [])
        files = {p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file()}
        for private in ("docs/operator_manual.md", "powerbi/CareerOS.pbix"):
            self.assertNotIn(private, files)
        self.assertFalse(any(f.startswith(("release/", "evidence/", ".venv")) for f in files))
        self.assertIn("README.md", files)
        self.assertIn(".github/workflows/ci.yml", files)

    def test_the_fixed_replacements_are_applied(self):
        self.assertEqual((self.out / "config" / "employers.yaml").read_text(encoding="utf-8"), build_snapshot.EMPLOYERS)
        own = (ENGINE_ROOT / "config" / "employers.yaml").read_text(encoding="utf-8")
        if own != build_snapshot.EMPLOYERS:   # the private repository: none of the employers it follows leaves
            real = {e["token"] for e in yaml.safe_load(own)["employers"]}
            public = {e["token"] for e in yaml.safe_load(build_snapshot.EMPLOYERS)["employers"]}
            self.assertEqual(public & real, set())
        self.assertIn("Copyright (c) 2026 example-user", (self.out / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn(json.dumps(build_snapshot.NEUTRAL_FOLDER)[1:-1], (self.out / build_snapshot.MODEL).read_text(encoding="utf-8"))
        for name in (build_snapshot.MODEL, "LICENSE", "config/employers.yaml"):   # Git's line endings, on any platform
            self.assertNotIn(b"\r\n", (self.out / name).read_bytes(), name)

    def test_it_refuses_a_folder_inside_the_repository_or_one_that_is_not_empty(self):
        with self.assertRaises(SystemExit):
            build_snapshot.main([str(ENGINE_ROOT / "snapshot-here"), "--owner", "x"])
        with self.assertRaises(SystemExit):
            build_snapshot.main([str(self.out), "--owner", "x"])

    def test_the_browser_extension_ships_and_only_the_operator_manual_stays_private(self):
        """2026-10-31: the owner decided to publish the form-filling extension with the code (allowlisted since
        2026-10-21, never in a published snapshot), so NEVER holds the operator manual alone."""
        self.assertEqual(build_snapshot.NEVER, ("docs/operator_manual.md",))
        self.assertTrue(build_snapshot.never("docs/operator_manual.md"))
        self.assertFalse(build_snapshot.never("extension/fill.js"))
        self.assertFalse(build_snapshot.never("tests/test_assist.py"))
        files = {p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file()}
        for shipped in ("extension/manifest.json", "extension/fill.js", "tests/test_extension.py",
                        "tests/test_extension_behaviour.py", "tests/extension_harness.cjs", "tests/test_assist.py",
                        "careeros/review/assist.py", "docs/assisted_applications.md"):
            self.assertIn(shipped, files)
        self.assertNotIn("docs/operator_manual.md", files)
        self.assertNotIn("docs/operator_manual.md", self.written)


@unittest.skipUnless(in_git_work_tree(), "needs this repository's Git history (absent in a scratch copy)")
class StatusGuardTest(unittest.TestCase):
    """2026-10-31: the 2026-10-29 publication's follow-up asked that the next snapshot refresh the README's
    `Project status (YYYY-MM-DD)` heading. The build now refuses a heading dated before the source commit."""

    def test_the_dates_are_read_from_the_source_commit(self):
        status, source = build_snapshot.status_dates("HEAD")
        for value in (status, source):
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(source, subprocess.run(["git", "-C", str(ENGINE_ROOT), "log", "-1", "--format=%cs", "HEAD"],
                                                capture_output=True, text=True, check=True).stdout.strip())
        committed = subprocess.run(["git", "-C", str(ENGINE_ROOT), "show", "HEAD:README.md"], capture_output=True, check=True).stdout.decode("utf-8")
        self.assertEqual(status, re.search(r"^### Project status \((\S+)\)", committed, re.M).group(1))

    def test_only_a_status_dated_before_the_commit_is_stale(self):
        self.assertTrue(build_snapshot.stale("2026-10-29", "2026-10-30"))
        self.assertFalse(build_snapshot.stale("2026-10-30", "2026-10-30"))
        self.assertFalse(build_snapshot.stale("2026-11-02", "2026-10-30"))

    def test_a_stale_status_is_refused_with_both_dates_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_snapshot, "status_dates", return_value=("2026-10-29", "2026-10-30")):
            out, err = Path(tmp) / "snapshot", io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
                build_snapshot.main([str(out), "--owner", "x"])
            self.assertIn("2026-10-29", err.getvalue())
            self.assertIn("2026-10-30", err.getvalue())
            self.assertIn("--allow-stale-status", err.getvalue())
            self.assertFalse(out.exists())

    def test_allow_stale_status_builds_and_the_build_ends_with_the_steps_left_by_hand(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_snapshot, "status_dates", return_value=("2026-10-29", "2026-10-30")), \
                mock.patch.object(build_snapshot, "RELEASE", Path(tmp) / "release"):   # the manifest stays out of release/
            out, log = Path(tmp) / "snapshot", io.StringIO()
            with contextlib.redirect_stdout(log):
                self.assertEqual(build_snapshot.main([str(out), "--owner", "example-user", "--allow-stale-status"]), 0)
            self.assertTrue((out / "README.md").exists())
            self.assertTrue((out / "extension" / "manifest.json").exists())
            self.assertFalse((out / "docs" / "operator_manual.md").exists())
            self.assertEqual(len(list((Path(tmp) / "release").glob("snapshot-*.json"))), 1)
            self.assertEqual(list(out.rglob("snapshot-*.json")), [])
            printed = log.getvalue()
            self.assertIn("next, by hand", printed)
            for phrase in ("regenerate evidence/", "tools/mvp_gate.py, then tools/mutate.py",
                           "project status is dated 2026-10-29 (source commit 2026-10-30)", "refresh the heading"):
                self.assertIn(phrase, printed)
            self.assertNotIn("extension", printed)


if __name__ == "__main__":
    unittest.main()
