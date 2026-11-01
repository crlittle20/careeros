"""Questions never answered from answers.yaml, read by their wording (ADR 0009 amendment, 2026-10-22).

A regression corpus from the second Claude review of the Codex corrections: real-world wordings an employer form may
use, in every category the rule names, and routine questions the rule must leave alone. The wording check is the
second line of defence; the first is that every answer declares a kind from a closed list with no kind for these
questions (tests/test_packages.py). Wording varies, so the check is conservative: the few routine questions it still
holds back are listed in ACCEPTED_FALSE_POSITIVES, and `careeros package gaps` names the words that held each back.
"""
import random
import re._parser
import unicodedata
import unittest

from careeros.review import answers


def open_parts(pattern: str) -> list[str]:
    """Each part of a regular expression that can take words it doesn't spell out: any character, a negated class, a
    class with a letter range or with letters other than the spelling variant [sz], or a class escape (\\w, \\S, \\D...).
    Read from Python's own parse of the pattern, so no way of writing it is missed."""
    found: list[str] = []

    def walk(items):
        for op, av in items:
            name = str(op)
            if name in ("ANY", "NOT_LITERAL"):
                found.append(name)
            elif name == "IN":
                members = [(str(o), a) for o, a in av]
                letters = {chr(a) for o, a in members if o == "LITERAL"} - {"-", " "}
                if not all(o == "LITERAL" or (o == "RANGE" and a == (48, 57)) or (o == "CATEGORY" and str(a) == "CATEGORY_DIGIT")
                           for o, a in members) or letters - {"s", "z"} or (letters and letters != {"s", "z"}):
                    found.append(f"{name} {members}")
            elif name == "SUBPATTERN":
                walk(av[-1])
            elif name == "BRANCH":
                for branch in av[1]:
                    walk(branch)
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                walk(av[2])
            elif name in ("ASSERT", "ASSERT_NOT"):
                walk(av[1])
            elif name == "ATOMIC_GROUP":
                walk(av)
            elif name not in ("LITERAL", "AT"):
                found.append(name)   # anything else is treated as open
    walk(re._parser.parse(pattern))
    return found

NEVER_ANSWERED = {
    "consent, agreement or attestation": [
        "I understand that any false statement may result in disqualification",
        "By submitting this application, I confirm the information is accurate",
        "I hereby declare that the information provided is true and complete",
        "I affirm that the statements in this application are true",
        "Do you accept the terms?",
        "I have read and understood the Applicant Notice",
        "By typing my name below I confirm the above",
        "The applicant authorizes the release of information from former employers",
        "Do you agree?",
        "By agreeing, you allow us to keep your CV on file",
        "Opt in to SMS updates about your application",
        "Would you like us to retain your data for future opportunities?",
        "Data retention: may we keep your application for 24 months?",
        "Can we keep your information on file for future roles?",
        "May we contact your current employer?",
        "Do you give permission for us to contact your references?",
        "I accept",
        "Truthfulness statement: everything above is accurate",
        "I confirm that the information I have provided in this application is true and complete.",
        "I understand and accept the terms of this application.",
        "I declare that the information in this application is correct",
        "Would you like to receive text messages about your application?",
        "Do you consent to the processing of your personal data?",
        "Consent to background check",
        "By checking this box you agree to the privacy policy",
        "Please acknowledge that the information provided is accurate",
        "Do you authorize us to contact your previous employers?",
        "I certify that the above is true",
        "Please confirm you have read our applicant privacy notice",
        "I agree",
        "Yes, I have read and agree to the privacy notice",
        "I consent", "GDPR Notice", "EEO Notice",
    ],
    "signatures": [
        "Type your full legal name", "Please enter your initials", "Signing below indicates you agree",
        "Applicant e signature", "Electronic signature", "Signature", "Please type your initials to confirm",
    ],
    "demographics, citizenship or national origin": [
        "Gender", "Are you male or female?", "Do you identify as LGBTQ+?", "How old are you?", "What is your DOB?",
        "Birthday", "What is your nationality?", "Are you Hispanic or Latino?", "Do you identify as transgender?",
        "Are you a member of a visible minority?", "Do you identify as Indigenous?",
        "Which of the following best describes you?", "Are you a woman in tech?", "What is your race/ethnicity?",
        "What is your race?",
        "Please self-identify your ethnic background", "Are you a U.S. citizen?", "What is your citizenship?",
        "Country of citizenship", "DOB",
    ],
    "disability, health or veteran status": [
        "Do you have any disabilities?", "Do you have a disability?",
        "Do you require a reasonable accommodation to complete the interview process?",
        "Do you have a physical or mental impairment?", "Do you have a medical condition that would affect your work?",
        "Are you handicapped?", "Have you served in the U.S. Armed Forces?", "Have you served in the military?",
        "Are you a military spouse?", "Are you a current member of the National Guard or Reserves?",
        "Uniformed services status", "Are you a protected veteran?", "Are you one of the protected veterans?",
        "Veterans status", "Do you qualify for veterans' preference?",
    ],
    "background, criminal and drug checks": [
        "Have you ever been found guilty of a crime?", "Have you ever pleaded guilty or no contest to an offense?",
        "Do you have any pending charges?", "Are you currently on probation or parole?", "Have you ever been incarcerated?",
        "Have you been charged with an offence?", "Have you ever been convicted of a felony?",
        "Are you willing to undergo a credit check?", "Will you submit to a background investigation?",
        "Do you consent to a background screening?", "Will you submit to a drug screen?",
        "Are you willing to take a pre-employment drug and alcohol screening?",
    ],
    "identity documents": [
        "Driver's License #", "Driver License Number", "Driving licence number", "Driver\u2019s license number",
        "Social Insurance Number (SIN)", "National Insurance number", "Passport No.", "Taxpayer identification number",
        "Social Security Number",
    ],
    "salary history": ["What is your current salary?", "What was your salary at your previous job?", "Current compensation",
                       "Salary at your last position", "Pay history", "Compensation history"],
}
# The same questions as a pasted form label may carry them: no-break spaces, line breaks, doubled spaces, curly quotes,
# soft hyphens, zero-width and fullwidth characters.
DISGUISED = [
    "What is your sexual\u00a0orientation?", "Date\u00a0of\u00a0birth", "Date  of birth", "Sexual\norientation",
    "Social\u00a0Security\u00a0Number", "Accept the terms\u00a0and\u00a0conditions",
    "Are you willing to undergo a background\u00a0check?", "Do you con\u00adsent to the processing of your data?",
    "G\u200bender", "\uff27ender", "Driver\u2019s license number",
    "Sexual&nbsp;orientation", "Terms &amp; Conditions", "Ge\u0301nder", "G\u0435nder",
]
ROUTINE = [
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require sponsorship for employment visa status?",
    "Will you now or in the future require visa sponsorship?", "When can you start?", "Earliest start date",
    "Are you willing to relocate?", "Do you have a PMP certification?", "Are you PMP certified?",
    "Are you certified in Epic?", "Which certifications do you hold?", "Do you hold a CPA certificate?",
    "Are you bound by a non-compete agreement?", "Are you subject to any non-solicitation agreements?",
    "Describe a time you had to reach agreement with a stakeholder", "Describe your experience with design systems",
    "Do you have experience with digital signage?", "Do you have experience with HL7 and data processing pipelines?",
    "Are you at least 18 years of age?", "Are you over the age of 18?", "What languages do you speak?",
    "Do you have experience with Medicare Advantage?", "How did you hear about us?", "What is your desired salary?",
    "What are your salary expectations?", "Do you have a valid driver's license?", "Do you have a valid passport?",
    "Do you have experience with race condition debugging?", "Have you worked with privacy-preserving analytics?",
    "Do you have experience with GDPR compliance programs?", "Have you used Salesforce Agreements?",
    "Are you comfortable with a sign-on bonus clawback?", "Have you designed SSO or authorization flows?",
    "Are you willing to sign a non-disclosure agreement?", "Have you signed an offer with another company?",
    "Are you fluent in American Sign Language?", "Are you familiar with HIPAA privacy and security rules?",
    "Do you have experience processing veteran benefits claims?", "Do you have experience reviewing cardiac arrest claims?",
    "Do you have experience protecting personal information (PHI/PII)?",
    "Do you have experience in claims adjudication and appeals?", "What is your current title?", "LinkedIn Profile",
    "Website", "Are you willing to work in the office 3 days a week?", "Do you have security clearance?",
    "Have you previously worked for this company?", "Were you referred by an employee?",
    "Do you have experience with background jobs and queues?", "First Name", "Last Name", "Email", "Phone",
    "Why do you want to work here?", "How many years of claims experience do you have?",
    "Are you willing to travel up to 25%?", "Yes", "No", "Prefer not to say",
]
# The fourth Claude review's end-to-end cases (2026-10-22): each reached the panel and the handoff while an answer's kind
# was matched by a trigger word anywhere in the question. Now no kind's whole-question shape takes any of them.
FOURTH_REVIEW = [
    'What is your current visa status?',
    'What type of visa do you currently hold?',
    'Have you ever had a US visa denied or revoked?',
    'Have you been a US resident for at least 3 of the last 5 years?',
    "Has your driver's license ever been suspended or revoked?",
    'Has your professional license ever been revoked, suspended, or subject to disciplinary action?',
    'Are you currently listed in the System for Award Management (SAM) as excluded?',
    'Are you over 40 years of age?',
    'Are you at least 18 years of age?',
    'What year did you graduate from high school?',
    'How many years of active duty service do you have?',
    'Do you have any upcoming Reserve or Guard duty that would affect your availability?',
    'Branch of service and years of active duty',
    'Have you used illegal substances in the past 5 years?',
    'Have you used any drugs other than as prescribed?',
    'Describe any physical or mental condition that may affect your ability to perform this job',
    'Do you have lived experience with mental health or substance use challenges?',
    'Are you receiving disability benefits that would limit the hours you can work?',
    'Have you ever filed a disability claim against a previous employer?',
    'Do you need a modified schedule for medical reasons?',
    'Do you have any planned maternity or parental leave that would affect your start date?',
    'Do you have personal experience as a Medicaid or Medicare member?',
    'How much were you making at your last job?',
    'What were you earning in your most recent position?',
    'Most recent employer and hourly rate',
    'Ending hourly rate at previous employer',
    'Total comp at your current role (base + bonus + equity)',
    'What are you expecting to earn, and what were you earning in your last position?',
    'What is your expected salary? What is your present salary?',
    'Are you a first-generation college student?',
    'Did you attend a Historically Black College or University (HBCU)?',
    'Were you eligible for free or reduced-price lunch at school?',
    'What language do you speak at home?',
    'Is it OK for a recruiter to text you at the number provided?',
    'Should we update the person who referred you on your application status?',
    'Is it OK to keep your profile in our database for 2 years?',
    'Do you have childcare responsibilities that limit your hours?',
    'Are you willing to relocate? If so, will your spouse also need employment?',
    'Passport expiry date (for travel)',
    'For international travel, which country issued your passport?',
    'Employment Authorization Document (EAD) number',
    'Naturalization certificate number',
    'Are you a resident of an Empowerment Zone or Rural Renewal County?',
    'Do you qualify for the Work Opportunity Tax Credit (WOTC)?',
    'Have you previously worked here under a maiden name?',
    'Have you ever been injured on the job? If yes, please describe.',
    'What is your current immigration/visa status?',
    'Are you the first person in your family to attend college?',
    'Have you been a resident of the United States for the past three years?',
    'What was your hourly rate at your last job?',
    'Are you 18 years of age or older?',
    'Has your license ever been suspended or revoked?',
    'Do you have experience with homelessness or housing instability?',
    'Are you at least 18 years of age?',
    'City and state of birth',
    'Mobile number for text updates',
    'Printed name',
]
# The fifth Claude review's cases (2026-10-22): each fit a kind's shape through a free part (a certificate's name, a
# "(if so, ...)" clause, a sponsorship tail) or a second sentence, before the free parts became short names.
FIFTH_REVIEW = [
    "Do you have a valid DOT medical certificate?", "Do you have a current medical examiner's certificate?",
    "Do you have a COVID-19 vaccination certificate?", "Do you have a Schedule A certification?",
    "Do you have a clean driver's license?", "Do you have an active license in good standing, free of any sanctions?",
    "Are you certified as a member of a federally recognized tribe?",
    "Do you need sponsorship for employment, or do you have refugee or asylee status?",
    "Will you require sponsorship to continue your DACA work authorization?",
    "Have you ever worked for us (if so, list your dates, title and final rate)?",
    "Have you ever been employed by the U.S. government in a uniformed capacity?",
    "Have you ever been employed by a correctional industries program as an inmate?",
    "Do you have experience with Facets.\nWhat was your final hourly rate?",
    "Do you have experience with Facets\u200bWhat was your rate?",
]
# The sixth Claude review's cases (2026-10-22): a free name before "clearance" or "certificate", a military or tribal
# term inside a skill or credential.
SIXTH_REVIEW = [
    "Are you able to obtain a background clearance?", "Are you able to obtain an FBI clearance?",
    "Are you able to obtain DOJ clearance?", "Do you have a valid police clearance certificate?",
    "Are you able to obtain a health clearance?", "Are you able to obtain a psychological clearance?",
    "Do you have a current DOT physical certificate?", "How many years of combat experience do you have?",
    "Do you have any experience in the USMC?", "Do you have a CDIB certificate?", "Are you a registered MMJ patient?",
    "Do you have a DD 214 certificate?", "Do you have a polygraph clearance?", "Have you ever worked for the USMC?",
]
# Routine questions each kind must still take, as a whole question.
KIND_EXAMPLES = {
    "work_authorization": ["Are you legally authorized to work in the United States?", "Are you authorized to work in the US?",
                           "Work authorization"],
    "sponsorship": ["Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?",
                    "Will you require visa sponsorship?", "Do you now or in the future require sponsorship?"],
    "start_date": ["When can you start?", "Earliest start date", "What is your expected start date?"],
    "notice_period": ["What is your notice period?", "How much notice do you need to give your current employer?"],
    "availability": ["Are you available to work full-time?", "Are you able to work evenings and weekends as needed?",
                     "Are you open to a contract-to-hire position?"],
    "relocation": ["Are you willing to relocate?", "Would you be willing to relocate for this role?",
                   "Do you need relocation assistance?"],
    "schedule": ["Are you comfortable with a rotating schedule?", "Can you work in the local time zone?",
                 "Are you able to work in the Pacific time zone?"],
    "work_location": ["Are you willing to work in the office 3 days a week?", "Where are you currently located?",
                      "Are you located in the United States?", "Do you have a reliable high-speed internet connection?",
                      "Do you live within commuting distance of our office?"],
    "travel": ["Are you willing to travel up to 25%?", "Are you willing to travel?"],
    "salary_expectation": ["What are your salary expectations?", "What is your desired salary?",
                           "Desired annual compensation"],
    "experience": ["How many years of experience do you have in claims adjudication?",
                   "How many years of medical claims experience do you have?",
                   "Do you have experience with medical coding (ICD-10, CPT, HCPCS)?",
                   "Do you have experience with Medicare Advantage?", "Describe your experience with SQL and Power BI."],
    "previous_employment": ["Have you previously worked for this company?", "Current employer"],
    "motivation": ["Why do you want to work here?", "Why are you interested in this role?"],
    "certification": ["Do you have a PMP certification?", "Are you PMP certified?", "Do you have a valid driver's license?",
                      "Are you a Certified Professional Coder (CPC)?", "Do you have an active secret clearance?",
                      "Are you able to obtain a public trust clearance?"],
    "education": ["What is your highest level of education?", "Do you have a bachelor's degree?"],
    "language": ["What languages do you speak?", "Are you fluent in Spanish?", "Do you speak Mandarin fluently?"],
    "referral_source": ["How did you hear about us?", "Were you referred by an employee?"],
    "link": ["LinkedIn Profile", "Website", "Portfolio URL"],
}
# The verification of the Pass 2 package (2026-10-22): free parts in six kinds the extension fills (a relocation reason
# or destination, a commuting destination or named office, a referral name, a language, a time zone) carried a
# protected subject. Each case below fit one of those kinds before the fix; all but the veterans one pass the wording
# list, so they test the shapes. Only the named kinds keep a free part now; the others are closed.
VERIFICATION_CASES = [
    "Are you willing to relocate for a pregnancy?", "Are you willing to relocate for medical treatment?",
    "Are you willing to relocate for family reasons?", "Would you be willing to relocate to a treatment facility?",
    "Do you live within commuting distance of the reservation?", "How did you hear about our veterans program?",
    "How did you hear about our tribal health program?", "Are you willing to work from our VA clinic office?",
    "Can you work in the tribal time zone?", "Do you speak Navajo?", "Are you fluent in ASL?",
    "Are you able to work Monday through Friday, 9am to 5pm ssi?",
]
# Routine questions from the owner's own field, kept to record what the rules cost there (the fourth and fifth Claude
# reviews' samples, synthetic). Questions that fit no kind are held back: you answer them on the form.
OWNERS_FIELD_KIND_SAMPLE = [
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?",
    "What are your salary expectations?", "Desired salary", "What is your desired hourly rate?",
    "When can you start?", "Earliest start date", "How did you hear about this job?", "LinkedIn Profile", "Website",
    "Are you willing to work remotely?", "Are you comfortable working remotely?",
    "Are you located in the United States?", "What state do you currently reside in?",
    "Do you live in one of the following states: FL, GA, TX, or NC?",
    "Do you have at least 3 years of claims processing experience?",
    "How many years of claims processing experience do you have?",
    "How many years of experience do you have with claims processing?",
    "How many years of medical claims experience do you have?", "Do you have experience with Facets?",
    "Do you have experience with QNXT?", "Do you have experience with medical coding (ICD-10, CPT, HCPCS)?",
    "Do you have experience with Medicare and Medicaid claims?",
    "Do you have experience with coordination of benefits (COB)?",
    "Do you have experience working with denied claims?", "Do you have experience with credit balance resolution?",
    "How many years of experience do you have with paid claims audits?",
    "Do you have experience reviewing claims for members with disabilities?",
    "Do you have experience with claims for pre-existing conditions?",
    "Do you have experience with claim number research and adjustments?", "Do you have experience with SQL?",
    "Are you proficient in Microsoft Excel (VLOOKUP, pivot tables)?", "Rate your proficiency in Microsoft Excel",
    "Do you have a high school diploma or GED?", "What is the highest level of education you have completed?",
    "Highest level of education", "Do you have a bachelor's degree?", "Are you a Certified Professional Coder (CPC)?",
    "Do you hold a CPC certification?", "Are you bilingual in English and Spanish?",
    "Are you able to work Monday through Friday, 8:00 AM to 5:00 PM EST?", "Are you available to work full-time?",
    "Are you able to work overtime as needed?", "Do you have a reliable high-speed internet connection?",
    "Do you have a dedicated, distraction-free home workspace?",
    "Are you comfortable with a production-based role with quality and productivity metrics?",
    "Are you a current or former employee of Synthetic Health?",
    "Have you previously been employed by Synthetic Health?", "Why are you interested in this role?",
    "Tell us about yourself", "Please describe your experience with claims adjudication.",
    "What is your current notice period?", "Are you open to a contract-to-hire position?",
    "Do you have experience with HIPAA compliance?",
    "Do you have experience with explanation of benefits (EOB) review?",
    "Do you have experience with provider disputes and appeals?", "Are you willing to relocate?",
    "What is your typing speed (WPM)?",
]
OWNERS_FIELD_WORDING_SAMPLE = [
    "How many years of experience do you have in claims adjudication?",
    "Describe your experience with ICD-10 diagnosis coding.",
    "Do you have experience with diagnosis and procedure codes (ICD-10, CPT)?",
    "Describe your experience handling protected health information (PHI).",
    "Do you have experience safeguarding member health information under HIPAA?",
    "Have you processed workers' compensation claims?", "Do you have experience auditing paid claims?",
    "How many paid and denied claims did you review per month?",
    "Describe your experience with provider pay-for-performance programs.",
    "Describe your experience with OIG and SAM exclusion screening of providers.",
    "Do you have experience with OIG compliance audits?",
    "Describe your experience with HEDIS measures for members with disabilities.",
    "Do you have experience with long-term disability claims?",
    "Do you have experience with pharmacy claims for controlled substances?",
    "Do you have experience with maternity and pregnancy-related claims?",
    "Describe your experience with COVID-19 claims or vaccine billing.",
    "Do you have experience with immunization measures (HEDIS CIS)?",
    "Describe your experience with Medicare Advantage risk adjustment (age/sex factors).",
    "Have you analyzed race and ethnicity data for health equity reporting?",
    "Do you have experience with member data privacy requirements?",
    "Describe your experience with HIPAA Privacy Notice requirements.",
    "Do you have experience with claims for veterans in VA community care?",
    "Do you have experience with Medicaid ID verification at enrollment?",
    "Describe your experience with consent forms for release of medical records.",
    "Describe your experience with medical history abstraction from records.",
    "Do you have experience with disabled member outreach?",
    "Have you worked with background check vendors for provider credentialing?",
    "Describe your experience with credit balance and overpayment recovery.",
    "Do you have experience with behavioral health and substance use disorder claims?",
    "Do you have experience with coordination of benefits and Medicare Secondary Payer rules?",
    "Do you have experience with prior authorization workflows?",
    "Describe your experience with claims appeals and grievances.",
    "Have you worked with provider reimbursement and fee schedules?",
    "Describe your experience with SQL and Excel for claims analytics.", "Do you have experience with subrogation?",
    "Do you have experience with dual-eligible members?",
    "Do you have experience with the Fair Credit Reporting Act in vendor oversight?",
    "Do you have experience with military health (TRICARE) claims?",
    "Describe your experience with fraud, waste and abuse investigations.",
    "Do you have experience with member retention analytics?",
    "Describe a project where you reduced claim pend rates.", "Do you have experience with 837 and 835 transactions?",
    "Do you have experience with DRG pricing and outlier payments?",
    "Have you supported open enrollment for employer group plans?",
    "Describe your experience with Medicaid redeterminations.",
    "Do you have experience with claims for members with chronic conditions?",
    "Do you have experience with accommodation requests from members?",
    "Do you have experience with wage and salary data for underwriting?",
]
# Independent Codex review, Pass 2 of 4787e2a (2026-10-22): an answer's own text was read only when 80 characters or
# fewer, so a long answer carrying a consent, or a short one padded with invisible characters, was used and filled.
# Each question fits its kind; only the answer's text holds it back.
LONG_OR_PADDED_SENSITIVE_ANSWERS = [
    ("start_date", "When can you start?", "I can start two weeks after accepting an offer. I agree to the privacy policy and "
     "consent to retention of my application."),
    ("start_date", "When can you start?", "I agree" + "\u200b" * 81),
    ("notice_period", "What is your notice period?", "Two weeks. By submitting this application I certify that the "
     "information I have provided is true and complete."),
    ("salary_expectation", "What are your salary expectations?", "Around $75,000 base, depending on the full package. I "
     "consent to a background check and to you contacting my references."),
    ("availability", "Are you available to work full-time?", "Yes, full-time, Monday through Friday. I have a disability "
     "and will need a reasonable accommodation for the interview process."),
    ("relocation", "Are you willing to relocate?", "No, I'm looking for remote roles only, and I acknowledge the privacy "
     "notice and the terms of this application process."),
]
# Routine long answers (synthetic), kept to record what reading every answer costs: eight of eighteen hold a word the
# wording list reads as a question about the person ("I agree", "consent forms", "to confirm", "on file", "pay"...) and
# are held back; one fillable answer is over the fill limit and is copied from the panel.
LONG_ROUTINE_ANSWERS = [
    ("motivation", "Why are you interested in this role?",
     "I'm drawn to this role because it combines claims analysis with process improvement, and your team's focus on "
     "accuracy matches how I like to work."),
    ("motivation", "Why do you want to work here?",
     "I agree with your mission of making coverage simpler for members, and I want to bring my claims experience to a "
     "team that measures quality."),
    ("experience", "Please describe your experience with claims adjudication.",
     "Five years of medical claims adjudication, including coordination of benefits, appeals and provider disputes, with "
     "daily use of Facets and Excel."),
    ("experience", "Describe your experience with SQL and Power BI.",
     "I write SQL against claims and eligibility tables every week and publish Power BI reports on pend rates, turnaround "
     "time and audit results for my team."),
    ("experience", "Do you have experience with HIPAA compliance?",
     "Yes. I handle protected health information daily, complete annual HIPAA training and follow minimum-necessary "
     "rules when I share claim details with providers."),
    ("experience", "Do you have experience with provider disputes and appeals?",
     "Yes. I reviewed provider disputes and member appeals against contract terms and policy, and wrote the decision "
     "letters that explained each outcome."),
    ("experience", "Please describe your experience with claims adjudication.",
     "I reviewed consent forms for the release of medical records before adjudicating claims that needed records from "
     "another provider."),
    ("salary_expectation", "What are your salary expectations?",
     "I'm looking for $72,000 to $80,000 base, and I'm flexible for the right role, schedule and benefits package."),
    ("start_date", "When can you start?",
     "I can start two weeks after an offer is accepted, since I'll give my current employer the usual two weeks' notice."),
    ("relocation", "Are you willing to relocate?",
     "No. I'm looking for remote roles only, but I can travel occasionally for training or team meetings if that's "
     "needed."),
    ("availability", "Are you available to work full-time?",
     "Yes. I can work full-time, Monday through Friday, and I can cover occasional evening or weekend shifts during "
     "month-end close and open enrollment."),
    ("motivation", "Tell us about yourself",
     "I'm a claims analyst who likes finding the root cause of a pended claim. I have always enjoyed working with "
     "providers to resolve disputes fairly and quickly."),
    # everyday claims phrasing (the read-only check of the Codex Pass 2 correction, 2026-10-22): each holds a word the
    # wording list reads as a question about the person, so each is held back
    ("experience", "Please describe your experience with claims adjudication.",
     "I call providers to confirm member eligibility and keep a note on file for every adjustment, so each claim is "
     "processed correctly the first time."),
    ("experience", "Do you have experience with provider disputes and appeals?",
     "Yes. I send acknowledgment letters for new appeals within five days and track each case to resolution in our "
     "appeals system."),
    ("motivation", "Why are you interested in this role?",
     "I'd like to grow into a senior analyst role here, and I'm open to future opportunities in quality auditing or "
     "training new staff."),
    ("experience", "Please describe your experience with claims adjudication.",
     "I schedule a weekly review of pended claims with the team lead and work the oldest items first to keep "
     "turnaround under ten days."),
    ("experience", "Do you have experience with HIPAA compliance?",
     "Yes. I routed member requests for reasonable accommodations to the right care management team and logged each "
     "one."),
    ("experience", "Describe your experience with SQL and Power BI.",
     "I built a Power BI report of provider pay against contract rates, so underpayments were corrected before the "
     "monthly close."),
]
# Routine questions the conservative check still holds back. Failing safe: you answer them on the form, and `careeros
# package gaps` names the words. Narrowing them would need context the wording doesn't give.
ACCEPTED_FALSE_POSITIVES = [
    "What's your experience with e-signature platforms like DocuSign?",
    "Do you have experience with consent management platforms?",
    "Are you familiar with Section 508 accessibility for users with disabilities?",
]


class WordingTests(unittest.TestCase):
    def test_every_wording_in_the_corpus_is_never_answered_and_says_why(self):
        for category, questions in NEVER_ANSWERED.items():
            for question in questions:
                hit = answers.classify(question)
                self.assertIsNotNone(hit, f"[{category}] {question}")

    def test_a_label_pasted_from_a_web_page_is_read_in_its_canonical_form(self):
        for question in DISGUISED:
            self.assertTrue(answers.sensitive(question), repr(question))
        self.assertEqual(answers.canonical("  G\u200bender\u00a0Identity\n"), "gender identity")
        self.assertEqual(answers.canonical("\uff27ender \u2014 \u201cself\u2011ID\u201d"), 'gender - "self-id"')
        self.assertEqual(answers.normalize("Driver\u2019s\u00a0License Number?*"), "driver's license number")

    def test_routine_questions_are_left_alone(self):
        for question in ROUTINE:
            self.assertIsNone(answers.classify(question), question)

    def test_the_accepted_false_positives_still_fail_safe(self):
        for question in ACCEPTED_FALSE_POSITIVES:
            self.assertIsNotNone(answers.classify(question), question)


class KindShapeTests(unittest.TestCase):
    """Deny by default (fourth Claude review, 2026-10-22): an answer is reused only for a question that is, as a whole,
    one of its kind's shapes."""

    def test_no_kind_takes_any_question_the_fourth_review_used(self):
        for question in FOURTH_REVIEW:
            self.assertEqual(answers.kinds_for(question), [], question)
            for kind in answers.KIND_SHAPES:   # whatever kind the owner gives it, it is held back
                entry = {"id": "x", "kind": kind, "question": question, "phrasings": [], "answer": "Yes", "field": None}
                self.assertIsNotNone(answers.blocked(entry), (kind, question))

    def test_no_kind_takes_any_question_the_fifth_review_used(self):
        for question in FIFTH_REVIEW:
            for kind in answers.KIND_SHAPES:
                entry = {"id": "x", "kind": kind, "question": question, "phrasings": [], "answer": "Yes", "field": None}
                self.assertIsNotNone(answers.blocked(entry), (kind, question))

    def test_no_kind_takes_any_question_the_sixth_review_used(self):
        for question in SIXTH_REVIEW:
            for kind in answers.KIND_SHAPES:
                entry = {"id": "x", "kind": kind, "question": question, "phrasings": [], "answer": "Yes", "field": None}
                self.assertIsNotNone(answers.blocked(entry), (kind, question))
        self.assertEqual(answers.kinds_for("Are you able to obtain a health clearance?"), [])
        self.assertEqual(answers.kinds_for("Are you able to obtain a gaming clearance?"), [])   # only real clearance levels

    def test_an_authorization_answer_is_a_plain_yes_or_no(self):
        base = {"id": "x", "kind": "work_authorization", "question": "Are you legally authorized to work in the US?",
                "phrasings": [], "field": None}
        self.assertIsNone(answers.blocked({**base, "answer": "Yes"}))
        for answer in ("Yes, on F-1 OPT", "Yes - H-1B (transfer needed)", "Yes (TN visa)"):
            self.assertIn("plain Yes or No", answers.blocked({**base, "answer": answer}), answer)

    def test_a_question_that_merely_mentions_pay_is_not_a_salary_expectation(self):
        self.assertIsNone(answers.classify("Salary data"))                       # not about the person, and
        self.assertEqual(answers.kinds_for("Salary data"), [])                  # not a question about expected pay

    def test_authorization_choices_must_be_a_yes_or_a_no(self):
        library = {"documents": {}, "answers": [
            {"id": "wa", "kind": "work_authorization", "question": "Work authorization", "phrasings": [],
             "answer": "Yes", "field": None, "facts": [], "approved": "2026-10-01", "expires": ""}]}
        statuses = ["H-1B", "F-1 OPT", "TN", "E-3", "Other"]   # none of these words is on the wording list
        self.assertEqual(answers.find("Work authorization", library, "2026-10-21", statuses)["status"], "kind_mismatch")
        self.assertEqual(answers.find("Work authorization", library, "2026-10-21", ["Yes", "No"])["status"], "matched")

    def test_a_contact_detail_must_be_written_for_its_own_field(self):
        entry = {"id": "x", "kind": "contact", "question": "Are you able to lift 50 pounds?", "phrasings": [],
                 "answer": "Yes", "field": "location"}
        self.assertIn("location field", answers.blocked(entry))
        self.assertIsNone(answers.blocked({**entry, "question": "Current location"}))

    def test_no_kind_takes_any_question_the_verification_used(self):
        for question in VERIFICATION_CASES:
            self.assertEqual(answers.kinds_for(question), [], question)
            for kind in answers.KIND_SHAPES:
                entry = {"id": "x", "kind": kind, "question": question, "phrasings": [], "answer": "Yes", "field": None}
                self.assertIsNotNone(answers.blocked(entry), (kind, question))

    def test_only_the_named_kinds_have_a_free_part(self):
        """Every other kind's shapes are fixed words, digits and closed lists, so nothing personal fits in them. The named
        kinds' free parts are vetoed and never handed to the extension."""
        for kind, shapes in answers.KIND_SHAPES.items():
            if kind in answers.NAMED_KINDS:
                continue
            for shape in shapes:
                self.assertEqual(open_parts(shape), [], (kind, shape))
        self.assertEqual(set(answers._VETOED_KINDS), set(answers.NAMED_KINDS))
        for free in ("[a-z]+", r"\w+", r"\D{1,40}", r"\S+", r"\W+", ".+", "[^?]+", "[a-z0-9 -]{1,40}", "[A-Z]", "[ab]",
                     answers._NAME, answers._SKILL):   # the reader itself: every way of writing a free part is seen
            self.assertTrue(open_parts(free), free)
        for closed in ("[sz]", "[- ]", "[0-9]", r"\d", r"u\.?s\.?a?", r"\(city\)", r"\bword\b"):
            self.assertEqual(open_parts(closed), [], closed)

    def test_the_cost_in_the_owners_field_is_recorded(self):
        """Measured on the final candidate; a change in either number is a change in what you can reuse."""
        no_kind = [q for q in OWNERS_FIELD_KIND_SAMPLE if not answers.kinds_for(q)]
        self.assertEqual((len(OWNERS_FIELD_KIND_SAMPLE), len(no_kind)), (58, 6), no_kind)
        self.assertEqual([q for q in OWNERS_FIELD_KIND_SAMPLE if answers.classify(q)], [])
        no_kind = [q for q in OWNERS_FIELD_WORDING_SAMPLE if not answers.kinds_for(q)]
        self.assertEqual((len(OWNERS_FIELD_WORDING_SAMPLE), len(no_kind)), (48, 32), no_kind)
        held = [q for q in OWNERS_FIELD_WORDING_SAMPLE if answers.classify(q)]
        self.assertEqual(len(held), 3, held)

    def test_each_kind_takes_its_routine_questions(self):
        for kind, questions in KIND_EXAMPLES.items():
            for question in questions:
                self.assertIn(kind, answers.kinds_for(question), question)
        self.assertEqual(set(KIND_EXAMPLES), set(answers.KIND_SHAPES))

    def test_a_skill_named_in_a_shape_that_is_about_the_person_is_never_taken(self):
        for question in ("Do you have experience with illegal drugs?", "Describe your experience in the military.",
                         "Do you have a suspended license?", "Have you worked for us under a maiden name?",
                         "Have you worked for Acme Corp, and if so, why did you leave?"):
            self.assertEqual(answers.kinds_for(question), [], question)

    def test_a_contact_detail_fills_only_its_own_standard_field(self):
        library = {"documents": {}, "answers": [
            {"id": "city", "kind": "contact", "question": "Location", "phrasings": ["City and state of birth"],
             "answer": "Tampa, FL", "field": "location", "facts": [], "approved": "2026-10-01", "expires": ""},
            {"id": "first", "kind": "contact", "question": "First Name", "phrasings": ["Printed name"],
             "answer": "Avery", "field": "first_name", "facts": [], "approved": "2026-10-01", "expires": ""}]}
        self.assertEqual(answers.find("City and state of birth", library, "2026-10-21")["status"], "missing")
        self.assertEqual(answers.find("Printed name", library, "2026-10-21")["status"], "missing")
        self.assertEqual(answers.find_field("first_name", library, "2026-10-21", "Printed name")["status"], "kind_mismatch")
        self.assertEqual(answers.find_field("first_name", library, "2026-10-21", "First Name")["status"], "matched")
        self.assertEqual(answers.find_field("first_name", library, "2026-10-21", "")["status"], "matched")


class AnswerTextTests(unittest.TestCase):
    """Independent Codex review, Pass 2 of 4787e2a (2026-10-22): an answer's own text is read whatever its length."""

    def entry(self, kind, question, answer):
        return {"id": "x", "kind": kind, "question": question, "phrasings": [], "answer": answer, "field": None}

    def test_a_long_or_padded_answer_that_reads_as_sensitive_is_never_used(self):
        for kind, question, answer in LONG_OR_PADDED_SENSITIVE_ANSWERS:
            self.assertTrue(answers.fits(kind, question), question)             # the question alone would be answered
            self.assertIn("its answer reads as", answers.blocked(self.entry(kind, question, answer)) or "", (kind, answer[:40]))
        padded = LONG_OR_PADDED_SENSITIVE_ANSWERS[1][2]
        self.assertEqual((len(padded) > 80, answers.canonical(padded)), (True, "i agree"))   # past the old limit, read as "i agree"

    def test_the_cost_in_long_routine_answers_is_recorded(self):
        held = [answer[:40] for kind, question, answer in LONG_ROUTINE_ANSWERS if answers.blocked(self.entry(kind, question, answer))]
        self.assertEqual(held, ["I agree with your mission of making cove", "I reviewed consent forms for the release",
                                "I call providers to confirm member eligi", "Yes. I send acknowledgment letters for n",
                                "I'd like to grow into a senior analyst r", "I schedule a weekly review of pended cla",
                                "Yes. I routed member requests for reason", "I built a Power BI report of provider pa"])
        too_long = [kind for kind, _question, answer in LONG_ROUTINE_ANSWERS
                    if kind not in answers.NAMED_KINDS and answers.not_fillable(answer)]
        self.assertEqual(too_long, ["availability"])                           # copied from the panel, never filled

    def test_only_a_short_value_in_plain_letters_is_ever_filled(self):
        """The read-only check of the Codex Pass 2 correction (2026-10-22): a line break the checks read as a space (a
        text box drops it), a look-alike or small capital outside the fixed list, an unassigned invisible character,
        and padding or entities that make the written text far longer than the text as read."""
        never = {
            "a line break inside a word": "Two weeks. I a\ngree to the priva\ncy policy.",
            "a carriage return": "Two weeks. I a\rgree.",
            "a tab": "Two\tweeks",
            "look-alike letters": "No. I a\u0261ree to the priva\u03f2y poli\u03f2y.",
            "small capitals": "\u026a \u1d00\u0262\u0280\u1d07\u1d07",
            "an unassigned invisible character": "I ag\u2065ree",
            "a no-break space": "Two\u00a0weeks",
            "zero-width padding": "Two weeks" + "\u200b" * 200,
            "entities": "Two weeks" + "&#8203;" * 20,
            "121 characters": "x" * 121,
            "a long s with a dot above, read as s, shown like an f": "Two weeks. I certi\u1e9by the above.",
            # the second read-only check: an entity that becomes a letter, and Latin-1 letters NFKD doesn't fold
            "an entity hiding a word": "I agree&ordf; to a background check&ordf;",
            "a legacy entity without its semicolon": "race&ethnicity",
            "an entity spelling a letter": "background check&#97;",
            "o with a stroke": "Yes. I c\u00f8nsent t\u00f8 a backgr\u00f8und check.",
            "ash": "I \u00e6gree",
            "eth": "\u00d0OB",
            "sharp s": "Stra\u00dfe",
            "spaces the checks collapse": "Two" + " " * 130 + "weeks",
            "a Latin letter without a decomposition": "\u0141ukasz",
            "a currency sign": "\u20ac75,000",
        }
        for name, value in never.items():
            self.assertIsNotNone(answers.not_fillable(value), name)
        for value in ("Yes", "Jos\u00e9 \u00c1lvarez", "Dvo\u0159\u00e1k", "$75,000\u2013$80,000 base",
                      "Two weeks\u2019 notice", "avery@example.invalid", "R&D and AT&T", "x" * 120):
            self.assertIsNone(answers.not_fillable(value), value)
        self.assertEqual(answers.not_fillable("\u0141ukasz"), "the character U+0141, which CareerOS doesn't fill")

    def test_every_character_a_filled_value_may_hold_reads_as_plain_ascii(self):
        """What the page shows is what the checks read, give or take an accent: a filled value is made only of
        characters that read as plain ASCII, each as the letter it is drawn on (its canonical base), so no character
        can hide a word from the wording list."""
        plain = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "`": "'"}
        odd = [hex(code) for code in range(0x10000)
               if answers.fill_char(chr(code)) and not chr(code).isspace()
               and answers.canonical(chr(code), entities=False)
               != plain.get(chr(code), unicodedata.normalize("NFD", chr(code))[0].lower())]
        self.assertEqual(odd, [])

    def test_a_filled_value_reads_to_the_checks_as_the_page_shows_it(self):
        """Character by character: lower case, an accent dropped, curly quotes and dashes plain, a backtick as an
        apostrophe, runs of spaces as one. Nothing a filled value holds changes its neighbours (a seeded sample)."""
        pool = [chr(code) for code in range(0x3000) if answers.fill_char(chr(code))]
        plain = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "`": "'"}
        rng = random.Random(20261022)
        for _ in range(3000):
            value = "".join(rng.choice(pool) for _ in range(rng.randint(1, 40)))
            if answers.not_fillable(value):
                continue
            seen = "".join(plain.get(ch, unicodedata.normalize("NFD", ch)[0]) for ch in value)
            self.assertEqual(answers.canonical(value), re.sub(r"\s+", " ", seen.lower()).strip(), ascii(value))

    def test_a_word_an_entity_hides_is_read_as_written_too(self):
        for text in ("I agree&ordf;", "race&ethnicity", "background check&#97;"):
            self.assertIsNotNone(answers.classify(text), text)


if __name__ == "__main__":
    unittest.main()
