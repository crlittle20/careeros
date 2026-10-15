"""Public-release scan (Portfolio Prompt 9, 2026-10-13): tools/release_scan.py finds what must not be published and
never prints it.

Values are planted in a throwaway git repository, built at run time so that no secret-shaped text sits in this file.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from careeros.paths import ENGINE_ROOT

sys.path.insert(0, str(ENGINE_ROOT / "tools"))
import release_scan  # noqa: E402

PLANTED = {
    "Anthropic API key": "sk-ant-" + "Q" * 24,
    "GitHub token": "ghp_" + "R" * 30,
    "AWS access key": "AKIA" + "S" * 16,
    "Google OAuth client secret": "GOCSPX-" + "T" * 20,
    "Private key": "-----BEGIN " + "PRIVATE KEY-----",
    "Email address": "jane.private" + "@" + "realmail.org",
    "Phone number": "(303) 867-" + "5309",
    "Machine path with a user name": "C:\\Users\\" + "janedoe" + "\\Documents",
}
PRIVATE_TERM = "Acme " + "Hidden Holdings"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                   check=True, capture_output=True)


class ReleaseScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(tempfile.mkdtemp(prefix="careeros-release-scan-"))
        cls.addClassCleanup(shutil.rmtree, cls.repo, True)
        git(cls.repo, "init", "-q")
        (cls.repo / "app.py").write_text("\n".join(f"value_{n} = '{v}'" for n, v in enumerate(PLANTED.values())) + "\n", encoding="utf-8")
        (cls.repo / "notes.md").write_text(f"We follow {PRIVATE_TERM} closely.\n", encoding="utf-8")
        (cls.repo / "placeholder.py").write_text(f"password = '{'abcdefghijklmnop'}'  # release-scan: fake\n", encoding="utf-8")
        (cls.repo / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (cls.repo / "fiction.md").write_text("Call (303) 555-0100 or write to someone@example.com.\n", encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "first")
        (cls.repo / "old.txt").write_text("token " + "ghp_" + "U" * 30 + "\n", encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "second")
        (cls.repo / "old.txt").unlink()                      # gone from the tree, still in history
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "third")
        cls.deny = cls.repo.parent / f"{cls.repo.name}-denylist.txt"
        cls.deny.write_text(f"# private\n{PRIVATE_TERM}\n", encoding="utf-8")
        cls.addClassCleanup(cls.deny.unlink)
        cls.result = release_scan.scan(cls.repo, release_scan.load_denylist(cls.deny), history=True)

    def test_every_planted_kind_is_found_in_its_file(self):
        findings = self.result["findings"]
        for kind in PLANTED:
            self.assertIn("app.py", findings.get(kind, {}), kind)
        self.assertIn("notes.md", findings["Denylisted term #1"])
        self.assertIn("shot.png", findings["Image or screenshot"])

    def test_history_only_secrets_and_commit_identities_are_found(self):
        self.assertTrue(any(f.startswith("history: old.txt") for f in self.result["findings"]["GitHub token"]))
        self.assertEqual(self.result["commit_metadata"], {"Name in commit author or committer": 3})   # example.com is not personal

    def test_placeholders_and_fiction_are_not_findings(self):
        files = {f for kind in self.result["findings"].values() for f in kind}
        self.assertNotIn("placeholder.py", files)
        self.assertNotIn("fiction.md", files)

    def test_no_value_is_ever_printed(self):
        out = subprocess.run([sys.executable, str(ENGINE_ROOT / "tools" / "release_scan.py"), "--root", str(self.repo), "--history",
                              "--denylist", str(self.deny)], capture_output=True, text=True)
        js = subprocess.run([sys.executable, str(ENGINE_ROOT / "tools" / "release_scan.py"), "--root", str(self.repo), "--history",
                             "--denylist", str(self.deny), "--json"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)                  # a secret-type finding blocks a release
        json.loads(js.stdout)
        for text in (out.stdout + out.stderr, js.stdout):
            for value in [*PLANTED.values(), PRIVATE_TERM, "ghp_" + "U" * 30, "janedoe", "realmail"]:
                self.assertNotIn(value, text)
        self.assertIn("Denylisted term #1", out.stdout)

    def test_a_folder_outside_git_is_walked_in_full(self):
        """A scratch copy (tools/mutate.py) has no Git metadata: the scan walks it instead of failing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "settings.py").write_text(f"key = '{PLANTED['GitHub token']}'\n", encoding="utf-8")
            (root / ".venv-x").mkdir()
            (root / ".venv-x" / "ignored.py").write_text(f"key = '{PLANTED['AWS access key']}'\n", encoding="utf-8")
            result = release_scan.scan(root, [], history=False)
        self.assertEqual(result["findings"].get("GitHub token"), {"pkg/settings.py": [1]})
        self.assertNotIn("AWS access key", result["findings"])   # virtual environments are never part of a release

    def test_this_repository_holds_no_secret(self):
        result = release_scan.scan(ENGINE_ROOT, [], history=False)
        self.assertEqual({k: v for k, v in result["findings"].items() if k in release_scan.SECRET_TYPES}, {})


if __name__ == "__main__":
    unittest.main()
