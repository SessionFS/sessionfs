"""Git remote normalization shared by the CLI, MCP server and API server.

Dependency-free on purpose (stdlib only): the CLI must not import the server
package (which needs PyJWT, FastAPI, ...) just to normalize a remote.
"""

from __future__ import annotations

import re


def normalize_git_remote(url: str) -> str:
    """Normalize git remote URL to 'owner/repo' format (lowercase).

    git@github.com:SessionFS/sessionfs.git -> sessionfs/sessionfs
    https://github.com/SessionFS/sessionfs.git -> sessionfs/sessionfs
    https://github.com/SessionFS/sessionfs -> sessionfs/sessionfs
    """
    if not url:
        return ""
    url = url.strip()
    # SSH format: git@github.com:owner/repo.git
    m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1).lower()
    # HTTPS format
    m = re.match(r"https?://[^/]+/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1).lower()
    return url.lower()
