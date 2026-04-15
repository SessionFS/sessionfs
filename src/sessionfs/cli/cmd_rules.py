"""`sfs rules …` — manage canonical project rules and tool-specific outputs.

Commands:
- sfs rules init     — create rules for this repo, preselect supported tools
- sfs rules edit     — edit canonical static preferences in $EDITOR
- sfs rules show     — summary of current rules + compile state
- sfs rules compile  — compile rules into tool outputs on disk
- sfs rules push     — upload canonical rules + latest version to cloud
- sfs rules pull     — download canonical rules from cloud

See `.agents/atlas-backend.md` + v0.9.9 implementation brief for details.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import typer

from sessionfs.cli.common import console, err_console, get_store_dir

rules_app = typer.Typer(
    name="rules",
    help="Manage canonical project rules and compiled tool outputs.",
    no_args_is_help=True,
)

# The 5 tools v0.9.9 supports out of the box. Kept here (not imported from
# the server side) so the CLI works when the server package isn't installed.
SUPPORTED_TOOLS: tuple[str, ...] = ("claude-code", "codex", "cursor", "copilot", "gemini")

TOOL_FILES: dict[str, str] = {
    "claude-code": "CLAUDE.md",
    "codex": "codex.md",
    "cursor": ".cursorrules",
    "copilot": ".github/copilot-instructions.md",
    "gemini": "GEMINI.md",
}

TOOL_ALIASES: dict[str, str] = {
    "codex": "codex",
    # Codex historically used AGENTS.md — still recognized for detection.
}


# ---------------------------------------------------------------------------
# Git / API helpers (matching the style of cmd_project.py)
# ---------------------------------------------------------------------------


def _get_git_remote() -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_git_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _normalize_remote(url: str) -> str:
    from sessionfs.server.github_app import normalize_git_remote
    return normalize_git_remote(url)


def _get_api_config() -> tuple[str, str]:
    from sessionfs.cli.cmd_cloud import _load_sync_config
    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)
    return cfg["api_url"], cfg["api_key"]


async def _api_request(
    method: str,
    path: str,
    api_url: str,
    api_key: str,
    json_data: dict | None = None,
    extra_headers: dict | None = None,
) -> tuple[int, dict | str, dict]:
    """Return (status_code, body, headers). Body is dict if JSON else text."""
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=json_data)
        elif method == "PUT":
            resp = await client.put(url, headers=headers, json=json_data)
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

    if resp.headers.get("content-type", "").startswith("application/json"):
        body: Any = resp.json()
    else:
        body = resp.text
    return resp.status_code, body, dict(resp.headers)


def _resolve_project_id(api_url: str, api_key: str, normalized: str) -> str:
    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key)
    )
    if status == 404:
        err_console.print(
            f"[yellow]No project found for {normalized}.[/yellow] "
            f"Run 'sfs project init' first."
        )
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)
    if not isinstance(body, dict):
        err_console.print("[red]Unexpected API response.[/red]")
        raise typer.Exit(1)
    return body["id"]


# ---------------------------------------------------------------------------
# Tool detection (existing rule files + recent session usage)
# ---------------------------------------------------------------------------


def _find_existing_rule_files(git_root: Path) -> dict[str, Path]:
    """Return {tool_slug: path} for recognized rule files present in the repo."""
    out: dict[str, Path] = {}
    for tool, rel in TOOL_FILES.items():
        p = git_root / rel
        if p.is_file():
            out[tool] = p
    # Also recognize AGENTS.md as a codex seed candidate.
    agents_md = git_root / "AGENTS.md"
    if agents_md.is_file() and "codex" not in out:
        out["codex"] = agents_md
    return out


def _recent_tool_usage(git_remote_normalized: str, days: int = 90) -> dict[str, int]:
    """Count sessions by source_tool in the local index for this git remote.

    Returns {tool_slug: count}. Empty dict on any failure — discovery is
    best-effort and must never break `rules init`.
    """
    from datetime import datetime, timedelta, timezone

    try:
        store_dir = get_store_dir()
    except Exception:
        return {}
    db_path = Path(store_dir) / "index.db"
    if not db_path.exists():
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT source_tool, COUNT(*) AS cnt FROM sessions "
            "WHERE source_tool IS NOT NULL AND created_at >= ? "
            "GROUP BY source_tool",
            (cutoff,),
        )
        counts: dict[str, int] = {}
        for row in cur.fetchall():
            tool = _canonicalize_tool(row["source_tool"])
            if not tool:
                continue
            counts[tool] = counts.get(tool, 0) + row["cnt"]
        conn.close()
        return counts
    except sqlite3.DatabaseError:
        return {}


def _canonicalize_tool(source_tool: str) -> str | None:
    """Normalize a source_tool label to one of SUPPORTED_TOOLS (or None)."""
    if not source_tool:
        return None
    s = source_tool.strip().lower()
    if s in SUPPORTED_TOOLS:
        return s
    if s in ("cc", "claude"):
        return "claude-code"
    if s in ("copilot-cli", "github-copilot"):
        return "copilot"
    if s in ("gemini-cli",):
        return "gemini"
    if s in ("cursor-ide",):
        return "cursor"
    return None


def _preselect_tools(
    existing: dict[str, Path], recent: dict[str, int]
) -> list[tuple[str, str]]:
    """Return [(tool, reason)] preselected in precedence order:
    existing file > recent usage > (no preselect).
    """
    selected: dict[str, str] = {}
    for tool in SUPPORTED_TOOLS:
        if tool in existing:
            selected[tool] = f"found {existing[tool].name}"
    for tool, count in recent.items():
        if tool in SUPPORTED_TOOLS and tool not in selected and count > 0:
            selected[tool] = f"{count} session(s) in last 90d"
    return [(t, selected[t]) for t in SUPPORTED_TOOLS if t in selected]


# ---------------------------------------------------------------------------
# Managed-file safety
# ---------------------------------------------------------------------------


_MANAGED_RE = re.compile(
    r"sessionfs[-_]managed.*?version[:=]\s*(\d+).*?hash[:=]\s*([0-9a-f]{8,64})",
    re.IGNORECASE | re.DOTALL,
)


def _is_managed(path: Path) -> bool:
    """True iff `path` has a SessionFS managed marker in its leading bytes.

    The marker must appear within the first 512 bytes of the file — compilers
    always emit it at the top. Restricting the search window prevents a
    hand-written file that happens to contain the marker phrase (e.g. a
    tutorial or copy-pasted snippet lower down) from being treated as
    SessionFS-managed and silently overwritten.
    """
    try:
        # Reading only the prefix also avoids loading large appendices into
        # memory on a mis-named file.
        with path.open("rb") as f:
            head = f.read(512)
        text = head.decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(_MANAGED_RE.search(text))


def _safe_target_path(
    git_root: Path, tool: str, filename: str | None
) -> Path | None:
    """Resolve a server-supplied compiled-output filename to a safe on-disk path.

    Defence-in-depth against a malformed/hostile compile API response that
    could otherwise make the CLI write outside the repo (path traversal, or
    overwriting an unrelated absolute path). The contract is:

      - the tool slug must be one we know about
      - we ignore the server-supplied filename and use the canonical
        ``TOOL_FILES[tool]`` value instead — that is the only filename
        SessionFS has ever advertised for each tool, and trusting the
        server adds zero product value
      - the resulting path must resolve **inside** ``git_root`` (no symlink
        escapes, no ``../`` traversal even if the canonical name is changed
        in a future version)

    Returns ``None`` if any check fails. Callers treat ``None`` as
    "skip this output silently" (the user has nothing to fix locally — the
    server is misbehaving).
    """
    if tool not in TOOL_FILES:
        return None
    canonical = TOOL_FILES[tool]
    if filename is not None and filename != canonical:
        # Surface this so a real server bug is debuggable, but never trust
        # the wire value. Stay quiet on a missing/None filename — the server
        # may legitimately omit it.
        err_console.print(
            f"[yellow]Server returned unexpected filename for {tool}: "
            f"{filename!r} (using canonical {canonical!r}).[/yellow]"
        )
    target = git_root / canonical
    try:
        resolved = target.resolve()
        root_resolved = git_root.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        err_console.print(
            f"[red]Refusing to write {canonical}: path escapes repo root.[/red]"
        )
        return None
    return target


def _gitignore_entries(git_root: Path) -> set[str]:
    p = git_root / ".gitignore"
    if not p.is_file():
        return set()
    try:
        return {
            line.strip()
            for line in p.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    except OSError:
        return set()


def _write_gitignore_entry(git_root: Path, relpath: str) -> None:
    p = git_root / ".gitignore"
    entries = _gitignore_entries(git_root)
    if relpath in entries:
        return
    line = f"{relpath}\n"
    try:
        if p.is_file():
            existing = p.read_text()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            p.write_text(existing + line)
        else:
            p.write_text(line)
    except OSError as exc:
        err_console.print(f"[yellow]Could not update .gitignore: {exc}[/yellow]")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@rules_app.command("init")
def rules_init(
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Gitignore compiled rule files (default: shared/committed to repo).",
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip interactive prompts."),
) -> None:
    """Create canonical project rules for this repo.

    Detects the git remote, scans for existing tool rule files, and checks
    recent session usage. Only the 5 supported tools are preselected
    (claude-code, codex, cursor, copilot, gemini).
    """
    git_root = _get_git_root()
    if git_root is None:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]No git remote 'origin' configured.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    if not normalized:
        err_console.print("[red]Could not parse git remote URL.[/red]")
        raise typer.Exit(1)

    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    existing = _find_existing_rule_files(git_root)
    recent = _recent_tool_usage(normalized)
    preselected = _preselect_tools(existing, recent)

    console.print(f"[bold]Repository:[/bold] {normalized}")
    console.print(f"[bold]Project:[/bold]    {project_id}")
    console.print()
    if preselected:
        console.print("Preselected tools:")
        for tool, reason in preselected:
            console.print(f"  - {tool}  [dim]({reason})[/dim]")
    else:
        console.print("[dim]No tools detected — none preselected.[/dim]")

    if not yes and not preselected:
        # Manual picker
        console.print()
        console.print(f"Supported: {', '.join(SUPPORTED_TOOLS)}")
        raw = typer.prompt(
            "Enable which tools? (comma-separated, empty = none)",
            default="",
        )
        chosen = [t.strip() for t in raw.split(",") if t.strip()]
        for t in chosen:
            if t not in SUPPORTED_TOOLS:
                err_console.print(f"[red]Unsupported tool: {t}[/red]")
                raise typer.Exit(1)
    else:
        chosen = [t for t, _ in preselected]
        if not yes and chosen:
            confirmed = typer.confirm(
                "Enable these tools?",
                default=True,
            )
            if not confirmed:
                raw = typer.prompt(
                    "Enter comma-separated list of tools "
                    f"({', '.join(SUPPORTED_TOOLS)})",
                    default=",".join(chosen),
                )
                chosen = [t.strip() for t in raw.split(",") if t.strip()]

    # Import path: offer if exactly ONE unmanaged recognized file is present
    # and we don't already have static_rules. Per brief, we do not auto-merge.
    single_import: tuple[str, Path] | None = None
    unmanaged = [
        (tool, p) for tool, p in existing.items() if not _is_managed(p)
    ]
    if len(unmanaged) == 1:
        single_import = unmanaged[0]

    # Fetch current canonical rules (always created-on-read)
    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{project_id}/rules", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]API error fetching rules: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)
    rules = body
    etag = rules["etag"]

    static_rules = rules.get("static_rules", "")
    if single_import and not static_rules.strip():
        if yes or typer.confirm(
            f"Import existing [bold]{single_import[1].name}[/bold] as canonical seed?",
            default=True,
        ):
            try:
                static_rules = single_import[1].read_text(encoding="utf-8")
            except OSError as exc:
                err_console.print(f"[yellow]Could not read {single_import[1]}: {exc}[/yellow]")

    # PUT with enabled_tools + optional imported static_rules
    update_body: dict = {"enabled_tools": chosen}
    if static_rules and static_rules != rules.get("static_rules", ""):
        update_body["static_rules"] = static_rules

    status, body, _ = asyncio.run(
        _api_request(
            "PUT",
            f"/api/v1/projects/{project_id}/rules",
            api_url, api_key,
            json_data=update_body,
            extra_headers={"If-Match": etag},
        )
    )
    if status == 409:
        err_console.print("[red]Rules were changed by another client — re-run init.[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]Failed to save rules: {status} {body}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[green]Rules initialized.[/green] Enabled: {', '.join(chosen) or 'none'}")

    if local_only and chosen:
        for tool in chosen:
            _write_gitignore_entry(git_root, TOOL_FILES[tool])
        console.print("[dim]Compiled outputs added to .gitignore (--local-only).[/dim]")
    elif chosen:
        console.print(
            "[dim]Compiled outputs are SHARED by default (committed). "
            "Use `sfs rules init --local-only` to gitignore instead.[/dim]"
        )

    console.print("\nNext: `sfs rules edit` to add project preferences, "
                  "then `sfs rules compile` to write tool files.")


@rules_app.command("edit")
def rules_edit() -> None:
    """Edit canonical static preferences in $EDITOR."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)
    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{project_id}/rules", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]API error: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)
    current = body.get("static_rules", "")
    etag = body["etag"]

    with tempfile.NamedTemporaryFile(
        suffix=".md", mode="w", delete=False, encoding="utf-8"
    ) as f:
        if not current.strip():
            f.write(
                "# Project Preferences\n\n"
                "<!-- Describe coding conventions, branch strategy, tool"
                " preferences, etc. -->\n"
            )
        else:
            f.write(current)
        temp_path = f.name

    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.run([editor, temp_path])
    except FileNotFoundError:
        err_console.print(f"[red]Editor not found: {editor}. Set $EDITOR.[/red]")
        os.unlink(temp_path)
        raise typer.Exit(1)

    try:
        with open(temp_path, encoding="utf-8") as f:
            new_content = f.read()
    finally:
        os.unlink(temp_path)

    if new_content == current:
        console.print("No changes.")
        return

    status, body, _ = asyncio.run(
        _api_request(
            "PUT",
            f"/api/v1/projects/{project_id}/rules",
            api_url, api_key,
            json_data={"static_rules": new_content},
            extra_headers={"If-Match": etag},
        )
    )
    if status == 409:
        err_console.print("[red]Rules changed by another client — re-run edit.[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]Failed to save: {status} {body}[/red]")
        raise typer.Exit(1)

    console.print(f"Rules updated ({len(new_content)} bytes).")


@rules_app.command("show")
def rules_show() -> None:
    """Show current rules + enabled tools + in-sync status."""
    git_root = _get_git_root()
    git_remote = _get_git_remote()
    if not git_remote or git_root is None:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)
    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{project_id}/rules", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]API error: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)

    console.print(f"[bold]Project:[/bold] {normalized}")
    console.print(f"[bold]Version:[/bold] {body['version']}")
    console.print(f"[bold]Enabled tools:[/bold] "
                  f"{', '.join(body.get('enabled_tools', [])) or '(none)'}")
    console.print(f"[bold]Knowledge injection:[/bold] "
                  f"{'on' if body.get('include_knowledge') else 'off'} — "
                  f"types={body.get('knowledge_types') or []}")
    console.print(f"[bold]Context injection:[/bold]   "
                  f"{'on' if body.get('include_context') else 'off'} — "
                  f"sections={body.get('context_sections') or []}")
    console.print()

    # Compare what would be compiled against disk state.
    status, body_comp, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/rules/compile",
            api_url, api_key,
            json_data={},
        )
    )
    if status >= 400:
        err_console.print(f"[yellow]Could not compile to check sync state: {status}[/yellow]")
        return
    assert isinstance(body_comp, dict)

    console.print("[bold]Compiled outputs:[/bold]")
    for out in body_comp.get("outputs", []):
        filename = out["filename"]
        disk_path = git_root / filename
        if disk_path.exists():
            # `out["hash"]` is the marker-independent body hash; the file on
            # disk includes the marker, so we compare full content byte-for-
            # byte rather than hashing the file. This matches what `sfs rules
            # compile` would have written.
            try:
                disk_bytes = disk_path.read_bytes()
            except OSError:
                disk_bytes = b""
            expected = out["content"].encode("utf-8")
            in_sync = disk_bytes == expected
            managed = _is_managed(disk_path)
            state = "in-sync" if in_sync else ("managed, out-of-date" if managed else "unmanaged")
        else:
            state = "not written"
        console.print(f"  - {out['tool']}: {filename}  "
                      f"[dim]({out['token_count']} tokens, {state})[/dim]")


@rules_app.command("compile")
def rules_compile(
    tool: str | None = typer.Option(None, "--tool", help="Compile only this tool."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write files."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite unmanaged files. Default is to refuse.",
    ),
) -> None:
    """Compile canonical rules into tool files on disk."""
    git_root = _get_git_root()
    git_remote = _get_git_remote()
    if not git_remote or git_root is None:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)
    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    body_req: dict = {}
    if tool is not None:
        if tool not in SUPPORTED_TOOLS:
            err_console.print(
                f"[red]Unsupported tool: {tool}. Supported: {', '.join(SUPPORTED_TOOLS)}[/red]"
            )
            raise typer.Exit(1)
        body_req["tools"] = [tool]
        # Partial compiles never bump the canonical version (that would
        # write a one-tool snapshot into rules_versions). Make that explicit
        # so users understand --tool is a preview/write-only operation.
        console.print(
            f"[dim]Partial compile: --tool={tool} — no canonical version bump.[/dim]"
        )

    status, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/rules/compile",
            api_url, api_key,
            json_data=body_req,
        )
    )
    if status >= 400:
        err_console.print(f"[red]Compile failed: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)

    outputs = body.get("outputs", [])
    if not outputs:
        console.print(
            "[yellow]No tools enabled. Run 'sfs rules init' to enable tools.[/yellow]"
        )
        return

    written = 0
    skipped: list[str] = []
    for out in outputs:
        out_tool = out.get("tool")
        if not isinstance(out_tool, str):
            err_console.print("[yellow]Skipping compile output with no tool slug.[/yellow]")
            continue
        target = _safe_target_path(git_root, out_tool, out.get("filename"))
        if target is None:
            # Already-explained refusal (unknown tool / path escape) — skip.
            continue
        filename = TOOL_FILES[out_tool]
        content = out.get("content")
        if not isinstance(content, str):
            err_console.print(
                f"[yellow]Skipping {filename}: server returned non-string content.[/yellow]"
            )
            continue
        if target.exists():
            if not _is_managed(target) and not force:
                skipped.append(filename)
                continue
        if dry_run:
            console.print(f"[dim]would write[/dim] {filename}  "
                          f"({out.get('token_count', 0)} tokens)")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written += 1
            console.print(f"wrote {filename}  ({out.get('token_count', 0)} tokens)")
        except OSError as exc:
            err_console.print(f"[red]Failed to write {filename}: {exc}[/red]")

    if skipped:
        console.print()
        err_console.print(
            "[yellow]Refused to overwrite unmanaged files:[/yellow]"
        )
        for f in skipped:
            err_console.print(f"  - {f}")
        err_console.print(
            "[dim]Re-run with --force to overwrite, or move the file "
            "aside first.[/dim]"
        )

    if not dry_run:
        if body.get("created_new_version"):
            console.print(f"[green]Rules version bumped to {body['version']}.[/green]")
        else:
            console.print("[dim]No content change — version not bumped.[/dim]")
        if written:
            console.print(f"[green]{written} file(s) updated.[/green]")


@rules_app.command("push")
def rules_push() -> None:
    """Push is implicit — canonical rules always live server-side. This command
    force-compiles the latest version on the server and confirms sync state.
    """
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)
    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    status, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/rules/compile",
            api_url, api_key,
            json_data={},
        )
    )
    if status >= 400:
        err_console.print(f"[red]Push/compile failed: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)
    console.print(
        f"[green]Pushed.[/green] Version {body['version']}, "
        f"new_version={body['created_new_version']}"
    )


@rules_app.command("pull")
def rules_pull() -> None:
    """Pull canonical rules from the cloud (prints summary; use compile to write)."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)
    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)

    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{project_id}/rules", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]Pull failed: {status} {body}[/red]")
        raise typer.Exit(1)
    assert isinstance(body, dict)

    console.print(json.dumps(
        {
            "version": body["version"],
            "enabled_tools": body.get("enabled_tools", []),
            "include_knowledge": body.get("include_knowledge"),
            "knowledge_types": body.get("knowledge_types"),
            "include_context": body.get("include_context"),
            "context_sections": body.get("context_sections"),
            "static_rules_len": len(body.get("static_rules", "")),
        },
        indent=2,
    ))
    console.print("\nRun `sfs rules compile` to materialize on disk.")
