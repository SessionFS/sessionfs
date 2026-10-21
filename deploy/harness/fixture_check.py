"""Offline end-to-end check of a harness image against a fixture package.

Builds a one-plugin public catalog from a tiny instruction-only skill with
the same generator the API uses (``native_feeds.build_catalog``), packs it
the way the projection endpoint does, and runs the real harness job path
(``works_in_harness.run_tool``: strict extraction, separate tool uid,
minimal env, the tool's own installer steps from TOOL_PLANS, post-job
reaper). Exits 0 only on a ``pass`` verdict. No server, key or network:

    docker run --rm --network none \\
      -v "$PWD/deploy/harness/fixture_check.py:/fixture_check.py:ro" \\
      --entrypoint python <image> -I /fixture_check.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from datetime import datetime, timezone

from sessionfs.skills import native_feeds
from sessionfs.skills import works_in_harness as harness

SKILL = b"---\nname: deploy\ndescription: Deploy the service.\n---\n\nRun the deploy checklist.\n"
CORE = json.dumps(
    {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
     "name": "deploy", "version": "1.2.3", "license": "MIT"},
    sort_keys=True,
).encode()


def fixture_bundle() -> tuple[bytes, dict[str, str]]:
    item = native_feeds.CatalogInput(
        version_id="skv_" + "1" * 24,
        package_id="spk_" + "1" * 24,
        namespace="acme",
        name="deploy",
        description="Deploy with the Acme runbook.",
        version_label="1.4.0",
        content_hash="sha256:" + "a" * 64,
        published_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        files={"plugin.json": CORE, "skills/deploy/SKILL.md": SKILL},
        license="MIT",
        homepage="https://app.sessionfs.dev/marketplace/acme/deploy",
        publisher_name="Acme",
    )
    projection = native_feeds.build_catalog("public", None, [item])
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, data in sorted(projection.files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    plugin = projection.plugins[0]
    meta = {
        "marketplace": projection.marketplace_name,
        "plugin": plugin.plugin_id,
        "plugin_path": plugin.plugin_path,
        "projection_hash": plugin.projection_hash,
    }
    return output.getvalue(), meta


def main() -> None:
    tool = os.environ["SFS_HARNESS_TOOL"]
    identity = harness.resolve_tool_identity()
    if identity is None:
        print("fixture check must run as root (tool commands drop to the tool user)", file=sys.stderr)
        raise SystemExit(1)
    bundle, meta = fixture_bundle()
    verdict = harness.run_tool(tool, bundle, meta, runner=harness.make_runner(identity), identity=identity)
    print(json.dumps({"tool": tool, "status": verdict.status, "tool_version": verdict.tool_version,
                      "reason": verdict.reason, "evidence": verdict.evidence}, indent=1, sort_keys=True))
    if verdict.status != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
