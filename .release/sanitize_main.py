#!/usr/bin/env python3
"""Deterministic release-time sanitizer for the public main branch.

Reads `.release/private-files.txt` as the single source of truth for
which paths must not appear on main, runs `git rm -rf` on each, then
verifies no leaks remain. Replaces the prior 14-chained-shell-command
sweep that the bash sandbox flagged as risky and was hard to audit.

Usage (from repo root, while checked out on main mid-merge):

    # See what would be removed (default, makes no changes):
    .venv/bin/python .release/sanitize_main.py

    # Actually run the git rm operations:
    .venv/bin/python .release/sanitize_main.py --apply

Exit codes:
    0  — clean (no leaks, or all removals succeeded)
    1  — leaks remain after sweep (or --apply not requested but leaks
         exist); the unsafe-to-push state
    2  — invocation error (missing file, not in a git tree, etc.)

Intentionally has no third-party deps so it runs anywhere
`/usr/bin/env python3` does — even before `pip install -e .[dev]`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRIVATE_FILES_LIST = Path(".release/private-files.txt")


def parse_private_paths(list_path: Path) -> list[str]:
    """Parse `.release/private-files.txt` into a list of paths.

    Honors the comment + blank-line conventions the file already uses:
      - lines starting with `#` are comments
      - blank lines are skipped
      - inline `# trailing comment` is stripped
      - leading/trailing whitespace stripped
      - directory entries end with `/` (preserved verbatim)
    """
    if not list_path.exists():
        raise FileNotFoundError(f"missing private-files manifest: {list_path}")

    paths: list[str] = []
    for raw in list_path.read_text().splitlines():
        line = raw
        # Strip inline comments AFTER first whitespace + `#`.
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue
        paths.append(line)
    return paths


def git_ls_files() -> set[str]:
    """Return the set of paths currently tracked by git."""
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def find_leaks(tracked: set[str], private_paths: list[str]) -> dict[str, list[str]]:
    """Return {private_path: [matching tracked files...]} for any path
    that is still present on the current branch. Directory entries
    (ending with `/`) match by prefix; file entries match exactly."""
    leaks: dict[str, list[str]] = {}
    for p in private_paths:
        if p.endswith("/"):
            hits = sorted(f for f in tracked if f.startswith(p))
        else:
            hits = sorted(f for f in tracked if f == p)
        if hits:
            leaks[p] = hits
    return leaks


def git_rm(path: str) -> tuple[bool, str]:
    """Run `git rm -rf` (or `-f` for files). Returns (success, message)."""
    flag = "-rf" if path.endswith("/") else "-f"
    result = subprocess.run(
        ["git", "rm", flag, path],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sanitize main branch by removing private files per .release/private-files.txt"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run git rm. Without this flag, the script only reports what would be removed.",
    )
    parser.add_argument(
        "--list",
        type=Path,
        default=PRIVATE_FILES_LIST,
        help="Path to private-files manifest (default: .release/private-files.txt)",
    )
    args = parser.parse_args()

    try:
        private_paths = parse_private_paths(args.list)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        tracked = git_ls_files()
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: `git ls-files` failed (not a git tree?): {exc.stderr}",
            file=sys.stderr,
        )
        return 2

    leaks = find_leaks(tracked, private_paths)

    if not leaks:
        print("CLEAN — no private files tracked on this branch.")
        return 0

    print(f"Found {len(leaks)} private path(s) tracked; {sum(len(v) for v in leaks.values())} files total:")
    for pattern, hits in leaks.items():
        print(f"  {pattern}")
        for f in hits[:5]:
            print(f"    {f}")
        if len(hits) > 5:
            print(f"    ... and {len(hits) - 5} more")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to remove. Exit code 1 because leaks exist.")
        return 1

    print()
    print("Applying git rm to each leak...")
    failures: list[tuple[str, str]] = []
    for pattern in leaks.keys():
        ok, msg = git_rm(pattern)
        status = "ok" if ok else "FAIL"
        print(f"  {status}  {pattern}  {msg}")
        if not ok:
            failures.append((pattern, msg))

    # Re-verify after the sweep — git rm should have left ls-files clean
    # for these patterns. Anything still present is a real leak.
    tracked_after = git_ls_files()
    residual = find_leaks(tracked_after, private_paths)

    if failures:
        print()
        print(f"ERROR: {len(failures)} git rm operations failed.")
        return 1
    if residual:
        print()
        print(f"ERROR: leaks remain after sweep: {sorted(residual.keys())}")
        return 1

    print()
    print("CLEAN — sanitize sweep complete; branch is safe to push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
