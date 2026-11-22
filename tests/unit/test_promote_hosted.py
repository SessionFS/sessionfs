"""Operator promotion trusts only its own snapshot record and rolls back on a
bad health check. Skips in a public checkout (no ``.release/``)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".release" / "promote_hosted.py"
if not SCRIPT.is_file():
    pytest.skip("no .release/ in this checkout", allow_module_level=True)

spec = importlib.util.spec_from_file_location("promote_hosted", SCRIPT)
assert spec and spec.loader
ph = importlib.util.module_from_spec(spec)
sys.modules["promote_hosted"] = ph
spec.loader.exec_module(ph)

SNAP = "a" * 40
PUB = "b" * 40
D = {n: "sha256:" + c * 64 for n, c in zip(ph.IMAGES, "123")}


def manifest(**over):
    m = {"version": "1.2.3", "snapshot_sha": SNAP, "public_sha": PUB,
         "images": {n: {"repository": f"{ph.REGISTRY}/sessionfs-ee-{n}", "tag": "1.2.3",
                        "digest": D[n]} for n in ph.IMAGES}}
    m.update(over)
    return m


RECORD = {"version": "1.2.3", "snapshot_sha": SNAP, "public_sha": PUB}


def test_manifest_must_match_the_local_record():
    assert ph.check_manifest(manifest(), RECORD, "1.2.3") == D
    with pytest.raises(SystemExit, match="this machine pushed"):
        ph.check_manifest(manifest(snapshot_sha="c" * 40), RECORD, "1.2.3")
    with pytest.raises(SystemExit, match="public SHA"):
        ph.check_manifest(manifest(public_sha="c" * 40), RECORD, "1.2.3")
    with pytest.raises(SystemExit, match="version mismatch"):
        ph.check_manifest(manifest(), RECORD, "1.2.4")


def test_manifest_images_are_pinned_to_the_ee_registry_and_tag():
    bad = manifest()
    bad["images"]["api"]["repository"] = "ghcr.io/evil/api"
    with pytest.raises(SystemExit, match="repository for api"):
        ph.check_manifest(bad, RECORD, "1.2.3")
    bad = manifest()
    bad["images"]["dashboard"]["digest"] = "latest"
    with pytest.raises(SystemExit, match="digest for dashboard"):
        ph.check_manifest(bad, RECORD, "1.2.3")
    bad = manifest()
    bad["images"]["mcp"]["tag"] = "1.2.2"
    with pytest.raises(SystemExit, match="tag for mcp"):
        ph.check_manifest(bad, RECORD, "1.2.3")


def test_never_goes_backwards_without_rollback():
    ph.check_direction("0.33.1", "0.33.0", rollback=False)
    ph.check_direction("0.33.1", "0.33.1", rollback=False)  # rerun is fine
    ph.check_direction("0.33.1", None, rollback=False)
    with pytest.raises(SystemExit, match="older than the serving 0.34.0"):
        ph.check_direction("0.33.1", "0.34.0", rollback=False)
    ph.check_direction("0.33.1", "0.34.0", rollback=True)
    assert ph.parse_version("0.10.2") > ph.parse_version("0.9.12")


def _job(name, image):
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}}}


OLD_API = "us-central1-docker.pkg.dev/p/sessionfs/sessionfs-api:latest"


def test_job_inventory_requires_the_known_jobs_and_readable_images():
    jobs = [
        _job("sessionfs-migrate", OLD_API),
        _job("sessionfs-catalog-mirror", OLD_API),
        _job("sessionfs-sweep", f"{ph.REGISTRY}/sessionfs-ee-api@{D['api']}"),
        _job("harness-claude", "us-central1-docker.pkg.dev/p/sessionfs-harness/sfs-harness-claude:1"),
        _job("sessionfs-apix", "us-central1-docker.pkg.dev/p/x/sessionfs-api-extra:1"),
    ]
    assert sorted(ph.job_inventory(jobs)) == ["sessionfs-catalog-mirror", "sessionfs-migrate",
                                              "sessionfs-sweep"]
    with pytest.raises(SystemExit, match="expected API-image jobs not found"):
        ph.job_inventory(jobs[2:])
    with pytest.raises(SystemExit, match="cannot read the image"):
        ph.job_inventory(jobs + [{"metadata": {"name": "odd"}, "spec": {}}])


def test_traffic_is_restored_exactly_or_refused():
    assert ph.traffic_arg([{"revisionName": "a", "percent": 90},
                           {"revisionName": "b", "percent": 10}]) == "--to-revisions=a=90,b=10"
    with pytest.raises(SystemExit, match="no revision name"):
        ph.traffic_arg([{"percent": 100}])
    with pytest.raises(SystemExit, match="add up to 100"):
        ph.traffic_arg([{"revisionName": "a", "percent": 50}])


def test_health_must_prove_the_enterprise_build():
    assert ph.health_ok({"version": "1.2.3", "edition": "enterprise", "ee_loaded": True}, "1.2.3")
    assert not ph.health_ok({"version": "1.2.3", "edition": "enterprise", "ee_loaded": False}, "1.2.3")
    assert not ph.health_ok({"version": "1.2.3", "edition": "community", "ee_loaded": True}, "1.2.3")
    assert not ph.health_ok({"version": "1.2.2", "edition": "enterprise", "ee_loaded": True}, "1.2.3")


def test_dashboard_output_must_be_the_enterprise_build(tmp_path):
    (tmp_path / "index.html").write_text("<html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "a.js").write_text('fetch("https://api.sessionfs.dev");v="1.2.3"')
    assert ph.check_dashboard_output(tmp_path, "1.2.3") == [
        "no Enterprise marker in the built JavaScript (Community build?)"]
    (tmp_path / "assets" / "b.js").write_text(f'x="{ph.EDITION_MARKER}"')
    assert ph.check_dashboard_output(tmp_path, "1.2.3") == []
    assert ph.check_dashboard_output(tmp_path, "9.9.9") == [
        "built JavaScript does not carry version 9.9.9"]


def test_static_site_needs_its_assets_and_the_spa_fallback(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script src="/assets/a.js"></script>'
                                         '<link href="/assets/b.css">')
    (tmp_path / "assets" / "a.js").write_text("x")
    cfg = {"routes": [{"handle": "filesystem"}, {"src": "/(.*)", "dest": "/index.html"}]}
    assert ph.check_static_site(tmp_path, cfg) == ["index.html references missing /assets/b.css"]
    (tmp_path / "assets" / "b.css").write_text("x")
    assert ph.check_static_site(tmp_path, cfg) == []
    assert ph.check_static_site(tmp_path, {"routes": []}) == [
        "no SPA fallback to /index.html in the Vercel output config"]
    assert ph.index_assets(tmp_path) == ["/assets/a.js", "/assets/b.css"]


# --------------------------------------------------------------------------
# A small simulated production for the stateful paths
# --------------------------------------------------------------------------


class Prod:
    def __init__(self, healthy=True, fail_job=None):
        self.traffic = [{"revisionName": "api-1", "percent": 100}]
        self.revisions = {"api-1": OLD_API.replace(":latest", "@sha256:" + "0" * 64)}
        self.created = "api-1"
        self.jobs = {"sessionfs-migrate": OLD_API, "sessionfs-catalog-mirror": OLD_API,
                     "sessionfs-reminders-due": OLD_API}
        self.dash = "https://dash-old.vercel.app"
        self.healthy = healthy
        self.fail_job = fail_job
        self.calls: list[list[str]] = []
        self.version = "1.2.2"

    def health(self, url):
        if self.traffic[0]["revisionName"] == "api-1":
            return {"version": self.version, "edition": "enterprise", "ee_loaded": True}
        return {"version": "1.2.3", "edition": "enterprise", "ee_loaded": self.healthy}

    def runner(self, cmd, cwd, env):
        self.calls.append(cmd)
        a = [c for c in cmd[1:] if not c.startswith(("--project=", "--region="))]
        if cmd[0] == "gcloud" and a[:3] == ["run", "services", "describe"]:
            return json.dumps({"status": {"latestReadyRevisionName": self.created,
                                          "latestCreatedRevisionName": self.created,
                                          "traffic": self.traffic},
                               "spec": {"template": {"spec": {"containers": [
                                   {"image": self.revisions[self.created]}]}}}})
        if cmd[0] == "gcloud" and a[:3] == ["run", "services", "update"]:
            n = f"api-{len(self.revisions) + 1}"
            self.revisions[n] = a[4].split("=", 1)[1]
            self.created = n
            return ""
        if cmd[0] == "gcloud" and a[:3] == ["run", "services", "update-traffic"] \
                and a[4] == "--to-latest":
            self.traffic = [{"revisionName": self.created, "percent": 100}]
            self.to_latest = True
            return ""
        if cmd[0] == "gcloud" and a[:3] == ["run", "services", "update-traffic"]:
            self.traffic = [{"revisionName": r.split("=")[0], "percent": int(r.split("=")[1])}
                            for r in a[4].split("=", 1)[1].split(",")]
            return ""
        if cmd[0] == "gcloud" and a[:3] == ["run", "revisions", "describe"]:
            return json.dumps({"spec": {"containers": [{"image": self.revisions[a[3]]}]}})
        if cmd[0] == "gcloud" and a[:3] == ["run", "jobs", "list"]:
            return json.dumps([_job(n, i) for n, i in self.jobs.items()])
        if cmd[0] == "gcloud" and a[:3] == ["run", "jobs", "update"]:
            if a[3] == self.fail_job and a[4] == f"--image={NEW_API}":
                raise SystemExit(f"gcloud failed: {a[3]}")
            self.jobs[a[3]] = a[4].split("=", 1)[1]
            return ""
        if cmd[0] == "gcloud" and a[:3] == ["run", "jobs", "execute"]:
            return ""
        if cmd[0] == "gcloud" and a[:4] == ["artifacts", "docker", "images", "describe"]:
            ref = a[4]
            digest = ref.split("@")[1] if "@" in ref else "sha256:" + "9" * 64
            if ref.startswith(ph.REGISTRY) and ":1.2.3" in ref:
                digest = D[ref.split("sessionfs-ee-")[1].split(":")[0]]
            return json.dumps({"image_summary": {"digest": digest}})
        if cmd[0] == "vercel" and cmd[1] == "inspect":
            return f"  id\t\tdpl_{'x' * 8}\n  url\t\t{self.dash}\n"
        if cmd[0] == "vercel" and cmd[1] == "promote":
            self.dash = cmd[2]
            return ""
        if cmd[0] == "gh" and cmd[2] == "list":
            return json.dumps([{"databaseId": 77, "headSha": SNAP, "headBranch": "ee-v1.2.3",
                                "status": "completed", "conclusion": "success"}])
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture()
def prod(monkeypatch, tmp_path):
    monkeypatch.setattr(ph, "gcloud_bin", lambda: "gcloud")
    monkeypatch.setattr(ph, "records_dir", lambda v: tmp_path / v)
    monkeypatch.setattr(ph, "load_manifest", lambda ctx, run_id: {**manifest(), "run_id": run_id})
    (tmp_path / "1.2.3").mkdir()
    (tmp_path / "1.2.3" / "snapshot.json").write_text(json.dumps(RECORD))
    return tmp_path / "1.2.3"


def _run(prod_state, *extra):
    ctx_kwargs = {"runner": prod_state.runner, "http": prod_state.health, "sleep": lambda s: None,
                  "fetch": lambda url: None}
    real = ph.Ctx

    def make(**kw):
        return real(**{**kw, **ctx_kwargs})

    ph.Ctx = make  # type: ignore[assignment]
    try:
        return ph.main(["--version", "1.2.3", "--skip-dashboard", *extra])
    finally:
        ph.Ctx = real  # type: ignore[assignment]


NEW_API = f"{ph.REGISTRY}/sessionfs-ee-api@{D['api']}"


def test_full_promotion_moves_api_and_every_job(prod):
    p = Prod()
    assert _run(p) == 0
    assert p.traffic == [{"revisionName": "api-2", "percent": 100}]
    assert getattr(p, "to_latest", False)  # Terraform revisions keep going live
    assert set(p.jobs.values()) == {NEW_API}
    promo = json.loads((prod / "promotion.json").read_text())
    assert promo["api_revision"] == "api-2"
    base = json.loads((prod / "baseline.json").read_text())
    assert base["api"]["traffic"] == [{"revisionName": "api-1", "percent": 100}]
    assert all("@sha256:" in i for i in base["jobs"].values())  # tags pinned to digests


def test_unhealthy_api_restores_traffic_and_the_migrate_job(prod):
    p = Prod(healthy=False)
    with pytest.raises(SystemExit, match="everything this run changed was restored"):
        _run(p)
    assert p.traffic == [{"revisionName": "api-1", "percent": 100}]
    assert "@sha256:" in p.jobs["sessionfs-migrate"] and p.jobs["sessionfs-migrate"] != NEW_API
    assert json.loads((prod / "failed.json").read_text())["restore_errors"] == []


def test_a_failed_job_update_restores_api_and_earlier_jobs(prod):
    p = Prod(fail_job="sessionfs-reminders-due")
    with pytest.raises(SystemExit, match="restored"):
        _run(p)
    assert p.traffic == [{"revisionName": "api-1", "percent": 100}]
    assert NEW_API not in p.jobs.values()


def test_retry_keeps_the_first_baseline(prod):
    p = Prod(fail_job="sessionfs-reminders-due")
    with pytest.raises(SystemExit):
        _run(p)
    first = (prod / "baseline.json").read_text()
    p.fail_job = None
    assert _run(p) == 0
    assert (prod / "baseline.json").read_text() == first


def test_refuses_to_repromote_or_go_backwards(prod):
    p = Prod()
    assert _run(p) == 0
    with pytest.raises(SystemExit, match="already promoted"):
        _run(p)
    q = Prod()
    q.version = "1.3.0"
    (prod / "promotion.json").unlink()
    with pytest.raises(SystemExit, match="older than the serving 1.3.0"):
        _run(q)


def test_rollback_undoes_only_while_this_release_serves(prod):
    p = Prod()
    assert _run(p) == 0
    p.traffic = [{"revisionName": "api-9", "percent": 100}]  # a newer release went live
    with pytest.raises(SystemExit, match="newer release may be live"):
        _run(p, "--rollback")
    p.traffic = [{"revisionName": "api-2", "percent": 100}]
    assert _run(p, "--rollback") == 0
    assert p.traffic == [{"revisionName": "api-1", "percent": 100}]
    assert NEW_API not in p.jobs.values()


def test_dashboard_that_does_not_serve_its_build_is_rolled_back(monkeypatch):
    monkeypatch.setattr(ph, "gcloud_bin", lambda: "gcloud")
    p = Prod()
    baseline = {"api": {"traffic": p.traffic}, "jobs": {},
                "dashboard": {"deployment_id": "dpl_old", "url": p.dash}}
    served = {"body": "<script src=/assets/old.js>"}
    ctx = ph.Ctx(version="1.2.3", plan=False, runner=p.runner, http=p.health,
                 sleep=lambda s: None, fetch=lambda url: served["body"])
    done: list[dict] = []
    with pytest.raises(SystemExit, match="does not serve the promoted build"):
        ph.promote_dashboard(ctx, "https://dash-new.vercel.app", ["/assets/new.js"], done)
    assert p.dash == "https://dash-new.vercel.app"
    assert ph.restore(ctx, baseline, done) == []
    assert p.dash == "https://dash-old.vercel.app"
    served["body"] = '<script src="/assets/new.js">'
    done.clear()
    ph.promote_dashboard(ctx, "https://dash-new.vercel.app", ["/assets/new.js"], done)
    assert p.dash == "https://dash-new.vercel.app"
