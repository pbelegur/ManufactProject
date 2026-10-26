"""Inject an unavailable image and verify the CLI restores a healthy deployment."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", default="mcp-demo")
    parser.add_argument("--output", default=".local/failed-rollout-verification.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=15s", "-n", args.namespace]

    def get(resource, selector=None):
        command = prefix + ["get", resource, "-o", "json"]
        if selector:
            command += ["-l", selector]
        return json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout)

    before = get("deployment/stateless-demo")
    old_image = before["spec"]["template"]["spec"]["containers"][0]["image"]
    old_pods = {p["metadata"]["uid"] for p in get("pods", "app=stateless-demo")["items"]}
    missing = old_image.rsplit(":", 1)[0] + ":deliberately-missing-" + uuid4().hex
    command = [sys.executable, str(root / "deployctl.py"), "deploy", str(root / "servers/stateless"),
               "--kubeconfig", args.kubeconfig, "--namespace", args.namespace, "--image", missing, "--timeout", "35"]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".log")
    samples = []
    print("$", subprocess.list2cmdline(command), flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 180
        try:
            while process.poll() is None:
                slices = get("endpointslices", "kubernetes.io/service-name=stateless-demo")
                ready = sum(e.get("conditions", {}).get("ready") is True
                            for s in slices["items"] for e in s["endpoints"])
                samples.append(ready)
                print(f"Ready stateless Service endpoints during failure check: {ready}", flush=True)
                if time.monotonic() > deadline:
                    raise RuntimeError("failure verification exceeded its deadline")
                time.sleep(2)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
    logs = log_path.read_text(encoding="utf-8")
    print(logs[-7000:], flush=True)
    assert process.returncode == 1, process.returncode
    assert "ROLLBACK OK: previous healthy deployment restored" in logs
    after = get("deployment/stateless-demo")
    assert after["spec"]["template"] == before["spec"]["template"]
    assert after["spec"]["replicas"] == after["status"]["readyReplicas"] == 2
    pods_after = {p["metadata"]["uid"] for p in get("pods", "app=stateless-demo")["items"]
                  if not p["metadata"].get("deletionTimestamp")}
    assert old_pods == pods_after
    assert min(samples) == 2
    evidence = {"result": "PASS", "expected_cli_exit": process.returncode,
                "previous_image": old_image, "failed_image": missing, "old_pod_uids_preserved": sorted(old_pods),
                "ready_endpoint_samples": samples, "restored_ready_replicas": 2, "log": str(log_path)}
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
