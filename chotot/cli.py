# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""``chotot`` command line entry point.

Exit codes are part of the interface, so scripts can branch on them:

===  ==========================================================
0    success
1    unexpected internal error
2    usage error (bad flag, unresolvable region, impossible filter)
3    success, but nothing matched
4    the requested listing or seller does not exist
5    network/transport failure
6    rate limited upstream
7    the gateway answered in a shape this version does not understand
===  ==========================================================

Human-facing chatter and warnings go to **stderr**; ``--json`` output goes to
**stdout** alone, so ``chotot search x --json > out.json`` is always parseable
while the operator still sees the caveats.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, TextIO

from chotot import __version__
from chotot.analyzer import MarketAnalyzer
from chotot import contract, facets
from chotot.client import (
    CONDITIONS, LISTING_TYPE_CHOICES, SELLER_TYPES, SORT_CHOICES, ChototClient,
)
from chotot.taxonomy import resolve_category
from chotot.errors import ChototError, NotFoundError, UsageError
from chotot.formatter import (
    Palette,
    detail_card,
    format_vnd,
    listings_csv,
    listings_markdown,
    listings_table,
    price_dashboard,
    render_table,
    render_warnings,
    search_summary,
    supports_colour,
    to_json,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_RESULTS = 3

PROG = "chotot"

#: Applied after parsing to any global option the user did not pass. The parser
#: itself uses argparse.SUPPRESS so that a flag given before the subcommand is
#: not overwritten by the subparser's copy of the same option.
GLOBAL_DEFAULTS = {
    "no_colour": False, "timeout": 20.0, "min_interval": 0.2,
    "retries": 3, "verbose": False, "proxy": None, "auto_proxy": False,
    # None, not "vn": the transport needs to tell "--geo was given" from
    # "--geo was defaulted" to warn when it is paired with a proxy it cannot
    # apply to. The default exit country lives in chotot.proxy.DEFAULT_GEO.
    "geo": None,
}


def _eprint(text: str) -> None:
    if text:
        print(text, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Search, inspect and analyse the Chợ Tốt marketplace (Vietnam).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  chotot search "iphone 13" --region hcm --limit 10
  chotot search "macbook air m1" --category laptop --max-price 15000000 --sort price_asc
  chotot search "xe wave" --region "da nang" --condition used --seller-type individual
  chotot search "can ho" --category can-ho-chung-cu --listing-type rent --region hcm
  chotot detail 134348455
  chotot detail https://www.chotot.com/134348455.htm --json
  chotot search "canon powershot v1" --match-all   # gateway UNIONs terms; this intersects
  chotot analyze "iphone 13 128gb" --category phone --samples 150
  chotot analyze "honda vision" --region hcm --price-check 25000000
  chotot seller 17864227
  chotot facets 5010
  chotot search "iphone" --category phone --facet mobile_brand=apple --facet mobile_capacity="256 GB"
  chotot export "ipad pro" --limit 200 --output ipad.csv
  chotot categories --parent 5000
  chotot regions --search "can tho"
  chotot search "iphone 13" --auto-proxy   # direct first; residential proxy only after a block
  chotot doctor --proxy auto
  chotot mcp

Prices are in Vietnamese dong (VND). Sorting, condition and seller-type filters
are applied locally because the gateway ignores them; every result says so.
""",
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")

    # Global options live on a parent parser so they are accepted both before
    # and after the subcommand. Users reach for `chotot search x --no-colour`
    # at least as often as the other order, and argparse only honours a
    # top-level flag in the leading position.
    # Defaults are SUPPRESS, not real values: the same option exists on the
    # top-level parser and on every subparser, and a concrete default on the
    # subparser would overwrite a flag the user passed BEFORE the subcommand.
    # Missing attributes are filled from GLOBAL_DEFAULTS after parsing.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-colour", "--no-color", action="store_true", dest="no_colour",
                        default=argparse.SUPPRESS,
                        help="Disable ANSI colour (also honours NO_COLOR).")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
                        help="Per-request timeout (default: 20).")
    common.add_argument("--min-interval", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
                        help="Minimum delay between gateway requests (default: 0.2).")
    common.add_argument("--retries", type=int, default=argparse.SUPPRESS, metavar="N",
                        help="Attempts per request on a retryable failure (default: 3).")
    common.add_argument("--proxy", default=argparse.SUPPRESS, metavar="URL",
                        help="HTTP proxy URL ('http://...'), 'auto' to resolve a residential "
                             "proxy now, or 'none' to force a direct connection. SOCKS is not "
                             "supported by the standard library. Env: CHOTOT_PROXY.")
    common.add_argument("--auto-proxy", action="store_true", default=argparse.SUPPRESS,
                        help="Start direct; after HTTP 403/429 or a connection block, switch "
                             "to the resolved residential proxy for the rest of the run. "
                             "Env: CHOTOT_AUTO_PROXY=1.")
    common.add_argument("--geo", default=argparse.SUPPRESS, metavar="CC",
                        help="Exit country for the residential proxy resolver (default: vn); "
                             "applies to 'auto' and the fallback only.")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="Log gateway activity to stderr.")
    for action in common._actions:
        parser._add_action(action)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    def add_filters(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-c", "--category", help="Category name, slug or code (see 'chotot categories').")
        sub.add_argument("-r", "--region", help="Province name, alias or region_v2 code (see 'chotot regions').")
        sub.add_argument("-d", "--district", help="District name or area_v2 code.")
        sub.add_argument("--min-price", type=int, metavar="VND", help="Minimum asking price in VND.")
        sub.add_argument("--max-price", type=int, metavar="VND", help="Maximum asking price in VND.")
        sub.add_argument("--condition", choices=CONDITIONS, default="any",
                         help="Filter by condition (applied locally).")
        sub.add_argument("--seller-type", choices=SELLER_TYPES, default="any",
                         help="Filter by seller type (applied locally).")
        sub.add_argument("--listing-type", choices=list(LISTING_TYPE_CHOICES), default="any",
                         help="Sale, rent, or wanted ads. Property and vehicle categories "
                              "mix sale and rental listings by default, which makes any "
                              "price comparison across them meaningless.")
        sub.add_argument("--facet", action="append", default=[], metavar="NAME=VALUE",
                         help="Category-specific filter, repeatable "
                              "(e.g. --facet mobile_brand=apple). See 'chotot facets <category>'.")
        sub.add_argument("--match-all", action="store_true",
                         help="Keep only ads whose own text contains EVERY word of the "
                              "query. The gateway unions search terms, so adding a word "
                              "widens the results instead of narrowing them (applied locally).")
        sub.add_argument("--no-expand-region", action="store_true",
                         help="Query only the matched legacy region code instead of every "
                              "code of the merged province.")

    search_p = subparsers.add_parser("search", aliases=["s"], parents=[common], help="Search marketplace listings.")
    search_p.add_argument("query", nargs="?", default=None, help="Free-text keywords.")
    add_filters(search_p)
    search_p.add_argument("-n", "--limit", type=int, default=20, help="Listings to return (default: 20).")
    search_p.add_argument("--sort", choices=SORT_CHOICES, default="relevance",
                          help="Order results (applied locally).")
    search_p.add_argument("--max-requests", type=int, default=24,
                          help="Ceiling on gateway requests (default: 24).")
    search_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")
    search_p.add_argument("--csv", action="store_true", help="Emit CSV on stdout.")
    search_p.add_argument("--markdown", action="store_true", help="Emit a Markdown table on stdout.")

    detail_p = subparsers.add_parser("detail", aliases=["d"], parents=[common], help="Show one listing in full.")
    detail_p.add_argument("id_or_url", help="Listing id or chotot.com URL.")
    detail_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    analyze_p = subparsers.add_parser("analyze", aliases=["a"], parents=[common], help="Analyse asking prices for a query.")
    analyze_p.add_argument("query", help="Product keywords, e.g. 'iphone 13 128gb'.")
    add_filters(analyze_p)
    analyze_p.add_argument("-n", "--samples", type=int, default=120,
                           help="Listings to sample (default: 120).")
    analyze_p.add_argument("--keep-outliers", action="store_true",
                           help="Do not trim outliers with the IQR fence.")
    analyze_p.add_argument("--price-check", type=int, metavar="VND",
                           help="Score this asking price against the sample.")
    analyze_p.add_argument("--max-requests", type=int, default=24,
                           help="Ceiling on gateway requests (default: 24).")
    analyze_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    seller_p = subparsers.add_parser("seller", parents=[common], help="List a seller's live ads.")
    seller_p.add_argument("account_id", help="Numeric seller account id.")
    seller_p.add_argument("-n", "--limit", type=int, default=None,
                          help="Ads to return (default: the seller's whole inventory).")
    seller_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    export_p = subparsers.add_parser("export", parents=[common], help="Write search results to a file.")
    export_p.add_argument("query", nargs="?", default=None, help="Free-text keywords.")
    add_filters(export_p)
    export_p.add_argument("-n", "--limit", type=int, default=100, help="Listings to export (default: 100).")
    export_p.add_argument("-o", "--output", required=True, help="Target file (.csv, .json or .md).")
    export_p.add_argument("--format", choices=["csv", "json", "md"],
                          help="Override the format inferred from the filename.")
    export_p.add_argument("--sort", choices=SORT_CHOICES, default="relevance", help="Order results.")
    export_p.add_argument("--max-requests", type=int, default=40,
                          help="Ceiling on gateway requests (default: 40).")
    export_p.add_argument("--overwrite", action="store_true", help="Replace the file if it exists.")

    cat_p = subparsers.add_parser("categories", aliases=["cats"], parents=[common], help="List category codes.")
    cat_p.add_argument("--parent", type=int, help="Show only children of this category code.")
    cat_p.add_argument("--search", help="Filter by name substring.")
    cat_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    reg_p = subparsers.add_parser("regions", parents=[common], help="List province and district codes.")
    reg_p.add_argument("--search", help="Filter by province name or alias.")
    reg_p.add_argument("--province", help="List the districts of this province.")
    reg_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    facet_p = subparsers.add_parser("facets", parents=[common],
                                    help="List the search filters a category supports.")
    facet_p.add_argument("category", help="Category name, slug or code.")
    facet_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    shop_p = subparsers.add_parser("shop", parents=[common], help="Show a professional shop's profile.")
    shop_p.add_argument("alias", help="Shop alias (from 'chotot detail <id> --json').")
    shop_p.add_argument("--show-contact", action="store_true",
                        help="Print phone numbers unredacted. Off by default: the gateway "
                             "masks the same number on a listing.")
    shop_p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")

    doctor = subparsers.add_parser("doctor", parents=[common],
                                   help="Re-measure the gateway contract and report health.")
    doctor.add_argument("--json", action="store_true",
                        help="Emit the graded checks and transport telemetry as JSON on stdout.")
    subparsers.add_parser("mcp", parents=[common], help="Serve the Model Context Protocol over stdio.")
    return parser


# -- command handlers ------------------------------------------------------

def _client(args: argparse.Namespace) -> ChototClient:
    # CHOTOT_BASE_URL points the CLI at another gateway root -- a mirror, or
    # the local servers the end-to-end suite stands up. Not a user-facing flag.
    return ChototClient(
        base_url=os.getenv("CHOTOT_BASE_URL") or contract.GATEWAY_BASE_URL,
        timeout=args.timeout,
        max_retries=args.retries,
        min_interval=args.min_interval,
        proxy=getattr(args, "proxy", None),
        auto_proxy=getattr(args, "auto_proxy", False),
        geo=getattr(args, "geo", None),
    )


def _search_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "query": getattr(args, "query", None),
        "category": args.category,
        "region": args.region,
        "district": args.district,
        "min_price": args.min_price,
        "max_price": args.max_price,
        "condition": args.condition,
        "seller_type": args.seller_type,
        "listing_type": args.listing_type,
        "expand_region": not args.no_expand_region,
        "match_all": args.match_all,
        "facets": facets.parse(resolve_category(args.category), args.facet),
    }


def cmd_search(args: argparse.Namespace, palette: Palette) -> int:
    result = _client(args).search(
        limit=args.limit, sort=args.sort, max_requests=args.max_requests, **_search_kwargs(args)
    )
    if args.json:
        print(to_json(result.to_dict()))
    elif args.csv:
        sys.stdout.write(listings_csv(result.listings))
    elif args.markdown:
        sys.stdout.write(listings_markdown(result.listings, title=args.query or "Chợ Tốt listings"))
    else:
        if result.listings:
            print(listings_table(result.listings, palette))
        else:
            print(palette("No listings matched.", "yellow"))
        _eprint(search_summary(result, palette))
    _eprint(render_warnings(result.warnings, palette))
    return EXIT_OK if result.listings else EXIT_NO_RESULTS


def cmd_detail(args: argparse.Namespace, palette: Palette) -> int:
    detail = _client(args).get_listing(args.id_or_url)
    if args.json:
        print(to_json(detail.to_dict()))
    else:
        print(detail_card(detail, palette))
        if not detail.is_active:
            _eprint(palette("! This listing is no longer active.", "yellow"))
    return EXIT_OK


def cmd_analyze(args: argparse.Namespace, palette: Palette) -> int:
    client = _client(args)
    result = client.search(limit=args.samples, sort="relevance", limit_flag="--samples",
                           max_requests=args.max_requests, **_search_kwargs(args))
    report = MarketAnalyzer.analyze(
        result.listings, query=args.query, remove_outliers=not args.keep_outliers,
    )
    # `is not None`, not truthiness: `--price-check 0` was silently dropped, so
    # the analyser's dedicated message for a non-positive price was unreachable.
    verdict = report.evaluate(args.price_check) if args.price_check is not None else None

    if args.json:
        payload = report.to_dict()
        payload["coverage"] = result.coverage.to_dict()
        payload["search_warnings"] = result.warnings
        if verdict:
            payload["price_check"] = {"price_vnd": args.price_check, **verdict}
        print(to_json(payload))
    else:
        print(price_dashboard(report, palette))
        if verdict:
            print()
            # Show what was asked for. format_vnd renders 0 as "Negotiable",
            # which is a marketplace state, not the number the user typed.
            checked = (format_vnd(args.price_check) if args.price_check > 0
                       else f"{args.price_check:,} VND".replace(",", "."))
            print(palette(f"Price check — {checked}", "bold", "cyan"))
            print(f"  {palette(verdict['verdict'], 'bold')}")
            print(f"  {verdict['note']}")
            if verdict.get("percentile") is not None:
                below = verdict["percentile"]
                print(palette(
                    f"  {below}% of the sampled listings ask less than this; "
                    f"{100 - below}% ask this much or more.", "dim"))
        _eprint(search_summary(result, palette))
    _eprint(render_warnings(list(result.warnings) + list(report.warnings), palette))
    return EXIT_OK if report.priced_count else EXIT_NO_RESULTS


def cmd_seller(args: argparse.Namespace, palette: Palette) -> int:
    storefront = _client(args).seller_listings(args.account_id, limit=args.limit)
    report = MarketAnalyzer.analyze(storefront.listings, query=f"seller {args.account_id}")
    # Reputation comes from the enrichment call, not from the theia rows: those
    # carry no rating or sold-count fields at all.
    seller = storefront.reputation or (storefront.listings[0].seller if storefront.listings else None)

    summary = report.to_dict()
    # Keep the caveats with the number: a median published bare can be read as
    # a fact about the seller rather than a sampled, possibly mixed figure.
    warnings = list(storefront.warnings) + list(summary["warnings"])

    if args.json:
        payload = storefront.to_dict()
        payload["asking_prices"] = summary["asking_price"]
        payload["asking_price_sample"] = summary["sample"]
        payload["mixes_sale_and_rent"] = summary["mixes_sale_and_rent"]
        payload["by_listing_type"] = summary["by_listing_type"]
        payload["warnings"] = warnings
        print(to_json(payload))
        return EXIT_OK

    print(palette(f"Seller {storefront.name or args.account_id}", "bold", "cyan"))
    rows = [
        ("Account ID", str(storefront.account_id or "-")),
        ("Account OID", storefront.account_oid or "-"),
        ("Shop alias", storefront.shop_alias or "-"),
        ("Live listings", f"{len(storefront.listings)} of {storefront.total}"),
        ("Median asking", format_vnd(report.median)),
    ]
    if seller is not None:
        rows.extend([
            ("Type", "Shop / professional" if seller.is_shop else "Individual"),
            ("Ads sold", str(seller.sold_ads) if seller.sold_ads is not None else "-"),
            ("Rating", f"{seller.average_rating:.1f}" if seller.average_rating is not None else "-"),
        ])
    for label, value in rows:
        print(f"  {palette(label, 'dim'):<24} {value}")
    print()
    print(listings_table(storefront.listings, palette))
    _eprint(render_warnings(warnings, palette))
    if storefront.shop_alias:
        _eprint(palette(f"This seller is a shop; run: chotot shop {storefront.shop_alias}", "dim"))
    return EXIT_OK


def cmd_shop(args: argparse.Namespace, palette: Palette) -> int:
    profile = _client(args).shop_profile(args.alias)
    if args.json:
        print(to_json(profile.to_dict(reveal_contact=args.show_contact)))
        return EXIT_OK

    print(palette(profile.name or profile.alias, "bold", "cyan"))
    data = profile.to_dict(reveal_contact=args.show_contact)
    rows = [
        ("Alias", profile.alias),
        ("Verified", "yes" if profile.is_verified else "no"),
        ("Created", profile.created_date or "-"),
        ("Address", profile.address or "-"),
        ("Category", str(profile.category_id or "-")),
        ("Listings", f"{len(profile.listings)} shown of {profile.total_listings or '?'}"),
        ("Phones", ", ".join(p for p in data["phones"] if p) or "-"),
    ]
    for label, value in rows:
        print(f"  {palette(label, 'dim'):<24} {value}")
    if data["phones_redacted"] and profile.phones:
        _eprint(palette("! Phone numbers redacted. Pass --show-contact to reveal them.", "yellow"))
    if profile.description:
        print()
        print(palette("Description", "bold", "magenta"))
        print(f"  {profile.description}")
    if profile.listings:
        print()
        print(listings_table(profile.listings, palette))
    if profile.total_listings and len(profile.listings) < profile.total_listings:
        _eprint(palette(
            f"! Showing the {len(profile.listings)} listings embedded in the shop profile; "
            f"the shop has {profile.total_listings}. Use 'chotot seller {profile.account_oid}' "
            f"for the full inventory.", "yellow"))
    return EXIT_OK


def cmd_facets(args: argparse.Namespace, palette: Palette) -> int:
    category_id = resolve_category(args.category)
    rows = facets.describe(category_id)
    if args.json:
        print(to_json({"category": category_id, "snapshot": facets.snapshot_date(), "facets": rows}))
        return EXIT_OK if rows else EXIT_NO_RESULTS
    if not rows:
        print(palette(f"No verified search facets for category {category_id}.", "yellow"))
        _eprint(palette("Some categories declare parameters the gateway then ignores; "
                        "only filters proven to work are listed.", "dim"))
        return EXIT_NO_RESULTS
    table = []
    for row in rows:
        options = row["options"]
        sample = ", ".join(list(options.values())[:5]) + (" …" if len(options) > 5 else "")
        table.append([row["param"], row["label"],
                      "range" if row["is_range"] else str(len(options)),
                      sample if not row["is_range"] else "MIN-MAX"])
    print(render_table(table, ["Facet", "Label", "Values", "Examples"], palette))
    _eprint(palette(f"{len(rows)} verified facets · use --facet NAME=VALUE · "
                    f"snapshot {facets.snapshot_date()}", "dim"))
    return EXIT_OK


def cmd_export(args: argparse.Namespace, palette: Palette) -> int:
    fmt = args.format
    if not fmt:
        suffix = os.path.splitext(args.output)[1].lower()
        fmt = {".csv": "csv", ".json": "json", ".md": "md", ".markdown": "md"}.get(suffix)
        if not fmt:
            raise UsageError(
                f"Cannot infer a format from {args.output!r}.",
                remedy="Use a .csv/.json/.md filename, or pass --format csv.",
            )
    if os.path.exists(args.output) and not args.overwrite:
        raise UsageError(
            f"{args.output} already exists.",
            remedy="Pass --overwrite to replace it, or choose another path.",
        )

    result = _client(args).search(limit=args.limit, sort=args.sort,
                                  max_requests=args.max_requests, **_search_kwargs(args))
    if not result.listings:
        _eprint(palette("No listings matched; nothing written.", "yellow"))
        return EXIT_NO_RESULTS

    if fmt == "csv":
        body = listings_csv(result.listings)
    elif fmt == "json":
        body = to_json(result.to_dict())
    else:
        body = listings_markdown(result.listings, title=args.query or "Chợ Tốt listings")

    # newline="" keeps csv from doubling line endings on Windows. A filesystem
    # error here is the user's path, not an internal bug, and the gateway
    # requests have already been spent -- so it gets a usage exit code and a
    # message naming the real cause.
    try:
        with open(args.output, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    except OSError as exc:
        raise UsageError(
            f"Could not write {args.output}: {exc.strerror or exc}.",
            remedy="Check the path exists, is a file rather than a directory, "
                   "and is writable.",
        ) from exc

    _eprint(palette(f"Wrote {len(result.listings)} listings to {args.output} ({fmt}).", "green"))
    _eprint(search_summary(result, palette))
    _eprint(render_warnings(result.warnings, palette))
    return EXIT_OK


def cmd_categories(args: argparse.Namespace, palette: Palette) -> int:
    from chotot import taxonomy

    entries = list(taxonomy.categories().values())
    if args.parent is not None:
        entries = [e for e in entries if e.get("parent") == args.parent]
    if args.search:
        needle = taxonomy.normalise(args.search)
        entries = [e for e in entries if needle in taxonomy.normalise(e["name"])]
    entries.sort(key=lambda e: (e.get("parent") or e["id"], e["id"]))

    if args.json:
        print(to_json(entries))
        return EXIT_OK if entries else EXIT_NO_RESULTS
    if not entries:
        print(palette("No categories matched.", "yellow"))
        return EXIT_NO_RESULTS
    rows = [[str(e["id"]), e["name"], e["slug"], str(e.get("parent") or "-")] for e in entries]
    print(render_table(rows, ["Code", "Name", "Slug", "Parent"], palette))
    _eprint(palette(f"{len(entries)} categories · snapshot {taxonomy.snapshot_date('categories.json')}", "dim"))
    return EXIT_OK


def cmd_regions(args: argparse.Namespace, palette: Palette) -> int:
    from chotot import taxonomy

    if args.province:
        # Expand the merger group, as the MCP tool does: listing one legacy
        # code's districts under the merged province's name showed 5 of the 23
        # districts of TP Cần Thơ.
        codes = taxonomy.province_codes(args.province)
        districts = [
            {**entry, "region_v2": code}
            for code in codes for entry in taxonomy.districts_of(code)
        ]
        if args.json:
            print(to_json(districts))
            return EXIT_OK if districts else EXIT_NO_RESULTS
        if not districts:
            print(palette(f"No districts recorded for {args.province}.", "yellow"))
            return EXIT_NO_RESULTS
        rows = [[str(d["area_v2"]), d["name"], str(d["region_v2"])] for d in districts]
        print(render_table(rows, ["area_v2", "District", "region_v2"], palette))
        _eprint(palette(
            f"{len(districts)} districts in {taxonomy.province_name(codes[0])}"
            + (f" across {len(codes)} legacy region codes {codes}" if len(codes) > 1 else ""),
            "dim"))
        return EXIT_OK

    entries = (taxonomy.search_provinces(args.search) if args.search
               else sorted(taxonomy.provinces().values(), key=lambda e: e["region_v2"]))
    if args.json:
        print(to_json(entries))
        return EXIT_OK if entries else EXIT_NO_RESULTS
    if not entries:
        print(palette("No provinces matched.", "yellow"))
        return EXIT_NO_RESULTS
    rows = []
    for entry in entries:
        siblings = entry.get("sibling_region_v2") or []
        rows.append([
            str(entry["region_v2"]),
            entry.get("modern_name") or entry["name"],
            entry["name"],
            ", ".join(str(s) for s in siblings) if len(siblings) > 1 else "-",
            ", ".join(entry.get("aliases", [])[:3]) or "-",
        ])
    print(render_table(rows, ["region_v2", "Province (2025)", "Legacy name", "Merged with", "Aliases"], palette))
    _eprint(palette(
        f"{len(entries)} legacy codes · {len(taxonomy.modern_groups())} modern provinces · "
        f"snapshot {taxonomy.snapshot_date()}", "dim"))
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, palette: Palette) -> int:
    from chotot.doctor import run_doctor

    return run_doctor(_client(args), palette, as_json=args.json)


def cmd_mcp(args: argparse.Namespace, palette: Palette) -> int:
    from chotot.mcp_server import serve_stdio

    return serve_stdio(_client(args))


HANDLERS = {
    "search": cmd_search, "s": cmd_search,
    "detail": cmd_detail, "d": cmd_detail,
    "analyze": cmd_analyze, "a": cmd_analyze,
    "seller": cmd_seller,
    "shop": cmd_shop,
    "facets": cmd_facets,
    "export": cmd_export,
    "categories": cmd_categories, "cats": cmd_categories,
    "regions": cmd_regions,
    "doctor": cmd_doctor,
    "mcp": cmd_mcp,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    palette = Palette(supports_colour() and not args.no_colour)

    try:
        return HANDLERS[args.command](args, palette)
    except ChototError as exc:
        _eprint(palette(f"error: {exc}", "red", "bold"))
        if exc.remedy:
            _eprint(palette(f"  {exc.remedy}", "dim"))
        return exc.exit_code
    except BrokenPipeError:
        # `chotot search ... | head` closes stdout early; that is not a failure.
        try:
            sys.stdout.close()
        finally:
            os._exit(EXIT_OK)
    except KeyboardInterrupt:
        _eprint(palette("interrupted", "yellow"))
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, never a bare traceback
        _eprint(palette(f"unexpected error: {type(exc).__name__}: {exc}", "red", "bold"))
        _eprint(palette("  Please report this with the command you ran.", "dim"))
        if args.verbose:
            raise
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
