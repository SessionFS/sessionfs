"""Verify _extract_manifest_metadata round-trips rules provenance."""

from __future__ import annotations

import io
import json
import tarfile


def _build_archive(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_with_provenance():
    from sessionfs.server.routes.sessions import _extract_manifest_metadata

    manifest = {
        "sfs_version": "0.1.0",
        "title": "T",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 3},
        "instruction_provenance": {
            "rules_source": "sessionfs",
            "rules_version": 5,
            "rules_hash": "sha256:cafe",
            "instruction_artifacts": [
                {"artifact_type": "rules_file", "path": "CLAUDE.md",
                 "scope": "project", "source": "sessionfs",
                 "hash": "sha256:cafe", "version": 5},
            ],
        },
    }
    meta = _extract_manifest_metadata(_build_archive(manifest))
    assert meta["rules_source"] == "sessionfs"
    assert meta["rules_version"] == 5
    assert meta["rules_hash"] == "cafe"  # prefix stripped on extraction
    arts = json.loads(meta["instruction_artifacts"])
    assert len(arts) == 1
    assert arts[0]["path"] == "CLAUDE.md"


def test_extract_without_provenance_defaults_none():
    from sessionfs.server.routes.sessions import _extract_manifest_metadata

    manifest = {
        "sfs_version": "0.1.0",
        "title": "T",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1},
    }
    meta = _extract_manifest_metadata(_build_archive(manifest))
    assert meta["rules_source"] == "none"
    assert meta["rules_version"] is None
    assert meta["rules_hash"] is None
    assert meta["instruction_artifacts"] == "[]"
