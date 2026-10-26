# Final performance and reliability validation

Measured **2026-10-26 UTC**, against the existing Floci real-mode EKS/k3s cluster.

**Outcome:** one controlled Knative rollout completed with **252/252 measured MCP
calls successful, zero failures, no application retries**. The historical 502's
precise root cause remains unproven. The final, matched-protocol warm benchmark
completed **1,200/1,200 measured tool calls successfully**. Both UI-deployed
always-on services remain healthy at **2/2**, with two ready Service endpoints.

No application, Dockerfile, deployment CLI, UI or infrastructure configuration
was changed. No images were built, no cluster was recreated, and no Full demo
or long regression suite was run. Changes are limited to this report and two
opt-in validation/analysis scripts in `tests/`.

## Historical HTTP 502: finding and evidence

The strongest supported hypothesis is a **transient upstream forwarding failure
on the existing revision's activator / queue-proxy / application path**. An
endpoint/draining race is plausible; the evidence does not identify the failing
downstream pod or establish the exact hop that generated the response.

The original run recorded 1,203 successes and one failure in 1,204 tool calls:
stateless 601/602; stateful 602/602. Its saved client error lacks the nested
exception, absolute request timestamp and shared HTTP request ID. Correlation
to the single gateway 502 is strong circumstantial evidence, not a complete
client-to-pod trace.

Rechecked the [original gateway log](evidence/demo/20261026T194844428045Z-full/3scale-kourier-gateway-diagnostic.log),
line 2968, and the [original activator log](evidence/demo/20261026T194844428045Z-full/activator-diagnostic.log).
The gateway entry is:

```text
[2026-10-26T20:06:33.783Z] "POST /mcp HTTP/1.1" 502 - 311 73 1197 1195 "-" "python-httpx2/2.16.0" "d962f5d2-3e02-409c-9a1f-b4ebd1de7531" "stateless-demo.mcp-auto.mcp.test" "10.42.0.21:8012"
```

`10.42.0.21:8012` is the activator. Duration is 1,197 ms, upstream service time
1,195 ms, and response flags are `-`. These favor a proxied upstream error over
an Envoy-local failure, without proving its origin. The bracketed timestamp is
request start; approximate completion was **20:06:34.980**.
[Envoy's documented default log format](https://www.envoyproxy.io/docs/envoy/latest/configuration/observability/access_log/usage)
defines these fields. [Knative's request flow](https://knative.dev/docs/serving/request-flow/)
explains the activator and queue-proxy paths.

| Hypothesis | Supporting evidence | Qualification |
| --- | --- | --- |
| Transient upstream forwarding/connection error | One proxied 502, activator upstream, surrounding MCP requests and gateway probes succeed | Most likely failure class; response body and downstream request trace are missing |
| Pod drain / endpoint transition race | Old revision 00010 briefly scaled 2→3→2; surplus pod received stop events at 20:06:10–11 and had readiness failures | Activator still recorded **two** old-revision backends until 20:07:09; no proof that surplus pod received this request |
| Single-host contention contributed | Candidate startup took roughly 35 seconds; latency rose during deployment | No incident-aligned CPU/throttling trace proves saturation |
| CLI routed to an unready candidate | Candidate had startup/readiness failures | **Not supported:** candidate 00011 became Ready around 20:06:44 and routing completed at 20:06:48, after the 502 |
| Redis failed | Stateful infrastructure was present | **Not supported:** the failed operation was stateless; no Redis dependency |

The old revision did not begin its final retirement until 20:07:08–09. Saved
post-incident EndpointSlices cannot reconstruct endpoints at 20:06:33. Readiness
events with first/last timestamps are aggregated events, not a continuous trace.
See [TECHNICAL_REVIEW.md](TECHNICAL_REVIEW.md) and its linked retained event capture
for the detailed historical reconstruction.

**No serving fix was applied:** changing readiness delays, minimum replicas or
drain timing without identifying the failing hop would not be evidence-backed.
The existing Knative pod already has a `/wait-for-drain` lifecycle hook and a
120-second termination grace period. The always-on backend retains its existing
30-second termination grace and health probes. `/health` is process health;
actual Redis behavior is verified through MCP tool calls.

The smallest optional consumer mitigation is a bounded, jittered retry for an
explicitly read-only operation such as echo or get_counter. This was **not
implemented or used in verification**. Never automatically retry
increment_counter after an uncertain response: its Redis write may already have
committed. Mutation retries require persistent idempotency/deduplication first.

## One controlled rollout

Used the existing stateless image digest with a new release annotation, forcing
revision **00014 → 00015** through the existing CLI without building or publishing.
This exercises revision replacement and traffic promotion, not changed application
bytes. Stateful and stateless requests ran simultaneously through Kourier using
the official MCP SDK and `/mcp`.

Two sequential streams targeted at most **2 requests/second/service** sent
read-only, no-delay echo and Redis get_counter. Slow calls reduced achieved RPS;
the generator did not produce catch-up bursts or retry failed operations.

- Traffic window: **21:28:00.702–21:29:06.212 UTC**, by request start.
- CLI deployment: **21:28:09.632–21:28:49.902**, **40.271 seconds**, exit **0**.
- Stateful counter remained **105** on every rollout read.

| Workload | Raw successes / attempts | Failures | p50 | p95 | p99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Knative stateless echo | **127/127** | **0** | 72.93 ms | 480.25 ms | 729.12 ms | 751.75 ms |
| Knative Redis get_counter | **125/125** | **0** | 96.96 ms | 695.63 ms | 985.72 ms | 1,063.83 ms |

During the CLI's execution specifically: **78/78 stateless** and **76/76 stateful**
calls succeeded. Two additional preflight tool calls succeeded and are excluded
from the 252 measured calls. All **254 tool-call HTTP requests returned 200**.
The eight discovery/handshake/schema HTTP operations also completed successfully;
initialized notifications returned the expected 202. All **262 HTTP request IDs
matched gateway log entries**; none had a 5xx response. Verification did not hide
failures behind retries.

Observed readiness and endpoint sequence:

| UTC time | Evidence |
| --- | --- |
| 21:28:18 | Candidate 00015 private EndpointSlice created empty; route still 00014 |
| 21:28:26–28 | Candidate endpoints present with `ready=false` |
| 21:28:43 / 45 | Candidate has one / two ready endpoints |
| 21:28:46 | Configuration ready; one candidate endpoint begins terminating, `ready=false, serving=true` |
| 21:28:47–48 | Route status changes to 00015; RoutesReady/Ready become True |
| 21:28:51 | Terminating candidate endpoint becomes `serving=false`; another replacement endpoint appears |
| 21:29:06 | Last measured tool call starts |
| 21:29:09 | Old revision endpoints become terminating/unready; queue-proxy logs attached drain handler and TERM/graceful shutdown |
| 21:29:14–15 | Old revision endpoints become `serving=false` |

Endpoint times above are Kubernetes managed-field update timestamps, with watch
event order/resource versions retained; they are not client-clock receipt times.
Candidate-pod churn occurred during traffic. **The final old-revision drain began
about three seconds after traffic ended**, so that final retirement interval was
observed through logs/watch events but was not covered by active test requests.
No second rollout was run to fill that gap.

The initial collector used the wrong application-container name
(`user-container` instead of `mcp`). Its empty application-log files are not
evidence of silence. The collector was corrected. Retired pod application logs
were already removed from the node; the later saved `00015-mcp.log` covers
benchmark-time pods, not the original rollout pods. Queue-proxy, activator,
gateway, readiness, events and EndpointSlice captures remain available.

This is a successful bounded observation, **not proof of guaranteed zero downtime**
or proof that the historical 502 has been fixed. Rollout latency was substantially
higher than quiet warm latency despite no failures.

## No-delay warm MCP latency

Final authoritative run: **21:30:28.979–21:32:31.293 UTC**, including activation
and setup. The 1,200 measured warm calls themselves ran approximately
**21:31:45.925–21:32:16.393**, before the separate fresh-client measurements.

Method:

- Temporary, non-root client pod uses the existing published stateful image and
  official MCP SDK **2.3.0**; no packages or infrastructure installed.
- Requests originate inside the same cluster, without kubectl port-forward in
  the data path. Paths use ClusterIP DNS and real MCP `/mcp` requests.
- Six persistent clients, one per service/path; **one measured call in flight
  globally**. A fixed shuffle interleaves paths; five calls/tool/path warm caches
  before the 100 recorded samples. No `delay_seconds` is supplied; server default
  is zero. Both echo and add_numbers are measured.
- Initial activation connections are closed, then new warm connections opened.
  All six measured clients negotiated **MCP 2026-07-28**.
- Stateful measurements execute actual Redis GET and atomic INCRBY. Each path
  owns a unique counter; 105 increments/path, including warmups, are followed by
  a final MCP read of **105**. No mutation retries.
- Percentiles use median for p50 and nearest rank `ceil(q*n)-1` for p95/p99.
  Latencies below are successful SDK `call_tool` round trips, including decoding
  and validation; connection construction and initialization are excluded.

`Service` = always-on service in `mcp-demo`. `Knative` = Kourier route with the
actual virtual Host in `mcp-auto`. `Private` = the same routed Knative revision's
private ClusterIP Service, including queue-proxy but bypassing Kourier/activator.
Private Services are diagnostic targets, not a proposed replacement ingress.

| Path | Tool | Samples | p50 ms | p95 ms | p99 ms | Max ms | Errors |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Service | echo | 100 | **12.86** | 34.04 | 45.52 | 59.97 | 0% |
| Knative | echo | 100 | **18.12** | 41.85 | 84.55 | 156.03 | 0% |
| Private | echo | 100 | 15.25 | 40.33 | 68.30 | 70.77 | 0% |
| Service | add_numbers | 100 | **13.50** | 38.49 | 94.19 | 147.89 | 0% |
| Knative | add_numbers | 100 | **20.26** | 56.15 | 95.75 | 131.26 | 0% |
| Private | add_numbers | 100 | 16.11 | 43.83 | 103.93 | 124.05 | 0% |
| Service | increment_counter | 100 | **20.71** | 49.58 | 88.38 | 88.53 | 0% |
| Knative | increment_counter | 100 | **25.98** | 77.12 | 99.53 | 113.78 | 0% |
| Private | increment_counter | 100 | 22.38 | 77.12 | 290.34 | 391.31 | 0% |
| Service | get_counter | 100 | **19.85** | 47.78 | 87.44 | 87.81 | 0% |
| Knative | get_counter | 100 | **25.18** | 77.54 | 105.79 | 106.91 | 0% |
| Private | get_counter | 100 | 22.62 | 85.19 | 180.11 | 188.67 | 0% |

All **1,341 tool calls** in this final run succeeded: 1,200 measured warm calls,
60 warmups, six activation probes, 72 fresh-warm calls and three final counter
checks. The earlier exploratory run also had 1,341/1,341 successes, but its
pooled Knative clients negotiated the legacy protocol after cold activation.
Its mixed-protocol percentiles are **not** the authoritative comparison above.

### Execution, transport and client costs

The benchmark records HTTP request-to-header and request-to-body-complete times
using client hooks, separately from the SDK call timer. Every warm tool call
was paired with exactly one `tools/call` HTTP record using route, tool and UTC
interval. The paired residual is time outside that HTTP interval: SDK scheduling,
serialization, parsing, validation and test bookkeeping. It is not wire latency.

| Path / tool | SDK call p50 | HTTP through body p50 | Paired time outside HTTP p50 |
| --- | ---: | ---: | ---: |
| Service echo | 12.86 ms | 8.24 ms | 4.28 ms |
| Knative echo | 18.12 ms | 14.15 ms | 4.02 ms |
| Service add_numbers | 13.50 ms | 9.54 ms | 3.97 ms |
| Knative add_numbers | 20.26 ms | 15.55 ms | 3.97 ms |
| Service increment_counter | 20.71 ms | 15.82 ms | 4.24 ms |
| Knative increment_counter | 25.98 ms | 21.52 ms | 4.10 ms |
| Service get_counter | 19.85 ms | 15.24 ms | 4.02 ms |
| Knative get_counter | 25.18 ms | 20.78 ms | 4.19 ms |

These independently summarized columns are not additive percentile equations.
HTTP time still contains networking, proxies, server dispatch and tool execution.
It is **not a measurement of network alone**.

To estimate tool-body cost without modifying serving code, an isolated Python
process in an existing always-on application pod imported its unchanged server
module and called each actual function 100 times after five warmups:

| Isolated tool body | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: |
| echo, no delay | 0.0127 | 0.0331 | 0.1020 | 0.1357 |
| add_numbers | 0.0026 | 0.0028 | 0.0029 | 0.0030 |
| increment_counter, real Redis | 7.39 | 38.33 | 51.02 | 58.87 |
| get_counter, real Redis | 4.16 | 27.65 | 36.43 | 40.35 |

These are **isolated execution diagnostics**, not per-request server spans.
They exclude MCP dispatch/thread-pool scheduling and may experience different
contention from served requests. There is no server-timing header or distributed
trace in the existing application. Exact serving execution versus network versus
gateway attribution therefore cannot be claimed from this platform's evidence.
Subtracting these independent percentiles would not establish that breakdown.

Private-versus-Knative differences of sample medians were approximately
**2.6–4.2 ms**, suggesting a modest gateway/activator contribution in this run.
They are aggregate path comparisons, not exact hop timings. The public path was
not consistently worse at the tail: private Redis p99 was higher in this sample.
There is no basis for blaming Kourier alone for the warm tail.

### Fresh-client overhead, separately measured

Twelve additional fresh warm connections per route used no-delay echo or
get_counter. Each measurement includes client construction, MCP discovery,
the first tool call (including tool-schema discovery), and teardown. All used
the modern protocol. Table entries are medians, not additive components:

| Fresh warm path | Client construction | MCP initialization | First tool call | MCP teardown | Whole connection |
| --- | ---: | ---: | ---: | ---: | ---: |
| Service stateless | 37.63 ms | 26.18 ms | 32.05 ms | 1.65 ms | **105.37 ms** |
| Knative stateless | 39.85 ms | 30.96 ms | 43.91 ms | 1.69 ms | **126.34 ms** |
| Service stateful | 44.97 ms | 24.55 ms | 40.75 ms | 1.71 ms | **118.64 ms** |
| Knative stateful | 36.78 ms | 29.35 ms | 43.93 ms | 1.79 ms | **110.79 ms** |

**Dominant avoidable end-to-end cost: recreating clients and repeating discovery**,
compared with the 13–26 ms pooled calls. For already-pooled stateless calls,
HTTP/MCP/scheduling overhead dominates the microsecond tool bodies. Redis tool
execution adds a material 4–7 ms median in the isolated diagnostic, but the
evidence does not isolate a single dominant component of the remaining warm
transport/scheduling time. No performance optimization was applied.

### Cold activation is excluded

The first run began with Knative applications at zero. Its initial MCP
initialization intervals were **25.182 s stateless** and **12.751 s stateful**;
subsequent first tool calls took 221.85 ms and 139.12 ms. These are separate
setup components, **not complete cold-request percentiles**. Cold auto-discovery
also fell back to the legacy handshake, motivating the matched warm pass.

The previously saved Full-demo complete cold observations remain **27.341 s**
stateless and **15.419 s** stateful, one observation each. They were not rerun
as a dedicated cold-start suite and are not mixed into any warm table.

## Final validation and preservation

- UI API returned real Kubernetes inventory without errors. Final saved status:
  `mcp-demo/stateless-demo` **2/2 Healthy**, `mcp-demo/stateful-demo` **2/2 Healthy**.
- Each always-on MCP Service retains **two ready endpoints**. Deployment UIDs,
  image references and both application pod UIDs are unchanged from before the
  test. The independent compatible-project deployment remains 2/2; Redis remains
  1/1 with the same pod and Deployment identities.
- Both Knative services were Ready with 2/2 replicas in the post-benchmark
  snapshot and later naturally returned to **Ready (scaled to zero)**. The final
  UI query records that distinction; zero idle replicas are not reported as 2/2.
- Cluster container identity and original start time are unchanged:
  `9907087e45e8f9dfd51f846673485fd5a6de6dbc8b677bfa66aa86f9b1045792`,
  started `2026-10-26T18:12:54.846142035Z`. Explicit isolated kubeconfig was used.
- Temporary client Pods and ConfigMaps were removed. No new persistent
  infrastructure was installed. Uniquely named benchmark counters remain as
  test data; existing counters were not incremented or reset.
- **22 focused CLI/manifest tests passed in 0.70 s**. Both new validation scripts
  compile, and offline analysis checks request correlation, protocol matching,
  counters and resource preservation. The prior 29-test result is historical;
  that full suite was not rerun here.

## Commands and raw evidence

Executed from the repository root:

```powershell
.venv/Scripts/python.exe tests/validate_final_performance.py --samples 100 --kubeconfig .local/floci.kubeconfig
.venv/Scripts/python.exe tests/validate_final_performance.py --samples 100 --warm-only --kubeconfig .local/floci.kubeconfig
.venv/Scripts/python.exe tests/analyze_final_performance.py evidence/performance/20261026T212500Z evidence/performance/20261026T213013Z
$validationTemp = Join-Path (Get-Location) ('.local/pytest-perf-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe -m pytest tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp $validationTemp
.venv/Scripts/python.exe -m py_compile tests/validate_final_performance.py tests/analyze_final_performance.py
kubectl --kubeconfig .local/floci.kubeconfig -n mcp-demo get deployments
Invoke-RestMethod http://127.0.0.1:8080/api/deployments?refresh=1
docker inspect --format '{{.Id}} {{.State.StartedAt}}' floci-aws-eks-mcp-platform
```

The **single actual deployment command**, invoked by the first script:

```powershell
.venv/Scripts/python.exe deployctl.py deploy servers/stateless --config mcp.knative.local.json --namespace mcp-auto --kubeconfig .local/floci.kubeconfig --image localhost:4566/000000000000/us-east-1/stateless-demo@sha256:cf69305bf48d2e09b3ebc040587e922eef0de4cc7befbea441068dc85925bf6e --version performance-20261026T212809Z --timeout 100 --rollback-timeout 100
```

For future latency-only reproduction, use `--warm-only`; the first command also
creates a new revision. Do not repeat the rollout just to recalculate percentiles.
Exact subprocess argv, timestamps and exit codes are retained in each run's
`commands.json`. The initial log-container-name and nullable EndpointSlice
analysis errors were collector/parser defects, corrected without changing the
platform; the latter was resolved by treating `endpoints: null` as an empty list.

- [Final raw warm measurements and HTTP timing](evidence/performance/20261026T213013Z/warm.json)
- [Computed results and offline checks](evidence/performance/20261026T213013Z/analysis.json)
- [Final UI status](evidence/performance/20261026T213013Z/final-ui-status.json)
- [Focused unit-test output](evidence/performance/20261026T213013Z/focused-tests.log)
- [Raw rollout requests and errors](evidence/performance/20261026T212500Z/traffic.json)
- [Rollout command/timestamps](evidence/performance/20261026T212500Z/rollout-operation.json)
- [CLI rollout output](evidence/performance/20261026T212500Z/rollout.log)
- [Request-ID-correlated gateway entries](evidence/performance/20261026T212500Z/correlated-gateway.log)
- [EndpointSlice watch](evidence/performance/20261026T212500Z/endpointslices-watch.json)
- [Pod readiness watch](evidence/performance/20261026T212500Z/pods-watch.json)
- [Knative Service/route watch](evidence/performance/20261026T212500Z/ksvc-watch.json)
- [Activator logs](evidence/performance/20261026T212500Z/activator.log)
- [Old revision queue-proxy/draining logs](evidence/performance/20261026T212500Z/stateless-demo-00014-queue-proxy.log)

## Limitations and recommended next improvements

1. **Historical cause unresolved.** Preserve failed HTTP bodies and request IDs
   through the downstream proxy/application path before choosing a serving fix.
   A future bounded test should continue traffic through confirmed retirement
   of the old revision, rather than a fixed 15-second post-promotion interval.
   No further rollout is authorized by this report or was run in this validation.
2. **Reuse MCP clients and HTTP pools in consumers** where connection semantics
   permit it. The measured setup/discovery cost supports this as the first
   performance improvement. It requires no infrastructure redesign.
3. **Profile Redis connection lifecycle before changing it.** The existing tools
   construct and close a Redis client/pool per operation. A lifecycle-managed
   shared pool may reduce overhead; this was not implemented or measured as an
   optimization, and Redis must remain the shared state backend.
4. **Do not tune gateway or pod limits from these percentiles alone.** The final
   client's cgroup reported only 36.181 ms additional quota-throttled time across
   the roughly 122-second run. That is client-only evidence; incident-time
   application, gateway and node saturation were not measured. Single-host
   scheduling contention and candidate startup remain confounders.
5. **These are local observations, not production SLOs.** Only 100 warm samples
   per tool/path and 12 fresh-client samples per route were collected. Warm p99
   is the second-largest of 100 measurements and has substantial uncertainty.
   Paths share one Docker/k3s host; CPU requests/memory limits and Redis instances
   differ between always-on and Knative namespaces. Persistent TCP connections
   may pin individual replicas. No production TLS, external AWS network, AZ
   failure, sustained saturation or large-fleet test is represented.

No guaranteed zero downtime, verified production HA or thousands-of-servers
capacity claim follows from these results.
