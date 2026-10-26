# Interview handoff for Enrico

## Opening explanation

"A developer brings a Dockerfile and mcp.json. One CLI deploys the project
either as an always-on Kubernetes service or a Knative service that scales with
requests. The two examples use the same pipeline: one is stateless; the other
stores application data in Redis outside its pods. Live MCP traffic goes through
Services or Kourier independently of the deployment command."

This is a working local evaluation on Floci real-mode EKS/k3s and ECR. It is
not a verified production AWS, multi-AZ or thousand-service deployment.

## Exact working demo command

From `C:\Users\Devi\Downloads\mcp-deployment-platform-starter`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 -Quick
```

The final recorded run passed all six steps in **393.708s**. It checks existing
infrastructure, two ready replicas/endpoints for each always-on application,
real MCP echo/add and increment/get calls, shared Redis state across replicas,
state after MCP pod replacement, a published v1→v2 rollout, and final health.
It reuses published images; it does not install infrastructure or rebuild the
cluster. It deliberately changes application versions and replaces MCP pods.

Prerequisites are the existing Docker/Floci environment, isolated
`.local/floci.kubeconfig`, Python `.venv`, working ECR images and Redis volumes.
Allow about 6–9 minutes. Use the printed PASS/FAIL summary and the new
`evidence/demo/<timestamp>-quick/` folder. Do not run Full during this handoff;
use its saved measurements. Full's recorded outcome remains **FAIL** for one
HTTP 502, even though scaling and recovery checks passed.

## What has actually been verified

| Capability | Evidence-backed result |
| --- | --- |
| Real MCP | SDK 2.3.0, sessionless Streamable HTTP `/mcp`, port 8000, `/health` |
| Two application types | echo/add; Redis-backed atomic increment/get |
| Shared pipeline | Dockerfile/mcp.json projects, including an independent custom-tool fixture |
| Kubernetes routing | Both always-on Deployments 2/2 ready; two ready endpoints each; actual MCP calls through Services |
| State survives application lifecycle | Separate connections and exact pods share Redis; replacement pods retain counters |
| Request scaling | Each example automatically reached six ready replicas under controlled MCP concurrency |
| Zero and wake | Both reached zero app pods, then woke through actual gateway MCP requests |
| Persistence at zero | Stateful cold read returned expected counter **161** |
| Release/recovery | Rolling updates, Knative revision promotion and failed-image rollback executed |
| Regressions | **29 passed**, latest recorded suite 17.41s; not rerun for this review |
| Continuity | Stateless 601/602; stateful 602/602 during deployment/recovery activity; one failure retained |

The Full load tests used eight concurrent clients and 3-second asynchronous
tool delays to sustain concurrency. Fast echo alone would not establish the
same scaling behavior. Independent-service redeployment and revision promotion
in the final demo reused existing image bytes; it did not rebuild new code
under traffic.

## Performance talking points

Saved warm baseline: 30 calls/service after warmup, fresh MCP connections,
12-second baseline at 2.5 tool calls/sec/service through local Kourier.

| Service/workload | Warm p50 | Warm p95 | Warm p99 | Separate cold observation |
| --- | ---: | ---: | ---: | ---: |
| Stateless echo, **includes deliberate 600ms delay** | 838.2ms | 930.4ms | 932.7ms | 27.341s, undelayed echo |
| Stateful Redis get_counter, no injected delay | 251.0ms | 323.3ms | 335.2ms | 15.419s |

These include protocol initialization and client teardown. They are not
server-only execution times or production internet latencies. p50 is median;
p95/p99 use nearest rank. With 30 samples, p99 is the observed maximum, not a
reliable tail SLO. Cold figures each have **one sample**, so do not describe
them as cold percentiles. No new benchmark was needed or executed.

## Explain the one HTTP 502 candidly

"One stateless MCP request failed among 1,204 requests. Kourier logged a 502
through the activator while the old revision was serving, a surplus old pod
was draining, and the new revision was starting. Candidate readiness and route
promotion happened afterward. A transient upstream connection or draining race
is the leading hypothesis, but we lack the per-request pod trace to prove it."

The 502 started at **20:06:33.783 UTC**; candidate Ready was **20:06:44** and
route status **20:06:48**. Do not claim this proves premature promotion, data
loss, a Redis outage, or general zero downtime. The old revision still had two
logged backends; current endpoint snapshots cannot reconstruct the exact
failure-time selection.

The smallest consumer mitigation for echo is one bounded jittered retry on
502. Never blindly retry increment_counter after an uncertain response; it
could increment twice. A short Knative scale-down delay is a possible future
configuration experiment to reduce churn, not a verified fix. No mitigation
was applied automatically. See [technical analysis](TECHNICAL_REVIEW.md).

## Why EKS, Knative and Redis

**EKS/Kubernetes:** a common deployment API, health-gated routing, resource
requests, replica management and a path to self-hosting. Locally, Floci emulates
AWS lifecycle/registry APIs while actually running k3s in Docker. This does not
validate production EKS identity, networking or AZ behavior.

**Knative:** adds concurrency-based application scaling, activation from zero
and revision routing to the same container pipeline. Kourier is ingress; the
activator buffers/routes during activation; the autoscaler requests replicas;
queue proxies measure and forward requests. These components remain up when
application replicas are zero. Always-on workloads retain the Deployment
backend. [Knative request flow](https://knative.dev/docs/serving/request-flow/)
describes the upstream model.

**Redis:** independent MCP replicas need an external source of truth. INCRBY
is atomic and GET reads shared state without sticky sessions. Each namespace
has its own Redis dependency. The local single-replica Redis/AOF/PVC setup
does not establish redundant storage, exactly-once requests or Redis failover.

## Cost versus ECS/Fargate

EKS is a portability and scaling choice, not an unconditional lowest-cost
claim. Ordinary ECS orchestration has no additional fee; standard-support
EKS is $0.10/cluster-hour (about $73 for 730 hours), excluding workers and other
resources. [ECS pricing](https://aws.amazon.com/ecs/pricing/),
[EKS pricing](https://aws.amazon.com/eks/pricing/).

The existing illustrative model includes two active replicas/project,
0.10 vCPU/128 MiB application requests, Knative overhead, worker reserves,
load balancing, NAT/IPv4, storage, image storage, Redis and telemetry budgets.

| Planning scenario, monthly total | ECS/Fargate | ECS/EC2 | EKS/EC2 | EKS/Fargate |
| --- | ---: | ---: | ---: | ---: |
| 10 active projects | $540.44 | $769.90 | $842.90 | $680.48 |
| 10 idle projects | $335.90 | $745.56 | $818.56 | $475.93 |
| 1,000 continuously active projects | $21,041.75 | $12,555.20 | $15,051.72 | $21,181.78 |

These are **scenario calculations**, not measured AWS bills or newly verified
SKU quotes. They use AWS-published reference rates plus explicitly assumed
quantities and Redis/operations budgets. See [the full cost model](COST_COMPARISON.md).
Tiny projects can pack below Fargate's minimum billable sizes on EC2; Fargate
can win for small or idle installations and reduces host operations. ECS/EC2
is cheaper than EKS/EC2 in this model. Knative app scale-to-zero frees capacity;
it does **not** reduce an unchanged EC2 fleet's bill. Worker consolidation is
separate, unimplemented production work.

## Scaling to thousands: design direction, not a result

Distinguish registered projects, active projects, replicas and concurrent
requests. One thousand projects at two active replicas means 2,000 application
pods plus platform/dependency capacity; mostly idle projects have a different
cost and cold-activation profile. No thousand-project experiment was run.

Measure representative tools, startup times and Redis usage first. Then size
and validate worker/pod-IP capacity, image pulls, API/controller load, revision
retention, ingress routes, telemetry cardinality and registry quotas. Add tenant
quotas, deployment serialization/locking, backpressure, isolation and capacity
management. Shared Redis must be sized or partitioned by throughput and memory,
with explicit tenant boundaries and recovery objectives. Knative replica
scaling cannot create missing worker capacity by itself. This work is future
engineering, not functionality installed in the local cluster.

## Multi-region and self-hosting: future approach

Multi-region would use independent regional clusters, local ingress and
registries, a global routing/failover policy and a coordinator tracking regional
versions. Define data ownership, consistency/conflicts, RPO/RTO and failover
drills before replicating Redis writes. A naive active-active global counter
does not preserve the demonstrated single-Redis semantics. Nothing multi-region
was implemented.

Self-hosting would preserve Dockerfile/mcp.json and Kubernetes/Knative APIs,
replacing AWS registry, identity, ingress/DNS/TLS and storage integrations.
Operate redundant control/worker nodes, Redis, backups and upgrades. The current
CLI already accepts an explicit kubeconfig; production operations remain work
even when the application contract is portable.

## Known limitations

One Docker host and one k3s node are a shared failure domain. Runtime CPU quota
is four CPUs; scheduler allocatable CPU remains 1930m. Local infrastructure
components and Redis use one replica. `/health` in the examples reports process
health, not Redis dependency health. No production HA, multi-AZ failure test,
host-loss recovery, zero-downtime guarantee or thousand-server test is claimed.

Public DNS/TLS, MCP/deployment authorization, secret lifecycle, tenant isolation,
distributed deployment locking and production observability are not complete.
These sessionless examples do not establish arbitrary long-lived MCP session
or stream recovery. Cold activation has noticeable latency. Ambiguous mutation
failures need idempotency before safe retries.

## Questions Enrico may ask

| Question | Answer to lead with |
| --- | --- |
| Is stateful MCP tied to a pod? | No. Application counters live in Redis; the MCP transport itself is sessionless. |
| What wakes a service at zero? | The running ingress/activator path holds the request while Knative requests app replicas. |
| What remains billable at zero? | Workers unless separately reduced, ingress, controllers, Redis, storage and networking. |
| Can any project use this? | Any compatible Dockerfile/mcp.json project meeting the port/health/HTTP contract; custom fixture verified. Arbitrary runtime compatibility has limits. |
| Why not ECS? | ECS is valid and can be cheaper. Kubernetes was chosen for Knative and a self-hosting path, with explicit operational overhead. |
| Are two replicas HA? | They tolerate some pod replacement scenarios; one host and single Redis are not production HA. |
| How do updates recover? | Retain prior healthy resources/route, promote after readiness, restore on failure, and still return nonzero for a failed deployment. |
| Did a rollout lose requests? | Yes: one 502 in the saved Full continuity run. The test remains FAIL. |
| Does Ready mean requests cannot fail? | No. Process/readiness checks and route status do not prove every downstream connection succeeds. |
| Can we retry failed increments? | Not safely without deduplication/idempotency; an increment may have committed before response loss. |
| What does p99 mean here? | Warm baseline p99 is the maximum of only 30 samples; it is not a production tail guarantee. |
| Have thousands or multiple regions been tested? | No. Those are planning designs and cost assumptions. |
| What would you do next? | Trace the failing request path, validate a narrow mitigation, then address identity/isolation, Redis recovery and measured capacity. |

Use [ARCHITECTURE.md](ARCHITECTURE.md), [FINAL_DEMO_REPORT.md](FINAL_DEMO_REPORT.md)
and [TECHNICAL_REVIEW.md](TECHNICAL_REVIEW.md) for supporting detail. This review
changed documentation and analysis artifacts only; it did not rerun the demo
or regressions or alter a working component.
