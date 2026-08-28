"""The transport is the only module that talks to the network.

It had no test of a *successful* response at all, so deleting gzip
decompression, the BOM-tolerant decode, or the throttle left the whole suite
green. Every test here drives the real ``HttpTransport`` through an injected
opener, so the decode and retry paths actually execute.
"""
from __future__ import annotations

import gzip
import io
import json
import urllib.error

import pytest

from chotot.errors import (
    NotFoundError, RateLimitedError, TransportError, UpstreamContractError, UsageError,
)
from chotot.http import HttpTransport


class FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, body: bytes, headers=None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def transport_returning(body: bytes, headers=None, **kwargs) -> HttpTransport:
    def opener(request, timeout=None):
        return FakeResponse(body, headers)

    return HttpTransport("https://example.invalid", opener=opener,
                         sleep=lambda _: None, min_interval=0, **kwargs)


def test_plain_json_is_parsed():
    payload = transport_returning(b'{"ads": [], "total": 7}').get_json("ad-listing")
    assert payload["total"] == 7


def test_gzip_encoded_body_is_decompressed():
    """The client asks for gzip; without decompression every response is binary."""
    body = gzip.compress(json.dumps({"total": 3}).encode())
    transport = transport_returning(body, {"Content-Encoding": "gzip"})
    assert transport.get_json("ad-listing")["total"] == 3


def test_utf8_bom_does_not_corrupt_the_first_key():
    """A BOM welded onto key one makes exactly that field silently unreadable."""
    body = "﻿".encode() + json.dumps({"total": 5, "ads": []}).encode()
    assert transport_returning(body).get_json("ad-listing")["total"] == 5


def test_vietnamese_text_survives_the_decode():
    body = json.dumps({"name": "Quận Gò Vấp"}, ensure_ascii=False).encode("utf-8")
    assert transport_returning(body).get_json("x")["name"] == "Quận Gò Vấp"


def test_query_parameters_are_encoded_into_the_url():
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        return FakeResponse(b"{}")

    HttpTransport("https://example.invalid", opener=opener, sleep=lambda _: None,
                  min_interval=0).get_json("ad-listing", {"q": "điện thoại", "limit": 5})
    assert "limit=5" in seen["url"]
    assert " " not in seen["url"]
    assert "%" in seen["url"], "unicode was not percent-encoded"


def test_none_valued_parameters_are_omitted():
    """Sending an explicit null asks a different question than not asking."""
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        return FakeResponse(b"{}")

    HttpTransport("https://example.invalid", opener=opener, sleep=lambda _: None,
                  min_interval=0).get_json("ad-listing", {"q": "x", "cg": None})
    assert "cg=" not in seen["url"]


def test_a_non_object_body_is_a_contract_error():
    with pytest.raises(UpstreamContractError):
        transport_returning(b"[1, 2, 3]").get_json("ad-listing")


def test_invalid_json_is_a_contract_error():
    with pytest.raises(UpstreamContractError):
        transport_returning(b"<html>captive portal</html>").get_json("ad-listing")


def _erroring(code: int, headers=None, attempts=None):
    def opener(request, timeout=None):
        if attempts is not None:
            attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, code, "err", headers or {},
                                     io.BytesIO(b'{"message":"x"}'))

    return opener


def test_404_is_not_found_and_is_not_retried():
    """A missing listing is an answer, not a transient fault."""
    attempts = []
    transport = HttpTransport("https://example.invalid", opener=_erroring(404, attempts=attempts),
                              sleep=lambda _: None, min_interval=0, max_retries=3)
    with pytest.raises(NotFoundError):
        transport.get_json("ad-listing/1")
    assert len(attempts) == 1, "a 404 was retried"


def test_400_is_a_usage_error_not_a_transport_failure():
    transport = HttpTransport("https://example.invalid", opener=_erroring(400),
                              sleep=lambda _: None, min_interval=0)
    with pytest.raises(UsageError):
        transport.get_json("ad-listing")


def test_503_is_retried_then_reported_as_transport():
    attempts = []
    transport = HttpTransport("https://example.invalid", opener=_erroring(503, attempts=attempts),
                              sleep=lambda _: None, min_interval=0, max_retries=3)
    with pytest.raises(TransportError):
        transport.get_json("ad-listing")
    assert len(attempts) == 3, f"expected 3 attempts, made {len(attempts)}"


def test_429_becomes_a_rate_limit_error_carrying_retry_after():
    """The server states the wait; probing for it would be slower AND wrong."""
    slept = []
    transport = HttpTransport(
        "https://example.invalid", opener=_erroring(429, {"Retry-After": "7"}),
        sleep=slept.append, min_interval=0, max_retries=2)
    with pytest.raises(RateLimitedError) as excinfo:
        transport.get_json("ad-listing")
    assert excinfo.value.retry_after == 7.0
    assert slept and slept[0] == 7.0, f"honoured backoff was {slept}"


def test_a_network_failure_is_retried_then_reported():
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        raise urllib.error.URLError("no route to host")

    transport = HttpTransport("https://example.invalid", opener=opener,
                              sleep=lambda _: None, min_interval=0, max_retries=3)
    with pytest.raises(TransportError):
        transport.get_json("ad-listing")
    assert len(attempts) == 3


def test_the_throttle_waits_between_requests():
    slept = []
    transport = HttpTransport("https://example.invalid",
                              opener=lambda r, timeout=None: FakeResponse(b"{}"),
                              sleep=slept.append, min_interval=0.5)
    transport.get_json("a")
    transport.get_json("b")
    assert slept, "no delay was applied between consecutive requests"


def test_request_count_is_reported_honestly():
    transport = transport_returning(b"{}")
    transport.get_json("a")
    transport.get_json("b")
    assert transport.request_count == 2
