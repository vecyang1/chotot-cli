# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

"""Category / province / district resolution.

Codes are loaded from ``chotot/data/*.json``, which ``tools/build_taxonomy.py``
generates from the live gateway. Nothing here is hand-maintained, for a reason
worth recording: the previous hand-written table mapped Đà Nẵng to ``11000`` and
Cần Thơ to ``14000``. Eleven of its thirteen provinces returned **zero** ads,
so ``search -r da-nang`` answered "nothing found" for a city with thousands of
listings — a wrong answer that looks exactly like a right one.

The real geo model, verified by probe:

* ``region_v2`` identifies a **province**: ``12000`` Hà Nội, ``13000`` HCM, and
  ``macro_id * 1000 + area_id`` for every other province.
* ``area_v2`` identifies a **district**: ``12000+d`` / ``13000+d`` inside the two
  cities, and a seven-digit ``{region_v2}{dd}`` elsewhere.

Resolution is deliberately strict. An unknown name raises with near-miss
suggestions rather than falling back to "search everywhere", because a silently
dropped location filter returns a plausible national result set that the user
reads as local.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from chotot.errors import ResolutionError

DATA_DIR = Path(__file__).resolve().parent / "data"


def normalise(text: str) -> str:
    """Fold Vietnamese text for matching: 'Đà Nẵng' -> 'da nang'."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _key(text: str) -> str:
    """Matching key: normalised and space-free, so 'ha-noi' == 'ha noi'."""
    return normalise(text).replace(" ", "")


@lru_cache(maxsize=None)
def _load(name: str) -> Dict[str, Any]:
    path = DATA_DIR / name
    if not path.exists():
        raise ResolutionError(
            f"Bundled taxonomy file is missing: {path}",
            remedy="Reinstall chotot-cli, or regenerate it with "
                   "'python3 tools/build_taxonomy.py'.",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def categories() -> Dict[str, Dict[str, Any]]:
    return _load("categories.json")["categories"]


def provinces() -> Dict[str, Dict[str, Any]]:
    return _load("regions.json")["provinces"]


def districts() -> Dict[str, Dict[str, Any]]:
    return _load("regions.json")["districts"]


def snapshot_date(which: str = "regions.json") -> str:
    """When the bundled snapshot was taken; surfaced so staleness is visible."""
    return _load(which).get("fetched_at", "unknown")


@lru_cache(maxsize=None)
def _category_index() -> Dict[str, int]:
    index: Dict[str, int] = {}
    for cid, entry in categories().items():
        index[_key(cid)] = int(cid)
        index[_key(entry["name"])] = int(cid)
        index[_key(entry["slug"])] = int(cid)
        for alias in entry.get("aliases", []):
            index.setdefault(_key(alias), int(cid))
    # English conveniences, each pointing at the category the word actually
    # names. These were guessed once and "computer" landed on 5040
    # "Máy tính bảng" -- tablets -- so a desktop search silently searched
    # tablets. tests/test_taxonomy.py now asserts every pair against the
    # bundled Vietnamese name.
    for english, target in (
        ("phone", "5010"), ("phones", "5010"), ("mobile", "5010"),
        ("laptop", "5030"), ("laptops", "5030"),
        ("tablet", "5040"), ("tablets", "5040"),
        ("desktop", "5070"), ("pc", "5070"),
        ("camera", "5050"), ("cameras", "5050"),
        ("electronics", "5000"),
        ("car", "2010"), ("cars", "2010"),
        ("motorbike", "2020"), ("motorcycle", "2020"),
        ("property", "1000"), ("realestate", "1000"), ("real estate", "1000"),
        ("job", "13010"), ("jobs", "13010"), ("recruitment", "13010"),
        ("home services", "15000"), ("home service", "15000"),
        ("cleaning", "15010"), ("moving", "15020"),
        ("appliance repair", "15030"),
    ):
        if target in categories():
            index.setdefault(_key(english), int(target))
    return index


@lru_cache(maxsize=None)
def _province_index() -> Dict[str, int]:
    index: Dict[str, int] = {}
    for pid, entry in provinces().items():
        index[_key(pid)] = int(pid)
        index[_key(entry["name"])] = int(pid)
        index[_key(entry["slug"])] = int(pid)
        for alias in entry.get("aliases", []):
            index.setdefault(_key(alias), int(pid))
    for extra, target in (
        ("saigon", "13000"), ("sg", "13000"), ("hcmc", "13000"), ("tphcm", "13000"),
        ("hanoi", "12000"), ("hn", "12000"),
    ):
        if target in provinces():
            index.setdefault(extra, int(target))
    return index


@lru_cache(maxsize=None)
def _district_index() -> Dict[str, List[int]]:
    """District names repeat across provinces ('Quận 1'), so values are lists."""
    index: Dict[str, List[int]] = {}
    for did, entry in districts().items():
        # id/name/slug often fold to the same key; a set keeps one district
        # from looking like an ambiguous pair of itself.
        for token in {_key(did), _key(entry["name"]), _key(entry["slug"])}:
            bucket = index.setdefault(token, [])
            if int(did) not in bucket:
                bucket.append(int(did))
    return index


def _suggest(needle: str, haystack: List[str], limit: int = 4) -> List[str]:
    return difflib.get_close_matches(normalise(needle), haystack, n=limit, cutoff=0.55)


def resolve_category(value: Optional[Union[str, int]]) -> Optional[int]:
    """Resolve a category name/slug/code to its numeric id. ``None`` passes through."""
    if value is None or value == "":
        return None
    if isinstance(value, int) or str(value).strip().isdigit():
        code = int(str(value).strip())
        if str(code) not in categories():
            raise ResolutionError(
                f"Unknown category code: {code}",
                remedy="Run 'chotot categories' to list valid codes.",
            )
        return code
    found = _category_index().get(_key(str(value)))
    if found is None:
        names = [normalise(e["name"]) for e in categories().values()]
        hint = _suggest(str(value), names)
        raise ResolutionError(
            f"Unknown category: {value!r}"
            + (f". Did you mean: {', '.join(hint)}?" if hint else "."),
            remedy="Run 'chotot categories' to list every category and code.",
        )
    return found


def resolve_province(value: Optional[Union[str, int]]) -> Optional[int]:
    """Resolve a province name/slug/alias/code to its ``region_v2`` id."""
    if value is None or value == "":
        return None
    if isinstance(value, int) or str(value).strip().isdigit():
        code = int(str(value).strip())
        if str(code) not in provinces():
            known = ", ".join(sorted(provinces())[:5])
            raise ResolutionError(
                f"Unknown province code: {code}. Note that region_v2 is not a "
                f"round number for most provinces (e.g. Đà Nẵng is 3017, not 11000).",
                remedy=f"Run 'chotot regions' to list valid codes (e.g. {known}).",
            )
        return code
    found = _province_index().get(_key(str(value)))
    if found is None:
        names = [normalise(e["name"]) for e in provinces().values()]
        hint = _suggest(str(value), names)
        raise ResolutionError(
            f"Unknown province: {value!r}"
            + (f". Did you mean: {', '.join(hint)}?" if hint else "."),
            remedy="Run 'chotot regions --search <name>' to find the code.",
        )
    return found


def modern_groups() -> Dict[str, List[int]]:
    """Modern province name -> every legacy ``region_v2`` that now belongs to it."""
    return _load("regions.json").get("modern_groups", {})


def modern_name(region_v2: int) -> Optional[str]:
    entry = provinces().get(str(region_v2))
    return entry.get("modern_name") if entry else None


def province_codes(value: Optional[Union[str, int]], expand: bool = True) -> List[int]:
    """Resolve a province to EVERY ``region_v2`` code that now serves it.

    Vietnam merged its provinces in 2025 but Chợ Tốt kept the old codes, so one
    modern province is often served by two or three of them. ``TP Hồ Chí Minh``
    is ``[2010, 2011, 13000]``: querying only ``13000`` silently drops every
    listing in Bình Dương and Bà Rịa - Vũng Tàu, which are now part of the city.

    Args:
        value: Province name, alias, slug, or numeric ``region_v2``.
        expand: When True (default) return the whole merger group. When False
            return only the code that matched, for callers that deliberately
            want one legacy subdivision.

    Returns:
        Sorted list of codes, or ``[]`` when ``value`` is empty.
    """
    resolved = resolve_province(value)
    if resolved is None:
        return []
    if not expand:
        return [resolved]
    entry = provinces().get(str(resolved), {})
    siblings = entry.get("sibling_region_v2") or []
    return sorted(siblings) if siblings else [resolved]


def resolve_district(
    value: Optional[Union[str, int]],
    province_v2: Optional[int] = None,
) -> Optional[int]:
    """Resolve a district to ``area_v2``, disambiguating by province when given.

    District names are not unique nationally -- 'Quận 1' exists in several
    provinces -- so an ambiguous name without a province raises rather than
    silently picking the first match.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int) or str(value).strip().isdigit():
        code = int(str(value).strip())
        if str(code) not in districts():
            raise ResolutionError(
                f"Unknown district code: {code}",
                remedy="Run 'chotot regions --province <name>' to list districts.",
            )
        return code

    matches = list(_district_index().get(_key(str(value)), []))
    if province_v2 is not None:
        matches = [m for m in matches if districts()[str(m)]["region_v2"] == province_v2]
    if not matches:
        pool = districts()
        if province_v2 is not None:
            pool = {k: v for k, v in pool.items() if v["region_v2"] == province_v2}
        hint = _suggest(str(value), [normalise(e["name"]) for e in pool.values()])
        scope = f" in {province_name(province_v2)}" if province_v2 else ""
        raise ResolutionError(
            f"Unknown district: {value!r}{scope}"
            + (f". Did you mean: {', '.join(hint)}?" if hint else "."),
            remedy="Run 'chotot regions --province <name>' to list its districts.",
        )
    if len(matches) > 1:
        where = ", ".join(
            f"{districts()[str(m)]['name']} ({province_name(districts()[str(m)]['region_v2'])}) = {m}"
            for m in sorted(matches)[:6]
        )
        raise ResolutionError(
            f"District {value!r} is ambiguous across provinces: {where}",
            remedy="Pass --region to disambiguate, or use the numeric area code.",
        )
    return matches[0]


def category_name(code: Optional[int]) -> str:
    if code is None:
        return "All categories"
    entry = categories().get(str(code))
    return entry["name"] if entry else f"Category {code}"


def province_name(code: Optional[int], prefer_modern: bool = True) -> str:
    """Display name for a province code.

    Defaults to the post-2025-merger name, because that is what the website and
    the ads themselves show. The pre-merger name stays reachable via
    ``prefer_modern=False`` so a user who typed the old name still sees it.
    """
    if code is None:
        return "All Vietnam"
    entry = provinces().get(str(code))
    if not entry:
        return f"Region {code}"
    if prefer_modern:
        return entry.get("modern_name") or entry["name"]
    return entry["name"]


def district_name(code: Optional[int]) -> str:
    if code is None:
        return ""
    entry = districts().get(str(code))
    return entry["name"] if entry else f"Area {code}"


def child_categories(parent: Optional[int]) -> List[Dict[str, Any]]:
    return sorted(
        (e for e in categories().values() if e.get("parent") == parent),
        key=lambda e: e["id"],
    )


def districts_of(province_v2: int) -> List[Dict[str, Any]]:
    return sorted(
        (e for e in districts().values() if e["region_v2"] == province_v2),
        key=lambda e: e["area_v2"],
    )


def search_provinces(needle: str) -> List[Dict[str, Any]]:
    key = normalise(needle)
    return sorted(
        (e for e in provinces().values()
         if key in normalise(e["name"]) or key in normalise(e["slug"])
         or key in normalise(e.get("modern_name") or "")
         or any(key in normalise(a) for a in e.get("aliases", []))),
        key=lambda e: e["region_v2"],
    )
