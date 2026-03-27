"""Daemon configuration management.

Loads from ~/.sessionfs/config.toml with sensible defaults.
Creates default config file on first run.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_CONFIG_DIR = Path.home() / ".sessionfs"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


class ClaudeCodeWatcherConfig(BaseModel):
    """Claude Code watcher configuration."""

    enabled: bool = True
    home_dir: Path = Field(default_factory=lambda: Path.home() / ".claude")


class CodexWatcherConfig(BaseModel):
    """Codex CLI watcher configuration (future)."""

    enabled: bool = False
    home_dir: Path = Field(default_factory=lambda: Path.home() / ".codex")


class GeminiWatcherConfig(BaseModel):
    """Gemini CLI watcher configuration."""

    enabled: bool = False
    home_dir: Path = Field(default_factory=lambda: Path.home() / ".gemini")


def _default_cursor_global_db() -> Path:
    import platform
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _default_cursor_workspace_storage() -> Path:
    import platform
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
    return Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage"


def _default_amp_data_dir() -> Path:
    import os
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "amp"
    return Path.home() / ".local" / "share" / "amp"


class CopilotWatcherConfig(BaseModel):
    """Copilot CLI watcher configuration."""

    enabled: bool = False
    home_dir: Path = Field(default_factory=lambda: Path.home() / ".copilot")


class AmpWatcherConfig(BaseModel):
    """Amp watcher configuration."""

    enabled: bool = False
    data_dir: Path = Field(default_factory=_default_amp_data_dir)


class CursorWatcherConfig(BaseModel):
    """Cursor IDE watcher configuration."""

    enabled: bool = False
    global_db_path: Path = Field(default_factory=_default_cursor_global_db)
    workspace_storage_path: Path = Field(default_factory=_default_cursor_workspace_storage)


def _default_cline_storage() -> Path:
    import platform
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    return Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"


def _default_roo_code_storage() -> Path:
    import platform
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    return Path.home() / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"


class ClineWatcherConfig(BaseModel):
    """Cline VS Code extension watcher configuration."""

    enabled: bool = False
    storage_dir: Path = Field(default_factory=_default_cline_storage)


class RooCodeWatcherConfig(BaseModel):
    """Roo Code VS Code extension watcher configuration."""

    enabled: bool = False
    storage_dir: Path = Field(default_factory=_default_roo_code_storage)


class StoragePolicyConfig(BaseModel):
    """Local storage retention policy."""

    max_local_storage: str = "2GB"
    local_retention_days: int = 90
    synced_retention_days: int = 30
    preserve_bookmarked: bool = True
    preserve_aliased: bool = True


class SyncConfig(BaseModel):
    """Cloud sync configuration."""

    enabled: bool = False
    api_url: str = "https://api.sessionfs.dev"
    api_key: str = ""
    push_interval: int = 30  # seconds between sync pushes
    retry_max: int = 5  # max consecutive failures before degraded


class JudgeConfig(BaseModel):
    """Judge LLM configuration."""

    api_key: str = ""
    base_url: str = ""


class DaemonConfig(BaseModel):
    """Top-level daemon configuration."""

    store_dir: Path = Field(default_factory=lambda: Path.home() / ".sessionfs")
    log_level: str = "INFO"
    scan_interval_s: float = 5.0
    claude_code: ClaudeCodeWatcherConfig = Field(default_factory=ClaudeCodeWatcherConfig)
    codex: CodexWatcherConfig = Field(default_factory=CodexWatcherConfig)
    gemini: GeminiWatcherConfig = Field(default_factory=GeminiWatcherConfig)
    copilot: CopilotWatcherConfig = Field(default_factory=CopilotWatcherConfig)
    cursor: CursorWatcherConfig = Field(default_factory=CursorWatcherConfig)
    cline: ClineWatcherConfig = Field(default_factory=ClineWatcherConfig)
    roo_code: RooCodeWatcherConfig = Field(default_factory=RooCodeWatcherConfig)
    amp: AmpWatcherConfig = Field(default_factory=AmpWatcherConfig)
    storage: StoragePolicyConfig = Field(default_factory=StoragePolicyConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> DaemonConfig:
    """Load config from TOML file. Returns defaults if file doesn't exist."""
    if not config_path.exists():
        return DaemonConfig()

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    return DaemonConfig.model_validate(raw)


def default_config_toml() -> str:
    """Generate default config TOML content."""
    return """\
# SessionFS Daemon Configuration

# Log level: DEBUG, INFO, WARNING, ERROR
log_level = "INFO"

# Seconds to wait after a filesystem event before re-scanning
scan_interval_s = 5.0

[claude_code]
enabled = true
# home_dir = "~/.claude"

[codex]
enabled = false
# home_dir = "~/.codex"

[gemini]
enabled = false
# home_dir = "~/.gemini"

[copilot]
enabled = false
# home_dir = "~/.copilot"

[cline]
enabled = false
# storage_dir = "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev"

[roo_code]
enabled = false
# storage_dir = "~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline"

[amp]
enabled = false
# data_dir = "~/.local/share/amp"

[storage]
max_local_storage = "2GB"                # Maximum local storage for .sfs sessions
local_retention_days = 90                # Delete unsynced sessions older than this
synced_retention_days = 30               # Delete synced sessions older than this
preserve_bookmarked = true               # Never auto-prune bookmarked sessions
preserve_aliased = true                  # Never auto-prune aliased sessions

[sync]
enabled = false                          # Must be explicitly enabled
api_url = "https://api.sessionfs.dev"    # Server URL
api_key = ""                             # Set by `sfs auth login`
push_interval = 30                       # Seconds between sync pushes (daemon)
retry_max = 5                            # Max consecutive failures before degraded
"""


def ensure_config(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Create default config file if it doesn't exist."""
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(default_config_toml())
