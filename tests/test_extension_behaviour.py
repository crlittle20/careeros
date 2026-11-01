"""How CareerOS Assist behaves, run for real (independent Codex review of ac83e00, 2026-10-22: F1, F5, F6; and the
second Claude review of its corrections, the same day).

tests/extension_harness.cjs runs the real extension scripts in Node with a synthetic page and fake Chrome APIs. It
needs a Node.js runtime: CAREEROS_NODE names one, or `node` on PATH. Without one these tests are skipped, and the
extension is still covered by tests/test_extension.py, which reads its source and manifest. tools/validate.py says
plainly when these tests were NOT RUN, and fails a --full run without them.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from careeros.paths import ENGINE_ROOT

NODE = os.environ.get("CAREEROS_NODE") or shutil.which("node")
SKIP = "no Node.js runtime (set CAREEROS_NODE): the extension's behaviour isn't run here"


@lru_cache(maxsize=1)
def results() -> dict:
    sys.path.insert(0, str(ENGINE_ROOT / "tools"))
    from check_handoff import make
    with tempfile.TemporaryDirectory() as tmp:   # the installed-check handoff, made at the harness's own clock
        check = Path(tmp) / "check.json"
        check.write_text(json.dumps(make("https://job-boards.greenhouse.io/synth/jobs/1",
                                         [("question_1", "select", "Yes")], datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc))),
                         encoding="utf-8")
        done = subprocess.run([NODE, str(ENGINE_ROOT / "tests" / "extension_harness.cjs")], capture_output=True, text=True,
                              encoding="utf-8", timeout=120,
                              env={**os.environ, "CAREEROS_CHECK_HANDOFF": str(check), "TZ": "UTC"})
    data = json.loads(done.stdout or "{}")
    if "harness_error" in data:
        raise AssertionError(data["harness_error"])
    return data


@lru_cache(maxsize=1)
def end_to_end() -> dict:
    """A handoff CareerOS itself writes (answers file, package, assisted session), loaded and filled by the real popup
    and page scripts (independent Codex review, Pass 2 of 4787e2a: exercise the whole way to the page)."""
    from unittest import mock

    from careeros.review import assist, packages
    from careeros.review.labels import record_label
    from tests.test_assist import (GREENHOUSE_FILL, HIDDEN_WORDS_LIBRARY, HIDDEN_WORDS_QUESTIONS, LONG_ANSWER_QUESTIONS,
                                   LONG_ANSWERS_LIBRARY)
    from tests.test_packages import FACTS, JOB, LIBRARY, MID, POLICY, Vault, posting_store
    vault = Vault(LIBRARY + LONG_ANSWERS_LIBRARY + HIDDEN_WORDS_LIBRARY)
    questions = [*LONG_ANSWER_QUESTIONS, *HIDDEN_WORDS_QUESTIONS]
    try:
        store, _ = posting_store()
        record_label(store, JOB, "apply", None, "k1")
        packages.prepare(store, POLICY, MID, JOB, facts=FACTS, library=vault.library, vault_root=vault.root,
                         questions=questions)
        with mock.patch.object(assist, "assistance_policy", return_value=GREENHOUSE_FILL):
            out = assist.open_session(store, POLICY, MID, JOB, "assisted", facts=FACTS, library=vault.library,
                                      vault_root=vault.root, questions=questions, key="e2e")
        page = ",".join(q["name"] for q in questions if q["name"] != "first_name")
        done = subprocess.run([NODE, str(ENGINE_ROOT / "tests" / "extension_harness.cjs")], capture_output=True, text=True,
                              encoding="utf-8", timeout=120,
                              env={**os.environ, "CAREEROS_E2E_HANDOFF": str(vault.root / out["handoff"]),
                                   "CAREEROS_E2E_NOW": store.clock.now(), "CAREEROS_E2E_PAGE": page, "TZ": "UTC"})
    finally:
        vault.close()
    data = json.loads(done.stdout or "{}")
    if "harness_error" in data:
        raise AssertionError(data["harness_error"])
    return data["e2e"]


def refused(case: str) -> bool:
    outcome = results()[case]
    return bool(outcome["result"]["refused"]) and outcome["result"]["filled"] == [] and outcome["value"] == ""


@unittest.skipUnless(NODE, SKIP)
class FillTimeChecksTests(unittest.TestCase):
    """F1: the posting, the tab and the expiry are checked again when you press Fill, and again inside the page."""

    def test_the_named_posting_is_filled_with_one_top_frame_call(self):
        same = results()["same_posting"]
        self.assertEqual((same["executions"], same["value"]), (1, "Avery"))
        self.assertEqual(same["spec"], ["args", "func", "target"])         # no world, no frames, nothing else
        self.assertEqual(same["target"], ["tabId"])
        self.assertEqual(same["events"], ["input", "change"])              # third review: never a submit event
        self.assertEqual(results()["select_events"], ["input", "change"])

    def test_a_page_that_refuses_is_reported_as_not_filled(self):
        """Third review: the tab looked right to the popup, but the page itself had moved."""
        refused = results()["page_refused_through_popup"]
        self.assertEqual((refused["executions"], refused["value"], refused["className"]), (1, "", "error"))
        self.assertIn("Not filled: this page isn't the posting", refused["result"])

    def test_a_tab_that_moved_to_another_posting_after_the_preview_is_never_filled(self):
        moved = results()["moved_after_preview"]
        self.assertEqual((moved["executions"], moved["value"]), (0, ""))
        self.assertIn("isn't the posting", moved["result"])
        longer = results()["moved_to_longer_number"]                          # /jobs/1 is not /jobs/12
        self.assertEqual((longer["executions"], longer["value"]), (0, ""))
        loaded = results()["loaded_on_longer_number"]
        self.assertEqual((loaded["executions"], loaded["handler"]), (0, False))

    def test_another_tab_in_front_at_fill_is_never_filled(self):
        self.assertEqual(results()["other_tab_at_fill"], {"executions": 0, "value": ""})

    def test_a_handoff_that_expired_after_the_preview_is_never_filled(self):
        expired = results()["expired_after_preview"]
        self.assertEqual((expired["executions"], expired["value"]), (0, ""))
        self.assertIn("expired", expired["result"])

    def test_the_page_function_refuses_another_posting_an_expired_handoff_or_no_guard(self):
        for case in ("page_guard_other_posting", "page_guard_longer_number", "page_guard_expired", "page_guard_missing",
                     "page_guard_path_elsewhere"):
            self.assertTrue(refused(case), case)

    def test_the_page_function_takes_its_sites_from_its_own_table_not_the_guard(self):
        """Second review: a guard with no path, no adapter, another adapter, an odd path or another origin is refused
        inside the page, whatever the popup passed."""
        for case in ("page_guard_empty_path", "page_guard_no_adapter", "page_guard_other_adapter",
                     "page_guard_inherited_adapter", "page_guard_odd_path", "page_guard_unlisted_origin",
                     "page_guard_self_declared_origin"):
            self.assertTrue(refused(case), case)

    def test_the_page_function_refuses_a_handoff_that_claims_a_longer_life(self):
        for case in ("page_guard_far_future", "page_guard_zoneless", "page_guard_three_hours", "page_guard_cap_plus_1ms"):
            self.assertTrue(refused(case), case)
        self.assertEqual(results()["page_guard_at_cap"]["result"]["filled"], ["first_name"])   # two hours and five minutes

    def test_the_page_function_fills_only_its_adapters_fields(self):
        outcome = results()["page_unreviewed_field"]
        self.assertEqual(outcome["result"]["filled"], [])
        self.assertTrue(any("not a field this adapter fills" in s for s in outcome["result"]["skipped"]))

    def test_hidden_password_and_search_inputs_are_never_filled(self):
        for case in ("input_hidden", "input_password", "input_search"):
            outcome = results()[case]
            self.assertEqual((outcome["value"], outcome["result"]["filled"]), ("", []), case)

    def test_a_name_two_elements_answer_to_is_left_for_you(self):
        outcome = results()["name_shared_by_two"]
        self.assertEqual(outcome["values"], ["", ""])
        self.assertTrue(any("2 fields" in s for s in outcome["result"]["skipped"]))


@unittest.skipUnless(NODE, SKIP)
class EndToEndTests(unittest.TestCase):
    """Independent Codex review, Pass 2 of 4787e2a: from the answers file to the page, a long or padded consent is never
    filled, a long routine answer is left to copy, and a short checked answer is filled; and (the read-only check of
    that correction) a value whose words the checks can't read as the page will show them is never filled either."""

    def test_a_handoff_careeros_writes_fills_only_its_short_checked_answers(self):
        e2e = end_to_end()
        self.assertEqual(e2e["executions"], 1, e2e)
        self.assertEqual(e2e["values"], {"first_name": "Avery", "question_75": "", "question_74": "", "question_76": "Two weeks",
                                         "question_77": "", "question_78": "", "question_79": "", "question_80": "",
                                         "question_81": "", "question_82": "", "question_83": ""})


@unittest.skipUnless(NODE, SKIP)
class ChoicesTests(unittest.TestCase):
    """F5: an existing choice is kept whatever its position; only an empty placeholder is replaced. Second review:
    the choice is picked by position, never a disabled one, never one of two that read alike."""

    def test_a_real_first_choice_is_kept(self):
        for case in ("select_first_real_choice", "select_empty_value_real_choice"):
            outcome = results()[case]
            self.assertEqual(outcome["index"], 0, case)
            self.assertEqual(outcome["result"]["filled"], [], case)
            self.assertTrue(any("already chosen" in s for s in outcome["result"]["skipped"]), case)

    def test_a_real_choice_further_down_is_kept(self):
        for case in ("select_later_real_choice", "select_later_real_choice_empty_value"):
            outcome = results()[case]
            self.assertEqual((outcome["index"], outcome["text"], outcome["result"]["filled"]), (1, "No", []), case)
            self.assertTrue(any("already chosen" in s for s in outcome["result"]["skipped"]), case)

    def test_an_empty_placeholder_is_replaced(self):
        for case in ("select_placeholder", "select_disabled_placeholder", "select_blank_placeholder"):
            outcome = results()[case]
            self.assertEqual((outcome["value"], outcome["result"]["filled"]), ("1", ["question_1"]), case)

    def test_the_choice_is_picked_by_position_even_when_values_are_shared_or_empty(self):
        for case in ("select_shared_value", "select_empty_value_target"):
            outcome = results()[case]
            self.assertEqual((outcome["text"], outcome["result"]["filled"]), ("Yes", ["question_1"]), case)

    def test_a_disabled_choice_or_two_choices_alike_are_left_for_you(self):
        for case in ("select_disabled_target", "select_two_choices_alike", "select_disabled_group"):
            outcome = results()[case]
            self.assertEqual((outcome["index"], outcome["result"]["filled"]), (0, []), case)
            self.assertTrue(outcome["result"]["skipped"], case)

    def test_a_list_of_several_choices_or_a_choice_the_page_rejects_is_left_for_you(self):
        several = results()["select_multiple"]
        self.assertEqual((several["index"], several["result"]["filled"]), (0, []))
        rejected = results()["select_that_doesnt_take"]
        self.assertEqual(rejected["filled"], [])
        self.assertTrue(any("didn't take" in s for s in rejected["skipped"]))

    def test_a_value_that_is_already_right_is_reported_and_left_alone(self):
        outcome = results()["select_already_matching"]
        self.assertEqual((outcome["value"], outcome["index"]), ("1", 1))
        self.assertEqual((outcome["result"]["skipped"], outcome["result"]["already"]), ([], ["question_1"]))
        text = results()["text_already_right"]
        self.assertEqual((text["result"]["already"], text["events"]), (["first_name"], []))

    def test_a_value_you_typed_is_kept(self):
        outcome = results()["typed_value"]
        self.assertEqual(outcome["value"], "Typed")
        self.assertTrue(any("already filled" in s for s in outcome["result"]["skipped"]))


@unittest.skipUnless(NODE, SKIP)
class ReviewedDestinationTests(unittest.TestCase):
    """F6: the extension knows its adapters' reviewed hosts and field names; a file can't grant itself more, nor a
    longer life (second review)."""

    def test_a_handoff_careeros_writes_is_accepted(self):
        self.assertEqual(results()["valid_handoff"], [])

    def test_a_file_cant_name_its_own_origin_path_field_or_adapter(self):
        for case in ("unlisted_origin", "mixed_origins", "unreviewed_field", "field_with_newline", "odd_path",
                     "unknown_adapter", "adapter_as_list"):
            self.assertTrue(results()[case], case)

    def test_a_file_cant_give_itself_a_longer_life_or_a_local_time(self):
        for case in ("far_future", "three_hours", "zoneless", "year_only"):
            self.assertTrue(results()[case], case)
        self.assertEqual(results()["far_future_click"], {"executions": 0, "value": ""})

    def test_a_file_without_what_careeros_writes_is_refused_with_a_message(self):
        for case in ("no_job", "no_session", "no_review", "bad_attach"):
            self.assertTrue(results()[case], case)
        self.assertEqual(results()["no_job_load"]["handler"], False)
        self.assertIn("missing what CareerOS writes", results()["no_job_load"]["status"])

    def test_a_forged_destination_is_never_filled(self):
        self.assertEqual(results()["forged_destination_click"], {"executions": 0, "value": ""})

    def test_both_tables_read_by_javascript_equal_careeros(self):
        """Third review: a repeated key would keep its last value; compare what JavaScript itself reads."""
        from careeros.review import adapters
        for name, table in results()["adapter_tables"].items():
            self.assertEqual(list(table), ["greenhouse"], name)
            self.assertEqual(table["greenhouse"]["origins"], list(adapters.GREENHOUSE_ORIGINS), name)
            self.assertEqual(table["greenhouse"]["path"].replace("\\/", "/"), adapters.GREENHOUSE_PATH, name)
            self.assertEqual(table["greenhouse"]["field"], adapters.GREENHOUSE_FIELD, name)

    def test_the_installed_check_handoff_is_one_the_extension_accepts(self):
        self.assertEqual(results()["check_handoff"], [])

    def test_the_extension_touches_only_tabs_query_and_execute_script(self):
        self.assertEqual(results()["chrome_members"], results()["chrome_expected"])


if __name__ == "__main__":
    unittest.main()
