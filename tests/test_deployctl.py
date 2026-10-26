import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import deployctl


def project(tmp_path, **overrides):
    cfg = {"name": "arbitrary-service", **overrides}
    (tmp_path / "mcp.json").write_text(json.dumps(cfg))
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    return deployctl.manifest(tmp_path)


def test_arbitrary_project_needs_no_example_python_layout(tmp_path):
    cfg = project(tmp_path, env={"CUSTOM_OPTION": "works"})
    deployment, service, pdb = deployctl.mcp_resources(cfg, "evaluation", "registry/app:v1")
    assert deployment["metadata"]["name"] == "arbitrary-service"
    assert deployment["spec"]["replicas"] == 2
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e["value"] for e in container["env"]}["CUSTOM_OPTION"] == "works"
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    assert service["spec"]["type"] == "ClusterIP"
    assert all(container[k]["httpGet"]["path"] == "/health" for k in ("startupProbe", "readinessProbe", "livenessProbe"))


@pytest.mark.parametrize("replicas", [0, -1, True, 1.5])
def test_invalid_replica_counts(tmp_path, replicas):
    with pytest.raises(ValueError):
        project(tmp_path, replicas=replicas)


def test_external_redis_configuration_is_preserved(tmp_path):
    cfg = project(tmp_path, mode="stateful", state_backend="redis", env={"REDIS_URL": "redis://external:6379/4"})
    deployment = deployctl.mcp_resources(cfg, "test", "image:v1")[0]
    env = {e["name"]: e["value"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["REDIS_URL"] == "redis://external:6379/4"
    assert "redis" not in cfg


def test_managed_redis_is_scoped_and_persistent(tmp_path):
    cfg = project(tmp_path, mode="stateful", state_backend="redis")
    assert cfg["redis"]["name"] == "arbitrary-service-redis"
    pvc, deployment, service = deployctl.redis_resources("test", cfg["redis"]["name"], "2Gi", "local-path")
    assert pvc["spec"]["storageClassName"] == "local-path"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == pvc["metadata"]["name"]
    assert pod["containers"][0]["command"] == ["redis-server", "--appendonly", "yes"]


def test_image_tag_contains_build_id(monkeypatch, tmp_path):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "build":
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)
    monkeypatch.setattr(deployctl, "run", fake_run)
    image = deployctl.build_image(tmp_path, "custom", "evaluation", "registry.local/team")
    assert image == "registry.local/team/custom:evaluation-" + "a" * 64
    assert calls[-1] == ["docker", "tag", "sha256:" + "a" * 64, image]


def test_rollout_failure_restores_previous_resources(monkeypatch, tmp_path):
    project(tmp_path)
    old = deployctl.mcp_resources(deployctl.manifest(tmp_path), "test", "old:healthy")
    old[0]["metadata"]["generation"] = 1
    old[0]["status"] = {"observedGeneration": 1, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    snapshots = dict(zip(("deployment", "service", "pdb"), old))
    applied = []
    waits = []
    class FakeKubernetes:
        def __init__(self, args):
            pass
        def get(self, kind, name):
            return copy.deepcopy(snapshots[kind])
        def apply(self, resources):
            applied.extend(resources)
        def run(self, *args, **kwargs):
            pass
        def diagnostics(self, name):
            pass
        def wait(self, name):
            waits.append(name)
            if len(waits) == 1:
                raise RuntimeError("unhealthy new rollout")
    monkeypatch.setattr(deployctl, "Kubernetes", FakeKubernetes)
    args = SimpleNamespace(folder=str(tmp_path), namespace="test", replicas=None,
                           image="new:broken", version="v2", push_registry=None, kind_cluster=None)
    with pytest.raises(RuntimeError, match="unhealthy new rollout"):
        deployctl.cmd_deploy(args)
    restored = [r for r in applied if r["kind"] == "Deployment"][-1]
    assert restored["spec"] == old[0]["spec"]
    assert "status" not in restored
    assert waits == ["arbitrary-service", "arbitrary-service"]


def test_knative_mapping_preserves_contract_and_external_state(tmp_path):
    cfg = project(tmp_path, mode="stateful", state_backend="redis", autoscaling={"enabled": True})
    resource = deployctl.knative_resource(cfg, "custom", "registry/image@sha256:abc", "v2")
    assert resource["apiVersion"] == "serving.knative.dev/v1"
    template = resource["spec"]["template"]
    annotations = template["metadata"]["annotations"]
    assert annotations["autoscaling.knative.dev/min-scale"] == "0"
    assert annotations["autoscaling.knative.dev/activation-scale"] == "2"
    assert annotations["autoscaling.knative.dev/initial-scale"] == "2"
    assert annotations["autoscaling.knative.dev/max-scale"] == "10"
    assert annotations["autoscaling.knative.dev/target"] == "2"
    container = template["spec"]["containers"][0]
    assert container["ports"] == [{"name": "http1", "containerPort": 8000}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": 8000}
    env = {e["name"]: e["value"] for e in container["env"]}
    assert "PORT" not in env  # Knative provides the reserved variable.
    assert env["REDIS_URL"] == "redis://arbitrary-service-redis:6379/0"
    assert "arbitrary-service.custom.mcp.test" in env["MCP_ALLOWED_HOSTS"]
    assert "volumes" not in template["spec"]


@pytest.mark.parametrize("bounds", [
    {"minReplicas": -1}, {"activationReplicas": 1}, {"maxReplicas": 1},
    {"minReplicas": 11, "maxReplicas": 10}, {"targetConcurrency": 0}, {"windowSeconds": 2},
])
def test_invalid_autoscaling_bounds(tmp_path, bounds):
    with pytest.raises(ValueError):
        project(tmp_path, autoscaling={"enabled": True, **bounds})


def test_zero_pods_is_a_ready_knative_service():
    service = {"metadata": {"generation": 2},
               "status": {"observedGeneration": 2, "conditions": [{"type": "Ready", "status": "True"}]}}
    assert deployctl.knative_ready(service)
    service["status"]["observedGeneration"] = 1
    assert not deployctl.knative_ready(service)


def test_scale_down_waits_for_transient_status_counters(monkeypatch):
    resource = {"metadata": {"generation": 2}, "spec": {"replicas": 2},
                "status": {"observedGeneration": 2, "updatedReplicas": 2, "readyReplicas": 3, "availableReplicas": 3}}
    calls = []
    class Cluster:
        timeout = 10
        def wait(self, name):
            pass
        def get(self, kind, name):
            calls.append(name)
            result = copy.deepcopy(resource)
            if len(calls) > 1:
                result["status"].update(readyReplicas=2, availableReplicas=2)
            return result
    monkeypatch.setattr(deployctl.time, "sleep", lambda seconds: None)
    current = deployctl.wait_deployment(Cluster(), "test")
    assert current["status"]["readyReplicas"] == 2
    assert len(calls) == 2


def test_knative_candidate_wait_rejects_stale_latest_created(monkeypatch):
    template = {"metadata": {"annotations": {"deployment.mcp.io/version": "v2"}},
                "spec": {"containers": [{"image": "image:v2"}]}}
    commands = []
    class Cluster:
        timeout = 10
        reads = 0
        def get(self, kind, name):
            if kind == "services.serving.knative.dev":
                self.reads += 1
                return {"metadata": {"generation": 2}, "status": {"observedGeneration": 2,
                        "latestCreatedRevisionName": "old" if self.reads == 1 else "new"}}
            if name == "old":
                return {"metadata": {"annotations": {"deployment.mcp.io/version": "v1"}},
                        "spec": {"containers": [{"image": "image:v1"}]}}
            return template
        def run(self, command, **kwargs):
            commands.append(command)
    monkeypatch.setattr(deployctl.time, "sleep", lambda seconds: None)
    assert deployctl.wait_knative(Cluster(), "sample", template) == "new"
    assert commands[0][1] == "revision/new"


def test_knative_promotion_waits_for_actual_route_target(monkeypatch):
    class Cluster:
        timeout = 10
        reads = 0
        def get(self, kind, name):
            self.reads += 1
            return {"metadata": {"generation": 2}, "status": {"observedGeneration": 2,
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "traffic": [{"revisionName": "old" if self.reads == 1 else "new", "percent": 100}]}}
    cluster = Cluster()
    monkeypatch.setattr(deployctl.time, "sleep", lambda seconds: None)
    result = deployctl.wait_knative_route(cluster, "sample", "new")
    assert result["status"]["traffic"][0]["revisionName"] == "new"
    assert cluster.reads == 2


def test_update_after_rollback_preserves_immutable_knative_metadata(monkeypatch, tmp_path):
    cfg = project(tmp_path, autoscaling={"enabled": True})
    previous = {"metadata": {"annotations": {"serving.knative.dev/creator": "original-admin"}},
                "status": {"latestReadyRevisionName": "old", "traffic": [{"revisionName": "old", "percent": 100}]}}
    dry_runs = []
    class Cluster:
        namespace = "test"
        timeout = 10
        def run(self, command, **kwargs):
            if "--dry-run=server" in command:
                dry_runs.append(json.loads(kwargs["stdin"]))
        def apply(self, resources):
            pass
    monkeypatch.setattr(deployctl, "wait_knative", lambda *args: "new")
    monkeypatch.setattr(deployctl, "wait_knative_route", lambda *args: {"status": {"url": "http://sample"}})
    deployctl.deploy_knative(Cluster(), cfg, "image:v2", "v2", previous)
    assert dry_runs[0]["metadata"]["annotations"]["serving.knative.dev/creator"] == "original-admin"
