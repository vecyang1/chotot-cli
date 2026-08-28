# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""chotot-cli - a command line client and market-analytics engine for Chợ Tốt.

Submodules are imported lazily. Eagerly importing the HTTP client here cost
every invocation -- including ``chotot --help`` and shell completion -- the price
of loading the whole stack.
"""
from __future__ import annotations

from typing import Any

__version__ = "2.1.0"
__all__ = ["__version__", "ChototClient", "MarketAnalyzer"]


def __getattr__(name: str) -> Any:  # PEP 562 lazy attribute access
    if name == "ChototClient":
        from chotot.client import ChototClient

        return ChototClient
    if name == "MarketAnalyzer":
        from chotot.analyzer import MarketAnalyzer

        return MarketAnalyzer
    raise AttributeError(f"module 'chotot' has no attribute {name!r}")
