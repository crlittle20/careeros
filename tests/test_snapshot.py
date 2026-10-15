"""The public snapshot generator (release blocker B2, 2026-10-14): only allowlisted files leave, never the private ones,
and the fixed replacements are applied. See tools/build_snapshot.py and docs/public_release_checklist.md.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
