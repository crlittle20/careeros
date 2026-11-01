"""CI and the local validation command (Portfolio Prompt 10, 2026-10-13): the workflow needs no secret and can only
read, its actions are pinned, and tools/validate.py and tools/offline_tests.py do what they claim.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
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

    def test_extension_behaviour_tests_not_run_are_said_so_and_fail_a_full_run(self):
        """Second Claude review of the Codex corrections (2026-10-22): skipped is not passed."""
        import argparse
        import subprocess
        done = subprocess.CompletedProcess([], 0, "", "Ran 3 tests in 0.1s\n\nOK (skipped=1)\n")
        with mock.patch.object(validate, "run", return_value=done), mock.patch.object(validate.shutil, "which", return_value=None):
            quick = validate.check_tests({"PATH": ""}, Path("."), argparse.Namespace(full=False))
            full = validate.check_tests({"PATH": ""}, Path("."), argparse.Namespace(full=True))
            with_node = validate.check_tests({"PATH": "", "CAREEROS_NODE": "node.exe"}, Path("."), argparse.Namespace(full=True))
        self.assertTrue(quick.ok)
        self.assertIn("extension behaviour tests NOT RUN", quick.detail)
        self.assertFalse(full.ok)
        self.assertTrue(with_node.ok)
        self.assertIn("extension behaviour run in Node", with_node.detail)



class MutationToolTest(unittest.TestCase):
    def test_scratch_copies_keep_what_the_tests_read(self):
        """Until 2026-10-14 the copies left out evidence/, which the documentation tests need, so every planted defect
        looked caught. The copies keep it now, and mutate.py refuses to run unless an unmutated copy passes."""
        import mutate
        self.assertNotIn("evidence", mutate.COPY_IGNORE)
        self.assertEqual(set(mutate.COPY_IGNORE), {".venv*", "__pycache__", ".git", ".mypy_cache", ".ruff_cache",
                                                     ".pytest_cache", ".vscode", ".idea", "Thumbs.db", "desktop.ini", ".DS_Store"})
        self.assertTrue(callable(mutate.copy_passes))

    def test_only_a_real_test_failure_counts_as_caught(self):
        """2026-10-17: an import failure, a shorter suite, a timeout or an unreadable run is never proof of a catch."""
        import mutate
        caught = {"exit": 1, "timed_out": False, "ran": 10, "failures": 1, "errors": 0, "load_errors": 0, "failing": ["t.T.test_a"]}
        self.assertEqual(mutate.classify(caught, 10), "CAUGHT")
        self.assertEqual(mutate.classify({**caught, "exit": 0, "failures": 0, "failing": []}, 10), "SURVIVED")
        self.assertEqual(mutate.classify({**caught, "load_errors": 1}, 10), "LOAD_ERROR")
        self.assertEqual(mutate.classify({**caught, "ran": 9}, 10), "LOAD_ERROR")
        self.assertEqual(mutate.classify({"exit": None, "timed_out": True}, 10), "TIMEOUT")
        self.assertEqual(mutate.classify({"exit": 1, "timed_out": False, "ran": None}, 10), "INFRA_ERROR")

    def test_the_suite_summary_is_read_from_unittest_itself(self):
        import mutate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_a.py").write_text(
                "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_ok(self):\n        pass\n\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n\n    def test_errors(self):\n        raise KeyError('x')\n\n"
                "    @unittest.skip('later')\n    def test_skipped(self):\n        pass\n", encoding="utf-8")
            (root / "tests" / "test_b.py").write_text("import a_module_that_does_not_exist\n", encoding="utf-8")
            out = mutate.suite(root, 120)
        self.assertEqual((out["ran"], out["failures"], out["errors"], out["skipped"], out["load_errors"]), (5, 1, 2, 1, 1))
        self.assertEqual(sorted(out["failing"]), ["tests.test_a.A.test_errors", "tests.test_a.A.test_fails"])
        self.assertEqual(mutate.classify(out, 5), "LOAD_ERROR")      # one module never loaded: not proof, even with failures

    def test_a_stopped_run_carries_over_only_clean_catches_of_the_same_defect(self):
        """2026-10-23: two six-hour runs were lost to a laptop that slept. A stopped run resumes, carrying over only what
        a sleep or a change can't have spoiled."""
        import mutate
        key = {"tree": "t" * 64, "env": "e" * 64}
        labels = [mutation[3] for mutation in mutate.MUTATIONS[:12]]

        def line(index, label=None, entry_key=key, **result):
            return json.dumps({"index": index, **entry_key, "result": {"mutation": label or labels[index], **result}}).encode()
        caught = {"result": "CAUGHT", "seconds": 120.0}
        lines = [line(0, **caught),                                                    # kept
                 line(1, **caught, slept=3600.0),                                      # slept through
                 line(2, result="SURVIVED", seconds=120.0),                            # not caught
                 line(3, "a defect since rewritten", **caught),
                 line(4, result="CAUGHT", seconds=365.1),                              # slower than the limit
                 line(5, **caught), line(5, **caught, slept=60.0),                     # the last line decides
                 line(6, result="CAUGHT", seconds=365.0),                              # exactly the limit: kept
                 line(7, result="CAUGHT", seconds=0), line(8, result="CAUGHT"),        # no time: never carried over
                 line(9, entry_key={"tree": "x" * 64, "env": "e" * 64}, **caught),     # another tree
                 line(10, entry_key={"tree": "t" * 64, "env": "x" * 64}, **caught),    # another environment
                 json.dumps({"index": 11, "result": {"mutation": labels[11], **caught}}).encode(),   # no tree at all
                 b"\x80\x81 not a line", line(99999, labels[0], **caught)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.jsonl"
            path.write_bytes(b"\n".join(lines) + b'\n{"index": 6, "res')
            kept = mutate.carried_over(path, 365.0, key)
            self.assertEqual(mutate.carried_over(Path(tmp) / "none.jsonl", 365.0, key), {})
        self.assertEqual(sorted(kept), [0, 6])
        self.assertTrue(kept[0]["carried_over"])

    def test_results_carry_over_only_to_exactly_the_same_tree_in_the_same_environment(self):
        import mutate
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, "ROOT", Path(tmp)):
            root = Path(tmp)
            (root / "evidence").mkdir()
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            first = mutate.tree_digest()
            self.assertEqual((mutate.tree_digest(root), mutate.tree_digest(files=mutate.tree_files())), (first, first))
            (root / "evidence" / "mutation-receipt.json").write_text("{}", encoding="utf-8")   # what the run writes
            for ignored in (".venv-dev", "__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".vscode"):   # never copied
                (root / ignored).mkdir()
                (root / ignored / "x.py").write_text("y", encoding="utf-8")
            (root / "Thumbs.db").write_bytes(b"\0")
            self.assertEqual(mutate.tree_digest(), first)
            before = mutate.tree_files()
            (root / "evidence" / "functional_mvp.json").write_text("{}", encoding="utf-8")     # a fixture tests read
            second = mutate.tree_digest()
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            third = mutate.tree_digest()
            (root / "a.py").rename(root / "b.py")                                              # same bytes, new name
            self.assertEqual(len({first, second, third, mutate.tree_digest()}), 4)
            self.assertEqual(mutate.tree_changes(before, mutate.tree_files()),
                             ["added b.py", "added evidence/functional_mvp.json", "removed a.py"])
        key = mutate.run_key("a" * 64, mutate.environment())
        self.assertEqual(set(key), {"tree", "env"})            # hashes only: no local path reaches a progress line
        self.assertNotEqual(mutate.progress_path(mutate.run_key("b" * 64, mutate.environment())), mutate.progress_path(key))

    def test_the_environment_covers_python_its_packages_and_node(self):
        import mutate
        dists = [SimpleNamespace(metadata={"Name": "x"}, version="1")]
        with mock.patch.dict(os.environ, {"CAREEROS_NODE": "node-a"}), \
                mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="v1\n")) as node, \
                mock.patch("importlib.metadata.distributions", return_value=dists):
            first = mutate.environment()
            node.return_value = SimpleNamespace(stdout="v2\n")
            newer_node = mutate.environment()
            node.return_value = SimpleNamespace(stdout="v1\n")
            dists[0] = SimpleNamespace(metadata={"Name": "x"}, version="2")
            newer_package = mutate.environment()
        self.assertEqual(set(first), {"python", "executable", "platform", "packages", "node", "node_version"})
        self.assertEqual((first["node_version"], newer_node["node_version"]), ("v1", "v2"))
        keys = {mutate.run_key("a" * 64, env)["env"] for env in (first, newer_node, newer_package, {**first, "python": "x"})}
        self.assertEqual(len(keys), 4)
        for env in (first, newer_node, newer_package):
            self.assertNotEqual(mutate.progress_path(mutate.run_key("a" * 64, env)),
                                mutate.progress_path(mutate.run_key("a" * 64, {**env, "python": "x"})))

    @unittest.skipUnless(sys.platform == "win32", "Windows paths ignore case")
    def test_the_same_node_spelled_differently_is_the_same_environment(self):
        import mutate
        with mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="v1\n")):
            with mock.patch.dict(os.environ, {"CAREEROS_NODE": r"C:\Tools\Node\node.exe"}):
                upper = mutate.environment()
            with mock.patch.dict(os.environ, {"CAREEROS_NODE": r"c:\tools\node\NODE.EXE"}):
                lower = mutate.environment()
        self.assertEqual(mutate.run_key("a" * 64, upper), mutate.run_key("a" * 64, lower))

    def test_a_line_cut_short_by_a_power_loss_costs_nothing_after_it(self):
        import mutate
        key = {"tree": "t" * 64, "env": "e" * 64}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.jsonl"
            path.write_bytes(b'{"index": 1, "res')                                             # cut short, no newline
            mutate.append_progress(path, {"index": 0, **key, "result": {"mutation": mutate.MUTATIONS[0][3], "result": "CAUGHT",
                                                                          "seconds": 100.0}})
            self.assertEqual(sorted(mutate.carried_over(path, 365.0, key)), [0])
            with mock.patch("builtins.print") as said:
                mutate.append_progress(Path(tmp), {"index": 0})                               # a folder: never written
            self.assertIn("runs again if the run is resumed", said.call_args[0][0])

    def test_a_sleep_is_read_around_each_defect_and_kept_with_its_result(self):
        import threading

        import mutate
        env = {"python": "3"}
        key = mutate.run_key("t" * 64, env)
        job = (0, "a.py", "x", "x", "y", mutate.MUTATIONS[0][3])
        caught = {"exit": 1, "timed_out": False, "ran": 5, "failures": 1, "errors": 0, "skipped": 0, "load_errors": 0,
                  "failing": ["t.T.test_a"], "seconds": 100.0, "tree": key["tree"]}
        for asleep, marked in ((6.0, True), (5.0, False)):
            reads = []

            def one(*_args, reads=reads):
                self.assertEqual(len(reads), 1)                                               # read before the defect ran
                return caught

            def clock(asleep=asleep, reads=reads):
                reads.append(None)
                return 1000.0 + (asleep if len(reads) > 1 else 0.0)
            with tempfile.TemporaryDirectory() as tmp, mock.patch("tempfile.tempdir", tmp), mock.patch("builtins.print"):
                progress = Path(tmp) / "progress.jsonl"
                index, record = mutate.run_one(job, 5, key, progress, threading.Lock(), one=one, clock=clock, env=lambda: env)
                line = json.loads(progress.read_text(encoding="utf-8"))
            self.assertEqual((index, record["result"], "slept" in record, "slept" in line["result"]), (0, "CAUGHT", marked, marked))
            self.assertEqual((line["tree"], line["env"], line["result"]["at"]), (key["tree"], key["env"], record["at"]))

    def test_a_result_counts_only_for_a_copy_of_the_tree_the_run_started_on(self):
        """Review of 6259a92: a change to the folder during a run was filed under the tree hashed at the start, and a
        later run on that tree carried it over. Each scratch copy is hashed now; a mismatch stops the run unrecorded."""
        import threading

        import mutate
        env = {"python": "3"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, "ROOT", Path(tmp) / "engine"), \
                mock.patch("tempfile.tempdir", tmp), mock.patch("builtins.print"):
            root = mutate.ROOT
            (root / "tests").mkdir(parents=True)
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_a.py").write_text(
                "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_add(self):\n        from calc import add\n"
                "        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            tree = mutate.tree_digest()
            key = mutate.run_key(tree, env)
            source = (root / "calc.py").read_text(encoding="utf-8")
            outcome = mutate._one("calc.py", source, "a + b", "a - b")
            self.assertEqual((outcome["tree"], mutate.classify(outcome, 1)), (tree, "CAUGHT"))
            stale = mutate._one("calc.py", source.replace("a + b", "a + b  # an older text"), "a + b", "a - b")
            self.assertNotEqual(stale["tree"], tree)                  # planted into text the copy's hash never covered
            with mock.patch.object(mutate, "suite") as suite:         # a copy known to be changed never runs its suite
                self.assertIsNone(mutate._one("calc.py", source, "a + b", "a - b", "0" * 64)["ran"])
            suite.assert_not_called()
            progress = Path(tmp) / "progress.jsonl"
            job = (0, "calc.py", source, "a + b", "a - b", mutate.MUTATIONS[0][3])
            self.assertEqual(mutate.run_one(job, 1, key, progress, threading.Lock(), env=lambda: env)[1]["result"], "CAUGHT")
            copy = mutate._copy

            def copy_with_a_visitor(dest):                            # in the copy, gone from the folder by the hash
                visitor = root / "tests" / "test_visitor.py"
                visitor.write_text("# here only while the copy is taken\n", encoding="utf-8")
                try:
                    copy(dest)
                finally:
                    visitor.unlink()
            halt = threading.Event()
            with mock.patch.object(mutate, "_copy", copy_with_a_visitor), self.assertRaises(mutate.TreeChanged):
                mutate.run_one(job, 1, key, progress, threading.Lock(), env=lambda: env, halt=halt)
            self.assertTrue(halt.is_set())                            # no other defect starts after it
            with self.assertRaises(mutate.Halted):
                mutate.run_one(job, 1, key, progress, threading.Lock(), env=lambda: env, halt=halt,
                               one=lambda *_: self.fail("a halted run started a defect"))
            with self.assertRaises(mutate.EnvironmentChanged):
                mutate.run_one(job, 1, key, progress, threading.Lock(), env=lambda: {"python": "4"})
            self.assertEqual(len(progress.read_text(encoding="utf-8").splitlines()), 1)      # the first result only
            broken = {"exit": None, "timed_out": False, "ran": None, "infrastructure": "OSError: no space left"}
            self.assertEqual(mutate.run_one(job, 1, key, progress, threading.Lock(), one=lambda *_: broken,
                                            env=lambda: env)[1]["result"], "INFRA_ERROR")

    def test_time_asleep_is_read_from_the_system_counters(self):
        import ctypes
        import time

        import mutate

        class Kernel:
            def __init__(self):
                self.GetTickCount64 = lambda: 10_000_000                                       # ms since boot, asleep or not

            def QueryUnbiasedInterruptTime(self, ref):
                ref._obj.value = 40_000_000_000                                                # 100 ns units awake: 4,000 s
                return 1
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=Kernel()), create=True):
            self.assertEqual(mutate.asleep_seconds(), 6000.0)
        clocks = {7: 500.0, 1: 380.0}
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(time, "CLOCK_MONOTONIC", 1, create=True), \
                mock.patch.object(time, "clock_gettime", clocks.__getitem__, create=True):
            self.assertEqual(mutate.asleep_seconds(), 120.0)

    def test_a_defect_the_machine_slept_through_runs_again_and_is_never_caught_while_asleep(self):
        import mutate
        calls = []

        def run(job, asleep_on=(0, 1, 1, 1, 1)):
            calls.append(job[0])
            if job[0] == 3:
                return 3, {"mutation": "x", "result": "SURVIVED"}                              # never slept: runs once
            slept = calls.count(job[0]) - 1 < asleep_on.count(job[0])   # index 0 sleeps once, index 1 every time
            return job[0], {"mutation": "x", "result": "CAUGHT", **({"slept": 600.0} if slept else {})}
        results = [None] * 4
        with mock.patch("builtins.print"):
            again = mutate.run_jobs([(0,), (1,), (2,), (3,)], run, 2, results)
        self.assertEqual([r["result"] for r in results], ["CAUGHT", "SLEPT", "CAUGHT", "SURVIVED"])
        self.assertNotIn("slept", results[0])
        self.assertEqual(again, 4)                                                            # 0 once, 1 three times
        self.assertEqual(sorted(calls), [0, 0, 1, 1, 1, 1, 2, 3])

    def test_a_failed_mutation_run_names_each_defects_final_result(self):
        out = ("TIMEOUT   a  (the machine slept 600.0 s)\nCAUGHT    a\nCAUGHT    c  (the machine slept 600.0 s)\n"
               "\nnot caught:\nSLEPT     c\nSURVIVED  b\n\n2/4 mutations caught (CAUGHT 2, SLEPT 1, SURVIVED 1)\n")
        with mock.patch.object(validate, "run", return_value=SimpleNamespace(stdout=out, returncode=1)):
            outcome = validate.check_mutations({}, Path("."), SimpleNamespace(workers=4))
        self.assertEqual((outcome.ok, outcome.detail, outcome.output.splitlines()),
                         (False, "2/4 mutations caught (CAUGHT 2, SLEPT 1, SURVIVED 1)", ["SLEPT     c", "SURVIVED  b"]))
        stopped = "CAUGHT    a\nTREE_CHANGED  b\n\nthe repository changed during the run (first seen with: b); stopped\n"
        with mock.patch.object(validate, "run", return_value=SimpleNamespace(stdout=stopped, returncode=2)):
            outcome = validate.check_mutations({}, Path("."), SimpleNamespace(workers=4))
        self.assertEqual((outcome.ok, outcome.output), (False, stopped))                    # why it stopped, in full


class MutationRunTest(unittest.TestCase):
    """tools/mutate.py's main() end to end on a tiny tree, with fake suites and a fake sleep clock (review of 6259a92:
    nothing tested how main() puts the resume, sleep, stop and tree rules together)."""

    HEALTHY = {"exit": 0, "timed_out": False, "ran": 5, "skipped": 0, "failures": 0, "errors": 0, "load_errors": 0,
               "failing": []}

    def setUp(self):
        import mutate
        self.mutate = mutate
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.temp = Path(tmp.name) / "engine", Path(tmp.name) / "temp"
        self.root.mkdir()
        self.temp.mkdir()
        (self.root / "a.py").write_bytes(b"A = 1\r\nB = 2\r\nC = 3\r\nD = 4\r\n")   # as a Windows checkout writes it
        self.defects = [("a.py", f"{name} = {n}", f"{name} = 0", name.lower()) for n, name in enumerate("ABCD", 1)]
        self.clock, self.calls, self.plan, self.on_call = [0.0], [], {}, None
        self.copy_seconds, self.copy_asleep, self.on_copy = 1000.0, 940.0, None
        self.env = {"python": "3", "executable": "python", "platform": "test", "packages": "p", "node": "node", "node_version": "v1"}

        def copy_outcome():
            self.clock[0] += self.copy_asleep         # by default 1,000 s on the clock, 940 of them asleep
            if self.on_copy:
                self.on_copy()
            return {**self.HEALTHY, "seconds": self.copy_seconds, "tree": mutate.tree_digest(self.root)}

        def one(relative, source, old, new, expected=None):
            label = next(defect[3] for defect in self.defects if defect[1] == old)
            self.calls.append(label)
            result, seconds, asleep = self.plan[label].pop(0)
            self.clock[0] += asleep
            if self.on_call:
                self.on_call(label)
            hit = result == "CAUGHT"
            return {**self.HEALTHY, "exit": int(hit), "failures": int(hit), "failing": ["t.T.test"] if hit else [],
                    "seconds": seconds, "tree": mutate.tree_digest(self.root)}
        for name, value in (("ROOT", self.root), ("MUTATIONS", self.defects), ("copy_outcome", copy_outcome), ("_one", one),
                            ("suite", lambda folder, timeout=None: {**self.HEALTHY, "seconds": 70.0}),
                            ("asleep_seconds", lambda: self.clock[0]), ("environment", lambda: dict(self.env))):
            patcher = mock.patch.object(mutate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("tempfile.tempdir", str(self.temp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mutate.main(["--workers", "1", *argv])
        return code, out.getvalue()

    def receipt(self):
        return json.loads((self.root / "evidence" / "mutation-receipt.json").read_text(encoding="utf-8"))

    def progress_files(self):
        return sorted(self.temp.glob("careeros-mutation-progress-*.jsonl"))

    def test_a_stopped_run_resumes_with_only_its_clean_catches(self):
        self.plan = {"a": [("CAUGHT", 100.0, 0)], "b": [("SURVIVED", 100.0, 0)], "c": [("CAUGHT", 100.0, 600.0)] * 4,
                     "d": [("CAUGHT", 400.0, 0)]}
        code, out = self.main()
        receipt = self.receipt()
        self.assertEqual((code, receipt["by_result"], receipt["reruns_after_sleep"], receipt["carried_over"]),
                         (1, {"CAUGHT": 2, "SLEPT": 1, "SURVIVED": 1}, 3, 0))
        self.assertEqual((receipt["copy_baseline"]["slept"], receipt["tree"], receipt["node_version"], receipt["packages"]),
                         (940.0, self.mutate.tree_digest(self.root), "v1", "p"))
        self.assertIn("\nnot caught:\nSURVIVED  b\nSLEPT     c\n", out)
        self.assertEqual(len(self.progress_files()), 1)
        self.calls.clear()
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "bcd"}
        code, out = self.main()
        receipt = self.receipt()
        # a carries over; d took 400 s, more than five copy baselines of 60 s awake, so it runs again
        self.assertEqual((code, sorted(self.calls), receipt["carried_over"]), (0, ["b", "c", "d"], 1))
        self.assertTrue(receipt["results"][0]["carried_over"])
        text = (self.root / "a.py").read_text(encoding="utf-8")                                # the text a copy holds
        self.assertEqual(receipt["source_sha256"]["a.py"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertIn("4/4 mutations caught (CAUGHT 4); 1 carried over from a stopped run", out)
        self.assertEqual(self.progress_files(), [])                                            # finished: nothing to resume

    def test_a_short_copy_baseline_still_allows_five_minutes(self):
        self.copy_seconds, self.copy_asleep = 30.0, 0.0
        self.plan = {"a": [("CAUGHT", 200.0, 0)], "b": [("SURVIVED", 100.0, 0), ("CAUGHT", 100.0, 0)],
                     "c": [("CAUGHT", 100.0, 0)], "d": [("CAUGHT", 100.0, 0)]}
        self.assertEqual(self.main()[0], 1)
        self.calls.clear()
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.calls, ["b"])                     # a's 200 s is under the 300 s floor: carried over

    def test_a_change_to_the_folder_during_a_run_stops_it_and_never_carries_over(self):
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "abcd"}
        self.on_call = lambda label: label == "b" and (self.root / "test_new.py").write_text("x = 1\n", encoding="utf-8")
        code, out = self.main()
        self.assertEqual((code, self.calls[:2]), (2, ["a", "b"]))
        self.assertIn("the repository changed during the run", out)
        self.assertIn("added test_new.py", out)
        self.assertFalse((self.root / "evidence" / "mutation-receipt.json").exists())
        lines = [json.loads(line) for line in self.progress_files()[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["result"]["mutation"] for line in lines], ["a"])
        (self.root / "test_new.py").unlink()                    # the change undone: the tree the run started on
        self.on_call, self.calls[:] = None, []
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "bcd"}
        code, out = self.main()
        self.assertEqual((code, sorted(self.calls), self.receipt()["carried_over"]), (0, ["b", "c", "d"], 1))

    def test_a_change_to_the_environment_during_a_run_stops_it(self):
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "abcd"}
        self.on_call = lambda label: label == "b" and self.env.update(node_version="v2")
        code, out = self.main()
        self.assertEqual((code, self.calls[:2]), (2, ["a", "b"]))
        self.assertIn("the environment changed during the run", out)
        lines = [json.loads(line) for line in self.progress_files()[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["result"]["mutation"] for line in lines], ["a"])

    def test_a_stop_request_ends_the_run_and_the_next_run_carries_on(self):
        stop = self.temp / self.mutate.STOP_FILE
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "abcd"}
        self.on_call = lambda label: label == "a" and stop.write_text("", encoding="utf-8")
        code, out = self.main()
        self.assertEqual((code, self.calls, stop.exists()), (3, ["a"], False))
        self.on_call, self.calls[:] = None, []
        code, out = self.main()
        self.assertEqual((code, sorted(self.calls)), (0, ["b", "c", "d"]))

    def test_only_a_stop_request_made_after_the_run_started_counts(self):
        import time
        stop = self.temp / self.mutate.STOP_FILE
        stop.write_text("", encoding="utf-8")
        os.utime(stop, (time.time() - 3600, time.time() - 3600))   # left from an hour ago
        self.plan = {label: [("CAUGHT", 100.0, 0)] for label in "abcd"}
        self.assertEqual(self.main()[0], 0)
        self.assertFalse(stop.exists())
        self.on_copy = lambda: stop.write_text("", encoding="utf-8")   # asked for while the baselines ran
        self.calls.clear()
        code, out = self.main()
        self.assertEqual((code, self.calls, stop.exists()), (3, [], False))

    def test_one_run_per_tree_and_fresh_starts_from_zero(self):
        mutate = self.mutate
        key = mutate.run_key(mutate.tree_digest(), mutate.environment())
        with mutate.only_run(mutate.progress_path(key)):
            code, out = self.main()
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("another run on this tree", out)
        self.plan = {label: [("CAUGHT", 100.0, 0)] * 2 for label in "acd"}
        self.plan["b"] = [("SURVIVED", 100.0, 0), ("CAUGHT", 100.0, 0)]
        self.assertEqual(self.main()[0], 1)
        self.calls[:] = []
        self.assertEqual(self.main("--fresh")[0], 0)
        self.assertEqual(sorted(self.calls), ["a", "b", "c", "d"])

    def test_a_change_after_the_checks_writes_no_receipt_even_when_every_result_carries_over(self):
        """Codex Pass 3 (0c907ec): with every defect carried over no scratch copy is made, so nothing looked at the tree
        or the environment after the baselines' check; an edit there put untested source hashes, under the old tree, in a
        passing receipt. The run reads its sources against the tree it accepted, and looks once more before a receipt."""
        mutate = self.mutate
        receipt = self.root / "evidence" / "mutation-receipt.json"
        original = "A = 1\nB = 2\nC = 3\nD = 4\n"
        changes = {"the file the defects change": lambda: (self.root / "a.py").write_text(original + "E = 5\n", encoding="utf-8"),
                   "another file": lambda: (self.root / "b.py").write_text("x = 1\n", encoding="utf-8"),
                   "the environment": lambda: self.env.update(node_version="v2")}
        only_run, run_jobs = mutate.only_run, mutate.run_jobs

        def changed_back(*args):   # the file put back after the sources were read, before the last look
            (self.root / "a.py").write_text(original, encoding="utf-8")
            return run_jobs(*args)
        for what, change in [*changes.items(), ("a change undone before the last look", changes["the file the defects change"])]:
            with self.subTest(what):
                (self.root / "a.py").write_text(original, encoding="utf-8")
                (self.root / "b.py").unlink(missing_ok=True)
                self.env["node_version"] = "v1"
                for path in self.progress_files():
                    path.unlink()
                key = mutate.run_key(mutate.tree_digest(), mutate.environment())
                for index, (relative, _old, _new, label) in enumerate(self.defects):   # every defect already caught
                    mutate.append_progress(mutate.progress_path(key), {"index": index, **key, "result": {
                        "mutation": label, "file": relative, "result": "CAUGHT", "seconds": 100.0, "ran": 5,
                        "failing": ["t.T.test"]}})
                receipt.parent.mkdir(exist_ok=True)
                receipt.write_text('{"the last receipt": true}', encoding="utf-8")

                @contextlib.contextmanager
                def after_the_checks(path, change=change):
                    with only_run(path):
                        change()
                        yield
                undo = changed_back if what.startswith("a change undone") else run_jobs
                with mock.patch.object(mutate, "only_run", after_the_checks), mock.patch.object(mutate, "run_jobs", undo):
                    code, out = self.main()
                self.assertEqual((code, self.calls), (2, []), out)
                self.assertEqual(receipt.read_text(encoding="utf-8"), '{"the last receipt": true}')   # never overwritten
                self.assertEqual(len(self.progress_files()), 1)                                          # still resumable

    def test_the_defect_list_that_runs_is_the_one_in_the_tree_and_every_defect_file_is_one_it_holds(self):
        """A fresh check of the Codex Pass 3 fix: a save of tools/mutate.py between Python reading it and the tree's hash
        ran one defect list and certified another; and a defect file spelled unlike the tree's own stopped every run as
        if the repository had changed."""
        mutate = self.mutate
        tool = self.root / "tools" / "mutate.py"
        tool.parent.mkdir()
        tool.write_text("MUTATIONS = " + repr(self.defects) + "\n", encoding="utf-8")
        with mock.patch.object(mutate, "__file__", str(tool)):
            files = mutate.tree_files()
            self.assertIsNone(mutate.defect_list_drift(files))
            tool.write_text("MUTATIONS = " + repr([*self.defects[:3], ("a.py", "D = 4", "D = 1", "d")]) + "\n", encoding="utf-8")
            self.assertIn("changed after the tree was hashed", mutate.defect_list_drift(files))
            self.assertIn("is not the one this process loaded", mutate.defect_list_drift(mutate.tree_files()))
            code, out = self.main()
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("is not the one this process loaded", out)
        tool.unlink()
        misspelled = [("A.PY", "A = 1", "A = 0", "spelled otherwise")]
        with mock.patch.object(mutate, "MUTATIONS", misspelled):
            self.assertIn("not a file of the tree as spelled", " ".join(mutate.anchor_problems()))
            code, out = self.main()
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("is not a file of the tree as spelled", out)

    def test_a_change_while_the_baselines_run_is_refused(self):
        self.on_copy = lambda: (self.root / "b.py").write_text("x = 1\n", encoding="utf-8")
        code, out = self.main()
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("changed while the baselines ran (added b.py)", out)
        (self.root / "b.py").unlink()
        self.on_copy = None
        real = self.mutate.copy_outcome
        with mock.patch.object(self.mutate, "copy_outcome", lambda: {**real(), "tree": "another tree"}):
            code, out = self.main()                             # a baseline copy that isn't the tree: refused too
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("changed and changed back", out)


if __name__ == "__main__":
    unittest.main()
