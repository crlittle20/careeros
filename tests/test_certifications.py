"""Certification requirements (2026-10-15): extraction, normalisation, issuer and family, requirement strength, your
status, the match, the decision effect, explanations and demand. All data is synthetic."""
import json
import unittest

import yaml

from careeros.demo import world
from careeros.extract.rules import certification_mentions, extract
from careeros.qualify import certifications as certs
from careeros.qualify.gate import evaluate
from careeros.qualify.profile import Profile
from careeros.qualify.scores import score
from careeros.review.certification_demand import demand

from tests import postings
from tests.helpers import add_job, new_store
from tests.test_qualify import NOW, POLICY, PROFILE, job

CATALOG = POLICY.get("certifications")
TITLE = "Senior Business Systems Analyst - Pharmacy Benefits"


def mentions(line, section="required"):
    from careeros.extract.rules import _requirement_type
    return certification_mentions(line, _requirement_type(section, line), section, CATALOG)


def person(*items):
    return Profile({**PROFILE.data, "certifications": [dict(i) for i in items]}, "cert-test")


def records(text, profile=PROFILE):
    return certs.analyse(extract(text, POLICY).requirements, profile, POLICY)


def posting_with(*lines):
    return postings.GOOD_FIT.replace("- CBAP certification preferred\n", "").replace(
        "- Experience with RxClaim or another PBM adjudication platform",
        "- Experience with RxClaim or another PBM adjudication platform\n" + "\n".join(f"- {line}" for line in lines))


def judged(profile, *lines):
    text = posting_with(*lines)
    j = job(TITLE, text)
    reqs = extract(text, POLICY).requirements
    return evaluate(j, reqs, profile, POLICY), reqs, j


class ExtractionTests(unittest.TestCase):
    def test_helpful_coding_certification_is_preferred_not_a_hard_requirement(self):
        line = "Experience or certification in coding/billing is helpful."
        self.assertEqual(mentions(line), [("unspecified certification", "preferred")])
        self.assertEqual(mentions("PMP required"), [("pmp", "hard_required")])
        self.assertEqual(mentions("PMP or equivalent experience required"),
                         [("pmp", "required_with_equivalency")])

    def test_named_credentials_and_their_strength(self):
        self.assertEqual(mentions("PMP required"), [("pmp", "hard_required")])
        self.assertEqual(mentions("PMP preferred"), [("pmp", "preferred")])
        self.assertEqual(mentions("Must hold PMP"), [("pmp", "hard_required")])
        self.assertEqual(mentions("PMP certification a plus"), [("pmp", "preferred")])

    def test_aliases_normalise_to_one_identity(self):
        for line in ("PL-300 certification preferred", "PL300 preferred", "Microsoft PL-300 preferred",
                     "Power BI Data Analyst certification preferred", "Microsoft Power BI Data Analyst certification preferred"):
            self.assertEqual(mentions(line), [("pl-300", "preferred")], line)
        for line in ("PMP preferred", "PMI PMP preferred", "Project Management Professional preferred"):
            self.assertEqual(mentions(line), [("pmp", "preferred")], line)
        for line in ("PSM I preferred", "PSM1 preferred", "Professional Scrum Master I preferred"):
            self.assertEqual(mentions(line), [("psm-i", "preferred")], line)
        self.assertEqual(certs.describe("pl-300", POLICY)["name"], "Microsoft PL-300 Power BI Data Analyst")

    def test_a_family_is_not_guessed_into_a_credential(self):
        self.assertEqual(mentions("Scrum certification preferred"), [("family:scrum", "preferred")])
        described = certs.describe("family:scrum", POLICY)
        self.assertEqual((described["name"], described["issuer"], described["family"]), ("Scrum certification", None, "scrum"))

    def test_issuers_only_where_the_credential_or_posting_fixes_them(self):
        self.assertEqual(certs.describe("pmp", POLICY)["issuer"], "PMI")
        self.assertIsNone(certs.describe("lss-green-belt", POLICY)["issuer"])   # many bodies award belts
        self.assertEqual(mentions("Current certification through AHIMA and/or AAPC required"),
                         [("issuer:aapc", "hard_required"), ("issuer:ahima", "hard_required")])
        self.assertEqual(certs.describe("issuer:aapc", POLICY)["family"], "medical_coding")

    def test_equivalency_is_kept(self):
        self.assertEqual(mentions("PMP or equivalent certification required"), [("pmp", "required_with_equivalency")])
        self.assertEqual(mentions("Certified Risk Adjustment Coder (CRC) or similar certification"), [("crc", "required_with_equivalency")])
        self.assertEqual(mentions("Advanced degree in a quantitative field, or an ASA credential."), [("asa", "required_with_equivalency")])
        self.assertEqual(mentions("Agile experience and/or certification"), [("family:agile", "required_with_equivalency")])

    def test_several_certifications_stay_separate(self):
        self.assertEqual(mentions("PMP, Scrum, or SAFe preferred"),
                         [("pmp", "preferred"), ("family:scrum", "preferred"), ("family:safe", "preferred")])
        self.assertEqual(mentions("PMP preferred. Scrum certification or SAFe certification strongly desired. CBAP required."),
                         [("pmp", "preferred"), ("family:scrum", "preferred"), ("family:safe", "preferred"), ("cbap", "hard_required")])
        self.assertEqual(mentions("Six Sigma Black Belt, CPC, CCS, or CFE certification"),
                         [("lss-black-belt", "hard_required"), ("cpc", "hard_required"), ("ccs", "hard_required"), ("cfe", "hard_required")])

    def test_examples_make_one_family_requirement(self):
        self.assertEqual(mentions("Project management certification (e.g., PRINCE2, PMP)"), [("family:project_management", "hard_required")])
        self.assertEqual(mentions("Coding certification such as CPC or CCS required"), [("family:medical_coding", "hard_required")])

    def test_unknown_certifications_are_kept_as_written(self):
        reqs = extract("Requirements:\n- Acmeworks Platform Analyst certification required", POLICY).requirements
        record = certs.analyse(reqs, PROFILE, POLICY)[0]
        self.assertEqual((record["certification_key"], record["kind"], record["match_type"], record["candidate_status"]),
                         ("unspecified certification", "unspecified", "unknown", "unknown"))
        self.assertEqual(record["raw_text"], "- Acmeworks Platform Analyst certification required")

    def test_licences_and_degrees_stay_separate(self):
        reqs = extract("Requirements:\n- Active RN license required\n- Bachelor's degree required\n- PMP required", POLICY).requirements
        self.assertEqual(sorted({r.category for r in reqs if r.category in ("license", "education", "certification")}),
                         ["certification", "education", "license"])
        self.assertEqual([r.name for r in reqs if r.category == "certification"], ["pmp"])

    def test_the_ai_readers_free_text_names_normalise_too(self):
        self.assertEqual(certs.describe("Project Management Professional", POLICY)["key"], "pmp")
        self.assertEqual(certs.describe("lean six sigma", POLICY)["key"], "family:lean_six_sigma")


class FalsePositiveTests(unittest.TestCase):
    def test_wording_that_asks_for_no_credential(self):
        for line in ("Please provide a certified copy of your transcript", "Responsible for certification of records",
                     "Work with our certified data team", "Board certified physicians review cases",
                     "Final pay is based on several factors including education, work experience, certifications, etc.",
                     "Example Health is certified as a Great Place to Work", "Reset login credentials for users",
                     "Experience with ITIL processes", "Our CSM team partners with clients", "Install TLS certificates on servers",
                     "Learning quickly matters more to us than any one credential"):
            self.assertEqual(mentions(line), [], line)

    def test_training_after_hire_is_not_a_requirement(self):
        self.assertEqual(mentions("Obtain the platform certification within 90 days"), [("unspecified certification", "responsibility")])
        gate, _reqs, _j = judged(PROFILE, "Obtain the platform certification within 90 days")
        self.assertNotIn("certification_check", {f["code"] for f in gate.flags})

    def test_not_required_is_not_a_requirement(self):
        self.assertEqual(mentions("Coding credentials help, though certification is not required."),
                         [("unspecified certification", "preferred")])
        gate, _reqs, _j = judged(PROFILE, "Coding credentials help, though certification is not required.")
        self.assertNotIn("certification_check", {f["code"] for f in gate.flags})


class MatchingTests(unittest.TestCase):
    def test_required_and_held_exactly(self):
        gate, reqs, _ = judged(person({"name": "PMP", "status": "held"}), "PMP required")
        record = certs.analyse(reqs, person({"name": "PMP", "status": "held"}), POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["match_type"], record["satisfied"], record["decision_effect"]),
                         ("held", "exact", True, "requirement_satisfied"))
        self.assertNotIn("certification_required", {f["code"] for f in gate.hard_failures})

    def test_a_related_credential_never_satisfies_a_named_one(self):
        capm = person({"name": "Certified Associate in Project Management (CAPM)", "status": "held"})
        gate, reqs, _ = judged(capm, "PMP required")
        record = certs.analyse(reqs, capm, POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["match_type"], record["satisfied"], record["decision_effect"]),
                         ("not_held", "related", False, "hard_failure"))
        failure = next(f for f in gate.hard_failures if f["code"] == "certification_required")
        self.assertIn("related, not counted", failure["detail"])

    def test_a_planned_credential_is_never_held(self):
        planned = person({"name": "PL-300", "status": "planned"})
        gate, reqs, _ = judged(planned, "PL-300 certification required")
        record = certs.analyse(reqs, planned, POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["match_type"], record["satisfied"]), ("planned", "exact", False))
        failure = next(f for f in gate.hard_failures if f["code"] == "certification_required")
        self.assertIn("yours is planned, not counted as held", failure["detail"])
        self.assertFalse(planned.has_certification("pl-300"))
        in_progress = person({"name": "PL-300", "status": "in_progress"})
        self.assertEqual(certs.analyse(reqs, in_progress, POLICY)[-1]["candidate_status"], "in_progress")
        self.assertFalse(certs.analyse(reqs, in_progress, POLICY)[-1]["satisfied"])

    def test_a_missing_status_is_unknown_never_held(self):
        unrecorded = person({"name": "PMP"})
        gate, reqs, _ = judged(unrecorded, "PMP required")
        record = certs.analyse(reqs, unrecorded, POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["satisfied"]), ("unknown", False))
        failure = next(f for f in gate.hard_failures if f["code"] == "certification_required")
        self.assertIn("yours is without a recorded status, not counted as held", failure["detail"])
        self.assertFalse(unrecorded.has_certification("pmp"))
        self.assertEqual(certs.profile_warnings(unrecorded, POLICY), ["PMP: no status recorded (counted as unknown, not held)"])
        self.assertIn("status not recorded", certs.summary(record))

    def test_a_completed_course_is_not_a_certification(self):
        course = person({"name": "Professional Scrum Master I", "kind": "course_certificate", "status": "completed"})
        _gate, reqs, _ = judged(course, "Scrum certification required")
        record = certs.analyse(reqs, course, POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["satisfied"]), ("completed", False))
        mislabelled = person({"name": "Professional Scrum Master I", "kind": "course_certificate", "status": "held"})
        self.assertFalse(certs.analyse(reqs, mislabelled, POLICY)[-1]["satisfied"])   # a course never counts as held
        self.assertEqual(certs.held_names(mislabelled, POLICY), [])
        self.assertFalse(mislabelled.has_certification("professional scrum master i"))

    def test_conflicting_records_are_flagged_and_never_held(self):
        both = person({"name": "PMP", "status": "held"}, {"name": "Project Management Professional", "status": "in_progress"})
        _gate, reqs, _ = judged(both, "PMP required")
        record = certs.analyse(reqs, both, POLICY)[-1]
        self.assertEqual((record["candidate_status"], record["satisfied"]), ("conflicting", False))
        self.assertEqual(len([w for w in certs.profile_warnings(both, POLICY) if "different statuses" in w]), 2)
        self.assertEqual(certs.credential_status(both, POLICY, "PMP"), "conflicting")

    def test_the_evidence_basis_is_shown_and_only_an_issuer_check_is_verification(self):
        _gate, reqs, _ = judged(PROFILE, "PMP required")
        for basis, words in (("self_reported", "held (self-reported)"), ("document_inspected", "held (certificate checked)"),
                             ("issuer_verified", "held (verified with the issuer)")):
            p = person({"name": "PMP", "status": "held", "evidence": {"source": "resume", "basis": basis}})
            record = certs.analyse(reqs, p, POLICY)[-1]
            self.assertIn(words, certs.summary(record), basis)
            self.assertIn(words, certs.explain([record])[0], basis)
            if basis != "issuer_verified":
                self.assertNotIn("verified", certs.summary(record) + certs.explain([record])[0], basis)

    def test_credential_backed_evidence_is_kept_but_flagged_for_review(self):
        skills = [{"name": "agile delivery", "evidence": "adjacent", "source": "PMP certification", "credentials": ["PMP"]}]
        for status, expected in (("held", []), ("in_progress", ["in_progress"]), (None, ["unknown"])):
            cert = {"name": "PMP", **({"status": status} if status else {})}
            p = Profile({**PROFILE.data, "certifications": [cert], "skills": skills}, "r")
            self.assertEqual([r["status"] for r in certs.evidence_review(p, POLICY)], expected, status)
            self.assertEqual(p.skill("agile delivery")["evidence"], "adjacent")     # never erased
        gone = Profile({**PROFILE.data, "certifications": [], "skills": skills}, "r")
        self.assertEqual(certs.evidence_review(gone, POLICY), [{"item": "agile delivery", "kind": "skill", "credential": "PMP",
                                                               "status": "absent"}])

    def test_preferred_held_is_a_plus_and_preferred_missing_is_nothing(self):
        held = person({"name": "PL-300", "status": "held"})
        gate, reqs, _ = judged(held, "PL-300 certification preferred")
        self.assertEqual(certs.analyse(reqs, held, POLICY)[-1]["decision_effect"], "positive_signal")
        gate, reqs, _ = judged(PROFILE, "PMP preferred")
        self.assertEqual(certs.analyse(reqs, PROFILE, POLICY)[-1]["decision_effect"], "none")
        self.assertFalse([f for f in gate.hard_failures + gate.flags if f["code"].startswith("certification")])

    def test_a_family_requirement_is_met_by_any_held_member(self):
        psm = person({"name": "Professional Scrum Master I", "status": "held"})
        gate, reqs, _ = judged(psm, "Scrum certification required")
        record = certs.analyse(reqs, psm, POLICY)[-1]
        self.assertEqual((record["match_type"], record["satisfied"]), ("family_match", True))
        self.assertFalse([f for f in gate.flags if f["code"] == "certification_check"])

    def test_alternatives_are_satisfied_by_any_one(self):
        cbap = person({"name": "CBAP", "status": "held"})
        gate, reqs, _ = judged(cbap, "PMP or CBAP certification required")
        self.assertNotIn("certification_required", {f["code"] for f in gate.hard_failures})
        pmp = next(r for r in certs.analyse(reqs, cbap, POLICY) if r["certification_key"] == "pmp")
        self.assertEqual(pmp["satisfied_by_alternative"], "Certified Business Analysis Professional (CBAP)")
        gate, _reqs, _ = judged(PROFILE, "PMP or CBAP certification required")
        failures = [f for f in gate.hard_failures if f["code"] == "certification_required"]
        self.assertEqual(len(failures), 1)   # one rejection for the set of alternatives, naming both
        self.assertIn("Project Management Professional (PMP) or Certified Business Analysis Professional (CBAP) required",
                      failures[0]["detail"])

    def test_owner_approved_equivalence_only(self):
        prince2 = person({"name": "PRINCE2", "status": "held"})
        _gate, reqs, _ = judged(prince2, "PMP required")
        self.assertEqual(certs.analyse(reqs, prince2, POLICY)[-1]["match_type"], "related")
        from tests.test_v21_policy import policy_with
        approved = policy_with(certifications={**CATALOG, "equivalent": [["pmp", "prince2"]]})
        record = certs.analyse(reqs, prince2, approved)[-1]
        self.assertEqual((record["match_type"], record["satisfied"]), ("equivalent", True))

    def test_an_unnamed_certification_is_a_check_with_the_wording_quoted(self):
        gate, _reqs, _ = judged(PROFILE, "Relevant industry certification required")
        flag = next(f for f in gate.flags if f["code"] == "certification_check")
        self.assertIn('("- Relevant industry certification required")', flag["detail"])


class ReviewFixTests(unittest.TestCase):
    """Fresh-context Claude review of 4486b82 (2026-10-18). Synthetic credentials only."""

    def failures(self, profile, *lines):
        gate, _, _ = judged(profile, *lines)
        return [f for f in gate.hard_failures if f["code"] == "certification_required"]

    def test_an_or_elsewhere_on_the_line_never_makes_separate_credentials_alternatives(self):
        cbap = person({"name": "CBAP", "status": "held"})
        self.assertTrue(self.failures(cbap, "Must hold both PMP and CBAP certifications, plus experience with Jira or Azure DevOps"))
        self.assertFalse(self.failures(cbap, "PMP or CBAP certification required"))
        self.assertFalse(self.failures(cbap, "PMP, PSM I, or CBAP certification required"))
        self.assertFalse(self.failures(cbap, "One of the following certifications is required: PMP, CBAP"))
        by_line = {r["normalized_name"]: r for r in records(posting_with("PMP and CBAP required; Jira or Azure DevOps"), cbap)}
        self.assertNotIn("satisfied_by_alternative", by_line["Project Management Professional (PMP)"])

    def test_a_course_or_an_unknown_kind_never_counts_as_held(self):
        for kind in ("course certificate", "course", "Course_Certificate", "banana"):
            p = person({"name": "PMP", "status": "held", "kind": kind})
            self.assertTrue(self.failures(p, "PMP required"), kind)
            self.assertEqual(certs.held_names(p, POLICY), [], kind)
            self.assertFalse(p.has_certification("pmp"), kind)
        self.assertIn("never counted as held", " ".join(certs.profile_warnings(person({"name": "PMP", "status": "held", "kind": "banana"}), POLICY)))
        self.assertIn("a course never counts", " ".join(certs.profile_warnings(person({"name": "PMP", "status": "held", "kind": "course"}), POLICY)))

    def test_a_course_named_like_a_certification_never_collides_with_it(self):
        both = person({"name": "Microsoft Power BI Data Analyst Professional Certificate", "status": "completed", "kind": "course_certificate"},
                      {"name": "PL-300", "status": "held"})
        self.assertEqual({c["status"] for c in certs.profile_credentials(both, POLICY)}, {"completed", "held"})
        self.assertFalse(self.failures(both, "PL-300 certification required"))
        course_first = person({"name": "Project Management Professional course", "status": "held", "kind": "course_certificate",
                               "matches": ["pmp"]}, {"name": "PMP", "status": "held"})
        self.assertFalse(self.failures(course_first, "PMP required"))

    def test_an_issuer_requirement_is_met_by_that_issuer_only(self):
        ccs = person({"name": "CCS", "status": "held"})
        record = records(posting_with("AAPC certification required"), ccs)[-1]
        self.assertFalse(record["satisfied"])
        self.assertTrue(records(posting_with("AAPC certification required"), person({"name": "CPC", "status": "held"}))[-1]["satisfied"])

    def test_status_words_kinds_and_bases_are_normalised_and_checked(self):
        for written in ("Held", "HELD", " held "):
            self.assertEqual(certs.held_names(person({"name": "PMP", "status": written}), POLICY), ["PMP"], written)
        for written in ("in progress", "in-progress", "In_Progress"):
            self.assertEqual(certs.profile_credentials(person({"name": "PMP", "status": written}), POLICY)[0]["status"], "in_progress")
        self.assertEqual(certs.held_names(person({"name": "PMP", "status": "expired"}), POLICY), [])
        odd = person({"name": "PMP", "status": "held", "evidence": {"basis": "verified"}})
        self.assertIsNone(certs.profile_credentials(odd, POLICY)[0]["basis"])                     # never shown as verified
        self.assertIn("unrecognised evidence basis", " ".join(certs.profile_warnings(odd, POLICY)))

    def test_a_skill_from_a_completed_course_is_not_under_review(self):
        trained = Profile({**PROFILE.data, "certifications": [{"name": "Data Course", "status": "completed", "kind": "course_certificate"}],
                           "skills": [{"name": "sql", "years": 1, "credentials": ["Data Course"]}]}, "t")
        self.assertEqual([r for r in certs.evidence_review(trained, POLICY) if r["item"] == "sql"], [])
        planned = Profile({**trained.data, "certifications": [{"name": "PL-300", "status": "planned"}],
                           "skills": [{"name": "power bi", "years": 2, "credentials": ["PL-300"]}]}, "u")
        self.assertEqual([r["status"] for r in certs.evidence_review(planned, POLICY)], ["planned"])


class ScoringTests(unittest.TestCase):
    def test_certifications_add_no_points(self):
        base, extra = job(TITLE, postings.GOOD_FIT), job(TITLE, posting_with("PMP preferred", "PL-300 certification preferred"))
        cards = []
        for j in (base, extra):
            reqs = extract(j["description_text"], POLICY).requirements
            cards.append(score(j, reqs, PROFILE, evaluate(j, reqs, PROFILE, POLICY), POLICY, NOW))
        self.assertEqual((cards[0].apply_priority, cards[0].candidate_fit, cards[0].tier, cards[0].components["skill_coverage"]),
                         (cards[1].apply_priority, cards[1].candidate_fit, cards[1].tier, cards[1].components["skill_coverage"]))
        self.assertEqual([r["certification_key"] for r in cards[1].components["certifications"]], ["pmp", "pl-300"])


class ExplanationTests(unittest.TestCase):
    def test_deterministic_lines_from_the_records(self):
        found = records(posting_with("PMP preferred", "PL-300 certification required"), person({"name": "PL-300", "status": "planned"}))
        first, second = certs.explain(found), certs.explain(json.loads(json.dumps(found)))
        self.assertEqual(first, second)
        self.assertEqual(first[0], 'Project Management Professional (PMP) · issuer PMI · preferred ("preferred") · not held · no match'
                                   ' · effect: no effect · from: "- PMP preferred"')
        self.assertIn("planned (not counted as held)", first[1])
        self.assertEqual(certs.explain([]), ["Certifications: none mentioned."])


class DemoSafetyTests(unittest.TestCase):
    def test_the_demo_persona_has_only_synthetic_certifications(self):
        persona = yaml.safe_load(world.PROFILE)
        self.assertIn("fictional", world.PROFILE.split("\n")[0])
        for cert in persona["certifications"]:
            self.assertEqual(set(cert), {"name", "status"})   # no credential ids, numbers or dates


class DemandTests(unittest.TestCase):
    def test_demand_counts_observed_jobs_only(self):
        store, _clock = new_store()
        lines = {"j1": ["PMP required", "PL-300 certification preferred"], "j2": ["PMP preferred"],
                 "j3": ["Obtain the platform certification after hire"]}
        writer = store.writer("extract")
        for job_id, text in lines.items():
            add_job(store, job_id, family="bsa_healthcare", extraction_fingerprint="fp")
            reqs = extract("Requirements:\n" + "\n".join(f"- {t}" for t in text), POLICY).requirements
            for i, r in enumerate(reqs):
                writer.insert("requirements", {"req_id": f"{job_id}:{i}", "job_id": job_id, "extraction_fingerprint": "fp",
                                               "extractor": "rules", "created_at": NOW, **r.as_dict()})
        result = demand(store, POLICY, person({"name": "PL-300", "status": "planned"}))
        self.assertEqual((result["relevant_jobs"], result["jobs_with_certification"]), (3, 2))   # j3 is training, not a requirement
        pmp = next(r for r in result["certifications"] if r["certification"].startswith("Project Management Professional"))
        self.assertEqual((pmp["jobs"], pmp["hard_required"], pmp["preferred"], pmp["your_status"], pmp["jobs_where_you_lack_it_as_required"]),
                         (2, 1, 1, "not_held", 1))
        self.assertEqual(result["jobs_where_a_planned_certification_appears"], 1)
        self.assertEqual([r["certification"] for r in result["not_held_by_demand"]][0], pmp["certification"])


if __name__ == "__main__":
    unittest.main()
