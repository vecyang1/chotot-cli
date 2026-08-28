# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Rendering: tables, cards, dashboards, JSON, CSV, Markdown.

Two things this module refuses to do:

* **Print a number the API did not state.** ``None`` renders as ``-``. A seller
  whose sold-count is unknown must not read as "0 sold".
* **Emit a CSV cell a spreadsheet will execute.** Values starting ``= + - @``
  (or a leading tab/CR) are prefixed with an apostrophe. Chợ Tốt titles are
  attacker-controlled free text, and an exported file is usually opened in
  Excel.

Colour is emitted only when stdout is a TTY, so piped output stays clean.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO

from chotot.analyzer import PriceReport
from chotot.client import SearchResult
from chotot.models import Listing, ListingDetail

#: Cell prefixes a spreadsheet interprets as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}


def supports_colour(stream: TextIO = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class Palette:
    """Colour helpers that degrade to plain text when colour is off."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_ANSI.get(s, "") for s in styles)
        return f"{prefix}{text}{_ANSI['reset']}"


def format_vnd(amount: Optional[int], *, short: bool = False) -> str:
    """Render VND. ``None`` becomes ``-``, never ``0 đ``."""
    if amount is None:
        return "-"
    if amount < 0:
        # A negative price is not a marketplace state; showing it as
        # "Negotiable" turned bad input into a plausible-looking answer.
        return f"invalid ({amount:,})".replace(",", ".")
    if amount == 0:
        return "Negotiable"
    if short:
        if amount >= 1_000_000_000:
            return f"{amount / 1_000_000_000:.2f}B đ".replace(".00B", "B")
        if amount >= 1_000_000:
            return f"{amount / 1_000_000:.1f}M đ".replace(".0M", "M")
        if amount >= 1_000:
            return f"{amount / 1_000:.0f}K đ"
    return f"{amount:,} đ".replace(",", ".")


#: Matches SGR colour escapes so they can be excluded from width measurement.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def display_width(text: str) -> int:
    """Columns a terminal will actually use to draw ``text``.

    Two things make ``len()`` wrong here, and both were visibly breaking the
    tables:

    * ANSI colour escapes are zero-width but counted by ``len``, so every
      coloured cell padded 13 characters short and the borders no longer lined
      up -- with colour on, which is the default in a terminal.
    * East Asian wide characters and emoji occupy two columns each. Vietnamese
      listings routinely carry both.
    """
    plain = _ANSI_RE.sub("", text)
    width = 0
    for char in plain:
        if unicodedata.combining(char):
            continue
        if unicodedata.east_asian_width(char) in ("W", "F") or ord(char) >= 0x1F000:
            width += 2
        else:
            width += 1
    return width


def _pad(text: str, width: int) -> str:
    """Left-justify to a DISPLAY width, ignoring escapes and counting wide glyphs."""
    return text + " " * max(0, width - display_width(text))


def _label(palette: "Palette", text: str, width: int, *styles: str) -> str:
    """A colourised, display-width-padded key for the key/value views.

    Formatting a coloured string with a width spec pads the *escaped* text, so
    the escape bytes count toward the width and every value moves left by their
    length. Pad first, colour second.
    """
    return palette(_pad(text, width), *(styles or ("dim",)))


def terminal_width(default: int = 100) -> int:
    """Usable columns, or ``default`` when not attached to a terminal.

    Piped output keeps a stable width so redirected tables and the README stay
    reproducible.
    """
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return default
    return max(60, shutil.get_terminal_size(fallback=(default, 24)).columns)


def _text(value: Any, dash: str = "-") -> str:
    if value is None or value == "":
        return dash
    return str(value)


def _truncate(text: str, width: int) -> str:
    """Trim to a DISPLAY width, keeping whole characters."""
    if width <= 1 or display_width(text) <= width:
        return text
    out = ""
    used = 0
    for char in text:
        step = display_width(char)
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "…"


def _relative_age(moment: Optional[datetime], fallback: Optional[str] = None) -> str:
    """Render an age. Falls back to the upstream relative string.

    The storefront endpoint omits ``list_time`` but does state ``date`` ("2 giờ
    trước"), so showing "-" discarded the only age it gave.
    """
    if moment is None:
        return _text(fallback)
    from datetime import timezone

    delta = datetime.now(timezone.utc) - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return moment.strftime("%Y-%m-%d")
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d ago"
    return moment.strftime("%Y-%m-%d")


def sanitise_csv_cell(value: Any) -> str:
    """Neutralise spreadsheet formula injection in an exported cell."""
    text = "" if value is None else str(value)
    if text and text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def to_json(payload: Any, indent: int = 2) -> str:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
        return str(obj)

    return json.dumps(payload, ensure_ascii=False, indent=indent, default=default)


# -- tables ----------------------------------------------------------------

def render_table(rows: Sequence[Sequence[str]], headers: Sequence[str],
                 palette: Optional[Palette] = None) -> str:
    """A plain box table sized to its content."""
    palette = palette or Palette(False)
    columns = len(headers)
    widths = [display_width(str(h)) for h in headers]
    for row in rows:
        for i in range(columns):
            widths[i] = max(widths[i], display_width(str(row[i])) if i < len(row) else 0)

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [line("┌", "┬", "┐")]
    # Pad BEFORE colouring: an escape sequence added first would be counted as
    # width by any later ljust.
    out.append("│" + "│".join(f" {palette(_pad(str(h), widths[i]), 'bold')} "
                              for i, h in enumerate(headers)) + "│")
    out.append(line("├", "┼", "┤"))
    for row in rows:
        cells = []
        for i in range(columns):
            cell = str(row[i]) if i < len(row) else ""
            cells.append(f" {_pad(cell, widths[i])} ")
        out.append("│" + "│".join(cells) + "│")
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def listings_table(listings: Sequence[Listing], palette: Optional[Palette] = None,
                   title_width: Optional[int] = None, width: Optional[int] = None) -> str:
    """Render listings, fitting the flexible columns to the terminal.

    Title and Location absorb whatever is left after the fixed columns, Title
    shrinking first, so an 80-column terminal gets a narrower but intact table
    rather than one wrapped into unreadable fragments.
    """
    palette = palette or Palette(False)
    headers = ["#", "Price", "Title", "Location", "Cond", "Age", "ID"]

    available = width or terminal_width()
    # Fixed columns plus borders and per-cell padding.
    overhead = 8 * 3 + len(str(len(listings))) + 9 + 6 + 8 + 10
    flexible = max(28, available - overhead)
    if title_width is None:
        title_width = max(16, int(flexible * 0.62))
    location_width = max(12, flexible - title_width)
    rows = []
    for index, listing in enumerate(listings, 1):
        location = ", ".join(
            p for p in (listing.area_name, listing.region_name_modern) if p) or "-"
        condition = {1: "New", 2: "Used", 3: "Repair"}.get(listing.condition_code or 0, "-")
        rows.append([
            str(index),
            palette(format_vnd(listing.price, short=True), "green", "bold"),
            _truncate(listing.subject, title_width),
            _truncate(location, location_width),
            condition,
            _truncate(_relative_age(listing.posted_at, listing.posted_label), 12),
            str(listing.list_id),
        ])
    return render_table(rows, headers, palette)


def search_summary(result: SearchResult, palette: Optional[Palette] = None) -> str:
    palette = palette or Palette(False)
    coverage = result.coverage
    total = coverage.reported_total
    if total is None:
        total_text = "unknown"
    elif coverage.total_is_capped:
        reason = {
            "upstream_cap": "upstream cap",
            "regions_skipped": "part of the province not queried",
        }.get(coverage.total_floor_reason or "", "floor")
        total_text = f"≥{total:,} ({reason})"
    else:
        total_text = f"{total:,}"
    return palette(
        f"{coverage.returned} shown · upstream matches: {total_text} · "
        f"{coverage.requests} request(s) · {coverage.fetched} fetched"
        + (f" · {coverage.duplicates_dropped} dupes removed" if coverage.duplicates_dropped else "")
        + (f" · {coverage.filtered_out} filtered client-side" if coverage.filtered_out else ""),
        "dim",
    )


def render_warnings(warnings: Sequence[str], palette: Optional[Palette] = None) -> str:
    """Warnings are for stderr so ``--json`` piped to a file stays parseable."""
    palette = palette or Palette(False)
    return "\n".join(palette(f"! {w}", "yellow") for w in warnings)


# -- detail card -----------------------------------------------------------

def detail_card(detail: ListingDetail, palette: Optional[Palette] = None) -> str:
    palette = palette or Palette(False)
    listing = detail.listing
    out: List[str] = []
    out.append(palette(listing.subject, "bold", "cyan"))
    out.append(palette("─" * min(len(listing.subject), 72), "dim"))
    out.append(f"{palette('Price', 'bold'):<22} {palette(format_vnd(listing.price), 'green', 'bold')}"
               + (f"  ({listing.price_label})" if listing.price_label else ""))

    location_parts = [p for p in (listing.ward_name, listing.area_name, listing.region_name_modern) if p]
    rows = [
        ("Listing ID", str(listing.list_id)),
        ("Category", _text(listing.category_name)),
        ("Condition", _text(listing.condition_label)),
        ("Location", ", ".join(location_parts) if location_parts else "-"),
        ("Address", _text(detail.detail_address)),
        ("Posted", (listing.posted_at.strftime("%Y-%m-%d %H:%M UTC") if listing.posted_at else "-")
                   + (f"  ({_relative_age(listing.posted_at)})" if listing.posted_at else "")),
        ("Status", f"{_text(detail.status)}/{_text(detail.state)}"
                   + ("  ACTIVE" if detail.is_active else "  NOT ACTIVE")),
    ]
    if detail.latitude is not None and detail.longitude is not None:
        rows.append(("Coordinates", f"{detail.latitude}, {detail.longitude}"))
    if detail.phone_masked:
        # Already masked upstream; shown as-is and labelled so nobody reads it
        # as a complete number.
        rows.append(("Phone (masked)", detail.phone_masked))
    for label, value in rows:
        out.append(f"{_label(palette, label, 22, 'bold')} {value}")

    seller = listing.seller
    out.append("")
    out.append(palette("Seller", "bold", "magenta"))
    seller_rows = [
        ("Name", _text(seller.name)),
        ("Account ID", _text(seller.account_id)),
        ("Type", "Shop / professional" if seller.is_shop else "Individual"),
        ("Verified shop", "yes" if seller.is_verified_shop else "no"),
        ("Ads sold", _text(seller.sold_ads)),
        ("Rating", f"{seller.average_rating:.1f}" if seller.average_rating is not None else "-"),
        ("Ratings count", _text(seller.total_rating)),
        ("Shop alias", _text(seller.shop_alias)),
    ]
    for label, value in seller_rows:
        out.append(f"  {_label(palette, label, 24, 'dim')} {value}")

    if detail.specs:
        out.append("")
        out.append(palette("Specifications", "bold", "magenta"))
        for label, value in detail.specs.items():
            out.append(f"  {_label(palette, label, 28, 'dim')} {value}")

    if listing.body:
        out.append("")
        out.append(palette("Description", "bold", "magenta"))
        for line in listing.body.splitlines():
            out.append(f"  {line}")

    if listing.images:
        out.append("")
        out.append(palette(f"Images ({len(listing.images)})", "bold", "magenta"))
        for url in listing.images[:5]:
            out.append(f"  {url}")
        if len(listing.images) > 5:
            out.append(palette(f"  … {len(listing.images) - 5} more", "dim"))

    out.append("")
    out.append(palette(listing.url, "blue"))
    return "\n".join(out)


# -- analytics dashboard ---------------------------------------------------

def _bar(fraction: float, width: int = 28) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "·" * (width - filled)


def price_dashboard(report: PriceReport, palette: Optional[Palette] = None) -> str:
    palette = palette or Palette(False)
    out: List[str] = []
    out.append(palette(f"Asking-price analysis — {report.query}", "bold", "cyan"))
    out.append(palette("─" * 72, "dim"))

    # State the basis of the numbers below, not a nearby number. `priced_count`
    # is pre-trim and `sample_size` is the deduplicated pool, so naming either
    # as "the sample" overstates the basis by the outliers removed.
    out.append(
        f"Sample: {report.sample_size} listings analysed"
        + (f", {report.unpriced_count} without a price" if report.unpriced_count else "")
        + (f", {report.outliers_removed} outliers removed" if report.outliers_removed else "")
    )
    out.append(
        f"Statistics below are computed over "
        f"{palette(str(report.analysed_count), 'bold')} priced listings."
    )
    if not report.is_confident:
        out.append(palette("  small sample — treat as indicative", "yellow"))
    out.append("")

    if report.median is None:
        out.append(palette("No priced listings — no statistics available.", "yellow"))
        return "\n".join(out)

    stats = [
        ("Minimum", report.minimum), ("P10", report.p10), ("P25", report.p25),
        ("Median", report.median), ("P75", report.p75), ("P90", report.p90),
        ("Maximum", report.maximum),
    ]
    for label, value in stats:
        if value is None:
            out.append(f"  {_label(palette, label, 20, 'dim')} {palette('withheld (sample too small)', 'dim')}")
        else:
            emphasis = ("green", "bold") if label == "Median" else ()
            out.append(f"  {_label(palette, label, 20, 'dim')} {palette(format_vnd(value), *emphasis)}")

    out.append("")
    out.append(f"  {palette('Mean', 'dim'):<20} {format_vnd(report.mean)}")
    out.append(f"  {palette('Trimmed mean', 'dim'):<20} {format_vnd(report.trimmed_mean)}")
    if report.stdev is not None:
        out.append(f"  {palette('Std deviation', 'dim'):<20} {format_vnd(report.stdev)}")

    if report.fair_range:
        low, high = report.fair_range
        out.append("")
        out.append(palette(f"Typical asking range  {format_vnd(low)} — {format_vnd(high)}",
                           "bold", "green"))

    if len({t for t in report.by_listing_type if t in ("s", "u")}) > 1:
        out.append("")
        out.append(palette("MIXED SALE AND RENTAL LISTINGS — figures above are not comparable",
                           "bold", "red"))
        out.append(palette("  Re-run with --listing-type sale or --listing-type rent.", "yellow"))

    if report.by_condition:
        out.append("")
        out.append(palette("By condition", "bold", "magenta"))
        for entry in report.by_condition:
            out.append(f"  {entry.condition:<36} n={entry.count:<4} "
                       f"median {format_vnd(entry.median, short=True)}")

    if report.histogram:
        out.append("")
        out.append(palette("Distribution", "bold", "magenta"))
        peak = max((b["count"] for b in report.histogram), default=0) or 1
        for bucket in report.histogram:
            label = f"{format_vnd(bucket['from'], short=True)}–{format_vnd(bucket['to'], short=True)}"
            out.append(f"  {label:<22} {_bar(bucket['count'] / peak)} "
                       f"{bucket['count']:>4}  {bucket['percentage']:>5.1f}%")
    return "\n".join(out)


# -- exports ---------------------------------------------------------------

CSV_COLUMNS = [
    "list_id", "url", "subject", "price_vnd", "price_label",
    # Without this a property export mixes ₫3,000,000,000 sale prices with
    # ₫10,000,000 monthly rents and nothing in the file distinguishes them.
    "listing_type", "listing_type_label",
    "category_id", "category_name", "condition_code", "condition_label",
    "region_name_modern", "region_name", "area_name", "ward_name",
    "posted_at_utc", "posted_relative",
    "seller_name", "seller_account_id", "seller_sold_ads", "seller_rating",
    "is_company_ad", "image",
]


#: Excel on Windows assumes the system codepage for a .csv without a byte-order
#: mark, which turns "Quận Gò Vấp" into mojibake. The BOM is the only signal it
#: honours, and utf-8-sig readers (including Python's csv) strip it transparently.
CSV_BOM = "\ufeff"


def listings_csv(listings: Sequence[Listing], bom: bool = True) -> str:
    """Render listings as CSV.

    Args:
        listings: Rows to write.
        bom: Prefix a UTF-8 BOM so Excel reads Vietnamese correctly. Pass False
            for pipelines that want bytes without it.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for listing in listings:
        record = listing.to_dict()
        writer.writerow({key: sanitise_csv_cell(record.get(key)) for key in CSV_COLUMNS})
    return (CSV_BOM if bom else "") + buffer.getvalue()


def listings_markdown(listings: Sequence[Listing], title: str = "Chợ Tốt listings") -> str:
    out = [f"# {title}", "", f"{len(listings)} listings.", ""]
    out.append("| Price | Type | Title | Location | Condition | Posted | Link |")
    out.append("|---|---|---|---|---|---|---|")
    for listing in listings:
        location = ", ".join(p for p in (listing.area_name, listing.region_name_modern) if p) or "-"
        posted = listing.posted_at.strftime("%Y-%m-%d") if listing.posted_at else "-"
        # Pipes inside free text would break the row.
        subject = listing.subject.replace("|", "\\|")
        kind = {"s": "sale", "u": "rent", "k": "want-buy", "h": "want-rent"}.get(
            listing.listing_type or "", "-")
        out.append(
            f"| {format_vnd(listing.price, short=True)} | {kind} | {subject} | {location} | "
            f"{_text(listing.condition_label)} | {posted} | [{listing.list_id}]({listing.url}) |"
        )
    return "\n".join(out) + "\n"
