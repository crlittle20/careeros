"""Saved job-alert emails become leads; readable job-board links are graded. Synthetic emails only, no network."""
import contextlib
import email
import email.policy
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

import httpx

from careeros.cli import _import_alert
from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.discovery.alerts import canonical_job_url, parse_eml
from careeros.discovery.http import HttpClient
from careeros.pipeline import Context
from careeros.qualify.profile import load_profile

from tests import postings
from tests.helpers import JOB_MATRIX
from tests.test_pipeline import gh_job
from tests.test_saved import POLICY

FIXTURES = Path(__file__).parent / "fixtures"
LI = "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D%3D&refId=xyz&lipi=urn&midToken=t"

ALERT_HTML = f"""<html><head><style>.a{{color:red}} Business Analyst</style></head><body>
<table><tr><td><a href="{LI}"><img src="logo.png"></a></td>
<td><a href="{LI}&trk=title">Claims Business Analyst</a><br>Synthetic Health · United States (Remote)</td></tr>
<tr><td><a href="{LI}&trk=button">View job</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099999999/?trackingId=q">Registered Nurse</a><br>Synth Hospital · Dallas, TX</td></tr>
<tr><td><a href="https://click.example.net/r?url=https%3A%2F%2Fjob-boards.greenhouse.io%2Fsynth%2Fjobs%2F555%3Fgh_src%3Dmail">Pharmacy Benefits Systems Analyst</a> Synthetic Health · Remote</td></tr>
<tr><td><a href="https://www.indeed.com/rc/clk/dl?jk=0a1b2c3d4e5f6a7b&from=ja&tk=zz">PBM Operations Analyst</a> Payer Co · Remote</td></tr>
<tr><td><a href="https://careers.northgategroup.com/job/123456/claims-analyst-remote/">Claims Analyst</a> Northgate · Remote</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/psettings/email-unsubscribe?x=1">Unsubscribe</a>
<a href="mailto:someone@example.com">Contact</a> <a href="javascript:alert(1)">Business Analyst</a></td></tr>
</table></body></html>"""


def alert_email(html_body=ALERT_HTML, text_body=None) -> bytes:
    message = EmailMessage()
    message["From"] = "LinkedIn Job Alerts <jobalerts@example.com>"
    message["To"] = "candidate@example.com"
    message["Subject"] = "Claims Business Analyst: Synthetic Health and 4 more"
    message["Date"] = "Sat, 10 Oct 2026 08:15:00 -0500"
    message.set_content(text_body or "plain part")
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return bytes(message)


AGENCY_TRACKER = "https://click.email.harborstaffing.com/test"
AG_1 = f"{AGENCY_TRACKER}?token=synthetic-1"
AG_2 = f"{AGENCY_TRACKER}?token=synthetic-2"


def agency_component(size, content):
    return f'<table class="stylingblock-content-wrapper"><tr><td style="font-size: {size}px">{content}</td></tr></table>'


def agency_card(title, url, location="Remote", description="Sample opening with a short description."):
    return (agency_component(18, f"<b>{title}</b>") + agency_component(14, location)
            + agency_component(14, description)
            + agency_component(14, f'<a href="{url}">Apply Now &rarr;</a>'))


def agency_email(body=None, sender=None, subject="Your jobs", bulk=True):
    sender = sender or "@".join(("synthetic", "email.harborstaffing.com"))
    message = EmailMessage()
    message["From"] = f"Job Alerts <{sender}>"
    message["To"] = "candidate@example.com"
    message["Subject"] = subject
    if bulk:
        message["List-Unsubscribe"] = "<https://example.invalid/unsubscribe>"
    footer = f'<tr><td><a href="{AGENCY_TRACKER}?token=synthetic-footer">Unsubscribe</a></td></tr>'
    body = agency_card("Claims Analyst", AG_1) + agency_card("Claims Analyst", AG_2) if body is None else body
    message.set_content(f"<html><body><table><tr><td>{body}</td></tr>{footer}</table></body></html>", subtype="html")
    return bytes(message)


EXTRA_JOB = f"{AGENCY_TRACKER}?token=synthetic-9"   # a job a browser would show and the reader must not miss


def agency_mail(html_body, plain=None, subject="Your jobs"):
    """An agency digest with this HTML (and, when given, a plain-text alternative a mail client may show instead)."""
    message = EmailMessage()
    message["From"] = "Job Alerts <" + "@".join(("synthetic", "email.harborstaffing.com")) + ">"
    message["To"] = "candidate@example.com"
    message["Subject"] = subject
    message["List-Unsubscribe"] = "<https://example.invalid/unsubscribe>"
    if plain is None:
        message.set_content(html_body, subtype="html")
    else:
        message.set_content(plain)
        message.add_alternative(html_body, subtype="html")
    return bytes(message)


@contextlib.contextmanager
def recorded(data: bytes):
    """The synthetic agency's entry with this digest's own layout recorded, as `careeros agency-layout` records it from
    a real one: the digest's structure (styles, elements) is then the template's, and only what is under test decides."""
    from unittest import mock
    from careeros.core import private_sites
    from careeros.discovery.alerts import agency_layout_report
    entry = private_sites.agency_digests()[0]._replace(layout=agency_layout_report(data)["layout"])
    with mock.patch.object(private_sites, "agency_digests", lambda: (entry,)):
        yield


def digest_html(style="", footer=None, head="", body=None):
    """The synthetic digest's markup (one card and the footer link), with a style sheet and head of the test's own."""
    footer = footer or f'<a href="{AGENCY_TRACKER}?token=synthetic-footer">Unsubscribe</a>'
    body = agency_card("Claims Analyst", AG_1) if body is None else body
    return (f"<html><head>{head}<style>{style}</style></head><body><table><tr><td>{body}</td></tr>"
            f"<tr><td>{footer}</td></tr></table></body></html>")


class HiddenLabelTests(unittest.TestCase):
    """Codex Pass 3, CR-AD-V2-01: a digest whose style can hide a link's text, or show other text in its place, is held:
    the reader would read a label a browser does not show (a job behind "Unsubscribe"). The red team before Pass 4 found
    more ways than any banned list holds, so the recorded layout now covers the digest's structure too: its styles and
    its elements, with their attribute names. Anything a recorded template does not have holds the digest."""

    def read(self, data):
        from careeros.discovery.alerts import parse_eml_bytes
        return parse_eml_bytes(data)

    def test_a_link_moved_under_another_style_changes_the_layout(self):
        """Codex Pass 4, CR-AD-V2-01: the same styles and elements, swapped between two places: a card or a link moved
        behind a hidden element. Where each style is counts, not only which styles exist."""
        card = agency_card("Claims Analyst", AG_1)
        footer = f'<a href="{AGENCY_TRACKER}?token=synthetic-view">View online</a>'
        hide, show = '<div style="display:none">', '<div style="display:block">'
        css = ".off{display:none} .on{display:block}"
        cases = {
            "a card behind a hidden style": (f"{hide}Preview text</div>{show}{card}</div>",
                                             f"{show}Preview text</div>{hide}{card}</div>", ""),
            "two links swapping styles": (f"{hide}{footer}</div>{show}{card}</div>", f"{show}{footer}</div>{hide}{card}</div>", ""),
            "a card behind a hidden class": (f'<div class="off">Preview text</div><div class="on">{card}</div>',
                                             f'<div class="on">Preview text</div><div class="off">{card}</div>', css),
        }
        for name, (template, swapped, style) in cases.items():
            with self.subTest(name):
                with recorded(agency_mail(digest_html(style=style, body=template))):
                    self.assertEqual(self.read(agency_mail(digest_html(style=style, body=template))).digest_status, "complete")
                    alert = self.read(agency_mail(digest_html(style=style, body=swapped)))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "layout"))

    def test_a_link_whose_image_changes_changes_the_layout(self):
        """Codex Pass 4, CR-AD-V2-01: the same alt text over another picture (a data: image showing a job's name)."""
        def footer(picture, token="synthetic-footer"):
            return f'<a href="{AGENCY_TRACKER}?token={token}"><img src="{picture}" alt="Unsubscribe"></a>'

        start = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http://www.w3.org/2000/svg%22%3E"
        marked = start + "%3C!--?--%3E"   # a picture whose text comes after a "?": the same up to it
        cases = {"another picture": (start + "Unsubscribe%3C/svg%3E", start + "View%20Reporting%20Analyst%20job%3C/svg%3E"),
                 "text after a question mark": (marked + "Unsubscribe%3C/svg%3E",
                                                marked + "View%20Reporting%20Analyst%20job%3C/svg%3E")}
        for name, (template, swapped) in cases.items():
            with self.subTest(name), recorded(agency_mail(digest_html(footer=footer(template)))):
                self.assertEqual(self.read(agency_mail(digest_html(footer=footer(template)))).digest_status, "complete")
                alert = self.read(agency_mail(digest_html(footer=footer(swapped, "synthetic-9"))))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "layout"))

    def test_the_styles_real_digests_use_read_in_full_once_recorded(self):
        for style in ("", ".preheader{display:none;max-height:0;overflow:hidden;mso-hide:all}",
                      "a{color:inherit;text-decoration:none} a span{font-family:Arial,sans-serif}",
                      "@media screen and (max-width:600px){.mobile-only{display:none!important;width:0}}",
                      "td{justify-content:center;align-content:start} img{border:0;height:auto}",
                      "th{display:none} #preheader{visibility:hidden}"):
            with self.subTest(style=style):
                data = agency_mail(digest_html(style, body=agency_card("Claims Analyst", AG_1)
                                               + '<div class="preheader" id="preheader">New jobs for you</div>'))
                with recorded(data):
                    alert = self.read(data)
                self.assertEqual((alert.digest_status, len(alert.links)), ("complete", 1))
                other = agency_mail(digest_html(style + " u{color:red}", body=agency_card("Claims Analyst", AG_1)
                                                + '<div class="preheader" id="preheader">New jobs for you</div>'))
                with recorded(data):   # any change of the recorded structure: held (the red team before Pass 4)
                    self.assertEqual(self.read(other).digest_reason, "layout")

    def test_a_link_whose_label_a_style_hides_or_replaces_holds_the_digest(self):
        hidden = '<span style="display:none">Unsubscribe</span>'
        cases = {
            "a generated label over hidden text": (".f::before{content:'View Reporting Analyst job'}",
                                                   f'<a class="f" href="{EXTRA_JOB}">{hidden}</a>', ""),
            "a generated label alone": ("a.f::after{content:'View job'}", f'<a class="f" href="{EXTRA_JOB}">Unsubscribe</a>', ""),
            "an escaped property name": (".f::before{cont\\65nt:'View job'}", f'<a class="f" href="{EXTRA_JOB}">Unsubscribe</a>', ""),
            "text hidden by its class": (".h{display:none}", f'<a href="{EXTRA_JOB}"><span class="h">Unsubscribe</span></a>', ""),
            "text hidden by its id": ("#u{display:none}", f'<a href="{EXTRA_JOB}"><span id="u">Unsubscribe</span></a>', ""),
            "a comment before the property": (".f::before{/* x */content:'View job'}",
                                              f'<a class="f" href="{EXTRA_JOB}">Unsubscribe</a>', ""),
            "text hidden by a tag rule": ("td a span{font-size:0}", f'<a href="{EXTRA_JOB}"><span>Unsubscribe</span></a>', ""),
            "a link hidden by an attribute rule": ("[data-x]{visibility:hidden}",
                                                   f'<a data-x="1" href="{EXTRA_JOB}">Unsubscribe</a>', ""),
            "the hidden attribute": ("", f'<a href="{EXTRA_JOB}"><span hidden>Unsubscribe</span></a>', ""),
            "its own zero size": ("", f'<a href="{EXTRA_JOB}" style="font-size: 0px">Unsubscribe</a>', ""),
            "a picture behind it": ("", f'<a href="{EXTRA_JOB}" style="background-image:url(https://example.invalid/j.png)">'
                                        "Unsubscribe</a>", ""),
            "a font from elsewhere": ("@font-face{font-family:Synth;src:url(https://example.invalid/s.woff)}", None, ""),
            "a style sheet imported from elsewhere": ("@import url(https://example.invalid/s.css);", None, ""),
            "generated text beside a link": ("td::before{content:'View Reporting Analyst job'}",
                                             f'<a href="{EXTRA_JOB}">Unsubscribe</a>', ""),
            "a style sheet from elsewhere": ("", None, '<link rel="stylesheet" href="https://example.invalid/s.css">'),
            "generated content on another element": ("", '<span style="content:url(https://example.invalid/j.png)">'
                                                         f'</span><a href="{AGENCY_TRACKER}?token=synthetic-footer">'
                                                         "Unsubscribe</a>", ""),
        }
        for name, (style, footer, head) in cases.items():
            with self.subTest(name):
                data = agency_mail(digest_html(style, footer, head))
                self.assertEqual(self.read(data).digest_status, "partial")   # not the recorded structure: held
                with recorded(data):   # even recorded as the template's own: held for what it does
                    alert = self.read(data)
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "markup"))

    def test_a_structure_the_recorded_template_lacks_holds_the_digest(self):
        base = agency_mail(digest_html())
        footers = {   # the red team before Pass 4: none of these uses a style the banned list knows
            "a title in the link": f'<a href="{EXTRA_JOB}"><title>Unsubscribe</title>View job</a>',
            "a closed details": f'<details><summary><a href="{EXTRA_JOB}">View job</a></summary>Unsubscribe</details>',
            "a popover": f'<a href="{EXTRA_JOB}"><span popover>Unsubscribe</span></a>',
            "a scaled-down label": f'<a href="{EXTRA_JOB}"><span style="scale:0">Unsubscribe</span></a>',
            "a quote drawn by the browser": f'<a href="{EXTRA_JOB}"><q>Unsubscribe</q></a>',
        }
        for name, footer in footers.items():
            with self.subTest(name), recorded(base):
                alert = self.read(agency_mail(digest_html(footer=footer)))
                self.assertEqual(alert.digest_status, "partial")
        styled = agency_mail(digest_html(footer=f'<a href="{AGENCY_TRACKER}?token=synthetic-footer" style="color:#333">'
                                             "Unsubscribe</a>"))
        with recorded(styled):   # the same elements, one inline style changed: held
            changed = digest_html(footer=f'<a href="{AGENCY_TRACKER}?token=synthetic-footer" style="color:#333;scale:0">'
                                         "Unsubscribe</a>")
            self.assertEqual(self.read(agency_mail(changed)).digest_reason, "layout")
        styles = ("q{quotes:'View job' ''}", ".k{font-size:0;&{}}", "@namespace h url(x); h|span{font-size:0}",
                  "[data-k=\"q z\"]{font-size:0}", "u{scale:0}")
        for style in styles:
            with self.subTest(style=style), recorded(base):
                self.assertEqual(self.read(agency_mail(digest_html(style))).digest_reason, "layout")

    def test_a_link_without_text_added_to_a_recorded_template_holds_the_digest(self):
        icon = '<a href="{href}"><img src="https://example.invalid/icon.png" alt=""></a>'
        footer = icon.format(href=f"{AGENCY_TRACKER}?token=synthetic-footer")
        base = agency_mail(digest_html(footer=footer))
        more = agency_mail(digest_html(footer=footer + icon.format(href=EXTRA_JOB)))   # a job behind a second picture
        with recorded(base):
            self.assertEqual(self.read(base).digest_status, "complete")
            self.assertEqual(self.read(more).digest_reason, "layout")   # the same elements: only the links tell them apart

    def test_anything_beside_the_one_html_part_holds_the_digest(self):
        footer = f"{AGENCY_TRACKER}?token=synthetic-footer"
        same = f"Claims Analyst\nRemote\n{AG_1}\n\nUnsubscribe: <{footer}>\n"
        cases = {"a plain-text copy with the same links": agency_mail(digest_html(), plain=same),
                 "a plain-text copy with another job": agency_mail(digest_html(), plain=same + f"Apply:{EXTRA_JOB}\n")}
        amp = EmailMessage()
        for key in ("From", "To", "Subject", "List-Unsubscribe"):
            amp[key] = email.message_from_bytes(agency_mail(digest_html()))[key]
        amp.set_content(digest_html(), subtype="html")
        amp.add_alternative(digest_html(footer=f'<a href="{EXTRA_JOB}">View job</a>'), subtype="x-amp-html")
        cases["an AMP copy"] = bytes(amp)
        attached = email.message_from_bytes(agency_mail(digest_html()), policy=email.policy.default)
        attached.make_mixed()
        attached.add_attachment(b"synthetic", maintype="application", subtype="octet-stream", filename="x.bin")
        cases["an attachment"] = bytes(attached)
        for name, data in cases.items():
            with self.subTest(name), recorded(agency_mail(digest_html())):
                alert = self.read(data)
                reason = "attachment" if name in ("an AMP copy", "an attachment") else "markup"   # any mail (D153 R8)
                self.assertEqual((alert.digest_status, alert.digest_reason, alert.links), ("partial", reason, []))
        with recorded(agency_mail(digest_html())):
            self.assertEqual(self.read(agency_mail(digest_html())).digest_status, "complete")

    def test_application_or_interview_words_beside_the_cards_hold_the_digest_unread(self):
        card = agency_card("Claims Analyst", AG_1)
        for words in ("<p>Thank you for your application. Please confirm your interview availability.</p>",
                      "<p>Thank you for your\napplication. Please confirm your&nbsp;interview availability.</p>",
                      "<p>Please confirm your <b>interview</b>  availability.</p>",
                      "<p>We'd like to invite you to interview with the client.</p>",
                      "<p>You were selected for a phone screen.</p>",
                      "<p>We submitted your profile to the client.</p>",
                      "<p>Please complete a short skills test.</p>"):   # the red team before Pass 4: wording and spacing
            with self.subTest(words=words):
                alert = self.read(agency_mail(digest_html(body=card + words), subject="Next steps"))
                self.assertEqual((alert.digest_status, alert.digest_reason, alert.links), ("partial", "correspondence", []))
        titled = agency_card("Interview Coordinator", AG_1, description="Application support for the claims team.")
        data = agency_mail(digest_html(body=titled))
        with recorded(data):   # such words in a card's own title or details: a job
            alert = self.read(data)
        self.assertEqual((alert.digest_status, len(alert.links)), ("complete", 1))


class CardCorrespondenceTests(unittest.TestCase):
    """Codex Pass 4, CR-AD-V2-11: application or interview words inside a card's own description hold the digest too."""

    def read(self, data):
        from careeros.discovery.alerts import parse_eml_bytes
        return parse_eml_bytes(data)

    def test_a_card_whose_description_is_correspondence_holds_the_digest(self):
        words = "We received your application. Please confirm your interview Tuesday at 2 p.m."
        for subject in ("Next steps", "Your jobs"):
            with self.subTest(subject=subject):
                alert = self.read(agency_email(agency_card("Claims Analyst", AG_1, description=words), subject=subject))
                self.assertEqual((alert.links, alert.digest_status, alert.digest_reason), ([], "partial", "correspondence"))
        for description in ("Process claims and interview members about their coverage.",
                            "Applications support analyst for pharmacy systems."):   # a job's own words are no correspondence
            with self.subTest(description=description):
                alert = self.read(agency_email(agency_card("Claims Analyst", AG_1, description=description)))
                self.assertEqual((len(alert.links), alert.digest_status), (1, "complete"))


class CardTitleCorrespondenceTests(unittest.TestCase):
    """Codex Pass 5, CR-AD-V2-11 (D153 R10): correspondence written as a card's title was read as a job."""

    def test_a_card_whose_title_is_correspondence_holds_the_digest(self):
        from careeros.discovery.alerts import parse_eml_bytes
        title = "Claims Analyst: please confirm your interview Tuesday at 2 p.m."
        for subject in ("Next steps", "Your jobs"):
            with self.subTest(subject=subject):
                alert = parse_eml_bytes(agency_email(agency_card(title, AG_1), subject=subject))
                self.assertEqual((alert.links, alert.digest_status, alert.digest_reason), ([], "partial", "correspondence"))
        for title in ("Interview Coordinator", "Applications Support Analyst", "Claims Analyst"):   # a job's own title
            with self.subTest(title=title):
                alert = parse_eml_bytes(agency_email(agency_card(title, AG_1)))
                self.assertEqual((len(alert.links), alert.digest_status), (1, "complete"))


class NeighbourStyleAndPictureTests(unittest.TestCase):
    """Codex Pass 5, CR-AD-V2-01 (D153 R7): under a layout recorded from the very same digest, a picture with another
    source (srcset) showed a job's label, and a rule selecting an element by its neighbour (+, ~) hid a card when
    classes moved between neighbours. Both hold the digest now; the leading "u +" email templates use to detect Gmail
    does not."""

    def read(self, data):
        from careeros.discovery.alerts import parse_eml_bytes
        with recorded(data):   # the digest's own layout recorded: only what is under test decides
            return parse_eml_bytes(data)

    def test_a_picture_with_another_source_holds_the_digest(self):
        footer = f'<a href="{AGENCY_TRACKER}?token=synthetic-footer"><img src="https://example.invalid/logo.png" alt="Logo"{{}}></a>'
        for extra in (' srcset="data:image/svg+xml,View%20Reporting%20Analyst%20job 2x"', ' srcset="logo-2x.png 2x"'):
            with self.subTest(extra=extra):
                alert = self.read(agency_mail(digest_html(footer=footer.format(extra))))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "markup"))
        picture = (f'<a href="{AGENCY_TRACKER}?token=synthetic-footer"><picture><source srcset="logo-2x.png">'
                   '<img src="https://example.invalid/logo.png" alt="Logo"></picture></a>')
        self.assertEqual(self.read(agency_mail(digest_html(footer=picture))).digest_reason, "markup")
        plain = self.read(agency_mail(digest_html(footer=footer.format(""))))   # one source only: read as before
        self.assertEqual((len(plain.links), plain.digest_status), (1, "complete"))

    def test_a_rule_that_selects_by_neighbours_holds_the_digest(self):
        for style in (".mute-next + .card-shell { display: none; }", ".a ~ .b { display: none }",
                      "td:has(.x) { color: red }", "u + .body .a + .b { color: red }", "p:not(.a + .b) { color: red }"):
            with self.subTest(style=style):
                alert = self.read(agency_mail(digest_html(style=style)))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "markup"))
        for style in ("u + .body .a { color: red }", "u ~ div .a { color: red }", 'a[href~="x"] { color: red }',
                      "td:nth-child(2n+1) { color: red }"):   # Gmail's detection, attributes and an+b only
            with self.subTest(style=style):
                alert = self.read(agency_mail(digest_html(style=style)))
                self.assertEqual((len(alert.links), alert.digest_status), (1, "complete"))


class AttachmentTests(unittest.TestCase):
    """Codex Pass 5, CR-AD-V2-06 (D153 R8): a calendar file folding the tracking host across two lines, and a PDF writing
    it in hex, got an ordinary receipt. Any part besides the email's text/plain and text/html body now holds it."""

    @staticmethod
    def html_only(add):
        from email.message import EmailMessage
        message = EmailMessage()
        message["From"] = "Synthetic Jobs <" + "@".join(("alerts", "example.org")) + ">"
        message["Subject"] = "New jobs for you"
        message.set_content('<html><body><a href="https://www.linkedin.com/jobs/view/4012345678">Claims Analyst</a>'
                            "<br>Synthetic Employer Inc · Remote</body></html>", subtype="html")
        add(message)
        return bytes(message)

    @staticmethod
    def with_part(add):
        from email.message import EmailMessage
        message = EmailMessage()
        message["From"] = "Synthetic Jobs <" + "@".join(("alerts", "example.org")) + ">"
        message["Subject"] = "New jobs for you"
        message.set_content("plain part")
        message.add_alternative('<html><body><a href="https://www.linkedin.com/jobs/view/4012345678">Claims Analyst</a>'
                                "<br>Synthetic Employer Inc · Remote</body></html>", subtype="html")
        add(message)
        return bytes(message)

    def test_any_part_besides_the_body_holds_the_email(self):
        from email.message import EmailMessage
        from careeros.discovery.alerts import parse_eml_bytes
        calendar = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nURL:https://click.email.harbor\r\n staffing.com/test?token=synthetic-cal"
                    "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode("ascii")
        pdf = (b"%PDF-1.4\n1 0 obj << /Type /Annot /A << /S /URI /URI <"
               + "https://click.email.harborstaffing.com/test?token=synthetic-pdf".encode("ascii").hex().encode("ascii")
               + b"> >> >> endobj\n%%EOF\n")
        inner = EmailMessage()
        inner["Subject"] = "forwarded"
        inner.set_content("an attached email")
        for name, add in (
                ("calendar", lambda m: m.add_attachment(calendar, maintype="text", subtype="calendar", filename="invite.ics")),
                ("pdf", lambda m: m.add_attachment(pdf, maintype="application", subtype="pdf", filename="jobs.pdf")),
                ("picture", lambda m: m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="logo.png")),
                ("email", lambda m: m.add_attachment(inner)),
                ("html file", lambda m: m.add_attachment("<p>jobs</p>", subtype="html", filename="jobs.html")),
                ("second html, inline", lambda m: m.add_attachment("<p>jobs</p>", subtype="html", disposition="inline")),
                ("second text, inline", lambda m: m.add_attachment("more jobs", disposition="inline"))):
            with self.subTest(name):
                alert = parse_eml_bytes(self.with_part(add))
                self.assertEqual((alert.links, alert.digest_status, alert.digest_reason), ([], "partial", "attachment"))
        plain = EmailMessage()
        plain["Subject"] = "forwarded"
        plain.set_content("an attached email, text only")
        for name, add in (("attached email", lambda m: m.add_attachment(plain)),
                          ("text file", lambda m: m.add_attachment("jobs list", filename="jobs.txt"))):
            with self.subTest(name, body="html only"):   # one text part and one HTML part in all: still held
                alert = parse_eml_bytes(self.html_only(add))
                self.assertEqual((alert.links, alert.digest_status, alert.digest_reason), ([], "partial", "attachment"))
        ordinary = parse_eml_bytes(self.with_part(lambda m: None))   # its body alone: read as before
        self.assertEqual((len(ordinary.links), ordinary.digest_status), (1, None))


class UnreadableHostTests(unittest.TestCase):
    """Codex Pass 5, CR-AD-V2-03 (D153 R5): a host a browser accepts and Python's IDNA refuses (a middle dot) was read as
    ordinary and exported. A link whose host, or a host inside it, CareerOS cannot read as a browser does is a tracking
    link now: local, never a lead of its own, never matched."""

    def test_a_link_carrying_a_host_python_cannot_read_stays_local(self):
        from urllib.parse import quote
        from careeros.discovery.alerts import (_LocalUrls, canonical_job_url, is_agency_lead, is_agency_tracking_url,
                                               lead_tracker_hosts, unreadable_host)
        middle = "https://a\u00b7b.example.com/job/SYNTH-PERSONAL"
        for link in (middle, "https://scanner.example.net/jobs/" + quote(middle, safe=""),
                     "https://scanner.example.net/r?u=" + quote(quote(middle, safe=""), safe=""),
                     "https://xn--ab-0ea.example.com/job/1", "https://fu\u00df.example.com/job/1",
                     "https://scanner.example.net/r?u=https%3A%2F%2Fxn--fu-hia.example.com%2Fj",
                     "https://scanner.example.net/go/a\u00b7b.example.com/job", "https:\\\\a\u00b7b.example.com/job"):
            with self.subTest(link=link[:60]):
                self.assertTrue(unreadable_host(link))
                self.assertTrue(is_agency_tracking_url(link))
                self.assertIsNone(canonical_job_url(link))
                self.assertTrue(is_agency_lead({"lead_id": "lead:1", "url": link}))
                local = _LocalUrls()   # under a stop check's own reading too
                local.hosts = frozenset(("click.email.harborstaffing.com",))
                self.assertTrue(is_agency_lead({"lead_id": "lead:1", "url": link}, local))
                self.assertEqual(lead_tracker_hosts(link, frozenset(("click.email.harborstaffing.com",))), set())
        for link in ("https://boards.greenhouse.io/synth/jobs/555", "https://example.com:8443/job/1",
                     "https://safelinks.example.net/?url=https%3A%2F%2Fwww.example.org%2Fjob",
                     "https://www.example.org/job/se\u00f1or-claims-analyst", "https://\uff45xample.com/job/1"):
            with self.subTest(link=link[:60]):   # ASCII hosts (a full-width letter a browser maps to one too)
                self.assertFalse(unreadable_host(link))


class CorrespondenceWordsTests(unittest.TestCase):
    def test_interview_scheduling_words_are_correspondence_whoever_sends_them(self):
        from careeros.discovery.alerts import explicit_correspondence
        for subject in ("Interview availability", "Please confirm the interview", "Confirm your availability",
                        "Confirm your\u00a0interview", "Thank you for\napplying"):   # Codex Pass 3, CR-AD-V2-11
            with self.subTest(subject=subject):
                self.assertTrue(explicit_correspondence(subject))
        for subject in ("Your jobs", "Interview Coordinator and 4 more jobs", "Next steps", "Application Developer jobs",
                        "Your Interview Coordinator jobs"):   # a job's title is no correspondence (the red team)
            with self.subTest(subject=subject):
                self.assertFalse(explicit_correspondence(subject))


class EvidenceTests(unittest.TestCase):
    """Codex Pass 3, CR-AD-V2-03 and -06: a tracking host a browser reaches is found however the link spells it."""

    def test_an_escaped_tab_inside_a_wrapped_link_still_names_the_tracking_host(self):
        from careeros.discovery.alerts import agency_for_url, is_agency_tracking_url
        for wrapped in ("https://scanner.example.net/jobs/https%3A%2F%2Fclick.email.harbor%09staffing.com%2Fjob%2FSYNTH",
                        "https://scanner.example.net/r?u=https%253A%252F%252Fclick.email.harbor%250Astaffing.com%252Fjob"):
            with self.subTest(wrapped=wrapped):
                self.assertTrue(is_agency_tracking_url(wrapped))
                self.assertIsNone(canonical_job_url(wrapped))
                self.assertIsNotNone(agency_for_url(wrapped))

    def test_a_host_a_browser_maps_with_newer_unicode_tables_is_the_tracking_host(self):
        from careeros.discovery.alerts import is_agency_tracking_url
        outlined_s = chr(0x1CCE8)   # OUTLINED LATIN SMALL LETTER S (Unicode 16): a browser's IDNA reads it as "s"
        for url in (f"https://click.email.harbor{outlined_s}taffing.com/test?token=synthetic-1",
                    f"https://scanner.example.net/jobs/https%3A%2F%2Fclick.email.harbor{outlined_s}taffing.com%2Fjob"):
            with self.subTest(url=ascii(url)):
                self.assertTrue(is_agency_tracking_url(url))   # the red team before Pass 4
        from careeros.discovery.alerts import parse_eml_bytes   # and in another sender's mail, evidence it is the agency's
        alert = parse_eml_bytes(alert_email(f'<p><a href="https://click.email.harbor{outlined_s}taffing.com/test?token='
                                            f'synthetic-1">Claims Analyst</a></p>'))   # (D153 R5 alone keeps the link local)
        self.assertEqual((alert.digest_status, alert.digest_reason, alert.links), ("partial", "envelope", []))

    def test_a_tracking_link_escaped_four_times_is_still_the_agencys(self):
        from urllib.parse import quote
        from careeros.discovery.alerts import is_agency_tracking_url
        link = "https://click.email.harborstaffing.com/test?token=synthetic-1"
        for _ in range(4):
            link = "https://scanner.example.net/jobs/" + quote(link, safe="")
        self.assertTrue(is_agency_tracking_url(link))
        self.assertIsNone(canonical_job_url(link))
        dots = "https://click%2Eemail%2Eharborstaffing%2Ecom/x"
        self.assertTrue(is_agency_tracking_url("https://scanner.example.net/r?u=" + quote(quote(quote(dots, safe=""), safe=""), safe="")))

    def test_a_tab_or_line_break_inside_a_links_host_is_still_the_agencys_evidence(self):
        from careeros.discovery.alerts import parse_eml_bytes
        for host in ("click.email.harbor&#9;staffing.com", "click.email.harbor\nstaffing.com", "click.email.harbor&Tab;staffing.com"):
            with self.subTest(host=repr(host)):
                alert = parse_eml_bytes(alert_email(f'<p><a href="https://{host}/test?token=synthetic-1">Claims Analyst</a></p>'))
                self.assertEqual((alert.digest_status, alert.digest_reason, alert.links), ("partial", "envelope", []))

    def test_a_host_at_the_start_of_a_line_is_never_joined_to_the_word_before_it(self):
        from careeros.discovery.alerts import _text_mentions_tracker
        hosts = frozenset({"click.email.harborstaffing.com"})
        self.assertTrue(_text_mentions_tracker("Apply here\nclick.email.harborstaffing.com/test", hosts))
        self.assertFalse(_text_mentions_tracker("click.email.harborstaffing.community/test", hosts))

    def test_unreadable_mail_naming_the_agency_in_any_spelling_is_held(self):
        from careeros.discovery.alerts import raw_mentions_agency
        for raw in (b"href=3D\"https://click.email.harbor=\r\nstaffing.com/x\"", b"click.email.harbor&#9;staffing.com",
                    b"click.email.harbor%09staffing.com"):
            with self.subTest(raw=raw):
                self.assertTrue(raw_mentions_agency(b"Subject: x\r\n\r\n" + raw))
        self.assertFalse(raw_mentions_agency(b"Subject: x\r\n\r\nhttps://jobs.example.invalid/1"))


class LinkTests(unittest.TestCase):
    def test_agency_tracker_is_not_a_generic_job_link(self):
        from urllib.parse import quote

        redirect = AG_1 + "&url=" + quote("https://boards.greenhouse.io/synthetic/jobs/123", safe="")
        self.assertIsNone(canonical_job_url(redirect))
        from careeros.discovery.alerts import greenhouse_candidate
        self.assertIsNone(greenhouse_candidate(AG_1 + "&gh_jid=123"))
        self.assertIsNone(canonical_job_url("https://example.invalid/wrap?url=" + quote(redirect, safe="")))

    def test_a_host_with_a_character_idna_refuses_is_never_a_lead(self):
        self.assertIsNone(canonical_job_url("https://jobs.example" + chr(0x378) + ".com/job/1"))   # M1-01: kept as it is
        for escaped in ("%2F", "%3F", "%23", "%20"):   # read as a host "jobs" by D153 R5's check; refused by this one
            with self.subTest(escaped=escaped):
                self.assertIsNone(canonical_job_url("https://jobs" + escaped + chr(0xE4) + "/job/1"))

    def test_tracking_is_removed_and_job_links_recognised(self):
        self.assertEqual(canonical_job_url(LI), ("https://www.linkedin.com/jobs/view/4012345678", "linkedin", True))
        self.assertEqual(canonical_job_url("https://www.linkedin.com/jobs/search/?currentJobId=4011111111&keywords=x")[0],
                         "https://www.linkedin.com/jobs/view/4011111111")
        self.assertEqual(canonical_job_url("https://www.indeed.com/rc/clk/dl?jk=0a1b2c3d4e5f6a7b&from=ja"),
                         ("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "indeed", True))
        self.assertEqual(canonical_job_url("https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=1009"),
                         ("https://www.glassdoor.com/job-listing/j?jl=1009", "glassdoor", True))
        self.assertEqual(canonical_job_url("https://t.example/r?url=https%3A%2F%2Fjobs.lever.co%2Facme%2Fabc-1%3Flever-source%3Dx"),
                         ("https://jobs.lever.co/acme/abc-1", "lever", True))
        self.assertEqual(canonical_job_url("https://apply.workable.com/acme/j/94F9E30E22/"),
                         ("https://apply.workable.com/acme/j/94F9E30E22", "workable", True))
        self.assertFalse(canonical_job_url("https://www.linkedin.com/comm/psettings/email-unsubscribe?x=1")[2])
        self.assertIsNone(canonical_job_url("javascript:alert(1)"))
        self.assertIsNone(canonical_job_url("mailto:a@example.com"))


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: bytes, name="alert.eml") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_one_link_per_job_with_its_title_and_the_text_beside_it(self):
        alert = parse_eml(self.write(alert_email()))
        self.assertEqual(alert.sender, "LinkedIn Job Alerts <jobalerts@example.com>")
        self.assertEqual(alert.received_at, "2026-10-10T13:15:00.000Z")
        links: dict = {}
        for link in alert.links:   # every occurrence is kept (part 3 of D192): the first one with a title, by link
            if link.text:
                links.setdefault(link.url, link)
        self.assertEqual(sum(l.url == "https://www.linkedin.com/jobs/view/4012345678" for l in alert.links), 3)   # logo, title, "View job"
        first = links["https://www.linkedin.com/jobs/view/4012345678"]
        self.assertEqual((first.text, first.site, first.job_like), ("Claims Business Analyst", "linkedin", True))
        self.assertIn("Synthetic Health · United States (Remote)", first.context)
        self.assertEqual(links["https://job-boards.greenhouse.io/synth/jobs/555"].text, "Pharmacy Benefits Systems Analyst")
        self.assertNotIn("javascript:alert(1)", links)
        self.assertNotIn("color:red", " ".join(l.text + l.context for l in alert.links))

    def test_agency_digest_pairs_job_headings_and_keeps_both_same_title_cards(self):
        alert = parse_eml(self.write(agency_email()))
        self.assertEqual(alert.digest_status, "complete")
        self.assertEqual([(link.url, link.site, link.text, link.context, link.job_like) for link in alert.links],
                         [(url, "harborstaffing", "Claims Analyst", "Harbor Staffing · Remote", True) for url in (AG_1, AG_2)])

    def test_agency_digest_rejects_missing_titles_other_hosts_and_unrelated_senders(self):
        orphan = f'<tr><td><a href="{AG_2}">Apply Now &rarr;</a></td></tr>'
        body = (agency_card("Claims Analyst", AG_1) + orphan
                + agency_card("Reporting Analyst", "https://click.email.harborstaffing.com.example.invalid/job"))
        alert = parse_eml(self.write(agency_email(body)))
        self.assertEqual([link.url for link in alert.links], [AG_1])
        self.assertEqual(alert.digest_status, "partial")
        lookalike = "@".join(("synthetic", "email.harborstaffing.com.example.invalid"))
        for kw in ({"sender": "someone@example.invalid"}, {"sender": lookalike}, {"bulk": False}):
            with self.subTest(**kw):
                outside = parse_eml(self.write(agency_email(**kw)))   # not the digest's envelope, but its tracking links:
                self.assertEqual((outside.links, outside.digest_status, outside.digest_reason), ([], "partial", "envelope"))

    def test_agency_digest_never_pairs_across_a_missing_card_title(self):
        first = agency_component(18, "<b>Claims Analyst</b>") + agency_component(14, "Remote")
        second = agency_card("Reporting Analyst", AG_2).replace("<b>Reporting Analyst</b>", "")
        alert = parse_eml(self.write(agency_email(first + second)))
        self.assertEqual(alert.links, [])
        self.assertEqual(alert.digest_status, "partial")

    def test_agency_title_cannot_cross_into_another_table_scope(self):
        title = agency_component(18, "<b>Claims Analyst</b>")
        details = agency_component(14, "Remote") + agency_component(14, "Sample opening")
        button = agency_component(14, f'<a href="{AG_1}">Apply Now &rarr;</a>')
        for heading, stray_close in ((title, ""), (title.replace("</b>", ""), "</b>")):
            with self.subTest(malformed=bool(stray_close)):
                body = f"<table><tr><td>{heading}</td></tr></table><table><tr><td>{stray_close}{details}{button}</td></tr></table>"
                alert = parse_eml(self.write(agency_email(body)))
                self.assertEqual(alert.links, [])
                self.assertEqual(alert.digest_status, "partial")

    def test_agency_digest_accepts_a_query_only_tracking_link_but_not_a_bare_host(self):
        query_only = "https://click.email.harborstaffing.com/?qs=synthetic-3"
        alert = parse_eml(self.write(agency_email(agency_card("Claims Analyst", query_only))))
        self.assertEqual(([link.url for link in alert.links], alert.digest_status), ([query_only], "complete"))
        for refused in ("https://click.email.harborstaffing.com/", "http://click.email.harborstaffing.com/job",
                        "https://click.email.harborstaffing.com/job#top"):
            with self.subTest(link=refused):
                alert = parse_eml(self.write(agency_email(agency_card("Claims Analyst", refused))))
                self.assertEqual((alert.links, alert.digest_status), ([], "partial"))

    def test_agency_card_keeps_its_location_pay_and_job_number(self):
        from careeros.discovery.note import context_note

        def context(location="Remote", description="Sample duties."):
            return parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1, location, description)))).links[0].context

        paid = context(description="Pay: $25.00 - $30.00 per hour. Sample duties.")
        self.assertEqual(paid, "Harbor Staffing · Remote · pay=$25.00 - $30.00 per hour")
        self.assertEqual(context_note(paid)["fields"], {"pay": "$25.00 - $30.00 per hour"})
        numbered = context(description="Job ID: 04530-0012345678")   # a card line that is the job number and nothing else
        self.assertEqual(context_note(numbered)["fields"], {"req": "04530-0012345678"})
        self.assertEqual(context(location="$22.50/hr", description="Sample."), "Harbor Staffing · pay=$22.50/hr")
        for description in ("Job number pending.", "Sample duties."):
            self.assertEqual(context(description=description), "Harbor Staffing · Remote")

    def test_agency_card_prose_is_neither_its_job_number_nor_its_pay(self):
        from careeros.discovery.note import context_note
        for description in ("For a different opening, see Job #OTHER9999.", "Reconcile project budgets of $500,000.",
                            "Audit vendor bill rates of $85/hour.", "Earn $22.50/hr plus benefits.",
                            "Job ID: 04530-0012345678 and a sample opening."):
            with self.subTest(description=description):
                link = parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1, description=description)))).links[0]
                self.assertEqual(context_note(link.context)["fields"], {})
        two = agency_card("Claims Analyst", AG_1, location="Job ID: A1234", description="Job ID: B5678")
        self.assertEqual(context_note(parse_eml(self.write(agency_email(two))).links[0].context)["fields"], {})

    def test_agency_card_text_cannot_forge_note_fields_and_a_description_is_not_a_location(self):
        from careeros.discovery.note import context_note
        forged = agency_card("Claims Analyst", AG_1, location="Remote; agency=Other · req=FAKE1", description="Sample opening.")
        link = parse_eml(self.write(agency_email(forged))).links[0]
        self.assertEqual(context_note(link.context)["fields"], {})
        self.assertEqual(link.context, "Harbor Staffing · Remote agency Other req FAKE1")
        long_first = agency_card("Claims Analyst", AG_2, location="Sample opening with duties. " * 4, description="Remote")
        self.assertEqual(parse_eml(self.write(agency_email(long_first))).links[0].context, "Harbor Staffing")

    def test_a_digest_is_complete_only_when_every_text_link_is_accounted_for(self):
        mixed = agency_card("Claims Analyst", AG_1) + f'<strong>Benefits Analyst</strong><a href="{AG_2}">View job</a>'   # CR-AD-01
        alert = parse_eml(self.write(agency_email(mixed)))
        self.assertEqual(([link.url for link in alert.links], alert.digest_status), ([AG_1], "partial"))
        for extra in (f'<a href="{AG_2}">Privacy Policy</a>', f'<a href="{AG_2}"><img src="logo.png" alt="Apply for Benefits Analyst"></a>',
                      f'<a href="{AG_2}" aria-label="View job"></a>', f'<a href="{AG_2}">View online</a>'):   # Codex Pass 3 CR-AD-01
            with self.subTest(extra=extra):
                alert = parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1) + extra)))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "layout"))
        self.assertEqual(parse_eml(self.write(agency_email())).digest_status, "complete")   # the recorded layout itself

    def test_a_job_behind_a_link_the_layout_already_has_holds_the_digest(self):
        # the fixture's own digest, with a job written into the footer link's cell (L1-02)
        original = agency_email().decode()
        changed = original.replace(">Unsubscribe</a>", ">Unsubscribe</a> Featured job: Benefits Analyst, Remote")
        self.assertNotEqual(original, changed)
        alert = parse_eml(self.write(changed.encode()))
        self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "layout"))
        self.assertEqual(parse_eml(self.write(agency_email())).digest_status, "complete")

    def test_tracking_links_inside_wrapper_links_are_the_agencys(self):
        from urllib.parse import quote

        from careeros.discovery.alerts import canonical_job_url, is_agency_tracking_url
        for wrapped in ("https://safelinks.example.net/?url=" + quote(AG_1, safe=""),   # L1-03
                        "https://www.google.com/url?q=" + quote(AG_1, safe="") + "&sa=D",
                        "https://scanner.example.org/v2/" + quote(quote(AG_1, safe=""), safe="") + "/x"):
            with self.subTest(wrapped=wrapped[:40]):
                self.assertTrue(is_agency_tracking_url(wrapped))
                self.assertIsNone(canonical_job_url(wrapped))
        self.assertFalse(is_agency_tracking_url("https://safelinks.example.net/?url=https%3A%2F%2Fwww.example.org%2Fjob"))
        forwarded = agency_email(agency_card("Claims Analyst", "https://safelinks.example.net/?url=" + quote(AG_1, safe="")), bulk=False)
        held = parse_eml(self.write(forwarded))
        self.assertEqual((held.links, held.digest_status, held.digest_reason), ([], "partial", "envelope"))
        only_wrapped = alert_email(f'<a href="https://safelinks.example.net/?url={quote(AG_1, safe="")}">Claims Analyst</a>'
                                   "<br>Synthetic Employer Inc · Remote")   # no plain tracking link anywhere: held all the same
        held = parse_eml(self.write(only_wrapped))
        self.assertEqual((held.links, held.digest_status, held.digest_reason), ([], "partial", "envelope"))

    def test_bulk_mail_named_like_the_agency_from_an_unknown_address_is_held(self):
        from email.message import EmailMessage
        message = EmailMessage()   # the agency after a move to another mail service (L4F-02)
        message["From"] = "Harbor Staffing Jobs <" + "@".join(("alerts", "news.example.net")) + ">"
        message["Subject"] = "Your jobs"
        message["List-Unsubscribe"] = "<https://example.invalid/unsubscribe>"
        message.set_content('<html><body><a href="https://links.example.net/j/1">Apply Now</a></body></html>', subtype="html")
        alert = parse_eml(self.write(bytes(message)))
        self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "envelope"))
        del message["List-Unsubscribe"]   # the same name on personal mail: not held (no bulk header)
        self.assertIsNone(parse_eml(self.write(bytes(message))).digest_status)

    def test_markup_a_browser_reads_differently_never_hides_a_job(self):
        from unittest import mock
        from careeros.discovery import alerts
        hidden = agency_card("Benefits Analyst", AG_2)
        shown = {   # a browser shows the second job; Python's parser alone would not (CR-AD-V2-01, Pass 2)
            "a comment closed by --!>": f"<!-- x --!>{hidden}<!-- y -->",
            "an empty comment <!-->": f"<!-->{hidden}",
            "an empty comment <!--->": f"<!--->{hidden}",
            "CDATA, a bogus comment to the first >": f"<![CDATA[ x >{hidden}",
            "an end tag with attributes": f"<style>p{{color:red}}</style foo>{hidden}",
        }
        for name, extra in shown.items():
            with self.subTest(name):
                data = agency_email(agency_card("Claims Analyst", AG_1) + extra)
                with recorded(data):   # its own style or elements recorded as the template's (the structure fingerprint)
                    alert = parse_eml(self.write(data))
                self.assertEqual((alert.digest_status, [link.text for link in alert.links]),
                                 ("complete", ["Claims Analyst", "Benefits Analyst"]))
        held = {
            "a link opened inside an open one, behind a stray <head>":
                (f'<a href="{AG_2}">View job<head><a href="https://ignored.invalid/">ignored</a></head>', "links"),
            "a link a browser closes at the end of its cell":
                (f'<table><tr><td><a href="{AG_2}">Apply Now</td><td>more</a></td></tr></table>', "links"),
            "an image-map link": (f'<map name="m"><area href="{AG_2}"></map>', "markup"),
            "a form's button": (f'<form action="{AG_2}"><button>Apply</button></form>', "markup"),
        }
        for name, (extra, reason) in held.items():
            with self.subTest(name):
                alert = parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1) + extra)))
                self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", reason))
        twice = agency_card("Claims Analyst", AG_1).replace(f'<a href="{AG_1}">', f'<a href="{AG_1}" href="https://elsewhere.example/">')
        self.assertEqual([link.url for link in parse_eml(self.write(agency_email(twice))).links], [AG_1])   # a browser's first href
        titled = agency_email(agency_card("Claims Analyst", AG_1) + f'<title><a href="{AG_2}">Apply Now</a></title>')
        with recorded(titled):
            title = parse_eml(self.write(titled))
        self.assertEqual(([link.url for link in title.links], title.digest_status), ([AG_1], "complete"))   # text: never a job
        with mock.patch.object(alerts, "_browser_markup", lambda markup: (markup, 99)):   # the two readings disagree
            self.assertEqual(parse_eml(self.write(agency_email())).digest_reason, "markup")

    def test_a_link_a_browser_opens_on_the_tracking_host_is_the_agencys_however_python_splits_it(self):
        from careeros.discovery.alerts import _norm_host, canonical_job_url, is_agency_tracking_url
        for link in ("https://click.email.harborstaffing.com\\@ordinary.example/job/synthetic-ambiguous-authority",
                     "https://click.email.harborstaffing.com\\job/synthetic", "https:click.email.harborstaffing.com/job/synthetic",
                     "https:\\\\click.email.harborstaffing.com/job/synthetic", "HTTPS://CLICK.EMAIL.HARBORSTAFFING.COM/job/synthetic",
                     " https://click.email.harbor\tstaffing.com/job/synthetic", "https://\uff43lick.email.harborstaffing.com/job/synthetic",
                     "https://click.email.harbor\u200bstaffing.com/job/synthetic",   # CR-AD-V2-03, Pass 2
                     # characters the browser mapping keeps (a joiner, a direction mark or override): dropped (Pass 5)
                     "https://click.email.harbor\u200dstaffing.com/job/synthetic", "https://click.email.harbor\u200estaffing.com/job/synthetic",
                     "https://click.email.harbor\u202estaffing.com/job/synthetic"):
            with self.subTest(link=link[:45]):
                self.assertEqual(_norm_host(link), "click.email.harborstaffing.com")
                self.assertTrue(is_agency_tracking_url(link))
                self.assertIsNone(canonical_job_url(link))
        for spelling in ("https://click.email.harborstaffing.com./job/t", "https://click.email%2Eharborstaffing.com/job/t"):
            self.assertEqual(_norm_host(spelling), "click.email.harborstaffing.com")   # the host itself, not only a mention
        self.assertFalse(is_agency_tracking_url("https://click.email.harborstaffing.com.ordinary.example/job/synthetic"))
        self.assertEqual(_norm_host("https://Ordinary.Example/job"), "ordinary.example")

    def test_the_agencys_tracking_host_in_any_part_and_any_case_holds_the_email(self):
        from email.message import EmailMessage

        def mail(plain, html=None):
            message = EmailMessage()
            message["From"] = "Newsletter <" + "@".join(("news", "example.org")) + ">"
            message["Subject"] = "Your jobs"
            message.set_content(plain)
            if html:
                message.add_alternative(html, subtype="html")
            return bytes(message)

        cases = {   # CR-AD-V2-06, Pass 2
            "the plain part beside an HTML part": mail(f"Claims Analyst\n{AG_1}\n",
                                                       "<html><body><p>Read the plain-text version of this email.</p></body></html>"),
            "an upper-case link in plain text": mail("Claims Analyst\nHTTPS://CLICK.EMAIL.HARBORSTAFFING.COM/test?token=synthetic-9\n"),
            "a host in full-width letters": mail("Claims Analyst\nhttps://\uff43lick.email.harborstaffing.com/test?token=synthetic-9\n"),
            "a host split by an invisible character":
                mail("Claims Analyst\nhttps://click.email.harbor\u200bstaffing.com/test?token=synthetic-9\n"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                alert = parse_eml(self.write(data))
                self.assertEqual((alert.links, alert.digest_status, alert.digest_reason), ([], "partial", "envelope"))
        self.assertIsNone(parse_eml(self.write(mail("Claims Analyst\nhttps://www.example.org/jobs/1\n"))).digest_status)

    def test_the_cheap_contrived_spellings_and_parts_of_the_red_team_are_held(self):
        import base64
        from email.message import EmailMessage
        from careeros.discovery.alerts import canonical_job_url, is_agency_tracking_url, raw_mentions_agency
        dropped = "https://click" + chr(0x1BCA0) + ".email.harborstaffing.com/job/synthetic"   # M1-01
        outlined = "https://click.email.harborstaffin" + chr(0x1CCDC) + ".com/job/synthetic"   # an outlined G: unknown to 3.12, G on 3.14
        for link in (dropped, outlined):
            self.assertIsNone(canonical_job_url(link))
        proofpoint = ("https://urldefense.proofpoint.com/v2/url?u=https-3A__click.email.harborstaffing.com_test-3Ftoken-3D"
                      "synthetic-2Dpp2&d=DwMF")   # M1-03, P4-03
        for link in (proofpoint, "https://click.email.harborstaffing.com%40ordinary.example/job/tok-pct"):
            self.assertTrue(is_agency_tracking_url(link))
        import email as email_lib
        import email.policy
        two = EmailMessage()   # M1-04: a second HTML part a mail client may show
        source = email_lib.message_from_bytes(agency_email(), policy=email_lib.policy.default)
        for header in ("From", "Subject", "List-Unsubscribe"):
            two[header] = source[header]
        two.set_content(source.get_body(preferencelist=("html",)).get_content(), subtype="html")
        two.add_attachment(f'<html><body>{agency_card("Benefits Analyst", AG_2)}</body></html>', subtype="html",
                           disposition="inline")
        self.assertEqual(parse_eml(self.write(bytes(two))).digest_reason, "attachment")   # any mail (D153 R8)
        forward = EmailMessage()   # M1-05: a digest attached base64-encoded, from another sender
        forward["From"] = "Me <" + "@".join(("me", "example.org")) + ">"
        forward["Subject"] = "Fwd: jobs"
        forward.set_content("See the attached email.")
        attached = email_lib.message_from_bytes(
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(agency_email()))
        forward.make_mixed()
        forward.attach(attached)
        self.assertEqual(parse_eml(self.write(bytes(forward))).digest_reason, "attachment")   # any mail (D153 R8)
        from careeros.discovery.alerts import _tracker_evidence   # and the evidence check sees it too, should R8 ever let
        self.assertTrue(_tracker_evidence(email_lib.message_from_bytes(bytes(forward), policy=email_lib.policy.default)))
        self.assertTrue(raw_mentions_agency(b"Subject: news from harbor staffing\r\n\r\nbody"))   # P4-05: the agency's name

    def test_an_html_file_attached_under_any_type_is_evidence_too(self):
        from careeros.discovery.alerts import _tracker_evidence   # checked directly too, should R8 ever let such mail by
        page = f'<html><body><a href="{AG_2}">Benefits Analyst</a></body></html>'
        for name, data in (("jobs.html", page.encode("utf-8")), ("jobs.bin", page.encode("utf-16"))):   # Codex Pass 4, CR-AD-V2-06
            with self.subTest(name=name):
                mail = EmailMessage()
                mail["From"] = "Me <" + "@".join(("me", "example.org")) + ">"
                mail["Subject"] = "Jobs"
                mail.set_content("See the attached page.")
                mail.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
                self.assertEqual(parse_eml(self.write(bytes(mail))).digest_reason, "attachment")   # held first (D153 R8)
                self.assertTrue(_tracker_evidence(email.message_from_bytes(bytes(mail), policy=email.policy.default)))
        ordinary = EmailMessage()
        ordinary["From"] = "Me <" + "@".join(("me", "example.org")) + ">"
        ordinary["Subject"] = "Jobs"
        ordinary.set_content("See the attached page.")
        ordinary.add_attachment(b"<html><body>No agency here.</body></html>", maintype="application", subtype="octet-stream",
                                filename="jobs.html")
        self.assertEqual(parse_eml(self.write(bytes(ordinary))).digest_reason, "attachment")   # any attachment (D153 R8)
        self.assertFalse(_tracker_evidence(email.message_from_bytes(bytes(ordinary), policy=email.policy.default)))

    def test_an_international_tracking_host_is_read_as_a_browser_reads_it(self):
        """Codex Pass 4, CR-AD-V2-03, -08, -09: "ß" is kept as browsers keep it (UTS 46, not transitional), so a host
        written with it is the xn-- host of the settings, directly and inside a wrapper; "ss" is another host."""
        from careeros.discovery.alerts import _mentions_tracker, _norm_host, lead_tracker_hosts
        tracker = "xn--fu-hia.example.com"
        for link in ("https://fuß.example.com/job/SYNTH-PERSONAL", "https://FUß.example.com/job/SYNTH-PERSONAL",
                     "https://fu%C3%9F.example.com/job/SYNTH-PERSONAL", f"https://{tracker}/job/SYNTH-PERSONAL"):
            with self.subTest(link=link):
                self.assertEqual(_norm_host(link), tracker)
        self.assertEqual(_norm_host("https://fuss.example.com/job/SYNTH-PERSONAL"), "fuss.example.com")
        known = frozenset((tracker,))
        for inner in ("https%3A%2F%2Ffu%C3%9F.example.com%2Fjob%2FSYNTH-PERSONAL",
                      "https%3A%2F%2Fxn--fu-hia.example.com%2Fjob%2FSYNTH-PERSONAL"):
            wrapped = f"https://scanner.example.net/jobs/{inner}"
            with self.subTest(wrapped=inner):
                self.assertTrue(_mentions_tracker(wrapped, known))
                self.assertEqual(lead_tracker_hosts(wrapped, known), {tracker})   # never the wrapper's own host
        self.assertFalse(_mentions_tracker("https://scanner.example.net/jobs/https%3A%2F%2Ffuss.example.com%2Fjob", known))

    def test_lead_classification_keeps_the_reading_of_its_stop_check(self):
        import os
        from unittest import mock
        from careeros.core import private_sites
        from careeros.core.clock import FixedClock
        from careeros.db.store import open_store
        from careeros.discovery.alerts import is_agency_lead, local_only_urls
        from tests.test_saved import POLICY as SAVED_POLICY
        store = open_store(":memory:", FixedClock("2026-10-11T12:00:00.000Z"), SAVED_POLICY.fingerprint)
        local = local_only_urls(store)   # the fixture's reading: the tracking host is known (red team S2F-01..03)
        lead = {"lead_id": "lead:synthetic", "url": AG_1}
        with mock.patch.dict(os.environ, {private_sites.ENV: ""}):   # a later reading without the agency: the bound one still decides
            self.assertTrue(is_agency_lead(lead, local))
            self.assertFalse(is_agency_lead(lead))

    def test_a_link_left_open_holds_the_digest(self):
        open_link = f'<strong>Benefits Analyst</strong><a href="{AG_2}" aria-label="Apply for Benefits Analyst">View job'   # CR-AD-V2-01
        alert = parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1) + open_link)))
        self.assertEqual((alert.digest_status, alert.digest_reason), ("partial", "links"))
        closed = parse_eml(self.write(agency_email(agency_card("Claims Analyst", AG_1) + open_link + "</a>")))
        self.assertEqual((closed.digest_status, closed.digest_reason), ("partial", "layout"))

    def test_the_tracking_host_is_recognised_in_any_spelling_a_browser_accepts(self):
        from careeros.discovery.alerts import canonical_job_url, is_agency_tracking_url
        for link in ("https://click.email.harborstaffing.com./job/t", "https://CLICK.Email.HarborStaffing.com/job/t",
                     "https://click.email%2Eharborstaffing.com/job/t", "https://click.email.harborstaffing.com\u3002/job/t",
                     "https://click\uff0eemail.harborstaffing.com/job/t"):   # CR-AD-V2-03, L1-04
            with self.subTest(link=link):
                self.assertTrue(is_agency_tracking_url(link))
                self.assertIsNone(canonical_job_url(link))   # never an ordinary job link
        self.assertFalse(is_agency_tracking_url("https://click.email.harborstaffing.com.example.invalid/job/t"))

    def test_without_a_recorded_layout_every_agency_digest_is_held(self):
        import os
        from unittest import mock

        from careeros.core import private_sites
        unrecorded = self.dir / "unrecorded.yaml"
        unrecorded.write_text("\n".join(("agency_digests:", "  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                                         "email.harborstaffing.com, tracker_host: click.email.harborstaffing.com}", "")),
                              encoding="utf-8")
        with mock.patch.dict(os.environ, {private_sites.ENV: str(unrecorded)}):
            alert = parse_eml(self.write(agency_email()))
        self.assertEqual((len(alert.links), alert.digest_status, alert.digest_reason), (2, "partial", "no layout recorded"))

    def test_the_agency_layout_command_prints_counts_and_the_fingerprint_only(self):
        import contextlib
        import io
        from argparse import Namespace

        from careeros.cli import cmd_agency_layout
        same, changed = self.write(agency_email(), "a.eml"), self.write(agency_email(agency_card("Claims Analyst", AG_1)
                                                                                   + f'<a href="{AG_2}">View online</a>'), "b.eml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(same), str(changed), str(self.write(alert_email(), "c.eml"))]))
        text = out.getvalue()
        self.assertIn("a.eml: Harbor Staffing: 2 cards read (2 titles, 2 Apply Now buttons); layout c5d892ac0ea84766 "
                      "(matches the recorded layout); would be complete", text)
        self.assertIn("DIFFERS from the recorded layout", text)
        self.assertIn("c.eml: not an agency digest", text)
        self.assertIn("different layouts", text)
        for private in ("Claims Analyst", "click.email", "synthetic-1", "Remote", "View online"):
            self.assertNotIn(private, text)
        out = io.StringIO()   # one digest is not enough; two that agree are; a job-like link outside the cards warns (L2-02f)
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(same)]))
        self.assertIn("Save a second digest", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(same), str(self.write(agency_email(), "a2.eml"))]))
        self.assertIn("record it in the agency's entry as layout: c5d892ac0ea84766", out.getvalue())
        self.assertNotIn("job-like", out.getvalue())
        featured = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">Apply for Benefits Analyst</a>')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(self.write(featured, "f.eml"))]))
        self.assertIn("1 link(s) outside the job cards have a job-like label", out.getvalue())
        listing = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">See all jobs</a>')   # a list link
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(self.write(listing, "l.eml"))]))
        self.assertNotIn("job-like", out.getvalue())
        # the elements around a link are not its label: a footer link in a table with role="presentation" (as in the
        # real template) is not job-like, while one whose picture's alt text reads like a job still is (2026-11-15)
        footer = agency_email(agency_card("Claims Analyst", AG_1) + '<table role="presentation"><tr><td>'
                              f'<a href="{AG_2}">Privacy Policy</a></td></tr></table>')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(self.write(footer, "p.eml"))]))
        self.assertIn("1 cards read", out.getvalue())
        self.assertNotIn("job-like", out.getvalue())
        pictured = agency_email(agency_card("Claims Analyst", AG_1) + '<table role="presentation"><tr><td>'
                                f'<a href="{AG_2}"><img src="https://cdn.example.com/x.png" alt="Open position"></a>'
                                '</td></tr></table>')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_agency_layout(None, {}, Namespace(files=[str(self.write(pictured, "i.eml"))]))
        self.assertIn("1 link(s) outside the job cards have a job-like label", out.getvalue())
        # the separators of the described form in the mail itself (Codex CR-2026-11-15-GATE-FIXES F7): an alt holding
        # "|c=" is still read whole, and an attribute around the link holding "|alt=" is still not the link's label
        for name, html_, job_like in (
                ("split.eml", f'<a href="{AG_2}"><img src="https://cdn.example.com/x.png" alt="Open |c= position"></a>', True),
                ("chain.eml", f'<table title="x|alt=Open position"><tr><td><a href="{AG_2}">Privacy Policy</a></td></tr></table>', False)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_agency_layout(None, {}, Namespace(files=[str(self.write(agency_email(agency_card("Claims Analyst", AG_1) + html_),
                                                                            name))]))
            self.assertEqual("job-like" in out.getvalue(), job_like, name)

    def test_mail_from_the_agency_sender_is_held_in_a_plain_text_or_new_host_form(self):
        from email.message import EmailMessage
        new_host = agency_card("Claims Analyst", "https://links.harborstaffing.com/job/synthetic")   # no old host anywhere
        message = EmailMessage()
        message["From"] = "Job Alerts <" + "@".join(("synthetic", "email.harborstaffing.com")) + ">"
        message["Subject"] = "Your jobs"
        message["List-Unsubscribe"] = "<https://example.invalid/unsubscribe>"
        message.set_content(f"<html><body>{new_host}</body></html>", subtype="html")
        alert = parse_eml(self.write(bytes(message)))
        self.assertEqual((alert.links, alert.digest_status), ([], "partial"))
        plain = EmailMessage()
        for key in ("From", "Subject", "List-Unsubscribe"):
            plain[key] = message[key]
        plain.set_content("\n".join(("Claims Analyst", "Remote", f"Apply now: {AG_1}", "")))
        alert = parse_eml(self.write(bytes(plain)))
        self.assertEqual((alert.links, alert.digest_status), ([], "partial"))

    def test_agency_mail_without_a_readable_card_is_held_never_read_as_a_newsletter(self):
        changed_layout = f'<strong>Claims Analyst</strong><p>Remote</p><a href="{AG_1}">View job</a>'  # Codex CR-AD-01
        for body in (agency_component(14, "Career tips for this month."), changed_layout,
                     agency_component(14, f'<a href="{AG_1}">Apply Now &rarr;</a>')):
            with self.subTest(body=body[:40]):
                alert = parse_eml(self.write(agency_email(body)))
                self.assertEqual((alert.links, alert.digest_status), ([], "partial"))

    def test_without_a_private_agency_digest_setting_the_mail_is_read_like_any_alert(self):
        import os
        from unittest import mock

        from careeros.core import private_sites
        from careeros.discovery.alerts import is_agency_tracking_url
        with mock.patch.dict(os.environ, {private_sites.ENV: ""}):
            alert = parse_eml(self.write(agency_email()))
            self.assertFalse(is_agency_tracking_url(AG_1))
        self.assertIsNone(alert.digest_status)
        self.assertFalse(any(link.site == "harborstaffing" or link.job_like for link in alert.links))

    def test_a_broken_agency_digest_setting_is_an_error_not_an_ordinary_link(self):
        import os
        from unittest import mock

        from careeros.core import private_sites
        from careeros.discovery.alerts import is_agency_tracking_url
        broken = self.dir / "private_sites.yaml"
        broken.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {private_sites.ENV: str(broken)}):
            with self.assertRaisesRegex(ValueError, "private_sites_invalid"):
                is_agency_tracking_url(AG_1)
            with self.assertRaisesRegex(ValueError, "private_sites_invalid"):
                parse_eml(self.write(agency_email()))

    def test_only_an_unreadable_link_is_skipped_any_other_error_while_reading_links_is_raised(self):
        # The agency-digest lookup now reads the private file before the links, so a broken file fails there first;
        # the link loop's own guard (hotfix HF2: only MalformedLink is skipped) is checked here directly.
        from unittest import mock
        with mock.patch("careeros.discovery.alerts.site_of", side_effect=ValueError("private_sites_invalid: synthetic")):
            with self.assertRaisesRegex(ValueError, "private_sites_invalid"):
                parse_eml(self.write(alert_email()))

    def test_job_alert_search_headers_are_not_saved_as_job_leads(self):
        html = (
            '<a href="https://www.linkedin.com/jobs/search/?keywords=Data%20Analyst">'
            'Your job alert for Data Analyst</a>'
            '<a href="https://www.linkedin.com/jobs/view/4488484632?trk=alert">'
            'Data Analyst Claims</a> Mercury Insurance - Remote'
            '<a href="https://www.indeed.com/jobs?q=claims+analyst">'
            'See more jobs</a>'
            '<a href="https://www.indeed.com/viewjob?jk=3074d70a34bee345">'
            'Business Systems Analyst</a> Sample Employer - Remote'
            '<a href="https://www.glassdoor.com/Job/jobs.htm?sc.keyword=analyst">'
            'Your job alert</a>'
            '<a href="https://[invalid">Malformed link</a>'
        )
        alert = parse_eml(self.write(alert_email(html_body=html)))
        self.assertEqual(
            {link.url for link in alert.links},
            {"https://www.linkedin.com/jobs/view/4488484632",
             "https://www.indeed.com/viewjob?jk=3074d70a34bee345"},
        )
        self.assertTrue(all(link.job_like for link in alert.links))

    def test_targeted_linkedin_search_result_with_specific_job_id_is_kept(self):
        html = (
            '<a href="https://www.linkedin.com/jobs/search/?currentJobId=4488484632">'
            'Data Analyst Claims</a>'
        )
        alert = parse_eml(self.write(alert_email(html_body=html)))
        self.assertEqual(
            [(link.url, link.text) for link in alert.links],
            [("https://www.linkedin.com/jobs/view/4488484632", "Data Analyst Claims")],
        )

    def test_plain_text_alerts_use_the_line_before_the_link(self):
        body = "New jobs for you\n\nPBM Claims Analyst\nPayer Co - Remote\nhttps://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b\n"
        alert = parse_eml(self.write(alert_email(html_body=None, text_body=body)))
        self.assertEqual([(l.url, l.text, l.context) for l in alert.links],
                         [("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "PBM Claims Analyst", "Payer Co - Remote")])

    def test_outlook_markup_keeps_every_job_link_and_title(self):
        """Alert emails carry Outlook-only markup: conditional comments, "<![if !mso]>" sections and VML buttons. Python's
        HTML parser reads some markup differently after 3.12.10, the version the engine ran until the Python 3.14 batch; on
        6,000 synthetic alert emails the links and titles were the same under 3.12.10 and 3.14.10 (2026-11-03)."""
        html = """<html xmlns:v="urn:schemas-microsoft-com:vml"><head><title>Jobs</title>
<!--[if mso]><style>.x{font-family:Arial}</style><![endif]--></head><body>
<div style="display:none">New jobs for you&nbsp;&zwnj;&nbsp;</div>
<!--[if mso | IE]><table role="presentation"><tr><td><![endif]-->
<!--[if mso]><v:roundrect href="https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml"><center>View job</center></v:roundrect><![endif]-->
<!--[if !mso]><!--><a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml">Claims Business Analyst</a><!--<![endif]-->
<p>Synthetic Health · Remote</p>
<![if !mso]><a href="https://jobs.lever.co/acme/abc-1">PBM Operations Analyst</a><![endif]><div>Acme · Remote</div>
<!--[if mso | IE]></td></tr></table><![endif]-->
</body></html>"""
        alert = parse_eml(self.write(alert_email(html_body=html)))
        self.assertEqual([(l.url, l.text) for l in alert.links],
                         [("https://www.linkedin.com/jobs/view/4012345678", "Claims Business Analyst"),
                          ("https://jobs.lever.co/acme/abc-1", "PBM Operations Analyst")])
        self.assertIn("Synthetic Health · Remote", alert.links[0].context)
        self.assertIn("Acme · Remote", alert.links[1].context)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.clock = FixedClock("2026-10-11T12:00:00.000Z")
        self.store = open_store(":memory:", self.clock, POLICY.fingerprint)
        self.ctx = Context(self.store, POLICY, load_profile(FIXTURES / "synthetic_profile.yaml"), JOB_MATRIX, Path(self.tmp.name) / "snap")
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            if request.url.path == "/v1/boards/synth/jobs/555":
                return httpx.Response(200, json=gh_job(555, "Pharmacy Benefits Systems Analyst", postings.GOOD_FIT, req="R-55"))
            return httpx.Response(404)

        self.client = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def run_import(self, name="alert.eml", data=None):
        path = self.inbox / name
        path.write_bytes(data or alert_email())
        return _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")

    def test_an_agency_digest_read_in_part_is_not_imported_and_is_reported_by_its_content(self):
        from unittest import mock
        held = "SELECT entity_type, entity_id, status, message FROM exceptions WHERE error_code = 'agency_digest_held'"
        incomplete = agency_email(agency_card("Claims Analyst", AG_1) + agency_component(18, "<b>Reporting Analyst</b>"))
        counts = self.run_import("digest.eml", incomplete)
        self.assertEqual((counts["lead_new"], counts["agency_digest_partial"]), (1, 1))
        self.assertTrue((self.inbox / "failed" / "2026-10-11" / "digest.eml").exists())
        problem = self.store.one(held)
        self.assertEqual((problem["entity_type"], problem["status"]), ("alert_file", "open"))
        self.assertTrue(problem["entity_id"].startswith("eml:"))   # the email's content, not its file name (CR-AD-05)
        self.assertIn("failed folder", problem["message"])
        self.assertNotIn("Harbor", problem["message"])
        self.assertFalse(self.requests)
        other = self.run_import("digest.eml", agency_email())   # another email saved under the same name
        self.assertEqual((other["lead_new"], other["lead_seen_before"]), (1, 1))
        self.assertTrue((self.inbox / "imported" / "2026-10-11" / "digest.eml").exists())
        self.assertEqual(self.store.one(held)["status"], "open")
        self.run_import("renamed.eml", incomplete)   # the same email under another name, still read in part: one problem
        self.assertEqual(len(self.store.all(held)), 1)
        with mock.patch("careeros.discovery.alerts._AgencyCards.status", return_value="complete"):
            self.run_import("again.eml", incomplete)   # the same email read in full after a fix closes it
        self.assertEqual(self.store.one(held)["status"], "resolved")

    def test_a_saved_digest_is_claimed_before_it_is_read(self):
        from unittest import mock

        import careeros.cli as cli
        held = "SELECT status FROM exceptions WHERE error_code = 'agency_digest_held'"
        incomplete = agency_email(agency_card("Claims Analyst", AG_1) + agency_component(18, "<b>Reporting Analyst</b>"))
        self.run_import("digest.eml", incomplete)   # B is held
        path = self.inbox / "digest.eml"
        for moment in ("_record_alert", "_release_digest"):   # B lands at the path before and after the content is read
            with self.subTest(moment=moment):
                original = getattr(cli, moment)

                def swap(*args, original=original, **kwargs):   # Codex CR-AD-05, Pass 2 and Pass 3
                    result = original(*args, **kwargs)
                    path.write_bytes(incomplete)
                    return result

                with mock.patch.object(cli, moment, side_effect=swap):
                    self.run_import("digest.eml", agency_email())   # A, complete
                self.assertEqual(path.read_bytes(), incomplete)   # B stays in the inbox for the next import
                self.assertEqual(self.store.one(held)["status"], "open")   # A's full read never closes B
                path.unlink()
        imported = sorted(p.name for p in (self.inbox / "imported" / "2026-10-11").iterdir())
        self.assertTrue(all(name.startswith("digest") and name.endswith(".eml") for name in imported))   # A, moved

    def test_recovering_an_interrupted_import_never_overwrites_a_newer_email_of_the_same_name(self):
        from careeros.cli import cmd_import
        first, second = agency_email(), agency_email(agency_card("Billing Specialist", AG_2))
        (self.inbox / "digest.eml.0a1b2c3d.importing").write_bytes(first)   # A, claimed when an import stopped
        (self.inbox / "digest.eml").write_bytes(second)                   # B, saved later under the same name
        cmd_import(self.ctx, {"inbox": self.inbox, "reports": Path(self.tmp.name) / "reports"}, None)   # CR-AD-V2-05
        kept = [p.read_bytes() for p in self.inbox.rglob("*.eml")]
        self.assertIn(first, kept)
        self.assertIn(second, kept)
        self.assertFalse(list(self.inbox.glob("*.importing")))

    def test_a_recognised_digest_is_read_whatever_its_subject(self):
        counts = self.run_import("digest.eml", agency_email(subject="Your next steps: new jobs this week"))   # L1-01
        self.assertEqual((counts["lead_new"], counts.get("correspondence_skipped", 0)), (2, 0))
        skipped = self.run_import("mail.eml", agency_email(subject="Your next steps", bulk=False))   # personal: skipped
        self.assertEqual(skipped["correspondence_skipped"], 1)

    def test_bulk_application_mail_from_the_agencys_sender_is_never_read_for_jobs(self):
        counts = self.run_import("confirm.eml", agency_email(subject="Application received: Claims Analyst"))   # CR-AD-V2-11
        self.assertEqual((counts.get("correspondence_skipped"), counts.get("lead_new", 0)), (1, 0))
        self.assertIsNone(self.store.one("SELECT 1 FROM leads"))
        changed = agency_email(agency_card("Claims Analyst", AG_1) + f'<a href="{AG_2}">View job</a>',
                               subject="A message from Harbor Staffing")   # ambiguous, and not read in full: held
        self.assertEqual(self.run_import("ambiguous.eml", changed)["agency_digest_partial"], 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing")
    def test_an_email_still_open_for_writing_is_left_for_the_next_import(self):
        import ctypes
        from ctypes import wintypes
        path = self.inbox / "digest.eml"
        path.write_bytes(agency_email())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        writer = kernel32.CreateFileW(str(path), 0x40000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)   # open for writing, shared
        self.assertNotIn(writer, (None, ctypes.c_void_p(-1).value))
        try:
            counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")   # CR-AD-V2-05, Pass 2
        finally:
            kernel32.CloseHandle(writer)
        self.assertEqual(counts["alert_file_busy"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["digest.eml"])
        self.assertIsNone(self.store.one("SELECT 1 FROM leads"))
        self.assertEqual(self.run_import("digest.eml", agency_email())["lead_new"], 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing")
    def test_an_email_still_being_written_is_never_judged_by_its_size(self):
        import ctypes
        from ctypes import wintypes
        from careeros.discovery.alerts import MAX_EML_BYTES
        path = self.inbox / "digest.eml"
        path.write_bytes(b"x" * (MAX_EML_BYTES + 1))   # an email part-way through being written (Codex Pass 3, CR-AD-V2-05)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        writer = kernel32.CreateFileW(str(path), 0x40000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)   # open for writing, shared
        self.assertNotIn(writer, (None, ctypes.c_void_p(-1).value))
        try:
            counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")
        finally:
            kernel32.CloseHandle(writer)
        self.assertEqual(counts["alert_file_busy"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["digest.eml"])
        self.assertFalse((self.inbox / "failed").exists())
        self.assertEqual(self.run_import("digest.eml", agency_email())["lead_new"], 2)   # finished: read in full

    @unittest.skipUnless(sys.platform == "win32", "Windows file locking")
    def test_an_email_another_program_has_locked_is_left_for_the_next_import(self):
        import ctypes
        from ctypes import wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wintypes.DWORD),
                        ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.POINTER(OVERLAPPED)]
        kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(OVERLAPPED)]
        path = self.inbox / "digest.eml"
        path.write_bytes(agency_email())
        handle, overlapped = kernel32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None), OVERLAPPED()
        self.assertTrue(kernel32.LockFileEx(handle, 0x2 | 0x1, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)))
        try:   # a byte-range lock, as a program writing the file may hold (the red team before Pass 4)
            counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")
        finally:
            kernel32.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))
            kernel32.CloseHandle(handle)
        self.assertEqual(counts["alert_file_busy"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["digest.eml"])
        self.assertEqual(self.run_import("digest.eml", agency_email())["lead_new"], 2)

    def test_an_email_larger_than_5_mb_is_set_aside_unread(self):
        from careeros.discovery.alerts import MAX_EML_BYTES
        counts = self.run_import("big.eml", b"x" * (MAX_EML_BYTES + 1))
        self.assertEqual(counts["failed"], 1)
        self.assertEqual([p.name for p in (self.inbox / "failed" / "2026-10-11").iterdir()], ["big.eml"])
        self.assertEqual((self.inbox / "failed" / "2026-10-11" / "big.eml").stat().st_size, MAX_EML_BYTES + 1)
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "alert_email_unreadable")

    def test_interview_or_application_mail_from_the_digest_sender_is_never_read_for_jobs(self):
        card = agency_card("Claims Analyst", AG_1)
        counts = self.run_import("interview.eml", agency_mail(digest_html(body=card), subject="Your interview availability"))
        self.assertEqual((counts["correspondence_skipped"], counts.get("lead_new", 0)), (1, 0))   # Codex Pass 3, CR-AD-V2-11
        words = "<p>We received your application and need to confirm your interview availability.</p>"
        counts = self.run_import("next.eml", agency_mail(digest_html(body=card + words), subject="Next steps"))
        self.assertEqual((counts["agency_digest_partial"], counts.get("lead_new", 0)), (1, 0))
        self.assertIsNone(self.store.one("SELECT 1 FROM leads"))
        self.assertIn("application, interview", self.store.one("SELECT message FROM exceptions WHERE error_code = "
                                                               "'agency_digest_held'")["message"])

    def test_an_import_reads_the_settings_once_and_stops_once_they_change(self):
        import os
        from unittest import mock
        import careeros.cli as cli
        from careeros.cli import cmd_import
        from careeros.core import private_sites
        settings = Path(self.tmp.name) / "private_sites.yaml"
        entry = ("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: email.harborstaffing.com, "
                 "tracker_host: click.email.harborstaffing.com, layout: c5d892ac0ea84766}\n")
        settings.write_text(entry, encoding="utf-8")
        (self.inbox / "a.eml").write_bytes(agency_email())
        (self.inbox / "b.eml").write_bytes(agency_email(agency_card("Billing Specialist", f"{AGENCY_TRACKER}?token=synthetic-7")))
        from tests import postings
        (self.inbox / "c.txt").write_text("URL: https://example.com/j/1\nTitle: Claims Analyst\nEmployer: Northgate\n\n"
                                          + postings.GOOD_FIT, encoding="utf-8")   # a saved posting after them (Codex Pass 4)
        real = cli._import_alert

        def then_changed(*args, **kwargs):
            counts = real(*args, **kwargs)
            settings.write_text(entry + "never_fetch: [synthetic.example]\n", encoding="utf-8")   # D141: an edit mid-import
            return counts

        with mock.patch.dict(os.environ, {private_sites.ENV: str(settings)}), mock.patch.object(cli, "_import_alert", then_changed), \
                mock.patch.object(cli, "process") as processed, mock.patch.object(cli, "cmd_report") as reported, \
                self.assertRaises(cli.CommandStopped):   # the whole command ends there (D153 R2)
            cmd_import(self.ctx, {"inbox": self.inbox, "reports": Path(self.tmp.name) / "reports"}, None)
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir() if p.is_file()), ["b.eml", "c.txt"])   # for the next run
        self.assertIsNone(self.store.one("SELECT 1 FROM jobs WHERE canonical_url LIKE '%example.com/j/1%'"))
        processed.assert_not_called()
        reported.assert_not_called()
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'private_sites_changed'"))

    def test_a_change_while_the_last_file_is_read_stops_the_import_before_anything_is_graded_or_reported(self):
        """Codex Pass 5 (D153 R2): the settings changed while the last file, an alert email, was read. The email went
        back to the inbox, but the import still graded the posting saved before it and wrote a report."""
        import os
        from unittest import mock
        import careeros.cli as cli
        from careeros.core import private_sites
        from tests import postings
        settings = Path(self.tmp.name) / "private_sites.yaml"
        entry = ("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: email.harborstaffing.com, "
                 "tracker_host: click.email.harborstaffing.com, layout: c5d892ac0ea84766}\n")
        settings.write_text(entry, encoding="utf-8")
        (self.inbox / "a.txt").write_text("URL: https://example.com/j/1\nTitle: Claims Analyst\nEmployer: Northgate\n\n"
                                          + postings.GOOD_FIT, encoding="utf-8")
        (self.inbox / "z.eml").write_bytes(agency_email())
        real = cli._decide_alert_file

        def changed_while_read(*args, **kwargs):
            settings.write_text(entry + "never_fetch: [synthetic.example]\n", encoding="utf-8")
            return real(*args, **kwargs)

        with mock.patch.dict(os.environ, {private_sites.ENV: str(settings)}), \
                mock.patch.object(cli, "_decide_alert_file", changed_while_read), \
                mock.patch.object(cli, "process") as processed, mock.patch.object(cli, "cmd_report") as reported, \
                self.assertRaises(cli.CommandStopped):
            cli.cmd_import(self.ctx, {"inbox": self.inbox, "reports": Path(self.tmp.name) / "reports"}, None)
        self.assertEqual([p.name for p in self.inbox.iterdir() if p.is_file()], ["z.eml"])   # back for the next import
        processed.assert_not_called()   # the posting saved before it is not graded
        reported.assert_not_called()

    def test_an_email_changed_after_its_read_is_archived_as_read_and_its_new_version_comes_back(self):
        import contextlib
        from unittest import mock
        import careeros.cli as cli
        path = self.inbox / "digest.eml"
        path.write_bytes(agency_email())
        read = path.read_bytes()

        @contextlib.contextmanager
        def unlocked(claimed, limit):   # where no lock keeps a writer out
            yield claimed.read_bytes()[:limit]

        real = cli._decide_alert_file

        def decide(*args):
            result = real(*args)
            next(self.inbox.glob("digest.eml.*.importing")).write_bytes(b"changed")   # written after the read
            return result

        with mock.patch.object(cli, "_steady", unlocked), mock.patch.object(cli, "_decide_alert_file", decide):
            counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")   # CR-AD-V2-05, Pass 2
        self.assertEqual(counts["alert_file_changed"], 1)
        self.assertEqual((self.inbox / "digest.changed-1.eml").read_bytes(), b"changed")
        self.assertEqual((self.inbox / "imported" / "2026-10-11" / "digest.eml").read_bytes(), read)

    def test_an_email_imported_while_the_settings_change_goes_back_to_the_inbox(self):
        import os
        from unittest import mock
        import careeros.cli as cli
        from careeros.core import private_sites
        settings = Path(self.tmp.name) / "private_sites.yaml"
        settings.write_text("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: "
                            "email.harborstaffing.com, tracker_host: click.email.harborstaffing.com, layout: c5d892ac0ea84766}\n",
                            encoding="utf-8")
        real = cli._record_alert

        def rewrite(*args, **kwargs):
            result = real(*args, **kwargs)
            settings.write_text("sources: {}\n", encoding="utf-8")   # CR-AD-V2-07: changed while the email is read
            return result

        path = self.inbox / "digest.eml"
        path.write_bytes(agency_email())
        with mock.patch.dict(os.environ, {private_sites.ENV: str(settings)}), mock.patch.object(cli, "_record_alert", rewrite):
            counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")
        self.assertEqual(counts["alert_config_error"], 1)
        self.assertEqual([p.name for p in self.inbox.glob("*.eml")], ["digest.eml"])   # not archived: read again next time
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "private_sites_changed")
        self.assertIsNone(self.store.one("SELECT 1 FROM leads"))   # nothing of the email kept (one transaction)

    def test_an_entry_removed_just_before_an_email_is_read_sends_it_back_to_the_inbox(self):
        import contextlib
        import os
        from unittest import mock
        import careeros.cli as cli
        from careeros.core import private_sites
        settings = Path(self.tmp.name) / "private_sites.yaml"
        entry = ("agency_digests:\n  - {name: Harbor Staffing, site: harborstaffing, sender_domain: email.harborstaffing.com, "
                 "tracker_host: click.email.harborstaffing.com, layout: c5d892ac0ea84766}\n")
        settings.write_text(entry, encoding="utf-8")
        real = cli._steady

        @contextlib.contextmanager
        def removed_meanwhile(claimed, limit):   # after the per-file check, before the pinned reading (red team S2-01)
            settings.write_text("sources: {}\n", encoding="utf-8")
            with real(claimed, limit) as data:
                yield data

        with mock.patch.dict(os.environ, {private_sites.ENV: str(settings)}):
            self.run_import("first.eml", agency_email())   # set up: enrolled
            path = self.inbox / "digest.eml"
            unused = f"{AGENCY_TRACKER}?token=synthetic-9"
            path.write_bytes(agency_email(agency_card("Claims Analyst", unused)))
            with mock.patch.object(cli, "_steady", removed_meanwhile):
                counts = _import_alert(self.ctx, path, self.client, self.inbox, "2026-10-11")
        self.assertEqual(counts["alert_config_error"], 1)
        self.assertTrue(path.exists())   # back in the inbox, never archived as imported
        self.assertIsNone(self.store.one("SELECT 1 FROM leads WHERE url = ?", (unused,)))
        self.assertTrue(self.store.one("SELECT 1 FROM exceptions WHERE error_code = 'agency_setting_missing'"))
        self.assertTrue(self.store.one("SELECT value FROM meta WHERE key = 'agency_stop_since'")["value"])

    def test_bytes_changed_after_the_read_go_back_to_the_inbox_and_the_archive_keeps_what_was_read(self):
        from careeros.cli import _archived_as_read
        archived = self.inbox / "imported" / "digest.eml"
        archived.parent.mkdir(parents=True)
        archived.write_bytes(b"B")
        self.assertFalse(_archived_as_read(archived, b"A", self.inbox / "digest.eml"))   # CR-AD-V2-05, Pass 2
        self.assertEqual((archived.read_bytes(), (self.inbox / "digest.changed-1.eml").read_bytes()), (b"A", b"B"))
        self.assertTrue(_archived_as_read(archived, b"A", self.inbox / "digest.eml"))

    def test_a_claim_returned_to_a_name_taken_just_before_the_rename_never_overwrites(self):
        from pathlib import Path as P
        from unittest import mock
        from careeros.cli import _return_claim
        claimed, original = self.inbox / "digest.eml.0123abcd.importing", self.inbox / "digest.eml"
        claimed.write_bytes(b"claimed")
        real = P.rename

        def racing(source, target):
            if source == claimed and P(target) == original and not original.exists():
                original.write_bytes(b"newer")   # another email saved under the name between the check and the rename
                raise FileExistsError(str(target))
            return real(source, target)

        with mock.patch.object(P, "rename", racing):
            back = _return_claim(claimed, original)   # CR-AD-V2-05, Pass 2 (WinError 183)
        self.assertEqual((back.name, back.read_bytes(), original.read_bytes()), ("digest.recovered-1.eml", b"claimed", b"newer"))

    def test_an_interrupted_import_returns_its_claimed_email_to_the_inbox(self):
        from careeros.cli import cmd_import
        claimed = self.inbox / "digest.eml.importing"
        claimed.write_bytes(agency_email())
        cmd_import(self.ctx, {"inbox": self.inbox, "reports": Path(self.tmp.name) / "reports"}, None)
        self.assertFalse(claimed.exists())
        self.assertTrue((self.inbox / "imported" / "2026-10-11" / "digest.eml").exists())

    def test_readable_boards_are_graded_and_other_sites_are_only_listed(self):
        counts = self.run_import()
        # only the Greenhouse link was fetched; LinkedIn, Indeed and NHG were never contacted
        self.assertEqual(self.requests, ["https://boards-api.greenhouse.io/v1/boards/synth/jobs/555"])
        leads = {r["site"]: r for r in self.store.all("SELECT * FROM leads WHERE family IS NOT NULL")}
        self.assertEqual(sorted(leads), ["greenhouse", "indeed", "linkedin"])
        # the careers page's card (its link names no posting) is a notice, never a lead (part 3 of D192)
        self.assertEqual([r["site"] for r in self.store.all("SELECT site FROM alert_notices")], ["nhg"])
        self.assertEqual((leads["greenhouse"]["status"], leads["greenhouse"]["job_id"]), ("ingested", "greenhouse:synth:555"))
        self.assertEqual(self.store.one("SELECT title FROM jobs WHERE job_id = 'greenhouse:synth:555'")["title"],
                         "Pharmacy Benefits Systems Analyst")
        self.assertEqual((leads["linkedin"]["title"], leads["linkedin"]["status"]), ("Claims Business Analyst", "new"))
        nurse = self.store.one("SELECT * FROM leads WHERE title = 'Registered Nurse'")
        self.assertEqual((nurse["family"], nurse["status"]), (None, "new"))  # kept for audit, hidden from the review page
        self.assertEqual((counts["lead_ingested"], counts["lead_new"], counts["lead_other_family"], counts["notice_new"]),
                         (1, 2, 1, 1))
        columns = {r["name"] for r in self.store.all("PRAGMA table_info(leads)")}
        self.assertNotIn("candidate@example.com", " ".join(str(v) for r in self.store.all("SELECT * FROM leads") for v in r))
        self.assertEqual(columns, {"lead_id", "url", "site", "title", "title_norm", "family", "context", "sender", "subject",
                                   "received_at", "first_seen_at", "status", "job_id", "profile",
                                   "status_reason", "status_at", "fetch_failure",   # schema 15: CareerOS's own reasons, never mail text
                                   "repeat_of"})   # schema 16: a possible repeat (an earlier lead's id)
        self.assertEqual({k: leads["linkedin"][k] for k in ("status_reason", "status_at", "fetch_failure")},
                         {"status_reason": None, "status_at": None, "fetch_failure": None})
        self.assertEqual(list(self.inbox.glob("*.eml")), [])
        self.assertEqual(len(list((self.inbox / "imported").rglob("*.eml"))), 1)

    def test_the_same_alert_twice_adds_nothing(self):
        self.run_import()
        before = self.store.one("SELECT COUNT(*) c FROM leads")["c"]
        counts = self.run_import("again.eml")
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM leads")["c"], before)
        self.assertGreaterEqual(counts["lead_seen_before"], 4)

    def test_unreadable_email_is_set_aside_with_an_exception(self):
        counts = self.run_import("bad.eml", b"From: x\nContent-Type: text/html; charset=x-no-such-charset\n\n<p>hi</p>")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(len(list((self.inbox / "failed").rglob("*.eml"))), 1)
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "alert_email_unreadable")

    def test_an_email_that_is_only_an_attachment_is_held_for_you(self):
        counts = self.run_import("bad.eml", b"From: x\nContent-Type: application/pdf\n\n%PDF-1.4")   # D153 R8
        self.assertEqual((counts["failed"], counts["agency_digest_partial"]), (0, 1))
        self.assertEqual(len(list((self.inbox / "failed").rglob("*.eml"))), 1)
        self.assertEqual(self.store.one("SELECT error_code FROM exceptions")["error_code"], "agency_digest_held")


if __name__ == "__main__":
    unittest.main()
