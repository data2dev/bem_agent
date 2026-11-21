import json

import anyio
import pytest
from mcp import Client
from test_results import make_run

from bem_agent.mcp_server import build_server
from bem_agent.results import load_run
from bem_agent.tools import TOOLS, ToolContext, execute, spec_from_args, to_text, tool_specs


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    c = ToolContext(base_dir=tmp_path, max_simulations=3)
    for run_id, name, scale in [("run_a", "Base", 1.0), ("run_b", "More glass", 1.5)]:
        load_run(make_run(tmp_path, run_id, name, scale), c.db)
    return c


# ---------- schemas ----------


def test_schemas_sent_to_llm_have_no_hard_constraints():
    """Groq rejects a whole request when a tool call breaks the schema; we validate."""
    text = json.dumps([t.parameters for t in tool_specs()])
    assert '"enum"' not in text and '"minimum"' not in text and '"maximum"' not in text
    run = next(t for t in tool_specs() if t.name == "run_simulation").parameters
    assert "one of: chicago" in run["properties"]["location"]["description"]
    assert "range: 0..0.8" in run["properties"]["window_to_wall_ratio"]["description"]


@pytest.mark.parametrize(
    "given, expected",
    [
        ("Washington DC", "washington_dc"),
        ("Washington, D.C.", "washington_dc"),
        ("San Francisco, CA", "san_francisco"),
        ("CHICAGO", "chicago"),
        ("Golden, Colorado", "golden_co"),
        ("Tampa FL", "tampa"),
    ],
)
def test_location_names(given, expected):
    assert spec_from_args({"location": given}).location == expected


def test_tool_schemas_are_compact_and_valid():
    specs = tool_specs()
    assert len({t.name for t in specs}) == len(specs)
    for t in specs:
        assert t.parameters["type"] == "object"
        assert len(t.description) < 800  # query_results carries the table schema
    total = sum(
        len(json.dumps({"name": t.name, "description": t.description, "parameters": t.parameters}))
        for t in specs
    )
    # Sent with every LLM call; Groq's free tier allows 8K tokens per minute.
    assert total < 7000, total


# ---------- building arguments ----------


def test_spec_from_floor_area():
    spec = spec_from_args(
        {"location": "tampa", "floor_area_m2": 3000, "num_floors": 3, "hvac": "pthp"}
    )
    assert spec.geometry.total_floor_area_m2 == pytest.approx(3000)
    assert spec.geometry.num_floors == 3
    assert spec.presets == {"envelope": "typical", "loads": "office", "schedules": "office"}


def test_spec_overrides():
    spec = spec_from_args(
        {
            "location": "chicago",
            "envelope": "efficient",
            "overrides": {
                "window_shgc": 0.2,
                "lighting_w_per_m2": 5.0,
                "cooling_setpoint_c": 25.0,
                "weekday_hours": [7, 19],
            },
        }
    )
    assert spec.envelope.window_shgc == 0.2
    assert spec.envelope.window_u_si == 1.4  # rest of the efficient preset kept
    assert spec.loads.lighting_w_per_m2 == 5.0
    assert spec.schedules.cooling_c == (25.0, 29.4)
    assert spec.schedules.weekdays == (7, 19)
    assert spec.presets == {"envelope": "custom", "loads": "custom", "schedules": "custom"}


@pytest.mark.parametrize(
    "args, message",
    [
        ({"location": "chicago", "colour": "red"}, "Unknown building fields"),
        ({"location": "chicago", "floor_area_m2": 100, "length_m": 10}, "not both"),
        ({"location": "chicago", "overrides": {"fan_power": 1}}, "Unknown overrides"),
        ({"location": "chicago", "window_to_wall_ratio": 0.95}, "window_to_wall_ratio"),
        ({"location": "chicago", "hvac": "geothermal"}, "hvac"),
        ({"location": "/etc/passwd.epw"}, "Unknown location"),
    ],
)
def test_bad_building_args_become_tool_errors(ctx, args, message):
    result = execute(ctx, "run_simulation", args)
    assert message in result["error"]
    assert ctx.simulations_used == 0  # rejected before using the budget


# ---------- execution ----------


def test_execute_errors(ctx):
    assert "Unknown tool" in execute(ctx, "rm_rf", {})["error"]
    assert "JSON object" in execute(ctx, "list_options", [1, 2])["error"]
    assert (
        "Invalid run_id"
        in execute(ctx, "get_run_details", {"run_id": "../etc", "section": "issues"})["error"]
    )
    assert (
        "Unknown run_id"
        in execute(ctx, "get_run_details", {"run_id": "nope", "section": "issues"})["error"]
    )


def test_simulation_budget(ctx):
    r = execute(ctx, "run_simulation", {"location": "chicago"})
    assert r["status"] == "invalid"  # no EnergyPlus here: caught by pre-run checks
    assert any("WEATHER" in i for i in r["issues"])
    study = {
        "name": "Too big",
        "base": {"location": "chicago"},
        "parameters": {"geometry.window_to_wall_ratio": [0.2, 0.3, 0.4, 0.5]},
    }
    assert "budget" in execute(ctx, "run_parametric_study", study)["error"]
    assert execute(ctx, "list_options", {})["simulations_left"] == 3  # invalid run refunded


def test_study_parameter_errors(ctx):
    r = execute(
        ctx,
        "run_parametric_study",
        {"name": "x", "base": {"location": "chicago"}, "parameters": {"secret": [1]}},
    )
    assert "Cannot vary" in r["error"]


def test_result_tools(ctx):
    details = execute(ctx, "get_run_details", {"run_id": "run_a", "section": "equipment"})
    assert details["rows"][0][0] == "Boiler:HotWater"
    zones = execute(ctx, "get_run_details", {"run_id": "run_a", "section": "zone_loads"})
    assert zones["rows"][0][:3] == ["F1_CORE", "Cooling", 10.0]
    uses = execute(ctx, "get_run_details", {"run_id": "run_a", "section": "end_uses"})
    assert uses["end_uses_kwh"][0][0] == "Heating"
    compared = execute(
        ctx, "compare_runs", {"run_ids": ["run_a", "run_b"], "sort_by": "site_eui_kwh_m2"}
    )
    assert [r["run_id"] for r in compared["runs"]] == ["run_a", "run_b"]
    ts = execute(ctx, "get_timeseries", {"run_id": "run_a", "variable": "Electricity:Facility"})
    assert ts["columns"] == ["month", "value"] and ts["points"][0] == [1, 480.0]
    assert (
        "too long"
        in execute(
            ctx,
            "get_timeseries",
            {"run_id": "run_a", "variable": "Electricity:Facility", "freq": "hourly"},
        )["error"]
    )
    q = execute(ctx, "query_results", {"sql": "SELECT COUNT(*) AS n FROM runs"})
    assert q["rows"] == [[2]]
    assert "Only SELECT" in execute(ctx, "query_results", {"sql": "DELETE FROM runs"})["error"]
    chart = execute(
        ctx,
        "make_chart",
        {"kind": "metric", "run_ids": ["run_a", "run_b"], "metric": "peak_cooling_kw"},
    )
    assert chart["chart"].endswith(".png")
    assert len(execute(ctx, "list_history", {})["runs"]) == 2


def test_to_text_truncates():
    text = to_text({"rows": ["x" * 100] * 100}, max_chars=500)
    assert len(text) <= 500 and "truncated" in text
    assert to_text({"a": 1}) == '{"a":1}'


# ---------- MCP server (real MCP client, in-process) ----------


def test_mcp_server_lists_and_calls_tools(ctx):
    async def scenario():
        async with Client(build_server(ctx)) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == [t.name for t in TOOLS]
            assert listed.tools[1].input_schema["required"] == ["location"]
            # MCP clients validate their own calls, so they keep the strict schema
            assert "enum" in listed.tools[1].input_schema["properties"]["location"]

            options = await client.call_tool("list_options", {})
            assert not options.is_error
            assert "chicago" in json.loads(options.content[0].text)["locations"]

            chart = await client.call_tool(
                "make_chart",
                {"kind": "metric", "run_ids": ["run_a", "run_b"], "metric": "site_eui_kwh_m2"},
            )
            assert [c.type for c in chart.content] == ["text", "image"]
            assert chart.content[1].mime_type == "image/png"

            bad = await client.call_tool("get_run_details", {"run_id": "nope", "section": "issues"})
            assert bad.is_error
            assert "Unknown run_id" in bad.content[0].text

    anyio.run(scenario)
