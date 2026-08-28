"""The documentation is an interface too, and nothing else asserts on it.

These are the decidable claims only: names, codes and counts that either match
the code or do not. Prose, tone and how much to explain stay matters for review.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONTRACT_DOC = (ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")


def test_readme_lists_every_mcp_tool():
    """A tool added without a README line is undiscoverable to a reader."""
    from chotot.mcp_server import TOOLS

    live = {t["name"] for t in TOOLS}
    mentioned = set(re.findall(r"chotot_[a-z_]+", README))
    missing = live - mentioned
    assert not missing, f"MCP tools absent from the README: {missing}"
    assert not (mentioned - live), f"README names tools that do not exist: {mentioned - live}"


def test_readme_tool_count_matches():
    stated = re.search(r"(\w+) tools are exposed", README)
    if stated:
        from chotot.mcp_server import TOOLS

        words = {"six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        expected = words.get(stated.group(1).lower())
        if expected:
            assert expected == len(TOOLS), \
                f"README says {stated.group(1)} tools, there are {len(TOOLS)}"


def test_readme_documents_every_subcommand():
    from chotot.cli import build_parser

    parser = build_parser()
    # Aliases share a handler and need no separate documentation; the canonical
    # names are the ones registered as their own subparser.
    canonical = {
        name for name, sub in
        next(a for a in parser._actions
             if getattr(a, "choices", None) and a.dest == "command").choices.items()
        if sub.prog.rsplit(" ", 1)[-1] == name
    }
    missing = {c for c in canonical if f"chotot {c}" not in README}
    assert not missing, f"subcommands absent from the README: {missing}"


def test_exit_code_table_matches_the_error_classes():
    from chotot import cli, errors

    documented = dict(re.findall(r"^\| (\d) \| ([^|]+) \|$", README, re.MULTILINE))
    assert documented, "the exit-code table is missing from the README"
    for code in ("0", "1", "2", "3", "4", "5", "6", "7"):
        assert code in documented, f"exit code {code} is not documented"

    live = {
        str(errors.UsageError.exit_code), str(errors.NotFoundError.exit_code),
        str(errors.TransportError.exit_code), str(errors.RateLimitedError.exit_code),
        str(errors.UpstreamContractError.exit_code), str(cli.EXIT_NO_RESULTS),
    }
    assert live <= set(documented), f"undocumented exit codes: {live - set(documented)}"


def test_contract_doc_agrees_with_the_contract_module():
    """The prose and the executable contract must not drift apart."""
    from chotot import contract

    assert str(contract.TOTAL_CAP) in CONTRACT_DOC.replace(",", "")
    assert str(contract.MAX_SEARCH_WINDOW) in CONTRACT_DOC.replace(",", "")
    assert str(contract.MAX_PAGE_SIZE) in CONTRACT_DOC
    for param in ("sp", "ep", "company_ad", "account_oid"):
        assert param in CONTRACT_DOC, f"{param} is on the ignore list but undocumented"


def test_contract_doc_does_not_claim_a_removed_error_param():
    """`direction` was moved out of ERRORING_PARAMS; the doc must not still refuse it."""
    from chotot import contract

    for param in contract.ERRORING_PARAMS:
        assert param in CONTRACT_DOC
    assert "direction" not in contract.ERRORING_PARAMS
    assert "Hướng cửa chính" in CONTRACT_DOC, \
        "the doc should explain that `direction` is also a working property facet"


def test_documented_facet_and_province_counts_are_current():
    """Counts rot silently; these come from the shipped snapshots."""
    from chotot import facets, taxonomy

    working = sum(len(facets.for_category(int(c))) for c in facets._all())
    stated = [int(n) for n in re.findall(r"(\d{3}) verified working facets", CONTRACT_DOC)]
    for value in stated:
        assert value == working, f"doc says {value} working facets, snapshot has {working}"

    modern = len(taxonomy.modern_groups())
    for value in [int(n) for n in re.findall(r"onto \*\*(\d+) modern provinces\*\*", CONTRACT_DOC)]:
        assert value == modern, f"doc says {value} modern provinces, snapshot has {modern}"
