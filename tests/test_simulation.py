"""Real EnergyPlus runs. Need ENERGYPLUS_DIR (CI installs EnergyPlus for these)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bem_agent.model import CITIES, BuildingSpec, Geometry, HVACSystem, resolve_weather, write_idf

pytestmark = pytest.mark.energyplus

OUT_ROOT = Path(__file__).resolve().parents[1] / "runs" / "ci"


@pytest.fixture(scope="module", autouse=True)
def require_energyplus():
    if not os.environ.get("ENERGYPLUS_DIR") or not shutil.which("energyplus"):
        pytest.skip("EnergyPlus not installed (set ENERGYPLUS_DIR and PATH)")


def run_energyplus(idf: Path, epw: Path, out: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [
            "energyplus",
            "--expandobjects",
            "--weather",
            str(epw),
            "--output-directory",
            str(out),
            str(idf),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    err_file = out / "eplusout.err"
    err = err_file.read_text(errors="replace") if err_file.exists() else proc.stdout + proc.stderr
    return proc.returncode, err


def severe_count(err: str) -> int:
    m = re.search(r"Completed Successfully--\s*\d+ Warning;\s*(\d+) Severe Errors", err)
    return int(m.group(1)) if m else -1


@pytest.mark.parametrize("city", sorted(CITIES))
def test_demo_city_weather_files_exist(city):
    weather = resolve_weather(city)
    assert len(weather.site_objects()) == 3


@pytest.mark.parametrize("hvac", list(HVACSystem))
def test_sizing_run_completes(hvac):
    spec = BuildingSpec(
        name=f"CI {hvac.value}",
        location="chicago",
        geometry=Geometry(length_m=40, width_m=25, num_floors=2, window_to_wall_ratio=0.4),
        hvac=hvac,
    )
    out = OUT_ROOT / hvac.value
    shutil.rmtree(out, ignore_errors=True)
    idf = write_idf(spec, out / "in.idf")
    code, err = run_energyplus(idf, resolve_weather("chicago").epw, out)
    severe = [line for line in err.splitlines() if "** Severe" in line or "**  Fatal" in line]
    assert code == 0 and severe_count(err) == 0, "\n".join(severe[:40]) or err[-4000:]
    assert (out / "eplusout.sql").exists()
