# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Typed views over the gateway's JSON payloads.

One rule shapes every field below: **absent is not zero.** A missing rating is
``None``, never ``0.0``; a missing sold-count is ``None``, never ``0``. The
gateway omits keys freely, and ``.get(key) or 0`` would turn "we could not read
this" into "the seller has sold nothing" -- a specific, plausible, wrong fact
that a buyer would act on. Renderers print ``-`` for ``None`` and a real number
only when the API stated one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: ``elt_condition`` is an integer in search results, with these labels taken
#: from the ``ad_params`` of matching detail responses.
CONDITION_LABELS: Dict[int, str] = {
    1: "Mới (New)",
    2: "Đã sử dụng (Used)",
    3: "Cũ / cần sửa (Used - needs repair)",
}


def _opt_int(value: Any) -> Optional[int]:
    """Int, or None when absent/unparseable. Never a stand-in zero."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: Below this, an epoch value must be seconds; above it, milliseconds. The
#: boundary is year 33658 in seconds and year 1970-08 in milliseconds, so no
#: real listing is ambiguous.
_EPOCH_MILLIS_THRESHOLD = 1_000_000_000_000


def _epoch_to_datetime(value: Optional[int]) -> Optional[datetime]:
    """Interpret an epoch stamp in either unit. ``None`` when unusable."""
    if not value or value <= 0:
        return None
    seconds = value / 1000 if value >= _EPOCH_MILLIS_THRESHOLD else float(value)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class Seller:
    """Seller facts as carried on an ad. There is no public profile endpoint."""

    account_id: Optional[int] = None
    account_oid: Optional[str] = None
    name: Optional[str] = None
    avatar: Optional[str] = None
    sold_ads: Optional[int] = None
    total_rating: Optional[int] = None
    average_rating: Optional[float] = None
    is_shop: bool = False
    is_verified_shop: bool = False
    shop_alias: Optional[str] = None

    @classmethod
    def from_ad(cls, ad: Dict[str, Any]) -> "Seller":
        info = ad.get("seller_info")
        # The gateway has been seen to send null here; a changed shape must not
        # become an AttributeError three frames later.
        info = info if isinstance(info, dict) else {}
        return cls(
            account_id=_opt_int(ad.get("account_id")),
            account_oid=_opt_str(ad.get("account_oid")),
            name=_opt_str(info.get("full_name")) or _opt_str(ad.get("account_name")) or _opt_str(ad.get("full_name")),
            avatar=_opt_str(info.get("avatar")) or _opt_str(ad.get("avatar")),
            # sold_ads lives on seller_info for some ads and top-level for
            # others; absent from both means unknown, not zero sales.
            sold_ads=_opt_int(info.get("sold_ads") if info.get("sold_ads") is not None else ad.get("sold_ads")),
            total_rating=_opt_int(ad.get("total_rating_for_seller") if ad.get("total_rating_for_seller") is not None else ad.get("total_rating")),
            average_rating=_opt_float(ad.get("average_rating_for_seller") if ad.get("average_rating_for_seller") is not None else ad.get("average_rating")),
            is_shop=bool(ad.get("company_ad")),
            is_verified_shop=bool(ad.get("is_shop_verified")),
            shop_alias=_opt_str(ad.get("shop_alias")),
        )

    @property
    def profile_url(self) -> Optional[str]:
        if self.shop_alias:
            return f"https://www.chotot.com/shop/{self.shop_alias}"
        if self.account_oid:
            return f"https://www.chotot.com/user/{self.account_oid}"
        return None


@dataclass(frozen=True)
class Listing:
    """One marketplace ad.

    ``price`` is ``None`` when the ad states no price (Chợ Tốt allows "thỏa
    thuận"/negotiable and giveaway ads). Treating that as ``0`` would drag every
    computed average toward zero, so statistics exclude it explicitly.
    """

    list_id: int
    subject: str
    price: Optional[int]
    price_label: Optional[str]
    category_id: Optional[int]
    category_name: Optional[str]
    region_v2: Optional[int]
    region_name: Optional[str]
    region_name_modern: Optional[str]
    area_v2: Optional[int]
    area_name: Optional[str]
    ward_name: Optional[str]
    condition_code: Optional[int]
    posted_at: Optional[datetime]
    posted_label: Optional[str]
    seller: Seller
    image: Optional[str] = None
    images: List[str] = field(default_factory=list)
    body: Optional[str] = None
    is_company_ad: bool = False
    #: 's' sale, 'u' rent, 'k' wanted-to-buy, 'h' wanted-to-rent. Mixing sale
    #: and rent in one price statistic makes it meaningless, so this is kept.
    listing_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def url(self) -> str:
        return f"https://www.chotot.com/{self.list_id}.htm"

    @property
    def condition_label(self) -> Optional[str]:
        if self.condition_code is None:
            return None
        return CONDITION_LABELS.get(self.condition_code, f"Code {self.condition_code}")

    @property
    def has_price(self) -> bool:
        return self.price is not None and self.price > 0

    @property
    def listing_type_label(self) -> Optional[str]:
        from chotot.contract import LISTING_TYPES

        if self.listing_type is None:
            return None
        return LISTING_TYPES.get(self.listing_type, self.listing_type)

    @classmethod
    def from_ad(cls, ad: Dict[str, Any]) -> "Listing":
        from chotot.errors import UpstreamContractError

        if not isinstance(ad, dict):
            # Documented as exit 7. Letting a bare TypeError escape reported a
            # changed upstream shape as "unexpected error ... please report
            # this", which sends the user to file a bug against their own tool.
            raise UpstreamContractError(
                f"Expected an ad object, got {type(ad).__name__}.",
                remedy="Run 'chotot doctor' to re-check the gateway contract.",
            )
        list_id = _opt_int(ad.get("list_id"))
        if list_id is None:
            from chotot.errors import UpstreamContractError

            raise UpstreamContractError(
                "An ad in the response has no 'list_id'.",
                remedy="Run 'chotot doctor' to re-check the gateway contract.",
            )

        # list_time is epoch MILLISECONDS. 'date' is a RELATIVE Vietnamese
        # string ("2 giờ trước" = 2 hours ago) and is useless in an export read
        # a week later, so the absolute timestamp is derived here and the
        # relative text is preserved separately for display.
        # `list_time` is milliseconds on /ad-listing and SECONDS on the shop
        # endpoint. Assuming one unit dated every shop listing 1970-01-21, so the
        # magnitude decides: a seconds value for any plausible listing is ~1.7e9,
        # a milliseconds value ~1.7e12.
        posted_at = _epoch_to_datetime(_opt_int(ad.get("list_time")))

        raw_images = ad.get("images")
        images = [i for i in (raw_images if isinstance(raw_images, list) else []) if isinstance(i, str)]
        price = _opt_int(ad.get("price"))

        return cls(
            list_id=list_id,
            subject=_opt_str(ad.get("subject")) or "(no title)",
            price=price,
            price_label=_opt_str(ad.get("price_string")),
            category_id=_opt_int(ad.get("category")),
            category_name=_opt_str(ad.get("category_name")),
            region_v2=_opt_int(ad.get("region_v2")),
            region_name=_opt_str(ad.get("region_name")),
            region_name_modern=_opt_str(ad.get("region_name_v3")),
            area_v2=_opt_int(ad.get("area_v2")),
            area_name=_opt_str(ad.get("area_name")),
            ward_name=_opt_str(ad.get("ward_name_v3")) or _opt_str(ad.get("ward_name")),
            # In search results `params` is ALWAYS an empty list -- measured
            # across every sampled category. The condition therefore has to come
            # from the top-level `elt_condition` int. Reading it from `params`
            # (as an earlier revision did) yields None for every ad and silently
            # collapses the condition breakdown into one bucket.
            condition_code=_opt_int(ad.get("elt_condition")),
            posted_at=posted_at,
            posted_label=_opt_str(ad.get("date")),
            seller=Seller.from_ad(ad),
            image=_opt_str(ad.get("image")) or (images[0] if images else None),
            images=images,
            body=_opt_str(ad.get("body")),
            is_company_ad=bool(ad.get("company_ad")),
            listing_type=_opt_str(ad.get("type")),
            raw=ad,
        )

    def to_dict(self, include_body: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "list_id": self.list_id,
            "url": self.url,
            "subject": self.subject,
            "price_vnd": self.price,
            "price_label": self.price_label,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "region_v2": self.region_v2,
            "region_name": self.region_name,
            "region_name_modern": self.region_name_modern,
            "area_name": self.area_name,
            "ward_name": self.ward_name,
            "condition_code": self.condition_code,
            "condition_label": self.condition_label,
            "posted_at_utc": self.posted_at.isoformat() if self.posted_at else None,
            "posted_relative": self.posted_label,
            "seller_name": self.seller.name,
            "seller_account_id": self.seller.account_id,
            "seller_account_oid": self.seller.account_oid,
            # Named by the help text and by an error remedy, so it has to be
            # reachable from the output those point at.
            "seller_shop_alias": self.seller.shop_alias,
            "seller_sold_ads": self.seller.sold_ads,
            "seller_rating": self.seller.average_rating,
            "is_company_ad": self.is_company_ad,
            "listing_type": self.listing_type,
            "listing_type_label": self.listing_type_label,
            "image": self.image,
        }
        if include_body:
            data["body"] = self.body
        return data


@dataclass(frozen=True)
class ListingDetail:
    """A single listing plus its structured, human-labelled specifications."""

    listing: Listing
    specs: Dict[str, str] = field(default_factory=dict)
    detail_address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    phone_masked: Optional[str] = None
    state: Optional[str] = None
    status: Optional[str] = None
    videos: List[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "ListingDetail":
        from chotot.errors import UpstreamContractError

        ad = payload.get("ad")
        if not isinstance(ad, dict):
            raise UpstreamContractError(
                "Detail response has no 'ad' object.",
                remedy="Run 'chotot doctor' to re-check the gateway contract.",
            )

        # ad_params is a dict of {id: {id,label,value}} with Vietnamese labels.
        specs: Dict[str, str] = {}
        raw_params = payload.get("ad_params")
        if raw_params is not None and not isinstance(raw_params, dict):
            raise UpstreamContractError(
                f"'ad_params' should be an object, got {type(raw_params).__name__}.",
                remedy="Run 'chotot doctor' to re-check the gateway contract.",
            )
        for key, entry in (raw_params or {}).items():
            if isinstance(entry, dict):
                label = _opt_str(entry.get("label")) or key
                value = _opt_str(entry.get("value"))
                if value:
                    specs[label] = value
            else:
                value = _opt_str(entry)
                if value:
                    specs[key] = value

        videos = [
            v.get("url") for v in (ad.get("videos") or [])
            if isinstance(v, dict) and _opt_str(v.get("url"))
        ]

        return cls(
            listing=Listing.from_ad(ad),
            specs=specs,
            detail_address=_opt_str(ad.get("detail_address")) or specs.get("Địa chỉ"),
            latitude=_opt_float(ad.get("latitude")),
            longitude=_opt_float(ad.get("longitude")),
            # The gateway itself returns this already masked (e.g. '034492****').
            # We never attempt to unmask it.
            phone_masked=_opt_str(ad.get("phone")),
            state=_opt_str(ad.get("state")),
            status=_opt_str(ad.get("status")),
            videos=[v for v in videos if v],
        )

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.state in (None, "accepted")

    def to_dict(self) -> Dict[str, Any]:
        data = self.listing.to_dict(include_body=True)
        data.update({
            "specs": self.specs,
            "detail_address": self.detail_address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phone_masked": self.phone_masked,
            "state": self.state,
            "status": self.status,
            "is_active": self.is_active,
            "images": self.listing.images,
            "videos": self.videos,
        })
        return data
