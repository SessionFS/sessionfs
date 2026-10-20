"""Request-path safety for MCP tools (see `sessionfs.url_path_safety`).

MCP tool arguments are chosen by the model (and therefore reachable by
prompt-injected content); every argument interpolated into an `/api/v1/...`
URL path goes through these shared helpers.
"""

from __future__ import annotations

from sessionfs.url_path_safety import _path_segment, _strict_repository_identifier

__all__ = ["_path_segment", "_strict_repository_identifier"]
