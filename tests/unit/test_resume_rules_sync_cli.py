"""CLI-wiring smoke tests for v0.9.9 resume rules sync.

Verifies the `--no-rules-sync` and `--force-rules` flags reach the preflight
helper. We do NOT spin up a full session store here — we assert wiring by
invoking the shared `_run_rules_preflight` bridge from cmd_ops and by
inspecting the typer command's declared options.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import typer

from sessionfs.cli import cmd_ops


def _resume_params() -> dict:
    """Return {param_name: default_or_Option} for the resume command."""
    sig = inspect.signature(cmd_ops.resume)
    return {name: p.default for name, p in sig.parameters.items()}


def test_resume_declares_no_rules_sync_and_force_rules_options():
    """Typer wiring: both flags exist on `sfs resume`."""
    params = _resume_params()
    assert "no_rules_sync" in params
    assert "force_rules" in params

    # Each should be a typer.Option with a bool default of False.
    for name in ("no_rules_sync", "force_rules"):
        opt = params[name]
        assert isinstance(opt, typer.models.OptionInfo), (
            f"{name} should be a typer.Option, got {type(opt)}"
        )
        assert opt.default is False
        # Flag name includes the dashed form.
        dashed = "--no-rules-sync" if name == "no_rules_sync" else "--force-rules"
        assert dashed in (opt.param_decls or []), (
            f"{name} missing dashed form {dashed}; got {opt.param_decls}"
        )


def test_run_rules_preflight_passes_skip_flag_through():
    """`--no-rules-sync` wiring: _run_rules_preflight(skip=True) reaches the helper."""
    with patch(
        "sessionfs.cli.resume_rules_sync.preflight_target_tool_rules"
    ) as mock_pre:
        mock_pre.return_value = type(
            "R",
            (),
            {
                "applied": False,
                "skipped_reason": "no-rules-sync",
                "target_tool": "claude-code",
                "filename": "CLAUDE.md",
                "source_rules_version": None,
                "source_rules_source": None,
                "current_rules_version": None,
                "used_force": False,
                "warning": None,
            },
        )()
        cmd_ops._run_rules_preflight(
            project_path="/tmp/x",
            target_tool="claude-code",
            source_manifest={"instruction_provenance": {"rules_version": 3, "rules_source": "sessionfs"}},
            skip=True,
            force=False,
        )
    mock_pre.assert_called_once()
    kwargs = mock_pre.call_args.kwargs
    assert kwargs["skip"] is True
    assert kwargs["force"] is False
    assert kwargs["target_tool"] == "claude-code"


def test_run_rules_preflight_passes_force_flag_through():
    """`--force-rules` wiring: force=True reaches the helper."""
    with patch(
        "sessionfs.cli.resume_rules_sync.preflight_target_tool_rules"
    ) as mock_pre:
        mock_pre.return_value = type(
            "R",
            (),
            {
                "applied": True,
                "skipped_reason": None,
                "target_tool": "claude-code",
                "filename": "CLAUDE.md",
                "source_rules_version": 3,
                "source_rules_source": "sessionfs",
                "current_rules_version": 5,
                "used_force": True,
                "warning": None,
            },
        )()
        cmd_ops._run_rules_preflight(
            project_path="/tmp/x",
            target_tool="claude-code",
            source_manifest={},
            skip=False,
            force=True,
        )
    assert mock_pre.call_args.kwargs["force"] is True
    assert mock_pre.call_args.kwargs["skip"] is False


def test_run_rules_preflight_never_raises_on_helper_exception():
    """Preflight failures must never bubble up — resume continues regardless."""
    with patch(
        "sessionfs.cli.resume_rules_sync.preflight_target_tool_rules",
        side_effect=RuntimeError("bad"),
    ):
        # Must not raise.
        cmd_ops._run_rules_preflight(
            project_path="/tmp/x",
            target_tool="claude-code",
            source_manifest={},
            skip=False,
            force=False,
        )
