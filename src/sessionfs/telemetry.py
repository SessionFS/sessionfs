"""Client-side telemetry emitter — fire-and-forget funnel events.

tk_8cad8c9376cb4e1b — v0.15 activation-funnel telemetry.

Privacy guarantees:
  - Opt-out via SFS_NO_TELEMETRY env or config.toml [telemetry] enabled=false
  - Fire-and-forget: never raises, never blocks the caller >1s
  - Payload contains NO paths, session content, email, or hostname
  - install_id is a random UUID4 hex, NOT derived from any identifying data

See docs/telemetry.md for the full disclosure.
"""

from __future__ import annotations

import logging
import os
import platform
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("sessionfs.telemetry")

# ---------------------------------------------------------------------------
# install_id — persisted once, shared across all events
# ---------------------------------------------------------------------------

_INSTALL_ID: str | None = None
_INSTALL_ID_LOCK = threading.Lock()


def _sessionfs_dir() -> Path:
    return Path.home() / ".sessionfs"


def _install_id_path() -> Path:
    return _sessionfs_dir() / "install_id"


def disclosure_shown() -> bool:
    """True once a telemetry notice has actually been SHOWN to the user.

    Tracked separately from the install id: a piped/non-TTY first command can
    create the id without any visible notice, and that must not suppress the
    disclosure on the next interactive surface. Never raises.
    """
    try:
        return (_MARKERS_DIR / "disclosure-shown").exists()
    except Exception:
        return True  # unsure → don't re-nag


def mark_disclosure_shown() -> None:
    """Record that a telemetry notice was displayed (best-effort)."""
    try:
        _MARKERS_DIR.mkdir(parents=True, exist_ok=True)
        (_MARKERS_DIR / "disclosure-shown").touch()
    except Exception:
        pass


def _print_first_run_disclosure() -> None:
    """One-line disclosure at the moment the install id is first created.

    ANY command can be a user's first contact (not just `sfs init`), so the
    notice rides on id creation itself. stderr + TTY-gated: interactive users
    see it exactly once; daemons and piped invocations stay clean (the daemon
    logs its own notice). Never raises.
    """
    try:
        if disclosure_shown():
            return
        if sys.stderr.isatty():
            print(
                "SessionFS collects anonymous usage telemetry (random install "
                "id, version, OS, event name — never paths or session "
                "content). Disable: SFS_NO_TELEMETRY=1 or "
                "`sfs config set telemetry.enabled false`. docs/telemetry.md",
                file=sys.stderr,
            )
            mark_disclosure_shown()
    except Exception:
        pass


def install_id_exists() -> bool:
    """True once an install id has been created (i.e. the first-run disclosure
    moment has passed). Never raises."""
    try:
        return _install_id_path().exists()
    except Exception:
        return True  # unsure → don't re-nag


def get_install_id() -> str:
    """Return the persistent install-id UUID4 hex, creating it on first call.

    Atomic write via O_CREAT|O_EXCL so two concurrent first-callers cannot
    race.  File is created 0600 (owner-only read/write).

    On first creation, fires the ``install`` telemetry event (emit_once
    guarded by marker file — at most once per install).
    """
    global _INSTALL_ID

    if _INSTALL_ID is not None:
        return _INSTALL_ID

    was_created = False

    with _INSTALL_ID_LOCK:
        if _INSTALL_ID is not None:
            return _INSTALL_ID

        path = _install_id_path()
        try:
            _INSTALL_ID = path.read_text().strip()
            if _INSTALL_ID:
                return _INSTALL_ID
        except FileNotFoundError:
            pass

        # First boot — create atomically.
        new_id = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            with os.fdopen(fd, "w") as f:
                f.write(new_id + "\n")
        except FileExistsError:
            # Another process beat us — read theirs. The winner may not have
            # finished writing yet, so retry briefly; never return an empty id
            # (fall back to a process-local one rather than send "" upstream).
            for _ in range(5):
                try:
                    existing = path.read_text().strip()
                except FileNotFoundError:
                    existing = ""
                if existing:
                    _INSTALL_ID = existing
                    return _INSTALL_ID
                time.sleep(0.01)
            _INSTALL_ID = new_id
            return _INSTALL_ID

        _INSTALL_ID = new_id
        was_created = True
        _print_first_run_disclosure()

    # Fire install event outside the lock (emit calls get_install_id again,
    # which returns the cached _INSTALL_ID immediately).
    if was_created:
        emit_once("install", "install")

    return _INSTALL_ID


# ---------------------------------------------------------------------------
# opt-out
# ---------------------------------------------------------------------------

_TELEMETRY_ENABLED: bool | None = None  # cached result


_CONFIG_PATH_OVERRIDE: "Path | None" = None


def set_config_path_override(path: "Path | None") -> None:
    """Pin the config file telemetry reads its opt-out from.

    A process launched with an explicit config (``sfsd --config /path``) calls
    this at startup so ``[telemetry] enabled=false`` in THAT file is honored
    (the profile/default resolution below is skipped). Also resets the cache.
    """
    global _CONFIG_PATH_OVERRIDE
    _CONFIG_PATH_OVERRIDE = path
    reset_telemetry_cache()


def reset_telemetry_cache() -> None:
    """Forget the cached enabled/disabled decision.

    Long-running processes (the daemon) call this on config reload so a user's
    `sfs config set telemetry.enabled false` takes effect without a restart.
    """
    global _TELEMETRY_ENABLED
    _TELEMETRY_ENABLED = None


def telemetry_enabled() -> bool:
    """Return False if telemetry is disabled via env or config, True otherwise.

    Checks (short-circuit, cheap):
      1. SFS_NO_TELEMETRY env var (any non-empty value → disabled)
      2. config.toml [telemetry] enabled = false

    Any config-read error → fail closed (return False) for privacy.
    Missing config key → default True.
    """
    global _TELEMETRY_ENABLED

    if _TELEMETRY_ENABLED is not None:
        return _TELEMETRY_ENABLED

    # 1. Env-var opt-out
    if os.environ.get("SFS_NO_TELEMETRY", "").strip():
        _TELEMETRY_ENABLED = False
        return False

    # 2. Config-file opt-out
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # Python < 3.11 fallback

    # Collect EVERY config file a user could plausibly have written the
    # opt-out to, and disable if ANY of them says so (privacy-conservative OR):
    #   1. an explicit override path (sfsd --config /path pins it — exclusive)
    #   2. the active profile's toml (named profiles) or the main config.toml
    #   3. the store-dir config.toml that `sfs config set telemetry.enabled
    #      false` (the DOCUMENTED opt-out) writes through cmd_config
    if _CONFIG_PATH_OVERRIDE is not None:
        candidates = [_CONFIG_PATH_OVERRIDE]
    else:
        candidates = [_sessionfs_dir() / "config.toml"]
        try:
            from sessionfs.profiles import (
                DEFAULT_PROFILE,
                profile_config_path,
                resolve_active_profile_name,
            )

            active = resolve_active_profile_name()
            if active != DEFAULT_PROFILE:
                candidates.append(profile_config_path(active))
        except Exception:
            pass
        try:
            from sessionfs.cli.common import get_store_dir

            candidates.append(get_store_dir() / "config.toml")
        except Exception:
            pass

    try:
        seen: set[str] = set()
        for config_path in candidates:
            key = str(config_path)
            if key in seen:
                continue
            seen.add(key)
            if not config_path.exists():
                continue
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            telemetry_cfg = raw.get("telemetry")
            if isinstance(telemetry_cfg, dict):
                enabled = telemetry_cfg.get("enabled")
                if enabled is False or enabled == "false":
                    _TELEMETRY_ENABLED = False
                    return False
    except Exception:
        # Any config-read error → fail closed for privacy.
        logger.debug("Telemetry config read failed — defaulting to disabled", exc_info=True)
        _TELEMETRY_ENABLED = False
        return False

    _TELEMETRY_ENABLED = True
    return True


# ---------------------------------------------------------------------------
# emit / emit_once
# ---------------------------------------------------------------------------

_EVENT_VOCABULARY = frozenset({
    "install",
    "init_completed",
    "first_capture",
    "first_magic",
    "heartbeat",
    "capture_degraded",
})

_MARKERS_DIR = _sessionfs_dir() / "telemetry-markers"


def _api_base_url() -> str:
    """Indirection point (stubbed in tests to a closed local port)."""
    return _resolve_api_base_url()


def _resolve_api_base_url() -> str:
    """Telemetry posts to the SAME server the rest of the product talks to.

    Precedence: SESSIONFS_API_URL env > the resolved profile/sync config
    api_url (self-hosted users' events go to THEIR server) > hosted default.
    """
    env_url = os.environ.get("SESSIONFS_API_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        from sessionfs.profiles import resolve_auth

        url = resolve_auth().api_url
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return "https://api.sessionfs.dev"


_PENDING_THREADS: "list[threading.Thread]" = []
_ATEXIT_REGISTERED = False


def _track_thread(t: "threading.Thread") -> None:
    """Track an in-flight post so a short-lived CLI process flushes it at exit.

    Threads are daemonized (they never BLOCK a running process), but a CLI
    one-shot like `sfs recapture` can exit before the post lands — the atexit
    hook joins outstanding posts with a 1s TOTAL budget so events aren't lost,
    while a hung network still can't stall exit beyond ~1s.
    """
    global _ATEXIT_REGISTERED
    _PENDING_THREADS.append(t)
    if len(_PENDING_THREADS) > 16:
        del _PENDING_THREADS[:-16]  # keep the tail; older threads are done
    if not _ATEXIT_REGISTERED:
        import atexit

        atexit.register(_flush_pending)
        _ATEXIT_REGISTERED = True


def _flush_pending() -> None:
    deadline = time.monotonic() + 1.0
    for t in list(_PENDING_THREADS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if t.is_alive():
            t.join(timeout=remaining)


def emit(event: str, *, tool: str | None = None) -> None:
    """Fire-and-forget one telemetry event.

    Hard rules:
      - NEVER raises
      - NEVER blocks the caller >1s
      - Opt-out checked BEFORE any thread spawn or network attempt
      - Payload contains install_id, version, os, event, event_ts, tool (if set)
    """
    if event not in _EVENT_VOCABULARY:
        return  # silent drop — programmer error, don't crash

    if not telemetry_enabled():
        return  # opt-out short-circuits before any work

    try:
        import sessionfs
        import httpx

        install_id = get_install_id()
        payload: dict = {
            "install_id": install_id,
            "version": sessionfs.__version__,
            "os": platform.system().lower(),
            "event": event,
            "event_ts": datetime.now(timezone.utc).isoformat(),
        }
        if tool is not None:
            payload["tool"] = tool

        url = f"{_api_base_url()}/api/v1/telemetry"

        def _post() -> None:
            try:
                with httpx.Client(timeout=1.0) as client:
                    client.post(url, json=payload)
            except Exception:
                pass  # fire-and-forget — never surface to caller

        t = threading.Thread(target=_post, daemon=True)
        _track_thread(t)
        t.start()
    except Exception:
        pass  # never raise


def emit_once(event: str, marker_name: str, *, tool: str | None = None) -> None:
    """Like emit() but guarded by a marker file so the event fires at most once.

    ``marker_name`` is the filename inside ~/.sessionfs/telemetry-markers/.
    Marker write is best-effort (non-fatal on failure).
    """
    if not telemetry_enabled():
        return

    marker_path = _MARKERS_DIR / marker_name
    # Atomic claim via O_CREAT|O_EXCL: exactly one process creates the marker
    # and emits; concurrent losers get FileExistsError and skip. Any other
    # marker error → skip the emit (fail quiet, never double-fire).
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return  # already fired
    except Exception:
        return

    emit(event, tool=tool)


def emit_heartbeat() -> None:
    """Emit the daily heartbeat at most once per UTC day.

    Uses ONE marker file holding the last-fired date (no per-day file
    accumulation). Best-effort; never raises.

    Re-reads the config fresh each day (cache reset) so a config-file opt-out
    reaches a long-running daemon within 24h even without a SIGHUP reload.
    """
    reset_telemetry_cache()
    if not telemetry_enabled():
        return
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker = _MARKERS_DIR / "heartbeat"
        try:
            if marker.read_text().strip() == today:
                return  # already fired today
        except FileNotFoundError:
            pass
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(today)
    except Exception:
        return
    emit("heartbeat")
