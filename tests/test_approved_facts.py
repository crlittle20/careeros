"""Approved facts (R6, 2026-10-16): drafts quote them word for word and check their own wording for a figure turned into
a daily rate, a guarantee or ownership, or an old figure used again. Synthetic facts only."""
import tempfile
import unittest
from pathlib import Path

from careeros.review import approved_facts as af
from careeros.review.labels import record_label
from careeros.review.shortlist import draft

from tests.helpers import add_job, new_store
from tests.test_qualify import PROFILE

FACT = {"id": "ticket-volume", "text": "Up to approximately 5,000 tickets within a two-week cycle.",
        "applies_to": ["ticket triage"], "superseded": ["3,500"], "source": "synthetic"}


class CheckTests(unittest.TestCase):
    def kinds(self, text):
        return [i["kind"] for i in af.check(text, [FACT])]

    def test_the_exact_wording_and_unrelated_numbers_pass(self):
        self.assertEqual(self.kinds("Up to approximately 5,000 tickets within a two-week cycle."), [])
        self.assertEqual(self.kinds("Three years of triage across 2 queues."), [])

    def test_distortions_of_the_figure_are_reported(self):
        cases = {"Triaged 5,000 tickets per day.": "a daily rate", "I handled 5k tickets daily.": "a daily rate",
                 "Guaranteed resolution of 5,000 tickets.": "a guarantee", "Consistently closed 5 thousand tickets.": "a guarantee",
                 "Led the triage of 5,000 tickets.": "ownership the fact doesn't state",
                 "Owned a queue of 5,000 tickets.": "ownership the fact doesn't state"}
        for text, kind in cases.items():
            self.assertEqual(self.kinds(text), [kind], text)

    def test_an_old_figure_is_reported_in_any_form(self):
        self.assertEqual(self.kinds("Handled 3,500 tickets every two weeks."), ["a superseded figure"])
        self.assertEqual(self.kinds("Handled 3.5k tickets every two weeks."), ["a superseded figure"])

    def test_the_common_distortions_the_review_found(self):
        """Fresh-context Claude review of 4486b82 (2026-10-18): each of these was missed before."""
        cases = {"Triaged 5,000/day.": "a daily rate", "Closed 5k tickets/day.": "a daily rate",
                 "Cleared 5,000 tickets each shift.": "a daily rate", "Handled 5,000 tickets per business day.": "a daily rate",
                 "Closed 5,000 tickets every workday.": "a daily rate",
                 "Responsible for 5,000 tickets.": "ownership the fact doesn't state",
                 "Managing 5,000 tickets.": "ownership the fact doesn't state",
                 "Spearheaded triage of 5,000 tickets.": "ownership the fact doesn't state",
                 "Guaranteeing 5,000 tickets.": "a guarantee", "Ensured 5,000 tickets were closed.": "a guarantee",
                 "Triaged five thousand tickets daily.": "a daily rate", "Triaged 5 000 tickets daily.": "a daily rate",
                 "Triaged 5,000\ntickets per day.": "a daily rate",
                 # a longer continuation is never read as a short next sentence: only joining the wrapped line finds it
                 "Triaged 5,000\ntickets across the whole regional queue per day.": "a daily rate"}
        for text, kind in cases.items():
            self.assertIn(kind, self.kinds(text), text)

    def test_words_beside_the_quote_are_read_with_it(self):
        self.assertEqual(self.kinds(f'I guaranteed "{FACT["text"]}" every day.'), ["a daily rate", "a guarantee"])
        self.assertEqual(self.kinds(f'Approved wording: "{FACT["text"]}"'), [])
        far = f'"{FACT["text"]}", and over the following quarters the whole regional team also handled 5,000 tickets per day.'
        self.assertEqual(self.kinds(far), ["a daily rate"])   # the quote never hides a reworded figure in its sentence

    def test_a_qualifier_left_alone_in_a_short_sentence_is_read_with_the_figure(self):
        """Independent Codex review of f984d15 (2026-10-20): "Daily." on its own was never read with the figure."""
        self.assertEqual(self.kinds("Approx. 5,000 tickets. Daily."), ["a daily rate"])
        self.assertEqual(self.kinds("Triaged 5,000 tickets. Guaranteed."), ["a guarantee"])
        self.assertEqual(self.kinds("Triaged 5,000 tickets. Daily standups kept the whole queue moving."), [])

    def test_no_false_alarm_on_approved_text_years_or_phone_numbers(self):
        abbreviated = {**FACT, "id": "abbr", "text": "Approx. 5,000 tickets per cycle at Acme Inc. within two weeks."}
        self.assertEqual(af.check(f'- "{abbreviated["text"]}"', [abbreviated]), [])
        second = {"id": "team", "text": "Led a team that closed 5,000 claims.", "applies_to": [], "superseded": [], "source": "s"}
        self.assertEqual(af.check(f'- "{FACT["text"]}"\n- "{second["text"]}"', [FACT, second]), [])
        self.assertEqual(self.kinds("Call (303) 555-3500 about the 2023-2025 role."), [])
        joined = {**FACT, "id": "joined", "superseded": ["2,000"]}
        self.assertEqual(af.check("Joined the team in 2000 and kept learning.", [joined]), [])          # a year, not a figure
        percent = {"id": "q", "text": "Kept a 98% quality score.", "applies_to": [], "superseded": [], "source": "s"}
        self.assertEqual([i["kind"] for i in af.check("I always kept a 98% quality score.", [percent])], ["a guarantee"])

    def test_field_types_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.yaml"
            for bad in ("applies_to: ticket triage", "superseded: 3500", "superseded: '3,500'"):
                path.write_text(f"- id: x\n  text: Some fact.\n  {bad}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "approved_fact_invalid"):
                    af.load(path)
            path.write_text("- id: x\n  text: Some fact.\n  applies_to: [' Ticket Triage ']\n  superseded: [3500]\n", encoding="utf-8")
            self.assertEqual(af.load(path)[0]["applies_to"], ["ticket triage"])

    def test_the_file_is_optional_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(af.load(Path(tmp) / "missing.yaml"), [])
            path = Path(tmp) / "facts.yaml"
            path.write_text("- {id: x, text: Up to 10 things., applies_to: [Ticket Triage]}\n", encoding="utf-8")
            self.assertEqual(af.load(path)[0]["applies_to"], ["ticket triage"])
            path.write_text("- {id: x}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                af.load(path)


class DraftTests(unittest.TestCase):
    def build(self, source):
        store, _clock = new_store()
        add_job(store, "gh:t:1", current_state="scored", description_text="Requirements:\n- Ticket triage experience required")
        store.writer("extract").update("jobs", {"extraction_fingerprint": "e1"}, "job_id = ?", ("gh:t:1",))
        store.writer("extract").insert("requirements", {"req_id": "r1", "job_id": "gh:t:1", "extraction_fingerprint": "e1",
                                                        "extractor": "rules-test", "created_at": store.clock.now(),
                                                        "category": "experience", "name": "ticket triage",
                                                        "requirement_type": "hard_required", "source_text": "Ticket triage experience required",
                                                        "confidence": 0.9, "years": None})
        record_label(store, "gh:t:1", "apply", None, "l1")
        from careeros.qualify.profile import Profile
        profile = Profile({**PROFILE.data, "functions": [{"name": "ticket triage", "years": 2, "evidence": "direct", "source": source}]}, "p")
        with tempfile.TemporaryDirectory() as tmp:
            return draft(store, profile, "gh:t:1", Path(tmp), facts=[FACT]).read_text(encoding="utf-8")

    def test_the_fact_is_quoted_word_for_word_and_the_wording_checked(self):
        text = self.build("helpdesk team")
        self.assertIn('Approved wording: "Up to approximately 5,000 tickets within a two-week cycle."', text)
        self.assertIn('## Approved facts (quote them exactly)\n\n- "Up to approximately 5,000 tickets within a two-week cycle."', text)
        self.assertIn("- no figure from your approved facts is reworded above", text)

    def test_a_reworded_figure_is_called_out(self):
        text = self.build("triaged 5,000 tickets per day")
        self.assertIn("## Wording check", text)
        self.assertIn("a daily rate ('per day')", text)


if __name__ == "__main__":
    unittest.main()
