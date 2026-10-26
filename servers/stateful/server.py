"""Application-stateful MCP demo with Redis shared by all replicas.

The MCP HTTP transport remains stateless: Redis stores application data.
"""
import os
import asyncio

import redis
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

mcp = MCPServer("stateful-demo")


def store() -> redis.Redis:
    # Each caller owns and closes its client and connection pool.
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _key(name: str) -> str:
    if not name or len(name) > 128 or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError("Counter name must be 1-128 letters, digits, '-' or '_'.")
    return f"mcp:counter:{name}"


@mcp.tool()
async def increment_counter(name: str, amount: int = 1, delay_seconds: float = 0) -> int:
    """Atomically increment Redis, optionally waiting 0-10s for concurrency tests."""
    if not 1 <= amount <= 1000:
        raise ValueError("amount must be 1-1000")
    if not 0 <= delay_seconds <= 10:
        raise ValueError("delay_seconds must be 0-10")
    await asyncio.sleep(delay_seconds)
    key = _key(name)
    def increment():
        with store() as client:
            return int(client.incrby(key, amount))
    return await asyncio.to_thread(increment)


@mcp.tool()
def get_counter(name: str) -> int:
    """Read the shared counter from Redis; zero when absent."""
    key = _key(name)
    with store() as client:
        return int(client.get(key) or 0)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Simple process health; does not report external dependency health."""
    return JSONResponse({"status": "ok"})


def security() -> TransportSecuritySettings:
    # Explicit allowlist; add ingress DNS names through MCP_ALLOWED_HOSTS.
    defaults = [
        "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*",
        "stateless", "stateless:*", "stateful", "stateful:*",
        "stateless-demo", "stateless-demo:*", "stateful-demo", "stateful-demo:*",
    ]
    extra = [x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if x.strip()]
    return TransportSecuritySettings(allowed_hosts=defaults + extra, allowed_origins=[])


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security(),
    )
