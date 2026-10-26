"""Measure steady MCP traffic during independent deploys, v2 rollout, pod loss and rollback."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time
import traceback
from uuid import uuid4

from knative_common import Cluster, call, save

ROOT = Path(__file__).resolve().parents[1]


async def main(args, cluster, gateway):
    hosts = {name: cluster.get(f"ksvc/{name}")["status"]["url"].split("://")[1]
             for name in ("stateless-demo", "stateful-demo")}
    counter = "reliability_" + uuid4().hex
    value, _ = await call(gateway, hosts["stateful-demo"], "increment_counter", {"name": counter, "amount": 17})
    assert value == 17
    assert (await call(gateway, hosts["stateless-demo"], "echo", {"message": "warm"}))[0] == "warm"
    started = time.monotonic()
    phase = "baseline"
    running = True
    requests = []
    operations = []

    async def request(name):
        sent = time.monotonic() - started
        current_phase = phase
        begin = time.perf_counter()
        try:
            if name == "stateless-demo":
                result, latency = await call(gateway, hosts[name], "echo", {"message": "continuous", "delay_seconds": 0.6})
                assert result == "continuous"
            else:
                result, latency = await call(gateway, hosts[name], "get_counter", {"name": counter})
                assert result == 17
            requests.append({"service": name, "phase": current_phase, "sent_seconds": sent,
                             "latency_seconds": latency, "success": True})
        except Exception as exc:
            requests.append({"service": name, "phase": current_phase, "sent_seconds": sent,
                             "latency_seconds": time.perf_counter() - begin, "success": False,
                             "error": f"{type(exc).__name__}: {exc}",
                             "traceback": "".join(traceback.format_exception(exc))})

    async def traffic(name):
        pending = set()
        next_request = time.monotonic()
        while running:
            task = asyncio.create_task(request(name))
            pending.add(task)
            task.add_done_callback(pending.discard)
            next_request += 1 / 2.5
            await asyncio.sleep(max(0, next_request - time.monotonic()))
        await asyncio.gather(*pending)

    async def deploy(folder, version, image=None, timeout=180, config=None):
        command = [sys.executable, str(ROOT / "deployctl.py"), "deploy", str(ROOT / folder),
                   "--config", config or args.config, "--namespace", cluster.namespace,
                   "--kubeconfig", cluster.kubeconfig, "--version", version, "--timeout", str(timeout)]
        reused_image = None
        if image is None and args.reuse_images:
            name = "stateless-demo" if folder == "servers/stateless" else args.independent_name
            existing = await asyncio.to_thread(cluster.get, f"ksvc/{name}")
            reused_image = existing["spec"]["template"]["spec"]["containers"][0]["image"]
        published_image = image or reused_image
        command += ["--image", published_image] if published_image else ["--push-registry", args.registry]
        path = Path(".local") / f"knative-{version}.log"
        print("$", " ".join(command), flush=True)
        start = time.monotonic()
        with path.open("w", encoding="utf-8") as log:
            process = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=asyncio.subprocess.STDOUT)
            code = await asyncio.wait_for(process.wait(), timeout=420)
        operation = {"phase": phase, "command": command, "exit_code": code,
                     "seconds": time.monotonic() - start, "log": str(path)}
        operations.append(operation)
        print(operation, flush=True)
        if code and image is None:
            raise RuntimeError(path.read_text(encoding="utf-8"))
        return code, path.read_text(encoding="utf-8")

    traffic_tasks = [asyncio.create_task(traffic(name)) for name in hosts]
    error = None
    try:
        await asyncio.sleep(12)
        phase = "independent-service-deploy"
        independent_config = ROOT / "tests/fixtures/compatible_project" / args.config
        if args.independent_name != "contract-probe":
            independent = json.loads(independent_config.read_text(encoding="utf-8"))
            independent["name"] = args.independent_name
            independent_config = ROOT / ".local" / (args.independent_name + ".json")
            independent_config.write_text(json.dumps(independent), encoding="utf-8")
        await deploy("tests/fixtures/compatible_project", args.release + "-independent", config=str(independent_config))
        generic = await asyncio.to_thread(cluster.get, f"ksvc/{args.independent_name}")
        generic_host = generic["status"]["url"].split("://")[1]
        product, _ = await call(gateway, generic_host, "multiply", {"value": 6, "factor": 7})
        assert product == {"product": 42, "label": "configured-by-project"}
        await asyncio.sleep(8)
        phase = "existing-service-v2-rollout"
        before = await asyncio.to_thread(cluster.get, "ksvc/stateless-demo")
        await deploy("servers/stateless", args.release)
        after = await asyncio.to_thread(cluster.get, "ksvc/stateless-demo")
        assert before["status"]["latestReadyRevisionName"] != after["status"]["latestReadyRevisionName"]
        assert after["status"]["traffic"][0]["revisionName"] == after["status"]["latestReadyRevisionName"]
        await asyncio.sleep(8)
        phase = "stateful-pod-deletion"
        pods = (await asyncio.to_thread(cluster.get, "pods", "serving.knative.dev/service=stateful-demo"))["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        victim = live[0]["metadata"]["name"]
        await asyncio.to_thread(cluster.run, ["delete", "pod", victim, "--wait=true", "--timeout=60s"])
        value, _ = await call(gateway, hosts["stateful-demo"], "get_counter", {"name": counter})
        assert value == 17
        operations.append({"phase": phase, "deleted_pod": victim, "counter_value_after": value})
        await asyncio.sleep(12)
        phase = "failed-rollout-recovery"
        before_failure = await asyncio.to_thread(cluster.get, "ksvc/stateless-demo")
        traffic_before = before_failure["status"]["traffic"]
        missing = args.registry + "/stateless-demo@sha256:" + "0" * 64
        code, log = await deploy("servers/stateless", "phase3-intentional-failure", missing, timeout=35)
        assert code == 1 and "ROLLBACK OK: previous healthy Knative route restored" in log, log[-6000:]
        recovered = await asyncio.to_thread(cluster.get, "ksvc/stateless-demo")
        assert recovered["status"]["traffic"][0]["revisionName"] == traffic_before[0]["revisionName"]
        phase = "after-recovery"
        await asyncio.sleep(15)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, flush=True)
    finally:
        running = False
        await asyncio.gather(*traffic_tasks)
        elapsed = time.monotonic() - started
        summaries = {}
        for name in hosts:
            selected = [r for r in requests if r["service"] == name]
            durations = sorted(r["latency_seconds"] for r in selected)
            summaries[name] = {"requests": len(selected), "successes": sum(r["success"] for r in selected),
                               "failures": sum(not r["success"] for r in selected), "scheduled_rps": len(selected) / elapsed,
                               "latency_p50_seconds": statistics.median(durations),
                               "latency_p95_seconds": durations[min(len(durations) - 1, int(len(durations) * .95))],
                               "latency_max_seconds": max(durations)}
        if error is None and any(summary["failures"] for summary in summaries.values()):
            error = "MCP traffic failures observed; see per-request evidence"
        result = {"result": "PASS" if error is None else "FAIL", "error": error, "elapsed_seconds": elapsed,
                  "counter": counter, "summaries": summaries, "operations": operations, "requests": requests}
        save("reliability.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "requests"}, indent=2), flush=True)
    if error:
        raise RuntimeError(error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--config", default="mcp.knative.local.json", help="explicit local max-4 profile by default")
    parser.add_argument("--independent-name", default="contract-probe", help="provision its registry repository first")
    parser.add_argument("--release", default="phase3-v2")
    parser.add_argument("--reuse-images", action="store_true", help="reuse existing published service images; skip Docker builds")
    args = parser.parse_args()
    cluster = Cluster(args.kubeconfig)
    with cluster.gateway() as gateway:
        asyncio.run(main(args, cluster, gateway))
