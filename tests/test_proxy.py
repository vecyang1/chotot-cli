"""Tests for proxy resolution, masking, and anti-scraping transport resilience."""
from __future__ import annotations

import json
import urllib.error
import io
import pytest

from chotot import proxy
from chotot.http import HttpTransport
from chotot.cli import build_parser


def test_mask_proxy_masks_credentials():
    assert proxy.mask_proxy("http://user123:secret456@gw.dataimpulse.com:823") == "http://user***:***@gw.dataimpulse.com:823"
    assert proxy.mask_proxy("http://u:p@127.0.0.1:7890") == "http://***:***@127.0.0.1:7890"


def test_mask_proxy_handles_none_or_plain():
    assert proxy.mask_proxy(None) == "none"
    assert proxy.mask_proxy("") == "none"
    assert proxy.mask_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_get_env_proxy_respects_env_vars(monkeypatch):
    monkeypatch.delenv("CHOTOT_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert proxy.get_env_proxy() is None

    monkeypatch.setenv("CHOTOT_PROXY", "http://chotot-proxy:8080")
    assert proxy.get_env_proxy() == "http://chotot-proxy:8080"

    monkeypatch.delenv("CHOTOT_PROXY")
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy:8080")
    assert proxy.get_env_proxy() == "http://https-proxy:8080"


def test_resolve_proxy_explicit_url():
    assert proxy.resolve_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert proxy.resolve_proxy("none") is None
    assert proxy.resolve_proxy("direct") is None


def test_resolve_proxy_formats_dataimpulse_with_geo():
    url = "http://myuser:mypass@gw.dataimpulse.com:823"
    resolved = proxy.resolve_proxy(url, geo="vn")
    assert resolved == "http://myuser__cr-vn:mypass@gw.dataimpulse.com:823"

    resolved_session = proxy.resolve_proxy(url, geo="vn", session_id="test1234")
    assert resolved_session == "http://myuser__cr-vn__session-test1234:mypass@gw.dataimpulse.com:823"


def test_resolve_proxy_reads_env_credentials(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_LOGIN", "envuser")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "envpass")
    monkeypatch.setenv("DATAIMPULSE_HOST", "gw.dataimpulse.com")
    monkeypatch.setenv("DATAIMPULSE_PORT", "823")

    resolved = proxy.resolve_proxy(auto=True, geo="vn")
    assert resolved == "http://envuser__cr-vn:envpass@gw.dataimpulse.com:823"


def test_resolve_proxy_reads_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("DATAIMPULSE_LOGIN", raising=False)
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    fake_cache = tmp_path / "proxy_cache.json"
    fake_cache.write_text(json.dumps({
        "login": "cacheuser",
        "password": "cachepassword",
        "hostname": "gw.dataimpulse.com",
        "port": 823
    }))
    monkeypatch.setattr(proxy, "CACHE_FILE", fake_cache)

    resolved = proxy.resolve_proxy(auto=True, geo="vn")
    assert resolved == "http://cacheuser__cr-vn:cachepassword@gw.dataimpulse.com:823"


def test_transport_with_proxy_initialization():
    transport = HttpTransport("https://example.invalid", proxy="http://127.0.0.1:8080", min_interval=0)
    assert transport.is_proxied is True
    assert transport.proxy_url == "http://127.0.0.1:8080"
    assert transport.proxy_masked == "http://127.0.0.1:8080"


def test_transport_auto_proxy_switches_on_429(monkeypatch):
    """When direct connection hits 429 and auto_proxy is enabled, switch to proxy and retry."""
    monkeypatch.setenv("DATAIMPULSE_LOGIN", "testuser")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "testpass")

    attempts = []

    class FakeResp:
        def __init__(self):
            self.headers = {}
        def read(self):
            return b'{"ads": [], "total": 1}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def opener_that_fails_then_succeeds(req, timeout=None):
        attempts.append(req)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, io.BytesIO(b"{}"))
        return FakeResp()

    # Pass opener that simulates first direct call failing then succeeding
    transport = HttpTransport(
        "https://example.invalid",
        auto_proxy=True,
        min_interval=0,
        opener=None, # Will use dynamic opener
        sleep=lambda _: None,
    )
    # Inject our mock opener as the direct opener
    transport._opener = opener_that_fails_then_succeeds
    transport._custom_opener_injected = False  # allow fallback

    data = transport.get_json("ad-listing")
    assert data["total"] == 1
    assert len(attempts) == 2
    assert transport.is_proxied is True
    assert "testuser__cr-vn" in (transport.proxy_url or "")


def test_cli_accepts_proxy_flags():
    parser = build_parser()
    args = parser.parse_args(["search", "iphone", "--proxy", "http://127.0.0.1:8080", "--auto-proxy", "--geo", "us"])
    assert args.proxy == "http://127.0.0.1:8080"
    assert args.auto_proxy is True
    assert args.geo == "us"
