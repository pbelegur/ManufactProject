"""Independent MCP text inspection project; no example-server imports."""
import os
import re

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

server = MCPServer("text-tools")


@server.tool()
def analyze_text(text: str) -> dict:
    """Read-only text statistics: words, characters, lines and a normalized slug."""
    if len(text) > 100_000:
        raise ValueError("Text must contain at most 100,000 characters")
    words = re.findall(r"\b\w+\b", text)
    return {"words": len(words), "characters": len(text), "lines": len(text.splitlines()),
            "slug": "-".join(word.lower() for word in words)}


@server.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    hosts += [value for value in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if value]
    server.run(transport="streamable-http", host=os.getenv("HOST", "0.0.0.0"),
               port=int(os.getenv("PORT", "8000")), streamable_http_path="/mcp",
               stateless_http=True, json_response=True,
               transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]))
