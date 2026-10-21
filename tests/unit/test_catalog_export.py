"""`sfs catalog export` for unattended mirroring (marketplace v2 D4b) and
the public GitHub mirror configuration.

No network: the SessionFS side is a local dumb-HTTP server, the destination
is a local bare repository, and SSH is a fake `ssh` on PATH that records
how git invoked it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from sessionfs.cli import cmd_catalog
from sessionfs.cli.main import app
from sessionfs.server.config import ServerConfig
from sessionfs.server.routes.native_catalogs import _add_commands
from sessionfs.skills import catalog_mirror, native_feeds
from sessionfs.skills.git_catalog import build_repository
from tests.unit.test_native_feeds import _inputs
from tests.utils.ansi import strip_ansi

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

KEY = "sk_sfs_" + "e" * 32
DEPLOY_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZS1kZXBsb3kta2V5LW1hdGVyaWFs\n-----END OPENSSH PRIVATE KEY-----\n"


class _BearerServer:
    """Dumb-HTTP catalog that, like the API, accepts the Bearer key."""

    def __init__(self, files: dict[str, bytes], key: str) -> None:
        outer = self
        self.files = files
        self.key = key

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {outer.key}":
                    self.send_response(401)
                    self.end_headers()
                    return
                data = outer.files.get(self.path.split("?", 1)[0].removeprefix("/cat.git/"))
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_BearerServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    projection = native_feeds.build_catalog("public", None, _inputs())
    repo = build_repository(projection.files, timestamp=projection.timestamp, message="catalog")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("SESSIONFS_API_KEY", KEY)
    # Tracing would print the Authorization header: export must drop it.
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    with _BearerServer(repo.served_files(), KEY) as server:
        monkeypatch.setenv("SESSIONFS_API_URL", server.url)
        body = {"items": [{"scope": "public", "scope_id": None, "marketplace": "sessionfs",
                           "clone_url": f"{server.url}/cat.git", "tools": {}}]}
        monkeypatch.setattr(cmd_catalog, "_request", lambda method, path, payload=None: body)
        yield repo


def _bare(path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True)
    return path


def _branch_sha(repo: Path, branch: str = "main") -> str | None:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def _export(*args: str):
    return CliRunner().invoke(app, ["catalog", "export", *args])


def _assert_no_secrets(output: str) -> None:
    assert KEY not in output
    assert "PRIVATE KEY" not in output
    assert "ZmFrZS1kZXBsb3kta2V5" not in output


def test_export_pushes_then_skips_when_up_to_date(catalog, tmp_path: Path) -> None:
    dest = _bare(tmp_path / "dest.git")
    first = _export("--to", str(dest))
    output = strip_ansi(first.output)
    assert first.exit_code == 0, output
    assert f"Pushed {catalog.commit_sha}" in output
    assert _branch_sha(dest) == catalog.commit_sha
    _assert_no_secrets(output)

    again = _export("--to", f"file://{dest}")
    output = strip_ansi(again.output)
    assert again.exit_code == 0, output
    assert "Already up to date" in output and "Pushed" not in output
    _assert_no_secrets(output)


def test_export_overwrites_unrelated_history_on_the_branch(catalog, tmp_path: Path) -> None:
    dest = _bare(tmp_path / "dest.git")
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    (work / "README.md").write_text("placeholder\n")
    for args in (["add", "README.md"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "--quiet", "-m", "init"],
                 ["push", "--quiet", str(dest), "HEAD:refs/heads/main"]):
        subprocess.run(["git", "-C", str(work), *args], check=True)
    assert _branch_sha(dest) not in (None, catalog.commit_sha)
    result = _export("--to", str(dest))
    assert result.exit_code == 0, strip_ansi(result.output)
    assert _branch_sha(dest) == catalog.commit_sha


def test_export_dry_run_does_not_touch_the_destination(catalog, tmp_path: Path) -> None:
    dest = _bare(tmp_path / "dest.git")
    result = _export("--to", str(dest), "--dry-run")
    assert result.exit_code == 0, strip_ansi(result.output)
    assert "Would push" in strip_ansi(result.output)
    assert _branch_sha(dest) is None


def test_export_failures_exit_non_zero_without_leaking(catalog, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _export("--to", str(tmp_path / "does-not-exist.git"))
    assert missing.exit_code == 1
    assert "Could not read the destination" in strip_ansi(missing.output)
    _assert_no_secrets(strip_ansi(missing.output))

    # The destination rejects the push (a pre-receive hook).
    dest = _bare(tmp_path / "locked.git")
    hook = dest / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    hook.chmod(0o755)
    refused = _export("--to", str(dest))
    assert refused.exit_code == 1
    assert "Push failed" in strip_ansi(refused.output)
    _assert_no_secrets(strip_ansi(refused.output))

    # Wrong SessionFS key: the clone is refused, the key is never printed.
    monkeypatch.setenv("SESSIONFS_API_KEY", "sk_sfs_" + "f" * 32)
    denied = _export("--to", str(_bare(tmp_path / "other.git")))
    assert denied.exit_code == 1
    assert "Clone failed" in strip_ansi(denied.output)
    assert "sk_sfs_" not in strip_ansi(denied.output)


def test_export_requires_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SESSIONFS_API_KEY", raising=False)
    monkeypatch.setenv("SESSIONFS_PROFILE", "nobody")
    result = _export("--to", str(tmp_path / "x.git"))
    assert result.exit_code == 1
    assert "Not authenticated" in strip_ansi(result.output)


@pytest.mark.parametrize(
    "remote",
    [
        "https://x-access-token:ghp_secretvalue@github.com/SessionFS/marketplace.git",
        "https://ghp_secretvalue@github.com/SessionFS/marketplace.git",
        "ssh://git:ghp_secretvalue@github.com/SessionFS/marketplace.git",
    ],
)
def test_remotes_with_credentials_are_refused_without_echo(remote: str) -> None:
    result = _export("--to", remote)
    assert result.exit_code == 2
    output = strip_ansi(result.output)
    assert "credentials in the remote URL" in output
    assert "ghp_secretvalue" not in output


@pytest.mark.parametrize("remote", ["ext::sh -c touch% /tmp/pwned", "fd::3", "-upload-pack=x", "http://example.test/x.git",
                                    "git://example.test/x.git"])
def test_unusual_transports_are_refused(remote: str) -> None:
    assert _export("--to", remote).exit_code == 2


def test_ssh_options_require_an_ssh_remote(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text(DEPLOY_KEY)
    https = _export("--to", "https://github.com/SessionFS/marketplace.git", "--ssh-key-file", str(key))
    assert https.exit_code == 2
    assert "needs an SSH remote" in strip_ansi(https.output)
    hosts_only = _export("--to", "git@github.com:SessionFS/marketplace.git", "--known-hosts", str(key))
    assert hosts_only.exit_code == 2


def _fake_ssh(bin_dir: Path, log: Path) -> None:
    """An `ssh` that records argv, the key file's mode and both files'
    contents' hashes, then fails like an unreachable host."""
    bin_dir.mkdir()
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "key = argv[argv.index('-i') + 1]\n"
        "opts = [argv[i + 1] for i, a in enumerate(argv) if a == '-o']\n"
        "hosts = [o.split('=', 1)[1] for o in opts if o.startswith('UserKnownHostsFile=')][0]\n"
        f"json.dump({{'argv': argv, 'key_mode': os.stat(key).st_mode & 0o777,"
        "'key_sha': hashlib.sha256(open(key, 'rb').read()).hexdigest(),"
        "'hosts': open(hosts).read(), 'agent': os.environ.get('SSH_AUTH_SOCK')}, "
        f"open({str(log)!r}, 'w'))\n"
        "sys.stderr.write('ssh: connect to host github.com port 22: Connection refused\\n')\n"
        "sys.exit(255)\n"
    )
    script.chmod(0o755)


def test_ssh_deploy_key_is_used_alone_with_pinned_host_keys(catalog, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    log = tmp_path / "ssh.json"
    _fake_ssh(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=no")
    key = tmp_path / "deploy_key"
    key.write_text(DEPLOY_KEY)
    key.chmod(0o444)  # like a mounted secret: ssh would refuse it as-is
    monkeypatch.setenv("SFS_CATALOG_EXPORT_SSH_KEY_FILE", str(key))
    result = _export("--to", "git@github.com:SessionFS/marketplace.git")
    output = strip_ansi(result.output)
    assert result.exit_code == 1, output
    assert "Could not read the destination" in output
    _assert_no_secrets(output)
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    for option in ("IdentitiesOnly=yes", "IdentityAgent=none", "StrictHostKeyChecking=yes",
                   "GlobalKnownHostsFile=/dev/null", "BatchMode=yes"):
        assert option in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert argv[argv.index("-F") + 1] == "/dev/null"
    assert seen["key_mode"] == 0o600
    assert seen["key_sha"] == hashlib.sha256(DEPLOY_KEY.encode()).hexdigest()
    assert seen["hosts"] == catalog_mirror.GITHUB_KNOWN_HOSTS.read_text()
    assert seen["agent"] is None
    assert not any("ZmFrZS1kZXBsb3kta2V5" in part for part in argv)
    # The private copies are gone with the temporary directory.
    assert not Path(argv[argv.index("-i") + 1]).exists()

    custom = tmp_path / "known_hosts"
    custom.write_text("git.example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n")
    result = _export("--to", "ssh://git@git.example.test/org/catalog.git", "--known-hosts", str(custom))
    assert result.exit_code == 1
    assert json.loads(log.read_text())["hosts"] == custom.read_text()


SAFE_PATH = cmd_catalog._SSH_SAFE_PATH


def test_ssh_sees_only_plain_paths_whatever_tmpdir_and_known_hosts_are(catalog, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "ssh.json"
    _fake_ssh(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    odd = tmp_path / "odd dir %h ~"
    odd.mkdir()
    monkeypatch.setattr(cmd_catalog.tempfile, "gettempdir", lambda: str(odd))
    key = odd / "deploy key %d"
    key.write_text(DEPLOY_KEY)
    hosts = odd / "known hosts ~%u"
    hosts.write_text("git.example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n")
    result = _export("--to", "ssh://git@git.example.test/org/catalog.git", "--ssh-key-file", str(key),
                     "--known-hosts", str(hosts))
    assert result.exit_code == 1, strip_ansi(result.output)
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    key_path = argv[argv.index("-i") + 1]
    hosts_path = next(o.split("=", 1)[1] for o in argv if o.startswith("UserKnownHostsFile="))
    assert SAFE_PATH.match(key_path) and SAFE_PATH.match(hosts_path)
    assert not key_path.startswith(str(odd))
    assert seen["hosts"] == hosts.read_text()


def test_no_plain_temporary_base_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_catalog.tempfile, "gettempdir", lambda: "/tmp/with space")
    monkeypatch.setattr(cmd_catalog.os.path, "isdir", lambda path: False)
    with pytest.raises(cmd_catalog.typer.BadParameter):
        cmd_catalog._ssh_temp_base()


def test_push_env_keeps_the_callers_git_setup_without_a_deploy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("GIT_SSH_COMMAND", "ssh -i ~/.ssh/work"), ("GIT_ASKPASS", "/usr/bin/askpass"),
                        ("GIT_CONFIG_GLOBAL", "/home/u/.gitconfig-work"), ("SSH_AUTH_SOCK", "/tmp/agent"),
                        ("GIT_TRACE", "1"), ("GIT_TRACE_CURL", "1"), ("GIT_CURL_VERBOSE", "1"),
                        ("GIT_REDACT_COOKIES", "x")):
        monkeypatch.setenv(name, value)
    plain = cmd_catalog._push_env(None, None)
    assert plain["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/work"
    assert plain["GIT_ASKPASS"] == "/usr/bin/askpass"
    assert plain["GIT_CONFIG_GLOBAL"] == "/home/u/.gitconfig-work"
    assert plain["SSH_AUTH_SOCK"] == "/tmp/agent"
    assert not any(k.startswith(("GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_REDACT")) for k in plain)
    keyed = cmd_catalog._push_env("/tmp/x/deploy_key", "/tmp/x/known_hosts")
    assert "GIT_ASKPASS" not in keyed and "GIT_CONFIG_GLOBAL" not in keyed and "SSH_AUTH_SOCK" not in keyed
    assert keyed["GIT_SSH_COMMAND"].startswith("ssh -F /dev/null -i /tmp/x/deploy_key ")
    clone = cmd_catalog._git_auth_env("https://api.example.test", KEY)
    assert "GIT_ASKPASS" not in clone and "GIT_SSH_COMMAND" not in clone and "GIT_CONFIG_GLOBAL" not in clone
    assert clone["GIT_CONFIG_COUNT"] == "1"
    with pytest.raises(cmd_catalog.typer.BadParameter):
        cmd_catalog._push_env("/tmp/a b/key", "/tmp/x/known_hosts")


def test_without_a_deploy_key_the_callers_ssh_command_is_used(catalog, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "called"
    fake = tmp_path / "my-ssh"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\nexit 255\n")
    fake.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(fake))
    result = _export("--to", "git@example.test:org/catalog.git")
    assert result.exit_code == 1
    assert "example.test" in marker.read_text()


def test_relative_destination_is_resolved_and_bare_names_refused(catalog, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _bare(tmp_path / "dest.git")
    monkeypatch.chdir(tmp_path)
    first = _export("--to", "./dest.git")
    assert first.exit_code == 0, strip_ansi(first.output)
    assert _branch_sha(tmp_path / "dest.git") == catalog.commit_sha
    again = _export("--to", "dest.git/")
    assert "Already up to date" in strip_ansi(again.output)
    for name in ("origin", "dest.git"):
        refused = _export("--to", name)
        assert refused.exit_code == 2
        assert "bare remote name" in strip_ansi(refused.output)


def test_git_timeouts_exit_non_zero(catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(cmd_catalog.subprocess, "run", slow)
    result = _export("--to", str(tmp_path / "dest.git"))
    assert result.exit_code == 1
    assert "timed out" in strip_ansi(result.output)
    assert cmd_catalog._CLONE_TIMEOUT + cmd_catalog._LS_REMOTE_TIMEOUT + cmd_catalog._PUSH_TIMEOUT + 60 <= 15 * 60


def test_hosted_without_run_is_refused() -> None:
    result = CliRunner().invoke(app, ["catalog", "add", "--for", "claude-code", "--hosted"])
    assert result.exit_code == 2


def test_unreadable_key_file_is_reported_without_contents(catalog, tmp_path: Path) -> None:
    result = _export("--to", "git@github.com:SessionFS/marketplace.git", "--ssh-key-file",
                     str(tmp_path / "missing"))
    assert result.exit_code == 2
    assert "cannot read deploy_key" in strip_ansi(result.output)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")
def test_bundled_github_host_keys_match_published_fingerprints() -> None:
    listed = subprocess.run(["ssh-keygen", "-lf", str(catalog_mirror.GITHUB_KNOWN_HOSTS)],
                            capture_output=True, text=True, check=True).stdout
    # https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
    for fingerprint in ("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
                        "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
                        "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s"):
        assert fingerprint in listed
    assert all(line.split()[2] == "github.com" for line in listed.splitlines())


def test_bundled_known_hosts_ships_in_the_package() -> None:
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert '"src/sessionfs/skills/data/*.txt"' in pyproject
    assert catalog_mirror.GITHUB_KNOWN_HOSTS.suffix == ".txt"
    assert stat.S_ISREG(catalog_mirror.GITHUB_KNOWN_HOSTS.stat().st_mode)


# ---------------------------------------------------------------------------
# Mirror configuration and add commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["SessionFS/marketplace", " SessionFS/marketplace ", "a/b", "a-b/c.d_e"])
def test_mirror_config_accepts_owner_repo(value: str) -> None:
    assert ServerConfig(public_catalog_mirror=value).public_catalog_mirror == value.strip()


@pytest.mark.parametrize(
    "value",
    ["https://github.com/SessionFS/marketplace", "SessionFS", "SessionFS/marketplace/extra",
     "SessionFS/marketplace.git", "-x/repo", "x-/repo", "a b/c", "owner/..", "owner/.",
     "git@github.com:SessionFS/marketplace.git", "o" * 40 + "/r", "owner/" + "r" * 101,
     "user:pass@SessionFS/marketplace"],
)
def test_mirror_config_refuses_anything_else(value: str) -> None:
    with pytest.raises(ValidationError):
        ServerConfig(public_catalog_mirror=value)


def test_mirror_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFS_PUBLIC_CATALOG_MIRROR", "SessionFS/marketplace")
    assert ServerConfig().public_catalog_mirror == "SessionFS/marketplace"
    monkeypatch.setenv("SFS_PUBLIC_CATALOG_MIRROR", "https://github.com/x/y")
    with pytest.raises(ValidationError):
        ServerConfig()
    monkeypatch.delenv("SFS_PUBLIC_CATALOG_MIRROR")
    assert ServerConfig().public_catalog_mirror == ""


CLONE = "https://api.example.test/api/v1/git/public/sessionfs.git"


def test_add_commands_without_a_mirror_are_unchanged() -> None:
    commands = _add_commands(CLONE, "sessionfs", public=True)
    assert commands["claude-code"] == {"add": f"/plugin marketplace add {CLONE}",
                                       "cli": f"claude plugin marketplace add {CLONE}",
                                       "install": "/plugin install <plugin>@sessionfs"}
    assert commands["copilot"]["add"] == f"copilot plugin marketplace add {CLONE}"
    assert commands["codex"]["add"] == f"codex plugin marketplace add {CLONE}"
    assert "add_hosted" not in json.dumps(commands)
    assert "github.com" not in json.dumps(commands)


def test_add_commands_lead_with_the_github_mirror() -> None:
    commands = _add_commands(CLONE, "sessionfs", public=True, mirror="SessionFS/marketplace")
    assert list(commands["claude-code"]) == ["add", "cli", "install", "add_hosted"]
    assert commands["claude-code"]["add"] == "/plugin marketplace add SessionFS/marketplace"
    assert commands["claude-code"]["cli"] == "claude plugin marketplace add SessionFS/marketplace"
    assert commands["claude-code"]["add_hosted"] == f"/plugin marketplace add {CLONE}"
    assert commands["copilot"]["add"] == "copilot plugin marketplace add SessionFS/marketplace"
    assert commands["copilot"]["add_hosted"] == f"copilot plugin marketplace add {CLONE}"
    assert commands["codex"]["add"] == "codex plugin marketplace add SessionFS/marketplace"
    assert commands["codex"]["add_hosted"] == f"codex plugin marketplace add {CLONE}"
    assert "https://github.com/SessionFS/marketplace" in commands["cursor"]["add"]
    assert "https://github.com/SessionFS/marketplace" in commands["antigravity"]["add"]
    # Gemini per-plugin repositories are not mirrored.
    assert commands["gemini"]["install"].startswith("gemini extensions install https://api.example.test/")
    # Private scopes ignore the mirror.
    private = _add_commands(CLONE, "sessionfs-org-x", public=False, mirror="SessionFS/marketplace")
    assert private == _add_commands(CLONE, "sessionfs-org-x", public=False)


def test_mirror_vendor_sources_cover_every_mirrored_tool() -> None:
    assert set(catalog_mirror.MIRROR_VENDOR_SOURCES) == {"claude-code", "copilot", "codex", "cursor"}
    assert all(url.startswith("https://") for url in catalog_mirror.MIRROR_VENDOR_SOURCES.values())


OLD_README = (
    "# sessionfs\n\nGenerated by SessionFS. Do not edit: every commit is rebuilt from the "
    "approved package set.\n\nAdd it to your tool with `sfs catalog add --for <tool>`.\n"
).encode()


def test_readme_mentions_the_mirror_only_when_configured() -> None:
    plain = native_feeds.build_catalog("public", None, _inputs())
    assert plain.files["README.md"] == OLD_README  # unconfigured bytes are unchanged
    mirrored = native_feeds.build_catalog("public", None, _inputs(), mirror="SessionFS/marketplace")
    readme = mirrored.files["README.md"].decode()
    assert readme.startswith(OLD_README.decode())
    assert "https://github.com/SessionFS/marketplace" in readme
    assert "`/plugin marketplace add SessionFS/marketplace`" in readme
    assert {k: v for k, v in mirrored.files.items() if k != "README.md"} == {
        k: v for k, v in plain.files.items() if k != "README.md"}
    org = native_feeds.build_catalog("org", "org_acme", _inputs(), mirror="SessionFS/marketplace")
    assert "github.com" not in org.files["README.md"].decode()


def test_catalog_add_run_uses_the_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSIONFS_API_KEY", KEY)
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test")
    body = {"items": [{"scope": "public", "scope_id": None, "marketplace": "sessionfs", "clone_url": CLONE,
                       "mirror": {"repo": "SessionFS/marketplace", "url": "https://github.com/SessionFS/marketplace",
                                  "branch": "main"},
                       "tools": _add_commands(CLONE, "sessionfs", public=True, mirror="SessionFS/marketplace")}]}
    monkeypatch.setattr(cmd_catalog, "_request", lambda method, path, payload=None: body)
    monkeypatch.setattr(cmd_catalog.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    monkeypatch.setattr(cmd_catalog.subprocess, "run", lambda argv, check=False: calls.append(argv) or Done())
    result = CliRunner().invoke(app, ["catalog", "add", "--for", "claude-code", "--run", "--no-credential-helper"])
    assert result.exit_code == 0, strip_ansi(result.output)
    output = strip_ansi(result.output)
    assert "https://github.com/SessionFS/marketplace" in output
    assert "/plugin marketplace add SessionFS/marketplace" in output
    assert calls[-1] == ["claude", "plugin", "marketplace", "add", "SessionFS/marketplace"]
    hosted = CliRunner().invoke(app, ["catalog", "add", "--for", "claude-code", "--run", "--hosted",
                                      "--no-credential-helper"])
    assert hosted.exit_code == 0, strip_ansi(hosted.output)
    assert calls[-1] == ["claude", "plugin", "marketplace", "add", CLONE]
