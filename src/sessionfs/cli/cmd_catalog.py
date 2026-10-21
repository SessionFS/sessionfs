"""`sfs catalog` - native per-tool marketplace catalogs (marketplace v2 §7).

SessionFS hosts each catalog as a read-only git repository. A tool adds it
with its OWN plugin command; git authenticates through the scoped
`sfs catalog credential` helper, so the API key never appears in a URL.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

catalog_app = typer.Typer(
    name="catalog",
    help="Add SessionFS native plugin catalogs to Claude Code, Copilot, Codex, Cursor and Gemini.",
    no_args_is_help=True,
)
harness_app = typer.Typer(
    name="harness",
    help="Opt-in works-in verification harness (sandboxed jobs only).",
    no_args_is_help=True,
)
catalog_app.add_typer(harness_app, name="harness")

TOOLS = ("claude-code", "copilot", "codex", "cursor", "gemini", "antigravity")
# Tools whose add command we may run for the user (documented CLI forms).
_RUNNABLE = {
    "claude-code": ("claude", "plugin", "marketplace", "add"),
    "copilot": ("copilot", "plugin", "marketplace", "add"),
    "codex": ("codex", "plugin", "marketplace", "add"),
}
_STEP_LABELS = {"add_hosted": "add (SessionFS host)"}


def _auth() -> Any:
    from sessionfs.profiles import resolve_auth

    auth = resolve_auth()
    if not auth.api_key:
        err_console.print("[red]Not authenticated.[/red] Run [bold]sfs auth login[/bold] first.")
        raise typer.Exit(1)
    return auth


def _request(method: str, path: str, payload: dict | None = None) -> Any:
    import httpx

    auth = _auth()
    url = f"{auth.api_url.rstrip('/')}/api/v1{path}"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.request(
                method, url, headers={"Authorization": f"Bearer {auth.api_key}"}, json=payload
            )
    except httpx.RequestError as exc:
        err_console.print(f"[red]Can't reach the SessionFS API at {auth.api_url}.[/red] ({type(exc).__name__})")
        raise typer.Exit(1) from exc
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.status_code >= 400:
        err_console.print(Text(format_api_error(body, response.status_code), style="red"))
        raise typer.Exit(1)
    return body


def _pick(items: list[dict], org: str | None, personal: bool) -> dict:
    if org and personal:
        raise typer.BadParameter("Use either --org or --personal, not both")
    if org:
        wanted = ("org", org)
    elif personal:
        wanted = ("user", None)
    else:
        wanted = ("public", None)
    for item in items:
        if item.get("scope") == wanted[0] and (wanted[1] is None or item.get("scope_id") == wanted[1]):
            return item
    scope = f"org {org}" if org else ("personal" if personal else "public")
    err_console.print(
        f"[red]No {scope} catalog is available to you.[/red] "
        "Org catalogs need Team or above; personal catalogs need Starter or above."
    )
    raise typer.Exit(1)


def helper_command() -> str:
    """The git credential.helper value that runs this CLI's helper."""
    sfs = shutil.which("sfs")
    if sfs:
        return f"!{shlex.quote(sfs)} catalog credential"
    return f"!{shlex.quote(sys.executable)} -m sessionfs.cli.main catalog credential"


def credential_config_key(api_url: str) -> str:
    from sessionfs.skills.catalog_credential import origin_of

    scheme, host = origin_of(api_url)
    return f"credential.{scheme}://{host}.helper"


def _install_helper(api_url: str) -> None:
    """Configure ``credential.<origin>.helper`` as an empty value (which
    resets the helper list git built from config read BEFORE it, e.g. a
    system-wide osxkeychain/manager/store helper) followed by ours, so for
    the SessionFS origin only our helper is consulted and no other helper
    is asked to ``store`` the key. The section is (re)written at the END of
    the global config so a generic ``credential.helper`` earlier in the file
    is reset too. Helpers configured after it (a repository's own config,
    ``-c`` on the command line) are not: ``helper_order_problem`` checks."""
    key = credential_config_key(api_url)
    if helper_order_problem(api_url, scope=["--global"]) is None:
        return
    section = key.removesuffix(".helper")
    subprocess.run(["git", "config", "--global", "--remove-section", section],  # noqa: S603
                   check=False, capture_output=True)
    for value in ("", helper_command()):
        subprocess.run(["git", "config", "--global", "--add", key, value], check=True)  # noqa: S603
    console.print(Text(f"Installed the git credential helper for {api_url} only ({key}).", style="dim"))


def _is_our_helper(value: str) -> bool:
    return value == helper_command() or value.rstrip().endswith(" catalog credential")


def catalog_probe_url(api_url: str) -> str:
    """A URL of the shape git requests for a catalog (helpers are matched
    against the URL, path included)."""
    return f"{api_url.rstrip('/')}/api/v1/git/public/sessionfs.git"


def _is_full_url(value: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return bool(parts.scheme) and bool(parts.hostname)


def _applies(subsection: str | None, url: str, cache: dict[str | None, bool]) -> bool:
    """Whether a ``credential.<subsection>.helper`` entry applies to ``url``,
    decided by git itself (``--get-urlmatch``: scheme/host case, ``*.``
    wildcards, ``user@``, ports and path prefixes all follow git's rules)."""
    if subsection is None:
        return True
    if subsection not in cache:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="sfs-urlmatch-") as temp:
            probe = os.path.join(temp, "config")
            written = subprocess.run(  # noqa: S603
                ["git", "config", "--file", probe, f"credential.{subsection}.helper", "probe"],
                capture_output=True, text=True, check=False,
            )
            matched = subprocess.run(  # noqa: S603
                ["git", "config", "--file", probe, "--get-urlmatch", "credential.helper", url],
                capture_output=True, text=True, check=False,
            )
            # Unparseable, or a partial URL git's credential matching still
            # applies (e.g. a bare host, or "https://"), which --get-urlmatch
            # does not report: count it (fail towards a warning).
            cache[subsection] = (
                written.returncode != 0 or matched.returncode == 0 or not _is_full_url(subsection)
            )
    return cache[subsection]


def helper_order_problem(api_url: str, *, scope: list[str] | None = None) -> str | None:
    """None when, among ALL credential-helper entries that apply to the
    SessionFS catalog URL (generic ``credential.helper`` and every
    ``credential.<url>.helper`` git would match, wildcards and path or
    user variants included), the LAST ones git reads are an empty reset
    followed only by our helper (in git's own order: system, global,
    repository, command line). Otherwise a warning naming the exact fix."""
    key = credential_config_key(api_url)
    subsection = key[len("credential."):-len(".helper")]
    result = subprocess.run(  # noqa: S603
        ["git", "config", *(scope or []), "--show-origin", "-z", "--get-regexp", r"^credential\..*helper$"],
        capture_output=True, text=True, check=False,
    )
    tokens = result.stdout.split("\0")
    url = catalog_probe_url(api_url)
    cache: dict[str | None, bool] = {}
    entries: list[tuple[str, str, str]] = []
    for origin, pair in zip(tokens[0::2], tokens[1::2]):
        name, _, value = pair.partition("\n")
        lowered = name.lower()
        if lowered == "credential.helper":
            sub = None
        elif lowered.startswith("credential.") and lowered.endswith(".helper"):
            sub = name[len("credential."):-len(".helper")]
        else:
            continue
        if _applies(sub, url, cache):
            entries.append((origin, name, value))
    resets = [i for i, (_, _name, value) in enumerate(entries) if value == ""]
    helper = helper_command()
    fix = (
        f"git config --global --remove-section {shlex.quote(key.removesuffix('.helper'))} && "
        f"git config --global --add {shlex.quote(key)} '' && "
        f"git config --global --add {shlex.quote(key)} {shlex.quote(helper)}"
    )
    if not resets:
        return f"No credential-helper reset for {subsection}: other git credential helpers may store your key. Fix: {fix}"
    later = [(origin, name, value) for origin, name, value in entries[resets[-1] + 1:] if not _is_our_helper(value)]
    if not later:
        if not any(_is_our_helper(value) for _, _, value in entries[resets[-1] + 1:]):
            return f"The SessionFS credential helper is not configured after the reset for {subsection}. Fix: {fix}"
        return None
    # The reset must come after the LAST foreign helper: re-write our section
    # at the end of the file that holds it.
    origin, name, value = later[-1]
    if origin.startswith("file:"):
        where = os.path.abspath(origin.removeprefix("file:"))
        path = shlex.quote(where)
        fix_later = (
            f"git config --file {path} --remove-section {shlex.quote(key.removesuffix('.helper'))}; "
            f"git config --file {path} --add {shlex.quote(key)} '' && "
            f"git config --file {path} --add {shlex.quote(key)} {shlex.quote(helper)}"
        )
    else:
        where = origin or "the command line"
        fix_later = f"remove the {name} setting from {where}"
    return (
        f"git consults another credential helper ({name} = {value!r} in {where}) after the SessionFS reset "
        f"for {subsection}, so it may receive or store your key. Fix: {fix_later}"
    )


@catalog_app.command("list")
@handle_errors
def list_catalogs(json_output: bool = typer.Option(False, "--json", help="Print JSON")) -> None:
    """List the native catalogs you can add, with clone URLs."""
    body = _request("GET", "/marketplace/catalogs")
    if json_output:
        console.print_json(json.dumps(body, sort_keys=True))
        return
    table = Table(title="Native catalogs")
    for column in ("Scope", "Marketplace", "Plugins", "Clone URL"):
        table.add_column(column)
    for item in body.get("items", []):
        scope = item["scope"] if not item.get("scope_id") else f"{item['scope']}:{item['scope_id']}"
        plugins = "-" if item.get("plugin_count") is None else str(item["plugin_count"])
        table.add_row(Text(scope), Text(item["marketplace"]), Text(plugins), Text(item["clone_url"]))
    console.print(table)


@catalog_app.command("add")
@handle_errors
def add_catalog(
    tool: str = typer.Option(..., "--for", help="claude-code | copilot | codex | cursor | gemini | antigravity"),
    org: str | None = typer.Option(None, "--org", help="Org catalog (Team+)"),
    personal: bool = typer.Option(False, "--personal", help="Your personal catalog (Starter+)"),
    run: bool = typer.Option(False, "--run", help="Run the tool's add command instead of printing it"),
    hosted: bool = typer.Option(
        False, "--hosted", help="With --run: add the SessionFS-hosted catalog even when a GitHub mirror exists",
    ),
    credential_helper: bool = typer.Option(
        True, "--credential-helper/--no-credential-helper",
        help="Configure git to authenticate to this SessionFS host via `sfs catalog credential`",
    ),
) -> None:
    """Print (or run) the tool's own command to add a SessionFS catalog."""
    if tool not in TOOLS:
        raise typer.BadParameter(f"--for must be one of: {', '.join(TOOLS)}")
    if hosted and not run:
        raise typer.BadParameter("--hosted only applies with --run", param_hint="--hosted")
    auth = _auth()
    item = _pick(_request("GET", "/marketplace/catalogs").get("items", []), org, personal)
    commands = item.get("tools", {}).get(tool)
    if not commands:
        err_console.print(f"[red]{tool} has no native catalog for this scope.[/red] Gemini installs the public mirror only.")
        raise typer.Exit(1)
    if credential_helper and shutil.which("git"):
        _install_helper(auth.api_url)
        problem = helper_order_problem(auth.api_url)
        if problem:
            err_console.print(Text(f"Warning: {problem}", style="yellow"), soft_wrap=True)
    mirror = item.get("mirror") if isinstance(item.get("mirror"), dict) else None
    console.print(Text(f"{item['marketplace']}  ({item['clone_url']})", style="bold"), soft_wrap=True)
    if mirror:
        console.print(Text(f"Keyless GitHub mirror: {mirror.get('url')}", style="dim"), soft_wrap=True)
    for label, command in commands.items():
        # soft_wrap: a copy-pasted command must never contain an inserted newline.
        shown = _STEP_LABELS.get(label, label)
        console.print(Text(f"  {shown}: ", style="dim") + Text(command), soft_wrap=True)
    if run:
        if tool not in _RUNNABLE:
            err_console.print(f"[yellow]--run is not available for {tool}; use the command above.[/yellow]")
            raise typer.Exit(1)
        source = mirror["repo"] if mirror and mirror.get("repo") and not hosted else item["clone_url"]
        argv = [*_RUNNABLE[tool], source]
        if shutil.which(argv[0]) is None:
            err_console.print(f"[red]{argv[0]} is not on PATH.[/red]")
            raise typer.Exit(1)
        raise typer.Exit(subprocess.run(argv, check=False).returncode)  # noqa: S603


@catalog_app.command("doctor")
@handle_errors
def doctor() -> None:
    """Check that git sends your SessionFS key only through `sfs catalog credential`.

    Run it inside a repository too: that repository's own git config is
    checked as well."""
    if shutil.which("git") is None:
        err_console.print("[red]git is not installed.[/red]")
        raise typer.Exit(1)
    auth = _auth()
    problem = helper_order_problem(auth.api_url)
    if problem:
        err_console.print(Text(problem, style="yellow"), soft_wrap=True)
        raise typer.Exit(1)
    console.print(Text(f"OK: for {auth.api_url} git consults only the SessionFS credential helper.",
                       style="green"))


@catalog_app.command("credential", hidden=True)
def credential(operation: str = typer.Argument("get")) -> None:
    """git credential helper (git calls this; scoped to the SessionFS API host).

    Answers ``get`` only; ``store`` and ``erase`` are ignored (the key is
    never written to any credential store)."""
    from sessionfs.profiles import resolve_auth
    from sessionfs.skills.catalog_credential import credential_response, parse_request

    request = parse_request(sys.stdin.read())
    if operation != "get":
        return
    auth = resolve_auth()
    sys.stdout.write(credential_response(request, api_url=auth.api_url, api_key=auth.api_key))


# Environment variables that make git print request details: dropped from
# every git command the export runs.
_TRACE_ENV_PREFIXES = ("GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_REDACT")
# Overrides of the ssh command, prompt helpers and config: dropped from the
# SessionFS clone (its source and auth are fixed) and, with --ssh-key-file,
# from the destination commands. Without --ssh-key-file the destination uses
# the caller's own git setup, these included.
_OVERRIDE_ENV_PREFIXES = ("GIT_SSH", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_")
_MAX_KEY_FILE_BYTES = 64 * 1024
# Bounded so a scheduled run always ends inside its job timeout
# (clone + destination read + push <= 12 minutes).
_CLONE_TIMEOUT = 300
_LS_REMOTE_TIMEOUT = 120
_PUSH_TIMEOUT = 300
# ssh splits option values on whitespace and expands % and ~ in -i and
# UserKnownHostsFile: the files it is given live under a path made only of
# these characters.
_SSH_SAFE_PATH = re.compile(r"^[A-Za-z0-9/._-]+$")


def _clean_git_env(*, strip_overrides: bool) -> dict[str, str]:
    """The caller's environment without tracing (and, when asked, without
    ssh / prompt / config overrides); git never prompts on a terminal."""
    prefixes = _TRACE_ENV_PREFIXES + (_OVERRIDE_ENV_PREFIXES if strip_overrides else ())
    env = {key: value for key, value in os.environ.items() if not key.startswith(prefixes)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_auth_env(api_url: str, api_key: str) -> dict[str, str]:
    """Pass the key to ONE git invocation through GIT_CONFIG_* env vars (not
    argv, not a URL), scoped to the SessionFS origin."""
    from sessionfs.skills.catalog_credential import origin_of

    scheme, host = origin_of(api_url)
    env = _clean_git_env(strip_overrides=True)
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{scheme}://{host}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {api_key}",
        }
    )
    return env


def _scrub(text: str, secrets_: list[str]) -> str:
    """Last 500 characters of git output with every known secret removed
    (and any line that looks like private key material)."""
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "[redacted]")
    lines = ["[redacted]" if "PRIVATE KEY" in line or "Authorization" in line else line
             for line in text.strip().splitlines()]
    return "\n".join(lines)[-500:]


def _is_scp_like(remote: str) -> bool:
    """git's own rule: no ``://`` and a colon before the first slash means
    ``[user@]host:path`` over SSH."""
    if "://" in remote:
        return False
    colon = remote.find(":")
    slash = remote.find("/")
    return colon > 0 and (slash == -1 or colon < slash)


def _is_ssh_remote(remote: str) -> bool:
    from urllib.parse import urlsplit

    if "://" in remote:
        return urlsplit(remote).scheme == "ssh"
    return _is_scp_like(remote)


def _resolve_remote(remote: str) -> str:
    """Refuse remotes that carry credentials or pick an unusual transport,
    and return the remote every git command uses: URLs and ``host:path``
    unchanged, a local path made absolute (so the destination read and the
    push, which run in different directories, reach the same repository).
    A bare name such as ``origin`` is refused: inside the temporary clone it
    would name the SessionFS catalog itself. Error messages never echo the
    remote (it may hold a token)."""
    from urllib.parse import urlsplit

    if not remote or remote.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in remote):
        raise typer.BadParameter("invalid remote", param_hint="--to")
    if "::" in remote.split("/", 1)[0]:
        raise typer.BadParameter("git remote helpers (<transport>::<address>) are not allowed", param_hint="--to")
    if "://" in remote:
        try:
            parts = urlsplit(remote)
            parts.port  # noqa: B018 - raises on a malformed port
        except ValueError:
            raise typer.BadParameter("invalid remote URL", param_hint="--to") from None
        if parts.scheme not in {"https", "ssh", "file"}:
            raise typer.BadParameter("the remote must be an https://, ssh:// or file:// URL, user@host:path, "
                                     "or a local path", param_hint="--to")
        if parts.password is not None or (parts.scheme == "https" and parts.username is not None):
            raise typer.BadParameter(
                "credentials in the remote URL are not allowed: use your git credential helper, "
                "or an SSH remote with --ssh-key-file",
                param_hint="--to",
            )
        return remote
    if _is_scp_like(remote):
        if ":" in remote.split("@", 1)[0] and "@" in remote:
            raise typer.BadParameter("credentials in the remote are not allowed", param_hint="--to")
        return remote
    if "/" not in remote:
        raise typer.BadParameter("a bare remote name is not allowed: pass a URL, user@host:path, "
                                 "or a path containing '/' (e.g. ./mirror.git)", param_hint="--to")
    return os.path.abspath(remote)


def _ssh_temp_base() -> str:
    """A temporary-file base whose path ssh takes literally."""
    for base in (tempfile.gettempdir(), "/tmp"):  # noqa: S108 - fallback base only; mkdtemp below
        if _SSH_SAFE_PATH.match(base) and os.path.isdir(base):
            return base
    raise typer.BadParameter(
        "no temporary directory with a plain path (letters, digits, / . _ -) for the SSH key: "
        "set TMPDIR to one",
        param_hint="--ssh-key-file",
    )


def _private_copy(source: str, directory: str, name: str, *, max_bytes: int) -> str:
    """Copy a key or known_hosts file into a 0600 file the push uses (ssh
    refuses keys readable by others, and mounted secrets often are). ssh only
    ever sees the copy, whose path is checked against ``_SSH_SAFE_PATH``."""
    try:
        with open(source, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise typer.BadParameter(f"cannot read {name} ({type(exc).__name__})") from None
    if not data or len(data) > max_bytes:
        raise typer.BadParameter(f"{name} is empty or too large")
    path = os.path.join(directory, name)
    if not _SSH_SAFE_PATH.match(path):
        raise typer.BadParameter("unsafe temporary path for the SSH files", param_hint="--ssh-key-file")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    del data
    return path


def _push_env(ssh_key: str | None, known_hosts: str | None) -> dict[str, str]:
    """Environment of the destination read and push. Without a deploy key:
    the caller's own git/ssh setup (tracing removed). With one: only that
    key, and none of the caller's ssh, prompt or config overrides."""
    if not ssh_key:
        return _clean_git_env(strip_overrides=False)
    env = _clean_git_env(strip_overrides=True)
    env.pop("SSH_AUTH_SOCK", None)
    for path in (ssh_key, known_hosts or ""):
        if not _SSH_SAFE_PATH.match(path):
            raise typer.BadParameter("unsafe temporary path for the SSH files", param_hint="--ssh-key-file")
    env["GIT_SSH_COMMAND"] = " ".join(
        (
            "ssh", "-F", "/dev/null", "-i", ssh_key,
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
        )
    )
    return env


def _run_git(argv: list[str], *, env: dict[str, str], timeout: int, what: str,
             secrets_: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git; a timeout or a non-zero exit prints ``what`` failed (output
    scrubbed) and exits 1."""
    try:
        result = subprocess.run(  # noqa: S603
            argv, env=env, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        err_console.print(Text(f"{what}: timed out after {timeout} s", style="red"))
        raise typer.Exit(1) from None
    if result.returncode != 0:
        err_console.print(Text(f"{what}: {_scrub(result.stderr, secrets_)}", style="red"))
        raise typer.Exit(1)
    return result


def _remote_head(target: str, remote: str, branch: str, env: dict[str, str], secrets_: list[str]) -> str | None:
    """The commit the destination branch points at (None when it does not
    exist), read from inside the clone the push runs in. Exits 1 when the
    destination cannot be read."""
    ref = f"refs/heads/{branch}"
    listed = _run_git(["git", "-C", target, "ls-remote", "--", remote, ref], env=env,
                      timeout=_LS_REMOTE_TIMEOUT, what="Could not read the destination", secrets_=secrets_)
    for line in listed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return None


@catalog_app.command("export")
@handle_errors
def export_catalog(
    to: str = typer.Option(..., "--to", help="Destination git remote you own (e.g. git@github.com:org/repo.git)"),
    org: str | None = typer.Option(None, "--org", help="Export an org catalog (Team+)"),
    personal: bool = typer.Option(False, "--personal", help="Export your personal catalog"),
    branch: str = typer.Option("main", "--branch", help="Destination branch (overwritten)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Clone and show what would be pushed"),
    ssh_key_file: str | None = typer.Option(
        None, "--ssh-key-file", envvar="SFS_CATALOG_EXPORT_SSH_KEY_FILE",
        help="Private SSH deploy key for the push only (needs an SSH remote; no agent, no other keys)",
    ),
    known_hosts: str | None = typer.Option(
        None, "--known-hosts", envvar="SFS_CATALOG_EXPORT_KNOWN_HOSTS",
        help="known_hosts file for --ssh-key-file (default: GitHub's published host keys)",
    ),
) -> None:
    """Mirror a SessionFS catalog to a git remote you own (D4b).

    The destination branch is OVERWRITTEN with the generated catalog commit
    (generated commits have no shared history). Nothing is pushed when the
    branch already points at that commit. Without --ssh-key-file the push
    uses your own git credentials; with it, ONLY that key, over SSH with
    strict host-key checking. Runs unattended (SESSIONFS_API_KEY /
    SESSIONFS_API_URL): git never prompts, and any failure exits 1."""
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
        raise typer.BadParameter("invalid branch name", param_hint="--branch")
    to = _resolve_remote(to)
    if known_hosts and not ssh_key_file:
        raise typer.BadParameter("--known-hosts only applies with --ssh-key-file", param_hint="--known-hosts")
    if ssh_key_file and not _is_ssh_remote(to):
        raise typer.BadParameter("--ssh-key-file needs an SSH remote (git@host:owner/repo.git or ssh://...)",
                                 param_hint="--to")
    temp_base = _ssh_temp_base() if ssh_key_file else None
    if shutil.which("git") is None:
        err_console.print("[red]git is not installed.[/red]")
        raise typer.Exit(1)
    if ssh_key_file and shutil.which("ssh") is None:
        err_console.print("[red]ssh is not installed (needed for --ssh-key-file).[/red]")
        raise typer.Exit(1)
    auth = _auth()
    item = _pick(_request("GET", "/marketplace/catalogs").get("items", []), org, personal)
    with tempfile.TemporaryDirectory(prefix="sfs-catalog-export-", dir=temp_base) as temp:
        key_copy = hosts_copy = None
        secrets_ = [auth.api_key]
        if ssh_key_file:
            from sessionfs.skills.catalog_mirror import GITHUB_KNOWN_HOSTS

            os.chmod(temp, 0o700)
            key_copy = _private_copy(ssh_key_file, temp, "deploy_key", max_bytes=_MAX_KEY_FILE_BYTES)
            hosts_copy = _private_copy(known_hosts or str(GITHUB_KNOWN_HOSTS), temp, "known_hosts",
                                       max_bytes=1024 * 1024)
        target = os.path.join(temp, "catalog.git")
        _run_git(["git", "clone", "--bare", "--quiet", "--", item["clone_url"], target],
                 env=_git_auth_env(auth.api_url, auth.api_key), timeout=_CLONE_TIMEOUT,
                 what="Clone failed", secrets_=secrets_)
        head = _run_git(["git", "-C", target, "rev-parse", "HEAD"], env=_clean_git_env(strip_overrides=True),
                        timeout=60, what="Could not read the clone", secrets_=secrets_).stdout.strip()
        console.print(Text(f"Catalog {item['marketplace']} at {head}"), soft_wrap=True)
        if dry_run:
            console.print(Text(f"Would push {head} to {to} (branch {branch}, overwriting it)."), soft_wrap=True)
            return
        env = _push_env(key_copy, hosts_copy)
        current = _remote_head(target, to, branch, env, secrets_)
        if current == head:
            console.print(Text(f"Already up to date: {to} ({branch}) is at {head}."), soft_wrap=True)
            return
        # The lease refuses the push if the branch moved since it was read
        # (e.g. an overlapping run): the next run retries from fresh state.
        _run_git(["git", "-C", target, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{current or ''}",
                  "--", to, f"HEAD:refs/heads/{branch}"],
                 env=env, timeout=_PUSH_TIMEOUT, what="Push failed", secrets_=secrets_)
        console.print(Text(f"Pushed {head} to {to} ({branch}).", style="green"), soft_wrap=True)


@harness_app.command("run")
@handle_errors
def harness_run(
    tool: str = typer.Option(..., "--tool", help="Tool whose installer this harness image carries"),
    limit: int = typer.Option(5, "--limit", min=1, max=50),
    image_digest: str | None = typer.Option(None, "--image-digest", envvar="SFS_HARNESS_IMAGE_DIGEST"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Claim queued works-in jobs, run the tool's installer, post results.

    Refuses to run unless SFS_WORKS_IN_HARNESS=1 (meant for the sandboxed
    harness job, never a workstation). Credentials come ONLY from
    SFS_HARNESS_API_URL / SFS_HARNESS_API_KEY (a skills_verify:run service
    key minted by a platform admin), never from your SessionFS profile; the
    key is removed from the environment before any tool runs. Run as root
    with an unprivileged tool user (SFS_HARNESS_TOOL_USER, default sfs-tool):
    tool commands run as that user."""
    from sessionfs.skills import works_in_harness as harness

    if tool not in harness.TOOL_PLANS:
        raise typer.BadParameter(f"--tool must be one of: {', '.join(sorted(harness.TOOL_PLANS))}")
    try:
        harness.require_opt_in()
        api_url, api_key = harness.harness_credential()
        identity = harness.resolve_tool_identity()
    except harness.HarnessError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
    client = harness.HarnessClient(api_url, api_key)
    del api_key
    if identity is not None and limit != 1:
        err_console.print("[yellow]Running one job per execution (separate tool user): --limit is 1.[/yellow]")
    try:
        results = harness.run_once(client, tool, limit=limit, image_digest=image_digest, identity=identity)
    except harness.SandboxBreach as exc:
        err_console.print(Text(str(exc), style="red"))
        if json_output:
            console.print_json(json.dumps(exc.results, sort_keys=True))
        raise typer.Exit(3) from exc
    if json_output:
        console.print_json(json.dumps(results, sort_keys=True))
        return
    for result in results:
        console.print(Text(f"{result['version_id']}: {result['status']} ({result.get('tool_version', 'n/a')}) "
                           f"{result.get('reason', '')}"))
    if not results:
        console.print("No queued jobs.")
