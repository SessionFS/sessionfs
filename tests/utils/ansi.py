"""ANSI-stripping helpers for CLI-output assertions.

Rich/Typer render text with embedded ANSI escape sequences when color
is enabled. Substring assertions like `"--to" in result.output` fail in
those environments because `--to` is split across `\\x1b[1;36m-\\x1b[0m`
+ `\\x1b[1;36m-to\\x1b[0m`. Local pytest disables color by default so
this only fails in CI (or under FORCE_COLOR=1).

Standard pattern: route every CLI-output substring check through
`strip_ansi()` or `assert_in_ansi()` so tests stay green regardless
of the color-detection environment.

Validate test suites under `FORCE_COLOR=1 pytest` to catch the
fragility class before it ships to CI.
"""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from `text`.

    Use whenever you're asserting on captured stdout/stderr from a
    Rich- or Typer-rendered CLI command. Idempotent: stripping an
    already-clean string is a no-op.
    """
    if not text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def assert_in_ansi(needle: str, output: str, *, case_insensitive: bool = True) -> None:
    """Assert `needle` appears in `output` after stripping ANSI codes.

    `case_insensitive=True` matches the common pattern across our tests
    where we compare on lowercased output (so `Missing option '--to'`
    matches whether the rendering is title-cased, bolded, etc.).
    """
    cleaned = strip_ansi(output)
    if case_insensitive:
        if needle.lower() not in cleaned.lower():
            raise AssertionError(
                f"expected {needle!r} in CLI output (after ANSI strip);"
                f" got: {cleaned!r}"
            )
    else:
        if needle not in cleaned:
            raise AssertionError(
                f"expected {needle!r} in CLI output (after ANSI strip);"
                f" got: {cleaned!r}"
            )


def assert_not_in_ansi(
    needle: str, output: str, *, case_insensitive: bool = True
) -> None:
    """Inverse of assert_in_ansi — fails if `needle` appears after stripping."""
    cleaned = strip_ansi(output)
    if case_insensitive:
        if needle.lower() in cleaned.lower():
            raise AssertionError(
                f"did NOT expect {needle!r} in CLI output (after ANSI strip);"
                f" got: {cleaned!r}"
            )
    else:
        if needle in cleaned:
            raise AssertionError(
                f"did NOT expect {needle!r} in CLI output (after ANSI strip);"
                f" got: {cleaned!r}"
            )
