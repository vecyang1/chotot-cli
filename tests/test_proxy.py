"""Proxy resolution: the contract between a flag, the environment, and the
resolver that owns the residential-proxy credential.

Three properties decide whether this module is safe to ship:

* nothing it prints can contain a credential, however the proxy was spelled;
* explicit intent never degrades silently -- ``--proxy auto`` that cannot
  resolve is an error, not a quiet direct connection;
* the credential is read by ONE owner (the resolver command), never by a
  second reader of that owner's cache.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from chotot import proxy
from chotot.errors import UsageError
from tests.localservers import resolver_script


# -- masking ---------------------------------------------------------------

MASK_VECTORS = [
    # (url, user, password) -- fixed vectors, including a password with no digit
    # at all and a user shorter than any prefix a mask might keep.
    ("http://user123:secret456@gw.dataimpulse.com:823", "user123", "secret456"),
    ("http://u:p@127.0.0.1:7890", "u", "p"),
    ("socks5://alice:onlyletters@proxy.example:1080", "alice", "onlyletters"),
    ("https://abcdefghijkl__cr-vn:zz%40yy@gw.dataimpulse.com:823", "abcdefghijkl", "zz%40yy"),
    ("http://longusernamehere:pw@host", "longusernamehere", "pw"),
]


@pytest.mark.parametrize("url,user,password", MASK_VECTORS)
def test_mask_keeps_no_substring_of_the_credential(url, user, password):
    """A safety-named function is believed on its name; check what it emits.
    Every prefix of length >= 3 is graded, not just the whole value: the old
    mask kept ``user[:4]``, which a whole-value check passes by construction.
    (Two letters are below the floor -- 'se' is a substring of 'dataimpulse'.)"""
    masked = proxy.mask_proxy(url)
    for secret in (user, password):
        for length in range(3, len(secret) + 1):
            assert secret[:length] not in masked, f"{secret[:length]!r} survived in {masked!r}"
    assert masked.startswith(url.split("://", 1)[0] + "://")
    assert masked.endswith("@" + url.rsplit("@", 1)[1])


def test_mask_leaves_a_credential_free_url_alone():
    assert proxy.mask_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert proxy.mask_proxy(None) == "none"
    assert proxy.mask_proxy("") == "none"


def test_mask_never_returns_the_input_when_it_carried_userinfo():
    assert proxy.mask_proxy("http://x:y@h") != "http://x:y@h"
    assert proxy.mask_proxy("garbage:with@at") not in ("garbage:with@at",)


# -- validation ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "socks5://127.0.0.1:1080", "socks5h://u:p@h:1", "socks4://h:1", "socks://h:1",
])
def test_socks_proxies_are_refused_with_the_cause_named(url):
    """urllib has no SOCKS support; handing it a socks URL fails deep inside
    the stack with 'unknown url type'. Name the cause where it is known."""
    with pytest.raises(UsageError) as info:
        proxy.validate_proxy_url(url, origin="--proxy")
    message = str(info.value)
    assert "SOCKS" in message and "--proxy" in message
    assert info.value.remedy and "http://" in info.value.remedy


def test_a_schemeless_proxy_is_refused():
    with pytest.raises(UsageError):
        proxy.validate_proxy_url("127.0.0.1:7890", origin="CHOTOT_PROXY")


def test_http_and_https_proxies_are_accepted_verbatim():
    assert proxy.validate_proxy_url("http://127.0.0.1:7890", origin="--proxy") == "http://127.0.0.1:7890"
    assert proxy.validate_proxy_url("HTTPS://h:1", origin="--proxy") == "HTTPS://h:1"


# -- environment -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _scrub_proxy_env(monkeypatch):
    for name in proxy.ALL_PROXY_ENV_NAMES + (proxy.ENV_RESOLVER, proxy.ENV_AUTO_PROXY):
        monkeypatch.delenv(name, raising=False)


def test_no_flag_and_no_env_is_direct():
    plan = proxy.resolve_proxy(None, geo="vn", resolver=lambda geo: "http://never:used@h:1")
    assert plan.url is None and plan.source == "direct"


def test_chotot_proxy_env_outranks_the_standard_variables(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://standard:9")
    monkeypatch.setenv("CHOTOT_PROXY", "http://specific:9")
    plan = proxy.resolve_proxy(None, geo="vn", resolver=lambda geo: None)
    assert plan.url == "http://specific:9" and plan.source == "env:CHOTOT_PROXY"


def test_standard_env_proxy_is_honoured_and_named(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://standard:9")
    plan = proxy.resolve_proxy(None, geo="vn", resolver=lambda geo: None)
    assert plan.url == "http://standard:9" and plan.source == "env:https_proxy"


@pytest.mark.parametrize("value", ["none", "direct", "0", "false", "DIRECT"])
def test_chotot_proxy_none_forces_direct_over_a_global_env_proxy(monkeypatch, value):
    monkeypatch.setenv("HTTPS_PROXY", "http://standard:9")
    monkeypatch.setenv("CHOTOT_PROXY", value)
    assert proxy.resolve_proxy(None, geo="vn", resolver=lambda geo: None).url is None


def test_an_env_socks_proxy_is_refused_naming_the_variable(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    with pytest.raises(UsageError) as info:
        proxy.resolve_proxy(None, geo="vn", resolver=lambda geo: None)
    assert "ALL_PROXY" in str(info.value)


# -- explicit flag ---------------------------------------------------------

def test_explicit_url_is_used_verbatim_and_beats_env(monkeypatch):
    monkeypatch.setenv("CHOTOT_PROXY", "http://env:9")
    plan = proxy.resolve_proxy("http://flag:9", geo="vn", resolver=lambda geo: None)
    assert plan.url == "http://flag:9" and plan.source == "flag"


@pytest.mark.parametrize("value", ["none", "direct"])
def test_explicit_none_is_direct_even_with_env_set(monkeypatch, value):
    monkeypatch.setenv("CHOTOT_PROXY", "http://env:9")
    assert proxy.resolve_proxy(value, geo="vn", resolver=lambda geo: None).url is None


def test_explicit_auto_resolves_now_and_is_labelled():
    calls = []

    def resolver(geo):
        calls.append(geo)
        return "http://u:p@residential:823"

    plan = proxy.resolve_proxy("auto", geo="jp", resolver=resolver)
    assert plan.url == "http://u:p@residential:823"
    assert plan.source == "resolver" and calls == ["jp"]


def test_explicit_auto_that_cannot_resolve_is_an_error_not_a_quiet_direct_connection(monkeypatch):
    """The old code fell through to `get_env_proxy()` and then to direct: the
    user asked for a proxy, paid nothing, and got their own address blocked."""
    monkeypatch.setenv("HTTPS_PROXY", "http://standard:9")  # must NOT be used as a substitute
    with pytest.raises(UsageError) as info:
        proxy.resolve_proxy("auto", geo="vn", resolver=lambda geo: None)
    assert "auto" in str(info.value)
    assert proxy.ENV_RESOLVER in (info.value.remedy or "")


def test_a_resolver_that_returns_socks_is_a_configuration_error():
    with pytest.raises(UsageError) as info:
        proxy.resolve_proxy("auto", geo="vn", resolver=lambda geo: "socks5://h:1")
    assert "resolver" in str(info.value).lower()


# -- the resolver command (the single credential chokepoint) ------------------

def test_resolver_command_from_env_substitutes_the_geo_placeholder(monkeypatch):
    monkeypatch.setenv(proxy.ENV_RESOLVER, "/usr/bin/resolver --country {geo} 'two words'")
    assert proxy.resolver_command("vn") == ["/usr/bin/resolver", "--country", "vn", "two words"]


def test_resolver_command_from_env_without_placeholders_is_left_alone(monkeypatch):
    monkeypatch.setenv(proxy.ENV_RESOLVER, "my-resolver --format url")
    assert proxy.resolver_command("vn") == ["my-resolver", "--format", "url"]


def test_resolver_command_falls_back_to_the_known_skill_locations(monkeypatch, tmp_path):
    script = tmp_path / "proxy_resolver.py"
    script.write_text("print('x')\n")
    monkeypatch.setattr(proxy, "DEFAULT_RESOLVER_CANDIDATES", (tmp_path / "missing.py", script))
    command = proxy.resolver_command("vn")
    assert command[:2] == [sys.executable, str(script)]
    assert command[2:] == ["--format", "url", "--geo", "vn"]


def test_no_resolver_anywhere_is_none_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "DEFAULT_RESOLVER_CANDIDATES", (tmp_path / "missing.py",))
    assert proxy.resolver_command("vn") is None
    assert proxy.resolve_residential("vn") is None


def test_resolve_residential_runs_the_command_and_reads_one_line(monkeypatch, tmp_path):
    """A real subprocess: the contract is 'stdout is the URL', nothing else."""
    log = tmp_path / "argv.log"
    script = resolver_script(tmp_path, "http://u:p@127.0.0.1:1\n", argv_log=str(log))
    monkeypatch.setenv(proxy.ENV_RESOLVER, f"{sys.executable} {script} --geo {{geo}}")
    assert proxy.resolve_residential("vn") == "http://u:p@127.0.0.1:1"
    assert log.read_text().strip() == "--geo vn"


def test_resolve_residential_treats_a_failing_or_silent_command_as_unresolved(monkeypatch, tmp_path):
    failing = tmp_path / "fail.py"
    failing.write_text("import sys; sys.exit(3)\n")
    monkeypatch.setenv(proxy.ENV_RESOLVER, f"{sys.executable} {failing}")
    assert proxy.resolve_residential("vn") is None

    silent = tmp_path / "silent.py"
    silent.write_text("print('')\n")
    monkeypatch.setenv(proxy.ENV_RESOLVER, f"{sys.executable} {silent}")
    assert proxy.resolve_residential("vn") is None


def test_resolve_residential_times_out_rather_than_hanging(monkeypatch, tmp_path):
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5); print('http://late:1')\n")
    monkeypatch.setenv(proxy.ENV_RESOLVER, f"{sys.executable} {slow}")
    assert proxy.resolve_residential("vn", timeout=0.5) is None


def test_the_module_reads_no_credential_cache_of_its_own():
    """Two readers of one credential store are two owners; chotot has none.
    The resolver command is the only path to a residential credential."""
    source = Path(proxy.__file__).read_text(encoding="utf-8")
    for forbidden in ("proxy_cache.json", "DATAIMPULSE_LOGIN", "DATAIMPULSE_PASSWORD",
                      "SCRAPER_PROXY_URL", "dataimpulse.com"):
        assert forbidden not in source, f"chotot must not read {forbidden} itself"
