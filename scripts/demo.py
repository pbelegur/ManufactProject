"""Demo orchestration only: reuse deployctl and the existing real MCP verifiers."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from knative_common import Cluster  # noqa: E402

NAMES = ("stateless-demo", "stateful-demo")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Demo:
    def __init__(self, args):
        self.args = args
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.output = Path(args.output_directory or f"evidence/demo/{self.run_id}-{args.mode.lower()}").resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.kubeconfig = str(Path(args.kubeconfig).resolve())
        self.cluster = Cluster(self.kubeconfig, "mcp-demo")
        self.auto = Cluster(self.kubeconfig)
        self.result = {"mode": args.mode, "started_utc": self.run_id, "result": "RUNNING", "steps": [], "commands": []}
        self.env = dict(os.environ, PYTHONUNBUFFERED="1", MCP_EVIDENCE_DIR=str(self.output))
        self.images = {}

    def save(self, filename, value):
        (self.output / filename).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def command(self, command, timeout=600):
        command = [str(c) for c in command]
        print("$ " + subprocess.list2cmdline(command), flush=True)
        started = time.monotonic()
        index = len(self.result["commands"]) + 1
        log = f"command-{index:02d}.log"
        log_path = self.output / log
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdout=output, stderr=subprocess.STDOUT)
            try:
                while True:
                    try:
                        code = process.wait(timeout=20)
                        break
                    except subprocess.TimeoutExpired:
                        elapsed = time.monotonic() - started
                        print(f"  Running ({elapsed:.0f}s); log: {log_path}", flush=True)
                        if elapsed > timeout:
                            raise subprocess.TimeoutExpired(command, timeout)
            except BaseException:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
                else:
                    process.terminate()
                process.wait(timeout=15)
                self.result["commands"].append({"argv": command, "exit_code": process.returncode,
                                               "seconds": round(time.monotonic() - started, 3), "log": log,
                                               "interrupted_or_timed_out": True})
                raise
        stdout = log_path.read_text(encoding="utf-8", errors="replace")
        # Keep the console readable; complete output and exact argv are retained.
        lines = stdout.splitlines()
        print("\n".join(lines[-16:]), flush=True)
        self.result["commands"].append({"argv": command, "exit_code": code,
                                         "seconds": round(time.monotonic() - started, 3), "log": log})
        require(code == 0, f"Command failed ({code}); see {self.output / log}")
        return stdout

    def step(self, name, action):
        print(f"\n--- {name} ---", flush=True)
        started = time.monotonic()
        entry = {"name": name}
        try:
            detail = action()
            entry.update(result="PASS", detail=detail)
            print(f"PASS: {name}", flush=True)
            return detail
        except Exception as exc:
            entry.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
            print(f"FAIL: {name}: {exc}", flush=True)
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            self.result["steps"].append(entry)
            self.save("summary.json", self.result)

    def preflight(self):
        require(Path(self.kubeconfig).is_file(), "Existing isolated kubeconfig is missing; demo does not create infrastructure")
        info = json.loads(self.command(["docker", "info", "--format", '{"NCPU":{{.NCPU}},"MemTotal":{{.MemTotal}}}'], 45))
        nodes = self.cluster.get("nodes")["items"]
        require(nodes and all(any(c["type"] == "Ready" and c["status"] == "True"
                                  for c in n["status"]["conditions"]) for n in nodes), "Kubernetes node is not Ready")
        runtime_format = '{"Id":{{json .Id}},"State":{{json .State}},"HostConfig":{"NanoCpus":{{.HostConfig.NanoCpus}}}}'
        runtime = json.loads(self.command(["docker", "inspect", "--format", runtime_format, "floci-aws-eks-mcp-platform"], 30))
        require(runtime["State"]["Running"], "Existing real-mode EKS Docker runtime is stopped")
        self.runtime_id = runtime["Id"]
        floci_ids = self.command(["docker", "compose", "-f", "compose.floci.yaml", "ps", "-q", "floci"], 30).strip()
        require(floci_ids, "Existing Floci service is not running")
        mode = self.command(["docker", "inspect", "--format", '{{range .Config.Env}}{{if eq . "FLOCI_SERVICES_EKS_MOCK=false"}}real{{end}}{{end}}', floci_ids], 30).strip()
        require(mode == "real", "Floci EKS must be in real mode")
        infra = {}
        for namespace in ("knative-serving", "kourier-system"):
            cluster = Cluster(self.kubeconfig, namespace)
            deployments = cluster.get("deployments")["items"]
            require(deployments, f"Existing {namespace} components missing; demo will not install them")
            for deployment in deployments:
                desired = deployment["spec"].get("replicas", 1)
                require(desired > 0 and deployment.get("status", {}).get("availableReplicas", 0) >= desired,
                        f"Infrastructure unhealthy: {namespace}/{deployment['metadata']['name']}")
            infra[namespace] = {d["metadata"]["name"]: d["status"].get("availableReplicas", 0) for d in deployments}
            hpas = cluster.get("hpa")["items"]
            require(all(h["spec"].get("minReplicas") == h["spec"]["maxReplicas"] == 1 for h in hpas),
                    "Local infrastructure HPA bounds differ from the verified single-node profile")
        for cluster in (self.cluster, self.auto):
            require("PONG" in cluster.run(["exec", "deployment/stateful-demo-redis", "--", "redis-cli", "ping"]),
                    f"Redis unavailable in {cluster.namespace}")
            claims = cluster.get("pvc")["items"]
            require(any(c.get("status", {}).get("phase") == "Bound" for c in claims), "Persistent Redis volume missing")
        for name in NAMES:
            service = self.auto.get(f"ksvc/{name}")
            revision = self.auto.get("revision/" + service["status"]["traffic"][0]["revisionName"])
            self.images[name] = revision["spec"]["containers"][0]["image"]
        self.registry = self.images["stateless-demo"].split("/stateless-demo", 1)[0]
        self.save("initial-deployments.json", self.cluster.get("deployments"))
        self.save("initial-knative-services.json", self.auto.get("ksvc"))
        hardware = {"docker_cpus": info["NCPU"], "docker_memory_bytes": info["MemTotal"],
                    "runtime_cpu_quota": runtime["HostConfig"]["NanoCpus"] / 1e9,
                    "runtime_id": self.runtime_id, "runtime_started": runtime["State"]["StartedAt"],
                    "nodes": [{"name": n["metadata"]["name"], "version": n["status"]["nodeInfo"]["kubeletVersion"],
                               "allocatable": n["status"]["allocatable"]} for n in nodes], "infrastructure": infra}
        self.save("preflight.json", hardware)
        print(json.dumps(hardware, indent=2), flush=True)
        return hardware

    def deploy(self, name, image, version, knative=False, max_ten=False):
        folder = "servers/stateless" if name == "stateless-demo" else "servers/stateful"
        command = [sys.executable, "deployctl.py", "deploy", folder, "--kubeconfig", self.kubeconfig,
                   "--namespace", "mcp-auto" if knative else "mcp-demo", "--image", image, "--version", version]
        command += ["--config", "mcp.knative.json" if max_ten else "mcp.knative.local.json"] if knative else ["--replicas", "2"]
        self.command(command)

    def ensure_always_on(self):
        details = {}
        for name in NAMES:
            deployment = self.cluster.get(f"deployment/{name}")
            desired = deployment["spec"]["replicas"]
            if desired != 2 or deployment.get("status", {}).get("readyReplicas") != 2:
                self.deploy(name, deployment["spec"]["template"]["spec"]["containers"][0]["image"], "demo-reuse")
                deployment = self.cluster.get(f"deployment/{name}")
            slices = self.cluster.get("endpointslices", f"kubernetes.io/service-name={name}")["items"]
            ready = sum(e.get("conditions", {}).get("ready") is True for s in slices for e in s.get("endpoints", []))
            require(deployment["status"].get("readyReplicas") == ready == 2, f"{name} needs 2 ready replicas and endpoints")
            details[name] = {"ready_replicas": 2, "ready_endpoints": ready, "action": "reuse existing deployment" if desired == 2 else "restore two replicas"}
        return details

    def verify_mcp(self, filename):
        self.command([sys.executable, "tests/verify_kubernetes.py", "--kubeconfig", self.kubeconfig,
                      "--output", str(self.output / filename)])
        evidence = json.loads((self.output / filename).read_text(encoding="utf-8"))
        require(evidence["result"] == "PASS", "MCP/Redis verification failed")
        return {"result": evidence["result"], "counter_after_pod_replacement": evidence["counter_after_deletion"], "evidence": filename}

    def rolling_update(self):
        # Two already-published images, never Docker builds. v2 includes the
        # real asynchronous MCP delay argument from Phase 3.
        before = self.cluster.get("deployment/stateless-demo")
        current = before["spec"]["template"]["spec"]["containers"][0]["image"]
        v2 = self.images["stateless-demo"]
        replicasets = self.cluster.get("replicasets", "app=stateless-demo")["items"]
        versions = [r["spec"]["template"]["spec"]["containers"][0]["image"] for r in replicasets]
        # Rollback exercises leave ReplicaSets with deliberately missing tags.
        # Confirm publication rather than trusting history or generating a tag.
        import boto3
        from botocore.config import Config
        ecr = boto3.client("ecr", endpoint_url="http://localhost:4566", region_name="us-east-1",
                           aws_access_key_id="test", aws_secret_access_key="test",
                           config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1}))
        published = ecr.describe_images(repositoryName="stateless-demo")["imageDetails"]
        published_refs = {f"{self.registry}/stateless-demo@{d['imageDigest']}" for d in published}
        published_refs.update(f"{self.registry}/stateless-demo:{tag}" for d in published for tag in d.get("imageTags", []))
        v1 = next((image for image in [current] + versions if image != v2 and image in published_refs), None)
        require(v1 is not None, "Need two existing published stateless image references for the build-free v1/v2 demonstration")
        if current != v1:
            self.deploy("stateless-demo", v1, "demo-v1")
        old = self.cluster.get("deployment/stateless-demo")
        self.deploy("stateless-demo", v2, "demo-v2")
        after = self.cluster.get("deployment/stateless-demo")
        require(after["metadata"]["generation"] > old["metadata"]["generation"], "Pod template did not change")
        require(after["status"].get("readyReplicas") == 2, "v2 not healthy")
        result = {"v1_image": v1, "v2_image": v2, "before_generation": old["metadata"]["generation"],
                  "after_generation": after["metadata"]["generation"], "strategy": after["spec"]["strategy"],
                  "note": "Existing published versions reused; this Quick check does not measure uninterrupted traffic"}
        self.save("rolling-update.json", result)
        return result

    def configure_knative(self, max_ten=False):
        for name in NAMES:
            self.deploy(name, self.images[name], f"demo-{self.run_id}-{'scale' if max_ten else 'continuity'}", True, max_ten)
        return {"min": 0, "activation": 2, "max": 10 if max_ten else 4, "target_concurrency": 2}

    def autoscaling(self):
        self.command([sys.executable, "tests/verify_autoscaling.py", "--kubeconfig", self.kubeconfig], 1100)
        summaries = {}
        for name in NAMES:
            data = json.loads((self.output / (name + "-autoscaling.json")).read_text(encoding="utf-8"))
            latencies = sorted(data["load"]["latency_seconds"])
            summaries[name] = {"requests": data["load"]["successes"] + len(data["load"]["failures"]),
                               "failures": len(data["load"]["failures"]),
                               "peak_ready": max(s["ready"] for s in data["load"]["samples"]),
                               "first_above_two_seconds": data["load"]["scale_up_seconds"],
                               "scale_to_zero_seconds": data["scale_to_zero_seconds"],
                               "cold_start_seconds": data["cold_start_seconds"],
                               "latency_p50_seconds": latencies[len(latencies) // 2],
                               "latency_p95_seconds": latencies[min(len(latencies) - 1, int(len(latencies) * .95))],
                               "persisted_counter": data["cold_start_value"] if name == "stateful-demo" else None}
        print(json.dumps(summaries, indent=2), flush=True)
        return summaries

    def continuity(self):
        try:
            self.command([sys.executable, "tests/verify_knative_reliability.py", "--kubeconfig", self.kubeconfig,
                          "--registry", self.registry, "--reuse-images", "--release", "demo-" + self.run_id], 700)
        finally:
            if (self.output / "reliability.json").exists():
                measured = json.loads((self.output / "reliability.json").read_text(encoding="utf-8"))
                print(json.dumps(measured["summaries"], indent=2), flush=True)
        evidence = json.loads((self.output / "reliability.json").read_text(encoding="utf-8"))
        require(evidence["result"] == "PASS", "Continuity operations failed")
        require(all(s["failures"] == 0 for s in evidence["summaries"].values()), "Traffic failures observed; see reliability.json")
        return {"summaries": evidence["summaries"], "elapsed_seconds": evidence["elapsed_seconds"],
                "note": "Independent existing fixture redeployed without rebuilding; v2, pod deletion and deliberate failed rollout also tested"}

    def final_status(self):
        self.ensure_always_on()
        self.save("final-deployments.json", self.cluster.get("deployments"))
        self.save("final-knative-services.json", self.auto.get("ksvc"))
        runtime_id = self.command(["docker", "inspect", "--format", "{{.Id}}", "floci-aws-eks-mcp-platform"], 30).strip()
        require(runtime_id == self.runtime_id, "Runtime container was replaced during demo")
        return {"runtime_preserved": True, "always_on_replicas": {name: 2 for name in NAMES}}

    def run(self):
        started = time.monotonic()
        try:
            self.step("Existing infrastructure health (read only)", self.preflight)
            self.step("Reuse/deploy two ready replicas and Service endpoints each", self.ensure_always_on)
            self.step("Real MCP echo/add, shared Redis across replicas, pod recovery", lambda: self.verify_mcp("mcp-before.json"))
            self.step("Published v1 to v2 rolling update", self.rolling_update)
            self.step("Real MCP and Redis after rolling update", lambda: self.verify_mcp("mcp-after.json"))
            if self.args.mode == "Full":
                require(self.result["steps"][0]["detail"]["docker_memory_bytes"] >= 6 * 1024**3,
                        "Full mode needs the previously verified local memory budget (at least 6 GiB Docker RAM)")
                self.step("Reuse published images with Knative max-ten profiles", lambda: self.configure_knative(True))
                self.step("Automatic scale above two, zero, cold MCP wake and Redis persistence", self.autoscaling)
                self.step("Restore bounded max-four profiles for simultaneous traffic", self.configure_knative)
                self.step("Existing traffic continuity, independent deploy, v2, pod loss and rollback", self.continuity)
            self.result["result"] = "PASS"
        except Exception as exc:
            self.result.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
            (self.output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            if hasattr(self, "runtime_id"):
                try:
                    self.step("Final health and preserved environment", self.final_status)
                except Exception as exc:
                    self.result.update(result="FAIL", final_health_error=f"{type(exc).__name__}: {exc}")
            self.result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.result["finished_utc"] = datetime.now(timezone.utc).isoformat()
            self.save("summary.json", self.result)
            print(f"\n{self.args.mode}: {self.result['result']} ({self.result['elapsed_seconds']}s)", flush=True)
            for step in self.result["steps"]:
                print(f"  {step['result']}: {step['name']} ({step['seconds']}s)", flush=True)
            print(f"Evidence: {self.output}", flush=True)
        return 0 if self.result["result"] == "PASS" else 1


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("Quick", "Full"), default="Quick")
    parser.add_argument("--kubeconfig", default=".local/floci.kubeconfig")
    parser.add_argument("--output-directory")
    sys.exit(Demo(parser.parse_args()).run())
