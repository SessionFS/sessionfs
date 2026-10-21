"""Registry polling building blocks (GitHub #69): tag patterns, the SSRF
guard, credential references, the git transport and the hardened fetch."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from sessionfs.server.services import registry_polling as rp
from sessionfs.skills.models import SkillError
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is required")


# ---------------------------------------------------------------------------
# Local git fixtures (shared with the integration tests)
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [GIT or "git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def plugin_files(name: str = "review-helper", *, version: str = "1.0.0", body: str = "Inspect the diff.") -> dict[str, str]:
    manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": name,
        "version": version,
        "description": f"{name} for the team.",
        "license": "MIT",
    }
    skill = (
        f"---\nname: {name}\ndescription: Review changes against the project contract.\n---\n\n"
        f"# {name}\n\n{body}\n"
    )
    return {"plugin.json": json.dumps(manifest), f"skills/{name}/SKILL.md": skill}


def write_tree(repo: Path, files: dict[str, str]) -> None:
    for existing in repo.iterdir():
        if existing.name == ".git":
            continue
        if existing.is_dir():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    # Partial-clone filters must be allowed for the bounded fetch.
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    return repo


def marketplace_tree(plugins: dict[str, dict[str, str]], *, external: bool = False) -> dict[str, str]:
    files: dict[str, str] = {}
    entries = []
    for name, tree in plugins.items():
        for rel, content in tree.items():
            files[f"plugins/{name}/{rel}"] = content
        entries.append({"name": name, "source": f"./plugins/{name}", "description": name})
    if external:
        entries.append({"name": "far-away", "source": {"source": "url", "url": "https://example.com/x.git"}})
    files[".claude-plugin/marketplace.json"] = json.dumps({"name": "team", "plugins": entries})
    return files


def file_transport(repo: Path, sandbox: Path) -> rp.GitTransport:
    """The production environment, except the protocol allowlist names
    ``file`` so the tests can serve a local repository."""
    env = rp._base_env(sandbox)
    env["GIT_ALLOW_PROTOCOL"] = "file"
    return rp.GitTransport(url=f"file://{repo}", env=env, config=())


# ---------------------------------------------------------------------------
# Tag patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [("v1.2.3", True), ("1.2.3", True), ("v10.20.30", True), ("v1.2", False), ("v1.2.3-rc1", False),
     ("release-1", False), ("vv1.2.3", False), ("", False)],
)
def test_default_tag_pattern_is_semver(name: str, expected: bool) -> None:
    assert rp.compile_tag_pattern(None).fullmatch(name) is expected


@pytest.mark.parametrize(
    "pattern",
    [r"v\d+\.\d+\.\d+", r"release-[0-9]{4}\.[0-9]{2}", r"^v\d+\.\d+\.\d+.*$",
     r"[a-z]+/v\d+", r"[^-]+-stable", r"v\d{1,3}\.\d{1,3}\.\d{1,3}", r"x?y*z+"],
)
def test_restricted_patterns_agree_with_re(pattern: str) -> None:
    compiled = rp.compile_tag_pattern(pattern)
    reference = re.compile(pattern.lstrip("^").rstrip("$"))
    samples = ["v1.2.3", "v1.2.3-rc1", "release-2026.10", "tools/v12", "abc-stable", "-stable", "xyz", "z",
               "yyz", "v123.4.5", "v1234.4.5", "", "v1", "a/b/v1"]
    for sample in samples:
        assert compiled.fullmatch(sample) == bool(reference.fullmatch(sample)), (pattern, sample)


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+", "a|b", r"(\d+)\1", "(?=v)v", "a**", "a+?", "a{70}", "a{3,2}", "[]", "[a-", "\\", r"\bv",
     "a{0}", "x" * 201, "vé", "(v)"],
)
def test_unsafe_or_unsupported_patterns_are_refused(pattern: str) -> None:
    with pytest.raises(ValueError):
        rp.compile_tag_pattern(pattern)


def test_pathological_pattern_runs_in_linear_time() -> None:
    # Catastrophic for a backtracking engine: (\d*){20} then a char that never comes.
    pattern = rp.compile_tag_pattern(r"\d*" * 20 + "x")
    started = time.perf_counter()
    for _ in range(20):
        assert pattern.fullmatch("1" * 200) is False
    assert time.perf_counter() - started < 2.0


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,allowed",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.5", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00:ec2::254", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("2002:0a00:0001::1", False),  # 6to4 wrapping 10.0.0.1
        ("64:ff9b::a00:1", False),  # NAT64 wrapping 10.0.0.1
        ("224.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_address_guard(address: str, allowed: bool) -> None:
    assert rp.address_allowed(address, []) is allowed


def test_private_opt_in_never_reaches_loopback_or_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS", "10.0.0.0/8, 169.254.0.0/16")
    # A list naming link-local is refused as a whole (fail closed).
    assert rp.allowed_private_networks() == []
    monkeypatch.setenv("SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS", "10.20.0.0/16")
    networks = rp.allowed_private_networks()
    assert rp.address_allowed("10.20.1.1", networks)
    assert not rp.address_allowed("10.21.1.1", networks)
    assert not rp.address_allowed("127.0.0.1", networks)
    assert not rp.address_allowed("169.254.169.254", networks)
    monkeypatch.setenv("SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS", "garbage")
    assert rp.allowed_private_networks() == []


def _fake_resolver(mapping: dict[str, list[str]]):
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in mapping[host]]

    return getaddrinfo


def test_private_resolution_and_rebinding_sets_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_resolver(
            {
                "git.example.com": ["140.82.112.3"],
                "internal.example.com": ["10.0.0.7"],
                # DNS rebinding: one public and one internal answer.
                "rebind.example.com": ["140.82.112.3", "169.254.169.254"],
            }
        ),
    )
    assert rp.resolve_host("git.example.com", 443) == ["140.82.112.3"]
    with pytest.raises(rp.FetchBlocked):
        rp.resolve_host("internal.example.com", 443)
    with pytest.raises(rp.FetchBlocked):
        rp.resolve_host("rebind.example.com", 443)
    with pytest.raises(rp.FetchBlocked):
        rp.resolve_host("missing.example.com", 443)
    with pytest.raises(rp.FetchBlocked):
        rp.resolve_host("127.0.0.1", 443)


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example.com/acme/skills",
        "ssh://git@git.example.com/acme/skills",
        "https://user:pw@git.example.com/acme/skills",
        "https://token@git.example.com/acme/skills",
        "file:///etc/passwd",
        "ext::sh -c id",
        "https://git.example.com/acme/skills?x=1",
    ],
)
def test_poll_url_refuses_non_https_and_userinfo(url: str) -> None:
    with pytest.raises(rp.FetchBlocked):
        rp.check_poll_url(url)


def test_transport_pins_resolution_disables_redirects_and_keeps_credentials_out_of_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver({"git.example.com": ["140.82.112.3"]}))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    token = "glpat-SECRETSECRETSECRET1234"
    transport = rp.prepare_transport(
        "https://git.example.com/acme/skills.git",
        rp.Credential(username="deploy", token=token),
        tmp_path,
    )
    assert transport.url == "https://git.example.com/acme/skills.git"
    assert "http.curloptResolve=git.example.com:443:140.82.112.3" in transport.config
    assert "http.followRedirects=false" in transport.config
    assert "protocol.allow=never" in transport.config
    joined = " ".join(transport.config) + transport.url + repr(transport)
    assert token not in joined
    # The process environment is replaced, not inherited.
    assert "HTTPS_PROXY" not in transport.env and "GIT_TRACE_CURL" not in transport.env
    assert transport.env["GIT_ALLOW_PROTOCOL"] == "https"
    assert transport.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert transport.env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert transport.env["HOME"] == str(tmp_path)
    assert transport.env["GIT_TERMINAL_PROMPT"] == "0"
    assert transport.env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraHeader"
    expected = base64.b64encode(f"deploy:{token}".encode()).decode()
    assert transport.env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert token in transport.secrets
    assert token not in repr(rp.Credential(username="deploy", token=token))


def test_transport_keeps_the_registry_path_and_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_resolver({"dev.azure.com": ["13.107.42.20"], "git.example.com": ["140.82.112.3"]}),
    )
    azure = rp.prepare_transport("https://dev.azure.com/acme/proj/_git/skills", None, tmp_path)
    assert azure.url == "https://dev.azure.com/acme/proj/_git/skills"
    custom = rp.prepare_transport("https://GIT.example.com:8443/acme/skills.git/", None, tmp_path)
    assert custom.url == "https://git.example.com:8443/acme/skills.git"
    assert "http.curloptResolve=git.example.com:8443:140.82.112.3" in custom.config


def test_transport_refuses_private_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver({"gitlab.corp": ["10.4.0.9"]}))
    with pytest.raises(rp.FetchBlocked):
        rp.prepare_transport("https://gitlab.corp/acme/skills", None, tmp_path)
    monkeypatch.setenv("SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS", "10.4.0.0/16")
    transport = rp.prepare_transport("https://gitlab.corp/acme/skills", None, tmp_path)
    assert "http.curloptResolve=gitlab.corp:443:10.4.0.9" in transport.config
    assert "GIT_CONFIG_COUNT" not in transport.env


# ---------------------------------------------------------------------------
# Credential references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["ghp_abcdefabcdefabcdefabcdef", "env:SFS_DATABASE_URL", "env:HOME", "projects/p/secrets/s/versions/1",
     "k8s:../etc", "k8s:UPPER", "env:SFS_REGISTRY_CRED_", ""],
)
def test_credential_ref_validation_refuses_plaintext_and_foreign_env(value: str) -> None:
    with pytest.raises(ValueError):
        rp.validate_credential_ref(value)


def _reason(ref: str, **kwargs) -> str:
    with pytest.raises(rp.CredentialError) as caught:
        rp.resolve_credential(ref, **kwargs)
    # One generic user-facing message for every cause (no existence oracle).
    assert str(caught.value) == rp.CREDENTIAL_UNAVAILABLE
    return caught.value.reason


def test_env_credential_must_be_bound_to_the_org_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "env:SFS_REGISTRY_CRED_ACME"
    kwargs = {"org_id": "org_a", "host": "git.example.com"}
    assert rp.validate_credential_ref(ref) == ref
    assert "did not resolve" in _reason(ref, **kwargs)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME", "tok-123")
    assert "not bound to an organization" in _reason(ref, **kwargs)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_ORG", "org_b")
    assert "different organization" in _reason(ref, **kwargs)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_ORG", "org_a")
    # The host binding is required: without it the credential is unusable.
    assert "host binding" in _reason(ref, **kwargs)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_HOSTS", "gitlab.example.com")
    assert "not bound to this registry host" in _reason(ref, **kwargs)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_HOSTS", "gitlab.example.com, GIT.example.com.")
    credential = rp.resolve_credential(ref, **kwargs)
    assert credential.token == "tok-123" and credential.username == rp.DEFAULT_CREDENTIAL_USERNAME
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME", "tok\r\nX-Injected: 1")
    assert "unsupported characters" in _reason(ref, **kwargs)
    for value in ("tok-123",):
        assert value not in rp.CREDENTIAL_UNAVAILABLE


def test_k8s_credential_reads_a_mounted_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "acme-gitlab"
    secret.mkdir()
    (secret / "token").write_text("deploy-token\n")
    (secret / "org").write_text("org_a\n")
    (secret / "username").write_text("gitlab+deploy-token-1\n")
    monkeypatch.setenv("SFS_REGISTRY_SECRETS_DIR", str(tmp_path))
    kwargs = {"org_id": "org_a", "host": "gitlab.example.com"}
    assert "host binding" in _reason("k8s:acme-gitlab", **kwargs)
    (secret / "hosts").write_text("gitlab.example.com\n")
    credential = rp.resolve_credential("k8s:acme-gitlab", **kwargs)
    assert credential.username == "gitlab+deploy-token-1" and credential.token == "deploy-token"
    assert "different organization" in _reason("k8s:acme-gitlab", org_id="org_other", host="gitlab.example.com")
    assert "did not resolve" in _reason("k8s:missing", **kwargs)


def test_sanitize_summary_strips_credentials() -> None:
    token = "glpat-AAAAAAAAAAAAAAAAAAAAAA"
    basic = base64.b64encode(f"u:{token}".encode()).decode()
    text = (
        f"fatal: could not read from https://u:{token}@git.example.com/x: Authorization: Basic {basic}\n"
        f"remote said {token} and ghp_{'a' * 36}\x1b[31m"
    )
    clean = rp.sanitize_summary(text, (token, basic))
    assert token not in clean and basic not in clean and "ghp_" not in clean
    assert "u:" not in clean and "\x1b" not in clean
    assert len(rp.sanitize_summary("x" * 5000)) <= 300


def test_backoff_is_exponential_and_capped() -> None:
    assert rp.backoff_seconds(900, 1) == 1800
    assert rp.backoff_seconds(900, 2) == 3600
    assert rp.backoff_seconds(900, 3) == 7200
    assert rp.backoff_seconds(900, 30) == rp.MAX_BACKOFF_SECONDS
    assert rp.backoff_seconds(60, 1) == 600  # the 300 s minimum is the base


def test_version_sort_is_numeric() -> None:
    names = ["v1.10.0", "v1.9.0", "v1.2.0", "v2.0.0"]
    assert sorted(names, key=rp.version_sort_key) == ["v1.2.0", "v1.9.0", "v1.10.0", "v2.0.0"]


@pytest.mark.parametrize("name", ["main", "release/2026", "feature-x"])
def test_branch_names(name: str) -> None:
    assert rp.validate_branch(name) == name


@pytest.mark.parametrize("name", ["", "-x", "a..b", "x.lock", "a b", "a@{1}", "/x", "x/"])
def test_bad_branch_names(name: str) -> None:
    with pytest.raises(ValueError):
        rp.validate_branch(name)


# ---------------------------------------------------------------------------
# Real git: listing, fetch, isolation, caps
# ---------------------------------------------------------------------------


@needs_git
def test_list_refs_peels_annotated_tags_and_filters_unsafe_names(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_tree(repo, plugin_files())
    first = commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "-a", "v1.1.0", "-m", "annotated")
    git(repo, "tag", "release-x")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    refs = {ref.name: ref for ref in rp.list_remote_refs(file_transport(repo, sandbox), sandbox)}
    assert set(refs) == {"v1.0.0", "v1.1.0", "release-x"}
    assert refs["v1.1.0"].commit == first  # peeled, not the tag object
    branch = rp.list_remote_refs(file_transport(repo, sandbox), sandbox, branch="main")
    assert [(ref.name, ref.commit) for ref in branch] == [("main", first)]


@needs_git
def test_fetch_collects_marketplace_plugins_and_skips_external_sources(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_tree(
        repo,
        marketplace_tree({"alpha": plugin_files("alpha"), "beta": plugin_files("beta")}, external=True),
    )
    commit = commit_all(repo, "two plugins")
    git(repo, "tag", "v1.0.0")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    ref = rp.RemoteRef(name="v1.0.0", ref="refs/tags/v1.0.0", commit=commit)
    packages = rp.fetch_ref_packages(file_transport(repo, sandbox), ref, deadline=time.monotonic() + 60)
    by_name = {package.name: package for package in packages}
    assert by_name["alpha"].data and by_name["alpha"].subdir == "plugins/alpha"
    assert by_name["beta"].data
    assert by_name["far-away"].data is None and by_name["far-away"].error_code == "external_source_not_fetched"


@needs_git
def test_fetch_refuses_a_moved_ref_and_oversized_files_and_expired_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_tree(repo, plugin_files())
    commit = commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    transport = file_transport(repo, sandbox)
    stale = rp.RemoteRef(name="v1.0.0", ref="refs/tags/v1.0.0", commit="0" * 40)
    with pytest.raises(SkillError, match="resolved to"):
        rp.fetch_ref_packages(transport, stale, deadline=time.monotonic() + 60)
    with pytest.raises(SkillError, match="timed out"):
        rp.fetch_ref_packages(
            transport, rp.RemoteRef("v1.0.0", "refs/tags/v1.0.0", commit), deadline=time.monotonic() - 1
        )
    from sessionfs.skills import sources

    monkeypatch.setattr(sources, "PER_FILE_CAP_BYTES", 64)
    files = plugin_files()
    files["skills/review-helper/references/big.md"] = "x" * 4096
    write_tree(repo, files)
    big = commit_all(repo, "big")
    git(repo, "tag", "v2.0.0")
    # Refused before any blob is read (the filtered blob is never fetched).
    with pytest.raises(SkillError, match="per-file limit|invalid entry"):
        rp.fetch_ref_packages(transport, rp.RemoteRef("v2.0.0", "refs/tags/v2.0.0", big), deadline=time.monotonic() + 60)


@needs_git
def test_fetch_ignores_global_config_hooks_and_templates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof that no hook can run: the API process's own HOME, global config,
    hooksPath and template directory (each carrying a hook that would drop a
    marker) are all ignored, because git runs with a replaced environment
    and core.hooksPath=/dev/null."""
    marker = tmp_path / "HOOK_RAN"
    evil = tmp_path / "evil"
    hooks = evil / "hooks"
    hooks.mkdir(parents=True)
    script = f"#!/bin/sh\ntouch {marker}\n"
    for hook in ("reference-transaction", "post-checkout", "post-merge", "pre-auto-gc", "fsmonitor-watchman"):
        path = hooks / hook
        path.write_text(script)
        path.chmod(0o755)
    template = evil / "template"
    shutil.copytree(hooks, template / "hooks")
    gitconfig = evil / "gitconfig"
    gitconfig.write_text(
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {hooks / 'fsmonitor-watchman'}\n"
        f"[init]\n\ttemplateDir = {template}\n"
        "[protocol]\n\tallow = always\n"
    )
    monkeypatch.setenv("HOME", str(evil))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    repo = make_repo(tmp_path)
    write_tree(repo, plugin_files())
    commit = commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    transport = file_transport(repo, sandbox)
    assert "GIT_TEMPLATE_DIR" not in transport.env
    rp.list_remote_refs(transport, sandbox)
    packages = rp.fetch_ref_packages(
        transport, rp.RemoteRef("v1.0.0", "refs/tags/v1.0.0", commit), deadline=time.monotonic() + 60
    )
    assert packages and packages[0].data
    assert not marker.exists()


@needs_git
def test_production_transport_refuses_the_file_protocol(tmp_path: Path) -> None:
    """Without the test override, GIT_ALLOW_PROTOCOL=https: a local path can
    never be fetched even if it reached git."""
    repo = make_repo(tmp_path)
    write_tree(repo, plugin_files())
    commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    transport = rp.GitTransport(url=f"file://{repo}", env=rp._base_env(sandbox), config=())
    with pytest.raises(SkillError):
        rp.list_remote_refs(transport, sandbox)


@needs_git
def test_fetch_refuses_repositories_over_the_file_and_byte_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.skills import sources

    repo = make_repo(tmp_path)
    files = plugin_files()
    for index in range(6):
        files[f"skills/review-helper/references/n{index}.md"] = "note\n"
    write_tree(repo, files)
    commit = commit_all(repo, "many")
    git(repo, "tag", "v1.0.0")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    ref = rp.RemoteRef("v1.0.0", "refs/tags/v1.0.0", commit)
    monkeypatch.setattr(sources, "MAX_GIT_FILES", 3)
    with pytest.raises(SkillError, match="exceeds import limits"):
        rp.fetch_ref_packages(file_transport(repo, sandbox), ref, deadline=time.monotonic() + 60)
    monkeypatch.setattr(sources, "MAX_GIT_FILES", 5_000)
    monkeypatch.setattr(sources, "MAX_GIT_TOTAL_BYTES", 100)
    with pytest.raises(SkillError, match="exceeds import limits"):
        rp.fetch_ref_packages(file_transport(repo, sandbox), ref, deadline=time.monotonic() + 60)


@needs_git
def test_redirects_are_never_followed_to_another_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry that answers with a redirect (e.g. to an internal host) is
    refused by git itself: the production config sets
    http.followRedirects=false, so the second host is never contacted."""
    import http.server
    import threading

    hits: list[str] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence
            pass

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_port = target.server_address[1]

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}{self.path}")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(rp, "resolve_host", lambda host, port: ["140.82.112.3"])
        production = rp.prepare_transport("https://git.example.com/acme/skills", None, tmp_path)
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        env = rp._base_env(sandbox)
        env["GIT_ALLOW_PROTOCOL"] = "http"  # the test servers speak plain http
        config = tuple(item for item in production.config if not item.startswith("http.curloptResolve"))
        transport = rp.GitTransport(
            url=f"http://127.0.0.1:{redirector.server_address[1]}/acme/skills.git", env=env, config=config
        )
        with pytest.raises(SkillError):
            rp.list_remote_refs(transport, sandbox)
        assert hits == []
    finally:
        for server in (target, redirector):
            server.shutdown()
            server.server_close()
