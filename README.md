# MCP deployment platform

Two real Python MCP servers, one container contract, and one Kubernetes pipeline
for arbitrary compatible projects. Both examples use official MCP SDK `2.3.0`,
sessionless Streamable HTTP at `/mcp`, `/health`, and container port 8000.

- Stateless: `echo`, `add_numbers`; results depend solely on arguments.
- Application-stateful: `increment_counter`, `get_counter`; shared Redis stores
  counters using atomic `INCRBY`. No counter state lives in MCP pod memory.
- `deployctl.py`: validate configuration, build, publish/load, provision Redis
  when needed, apply Deployment/Service/PDB, wait for readiness, and recover a
  failed application rollout.

See [Phase 1 evidence](PHASE1_REPORT.md), [Phase 2 evidence](PHASE2_REPORT.md), and
the [project configuration contract](DEPLOYMENT_CONTRACT.md).

Phase 3 adds optional Knative Serving/Kourier autoscaling; see
[Phase 3 measurements](PHASE3_REPORT.md). Existing `mcp.json` configurations keep
the always-on Deployment backend.

## Local web UI

The optional FastAPI UI uses simple HTML/CSS at **http://127.0.0.1:8080**.
It reads real Kubernetes status and runs the existing `deployctl.py` in
background jobs. The UI does not install or recreate infrastructure.

Use a separate virtual environment so UI packages do not alter the working MCP
server environment:

```powershell
python -m venv .venv-ui
python -m pip --python .venv-ui install -r requirements-ui.txt
$env:MCP_UI_MAX_JOBS = '2' # 1, 2 or 3; default 2 on the shared local host
.\.venv-ui\Scripts\python.exe -m ui --port 8080
```

Leave this terminal running and open **http://127.0.0.1:8080** in a browser.
The launcher binds only to IPv4 loopback; no LAN/public listener is exposed.
It uses the existing `.local/floci.kubeconfig`, `.venv` deployment interpreter
and Floci registry `localhost:4566/000000000000/us-east-1`. Optional trusted
operator settings are `MCP_UI_KUBECONFIG`, `MCP_UI_DEPLOY_PYTHON` and
`MCP_UI_REGISTRY`. On Linux/macOS use `.venv-ui/bin/python`.

1. Select **Deploy project** and choose a registered project, or use **Import source** for your own server.
2. Choose **Always-on** (`mcp-demo`) or **Knative** (`mcp-auto`). The existing
   project configuration is previewed without environment values.
3. Choose **Reuse an existing published image**, or **Build Dockerfile and
   publish to Floci ECR**. Builds use the same CLI/Dockerfiles as terminal
   deployments. A new release version creates a versioned image tag on builds.
4. Click **Deploy**. The job page opens immediately with its ID and timestamped
   **QUEUED → BUILDING → PUSHING → DEPLOYING → VERIFYING → SUCCEEDED/FAILED**
   transitions, actual command arguments, live CLI logs and final exit code.
   Image reuse skips build/push stages. The page polls status/logs every second.
5. Open **Inspect Kubernetes deployment** for actual replica counts, health
   conditions, endpoint, image, application logs and a real redeploy action.

**My MCP servers** separates developer deployments, test probes and unregistered
runtimes using explicit metadata. It shows source provenance, namespace/name,
image/revision, ready/desired counts, health, last verified deployment and latest
UI job. Details include retained rollout history, related events and MCP pod logs.
Partial readiness is never labeled Healthy. Use **Call reviewed read-only tool**
for one actual MCP check with its response, timing and error; it never increments
a counter or retries an uncertain operation. These checks are not general request analytics.

Dashboard status is cached for at most five seconds and includes its observation
time. Refresh explicitly to read it again. Knative zero is shown as a ready
route with zero application replicas; readiness is not a guarantee of Redis
health. Details use the routed revision's image rather than assume the latest
ready revision receives traffic after rollback. A failing job remains failed
even when the CLI successfully restores a healthy previous deployment.

Example sources are in [ui/projects.json](ui/projects.json). Imported projects
are registered under `.local/ui/imports/` with provenance in `.local/ui/sources.json`.
Use a complete ZIP, an exact approved GitHub URL from
[ui/trusted_repositories.json](ui/trusted_repositories.json), or a copy of an
existing example template. GitHub sources are pinned to a resolved commit; ZIPs
have a recorded SHA-256. Without mcp.json, guided configuration requires an
explicit state model. New imports reserve a unique name across both namespaces.
Build paths must stay within the staged source; alternate ports/endpoints are
rejected because the existing contract is 8000 and /mcp. No executable or arbitrary
shell command can be supplied through the browser.
Subprocesses use argument arrays with `shell=False`. Local Host/client checks,
same-origin POST checks and form tokens protect this unauthenticated demo.
Image overrides are restricted to the selected project's configured ECR repo.
Knative reuse requires an image digest, avoiding controller-side resolution of
localhost registry tags; the build/publish CLI supplies a digest automatically.

Jobs use a bounded worker pool in one UI process: `MCP_UI_MAX_JOBS=1`, `2`
(default), or `3`, with eight waiting jobs; a full queue returns HTTP 429.
Conflicting namespace/name updates and shared managed-Redis updates retain
submission order. Unrelated jobs can pass a blocked conflicting job.
Metadata and redacted logs live in ignored `.local/ui/jobs/`. Each job retains
three rotating 1-MiB segments by default (`MCP_UI_JOB_LOG_BYTES`, 65536–16777216
bytes per segment); the browser shows the last 256 KB. Metadata/logs have no
automatic expiry, so archive old jobs/images to manage total disk use. Output
lines over 16 KiB are omitted. Redaction handles known environment secrets,
credential fields, authorization headers, URL credentials and private-key blocks;
it cannot guarantee sanitizing arbitrary hostile application output.
Do not run another UI instance or deploy the same app concurrently from the
terminal: ownership is local to this process, not a distributed lock.
On graceful shutdown the UI lets queued/running CLI work finish. After an
unexpected restart, unfinished jobs have legacy status **interrupted** and stage
**FAILED**;
inspect the real cluster and any remaining CLI process before retrying. This
is a local worker, not a durable production job system or authenticated control
plane. Delete/scale controls and a manual rollback UI are not implemented.

ClusterIP MCP addresses are internal to Kubernetes. Local Knative `mcp.test`
routes also require the existing Kourier/Host-header access described below;
the UI does not expose a new public ingress. Manual Test MCP checks create a
temporary loopback port-forward; production MCP traffic remains independent of UI jobs.

For the real third-project walkthrough, build limits, security boundaries and
monitoring steps, see [BYO_MCP_GUIDE.md](BYO_MCP_GUIDE.md). Download the complete
[text-tools sample](examples/text-tools-mcp) from **Import source** in the UI.
All UI build containers have one CPU/1 GiB limits and a five-minute command
timeout, using Docker's opt-in deprecated legacy builder on this existing engine.
Each job uses a private, bounded source snapshot and temporary directory, with
runtime configuration outside the Docker context. A job UUID image label makes
each UI build's image reference unique. Plain terminal CLI builds retain their
existing defaults. Docker daemon work, image disk use and the aggregate host are
not bounded by the build-container limits. This is trusted
local code execution, not a sandbox for untrusted uploads; production needs auth,
isolated workers and stronger resource/network/storage isolation.

Phase 5 results: [UI_FINAL_REPORT.md](UI_FINAL_REPORT.md). Focused checks:

```powershell
.venv-ui/Scripts/python.exe -m pytest tests/test_ui_jobs.py tests/test_ui_sources.py tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp .local/pytest-ui-jobs-new-run
.venv/Scripts/python.exe -m pytest tests/test_ui_sources.py tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp .local/pytest-ui-new-run
.venv-ui/Scripts/python.exe tests/verify_ui_guards.py
# Read-only browser verification of the already imported evaluation deployment:
.venv-ui/Scripts/python.exe tests/verify_ui_final.py --resume evidence/ui-final/20261026T220451Z
```

For the controlled parallel-build demonstration, stop an **idle** UI and start
it in its terminal with three workers. Keep only one API process:

```powershell
$env:MCP_UI_MAX_JOBS = '3'
.\.venv-ui\Scripts\python.exe -m ui --port 8080
```

In another PowerShell terminal, with Floci/ECR and the existing registered
`manufact-text-tools` and intentional `manufact-invalid-build` sources available:

```powershell
.\.venv-ui\Scripts\python.exe tests/verify_deployment_concurrency.py --kubeconfig .local/floci.kubeconfig
```

This deliberately submits three independent real builds, two serialized updates
to the stateless service and one expected invalid-Dockerfile failure. Read-only
MCP `echo`/`get_counter` traffic runs through ClusterIP Services in a temporary
client pod, including 45 seconds after the jobs finish. The current demo fixture
expects saved Redis key `ui_persist_a59837a6a3` to remain 13. No increments or
request retries are performed. Raw requests, p95, queue/stage observations,
per-job logs and resource snapshots go to `evidence/concurrency/TIMESTAMP/`.
This is opt-in and changes application image versions; it does not recreate the
cluster or run Full demo. See [CONCURRENCY_REPORT.md](CONCURRENCY_REPORT.md) for
executed results and limitations.

The no-argument `verify_ui_final.py` flow performs one-time imports and a real
build/deployment with fixed names. Those names are now reserved; do not repeat
that onboarding flow to recheck existing services. The resume mode sends only
read-only MCP checks and reads runtime/log/status evidence; it does not deploy.

Optional real-browser verification uses an **already-installed Microsoft Edge**
(no browser download or cluster setup):

```powershell
python -m pip --python .venv-ui install -r requirements-ui-test.txt
.\.venv-ui\Scripts\python.exe tests/verify_ui_browser.py --output evidence/ui/my-run
```

This opt-in test performs real application updates: both example image builds
and deployments, redeploy, one Knative deployment, a deliberately missing-image
rollout with recovery, and before/after MCP calls through ClusterIP Services.
It is not the Full demo. Use a fresh evidence directory. See
[UI_DEMO_REPORT.md](UI_DEMO_REPORT.md) for actual executed results and limits.

## Demo for Enrico: existing environment

Run from this repository in Windows PowerShell 5.1 or PowerShell 7. The existing
Docker/Floci real-mode cluster, isolated `.local/floci.kubeconfig`, Python venv,
published ECR images, Knative/Kourier and Redis volumes must already exist.
The demo verifies them and stops on a blocker; it installs no infrastructure,
does not recreate the cluster, and does not rebuild images.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Quick
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Full
```

Quick confirms infrastructure health, two ready replicas/endpoints per app,
actual MCP echo/add and counter calls, shared Redis across replicas and pod
replacement, then rolls a published v1 image to v2 and checks MCP again. It
reuses existing resources and resets the stateless deployment to a published
historic v1 when needed so repeated demos still show a real rollout.

Full includes Quick, sequential sustained-concurrency scale-up above two,
automatic zero application pods, real MCP cold wake and persisted Redis state.
It then runs the existing 2.5 requests/sec/service continuity test with bounded
max-four profiles: independent fixture redeployment, new stateless revision,
stateful pod deletion, and intentional failed-image rollback. It reuses images;
the independent fixture is an existing third service. The expected failed
candidate is a rollback test, not a hidden failure. Actual traffic failures
cause the Full demo to fail.

Allow roughly 6-9 minutes for Quick and 20-25 minutes for Full on this host;
scaling/rollout times vary. Both modes mutate application deployments and
delete stateful **MCP** pods as part of recovery verification. They retain Redis
and its PVC. Only one operator should deploy these demo services at a time.
The always-on apps finish at two replicas; Knative apps naturally return to
zero when idle. Controllers, gateway and Redis stay running.

Each step prints PASS/FAIL and the process exits 0/1. Complete command logs,
request-level continuity measurements, scaling samples, cold-start results and
`summary.json` go in a new `evidence/demo/<UTC timestamp>-<mode>/` directory.
Historical Phase 3 evidence is preserved. To select paths explicitly:

```powershell
.\demo.ps1 -Quick -Kubeconfig .local/floci.kubeconfig -OutputDirectory evidence/demo/my-quick-run
```

Use a fresh output directory for each run. Full requires at least the existing
6 GiB Docker memory budget; the documented host has 8 logical CPUs, about
7.6 GiB Docker RAM, a four-CPU k3s runtime quota, and 1930m scheduler allocatable
CPU. If capacity or infrastructure health is insufficient, keep the failure
evidence and restore the existing environment before retrying.

Run all regression tests separately, including live Compose MCP/restart checks:

```powershell
docker compose up -d --no-build --wait --wait-timeout 120
.\.venv\Scripts\python.exe -m pytest -q -s tests --run-integration --run-compose-restart
```

See [architecture](ARCHITECTURE.md), [cost comparison](COST_COMPARISON.md), and
[actual final demo outcomes](FINAL_DEMO_REPORT.md). The cost model's 1,000-server
scenario is hypothetical. Local replicas do not establish production HA.

Latest Full verification returned **FAIL** for one HTTP 502 during revision
rollout (1 failure in 1,204 continuity requests). Scaling, cold activation,
Redis persistence and rollback passed. Quick and all 29 regressions passed;
see the report for the retained failure evidence.

While the demo runs, explain four points: developers supply a Dockerfile and
manifest; the same CLI deploys either server type; live MCP requests use the
gateway/Services independently of the deployment process; Redis retains data
when application pods disappear. Quick shows the always-on backend. Full adds
Knative activation and measured traffic during deployment and recovery.

## Compose development and Phase 1 regression tests

Prerequisites: Docker Desktop/Engine with Compose, Python 3.11+.

```powershell
python -m venv .venv
python -m pip --python .venv install -r requirements-dev.txt
docker compose up -d --build --wait --wait-timeout 120
.\.venv\Scripts\python.exe tests/smoke_mcp.py
.\.venv\Scripts\python.exe -m pytest -q -s tests --run-integration --run-compose-restart
```

On Linux/macOS use `.venv/bin/python` instead. Stateless is available at
`http://localhost:8001/mcp`, stateful at `http://localhost:8002/mcp`. Both health
routes are `/health`. Without integration flags, tests needing live services or
the deliberate Compose restart are skipped. Compose runs one instance per app.

Both Dockerfiles use direct Python execution as a non-root user, SIGTERM,
matching health checks and defaults `HOST=0.0.0.0`, `PORT=8000`. Additional
configuration is `MCP_ALLOWED_HOSTS` and, for stateful, `REDIS_URL`. Compose Redis
uses AOF and a named volume. `/health` checks process health only.

## Preferred Kubernetes environment: Floci EKS and ECR

Prerequisites: Docker and `kubectl`. The helper uses boto3 instead of requiring
the AWS CLI. It writes an isolated admin-certificate kubeconfig under ignored
`.local/`, without changing the current Kubernetes context. This is local emulator
setup, not production EKS authentication.

```powershell
python -m pip --python .venv install -r requirements-platform.txt
docker compose -f compose.floci.yaml up -d
.\.venv\Scripts\python.exe scripts/floci_setup.py --repository stateless-demo --repository stateful-demo --repository contract-probe
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateful --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version v1
kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo get deploy,pods,svc,pvc,pdb
.\.venv\Scripts\python.exe tests/verify_kubernetes.py --kubeconfig .local/floci.kubeconfig
```

The Compose file explicitly selects Floci **real mode**, a shared Docker network,
persistent emulator storage, local-path storage support and path-style ECR URIs.
Path-style URIs avoid Docker Desktop wildcard `*.localhost` DNS issues. Floci
injects the ECR mirror used by k3s/containerd; a successful host push alone is
insufficient evidence that Kubernetes can pull an image.

For an existing cluster without a StorageClass, the helper installs the pinned
official local-path provisioner with its data under the persistent k3s Docker
volume. Fresh clusters use Floci's bundled provisioner. Local storage depends on
the Docker host and is not production AWS EBS or a redundant Redis service.

Create an ECR repository for each additional project's `mcp.json` name by rerunning
the helper with `--repository NAME`. The deployment CLI itself is registry-neutral.

## Generic projects, replicas, versions and recovery

Any directory containing `Dockerfile` and `mcp.json` can use the same implementation.
It need not use the examples' language, filenames or tools. See
[DEPLOYMENT_CONTRACT.md](DEPLOYMENT_CONTRACT.md) for all configuration fields.

The independent fixture demonstrates custom tools, entrypoint and environment:

```powershell
.\.venv\Scripts\python.exe deployctl.py deploy tests/fixtures/compatible_project --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase2-generic-v1 --replicas 2
.\.venv\Scripts\python.exe tests/verify_kubernetes.py --kubeconfig .local/floci.kubeconfig --verify-generic
.\.venv\Scripts\python.exe tests/verify_redeploy.py --kubeconfig .local/floci.kubeconfig --registry localhost:4566/000000000000/us-east-1
.\.venv\Scripts\python.exe tests/verify_failed_rollout.py --kubeconfig .local/floci.kubeconfig
```

The last command deliberately deploys a missing stateless image and expects the
CLI to exit nonzero after restoring the previous healthy application. The redeploy
check changes the fixture to three replicas, restores two, then repeats the
identical deployment. Run these checks with one operator per application.

Images use `VERSION-FULL_BUILD_ID` tags. Different build bytes cannot silently
replace a previous image under the same tag. Default prefix `build` and unchanged
cached builds allow repeated deployments without a new pod template. An explicit
version change triggers a new rollout. All apps default to two replicas and use
CPU/memory requests and limits, startup/readiness/liveness probes,
`maxUnavailable=0`, `maxSurge=1`, and a 30-second termination grace period.

## kind fallback

Use this adapter if Floci is unavailable. It uses the same resource generator and
rollout/recovery path; only image publishing changes. The adapter selects the
`kind-mcp-demo` context automatically, even if another cluster is current.

```powershell
kind create cluster --name mcp-demo
python deployctl.py deploy servers/stateless --kind-cluster mcp-demo --version v1
python deployctl.py deploy servers/stateful --kind-cluster mcp-demo --version v1
```

For interactive host access use separate port-forward terminals. Ports 18001 and
18002 avoid conflicts with the still-running Compose services:

```powershell
kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo port-forward svc/stateless-demo 18001:8000
kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo port-forward svc/stateful-demo 18002:8000
```

Port-forward selects a pod behind a Service; it does not demonstrate ClusterIP
load balancing. The Kubernetes verification script instead runs SDK clients inside
Jobs against Service DNS, and uses exact pod calls to prove shared Redis state.

## Knative autoscaling and scale-to-zero

Install pinned Serving/Kourier 1.23.0 on the existing Floci Kubernetes 1.34+ cluster.
The explicit local profile reduces control-plane requests for this small node;
it is not a production installation profile.
It also bounds the bundled activator/webhook/gateway HPAs to one replica; only
application revisions are allowed to expand in this profile.

```powershell
python -m pip --python .venv install -r requirements-platform.txt
.\.venv\Scripts\python.exe scripts/install_knative.py --kubeconfig .local/floci.kubeconfig --local-profile
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateless --config mcp.knative.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-v1
.\.venv\Scripts\python.exe deployctl.py deploy servers/stateful --config mcp.knative.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --push-registry localhost:4566/000000000000/us-east-1 --version phase3-v1
.\.venv\Scripts\python.exe tests/verify_autoscaling.py --kubeconfig .local/floci.kubeconfig
```

That test uses real MCP tools with optional three-second asynchronous delays,
eight concurrent clients and a 20-second scaling window. It records counts,
request outcomes, time until all MCP pods disappear, cold latency and Redis state.
The two samples accept `delay_seconds` (0-10 seconds) in echo/increment; default
behavior remains immediate.

For simultaneous reliability testing, first deploy both using
`--config mcp.knative.local.json --version phase3-local-v1` instead. That explicit
local profile caps each at four. Then run:

```powershell
docker update --cpus 4 floci-aws-eks-mcp-platform
.\.venv\Scripts\python.exe scripts/floci_setup.py --repository phase3-verified-probe
.\.venv\Scripts\python.exe tests/verify_knative_reliability.py --kubeconfig .local/floci.kubeconfig --registry localhost:4566/000000000000/us-east-1 --independent-name phase3-verified-probe --release phase3-v2-verified
```

This schedules 2.5 real MCP calls/second per service through Kourier, deploys an
independent project, rolls out v2, deletes a stateful pod and injects a failed
rollout. Results are saved under `evidence/phase3`. Gateway forwarding targets
Kourier, never an application pod, including at zero replicas. The test allocates
a free port and cleans up its own process tree. Production ingress, DNS, TLS and
authentication are separate work.

The CPU command is an in-place local runtime-quota adjustment used for the
reported continuity test. It does not restart k3s, increase scheduler allocatable
CPU or add nodes. Older installations made before the HPA profile correction can
apply `scripts/bound_local_knative_hpas.py --kubeconfig .local/floci.kubeconfig`.
Use a fresh independent name/repository and release when repeating the test to
exercise a new service and a new revision; otherwise the pipeline is idempotent.

## Architecture and scope

The CLI is the control plane. Live MCP requests travel through ClusterIP Services
directly to the MCP pods and Redis, independently of Docker builds and deployment
operations. Deployment execution is synchronous and intended for a trusted local
operator. No public deployment API, build isolation, tenant controls or distributed
deployment lock is implemented.

Floci's real Kubernetes runtime still shares one local Docker host. Two replicas,
rolling updates and PDBs do not establish HA or zero downtime. PDBs govern voluntary
evictions and do not protect every failure. Managed Redis has one instance and a
PVC. Production would require a separate availability, identity, networking,
storage, secrets and observability design. Multi-region and node autoscaling are
outside this phase; no Karpenter is installed.
