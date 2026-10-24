"""Git remote normalization shared by the CLI, MCP server and API server.

Dependency-free on purpose (stdlib only): the CLI must not import the server
package (which needs PyJWT, FastAPI, ...) just to normalize a remote.
"""

from __future__ import annotations

import re

# scheme://[userinfo@]host[:port]/path — any case, the transports git accepts.
_URL_REMOTE = re.compile(
    r"^(https?|ssh|git|git\+ssh|ssh\+git)://(?:([^@/]*)@)?[^/]+/(.+)$",
    re.IGNORECASE,
)
# scp-like [user@]host:path. The host must be longer than one character so a
# Windows drive ("c:/repo") is never read as one, and "scheme://" is excluded.
_SCP_REMOTE = re.compile(r"^(?:([^@/:]+)@)?([^/:]{2,}):(?!//)(.+)$")


def normalize_git_remote(url: str) -> str:
    """Normalize a git remote to its repository path, lowercased.

    Every URL form of one repository maps to the same value, so the same repo
    never ends up as two projects:

    git@github.com:SessionFS/sessionfs.git -> sessionfs/sessionfs
    https://github.com/SessionFS/sessionfs.git -> sessionfs/sessionfs
    ssh://git@gitlab.example.com:2222/group/sub/repo.git -> group/sub/repo
    deploy@gitlab.example.com:group/repo -> group/repo
    HTTPS://GitLab.example.com/Group/Repo/ -> group/repo
    group/repo.git -> group/repo

    Local paths and file:// remotes keep their path (minus a trailing slash
    and ``.git``). So do repositories in a user's home directory or at an
    absolute path on a personal server (``alice@box:~/app``,
    ``ssh://box/~/app``, ``box:/srv/app``): their path says nothing without
    the host, so the whole remote is kept. ``git@host:`` remotes have always
    dropped the host and still do.
    """
    if not url:
        return ""
    url = url.strip()
    m = _URL_REMOTE.match(url)
    if m:
        scheme, user, path = m.group(1).lower(), m.group(2), m.group(3)
        # ~user on a forge over https (sourcehut) is a namespace; over ssh
        # without the git user it is someone's home directory.
        if path.startswith("~") and user != "git" and not scheme.startswith("http"):
            path = url
    else:
        m = _SCP_REMOTE.match(url)
        if m is None:
            path = url
        else:
            user, path = m.group(1), m.group(3)
            if user != "git" and path.startswith(("/", "~")):
                path = url
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.lower()
