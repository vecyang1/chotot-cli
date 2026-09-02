"""CLI wiring: exit codes, stream discipline, and refusal paths.

Exit codes are part of the interface, so they are asserted rather than assumed.
"""
from __future__ import annotations

import json

import pytest

from chotot import cli
from chotot.client import ChototClient


@pytest.fixture()
def patched(monkeypatch, fake_transport):
    """Route every CLI-constructed client through the fake transport."""
    monkeypatch.setattr(cli, "_client", lambda args: ChototClient(transport=fake_transport))
    return fake_transport


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_no_command_prints_help_and_exits_usage(capsys):
    code, out, _ = run([], capsys)
    assert code == cli.EXIT_USAGE
    assert "usage:" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


def test_search_returns_zero_and_prints_a_table(patched, capsys):
    code, out, err = run(["search", "iphone", "--limit", "3", "--no-colour"], capsys)
    assert code == cli.EXIT_OK
    assert "Price" in out and "iPhone" in out or "iphone" in out.lower()


def test_json_output_is_parseable_and_alone_on_stdout(patched, capsys):
    """Warnings must go to stderr or `--json > file` produces invalid JSON."""
    code, out, err = run(["search", "iphone", "--limit", "3", "--json"], capsys)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["count"] == 3
    assert "coverage" in payload
    # The caveats still reach the operator, just not the pipe.
    assert err.strip(), "warnings vanished instead of going to stderr"


def test_csv_output_is_alone_on_stdout(patched, capsys):
    code, out, _ = run(["search", "iphone", "--limit", "3", "--csv"], capsys)
    assert code == cli.EXIT_OK
    assert out.lstrip("\ufeff").splitlines()[0].startswith("list_id,")


def test_no_results_uses_a_distinct_exit_code(patched, capsys):
    code, _, _ = run(["search", "zzzznonexistentqueryxyz", "--json"], capsys)
    assert code == cli.EXIT_NO_RESULTS


def test_unknown_region_exits_usage_with_a_suggestion(patched, capsys):
    code, _, err = run(["search", "iphone", "--region", "ha noii"], capsys)
    assert code == cli.EXIT_USAGE
    assert "Did you mean" in err


def test_dead_legacy_region_code_is_refused(patched, capsys):
    """11000 'Đà Nẵng' silently returned nothing in the predecessor."""
    code, _, err = run(["search", "iphone", "--region", "11000"], capsys)
    assert code == cli.EXIT_USAGE
    assert "3017" in err


def test_missing_listing_exits_not_found(patched, capsys):
    code, _, err = run(["detail", "1"], capsys)
    assert code == 4
    assert "not found" in err.lower()


def test_detail_json(patched, capsys):
    code, out, _ = run(["detail", "134397861", "--json"], capsys)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["list_id"] and "specs" in payload


def test_analyze_dashboard(patched, capsys):
    code, out, err = run(["analyze", "iphone", "--samples", "40", "--no-colour"], capsys)
    assert code == cli.EXIT_OK
    assert "Asking-price analysis" in out
    assert "ASKING" in err, "the asking-vs-sold caveat must reach the user"


def test_analyze_price_check(patched, capsys):
    code, out, _ = run(["analyze", "iphone", "--samples", "40",
                        "--price-check", "1000000", "--no-colour"], capsys)
    assert code == cli.EXIT_OK
    assert "Price check" in out


def test_export_refuses_to_clobber(patched, tmp_path, capsys):
    target = tmp_path / "out.csv"
    target.write_text("existing")
    code, _, err = run(["export", "iphone", "--output", str(target)], capsys)
    assert code == cli.EXIT_USAGE
    assert target.read_text() == "existing", "an existing file was overwritten"
    assert "--overwrite" in err


def test_export_writes_a_csv(patched, tmp_path, capsys):
    target = tmp_path / "out.csv"
    code, _, _ = run(["export", "iphone", "--limit", "5", "--output", str(target)], capsys)
    assert code == cli.EXIT_OK
    # Read as Excel/pandas would: utf-8-sig strips the BOM transparently.
    assert target.read_text(encoding="utf-8-sig").splitlines()[0].startswith("list_id,")


def test_export_infers_format_and_rejects_unknown_suffix(patched, tmp_path, capsys):
    code, _, err = run(["export", "iphone", "--output", str(tmp_path / "out.xyz")], capsys)
    assert code == cli.EXIT_USAGE
    assert "--format" in err


def test_categories_and_regions_do_not_need_the_network(capsys):
    """Taxonomy is bundled, so these must work offline."""
    for argv in (["categories", "--parent", "5000"], ["regions", "--search", "can tho"]):
        code, out, _ = run(argv + ["--no-colour"], capsys)
        assert code == cli.EXIT_OK
        assert out.strip()


def test_regions_shows_merged_province_codes(capsys):
    code, out, _ = run(["regions", "--search", "ho chi minh", "--json"], capsys)
    assert code == cli.EXIT_OK
    entries = json.loads(out)
    assert any(len(e.get("sibling_region_v2") or []) > 1 for e in entries)


def test_no_colour_flag_suppresses_ansi(patched, capsys):
    _, out, _ = run(["search", "iphone", "--limit", "2", "--no-colour"], capsys)
    assert "\033[" not in out


def test_every_subcommand_is_reachable():
    """A handler map that drifts from the parser silently loses a command."""
    parser = cli.build_parser()
    choices = set()
    for action in parser._actions:
        if getattr(action, "choices", None) and action.dest == "command":
            choices = set(action.choices)
    assert choices, "no subcommands registered"
    missing = choices - set(cli.HANDLERS)
    assert not missing, f"subcommands with no handler: {missing}"


def test_price_check_zero_reaches_the_analyser(patched, capsys):
    """`if args.price_check` dropped an explicit 0, so the analyser's dedicated
    non-positive-price message was unreachable from the CLI."""
    code, out, _ = run(["analyze", "iphone", "--samples", "30",
                        "--price-check", "0", "--no-colour"], capsys)
    assert code == cli.EXIT_OK
    assert "Price check" in out
    assert "positive" in out.lower()


def test_price_check_zero_is_not_rendered_as_negotiable(patched, capsys):
    _, out, _ = run(["analyze", "iphone", "--samples", "30",
                     "--price-check", "0", "--no-colour"], capsys)
    assert "Price check — Negotiable" not in out


def test_regions_province_expands_the_merger_group(capsys):
    """The MCP tool was fixed and the CLI was not: 5 districts of 23 for Cần Thơ."""
    code, out, _ = run(["regions", "--province", "can tho", "--json"], capsys)
    assert code == cli.EXIT_OK
    districts = json.loads(out)
    assert len({d["region_v2"] for d in districts}) > 1, \
        "only one legacy code's districts were listed"


def test_export_reports_a_filesystem_error_as_usage_not_an_internal_bug(patched, tmp_path, capsys):
    target = tmp_path / "adir"
    target.mkdir()
    code, _, err = run(["export", "iphone", "--limit", "5",
                        "--output", str(target), "--overwrite"], capsys)
    assert code == cli.EXIT_USAGE
    assert "unexpected error" not in err


# -- shop, and flags reaching the client ------------------------------------

def test_shop_redacts_phones_by_default(patched, capsys):
    """The CLI path had no test at all, so the privacy switch could be inverted
    and nothing would notice."""
    code, out, err = run(["shop", "someshop", "--no-colour"], capsys)
    assert code == cli.EXIT_OK
    assert "0909937666" not in out
    assert "redacted" in err.lower()


def test_shop_show_contact_reveals_only_on_request(patched, capsys):
    code, out, _ = run(["shop", "someshop", "--show-contact", "--json"], capsys)
    assert code == cli.EXIT_OK
    assert "0909937666" in json.loads(out)["phones"]


def test_shop_json_marks_the_redaction_state(patched, capsys):
    _, out, _ = run(["shop", "someshop", "--json"], capsys)
    payload = json.loads(out)
    assert payload["phones_redacted"] is True
    assert all("*" in p for p in payload["phones"])


class _Recorder:
    """Captures what the CLI actually asked the client for.

    The existing fixture replaced `_client` with a lambda that discards `args`,
    so nothing downstream could observe a dropped flag -- ten flag-dropping
    mutants escaped.
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.inner.search(**kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture()
def recorded(monkeypatch, fake_transport):
    from chotot.client import ChototClient

    holder = {}

    def make(args):
        holder["client"] = _Recorder(ChototClient(transport=fake_transport))
        return holder["client"]

    monkeypatch.setattr(cli, "_client", make)
    return holder


@pytest.mark.parametrize("argv,expected", [
    (["--category", "phone"], {"category": "phone"}),
    (["--region", "hcm"], {"region": "hcm"}),
    (["--district", "quan 1"], {"district": "quan 1"}),
    (["--min-price", "1000"], {"min_price": 1000}),
    (["--max-price", "9000"], {"max_price": 9000}),
    (["--condition", "used"], {"condition": "used"}),
    (["--seller-type", "shop"], {"seller_type": "shop"}),
    (["--listing-type", "rent"], {"listing_type": "rent"}),
    # --facet needs --category to resolve the facet's vocabulary, by design.
    (["--category", "phone", "--facet", "mobile_brand=apple"],
     {"facets": {"mobile_brand": "1"}}),
    (["--no-expand-region"], {"expand_region": False}),
])
def test_every_search_flag_reaches_the_client(recorded, capsys, argv, expected):
    cli.main(["search", "iphone", "--limit", "3"] + argv)
    capsys.readouterr()
    call = recorded["client"].calls[0]
    for name, value in expected.items():
        assert call.get(name) == value, f"{name}: CLI sent {call.get(name)!r}"


def test_sort_and_limit_reach_the_client(recorded, capsys):
    cli.main(["search", "iphone", "--limit", "7", "--sort", "price_desc"])
    capsys.readouterr()
    call = recorded["client"].calls[0]
    assert call["limit"] == 7 and call["sort"] == "price_desc"


def test_analyze_flags_reach_the_client(recorded, capsys):
    cli.main(["analyze", "iphone", "--samples", "33", "--region", "hcm"])
    capsys.readouterr()
    call = recorded["client"].calls[0]
    assert call["limit"] == 33 and call["region"] == "hcm"


def test_facet_without_category_is_refused_before_any_request(patched, capsys):
    """The facet vocabulary is per category, so there is nothing to resolve
    against; forwarding it blindly would filter on a code from another category."""
    code, _, err = run(["search", "iphone", "--facet", "mobile_brand=apple"], capsys)
    assert code == cli.EXIT_USAGE
    assert "category" in err.lower()


# -- proxy flags reach the transport, and refusals happen before any request ----

def test_socks_proxy_exits_usage_before_any_request(capsys):
    """The transport is constructed before the first request; a SOCKS URL must
    be refused there, not deep inside urllib as 'unknown url type'. Unpatched
    on purpose: the refusal happens before anything could reach the network."""
    code, _, err = run(["search", "iphone", "--proxy", "socks5://127.0.0.1:1080"], capsys)
    assert code == cli.EXIT_USAGE
    assert "SOCKS" in err and "--proxy" in err


def test_unresolvable_explicit_auto_exits_usage(monkeypatch, capsys):
    import sys
    monkeypatch.setenv("CHOTOT_PROXY_RESOLVER", f"{sys.executable} -c 'raise SystemExit(1)'")
    code, _, err = run(["search", "iphone", "--proxy", "auto"], capsys)
    assert code == cli.EXIT_USAGE
    assert "CHOTOT_PROXY_RESOLVER" in err


def test_proxy_flags_reach_the_transport(monkeypatch):
    calls = {}

    def fake_transport_ctor(base_url, **kwargs):
        calls.update(kwargs, base_url=base_url)
        return object()

    monkeypatch.setattr("chotot.client.HttpTransport", fake_transport_ctor)
    args = cli.build_parser().parse_args(
        ["--proxy", "http://127.0.0.1:8080", "--auto-proxy", "--geo", "jp", "search", "x"])
    for name, value in cli.GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    cli._client(args)
    assert calls["proxy"] == "http://127.0.0.1:8080"
    assert calls["auto_proxy"] is True and calls["geo"] == "jp"


def test_geo_is_unset_unless_given(monkeypatch):
    """A default of 'vn' made 'given explicitly' indistinguishable from
    'defaulted', so the transport could not warn when --geo was paired with a
    proxy URL it cannot apply to."""
    calls = {}
    monkeypatch.setattr("chotot.client.HttpTransport",
                        lambda base_url, **kw: calls.update(kw) or object())
    args = cli.build_parser().parse_args(["search", "x"])
    for name, value in cli.GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    cli._client(args)
    assert calls["geo"] is None


def test_base_url_is_overridable_from_the_environment(monkeypatch):
    """The hook the end-to-end suite stands on: point the CLI at a local server."""
    calls = {}
    monkeypatch.setattr("chotot.client.HttpTransport",
                        lambda base_url, **kw: calls.update(base_url=base_url) or object())
    monkeypatch.setenv("CHOTOT_BASE_URL", "http://127.0.0.1:1/v1/public")
    args = cli.build_parser().parse_args(["search", "x"])
    for name, value in cli.GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    cli._client(args)
    assert calls["base_url"] == "http://127.0.0.1:1/v1/public"


def test_mcp_serves_the_cli_configured_client(monkeypatch, capsys):
    """`chotot mcp --proxy ...` accepted the flags and served a default client."""
    seen = {}
    monkeypatch.setattr("chotot.mcp_server.serve_stdio", lambda client: seen.update(client=client) or 0)
    monkeypatch.setattr(cli, "_client", lambda args: ("configured", args.proxy))
    assert cli.main(["mcp", "--proxy", "http://127.0.0.1:1"]) == 0
    assert seen["client"] == ("configured", "http://127.0.0.1:1")
