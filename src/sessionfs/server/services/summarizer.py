"""Deterministic session summary extraction + optional LLM narrative."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("sessionfs.summarizer")


@dataclass
class TestResults:
    total: int = 0
    passed: int = 0
    failed: int = 0


@dataclass
class SessionSummary:
    session_id: str = ""
    title: str = ""
    tool: str = ""
    model: str | None = None
    duration_minutes: int = 0
    message_count: int = 0
    tool_call_count: int = 0
    branch: str | None = None
    commit: str | None = None

    # Deterministic
    files_modified: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    commands_executed: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    packages_installed: list[str] = field(default_factory=list)
    errors_encountered: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)

    # LLM narrative (optional)
    what_happened: str | None = None
    key_decisions: list[str] | None = None
    outcome: str | None = None
    open_issues: list[str] | None = None
    narrative_model: str | None = None

    generated_at: str = ""


def summarize_session(
    messages: list[dict],
    manifest: dict | None = None,
    workspace: dict | None = None,
) -> SessionSummary:
    """Extract structured summary from session data without LLM calls."""
    manifest = manifest or {}
    workspace = workspace or {}
    model_info = manifest.get("model") or {}
    git_info = workspace.get("git") or {}

    tool_calls = _extract_tool_calls(messages)

    files_written = _extract_files(tool_calls, ("Write", "write"))
    files_edited = _extract_files(tool_calls, ("Edit", "edit"))
    files_read_raw = _extract_files(tool_calls, ("Read", "read", "Glob", "glob", "Grep", "grep"))
    modified = list(set(files_written + files_edited))
    read_only = list(set(files_read_raw) - set(modified))

    bash_commands = _extract_bash_commands(tool_calls)
    test_results = _extract_test_results(tool_calls)
    installs = _extract_installs(bash_commands)
    errors = _extract_errors(tool_calls)

    duration = _calc_duration(messages)

    # Detect slash-command skills
    from sessionfs.converters.skill_detector import detect_skills

    source_tool = manifest.get("source_tool", manifest.get("source", {}).get("tool", ""))
    detected_skills = detect_skills(messages, source_tool)
    skills_used = sorted(set(s.name for s in detected_skills))

    return SessionSummary(
        session_id=manifest.get("session_id", ""),
        title=manifest.get("title", "Untitled"),
        tool=manifest.get("source_tool", ""),
        model=model_info.get("model_id"),
        duration_minutes=duration,
        message_count=len(messages),
        tool_call_count=len(tool_calls),
        branch=git_info.get("branch"),
        commit=git_info.get("commit_sha", "")[:7] if git_info.get("commit_sha") else None,
        files_modified=modified,
        files_read=read_only,
        commands_executed=len(bash_commands),
        tests_run=test_results.total,
        tests_passed=test_results.passed,
        tests_failed=test_results.failed,
        packages_installed=installs,
        errors_encountered=errors[:5],
        skills_used=skills_used,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


_NARRATIVE_SYSTEM_PROMPT = """\
You summarize AI coding sessions. Given the session stats and recent messages, provide a brief narrative summary.
Respond with JSON: {"what_happened": "...", "key_decisions": ["..."], "outcome": "...", "open_issues": ["..."]}
- what_happened: 2-3 sentences describing what was accomplished
- key_decisions: list of important decisions made during the session
- outcome: 1 sentence — was the task completed, partially done, or blocked?
- open_issues: list of things left undone or needing follow-up
Only output JSON, nothing else."""


def _build_narrative_prompt(summary: SessionSummary, messages: list[dict]) -> str:
    """Build a user prompt from deterministic stats + recent assistant messages."""
    parts: list[str] = []
    parts.append("## Session stats")
    parts.append(f"- Title: {summary.title}")
    parts.append(f"- Tool: {summary.tool}, Model: {summary.model or 'unknown'}")
    parts.append(f"- Duration: {summary.duration_minutes} min, Messages: {summary.message_count}")
    parts.append(f"- Tool calls: {summary.tool_call_count}, Commands: {summary.commands_executed}")

    if summary.files_modified:
        parts.append(f"- Files modified: {', '.join(summary.files_modified[:20])}")
    if summary.tests_run > 0:
        parts.append(f"- Tests: {summary.tests_passed}/{summary.tests_run} passed, {summary.tests_failed} failed")
    if summary.packages_installed:
        parts.append(f"- Packages installed: {', '.join(summary.packages_installed[:10])}")
    if summary.errors_encountered:
        parts.append(f"- Errors: {'; '.join(summary.errors_encountered[:3])}")

    # Last 20 assistant messages (truncated)
    assistant_msgs = [
        m for m in messages if m.get("role") == "assistant"
    ][-20:]
    if assistant_msgs:
        parts.append("\n## Recent assistant messages (truncated)")
        for msg in assistant_msgs:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = " ".join(text_parts)
            if isinstance(content, str) and content.strip():
                parts.append(content[:500])

    return "\n".join(parts)


async def generate_narrative(
    summary: SessionSummary,
    messages: list[dict],
    model: str = "claude-sonnet-4",
    api_key: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
) -> SessionSummary:
    """Add LLM-generated narrative to an existing deterministic summary."""
    if not api_key:
        logger.warning("No API key provided for narrative generation — skipping")
        return summary

    from sessionfs.judge.providers import call_llm

    prompt = _build_narrative_prompt(summary, messages)

    try:
        raw = await call_llm(
            model=model,
            system=_NARRATIVE_SYSTEM_PROMPT,
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )

        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)

        summary.what_happened = data.get("what_happened")
        summary.key_decisions = data.get("key_decisions") or []
        summary.outcome = data.get("outcome")
        summary.open_issues = data.get("open_issues") or []
        summary.narrative_model = model
    except Exception:
        logger.warning("Narrative generation failed — returning summary without narrative", exc_info=True)

    return summary


def _extract_tool_calls(messages: list[dict]) -> list[dict]:
    """Extract all tool_use + tool_result pairs."""
    tool_uses: dict[str, dict] = {}
    calls: list[dict] = []

    for msg in messages:
        content = msg.get("content", "")
        blocks = content if isinstance(content, list) else []

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_use":
                tool_uses[block.get("id", "")] = block
            elif btype == "tool_result":
                use_id = block.get("tool_use_id", "")
                use = tool_uses.get(use_id, {})
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = "\n".join(
                        b.get("text", "") for b in result_content if isinstance(b, dict)
                    )
                calls.append({
                    "name": use.get("name", "unknown"),
                    "input": use.get("input", {}),
                    "output": str(result_content),
                })

    # Also handle role=tool messages
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        use_id = msg.get("tool_use_id", "")
        use = tool_uses.get(use_id, {})
        result_content = msg.get("content", "")
        if isinstance(result_content, list):
            result_content = "\n".join(
                b.get("text", "") for b in result_content if isinstance(b, dict)
            )
        calls.append({
            "name": use.get("name", msg.get("name", "unknown")),
            "input": use.get("input", {}),
            "output": str(result_content),
        })

    return calls


def _extract_files(tool_calls: list[dict], tool_names: tuple[str, ...]) -> list[str]:
    """Extract file paths from tool calls."""
    files = []
    for call in tool_calls:
        if call["name"] not in tool_names:
            continue
        inp = call.get("input", {})
        if not isinstance(inp, dict):
            continue
        for key in ("file_path", "path", "pattern", "filename"):
            val = inp.get(key, "")
            if val and isinstance(val, str) and "/" in val:
                files.append(val)
                break
    return files


def _extract_bash_commands(tool_calls: list[dict]) -> list[str]:
    """Extract commands from Bash tool calls."""
    return [
        call["input"].get("command", "")
        for call in tool_calls
        if call["name"] in ("Bash", "bash", "execute_command", "shell")
        and isinstance(call.get("input"), dict)
        and call["input"].get("command")
    ]


def _extract_test_results(tool_calls: list[dict]) -> TestResults:
    """Parse test run results from Bash outputs."""
    total = passed = failed = 0
    for call in tool_calls:
        if call["name"] not in ("Bash", "bash"):
            continue
        output = str(call.get("output", ""))

        # jest: "Tests: 5 passed, 1 failed, 6 total" (check first — more specific)
        jest_match = re.search(r"Tests:\s*(\d+)\s*passed.*?(\d+)\s*failed", output)
        if jest_match:
            passed += int(jest_match.group(1))
            failed += int(jest_match.group(2))
            total += int(jest_match.group(1)) + int(jest_match.group(2))
            continue

        # pytest: "5 passed, 1 failed in 2.3s"
        m = re.search(r"(\d+) passed", output)
        if m:
            p = int(m.group(1))
            passed += p
            total += p
        m = re.search(r"(\d+) failed", output)
        if m:
            f = int(m.group(1))
            failed += f
            total += f

        # go test: "ok" or "FAIL"
        if re.search(r"^ok\s+\S+", output, re.MULTILINE):
            passed += 1
            total += 1
        if re.search(r"^FAIL\s+\S+", output, re.MULTILINE):
            failed += 1
            total += 1

    return TestResults(total=total, passed=passed, failed=failed)


def _extract_installs(commands: list[str]) -> list[str]:
    """Extract installed packages from install commands."""
    packages: set[str] = set()
    for cmd in commands:
        if "pip install" in cmd:
            parts = cmd.split("pip install")[-1].strip().split()
            packages.update(p for p in parts if not p.startswith("-") and p not in (".", "-e"))
        elif "npm install" in cmd or "npm i " in cmd:
            parts = cmd.split("install")[-1].strip().split()
            packages.update(p for p in parts if not p.startswith("-") and p != "--save-dev")
    return sorted(packages)


def _extract_errors(tool_calls: list[dict]) -> list[str]:
    """Extract unique error messages from tool outputs."""
    errors: set[str] = set()
    for call in tool_calls:
        output = str(call.get("output", ""))
        for pattern in (r"Error:.*", r"FAILED.*", r"Traceback.*", r"Exception:.*"):
            for m in re.finditer(pattern, output):
                line = m.group(0).strip()[:200]
                if len(line) > 15:
                    errors.add(line)
    return sorted(errors)


def _calc_duration(messages: list[dict]) -> int:
    """Calculate session duration in minutes from timestamps."""
    timestamps = []
    for msg in messages:
        ts = msg.get("timestamp")
        if ts and isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                timestamps.append(dt)
            except (ValueError, TypeError):
                pass

    if len(timestamps) < 2:
        return 0

    delta = max(timestamps) - min(timestamps)
    return max(1, int(delta.total_seconds() / 60))
