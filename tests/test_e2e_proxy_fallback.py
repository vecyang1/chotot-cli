"""End-to-end: the real entry point, real sockets, the paid path.

Every scenario runs ``python -m chotot.cli`` as a process against three local
servers (see ``tests/localservers.py``). The point is the wiring nobody had
executed: a blocked direct request, the resolver consulted as a subprocess,
the opener rebuilt, the request re-issued through a proxy -- and the negative
half, where none of that may happen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import load_fixture
from tests.localservers import BlockingGateway, RewritingProxy, UpstreamStub, resolver_script

ROOT = Path(__file__).resolve().parent.parent
FAKE_CREDENTIAL = "s3cretpw"


def run_cli(args, env_extra, timeout=60):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith("_PROXY") and not k.startswith("CHOTOT_")}
    env.update({"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1"})
    env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "chotot.cli", *args],
                          capture_output=True, text=True, env=env, timeout=timeout, cwd=str(ROOT))


@pytest.fixture()
def world(tmp_path):
    """A blocking gateway, an upstream that answers, a proxy that forwards to it."""
    payload = load_fixture("search_iphone.json")
    with UpstreamStub(payload) as upstream, RewritingProxy(upstream.url) as forward, \
            BlockingGateway(403) as gateway:
        log = tmp_path / "resolver.log"
        script = resolver_script(tmp_path, f"http://user:{FAKE_CREDENTIAL}@127.0.0.1:{forward.port}",
                                 argv_log=str(log))
        yield {
            "gateway": gateway, "proxy": forward, "upstream": upstream,
            "resolver_env": f"{sys.executable} {script} --geo {{geo}}",
            "resolver_log": log,
            "base_env": {"CHOTOT_BASE_URL": gateway.url},
        }


def test_auto_proxy_starts_direct_then_switches_after_a_block(world):
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"]}
    result = run_cli(["search", "iphone", "--limit", "3", "--auto-proxy", "--json",
                      "--retries", "2", "--min-interval", "0"], env)

    assert result.returncode == 0, result.stderr
    listings = json.loads(result.stdout)["listings"]
    assert len(listings) == 3

    # The write path, read back from the other side of each socket:
    assert len(world["gateway"].hits) >= 1, "the first request must be direct"
    assert len(world["proxy"].hits) >= 1, "the retry must go through the proxy"
    assert world["upstream"].hits, "the proxy must have forwarded to upstream"
    assert world["resolver_log"].read_text().strip() == "--geo vn", "resolver called once, with the geo"

    # Announced on stderr, credential masked.
    assert "residential proxy" in result.stderr
    assert FAKE_CREDENTIAL not in result.stderr and FAKE_CREDENTIAL not in result.stdout


def test_without_auto_proxy_a_block_is_reported_and_the_flag_is_named(world):
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"]}
    result = run_cli(["search", "iphone", "--limit", "3", "--json", "--retries", "2",
                      "--min-interval", "0"], env)
    assert result.returncode == 5, result.stderr  # transport failure
    assert "403" in result.stderr and "--auto-proxy" in result.stderr
    assert world["proxy"].hits == [], "no proxy may be used without the flag"
    assert not world["resolver_log"].exists(), "the resolver must not even be consulted"


def test_explicit_proxy_auto_never_touches_the_gateway_directly(world):
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"]}
    result = run_cli(["search", "iphone", "--limit", "2", "--proxy", "auto", "--json",
                      "--min-interval", "0"], env)
    assert result.returncode == 0, result.stderr
    assert world["gateway"].hits == [], "'--proxy auto' means proxied from the first request"
    assert len(world["proxy"].hits) >= 1


def test_env_can_arm_the_fallback_without_a_flag(world):
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"],
           "CHOTOT_AUTO_PROXY": "1"}
    result = run_cli(["search", "iphone", "--limit", "2", "--json", "--retries", "2",
                      "--min-interval", "0"], env)
    assert result.returncode == 0, result.stderr
    assert world["gateway"].hits and world["proxy"].hits


def test_fallback_statuses_are_configurable(world):
    """With 403 excluded from the trigger set, a 403 is a plain failure again."""
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"],
           "CHOTOT_PROXY_FALLBACK_STATUSES": "429"}
    result = run_cli(["search", "iphone", "--limit", "2", "--auto-proxy", "--json",
                      "--retries", "2", "--min-interval", "0"], env)
    assert result.returncode == 5, result.stderr
    assert world["proxy"].hits == []


def test_a_429_block_also_switches(tmp_path):
    payload = load_fixture("search_iphone.json")
    with UpstreamStub(payload) as upstream, RewritingProxy(upstream.url) as forward, \
            BlockingGateway(429) as gateway:
        script = resolver_script(tmp_path, f"http://u:{FAKE_CREDENTIAL}@127.0.0.1:{forward.port}")
        env = {"CHOTOT_BASE_URL": gateway.url,
               "CHOTOT_PROXY_RESOLVER": f"{sys.executable} {script}"}
        result = run_cli(["search", "iphone", "--limit", "2", "--auto-proxy", "--json",
                          "--retries", "2", "--min-interval", "0"], env)
        assert result.returncode == 0, result.stderr
        assert gateway.hits and forward.hits


def test_socks_proxy_is_refused_before_any_request(world):
    result = run_cli(["search", "iphone", "--proxy", "socks5://127.0.0.1:1080", "--json"],
                     world["base_env"])
    assert result.returncode == 2, result.stderr
    assert "SOCKS" in result.stderr
    assert world["gateway"].hits == [] and world["proxy"].hits == []


def test_unresolvable_explicit_auto_is_a_usage_error_before_any_request(world, tmp_path):
    failing = tmp_path / "fail.py"
    failing.write_text("import sys; sys.exit(1)\n")
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": f"{sys.executable} {failing}"}
    result = run_cli(["search", "iphone", "--proxy", "auto", "--json"], env)
    assert result.returncode == 2, result.stderr
    assert "CHOTOT_PROXY_RESOLVER" in result.stderr
    assert world["gateway"].hits == []


def test_unresolvable_fallback_warns_once_and_keeps_going_direct(world, tmp_path):
    failing = tmp_path / "fail.py"
    failing.write_text("import sys; sys.exit(1)\n")
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": f"{sys.executable} {failing}"}
    result = run_cli(["search", "iphone", "--auto-proxy", "--json", "--retries", "3",
                      "--min-interval", "0"], env)
    assert result.returncode == 5, result.stderr
    assert result.stderr.count("could not resolve") == 1, result.stderr
    assert world["proxy"].hits == []


def test_doctor_json_reports_the_transport_mode(world):
    """Telemetry is part of the contract: a reader must be able to tell which
    address the checks were graded from."""
    env = {**world["base_env"], "CHOTOT_PROXY_RESOLVER": world["resolver_env"]}
    result = run_cli(["doctor", "--proxy", "auto", "--json", "--min-interval", "0",
                      "--retries", "1"], env, timeout=120)
    # The upstream stub answers every path with one search payload, so most
    # checks FAIL; the transport block must still be reported and exit stay
    # non-zero -- a failing doctor is the honest outcome here.
    payload = json.loads(result.stdout)
    assert payload["transport"]["mode"] == "proxy"
    assert payload["transport"]["source"] == "resolver"
    assert FAKE_CREDENTIAL not in result.stdout
