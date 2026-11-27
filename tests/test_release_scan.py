"""Public-release scan (Portfolio Prompt 9, 2026-10-13): tools/release_scan.py finds what must not be published and
never prints it.

Values are planted in a throwaway git repository, built at run time so that no secret-shaped text sits in this file.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from careeros.core.scratch import remove_tree
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


# what Git is given of the environment (Codex's Pass 16 run: Git returned 128 at `git init` once more, its message lost):
# only what it needs to run, and the fixture's own settings (own_git_settings), so no GIT_* variable another test left
# behind can reach it
GIT_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP")


def git(root: Path, *args: str) -> None:
    """Run Git in the throwaway repository; a failure raises with the command and Git's own message."""
    env = {name: os.environ[name] for name in (*GIT_ENV, "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM") if name in os.environ}
    done = subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {root} returned {done.returncode}: "
                           f"{(done.stderr or done.stdout).strip() or 'no message'}")


# the throwaway repository's Git settings, whatever this machine's own are (Codex's Pass 15 run: Git returned 128
# building it, and the class passed when run alone): no system or global configuration is read while it is built and
# scanned, only this, so a default branch, a signing rule, a hooks folder or an ownership check set elsewhere never
# reaches it
OWN_SETTINGS = "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n[safe]\n\tdirectory = *\n"


@contextlib.contextmanager
def own_git_settings(config: Path):
    """While the fixture is built and scanned: Git reads only `config`, and no other GIT_* variable is set (the scan
    runs Git itself, with this process's environment); every one is restored afterwards."""
    config.write_text(OWN_SETTINGS, encoding="utf-8")
    saved = {name: value for name, value in os.environ.items() if name.startswith("GIT_")}
    for name in saved:
        del os.environ[name]
    os.environ.update({"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
    try:
        yield
    finally:
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        os.environ.update(saved)


def scanner_env(config: Path) -> dict:
    """The environment of a scanner run as its own process (tools/release_scan.py, which runs Git itself): this
    process's, with no GIT_* variable but the fixture's own settings, as own_git_settings gives the scan run in this
    process (Codex's Pass 17 run: a GIT_CONFIG_COUNT set outside the test, its GIT_CONFIG_VALUE_0 missing, made the
    scanner's `git rev-list --all` return 128)."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return env | {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}


class ReleaseScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(tempfile.mkdtemp(prefix="careeros-release-scan-"))
        cls.addClassCleanup(remove_tree, cls.repo)   # git's read-only objects too (item 10, 2026-11-02)
        cls.config = config = cls.repo.parent / f"{cls.repo.name}-gitconfig"
        cls.addClassCleanup(config.unlink, missing_ok=True)
        with own_git_settings(config):
            cls._build()

    @classmethod
    def _build(cls):
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

    def test_a_starred_term_is_found_with_letters_attached_and_others_stay_whole_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deny.txt"
            path.write_text(f"{PRIVATE_TERM}*\nPlainword\n", encoding="utf-8")
            deny = release_scan.load_denylist(path)
        self.assertEqual(release_scan.scan_text(f"the {PRIVATE_TERM}Rx unit\n", deny), [("Denylisted term #1", 1)])
        self.assertEqual(release_scan.scan_text(f"sub{PRIVATE_TERM.lower()}\n", deny), [])   # inside a longer word: another word
        self.assertEqual(release_scan.scan_text("Plainword here\n", deny), [("Denylisted term #2", 1)])
        self.assertEqual(release_scan.scan_text("Plainwordly\n", deny), [])

    def test_placeholders_and_fiction_are_not_findings(self):
        files = {f for kind in self.result["findings"].values() for f in kind}
        self.assertNotIn("placeholder.py", files)
        self.assertNotIn("fiction.md", files)

    def test_no_value_is_ever_printed(self):
        env = scanner_env(self.config)   # the fixture's own Git settings reach the scanner's Git too
        out = subprocess.run([sys.executable, str(ENGINE_ROOT / "tools" / "release_scan.py"), "--root", str(self.repo), "--history",
                              "--denylist", str(self.deny)], capture_output=True, text=True, env=env)
        js = subprocess.run([sys.executable, str(ENGINE_ROOT / "tools" / "release_scan.py"), "--root", str(self.repo), "--history",
                             "--denylist", str(self.deny), "--json"], capture_output=True, text=True, env=env)
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


class GitFixtureTests(unittest.TestCase):
    """The throwaway repository's Git (Codex Passes 16 and 17, not blockers): a failure says what Git said, and no GIT_*
    variable set outside the fixture reaches Git while it is built and scanned, in this process or the scanner's."""

    def test_a_git_setting_left_outside_reaches_no_git_of_the_fixture(self):
        """Codex's Pass 17 environment: GIT_CONFIG_COUNT=1 and GIT_CONFIG_KEY_0 set outside the test, no
        GIT_CONFIG_VALUE_0 (Git refuses every command then). All nine tests of this module pass: the seven of
        ReleaseScanTest, whose scanner runs as its own process too, and this class's other two."""
        from unittest import mock
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite([loader.loadTestsFromTestCase(ReleaseScanTest),
                                    GitFixtureTests("test_a_failing_git_command_says_what_git_said"),
                                    GitFixtureTests("test_no_other_git_variable_reaches_the_fixture")])
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare"}):
            os.environ.pop("GIT_CONFIG_VALUE_0", None)
            refused = subprocess.run(["git", "config", "--list"], capture_output=True, text=True)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertNotEqual(refused.returncode, 0)   # the setting does break Git here
        self.assertEqual(result.testsRun, 9)
        self.assertEqual([str(test) for test, _ in result.failures + result.errors], [])

    def test_a_failing_git_command_says_what_git_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as failed:
                git(Path(tmp) / "missing", "status")
        self.assertIn("git status in", str(failed.exception))
        self.assertIn("fatal:", str(failed.exception))   # Git's own message, kept

    def test_no_other_git_variable_reaches_the_fixture(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            stray = {"GIT_INDEX_FILE": str(Path(tmp) / "no-such-folder" / "index"),
                     "GIT_CONFIG_PARAMETERS": "'core.bare'='true'"}
            try:
                with mock.patch.dict(os.environ, stray):
                    with own_git_settings(Path(tmp) / "gitconfig"):
                        git(repo, "init", "-q")
                        (repo / "a.txt").write_text("a\n", encoding="utf-8")
                        git(repo, "add", "-A")
                        git(repo, "commit", "-q", "-m", "one")
                        result = release_scan.scan(repo, [], history=True)   # the scan's own Git too
                    for name, value in stray.items():   # restored afterwards
                        self.assertEqual(os.environ[name], value)
            finally:
                remove_tree(repo)   # git's read-only objects too
        self.assertEqual(result["commit_metadata"], {"Name in commit author or committer": 1})


if __name__ == "__main__":
    unittest.main()
