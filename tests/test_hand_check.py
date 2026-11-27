"""tools/hand_check.py (efficiency review of 2026-11-16, item 3): one repository hand-check tool. It chooses defects as
the per-batch scripts did, runs the tests most likely to catch each first, and counts a catch only by tools/mutate.py's
rule."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from careeros.paths import ENGINE_ROOT

sys.path.insert(0, str(ENGINE_ROOT / "tools"))
import hand_check  # noqa: E402
import mutate  # noqa: E402

CALC = "def add(a, b):\n    return a + b\n\n\ndef twice(a):\n    return a * 2\n"
TEST_CALC = ("import unittest\n\nfrom calc import add\n\n\nclass A(unittest.TestCase):\n    def test_add(self):\n"
             "        self.assertEqual(add(2, 3), 5)\n")
TEST_OTHER = ("import unittest\nfrom pathlib import Path\n\n\nclass B(unittest.TestCase):\n    def test_writes_into_its_tree(self):\n"
              "        Path('stray.txt').write_text('a test that leaves a file in the copy', encoding='utf-8')\n\n"
              "    def test_twice(self):\n        from calc import twice\n        self.assertEqual(twice(2), 4)\n")


class ChoosingTest(unittest.TestCase):
    MUTATIONS = [("a.py", "x", "y", "one in a"), ("a.py", "z", "w", "two in a"), ("b.py", "x", "y", "one in b"),
                 ("c.py", "x", "y", "new in c")]

    def test_the_defect_list_is_read_from_the_tool_text(self):
        text = (ENGINE_ROOT / "tools" / "mutate.py").read_text(encoding="utf-8")
        self.assertEqual(hand_check.listed(text), [tuple(m) for m in mutate.MUTATIONS])

    def test_new_and_changed_file_defects_are_chosen(self):
        base = self.MUTATIONS[:3]
        picked = hand_check.choose(self.MUTATIONS, base_list=base, changed={"b.py"})
        self.assertEqual(picked, [(2, "shared"), (3, "new")])
        reanchored = [("a.py", "x-old", "y", "one in a"), *self.MUTATIONS[1:3]]
        self.assertEqual(hand_check.choose(self.MUTATIONS, base_list=reanchored), [(0, "new"), (3, "new")])

    def test_files_labels_and_all(self):
        self.assertEqual(hand_check.choose(self.MUTATIONS, files={"a.py"}), [(0, "shared"), (1, "shared")])
        self.assertEqual(hand_check.choose(self.MUTATIONS, labels=["two", "in b"]), [(1, "shared"), (2, "shared")])
        self.assertEqual(hand_check.choose(self.MUTATIONS, files={"a.py"}, labels=["two"]), [(1, "shared")])   # narrows
        self.assertEqual(len(hand_check.choose(self.MUTATIONS, everything=True)), 4)
        self.assertEqual(hand_check.choose(self.MUTATIONS), [])


class PriorCatchesTest(unittest.TestCase):
    def test_earlier_catches_become_the_first_stages(self):
        receipts = [
            {"results": [{"mutation": "d1", "file": "a.py", "result": "CAUGHT", "failing": ["tests.test_x.A.test_one", "not a test id"]},
                         {"mutation": "d2", "file": "a.py", "result": "SURVIVED", "failing": ["tests.test_z.C.test_never"]}]},
            {"results": [{"mutation": "d1", "file": "a.py", "result": "CAUGHT", "failing": ["tests.test_y.B.test_two", "tests.test_x.A.test_one"]},
                         "not a row"]},
        ]
        by_label, by_file = hand_check.prior_catches(receipts)
        self.assertEqual(by_label, {"d1": ["tests.test_x.A.test_one", "tests.test_y.B.test_two"]})
        self.assertEqual(by_file, {"a.py": ["tests.test_x", "tests.test_y"]})
        names = hand_check.stage_names("d1", "a.py", by_label, by_file, ["tests.test_w"], {"tests.test_x.A.test_one", "tests.test_x", "tests.test_w"})
        self.assertEqual(names, {"prior": ["tests.test_x.A.test_one"], "modules": ["tests.test_x", "tests.test_w"], "suite": None})

    def test_a_selection_never_names_a_test_twice(self):
        """2026-11-17: the first --all campaign named a class and one of its tests; that test ran twice in one process and
        failed on its own leftover folder."""
        names = ["tests.test_a.A.test_one", "tests.test_a.A", "tests.test_b.B.test_two", "tests.test_a.A.test_one", "tests.test_b",
                 "tests.test_c.C.test_three", "tests.test_cc.C.test_four"]
        self.assertEqual(hand_check.tidy(names), ["tests.test_a.A", "tests.test_b", "tests.test_c.C.test_three", "tests.test_cc.C.test_four"])


class RunningTest(unittest.TestCase):
    def test_the_summary_reads_unittest_as_mutate_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_a.py").write_text(
                "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_ok(self):\n        pass\n\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n\n    def test_errors(self):\n        raise KeyError('x')\n\n"
                "    @unittest.skip('later')\n    def test_skipped(self):\n        pass\n", encoding="utf-8")
            (root / "tests" / "test_b.py").write_text("import a_module_that_does_not_exist\n", encoding="utf-8")
            theirs = mutate.suite(root, 120)
            ours = hand_check.run_tests(root, None)
            named = hand_check.run_tests(root, ["tests.test_a.A.test_ok", "tests.test_a.A.test_fails"])
            flaky = hand_check.run_tests(root, ["tests.test_a.A.test_fails"], flaky=frozenset({"test_fails"}))
            many = hand_check.run_tests(root, ["tests.test_a.A.test_ok"] * 2000)   # 44,000 characters of names (WinError 206 on 2026-11-17)
        fields = ("exit", "ran", "failures", "errors", "skipped", "load_errors")
        self.assertEqual({k: ours[k] for k in fields}, {k: theirs[k] for k in fields})
        self.assertEqual(sorted(ours["failing"]), sorted(theirs["failing"]))
        self.assertEqual(hand_check.verdict(ours, 5), "LOAD_ERROR")      # a module never loaded: not proof, even with failures
        self.assertEqual((named["ran"], hand_check.verdict(named, 2)), (2, "CAUGHT"))
        self.assertEqual(hand_check.verdict(named, 3), "LOAD_ERROR")    # fewer tests ran than the names count
        self.assertTrue(flaky["flaky_only"])
        self.assertEqual(hand_check.verdict(flaky, 1), "SURVIVED")     # a known-flaky failure alone is never a catch
        self.assertTrue(hand_check.passes(flaky))
        self.assertEqual((many["exit"], many["ran"]), (0, 2000))

    def test_a_stopped_run_carries_over_only_clean_catches_on_the_same_tree(self):
        mutations = [("a.py", "x", "y", "d0"), ("a.py", "z", "w", "d1"), ("a.py", "q", "r", "d2")]
        key = {"tree": "t" * 64, "env": "e" * 64}
        lines = [{"index": 0, **key, "result": {"mutation": "d0", "result": "CAUGHT"}},
                 {"index": 1, **key, "result": {"mutation": "d1", "result": "CAUGHT", "slept": 30}},
                 {"index": 2, "tree": "o" * 64, "env": key["env"], "result": {"mutation": "d2", "result": "CAUGHT"}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            path.write_bytes(b"\n".join(json.dumps(line).encode("utf-8") for line in lines) + b"\n{cut sho")
            kept = hand_check.carried_over(path, key, mutations)
        self.assertEqual(list(kept), [0])
        self.assertTrue(kept[0]["carried_over"])


class EndToEndTest(unittest.TestCase):
    def test_a_hand_check_of_a_small_tree(self):
        """Prior tests catch what they caught before; a defect nothing catches reaches the whole suite and survives; a
        defect that breaks an import is never counted as caught; a missing anchor is reported; a copy a test changed is
        rebuilt; a catch by a few tests while the whole suite no longer loads is no catch; the receipt is written outside the
        tree and names what it checked."""
        mutations = [("calc.py", "return a + b", "return a - b", "add broken"),
                     ("calc.py", "return a * 2", "return a * 2 + 0", "a change no test can see"),
                     ("calc.py", "def add(a, b):", "def add(a, b:", "calc no longer imports"),
                     ("calc.py", "return a / b", "return b / a", "anchor gone"),
                     ("calc.py", "def twice(a):", "def twice(a:", "caught by a lazy import while the suite no longer loads")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, "ROOT", Path(tmp) / "engine"), \
                mock.patch.object(mutate, "MUTATIONS", mutations), mock.patch("tempfile.tempdir", tmp):
            root = mutate.ROOT
            (root / "tests").mkdir(parents=True)
            (root / "evidence").mkdir()
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
            (root / "tests" / "test_other.py").write_text(TEST_OTHER, encoding="utf-8")
            (root / "calc.py").write_text(CALC, encoding="utf-8")
            (root / "evidence" / "mutation-receipt.json").write_text(json.dumps({"results": [
                {"mutation": "add broken", "file": "calc.py", "result": "CAUGHT", "failing": ["tests.test_calc.A.test_add"]},
                {"mutation": "an old defect", "file": "calc.py", "result": "CAUGHT", "failing": ["tests.test_gone.G.test_old"]},
                {"mutation": "caught by a lazy import while the suite no longer loads", "file": "calc.py", "result": "CAUGHT",
                 "failing": ["tests.test_other.B.test_twice"]}]}),
                encoding="utf-8")
            out = Path(tmp) / "hc"
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                inside = hand_check.main([str(root / "evidence" / "hc"), "--files", "calc.py"])
                code = hand_check.main([str(out), "--files", "calc.py", "--workers", "1", "--what", "a test"])
            receipt = json.loads((Path(tmp) / "hc.json").read_text(encoding="utf-8"))
            left = (Path(tmp) / "hc.jsonl").exists()
        self.assertEqual(inside, 2, printed.getvalue())
        self.assertEqual(code, 1, printed.getvalue())
        rows = {r["mutation"]: r for r in receipt["results"]}
        self.assertEqual((rows["add broken"]["result"], rows["add broken"]["scope"]), ("CAUGHT", "prior"))
        self.assertEqual((rows["a change no test can see"]["result"], rows["a change no test can see"]["scope"]), ("SURVIVED", "suite"))
        self.assertEqual(rows["calc no longer imports"]["result"], "LOAD_ERROR")
        self.assertEqual(rows["anchor gone"]["result"], "ANCHOR_NOT_FOUND")
        lazy = rows["caught by a lazy import while the suite no longer loads"]
        self.assertEqual((lazy["result"], lazy["stages"][0]["stage"], lazy["stages"][0]["result"]), ("LOAD_ERROR", "prior", "LOAD_ERROR"))
        self.assertNotIn("CAUGHT", [s["result"] for s in lazy["stages"]])   # its test failed, but a full run would never count it
        self.assertEqual((receipt["checked"], receipt["caught"], receipt["defined"]), (5, 1, 5))
        self.assertEqual((receipt["what"], receipt["by_scope"]), ("a test", {"prior": 1}))
        self.assertIn("test names from earlier receipts no longer load", printed.getvalue())
        self.assertTrue(left)   # not everything was caught: the progress file stays for a resumed run

    def test_a_selection_that_fails_without_the_defect_never_catches_it(self):
        """A test that passes in the whole suite but fails alone (it needs state another module sets) would fail under any
        defect when run alone. Its selection is checked on the unmutated copy first and skipped; the whole suite decides."""
        sets = ("import builtins\nimport unittest\n\nfrom calc import add\n\nbuiltins.CAREEROS_HC_FLAG = True\n\n\n"
                "class S(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        needs = ("import builtins\nimport unittest\n\n\nclass Z(unittest.TestCase):\n    def test_needs(self):\n"
                 "        self.assertTrue(getattr(builtins, 'CAREEROS_HC_FLAG', False))\n")
        mutations = [("calc.py", "return a + b", "return a - b", "add broken")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, "ROOT", Path(tmp) / "engine"), \
                mock.patch.object(mutate, "MUTATIONS", mutations), mock.patch("tempfile.tempdir", tmp):
            root = mutate.ROOT
            (root / "tests").mkdir(parents=True)
            (root / "evidence").mkdir()
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_aa_sets.py").write_text(sets, encoding="utf-8")
            (root / "tests" / "test_zz_needs.py").write_text(needs, encoding="utf-8")
            (root / "calc.py").write_text(CALC, encoding="utf-8")
            (root / "evidence" / "mutation-receipt.json").write_text(json.dumps({"results": [   # a misleading earlier catch
                {"mutation": "add broken", "file": "calc.py", "result": "CAUGHT", "failing": ["tests.test_zz_needs.Z.test_needs"]}]}),
                encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                code = hand_check.main([str(Path(tmp) / "hc"), "--all", "--workers", "1"])
            receipt = json.loads((Path(tmp) / "hc.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, printed.getvalue())
        row = receipt["results"][0]
        self.assertEqual((row["result"], row["scope"]), ("CAUGHT", "suite"))
        self.assertEqual([s["result"] for s in row["stages"]], ["SKIPPED: fails unmutated", "SKIPPED: fails unmutated", "CAUGHT"])
        self.assertEqual(receipt["selections_failing_unmutated"], [["tests.test_zz_needs.Z.test_needs"], ["tests.test_zz_needs"]])


if __name__ == "__main__":
    unittest.main()
