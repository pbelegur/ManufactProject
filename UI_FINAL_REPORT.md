# Phase 5: Manufact-focused developer experience

Completed **2026-10-26 UTC** on the existing local environment.

**The third independent MCP project was uploaded through the browser, registered,
built, published to Floci ECR and deployed at 2/2 ready replicas. Its actual
`analyze_text` MCP call succeeded from the details page.** The original stateless
and Redis-backed services remain healthy and unchanged.

Open **http://127.0.0.1:8080**, then select **manufact-text-tools** in **My MCP
servers**, or open its [local details page](http://127.0.0.1:8080/deployments/mcp-demo/manufact-text-tools).
Follow [BYO_MCP_GUIDE.md](BYO_MCP_GUIDE.md) to import another project with a new name.

## Implemented developer workflow

- **My MCP servers:** source project/provenance, namespace/name, image version,
  runtime revision, actual ready/desired counts, health, last verified deployment
  with its timestamp basis, and latest recorded UI job. Each row links to details.
- **Explicit ownership:** developer/probe/unregistered groups use configured
  metadata and resource labels, not filename or name-pattern guesses. Existing
  examples are adopted through `ui/projects.json`; old probes have explicit
  `ui/runtime_metadata.json` entries. New workloads carry project/category labels.
- **Truthful statuses:** Healthy requires complete readiness; partial Knative
  readiness is Scaling, failures are Failed/Degraded, active jobs are Deploying,
  and healthy zero-replica routes are Idle/Scaled-to-zero. Failed job results
  remain separate from a previous runtime recovered by rollback.
- **Details:** source identity, current image and observed pod image IDs, actual
  endpoint, revision, jobs/build logs, retained ReplicaSet/Knative revision history,
  related Kubernetes events, MCP container logs and credential-free Redis settings.
- **Test MCP:** one reviewed read-only tool from stored policy, using the official
  client over real Streamable HTTP. Shows the actual response, initialization/tool
  timing and errors. No automatic retries; increment_counter is explicitly
  forbidden. The UI does not invent request volumes, charts or traffic metrics.
- **Bring your own source:** complete ZIP, exact trusted public GitHub URL, or a
  copy of an existing example template. Configuration is reviewed before source
  registration; deployment remains a separate asynchronous job. Missing mcp.json
  requires an explicit state-model selection.

New imports reserve names across both managed namespaces and existing resources,
including Redis names. Original resources were not renamed: legacy examples
already have the same base name in two namespaces and are shown using their
unique **namespace/name** identities. No namespace migration was performed.

The original port-8000, `/mcp`, `/health` contract is preserved. Other ports or
MCP paths are rejected clearly, rather than displayed as supported endpoints.

## Same deployment pipeline

The browser job invokes **`deployctl.py deploy`**, with validated argv and
`shell=False`. There is no second Docker/ECR/Kubernetes implementation in the UI.
Small additions to the existing CLI provide:

1. Optional project-contained Dockerfile and build-context paths.
2. Opt-in bounded local imports: existing Docker engine, one CPU, 1 GiB memory,
   no extra swap, and a 300-second build-command timeout. This uses the engine's
   supported but deprecated legacy builder; original default BuildKit builds
   retain their existing behavior. No separate builder was installed.
3. Explicit ownership labels without changing workload selectors.
4. Optional creation of **only the application's ECR repository** on the fixed
   local Floci endpoint. No EKS, storage, Knative or network setup is called.

Two replicas, Service/PDB creation, probes, rolling-update settings and recovery
remain in the existing deployment code. Images retain the full build-ID tag.
Build/rollout phases are inferred from actual emitted CLI markers, not fabricated
percent-complete values. UI jobs have a 20-minute outer execution bound.

## Actual third-project deployment

| Field | Observed result |
| --- | --- |
| Independent source | [examples/text-tools-mcp](examples/text-tools-mcp), five files, no imports from the two original servers |
| Import | Complete project ZIP uploaded in Edge; **no mcp.json** |
| ZIP SHA-256 | `dc773ecf74bb605349f6211608d8c1617b0740aa6494078a69729b12a4b3f01c` |
| Guided choice | Explicit stateless, always-on, Dockerfile, context `.`, 8000, `/mcp` |
| Server | `manufact-text-tools` |
| Project ID | `byo-9a1c65f1bfd64bc48459` |
| Runtime | `mcp-demo/manufact-text-tools` |
| Release | `phase5-v1` |
| Successful UI job | `4ce1cba0091c42a8bc9d7092c1db99d6` |
| Job duration | **386.853 seconds**, 22:06:03.662–22:12:30.515 UTC, including first build, publish and rollout |
| CLI result | Exit **0**, healthy, **2/2 ready replicas** |
| Image/digest | `localhost:4566/000000000000/us-east-1/manufact-text-tools:phase5-v1-ce7382a9b2b5f49dfd838049d6f98949b2c2ec18c3aa21ff5d92c07dd85cf1b7`; observed pod digest `sha256:ce7382a9b2b5f49dfd838049d6f98949b2c2ec18c3aa21ff5d92c07dd85cf1b7` |
| Service endpoint | `http://manufact-text-tools.mcp-demo.svc.cluster.local:8000/mcp` |
| ClusterIP | `10.100.157.200:8000` |
| Runtime revision | Deployment revision **1** |
| Last verified deployment | `2026-10-26T22:12:30.515437+00:00`, successful matching job finish |

Actual UI Test MCP input:

```json
{"tool":"analyze_text","arguments":{"text":"Manufact builds MCP tools"}}
```

Actual response:

```json
{"words":4,"characters":25,"lines":1,"slug":"manufact-builds-mcp-tools"}
```

The single manual check measured **2,520.39 ms** for client initialization plus
tool call, of which **221.14 ms** was the tool-call round trip and **2,299.20 ms**
initialization. Port-forward setup is excluded from those timings. These are
one-off host/UI checks, **not warm latency percentiles** and not comparable to
the prior in-cluster pooled benchmark. No delay was injected.

The browser verified source linkage, observed digest, correct job links, the
current MCP pod logs, actual endpoint, healthy replicas, and developer/probe
separation. The failed-build job did not appear in the successful server's
details history.

## GitHub, failures and security evidence

**Real GitHub staging passed**, resolving the explicitly approved public source:

- Repository: `https://github.com/modelcontextprotocol/quickstart-resources`
- Resolved commit: `38e14ca4ce362d27dfaaa498f3b4592bea4c6710`
- Downloaded archive SHA-256: `2cf79b7f6f847ac1450edd00daa0f169f2a27af9cfe6305d4081f2a0fdde94bb`
- Extracted: **66 files, 812,571 bytes**.

This verifies public-source resolution, commit pinning and staging. The tutorial
repository itself was **not registered or deployed**; the third runtime came
from the independent compatible ZIP project above.

Failure evidence:

- A browser ZIP upload with `../escape.py` was rejected with a clear import error.
- A separate complete source with an invalid Dockerfile was registered as
  `manufact-invalid-build`. Its real build failed with `unknown instruction:
  NOT_A_DOCKERFILE`; job `569481156e8a425cb309d8bfa8888685`, exit **1**, duration
  **6.308 s**. No workload was created. The dashboard shows **Failed**, no image
  or endpoint, and unobserved replica counts instead of invented runtime data.
- **14 live HTTP guards passed**, including invalid CSRF, untrusted/SSRF URL,
  existing server/Redis names, escaped/absolute build paths, unsupported port/path,
  increment_counter, absent read-only confirmation, non-object arguments,
  arbitrary command field, shell-like release and missing state model.
- **Nine live build containers** were inspected during the actual build:
  `Memory=1073741824`, `MemorySwap=1073741824`, `CpuPeriod=100000`,
  `CpuQuota=100000`, `Privileged=false`, `Binds=null`. This verifies the requested
  container limits and absence of host bind mounts in those observed containers.
- UI listener verified at **127.0.0.1:8080** only. The server-side APIs do not
  expose Docker socket access, a shell, kubeconfig contents or Kubernetes Secrets.

ZIP limits and traversal, symlink, collision, credential-file, compression-ratio,
expanded-size and file-count validation are covered by focused unit tests.
These controls and local cgroup limits **do not make uploaded code safe for
untrusted users**. Docker daemon activity, image storage and arbitrary trusted
build code are not fully isolated. Log redaction is best effort; operators must
not import credentials. Production requires auth and isolated build workers.

## Preservation and tests actually executed

- **46 focused unit tests passed in 2.80 s**: import/security/status tests plus
  the existing CLI/manifest tests, including rollback behavior. Initial focused
  run had 44 passes before two timestamp-specific tests were added.
- **14 live HTTP guard checks passed**, without submitting a deployment job.
- **Browser onboarding and resumed verification passed**, with no JavaScript
  errors. Real Edge **156.0.4314.42**, driven by the already installed Playwright.
- **35 inventory samples** during onboarding/build observed stateless, stateful
  and compatibility-fixture deployments healthy at 2/2. Sampling is not a
  continuous availability guarantee.
- **Six real MCP checks** against the original examples succeeded: before,
  during the new build and after deployment. All three Redis get_counter checks
  returned the existing persisted value **13**. No original counter was
  incremented as a health check.
- Before/after snapshots confirm unchanged Deployment UIDs/specs and application
  pod UIDs for `stateless-demo`, `stateful-demo`, `stateful-demo-redis` and
  `contract-probe`. Their existing healthy resources were not redeployed.
- Floci's k3s container remains
  `9907087e45e8f9dfd51f846673485fd5a6de6dbc8b677bfa66aa86f9b1045792`, started
  `2026-10-26T18:12:54.846142035Z`; no cluster/container recreation or restart.
- The original Knative routes remain idle/Ready at zero, not falsely labeled
  2/2. The new imported project was verified on the always-on backend; a new
  imported Knative rollout was **not additionally exercised** in this phase.
  Its configuration is routed through the existing tested backend, but this
  report does not claim a fresh end-to-end BYO Knative deployment test.

Two verification defects were handled transparently:

1. The first browser test aborted **after a successful deployment** while saving
   Unicode pip output using Windows' default text encoding. The writer was
   corrected to UTF-8. Verification resumed against the existing deployment;
   **no second image build or deployment** was submitted. The initial collector
   traceback is retained in `summary.json` as `initial_collection_error`.
2. Image-only job matching could attribute the old UI job's time to a newer
   Knative revision created from identical bytes by the terminal CLI. Matching
   now requires the actual routed revision. For older jobs lacking that metadata,
   the UI uses the labeled Kubernetes route-condition time. Two focused tests
   cover this case; the final dashboard check confirms stateless revision 00015
   uses **21:28:48Z**, not the older UI job's completion time.

The complete 29-test historical suite, Full demo, autoscaling load campaign and
new rolling-update continuity test were **not rerun**. The historical unresolved
502 is not claimed fixed by this UI work. No production HA or large-fleet
capacity claim is made.

## Commands and evidence

Actual primary commands:

```powershell
.venv-ui/Scripts/python.exe -m ui --port 8080
$uiTestTemp = Join-Path (Get-Location) ('.local/pytest-phase5-final-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe -m pytest tests/test_ui_sources.py tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp $uiTestTemp
.venv-ui/Scripts/python.exe tests/verify_ui_final.py
.venv-ui/Scripts/python.exe tests/verify_ui_guards.py
.venv-ui/Scripts/python.exe tests/verify_ui_final.py --resume evidence/ui-final/20261026T220451Z
```

The real successful browser job executed the same CLI, with these concrete inputs:

```powershell
.venv/Scripts/python.exe deployctl.py deploy .local/ui/imports/9a1c65f1bfd64bc4845917450119cc24/project --config .deploy-always-on.json --namespace mcp-demo --kubeconfig .local/floci.kubeconfig --version phase5-v1 --timeout 180 --rollback-timeout 180 --push-registry localhost:4566/000000000000/us-east-1 --ensure-floci-repository
```

The UI used absolute folder/config paths; the command above uses the equivalent
project-relative configuration filename. The exact argv is in the saved job
record. Import and registration preceded the Deploy action. Build output records
the resource flags, repository creation, publishing, server dry-run and rollout.

- [Raw browser summary, jobs, MCP responses and build-limit observations](evidence/ui-final/20261026T220451Z/summary.json)
- [Corrected final dashboard screenshot](evidence/ui-final/20261026T220451Z/dashboard-final.png)
- [Third-server details screenshot](evidence/ui-final/20261026T220451Z/third-server-details.png)
- [Actual complete uploaded ZIP](evidence/ui-final/20261026T220451Z/text-tools-mcp.zip)
- [Successful CLI/build/deploy log](evidence/ui-final/20261026T220451Z/4ce1cba0091c42a8bc9d7092c1db99d6.log)
- [Expected failed build log](evidence/ui-final/20261026T220451Z/569481156e8a425cb309d8bfa8888685.log)
- [Rejected-import screenshot](evidence/ui-final/20261026T220451Z/rejected-import.png)
- [Before resources](evidence/ui-final/20261026T220451Z/before.json) and [after resources](evidence/ui-final/20261026T220451Z/after.json)
- [Live guards](evidence/ui-final/guards.json), [focused unit tests](evidence/ui-final/focused-tests.log), [final browser status](evidence/ui-final/final-browser.json), [final inventory](evidence/ui-final/final-status.json)

For the local builder's supported resource flags and deprecation status see
[Docker's legacy build reference](https://docs.docker.com/reference/cli/docker/image/build/).
Commit resolution follows [GitHub's public commits API](https://docs.github.com/en/rest/commits/commits).

The third server is running and visible. Implementation and verification stop
here; no further infrastructure or features were added.
