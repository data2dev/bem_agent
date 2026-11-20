import json
import sqlite3
from pathlib import Path

import pytest

from bem_agent.model import BuildingSpec, Geometry, OperatingHours
from bem_agent.results import sql
from bem_agent.sim import parse_err, simulate
from bem_agent.sim.runner import RunResult
from bem_agent.validation import RunMetrics, check_metrics, check_run, validate_spec

ERR_OK = """Program Version,EnergyPlus, Version 25.1.0
   ** Warning ** GetSurfaceData: Very small surface area
   **   ~~~   ** Surface = F1_SOUTH_WALL2
   **   ~~~   ** Area = 0.01
   ** Warning ** CheckUsedConstructions: 1 construction is not used.
   ************* EnergyPlus Completed Successfully-- 2 Warning; 0 Severe Errors; Elapsed Time=00hr 00min  2.10sec
"""

ERR_FATAL = """   ** Severe  ** <root>[Zone] - Missing required property 'name'.
   ** Severe  ** GetZoneData: bad zone
   **  Fatal  ** Errors occurred on processing input file. Preceding condition(s) cause termination.
   ************* EnergyPlus Terminated--Fatal Error Detected. 0 Warning; 2 Severe Errors; Elapsed Time=00hr 00min  0.20sec
"""


# ---------- .err parsing ----------


def test_parse_err_success():
    s = parse_err(ERR_OK)
    assert s.completed and not s.fatal
    assert (s.warnings, s.severe) == (2, 0)
    assert len(s.messages) == 2
    assert "F1_SOUTH_WALL2" in s.messages[0].text


def test_parse_err_fatal():
    s = parse_err(ERR_FATAL)
    assert s.fatal and not s.completed
    assert s.severe == 2
    assert [m.level for m in s.messages] == ["Severe", "Severe", "Fatal"]


def test_parse_err_without_summary_line():
    s = parse_err("   ** Severe  ** something broke\n")
    assert not s.found_summary and not s.completed
    assert s.severe == 1


# ---------- pre-run checks ----------


def codes(spec: BuildingSpec) -> list[str]:
    return validate_spec(spec, check_weather=False).codes()


def test_default_spec_is_clean():
    report = validate_spec(BuildingSpec(), check_weather=False)
    assert report.ok
    assert not report.warnings


def test_setpoint_errors():
    spec = BuildingSpec(schedules=OperatingHours(heating_c=(23.0, 15.0), cooling_c=(23.5, 29.0)))
    assert "SETPOINT_DEADBAND" in codes(spec)
    spec = BuildingSpec(schedules=OperatingHours(heating_c=(21.0, 22.0)))
    assert "HEATING_SETBACK" in codes(spec)
    spec = BuildingSpec(schedules=OperatingHours(cooling_c=(24.0, 22.0)))
    assert "COOLING_SETBACK" in codes(spec)


def test_geometry_and_load_warnings():
    spec = BuildingSpec(
        geometry=Geometry(length_m=120, width_m=12, window_to_wall_ratio=0.7),
        loads={"lighting_w_per_m2": 20.0, "equipment_w_per_m2": 50.0, "people_per_m2": 0.6},
    )
    c = codes(spec)
    for code in ["ASPECT_RATIO", "HIGH_WWR", "HIGH_LPD", "HIGH_EPD", "HIGH_OCCUPANCY"]:
        assert code in c
    assert validate_spec(spec, check_weather=False).ok  # warnings don't block a run


def test_weather_error(monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    report = validate_spec(BuildingSpec())
    assert not report.ok
    assert report.codes() == ["WEATHER"]


# ---------- post-run checks ----------


def run_result(tmp_path: Path, err: str, returncode=0, timed_out=False) -> RunResult:
    return RunResult(tmp_path, returncode, 1.0, timed_out, parse_err(err))


def test_check_run(tmp_path):
    (tmp_path / "eplusout.sql").write_text("")
    assert check_run(run_result(tmp_path, ERR_OK)).ok
    failed = check_run(run_result(tmp_path, ERR_FATAL, returncode=1))
    assert failed.codes() == ["SIMULATION_FAILED"]
    assert "bad zone" in failed.issues[0].message
    assert check_run(run_result(tmp_path, "", returncode=None, timed_out=True)).codes() == [
        "TIMEOUT"
    ]


def metrics(**kw) -> RunMetrics:
    base = dict(
        zones_expected=["A", "B"],
        zones_sized={
            "A": {"Cooling": 30000.0, "Heating": 20000.0},
            "B": {"Cooling": 30000.0, "Heating": 20000.0},
        },
        floor_area_m2=1000.0,
    )
    base.update(kw)
    return RunMetrics(**base)


def test_check_metrics_plausible():
    assert not check_metrics(metrics()).issues  # 60 W/m² cooling, 40 W/m² heating


def test_check_metrics_flags_problems():
    r = check_metrics(
        metrics(
            zones_sized={"A": {"Cooling": 1_000_000.0, "Heating": 0.0}},
            eui_kwh_m2=2000.0,
            unmet_hours=(250.0, 120.0),
        )
    )
    assert set(r.codes()) == {
        "ZONES_NOT_SIZED",
        "COOLING_DENSITY",
        "HEATING_DENSITY",
        "EUI_RANGE",
        "UNMET_HOURS",
    }
    assert not r.ok  # a missing zone is an error


# ---------- SQL reader (synthetic database with EnergyPlus's table layout) ----------


def make_sql(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ZoneSizes (ZoneName TEXT, LoadType TEXT, CalcDesLoad REAL, "
        "UserDesLoad REAL, DesDayName TEXT, PeakHrMin TEXT)"
    )
    con.executemany(
        "INSERT INTO ZoneSizes VALUES (?,?,?,?,?,?)",
        [
            ("F1_CORE", "Cooling", 9000.0, 10000.0, "CLG DD", "7/21 15:00:00"),
            ("F1_CORE", "Heating", 4000.0, 5000.0, "HTG DD", "1/21 06:00:00"),
        ],
    )
    con.execute(
        "CREATE TABLE TabularDataWithStrings (ReportName TEXT, TableName TEXT, RowName TEXT, "
        "ColumnName TEXT, Value TEXT)"
    )
    con.executemany(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?)",
        [
            ("SystemSummary", "Time Setpoint Not Met", "Facility", "During Heating", "  12.50"),
            ("SystemSummary", "Time Setpoint Not Met", "Facility", "During Cooling", "3.00"),
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Site and Source Energy",
                "Total Site Energy",
                "Total Energy",
                "   720.00",
            ),
        ],
    )
    con.commit()
    con.close()
    return path


def test_sql_reader(tmp_path):
    db = make_sql(tmp_path / "eplusout.sql")
    rows = sql.zone_sizing(db)
    assert [(r.zone, r.load_type, r.user_load_w) for r in rows] == [
        ("F1_CORE", "Cooling", 10000.0),
        ("F1_CORE", "Heating", 5000.0),
    ]
    assert sql.unmet_hours(db) == (12.5, 3.0)
    assert sql.site_energy_gj(db) == 720.0
    with pytest.raises(FileNotFoundError):
        sql.zone_sizing(tmp_path / "missing.sql")


# ---------- pipeline (invalid input never reaches EnergyPlus) ----------


def test_simulate_stops_on_invalid_input(tmp_path, monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    spec = BuildingSpec(name="Bad Setpoints", schedules=OperatingHours(heating_c=(24.0, 15.0)))
    result = simulate(spec, base_dir=tmp_path)
    assert result.status == "invalid"
    assert result.run is None
    saved = json.loads((result.out_dir / "run.json").read_text())
    assert saved["status"] == "invalid"
    assert any("SETPOINT_DEADBAND" in i for i in saved["issues"])
    assert (result.out_dir / "spec.json").exists()
