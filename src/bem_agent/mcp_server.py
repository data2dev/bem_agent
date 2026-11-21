"""MCP server exposing the simulation tools to any MCP client.

    python -m bem_agent.mcp_server                        # stdio (e.g. Claude Desktop)
    python -m bem_agent.mcp_server --http --port 8080     # streamable HTTP at /mcp

It serves the same tool registry as the built-in agent loop (bem_agent.tools), so both
paths share one set of schemas, validation and guardrails. The LLM lives in the client;
this server never calls a model.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server.lowlevel import Server

from .tools import TOOLS, ToolContext, execute, to_text

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Building energy modeling with EnergyPlus. Start with list_options. run_simulation "
    "builds and runs one model; run_parametric_study runs every combination of options. "
    "Peak zone loads do not depend on the HVAC system: compare systems on installed "
    "capacity and annual EUI. All units are SI."
)


def build_server(ctx: ToolContext | None = None) -> Server:
    ctx = ctx or ToolContext()
    tools = [
        types.Tool(name=t.name, description=t.description, input_schema=t.parameters) for t in TOOLS
    ]

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Simulations block for seconds to minutes: run them off the event loop.
        result = await anyio.to_thread.run_sync(
            execute, ctx, params.name, dict(params.arguments or {})
        )
        content: list[Any] = [types.TextContent(type="text", text=to_text(result))]
        chart = result.get("chart")
        if chart and Path(chart).is_file():
            data = base64.b64encode(Path(chart).read_bytes()).decode()
            content.append(types.ImageContent(type="image", data=data, mime_type="image/png"))
        return types.CallToolResult(content=content, is_error="error" in result)

    return Server(
        "bem_agent",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_http(server: Server, host: str, port: int) -> None:
    import uvicorn

    app = server.streamable_http_app(host=host, stateless_http=True, json_response=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bem-mcp", description=__doc__)
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--runs-dir", type=Path)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    ctx = ToolContext(base_dir=a.runs_dir) if a.runs_dir else ToolContext()
    server = build_server(ctx)
    if a.http:
        serve_http(server, a.host, a.port)
    else:
        anyio.run(serve_stdio, server)


if __name__ == "__main__":
    main()
