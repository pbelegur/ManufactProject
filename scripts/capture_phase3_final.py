"""Read-only final evidence capture; waits for natural scale-to-zero."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args()
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=30s"]
    output = Path("evidence/phase3")
    output.mkdir(parents=True, exist_ok=True)

    def get(command):
        return json.loads(subprocess.run(prefix + command + ["-o", "json"], check=True, capture_output=True,
                                         text=True, encoding="utf-8", timeout=60).stdout)

    started = time.monotonic()
    samples = []
    while True:
        pods = get(["-n", "mcp-auto", "get", "pods"])["items"]
        mcp = [p for p in pods if "serving.knative.dev/service" in p["metadata"].get("labels", {})]
        deployments = get(["-n", "mcp-auto", "get", "deployments"])["items"]
        desired = sum(d["spec"].get("replicas", 0) for d in deployments
                      if "serving.knative.dev/service" in d["metadata"].get("labels", {}))
        samples.append({"seconds": time.monotonic() - started, "application_pods": len(mcp), "desired": desired})
        print(samples[-1], flush=True)
        if not mcp and desired == 0:
            break
        if time.monotonic() - started > 180:
            raise RuntimeError("application revisions did not return to zero")
        time.sleep(4)
    for name, command in (
        ("final-knative-services.json", ["-n", "mcp-auto", "get", "ksvc"]),
        ("final-knative-workloads.json", ["-n", "mcp-auto", "get", "deployment,pods,pvc"]),
        ("final-phase2-workloads.json", ["-n", "mcp-demo", "get", "deployment,pods,service,pvc"]),
        ("final-knative-infrastructure.json", ["-n", "knative-serving", "get", "deployment,hpa"]),
        ("final-kourier-infrastructure.json", ["-n", "kourier-system", "get", "deployment,hpa,service"]),
        ("final-node.json", ["get", "nodes"]),
    ):
        (output / name).write_text(json.dumps(get(command), indent=2), encoding="utf-8")
    (output / "final-idle-zero.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
    reliability = json.loads((output / "reliability.json").read_text(encoding="utf-8"))
    phases = defaultdict(lambda: {"successes": 0, "failures": 0, "latencies": []})
    for request in reliability["requests"]:
        group = phases[request["phase"] + "/" + request["service"]]
        group["successes" if request["success"] else "failures"] += 1
        group["latencies"].append(request["latency_seconds"])
    summary = {"summaries": reliability["summaries"], "phases": {}}
    for key, value in phases.items():
        latencies = sorted(value.pop("latencies"))
        value.update(p95_seconds=latencies[min(int(len(latencies) * .95), len(latencies) - 1)], max_seconds=max(latencies))
        summary["phases"][key] = value
    (output / "continuity-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print("PASS: final application pods are zero; resource snapshots saved", flush=True)


if __name__ == "__main__":
    main()
