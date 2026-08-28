# CHANGELOG — chotot-cli

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-08-28

### Added
- **Proxy Readiness & Anti-Scraping Self-Healing**: Zero-dependency proxy support with `--proxy <url>`, `--proxy auto`, `--auto-proxy`, and `--geo <cc>`.
- **Ultra-Low-Cost Scraper Integration**: Seamless resolution of DataImpulse residential proxy pools with Vietnam geotargeting (`__cr-vn`) from environment variables, local cache, or 1Password Agent Automation vault.
- **Automatic Fallback on Anti-Bot/Rate Limits**: Transport automatically detects HTTP 429 / 403 / connection blocks on direct requests and activates residential proxy tunnel on the fly.
- **Doctor Transport Telemetry**: `chotot doctor` displays masked proxy information and verifies contract health through proxy tunnels.

## [2.0.0] - 2026-08-28

### Added
- **Full CLI Suite**: `search`, `detail`, `analyze`, `seller`, `shop`, `facets`, `categories`, `regions`, `export`, `doctor`, `mcp`.
- **Asking-Price Analyser**: Compute price distributions, medians, IQR, trimmed means, condition breakdowns, price histograms, and individual listing price checks (`--price-check`).
- **Standard Library Only Architecture**: Zero runtime dependencies; runs on pure Python 3.9+ standard library (`urllib`, `json`, `csv`, `statistics`).
- **MCP Server**: Stdio Model Context Protocol integration exposing 8 tools (`chotot_search`, `chotot_get_listing`, `chotot_analyze_prices`, `chotot_seller_listings`, `chotot_shop_profile`, `chotot_list_categories`, `chotot_list_regions`, `chotot_list_facets`) for agentic workflows.
- **Privacy Controls**: Default automatic redaction of seller and shop telephone numbers across CLI output and MCP responses, with explicit `--show-contact` bypass.
- **2025 Vietnam Administrative Reforms**: Bundled taxonomy mapping for 34 merged administrative regions with automatic sub-code query fanout and draws proportional to each region's real size.
- **Category-Specific Facets**: Support for verified filters (brands, storage capacity, fuel, transmission, year, etc.) via `chotot facets 5010` and `--facet key=value`.
- **Multi-Format Export**: Stream search results to CSV (with formula injection protection and UTF-8 BOM), JSON, or Markdown.
- **Doctor Command**: Automated probe testing live upstream gateway endpoints and verifying API contract invariants.
- **Union-aware search**: the gateway's `q` unions its terms rather than intersecting them,
  so `canon powershot v1` returned 39 motorbikes plus 1 camera and `analyze` reported a
  ₫17,500,000 median for a product with zero local listings. Every search now verifies the
  returned rows against the query text and warns; `--match-all` keeps only rows carrying
  every term, and the crawl budget counts survivors so the filter cannot starve the result.
- **Expanded Category Taxonomy**: Added support for standalone `Việc làm` (Jobs - `13010`) and `Dịch vụ nhà cửa` (Home Services - `15000..15040`) categories with English/Vietnamese alias resolution (`job`, `jobs`, `recruitment`, `cleaning`, `moving`, `appliance repair`).
- **Mutation Testing Harness**: 55 mutation test cases in `tools/mutate.py` achieving 100% catch rate.
- **Unit & Packaging Tests**: 392 comprehensive test cases covering client, analyzer, parser, error handling, taxonomy, facets, doctor, formatting, MCP, and isolated wheel installation.
