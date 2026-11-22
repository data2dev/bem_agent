"""HTTP auth (a real uvicorn server + MCP client) and Cloud Run storage behavior."""

import json
import socket
import sqlite3
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_results import make_run

from bem_agent import storage
from bem_agent.mcp_server import build_server, http_app, load_keys, main
from bem_agent.results import load_run
from bem_agent.tools import ToolContext, execute

KEYS = {"alice": "a" * 32, "grok-connector": "g" * 40}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    monkeypatch.delenv("BEM_ARCHIVE_DIR", raising=False)
    ctx = ToolContext(base_dir=tmp_path, max_simulations=2)
    load_run(make_run(tmp_path, "run_a", "Base"), ctx.db)
    server = build_server(ctx)
    port = free_port()
    config = uvicorn.Config(
        http_app(server, KEYS, "127.0.0.1"), host="127.0.0.1", port=port, log_level="warning"
    )
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", server
    uv.should_exit = True
    thread.join(timeout=5)


def call(url: str, headers: dict, tool: str, args: dict):
    async def scenario():
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
                return await client.call_tool(tool, args)

    return anyio.run(scenario)


# ---------- API keys over HTTP ----------


def test_health_check_needs_no_key(live_server):
    url, _ = live_server
    for path in ("/health", "/healthz"):
        r = httpx2.get(f"{url}{path}")
        assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key-wrong-key-wrong-key"},
        {"X-API-Key": "a" * 31},
        {"Authorization": "Basic " + "a" * 32},  # a valid key under an unsupported scheme
    ],
)
def test_requests_without_a_valid_key_are_rejected(live_server, headers):
    url, _ = live_server
    r = httpx2.post(
        f"{url}/mcp",
        headers={
            **headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401
    assert "API key" in r.json()["error"]
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_oauth_discovery_gets_a_clear_404(live_server):
    url, _ = live_server
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ):
        r = httpx2.get(f"{url}{path}")
        assert r.status_code == 404 and "API keys" in r.json()["error"]


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer " + KEYS["alice"]},
        {"Authorization": "bearer  " + KEYS["alice"] + " "},
        {"Authorization": KEYS["alice"]},  # bare key, no scheme
        {"Authorization": '"' + KEYS["alice"] + '"'},
        {"X-API-Key": KEYS["alice"]},
    ],
)
def test_accepted_key_formats(header):
    from bem_agent.mcp_server import APIKeyAuth

    auth = APIKeyAuth(None, KEYS)
    assert auth.identify({k.lower(): v for k, v in header.items()}) == "alice"


def test_rejection_log_never_contains_the_key():
    from bem_agent.mcp_server import APIKeyAuth

    text = APIKeyAuth.describe({"authorization": "Bearer " + "x" * 40, "user-agent": "grok"})
    assert "x" * 10 not in text
    assert "scheme=Bearer" in text and "user-agent" in text
    assert APIKeyAuth.describe({}).startswith("no auth headers")


def test_bearer_and_x_api_key_both_work_and_budgets_are_per_key(live_server):
    url, server = live_server
    r1 = call(url, {"Authorization": f"Bearer {KEYS['alice']}"}, "list_options", {})
    r2 = call(
        url,
        {"X-API-Key": KEYS["grok-connector"]},
        "get_run_details",
        {"run_id": "run_a", "section": "issues"},
    )
    assert not r1.is_error and "chicago" in json.loads(r1.content[0].text)["locations"]
    assert not r2.is_error and json.loads(r2.content[0].text)["run_id"] == "run_a"
    # each key got its own tool context (and so its own simulation budget)
    assert set(server.key_contexts._contexts) == {"alice", "grok-connector"}


def test_load_keys():
    assert load_keys("") == {}
    assert load_keys(json.dumps(KEYS)) == KEYS
    with pytest.raises(ValueError, match="JSON"):
        load_keys("alice=secret")
    with pytest.raises(ValueError, match="JSON object"):
        load_keys('["a"]')
    with pytest.raises(ValueError, match="24 characters"):
        load_keys('{"bob": "short"}')


def test_refuses_public_http_without_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("BEM_MCP_KEYS", raising=False)
    with pytest.raises(SystemExit, match="Refusing"):
        main(["--http", "--host", "0.0.0.0", "--runs-dir", str(tmp_path)])


# ---------- storage: local work, archived to the bucket mount ----------


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    monkeypatch.setenv("BEM_ARCHIVE_DIR", str(path))
    return path


def test_storage_is_a_no_op_without_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("BEM_ARCHIVE_DIR", raising=False)
    run_dir = make_run(tmp_path, "run_x", "X")
    assert storage.archive_run(run_dir) is None and run_dir.exists()
    assert storage.snapshot_db(tmp_path / "results.db") is None


def test_archive_and_find_run(tmp_path, archive):
    local = tmp_path / "local"
    run_dir = make_run(local, "run_x", "X")
    target = storage.archive_run(run_dir)
    assert target == archive / "runs" / "run_x"
    assert (target / "eplusout.sql").exists() and not run_dir.exists()  # local disk freed
    assert storage.find_run_dir(local, "run_x") == target
    assert storage.find_run_dir(local, "nope") is None


def test_db_snapshot_and_restore(tmp_path, archive):
    db = tmp_path / "local" / "results.db"
    load_run(make_run(tmp_path, "run_a", "A"), db)
    snapshot = storage.snapshot_db(db)
    assert snapshot == archive / "results.db"
    assert sqlite3.connect(snapshot).execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    fresh = tmp_path / "new_instance" / "results.db"
    assert storage.restore_db(fresh)  # a new Cloud Run instance gets the history back
    assert sqlite3.connect(fresh).execute("SELECT run_id FROM runs").fetchone() == ("run_a",)
    assert not storage.restore_db(fresh)  # never overwrites a local database


def test_tools_archive_runs_and_read_them_back(tmp_path, archive, monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    ctx = ToolContext(base_dir=tmp_path / "local")
    result = execute(ctx, "run_simulation", {"location": "chicago"})  # invalid here: no EnergyPlus
    run_id = result["run_id"]
    assert not (ctx.base_dir / run_id).exists()
    assert (archive / "runs" / run_id / "run.json").exists()
    assert (archive / "results.db").exists()
    issues = execute(ctx, "get_run_details", {"run_id": run_id, "section": "issues"})
    assert issues["status"] == "invalid"
