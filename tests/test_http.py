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


# -- the TLS trust-store fallback, which never fires on a healthy interpreter --
#
# 193 CAs are present in both the system interpreter and a dependency-free venv
# on the reference machine, so this path had never executed. A branch that only
# runs on a broken host is exactly the one that must be tested on a working one.

def _forced_empty_store(monkeypatch, certifi_available):
    import ssl as ssl_module
    from chotot import http as http_module

    monkeypatch.setattr(http_module, "_DEFAULT_SSL_CONTEXT", None)

    class _EmptyContext:
        def get_ca_certs(self):
            return []

    made = {"cafile": "unset"}

    def fake_create(cafile=None, **kwargs):
        made["cafile"] = cafile
        return _EmptyContext()

    monkeypatch.setattr(ssl_module, "create_default_context", fake_create)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def guarded(name, *args, **kwargs):
        if name == "certifi" and not certifi_available:
            raise ImportError("No module named 'certifi'")
        if name == "certifi":
            import types

            module = types.ModuleType("certifi")
            module.where = lambda: "/fake/cacert.pem"
            return module
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded)
    return made


def test_empty_trust_store_uses_certifi_when_present(monkeypatch):
    from chotot.http import get_default_ssl_context

    made = _forced_empty_store(monkeypatch, certifi_available=True)
    get_default_ssl_context()
    assert made["cafile"] == "/fake/cacert.pem"


def test_empty_trust_store_without_certifi_names_the_remedy(monkeypatch, caplog):
    """Silence here produced a bare SSL error on every request and no cause."""
    import logging

    from chotot.http import get_default_ssl_context

    _forced_empty_store(monkeypatch, certifi_available=False)
    with caplog.at_level(logging.WARNING, logger="chotot.http"):
        get_default_ssl_context()

    message = " ".join(r.message for r in caplog.records)
    assert "empty CA trust store" in message
    assert "certifi" in message


def test_healthy_interpreter_never_touches_certifi(monkeypatch):
    """The other direction: a warning that fires on healthy input gets muted."""
    import logging

    from chotot import http as http_module

    monkeypatch.setattr(http_module, "_DEFAULT_SSL_CONTEXT", None)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("chotot.http")
    logger.addHandler(handler)
    try:
        ctx = http_module.get_default_ssl_context()
    finally:
        logger.removeHandler(handler)

    assert ctx.get_ca_certs(), "reference machine has a populated trust store"
    assert not [r for r in records if r.levelno >= logging.WARNING]


# -- residential-proxy fallback: direct first, switch on a block ----------------
#
# The 2.1.0 transport resolved the proxy in its constructor whenever
# ``auto_proxy`` was set, so every request was paid from the first one and the
# fallback branch below it could never execute: with a credential it was
# already proxied, without one it had nothing to switch to. The only test of
# the branch constructed an already-proxied transport and passed vacuously.

import logging as _logging

from chotot.errors import UsageError as _UsageError


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    """Hermetic: a developer's HTTPS_PROXY must not decide what 'direct' means."""
    from chotot import proxy as _proxy

    for name in _proxy.ALL_PROXY_ENV_NAMES + (_proxy.ENV_RESOLVER, _proxy.ENV_AUTO_PROXY,
                                              "CHOTOT_PROXY_FALLBACK_STATUSES"):
        monkeypatch.delenv(name, raising=False)


class _Recorder:
    """Records which proxy URL each opener was built with, and serves scripted
    responses, so the switch is observed from the opener's side."""

    def __init__(self, script):
        self.script = list(script)  # each item: FakeResponse or an exception
        self.built_with = []
        self.served_via = []

    def factory(self, proxy_url):
        self.built_with.append(proxy_url)

        def opener(request, timeout=None):
            self.served_via.append(proxy_url)
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return opener


def _blocked(status, retry_after="0"):
    headers = {"Retry-After": retry_after} if status == 429 else {}
    return urllib.error.HTTPError("https://example.invalid/x", status, "blocked", headers,
                                  io.BytesIO(b'{"blocked":true}'))


def _ok():
    return FakeResponse(b'{"ads": [], "total": 1}')


def _transport(recorder, resolver, **kwargs):
    kwargs.setdefault("min_interval", 0)
    return HttpTransport("https://example.invalid", opener_factory=recorder.factory,
                         resolver=resolver, sleep=lambda _: None, **kwargs)


def test_auto_proxy_starts_direct_even_when_a_proxy_is_resolvable():
    recorder = _Recorder([_ok()])
    transport = _transport(recorder, resolver=lambda geo: "http://u:p@res:1", auto_proxy=True)
    assert transport.is_proxied is False
    assert transport.transport_mode == "direct"
    transport.get_json("ad-listing")
    assert recorder.served_via == [None]


def test_a_direct_429_switches_to_the_resolved_proxy_and_retries(caplog):
    recorder = _Recorder([_blocked(429), _ok()])
    calls = []

    def resolver(geo):
        calls.append(geo)
        return "http://u:p@res:1"

    transport = _transport(recorder, resolver, auto_proxy=True, geo="vn", max_retries=3)
    with caplog.at_level(_logging.WARNING, logger="chotot.http"):
        payload = transport.get_json("ad-listing")

    assert payload["total"] == 1
    assert recorder.built_with == [None, "http://u:p@res:1"]
    assert recorder.served_via == [None, "http://u:p@res:1"]
    assert calls == ["vn"]
    assert transport.is_proxied and transport.transport_mode == "proxy (after fallback)"
    assert transport.fallback_fired_at == 1 and transport.proxied_request_count == 1
    announced = [r.message for r in caplog.records if "residential proxy" in r.message]
    assert len(announced) == 1 and "u:p@" not in announced[0]


def test_a_direct_403_switches_when_the_fallback_is_armed():
    """403 is not retryable on its own; with the fallback armed it is the
    signal the fallback exists for."""
    recorder = _Recorder([_blocked(403), _ok()])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=True)
    assert transport.get_json("ad-listing")["total"] == 1
    assert recorder.served_via == [None, "http://u:p@res:1"]


def test_a_403_without_the_fallback_is_not_retried_and_the_remedy_names_the_flag():
    recorder = _Recorder([_blocked(403)])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=False)
    with pytest.raises(TransportError) as info:
        transport.get_json("ad-listing")
    assert "403" in str(info.value)
    assert "--auto-proxy" in (info.value.remedy or "")
    assert recorder.served_via == [None]


def test_the_fallback_is_resolved_once_and_warns_once_when_nothing_resolves(caplog):
    recorder = _Recorder([_blocked(429), _blocked(429), _blocked(429)])
    calls = []

    def resolver(geo):
        calls.append(geo)
        return None

    transport = _transport(recorder, resolver, auto_proxy=True, max_retries=3)
    with caplog.at_level(_logging.WARNING, logger="chotot.http"):
        with pytest.raises(RateLimitedError):
            transport.get_json("ad-listing")
    assert calls == ["vn"], "resolution is attempted once per transport, not per attempt"
    assert sum("could not resolve" in r.message for r in caplog.records) == 1
    assert recorder.served_via == [None, None, None]


def test_a_connection_error_triggers_the_fallback():
    recorder = _Recorder([urllib.error.URLError("connection refused"), _ok()])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=True)
    assert transport.get_json("ad-listing")["total"] == 1
    assert recorder.served_via == [None, "http://u:p@res:1"]


def test_a_switch_on_the_last_attempt_still_gets_one_proxied_try():
    """--retries 1 --auto-proxy would otherwise resolve a proxy and never use it."""
    recorder = _Recorder([_blocked(429), _ok()])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=True, max_retries=1)
    assert transport.get_json("ad-listing")["total"] == 1
    assert recorder.served_via == [None, "http://u:p@res:1"]


def test_fallback_statuses_are_configurable_per_transport():
    recorder = _Recorder([_blocked(403)])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=True,
                           fallback_statuses=frozenset({429}))
    with pytest.raises(TransportError):
        transport.get_json("ad-listing")
    assert recorder.served_via == [None]


def test_fallback_statuses_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHOTOT_PROXY_FALLBACK_STATUSES", "429, 503")
    recorder = _Recorder([_blocked(403)])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", auto_proxy=True)
    assert transport.fallback_statuses == frozenset({429, 503})
    with pytest.raises(TransportError):
        transport.get_json("ad-listing")


def test_a_malformed_fallback_status_list_is_a_usage_error_naming_the_variable(monkeypatch):
    monkeypatch.setenv("CHOTOT_PROXY_FALLBACK_STATUSES", "429,teapot")
    with pytest.raises(_UsageError) as info:
        _transport(_Recorder([]), lambda geo: None, auto_proxy=True)
    assert "CHOTOT_PROXY_FALLBACK_STATUSES" in str(info.value)


def test_auto_proxy_is_armed_by_the_environment(monkeypatch):
    monkeypatch.setenv("CHOTOT_AUTO_PROXY", "1")
    recorder = _Recorder([_blocked(429), _ok()])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1")
    assert transport.auto_proxy is True
    assert transport.get_json("ad-listing")["total"] == 1
    assert recorder.served_via == [None, "http://u:p@res:1"]


def test_explicit_auto_resolves_before_the_first_request():
    recorder = _Recorder([_ok()])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1", proxy="auto")
    assert transport.is_proxied and transport.transport_mode == "proxy"
    assert transport.proxy_source == "resolver"
    transport.get_json("ad-listing")
    assert recorder.served_via == ["http://u:p@res:1"]
    assert transport.proxied_request_count == 1


def test_an_explicit_proxy_is_never_replaced_by_the_fallback():
    """A user-chosen proxy that gets blocked is the user's decision to revisit;
    silently swapping in a paid one spends money they did not ask to spend."""
    recorder = _Recorder([_blocked(429), _blocked(429)])
    transport = _transport(recorder, lambda geo: "http://u:p@res:1",
                           proxy="http://mine:1", auto_proxy=True, max_retries=2)
    with pytest.raises(RateLimitedError):
        transport.get_json("ad-listing")
    assert recorder.served_via == ["http://mine:1", "http://mine:1"]


def test_geo_with_an_explicit_url_is_warned_about(caplog):
    with caplog.at_level(_logging.WARNING, logger="chotot.http"):
        _transport(_Recorder([]), lambda geo: None, proxy="http://mine:1", geo="jp")
    assert any("--geo" in r.message for r in caplog.records)


def test_transport_summary_is_credential_free():
    recorder = _Recorder([_ok()])
    transport = _transport(recorder, lambda geo: "http://user:pw@res:1", proxy="auto")
    summary = transport.transport_summary()
    assert summary == {"mode": "proxy", "proxy": "http://***:***@res:1", "source": "resolver",
                       "fallback_fired_at": None, "requests": 0, "proxied_requests": 0}
    assert "pw" not in json.dumps(summary)
