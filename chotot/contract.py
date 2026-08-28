# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""The measured contract of the Chợ Tốt public gateway.

This module is the single place that records *what the upstream API actually
does*, as opposed to what its parameter names suggest. Every entry below was
established by differential probing on 2026-08-28: a parameter counts as
supported only if changing its value changes the result set in the direction its
name claims.

Why this exists as data rather than prose: the gateway answers ``HTTP 200`` for
parameters it ignores. ``sp=50000000`` (which reads like a 50M minimum price)
returns ₫250,000 listings. A client that forwards such a parameter reports a
filtered result set that was never filtered, and the user believes it. Keeping
the ignore-list executable means the client can *refuse* instead of lying, and
``tests/test_contract_live.py`` can re-measure every claim on demand.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, NamedTuple

GATEWAY_BASE_URL = "https://gateway.chotot.com/v1/public"

#: The gateway clamps ``limit`` here. Requesting 200 returns 50, silently.
MAX_PAGE_SIZE = 50

#: ``total`` saturates at this value. Above it the field states the cap, not a
#: count, so it must be reported as a floor (">= 10000") and never as an exact
#: number. Verified: a bare query and ``q=iphone`` both report exactly 10000
#: while ``q=honda sh`` reports a genuine 3095.
TOTAL_CAP = 10_000

#: Elasticsearch ``max_result_window``. The ceiling is on the WINDOW, not the
#: offset: ``o + limit <= 20000`` succeeds and 20001 returns HTTP 400
#: ``"invalid input - reach max search window size"``. Verified at two different
#: page sizes, so it is not an artefact of ``limit``.
#:
#: Note this is NOT ``TOTAL_CAP``. Results keep coming well past the point where
#: ``total`` saturates at 10000, so the display cap must never be used as the
#: crawl bound.
MAX_SEARCH_WINDOW = 20_000


class Endpoint(NamedTuple):
    path: str
    description: str


ENDPOINTS: Dict[str, Endpoint] = {
    "search": Endpoint("ad-listing", "Search/browse listings"),
    "detail": Endpoint("ad-listing/{list_id}", "One listing with ad_params"),
    "categories": Endpoint("chapy-pro/categories", "Category tree"),
    "regions": Endpoint("chapy-pro/regions", "Macro regions with their areas"),
    "facets": Endpoint("chapy-pro/ad-params", "Per-category parameter definitions (needs ?cg=)"),
    "storefront": Endpoint("theia/{account}", "One seller's complete live inventory"),
    "shop": Endpoint("shops/alias/{alias}", "Full shop profile for a professional seller"),
}

#: ``theia`` takes either the numeric ``account_id`` or the 32-char
#: ``account_oid``. Its ``limit`` is NOT clamped at 50, but ``page`` and ``o``
#: are both accepted and ignored -- ``?o=20`` returns the same 20 rows -- and
#: ``paging.totalPage`` advertises pages that cannot be fetched. The only
#: correct usage is to read ``total`` from a cheap ``limit=1`` call and then
#: re-request once with ``limit >= total``.
STOREFRONT_PAGINATION_WORKS = False

#: Probed and confirmed absent (HTTP 404). Recorded so that a future reader sees
#: a measured absence instead of re-deriving it, and so no command is designed
#: around an endpoint that was never there.
#: Confirmed absent. The gateway is Kong, and it distinguishes "no route exists"
#: (``{"message":"no Route matched with those values"}``) from "route exists,
#: upstream said 404". Only the first is proof of absence; the entries here
#: returned 404 to a direct probe.
KNOWN_ABSENT_ENDPOINTS = (
    "profile/{account_id}",
    "user/{account_id}",
    "categories",
    "regions",
    "chotot-rating/public/rating/{account_id}",
    "user-listing",
)

#: Seller ratings need no endpoint: ``average_rating_for_seller``,
#: ``total_rating_for_seller`` and ``is_shop_verified`` are already carried on
#: search results (populated on roughly 44 of 50 sampled ads).
SELLER_RATING_IS_EMBEDDED_IN_SEARCH = True

#: ``shops/alias/{alias}`` returns an UNMASKED ``phoneNumber`` and
#: ``additionalPhone1/2``, while ``ad-listing/{id}`` masks the same seller's
#: number (``034492****``). The CLI must not print these by default.
SHOP_PROFILE_EXPOSES_UNMASKED_PHONE = True

#: Query parameters that measurably filter. Anything not listed here is either
#: ignored or an error; see below.
SUPPORTED_PARAMS: FrozenSet[str] = frozenset({
    "q",            # free text; 20/20 subject relevance on four sample queries
    "cg",           # category, hierarchical (cg=2000 yields 2010/2020/2060)
    "region_v2",    # province
    "area_v2",      # district
    "price",        # "MIN-MAX" range string, "MIN-" allowed
    "limit",        # clamped at MAX_PAGE_SIZE
    "o",            # offset (NOT "page")
    "account_id",   # that seller's listings
    "st",           # listing type: sale / rent / wanted -- see LISTING_TYPES
})

#: ``st`` selects the listing TYPE, and omitting it is not neutral. A property
#: browse (``cg=1000``) returns roughly 55% rentals mixed with 45% sales, so any
#: price statistic over the unfiltered set averages a monthly rent against a
#: purchase price. Verified: ``st=s`` -> 40/40 sale, ``st=u`` -> 40/40 rent.
#:
#: A comma list does NOT union: ``st=s,k`` returned only ``s``.
LISTING_TYPES = {
    "s": "Cần bán (for sale)",
    "u": "Cho thuê (for rent)",
    "k": "Cần mua (wanted to buy)",
    "h": "Cần thuê (wanted to rent)",
}

#: Category-scoped facets. Confirmed working for phones (cg=5010); other
#: categories expose their own and are resolved at runtime, never guessed.
SUPPORTED_FACET_PARAMS: FrozenSet[str] = frozenset({
    "mobile_brand",
    "mobile_capacity",
})

#: Accepted with HTTP 200 and then IGNORED. Sending any of these produces an
#: unfiltered result set presented as a filtered one. The value is the reason
#: shown to the user when they ask for the capability it appears to offer.
IGNORED_PARAMS: Dict[str, str] = {
    "sp": "not a minimum price - it is a listing-type facet (sp=1 returns ₫0 ads)",
    "ep": "not a maximum price - ignored entirely",
    "minprice": "ignored; use the 'price' range parameter",
    "maxprice": "ignored; use the 'price' range parameter",
    "price_from": "ignored; use the 'price' range parameter",
    "price_to": "ignored; use the 'price' range parameter",
    "fromprice": "ignored; use the 'price' range parameter",
    "toprice": "ignored; use the 'price' range parameter",
    "pf": "ignored; use the 'price' range parameter",
    "pt": "ignored; use the 'price' range parameter",
    "condition": "ignored by the gateway - condition can only be filtered client-side",
    "elt_condition": "ignored as a query parameter (it is a response field only)",
    "company_ad": "ignored - seller type can only be filtered client-side",
    "account_oid": "ignored - use account_id (the numeric id) instead",
    "seller_id": "ignored - use account_id",
    "uid": "ignored - use account_id",
    "owner": "ignored - use account_id",
    "page": "ignored - pagination uses the 'o' offset parameter",
    "sort": "ignored when numeric, and HTTP 400 when combined with 'direction' - "
            "there is no server-side sort",
}

#: Parameter combinations that make the gateway answer HTTP 400.
#:
#: ``direction`` is deliberately NOT here. One name, two meanings: paired with
#: ``sort`` it is the sort modifier and 400s, but on property categories it is a
#: verified facet ("Hướng cửa chính", main-door direction) that filters
#: correctly -- ``cg=1010&direction=1`` drops the total from 10000 to 334.
#: Blanket-refusing the name made the CLI advertise that facet and then refuse
#: it. The no-server-side-sort fact belongs to ``sort``, which is where the 400
#: actually originates.
ERRORING_PARAMS: Dict[str, str] = {}

#: There is no working server-side sort on this gateway: ``sort=price`` with
#: ``direction=asc`` returns HTTP 400, and numeric ``sort`` values are ignored.
#: Ordering is therefore applied client-side over a deduplicated pool, and every
#: sorted result is labelled with the pool size so nobody reads it as a global
#: ranking of the whole marketplace.
SERVER_SIDE_SORT_AVAILABLE = False

#: Full-text ``q`` is a UNION of its terms, not an intersection. Adding a word
#: therefore WIDENS the result set, which is the opposite of what every search
#: box teaches a user to expect, and the least specific word dominates the
#: ranking. Measured 2026-08-28, region_v2=3017:
#:
#:     q="canon"              -> total 281  (cameras)
#:     q="v1"                 -> total  39  (Honda Winner V1 motorbikes)
#:     q="canon powershot"    -> total   1  (a real Canon PowerShot)
#:     q="canon powershot v1" -> total  40  ==  39 + 1
#:
#: The single relevant ad ranked 23rd of 40. `analyze "canon powershot v1"
#: --region "da nang"` consequently reported a median of 17,500,000d -- a
#: confident, plausible figure describing motorbikes, for a camera with zero
#: listings in that city.
#:
#: A nonsense token collapses the whole query to nothing (q="zzzqqqxxx v1"
#: returns no ads at all while q="v1" returns 39), so the union is over terms
#: the index recognises rather than a plain OR.
#:
#: There is no server-side way to request an intersection. The client therefore
#: recomputes it from the ad text the gateway itself returned, warns
#: unconditionally when the returned rows do not all carry every term, and
#: offers --match-all to keep only those that do.
QUERY_TERMS_ARE_UNIONED_NOT_INTERSECTED = True

#: `total` is OMITTED, not zero, when a query matches nothing: an empty result
#: body is `{"ads": []}` with no `total` key at all. Reading it as `data.get(
#: "total") or 0` would be right by accident here and wrong the moment the key
#: moves, so absence is carried as None and rendered as unknown.
TOTAL_KEY_ABSENT_WHEN_NO_MATCHES = True

#: Overlapping offset windows return rows already seen. Measured twice: 8.4%
#: over 5 sequential pages, and 7.2% over 8 pages fetched CONCURRENTLY in 0.75s.
#:
#: The mechanism is worth stating precisely, because the obvious explanation is
#: wrong. Ranking is **deterministic**: the same offset fetched twice returns
#: byte-identical ids in identical order. The duplication is structural
#: page-boundary bleed -- Elasticsearch ``from``/``size`` across shards with no
#: tiebreaker, so equally-ranked documents surface on more than one page.
#:
#: The operational consequence is the part that matters: crawling faster does
#: NOT reduce duplicates, because they are not a time-drift artefact.
#: Deduplication by ``list_id`` is mandatory at any speed.
RANKING_IS_DETERMINISTIC_AT_A_GIVEN_OFFSET = True
PAGES_OVERLAP_AT_BOUNDARIES = True
MEASURED_DUPLICATE_RATE = 0.084

#: No 429, no 503 and no ``RateLimit-*``/``Retry-After`` headers were observed up
#: to 8 concurrent requests; the gateway degrades by queueing (190ms solo ->
#: 750ms at concurrency 8) rather than rejecting. There is therefore no quota to
#: read and the client must self-limit. Escalation beyond 8 was deliberately not
#: attempted against a live production service, so the throttle point is
#: unmeasured rather than absent.
RATE_LIMIT_HEADERS_AVAILABLE = False
MEASURED_SAFE_CONCURRENCY = 8

CONTRACT_MEASURED_AT = "2026-08-28"


def describe_ignored(param: str) -> str:
    """Return the user-facing reason a parameter cannot be forwarded."""
    if param in IGNORED_PARAMS:
        return IGNORED_PARAMS[param]
    if param in ERRORING_PARAMS:
        return ERRORING_PARAMS[param]
    return "not supported by the Chợ Tốt gateway"


def assert_forwardable(params: Dict[str, object]) -> None:
    """Guard the outgoing query string.

    Raises :class:`~chotot.errors.UnsupportedFilterError` if a caller tries to
    forward a parameter the gateway is known to ignore. This is a programming
    error rather than user input -- the CLI translates user intent into
    client-side filtering long before this point -- so the message names the
    parameter and the measured reason.
    """
    from chotot.errors import UnsupportedFilterError

    for name in params:
        if name in IGNORED_PARAMS or name in ERRORING_PARAMS:
            raise UnsupportedFilterError(
                f"Refusing to send '{name}': {describe_ignored(name)}.",
                remedy=(
                    "The gateway would answer HTTP 200 with an unfiltered result "
                    "set, which would be reported as filtered. Apply this "
                    "constraint client-side instead."
                ),
            )
