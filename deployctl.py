"""Build/publish/roll out any MCP container project. No MCP traffic uses this CLI."""
import argparse
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def run(args, stdin=None, capture=False, timeout=1200, env=None):
    print("$", subprocess.list2cmdline([str(a) for a in args]), flush=True)
    return subprocess.run(args, check=True, input=stdin, text=True,
                          encoding="utf-8", capture_output=capture, timeout=timeout, env=env)


def project_path(folder, relative, directory=False):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or "\\" in relative or ":" in relative:
        raise ValueError("Build paths must be project-relative paths")
    target = (folder / relative).resolve()
    if not target.is_relative_to(folder.resolve()) or not (target.is_dir() if directory else target.is_file()):
        raise ValueError("Build path must exist inside the project")
    return target


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("name must be a Kubernetes DNS label, <=63 chars")
    return name


def positive(value):
    if isinstance(value, bool) or int(value) != float(value) or int(value) < 1:
        raise ValueError("replicas and timeouts must be positive integers")
    return int(value)


def manifest(folder, config="mcp.json"):
    cfg = json.loads((folder / config).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("mcp.json must contain an object")
    validate_name(cfg["name"])
    cfg.setdefault("mode", "stateless")
    cfg.setdefault("port", 8000)
    cfg.setdefault("replicas", 2)
    if cfg["mode"] not in {"stateless", "stateful"}:
        raise ValueError("mode must be stateless or stateful")
    if cfg["port"] != 8000:
        raise ValueError("the compatible container contract requires port 8000")
    cfg["replicas"] = positive(cfg["replicas"])
    build = cfg.get("build", {})
    if not isinstance(build, dict):
        raise ValueError("build must be an object")
    project_path(folder, build.get("dockerfile", "Dockerfile"))
    project_path(folder, build.get("context", "."), directory=True)
    if cfg.get("mcp_path", "/mcp") != "/mcp":
        raise ValueError("the compatible container contract requires MCP endpoint /mcp")
    if "bounded" in build and not isinstance(build["bounded"], bool):
        raise ValueError("build.bounded must be a boolean")
    metadata = cfg.get("platform_metadata", {})
    if not isinstance(metadata, dict) or any(k not in {"project_id", "category"} or not isinstance(v, str) or
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", v) for k, v in metadata.items()):
        raise ValueError("platform_metadata must contain DNS-label project_id/category values")
    env = cfg.setdefault("env", {})
    if not isinstance(env, dict) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str)
        for k, v in env.items()
    ):
        raise ValueError("env must map environment variable names to strings")
    if cfg["mode"] == "stateful" and cfg.get("state_backend") != "redis":
        raise ValueError("stateful projects must declare state_backend=redis")
    resources = cfg.setdefault("resources", {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    })
    for group in ("requests", "limits"):
        if not isinstance(resources.get(group), dict) or not all(
            isinstance(resources[group].get(k), str) for k in ("cpu", "memory")
        ):
            raise ValueError("resources must specify CPU/memory requests and limits as strings")
    if "REDIS_URL" not in env and cfg["mode"] == "stateful":
        name = cfg["name"]
        redis_name = name + "-redis" if len(name) <= 52 else name[:43].rstrip("-") + "-" + sha256(name.encode()).hexdigest()[:8] + "-redis"
        redis_cfg = cfg.setdefault("redis", {})
        if not isinstance(redis_cfg, dict):
            raise ValueError("redis must be an object")
        validate_name(redis_cfg.setdefault("name", redis_name))
        validate_name(redis_cfg["name"] + "-data")
    if "image_pull_secrets" in cfg and (not isinstance(cfg["image_pull_secrets"], list) or
                                        not all(isinstance(n, str) for n in cfg["image_pull_secrets"])):
        raise ValueError("image_pull_secrets must be a list of existing Secret names")
    if "progress_deadline_seconds" in cfg:
        cfg["progress_deadline_seconds"] = positive(cfg["progress_deadline_seconds"])
        if cfg["progress_deadline_seconds"] <= 3:
            raise ValueError("progress_deadline_seconds must exceed minReadySeconds=3")
    scaling = cfg.setdefault("autoscaling", {"enabled": False})
    if not isinstance(scaling, dict) or not isinstance(scaling.get("enabled", False), bool):
        raise ValueError("autoscaling.enabled must be a boolean")
    if scaling.get("enabled"):
        defaults = {"minReplicas": 0, "activationReplicas": 2, "maxReplicas": 10,
                    "targetConcurrency": 2, "windowSeconds": 60, "routeDomain": "mcp.test"}
        for key, value in defaults.items():
            scaling.setdefault(key, value)
        minimum = scaling["minReplicas"]
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ValueError("minReplicas must be an integer >=0")
        for key in ("activationReplicas", "maxReplicas", "targetConcurrency", "windowSeconds"):
            scaling[key] = positive(scaling[key])
        if scaling["activationReplicas"] < 2:
            raise ValueError("Knative activationReplicas must be >=2")
        if scaling["maxReplicas"] < max(minimum, scaling["activationReplicas"]):
            raise ValueError("maxReplicas must cover minReplicas and activationReplicas")
        if not 6 <= scaling["windowSeconds"] <= 3600:
            raise ValueError("windowSeconds must be 6-3600")
        if not re.fullmatch(r"[a-z0-9.-]+", scaling["routeDomain"]):
            raise ValueError("routeDomain must be a DNS domain")
    return cfg


def redis_resources(namespace, name="redis", storage="1Gi", storage_class=None):
    """Persistent single-instance development Redis, scoped to its application."""
    metadata = {"name": name, "namespace": namespace}
    pvc_spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": storage}}}
    if storage_class is not None:
        pvc_spec["storageClassName"] = storage_class
    return [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": name + "-data", "namespace": namespace}, "spec": pvc_spec},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": {"app": name}},
                  "template": {"metadata": {"labels": {"app": name}}, "spec": {
                      "terminationGracePeriodSeconds": 30,
                      "containers": [{"name": "redis", "image": "redis:7-alpine",
                          "command": ["redis-server", "--appendonly", "yes"],
                          "ports": [{"containerPort": 6379}],
                          "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                        "limits": {"cpu": "500m", "memory": "256Mi"}},
                          "readinessProbe": {"exec": {"command": ["redis-cli", "ping"]}, "periodSeconds": 3},
                          "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": name + "-data"}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": {"app": name},
                  "ports": [{"port": 6379, "targetPort": 6379}]}}
    ]


def mcp_resources(cfg, namespace, image):
    name = cfg["name"]
    labels = {"app": name}
    ownership = {"platform.manufact.dev/" + k.replace("_", "-"): v for k, v in cfg.get("platform_metadata", {}).items()}
    hosts = [name, f"{name}:*", f"{name}.{namespace}", f"{name}.{namespace}:*",
             f"{name}.{namespace}.svc", f"{name}.{namespace}.svc:*",
             f"{name}.{namespace}.svc.cluster.local", f"{name}.{namespace}.svc.cluster.local:*"]
    env = {**cfg.get("env", {}), "HOST": "0.0.0.0", "PORT": "8000"}
    env["MCP_ALLOWED_HOSTS"] = ",".join(hosts + [env.get("MCP_ALLOWED_HOSTS", "")]).rstrip(",")
    if cfg["mode"] == "stateful" and "REDIS_URL" not in env:
        env["REDIS_URL"] = f"redis://{cfg['redis']['name']}:6379/0"
    container = {
        "name": "mcp", "image": image, "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": 8000}],
        "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
        "resources": cfg.get("resources", {"requests": {"cpu": "100m", "memory": "128Mi"},
                                           "limits": {"cpu": "500m", "memory": "512Mi"}}),
        "startupProbe": {"httpGet": {"path": "/health", "port": "http"},
                         "failureThreshold": 30, "periodSeconds": 2},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"},
                           "periodSeconds": 3, "failureThreshold": 2},
        "livenessProbe": {"httpGet": {"path": "/health", "port": "http"},
                          "periodSeconds": 10, "failureThreshold": 3},
    }
    pod_spec = {"terminationGracePeriodSeconds": 30, "automountServiceAccountToken": False,
                "containers": [container]}
    if cfg.get("image_pull_secrets"):
        pod_spec["imagePullSecrets"] = [{"name": validate_name(n)} for n in cfg["image_pull_secrets"]]
    metadata = {"name": name, "namespace": namespace, "labels": {**labels, **ownership}}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": cfg.get("replicas", 2), "minReadySeconds": 3,
                  "revisionHistoryLimit": 5, "progressDeadlineSeconds": cfg.get("progress_deadline_seconds", 180),
                  "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                  "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": {**labels, **ownership}}, "spec": pod_spec}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": labels,
                  "ports": [{"name": "http", "port": 8000, "targetPort": "http"}]}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": metadata,
         "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}},
    ]


def knative_resource(cfg, namespace, image, version):
    scaling = cfg["autoscaling"]
    # Reuse the same application environment/resources/Redis contract.
    container = copy.deepcopy(mcp_resources(cfg, namespace, image)[0]["spec"]["template"]["spec"]["containers"][0])
    container["name"] = "mcp"
    # Knative owns PORT and supplies the declared containerPort itself.
    container["env"] = [env for env in container["env"] if env["name"] != "PORT"]
    container["ports"][0]["name"] = "http1"
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        container[probe]["httpGet"]["port"] = 8000
    hostname = f"{cfg['name']}.{namespace}.{scaling['routeDomain']}"
    for env in container["env"]:
        if env["name"] == "MCP_ALLOWED_HOSTS":
            env["value"] += f",{hostname},{hostname}:*"
    annotations = {
        "autoscaling.knative.dev/class": "kpa.autoscaling.knative.dev",
        "autoscaling.knative.dev/metric": "concurrency",
        "autoscaling.knative.dev/min-scale": str(scaling["minReplicas"]),
        "autoscaling.knative.dev/activation-scale": str(scaling["activationReplicas"]),
        "autoscaling.knative.dev/initial-scale": str(max(scaling["minReplicas"], scaling["activationReplicas"])),
        "autoscaling.knative.dev/max-scale": str(scaling["maxReplicas"]),
        "autoscaling.knative.dev/target": str(scaling["targetConcurrency"]),
        "autoscaling.knative.dev/window": f"{scaling['windowSeconds']}s",
        "deployment.mcp.io/version": version,
    }
    template_spec = {"containers": [container], "timeoutSeconds": 120, "containerConcurrency": 0}
    if cfg.get("image_pull_secrets"):
        template_spec["imagePullSecrets"] = [{"name": name} for name in cfg["image_pull_secrets"]]
    return {"apiVersion": "serving.knative.dev/v1", "kind": "Service",
            "metadata": {"name": cfg["name"], "namespace": namespace, "labels": {"app": cfg["name"],
                **{"platform.manufact.dev/" + k.replace("_", "-"): v for k, v in cfg.get("platform_metadata", {}).items()}}},
            "spec": {"template": {"metadata": {"annotations": annotations}, "spec": template_spec},
                     "traffic": [{"latestRevision": True, "percent": 100}]}}


def knative_ready(service):
    return bool(service and service.get("status", {}).get("observedGeneration", 0) >= service["metadata"].get("generation", 1)
                and any(c["type"] == "Ready" and c["status"] == "True" for c in service.get("status", {}).get("conditions", [])))


def contains(actual, expected):
    """Compare desired fields while allowing Kubernetes admission defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def wait_knative(kube, name, template=None):
    deadline = time.monotonic() + kube.timeout
    while True:
        service = kube.get("services.serving.knative.dev", name)
        status = service.get("status", {}) if service else {}
        if service and status.get("observedGeneration", 0) >= service["metadata"]["generation"] and status.get("latestCreatedRevisionName"):
            revision = status["latestCreatedRevisionName"]
            candidate = kube.get("revisions.serving.knative.dev", revision)
            if candidate and (template is None or contains(candidate, template)):
                kube.run(["wait", f"revision/{revision}", "--for=condition=Ready", f"--timeout={kube.timeout}s"], timeout=kube.timeout + 30)
                return revision
        if time.monotonic() > deadline:
            raise RuntimeError("Knative did not observe the new configuration")
        time.sleep(1)


def wait_knative_route(kube, name, revision):
    deadline = time.monotonic() + kube.timeout
    while True:
        current = kube.get("ksvc", name)
        traffic = current.get("status", {}).get("traffic", []) if current else []
        if knative_ready(current) and len(traffic) == 1 and traffic[0].get("revisionName") == revision and traffic[0].get("percent") == 100:
            return current
        if time.monotonic() > deadline:
            raise RuntimeError("Knative route did not become ready at the current generation")
        time.sleep(1)


def deploy_knative(kube, cfg, image, version, previous, rollback_timeout=180):
    name = cfg["name"]
    resource = knative_resource(cfg, kube.namespace, image, version)
    if previous:
        # Rollback snapshots may have put admission-owned annotations into
        # kubectl's last-applied record. Preserve their immutable values on
        # later updates rather than generating a removal patch.
        for key in ("serving.knative.dev/creator", "serving.knative.dev/lastModifier"):
            value = previous["metadata"].get("annotations", {}).get(key)
            if value is not None:
                resource["metadata"].setdefault("annotations", {})[key] = value
    previous_revision = previous.get("status", {}).get("latestReadyRevisionName") if previous else None
    if previous:
        previous_revision = next((t["revisionName"] for t in previous.get("status", {}).get("traffic", [])
                                  if t.get("percent") == 100 and t.get("revisionName")), previous_revision)
    if previous_revision:
        # Keep the old route while the candidate revision is building/starting.
        resource["spec"]["traffic"] = [{"revisionName": previous_revision, "percent": 100}]
    kube.run(["apply", "--dry-run=server", "-f", "-"], stdin=json.dumps(resource))
    try:
        kube.apply([resource])
        revision = wait_knative(kube, name, resource["spec"]["template"])
        resource["spec"]["traffic"] = [{"revisionName": revision, "percent": 100}]
        kube.apply([resource])
        current = wait_knative_route(kube, name, revision)
    except (subprocess.SubprocessError, OSError, RuntimeError):
        print("ERROR: Knative rollout failed; restoring previous route/configuration", file=sys.stderr)
        failed_service = kube.get("ksvc", name)
        failed_revision = failed_service.get("status", {}).get("latestCreatedRevisionName") if failed_service else None
        for command in (["get", "revisions,pods", "-l", f"serving.knative.dev/service={name}", "-o", "wide"],
                        ["describe", "ksvc", name]):
            try:
                kube.run(command)
            except subprocess.SubprocessError:
                pass
        if previous:
            # A deliberately short candidate timeout must not also truncate
            # recovery while the restored configuration starts its replicas.
            kube.timeout = positive(rollback_timeout)
            restored = restorable(previous)
            restored["spec"]["traffic"] = [{"revisionName": previous_revision, "percent": 100}]
            kube.apply([restored])
            wait_knative(kube, name, restored["spec"]["template"])
            wait_knative_route(kube, name, previous_revision)
            if failed_revision and failed_revision != previous_revision:
                failed = kube.get("revisions.serving.knative.dev", failed_revision)
                if failed and failed["metadata"].get("labels", {}).get("serving.knative.dev/service") == name:
                    kube.run(["delete", f"revision/{failed_revision}", "--ignore-not-found", "--wait=false"])
            print("ROLLBACK OK: previous healthy Knative route restored", flush=True)
        else:
            kube.run(["delete", f"ksvc/{name}", "--ignore-not-found", "--wait=false"])
        raise
    print(json.dumps({"status": "healthy", "backend": "knative", "name": name, "namespace": kube.namespace,
                      "image": image, "revision": revision, "mcp_url": current["status"]["url"] + "/mcp"}), flush=True)


class Kubernetes:
    def __init__(self, args):
        self.prefix = ["kubectl"]
        if args.kubeconfig:
            self.prefix += ["--kubeconfig", args.kubeconfig]
        context = args.context or (f"kind-{args.kind_cluster}" if args.kind_cluster else None)
        if context:
            self.prefix += ["--context", context]
        self.prefix += ["--request-timeout=30s"]
        self.namespace = args.namespace
        self.timeout = positive(args.timeout)

    def run(self, args, **kwargs):
        return run(self.prefix + ["-n", self.namespace] + args, **kwargs)

    def get(self, kind, name):
        result = self.run(["get", f"{kind}/{name}", "--ignore-not-found", "-o", "json"], capture=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def apply(self, resources):
        self.run(["apply", "-f", "-"], stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}))

    def wait(self, name):
        self.run(["rollout", "status", f"deployment/{name}", f"--timeout={self.timeout}s"], timeout=self.timeout + 45)

    def diagnostics(self, name):
        for command in (["get", "pods", "-l", f"app={name}", "-o", "wide"],
                        ["describe", "deployment", name],
                        ["get", "events", "--sort-by=.lastTimestamp"]):
            try:
                self.run(command, timeout=45)
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"Diagnostic failed: {exc}", file=sys.stderr)


def healthy(deployment):
    spec, status = deployment["spec"], deployment.get("status", {})
    count = spec.get("replicas", 1)
    return (status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 1)
            and status.get("updatedReplicas", 0) == count
            and status.get("readyReplicas", 0) == count
            and status.get("availableReplicas", 0) == count)


def wait_deployment(kube, name):
    kube.wait(name)
    # rollout status can finish before a scale-down's status counters settle.
    # Wait for current generation and exact counts instead of falsely rolling
    # back a successful change while a terminating pod is still counted.
    deadline = time.monotonic() + getattr(kube, "timeout", 180)
    while True:
        current = kube.get("deployment", name)
        if current and healthy(current):
            return current
        if time.monotonic() > deadline:
            raise RuntimeError("rollout finished without the requested ready/available replica count")
        time.sleep(1)


def restorable(resource):
    restored = {k: copy.deepcopy(resource[k]) for k in ("apiVersion", "kind", "spec")}
    restored["metadata"] = {k: copy.deepcopy(resource["metadata"][k])
                            for k in ("name", "namespace", "labels", "annotations") if k in resource["metadata"]}
    restored["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return restored


def build_image(folder, name, version, registry, build=None):
    # Full build ID prevents replacing a previous version with different bytes.
    # Cached repeat builds resolve to the same tag and unchanged pod template.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", version):
        raise ValueError("version must be a Docker tag prefix of at most 63 characters")
    with tempfile.TemporaryDirectory(prefix="mcp-build-") as temporary:
        iidfile = Path(temporary) / "image-id"
        # BuildKit attestations include timestamps and would change the tag on
        # identical cached builds; disable them for this local content identity.
        build = build or {}
        context = project_path(folder, build.get("context", "."), directory=True)
        dockerfile = project_path(folder, build.get("dockerfile", "Dockerfile")) if build else folder / "Dockerfile"
        command = ["docker", "build", "--iidfile", str(iidfile)]
        if build.get("bounded"):
            # Opt-in trusted local imports only. Legacy RUN cgroups give hard
            # limits on the existing Docker engine, without creating a builder.
            # Fail closed if this engine has removed legacy-builder support.
            command += ["--memory", "1g", "--memory-swap", "1g", "--cpu-period", "100000",
                        "--cpu-quota", "100000", "--force-rm", "--network", "default"]
            run(command + ["-f", str(dockerfile), str(context)],
                timeout=300, env={**os.environ, "DOCKER_BUILDKIT": "0"})
        else:
            run(command + ["--provenance=false", "-f", str(dockerfile), str(context)])
        image_id = iidfile.read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError(f"unexpected Docker image ID: {image_id}")
    image = f"{registry.rstrip('/') if registry else 'mcp'}/{name}:{version}-{image_id[7:]}"
    run(["docker", "tag", image_id, image])
    return image


def cmd_deploy(args):
    folder = Path(args.folder).resolve()
    cfg = manifest(folder, getattr(args, "config", "mcp.json"))
    validate_name(args.namespace)
    if args.replicas is not None:
        cfg["replicas"] = positive(args.replicas)
    kube = Kubernetes(args)
    name = cfg["name"]
    autoscale = cfg["autoscaling"].get("enabled", False)
    if autoscale and args.replicas is not None:
        raise ValueError("use autoscaling bounds instead of --replicas with Knative")
    if autoscale:
        if kube.get("deployment", name):
            raise ValueError("Deployment backend already owns this name; use a separate namespace or an explicit migration")
        previous_knative = kube.get("services.serving.knative.dev", name)
        if previous_knative and not knative_ready(previous_knative):
            raise ValueError("existing Knative service must be healthy before deploying")
        previous = {}
    else:
        previous = {kind: kube.get(kind, name) for kind in ("deployment", "service", "pdb")}
        if previous["service"] and any(owner.get("apiVersion", "").startswith("serving.knative.dev/")
                                       for owner in previous["service"]["metadata"].get("ownerReferences", [])):
            raise ValueError("Knative already owns this name; use a separate namespace or an explicit migration")
    if previous.get("deployment") and not healthy(previous["deployment"]):
        raise ValueError("existing deployment is not fully healthy; recover it before deploying a new version")
    image = args.image or build_image(folder, name, args.version, args.push_registry, cfg.get("build"))
    if not args.image:
        if args.push_registry:
            if getattr(args, "ensure_floci_repository", False):
                if args.push_registry != "localhost:4566/000000000000/us-east-1":
                    raise ValueError("--ensure-floci-repository is restricted to the local demo registry")
                import boto3
                from botocore.config import Config
                ecr = boto3.client("ecr", endpoint_url="http://localhost:4566", region_name="us-east-1",
                    aws_access_key_id="test", aws_secret_access_key="test",
                    config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}))
                try:
                    ecr.create_repository(repositoryName=name, imageTagMutability="IMMUTABLE")
                    print("Created application ECR repository:", name, flush=True)
                except ecr.exceptions.RepositoryAlreadyExistsException:
                    pass
            run(["docker", "push", image])
        elif args.kind_cluster:
            run(["kind", "load", "docker-image", image, "--name", args.kind_cluster])
        else:
            raise ValueError("choose --push-registry or --kind-cluster to make built images accessible")
    kube.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": args.namespace}}])
    if cfg["mode"] == "stateful" and "REDIS_URL" not in cfg["env"]:
        redis = cfg["redis"]
        kube.apply(redis_resources(args.namespace, redis["name"], redis.get("storage", "1Gi"), redis.get("storage_class")))
        try:
            kube.wait(redis["name"])
        except (subprocess.SubprocessError, OSError):
            kube.diagnostics(redis["name"])
            raise
    if autoscale:
        if not args.image:
            # The built tag ends in its published manifest digest. A digest
            # avoids controller-side localhost registry tag resolution; k3s
            # pulls it through the same Floci ECR containerd mirror.
            image = image.rsplit(":", 1)[0] + "@sha256:" + image.rsplit("-", 1)[1]
        return deploy_knative(kube, cfg, image, args.version, previous_knative, getattr(args, "rollback_timeout", 180))
    resources = mcp_resources(cfg, args.namespace, image)
    kube.run(["apply", "--dry-run=server", "-f", "-"],
             stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}))
    try:
        kube.apply(resources)
        current = wait_deployment(kube, name)
    except (subprocess.SubprocessError, OSError, RuntimeError):
        print("ERROR: rollout failed; collecting diagnostics and restoring previous application resources", file=sys.stderr)
        kube.diagnostics(name)
        try:
            kube.timeout = positive(getattr(args, "rollback_timeout", 180))
            for kind, snapshot in previous.items():
                if snapshot:
                    kube.apply([restorable(snapshot)])
                else:
                    kube.run(["delete", f"{kind}/{name}", "--ignore-not-found", "--wait=false"])
            if previous["deployment"]:
                wait_deployment(kube, name)
                print("ROLLBACK OK: previous healthy deployment restored", flush=True)
        except (subprocess.SubprocessError, OSError, RuntimeError) as rollback_error:
            print(f"ROLLBACK FAILED: {rollback_error}; manual recovery required", file=sys.stderr)
        raise
    print(json.dumps({"status": "healthy", "name": name, "namespace": args.namespace,
                      "image": image, "ready_replicas": current["status"]["readyReplicas"],
                      "mcp_url": f"http://{name}.{args.namespace}.svc.cluster.local:8000/mcp"}), flush=True)
    kube.run(["get", "deployment,service,pdb", "-l", f"app={name}"])


def main():
    parser = argparse.ArgumentParser(description="One pipeline for compatible MCP container projects")
    sub = parser.add_subparsers(dest="action", required=True)
    deploy = sub.add_parser("deploy")
    deploy.add_argument("folder", help="project directory containing Dockerfile and mcp.json")
    deploy.add_argument("--config", default="mcp.json", help="project-relative configuration file; autoscaling.enabled selects Knative")
    deploy.add_argument("--namespace", default="mcp-demo")
    deploy.add_argument("--version", default="build",
                        help="version prefix; final tag includes the full Docker build ID")
    deploy.add_argument("--replicas", type=int, help="override mcp.json replicas (default 2)")
    publisher = deploy.add_mutually_exclusive_group(required=True)
    publisher.add_argument("--kind-cluster", help="load built image into the named kind cluster")
    publisher.add_argument("--push-registry", help="push built image to this registry/prefix")
    publisher.add_argument("--image", help="use an already published immutable image reference, skipping build/publish")
    deploy.add_argument("--context", help="explicit kubectl context (kind selects kind-NAME automatically)")
    deploy.add_argument("--kubeconfig", help="isolated kubeconfig path")
    deploy.add_argument("--timeout", type=int, default=180, help="rollout wait timeout in seconds")
    deploy.add_argument("--rollback-timeout", type=int, default=180, help="separate recovery wait timeout in seconds")
    deploy.add_argument("--ensure-floci-repository", action="store_true", help="create only this application's local ECR repository if absent")
    deploy.set_defaults(func=cmd_deploy)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
