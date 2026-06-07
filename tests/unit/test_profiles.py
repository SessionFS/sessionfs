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
