#!/usr/bin/env python3
"""Inspect public build artifacts for Enterprise-only content.

Usage: check_public_artifacts.py PATH [PATH ...]

Each PATH is a built artifact:
  * a wheel (``.whl``)          -> zip archive;
  * an sdist (``.tar.gz``/``.tgz``) -> gzipped tar archive;
  * a directory                 -> e.g. an extracted image filesystem or a
                                   ``dashboard/dist`` bundle.

The script exits non-zero if any member of any artifact:
  * has a path containing ``sessionfs_ee/``, a repo-root-level ``ee/``, or
    ``dashboard/src/ee/``; or
  * has content containing ``SESSIONFS-ENTERPRISE-ONLY``.

Public artifacts must be Community-only, so any of those is a release blocker.
The script uses only the standard library so it runs in any CI image.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path

MARKER = b"SESSIONFS-ENTERPRISE-ONLY"

ENTERPRISE_PACKAGE = "sessionfs_ee"
DASHBOARD_EE = "dashboard/src/ee"


def path_is_enterprise(rel_path: str) -> str | None:
    """Return a reason if a member path belongs to the Enterprise edition.

    ``rel_path`` is a member path relative to the artifact root (or, for an
    sdist, relative to the top-level ``<name>-<version>/`` directory).
    """
    normalized = rel_path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]

    if ENTERPRISE_PACKAGE in parts:
        return f"path contains the {ENTERPRISE_PACKAGE!r} package"
    if DASHBOARD_EE in normalized:
        return "path is under dashboard/src/ee/"
    # A repo-root-level ee/ directory. Sdists wrap members under
    # `<name>-<version>/`, and an extracted image filesystem or a dashboard
    # bundle places `ee/` at the root, so an `ee` component at depth 0 or 1
    # is the enterprise tree.
    for idx, part in enumerate(parts):
        if part == "ee" and idx <= 1:
            return "path is under the repo-root ee/ directory"
    return None


def _check_member(rel_path: str, data: bytes, problems: list[str]) -> None:
    reason = path_is_enterprise(rel_path)
    if reason:
        problems.append(f"{rel_path}: {reason}")
    if MARKER in data:
        problems.append(f"{rel_path}: content contains {MARKER.decode()!r}")


def inspect_zip(path: Path) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            reason = path_is_enterprise(info.filename)
            if reason:
                problems.append(f"{info.filename}: {reason}")
            if info.is_dir():
                continue
            data = zf.read(info.filename)
            if MARKER in data:
                problems.append(
                    f"{info.filename}: content contains {MARKER.decode()!r}"
                )
    return problems


def inspect_tar_gz(path: Path) -> list[str]:
    problems: list[str] = []
    with tarfile.open(path, "r:gz") as tf:
        for member in tf.getmembers():
            reason = path_is_enterprise(member.name)
            if reason:
                problems.append(f"{member.name}: {reason}")
            if not member.isfile():
                continue
            extracted = tf.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if MARKER in data:
                problems.append(
                    f"{member.name}: content contains {MARKER.decode()!r}"
                )
    return problems


def inspect_directory(path: Path) -> list[str]:
    problems: list[str] = []
    for entry in sorted(path.rglob("*")):
        rel = entry.relative_to(path).as_posix()
        reason = path_is_enterprise(rel)
        if reason:
            problems.append(f"{rel}: {reason}")
        if entry.is_file() and MARKER in entry.read_bytes():
            problems.append(f"{rel}: content contains {MARKER.decode()!r}")
    return problems


def inspect_artifact(path: Path) -> list[str]:
    name = path.name
    if name.endswith(".whl"):
        return inspect_zip(path)
    if name.endswith((".tar.gz", ".tgz")):
        return inspect_tar_gz(path)
    if path.is_dir():
        return inspect_directory(path)
    raise SystemExit(
        f"unsupported artifact type: {path} (expected .whl, .tar.gz, or a directory)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if a public artifact leaks Enterprise-only content."
    )
    parser.add_argument("paths", nargs="+", type=Path, help="artifacts to inspect")
    args = parser.parse_args(argv)

    failed = False
    for path in args.paths:
        if not path.exists():
            print(f"ERROR: {path}: no such file or directory", file=sys.stderr)
            failed = True
            continue
        problems = inspect_artifact(path)
        if problems:
            failed = True
            print(f"FAIL {path}:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print(f"ok {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
