"""Taxonomy resolution, including the 2025 province merger.

The predecessor's hand-written table had 11 of 13 provinces returning zero ads.
These tests grade the bundled snapshot structurally so that rot is caught
offline, and report how many subjects they graded.
"""
from __future__ import annotations

import pytest

from chotot import taxonomy
from chotot.errors import ResolutionError


def test_snapshot_is_populated():
    assert len(taxonomy.provinces()) >= 60
    assert len(taxonomy.categories()) >= 50
    assert len(taxonomy.districts()) >= 200


def test_every_province_has_the_fields_resolution_depends_on():
    graded = 0
    for code, entry in taxonomy.provinces().items():
        graded += 1
        assert entry["region_v2"] == int(code)
        assert entry["name"]
        assert isinstance(entry.get("aliases"), list)
        assert isinstance(entry.get("sibling_region_v2"), list)
    assert graded == len(taxonomy.provinces()) >= 60


def test_no_province_uses_the_bogus_round_codes_of_the_old_table():
    """11000, 14000, 15000... returned zero ads. They must not reappear."""
    codes = set(taxonomy.provinces())
    for bogus in ("11000", "14000", "15000", "16000", "17000",
                  "18000", "19000", "20000", "21000", "22000"):
        assert bogus not in codes, f"{bogus} is a known-dead region code"


@pytest.mark.parametrize("alias,expected_modern", [
    ("hcm", "TP Hồ Chí Minh"), ("saigon", "TP Hồ Chí Minh"),
    ("hanoi", "TP Hà Nội"), ("ha noi", "TP Hà Nội"),
    ("da nang", "TP Đà Nẵng"), ("can tho", "TP Cần Thơ"),
])
def test_common_aliases_resolve_to_the_modern_province(alias, expected_modern):
    codes = taxonomy.province_codes(alias)
    assert codes
    assert taxonomy.province_name(codes[0]) == expected_modern


def test_merged_provinces_expand_to_every_legacy_code():
    """Vietnam merged provinces in 2025; Chợ Tốt kept one code per old province.

    Resolving to a single code silently drops the rest of the modern province.
    """
    hcm = taxonomy.province_codes("hcm")
    assert len(hcm) >= 2, "HCM must expand to its merged legacy codes"
    assert 13000 in hcm

    da_nang = taxonomy.province_codes("da nang")
    assert set(da_nang) >= {3016, 3017}, "Đà Nẵng must include former Quảng Nam (3016)"


def test_expansion_can_be_disabled_for_a_single_subdivision():
    assert len(taxonomy.province_codes("hcm", expand=False)) == 1


def test_legacy_name_still_resolves_after_the_merger():
    """Someone typing the pre-merger name must still find the listings."""
    assert taxonomy.province_codes("quang nam")


def test_modern_groups_cover_every_resolvable_province():
    groups = taxonomy.modern_groups()
    assert 25 <= len(groups) <= 40, f"expected ~34 modern provinces, got {len(groups)}"
    for name, codes in groups.items():
        assert codes and all(str(c) in taxonomy.provinces() for c in codes)


def test_unknown_province_raises_with_a_suggestion():
    with pytest.raises(ResolutionError) as excinfo:
        taxonomy.resolve_province("ha noii")
    assert "Did you mean" in str(excinfo.value)


def test_dead_round_code_is_refused_with_an_explanation():
    with pytest.raises(ResolutionError) as excinfo:
        taxonomy.resolve_province(11000)
    assert "3017" in str(excinfo.value), "the error should name a code that works"


def test_empty_input_resolves_to_nothing_rather_than_a_default():
    assert taxonomy.resolve_province(None) is None
    assert taxonomy.resolve_category(None) is None
    assert taxonomy.province_codes(None) == []


@pytest.mark.parametrize("value,expected", [
    ("phone", 5010), ("5010", 5010), (5010, 5010), ("dien thoai", 5010),
])
def test_category_resolution(value, expected):
    assert taxonomy.resolve_category(value) == expected


def test_unknown_category_raises():
    with pytest.raises(ResolutionError):
        taxonomy.resolve_category("teleportation devices")


def test_district_resolves_within_its_province():
    code = taxonomy.resolve_district("quan go vap", 13000)
    assert taxonomy.districts()[str(code)]["region_v2"] == 13000


def test_district_index_has_no_self_duplicates():
    """id/name/slug fold to the same key; a district must not look ambiguous with itself."""
    for key, codes in taxonomy._district_index().items():
        assert len(codes) == len(set(codes)), f"{key} maps to duplicate codes {codes}"


def test_no_alias_points_at_two_different_modern_provinces():
    """An ambiguous alias would resolve a user's city to someone else's."""
    owners = {}
    for entry in taxonomy.provinces().values():
        owner = entry.get("modern_name") or entry["name"]
        for alias in entry.get("aliases", []):
            owners.setdefault(alias, set()).add(owner)
    ambiguous = {a: o for a, o in owners.items() if len(o) > 1}
    assert not ambiguous, f"aliases resolve to multiple provinces: {ambiguous}"


def test_normalise_folds_vietnamese_diacritics():
    assert taxonomy.normalise("Đà Nẵng") == "da nang"
    assert taxonomy.normalise("Tp Hồ Chí Minh") == "tp ho chi minh"


def test_english_category_aliases_name_the_right_category():
    """These were guessed once and "computer" landed on tablets, so a desktop
    search silently searched tablets. Each pair is graded against the bundled
    Vietnamese name rather than against another guess."""
    expected = {
        "phone": "Điện thoại", "laptop": "Laptop", "tablet": "Máy tính bảng",
        "desktop": "Máy tính để bàn", "pc": "Máy tính để bàn",
        "camera": "Máy ảnh, Máy quay", "car": "Ô tô", "motorbike": "Xe máy",
    }
    for english, vietnamese in expected.items():
        code = taxonomy.resolve_category(english)
        assert taxonomy.category_name(code) == vietnamese, \
            f"{english!r} -> {taxonomy.category_name(code)!r}, expected {vietnamese!r}"


def test_an_ambiguous_english_word_is_refused_rather_than_guessed():
    """"computer" names a laptop, a desktop or a tablet depending on who says it."""
    with pytest.raises(ResolutionError):
        taxonomy.resolve_category("computer")
