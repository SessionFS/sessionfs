"""A large pushed session for the sync-push memory regression test.

About 45 MB of messages.jsonl, mostly long tool outputs (hex noise, so it
compresses about as poorly as real output does), with lines that stress the
streamed DLP scan: non-ASCII text, redaction markers from an earlier redact
(a re-push), private-key blocks and secrets placed around every multiple of
the scanner's batch size, a pretty-printed .json member and a member with
Windows line endings.
"""

from __future__ import annotations

import io
import json
import random
import tarfile

from tests.utils.synthetic_session import build_messages

# The DLP scanner's batch size in characters (dlp._GATE_BATCH_CHARS).
BATCH_CHARS = 4 * 1024 * 1024

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA1xYlqM3v0Q6t5bX0d0Wc5o9WcQ8m3H8y0p8Q8xGZJm6l\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def special_line(i: int) -> str:
    """One JSONL message holding something the scan must get right."""
    kind = i % 5
    if kind == 0:
        text = "Grüße aus Zürich — 日本語のテキスト 🚀 " * 20 + f"AKIA{i:016d}"
    elif kind == 1:
        text = (
            'password = "[REDACTED:generic_password_assignment]" and token '
            f"[REDACTED:github_token] then secret = 'plain{i:08d}secret'"
        )
    elif kind == 2:
        text = "key file:\n" + PEM
    elif kind == 3:
        text = f"export ANTHROPIC_API_KEY=sk-ant-{i:040d}"
    else:
        text = "line one\r\nline two  ghp_" + f"{i:040d}"
    return json.dumps(
        {"msg_id": f"s{i}", "role": "user", "content": [{"type": "text", "text": text}]},
        ensure_ascii=(i % 2 == 0),
    )


def _tool_output(rng: random.Random, i: int) -> str:
    rows = [rng.randbytes(rng.randint(20, 60)).hex() for _ in range(rng.randint(60, 140))]
    return json.dumps(
        {
            "msg_id": f"t{i}",
            "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": "\n".join(rows)}],
        }
    )


def build_large_messages(target_bytes: int = 45 * 1024 * 1024) -> bytes:
    """messages.jsonl bytes with special lines spread through, several of
    them right around each multiple of the batch size."""
    rng = random.Random(4242)
    out = build_messages(600, n_high=4, n_medium=12, seed=4242).decode().splitlines()
    size = sum(len(line) + 1 for line in out)
    next_mark = BATCH_CHARS
    special = 0
    i = 0
    while size < target_bytes:
        if size >= next_mark - 3_000:
            for _ in range(5):
                out.append(special_line(special))
                size += len(out[-1]) + 1
                special += 1
            next_mark += BATCH_CHARS
        elif i % 97 == 0:
            out.append(special_line(special))
            size += len(out[-1]) + 1
            special += 1
        out.append(_tool_output(rng, i))
        size += len(out[-1]) + 1
        i += 1
    return ("\n".join(out) + "\n").encode()


def build_large_archive(session_id: str = "ses_largepush0001") -> tuple[bytes, int]:
    """(archive bytes, decompressed messages.jsonl size)."""
    messages = build_large_messages()
    manifest = json.dumps(
        {
            "sfs_version": "0.1.0",
            "session_id": session_id,
            "title": "",
            "source": {"tool": "claude-code"},
            "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
            "stats": {"message_count": 1000},
        },
        indent=2,
    ).encode()
    workspace = b'{"root_path": ".",\r\n "git_remote": "https://github.com/acme/app.git"}\r\n'
    notes = json.dumps({"note": "Bearer abcdefghijklmnopqrstuvwxyz0123"}).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("manifest.json", manifest),
            ("messages.jsonl", messages),
            ("workspace.json", workspace),
            ("notes.json", notes),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue(), len(messages)
