"""End-to-end verification against an isolated Kubernetes context.

Real ClusterIP traffic, exact-replica Redis checks, and stateful pod deletion.
Run after deploying both examples; --verify-generic includes the third fixture.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", default="mcp-demo")
    parser.add_argument("--verify-generic", action="store_true")
    parser.add_argument("--output", default=".local/kubernetes-verification.json")
    args = parser.parse_args()
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=30s", "-n", args.namespace]

    def run(command, stdin=None, capture=False, timeout=210):
        print("$", subprocess.list2cmdline(prefix + command), flush=True)
        result = subprocess.run(prefix + command, input=stdin, text=True, encoding="utf-8",
                                capture_output=capture, check=True, timeout=timeout)
        return result.stdout if capture else None

    def get(kind, selector=None):
        command = ["get", kind, "-o", "json"]
        if selector:
            command += ["-l", selector]
        return json.loads(run(command, capture=True))

    def ready_pods():
        return [pod for pod in get("pods", "app=stateful-demo")["items"]
                if not pod["metadata"].get("deletionTimestamp") and
                any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))]

    evidence = {"deployments": {}, "endpoints": {}, "replica_checks": []}
    names = ["stateless-demo", "stateful-demo"] + (["contract-probe"] if args.verify_generic else [])
    for name in names:
        deployment = get(f"deployment/{name}")
        assert deployment["spec"]["replicas"] == deployment["status"]["readyReplicas"] == 2
        evidence["deployments"][name] = {
            "ready": 2, "generation": deployment["metadata"]["generation"],
            "image": deployment["spec"]["template"]["spec"]["containers"][0]["image"],
        }
        slices = get("endpointslices", f"kubernetes.io/service-name={name}")
        endpoints = [endpoint for item in slices["items"] for endpoint in item["endpoints"]
                     if endpoint.get("conditions", {}).get("ready") is True]
        assert len(endpoints) == 2, endpoints
        evidence["endpoints"][name] = [endpoint["addresses"] for endpoint in endpoints]
    claims = get("pvc")
    assert any(claim.get("status", {}).get("phase") == "Bound" for claim in claims["items"])
    evidence["pvcs"] = [{"name": c["metadata"]["name"], "phase": c.get("status", {}).get("phase"),
                         "storage_class": c["spec"].get("storageClassName")} for c in claims["items"]]

    pods = ready_pods()
    assert len(pods) == 2
    counter = "replicas_" + uuid4().hex
    code = '''import asyncio,json,sys
from mcp import Client
async def main():
    async with Client("http://localhost:8000/mcp", read_timeout_seconds=15) as client:
        result=await client.call_tool(sys.argv[1],json.loads(sys.argv[2]))
        assert not result.is_error,str(result)
        assert result.structured_content["result"]==int(sys.argv[3]),str(result)
        print(json.dumps({"tool":sys.argv[1],"result":result.structured_content["result"]}))
asyncio.run(main())
'''

    def replica_call(pod, tool, arguments, expected):
        result = run(["exec", pod, "--", "python", "-c", code, tool, json.dumps(arguments), str(expected)], capture=True)
        print(f"{pod}: {result.strip()}", flush=True)
        evidence["replica_checks"].append({"pod": pod, "tool": tool, "expected": expected})

    a, b = [p["metadata"]["name"] for p in pods]
    replica_call(a, "increment_counter", {"name": counter, "amount": 11}, 11)
    replica_call(b, "get_counter", {"name": counter}, 11)
    replica_call(b, "increment_counter", {"name": counter, "amount": 2}, 13)
    replica_call(a, "get_counter", {"name": counter}, 13)

    client_code = (Path(__file__).parent / "kubernetes_client.py").read_text(encoding="utf-8")
    cm_name = "mcp-verify-" + uuid4().hex[:8]
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cm_name},
                 "data": {"client.py": client_code}}
    run(["apply", "-f", "-"], stdin=json.dumps(configmap))
    image = evidence["deployments"]["stateful-demo"]["image"]

    def service_check(stage):
        job_name = cm_name + "-" + stage
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": job_name}, "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": 120, "ttlSecondsAfterFinished": 3600,
            "template": {"spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                "containers": [{"name": "client", "image": image, "command": ["python", "/test/client.py"],
                    "env": [{"name": "PERSISTED_COUNTER", "value": counter},
                            {"name": "VERIFY_GENERIC", "value": "1" if args.verify_generic else "0"}],
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                  "limits": {"cpu": "300m", "memory": "256Mi"}},
                    "volumeMounts": [{"name": "script", "mountPath": "/test", "readOnly": True}]}],
                "volumes": [{"name": "script", "configMap": {"name": cm_name}}]}}}}
        run(["apply", "-f", "-"], stdin=json.dumps(job))
        try:
            deadline = time.monotonic() + 150
            while True:
                status = get(f"job/{job_name}").get("status", {})
                if status.get("succeeded", 0) == 1:
                    break
                if status.get("failed", 0) or time.monotonic() > deadline:
                    raise RuntimeError(f"in-cluster service test failed: {status}")
                time.sleep(2)
        finally:
            logs = run(["logs", f"job/{job_name}"], capture=True)
            print(logs, flush=True)
        evidence[stage + "_service_test"] = json.loads(logs.strip().splitlines()[-1])

    service_check("before")
    deleted_uid = pods[0]["metadata"]["uid"]
    run(["delete", "pod", a, "--wait=true", "--timeout=60s"])
    run(["rollout", "status", "deployment/stateful-demo", "--timeout=180s"])
    deadline = time.monotonic() + 60
    while len(ready_pods()) != 2:
        if time.monotonic() > deadline:
            raise RuntimeError("stateful replicas did not recover")
        time.sleep(1)
    replacements = ready_pods()
    assert deleted_uid not in {p["metadata"]["uid"] for p in replacements}
    new_pod = next(p["metadata"]["name"] for p in replacements if p["metadata"]["name"] != b)
    replica_call(new_pod, "get_counter", {"name": counter}, 13)
    service_check("after")
    evidence["deleted_pod"] = a
    evidence["replacement_pod"] = new_pod
    evidence["counter"] = counter
    evidence["counter_after_deletion"] = 13
    evidence["result"] = "PASS"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    run(["delete", "configmap", cm_name])
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
