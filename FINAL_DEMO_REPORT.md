# Final MCP platform demonstration

Executed 2026-10-26, America/Phoenix, against the existing Floci real-mode
EKS/k3s environment. This report records actual outcomes; historical Phase 3
measurements are not substituted for a new demo run.

**Final outcome: Quick PASS; 29 regressions PASS; Full FAIL for one HTTP 502
during revision rollout.** Scaling, cold activation, Redis persistence and
rollback passed. The remaining continuity limitation is documented below.

## Scope delivered

- `demo.ps1 -Quick` / `-Full`, backed by `scripts/demo.py`, reuse `deployctl.py`
  and the existing Kubernetes, autoscaling and continuity verification scripts.
- Published image reuse skips Docker build/push. Fresh evidence folders retain
  exact subprocess arguments, logs, timings, request outcomes and PASS/FAIL.
- `ARCHITECTURE.md`, `COST_COMPARISON.md`, and exact demo instructions in README.
- Verification-helper changes: optional evidence-directory override,
  `--reuse-images`, nested exception diagnostics, and a failing standalone
  continuity result when any MCP request fails. Server code, Dockerfiles, Redis
  integration, deployment backend and regression assertions remain unchanged.

## Commands executed

From the repository root in Windows PowerShell 5.1:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Quick
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Full
# The first Full trial exposed the history-selection bug described below.
# After the demo-only correction, the same command was run again:
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Full

# Final Quick verification after the demo fixes:
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Quick

docker compose up -d --no-build --wait --wait-timeout 120
.\.venv\Scripts\python.exe -m pytest -q -s tests --run-integration --run-compose-restart
```

Read-only checks included `docker info`, selected fields from `docker inspect`,
`docker stats --no-stream floci-aws-eks-mcp-platform`, explicit-kubeconfig
`kubectl get` checks, Redis `PING`, and Floci ECR `DescribeImages`. No setup or
installation script was run. Test Jobs and application revisions are temporary
application/test resources, not a new cluster or infrastructure installation.

## Executed results

| Check | Actual result |
| --- | --- |
| Initial Quick command | **PASS**, exit 0, 337.796 seconds |
| Final Quick after demo fixes | **PASS**, exit 0, 393.708 seconds; all six steps passed |
| Infrastructure | Node Ready; Serving controllers, activator, autoscaler, webhook and Kourier healthy |
| Always-on application Deployments/Service endpoints | Both **2/2** ready, two ready endpoints each |
| Real MCP calls | echo, add_numbers, increment_counter, get_counter passed through Kubernetes routing |
| Shared state | Increment/read alternated across both exact stateful replicas; counter reached 13 |
| Pod replacement | Deleted stateful MCP pods; replacement pods read 13 from Redis, before and after rollout |
| Stateless rolling update | Published historic image to Phase 3 digest; initial generation 6 to 7, final Quick 12 to 13; ready 2/2 |
| First Full trial | **FAIL**, exit 1, 342.744 seconds; safely restored previous deployment |
| Corrected Full trial | **FAIL**, exit 1, 1,216.307 seconds; one stateless HTTP 502 during continuity |
| Complete regression suite | **29 passed in 17.41 seconds** after the demo, no skips; earlier run also passed in 14.54s |

The initial Quick run's six steps all passed. Its two separate Redis counters
were `replicas_0bd7e90e7d204dcaa497419d8739f24a` and
`replicas_f00fb8d1bc7240cf873b2e610b6f67dc`, each verified at 13 after pod deletion.
The Quick check establishes successful before/after calls, not uninterrupted
traffic during that particular rollout.

- [Initial Quick summary](evidence/demo/20261026T193539664056Z-quick/summary.json)
- [Final Quick summary](evidence/demo/20261026T201126606796Z-quick/summary.json)
- [Final Quick image rollout](evidence/demo/20261026T201126606796Z-quick/rolling-update.json)
- [Initial Quick rolling images and strategy](evidence/demo/20261026T193539664056Z-quick/rolling-update.json)
- [Shared replica state and pod recovery](evidence/demo/20261026T193539664056Z-quick/mcp-before.json)
- [All 29 regression results](evidence/demo/regression-final.log)
- [Post-demo 29-test regression results](evidence/demo/regression-after-demo.log)

The regression suite explicitly restarted the Compose stateful container and
read its Redis counter as 7 before and 7 after. Unit rollback tests intentionally
emit rollout-error diagnostics while asserting recovery; the suite's final
result was 29 passed. Compose warned that the separately configured existing
Floci container was an orphan relative to `compose.yaml`; it was retained.

## Defect found and fixed in the demo

The initial Full trial repeated Quick from a deployment already at v2. Its
initial implementation selected an older ReplicaSet image as v1. Earlier
rollback tests had intentionally left ReplicaSets referencing missing tags,
so it selected one of those and reached `ImagePullBackOff`. The CLI failed the
rollout after its timeout and logged `ROLLBACK OK: previous healthy deployment
restored`. Two healthy previous replicas were observed during the stalled
rollout. This was a real demo-runner defect, not an intentionally planned
success case.

The runner now checks published Floci ECR tags/digests before selecting an old
version. Read-only discovery found seven valid historical tags and excluded
the missing rollback candidates. The failed run and diagnostics are retained:

- [First Full failure summary](evidence/demo/20261026T194245264056Z-full/summary.json)
- [Failed rollout and successful recovery log](evidence/demo/20261026T194245264056Z-full/command-06.log)

A separate missing-kubeconfig negative check returned exit 1 immediately with
a clear infrastructure FAIL message and created no resources. Its evidence is
in ignored `.local/demo-preflight-check/`.

## Corrected Full run: scaling and cold activation

The corrected Full run uses evidence folder
`evidence/demo/20261026T194844428045Z-full/`. Both services were deployed through
the same CLI with existing immutable image digests. Sequential autoscaling
tests used min 0, activation 2, max 10, target concurrency 2, window 20 seconds,
eight concurrent real MCP clients and three-second asynchronous tool calls.

| Measurement | Stateless | Redis-backed stateful |
| --- | ---: | ---: |
| Successful load tool calls | 138 | 154 |
| Failed load calls | 0 | 0 |
| Peak ready replicas | 6 | 6 |
| First observed ready count above two | 40.38s | 27.94s |
| Idle time until desired replicas and **all** application pods were zero | 56.76s | 53.94s |
| First MCP request after confirmed zero pods | 27.34s | 15.42s |
| Load latency p50 / p95 (including deliberate delay) | 3.38s / 6.19s | 3.35s / 4.23s |
| Cold MCP result | `cold-start` | Counter 161, expected 161 |

The stateful key `autoscale_c78c2e126df84f178be16deaeed06000` started at 7 and
received 154 successful increments. After every stateful MCP pod disappeared,
a fresh SDK client through Kourier read **161**. Redis remained outside the
scaled pods. Cold requests used the gateway, not direct pod connections.
Stateless activation had one ready pod and a second starting when its response
completed; stateful activation had two ready pods at its sampled completion.
Both requested two activation replicas. No manual app scaling was used.

- [Stateless load, zero and cold request](evidence/demo/20261026T194844428045Z-full/stateless-demo-autoscaling.json)
- [Stateful load, zero and Redis persistence](evidence/demo/20261026T194844428045Z-full/stateful-demo-autoscaling.json)

The runner restored max-four local profiles before simultaneous traffic.

## Full run: continuity failure retained

The continuity generator ran for **241.316 seconds**, scheduling approximately
**2.495 real MCP tool requests/sec/service**, with fresh SDK connections and no
request retries. It performed all deployment/recovery operations successfully:

| Operation | Observed outcome |
| --- | --- |
| Independent existing fixture, new revision, existing image | CLI exit 0; 34.56s; multiply tool returned 42 |
| Stateless v2 revision promotion | CLI exit 0; 52.49s; traffic switched to revision `stateless-demo-00011` |
| Stateful MCP pod deletion | Counter remained 17 after deletion |
| Intentional missing-image Knative update | Expected CLI exit 1; 59.17s including successful rollback; healthy route preserved |

| Service | Requests | Successes | Failures | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stateless | 602 | 601 | **1** | 0.900s | 2.347s | 3.810s |
| Stateful | 602 | 602 | **0** | 0.296s | 1.717s | 3.004s |

Latency statistics include successful and failed attempts; deliberate stateless
tool delay is 0.6 seconds. The operation timings exclude the surrounding
baseline/settling windows.

The failed stateless request was sent 94.429s into the run during
`existing-service-v2-rollout`, and failed after 2.362s. Its original client
record contains an `ExceptionGroup`. Kourier's access log records a matching
**HTTP 502 on POST /mcp at 20:06:33.783 UTC**, forwarding to the activator at
`10.42.0.21:8012`. Activator diagnostics also show backend probe timeouts during
candidate startup. These observations locate the failure in the Knative
request path; they do not prove a precise root cause or justify a speculative
routing/infrastructure change.

The demo therefore returned **FAIL**, even though scaling, persistence,
deployment commands and rollback passed. The original continuity helper's
`result: PASS` meant its operations passed, while its own request table still
showed one failure. The demo caught this inconsistency. The helper now also
returns FAIL/nonzero for any traffic failure and records nested tracebacks.
Existing evidence was not rewritten, and no retry or relaxed success threshold
was added. This prevents misleading green results in future standalone runs.

- [Corrected Full summary: FAIL](evidence/demo/20261026T194844428045Z-full/summary.json)
- [All 1,204 continuity request records and operation commands](evidence/demo/20261026T194844428045Z-full/reliability.json)
- [Kourier access diagnostics, including the 502](evidence/demo/20261026T194844428045Z-full/3scale-kourier-gateway-diagnostic.log)
- [Activator diagnostics](evidence/demo/20261026T194844428045Z-full/activator-diagnostic.log)

**Remaining blocker:** a repeatable clean Full demo is not established because
this run lost one MCP request during revision rollout. Quick remains available
for the working deployment/tool/state demonstration. Further routing/draining
investigation is needed before promising uninterrupted Knative deployment
traffic. The architecture and installed infrastructure were preserved.

## Environment and limits

Final read-only status captured at **20:19:03 UTC**:

| Component | Final observed state |
| --- | --- |
| Always-on stateless / stateful | Each 2/2 ready; each Service has two ready endpoints |
| Knative stateless / stateful | Each desired 0, zero remaining app pods; both Services Ready; min 0 / max 4 local profiles |
| Active stateless Knative route | 100% to healthy revision 00011 after rollback; latest-ready metadata is 00013 |
| Redis persistence | Both Kubernetes PVCs Bound; Redis not scaled with MCP pods |
| Existing Serving/Kourier infrastructure | Every Deployment available 1/1 |
| Compose | Floci, Redis, stateless and stateful running and healthy |
| Default kubectl context | Still `kind-manufact-lab`; all demo operations used the isolated kubeconfig |
| Cluster runtime | Same container ID and original start time; running |

Evidence: [Final resource, route and container status](evidence/demo/final-status.json).

The existing runtime container ID is
`9907087e45e8f9dfd51f846673485fd5a6de6dbc8b677bfa66aa86f9b1045792`,
started at `2026-10-26T18:12:54.846142035Z`. It was not recreated by the demo.
Docker reports 8 logical CPUs and 8,195,891,200 bytes RAM. k3s runtime CPU quota
is four CPUs; Kubernetes allocatable CPU remains 1930m and allocatable memory
is 5,072,088 KiB. Serving/Kourier are the existing pinned 1.23.0 installation;
Kubernetes is 1.34.1+k3s1. Local infrastructure HPAs remain bounded at one.

The isolated private kubeconfig remains in `.local/`; no administrator
certificates were copied into public evidence. The unrelated default Kubernetes
context is not used by demo commands.

There is one Docker host, one k3s node, single-replica Redis per environment,
and host-backed local storage. No production HA, multi-AZ failure tolerance,
host-loss durability, or 1,000-server capacity has been verified. Cost figures
are explicit assumptions and AWS-published reference rates, not observed bills
or verified deployment-scale measurements. No multi-region or new worker
autoscaling infrastructure was implemented.
