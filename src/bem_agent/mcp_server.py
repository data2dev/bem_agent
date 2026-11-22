"""MCP server exposing the simulation tools to any MCP client.

    bem-mcp                                     # stdio (local MCP clients)
    bem-mcp --http --port 8080                  # streamable HTTP at /mcp

It serves the same tool registry as the built-in agent loop (bem_agent.tools), so both
paths share one set of schemas, validation and guardrails. The LLM lives in the client;
this server never calls a model.

Over HTTP every request needs an API key, one per person, from BEM_MCP_KEYS (a JSON map
of name -> key, e.g. from Secret Manager). Send it as `Authorization: Bearer <key>` or
`X-API-Key: <key>`. Each key gets its own simulation budget and every tool call is
logged with the key's name. The server refuses to listen on a public interface without
keys. GET /healthz is open, for health checks.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import hmac
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server.lowlevel import Server

from . import storage
from .tools import TOOLS, ToolContext, execute, to_text

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Building energy modeling with EnergyPlus. Start with list_options. run_simulation "
    "builds and runs one model; run_parametric_study runs every combination of options. "
    "Peak zone loads do not depend on the HVAC system: compare systems on installed "
    "capacity and annual EUI. All units are SI."
)

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Name of the API key behind the current request ("local" for stdio / no-auth).
current_key: contextvars.ContextVar[str] = contextvars.ContextVar("current_key", default="local")


class KeyContexts:
    """One ToolContext per API key: separate simulation budgets, shared storage."""

    def __init__(self, base: ToolContext) -> None:
        self.base = base
        self._contexts: dict[str, ToolContext] = {}
        self._lock = threading.Lock()

    def get(self, key_name: str) -> ToolContext:
        with self._lock:
            if key_name not in self._contexts:
                self._contexts[key_name] = ToolContext(
                    base_dir=self.base.base_dir,
                    max_simulations=self.base.max_simulations,
                    timeout_s=self.base.timeout_s,
                    max_workers=self.base.max_workers,
                )
            return self._contexts[key_name]


def build_server(ctx: ToolContext | None = None) -> Server:
    contexts = KeyContexts(ctx or ToolContext())
    storage.restore_db(contexts.base.db)
    tools = [
        types.Tool(name=t.name, description=t.description, input_schema=t.parameters) for t in TOOLS
    ]

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        key_name = current_key.get()
        tool_ctx = contexts.get(key_name)
        log.info("tool=%s key=%s", params.name, key_name)
        # Simulations block for seconds to minutes: run them off the event loop.
        result = await anyio.to_thread.run_sync(
            execute, tool_ctx, params.name, dict(params.arguments or {})
        )
        if "error" in result:
            log.info("tool=%s key=%s error=%s", params.name, key_name, result["error"][:200])
        content: list[Any] = [types.TextContent(type="text", text=to_text(result))]
        chart = result.get("chart")
        if chart and Path(chart).is_file():
            data = base64.b64encode(Path(chart).read_bytes()).decode()
            content.append(types.ImageContent(type="image", data=data, mime_type="image/png"))
        return types.CallToolResult(content=content, is_error="error" in result)

    server = Server(
        "bem_agent",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.key_contexts = contexts  # type: ignore[attr-defined]  # for tests and logging
    return server


# ---------- API keys ----------


def load_keys(raw: str | None = None) -> dict[str, str]:
    """Parse BEM_MCP_KEYS: a JSON object mapping a person/client name to their key."""
    raw = os.environ.get("BEM_MCP_KEYS", "") if raw is None else raw
    if not raw.strip():
        return {}
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"BEM_MCP_KEYS is not valid JSON: {exc}") from exc
    if not isinstance(keys, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
    ):
        raise ValueError('BEM_MCP_KEYS must be a JSON object like {"name": "key"}')
    short = [name for name, key in keys.items() if len(key) < 24]
    if short:
        raise ValueError(f"Keys must be at least 24 characters: {short}")
    return keys


class APIKeyAuth:
    """ASGI middleware: reject requests without a valid API key before they reach MCP."""

    def __init__(self, app: Any, keys: dict[str, str]) -> None:
        self.app = app
        self._keys = [(name, key.encode()) for name, key in keys.items()]

    def identify(self, headers: dict[str, str]) -> str | None:
        token = headers.get("x-api-key", "")
        auth = headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            return None
        candidate = token.encode()
        match = None
        for name, key in self._keys:  # check every key: timing doesn't reveal which matched
            if hmac.compare_digest(candidate, key):
                match = name
        return match

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/healthz":
            await _json(send, 200, {"ok": True})
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        name = self.identify(headers)
        if name is None:
            log.warning("rejected request without a valid API key from %s", scope.get("client"))
            await _json(
                send,
                401,
                {"error": "Missing or invalid API key (Authorization: Bearer <key>)"},
                [(b"www-authenticate", b'Bearer realm="bem_agent"')],
            )
            return
        token = current_key.set(name)
        try:
            await self.app(scope, receive, send)
        finally:
            current_key.reset(token)


async def _json(send: Any, status: int, body: dict, extra: list | None = None) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(extra or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def http_app(server: Server, keys: dict[str, str], host: str = "0.0.0.0") -> Any:
    app = server.streamable_http_app(host=host, stateless_http=True, json_response=True)
    return APIKeyAuth(app, keys) if keys else app


def serve_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(run)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bem-mcp", description=__doc__)
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--runs-dir", type=Path)
    p.add_argument(
        "--no-auth", action="store_true", help="allow HTTP without API keys (local testing only)"
    )
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ctx = ToolContext(base_dir=a.runs_dir) if a.runs_dir else ToolContext()
    server = build_server(ctx)
    if not a.http:
        serve_stdio(server)
        return
    keys = load_keys()
    if not keys and not a.no_auth and a.host not in LOCAL_HOSTS:
        sys.exit(
            "Refusing to serve on a public interface without API keys: set BEM_MCP_KEYS "
            '(JSON {"name": "key"}), or use --no-auth for local testing.'
        )
    if not keys:
        log.warning("Serving without API keys")
    else:
        log.info("API keys loaded for: %s", ", ".join(sorted(keys)))
    import uvicorn

    uvicorn.run(
        http_app(server, keys, a.host),
        host=a.host,
        port=a.port,
        log_level="info",
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
