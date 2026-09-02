# chotot-cli - command-line client and price analyser for Chợ Tốt.
# Copyright (C) 2026 V
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# Affero General Public License <https://www.gnu.org/licenses/> for details.

#!/usr/bin/env python3
"""Mutation harness: prove the suite can fail for the right reasons.

Each mutation reintroduces a defect this project actually shipped or measured.
If the suite stays green, the corresponding test is decorative.

Bytecode caching is disabled throughout. CPython decides a ``.pyc`` is stale by
comparing mtime in WHOLE SECONDS plus size, and a mutation loop rewrites a
module several times a second, so a cached mutant can be executed during the
restore run — producing verdicts decided by the cache rather than the code.

Mutants are applied to a SCRATCH COPY of the tree, never to the working tree.
Measured 2026-09-02: a reviewer read ``chotot/http.py`` while this harness was
running in the background, saw mutant #1 (``if False: logger.warning``) in the
file, and drafted it as a shipped defect. A harness that edits the tree in
place also leaves a mutant behind if it is killed mid-run — and the commit that
follows sweeps it in. Copying costs a second; both hazards go away.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, NamedTuple

ROOT = Path(__file__).resolve().parent.parent

#: Never copied into the scratch tree: build products and caches would only
#: slow the copy, and a nested ``.git`` would make the copy look like a repo.
SCRATCH_IGNORE = shutil.ignore_patterns(
    ".git", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache",
    "graphify-out", ".mypy_cache", ".ruff_cache",
)


class Mutation(NamedTuple):
    name: str
    path: str
    before: str
    after: str
    why: str


MUTATIONS: List[Mutation] = [
    Mutation(
        "empty TLS trust store fails silently again",
        "chotot/http.py",
        '                logger.warning(\n'
        '                    "This interpreter has an empty CA trust store and certifi is "',
        '                if False: logger.warning(\n'
        '                    "This interpreter has an empty CA trust store and certifi is "',
        "every request then fails with a bare SSL error and nothing names the cause",
    ),
    Mutation(
        "crawl budget counts fetched rows instead of surviving ones",
        "chotot/client.py",
        "            return sum(1 for x in buckets[target] if survives(x))",
        "            return len(buckets[target])",
        "a filter that discards most rows then starves the result: 50 fetched, "
        "49 dropped, 1 returned of the 10 asked for",
    ),
    Mutation(
        "match-all stops filtering on query terms",
        "chotot/client.py",
        "                and (not match_all or covers_all_terms(listing, terms))",
        "                and True",
        "--match-all silently returns the unioned pool it was asked to intersect",
    ),
    Mutation(
        "query coverage guard goes silent",
        "chotot/client.py",
        "        if len(terms) > 1 and ordered:",
        "        if False:",
        "the gateway unions query terms; without the warning an analyse over "
        "unrelated ads reads as a price for the product asked about",
    ),
    Mutation(
        "coverage guard treats a partial match as full",
        "chotot/client.py",
        "            matched = sum(1 for x in ordered if covers_all_terms(x, terms))",
        "            matched = len(ordered)",
        "every row is claimed to carry every term, so the guard never fires",
    ),
    Mutation(
        "single-character terms drive the coverage guard",
        "chotot/client.py",
        "        if len(term) >= MIN_QUERY_TERM_LEN and term not in seen:",
        "        if term not in seen:",
        "one-character terms match nearly every ad and make the guard noise",
    ),
    Mutation(
        "price filter reverts to the ignored sp/ep spelling",
        "chotot/client.py",
        'return f"{low}-{int(max_price)}" if max_price is not None else f"{low}-"',
        'return None',
        "the gateway ignores sp/ep and returns an unfiltered page as filtered",
    ),
    Mutation(
        "pagination stops deduplicating",
        "chotot/client.py",
        """                if listing.list_id in seen:
                    coverage.duplicates_dropped += 1
                    continue""",
        """                if False:
                    coverage.duplicates_dropped += 1
                    continue""",
        "overlapping offset windows repeat ~8% of rows",
    ),
    Mutation(
        "capped total reported as an exact count",
        "chotot/client.py",
        "coverage.total_is_capped = any(v >= contract.TOTAL_CAP for v in stated)",
        "coverage.total_is_capped = False",
        "total saturates at 10000; reporting it as a count overstates the market",
    ),
    Mutation(
        "condition read from the always-empty params list",
        "chotot/models.py",
        'condition_code=_opt_int(ad.get("elt_condition")),',
        'condition_code=next((p.get("value") for p in (ad.get("params") or [])'
        ' if p.get("id") == "elt_condition"), None),',
        "search results always return params=[], so every condition becomes None",
    ),
    Mutation(
        "absent seller counts become a confident zero",
        "chotot/models.py",
        'sold_ads=_opt_int(info.get("sold_ads") if info.get("sold_ads") is not None else ad.get("sold_ads")),',
        'sold_ads=int(info.get("sold_ads") or ad.get("sold_ads") or 0),',
        "an unknown sold-count would read as '0 sold'",
    ),
    Mutation(
        "unpriced listings treated as zero when sorting",
        "chotot/client.py",
        "priced = [x for x in listings if x.has_price]",
        "priced = list(listings)",
        "'cheapest first' would open with every negotiable ad",
    ),
    Mutation(
        "percentiles computed on a tiny sample",
        "chotot/analyzer.py",
        "MIN_SAMPLE_FOR_PERCENTILES = 8",
        "MIN_SAMPLE_FOR_PERCENTILES = 1",
        "a P25 over four listings is arithmetic, not information",
    ),
    Mutation(
        "unpriced listings counted as zero in statistics",
        "chotot/analyzer.py",
        "priced = [x for x in listings if x.has_price]",
        "priced = list(listings)",
        "negotiable ads would drag every average toward zero",
    ),
    Mutation(
        "CSV formula injection left unescaped",
        "chotot/formatter.py",
        '    if text and text[0] in _FORMULA_PREFIXES:\n        return "\'" + text',
        '    if False:\n        return "\'" + text',
        "listing titles are attacker-controlled and exports open in Excel",
    ),
    Mutation(
        "CSV loses its BOM",
        "chotot/formatter.py",
        'return (CSV_BOM if bom else "") + buffer.getvalue()',
        "return buffer.getvalue()",
        "Excel mojibakes Vietnamese without it",
    ),
    Mutation(
        "province resolution stops expanding merged provinces",
        "chotot/taxonomy.py",
        "return sorted(siblings) if siblings else [resolved]",
        "return [resolved]",
        "HCM would silently drop Bình Dương and Bà Rịa - Vũng Tàu",
    ),
    Mutation(
        "ignored parameters are forwarded again",
        "chotot/contract.py",
        "if name in IGNORED_PARAMS or name in ERRORING_PARAMS:",
        "if False:",
        "the guard is what stops a filtered-looking unfiltered answer",
    ),
    Mutation(
        "shop phone numbers revealed by default",
        "chotot/seller.py",
        "phones = self.phones if reveal_contact else [_redact_phone(p) for p in self.phones]",
        "phones = self.phones",
        "the listing API masks the same number",
    ),
    Mutation(
        "unverified facets offered as filters",
        "chotot/facets.py",
        "return {name: spec for name, spec in entry.items() if _works(spec)}",
        "return dict(entry)",
        "the gateway accepts and ignores several declared parameters",
    ),
    Mutation(
        "storefront reports the lookup key as an account_oid",
        "chotot/seller.py",
        "account_oid = raw_oid if (raw_oid and len(raw_oid) >= 24 and not raw_oid.isdigit()) else None",
        "account_oid = raw_oid",
        "theia echoes the key back, so a numeric lookup would report an id as an OID",
    ),
    Mutation(
        "crawl no longer stops when a page adds nothing new",
        "chotot/client.py",
        "            if fresh == 0:",
        "            if False:",
        "an overlapping feed would burn the whole request budget",
    ),
    Mutation(
        "listing type stops being pushed server-side",
        "chotot/client.py",
        '        if st_code is not None:\n            base["st"] = st_code',
        '        if False:\n            base["st"] = st_code',
        "a property search would mix ~55% rentals with sales",
    ),
    Mutation(
        "mixed sale/rent samples no longer warned about",
        "chotot/analyzer.py",
        "        if len(surviving) > 1:",
        "        if False:",
        "a median averaging a monthly rent against a purchase price reads as a fact",
    ),
    Mutation(
        "merged-province result truncated positionally",
        "chotot/client.py",
        "                target = min(",
        "                target = available[0]  # noqa\n                _unused = min(",
        "codes sort ascending, so HCM proper is last and gets dropped entirely",
    ),
    Mutation(
        "totals accumulated per request instead of per region",
        "chotot/client.py",
        "        stated = [v for v in region_totals.values() if v is not None]",
        "        stated = [v for v in region_totals.values() if v is not None] * 2",
        "each region's total counted once per page reported 11,071 as 22,142",
    ),
    Mutation(
        "exhaustion becomes an OR across regions",
        "chotot/client.py",
        "        exhausted = all(region_exhausted[t] for t in targets)",
        "        exhausted = any(region_exhausted[t] for t in targets)",
        "one tiny region running dry suppressed the under-delivery warning",
    ),
    Mutation(
        "phone redaction keeps a trailing suffix again",
        "chotot/seller.py",
        'return f"{text[:_PHONE_PREFIX_KEPT]}{\'*\' * (len(text) - _PHONE_PREFIX_KEPT)}"',
        'return f"{text[:4]}{\'*\' * (len(text) - 7)}{text[-3:]}"',
        "7 of 10 digits shown under a phones_redacted:true flag",
    ),
    Mutation(
        "seller id interpolated into the URL unvalidated",
        "chotot/seller.py",
        "        if not _SELLER_KEY.match(key):",
        "        if False:",
        "a path segment that can add query parameters or reach another endpoint",
    ),
    Mutation(
        "theia listing-type vocabulary left un-normalised",
        "chotot/seller.py",
        '        normalised["type"] = _STOREFRONT_TYPES.get(raw_type.lower(), raw_type)',
        '        normalised["type"] = raw_type',
        "theia says sell/let, so the mixed sale/rent guard never fires on a storefront",
    ),
    Mutation(
        "table width counts ANSI escapes as visible columns",
        "chotot/formatter.py",
        "    plain = _ANSI_RE.sub(\"\", text)",
        "    plain = text",
        "coloured rows padded 13 columns short and the borders stopped lining up",
    ),
    Mutation(
        "MCP privacy switch coerced with bool()",
        "chotot/mcp_server.py",
        '    return args.get(name) is True',
        '    return bool(args.get(name))',
        'reveal_contact:"false" is truthy and prints unmasked phone numbers',
    ),
    Mutation(
        "offset derived from surviving rows instead of pages requested",
        "chotot/client.py",
        "                offset = pages[target] * contract.MAX_PAGE_SIZE",
        "                offset = (len(buckets[target]) // contract.MAX_PAGE_SIZE + 1) * contract.MAX_PAGE_SIZE",
        "skipped o=50 entirely and re-requested o=150, capping every search at ~146 rows",
    ),
    Mutation(
        "crawl quota counts rows the filter will discard",
        "chotot/client.py",
        "            return any(not region_exhausted[t] and surviving(t) < quota(t) for t in targets)",
        "            return any(not region_exhausted[t] and len(buckets[t]) < quota(t) for t in targets)",
        "'--condition new --limit 20' returned 1 result with 22 requests unspent",
    ),
    Mutation(
        "capped totals weighted as comparable magnitudes",
        "chotot/client.py",
        "            t: (uncapped_max + contract.TOTAL_CAP if t in capped else v)",
        "            t: v",
        "two regions at the 10,000 cap tie, and the city loses the tie on code order",
    ),
    Mutation(
        "epoch seconds misread as milliseconds",
        "chotot/models.py",
        "    seconds = value / 1000 if value >= _EPOCH_MILLIS_THRESHOLD else float(value)",
        "    seconds = value / 1000",
        "every shop listing was dated 1970-01-21",
    ),
    Mutation(
        "seller --limit 0 read as 'unset'",
        "chotot/seller.py",
        "        if limit is not None and limit < 1:",
        "        if False:",
        "an explicit 0 fell through to `limit or total` and fetched everything",
    ),
    Mutation(
        "exports drop the sale/rent distinction",
        "chotot/formatter.py",
        '    "listing_type", "listing_type_label",',
        "",
        "a property export mixes ₫3B sale prices with ₫10M monthly rents",
    ),
    Mutation(
        "price_check of 0 silently dropped",
        "chotot/cli.py",
        "    verdict = report.evaluate(args.price_check) if args.price_check is not None else None",
        "    verdict = report.evaluate(args.price_check) if args.price_check else None",
        "the analyser's non-positive-price message became unreachable",
    ),
    Mutation(
        "gzip decompression removed from the transport",
        "chotot/http.py",
        '            if "gzip" in header:\n                raw = gzip.decompress(raw)',
        '            if False:\n                raw = gzip.decompress(raw)',
        "the client requests gzip, so every response would fail to parse",
    ),
    Mutation(
        "BOM-tolerant decode replaced with plain utf-8",
        "chotot/http.py",
        'return raw.decode("utf-8-sig")',
        'return raw.decode("utf-8")',
        "a BOM welds onto the first key and makes exactly that field unreadable",
    ),
    Mutation(
        "404 becomes retryable",
        "chotot/http.py",
        "                if exc.code == 404:",
        "                if False:",
        "a missing listing would be retried and then reported as a transport failure",
    ),
    Mutation(
        "Retry-After ignored in favour of guessed backoff",
        "chotot/http.py",
        "        if retry_after is not None:\n            return min(retry_after, MAX_BACKOFF)",
        "        if False:\n            return min(retry_after, MAX_BACKOFF)",
        "the server states the wait; probing for it is slower and wrong",
    ),
    Mutation(
        "ambiguous facet label resolved to the first match",
        "chotot/facets.py",
        "        if len(candidates) > 1:",
        "        if False:",
        "'> 256 GB' folds to '256 GB'; picking the first silently filtered on the wrong code",
    ),
    Mutation(
        "exact facet label no longer outranks the code table",
        "chotot/facets.py",
        '        ("exact label", lambda label: label.strip().casefold() == text.casefold()),\n'
        '        ("code", None),',
        '        ("code", None),\n'
        '        ("exact label", lambda label: label.strip().casefold() == text.casefold()),',
        "carseats=4 would mean code 4 ('32 seats'), not four seats",
    ),
    Mutation(
        "district fan-out no longer narrowed to its own region",
        "chotot/client.py",
        "            if owner is not None:\n                region_codes = [owner]",
        "            if False:\n                region_codes = [owner]",
        "the gateway ANDs region and area, so sibling requests can only return zero",
    ),
    Mutation(
        "round-one order ignores the code the user's input matched",
        "chotot/client.py",
        "            key=lambda t: (0 if t == matched else 1, 0 if (t and t % 1000 == 0) else 1, t or 0),",
        "            key=lambda t: (t or 0),",
        "a small budget went entirely to the annexed province in 22 merger groups",
    ),
    Mutation(
        "storefront relative ages no longer parsed",
        "chotot/seller.py",
        "    if listing.posted_at is None and listing.posted_label:",
        "    if False:",
        "every seller listing showed an untranslated Vietnamese string and a null timestamp",
    ),
    Mutation(
        "MCP price_check reverts to truthiness",
        "chotot/mcp_server.py",
        "        raw_check = args.get(\"price_check\")\n        if raw_check is not None:",
        "        raw_check = args.get(\"price_check\")\n        if raw_check:",
        "an explicit 0 was dropped with no error and no price_check field",
    ),
    Mutation(
        "listing-type read-back trusts the request instead of the rows",
        "chotot/client.py",
        "        if requested is not None:\n            unexpected = {t: n for t, n in counts.items() if t != requested}",
        "        if False:\n            unexpected = {t: n for t, n in counts.items() if t != requested}",
        "if `st` stopped filtering, unfiltered property results would be reported as filtered",
    ),
    Mutation(
        "mixed-type warning computed over the pre-trim set again",
        "chotot/analyzer.py",
        "        surviving = {t for t in kept_types if t in (\"s\", \"u\")}",
        "        surviving = {t for t in sampled_types if t in (\"s\", \"u\")}",
        "the warning claimed the figures average a rent against a purchase price "
        "in runs where the fence had removed every sale ad",
    ),
    Mutation(
        "budget-caused floor blamed on the upstream cap",
        "chotot/client.py",
        '                coverage.total_floor_reason = "regions_skipped"',
        '                pass',
        "a 1,165 floor was labelled '(upstream cap)' when the cap is 10,000",
    ),
    Mutation(
        "a non-object ad becomes an internal error again",
        "chotot/models.py",
        "        if not isinstance(ad, dict):",
        "        if False:",
        "a changed upstream shape reported as 'unexpected error ... please report this'",
    ),
    Mutation(
        "weighted draw serves every region before any twice",
        "chotot/client.py",
        "                    key=lambda t: ((drawn[t] + 1) / share_of[t] if share_of[t]",
        "                    key=lambda t: ((drawn[t]) / share_of[t] if share_of[t]",
        "--limit 3 returned a third of its rows from a city that is 90% of the province",
    ),
    Mutation(
        "condition filter guesses unstated ads into a bucket",
        "chotot/client.py",
        "return listing.condition_code in wanted",
        "return listing.condition_code in wanted or listing.condition_code is None",
        "an unstated condition is not evidence for 'new'",
    ),
    # -- the residential-proxy fallback (2.2.0) ---------------------------------
    Mutation(
        'auto-proxy resolves the residential proxy before the first request again',
        'chotot/http.py',
        '                else resolve_proxy(proxy, geo=self.geo, resolver=self._resolver))',
        '                else resolve_proxy("auto" if self.auto_proxy else proxy, geo=self.geo, resolver=self._resolver))',
        'every request is paid from the first one and the fallback branch is dead (the 2.1.0 defect)',
    ),
    Mutation(
        '403 no longer triggers the fallback',
        'chotot/http.py',
        'PROXY_FALLBACK_STATUSES: FrozenSet[int] = frozenset({403, 429})',
        'PROXY_FALLBACK_STATUSES: FrozenSet[int] = frozenset({429})',
        'the one status an anti-bot block actually returns never switches (the 2.1.0 defect)',
    ),
    Mutation(
        'the fallback is resolved on every attempt',
        'chotot/http.py',
        '        self._fallback_attempted = True\n        url = self._resolver(self.geo)',
        '        url = self._resolver(self.geo)',
        'a resolver that had nothing is re-run and re-warned on every retry',
    ),
    Mutation(
        'the switch to a paid proxy is not announced',
        'chotot/http.py',
        '        logger.warning(\n            "%s on the direct connection; switching to the residential proxy',
        '        logger.debug(\n            "%s on the direct connection; switching to the residential proxy',
        'money is spent with nothing on stderr saying so',
    ),
    Mutation(
        'a user-named proxy is replaced by the paid one',
        'chotot/http.py',
        '        if not self.auto_proxy or self._proxy_url or self._fallback_attempted:',
        '        if not self.auto_proxy or self._fallback_attempted:',
        "the user's own proxy gets swapped for a residential one behind their back",
    ),
    Mutation(
        'a switch on the final attempt gets no proxied try',
        'chotot/http.py',
        '                budget += 1\n            if attempt < budget:',
        '                pass\n            if attempt < budget:',
        '--retries 1 --auto-proxy resolves a proxy and never uses it',
    ),
    Mutation(
        'the proxy mask keeps the login again',
        'chotot/proxy.py',
        '        return f"{parts.scheme or \'proxy\'}://***:***@{hostport}"',
        '        return f"{parts.scheme or \'proxy\'}://{parts.netloc.split(\':\', 1)[0][:4]}***:***@{hostport}"',
        'four characters of a residential login reach every log line and doctor report (the 2.1.0 defect)',
    ),
    Mutation(
        'SOCKS refusal loses its named cause',
        'chotot/proxy.py',
        '    if scheme in SOCKS_SCHEMES:',
        '    if False:',
        "a socks5:// URL is refused as 'not an http URL' and the user does not learn why",
    ),
    Mutation(
        'explicit --proxy auto degrades silently to direct',
        'chotot/proxy.py',
        '            if not url:\n                raise UsageError(\n                    f"--proxy auto could not resolve',
        '            if not url:\n                return ProxyPlan(None, "direct")\n            if False:\n                raise UsageError(\n                    f"--proxy auto could not resolve',
        'the user asked for a proxy, paid nothing, and got their own address blocked (the 2.1.0 defect)',
    ),
    Mutation(
        'CHOTOT_PROXY=none no longer outranks HTTPS_PROXY',
        'chotot/proxy.py',
        '        if value.strip().lower() in DIRECT_WORDS:\n            return None, name',
        '        if value.strip().lower() in DIRECT_WORDS:\n            continue',
        'there is no way to escape a global proxy for one tool',
    ),
]


def run_suite(tree: Path) -> bool:
    """Grade ``tree`` -- always the scratch copy, never ROOT."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(tree))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-x", "-q", "-m", "not slow",
         "-p", "no:cacheprovider", "tests/"],
        cwd=tree, capture_output=True, text=True, env=env, timeout=600,
    )
    return result.returncode == 0


def main() -> int:
    # Line-buffered even when redirected to a file: a 20-minute run whose
    # progress appears only at exit looks stuck from the outside.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    with tempfile.TemporaryDirectory(prefix="chotot-mutate-") as scratch:
        tree = Path(scratch) / "tree"
        shutil.copytree(ROOT, tree, ignore=SCRATCH_IGNORE, symlinks=True)
        print(f"grading a scratch copy at {tree}; the working tree is not touched")
        return _grade(tree)


def _grade(tree: Path) -> int:
    print("baseline ...", end=" ", flush=True)
    if not run_suite(tree):
        print("RED — fix the suite before mutating")
        return 1
    print("green")

    caught, escaped, skipped = [], [], []
    for index, mutation in enumerate(MUTATIONS, 1):
        target = tree / mutation.path
        original = target.read_text(encoding="utf-8")
        if mutation.before not in original:
            skipped.append(mutation)
            print(f"[{index:2d}/{len(MUTATIONS)}] SKIP    {mutation.name} (pattern not found)")
            continue
        target.write_text(original.replace(mutation.before, mutation.after, 1), encoding="utf-8")
        try:
            survived = run_suite(tree)
        finally:
            target.write_text(original, encoding="utf-8")
        if survived:
            escaped.append(mutation)
            print(f"[{index:2d}/{len(MUTATIONS)}] ESCAPED {mutation.name}")
            print(f"                 -> {mutation.why}")
        else:
            caught.append(mutation)
            print(f"[{index:2d}/{len(MUTATIONS)}] caught  {mutation.name}")

    print("\nre-checking baseline after restore ...", end=" ", flush=True)
    print("green" if run_suite(tree) else "RED — a restore failed")

    total = len(MUTATIONS)
    print(f"\ngraded {total} mutants: {len(caught)} caught, "
          f"{len(escaped)} escaped, {len(skipped)} skipped (pattern not found)")
    if skipped:
        print("  A skipped mutant means the harness is stale, not that the code is safe:")
        for mutation in skipped:
            print(f"    - {mutation.name} ({mutation.path})")
    return 1 if escaped or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
