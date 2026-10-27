"""Import/deploy route regressions in an isolated workspace; no real workloads."""
import io
import asyncio
import json
from pathlib import Path
import re
import sys
import zipfile

import pytest
from starlette.testclient import TestClient

import ui.app as application
from ui.sources import Sources


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "ui/static").mkdir(parents=True)
    source = tmp_path / "example"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch")
    (source / "mcp.json").write_text('{"name":"existing-echo","mode":"stateless"}')
    (tmp_path / "ui/projects.json").write_text(json.dumps({"echo": {"title": "Echo & Math", "category": "developer",
        "path": "example", "configs": {"always-on": "mcp.json"}}}))
    (tmp_path / "ui/runtime_metadata.json").write_text(json.dumps({"mcp-demo/failure-fixture": {
        "category": "probe", "display_name": "Expected failure \u00b7 fixture"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "ui/trusted_repositories.json").write_text('{"repositories":[]}')
    monkeypatch.setattr(application, "ROOT", tmp_path)
    monkeypatch.setenv("MCP_UI_DEPLOY_PYTHON", sys.executable)
    monkeypatch.setenv("MCP_UI_MAX_JOBS", "2")
    resource = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "existing-echo", "namespace": "mcp-demo",
        "uid": "unchanged-uid", "generation": 1, "creationTimestamp": "2026-01-01T00:00:00Z", "labels": {"app": "existing-echo"}},
        "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "mcp", "image": "registry/echo:v1",
            "ports": [{"containerPort": 8000}]}]}}}, "status": {"observedGeneration": 1,
            "updatedReplicas": 2, "availableReplicas": 2, "readyReplicas": 2, "conditions": []}}
    def kubectl(self, namespace, argv):
        if namespace == "mcp-demo" and argv[:1] == ["get"] and argv[1] != "events":
            return json.dumps({"items": [resource]})
        return '{"items":[]}'
    monkeypatch.setattr(application.Platform, "kubectl", kubectl)
    with TestClient(application.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as session:
        yield session


def stage(client, filename="customer-tools.zip", dockerfile="Dockerfile"):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr(dockerfile, "FROM scratch")
        archive.writestr("server.py", "# trusted fixture")
    token = client.app.state.platform.csrf
    response = client.post("/imports", data={"csrf": token, "kind": "zip", "trusted_code": "yes"},
                           files={"archive": (filename, contents.getvalue(), "application/zip")}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def fields(client, **updates):
    return {"csrf": client.app.state.platform.csrf, "server_name": "customer-tools", "display_name": "Customer Tools",
        "dockerfile": "Dockerfile", "build_context": ".", "port": "8000", "mcp_path": "/mcp", "mode": "stateless",
        "backend": "always-on", "version": "demo-v1", "safe_tool": "", "safe_arguments": "{}", "trusted_code": "true", **updates}


def test_both_configuration_urls_work_and_pending_import_is_resumable(client):
    identifier = stage(client)
    for url in ["/imports/" + identifier, "/imports/" + identifier + "/configure?utm_source=chatgpt.com"]:
        response = client.get(url)
        assert response.status_code == 200 and "customer-tools.zip" in response.text
        assert '<option value="">Choose a state model' in response.text
    for url in ["/imports", "/", "/deploy"]:
        response = client.get(url)
        assert "/imports/" + identifier + "/configure" in response.text


def test_registration_error_keeps_source_and_all_editable_fields(client):
    identifier = stage(client)
    response = client.post("/imports/" + identifier + "/configure", data=fields(client, mode=""))
    assert response.status_code == 422 and 'id="configure-form"' in response.text
    assert 'value="customer-tools"' in response.text and "Configuration failed" in response.text
    assert "customer-tools.zip" in response.text
    assert not client.app.state.platform.sources.registered


def test_successful_registration_selects_exact_project_and_preserves_release(client):
    identifier = stage(client)
    response = client.post("/imports/" + identifier + "/configure", data=fields(client))
    assert response.status_code == 200 and 'id="deploy-form"' in response.text
    assert response.url.query == ("project=byo-" + identifier[:20]).encode()
    project = client.app.state.platform.projects["byo-" + identifier[:20]]
    assert project["title"] == "Customer Tools" and project["default_version"] == "demo-v1"
    assert client.get("/imports/" + identifier + "/configure", follow_redirects=False).status_code == 303
    assert identifier not in [item["id"] for item in client.app.state.platform.sources.pending()]


def test_detects_nonstandard_dockerfile_name(client):
    identifier = stage(client, dockerfile="Dockerfile.dockerfile")
    response = client.get("/imports/" + identifier + "/configure")
    assert 'name="dockerfile" required value="Dockerfile.dockerfile"' in response.text


def test_name_conflict_recovers_on_form_without_hiding_the_error(client):
    identifier = stage(client)
    response = client.post("/imports/" + identifier + "/configure", data=fields(client, server_name="existing-echo"))
    assert response.status_code == 409 and 'id="configure-form"' in response.text and "already reserved" in response.text


def test_invalid_zip_and_deploy_errors_are_actionable_forms(client):
    response = client.post("/imports", data={"csrf": client.app.state.platform.csrf, "kind": "zip", "trusted_code": "yes"},
                           files={"archive": ("broken.zip", b"not a ZIP", "application/zip")})
    assert response.status_code == 422 and 'id="import-form"' in response.text and "valid ZIP" in response.text
    response = client.post("/deploy", data={"csrf": client.app.state.platform.csrf, "project_id": "missing"})
    assert response.status_code == 422 and 'id="deploy-form"' in response.text
    assert not client.app.state.platform.jobs
    assert client.get("/deploy?project=missing").status_code == 404


def test_display_rename_is_persisted_escaped_and_does_not_touch_kubernetes(client):
    response = client.post("/deployments/mcp-demo/existing-echo/display-name",
                           data={"csrf": client.app.state.platform.csrf, "display_name": "Customer <Tools>"})
    assert response.status_code == 200 and "Customer &lt;Tools&gt;" in response.text
    platform = client.app.state.platform
    assert json.loads(platform.names_path.read_text())["mcp-demo/existing-echo"] == "Customer <Tools>"
    assert not platform.jobs
    assert client.post("/deployments/mcp-demo/existing-echo/display-name", data={"display_name": "changed"}).status_code == 403


def test_job_names_distinguish_attempts_and_explicit_test_fixture(client):
    platform = client.app.state.platform
    for index in range(2):
        job = {"id": str(index) * 32, "name": "existing-echo", "namespace": "mcp-demo", "backend": "always-on",
               "project_id": "echo", "source": "reuse", "status": "succeeded", "version": "v1",
               "created_at": f"2026-01-01T00:00:0{index}Z", "finished_at": "2026-01-01T00:01:00Z", "command": []}
        platform.jobs[job["id"]] = job
    jobs = [platform.present_job(job) for job in platform.jobs.values()]
    assert [job["attempt"] for job in jobs] == [1, 2]
    assert jobs[0]["display_name"] == "Echo & Math" and jobs[0]["action"] == "Reuse published image"
    failure = {**next(iter(platform.jobs.values())), "name": "failure-fixture", "status": "failed"}
    assert platform.present_job(failure)["category"] == "probe"
    assert platform.present_job(failure)["display_name"] == "Expected failure \u00b7 fixture"
    assert "attempt #2" in client.get("/").text


def test_registry_write_failure_is_not_reported_as_registered(client, monkeypatch):
    identifier = stage(client)
    import ui.sources as sources
    def failed_write(*_):
        raise OSError("test disk write failure")
    monkeypatch.setattr(sources, "atomic_json", failed_write)
    with pytest.raises(OSError):
        client.app.state.platform.sources.register(identifier, {"id": "not-persisted"})
    assert identifier not in client.app.state.platform.sources.registered


def test_restart_then_register_another_import_keeps_registry_serializable(client):
    first = stage(client)
    response = client.post("/imports/" + first + "/configure", data=fields(client))
    assert response.status_code == 200
    # This is the actual failure sequence: hydrate an existing registry, then
    # persist another import. The runtime adds Path fields to its own objects.
    restarted = application.Platform()
    assert "folder" in restarted.projects["byo-" + first[:20]]
    assert "folder" not in restarted.sources.registered[first]
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr("Dockerfile", "FROM scratch")
    second = restarted.sources.stage("zip", data=contents.getvalue())
    payload = application.RegistrationRequest(server_name="second-tools", mode="stateless", backend="always-on",
                                               version="v1", trusted_code=True)
    asyncio.run(restarted.register(second["id"], payload))
    saved = json.loads(restarted.sources.registry_path.read_text())
    assert len(saved) == 2 and all("folder" not in project for project in saved.values())
