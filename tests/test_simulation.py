"""Real EnergyPlus runs. Need ENERGYPLUS_DIR (CI installs EnergyPlus for these)."""

import json
import os
import shutil
from pathlib import Path

import pytest

from bem_agent.model import CITIES, BuildingSpec, HVACSystem, resolve_weather
from bem_agent.results import compare_runs, end_uses, equipment_sizes, run_sql, timeseries
from bem_agent.results.plots import plot_metric_comparison, plot_timeseries
from bem_agent.sim import simulate

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
    print(json.dumps(equipment[:15], indent=1))
    if hvac is HVACSystem.VAV:
        types = {e["component_type"].lower() for e in equipment}
        assert any("chiller" in t for t in types), types
        assert any("boiler" in t for t in types), types


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
