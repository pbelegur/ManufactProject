# Independent text-tools MCP sample

Python MCP server with one read-only `analyze_text` tool. It returns word,
character and line counts and a normalized slug. It has its own Dockerfile and
dependencies, with no imports from the platform's two example servers.

This sample intentionally has no mcp.json. Upload its complete project ZIP in
the UI, then choose **stateless** explicitly in the guided configuration.
Use port 8000, /mcp, Dockerfile, context `.`, release `v1` and a unique name.
Configure Test MCP as `analyze_text` with
`{"text":"Manufact builds MCP tools"}` and confirm it is read-only.

The platform creates a validated deployment configuration and runs deployctl.py.
See the repository's BYO_MCP_GUIDE.md for exact browser steps.
