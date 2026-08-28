# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Seller and shop lookups.

Two endpoints the search API does not expose:

* ``theia/{account_id_or_oid}`` — a seller's **complete** live inventory, with a
  real ``total``. Unlike ``ad-listing`` its ``limit`` is not clamped at 50, but
  ``page`` and ``o`` are accepted and ignored, so the only correct usage is to
  read the total from a cheap probe and then fetch it in a single request.
* ``shops/alias/{alias}`` — the full profile of a professional shop.

The shop profile carries an **unmasked** phone number, while the same seller's
number is masked on a listing. That asymmetry is upstream's, not ours; this
module redacts by default and only reveals on an explicit opt-in, so a routine
lookup cannot spill a contact into a terminal, a log, or an export.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from chotot import contract
from chotot.errors import NotFoundError, UsageError
from chotot.http import HttpTransport
from chotot.models import Listing, Seller, _opt_float, _opt_int, _opt_str

#: Probe size used to learn a storefront's true total before fetching it.
_PROBE_LIMIT = 1

#: Never fetch more than this in one storefront request, however large `total`.
_STOREFRONT_CEILING = 500

#: A seller key is a numeric account_id or a hex account_oid. Anything else is
#: refused BEFORE it reaches the URL: these values are interpolated into a path
#: segment, so an unvalidated key can add query parameters or walk to a
#: different endpoint entirely.
_SELLER_KEY = re.compile(r"\A(?:\d{1,20}|[0-9a-fA-F]{24,64})\Z")

#: Shop aliases observed are short alphanumeric tokens.
_SHOP_ALIAS = re.compile(r"\A[A-Za-z0-9_-]{3,64}\Z")

#: theia states only a Vietnamese relative age ("2 giờ trước"), never
#: `list_time`. Parsing it gives every storefront row a real timestamp, so
#: sorting, exports and the Age column work the same as they do elsewhere.
_RELATIVE_UNITS = {
    "giây": 1, "phút": 60, "giờ": 3600,
    "ngày": 86400, "tuần": 604800, "tháng": 2592000, "năm": 31536000,
}

#: theia and ad-listing describe the SAME field with different alphabets:
#: theia says 'sell'/'let' where ad-listing says 's'/'u'. Left unnormalised, the
#: mixed sale/rent guard silently never fires on storefront data -- a seller
#: with 9 sales and 21 rentals gets one meaningless median and no warning.
_STOREFRONT_TYPES = {
    "sell": "s", "let": "u", "buy": "k", "rent": "h",
    "s": "s", "u": "u", "k": "k", "h": "h",
}


#: How much of a phone number may be shown. The gateway's own listing endpoint
#: masks the tail (``034492****``), so revealing a suffix here would publish
#: MORE than upstream does, and the two together would reconstruct the number.
_PHONE_PREFIX_KEPT = 4


def _parse_relative_age(text: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """Turn "2 giờ trước" into an absolute UTC time. ``None`` when unparseable.

    Approximate by construction -- the source is approximate -- but a real
    timestamp an exporter can sort beats an untranslated string.
    """
    if not text:
        return None
    match = re.match(r"\s*(\d+)\s+(\S+)", str(text))
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    seconds = _RELATIVE_UNITS.get(unit)
    if not seconds:
        return None
    reference = now or datetime.now(timezone.utc)
    return reference - timedelta(seconds=amount * seconds)


def _redact_phone(value: Optional[str]) -> Optional[str]:
    """Mask a phone number, never revealing a digit upstream withheld.

    Keeps only a short leading carrier prefix and stars the rest -- a strict
    subset of what ``/ad-listing/{id}`` already publishes. The earlier form kept
    the last three digits as well, which left 7 of 10 digits visible under a
    ``phones_redacted: true`` flag; combined with the masked listing number it
    reconstructed the whole thing.
    """
    if not value:
        return None
    text = str(value).strip()
    if len(text) <= _PHONE_PREFIX_KEPT:
        return "*" * len(text)
    return f"{text[:_PHONE_PREFIX_KEPT]}{'*' * (len(text) - _PHONE_PREFIX_KEPT)}"


@dataclass
class Storefront:
    """A seller's complete live inventory."""

    account_id: Optional[int]
    account_oid: Optional[str]
    name: Optional[str]
    total: Optional[int]
    listings: List[Listing] = field(default_factory=list)
    shop_alias: Optional[str] = None
    #: Reputation is absent from the storefront payload but present on search
    #: results, so it is fetched separately rather than reported as unknown.
    reputation: Optional[Seller] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_oid": self.account_oid,
            "name": self.name,
            "shop_alias": self.shop_alias,
            "live_listings_reported": self.total,
            "live_listings_fetched": len(self.listings),
            "is_shop": self.reputation.is_shop if self.reputation else None,
            "is_verified_shop": self.reputation.is_verified_shop if self.reputation else None,
            "sold_ads": self.reputation.sold_ads if self.reputation else None,
            "average_rating": self.reputation.average_rating if self.reputation else None,
            "total_rating": self.reputation.total_rating if self.reputation else None,
            "listings": [x.to_dict() for x in self.listings],
            "warnings": self.warnings,
        }


@dataclass
class ShopProfile:
    """A professional shop's public profile."""

    alias: str
    name: Optional[str]
    is_verified: bool
    created_date: Optional[str]
    address: Optional[str]
    description: Optional[str]
    category_id: Optional[int]
    account_oid: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    phones: List[str] = field(default_factory=list)
    listings: List[Listing] = field(default_factory=list)
    total_listings: Optional[int] = None

    def to_dict(self, reveal_contact: bool = False) -> Dict[str, Any]:
        """Serialise the profile.

        Args:
            reveal_contact: Include phone numbers verbatim. Off by default --
                upstream masks the same number on a listing, so revealing it
                here should be a deliberate act, not a side effect of a lookup.
        """
        phones = self.phones if reveal_contact else [_redact_phone(p) for p in self.phones]
        return {
            "alias": self.alias,
            "name": self.name,
            "is_verified": self.is_verified,
            "created_date": self.created_date,
            "address": self.address,
            "description": self.description,
            "category_id": self.category_id,
            "account_oid": self.account_oid,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phones": phones,
            "phones_redacted": not reveal_contact,
            "total_listings": self.total_listings,
            "listings": [x.to_dict() for x in self.listings],
        }


def _storefront_ad_to_listing(entry: Dict[str, Any]) -> Optional[Listing]:
    """Normalise a theia row into the shape the rest of the tool speaks.

    theia nests the ad under ``info`` and returns ``price`` as a STRING, where
    ``ad-listing`` returns an integer. Passing that straight through would make
    every storefront price unusable for arithmetic.
    """
    info = entry.get("info") if isinstance(entry.get("info"), dict) else entry
    if not isinstance(info, dict) or not info.get("list_id"):
        return None
    # theia returns `price` as a STRING where ad-listing returns an integer.
    # Listing.from_ad parses it through _opt_int, so no conversion is needed
    # here; tests/test_client.py asserts the parsed price really is an int.
    normalised = dict(info)

    raw_type = _opt_str(info.get("type"))
    if raw_type:
        normalised["type"] = _STOREFRONT_TYPES.get(raw_type.lower(), raw_type)

    if "subject" not in normalised and info.get("name"):
        normalised["subject"] = info["name"]

    # theia reports only the pre-2025-merger region_name and omits
    # region_name_v3, so the modern province is resolved from region_v2 here.
    # Without this a storefront row shows a district with no province.
    region_v2 = _opt_int(info.get("region_v2"))
    if region_v2 is not None and not normalised.get("region_name_v3"):
        from chotot.taxonomy import province_name, provinces

        if str(region_v2) in provinces():
            normalised["region_name_v3"] = province_name(region_v2)
    # theia populates `params` (search results never do); lift the condition so
    # the shared model finds it where it expects.
    for param in info.get("params") or []:
        if isinstance(param, dict) and param.get("id") == "elt_condition":
            normalised.setdefault("elt_condition", _opt_int(param.get("value")))
    try:
        listing = Listing.from_ad(normalised)
    except Exception:  # noqa: BLE001 - one malformed row must not kill the page
        return None
    if listing.posted_at is None and listing.posted_label:
        approximate = _parse_relative_age(listing.posted_label)
        if approximate is not None:
            listing = replace(listing, posted_at=approximate)
    return listing


class SellerClient:
    """Reads the storefront and shop endpoints."""

    def __init__(self, transport: HttpTransport) -> None:
        self.transport = transport

    def storefront(self, account: Any, limit: Optional[int] = None) -> Storefront:
        """Fetch a seller's live inventory.

        Accepts the numeric ``account_id`` or the 32-character ``account_oid``.
        """
        key = str(account).strip()
        if not _SELLER_KEY.match(key):
            raise UsageError(
                f"{account!r} is not a valid seller id.",
                remedy="Pass the numeric account_id or the 32-character account_oid. "
                       "Both appear in 'chotot detail <id> --json'.",
            )
        path = contract.ENDPOINTS["storefront"].path.format(
            account=urllib.parse.quote(key, safe=""))
        warnings: List[str] = []

        probe = self.transport.get_json(path, {"limit": _PROBE_LIMIT})
        total = _opt_int(probe.get("total"))
        if not total:
            raise NotFoundError(
                f"Seller {key} has no live listings.",
                remedy="The account may exist with nothing posted. Chợ Tốt "
                       "publishes no separate profile endpoint, so an empty "
                       "storefront and a missing account look the same.",
            )

        if limit is not None and limit < 1:
            raise UsageError(f"--limit must be at least 1, got {limit}.",
                             remedy="Omit --limit to fetch the whole storefront.")
        # `limit or total` read an explicit 0 as "unset" and fetched everything.
        want = min(total, total if limit is None else limit, _STOREFRONT_CEILING)
        if want < total:
            warnings.append(
                f"Fetched {want} of {total} live listings "
                f"(storefront pagination does not work, so this is one request)."
            )
        # `page` and `o` are accepted and ignored here, so a second request would
        # return the same rows. One request with a large limit is the only way.
        payload = self.transport.get_json(path, {"limit": want})

        listings: List[Listing] = []
        for entry in payload.get("ads") or []:
            listing = _storefront_ad_to_listing(entry)
            if listing is not None:
                listings.append(listing)
        dropped = len(payload.get("ads") or []) - len(listings)
        if dropped:
            warnings.append(f"{dropped} storefront row(s) could not be parsed and were skipped.")

        first = (payload.get("ads") or [{}])[0]
        info = first.get("info") if isinstance(first.get("info"), dict) else first

        # theia's rows carry no rating/sold_ads fields; ad-listing's do. One
        # extra request beats reporting a rated seller as unrated.
        reputation: Optional[Seller] = None
        numeric_id = _opt_int(info.get("account_id"))
        if numeric_id:
            try:
                sample = self.transport.get_json(
                    contract.ENDPOINTS["search"].path,
                    {"account_id": numeric_id, "limit": 1})
                rows = sample.get("ads") or []
                if rows:
                    reputation = Seller.from_ad(rows[0])
            except Exception:  # noqa: BLE001 - enrichment, never fatal
                warnings.append("Could not fetch seller reputation; ratings shown as unknown.")

        # theia echoes back whatever key it was called with as `account_oid`,
        # so a numeric lookup returns the numeric id in that field. Only accept
        # a value that actually looks like the 32-character hash.
        raw_oid = _opt_str(info.get("account_oid"))
        account_oid = raw_oid if (raw_oid and len(raw_oid) >= 24 and not raw_oid.isdigit()) else None

        return Storefront(
            account_id=_opt_int(info.get("account_id")),
            account_oid=account_oid,
            name=_opt_str(info.get("name")) or _opt_str(info.get("account_name")),
            total=total,
            listings=listings,
            shop_alias=_opt_str(info.get("shop_alias")) or (
                reputation.shop_alias if reputation else None),
            reputation=reputation,
            warnings=warnings,
        )

    def shop(self, alias: str) -> ShopProfile:
        """Fetch a professional shop's profile by its ``shop_alias``."""
        clean = str(alias).strip()
        if not _SHOP_ALIAS.match(clean):
            raise UsageError(
                f"{alias!r} is not a valid shop alias.",
                remedy="Find it in 'chotot detail <id> --json' as seller_shop_alias.",
            )
        payload = self.transport.get_json(
            contract.ENDPOINTS["shop"].path.format(
                alias=urllib.parse.quote(clean, safe="")))

        # A 200 that is not shop-shaped is not a shop. Without this an
        # unexpected payload becomes a fabricated all-null profile reported as
        # a success.
        if not (payload.get("name") or payload.get("alias") or payload.get("accountOid")):
            raise NotFoundError(
                f"No shop found for alias {clean!r}.",
                remedy="Check the alias in 'chotot detail <id> --json'.",
            )

        phones = [
            str(payload[key]) for key in ("phoneNumber", "additionalPhone1", "additionalPhone2")
            if _opt_str(payload.get(key))
        ]
        embedded = payload.get("shopAds") or {}
        listings: List[Listing] = []
        for entry in embedded.get("ads") or []:
            listing = _storefront_ad_to_listing(entry)
            if listing is not None:
                listings.append(listing)

        return ShopProfile(
            alias=clean,
            name=_opt_str(payload.get("name")),
            is_verified=bool(payload.get("isVerified")),
            created_date=_opt_str(payload.get("createdDate")),
            address=_opt_str(payload.get("address")),
            description=_opt_str(payload.get("description")),
            category_id=_opt_int(payload.get("categoryId")),
            account_oid=(payload.get("accountOid") or [None])[0]
                        if isinstance(payload.get("accountOid"), list)
                        else _opt_str(payload.get("accountOid")),
            latitude=_opt_float(payload.get("latitude")),
            longitude=_opt_float(payload.get("longitude")),
            phones=phones,
            listings=listings,
            total_listings=_opt_int(embedded.get("total")),
        )
