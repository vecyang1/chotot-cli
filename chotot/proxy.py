# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Proxy resolution and formatting for chotot-cli.

Provides zero-dependency proxy resolution supporting:
1. Explicit CLI/API proxy URLs (--proxy http://...)
2. Environment variables (CHOTOT_PROXY, HTTPS_PROXY, HTTP_PROXY, ALL_PROXY)
3. Residential proxy auto-resolution (DataImpulse / ultra-low-cost-scraper)
   with Vietnam geo-targeting (__cr-vn) and sticky sessions (__session-{id}).
4. Credential masking for safe logging and status output.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("chotot.proxy")

DEFAULT_GEO = "vn"
CACHE_FILE = Path.home() / ".cache" / "ultra-low-cost-scraper" / "proxy_cache.json"


def mask_proxy(url: Optional[str]) -> str:
    """Mask proxy credentials for safe display and logging.

    Example: 'http://user:password@gw.dataimpulse.com:823' -> 'http://user***:***@gw.dataimpulse.com:823'
    """
    if not url:
        return "none"
    match = re.match(r"^(https?://)([^:]+):([^@]+)@(.*)$", url)
    if match:
        scheme, user, _pwd, hostport = match.groups()
        masked_user = user[:4] + "***" if len(user) > 4 else "***"
        return f"{scheme}{masked_user}:***@{hostport}"
    return url


def get_env_proxy() -> Optional[str]:
    """Check standard environment variables for configured proxy."""
    for key in (
        "CHOTOT_PROXY",
        "chotot_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        val = os.getenv(key)
        if val and val.strip() and val.lower() not in ("none", "direct", "0", "false"):
            return val.strip()
    return None


def _format_dataimpulse_url(
    base_url: str, geo: Optional[str] = None, session_id: Optional[str] = None,
) -> str:
    """Inject __cr-{geo} and __session-{session_id} into DataImpulse proxy URL."""
    match = re.match(r"^(https?://)([^:]+):([^@]+)@([^:]+:\d+)$", base_url)
    if not match:
        return base_url

    scheme, user, pwd, hostport = match.groups()
    clean_user = re.split(r"__(cr|country|session)", user)[0]
    addons = []
    if geo:
        addons.append(f"__cr-{geo.lower()}")
    if session_id:
        addons.append(f"__session-{session_id}")

    return f"{scheme}{clean_user}{''.join(addons)}:{pwd}@{hostport}"


def _resolve_from_cache(geo: Optional[str] = None, session_id: Optional[str] = None) -> Optional[str]:
    """Attempt reading cached DataImpulse credentials from ultra-low-cost-scraper cache."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        login = data.get("login")
        password = data.get("password")
        hostname = data.get("hostname", "gw.dataimpulse.com")
        port = data.get("port", "823")
        if login and password:
            base_url = f"http://{login}:{password}@{hostname}:{port}"
            return _format_dataimpulse_url(base_url, geo=geo, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to read proxy cache at %s: %s", CACHE_FILE, exc)
    return None


def _resolve_from_env_credentials(
    geo: Optional[str] = None, session_id: Optional[str] = None,
) -> Optional[str]:
    """Attempt resolution from SCRAPER_PROXY_URL / DATAIMPULSE env vars."""
    url = os.getenv("SCRAPER_PROXY_URL") or os.getenv("DATAIMPULSE_PROXY_URL")
    if url:
        return _format_dataimpulse_url(url.strip(), geo=geo, session_id=session_id)

    login = os.getenv("DATAIMPULSE_LOGIN")
    password = os.getenv("DATAIMPULSE_PASSWORD")
    if login and password:
        host = os.getenv("DATAIMPULSE_HOST", "gw.dataimpulse.com")
        port = os.getenv("DATAIMPULSE_PORT", "823")
        base_url = f"http://{login}:{password}@{host}:{port}"
        return _format_dataimpulse_url(base_url, geo=geo, session_id=session_id)
    return None


def _resolve_from_resolver_script(
    geo: Optional[str] = None, session_id: Optional[str] = None,
) -> Optional[str]:
    """Attempt resolution via ultra-low-cost-scraper proxy_resolver.py."""
    candidates = [
        Path.home() / ".gemini" / "antigravity" / "skills" / "ultra-low-cost-scraper" / "scripts" / "proxy_resolver.py",
        Path.home() / ".agents" / "skills" / "ultra-low-cost-scraper" / "scripts" / "proxy_resolver.py",
    ]
    script = next((p for p in candidates if p.exists()), None)
    if not script:
        return None

    cmd = [sys.executable, str(script), "--format", "url"]
    if geo:
        cmd.extend(["--geo", geo])
    if session_id:
        cmd.extend(["--session", session_id])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed running proxy resolver %s: %s", script, exc)
    return None


def resolve_proxy(
    proxy_arg: Optional[str] = None,
    geo: Optional[str] = DEFAULT_GEO,
    auto: bool = False,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve an effective proxy URL based on arguments and environment.

    Args:
        proxy_arg: Explicit URL, 'auto', 'direct', 'none', or None.
        geo: Target country code (default 'vn' for Chợ Tốt Vietnam).
        auto: If True, allow auto-resolving residential proxy when none is given.
        session_id: Optional sticky session ID for continuous crawls.

    Returns:
        Fully-qualified proxy URL (e.g. 'http://user__cr-vn:pass@gw.dataimpulse.com:823') or None.
    """
    if proxy_arg:
        val = proxy_arg.strip()
        if val.lower() in ("none", "direct"):
            return None
        if val.lower() != "auto":
            # Format if it's DataImpulse
            if "dataimpulse.com" in val:
                return _format_dataimpulse_url(val, geo=geo, session_id=session_id)
            return val

    # If auto resolution was requested or CHOTOT_AUTO_PROXY is enabled
    auto_enabled = (
        auto
        or (proxy_arg and proxy_arg.strip().lower() == "auto")
        or os.getenv("CHOTOT_AUTO_PROXY", "").lower() in ("1", "true", "yes")
    )

    if auto_enabled:
        # 1. Environment proxy URL with credentials
        res = _resolve_from_env_credentials(geo=geo, session_id=session_id)
        if res:
            return res

        # 2. Disk cache from ultra-low-cost-scraper
        res = _resolve_from_cache(geo=geo, session_id=session_id)
        if res:
            return res

        # 3. Dynamic resolution via 1Password bridge in proxy_resolver.py
        res = _resolve_from_resolver_script(geo=geo, session_id=session_id)
        if res:
            return res

    # Fall back to standard environment proxy (if any)
    return get_env_proxy()
