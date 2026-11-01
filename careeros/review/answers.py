"""Reusable answers (P2, 2026-10-21; ADR 0009): your own answers to application questions, reused only where you said so.

They live in a vault file you write (vault/profile/answers.yaml), next to your approved facts. CareerOS reads it and
never writes it:

  documents:
    resume: documents/resume.pdf        # a path inside the vault; packages record its SHA-256
    cover_letter: null                  # optional
  answers:
    - id: work-authorization            # letters, digits, - and _ only; each id once
      kind: work_authorization          # what the question is about: one of KINDS (a contact field needs none)
      question: "Are you legally authorized to work in the United States?"
      phrasings: ["Are you authorized to work in the US?"]   # other wordings you accept as the same question
      answer: "Yes"                     # quoted: an unquoted Yes, No, 02134 or 2:30 is read as something else
      field: null                       # or a standard form field it fills: first_name, last_name, email, phone, ...
      facts: []                         # ids of approved facts the answer rests on
      approved: 2026-10-21              # when you approved this wording
      expires: 2027-04-21               # optional: after this date it needs your review again

An answer is used for the exact question it was written for, or for a phrasing you listed with it, compared in the
form normalize() gives (case, spacing, look-alike quotes and dashes, HTML entities, a leading * or -, a quote at either
end and a trailing ?, ., :, ! or * evened out). A model's judgment that two questions are similar is never enough.

Some questions are never meant to be answered from this file: consent, agreement and attestation, demographics, health,
disability and military service, signatures, background, criminal, credit and drug checks, identity documents and
salary history. You answer them on the form yourself. Two rules keep them out (second and third Claude reviews of the
Codex corrections, 2026-10-22):
- Deny by default. Each answer declares its `kind`, from a closed list (KINDS), and is used only where its own
  question and the form's question are each, as a whole, one of that kind's question shapes (KIND_SHAPES): a
  work-authorization answer only for "Are you legally authorized to work in the United States?" and its like, a salary
  answer only for "What are your salary expectations?" and its like. No shape is about the questions above, so an
  answer for one of them is never used, whatever kind it claims. A contact detail fills only its own standard field.
- A second line reads the wording. An answer is never used when its question, a phrasing, its own text (whatever its
  length), or the form's question or choices read as one of those categories (SENSITIVE_CATEGORIES). Both checks read a canonical
  form: HTML entities decoded, compatibility forms (fullwidth letters, ligatures, no-break spaces), accents and other
  combining marks, invisible formatting characters and blank letters dropped, a fixed list of common look-alike letters,
  quotes and dashes made plain,
  one space, lower case, no trailing punctuation. Wording varies, so neither check is complete: each holds back what it
  recognises and says which words held an answer back, but a sensitive question that fits a shape and avoids the
  wording list can still be answered. What bounds that: only the named kinds (NAMED_KINDS) have a free part in their
  shapes, the extension is never handed their answers (you copy them from the panel), and you review every form.
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import date
from pathlib import Path

import yaml

# Standard fields a form adapter can fill from an answer marked `field: ...`.
STANDARD_FIELDS = ("first_name", "last_name", "preferred_name", "email", "phone", "location", "linkedin", "website",
                   "github")
DOCUMENT_KINDS = ("resume", "cover_letter")
# The questions each kind answers: whole-question shapes, matched against the entire normalize()d question (fourth
# and fifth Claude reviews, 2026-10-22). A trigger word anywhere in a question was not enough: "visa" is in "Will you
# require visa sponsorship?" and in "What is your current visa status?". An answer is used only where its own question
# and the form's question each match one of its kind's shapes from start to end. Only the named kinds (NAMED_KINDS)
# have free parts: short names (a skill, a certification, a company, a school: a few words, no clause, no sentence),
# and a name that is about the person (TOPIC_VETO) is refused. Every other kind's shapes are closed: fixed words,
# numbers, and closed lists of languages and time zones (the verification of 2026-10-22 found a relocation reason, a
# commuting destination and a referral name carrying a protected subject). A contact detail is bound by its standard field instead. No shape is about consent,
# demographics, health, military service, signatures, checks, identity documents or pay history.
_US = r"(?:the )?(?:united states(?: of america)?|u\.?s\.?a?|us|usa|country(?: where (?:this|the) (?:job|role|position) is located)?)"
_WORD = r"[a-z0-9][a-z0-9&'/+#-]*"
_NAME = rf"{_WORD}(?: {_WORD}){{0,3}}(?: \([a-z0-9-]{{2,12}}\))?"   # up to four words, and an abbreviation: "(cpc)"
_SKILL = rf"{_WORD}(?:,? (?:and |or )?{_WORD}){{0,5}}(?: \([a-z0-9, -]{{2,24}}\))?"   # "sql and power bi", "icd-10 coding"
_ZONE = r"(?:eastern|central|mountain|pacific|atlantic|est|cst|mst|pst|edt|cdt|mdt|pdt|et|ct|mt|pt|utc|gmt)"
_LANGUAGE = (r"(?:english|spanish|french|german|portuguese|italian|mandarin|cantonese|chinese|japanese|korean|vietnamese|"
             r"tagalog|arabic|russian|hindi|polish|dutch)")
_THIS_JOB = r"(?:this|the) (?:role|position|job|opportunity)"
_OUR_SITE = r"(?:our|the|this) (?:job |work )?(?:location|office|area|city|site|worksite)"
KIND_SHAPES = {
    "work_authorization": [
        rf"(?:are you )?(?:currently )?(?:legally )?(?:authori[sz]ed|eligible|permitted) to work (?:in|for an employer in) {_US}"
        r"(?: for any employer)?(?: without (?:visa )?sponsorship)?",
        rf"do you have (?:the )?(?:legal )?(?:right|authori[sz]ation) to work in {_US}",
        r"(?:us |u\.s\. )?work authori[sz]ation",
    ],
    "sponsorship": [
        r"(?:will|do|would) you(?: now)?(?:,? or (?:will you )?in the future,?| or in the future)? (?:require|need)"
        r"(?: (?:employment )?visa)? sponsorship(?: for (?:employment )?(?:visa )?status| for (?:an? )?(?:employment|work) visa|"
        rf" to work in {_US})?(?: \(e\.g\.?,? h-1b(?: visa)?(?: status)?\))?",
        r"(?:will|do|would) you(?: now)?(?: or in the future)? (?:require|need) (?:an? )?(?:employer|company|us) to sponsor"
        r" (?:you|your (?:work )?visa)",
        r"(?:visa |immigration )?sponsorship (?:required|needed)",
    ],
    "start_date": [
        r"(?:what is your |your )?(?:earliest |expected |desired |preferred |anticipated )?(?:possible )?start date",
        r"when (?:can|could|would) you (?:start|begin|join(?: us)?)(?: (?:working|work))?(?: if (?:offered|hired))?",
        r"when (?:are you|would you be) available to (?:start|begin)(?: (?:work|working))?",
        r"(?:date )?available to start|availability to start|how soon (?:can|could) you start",
    ],
    "notice_period": [
        r"(?:what is your |your )?(?:current )?notice period",
        r"how (?:much notice|many weeks'? notice|long a notice period) (?:do|would) you (?:need to )?(?:give|have|require)"
        r"(?: (?:to )?your current employer)?",
    ],
    "availability": [
        r"(?:are you )?(?:available|able|willing) to work (?:full[- ]time|part[- ]time|weekends|evenings|nights|overtime|"
        r"holidays|on[- ]call|(?:a |the )?(?:standard|regular) (?:schedule|hours|business hours)|[0-9]{1,2} hours (?:a|per) week)"
        r"(?:(?:,| and| or|, and|, or) (?:full[- ]time|part[- ]time|weekends|evenings|nights|overtime|holidays|on[- ]call))*"
        r"(?: (?:if|as) (?:needed|required))?",
        r"(?:are you )?(?:able|available|willing) to work monday (?:through|to|-) friday(?:,? [0-9]{1,2}(?::[0-9]{2})? ?(?:am|pm|a\.m\.|p\.m\.)?"
        rf" (?:to|-) [0-9]{{1,2}}(?::[0-9]{{2}})? ?(?:am|pm|a\.m\.|p\.m\.)?(?: {_ZONE})?)?",
        r"(?:what|which) (?:hours|days|shifts) are you available(?: to work)?",
        r"(?:are you )?(?:seeking|looking for) (?:full[- ]time|part[- ]time)(?: or (?:full[- ]time|part[- ]time))?"
        r"(?: (?:work|employment))?",
        r"desired (?:employment type|schedule)",
        r"(?:are you )?open to (?:a )?(?:contract|contract-to-hire|temp-to-hire|temporary|full-time|part-time)"
        r" (?:position|role|assignment)",
    ],
    "schedule": [
        rf"(?:can you|are you able to|are you willing to) work (?:in )?(?:the )?(?:{_ZONE}|same|local|company's) time ?zone"
        r"(?: hours)?",
        r"(?:are you )?(?:comfortable|willing|able) (?:with|to work) (?:a )?(?:rotating|variable|flexible|shift)"
        r" (?:schedule|shifts?|hours)",
    ],
    "relocation": [
        rf"(?:are you |would you be )?(?:willing|open|able) to relocate(?: (?:for {_THIS_JOB}|to {_OUR_SITE}|if (?:needed|"
        r"required|offered)))?",
        rf"(?:would you |do you )?(?:consider|need) relocat(?:ing|ion)(?: (?:for {_THIS_JOB}|to {_OUR_SITE}))?(?: assistance)?",
        r"relocation",
    ],
    "work_location": [
        r"(?:are you )?(?:willing|able|comfortable|open) to (?:work|commute) (?:remotely|on[- ]?site|in[- ]office|in the office|"
        r"hybrid|from (?:our|the|an?) office|to (?:our|the|this) office)(?: [0-9] days (?:a|per) week)?",
        r"(?:are you )?(?:comfortable|happy) working (?:remotely|on[- ]?site|in (?:an|the) office|hybrid)",
        r"(?:are you )?(?:willing|able) to work (?:in (?:the )?office|on[- ]?site) [0-9] days (?:a|per) week",
        r"where are you (?:currently )?(?:located|based)|(?:current )?location(?: \(city\))?|current city",
        rf"are you (?:currently )?(?:located|based) in {_US}",
        r"do you have (?:a )?(?:reliable |stable )?(?:high-speed )?internet(?: connection)?|do you have a (?:dedicated|quiet|"
        r"private)(?:,? (?:distraction-free|quiet|private))? (?:home )?(?:workspace|office|work area)",
        r"what (?:city|state|city and state) do you (?:currently )?(?:live|reside) in",
        rf"do you (?:currently )?live within (?:a )?(?:commut(?:e|ing|able)|reasonable) distance(?: of {_OUR_SITE})?",
        r"(?:work (?:location|arrangement)|(?:remote|hybrid|on[- ]?site)) preference|preferred work (?:location|arrangement)",
    ],
    "travel": [
        r"(?:are you )?(?:willing|able|open) to travel(?: up to [0-9]{1,3} ?%(?: of the time)?|"
        r" (?:occasionally|frequently|as needed|as required|for work|overnight))?(?:,? (?:including )?overnight)?",
        r"what percentage of travel (?:are you|would you be) (?:willing|comfortable) (?:to do|with)",
        r"willingness to travel",
    ],
    "salary_expectation": [
        r"(?:what (?:are|is) your |your )?(?:desired |expected |target |requested |minimum )?(?:annual |hourly |base )?"
        r"(?:salary|pay|compensation|wage|rate)(?: range)? (?:expectations?|requirements?)(?: for this (?:role|position))?",
        r"(?:what is your |your )?(?:desired|expected|target|requested|minimum) (?:annual |hourly |base )?"
        r"(?:salary|pay|compensation|wage|rate)(?: range)?(?: for this (?:role|position))?",
        r"what (?:salary|pay|compensation)(?: range)? are you (?:looking for|seeking|expecting)(?: for this (?:role|position))?",
    ],
    "experience": [
        rf"(?:how many )?years of (?:professional |relevant |related |hands-on |direct )?experience (?:do you have )?"
        rf"(?:with|in|using|working (?:with|in|on|as)) {_SKILL}(?: do you have)?",
        rf"how many years of {_SKILL} experience do you have",
        rf"do you have (?:at least |a minimum of )?[0-9]{{1,2}}\+? years of (?:{_SKILL} )?experience(?: (?:with|in) {_SKILL})?",
        rf"how many years (?:have you|do you have) (?:worked|been working|of experience) (?:with|in|on|as) {_SKILL}",
        rf"(?:please )?(?:describe|tell us about|summari[sz]e|explain) your (?:professional |relevant )?experience "
        rf"(?:with|in|using|working with|as) {_SKILL}",
        rf"do you have (?:any )?(?:professional |hands-on |direct |prior |previous )?experience (?:with|in|using|working "
        rf"(?:with|in|on|as)) {_SKILL}",
        rf"(?:are you )?(?:proficient|familiar|experienced) (?:with|in) {_SKILL}",
        rf"what is your (?:level of )?(?:proficiency|experience|familiarity) (?:with|in) {_SKILL}",
    ],
    "previous_employment": [
        rf"(?:have you )?(?:ever )?(?:previously |formerly )?(?:worked|been employed) (?:for|at|with|by) (?:us|{_NAME})"
        r"(?: (?:before|previously|in the past))?",
        rf"(?:are you|were you|have you been) (?:a )?(?:current|former|current or former) (?:employee|contractor)"
        rf"(?: (?:of|at) {_NAME})?",
        r"(?:what is your |name of (?:your )?)?(?:current|most recent|previous) (?:employer|company|job title|title|position|role)",
    ],
    "motivation": [
        rf"why (?:do you want to|would you like to|are you interested in|are you applying to) (?:work (?:here|for us|at {_NAME}|"
        rf"for {_NAME})|join (?:us|our team|{_NAME})|this (?:role|position|job))",
        rf"why are you interested in (?:this (?:role|position|job|opportunity)|working (?:here|for us|at {_NAME}))",
        rf"what (?:interests|excites|attracts|draws) you (?:about|to) (?:this (?:role|position|job|opportunity)|{_NAME})",
        r"cover letter|why us|why this role|tell us (?:a bit )?about yourself",
        r"what makes you a (?:good|great|strong) fit(?: for this (?:role|position))?",
    ],
    "certification": [
        rf"(?:do you (?:have|hold)|are you) (?:an? |any )?(?:active |current |valid )?(?:{_NAME} )?(?:certification|certificate|"
        rf"certified|license|licensed|credential)s?(?: (?:in|as|for) {_NAME})?",
        rf"are you an? (?:certified|licensed|registered) {_NAME}",
        r"(?:which|what) (?:certifications|licenses|credentials) do you (?:have|hold)",
        r"(?:list (?:your|any) )?(?:relevant )?(?:professional )?(?:certifications|licenses|credentials)",
        r"(?:do you (?:have|hold)|are you (?:able|eligible) to obtain) (?:an? )?(?:active |current )?(?:(?:interim )?"
        r"(?:confidential|secret|top secret|ts|ts/sci|public trust|dod|doe|q|l) )?(?:security )?clearance",
    ],
    "education": [
        r"(?:what is your )?(?:highest )?(?:level of )?(?:education|degree)(?: (?:completed|attained|earned))?",
        r"what is the highest (?:level of education|degree) you have (?:completed|attained|earned)",
        rf"do you have (?:a|an) {_NAME} degree(?: (?:in|or) {_NAME})?",
        r"do you have a (?:high school )?diploma(?: or (?:a )?ged)?|do you have a ged",
        r"(?:what|which) degrees? do you (?:have|hold)",
        r"(?:school|university|college|institution)(?: name)?|field of study|major|discipline",
    ],
    "language": [
        r"(?:what|which) languages? (?:other than english )?do you (?:speak|read|write)(?: (?:fluently|professionally))?",
        rf"(?:are you |do you )?(?:fluent|proficient|bilingual) (?:in )?{_LANGUAGE}(?: and {_LANGUAGE})?",
        rf"do you speak {_LANGUAGE}(?: (?:fluently|professionally))?",
        r"languages?(?: spoken)?|language proficiency",
    ],
    "referral_source": [
        rf"how did you (?:hear|learn|find out) about (?:us|{_THIS_JOB})",
        r"(?:where|how) did you (?:find|see|hear about) (?:this|the) (?:job|role|position|posting|opening)",
        r"(?:referral )?source|referred by|were you referred(?: by an employee)?|(?:employee )?referral(?: name)?",
    ],
    "link": [
        r"(?:linkedin|github|portfolio)(?: profile)?(?: url| link)?|(?:personal|professional) (?:website|web site|url|link|"
        r"portfolio)(?: url| link)?",
        r"(?:link|url) to (?:your )?(?:portfolio|website|linkedin profile|github(?: profile)?|work samples)",
        r"website|portfolio|other website",
    ],
}
# A skill, certification, company or place named in a shape that is really about the person: never reused.
TOPIC_VETO = (r"\b(?:drugs?|substances?|alcohol|marijuana|cannabis|sobriety|arrest\w*|crimes?|criminal|convict\w*|jail|prison|"
              r"inmates?|correction\w*|warrants?|disab\w*|(?<!pre-existing )conditions?|illness\w*|mental|injur\w*|pregnan\w*|maternity|vaccin\w*|immuni\w*|covid\w*|flu|hepatitis|tb|tuberculosis|military|"
              r"active duty|armed|veterans?|reserves?|guard|uniformed|government|immigra\w*|visas?|citizen\w*|nationality|"
              r"refugee|asyl\w*|daca|tps|religio\w*|faith|church|gender|race|ethnic\w*|sexual\w*|unions?|tribes?|tribal|"
              r"members?|bankrupt\w*|debts?|credit(?! balances?)|liens?|judgments?|lived experience|family|spouse|"
              r"child\w*|parent\w*|age|born|birth|suspend\w*|revok\w*|disciplin\w*|denied(?! claims?)|sanction\w*|"
              r"encumb\w*|clean|standing|schedule a|rehabilitat\w*|bond\w*|expir\w*|number(?! research)|#|salary|pay|"
              r"paid(?! claims?)|earn\w*|wages?|rate|income|compensation|homeless\w*|housing|foster|poverty|welfare|"
              r"incarcerat\w*|addiction|abuse|domestic|trauma\w*|physical|fit(?:ness)?|medically|"
              r"psych\w*|police|fbi|doj|livescan|polygraph|suitability|hearing|cdib|indian|mmj|patients?|foreign|dd|214|"
              r"combat|infantry|usmc|usaf|usn|usarmy|rotc|deploy\w*|marines?|navy|army|air force|coast guard|"
              r"sheltered|depression|justice)\b")
# The labels a standard field may carry on a form (an empty label is read by the field's name alone), and the question
# a contact detail in answers.yaml must have.
FIELD_LABELS = {
    "first_name": r"(?:legal )?first name|given name|first",
    "last_name": r"(?:legal )?last name|surname|family name|last",
    "preferred_name": r"preferred (?:first )?name|nickname",
    "email": r"(?:your )?e-?mail(?: address)?",
    "phone": r"(?:your )?(?:mobile |cell |home |primary )?(?:phone|telephone)(?: number)?",
    "location": r"(?:current )?location(?: \(city\))?|city",
    "linkedin": r"linkedin(?: profile)?(?: url)?",
    "website": r"(?:personal )?website(?: url)?|portfolio(?: url)?",
    "github": r"github(?: profile)?(?: url)?",
}
# The kinds: a closed list. "contact" is bound by a standard field, not by wording.
KINDS = ("contact", *KIND_SHAPES)
_KIND_PATTERNS = {kind: [re.compile(p) for p in shapes] for kind, shapes in KIND_SHAPES.items()}
_TOPIC_VETO = re.compile(TOPIC_VETO)
_VETOED_KINDS = ("experience", "previous_employment", "motivation", "certification", "education")
_YES_NO_KINDS = ("work_authorization", "sponsorship")   # answered yes or no: anything else names an immigration status
# Kinds whose shapes name something (a skill, a credential, an employer, a school), the only kinds with a free part in
# their shapes: the extension is never handed their answers, you copy them from the panel (sixth Claude review,
# 2026-10-22: a name can always encode a protected subject).
NAMED_KINDS = ("experience", "previous_employment", "motivation", "certification", "education")
_FIELD_LABELS = {field: re.compile(pattern) for field, pattern in FIELD_LABELS.items()}
ANSWER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ANSWER_FILL_CHARS = 120   # a longer answer is prose: shown in the panel for you to copy, never filled (Codex Pass 2)
# The marks a value the extension fills may hold besides printable ASCII and accented Latin letters: curly quotes and
# dashes, which the checks read as plain ones. Read-only checks of the Codex Pass 2 correction (2026-10-22) found that
# a line break (a text box drops it), look-alike and unfolded letters (o with a stroke, eth, ash, small capitals),
# invisible or unassigned characters and HTML entities each let the checks read other words than the page shows.
_FILL_MARKS = "\u2018\u2019\u201c\u201d\u2013\u2014"
# The second line of defence: wording that is never answered from the file, by category, matched against normalize()
# text. Widened in the second and third Claude reviews (2026-10-22) against a corpus of real-world wordings
# (tests/test_sensitive_questions.py). Some patterns are narrowed by context where a routine question would otherwise
# be held back: "race condition", "veteran benefits", "cardiac arrest", "disability claims", "military health",
# "permission sets", "member retention", "salary expectations", a driver's license or passport you merely hold.
SENSITIVE_CATEGORIES = {
    "consent, agreement or attestation":
        r"consent|\bagree(?:s|d|ing)?\b|acknowledg|\battest|\bcertif(?:y|ies)\b|\bhereby\b|\bdeclar(?:e|es|ation)\b|"
        r"\baffirm|truthful|\btrue,? (?:and|&) (?:complete|correct|accurate)|\b(?:accurate|correct),? (?:and|&) complete|"
        r"\bto the best of (?:my|your) knowledge\b|\binformation (?:above|provided|given|i have (?:given|provided)) is\b|"
        r"\bfalse (?:statement|information|answer)|misrepresent|omission|\bperjury\b|\bpenalty of\b|"
        r"\bi (?:have read|confirm|authori[sz]e|acknowledge|agree|consent|certify|declare|affirm|attest)\b|"
        r"\bi understand (?:that|and (?:agree|accept|acknowledge))\b|\bi give (?:my )?(?:consent|permission|authori[sz])|"
        r"\bi (?:grant|allow|permit) (?:you|the company|[a-z]+ to)\b|"
        r"\bby (?:submitting|checking|clicking|signing|typing|agreeing|continuing|applying|providing|selecting)\b|"
        r"\bto confirm\b|\bplease confirm (?:that|you|the above|the information|your (?:agreement|consent|understanding))\b|"
        r"\bconfirm (?:that|the above|the information|you have read)\b|"
        r"\baccept(?:s|ed|ing)? (?:the|our|these|this|all|terms|conditions)\b|^i accept$|\bi accept (?:the|these|this|"
        r"all|your|terms|conditions)\b|^(?:i )?(?:accept|agree|decline|acknowledge|consent)(?:ed|d)?$|"
        r"\bterms (?:and|&) conditions\b|\bterms of (?:use|service|this)\b|\bthe terms\b|\barbitration\b|\bat[- ]will\b|"
        r"(?<!hipaa )\b(?:applicant|candidate|privacy|data protection|data privacy) (?:notice|policy|statement)\b|"
        r"\b(?:candidate|applicant) privacy\b|^(?:data|personal) privacy$|\byour (?:data|personal) privacy\b|"
        r"\bauthori[sz]e[sd]? (?:the|us|our|you|release|a)\b|\bauthori[sz]e\b(?!d)|"
        r"\bauthori[sz]ation (?:for|to) (?:us|the company|release|contact|obtain|verify|conduct|perform|run|check)\b|"
        r"\bpermission\b(?! sets?)|\b(?:may|can) we (?:contact|keep|retain|store|share|use|send|text|call|email|reach|"
        r"process|save|hold|consider)\b|\bcontact (?:your )?(?:current |previous |former |past |prior )?(?:employers?|"
        r"references|supervisors?|managers?)\b|\b(?:check|call|reach|verify) (?:your )?(?:professional |personal )?"
        r"references\b|\b(?:data|information|resume|cv|application|candidate) retention\b|\bretain (?:your|my|the) "
        r"(?:data|information|resume|cv|application|details)\b|\bon file\b|\bopt(?:-| )?(?:in|out)\b|"
        r"\bsms (?:updates?|messages?|notifications?|alerts?|texts?)\b|\b(?:by|via|receive) sms\b|\btext messages?\b|"
        r"\bmarketing (?:emails?|messages?|communications?|texts?|calls?|purposes)\b|\bfor marketing\b|"
        r"\btalent (?:community|network|pool)\b|\bfuture (?:opportunities|roles|openings|positions|vacancies)\b|"
        r"\bconsidered for (?:other|future)\b|\bother (?:positions|roles|openings)\b|"
        r"\b(?:processing|collection|use) of (?:your|my) (?:personal )?(?:data|information)\b|"
        r"\b(?:your|my) personal (?:data|information)\b|\bunder (?:the )?(?:gdpr|ccpa)\b|"
        r"(?<!hipaa )\b(?:ccpa|cpra|gdpr|privacy|eeo|eeoc|fcra|data|applicant) notice\b|\bnotice at collection\b|"
        r"\bapplicant (?:certification|statement|agreement|attestation|declaration)\b|"
        r"\bcertification and (?:agreement|authori[sz]ation|release)\b|\bdo you allow\b|\bshare your (?:profile|data|"
        r"information|details|resume|cv|application)\b|\bhold your details\b|\bkeep (?:my|your) details\b|"
        r"\bprocess your data\b|\bupdates by text\b|\bjob alerts?\b|\bnewsletter\b|\bkeep me (?:informed|updated)\b|"
        r"\brelease of information\b|^understood$|^privacy$|\bplease read and confirm\b|\bfalsification\b|"
        r"\bi'?ve read\b|\bhappy for (?:us|you) to\b|\btext you\b|\bin our database\b|\bkeep your profile\b",
    "demographics, citizenship or national origin":
        r"\bgender\b|(?<!age/)\bsex\b|\bmale\b|\bfemale\b|\bnon-?binary\b|\btransgender\b|\blgbt|\bsexual orientation\b|"
        r"\bpronouns?\b|\brace\b(?! condition| and ethnicity data)|\bracial\b|\braces\b|\bethnic(?!ity data)|\bhispanic\b|\blatin[oax]\b|\bindigenous\b|"
        r"\baboriginal\b|\bfirst nations\b|\bminorit(?:y|ies)\b|\bafrican american\b|\bblack or african\b|\basian\b|"
        r"\bpacific islander\b|\bnative (?:american|hawaiian)\b|\balaska native\b|\bcaucasian\b|"
        r"\bmarital\b|\bmarried\b|\breligio|"
        r"\bnational origin\b|\bnationality\b|\bcitizen(?:ship)?\b(?! verification)|\bcountry of (?:birth|origin|"
        r"citizenship)\b|\bplace of birth\b|\bwhere were you born\b|\bborn in\b|\bimmigration status\b|"
        r"\bdate of birth\b|\bbirth ?date\b|\bbirthday\b|\bdob\b|\byear of birth\b|\bhow old\b|\bwhat is your age\b|"
        r"\byour age\b|^age$|\b(?:40|forty) years (?:of age )?or older\b|\bage(?:d)? (?:40|forty)\b|"
        r"\bbest describes? you\b|\bidentify as\b|\bhow do you identify\b|\bself[- /]?identif|"
        r"\bare you a (?:wom[ae]n|man)\b|^(?:man|woman|male|female|white)$|\bwhite (?:or|/)|\bbipoc\b|"
        r"\b(?:person|people) of colou?r\b|\b(?:gay|lesbian|queer|bisexual)\b|\bcaste\b|\bheritage\b|\bimmigrant\b|"
        r"\bage (?:band|group|range|bracket)\b|^age(?: \(.*\))?$|\bfirst in (?:your|my) family\b|\bfee-paying\b|"
        r"\bfree school meals\b|\bsocio-?economic\b|\b(?:first|native) language\b|\bmother tongue\b|"
        r"\b(?:lived|resided) in (?:the )?(?:us|u\.?s\.?|united states|country|uk|canada)\b|"
        r"\bdo you have (?:any )?(?:children|dependents|kids)\b|\bnumber of (?:children|dependents)\b|"
        r"\bcaring responsibilities\b|\bparent or carer\b|\bfamily status\b|\b(?:your|current|present) visa (?:status|type|category)\b|\bvisa (?:expir\w*|denied|"
        r"revok\w*)\b|^visa (?:status|type|category)$|\bwhat (?:type of )?visa\b|immigration/visa|\b(?:us|u\.s\.|united states) resident\b|\bresident of the (?:us|united states)\b|"
        r"\bfirst[- ]gen\w*|\bfirst person in (?:your|my) family\b|\bhbcu\b|\bhistorically black\b|"
        r"\breduced[- ]price lunch\b|\bspeak at home\b|\bspouse\b|\bmaiden name\b|\bchild ?care\b|\bempowerment zone\b|"
        r"\bwotc\b|\bwork opportunity tax credit\b|\bover (?:40|forty)\b|\brefugee\b|\basyl(?:ee|um)\b|\bdaca\b|"
        r"\btps\b|\b(?:lawful )?permanent resident\b|\bcdib\b|\bdegree of indian blood\b|\bfederally recognized "
        r"tribe\b|\btribal (?:member\w*|"
        r"affiliation|enrollment)\b",
    "disability, health or military service":
        r"(?<!members with )(?<!patients with )\bdisab(?:led|ility|ilities)\b(?! (?:claims?|insurance|benefits?|management|"
        r"cases?|programs?|members?|patients?|populations?|outreach))|\b(?:receiving|receive|filed|file) (?:a |any )?disability\b|"
        r"\bhandicap|"
        r"\bimpairment|\b(?:reasonable )?accommodations?\b(?! requests?)|\breasonable adjustments?\b|"
        r"\b(?:your|any|a|my) (?:medical|health|mental health) (?:condition|history|information|issue)|"
        r"\b(?:physical|mental) (?:or (?:physical|mental) )?(?:condition|limitation)s?\b|\bmedical reasons\b|"
        r"\b(?:are you|have you been|were you) (?:fully )?vaccinated\b|\bvaccinated against\b|\bvaccination (?:status|record|"
        r"card)\b|\bproof of (?:covid-19 )?vaccination\b|\byour (?:vaccin|immuni[sz])\w*|\bimmuni[sz]ation (?:status|record)s?\b|"
        r"\bparental leave\b|\blived experience\b|\bactive duty\b|\bbranch of service\b|\breserve or guard\b|"
        r"\b(?:dot )?medical (?:certificate|card|examiner\w*|clearance)\b|\b(?:vaccination|vaccine|immuni[sz]ation|"
        r"tb test|flu shot|hepatitis b?) certificate\b|\bcertified as (?:fully )?vaccinated\b|\bschedule a\b|"
        r"\b(?:are you|you are|currently|planning to become) pregnant\b|\bpregnancy status\b|\bmaternity leave\b|\bveterans?\b(?!'? benefits)|\bmilitary\b(?! (?:health|families|family|benefits|time))|"
        r"\barmed forces\b|\bnational guard\b|\breserves\b|\buniformed services?\b|\bserved in the\b|"
        r"\bserve in the\b|\bservice member\b|\b(?:army|navy|air force|marines?|marine corps|coast guard|space force)\b|"
        r"\b(?:honorabl[ey]|dishonorabl[ey]|other than honorabl[ey]) discharg|\bdischarged from (?:the )?(?:military|"
        r"service|army|navy)\b|\bdd[- ]?214\b|\bdischarge status\b|\bblind\b|\bdeaf\b|\bvisually impaired\b|"
        r"\b(?:health|psych\w*|physical|medical|tb|fitness for duty) (?:clearance|certificate|card|exam)s?\b|\bmedically fit\b|\bfit for duty\b|"
        r"\b(?:usmc|usaf|usn|rotc|infantry|combat)\b|\bmmj\b|\bmedical marijuana\b|"
        r"\bhard of hearing\b|\bneurodiver|\badhd\b|\bautis|\bdyslex|"
        r"\blong-term (?:health )?conditions?\b|\bphysical limitations?\b|\bsupport to attend\b|"
        r"\badjustments? (?:to|in|for) the (?:recruitment|interview|application)\b|\bsubstantially limits\b|"
        r"\bmajor life activit|\b(?:been|were|are you|ever) diagnosed\b|\bdiagnosed with\b|\byour diagnos\w*",
    "signatures":
        r"signature|\be-? ?sign(?:ed)?\b|\bsign(?:ed|ing)? (?:here|below|above|this|your name)\b|"
        r"\b(?:name|initials) to sign\b|\bsign (?:this|the) (?:form|application)\b|"
        r"\btype (?:in )?(?:your )?(?:full )?(?:legal )?name\b|\btyped name\b|\bfull legal name\b|\binitials\b|"
        r"\binitial here\b|^sign$|\b(?:name of applicant|applicant name) and date\b",
    "background, criminal, credit and drug checks":
        r"\bcriminal\b|\bcrimes?\b|\bconvict(?:ed|ion|ions)?\b|\bfelon(?:y|ies)?\b|\bmisdemeanou?rs?\b|\barrested\b|"
        r"(?<!cardiac )(?<!respiratory )\barrests?\b|\bguilty\b|\bno contest\b|\bnolo contendere\b|"
        r"\bpending charges?\b|\bcharged with\b|\boffen[cs]es?\b|\bprobation\b|\bparole\b|\bincarcerat|\bimprison|"
        r"\bjail\b|\bprison\b|\bexpunge|\bsex offender|\bdui\b|\bdwi\b|\bdriving under the influence\b|\bbankrupt|"
        r"\bbackground (?:check|screen|screening|investigation|verification)s?\b(?! (?:vendors?|providers?|"
        r"process(?:es|ing)?|software|companies|industry))|\bcredit (?:check|history|report|screening)s?\b(?! analy)|"
        r"\bfair credit reporting\b|\bfcra\b|\bconsumer reports?\b|\bdrug (?:test|screen|screening|testing)s?\b|"
        r"\balcohol (?:test|screen)|\bpre-?employment (?:screening|drug|test)|\bmotor vehicle record|"
        r"\bfingerprint(?:s|ing|ed)?\b|\bexclu(?:ded|sion) (?:list|from (?:participation|federal))\b|"
        r"\b(?:listed (?:on|in)|on the|excluded by) (?:the )?oig\b|\boig exclusion list\b|\bsystem for award management\b|"
        r"\bsam\.gov\b|\b(?:licen[cs]e|clearance)\b[^?.]{0,30}\b(?:suspended|revoked)\b|\bdisciplinary action\b|"
        r"\bdebar|\bsanction(?:ed|s)? (?:list|by)\b|\bduis\b|\bdwis\b|\b(?:outstanding|open|active) warrants?\b|"
        r"\bby the police\b|\bpolice (?:record|caution|check)\b|\bhave (?:a|any) (?:criminal |police )?record\b|"
        r"\bundergo (?:a |any )?screenings?\b|\btested for (?:drugs|alcohol)\b|\bmarijuana\b|\bcannabis\b|"
        r"\b(?:use|used|using|possess\w*|sell\w*|sold) (?:of )?(?:any )?(?:illegal drugs?|controlled substances?|illegal "
        r"substances?)\b|\billegal (?:drugs?|substances?)\b|\bdrugs? other than (?:as )?prescribed\b|\bdo you smoke\b|\btobacco\b|\bnicotine\b|"
        r"\bmoving violations?\b|\btraffic (?:violations?|tickets?)\b|\bdriving record\b|\bwith(?:out)? (?:no |any )?"
        r"violations?\b|\bno violations\b|"
        r"\bfiled (?:a|any) workers'? comp|\b(?:background|fbi|doj|police|polygraph|livescan|live scan|"
        r"suitability|child abuse) (?:clearance|certificate|card)s?\b",
    "identity documents":
        r"\bsocial security (?:number|#|no\b|card)|\bssn\b|\bss ?#|\blast (?:4|four) (?:digits )?of (?:your )?"
        r"(?:social|ssn)\b|\byour social\b(?! media)|\bsocial insurance\b|\(sin\)|\bsin (?:#|no\b|number)|"
        r"\bnational insurance\b|\bnational id|\bpassport (?:#|no\b|num|number|details)|^passport$|"
        r"\bdriv(?:er'?s?|ing) licen[cs]e ?(?:state|#|no\b|num|number)|\blicen[cs]e (?:#|no\b|number)|"
        r"\btax(?:payer)? (?:id|identification)\b|\bitin\b|\bgovernment(?:-issued)? id|\balien (?:registration )?number|"
        r"\ba-number\b|\bgreen card\b|\bvisa number\b|\bi-9\b|\be-verify\b|\bsoc\.? ?sec\b|\bdl ?#|"
        r"\b(?:state |national |government |photo )?id (?:number|#|no)\b|\bid ?#|^tin$|\btin (?:#|number)\b|\buscis\b|"
        r"\bi-94\b|\b(?:permanent )?resident card\b|\bmedicare (?:number|#|id|card)\b|\byour medicaid (?:number|#|id)\b|"
        r"^medicaid (?:number|#|id)(?: number)?$|\bead\b|\bemployment authori[sz]ation document\b|\bnaturali[sz]ation\b|"
        r"\bpassport (?:expir|issu)\w*|\bissued your passport\b|"
        r"\bphoto of your id\b|\bcopy of your (?:id|passport|licen[cs]e)\b|\bpassport (?:id|copy|scan|photo)\b",
    "salary history":
        r"\b(?:current|most recent|previous|prior|last|past|former)(?: annual| base| hourly| total| yearly| monthly)? "
        r"(?:salary|compensation|pay|base|wage|rate|earnings|income|ctc)\b(?! expectations?| requirements?| range)|"
        r"\bsalary history\b|\bwhat (?:is|was) your salary\b|\bcurrently (?:earn|make)\b|\bwhat do you (?:currently )?"
        r"(?:earn|make)\b|\bhow much (?:do|did) you (?:currently )?(?:make|earn)\b|\bcurrently making\b|"
        r"^(?!.*\b(?:expect|desired|target|requested|requirements?|range|looking for|seeking|minimum|preferred|"
        r"negotiable)).*?\b(salary(?! data)|(?<!workers' )(?<!workers )compensation(?! claims?)|pay(?![- ]for[- ]performance)|"
        r"paid(?! (?:and (?:denied|unpaid) )?claims?)|wages?(?! and salary)|earnings|income|ctc|remuneration)\b|"
        r"\bwere you (?:earning|making|paid)\b|\bdid you (?:earn|make)\b|\bpresent (?:salary|pay)\b|\btotal comp\b|"
        r"\bw-?2 (?:earnings|income)\b|\byour (?:final |ending |current |last )?(?:hourly )?(?:rate|pay)\b(?! "
        r"(?:expectations?|requirements?|range))|\bfinal (?:hourly )?rate\b",
}
_CATEGORIES = [(name, re.compile(pattern)) for name, pattern in SENSITIVE_CATEGORIES.items()]
# Quotes and dashes a pasted form label may carry, as their plain forms, and common look-alike letters.
_PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'",
                        "`": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"', "\u2010": "-",
                        "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
                        "\u2043": "-", "\u1806": "-",
                        "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
                        "\u0445": "x", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04cf": "l",
                        "\u03b1": "a", "\u03bf": "o", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
                        "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x"})
# Letters and symbols that print as nothing: Hangul fillers, the braille blank, a musical null.
_BLANKS = "\u115f\u1160\u3164\uffa0\u2800\U0001d159"
_BLANK = dict.fromkeys(map(ord, _BLANKS))
_BLANK_AS_SPACE = dict.fromkeys(map(ord, _BLANKS), " ")


def canonical(text: str, blanks: dict = _BLANK, entities: bool = True) -> str:
    """The text as the checks read it: HTML entities decoded (unless `entities` is False); lower case; compatibility
    forms (fullwidth letters, ligatures, no-break spaces); accents and other combining marks, invisible format
    characters and blank letters dropped; common look-alike letters, quotes and dashes made plain; one space."""
    text = str(text or "")
    text = unicodedata.normalize("NFKD", (html.unescape(text) if entities else text).lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Mn", "Me", "Cf"))
    text = unicodedata.normalize("NFKC", text.translate(blanks).translate(_PLAIN))
    return re.sub(r"\s+", " ", text).strip()


def normalize(question: str) -> str:
    """canonical(), without trailing punctuation: how questions are compared and read ("Passport:" is "passport")."""
    return _trim(canonical(question))


def _trim(text: str) -> str:
    return text.lstrip(" *-\"'").rstrip(" *?.:!\"'")


def classify(text: str) -> tuple[str, str] | None:
    """(category, the words that matched) when the text reads as a question never answered from the file, else None."""
    # and as written: an entity can hide a word ("I agree&ordf;" decodes to "i agreea"), the page shows it as written
    forms = {normalize(text), _trim(canonical(text, _BLANK_AS_SPACE)), _trim(canonical(text, entities=False))}
    for name, pattern in _CATEGORIES:
        for words in forms:
            hit = pattern.search(words)
            if hit:
                return name, (hit.group(1) if pattern.groups and hit.group(1) else hit.group(0))
    return None


def sensitive(question: str) -> bool:
    return classify(question) is not None


def fits(kind: str, question: str) -> bool:
    """Is the whole question one of this kind's shapes (and, where a shape names a skill, a certification or a company,
    not about the person)? A contact detail is bound by its field, not its wording."""
    if kind == "contact":
        return True
    words = normalize(question)
    return any(p.fullmatch(words) for p in _KIND_PATTERNS[kind]) \
        and not (kind in _VETOED_KINDS and _TOPIC_VETO.search(words))


def kinds_for(question: str) -> list[str]:
    """The kinds a question reads as (none for anything the file never answers)."""
    if sensitive(question):
        return []
    return [kind for kind in KIND_SHAPES if fits(kind, question)]


def _reason(where: str, hit: tuple[str, str]) -> str:
    return f"{where} reads as {hit[0]} ('{hit[1].strip()}')"


def blocked(entry: dict) -> str | None:
    """Why an answer is never used, whatever form question or field it would match, or None: its question or a
    phrasing reads as a sensitive category, its own text does, whatever its length (an answer such as "I agree" is
    consent wherever it is used), or its question doesn't read as the kind it declares."""
    texts = [("its question", entry["question"]), *(("a phrasing", p) for p in entry["phrasings"])]
    # and its own text, whatever its length: a long answer carries a consent as surely as a short one (independent
    # Codex review, Pass 2 of 4787e2a: a 121-character start date with "I agree to the privacy policy" was filled)
    texts.append(("its answer", entry["answer"]))
    for where, text in texts:
        hit = classify(text)
        if hit:
            return _reason(where, hit)
    if not fits(entry["kind"], entry["question"]):
        return f"its question doesn't read as {entry['kind']}: choose the kind it is, or answer it on the form"
    if entry["kind"] in _YES_NO_KINDS and normalize(entry["answer"]) not in ("yes", "no"):
        return f"a {entry['kind']} answer is a plain Yes or No: a status in it is yours to give on the form"
    if entry["field"] in _FIELD_LABELS and not _FIELD_LABELS[entry["field"]].fullmatch(normalize(entry["question"])):
        return f"its question doesn't read as the {entry['field']} field: name the field the way a form labels it"
    return None


def fill_char(ch: str) -> bool:
    """A character the checks read as the page shows it: printable ASCII, a curly quote or dash, or a Latin letter
    with accents (read as the plain letter). Everything a filled value holds is one of these."""
    if " " <= ch <= "~" or ch in _FILL_MARKS:
        return True
    # canonical decomposition only: a compatibility fold would read the long s with a dot above (it looks like an f)
    # as "s" (owner-authorized read-only re-check, 2026-10-23)
    letters = unicodedata.normalize("NFD", ch)
    return len(letters) > 1 and letters[0].isascii() and letters[0].isalpha() \
        and all(unicodedata.category(mark) == "Mn" for mark in letters[1:])


def not_fillable(value: str) -> str | None:
    """Why the extension is never handed this value (you copy it from the panel instead), or None: it is prose (over
    ANSWER_FILL_CHARS as written; the checks never read it longer), it holds an HTML entity, or a character the checks
    would read as something other than the page shows (fill_char)."""
    if len(value) > ANSWER_FILL_CHARS:
        return f"over {ANSWER_FILL_CHARS} characters"
    if html.unescape(value) != value:
        return "an HTML entity, which the page would show as written"
    odd = next((ch for ch in value if not fill_char(ch)), None)
    if odd is not None:
        return f"the character U+{ord(odd):04X}, which CareerOS doesn't fill"
    return None


def sensitive_entry(entry: dict) -> bool:
    return blocked(entry) is not None


def form_blocked(label: str, options=None) -> str | None:
    """Why a form question is never answered from the file, or None: its label or any of its choices reads as a
    sensitive category (an employer may put the consent in the choices: "Yes, I agree to the privacy notice")."""
    hit = classify(label)
    if hit:
        return _reason("the form's question", hit)
    for option in options or []:
        hit = classify(option)
        if hit:
            return _reason("a choice on the form", hit)
    return None


def _date(value, what: str, answer_id: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"answer_invalid: {what} must be a date (YYYY-MM-DD) ({answer_id})") from exc


def _text(value, what: str, answer_id: str) -> str:
    """A value the file gives as text. YAML reads an unquoted Yes as true, 02134 as 1116 and 2:30 as 150: those are
    refused, not turned back into text (third Claude review, 2026-10-22)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"answer_invalid: {what} must be text: put it in quotes ({answer_id})")
    return value.strip()


def load(path: Path | None) -> dict:
    """{documents: {kind: vault-relative path}, answers: [...]}; empty when the file is missing. Refuses anything it
    can't read exactly: a missing id, question, answer or approval date; a question or answer that isn't quoted text
    where YAML would read it as something else; an unknown field or document kind; a phrasing list that isn't a list;
    two answers for one question; two answers with one id (a later one would otherwise stand in for the first after
    matching: Codex review of ac83e00, 2026-10-22); an id that isn't plain ASCII letters, digits, - and _; and a kind
    CareerOS doesn't reuse answers for (Claude reviews, 2026-10-22)."""
    if path is None or not Path(path).exists():
        return {"documents": {}, "answers": []}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("answer_invalid: the file must hold 'documents' and 'answers'")
    documents = data.get("documents") or {}
    if not isinstance(documents, dict) or set(documents) - set(DOCUMENT_KINDS):
        raise ValueError(f"answer_invalid: documents may name only {', '.join(DOCUMENT_KINDS)}")
    docs = {}
    for kind, value in documents.items():
        if value in (None, ""):
            continue
        rel = Path(str(value))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"answer_invalid: {kind} must be a path inside the vault")
        docs[kind] = rel.as_posix()
    out: list[dict] = []
    seen: dict[str, str] = {}
    ids: set[str] = set()
    for item in data.get("answers") or []:
        if not isinstance(item, dict):
            raise ValueError(f"answer_invalid: {item!r}")
        raw_id = item.get("id")
        answer_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
        question = _text(item.get("question"), "question", answer_id)
        text = _text(item.get("answer"), "answer", answer_id)
        if not answer_id or not question or not text:
            raise ValueError(f"answer_invalid: each answer needs an id, a question and an answer ({answer_id or question})")
        if not answer_id.isascii() or not ANSWER_ID.fullmatch(answer_id.lower()):
            raise ValueError(f"answer_invalid: an id may use only letters, digits, - and _ ({answer_id!r})")
        if answer_id.lower() in ids:
            raise ValueError(f"answer_invalid: two answers have the id {answer_id}")
        ids.add(answer_id.lower())
        phrasings, facts = item.get("phrasings") or [], item.get("facts") or []
        if not isinstance(phrasings, list) or not all(isinstance(p, str) for p in phrasings) \
                or not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
            raise ValueError(f"answer_invalid: phrasings and facts must be lists of text ({answer_id})")
        field = item.get("field")
        if field is not None and field not in STANDARD_FIELDS:
            raise ValueError(f"answer_invalid: unknown field {field!r} ({answer_id})")
        kind = item.get("kind") or ("contact" if field else None)
        if kind not in KINDS or (kind == "contact") != bool(field):
            raise ValueError(f"answer_invalid: {answer_id} needs a kind CareerOS reuses answers for: "
                             f"{', '.join(KIND_SHAPES)} (a contact detail names its field instead). Questions about "
                             "consent, demographics, health or military service, signatures, checks, identity "
                             "documents or pay history have no kind: answer them on the form")
        approved = _date(item.get("approved"), "approved", answer_id)
        if approved is None:
            raise ValueError(f"answer_invalid: approved (the date you approved this wording) is required ({answer_id})")
        expires = _date(item.get("expires"), "expires", answer_id)
        listed = [p.strip() for p in phrasings if p.strip()]
        entry = {"id": answer_id, "kind": kind, "question": question, "phrasings": listed,
                 "answer": text, "field": field, "facts": [f.strip() for f in facts if f.strip()],
                 "approved": approved.isoformat(), "expires": expires.isoformat() if expires else ""}
        for key in [normalize(question), *map(normalize, listed)] + ([f"field:{field}"] if field else []):
            if key in seen and seen[key] != answer_id:
                raise ValueError(f"answer_invalid: {seen[key]} and {answer_id} answer the same question")
            seen[key] = answer_id
        out.append(entry)
    return {"documents": docs, "answers": out}


def _current(entry: dict, today: str) -> bool:
    return not entry["expires"] or entry["expires"] >= today


def usable(entry: dict, today: str) -> bool:
    """Current and not held back: what a package may offer you to copy."""
    return _current(entry, today) and not sensitive_entry(entry)


def withheld(entry: dict, today: str) -> str | None:
    """Why an answer isn't offered for copying now, or None."""
    reason = blocked(entry)
    if reason:
        return "held back: " + reason
    if not _current(entry, today):
        return f"expired {entry['expires']}: review it in answers.yaml"
    return None


def _held(status: str, reason: str) -> dict:
    return {"status": status, "answer": None, "via": None, "blocked": reason}


def find(question: str, library: dict, today: str, options=None) -> dict:
    """{status, answer, via, blocked} for one question: matched (exact question or one of your phrasings, and the
    question reads as the answer's kind), expired (your answer needs review), sensitive (you answer it on the form;
    `blocked` says which words held it back), kind_mismatch (the form's question doesn't read as your answer's kind)
    or missing."""
    reason = form_blocked(question, options)
    if reason:
        return _held("sensitive", reason)
    key = normalize(question)
    for entry in library["answers"]:
        if entry["field"]:   # a contact detail fills its own standard field only (find_field), never a custom question
            continue
        via = "question" if normalize(entry["question"]) == key else \
            "phrasing" if key in map(normalize, entry["phrasings"]) else None
        if via:
            reason = blocked(entry)
            if reason:
                return _held("sensitive", f"your answer {entry['id']}: {reason}")
            if entry["kind"] in _YES_NO_KINDS and options \
                    and not all(normalize(o) in ("yes", "no") for o in options):
                return _held("kind_mismatch", f"the form's choices for this {entry['kind']} question aren't a yes or a "
                                              "no: a status list is yours to answer")
            if not fits(entry["kind"], question):
                return _held("kind_mismatch", f"the form's question doesn't read as {entry['kind']}, the kind of your "
                                              f"answer {entry['id']}")
            return {"status": "matched" if _current(entry, today) else "expired", "answer": entry, "via": via,
                    "blocked": None}
    return {"status": "missing", "answer": None, "via": None, "blocked": None}


def find_field(field: str, library: dict, today: str, label: str = "", options=None) -> dict:
    """The answer marked as filling a standard field (first_name, email...), with the same statuses as `find`. The
    form's own label and choices are read too: a field the form labels as a signature or a consent is yours to fill."""
    reason = form_blocked(label, options)
    if reason:
        return _held("sensitive", reason)
    if label and field in _FIELD_LABELS and not _FIELD_LABELS[field].fullmatch(normalize(label)):
        return _held("kind_mismatch", f"the form labels this {field} field '{label}', which isn't how that field reads")
    for entry in library["answers"]:
        if entry["field"] == field:
            reason = blocked(entry)
            if reason:
                return _held("sensitive", f"your answer {entry['id']}: {reason}")
            return {"status": "matched" if _current(entry, today) else "expired", "answer": entry, "via": "field",
                    "blocked": None}
    return {"status": "missing", "answer": None, "via": None, "blocked": None}
