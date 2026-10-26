# Compatible MCP projects

`deployctl.py` accepts any project directory containing `Dockerfile` and `mcp.json`.
`--config FILE` selects another project-relative JSON file without changing the
Docker build context or application code layout.
It does not import application code, assume a Python entry file, inspect tool
names, or choose a separate deployment implementation by example name.

The container must listen on `0.0.0.0:8000`, expose real MCP Streamable HTTP at
`/mcp`, return HTTP 200 on `/health` when ready to accept requests, and handle
SIGTERM within 30 seconds. Use sessionless HTTP or otherwise externalize transport
state; this pipeline does not provide sticky sessions. Application state belongs
in Redis or an external dependency rather than MCP pod memory/filesystems.

Minimal `mcp.json` (defaults to stateless, port 8000, two replicas):

```json
{"name": "my-mcp-server"}
```

An application-stateful project with managed local-test Redis:

```json
{
  "name": "my-counter-service",
  "mode": "stateful",
  "state_backend": "redis",
  "replicas": 2,
  "port": 8000,
  "env": {"CUSTOM_OPTION": "example"},
  "resources": {
    "requests": {"cpu": "100m", "memory": "128Mi"},
    "limits": {"cpu": "500m", "memory": "512Mi"}
  },
  "redis": {"storage": "1Gi", "storage_class": "local-path"},
  "progress_deadline_seconds": 180
}
```

| Field | Behavior |
| --- | --- |
| `name` | Lowercase Kubernetes DNS label, starting with a letter, at most 63 characters |
| `mode` | `stateless` (default) or `stateful` |
| `replicas` | Positive integer; default 2; `--replicas` overrides it |
| `port` | Must be 8000 for this contract |
| `env` | String environment values; forwarded to the container |
| `resources` | CPU and memory in both `requests` and `limits`; defaults shown above |
| `state_backend` | Must be `redis` for stateful projects |
| `redis.name` | Optional managed Redis name; otherwise unique application-derived name |
| `redis.storage` | Managed PVC size, default 1Gi |
| `redis.storage_class` | Optional StorageClass; omitted uses cluster default |
| `image_pull_secrets` | Optional list of pre-existing Kubernetes Secret names |
| `progress_deadline_seconds` | Deployment progress deadline, default 180; must exceed 3 |

`HOST=0.0.0.0` and `PORT=8000` are reserved by the deployment contract.
Service DNS names are added to `MCP_ALLOWED_HOSTS` along with project-supplied
additional values. The application must support its Service hostname or consume
this setting when using host validation.

For stateful projects, an explicit `env.REDIS_URL` uses that external Redis and
skips managed Redis provisioning. Otherwise the pipeline creates an application-
scoped Redis Deployment, ClusterIP Service and PVC, then sets `REDIS_URL` in all
MCP replicas. Managed Redis is a single development instance, not a Redis HA setup.
The pipeline never deletes PVCs on a failed application rollout. Plain `env`
values are visible in Deployment specs; use pre-existing private registry Secrets
for `image_pull_secrets`. Secret-valued application configuration needs a future
Secret reference extension rather than committing credentials to `mcp.json`.

## Build and deploy

```powershell
python deployctl.py deploy PATH --push-registry REGISTRY/PREFIX --version v1 --replicas 2 --kubeconfig PATH_TO_CONFIG
python deployctl.py deploy PATH --kind-cluster mcp-demo --version v1 --replicas 2
```

Each invocation follows the same path: validate project and healthy baseline,
Docker build, tag, publish or kind load, provision Redis if needed, validate
manifests against the API, apply application resources, wait for rollout, and
report readiness. Only the image-publishing adapter differs. The local kind
adapter selects `kind-NAME` automatically; `--context` and `--kubeconfig` are
available to target an explicit cluster without changing the current context.

Final image tags are `VERSION-FULL_DOCKER_BUILD_ID`. The default version prefix is
`build`, so unchanged repeated builds/deployments remain idempotent. Different
image bytes or an explicit version change produce a different tag. BuildKit
provenance is disabled here to avoid timestamped attestations changing otherwise
identical image IDs. Production supply-chain attestation is a separate extension.

`--image REGISTRY/PROJECT@sha256:DIGEST` can deploy a pre-published image without
building it. It is mutually exclusive with publishing adapters. The caller owns
registry authentication, image availability, and any required pull Secrets.
Changing `--version` on this path has no effect; the supplied reference is used.

Rolling updates use `maxUnavailable=0`, `maxSurge=1`, three seconds of minimum
readiness, health probes, and a 30-second termination grace period. On a failed
application apply/rollout the CLI collects diagnostics and restores the previous
healthy Deployment, Service and PDB; it exits nonzero even if recovery succeeds.
A failed first deployment removes its new application resources and retains
dependency data. An already unhealthy deployment must be recovered before a new
version is deployed. `--timeout` controls each rollout wait (default 180 seconds).

This synchronous local CLI assumes one trusted operator per application. It has
no distributed deployment lock, queue, tenant isolation, admission policy, or
multi-user concurrency control. Health is only as meaningful as the project's
`/health` endpoint. In the two examples it reports process health, not Redis health.

## Optional Knative backend

An `autoscaling` object with `enabled: true` selects Knative Serving; omission or
`enabled: false` keeps the Deployment backend. Both use the same build/publish,
application environment and managed/external Redis code. Serving and networking
must already be installed.

```json
{
  "name": "my-mcp-server",
  "autoscaling": {
    "enabled": true,
    "minReplicas": 0,
    "activationReplicas": 2,
    "maxReplicas": 10,
    "targetConcurrency": 2,
    "windowSeconds": 60,
    "routeDomain": "mcp.test"
  }
}
```

| Configuration | Revision annotation |
| --- | --- |
| `minReplicas` | `autoscaling.knative.dev/min-scale` |
| `activationReplicas` | `autoscaling.knative.dev/activation-scale` and `initial-scale` |
| `maxReplicas` | `autoscaling.knative.dev/max-scale` |
| `targetConcurrency` | `autoscaling.knative.dev/target`, metric `concurrency`, class KPA |
| `windowSeconds` | `autoscaling.knative.dev/window` (6-3600 seconds) |

Initial scale is the larger of minimum and activation. Activation defaults to two
and must be at least two here. Target concurrency is a soft per-pod target, not
RPS or a hard limit; Knative target utilization also affects desired counts.
`--replicas` is rejected for this backend; use autoscaling bounds instead.

`routeDomain` must match Knative's configured domain. It adds the external route
to `MCP_ALLOWED_HOSTS`, without provisioning DNS or TLS. Local tests use `mcp.test`,
the Kourier gateway and an explicit HTTP Host header. Gateway/activator stay up at
zero application replicas. Redis remains always-on outside MCP pods.

Knative declares port 8000 as `http1` and supplies its reserved `PORT` variable.
`/mcp`, `/health` and SIGTERM handling remain. Health probes use integer port 8000.
Knative adds the queue proxy and owns replica counts, draining and its generated
Deployment. The CLI creates no conflicting application PDB or manual scale setting.

Built images use their published digest, avoiding controller-side localhost tag
resolution while k3s pulls through Floci's registry mirror. Release version is a
revision annotation; changing it creates a revision even with identical image
bytes. An update pins 100% traffic to the prior ready revision while its candidate
starts, then promotes the ready candidate. Failure restores the prior template
and pins the old route. This is not a canary or process-crash recovery system.

Use separate namespaces to compare backends; the CLI rejects taking names owned
by the other backend. Provided `mcp.knative.json` profiles demonstrate max 10 one
service at a time. `mcp.knative.local.json` caps four for simultaneous local tests.
Bounds do not promise enough host capacity for every service's maximum at once.

Candidate selection checks the desired revision template, rather than trusting a
possibly stale `latestCreatedRevisionName`. Promotion waits for the actual status
traffic target to be 100% on that revision. `--timeout` bounds candidate startup;
`--rollback-timeout` independently bounds recovery (default 180 seconds). After
restoring the previous route/configuration, the CLI removes its failed unrouted
candidate so failed image pulls do not reserve local capacity indefinitely.
# Imported-project build inputs

The same `deployctl.py` build function also accepts optional project-contained
paths in `mcp.json`: `build.dockerfile` (default `Dockerfile`) and `build.context`
(default `.`). Absolute paths and paths that resolve outside the project are
rejected. Runtime port 8000, MCP `/mcp` and health `/health` remain unchanged.

The trusted local UI generates `build.bounded: true` for imported projects. This
opts that build into the existing Docker engine's legacy builder with 1 GiB
memory, no additional swap, one CPU quota and a 300-second build timeout. It does
not change default builds or create a new builder. An unsupported legacy backend
fails the build instead of falling back to unlimited execution.

Optional `platform_metadata.project_id` and `platform_metadata.category` become
explicit ownership labels on workload resources. They do not change selectors,
replica behavior or rollback semantics. `--ensure-floci-repository` provisions
only the application's ECR repository on the fixed local Floci registry; it does
not provision or configure EKS, storage, networking or Knative.

