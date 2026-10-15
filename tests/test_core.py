import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from careeros.core.clock import from_external, parse_utc, require_order
from careeros.core.fingerprint import canonical_json, fingerprint
from careeros.core.locks import LockError, SingleWriterLock
from careeros.core.records import validate_audit_event, validate_exception
from careeros.core.redact import redact
from careeros.core.transitions import TransitionRejected, load_matrix, verify_history
from careeros.db.store import DataQualityError, IdempotencyCollision, PermissionDenied

from tests.helpers import JOB_MATRIX, add_job, new_store


class ClockTests(unittest.TestCase):
    def test_profile_and_order(self):
        self.assertEqual(parse_utc("2026-10-10T12:00:00Z").year, 2026)
        with self.assertRaises(ValueError):
            parse_utc("2026-10-10 12:00:00")
        with self.assertRaises(ValueError):
            require_order("2026-10-10T12:00:00.000Z", "2026-10-10T11:59:59.000Z")

    def test_external_conversion(self):
        self.assertEqual(from_external("2026-10-01T09:30:00-05:00"), "2026-10-01T14:30:00.000Z")
        self.assertEqual(from_external(1759312200000), "2025-10-01T09:50:00.000Z")
        self.assertIsNone(from_external("not a date"))


class FingerprintTests(unittest.TestCase):
    def test_key_order_irrelevant_and_nan_rejected(self):
        self.assertEqual(fingerprint({"a": 1, "b": [1, 2]}), fingerprint({"b": [1, 2], "a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})


class MatrixTests(unittest.TestCase):
    def test_rules(self):
        m = JOB_MATRIX
        m.check("discovered", "normalized")
        for args, code in [(("discovered", "discovered"), "self_transition"), (("discovered", "scored"), "transition_not_allowed"),
                           (("extracted", "rejected"), "reason_required"), (("duplicate", "normalized"), "terminal_state"),
                           (("closed", "normalized"), "reason_required"),
                           (("nope", "normalized"), "unknown_state")]:
            with self.assertRaises(TransitionRejected) as ctx:
                m.check(*args)
            self.assertEqual(ctx.exception.code, code)

    def test_invalid_matrix_files_rejected(self):
        bad = {"workflow": "x", "states": {"a": "active", "b": "done"}, "initial_state": "a", "terminal": [], "allowed": []}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_matrix(p)

    def test_history_invariants(self):
        rows = [
            {"transition_id": "t1", "sequence": 1, "from_state": "discovered", "to_state": "normalized", "transitioned_at": "2026-10-10T12:00:01.000Z"},
            {"transition_id": "t2", "sequence": 3, "from_state": "extracted", "to_state": "eligible", "transitioned_at": "2026-10-10T12:00:00.000Z"},
        ]
        errors = verify_history(JOB_MATRIX, rows, initial_entered_at="2026-10-10T12:00:00.000Z")
        self.assertEqual(errors, {"sequence_gap", "broken_chain", "time_order"})


class RecordTests(unittest.TestCase):
    def test_audit_and_exception_validation(self):
        ok = {"contract_version": "0.2.0", "event_id": "e", "event_type": "job_found", "occurred_at": "2026-10-10T12:00:00.000Z",
              "project_id": "careeros", "project_version": "0.1.0", "source": "t", "outcome": "success"}
        self.assertEqual(validate_audit_event(ok), [])
        self.assertIn("event_type_format", validate_audit_event({**ok, "event_type": "Job Found"}))
        self.assertIn("outcome", validate_audit_event({**ok, "outcome": "maybe"}))
        exc = {"contract_version": "0.2.0", "exception_id": "x", "occurred_at": "2026-10-10T12:00:00.000Z", "project_id": "careeros",
               "source": "t", "exception_type": "integration", "severity": "low", "message": "m", "status": "resolved"}
        self.assertIn("resolution_required", validate_exception(exc))
        self.assertIn("status", validate_exception({**exc, "status": "ignored"}))


class RedactTests(unittest.TestCase):
    def test_personal_data_removed_ids_kept(self):
        fake_key = "sk-" + "ant-" + "abcdefghijk"  # built at runtime so the repo never contains a key-shaped string
        text = redact(f"mail jane.doe@example.com call (512) 555-0147 ssn 123-45-6789 key {fake_key} job gh:acme:4012345678")
        for secret in ("jane.doe", "555-0147", "123-45-6789", fake_key[:10]):
            self.assertNotIn(secret, text)
        self.assertIn("gh:acme:4012345678", text)

    def test_github_and_google_token_shapes_are_removed(self):
        # built at runtime so the repository never holds a token-shaped string
        tokens = ["ghp_" + "A1b2" * 9, "github_pat_" + "Z9_y" * 8, "ya29." + "a0Af-x" * 5, "1//" + "0gXy-Z_" * 5, "GOCSPX-" + "s3cr" * 6]
        text = redact("tokens: " + " ".join(tokens) + " see https://example.com//path and gh:acme:4012345678")
        for token in tokens:
            self.assertNotIn(token[6:18], text)
        self.assertIn("https://example.com//path", text)
        self.assertIn("gh:acme:4012345678", text)


class LockTests(unittest.TestCase):
    def test_second_writer_refused_and_stale_lock_held(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "writer.lock"
            with SingleWriterLock(path):
                with self.assertRaises(LockError) as ctx:
                    SingleWriterLock(path).acquire()
                self.assertIn("writer_already_running", str(ctx.exception))
            path.write_text(json.dumps({"pid": 999999, "nonce": "dead"}), encoding="utf-8")
            with self.assertRaises(LockError) as ctx:
                SingleWriterLock(path).acquire()
            self.assertIn("stale_lock_held", str(ctx.exception))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = new_store()
        self.job = add_job(self.store)

    def test_transition_writes_history_projection_and_audit(self):
        w = self.store.writer("discovery")
        self.clock.advance(seconds=1)
        w.transition(JOB_MATRIX, self.job, "normalized")
        job = self.store.one("SELECT * FROM jobs WHERE job_id = ?", (self.job,))
        self.assertEqual((job["current_state"], job["state_sequence"]), ("normalized", 1))
        self.assertEqual(self.store.verify_job_consistency(JOB_MATRIX, self.job), set())
        event = self.store.one("SELECT * FROM events WHERE transition_id = ?", (f"tr:{self.job}:1",))
        self.assertEqual((event["previous_state"], event["new_state"]), ("discovered", "normalized"))

    def test_illegal_transition_leaves_no_trace(self):
        with self.assertRaises(TransitionRejected):
            self.store.writer("qualify").transition(JOB_MATRIX, self.job, "scored")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM transitions")["c"], 0)

    def test_history_tables_are_immutable(self):
        self.store.writer("discovery").transition(JOB_MATRIX, self.job, "normalized")
        for sql in ("UPDATE transitions SET to_state = 'scored'", "DELETE FROM events", "UPDATE events SET message = 'x'"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.conn.execute(sql)

    def test_state_columns_only_via_transition(self):
        with self.assertRaises(PermissionDenied):
            self.store.writer("discovery").update("jobs", {"current_state": "scored"}, "job_id = ?", (self.job,))
        with self.assertRaises(sqlite3.IntegrityError):  # even raw SQL is blocked by the trigger
            self.store.conn.execute("UPDATE jobs SET current_state='scored', state_sequence=1 WHERE job_id=?", (self.job,))

    def test_separation_of_duties(self):
        with self.assertRaises(PermissionDenied):
            self.store.writer("qualify").insert("labels", {"label_id": "l", "job_id": self.job, "at": self.clock.now(), "label": "apply"})
        with self.assertRaises(PermissionDenied):
            self.store.writer("review").update("jobs", {"title": "x"}, "job_id = ?", (self.job,))
        with self.assertRaises(PermissionDenied):
            self.store.writer("extract").update("jobs", {"title": "x"}, "job_id = ?", (self.job,))

    def test_projection_mismatch_fails_closed(self):
        # Simulate a corrupt projection by inserting a transition row without updating the job.
        self.store.conn.execute(
            "INSERT INTO transitions (transition_id, contract_version, project_id, workflow_id, sequence, entity_type, entity_id, "
            "from_state, to_state, transitioned_at, trigger_type) VALUES ('rogue','0.2.0','careeros',?,1,'job',?,'discovered','normalized',?,'system')",
            (f"job:{self.job}", self.job, self.clock.now()))
        with self.assertRaises(DataQualityError):
            self.store.writer("discovery").transition(JOB_MATRIX, self.job, "normalized")
        self.assertIn("projection_mismatch", self.store.verify_job_consistency(JOB_MATRIX, self.job))

    def test_idempotency_receipts(self):
        w = self.store.writer("review")
        self.assertEqual(w.receipt("label", "k1", {"label": "apply"}), "new")
        self.assertEqual(w.receipt("label", "k1", {"label": "apply"}), "replay")
        with self.assertRaises(IdempotencyCollision):
            w.receipt("label", "k1", {"label": "skip"})

    def test_event_chain_detects_tampering(self):
        w = self.store.writer("discovery")
        w.event("job_found", outcome="success", entity_type="job", entity_id=self.job)
        w.event("job_found", outcome="success", entity_type="job", entity_id="other")
        self.assertEqual(self.store.verify_event_chain(), [])
        self.store.conn.execute("DROP TRIGGER events_no_update")
        self.store.conn.execute("UPDATE events SET message = 'tampered' WHERE seq = 1")
        self.assertTrue(self.store.verify_event_chain())

    def test_exception_record_and_event(self):
        exc = self.store.writer("discovery").exception("integration", "medium", "board fetch failed for user@example.com",
                                                       error_code="http_503", retryable=True)
        row = self.store.one("SELECT * FROM exceptions WHERE exception_id = ?", (exc,))
        self.assertEqual(row["status"], "open")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM events WHERE entity_id = ?", (exc,))["c"], 1)


if __name__ == "__main__":
    unittest.main()
