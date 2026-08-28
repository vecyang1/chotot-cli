# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

#!/usr/bin/env python3
"""Regenerate the bundled taxonomy snapshots from the live Chợ Tốt gateway.

Run this to refresh ``chotot/data/categories.json`` and ``chotot/data/regions.json``.
The CLI ships the generated snapshots so it works offline; this script is the only
thing that talks to the network, and it records the fetch date in the payload.

    python3 tools/build_taxonomy.py [--out chotot/data] [--harvest-areas]

The geo model is documented in ``docs/api-contract.md``; briefly:
  region_v2 (province)  = 12000 (Hà Nội) | 13000 (HCM) | macro_id*1000 + area_id
  area_v2   (district)  = 12000+d / 13000+d for HN/HCM, else f"{region_v2}{dd:02d}"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GATEWAY = "https://gateway.chotot.com/v1/public"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# Macro regions that are themselves a single province. Their sub-units are
# districts (area_v2), not provinces -- verified: region_v2=12079 returns nothing
# while area_v2=12079 returns Quận Cầu Giấy.
CITY_MACROS = {12: 12000, 13: 13000}


def _get(path: str, **params: Any) -> Dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{GATEWAY}/{path.lstrip('/')}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        # Decode explicitly: the gateway does not always declare a charset, and
        # utf-8-sig strips a BOM if one ever appears (a BOM welded onto the first
        # key would silently make that field unreadable).
        return json.loads(response.read().decode("utf-8-sig"))


def slugify(text: str) -> str:
    """Vietnamese-aware slug: 'Bà Rịa - Vũng Tàu' -> 'ba-ria-vung-tau'."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


# Prefixes stripped when generating the short alias for a province.
_PROVINCE_PREFIXES = ("tp-", "thanh-pho-", "tinh-")


def province_aliases(name: str) -> List[str]:
    """Aliases people actually type. Derived, never hand-maintained."""
    slug = slugify(name)
    aliases = {slug}
    bare = slug
    for prefix in _PROVINCE_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    aliases.add(bare)
    aliases.add(bare.replace("-", ""))
    # Initials, e.g. 'ba-ria-vung-tau' -> 'brvt'; only when it disambiguates.
    parts = [p for p in bare.split("-") if p]
    if len(parts) >= 2:
        aliases.add("".join(p[0] for p in parts))
    return sorted(a for a in aliases if a)


def fetch_categories() -> Dict[str, Any]:
    payload = _get("chapy-pro/categories")
    groups = payload.get("categories")
    if not groups:
        raise SystemExit("categories: gateway returned no 'categories' key")

    categories: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        gid = str(group.get("id") or "").strip()
        gname = (group.get("name") or "").strip()
        if not gid or not gname:
            continue
        categories[gid] = {
            "id": int(gid),
            "name": gname,
            "slug": slugify(gname),
            "parent": None,
            "aliases": province_aliases(gname),
        }
        for sub in group.get("subcategories") or []:
            sid = str(sub.get("id") or "").strip()
            sname = (sub.get("name") or "").strip()
            if not sid or not sname:
                continue
            categories[sid] = {
                "id": int(sid),
                "name": sname,
                "slug": slugify(sname),
                "parent": int(gid),
                "aliases": province_aliases(sname),
            }
    return categories


def fetch_regions() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (provinces, districts) keyed by their *_v2 id as strings."""
    payload = _get("chapy-pro/regions")
    macros = payload.get("regions")
    if not macros:
        raise SystemExit("regions: gateway returned no 'regions' key")

    provinces: Dict[str, Dict[str, Any]] = {}
    districts: Dict[str, Dict[str, Any]] = {}

    for entry in macros:
        for macro_id_raw, macro in entry.items():
            macro_id = int(macro_id_raw)
            macro_name = (macro.get("name") or "").strip()
            areas = macro.get("area") or []

            if macro_id in CITY_MACROS:
                # The macro IS the province; its areas are districts.
                province_v2 = CITY_MACROS[macro_id]
                provinces[str(province_v2)] = {
                    "region_v2": province_v2,
                    "name": macro_name,
                    "slug": slugify(macro_name),
                    "aliases": province_aliases(macro_name),
                    "macro_id": macro_id,
                    "macro_name": macro_name,
                }
                for area in areas:
                    for area_id_raw, area_name in area.items():
                        area_v2 = macro_id * 1000 + int(area_id_raw)
                        districts[str(area_v2)] = {
                            "area_v2": area_v2,
                            "name": area_name,
                            "slug": slugify(area_name),
                            "region_v2": province_v2,
                        }
                continue

            # Multi-province macro: each area is a province.
            for area in areas:
                for area_id_raw, area_name in area.items():
                    province_v2 = macro_id * 1000 + int(area_id_raw)
                    provinces[str(province_v2)] = {
                        "region_v2": province_v2,
                        "name": area_name,
                        "slug": slugify(area_name),
                        "aliases": province_aliases(area_name),
                        "macro_id": macro_id,
                        "macro_name": macro_name,
                    }
    return provinces, districts


def harvest_modern_provinces(
    provinces: Dict[str, Any], pause: float = 0.2
) -> Dict[str, str]:
    """Map each legacy ``region_v2`` to its POST-2025-merger province name.

    Vietnam reorganised its provinces in 2025. Chợ Tốt kept the old
    ``region_v2`` codes and the old ``region_name``, but ads now display the
    merged name in ``region_name_v3``. So "TP Đà Nẵng" is served by two codes
    (3016 former Quảng Nam, 3017 the city) and "TP Hồ Chí Minh" by three
    (2010 Bà Rịa Vũng Tàu, 2011 Bình Dương, 13000 HCM).

    A client that resolves a province name to ONE code therefore returns a
    partial result set and calls it complete. Only the ads state the merged
    name, so it has to be measured here rather than assumed.
    """
    modern: Dict[str, str] = {}
    for code in sorted(provinces, key=int):
        try:
            data = _get("ad-listing", region_v2=int(code), limit=20)
        except Exception as exc:  # noqa: BLE001
            print(f"  [modern] {code}: {exc}", file=sys.stderr)
            continue
        names = Counter(
            ad.get("region_name_v3")
            for ad in data.get("ads") or []
            if ad.get("region_name_v3")
        )
        if names:
            modern[code] = names.most_common(1)[0][0]
        time.sleep(pause)
    return modern


def harvest_districts(provinces: Dict[str, Any], budget: int = 60, pause: float = 0.25) -> Dict[str, Any]:
    """Discover non-HN/HCM districts from live ads.

    The regions endpoint only describes provinces for the multi-province macros,
    so their districts (7-digit area_v2 such as 1006401) exist solely in ad data.
    """
    found: Dict[str, Dict[str, Any]] = {}
    targets = [p for p in provinces.values() if p["region_v2"] not in CITY_MACROS.values()]
    targets.sort(key=lambda p: p["region_v2"])
    for index, province in enumerate(targets):
        if index >= budget:
            print(
                f"  [harvest] stopped at budget {budget}; "
                f"{len(targets) - budget} provinces not swept",
                file=sys.stderr,
            )
            break
        try:
            data = _get("ad-listing", region_v2=province["region_v2"], limit=50)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            print(f"  [harvest] {province['name']}: {exc}", file=sys.stderr)
            continue
        for ad in data.get("ads") or []:
            area_v2, area_name = ad.get("area_v2"), ad.get("area_name")
            if area_v2 and area_name:
                found[str(area_v2)] = {
                    "area_v2": int(area_v2),
                    "name": area_name,
                    "slug": slugify(area_name),
                    "region_v2": province["region_v2"],
                }
        time.sleep(pause)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "chotot" / "data"))
    parser.add_argument("--harvest-areas", action="store_true", help="also sweep live ads for non-HN/HCM districts")
    parser.add_argument("--harvest-budget", type=int, default=60)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("fetching categories ...")
    categories = fetch_categories()
    print(f"  {len(categories)} categories")

    print("fetching regions ...")
    provinces, districts = fetch_regions()
    print(f"  {len(provinces)} provinces, {len(districts)} HN/HCM districts")

    print("resolving post-2025-merger province names ...")
    modern = harvest_modern_provinces(provinces)
    groups: Dict[str, List[int]] = {}
    for code, province in provinces.items():
        modern_name = modern.get(code)
        province["modern_name"] = modern_name
        province["legacy_name"] = province["name"]
        if modern_name:
            groups.setdefault(modern_name, []).append(int(code))
    for province in provinces.values():
        name = province.get("modern_name")
        province["sibling_region_v2"] = sorted(groups.get(name, [])) if name else []
    merged = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"  {len(groups)} modern provinces from {len(provinces)} legacy codes"
          f" ({len(merged)} are merger groups)")
    unresolved = [c for c in provinces if c not in modern]
    if unresolved:
        print(f"  {len(unresolved)} codes returned no ads (kept, marked unresolved)")

    if args.harvest_areas:
        print("harvesting provincial districts from live ads ...")
        extra = harvest_districts(provinces, budget=args.harvest_budget)
        districts.update(extra)
        print(f"  +{len(extra)} provincial districts (total {len(districts)})")

    # Alias collisions would make resolution non-deterministic. Detect, do not guess.
    for province in provinces.values():
        modern_name = province.get("modern_name")
        if modern_name and modern_name != province["name"]:
            province["aliases"] = sorted(set(province["aliases"]) | set(province_aliases(modern_name)))

    # An alias is ambiguous only when it points at two DIFFERENT modern
    # provinces. Siblings of one merger group legitimately share every alias --
    # that is the whole point of the group -- so counting raw code hits here
    # would delete exactly the aliases the merge exists to provide.
    alias_owners: Dict[str, set] = {}
    for province in provinces.values():
        owner = province.get("modern_name") or province["name"]
        for alias in province["aliases"]:
            alias_owners.setdefault(alias, set()).add(owner)
    collisions = {a for a, owners in alias_owners.items() if len(owners) > 1}
    for province in provinces.values():
        province["aliases"] = [a for a in province["aliases"] if a not in collisions]
    if collisions:
        print(f"  dropped {len(collisions)} cross-province aliases: {sorted(collisions)[:8]}")

    (out_dir / "categories.json").write_text(
        json.dumps({"fetched_at": fetched_at, "source": f"{GATEWAY}/chapy-pro/categories",
                    "categories": categories}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    (out_dir / "regions.json").write_text(
        json.dumps({"fetched_at": fetched_at, "source": f"{GATEWAY}/chapy-pro/regions",
                    "provinces": provinces, "districts": districts,
                    "modern_groups": groups}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_dir}/categories.json and {out_dir}/regions.json  (fetched_at={fetched_at})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
