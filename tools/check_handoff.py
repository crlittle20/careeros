"""A synthetic handoff for the installed-extension check (docs/assisted_applications.md, "Before the first real form").

    .venv\\Scripts\\python.exe tools\\check_handoff.py https://job-boards.greenhouse.io/BOARD/jobs/ID
    .venv\\Scripts\\python.exe tools\\check_handoff.py URL --select question_123=Yes --text question_456=Synthetic

(from the engine folder; in a development checkout the environment is .venv-dev)

It writes careeros-test-handoff.json in your temporary folder (it prints where): synthetic values only, nothing from
your vault, bound to that posting's reviewed address and expiring in 30 minutes (--minutes N for a shorter life, to
see an expired file refused). Load it in the installed extension on that posting, press Fill, check each field it
reports, and close the tab without submitting. Then record the date in your vault overrides
(assistance: {greenhouse.io: {fill: supervised_only, installed_check: "YYYY-MM-DD"}}).

Added after the Codex review of ac83e00 asked for a supervised check of the installed extension before it fills a
real form (second Claude review, 2026-10-22).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careeros.review import adapters  # noqa: E402

VALUES = [("first_name", "First Name", "Synthetic"), ("last_name", "Last Name", "Check"),
          ("email", "Email", "synthetic.check@example.invalid")]
MINUTES = 30


def make(url: str, extra: list[tuple[str, str, str]], now: datetime, minutes: int = MINUTES) -> dict:
    """The handoff, in the shape CareerOS writes (careeros/review/adapters.py: handoff), with synthetic values."""
    if not 1 <= minutes <= 120:   # never more than the two hours CareerOS gives a handoff
        raise ValueError(f"the life must be 1 to 120 minutes: {minutes}")
    fields = [{"name": n, "label": label, "kind": "text", "value": v} for n, label, v in VALUES]
    for name, kind, value in extra:
        if not re.fullmatch(adapters.GREENHOUSE_FIELD, name):
            raise ValueError(f"not a field the Greenhouse adapter fills: {name!r}")
        if kind not in ("text", "textarea", "select"):
            raise ValueError(f"not a kind of field the extension fills: {kind!r}")
        if not value.strip() or len(value) > 5000:
            raise ValueError(f"a value must be 1 to 5000 characters: {name!r}")
        fields.append({"name": name, "label": name, "kind": kind, "value": value})
    expires = (now.astimezone(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"careeros_handoff": 1, "adapter": "greenhouse", "session_id": "ses:installed-check",
            "package_id": "pkg:installed-check", "manifest_sha256": "0" * 64, "expires_at": expires,
            "destination": adapters.destination("greenhouse", url),
            "job": {"title": "Installed-extension check", "employer": "synthetic values only"},
            "fields": fields, "review": ["close this tab without submitting: this is a check, not an application"],
            "attach": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a synthetic handoff for the installed-extension check")
    parser.add_argument("url", help="a public Greenhouse posting: https://job-boards.greenhouse.io/BOARD/jobs/ID")
    parser.add_argument("--select", action="append", default=[], metavar="NAME=CHOICE", help="a plain list to try")
    parser.add_argument("--text", action="append", default=[], metavar="NAME=VALUE", help="another text field to try")
    parser.add_argument("--textarea", action="append", default=[], metavar="NAME=VALUE", help="a text area to try")
    parser.add_argument("--minutes", type=int, default=MINUTES, help="how long the file lives (1 to 120)")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "careeros-test-handoff.json"))
    args = parser.parse_args(argv)
    try:
        extra = []
        for kind, items in (("select", args.select), ("text", args.text), ("textarea", args.textarea)):
            for item in items:
                if "=" not in item:
                    raise ValueError(f"write --{kind} NAME=VALUE: {item!r}")
                name, value = item.split("=", 1)
                extra.append((name, kind, value))
        data = make(args.url, extra, datetime.now(timezone.utc), args.minutes)
    except ValueError as exc:
        print(f"Not written: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}: {len(data['fields'])} synthetic values for {data['destination']['path']}, "
          f"expiring {data['expires_at']}. Never submit the form.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
