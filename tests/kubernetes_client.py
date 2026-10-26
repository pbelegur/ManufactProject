"""Runs inside a Kubernetes Job; reaches real ClusterIP Services using MCP."""
import asyncio
import json
import os
from uuid import uuid4

from mcp import Client


async def call(url, tool, arguments):
    async with Client(url, read_timeout_seconds=15) as client:
        result = await client.call_tool(tool, arguments)
        assert not result.is_error, str(result)
        value = result.structured_content
        if value is None:
            # MCP tools may legitimately return JSON as TextContent rather than
            # advertise an output schema (e.g. a bare dict return annotation).
            texts = [block.text for block in result.content if hasattr(block, "text")]
            assert len(texts) == 1, str(result)
            value = json.loads(texts[0])
        return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value


async def main():
    stateless = "http://stateless-demo:8000/mcp"
    stateful = "http://stateful-demo:8000/mcp"
    assert await call(stateless, "echo", {"message": "through-ClusterIP"}) == "through-ClusterIP"
    assert await call(stateless, "add_numbers", {"a": 19, "b": 23}) == 42
    key = "service_" + uuid4().hex
    assert await call(stateful, "get_counter", {"name": key}) == 0
    assert await call(stateful, "increment_counter", {"name": key, "amount": 4}) == 4
    assert await call(stateful, "get_counter", {"name": key}) == 4
    await asyncio.gather(*[
        call(stateless, "add_numbers", {"a": 1, "b": 2}),
        call(stateful, "get_counter", {"name": key}),
    ])
    if os.getenv("PERSISTED_COUNTER"):
        assert await call(stateful, "get_counter", {"name": os.environ["PERSISTED_COUNTER"]}) == 13
    if os.getenv("VERIFY_GENERIC") == "1":
        result = await call("http://contract-probe:8000/mcp", "multiply", {"value": 6, "factor": 7})
        assert result == {"product": 42, "label": "configured-by-project"}, result
    print(json.dumps({"result": "PASS", "routing": "Kubernetes ClusterIP Services",
                      "stateless": ["echo", "add_numbers"], "stateful": ["increment_counter", "get_counter"],
                      "persisted_counter": os.getenv("PERSISTED_COUNTER"),
                      "generic_project": os.getenv("VERIFY_GENERIC") == "1"}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
