"""Probe candidate employers on the supported public job boards and report which ones exist and carry relevant roles.

    python tools/probe_boards.py SLUG [SLUG ...]

Read-only: one listing request per (board, slug), rate-limited. Prints board, slug, total postings, postings in a
target job family, and how many of those are US + remote/unknown (the ones that could pass the screen).
"""
from __future__ import annotations

import sys

from careeros.core.policy import load_policy
from careeros.discovery.boards import FETCHERS
from careeros.discovery.http import FetchError, HttpClient
from careeros.normalize.posting import classify_family, detect_country, detect_remote, excluded_term, norm
from careeros.paths import CONFIG_DIR, vault_paths


def main(slugs: list[str]) -> None:
    policy = load_policy(CONFIG_DIR / "policy" / "defaults.yaml", vault_paths()["overrides"])
    families, excluded = policy.get("families", default={}), policy.get("title_exclude", default=[])
    client = HttpClient(min_interval=0.6)
    try:
        for slug in slugs:
            for ats, fetch in FETCHERS.items():
                try:
                    postings = fetch(client, slug)
                except FetchError as exc:
                    if exc.code != "not_found":
                        print(f"{ats:10} {slug:24} error {exc.code}", flush=True)
                    continue
                relevant = [p for p in postings if classify_family(norm(p.title), families) and not excluded_term(norm(p.title), excluded)]
                usable = [p for p in relevant
                          if detect_country(p.location, p.description_text, r := detect_remote(p.location, p.workplace_hint, p.description_text)) != "non_US"
                          and r in ("remote", "unknown")]
                print(f"{ats:10} {slug:24} jobs={len(postings):4} relevant={len(relevant):3} us_remote={len(usable):3}  "
                      + "; ".join(p.title for p in usable[:4]), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main(sys.argv[1:])
