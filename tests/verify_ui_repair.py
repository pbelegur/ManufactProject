"""Real browser import/deploy/test verification on existing infrastructure.

One independent ZIP deployment; no existing application rollout, Full demo,
cluster recreation or failed MCP-call retry. Retains failures as evidence.
"""
import asyncio
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import time
import traceback
import zipfile

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"
SERVER = "text-analyzer-upload"
OUTPUT = ROOT / "evidence/ui-repair" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot():
    result = subprocess.run(["kubectl", "--kubeconfig", str(ROOT / ".local/floci.kubeconfig"), "-n", "mcp-demo",
                             "--request-timeout=15s", "get", "deployments,pods,services,pvc", "-o", "json"], capture_output=True,
                            text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise RuntimeError("Kubernetes snapshot failed: " + result.stderr + result.stdout)
    return json.loads(result.stdout)


async def main(args):
    OUTPUT.mkdir(parents=True)
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "result": "RUNNING", "checks": [],
              "mcp": [], "browser_errors": [], "availability": []}
    previous = json.loads((Path(args.resume_evidence) / "summary.json").read_text(encoding="utf-8")) if args.resume_evidence else None
    if previous:
        result["previous_evidence"] = args.resume_evidence
    try:
        before = await asyncio.to_thread(snapshot)
    except Exception:
        result.update(result="FAIL", error=traceback.format_exc(), phase="Kubernetes preflight; no jobs submitted")
        (OUTPUT / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        raise
    (OUTPUT / "before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1050})
            page = await context.new_page()
            page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
            await page.goto(BASE + "/imports", timeout=60000)
            token = await page.locator('input[name="csrf"]').input_value()

            async def mcp(name, namespace="mcp-demo"):
                response = await context.request.post(f"{BASE}/api/deployments/{namespace}/{name}/test",
                    headers={"X-CSRF-Token": token}, timeout=220000)
                check = await response.json()
                result["mcp"].append(check)
                assert response.ok and check.get("success"), check
                if name == "stateful-demo" and namespace == "mcp-demo":
                    assert check["response"]["structured_content"] == {"result": 13}, check
                print("MCP PASS", namespace + "/" + name, check.get("tool"), flush=True)
                return check

            for name in ["stateless-demo", "stateful-demo", "manufact-text-tools"]:
                await mcp(name)
            result["checks"].append("Original three services: real read-only MCP tools succeed")

            # Existing pending import URLs, including the exact former 405 path.
            link = page.locator('a[href^="/imports/"][href$="/configure"]').first
            if await link.count():
                target = await link.get_attribute("href")
                response = await page.goto(BASE + target + "?utm_source=chatgpt.com")
                assert response.ok and await page.locator("#configure-form").count() == 1
                result["checks"].append("Pending import resumes through /configure GET")

            # Both template imports retain explicit state/configuration defaults.
            for template in ([] if previous else ["stateless", "stateful"]):
                await page.goto(BASE + "/imports")
                await page.select_option("#kind", "template")
                await page.select_option("#template", template)
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Import and review configuration").click()
                await page.wait_for_url("**/imports/*")
                assert await page.locator("#mode").input_value() == template
                assert await page.locator("#safe_tool").input_value() == ("echo" if template == "stateless" else "get_counter")
                result["checks"].append(template + " template imports with correct defaults")

            # Real approved GitHub resolution/download, not an assumed deployable repo.
            if args.github_import:
                await page.goto(BASE + "/imports/" + args.github_import + "/configure")
            else:
                await page.goto(BASE + "/imports")
                await page.select_option("#kind", "github")
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Import and review configuration").click(timeout=130000)
                await page.wait_for_load_state("domcontentloaded")
            if await page.locator("#configure-form").count():
                await page.get_by_text("Source verification", exact=True).click()
                source = json.loads(await page.locator("#source-provenance").inner_text())
                assert len(source["commit"]) == 40
                assert "No Dockerfile was found" in await page.locator("body").inner_text()
                result["github"] = {"result": "PASS", **source}
            else:
                result["github"] = {"result": "FAIL", "error": await page.locator("body").inner_text()}

            if previous:
                await page.goto(previous["import_url"] + "/configure")
            else:
                await page.goto(BASE + "/imports")
                await page.set_input_files("#archive", {"name": "invalid.zip", "mimeType": "application/zip", "buffer": b"invalid"})
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Import and review configuration").click()
                await page.wait_for_load_state("domcontentloaded")
                assert "Import failed" in await page.locator("body").inner_text() and await page.locator("#import-form").count()
                result["checks"].append("Bad ZIP shows error on recoverable import form")

                sample = await context.request.get(BASE + "/sample-project.zip")
                data = await sample.body()
                (OUTPUT / "text-analyzer-project.zip").write_bytes(data)
                await page.set_input_files("#archive", {"name": "text-analyzer-project.zip", "mimeType": "application/zip", "buffer": data})
                await page.locator('input[name="trusted_code"]').check()
                await page.get_by_role("button", name="Import and review configuration").click()
                await page.wait_for_url("**/imports/*")
            result["import_url"] = page.url
            assert await page.locator("#mode").input_value() == ""
            assert "text-analyzer-project.zip" in await page.locator("#import-label").inner_text()
            await page.fill("#display_name", "Imported Text Analyzer")
            await page.fill("#server_name", "stateless-demo")
            await page.select_option("#mode", "stateless")
            await page.fill("#version", "browser-v1")
            await page.fill("#safe_tool", "analyze_text")
            await page.fill("#safe_arguments", '{"text":"Manufact demo"}')
            await page.locator('input[name="confirm_read_only"]').check()
            await page.locator('input[name="trusted_code"]').check()
            await page.get_by_role("button", name="Save and continue to deployment").click()
            await page.wait_for_load_state("domcontentloaded")
            assert "already reserved" in await page.locator("body").inner_text()
            assert await page.locator("#safe_tool").input_value() == "analyze_text"
            await page.fill("#server_name", SERVER)
            await page.locator('input[name="trusted_code"]').check()
            await page.get_by_role("button", name="Save and continue to deployment").click()
            await page.wait_for_url("**/deploy?project=*", timeout=40000)
            result["project_id"] = await page.locator("#project_id").input_value()
            assert await page.locator("#selected-id").inner_text() == SERVER
            assert await page.locator("#version").input_value() == "browser-v1"
            assert await page.locator("#source").input_value() == "build"
            result["checks"].append("Conflict preserves configuration; correction selects exact imported project/release")
            await page.screenshot(path=str(OUTPUT / "deploy-review.png"), full_page=True)
            await page.get_by_role("button", name="Deploy", exact=True).click()
            await page.wait_for_url("**/jobs/*", timeout=20000)
            job_id = page.url.rsplit("/", 1)[1]
            result["job_id"] = job_id
            print("Deploying uploaded source; job", job_id, flush=True)
            deadline, last_check = time.monotonic() + 600, 0
            previous_stage = None
            while time.monotonic() < deadline:
                response = await context.request.get(BASE + "/api/jobs/" + job_id)
                job = await response.json()
                if job.get("stage") != previous_stage:
                    previous_stage = job.get("stage")
                    print("Job stage:", previous_stage, flush=True)
                if job.get("finished_at"):
                    result["job"] = {k: v for k, v in job.items() if k != "log"}
                    (OUTPUT / "deployment.log").write_text(job["log"], encoding="utf-8")
                    assert job["status"] == "succeeded", job["log"][-6000:]
                    break
                if time.monotonic() - last_check > 20:
                    for name in ["stateless-demo", "stateful-demo"]:
                        check = await mcp(name)
                        result["availability"].append({"identity": check["identity"], "success": check["success"], "observed_at": check["observed_at"]})
                    last_check = time.monotonic()
                await asyncio.sleep(1)
            else:
                raise TimeoutError("New uploaded project deployment timed out")
            await page.goto(BASE + "/deployments/mcp-demo/" + SERVER + "?refresh=1", timeout=60000)
            assert await page.locator("#replicas").inner_text() == "2 / 2"
            await page.get_by_role("button", name="Test MCP tool").click()
            await page.wait_for_function("document.getElementById('mcp-result').textContent.includes('success')", timeout=220000)
            check = json.loads(await page.locator("#mcp-result").inner_text())
            result["uploaded_mcp"] = check
            assert check["success"]
            content = check["response"].get("structured_content") or json.loads(check["response"]["content"][0]["text"])
            assert content == {"words": 2, "characters": 13, "lines": 1, "slug": "manufact-demo"}, content
            await page.get_by_role("button", name="Load current pod logs").click()
            await page.wait_for_function("document.getElementById('app-logs').textContent.includes('text-analyzer-upload-')", timeout=40000)
            await page.locator('summary').filter(has_text="Change display name").click()
            await page.fill("#display_name", "My Text Analyzer")
            await page.get_by_role("button", name="Save display name").click()
            await page.wait_for_load_state("domcontentloaded")
            assert await page.locator("h1").inner_text() == "My Text Analyzer"
            await page.screenshot(path=str(OUTPUT / "server-details.png"), full_page=True)
            result["checks"].append("Real build/publish/rollout, real MCP tool, application logs and rename succeed")
            for name in ["stateless-demo", "stateful-demo"]:
                await mcp(name)
            for name in ["stateless-demo", "stateful-demo"]:
                await mcp(name, "mcp-auto")
            await page.goto(BASE + "/?refresh=1", timeout=60000)
            developer = page.locator('section[data-category="developer"]')
            assert await developer.locator('tr[data-identity="mcp-demo/manufact-invalid-build"]').count() == 0
            assert await developer.locator('tr[data-identity="mcp-demo/text-analyzer-upload"]').count() == 1
            assert "My Text Analyzer" in await developer.inner_text()
            assert "Echo & Math" in await developer.inner_text() and "Shared Counter" in await developer.inner_text()
            await page.screenshot(path=str(OUTPUT / "dashboard.png"), full_page=True)
            assert not result["browser_errors"], result["browser_errors"]
            await browser.close()
        after = await asyncio.to_thread(snapshot)
        (OUTPUT / "after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
        for name in ["stateless-demo", "stateful-demo", "stateful-demo-redis", "manufact-text-tools", "contract-probe"]:
            old = next(r for r in before["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            new = next(r for r in after["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            assert old["metadata"]["uid"] == new["metadata"]["uid"] and old["spec"] == new["spec"]
        result["checks"].append("All original always-on Deployment specs and identities preserved")
        result["result"] = "PASS" if result["github"]["result"] == "PASS" else "PARTIAL"
    except Exception:
        result.update(result="FAIL", error=traceback.format_exc())
        raise
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUTPUT / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"result": result["result"], "output": str(OUTPUT), "job_id": result.get("job_id"), "error": result.get("error")}, indent=2), flush=True)


async def finish(folder, reuse_checks=False):
    """Read-only completion after a deployment: never import/build/deploy again."""
    output = Path(folder)
    previous = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    result = {"result": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat(),
              "job_id": previous["job_id"], "mcp": [], "browser_errors": []}
    targets = [("mcp-demo", SERVER), ("mcp-demo", "stateless-demo"),
               ("mcp-demo", "stateful-demo"), ("mcp-demo", "manufact-text-tools"),
               ("mcp-auto", "stateless-demo"), ("mcp-auto", "stateful-demo")]
    if reuse_checks:
        retained = json.loads((output / "finish-summary.json").read_text(encoding="utf-8"))
        assert {r["identity"] for r in retained["mcp"] if r["success"]} == {ns + "/" + name for ns, name in targets}
        (output / "finish-collector-failure.json").write_text(json.dumps(retained, indent=2), encoding="utf-8")
        result.update(mcp=retained["mcp"], reused_mcp_checks="finish-collector-failure.json; actual successful calls retained, not sent again")
    assert previous["job"]["status"] == "succeeded", "Finish mode requires an already verified successful deployment"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1050})
            page = await context.new_page()
            page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
            for namespace, name in ([] if reuse_checks else targets):
                await page.goto(BASE + f"/deployments/{namespace}/{name}?refresh=1", timeout=60000)
                await page.get_by_role("button", name="Test MCP tool").click()
                await page.wait_for_function("document.getElementById('mcp-result').textContent.includes('success')", timeout=220000)
                check = json.loads(await page.locator("#mcp-result").inner_text())
                result["mcp"].append(check)
                assert check["success"], check
                if namespace == "mcp-demo" and name == "stateful-demo":
                    assert check["response"]["structured_content"] == {"result": 13}
                print("MCP PASS", namespace + "/" + name, flush=True)
                if name == SERVER:
                    assert await page.locator("h1").inner_text() == "My Text Analyzer"
                    assert await page.locator("#replicas").inner_text() == "2 / 2"
                    await page.get_by_role("button", name="Load current pod logs").click()
                    await page.wait_for_function("document.getElementById('app-logs').textContent.includes('text-analyzer-upload-')", timeout=40000)
                    await page.screenshot(path=str(output / "server-details-final.png"), full_page=True)
            await page.goto(BASE + "/?refresh=1", timeout=60000)
            developer = page.locator('section[data-category="developer"]')
            assert await developer.locator('tr[data-identity="mcp-demo/manufact-invalid-build"]').count() == 0
            assert await developer.locator('tr[data-identity="mcp-demo/text-analyzer-upload"]').count() == 1
            developer_text = await developer.inner_text()
            assert all(label in developer_text for label in ["Echo & Math", "Shared Counter", "Text Analyzer", "My Text Analyzer"])
            assert "attempt #" in await page.locator(".job-row").first.inner_text()
            await page.screenshot(path=str(output / "dashboard-final.png"), full_page=True)
            response = await context.request.get(BASE + "/api/jobs/" + previous["job_id"])
            job = await response.json()
            assert job["status"] == "succeeded" and job["display_name"] == "My Text Analyzer"
            result["job"] = {k: v for k, v in job.items() if k != "log"}
            inventory = await (await context.request.get(BASE + "/api/deployments?refresh=1")).json()
            (output / "inventory-final.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            assert not inventory["errors"] and not result["browser_errors"]
            await browser.close()
        after = await asyncio.to_thread(snapshot)
        (output / "after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
        before = json.loads((output / "before.json").read_text(encoding="utf-8"))
        for name in ["stateless-demo", "stateful-demo", "stateful-demo-redis", "manufact-text-tools", "contract-probe"]:
            old = next(r for r in before["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            new = next(r for r in after["items"] if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            assert old["metadata"]["uid"] == new["metadata"]["uid"] and old["spec"] == new["spec"]
        result.update(result="PASS", originals_preserved=True)
    except Exception:
        result.update(result="FAIL", error=traceback.format_exc())
        raise
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "finish-summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"result": result["result"], "output": str(output), "mcp_checks": len(result["mcp"])}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-import", help="Resume an already downloaded/pinned GitHub import without another network fetch")
    parser.add_argument("--resume-evidence", help="Continue a pre-build failure using its existing uploaded project")
    parser.add_argument("--finish", help="Read-only verification of an already deployed server; never rebuilds")
    parser.add_argument("--resume-checks", action="store_true", help="Finish dashboard/snapshot collection using all six retained successful MCP checks")
    options = parser.parse_args()
    asyncio.run(finish(options.finish, options.resume_checks) if options.finish else main(options))
