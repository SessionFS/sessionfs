"""Tests for shared project context feature."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest


class TestGitRemoteDetection:
    """CLI git remote detection."""

    def test_detect_from_cwd(self):
        from sessionfs.cli.cmd_project import _get_git_remote

        # Should detect the current repo's remote (we're in a git repo)
        remote = _get_git_remote()
        assert remote is not None
        assert "sessionfs" in remote.lower() or len(remote) > 0

    def test_normalize_ssh(self):
        from sessionfs.cli.cmd_project import _normalize_remote

        assert _normalize_remote("git@github.com:SessionFS/sessionfs.git") == "sessionfs/sessionfs"

    def test_normalize_https(self):
        from sessionfs.cli.cmd_project import _normalize_remote

        assert _normalize_remote("https://github.com/SessionFS/sessionfs.git") == "sessionfs/sessionfs"

    def test_normalize_no_git_suffix(self):
        from sessionfs.cli.cmd_project import _normalize_remote

        assert _normalize_remote("https://github.com/SessionFS/sessionfs") == "sessionfs/sessionfs"

    def test_normalize_case_insensitive(self):
        from sessionfs.cli.cmd_project import _normalize_remote

        assert _normalize_remote("https://github.com/MyOrg/MyRepo") == "myorg/myrepo"


class TestProjectApiRoutes:
    """API route validation."""

    def test_project_model_exists(self):
        from sessionfs.server.db.models import Project

        assert hasattr(Project, "id")
        assert hasattr(Project, "name")
        assert hasattr(Project, "git_remote_normalized")
        assert hasattr(Project, "context_document")
        assert hasattr(Project, "owner_id")
        assert hasattr(Project, "created_at")
        assert hasattr(Project, "updated_at")

    def test_default_template_content(self):
        from sessionfs.server.routes.projects import DEFAULT_TEMPLATE

        assert "# Project Context" in DEFAULT_TEMPLATE
        assert "## Architecture" in DEFAULT_TEMPLATE
        assert "## Conventions" in DEFAULT_TEMPLATE
        assert "## Key Decisions" in DEFAULT_TEMPLATE

    def test_create_project_request_model(self):
        from sessionfs.server.routes.projects import CreateProjectRequest

        req = CreateProjectRequest(name="test", git_remote_normalized="owner/repo")
        assert req.name == "test"
        assert req.git_remote_normalized == "owner/repo"

    def test_update_context_request_model(self):
        from sessionfs.server.routes.projects import UpdateContextRequest

        req = UpdateContextRequest(context_document="# Hello")
        assert req.context_document == "# Hello"


class TestMcpToolDefinition:
    """MCP servers expose get_project_context tool."""

    def test_local_server_has_tool(self):
        from sessionfs.mcp.server import _TOOLS

        tool_names = [t.name for t in _TOOLS]
        assert "get_project_context" in tool_names

    def test_remote_server_has_tool(self):
        from sessionfs.mcp.remote_server import _TOOLS

        tool_names = [t.name for t in _TOOLS]
        assert "get_project_context" in tool_names

    def test_tool_description_mentions_architecture(self):
        from sessionfs.mcp.server import _TOOLS

        tool = next(t for t in _TOOLS if t.name == "get_project_context")
        assert "architecture" in tool.description.lower()

    def test_tool_has_git_remote_param(self):
        from sessionfs.mcp.server import _TOOLS

        tool = next(t for t in _TOOLS if t.name == "get_project_context")
        assert "git_remote" in tool.inputSchema["properties"]


class TestCloudClient:
    """Cloud API client supports project context."""

    def test_get_project_context_method_exists(self):
        from sessionfs.mcp.cloud_client import CloudAPIClient

        client = CloudAPIClient()
        assert hasattr(client, "get_project_context")
        import inspect
        assert inspect.iscoroutinefunction(client.get_project_context)


class TestCliCommands:
    """CLI command registration."""

    def test_project_app_exists(self):
        from sessionfs.cli.cmd_project import project_app

        assert project_app is not None
        # Check that commands are registered
        command_names = [cmd.name for cmd in project_app.registered_commands]
        assert "init" in command_names
        assert "show" in command_names
        assert "edit" in command_names
        assert "set-context" in command_names
        assert "get-context" in command_names

    def test_project_app_in_main(self):
        """project subcommand should be registered in main CLI."""
        from sessionfs.cli.main import app

        group_names = [g.typer_instance.info.name for g in app.registered_groups if g.typer_instance and g.typer_instance.info]
        assert "project" in group_names


class TestMigration:
    """Migration file structure."""

    def test_migration_exists(self):
        from pathlib import Path

        migration = Path("src/sessionfs/server/db/migrations/versions/012_projects.py")
        assert migration.exists()
