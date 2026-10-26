"""Verify CLI replica overrides and repeated deployment idempotency on the generic fixture."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", default="mcp-demo")
    parser.add_argument("--output", default=".local/redeploy-verification.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "-n", args.namespace]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def get(resource, selector=None):
        command = prefix + ["get", resource, "-o", "json"]
        if selector:
            command += ["-l", selector]
        return json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout)

    evidence = {"replica_sequence": []}
    previous_command = None
    for replicas in (3, 2, 2):
        before = get("deployment/contract-probe")
        before_pods = {p["metadata"]["uid"] for p in get("pods", "app=contract-probe")["items"]
                       if not p["metadata"].get("deletionTimestamp")}
        command = [sys.executable, str(root / "deployctl.py"), "deploy", str(root / "tests/fixtures/compatible_project"),
                   "--kubeconfig", args.kubeconfig, "--namespace", args.namespace,
                   "--push-registry", args.registry, "--version", "phase2-generic-v1", "--replicas", str(replicas)]
        print("$", subprocess.list2cmdline(command), flush=True)
        log_path = output.parent / f"redeploy-{len(evidence['replica_sequence'])}.log"
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True, timeout=300)
        if result.returncode:
            print(log_path.read_text(encoding="utf-8"), flush=True)
        assert result.returncode == 0
        after = get("deployment/contract-probe")
        assert after["status"]["readyReplicas"] == after["spec"]["replicas"] == replicas
        image = after["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image == before["spec"]["template"]["spec"]["containers"][0]["image"]
        # Equal desired replicas alone does not mean the prior operation used
        # this command (for example it may have been a rollback snapshot).
        if command == previous_command:
            assert after["metadata"]["generation"] == before["metadata"]["generation"]
            after_pods = {p["metadata"]["uid"] for p in get("pods", "app=contract-probe")["items"]
                          if not p["metadata"].get("deletionTimestamp")}
            assert before_pods == after_pods
            evidence["idempotent_generation"] = after["metadata"]["generation"]
            evidence["unchanged_pod_uids"] = sorted(after_pods)
        evidence["replica_sequence"].append({"requested": replicas, "ready": replicas, "image": image})
        previous_command = command
        print(f"PASS: requested={replicas}, ready={replicas}, generation={after['metadata']['generation']}", flush=True)
    evidence["result"] = "PASS"
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
