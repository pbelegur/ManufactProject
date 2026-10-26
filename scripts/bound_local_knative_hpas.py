"""Apply the corrected single-replica local infrastructure profile in place."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args()
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig]
    previous = json.loads(subprocess.run(prefix + ["get", "hpa", "-A", "-o", "json"], check=True,
                                         capture_output=True, text=True, timeout=30).stdout)
    Path(".local/knative-before-hpa-bounds.json").write_text(json.dumps(previous, indent=2), encoding="utf-8")
    for namespace, name in (("knative-serving", "activator"), ("knative-serving", "webhook"),
                             ("kourier-system", "3scale-kourier-gateway")):
        subprocess.run(prefix + ["-n", namespace, "patch", "hpa", name, "--type=merge", "-p",
                                  json.dumps({"spec": {"minReplicas": 1, "maxReplicas": 1}})], check=True, timeout=30)


if __name__ == "__main__":
    main()
