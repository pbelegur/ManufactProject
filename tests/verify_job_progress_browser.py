"""Read-only browser smoke check for existing deployment jobs; no deployments."""
import argparse
import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright


async def main(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    errors, observations = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:8080/jobs/" + args.job)
        await page.wait_for_function("document.body.dataset.jobStage !== undefined")
        previous = None
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            stage = await page.locator("#job-status").inner_text()
            assert stage in {"QUEUED", "BUILDING", "PUSHING", "DEPLOYING", "VERIFYING", "SUCCEEDED", "FAILED"}, stage
            assert await page.locator("#job-id").inner_text() == args.job
            log = await page.locator("#job-log").inner_text()
            observation = {"stage": stage, "log_chars": len(log), "queue": await page.locator("#queue-state").inner_text(),
                           "history_rows": await page.locator("#stage-history p").count()}
            if observation != previous:
                observations.append(observation)
                previous = observation
            await asyncio.sleep(1)
        await page.screenshot(path=str(folder / (args.job + "-browser.png")), full_page=True)
        assert not errors, errors
        evidence = {"job_id": args.job, "browser_version": browser.version, "observations": observations, "errors": errors}
        (folder / (args.job + "-browser.json")).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence, indent=2))
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--output", default="evidence/concurrency/browser")
    asyncio.run(main(parser.parse_args()))
