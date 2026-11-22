"""End-to-end check of a running MCP server over HTTP, the way a connector uses it.

    python scripts/smoke_mcp.py https://<service>.run.app   # key from $BEM_MCP_KEY
    python scripts/smoke_mcp.py http://localhost:8080 --skip-simulation

Checks: health endpoint, a request without a key is rejected, tools are listed, and
(unless skipped) one small sizing simulation completes and returns installed capacity.
Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


async def check(url: str, key: str, simulate: bool) -> None:
    async with httpx2.AsyncClient(timeout=60) as http:
        r = await http.get(f"{url}/healthz")
        if r.status_code != 200:
            fail(f"/healthz returned {r.status_code}")
        print("ok   health check")
        r = await http.post(
            f"{url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        if r.status_code != 401:
            fail(f"request without a key returned {r.status_code}, expected 401")
        print("ok   request without a key is rejected (401)")

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx2.AsyncClient(headers=headers, timeout=900) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
            tools = [t.name for t in (await client.list_tools()).tools]
            if "run_simulation" not in tools:
                fail(f"tools missing: {tools}")
            print(f"ok   {len(tools)} tools: {', '.join(tools)}")
            if not simulate:
                return
            start = time.monotonic()
            r = await client.call_tool(
                "run_simulation",
                {"name": "Smoke test", "location": "chicago", "floor_area_m2": 500, "hvac": "pthp"},
            )
            summary = json.loads(r.content[0].text)
            if r.is_error or summary.get("status") not in ("completed", "completed_with_warnings"):
                fail(f"simulation did not complete: {json.dumps(summary)[:1000]}")
            print(
                f"ok   simulation {summary['run_id']} in {time.monotonic() - start:.0f}s: "
                f"cooling capacity {summary.get('cooling_capacity_kw')} kW"
            )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url", help="server base URL, without /mcp")
    p.add_argument("--key", default=os.environ.get("BEM_MCP_KEY"), help="default $BEM_MCP_KEY")
    p.add_argument("--skip-simulation", action="store_true")
    a = p.parse_args()
    if not a.key:
        fail("no API key: pass --key or set BEM_MCP_KEY")
    anyio.run(check, a.url.rstrip("/"), a.key, not a.skip_simulation)
    print("All checks passed.")


if __name__ == "__main__":
    main()
