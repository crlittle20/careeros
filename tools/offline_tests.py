"""Run the test suite with outbound network refused (Portfolio Prompt 10, 2026-10-13).

    .venv-dev\\Scripts\\python.exe tools\\offline_tests.py [unittest options]

Only loopback addresses may be resolved or connected to (the review page's 127.0.0.1 server). A test that needs the
internet, such as a live job board, fails here instead of passing or failing with the weather. The guard covers this
process; the few tests that start another program start local tools (git, release_scan.py), never a network client.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class NetworkRefused(OSError):
    """Raised instead of reaching anything but loopback."""


def is_loopback(host: object) -> bool:
    if host is None or host in ("", "localhost", b"localhost"):
        return True
    text = host.decode() if isinstance(host, bytes) else str(host)
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


LOOKUPS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex")
CONNECTS = ("connect", "connect_ex")


def install() -> Callable[[], None]:
    """Refuse every name lookup and connection that is not loopback. Returns a function that removes the guard."""
    real: dict[str, Any] = {name: getattr(socket, name) for name in LOOKUPS} | {name: getattr(socket.socket, name) for name in CONNECTS}

    def lookup(name: str) -> Callable[..., Any]:
        def guarded(host: object, *args: Any, **kwargs: Any) -> Any:
            if not is_loopback(host):
                raise NetworkRefused(f"offline tests: {name}({host!r}) refused; tests must not need the network")
            return real[name](host, *args, **kwargs)
        return guarded

    def connection(name: str) -> Callable[..., Any]:
        def guarded(self: socket.socket, address: Any) -> Any:
            if self.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback(address[0]):
                raise NetworkRefused(f"offline tests: {name} to {address[0]!r} refused; tests must not need the network")
            return real[name](self, address)
        return guarded

    for name in LOOKUPS:
        setattr(socket, name, lookup(name))
    for name in CONNECTS:
        setattr(socket.socket, name, connection(name))

    def remove() -> None:
        for name in LOOKUPS:
            setattr(socket, name, real[name])
        for name in CONNECTS:
            setattr(socket.socket, name, real[name])
    return remove


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT))
    install()
    program = unittest.main(module=None, exit=False,
                            argv=["offline_tests", "discover", "-s", str(ROOT / "tests"), "-t", str(ROOT), *(argv or [])])
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
