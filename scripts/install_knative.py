"""Pinned Serving/Kourier install on an existing cluster, with a local-only profile."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

import yaml

VERSION = "1.23.0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--local-profile", action="store_true", help="one replica and small requests for local functional testing")
    args = parser.parse_args()
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=60s"]
    output = Path(".local/knative-install")
    output.mkdir(parents=True, exist_ok=True)

    def run(command, value=None, capture=False):
        print("$", subprocess.list2cmdline(prefix + command), flush=True)
        result = subprocess.run(prefix + command, input=json.dumps(value) if value else None,
                                check=True, capture_output=capture, text=True, encoding="utf-8", timeout=420)
        return result.stdout

    version = json.loads(run(["version", "-o", "json"], capture=True))["serverVersion"]
    if int(version["major"]) != 1 or int(version["minor"].rstrip("+")) < 34:
        raise RuntimeError("Knative 1.23.0 requires Kubernetes >=1.34; no version override is applied")
    snapshot = run(["-n", "mcp-demo", "get", "deploy,svc,pvc,pdb", "-o", "json"], capture=True)
    (output / "existing-workloads.json").write_text(snapshot, encoding="utf-8")
    evidence = {"version": VERSION, "local_profile": args.local_profile, "assets": {}}
    assets = [
        ("serving-crds.yaml", f"https://github.com/knative/serving/releases/download/knative-v{VERSION}/serving-crds.yaml"),
        ("serving-core.yaml", f"https://github.com/knative/serving/releases/download/knative-v{VERSION}/serving-core.yaml"),
        ("kourier.yaml", f"https://github.com/knative-extensions/net-kourier/releases/download/knative-v{VERSION}/kourier.yaml"),
    ]
    for name, url in assets:
        path = output / name
        if not path.exists():
            with urlopen(url, timeout=90) as response:
                path.write_bytes(response.read())
        raw = path.read_bytes()
        evidence["assets"][name] = {"url": url, "sha256": sha256(raw).hexdigest()}
        documents = [doc for doc in yaml.safe_load_all(raw) if doc]
        if args.local_profile:
            for doc in documents:
                if doc["kind"] == "Deployment":
                    doc["spec"]["replicas"] = 1
                    for container in doc["spec"]["template"]["spec"]["containers"]:
                        container.setdefault("resources", {})["requests"] = {"cpu": "50m", "memory": "96Mi"}
                # No cloud load balancer exists on this local host; use the
                # gateway ClusterIP (and a port-forward for host-side clients).
                if doc["kind"] == "Service" and doc.get("spec", {}).get("type") == "LoadBalancer":
                    doc["spec"]["type"] = "ClusterIP"
                if doc["kind"] == "HorizontalPodAutoscaler":
                    # Bounding Deployment replicas alone is ineffective when
                    # a bundled infrastructure HPA expands them under load.
                    doc["spec"]["minReplicas"] = 1
                    doc["spec"]["maxReplicas"] = 1
        rendered = output / (name + ".json")
        rendered.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}), encoding="utf-8")
        run(["apply", "-f", str(rendered)])
        if name == "serving-crds.yaml":
            run(["wait", "--for=condition=Established", "crd/services.serving.knative.dev", "--timeout=90s"])
        if name == "serving-core.yaml":
            for deployment in ("controller", "webhook", "autoscaler", "activator"):
                run(["-n", "knative-serving", "rollout", "status", f"deployment/{deployment}", "--timeout=300s"])
    run(["-n", "knative-serving", "patch", "configmap", "config-network", "--type=merge", "-p",
         json.dumps({"data": {"ingress-class": "kourier.ingress.networking.knative.dev"}})])
    run(["-n", "knative-serving", "patch", "configmap", "config-domain", "--type=merge", "-p",
         json.dumps({"data": {"mcp.test": ""}})])
    if args.local_profile:
        run(["-n", "knative-serving", "patch", "configmap", "config-deployment", "--type=merge", "-p",
             json.dumps({"data": {"queue-sidecar-cpu-request": "25m", "queue-sidecar-memory-request": "32Mi"}})])
    for namespace, deployment in (("knative-serving", "net-kourier-controller"), ("kourier-system", "3scale-kourier-gateway")):
        run(["-n", namespace, "rollout", "status", f"deployment/{deployment}", "--timeout=300s"])
    run(["get", "pods", "-n", "knative-serving", "-o", "wide"])
    run(["get", "pods,svc", "-n", "kourier-system", "-o", "wide"])
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("PASS: pinned Knative Serving and Kourier controllers/gateway ready", flush=True)


if __name__ == "__main__":
    main()
