"""Integration smoke test for Docker Compose or kubectl port-forwards.

Run: python tests/smoke_mcp.py
"""
import asyncio
import os
from uuid import uuid4

from mcp import Client


async def call(url: str, tool: str, args: dict) -> object:
    async with Client(url, read_timeout_seconds=10) as client:
        result = await client.call_tool(tool, args)
        if result.is_error:
            raise AssertionError(f"{tool} failed: {result}")
        structured = result.structured_content
        # v2's scalar tool results are represented as {"result": scalar}.
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured


async def main():
    stateless = os.getenv("STATELESS_URL", "http://localhost:8001/mcp")
    stateful = os.getenv("STATEFUL_URL", "http://localhost:8002/mcp")
    assert await call(stateless, "echo", {"message": "hello"}) == "hello"
    assert await call(stateless, "add_numbers", {"a": 3, "b": 5}) == 8
    key = "smoke_" + uuid4().hex
    assert await call(stateful, "increment_counter", {"name": key, "amount": 2}) == 2
    assert await call(stateful, "get_counter", {"name": key}) == 2
    assert await call(stateful, "increment_counter", {"name": key, "amount": 3}) == 5
    print("PASS: stateless tools and Redis-backed stateful tools respond over MCP")


if __name__ == "__main__":
    asyncio.run(main())
