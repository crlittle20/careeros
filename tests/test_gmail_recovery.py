"""Gmail API and IMAP search overflow recovery. All tests use synthetic IDs; no network."""
import re
import unittest
from datetime import datetime

from careeros.discovery.gmail import GmailError
from careeros.discovery.gmail_recovery import search_all

NOW = "2026-11-08T12:00:00.000Z"
NOW_SECONDS = int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp())


class FakeReader:
    def __init__(self, messages, cap=2):
        self.messages = messages
        self.cap = cap
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        start = re.search(r"(?:^|\s)after:(\d+)", query)
        stop = re.search(r"(?:^|\s)before:(\d+)", query)
        ids = [
            msgid for timestamp, msgid in self.messages
            if (not start or timestamp > int(start.group(1)))
            and (not stop or timestamp < int(stop.group(1)))
        ]
        if len(ids) > self.cap:
            raise GmailError("search_limit_reached")
        return ids


class SearchRecoveryTests(unittest.TestCase):
    def test_normal_search_uses_original_authorized_query_once(self):
        reader = FakeReader([(NOW_SECONDS - 100, "one")])
        self.assertEqual(search_all(reader, "newer_than:3d from:alerts", NOW), ["one"])
        self.assertEqual(reader.calls, ["newer_than:3d from:alerts"])

    def test_overflow_recovers_all_messages_without_duplicate_boundaries(self):
        reader = FakeReader([
            (NOW_SECONDS - 220000, "oldest"),
            (NOW_SECONDS - 100000, "middle"),
            (NOW_SECONDS - 3600, "newest"),
        ])
        self.assertEqual(
            set(search_all(reader, "newer_than:3d from:alerts", NOW)),
            {"oldest", "middle", "newest"},
        )
        self.assertGreater(len(reader.calls), 2)
        self.assertTrue(all("from:alerts" in q for q in reader.calls))
        self.assertTrue(all("newer_than:" not in q for q in reader.calls[1:]))

    def test_more_than_limit_in_one_minute_fails_closed(self):
        reader = FakeReader([
            (NOW_SECONDS - 12, "a"),
            (NOW_SECONDS - 11, "b"),
            (NOW_SECONDS - 10, "c"),
        ])
        with self.assertRaises(GmailError) as caught:
            search_all(reader, "newer_than:3d from:alerts", NOW)
        self.assertEqual(caught.exception.code, "recovery_window_overflow")

    def test_conflicting_or_unsupported_window_is_not_guessed(self):
        for query in ("from:alerts", "newer_than:3d after:123 from:alerts", "newer_than:65d from:alerts"):
            with self.subTest(query=query):
                reader = FakeReader([(NOW_SECONDS - 12, "a"), (NOW_SECONDS - 11, "b"),
                                     (NOW_SECONDS - 10, "c")])
                with self.assertRaises(GmailError):
                    search_all(reader, query, NOW)

    def test_other_gmail_errors_are_not_swallowed(self):
        class BrokenReader:
            def search(self, query):
                raise GmailError("imap_search_failed")
        with self.assertRaises(GmailError) as caught:
            search_all(BrokenReader(), "newer_than:3d from:alerts", NOW)
        self.assertEqual(caught.exception.code, "imap_search_failed")


if __name__ == "__main__":
    unittest.main()
