// CareerOS Assist: the only code that touches the employer's page (ADR 0009).
//
// fillFields(fields, guard) runs inside the page you opened, once, when you press "Fill these fields" in the popup.
// - First it checks, inside the page itself, that this is a posting address its own reviewed adapter covers and the
//   one the handoff names, and that the handoff hasn't expired (and was never given more than two hours, plus five
//   minutes for clock differences). If not, it writes nothing. The reviewed sites, path shape and field names are fixed here as well as in the popup and in
//   CareerOS; tests check the three copies are equal (Codex review of ac83e00 and the second Claude review, 2026-10-22).
// - It sets the value of the fields named in the handoff, and only fields its adapter fills: text inputs, text areas
//   and plain drop-down lists. A name that more than one element on the page answers to is left for you.
// - It never overwrites a value you typed or a choice already made (only an empty placeholder is replaced), picks a
//   choice by its position (never by a value two choices may share), never picks a disabled choice (or one in a
//   disabled group), leaves lists of several choices alone, never clicks
//   anything, never submits, never presses a key, never uploads, never reads anything but the fields it fills, and
//   never sends anything anywhere.
// - It returns what it filled, what was already right, what it couldn't find and what it skipped.
// Keep it self-contained: chrome.scripting.executeScript serialises the function into the page.
function fillFields(fields, guard) {
  const result = { filled: [], already: [], missing: [], skipped: [], refused: "" };
  const ADAPTERS = {
    greenhouse: {
      origins: ["https://boards.greenhouse.io", "https://job-boards.greenhouse.io"],
      path: /^\/[A-Za-z0-9_-]{1,100}\/jobs\/[0-9]{1,20}$/,
      field: /^(?:first_name|last_name|preferred_name|email|phone|question_[0-9]{1,20})$/,
    },
  };
  const LIFETIME_MS = (2 * 60 + 5) * 60 * 1000;   // two hours, as CareerOS writes it, plus five minutes of clock skew
  const STAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z$/;
  const TEXT_TYPES = ["text", "email", "tel", "url", ""];
  const PLACEHOLDER = /^\s*$|^\s*(?:select|choose|please)\b|^\s*[-\u2013\u2014.]+\s*$/i;

  const here = typeof location === "undefined" ? null : location;
  const adapter = guard && typeof guard.adapter === "string"
    && Object.prototype.hasOwnProperty.call(ADAPTERS, guard.adapter) ? ADAPTERS[guard.adapter] : null;
  if (!adapter || typeof guard.path !== "string" || !adapter.path.test(guard.path)) {
    result.refused = "no reviewed destination was given";
    return result;
  }
  if (!here || !adapter.origins.includes(here.origin)
      || !(here.pathname === guard.path || here.pathname.startsWith(guard.path + "/"))) {
    result.refused = "this page isn't the posting the handoff is for";
    return result;
  }
  const expires = typeof guard.expires_at === "string" && STAMP.test(guard.expires_at) ? Date.parse(guard.expires_at) : NaN;
  const now = Date.now();
  if (!(expires > now) || expires - now > LIFETIME_MS) {
    result.refused = "the handoff has expired or claims a longer life than CareerOS gives";
    return result;
  }

  function announce(el) {
    // Frameworks such as React listen for these, as they do when browser autofill fills a field.
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setValue(el, value) {
    // The native setter: frameworks watch it. This is how browser autofill fills such fields too.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    announce(el);
  }

  function choose(el, index) {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "selectedIndex").set.call(el, index);
    announce(el);
  }

  for (const field of fields) {
    if (!field || typeof field.name !== "string" || !adapter.field.test(field.name) || typeof field.value !== "string") {
      result.skipped.push(String(field && field.name) + " (not a field this adapter fills)");
      continue;
    }
    // The name is plain letters, digits and _, checked above, so it is safe inside the selector.
    const matches = document.querySelectorAll('[id="' + field.name + '"], [name="' + field.name + '"]');
    if (matches.length === 0) {
      result.missing.push(field.name);
      continue;
    }
    if (matches.length > 1) {
      result.skipped.push(field.name + " (" + matches.length + " fields on the page answer to this name)");
      continue;
    }
    const el = matches[0];
    if (field.kind === "select") {
      if (!(el instanceof HTMLSelectElement) || el.multiple) {
        result.skipped.push(field.name + " (not a plain list of one choice)");
        continue;
      }
      const options = Array.from(el.options).filter((o) => o.text.trim() === field.value);
      if (options.length !== 1) {
        result.skipped.push(field.name + (options.length ? " (more than one choice reads like this)" : " (no such choice)"));
        continue;
      }
      const option = options[0];
      if (el.selectedIndex === option.index) {
        result.already.push(field.name);
        continue;
      }
      // Whatever its position, the current choice is kept unless it is an empty placeholder ("Select...", "--",
      // blank or disabled, with no value): a first option such as "No" may be a real answer.
      const current = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      const placeholder = !current || (current.value === "" && (current.disabled || PLACEHOLDER.test(current.text)));
      if (!placeholder) {
        result.skipped.push(field.name + " (already chosen)");
        continue;
      }
      const group = option.parentNode;   // a choice inside a disabled group is disabled too
      if (option.disabled || (group && group.tagName === "OPTGROUP" && group.disabled)) {
        result.skipped.push(field.name + " (that choice is disabled)");
        continue;
      }
      choose(el, option.index);
      if (el.selectedIndex !== option.index) {
        result.skipped.push(field.name + " (the choice didn't take)");
        continue;
      }
    } else {
      const fits = (field.kind === "textarea" && el instanceof HTMLTextAreaElement)
        || (field.kind === "text" && el instanceof HTMLInputElement && TEXT_TYPES.includes(el.getAttribute("type") || ""));
      if (!fits) {
        result.skipped.push(field.name + " (different kind of field)");
        continue;
      }
      if (el.value === field.value) {
        result.already.push(field.name);
        continue;
      }
      if (el.value) {
        result.skipped.push(field.name + " (already filled)");
        continue;
      }
      setValue(el, field.value);
    }
    el.style.outline = "2px solid #1a7f37";
    result.filled.push(field.name);
  }
  return result;
}
