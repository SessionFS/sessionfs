"""The SessionFS-authored launch catalog under catalog/ must stay publishable."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from sessionfs.server.services.skill_packages import validate_hosted_package

CATALOG = Path(__file__).resolve().parents[2] / "catalog"
PACKAGES = sorted(path for path in CATALOG.iterdir() if (path / "plugin.json").is_file())


def _tar(root: Path) -> bytes:
    output = io.BytesIO()
    # Hosted uploads must be plain ustar (no PAX/GNU extended headers).
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                payload = path.read_bytes()
                info = tarfile.TarInfo(path.relative_to(root).as_posix())
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def test_catalog_is_not_empty():
    assert PACKAGES


@pytest.mark.parametrize("root", PACKAGES, ids=lambda path: path.name)
def test_catalog_package_validates_for_public_listing(root: Path):
    hosted = validate_hosted_package(_tar(root), source_locator=f"catalog:{root.name}")
    # Public listings redistribute the package, so a license must be declared;
    # launch packages must not ask users for risky capability consent.
    assert hosted.license_metadata.get("declared")
    assert hosted.instruction_bundle
    risk = hosted.risk_summary
    assert not (risk["scripts"] or risk["mcp"] or risk["hooks"] or risk["executable_extensions"])
    assert risk["allowed_tools"] == [] and risk["instruction_only_load"]
    assert risk["dlp"]["findings"] == []
