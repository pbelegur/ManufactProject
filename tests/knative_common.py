"""Real MCP SDK over a forwarded Kourier gateway, with virtual-host routing."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import re
import time
from urllib.request import urlopen

import httpx2
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def call(gateway, hostname, tool, arguments):
    start = time.perf_counter()
    async with create_mcp_http_client(headers={"Host": hostname}, timeout=httpx2.Timeout(120)) as http:
        transport = streamable_http_client(gateway + "/mcp", http_client=http)
        async with Client(transport, read_timeout_seconds=120) as client:
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                raise RuntimeError(str(result))
            value = result.structured_content
            if value is None:
                value = json.loads(next(block.text for block in result.content if hasattr(block, "text")))
            if isinstance(value, dict) and set(value) == {"result"}:
                value = value["result"]
    return value, time.perf_counter() - start


class Cluster:
    def __init__(self, kubeconfig, namespace="mcp-auto"):
        self.kubeconfig, self.namespace = kubeconfig, namespace
        self.prefix = ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=30s", "-n", namespace]

    def run(self, args, timeout=240):
        result = subprocess.run(self.prefix + args, check=True, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        return result.stdout

    def get(self, resource, selector=None):
        command = ["get", resource, "-o", "json"]
        if selector:
            command += ["-l", selector]
        return json.loads(self.run(command))

    def counts(self, name):
        deployments = self.get("deployments", f"serving.knative.dev/service={name}")["items"]
        pods = self.get("pods", f"serving.knative.dev/service={name}")["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        return {"desired": sum(d["spec"].get("replicas", 0) for d in deployments),
                "ready": sum(d.get("status", {}).get("readyReplicas", 0) for d in deployments),
                "pods": len(active), "all_pods": len(pods)}

    @contextmanager
    def gateway(self, port=0):
        path = Path(".local/kourier-port-forward.log")
        path.parent.mkdir(exist_ok=True)
        with path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(["kubectl", "--kubeconfig", self.kubeconfig, "-n", "kourier-system",
                                        "port-forward", "svc/kourier", f"{port}:80", "--address=127.0.0.1"],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while "Forwarding from" not in path.read_text(encoding="utf-8"):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(path.read_text(encoding="utf-8"))
                    time.sleep(0.2)
                forwarding = path.read_text(encoding="utf-8")
                allocated = re.search(r"127\.0\.0\.1:(\d+)", forwarding)
                if not allocated:
                    raise RuntimeError(forwarding)
                yield f"http://127.0.0.1:{allocated.group(1)}"
            finally:
                if os.name == "nt" and process.poll() is None:
                    # Chocolatey's kubectl launcher has a child process; stop
                    # only this test's tree rather than leaving its port bound.
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                elif process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)


def save(name, value):
    folder = Path(os.environ.get("MCP_EVIDENCE_DIR", "evidence/phase3"))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
