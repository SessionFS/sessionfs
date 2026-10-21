"""Org skills policy: schema, evaluation and the per-tool compiler
(marketplace v2 §8.2/§8.3/§8.9).

Golden files live in tests/fixtures/policy_compiler/<fixture>/<tool>.<ext>;
regenerate with SFS_UPDATE_GOLDEN=1 after a deliberate compiler change and
review the diff (every emitted key is vendor-confirmed - see
server/services/policy_compiler/tools.py for the sources).
"""

from __future__ import annotations

import json
import os
import random
import tomllib
from pathlib import Path

import pytest

from sessionfs.server.services.policy_compiler import (
    POLICY_FIELDS,
    TOOLS,
    CatalogSource,
    compile_all,
    compile_policy,
)
from sessionfs.skills.policy import (
    MODES,
    AllowlistEntry,
    SEVERITIES,
    PolicyError,
    PolicySubject,
    SkillsPolicy,
    canonical_origin,
    evaluate,
    normalize_source,
    parse_policy,
    policy_hash,
)

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "policy_compiler"
CATALOGS = [CatalogSource("sessionfs-acme", "https://api.sessionfs.dev/catalogs/acme")]

FIXTURES: dict[str, tuple[dict, list[CatalogSource]]] = {
    "open_defaults": ({"mode": "open"}, []),
    "permissive": (
        {
            "mode": "open",
            "allow_hooks": True,
            "allow_local_mcp": True,
            "allow_remote_mcp": True,
            "allow_skill_scripts": True,
            "allow_hidden_unicode": True,
            "require_dlp_clean_import": False,
            "max_scan_severity": "critical",
        },
        [],
    ),
    "allowlist_catalog": (
        {
            "mode": "allowlist",
            "allowlist": [
                {"publisher": "pub_acme", "require_attestation": True},
                {"source": "https://github.com/acme/skills.git", "sha": "any"},
            ],
            "allow_remote_mcp": True,
            "allowed_remote_mcp_origins": ["https://mcp.acme.com"],
            "block_unmanaged_marketplaces": True,
        },
        CATALOGS,
    ),
    "local_mcp_only": (
        {"mode": "library_only", "allow_hooks": True, "allow_local_mcp": True, "allow_remote_mcp": False},
        CATALOGS,
    ),
    "disabled_lockdown": (
        {"mode": "disabled", "allow_local_mcp": False, "allow_remote_mcp": False, "tools": {"cursor": {"enforcement": "report_only"}}},
        [],
    ),
}
EXT = {"json": "json", "toml": "toml", "markdown": "md"}


def _policy(raw: dict) -> SkillsPolicy:
    return parse_policy({"skills": raw}).skills


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_v016_block_reads_as_version_2_and_hash_is_stable() -> None:
    legacy = parse_policy({"skills": {"mode": "allowlist", "allowlist": [{"source": "github.com/acme/skills", "sha": "ANY"}], "allow_hooks": False}})
    assert legacy.skills.policy_version == 2
    assert legacy.skills.allowlist[0].sha == "any"
    again = parse_policy(json.loads(json.dumps(legacy.model_dump(mode="json"))))
    assert policy_hash(legacy) == policy_hash(again)


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "everything"},
        {"mode": "open", "surprise": True},
        {"allowlist": [{"publisher": "pub_x", "source": "github.com/a/b"}]},
        {"allowlist": [{}]},
        {"allowlist": [{"publisher": "acme"}]},
        {"allowlist": [{"source": "github.com/a/b", "sha": "zzz"}]},
        {"allowed_remote_mcp_origins": ["http://mcp.acme.com"]},
        {"allowed_remote_mcp_origins": ["https://mcp.acme.com/path"]},
        {"allowed_remote_mcp_origins": ["https://user:pw@mcp.acme.com"]},
        {"tools": {"notepad": {"enforcement": "managed"}}},
        {"max_scan_severity": "warn"},
        {"policy_version": 3},
    ],
)
def test_invalid_policies_are_refused(bad: dict) -> None:
    with pytest.raises(PolicyError):
        parse_policy({"skills": bad})


def test_policy_must_have_a_skills_block() -> None:
    with pytest.raises(PolicyError):
        parse_policy({"mode": "open"})
    with pytest.raises(PolicyError):
        parse_policy([])


def test_source_and_origin_normalization() -> None:
    same = {
        normalize_source("https://github.com/Acme/Skills.git"),
        normalize_source("git@github.com:acme/skills"),
        normalize_source("github.com/acme/skills/"),
        normalize_source("git+ssh://git@github.com/acme/skills.git"),
    }
    assert same == {"github.com/acme/skills"}
    assert normalize_source("not-a-source") == ""
    assert canonical_origin("https://MCP.Acme.com:443/") == "https://mcp.acme.com"
    assert canonical_origin("https://mcp.acme.com:8443") == "https://mcp.acme.com:8443"
    assert canonical_origin("http://mcp.acme.com") is None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _manifest(**overrides) -> dict:
    manifest = {
        "manifest_version": 1,
        "instructions": True,
        "scripts": [],
        "hooks": [],
        "mcp_local": [],
        "mcp_remote": [],
        "executable_extensions": [],
        "mcp_declared": False,
        "unicode_counts": {"low": 0, "medium": 0, "high": 0, "critical": 0},
        "unicode_truncated": False,
        "incomplete": [],
    }
    manifest.update(overrides)
    return manifest


def _subject(**overrides) -> PolicySubject:
    values = dict(
        package_org_id=None,
        org_id="org_a",
        publisher_id="pub_acme",
        publisher_verified=True,
        verified_repository_id=4242,
        verified_commit="0123456789abcdef0123456789abcdef01234567",
        manifest=_manifest(),
        attested=True,
        dlp_status="passed",
        scan_max_severity="none",
    )
    values.update(overrides)
    return PolicySubject(**values)


def _codes(policy: dict, **subject) -> set[str]:
    return {v.code for v in evaluate(_policy(policy), _subject(**subject)).violations}


def test_modes() -> None:
    assert _codes({"mode": "open"}) == set()
    assert _codes({"mode": "disabled"}) == {"skills_disabled"}
    assert _codes({"mode": "library_only"}) == {"not_in_org_library"}
    assert _codes({"mode": "library_only"}, package_org_id="org_a") == set()
    assert _codes({"mode": "allowlist"}) == {"not_allowlisted"}
    assert _codes({"mode": "allowlist"}, package_org_id="org_a") == set()  # own library


def test_allowlist_publisher_entries() -> None:
    policy = {"mode": "allowlist", "allowlist": [{"publisher": "pub_acme", "require_attestation": True}]}
    assert _codes(policy) == set()
    assert _codes(policy, attested=False) == {"not_allowlisted"}
    assert _codes(policy, publisher_verified=False) == {"not_allowlisted"}
    assert _codes({**policy, "allowed_publishers_verified_only": False}, publisher_verified=False) == set()
    assert _codes(policy, publisher_id="pub_other") == {"not_allowlisted"}


def test_allowlist_source_entries_and_commit_pins() -> None:
    pinned = {
        "mode": "allowlist",
        "allowlist": [{"source": "git@github.com:acme/skills.git", "sha": "0123456789abcdef0123456789abcdef01234567", "repository_id": 4242}],
    }
    assert _codes(pinned) == set()
    assert _codes(pinned, verified_commit="fedcba9876543210") == {"not_allowlisted"}
    # A pin is the full commit and matches exactly: a commit that merely
    # shares a prefix with it (one an attacker could grind out) is refused,
    # and an abbreviated pin is not accepted at all.
    assert _codes(pinned, verified_commit="0123456789abcdef0123456789abcdef0123ffff") == {"not_allowlisted"}
    with pytest.raises(ValueError):
        AllowlistEntry(source="github.com/acme/skills", sha="0123456", repository_id=4242)
    assert _codes(pinned, verified_commit=None) == {"not_allowlisted"}
    # Matching is by repository id: another repository is refused even if it
    # later took over the allowlisted NAME (rename / delete + re-register)...
    assert _codes(pinned, verified_repository_id=9999) == {"not_allowlisted"}
    # ...and no verified provenance matches nothing.
    unpinned = {"mode": "allowlist", "allowlist": [{"source": "github.com/acme/skills", "sha": "any", "repository_id": 4242}]}
    assert _codes(unpinned) == set()
    assert _codes(unpinned, verified_repository_id=None, verified_commit=None) == {"not_allowlisted"}
    # An entry never pinned to an id (a legacy name-only entry) matches nothing.
    name_only = {"mode": "allowlist", "allowlist": [{"source": "github.com/acme/skills", "sha": "any"}]}
    assert _codes(name_only) == {"not_allowlisted"}


def test_repository_id_is_for_source_entries_only() -> None:
    with pytest.raises(PolicyError):
        parse_policy({"skills": {"allowlist": [{"publisher": "pub_x", "repository_id": 1}]}})
    with pytest.raises(PolicyError):
        parse_policy({"skills": {"allowlist": [{"source": "github.com/a/b", "repository_id": 0}]}})


def test_capability_ceilings() -> None:
    assert _codes({}, manifest=_manifest(scripts=[{"path": "a.sh"}])) == {"scripts_not_allowed"}
    assert _codes({}, manifest=_manifest(executable_extensions=[{"kind": "commands"}])) == {"scripts_not_allowed"}
    assert _codes({}, manifest=_manifest(hooks=[{"event": "PreToolUse"}])) == {"hooks_not_allowed"}
    assert _codes({}, manifest=_manifest(executable_extensions=[{"kind": "skill-hooks"}])) == {"hooks_not_allowed"}
    assert _codes({}, manifest=_manifest(mcp_local=[{"name": "db"}])) == {"local_mcp_not_allowed"}
    # Declared MCP that resolves to nothing is treated as local (fail closed).
    assert _codes({}, manifest=_manifest(mcp_declared=True)) == {"local_mcp_not_allowed"}
    remote = _manifest(mcp_remote=[{"name": "s", "origin": "https://mcp.example.com"}])
    assert _codes({}, manifest=remote) == set()
    assert _codes({"allow_remote_mcp": False}, manifest=remote) == {"remote_mcp_not_allowed"}
    assert _codes({"allowed_remote_mcp_origins": ["https://mcp.acme.com"]}, manifest=remote) == {
        "remote_mcp_origin_not_allowed"
    }
    assert _codes({"allowed_remote_mcp_origins": ["https://mcp.example.com"]}, manifest=remote) == set()
    hidden = _manifest(unicode_counts={"low": 1})
    assert _codes({}, manifest=hidden) == {"hidden_unicode_not_allowed"}
    assert _codes({"allow_hidden_unicode": True}, manifest=hidden) == set()


def test_trust_gates() -> None:
    assert _codes({"require_signature": True}, attested=False) == {"signature_required"}
    assert _codes({}, dlp_status="blocked") == {"dlp_not_clean"}
    assert _codes({}, scan_max_severity="high") == {"scan_severity"}
    assert _codes({"max_scan_severity": "high"}, scan_max_severity="high") == set()
    assert _codes({}, scan_max_severity="bogus") == {"scan_severity"}  # unknown = worst


def test_unknown_manifest_fails_closed_only_when_ceilings_apply() -> None:
    assert _codes({}, manifest=None) == {"manifest_unavailable"}
    assert _codes({}, manifest=_manifest(incomplete=["too_large:x"])) == {"manifest_unavailable"}
    permissive = {
        "allow_hooks": True, "allow_local_mcp": True, "allow_remote_mcp": True,
        "allow_skill_scripts": True, "allow_hidden_unicode": True,
    }
    assert _codes(permissive, manifest=None) == set()


# Property tests (no hypothesis dependency: seeded random generation).

_BOOL_RESTRICTIVE = {  # field -> the restrictive value
    "allowed_publishers_verified_only": True,
    "allow_hooks": False,
    "allow_local_mcp": False,
    "allow_remote_mcp": False,
    "allow_skill_scripts": False,
    "allow_hidden_unicode": False,
    "require_signature": True,
    "require_dlp_clean_import": True,
}


def _random_policy(rng: random.Random) -> dict:
    policy: dict = {name: rng.random() < 0.5 for name in _BOOL_RESTRICTIVE}
    policy["mode"] = rng.choice(MODES)
    policy["max_scan_severity"] = rng.choice(SEVERITIES)
    policy["allowlist"] = rng.sample(
        [
            {"publisher": "pub_acme"},
            {"publisher": "pub_acme", "require_attestation": True},
            {"source": "github.com/acme/skills", "sha": "any", "repository_id": 4242},
            {"source": "github.com/acme/skills", "sha": "0123456789abcdef0123456789abcdef01234567", "repository_id": 4242},
        ],
        rng.randint(0, 2),
    )
    policy["allowed_remote_mcp_origins"] = rng.sample(
        ["https://mcp.example.com", "https://mcp.acme.com"], rng.randint(0, 1)
    )
    return policy


def _random_subject(rng: random.Random) -> PolicySubject:
    manifest = _manifest(
        scripts=[{"path": "s"}] if rng.random() < 0.3 else [],
        hooks=[{"event": "e"}] if rng.random() < 0.3 else [],
        mcp_local=[{"name": "l"}] if rng.random() < 0.3 else [],
        mcp_remote=[{"name": "r", "origin": rng.choice(["https://mcp.example.com", "https://x.dev"])}]
        if rng.random() < 0.3 else [],
        unicode_counts={"low": 1 if rng.random() < 0.2 else 0},
    )
    return _subject(
        package_org_id=rng.choice([None, "org_a", "org_b"]),
        publisher_id=rng.choice(["pub_acme", "pub_other", None]),
        publisher_verified=rng.random() < 0.5,
        attested=rng.random() < 0.5,
        verified_repository_id=rng.choice([4242, 9999, None]),
        verified_commit=rng.choice(["0123456789abcdef0123456789abcdef01234567", "ffff000011112222333344445555666677778888", None]),
        manifest=manifest if rng.random() < 0.9 else None,
        dlp_status=rng.choice(["passed", "passed", "blocked"]),
        scan_max_severity=rng.choice(SEVERITIES),
    )


def _tighten(policy: dict, rng: random.Random) -> dict:
    tighter = json.loads(json.dumps(policy))
    field = rng.choice(list(_BOOL_RESTRICTIVE) + ["mode", "max_scan_severity", "allowlist", "origins"])
    if field in _BOOL_RESTRICTIVE:
        tighter[field] = _BOOL_RESTRICTIVE[field]
    elif field == "mode":
        tighter["mode"] = MODES[min(len(MODES) - 1, MODES.index(policy["mode"]) + rng.randint(0, 2))]
    elif field == "max_scan_severity":
        tighter["max_scan_severity"] = SEVERITIES[max(0, SEVERITIES.index(policy["max_scan_severity"]) - 1)]
    elif field == "allowlist" and tighter["allowlist"]:
        tighter["allowlist"].pop()
    elif field == "origins":
        # An empty origin list means "any origin"; restricting it is tighter.
        if not tighter["allowed_remote_mcp_origins"]:
            tighter["allowed_remote_mcp_origins"] = ["https://mcp.acme.com"]
    return tighter


def test_property_monotonic_restriction() -> None:
    """Tightening any field never removes a violation."""
    rng = random.Random(20261020)
    for _ in range(3000):
        loose = _random_policy(rng)
        tight = _tighten(loose, rng)
        subject = _random_subject(rng)
        loose_verdict = evaluate(_policy(loose), subject)
        tight_verdict = evaluate(_policy(tight), subject)
        # Never allowed by the tighter policy when refused by the looser one.
        assert loose_verdict.allowed or not tight_verdict.allowed, (loose, tight)
        # Outside the mode (whose single violation names the strictest mode
        # reached), no violation disappears.
        before = {v.code for v in loose_verdict.violations if v.field not in {"mode", "allowlist"}}
        after = {v.code for v in tight_verdict.violations if v.field not in {"mode", "allowlist"}}
        assert before <= after, (loose, tight, before, after)


def test_property_restrictive_input_never_allows_forbidden_capability() -> None:
    rng = random.Random(7)
    for _ in range(2000):
        policy = _policy(_random_policy(rng))
        subject = _random_subject(rng)
        verdict = evaluate(policy, subject)
        manifest = subject.manifest
        if manifest is None:
            continue
        if not policy.allow_hooks and manifest["hooks"]:
            assert not verdict.allowed
        if not policy.allow_skill_scripts and manifest["scripts"]:
            assert not verdict.allowed
        if not policy.allow_local_mcp and manifest["mcp_local"]:
            assert not verdict.allowed
        if policy.mode == "disabled":
            assert not verdict.allowed


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_golden_artifacts(fixture: str) -> None:
    raw, catalogs = FIXTURES[fixture]
    compiled = compile_all(_policy(raw), catalogs)
    update = os.environ.get("SFS_UPDATE_GOLDEN") == "1"
    for tool, result in compiled.items():
        summary = {
            "enforcement": result.enforcement,
            "enforceable": result.enforceable,
            "report_only": result.report_only,
            "unsupported": result.unsupported,
            "warnings": [w.code for w in result.warnings],
        }
        expected_files = {f"{tool}.fields.json": json.dumps(summary, indent=2, sort_keys=True) + "\n"}
        for index, artifact in enumerate(result.artifacts):
            suffix = "" if index == 0 else f".{index}"
            expected_files[f"{tool}{suffix}.{EXT[artifact.format]}"] = artifact.content
        for name, content in expected_files.items():
            path = GOLDEN / fixture / name
            if update:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, "utf-8")
            assert path.is_file(), f"missing golden file {path} (run with SFS_UPDATE_GOLDEN=1)"
            assert path.read_text("utf-8") == content, f"{fixture}/{name} changed"


def test_every_field_lands_in_exactly_one_list() -> None:
    rng = random.Random(3)
    for _ in range(300):
        policy = _policy(_random_policy(rng))
        for tool, result in compile_all(policy, rng.choice([[], CATALOGS])).items():
            listed = result.enforceable + result.report_only + result.unsupported
            assert sorted(listed) == sorted(POLICY_FIELDS), tool
            # Fields SessionFS never checks yet are never shown as enforced.
            assert "auto_update" in result.unsupported


def test_artifacts_parse_and_are_deterministic() -> None:
    rng = random.Random(11)
    for _ in range(200):
        raw = _random_policy(rng)
        first = compile_all(_policy(raw), CATALOGS)
        second = compile_all(_policy(raw), CATALOGS)
        for tool in TOOLS:
            assert first[tool].as_dict() == second[tool].as_dict()
            for artifact in first[tool].artifacts:
                if artifact.format == "json":
                    json.loads(artifact.content)
                elif artifact.format == "toml":
                    tomllib.loads(artifact.content)


def test_compiled_artifact_never_allows_what_the_policy_forbids() -> None:
    rng = random.Random(5)
    for _ in range(500):
        policy = _policy(_random_policy(rng))
        catalogs = rng.choice([[], CATALOGS])
        claude = json.loads(compile_policy(policy, catalogs, "claude-code").artifacts[0].content)
        copilot = json.loads(compile_policy(policy, catalogs, "copilot").artifacts[0].content)
        codex = tomllib.loads(compile_policy(policy, catalogs, "codex").artifacts[0].content)
        restricted = policy.block_unmanaged_marketplaces or policy.mode != "open"
        for settings in (claude, copilot):
            if restricted:
                allowed = settings["strictKnownMarketplaces"]
                # Only SessionFS catalogs (and the skills directory) - never
                # an open marketplace.
                assert all(
                    entry == {"source": "skills-dir"} or entry["url"] in {c.git_url for c in catalogs}
                    for entry in allowed
                )
                if policy.mode == "disabled":
                    assert allowed == []
            else:
                assert "strictKnownMarketplaces" not in settings
            if not policy.allow_local_mcp and not policy.allow_remote_mcp:
                assert settings["allowedMcpServers"] == []
            if policy.allow_local_mcp and not policy.allow_remote_mcp:
                assert settings["deniedMcpServers"]
            # A permissive allow-list is never emitted.
            for entry in settings.get("allowedMcpServers", []):
                assert entry["serverUrl"].removesuffix("/*") in policy.allowed_remote_mcp_origins
        if not policy.allow_hooks:
            assert claude["allowManagedHooksOnly"] is True
            assert codex["allow_managed_hooks_only"] is True
        else:
            assert "allowManagedHooksOnly" not in claude
        if restricted and policy.mode != "disabled":
            assert codex["marketplaces"]["restrict_to_allowed_sources"] is True
        if policy.mode == "disabled":
            assert codex["features"]["plugins"] is False
        if not policy.allow_local_mcp and not policy.allow_remote_mcp:
            assert codex["mcp_servers"] == {}


def test_tool_overrides_and_unsupported_tools() -> None:
    policy = _policy({"tools": {"claude-code": {"enforcement": "off"}, "codex": {"enforcement": "report_only"}}})
    off = compile_policy(policy, [], "claude-code")
    assert off.enforcement == "off" and not off.artifacts and sorted(off.unsupported) == sorted(POLICY_FIELDS)
    report = compile_policy(policy, [], "codex")
    assert report.enforcement == "report_only" and not report.artifacts and not report.enforceable
    assert compile_policy(policy, [], "antigravity").enforcement == "unsupported"
    assert compile_policy(policy, [], "amp").enforcement == "report_only"
    with pytest.raises(KeyError):
        compile_policy(policy, [], "notepad")


def test_every_compiler_cites_its_sources() -> None:
    policy = _policy({"mode": "allowlist"})
    for tool in ("claude-code", "copilot", "vscode", "gemini", "codex", "cursor"):
        result = compile_policy(policy, CATALOGS, tool)
        assert result.sources and all(s["url"].startswith("https://") for s in result.sources), tool
        assert all(s["checked"] == "2026-10-20" for s in result.sources)


def test_cursor_is_a_checklist_with_nothing_enforced() -> None:
    result = compile_policy(_policy({"mode": "allowlist", "allow_local_mcp": False}), CATALOGS, "cursor")
    assert result.enforcement == "checklist"
    assert result.enforceable == []
    [artifact] = result.artifacts
    assert artifact.kind == "checklist" and "- [ ]" in artifact.content


def test_sessionfs_mcp_block_is_always_a_warning() -> None:
    """Every compiled managed-settings file that would block SessionFS's own
    stdio MCP server (`sfs mcp serve`) carries an acknowledgement warning."""
    origins_only = _policy(
        {"allow_local_mcp": False, "allow_remote_mcp": True, "allowed_remote_mcp_origins": ["https://mcp.acme.com"]}
    )
    for tool in ("claude-code", "copilot"):
        result = compile_policy(origins_only, [], tool)
        settings = json.loads(result.artifacts[0].content)
        assert settings["allowedMcpServers"] == [{"serverUrl": "https://mcp.acme.com/*"}]
        assert f"{tool}.sessionfs_mcp_blocked" in {w.code for w in result.warnings}
        assert "SessionFS MCP server" in next(
            w.message for w in result.warnings if w.code == f"{tool}.sessionfs_mcp_blocked"
        )
    rng = random.Random(17)
    for _ in range(500):
        policy = _policy(_random_policy(rng))
        for tool in ("claude-code", "copilot"):
            result = compile_policy(policy, [], tool)
            settings = json.loads(result.artifacts[0].content)
            allowed = settings.get("allowedMcpServers")
            blocks_stdio = allowed is not None and not any("serverCommand" in e or "serverName" in e for e in allowed)
            codes = {w.code for w in result.warnings}
            if blocks_stdio:
                assert codes & {f"{tool}.mcp_lockdown", f"{tool}.sessionfs_mcp_blocked"}, policy
            else:
                assert f"{tool}.sessionfs_mcp_blocked" not in codes


# ---------------------------------------------------------------------------
# Relax-only revisions (an org below Enterprise keeps its last revision)
# ---------------------------------------------------------------------------


def test_relaxing_never_refuses_what_the_old_revision_allowed() -> None:
    """If tightened_fields(old, new) is empty, every version the old
    revision allowed is still allowed by the new one."""
    from sessionfs.skills.policy import tightened_fields

    rng = random.Random(23)
    checked = 0
    for _ in range(20000):
        old = _policy(_random_policy(rng))
        new = _policy(_random_policy(rng))
        if tightened_fields(old, new):
            continue
        checked += 1
        for _ in range(5):
            subject = _random_subject(rng)
            assert not evaluate(old, subject).allowed or evaluate(new, subject).allowed, (old, new)
    assert checked > 200


def test_tightening_is_named_field_by_field() -> None:
    from sessionfs.skills.policy import tightened_fields, unrestricted_from

    base = _policy({"mode": "allowlist", "allow_hooks": True, "allowlist": [{"publisher": "pub_acme"}]})
    assert tightened_fields(base, base) == []
    assert tightened_fields(base, _policy({"mode": "disabled", "allow_hooks": True})) == ["mode"]
    assert tightened_fields(base, _policy({"mode": "allowlist", "allow_hooks": True})) == ["allowlist"]
    assert tightened_fields(base, _policy({"mode": "allowlist", "allowlist": [{"publisher": "pub_acme"}]})) == [
        "allow_hooks"
    ]
    assert "allowed_remote_mcp_origins" in tightened_fields(
        base,
        _policy({"mode": "allowlist", "allow_hooks": True, "allowlist": [{"publisher": "pub_acme"}],
                 "allowed_remote_mcp_origins": ["https://mcp.acme.com"]}),
    )
    assert "tools" in tightened_fields(
        _policy({"tools": {"codex": {"enforcement": "off"}}}), _policy({})
    )
    # Relaxing to open, or disabling, is never a tightening.
    assert tightened_fields(base, _policy({"mode": "open", "allow_hooks": True})) == []
    for raw in (
        {"mode": "disabled", "auto_update": "non_widening", "trust_code_updates_publishers": ["pub_x"],
         "tools": {"amp": {"enforcement": "off"}}},
        {},
    ):
        old = _policy(raw)
        assert tightened_fields(old, unrestricted_from(old).skills) == []
    assert all(r.warnings == [] for r in compile_all(unrestricted_from(None).skills, CATALOGS).values())
