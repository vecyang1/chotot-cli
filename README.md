# chotot-cli

A command-line client, market-price analyser and MCP server for
[Chợ Tốt](https://www.chotot.com) — Vietnam's largest classifieds marketplace.

Search listings, inspect a seller, and get an asking-price distribution for any
product, from a terminal or from an AI agent.

```console
$ chotot search "iphone 13" --region hcm --min-price 2000000 --max-price 8000000 --sort price_asc --limit 4
┌───┬────────┬────────────────────────────┬──────────────────┬────────┬─────────┬───────────┐
│ # │ Price  │ Title                      │ Location         │ Cond   │ Age     │ ID        │
├───┼────────┼────────────────────────────┼──────────────────┼────────┼─────────┼───────────┤
│ 1 │ 2.8M đ │ iphone 13 wifi             │ TP Hồ Chí Minh   │ Used   │ 6h ago  │ 134384301 │
│ 2 │ 2.8M đ │ Apple iPhone 13 Pro Max 1… │ TP Hồ Chí Minh   │ Used   │ 23d ago │ 133356210 │
│ 3 │ 3.2M đ │ iPhone 13 Pro Max 256GB X… │ Quận 12, TP Hồ … │ Used   │ 2d ago  │ 133801879 │
│ 4 │ 4.0M đ │ Apple iPhone 13 128GB      │ Quận Gò Vấp, TP… │ Used   │ 5h ago  │ 134381902 │
└───┴────────┴────────────────────────────┴──────────────────┴────────┴─────────┴───────────┘
```

The `--min-price` is doing real work there. Chợ Tốt matches keywords against the
whole ad, so "iphone 13" also finds ₫5,000 phone cases and ₫150,000 screen
protectors — and with `--sort price_asc` those come first. A floor is usually
what you want when sorting by price.


No API key. No account. No runtime dependencies.

---

## Install

```bash
pipx install chotot-cli
```

If `chotot` is then not found, `pipx` has not added its bin directory to your
`PATH` — run `pipx ensurepath` and open a new shell.

Or with pip:

```bash
pip install chotot-cli
```

Python 3.9 or newer. The client is built on the standard library alone, so
nothing else is pulled in.

---

## What it does

| Command | Purpose |
|---|---|
| `chotot search "iphone 13"` | Find listings by keyword, category, province, district, price, condition, seller type |
| `chotot detail <id>` | One listing in full: specs, location, seller, images, active status |
| `chotot analyze "iphone 13"` | Asking-price distribution — median, quartiles, per-condition, histogram |
| `chotot seller <account_id>` | A seller's complete live inventory and price summary |
| `chotot shop <alias>` | A professional shop's profile |
| `chotot facets <category>` | The category-specific filters a category supports |
| `chotot categories` / `chotot regions` | Browse the taxonomy and its codes |
| `chotot export "ipad" -o out.csv` | Write results to CSV, JSON or Markdown |
| `chotot doctor` | Re-measure the upstream API contract and report health |
| `chotot mcp` | Serve the Model Context Protocol over stdio |

### Find what something is worth

```console
$ chotot analyze "iphone 13 128gb" --category phone --samples 150 --price-check 6000000

Asking-price analysis — iphone 13 128gb
────────────────────────────────────────────────────────────────────────
Sample: 150 listings analysed, 3 outliers removed
Statistics below are computed over 147 priced listings.

  Minimum              2.700.000 đ
  P25                  5.800.000 đ
  Median               6.800.000 đ
  P75                  7.925.000 đ
  Maximum              10.500.000 đ

Typical asking range  5.800.000 đ — 7.925.000 đ

By condition
  Đã sử dụng (Used)                    n=133  median 7M đ
  Cũ / cần sửa (Used - needs repair)   n=13   median 6M đ

Price check — 6.000.000 đ
  Below the typical asking range
  In the lowest quartile of comparable asking prices.
  27% of the sampled listings ask less than this; 73% ask this much or more.
```

(Abridged — the full dashboard also prints P10/P90, mean, trimmed mean, standard
deviation and a histogram.)

### Category-specific filters

Phones filter by brand, capacity and colour; cars by make, gearbox, fuel and
year. Ask a category what it supports:

```console
$ chotot facets 5010
┌─────────────────┬────────────┬────────┬────────────────────────────────────────┐
│ Facet           │ Label      │ Values │ Examples                               │
├─────────────────┼────────────┼────────┼────────────────────────────────────────┤
│ mobile_brand    │ Hãng       │ 64     │ Alcatel, Apple, Aquos, Arbutus, Asanzo…│
│ mobile_capacity │ Dung lượng │ 8      │ < 8GB, 8 GB, 16 GB, 32 GB, 64 GB …     │
└─────────────────┴────────────┴────────┴────────────────────────────────────────┘

$ chotot search "iphone" --category phone --facet mobile_brand=apple --facet mobile_capacity="256 GB"
```

Values accept the Vietnamese label, an accent-free version of it, or the raw
code — `apple`, `Apple` and `1` all work.

### Use it from an AI agent

`chotot mcp` speaks the Model Context Protocol over stdio. Register it with any
MCP client:

```json
{
  "mcpServers": {
    "chotot": { "command": "chotot", "args": ["mcp"] }
  }
}
```

Eight tools are exposed: `chotot_search`, `chotot_get_listing`,
`chotot_analyze_prices`, `chotot_seller_listings`, `chotot_shop_profile`,
`chotot_list_facets`, `chotot_list_categories`, `chotot_list_regions`.

---

## Things worth knowing before you trust a number

Chợ Tốt's public gateway answers `HTTP 200` for several parameters it then
ignores, so a naive client returns unfiltered results and presents them as
filtered. This tool refuses to do that. What that means in practice:

- **Prices are asking prices, not sold prices.** Chợ Tốt does not publish what
  anything sold for. Every analysis says so.
- **Search terms are UNIONed, not intersected — adding a word widens the
  results.** Measured 2026-08-28: `canon`=281 ads, `v1`=39 (Honda Winner V1
  motorbikes), `canon powershot`=1, and `canon powershot v1`=**40**, which is
  exactly 39+1. The single relevant ad ranked 23rd. Before this was found,
  `analyze "canon powershot v1" --region "da nang"` reported a median of
  ₫17,500,000 — a confident, plausible figure describing motorbikes, for a
  camera with **zero** listings in that city. Every search now checks the text
  of the rows it got back and says so when they do not all carry every term;
  `--match-all` keeps only those that do.
- **Property and vehicle categories mix sale and rental ads.** A property browse
  is roughly 55% rentals, so an unfiltered median averages a monthly rent
  against a purchase price — ₫6.5M instead of ₫2.52 billion for apartments. Use
  `--listing-type sale` or `--listing-type rent`; both search and analyze warn
  when a result set turns out mixed.
- **Match counts saturate at 10,000.** Above that the API states the cap, not a
  count, and results are reported as `≥10,000` rather than as a total.
- **Condition, seller type and sort order are applied locally**, because the
  gateway ignores all three. Results are sorted over the pool actually fetched,
  and say so.
- **Results are deduplicated.** Adjacent pages overlap by roughly 8% (an
  Elasticsearch shard-boundary artefact, not time drift), so an undeduplicated
  crawl over-counts and skews every statistic built on it.
- **Provinces merged in 2025 and Chợ Tốt kept the old codes.** `TP Hồ Chí Minh`
  is served by three of them, so searching only `13000` silently drops every
  listing in Bình Dương and Bà Rịa – Vũng Tàu. `--region hcm` queries all three
  and draws from each **in proportion to its real size**, so a result for the
  city is mostly the city rather than mostly the annexed provinces.
- **Phone numbers are never fully shown.** The listing API masks them and this
  tool keeps them masked; the shop endpoint returns them unmasked and this tool
  redacts to a carrier prefix unless you pass `--show-contact`.

Run `chotot doctor` to re-measure every one of these claims against the live API:

```console
$ chotot doctor
│ PASS │ price range filter         │ 50 results, max 3,900,000 <= 5,000,000     │
│ PASS │ sp/ep still ignored        │ 32/50 results ignored the bound            │
│ PASS │ total cap                  │ broad=10,000 (cap), narrow=3,088 (real)    │
│ PASS │ listing type filter (st)   │ unfiltered {u:38, s:12} vs st=s {s:50}     │
│ PASS │ merged provinces expand    │ HCM = [2010, 2011, 13000], all populated   │
│ PASS │ listing phone stays masked │ masked as 0399****                         │
Graded 17 subjects · 17 passed · 0 warned · 0 failed
```

(Abridged — 17 subjects are graded, covering every claim in the list above.)

A `FAIL` means upstream changed and results may be wrong until the contract is
re-measured — and each check has a negative side, so it can actually fail: it
proves `price` still filters *and* that `sp`/`ep` are still ignored. Full detail:
[docs/api-contract.md](docs/api-contract.md).

---

## Proxies and blocks

Every request is direct by default. Nothing here is needed until Chợ Tốt
starts answering `HTTP 403` or `429` to your address.

| Option | What it does |
|---|---|
| `--proxy http://host:port` | Use this HTTP proxy for every request. Also `CHOTOT_PROXY`, which outranks `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`; `none` forces a direct connection even when those are set. |
| `--proxy auto` | Resolve a residential proxy **now** and use it from the first request. If nothing resolves, this is an error — never a quiet direct connection. |
| `--auto-proxy` | **Direct first.** After a `403`, a `429` or a connection failure, resolve a residential proxy and use it for the rest of the run. Announced on stderr once. Also `CHOTOT_AUTO_PROXY=1`. |
| `--geo CC` | Exit country for the resolver (default `vn`). Applies to `auto` and the fallback only; an explicit URL is used exactly as given. |

The residential proxy costs money per byte, which is why `--auto-proxy` pays
nothing until a block is actually seen, and why a proxy you named yourself is
never swapped for a paid one behind your back.

**SOCKS is not supported.** The standard library has no SOCKS client, so a
`socks5://` URL — on the flag or in `ALL_PROXY` — is refused with the cause
named rather than failing deep inside `urllib`. Clash and mihomo expose an HTTP
proxy on the same port; use that.

**Where the residential credential comes from.** `chotot` never holds one. It
runs a *resolver command* whose stdout is a proxy URL:

```bash
export CHOTOT_PROXY_RESOLVER='my-resolver --country {geo}'   # {geo} is replaced
```

Exit non-zero or print nothing to say "no proxy available". When the variable
is unset, the [`ultra-low-cost-scraper`](https://github.com/vecyang1) skill's
`proxy_resolver.py` is used if it is installed; it owns the DataImpulse
credential (1Password, environment or its own cache) and its geo tagging, and
this tool reads none of that itself.

Other knobs: `CHOTOT_PROXY_FALLBACK_STATUSES=403,429` changes which statuses
trigger the switch; `CHOTOT_BASE_URL` points the CLI at another gateway root (a
mirror, or the local servers the end-to-end suite stands up).

`chotot doctor --proxy auto` grades the whole contract through the proxy and
prints the transport line — mode, masked proxy, source, and how many requests
were proxied — so a report always says which address it was measured from.
`chotot doctor --json` emits the same checks and the transport object as JSON.

The shared options `--timeout`, `--min-interval`, `--retries`, `--verbose` and
`--no-colour` are accepted before or after the subcommand; `--verbose` logs
every gateway request to stderr, with the proxy masked.

---

## Exit codes

Scripts can branch on these:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected internal error |
| 2 | usage error (bad flag, unresolvable region, impossible filter) |
| 3 | success, but nothing matched |
| 4 | the listing or seller does not exist |
| 5 | network / transport failure |
| 6 | rate limited upstream |
| 7 | the gateway answered in an unrecognised shape |

`--json` output goes to stdout alone; warnings go to stderr, so
`chotot search x --json > out.json` is always valid JSON.

---

## Etiquette and legality

This reads the same public endpoints the Chợ Tốt website uses, at a default of
one request every 200 ms. Please keep it that way: raise `--min-interval` rather
than lower it for bulk work, and do not use this to spam sellers.

Phone numbers are masked by the listing API and are left masked here. The shop
endpoint exposes them unmasked; this tool redacts them unless you pass
`--show-contact`.

Not affiliated with or endorsed by Chợ Tốt or Carousell.

---

## Development

```bash
python3 -m pytest tests/ -rs             # -rs so a skip cannot read as a pass
python3 tools/mutate.py                  # every mutant must be caught; the run prints the count
chotot doctor                            # re-measure the live contract against the gateway
```

The suite includes a real-process end-to-end run of the proxy fallback: the
CLI is executed against three servers on `127.0.0.1` — a gateway that blocks,
a proxy that forwards, an upstream that answers — through the real entry point,
so the one paid path is exercised on every run without a residential byte.

The suite is checked on Python 3.10 as well as 3.14 — an earlier revision could
not be imported at all below 3.14, and passed its tests anyway.

Use `-rs`. The packaging tests build a wheel and install it into a clean
virtualenv, and they **skip** rather than fail when pip's build backend is
unavailable — an editable-install `.pth` left in a system interpreter was enough
to skip all of them, and the run still printed a healthy-looking `passed`. A
gate that skips itself reports the same green as one that passed.

Refresh the bundled taxonomy and facet snapshots:

```bash
python3 tools/build_taxonomy.py --harvest-areas
python3 tools/build_facets.py
```

Licensed under the **GNU Affero General Public License v3.0 or later**
([AGPL-3.0-or-later](LICENSE)).

You may use, study, modify and redistribute this tool freely. The AGPL adds one
condition beyond the GPL that matters for a marketplace client: if you run a
**modified** version as a network service — a hosted price API, a bot, a web
front-end — you must offer its users the corresponding source of your modified
version. Running the unmodified tool, for any purpose including commercial, is
unrestricted.
