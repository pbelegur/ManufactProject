# Final technical review

Reviewed 2026-10-26. No Full run, infrastructure rebuild, benchmark, regression
suite, deployment or application change was performed for this review. Existing
warm data was sufficient. Read-only Kubernetes queries recovered retained events
and current revision/Deployment/EndpointSlice metadata; the capture is timestamped
separately from the incident.

## 502 finding

**The most likely failure class is a transient upstream forwarding/connection
failure in the existing revision's activator → queue-proxy → application path,
during overlapping pod draining and candidate startup. A draining/endpoint race
is plausible, but the precise root cause is not proven.** The evidence does not
support claiming that the CLI promoted the new revision before readiness.

The client recorded one failed stateless call among 1,204 total tool requests:
601/602 stateless and 602/602 stateful successes. The failed call began at
relative time 94.429s in `existing-service-v2-rollout` and lasted 2.362s. Its
saved exception is an `ExceptionGroup` without the nested cause. The generator
did not save an HTTP request ID or absolute timestamp for each SDK call, so the
client/gateway correlation is strong circumstantial evidence, not an end-to-end
trace with a shared identifier.

### Reconstructed timeline

All wall times are **2026-10-26 UTC**. Revision suffixes refer to stateless-demo.

| Time | Evidence | Interpretation |
| --- | --- | --- |
| 20:04:58.215 | Activator reports two backends for old revision 00010 | Existing serving capacity before the incident |
| 20:06:00 / 20:06:02 | Deployment events scale old revision 00010 from 2→3→2 | Autoscaling churn overlaps the later update |
| 20:06:08–11 | Revision 00011 created; its Deployment scales 0→2; activator initially reports zero backends | Candidate startup |
| 20:06:10–11 | Old surplus pod `...00010...-ntzgq` receives container stop events | A pod in the old revision is draining |
| 20:06:11–40 | That terminating pod has aggregated readiness-503 events; connection refused recorded at 20:06:43 | Consistent with drain/termination, not proof that it received the failed call |
| 20:06:17–42 | Candidate 00011 backend probes return 503/refusal/timeouts; startup probes fail while Python starts | Candidate is not yet healthy; these are not user HTTP 503s |
| **20:06:33.783** | **Kourier POST `/mcp` returns 502**, Host stateless-demo route, upstream `10.42.0.21:8012` | Error traverses the activator, not a direct Kourier→application connection |
| 20:06:33.226 / 20:06:36.694 | Gateway `/ready` probes return 200; surrounding MCP requests return 200 | No evidence of a sustained gateway outage |
| 20:06:43.465 / 44.537 | Candidate activator backend count becomes 1 / 2 | Candidate warming completes after the failed request |
| 20:06:43 / 44 | Revision 00011 ContainerHealthy / Ready transitions | Readiness occurs about 10 seconds after the 502 started |
| 20:06:48 | Saved RoutesReady transition and deployment log confirm route to 00011 | Promotion is later than the failed request |
| 20:07:08–09 | Old revision scales 2→0; old backend count falls 2→1→0 | Normal retirement of the old revision is also later than the 502 |

The gateway entry is source line **2968**, request ID
`d962f5d2-3e02-409c-9a1f-b4ebd1de7531`, response flags `-`, HTTP duration
**1,197ms**, upstream service time **1,195ms**, response body length 73 bytes.
The body itself was not saved. The bracketed time is the HTTP request **start**,
so approximate completion is 20:06:34.980, not 20:06:33.783.
[Envoy's default log format](https://www.envoyproxy.io/docs/envoy/latest/configuration/observability/access_log/usage)
defines these fields. A normal flags field and populated upstream timing favor
a proxied upstream error over an Envoy-local connection failure; they do not
identify whether the activator, queue proxy or application originated the 502.

### Readiness and endpoint evidence: what is and is not established

The deployment code pins 100% traffic to the previously routed revision, waits
for the candidate's Ready condition, then applies the traffic switch and waits
for route status. See [deployctl.py](deployctl.py). The saved deployment log
confirms that sequence. Candidate startup errors alone therefore do not explain
the failed call: it should still have used revision 00010.

Activator logs show **two old-revision backends from 20:04:58 until 20:07:09**,
without a logged count reduction at the failure. The extra draining pod is
real, but the logs do not establish that it ever entered that ready backend set.
This is evidence against asserting a proven old-endpoint-loss event. Kubernetes
events aggregate first/last occurrences; a readiness event spanning minutes
does not mean the pod was continuously unready for every moment in between.

The retained events show no old-revision scale-to-zero at 20:06:33. The retained
revision conditions place candidate readiness after the failure. Current
EndpointSlices show the public revision Services targeting the activator and
empty private slices after application scale-to-zero. **They are post-incident
snapshots, not historical EndpointSlice changes at 20:06:33.** There was no saved
EndpointSlice watch or Envoy route dump for that instant.

Single-node contention is a possible contributor: candidate startup took about
35s and successful-call latency rose during the update. However, there is no
incident-aligned CPU-throttling/memory time series proving resource saturation.
No saved evidence ties this request to an OOM, a Redis failure, a failed image
pull or the later intentional missing-image rollback test. The stateless tool
does not use Redis.

### Smallest safe mitigation: proposal only

For **this failed echo call**, one bounded retry with a small jittered delay
(for example 100–300ms) after an HTTP 502 is a safe consumer-side mitigation.
Apply it only to explicitly read-only/idempotent tools such as echo,
add_numbers and get_counter. Do **not** transparently retry increment_counter:
an uncertain HTTP response does not prove that the Redis increment was not
committed. Mutation retries would need an idempotency mechanism first.
Keep verification counts before any retry so failures remain visible.

If a server-side configuration experiment is preferred, a per-revision
`autoscaling.knative.dev/scale-down-delay: "60s"` is a small, reversible way to
reduce the observed 2→3→2 churn while preserving eventual zero. It retains
capacity longer, changes idle timing, and needs spare capacity on this host.
It is **not a demonstrated fix**, and the CLI currently does not expose this
annotation as an mcp.json field. Nothing was patched.
[Knative scale-down delay](https://knative.dev/docs/serving/autoscaling/scale-bounds/#scale-down-delay)
documents the behavior.

Do not extend readiness delays or set minReplicas=2 and claim the 502 is solved:
the failure predates promotion and the old revision already had two recorded
backends. Before changing server behavior, capture the HTTP error body and
request ID across activator/queue/app logs, revision identity, EndpointSlice
changes and termination timestamps. The pinned
[Knative activator handler](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/activator/handler/handler.go)
uses an upstream reverse proxy; the missing per-request downstream evidence is
what prevents resolving its exact failing hop here.

## Performance: warm and cold kept separate

No new requests were sent. Calculations use the saved successful per-request
measurements, not reconstructed durations from gateway HTTP operations.

### Warm baseline before deployment activity

Both services were explicitly warmed before the generator started. The first
12-second `baseline` phase contains **30 successful calls per service**,
scheduled at 2.5 tool calls/sec/service. Each call creates a fresh MCP client
through the forwarded Kourier gateway with a Host header. The timer covers MCP
initialization, tool execution and client context teardown; this is not a
single HTTP-hop or tool-body-only measurement.

| Warm workload | Samples / failures | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: |
| Stateless echo **with 600ms intentional delay** | 30 / 0 | **838.2ms** | **930.4ms** | **932.7ms** |
| Stateful Redis get_counter, no injected delay | 30 / 0 | **251.0ms** | **323.3ms** | **335.2ms** |

p50 is the sample median; p95/p99 use nearest rank
`sorted[ceil(percentile * n) - 1]`. With n=30, p99 equals the maximum and is a
weak tail estimate, not a production p99 SLO. No undelayed stateless baseline
was measured in this dataset. Subtracting 600ms would produce a derived
estimate, not an independently measured no-delay percentile.

### Successful calls across deployment and recovery activity

This separate table includes contention and revision changes, so it is not a
quiet warm benchmark. Failed attempts are excluded from latency percentiles
and counted explicitly. The original report used a slightly different p95
index and included the failed attempt; the values below are recomputed from
the raw successful records with the stated method.

| Workload | Successful / total calls | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: |
| Stateless echo, 600ms injected delay | 601 / 602 | 899.9ms | 2,313.1ms | 3,048.9ms |
| Stateful get_counter | 602 / 602 | 295.7ms | 1,717.2ms | 2,497.5ms |

### Cold activation after confirmed zero pods

| Service | Cold samples | End-to-end latency | Result |
| --- | ---: | ---: | --- |
| Stateless | 1 | **27.341s** | echo returned `cold-start`; no injected delay |
| Stateful | 1 | **15.419s** | get_counter returned **161**, the expected persisted value |

These are individual cold-start observations, **not cold p50/p95/p99**. The
tests first observed desired replicas and all application pods at zero, then
made a real MCP request through Kourier/activator. Redis and ingress stayed up.
The separate 3-second delayed autoscaling-load percentiles are not baseline
warm service latency and are not mixed into either table.

## Evidence and review scope

- [Raw client outcomes](evidence/demo/20261026T194844428045Z-full/reliability.json)
- [Saved Kourier log](evidence/demo/20261026T194844428045Z-full/3scale-kourier-gateway-diagnostic.log)
- [Saved activator log](evidence/demo/20261026T194844428045Z-full/activator-diagnostic.log)
- [Selected incident logs with original line numbers](evidence/review/incident-log-excerpts.txt)
- [Retained events and post-incident readiness/endpoint snapshot](evidence/review/additional-readonly-capture.json), captured **20:41:12 UTC**
- [Calculated latency values and methodology](evidence/review/latency-analysis.json)
- [Cold stateless evidence](evidence/demo/20261026T194844428045Z-full/stateless-demo-autoscaling.json)
- [Cold stateful evidence](evidence/demo/20261026T194844428045Z-full/stateful-demo-autoscaling.json)

Read-only queries used the existing isolated kubeconfig and namespace mcp-auto:
`kubectl --kubeconfig .local/floci.kubeconfig --request-timeout=15s -n mcp-auto
get RESOURCE -o json`, for events, revisions, deployments and endpointslices.
Exact argv and capture time are retained in the JSON snapshot. Older Phase 3
capacity-failure events were inspected as historical context and were not
misattributed to this incident. No working component or historical evidence
was modified.
