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

async def check(args, url, host):
    begin = time.perf_counter()
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    imported = time.perf_counter()
    async with create_mcp_http_client(headers={"Host": host}, timeout=httpx2.Timeout(90)) as http:
        async with Client(streamable_http_client(url + "/mcp", http_client=http), read_timeout_seconds=90) as client:
            initialized = time.perf_counter()
            response = await client.call_tool(args.tool, json.loads(args.arguments))
            elapsed = (time.perf_counter() - initialized) * 1000
            if response.is_error:
                raise RuntimeError(str(response))
            return {"success": True, "response": response.model_dump(mode="json"),
                    "tool_latency_ms": elapsed, "initialization_ms": (initialized - begin) * 1000,
                    "sdk_import_ms": (imported - begin) * 1000,
                    "latency_ms": (time.perf_counter() - begin) * 1000,
                    "latency_scope": "SDK loading, client initialization and one tool call; excludes port-forward setup", "attempts": 1}


def forwarded_port(output):
    # Error logs can also contain 127.0.0.1 API URLs. Accept only kubectl's
    # explicit forwarding-ready line, never an arbitrary localhost address.
    # kubectl prints the resolved POD port (e.g. Kourier 8080), not the Service
    # port supplied in our command (80). The selected Service determines mapping.
    match = re.search(r"^Forwarding from 127\.0\.0\.1:(\d+) -> (\d+)\s*$", output, re.M)
    return int(match[1]) if match and all(1 <= int(part) <= 65535 for part in match.groups()) else None


def main(args):
    if args.tool == "increment_counter":
        raise ValueError("Mutation tools are forbidden in Test MCP")
    namespace = "kourier-system" if args.backend == "knative" else args.namespace
    service, port = ("kourier", 80) if args.backend == "knative" else (args.name, 8000)
    host = f"{args.name}.{args.namespace}.mcp.test" if args.backend == "knative" else args.name
    forward_timeout = getattr(args, "forward_timeout", 45)
    setup_start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ui-mcp-check-") as directory:
        path = Path(directory) / "forward.log"
        with path.open("w", encoding="utf-8") as log:
            command = ["kubectl", "--kubeconfig", args.kubeconfig, "-n", namespace,
                       "port-forward", "svc/" + service, f"0:{port}", "--address=127.0.0.1"]
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, shell=False)
            try:
                deadline = time.monotonic() + forward_timeout
                match = None
                while time.monotonic() < deadline:
                    match = forwarded_port(path.read_text(encoding="utf-8"))
                    if match:
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Service port-forward exited: " + path.read_text(encoding="utf-8"))
                    time.sleep(.1)
                if not match:
                    detail = path.read_text(encoding="utf-8").strip()[-4000:]
                    raise TimeoutError(f"Service port-forward did not start within {forward_timeout} seconds. "
                                       "No MCP request was sent. " + (detail or "kubectl returned no startup output; check cluster connectivity and host load."))
                setup_ms = (time.perf_counter() - setup_start) * 1000
                async def bounded_check():
                    return await asyncio.wait_for(check(args, "http://127.0.0.1:" + str(match), host), timeout=90)
                result = asyncio.run(bounded_check())
                return {**result, "forward_setup_ms": setup_ms}
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
    parser.add_argument("--forward-timeout", type=int, default=45)
    args = parser.parse_args()
    if not 10 <= args.forward_timeout <= 90:
        parser.error("--forward-timeout must be 10..90 seconds")
    started = time.perf_counter()
    try:
        result = main(args)
    except Exception as exc:
        result = {"success": False, "error_message": f"{type(exc).__name__}: {exc}", "error": "".join(traceback.format_exception(exc))[-5000:],
                  "latency_ms": (time.perf_counter() - started) * 1000,
                  "latency_scope": "Failed attempt including forwarding/setup", "attempts": 1}
    print(json.dumps(result), flush=True)
