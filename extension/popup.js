// CareerOS Assist popup (ADR 0009). It reads the handoff file you choose, checks it, shows every value, and fills the
// page only when you press the button, and only on the posting the handoff names. Nothing is stored: the popup
// forgets everything when it closes. Text is always shown with textContent, never as HTML.
//
// What the extension trusts, and what it doesn't. The handoff is a file in your vault that CareerOS wrote; nothing
// signs it, so it is trusted only as far as your Windows account is. The extension does not trust it for where it may
// fill: the reviewed hosts, posting-path shape and field names of each adapter are fixed here (and again inside the
// page function, extension/fill.js), so a file can't grant itself another destination or field. It does not trust it
// for how long it may be used either: a handoff whose expiry is more than two hours and five minutes away (CareerOS
// writes exactly two; the five minutes allow for clock differences) is refused. It does trust the file's values and
// the posting it names (which must fit the reviewed posting-path shape), as CareerOS wrote them (second Claude review
// of the Codex corrections, 2026-10-22).
//
// Every check is repeated when you press Fill, on the file's contents as loaded (the tab may have moved, the file may
// have expired), and the page function checks the posting and the expiry once more before writing anything.
"use strict";

const ADAPTERS = {
  greenhouse: {
    origins: ["https://boards.greenhouse.io", "https://job-boards.greenhouse.io"],
    path: /^\/[A-Za-z0-9_-]{1,100}\/jobs\/[0-9]{1,20}$/,
    field: /^(?:first_name|last_name|preferred_name|email|phone|question_[0-9]{1,20})$/,
  },
};
const KINDS = ["text", "textarea", "select"];
const LIFETIME_MS = (2 * 60 + 5) * 60 * 1000;   // two hours, as CareerOS writes it, plus five minutes of clock skew
const STAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z$/;

function text(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 5000;
}

function validateHandoff(data, nowMs) {
  const errors = [];
  if (!data || data.careeros_handoff !== 1) errors.push("not a CareerOS handoff file");
  const adapter = data && typeof data.adapter === "string"
    && Object.prototype.hasOwnProperty.call(ADAPTERS, data.adapter) ? ADAPTERS[data.adapter] : null;
  if (!adapter) {
    errors.push("unknown form adapter");
    return errors;
  }
  if (!text(data.session_id) || !text(data.package_id) || !data.job || !text(data.job.title) || !text(data.job.employer)
      || !Array.isArray(data.review) || !data.review.every(text) || !Array.isArray(data.attach)
      || !data.attach.every((a) => a && text(a.kind) && text(a.path))) {
    errors.push("the file is missing what CareerOS writes (its session, job, review list or attachments)");
  }
  const expires = typeof data.expires_at === "string" && STAMP.test(data.expires_at) ? Date.parse(data.expires_at) : NaN;
  if (Number.isNaN(expires)) errors.push("this handoff's expiry isn't a time CareerOS writes (UTC, ending in Z)");
  else if (!(expires > nowMs)) errors.push("this handoff has expired: open a new session in CareerOS");
  else if (expires - nowMs > LIFETIME_MS) errors.push("this handoff claims a longer life than CareerOS gives");
  const dest = data.destination || {};
  if (!Array.isArray(dest.origins) || !dest.origins.length || !dest.origins.every((o) => adapter.origins.includes(o))) {
    errors.push("the destination isn't one of this adapter's reviewed sites");
  }
  if (typeof dest.path !== "string" || !adapter.path.test(dest.path)) errors.push("the destination isn't a posting address");
  const fields = data.fields;
  if (!Array.isArray(fields)) errors.push("fields must be a list");
  for (const f of Array.isArray(fields) ? fields : []) {
    if (!f || typeof f.name !== "string" || !adapter.field.test(f.name) || !KINDS.includes(f.kind)
        || typeof f.label !== "string" || typeof f.value !== "string" || f.value.length > 5000) {
      errors.push("a field in the file isn't one this adapter fills");
      break;
    }
  }
  return errors;
}

function onThePosting(data, url) {
  let page;
  try {
    page = new URL(url);
  } catch (e) {
    return false;
  }
  const path = data.destination.path;
  return data.destination.origins.includes(page.origin)
    && (page.pathname === path || page.pathname.startsWith(path + "/"));
}

function show(list, items) {
  list.replaceChildren();
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// Every check, at the moment it matters: the file, the expiry and the tab's current address.
async function checkNow(data, tabId) {
  const errors = validateHandoff(data, Date.now());
  const tab = await activeTab();
  if (!errors.length && !(tab && (tabId === undefined || tab.id === tabId) && onThePosting(data, tab.url))) {
    errors.push("this tab isn't the posting the handoff is for: open the application form for "
      + data.job.title + " at " + data.job.employer + " and try again");
  }
  return { errors, tab };
}

document.getElementById("handoff").addEventListener("change", async (event) => {
  const status = document.getElementById("status");
  const preview = document.getElementById("preview");
  const button = document.getElementById("fill");
  const result = document.getElementById("result");
  preview.hidden = true;
  button.disabled = true;
  button.onclick = null;
  status.className = "note";
  result.textContent = "";
  const file = event.target.files[0];
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (e) {
    status.className = "error";
    status.textContent = "This file isn't readable JSON.";
    return;
  }
  const first = await checkNow(data);
  if (first.errors.length) {
    status.className = "error";
    status.textContent = first.errors.join("; ");
    return;
  }
  const tabId = first.tab.id;
  document.getElementById("job").textContent = data.job.title + " at " + data.job.employer;
  show(document.getElementById("fields"), data.fields.map((f) => f.label + ": " + f.value));
  show(document.getElementById("review"), data.review.concat(data.attach.map((a) => "attach your " + a.kind.replace("_", " ")
    + ": " + a.path)));
  preview.hidden = false;
  button.disabled = data.fields.length === 0;
  status.textContent = "Check every value. Pressing the button fills them; nothing is submitted.";
  button.onclick = async () => {
    button.disabled = true;
    const now = await checkNow(data, tabId);
    if (now.errors.length) {
      result.className = "error";
      result.textContent = "Not filled: " + now.errors.join("; ") + ".";
      return;
    }
    // The page function takes its sites from its own table: only the adapter's name, the path and the expiry go in.
    const guard = { adapter: data.adapter, path: data.destination.path, expires_at: data.expires_at };
    const [run] = await chrome.scripting.executeScript({
      target: { tabId: now.tab.id },
      func: fillFields,
      args: [data.fields.map((f) => ({ name: f.name, kind: f.kind, value: f.value })), guard],
    });
    const out = run.result;
    if (out.refused) {
      result.className = "error";
      result.textContent = "Not filled: " + out.refused + ".";
      return;
    }
    result.className = out.missing.length || out.skipped.length ? "note" : "ok";
    result.textContent = "Filled " + out.filled.length + ". Already right: " + (out.already.join(", ") || "none")
      + ". Not found: " + (out.missing.join(", ") || "none")
      + ". Left for you: " + (out.skipped.join(", ") || "none")
      + ". Now review the whole form, attach your files, answer the rest, and submit it yourself."
      + " Then close the session in CareerOS.";
  };
});
