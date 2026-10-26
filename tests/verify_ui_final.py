"""Opt-in browser onboarding verification on existing infrastructure; no Full demo."""
import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import zipfile

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence/ui-final" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
BASE = "http://127.0.0.1:8080"
PREFIX = ["kubectl", "--kubeconfig", str(ROOT / ".local/floci.kubeconfig"), "--request-timeout=20s", "-n", "mcp-demo"]


def snapshot():
    result = subprocess.run(PREFIX + ["get", "deployments,pods,services,endpointslices,pvc", "-o", "json"],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=40)
    return json.loads(result.stdout)


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in entries:
            output.writestr(name, data)
    return buffer.getvalue()


async def main():
    OUTPUT.mkdir(parents=True)
    results = {"started_at": datetime.now(timezone.utc).isoformat(), "result": "RUNNING", "jobs": [],
               "checks": [], "mcp_checks": [], "availability": [], "browser_errors": []}
    initial = snapshot()
    (OUTPUT / "before.json").write_text(json.dumps(initial, indent=2))
    monitor = None
    running = True
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            results["browser_version"] = browser.version
            context = await browser.new_context(viewport={"width": 1440, "height": 1050})
            page = await context.new_page()
            page.on("pageerror", lambda error: results["browser_errors"].append(str(error)))
            await page.goto(BASE + "/imports")
            csrf = await page.locator('input[name="csrf"]').input_value()
            headers = {"X-CSRF-Token": csrf}

            async def mcp(name):
                response = await context.request.post(BASE + "/api/deployments/mcp-demo/" + name + "/test", headers=headers, timeout=150000)
                data = await response.json()
                results["mcp_checks"].append(data)
                assert response.ok and data["success"], data
                if name == "stateful-demo":
                    result = data["response"].get("structured_content") or data["response"].get("structuredContent")
                    assert result == {"result": 13}, result
                return data

            await mcp("stateless-demo")
            await mcp("stateful-demo")

            async def availability():
                while running:
                    response = await context.request.get(BASE + "/api/deployments?refresh=1")
                    data = await response.json()
                    assert response.ok and not data["errors"], data
                    selected = [r for r in data["items"] if r["namespace"] == "mcp-demo" and r["name"] in {"stateless-demo", "stateful-demo", "contract-probe"}]
                    assert len(selected) == 3 and all(r["health"] == "Healthy" and r["healthy_replicas"] == r["desired_replicas"] == 2 for r in selected)
                    results["availability"].append({"observed_at": data["observed_at"], "ready": {r["name"]: r["healthy_replicas"] for r in selected}})
                    await asyncio.sleep(5)
            monitor = asyncio.create_task(availability())

            async def upload(data, filename):
                await page.goto(BASE + "/imports")
                await page.set_input_files("#archive", {"name": filename, "mimeType": "application/zip", "buffer": data})
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Import and review configuration").click()
                await page.wait_for_load_state("domcontentloaded")

            await upload(zip_bytes([("../escape.py", "unsafe")]), "unsafe.zip")
            assert "Import failed" in await page.locator("body").inner_text()
            results["checks"].append({"name": "Traversal import rejected in browser", "result": "PASS"})
            await page.screenshot(path=str(OUTPUT / "rejected-import.png"), full_page=True)

            # Live trusted GitHub staging verifies real commit resolution/download;
            # does not register/deploy that unrelated tutorial repository.
            await page.goto(BASE + "/imports")
            await page.select_option("#kind", "github")
            await page.locator('input[name="trusted_code"]').check()
            await page.get_by_role("button", name="Import and review configuration").click(timeout=120000)
            await page.wait_for_load_state("domcontentloaded")
            if "/imports/" in page.url:
                provenance = json.loads(await page.locator("#source-provenance").inner_text())
                assert re.fullmatch(r"[0-9a-f]{40}", provenance["commit"])
                results["github_import"] = {"result": "PASS", **provenance}
            else:
                results["github_import"] = {"result": "FAIL", "error": await page.locator("body").inner_text()}

            async def configure(name, safe=False):
                assert await page.locator("#mode").input_value() == "", "Missing config silently defaulted mode"
                await page.fill("#server_name", name)
                await page.select_option("#mode", "stateless")
                await page.fill("#version", "phase5-v1")
                if safe:
                    await page.fill("#safe_tool", "analyze_text")
                    await page.fill("#safe_arguments", '{"text":"Manufact builds MCP tools"}')
                    await page.locator('input[name="confirm_read_only"]').check()
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Register server").click()
                await page.wait_for_url("**/deploy?project=*", timeout=30000)
                return await page.locator("#project_id").input_value()

            async def deploy(expected):
                await page.get_by_role("button", name="Deploy", exact=True).click()
                await page.wait_for_url("**/jobs/*", timeout=15000)
                identifier = page.url.rsplit("/", 1)[1]
                deadline = time.monotonic() + 450
                captured = set()
                checked_mid_build = False
                while time.monotonic() < deadline:
                    response = await context.request.get(BASE + "/api/jobs/" + identifier)
                    job = await response.json()
                    if expected == "succeeded" and not checked_mid_build and job.get("phase") == "Building Docker image":
                        await mcp("stateless-demo")
                        await mcp("stateful-demo")
                        checked_mid_build = True
                    for container in re.findall(r"Running in ([0-9a-f]{12,64})", job["log"]):
                        if container in captured:
                            continue
                        inspected = await asyncio.to_thread(subprocess.run, ["docker", "inspect", container],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
                        if inspected.returncode == 0:
                            host = json.loads(inspected.stdout)[0]["HostConfig"]
                            limits = {k: host.get(k) for k in ["Memory", "MemorySwap", "CpuPeriod", "CpuQuota", "Privileged", "Binds"]}
                            results.setdefault("observed_build_limits", []).append(limits)
                            assert limits["Memory"] == 1073741824 and limits["CpuQuota"] == limits["CpuPeriod"] == 100000
                            assert not limits["Privileged"] and not limits["Binds"]
                            captured.add(container)
                    if job["status"] not in {"queued", "running"}:
                        results["jobs"].append({k: v for k, v in job.items() if k != "log"})
                        (OUTPUT / (identifier + ".log")).write_text(job["log"], encoding="utf-8")
                        assert job["status"] == expected, job["log"][-7000:]
                        assert Path(job["command"][1]).resolve() == ROOT / "deployctl.py"
                        assert "--ensure-floci-repository" in job["command"]
                        await page.wait_for_function("document.body.dataset.jobStatus === '" + expected + "'", timeout=15000)
                        return job
                    await asyncio.sleep(1)
                raise TimeoutError("Imported deployment did not finish")

            await upload(zip_bytes([("Dockerfile", "NOT_A_DOCKERFILE\n"), ("server.py", "print('never executed')\n")]), "broken-project.zip")
            await configure("manufact-invalid-build")
            failed_job = await deploy("failed")
            assert failed_job["exit_code"] != 0
            assert "deployctl.py exited" in await page.locator("#job-error").inner_text()
            results["checks"].append({"name": "Build failure visible without touching running deployments", "result": "PASS"})
            await page.screenshot(path=str(OUTPUT / "failed-build.png"), full_page=True)

            sample_response = await context.request.get(BASE + "/sample-project.zip")
            assert sample_response.ok
            sample = await sample_response.body()
            (OUTPUT / "text-tools-mcp.zip").write_bytes(sample)
            await upload(sample, "text-tools-mcp.zip")
            pid = await configure("manufact-text-tools", safe=True)
            results["project_id"] = pid
            success_job = await deploy("succeeded")
            results["successful_job_id"] = success_job["id"]
            await page.get_by_role("link", name="Inspect Kubernetes deployment").click()
            await page.wait_for_load_state("domcontentloaded")
            assert await page.locator("#replicas").inner_text() == "2 / 2"
            assert "/mcp" in await page.locator("#endpoint").inner_text()
            assert "imports/" in await page.locator("#source-project").inner_text()
            await page.get_by_role("button", name="Call reviewed read-only tool").click()
            await page.wait_for_function("document.getElementById('mcp-result').textContent.includes('success')", timeout=150000)
            check = json.loads(await page.locator("#mcp-result").inner_text())
            assert check["success"], check
            structured = check["response"].get("structured_content") or check["response"].get("structuredContent")
            if not structured:
                structured = json.loads(check["response"]["content"][0]["text"])
            assert structured == {"words": 4, "characters": 25, "lines": 1, "slug": "manufact-builds-mcp-tools"}, structured
            results["third_mcp"] = check
            await page.get_by_role("button", name="Load current pod logs").click()
            await page.wait_for_function("!document.getElementById('app-logs').textContent.includes('Reading Kubernetes')", timeout=30000)
            app_log = await page.locator("#app-logs").inner_text()
            assert "manufact-text-tools-" in app_log and "HTTP" in app_log, app_log
            assert success_job["id"][:12] in await page.locator("body").inner_text()
            assert failed_job["id"][:12] not in await page.locator("body").inner_text()
            await page.screenshot(path=str(OUTPUT / "third-server-details.png"), full_page=True)
            await mcp("stateless-demo")
            await mcp("stateful-demo")
            await page.goto(BASE + "/?refresh=1")
            developer = page.locator('section[data-category="developer"]')
            assert await developer.locator('tr[data-identity="mcp-demo/manufact-text-tools"]').count() == 1
            assert await developer.locator('tr[data-identity="mcp-demo/contract-probe"]').count() == 0
            response = await context.request.get(BASE + "/api/deployments?refresh=1")
            inventory = await response.json()
            (OUTPUT / "inventory.json").write_text(json.dumps(inventory, indent=2))
            third = next(r for r in inventory["items"] if r["identity"] == "mcp-demo/manufact-text-tools")
            assert third["project_id"] == pid and third["health"] == "Healthy" and third["image_digests"]
            assert third["latest_job"]["id"] == success_job["id"] and third["last_deployed_at"] == success_job["finished_at"]
            assert third["source_identity"]["kind"] == "zip" and third["category"] == "developer"
            failed = next(r for r in inventory["items"] if r["identity"] == "mcp-demo/manufact-invalid-build")
            assert failed["health"] == "Failed" and failed["exists"] is False and failed["image"] is None
            assert not results["browser_errors"]
            await page.screenshot(path=str(OUTPUT / "dashboard.png"), full_page=True)
            await browser.close()
        final = snapshot()
        (OUTPUT / "after.json").write_text(json.dumps(final, indent=2))
        for name in ["stateless-demo", "stateful-demo", "stateful-demo-redis", "contract-probe"]:
            old = next(r for r in initial["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            new = next(r for r in final["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            assert old["metadata"]["uid"] == new["metadata"]["uid"] and old["spec"] == new["spec"]
            original_pods = {r["metadata"]["uid"] for r in initial["items"] if r["kind"] == "Pod" and r["metadata"].get("labels", {}).get("app") == name}
            final_pods = {r["metadata"]["uid"] for r in final["items"] if r["kind"] == "Pod" and r["metadata"].get("labels", {}).get("app") == name}
            assert original_pods == final_pods
        assert results.get("observed_build_limits"), "No running build container was observed; limits not verified"
        results["checks"].append({"name": "Original Deployment specs and pod UIDs preserved; Redis counter still 13", "result": "PASS"})
        results["result"] = "PASS"
    except Exception:
        results.update(result="FAIL", error=traceback.format_exc())
        raise
    finally:
        running = False
        if monitor:
            try:
                await monitor
            except Exception:
                results.update(result="FAIL", monitor_error=traceback.format_exc())
        results["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUTPUT / "summary.json").write_text(json.dumps(results, indent=2))
        print(json.dumps({"result": results["result"], "output": str(OUTPUT), "jobs": len(results["jobs"])}), flush=True)


async def resume(folder):
    """Read-only verification recovery; never imports, builds or deploys again."""
    output = Path(folder)
    results = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    results["initial_collection_error"] = results.pop("error", None)
    results["result"] = "RUNNING"
    success = next(j for j in results["jobs"] if j["name"] == "manufact-text-tools" and j["status"] == "succeeded")
    failed = next(j for j in results["jobs"] if j["name"] == "manufact-invalid-build")
    for job in results["jobs"]:
        log = (ROOT / ".local/ui/jobs" / (job["id"] + ".log")).read_text(encoding="utf-8")
        (output / (job["id"] + ".log")).write_text(log, encoding="utf-8")
    initial = json.loads((output / "before.json").read_text(encoding="utf-8"))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1050})
            page = await context.new_page()
            page.on("pageerror", lambda error: results["browser_errors"].append(str(error)))
            await page.goto(BASE + "/deployments/mcp-demo/manufact-text-tools?refresh=1")
            token = await page.locator('input[name="csrf"]').input_value()
            assert await page.locator("#replicas").inner_text() == "2 / 2"
            assert "/mcp" in await page.locator("#endpoint").inner_text()
            assert "imports/" in await page.locator("#source-project").inner_text()
            await page.get_by_role("button", name="Call reviewed read-only tool").click()
            await page.wait_for_function("document.getElementById('mcp-result').textContent.includes('success')", timeout=150000)
            check = json.loads(await page.locator("#mcp-result").inner_text())
            assert check["success"], check
            structured = check["response"].get("structured_content") or check["response"].get("structuredContent")
            if not structured:
                structured = json.loads(check["response"]["content"][0]["text"])
            assert structured == {"words": 4, "characters": 25, "lines": 1, "slug": "manufact-builds-mcp-tools"}, structured
            results["third_mcp"] = check
            await page.get_by_role("button", name="Load current pod logs").click()
            await page.wait_for_function("!document.getElementById('app-logs').textContent.includes('Reading Kubernetes')", timeout=30000)
            app_log = await page.locator("#app-logs").inner_text()
            assert "manufact-text-tools-" in app_log and "HTTP" in app_log
            assert success["id"][:12] in await page.locator("body").inner_text()
            assert failed["id"][:12] not in await page.locator("body").inner_text()
            await page.screenshot(path=str(output / "third-server-details.png"), full_page=True)
            for name in ["stateless-demo", "stateful-demo"]:
                response = await context.request.post(BASE + "/api/deployments/mcp-demo/" + name + "/test",
                                                      headers={"X-CSRF-Token": token}, timeout=150000)
                check = await response.json()
                assert response.ok and check["success"], check
                if name == "stateful-demo":
                    assert check["response"]["structured_content"] == {"result": 13}
                results["mcp_checks"].append(check)
            await page.goto(BASE + "/?refresh=1")
            developer = page.locator('section[data-category="developer"]')
            assert await developer.locator('tr[data-identity="mcp-demo/manufact-text-tools"]').count() == 1
            assert await developer.locator('tr[data-identity="mcp-demo/contract-probe"]').count() == 0
            response = await context.request.get(BASE + "/api/deployments?refresh=1")
            inventory = await response.json()
            (output / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            third = next(r for r in inventory["items"] if r["identity"] == "mcp-demo/manufact-text-tools")
            assert third["project_id"] == results["project_id"] and third["health"] == "Healthy" and third["image_digests"]
            assert third["latest_job"]["id"] == success["id"] and third["last_deployed_at"] == success["finished_at"]
            assert third["source_identity"]["kind"] == "zip" and third["category"] == "developer"
            failed_row = next(r for r in inventory["items"] if r["identity"] == "mcp-demo/manufact-invalid-build")
            assert failed_row["health"] == "Failed" and not failed_row["exists"] and failed_row["image"] is None
            assert not results["browser_errors"]
            await page.screenshot(path=str(output / "dashboard.png"), full_page=True)
            await browser.close()
        final = snapshot()
        (output / "after.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
        preserved = []
        for name in ["stateless-demo", "stateful-demo", "stateful-demo-redis", "contract-probe"]:
            old = next(r for r in initial["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            new = next(r for r in final["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            assert old["metadata"]["uid"] == new["metadata"]["uid"] and old["spec"] == new["spec"]
            ids = lambda data: {r["metadata"]["uid"] for r in data["items"] if r["kind"] == "Pod" and r["metadata"].get("labels", {}).get("app") == name}
            assert ids(initial) == ids(final)
            preserved.append(name)
        results["preserved_deployments"] = preserved
        assert results.get("observed_build_limits")
        results.update(result="PASS", successful_job_id=success["id"], resume_verified_at=datetime.now(timezone.utc).isoformat())
    except Exception:
        results.update(result="FAIL", error=traceback.format_exc())
        raise
    finally:
        (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps({"result": results["result"], "output": str(output), "resume_only": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(resume(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "--resume" else main())
