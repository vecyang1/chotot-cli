"""MCP protocol conformance over the real stdio framing."""
from __future__ import annotations

import io
import json

import pytest

from chotot.client import ChototClient
from chotot.mcp_server import METHOD_NOT_FOUND, PARSE_ERROR, TOOLS, McpServer


def drive(lines, fake_transport):
    stdin = io.StringIO("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n")
    stdout = io.StringIO()
    McpServer(ChototClient(transport=fake_transport), stdin=stdin, stdout=stdout).serve()
    return [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]


def test_initialize_reports_protocol_and_server(fake_transport):
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}], fake_transport)
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "chotot"
    assert "tools" in response["result"]["capabilities"]


def test_notification_produces_no_response(fake_transport):
    """A JSON-RPC notification has no id and must not be answered."""
    assert drive([{"jsonrpc": "2.0", "method": "notifications/initialized"}], fake_transport) == []


def test_tools_list_matches_the_declared_schema(fake_transport):
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], fake_transport)
    names = [t["name"] for t in response["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS]
    for tool in response["result"]["tools"]:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"


def test_every_declared_tool_has_a_handler(fake_transport):
    server = McpServer(ChototClient(transport=fake_transport))
    for tool in TOOLS:
        assert tool["name"] in server._handlers, f"{tool['name']} is advertised but unhandled"


def test_search_tool_returns_structured_content(fake_transport):
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "chotot_search",
                                    "arguments": {"query": "iphone", "limit": 3}}}], fake_transport)
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["count"] == 3
    assert "coverage" in payload and "warnings" in payload


def test_domain_error_is_a_tool_result_not_a_protocol_error(fake_transport):
    """An agent must be able to read and adapt, not see the server as broken."""
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "chotot_search",
                                    "arguments": {"region": "atlantis"}}}], fake_transport)
    assert "error" not in response
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "remedy" in payload


def test_unknown_tool_is_a_protocol_error(fake_transport):
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "nope", "arguments": {}}}], fake_transport)
    assert response["error"]["code"] == -32602


def test_unknown_method(fake_transport):
    [response] = drive([{"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"}], fake_transport)
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_malformed_json_is_reported_and_the_stream_survives(fake_transport):
    responses = drive(["{not json", {"jsonrpc": "2.0", "id": 2, "method": "ping"}], fake_transport)
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[1]["result"] == {}, "the server stopped after one bad line"


def test_tool_descriptions_state_the_sampling_limits():
    """An agent that cannot see the caveat will report a sample as a market fact."""
    for name in ("chotot_search", "chotot_analyze_prices"):
        tool = next(t for t in TOOLS if t["name"] == name)
        assert "ASKING" in tool["description"]
        assert "10000" in tool["description"] or "sample" in tool["description"].lower()


def test_regions_tool_explains_the_merger(fake_transport):
    tool = next(t for t in TOOLS if t["name"] == "chotot_list_regions")
    assert "2025" in tool["description"]
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "chotot_list_regions",
                                    "arguments": {"search": "ho chi minh"}}}], fake_transport)
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["modern_province_groups"]


# -- argument robustness ---------------------------------------------------
#
# Agents send plausible-but-wrong types constantly. Each of these used to
# surface as JSON-RPC -32603, telling the model the SERVER is broken rather
# than that its argument was wrong.

def call(fake_transport, name, arguments):
    [response] = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}}], fake_transport)
    return response


def body(response):
    return json.loads(response["result"]["content"][0]["text"])


@pytest.mark.parametrize("arguments", [
    {"query": "iphone", "limit": "3"},
    {"query": "iphone", "min_price": "1000000", "max_price": "5000000"},
])
def test_numeric_arguments_sent_as_strings_are_accepted(fake_transport, arguments):
    response = call(fake_transport, "chotot_search", arguments)
    assert "error" not in response
    assert response["result"]["isError"] is False


@pytest.mark.parametrize("arguments", [
    {"query": "iphone", "limit": "abc"},
    {"query": "iphone", "limit": True},
])
def test_unusable_numeric_arguments_are_readable_tool_errors(fake_transport, arguments):
    response = call(fake_transport, "chotot_search", arguments)
    assert "error" not in response, "surfaced as a protocol error instead of a tool error"
    assert response["result"]["isError"] is True
    assert "limit" in body(response)["error"]


def test_facets_accepted_as_a_list_as_well_as_an_object(fake_transport):
    for facets in ({"mobile_brand": "apple"}, ["mobile_brand=apple"]):
        response = call(fake_transport, "chotot_search",
                        {"query": "iphone", "category": "5010", "facets": facets})
        assert "error" not in response
        assert response["result"]["isError"] is False


def test_explicit_null_for_a_required_argument_is_refused(fake_transport):
    """`{"query": null}` passed an `in args` check and returned whole-market stats."""
    response = call(fake_transport, "chotot_analyze_prices", {"query": None, "samples": 10})
    assert response["result"]["isError"] is True
    assert "required" in body(response)["error"]


def test_limit_is_clamped_in_the_handler_not_only_advertised(fake_transport):
    """A schema maximum is enforced by nobody; `limit: 300` emitted 274 KB."""
    response = call(fake_transport, "chotot_search", {"query": "iphone", "limit": 5000})
    assert response["result"]["isError"] is False
    assert body(response)["count"] <= 200


def test_reveal_contact_only_accepts_a_genuine_json_true(fake_transport):
    """bool("false") is True, and this switch governs printing phone numbers."""
    for value in ("false", "0", "no", 0, None, False):
        payload = body(call(fake_transport, "chotot_shop_profile",
                            {"alias": "someshop", "reveal_contact": value}))
        assert payload["phones_redacted"] is True, f"reveal_contact={value!r} exposed phones"
        assert all("*" in p for p in payload["phones"])
    payload = body(call(fake_transport, "chotot_shop_profile",
                        {"alias": "someshop", "reveal_contact": True}))
    assert payload["phones_redacted"] is False


def test_seller_tool_keeps_the_analyser_caveats(fake_transport):
    """Publishing the median alone let an agent report a sampled figure as fact."""
    payload = body(call(fake_transport, "chotot_seller_listings", {"account_id": "32068064"}))
    assert payload["warnings"], "every caveat was stripped from the seller payload"
    assert "asking_price_sample" in payload
    assert any("ASKING" in w for w in payload["warnings"])


def test_regions_tool_expands_a_merged_province(fake_transport):
    """Returning one legacy code's districts under the merged name omits most of it."""
    payload = body(call(fake_transport, "chotot_list_regions", {"province": "hcm"}))
    assert len(payload["region_v2_codes"]) > 1
    assert {d["region_v2"] for d in payload["districts"]} == set(payload["region_v2_codes"])


def test_no_cli_flag_reaches_an_mcp_client(fake_transport):
    """Errors are raised by shared code whose remedies name CLI flags.

    An agent cannot pass `--facet`; telling it to is advice it cannot act on.
    Ranges over error AND success payloads, since warnings name flags too.
    """
    import re

    graded = 0
    for name, arguments in [
        ("chotot_search", {"query": "iphone", "category": "5010",
                           "facets": {"elt_warranty": "1"}}),
        ("chotot_search", {"query": "x", "region": "atlantis"}),
        ("chotot_search", {"query": "iphone", "limit": 5}),
        ("chotot_analyze_prices", {"query": "iphone", "samples": 30}),
        ("chotot_seller_listings", {"account_id": "32068064"}),
    ]:
        graded += 1
        response = call(fake_transport, name, arguments)
        text = response["result"]["content"][0]["text"]
        assert not re.findall(r"--[a-z][a-z-]+", text), \
            f"{name} handed the agent a CLI flag: {re.findall(r'--[a-z][a-z-]+', text)}"
    assert graded == 5


def test_keep_outliers_is_exposed_since_a_warning_names_it(fake_transport):
    """A remedy that names an argument the tool does not accept is unactionable."""
    trimmed = body(call(fake_transport, "chotot_analyze_prices",
                        {"query": "iphone", "samples": 60}))
    kept = body(call(fake_transport, "chotot_analyze_prices",
                     {"query": "iphone", "samples": 60, "keep_outliers": True}))
    assert kept["sample"]["outliers_removed"] == 0
    assert kept["sample"]["outliers_removed"] <= trimmed["sample"]["outliers_removed"]


def test_mcp_price_check_zero_reaches_the_analyser(fake_transport):
    """The identical truthiness bug was fixed on the CLI with a test; this line
    kept dropping an explicit 0 with no error and no price_check field."""
    payload = body(call(fake_transport, "chotot_analyze_prices",
                        {"query": "iphone", "samples": 30, "price_check": 0}))
    assert "price_check" in payload, "an explicit 0 was silently dropped"
    assert payload["price_check"]["tier"] == "unknown"
    assert "positive" in payload["price_check"]["note"].lower()


def test_mcp_price_check_is_absent_when_not_asked_for(fake_transport):
    payload = body(call(fake_transport, "chotot_analyze_prices",
                        {"query": "iphone", "samples": 30}))
    assert "price_check" not in payload
