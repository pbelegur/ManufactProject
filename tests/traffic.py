"""Generate 2-3 MCP requests/second while deploying/updating the other server.

Print successes/failures so we do not claim zero downtime without measurement.
"""
import asyncio
import os
import time
from mcp import Client


async def main():
    url = os.getenv("STATELESS_URL", "http://localhost:8001/mcp")
    seconds = int(os.getenv("DURATION", "60"))
    rate = float(os.getenv("RATE", "2.5"))
    success = errors = 0
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            async with Client(url, read_timeout_seconds=10) as client:
                result = await client.call_tool("add_numbers", {"a": 2, "b": 2})
                if result.is_error or result.structured_content.get("result") != 4:
                    raise RuntimeError(f"unexpected MCP result: {result}")
            success += 1
        except Exception as exc:
            errors += 1
            print("REQUEST FAILURE", type(exc).__name__, str(exc)[:180])
        await asyncio.sleep(1/rate)
    print(f"RPS target={rate}, successes={success}, failures={errors}, observed={success/(time.monotonic()-start):.2f} successful RPS")


if __name__ == "__main__":
    asyncio.run(main())
