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

Two decisions worth stating, because both are places this class of client
usually goes wrong:

* **Bytes are decoded explicitly.** The gateway does not always declare a
  charset. Letting a library guess (``requests`` guesses ISO-8859-1 for
  ``text/*``) welds a BOM onto the first JSON key, which makes exactly one field
  silently unreadable while its neighbours look fine.
* **A rate limit is read, not probed.** When the server states ``Retry-After``
  we wait that long; we never re-run a request to discover a duration the
  response already contained.
"""
from __future__ import annotations

import gzip
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Callable, Dict, Optional

from chotot.errors import (
    NotFoundError, RateLimitedError, TransportError, UpstreamContractError, UsageError,
)

logger = logging.getLogger("chotot.http")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

#: Statuses worth another attempt. 404 is deliberately absent: a missing listing
#: is an answer, not a transient fault, and retrying it wastes the user's time.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_MIN_INTERVAL = 0.2
MAX_BACKOFF = 30.0


class HttpTransport:
    """A small, polite JSON client.

    Args:
        base_url: Gateway root, without a trailing slash.
        timeout: Per-request timeout in seconds.
        max_retries: Total attempts for a retryable failure (1 = no retry).
        min_interval: Minimum spacing between requests, applied automatically.
        user_agent: Sent verbatim; the gateway serves anonymous traffic.
        opener: Injection point for tests. Must accept ``(request, timeout)``.
        sleep: Injection point for tests, so backoff is asserted, not waited on.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        user_agent: str = DEFAULT_USER_AGENT,
        opener: Optional[Callable[..., Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.min_interval = max(0.0, min_interval)
        self.user_agent = user_agent
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._last_request_at = 0.0
        #: Requests actually issued, so callers can report cost honestly.
        self.request_count = 0

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

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers())
            try:
                self.request_count += 1
                # --verbose promised "log gateway activity" and logged nothing:
                # the only record in this module was the retry warning, so a
                # successful run -- every run a user would debug -- was silent.
                # The gateway needs no credential, so the URL carries no secret.
                logger.debug("GET %s (attempt %d/%d)", url, attempt, self.max_retries)
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
                if exc.code not in RETRYABLE_STATUSES:
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
                        remedy="This status is not retryable. Verify the query "
                               "parameters with 'chotot doctor'.",
                    ) from exc
                last_error = f"HTTP {exc.code}"

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            except json.JSONDecodeError as exc:
                raise UpstreamContractError(
                    f"Chợ Tốt gateway returned a non-JSON body for /{path.lstrip('/')}: {exc}",
                    remedy="The endpoint may be behind a captive portal or a "
                           "proxy interception page.",
                ) from exc

            if attempt < self.max_retries:
                delay = self._backoff(attempt, last_retry_after)
                logger.warning(
                    "attempt %d/%d failed (%s); retrying in %.2fs",
                    attempt, self.max_retries, last_error, delay,
                )
                self._sleep(delay)

        if last_status == 429:
            raise RateLimitedError(
                f"Chợ Tốt gateway rate-limited this client after "
                f"{self.max_retries} attempts.",
                remedy="Raise --min-interval (default 0.2) and retry, "
                       "e.g. --min-interval 1.",
                retry_after=last_retry_after,
            )
        raise TransportError(
            f"Could not reach the Chợ Tốt gateway after {self.max_retries} "
            f"attempts ({last_error}).",
            remedy="Check network connectivity, then run 'chotot doctor'.",
        )
