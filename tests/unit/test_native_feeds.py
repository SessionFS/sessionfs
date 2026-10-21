"""Native per-tool catalogs: golden feeds, determinism, L0 checks, git
safety, a real `git clone` over dumb HTTP with the credential helper, and
the opt-in works-in harness (marketplace v2 §7)."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sessionfs.skills import native_feeds, works_in_harness
from sessionfs.skills.catalog_credential import credential_response, parse_request
from sessionfs.skills.git_catalog import GitCatalogError, build_repository, validate_repo_path
from tests.utils.ansi import strip_ansi

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "native_feeds"
UPDATE = os.environ.get("SFS_UPDATE_GOLDENS") == "1"
SKILL = b"---\nname: review-helper\ndescription: Review changes against the project contract.\n---\n\n# Review\n"
DEPLOY = b"---\nname: deploy\ndescription: Deploy the service.\n---\n\nRun the deploy checklist.\n"


def _core(name: str, **extra) -> bytes:
    return json.dumps(
        {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": name,
         "version": "1.2.3", **extra},
        sort_keys=True,
    ).encode()


def _inputs() -> list[native_feeds.CatalogInput]:
    return [
        native_feeds.CatalogInput(
            version_id="skv_" + "1" * 24,
            package_id="spk_" + "1" * 24,
            namespace="acme",
            name="deploy",
            description="Deploy with the Acme runbook.",
            version_label="1.4.0",
            content_hash="sha256:" + "a" * 64,
            published_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            files={
                "plugin.json": _core("deploy", license="MIT"),
                "skills/deploy/SKILL.md": DEPLOY,
                # A package's own vendor manifest is replaced, never merged.
                ".claude-plugin/plugin.json": b'{"name": "evil", "hooks": "./x.json"}',
            },
            license="MIT",
            homepage="https://app.sessionfs.dev/marketplace/acme/deploy",
            publisher_name="Acme",
            omitted_components=("hooks_require_consent",),
        ),
        native_feeds.CatalogInput(
            version_id="skv_" + "2" * 24,
            package_id="spk_" + "2" * 24,
            namespace="pub",
            name="review-helper",
            description="Review a change.\nSecond line\x1b[31m",
            version_label="1.2.3",
            content_hash="sha256:" + "b" * 64,
            published_at=datetime(2026, 10, 2, 8, 30, tzinfo=timezone.utc),
            files={"plugin.json": _core("review-helper"), "skills/review-helper/SKILL.md": SKILL},
        ),
    ]


def _golden(name: str, data: bytes) -> None:
    path = GOLDEN / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert path.exists(), f"missing golden {path}; run with SFS_UPDATE_GOLDENS=1"
    assert data == path.read_bytes(), f"{name} differs from its golden copy"


# ---------------------------------------------------------------------------
# Golden feeds
# ---------------------------------------------------------------------------

GOLDEN_FILES = (
    ".claude-plugin/marketplace.json",
    ".github/plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    ".cursor-plugin/marketplace.json",
    "SESSIONFS-CATALOG.json",
    "plugins/acme--deploy/plugin.json",
    "plugins/acme--deploy/.claude-plugin/plugin.json",
    "plugins/acme--deploy/.cursor-plugin/plugin.json",
    "plugins/acme--deploy/gemini-extension.json",
)


@pytest.mark.parametrize("scope", ["public", "org"])
def test_golden_feeds(scope: str) -> None:
    projection = native_feeds.build_catalog(scope, None if scope == "public" else "org_acme", _inputs())
    for path in GOLDEN_FILES:
        if scope == "org" and path.endswith("gemini-extension.json"):
            assert path not in projection.files  # Gemini: public mirror only
            continue
        _golden(f"{scope}/{path.replace('/', '__')}", projection.files[path])
    repo = build_repository(projection.files, timestamp=projection.timestamp, message="golden")
    _golden(f"{scope}/COMMIT", (repo.commit_sha + "\n").encode())


def test_projection_is_deterministic_and_order_independent() -> None:
    first = native_feeds.build_catalog("public", None, _inputs())
    second = native_feeds.build_catalog("public", None, list(reversed(_inputs())))
    assert first.files == second.files
    a = build_repository(first.files, timestamp=first.timestamp, message="m")
    b = build_repository(second.files, timestamp=second.timestamp, message="m")
    assert a.commit_sha == b.commit_sha
    assert a.pack() == b.pack()
    # Timestamp is the newest published_at, not the wall clock.
    assert first.timestamp == int(datetime(2026, 10, 2, 8, 30, tzinfo=timezone.utc).timestamp())


def test_generated_manifests_replace_package_vendor_files() -> None:
    projection = native_feeds.build_catalog("public", None, _inputs())
    claude = json.loads(projection.files["plugins/acme--deploy/.claude-plugin/plugin.json"])
    assert claude == {
        "author": {"name": "Acme"},
        "description": "Deploy with the Acme runbook.",
        "homepage": "https://app.sessionfs.dev/marketplace/acme/deploy",
        "license": "MIT",
        "name": "acme--deploy",
        "version": "1.4.0",
    }
    entry = json.loads(projection.files[".claude-plugin/marketplace.json"])["plugins"][1]
    # Control characters never reach a catalog.
    assert entry["description"] == "Review a change. Second line[31m"
    assert "version" not in entry  # plugin.json owns the version
    index = json.loads(projection.files["SESSIONFS-CATALOG.json"])
    assert index["plugins"][0]["omitted_components"] == ["hooks_require_consent"]
    assert all(plugin["projection_hash"].startswith("sha256:") for plugin in index["plugins"])


def test_l0_passes_for_every_tool_on_golden_inputs() -> None:
    projection = native_feeds.build_catalog("public", None, _inputs())
    for verdicts in projection.verdicts.values():
        assert {(v.tool, v.status) for v in verdicts} == {(t, "pass") for t in native_feeds.NATIVE_TOOLS}
    private = native_feeds.build_catalog("org", "org_acme", _inputs())
    gemini = [v for vs in private.verdicts.values() for v in vs if v.tool == "gemini"]
    assert {v.status for v in gemini} == {"skipped"}


def test_l0_flags_reserved_claude_names_and_missing_descriptions() -> None:
    item = _inputs()[1]
    reserved = native_feeds.CatalogInput(**{**item.__dict__, "namespace": "claude", "version_id": "skv_x"})
    no_description = native_feeds.CatalogInput(
        **{**item.__dict__, "namespace": "nodesc", "version_id": "skv_y", "package_id": "spk_y",
           "files": {"plugin.json": _core("x"), "skills/x/SKILL.md": b"---\nname: x\n---\nbody\n"}}
    )
    projection = native_feeds.build_catalog("public", None, [reserved, no_description])
    by_tool = {v.tool: v for v in projection.verdicts["skv_x"]}
    assert by_tool["claude-code"].status == "fail"
    assert "reserved" in by_tool["claude-code"].reason
    assert by_tool["codex"].status == "pass"
    nodesc = {v.tool: v for v in projection.verdicts["skv_y"]}
    assert nodesc["antigravity"].status == "fail" and "description" in nodesc["antigravity"].reason
    assert nodesc["gemini"].status == "fail"


def test_plugin_id_collisions_get_a_stable_suffix() -> None:
    a, b = _inputs()
    dotted = native_feeds.CatalogInput(**{**b.__dict__, "namespace": "pub.x", "name": "review",
                                          "version_id": "skv_d", "package_id": "spk_d"})
    dashed = native_feeds.CatalogInput(**{**b.__dict__, "namespace": "pub-x", "name": "review",
                                          "version_id": "skv_e", "package_id": "spk_e"})
    ids = native_feeds.plugin_ids([dotted, dashed])
    assert len(set(ids.values())) == 2
    assert all(value.startswith("pub-x--review-") for value in ids.values())
    assert native_feeds.plugin_ids([dashed, dotted]) == ids


@pytest.mark.parametrize(
    "path",
    [".git/config", "a/.GIT/x", "a/.gitmodules", "x/.gitattributes", "GIT~1/HEAD", ".git./x",
     "a/.git‌/x", "../x", "/abs", "a//b", "a\\b", "a/\x01", ""],
)
def test_git_metadata_and_unsafe_paths_are_refused(path: str) -> None:
    with pytest.raises(GitCatalogError):
        validate_repo_path(path)


def test_unsafe_package_member_excludes_only_that_entry() -> None:
    a, b = _inputs()
    bad = native_feeds.CatalogInput(**{**a.__dict__, "files": {**a.files, "skills/deploy/.gitattributes": b"*"}})
    projection = native_feeds.build_catalog("public", None, [bad, b])
    assert [plugin.plugin_id for plugin in projection.plugins] == ["pub--review-helper"]
    assert projection.excluded[0]["version_id"] == a.version_id


def test_marketplace_names() -> None:
    assert native_feeds.marketplace_name("public", None) == "sessionfs"
    assert native_feeds.marketplace_name("org", "org_ABC_1") == "sessionfs-org-org-abc-1"
    user = native_feeds.marketplace_name("user", "6f1c")
    assert user.startswith("sessionfs-user-") and "6f1c" not in user


# ---------------------------------------------------------------------------
# Real git over dumb HTTP + credential helper
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self, files: dict[str, bytes], key: str) -> None:
        self.files = files
        self.key = key
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D401 - silence
                return

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.path)
                expected = "Basic " + base64.b64encode(f"sessionfs:{outer.key}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="SessionFS catalog"')
                    self.end_headers()
                    return
                path = self.path.split("?", 1)[0].removeprefix("/cat.git/")
                data = outer.files.get(path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _git_env(home: Path, api_url: str, key: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "SESSIONFS_API_URL": api_url,
        "SESSIONFS_API_KEY": key,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    return env


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_system_git_clones_catalog_with_credential_helper(tmp_path: Path) -> None:
    projection = native_feeds.build_catalog("public", None, _inputs())
    repo = build_repository(projection.files, timestamp=projection.timestamp, message="catalog",
                            tags={"v1.4.0": None})
    key = "sk_sfs_" + "c" * 32
    home = tmp_path / "home"
    home.mkdir()
    helper = f"!{sys.executable} -m sessionfs.cli.main catalog credential"
    with _Server(repo.served_files(), key) as server:
        api_url = f"http://127.0.0.1:{server.port}"
        env = _git_env(home, api_url, key)
        url = f"{api_url}/cat.git"
        # Without the helper: refused.
        denied = subprocess.run(["git", "clone", "-q", url, str(tmp_path / "denied")], env=env,
                                capture_output=True, text=True, check=False)
        assert denied.returncode != 0
        clone = subprocess.run(
            ["git", "-c", f"credential.{api_url}.helper={helper}", "clone", "-q", url, str(tmp_path / "c")],
            env=env, capture_output=True, text=True, check=False,
        )
        assert clone.returncode == 0, clone.stderr
        assert any(path.startswith("/cat.git/objects/pack/") for path in server.requests)
        checkout = tmp_path / "c"
        fsck = subprocess.run(["git", "-C", str(checkout), "fsck", "--strict"], env=env,
                              capture_output=True, text=True, check=False)
        assert fsck.returncode == 0, fsck.stderr
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
        assert head == repo.commit_sha
        tags = subprocess.run(["git", "-C", str(checkout), "ls-remote", "--tags", "origin"], env=env,
                              capture_output=True, text=True, check=False)
        for path, data in projection.files.items():
            assert (checkout / path).read_bytes() == data
        assert not any(os.access(p, os.X_OK) for p in checkout.rglob("*")
                           if p.is_file() and ".git" not in p.relative_to(checkout).parts)
        assert tags.returncode != 0 or repo.commit_sha in tags.stdout or tags.stdout == ""


def test_credential_helper_answers_only_for_the_sessionfs_origin() -> None:
    key = "sk_sfs_secret"
    ok = credential_response({"protocol": "https", "host": "api.sessionfs.dev"},
                             api_url="https://api.sessionfs.dev", api_key=key)
    assert ok == f"username=sessionfs\npassword={key}\n"
    for request in (
        {"protocol": "https", "host": "github.com"},
        {"protocol": "http", "host": "api.sessionfs.dev"},
        {"protocol": "https", "host": "api.sessionfs.dev.evil.com"},
        {"protocol": "https", "host": "api.sessionfs.dev", "path": "other/repo.git"},
    ):
        assert credential_response(request, api_url="https://api.sessionfs.dev", api_key=key) == ""
    # Plain http only for loopback (local development).
    assert credential_response({"protocol": "http", "host": "127.0.0.1:8000"},
                               api_url="http://127.0.0.1:8000", api_key=key)
    assert credential_response({"protocol": "https", "host": "api.sessionfs.dev"},
                               api_url="https://api.sessionfs.dev", api_key="") == ""
    assert parse_request("protocol=https\nhost=x\n\nignored=1\n") == {"protocol": "https", "host": "x"}


def test_credential_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli.main import app

    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_cli")
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test")
    runner = CliRunner()
    result = runner.invoke(app, ["catalog", "credential", "get"],
                           input="protocol=https\nhost=api.example.test\n\n")
    assert result.exit_code == 0
    assert result.stdout == "username=sessionfs\npassword=sk_sfs_cli\n"
    other = runner.invoke(app, ["catalog", "credential", "get"], input="protocol=https\nhost=github.com\n\n")
    assert other.stdout == ""
    assert runner.invoke(app, ["catalog", "credential", "store"], input="password=x\n\n").stdout == ""


def test_catalog_add_prints_tool_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli import cmd_catalog
    from sessionfs.cli.main import app

    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_cli")
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test")
    clone = "https://api.example.test/api/v1/git/public/sessionfs.git"
    body = {"items": [{"scope": "public", "scope_id": None, "marketplace": "sessionfs", "clone_url": clone,
                       "plugin_count": 1,
                       "tools": {"claude-code": {"add": f"/plugin marketplace add {clone}",
                                                 "install": "/plugin install <plugin>@sessionfs"}}}]}
    monkeypatch.setattr(cmd_catalog, "_request", lambda method, path, payload=None: body)
    result = CliRunner().invoke(app, ["catalog", "add", "--for", "claude-code", "--no-credential-helper"])
    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output)
    assert f"/plugin marketplace add {clone}" in output
    missing = CliRunner().invoke(app, ["catalog", "add", "--for", "gemini", "--no-credential-helper"])
    assert missing.exit_code == 1
    org = CliRunner().invoke(app, ["catalog", "add", "--for", "claude-code", "--org", "org_x",
                                   "--no-credential-helper"])
    assert org.exit_code == 1
    assert "Team or above" in strip_ansi(org.output)
    assert cmd_catalog.credential_config_key("https://API.example.test:8443/x") == (
        "credential.https://api.example.test:8443.helper"
    )


# ---------------------------------------------------------------------------
# Opt-in harness
# ---------------------------------------------------------------------------


def _bundle(files: dict[str, bytes], *, symlink: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo("plugins/x/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return output.getvalue()


def test_harness_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(works_in_harness.OPT_IN_ENV, raising=False)
    with pytest.raises(works_in_harness.HarnessError):
        works_in_harness.require_opt_in()
    from sessionfs.cli.main import app

    result = CliRunner().invoke(app, ["catalog", "harness", "run", "--tool", "claude-code"])
    assert result.exit_code == 2
    assert "SFS_WORKS_IN_HARNESS=1" in strip_ansi(result.output)


def test_harness_runs_installer_in_a_clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_must_not_leak")
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    bundle = _bundle(projection.files)
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy",
            "projection_hash": projection.plugins[0].projection_hash}
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv, env, cwd):
        calls.append((argv, env))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.1.240 (Claude Code)\n", "")
        assert (Path(argv[-1]) / ".claude-plugin/marketplace.json").exists() or "@" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    verdict = works_in_harness.run_tool("claude-code", bundle, meta, runner=runner, which=lambda _: "/bin/claude")
    assert verdict.status == "pass"
    assert verdict.tool_version == "2.1.240 (Claude Code)"
    assert [call[0][1:3] for call in calls[1:]] == [["plugin", "validate"], ["plugin", "marketplace"],
                                                    ["plugin", "install"]]
    assert calls[-1][0][-1] == "acme--deploy@sessionfs"
    for _, env in calls:
        assert "SESSIONFS_API_KEY" not in env and env["HOME"].endswith("home")
    assert "<sandbox>" in json.dumps(verdict.evidence)

    def failing(argv, env, cwd):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.2.0\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "invalid manifest")

    failed = works_in_harness.run_tool("claude-code", bundle, meta, runner=failing, which=lambda _: "/x")
    assert failed.status == "fail" and "exited 1" in failed.reason
    missing = works_in_harness.run_tool("claude-code", bundle, meta, runner=runner, which=lambda _: None)
    assert missing.status == "skipped"
    codex = works_in_harness.run_tool("codex", bundle, meta, runner=runner, which=lambda _: "/x")
    assert codex.status == "skipped"


def test_harness_rejects_hostile_bundles(tmp_path: Path) -> None:
    with pytest.raises(works_in_harness.HarnessError):
        works_in_harness.extract_bundle(_bundle({"a.txt": b"x"}, symlink=True), tmp_path / "a")
    with pytest.raises(works_in_harness.HarnessError):
        works_in_harness.extract_bundle(_bundle({".git/hooks/post-checkout": b"x"}), tmp_path / "b")
    with pytest.raises(works_in_harness.HarnessError):
        works_in_harness.extract_bundle(_bundle({"../escape": b"x"}), tmp_path / "c")


# ---------------------------------------------------------------------------
# Failure isolation and budgets (one package never breaks a catalog)
# ---------------------------------------------------------------------------


def _with_files(item: native_feeds.CatalogInput, files: dict[str, bytes]) -> native_feeds.CatalogInput:
    return native_feeds.CatalogInput(**{**item.__dict__, "files": {**item.files, **files}})


@pytest.mark.parametrize("scope", ["public", "org"])
@pytest.mark.parametrize(
    "path",
    ["gemini-extension.json/x", "plugin.json/x", ".claude-plugin", ".cursor-plugin", "Plugin.json",
     ".Claude-Plugin/plugin.json", "GEMINI-EXTENSION.JSON/a/b"],
)
def test_generated_manifest_name_conflict_excludes_only_that_plugin(scope: str, path: str) -> None:
    a, b = _inputs()
    projection = native_feeds.build_catalog(scope, None if scope == "public" else "org_x",
                                            [_with_files(a, {path: b"x"}), b])
    assert [plugin.plugin_id for plugin in projection.plugins] == ["pub--review-helper"]
    assert [entry["version_id"] for entry in projection.excluded] == [a.version_id]
    # The rest of the catalog still builds into a servable repository.
    build_repository(projection.files, timestamp=projection.timestamp, message="catalog")
    disclosed = json.loads(projection.files["SESSIONFS-CATALOG.json"])
    assert disclosed["excluded"][0]["plugin"] == "acme--deploy"


def test_oversized_plugin_is_excluded_not_fatal() -> None:
    a, b = _inputs()
    many = {f"skills/deploy/ref/{i}.md": b"x" for i in range(native_feeds.MAX_PLUGIN_FILES + 1)}
    projection = native_feeds.build_catalog("public", None, [_with_files(a, many), b])
    assert [plugin.plugin_id for plugin in projection.plugins] == ["pub--review-helper"]
    assert "more than" in projection.excluded[0]["reason"]


def test_catalog_over_the_git_file_budget_still_builds() -> None:
    """More files than one repository may hold: plugins are admitted in
    catalog order until the budget is used, the rest are disclosed."""
    a, _ = _inputs()
    inputs = []
    for index in range(5):
        files = {f"skills/s{index}/ref/{i}.md": b"r" for i in range(4_500)}
        inputs.append(native_feeds.CatalogInput(**{
            **a.__dict__, "version_id": f"skv_{index:024d}", "package_id": f"spk_{index:024d}",
            "name": f"pkg{index}", "files": {**a.files, **files},
        }))
    projection = native_feeds.build_catalog("org", "org_x", inputs)
    assert [plugin.plugin_id for plugin in projection.plugins] == [f"acme--pkg{i}" for i in range(4)]
    [budget] = projection.excluded
    assert budget["plugin"] == "acme--pkg4" and budget["reason"].startswith("catalog_budget")
    repo = build_repository(projection.files, timestamp=projection.timestamp, message="catalog")
    assert repo.commit_sha
    disclosed = json.loads(projection.files["SESSIONFS-CATALOG.json"])
    assert disclosed["budget"]["order"] == native_feeds.BUDGET_ORDER
    assert [entry["plugin"] for entry in disclosed["excluded"]] == ["acme--pkg4"]


def test_byte_budget_and_skipped_inputs_are_disclosed() -> None:
    a, b = _inputs()
    unavailable = native_feeds.CatalogInput(**{**a.__dict__, "files": {}, "skip_reason": "package archive is unavailable"})
    projection = native_feeds.build_catalog("public", None, [unavailable, b])
    assert projection.excluded == [{"version_id": a.version_id, "plugin": "acme--deploy",
                                    "reason": "package archive is unavailable"}]
    tight = native_feeds.build_catalog("public", None, [a, b], max_bytes=1)
    assert tight.plugins == [] and {entry["reason"][:14] for entry in tight.excluded} == {"catalog_budget"}
    build_repository(tight.files, timestamp=tight.timestamp, message="empty")


# ---------------------------------------------------------------------------
# Credential helper: our helper only, never stored by another helper
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_install_helper_resets_other_helpers_for_the_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli import cmd_catalog

    home = tmp_path / "home"
    home.mkdir()
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    helper = f"!{sys.executable} -m sessionfs.cli.main catalog credential"
    monkeypatch.setattr(cmd_catalog, "helper_command", lambda: helper)
    # A user-wide helper that would persist any credential it is offered.
    subprocess.run(["git", "config", "--global", "credential.helper", "store"], check=True)

    projection = native_feeds.build_catalog("public", None, _inputs())
    repo = build_repository(projection.files, timestamp=projection.timestamp, message="catalog")
    key = "sk_sfs_" + "d" * 32
    with _Server(repo.served_files(), key) as server:
        api_url = f"http://127.0.0.1:{server.port}"
        monkeypatch.setenv("SESSIONFS_API_URL", api_url)
        monkeypatch.setenv("SESSIONFS_API_KEY", key)
        cmd_catalog._install_helper(api_url)
        cmd_catalog._install_helper(api_url)  # idempotent
        config_key = cmd_catalog.credential_config_key(api_url)
        values = subprocess.run(["git", "config", "--global", "--get-all", config_key],
                                capture_output=True, text=True, check=True).stdout.split("\n")[:-1]
        assert values == ["", helper]
        clone = subprocess.run(["git", "clone", "-q", f"{api_url}/cat.git", str(tmp_path / "c")],
                               capture_output=True, text=True, check=False)
        assert clone.returncode == 0, clone.stderr
    # git asked helpers to `store` the working credential: the reset kept the
    # user-wide store helper out of it, and ours ignores `store`.
    assert not (home / ".git-credentials").exists()
    assert key not in "".join(p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())


def test_credential_helper_ignores_store_and_erase(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli.main import app

    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_cli")
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test")
    request = "protocol=https\nhost=api.example.test\nusername=sessionfs\npassword=sk_sfs_cli\n\n"
    for operation in ("store", "erase"):
        result = CliRunner().invoke(app, ["catalog", "credential", operation], input=request)
        assert result.exit_code == 0 and result.stdout == ""


# ---------------------------------------------------------------------------
# Harness: credential from env only, scrubbed; separate uid; per-job errors
# ---------------------------------------------------------------------------


def test_harness_cli_never_reads_the_profile_and_scrubs_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli.main import app

    monkeypatch.setenv(works_in_harness.OPT_IN_ENV, "1")
    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_profile_key")
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test")
    monkeypatch.delenv(works_in_harness.API_KEY_ENV, raising=False)
    monkeypatch.delenv(works_in_harness.API_URL_ENV, raising=False)
    refused = CliRunner().invoke(app, ["catalog", "harness", "run", "--tool", "claude-code"])
    assert refused.exit_code == 2
    assert works_in_harness.API_KEY_ENV in strip_ansi(refused.output)
    assert "SESSIONFS_API_KEY" not in os.environ  # scrubbed even when refusing

    seen: dict = {}

    def fake_run_once(client, tool, **kwargs):
        seen["key"] = client.api_key
        seen["url"] = client.api_url
        seen["env"] = dict(os.environ)
        seen["identity"] = kwargs.get("identity")
        return []

    monkeypatch.setattr(works_in_harness, "run_once", fake_run_once)
    monkeypatch.setenv(works_in_harness.API_URL_ENV, "https://harness.example.test")
    monkeypatch.setenv(works_in_harness.API_KEY_ENV, "sk_sfs_harness_key")
    monkeypatch.setenv(works_in_harness.SAME_UID_OVERRIDE_ENV, "1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    ok = CliRunner().invoke(app, ["catalog", "harness", "run", "--tool", "claude-code"])
    assert ok.exit_code == 0, ok.output
    assert seen["key"] == "sk_sfs_harness_key" and seen["url"] == "https://harness.example.test"
    assert works_in_harness.API_KEY_ENV not in seen["env"] and "SESSIONFS_API_KEY" not in seen["env"]
    assert seen["identity"] is None


def test_harness_refuses_same_uid_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.delenv(works_in_harness.SAME_UID_OVERRIDE_ENV, raising=False)
    with pytest.raises(works_in_harness.HarnessError, match="separate uid"):
        works_in_harness.resolve_tool_identity()
    from sessionfs.cli.main import app

    monkeypatch.setenv(works_in_harness.OPT_IN_ENV, "1")
    monkeypatch.setenv(works_in_harness.API_URL_ENV, "https://harness.example.test")
    monkeypatch.setenv(works_in_harness.API_KEY_ENV, "sk_sfs_harness_key")
    result = CliRunner().invoke(app, ["catalog", "harness", "run", "--tool", "claude-code"])
    assert result.exit_code == 2
    assert works_in_harness.SAME_UID_OVERRIDE_ENV in strip_ansi(result.output)


@pytest.mark.skipif(os.name != "posix", reason="uid separation is POSIX-only")
def test_harness_as_root_runs_tools_as_the_tool_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pwd

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv(works_in_harness.TOOL_USER_ENV, "sfs-tool")

    class Entry:
        pw_uid = 4242
        pw_gid = 4343

    def getpwnam(name):
        if name != "sfs-tool":
            raise KeyError(name)
        return Entry()

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    identity = works_in_harness.resolve_tool_identity()
    assert identity == works_in_harness.ToolIdentity(4242, 4343)
    monkeypatch.setenv(works_in_harness.TOOL_USER_ENV, "missing")
    with pytest.raises(works_in_harness.HarnessError, match="does not exist"):
        works_in_harness.resolve_tool_identity()

    calls: list[dict] = []

    class FakePopen:
        pid = 999_999_999
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append(kwargs)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(works_in_harness, "_kill_group", lambda pid: None)
    works_in_harness.make_runner(identity)(["true"], {}, tmp_path)
    assert calls[0]["user"] == 4242 and calls[0]["group"] == 4343 and calls[0]["extra_groups"] == []
    assert calls[0]["start_new_session"] is True
    swept: list[dict] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: swept.append({"argv": argv, **kwargs})
                        or subprocess.CompletedProcess(argv, 0, "", ""))

    chowned: list[tuple] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, **kw: chowned.append((Path(path), uid, gid)))
    proc = tmp_path / "proc"
    _fake_proc(proc, {})
    monkeypatch.setattr(works_in_harness, "PROC_ROOT", proc)
    monkeypatch.setattr(works_in_harness, "_reap_children", lambda: None)
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy"}
    works_in_harness.run_tool(
        "claude-code", _bundle(projection.files), meta, identity=identity, which=lambda _: "/x",
        runner=lambda argv, env, cwd: subprocess.CompletedProcess(argv, 0, "1.0\n", ""),
    )
    owned = {path.name for path, uid, gid in chowned if (uid, gid) == (4242, 4343)}
    assert {"home", "catalog", "tmp", "marketplace.json"} <= owned
    # After the job, every process of the tool uid is killed from a child
    # running AS that uid (it cannot signal the root harness).
    [sweep] = [call for call in swept if "os.kill(-1" in " ".join(call["argv"])]
    assert (sweep["user"], sweep["group"], sweep["extra_groups"]) == (4242, 4343, [])


def test_harness_batch_survives_per_job_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(works_in_harness.OPT_IN_ENV, "1")
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    bundle = _bundle(projection.files)
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy",
            "projection_hash": projection.plugins[0].projection_hash}

    class Client:
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.reports: list[str] = []

        def claim(self, tool, limit, digest):
            return [{"job_id": f"svj_{i}", "version_id": f"skv_{i}", "claim_token": "t" * 12,
                     "projection_path": "/api/v1/admin/skills/verify/jobs/x/projection"} for i in range(4)]

        def projection(self, job):
            if job["job_id"] == "svj_0":
                return None  # server skipped: no longer deliverable
            if job["job_id"] == "svj_1":
                raise RuntimeError("503 Package blob unavailable")
            return bundle, meta

        def report_error(self, job, reason):
            self.errors.append(job["job_id"])
            return {}

        def report(self, job, verdict, **kwargs):
            if job["job_id"] == "svj_2":
                raise RuntimeError("connection reset")
            self.reports.append(job["job_id"])
            return {"verification_id": "stv_x", "status": verdict.status}

    client = Client()
    runner = lambda argv, env, cwd: subprocess.CompletedProcess(argv, 0, "1.0\n", "")  # noqa: E731
    results = works_in_harness.run_once(client, "claude-code", runner=runner, which=lambda _: "/x")  # type: ignore[arg-type]
    assert [result["status"] for result in results] == ["skipped", "error", "error", "pass"]
    assert client.errors == ["svj_1"] and client.reports == ["svj_3"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_helper_order_check_flags_helpers_after_the_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli import cmd_catalog
    from sessionfs.cli.main import app

    home = tmp_path / "home"
    home.mkdir()
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    helper = f"!{sys.executable} -m sessionfs.cli.main catalog credential"
    monkeypatch.setattr(cmd_catalog, "helper_command", lambda: helper)
    api_url = "https://api.example.test"
    monkeypatch.setenv("SESSIONFS_API_URL", api_url)
    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_doctor")
    key = cmd_catalog.credential_config_key(api_url)

    assert "No credential-helper reset" in (cmd_catalog.helper_order_problem(api_url) or "")
    cmd_catalog._install_helper(api_url)
    assert cmd_catalog.helper_order_problem(api_url) is None
    assert CliRunner().invoke(app, ["catalog", "doctor"]).exit_code == 0

    # A generic helper added to the global file AFTER our section is consulted.
    subprocess.run(["git", "config", "--global", "--add", "credential.helper", "store"], check=True)
    problem = cmd_catalog.helper_order_problem(api_url)
    assert problem and "credential.helper = 'store'" in problem and "Fix:" in problem
    doctor = CliRunner().invoke(app, ["catalog", "doctor"])
    assert doctor.exit_code == 1 and "credential.helper" in strip_ansi(doctor.output)
    # Re-installing moves our section to the end of the file.
    cmd_catalog._install_helper(api_url)
    assert cmd_catalog.helper_order_problem(api_url) is None
    values = subprocess.run(["git", "config", "--global", "--get-all", key],
                            capture_output=True, text=True, check=True).stdout.split("\n")[:-1]
    assert values == ["", helper]

    # A repository's own helper comes after the global reset: named with the
    # exact per-file fix.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "credential.helper", "cache"], check=True)
    monkeypatch.chdir(repo)
    problem = cmd_catalog.helper_order_problem(api_url)
    assert problem and "git config --file" in problem and str((repo / ".git" / "config").resolve()) in problem


def _alive(pid: int, *, wait: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        time.sleep(0.05)
    return True


_SPAWN = (
    "import subprocess, sys, time\n"
    "kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])]\n"
    "kids.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True))\n"
    "print(kids[0].pid, kids[1].pid, flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
@pytest.mark.parametrize("linger", [0.0, 300.0], ids=["step-returns", "step-times-out"])
def test_runner_kills_the_steps_whole_process_group(tmp_path: Path, linger: float) -> None:
    """A step that leaves a background child behind (or hangs) has its whole
    group killed. A child that escaped with setsid survives the group kill,
    which is what the per-job uid sweep is for."""
    script = tmp_path / "spawn.py"
    script.write_text(_SPAWN)
    out = tmp_path / "pids"
    runner = works_in_harness.make_runner(None, timeout=2)
    argv = ["/bin/sh", "-c", f'"{sys.executable}" "{script}" {linger} > "{out}"']
    if linger:
        with pytest.raises(subprocess.TimeoutExpired):
            runner(argv, dict(os.environ), tmp_path)
    else:
        assert runner(argv, dict(os.environ), tmp_path).returncode == 0
    grouped, escaped = (int(value) for value in out.read_text().split())
    try:
        assert not _alive(grouped)
        assert _alive(escaped, wait=0.2)
    finally:
        try:
            os.kill(escaped, 9)
        except ProcessLookupError:
            pass


def test_every_job_is_followed_by_the_uid_sweep() -> None:
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy"}
    identity = works_in_harness.ToolIdentity(4242, 4343)
    reaped: list[tuple] = []
    seen_dirs: list[Path] = []

    def reaper(who):
        reaped.append((who, [path.exists() for path in seen_dirs]))
        return True

    def runner(argv, env, cwd):
        seen_dirs.append(Path(cwd))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "1.0\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    for _ in range(2):
        works_in_harness.run_tool("claude-code", _bundle(projection.files), meta, runner=runner,
                                  which=lambda _: "/x", reaper=reaper)
    # The sweep ran after each job, while that job's directory still existed
    # (so nothing can recreate it), and the directory is gone afterwards.
    assert len(reaped) == 2 and all(states[-1] for _, states in reaped)
    assert len(set(seen_dirs)) == 2 and not any(path.exists() for path in seen_dirs)
    works_in_harness.run_tool("claude-code", _bundle(projection.files), meta, runner=runner,
                              which=lambda _: "/x", reaper=reaper, identity=None)
    assert reaped[-1][0] is None
    assert identity  # kill_tool_processes itself is covered by the root test


def test_reaper_treats_nothing_to_signal_as_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # kill(-1) as the tool uid raises ESRCH when the job left no process
    # behind (the normal case); that must not read as a failed sweep.
    calls: list[tuple[int, int]] = []

    def no_such_process(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", no_such_process)
    exec(compile(works_in_harness._REAPER_CODE, "<reaper>", "exec"), {})  # noqa: S102 - fixed constant
    assert calls == [(-1, signal.SIGKILL)]

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied)
    with pytest.raises(PermissionError):  # any other error still fails the sweep
        exec(compile(works_in_harness._REAPER_CODE, "<reaper>", "exec"), {})  # noqa: S102


def test_uid_sweep_is_a_no_op_without_a_separate_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    works_in_harness.kill_tool_processes(None)  # kill(-1) as ourselves would hit the harness
    assert called == []


def test_one_plugin_cannot_monopolise_the_budget_and_rejected_bytes_are_free() -> None:
    a, b = _inputs()
    big = _with_files(a, {"skills/deploy/blob.bin": b"x" * 5_000})
    small_size = native_feeds.admission_size(b, public=True)
    # The big plugin exceeds the per-plugin cap: excluded, and the smaller one
    # still fits a budget that only it can satisfy (its bytes are not charged).
    projection = native_feeds.build_catalog("public", None, [big, b], max_bytes=small_size,
                                            max_plugin_bytes=4_000)
    assert [plugin.plugin_id for plugin in projection.plugins] == ["pub--review-helper"]
    assert projection.excluded[0]["reason"].startswith("plugin_too_large")
    disclosed = json.loads(projection.files["SESSIONFS-CATALOG.json"])
    assert disclosed["budget"]["max_plugin_bytes"] == 4_000
    # A plugin excluded by preflight is not charged either.
    bad = _with_files(a, {"gemini-extension.json/x": b"x" * 5_000})
    projection = native_feeds.build_catalog("public", None, [bad, b], max_bytes=small_size)
    assert [plugin.plugin_id for plugin in projection.plugins] == ["pub--review-helper"]
    assert native_feeds.DEFAULT_MAX_ADMITTED_PLUGIN_BYTES < native_feeds.DEFAULT_MAX_CATALOG_BYTES // 8



def _fake_proc(root: Path, processes: dict[int, tuple[int, str]]) -> None:
    (root / "self").mkdir(parents=True, exist_ok=True)
    (root / "self" / "status").write_text("Name:\tpython\nState:\tR (running)\nUid:\t0\t0\t0\t0\n")
    for pid, (uid, state) in processes.items():
        (root / str(pid)).mkdir(exist_ok=True)
        (root / str(pid) / "status").write_text(
            f"Name:\tx\nState:\t{state} (x)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )


def test_tool_pid_scan_reads_proc_and_ignores_zombies(tmp_path: Path) -> None:
    _fake_proc(tmp_path, {10: (4242, "S"), 11: (4242, "Z"), 12: (0, "R"), 13: (4242, "R")})
    assert works_in_harness.tool_pids(4242, tmp_path) == [10, 13]
    assert works_in_harness.tool_pids(4242, tmp_path / "missing") is None  # no /proc: unverifiable


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only sweep")
def test_uid_sweep_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = works_in_harness.ToolIdentity(4242, 4343)
    proc = tmp_path / "proc"
    monkeypatch.setattr(works_in_harness, "PROC_ROOT", proc)
    monkeypatch.setattr(works_in_harness, "_reap_children", lambda: None)
    monkeypatch.setattr(works_in_harness, "_remove_tool_temp", lambda identity: None)
    monkeypatch.setattr("time.sleep", lambda _: None)
    reaper_rc = {"value": 0}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, reaper_rc["value"]))
    killed: list[int] = []

    def kill_nothing(pid, sig):
        killed.append(pid)

    monkeypatch.setattr(os, "kill", kill_nothing)

    # A process of the tool uid that survives every kill from root: fail.
    _fake_proc(proc, {77: (4242, "R")})
    assert works_in_harness.kill_tool_processes(identity) is False
    assert killed and set(killed) == {77}

    # Root's kill works: clean.
    def kill_and_vanish(pid, sig):
        shutil.rmtree(proc / str(pid))

    monkeypatch.setattr(os, "kill", kill_and_vanish)
    assert works_in_harness.kill_tool_processes(identity) is True

    # The reaper child itself was killed (a hostile setsid loop): fail even
    # though nothing remains.
    reaper_rc["value"] = -9
    assert works_in_harness.kill_tool_processes(identity) is False

    # No /proc to verify with: fail closed.
    reaper_rc["value"] = 0
    shutil.rmtree(proc)
    assert works_in_harness.kill_tool_processes(identity) is False
    # Development mode (no separate uid) has nothing to sweep.
    assert works_in_harness.kill_tool_processes(None) is True


def test_failed_sweep_stops_the_harness_after_one_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(works_in_harness.OPT_IN_ENV, "1")
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    bundle = _bundle(projection.files)
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy",
            "projection_hash": projection.plugins[0].projection_hash}

    class Client:
        def __init__(self) -> None:
            self.limits: list[int] = []
            self.errors: list[tuple[str, bool]] = []
            self.reports: list[str] = []

        def claim(self, tool, limit, digest):
            self.limits.append(limit)
            return [{"job_id": f"svj_{i}", "version_id": f"skv_{i}", "claim_token": "t" * 12,
                     "projection_path": "/api/v1/admin/skills/verify/jobs/x/projection"} for i in range(limit)]

        def projection(self, job):
            return bundle, meta

        def report_error(self, job, reason, *, final=False):
            self.errors.append((job["job_id"], final))
            return {}

        def report(self, job, verdict, **kwargs):
            self.reports.append(job["job_id"])
            return {"status": verdict.status}

    client = Client()
    runner = lambda argv, env, cwd: subprocess.CompletedProcess(argv, 0, "1.0\n", "")  # noqa: E731
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    with pytest.raises(works_in_harness.SandboxBreach) as caught:
        works_in_harness.run_once(client, "claude-code", limit=5, runner=runner, which=lambda _: "/x",  # type: ignore[arg-type]
                                  identity=works_in_harness.ToolIdentity(4242, 4343),
                                  reaper=lambda identity: False)
    assert client.limits == [1]  # one job per execution with a separate uid
    assert client.errors == [("svj_0", True)] and client.reports == []
    assert caught.value.results[0]["status"] == "error"

    from sessionfs.cli.main import app

    monkeypatch.setenv(works_in_harness.API_URL_ENV, "https://harness.example.test")
    monkeypatch.setenv(works_in_harness.API_KEY_ENV, "sk_sfs_harness_key")
    monkeypatch.setattr(works_in_harness, "resolve_tool_identity", lambda: works_in_harness.ToolIdentity(1, 1))

    def breach(*args, **kwargs):
        raise works_in_harness.SandboxBreach("tool processes could not be proven gone")

    monkeypatch.setattr(works_in_harness, "run_once", breach)
    result = CliRunner().invoke(app, ["catalog", "harness", "run", "--tool", "claude-code"])
    assert result.exit_code == 3



def test_sweep_is_checked_even_when_the_job_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    projection = native_feeds.build_catalog("public", None, _inputs()[:1])
    meta = {"marketplace": "sessionfs", "plugin": "acme--deploy", "plugin_path": "plugins/acme--deploy"}

    def exploding(argv, env, cwd):
        raise RuntimeError("tool crashed the step runner")

    with pytest.raises(works_in_harness.SandboxBreach):
        works_in_harness.run_tool("claude-code", _bundle(projection.files), meta, runner=exploding,
                                  which=lambda _: "/x", reaper=lambda identity: False)
    with pytest.raises(RuntimeError):
        works_in_harness.run_tool("claude-code", _bundle(projection.files), meta, runner=exploding,
                                  which=lambda _: "/x", reaper=lambda identity: True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize(
    ("variant", "flagged"),
    [
        ("https://*.example.test", True),            # wildcard subdomain
        ("https://API.Example.Test", True),           # host case
        ("https://api.example.test/api/v1", True),    # path prefix of the catalog URL
        ("https://user@api.example.test", False),     # git requests no username
        ("https://api.example.test/elsewhere", False),
        ("https://api.example.test:8443", False),
        ("api.example.test", True),                   # partial URL: bare host
        ("https://", True),                           # partial URL: scheme only
    ],
)
def test_helper_check_uses_gits_url_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str, flagged: bool
) -> None:
    from sessionfs.cli import cmd_catalog

    home = tmp_path / "home"
    home.mkdir()
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    helper = f"!{sys.executable} -m sessionfs.cli.main catalog credential"
    monkeypatch.setattr(cmd_catalog, "helper_command", lambda: helper)
    api_url = "https://api.example.test"
    cmd_catalog._install_helper(api_url)
    assert cmd_catalog.helper_order_problem(api_url) is None
    # A helper for a URL variant, written AFTER the SessionFS reset.
    subprocess.run(["git", "config", "--global", "--add", f"credential.{variant}.helper", "store"], check=True)
    problem = cmd_catalog.helper_order_problem(api_url)
    assert (problem is not None) is flagged, problem
    if flagged:
        assert problem is not None and "store" in problem
        # The printed fix works as given.
        subprocess.run(["/bin/sh", "-c", problem.split("Fix: ", 1)[1]], check=True, capture_output=True)
        assert cmd_catalog.helper_order_problem(api_url) is None



def test_zombie_leader_with_live_threads_counts_as_live(tmp_path: Path) -> None:
    _fake_proc(tmp_path, {21: (4242, "Z"), 22: (4242, "Z")})
    for pid in (21, 22):
        status = tmp_path / str(pid) / "status"
        # Linux keeps the leader's own count until it is reaped.
        status.write_text(status.read_text() + "Threads:\t1\n")
        (tmp_path / str(pid) / "task" / str(pid)).mkdir(parents=True)
        (tmp_path / str(pid) / "task" / str(pid) / "status").write_text("State:\tZ (zombie)\n")
    # 21: a hostile leader exited, but one of its threads still runs.
    (tmp_path / "21" / "task" / "99").mkdir(parents=True)
    (tmp_path / "21" / "task" / "99" / "status").write_text("State:\tS (sleeping)\n")
    # 22: a plain zombie (Threads: 1, no live task) is ignored: root cannot
    # kill it, and it cannot act.
    assert works_in_harness.tool_pids(4242, tmp_path) == [21]
