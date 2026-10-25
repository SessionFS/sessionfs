"""Extract session archive contents from a ``.sfs`` tar.gz blob.

Kept free of any route/request dependency so the session helper tools and
the summary/serve paths can read the same archive shape without importing
``routes/sessions.py``.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile

logger = logging.getLogger("sessionfs.archive")


def extract_archive(data: bytes) -> dict:
    """Extract messages, manifest, workspace, and tools from a .sfs archive.

    Returns ``{"messages": list[dict], "message_indexes": list[int],
    "manifest": dict, "workspace": dict|None, "tools": dict|None}``.

    ``message_indexes[i]`` is the 0-based line number of ``messages[i]`` in
    ``messages.jsonl``, counting every line (blank and malformed included).
    Only lines that parse as JSON objects are kept; blank lines and lines that
    parse to a non-dict value are skipped (but still consume a line number).
    A member that fails to parse is skipped rather than aborting the archive.
    """
    messages: list[dict] = []
    message_indexes: list[int] = []
    manifest: dict = {}
    workspace: dict | None = None
    tools: dict | None = None

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                basename = member.name.rsplit("/", 1)[-1] if "/" in member.name else member.name
                f = tar.extractfile(member)
                if f is None:
                    continue
                if basename == "messages.jsonl":
                    line_index = 0
                    for line in f:
                        idx = line_index
                        line_index += 1
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            value = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, dict):
                            messages.append(value)
                            message_indexes.append(idx)
                elif basename == "manifest.json":
                    manifest = _loads_object(f.read())
                elif basename == "workspace.json":
                    workspace = _loads_object(f.read())
                elif basename == "tools.json":
                    tools = _loads_object(f.read())
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Failed to extract session archive: %s", exc)

    return {
        "messages": messages,
        "message_indexes": message_indexes,
        "manifest": manifest,
        "workspace": workspace,
        "tools": tools,
    }


def _loads_object(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None
