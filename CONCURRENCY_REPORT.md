# Live job progress and bounded parallel deployment verification

Executed 2026-10-26 against the existing Floci real-mode EKS/k3s/ECR environment.
The UI changes work, all five intended deployments succeeded, and the independent
invalid build failed correctly. **The continuous-traffic assertion failed:
1,195/1,198 measured MCP requests succeeded; three failed.** No zero-downtime
claim is supported by this run.

## Implementation

The existing FastAPI adapter still executes `deployctl.py` as an argv subprocess
with `shell=False`; no Docker, registry or Kubernetes deployment implementation
was duplicated. Requests enqueue work and return HTTP 202/job ID immediately.

`ui/jobs.py` supplies a bounded, one-process worker queue. `MCP_UI_MAX_JOBS` is
validated to 1–3, defaults to 2, and was set to 3 for this demonstration. Eight
additional jobs can wait; a full queue returns HTTP 429. Namespace/name and any
managed Redis name are held for the entire job, including build, rollout,
verification and recovery. Conflicting jobs retain submission order; an unrelated
job can pass a blocked predecessor. Every worker releases ownership on failure.

Each job gets a UUID, a private source snapshot (2,000 files/50 MiB maximum), a
private temporary directory, separate subprocess output and a unique image
reference. A UUID image label makes even a cached UI build unique. Runtime
configuration is outside the Docker context. Private snapshots are removed at
completion. Existing CLI build defaults and image reuse remain available.

All UI builds use the existing bounded legacy Docker build option: 1 CPU,
1 GiB memory, memory+swap 1 GiB, five-minute build timeout; a full CLI job has a
20-minute timeout. These limits cover build containers, not all Docker daemon,
registry, image disk, network or host work. Graceful API shutdown drains accepted
jobs rather than cancelling a rollout midway.

The CLI emits explicit BUILDING/PUSHING/DEPLOYING/VERIFYING events. The adapter
adds QUEUED/SUCCEEDED/FAILED, timestamps and stage history. Docker build, push and
rollout output drains continuously into the corresponding job's log. The job
page polls once per second and displays progress, queue/resource waits, errors
and retained logs; reuse jobs skip build/push. Legacy lowercase `status` remains
for existing clients, while `stage`/`phase` and visible badges use uppercase.

Logs are redacted before disk writes, stored by UUID, and rotated through three
bounded segments (default 1 MiB each); the browser reads at most a 256-KB tail.
`MCP_UI_JOB_LOG_BYTES` configures segment size, 64 KiB–16 MiB. Overlong subprocess
lines are omitted. Known environment secrets, credential fields, authorization
headers, URL credentials and private-key blocks are filtered. Arbitrary hostile
output cannot be guaranteed safe; builds remain trusted local code execution.
Terminal metadata and retained logs survive UI restarts without automatic expiry.

The existing rolling strategy, readiness/startup/liveness probes, termination
grace and rollback implementation were preserved. Services and previous pods
are not modified while images build.

## Commands

Start exactly one localhost UI in its own PowerShell terminal. Restart an idle
existing UI first rather than starting a second process:

```powershell
$env:MCP_UI_MAX_JOBS = '3'
.\.venv-ui\Scripts\python.exe -m ui --port 8080
```

The existing registered examples, `manufact-text-tools` imported source and
intentional `manufact-invalid-build` source must be present. In another terminal:

```powershell
.\.venv-ui\Scripts\python.exe tests/verify_deployment_concurrency.py --kubeconfig .local/floci.kubeconfig
```

That exact verification command was executed once. It returned exit code 1
because of the three MCP failures, retaining all evidence. It did not run Full
demo. It deployed application versions through the real UI API; a small temporary
client Pod/ConfigMap used an already published image and was removed afterward.
The cluster was not recreated, restarted or reconfigured.

Focused tests executed:

```powershell
$jobTestTemp = Join-Path (Get-Location) ('.local/pytest-jobs-' + [guid]::NewGuid().ToString('N'))
.\.venv-ui\Scripts\python.exe -m pytest tests/test_ui_jobs.py tests/test_ui_sources.py tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp $jobTestTemp
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_integration.py --run-integration -m 'not compose_restart' -q -p no:cacheprovider
.\.venv-ui\Scripts\python.exe tests/verify_ui_guards.py
.\.venv-ui\Scripts\python.exe tests/verify_job_progress_browser.py --job 382fb0ecf860473094c9031b2807fb55 --seconds 20 --output evidence/concurrency/20261026T230456Z
.\.venv-ui\Scripts\python.exe tests/verify_job_progress_browser.py --job d51ffad817954270a4415b898536826e --seconds 2 --output evidence/concurrency/20261026T230456Z
.\.venv-ui\Scripts\python.exe tests/verify_job_progress_browser.py --job ca5af584c21340abb4755ce34fe2e743 --seconds 2 --output evidence/concurrency/browser-terminal
```

Windows sandbox permissions initially denied pytest's temporary fixture directory;
the focused suite passed when run with normal host access and a fresh directory.
A log stress test also exposed excessive regex work on long nonsecret lines;
bounded patterns fixed it before the live run. The existing MCP integration
selection deliberately excludes the Compose container restart check.

## Job evidence

Evidence: [summary.json](evidence/concurrency/20261026T230456Z/summary.json),
[observations.json](evidence/concurrency/20261026T230456Z/observations.json),
[commands.json](evidence/concurrency/20261026T230456Z/commands.json).
Each UUID also has its own JSON metadata and log in that directory. Metadata
contains the exact executed CLI argument array and verified image reference.

| Job | ID | Queue wait | Execution | Result |
|---|---|---:|---:|---|
| Independent stateful | `ca1be4510f5746558a167bc614b401c1` | 0.01 s | 228.97 s | SUCCEEDED |
| Independent stateless | `382fb0ecf860473094c9031b2807fb55` | 0.02 s | 225.60 s | SUCCEEDED |
| Independent text-tools | `9049a6ced4514a30a58b0f2908c98a9a` | 0.03 s | 124.82 s | SUCCEEDED |
| Same stateless, first queued update | `f8288776ad844c43bde8d411d0a7e60a` | 225.53 s | 49.33 s | SUCCEEDED |
| Same stateless, second queued update | `ca5af584c21340abb4755ce34fe2e743` | 274.82 s | 79.55 s | SUCCEEDED |
| Expected invalid Dockerfile | `d51ffad817954270a4415b898536826e` | 124.71 s | 3.81 s | FAILED, exit 1 |

Peak executing jobs: **3**. All three independent jobs were BUILDING together
from 23:05:50.540 to 23:06:08.153 UTC. Their API acceptance times were 89–122 ms.
The two same-target updates started only after the previous job finished. The
invalid source bypassed those blocked jobs when a worker became available,
failed before Kubernetes apply, and did not prevent subsequent success.
Five successful jobs produced five distinct image references and private
contexts; no build contexts remain. Nineteen live intermediate-container
inspections showed 1-GiB memory/memory+swap, CPU quota/period 100000/100000,
unprivileged execution and no host binds.

All final MCP Deployments (including the existing contract probe) were 2/2
ready and updated. Redis remained 1/1 with the same Pod UID, zero restarts and
the same Bound PVC. Existing Service and PVC UIDs were unchanged. Successful
`get_counter` calls continued returning 13 before and after rollout. Existing
Knative routes remained Ready; this demonstration exercised always-on Service
routing, not a new Knative scale-to-zero test.

## Continuous MCP traffic

The official MCP client ran inside Kubernetes against `http://stateless-demo:8000/mcp`
and `http://stateful-demo:8000/mcp`: real `echo` and read-only `get_counter` calls,
without delay arguments, mutations or application retries. Service DNS uses
ClusterIP routing; no application-pod port-forward was used. Measurement continued
at least 45 seconds beyond the final job, covering retiring pods' grace period.

| Scope | Requests | Successes | Failures | Successful-call p95 | Max |
|---|---:|---:|---:|---:|---:|
| Measured window | 1,198 | 1,195 | 3 | 1,447.70 ms | 6,609.68 ms |
| During builds/rollouts | 980 | 977 | 3 | 1,559.40 ms | 6,609.68 ms |
| Stateless measured | 613 | 611 | 2 | 1,351.53 ms | 6,609.68 ms |
| Stateful measured | 585 | 584 | 1 | 1,593.60 ms | 5,852.12 ms |

Raw totals including four warmup calls: **1,202 attempts, 1,199 successes,
3 failures**. Measured error rate: **0.2504%**. Target was at most 2 calls/second
per service; achieved combined rate was approximately 2.83/s over 423 seconds.
These are logical MCP attempts, each including client creation, protocol/tool
initialization, networking and teardown; they are not counts of individual HTTP
POSTs, isolated tool execution latency or pooled warm benchmark results. The
client had a 500-mCPU limit and shared the busy local host. No cold-start result
is claimed.

Raw records and complete errors are in
[traffic.json](evidence/concurrency/20261026T230456Z/traffic.json).

| Request started, UTC | Failure | Supporting runtime evidence |
|---|---|---|
| 23:09:09.982 | Stateless initialization: ConnectError, all connection attempts failed | Old stateless pod `75f974cf9b-622cc` stopped at 23:09:09. |
| 23:09:30.890 | Stateless initialization: ReadError | Other old stateless pod `75f974cf9b-4jqv9` stopped at 23:09:30. |
| 23:11:38.424 | Stateful MCP tool error | At 23:11:42.208, application log reports `redis.exceptions.TimeoutError: Timeout reading from socket`. |

The two exact-second termination correlations support a Service endpoint/drain
race. Client records do not identify the chosen destination pod, and there was
no endpoint watch or packet capture, so that mechanism is not proven. These
were direct Service calls, not Kourier HTTP 502 responses. Redis timeout is
confirmed, but its underlying cause (host contention, dependency scheduling or
networking) is not established. Redis had no restart and preserved its data.
Readiness checks process health; they do not prove every Redis operation works.
Runtime errors/events were saved in `stateful-runtime.log`, `redis-runtime.log`
and `events.json` beside the raw traffic. No retries or unverified manifest/
timeout changes were introduced to hide these failures.

## Focused validation and limitations

- **61 focused tests passed**: existing source/config/manifest/rollback checks
  plus queue bounds, conflict ordering, independent progress, failure isolation,
  real subprocess streaming, stage ownership, output/time limits, log rotation,
  redaction, context isolation and persisted failure inspection after reload.
- **6 existing live MCP tests passed**, 1 restart test deselected.
- **14 live UI guard checks passed**, with no deployment jobs submitted.
- Edge 156 browser checks saw build log growth before completion, the failed
  build's error, and a successful terminal stage/history; no JavaScript errors.

The queue/ownership state is in memory, not durable across restarts or shared
between API instances. Unexpected restarts mark unfinished jobs interrupted/
FAILED without automatically retrying them; a CLI may still be alive and needs
operator inspection. Keep one API process and one operator per runtime; a
competing terminal CLI bypasses these locks. Total retained metadata/images/log
history has no global disk quota. Redaction and local RUN limits are not a
production secret boundary or an untrusted-code sandbox. Legacy Docker builder
support is deprecated and required for the current local hard RUN limits.

Recommended next reliability work is a short endpoint/termination trace before
changing draining, followed by a narrowly scoped pre-stop drain experiment on
compatible Kubernetes versions. Profile Redis/host CPU and cgroup throttling;
keep concurrency at the default 2 or lower on a busy host before increasing
timeouts. Never blindly retry increments or other non-idempotent tools. No
production HA, zero downtime, multi-AZ behavior or large-scale load was verified.

## Changed files

- `ui/jobs.py`: bounded scheduling, source snapshots, subprocess output and logs.
- `ui/app.py`: integrate the pool, persistent stages and job status/log API.
- `deployctl.py`: explicit progress events and optional UI job image label.
- `ui/monitoring.py`: expanded bounded credential redaction.
- `ui/__main__.py`: single API process with graceful job drain.
- `ui/templates/job.html`, `dashboard.html`, `details.html`, `deploy.html` and
  `ui/static/style.css`: real stages, polling, queue visibility and progress.
- `tests/test_ui_jobs.py`, `verify_deployment_concurrency.py`,
  `verify_job_progress_browser.py`, `verify_ui_browser.py`: focused automated and
  opt-in live checks, with existing browser expectations updated for badges.
- `requirements-ui-test.txt`, `README.md`, `BYO_MCP_GUIDE.md`, this report:
  test dependency, exact operation commands and documented limits.
