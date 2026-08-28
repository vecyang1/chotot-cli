# AGENTS.md — chotot-cli

Runtime and behaviour owner for this repo. Read `docs/api-contract.md` before
modifying anything in `chotot/` or `tools/`.

## Hard rules

0. **Zero runtime dependencies.**
   `chotot-cli` is built exclusively on Python 3.9+ standard library modules (`urllib.request`,
   `json`, `sqlite3`, `argparse`, `math`, `csv`). No third-party packages may be added to
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

3. **Multi-province expansion (2025 Administrative Reforms).**
   Vietnam's 2025 administrative mergers combine previously separate province codes. The taxonomy
   layer (`chotot/taxonomy.py`) maps merged province keys to their underlying modern sub-codes
   and performs balanced round-robin sampling across all constituent regions.

4. **Rate limiting and backoff.**
   `chotot/http.py` enforces a minimum 0.2s interval between requests, honours `Retry-After`
   headers, and uses exponential backoff on HTTP 429/503. Never disable pacing or retries.

5. **Location-independent launcher.**
   `bin/chotot` resolves its path through symlinks and injects `$root` into `PYTHONPATH`.
   Documented CLI commands must use `chotot <cmd>` or `bin/chotot <cmd>`, never `cd <repo> && python3 -m chotot.cli`.

## CLI Subcommands

| Command | Description |
|---|---|
| `chotot search <query>` | Search listings by keyword, region, category, price, condition, seller type |
| `chotot detail <id\|url>` | Fetch complete listing record (images, specs, seller, location) |
| `chotot analyze <query>` | Market price distribution (median, IQR, condition breakdown, histogram) |
| `chotot seller <account_id>` | List live inventory and pricing summary for a seller |
| `chotot shop <alias>` | Fetch professional storefront profile (redacted by default) |
| `chotot facets <category>` | List probe-verified filter parameters for a category |
| `chotot categories` | Display category taxonomy and numeric codes |
| `chotot regions` | Display province and district codes (including 2025 merger groups) |
| `chotot export <query>` | Export search results to CSV, JSON, or Markdown |
| `chotot doctor` | Re-measure upstream gateway contract and report API health |
| `chotot mcp` | Serve FastMCP/JSON-RPC server over stdio for AI agent tool calling |

## Verification Protocol

```bash
# 1. Full unit test suite (388 tests)
python3 -m pytest tests/

# 2. Packaging test (builds and validates wheel in isolated sandbox)
python3 -m pytest tests/test_packaging.py

# 3. Mutation test harness (54/54 mutants caught)
python3 tools/mutate.py

# 4. Upstream live contract check
chotot doctor
```
