# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Proxy resolution: one decision, one credential owner, nothing printed.

The transport asks this module one question -- *which proxy, if any, and
why* -- and gets a :class:`ProxyPlan` back. Three rules shape the answer:

* **A proxy is an ``http://`` or ``https://`` URL.** The standard library has
  no SOCKS support, and handing ``urllib`` a ``socks5://`` URL fails deep in
  the stack as ``unknown url type``. That cause is named here, where it is
  known, instead of there.
* **Explicit intent never degrades silently.** ``--proxy auto`` that cannot
  resolve a proxy is an error. The 2.1.0 code fell through to a direct
  connection, so the user asked for a proxy, paid nothing, and got their own
  address blocked while the output looked normal.
* **The residential credential has exactly one reader: the resolver
  command.** This module does not know DataImpulse, its environment
  variables, or its credential cache. It runs a command whose stdout is a
  proxy URL -- ``CHOTOT_PROXY_RESOLVER`` if set, else the
  ``ultra-low-cost-scraper`` skill's ``proxy_resolver.py`` where that is
  installed -- and keeps nothing. A second reader of another tool's
  credential store is a second owner, and two owners drift.

Only :func:`mask_proxy` output may ever reach a log, a terminal or a JSON
report; ``tests/test_proxy.py`` grades every prefix of a credential against it.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from chotot.errors import UsageError

logger = logging.getLogger("chotot.proxy")

DEFAULT_GEO = "vn"

#: The chotot-specific override; wins over the standard variables below.
#: ``none``/``direct`` forces a direct connection even when they are set.
ENV_PROXY = "CHOTOT_PROXY"
#: ``1``/``true``/``yes`` arms the residential fallback without ``--auto-proxy``.
ENV_AUTO_PROXY = "CHOTOT_AUTO_PROXY"
#: A command (shell-quoted) that prints a proxy URL on stdout. ``{geo}`` is
#: replaced with the requested exit country. Exit non-zero or print nothing to
#: report "no proxy available".
ENV_RESOLVER = "CHOTOT_PROXY_RESOLVER"

STANDARD_PROXY_ENV_NAMES: Tuple[str, ...] = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
)
ALL_PROXY_ENV_NAMES: Tuple[str, ...] = (ENV_PROXY,) + STANDARD_PROXY_ENV_NAMES

DIRECT_WORDS = frozenset({"none", "direct", "off", "0", "false"})
SUPPORTED_SCHEMES = frozenset({"http", "https"})
SOCKS_SCHEMES = frozenset({"socks", "socks4", "socks4a", "socks5", "socks5h"})

#: Where the owning resolver lives when the skill is installed. Overridable
#: through ``CHOTOT_PROXY_RESOLVER``; probed in order, first existing file wins.
DEFAULT_RESOLVER_CANDIDATES: Tuple[Path, ...] = (
    Path.home() / ".gemini" / "antigravity" / "skills" / "ultra-low-cost-scraper"
    / "scripts" / "proxy_resolver.py",
    Path.home() / ".agents" / "skills" / "ultra-low-cost-scraper" / "scripts" / "proxy_resolver.py",
)
#: The owning resolver may consult 1Password, to which it gives 15 s itself.
RESOLVER_TIMEOUT = 25.0

Resolver = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class ProxyPlan:
    """The transport's marching orders: ``url`` (None = direct) and ``source``,
    one of ``direct``, ``flag``, ``env:<VARIABLE>``, ``resolver``, ``injected``."""

    url: Optional[str]
    source: str


# -- display ---------------------------------------------------------------

def mask_proxy(url: Optional[str]) -> str:
    """Render a proxy URL with its userinfo removed entirely.

    ``http://user:pw@host:823`` -> ``http://***:***@host:823``. No prefix of
    the user name survives: the previous mask kept four characters of it, and
    four characters of a residential login are a working eighth of the secret.
    """
    if not url:
        return "none"
    parts = urlsplit(url)
    if "@" in parts.netloc:
        hostport = parts.netloc.rsplit("@", 1)[1]
        return f"{parts.scheme or 'proxy'}://***:***@{hostport}"
    if "@" in url:
        # Unparseable but carrying something that looks like a credential:
        # showing any of it is the wrong default.
        return "***"
    return url


# -- validation ------------------------------------------------------------

def validate_proxy_url(url: str, origin: str) -> str:
    """Accept only what ``urllib`` can actually use, naming ``origin`` on refusal."""
    value = url.strip()
    scheme = urlsplit(value).scheme.lower()
    if scheme in SOCKS_SCHEMES:
        raise UsageError(
            f"{origin} is a SOCKS proxy ({mask_proxy(value)}), which the Python standard "
            f"library cannot speak.",
            remedy="Point it at an HTTP proxy instead (Clash and mihomo expose one, usually "
                   "http://127.0.0.1:7890), or use --proxy auto for the residential resolver.",
        )
    if scheme not in SUPPORTED_SCHEMES or not urlsplit(value).netloc:
        raise UsageError(
            f"{origin} is not an http:// or https:// proxy URL: {mask_proxy(value)}",
            remedy="Write the scheme and host, e.g. http://127.0.0.1:7890.",
        )
    return value


# -- sources ---------------------------------------------------------------

def env_proxy() -> Optional[Tuple[Optional[str], str]]:
    """The first proxy variable that is set, as ``(url_or_None, variable)``.

    ``None`` means no variable is set at all. A variable holding ``none`` or
    ``direct`` returns ``(None, name)``: an explicit direct connection, which
    outranks any variable further down the list.
    """
    for name in ALL_PROXY_ENV_NAMES:
        value = os.getenv(name)
        if value is None or not value.strip():
            continue
        if value.strip().lower() in DIRECT_WORDS:
            return None, name
        return validate_proxy_url(value, origin=name), name
    return None


def resolver_command(geo: str) -> Optional[List[str]]:
    """The command that owns the residential credential, or None if there is none."""
    configured = os.getenv(ENV_RESOLVER, "").strip()
    if configured:
        return [part.replace("{geo}", geo) for part in shlex.split(configured)]
    for candidate in DEFAULT_RESOLVER_CANDIDATES:
        if candidate.is_file():
            return [sys.executable, str(candidate), "--format", "url", "--geo", geo]
    return None


def resolve_residential(geo: str, timeout: float = RESOLVER_TIMEOUT) -> Optional[str]:
    """Run the resolver command and return the URL it printed, or None.

    The URL is returned to the caller and to nobody else: it is never logged,
    and the resolver's own output is never echoed.
    """
    command = resolver_command(geo)
    if command is None:
        logger.debug("no proxy resolver: %s is unset and no known resolver script exists",
                     ENV_RESOLVER)
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("proxy resolver %s gave no answer within %.0fs", command[0], timeout)
        return None
    except OSError as exc:
        logger.warning("proxy resolver %s could not be started: %s", command[0], exc)
        return None
    if result.returncode != 0:
        logger.debug("proxy resolver exited %d; treating as unresolved", result.returncode)
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


# -- the decision ------------------------------------------------------------

def resolve_proxy(proxy_arg: Optional[str], geo: Optional[str] = None,
                  resolver: Optional[Resolver] = None) -> ProxyPlan:
    """Decide the proxy for a transport from the flag, then the environment.

    ``proxy_arg`` is the ``--proxy`` value: a URL, ``auto``, ``none``/``direct``,
    or None when the flag was not given. ``auto`` resolves NOW and fails loudly;
    the fallback armed by ``--auto-proxy`` is the transport's business, not this
    function's, because it is a reaction to a response.
    """
    exit_country = geo or DEFAULT_GEO
    resolve = resolver or resolve_residential

    if proxy_arg is not None and proxy_arg.strip():
        value = proxy_arg.strip()
        if value.lower() in DIRECT_WORDS:
            return ProxyPlan(None, "direct")
        if value.lower() == "auto":
            url = resolve(exit_country)
            if not url:
                raise UsageError(
                    f"--proxy auto could not resolve a residential proxy for geo={exit_country}.",
                    remedy=f"Set {ENV_RESOLVER} to a command that prints a proxy URL on "
                           f"stdout, or pass an explicit --proxy http://... (or set "
                           f"{ENV_PROXY}). The ultra-low-cost-scraper skill's "
                           f"proxy_resolver.py is found automatically when it is installed.",
                )
            return ProxyPlan(validate_proxy_url(url, origin="the proxy resolver"), "resolver")
        return ProxyPlan(validate_proxy_url(value, origin="--proxy"), "flag")

    found = env_proxy()
    if found is None:
        return ProxyPlan(None, "direct")
    url, name = found
    return ProxyPlan(url, f"env:{name}") if url else ProxyPlan(None, "direct")
