"""Bounded, opt-in measurements against the EXISTING local cluster.

No image builds, infrastructure changes, application patches or request retries.
The temporary client pod uses an already published image and is removed afterward.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
import traceback
from urllib.request import urlopen
from uuid import uuid4


def utc():
    return datetime.now(timezone.utc).isoformat()


def summary(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": statistics.median(values),
            "p95_ms": values[math.ceil(.95 * len(values)) - 1],
            "p99_ms": values[math.ceil(.99 * len(values)) - 1], "max_ms": max(values)}


def value(result):
    if result.is_error:
        raise AssertionError(str(result))
    answer = result.structured_content
    if answer is None:
        answer = json.loads(next(c.text for c in result.content if hasattr(c, "text")))
    return answer["result"] if isinstance(answer, dict) and set(answer) == {"result"} else answer


def cpu_stat():
    path = Path("/sys/fs/cgroup/cpu.stat")
    return path.read_text() if path.exists() else None


@asynccontextmanager
async def connection(route, output, label):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    start = time.perf_counter()
    http = create_mcp_http_client(headers={"Host": route["host"]}, timeout=httpx2.Timeout(90))
    setup = {"route": route["id"], "label": label, "utc": utc(),
             "factory_ms": (time.perf_counter() - start) * 1000}
    output["connections"].append(setup)

    async def request_hook(request):
        request.headers["X-Request-ID"] = str(uuid4())
        try:
            body = json.loads(request.content) if request.method == "POST" else {}
        except (ValueError, RuntimeError):
            body = {}
        record = {"route": route["id"], "label": label, "utc": utc(),
                  "request_id": request.headers["X-Request-ID"],
                  "http_method": request.method, "rpc_method": body.get("method"),
                  "tool": body.get("params", {}).get("name"), "path": request.url.path}
        request.extensions["perf_start"] = time.perf_counter()
        request.extensions["perf_record"] = record
        output["http"].append(record)

    async def response_hook(response):
        record = response.request.extensions["perf_record"]
        record.update(status=response.status_code,
                      headers_ms=(time.perf_counter() - response.request.extensions["perf_start"]) * 1000,
                      response_request_id=response.headers.get("x-request-id"))
        # Our servers return finite JSON, not event streams. Do not consume GET SSE.
        if "application/json" in response.headers.get("content-type", "") or response.status_code >= 400:
            await response.aread()
            record["body_complete_ms"] = (time.perf_counter() - response.request.extensions["perf_start"]) * 1000
            if response.status_code >= 400:
                record["error_body"] = response.text[:4096]

    http.event_hooks["request"].append(request_hook)
    http.event_hooks["response"].append(response_hook)
    async with http:
        client = Client(streamable_http_client(route["url"], http_client=http), read_timeout_seconds=90)
        begin = time.perf_counter()
        async with client:
            setup["mcp_initialize_ms"] = (time.perf_counter() - begin) * 1000
            setup["protocol_version"] = client.protocol_version
            try:
                yield client
            finally:
                begin = time.perf_counter()
        setup["mcp_teardown_ms"] = (time.perf_counter() - begin) * 1000
    setup["connection_total_ms"] = (time.perf_counter() - start) * 1000


async def invoke(client, route, tool, arguments, expected, output, phase):
    record = {"route": route["id"], "tool": tool, "phase": phase, "utc": utc()}
    begin = time.perf_counter()
    try:
        answer = value(await client.call_tool(tool, arguments))
        if expected is not None:
            assert answer == expected, (answer, expected)
        record.update(success=True, value=answer)
    except Exception as exc:
        record.update(success=False, error=repr(exc), traceback="".join(traceback.format_exception(exc)))
    record["ms"] = (time.perf_counter() - begin) * 1000
    output["requests"].append(record)
    return record


async def warm(args):
    routes = json.loads(Path("/validation/routes.json").read_text())
    out = {"started_utc": utc(), "connections": [], "http": [], "requests": [],
           "samples_per_tool_route": args.samples, "cpu_before": cpu_stat(), "counter_checks": []}
    # Hold all six clients; interleave routes/tools to keep applications warm.
    async with AsyncExitStack() as stack:
        clients = {}
        for route in routes:
            clients[route["id"]] = await stack.enter_async_context(connection(route, out, "initial-warmup"))
            tool, arguments, expected = (("echo", {"message": "warm"}, "warm") if route["mode"] == "stateless"
                                         else ("get_counter", {"name": args.counter}, 0))
            await invoke(clients[route["id"]], route, tool, arguments, expected, out, "activation-excluded")
        # Cold auto-discovery can fall back to a legacy handshake. Reconnect
        # after activation so path comparisons use the same negotiated protocol.
        await stack.aclose()
        for route in routes:
            clients[route["id"]] = await stack.enter_async_context(connection(route, out, "warm-pooled"))
        versions = {c["protocol_version"] for c in out["connections"] if c["label"] == "warm-pooled"}
        assert len(versions) == 1, f"Cannot compare mixed warm protocols: {versions}"
        out["warm_protocol_version"] = next(iter(versions))
        randomizer = random.Random(42)
        for iteration in range(args.samples + 5):
            shuffled = list(routes)
            randomizer.shuffle(shuffled)
            for route in shuffled:
                if route["mode"] == "stateless":
                    tools = [("echo", {"message": "no-delay"}, "no-delay"),
                             ("add_numbers", {"a": 19, "b": 23}, 42)]
                else:
                    key = args.counter + "_" + route["id"].replace("-", "_")
                    tools = [("increment_counter", {"name": key, "amount": 1}, iteration + 1),
                             ("get_counter", {"name": key}, iteration + 1)]
                for tool, arguments, expected in tools:
                    await invoke(clients[route["id"]], route, tool, arguments, expected, out,
                                 "warm" if iteration >= 5 else "warmup-excluded")
        # Compare fresh connection setup to pooled call time, separately, without mutations.
        for iteration in range(12):
            for route in routes:
                start = time.perf_counter()
                async with connection(route, out, "fresh-warm") as client:
                    tool, arguments, expected = (("echo", {"message": "fresh"}, "fresh") if route["mode"] == "stateless"
                                                 else ("get_counter", {"name": args.counter}, 0))
                    record = await invoke(client, route, tool, arguments, expected, out, "fresh-warm")
                record["whole_connection_ms"] = (time.perf_counter() - start) * 1000
        for route in routes:
            if route["mode"] == "stateful":
                key = args.counter + "_" + route["id"].replace("-", "_")
                record = await invoke(clients[route["id"]], route, "get_counter", {"name": key},
                                      args.samples + 5, out, "final-counter-check")
                out["counter_checks"].append(record)
    out.update(finished_utc=utc(), cpu_after=cpu_stat())
    out["summary"] = []
    for route in routes:
        for tool in (["echo", "add_numbers"] if route["mode"] == "stateless" else ["increment_counter", "get_counter"]):
            records = [r for r in out["requests"] if r["route"] == route["id"] and r["tool"] == tool and r["phase"] == "warm"]
            out["summary"].append({"route": route["id"], "tool": tool,
                                   "attempts": len(records), "failures": sum(not r["success"] for r in records),
                                   **summary([r["ms"] for r in records if r["success"]])})
    Path("/tmp/warm.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out["summary"]), flush=True)
    assert all(r["success"] for r in out["requests"]), "Request failures saved; no retries performed"


async def traffic(args):
    routes = [r for r in json.loads(Path("/validation/routes.json").read_text()) if r["path"] == "knative"]
    out = {"started_utc": utc(), "connections": [], "http": [], "requests": [], "cpu_before": cpu_stat()}
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        clients = {r["id"]: await stack.enter_async_context(connection(r, out, "rollout-pooled")) for r in routes}
        for route in routes:
            tool, arguments, expected = (("echo", {"message": "rollout"}, "rollout") if route["mode"] == "stateless"
                                         else ("get_counter", {"name": args.counter + "_knative_stateful"}, args.samples + 5))
            await invoke(clients[route["id"]], route, tool, arguments, expected, out, "preflight")
        Path("/tmp/traffic-ready").write_text(utc())
        print("TRAFFIC_READY", flush=True)
        async def stream(route):
            tool, arguments, expected = (("echo", {"message": "rollout"}, "rollout") if route["mode"] == "stateless"
                                         else ("get_counter", {"name": args.counter + "_knative_stateful"}, args.samples + 5))
            # One sequential stream per service, at most 2 requests/sec. No catch-up bursts.
            while time.monotonic() - started < args.seconds:
                begin = time.monotonic()
                record = await invoke(clients[route["id"]], route, tool, arguments, expected, out, "rollout")
                record["relative_seconds"] = begin - started
                if Path("/tmp/rollout-done").exists():
                    done = float(Path("/tmp/rollout-done").read_text())
                    if time.time() - done >= 15:
                        break
                await asyncio.sleep(max(0, .5 - (time.monotonic() - begin)))
        await asyncio.gather(*(stream(r) for r in routes))
    out.update(finished_utc=utc(), cpu_after=cpu_stat())
    out["summary"] = [{"route": r["id"],
                       "attempts": len(records := [v for v in out["requests"] if v["route"] == r["id"] and v["phase"] == "rollout"]),
                       "successes": sum(v["success"] for v in records),
                       "failures": sum(not v["success"] for v in records),
                       **summary([v["ms"] for v in records if v["success"]])} for r in routes]
    Path("/tmp/traffic.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out["summary"]), flush=True)


async def body(args):
    # Runs in an existing application pod, imports its unmodified server module.
    # Isolated tool-body diagnostic: intentionally excludes MCP dispatch/HTTP.
    sys.path.insert(0, "/app")
    import server
    import inspect
    names = (["echo", "add_numbers"] if args.body == "stateless" else ["increment_counter", "get_counter"])
    out = {"utc": utc(), "mode": args.body, "diagnostic": "isolated tool body; not serving request instrumentation", "results": []}
    for name in names:
        function = getattr(server, name)
        values = []
        for i in range(args.samples + 5):
            arguments = ({"message": "no-delay"} if name == "echo" else {"a": 19, "b": 23} if name == "add_numbers"
                         else {"name": args.counter, **({"amount": 1} if name == "increment_counter" else {})})
            begin = time.perf_counter()
            result = function(**arguments)
            if inspect.isawaitable(result):
                result = await result
            if i >= 5:
                values.append((time.perf_counter() - begin) * 1000)
        out["results"].append({"tool": name, **summary(values), "last_value": result})
    print(json.dumps(out), flush=True)


def orchestrate(args):
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    folder = root / "evidence" / "performance" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=30s"]
    commands = []

    def run(command, *, stdin=None, timeout=120, check=True):
        start = utc()
        result = subprocess.run(command, input=stdin, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        commands.append({"utc": start, "argv": command, "exit_code": result.returncode})
        (folder / "commands.json").write_text(json.dumps(commands, indent=2))
        if check and result.returncode:
            raise RuntimeError(f"{command}: {result.stdout}\n{result.stderr}")
        return result.stdout

    def k(*argv, namespace="mcp-auto", **kwargs):
        return run(prefix + ["-n", namespace, *argv], **kwargs)

    def snapshot(label):
        data = {"utc": utc()}
        for ns in ["mcp-auto", "mcp-demo", "knative-serving", "kourier-system"]:
            data[ns] = json.loads(k("get", "deployments,pods,services,endpointslices", "-o", "json", namespace=ns))
        data["knative"] = json.loads(k("get", "ksvc,revisions", "-o", "json"))
        with urlopen("http://127.0.0.1:8080/api/deployments?refresh=1", timeout=45) as response:
            data["ui"] = json.load(response)
        (folder / (label + ".json")).write_text(json.dumps(data, indent=2))
        return data

    print("Evidence:", folder, flush=True)
    before = snapshot("before")
    image = next(d for d in before["mcp-demo"]["items"] if d["kind"] == "Deployment" and d["metadata"]["name"] == "stateful-demo")["spec"]["template"]["spec"]["containers"][0]["image"]
    routes = []
    for name, mode in [("stateless-demo", "stateless"), ("stateful-demo", "stateful")]:
        service = next(s for s in before["knative"]["items"] if s["kind"] == "Service" and s["metadata"]["name"] == name)
        revision = next(t["revisionName"] for t in service["status"]["traffic"] if t.get("percent") == 100)
        for path in ["service", "knative", "private"]:
            host = name + ".mcp-auto.mcp.test" if path != "service" else name
            url = (f"http://{name}.mcp-demo.svc.cluster.local:8000/mcp" if path == "service" else
                   "http://kourier.kourier-system.svc.cluster.local/mcp" if path == "knative" else
                   f"http://{revision}-private.mcp-auto.svc.cluster.local/mcp")
            routes.append({"id": path + "-" + mode, "path": path, "mode": mode, "host": host, "url": url})
    identifier = "perf-client-" + uuid4().hex[:10]
    counter = "perf_" + uuid4().hex
    metadata = {"name": identifier, "namespace": "mcp-demo"}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata,
              "data": {"worker.py": Path(__file__).read_text(), "routes.json": json.dumps(routes)}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata,
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "client", "image": image, "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "-c", "import time; time.sleep(1200)"],
                                    "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                                                  "limits": {"cpu": "1", "memory": "256Mi"}},
                                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                                    "volumeMounts": [{"name": "validation", "mountPath": "/validation", "readOnly": True}]}],
                    "volumes": [{"name": "validation", "configMap": {"name": identifier}}]}}
    (folder / "client-resources.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [config, pod]}, indent=2))
    watchers, streams = [], []
    started = utc()
    try:
        k("apply", "-f", str(folder / "client-resources.json"), namespace="mcp-demo")
        k("wait", "--for=condition=Ready", "pod/" + identifier, "--timeout=90s", namespace="mcp-demo")
        common = ["exec", identifier, "--", "python", "/validation/worker.py", "--counter", counter, "--samples", str(args.samples)]
        print("Measuring no-delay MCP calls inside the cluster...", flush=True)
        warm_stdout = k(*common, "--worker", "warm", namespace="mcp-demo", timeout=360)
        (folder / "warm-stdout.log").write_text(warm_stdout)
        (folder / "warm.json").write_text(k("exec", identifier, "--", "cat", "/tmp/warm.json", namespace="mcp-demo"))
        if args.warm_only:
            snapshot("after")
            print("Warm-only completed:", folder, flush=True)
            return
        for mode in ["stateless", "stateful"]:
            app_pod = next(p["metadata"]["name"] for p in before["mcp-demo"]["items"] if p["kind"] == "Pod" and
                           p["metadata"].get("labels", {}).get("app") == mode + "-demo")
            result = k("exec", "-i", app_pod, "--", "python", "-", "--body", mode, "--counter", counter + "_body",
                       "--samples", str(args.samples), namespace="mcp-demo", stdin=Path(__file__).read_text())
            (folder / ("body-" + mode + ".json")).write_text(result)
        print("Starting one controlled Knative rollout; read-only traffic, no retries...", flush=True)
        for resource in ["pods", "endpointslices", "deployments", "ksvc"]:
            stream = (folder / (resource + "-watch.json")).open("w", encoding="utf-8")
            streams.append(stream)
            command = prefix + ["-n", "mcp-auto", "get", resource, "--watch", "--output-watch-events", "-o", "json", "--request-timeout=180s"]
            watchers.append(subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT))
            commands.append({"utc": utc(), "argv": command, "operation": "watch"})
        traffic_stream = (folder / "traffic-stdout.log").open("w", encoding="utf-8")
        streams.append(traffic_stream)
        traffic_command = prefix + ["-n", "mcp-demo", *common, "--worker", "traffic", "--seconds", "150"]
        traffic_process = subprocess.Popen(traffic_command, stdout=traffic_stream, stderr=subprocess.STDOUT)
        commands.append({"utc": utc(), "argv": traffic_command})
        deadline = time.monotonic() + 90
        while not k("exec", identifier, "--", "sh", "-c", "test -f /tmp/traffic-ready && echo ready", namespace="mcp-demo", check=False).strip():
            if traffic_process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Traffic preflight did not become ready; see traffic-stdout.log")
            time.sleep(1)
        time.sleep(6)
        old = json.loads(k("get", "ksvc/stateless-demo", "-o", "json"))
        digest = old["spec"]["template"]["spec"]["containers"][0]["image"]
        release = "performance-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        command = [sys.executable, "deployctl.py", "deploy", "servers/stateless", "--config", "mcp.knative.local.json",
                   "--namespace", "mcp-auto", "--kubeconfig", args.kubeconfig, "--image", digest,
                   "--version", release, "--timeout", "100", "--rollback-timeout", "100"]
        rollout_start = utc()
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=240)
        rollout_end = utc()
        (folder / "rollout.log").write_text(result.stdout + result.stderr)
        commands.append({"utc": rollout_start, "finished_utc": rollout_end, "argv": command, "exit_code": result.returncode})
        k("exec", identifier, "--", "python", "-c", "import time; open('/tmp/rollout-done','w').write(str(time.time()))", namespace="mcp-demo")
        traffic_process.wait(timeout=170)
        (folder / "traffic.json").write_text(k("exec", identifier, "--", "cat", "/tmp/traffic.json", namespace="mcp-demo"))
        (folder / "rollout-operation.json").write_text(json.dumps({"start": rollout_start, "end": rollout_end,
                                                                    "exit_code": result.returncode, "argv": command}, indent=2))
        snapshot("after")
        for namespace, deployment, container in [("kourier-system", "3scale-kourier-gateway", None),
                                                 ("knative-serving", "activator", None)]:
            arguments = ["logs", "deployment/" + deployment, "--since-time=" + started, "--timestamps=true"]
            if container:
                arguments += ["-c", container]
            (folder / (deployment + ".log")).write_text(k(*arguments, namespace=namespace, check=False))
        (folder / "events.json").write_text(k("get", "events", "-o", "json"))
        for revision in [old["status"]["traffic"][0]["revisionName"],
                         json.loads(k("get", "ksvc/stateless-demo", "-o", "json"))["status"]["traffic"][0]["revisionName"]]:
            for container in ["mcp", "queue-proxy"]:
                (folder / (revision + "-" + container + ".log")).write_text(k("logs", "-l", "serving.knative.dev/revision=" + revision,
                    "-c", container, "--since-time=" + started, "--timestamps=true", "--prefix=true", "--tail=-1", check=False))
        measured_traffic = json.loads((folder / "traffic.json").read_text())
        assert result.returncode == 0, "Rollout failed; retained deployment output and traffic evidence"
        assert traffic_process.returncode == 0, "Traffic worker failed; retained raw evidence"
        assert all(r["success"] for r in measured_traffic["requests"]), "MCP request failures retained; no retries"
        print("Completed:", folder, "rollout exit:", result.returncode, flush=True)
    finally:
        for watcher in watchers:
            if watcher.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(watcher.pid), "/T", "/F"], capture_output=True, timeout=15)
                else:
                    watcher.terminate()
                watcher.wait(timeout=15)
        for stream in streams:
            stream.close()
        k("delete", "pod,configmap", identifier, "--ignore-not-found=true", "--wait=false", namespace="mcp-demo", check=False)
        (folder / "commands.json").write_text(json.dumps(commands, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["warm", "traffic"])
    parser.add_argument("--warm-only", action="store_true", help="No rollout; repeat only the short latency measurement")
    parser.add_argument("--body", choices=["stateless", "stateful"])
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--counter", default="unused")
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--kubeconfig", default=".local/floci.kubeconfig")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(warm(args) if args.worker == "warm" else traffic(args))
    elif args.body:
        asyncio.run(body(args))
    else:
        orchestrate(args)
