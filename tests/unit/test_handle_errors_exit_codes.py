"""Regression guard: `handle_errors` must preserve deliberate exit codes
across typer releases.

typer 0.27 made `typer.Exit` / `typer.Abort` standalone classes that no
longer inherit from click's, so a `raise typer.Exit(2)` inside a decorated
command printed "Unexpected error: 2" and exited 1 (and `typer.Exit(0)` on a
success path became a failure). This failed only in CI, which installs the
newest typer, while local environments on 0.26 stayed green.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from sessionfs.cli.common import handle_errors

runner = CliRunner()


def _app_raising(exc: BaseException) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    @handle_errors
    def cmd() -> None:
        raise exc

    return app


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42])
def test_typer_exit_code_is_preserved(code):
    result = runner.invoke(_app_raising(typer.Exit(code)), [])
    assert result.exit_code == code
    assert "Unexpected error" not in result.output


def test_typer_abort_is_a_clean_cancel_not_a_crash():
    result = runner.invoke(_app_raising(typer.Abort()), [])
    assert result.exit_code == 130
    assert "Unexpected error" not in result.output


def test_typer_bad_parameter_still_renders_as_usage_error():
    result = runner.invoke(_app_raising(typer.BadParameter("nope")), [])
    assert result.exit_code == 2
    assert "Unexpected error" not in result.output


def test_genuine_errors_still_report_as_unexpected():
    result = runner.invoke(_app_raising(ValueError("boom")), [])
    assert result.exit_code == 1
    assert "Unexpected error" in result.output
