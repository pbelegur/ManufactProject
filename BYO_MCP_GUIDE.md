# Bring your own MCP server

This is a trusted, localhost-only developer demo. It builds application code on
the existing Docker engine and deploys through **the same `deployctl.py`** used
by the original stateless and Redis-backed servers. No new cluster, builder,
gateway or framework is installed.

## Open the UI

The existing UI runs at **http://127.0.0.1:8080**. If it is stopped, from the
repository root run:

```powershell
.venv-ui/Scripts/python.exe -m ui --port 8080
```

For a fresh Python UI environment, use the existing isolated requirements:

```powershell
python -m venv .venv-ui
python -m pip --python .venv-ui install -r requirements-ui.txt
```

The CLI environment remains `.venv`; it must already contain the platform and
MCP dependencies. Kubernetes uses `.local/floci.kubeconfig`, never the user's
default context. Do not start a second UI process while a job is active.

## Deploy the independent text-tools sample

The complete source lives in [examples/text-tools-mcp](examples/text-tools-mcp).
It has its own `server.py`, Dockerfile and pinned MCP dependency. It is not the
old compatibility test fixture. Its `analyze_text` tool reads text and returns
word/character/line counts and a slug.

1. Open **Add a server** and download **working Text Analyzer project (.zip)**. This is a complete project ZIP with no mcp.json, deliberately
   exercising guided configuration.
2. Select **Upload a project ZIP**, choose that ZIP, review the code
   and acknowledge that you trust it and that it contains no credentials.
   Click **Import and review configuration**. Importing does not execute code.
3. Choose a readable **Display name**, such as My Text Tools, and a **new unique server ID**, for example `my-text-tools`. The evaluation
   deployment uses `manufact-text-tools`, so use another name for a new import.
   Names are lowercase DNS labels, at most 52 characters, and reserved across
   both UI-managed namespaces and registered projects.
4. Use these onboarding fields:

   | Field | Value |
   | --- | --- |
   | Dockerfile path | `Dockerfile` |
   | Build context | `.` |
   | Port | `8000` |
   | MCP endpoint | `/mcp` |
   | Application state model | **Stateless**, chosen explicitly |
   | Deployment mode | **Always-on**, two replicas |
   | Release | `v1` |
   | Read-only tool | `analyze_text` |
   | Tool arguments | `{"text":"Manufact builds MCP tools"}` |

5. Confirm that the selected tool is read-only and that you trust this source.
   Click **Save and continue to deployment**. This persists the source and deployment identity;
   it does not start a deployment.
6. On the deployment form, the imported project is selected. Choose **Build this project's code and deploy a new image**, review the configuration, then click
   **Deploy**. The page returns a job ID immediately and polls real CLI output.
7. Follow **Inspect Kubernetes deployment** after the job succeeds. Confirm
   `Healthy`, `2 / 2`, the image/digest, source provenance and actual endpoint.
8. Click **Test MCP tool**. Expected response:

   ```json
   {"words":4,"characters":25,"lines":1,"slug":"manufact-builds-mcp-tools"}
   ```

   The page displays the actual MCP response, timing and any error. It performs
   one attempt, never increment_counter, and never retries a mutation.
9. Load **current pod logs**, inspect **recent Kubernetes events**, the
   **Kubernetes rollout history** and the linked **deployment/build job log**.
   The dashboard links the source, runtime identity and latest job.

For an existing registered server, use **Deploy / update** with its project ID
or **Redeploy published image** on its details page. Reuse does not rebuild.
An identical always-on image may be an idempotent no-op. Knative reuse requires
the published digest, matching the existing local registry behavior.

Display names can be changed on the details page without renaming Kubernetes
resources or altering endpoints. Job history uses names and attempt numbers;
the full job UUID remains available for diagnostics. Unfinished uploads are
resumable from the import page, dashboard or deployment page. A failed form
keeps its source and editable values. Existing services keep running while
a new project is imported, built and verified.

The evaluated uploaded project is **My Text Analyzer**, server ID
`text-analyzer-upload`. The original independent sample is **Text Analyzer**,
ID `manufact-text-tools`; both are running. Use a fresh server ID for another
import. The GitHub quickstart URL is a trusted tutorial source, not a compatible
Docker/HTTP project out of the box. The UI warns when no Dockerfile is present.

See [UI_REPAIR_REPORT.md](UI_REPAIR_REPORT.md) for the restart/import bug fix,
executed browser checks, original-service preservation and remaining limits.

## Your project and mcp.json

The ZIP must contain the application, dependencies and Dockerfile. A single
top-level project folder is unwrapped. Dockerfile and build-context paths are
relative to that staged project root; both must stay inside it. A monorepo can
select, for example, `services/tool/Dockerfile` and `services/tool`.

The existing runtime contract is intentionally preserved:

- Real MCP Streamable HTTP at `/mcp`, container port 8000.
- GET `/health` for Kubernetes probes.
- Configurable environment, graceful SIGTERM handling and independent replicas.
- Stateful application data in shared Redis, using `REDIS_URL`.

Alternate ports/endpoints are shown as fields but **rejected clearly**: the
existing deployment engine supports only 8000 and `/mcp`. They are not silently
translated into a fictional endpoint.

If mcp.json exists, the guide reads its name, state model and build-path defaults.
It preserves valid non-secret application environment settings. The final
generated `.deploy-<backend>.json` uses the chosen unique identity, fixed local
resource bounds and explicit ownership metadata. It does not blindly apply
arbitrary resource, namespace or credential settings from imported JSON.
If mcp.json is absent, the state-model selector remains empty and required.

For **Stateful with shared Redis**, leave the Redis URL blank for the CLI's
server-scoped persistent Redis/PVC, or explicitly provide a credential-free
existing Redis URL. The application must actually use Redis; selecting the UI
field cannot convert in-process variables into durable state. Sensitive
environment values and inline Redis credentials are unsupported in this demo.

For **Knative**, the existing local profile uses min=0, activation=2, max=4,
target concurrency=2 and a 20-second scaling window. Redis stays running outside
MCP pods. Ready at zero is labeled **Idle/Scaled-to-zero**, not Healthy with two
replicas. The endpoint remains the real Knative route; Test MCP forwards the
existing Kourier Service and uses the route Host header for activation.

## Trusted GitHub import

An operator must first add an exact public URL to
[ui/trusted_repositories.json](ui/trusted_repositories.json), then restart the
idle UI to load that allowlist. Example format:

```json
{"repositories":["https://github.com/your-organization/your-mcp-project"]}
```

Only `https://github.com/OWNER/REPOSITORY` URLs in that explicit list are accepted.
There is no arbitrary URL fetch, local git path, SSH URL or shell command field.
The importer resolves the default branch using GitHub's public commit API,
records its 40-character commit SHA, then downloads the archive **for that SHA**.
It also records the archive SHA-256. Submodules, git hooks and LFS are not run.
Their required content must already be present in a complete compatible project.

The preapproved official `modelcontextprotocol/quickstart-resources` repository
is useful for verifying GitHub source staging. Staging it is not a claim that
every project in that tutorial monorepo has the required Dockerfile/HTTP contract.
Choose a compatible project and valid build paths before deployment.

## Identity, status and monitoring

New imports reserve a unique server name. Existing infrastructure is not renamed:
legacy examples already run with the same base name in two namespaces, so every
runtime is identified explicitly by **namespace/name**, such as
`mcp-demo/stateless-demo` versus `mcp-auto/stateless-demo`.

New resources carry `platform.manufact.dev/project-id` and `category` labels.
Original examples have explicit metadata in `ui/projects.json`; old test probes
are explicitly adopted in `ui/runtime_metadata.json`. Unknown runtimes remain
unregistered rather than being classified by filename/name patterns.

Runtime health comes from observed generation, ready/desired/available counts,
conditions and container failures. Partial readiness is never Healthy. An active
job is **Deploying**; Knative replica convergence is **Scaling**; process failures
are **Failed/Degraded** as appropriate. A failed job can coexist with a Healthy
previous version after rollback. No success is inferred merely from submission.

Last-deployed time comes from a successful matching job after CLI health
verification, or a clearly labeled Kubernetes rollout/route condition timestamp.
Unknown timestamps remain unknown. History shows actual retained
ReplicaSets/Knative revisions; jobs show recorded UI submissions. Manual checks
are not general request analytics. No RPS, historical traffic chart or SLO is
fabricated.

## Bounds and limitations

ZIP imports are bounded to 20 MiB compressed, 50 MiB expanded, 20 MiB/file,
2,000 files, 4,000 entries, depth 20 and a maximum 100:1 compression ratio.
Traversal, absolute/drive paths, case collisions, symlinks/special files,
encrypted ZIPs and obvious credential directories/files are rejected. Staging is
limited to 20 source imports; an operator must archive old local staging if full.

All UI builds use the existing Docker engine's legacy build path to
enforce **one CPU and 1 GiB memory/swap** on build containers, with a **300-second
build-command timeout**. This does not create another builder or change the
default terminal CLI BuildKit pipeline. Legacy building is deprecated;
if a future engine removes it, imports fail rather than silently build without
these limits. Full UI jobs are limited to 20 minutes. These are local resource
bounds, **not a malicious-code sandbox or a host-wide storage quota**.

The UI has a local worker pool (`MCP_UI_MAX_JOBS=1..3`, default 2), eight waiting
jobs and per-resource submission ordering. Each build copies the registered
source to its own bounded directory; runtime configuration stays outside that
Docker context. Each UI build has a job-UUID image label and unique versioned
reference. The job page polls real stages and timestamped logs every second.
Logs are redacted before writing and rotated through three bounded segments;
older output can be lost, while retained files remain after a failed job or UI
restart. The in-memory queue and locks do not survive process restarts or span
multiple API instances. Keep one UI process and avoid competing CLI updates.

The executable UI binds only to `127.0.0.1`, rejects non-loopback clients,
untrusted Host/cross-origin requests and missing CSRF tokens. Commands use argv
lists with `shell=False`; no raw user command is accepted. Builds receive only
their staged project context, never a Docker socket or Kubernetes credentials.
Details do not return kubeconfig or secret objects. Log redaction is best effort;
never upload credentials or trust arbitrary code to avoid printing them.

Production requires authentication/authorization, isolated build workers with
enforced cancellation and storage/network quotas, secret management, durable
job coordination and a source trust/review policy. This phase does not implement
those controls, multi-region infrastructure, a new ingress or production HA.
