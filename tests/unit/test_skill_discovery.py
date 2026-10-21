"""Pure discovery rules (marketplace v2 §9): facets, k-floor, popularity,
README sanitizing, package paths and image sniffing."""

from __future__ import annotations

import json

import pytest

from sessionfs.skills import discovery


def test_categories_are_a_fixed_taxonomy() -> None:
    assert len(discovery.CATEGORIES) == 15
    assert len(discovery.CATEGORY_SLUGS) == 15
    assert discovery.MAX_CATEGORIES_PER_PACKAGE == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"declared": True, "value": " Apache-2.0 "}, "apache-2.0"),
        ({"declared": True, "value": "MIT  OR\tApache-2.0"}, "mit or apache-2.0"),
        ({"declared": False}, None),
        ({"declared": True, "value": 7}, None),
        ("not json", None),
        (None, None),
    ],
)
def test_license_key(raw, expected) -> None:
    value = json.dumps(raw) if isinstance(raw, dict) else raw
    assert discovery.license_key(value) == expected


def test_severity_rank_prefers_open_severity() -> None:
    assert discovery.scan_severity_rank(json.dumps({"max_severity": "high", "open_max_severity": "low"})) == 1
    assert discovery.scan_severity_rank(json.dumps({"max_severity": "critical"})) == 4
    assert discovery.scan_severity_rank("{}") == 0
    assert discovery.scan_severity_rank(json.dumps({"max_severity": "weird"})) == 0


def test_capability_flags() -> None:
    risk = {"scripts": True, "mcp": False, "hooks": True, "executable_extensions": True, "allowed_tools": ["Bash"]}
    assert discovery.capability_flags(json.dumps(risk)) == ",scripts,hooks,executables,allowed_tools,"
    assert discovery.capability_flags(json.dumps({"allowed_tools": []})) == ""


def test_provenance_attested() -> None:
    assert discovery.provenance_attested(json.dumps({"kind": "github_oidc", "verification": "oidc_token"}))
    assert discovery.provenance_attested(json.dumps({"attested": True}))
    assert not discovery.provenance_attested(json.dumps({"kind": "github_oidc"}))
    assert not discovery.provenance_attested(None)


def test_floor_and_popularity() -> None:
    assert discovery.floored(0) == {"count": 0, "display": "0"}
    assert discovery.floored(9) == {"count": None, "display": "<10"}
    assert discovery.floored(10) == {"count": 10, "display": "10"}
    assert discovery.popularity_rank(0) == 0
    assert discovery.popularity_rank(1) == 1
    assert discovery.popularity_rank(1023) == 10
    # Service installs weigh a tenth.
    assert discovery.popularity_rank(0, 30) == discovery.popularity_rank(3)


def test_sanitize_readme_marks_hidden_characters() -> None:
    safe = discovery.sanitize_readme("a‮b\r\nc​d 👨‍👩 ❤️".encode())
    assert safe.content == "a⟦U+202E⟧b\nc⟦U+200B⟧d 👨‍👩 ❤️"
    assert [(f["line"], f["column"], f["kind"]) for f in safe.hidden_unicode] == [
        (1, 2, "bidi"), (2, 2, "zero_width"),
    ]
    tags = discovery.sanitize_readme("x\U000e0041\x07︀︁︂")
    assert [f["kind"] for f in tags.hidden_unicode] == ["tag", "control", *["variation_selector"] * 3]


def test_sanitize_readme_caps_size_and_replaces_bad_utf8() -> None:
    big = b"a" * (discovery.README_MAX_BYTES + 10)
    safe = discovery.sanitize_readme(big)
    assert safe.truncated and len(safe.content) == discovery.README_MAX_BYTES
    assert discovery.sanitize_readme(b"\xff ok").content == "� ok"


def test_first_skill_body_strips_frontmatter() -> None:
    bundle = [
        {"path": "skills/a/references/x.md", "content": "nope"},
        {"path": "skills/a/SKILL.md", "content": "---\nname: a\n---\n\n# A\nbody\n"},
    ]
    assert discovery.first_skill_body(bundle) == ("skills/a/SKILL.md", "# A\nbody\n")
    assert discovery.first_skill_body([]) is None


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("README.md", True),
        ("docs/guide.md", True),
        ("../x", False),
        ("/etc/passwd", False),
        ("a//b", False),
        ("a/./b", False),
        ("a\\b", False),
        ("", False),
        ("a\x00b", False),
    ],
)
def test_valid_package_path(path, ok) -> None:
    assert discovery.valid_package_path(path) is ok


def test_image_media_type_requires_matching_magic() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
    assert discovery.image_media_type("a.png", png) == "image/png"
    assert discovery.image_media_type("a.jpg", png) is None
    assert discovery.image_media_type("a.svg", b"<svg/>") is None
    assert discovery.image_media_type("a.png", b"<html>") is None
    assert discovery.image_media_type("a.gif", b"GIF89a....") == "image/gif"
    assert discovery.image_media_type("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert discovery.image_media_type("a.png", png + b"0" * discovery.IMAGE_MAX_BYTES) is None


def test_floored_group_rounds_totals_with_hidden_parts() -> None:
    exact = [discovery.floored(12), discovery.floored(20)]
    assert discovery.floored_group(32, exact) == {"count": 32, "display": "32"}
    hidden = [discovery.floored(12), discovery.floored(3)]
    assert discovery.floored_group(15, hidden) == {"count": 10, "display": "10+", "approximate": True}
    assert discovery.floored_group(7, hidden) == {"count": None, "display": "<10"}
    approx = [discovery.floored_group(15, hidden), discovery.floored(40)]
    assert discovery.floored_group(55, approx)["display"] == "50+"
    # A zero part hides nothing.
    assert discovery.floored_group(12, [discovery.floored(12), discovery.floored(0)])["display"] == "12"


def test_coarse_counter_rounds_everything_when_any_cell_is_hidden() -> None:
    exact = discovery.coarse_counter([12, 0, 30])
    assert exact(42) == {"count": 42, "display": "42"}
    coarse = discovery.coarse_counter([12, 3, 30])
    assert coarse(45) == {"count": 40, "display": "40+", "approximate": True}
    assert coarse(3) == {"count": None, "display": "<10"}
    assert coarse(0) == {"count": 0, "display": "0"}
    assert discovery.ANALYTICS_WINDOWS == (7, 30, 90)


def test_displayed_rate_uses_displayed_counts_only() -> None:
    assert discovery.displayed_rate(discovery.floored(3), discovery.floored(40)) is None
    assert discovery.displayed_rate(discovery.floored(10), discovery.floored(40)) == 0.25
    rounded = discovery.floored_group(47, [discovery.floored(3), discovery.floored(44)])
    assert discovery.displayed_rate(discovery.floored(12), rounded) == round(12 / 40, 2)


def test_terminal_safe_and_hidden_character_detection() -> None:
    assert discovery.terminal_safe("a\x1b[31mb‮c​d\x9be\U000e0041f\n\tg") == "a[31mbcdef\n\tg"
    assert discovery.terminal_safe(None) == ""
    assert discovery.has_hidden_characters("x‮y")
    assert not discovery.has_hidden_characters("plain — text ❤️")


def _tar_bytes(members: list[tuple[str, bytes | None]]) -> bytes:
    import io
    import tarfile

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            if payload is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def test_read_member_streams_to_the_first_regular_match() -> None:
    from sessionfs.server.services.skill_discovery import _read_member, _readme_member

    payload = _tar_bytes([
        ("README.md", None),  # a symlink is never read
        ("skills/x/README.md", b"nested"),
        ("./Readme.md", b"# root"),
        ("big.bin", b"0" * 5000),
    ])
    assert _read_member(payload, _readme_member(None), 100) == ("Readme.md", b"# root")
    assert _read_member(payload, _readme_member("skills/x/README.md"), 100) == ("skills/x/README.md", b"nested")
    # The read is capped at limit + 1 bytes.
    assert _read_member(payload, lambda name: name == "big.bin", 10) == ("big.bin", b"0" * 11)
    assert _read_member(payload, lambda name: name == "missing", 10) is None
    assert _read_member(b"not a gzip", lambda name: True, 10) is None
