"""Preflighted, atomic, per-target MCP config edits shared by every `sfs mcp install` target."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from sessionfs.cli import mcp_config_io as cfg

ENTRY = {"command": "/usr/local/bin/sfs", "args": ["mcp", "serve"]}


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_install_keeps_permissions_and_other_servers(tmp_path: Path):
    from sessionfs.cli.cmd_mcp import _install_json_config

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    os.chmod(path, 0o600)
    inode = os.lstat(path).st_ino
    _install_json_config(path, ENTRY, "Tool")
    data = json.loads(path.read_text())
    assert data == {"mcpServers": {"other": {"command": "x"}, "sessionfs": ENTRY}, "theme": "dark"}
    assert _mode(path) == 0o600
    assert os.lstat(path).st_ino != inode  # replaced, not rewritten in place
    assert not list(tmp_path.glob(".mcp.json.*.tmp"))


def test_preflight_failure_changes_nothing(tmp_path: Path):
    from sessionfs.cli.cmd_mcp import _install_json_configs

    good = tmp_path / "a.json"
    good.write_text(json.dumps({"mcpServers": {}}))
    bad = tmp_path / "b.json"
    bad.write_text("{not json")
    before = good.read_text()
    with pytest.raises(SystemExit):
        _install_json_configs([good, bad], ENTRY, "Tool")
    assert good.read_text() == before
    assert bad.read_text() == "{not json"


def test_unwritable_file_refused_before_any_write(tmp_path: Path):
    good = tmp_path / "a.json"
    good.write_text("{}")
    locked = tmp_path / "b.json"
    locked.write_text("{}")
    os.chmod(locked, 0o400)
    try:
        with pytest.raises(cfg.ConfigFileError):
            [cfg.plan_install(p, "sessionfs", ENTRY, "Tool") for p in (good, locked)]
    finally:
        os.chmod(locked, 0o600)
    assert good.read_text() == "{}"


def test_symlinked_config_edits_the_target_and_keeps_the_link(tmp_path: Path):
    from sessionfs.cli.cmd_mcp import _install_json_config

    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    _install_json_config(link, ENTRY, "Tool")
    assert link.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["sessionfs"] == ENTRY


def test_symlink_to_non_file_is_refused(tmp_path: Path):
    target_dir = tmp_path / "somedir"
    target_dir.mkdir()
    link = tmp_path / "mcp.json"
    link.symlink_to(target_dir)
    with pytest.raises(cfg.ConfigFileError):
        cfg.plan_install(link, "sessionfs", ENTRY, "Tool")


def test_concurrent_modification_is_merged_on_retry(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    plan = cfg.plan_install(path, "sessionfs", ENTRY, "Tool")
    # Another program edits the file after our preflight.
    path.write_text(json.dumps({"mcpServers": {"theirs": {"command": "y"}}}))
    results = cfg.apply_plans(
        [plan], lambda p: cfg.plan_install(p.path, "sessionfs", ENTRY, "Tool"),
    )
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {"theirs": {"command": "y"}, "sessionfs": ENTRY}
    assert results[0].ok and results[0].plan.action == "added"


def test_targets_are_independent_and_each_result_reported(tmp_path: Path, monkeypatch):
    """No cross-file rollback: a target that keeps changing is left exactly
    as it is and reported; the other target keeps its change."""
    first = tmp_path / "a.json"
    first.write_text("{}")
    second = tmp_path / "b.json"
    second.write_text("{}")
    plans = [cfg.plan_install(p, "sessionfs", ENTRY, "Tool") for p in (first, second)]
    real_snapshot = cfg._snapshot
    ticks = iter(range(10_000))

    def always_changed(target):
        snap = real_snapshot(target)
        if Path(target) == second and snap is not None:
            return (snap[0], snap[1], snap[2], f"someone-else-{next(ticks)}")
        return snap

    monkeypatch.setattr(cfg, "_snapshot", always_changed)
    results = cfg.apply_plans(plans, lambda p: cfg.plan_install(p.path, "sessionfs", ENTRY, "Tool"))
    assert [r.ok for r in results] == [True, False]
    assert "kept changing" in (results[1].error or "")
    assert json.loads(first.read_text())["mcpServers"]["sessionfs"] == ENTRY
    assert second.read_text() == "{}"


def test_partial_success_is_reported_by_the_cli(tmp_path: Path, monkeypatch, capsys):
    from sessionfs.cli.cmd_mcp import _install_json_configs

    first = tmp_path / "a.json"
    first.write_text("{}")
    second = tmp_path / "b.json"
    second.write_text("{}")
    real_apply = cfg.apply_plan

    def fail_second(plan, replan):
        if plan.path == second:
            return cfg.ApplyResult(plan, False, error=f"{second}: disk full")
        return real_apply(plan, replan)

    monkeypatch.setattr(cfg, "apply_plan", fail_second)
    with pytest.raises(SystemExit):
        _install_json_configs([first, second], ENTRY, "Tool")
    out = capsys.readouterr()
    assert "Added SessionFS MCP server" in out.out
    assert "Not changed" in out.err and "Some config files were updated" in out.err


def test_previous_version_kept_as_timestamped_backup(tmp_path: Path):
    path = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    path.write_text(original)
    os.chmod(path, 0o600)
    plan = cfg.plan_install(path, "sessionfs", ENTRY, "Tool")
    result = cfg.apply_plan(plan, lambda p: cfg.plan_install(p.path, "sessionfs", ENTRY, "Tool"))
    assert result.ok and result.backup is not None
    assert result.backup.read_text() == original
    assert result.backup.name.startswith("mcp.json.sessionfs-backup-")
    assert _mode(result.backup) == 0o600


def test_old_backups_are_pruned_and_user_files_never_deleted(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text("{}")
    keep = tmp_path / "mcp.json.bak"  # the user's own backup: never touched
    keep.write_text("mine")
    for i in range(6):
        entry = {"command": f"/bin/sfs{i}", "args": ["mcp", "serve"]}
        plan = cfg.plan_install(path, "sessionfs", entry, "Tool")
        assert cfg.apply_plan(plan, lambda p, e=entry: cfg.plan_install(p.path, "sessionfs", e, "Tool")).ok
    backups = sorted(tmp_path.glob("mcp.json.sessionfs-backup-*"))
    assert len(backups) == cfg.KEEP_BACKUPS
    assert keep.read_text() == "mine"


def test_config_path_always_holds_a_complete_file(tmp_path: Path, monkeypatch):
    """The write is a rename over the file: at no point is the path absent."""
    path = tmp_path / "mcp.json"
    path.write_text("{}")
    real_replace = os.replace
    seen: list[bool] = []

    def checking_replace(src, dst):
        seen.append(Path(dst).exists())
        return real_replace(src, dst)

    monkeypatch.setattr(cfg.os, "replace", checking_replace)
    real_rename = os.rename
    monkeypatch.setattr(cfg.os, "rename", lambda *a: pytest.fail("the config must not be moved aside"))
    plan = cfg.plan_install(path, "sessionfs", ENTRY, "Tool")
    assert cfg.apply_plan(plan, lambda p: p).ok
    monkeypatch.setattr(cfg.os, "rename", real_rename)
    assert seen == [True]
    assert json.loads(path.read_text())["mcpServers"]["sessionfs"] == ENTRY


def test_uninstall_is_atomic_and_reports(tmp_path: Path, capsys):
    from sessionfs.cli.cmd_mcp import _uninstall_json_config

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"sessionfs": ENTRY, "other": {"command": "x"}}}))
    os.chmod(path, 0o640)
    _uninstall_json_config(path, "Tool")
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "x"}}}
    assert _mode(path) == 0o640
    _uninstall_json_config(path, "Tool")
    _uninstall_json_config(tmp_path / "missing.json", "Tool")
    out = capsys.readouterr().out
    assert "Removed SessionFS" in out and "Nothing to remove" in out


def test_uninstall_unparseable_raises_and_leaves_file(tmp_path: Path):
    from sessionfs.cli.cmd_mcp import _uninstall_json_config

    path = tmp_path / "mcp.json"
    path.write_text("{bad")
    with pytest.raises(RuntimeError):
        _uninstall_json_config(path, "Tool")
    assert path.read_text() == "{bad"


def test_new_file_never_replaces_one_created_concurrently(tmp_path: Path):
    path = tmp_path / "mcp.json"
    plan = cfg.plan_install(path, "sessionfs", ENTRY, "Tool")
    assert plan.original is None
    path.write_text(json.dumps({"mcpServers": {"theirs": {"command": "y"}}}))
    results = cfg.apply_plans([plan], lambda p: cfg.plan_install(p.path, "sessionfs", ENTRY, "Tool"))
    assert results[0].ok
    assert json.loads(path.read_text())["mcpServers"] == {"theirs": {"command": "y"}, "sessionfs": ENTRY}


def test_backup_holds_the_bytes_actually_replaced(tmp_path: Path, monkeypatch):
    """Even if a change slipped past the snapshot check, the backup is what was
    on disk right before the replace — not the version planned from."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    plan = cfg.plan_install(path, "sessionfs", ENTRY, "Tool")
    external = json.dumps({"mcpServers": {"theirs": {"command": "late"}}})
    path.write_text(external)
    monkeypatch.setattr(cfg, "_snapshot", lambda target: plan.snapshot)  # check misses it
    result = cfg.apply_plan(plan, lambda p: p)
    assert result.ok and result.backup is not None
    assert result.backup.read_text() == external
