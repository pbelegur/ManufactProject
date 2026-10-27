# Manufact MCP UI repair

Verified 2026-10-27. The existing deployment engine and infrastructure were
preserved. One new uploaded application was built/deployed through the browser.

## Confirmed failures and fixes

1. **Import registration failed after a UI restart.** `Platform.__init__` reused
   dictionaries from `Sources.registered`, then added runtime `folder: Path`
   values to them. Registering the next project tried to JSON-serialize the
   entire registry and raised `TypeError: WindowsPath is not JSON serializable`.
   Runtime projects now use deep copies. Registry writes complete before the
   in-memory registry is updated, so a failed write cannot leave a falsely
   registered source. A regression recreates registration, restart and another
   registration. The reproduced live 500 is retained in
   `evidence/ui-repair/20261027T160013Z/registration-500.log`.
2. **GET `/imports/ID/configure` returned 405.** Both configuration URL forms
   now work, including links containing query parameters. Registered imports
   lead to their correct deployment form; unfinished imports remain resumable.
3. **The workflow could deploy the example instead of the uploaded source.**
   Unregistered uploads are visible as unfinished imports. Registration leads
   to a review of the exact server ID, project and release. Build is the default
   action; image reuse explicitly warns that an unchanged image may be a no-op.
   Form errors retain editable values. Submission disables repeated clicks.
4. **Names and history were hard to distinguish.** The UI is now Manufact MCP.
   Human display names, stable server IDs, runtime mode, release, deployment
   action and numbered attempts are separate. Display names are editable and
   persist across UI restarts without changing Kubernetes resources. Windows
   metadata reads explicitly use UTF-8. Times use the browser's locale where
   displayed in the dashboard and job list.
5. **The intentional invalid-build fixture looked like a broken user server.**
   Explicit metadata puts it under test probes and its jobs in test history.
   Genuine application failures are still shown as failures.
6. **MCP check setup could time out with little diagnostic detail.** The helper
   accepts only kubectl's explicit IPv4 forwarding-ready line, including the
   Service-to-pod port mapping used by Kourier (80 to 8080). Startup defaults to
   a bounded 45 seconds, configurable through the helper's `--forward-timeout`.
   Error output explains when no MCP request was sent. SDK imports are deferred;
   setup/module-loading timings are separate from tool execution. The parent
   stops its owned check/forwarder process tree on total timeout. No tool or
   deployment retries were added. The UI shows an actual success/error summary
   and retains the raw MCP response.

## Working names

| Display name | Stable server ID | Purpose |
|---|---|---|
| Echo & Math | `stateless-demo` | Stateless echo/add tools |
| Shared Counter | `stateful-demo` | Redis-backed counters |
| Text Analyzer | `manufact-text-tools` | Previously imported independent server |
| My Text Analyzer | `text-analyzer-upload` | New browser-imported and verified project |

The original examples also have Knative runtimes in `mcp-auto`; the dashboard
labels their mode separately from always-on runtimes in `mcp-demo`. A deployment
job is an attempt against a runtime, not a separate server. Renaming a display
label does not rewrite the application's internal MCP server-info name.

## Executed browser verification

The actual flow downloaded a complete sample ZIP, uploaded it, rejected an
existing server ID while preserving form values, registered a corrected unique
ID, reviewed the correct project, then pressed the real Deploy button.

- Job: `f1dd7b09f871452ab65aaa83b1a987ee`.
- Release: `browser-v1`; execution 16:19:33–16:20:45 UTC, about 72 seconds.
- Real stages: QUEUED, BUILDING, PUSHING, DEPLOYING, VERIFYING, SUCCEEDED.
- Pipeline: the existing `deployctl.py`, Dockerfile build, Floci ECR publishing,
  and Kubernetes Deployment/Service/PDB; **2/2 ready replicas**.
- Image: `localhost:4566/000000000000/us-east-1/text-analyzer-upload:browser-v1-ed1ad3fc00668c66cf8b993f8fe7d952cab06d87f7ec2d332efe0a01f2198033`.
- MCP endpoint: `http://text-analyzer-upload.mcp-demo.svc.cluster.local:8000/mcp`.
- Browser MCP call: `analyze_text({"text":"Manufact demo"})` returned
  `{"words":2,"characters":13,"lines":1,"slug":"manufact-demo"}`.
- Real application pod logs loaded; display name changed to **My Text Analyzer**.
- Four read-only spot checks against the original examples succeeded while
  the new application built/rolled out. This was not a continuous load test.

Final browser checks called real tools successfully on all six runtimes:

| Runtime | Actual tool | Result |
|---|---|---|
| `mcp-demo/text-analyzer-upload` | analyze_text | PASS |
| `mcp-demo/stateless-demo` | echo | PASS |
| `mcp-demo/stateful-demo` | get_counter | PASS, saved counter still 13 |
| `mcp-demo/manufact-text-tools` | analyze_text | PASS |
| `mcp-auto/stateless-demo` | echo through Kourier | PASS |
| `mcp-auto/stateful-demo` | get_counter through Kourier | PASS, counter 0 in its separate Redis |

Managed Redis is scoped by server/namespace. Counter 0 in the autoscaling
namespace is not a reset of the always-on counter 13. No test health action
executed increment_counter. Original always-on Deployment specs/UIDs were
unchanged, including Redis and the compatibility probe. The new display name
and source registry survived a final UI restart. No cluster recreation,
gateway installation, original Kubernetes application rollout or Full demo ran.

Both example-template imports were exercised. The approved public GitHub
quickstart source was genuinely downloaded and pinned to commit
`38e14ca4ce362d27dfaaa498f3b4592bea4c6710`; subsequent checks reused that exact
import. It has no compatible Dockerfile, and the configuration page now explains
that limitation. This does **not** claim that the tutorial repository deployed.
A custom `Dockerfile.dockerfile` filename is covered by a form regression;
the real deployment above used the sample's standard Dockerfile.

Evidence:

- [Final browser results](evidence/ui-repair/20261027T161829Z/finish-summary.json)
- [Final dashboard](evidence/ui-repair/20261027T161829Z/dashboard-final.png)
- [Real deployment log](evidence/ui-repair/20261027T161829Z/deployment.log)
- [Restart smoke checks](evidence/ui-repair/20261027T161829Z/restart-smoke.json)
- [Focused tests](evidence/ui-repair/focused-tests.log)
- [Final live MCP tests](evidence/ui-repair/mcp-tests-final.log)

## Tests and retained failures

- **74 focused tests passed**: source/manifest/rollback/queue tests plus route
  recovery, post-restart import persistence, names/escaping/UTF-8, correct project
  selection, custom Dockerfile detection, forwarding readiness and owned-process
  timeout cleanup.
- **6 existing live MCP tests passed**, with the Compose restart test deselected.
- **14 live localhost/CSRF/import-validation guard checks passed**; no jobs submitted.
- Final browser checks had no JavaScript errors.

Earlier verification attempts are retained, not reported as passes. They exposed
the real registration 500, a transient Kubernetes preflight error and a
20-second forwarding-start timeout. A collector initially read a collapsed
panel incorrectly and later had an async-generator assertion error; these were
fixed. An overly strict new forwarding parser briefly rejected Kourier's resolved
8080 target; it was corrected and covered by a regression. The final read-only
collector resumed six already successful MCP calls rather than repeating builds
or rewriting the earlier failures. `summary.json` and
`finish-collector-failure.json` preserve those earlier results.

The separate Compose stateless container had repeated Docker health timeouts.
Only that unhealthy container was restarted, without a rebuild; it recovered.
Redis and Kubernetes were not restarted. One earlier six-test run failed its
health probe; another passed five tests but timed out an initialize request in
the 20-client burst. The final six-test run, after build/browser traffic ended,
passed in 12.02 seconds with fresh isolated counter keys. This does not prove
that every burst succeeds on this shared host; no failed increments were retried.

## Use it

Open **http://127.0.0.1:8080** and refresh. The UI is running with two job workers.
If it is stopped, run one instance from the repository root:

```powershell
$env:MCP_UI_MAX_JOBS = '2'
.\.venv-ui\Scripts\python.exe -m ui --port 8080
```

Use **Add a server → Name and configure → Save and continue to deployment →
Deploy**. For the evaluated upload, open
**http://127.0.0.1:8080/deployments/mcp-demo/text-analyzer-upload**.

Focused regression command:

```powershell
$uiFixTemp = Join-Path (Get-Location) ('.local/pytest-ui-fix-' + [guid]::NewGuid().ToString('N'))
.\.venv-ui\Scripts\python.exe -m pytest tests/test_ui_workflow.py tests/test_ui_mcp_check.py tests/test_ui_jobs.py tests/test_ui_sources.py tests/test_deployctl.py tests/test_manifest.py -q -p no:cacheprovider --basetemp $uiFixTemp
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_integration.py --run-integration -m 'not compose_restart' -q -p no:cacheprovider
```

The executed browser flow used `tests/verify_ui_repair.py` with the existing
GitHub/source imports; `--finish` and `--resume-checks` are read-only collection
recovery modes, not deployment retries. Do not repeat its fixed-name onboarding
against the already registered upload.

## Limits

This remains a trusted localhost demo with a bounded, one-process in-memory job
queue and single-host infrastructure. Staging retains unfinished imports and
the existing 20-import limit; original user uploads were not deleted. GitHub
trust approval does not make arbitrary code compatible or safe. Production
authentication, durable workers and isolated builds remain future work.
Transient local startup/load failures and the historical rollout 502 have not
been proven eliminated. No production HA or guaranteed zero downtime is claimed.
