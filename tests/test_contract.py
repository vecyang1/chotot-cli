"""The contract module is the client's single source of truth about the API."""
from __future__ import annotations

import pytest

from chotot import contract
from chotot.errors import UnsupportedFilterError


def test_supported_and_ignored_sets_are_disjoint():
    """A parameter cannot be both honoured and ignored."""
    overlap = contract.SUPPORTED_PARAMS & set(contract.IGNORED_PARAMS)
    assert not overlap, f"parameters listed as both supported and ignored: {overlap}"


def test_price_spellings_that_do_not_work_are_all_recorded():
    """Every plausible-looking price parameter must be on the ignore list.

    These are the spellings a maintainer would reach for; each returns HTTP 200
    and an unfiltered page, so forgetting one reintroduces the original defect.
    """
    for spelling in ("sp", "ep", "minprice", "maxprice", "price_from",
                     "price_to", "fromprice", "toprice", "pf", "pt"):
        assert spelling in contract.IGNORED_PARAMS, f"{spelling} is not recorded as ignored"
    assert "price" in contract.SUPPORTED_PARAMS


@pytest.mark.parametrize("param", sorted(contract.IGNORED_PARAMS))
def test_every_ignored_param_is_refused(param):
    """The guard must fire for each ignored parameter, not just a sampled one."""
    with pytest.raises(UnsupportedFilterError) as excinfo:
        contract.assert_forwardable({param: "x"})
    assert param in str(excinfo.value)


def test_supported_params_pass_the_guard():
    """The negative side: the guard must not refuse legitimate parameters."""
    contract.assert_forwardable({p: 1 for p in contract.SUPPORTED_PARAMS})


def test_every_ignored_param_states_a_reason():
    for param, reason in contract.IGNORED_PARAMS.items():
        assert reason and len(reason) > 10, f"{param} has no usable explanation"


def test_no_server_side_sort_is_recorded():
    """If this flips, sorting should move server-side; doctor watches for it."""
    assert contract.SERVER_SIDE_SORT_AVAILABLE is False


def test_page_overlap_is_recorded_with_its_real_mechanism():
    """Ranking is deterministic; the duplicates are shard boundary bleed.

    The distinction is operational: if duplicates were time drift, crawling
    faster would reduce them. It does not -- 7.2% over 8 concurrent pages in
    0.75s -- so deduplication is mandatory at any speed.
    """
    assert contract.RANKING_IS_DETERMINISTIC_AT_A_GIVEN_OFFSET is True
    assert contract.PAGES_OVERLAP_AT_BOUNDARIES is True
    assert contract.MEASURED_DUPLICATE_RATE > 0


def test_search_window_is_larger_than_the_total_cap():
    """total saturates at 10000 but results continue to the 20000 window.

    Using the display cap as the crawl bound would silently halve reach.
    """
    assert contract.MAX_SEARCH_WINDOW > contract.TOTAL_CAP


def test_known_absent_endpoints_are_not_also_declared_live():
    live = {e.path for e in contract.ENDPOINTS.values()}
    assert not (set(contract.KNOWN_ABSENT_ENDPOINTS) & live)


def test_a_gateway_refusal_is_a_usage_error_not_a_transport_failure():
    """HTTP 400/414 are caused by what was asked for, not by the network.

    Reporting them as a transport failure sent the user to check their
    connection over a malformed query, and it silently turned a doctor check
    into a WARN when the classification changed.
    """
    import io
    import urllib.error

    from chotot.errors import UsageError
    from chotot.http import HttpTransport

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"message":"bad"}'))

    transport = HttpTransport("https://example.invalid", opener=refuse, sleep=lambda _: None)
    with pytest.raises(UsageError) as excinfo:
        transport.get_json("ad-listing", {"q": "x"})
    assert excinfo.value.exit_code == 2


def test_a_server_error_is_still_a_transport_failure():
    """The negative side: 5xx must not be reclassified as the user's fault."""
    import io
    import urllib.error

    from chotot.errors import TransportError
    from chotot.http import HttpTransport

    def fail(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO(b""))

    transport = HttpTransport("https://example.invalid", opener=fail,
                              sleep=lambda _: None, max_retries=2)
    with pytest.raises(TransportError):
        transport.get_json("ad-listing", {"q": "x"})
