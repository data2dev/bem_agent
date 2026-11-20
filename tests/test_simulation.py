"""Real EnergyPlus runs. Need ENERGYPLUS_DIR (CI installs EnergyPlus for these)."""

import json
import os
import shutil
from pathlib import Path

import pytest

from bem_agent.model import CITIES, BuildingSpec, HVACSystem, resolve_weather
from bem_agent.sim import simulate

pytestmark = pytest.mark.energyplus

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "runs" / "ci"
REFERENCE = Path(__file__).parent / "reference" / "office_chicago_ideal.json"


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


def test_annual_run_reports_eui_and_unmet_hours():
    run = simulate(demo_spec(HVACSystem.IDEAL_LOADS, run_period="annual"), base_dir=OUT_ROOT)
    assert_completed(run)
    s = run.summary()
    print(json.dumps(s, indent=2))
    assert s["site_eui_kwh_m2"] > 0
    assert "unmet_hours" in s


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
