"""Polite HTTP for public job-board APIs.

Retry policy (GrowthOS retryPlan pattern): 429 waits for Retry-After (capped) once; 5xx and timeouts retry once;
anything else fails with a coded error. Reads only, so a retry can never duplicate an action.

Discovery (2026-10-12) adds two optional guards:
- `allowed_hosts`: every request, including each redirect hop, must go to one of these hosts (exact match);
- `budget`: a RequestBudget that counts every request actually sent (retries and redirects included) against a global
  cap and a per-provider cap, and refuses to send once either is spent.
A Retry-After longer than MAX_RETRY_AFTER is never slept through: the request fails with `rate_limited` and the
wait is reported, so the caller can schedule the retry for later.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

USER_AGENT = "CareerOS/0.1 (personal job search; low volume)"
MAX_RETRY_AFTER = 60
# Hosts of the four allowed public listing APIs and their provider names (for per-provider budgets).
PROVIDER_HOSTS = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}


class FetchError(Exception):
    def __init__(self, code: str, status: int | None = None, retryable: bool = False, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.status, self.retryable, self.retry_after = code, status, retryable, retry_after


class RequestBudget:
    """Caps on requests actually sent: `total` overall and `per_provider[name]` for each provider. Spent counters
    are public so callers can record the request cost of each decision."""

    def __init__(self, total: int, per_provider: dict | None = None):
        self.total, self.per_provider = int(total), {k: int(v) for k, v in (per_provider or {}).items()}
        self.spent, self.spent_by = 0, {}

    def remaining(self, provider: str | None = None) -> int:
        left = self.total - self.spent
        if provider and provider in self.per_provider:
            left = min(left, self.per_provider[provider] - self.spent_by.get(provider, 0))
        return max(0, left)

    def take(self, provider: str | None) -> None:
        if self.remaining(provider) <= 0:
            raise FetchError("budget_exhausted")
        self.spent += 1
        if provider:
            self.spent_by[provider] = self.spent_by.get(provider, 0) + 1


class LimitedBudget:
    """A slice of a parent budget: at most `limit` requests, each also counted against the parent."""

    def __init__(self, parent: RequestBudget, limit: int):
        self.parent, self.limit, self.spent = parent, max(0, int(limit)), 0

    @property
    def total(self) -> int:
        return self.parent.total

    def remaining(self, provider: str | None = None) -> int:
        return min(self.limit - self.spent, self.parent.remaining(provider))

    def take(self, provider: str | None) -> None:
        if self.spent >= self.limit:
            raise FetchError("budget_exhausted")
        self.parent.take(provider)
        self.spent += 1


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


class HttpClient:
    def __init__(self, timeout: float = 20.0, min_interval: float = 0.5, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, allowed_hosts: set | None = None, budget: RequestBudget | None = None):
        self.allowed_hosts, self.budget, self.requests = (set(allowed_hosts) if allowed_hosts else None), budget, 0
        if transport is None:  # Demo Mode answers from its synthetic world and never reaches the real network
            from careeros.demo.sandbox import transport as demo_transport
            transport = demo_transport("ats")
            if transport is not None:
                min_interval = 0.0  # politeness pauses protect real servers; the synthetic world needs none
        # trust_env=False: proxy variables and .netrc in the environment can never redirect or authenticate these requests
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport, trust_env=False,
                                   follow_redirects=True, event_hooks={"request": [self._guard]})
        self.min_interval, self.sleep, self._last = min_interval, sleep, 0.0

    def _guard(self, request: httpx.Request) -> None:
        """Runs for every request httpx sends, including each redirect hop."""
        host = request.url.host.lower()
        if request.url.scheme != "https":
            raise FetchError("https_required")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise FetchError(f"host_not_allowed:{host}")
        if self.budget is not None:
            self.budget.take(PROVIDER_HOSTS.get(host))
        self.requests += 1

    def close(self) -> None:
        self.client.close()

    def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str) -> httpx.Response:
        if not url.startswith("https://"):
            raise FetchError("https_required")
        for attempt in (1, 2):
            self._pace()
            try:
                response = self.client.get(url)
            except FetchError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 1:
                    self.sleep(2)
                    continue
                raise FetchError(f"network:{type(exc).__name__}", retryable=True) from None
            if response.status_code == 429:
                delay = _retry_after_seconds(response.headers.get("retry-after"))
                if delay is not None and delay > MAX_RETRY_AFTER:
                    raise FetchError("rate_limited", 429, True, retry_after=delay)  # never sleep through a long wait
                if attempt == 1:
                    self.sleep(min(MAX_RETRY_AFTER, max(1, delay if delay is not None else 5)))
                    continue
                raise FetchError("http_429", 429, True, retry_after=delay)
            if response.status_code >= 500 and attempt == 1:
                self.sleep(2)
                continue
            if response.status_code == 404:
                raise FetchError("not_found", 404)
            if response.status_code in (401, 403):
                raise FetchError("access_restricted", response.status_code)
            if response.status_code >= 400:
                raise FetchError(f"http_{response.status_code}", response.status_code, response.status_code >= 500)
            return response
        raise FetchError("retries_exhausted", retryable=True)

    def get_json(self, url: str):
        response = self.get(url)
        try:
            return response.json()
        except ValueError:
            raise FetchError("invalid_json", response.status_code) from None
