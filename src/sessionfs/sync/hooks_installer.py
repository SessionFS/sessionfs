"""SessionStart hook installation/removal for Claude Code's settings.json.

Claude Code reads ``~/.claude/settings.json`` (user scope) and
``.claude/settings.json`` (project scope). The ``hooks.SessionStart`` slot
holds an array of "matcher entries"; each entry contains a list of hook
commands. When a session starts, Claude Code runs every matched command and
captures its stdout, allowing the command to inject additional system context.

SessionFS uses this to wire compiled project rules into Claude Code without
requiring a CLAUDE.md file. The installed entry carries a sentinel marker
``"sfs:managed": true`` so we can find and remove only our own entries when
uninstalling — user-defined hooks are preserved untouched.

The module exposes three pure functions:

- :func:`install_session_start_hook` — idempotently merge our SessionStart
  entry into the file at ``settings_path``. Creates the file (and parent
  directory) if missing. Returns ``True`` if the file changed.
- :func:`uninstall_session_start_hook` — remove ONLY entries with our
  sentinel; preserves user hooks. Returns ``True`` if the file changed.
- :func:`is_hook_installed` — quick read-only check used by ``sfs hooks status``.

A malformed settings.json is surfaced as :class:`MalformedSettingsError` so
callers can render a clear error rather than crashing or silently
overwriting user data.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Sentinel marker we attach to SessionFS-managed hook entries. Must be stable
# across releases — uninstall searches for this exact key.
SFS_MANAGED_KEY = "sfs:managed"


class MalformedSettingsError(Exception):
    """Raised when settings.json exists but cannot be parsed as a JSON object."""


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_EXPECTED_COMMAND_PREFIX = "sfs rules emit "


def _is_managed_entry(entry: Any) -> bool:
    """True iff ``entry`` is a SessionFS-managed SessionStart matcher entry.

    Verifies BOTH the sentinel marker AND that the embedded command is
    actually ``sfs rules emit ...`` — protecting against unrelated entries
    that happen to share the sentinel key.
    """
    if not isinstance(entry, dict) or entry.get(SFS_MANAGED_KEY) is not True:
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for h in hooks:
        if not isinstance(h, dict):
            continue
        cmd = h.get("command", "")
        if isinstance(cmd, str) and cmd.startswith(_EXPECTED_COMMAND_PREFIX):
            return True
    return False


@contextmanager
def _locked(settings_path: Path):
    """Hold an exclusive flock on a sibling lock file for the duration.

    Prevents concurrent install/uninstall (or any other process using the
    same lock convention) from clobbering each other's writes. Lock file
    is at ``<settings_path>.sfs-lock`` and is created if missing. The lock
    is advisory — external editors that don't honour it can still race,
    but our own CLI invocations are race-safe against each other.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = settings_path.with_suffix(settings_path.suffix + ".sfs-lock")
    with open(lock_path, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _read_settings(settings_path: Path) -> dict[str, Any]:
    """Return the parsed settings dict, or ``{}`` if the file is missing.

    Raises :class:`MalformedSettingsError` if the file exists but cannot be
    parsed as a JSON object. We refuse to silently overwrite a corrupt user
    settings file — the caller surfaces a clear error and exits.
    """
    if not settings_path.exists():
        return {}
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedSettingsError(f"could not read {settings_path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedSettingsError(
            f"settings.json at {settings_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MalformedSettingsError(
            f"settings.json at {settings_path} must be a JSON object, "
            f"got {type(data).__name__}"
        )
    return data


def _write_settings(settings_path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``settings_path``, creating parent dirs as needed.

    Uses 2-space indentation to match the Claude Code convention and
    appends a trailing newline so editors don't flag the file as missing one.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _matcher_entries_for_session_start(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the existing list of SessionStart matcher entries.

    If ``hooks`` is missing, malformed, or ``SessionStart`` is missing, an
    empty list is returned. The caller is expected to set the entries back
    via the returned reference (we mutate in place where possible) but never
    relies on aliasing — we always re-assign the slot before writing.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get("SessionStart")
    if not isinstance(entries, list):
        return []
    return entries


def _build_managed_entry(command: str) -> dict[str, Any]:
    """Construct the SessionFS-managed matcher entry shape Claude Code expects."""
    return {
        SFS_MANAGED_KEY: True,
        "hooks": [{"type": "command", "command": command}],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_hook_installed(settings_path: Path) -> bool:
    """Return True iff a SessionFS-managed SessionStart entry is present.

    Missing or malformed files are treated as "not installed" — callers
    that want a hard error should call :func:`_read_settings` directly.
    """
    try:
        data = _read_settings(settings_path)
    except MalformedSettingsError:
        return False
    for entry in _matcher_entries_for_session_start(data):
        if _is_managed_entry(entry):
            return True
    return False


def install_session_start_hook(settings_path: Path, command: str) -> bool:
    """Idempotently install a SessionFS-managed SessionStart hook.

    Behaviour:

    - missing file: create it (and parent dir) with the hook
    - malformed file: raises :class:`MalformedSettingsError`
    - already-installed identical entry: no-op (returns ``False``)
    - already-installed entry with stale command: command is updated in place
    - all unrelated keys (hooks for other events, user-defined hook entries)
      are preserved untouched

    Returns ``True`` if the file was modified, ``False`` otherwise. Callers
    use the return value to decide whether to print "installed" vs.
    "already installed".
    """
    with _locked(settings_path):
        return _install_locked(settings_path, command)


def _install_locked(settings_path: Path, command: str) -> bool:
    data = _read_settings(settings_path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        # Refuse to clobber a user-set non-dict value at "hooks". The
        # malformed-file path is reserved for parse failures; this is a
        # type mismatch in otherwise-valid JSON, which is just as bad.
        raise MalformedSettingsError(
            f"settings.json at {settings_path}: 'hooks' must be a JSON object, "
            f"got {type(hooks).__name__}"
        )

    entries = hooks.get("SessionStart")
    if not isinstance(entries, list):
        entries = []

    # Find an existing managed entry — if multiple somehow exist (legacy bug
    # or user copy-paste), keep only the first and drop the rest.
    new_entries: list[Any] = []
    found_managed = False
    changed = False
    for entry in entries:
        if _is_managed_entry(entry):
            if found_managed:
                # Drop subsequent duplicate managed entries.
                changed = True
                continue
            found_managed = True
            # Refresh the command if it differs; preserve everything else
            # the user may have added under our sentinel (unlikely, but
            # keeps us minimally destructive).
            existing_hooks = entry.get("hooks")
            desired_hooks = [{"type": "command", "command": command}]
            if existing_hooks != desired_hooks:
                entry["hooks"] = desired_hooks
                changed = True
            new_entries.append(entry)
        else:
            new_entries.append(entry)

    if not found_managed:
        new_entries.append(_build_managed_entry(command))
        changed = True

    if changed:
        hooks["SessionStart"] = new_entries
        data["hooks"] = hooks
        _write_settings(settings_path, data)
    return changed


def uninstall_session_start_hook(settings_path: Path) -> bool:
    """Remove ONLY SessionFS-managed SessionStart entries.

    Idempotent: a no-op if the file is missing or has no managed entries.
    User-defined SessionStart entries (or any other hook event) are
    preserved. Empty SessionStart arrays after removal are pruned to
    keep the file tidy. Empty top-level ``hooks`` blocks are also pruned.

    Returns ``True`` if the file was modified.
    """
    with _locked(settings_path):
        return _uninstall_locked(settings_path)


def _uninstall_locked(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    try:
        data = _read_settings(settings_path)
    except MalformedSettingsError:
        # On malformed input we surface the error rather than nuking the file.
        # The CLI catches this and renders a clear message.
        raise

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("SessionStart")
    if not isinstance(entries, list):
        return False

    kept = [e for e in entries if not _is_managed_entry(e)]
    if len(kept) == len(entries):
        return False  # nothing of ours to remove

    if kept:
        hooks["SessionStart"] = kept
    else:
        # Drop the now-empty SessionStart slot entirely.
        hooks.pop("SessionStart", None)

    if not hooks:
        # Drop the now-empty top-level hooks dict to keep settings.json tidy.
        data.pop("hooks", None)

    _write_settings(settings_path, data)
    return True
