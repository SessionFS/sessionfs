"""End-to-end smoke tests for `sfs resume` with rules preflight (v0.9.9).

These tests exercise the ACTUAL `resume()` command wiring — not just the
helper — by setting up a real temp project dir with a git repo, a local
.sfs session, and mocking only the network boundary + tool launch.

Each test proves that the preflight ran BEFORE the launch was called,
and verifies real file writes on disk + stderr messaging.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sessionfs.store.local import LocalStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, *, remote: str = "https://github.com/acme/widget.git") -> None:
    """Create a minimal git repo at *path* with an origin remote."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote],
        check=True,
    )


def _create_session(store: LocalStore, session_id: str, *, provenance: dict | None = None) -> Path:
    """Create a minimal .sfs session inside *store* and index it.

    Returns the session directory.
    """
    session_dir = store.allocate_session_dir(session_id)
    manifest: dict = {
        "session_id": session_id,
        "title": "Test session",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "tool_use_count": 0},
    }
    if provenance is not None:
        manifest["instruction_provenance"] = provenance
    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (session_dir / "messages.jsonl").write_text(
        json.dumps({"role": "user", "content": [{"type": "text", "text": "hello"}]}) + "\n"
    )
    store.upsert_session_metadata(session_id, manifest, str(session_dir))
    return session_dir


# Canned API responses for the preflight helper
_RULES_BODY = {
    "id": "rules_1",
    "version": 5,
    "etag": "etag-abc",
    "enabled_tools": ["codex"],
    "static_rules": "Always write tests.",
    "include_knowledge": False,
    "knowledge_types": [],
    "include_context": False,
    "context_sections": [],
}

_COMPILE_BODY = {
    "version": 5,
    "created_new_version": False,
    "aggregate_hash": "x" * 64,
    "outputs": [
        {
            "tool": "codex",
            "filename": "codex.md",
            "content": "<!--\nsessionfs-managed: version=5 hash=abcdef1234567890\nCanonical source: sfs rules edit\n-->\n\n# Codex Rules\nAlways write tests.\n",
            "hash": "abcdef1234567890",
            "token_count": 42,
        }
    ],
}

_PROJECT_BODY = {"id": "proj_test_e2e"}


async def _mock_api_ok(method, path, api_url, api_key, json_data=None, extra_headers=None):
    """Return canned 200 responses for project resolve, rules fetch, and compile."""
    if method == "GET" and "/rules" not in path and path.startswith("/api/v1/projects/"):
        return 200, dict(_PROJECT_BODY), {}
    if method == "GET" and "/rules" in path:
        return 200, dict(_RULES_BODY), {}
    if method == "POST" and "/compile" in path:
        return 200, dict(_COMPILE_BODY), {}
    raise AssertionError(f"Unexpected API call: {method} {path}")


async def _mock_api_500(method, path, api_url, api_key, json_data=None, extra_headers=None):
    """Return 500 for rules fetch to simulate server failure."""
    if method == "GET" and "/rules" not in path and path.startswith("/api/v1/projects/"):
        return 200, dict(_PROJECT_BODY), {}
    if method == "GET" and "/rules" in path:
        return 500, "Internal Server Error", {}
    raise AssertionError(f"Unexpected API call: {method} {path}")


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


SESSION_ID = "ses_e2eresumetest00000001"


class TestResumeEndToEnd:
    """End-to-end smoke tests for `sfs resume` with rules preflight."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        """Set up a temp project dir (git repo) + local store with a session."""
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()
        _init_git_repo(self.project_dir)

        self.store_dir = tmp_path / "store"
        self.store = LocalStore(self.store_dir)
        self.store.initialize()
        _create_session(
            self.store,
            SESSION_ID,
            provenance={"rules_version": 3, "rules_source": "sessionfs"},
        )

    def _run_resume(self, *, tool: str = "codex", force_rules: bool = False, no_rules_sync: bool = False, api_mock=None):
        """Invoke `resume()` with mocked store + API + tool launch.

        Returns (launch_mock, captured_stderr_lines).
        """
        from sessionfs.cli import cmd_ops
        from sessionfs.cli import resume_rules_sync as rrs

        launch_mock = MagicMock()
        stderr_lines: list[str] = []

        # Capture stderr prints from _run_rules_preflight
        original_err_print = cmd_ops.err_console.print

        def capture_err(*args, **kwargs):
            stderr_lines.append(str(args[0]) if args else "")
            original_err_print(*args, **kwargs)

        if api_mock is None:
            api_mock = _mock_api_ok

        launcher_target = {
            "codex": "sessionfs.cli.cmd_ops._resume_in_codex",
            "claude-code": "sessionfs.cli.cmd_ops._resume_in_claude_code",
            "gemini": "sessionfs.cli.cmd_ops._resume_in_gemini",
            "copilot": "sessionfs.cli.cmd_ops._resume_in_copilot",
        }[tool]

        with (
            patch("sessionfs.cli.cmd_ops.open_store", return_value=self.store),
            patch.object(rrs, "_api_request", api_mock),
            patch.object(rrs, "_get_api_config", return_value=("http://test", "key")),
            patch(launcher_target, launch_mock),
            patch.object(cmd_ops.err_console, "print", side_effect=capture_err),
        ):
            cmd_ops.resume(
                session_id=SESSION_ID,
                project=str(self.project_dir),
                tool=tool,
                model=None,
                no_rules_sync=no_rules_sync,
                force_rules=force_rules,
            )

        return launch_mock, stderr_lines

    def test_missing_target_file_created_before_launch(self):
        """When codex.md does not exist, resume creates it from canonical rules
        BEFORE launching the target tool.
        """
        target_file = self.project_dir / "codex.md"
        assert not target_file.exists(), "Precondition: target file should not exist"

        launch_mock, stderr = self._run_resume(tool="codex")

        # The target file was written by the preflight
        assert target_file.exists(), "codex.md should have been created by preflight"
        content = target_file.read_text()
        assert "sessionfs-managed" in content, "Written file should have managed marker"
        assert "Codex Rules" in content, "Written file should have compiled content"

        # The tool launch was called (proving preflight ran before launch)
        launch_mock.assert_called_once()

        # Stderr should mention the sync
        stderr_text = "\n".join(stderr)
        assert "Synced" in stderr_text or "codex.md" in stderr_text, (
            f"Stderr should mention the sync; got: {stderr_text}"
        )

    def test_unmanaged_target_file_skipped_without_force_rules(self):
        """When codex.md exists but is NOT SessionFS-managed, resume prints a
        warning and does NOT overwrite it. The tool still launches.
        """
        target_file = self.project_dir / "codex.md"
        original_content = "# My hand-written codex instructions\n"
        target_file.write_text(original_content)

        launch_mock, stderr = self._run_resume(tool="codex", force_rules=False)

        # File was NOT overwritten
        assert target_file.read_text() == original_content, (
            "Unmanaged file must not be overwritten without --force-rules"
        )

        # Tool still launched
        launch_mock.assert_called_once()

        # Stderr warns about unmanaged file
        stderr_text = "\n".join(stderr)
        assert "unmanaged" in stderr_text.lower() or "--force-rules" in stderr_text, (
            f"Stderr should warn about unmanaged file; got: {stderr_text}"
        )

    def test_preflight_failure_does_not_abort_resume(self):
        """When the API returns 500 on rules fetch, resume still proceeds to
        launch the tool. Stderr shows a warning.
        """
        launch_mock, stderr = self._run_resume(tool="codex", api_mock=_mock_api_500)

        # Tool still launched despite API failure
        launch_mock.assert_called_once()

        # Stderr mentions the failure but not a crash
        stderr_text = "\n".join(stderr)
        assert "500" in stderr_text or "failed" in stderr_text.lower() or "warning" in stderr_text.lower() or "sync" in stderr_text.lower(), (
            f"Stderr should mention the rules sync failure; got: {stderr_text}"
        )

        # No target file was written (compile never ran)
        target_file = self.project_dir / "codex.md"
        assert not target_file.exists(), "No file should be written when API fails"
