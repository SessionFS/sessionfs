"""Synthetic .sfs sessions shaped like real coding sessions (GH #71).

The full-size shape matches the reported push: ~9.4k messages, ~18 MB
messages.jsonl, 35 secrets (7 high, 28 medium) spread through user text,
tool commands and tool output, with the key/value density of real sessions
(~5k scalars per MB).
"""

from __future__ import annotations

import io
import json
import random
import tarfile

FULL_MESSAGES = 9435
FULL_HIGH = 7
FULL_MEDIUM = 28

_CODE = '''def handler(request, *, timeout=30):
    """Fetch the "user" record and render it."""
    user = db.get(request.args["id"])  # TODO: cache
    if not user:
        raise NotFound(f"user {request.args['id']!r} missing")
    data = {"name": user.name, "email": user.email, "tags": [t.slug for t in user.tags]}
    return json.dumps(data, indent=2)
'''

_PROSE = (
    "I'll look at the failing test first, then check how the retry loop handles a "
    "409 from the server. The config is read in load_settings() and passed down "
    "to the client; let's make sure the timeout is honoured end to end. "
)

_WORDS = (
    "user", "retry", "timeout", "client", "config", "server", "token_count",
    "session", "handler", "queue", "worker", "etag",
)


def _secrets(rng: random.Random, n_high: int, n_medium: int) -> list[str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    def tok(n: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    high = [
        lambda: f"export OPENAI_API_KEY=sk-proj-{tok(40)}",
        lambda: f"token ghp_{tok(36)} was rotated",
        lambda: f"DATABASE_URL=postgres://app:{tok(16)}@db.internal:5432/prod",
        lambda: f"ANTHROPIC_API_KEY=sk-ant-{tok(48)}",
        lambda: f"stripe key sk_live_{tok(28)}",
    ]
    medium = [
        lambda: f'password = "{tok(14)}"',
        lambda: f"Authorization: Bearer {tok(40)}",
        lambda: f"eyJ{tok(20)}.eyJ{tok(30)}.{tok(30)}",
        lambda: f'api_key: "{tok(24)}"',
    ]
    return [high[i % len(high)]() for i in range(n_high)] + [
        medium[i % len(medium)]() for i in range(n_medium)
    ]


def build_messages(
    n_messages: int = FULL_MESSAGES,
    n_high: int = FULL_HIGH,
    n_medium: int = FULL_MEDIUM,
    seed: int = 71,
) -> bytes:
    """messages.jsonl bytes: user text, assistant tool calls, tool output."""
    rng = random.Random(seed)
    secrets = _secrets(rng, n_high, n_medium)
    slots = sorted(rng.sample(range(n_messages), min(len(secrets), n_messages)))
    secret_at = dict(zip(slots, secrets))
    lines: list[str] = []
    tool_use = 0
    for i in range(n_messages):
        extra = secret_at.get(i)
        kind = i % 3
        if kind == 0:
            text = _PROSE * rng.randint(1, 3) + " ".join(
                format(rng.getrandbits(24), "x") for _ in range(rng.randint(5, 60))
            )
            if extra:
                text += "\nI found this in the logs: " + extra
            msg: dict = {
                "msg_id": f"m{i}",
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "timestamp": "2026-10-01T12:00:00Z",
            }
        elif kind == 1:
            tool_use += 1
            cmd = f"grep -rn 'retry' src/ --include='*.py' | head -{rng.randint(5, 50)}"
            if extra:
                cmd += f" && echo '{extra}'"
            msg = {
                "msg_id": f"m{i}",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": _PROSE * rng.randint(1, 2)},
                    {"type": "text", "text": _PROSE},
                    {
                        "type": "tool_use",
                        "tool_name": "Bash",
                        "tool_use_id": f"tu_{tool_use}",
                        "input": {"command": cmd, "description": "Search for retries", "timeout": 120000},
                    },
                ],
                "timestamp": "2026-10-01T12:00:01Z",
                "model_used": "claude-opus-4-6",
                "usage": {
                    "input_tokens": rng.randint(100, 9000),
                    "output_tokens": rng.randint(10, 900),
                },
            }
        else:
            body = "".join(
                _CODE.replace("handler", rng.choice(_WORDS) + "_" + format(rng.getrandbits(32), "x"))
                .replace("cache", " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 12))))
                .replace("30", str(rng.randint(1, 99999)))
                for _ in range(rng.randint(4, 14))
            )
            if extra:
                body += "\n# config\n" + extra + "\n"
            msg = {
                "msg_id": f"m{i}",
                "role": "tool",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"tu_{tool_use}", "content": body, "is_error": False}
                ],
                "timestamp": "2026-10-01T12:00:02Z",
            }
        lines.append(json.dumps(msg))
    return ("\n".join(lines) + "\n").encode()


def build_archive(session_id: str, messages: bytes, n_messages: int) -> bytes:
    """A .sfs tar.gz with manifest, messages, workspace and tools members."""
    manifest = json.dumps(
        {
            "sfs_version": "0.1.0",
            "session_id": session_id,
            "title": "Fix retry handling",
            "tags": ["perf"],
            "source": {"tool": "claude-code", "tool_version": "1.0.0", "original_session_id": "abc"},
            "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
            "stats": {
                "message_count": n_messages,
                "turn_count": n_messages // 3,
                "tool_use_count": n_messages // 3,
            },
        }
    ).encode()
    workspace = json.dumps(
        {
            "root_path": ".",
            "git_remote": "https://github.com/acme/app.git",
            "git_branch": "main",
            "git_commit": "abc123",
        }
    ).encode()
    tools = json.dumps({"tools": [{"name": "Bash"}, {"name": "Read"}]}).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("manifest.json", manifest),
            ("messages.jsonl", messages),
            ("workspace.json", workspace),
            ("tools.json", tools),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def build_session(session_id: str, scale: float = 1.0, secret_scale: float | None = None) -> bytes:
    """An archive at ``scale`` x the GH #71 shape; secrets scale with size
    unless ``secret_scale`` is given."""
    n = max(3, int(FULL_MESSAGES * scale))
    s = scale if secret_scale is None else secret_scale
    return build_archive(
        session_id,
        build_messages(n, n_high=round(FULL_HIGH * s), n_medium=round(FULL_MEDIUM * s)),
        n,
    )
