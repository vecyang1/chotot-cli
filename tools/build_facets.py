# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

#!/usr/bin/env python3
"""Generate ``chotot/data/facets.json`` — category-specific search filters.

Two sources are combined, because neither alone is sufficient:

1. ``GET /chapy-pro/ad-params?cg=<code>`` states each category's parameters with
   their Vietnamese labels and option codes. This is the *posting form* schema —
   it says what a field means, not whether you can search by it.
2. A differential probe against ``GET /ad-listing`` says whether the gateway
   actually filters on that parameter. This matters because the gateway answers
   HTTP 200 for parameters it ignores: ``elt_warranty``, ``elt_cellular_type``
   and ``min_salary`` are all declared by (1) and all silently ignored by search.

A facet is recorded as ``works`` only when a probe with a real option code
collapses the returned distribution to that code. Year-like parameters
(``mfdate``, ``regdate``) are ignored as scalars but honoured as ``MIN-MAX``
ranges, exactly like ``price``, so they are probed and recorded as ranges.

    python3 tools/build_facets.py [--categories 5010,2010] [--no-probe]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GATEWAY = "https://gateway.chotot.com/v1/public"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

#: Parameters whose value is a MIN-MAX range rather than a single code. Probing
#: these as scalars reports them as ignored, which is true and useless.
RANGE_PARAMS = {
    "mfdate", "regdate", "size", "min_salary", "max_salary", "price",
    "mileage_v2", "rooms", "toilets", "floors", "size_used", "land_size",
}

#: Never emitted as a searchable facet even when the form declares them: these
#: are posting-form fields with no search counterpart, or already first-class.
SKIP_PARAMS = {"price", "subject", "body", "images", "region", "area", "ward",
               "address", "latitude", "longitude", "phone"}


def _get(path: str, **params: Any) -> Dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{GATEWAY}/{path.lstrip('/')}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def extract_definitions(category: int) -> Dict[str, Dict[str, Any]]:
    """Pull parameter definitions (label + option codes) for one category."""
    try:
        payload = _get("chapy-pro/ad-params", cg=category)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{category}] ad-params failed: {exc}", file=sys.stderr)
        return {}

    found: Dict[str, Dict[str, Any]] = {}
    for section in payload.get("ad_params") or []:
        if not isinstance(section, dict):
            continue
        for listing_type, body in section.items():
            if not isinstance(body, dict):
                continue
            for entry in body.get("params") or []:
                if not isinstance(entry, dict):
                    continue
                for name, spec in entry.items():
                    if name in SKIP_PARAMS or not isinstance(spec, dict):
                        continue
                    options: Dict[str, str] = {}
                    for option in spec.get("options") or []:
                        if isinstance(option, dict):
                            for code, label in option.items():
                                options[str(code)] = str(label)
                    record = found.setdefault(name, {
                        "param": name,
                        "label": spec.get("label") or name,
                        "type": spec.get("type") or "integer",
                        "options": {},
                        "listing_types": [],
                    })
                    record["options"].update(options)
                    if listing_type not in record["listing_types"]:
                        record["listing_types"].append(listing_type)
    return found


def _distribution(category: int, param: Optional[str], value: Any) -> Tuple[int, Optional[int], Counter]:
    params: Dict[str, Any] = {"cg": category, "limit": 50}
    if param:
        params[param] = value
    payload = _get("ad-listing", **params)
    ads = payload.get("ads") or []
    key = param if param else "list_id"
    return len(ads), payload.get("total"), Counter(str(ad.get(key)) for ad in ads)


def probe(category: int, name: str, spec: Dict[str, Any],
          baseline_total: Optional[int], pause: float) -> Dict[str, Any]:
    """Decide whether the gateway actually filters on ``name``.

    Verdict rule: the parameter works only if the returned rows collapse to the
    requested value. A same-as-baseline total with a mixed distribution is the
    signature of silent acceptance.
    """
    options = spec.get("options") or {}
    is_range = name in RANGE_PARAMS

    if is_range:
        # Discover a plausible range from live data rather than guessing years.
        try:
            _, _, seen = _distribution(category, name, None)
        except Exception:  # noqa: BLE001
            return {"works": False, "reason": "probe failed"}
        numeric = sorted(int(v) for v in seen if v.isdigit())
        if not numeric:
            return {"works": False, "reason": "no numeric values observed"}
        low, high = numeric[0], numeric[min(len(numeric) - 1, 1)]
        value = f"{low}-{high}"
    else:
        codes = [c for c in options if c.isdigit()]
        if not codes:
            return {"works": False, "reason": "free-text field, not a searchable facet"}
        value = codes[len(codes) // 2]

    time.sleep(pause)
    try:
        count, total, seen = _distribution(category, name, value)
    except Exception as exc:  # noqa: BLE001
        return {"works": False, "reason": f"probe error: {exc}"}

    if not count:
        return {"works": False, "reason": "probe returned no rows", "probed": value}

    if is_range:
        low, high = (int(x) for x in str(value).split("-"))
        numeric = [int(v) for v in seen.elements() if str(v).isdigit()]
        inside = all(low <= v <= high for v in numeric) if numeric else False
        return {"works": bool(inside), "range": True, "probed": value,
                "reason": "range honoured" if inside else "values outside the range",
                "evidence": f"n={count} total={total}"}

    collapsed = seen.get(str(value), 0) == count

    # Collapse alone is not evidence. When a category's page is already almost
    # entirely one value, "all rows match" is true whether or not the parameter
    # did anything -- a check that cannot fail. So require the parameter to have
    # CHANGED something too: the total must move, or the returned ids must
    # differ from the unfiltered page.
    changed_total = (total is not None and baseline_total is not None
                     and total != baseline_total)
    changed_rows = False
    if collapsed and not changed_total:
        try:
            time.sleep(pause)
            _, _, baseline_ids = _distribution(category, None, None)
            _, _, probed_ids = _distribution(category, name, value)
            changed_rows = set(baseline_ids) != set(probed_ids)
        except Exception:  # noqa: BLE001 - treat an unmeasurable probe as not proven
            changed_rows = False

    works = bool(collapsed and (changed_total or changed_rows))
    if works:
        reason = ("distribution collapsed and the total changed" if changed_total
                  else "distribution collapsed and the result set changed")
    elif collapsed:
        reason = ("page was already homogeneous - collapse proves nothing and "
                  "nothing changed, so treated as ignored")
    else:
        reason = "accepted and ignored (mixed distribution)"
    return {
        "works": works,
        "probed": value,
        "reason": reason,
        "evidence": f"n={count} total={total} baseline_total={baseline_total} "
                    f"matched={seen.get(str(value), 0)}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "chotot" / "data"))
    parser.add_argument("--categories", help="Comma-separated category codes (default: all leaf categories).")
    parser.add_argument("--no-probe", action="store_true", help="Record definitions without verifying them.")
    parser.add_argument("--pause", type=float, default=0.25)
    args = parser.parse_args()

    out_dir = Path(args.out)
    taxonomy = json.loads((out_dir / "categories.json").read_text(encoding="utf-8"))["categories"]
    if args.categories:
        targets = [int(c) for c in args.categories.split(",")]
    else:
        # Leaf categories carry the facets; parents inherit nothing useful.
        targets = sorted(int(c) for c, e in taxonomy.items() if e.get("parent") is not None)

    result: Dict[str, Any] = {}
    working = ignored = 0
    for index, category in enumerate(targets, 1):
        definitions = extract_definitions(category)
        if not definitions:
            continue
        entry: Dict[str, Any] = {}
        baseline_total = None
        if not args.no_probe:
            try:
                _, baseline_total, _ = _distribution(category, None, None)
            except Exception:  # noqa: BLE001
                baseline_total = None
            time.sleep(args.pause)

        for name, spec in sorted(definitions.items()):
            record = dict(spec)
            if args.no_probe:
                record["verified"] = None
            else:
                verdict = probe(category, name, spec, baseline_total, args.pause)
                record["verified"] = verdict
                record["is_range"] = bool(verdict.get("range"))
                working += bool(verdict.get("works"))
                ignored += not verdict.get("works")
                time.sleep(args.pause)
            entry[name] = record
        result[str(category)] = entry
        print(f"  [{index}/{len(targets)}] cg={category}: {len(entry)} params", file=sys.stderr)

    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"{GATEWAY}/chapy-pro/ad-params + differential probe of /ad-listing",
        "probed": not args.no_probe,
        "facets": result,
    }
    (out_dir / "facets.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out_dir}/facets.json — {len(result)} categories, "
          f"{working} verified working, {ignored} not usable as filters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
