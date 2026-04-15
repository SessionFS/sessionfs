"""Unit tests for `preflight_target_tool_rules` (v0.9.9 resume rules sync).

All tests avoid standing up a real API server — `_api_request` is mocked at
the module boundary so we exercise only the helper logic + disk behavior.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sessionfs.cli import resume_rules_sync as rrs
from sessionfs.cli.resume_rules_sync import (
    RulesSyncResult,
    format_messages,
    preflight_target_tool_rules,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, *, remote: str = "https://github.com/acme/widget.git") -> None:
    """Create a minimal git repo at `path` with an origin remote.

    We shell out rather than mocking `subprocess.run` because `_is_managed`
    and other helpers assume real filesystem semantics, and a real repo is
    cheap. Git is a build-time dep anyway.
    """
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote],
        check=True,
    )


MANAGED_MARKER = (
    "<!--\nsessionfs-managed: version={v} hash={h}\n"
    "Canonical source: sfs rules edit\n-->\n\n"
)


def _managed_body(version: int = 5, body: str = "# Rules v{v}\n") -> str:
    return MANAGED_MARKER.format(v=version, h="a" * 16) + body.format(v=version)


class _ApiRecorder:
    """Records calls to `_api_request` and returns scripted responses.

    Responses are matched by (method, path_substring). First match wins. The
    project-resolution GET (`/api/v1/projects/<remote>` with no `/rules` or
    `/rules/compile` suffix) is always answered with a canned 200 body so
    individual tests only need to script what they actually care about.
    """

    PROJECT_RESOLVE_BODY = {"id": "proj_test_123"}

    def __init__(self, responses: list[tuple[str, str, int, Any]]):
        # (method, path_contains, status, body)
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(
        self, method, path, api_url, api_key, json_data=None, extra_headers=None
    ):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json": json_data,
            }
        )
        # Auto-answer project resolution (GET /api/v1/projects/<remote>).
        # Identified by path ending in the bare project ref — no /rules suffix.
        if (
            method == "GET"
            and path.startswith("/api/v1/projects/")
            and "/rules" not in path
        ):
            return 200, dict(self.PROJECT_RESOLVE_BODY), {}

        for i, (m, frag, status, body) in enumerate(self.responses):
            if m == method and frag in path:
                self.responses.pop(i)
                return status, body, {}
        raise AssertionError(f"Unexpected API call: {method} {path}")


def _patch_api(responses: list[tuple[str, str, int, Any]]):
    """Patch the `_api_request` reference used by resume_rules_sync."""
    recorder = _ApiRecorder(responses)
    p = patch.object(rrs, "_api_request", recorder)
    return p, recorder


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    # Always make API config resolvable in tests.
    with patch.object(rrs, "_get_api_config", return_value=("http://test", "k")):
        yield tmp_path


def _manifest(version: int | None = 3, source: str | None = "sessionfs") -> dict:
    prov: dict = {}
    if version is not None:
        prov["rules_version"] = version
    if source is not None:
        prov["rules_source"] = source
    return {"instruction_provenance": prov}


def _rules_ok(version: int = 5, enabled: list[str] | None = None) -> dict:
    return {
        "id": "rules_1",
        "version": version,
        "etag": "etag",
        "enabled_tools": enabled if enabled is not None else ["claude-code"],
        "static_rules": "hello",
        "include_knowledge": False,
        "knowledge_types": [],
        "include_context": False,
        "context_sections": [],
    }


def _compile_ok(tool: str, filename: str, content: str, version: int = 5) -> dict:
    return {
        "version": version,
        "created_new_version": False,  # partial compile never bumps
        "aggregate_hash": "x" * 64,
        "outputs": [
            {
                "tool": tool,
                "filename": filename,
                "content": content,
                "hash": "y" * 64,
                "token_count": 42,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1–4: Cases A / B / C / D
# ---------------------------------------------------------------------------


def test_preflight_missing_file_writes_compiled_output(repo: Path):
    """Case A — target file missing → write compiled output."""
    managed_content = _managed_body(5)
    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed_content, 5)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is True
    assert result.skipped_reason is None
    assert result.filename == "CLAUDE.md"
    assert result.current_rules_version == 5
    assert (repo / "CLAUDE.md").read_text() == managed_content


def test_preflight_managed_file_refreshed(repo: Path):
    """Case B — existing managed file → overwrite."""
    stale = _managed_body(2, body="# stale v{v}\n")
    fresh = _managed_body(6, body="# fresh v{v}\n")
    (repo / "codex.md").write_text(stale)
    assert "sessionfs-managed" in stale  # sanity

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=6, enabled=["codex"])),
            ("POST", "/rules/compile", 200, _compile_ok("codex", "codex.md", fresh, 6)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="codex",
            source_manifest=_manifest(2, "sessionfs"),
        )
    assert result.applied is True
    assert (repo / "codex.md").read_text() == fresh


def test_preflight_unmanaged_file_skipped_by_default(repo: Path):
    """Case C — unmanaged file, force=False → skip + warn."""
    original = "# My hand-written rules\nNo marker here.\n"
    (repo / "CLAUDE.md").write_text(original)

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", _managed_body(5), 5)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "unmanaged-skip"
    assert result.used_force is False
    # File untouched.
    assert (repo / "CLAUDE.md").read_text() == original


def test_preflight_unmanaged_file_overwritten_with_force(repo: Path):
    """Case D — unmanaged file + force=True → overwrite with managed content."""
    original = "# Hand-written\n"
    (repo / "CLAUDE.md").write_text(original)
    managed = _managed_body(5)

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed, 5)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
            force=True,
        )
    assert result.applied is True
    assert result.used_force is True
    assert (repo / "CLAUDE.md").read_text() == managed


# ---------------------------------------------------------------------------
# 5: --no-rules-sync
# ---------------------------------------------------------------------------


def test_preflight_no_rules_sync_skips_entirely(repo: Path):
    """skip=True → no API call made, no file written, clean skip reason."""
    patcher, rec = _patch_api([])  # empty: any API call would raise
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
            skip=True,
        )
    assert result.applied is False
    assert result.skipped_reason == "no-rules-sync"
    assert rec.calls == []
    assert not (repo / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# 6: not meaningfully initialized
# ---------------------------------------------------------------------------


def test_preflight_uninitialized_rules_skipped(repo: Path):
    """enabled_tools=[] + static_rules="" + version=0 → not-initialized."""
    uninit = _rules_ok(version=0, enabled=[])
    uninit["static_rules"] = ""

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, uninit),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "not-initialized"
    # Must not have hit /compile (no POST scripted).
    assert all("compile" not in c["path"] for c in rec.calls)
    assert not (repo / "CLAUDE.md").exists()


def test_preflight_default_version1_row_without_content_is_not_initialized(
    repo: Path,
):
    """Regression: `get_or_create_rules` seeds default rows at version=1 with
    empty enabled_tools/static_rules. An earlier version-proxy check treated
    any ``version > 0`` as initialised, which would cause resume to compile +
    write into every brand-new project. The initialised check now requires
    actual editable content (enabled_tools / static_rules / tool_overrides).
    """
    default_row = _rules_ok(version=1, enabled=[])
    default_row["static_rules"] = ""  # untouched — the default state
    default_row["tool_overrides"] = {}

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, default_row),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "not-initialized"
    assert all("compile" not in c["path"] for c in rec.calls)
    assert not (repo / "CLAUDE.md").exists()


def test_preflight_tool_overrides_alone_count_as_initialized(repo: Path):
    """tool_overrides present is enough to treat the project as initialised.

    Covers the case where a user has curated per-tool "extra" strings but
    hasn't listed tools in enabled_tools yet.
    """
    rules = _rules_ok(version=1, enabled=[])
    rules["static_rules"] = ""
    rules["tool_overrides"] = {"claude-code": {"extra": "be terse"}}

    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, rules),
            (
                "POST",
                "/rules/compile",
                200,
                _compile_ok("claude-code", "CLAUDE.md", "content", version=1),
            ),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is True, (
        "tool_overrides is one of the three initialised signals"
    )
    assert (repo / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# 7: target tool not in enabled_tools still compiles (partial, non-versioning)
# ---------------------------------------------------------------------------


def test_preflight_target_not_in_enabled_tools_still_compiles(repo: Path):
    """Canonical enabled_tools = ['claude-code']; user resumes into codex.

    Partial compile succeeds, codex.md written, enabled_tools NOT mutated.
    """
    managed = _managed_body(7, body="# codex v{v}\n")
    rules_body = _rules_ok(version=7, enabled=["claude-code"])  # codex NOT enabled

    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, rules_body),
            ("POST", "/rules/compile", 200, _compile_ok("codex", "codex.md", managed, 7)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="codex",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is True
    assert (repo / "codex.md").read_text() == managed
    # Verify the compile POST carried tools=[codex] (partial, non-versioning).
    compile_calls = [c for c in rec.calls if "compile" in c["path"]]
    assert compile_calls[0]["json"] == {"tools": ["codex"]}
    # Verify we never issued a PUT to /rules (which would mutate enabled_tools).
    assert all(c["method"] != "PUT" for c in rec.calls)


# ---------------------------------------------------------------------------
# 8: source session provenance surfaced
# ---------------------------------------------------------------------------


def test_preflight_displays_source_session_provenance(repo: Path):
    managed = _managed_body(5)
    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed, 5)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.source_rules_version == 3
    assert result.source_rules_source == "sessionfs"
    msgs = format_messages(result)
    assert any("rules v3 (sessionfs)" in m for m in msgs)


def test_preflight_missing_provenance_uses_fallback_messaging():
    """Manifest with no instruction_provenance → fallback line."""
    result = RulesSyncResult(
        applied=True,
        skipped_reason=None,
        target_tool="claude-code",
        filename="CLAUDE.md",
        source_rules_version=None,
        source_rules_source=None,
        current_rules_version=5,
    )
    msgs = format_messages(result)
    assert any("no recorded rules provenance" in m for m in msgs)


# ---------------------------------------------------------------------------
# 9: API failure is non-fatal
# ---------------------------------------------------------------------------


def test_preflight_api_failure_is_non_fatal(repo: Path):
    """Mock API returns 500 on /rules → applied=False, reason=api-error, warning set."""
    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 500, {"detail": "boom"}),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "api-error"
    assert result.warning is not None
    assert not (repo / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    "bad_output",
    [
        # content is not a string
        {"tool": "claude-code", "filename": "CLAUDE.md", "content": None},
        {"tool": "claude-code", "filename": "CLAUDE.md", "content": 123},
        # filename is not a string
        {"tool": "claude-code", "filename": [], "content": "ok"},
        {"tool": "claude-code", "filename": {"x": 1}, "content": "ok"},
        # outputs wrapper not a list
    ],
)
def test_preflight_malformed_compile_payload_returns_api_error(
    repo: Path, bad_output
):
    """Regression: the helper must never raise on a malformed /compile payload.
    An earlier version accessed ``output["filename"]`` / ``output["content"]``
    without type validation, so a bad body (null/array/object in those fields)
    would raise TypeError before a RulesSyncResult could be returned. The
    helper's non-fatal contract must hold even when callers don't wrap it in
    a catch-all.
    """
    compile_body = {
        "version": 5,
        "created_new_version": False,
        "aggregate_hash": "x" * 64,
        "outputs": [bad_output],
    }
    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok()),
            ("POST", "/rules/compile", 200, compile_body),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "api-error"
    assert result.warning is not None
    assert not (repo / "CLAUDE.md").exists()


def test_preflight_malformed_outputs_wrapper_returns_api_error(repo: Path):
    """`outputs` itself must be a list — any other shape returns api-error
    rather than crashing the helper.
    """
    compile_body = {
        "version": 5,
        "created_new_version": False,
        "aggregate_hash": "x" * 64,
        "outputs": "not-a-list",
    }
    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok()),
            ("POST", "/rules/compile", 200, compile_body),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is False
    assert result.skipped_reason == "api-error"
    assert not (repo / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# 10: never creates a canonical version (partial compile semantics)
# ---------------------------------------------------------------------------


def test_preflight_never_creates_canonical_version(repo: Path):
    """Verify the /compile POST carries tools=[target] — the server treats that
    as a partial, non-versioning compile (see integration test
    test_partial_tool_compile_does_not_version).
    """
    managed = _managed_body(5)
    patcher, rec = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed, 5)),
        ]
    )
    with patcher:
        preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    compile_calls = [c for c in rec.calls if "compile" in c["path"]]
    assert len(compile_calls) == 1
    assert compile_calls[0]["json"] == {"tools": ["claude-code"]}


# ---------------------------------------------------------------------------
# 11: writes only the target tool file (no sibling touch)
# ---------------------------------------------------------------------------


def test_preflight_writes_only_target_tool_file(repo: Path):
    """With siblings for other tools present, only the target file is touched."""
    (repo / "CLAUDE.md").write_text("sibling-claude\n")
    (repo / "GEMINI.md").write_text("sibling-gemini\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "copilot-instructions.md").write_text("sibling-copilot\n")
    # Target: codex.md does not yet exist.

    managed = _managed_body(5, body="# codex v{v}\n")
    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5, enabled=["codex"])),
            ("POST", "/rules/compile", 200, _compile_ok("codex", "codex.md", managed, 5)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="codex",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is True
    assert (repo / "codex.md").read_text() == managed
    # Siblings untouched.
    assert (repo / "CLAUDE.md").read_text() == "sibling-claude\n"
    assert (repo / "GEMINI.md").read_text() == "sibling-gemini\n"
    assert (repo / ".github" / "copilot-instructions.md").read_text() == "sibling-copilot\n"


# ---------------------------------------------------------------------------
# 12: unmanaged detection matches `sfs rules compile`
# ---------------------------------------------------------------------------


def test_preflight_unmanaged_detection_matches_rules_compile(repo: Path, tmp_path: Path):
    """Same `_is_managed()` helper is used — a marker in the first 512 bytes
    is detected; a decoy past 512 bytes is not (matches the test assertion
    in tests/unit/test_cmd_rules.py::test_is_managed_only_scans_first_512_bytes).
    """
    from sessionfs.cli.cmd_rules import _is_managed

    managed_path = repo / "CLAUDE.md"
    managed_path.write_text(_managed_body(5))
    assert _is_managed(managed_path) is True

    decoy_path = tmp_path / "decoy.md"
    decoy_path.write_text(
        "# My rules\n" + ("filler. " * 200) +
        "\nNote: sessionfs-managed: version=1 hash=abcdef12 (past 512b).\n"
    )
    assert _is_managed(decoy_path) is False

    # And: Case B overwrite flow uses exactly that helper to decide.
    patcher, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=6)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", _managed_body(6), 6)),
        ]
    )
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
        )
    assert result.applied is True  # managed file was refreshed


# ---------------------------------------------------------------------------
# EXTRA: ownership-transition regression
# ---------------------------------------------------------------------------


def test_force_rules_ownership_transition(repo: Path):
    """`--force-rules` is a one-time permission.

    Start: hand-written (unmanaged) CLAUDE.md.
    Step 1: preflight(force=True) → Case D → overwrites with managed content.
    Step 2: preflight(force=False) → file is now managed → Case B refresh,
        NOT Case C skip.
    """
    (repo / "CLAUDE.md").write_text("# Hand-written\n")
    managed_v5 = _managed_body(5, body="# managed v{v}\n")
    managed_v6 = _managed_body(6, body="# managed v{v}\n")

    # Step 1: force=True, seed managed content.
    patcher1, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=5)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed_v5, 5)),
        ]
    )
    with patcher1:
        r1 = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(3, "sessionfs"),
            force=True,
        )
    assert r1.applied is True
    assert r1.used_force is True
    assert (repo / "CLAUDE.md").read_text() == managed_v5

    # Step 2: force=False, file is now managed — MUST refresh (Case B),
    # NOT skip as unmanaged (Case C). This is the regression.
    patcher2, _ = _patch_api(
        [
            ("GET", "/rules", 200, _rules_ok(version=6)),
            ("POST", "/rules/compile", 200, _compile_ok("claude-code", "CLAUDE.md", managed_v6, 6)),
        ]
    )
    with patcher2:
        r2 = preflight_target_tool_rules(
            project_path=str(repo),
            target_tool="claude-code",
            source_manifest=_manifest(5, "sessionfs"),
            force=False,
        )
    assert r2.applied is True, (
        "second preflight must refresh the now-managed file (Case B), "
        "not treat it as unmanaged (Case C)"
    )
    assert r2.used_force is False
    assert r2.skipped_reason is None
    assert (repo / "CLAUDE.md").read_text() == managed_v6


# ---------------------------------------------------------------------------
# Additional short-circuit cases (unsupported tool, not in git)
# ---------------------------------------------------------------------------


def test_preflight_unsupported_tool_short_circuits(tmp_path: Path):
    """cursor is resume-incapable → skipped_reason='unsupported-tool', no API."""
    patcher, rec = _patch_api([])
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(tmp_path),
            target_tool="cursor",
            source_manifest=None,
        )
    assert result.applied is False
    assert result.skipped_reason == "unsupported-tool"
    assert rec.calls == []


def test_preflight_not_in_git_short_circuits(tmp_path: Path):
    """Project path not inside a git repo → skipped_reason='not-in-git'."""
    # tmp_path has no .git. Don't init one.
    patcher, rec = _patch_api([])
    with patcher:
        result = preflight_target_tool_rules(
            project_path=str(tmp_path),
            target_tool="claude-code",
            source_manifest=None,
        )
    assert result.applied is False
    assert result.skipped_reason == "not-in-git"
    assert rec.calls == []


def test_format_messages_respects_three_line_cap():
    """Hard cap at 3 lines regardless of state."""
    r = RulesSyncResult(
        applied=True,
        skipped_reason=None,
        target_tool="claude-code",
        filename="CLAUDE.md",
        source_rules_version=3,
        source_rules_source="sessionfs",
        current_rules_version=5,
    )
    msgs = format_messages(r)
    assert len(msgs) <= 3
    assert any("Source session used rules v3 (sessionfs)" in m for m in msgs)
    assert any("Current project rules are v5" in m for m in msgs)
    assert any("Synced CLAUDE.md from SessionFS rules v5" in m for m in msgs)
