# AGENTS.md — chotot-cli

Runtime and behaviour owner for this repo. Read `docs/api-contract.md` before
modifying anything in `chotot/` or `tools/`.

## Hard rules

0. **Zero runtime dependencies.**
   `chotot-cli` is built exclusively on Python 3.9+ standard library modules (`urllib.request`,
   `json`, `argparse`, `math`, `csv`, `statistics`). There is no database: the bundled
   taxonomy and facet snapshots are plain JSON under `chotot/data/`. No third-party packages may be added to
   `dependencies = []` in `pyproject.toml`. This guarantees it runs in any bare Python environment.

1. **Privacy by default.**
   Seller and shop contact details (phone numbers) are redacted by default across CLI and MCP
   surfaces. Only explicit operator intent via `--show-contact` (or `show_contact: true` in MCP)
   exposes raw phone numbers. Never bypass or soften this redaction.

2. **Honest contract audit.**
   The Chợ Tốt gateway silently ignores several query parameters (e.g., sort order, seller
   type, and condition on certain categories), returning unranked or unfiltered sets.
   `chotot` enforces these client-side and explicitly states when sorting or filtering is
   performed locally. Never present a sampled or locally-filtered result as an exact server-side count.

3. **Query terms are UNIONed upstream, never intersected.**
   `q="canon powershot v1"` returns the 39 ads matching `v1` plus the 1 matching
   `canon powershot` — adding a word WIDENS the result set, and the least specific word
   dominates the ranking. Every search checks the text of the rows it received and warns
   when they do not all carry every term; `--match-all` recomputes the intersection
   client-side. Never remove that guard: without it `analyze` reports a confident median
   for a product that has no listings at all.

4. **Multi-province expansion (2025 Administrative Reforms).**
   Vietnam's 2025 administrative mergers combine previously separate province codes. The taxonomy
   layer (`chotot/taxonomy.py`) maps merged province keys to their underlying legacy sub-codes.
   Sampling across them is **proportional to each region's real size**, not balanced
   round-robin: an equal split made `--region hcm` return mostly the annexed provinces
   and almost none of the city itself.

5. **Rate limiting and backoff.**
   `chotot/http.py` enforces a minimum 0.2s interval between requests, honours `Retry-After`
   headers, and uses exponential backoff on HTTP 429/503. Never disable pacing or retries.

6. **Location-independent launcher.**
   `bin/chotot` resolves its path through symlinks and injects `$root` into `PYTHONPATH`.
   CLI commands should be invoked directly via the `chotot` binary or `bin/chotot`, never via `cd <repo> && python3 -m chotot.cli`.

## CLI Subcommands

| Command | Description |
|---|---|
| `chotot search "iphone 13"` | Search listings by keyword, region, category, price, condition, seller type |
| `chotot detail 134348455` | Fetch complete listing record (images, specs, seller, location) |
| `chotot analyze "iphone 13"` | Market price distribution (median, IQR, condition breakdown, histogram) |
| `chotot seller 17864227` | List live inventory and pricing summary for a seller |
| `chotot shop apple_store` | Fetch professional storefront profile (redacted by default) |
| `chotot facets 5010` | List probe-verified filter parameters for a category |
| `chotot categories` | Display category taxonomy and numeric codes |
| `chotot regions` | Display province and district codes (including 2025 merger groups) |
| `chotot export "ipad" -o out.csv` | Export search results to CSV, JSON, or Markdown |
| `chotot doctor` | Re-measure upstream gateway contract and report API health |
| `chotot mcp` | Serve the Model Context Protocol (JSON-RPC 2.0) over stdio; 8 tools |

## Verification Protocol

```bash
# 1. Full unit test suite (391 tests)
python3 -m pytest tests/ -rs   # -rs: a skipped gate must not read as a pass

# 2. Packaging test (builds and validates wheel in isolated sandbox)
python3 -m pytest tests/test_packaging.py

# 3. Mutation test harness (55/55 mutants caught)
python3 tools/mutate.py

# 4. Upstream live contract check
chotot doctor
```
