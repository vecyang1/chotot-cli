# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""The Chợ Tốt client.

Everything the gateway genuinely supports is pushed server-side; everything it
only *appears* to support is applied here, in the open, and labelled in the
result. The distinction matters because the gateway answers HTTP 200 for
parameters it ignores -- forwarding ``condition=1`` yields an unfiltered page
that a caller would report as filtered.

Three invariants hold for every method that returns listings:

* **Deduplicated.** Ranking is unstable between requests, so overlapping offset
  windows repeat rows (8.4% over five measured pages). Results are keyed by
  ``list_id``.
* **Honest about totals.** ``total`` saturates at 10000, so the result reports a
  floor rather than a count when it is at the cap.
* **Self-describing.** Every result carries ``warnings`` and a ``coverage``
  block saying how many requests were spent, how many duplicates were dropped,
  and which filters were applied client-side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from chotot import contract
from chotot.errors import NotFoundError, ResolutionError, UsageError
from chotot.http import HttpTransport
from chotot.models import Listing, ListingDetail

if False:  # typing-only, avoids importing seller at module load
    from chotot.seller import ShopProfile, Storefront
from chotot.taxonomy import (
    category_name,
    district_name,
    normalise,
    province_codes,
    province_name,
    resolve_category,
    resolve_district,
    resolve_province,
)

logger = logging.getLogger("chotot.client")

SORT_CHOICES = ("relevance", "price_asc", "price_desc", "newest", "oldest")

#: Terms shorter than this match nearly every ad, so they cannot tell a
#: relevant result from an unrelated one and are excluded from the guard.
MIN_QUERY_TERM_LEN = 2


def query_terms(query: Optional[str]) -> List[str]:
    """The distinct words a user typed, folded for Vietnamese matching."""
    if not query:
        return []
    seen: List[str] = []
    for term in normalise(query).split():
        if len(term) >= MIN_QUERY_TERM_LEN and term not in seen:
            seen.append(term)
    return seen


def covers_all_terms(listing: Listing, terms: Sequence[str]) -> bool:
    """True when the ad's own text contains every term the user typed.

    The gateway's full-text search is a UNION, not an intersection: measured
    2026-08-28, `q=canon powershot v1` returned 40 ads in region 3017, which is
    exactly the 39 ads matching `v1` plus the 1 matching `canon powershot`.
    Adding a word therefore WIDENS the result set, and the ads that match only
    the least specific word outrank the one the user actually wanted. There is
    no server-side way to ask for an intersection, so it is recomputed here
    from the text the gateway itself returned.
    """
    if not terms:
        return True
    haystack = normalise(f"{listing.subject or ''} {listing.body or ''}")
    return all(term in haystack for term in terms)
SELLER_TYPES = ("any", "individual", "shop")

#: User-facing names for the gateway's single-letter ``st`` codes.
LISTING_TYPE_CHOICES = {
    "any": None, "sale": "s", "rent": "u",
    "wanted-buy": "k", "wanted-rent": "h",
}
CONDITIONS = ("any", "new", "used")

#: ``elt_condition`` codes grouped for the user-facing ``--condition`` flag.
_CONDITION_CODES: Dict[str, Tuple[int, ...]] = {"new": (1,), "used": (2, 3)}


@dataclass
class Coverage:
    """How a result set was actually obtained.

    Printed with every non-trivial query so a number is never read as more
    authoritative than the sampling behind it.
    """

    requests: int = 0
    fetched: int = 0
    duplicates_dropped: int = 0
    filtered_out: int = 0
    returned: int = 0
    reported_total: Optional[int] = None
    total_is_capped: bool = False
    #: Why the total is a floor: "upstream_cap" (the gateway saturated at
    #: 10,000) or "regions_skipped" (the budget ran out first). Collapsing the
    #: two into one boolean made the summary blame the gateway for a number
    #: nowhere near its cap.
    total_floor_reason: Optional[str] = None
    region_codes: List[int] = field(default_factory=list)
    client_side_filters: List[str] = field(default_factory=list)
    exhausted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "fetched": self.fetched,
            "duplicates_dropped": self.duplicates_dropped,
            "filtered_out_client_side": self.filtered_out,
            "returned": self.returned,
            "upstream_total": self.reported_total,
            "upstream_total_is_a_floor": self.total_is_capped,
            "upstream_total_floor_reason": self.total_floor_reason,
            "region_codes_queried": self.region_codes,
            "client_side_filters": self.client_side_filters,
            "pool_exhausted": self.exhausted,
        }


@dataclass
class SearchResult:
    listings: List[Listing]
    coverage: Coverage
    warnings: List[str] = field(default_factory=list)
    query: Optional[str] = None
    category_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "category": category_name(self.category_id) if self.category_id else None,
            "count": len(self.listings),
            "coverage": self.coverage.to_dict(),
            "warnings": self.warnings,
            "listings": [listing.to_dict() for listing in self.listings],
        }


class ChototClient:
    """High-level access to the Chợ Tốt public gateway."""

    def __init__(
        self,
        transport: Optional[HttpTransport] = None,
        base_url: str = contract.GATEWAY_BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 0.2,
    ) -> None:
        self.transport = transport or HttpTransport(
            base_url, timeout=timeout, max_retries=max_retries, min_interval=min_interval,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _price_param(min_price: Optional[int], max_price: Optional[int]) -> Optional[str]:
        """Build the ``price`` range string.

        The gateway takes ``price=MIN-MAX`` (``MIN-`` for an open top). It does
        NOT take sp/ep, min_price/max_price or any of the other spellings that
        look right -- those return HTTP 200 and an unfiltered page.
        """
        if min_price is None and max_price is None:
            return None
        if min_price is not None and min_price < 0:
            raise UsageError("--min-price cannot be negative.",
                             remedy="Prices are in VND, e.g. --min-price 1000000")
        if max_price is not None and max_price < 0:
            raise UsageError("--max-price cannot be negative.",
                             remedy="Prices are in VND, e.g. --max-price 15000000")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise UsageError(
                f"--min-price ({min_price:,}) is above --max-price ({max_price:,}).",
                remedy="Swap the two values.",
            )
        low = int(min_price) if min_price is not None else 0
        return f"{low}-{int(max_price)}" if max_price is not None else f"{low}-"

    @staticmethod
    def _matches_condition(listing: Listing, condition: str) -> bool:
        if condition == "any":
            return True
        wanted = _CONDITION_CODES.get(condition)
        if not wanted:
            return True
        # Unknown condition is not evidence for either bucket. An ad whose
        # condition the API did not state is excluded from a condition-filtered
        # view rather than being guessed into one.
        return listing.condition_code in wanted

    @staticmethod
    def _matches_seller_type(listing: Listing, seller_type: str) -> bool:
        if seller_type == "any":
            return True
        return listing.is_company_ad if seller_type == "shop" else not listing.is_company_ad

    @staticmethod
    def _sort(listings: List[Listing], sort: str) -> List[Listing]:
        """Order client-side; the gateway has no working sort (see contract)."""
        if sort in ("relevance", ""):
            return listings
        if sort in ("price_asc", "price_desc"):
            # Priceless ads cannot be ranked by price. They go last in both
            # directions rather than being treated as ₫0, which would make every
            # "cheapest first" view open with negotiable ads.
            priced = [x for x in listings if x.has_price]
            unpriced = [x for x in listings if not x.has_price]
            priced.sort(key=lambda x: x.price or 0, reverse=(sort == "price_desc"))
            return priced + unpriced
        if sort in ("newest", "oldest"):
            dated = [x for x in listings if x.posted_at is not None]
            undated = [x for x in listings if x.posted_at is None]
            dated.sort(key=lambda x: x.posted_at, reverse=(sort == "newest"))  # type: ignore[arg-type]
            return dated + undated
        return listings

    # -- core --------------------------------------------------------------

    def _page(self, params: Dict[str, Any]) -> Tuple[List[Listing], Optional[int]]:
        contract.assert_forwardable(params)
        payload = self.transport.get_json(contract.ENDPOINTS["search"].path, params)
        ads = payload.get("ads") or []
        # `total` is ABSENT (not 0) when nothing matches, so a missing key means
        # "no result", while a present 10000 means "at least 10000".
        total = payload.get("total")
        return [Listing.from_ad(ad) for ad in ads if isinstance(ad, dict)], total

    def search(
        self,
        query: Optional[str] = None,
        category: Optional[Union[str, int]] = None,
        region: Optional[Union[str, int]] = None,
        district: Optional[Union[str, int]] = None,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        condition: str = "any",
        seller_type: str = "any",
        listing_type: str = "any",
        sort: str = "relevance",
        limit: int = 20,
        limit_flag: str = "--limit",
        account_id: Optional[int] = None,
        expand_region: bool = True,
        max_requests: int = 24,
        facets: Optional[Dict[str, Any]] = None,
        match_all: bool = False,
    ) -> SearchResult:
        """Search listings, deduplicated and honestly labelled.

        ``condition``, ``seller_type`` and ``sort`` are applied client-side --
        the gateway ignores all three. Because they narrow an already-fetched
        pool, the crawler over-fetches when one is active so the requested
        ``limit`` can still be met.

        Args:
            query: Free text.
            category: Name, slug, or numeric id.
            region: Province name/alias/code. Expanded to every legacy code of
                the modern province unless ``expand_region`` is False.
            district: District name or ``area_v2`` code.
            min_price, max_price: VND bounds, sent as the ``price`` range.
            condition: ``any`` | ``new`` | ``used`` (client-side).
            seller_type: ``any`` | ``individual`` | ``shop`` (client-side).
            sort: see :data:`SORT_CHOICES` (client-side).
            limit: How many listings to return.
            account_id: Restrict to one seller (server-side).
            max_requests: Hard ceiling on gateway calls for this search.
            facets: Extra category-specific params, validated against the
                contract before being forwarded.
            match_all: Keep only ads whose own text contains every word of
                ``query`` (client-side). The gateway unions its terms, so this
                is the only way to ask for an intersection.

        Returns:
            :class:`SearchResult` with listings, coverage, and warnings.
        """
        if sort not in SORT_CHOICES:
            raise UsageError(f"Unknown sort {sort!r}.",
                             remedy=f"Choose one of: {', '.join(SORT_CHOICES)}")
        if condition not in CONDITIONS:
            raise UsageError(f"Unknown condition {condition!r}.",
                             remedy=f"Choose one of: {', '.join(CONDITIONS)}")
        if seller_type not in SELLER_TYPES:
            raise UsageError(f"Unknown seller type {seller_type!r}.",
                             remedy=f"Choose one of: {', '.join(SELLER_TYPES)}")
        if listing_type not in LISTING_TYPE_CHOICES:
            raise UsageError(f"Unknown listing type {listing_type!r}.",
                             remedy=f"Choose one of: {', '.join(LISTING_TYPE_CHOICES)}")
        if query is not None and query.strip() == "" and query != "":
            raise UsageError(
                "The search query is only whitespace.",
                remedy="Pass keywords, or omit the query to browse a category.",
            )
        if limit < 1:
            # The caller's flag name, not this function's parameter name:
            # `analyze --samples 0` used to be told to fix `--limit`.
            raise UsageError(f"{limit_flag} must be at least 1.",
                             remedy=f"Try {limit_flag} 20")

        category_id = resolve_category(category)
        region_codes = province_codes(region, expand=expand_region) if region else []
        area_v2 = _resolve_district_in(district, region_codes) if district else None
        if area_v2 is not None:
            # The gateway ANDs region_v2 and area_v2, and a district belongs to
            # exactly one legacy region. Keeping the whole merger group sent
            # requests that were structurally guaranteed to return zero -- and
            # at a low --max-requests the budget went entirely to those, so
            # passing the correct --region was worse than omitting it.
            from chotot.taxonomy import districts as _districts

            owner = (_districts().get(str(area_v2)) or {}).get("region_v2")
            if owner is not None:
                region_codes = [owner]

        warnings: List[str] = []
        coverage = Coverage(region_codes=list(region_codes))

        client_filters = [
            name for name, active in (
                (f"condition={condition}", condition != "any"),
                (f"seller_type={seller_type}", seller_type != "any"),
                (f"sort={sort}", sort != "relevance"),
                ("match_all", match_all),
            ) if active
        ]
        # Computed once: the guard below runs on every search, filtered or not.
        terms = query_terms(query)

        def survives(listing: Listing) -> bool:
            """Every client-side filter, in one place.

            Both the crawl's budget accounting and the final result list call
            this. They must never be able to disagree about what a filter does.
            """
            return (
                self._matches_condition(listing, condition)
                and self._matches_seller_type(listing, seller_type)
                and (not match_all or covers_all_terms(listing, terms))
            )
        coverage.client_side_filters = client_filters


        if condition != "any":
            warnings.append(
                "Condition is filtered client-side: the gateway accepts a "
                "'condition' parameter and ignores it."
            )
        if seller_type != "any":
            warnings.append(
                "Seller type is filtered client-side: the gateway accepts "
                "'company_ad' and ignores it."
            )
        if sort not in ("relevance", ""):
            warnings.append(
                f"Sorted client-side over the {{pool}} listings fetched, not over "
                f"the whole marketplace - the gateway has no server-side sort."
            )

        price = self._price_param(min_price, max_price)
        base: Dict[str, Any] = {"limit": contract.MAX_PAGE_SIZE}
        if query:
            base["q"] = query.strip()
        if category_id is not None:
            base["cg"] = category_id
        if area_v2 is not None:
            base["area_v2"] = area_v2
        if price is not None:
            base["price"] = price
        if account_id is not None:
            base["account_id"] = int(account_id)
        st_code = LISTING_TYPE_CHOICES.get(listing_type)
        if st_code is not None:
            base["st"] = st_code
        # Facets are resolved and validated by chotot.facets against the
        # probe-verified snapshot before they reach here; anything unverified
        # was already refused with the measured reason it does not filter.
        for name, value in (facets or {}).items():
            base[name] = value

        # Over-fetch when a client-side filter will discard part of the pool, so
        # `--limit 20 --condition new` still returns 20 where 20 exist.
        # With client-side filters the quota is measured in SURVIVING rows, so
        # `needed` is the real target rather than a guessed multiple.
        needed = limit

        # One query per legacy region code -- the gateway rejects a repeated
        # region_v2 with HTTP 400, so the fan-out happens here.
        #
        # Two things this must NOT do, both of which produce a confidently wrong
        # answer for a merged province:
        #
        #  * Concatenate per-region pages and truncate positionally. The codes
        #    sort ascending, so HCM proper (13000) comes LAST while Bà Rịa-Vũng
        #    Tàu (2010) comes first -- `--region hcm --limit 20` then returned 20
        #    ads from Vũng Tàu, 100km away, and none from Saigon.
        #  * Split the quota equally. HCM proper is >90% of the province's
        #    listings; giving it a third of the sample skews every statistic.
        #
        # So each region is collected into its own bucket and the result is
        # drawn proportionally to how many listings each region actually has.
        targets: Sequence[Optional[int]] = list(region_codes) if region_codes else [None]
        buckets: Dict[Optional[int], List[Listing]] = {t: [] for t in targets}
        #: Pages actually REQUESTED per region. The offset must be derived from
        #: this, never from the surviving row count: deduplication makes the
        #: bucket a lossy proxy for crawl depth, so a page bled by shard overlap
        #: leaves the counter short, the next offset repeats one already fetched,
        #: and the repeat -- adding nothing new -- is mistaken for exhaustion.
        pages: Dict[Optional[int], int] = {t: 0 for t in targets}
        region_totals: Dict[Optional[int], Optional[int]] = {t: None for t in targets}
        region_exhausted: Dict[Optional[int], bool] = {t: False for t in targets}
        queried: List[Optional[int]] = []
        seen: Set[int] = set()

        def fetch_page(target: Optional[int], offset: int) -> int:
            """Fetch one page into ``target``'s bucket; return how many were new."""
            params = dict(base)
            if target is not None:
                params["region_v2"] = target
            params["o"] = offset
            listings, total = self._page(params)
            coverage.requests += 1
            coverage.fetched += len(listings)
            if target not in queried:
                queried.append(target)
            pages[target] += 1
            if total is not None:
                # `total` is a property of the query, not of the offset, so the
                # pages of one region all report it. Keep ONE value per region:
                # summing per-page totals multiplies the headline by the page
                # count (11,071 was reported as 22,142).
                previous = region_totals[target]
                region_totals[target] = int(total) if previous is None else max(previous, int(total))
            if not listings:
                region_exhausted[target] = True
                return 0
            fresh = 0
            for listing in listings:
                if listing.list_id in seen:
                    coverage.duplicates_dropped += 1
                    continue
                seen.add(listing.list_id)
                buckets[target].append(listing)
                fresh += 1
            if fresh == 0:
                # Pages overlap at shard boundaries, so a window can be entirely
                # stale. Stop on "nothing new" rather than trusting the offset.
                region_exhausted[target] = True
            return fresh

        def next_offset(target: Optional[int]) -> int:
            return pages[target] * contract.MAX_PAGE_SIZE

        # Round one: a page from every region, which also learns their totals.
        # Largest first -- the codes sort ascending and the main city sorts last,
        # so a low --max-requests otherwise spends the whole budget on the
        # annexed provinces and returns none of the city. Size is unknown before
        # the first response, so the province's own code (a round *000 value) is
        # the best available prior.
        # Size is unknown before the first response, so order by the best prior
        # available: the code the user's own input resolved to, then the
        # province-level round *000 codes, then ascending. Using only the round
        # test identified Hà Nội and HCM and left the other 22 merger groups
        # spending a small budget entirely on the annexed province.
        matched = resolve_province(region) if region else None
        first_round = sorted(
            targets,
            key=lambda t: (0 if t == matched else 1, 0 if (t and t % 1000 == 0) else 1, t or 0),
        )
        for target in first_round:
            if coverage.requests >= max_requests:
                break
            fetch_page(target, 0)

        # Allocate the rest proportionally to each region's real size, falling
        # back to an equal share when the gateway stated no total.
        #
        # A `total` at the cap is a FLOOR, not a magnitude: two regions both
        # reporting 10,000 are indistinguishable, and weighting them equally is
        # the equal split this fan-out exists to avoid. Capped regions are
        # therefore weighted above every uncapped one, and tie among themselves.
        known = {t: v for t, v in region_totals.items() if v}
        capped = {t for t, v in known.items() if v >= contract.TOTAL_CAP}
        uncapped_max = max([v for t, v in known.items() if t not in capped] or [0])
        weights = {
            t: (uncapped_max + contract.TOTAL_CAP if t in capped else v)
            for t, v in known.items()
        }
        weight_total = sum(weights.values()) or 0
        if len(capped) > 1:
            warnings.append(
                f"{len(capped)} of this province's region codes report the "
                f"{contract.TOTAL_CAP:,} cap, so their relative sizes are unknown "
                f"and the sample splits them evenly."
            )
        def quota(target: Optional[int]) -> int:
            if not weight_total or target not in weights:
                return max(1, needed // max(1, len(targets)))
            return max(1, round(needed * weights[target] / weight_total))

        # Drive on whether any REGION is still short of its quota, not on the
        # global row count: round one over-fetches (a 50-row page per region),
        # so a global test is already satisfied while the largest region -- the
        # one that should dominate the sample -- still holds a single page.
        def surviving(target: Optional[int]) -> int:
            """Rows in this bucket that will survive the client-side filters.

            Counting raw rows made `--condition new --limit 20` stop after two
            requests with one result: the unfiltered pool had met its quota while
            92 of 100 rows were about to be discarded.

            It counts with `self._survives`, the SAME predicate that builds the
            final list. Re-stating the filters here let a newly added one
            (`--match-all`) be registered in `client_filters` and applied to the
            output while the crawl budget still counted rows it was about to
            discard -- one request, 50 rows fetched, 49 dropped, 1 returned of
            the 10 asked for. Two owners for one path is how that happens twice.
            """
            if not client_filters:
                return len(buckets[target])
            return sum(1 for x in buckets[target] if survives(x))

        def below_quota() -> bool:
            return any(not region_exhausted[t] and surviving(t) < quota(t) for t in targets)

        progressed = True
        while progressed and coverage.requests < max_requests and below_quota():
            progressed = False
            for target in targets:
                if coverage.requests >= max_requests:
                    break
                if region_exhausted[target] or surviving(target) >= quota(target):
                    continue
                offset = pages[target] * contract.MAX_PAGE_SIZE
                if offset + contract.MAX_PAGE_SIZE > contract.MAX_SEARCH_WINDOW:
                    region_exhausted[target] = True
                    warnings.append(
                        f"Reached the gateway's {contract.MAX_SEARCH_WINDOW:,}-result "
                        f"search window; deeper listings are not addressable."
                    )
                    continue
                if fetch_page(target, offset):
                    progressed = True

        # Draw the result proportionally, so a merged province is represented by
        # its real composition rather than by whichever code sorts first.
        pool: List[Listing] = []
        if len(targets) == 1:
            pool = list(buckets[targets[0]])
        else:
            remaining = dict(buckets)
            # Smooth weighted draw: at each step take from whichever region is
            # furthest below its proportional entitlement. Emitting `share` rows
            # from one region before moving on made the first four results of
            # `--region hcm --limit 4` all Bình Dương and none Ho Chi Minh City.
            drawn: Dict[Optional[int], int] = {t: 0 for t in targets}
            share_of = {
                t: (weights.get(t, 0) / weight_total if weight_total else 1 / len(targets))
                for t in targets
            }
            while any(remaining[t] for t in targets):
                available = [t for t in targets if remaining[t]]
                # Classic smooth weighted round robin: pick whoever would be
                # furthest below entitlement AFTER taking one more row. Using
                # `drawn / share` instead served every region once before any
                # was served twice, so at --limit 3 a province that is 90% one
                # city returned a third of its rows from it.
                target = min(
                    available,
                    key=lambda t: ((drawn[t] + 1) / share_of[t] if share_of[t]
                                   else float("inf"),
                                   -(weights.get(t) or 0)),
                )
                pool.append(remaining[target].pop(0))
                drawn[target] += 1

        # Exhaustion is an AND: one small region running dry says nothing about
        # the province. Reporting it as exhausted hides an under-delivery.
        exhausted = all(region_exhausted[t] for t in targets)
        coverage.exhausted = exhausted
        coverage.region_codes = [t for t in queried if t is not None]
        skipped = [t for t in targets if t is not None and t not in queried]
        # Stated AFTER the crawl, from what was actually queried. Announcing
        # "all were queried and merged" up front contradicted the budget warning
        # three lines later.
        if len(region_codes) > 1:
            actual = [t for t in queried if t is not None]
            warnings.append(
                f"'{province_name(region_codes[0])}' is served by {len(region_codes)} "
                f"legacy region codes {sorted(region_codes)} after Vietnam's 2025 "
                f"province merger; {len(actual)} of them were queried and merged."
            )
        if skipped:
            warnings.append(
                f"Request budget ({max_requests}) ran out before querying region "
                f"code(s) {skipped}; those parts of the province are missing. "
                f"Raise --max-requests."
            )

        before = len(pool)
        filtered = [listing for listing in pool if survives(listing)]
        coverage.filtered_out = before - len(filtered)

        ordered = self._sort(filtered, sort)[:limit]
        coverage.returned = len(ordered)

        stated = [v for v in region_totals.values() if v is not None]
        if stated:
            # One total per REGION (summed across the province), never one per
            # request.
            reported = max(stated) if len(targets) == 1 else sum(stated)
            coverage.reported_total = reported
            coverage.total_is_capped = any(v >= contract.TOTAL_CAP for v in stated)
            if coverage.total_is_capped:
                coverage.total_floor_reason = "upstream_cap"
            if skipped:
                coverage.total_floor_reason = "regions_skipped"
                # The total covers only the regions actually reached, so it is a
                # floor for the province regardless of the upstream cap.
                coverage.total_is_capped = True
            if any(v >= contract.TOTAL_CAP for v in stated):
                warnings.append(
                    f"Upstream 'total' saturates at {contract.TOTAL_CAP:,}; the "
                    f"reported {reported:,} is a floor, not a count."
                )
            elif skipped:
                warnings.append(
                    f"The reported {reported:,} covers only the region codes "
                    f"actually queried, so it is a floor for this province."
                )

        # Mixing sale and rent prices produces a statistic with no meaning -- a
        # monthly rent averaged against a purchase price.
        #
        # This is checked UNCONDITIONALLY, against the rows actually returned.
        # Gating it on `listing_type == "any"` trusted `st` to have worked, and
        # this gateway's signature failure is accepting a parameter and ignoring
        # it -- so the one place the tool could have presented unfiltered data
        # as filtered was the branch where the user had asked for a filter. A
        # request is not a receipt; the rows are.
        # The gateway's full-text search UNIONS its terms instead of
        # intersecting them, so adding a word WIDENS the result set and the
        # least specific word dominates the ranking. Measured 2026-08-28:
        # `canon powershot v1` in region 3017 returned 40 ads -- the 39 matching
        # `v1` (Honda Winner V1 motorbikes) plus the 1 real Canon PowerShot,
        # which landed at position 23. `analyze` then reported a median of
        # 17,500,000d for "canon powershot v1" in Da Nang, a confident figure
        # describing motorbikes, for a camera with zero listings in that city.
        #
        # Checked UNCONDITIONALLY, against the text of the rows returned: the
        # user cannot ask the gateway for an intersection, so silence here is
        # indistinguishable from a query that worked.
        if len(terms) > 1 and ordered:
            matched = sum(1 for x in ordered if covers_all_terms(x, terms))
            if matched == 0:
                warnings.insert(0, (
                    f"NONE of the {len(ordered)} results contain all of "
                    f"{terms} - the gateway unions search terms, so these ads "
                    f"match only part of your query and any statistic over them "
                    f"describes something else. Search fewer, more specific "
                    f"words, or pass --match-all."
                ))
            elif matched < len(ordered):
                warnings.insert(0, (
                    f"Only {matched} of {len(ordered)} results contain all of "
                    f"{terms}; the gateway unions search terms rather than "
                    f"intersecting them. Pass --match-all to keep just the "
                    f"{matched} that match every word."
                ))
        if match_all and coverage.filtered_out:
            warnings.append(
                f"--match-all discarded listings whose text was missing one of "
                f"{terms}. The gateway cannot filter this way, so the discard "
                f"happened here, after fetching."
            )

        counts = {t: sum(1 for x in ordered if x.listing_type == t)
                  for t in sorted({x.listing_type for x in ordered if x.listing_type})}
        priced_kinds = {t for t in counts if t in ("s", "u")}
        requested = LISTING_TYPE_CHOICES.get(listing_type)

        if requested is not None:
            unexpected = {t: n for t, n in counts.items() if t != requested}
            if unexpected:
                warnings.append(
                    f"--listing-type {listing_type} was requested but the gateway "
                    f"returned other types {unexpected}; the 'st' filter is no "
                    f"longer being honoured. Treat these results as UNFILTERED "
                    f"and re-run 'chotot doctor'."
                )
        elif len(priced_kinds) > 1:
            warnings.append(
                f"Results mix listing types {counts} (s=for sale, u=for rent). "
                f"Any price comparison across them is meaningless - pass "
                f"--listing-type sale or --listing-type rent."
            )

        if client_filters and coverage.reported_total is not None:
            warnings.append(
                f"'Upstream matches' counts what the gateway returned BEFORE "
                f"{', '.join(client_filters)} was applied here, so it is larger "
                f"than the number of listings that actually match."
            )

        if coverage.duplicates_dropped:
            warnings.append(
                f"{coverage.duplicates_dropped} duplicate listings were returned "
                f"across pages and removed (adjacent pages overlap at shard "
                f"boundaries; fetching faster does not avoid it)."
            )
        if coverage.requests >= max_requests and not exhausted:
            warnings.append(
                f"Stopped at the {max_requests}-request budget; more listings exist."
            )
        # Name the real cause. "The pool did not contain enough matches" is true
        # and useless when the filter is simply inapplicable to the category:
        # property ads carry no condition at all, so `--condition used` discards
        # every row and looks like "there is nothing for sale".
        if condition != "any" and pool and coverage.filtered_out == before:
            unstated = sum(1 for x in pool if x.condition_code is None)
            if unstated == before:
                warnings.append(
                    f"All {before} listings fetched state no condition, so none can "
                    f"match --condition {condition}. Categories such as property and "
                    f"jobs do not carry a condition field; drop the filter to see them."
                )
        elif len(ordered) < limit and not exhausted:
            warnings.append(
                f"Returned {len(ordered)} of the {limit} requested - the fetched "
                f"pool did not contain enough matches."
            )

        warnings = [w.replace("{pool}", str(len(filtered))) for w in warnings]

        return SearchResult(
            listings=ordered, coverage=coverage, warnings=warnings,
            query=query, category_id=category_id,
        )

    def get_listing(self, id_or_url: Union[int, str]) -> ListingDetail:
        """Fetch one listing by numeric id or Chợ Tốt URL."""
        listing_id = parse_listing_id(id_or_url)
        payload = self.transport.get_json(
            contract.ENDPOINTS["detail"].path.format(list_id=listing_id)
        )
        return ListingDetail.from_payload(payload)

    def seller_listings(
        self, account_id: Union[int, str], limit: Optional[int] = None,
    ) -> "Storefront":
        """A seller's complete live inventory.

        Uses the ``theia`` storefront endpoint, which states a real total and
        accepts either the numeric ``account_id`` or the ``account_oid``. This
        is strictly better than ``ad-listing?account_id=``: that route works but
        is clamped to 50 rows a page and gives no authoritative total.
        """
        from chotot.seller import SellerClient

        return SellerClient(self.transport).storefront(account_id, limit=limit)

    def shop_profile(self, alias: str) -> "ShopProfile":
        """A professional shop's profile, including its embedded listings."""
        from chotot.seller import SellerClient

        return SellerClient(self.transport).shop(alias)


def _resolve_district_in(district: Union[str, int], region_codes: Sequence[int]) -> Optional[int]:
    """Resolve a district within a province, trying each of its legacy codes.

    ``resolve_district`` disambiguates by a single province code, but a merged
    province has several. Passing ``None`` made "Quận Ninh Kiều" inside
    ``--region "can tho"`` fail as ambiguous, with a remedy telling the user to
    pass the ``--region`` they had already passed.
    """
    if not region_codes:
        return resolve_district(district, None)
    last: Optional[Exception] = None
    for code in region_codes:
        try:
            return resolve_district(district, code)
        except ResolutionError as exc:
            last = exc
    # Not in any of them: report against the province as a whole.
    raise resolve_district(district, region_codes[0]) if last is None else last


def parse_listing_id(id_or_url: Union[int, str]) -> int:
    """Extract a numeric listing id from an id or a chotot.com URL."""
    import re

    text = str(id_or_url).strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"/(\d{6,})\.htm", text) or re.search(r"(\d{6,})", text)
    if match:
        return int(match.group(1))
    raise UsageError(
        f"Could not read a listing id from {id_or_url!r}.",
        remedy="Pass the number (e.g. 134348455) or the full URL "
               "(e.g. https://www.chotot.com/134348455.htm).",
    )
