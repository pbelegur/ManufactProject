"""Opt-in real-browser deployment verification; no mocks and no Full demo.

Run from the repository root with the UI running. Uses installed Microsoft Edge,
the actual HTTP forms/CLI/Kubernetes, and the existing in-cluster MCP client.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def verify(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    prefix = ["kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=20s", "-n", "mcp-demo"]
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "result": "RUNNING", "jobs": [],
              "availability_samples": [], "validation": [], "browser_errors": []}
    suffix = uuid4().hex[:10]
    configmap = "ui-verify-" + suffix
    counter = "ui_persist_" + suffix
    jobs = []

    def kube(arguments, stdin=None):
        process = subprocess.run(prefix + arguments, input=stdin, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=120, check=True)
        return process.stdout

    def snapshot():
        return json.loads(kube(["get", "deployments,pods,endpointslices", "-o", "json"]))

    def mcp_stage(stage):
        code = (ROOT / "tests/kubernetes_client.py").read_text(encoding="utf-8")
        code += '''
if os.getenv("UI_SEED_COUNTER"):
    async def seed():
        value = await call("http://stateful-demo:8000/mcp", "increment_counter", {"name": os.environ["UI_SEED_COUNTER"], "amount": 13})
        assert value == 13, value
        print(json.dumps({"seeded_counter": os.environ["UI_SEED_COUNTER"], "value": value}), flush=True)
    asyncio.run(seed())
'''
        kube(["apply", "-f", "-"], json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
              "metadata": {"name": configmap}, "data": {"client.py": code}}))
        workloads = json.loads(kube(["get", "deployment/stateful-demo", "-o", "json"]))
        image = workloads["spec"]["template"]["spec"]["containers"][0]["image"]
        name = configmap + "-" + stage
        jobs.append(name)
        env = [{"name": "VERIFY_GENERIC", "value": "1"},
               {"name": "UI_SEED_COUNTER" if stage == "before" else "PERSISTED_COUNTER", "value": counter}]
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name}, "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": 120, "ttlSecondsAfterFinished": 3600,
            "template": {"spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                "containers": [{"name": "client", "image": image, "command": ["python", "/test/client.py"], "env": env,
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "300m", "memory": "256Mi"}},
                    "volumeMounts": [{"name": "script", "mountPath": "/test", "readOnly": True}]}],
                "volumes": [{"name": "script", "configMap": {"name": configmap}}]}}}}
        kube(["apply", "-f", "-"], json.dumps(job))
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            status = json.loads(kube(["get", "job/" + name, "-o", "json"])).get("status", {})
            if status.get("succeeded") == 1:
                log = kube(["logs", "job/" + name])
                (output / ("mcp-" + stage + ".log")).write_text(log, encoding="utf-8")
                return {"result": "PASS", "job": name, "log": log, "counter": counter}
            if status.get("failed"):
                raise AssertionError(kube(["logs", "job/" + name]))
            time.sleep(2)
        raise TimeoutError("MCP verification Job timed out")

    async def save_job(context, page, expected="succeeded"):
        await page.wait_for_url("**/jobs/*", timeout=15000)
        job_id = page.url.rsplit("/", 1)[1]
        started = time.monotonic()
        while time.monotonic() - started < 480:
            response = await context.request.get(args.url + "/api/jobs/" + job_id)
            assert response.ok, await response.text()
            job = await response.json()
            if job["status"] not in ("queued", "running"):
                result["jobs"].append({k: v for k, v in job.items() if k != "log"})
                (output / (job_id + ".log")).write_text(job["log"], encoding="utf-8")
                (output / (job_id + ".json")).write_text(json.dumps({k: v for k, v in job.items() if k != "log"}, indent=2), encoding="utf-8")
                assert job["status"] == expected, job["error"] or job["log"][-5000:]
                await page.wait_for_function("document.body.dataset.jobStatus === '" + expected + "'", timeout=10000)
                assert await page.locator("#job-status").inner_text() == expected.upper()
                await page.screenshot(path=str(output / (job_id + ".png")), full_page=True)
                print(job_id, job["name"], job["backend"], job["status"], "exit", job["exit_code"], flush=True)
                return job
            await asyncio.sleep(2)
        raise TimeoutError("UI job did not finish")

    async def submit(page, project, backend="always-on", source="build", image="", timeout=180):
        await page.goto(args.url + "/deploy")
        await page.select_option("#project_id", project)
        await page.select_option("#backend", backend)
        await page.select_option("#source", source)
        await page.fill("#version", "ui-" + suffix + "-" + project + "-" + backend)
        if image or timeout != 180:
            await page.locator("summary").click()
            await page.fill("#image", image)
            await page.fill("#timeout_seconds", str(timeout))
        started = time.monotonic()
        await page.get_by_role("button", name="Deploy", exact=True).click()
        await page.wait_for_url("**/jobs/*", timeout=15000)
        elapsed = time.monotonic() - started
        result.setdefault("form_submission_seconds", []).append(elapsed)
        assert elapsed < 10, "Form blocked waiting for deployment instead of returning a job"

    monitor_running = True
    monitor_task = None
    try:
        initial = await asyncio.to_thread(snapshot)
        (output / "initial-kubernetes.json").write_text(json.dumps(initial, indent=2), encoding="utf-8")
        print("Verifying actual MCP tools before UI deployments", flush=True)
        result["mcp_before"] = await asyncio.to_thread(mcp_stage, "before")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            result["browser"] = {"channel": "msedge", "version": browser.version}
            context = await browser.new_context(viewport={"width": 1360, "height": 980})
            page = await context.new_page()
            page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
            await page.goto(args.url + "/")
            assert await page.title() == "MCP platform"
            await page.goto(args.url + "/deploy")
            token = await page.locator('input[name="csrf"]').input_value()
            headers = {"X-CSRF-Token": token}
            for label, body, custom, expected in (
                ("CSRF required", {"project_id": "stateless"}, {}, 403),
                ("path traversal rejected", {"project_id": "../../servers/stateless"}, headers, 422),
                ("arbitrary executable path rejected", {"project_id": "stateless", "folder": "C:/Windows"}, headers, 422),
                ("shell input rejected", {"project_id": "stateless", "version": "v1; whoami"}, headers, 422),
                ("foreign image repository rejected", {"project_id": "stateless", "image": "example.com/other:latest"}, headers, 422),
                ("cross-origin POST rejected", {"project_id": "stateless"}, {**headers, "Origin": "https://example.com"}, 403),
            ):
                response = await context.request.post(args.url + "/api/jobs", data=body, headers=custom)
                assert response.status == expected, (label, response.status, await response.text())
                result["validation"].append({"check": label, "status": response.status, "result": "PASS"})

            async def availability():
                while monitor_running:
                    response = await context.request.get(args.url + "/api/deployments?refresh=1")
                    data = await response.json()
                    assert not data["errors"], data["errors"]
                    selected = [r for r in data["items"] if r["namespace"] == "mcp-demo" and r["name"] in ("stateless-demo", "stateful-demo", "contract-probe")]
                    assert len(selected) == 3
                    result["availability_samples"].append({"observed_at": data["observed_at"], "ready": {r["name"]: r["healthy_replicas"] for r in selected}})
                    assert all(r["healthy_replicas"] >= 2 for r in selected), selected
                    await asyncio.sleep(5)

            monitor_task = asyncio.create_task(availability())
            for project in ("stateless", "stateful"):
                await submit(page, project)
                job = await save_job(context, page)
                assert Path(job["command"][1]).resolve() == ROOT / "deployctl.py"
                assert "--push-registry" in job["command"] and job["exit_code"] == 0
                assert "docker build" in (output / (job["id"] + ".log")).read_text()
                await page.get_by_role("link", name="Inspect Kubernetes deployment").click()
                assert await page.locator("#replicas").inner_text() == "2 / 2"
                assert "/mcp" in await page.locator("#endpoint").inner_text()
                assert job["version"] in await page.locator("#image-reference").inner_text()
                await page.get_by_role("button", name="Load current pod logs").click()
                await page.wait_for_function("!document.getElementById('app-logs').textContent.includes('Reading Kubernetes')")
                assert "Starting" in await page.locator("#app-logs").inner_text() or "HTTP" in await page.locator("#app-logs").inner_text()

            # Verify the real redeploy action; always-on image reuse is idempotent.
            await page.goto(args.url + "/deployments/mcp-demo/stateless-demo")
            await page.get_by_role("button", name="Redeploy published image").click()
            redeploy = await save_job(context, page)
            assert "--image" in redeploy["command"]

            # One small Knative application deployment, not autoscaling/load/Full.
            await submit(page, "stateless", backend="knative", source="reuse")
            knative = await save_job(context, page)
            assert "--image" in knative["command"] and "mcp.knative.local.json" in " ".join(knative["command"])
            await page.get_by_role("link", name="Inspect Kubernetes deployment").click()
            assert "stateless-demo.mcp-auto.mcp.test/mcp" in await page.locator("#endpoint").inner_text()

            missing = "localhost:4566/000000000000/us-east-1/stateless-demo:ui-missing-" + suffix
            await submit(page, "stateless", source="reuse", image=missing, timeout=35)
            failed = await save_job(context, page, "failed")
            log = (output / (failed["id"] + ".log")).read_text()
            assert failed["exit_code"] == 1 and "ROLLBACK OK: previous healthy deployment restored" in log
            assert await page.locator("#job-error").is_visible()
            assert "exited 1" in await page.locator("#job-error").inner_text()
            await page.goto(args.url + "/?refresh=1")
            await page.screenshot(path=str(output / "dashboard-final.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
            assert not result["browser_errors"], result["browser_errors"]
            monitor_running = False
            await monitor_task
            monitor_task = None
            await browser.close()

        print("Verifying real MCP tools and persisted Redis counter after UI deployments", flush=True)
        result["mcp_after"] = await asyncio.to_thread(mcp_stage, "after")
        final = await asyncio.to_thread(snapshot)
        (output / "final-kubernetes.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
        initial_fixture = {x["metadata"]["uid"] for x in initial["items"] if x["kind"] == "Pod" and x["metadata"].get("labels", {}).get("app") in ("contract-probe", "stateful-demo-redis")}
        final_fixture = {x["metadata"]["uid"] for x in final["items"] if x["kind"] == "Pod" and x["metadata"].get("labels", {}).get("app") in ("contract-probe", "stateful-demo-redis")}
        assert initial_fixture == final_fixture, "Independent project or Redis pods were replaced"
        result.update(result="PASS", independent_and_redis_pod_uids_preserved=True)
    except Exception as exc:
        import traceback
        result.update(result="FAIL", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        print(result["traceback"], flush=True)
    finally:
        monitor_running = False
        if monitor_task:
            await asyncio.gather(monitor_task, return_exceptions=True)
        for name in jobs:
            await asyncio.to_thread(kube, ["delete", "job/" + name, "--ignore-not-found", "--wait=false"])
        await asyncio.to_thread(kube, ["delete", "configmap/" + configmap, "--ignore-not-found", "--wait=false"])
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("availability_samples", "mcp_before", "mcp_after", "jobs")}, indent=2), flush=True)
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--kubeconfig", default=".local/floci.kubeconfig")
    parser.add_argument("--output", default="evidence/ui")
    raise SystemExit(asyncio.run(verify(parser.parse_args())))
