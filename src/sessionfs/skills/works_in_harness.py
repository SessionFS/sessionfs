"""Opt-in "works in" harness (marketplace v2 §7.4, levels L1).

Runs a REAL tool's own plugin installer against a one-plugin catalog the
API hands out, and posts the verdict back. It never runs inside the API
request path: it is the ``sfs catalog harness run`` command, meant for the
Terraform-managed Cloud Run job (one image per tool at a pinned version) or
a self-hosted equivalent.

Hostile-input posture (the package under test is untrusted):

- refuses to start unless ``SFS_WORKS_IN_HARNESS=1`` (opt-in);
- its only credential is a ``skills_verify:run`` service key read from
  ``SFS_HARNESS_API_KEY`` (never the SessionFS profile), removed from the
  process environment before anything is spawned;
- tool commands run as a SEPARATE uid (``SFS_HARNESS_TOOL_USER``, default
  ``sfs-tool``) when the harness runs as root, so a hostile installer can
  read neither the harness's memory nor its environment. Not root: it
  refuses to run unless ``SFS_HARNESS_ALLOW_SAME_UID=1`` (development only).
  The Cloud Run job image must run the harness as root with that separate,
  unprivileged tool user;
- each tool command runs with a minimal environment: a fresh temporary
  HOME owned by the tool user, no SessionFS key, no cloud credentials, no
  proxy settings copied;
- per-command wall-clock timeout plus CPU / file-size rlimits (POSIX);
- every tool step runs in its own session / process group, and the whole
  group is killed on timeout and again when the step returns, so a step
  cannot leave background children behind;
- with a separate tool uid the harness runs exactly ONE job per execution
  (``--limit`` is forced to 1): the container / execution boundary is the
  real isolation between packages;
- after the job, every process still owned by the tool uid is killed (a
  short-lived child that drops to the tool uid and calls ``kill(-1)``: it
  can only signal that uid, never the root harness), then the root harness
  VERIFIES that no live process of the tool uid remains (``/proc`` scan,
  killing stragglers itself). This fails closed: if the reaper did not exit
  cleanly, a process survives, or the check is impossible (no ``/proc``),
  the job is reported errored (final, not retried) and the harness exits
  non-zero without claiming anything else. Files the tool uid left at the
  top of the temp directory are removed, and each job's 0700 directory is
  deleted only after that sweep;
- the bundle is extracted with strict path rules (regular files only, no
  git metadata names, size caps);
- installers only: no step asks a tool to RUN a skill, hook or MCP server.
  Network egress limits and a read-only root are the job's infrastructure
  (VPC egress firewall, read-only container), not something this process
  can promise.

Only commands confirmed in vendor documentation (2026-10-20) are used. A
tool without a confirmed non-interactive installer reports ``skipped``.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import sys
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sessionfs.skills.git_catalog import GitCatalogError, validate_repo_path

OPT_IN_ENV = "SFS_WORKS_IN_HARNESS"
API_URL_ENV = "SFS_HARNESS_API_URL"
API_KEY_ENV = "SFS_HARNESS_API_KEY"
TOOL_USER_ENV = "SFS_HARNESS_TOOL_USER"
DEFAULT_TOOL_USER = "sfs-tool"
SAME_UID_OVERRIDE_ENV = "SFS_HARNESS_ALLOW_SAME_UID"
# Removed from the harness's own environment before any tool runs.
SCRUBBED_ENV = (API_KEY_ENV, "SESSIONFS_API_KEY")
COMMAND_TIMEOUT_SECONDS = 180
MAX_BUNDLE_FILES = 5_000
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_EVIDENCE_TAIL = 2_000


class HarnessError(RuntimeError):
    pass


class SandboxBreach(HarnessError):
    """Tool processes could not be proven gone after a job: stop at once."""

    def __init__(self, message: str, results: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.results = results or []


@dataclass(frozen=True)
class ToolPlan:
    binary: str
    version_args: tuple[str, ...]
    # Each step: argv template. {catalog} = extracted catalog dir,
    # {plugin_dir} = its plugin directory, {plugin}, {marketplace}.
    steps: tuple[tuple[str, ...], ...]
    skipped_reason: str | None = None


# Vendor docs (checked 2026-10-20): see sessionfs.skills.native_feeds.VENDOR_SOURCES.
TOOL_PLANS: dict[str, ToolPlan] = {
    "claude-code": ToolPlan(
        binary="claude",
        version_args=("--version",),
        steps=(
            ("claude", "plugin", "validate", "{catalog}"),
            ("claude", "plugin", "marketplace", "add", "{catalog}"),
            ("claude", "plugin", "install", "{plugin}@{marketplace}"),
        ),
    ),
    "copilot": ToolPlan(
        binary="copilot",
        version_args=("--version",),
        steps=(
            ("copilot", "plugin", "marketplace", "add", "{catalog}"),
            ("copilot", "plugin", "install", "{plugin}@{marketplace}"),
        ),
    ),
    "gemini": ToolPlan(
        binary="gemini",
        version_args=("--version",),
        steps=(("gemini", "extensions", "install", "{plugin_dir}", "--consent"),),
    ),
    "antigravity": ToolPlan(
        binary="agy",
        version_args=("--version",),
        steps=(("agy", "plugin", "install", "{plugin_dir}"),),
    ),
    "codex": ToolPlan(
        binary="codex",
        version_args=("--version",),
        steps=(),
        skipped_reason="Codex documents no non-interactive plugin install command (installs happen in the app)",
    ),
    "cursor": ToolPlan(
        binary="cursor",
        version_args=(),
        steps=(),
        skipped_reason="Cursor team marketplaces are imported in the Cursor dashboard; no installer CLI",
    ),
}


@dataclass
class Verdict:
    status: str  # pass | fail | skipped
    tool_version: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def require_opt_in() -> None:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise HarnessError(
            f"The works-in harness runs real tool installers against untrusted packages; "
            f"set {OPT_IN_ENV}=1 inside an isolated sandbox to run it."
        )


def harness_credential() -> tuple[str, str]:
    """(api_url, api_key) from the harness environment ONLY (never the
    SessionFS profile on disk), then scrub the key from ``os.environ`` so
    no process spawned later can inherit it."""
    api_url = os.environ.get(API_URL_ENV, "").strip()
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    for name in SCRUBBED_ENV:
        os.environ.pop(name, None)
    if not api_url or not api_key:
        raise HarnessError(
            f"Set {API_URL_ENV} and {API_KEY_ENV} (a skills_verify:run service key minted by a "
            "platform admin). The harness never reads the SessionFS profile."
        )
    return api_url, api_key


@dataclass(frozen=True)
class ToolIdentity:
    uid: int
    gid: int


def resolve_tool_identity() -> ToolIdentity | None:
    """The uid/gid tool commands run as. Root: the separate tool user
    (required). Not root: refuse unless the explicit development override
    is set, in which case tools run as the harness's own uid (None)."""
    if os.name == "posix" and os.geteuid() == 0:
        import pwd

        name = os.environ.get(TOOL_USER_ENV, "").strip() or DEFAULT_TOOL_USER
        try:
            entry = pwd.getpwnam(name)
        except KeyError as exc:
            raise HarnessError(
                f"Tool user {name!r} does not exist: create an unprivileged user for tool "
                f"commands (or name one with {TOOL_USER_ENV})."
            ) from exc
        if entry.pw_uid == 0 or entry.pw_gid == 0:
            raise HarnessError(f"Tool user {name!r} must not be root.")
        return ToolIdentity(entry.pw_uid, entry.pw_gid)
    if os.environ.get(SAME_UID_OVERRIDE_ENV) == "1":
        return None
    raise HarnessError(
        "Refusing to run L1: tool commands must run as a separate uid from the harness. Run the "
        f"harness as root with an unprivileged tool user ({TOOL_USER_ENV}), or set "
        f"{SAME_UID_OVERRIDE_ENV}=1 for local development only."
    )


def _hand_over(root: Path, identity: ToolIdentity | None) -> None:
    """Give the sandbox tree (catalog, HOME, tmp) to the tool user."""
    if identity is None:
        return
    for path in [root, *root.rglob("*")]:
        os.chown(path, identity.uid, identity.gid, follow_symlinks=False)


def extract_bundle(payload: bytes, destination: Path) -> None:
    """Strict extraction of a one-plugin catalog tarball."""
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            count += 1
            if count > MAX_BUNDLE_FILES:
                raise HarnessError("bundle has too many files")
            if not member.isfile():
                raise HarnessError(f"bundle member is not a regular file: {member.name}")
            try:
                validate_repo_path(member.name)
            except GitCatalogError as exc:
                raise HarnessError(str(exc)) from exc
            total += member.size
            if total > MAX_BUNDLE_BYTES:
                raise HarnessError("bundle is too large")
            source = archive.extractfile(member)
            if source is None:
                raise HarnessError(f"unreadable bundle member: {member.name}")
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            target.chmod(0o644)


def sandbox_env(home: Path) -> dict[str, str]:
    """A minimal environment: no inherited secrets, a throwaway HOME."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "CI": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return env


def _limits() -> None:  # pragma: no cover - runs in the child process
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (COMMAND_TIMEOUT_SECONDS, COMMAND_TIMEOUT_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass


Runner = Callable[[list[str], dict[str, str], Path], "subprocess.CompletedProcess[str]"]


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _read_tail(handle: Any) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - 4 * _EVIDENCE_TAIL))
    return handle.read().decode("utf-8", "replace")


def make_runner(identity: ToolIdentity | None, *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> Runner:
    """A runner that executes tool commands as ``identity`` (no shell), each
    in a new session whose whole process group is killed on timeout and
    when the step returns (background children included). Output goes to
    unlinked temporary files, not pipes: a child that escaped the group and
    holds the output open can never stall the harness."""

    def run(argv: list[str], env: dict[str, str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        extra: dict[str, Any] = {}
        if identity is not None:
            extra = {"user": identity.uid, "group": identity.gid, "extra_groups": []}
        posix = os.name == "posix"
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(  # noqa: S603 - argv from a fixed table, no shell
                argv,
                env=env,
                cwd=cwd,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=posix,
                preexec_fn=_limits if posix else None,  # noqa: PLW1509
                **extra,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if posix:
                    _kill_group(process.pid)
                process.kill()
                process.wait()
                raise
            finally:
                if posix:
                    _kill_group(process.pid)  # whatever the step left running in its group
            return subprocess.CompletedProcess(argv, process.returncode, _read_tail(out), _read_tail(err))

    return run


Reaper = Callable[["ToolIdentity | None"], bool]
PROC_ROOT = Path("/proc")
_ROOT_KILL_ROUNDS = 5


def tool_pids(uid: int, proc_root: Path | None = None) -> list[int] | None:
    """Live (not zombie) processes with any uid field equal to ``uid``, from
    ``/proc/<pid>/status`` (Linux). None when this cannot be checked."""
    root = proc_root if proc_root is not None else PROC_ROOT
    if not (root / "self" / "status").is_file():
        return None
    found: list[int] = []
    try:
        candidates = [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    for entry in candidates:
        try:
            text = (entry / "status").read_text(errors="replace")
        except OSError:
            continue  # exited meanwhile
        uids: list[int] = []
        state = ""
        for line in text.splitlines():
            if line.startswith("Uid:"):
                uids = [int(value) for value in line.split()[1:] if value.isdigit()]
            elif line.startswith("State:"):
                state = (line.split()[1:2] or [""])[0]
        if uid not in uids:
            continue
        # A zombie thread-group leader can still have live threads. Its own
        # `Threads:` count stays at 1 until it is reaped, so it says nothing;
        # task/ lists every thread, and a live one there is the real signal.
        if state not in {"Z", "X"} or _has_live_task(entry):
            found.append(int(entry.name))
    return sorted(found)


def _has_live_task(entry: Path) -> bool:
    try:
        tasks = list((entry / "task").iterdir())
    except OSError:
        return False
    for task in tasks:
        try:
            text = (task / "status").read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("State:"):
                if (line.split()[1:2] or [""])[0] not in {"Z", "X"}:
                    return True
                break
    return False


def _reap_children() -> None:
    """Collect exited children (the harness may be PID 1 in its container,
    inheriting orphans)."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return


def _verify_no_tool_processes(uid: int) -> bool:
    import time

    for _ in range(_ROOT_KILL_ROUNDS):
        _reap_children()
        remaining = tool_pids(uid)
        if remaining is None:
            return False  # unverifiable: fail closed
        if not remaining:
            return True
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError:
                return False
        time.sleep(0.1)
    _reap_children()
    return tool_pids(uid) == []


# Runs as the tool uid. kill(-1) fails with ESRCH when that uid has no other
# process to signal: the common, clean case after a well-behaved job, so it
# must exit 0. Any other error (or the child being killed) still fails.
_REAPER_CODE = (
    "import os, signal\n"
    "try:\n"
    "    os.kill(-1, signal.SIGKILL)\n"
    "except ProcessLookupError:\n"
    "    pass\n"
)


def kill_tool_processes(identity: ToolIdentity | None) -> bool:
    """After a job: kill EVERY process owned by the tool uid (including any
    that escaped its process group with setsid) and remove what the uid
    left at the top of the temp directory. Runs ``kill(-1, SIGKILL)`` in a
    short-lived child that has dropped to the tool uid, so it can signal
    only that uid's processes, never the root harness. Without a separate
    tool uid (the development override) there is nothing safe to sweep:
    ``kill(-1)`` would hit the harness itself.

    Returns True only when the reaper exited 0 AND the root harness then
    verified that no live process of the tool uid remains. Fails closed."""
    if identity is None:
        return True
    if os.name != "posix":
        return False
    clean = False
    try:
        reaper = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-I", "-c", _REAPER_CODE],
            user=identity.uid, group=identity.gid, extra_groups=[], env={}, cwd="/",
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
        )
        clean = reaper.returncode == 0
    except (OSError, subprocess.SubprocessError):
        clean = False
    # Verify from root whatever the reaper reported (and kill stragglers).
    verified = _verify_no_tool_processes(identity.uid)
    _remove_tool_temp(identity)
    return clean and verified


def _remove_tool_temp(identity: ToolIdentity) -> None:
    temp_root = Path(tempfile.gettempdir())
    try:
        entries = list(temp_root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.lstat().st_uid != identity.uid:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


_default_runner = make_runner(None)


def _tail(value: str | None) -> str:
    return (value or "")[-_EVIDENCE_TAIL:]


def run_tool(
    tool: str,
    bundle: bytes,
    meta: dict[str, str],
    *,
    runner: Runner = _default_runner,
    which: Callable[[str], str | None] = shutil.which,
    identity: ToolIdentity | None = None,
    reaper: Reaper = kill_tool_processes,
) -> Verdict:
    plan = TOOL_PLANS.get(tool)
    if plan is None:
        return Verdict("skipped", "unknown", f"no harness plan for {tool}")
    if plan.skipped_reason:
        return Verdict("skipped", "n/a", plan.skipped_reason)
    if which(plan.binary) is None:
        return Verdict("skipped", "not-installed", f"{plan.binary} is not installed in this harness image")
    # A fresh 0700 directory per job; the tool uid's processes are killed
    # BEFORE it is removed, so nothing can recreate it for the next job.
    with tempfile.TemporaryDirectory(prefix="sfs-works-in-") as temp:
        breach = SandboxBreach(
            "tool processes could not be proven gone after the job (the reaper failed or a "
            "process of the tool user survived); stopping without claiming more jobs"
        )
        try:
            verdict = _run_in_sandbox(plan, Path(temp), bundle, meta, runner=runner, identity=identity)
        except BaseException as exc:
            # The sweep runs, and is checked, however the job ended.
            if not reaper(identity):
                raise breach from exc
            raise
        if not reaper(identity):
            raise breach
        return verdict


def _run_in_sandbox(
    plan: ToolPlan,
    root: Path,
    bundle: bytes,
    meta: dict[str, str],
    *,
    runner: Runner,
    identity: ToolIdentity | None,
) -> Verdict:
    catalog = root / "catalog"
    home = root / "home"
    catalog.mkdir()
    home.mkdir()
    try:
        extract_bundle(bundle, catalog)
    except (HarnessError, tarfile.TarError, OSError) as exc:
        return Verdict("fail", "n/a", f"bundle rejected: {exc}")
    env = sandbox_env(home)
    _hand_over(root, identity)
    tool_version = "unknown"
    if plan.version_args:
        try:
            probe = runner([plan.binary, *plan.version_args], env, root)
            tool_version = (probe.stdout.strip().splitlines() or ["unknown"])[0][:50] or "unknown"
        except (OSError, subprocess.SubprocessError):
            tool_version = "unknown"
    values = {
        "catalog": str(catalog),
        "plugin_dir": str(catalog / meta["plugin_path"]),
        "plugin": meta["plugin"],
        "marketplace": meta["marketplace"],
    }
    steps: list[dict[str, Any]] = []
    for template in plan.steps:
        argv = [part.format(**values) for part in template]
        shown = [part.replace(str(root), "<sandbox>") for part in argv]
        try:
            result = runner(argv, env, root)
        except subprocess.TimeoutExpired:
            steps.append({"argv": shown, "exit_code": None, "error": "timeout"})
            return Verdict("fail", tool_version, f"{' '.join(shown)} timed out", {"steps": steps})
        except OSError as exc:
            steps.append({"argv": shown, "exit_code": None, "error": type(exc).__name__})
            return Verdict("fail", tool_version, f"{' '.join(shown)} could not start", {"steps": steps})
        steps.append(
            {
                "argv": shown,
                "exit_code": result.returncode,
                "stdout_tail": _tail(result.stdout).replace(str(root), "<sandbox>"),
                "stderr_tail": _tail(result.stderr).replace(str(root), "<sandbox>"),
            }
        )
        if result.returncode != 0:
            return Verdict(
                "fail", tool_version, f"{' '.join(shown)} exited {result.returncode}", {"steps": steps}
            )
    return Verdict("pass", tool_version, "the tool's own installer accepted the projection",
                   {"steps": steps, "projection_hash": meta.get("projection_hash")})


class HarnessClient:
    """Talks to the platform-admin harness endpoints."""

    def __init__(self, api_url: str, api_key: str, *, transport: Any = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    def _client(self):
        import httpx

        kwargs: dict[str, Any] = {
            "timeout": 60.0,
            "headers": {"Authorization": f"Bearer {self.api_key}"},
            "follow_redirects": False,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    def claim(self, tool: str, limit: int, image_digest: str | None) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.post(
                f"{self.api_url}/api/v1/admin/skills/verify/jobs/claim",
                json={"tool": tool, "limit": limit, "harness_image_digest": image_digest},
            )
        response.raise_for_status()
        return list(response.json().get("jobs", []))

    def projection(self, job: dict[str, Any]) -> tuple[bytes, dict[str, str]] | None:
        """The job's bundle, or None when the server skipped the job (the
        package is no longer deliverable)."""
        path = str(job["projection_path"])
        if not path.startswith("/api/v1/admin/skills/verify/jobs/"):
            raise HarnessError("unexpected projection path")
        with self._client() as client:
            response = client.get(
                f"{self.api_url}{path}", headers={"X-SessionFS-Claim-Token": job["claim_token"]}
            )
        if response.status_code == 410:
            return None
        response.raise_for_status()
        meta = {
            "marketplace": response.headers["X-SessionFS-Marketplace"],
            "plugin": response.headers["X-SessionFS-Plugin"],
            "projection_hash": response.headers.get("X-SessionFS-Projection-Hash", ""),
        }
        meta["plugin_path"] = f"plugins/{meta['plugin']}"
        return response.content, meta

    def report(self, job: dict[str, Any], verdict: Verdict, *, projection_hash: str | None,
               image_digest: str | None) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"{self.api_url}/api/v1/admin/skills/verify/jobs/{job['job_id']}/result",
                json={
                    "claim_token": job["claim_token"],
                    "level": "L1",
                    "status": verdict.status,
                    "tool_version": verdict.tool_version[:50],
                    "reason": verdict.reason[:2000],
                    "evidence": verdict.evidence,
                    "projection_hash": projection_hash,
                    "harness_image_digest": image_digest,
                },
            )
        response.raise_for_status()
        return dict(response.json())

    def report_error(self, job: dict[str, Any], reason: str, *, final: bool = False) -> dict[str, Any]:
        """The harness could not run this job: the server re-queues it until
        the attempt cap, then ends it 'errored' (``final``: errored now)."""
        with self._client() as client:
            response = client.post(
                f"{self.api_url}/api/v1/admin/skills/verify/jobs/{job['job_id']}/result",
                json={"claim_token": job["claim_token"], "level": "L1", "status": "error",
                      "tool_version": "n/a", "reason": reason[:2000], "final": final},
            )
        response.raise_for_status()
        return dict(response.json())


def _error_reason(exc: BaseException) -> str:
    return f"harness error: {type(exc).__name__}: {str(exc)[:300]}"


def run_once(
    client: HarnessClient,
    tool: str,
    *,
    limit: int = 5,
    image_digest: str | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    identity: ToolIdentity | None = None,
    reaper: Reaper = kill_tool_processes,
) -> list[dict[str, Any]]:
    """Claim and run up to ``limit`` jobs (exactly one with a separate tool
    uid). One job's failure (bundle fetch, tool crash, report) is reported
    for that job and never aborts the batch, EXCEPT a failed post-job sweep:
    that job is reported errored (final) and ``SandboxBreach`` is raised so
    nothing else is claimed."""
    require_opt_in()
    if identity is not None:
        limit = 1  # one package per execution: the container is the isolation
    run = runner if runner is not None else make_runner(identity)
    results: list[dict[str, Any]] = []
    for job in client.claim(tool, limit, image_digest):
        base = {"job_id": job.get("job_id"), "version_id": job.get("version_id")}
        try:
            fetched = client.projection(job)
            if fetched is None:
                results.append({**base, "status": "skipped", "tool_version": "n/a",
                                "reason": "the package is no longer deliverable; the server skipped the job"})
                continue
            bundle, meta = fetched
            verdict = run_tool(tool, bundle, meta, runner=run, which=which, identity=identity, reaper=reaper)
        except SandboxBreach as exc:
            try:
                client.report_error(job, str(exc), final=True)
            except Exception:  # noqa: BLE001 - still stop: never run another job here
                pass
            results.append({**base, "status": "error", "tool_version": "n/a", "reason": str(exc)})
            raise SandboxBreach(str(exc), results) from exc
        except Exception as exc:  # noqa: BLE001 - one job never stops the batch
            reason = _error_reason(exc)
            try:
                client.report_error(job, reason)
            except Exception:  # noqa: BLE001 - the claim expires and is retried
                pass
            results.append({**base, "status": "error", "tool_version": "n/a", "reason": reason})
            continue
        try:
            reported = client.report(job, verdict, projection_hash=meta.get("projection_hash") or None,
                                     image_digest=image_digest)
        except Exception as exc:  # noqa: BLE001 - the claim expires and is retried (attempts capped)
            results.append({**base, "status": "error", "tool_version": verdict.tool_version,
                            "reason": f"could not report the verdict: {_error_reason(exc)}"})
            continue
        results.append({**base, **reported, "reason": verdict.reason, "tool_version": verdict.tool_version})
    return results
