"""Shared fixtures.

The fake transport below models the gateway's *misbehaviour*, not its
documentation. A double that quietly honours every parameter would make the
whole point of this client — refusing to forward filters the API ignores, and
deduplicating an unstably-ranked feed — impossible to express in a test, and the
suite would pass while the bug shipped.

So the fake:

* returns rows regardless of ``sp``/``ep``/``condition``/``company_ad``;
* rejects ``sort`` + ``direction`` with an HTTP 400, as the real one does;
* clamps ``limit`` to 50;
* omits ``total`` entirely when nothing matches;
* reports ``total`` as the 10000 cap for broad queries;
* **overlaps its offset windows**, so a crawl that forgets to deduplicate
  returns duplicate rows exactly as it would in production.
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Dict[str, Any]:
    """Decode exactly as the client does, from the captured raw bytes."""
    return json.loads((FIXTURES / name).read_bytes().decode("utf-8-sig"))


@pytest.fixture(scope="session")
def meta() -> Dict[str, Any]:
    return load_fixture("_meta.json")


@pytest.fixture(scope="session")
def search_payload() -> Dict[str, Any]:
    return load_fixture("search_iphone.json")


@pytest.fixture(scope="session")
def detail_payload() -> Dict[str, Any]:
    return load_fixture("detail_listing.json")


@pytest.fixture(scope="session")
def sample_ads(search_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return search_payload["ads"]


class FakeTransport:
    """Stands in for :class:`chotot.http.HttpTransport`.

    Records every request so tests can assert on what was *not* sent, which a
    stub that only returns a value cannot express.
    """

    #: Fraction of a requested offset that is actually honoured, so windows
    #: overlap at EVERY offset rather than only at a fixed stride.
    #:
    #: This matters more than it looks. Modelling overlap as a fixed backward
    #: shift (``start = offset - 10``) produces overlap only between windows
    #: exactly 50 apart, so a client that jumps 0 -> 100 -> 150 sees none, and
    #: two tests named for deduplication ran with `duplicates_dropped == 0` --
    #: green over a defect that halved every result set in production.
    OVERLAP_RATIO = 0.92

    def __init__(self, ads: List[Dict[str, Any]], detail: Optional[Dict[str, Any]] = None,
                 total: Optional[int] = 10_000, stale_after: Optional[int] = None) -> None:
        self._ads = ads
        self._detail = detail
        self._total = total
        #: After this many pages, keep returning the FIRST window forever. A
        #: saturated feed does exactly this: rows keep coming and none of them
        #: are new. Without it every page carries something fresh, and a crawler
        #: that never checks for a stale window still terminates -- which makes
        #: the "stop when nothing is new" test pass for the wrong reason.
        self._stale_after = stale_after
        self._search_calls = 0
        self.requests: List[Dict[str, Any]] = []
        #: Every search offset requested, so a test can assert the crawl walks
        #: 0, 50, 100 ... instead of skipping or repeating pages.
        self.offsets: List[int] = []
        self.request_count = 0
        self.fail_with: Optional[Exception] = None

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from chotot.errors import NotFoundError, TransportError

        params = dict(params or {})
        self.requests.append({"path": path, "params": params})
        self.request_count += 1
        if self.fail_with is not None:
            raise self.fail_with

        if path.startswith("theia/"):
            key = path.split("/", 1)[1]
            if key == "0":
                return {"ads": [], "total": 0}
            rows = self._ads[: min(int(params.get("limit", 20)), len(self._ads))]
            # theia nests under `info`, returns price as a STRING, and echoes
            # the lookup key back as account_oid -- all three are real.
            ads = []
            for ad in rows:
                info = dict(ad)
                info["price"] = str(ad.get("price") or "")
                info["account_oid"] = key
                ads.append({"info": info})
            return {"ads": ads, "total": len(self._ads),
                    "paging": {"currPage": 1, "perPage": len(ads), "total": len(self._ads)}}

        if path.startswith("shops/alias/"):
            return {
                "name": "Test Shop", "alias": path.rsplit("/", 1)[1], "isVerified": True,
                "phoneNumber": "0909937666", "additionalPhone1": "0909937667",
                "categoryId": 5000, "accountOid": ["abc123"], "createdDate": "2020-01-01T00:00:00.000",
                "address": "1 Test St", "description": "d",
                "shopAds": {"total": len(self._ads),
                            "ads": [{"info": {**a, "price": str(a.get("price") or "")}}
                                    for a in self._ads[:5]]},
            }

        if path.startswith("ad-listing/"):
            listing_id = path.split("/", 1)[1]
            if self._detail is None or listing_id in ("1", "999999999"):
                raise NotFoundError(f"Not found: /{path}", remedy="Check the id.")
            return self._detail

        # The real gateway 400s on sort+direction TOGETHER. `direction` alone is
        # a working property facet ("Hướng cửa chính"), so refusing it
        # unconditionally made the fake disagree with the contract and hid the
        # facet path from every test.
        if "sort" in params and "direction" in params:
            raise TransportError("Chợ Tốt gateway returned HTTP 400", remedy="no server sort")

        limit = min(int(params.get("limit", 20)), 50)
        offset = int(params.get("o", 0))
        self._search_calls += 1
        if self._stale_after is not None and self._search_calls > self._stale_after:
            offset = 0  # every further page repeats rows already seen

        # Overlapping windows at any offset: a naive crawler sees ids twice,
        # and a crawler that derives its next offset from the surviving row
        # count stalls and re-requests a window it already has.
        self.offsets.append(offset)
        start = int(offset * self.OVERLAP_RATIO)
        window = self._ads[start:start + limit]

        if params.get("q") == "zzzznonexistentqueryxyz":
            return {"ads": []}  # note: no "total" key at all
        if not window:
            return {"ads": []}
        payload: Dict[str, Any] = {"ads": window}
        if self._total is not None:
            payload["total"] = self._total
        return payload


def expand_ads(ads: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """Grow the captured ads into a pool big enough to paginate.

    The real fixture holds 20 ads. With a 50-row page size the crawler issued a
    single request, so the overlap the fake models was never reached and the
    deduplication test could not fail — it passed for want of a second page.
    Ids and prices are varied so duplicates are detectable and statistics remain
    meaningful.
    """
    grown: List[Dict[str, Any]] = []
    for index in range(count):
        source = dict(ads[index % len(ads)])
        source["list_id"] = 900_000_000 + index
        base = source.get("price") or 1_000_000
        source["price"] = int(base) + (index % 37) * 25_000
        grown.append(source)
    return grown


@pytest.fixture()
def fake_transport(sample_ads, detail_payload) -> FakeTransport:
    return FakeTransport(expand_ads(sample_ads, 300), detail_payload)


@pytest.fixture()
def client(fake_transport):
    from chotot.client import ChototClient

    return ChototClient(transport=fake_transport)
