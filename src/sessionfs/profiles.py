"""Named auth profiles — multi-account identity resolution.

tk_457d060822bc48c0. Before this module, two code paths resolved the
cloud identity and DISAGREED:

  - ``cli/cmd_rules.py:_get_api_config`` — env var ``SESSIONFS_API_KEY``
    > ``config.toml``. Used by ticket / persona / agent / rules / keys
    / admin commands.
  - ``cli/cmd_cloud.py:_load_sync_config`` — ``config.toml`` only,
    ignored the env var. Used by push / pull / sync / delete / project
    and the daemon.

So ``export SESSIONFS_API_KEY=<other>`` made a terminal act as one
account for coordination commands but another for sync. This module is
the SINGLE resolver both paths now call, so every command in a shell
resolves to the same identity.

## Storage layout

  - ``~/.sessionfs/config.toml`` — the ``default`` profile. Existing
    single-account installs keep working untouched: their config.toml
    IS the default profile, no migration.
  - ``~/.sessionfs/profiles/<name>.toml`` — a named profile. Holds a
    ``[sync]`` block (api_url, api_key, enabled) and an optional
    top-level ``store_dir``.
  - ``~/.sessionfs/active_profile`` — one line naming the persisted
    active profile (absent ⇒ ``default``).

## Precedence (highest first)

  1. ``SESSIONFS_API_KEY`` (+ optional ``SESSIONFS_API_URL``) — an
     ephemeral profile. Keeps CI integrations + existing docs working.
  2. ``SESSIONFS_PROFILE`` env var — a per-shell active-profile
     override (the clean replacement for the per-command env-key hack).
  3. The persisted ``active_profile`` file.
  4. ``default`` (config.toml).

Profile files are written ``chmod 600`` — they hold raw API keys, same
sensitivity as the config.toml ``[sync]`` block. No key is ever logged.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_API_URL = "https://api.sessionfs.dev"
DEFAULT_PROFILE = "default"

# Profile names map to filenames, so keep them filesystem-safe and
# predictable: lowercase alnum, dash, underscore. "default" is reserved
# for config.toml.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _sessionfs_dir() -> Path:
    """Root config dir. Honors HOME so a separate HOME still isolates."""
    return Path.home() / ".sessionfs"


def config_toml_path() -> Path:
    """Path to the default profile's config.toml."""
    return _sessionfs_dir() / "config.toml"


def profiles_dir() -> Path:
    return _sessionfs_dir() / "profiles"


def active_profile_path() -> Path:
    return _sessionfs_dir() / "active_profile"


def is_valid_profile_name(name: str) -> bool:
    return bool(_PROFILE_NAME_RE.match(name))


def profile_config_path(name: str) -> Path:
    """File backing a profile. ``default`` ⇒ config.toml (back-compat)."""
    if name == DEFAULT_PROFILE:
        return config_toml_path()
    return profiles_dir() / f"{name}.toml"


@dataclass
class ResolvedAuth:
    """The resolved cloud identity for the current command.

    ``source`` is one of: 'env_key' (SESSIONFS_API_KEY), 'profile'
    (named or default profile file). ``profile_name`` is the profile
    that supplied it, or None for the ephemeral env-key path.
    """

    api_url: str
    api_key: str
    source: str
    profile_name: str | None


def resolve_active_profile_name() -> str:
    """Active profile name by precedence (excluding the env-KEY path).

    SESSIONFS_PROFILE > persisted active_profile file > 'default'.

    Names from the env var or the persisted file are VALIDATED here
    (not just at `sfs auth use` / `login` time): a hand-edited
    active_profile or a hostile ``SESSIONFS_PROFILE=../bad`` must not
    flow into ``profile_config_path`` and point key resolution at a
    path outside ``profiles/``. Invalid names are ignored and we fall
    through to the next precedence tier (ultimately 'default'). Codex
    R1 LOW on tk_457d060822bc48c0.
    """
    env_profile = os.environ.get("SESSIONFS_PROFILE")
    if env_profile:
        env_profile = env_profile.strip()
        if is_valid_profile_name(env_profile):
            return env_profile
        import logging
        logging.getLogger("sessionfs.profiles").warning(
            "Ignoring invalid SESSIONFS_PROFILE=%r (not a valid profile "
            "name); falling back to the persisted/default profile.",
            env_profile,
        )
    apath = active_profile_path()
    if apath.exists():
        try:
            name = apath.read_text().strip()
            if name and is_valid_profile_name(name):
                return name
            if name:
                import logging
                logging.getLogger("sessionfs.profiles").warning(
                    "Ignoring invalid active_profile %r; using 'default'.",
                    name,
                )
        except OSError:
            pass
    return DEFAULT_PROFILE


def _read_profile_sync(name: str) -> tuple[str, str]:
    """Return (api_url, api_key) from a profile file. Missing ⇒ ('', '')."""
    path = profile_config_path(name)
    if not path.exists():
        return (DEFAULT_API_URL, "")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return (DEFAULT_API_URL, "")
    sync = raw.get("sync", {}) if isinstance(raw, dict) else {}
    api_url = sync.get("api_url") or DEFAULT_API_URL
    api_key = sync.get("api_key") or ""
    return (api_url, api_key)


def resolve_auth() -> ResolvedAuth:
    """The single auth resolver. Both _get_api_config and
    _load_sync_config (and the daemon) flow through this.

    Precedence: SESSIONFS_API_KEY > SESSIONFS_PROFILE > persisted
    active_profile > default. Never raises — callers decide what an
    empty api_key means (interactive error vs. daemon disabled-sync).
    """
    env_key = os.environ.get("SESSIONFS_API_KEY")
    if env_key:
        env_url = os.environ.get("SESSIONFS_API_URL") or DEFAULT_API_URL
        return ResolvedAuth(
            api_url=env_url.rstrip("/"),
            api_key=env_key,
            source="env_key",
            profile_name=None,
        )

    name = resolve_active_profile_name()
    api_url, api_key = _read_profile_sync(name)
    return ResolvedAuth(
        api_url=api_url.rstrip("/"),
        api_key=api_key,
        source="profile",
        profile_name=name,
    )


def resolve_store_dir() -> Path:
    """Per-profile session-store directory for daemon + sync paths.

    The default profile keeps ``~/.sessionfs`` (back-compat). A named
    profile defaults to ``~/.sessionfs/profiles/<name>/store`` unless it
    sets ``store_dir`` explicitly. The env-key ephemeral path uses the
    default store (it's a transient identity, not a persisted profile).
    """
    env_key = os.environ.get("SESSIONFS_API_KEY")
    if env_key:
        return _sessionfs_dir()

    name = resolve_active_profile_name()
    if name == DEFAULT_PROFILE:
        return _sessionfs_dir()

    path = profile_config_path(name)
    if path.exists():
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            store = raw.get("store_dir") if isinstance(raw, dict) else None
            if isinstance(store, str) and store:
                return Path(store).expanduser()
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return profiles_dir() / name / "store"


def list_profiles() -> list[str]:
    """All known profile names. Always includes 'default' if config.toml
    exists; plus every ``profiles/*.toml`` stem."""
    names: list[str] = []
    if config_toml_path().exists():
        names.append(DEFAULT_PROFILE)
    pdir = profiles_dir()
    if pdir.exists():
        for child in sorted(pdir.glob("*.toml")):
            names.append(child.stem)
    return names


def _write_private_file(path: Path, content: str) -> None:
    """Create/overwrite ``path`` with 0o600 enforced AT CREATION.

    Shield-SR v0.10.29: ``write_text()`` + ``os.chmod()`` leaves a window
    (or, if the process dies in between, a permanent state) where the
    file exists with umask-default permissions — typically 0o644, world-
    readable — while holding a raw API key. ``os.open`` with an explicit
    mode applies 0o600 atomically at creation. The trailing chmod also
    tightens a pre-existing file that may have looser permissions.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def set_active_profile(name: str) -> None:
    """Persist the active profile pointer (chmod 600 on the dir's file)."""
    d = _sessionfs_dir()
    d.mkdir(parents=True, exist_ok=True)
    apath = active_profile_path()
    _write_private_file(apath, name + "\n")


def write_named_profile(name: str, api_url: str, api_key: str) -> Path:
    """Write a named-profile TOML file (NOT the default config.toml).

    The default profile is written through cmd_cloud._save_sync_config
    so its watcher sections etc. are preserved. Named profiles are
    self-contained: a [sync] block + a profile-scoped store_dir.
    """
    if name == DEFAULT_PROFILE:
        raise ValueError(
            "write_named_profile must not be used for the default profile; "
            "use _save_sync_config (config.toml) instead."
        )
    pdir = profiles_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    store_dir = pdir / name / "store"
    path = profile_config_path(name)
    content = (
        f"# SessionFS profile: {name}\n"
        f"# Written by `sfs auth login --profile {name}`.\n"
        f'store_dir = "{store_dir}"\n'
        "\n"
        "[sync]\n"
        "enabled = true\n"
        f'api_url = "{api_url}"\n'
        f'api_key = "{api_key}"\n'
        "push_interval = 30\n"
        "retry_max = 5\n"
    )
    # 0o600 enforced at creation — holds a raw key (Shield-SR v0.10.29:
    # write_text + chmod left a umask-default window; see _write_private_file).
    _write_private_file(path, content)
    return path
