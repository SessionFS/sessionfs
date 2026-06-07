"""Named auth profile resolution — tk_457d060822bc48c0.

Covers the precedence chain, the unified resolver that BOTH the
coordination commands (_get_api_config) and the sync commands
(_load_sync_config) now flow through, per-profile store isolation,
and backward compatibility with a pre-existing single-account
config.toml.
"""

from __future__ import annotations

import importlib
import stat

import pytest


@pytest.fixture
def sfs_home(tmp_path, monkeypatch):
    """Redirect HOME so ~/.sessionfs resolves into tmp, and clear the
    env-var overrides so tests start from a known state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SESSIONFS_API_KEY", raising=False)
    monkeypatch.delenv("SESSIONFS_API_URL", raising=False)
    monkeypatch.delenv("SESSIONFS_PROFILE", raising=False)
    # profiles.py reads Path.home() at call time, so no reload needed.
    import sessionfs.profiles as profiles
    importlib.reload(profiles)
    (tmp_path / ".sessionfs").mkdir(parents=True, exist_ok=True)
    return tmp_path / ".sessionfs"


def _write_default_config(sfs_dir, api_key="key_default", api_url="https://api.sessionfs.dev"):
    (sfs_dir / "config.toml").write_text(
        "[sync]\n"
        "enabled = true\n"
        f'api_url = "{api_url}"\n'
        f'api_key = "{api_key}"\n'
    )


# ── precedence chain ──


def test_default_profile_from_config_toml(sfs_home):
    from sessionfs.profiles import resolve_auth

    _write_default_config(sfs_home, api_key="key_default")
    auth = resolve_auth()
    assert auth.api_key == "key_default"
    assert auth.source == "profile"
    assert auth.profile_name == "default"


def test_env_key_beats_everything(sfs_home, monkeypatch):
    from sessionfs.profiles import resolve_auth

    _write_default_config(sfs_home, api_key="key_default")
    monkeypatch.setenv("SESSIONFS_API_KEY", "key_env")
    monkeypatch.setenv("SESSIONFS_PROFILE", "work")
    auth = resolve_auth()
    assert auth.api_key == "key_env"
    assert auth.source == "env_key"
    assert auth.profile_name is None


def test_env_profile_beats_persisted_active(sfs_home, monkeypatch):
    from sessionfs.profiles import resolve_auth, set_active_profile, write_named_profile

    _write_default_config(sfs_home, api_key="key_default")
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    write_named_profile("client", "https://api.sessionfs.dev", "key_client")
    set_active_profile("work")  # persisted active = work
    monkeypatch.setenv("SESSIONFS_PROFILE", "client")  # env override = client
    auth = resolve_auth()
    assert auth.api_key == "key_client"
    assert auth.profile_name == "client"


def test_persisted_active_beats_default(sfs_home):
    from sessionfs.profiles import resolve_auth, set_active_profile, write_named_profile

    _write_default_config(sfs_home, api_key="key_default")
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    set_active_profile("work")
    auth = resolve_auth()
    assert auth.api_key == "key_work"
    assert auth.profile_name == "work"


def test_falls_back_to_default_when_no_active(sfs_home):
    from sessionfs.profiles import resolve_auth

    _write_default_config(sfs_home, api_key="key_default")
    auth = resolve_auth()
    assert auth.profile_name == "default"


# ── the unification — AC #3: both helpers resolve to the SAME key ──


def test_get_api_config_and_load_sync_config_agree(sfs_home, monkeypatch):
    """The heart of the ticket: a coordination-command resolver
    (_get_api_config) and a sync-command resolver (_load_sync_config)
    must return the same api_key for the active profile."""
    from sessionfs.profiles import set_active_profile, write_named_profile

    _write_default_config(sfs_home, api_key="key_default")
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    set_active_profile("work")

    from sessionfs.cli.cmd_rules import _get_api_config
    from sessionfs.cli.cmd_cloud import _load_sync_config

    _, coord_key = _get_api_config()
    sync_cfg = _load_sync_config()
    assert coord_key == "key_work"
    assert sync_cfg["api_key"] == "key_work"
    assert coord_key == sync_cfg["api_key"]


def test_both_helpers_agree_under_env_key(sfs_home, monkeypatch):
    _write_default_config(sfs_home, api_key="key_default")
    monkeypatch.setenv("SESSIONFS_API_KEY", "key_env")

    from sessionfs.cli.cmd_rules import _get_api_config
    from sessionfs.cli.cmd_cloud import _load_sync_config

    _, coord_key = _get_api_config()
    sync_cfg = _load_sync_config()
    assert coord_key == "key_env"
    assert sync_cfg["api_key"] == "key_env"


# ── per-profile store isolation — AC #5 ──


def test_default_profile_store_is_sessionfs_root(sfs_home):
    from sessionfs.profiles import resolve_store_dir

    _write_default_config(sfs_home)
    assert resolve_store_dir() == sfs_home


def test_named_profile_store_is_isolated(sfs_home):
    from sessionfs.profiles import resolve_store_dir, set_active_profile, write_named_profile

    _write_default_config(sfs_home)
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    set_active_profile("work")
    store = resolve_store_dir()
    assert store != sfs_home
    assert "work" in str(store)


# ── backward compat — AC #7 ──


def test_preexisting_config_toml_is_default_profile(sfs_home):
    """A single-account install with only config.toml must keep working
    with zero migration — treated as the 'default' profile."""
    from sessionfs.profiles import list_profiles, resolve_auth

    _write_default_config(sfs_home, api_key="legacy_key")
    assert "default" in list_profiles()
    auth = resolve_auth()
    assert auth.api_key == "legacy_key"
    assert auth.profile_name == "default"


def test_load_config_default_path_resolves_default_profile(sfs_home):
    """daemon load_config() with no args resolves the active profile;
    with only config.toml present that's 'default' → config.toml."""
    from sessionfs.daemon.config import load_config

    _write_default_config(sfs_home, api_key="legacy_key")
    cfg = load_config()
    assert cfg.sync.api_key == "legacy_key"


def test_load_config_named_profile_gets_isolated_store(sfs_home):
    from sessionfs.daemon.config import load_config
    from sessionfs.profiles import set_active_profile, write_named_profile

    _write_default_config(sfs_home)
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    set_active_profile("work")
    cfg = load_config()
    assert cfg.sync.api_key == "key_work"
    assert "work" in str(cfg.store_dir)


# ── file permissions — Sentinel pairing ──


def test_named_profile_file_is_chmod_600(sfs_home):
    from sessionfs.profiles import profile_config_path, write_named_profile

    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    path = profile_config_path("work")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"profile file must be 0o600, got {oct(mode)}"


def test_active_profile_file_is_chmod_600(sfs_home):
    from sessionfs.profiles import active_profile_path, set_active_profile

    set_active_profile("default")
    mode = stat.S_IMODE(active_profile_path().stat().st_mode)
    assert mode == 0o600


# ── name validation ──


def test_invalid_profile_names_rejected(sfs_home):
    from sessionfs.profiles import is_valid_profile_name

    assert is_valid_profile_name("work")
    assert is_valid_profile_name("client-acme")
    assert is_valid_profile_name("ci_runner_2")
    assert not is_valid_profile_name("../etc/passwd")
    assert not is_valid_profile_name("Work")  # uppercase
    assert not is_valid_profile_name("a b")   # space
    assert not is_valid_profile_name("")


def test_list_profiles_includes_named_and_default(sfs_home):
    from sessionfs.profiles import list_profiles, write_named_profile

    _write_default_config(sfs_home)
    write_named_profile("work", "https://api.sessionfs.dev", "k1")
    write_named_profile("client", "https://api.sessionfs.dev", "k2")
    names = list_profiles()
    assert "default" in names
    assert "work" in names
    assert "client" in names


# ── R1 LOW #4: invalid SESSIONFS_PROFILE / active_profile rejected ──


def test_invalid_env_profile_falls_back(sfs_home, monkeypatch):
    """A hostile SESSIONFS_PROFILE='../bad' must not flow into
    profile_config_path; resolution falls back to default."""
    from sessionfs.profiles import resolve_active_profile_name

    _write_default_config(sfs_home, api_key="key_default")
    monkeypatch.setenv("SESSIONFS_PROFILE", "../bad")
    assert resolve_active_profile_name() == "default"


def test_invalid_active_profile_file_falls_back(sfs_home):
    """A hand-edited active_profile with a bad name is ignored."""
    from sessionfs.profiles import active_profile_path, resolve_active_profile_name

    _write_default_config(sfs_home)
    active_profile_path().write_text("../../etc/passwd\n")
    assert resolve_active_profile_name() == "default"


def test_invalid_env_profile_resolve_auth_uses_default(sfs_home, monkeypatch):
    from sessionfs.profiles import resolve_auth

    _write_default_config(sfs_home, api_key="key_default")
    monkeypatch.setenv("SESSIONFS_PROFILE", "../bad")
    auth = resolve_auth()
    assert auth.api_key == "key_default"
    assert auth.profile_name == "default"


# ── R1 MED #2: delete/trash/restore share the resolver (env-key honored) ──


def test_delete_load_sync_config_honors_env_key(sfs_home, monkeypatch):
    """cmd_delete._load_sync_config must honor SESSIONFS_API_KEY like
    push/ticket — it delegates to the shared resolver now."""
    _write_default_config(sfs_home, api_key="key_default")
    monkeypatch.setenv("SESSIONFS_API_KEY", "key_env")

    from sessionfs.cli.cmd_delete import _load_sync_config as delete_cfg
    from sessionfs.cli.cmd_cloud import _load_sync_config as cloud_cfg

    assert delete_cfg()["api_key"] == "key_env"
    assert delete_cfg()["api_key"] == cloud_cfg()["api_key"]


def test_delete_resolver_agrees_with_active_profile(sfs_home):
    from sessionfs.profiles import set_active_profile, write_named_profile

    _write_default_config(sfs_home, api_key="key_default")
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")
    set_active_profile("work")

    from sessionfs.cli.cmd_delete import _load_sync_config as delete_cfg

    assert delete_cfg()["api_key"] == "key_work"


# ── R1 MED #3: two profiles do not share deleted.json ──


def test_two_profiles_have_isolated_deleted_json(sfs_home):
    """A deletion recorded under the default profile's store must NOT be
    visible from a named profile's store dir, and vice versa."""
    from sessionfs.profiles import resolve_store_dir, set_active_profile, write_named_profile
    from sessionfs.store import deleted as d

    _write_default_config(sfs_home)
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")

    # Default profile active → store dir = ~/.sessionfs.
    default_store = resolve_store_dir()
    d.mark_deleted("ses_default_only", "cloud", reason="too_large", base_dir=default_store)

    # Switch to 'work' → its own store dir.
    set_active_profile("work")
    work_store = resolve_store_dir()
    assert work_store != default_store
    d.mark_deleted("ses_work_only", "cloud", reason="too_large", base_dir=work_store)

    # Each store sees only its own entry.
    default_entries = d.list_deleted(base_dir=default_store)
    work_entries = d.list_deleted(base_dir=work_store)
    assert "ses_default_only" in default_entries
    assert "ses_default_only" not in work_entries
    assert "ses_work_only" in work_entries
    assert "ses_work_only" not in default_entries


# ── R1 HIGH #1: daemon reload does not silently switch profile ──


def test_daemon_reload_does_not_switch_profile_mid_run(sfs_home, monkeypatch):
    """A daemon started under the default profile must keep the default
    profile's sync key after a reload, even if the active profile was
    changed to a different one in the meantime."""
    from sessionfs.daemon.config import DaemonConfig
    from sessionfs.daemon.main import Daemon
    from sessionfs.profiles import set_active_profile, write_named_profile

    _write_default_config(sfs_home, api_key="key_default")
    write_named_profile("work", "https://api.sessionfs.dev", "key_work")

    # Build a daemon config carrying the default profile's key, pinned to
    # 'default'. (We construct DaemonConfig directly + set the syncer key
    # so we don't depend on the watcher subsystem.)
    cfg = DaemonConfig(sync={"enabled": True, "api_key": "key_default",
                             "api_url": "https://api.sessionfs.dev"})
    daemon = Daemon(cfg)
    assert daemon._pinned_profile == "default"

    # Operator switches the active profile to 'work' between SIGHUPs.
    set_active_profile("work")

    # Reload must NOT adopt profile 'work's key — pinned to default.
    daemon._reload_config()
    assert daemon.config.sync.api_key == "key_default", (
        "daemon reload must not switch identity mid-run"
    )
