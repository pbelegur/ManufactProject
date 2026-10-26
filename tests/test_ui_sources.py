import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

import deployctl
from ui import sources
from ui.monitoring import health, redis_view, job_matches_runtime


def archive(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as output:
        for name, content in entries:
            output.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "C:/drive.py", "..\\escape.py", "NUL.txt", "a/.aws/config", ".env", "key.pem"])
def test_archive_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(ValueError):
        sources.extract_zip(archive([(name, "unsafe")]), tmp_path / "project")
    assert not (tmp_path / "project").exists()


def test_archive_rejects_links_and_case_collisions(tmp_path):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="Symlinks"):
        sources.extract_zip(archive([(link, "/etc/passwd")]), tmp_path / "link")
    with pytest.raises(ValueError, match="colliding"):
        sources.extract_zip(archive([("server.py", "a"), ("SERVER.py", "b")]), tmp_path / "collision")


def test_archive_bomb_and_file_count_limits(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="ratio"):
        sources.extract_zip(archive([("bomb.txt", "a" * 1_000_000)]), tmp_path / "bomb")
    monkeypatch.setattr(sources, "MAX_FILES", 1)
    with pytest.raises(ValueError, match="files"):
        sources.extract_zip(archive([("a", "a"), ("b", "b")]), tmp_path / "many")


def test_archive_validates_real_expanded_and_compressed_limits(tmp_path, monkeypatch):
    data = archive([("project/server.py", "print('hello')"), ("project/Dockerfile", "FROM scratch")])
    stats = sources.extract_zip(data, tmp_path / "valid")
    assert (tmp_path / "valid/server.py").read_text() == "print('hello')"
    assert stats["files"] == 2 and len(stats["archive_sha256"]) == 64
    monkeypatch.setattr(sources, "MAX_EXPANDED", 2)
    with pytest.raises(ValueError):
        sources.extract_zip(data, tmp_path / "expanded")
    monkeypatch.setattr(sources, "MAX_ZIP", 2)
    with pytest.raises(ValueError, match="compressed"):
        sources.extract_zip(data, tmp_path / "compressed")


def source_store(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui/trusted_repositories.json").write_text(json.dumps({"repositories": ["https://github.com/trusted/project"]}))
    return sources.Sources(tmp_path)


def test_missing_config_is_not_assumed_stateless(tmp_path):
    store = source_store(tmp_path)
    staged = store.stage("zip", data=archive([("Dockerfile", "FROM scratch")]))
    assert staged["has_config"] is False
    assert "mode" not in staged["initial"]


def test_github_exact_allowlist_and_commit_pin(tmp_path, monkeypatch):
    store = source_store(tmp_path)
    calls = []
    commit = "a" * 40
    def fake_download(url, limit):
        calls.append(url)
        return json.dumps({"sha": commit}).encode() if url.endswith("commits/HEAD") else archive([("root/Dockerfile", "FROM scratch")])
    monkeypatch.setattr(sources, "download", fake_download)
    with pytest.raises(ValueError, match="trusted"):
        store.stage("github", url="https://github.com/trusted/project.evil")
    assert not calls
    result = store.stage("github", url="https://github.com/trusted/project")
    assert result["source"]["commit"] == commit
    assert calls[-1] == "https://codeload.github.com/trusted/project/zip/" + commit


def test_malformed_configuration_has_clear_import_error(tmp_path):
    store = source_store(tmp_path)
    with pytest.raises(ValueError, match="objects"):
        store.stage("zip", data=archive([("mcp.json", '{"build":[]}')]))
    assert not list(store.folder.iterdir())


def test_build_paths_cannot_escape_project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (tmp_path / "outside").write_text("FROM scratch")
    for value in ["../outside", str(tmp_path / "outside"), "C:/Windows/file", "..\\outside"]:
        with pytest.raises(ValueError):
            deployctl.project_path(folder, value)


def test_bounded_build_passes_real_engine_limits(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM scratch")
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "build":
            Path(argv[argv.index("--iidfile") + 1]).write_text("sha256:" + "b" * 64)
    monkeypatch.setattr(deployctl, "run", fake_run)
    deployctl.build_image(tmp_path, "owned", "v1", "registry", {"bounded": True})
    command, options = calls[0]
    assert command[command.index("--memory") + 1] == "1g"
    assert command[command.index("--cpu-quota") + 1] == "100000"
    assert options["env"]["DOCKER_BUILDKIT"] == "0" and options["timeout"] == 300
    assert "--provenance=false" not in command


@pytest.mark.parametrize("backend,desired,ready,expected", [("always-on", 2, 1, "Degraded"), ("knative", 2, 1, "Scaling"),
    ("knative", 0, 0, "Idle/Scaled-to-zero"), ("always-on", 2, 2, "Healthy")])
def test_partial_readiness_is_never_healthy(backend, desired, ready, expected):
    resource = {"metadata": {"generation": 1}, "status": {"observedGeneration": 1, "updatedReplicas": desired,
        "availableReplicas": ready, "conditions": [{"type": "Ready", "status": "True"}]}}
    assert health(backend, desired, ready, resource, []) == expected


def test_failed_pod_and_failed_rollout_are_not_healthy():
    resource = {"metadata": {"generation": 1}, "status": {"observedGeneration": 1,
        "conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}]}}
    assert health("always-on", 2, 0, resource, []) == "Failed"
    assert health("always-on", 2, 1, resource, []) == "Degraded"


def test_redis_configuration_never_returns_inline_credentials():
    view = redis_view({"env": [{"name": "REDIS_URL", "value": "redis://user:private@shared:6379/2"}]}, {"shared": {}})
    assert view["host"] == "shared" and view["database"] == "2"
    assert "private" not in json.dumps(view) and "user" not in json.dumps(view)


def test_same_image_different_revision_does_not_inherit_old_job_timestamp():
    row = {"backend": "knative", "image": "registry/tool@sha256:same", "traffic": [{"percent": 100, "revisionName": "tool-00002"}]}
    job = {"status": "succeeded", "deployment_result": {"image": row["image"], "revision": "tool-00001"}}
    assert not job_matches_runtime(job, row)
    job["deployment_result"]["revision"] = "tool-00002"
    assert job_matches_runtime(job, row)
    job["status"] = "failed"
    assert not job_matches_runtime(job, row)


def test_legacy_knative_job_without_revision_uses_kubernetes_time_instead():
    row = {"backend": "knative", "image": "registry/tool@sha256:same", "traffic": [{"percent": 100, "revisionName": "tool-00002"}]}
    assert not job_matches_runtime({"status": "succeeded"}, row)
