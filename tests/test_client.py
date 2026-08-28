"""Client behaviour: what it sends, what it refuses to send, and how it counts."""
from __future__ import annotations

import json

import pytest

from chotot.client import ChototClient, parse_listing_id
from chotot.errors import NotFoundError, UsageError


def _sent(transport):
    return [r["params"] for r in transport.requests]


# -- what must never reach the wire ---------------------------------------

@pytest.mark.parametrize("kwargs,forbidden", [
    ({"condition": "new"}, ("condition", "elt_condition")),
    ({"seller_type": "shop"}, ("company_ad",)),
    ({"sort": "price_asc"}, ("sort", "direction")),
    ({"min_price": 1_000_000, "max_price": 5_000_000}, ("sp", "ep", "minprice", "maxprice")),
])
def test_ignored_parameters_are_never_forwarded(client, fake_transport, kwargs, forbidden):
    """The gateway answers 200 and ignores these, so sending one would report an
    unfiltered page as filtered."""
    client.search(query="iphone", limit=5, **kwargs)
    assert fake_transport.requests, "no request was made"
    for params in _sent(fake_transport):
        for name in forbidden:
            assert name not in params, f"{name} was forwarded despite being ignored upstream"


def test_price_bounds_use_the_range_parameter(client, fake_transport):
    client.search(query="iphone", min_price=1_000_000, max_price=5_000_000, limit=5)
    assert _sent(fake_transport)[0]["price"] == "1000000-5000000"


def test_open_ended_max_price_sends_a_trailing_dash(client, fake_transport):
    client.search(query="iphone", min_price=20_000_000, limit=5)
    assert _sent(fake_transport)[0]["price"] == "20000000-"


def test_crawl_walks_consecutive_pages_without_skipping_or_repeating(client, fake_transport):
    """The offset must come from pages REQUESTED, not from rows that survived.

    Deriving it from the deduplicated bucket length skipped o=50 entirely and
    then re-requested o=150; the repeat added nothing new and was mistaken for
    upstream exhaustion, capping every large search at ~146 rows while leaving
    20 of 24 allowed requests unspent.
    """
    client.search(query="iphone", limit=250, max_requests=12)
    offsets = fake_transport.offsets
    assert offsets == sorted(offsets), f"offsets went backwards: {offsets}"
    assert len(offsets) == len(set(offsets)), f"an offset was requested twice: {offsets}"
    assert offsets[:5] == [0, 50, 100, 150, 200], offsets


def test_a_large_limit_is_actually_delivered(client, fake_transport):
    """The budget, not a stalled counter, must be what ends a large crawl."""
    result = client.search(query="iphone", limit=250, max_requests=12)
    assert len(result.listings) == 250, (
        f"asked 250, got {len(result.listings)} using "
        f"{fake_transport.request_count} of 12 requests")


def test_the_fake_really_does_produce_duplicates(client, fake_transport):
    """Guards the guard.

    An earlier fake modelled overlap as a fixed backward shift, which produced
    NO overlap under the offsets the client actually requests -- so the two
    deduplication tests below ran over a pool with zero duplicates.
    """
    result = client.search(query="iphone", limit=250, max_requests=12)
    assert result.coverage.duplicates_dropped > 0, (
        "the transport produced no overlapping rows, so the deduplication "
        "tests are vacuous")


def test_pagination_uses_offset_not_page(client, fake_transport):
    client.search(query="iphone", limit=60, max_requests=3)
    params = _sent(fake_transport)
    assert all("page" not in p for p in params)
    assert any("o" in p for p in params)


# -- deduplication ---------------------------------------------------------

def test_overlapping_pages_are_deduplicated(client, fake_transport):
    """The fake overlaps windows by 10, as the real unstably-ranked feed does."""
    result = client.search(query="iphone", limit=200, max_requests=6)
    ids = [x.list_id for x in result.listings]
    assert len(ids) == len(set(ids)), "duplicate listings survived into the result"


def test_duplicates_are_reported_not_hidden(client, fake_transport):
    result = client.search(query="iphone", limit=200, max_requests=6)
    if result.coverage.duplicates_dropped:
        assert any("duplicate" in w.lower() for w in result.warnings)


def test_crawl_stops_when_a_window_adds_nothing_new(sample_ads, detail_payload):
    """A saturated feed keeps returning rows, none of them new.

    Terminating only on an EMPTY page is not enough: this transport never sends
    one, so a crawler without a staleness check burns the whole request budget.
    """
    from tests.conftest import FakeTransport, expand_ads

    transport = FakeTransport(expand_ads(sample_ads, 300), detail_payload, stale_after=2)
    result = ChototClient(transport=transport).search(
        query="iphone", limit=10_000, max_requests=20)
    assert result.coverage.exhausted
    assert transport.request_count < 20, (
        f"crawl made {transport.request_count} requests against a feed that "
        f"stopped producing new rows after 2 pages")


def test_crawl_stops_on_an_exhausted_pool(client, fake_transport):
    result = client.search(query="iphone", limit=10_000, max_requests=20)
    assert result.coverage.exhausted


# -- totals ----------------------------------------------------------------

def test_capped_total_is_reported_as_a_floor(client):
    result = client.search(query="iphone", limit=5)
    assert result.coverage.total_is_capped is True
    assert any("floor" in w or "saturat" in w for w in result.warnings)


def test_absent_total_is_not_reported_as_zero(sample_ads, detail_payload):
    """A no-match query omits `total`; it must not become a confident 0."""
    from tests.conftest import FakeTransport

    transport = FakeTransport(sample_ads, detail_payload)
    client = ChototClient(transport=transport)
    result = client.search(query="zzzznonexistentqueryxyz", limit=5)
    assert result.listings == []
    assert result.coverage.reported_total is None


def test_uncapped_total_is_reported_verbatim(sample_ads, detail_payload):
    from tests.conftest import FakeTransport

    transport = FakeTransport(sample_ads, detail_payload, total=3095)
    result = ChototClient(transport=transport).search(query="honda sh", limit=5)
    assert result.coverage.reported_total == 3095
    assert result.coverage.total_is_capped is False


# -- client-side filtering and sorting ------------------------------------

def test_sorting_is_applied_and_labelled(client):
    result = client.search(query="iphone", limit=20, sort="price_asc")
    priced = [x.price for x in result.listings if x.has_price]
    assert priced == sorted(priced)
    assert any("client-side" in w or "Sorted" in w for w in result.warnings)


def test_unpriced_listings_sort_last_not_as_zero(sample_ads, detail_payload):
    """Otherwise 'cheapest first' opens with every negotiable ad."""
    import copy
    from tests.conftest import FakeTransport

    ads = copy.deepcopy(sample_ads)
    ads[0]["price"] = None
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="iphone", limit=20, sort="price_asc")
    assert result.listings[0].has_price, "an unpriced ad was ranked as the cheapest"


def test_condition_filter_excludes_unstated_rather_than_guessing(sample_ads, detail_payload):
    import copy
    from tests.conftest import FakeTransport

    ads = copy.deepcopy(sample_ads)
    for ad in ads:
        ad.pop("elt_condition", None)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="iphone", limit=20, condition="new")
    assert result.listings == [], "ads with no stated condition were guessed into a bucket"


def test_listing_type_is_pushed_server_side(client, fake_transport):
    """`st` is one of the few filters the gateway actually honours."""
    client.search(query="can ho", listing_type="rent", limit=5)
    assert _sent(fake_transport)[0]["st"] == "u"


def test_listing_type_any_sends_nothing(client, fake_transport):
    client.search(query="can ho", listing_type="any", limit=5)
    assert "st" not in _sent(fake_transport)[0]


def test_unknown_listing_type_is_refused(client):
    with pytest.raises(UsageError):
        client.search(query="x", listing_type="lease")


def test_mixed_sale_and_rent_results_are_flagged(sample_ads, detail_payload):
    """A property browse returns ~55% rentals; averaging across them is nonsense."""
    import copy
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for index, ad in enumerate(ads):
        ad["type"] = "s" if index % 2 else "u"
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="can ho", limit=20)
    assert any("mix listing types" in w for w in result.warnings)


def test_single_type_results_are_not_flagged(sample_ads, detail_payload):
    """The negative side: a clean sample must not carry a scary warning."""
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for ad in ads:
        ad["type"] = "s"
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="can ho", limit=20)
    assert not any("mix listing types" in w for w in result.warnings)


def test_client_side_filters_are_declared_in_coverage(client):
    result = client.search(query="iphone", limit=10, condition="used", sort="price_desc")
    assert "condition=used" in result.coverage.client_side_filters
    assert "sort=price_desc" in result.coverage.client_side_filters


# -- validation ------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"sort": "cheapest"}, {"condition": "refurbished"},
    {"seller_type": "wholesaler"}, {"limit": 0},
    {"min_price": 5_000_000, "max_price": 1_000_000}, {"min_price": -1},
])
def test_bad_input_raises_usage_error(client, kwargs):
    with pytest.raises(UsageError):
        client.search(query="x", **kwargs)


def test_validated_facets_are_forwarded_verbatim(client, fake_transport):
    """Facet names/values are validated by chotot.facets before this point;
    the client's job is only to place them on the query string."""
    client.search(query="x", facets={"mobile_brand": "1"}, limit=5)
    assert _sent(fake_transport)[0]["mobile_brand"] == "1"


# -- ids and sellers -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (134348455, 134348455),
    ("134348455", 134348455),
    ("https://www.chotot.com/134348455.htm", 134348455),
    ("https://www.chotot.com/mua-ban-dien-thoai/134348455.htm", 134348455),
])
def test_listing_id_parsing(value, expected):
    assert parse_listing_id(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "https://www.chotot.com/"])
def test_unparseable_listing_id_raises(value):
    with pytest.raises(UsageError):
        parse_listing_id(value)


def test_seller_accepts_both_the_numeric_id_and_the_oid(client):
    """The storefront endpoint takes either; ad-listing?account_id= took only one."""
    for key in (32068064, "61d4cb0a4ae54ff54d7dafb17cb413a1"):
        storefront = client.seller_listings(key, limit=5)
        assert storefront.listings


def test_seller_uses_the_storefront_endpoint(client, fake_transport):
    client.seller_listings(32068064, limit=5)
    paths = [r["path"] for r in fake_transport.requests]
    assert any(p.startswith("theia/") for p in paths), paths


def test_seller_reads_the_total_before_fetching(client, fake_transport):
    """Storefront pagination is ignored upstream, so the size must be known first."""
    client.seller_listings(32068064)
    limits = [r["params"].get("limit") for r in fake_transport.requests]
    assert limits[0] == 1, "the cheap probe for `total` is missing"


def test_seller_with_no_listings_raises_not_found(client):
    """A well-formed id with an empty storefront is a 'not found', not a usage error."""
    with pytest.raises(NotFoundError):
        client.seller_listings("0")


@pytest.mark.parametrize("hostile", [
    "../ad-listing/1", "1?limit=99", "../../etc/passwd", "abc", "", "1 2",
])
def test_malformed_seller_ids_are_refused_before_the_url_is_built(client, fake_transport, hostile):
    """These are interpolated into a PATH segment; an unvalidated one can add
    query parameters or walk to a different endpoint."""
    with pytest.raises(UsageError):
        client.seller_listings(hostile)
    assert not fake_transport.requests, "a request was issued for a hostile id"


@pytest.mark.parametrize("hostile", ["../../ad-listing/1", "x?a=b", "", "a/b"])
def test_malformed_shop_aliases_are_refused(client, fake_transport, hostile):
    with pytest.raises(UsageError):
        client.shop_profile(hostile)
    assert not fake_transport.requests


def test_shop_phone_redaction_reveals_no_trailing_digits(client):
    """The listing API masks the TAIL, so revealing a suffix here publishes more
    than upstream does, and the two together reconstruct the number."""
    payload = client.shop_profile("someshop").to_dict()
    original = client.shop_profile("someshop").phones[0]
    for redacted in payload["phones"]:
        assert not redacted.endswith(original[-1]), "a trailing digit survived redaction"
        # No suffix of the original may appear in the redacted form.
        for length in range(1, len(original)):
            assert original[-length:] not in redacted


def test_storefront_type_vocabulary_is_normalised(sample_ads, detail_payload):
    """theia says 'sell'/'let' where ad-listing says 's'/'u'.

    Left unnormalised the mixed sale/rent guard never fires on storefront data.
    """
    import copy
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 40)
    for index, ad in enumerate(ads):
        ad["type"] = "sell" if index % 2 else "let"
    storefront = ChototClient(transport=FakeTransport(ads, detail_payload)).seller_listings(1)
    kinds = {x.listing_type for x in storefront.listings}
    assert kinds <= {"s", "u"}, f"un-normalised theia vocabulary leaked: {kinds}"
    assert kinds == {"s", "u"}


def test_storefront_string_prices_become_integers(client):
    """theia returns price as a string; leaving it breaks every statistic."""
    storefront = client.seller_listings(32068064, limit=5)
    assert all(x.price is None or isinstance(x.price, int) for x in storefront.listings)


def test_storefront_does_not_report_the_lookup_key_as_an_oid(client):
    """theia echoes the key back in account_oid; a numeric id is not an OID."""
    storefront = client.seller_listings(32068064, limit=5)
    assert storefront.account_oid != "32068064"


def test_shop_profile_redacts_phones_by_default(client):
    profile = client.shop_profile("someshop")
    payload = profile.to_dict()
    assert payload["phones_redacted"] is True
    assert all("*" in p for p in payload["phones"])
    assert "0909937666" not in json.dumps(payload)


def test_shop_profile_reveals_phones_only_on_request(client):
    payload = client.shop_profile("someshop").to_dict(reveal_contact=True)
    assert "0909937666" in payload["phones"]


def test_missing_listing_raises_not_found(client):
    with pytest.raises(NotFoundError):
        client.get_listing(1)


# -- merged-province fan-out ----------------------------------------------
#
# There was no coverage here at all, which is exactly why `--region hcm`
# shipped returning 20 listings from Bà Rịa-Vũng Tàu and none from Saigon.

class RegionAwareTransport:
    """Serves a different corpus per region_v2, with per-region totals.

    A fake that ignores region_v2 cannot express the defect this guards: every
    region would return the same rows and any allocation would look correct.
    """

    def __init__(self, sizes):
        self.sizes = sizes  # {region_v2: total}
        self.requests = []
        self.request_count = 0

    def get_json(self, path, params=None):
        params = dict(params or {})
        self.requests.append({"path": path, "params": params})
        self.request_count += 1
        region = params.get("region_v2")
        total = self.sizes.get(region, 0)
        offset = int(params.get("o", 0))
        limit = min(int(params.get("limit", 20)), 50)
        rows = []
        for index in range(offset, min(offset + limit, total)):
            rows.append({
                "list_id": (region or 0) * 100_000 + index,
                "subject": f"item {index}", "price": 1_000_000 + index,
                "region_v2": region, "elt_condition": 2,
                "list_time": 1787892561000, "category": 5010, "type": "s",
            })
        payload = {"ads": rows}
        if total:
            payload["total"] = total
        return payload


HCM_SIZES = {2010: 178, 2011: 893, 13000: 10_000}


def _hcm_client(monkeypatch, sizes=None):
    transport = RegionAwareTransport(sizes or HCM_SIZES)
    return ChototClient(transport=transport), transport


def test_merged_province_returns_the_main_city_not_only_the_annexed_ones(monkeypatch):
    """The regression: codes sort ascending, so HCM proper (13000) is LAST.

    Concatenating per-region pages and truncating positionally returned 20 ads
    from Vũng Tàu and none from Saigon, under a warning saying all codes were
    merged.
    """
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=20)
    regions = {x.region_v2 for x in result.listings}
    assert 13000 in regions, f"Ho Chi Minh City is absent; got {regions}"


def test_merged_province_sample_is_proportional_to_region_size(monkeypatch):
    """HCM proper is >90% of the province, so it must dominate the sample.

    An equal three-way split would make every price statistic for the city
    56% not-the-city.
    """
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=100)
    from collections import Counter

    share = Counter(x.region_v2 for x in result.listings)
    assert share[13000] / len(result.listings) > 0.6, share


def test_every_legacy_code_is_represented_when_the_limit_allows(monkeypatch):
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=200)
    assert {x.region_v2 for x in result.listings} == set(HCM_SIZES)


def test_upstream_total_sums_regions_once_not_once_per_page(monkeypatch):
    """`total` is a property of the query, not the offset.

    Appending per-page totals reported 11,071 matches as 22,142.
    """
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=200)
    assert result.coverage.reported_total == sum(HCM_SIZES.values())


def test_single_region_total_is_not_multiplied_either(monkeypatch):
    client, transport = _hcm_client(monkeypatch, {12000: 6138})
    result = client.search(query="iphone", region="hanoi", limit=200)
    assert result.coverage.reported_total == 6138


def test_coverage_reports_the_codes_actually_queried(monkeypatch):
    """A budget that ends the fan-out early must not claim full coverage."""
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=200, max_requests=2)
    assert len(result.coverage.region_codes) <= 2
    assert any("budget" in w for w in result.warnings)


def test_exhaustion_is_an_and_across_regions(monkeypatch):
    """One tiny region running dry says nothing about the province."""
    client, _ = _hcm_client(monkeypatch, {2010: 5, 13000: 10_000})
    result = client.search(query="iphone", region="hcm", limit=60)
    assert result.coverage.exhausted is False


def test_exhaustion_is_true_when_every_region_is_dry(monkeypatch):
    client, _ = _hcm_client(monkeypatch, {2010: 3, 2011: 4, 13000: 5})
    result = client.search(query="iphone", region="hcm", limit=200)
    assert result.coverage.exhausted is True
    assert len(result.listings) == 12


def test_inapplicable_condition_filter_names_the_real_cause(sample_ads, detail_payload):
    """Property and job ads carry no condition at all.

    "The pool did not contain enough matches" is true and useless there: the
    filter is inapplicable, not unlucky, and the user reads 0 results as "there
    is nothing for sale".
    """
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for ad in ads:
        ad.pop("elt_condition", None)
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="can ho", condition="used", limit=20)
    assert result.listings == []
    assert any("state no condition" in w for w in result.warnings), result.warnings


def test_partial_pool_still_reports_the_ordinary_shortfall(sample_ads, detail_payload):
    """The negative side: a genuinely thin pool must not get the wrong message."""
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for index, ad in enumerate(ads):
        ad["elt_condition"] = 1 if index < 3 else 2
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="iphone", condition="new", limit=20)
    assert len(result.listings) == 3
    assert not any("state no condition" in w for w in result.warnings)


# -- round-2 regressions ---------------------------------------------------

def test_client_side_filter_crawl_counts_surviving_rows(sample_ads, detail_payload):
    """The quota must count rows that will SURVIVE the filter.

    Counting raw rows made `--condition new --limit 20` stop after two requests
    with one result, while 92 of 100 fetched rows were about to be discarded.
    """
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 600)
    for index, ad in enumerate(ads):
        ad["elt_condition"] = 1 if index % 10 == 0 else 2  # 10% are "new"
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="iphone", condition="new", limit=20, max_requests=24)
    assert len(result.listings) == 20, (
        f"asked for 20 'new' listings, got {len(result.listings)}")


def test_district_resolves_inside_a_merged_province(monkeypatch):
    """A merged province has several codes; resolving against only the first
    made a real district fail as ambiguous, with a remedy naming the --region
    the user had already passed."""
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", district="quan go vap", limit=5)
    assert result is not None  # resolution did not raise


def test_first_round_queries_the_province_code_before_the_annexed_ones(monkeypatch):
    """Codes sort ascending and the main city sorts last, so a low budget
    otherwise spends everything on the annexed provinces."""
    client, transport = _hcm_client(monkeypatch)
    client.search(query="iphone", region="hcm", limit=20, max_requests=1)
    first = transport.requests[0]["params"]["region_v2"]
    assert first == 13000, f"first request went to {first}, not the city"


def test_a_capped_total_outweighs_a_large_uncapped_one(monkeypatch):
    """A `total` at the cap is a FLOOR, not a magnitude.

    Weighting 10,000 (capped, could be 10x that) as merely marginally larger
    than 9,000 (a real count) gives the two regions almost equal shares -- the
    equal split this fan-out exists to avoid.
    """
    from collections import Counter

    client, _ = _hcm_client(monkeypatch, {2010: 9_000, 13000: 10_000})
    result = client.search(query="iphone", region="hcm", limit=100)
    share = Counter(x.region_v2 for x in result.listings)
    assert share[13000] / len(result.listings) > 0.6, (
        f"capped region got only {share[13000]}/{len(result.listings)}; "
        f"a floor was weighted as a count")


def test_multiple_capped_regions_are_declared_as_unknown_sizes(monkeypatch):
    client, _ = _hcm_client(monkeypatch, {2011: 10_000, 13000: 10_000, 2010: 178})
    result = client.search(query="iphone", region="hcm", limit=4)
    assert 13000 in {x.region_v2 for x in result.listings}
    assert any("cap" in w for w in result.warnings)


def test_whitespace_only_query_is_refused_without_a_request(client, fake_transport):
    with pytest.raises(UsageError):
        client.search(query="   ", limit=5)
    assert not fake_transport.requests


def test_seller_limit_zero_is_refused_rather_than_meaning_everything(client):
    """`limit or total` read an explicit 0 as unset and fetched the lot."""
    with pytest.raises(UsageError):
        client.seller_listings(32068064, limit=0)


# -- what MUST be sent -----------------------------------------------------
#
# The suite proved exhaustively what must NOT reach the wire, and never that the
# working parameters do. A dropped `cg` or `area_v2` silently widens the search
# to the whole marketplace and returns a plausible answer to a different question.

@pytest.mark.parametrize("kwargs,expected", [
    ({"query": "iphone"}, {"q": "iphone"}),
    ({"category": "phone"}, {"cg": 5010}),
    ({"region": "hanoi"}, {"region_v2": 12000}),
    ({"min_price": 1_000_000, "max_price": 5_000_000}, {"price": "1000000-5000000"}),
    ({"listing_type": "rent"}, {"st": "u"}),
    ({"account_id": 42}, {"account_id": 42}),
    ({"facets": {"mobile_brand": "1"}}, {"mobile_brand": "1"}),
])
def test_supported_parameters_actually_reach_the_wire(client, fake_transport, kwargs, expected):
    client.search(limit=5, **kwargs)
    sent = fake_transport.requests[0]["params"]
    for name, value in expected.items():
        assert sent.get(name) == value, f"{name} missing or wrong: sent {sent}"


def test_district_reaches_the_wire_as_area_v2(client, fake_transport):
    client.search(query="x", region="hcm", district="quan go vap", limit=5)
    assert fake_transport.requests[0]["params"]["area_v2"] == 13110


def test_district_narrows_the_fanout_to_its_own_region(client, fake_transport):
    """A district belongs to one legacy region; the gateway ANDs the two, so a
    sibling request is structurally guaranteed to return zero."""
    client.search(query="x", region="hcm", district="quan go vap", limit=5)
    regions = {r["params"].get("region_v2") for r in fake_transport.requests}
    assert regions == {13000}, f"queried impossible pairs: {regions}"


# -- client-side ordering and filtering ------------------------------------

def test_newest_and_oldest_actually_order_by_time(sample_ads, detail_payload):
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for index, ad in enumerate(ads):
        ad["list_time"] = 1_700_000_000_000 + index * 86_400_000
    transport_new = FakeTransport(ads, detail_payload)
    newest = ChototClient(transport=transport_new).search(query="x", limit=10, sort="newest")
    stamps = [x.posted_at for x in newest.listings]
    assert stamps == sorted(stamps, reverse=True), "newest was not newest-first"

    oldest = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="x", limit=10, sort="oldest")
    stamps = [x.posted_at for x in oldest.listings]
    assert stamps == sorted(stamps), "oldest was not oldest-first"


def test_price_desc_orders_by_price(client):
    result = client.search(query="x", limit=10, sort="price_desc")
    prices = [x.price for x in result.listings if x.has_price]
    assert prices == sorted(prices, reverse=True)


@pytest.mark.parametrize("seller_type,expected", [("shop", True), ("individual", False)])
def test_seller_type_filter_actually_partitions(sample_ads, detail_payload, seller_type, expected):
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for index, ad in enumerate(ads):
        ad["company_ad"] = index % 2 == 0
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="x", limit=15, seller_type=seller_type)
    assert result.listings
    assert all(x.is_company_ad is expected for x in result.listings)


def test_budget_warning_is_matched_by_its_own_words(client):
    """Graded by a substring a different, generic warning also satisfies, the
    test would pass with the specific warning removed."""
    result = client.search(query="iphone", region="hcm", limit=200, max_requests=2)
    budget = [w for w in result.warnings if "Request budget" in w and "region code" in w]
    assert budget, f"the region-budget warning is missing: {result.warnings}"


def test_merger_warning_states_what_was_actually_queried(monkeypatch):
    """It used to assert "all were queried and merged" before the crawl ran, and
    then contradict itself three lines later."""
    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=200, max_requests=1)
    merged = [w for w in result.warnings if "legacy region codes" in w]
    assert merged
    assert "1 of them were queried" in merged[0], merged[0]


def test_storefront_listings_get_real_timestamps(sample_ads, detail_payload):
    """theia states only "2 giờ trước" and never list_time, so without parsing
    it every seller row had a null timestamp and an untranslated string."""
    import copy

    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 10)
    for ad in ads:
        ad.pop("list_time", None)
        ad["date"] = "3 giờ trước"
    storefront = ChototClient(transport=FakeTransport(ads, detail_payload)).seller_listings(1)
    assert storefront.listings
    assert all(x.posted_at is not None for x in storefront.listings), \
        "no storefront listing got a timestamp"


def test_unparseable_relative_age_stays_none_rather_than_guessing(sample_ads, detail_payload):
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 5)
    for ad in ads:
        ad.pop("list_time", None)
        ad["date"] = "vừa xong"
    storefront = ChototClient(transport=FakeTransport(ads, detail_payload)).seller_listings(1)
    assert all(x.posted_at is None for x in storefront.listings)


def test_a_requested_listing_type_is_verified_against_the_rows(sample_ads, detail_payload):
    """A request is not a receipt.

    Gating the mix check on `listing_type == "any"` trusted `st` to have worked
    -- and this gateway's signature failure is accepting a parameter and
    ignoring it. So the one place the tool could present unfiltered data as
    filtered was the branch where the user had ASKED for a filter.
    """
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for index, ad in enumerate(ads):
        ad["type"] = "s" if index % 2 else "u"  # transport ignores `st`
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="can ho", limit=20, listing_type="sale")
    assert any("no longer being honoured" in w for w in result.warnings), result.warnings
    assert any("UNFILTERED" in w for w in result.warnings)


def test_an_honoured_listing_type_produces_no_alarm(sample_ads, detail_payload):
    """The negative side: a working filter must not cry wolf."""
    from tests.conftest import FakeTransport, expand_ads

    ads = expand_ads(sample_ads, 60)
    for ad in ads:
        ad["type"] = "s"
    result = ChototClient(transport=FakeTransport(ads, detail_payload)).search(
        query="can ho", limit=20, listing_type="sale")
    assert not any("no longer being honoured" in w for w in result.warnings)


def test_a_budget_caused_floor_is_not_blamed_on_the_upstream_cap(monkeypatch):
    """1,165 is nowhere near the 10,000 cap; saying "upstream cap" was false."""
    client, _ = _hcm_client(monkeypatch, {2010: 178, 2011: 893, 13000: 94})
    result = client.search(query="iphone", region="hcm", limit=200, max_requests=1)
    assert result.coverage.total_is_capped is True
    assert result.coverage.total_floor_reason == "regions_skipped"


def test_a_genuine_cap_is_still_attributed_to_the_gateway(monkeypatch):
    client, _ = _hcm_client(monkeypatch, {13000: 10_000})
    result = client.search(query="iphone", region="hanoi", limit=5)
    if result.coverage.total_is_capped:
        assert result.coverage.total_floor_reason == "upstream_cap"


@pytest.mark.parametrize("limit", [2, 3, 4, 8, 20, 60])
def test_the_proportional_draw_holds_at_small_limits_too(monkeypatch, limit):
    """HCM proper is ~90% of the province, at every result count.

    Serving every region once before any is served twice made `--limit 3` return
    a third of its rows from the city -- proportionally wrong precisely where a
    user looks first.
    """
    from collections import Counter

    client, _ = _hcm_client(monkeypatch)
    result = client.search(query="iphone", region="hcm", limit=limit)
    share = Counter(x.region_v2 for x in result.listings)
    assert share[13000] / len(result.listings) >= 0.8, (
        f"limit={limit}: city got {share[13000]}/{len(result.listings)} — {dict(share)}")


# -- the gateway unions query terms; the client must not pretend otherwise ----
#
# Measured 2026-08-28: q="canon"=281, q="v1"=39, q="canon powershot"=1, and
# q="canon powershot v1"=40 -- exactly 39+1. Adding a word WIDENS the result
# set and the least specific word dominates the ranking, so `analyze` reported
# a 17,500,000d median for a camera that had zero listings in that city.

def _pool(sample_ads, subjects):
    """A pool whose ad text is under the test's control."""
    from tests.conftest import expand_ads

    ads = expand_ads(sample_ads, len(subjects))
    for ad, subject in zip(ads, subjects):
        ad["subject"] = subject
        ad["body"] = ""
    return ads


def test_query_terms_splits_and_folds_vietnamese():
    from chotot.client import query_terms

    assert query_terms("canon powershot v1") == ["canon", "powershot", "v1"]
    assert query_terms("Máy ảnh Đà Nẵng") == ["may", "anh", "da", "nang"]
    assert query_terms(None) == []
    # Single characters match nearly every ad, so they cannot separate a
    # relevant result from an unrelated one and must not drive the guard.
    assert query_terms("a b") == []


def test_coverage_guard_fires_when_no_row_carries_every_term(sample_ads, detail_payload):
    """The failure that shipped: 40 motorbikes returned for a camera query."""
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Honda Winner V1"] * 40)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon powershot v1", limit=10)

    assert result.listings, "the guard must not depend on an empty result"
    warning = " ".join(result.warnings)
    assert "NONE of the" in warning
    assert "unions search terms" in warning


def test_coverage_guard_is_silent_when_every_row_matches(sample_ads, detail_payload):
    """The other direction: a warning that fires on healthy input gets muted."""
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Canon PowerShot V1 den"] * 40)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon powershot v1", limit=10)

    assert not any("unions search terms" in w for w in result.warnings)


def test_coverage_guard_reports_the_partial_count(sample_ads, detail_payload):
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Canon PowerShot V1"] * 5 + ["Honda Winner V1"] * 35)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon powershot v1", limit=40)

    assert any("Only 5 of 40 results contain all of" in w for w in result.warnings)


def test_single_word_query_never_triggers_the_guard(sample_ads, detail_payload):
    """One term cannot be unioned with anything, so the warning would be noise."""
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Honda Winner V1"] * 40)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon", limit=10)

    assert not any("unions search terms" in w for w in result.warnings)


def test_match_all_keeps_only_rows_carrying_every_term(sample_ads, detail_payload):
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Canon PowerShot V1"] * 3 + ["Honda Winner V1"] * 37)
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon powershot v1", limit=40, match_all=True)

    assert len(result.listings) == 3
    assert all("PowerShot" in x.subject for x in result.listings)


def test_match_all_matches_against_the_body_too(sample_ads, detail_payload):
    """Sellers put the model in the description; ignoring it would over-filter."""
    from tests.conftest import FakeTransport

    ads = _pool(sample_ads, ["Canon PowerShot"] * 4)
    for ad in ads[:2]:
        ad["body"] = "ban may anh V1 con bao hanh"
    client = ChototClient(transport=FakeTransport(ads, detail_payload))
    result = client.search(query="canon powershot v1", limit=10, match_all=True)

    assert len(result.listings) == 2


# -- the crawl budget must account for EVERY client-side filter ---------------
#
# This has now cost two bugs with the same shape. `--condition new --limit 20`
# once stopped after two requests with one result, because the budget counted
# raw rows while 92 of 100 were about to be discarded. That was fixed by
# counting survivors -- but the survivor count RE-STATED the filters instead of
# reusing the predicate that builds the result, so when `--match-all` was added
# it was registered in `client_side_filters`, applied to the output, and left
# out of the budget: one request, 50 rows fetched, 49 dropped, 1 returned of
# the 10 asked for.
#
# The parametrisation below is therefore checked against the client's own
# declared filter list, so adding a fourth filter fails this file rather than
# silently shipping the same bug a third time.

#: filter name -> (search kwargs, how to mark an ad as surviving it)
ROW_DISCARDING_FILTERS = {
    "condition": ({"condition": "new"}, lambda ad: ad.update(elt_condition=1)),
    "seller_type": ({"seller_type": "shop"}, lambda ad: ad.update(company_ad=True)),
    "match_all": ({"match_all": True}, lambda ad: ad.update(subject="canon powershot v1")),
}


def _mostly_discarded_pool(sample_ads, mark_survivor, survivors=12, total=300):
    """A pool of `total` rows where only `survivors` pass the filter."""
    from tests.conftest import expand_ads

    ads = expand_ads(sample_ads, total)
    for ad in ads:
        ad["elt_condition"] = 2          # used
        ad["company_ad"] = False         # individual
        ad["subject"] = "Honda Winner V1"
        ad["body"] = ""
    # Spread survivors across the pool so they are not all on page one.
    step = max(1, total // survivors)
    for index in range(0, total, step):
        mark_survivor(ads[index])
    return ads


@pytest.mark.parametrize("name", sorted(ROW_DISCARDING_FILTERS))
def test_crawl_budget_counts_surviving_rows_not_fetched_rows(
    name, sample_ads, detail_payload
):
    from tests.conftest import FakeTransport

    kwargs, mark = ROW_DISCARDING_FILTERS[name]
    ads = _mostly_discarded_pool(sample_ads, mark)
    transport = FakeTransport(ads, detail_payload)
    result = ChototClient(transport=transport).search(
        query="canon powershot v1", limit=10, max_requests=24, **kwargs
    )

    assert transport.request_count > 1, (
        f"--{name} stopped after one request: the budget counted rows it was "
        f"about to discard"
    )
    assert len(result.listings) == 10, (
        f"--{name} returned {len(result.listings)} of the 10 requested"
    )


def test_every_row_discarding_filter_is_exercised_above(sample_ads, detail_payload):
    """The denominator guard: a new filter must be added to the table above.

    `sort` is excluded deliberately -- it reorders rows, it never removes them,
    so it cannot starve the budget.
    """
    from tests.conftest import FakeTransport

    result = ChototClient(transport=FakeTransport(sample_ads, detail_payload)).search(
        query="canon powershot v1", limit=1,
        condition="new", seller_type="shop", sort="price_asc", match_all=True,
    )
    declared = {f.split("=")[0] for f in result.coverage.client_side_filters}
    assert declared - {"sort"} == set(ROW_DISCARDING_FILTERS), (
        f"client-side filters {sorted(declared - {'sort'})} but the budget test "
        f"only covers {sorted(ROW_DISCARDING_FILTERS)}"
    )
