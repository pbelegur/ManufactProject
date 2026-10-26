"""Live, focused HTTP validation. Never submits a deployment job."""
import asyncio
import json
from pathlib import Path
import re

from playwright.async_api import async_playwright


async def main():
    base = "http://127.0.0.1:8080"
    checks = []
    async with async_playwright() as playwright:
        client = await playwright.request.new_context(base_url=base)
        page = await client.get("/imports")
        token = re.search(r'name="csrf" value="([^"]+)"', await page.text())[1]
        sample = await (await client.get("/sample-project.zip")).body()
        body = {"kind": "zip", "csrf": token, "trusted_code": "yes",
                "archive": {"name": "guards.zip", "mimeType": "application/zip", "buffer": sample}}
        denied = await client.post("/imports", multipart={**body, "csrf": "wrong"}, max_redirects=0)
        assert denied.status == 403
        checks.append({"check": "Missing/invalid import CSRF", "status": denied.status})
        denied = await client.post("/imports", form={"csrf": token, "kind": "github", "trusted_code": "yes",
            "repository": "http://127.0.0.1:4566"}, max_redirects=0)
        assert denied.status == 422 and "not explicitly trusted" in await denied.text()
        checks.append({"check": "Untrusted URL / SSRF rejected before fetch", "status": denied.status})
        accepted = await client.post("/imports", multipart=body, max_redirects=0)
        assert accepted.status == 303, await accepted.text()
        target = accepted.headers["location"] + "/configure"
        valid = {"csrf": token, "server_name": "guard-only-not-registered", "dockerfile": "Dockerfile", "build_context": ".",
                 "port": "8000", "mcp_path": "/mcp", "mode": "stateless", "backend": "always-on", "version": "v1",
                 "redis_url": "", "safe_tool": "analyze_text", "safe_arguments": '{"text":"read-only"}',
                 "confirm_read_only": "true", "trusted_code": "true"}
        for label, changes, expected in [
            ("Existing name reserved", {"server_name": "stateless-demo"}, 409),
            ("Redis runtime name reserved", {"server_name": "stateful-demo-redis"}, 409),
            ("Alternate port rejected", {"port": "9000"}, 422),
            ("Alternate MCP endpoint rejected", {"mcp_path": "/other"}, 422),
            ("Build context traversal rejected", {"build_context": "../../.."}, 422),
            ("Absolute Dockerfile rejected", {"dockerfile": "C:/Windows/file"}, 422),
            ("Mutation tool rejected", {"safe_tool": "increment_counter"}, 422),
            ("Read-only confirmation required", {"confirm_read_only": "false"}, 422),
            ("Tool arguments must be object", {"safe_arguments": "[]"}, 422),
            ("Raw command field forbidden", {"command": "whoami"}, 422),
            ("Shell-looking release rejected", {"version": "v1; whoami"}, 422),
            ("Explicit state model required", {"mode": ""}, 422),
        ]:
            response = await client.post(target, form={**valid, **changes}, max_redirects=0)
            assert response.status == expected, (label, response.status, await response.text())
            checks.append({"check": label, "status": response.status, "result": "PASS"})
        await client.dispose()
    destination = Path("evidence/ui-final/guards.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"result": "PASS", "checks": checks}, indent=2))
    print(f"PASS: {len(checks)} live guard checks; no jobs submitted")


if __name__ == "__main__":
    asyncio.run(main())
