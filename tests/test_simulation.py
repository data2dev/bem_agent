"""Real EnergyPlus runs. Need ENERGYPLUS_DIR (CI installs EnergyPlus for these)."""

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from bem_agent.model import CITIES, BuildingSpec, HVACSystem, resolve_weather
from bem_agent.results import (
    compare_runs,
    end_uses,
    equipment_sizes,
    run_sql,
    study_table,
    timeseries,
)
from bem_agent.results.plots import plot_metric_comparison, plot_study, plot_timeseries
from bem_agent.sim import ParametricStudy, run_study, simulate

pytestmark = pytest.mark.energyplus

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "runs" / "ci"
REFERENCE = Path(__file__).parent / "reference" / "office_chicago_ideal.json"
DB = OUT_ROOT / "results.db"


@pytest.fixture(scope="module", autouse=True)
def require_energyplus():
    if not os.environ.get("ENERGYPLUS_DIR") or not shutil.which("energyplus"):
        pytest.skip("EnergyPlus not installed (set ENERGYPLUS_DIR and PATH)")


def demo_spec(hvac: HVACSystem, **kw) -> BuildingSpec:
    return BuildingSpec(
        name=f"CI {hvac.value}",
        location="chicago",
        geometry={"length_m": 40, "width_m": 25, "num_floors": 2, "window_to_wall_ratio": 0.4},
        hvac=hvac,
        **kw,
    )


def assert_completed(run):
    assert run.status in ("completed", "completed_with_warnings"), json.dumps(
        run.summary(), indent=2
    )


@pytest.mark.parametrize("city", sorted(CITIES))
def test_demo_city_weather_files_exist(city):
    assert len(resolve_weather(city).site_objects()) == 3


@pytest.mark.parametrize("hvac", list(HVACSystem))
def test_sizing_pipeline(hvac):
    run = simulate(demo_spec(hvac), base_dir=OUT_ROOT)
    assert_completed(run)
    s = run.summary()
    print(json.dumps(s, indent=2))
    assert s["energyplus"]["severe"] == 0
    assert s["peak_cooling_kw"] > 0 and s["peak_heating_kw"] > 0
    assert len(run.metrics.zones_sized) == 10
    # ETL: the run and its zone sizing landed in results.db
    row = compare_runs(DB, [run.run_id])[0]
    assert row["peak_cooling_kw"] == s["peak_cooling_kw"]
    zones = run_sql(
        DB,
        "SELECT load_type, COUNT(*) FROM zone_sizing "
        f"WHERE run_id = '{run.run_id}' GROUP BY load_type ORDER BY load_type",
    )
    assert zones["rows"] == [["Cooling", 10], ["Heating", 10]]
    equipment = equipment_sizes(DB, run.run_id)
    print(json.dumps(equipment, indent=1))
    if hvac is HVACSystem.VAV:
        types = {e["component_type"].lower() for e in equipment}
        assert any("chiller" in t for t in types), types
        assert any("boiler" in t for t in types), types
    if hvac is HVACSystem.IDEAL_LOADS:
        assert "cooling_capacity_kw" not in s
    else:
        # equipment also covers ventilation air and fan heat, so it exceeds zone loads
        assert s["cooling_capacity_kw"] > s["peak_cooling_kw"], s
        assert s["heating_capacity_kw"] > 0, s


def test_annual_run_reports_eui_and_unmet_hours():
    run = simulate(demo_spec(HVACSystem.IDEAL_LOADS, run_period="annual"), base_dir=OUT_ROOT)
    assert_completed(run)
    s = run.summary()
    print(json.dumps(s, indent=2))
    assert s["site_eui_kwh_m2"] > 0
    assert "unmet_hours" in s
    # ETL: hourly series, monthly aggregation, end uses, plots
    monthly = timeseries(DB, run.run_id, "DistrictCooling:Facility", freq="monthly")
    print(json.dumps(monthly, indent=1))
    assert [p["month"] for p in monthly] == list(range(1, 13))
    assert max(p["value"] for p in monthly) > 0
    july = next(p["value"] for p in monthly if p["month"] == 7)
    january = next(p["value"] for p in monthly if p["month"] == 1)
    assert july > january  # Chicago: more cooling in July than January
    temps = timeseries(DB, run.run_id, "Zone Mean Air Temperature", freq="annual", key="*")
    assert 15 < temps[0]["value"] < 30
    uses = end_uses(DB, run.run_id)
    print(json.dumps(uses, indent=1))
    assert uses
    png = plot_timeseries(DB, [run.run_id], "DistrictCooling:Facility", OUT_ROOT / "cooling.png")
    assert png.stat().st_size > 1000
    png = plot_metric_comparison(DB, [run.run_id], "site_eui_kwh_m2", OUT_ROOT / "eui.png")
    assert png.stat().st_size > 1000


def test_reference_case_regression():
    """Peak loads for a fixed reference building must not drift.

    Regenerate after an intentional modeling change:
        python scripts/update_reference.py
    """
    if not REFERENCE.exists():
        pytest.fail(f"Missing {REFERENCE.name}; run scripts/update_reference.py")
    ref = json.loads(REFERENCE.read_text())
    run = simulate(BuildingSpec.model_validate(ref["spec"]), base_dir=OUT_ROOT)
    assert_completed(run)
    got = run.summary()
    tol = ref["rel_tolerance"]
    for key, expected in ref["expected"].items():
        assert got[key] == pytest.approx(expected, rel=tol), (
            f"{key}: got {got[key]}, reference {expected} (±{tol:.0%})"
        )


def test_parametric_study_hvac_by_wwr():
    study = ParametricStudy(
        name="CI HVAC x WWR",
        base=BuildingSpec(location="chicago", geometry={"length_m": 30, "width_m": 20}),
        parameters={
            "hvac": ["ideal_loads", "vav_chiller_boiler", "pthp"],
            "geometry.window_to_wall_ratio": [0.2, 0.5],
        },
    )
    result = run_study(study, base_dir=OUT_ROOT, max_workers=2)
    summary = result.summary()
    print(json.dumps(summary, indent=2))
    assert summary["completed"] == 6, summary
    # More glass -> higher peak cooling, averaged over HVAC systems
    wwr = summary["main_effects"]["WWR"]["peak_cooling_kw"]
    assert wwr["50%"] > wwr["20%"]
    # ...and for every HVAC system individually
    rows = study_table(DB, result.study_id)
    for hvac in ("ideal_loads", "vav_chiller_boiler", "pthp"):
        by_wwr = {
            r["window_to_wall_ratio"]: r["peak_cooling_kw"] for r in rows if r["hvac"] == hvac
        }
        assert by_wwr[0.5] > by_wwr[0.2], (hvac, by_wwr)
    # Zone loads don't depend on the HVAC system; installed capacity does.
    capacity = summary["main_effects"]["HVAC"]["cooling_capacity_kw"]
    assert set(capacity) == {"VAV", "PTHP"} and capacity["VAV"] != capacity["PTHP"]
    for metric in ("peak_cooling_kw", "cooling_capacity_kw"):
        png = plot_study(DB, result.study_id, metric, OUT_ROOT / f"study_{metric}.png")
        assert png.stat().st_size > 1000


# ---------- agent and MCP server on real EnergyPlus ----------


def test_agent_drives_a_study_with_scripted_llm(tmp_path):
    from test_agent import ScriptedClient, call, say

    from bem_agent.agent import Agent
    from bem_agent.tools import ToolContext

    client = ScriptedClient(
        [
            call(
                "run_parametric_study",
                {
                    "name": "Agent HVAC study",
                    "base": {"location": "tampa", "floor_area_m2": 800, "num_floors": 2},
                    "parameters": {"hvac": ["vav_chiller_boiler", "pthp"]},
                },
            ),
            say("Study done."),
        ]
    )
    ctx = ToolContext(base_dir=tmp_path, max_simulations=4)
    result = Agent(client, ctx).run("Compare VAV and PTHP for a Tampa office")
    study = result.steps[0].result
    print(json.dumps(study, indent=1)[:3000])
    assert study["completed"] == 2, study
    assert {r["label"] for r in study["table"]} == {"VAV", "PTHP"}
    assert all(r["cooling_capacity_kw"] > 0 for r in study["table"])
    assert ctx.simulations_used == 2
    # the tool result the model saw is compact
    tool_msg = client.requests[1]["messages"][-1].content
    assert len(tool_msg) < 4000


def test_mcp_server_runs_a_simulation(tmp_path):
    import anyio
    from mcp import Client

    from bem_agent.mcp_server import build_server
    from bem_agent.tools import ToolContext

    async def scenario():
        async with Client(build_server(ToolContext(base_dir=tmp_path))) as client:
            r = await client.call_tool(
                "run_simulation", {"location": "golden_co", "floor_area_m2": 500, "hvac": "pthp"}
            )
            assert not r.is_error, r.content[0].text
            summary = json.loads(r.content[0].text)
            assert summary["status"] in ("completed", "completed_with_warnings"), summary
            chart = await client.call_tool(
                "make_chart",
                {"kind": "metric", "run_ids": [summary["run_id"]], "metric": "cooling_capacity_kw"},
            )
            assert chart.content[1].type == "image"

    anyio.run(scenario)


@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set")
def test_live_groq_agent(tmp_path):
    """One real question to the default LLM. Runs only when a Groq key is available."""
    from bem_agent.agent import Agent
    from bem_agent.llm import get_client
    from bem_agent.tools import ToolContext

    agent = Agent(
        get_client("groq"), ToolContext(base_dir=tmp_path, max_simulations=3), max_steps=6
    )
    result = agent.run(
        "Run a sizing simulation for a one-story, 600 m2 office in Chicago with a VAV system. "
        "What is the installed cooling capacity in kW?"
    )
    print(json.dumps(result.to_dict(), indent=1, default=str)[:4000])
    runs = [s for s in result.steps if s.tool == "run_simulation" and "run_id" in s.result]
    assert runs, "the model never ran a simulation"
    capacity = runs[-1].result.get("cooling_capacity_kw")
    assert capacity
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", result.answer.replace(",", ""))]
    # the answer must report the simulated capacity (kW or tons), not an invented one
    assert any(abs(n - c) <= 0.02 * c for n in numbers for c in (capacity, capacity / 3.517)), (
        capacity,
        result.answer,
    )
