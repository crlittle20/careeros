"""CI and the local validation command (Portfolio Prompt 10, 2026-10-13): the workflow needs no secret and can only
read, its actions are pinned, and tools/validate.py and tools/offline_tests.py do what they claim.
"""
import os
import re
import socket
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import yaml

from careeros.paths import ENGINE_ROOT

sys.path.insert(0, str(ENGINE_ROOT / "tools"))
import offline_tests  # noqa: E402
import validate  # noqa: E402

WORKFLOW = ENGINE_ROOT / ".github" / "workflows" / "ci.yml"


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.job = cls.workflow["jobs"]["validate"]
        cls.steps = cls.job["steps"]

    def test_no_secret_or_credential_is_used(self):
        self.assertNotIn("secrets.", self.text)
        for step in self.steps:
            for name in (step.get("env") or {}):
                self.assertNotRegex(name, r"(?i)key|token|secret|password|credential", name)

    def test_the_job_token_can_only_read_and_is_not_kept(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        checkout = next(s for s in self.steps if s.get("uses", "").startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_actions_are_pinned_to_full_commits(self):
        used = [s["uses"] for s in self.steps if "uses" in s]
        self.assertTrue(used)
        for uses in used:
            self.assertRegex(uses, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_only_pushes_pull_requests_and_manual_runs_start_it(self):
        triggers = self.workflow.get("on", self.workflow.get(True))   # YAML 1.1 reads a bare `on` as True
        self.assertEqual(set(triggers), {"push", "pull_request", "workflow_dispatch"})

    def test_it_installs_the_hash_locked_tools_and_runs_validate(self):
        runs = " ".join(s.get("run", "") for s in self.steps)
        for part in ("--require-hashes", "--only-binary :all:", "--no-deps", "-r requirements-dev.lock", "python tools/validate.py"):
            self.assertIn(part, runs)
        self.assertNotIn("--offline", runs)   # CI also runs pip-audit

    def test_windows_is_required_and_linux_informational(self):
        matrix = {entry["os"]: entry["experimental"] for entry in self.job["strategy"]["matrix"]["include"]}
        self.assertEqual(matrix, {"windows-latest": False, "ubuntu-latest": True})
        self.assertIn("matrix.experimental", self.job["continue-on-error"])


class OfflineGuardTest(unittest.TestCase):
    def test_only_loopback_can_be_resolved_or_reached(self):
        before = socket.getaddrinfo
        remove = offline_tests.install()
        try:
            with self.assertRaises(offline_tests.NetworkRefused):
                socket.getaddrinfo("boards-api.greenhouse.io", 443)
            with self.assertRaises(offline_tests.NetworkRefused):
                socket.gethostbyname("api.lever.co")
            with self.assertRaises(offline_tests.NetworkRefused):
                socket.create_connection(("192.0.2.10", 443), timeout=1)
            with socket.socket() as raw, self.assertRaises(offline_tests.NetworkRefused):
                raw.connect(("192.0.2.10", 443))
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen()
                with socket.create_connection(server.getsockname(), timeout=2):
                    pass                                      # the review page's loopback server stays reachable
            self.assertTrue(socket.getaddrinfo("localhost", 80))
        finally:
            remove()
        self.assertIs(socket.getaddrinfo, before)

    def test_loopback_is_recognised(self):
        for host in ("127.0.0.1", "::1", "localhost", None, b"127.0.0.1"):
            self.assertTrue(offline_tests.is_loopback(host), host)
        for host in ("10.0.0.1", "192.0.2.10", "example.org", "0.0.0.0"):  # noqa: S104 - an address checked, never bound
            self.assertFalse(offline_tests.is_loopback(host), host)


class ValidateTest(unittest.TestCase):
    def test_type_errors_ignore_line_numbers_and_notes(self):
        output = ("careeros\\a.py:10: error: Bad thing  [misc]\n"
                  "careeros/a.py:12:5: error: Bad thing  [misc]\n"
                  "careeros/a.py:3: note: not an error\n")
        self.assertEqual(validate.type_errors(output), Counter({"careeros/a.py: Bad thing  [misc]": 2}))

    def test_a_new_type_error_fails_a_moved_one_does_not_and_fixed_ones_are_counted(self):
        baseline = validate.read_baseline("# header\nx.py: A  [a]\nx.py: A  [a]\ny.py: B  [b]\n")
        new, fixed = validate.compare_types(baseline, Counter({"x.py: A  [a]": 1, "z.py: C  [c]": 1}))
        self.assertEqual((new, fixed), (["z.py: C  [c]"], 2))
        self.assertEqual(validate.compare_types(baseline, baseline), ([], 0))
        text = validate.baseline_text(Counter({"x.py: A  [a]": 2}))
        self.assertEqual(validate.read_baseline(text), Counter({"x.py: A  [a]": 2}))

    def test_only_engine_code_carries_known_type_errors(self):
        for line in validate.read_baseline(validate.BASELINE.read_text(encoding="utf-8")):
            self.assertRegex(line, r"^careeros/[\w/]+\.py: .+  \[[a-z-]+\]$")   # the tools and tests must type-check cleanly

    def test_a_lower_mvp_grade_fails(self):
        committed = {"a": "PASS", "b": "PARTIAL", "c": "PASS"}
        self.assertEqual(validate.grade_drops(committed, {"a": "PARTIAL", "b": "PASS", "c": "PASS"}), ["a: PASS -> PARTIAL"])
        self.assertEqual(validate.grade_drops(committed, {"a": "PASS", "b": "FAIL"}), ["b: PARTIAL -> FAIL", "c: PASS -> missing"])
        self.assertEqual(validate.grade_drops(committed, committed), [])

    def test_checks_run_apart_from_private_data_and_credentials(self):
        planted = {name: "planted" for name in validate.REMOVED_ENV} | {"CAREEROS_VAULT": str(ENGINE_ROOT.parent / "vault")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, planted):
            env = validate.isolated_env(Path(tmp))
            self.assertEqual([name for name in validate.REMOVED_ENV if name in env], [])
            self.assertTrue(Path(env["CAREEROS_VAULT"]).is_relative_to(tmp))
            self.assertFalse(Path(env["CAREEROS_VAULT"]).exists())

    def test_every_check_is_documented_and_runnable(self):
        self.assertEqual(set(validate.STEPS), set(validate.CHECKS))
        doc = validate.__doc__ or ""
        for name in validate.STEPS:
            self.assertRegex(doc, rf"(?m)^  {name}\s", name)
        self.assertTrue(re.search(r"--full", doc))



class MutationToolTest(unittest.TestCase):
    def test_scratch_copies_keep_what_the_tests_read(self):
        """Until 2026-10-14 the copies left out evidence/, which the documentation tests need, so every planted defect
        looked caught. The copies keep it now, and mutate.py refuses to run unless an unmutated copy passes."""
        import mutate
        self.assertNotIn("evidence", mutate.COPY_IGNORE)
        self.assertEqual(set(mutate.COPY_IGNORE), {".venv*", "__pycache__", ".git"})
        self.assertTrue(callable(mutate.copy_passes))

if __name__ == "__main__":
    unittest.main()
