"""Release App credentials stay in memory and out of argv and output.

Skips in a public checkout, which has no ``.release/`` directory.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".release" / "release_app.py"
if not SCRIPT.is_file():
    pytest.skip("no .release/ in this checkout", allow_module_level=True)

spec = importlib.util.spec_from_file_location("release_app", SCRIPT)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules["release_app"] = app
spec.loader.exec_module(app)


@pytest.fixture(scope="module")
def private_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_config_requires_the_private_repo(tmp_path):
    cfg = tmp_path / "app.toml"
    cfg.write_text('app_id = 1\ninstallation_id = 2\nrepository = "sessionfs"\n')
    with pytest.raises(SystemExit, match="must be sessionfs-enterprise"):
        app.load_config(cfg)
    cfg.write_text('app_id = 1\ninstallation_id = 2\nrepository = "sessionfs-enterprise"\n')
    assert app.load_config(cfg) == app.AppConfig(1, 2, "sessionfs-enterprise")


def test_missing_config_explains_setup(tmp_path):
    with pytest.raises(SystemExit, match="Create the SessionFS Release GitHub App"):
        app.load_config(tmp_path / "absent.toml")


def test_token_request_is_scoped_to_one_repo_and_contents_write(private_key):
    import jwt

    seen = {}

    def http(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return {"token": "ghs_secret"}

    cfg = app.AppConfig(123, 456, "sessionfs-enterprise")
    assert app.installation_token(cfg, private_key, http=http) == "ghs_secret"
    assert seen["url"].endswith("/app/installations/456/access_tokens")
    # workflows: write because every snapshot replaces the workflow file.
    assert seen["body"] == {"repositories": ["sessionfs-enterprise"],
                            "permissions": {"contents": "write", "workflows": "write"}}
    claims = jwt.decode(seen["headers"]["Authorization"].split()[1],
                        options={"verify_signature": False})
    assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600


def test_token_failure_does_not_echo_credentials(private_key):
    def http(url, headers, body):
        raise RuntimeError("HTTP Error 401: Unauthorized")

    with pytest.raises(SystemExit) as exc:
        app.installation_token(app.AppConfig(1, 2, "sessionfs-enterprise"), private_key, http=http)
    assert "Bearer" not in str(exc.value) and "PRIVATE KEY" not in str(exc.value)


def test_push_env_keeps_token_out_of_argv_and_disables_helpers():
    env = app.git_push_env("ghs_secret")
    header = env["GIT_CONFIG_VALUE_0"]
    assert header.startswith("AUTHORIZATION: basic ")
    assert base64.b64decode(header.split()[-1]).decode() == "x-access-token:ghs_secret"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper" and env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_reads_the_header_from_the_environment(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = app.git_push_env("ghs_secret")
    out = subprocess.run(["git", "config", "--get-urlmatch", "http.extraheader",
                          "https://github.com/SessionFS/sessionfs-enterprise.git"],
                         cwd=tmp_path, capture_output=True, text=True,
                         env=app.clean_env(env)).stdout.strip()
    assert out == env["GIT_CONFIG_VALUE_0"]


def test_scrub_removes_header_and_encoded_token():
    env = app.git_push_env("ghs_secret")
    encoded = env["GIT_CONFIG_VALUE_0"].split()[-1]
    text = f"fatal: header {env['GIT_CONFIG_VALUE_0']} and {encoded}"
    cleaned = app.scrub(text, env)
    assert encoded not in cleaned and "basic" not in cleaned.replace("<redacted>", "")


def test_clean_env_drops_inherited_tracing_and_the_raw_token(monkeypatch):
    monkeypatch.setenv("GIT_TRACE", "/tmp/trace.log")
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    env = app.clean_env(app.git_push_env("ghs_secret"))
    assert not {"GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE"} & set(env)
    assert "ghs_secret" not in "".join(env.values())
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_scrub_removes_the_raw_token_too():
    env = app.git_push_env("ghs_secret")
    assert "ghs_secret" not in app.scrub("remote: bad token ghs_secret", env)


def test_keychain_key_must_be_base64_pem(monkeypatch, private_key):
    import base64 as b64

    class R:
        def __init__(self, out, code=0):
            self.stdout, self.returncode, self.stderr = out, code, ""

    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: R(b64.b64encode(private_key.encode()).decode()))
    assert "PRIVATE KEY" in app.read_private_key()
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R("not base64!"))
    with pytest.raises(SystemExit, match="base64"):
        app.read_private_key()
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R("", 44))
    with pytest.raises(SystemExit, match="not in the login keychain"):
        app.read_private_key()
