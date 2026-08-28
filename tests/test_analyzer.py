"""Statistics, and the honesty rules around them."""
from __future__ import annotations

import pytest

from chotot.analyzer import (
    MIN_SAMPLE_FOR_CONFIDENCE,
    MIN_SAMPLE_FOR_PERCENTILES,
    MarketAnalyzer,
    percentile,
)
from chotot.models import Listing


def make(prices, condition=2, listing_type="s"):
    """Build listings with the given prices; None means 'no price stated'."""
    out = []
    for index, price in enumerate(prices, 1):
        ad = {
            "list_id": 1000 + index, "subject": f"item {index}", "price": price,
            "elt_condition": condition, "list_time": 1787892561000,
            "region_v2": 13000, "category": 5010, "type": listing_type,
        }
        out.append(Listing.from_ad(ad))
    return out


def test_percentile_interpolates():
    assert percentile([10, 20, 30, 40], 0.5) == 25.0
    assert percentile([10], 0.9) == 10.0
    assert percentile([], 0.5) is None


def test_empty_input_does_not_crash_or_invent_numbers():
    report = MarketAnalyzer.analyze([], query="nothing")
    assert report.priced_count == 0
    assert report.median is None and report.p25 is None
    assert report.fair_range is None
    assert report.warnings


def test_percentiles_are_withheld_below_the_minimum_sample():
    """Four listings cannot support a P25; reporting one would invent precision."""
    report = MarketAnalyzer.analyze(make([1_000, 2_000, 3_000, 4_000]), query="tiny")
    assert report.priced_count < MIN_SAMPLE_FOR_PERCENTILES
    assert report.p25 is None and report.p75 is None
    assert report.fair_range is None
    assert report.median is not None, "the median is still shown, marked indicative"
    assert any("withheld" in w for w in report.warnings)


def test_percentiles_appear_once_the_sample_is_large_enough():
    report = MarketAnalyzer.analyze(make(list(range(1_000, 1_000 + 100 * 30, 30))), query="big")
    assert report.p25 is not None and report.p75 is not None
    assert report.fair_range is not None
    assert report.is_confident


def test_unpriced_listings_are_excluded_not_counted_as_zero():
    """Otherwise every average is dragged toward zero by negotiable ads."""
    report = MarketAnalyzer.analyze(make([None] * 10 + [1_000_000] * 10), query="mixed")
    assert report.unpriced_count == 10
    assert report.priced_count == 10
    assert report.minimum == 1_000_000, "an unpriced ad leaked in as ₫0"
    assert report.mean == 1_000_000


def test_zero_prices_are_excluded_from_statistics():
    report = MarketAnalyzer.analyze(make([0] * 5 + [2_000_000] * 10), query="giveaways")
    assert report.minimum == 2_000_000


def test_outlier_trimming_reports_what_it_removed():
    prices = list(range(1_000_000, 1_000_000 + 20 * 50_000, 50_000)) + [900_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="outlier")
    assert report.outliers_removed >= 1
    assert report.maximum < 900_000_000
    assert any("outlier" in w for w in report.warnings)


def test_mad_fence_catches_an_outlier_when_the_iqr_collapses():
    """Many sellers copy one asking price, so a zero IQR is common in practice."""
    prices = [1_000_000] * 8 + [1_050_000] * 6 + [950_000] * 6 + [900_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="flat")
    assert report.outliers_removed >= 1, "the MAD fallback did not fire"
    assert report.maximum < 900_000_000


def test_order_of_magnitude_fence_catches_a_typo_on_a_point_mass():
    """Twenty sellers at ₫1M and one at ₫900M: every dispersion estimate is zero."""
    prices = [1_000_000] * 20 + [900_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="pointmass")
    assert report.outliers_removed == 1
    assert report.maximum == 1_000_000


def test_order_of_magnitude_fence_keeps_merely_expensive_listings():
    """The blunt fence must not eat a legitimately dearer listing."""
    prices = [1_000_000] * 20 + [3_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="pointmass")
    assert report.outliers_removed == 0
    assert report.maximum == 3_000_000


def test_identical_prices_do_not_crash_or_trim():
    report = MarketAnalyzer.analyze(make([1_000_000] * 25), query="constant")
    assert report.outliers_removed == 0
    assert report.median == 1_000_000
    assert report.stdev == 0


def test_outlier_trimming_can_be_disabled():
    prices = [1_000_000] * 20 + [900_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="outlier", remove_outliers=False)
    assert report.outliers_removed == 0
    assert report.maximum == 900_000_000


def test_trimming_never_empties_the_sample():
    """A pathological spread is the finding, not a reason to return nothing."""
    report = MarketAnalyzer.analyze(make([1, 2, 3, 1_000_000_000, 2_000_000_000] * 4), query="wild")
    assert report.priced_count > 0
    assert report.median is not None


def test_unstated_condition_gets_its_own_bucket():
    """Folding it into 'Used' would invent a fact the API never stated."""
    listings = make([1_000_000] * 5, condition=2)
    for listing in make([2_000_000] * 5, condition=None):
        listings.append(listing)
    report = MarketAnalyzer.analyze(listings, query="mixed conditions")
    labels = {entry.condition for entry in report.by_condition}
    assert "Unstated" in labels


def test_evaluate_returns_unknown_when_the_sample_cannot_support_a_verdict():
    """A missing basis is not evidence for 'fair price'."""
    report = MarketAnalyzer.analyze(make([1_000_000, 2_000_000]), query="tiny")
    verdict = report.evaluate(1_500_000)
    assert verdict["tier"] == "unknown"
    assert verdict["percentile"] is None


def test_evaluate_places_a_price_in_the_distribution():
    report = MarketAnalyzer.analyze(make(list(range(1_000_000, 1_000_000 + 100 * 10_000, 10_000))),
                                    query="spread")
    assert report.evaluate(1_050_000)["tier"] in ("far_below", "below")
    assert report.evaluate(report.median or 0)["tier"] == "typical"
    assert report.evaluate(1_990_000)["tier"] in ("above", "far_above")


def test_asking_price_caveat_is_always_present():
    """The number is asking prices, never sold prices; the caveat travels with it."""
    report = MarketAnalyzer.analyze(make([1_000_000] * 30), query="x")
    assert any("ASKING" in w for w in report.warnings)


def test_histogram_buckets_account_for_every_price():
    listings = make(list(range(1_000, 1_000 + 50 * 100, 100)))
    report = MarketAnalyzer.analyze(listings, query="hist", remove_outliers=False)
    assert sum(b["count"] for b in report.histogram) == report.priced_count


def test_small_sample_is_flagged_as_not_confident():
    report = MarketAnalyzer.analyze(make([1_000_000] * (MIN_SAMPLE_FOR_CONFIDENCE - 5)), query="x")
    assert report.is_confident is False


def test_mixed_sale_and_rent_warning_leads_the_report():
    """A median over rents and sale prices is not a number about anything.

    The warning is inserted FIRST so it cannot be lost below the caveats.
    """
    listings = make([2_000_000_000] * 15, listing_type="s") + \
               make([8_000_000] * 15, listing_type="u")
    report = MarketAnalyzer.analyze(listings, query="can ho")
    assert report.to_dict()["mixes_sale_and_rent"] is True
    assert "MIXES SALE AND RENTAL" in report.warnings[0]


def test_single_listing_type_is_not_flagged():
    report = MarketAnalyzer.analyze(make([2_000_000_000] * 30, listing_type="s"), query="can ho")
    assert report.to_dict()["mixes_sale_and_rent"] is False
    assert not any("MIXES SALE" in w for w in report.warnings)


def test_listing_type_breakdown_is_reported():
    listings = make([1_000_000] * 10, listing_type="s") + make([2_000_000] * 5, listing_type="u")
    report = MarketAnalyzer.analyze(listings, query="x")
    assert report.by_listing_type == {"s": 10, "u": 5}


def test_evaluate_boundaries_agree_with_the_printed_typical_range():
    """P25 and P75 are the ENDS of the range the dashboard prints as typical."""
    report = MarketAnalyzer.analyze(
        make(list(range(1_000_000, 1_000_000 + 100 * 10_000, 10_000))), query="spread")
    assert report.p25 is not None and report.p75 is not None
    assert report.evaluate(report.p25)["tier"] == "typical"
    assert report.evaluate(report.p75)["tier"] == "typical"


def test_non_positive_price_gets_its_own_diagnosis():
    """Telling a user who passed 0 that the SAMPLE is too small is a wrong cause."""
    report = MarketAnalyzer.analyze(make([1_000_000] * 40), query="big")
    verdict = report.evaluate(0)
    assert verdict["tier"] == "unknown"
    assert "positive" in verdict["note"].lower()
    assert "sample" not in verdict["note"].lower()


def test_analysed_count_is_reported_separately_from_priced_count():
    """The statistics describe the post-trim set; labelling it 'with_price'
    overstates the basis by exactly the number of outliers removed."""
    prices = list(range(1_000_000, 1_000_000 + 20 * 50_000, 50_000)) + [900_000_000]
    report = MarketAnalyzer.analyze(make(prices), query="outlier")
    sample = report.to_dict()["sample"]
    assert sample["with_price"] == 21
    assert sample["statistics_computed_over"] == report.analysed_count < 21


def test_trimming_away_one_listing_type_is_stated_truthfully():
    """The old warning said "the figures below average a monthly rent against a
    purchase price" in runs where the fence had already removed every sale ad --
    so the numbers were pure rents and the sentence was false about its own run.
    """
    listings = make([5_000_000 + i * 20_000 for i in range(53)], listing_type="u") + \
               make([2_400_000_000] * 7, listing_type="s")
    report = MarketAnalyzer.analyze(listings, query="can ho")
    assert report.to_dict()["mixes_sale_and_rent"] is False
    assert "describe rental listings ONLY" in report.warnings[0], report.warnings[0]
    assert "average a monthly rent against a purchase price" not in report.warnings[0]


def test_a_genuinely_mixed_surviving_sample_still_says_so():
    """The negative side: when both types survive the trim, the original
    warning is the true one."""
    listings = make([5_000_000] * 20, listing_type="u") + \
               make([6_000_000] * 20, listing_type="s")
    report = MarketAnalyzer.analyze(listings, query="mixed")
    assert report.to_dict()["mixes_sale_and_rent"] is True
    assert "MIXES SALE AND RENTAL" in report.warnings[0]


def test_pre_trim_composition_is_carried_alongside_the_post_trim_flag():
    """So a JSON consumer can never read the two as contradicting each other."""
    listings = make([5_000_000 + i * 20_000 for i in range(53)], listing_type="u") + \
               make([2_400_000_000] * 7, listing_type="s")
    payload = MarketAnalyzer.analyze(listings, query="can ho").to_dict()
    assert payload["sampled_listing_types"] == {"s": 7, "u": 53}
    assert set(payload["by_listing_type"]) == {"u"}
