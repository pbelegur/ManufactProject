"""Provision a local real-mode Floci EKS cluster and ECR repositories.

Uses boto3 when the AWS CLI is unavailable. Writes a private, isolated local
admin-certificate kubeconfig; it does not change the user's current context.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://localhost:4566")
    parser.add_argument("--cluster", default="mcp-platform")
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--output", default=".local/floci.kubeconfig")
    args = parser.parse_args()
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")

    def client(service):
        return session.client(service, endpoint_url=args.endpoint,
                              config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 2}))

    iam, eks, ecr = (client(service) for service in ("iam", "eks", "ecr"))
    try:
        role = iam.get_role(RoleName="mcp-platform-eks")["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName="mcp-platform-eks", AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "eks.amazonaws.com"},
                                                    "Action": "sts:AssumeRole"}]}))["Role"]
    try:
        cluster = eks.describe_cluster(name=args.cluster)["cluster"]
    except eks.exceptions.ResourceNotFoundException:
        cluster = eks.create_cluster(name=args.cluster, roleArn=role["Arn"], resourcesVpcConfig={})["cluster"]
    deadline = time.monotonic() + 420
    while cluster["status"] != "ACTIVE":
        if cluster["status"] == "FAILED" or time.monotonic() > deadline:
            raise RuntimeError(f"Floci cluster did not become ACTIVE: {cluster}")
        print(f"EKS {args.cluster}: {cluster['status']}", flush=True)
        time.sleep(5)
        cluster = eks.describe_cluster(name=args.cluster)["cluster"]
    # Mock mode cannot pass this check: a real k3s container must exist and
    # kubectl must subsequently reach its real Kubernetes API.
    containers = subprocess.run(["docker", "ps", "--filter", "label=io.floci.service=eks",
                                 "--filter", f"label=io.floci.resource-id={args.cluster}",
                                 "--filter", "label=io.floci.account=000000000000", "--format", "{{.ID}}"],
                                check=True, capture_output=True, text=True, timeout=30).stdout.split()
    if len(containers) != 1:
        raise RuntimeError(f"expected one real Floci k3s container, found {containers}")
    result = subprocess.run(["docker", "exec", containers[0], "cat", "/etc/rancher/k3s/k3s.yaml"],
                            check=True, capture_output=True, text=True, timeout=30)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    config = result.stdout.replace("https://127.0.0.1:6443", cluster["endpoint"])
    output.write_text(config, encoding="utf-8")
    subprocess.run(["kubectl", "--kubeconfig", str(output), "wait", "node", "--all", "--for=condition=Ready", "--timeout=180s"],
                   check=True, timeout=210)
    # Floci adopts existing k3s containers without changing their startup flags.
    # Older clusters may still have --disable=local-storage after enabling the
    # Compose option. Install the pinned upstream provisioner only when absent.
    kubectl = ["kubectl", "--kubeconfig", str(output)]
    classes = json.loads(subprocess.run(kubectl + ["get", "storageclasses", "-o", "json"],
                                       check=True, capture_output=True, text=True, timeout=30).stdout)
    if not classes["items"]:
        url = "https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.34/deploy/local-path-storage.yaml"
        with urlopen(url, timeout=30) as response:
            storage_manifest = response.read().decode().replace("/opt/local-path-provisioner", "/var/lib/rancher/k3s/storage")
        subprocess.run(kubectl + ["apply", "-f", "-"], input=storage_manifest, text=True, check=True, timeout=60)
        subprocess.run(kubectl + ["annotate", "storageclass", "local-path",
                                  "storageclass.kubernetes.io/is-default-class=true", "--overwrite"], check=True, timeout=30)
        subprocess.run(kubectl + ["-n", "local-path-storage", "rollout", "status", "deployment/local-path-provisioner", "--timeout=180s"],
                       check=True, timeout=210)
    repositories = {}
    for name in args.repository:
        try:
            repo = ecr.create_repository(repositoryName=name, imageTagMutability="IMMUTABLE")["repository"]
        except ecr.exceptions.RepositoryAlreadyExistsException:
            repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
        repositories[name] = repo["repositoryUri"]
        print(f"ECR repository: {repo['repositoryUri']}", flush=True)
    if repositories:
        auth = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        registry = next(iter(repositories.values())).split("/")[0]
        subprocess.run(["docker", "login", "--username", username, "--password-stdin", registry],
                       input=password, text=True, check=True, timeout=60)
    evidence = {"cluster": args.cluster, "status": cluster["status"], "endpoint": cluster["endpoint"],
                "kubeconfig": str(output), "repositories": repositories}
    (output.parent / "floci.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
