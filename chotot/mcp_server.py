# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Model Context Protocol server over stdio.

Speaks MCP 2024-11-05 JSON-RPC 2.0 on stdin/stdout. Register it with any MCP
client as ``chotot mcp``.

The tool descriptions deliberately state the *limits* of each answer -- asking
prices rather than sold prices, a sampled pool rather than the whole
marketplace, an upstream total that saturates at 10,000. An agent that cannot
see those caveats will present a sampled median as a market fact, so they travel
with the data rather than in a README the agent never reads.

Framing note: stdout carries protocol only. Anything diagnostic goes to stderr,
because a stray print would corrupt the JSON-RPC stream.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

from chotot import __version__
from chotot.analyzer import MarketAnalyzer
from chotot.client import (
    CONDITIONS, LISTING_TYPE_CHOICES, SELLER_TYPES, SORT_CHOICES, ChototClient,
)
from chotot.errors import ChototError

logger = logging.getLogger("chotot.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "chotot"

# JSON-RPC 2.0 error codes
#: Errors are raised by shared code that mostly serves the CLI, so their
#: remedies name CLI flags. An agent cannot pass `--facet`; it passes a `facets`
#: argument. Translating at this boundary keeps one set of messages while making
#: them actionable on both surfaces.
_REMEDY_TERMS = (
    # Longest first: "--facet" is a prefix of "--facets", and "--limit" of
    # "--limit-flag"; substituting the short form first mangles the long one.
    ("--no-expand-region", "expand_region=false"),
    ("--keep-outliers", "the keep_outliers argument"),
    ("--listing-type", "the listing_type argument"),
    ("--seller-type", "the seller_type argument"),
    ("--min-interval", "a slower request rate"),
    ("--max-requests", "a smaller limit"),
    ("--show-contact", "reveal_contact=true"),
    ("--overwrite", "a different path"),
    ("--min-price", "the min_price argument"),
    ("--max-price", "the max_price argument"),
    ("--condition", "the condition argument"),
    ("--district", "the district argument"),
    ("--samples", "the samples argument"),
    ("--category", "the category argument"),
    ("--format", "a different format"),
    ("--facet", "the facets argument"),
    ("--region", "the region argument"),
    ("--limit", "the limit argument"),
    ("--sort", "the sort argument"),
    ("chotot facets <category>", "the chotot_list_facets tool"),
    ("chotot regions --search <name>", "the chotot_list_regions tool"),
    ("chotot regions", "the chotot_list_regions tool"),
    ("chotot categories", "the chotot_list_categories tool"),
    ("chotot detail <id> --json", "the chotot_get_listing tool"),
    ("chotot doctor", "the CLI's doctor command"),
)


def _for_agent(text: Optional[str]) -> Optional[str]:
    """Rewrite CLI-shaped advice into something an MCP client can act on."""
    if not text:
        return text
    for cli_term, mcp_term in _REMEDY_TERMS:
        text = text.replace(cli_term, mcp_term)
    # The CLI phrasing wraps commands in "Run '...'", which reads wrong once the
    # command has become a tool name.
    # "the facets argument needs the category argument" reads correctly;
    # "needs --category" did not.
    text = text.replace("Run 'the ", "Call the ").replace("' to list", " to list")
    text = text.replace("' for ", " for ").replace("' to find", " to find")
    text = text.replace("Run the ", "Call the ")
    # The CLI phrasing "search and analyze accept --x" becomes nonsense once the
    # flag is a tool argument; state the argument plainly instead.
    text = re.sub(r"\(?(?:search and analyze|analyze) accepts? ([a-z_ ]+?)(?: to [a-z ]+)?\)?\.",
                  r"(pass \1).", text)
    return text.replace("''", "'")


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_LIMITS = (
    "Returns ASKING prices from live ads, not sold prices. Results are a "
    "deduplicated sample of an unstably-ranked feed, and the upstream match "
    "count saturates at 10000, so it is a floor and not an exact total."
)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "chotot_search",
        "description": (
            "Search Chợ Tốt (Vietnam's largest classifieds marketplace) for listings. "
            "Supports free text, category, province, district and a VND price range. "
            "Condition, seller type and sort order are applied locally because the "
            "gateway ignores them; the response says which were applied. " + _LIMITS
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text keywords, e.g. 'iphone 13 128gb'."},
                "category": {"type": "string", "description": "Category name, slug or code (see chotot_list_categories)."},
                "region": {"type": "string", "description": "Province name or alias, e.g. 'hcm', 'ha noi', 'da nang'. Merged 2025 provinces are expanded to every legacy code."},
                "district": {"type": "string", "description": "District name, e.g. 'Quận Gò Vấp'."},
                "min_price": {"type": "integer", "description": "Minimum asking price in VND."},
                "max_price": {"type": "integer", "description": "Maximum asking price in VND."},
                "condition": {"type": "string", "enum": list(CONDITIONS), "default": "any"},
                "seller_type": {"type": "string", "enum": list(SELLER_TYPES), "default": "any"},
                "listing_type": {"type": "string", "enum": list(LISTING_TYPE_CHOICES), "default": "any",
                                  "description": "Sale, rent or wanted ads. Property and vehicle "
                                                 "categories mix sale and rental listings by default; "
                                                 "comparing prices across them is meaningless."},
                "match_all": {"type": "boolean", "default": False,
                               "description": "Keep only ads whose own text contains EVERY word of the query. The gateway UNIONS search terms, so a multi-word query returns ads matching only one of them and a price statistic over them describes the wrong product."},
                "sort": {"type": "string", "enum": list(SORT_CHOICES), "default": "relevance"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "facets": {
                    "type": "object",
                    "description": "Category-specific filters as name=value, e.g. "
                                   "{\"mobile_brand\": \"apple\"}. Call chotot_list_facets "
                                   "first; unverified names are refused rather than silently ignored.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": [],
        },
    },
    {
        "name": "chotot_get_listing",
        "description": (
            "Fetch one Chợ Tốt listing in full: price, structured specifications with "
            "their Vietnamese labels, location and coordinates, seller facts, images, "
            "and whether the ad is still active. The phone number is returned masked by "
            "the upstream API and is not unmaskable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_or_url": {"type": "string", "description": "Listing id (e.g. '134348455') or a chotot.com URL."},
            },
            "required": ["id_or_url"],
        },
    },
    {
        "name": "chotot_analyze_prices",
        "description": (
            "Compute the asking-price distribution for a product query: median, "
            "quartiles, interquartile range, per-condition breakdown and a histogram. "
            "Optionally scores one price against the sample. Percentiles are withheld "
            "when fewer than 8 priced listings are found rather than computed from a "
            "handful. " + _LIMITS
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Product keywords, e.g. 'macbook air m1'."},
                "category": {"type": "string"},
                "region": {"type": "string"},
                "min_price": {"type": "integer"},
                "max_price": {"type": "integer"},
                "condition": {"type": "string", "enum": list(CONDITIONS), "default": "any"},
                "listing_type": {"type": "string", "enum": list(LISTING_TYPE_CHOICES), "default": "any"},
                "match_all": {"type": "boolean", "default": False,
                               "description": "Keep only ads whose own text contains EVERY word of the query. The gateway UNIONS search terms, so a multi-word query returns ads matching only one of them and a price statistic over them describes the wrong product."},
                "samples": {"type": "integer", "default": 120, "minimum": 10, "maximum": 400,
                             "description": "Listings to sample. More samples cost more requests."},
                "keep_outliers": {"type": "boolean", "default": False,
                                   "description": "Do not trim outliers with the IQR fence."},
                "price_check": {"type": "integer", "description": "Score this VND price against the sample."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "chotot_seller_listings",
        "description": (
            "Fetch a seller's COMPLETE live inventory and its asking-price summary. "
            "Accepts either the numeric account_id or the 32-character account_oid. "
            "Returns an authoritative total. Chợ Tốt publishes no separate profile "
            "endpoint, so seller facts come from the ads and may be absent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Numeric account_id or the 32-char account_oid."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                           "description": "Cap the inventory fetched. Omit for the whole storefront."},
                "keep_outliers": {"type": "boolean", "default": False,
                                   "description": "Do not trim outlier prices with the IQR fence."},
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "chotot_list_facets",
        "description": (
            "List the category-specific search filters available for a category "
            "(phone brand/capacity/colour, car make/model/gearbox/fuel/year, property "
            "rooms/direction, and so on) with their valid values. Only filters PROVEN "
            "to work are listed: Chợ Tốt declares several parameters it then accepts "
            "and silently ignores, and those are excluded. Pass the results to "
            "chotot_search as the 'facets' argument."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category name, slug or code, e.g. 'phone' or 5010."},
            },
            "required": ["category"],
        },
    },
    {
        "name": "chotot_shop_profile",
        "description": (
            "Fetch a professional shop's public profile by its shop_alias: name, "
            "verification status, address, category and its listings. Phone numbers are "
            "REDACTED by default because the listing API masks the same number; set "
            "reveal_contact only when the user has explicitly asked for it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "shop_alias from a listing."},
                "reveal_contact": {"type": "boolean", "default": False},
            },
            "required": ["alias"],
        },
    },
    {
        "name": "chotot_list_categories",
        "description": "List Chợ Tốt category codes, names and hierarchy, from a live-derived snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "parent": {"type": "integer", "description": "Only children of this category code."},
                "search": {"type": "string", "description": "Filter by name substring."},
            },
            "required": [],
        },
    },
    {
        "name": "chotot_list_regions",
        "description": (
            "List Vietnamese provinces and their Chợ Tốt region codes. Vietnam merged "
            "provinces in 2025 while Chợ Tốt kept the old codes, so one modern province "
            "may be served by several: 'TP Hồ Chí Minh' is [2010, 2011, 13000]. Pass "
            "province to list that province's districts instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by province name or alias."},
                "province": {"type": "string", "description": "List this province's districts."},
            },
            "required": [],
        },
    },
]


#: Hard ceilings, enforced in the handler. A schema `maximum` is advertised to
#: the model and enforced by nobody: `limit: 300` returned a 274 KB tool result.
_MAX_LIMIT = 200
_MAX_SAMPLES = 400


def _int_arg(args: Dict[str, Any], name: str, default: Optional[int] = None,
             minimum: int = 1, maximum: Optional[int] = None) -> Optional[int]:
    """Read an integer argument tolerantly, and refuse readably.

    Agents routinely send numbers as strings. ``int(value)`` on the wrong type
    raised a bare ``TypeError`` that surfaced as JSON-RPC ``-32603`` -- an
    internal error, which tells the model the server is broken rather than that
    its argument was wrong. Out-of-range values are CLAMPED and reported, not
    rejected, because a too-large limit is a reasonable request for less data.
    """
    from chotot.errors import UsageError

    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass; almost never intended
        raise UsageError(f"'{name}' must be a number, got a boolean.",
                         remedy=f"Pass {name} as a number, e.g. {default or 20}.")
    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("+-").isdigit():
            raise UsageError(f"'{name}' must be a whole number, got {value!r}.",
                             remedy=f"Pass {name} as a number, e.g. {default or 20}.")
        value = int(text)
    if not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise UsageError(f"'{name}' must be a whole number, got {type(value).__name__}.",
                             remedy=f"Pass {name} as a number, e.g. {default or 20}.") from None
    if maximum is not None:
        value = min(value, maximum)
    return max(minimum, value)


def _required_str(args: Dict[str, Any], name: str, example: str) -> str:
    """Fetch a required string argument.

    An explicit ``null`` passes a plain ``if name not in args`` check, so
    ``{"query": null}`` reached the search as "no keywords" and returned
    whole-marketplace statistics labelled with the caller's intent.
    """
    from chotot.errors import UsageError

    value = args.get(name)
    if value is None or not str(value).strip():
        raise UsageError(f"'{name}' is required.", remedy=f"For example: {example}")
    return str(value).strip()


def _flag(args: Dict[str, Any], name: str) -> bool:
    """Strict boolean for a privacy switch.

    ``bool("false")`` is ``True``. Coercing a switch that governs whether phone
    numbers are printed must never accept a truthy-looking string, so only a
    genuine JSON ``true`` enables it.
    """
    return args.get(name) is True


def _arg(args: Dict[str, Any], name: str, default: Any = None) -> Any:
    """Read an argument, treating an explicit JSON ``null`` as "not supplied".

    ``args.get(name, default)`` returns ``None`` when the caller sent
    ``"limit": null``, and ``int(None)`` then raises a raw ``TypeError`` that
    surfaces as an internal error. Agents emit explicit nulls for unset optional
    fields routinely, so this is the common path, not an edge case.
    """
    value = args.get(name)
    return default if value is None else value


class McpServer:
    """Minimal, dependency-free MCP implementation."""

    def __init__(self, client: ChototClient,
                 stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        self.client = client
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "chotot_search": self._tool_search,
            "chotot_get_listing": self._tool_get_listing,
            "chotot_analyze_prices": self._tool_analyze,
            "chotot_seller_listings": self._tool_seller,
            "chotot_list_categories": self._tool_categories,
            "chotot_list_regions": self._tool_regions,
            "chotot_list_facets": self._tool_facets,
            "chotot_shop_profile": self._tool_shop,
        }

    # -- tools -------------------------------------------------------------

    def _tool_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self.client.search(
            query=_arg(args, "query"),
            category=_arg(args, "category"),
            region=_arg(args, "region"),
            district=_arg(args, "district"),
            min_price=_int_arg(args, "min_price", None, minimum=0),
            max_price=_int_arg(args, "max_price", None, minimum=0),
            condition=_arg(args, "condition", "any"),
            seller_type=_arg(args, "seller_type", "any"),
            listing_type=_arg(args, "listing_type", "any"),
            sort=_arg(args, "sort", "relevance"),
            match_all=_flag(args, "match_all"),
            limit=_int_arg(args, "limit", 20, maximum=_MAX_LIMIT) or 20,
            facets=self._resolve_facets(args),
        )
        return result.to_dict()

    def _resolve_facets(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from chotot import facets
        from chotot.taxonomy import resolve_category

        raw = _arg(args, "facets") or {}
        if not raw:
            return {}
        # Agents send this as an object or as a list of "name=value" strings.
        # A list reaching .items() raised a bare AttributeError as -32603.
        if isinstance(raw, dict):
            pairs = [f"{k}={v}" for k, v in raw.items()]
        elif isinstance(raw, list):
            pairs = [str(item) for item in raw]
        else:
            from chotot.errors import UsageError

            raise UsageError(
                f"'facets' must be an object or a list, got {type(raw).__name__}.",
                remedy='For example: {"mobile_brand": "apple"}',
            )
        return facets.parse(resolve_category(_arg(args, "category")), pairs)

    def _tool_get_listing(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.get_listing(args["id_or_url"]).to_dict()

    def _tool_analyze(self, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self.client.search(
            query=_required_str(args, "query", '"macbook air m1"'),
            category=_arg(args, "category"),
            region=_arg(args, "region"),
            min_price=_int_arg(args, "min_price", None, minimum=0),
            max_price=_int_arg(args, "max_price", None, minimum=0),
            condition=_arg(args, "condition", "any"),
            listing_type=_arg(args, "listing_type", "any"),
            match_all=_flag(args, "match_all"),
            limit=_int_arg(args, "samples", 120, maximum=_MAX_SAMPLES) or 120,
        )
        report = MarketAnalyzer.analyze(
            result.listings, query=str(args["query"]),
            remove_outliers=not _flag(args, "keep_outliers"))
        payload = report.to_dict()
        payload["coverage"] = result.coverage.to_dict()
        payload["search_warnings"] = result.warnings
        # `is not None`, not truthiness: an explicit 0 was dropped, so the
        # analyser's dedicated non-positive-price verdict was unreachable. A
        # negative value is reported rather than clamped, for the same reason.
        raw_check = args.get("price_check")
        if raw_check is not None:
            price_check = _int_arg(args, "price_check", None, minimum=-(2 ** 62))
            payload["price_check"] = {
                "price_vnd": price_check, **report.evaluate(int(price_check or 0)),
            }
        return payload

    def _tool_seller(self, args: Dict[str, Any]) -> Dict[str, Any]:
        storefront = self.client.seller_listings(
            _required_str(args, "account_id", "17864227"),
            limit=_int_arg(args, "limit", None, maximum=_MAX_SAMPLES))
        # The analyser's warnings name this, so the tool has to accept it --
        # advice an agent cannot act on is worse than none.
        report = MarketAnalyzer.analyze(
            storefront.listings, query=f"seller {args['account_id']}",
            remove_outliers=not _flag(args, "keep_outliers"))
        summary = report.to_dict()
        payload = storefront.to_dict()
        payload["asking_prices"] = summary["asking_price"]
        # Keep the caveats attached to the number. Publishing the median alone
        # let an agent report a sampled, possibly sale/rent-mixed figure as fact.
        payload["asking_price_sample"] = summary["sample"]
        payload["mixes_sale_and_rent"] = summary["mixes_sale_and_rent"]
        payload["by_listing_type"] = summary["by_listing_type"]
        payload["warnings"] = list(payload.get("warnings") or []) + list(summary["warnings"])
        return payload

    def _tool_facets(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from chotot import facets
        from chotot.taxonomy import category_name, resolve_category

        category_id = resolve_category(
            _required_str(args, "category", '"phone" or 5010'))
        rows = facets.describe(category_id)
        return {
            "category_id": category_id,
            "category": category_name(category_id),
            "snapshot": facets.snapshot_date(),
            "count": len(rows),
            "facets": rows,
            "usage": 'Pass these to chotot_search as "facets", e.g. {"mobile_brand": "apple"}.',
        }

    def _tool_shop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        profile = self.client.shop_profile(_required_str(args, "alias", "pWOkAaEOdDRG6nY"))
        return profile.to_dict(reveal_contact=_flag(args, "reveal_contact"))

    def _tool_categories(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from chotot import taxonomy

        entries = list(taxonomy.categories().values())
        parent = _int_arg(args, "parent", None, minimum=0)
        if parent is not None:
            entries = [e for e in entries if e.get("parent") == parent]
        if _arg(args, "search"):
            needle = taxonomy.normalise(str(_arg(args, "search")))
            entries = [e for e in entries if needle in taxonomy.normalise(e["name"])]
        return {"count": len(entries), "snapshot": taxonomy.snapshot_date("categories.json"),
                "categories": sorted(entries, key=lambda e: e["id"])}

    def _tool_regions(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from chotot import taxonomy

        if _arg(args, "province"):
            # Expand the merger group: returning one legacy code's districts
            # under the merged province's name omits most of the province.
            codes = taxonomy.province_codes(str(_arg(args, "province")))
            districts: List[Dict[str, Any]] = []
            for code in codes:
                for entry in taxonomy.districts_of(code):
                    districts.append({**entry, "region_v2": code})
            return {"province": taxonomy.province_name(codes[0]) if codes else None,
                    "region_v2_codes": codes, "count": len(districts),
                    "districts": districts}
        entries = (taxonomy.search_provinces(str(_arg(args, "search"))) if _arg(args, "search")
                   else sorted(taxonomy.provinces().values(), key=lambda e: e["region_v2"]))
        return {"count": len(entries), "snapshot": taxonomy.snapshot_date(),
                "modern_province_groups": taxonomy.modern_groups(), "provinces": entries}

    # -- protocol ----------------------------------------------------------

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a response, or None for a notification (which takes no reply)."""
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        is_notification = "id" not in request

        if method == "initialize":
            return self._ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, params)

        if is_notification:
            return None
        return self._err(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _call_tool(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = self._handlers.get(name)
        if handler is None:
            return self._err(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            payload = handler(arguments)
        except ChototError as exc:
            # A domain error is a tool-level result, not a protocol error: the
            # agent needs to read it and adapt, not treat the server as broken.
            return self._ok(request_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": _for_agent(str(exc)), "remedy": _for_agent(exc.remedy)},
                    ensure_ascii=False)}],
                "isError": True,
            })
        except KeyError as exc:
            return self._err(request_id, INVALID_PARAMS, f"Missing required argument: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            return self._err(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if isinstance(payload, dict) and isinstance(payload.get("warnings"), list):
            payload["warnings"] = [_for_agent(w) for w in payload["warnings"]]
        if isinstance(payload, dict) and isinstance(payload.get("search_warnings"), list):
            payload["search_warnings"] = [_for_agent(w) for w in payload["search_warnings"]]
        return self._ok(request_id, {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, default=str)}],
            "isError": False,
        })

    @staticmethod
    def _ok(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _err(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def serve(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write(self._err(None, PARSE_ERROR, f"Invalid JSON: {exc}"))
                continue
            if not isinstance(request, dict):
                self._write(self._err(None, INVALID_REQUEST, "Request must be a JSON object"))
                continue
            response = self.handle(request)
            if response is not None:
                self._write(response)
        return 0

    def _write(self, payload: Dict[str, Any]) -> None:
        self.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self.stdout.flush()


def serve_stdio(client: Optional[ChototClient] = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    return McpServer(client or ChototClient()).serve()
