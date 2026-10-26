"""Opt-in live UI build/rollout test; uses existing projects and infrastructure.

Five successful builds (three independent, two conflicting) and one intentional
invalid Dockerfile. Real read-only MCP traffic uses ClusterIP routing in a small
temporary client pod. No request/deployment retries and no Full demo.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def utc():
    return datetime.now(timezone.utc).isoformat()


def statistics(records):
    good = sorted(r["ms"] for r in records if r["success"])
    return {"requests": len(records), "successes": len(good), "failures": len(records) - len(good),
            "p95_ms": good[math.ceil(.95 * len(good)) - 1] if good else None,
            "max_ms": max(good) if good else None}


async def traffic():
    from mcp import Client
    from validate_final_performance import value
    records = []
    ready = set()
    deadline = time.monotonic() + 1600

    async def stream(name, tool, arguments, expected):
        while time.monotonic() < deadline and not Path("/tmp/stop-traffic").exists():
            start = time.perf_counter()
            record = {"utc": utc(), "server": name, "tool": tool,
                      "phase": "measured" if Path("/tmp/traffic-ready").exists() else "warmup"}
            try:
                async with Client(f"http://{name}:8000/mcp", read_timeout_seconds=30) as client:
                    answer = value(await client.call_tool(tool, arguments))
                    assert answer == expected, (answer, expected)
                record.update(success=True, value=answer)
                ready.add(name)
                if len(ready) == 2:
                    Path("/tmp/traffic-ready").touch()
            except Exception as exc:
                record.update(success=False, error=repr(exc), traceback=traceback.format_exc())
            record["ms"] = (time.perf_counter() - start) * 1000
            records.append(record)
            print(json.dumps(record), flush=True)
            # At most 2 RPS per service; no catch-up bursts and no failed-call retry.
            await asyncio.sleep(max(0, .5 - (time.perf_counter() - start)))

    try:
        await asyncio.gather(stream("stateless-demo", "echo", {"message": "parallel-build-continuity"}, "parallel-build-continuity"),
                             stream("stateful-demo", "get_counter", {"name": "ui_persist_a59837a6a3"}, 13))
    finally:
        Path("/tmp/traffic.json").write_text(json.dumps({"records": records, "summary": statistics(records)}, indent=2))


async def run(args):
    folder = ROOT / "evidence/concurrency" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    print("Evidence:", folder, flush=True)
    commands, observation, jobs, results = [], [], [], {"started_at": utc(), "result": "RUNNING"}
    prefix = ["kubectl", "--kubeconfig", str(Path(args.kubeconfig).resolve()), "--request-timeout=20s", "-n", "mcp-demo"]
    identifier = "concurrency-client-" + uuid4().hex[:10]
    process = output = None
    csrf = ""

    def save(name, value):
        (folder / name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def command(argv, *, check=True, timeout=45):
        started = utc()
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        commands.append({"at": started, "argv": argv, "exit_code": result.returncode})
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    async def k(*argv, **kwargs):
        return await asyncio.to_thread(command, prefix + list(argv), **kwargs)

    async def api(path, payload=None):
        def request():
            data = json.dumps(payload).encode() if payload is not None else None
            req = Request(args.base.rstrip("/") + path, data=data,
                          headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
            with urlopen(req, timeout=60) as response:
                raw = response.read().decode()
                return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
        return await asyncio.to_thread(request)

    async def snapshot(label):
        data = json.loads(await k("get", "deployments,pods,services,endpointslices,pvc", "-o", "json"))
        save(label + ".json", data)
        save(label + "-ui.json", await api("/api/deployments?refresh=1"))
        return data

    async def submit(project, release, role):
        submitted = time.perf_counter()
        answer = await api("/api/jobs", {"project_id": project, "backend": "always-on", "source": "build",
                                         "version": release, "timeout_seconds": 180})
        item = {"id": answer["job_id"], "role": role, "accepted_at": utc(),
                "acceptance_ms": (time.perf_counter() - submitted) * 1000}
        jobs.append(item)
        print("Accepted", role, item["id"], flush=True)
        return item

    try:
        page = await api("/deploy")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        projects = (await api("/api/projects"))["projects"]
        by_name = {p["profiles"]["always-on"]["name"]: p["id"] for p in projects if "always-on" in p["profiles"]}
        for name in ["stateless-demo", "stateful-demo", "manufact-text-tools", "manufact-invalid-build"]:
            assert name in by_name, f"Register the existing trusted demo source first: {name}"
        before = await snapshot("before")
        save("docker-before.json", [json.loads(line) for line in (await asyncio.to_thread(command,
             ["docker", "stats", "--no-stream", "--format", "{{json .}}"])).splitlines() if line.strip()])
        image = next(d for d in before["items"] if d["kind"] == "Deployment" and d["metadata"]["name"] == "stateful-demo")["spec"]["template"]["spec"]["containers"][0]["image"]
        metadata = {"name": identifier, "namespace": "mcp-demo"}
        config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {
            "worker.py": Path(__file__).read_text(encoding="utf-8"),
            "validate_final_performance.py": (ROOT / "tests/validate_final_performance.py").read_text(encoding="utf-8")}}
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "client", "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-c", "import time; time.sleep(2400)"],
                "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [{"name": "scripts", "mountPath": "/validation", "readOnly": True}]}],
            "volumes": [{"name": "scripts", "configMap": {"name": identifier}}]}}
        save("client-resources.json", {"apiVersion": "v1", "kind": "List", "items": [config, pod]})
        await k("apply", "-f", str(folder / "client-resources.json"))
        await k("wait", "--for=condition=Ready", "pod/" + identifier, "--timeout=90s", timeout=100)
        output = (folder / "traffic-stdout.log").open("w", encoding="utf-8")
        argv = prefix + ["exec", identifier, "--", "python", "-u", "/validation/worker.py", "--worker"]
        commands.append({"at": utc(), "argv": argv})
        process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while not (await k("exec", identifier, "--", "python", "-c", "from pathlib import Path; print(Path('/tmp/traffic-ready').exists())")).strip() == "True":
            assert process.poll() is None and time.monotonic() < deadline, "Read-only MCP traffic preflight failed; see traffic-stdout.log"
            await asyncio.sleep(1)
        await asyncio.sleep(8)
        results["build_window_start"] = utc()
        release = "parallel-" + datetime.now(timezone.utc).strftime("%H%M%S")
        await asyncio.gather(*(submit(by_name[name], release + "-" + str(i), "independent-" + name)
                              for i, name in enumerate(["stateless-demo", "stateful-demo", "manufact-text-tools"])))
        # Separate submissions preserve the intended same-target FIFO order.
        await submit(by_name["stateless-demo"], release + "-same1", "conflict-first")
        await submit(by_name["stateless-demo"], release + "-same2", "conflict-second")
        await submit(by_name["manufact-invalid-build"], release + "-broken", "expected-failure")
        deadline = time.monotonic() + 1400
        inspected, finished, peak, previous = set(), set(), 0, None
        while len(finished) < len(jobs):
            assert time.monotonic() < deadline, "Jobs did not finish; inspect live job pages before retrying"
            current = await asyncio.gather(*(api("/api/jobs/" + j["id"]) for j in jobs))
            active = [j for j in current if j["status"] == "running"]
            peak = max(peak, len(active))
            state = {j["id"]: j["stage"] for j in current}
            observation.append({"at": utc(), "jobs": [{k: j.get(k) for k in ["id", "stage", "status", "queue_position", "waiting_reason"]} for j in current]})
            if state != previous:
                print("Stages", ", ".join(j["name"] + ":" + j["stage"] for j in current), flush=True)
                previous = state
            assert len(active) <= 3 and len({j["name"] for j in active}) == len(active), "Worker/resource concurrency violation"
            for job in current:
                if job["id"] not in finished and job.get("finished_at"):
                    finished.add(job["id"])
                    save(job["id"] + ".json", {k: v for k, v in job.items() if k != "log"})
                    (folder / (job["id"] + ".log")).write_text(job["log"], encoding="utf-8")
                for container in re.findall(r"Running in ([0-9a-f]{12,64})", job["log"]):
                    if container in inspected:
                        continue
                    inspected.add(container)
                    raw = await asyncio.to_thread(command, ["docker", "inspect", container], check=False)
                    if raw.strip() and raw.strip() != "[]":
                        host = json.loads(raw)[0]["HostConfig"]
                        results.setdefault("observed_build_limits", []).append({k: host.get(k) for k in ["Memory", "MemorySwap", "CpuPeriod", "CpuQuota", "Privileged", "Binds"]})
            await asyncio.sleep(1)
        results.update(build_window_end=utc(), peak_running_jobs=peak)
        completed = {j["id"]: await api("/api/jobs/" + j["id"]) for j in jobs}
        results["jobs"] = [{**j, **{k: v for k, v in completed[j["id"]].items() if k != "log"}} for j in jobs]
        # Keep measuring through old pods' 30-second graceful termination.
        print("Jobs finished; observing another 45 seconds of traffic/draining", flush=True)
        await asyncio.sleep(45)
        after = await snapshot("after")
        for kind in ["Service", "PersistentVolumeClaim"]:
            old = {r["metadata"]["name"]: r["metadata"]["uid"] for r in before["items"] if r["kind"] == kind}
            new = {r["metadata"]["name"]: r["metadata"]["uid"] for r in after["items"] if r["kind"] == kind}
            assert old == new, f"Existing {kind} identities changed"
        for name in ["stateless-demo", "stateful-demo", "manufact-text-tools", "contract-probe"]:
            deployment = next(d for d in after["items"] if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
            assert deployment["status"]["readyReplicas"] == deployment["status"]["updatedReplicas"] == deployment["spec"]["replicas"] == 2
        for item in results["jobs"]:
            assert item["status"] == ("failed" if item["role"] == "expected-failure" else "succeeded"), item
        same = [next(j for j in results["jobs"] if j["role"] == role) for role in ["independent-stateless-demo", "conflict-first", "conflict-second"]]
        assert all(b["started_at"] >= a["finished_at"] for a, b in zip(same, same[1:])), "Conflicting updates overlapped"
        assert peak == 3, "Did not observe three concurrent jobs; check MCP_UI_MAX_JOBS=3"
        assert len({j["deployment_result"]["image"] for j in results["jobs"] if j["status"] == "succeeded"}) == 5
        results["result"] = "PASS"
    except Exception as exc:
        results.update(result="FAIL", error=repr(exc), traceback=traceback.format_exc())
        print("FAIL:", repr(exc), flush=True)
    finally:
        if process is not None:
            try:
                await k("exec", identifier, "--", "python", "-c", "from pathlib import Path; Path('/tmp/stop-traffic').touch()")
                await asyncio.to_thread(process.wait, timeout=65)
                measured = json.loads(await k("exec", identifier, "--", "cat", "/tmp/traffic.json"))
                save("traffic.json", measured)
                records = [r for r in measured["records"] if r["phase"] == "measured"]
                results["traffic"] = statistics(records)
                results["traffic_by_server"] = {name: statistics([r for r in records if r["server"] == name])
                    for name in ["stateless-demo", "stateful-demo"]}
                results["during_builds_and_rollouts"] = statistics([r for r in records if results.get("build_window_start", "z") <= r["utc"] <= results.get("build_window_end", "z")])
                if not records or any(not r["success"] for r in measured["records"]):
                    results.update(result="FAIL", traffic_error="MCP failures retained without retries")
            except Exception as exc:
                results.update(result="FAIL", traffic_collection_error=repr(exc))
            if process.poll() is None:
                process.terminate()  # Only this test's kubectl exec client, never the API/pipeline.
        if output:
            output.close()
        await k("delete", "pod,configmap", identifier, "--ignore-not-found=true", "--wait=false", check=False)
        results["finished_at"] = utc()
        save("observations.json", observation)
        save("commands.json", commands)
        save("summary.json", results)
        print(json.dumps({k: results.get(k) for k in ["result", "peak_running_jobs", "traffic", "during_builds_and_rollouts", "error"]}, indent=2), flush=True)
    return results["result"] == "PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--kubeconfig", default=".local/floci.kubeconfig")
    options = parser.parse_args()
    if options.worker:
        asyncio.run(traffic())
    else:
        if not options.base.startswith("http://127.0.0.1:"):
            parser.error("The demo targets only a localhost UI")
        sys.exit(0 if asyncio.run(run(options)) else 1)
