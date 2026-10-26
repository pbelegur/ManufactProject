# Phase 3 — MCP autoscaling and scale-to-zero

Verified **2026-10-26, America/Phoenix**, on the existing Floci real-mode EKS/k3s
cluster. Knative Serving and Kourier **1.23.0** are installed. The optional backend,
automatic scale-up/zero, MCP cold starts, Redis persistence, traffic continuity,
v2 promotion, pod replacement and rollback were executed successfully with the
local profiles described below. **29 regression tests passed.**

Earlier failed capacity and implementation trials are retained separately. The
successful continuity result is a bounded local observation, not a general
zero-downtime or HA guarantee.

## Results and evidence

### Automatic scaling and cold starts

These measurements used the `mcp.knative.json` profiles: min 0, activation/initial
2, max 10, target concurrency 2, window 20 seconds. Each service was tested
sequentially with **eight concurrent real MCP clients**, using three-second
asynchronous tool calls for about 65 seconds. The k3s runtime quota was **two CPUs**
for this measurement.

| Measurement | Stateless | Redis-backed stateful |
| --- | --- | --- |
| Successful load tool calls | **145** | **110** |
| Failed load tool calls | **0** | **0** |
| First observed ready count above 2 | **31.85s** | **4.70s** |
| Peak ready replicas during load | **6** | **6** |
| Peak desired replicas during load | **6** | **10** |
| Idle time until desired replicas and all application pods were zero | **55.48s** | **54.93s** |
| Cold MCP request latency after observing zero pods | **11.83s** | **14.41s** |
| Cold request result | `cold-start` | Counter **117**, as expected |

Desired replicas are not ready replicas: the stateful service requested ten, but
the load sampling did not establish ten ready replicas. Its later idle sampling
briefly observed eight ready while newly started pods finished warming.

No manual application scale commands were used. The test stopped traffic, polled
the generated Deployments and pods, then sent the cold request through **Kourier**
with no application pod running. Knative requested two activation replicas; the
stateful response completed while one was ready and the second was starting.

Counter `autoscale_8e95c12d9c0e45118b1d5a076ef4050f` was seeded at 7 and incremented
110 times through MCP. After all stateful MCP pods disappeared, the cold request
read **117** from Redis. Redis was kept outside the scaled application pods.

Raw evidence, including timestamped desired/ready/pod counts and per-call load
latencies:

- [Stateless scaling and cold start](evidence/phase3/stateless-demo-autoscaling.json)
- [Stateful scaling and persisted counter](evidence/phase3/stateful-demo-autoscaling.json)

### Final continuity run

The final run used **max-four local application profiles**, single-replica local
infrastructure HPAs, and an in-place **four-CPU k3s runtime quota**. Scheduler
allocatable CPU remained **1930m**. The generator scheduled **2.5 MCP tool calls/sec
per service**, using fresh SDK connections through Kourier. No retries hid failures.

Duration: **259.65 seconds**. Observed scheduled rate: **2.496 calls/sec per service**.

| Service | Successes | Failures | p50 latency | p95 latency | Maximum latency |
| --- | --- | --- | --- | --- | --- |
| Stateless | **648/648** | **0** | 0.906s | 2.765s | 4.648s |
| Stateful | **648/648** | **0** | 0.302s | 2.382s | 4.490s |

Latency includes MCP connection/discovery, tool execution and client teardown.
Stateless calls deliberately included a 0.6-second asynchronous delay. MCP
discovery and protocol requests mean the HTTP request rate exceeds the tool-call
rate. These figures are not raw HTTP throughput benchmarks.

| Phase | Successful calls per service | Failures per service |
| --- | --- | --- |
| Baseline | 30 | 0 |
| Independent new service deployment | 189 | 0 |
| Existing service v2 rollout | 118 | 0 |
| Stateful pod deletion/recovery | 115 | 0 |
| Deliberately failed rollout/recovery | 158 | 0 |
| After recovery | 38 | 0 |

Executed operations:

- Created the genuinely new `phase3-verified-probe` Knative Service using the
  independent fixture, custom project configuration and the same CLI. Its MCP
  `multiply` tool returned product 42 and `configured-by-project`. Deployment took
  **64.43s** including build/push/readiness.
- Deployed stateless release **`phase3-v2-verified`**, waited for the desired
  candidate template, and confirmed actual route status sent 100% to
  **`stateless-demo-00006`**. This took **37.88s**. This test used cached application
  image bytes with a new release/revision annotation; it demonstrates revision
  rollout and route promotion, not a changed tool implementation.
- Deleted `stateful-demo-00002-deployment-54c5f48977-4xvlg`. A subsequent MCP call
  returned the seeded counter value **17**, and continuous stateful reads also
  remained correct.
- Deployed an intentionally nonexistent image digest with `--timeout 35`. The
  CLI exited **1**, restored the previous template and pinned ready route, waited
  with the separate recovery timeout, then deleted failed unrouted revision
  **`stateless-demo-00007`**. Total failure/recovery operation: **62.27s**.
  Restored configuration revision `00008` became ready while traffic remained on
  the previous healthy `00006` route. The log recorded **`ROLLBACK OK`**.

[Every measured request and actual subprocess command](evidence/phase3/reliability.json),
[per-phase latency summary](evidence/phase3/continuity-summary.json),
[independent deployment log](evidence/phase3/knative-phase3-independent-v1.log),
[v2 promotion log](evidence/phase3/knative-phase3-v2-verified.log),
[failed rollout/recovery log](evidence/phase3/knative-phase3-intentional-failure.log).

The final read-only capture observed **zero Knative application pods and zero
desired application replicas** after traffic stopped. Redis and Knative
infrastructure remained running. See [final idle observation](evidence/phase3/final-idle-zero.json)
and [final application resources](evidence/phase3/final-knative-workloads.json).

## Infrastructure verification and protection

- Floci **2.3.0**, real k3s/containerd runtime; no mock EKS or kind fallback.
- Kubernetes **v1.34.1+k3s1**, containerd **2.1.4-k3s2**. Serving 1.23.0's pinned
  dependency defines minimum Kubernetes **1.34.0**; no minimum-version override
  was used.
- Docker host: **8 logical CPUs**, about **7.63 GiB RAM**. k3s container memory
  limit: **6.105 GiB**; Kubernetes allocatable memory about **4.84 GiB** and CPU
  **1930m**. Initial runtime CPU quota was two CPUs; continuity testing eventually
  used four. Memory limits and node allocatable resources were not increased.
- Existing IPv4 Flannel networking, pod CIDR `10.42.0.0/24`, Service range
  `10.100.0.0/16`, CoreDNS and persistent local-path storage were available.
- Pinned release CRDs/core/Kourier manifests were downloaded, their SHA-256 values
  recorded, and a local-only overlay applied. The installer snapshots the existing
  `mcp-demo` Deployment/Service/PVC/PDB resources before installation.
- Local overlay: one replica of each Knative control component and gateway,
  **50m CPU/96Mi memory requests**, and queue proxy **25m CPU/32Mi** requests.
  Bundled activator/webhook/gateway HPAs are bounded to one replica. Application
  KPA scaling remains enabled. Kourier exposes ClusterIP Services on this host;
  the tests forward the **gateway**, not an application pod.

Controller, webhook, autoscaler, activator, net-kourier-controller and gateway all
reached Ready. [Installation assets and hashes](evidence/phase3/knative-install.json),
[final Serving infrastructure](evidence/phase3/final-knative-infrastructure.json),
[final Kourier infrastructure](evidence/phase3/final-kourier-infrastructure.json).

No cluster or Docker volume was destroyed/recreated. Knative workloads were placed
in **`mcp-auto`**, preserving always-on workloads in **`mcp-demo`**. The runtime
quota change used `docker update`; container ID remained
`9907087e45e8f9dfd51f846673485fd5a6de6dbc8b677bfa66aa86f9b1045792` and start time
remained `2026-10-26T18:12:54.846142035Z`. The user's current context remained
**`kind-manufact-lab`**. All operations targeted the isolated Floci kubeconfig.

Final `mcp-demo` stateless/stateful/independent-fixture Deployments are **2/2**,
their Redis is **1/1**, and Compose stateless/stateful/Redis remain healthy.
[Preserved Phase 2 resources](evidence/phase3/final-phase2-workloads.json).

## Architecture and configuration

```text
Developer -> deployctl deploy PROJECT --config CONFIG
                 |
       Docker build -> Floci ECR push -> immutable digest
                 |
       Deployment backend OR Knative Service
                                  |
                       Configuration -> Revision -> generated Deployment
                                  |                     0..max MCP pods
Client -> Kourier -> Activator when needed -> queue proxy -> real MCP /mcp
                         |                      |                 |
                  buffers cold requests      concurrency       Redis
                                             metrics       (stateful only)
                                                |
                                          Knative KPA
```

Kourier and the activator remain reachable at zero application replicas. The
activator buffers the request while KPA creates replicas; queue proxies report
concurrency and proxy traffic to port 8000. MCP traffic never passes through the
CLI, Docker builder or image publisher. Stateful counters use the same external
Redis `INCRBY`/`GET` design as before.

The CLI continues to support arbitrary compatible projects with no hardcoded
sample names, tool names or Python entry file. `--config` selects a project-relative
JSON file. `autoscaling.enabled: true` selects Serving; default `mcp.json` files
retain the always-on Deployment backend. Both reuse image building/publishing,
environment/resource generation and managed/external Redis configuration.

| Configuration | Revision annotation |
| --- | --- |
| `minReplicas: 0` | `autoscaling.knative.dev/min-scale: "0"` |
| `activationReplicas: 2` | `activation-scale: "2"`; `initial-scale` is max(minimum, activation) |
| `maxReplicas: 10` | `autoscaling.knative.dev/max-scale: "10"` |
| `targetConcurrency: 2` | `autoscaling.knative.dev/target: "2"` |
| Concurrency KPA | metric `concurrency`, class `kpa.autoscaling.knative.dev` |
| Demo `windowSeconds: 20` | `autoscaling.knative.dev/window: "20s"` |

The full annotation prefix applies to activation/initial scale too. Target
concurrency is soft; `containerConcurrency=0` imposes no hard per-pod cap. Default
target utilization also affects scaling. `--replicas` is rejected for Serving;
use autoscaling bounds. No PDB pins a scaled revision above zero.

Knative supplies reserved `PORT`; the CLI declares `http1` port **8000** and retains
`/mcp`, `/health`, probes and the image's SIGTERM behavior. Route hosts are added to
`MCP_ALLOWED_HOSTS`. Local `routeDomain=mcp.test` matches Knative's domain config;
the clients use a Host header through the forwarded gateway. DNS/TLS provisioning
is not implied.

Published image digests avoid controller-side resolution of a localhost registry
tag. k3s still pulls actual image bytes through Floci's existing ECR mirror. The
Dockerfiles were not replaced. Echo/increment gained optional asynchronous
`delay_seconds` (0-10, default 0), preserving their normal results and tool names.
Increment still writes only Redis; its blocking Redis call runs in a worker thread.

Updates retain 100% traffic on the old ready revision until the desired candidate
is Ready, then verify the actual new route target. Failure restores the previous
configuration/route and cleans up the failed candidate. Candidate and recovery
waits have separate `--timeout`/`--rollback-timeout` settings. This is synchronous
local rollback, not a durable job or crash-recovery controller.

## Actual commands executed

Working directory: `C:\Users\Devi\Downloads\mcp-deployment-platform-starter`.
Docker/Kubernetes/networked test commands required elevated tool execution in the
managed sandbox. Python dependencies were installed only into workspace `.venv`.

```powershell
kubectl --kubeconfig .local/floci.kubeconfig get nodes -o wide
kubectl --kubeconfig .local/floci.kubeconfig get pods -A
kubectl --kubeconfig .local/floci.kubeconfig describe node
docker stats --no-stream
docker inspect --format '{{json .HostConfig}}' floci-aws-eks-mcp-platform
python -m pip --python .venv install PyYAML==6.0.3 --no-cache-dir
.\.venv\Scripts\python.exe scripts/install_knative.py --kubeconfig .local/floci.kubeconfig --local-profile

.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --config mcp.knative.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateful --config mcp.knative.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-v1
.\.venv\Scripts\python.exe tests/verify_autoscaling.py --kubeconfig .local/floci.kubeconfig

.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --config mcp.knative.local.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-local-v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateful --config mcp.knative.local.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-local-v1
docker update --cpus 4 floci-aws-eks-mcp-platform
.\.venv\Scripts\python.exe scripts/bound_local_knative_hpas.py --kubeconfig .local/floci.kubeconfig
.\.venv\Scripts\python.exe scripts/floci_setup.py --repository phase3-verified-probe
.\.venv\Scripts\python.exe tests/verify_knative_reliability.py --kubeconfig .local/floci.kubeconfig --registry localhost:4566/000000000000/us-east-1 --independent-name phase3-verified-probe --release phase3-v2-verified

docker compose up -d --build --wait --wait-timeout 120
.\.venv\Scripts\python.exe -m pytest -q -s tests --run-integration --run-compose-restart
.\.venv\Scripts\python.exe tests/verify_kubernetes.py --kubeconfig .local/floci.kubeconfig --verify-generic --output evidence/phase3/phase2-kubernetes-regression.json
.\.venv\Scripts\python.exe tests/verify_failed_rollout.py --kubeconfig .local/floci.kubeconfig --output evidence/phase3/phase2-rollback-regression.json
.\.venv\Scripts\python.exe tests/verify_redeploy.py --kubeconfig .local/floci.kubeconfig --registry localhost:4566/000000000000/us-east-1 --output evidence/phase3/phase2-redeploy-regression.json
.\.venv\Scripts\python.exe scripts/capture_phase3_final.py --kubeconfig .local/floci.kubeconfig
docker compose ps
kubectl config current-context
```

The reliability driver generated an alternate fixture manifest under `.local/`
to name the new service, retaining its original Docker build context. Its exact
CLI arguments, including the invalid digest and 35-second failure timeout, are
stored in `reliability.json`. The gateway helper invoked `kubectl -n kourier-system
port-forward svc/kourier 0:80 --address=127.0.0.1`, selecting a free port. Windows
test-owned launcher/child process trees were cleaned up.

## Regression outcomes and fixes

- Final full pytest run: **29 passed in 17.69s**, including all original eight
  Phase 1 tests, existing deployment cases and new autoscaling/race cases.
  The Compose stateful restart counter remained **7**.
- Phase 2 live ClusterIP MCP/cross-replica Redis/pod replacement: **PASS**.
  [Evidence](evidence/phase3/phase2-kubernetes-regression.json).
- Phase 2 unavailable-image rollback: **PASS**, CLI exit 1 as expected, old pod
  UIDs retained and all 21 sampled ready endpoint counts were two.
  [Evidence](evidence/phase3/phase2-rollback-regression.json).
- Phase 2 replica override/idempotency: **3/3, then 2/2, then unchanged 2/2**.
  The exact repeated command retained generation **8**, image and pod UIDs.
  [Evidence](evidence/phase3/phase2-redeploy-regression.json).

Live tests exposed and fixed these implementation issues:

1. Knative rejects explicit `PORT` environment values; the backend now lets
   Knative supply it from the declared container port.
2. A Deployment scale-down can report rollout completion before exact status
   counts settle. The CLI now waits for current generation and exact counts,
   avoiding a false rollback during transient scale-down status.
3. Knative Service observed generation can advance while its latest-created name
   still references the old revision. Candidate selection now matches desired
   template fields, and promotion verifies actual status traffic destination.
4. A short injected candidate timeout also truncated restoration. Recovery now
   has its own timeout; failed unrouted candidates are deleted after recovery.
5. Snapshot restoration can put Knative admission annotations into last-applied
   metadata. Subsequent updates preserve the immutable creator and admission-owned
   last-modifier values.
6. Windows' kubectl shim left a port-forward child behind. The helper now uses an
   ephemeral port and terminates only its own process tree.
7. The redeploy driver now compares identical command invocations for idempotency,
   rather than assuming equal desired replica counts imply an identical prior
   operation after rollback normalization.

## Failed/excluded trials — not passed results

The initial unbounded local control-plane HPA profile was insufficient for
simultaneous traffic. It is corrected in both the installer and in-place helper.

| Trial | Recorded outcome |
| --- | --- |
| Simultaneous max-ten app caps, two-CPU runtime | Pods reported **Insufficient CPU**. Test process tree stopped; no complete per-request result was produced. [Scheduling evidence](evidence/phase3/continuity-capacity-failure-events.json). |
| Max-four tuning run, runtime adjusted during the run, infrastructure HPAs still unbounded | **FAIL**: 1296/1297 successes for each service; one transport ExceptionGroup each. Recovery's short timeout failed even though the old route remained. [Full record](evidence/phase3/reliability-tuning-run.json). |
| Subsequent run while infrastructure HPAs expanded to 14 activators | **FAIL**: stateless 378/561; stateful 0/561. New service could not schedule. HPA bounds were corrected during recovery. [Full record](evidence/phase3/reliability-hpa-tuning-run.json). |
| Stable-profile metadata regression | New service creation succeeded and traffic was 174/174 for each app, but v2 was rejected by the immutable creator annotation. Overall **FAIL**, followed by the metadata fix and full rerun. [Record](evidence/phase3/reliability-metadata-regression.json). |

These failures prohibit claiming zero downtime over the entire development
session. The final 648-per-service run alone observed zero request failures.

## Simulator limitations and remaining production work

- This is one local Docker host and one Floci k3s node. Zone labels do not
  represent actual AZ redundancy. No multi-AZ HA, thousands-of-servers test,
  multi-region deployment or Karpenter was implemented or claimed.
- Simultaneous max-ten caps exceed this node's scheduler headroom with the
  preserved Phase 2 workloads. The final continuity test therefore used max four.
  The four-CPU runtime override is local tuning, not extra Kubernetes allocatable
  capacity or node autoscaling; an emulator/container recreation may require
  reapplying it. Production needs capacity planning and a separate node design.
- Single-replica infrastructure HPAs are explicitly a local functional profile,
  not production control-plane availability settings. The host is below Knative's
  recommended single-node installation sizing. The reported timings are not SLOs.
- Redis remains one always-on process/PVC per namespace. MCP scale-to-zero does
  not eliminate Redis, gateway, activator or controller costs. Redis HA, backup,
  restore, node/storage failure and multi-zone persistence were not tested.
- The tests cover sessionless JSON-response MCP Streamable HTTP. Long-lived SSE,
  resumable sessions, client notifications and long tool-duration timeout policies
  need additional protocol and ingress testing.
- Production requires DNS, TLS, authentication/authorization, ECR IAM credential
  management, secrets, quotas, build isolation/signing, durable deployment jobs,
  per-service concurrency/capacity tuning, observability and alerting.
- `/health` still reports application process health rather than Redis dependency
  health. Normal CLI rollback is tested; process crashes, API outages, concurrent
  operators and all failure modes are not covered.

No remaining blocker prevents the verified bounded local workflow. Simultaneous
max-ten operation on this host remains an explicit capacity limitation.

Primary references checked:

- [Pinned Serving 1.23.0 Kubernetes minimum-version code](https://github.com/knative/serving/blob/knative-v1.23.0/vendor/knative.dev/pkg/version/version.go)
- [Official Serving/Kourier installation](https://knative.dev/docs/install/yaml-install/serving/install-serving-with-yaml/)
- [Scale bounds, initial scale and activation scale](https://knative.dev/docs/serving/autoscaling/scale-bounds/)
- [Knative autoscaler configuration](https://github.com/knative/serving/blob/knative-v1.23.0/config/core/configmaps/autoscaler.yaml)
