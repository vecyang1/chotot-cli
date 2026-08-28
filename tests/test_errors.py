"""Error messages are an interface. Nothing else asserts on them, so this does.

The decidable slice: **if a remedy names a command, that command must parse.**
Wording stays a matter of review; a remedy telling the user to run something
that exits immediately does not.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from chotot.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent

#: Subcommands are lowercase words; 'import' is Python, not a subcommand.
_NOT_A_SUBCOMMAND = {"import"}

#: Placeholders substituted at runtime: '<id>' typed by a user, and '{expr}'
#: interpolated by an f-string. Both are replaced BEFORE the shell split, or
#: '<id>' is mistaken for a redirect.
PLACEHOLDER = re.compile(r"<[^>]+>|\{[^}]+\}")

#: Shell plumbing and trailing comments are not part of the command the parser
#: sees: `chotot doctor   # re-measure` is one command plus prose.
SHELL_BREAK = re.compile(r"\s[|><&;#]")

_MARKER = "chotot "


def _candidate_commands(text: str):
    """Yield every printed ``chotot`` invocation.

    Written as a scanner rather than one regex because a character class that
    excludes quotes truncates every command at its first quoted argument --
    ``chotot analyze "iphone 13" --samples 150`` becomes ``analyze``, which then
    fails to parse and looks like a defect in the command rather than in the
    extractor. Here a command wrapped in quotes ends at its closing quote, and
    an unwrapped one ends at the line or a Markdown backtick.
    """
    for line in text.splitlines():
        position = 0
        while True:
            index = line.find(_MARKER, position)
            if index == -1:
                break
            position = index + len(_MARKER)
            before = line[index - 1] if index else ""
            if line[max(0, index - 5):index] == "from ":
                continue  # Python import, not a command
            rest = line[position:]
            if before in ("'", '"', "`"):
                # Command wrapped in quotes or backticks:
                #   'chotot regions --search <name>'   ``chotot doctor``
                closing = rest.find(before)
                raw = rest[:closing] if closing != -1 else rest
            else:
                # Command embedded mid-literal:
                #   f"... run: chotot shop {alias}", "dim"
                # Stop at the quote that closes the enclosing literal, or the
                # trailing Python code is graded as part of the command.
                opener = max(line.rfind('"', 0, index), line.rfind("'", 0, index))
                if opener != -1:
                    quote = line[opener]
                    closing = rest.find(quote)
                    raw = rest[:closing] if closing != -1 else rest
                else:
                    raw = rest.split("`")[0]
            raw = PLACEHOLDER.sub("placeholder", raw)
            raw = SHELL_BREAK.split(raw)[0].strip().rstrip(".,;:)")
            if not raw or raw.startswith("-"):
                continue
            if raw.split()[0] in _NOT_A_SUBCOMMAND:
                continue
            yield raw


def _parses(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    parser = build_parser()
    try:
        parser.parse_args(argv)
        return True
    except SystemExit:
        return False


def _sources():
    """Every file that can contain a printed command, not just one of them."""
    files = list((ROOT / "chotot").rglob("*.py"))
    files += [p for p in ROOT.glob("*.md")]
    files += [p for p in (ROOT / "docs").rglob("*.md")] if (ROOT / "docs").exists() else []
    return [p for p in files if p.is_file()]


def test_every_command_string_in_source_and_docs_parses():
    """Ranges over source AND docs: a gate named 'every command' that reads one
    file is a narrower denominator than its name claims."""
    graded, broken = 0, []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for command in _candidate_commands(text):
            graded += 1
            if not _parses(command):
                broken.append(f"{path.relative_to(ROOT)}: chotot {command}")
    # Report the denominator: a selector that silently narrows later (a renamed
    # file, a moved directory) shows up as a number that dropped, not as
    # continued green.
    print(f"graded {graded} command strings across {len(_sources())} files")
    assert graded >= 20, f"only graded {graded} command strings; the extractor is too narrow"
    assert not broken, "commands that do not parse:\n  " + "\n  ".join(broken)


def test_the_command_gate_can_actually_fail():
    """A gate that has only ever seen valid input is not evidence."""
    assert not _parses("search --nonexistent-flag x")
    assert not _parses("teleport 42")
    assert _parses("search iphone --region hcm")


def test_every_error_carries_an_exit_code_and_they_are_distinct():
    from chotot import errors

    classes = [
        errors.UsageError, errors.NotFoundError, errors.TransportError,
        errors.RateLimitedError, errors.UpstreamContractError,
    ]
    codes = {c.exit_code for c in classes}
    assert len(codes) == len(classes), "two error classes share an exit code"
    assert all(c.exit_code > 0 for c in classes)


def test_shared_errors_do_not_name_one_caller_operation():
    """A message raised from shared code must say only what every caller shares."""
    from chotot.errors import TransportError

    message = str(TransportError("Could not reach the Chợ Tốt gateway"))
    for specific in ("search", "detail", "analyze", "export"):
        assert specific not in message.lower()


def test_remedies_are_actionable_sentences():
    from chotot.errors import ResolutionError

    error = ResolutionError("Unknown province: 'x'", remedy="Run 'chotot regions --search <name>'.")
    assert error.remedy and len(error.remedy) > 10
