"""Safe reading of employer-controlled web pages for board verification (2026-10-13).

Board verification reads an employer's homepage and careers page to see which job board the employer itself links to.
Those addresses come from discovery (a lead, ChatGPT research, a person), so every fetch is a security boundary:

- only https, only port 443, no credentials in the URL, no IP-address hosts, no single-label / internal host names
  (localhost, *.local, *.internal, ...);
- every address a host name resolves to must be public: loopback, private (RFC 1918), link-local (incl. the
  169.254.169.254 cloud metadata address), carrier-grade NAT, multicast and reserved ranges are refused;
- the connection is pinned to the address that was checked (the TLS certificate is still verified against the host
  name), so a DNS answer cannot change between the check and the request (DNS rebinding);
- redirects are followed by hand, at most MAX_REDIRECTS, and every hop is checked like the first URL. A hop that
  leaves the employer's own domain is never fetched: it is returned as an OffsiteRedirect so the caller can record
  where the employer sends people (often the job board itself);
- robots.txt is honoured (RFC 9309: 4xx = allowed; 5xx or unreachable = do not fetch);
- responses are capped in size, must be HTML (or plain text for robots.txt), and are never executed: page content is
  data to be parsed, nothing else;
- no cookies are kept, proxy settings from the environment are ignored, and every request counts against a budget.

Failures are classified (SiteFetchError.failure) so a timeout is never mistaken for an identity problem.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from careeros.core import private_sites

from .http import FetchError, RequestBudget

USER_AGENT = "CareerOS-board-verifier/1.0 (personal job search; checks a careers page about once a month)"
ROBOTS_AGENT = "CareerOS"
MAX_REDIRECTS = 5
MAX_BYTES = 1_500_000
HTML_TYPES = ("text/html", "application/xhtml+xml")
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".home.arpa", ".test",
                     ".invalid", ".example", ".onion", ".localdomain")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Never an employer's own site, and never read automatically: job sites and networks, and application systems /
# job boards (they are reached through their own rules). Sites tied to the job seeker's own situation are added
# from the vault (core/private_sites.py).
NEVER_FETCH = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com", "dice.com", "google.com",
               "facebook.com", "smartrecruiters.com", "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "icims.com", "taleo.net",
               "successfactors.com", "ultipro.com", "ukg.net", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")
# Failures that are security problems: never retried as if transient, and never bypassed by a manual override.
SECURITY_FAILURES = {"unsafe_url", "unsafe_redirect", "private_network", "unsupported_protocol", "malformed_url"}
TRANSIENT_FAILURES = {"dns_failure", "timeout", "network", "rate_limited", "http_error", "robots_unavailable"}


class SiteFetchError(Exception):
    def __init__(self, failure: str, detail: str = "", url: str | None = None):
        super().__init__(f"{failure}: {detail}" if detail else failure)
        self.failure, self.detail, self.url = failure, detail, url


@dataclass
class Page:
    url: str                     # the URL asked for
    final_url: str               # after same-site redirects
    status: int
    content_type: str
    text: str
    redirects: list = field(default_factory=list)
    content_hash: str = ""


@dataclass
class OffsiteRedirect:
    url: str                     # the URL asked for
    destination: str             # where the employer's site sent us (not fetched)
    redirects: list = field(default_factory=list)


def https_url(url: str) -> str:
    """http -> https upgrade for a page on an employer site; anything else unchanged."""
    parts = urlsplit((url or "").strip())
    if parts.scheme == "http":
        netloc = parts.netloc[:-3] if parts.netloc.endswith(":80") else parts.netloc
        return urlunsplit(("https", netloc, parts.path, parts.query, ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def check_url(url: str) -> str:
    """The URL's host name, or SiteFetchError. Checks the URL itself; vet_addresses checks where it resolves."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        raise SiteFetchError("malformed_url", url=url) from None
    if parts.scheme != "https":
        raise SiteFetchError("unsupported_protocol", parts.scheme or "none", url)
    if parts.username or parts.password:
        raise SiteFetchError("unsafe_url", "credentials in URL", url)
    if port not in (None, 443):
        raise SiteFetchError("unsafe_url", f"port {port}", url)
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise SiteFetchError("malformed_url", "no host", url)
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise SiteFetchError("unsafe_url", "IP address host", url)
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise SiteFetchError("malformed_url", "host name", url) from None
    if (ascii_host in _BLOCKED_HOSTS or ascii_host.endswith(_BLOCKED_SUFFIXES) or "." not in ascii_host
            or len(ascii_host) > 253 or not all(_LABEL.match(label) for label in ascii_host.split("."))):
        raise SiteFetchError("unsafe_url", f"host {ascii_host}", url)
    return ascii_host


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                                 or ip.is_unspecified)


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SiteFetchError("dns_failure", str(exc.errno), host) from None
    return sorted({info[4][0] for info in infos})


def vet_addresses(host: str, resolver=system_resolver) -> list[str]:
    """Every address the host resolves to, all public; SiteFetchError otherwise."""
    addresses = resolver(host)
    if not addresses:
        raise SiteFetchError("dns_failure", "no address", host)
    for address in addresses:
        try:
            public = _public(address)
        except ValueError:
            raise SiteFetchError("private_network", f"unparseable address {address}", host) from None
        if not public:
            raise SiteFetchError("private_network", f"{host} -> {address}", host)
    return addresses


class SiteFetcher:
    """Reads pages on one employer's own domain (and its subdomains). Not a crawler: the caller asks for specific
    pages, at most `max_pages` per employer, each counted against `budget` (a RequestBudget with provider 'site')."""

    def __init__(self, domain: str, *, budget: RequestBudget | None = None, max_pages: int = 8, max_bytes: int = MAX_BYTES,
                 timeout: float = 10.0, min_interval: float = 1.0, transport: httpx.BaseTransport | None = None,
                 resolver=None, sleep=time.sleep, pin: bool | None = None):
        self.domain = check_url(f"https://{domain}/")
        if any(self.domain == d or self.domain.endswith("." + d) for d in NEVER_FETCH + private_sites.never_fetch()):
            raise SiteFetchError("blocked_site", self.domain, f"https://{self.domain}/")
        self.budget, self.max_pages, self.max_bytes = budget, max_pages, max_bytes
        if transport is None:  # Demo Mode: synthetic employer sites, synthetic name resolution, never the real network
            from careeros.demo import sandbox
            transport, resolver = sandbox.transport("site"), resolver or sandbox.resolver()
            if transport is not None:
                min_interval = 0.0  # no real site to be polite to
        self.resolver = resolver or system_resolver
        self.pin = (transport is None) if pin is None else pin
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False,
                                   timeout=httpx.Timeout(timeout, connect=5.0),
                                   headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
        self.min_interval, self.sleep, self._last = min_interval, sleep, 0.0
        self.requests = 0
        self._robots: dict[str, RobotFileParser | None] = {}

    def close(self) -> None:
        self.client.close()

    def in_scope(self, host: str) -> bool:
        return host == self.domain or host.endswith("." + self.domain)

    # --- one request, pinned to a vetted address ------------------------------------------------------------
    def _send(self, url: str, host: str, accept: tuple[str, ...]) -> tuple[int, dict, bytes, str]:
        if self.requests >= self.max_pages:
            raise SiteFetchError("budget_exhausted", "page limit for this employer", url)
        if self.budget is not None:
            try:
                self.budget.take("site")
            except FetchError:
                raise SiteFetchError("budget_exhausted", "daily site budget", url) from None
        addresses = vet_addresses(host, self.resolver)
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        target, headers, extensions = url, {}, {}
        if self.pin:  # connect to the checked address; TLS still verifies the certificate for the host name
            address = addresses[0]
            parts = urlsplit(url)
            netloc = f"[{address}]" if ":" in address else address
            target = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
            headers, extensions = {"Host": host}, {"sni_hostname": host}
        try:
            with self.client.stream("GET", target, headers=headers, extensions=extensions) as response:
                status, response_headers = response.status_code, dict(response.headers)
                body = b""
                if status < 300:
                    ctype = response_headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(accept):
                        raise SiteFetchError("bad_content_type", ctype, url)
                    declared = response_headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise SiteFetchError("too_large", declared, url)
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > self.max_bytes:
                            raise SiteFetchError("too_large", f">{self.max_bytes}", url)
                charset = response.charset_encoding or "utf-8"
        except SiteFetchError:
            raise
        except httpx.TimeoutException:
            raise SiteFetchError("timeout", "", url) from None
        except httpx.HTTPError as exc:
            raise SiteFetchError("network", type(exc).__name__, url) from None
        finally:
            self.client.cookies.clear()
        return status, response_headers, body, charset

    def _get(self, url: str, accept: tuple[str, ...], *, robots: bool) -> Page | OffsiteRedirect:
        start, chain = https_url(url), []
        current = start
        for hop in range(MAX_REDIRECTS + 1):
            try:
                host = check_url(current)
            except SiteFetchError as exc:
                if hop:
                    raise SiteFetchError("unsafe_redirect", f"{exc.failure}: {current}", start) from None
                raise
            if not self.in_scope(host):
                if hop:
                    return OffsiteRedirect(start, current, chain)
                raise SiteFetchError("out_of_scope", host, current)
            try:
                if robots and not self._robots_allow(current, host):
                    raise SiteFetchError("robots_disallowed", urlsplit(current).path or "/", current)
                status, headers, body, charset = self._send(current, host, accept)
            except SiteFetchError as exc:
                if hop and exc.failure == "private_network":  # a redirect that leads into a private network
                    raise SiteFetchError("unsafe_redirect", f"private_network: {current}", start) from None
                raise
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise SiteFetchError("http_error", f"{status} without Location", current)
                nxt = urljoin(current, location.strip())
                parts = urlsplit(nxt)
                if parts.scheme == "http" and self.in_scope((parts.hostname or "").lower()):
                    nxt = https_url(nxt)  # an employer page upgraded to https; anything else is checked as it is
                chain.append(nxt)
                current = nxt
                continue
            if status in (401, 403):
                raise SiteFetchError("auth_required", str(status), current)
            if status in (404, 410):
                raise SiteFetchError("not_found", str(status), current)
            if status == 429:
                raise SiteFetchError("rate_limited", "429", current)
            if status >= 400 or status < 200:
                raise SiteFetchError("http_error", str(status), current)
            text = body.decode(charset, errors="replace")
            return Page(start, current, status, headers.get("content-type", ""), text, chain,
                        hashlib.sha256(body).hexdigest()[:32])
        raise SiteFetchError("too_many_redirects", str(MAX_REDIRECTS), start)

    def get(self, url: str) -> Page | OffsiteRedirect:
        return self._get(url, HTML_TYPES, robots=True)

    # --- robots.txt -------------------------------------------------------------------------------------------
    def _robots_allow(self, url: str, host: str) -> bool:
        if host not in self._robots:
            parser = RobotFileParser()
            try:
                result = self._get(f"https://{host}/robots.txt", ("text/plain", "text/html"), robots=False)
                lines = result.text.splitlines() if isinstance(result, Page) else []
                parser.parse(lines)
            except SiteFetchError as exc:
                if exc.failure in SECURITY_FAILURES or exc.failure in ("dns_failure", "budget_exhausted", "out_of_scope"):
                    raise
                if exc.failure in ("not_found", "auth_required", "bad_content_type", "too_large") or (
                        exc.failure == "http_error" and exc.detail.startswith("4")):
                    parser.parse([])  # RFC 9309: an unavailable robots.txt (4xx) allows everything
                else:  # 5xx, timeout, network: do not fetch
                    raise SiteFetchError("robots_unavailable", exc.failure, url) from None
            self._robots[host] = parser
        return self._robots[host].can_fetch(ROBOTS_AGENT, url)
