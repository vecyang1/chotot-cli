"""Rendering, export safety, and the refusal to print numbers nobody stated."""
from __future__ import annotations

import csv
import io
import json

import pytest

from chotot.formatter import (
    CSV_COLUMNS, Palette, format_vnd, listings_csv, listings_markdown,
    listings_table, sanitise_csv_cell, to_json,
)
from chotot.models import Listing


def make(**overrides):
    ad = {"list_id": 1, "subject": "Test", "price": 1_500_000, "elt_condition": 2,
          "list_time": 1787892561000, "region_v2": 13000, "area_name": "Quận 1"}
    ad.update(overrides)
    return Listing.from_ad(ad)


@pytest.mark.parametrize("value,expected", [
    (None, "-"),
    (0, "Negotiable"),
    (1_500_000, "1.500.000 đ"),
])
def test_vnd_formatting(value, expected):
    assert format_vnd(value) == expected


def test_missing_price_never_renders_as_zero_dong():
    """'0 đ' would read as a free item; the price is simply unknown."""
    assert format_vnd(None) == "-"
    assert "0" not in format_vnd(None)


def test_short_form_is_compact():
    assert format_vnd(11_000_000, short=True) == "11M đ"
    assert format_vnd(1_500_000_000, short=True) == "1.50B đ"


@pytest.mark.parametrize("payload", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_csv_injection_is_neutralised(payload):
    """Listing titles are attacker-controlled and exports open in Excel."""
    assert sanitise_csv_cell(payload).startswith("'")


def test_benign_csv_cells_are_untouched():
    """The negative side: quoting everything would corrupt ordinary data."""
    for benign in ("iPhone 13", "1500000", "Quận 1", ""):
        assert sanitise_csv_cell(benign) == benign


def _read_csv(text):
    """Read as a utf-8-sig consumer would, so the BOM is stripped from key one."""
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


def test_csv_export_round_trips_with_a_malicious_title():
    listings = [make(subject="=HYPERLINK(\"http://evil\",\"click\")")]
    rows = _read_csv(listings_csv(listings))
    assert len(rows) == 1
    assert rows[0]["subject"].startswith("'=")
    assert set(rows[0]) == set(CSV_COLUMNS)


def test_csv_starts_with_a_bom_so_excel_reads_vietnamese():
    """Without it Excel applies the system codepage and mojibakes 'Quận Gò Vấp'."""
    body = listings_csv([make(area_name="Quận Gò Vấp")])
    assert body.startswith("\ufeff")
    assert body.encode("utf-8").startswith(b"\xef\xbb\xbf")


def test_csv_bom_can_be_disabled_for_pipelines():
    assert not listings_csv([make()], bom=False).startswith("\ufeff")


def test_csv_bom_does_not_corrupt_the_first_column_name():
    rows = _read_csv(listings_csv([make()]))
    assert "list_id" in rows[0], "the BOM welded itself onto the first key"


def test_csv_carries_absolute_time_not_relative_text():
    """'2 giờ trước' in a file read next week is worthless."""
    rows = _read_csv(listings_csv([make()]))
    assert "T" in rows[0]["posted_at_utc"]


def test_csv_leaves_unknown_values_empty_rather_than_zero():
    rows = _read_csv(listings_csv([make(price=None)]))
    assert rows[0]["price_vnd"] == ""


def test_markdown_escapes_pipes_in_titles():
    body = listings_markdown([make(subject="a | b")])
    assert "a \\| b" in body
    # header + separator + one row
    assert len([l for l in body.splitlines() if l.startswith("|")]) == 3


def test_table_renders_without_colour_codes_when_disabled():
    output = listings_table([make()], Palette(False))
    assert "\033[" not in output
    assert "Test" in output


def test_table_renders_colour_when_enabled():
    assert "\033[" in listings_table([make()], Palette(True))


def test_json_is_parseable_and_preserves_vietnamese():
    payload = json.loads(to_json({"name": "Đà Nẵng", "n": 1}))
    assert payload["name"] == "Đà Nẵng"


# -- display width ---------------------------------------------------------

def test_display_width_ignores_ansi_escapes():
    """Escapes are zero-width; counting them broke every coloured table."""
    from chotot.formatter import display_width

    assert display_width("\033[1mabc\033[0m") == 3


def test_display_width_counts_wide_characters_as_two():
    from chotot.formatter import display_width

    assert display_width("小米") == 4
    assert display_width("📱") == 2
    assert display_width("Quận") == 4  # combining marks are zero-width


def test_coloured_table_rows_all_have_the_same_display_width():
    """The regression: with colour on, data rows were 73 columns to 86 borders."""
    from chotot.formatter import display_width

    listings = [make(subject="iPhone 13"), make(list_id=2, subject="📱 điện thoại 小米 手机")]
    output = listings_table(listings, Palette(True), width=100)
    widths = {display_width(line) for line in output.splitlines()}
    assert len(widths) == 1, f"table rows have mismatched widths: {widths}"


def test_table_fits_a_narrow_terminal():
    from chotot.formatter import display_width

    output = listings_table([make(subject="a very long title " * 6)], Palette(False), width=80)
    assert max(display_width(l) for l in output.splitlines()) <= 80


def test_truncation_respects_wide_characters():
    from chotot.formatter import _truncate, display_width

    assert display_width(_truncate("小米手机超长标题测试文本", 10)) <= 10


def test_negative_price_is_not_rendered_as_negotiable():
    """"Negotiable" is a marketplace state; bad input must not borrow it."""
    assert "Negotiable" not in format_vnd(-5000)
    assert format_vnd(0) == "Negotiable"


def test_exports_distinguish_sale_from_rent():
    """Otherwise a property export mixes ₫3B sale prices with ₫10M rents."""
    rows = _read_csv(listings_csv([make(list_id=1), make(list_id=2)]))
    assert "listing_type" in rows[0]
    assert "| Type |" in listings_markdown([make()])


def test_age_falls_back_to_the_upstream_relative_string():
    """The storefront omits list_time but does state "2 giờ trước"."""
    from chotot.models import Listing

    ad = {"list_id": 1, "subject": "x", "price": 1, "date": "2 giờ trước", "type": "s"}
    listing = Listing.from_ad(ad)
    assert listing.posted_at is None
    assert "giờ" in listings_table([listing], Palette(False), width=120)
