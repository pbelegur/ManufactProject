"""Localhost UI adapter: read Kubernetes; execute only the existing deployctl."""
import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import threading
import zipfile
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import Response

import deployctl
from ui.sources import Sources, MAX_ZIP, atomic_json
from ui.monitoring import health as runtime_health, redact, redis_view, job_matches_runtime
from ui.jobs import DeploymentQueue, BoundedLog, LogRedactor, run_pipeline, snapshot_project, worker_limit, STAGES

ROOT = Path(__file__).resolve().parents[1]
NAMESPACES = {"always-on": "mcp-demo", "knative": "mcp-auto"}
TEMPLATES = Jinja2Templates(directory=str(ROOT / "ui/templates"))


def now():
    return datetime.now(timezone.utc).isoformat()


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    backend: Literal["always-on", "knative"] = "always-on"
    source: Literal["reuse", "build"] = "reuse"
    version: str = Field(default="ui-v1", pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
    image: str = Field(default="", max_length=400)
    timeout_seconds: int = Field(default=180, ge=30, le=300)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,51}$")
    dockerfile: str = Field(default="Dockerfile", max_length=200)
    build_context: str = Field(default=".", max_length=200)
    port: int = 8000
    mcp_path: str = "/mcp"
    mode: Literal["stateless", "stateful"]
    backend: Literal["always-on", "knative"]
    version: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
    redis_url: str = Field(default="", max_length=400)
    safe_tool: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]{0,100}$")
    safe_arguments: str = Field(default="{}", max_length=8192)
    confirm_read_only: bool = False
    trusted_code: bool = False


class Platform:
    def __init__(self):
        self.kubeconfig = Path(os.environ.get("MCP_UI_KUBECONFIG", ROOT / ".local/floci.kubeconfig")).resolve()
        self.python = Path(os.environ.get("MCP_UI_DEPLOY_PYTHON", ROOT / ".venv/Scripts/python.exe")).resolve()
        if os.name != "nt" and "MCP_UI_DEPLOY_PYTHON" not in os.environ:
            self.python = ROOT / ".venv/bin/python"
        self.registry = os.environ.get("MCP_UI_REGISTRY", "localhost:4566/000000000000/us-east-1").rstrip("/")
        self.jobs_dir = ROOT / ".local/ui/jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.job_guard = threading.RLock()
        self.log_bytes = int(os.environ.get("MCP_UI_JOB_LOG_BYTES", "1048576"))
        if not 65536 <= self.log_bytes <= 16 * 1024 * 1024:
            raise ValueError("MCP_UI_JOB_LOG_BYTES must be 65536..16777216")
        self.max_jobs = worker_limit()
        self.job_inputs = {}
        self.logs = {}
        self.work_dir = ROOT / ".local/ui/work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.projects = json.loads((ROOT / "ui/projects.json").read_text(encoding="utf-8"))
        self.sources = Sources(ROOT)
        self.projects.update({p["id"]: p for p in self.sources.registered.values()})
        self.runtime_metadata = json.loads((ROOT / "ui/runtime_metadata.json").read_text())
        self.registration_lock = asyncio.Lock()
        self.import_lock = asyncio.Lock()
        self.test_lock = asyncio.Lock()
        for project in self.projects.values():
            folder = (ROOT / project["path"]).resolve()
            if not folder.is_relative_to(ROOT) or not folder.is_dir():
                raise ValueError("Allowlisted project must resolve inside this workspace")
            project["folder"] = folder
        self.jobs = {}
        for path in self.jobs_dir.glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", finished_at=now(),
                           stage="FAILED", stage_at=now(),
                           error="UI restarted before completion was recorded. Inspect Kubernetes and the log before retrying; a CLI process may still be running.")
                self.persist(job)
            self.jobs[job["id"]] = job
        self.queue = DeploymentQueue(self.max_jobs, capacity=8)
        self.csrf = secrets.token_urlsafe(32)
        self.cache = None
        self.cached_at = 0
        self.cache_lock = asyncio.Lock()
        self.accepting = True

    def config(self, project_id, backend):
        project = self.projects.get(project_id)
        if not project or backend not in NAMESPACES:
            raise HTTPException(422, "Choose a project and backend from the local allowlist")
        folder = (ROOT / project["path"]).resolve()
        if folder != project["folder"] or not folder.is_relative_to(ROOT):
            raise HTTPException(422, "Allowlisted project path changed")
        filename = project["configs"].get(backend)
        if not filename:
            raise HTTPException(422, "This project has no configuration for that backend")
        config_path = (folder / filename).resolve()
        if not config_path.is_relative_to(folder):
            raise HTTPException(422, "Configuration must stay inside the allowlisted project")
        try:
            cfg = deployctl.manifest(folder, str(config_path))
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(422, f"Project configuration is invalid: {exc}") from exc
        if cfg["autoscaling"].get("enabled", False) != (backend == "knative"):
            raise HTTPException(422, "Allowlisted configuration does not match the selected backend")
        return project, cfg, config_path

    def catalogue(self):
        result = []
        for project_id, project in self.projects.items():
            profiles = {}
            for backend in project["configs"]:
                _, cfg, path = self.config(project_id, backend)
                profiles[backend] = {"filename": path.name, **{k: cfg[k] for k in
                    ("name", "mode", "port", "replicas", "resources", "autoscaling")}}
            result.append({"id": project_id, "title": project["title"], "path": project["path"], "profiles": profiles,
                           "default_version": project.get("default_version", "ui-v1"), "category": project.get("category", "unregistered")})
        return result

    def kubectl(self, namespace, arguments):
        if namespace not in NAMESPACES.values():
            raise HTTPException(404, "Namespace is not managed by this local UI")
        if not self.kubeconfig.is_file():
            raise RuntimeError(f"Isolated kubeconfig not found: {self.kubeconfig}")
        command = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--request-timeout=15s", "-n", namespace, *arguments]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=25, shell=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-4000:])
        return result.stdout or result.stderr

    def read_inventory(self):
        items, errors = [], []
        for backend, namespace in NAMESPACES.items():
            try:
                # Status only; these commands cannot create or update resources.
                raw = json.loads(self.kubectl(namespace, ["get", "deployments,services,pods,replicasets,services.serving.knative.dev,revisions.serving.knative.dev", "-o", "json"]))
                resources = raw["items"]
                deployments = [r for r in resources if r["kind"] == "Deployment"]
                services = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Service" and r["apiVersion"] == "v1"}
                revisions = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Revision"}
                if backend == "always-on":
                    for deployment in deployments:
                        name = deployment["metadata"]["name"]
                        containers = deployment["spec"]["template"]["spec"]["containers"]
                        if deployment["metadata"].get("labels", {}).get("app") != name:
                            continue
                        mcp = next((c for c in containers if any(p.get("containerPort") == 8000 for p in c.get("ports", []))), None)
                        if not mcp:
                            continue
                        service = services.get(name)
                        port = next((p["port"] for p in service["spec"]["ports"] if p["port"] == 8000), None) if service else None
                        endpoint = f"http://{name}.{namespace}.svc.cluster.local:{port}/mcp" if port else None
                        status = deployment.get("status", {})
                        desired = deployment["spec"].get("replicas", 1)
                        row = self.view(namespace, name, backend, mcp, desired, status.get("readyReplicas", 0), "Unknown",
                                        endpoint, deployment.get("status", {}).get("conditions", []))
                        self.enrich(row, deployment, resources, services)
                        items.append(row)
                else:
                    for ksvc in [r for r in resources if r["kind"] == "Service" and r["apiVersion"].startswith("serving.knative.dev/")]:
                        name = ksvc["metadata"]["name"]
                        status = ksvc.get("status", {})
                        routed = [revisions[t["revisionName"]] for t in status.get("traffic", []) if t.get("percent", 0) and t.get("revisionName") in revisions]
                        container = routed[0]["spec"]["containers"][0] if len(routed) == 1 else ksvc["spec"]["template"]["spec"]["containers"][0]
                        owned = [d for d in deployments if d["metadata"].get("labels", {}).get("serving.knative.dev/service") == name]
                        desired = sum(d["spec"].get("replicas", 0) for d in owned)
                        ready = sum(d.get("status", {}).get("readyReplicas", 0) for d in owned)
                        endpoint = status.get("url")
                        row = self.view(namespace, name, backend, container, desired, ready, "Unknown",
                                        endpoint.rstrip("/") + "/mcp" if endpoint else None, status.get("conditions", []))
                        row.update(traffic=status.get("traffic", []), latest_ready=status.get("latestReadyRevisionName"),
                                   configured_image=ksvc["spec"]["template"]["spec"]["containers"][0]["image"],
                                   release=(routed[0]["metadata"].get("annotations", {}).get("deployment.mcp.io/version") if len(routed) == 1 else None))
                        self.enrich(row, ksvc, resources, services)
                        items.append(row)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                errors.append({"namespace": namespace, "error": str(exc)})
        known = {(r["namespace"], r["name"]) for r in items}
        for pid, project in self.projects.items():
            if project.get("server_name") and (project["namespace"], project["server_name"]) not in known:
                jobs = self.server_jobs(project["namespace"], project["server_name"])
                latest = jobs[0] if jobs else None
                state = ("Deploying" if latest and latest["status"] in {"queued", "running"} else
                         "Failed" if latest and latest["status"] in {"failed", "interrupted"} else "Not deployed")
                if any(e["namespace"] == project["namespace"] for e in errors):
                    state = "Unknown"
                items.append({"namespace": project["namespace"], "name": project["server_name"], "backend": project["backend"],
                    "identity": project["namespace"] + "/" + project["server_name"], "mode": project["mode"],
                    "project_id": pid, "source_project": project["path"], "source_identity": project["source"],
                    "category": "developer", "version": None, "image": None, "revision": None, "image_digests": [],
                    "desired_replicas": None, "healthy_replicas": None, "health": state, "runtime_health": "Not observed",
                    "endpoint": None, "conditions": [], "latest_job": latest, "last_deployed_at": None,
                    "deployment_time_source": "No successful deployment recorded", "exists": False, "redis": None,
                    "safe_test": project.get("safe_test"), "history": [], "events": [], "pods": []})
        return {"items": sorted(items, key=lambda r: (r["name"], r["namespace"])), "errors": errors, "observed_at": now()}

    def view(self, namespace, name, backend, container, desired, ready, health, endpoint, conditions):
        image = container["image"]
        version = image.split("@", 1)[1] if "@" in image else image.rsplit("/", 1)[-1].partition(":")[2] or "Unspecified"
        mode = "stateful" if any(e["name"] == "REDIS_URL" for e in container.get("env", [])) else "stateless"
        project_id = next((pid for pid, p in self.projects.items() if backend in p["configs"] and self.config(pid, backend)[1]["name"] == name), None)
        return {"namespace": namespace, "name": name, "backend": backend, "mode": mode, "version": version,
                "image": image, "desired_replicas": desired, "healthy_replicas": ready, "health": health,
                "endpoint": endpoint, "conditions": conditions, "project_id": project_id}

    def server_jobs(self, namespace, name):
        with self.job_guard:
            jobs = copy.deepcopy(list(self.jobs.values()))
        return sorted((j for j in jobs if j["namespace"] == namespace and j["name"] == name),
                      key=lambda j: j["created_at"], reverse=True)

    def enrich(self, row, resource, resources, services):
        name, namespace, backend = row["name"], row["namespace"], row["backend"]
        selector = "serving.knative.dev/service" if backend == "knative" else "app"
        pods = [r for r in resources if r["kind"] == "Pod" and r["metadata"].get("labels", {}).get(selector) == name]
        explicit = self.runtime_metadata.get(namespace + "/" + name, {})
        pid = resource["metadata"].get("labels", {}).get("platform.manufact.dev/project-id") or row["project_id"] or explicit.get("project_id")
        project = self.projects.get(pid, {})
        row.update(identity=namespace + "/" + name, project_id=pid,
                   category=resource["metadata"].get("labels", {}).get("platform.manufact.dev/category") or explicit.get("category") or project.get("category", "unregistered"),
                   source_project=project.get("path"), source_identity=explicit.get("source") or project.get("source"),
                   safe_test=project.get("safe_test"), exists=True)
        jobs = self.server_jobs(namespace, name)
        state = runtime_health(backend, row["desired_replicas"], row["healthy_replicas"], resource, pods)
        row.update(runtime_health=state, health="Deploying" if jobs and jobs[0]["status"] in {"queued", "running"} else state,
                   latest_job=jobs[0] if jobs else None)
        routed_names = {t.get("revisionName") for t in row.get("traffic", []) if t.get("percent")}
        actual_revision = next((r for r in resources if r["kind"] == "Revision" and r["metadata"]["name"] in routed_names), None)
        container = (actual_revision["spec"]["containers"][0] if actual_revision else resource["spec"]["template"]["spec"]["containers"][0])
        row["redis"] = redis_view(container, services)
        row["pods"] = [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
                        "phase": p.get("status", {}).get("phase"), "deleting": p["metadata"].get("deletionTimestamp"),
                        "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", [])),
                        "images": [c.get("imageID") for c in p.get("status", {}).get("containerStatuses", []) if c["name"] == "mcp"]} for p in pods]
        row["image_digests"] = sorted({image for p in row["pods"] for image in p["images"] if image})
        row["revision"] = (", ".join(t.get("revisionName", "unknown") for t in row.get("traffic", []) if t.get("percent")) if backend == "knative"
                           else resource["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision"))
        history = [r for r in resources if r["kind"] in {"Revision", "ReplicaSet"} and r["metadata"].get("labels", {}).get(selector) == name]
        row["history"] = sorted([{"name": r["metadata"]["name"], "kind": r["kind"], "created_at": r["metadata"]["creationTimestamp"],
            "image": (r["spec"]["containers"] if r["kind"] == "Revision" else r["spec"]["template"]["spec"]["containers"])[0]["image"],
            "revision": r["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision"),
            "release": r["metadata"].get("annotations", {}).get("deployment.mcp.io/version")} for r in history], key=lambda r: r["created_at"], reverse=True)
        successful = next((j for j in jobs if job_matches_runtime(j, row)), None)
        rollout = next((c.get("lastUpdateTime") or c.get("lastTransitionTime") for c in row["conditions"] if
                        c["type"] == ("RoutesReady" if backend == "knative" else "Progressing") and c["status"] == "True"), None)
        row.update(last_deployed_at=successful["finished_at"] if successful else rollout,
                   deployment_time_source="UI job finished after CLI verified health" if successful else "Kubernetes rollout/route condition time" if rollout else "Not recorded")

    def events(self, row):
        if not row["exists"]:
            return []
        allowed = {row["name"], *(p["name"] for p in row["pods"]), *(r["name"] for r in row["history"])}
        raw = json.loads(self.kubectl(row["namespace"], ["get", "events", "-o", "json"]))
        events = [{"time": e.get("lastTimestamp") or e.get("eventTime") or e["metadata"]["creationTimestamp"],
                   "object": e.get("involvedObject", {}).get("name"), "reason": e.get("reason"), "type": e.get("type"),
                   "count": e.get("count", 1), "message": redact(e.get("message", ""))} for e in raw["items"] if e.get("involvedObject", {}).get("name") in allowed]
        return sorted(events, key=lambda e: e["time"], reverse=True)[:30]

    async def inventory(self, force=False):
        async with self.cache_lock:
            if force or self.cache is None or time.monotonic() - self.cached_at > 5:
                self.cache = await asyncio.to_thread(self.read_inventory)
                self.cached_at = time.monotonic()
            return self.cache

    async def deployment(self, namespace, name):
        if namespace not in NAMESPACES.values() or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
            raise HTTPException(404, "Deployment not found")
        inventory = await self.inventory()
        row = next((r for r in inventory["items"] if r["namespace"] == namespace and r["name"] == name), None)
        if not row:
            if inventory["errors"]:
                raise HTTPException(503, "Cannot confirm deployment status: " + str(inventory["errors"]))
            raise HTTPException(404, "Deployment not found")
        return row, inventory["observed_at"]

    def persist(self, job):
        with self.job_guard:
            path = self.jobs_dir / (job["id"] + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(job, indent=2), encoding="utf-8")
            temporary.replace(path)

    def transition(self, job, stage, message):
        if stage not in STAGES:
            raise ValueError("Unknown deployment stage")
        with self.job_guard:
            message = redact(message)[:1000]
            if job.get("stage") == stage and job.get("stage_message") == message:
                return
            event = {"stage": stage, "at": now(), "message": message}
            job.update(stage=stage, stage_at=event["at"], stage_message=message)
            history = job.setdefault("stage_history", [])
            history.append(event)
            del history[:-128]
            self.persist(job)

    async def register(self, identifier, payload):
        if not payload.trusted_code:
            raise HTTPException(422, "Confirm that you trust this source code before registering a build")
        if payload.port != 8000 or payload.mcp_path != "/mcp":
            raise HTTPException(422, "The existing pipeline supports port 8000 and /mcp only; configure the application accordingly")
        async with self.registration_lock:
            stage = self.sources.get(identifier)
            if identifier in self.sources.registered:
                raise HTTPException(409, "This import is already registered")
            name = payload.server_name
            reserved = {p.get("server_name") for p in self.projects.values()}
            for namespace in NAMESPACES.values():
                raw = await asyncio.to_thread(self.kubectl, namespace, ["get", "deployments,services,services.serving.knative.dev", "-o", "json"])
                reserved.update(r["metadata"]["name"] for r in json.loads(raw)["items"])
            if name in reserved or (payload.mode == "stateful" and name + "-redis" in reserved):
                raise HTTPException(409, "Server name or its Redis name is already reserved in this workspace; choose a unique name")
            folder = self.sources.folder / identifier / "project"
            deployctl.project_path(folder, payload.dockerfile)
            deployctl.project_path(folder, payload.build_context, directory=True)
            env = dict(stage["initial"].get("env", {}))
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                raise ValueError("Project env must contain strings")
            if any(re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|API_KEY", k, re.I) for k in env):
                raise ValueError("Credential environment values are unsupported in local imports; remove them before uploading")
            env.pop("REDIS_URL", None)
            if payload.redis_url:
                from urllib.parse import urlsplit
                url = urlsplit(payload.redis_url)
                if payload.mode != "stateful" or url.scheme not in {"redis", "rediss"} or not url.hostname or url.username or url.password:
                    raise ValueError("Redis URL requires stateful mode, redis(s) scheme and no inline credentials")
                env["REDIS_URL"] = payload.redis_url
            policy = None
            if payload.safe_tool:
                if not payload.confirm_read_only or payload.safe_tool.casefold() == "increment_counter":
                    raise ValueError("Test MCP requires an explicitly reviewed read-only tool; increment_counter is forbidden")
                arguments = json.loads(payload.safe_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Test MCP arguments must be a JSON object")
                policy = {"tool": payload.safe_tool, "arguments": arguments}
            pid = "byo-" + identifier[:20]
            cfg = {"name": name, "mode": payload.mode, "port": 8000, "mcp_path": "/mcp", "replicas": 2, "env": env,
                   "build": {"context": payload.build_context, "dockerfile": payload.dockerfile, "bounded": True},
                   "platform_metadata": {"project_id": pid, "category": "developer"},
                   "resources": {"requests": {"cpu": "50m", "memory": "96Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                   "autoscaling": {"enabled": payload.backend == "knative"}}
            if payload.mode == "stateful":
                cfg["state_backend"] = "redis"
            if payload.backend == "knative":
                cfg["autoscaling"].update(minReplicas=0, activationReplicas=2, maxReplicas=4, targetConcurrency=2,
                                          windowSeconds=20, routeDomain="mcp.test")
            filename = ".deploy-" + payload.backend + ".json"
            atomic_json(folder / filename, cfg)
            deployctl.manifest(folder, filename)
            project = {"id": pid, "title": name, "server_name": name, "backend": payload.backend, "mode": payload.mode,
                       "namespace": NAMESPACES[payload.backend], "category": "developer", "source": stage["source"],
                       "path": folder.relative_to(ROOT).as_posix(), "configs": {payload.backend: filename},
                       "safe_test": policy, "default_version": payload.version, "registered_at": now()}
            self.sources.register(identifier, project)
            self.projects[pid] = {**project, "folder": folder.resolve()}
            self.cached_at = 0
            return project

    async def enqueue(self, payload):
        if not self.accepting:
            raise HTTPException(503, "UI is shutting down")
        project, cfg, config_path = self.config(payload.project_id, payload.backend)
        namespace = NAMESPACES[payload.backend]
        if not self.python.is_file():
            raise HTTPException(503, "Deployment Python is missing; configure MCP_UI_DEPLOY_PYTHON")
        image = payload.image.strip()
        if payload.source == "build" and image:
            raise HTTPException(422, "Choose image reuse or build; not both")
        if payload.source == "reuse" and not image:
            inventory = await self.inventory()
            choices = [r for r in inventory["items"] if r["name"] == cfg["name"]]
            choices.sort(key=lambda r: r["namespace"] != namespace)
            if not choices:
                raise HTTPException(422, "No deployed image to reuse. Choose Build and publish, or provide a published image.")
            image = choices[0]["image"]
        if image:
            prefix = self.registry + "/" + cfg["name"]
            if not re.fullmatch(re.escape(prefix) + r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})", image):
                raise HTTPException(422, "Image must be a tag or digest in this project's configured Floci ECR repository")
            if payload.backend == "knative" and "@sha256:" not in image:
                raise HTTPException(422, "Use a published image digest for local Knative, or choose Build and publish; controller-side localhost tag resolution is not configured")
        command = [str(self.python), str(ROOT / "deployctl.py"), "deploy", str(project["folder"]),
                   "--config", str(config_path), "--namespace", namespace, "--kubeconfig", str(self.kubeconfig),
                   "--version", payload.version, "--timeout", str(payload.timeout_seconds), "--rollback-timeout", "180"]
        command += ["--image", image] if payload.source == "reuse" else ["--push-registry", self.registry]
        if project.get("server_name") and payload.source == "build":
            command += ["--ensure-floci-repository"]
        job = {"id": uuid4().hex, "status": "queued", "project_id": payload.project_id, "name": cfg["name"],
               "namespace": namespace, "backend": payload.backend, "mode": cfg["mode"], "version": payload.version,
               "source": payload.source, "created_at": now(), "started_at": None, "finished_at": None,
               "exit_code": None, "error": None, "command": command}
        job.update(stage="QUEUED", stage_at=job["created_at"], stage_message="Waiting for a worker and resource ownership",
                   stage_history=[{"stage": "QUEUED", "at": job["created_at"], "message": "Job accepted"}],
                   build_limits={"cpu": 1, "memory_bytes": 1073741824, "build_timeout_seconds": 300},
                   log_policy={"segment_bytes": self.log_bytes, "segments": 3, "browser_tail_bytes": 256000})
        keys = [namespace + "/" + cfg["name"]]
        if cfg["mode"] == "stateful" and "REDIS_URL" not in cfg["env"]:
            keys.append(namespace + "/" + cfg["redis"]["name"])
        job["resource_keys"] = keys
        with self.job_guard:
            self.jobs[job["id"]] = job
            self.job_inputs[job["id"]] = {"folder": project["folder"], "cfg": copy.deepcopy(cfg)}
            self.persist(job)
        try:
            await self.queue.put(job, keys)
        except asyncio.QueueFull:
            with self.job_guard:
                self.jobs.pop(job["id"], None)
                self.job_inputs.pop(job["id"], None)
                (self.jobs_dir / (job["id"] + ".json")).unlink(missing_ok=True)
            raise HTTPException(429, "Local deployment queue is full; wait for a job to finish")
        return job

    def execute(self, job):
        inputs = self.job_inputs[job["id"]]
        cfg = inputs["cfg"]
        secret_key = re.compile(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|API_KEY|ACCESS_KEY|PRIVATE_KEY", re.I)
        hidden = [value for values in (os.environ, cfg.get("env", {})) for key, value in values.items() if secret_key.search(key)]
        log = BoundedLog(self.jobs_dir / (job["id"] + ".log"), self.log_bytes,
                         guard=self.job_guard, redactor=LogRedactor(hidden))
        self.logs[job["id"]] = log
        work = self.work_dir / job["id"]
        with self.job_guard:
            job.update(status="running", started_at=now())
            self.persist(job)
        self.transition(job, "BUILDING" if job["source"] == "build" else "DEPLOYING", "Preparing a private source/configuration snapshot")
        log.append("Worker claimed job " + job["id"] + "; held resources: " + ", ".join(job["resource_keys"]))
        try:
            work.mkdir(exist_ok=False)
            snapshot = snapshot_project(inputs["folder"], work / "source")
            cfg.setdefault("build", {})["bounded"] = True
            # Runtime environment values must not become part of Docker's context.
            atomic_json(work / ".job-deploy.json", cfg)
            temporary = work / "tmp"
            temporary.mkdir()
            command = list(job["command"])
            command[3] = str(work / "source")
            command[command.index("--config") + 1] = str(work / ".job-deploy.json")
            with self.job_guard:
                job.update(command=command, source_snapshot=snapshot, context_path=str(work / "source"))
                self.persist(job)
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", MCP_DEPLOY_JOB_ID=job["id"],
                       TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
            result = {}
            def received(record):
                if record.get("name") == job["name"] and record.get("namespace") == job["namespace"]:
                    result.update({k: v for k, v in record.items() if k in {"status", "name", "namespace", "image", "revision", "mcp_url", "ready_replicas", "backend"}})
            def pid(value):
                with self.job_guard:
                    job["pid"] = value
                    self.persist(job)
            code = run_pipeline(command, cwd=ROOT, env=env, log=log, on_stage=lambda stage, message: self.transition(job, stage, message),
                                on_result=received, on_pid=pid)
            if code == 0 and not result:
                raise RuntimeError("CLI exited zero without a verified deployment result; inspect runtime status")
            with self.job_guard:
                job.update(status="succeeded" if code == 0 else "failed", exit_code=code,
                           error=None if code == 0 else f"deployctl.py exited {code}. See the timestamped job log and recovery diagnostics.")
                if code == 0 and result:
                    job["deployment_result"] = result
            self.transition(job, "SUCCEEDED" if code == 0 else "FAILED", "CLI completed successfully" if code == 0 else job["error"])
            log.append(job["stage"] + ": " + job["stage_message"])
        except Exception as exc:
            with self.job_guard:
                job.update(status="failed", error=redact(f"{type(exc).__name__}: {exc}"))
            self.transition(job, "FAILED", job["error"])
            log.append("FAILED: " + job["error"])
        finally:
            try:
                if work.exists():
                    if work.resolve().parent != self.work_dir.resolve() or not re.fullmatch(r"[0-9a-f]{32}", work.name):
                        raise RuntimeError("Refusing build cleanup outside the job work directory")
                    shutil.rmtree(work)
            except OSError as exc:
                log.append("Build snapshot cleanup failed: " + str(exc))
            with self.job_guard:
                job.update(finished_at=now(), log_rotations=log.rotations)
                self.persist(job)
                self.job_inputs.pop(job["id"], None)

    async def worker(self):
        while True:
            job = await self.queue.claim()
            if job is None:
                return
            try:
                await asyncio.to_thread(self.execute, job)
                self.cached_at = 0
            except Exception as exc:
                with self.job_guard:
                    job.update(status="failed", error=redact(str(exc)), finished_at=now())
                    self.transition(job, "FAILED", "Worker error: " + job["error"])
            finally:
                await self.queue.release(job["id"])

    def get_job(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or job_id not in self.jobs:
            raise HTTPException(404, "Job not found")
        with self.job_guard:
            return copy.deepcopy(self.jobs[job_id])

    def job_log(self, job_id):
        self.get_job(job_id)
        path = self.jobs_dir / (job_id + ".log")
        if not path.exists():
            return "Waiting for the deployment worker."
        log = self.logs.get(job_id) or BoundedLog(path, self.log_bytes, guard=self.job_guard)
        return "[Retained job-local log tail; up to 256 KB shown; older output may be rotated]\n" + redact(log.tail())

    def progress(self, job):
        return job.get("stage") or ("FAILED" if job["status"] == "interrupted" else job["status"].upper())


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        platform = Platform()
        app.state.platform = platform
        workers = [asyncio.create_task(platform.worker()) for _ in range(platform.max_jobs)]
        yield
        platform.accepting = False
        await platform.queue.close()
        await asyncio.gather(*workers)  # Drain accepted jobs, including queued resource conflicts.

    app = FastAPI(title="MCP deployment platform", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.mount("/static", StaticFiles(directory=str(ROOT / "ui/static")), name="static")

    @app.middleware("http")
    async def local_only(request, call_next):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        if not local:
            return JSONResponse({"detail": "This demo UI accepts loopback clients only"}, status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin deployment requests are forbidden"}, status_code=403)
            if request.url.path == "/imports":
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_ZIP + 65536:
                        return JSONResponse({"detail": "Import request exceeds the 20 MiB ZIP limit"}, status_code=413)
                    body.extend(chunk)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def context(request, **values):
        return {"request": request, "csrf": request.app.state.platform.csrf, **values}

    def csrf(request, token):
        if not token or not secrets.compare_digest(token, request.app.state.platform.csrf):
            raise HTTPException(403, "Invalid local form token; refresh the page")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        platform = request.app.state.platform
        inventory = await platform.inventory(force=request.query_params.get("refresh") == "1")
        jobs = sorted(platform.jobs.values(), key=lambda j: j["created_at"], reverse=True)[:8]
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html", context=context(request, inventory=inventory, jobs=jobs))

    @app.get("/deploy", response_class=HTMLResponse)
    async def new_deployment(request: Request):
        projects = request.app.state.platform.catalogue()
        return TEMPLATES.TemplateResponse(request=request, name="deploy.html", context=context(request, projects=projects))

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request):
        return TEMPLATES.TemplateResponse(request=request, name="import.html", context=context(request,
            trusted=request.app.state.platform.sources.trusted))

    @app.get("/sample-project.zip")
    async def sample_project():
        buffer = io.BytesIO()
        folder = ROOT / "examples/text-tools-mcp"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("server.py", "Dockerfile", "requirements.txt", ".dockerignore", "README.md"):
                archive.write(folder / name, name)
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="text-tools-mcp.zip"'})

    @app.post("/imports")
    async def import_source(request: Request):
        platform = request.app.state.platform
        form = await request.form(max_files=1, max_fields=10, max_part_size=16384)
        csrf(request, form.get("csrf", ""))
        if form.get("trusted_code") != "yes":
            raise HTTPException(422, "Source import requires a trusted-code acknowledgement")
        if platform.import_lock.locked():
            raise HTTPException(409, "Another source is being staged; wait before importing")
        try:
            async with platform.import_lock:
                kind = form.get("kind")
                upload = form.get("archive")
                data = await upload.read(MAX_ZIP + 1) if kind == "zip" and hasattr(upload, "read") else None
                stage = await asyncio.to_thread(platform.sources.stage, kind, data=data,
                                                url=form.get("repository"), template=form.get("template"))
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
            raise HTTPException(422, "Import failed: " + str(exc)) from exc
        return RedirectResponse("/imports/" + stage["id"], status_code=303)

    @app.get("/imports/{identifier}", response_class=HTMLResponse)
    async def configure_source(request: Request, identifier: str):
        try:
            stage = request.app.state.platform.sources.get(identifier)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        initial = stage["initial"]
        return TEMPLATES.TemplateResponse(request=request, name="configure.html", context=context(request, stage=stage,
            defaults={"name": initial.get("name", ""), "mode": initial.get("mode", ""),
                      "dockerfile": initial.get("build", {}).get("dockerfile", "Dockerfile"),
                      "build_context": initial.get("build", {}).get("context", ".")}))

    @app.post("/imports/{identifier}/configure")
    async def register_source(request: Request, identifier: str):
        form = dict(await request.form(max_files=0, max_fields=20, max_part_size=16384))
        csrf(request, form.pop("csrf", ""))
        try:
            payload = RegistrationRequest.model_validate(form)
            project = await request.app.state.platform.register(identifier, payload)
        except (ValidationError, ValueError, OSError, KeyError) as exc:
            raise HTTPException(422, "Configuration failed: " + str(exc)) from exc
        return RedirectResponse("/deploy?project=" + project["id"], status_code=303)

    @app.post("/deploy")
    async def deploy_form(request: Request):
        form = dict(await request.form())
        csrf(request, form.pop("csrf", ""))
        try:
            payload = DeploymentRequest.model_validate(form)
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        job = await request.app.state.platform.enqueue(payload)
        return RedirectResponse("/jobs/" + job["id"], status_code=303)

    @app.get("/deployments/{namespace}/{name}", response_class=HTMLResponse)
    async def details(request: Request, namespace: str, name: str):
        platform = request.app.state.platform
        if request.query_params.get("refresh") == "1":
            await platform.inventory(force=True)
        row, observed = await platform.deployment(namespace, name)
        jobs = sorted((j for j in platform.jobs.values() if j["name"] == name and j["namespace"] == namespace), key=lambda j: j["created_at"], reverse=True)
        try:
            recent_events = await asyncio.to_thread(platform.events, row)
            events_error = None
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            recent_events, events_error = [], str(exc)
        return TEMPLATES.TemplateResponse(request=request, name="details.html", context=context(request, deployment=row,
            observed_at=observed, jobs=jobs, events=recent_events, events_error=events_error))

    @app.post("/deployments/{namespace}/{name}/redeploy")
    async def redeploy(request: Request, namespace: str, name: str):
        form = await request.form()
        csrf(request, form.get("csrf", ""))
        platform = request.app.state.platform
        row, _ = await platform.deployment(namespace, name)
        if not row["project_id"]:
            raise HTTPException(422, "Project is not in the executable allowlist")
        if platform.config(row["project_id"], row["backend"])[1]["name"] != name or not row["exists"]:
            raise HTTPException(422, "This runtime is not the registered deployment identity; use its registered project form")
        job = await platform.enqueue(DeploymentRequest(project_id=row["project_id"], backend=row["backend"],
                                      source="reuse", image=row["image"], version="ui-redeploy-" + uuid4().hex[:8]))
        return RedirectResponse("/jobs/" + job["id"], status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str):
        platform = request.app.state.platform
        job = platform.get_job(job_id)
        return TEMPLATES.TemplateResponse(request=request, name="job.html", context=context(request, job=job, log=platform.job_log(job_id)))

    @app.get("/api/deployments")
    async def inventory_api(request: Request):
        return await request.app.state.platform.inventory(force=request.query_params.get("refresh") == "1")

    @app.get("/api/projects")
    async def projects_api(request: Request):
        return {"projects": request.app.state.platform.catalogue()}

    @app.post("/api/jobs", status_code=202)
    async def deploy_api(request: Request, payload: DeploymentRequest):
        csrf(request, request.headers.get("x-csrf-token"))
        job = await request.app.state.platform.enqueue(payload)
        return {"job_id": job["id"], "status": job["status"], "stage": job["stage"], "url": "/jobs/" + job["id"]}

    @app.get("/api/jobs/{job_id}")
    async def job_api(request: Request, job_id: str):
        platform = request.app.state.platform
        job = platform.get_job(job_id)
        stage = platform.progress(job)
        return {**job, "stage": stage, "phase": stage, **platform.queue.waiting(job_id), "max_concurrent_jobs": platform.max_jobs,
                "log": await asyncio.to_thread(platform.job_log, job_id)}

    @app.get("/api/deployments/{namespace}/{name}/logs")
    async def app_logs(request: Request, namespace: str, name: str):
        platform = request.app.state.platform
        row, _ = await platform.deployment(namespace, name)
        selector = ("serving.knative.dev/service=" if row["backend"] == "knative" else "app=") + name
        try:
            log = await asyncio.to_thread(platform.kubectl, namespace, ["logs", "-l", selector, "-c", "mcp", "--tail=60", "--prefix=true", "--max-log-requests=10"])
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"log": redact(log) or "kubectl returned no log lines; the application may be idle at zero replicas."}

    @app.post("/api/deployments/{namespace}/{name}/test")
    async def test_mcp(request: Request, namespace: str, name: str):
        csrf(request, request.headers.get("x-csrf-token"))
        platform = request.app.state.platform
        row, _ = await platform.deployment(namespace, name)
        policy = row.get("safe_test")
        if not row["exists"] or not policy or policy["tool"].casefold() == "increment_counter":
            raise HTTPException(422, "No reviewed read-only tool is configured for this runtime")
        if platform.test_lock.locked():
            raise HTTPException(409, "A Test MCP request is already running; wait for its result")
        async with platform.test_lock:
            command = [str(platform.python), str(ROOT / "scripts/ui_mcp_check.py"), "--kubeconfig", str(platform.kubeconfig),
                       "--namespace", namespace, "--name", name, "--backend", row["backend"],
                       "--tool", policy["tool"], "--arguments", json.dumps(policy["arguments"])]
            try:
                result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, capture_output=True, text=True,
                                                 encoding="utf-8", timeout=140, shell=False)
                lines = result.stdout.strip().splitlines()
                check = json.loads(lines[-1]) if lines else {"success": False, "error": "MCP client process returned no result"}
                check.update(observed_at=now(), tool=policy["tool"], identity=row["identity"])
                if check.get("error"):
                    check["error"] = redact(check["error"])
                return check
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return {"success": False, "identity": row["identity"], "tool": policy["tool"],
                        "observed_at": now(), "error": redact(str(exc)), "attempts": 1}

    @app.exception_handler(HTTPException)
    async def error_page(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return TEMPLATES.TemplateResponse(request=request, name="error.html", context=context(request, error=exc.detail), status_code=exc.status_code)

    return app


app = create_app()
