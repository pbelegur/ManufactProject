"""One explicitly approved read-only MCP call. No retries; localhost forwarding."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import traceback

import httpx2
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def check(args, url, host):
    begin = time.perf_counter()
    async with create_mcp_http_client(headers={"Host": host}, timeout=httpx2.Timeout(90)) as http:
        async with Client(streamable_http_client(url + "/mcp", http_client=http), read_timeout_seconds=90) as client:
            initialized = time.perf_counter()
            response = await client.call_tool(args.tool, json.loads(args.arguments))
            elapsed = (time.perf_counter() - initialized) * 1000
            if response.is_error:
                raise RuntimeError(str(response))
            return {"success": True, "response": response.model_dump(mode="json"),
                    "tool_latency_ms": elapsed, "initialization_ms": (initialized - begin) * 1000,
                    "latency_ms": (time.perf_counter() - begin) * 1000,
                    "latency_scope": "Client initialization plus tool call; excludes port-forward setup", "attempts": 1}


def main(args):
    if args.tool == "increment_counter":
        raise ValueError("Mutation tools are forbidden in Test MCP")
    namespace = "kourier-system" if args.backend == "knative" else args.namespace
    service, port = ("kourier", 80) if args.backend == "knative" else (args.name, 8000)
    host = f"{args.name}.{args.namespace}.mcp.test" if args.backend == "knative" else args.name
    with tempfile.TemporaryDirectory(prefix="ui-mcp-check-") as directory:
        path = Path(directory) / "forward.log"
        with path.open("w", encoding="utf-8") as log:
            command = ["kubectl", "--kubeconfig", args.kubeconfig, "-n", namespace,
                       "port-forward", "svc/" + service, f"0:{port}", "--address=127.0.0.1"]
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, shell=False)
            try:
                deadline = time.monotonic() + 20
                match = None
                while time.monotonic() < deadline:
                    match = re.search(r"127\.0\.0\.1:(\d+)", path.read_text(encoding="utf-8"))
                    if match:
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Service port-forward exited: " + path.read_text(encoding="utf-8"))
                    time.sleep(.1)
                if not match:
                    raise TimeoutError("Service port-forward did not start within 20 seconds")
                return asyncio.run(check(args, "http://127.0.0.1:" + match[1], host))
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
                    else:
                        process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--backend", choices=["always-on", "knative"], required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        result = main(args)
    except Exception as exc:
        result = {"success": False, "error": "".join(traceback.format_exception(exc))[-5000:],
                  "latency_ms": (time.perf_counter() - started) * 1000,
                  "latency_scope": "Failed attempt including forwarding/setup", "attempts": 1}
    print(json.dumps(result), flush=True)
