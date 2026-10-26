"""Stateless example: every tool invocation is independent."""
import os
import asyncio
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

mcp = MCPServer("stateless-demo")


@mcp.tool()
async def echo(message: str, delay_seconds: float = 0) -> str:
    """Return the message, optionally waiting 0-10 seconds for concurrency tests."""
    if not 0 <= delay_seconds <= 10:
        raise ValueError("delay_seconds must be 0-10")
    await asyncio.sleep(delay_seconds)
    return message


@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Add two numbers without using persistent state."""
    return a + b


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
