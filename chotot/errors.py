# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Exception hierarchy for chotot-cli.

Every error carries a *remedy* the user can act on. Two rules govern the text,
both learned the hard way:

1. A message raised from shared code says only what every caller shares. If we
   cannot name the operation that failed, we do not name one.
2. If a remedy names a command, that command must parse. ``tests/test_errors.py``
   feeds every remedy string to the real argument parser.
"""
from __future__ import annotations

from typing import Optional


class ChototError(Exception):
    """Base class. ``remedy`` is printed under the message when present."""

    exit_code = 1

    def __init__(self, message: str, remedy: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self) -> str:
        return self.message


class UsageError(ChototError):
    """The request cannot be formed from what the user asked for."""

    exit_code = 2


class UnsupportedFilterError(UsageError):
    """A filter the upstream API accepts but silently ignores.

    Refusing beats forwarding: the gateway returns HTTP 200 and an unfiltered
    result set, so a forwarded filter produces a confident wrong answer rather
    than an error.
    """

    exit_code = 2


class ResolutionError(UsageError):
    """A category/region/district name could not be resolved to a code."""

    exit_code = 2


class NotFoundError(ChototError):
    """The listing or seller does not exist (or has expired)."""

    exit_code = 4


class TransportError(ChototError):
    """Network failure, timeout, or an unrecoverable HTTP status."""

    exit_code = 5


class RateLimitedError(TransportError):
    """Upstream asked us to slow down.

    ``retry_after`` is taken from the response header when the server states it.
    Reading the block's own header beats probing for the duration.
    """

    exit_code = 6

    def __init__(self, message: str, remedy: Optional[str] = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message, remedy)
        self.retry_after = retry_after


class UpstreamContractError(ChototError):
    """The gateway answered, but not in the shape this client was built for.

    Raised instead of silently coercing a changed payload into plausible zeros.
    """

    exit_code = 7
