# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Price statistics over a sampled pool of listings.

Everything here describes **asking prices in a sample**, never "the market
price". Chợ Tốt publishes no sold prices, the fetched pool is a non-random slice
of an unstably-ranked feed, and the upstream total saturates at 10,000. Each of
those limits is carried on the result rather than left for the reader to
remember, because a bare median reads as a fact about Vietnam and is actually a
fact about ~120 ads.

Sample-size honesty is enforced, not advisory: below
:data:`MIN_SAMPLE_FOR_PERCENTILES` the percentile fields are ``None`` instead of
a number computed from four listings.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chotot.models import CONDITION_LABELS, Listing

#: Below this many priced listings, percentiles are not reported at all. A P25
#: over 6 points is arithmetic, not information.
MIN_SAMPLE_FOR_PERCENTILES = 8

#: Below this, even the median is flagged as indicative only.
MIN_SAMPLE_FOR_CONFIDENCE = 20

#: Tukey fence multiplier for outlier trimming.
IQR_FENCE = 1.5

#: Last-resort fence used only when IQR and MAD are both zero. Blunt on purpose.
ORDER_OF_MAGNITUDE_FENCE = 10


def percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile. ``q`` in [0, 1]. ``None`` when empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(sorted_values[lower]) * (1 - weight) + float(sorted_values[upper]) * weight


@dataclass
class ConditionStats:
    condition: str
    count: int
    median: Optional[int]
    minimum: int
    maximum: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition": self.condition, "count": self.count,
            "median_vnd": self.median, "min_vnd": self.minimum, "max_vnd": self.maximum,
        }


@dataclass
class PriceReport:
    """Descriptive statistics over the asking prices of a sampled pool."""

    query: str
    sample_size: int
    #: Listings that carried a usable price BEFORE outlier trimming.
    priced_count: int
    unpriced_count: int
    outliers_removed: int
    #: Listings the statistics below are actually computed over (post-trim).
    analysed_count: int = 0

    minimum: Optional[int] = None
    maximum: Optional[int] = None
    mean: Optional[int] = None
    median: Optional[int] = None
    p10: Optional[int] = None
    p25: Optional[int] = None
    p75: Optional[int] = None
    p90: Optional[int] = None
    iqr: Optional[int] = None
    trimmed_mean: Optional[int] = None
    stdev: Optional[int] = None

    by_condition: List[ConditionStats] = field(default_factory=list)
    by_listing_type: Dict[str, int] = field(default_factory=dict)
    #: Composition BEFORE outlier trimming, so a reader can see when the trim
    #: removed a whole listing type rather than inferring it from a warning.
    sampled_listing_types: Dict[str, int] = field(default_factory=dict)
    histogram: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    currency: str = "VND"

    @property
    def has_percentiles(self) -> bool:
        return self.p25 is not None and self.p75 is not None

    @property
    def fair_range(self) -> Optional[Tuple[int, int]]:
        """Interquartile asking range, or ``None`` when the sample is too small."""
        if self.p25 is None or self.p75 is None:
            return None
        return (self.p25, self.p75)

    @property
    def is_confident(self) -> bool:
        return self.analysed_count >= MIN_SAMPLE_FOR_CONFIDENCE

    def evaluate(self, price: int) -> Dict[str, Any]:
        """Place one asking price against the sampled distribution.

        Returns a verdict of ``unknown`` when the sample cannot support one --
        a missing basis is not evidence for "fair".
        """
        # Two different causes, two different messages. Collapsing them told a
        # user who passed 0 that the sample was too small, which is a wrong
        # diagnosis under a correct refusal.
        if price <= 0:
            return {
                "tier": "unknown",
                "verdict": "A price must be a positive amount in VND",
                "note": f"Cannot score {price}. Pass a positive VND amount, e.g. 6000000.",
                "percentile": None,
            }
        if not self.has_percentiles:
            return {
                "tier": "unknown",
                "verdict": "Not enough comparable listings to judge this price",
                "note": f"Only {self.analysed_count} priced listing(s) in the sample; "
                        f"{MIN_SAMPLE_FOR_PERCENTILES} are needed for percentiles.",
                "percentile": None,
            }
        assert self.p10 is not None and self.p25 is not None
        assert self.p75 is not None and self.p90 is not None

        # The boundaries must agree with the interval the dashboard prints as
        # "Typical asking range  P25 — P75". With `price <= self.p25` mapping to
        # "below", a price exactly equal to P25 was called below a range whose
        # printed lower bound it is.
        if price < self.p10:
            tier, verdict = "far_below", "Far below the sampled range"
            note = ("Bottom 10% of asking prices. Verify condition, completeness and "
                    "seller history before treating this as a bargain.")
        elif price < self.p25:
            tier, verdict = "below", "Below the typical asking range"
            note = "In the lowest quartile of comparable asking prices."
        elif price <= self.p75:
            tier, verdict = "typical", "Within the typical asking range"
            note = "Between the 25th and 75th percentile of the sample."
        elif price <= self.p90:
            tier, verdict = "above", "Above the typical asking range"
            note = "In the upper quartile. Check what accessories or warranty are included."
        else:
            tier, verdict = "far_above", "Far above the sampled range"
            note = "Top 10% of asking prices in the sample."

        return {
            "tier": tier, "verdict": verdict, "note": note,
            "percentile": self._percentile_of(price),
            "based_on": f"{self.analysed_count} priced listings",
            "confident": self.is_confident,
        }

    def _percentile_of(self, price: int) -> Optional[int]:
        if not self._sorted_prices:
            return None
        below = sum(1 for p in self._sorted_prices if p < price)
        return round(100 * below / len(self._sorted_prices))

    _sorted_prices: List[int] = field(default_factory=list, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "currency": self.currency,
            "sample": {
                "listings_sampled": self.sample_size,
                "with_price": self.priced_count,
                "without_price": self.unpriced_count,
                "outliers_removed": self.outliers_removed,
                # The statistics below describe this many listings, not
                # `with_price`; conflating the two overstates the basis by
                # exactly the number of trimmed outliers.
                "statistics_computed_over": self.analysed_count,
                "confident": self.is_confident,
            },
            "asking_price": {
                "min": self.minimum, "max": self.maximum,
                "mean": self.mean, "median": self.median,
                "trimmed_mean": self.trimmed_mean, "stdev": self.stdev,
                "p10": self.p10, "p25": self.p25, "p75": self.p75, "p90": self.p90,
                "iqr": self.iqr,
            },
            "typical_range": {"from": self.p25, "to": self.p75} if self.has_percentiles else None,
            "by_condition": [c.to_dict() for c in self.by_condition],
            # `by_listing_type` describes the analysed (post-trim) set, so the
            # flag derived from it is about the numbers above it. The pre-trim
            # composition is carried alongside so the two can never be read as
            # contradicting each other.
            "by_listing_type": self.by_listing_type,
            "sampled_listing_types": self.sampled_listing_types,
            "mixes_sale_and_rent": len({t for t in self.by_listing_type if t in ("s", "u")}) > 1,
            "histogram": self.histogram,
            "warnings": self.warnings,
        }


class MarketAnalyzer:
    """Turns a pool of listings into a :class:`PriceReport`."""

    @classmethod
    def analyze(
        cls,
        listings: Sequence[Listing],
        query: str = "sample",
        remove_outliers: bool = True,
        buckets: int = 8,
    ) -> PriceReport:
        sample_size = len(listings)
        priced = [x for x in listings if x.has_price]
        prices = sorted(int(x.price) for x in priced if x.price is not None)
        unpriced = sample_size - len(priced)

        warnings: List[str] = []
        if unpriced:
            warnings.append(
                f"{unpriced} of {sample_size} listings state no price (negotiable or "
                f"giveaway) and are excluded from every statistic."
            )

        if not prices:
            warnings.append("No listing in the sample carried a usable price.")
            return PriceReport(
                query=query, sample_size=sample_size, priced_count=0,
                unpriced_count=unpriced, outliers_removed=0, warnings=warnings,
            )

        kept = prices
        removed = 0
        degenerate = False
        if remove_outliers and len(prices) >= 10:
            bounds = cls._outlier_bounds(prices)
            if bounds is None:
                # Every robust spread measure is zero (e.g. 20 listings at the
                # same price plus one extreme). A Tukey fence is undefined here,
                # so rather than invent one we keep the sample intact and say so
                # -- a silently skipped trim reads exactly like a clean sample.
                degenerate = True
            else:
                low, high = bounds
                trimmed = [p for p in prices if low <= p <= high]
                # Never trim the sample away entirely; if the fence rejects
                # everything the distribution is the finding, not the outliers.
                if len(trimmed) >= max(4, len(prices) // 4):
                    removed = len(prices) - len(trimmed)
                    kept = trimmed
        if degenerate:
            warnings.append(
                "Outlier trimming was skipped: the sample has no measurable "
                "spread (interquartile range and median absolute deviation are "
                "both zero), so min/max may include an extreme value."
            )
        if removed:
            warnings.append(
                f"{removed} outlier price(s) removed using a {IQR_FENCE}x IQR fence "
                f"(analyze accepts --keep-outliers to see the untrimmed distribution)."
            )

        count = len(kept)
        # The condition breakdown must be computed over the SAME trimmed set as
        # the headline statistics. Mixing them lets a single outlier move a
        # per-condition median while the summary above it says the outlier was
        # removed.
        kept_range = (kept[0], kept[-1]) if removed else None
        enough = count >= MIN_SAMPLE_FOR_PERCENTILES
        if not enough:
            warnings.append(
                f"Only {count} priced listing(s): percentiles are withheld "
                f"(need {MIN_SAMPLE_FOR_PERCENTILES}). Median shown as indicative only."
            )
        elif count < MIN_SAMPLE_FOR_CONFIDENCE:
            warnings.append(
                f"Small sample ({count} priced listings). Treat the range as "
                f"indicative; analyze accepts --samples to widen it."
            )

        warnings.append(
            "These are ASKING prices scraped from live ads, not transaction prices. "
            "Chợ Tốt does not publish what items actually sold for."
        )

        # A sample that mixes for-sale and for-rent ads has no meaningful median:
        # a monthly rent and a purchase price are different quantities.
        #
        # Both messages below are computed over `kept` -- the set the statistics
        # actually describe. Warning from the PRE-trim set said "the figures
        # below average a monthly rent against a purchase price" in runs where
        # the IQR fence had already removed every sale ad, so the numbers were
        # pure rents and the sentence was false about its own run.
        kept_types = {t: sum(1 for x in priced if x.listing_type == t
                             and (kept_range is None
                                  or kept_range[0] <= int(x.price or 0) <= kept_range[1]))
                      for t in sorted({x.listing_type for x in priced if x.listing_type})}
        kept_types = {t: n for t, n in kept_types.items() if n}
        sampled_types = {t: sum(1 for x in priced if x.listing_type == t)
                         for t in sorted({x.listing_type for x in priced if x.listing_type})}

        surviving = {t for t in kept_types if t in ("s", "u")}
        if len(surviving) > 1:
            warnings.insert(0, (
                f"SAMPLE MIXES SALE AND RENTAL LISTINGS {kept_types} - the figures "
                f"below average a monthly rent against a purchase price and are not "
                f"meaningful. Restrict the sample to one listing type before "
                f"comparing (search and analyze accept --listing-type sale|rent)."
            ))
        elif len({t for t in sampled_types if t in ("s", "u")}) > 1 and surviving:
            # Outlier trimming removed one type wholesale. The numbers are sound
            # but they describe only what survived, and saying which is the
            # difference between a rent median and an apartment price.
            kind = {"s": "for-sale", "u": "rental"}[next(iter(surviving))]
            dropped = {t: n for t, n in sampled_types.items() if t not in kept_types}
            warnings.insert(0, (
                f"The sample contained both sale and rental listings {sampled_types}, "
                f"and outlier trimming removed {dropped} entirely. The figures below "
                f"describe {kind} listings ONLY."
            ))

        def as_int(value: Optional[float]) -> Optional[int]:
            return int(round(value)) if value is not None else None

        p25 = percentile(kept, 0.25) if enough else None
        p75 = percentile(kept, 0.75) if enough else None
        trim = int(count * 0.10)
        core = kept[trim:count - trim] if trim and count - 2 * trim >= 1 else kept

        report = PriceReport(
            query=query,
            sample_size=sample_size,
            priced_count=len(prices),
            analysed_count=count,
            unpriced_count=unpriced,
            outliers_removed=removed,
            minimum=kept[0],
            maximum=kept[-1],
            mean=as_int(statistics.fmean(kept)),
            median=as_int(percentile(kept, 0.50)),
            p10=as_int(percentile(kept, 0.10)) if enough else None,
            p25=as_int(p25),
            p75=as_int(p75),
            p90=as_int(percentile(kept, 0.90)) if enough else None,
            iqr=as_int(p75 - p25) if (p25 is not None and p75 is not None) else None,
            trimmed_mean=as_int(statistics.fmean(core)),
            stdev=as_int(statistics.stdev(kept)) if count >= 2 else None,
            by_condition=cls._by_condition(priced, kept_range),
            # Same trimmed range as by_condition and the headline statistics;
            # counting the pre-trim set made the two breakdowns disagree.
            by_listing_type=cls._by_listing_type(priced, kept_range),
            sampled_listing_types=sampled_types,
            histogram=cls._histogram(kept, buckets),
            warnings=warnings,
        )
        report._sorted_prices = kept
        return report

    @staticmethod
    def _outlier_bounds(prices: Sequence[int]) -> Optional[Tuple[float, float]]:
        """Robust inlier bounds, or ``None`` when the spread is degenerate.

        Prefers the Tukey IQR fence. When the interquartile range collapses to
        zero -- common on marketplaces where many sellers copy one asking price
        -- it falls back to a median-absolute-deviation fence, which still has a
        spread to measure. When both are zero there is no defensible fence, and
        the caller is told rather than handed an untrimmed sample that looks
        trimmed.
        """
        q1 = percentile(prices, 0.25)
        q3 = percentile(prices, 0.75)
        if q1 is not None and q3 is not None and q3 > q1:
            spread = q3 - q1
            return q1 - IQR_FENCE * spread, q3 + IQR_FENCE * spread

        median = percentile(prices, 0.50)
        if median is None:
            return None
        deviations = sorted(abs(p - median) for p in prices)
        mad = percentile(deviations, 0.50)
        if mad and mad > 0:
            # 1.4826 rescales MAD to a normal-consistent sigma; 3 sigma is the
            # conventional cut.
            fence = 3 * 1.4826 * mad
            return median - fence, median + fence

        # Last resort: more than half the sample sits exactly on the median, so
        # every dispersion estimate is zero. This happens when many sellers copy
        # one asking price. Only an order-of-magnitude departure is treated as an
        # outlier -- deliberately blunt, so it removes the ₫900,000,000 typo next
        # to twenty ₫1,000,000 listings and nothing subtler.
        if median > 0:
            return median / ORDER_OF_MAGNITUDE_FENCE, median * ORDER_OF_MAGNITUDE_FENCE
        return None

    @staticmethod
    def _by_listing_type(
        priced: Sequence[Listing],
        kept_range: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for listing in priced:
            if kept_range is not None:
                price = int(listing.price or 0)
                if not (kept_range[0] <= price <= kept_range[1]):
                    continue
            if listing.listing_type:
                counts[listing.listing_type] = counts.get(listing.listing_type, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _by_condition(
        priced: Sequence[Listing],
        kept_range: Optional[Tuple[int, int]] = None,
    ) -> List[ConditionStats]:
        """Group prices by condition, over the same trimmed range as the headline."""
        groups: Dict[str, List[int]] = {}
        for listing in priced:
            if kept_range is not None:
                price = int(listing.price or 0)
                if not (kept_range[0] <= price <= kept_range[1]):
                    continue
            # Unknown stays its own bucket. Folding it into "Used" would invent
            # a fact the API never stated.
            label = (CONDITION_LABELS.get(listing.condition_code, f"Code {listing.condition_code}")
                     if listing.condition_code is not None else "Unstated")
            groups.setdefault(label, []).append(int(listing.price or 0))
        out = []
        for label, values in groups.items():
            values.sort()
            out.append(ConditionStats(
                condition=label, count=len(values),
                median=int(round(percentile(values, 0.50) or 0)),
                minimum=values[0], maximum=values[-1],
            ))
        return sorted(out, key=lambda c: -c.count)

    @staticmethod
    def _histogram(prices: Sequence[int], buckets: int) -> List[Dict[str, Any]]:
        if not prices or buckets < 1:
            return []
        low, high = prices[0], prices[-1]
        if high == low:
            return [{"from": low, "to": high, "count": len(prices), "percentage": 100.0}]
        width = (high - low) / buckets
        edges = [low + i * width for i in range(buckets + 1)]
        edges[-1] = high
        counts = [0] * buckets
        for price in prices:
            index = min(int((price - low) / width), buckets - 1)
            counts[index] += 1
        total = len(prices)
        return [
            {
                "from": int(edges[i]), "to": int(edges[i + 1]),
                "count": counts[i], "percentage": round(100 * counts[i] / total, 1),
            }
            for i in range(buckets)
        ]
