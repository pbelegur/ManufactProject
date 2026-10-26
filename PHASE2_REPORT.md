# Phase 2 — unified Kubernetes deployment platform

Verified on **2026-10-26 (America/Phoenix)**. Phase 2 is complete on **Floci real-mode
EKS/k3s with Floci ECR**. No kind fallback was needed. The existing MCP server
code, both production Dockerfiles, Compose Redis integration and Phase 1 tests
were preserved. The user's existing `kind-manufact-lab` current context was left
unchanged; every platform operation used `.local/floci.kubeconfig` explicitly.

## Result

| Workload | Ready/desired | Service | Final ready endpoints |
| --- | --- | --- | --- |
| `stateless-demo` | **2/2** | `10.100.59.127:8000` | `10.42.0.11`, `10.42.0.14` |
| `stateful-demo` | **2/2** | `10.100.81.126:8000` | `10.42.0.10`, `10.42.0.19` |
| `stateful-demo-redis` | **1/1** | `10.100.204.159:6379` | `10.42.0.8` |
| Independent `contract-probe` project | **2/2** | `10.100.253.222:8000` | `10.42.0.12`, `10.42.0.13` |

Redis claim `stateful-demo-redis-data` is **Bound**, **1Gi**, **ReadWriteOnce**, using
StorageClass `local-path`. All workloads run concurrently in namespace `mcp-demo`.
The two original Compose MCP containers and Compose Redis also remain healthy.

Saved, non-secret evidence:

- [Final resource status](evidence/phase2/final-resources.txt)
- [Final EndpointSlices including readiness](evidence/phase2/final-endpoints.json)
- [MCP routing, replica sharing and pod replacement](evidence/phase2/kubernetes-verification.json)
- [Real failed-rollout recovery](evidence/phase2/failed-rollout-verification.json)
- [Replica changes and idempotent redeployment](evidence/phase2/redeploy-verification.json)

The isolated administrator kubeconfig contains private credentials and stays in
ignored `.local/`; it was not copied into the evidence directory.

## One deployment implementation

`deployctl.py deploy PROJECT_PATH` reads `PROJECT_PATH/mcp.json` and builds its
`Dockerfile`. It does not import example modules or assume `server.py`, Python,
tool names, or one of the examples' directory names. Project settings control
name, replicas, environment, CPU/memory resources, image pull Secrets and Redis
configuration. See [DEPLOYMENT_CONTRACT.md](DEPLOYMENT_CONTRACT.md).

The independent fixture in `tests/fixtures/compatible_project` uses `main.py`, a
`multiply` tool, its own environment value and different CPU/memory settings. It
was built, published, deployed and called through the same pipeline. Its minimal
manifest omits mode, port and replicas, exercising defaults of stateless/8000/2.

The shared execution path is:

1. Validate the project and replica settings; snapshot existing application
   resources and require a healthy baseline before updating an existing app.
2. Build its Dockerfile and create a `VERSION-FULL_BUILD_ID` image tag.
3. Publish to a registry or load into a named kind cluster. These are adapters;
   application resource generation, rollout and recovery are identical.
4. Create/update the namespace and, if required, the application's managed Redis
   resources. An explicit `env.REDIS_URL` uses an external Redis instead.
5. Validate application resources with Kubernetes server-side dry-run, then apply
   Deployment, ClusterIP Service and PodDisruptionBudget.
6. Wait for rollout and verify observed generation plus updated, ready and
   available replicas. Report the image, count and internal MCP URL as JSON.
7. On application apply/rollout failure, collect pod/deployment/event diagnostics,
   restore the previous healthy application resources, wait for recovery and exit
   nonzero. A failed initial app deployment removes its new app resources while
   retaining dependency storage.

Unchanged cached builds resolve to the same image tag. The default version prefix
is `build`; explicitly changing `--version` changes the image reference. BuildKit
provenance is disabled for this local path so timestamped attestations do not
change otherwise identical image IDs. Floci repositories were created immutable.
Production provenance/signing is not implemented.

Final deployed image suffixes:

```text
stateless-demo:phase2-v2-109b4a37e6d6196e1124c12d29827b0c42eb1dbad18aab653d9fb30a407093ff
stateful-demo:phase2-v1-7e41ef3f453924481997f8a5533d3570f4d8f135a1fe3257ec64adf412588702
contract-probe:phase2-generic-v1-4a173a148606a17a3e4f298736c3d6d0d27a1de15f7bb9791bd17d4ec338507d
```

All use registry prefix `localhost:4566/000000000000/us-east-1`.
Kubernetes reported the expected stateful image digest
`sha256:7e41ef3f453924481997f8a5533d3570f4d8f135a1fe3257ec64adf412588702`
on both replicas. Pod events recorded actual pulls from Floci ECR; images were
not imported into k3s from the Docker image store.

## Infrastructure and resources

Observed runtime: Floci **2.3.0**, Kubernetes **v1.34.1+k3s1**, containerd
**2.1.4-k3s2**, one Ready node `i-f8241bc031e1365ba.ec2.internal`. The k3s container
was discovered through Floci service/resource/account labels rather than an
assumed container name. The Kubernetes API is reachable at `https://localhost:6500`.

| Resource | Purpose |
| --- | --- |
| Namespace | Groups the platform demo workloads |
| MCP Deployment | Reconciles independent MCP replicas and manages rolling updates |
| MCP ClusterIP Service | Stable internal DNS/IP and routing to ready MCP endpoints |
| MCP PodDisruptionBudget | Keeps at least one replica available during supported voluntary evictions |
| Redis Deployment | Runs one shared Redis process; Recreate strategy avoids overlapping instances on its PVC |
| Redis ClusterIP Service | Gives all MCP replicas the same Redis URL |
| Redis PVC/PV | Keeps AOF files outside MCP pods; uses local storage in the k3s Docker volume |
| StorageClass/provisioner | Allocates local-test persistent volumes on this node |
| Verification ConfigMap/Jobs | Runs real SDK clients inside Kubernetes against ClusterIP Services |

MCP defaults are requests **100m CPU/128Mi memory**, limits **500m CPU/512Mi**.
All MCP Deployments use startup, readiness and liveness probes on `/health`,
`minReadySeconds=3`, `maxUnavailable=0`, `maxSurge=1`, five revisions of history,
and a **30-second** graceful termination period. The Docker contract remains
port **8000**, `/mcp`, `/health`, direct SIGTERM handling and environment settings.
Automatic ServiceAccount token mounting is disabled on MCP pods.

The application-stateful MCP deployment is a normal Deployment: its replicas
need neither stable pod identities nor local volumes. Redis holds the data.

## Actual commands executed

Working directory: `C:\Users\Devi\Downloads\mcp-deployment-platform-starter`.
Docker, Kubernetes and networked client commands used elevated tool access due to
the managed sandbox. The AWS CLI was absent; boto3 was installed only into the
workspace virtual environment. The deployment CLI itself uses Python stdlib and
the Docker/kubectl executables, with kind needed only for its fallback adapter.

```powershell
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}'
kubectl config get-contexts
kind get clusters
python -m pip --python .venv install boto3 --no-cache-dir
docker compose -f compose.floci.yaml up -d
.\.venv\Scripts\python.exe scripts/floci_setup.py --repository stateless-demo --repository stateful-demo --repository contract-probe
kubectl --kubeconfig .local/floci.kubeconfig get nodes,storageclass -o wide

.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase2-v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateful --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase2-v1
.\.venv\Scripts\python.exe deployctl.py deploy tests/fixtures/compatible_project --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase2-generic-v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase2-v2

.\.venv\Scripts\python.exe -m pytest -q -s tests --run-integration --run-compose-restart
.\.venv\Scripts\python.exe tests/verify_kubernetes.py --kubeconfig .local/floci.kubeconfig --verify-generic
.\.venv\Scripts\python.exe tests/verify_failed_rollout.py --kubeconfig .local/floci.kubeconfig
.\.venv\Scripts\python.exe tests/verify_redeploy.py --kubeconfig .local/floci.kubeconfig --registry localhost:4566/000000000000/us-east-1
.\.venv\Scripts\python.exe -m pytest -q tests/test_manifest.py tests/test_deployctl.py

kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo get deployment,pods,service,pvc,pdb -o wide
kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo get endpointslices
kubectl config current-context
docker compose ps
```

The redeploy script actually issued the generic project's deploy command three
times with the same `phase2-generic-v1` version and respectively `--replicas 3`,
`--replicas 2`, `--replicas 2`. Full captured output is under `.local/redeploy-*.log`.
The failed-rollout script issued the stateless deploy command with
`--image localhost:4566/000000000000/us-east-1/stateless-demo:deliberately-missing-d54c362b510f40a8869f74f4f8250cad --timeout 35`.

The verification script's exact pod deletion was:

```powershell
kubectl --kubeconfig .local/floci.kubeconfig --request-timeout=30s -n mcp-demo delete pod stateful-demo-7688cd9947-7n89v --wait=true --timeout=60s
```

## Executed verification outcomes

- **17 pytest tests passed in 14.55s**, including all original eight Phase 1 tests
  against Compose and nine new deployment cases. The Compose stateful restart
  retained its test counter at 7. After final deployment-validation refinements,
  the manifest/deployment subset was rerun: **10 passed in 0.49s**.
- **Kubernetes Service routing passed before and after pod deletion.** Real SDK
  clients inside Jobs called stateless echo/add, stateful increment/get, and the
  independent project's multiply tool. These used `http://SERVICE:8000/mcp`, not
  host port-forwards. Echo returned `through-ClusterIP`; addition returned 42;
  the generic tool returned product 42 and `configured-by-project`.
- **Shared state across exact replicas passed.** Pod
  `stateful-demo-7688cd9947-7n89v` incremented counter
  `replicas_9abbd025973e42af86c10a3e394f98b4` to 11. Pod
  `stateful-demo-7688cd9947-hxq8z` read 11, incremented to 13, and the first pod
  read 13. Every operation used a separate real MCP connection.
- **MCP pod deletion recovery passed.** The first pod was deleted; replacement
  `stateful-demo-7688cd9947-dkhx7` read **13**. Stateful returned to 2/2 and the
  after-deletion Job read the same counter through its Service.
- **Failed-rollout recovery passed.** The missing tag produced `ErrImagePull` /
  `ImagePullBackOff`. The CLI restored the previous spec and reported
  `ROLLBACK OK`, while exiting **1** to report the failed deployment. Both prior
  healthy stateless pod UIDs were retained. All **19 sampled** ready Service
  endpoint counts were **2**. This tests an unavailable-image failure, not every
  possible application or infrastructure failure.
- **Replica overrides passed: 3/3, then 2/2.** The subsequent identical 2-replica
  deployment retained **generation 3**, the same image and the same pod UIDs.
- **Simultaneous operation passed.** Both MCP services, Redis and the generic
  fixture were running together. Compose regression tests also ran independently
  of the Kubernetes applications.

## Issues encountered and resolved

1. Documentation's k3s container-name example differed from Floci 2.3.0's actual
   `floci-aws-eks-mcp-platform` name. The helper now discovers labels.
2. Docker Desktop could not resolve the default wildcard ECR hostname. Floci's
   documented `FLOCI_SERVICES_ECR_URI_STYLE=path` fallback produced working host
   pushes and real Kubernetes pulls. No kind fallback was required.
3. The cluster was originally created without automatic storage provisioning.
   Enabling `FLOCI_SERVICES_EKS_DEFAULT_STORAGE_CLASS=true` did not alter the
   already-adopted container's flags. The helper installed pinned upstream
   local-path-provisioner v0.0.34, putting data below `/var/lib/rancher/k3s/storage`
   inside the persistent k3s Docker volume. The Redis PVC then bound successfully.
4. The first in-cluster generic-tool check failed because the test helper assumed
   every tool supplied `structured_content`. A bare dict result legitimately used
   MCP TextContent. The client now parses that JSON text; the complete verification
   was rerun and passed. The failed test Job and its ConfigMap were removed.
5. A final optional PowerShell JSONPath display command had a quoting error;
   `custom-columns` output succeeded and confirmed both stateful image digests.

No outstanding Phase 2 blocker remains.

## Limits versus production AWS EKS

This proves real Kubernetes deployment, actual image publication/pulls, MCP
routing and shared application state on this local environment. It does **not**
prove HA, zero downtime, multi-node fault tolerance or load capacity.

Floci uses one privileged k3s Docker container and emulates AWS control APIs;
production EKS has managed control-plane behavior, real worker capacity, VPC/CNI,
IAM/OIDC, registry permissions, load balancing, storage drivers and operational
requirements that are not equivalent to this host. The local ECR endpoint is HTTP
with test credentials and a generated containerd mirror, not production ECR IAM
authentication. This helper uses a local administrator certificate, not a verified
production `aws eks get-token`/RBAC access workflow.

The Redis backend is one process and local PVC. It survives MCP pod replacement,
but this run did not test Redis crash recovery, node loss, backup restore or
multi-zone storage. The PVC and the Docker volume depend on the same host. The
example `/health` endpoints report process health, not successful Redis operations.

Rolling settings and a PDB are constraints, not an availability guarantee. Direct
pod deletion bypasses eviction PDB protections. The rollback test sampled ready
endpoints and checked pod identity; it did not continuously measure all live
requests or establish zero downtime during every type of rollout.

Deployment execution assumes one trusted operator per application. There is no
multi-user lock, hosted job queue, tenant isolation or production secret injection.
No multi-region, autoscaling or scale-to-zero was implemented. The existing kind
publishing adapter is retained, but Phase 2 did not execute a kind deployment.

References verified against primary sources:

- [Floci EKS real mode, ECR mirrors and storage configuration](https://github.com/floci-io/floci/blob/main/docs/services/eks.md)
- [Floci ECR publishing and path-style URI configuration](https://github.com/floci-io/floci/blob/main/docs/services/ecr.md)
- [Official local-path provisioner v0.0.34 manifest](https://github.com/rancher/local-path-provisioner/blob/v0.0.34/deploy/local-path-storage.yaml)
