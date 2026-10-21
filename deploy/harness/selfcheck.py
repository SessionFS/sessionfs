"""Works-in harness image self-check (run as root inside the image).

Exercises the harness's own sandbox path without a server or a package:
resolves the unprivileged tool user exactly as ``sfs catalog harness run``
does, runs the tool's version probe through the harness runner (separate
uid, minimal environment, throwaway HOME, own process group), runs the
post-job reaper and its /proc verification, and checks the reported version
against the pin in /opt/sfs-harness/tools.json. Exit 0 only when all pass.

    docker run --rm --entrypoint python <image> -I /opt/sfs-harness/selfcheck.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sessionfs.skills import works_in_harness as harness


def fail(message: str) -> None:
    print(f"selfcheck FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    tool = os.environ.get("SFS_HARNESS_TOOL", "")
    manifest = json.loads(Path("/opt/sfs-harness/tools.json").read_text())
    pinned = manifest["tools"].get(tool)
    plan = harness.TOOL_PLANS.get(tool)
    if pinned is None or plan is None:
        fail(f"unknown tool {tool!r}")
    if plan.skipped_reason or not plan.steps:
        fail(f"{tool} has no installer steps in TOOL_PLANS")
    if plan.binary != pinned["binary"]:
        fail(f"TOOL_PLANS binary {plan.binary!r} != tools.json {pinned['binary']!r}")
    if shutil.which(plan.binary) is None:
        fail(f"{plan.binary} is not on PATH")
    try:
        identity = harness.resolve_tool_identity()
    except harness.HarnessError as exc:
        fail(str(exc))
    if identity is None:
        fail("the image must run the harness as root (tool commands drop to the tool user)")

    runner = harness.make_runner(identity, timeout=60)
    with tempfile.TemporaryDirectory(prefix="sfs-selfcheck-") as temp:
        root = Path(temp)
        home = root / "home"
        home.mkdir()
        env = harness.sandbox_env(home)
        harness._hand_over(root, identity)  # noqa: SLF001 - same hand-over as a real job
        probe = runner([plan.binary, *plan.version_args], env, root)
        reaped = harness.kill_tool_processes(identity)
    version_line = (probe.stdout.strip().splitlines() or [""])[0]
    if probe.returncode != 0:
        fail(f"{plan.binary} {' '.join(plan.version_args)} exited {probe.returncode}: {probe.stderr[-500:]}")
    if pinned["version"] not in version_line:
        fail(f"version probe printed {version_line!r}, expected {pinned['version']}")
    if not reaped:
        fail("post-job reaper could not prove the tool user's processes gone")
    print(json.dumps({
        "tool": tool,
        "binary": plan.binary,
        "version": version_line,
        "tool_uid": identity.uid,
        "reaper": "clean",
    }, sort_keys=True))


if __name__ == "__main__":
    main()
