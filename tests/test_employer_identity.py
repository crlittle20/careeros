"""Employer identity resolver (R1, 2026-10-16): not wired into anything yet. Synthetic names only: the fictional
Northgate Group family in tests/fixtures/synthetic_overrides.yaml mirrors a real one alias for alias, and HELD below
mirrors its four held names."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from careeros.core.policy import load_policy
from careeros.normalize.employer_identity import (CONFLICTING, RESOLVED, UNRESOLVED, Match, in_families, legal_norm, resolve,
                                                  resolve_job)
from careeros.paths import CONFIG_DIR, ENGINE_ROOT
from careeros.qualify.gate import GateResult, held_rule

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", FIXTURES / "synthetic_overrides.yaml")
PARENTS = POLICY.get("parents")
FAMILY = "Northgate Group"
ALIASES = PARENTS[FAMILY]
HELD = ["Northgate Group", "Northgate", "Bridgewell Claims", "NorthgateCare"]
UNION = "Northgate Care Workers East"          # a different organisation whose name starts with an alias
GUARD = ["Northgate Care Workers"]


class HeldNamesAndAliasesTests(unittest.TestCase):
    def test_every_alias_resolves_to_the_family_and_its_holds(self):
        self.assertEqual(len(ALIASES), 10)
        for alias in ALIASES:
            found = resolve(alias, None, PARENTS)
            self.assertEqual((found.status, found.family, found.known_family, found.evidence[0].rule), (RESOLVED, FAMILY, True, "exact"), alias)
            self.assertTrue(in_families(found, HELD, PARENTS), alias)

    def test_the_four_held_names_resolve_as_employer_or_as_parent(self):
        for name in HELD:
            self.assertEqual(resolve(name, name, PARENTS).family, FAMILY, name)
            self.assertEqual(resolve(None, name, PARENTS).family, FAMILY, name)

    def test_case_punctuation_and_legal_names(self):
        cases = {"NORTHGATE RX, INC.": ("exact", "northgate rx"), "Northgate-Rx": ("exact", "northgate rx"),
                 "northgate.rx llc": ("exact", "northgate rx"), "NorthgateRx, Inc.": ("exact", "northgaterx"),
                 "NorthgateInsight, Inc.": ("compact", "northgate insight"), "The Northgate Group, Incorporated": ("exact", "Northgate Group"),
                 "Northgate Health & Wellness Holdings LLC": ("prefix", "northgate health"),
                 "Bridgewell Claims Technologies, L.L.C.": ("prefix", "bridgewell claims"),
                 "NorthgateCare Services, Inc.": ("prefix", "northgatecare"), "Northgate Care Community Plan": ("prefix", "northgate care")}
        for name, (rule, via) in cases.items():
            found = resolve(name, None, PARENTS)
            self.assertEqual((found.family, found.evidence[0].rule, found.evidence[0].via), (FAMILY, rule, via), name)

    def test_legal_name_normalisation(self):
        self.assertEqual(legal_norm("The Northgate Rx, Inc."), "northgate rx")
        self.assertEqual((legal_norm("Company"), legal_norm("The"), legal_norm(None)), ("", "", ""))


class NegativeTests(unittest.TestCase):
    def test_a_listed_different_organisation_is_never_the_family(self):
        self.assertEqual(resolve(UNION, None, PARENTS).family, FAMILY)   # without the guard the alias prefix would claim it
        found = resolve(UNION, None, PARENTS, different=GUARD)
        self.assertEqual((found.status, found.family, found.known_family, found.evidence[0].rule),
                         (RESOLVED, UNION, False, "different_organisation"))
        self.assertFalse(in_families(found, HELD, PARENTS, different=GUARD))

    def test_the_most_specific_match_wins_and_a_tie_is_never_settled_by_order(self):
        """2026-10-17 (fresh-context review): a short guard no longer shadows a longer alias, one alias in two families is
        a contradiction, a listed agency stays an agency, and the guard matches its compact form too."""
        short = ["Northgate"]
        self.assertEqual(resolve("Northgate Rx Specialty", None, PARENTS, different=short).family, FAMILY)   # longer alias
        tie = resolve("Northgate Logistics", None, PARENTS, different=short)
        self.assertEqual((tie.status, tie.evidence[0].rule), (CONFLICTING, "ambiguous"))
        both = {**PARENTS, "Alder Partners": ["Northgate Rx"]}
        found = resolve("Northgate Rx", None, both)
        self.assertEqual((found.status, found.reason), (CONFLICTING, "ambiguous_name"))
        self.assertIsNone(in_families(found, HELD, both))
        agency = resolve("Northgate Staffing Partners", None, PARENTS, agencies=["Northgate Staffing"])
        self.assertEqual((agency.status, agency.evidence[0].rule), (UNRESOLVED, "agency"))
        glued = resolve("NorthgateCare Workers East", None, PARENTS, different=GUARD)
        self.assertEqual((glued.family, glued.evidence[0].rule), ("NorthgateCare Workers East", "different_organisation"))

    def test_board_aliases_respect_the_guard_and_never_pick_between_two_families(self):
        union = resolve("ncwe", None, PARENTS, board_aliases={"ncwe": "Northgate Care Workers East"}, different=GUARD)
        self.assertNotEqual(union.family, FAMILY)
        two = {**PARENTS, "Alder Partners": []}
        found = resolve("nhg", None, two, board_aliases={"nhg": ["Alder Partners", "Northgate Rx"]})
        self.assertEqual((found.status, found.evidence[0].rule), (CONFLICTING, "ambiguous"))

    def test_accents_and_full_width_letters_are_folded(self):
        self.assertEqual(legal_norm("Nórdica Health"), "nordica health")
        self.assertEqual(legal_norm("\uff2eorthgate Rx"), "northgate rx")
        self.assertEqual(resolve("\uff2eorthgate Rx", None, PARENTS).family, FAMILY)

    def test_staffing_recruiting_and_talent_names_need_identity_review(self):
        """The owner's rule (2026-10-18): a signal for review, not proof of an agency; submission stays blocked while the
        hiring employer is unresolved, and a family named by the stored parent still decides it."""
        for employer, parent in (("Harborline Talent", None), ("Harborline Talent", "Harborline Holdings"),
                                 ("Crestway Recruiting Group", "Crestway Recruiting Group"), (None, "Pinefield Staffing")):
            found = resolve(employer, parent, PARENTS)
            self.assertEqual((found.status, found.reason), (UNRESOLVED, "possible_agency_name_needs_review"), (employer, parent))
        self.assertEqual(resolve("Harborline Talent", "Northgate Group", PARENTS).family, FAMILY)    # the client is named
        client = resolve("Acme Health", "Crestway Recruiting Group", PARENTS)
        self.assertEqual((client.status, client.family), (RESOLVED, "Acme Health"))
        confirmed = resolve("Brightwater Talent Health", None, {**PARENTS, "Brightwater Talent Health": []})
        self.assertEqual((confirmed.status, confirmed.known_family), (RESOLVED, True))            # listed: confirmed
        for name in ("Talentia Labs", "Restaffingly Health"):
            self.assertEqual(resolve(name, None, PARENTS).status, RESOLVED, name)                  # whole words only

    def test_lookalike_names_do_not_match(self):
        for name in ("Northgait Healthcare", "Northgold Health", "Southnorthgate Labs", "Bridgewellness Partners",
                     "Northgateway Logistics"):
            found = resolve(name, None, PARENTS)
            self.assertEqual((found.status, found.known_family, found.evidence[0].rule), (RESOLVED, False, "own"), name)
            self.assertFalse(in_families(found, HELD, PARENTS), name)

    def test_another_family_is_not_this_one(self):
        found = resolve("Aetna", None, PARENTS)          # a member of a generic family in the engine defaults
        self.assertEqual(found.family, "CVS Health")
        self.assertFalse(in_families(found, HELD, PARENTS))


class StoredParentTests(unittest.TestCase):
    def test_missing_parent_uses_the_employer_name(self):
        for parent in (None, "", "   "):
            found = resolve("Northgate Rx", parent, PARENTS)
            self.assertEqual((found.family, found.reason), (FAMILY, "employer_name_exact"), repr(parent))

    def test_a_parent_copied_from_the_employer_name_still_resolves(self):
        self.assertEqual(resolve("NorthgateRx", "NorthgateRx", PARENTS).family, FAMILY)   # board registration copies the name

    def test_a_subsidiary_under_a_family_parent_is_that_family(self):
        found = resolve("Brightpath Analytics", "Northgate Group", PARENTS)
        self.assertEqual((found.status, found.family, found.reason), (RESOLVED, FAMILY, "parent_exact"))

    def test_conflicting_parents_are_never_either_employer(self):
        cases = {("Northgate", "CVS Health"): "names_point_to_different_families",
                 ("Northgate Rx", "Acme Health"): "parent_names_a_different_employer"}
        for (employer, parent), reason in cases.items():
            found = resolve(employer, parent, PARENTS)
            self.assertEqual((found.status, found.family, found.reason), (CONFLICTING, None, reason))
            self.assertIsNone(in_families(found, HELD, PARENTS))

    def test_a_stale_prefix_parent_on_a_different_organisation_conflicts(self):
        found = resolve(UNION, FAMILY, PARENTS, different=GUARD)
        self.assertEqual((found.status, found.reason), (CONFLICTING, "employer_name_is_a_different_organisation"))

    def test_unknown_employers_take_the_stored_parent(self):
        found = resolve("Acme Pharmacy", "Acme Holdings Inc", PARENTS)
        self.assertEqual((found.status, found.family, found.known_family, found.key, found.reason),
                         (RESOLVED, "Acme Holdings Inc", False, "acme", "parent_own_name"))


class UnresolvedEmployerTests(unittest.TestCase):
    def test_confidential_clients_are_unresolved(self):
        for name in ("Confidential", "Confidential Healthcare Client", "Undisclosed Fortune 500 Company", "Anonymous Health Plan",
                     "Hiring for a client of ours"):
            found = resolve(name, name, PARENTS)
            self.assertEqual((found.status, found.family, found.reason), (UNRESOLVED, None, "confidential_client"), name)
            self.assertIsNone(in_families(found, HELD, PARENTS), name)
        self.assertEqual(resolve("Confidential", "Acme Health", PARENTS).status, UNRESOLVED)   # an unmatched parent doesn't name it
        self.assertEqual(resolve("Confidential", FAMILY, PARENTS).family, FAMILY)               # a family parent does

    def test_no_usable_name(self):
        for employer, parent in ((None, None), ("", "  "), ("Company", None), ("The", "Inc.")):
            self.assertEqual(resolve(employer, parent, PARENTS).reason, "no_employer_name", (employer, parent))

    def test_agency_postings_without_a_named_client(self):
        found = resolve_job({"employer_name": "TEKsystems", "parent": "TEKsystems"}, POLICY)   # an agency in the defaults
        self.assertEqual((found.status, found.reason, found.evidence[0].rule), (UNRESOLVED, "agency_client_not_named", "agency"))
        self.assertEqual(resolve_job({"employer_name": "Harbor Staffing"}, POLICY).reason, "possible_agency_name_needs_review")   # private agencies come from
        self.assertEqual(resolve_job({"employer_name": "Harbor Staffing"}, POLICY, agencies=["Harbor Staffing"]).reason,   # the caller
                         "agency_client_not_named")

    def test_an_agency_posting_that_names_its_client(self):
        found = resolve_job({"employer_name": "TEKsystems", "parent": FAMILY}, POLICY)
        self.assertEqual((found.status, found.family, [m.rule for m in found.evidence]), (RESOLVED, FAMILY, ["agency", "exact"]))


class DeterminismAndEvidenceTests(unittest.TestCase):
    def test_same_answer_and_evidence_every_time_whatever_the_policy_order(self):
        first = resolve("Northgate Health & Wellness LLC", None, PARENTS)
        self.assertEqual(first, resolve("Northgate Health & Wellness LLC", None, PARENTS))
        reordered = dict(reversed(list(PARENTS.items())))
        self.assertEqual(first, resolve("Northgate Health & Wellness LLC", None, reordered))
        self.assertEqual(first.evidence, (Match("employer_name", "Northgate Health & Wellness LLC", "northgate health and wellness", "prefix",
                                                FAMILY, "northgate health"),
                                          Match("parent", "", "", "empty", None, None)))

    def test_the_same_evidence_under_any_hash_seed(self):
        """Set iteration order changes with PYTHONHASHSEED; the evidence must not (a tie once left the alias to it)."""
        script = ("import json; from tests.test_employer_identity import ALIASES, HELD, PARENTS, resolve; "
                  "print(json.dumps([[m.__dict__ for m in resolve(n, n, PARENTS).evidence] for n in [*ALIASES, *HELD, 'The Northgate Group']]))")
        outputs = {subprocess.run([sys.executable, "-c", script], cwd=ENGINE_ROOT, capture_output=True, text=True, check=True,
                                  env={**os.environ, "PYTHONHASHSEED": str(seed)}).stdout for seed in range(1, 9)}
        self.assertEqual(len(outputs), 1)

    def test_spellings_that_normalize_alike_keep_one_order_under_any_hash_seed(self):
        """They tie on the alias and the family, so only the spelling orders them; the test data above has no such tie,
        which let a key without the spelling survive the 2026-10-18 full run."""
        script = ("from careeros.normalize.employer_identity import _aliases; "
                  "print('|'.join(a[2] for a in _aliases({'Northgate Health': ['Northgate Rx', 'NORTHGATE RX', 'northgate rx']}) "
                  "if a[0] == 'northgate rx'))")
        outputs = {subprocess.run([sys.executable, "-c", script], cwd=ENGINE_ROOT, capture_output=True, text=True, check=True,
                                  env={**os.environ, "PYTHONHASHSEED": str(seed)}).stdout.strip() for seed in range(1, 9)}
        self.assertEqual(outputs, {"NORTHGATE RX|Northgate Rx|northgate rx"})

    def test_board_aliases_resolve_through_the_family(self):
        found = resolve("NHG Pharmacy Services", None, PARENTS, board_aliases={"nhg pharmacy services": "northgate rx"})
        self.assertEqual((found.family, found.evidence[0].rule, found.evidence[0].via), (FAMILY, "board_alias", "nhg pharmacy services -> northgate rx"))


class NotInQualificationTests(unittest.TestCase):
    def test_only_submission_permission_and_night_conflicts_use_the_resolver(self):
        """Submission holds (R2) and NIGHT's same-employer-family check (R3) use it; the gate, scores and discovery don't."""
        uses = re.compile(r"normalize\.employer_identity|normalize import employer_identity|from \.employer_identity")
        users = [p.relative_to(ENGINE_ROOT).as_posix() for p in (ENGINE_ROOT / "careeros").rglob("*.py")
                 if p.name != "employer_identity.py" and uses.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(users, ["careeros/qualify/night.py", "careeros/review/submission.py"])

    def test_the_existing_hold_rule_is_unchanged(self):
        """Today's gate compares exact names (changed only in a later, approved step): an alias-only name isn't held there,
        although the resolver places it in the family."""
        def flagged(employer, parent):
            result = GateResult("eligible")
            held_rule(result, {"employer_name": employer, "parent": parent}, POLICY)
            return result.has_flag("held_d13")
        self.assertFalse(flagged("NorthgateRx", "NorthgateRx"))
        self.assertTrue(in_families(resolve("NorthgateRx", "NorthgateRx", PARENTS), HELD, PARENTS))
        self.assertTrue(flagged("Northgate Rx", FAMILY))


class CodexReviewTests(unittest.TestCase):
    """Independent Codex review of f984d15 (2026-10-20): a known agency must never turn into a hiring employer."""
    AGENCY = "Harborline Staffing"          # fictional

    def test_a_board_alias_to_a_known_agency_keeps_the_agency(self):
        found = resolve("HS", "HS", PARENTS, agencies=[self.AGENCY], board_aliases={"hs": self.AGENCY})
        self.assertEqual((found.status, found.reason, found.evidence[0].rule), (UNRESOLVED, "agency_client_not_named", "agency"))

    def test_a_board_alias_to_a_family_and_an_agency_is_ambiguous(self):
        mixed = resolve("HS", None, PARENTS, agencies=[self.AGENCY], board_aliases={"hs": [self.AGENCY, FAMILY]})
        self.assertEqual((mixed.status, mixed.reason), (CONFLICTING, "ambiguous_name"))

    def test_a_listed_agency_under_its_own_corporate_parent_names_no_client(self):
        found = resolve(self.AGENCY, "Brightpath Holdings", PARENTS, agencies=[self.AGENCY])
        self.assertEqual((found.status, found.reason), (UNRESOLVED, "agency_client_not_named"))
        self.assertEqual(resolve(self.AGENCY, FAMILY, PARENTS, agencies=[self.AGENCY]).family, FAMILY)   # a family parent still decides

if __name__ == "__main__":
    unittest.main()
