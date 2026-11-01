// Behavioural checks of the CareerOS Assist extension (independent Codex review of ac83e00 and the second Claude review
// of its corrections, 2026-10-22). Runs the real extension/fill.js and extension/popup.js in a Node VM, with a
// synthetic page and fake Chrome APIs. No browser, network, real form or applicant data is used.
// Usage: node tests/extension_harness.cjs > results.json. tests/test_extension_behaviour.py reads the JSON.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const EXT = path.join(__dirname, "..", "extension");
const SOURCES = ["fill.js", "popup.js"].map((n) => fs.readFileSync(path.join(EXT, n), "utf8"));
const T0 = Date.parse("2026-10-22T10:00:00Z");
const POSTING = "https://job-boards.greenhouse.io/synth/jobs/1";
const CHROME_CALLS = ["tabs", "tabs.query", "scripting", "scripting.executeScript"];

function world() {
  // url: where the tab is as chrome.tabs reports it; pageUrl: where the page itself is when the function runs
  const state = { now: T0, url: POSTING, pageUrl: null, tabId: 9, activeId: 9, executions: 0, chrome: [], spec: null,
                  target: null };
  class Element {
    constructor() { this._value = ""; this.style = {}; this.handlers = {}; this.children = []; this.events = []; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    dispatchEvent(event) { this.events.push(event.type); }
    getAttribute(name) { return name === "type" ? this.type : null; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
  }
  // a text box drops line breaks, as a browser's does (the checks must read what the page will hold)
  class Input extends Element {
    get value() { return this._value; }
    set value(v) { this._value = this.type === "text" ? String(v).replace(/[\r\n]/g, "") : String(v); }
  }
  class TextArea extends Element { get value() { return this._value; } set value(v) { this._value = String(v); } }
  // A plain list, following the HTML rules that matter here: setting the value picks the first option with that
  // value; setting selectedIndex picks that exact option.
  class Select extends Element {
    constructor(options, selected, extra) {
      super();
      this.options = options.map((o, index) => Object.assign({ disabled: false, parentNode: o.group || null }, o, { index }));
      this._index = selected || 0;
      this.multiple = Boolean(extra && extra.multiple);
    }
    get selectedIndex() { return this._index; }
    set selectedIndex(i) { this._index = i >= 0 && i < this.options.length ? i : -1; }
    get value() { return this._index >= 0 ? this.options[this._index].value : ""; }
    set value(v) { this._index = this.options.findIndex((o) => o.value === v); }
  }
  const nodes = Object.fromEntries(["handoff", "status", "preview", "fill", "job", "fields", "review", "result"]
    .map((n) => [n, new Element()]));
  // The synthetic page: elements in document order, each with an id and/or a name.
  const elements = [];
  const add = (el, id, name) => { el.id = id; el.name = name; elements.push(el); return el; };
  const page = { first_name: add(Object.assign(new Input(), { type: "text" }), "first_name", "first_name") };
  const popupDoc = { getElementById: (id) => nodes[id], createElement: () => new Element() };
  const pageDoc = {
    querySelectorAll: (selector) => {
      const m = /^\[id="([^"]*)"\], \[name="([^"]*)"\]$/.exec(selector);
      if (!m) throw new Error("unexpected selector: " + selector);
      return elements.filter((el) => el.id === m[1] || el.name === m[2]);
    },
  };
  class FakeDate extends Date { static now() { return state.now; } }
  const location = () => {
    const u = new URL(state.pageUrl || state.url);
    return { origin: u.origin, pathname: u.pathname, href: u.href };
  };
  let context;
  // chrome, recording every member the extension touches: only tabs.query and scripting.executeScript are expected
  const record = (prefix, target) => new Proxy(target, {
    get(obj, prop) {
      const name = prefix ? prefix + "." + String(prop) : String(prop);
      state.chrome.push(name);
      const value = obj[prop];
      return value && typeof value === "object" ? record(name, value) : value;
    },
  });
  const chrome = record("", {
    tabs: { query: async () => [{ id: state.activeId, url: state.url }] },
    scripting: {
      executeScript: async (spec) => {
        state.executions += 1;
        state.spec = Object.keys(spec).sort();
        state.target = Object.keys(spec.target || {}).sort();
        context.document = pageDoc;
        context.location = location();
        try { return [{ result: spec.func(...spec.args) }]; } finally { context.document = popupDoc; context.location = undefined; }
      },
    },
  });
  context = vm.createContext({ document: popupDoc, chrome, URL, Date: FakeDate, console,
    Event: class { constructor(type) { this.type = type; } },
    HTMLInputElement: Input, HTMLTextAreaElement: TextArea, HTMLSelectElement: Select });
  for (const source of SOURCES) vm.runInContext(source, context);
  const handoff = {
    careeros_handoff: 1, adapter: "greenhouse", session_id: "ses:synthetic", package_id: "pkg:synthetic",
    manifest_sha256: "a".repeat(64), expires_at: "2026-10-22T12:00:00.000Z",
    destination: { origins: ["https://boards.greenhouse.io", "https://job-boards.greenhouse.io"], path: "/synth/jobs/1" },
    job: { title: "Synthetic Analyst", employer: "Synthetic Health" },
    fields: [{ name: "first_name", label: "First Name", kind: "text", value: "Avery" }], review: [], attach: [],
  };
  const load = async (data) => nodes.handoff.handlers.change({ target: { files: [{ text: async () => JSON.stringify(data || handoff) }] } });
  // run fillFields as if inside the page at state.url
  const inPage = (fields, guard) => {
    context.document = pageDoc;
    context.location = location();
    try { return context.fillFields(fields, guard); } finally { context.document = popupDoc; context.location = undefined; }
  };
  const guard = () => ({ adapter: "greenhouse", path: handoff.destination.path, expires_at: handoff.expires_at });
  const copy = (change) => Object.assign(JSON.parse(JSON.stringify(handoff)), change);
  return { state, nodes, page, handoff, load, inPage, guard, copy, add, Select, Input, TextArea, context };
}

const out = {};
(async () => {
  // F1: the posting, the tab and the expiry, at the preview, at the click and inside the page
  let w = world();
  await w.load();
  await w.nodes.fill.onclick();
  out.same_posting = { executions: w.state.executions, value: w.page.first_name.value, spec: w.state.spec,
                       target: w.state.target, events: w.page.first_name.events, result: w.nodes.result.textContent };

  w = world();
  await w.load();
  w.state.pageUrl = "https://job-boards.greenhouse.io/synth/jobs/12";   // the page moved after the popup's last look
  await w.nodes.fill.onclick();
  out.page_refused_through_popup = { executions: w.state.executions, value: w.page.first_name.value,
                                     result: w.nodes.result.textContent, className: w.nodes.result.className };

  w = world();
  await w.load();
  w.state.url = "https://job-boards.greenhouse.io/synth/jobs/2";
  await w.nodes.fill.onclick();
  out.moved_after_preview = { executions: w.state.executions, value: w.page.first_name.value, result: w.nodes.result.textContent };

  w = world();
  await w.load();
  w.state.url = "https://job-boards.greenhouse.io/synth/jobs/12";        // a longer posting number, same prefix
  await w.nodes.fill.onclick();
  out.moved_to_longer_number = { executions: w.state.executions, value: w.page.first_name.value };

  w = world();
  w.state.url = "https://job-boards.greenhouse.io/synth/jobs/12";
  await w.load();
  out.loaded_on_longer_number = { executions: w.state.executions, handler: Boolean(w.nodes.fill.onclick),
                                  status: w.nodes.status.textContent };

  w = world();
  await w.load();
  w.state.activeId = 10;                                                   // another tab, on the same posting, in front
  await w.nodes.fill.onclick();
  out.other_tab_at_fill = { executions: w.state.executions, value: w.page.first_name.value };

  w = world();
  await w.load();
  w.state.now = Date.parse("2026-10-22T13:00:00Z");
  await w.nodes.fill.onclick();
  out.expired_after_preview = { executions: w.state.executions, value: w.page.first_name.value, result: w.nodes.result.textContent };

  const pageGuard = (url, guard, now) => {
    const p = world();
    if (url) p.state.url = url;
    if (now) p.state.now = now;
    return { result: p.inPage(p.handoff.fields, guard === undefined ? p.guard() : guard(p)), value: p.page.first_name.value };
  };
  out.page_guard_other_posting = pageGuard("https://job-boards.greenhouse.io/synth/jobs/2");
  out.page_guard_longer_number = pageGuard("https://job-boards.greenhouse.io/synth/jobs/12");
  out.page_guard_expired = pageGuard(null, undefined, Date.parse("2026-10-22T12:00:01Z"));
  out.page_guard_missing = pageGuard(null, () => undefined);
  out.page_guard_empty_path = pageGuard(null, (p) => Object.assign(p.guard(), { path: "" }));
  out.page_guard_no_adapter = pageGuard(null, (p) => ({ path: "/synth/jobs/1", expires_at: p.handoff.expires_at }));
  out.page_guard_other_adapter = pageGuard(null, (p) => Object.assign(p.guard(), { adapter: "workday" }));
  out.page_guard_inherited_adapter = pageGuard(null, (p) => Object.assign(p.guard(), { adapter: "toString" }));
  out.page_guard_odd_path = pageGuard("https://job-boards.greenhouse.io/synth/apply", (p) => Object.assign(p.guard(), { path: "/synth/apply" }));
  out.page_guard_unlisted_origin = pageGuard("https://unlisted.example.org/synth/jobs/1");
  out.page_guard_self_declared_origin = pageGuard("https://unlisted.example.org/synth/jobs/1",
    (p) => Object.assign(p.guard(), { origins: ["https://unlisted.example.org"] }));   // the page ignores a guard's origins
  out.page_guard_far_future = pageGuard(null, (p) => Object.assign(p.guard(), { expires_at: "9999-12-31T23:59:59Z" }));
  out.page_guard_three_hours = pageGuard(null, (p) => Object.assign(p.guard(), { expires_at: "2026-10-22T13:00:00.000Z" }));
  out.page_guard_cap_plus_1ms = pageGuard(null, (p) => Object.assign(p.guard(), { expires_at: "2026-10-22T12:05:00.001Z" }));
  out.page_guard_at_cap = pageGuard(null, (p) => Object.assign(p.guard(), { expires_at: "2026-10-22T12:05:00.000Z" }));
  out.page_guard_path_elsewhere = pageGuard("https://job-boards.greenhouse.io/x/synth/jobs/1/apply");
  // an hour ahead if read as UTC (the test runs this file with TZ=UTC), so only the format check refuses it
  out.page_guard_zoneless = pageGuard(null, (p) => Object.assign(p.guard(), { expires_at: "2026-10-22T11:00:00" }));

  w = world();
  w.add(Object.assign(new w.Input(), { type: "text" }), "unreviewed_field", "unreviewed_field");
  out.page_unreviewed_field = { result: w.inPage([{ name: "unreviewed_field", kind: "text", value: "Avery" }], w.guard()),
                                value: w.page.first_name.value };

  // F5 and the second review: choices are kept, and picked by position
  const select = (options, selected, value) => {
    const s = world();
    s.page.question_1 = s.add(new s.Select(options, selected), "question_1", "question_1");
    const result = s.inPage([{ name: "question_1", kind: "select", value: value || "Yes" }], s.guard());
    const el = s.page.question_1;
    return { result, value: el.value, index: el.selectedIndex, text: el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : null };
  };
  out.select_first_real_choice = select([{ text: "No", value: "0" }, { text: "Yes", value: "1" }], 0);
  out.select_placeholder = select([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }, { text: "No", value: "0" }], 0);
  out.select_empty_value_real_choice = select([{ text: "No", value: "" }, { text: "Yes", value: "1" }], 0);
  out.select_disabled_placeholder = select([{ text: "Pick one", value: "", disabled: true }, { text: "Yes", value: "1" }], 0);
  out.select_blank_placeholder = select([{ text: "", value: "" }, { text: "Yes", value: "1" }], 0);
  out.select_later_real_choice = select([{ text: "Select...", value: "" }, { text: "No", value: "0" }, { text: "Yes", value: "1" }], 1);
  out.select_later_real_choice_empty_value = select([{ text: "Select...", value: "" }, { text: "No", value: "" },
    { text: "Yes", value: "1" }], 1);
  out.select_already_matching = select([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }], 1);
  out.select_shared_value = select([{ text: "Select...", value: "" }, { text: "No", value: "true" }, { text: "Yes", value: "true" }], 0);
  out.select_empty_value_target = select([{ text: "Select...", value: "" }, { text: "Yes", value: "" }], 0);
  out.select_disabled_target = select([{ text: "Select...", value: "" }, { text: "Yes", value: "1", disabled: true }], 0);
  out.select_two_choices_alike = select([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }, { text: "Yes ", value: "2" }], 0);
  out.select_disabled_group = select([{ text: "Select...", value: "" },
    { text: "Yes", value: "1", group: { tagName: "OPTGROUP", disabled: true } }], 0);
  const several = world();
  several.page.question_1 = several.add(new several.Select([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }],
                                                           0, { multiple: true }), "question_1", "question_1");
  out.select_multiple = { result: several.inPage([{ name: "question_1", kind: "select", value: "Yes" }], several.guard()),
                          index: several.page.question_1.selectedIndex };
  const stuck = world();
  // a page script that puts the old choice back when it hears the change (a controlled list rejecting the value)
  class Stuck extends stuck.Select { dispatchEvent(e) { super.dispatchEvent(e); if (e.type === "change") this._index = 0; } }
  stuck.page.question_1 = stuck.add(new Stuck([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }], 0),
                                    "question_1", "question_1");
  out.select_that_doesnt_take = stuck.inPage([{ name: "question_1", kind: "select", value: "Yes" }], stuck.guard());
  const placed = world();
  placed.page.question_1 = placed.add(new placed.Select([{ text: "Select...", value: "" }, { text: "Yes", value: "1" }], 0),
                                      "question_1", "question_1");
  placed.inPage([{ name: "question_1", kind: "select", value: "Yes" }], placed.guard());
  out.select_events = placed.page.question_1.events;
  for (const type of ["hidden", "password", "search"]) {
    const h = world();
    h.page.first_name.type = type;
    out["input_" + type] = { result: h.inPage(h.handoff.fields, h.guard()), value: h.page.first_name.value };
  }

  w = world();
  w.page.first_name.value = "Typed";
  out.typed_value = { result: w.inPage(w.handoff.fields, w.guard()), value: w.page.first_name.value };

  w = world();
  w.page.first_name.value = "Avery";
  out.text_already_right = { result: w.inPage(w.handoff.fields, w.guard()), events: w.page.first_name.events };

  w = world();                                                             // a sign-in box named email, then the form's
  const footer = w.add(Object.assign(new w.Input(), { type: "email" }), undefined, "email");
  const real = w.add(Object.assign(new w.Input(), { type: "email" }), undefined, "email");
  out.name_shared_by_two = { result: w.inPage([{ name: "email", kind: "text", value: "avery@example.invalid" }], w.guard()),
                             values: [footer.value, real.value] };

  // F6 and the second review: the file can't widen the destination, the fields or its own life
  w = world();
  const validate = (change) => w.context.validateHandoff(w.copy(change), T0);
  out.valid_handoff = validate({});
  out.unlisted_origin = validate({ destination: { origins: ["https://unlisted.example.org"], path: "/apply" } });
  out.mixed_origins = validate({ destination: { origins: ["https://job-boards.greenhouse.io", "https://evil.example"], path: "/synth/jobs/1" } });
  out.unreviewed_field = validate({ fields: [{ name: "unreviewed_field", label: "x", kind: "text", value: "v" }] });
  out.field_with_newline = validate({ fields: [{ name: "question_1\n", label: "x", kind: "text", value: "v" }] });
  out.odd_path = validate({ destination: { origins: ["https://job-boards.greenhouse.io"], path: "/synth/apply" } });
  out.unknown_adapter = validate({ adapter: "workday" });
  out.adapter_as_list = validate({ adapter: ["greenhouse"] });
  out.far_future = validate({ expires_at: "9999-12-31T23:59:59Z" });
  out.three_hours = validate({ expires_at: "2026-10-22T13:00:00.000Z" });
  out.zoneless = validate({ expires_at: "2026-10-22T11:00:00" });
  out.year_only = validate({ expires_at: "12345" });
  out.no_job = validate({ job: undefined });
  out.no_session = validate({ session_id: undefined });
  out.no_review = validate({ review: undefined });
  out.bad_attach = validate({ attach: [{ kind: "resume" }] });

  w = world();
  await w.load(w.copy({ expires_at: "9999-12-31T23:59:59Z" }));
  if (w.nodes.fill.onclick) await w.nodes.fill.onclick();
  out.far_future_click = { executions: w.state.executions, value: w.page.first_name.value };

  w = world();
  await w.load(w.copy({ job: undefined }));
  out.no_job_load = { handler: Boolean(w.nodes.fill.onclick), status: w.nodes.status.textContent };

  w = world();
  w.state.url = "https://unlisted.example.org/apply";
  await w.load(w.copy({ destination: { origins: ["https://unlisted.example.org"], path: "/apply" } }));
  if (w.nodes.fill.onclick) await w.nodes.fill.onclick();   // a refused file never gets a Fill handler
  out.forged_destination_click = { executions: w.state.executions, value: w.page.first_name.value };

  // the synthetic handoff tools/check_handoff.py writes for the installed-extension check, made at T0 by the test
  if (process.env.CAREEROS_CHECK_HANDOFF) {
    const check = JSON.parse(fs.readFileSync(process.env.CAREEROS_CHECK_HANDOFF, "utf8"));
    out.check_handoff = world().context.validateHandoff(check, T0);
  }

  // a handoff CareerOS itself wrote (answers file, package, session), loaded and filled through the popup on a page
  // that has every field the form asked about (independent Codex review, Pass 2 of 4787e2a)
  if (process.env.CAREEROS_E2E_HANDOFF) {
    const data = JSON.parse(fs.readFileSync(process.env.CAREEROS_E2E_HANDOFF, "utf8"));
    const e = world();
    e.state.now = Date.parse(process.env.CAREEROS_E2E_NOW);
    e.state.url = "https://job-boards.greenhouse.io" + data.destination.path;
    for (const name of (process.env.CAREEROS_E2E_PAGE || "").split(",").filter(Boolean)) {
      e.page[name] = e.add(Object.assign(new e.Input(), { type: "text" }), name, name);
    }
    await e.load(data);
    if (e.nodes.fill.onclick) await e.nodes.fill.onclick();
    out.e2e = { executions: e.state.executions, status: e.nodes.status.textContent, result: e.nodes.result.textContent,
                values: Object.fromEntries(Object.entries(e.page).map(([name, el]) => [name, el.value])) };
  }

  // each script's adapter table as JavaScript itself reads it (a repeated key keeps its last value)
  out.adapter_tables = {};
  for (const [name, source] of [["popup.js", SOURCES[1]], ["fill.js", SOURCES[0]]]) {
    const start = source.indexOf("{", source.indexOf("const ADAPTERS = "));
    let depth = 0;
    let end = start;
    for (; end < source.length; end += 1) {
      if (source[end] === "{") depth += 1;
      if (source[end] === "}" && --depth === 0) break;
    }
    const table = vm.runInNewContext("(" + source.slice(start, end + 1) + ")");
    out.adapter_tables[name] = Object.fromEntries(Object.entries(table).map(([k, a]) =>
      [k, { origins: a.origins, path: a.path.source, field: a.field.source }]));
  }

  // every chrome member a whole load-and-fill touches
  const full = world();
  await full.load();
  await full.nodes.fill.onclick();
  out.chrome_members = Array.from(new Set(full.state.chrome)).sort();
  out.chrome_expected = CHROME_CALLS.slice().sort();

  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify({ harness_error: String(e && e.stack || e) })); });
