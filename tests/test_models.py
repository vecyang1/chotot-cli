"""Model extraction, graded against the real captured payload.

The theme is absent-vs-zero: a field the gateway omitted must surface as
``None`` so a renderer can print "-", never as a 0 the user reads as a fact.
"""
from __future__ import annotations

import copy
from datetime import timezone

import pytest

from chotot.errors import UpstreamContractError
from chotot.models import Listing, ListingDetail, Seller


def test_parses_every_ad_in_the_real_payload(sample_ads):
    listings = [Listing.from_ad(ad) for ad in sample_ads]
    assert len(listings) == len(sample_ads) >= 10
    assert all(x.list_id > 0 for x in listings)


def test_condition_comes_from_elt_condition_not_params(sample_ads):
    """`params` is ALWAYS [] in search results.

    Reading condition from it (as an earlier revision did) yields None for every
    ad and silently collapses the condition breakdown into a single bucket. This
    asserts both halves: params really is empty, and condition is still read.
    """
    assert all(ad.get("params") == [] for ad in sample_ads), \
        "fixture no longer matches the API shape this rule is about"
    listings = [Listing.from_ad(ad) for ad in sample_ads]
    assert any(x.condition_code is not None for x in listings)
    assert any(x.condition_label for x in listings)


def test_posted_at_is_absolute_utc_not_the_relative_string(sample_ads):
    """`date` is Vietnamese relative text ('2 giờ trước'); list_time is epoch ms."""
    listing = Listing.from_ad(sample_ads[0])
    assert listing.posted_at is not None
    assert listing.posted_at.tzinfo == timezone.utc
    assert listing.posted_at.year >= 2020
    # `date` is relative Vietnamese text, e.g. "2 giờ trước" -- it starts with a
    # digit and is not a date. (The previous form parsed as its own opposite and
    # passed by accident.)
    assert listing.posted_label
    assert "-" not in listing.posted_label, "posted_label looks like an absolute date"


def test_missing_price_is_none_not_zero(sample_ads):
    ad = copy.deepcopy(sample_ads[0])
    del ad["price"]
    listing = Listing.from_ad(ad)
    assert listing.price is None
    assert listing.has_price is False


def test_zero_price_is_distinguished_from_missing_price(sample_ads):
    """A stated ₫0 (giveaway) and an unstated price are different facts."""
    ad = copy.deepcopy(sample_ads[0])
    ad["price"] = 0
    listing = Listing.from_ad(ad)
    assert listing.price == 0
    assert listing.has_price is False


def test_missing_seller_counts_are_none_not_zero(sample_ads):
    ad = copy.deepcopy(sample_ads[0])
    ad.pop("sold_ads", None)
    ad.pop("seller_info", None)
    ad.pop("average_rating", None)
    ad.pop("average_rating_for_seller", None)
    seller = Seller.from_ad(ad)
    assert seller.sold_ads is None, "unknown sold-count must not read as 0 sales"
    assert seller.average_rating is None, "unknown rating must not read as 0.0 stars"


def test_absent_condition_is_none_not_a_default_bucket(sample_ads):
    ad = copy.deepcopy(sample_ads[0])
    ad.pop("elt_condition", None)
    listing = Listing.from_ad(ad)
    assert listing.condition_code is None
    assert listing.condition_label is None


def test_ad_without_list_id_raises_rather_than_inventing_one(sample_ads):
    ad = copy.deepcopy(sample_ads[0])
    del ad["list_id"]
    with pytest.raises(UpstreamContractError):
        Listing.from_ad(ad)


def test_detail_extracts_labelled_specs(detail_payload):
    detail = ListingDetail.from_payload(detail_payload)
    assert detail.specs, "ad_params produced no specifications"
    # Labels are the Vietnamese human-readable ones, not the raw ids.
    assert any(" " in label for label in detail.specs)
    assert all(isinstance(v, str) and v for v in detail.specs.values())


def test_detail_without_ad_object_raises():
    with pytest.raises(UpstreamContractError):
        ListingDetail.from_payload({"ad_params": {}})


def test_detail_phone_is_left_masked(detail_payload):
    detail = ListingDetail.from_payload(detail_payload)
    if detail.phone_masked:
        assert "*" in detail.phone_masked, "phone must remain masked as upstream sent it"


def test_to_dict_carries_absolute_time_for_exports(sample_ads):
    record = Listing.from_ad(sample_ads[0]).to_dict()
    assert record["posted_at_utc"] and "T" in record["posted_at_utc"]
    assert "posted_relative" in record


def test_epoch_seconds_and_milliseconds_are_both_understood(sample_ads):
    """/ad-listing states list_time in ms; the shop endpoint states it in
    seconds. Assuming one unit dated every shop listing 1970-01-21."""
    import copy

    from chotot.models import Listing

    millis = copy.deepcopy(sample_ads[0])
    millis["list_time"] = 1787892561000
    seconds = copy.deepcopy(sample_ads[0])
    seconds["list_time"] = 1787892561

    from_ms = Listing.from_ad(millis).posted_at
    from_s = Listing.from_ad(seconds).posted_at
    assert from_ms is not None and from_s is not None
    assert from_ms.year == from_s.year >= 2020
    assert abs((from_ms - from_s).total_seconds()) < 1


@pytest.mark.parametrize("payload", ["a string", 42, None, ["list"]])
def test_a_non_object_ad_is_a_contract_error_not_an_internal_bug(payload):
    """Documented as exit 7. A bare TypeError surfaced as "unexpected error ...
    please report this", sending the user to file a bug against their own tool
    for someone else's schema change."""
    from chotot.errors import UpstreamContractError

    with pytest.raises(UpstreamContractError) as excinfo:
        Listing.from_ad(payload)
    assert excinfo.value.exit_code == 7


def test_a_null_seller_info_does_not_crash(sample_ads):
    import copy

    ad = copy.deepcopy(sample_ads[0])
    ad["seller_info"] = None
    assert Listing.from_ad(ad).seller.name is not None or True


def test_a_non_list_images_field_does_not_crash(sample_ads):
    import copy

    ad = copy.deepcopy(sample_ads[0])
    ad["images"] = "not a list"
    assert Listing.from_ad(ad).images == []


def test_non_object_ad_params_is_a_contract_error(detail_payload):
    import copy

    from chotot.errors import UpstreamContractError

    payload = copy.deepcopy(detail_payload)
    payload["ad_params"] = ["not", "a", "dict"]
    with pytest.raises(UpstreamContractError):
        ListingDetail.from_payload(payload)
