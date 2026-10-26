# Phase 4: local MCP deployment UI

Verified **2026-10-26**, using the existing Floci real-mode EKS/k3s and ECR.
The UI is running at **http://127.0.0.1:8080**. Real Microsoft Edge browser
verification passed: both MCP examples were built/published/deployed from HTML
forms, a real redeploy and Knative deployment succeeded, actual MCP tools worked
afterward, and a deliberately failing deployment displayed its error/recovery.

No infrastructure was installed, rebuilt or recreated. The original MCP
servers, Dockerfiles, deployment CLI, Redis integration and existing test
assertions were not edited. Full was not run. The old 29-test result remains
historical; this phase ran only the short 22-unit-test subset and targeted UI/MCP
checks described below.

## Run and use

From the repository root:

```powershell
python -m venv .venv-ui
python -m pip --python .venv-ui install -r requirements-ui.txt
.\.venv-ui\Scripts\python.exe -m ui --port 8080
```

Open **http://127.0.0.1:8080**, select **New deployment**, choose an allowlisted
project/backend/image source/version, and click **Deploy**. Follow the returned
job page, then select **Inspect Kubernetes deployment**. See [README](README.md)
for configuration, image reuse, shutdown behavior and constraints.

For this session the server was launched with hidden `Start-Process`, stdout and
stderr redirected to `.local/ui-server.*.log`; `.local/ui-server.pid` records
the UI launcher PID. Only the new UI was restarted, after every job finished,
to load final input validation and verify persisted history. No MCP, Redis,
Floci or Kubernetes process was restarted by that UI restart.

## Implementation

- FastAPI **0.162.0**, Starlette **1.10.0**, Uvicorn **0.54.0**, Jinja2 **3.1.6**,
  simple HTML forms/CSS, and small browser scripts for configuration preview
  and job-log polling. No React, UI build system or additional hosting service.
- [ui/app.py](ui/app.py) reads actual Kubernetes resources using explicit
  kubeconfig/namespace arguments. Deployments, Services, Knative Services and
  Revisions supply the status, endpoints and images. Redis infrastructure is
  excluded from the MCP workload list. Unknown cluster status is shown as an
  error, not an invented healthy state.
- Deployment operations are exclusively argument-array subprocess invocations
  of the existing **deployctl.py**. Build/ECR publication, Redis provisioning,
  Kubernetes apply/wait and rollback remain in that pipeline.
- One asynchronous queue delegates CLI execution to a worker thread. Forms
  return a job URL immediately; `POST /api/jobs` returns **202** with a job ID.
  Status is queued/running/succeeded/failed/interrupted, based on actual worker
  state and CLI exit code. Complete stdout/stderr and metadata persist under
  ignored `.local/ui/jobs/`; the browser polls and shows real errors.
- [ui/projects.json](ui/projects.json) is the trusted local allowlist for both
  examples and the independent compatible fixture. Each backend reads its
  existing project configuration; no example-specific deployment code exists
  in the UI adapter. The namespaces are fixed to mcp-demo/mcp-auto for this demo.
- Details distinguish routed Knative images from latest-ready metadata after
  rollback. Zero desired/ready counts represent the route/application replica
  state; terminating pods can still drain. Conditions and observation time are
  visible, and status reads are cached at most five seconds.

The template and lifecycle APIs were checked against the official
[FastAPI template documentation](https://fastapi.tiangolo.com/advanced/templates/)
and [lifespan documentation](https://fastapi.tiangolo.com/advanced/events/).
UI dependencies are isolated in `.venv-ui`; the MCP interpreter remains `.venv`.

## Actual browser deployment results

Executed with the **already-installed Microsoft Edge 156.0.4314.42**, using
Playwright 1.64.1. No browser binaries were downloaded or installed.

```powershell
python -m pip --python .venv-ui install -r requirements-ui.txt -r requirements-ui-test.txt
.\.venv-ui\Scripts\python.exe tests/verify_ui_browser.py --output evidence/ui/run-1
```

Browser verification ran **21:04:49–21:09:57 UTC** and returned **PASS**, exit 0.

| Browser action | Job ID | CLI result | Worker duration |
| --- | --- | --- | ---: |
| Stateless Dockerfile build, ECR push, always-on deploy | `571df16d246c4e67aeb2c28b0b304374` | exit 0, succeeded | 58.90s |
| Stateful Dockerfile build, ECR push, always-on deploy | `f79112a052434e3780985d9f7f944bf1` | exit 0, succeeded | 50.36s |
| Details-page redeploy of published stateless image | `3763393ce3274bdfb0ba00ea105b0e40` | exit 0, succeeded; idempotent always-on image reuse | 14.27s |
| Stateless Knative deployment using existing digest | `045d778716644f98bf3e2db9947efbe5` | exit 0, succeeded | 31.70s |
| Deliberately nonexistent stateless image, 35s rollout timeout | `bf83eeb111744f279a6929154bb454ce` | **exit 1, failed**, expected | 50.01s including rollback |

Every recorded command uses the same `.venv` interpreter and `deployctl.py`.
The first two contain `--push-registry` and actual Docker build/push output.
The others use the existing `--image` path. Their screenshots show the browser
job pages, not simulated deployment results. The four new-deployment form
submissions returned their job pages in **0.215–0.298 seconds**, without waiting
for rollout. There were **zero browser JavaScript errors**.

The failed image job stayed visibly **failed** with CLI exit 1 and an error
banner. Its log contains **`ROLLBACK OK: previous healthy deployment restored`**.
The job did not turn green just because recovery succeeded. This was an
intentional image-pull failure, separate from the historical Full HTTP 502.

Both always-on example Deployments finished at **2/2 ready** with new versioned
image references in the existing registry:

- Stateless: `stateless-demo:ui-a59837a6a3-stateless-always-on-cf69305bf48d2e09b3ebc040587e922eef0de4cc7befbea441068dc85925bf6e`
- Stateful: `stateful-demo:ui-a59837a6a3-stateful-always-on-d9ca459212419eb47b78950d58acd9a5fc7cd3f6d09a6c1ca73823d6ec14e510`

## Real MCP and availability checks

The browser verifier reused [tests/kubernetes_client.py](tests/kubernetes_client.py)
inside short-lived Kubernetes test Jobs, calling actual ClusterIP Services
before and after deployment. It verified:

- Stateless echo and add_numbers, and stateful get/increment across fresh SDK
  connections; these were actual MCP calls, not health-only REST checks.
- Independent compatible fixture multiply remained available and returned 42.
- Counter **`ui_persist_a59837a6a3`**, seeded at **13** before updates, still read
  **13** afterward through MCP. Application Redis data was retained.
- **29 sampled Kubernetes inventory reads** during browser operations showed
  at least **two ready replicas** for stateless-demo, stateful-demo and
  contract-probe on every sample.
- The independent project's pods and Redis pod kept their original UIDs.
- Application logs were loaded through the details-page button.

The new Knative stateless deployment was also checked separately using the
existing official MCP client helper through **Kourier with the route Host
header**: echo returned `deployed-through-ui`, add_numbers returned **42**, and
the sampled application count was **2 desired / 2 ready**. These two calls were
a smoke check, not a new load/scaling/Full run.

Replica samples plus before/after MCP success do **not** establish zero failed
requests throughout every rollout. This phase did not run a continuous MCP
traffic generator or re-evaluate the historical Knative 502.

Test Jobs/ConfigMap created by the verifier were cleaned up afterward; existing
application resources and Redis PVCs were retained.

## Validation, restart and unit checks

The live HTTP/browser checks rejected these requests without creating jobs:

| Input | Actual HTTP result |
| --- | ---: |
| Missing CSRF/form token | 403 |
| Project path traversal | 422 |
| Extra executable/project path field | 422 |
| Shell-like version string | 422 |
| Image from another repository | 422 |
| Cross-origin POST | 403 |
| Foreign Host header | 400 |
| Local Knative tag override instead of published digest | 422 |

The final two checks and persisted job history were checked after the UI-only
restart. All five earlier job results and logs survived. A further real API
redeploy, job **`63c115d9c27f4e4da63824dad14a5e0b`**, returned **202 in 0.0165s**,
then completed through deployctl with exit 0. The image was reused idempotently.

The short existing unit subset passed **22 tests in 0.85s**:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .local/ui-pytest-63297bbbe09c4298b7c827f3f5ed4a0d tests/test_deployctl.py tests/test_manifest.py
```

That temp directory was verified new and inside this workspace before execution.
The initial sandboxed attempt against pytest's default system temp/cache paths
had **5 passed / 17 setup errors** due to Windows directory permissions. No
application assertion failed in that attempt; rerunning with a permitted fresh
temp directory resolved it without changing the tests or pipeline.

## Evidence

- [Browser results, commands, samples and validation outcomes](evidence/ui/run-1/summary.json)
- [Final desktop dashboard](evidence/ui/run-1/dashboard-final.png)
- [Mobile dashboard](evidence/ui/run-1/dashboard-mobile.png)
- [Failed-job screenshot](evidence/ui/run-1/bf83eeb111744f279a6929154bb454ce.png)
- [Actual failed CLI log and rollback](evidence/ui/run-1/bf83eeb111744f279a6929154bb454ce.log)
- [Before-deployment MCP results](evidence/ui/run-1/mcp-before.log)
- [After-deployment MCP and retained counter](evidence/ui/run-1/mcp-after.log)
- [Knative MCP smoke result](evidence/ui/knative-mcp.json)
- [Restart history, validation and 202/API redeploy](evidence/ui/api-checks.json)
- [Short unit-test output](evidence/ui/unit-tests.log)
- [Final read-only cluster status and unchanged runtime](evidence/ui/final-status.json)

The cluster runtime remains
`9907087e45e8f9dfd51f846673485fd5a6de6dbc8b677bfa66aa86f9b1045792`, with original
start time **2026-10-26T18:12:54.846142035Z**. The private kubeconfig was not
copied into evidence. No Floci, Knative, Redis or cluster installation script ran.

## Limits

This is a trusted localhost demonstration with no production login, TLS,
multi-user authorization or durable external job service. One UI process
serializes its own jobs; it cannot lock out an independent CLI operator.
Interrupted work is marked honestly and needs operator inspection; automatic
crash recovery and two concurrent UI processes were not tested.

New projects require trusted allowlist configuration, compatible Dockerfile and
manifest, and an existing accessible registry repository. Arbitrary browser
uploads, free-form executable paths, delete/scale actions, manual rollback UI,
MCP request proxying and public DNS were not added. Knative image reuse requires
digests for this local registry setup. Cold starts and the existing simulator,
single-host, single-Redis and historical rollout limitations still apply. No
production HA, uninterrupted traffic guarantee or thousand-server claim follows
from these results.
