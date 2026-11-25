"""Tests for the Enterprise extension registry seam.

The registry lives in ``sessionfs.features``; the Community call sites are
``server/app.py`` (routes + sweeps), ``mcp/server.py`` (MCP tools),
``cli/main.py`` (CLI commands) and ``server/services/attention.py`` (For You
providers). These tests force both states through ``sys.modules`` / ``sys.path``:

* no ``sessionfs_ee`` install — every hook is a no-op and the Community
  surface (route count, MCP tool count, CLI command count, providers) is
  unchanged;
* a fake ``sessionfs_ee`` whose ``register(registry)`` installs hooks — each
  hook appears at its call site;
* a fake whose ``register()`` raises — ``create_app()`` refuses to start.

The fake package is installed via ``sys.modules`` (never a real import path),
and every test restores ``sys.modules`` / ``sys.path`` through ``monkeypatch``.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from sessionfs import features as features_mod


def _strip_ee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a Community install: no importable ``sessionfs_ee``."""
    for name in list(sys.modules):
        if name == "sessionfs_ee" or name.startswith("sessionfs_ee."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        sys,
        "path",
        [p for p in sys.path if p != "ee" and not p.rstrip("/").endswith("/ee")],
    )
    features_mod.reset_extensions()


def _install_fake_ee(monkeypatch: pytest.MonkeyPatch, register) -> None:
    """Install a fake ``sessionfs_ee`` package whose ``register`` is ``register``."""
    _strip_ee(monkeypatch)
    pkg = ModuleType("sessionfs_ee")
    pkg.__path__ = []
    pkg.register = register
    monkeypatch.setitem(sys.modules, "sessionfs_ee", pkg)
    features_mod.reset_extensions()


# -- no ee: the Community surface is unchanged ----------------------------


def test_no_ee_app_routes_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_ee(monkeypatch)
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.route_introspection import iter_api_routes

    # The Community app must expose exactly the committed API contract
    # (compared via app.openapi(), the same view the snapshot records), and
    # nothing Enterprise. len(app.routes) is not used: its shape changes
    # across FastAPI releases.
    import json
    from pathlib import Path

    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    live = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
    }
    snapshot = json.loads(
        (Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.json").read_text()
    )
    contract = {
        (method.upper(), path) for path, ops in snapshot["paths"].items() for method in ops
    }
    assert live == contract
    assert not any("enterprise" in path for _, path in live)
    assert len(list(iter_api_routes(app))) > 0


def test_no_ee_mcp_tool_count_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_ee(monkeypatch)
    import sessionfs.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "_EXTRA_TOOLS", [])
    monkeypatch.setattr(mcp_server, "_EXTRA_HANDLERS", {})
    mcp_server.register_mcp_tools(mcp_server.app)

    assert len(mcp_server._TOOLS) == 149
    assert mcp_server._EXTRA_TOOLS == []
    assert mcp_server._EXTRA_HANDLERS == {}


def test_no_ee_cli_commands_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_ee(monkeypatch)
    from sessionfs.cli.main import app

    assert len(app.registered_commands) == 23


def test_no_ee_providers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_ee(monkeypatch)
    assert features_mod.register_providers("attention") == []


# -- fake ee: each hook appears at its call site --------------------------


def test_fake_ee_adds_route_and_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    sweep_calls: list[object] = []

    def register(registry) -> None:
        def add_route(app) -> None:
            app.add_api_route(
                "/api/v1/enterprise/ping",
                lambda: {"ok": True},
                methods=["GET"],
            )

        def add_sweep() -> None:
            sweep_calls.append(True)

        registry.add_routes(add_route)
        registry.add_sweeps(add_sweep)

    from sessionfs import features as _features
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.route_introspection import iter_api_routes

    # Baseline in this same environment, with Enterprise blocked.
    _strip_ee(monkeypatch)
    baseline = list(
        iter_api_routes(create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard")))
    )
    _features.reset_extensions()

    _install_fake_ee(monkeypatch, register)
    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    api_routes = list(iter_api_routes(app))
    assert len(api_routes) == len(baseline) + 1
    assert any(entry.path == "/api/v1/enterprise/ping" for entry in api_routes)
    # Sweeps start in the lifespan, after startup state exists, not at build.
    assert sweep_calls == []
    from sessionfs import features

    features.register_sweeps()
    assert sweep_calls == [True]


def test_fake_ee_adds_mcp_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    import sessionfs.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "_EXTRA_TOOLS", [])
    monkeypatch.setattr(mcp_server, "_EXTRA_HANDLERS", {})

    def register(registry) -> None:
        def add_mcp(server) -> None:
            from mcp.types import Tool

            from sessionfs.mcp.server import add_tool

            async def handler(arguments):
                return {"pong": True}

            add_tool(
                Tool(
                    name="ee_ping",
                    description="Enterprise ping",
                    inputSchema={"type": "object", "properties": {}},
                ),
                handler,
            )

        registry.add_mcp_tools(add_mcp)

    _install_fake_ee(monkeypatch, register)
    mcp_server.register_mcp_tools(mcp_server.app)

    assert [t.name for t in mcp_server._EXTRA_TOOLS] == ["ee_ping"]
    assert "ee_ping" in mcp_server._EXTRA_HANDLERS


def test_fake_ee_adds_cli_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    def register(registry) -> None:
        def add_cli(app) -> None:
            @app.command("ee-ping")
            def ee_ping() -> None:
                """Enterprise ping."""

        registry.add_cli(add_cli)

    _install_fake_ee(monkeypatch, register)

    cli = typer.Typer()
    features_mod.register_cli(cli)
    names = {cmd.callback.__name__ for cmd in cli.registered_commands}
    assert "ee_ping" in names


def test_fake_ee_adds_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()

    def register(registry) -> None:
        def provider_hook(kind: str):
            return [("ee_provider", marker)]

        registry.add_providers(provider_hook, "attention")

    _install_fake_ee(monkeypatch, register)

    providers = features_mod.register_providers("attention")
    assert ("ee_provider", marker) in providers


def test_provider_kind_is_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    def register(registry) -> None:
        registry.add_providers(lambda kind: [("other", object())], "other_kind")

    _install_fake_ee(monkeypatch, register)

    assert features_mod.register_providers("attention") == []


# -- a broken Enterprise build cannot silently run as Community -----------


def test_fake_ee_register_raises_makes_create_app_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def register(registry) -> None:
        raise RuntimeError("broken enterprise register")

    _install_fake_ee(monkeypatch, register)

    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    with pytest.raises(RuntimeError, match="broken enterprise register"):
        create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))


def test_enterprise_package_with_a_missing_dependency_fails_loudly(tmp_path, monkeypatch):
    """sessionfs_ee present but importing a missing module must raise, not
    fall back to Community."""
    import sys

    from sessionfs import features

    pkg = tmp_path / "sessionfs_ee"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "# SESSIONFS-ENTERPRISE-ONLY\nimport sessionfs_ee_missing_dependency  # noqa: F401\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sessionfs_ee", raising=False)
    features.reset_extensions()
    try:
        with pytest.raises(ModuleNotFoundError):
            features.register_providers("attention")
    finally:
        sys.modules.pop("sessionfs_ee", None)
        features.reset_extensions()


# -- an Enterprise package without a callable register() is broken ---------


@pytest.mark.parametrize("register", [None, "not-callable", 42])
def test_fake_ee_without_callable_register_raises(
    monkeypatch: pytest.MonkeyPatch, register: object
) -> None:
    """A ``sessionfs_ee`` that imports but exposes no callable ``register`` must
    refuse to run as Community — otherwise every Enterprise feature silently
    drops out."""
    _strip_ee(monkeypatch)
    pkg = ModuleType("sessionfs_ee")
    pkg.__path__ = []
    if register is not None:
        pkg.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sessionfs_ee", pkg)
    features_mod.reset_extensions()

    with pytest.raises(ImportError, match="missing a callable register"):
        features_mod.register_providers("attention")


# -- early/late route phases ------------------------------------------------


def test_fake_ee_early_route_beats_projects_catch_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An Enterprise route registered in the ``early`` phase claims a path the
    greedy ``projects`` catch-all (/{git_remote_normalized:path}) would
    otherwise shadow."""
    from fastapi.testclient import TestClient

    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    def register(registry) -> None:
        def add_early_route(app) -> None:
            app.add_api_route(
                "/api/v1/projects/{project_id}/ee-test",
                lambda project_id: {"served_by": "enterprise", "project_id": project_id},
                methods=["GET"],
            )

        registry.add_routes(add_early_route, phase="early")

    _install_fake_ee(monkeypatch, register)

    app = create_app(
        ServerConfig(
            dashboard_dir="/nonexistent-sessionfs-dashboard",
            database_url="sqlite+aiosqlite://",
            blob_store_local_path=str(tmp_path / "blobs"),
        )
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/projects/proj_abc123/ee-test")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"served_by": "enterprise", "project_id": "proj_abc123"}


# -- sweep hooks run once per process --------------------------------------


def test_sweep_hooks_run_once_across_create_app_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sequential ``create_app()`` calls must run each sweep hook exactly
    once; sweep hooks start background jobs that would otherwise duplicate."""
    sweep_calls: list[object] = []

    def register(registry) -> None:
        registry.add_sweeps(lambda: sweep_calls.append(True))

    _install_fake_ee(monkeypatch, register)

    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    from sessionfs import features

    create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    assert sweep_calls == []  # building an app never starts sweeps
    features.register_sweeps()  # what each app's lifespan calls at startup
    features.register_sweeps()
    assert sweep_calls == [True]


def test_partial_register_leaves_no_hooks_and_retry_does_not_duplicate(monkeypatch):
    """register() that adds a hook and then raises must publish nothing, so a
    retry after fixing it registers each hook exactly once."""
    from sessionfs import features

    state = {"fail": True}

    def register(registry) -> None:
        registry.add_providers(lambda kind: [("p", object())], "attention")
        if state["fail"]:
            raise RuntimeError("boom")

    _install_fake_ee(monkeypatch, register)
    with pytest.raises(RuntimeError):
        features.register_providers("attention")
    assert features._registry.register_providers("attention") == []
    state["fail"] = False
    assert len(features.register_providers("attention")) == 1
    assert len(features.register_providers("attention")) == 1


def test_a_succeeded_sweep_is_not_rerun_when_a_later_one_failed(monkeypatch):
    from sessionfs import features

    features.reset_extensions()
    monkeypatch.setattr(features, "_load_extensions", lambda: None)
    calls = {"first": 0, "second": 0}
    state = {"fail": True}

    def first():
        calls["first"] += 1

    def second():
        calls["second"] += 1
        if state["fail"]:
            raise RuntimeError("second failed")

    features._registry.add_sweeps(first)
    features._registry.add_sweeps(second)
    try:
        with pytest.raises(RuntimeError):
            features.register_sweeps()
        state["fail"] = False
        features.register_sweeps()
        assert calls == {"first": 1, "second": 2}
    finally:
        features.reset_extensions()


# -- MCP tool name collisions + dispatch -----------------------------------


def test_add_tool_rejects_community_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import sessionfs.mcp.server as mcp_server
    from mcp.types import Tool

    monkeypatch.setattr(mcp_server, "_EXTRA_TOOLS", [])
    monkeypatch.setattr(mcp_server, "_EXTRA_HANDLERS", {})

    with pytest.raises(ValueError, match="already registered"):
        mcp_server.add_tool(
            Tool(
                name="search_sessions",
                description="shadow",
                inputSchema={"type": "object", "properties": {}},
            ),
            lambda arguments: {"shadow": True},
        )


def test_add_tool_rejects_duplicate_enterprise_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sessionfs.mcp.server as mcp_server
    from mcp.types import Tool

    monkeypatch.setattr(mcp_server, "_EXTRA_TOOLS", [])
    monkeypatch.setattr(mcp_server, "_EXTRA_HANDLERS", {})

    async def handler(arguments):
        return {"ok": True}

    mcp_server.add_tool(
        Tool(name="ee_ping", description="first", inputSchema={"type": "object"}),
        handler,
    )
    with pytest.raises(ValueError, match="already registered"):
        mcp_server.add_tool(
            Tool(name="ee_ping", description="second", inputSchema={"type": "object"}),
            handler,
        )


@pytest.mark.asyncio
async def test_call_tool_dispatches_registered_enterprise_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import sessionfs.mcp.server as mcp_server
    from mcp.types import Tool

    monkeypatch.setattr(mcp_server, "_EXTRA_TOOLS", [])
    monkeypatch.setattr(mcp_server, "_EXTRA_HANDLERS", {})

    async def handler(arguments):
        return {"pong": arguments.get("value", "missing")}

    mcp_server.add_tool(
        Tool(name="ee_ping", description="ping", inputSchema={"type": "object"}),
        handler,
    )

    out = await mcp_server.call_tool("ee_ping", {"value": "hello"})
    assert len(out) == 1
    assert json.loads(out[0].text) == {"pong": "hello"}


def test_unknown_route_phase_is_refused():
    from sessionfs.features import ExtensionRegistry

    reg = ExtensionRegistry()
    with pytest.raises(ValueError):
        reg.add_routes(lambda app: None, phase="earliy")


def test_failed_sweep_is_retried_on_the_next_startup(monkeypatch):
    from sessionfs import features

    features.reset_extensions()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(features, "_load_extensions", lambda: None)
    features._registry.add_sweeps(boom)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                features.register_sweeps()
        assert calls["n"] == 2
    finally:
        features.reset_extensions()
