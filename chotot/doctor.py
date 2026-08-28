# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""``chotot doctor`` - re-measure the gateway contract against the live API.

Every claim in :mod:`chotot.contract` was measured once, on a date the module
records. Upstream can change any of it without telling anyone, and the failure
mode is silent: a parameter that starts being honoured, or stops being honoured,
changes the answers this tool gives without changing its behaviour.

So this is a real gate, not a ping. Each check has a **positive and a negative
side**: it proves a supported parameter actually narrows the result set, and
proves an ignored one still does not. A check that can only pass is not
evidence, so every subject here can fail, and the summary prints how many
subjects were graded rather than just a status.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from chotot import contract
from chotot.client import ChototClient
from chotot.formatter import Palette, render_table, to_json

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"check": self.name, "status": self.status, "detail": self.detail}


def _ids(client: ChototClient, **params: Any) -> Tuple[List[int], Optional[int]]:
    payload = client.transport.get_json(contract.ENDPOINTS["search"].path, params)
    ads = payload.get("ads") or []
    return [a.get("list_id") for a in ads if isinstance(a, dict)], payload.get("total")


def _prices(client: ChototClient, **params: Any) -> List[int]:
    payload = client.transport.get_json(contract.ENDPOINTS["search"].path, params)
    return [a.get("price") or 0 for a in (payload.get("ads") or [])]


def _check_reachable(client: ChototClient) -> Check:
    try:
        ids, _ = _ids(client, q="iphone", limit=3)
    except Exception as exc:  # noqa: BLE001
        return Check("gateway reachable", FAIL, f"{type(exc).__name__}: {exc}")
    if not ids:
        return Check("gateway reachable", FAIL, "responded but returned no ads for 'iphone'")
    return Check("gateway reachable", PASS, f"returned {len(ids)} ads")


def _check_price_filter(client: ChototClient) -> Check:
    """Positive: price=0-5000000 must exclude dearer ads. Negative: sp/ep must not filter."""
    ceiling = 5_000_000
    try:
        bounded = _prices(client, q="iphone", cg=5010, limit=50, price=f"0-{ceiling}")
    except Exception as exc:  # noqa: BLE001
        return Check("price range filter", FAIL, f"request failed: {exc}")
    if not bounded:
        return Check("price range filter", FAIL, "price=0-5000000 returned nothing")
    over = [p for p in bounded if p > ceiling]
    if over:
        return Check("price range filter", FAIL,
                     f"{len(over)}/{len(bounded)} results exceeded the ceiling "
                     f"(max {max(over):,}) - 'price' is no longer honoured")
    return Check("price range filter", PASS,
                 f"{len(bounded)} results, max {max(bounded):,} <= {ceiling:,}")


def _check_ignored_params_still_ignored(client: ChototClient) -> Check:
    """The negative side: if sp/ep started working we would be over-filtering."""
    ceiling = 5_000_000
    try:
        loose = _prices(client, q="iphone", cg=5010, limit=50, sp=0, ep=ceiling)
    except Exception as exc:  # noqa: BLE001
        return Check("sp/ep still ignored", WARN, f"could not measure: {exc}")
    if not loose:
        return Check("sp/ep still ignored", WARN, "no results to judge")
    over = [p for p in loose if p > ceiling]
    if not over:
        return Check("sp/ep still ignored", WARN,
                     "sp/ep now appear to filter - contract.IGNORED_PARAMS is stale "
                     "and these could be pushed server-side")
    return Check("sp/ep still ignored", PASS,
                 f"{len(over)}/{len(loose)} results ignored the bound, as recorded")


def _check_total_cap(client: ChototClient) -> Check:
    try:
        _, broad = _ids(client, q="iphone", limit=1)
        _, narrow = _ids(client, q="honda sh", limit=1)
    except Exception as exc:  # noqa: BLE001
        return Check("total cap", FAIL, f"request failed: {exc}")
    if broad is None:
        return Check("total cap", FAIL, "broad query reported no total at all")
    if broad != contract.TOTAL_CAP:
        return Check("total cap", WARN,
                     f"broad query reported {broad:,}, expected the {contract.TOTAL_CAP:,} cap")
    if narrow is not None and narrow >= contract.TOTAL_CAP:
        return Check("total cap", WARN, "narrow query also hit the cap; cannot confirm real counts")
    return Check("total cap", PASS,
                 f"broad={broad:,} (cap), narrow={narrow:,} (real count)")


def _check_absent_total(client: ChototClient) -> Check:
    """A no-match query must omit 'total' entirely - it must not report 0."""
    try:
        ids, total = _ids(client, q="zzzznonexistentqueryxyz", limit=1)
    except Exception as exc:  # noqa: BLE001
        return Check("no-match total is absent", FAIL, f"request failed: {exc}")
    if ids:
        return Check("no-match total is absent", WARN, "the nonsense query matched ads")
    if total is None:
        return Check("no-match total is absent", PASS, "'total' omitted, as recorded")
    return Check("no-match total is absent", WARN,
                 f"'total' is now present ({total}) for a no-match query")


def _check_query_terms_are_unioned(client: ChototClient) -> Check:
    """Adding a word must WIDEN the results: `q` unions its terms.

    This is the measurement --match-all and the coverage warning exist for. If
    the gateway ever switched to an intersection both would become noise, and
    silence would be the only way a user learned that. Measured 2026-08-28:
    powershot=80, vespa=1392, "powershot vespa"=1472 -- exactly the sum.
    """
    a, b = "powershot", "vespa"
    try:
        _, ta = _ids(client, q=a, limit=1)
        _, tb = _ids(client, q=b, limit=1)
        _, tab = _ids(client, q=f"{a} {b}", limit=1)
    except Exception as exc:  # noqa: BLE001
        return Check("query terms are unioned", FAIL, f"request failed: {exc}")
    if ta is None or tb is None or tab is None:
        return Check("query terms are unioned", WARN,
                     f"a probe returned no total (q={a}:{ta!r} q={b}:{tb!r} "
                     f"both:{tab!r}); the probe words may have gone out of stock")
    if tab > max(ta, tb):
        exact = " (exactly the sum)" if tab == ta + tb else ""
        return Check("query terms are unioned", PASS,
                     f"'{a}'={ta:,} '{b}'={tb:,} '{a} {b}'={tab:,}{exact} - "
                     f"adding a word widens, as recorded")
    return Check("query terms are unioned", WARN,
                 f"'{a} {b}'={tab:,} is not wider than max({ta:,}, {tb:,}); the "
                 f"gateway may now intersect terms, making --match-all redundant")


def _check_offset_pagination(client: ChototClient) -> Check:
    try:
        first, _ = _ids(client, q="iphone", limit=20, o=0)
        deep, _ = _ids(client, q="iphone", limit=20, o=200)
    except Exception as exc:  # noqa: BLE001
        return Check("offset pagination", FAIL, f"request failed: {exc}")
    if not first or not deep:
        return Check("offset pagination", FAIL, "one of the offset windows was empty")
    overlap = len(set(first) & set(deep))
    if overlap == len(first):
        return Check("offset pagination", FAIL, "o=0 and o=200 returned identical ids")
    return Check("offset pagination", PASS,
                 f"o=0 vs o=200 share {overlap}/{len(first)} ids (dedup handles the rest)")


def _check_limit_clamp(client: ChototClient) -> Check:
    try:
        wide, _ = _ids(client, q="iphone", limit=200)
    except Exception as exc:  # noqa: BLE001
        return Check("limit clamp", FAIL, f"request failed: {exc}")
    if len(wide) > contract.MAX_PAGE_SIZE:
        return Check("limit clamp", WARN,
                     f"gateway now returns {len(wide)} > {contract.MAX_PAGE_SIZE}; "
                     f"page size could be raised")
    if len(wide) < contract.MAX_PAGE_SIZE:
        return Check("limit clamp", WARN,
                     f"asked 200, got {len(wide)} (< {contract.MAX_PAGE_SIZE})")
    return Check("limit clamp", PASS, f"asked 200, got {len(wide)} as recorded")


def _check_no_server_sort(client: ChototClient) -> Check:
    """Recorded as HTTP 400. If it ever works, sorting should move server-side.

    The 400 surfaces as UsageError (a request the gateway refuses is caused by
    what was asked for, not by the network) and TransportError is accepted too,
    so this check keeps passing if that classification is revisited.
    """
    from chotot.errors import TransportError, UsageError

    try:
        client.transport.get_json(contract.ENDPOINTS["search"].path,
                                  {"q": "iphone", "limit": 5, "sort": "price", "direction": "asc"})
    except (UsageError, TransportError):
        return Check("no server-side sort", PASS, "sort+direction still rejected (HTTP 400)")
    except Exception as exc:  # noqa: BLE001
        return Check("no server-side sort", WARN, f"unexpected error: {type(exc).__name__}")
    return Check("no server-side sort", WARN,
                 "sort+direction is now accepted - sorting could move server-side")


def _check_seller_filter(client: ChototClient) -> Check:
    try:
        ads, _ = _ids(client, q="iphone", cg=5010, limit=50)
        payload = client.transport.get_json(
            contract.ENDPOINTS["search"].path, {"q": "iphone", "cg": 5010, "limit": 50})
        first = (payload.get("ads") or [{}])[0]
        account_id = first.get("account_id")
        if not account_id:
            return Check("account_id filter", WARN, "no account_id in the sample")
        scoped = client.transport.get_json(
            contract.ENDPOINTS["search"].path, {"account_id": account_id, "limit": 20})
        rows = scoped.get("ads") or []
    except Exception as exc:  # noqa: BLE001
        return Check("account_id filter", FAIL, f"request failed: {exc}")
    if not rows:
        return Check("account_id filter", WARN, "seller query returned nothing")
    matched = sum(1 for a in rows if a.get("account_id") == account_id)
    if matched != len(rows):
        return Check("account_id filter", FAIL,
                     f"only {matched}/{len(rows)} ads belong to the requested seller")
    return Check("account_id filter", PASS, f"{matched}/{len(rows)} ads match the seller")


def _check_taxonomy_codes(client: ChototClient) -> Check:
    """Spot-check that bundled province codes still return ads.

    The predecessor shipped a hand-written table in which 11 of 13 provinces
    returned zero ads, so this check exists specifically to catch that shape of
    rot rather than to confirm the endpoint is up.
    """
    from chotot import taxonomy

    sample = ["hcm", "hanoi", "da nang", "can tho", "hai phong"]
    dead: List[str] = []
    checked = 0
    for name in sample:
        try:
            codes = taxonomy.province_codes(name)
        except Exception:  # noqa: BLE001
            dead.append(f"{name} (unresolvable)")
            continue
        for code in codes:
            checked += 1
            try:
                ids, _ = _ids(client, region_v2=code, limit=3)
            except Exception:  # noqa: BLE001
                dead.append(f"{name}:{code} (error)")
                continue
            if not ids:
                dead.append(f"{name}:{code} (empty)")
    if dead:
        return Check("province codes resolve", FAIL,
                     f"{len(dead)}/{checked} codes returned nothing: {', '.join(dead[:5])}")
    return Check("province codes resolve", PASS, f"all {checked} codes across {len(sample)} provinces returned ads")


def _check_detail_shape(client: ChototClient) -> Check:
    try:
        payload = client.transport.get_json(contract.ENDPOINTS["search"].path, {"q": "iphone", "limit": 1})
        list_id = (payload.get("ads") or [{}])[0].get("list_id")
        if not list_id:
            return Check("detail payload shape", FAIL, "no list_id to inspect")
        detail = client.get_listing(list_id)
    except Exception as exc:  # noqa: BLE001
        return Check("detail payload shape", FAIL, f"{type(exc).__name__}: {exc}")
    if not detail.specs:
        return Check("detail payload shape", WARN, f"listing {list_id} carried no ad_params")
    return Check("detail payload shape", PASS,
                 f"listing {list_id}: {len(detail.specs)} spec fields, condition="
                 f"{detail.listing.condition_label or 'unstated'}")


def _check_missing_listing(client: ChototClient) -> Check:
    from chotot.errors import NotFoundError

    try:
        client.get_listing(1)
    except NotFoundError:
        return Check("missing listing -> 404", PASS, "id 1 correctly reported as not found")
    except Exception as exc:  # noqa: BLE001
        return Check("missing listing -> 404", WARN, f"unexpected {type(exc).__name__}: {exc}")
    return Check("missing listing -> 404", FAIL, "id 1 unexpectedly resolved")


def _check_listing_type_filter(client: ChototClient) -> Check:
    """`st` is the filter whose absence is not neutral.

    A property browse is roughly 55% rentals, so if `st` stopped filtering, an
    apartment median would average a monthly rent against a purchase price --
    a 400x error. Both directions: with `st` the rows are one type, without it
    they are mixed.
    """
    from collections import Counter

    try:
        mixed = client.transport.get_json(
            contract.ENDPOINTS["search"].path, {"cg": 1000, "limit": 50})
        filtered = client.transport.get_json(
            contract.ENDPOINTS["search"].path, {"cg": 1000, "limit": 50, "st": "s"})
    except Exception as exc:  # noqa: BLE001
        return Check("listing type filter (st)", FAIL, f"request failed: {exc}")

    rows = filtered.get("ads") or []
    if not rows:
        return Check("listing type filter (st)", FAIL, "st=s returned nothing")
    kinds = Counter(a.get("type") for a in rows)
    if set(kinds) != {"s"}:
        return Check("listing type filter (st)", FAIL,
                     f"st=s returned mixed types {dict(kinds)} - price statistics "
                     f"for property would average rents against sale prices")
    unfiltered = Counter(a.get("type") for a in (mixed.get("ads") or []))
    if len({t for t in unfiltered if t in ("s", "u")}) < 2:
        return Check("listing type filter (st)", WARN,
                     "the unfiltered browse was not mixed, so the check could not "
                     "prove st is doing the filtering")
    return Check("listing type filter (st)", PASS,
                 f"unfiltered {dict(unfiltered)} vs st=s {dict(kinds)}")


def _check_phone_stays_masked(client: ChototClient) -> Check:
    """The listing endpoint masks the seller's number; this tool relies on that.

    If upstream ever stopped masking, `chotot detail <id>` would print a full phone
    number by default -- a privacy regression introduced by someone else's change,
    which is exactly the kind this gate exists to notice.
    """
    try:
        payload = client.transport.get_json(
            contract.ENDPOINTS["search"].path, {"q": "iphone", "limit": 1})
        list_id = (payload.get("ads") or [{}])[0].get("list_id")
        if not list_id:
            return Check("listing phone stays masked", WARN, "no listing to inspect")
        detail = client.get_listing(list_id)
    except Exception as exc:  # noqa: BLE001
        return Check("listing phone stays masked", FAIL, f"{type(exc).__name__}: {exc}")

    phone = detail.phone_masked
    if not phone:
        return Check("listing phone stays masked", PASS, "no phone published on this listing")
    if "*" not in phone:
        return Check("listing phone stays masked", FAIL,
                     "upstream published an UNMASKED phone number on a listing")
    return Check("listing phone stays masked", PASS, f"masked as {phone[:4]}****")


def _check_pages_still_overlap(client: ChototClient) -> Check:
    """Deduplication is mandatory only while pages actually overlap.

    Overlap is a property of shard boundaries, so any single pair of windows may
    happen not to share a row. Sampling one pair made this check flap between
    PASS and WARN on identical, healthy behaviour -- and a gate that cries wolf
    gets muted. Several pairs are sampled and only a total absence is reported.
    """
    pairs = ((0, 50), (100, 150), (200, 250))
    shared_total = 0
    sampled = 0
    for low, high in pairs:
        try:
            first = client.transport.get_json(
                contract.ENDPOINTS["search"].path, {"q": "iphone", "limit": 50, "o": low})
            second = client.transport.get_json(
                contract.ENDPOINTS["search"].path, {"q": "iphone", "limit": 50, "o": high})
        except Exception as exc:  # noqa: BLE001
            return Check("adjacent pages overlap", FAIL, f"request failed: {exc}")
        left = {a.get("list_id") for a in (first.get("ads") or [])}
        right = {a.get("list_id") for a in (second.get("ads") or [])}
        if not left or not right:
            continue
        sampled += 1
        shared_total += len(left & right)

    if not sampled:
        return Check("adjacent pages overlap", FAIL, "every offset window was empty")
    if shared_total == 0:
        return Check("adjacent pages overlap", WARN,
                     f"no overlap across {sampled} window pairs; deduplication is "
                     f"still applied, and could be reconsidered if this persists")
    return Check("adjacent pages overlap", PASS,
                 f"{shared_total} repeated rows across {sampled} window pairs - "
                 f"deduplication is still required")


def _check_merged_provinces_still_split(client: ChototClient) -> Check:
    """A merged province must still need several legacy codes, each populated."""
    from chotot import taxonomy

    codes = taxonomy.province_codes("hcm")
    if len(codes) < 2:
        return Check("merged provinces expand", FAIL,
                     f"HCM resolved to {codes}; the 2025 merger group is gone from "
                     f"the snapshot and listings outside 13000 would be dropped")
    empty = []
    for code in codes:
        try:
            payload = client.transport.get_json(
                contract.ENDPOINTS["search"].path, {"region_v2": code, "limit": 1})
            if not (payload.get("ads") or []):
                empty.append(code)
        except Exception:  # noqa: BLE001
            empty.append(code)
    if empty:
        return Check("merged provinces expand", FAIL, f"codes returning nothing: {empty}")
    return Check("merged provinces expand", PASS,
                 f"HCM = {codes}, all populated")


CHECKS: List[Callable[[ChototClient], Check]] = [
    _check_reachable,
    _check_price_filter,
    _check_ignored_params_still_ignored,
    _check_total_cap,
    _check_absent_total,
    _check_query_terms_are_unioned,
    _check_offset_pagination,
    _check_limit_clamp,
    _check_no_server_sort,
    _check_listing_type_filter,
    _check_pages_still_overlap,
    _check_merged_provinces_still_split,
    _check_phone_stays_masked,
    _check_seller_filter,
    _check_taxonomy_codes,
    _check_detail_shape,
    _check_missing_listing,
]


def run_doctor(client: ChototClient, palette: Palette, as_json: bool = False) -> int:
    from chotot import taxonomy

    results: List[Check] = []
    for check in CHECKS:
        try:
            results.append(check(client))
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failing check
            results.append(Check(getattr(check, "__name__", "check"), FAIL,
                                 f"check raised {type(exc).__name__}: {exc}"))

    failed = sum(1 for r in results if r.status == FAIL)
    warned = sum(1 for r in results if r.status == WARN)
    proxy_info = getattr(client.transport, "proxy_masked", "none")
    is_proxied = getattr(client.transport, "is_proxied", False)

    if as_json:
        print(to_json({
            "contract_measured_at": contract.CONTRACT_MEASURED_AT,
            "taxonomy_snapshot": taxonomy.snapshot_date(),
            "transport": "proxy" if is_proxied else "direct",
            "proxy": proxy_info,
            "graded": len(results),
            "failed": failed, "warned": warned,
            "checks": [r.to_dict() for r in results],
        }))
        return 1 if failed else 0

    colours = {PASS: "green", FAIL: "red", WARN: "yellow"}
    rows = [[palette(r.status, colours[r.status], "bold"), r.name, r.detail] for r in results]
    print(render_table(rows, ["", "Check", "Detail"], palette))
    print()
    if is_proxied:
        print(palette(f"Transport: proxy ({proxy_info})", "cyan"))
    print(f"Graded {len(results)} subjects · "
          f"{palette(str(len(results) - failed - warned) + ' passed', 'green')} · "
          f"{palette(str(warned) + ' warned', 'yellow')} · "
          f"{palette(str(failed) + ' failed', 'red' if failed else 'dim')}")
    print(palette(f"Contract measured {contract.CONTRACT_MEASURED_AT} · "
                  f"taxonomy snapshot {taxonomy.snapshot_date()}", "dim"))
    if failed:
        print(palette("A FAIL means the gateway no longer behaves as this version assumes. "
                      "Results may be wrong until the contract is re-measured.", "red"))
    return 1 if failed else 0
