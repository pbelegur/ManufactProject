"""A third independent project used to verify the generic deployment contract."""
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

server = MCPServer("independent-contract-probe")


@server.tool()
def multiply(value: int, factor: int) -> dict:
    return {"product": value * factor, "label": os.environ["PROBE_LABEL"]}


@server.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    hosts = os.environ.get("MCP_ALLOWED_HOSTS", "localhost,localhost:*").split(",")
    server.run(transport="streamable-http", host=os.getenv("HOST", "0.0.0.0"),
               port=int(os.getenv("PORT", "8000")), streamable_http_path="/mcp",
               json_response=True, stateless_http=True,
               transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]))
