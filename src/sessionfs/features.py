"""Enterprise Edition feature loading and extension registry.

Community code never imports ``sessionfs_ee`` statically; this module is the
single seam. The first time an extension hook runs it discovers the Enterprise
package with :func:`importlib.import_module` and hands it an
:class:`ExtensionRegistry` via ``sessionfs_ee.register(registry)``.

When the Enterprise package is absent every hook is a no-op. When it is
present but its ``register()`` raises, the error propagates so a broken
Enterprise build cannot silently run as Community.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any, Callable

logger = logging.getLogger("sessionfs.features")

# -- typed hook signatures ------------------------------------------------
# The concrete types (FastAPI, the MCP ``Server``, the Typer app) are not
# imported here so the seam stays light and import-order safe.
RouteHook = Callable[[Any], None]
McpToolHook = Callable[[Any], None]
CliHook = Callable[[Any], None]
SweepHook = Callable[[], None]
# A provider hook returns extra ``(name, provider)`` pairs for ``kind``.
ProviderHook = Callable[[str], list[tuple[str, Any]]]


_ROUTE_PHASES = frozenset({"early", "late"})


class ExtensionRegistry:
    """Collects the hooks an Enterprise ``sessionfs_ee.register()`` installs."""

    def __init__(self) -> None:
        self._route_hooks: dict[str, list[RouteHook]] = {}
        self._mcp_tool_hooks: list[McpToolHook] = []
        self._cli_hooks: list[CliHook] = []
        self._sweep_hooks: list[SweepHook] = []
        self._sweeps_done: set[int] = set()
        self._provider_hooks: dict[str, list[ProviderHook]] = {}

    # -- registration surface used by sessionfs_ee.register(registry) --
    def add_routes(self, hook: RouteHook, phase: str = "late") -> None:
        """Register a callback invoked as ``hook(app)`` on the FastAPI app.

        ``phase`` controls ordering relative to the Community routers:
        ``"early"`` hooks run before the projects catch-all (so an Enterprise
        route can claim a more specific path), ``"late"`` hooks run after every
        Community router.
        """
        if phase not in _ROUTE_PHASES:
            raise ValueError(f"route phase must be one of {sorted(_ROUTE_PHASES)}, got {phase!r}")
        self._route_hooks.setdefault(phase, []).append(hook)

    def add_mcp_tools(self, hook: McpToolHook) -> None:
        """Register a callback invoked as ``hook(server)`` on the MCP server."""
        self._mcp_tool_hooks.append(hook)

    def add_cli(self, hook: CliHook) -> None:
        """Register a callback invoked as ``hook(typer_app)`` on the CLI root."""
        self._cli_hooks.append(hook)

    def add_sweeps(self, hook: SweepHook) -> None:
        """Register a callback invoked with no arguments for extra sweeps."""
        self._sweep_hooks.append(hook)

    def add_providers(self, hook: ProviderHook, kind: str) -> None:
        """Register a callback returning extra ``(name, provider)`` pairs."""
        self._provider_hooks.setdefault(kind, []).append(hook)

    # -- hook points invoked by Community call sites --
    def register_routes(self, app: Any, phase: str = "late") -> None:
        for hook in self._route_hooks.get(phase, ()):
            hook(app)

    def register_mcp_tools(self, server: Any) -> None:
        for hook in self._mcp_tool_hooks:
            hook(server)

    def register_cli(self, typer_app: Any) -> None:
        for hook in self._cli_hooks:
            hook(typer_app)

    def register_sweeps(self) -> None:
        # Each hook runs at most once per process: a hook that already
        # succeeded is skipped on a retry after a later hook failed.
        for hook in self._sweep_hooks:
            if id(hook) in self._sweeps_done:
                continue
            hook()
            self._sweeps_done.add(id(hook))

    def _merge(self, other: "ExtensionRegistry") -> None:
        """Adopt every hook ``other`` collected (used after a clean register())."""
        for phase, hooks in other._route_hooks.items():
            self._route_hooks.setdefault(phase, []).extend(hooks)
        self._mcp_tool_hooks.extend(other._mcp_tool_hooks)
        self._cli_hooks.extend(other._cli_hooks)
        self._sweep_hooks.extend(other._sweep_hooks)
        for kind, hooks in other._provider_hooks.items():
            self._provider_hooks.setdefault(kind, []).extend(hooks)

    def register_providers(self, kind: str) -> list[tuple[str, Any]]:
        providers: list[tuple[str, Any]] = []
        for hook in self._provider_hooks.get(kind, ()):
            providers.extend(hook(kind))
        return providers

    def _clear(self) -> None:
        """Drop every registered hook (used by ``reset_extensions``)."""
        self._route_hooks.clear()
        self._mcp_tool_hooks.clear()
        self._cli_hooks.clear()
        self._sweep_hooks.clear()
        self._sweeps_done.clear()
        self._provider_hooks.clear()


# Process-wide registry, populated once by _load_extensions().
_registry = ExtensionRegistry()
_loaded = False
# Sweep hooks run once per process (see register_sweeps), not once per app.


def _load_extensions() -> None:
    """Discover ``sessionfs_ee`` and run its ``register(registry)`` once."""
    global _loaded
    if _loaded:
        return
    try:
        module = importlib.import_module("sessionfs_ee")
    except ModuleNotFoundError as exc:
        if exc.name != "sessionfs_ee":
            # The Enterprise package is there but one of its own imports is
            # missing: a broken Enterprise build, never a Community install.
            raise
        _loaded = True
        return
    register = getattr(module, "register", None)
    if not callable(register):
        # An Enterprise package without a callable register() cannot have
        # installed any hooks; treating it as Community would silently drop
        # every Enterprise feature. Refuse to start instead.
        raise ImportError(
            "sessionfs_ee is missing a callable register(registry); "
            "refusing to run as Community"
        )
    # Register into a scratch registry and publish only on success, so a
    # register() that adds some hooks and then raises leaves nothing behind
    # (and a retry can't register the same hooks twice).
    staged = ExtensionRegistry()
    try:
        register(staged)
    except Exception:
        # Log the module name only — never anything a raised Enterprise
        # exception might carry (credentials, licence text, ...).
        logger.exception(
            "Enterprise package %s failed to register; refusing to run as Community",
            getattr(module, "__name__", "sessionfs_ee"),
        )
        raise
    _registry._merge(staged)
    _loaded = True


def reset_extensions() -> None:
    """Reset registry and discovery state so tests can swap the ee package."""
    global _loaded
    _loaded = False
    _registry._clear()


def is_ee_available() -> bool:
    """Check if Enterprise Edition code is available.

    Only a ``ModuleNotFoundError`` for ``sessionfs_ee`` itself counts as "not
    installed". Any other import failure — a missing dependency of an
    installed Enterprise package — propagates so a broken Enterprise build is
    never mistaken for a Community one.
    """
    try:
        importlib.import_module("sessionfs_ee")
        return True
    except ModuleNotFoundError as exc:
        if exc.name != "sessionfs_ee":
            raise
        return False


def get_feature(feature_name: str) -> ModuleType | None:
    """Load a feature from ee/ if available, otherwise return None.

    Like :func:`is_ee_available`, a ``ModuleNotFoundError`` for a module other
    than the requested feature (i.e. one of its own missing dependencies) is
    re-raised rather than swallowed as "feature absent".
    """
    if not is_ee_available():
        return None
    try:
        return importlib.import_module(f"sessionfs_ee.{feature_name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"sessionfs_ee.{feature_name}":
            raise
        return None


# -- module-level hook points (the Community call sites) --
def register_routes(app: Any, phase: str = "late") -> None:
    _load_extensions()
    _registry.register_routes(app, phase)


def register_mcp_tools(server: Any) -> None:
    _load_extensions()
    _registry.register_mcp_tools(server)


def register_cli(typer_app: Any) -> None:
    _load_extensions()
    _registry.register_cli(typer_app)


def register_sweeps() -> None:
    """Run Enterprise sweep hooks, each at most once per process.

    Called from the API lifespan after the database, blob store and email
    service are initialised, so hooks never start against incomplete state.
    A hook that already succeeded is not run again; a failed one is retried
    on the next startup. Route hooks are deliberately per app instead.
    """
    _load_extensions()
    _registry.register_sweeps()

def register_providers(kind: str) -> list[tuple[str, Any]]:
    _load_extensions()
    return _registry.register_providers(kind)
