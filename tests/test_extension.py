"""The CareerOS Assist browser extension (P4, 2026-10-21; ADR 0009): its permissions and its source.

These tests read the extension's files and check:
- the manifest grants only what filling one page you chose needs;
- no code path can submit, click, press keys, upload, navigate, store or send anything. A small JavaScript lexer sets
  comments aside and finds every string and regular-expression literal, so text in one can't hide code in another
  (third Claude review of the Codex corrections, 2026-10-22). Then:
  - a deny-list of known calls;
  - allow-lists of every name, member, string literal, regular expression and subscript each script uses (second and
    third reviews), so a new spelling of the same thing (HTMLFormElement.prototype.submit.call, el["form"],
    new Image().src, world: "MAIN", chrome.windows, a "submit" event) fails here until someone looks at it;
  - the only events it raises are "input" and "change", and no object key is computed;
- the popup accepts exactly the handoff files CareerOS writes, and the three copies of each adapter's reviewed sites
  and field names (CareerOS, the popup, the page function) are equal, each key written once.

How it behaves is run for real, in Node, by tests/test_extension_behaviour.py (when a Node runtime is available).
"""
import json
import re
import unittest

from careeros.paths import ENGINE_ROOT

EXT = ENGINE_ROOT / "extension"
FORBIDDEN = {
    "submits a form": r"\.submit\b|requestSubmit|\bsubmit\b|\bform\b",
    "clicks something": r"\.click\b|MouseEvent|PointerEvent|\bclick\b",
    "presses a key": r"KeyboardEvent|\bkey(?:down|press|up)\b",
    "sends or fetches anything": r"\bfetch\b|XMLHttpRequest|WebSocket|sendBeacon|EventSource|\bImage\b|navigator|defaultView",
    "runs code from text": r"\beval\s*\(|new\s+Function|setTimeout\s*\(\s*['\"]|importScripts",
    "stores applicant data": r"chrome\.storage|localStorage|sessionStorage|indexedDB|document\.cookie",
    "talks to a native program": r"connectNative|sendNativeMessage|chrome\.runtime",
    "navigates or opens pages": r"window\b|\bopen\s*\(|\blocation\s*\.\s*\w+\s*=(?!=)|\blocation\s*=(?!=)|"
                                r"location\.(?:assign|replace|reload)|chrome\.(?:windows|tabs\.(?!query\b))|history\.",
    "writes HTML": r"innerHTML|outerHTML|insertAdjacentHTML|document\.write",
    "touches file inputs": r"\.files\s*=|DataTransfer",
    "runs in the page's own world or in every frame": r"\bworld\b|allFrames|frameIds|documentIds",
}
# Every member and bare name each script uses, with comments and literals set aside. Add a name here only after
# checking it can't submit, click, navigate, store or send anything.
MEMBERS = {
    "fill.js": {"adapter", "already", "call", "disabled", "dispatchEvent", "expires_at", "field", "filled", "filter",
                "from", "getAttribute", "getOwnPropertyDescriptor", "hasOwnProperty", "includes", "index", "kind",
                "length", "missing", "multiple", "name", "now", "options", "origin", "origins", "outline", "parentNode",
                "parse", "path", "pathname", "prototype", "push", "querySelectorAll", "refused", "selectedIndex", "set",
                "skipped", "startsWith", "style", "tagName", "test", "text", "trim", "value"},
    "popup.js": {"adapter", "addEventListener", "already", "appendChild", "attach", "call", "careeros_handoff",
                 "className", "concat", "createElement", "destination", "disabled", "employer", "errors", "every",
                 "executeScript", "expires_at", "field", "fields", "files", "filled", "getElementById", "hasOwnProperty",
                 "hidden", "id", "includes", "isArray", "isNaN", "job", "join", "kind", "label", "length", "map",
                 "missing", "name", "now", "onclick", "origin", "origins", "package_id", "parse", "path", "pathname",
                 "prototype", "push", "query", "refused", "replace", "replaceChildren", "result", "review", "scripting",
                 "session_id", "skipped", "startsWith", "tab", "tabs", "target", "test", "text", "textContent", "title",
                 "url", "value"},
}
GLOBALS = {   # names that are neither keywords nor declared in the script: what it reaches outside itself
    "fill.js": {"Array", "Date", "Event", "HTMLInputElement", "HTMLSelectElement", "HTMLTextAreaElement", "NaN",
                "Object", "String", "document", "location"},
    "popup.js": {"Array", "Date", "JSON", "NaN", "Number", "Object", "URL", "chrome", "document", "fillFields",
                 "undefined"},
}
KEYWORDS = {"async", "await", "break", "catch", "const", "continue", "else", "false", "for", "function", "if",
            "instanceof", "let", "new", "null", "of", "return", "true", "try", "typeof"}
SUBSCRIPTS = {   # what may stand inside [...] after a name, a call or a bracket (destructuring included)
    "fill.js": {"0", "el.selectedIndex", "guard.adapter"},
    "popup.js": {"0", "data.adapter", "run", "tab"},
}
STRINGS = {   # every string literal each script holds: a new one ("submit", "form", "allFrames") fails until reviewed
    "fill.js": {
        '',
        ' (',
        ' (already chosen)',
        ' (already filled)',
        ' (different kind of field)',
        ' (more than one choice reads like this)',
        ' (no such choice)',
        ' (not a field this adapter fills)',
        ' (not a plain list of one choice)',
        ' (that choice is disabled)',
        " (the choice didn't take)",
        ' fields on the page answer to this name)',
        '"]',
        '"], [name="',
        '/',
        '2px solid #1a7f37',
        'OPTGROUP',
        '[id="',
        'change',
        'email',
        'https://boards.greenhouse.io',
        'https://job-boards.greenhouse.io',
        'input',
        'no reviewed destination was given',
        'select',
        'selectedIndex',
        'string',
        'tel',
        'text',
        'textarea',
        'the handoff has expired or claims a longer life than CareerOS gives',
        "this page isn't the posting the handoff is for",
        'type',
        'undefined',
        'url',
        'value',
    },
    "popup.js": {
        '',
        ' ',
        ' Then close the session in CareerOS.',
        ' and try again',
        ' at ',
        ', ',
        '.',
        '. Already right: ',
        '. Left for you: ',
        '. Not found: ',
        '. Now review the whole form, attach your files, answer the rest, and submit it yourself.',
        '/',
        ': ',
        '; ',
        'Check every value. Pressing the button fills them; nothing is submitted.',
        'Filled ',
        'Not filled: ',
        "This file isn't readable JSON.",
        '_',
        "a field in the file isn't one this adapter fills",
        'attach your ',
        'change',
        'error',
        'fields',
        'fields must be a list',
        'fill',
        'handoff',
        'https://boards.greenhouse.io',
        'https://job-boards.greenhouse.io',
        'job',
        'li',
        'none',
        'not a CareerOS handoff file',
        'note',
        'ok',
        'preview',
        'result',
        'review',
        'select',
        'status',
        'string',
        'text',
        'textarea',
        "the destination isn't a posting address",
        "the destination isn't one of this adapter's reviewed sites",
        'the file is missing what CareerOS writes (its session, job, review list or attachments)',
        'this handoff claims a longer life than CareerOS gives',
        'this handoff has expired: open a new session in CareerOS',
        "this handoff's expiry isn't a time CareerOS writes (UTC, ending in Z)",
        "this tab isn't the posting the handoff is for: open the application form for ",
        'unknown form adapter',
        'use strict',
    },
}
REGEXES = {   # every regular expression, in order
    "fill.js": [
        '/^\\/[A-Za-z0-9_-]{1,100}\\/jobs\\/[0-9]{1,20}$/',
        '/^(?:first_name|last_name|preferred_name|email|phone|question_[0-9]{1,20})$/',
        '/^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d(?:\\.\\d{3})?Z$/',
        '/^\\s*$|^\\s*(?:select|choose|please)\\b|^\\s*[-\\u2013\\u2014.]+\\s*$/i',
    ],
    "popup.js": [
        '/^\\/[A-Za-z0-9_-]{1,100}\\/jobs\\/[0-9]{1,20}$/',
        '/^(?:first_name|last_name|preferred_name|email|phone|question_[0-9]{1,20})$/',
        '/^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d(?:\\.\\d{3})?Z$/',
    ],
}


def lex(source: str) -> tuple[str, list[str], list[str]]:
    """(code with each string literal as "" and each regular expression as REGEX, the strings, the regular
    expressions), comments dropped. A template literal, or a literal that doesn't end on its line, is refused."""
    code, strings, regexes = [], [], []
    i, n, last = 0, len(source), ""
    while i < n:
        c = source[i]
        if c in "\"'":
            j = i + 1
            while source[j] != c:
                if source[j] == "\\":
                    j += 1
                if source[j] == "\n":
                    raise ValueError(f"a string literal that doesn't end on its line, at {i}")
                j += 1
            strings.append(source[i + 1:j])
            code.append('""')
            last, i = '"', j + 1
            continue
        if c == "`":
            raise ValueError("a template literal")
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if source.startswith("/*", i):
            i = source.index("*/", i) + 2
            continue
        if c == "/" and (last == "" or last in "=(:,[!&|?{};" or re.search(r"\breturn\s*$", "".join(code[-3:]))):
            j, in_class = i + 1, False
            while True:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    raise ValueError(f"a regular expression that doesn't end on its line, at {i}")
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            k = j + 1
            while k < n and source[k].isalpha():
                k += 1
            regexes.append(source[i:k])
            code.append(" REGEX ")
            last, i = "R", k
            continue
        code.append(c)
        if not c.isspace():
            last = c
        i += 1
    return "".join(code), strings, regexes


def code(name: str) -> str:
    """The script with comments dropped and its literals set aside (lex)."""
    return lex((EXT / name).read_text(encoding="utf-8"))[0]


def source(name: str) -> str:
    """The script as written, without its comments (each comment in these files is a whole line)."""
    return re.sub(r"(?m)^\s*//.*$", "", (EXT / name).read_text(encoding="utf-8"))


def names(name: str) -> tuple[set, set, set]:
    """(members, bare names, declared names) of a script, with comments and literals set aside."""
    return names_in(code(name))


def names_in(text: str) -> tuple[set, set, set]:
    """names() for code already lexed."""
    members = set(re.findall(r"\.\s*([A-Za-z_$][\w$]*)", text))
    bare = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)", text)) - {"REGEX"}
    declared = set(re.findall(r"\b(?:const|let|function|class)\s+([A-Za-z_$][\w$]*)", text))
    declared |= {n for group in re.findall(r"\b(?:const|let)\s+\[([^\]]*)\]", text) for n in re.findall(r"\w+", group)}
    for params in re.findall(r"function\s*\w*\s*\(([^)]*)\)|\(([^()]*)\)\s*=>|\b(\w+)\s*=>|catch\s*\((\w+)\)", text):
        declared |= {n for p in params for n in re.findall(r"[A-Za-z_$][\w$]*", p)}
    # object keys only, e.g. { active: true }: a name after "{" or "," and before ":". The middle of a ? : is not a key,
    # so `x ? open : 0` is still held to the allow-list (read-only check of the Codex package, 2026-10-22)
    keys = set(re.findall(r"[{,]\s*([A-Za-z_$][\w$]*)\s*:(?!:)", text))
    return members, bare, declared | keys


def subscripts(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"[\w$)\]]\s*\[", text):
        depth, j = 1, m.end()
        while depth:
            depth += {"[": 1, "]": -1}.get(text[j], 0)
            j += 1
        out.append(text[m.end():j - 1].strip())
    return out


class ManifestTests(unittest.TestCase):
    def test_only_the_active_tab_and_scripting_permissions(self):
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["permissions"], ["activeTab", "scripting"])
        for key in ("host_permissions", "optional_host_permissions", "optional_permissions", "content_scripts", "background",
                    "externally_connectable", "web_accessible_resources", "content_security_policy"):
            self.assertNotIn(key, manifest, key)
        self.assertEqual(manifest["action"]["default_popup"], "popup.html")

    def test_the_popup_loads_only_its_own_scripts(self):
        html = (EXT / "popup.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<script src="([^"]+)"', html), ["fill.js", "popup.js"])
        self.assertNotRegex(html, r"<script>|<script [^>]*>[^<]|https?://")   # no inline or remote script, no remote anything
        for name in ("fill.js", "popup.js"):
            self.assertTrue((EXT / name).is_file(), name)


class SourceTests(unittest.TestCase):
    def test_no_script_can_submit_click_type_send_store_or_navigate(self):
        for name in ("fill.js", "popup.js"):
            text = code(name)   # words in messages don't count, and a literal can't hide code
            for what, pattern in FORBIDDEN.items():
                self.assertIsNone(re.search(pattern, text), f"{name} {what}")

    def test_each_script_uses_only_the_names_and_members_allowed_for_it(self):
        """Second Claude review: allow-lists, so another spelling of submit, click, navigate or send can't slip by."""
        for name in ("fill.js", "popup.js"):
            members, bare, declared = names(name)
            self.assertEqual(members - MEMBERS[name], set(), f"{name}: members not on its allow-list")
            self.assertEqual(bare - declared - KEYWORDS - GLOBALS[name], set(), f"{name}: names not on its allow-list")
        self.assertEqual(sorted(set(re.findall(r"chrome\.[\w.]+", code("popup.js")))),
                         ["chrome.scripting.executeScript", "chrome.tabs.query"])

    def test_only_object_keys_are_exempt_from_the_name_list(self):
        """Read-only check of the Codex package (2026-10-22): a name in the middle of a ? : is a name, not a key."""
        members, bare, declared = names_in(lex('const a = x ? open : 0; const o = { active: true, b: c }; f({ d: 1 }, { e: 2 });')[0])
        self.assertTrue({"open", "x", "c", "f"} <= bare - declared, bare - declared)
        self.assertTrue({"active", "b", "d", "e"} <= declared, declared)

    def test_each_script_uses_only_the_literals_and_subscripts_allowed_for_it(self):
        """Third Claude review: a string can't name a member (el["form"]), a regular expression can't pair quotes to
        hide code, no object key is computed, and the only events raised are input and change."""
        for name in ("fill.js", "popup.js"):
            text, strings, regexes = lex((EXT / name).read_text(encoding="utf-8"))
            self.assertEqual(set(strings) - STRINGS[name], set(), f"{name}: strings not on its allow-list")
            self.assertEqual(regexes, REGEXES[name], f"{name}: regular expressions")
            self.assertFalse([r for r in regexes if '"' in r or "'" in r], f"{name}: a quote inside a regular expression")
            self.assertEqual(set(subscripts(text)) - SUBSCRIPTS[name], set(), f"{name}: subscripts not on its allow-list")
            self.assertIsNone(re.search(r"[{,]\s*\[", text), f"{name}: a computed object key")
            made = re.findall(r"new\s+(\w+)\s*\(\s*(\S)", text)   # what each script constructs
            self.assertTrue(all(kind in ("Event", "URL") for kind, _ in made), f"{name}: {made}")
            self.assertTrue(all(arg == '"' for kind, arg in made if kind == "Event"), f"{name}: {made}")
        raw = (EXT / "fill.js").read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r'new Event\("(\w+)"', raw))), ["change", "input"])
        self.assertEqual(len(re.findall(r"new\s+Event\s*\(", raw)), 2)

    def test_the_page_script_is_one_function_that_never_overwrites_your_typing(self):
        text = source("fill.js")
        self.assertEqual(re.findall(r"(?m)^function (\w+)", text), ["fillFields"])
        self.assertIsNone(re.search(r"(?m)^(?!function|\}|\s|$)", text), "fill.js runs code outside fillFields")
        # The page and expiry checks come first; a value you typed or a choice already made is left alone. How these
        # behave is run in tests/test_extension_behaviour.py; here: they come before any value is set.
        first_write = text.index("function announce")
        for guard in ("adapter.path.test(guard.path)", "adapter.origins.includes(here.origin)", "expires - now > LIFETIME_MS"):
            self.assertLess(text.index(guard), first_write, guard)
        self.assertLess(text.index("if (el.value) {"), text.index("setValue(el, field.value);"))
        for check in ("if (!placeholder) {", "if (option.disabled ||"):
            self.assertLess(text.index(check), text.index("choose(el, option.index);"), check)
        self.assertEqual(text.count("setValue("), 2)                           # its definition and the one guarded call
        self.assertEqual(text.count("choose("), 2)

    def test_the_popup_fills_only_after_your_click_and_only_on_the_named_posting(self):
        text = source("popup.js")
        fill_calls = [m.start() for m in re.finditer(r"executeScript", text)]
        self.assertEqual(len(fill_calls), 1)
        self.assertGreater(fill_calls[0], text.index("button.onclick"))       # only inside the button's handler
        handler = text[text.index("button.onclick"):]
        self.assertLess(handler.index("checkNow(data, tabId)"), handler.index("executeScript"))   # every check again, at Fill
        self.assertIn("onThePosting(data, tab.url)", text)
        self.assertIn("(tabId === undefined || tab.id === tabId)", text)
        spec = re.search(r"executeScript\(\{(.*?)\}\);", text, re.S).group(1)
        self.assertEqual(re.findall(r"(?m)^\s*(\w+):", spec), ["target", "func", "args"])   # the top frame of that tab
        self.assertIn("target: { tabId: now.tab.id },", spec)
        self.assertIn("guard],", spec)                                         # and the page checks it once more

    def test_careeros_the_popup_and_the_page_hold_the_same_reviewed_destinations_and_fields(self):
        """The extension's reviewed hosts, posting path and field names are the ones CareerOS writes handoffs for, so a
        handoff file can't grant itself more (independent Codex review of ac83e00, F6). The page function holds its
        own copy, so the guard can't widen them either (second Claude review). Each key is written once: JavaScript
        would keep a repeated key's last value (third review); the values JavaScript itself reads are compared in
        tests/test_extension_behaviour.py."""
        from careeros.review import adapters
        for name in ("popup.js", "fill.js"):
            text = source(name)
            table = text[text.index("const ADAPTERS"):text.index("};", text.index("const ADAPTERS"))]
            self.assertEqual(re.findall(r"(?m)^\s+(\w+):", table), ["greenhouse", "origins", "path", "field"], name)
            block = text[text.index("greenhouse: {"):text.index("},", text.index("greenhouse: {"))]
            self.assertEqual(json.loads(re.search(r"origins: (\[.*?\]),", block).group(1)), list(adapters.GREENHOUSE_ORIGINS),
                             name)
            self.assertEqual(re.search(r"path: /(.+)/,", block).group(1).replace("\\/", "/"), adapters.GREENHOUSE_PATH, name)
            field = re.search(r"field: /(.+)/,", block).group(1)
            self.assertEqual(field, adapters.GREENHOUSE_FIELD, name)
        text = source("popup.js")
        kinds = json.loads(re.search(r"const KINDS = (\[.*\]);", text).group(1))
        self.assertEqual(sorted(kinds), sorted({"text", "textarea", "select"}))
        found = adapters.greenhouse_questions({"questions": [
            {"label": "First Name", "fields": [{"name": "first_name", "type": "input_text"}]},
            {"label": "Q", "fields": [{"name": "question_12345678", "type": "textarea"}]}]})
        for q in found:
            self.assertRegex(q["name"], field)
        for bad in ('x" onfocus="alert(1)', "job_application[answers_attributes][0][text_value]", "unreviewed_field",
                    "question_1\n"):
            self.assertIsNone(re.fullmatch(field, bad), bad)
        lifetimes = {re.search(r"const LIFETIME_MS = (.*?);", source(n)).group(1) for n in ("popup.js", "fill.js")}
        self.assertEqual(lifetimes, {"(2 * 60 + 5) * 60 * 1000"})              # assist.HANDOFF_HOURS, plus skew
        from careeros.review import assist
        self.assertEqual(assist.HANDOFF_HOURS, 2)


if __name__ == "__main__":
    unittest.main()
