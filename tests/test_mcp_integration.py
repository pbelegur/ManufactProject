"""Real SDK clients over HTTP. Restart is explicit and scoped to this Compose project."""
import asyncio
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from uuid import uuid4

from mcp import Client
import pytest

from smoke_mcp import call


pytestmark = pytest.mark.integration
STATELESS = os.getenv("STATELESS_URL", "http://localhost:8001/mcp")
STATEFUL = os.getenv("STATEFUL_URL", "http://localhost:8002/mcp")
ROOT = Path(__file__).resolve().parents[1]


def test_health():
    for url in (STATELESS, STATEFUL):
        with urlopen(url.removesuffix("/mcp") + "/health", timeout=3) as response:
            assert response.status == 200


def test_stateless_tools():
    async def check():
        assert await call(STATELESS, "echo", {"message": "hello MCP \u2603"}) == "hello MCP \u2603"
        assert await call(STATELESS, "add_numbers", {"a": -7, "b": 12}) == 5
        assert await call(STATELESS, "echo", {"message": ""}) == ""
        assert await call(STATELESS, "add_numbers", {"a": 0, "b": 0}) == 0
    asyncio.run(check())


def test_mcp_tool_discovery():
    async def check():
        for url, expected in (
            (STATELESS, {"echo", "add_numbers"}),
            (STATEFUL, {"increment_counter", "get_counter"}),
        ):
            async with Client(url, read_timeout_seconds=10) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == expected
    asyncio.run(check())


def test_counter_across_connections():
    async def check():
        name = "connections_" + uuid4().hex
        # call() creates and closes an independent SDK client every time.
        assert await call(STATEFUL, "get_counter", {"name": name}) == 0
        assert await call(STATEFUL, "increment_counter", {"name": name, "amount": 2}) == 2
        assert await call(STATEFUL, "increment_counter", {"name": name}) == 3
        assert await call(STATEFUL, "get_counter", {"name": name}) == 3
    asyncio.run(check())


def test_atomic_concurrent_increments():
    async def check():
        name = "concurrent_" + uuid4().hex
        values = await asyncio.gather(*[
            call(STATEFUL, "increment_counter", {"name": name}) for _ in range(20)
        ])
        assert sorted(values) == list(range(1, 21))
        assert await call(STATEFUL, "get_counter", {"name": name}) == 20
    asyncio.run(check())


def test_rejected_counter_inputs_do_not_mutate_state():
    async def check():
        name = "validation_" + uuid4().hex
        async with Client(STATEFUL, raise_exceptions=False) as client:
            for args in ({"name": name, "amount": 0}, {"name": name, "amount": 1001}, {"name": "bad/key"}):
                result = await client.call_tool("increment_counter", args)
                assert result.is_error
        assert await call(STATEFUL, "get_counter", {"name": name}) == 0
    asyncio.run(check())


@pytest.mark.compose_restart
def test_state_survives_container_restart():
    if STATEFUL != "http://localhost:8002/mcp":
        pytest.fail("Compose restart test requires the default local STATEFUL_URL")
    name = "restart_" + uuid4().hex
    assert asyncio.run(call(STATEFUL, "increment_counter", {"name": name, "amount": 7})) == 7
    subprocess.run(["docker", "compose", "restart", "--timeout", "30", "stateful"], cwd=ROOT, check=True, timeout=60)
    deadline = time.monotonic() + 60
    while True:
        try:
            with urlopen("http://localhost:8002/health", timeout=2) as response:
                assert response.status == 200
            break
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    assert asyncio.run(call(STATEFUL, "get_counter", {"name": name})) == 7
    print(f"Redis counter {name}: 7 before restart, 7 after restart")
