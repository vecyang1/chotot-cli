# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""HTTP transport for the Chợ Tốt gateway.

Deliberately built on ``urllib`` from the standard library: the CLI has no
required third-party dependency, so ``pip install chotot-cli`` cannot fail on a
transitive resolution, and the tool runs on a bare Python.

Three decisions worth stating, because each is a place this class of client
usually goes wrong:

* **Bytes are decoded explicitly.** The gateway does not always declare a
  charset. Letting a library guess (``requests`` guesses ISO-8859-1 for
  ``text/*``) welds a BOM onto the first JSON key, which makes exactly one field
  silently unreadable while its neighbours look fine.
* **A rate limit is read, not probed.** When the server states ``Retry-After``
  we wait that long; we never re-run a request to discover a duration the
  response already contained.
* **The residential proxy is a reaction, not a default.** With ``auto_proxy``
  the first request is always direct; the proxy is resolved and switched in
  only after a block (HTTP 403/429 by default, or a connection failure), once
  per transport, announced on stderr, and it stays for the rest of the run.
  The 2.1.0 transport resolved it in the constructor, so every request was
  paid from the first and the fallback branch could never execute.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
import ssl
from typing import Any, Callable, Dict, FrozenSet, Optional

from chotot.errors import (
    NotFoundError, RateLimitedError, TransportError, UpstreamContractError, UsageError,
)
from chotot.proxy import (
    DEFAULT_GEO, ENV_AUTO_PROXY, ENV_PROXY, ENV_RESOLVER, ProxyPlan, Resolver,
    mask_proxy, resolve_proxy, resolve_residential, validate_proxy_url,
)

logger = logging.getLogger("chotot.http")

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_MIN_INTERVAL = 0.2
MAX_BACKOFF = 30.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

#: Statuses worth another attempt. 404 is deliberately absent: a missing listing
#: is an answer, not a transient fault, and retrying it wastes the user's time.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses that mean "this address is blocked" and so justify switching to the
#: residential proxy when the fallback is armed. 403 is here although it is not
#: retryable on its own: an anti-bot block IS the signal the fallback exists for.
PROXY_FALLBACK_STATUSES: FrozenSet[int] = frozenset({403, 429})
#: Comma-separated override, e.g. ``403,429,503``.
ENV_FALLBACK_STATUSES = "CHOTOT_PROXY_FALLBACK_STATUSES"
#: A connection failure or timeout on the direct path also triggers the switch:
#: a DNS-level block looks exactly like that from the client's side.
FALLBACK_ON_CONNECT_ERROR = True

_DEFAULT_SSL_CONTEXT: Optional[ssl.SSLContext] = None


def get_default_ssl_context() -> ssl.SSLContext:
    """Return a default SSLContext with certifi fallback for barren interpreters."""
    global _DEFAULT_SSL_CONTEXT
    if _DEFAULT_SSL_CONTEXT is None:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            # An empty trust store is not a warning sign, it is a certainty:
            # every HTTPS request will fail verification. certifi carries a
            # store, and is optional precisely because this path does not fire
            # on a normally-installed Python (193 CAs on the reference machine,
            # in the system interpreter and in a dependency-free venv alike).
            #
            # `except Exception: pass` used to swallow the absence, so the only
            # symptom was a bare SSL error on every subsequent request with
            # nothing naming the cause. The remedy has to be stated where the
            # cause is known.
            try:
                import certifi
            except ImportError:
                logger.warning(
                    "This interpreter has an empty CA trust store and certifi is "
                    "not installed, so HTTPS verification will fail. Install your "
                    "Python's certificates (on macOS: 'Install Certificates.command' "
                    "in the Python folder), or 'pip install certifi'."
                )
            else:
                ctx = ssl.create_default_context(cafile=certifi.where())
                logger.debug("empty system trust store; using certifi at %s", certifi.where())
        _DEFAULT_SSL_CONTEXT = ctx
    return _DEFAULT_SSL_CONTEXT


def fallback_statuses_from_env() -> FrozenSet[int]:
    """``CHOTOT_PROXY_FALLBACK_STATUSES`` parsed, or the default set."""
    raw = os.getenv(ENV_FALLBACK_STATUSES, "").strip()
    if not raw:
        return PROXY_FALLBACK_STATUSES
    statuses = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit() or not 100 <= int(token) <= 599:
            raise UsageError(
                f"{ENV_FALLBACK_STATUSES} contains {token!r}, which is not an HTTP status.",
                remedy="Use a comma-separated list of statuses, e.g. 403,429.",
            )
        statuses.add(int(token))
    return frozenset(statuses)


class _UnconditionalProxyHandler(urllib.request.ProxyHandler):
    """A ``ProxyHandler`` that never bypasses the proxy it was given.

    The stock handler consults ``proxy_bypass()`` before every request: the
    ``no_proxy`` variable, and on macOS the System Settings exclusion list,
    which on the reference machine bypasses loopback. A proxy this tool chose
    explicitly -- a flag, ``CHOTOT_PROXY``, or the residential fallback -- must
    not be silently skipped by a host setting invisible from here, and the
    end-to-end suite's servers on 127.0.0.1 would be bypassed on any such Mac
    while the run still reported the switch. This is the stock ``proxy_open``
    with that one check removed.
    """

    def proxy_open(self, req: urllib.request.Request, proxy: str, type: str) -> Any:
        orig_type = req.type
        proxy_type, user, password, hostport = urllib.request._parse_proxy(proxy)
        if proxy_type is None:
            proxy_type = orig_type
        if user and password:
            user_pass = f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}"
            credentials = base64.b64encode(user_pass.encode()).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + credentials)
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if orig_type == proxy_type or orig_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


def _make_opener(proxy_url: Optional[str] = None) -> Callable[..., Any]:
    """Build the opener for a proxy URL, or a DIRECT one for None.

    Direct means direct: ``urlopen`` on its own would still honour
    ``HTTPS_PROXY`` from the environment, so ``CHOTOT_PROXY=none`` could not
    have escaped a global proxy. The environment is consulted exactly once,
    in :mod:`chotot.proxy`, and its verdict is what arrives here.
    """
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    opener = urllib.request.build_opener(
        _UnconditionalProxyHandler(proxies),
        urllib.request.HTTPSHandler(context=get_default_ssl_context()),
    )
    return lambda req, timeout=DEFAULT_TIMEOUT: opener.open(req, timeout=timeout)


class HttpTransport:
    """A small, polite JSON client with a residential-proxy fallback.

    Args:
        base_url: Gateway root, without a trailing slash.
        timeout: Per-request timeout in seconds.
        max_retries: Total attempts for a retryable failure (1 = no retry).
        min_interval: Minimum spacing between requests, applied automatically.
        user_agent: Sent verbatim; the gateway serves anonymous traffic.
        proxy: ``--proxy`` as typed: a URL, ``auto``, ``none``, or None.
        auto_proxy: Arm the fallback: direct first, residential proxy after a
            block. Also armed by ``CHOTOT_AUTO_PROXY=1``.
        geo: Exit country for the resolver; applies to ``auto`` and to the
            fallback only. None means the default (``vn``).
        opener: Injection point for tests. Must accept ``(request, timeout)``.
            When given, no proxy is resolved at construction.
        sleep: Injection point for tests, so backoff is asserted, not waited on.
        resolver: Injection point: ``geo -> proxy URL or None``. Defaults to
            the resolver command owned by :mod:`chotot.proxy`.
        opener_factory: Injection point: ``proxy URL or None -> opener``. The
            fallback rebuilds the opener through it, so a test can observe
            which proxy the switch chose.
        fallback_statuses: Override the trigger set (else the environment,
            else ``PROXY_FALLBACK_STATUSES``).
        fallback_on_connect_error: Whether a connection failure also triggers
            the switch.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: Optional[str] = None,
        auto_proxy: bool = False,
        geo: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        resolver: Optional[Resolver] = None,
        opener_factory: Optional[Callable[[Optional[str]], Callable[..., Any]]] = None,
        fallback_statuses: Optional[FrozenSet[int]] = None,
        fallback_on_connect_error: bool = FALLBACK_ON_CONNECT_ERROR,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.min_interval = max(0.0, min_interval)
        self.user_agent = user_agent
        # One owner for the "is the fallback armed" decision: the transport.
        self.auto_proxy = bool(auto_proxy) or (
            os.getenv(ENV_AUTO_PROXY, "").strip().lower() in ("1", "true", "yes", "on")
        )
        self.geo = geo or DEFAULT_GEO
        self._resolver: Resolver = resolver or resolve_residential
        self._opener_factory = opener_factory or _make_opener
        self.fallback_statuses = (
            fallback_statuses if fallback_statuses is not None else fallback_statuses_from_env()
        )
        self.fallback_on_connect_error = fallback_on_connect_error

        plan = (ProxyPlan(None, "injected") if opener is not None
                else resolve_proxy(proxy, geo=self.geo, resolver=self._resolver))
        if geo and plan.source == "flag":
            logger.warning(
                "--geo %s applies to '--proxy auto' and the fallback only; the explicit "
                "proxy %s is used exactly as given", geo, mask_proxy(plan.url),
            )
        self._proxy_url: Optional[str] = plan.url
        self.proxy_source = plan.source
        self._opener = opener or self._opener_factory(self._proxy_url)
        self._sleep = sleep or time.sleep
        self._last_request_at = 0.0

        #: The fallback is resolved at most once per transport; a resolver that
        #: answered "nothing" is not asked again on the next attempt.
        self._fallback_attempted = False
        #: Request number after which the switch happened, or None.
        self.fallback_fired_at: Optional[int] = None
        #: Requests actually issued, so callers can report cost honestly.
        self.request_count = 0
        #: ...and how many of them went through a proxy, which is what costs.
        self.proxied_request_count = 0

    # -- state -------------------------------------------------------------

    @property
    def proxy_url(self) -> Optional[str]:
        """The active proxy URL (credential included -- never print this)."""
        return self._proxy_url

    @property
    def proxy_masked(self) -> str:
        """The active proxy, safe for display."""
        return mask_proxy(self._proxy_url)

    @property
    def is_proxied(self) -> bool:
        return bool(self._proxy_url)

    @property
    def transport_mode(self) -> str:
        if not self._proxy_url:
            return "direct"
        return "proxy (after fallback)" if self.fallback_fired_at is not None else "proxy"

    def transport_summary(self) -> Dict[str, Any]:
        """Credential-free telemetry for ``doctor`` and JSON reports."""
        return {
            "mode": self.transport_mode,
            "proxy": self.proxy_masked,
            "source": self.proxy_source,
            "fallback_fired_at": self.fallback_fired_at,
            "requests": self.request_count,
            "proxied_requests": self.proxied_request_count,
        }

    # -- internals ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Origin": "https://www.chotot.com",
            "Referer": "https://www.chotot.com/",
        }

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _decode(raw: bytes, encoding_header: Optional[str]) -> str:
        """Decode a response body without letting anything guess the charset."""
        if encoding_header:
            header = encoding_header.lower()
            if "gzip" in header:
                raw = gzip.decompress(raw)
            elif "deflate" in header:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        # utf-8-sig strips a BOM if present; the gateway does not send one today
        # but a BOM would corrupt the first key rather than raise, so we absorb
        # it here instead of discovering it as one permanently empty field.
        return raw.decode("utf-8-sig")

    @staticmethod
    def _retry_after_seconds(headers: Any) -> Optional[float]:
        """Honour the server's own statement of how long to wait."""
        if headers is None:
            return None
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            # The HTTP-date form is legal but rare here; treat it as unknown
            # rather than inventing a number.
            return None

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_BACKOFF)
        # Full jitter: bounded exponential with a random factor, so concurrent
        # callers do not resynchronise onto the same retry instant.
        ceiling = min(DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)), MAX_BACKOFF)
        return random.uniform(0.0, ceiling)

    def _maybe_fall_back(self, status: Optional[int], reason: str) -> bool:
        """Switch to the residential proxy if this failure qualifies.

        Returns True only when the switch actually happened. Resolution is
        attempted once per transport: a resolver that had nothing is not
        re-run on every retry, and the warning is printed once.
        """
        if not self.auto_proxy or self._proxy_url or self._fallback_attempted:
            return False
        if status is None:
            if not self.fallback_on_connect_error:
                return False
        elif status not in self.fallback_statuses:
            return False

        self._fallback_attempted = True
        url = self._resolver(self.geo)
        if not url:
            logger.warning(
                "%s on the direct connection, but could not resolve a residential proxy "
                "(geo=%s); continuing direct. Set %s to a resolver command, or %s to a "
                "proxy URL.", reason, self.geo, ENV_RESOLVER, ENV_PROXY,
            )
            return False
        url = validate_proxy_url(url, origin="the proxy resolver")
        self._proxy_url = url
        self.proxy_source = "resolver"
        self._opener = self._opener_factory(url)
        self.fallback_fired_at = self.request_count
        logger.warning(
            "%s on the direct connection; switching to the residential proxy %s (geo=%s) "
            "for the rest of this run", reason, mask_proxy(url), self.geo,
        )
        return True

    def _blocked_remedy(self, status: int) -> str:
        if status == 403 and self._proxy_url:
            return ("HTTP 403 through the proxy as well: that exit is blocked too. Try "
                    "another --geo, or another --proxy.")
        if status == 403 and self.auto_proxy:
            return ("HTTP 403 is usually an anti-bot block of this address, and no residential "
                    f"proxy could be resolved to fall back to. Set {ENV_RESOLVER}, or pass an "
                    "explicit --proxy http://...")
        if status == 403:
            return ("HTTP 403 is usually an anti-bot block of this address. Re-run with "
                    "--auto-proxy (direct first, residential proxy after a block) or "
                    "--proxy auto, or check with 'chotot doctor --auto-proxy'.")
        return "This status is not retryable. Verify the query parameters with 'chotot doctor'."

    # -- public ------------------------------------------------------------

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET ``path`` and parse JSON.

        Raises:
            NotFoundError: upstream answered 404.
            RateLimitedError: upstream answered 429 and retries were exhausted.
            TransportError: network failure or a non-retryable HTTP status.
            UpstreamContractError: the body was not a JSON object.
        """
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None},
            doseq=True,
        )
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")

        last_error: Optional[str] = None
        last_status: Optional[int] = None
        last_retry_after: Optional[float] = None

        attempt = 0
        budget = self.max_retries
        while attempt < budget:
            attempt += 1
            switched = False
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers())
            try:
                self.request_count += 1
                if self._proxy_url:
                    self.proxied_request_count += 1
                # --verbose promised "log gateway activity" and logged nothing:
                # the only record in this module was the retry warning, so a
                # successful run -- every run a user would debug -- was silent.
                # The gateway needs no credential, so the URL carries no secret.
                logger.debug("GET %s (attempt %d/%d, %s)", url, attempt, budget, self.transport_mode)
                with self._opener(request, timeout=self.timeout) as response:
                    body = self._decode(response.read(), response.headers.get("Content-Encoding"))
                    payload = json.loads(body)
                    logger.debug(
                        "  <- %s keys=%s total=%r ads=%d",
                        getattr(response, "status", "200"),
                        ",".join(sorted(payload)[:6]) if isinstance(payload, dict) else "-",
                        payload.get("total") if isinstance(payload, dict) else None,
                        len(payload.get("ads") or []) if isinstance(payload, dict) else 0,
                    )
                    if not isinstance(payload, dict):
                        raise UpstreamContractError(
                            f"Expected a JSON object from /{path.lstrip('/')}, "
                            f"got {type(payload).__name__}.",
                            remedy="The gateway response shape changed; re-run "
                                   "'chotot doctor' to re-measure the contract.",
                        )
                    return payload

            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_retry_after = self._retry_after_seconds(getattr(exc, "headers", None))
                if exc.code == 404:
                    raise NotFoundError(
                        f"Not found: /{path.lstrip('/')}",
                        remedy="Check the id. Listings are removed when sold or expired.",
                    ) from exc
                switched = self._maybe_fall_back(exc.code, f"HTTP {exc.code}")
                if exc.code not in RETRYABLE_STATUSES and not switched:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8-sig", "replace")[:200]
                    except Exception:  # noqa: BLE001 - the status is the useful part
                        pass
                    # 400/414 are caused by what was asked for, not by the
                    # network. Reporting them as a transport failure sent the
                    # user to check their connection over a bad query.
                    if exc.code in (400, 413, 414, 422):
                        raise UsageError(
                            f"Chợ Tốt rejected the request (HTTP {exc.code})"
                            + (f": {detail}" if detail else "."),
                            remedy="A parameter is out of range or malformed - "
                                   "commonly a query that is empty or extremely "
                                   "long, or a page beyond the search window.",
                        ) from exc
                    raise TransportError(
                        f"Chợ Tốt gateway returned HTTP {exc.code} for /{path.lstrip('/')}"
                        + (f": {detail}" if detail else ""),
                        remedy=self._blocked_remedy(exc.code),
                    ) from exc
                last_error = f"HTTP {exc.code}"

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                switched = self._maybe_fall_back(None, last_error)

            except json.JSONDecodeError as exc:
                raise UpstreamContractError(
                    f"Chợ Tốt gateway returned a non-JSON body for /{path.lstrip('/')}: {exc}",
                    remedy="The endpoint may be behind a captive portal or a "
                           "proxy interception page.",
                ) from exc

            if switched and attempt == budget:
                # A switch on the final attempt deserves one proxied try, or
                # `--retries 1 --auto-proxy` resolves a proxy and never uses it.
                budget += 1
            if attempt < budget:
                delay = self._backoff(attempt, last_retry_after)
                logger.warning(
                    "attempt %d/%d failed (%s); retrying in %.2fs",
                    attempt, budget, last_error, delay,
                )
                self._sleep(delay)

        if last_status == 429:
            remedy = "Raise --min-interval (default 0.2) and retry, e.g. --min-interval 1."
            if not self._proxy_url and not self.auto_proxy:
                remedy += " Or re-run with --auto-proxy to fall back to a residential proxy."
            raise RateLimitedError(
                f"Chợ Tốt gateway rate-limited this client after {attempt} attempts.",
                remedy=remedy,
                retry_after=last_retry_after,
            )
        raise TransportError(
            f"Could not reach the Chợ Tốt gateway after {attempt} attempts ({last_error}).",
            remedy="Check network connectivity, then run 'chotot doctor'.",
        )
