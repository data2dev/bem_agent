import json
import sqlite3
from pathlib import Path

import pytest

from bem_agent.model import BuildingSpec
from bem_agent.results import (
    QueryError,
    compare_runs,
    end_uses,
    equipment_sizes,
    list_runs,
    load_run,
    run_sql,
    timeseries,
)
from bem_agent.results.plots import plot_metric_comparison, plot_timeseries

J_PER_KWH = 3.6e6


def fake_eplus_sql(path: Path, scale: float = 1.0) -> None:
    """A small eplusout.sql with EnergyPlus's table layout and one week of hourly data."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE ZoneSizes (ZoneName TEXT, LoadType TEXT, CalcDesLoad REAL,
            UserDesLoad REAL, DesDayName TEXT, PeakHrMin TEXT);
        CREATE TABLE ComponentSizes (CompType TEXT, CompName TEXT, Description TEXT,
            Value REAL, Units TEXT);
        CREATE TABLE TabularDataWithStrings (ReportName TEXT, TableName TEXT, RowName TEXT,
            ColumnName TEXT, Units TEXT, Value TEXT);
        CREATE TABLE EnvironmentPeriods (EnvironmentPeriodIndex INTEGER, SimulationIndex INTEGER,
            EnvironmentName TEXT, EnvironmentType INTEGER);
        CREATE TABLE Time (TimeIndex INTEGER, Month INTEGER, Day INTEGER, Hour INTEGER,
            EnvironmentPeriodIndex INTEGER, WarmupFlag INTEGER);
        CREATE TABLE ReportDataDictionary (ReportDataDictionaryIndex INTEGER, IsMeter INTEGER,
            KeyValue TEXT, Name TEXT, ReportingFrequency TEXT, Units TEXT);
        CREATE TABLE ReportData (ReportDataIndex INTEGER, TimeIndex INTEGER,
            ReportDataDictionaryIndex INTEGER, Value REAL);
        """
    )
    con.executemany(
        "INSERT INTO ZoneSizes VALUES (?,?,?,?,?,?)",
        [
            ("F1_CORE", "Cooling", 9000, 10000 * scale, "CLG DD", "7/21 15:00:00"),
            ("F1_CORE", "Heating", 4000, 5000 * scale, "HTG DD", "1/21 06:00:00"),
        ],
    )
    con.executemany(
        "INSERT INTO ComponentSizes VALUES (?,?,?,?,?)",
        [
            (
                "Chiller:Electric:EIR",
                "CHILLER",
                "Design Size Reference Capacity",
                120000 * scale,
                "W",
            ),
            ("Boiler:HotWater", "BOILER", "Design Size Nominal Capacity", 90000 * scale, "W"),
        ],
    )
    abups = "AnnualBuildingUtilityPerformanceSummary"
    con.executemany(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?)",
        [
            (abups, "End Uses", "Cooling", "Electricity", "GJ", f"{36.0 * scale:.2f}"),
            (abups, "End Uses", "Heating", "Natural Gas", "GJ", "72.00"),
            (abups, "End Uses", "Interior Lighting", "Electricity", "GJ", "18.00"),
            (abups, "End Uses", "Fans", "Natural Gas", "GJ", "0.00"),
            (abups, "End Uses", "Total End Uses", "Electricity", "GJ", "54.00"),
        ],
    )
    con.executemany(
        "INSERT INTO EnvironmentPeriods VALUES (?,?,?,?)",
        [(1, 1, "CLG DD", 1), (2, 1, "ANNUAL", 3)],
    )
    con.executemany(
        "INSERT INTO ReportDataDictionary VALUES (?,?,?,?,?,?)",
        [
            (1, 1, "", "Electricity:Facility", "Hourly", "J"),
            (2, 0, "F1_Core", "Zone Mean Air Temperature", "Hourly", "C"),
            (3, 0, "F1_South", "Zone Mean Air Temperature", "Hourly", "C"),
            (4, 1, "", "Water:Facility", "Hourly", "m3"),  # not kept
        ],
    )
    times, data, t, rd = [], [], 0, 0
    # A design-day hour and a warmup hour that must be excluded.
    for env, warm in [(1, 0), (2, 1)]:
        t += 1
        times.append((t, 7, 21, 15, env, warm))
        rd += 1
        data.append((rd, t, 1, 999 * J_PER_KWH))
    for month, day in [(1, 1), (1, 2), (2, 1)]:
        for hour in range(1, 25):
            t += 1
            times.append((t, month, day, hour, 2, 0))
            for dict_idx, value in [
                (1, 10 * scale * J_PER_KWH),
                (2, 20.0 + hour / 24),
                (3, 22.0 + hour / 24),
                (4, 1.0),
            ]:
                rd += 1
                data.append((rd, t, dict_idx, value))
    con.executemany("INSERT INTO Time VALUES (?,?,?,?,?,?)", times)
    con.executemany("INSERT INTO ReportData VALUES (?,?,?,?)", data)
    con.commit()
    con.close()


def make_run(base: Path, run_id: str, name: str, scale: float = 1.0, wwr: float = 0.3) -> Path:
    run_dir = base / run_id
    run_dir.mkdir(parents=True)
    spec = BuildingSpec(
        name=name, run_period="annual", geometry={"window_to_wall_ratio": wwr}, envelope="efficient"
    )
    (run_dir / "spec.json").write_text(spec.model_dump_json())
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "completed",
                "energyplus": {"elapsed_s": 3.2, "warnings": 4, "severe": 0},
                "peak_cooling_kw": 10.0 * scale,
                "peak_heating_kw": 5.0,
                "peak_cooling_w_m2": 16.7 * scale,
                "peak_heating_w_m2": 8.3,
                "site_eui_kwh_m2": 120.0 * scale,
                "unmet_hours": {"heating": 1.0, "cooling": 2.0},
            }
        )
    )
    fake_eplus_sql(run_dir / "eplusout.sql", scale)
    return run_dir


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "results.db"
    counts = load_run(make_run(tmp_path, "run_a", "WWR 30%"), db_path)
    load_run(make_run(tmp_path, "run_b", "WWR 50%", scale=1.5, wwr=0.5), db_path)
    assert counts == {
        "runs": 1,
        "zone_sizing": 2,
        "equipment_sizing": 2,
        "end_uses": 3,
        "hourly": 3 * 72,
    }
    return db_path


# ---------- ETL ----------


def test_etl_transforms(db):
    con = sqlite3.connect(db)
    run = con.execute(
        "SELECT envelope, floor_area_m2, site_energy_kwh, window_to_wall_ratio FROM runs WHERE run_id='run_a'"
    ).fetchone()
    assert run == ("efficient", 600.0, 72000.0, 0.3)
    assert con.execute(
        "SELECT design_load_kw FROM zone_sizing WHERE run_id='run_a' AND load_type='Cooling'"
    ).fetchone() == (10.0,)
    # 36 GJ -> 10,000 kWh; zero rows and totals dropped
    assert dict(
        con.execute("SELECT end_use, energy_kwh FROM end_uses WHERE run_id='run_a'").fetchall()
    ) == {
        "Cooling": 10000.0,
        "Heating": 20000.0,
        "Interior Lighting": 5000.0,
    }
    # J -> kWh, design days and warmup excluded, zone keys uppercased
    hourly = con.execute(
        "SELECT MAX(value), units, COUNT(*) FROM hourly WHERE run_id='run_a' AND variable='Electricity:Facility'"
    ).fetchone()
    assert hourly == (pytest.approx(10.0), "kWh", 72)
    keys = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT key FROM hourly WHERE variable='Zone Mean Air Temperature'"
        )
    }
    assert keys == {"F1_CORE", "F1_SOUTH"}


def test_reload_replaces_rows(tmp_path, db):
    load_run(tmp_path / "run_a", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone() == (2,)
    assert con.execute("SELECT COUNT(*) FROM hourly WHERE run_id='run_a'").fetchone() == (216,)


def test_failed_run_without_sql_is_recorded(tmp_path, db):
    run_dir = make_run(tmp_path, "run_bad", "Bad")
    (run_dir / "eplusout.sql").unlink()
    counts = load_run(run_dir, db)
    assert counts["hourly"] == 0 and counts["runs"] == 1


# ---------- queries ----------


def test_compare_runs(db):
    rows = compare_runs(db, ["run_b", "run_a"])
    assert [r["run_id"] for r in rows] == ["run_b", "run_a"]  # caller's order kept
    assert rows[0]["peak_cooling_kw"] == 15.0
    sorted_rows = compare_runs(db, ["run_b", "run_a"], sort_by="site_eui_kwh_m2")
    assert [r["run_id"] for r in sorted_rows] == ["run_a", "run_b"]
    with pytest.raises(QueryError, match="Unknown"):
        compare_runs(db, ["nope"])
    with pytest.raises(QueryError):
        compare_runs(db, ["run_a"], sort_by="run_id; DROP TABLE runs")


def test_lists_and_details(db):
    assert {r["run_id"] for r in list_runs(db)} == {"run_a", "run_b"}
    assert equipment_sizes(db, "run_a")[0]["component"] == "BOILER"
    assert end_uses(db, "run_a")[0]["end_use"] == "Heating"


def test_timeseries_aggregation(db):
    monthly = timeseries(db, "run_a", "Electricity:Facility", freq="monthly")
    assert [(p["month"], p["value"]) for p in monthly] == [(1, 480.0), (2, 240.0)]
    assert monthly[0]["units"] == "kWh"
    daily = timeseries(db, "run_a", "Electricity:Facility", freq="daily", agg="max")
    assert len(daily) == 3 and daily[0]["value"] == 10.0
    temps = timeseries(db, "run_a", "Zone Mean Air Temperature", freq="annual", key="*")
    expected = sum(21.0 + h / 24 for h in range(1, 25)) / 24  # mean of the two zones
    assert temps[0]["value"] == pytest.approx(expected, abs=1e-3)
    core = timeseries(db, "run_a", "Zone Mean Air Temperature", freq="annual", key="f1_core")
    assert core[0]["value"] < temps[0]["value"]
    with pytest.raises(QueryError, match="No hourly data"):
        timeseries(db, "run_a", "Not:A:Meter")


def test_run_sql_is_read_only(db):
    result = run_sql(db, "SELECT name, site_eui_kwh_m2 FROM runs ORDER BY site_eui_kwh_m2")
    assert result["columns"] == ["name", "site_eui_kwh_m2"]
    assert result["rows"] == [["WWR 30%", 120.0], ["WWR 50%", 180.0]]
    capped = run_sql(db, "SELECT * FROM hourly", max_rows=10)
    assert len(capped["rows"]) == 10 and capped["truncated"]
    for bad in [
        "DELETE FROM runs",
        "SELECT 1; DROP TABLE runs",
        "WITH x AS (SELECT 1) DELETE FROM runs",
        "PRAGMA table_info(runs)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
    ]:
        with pytest.raises(QueryError):
            run_sql(db, bad)
    with pytest.raises(QueryError, match="SQL error"):
        run_sql(db, "SELECT nope FROM runs")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM runs").fetchone() == (2,)


# ---------- plots ----------


def test_plots_write_png_and_csv(db, tmp_path):
    bar = plot_metric_comparison(db, ["run_a", "run_b"], "peak_cooling_kw", tmp_path / "peaks.png")
    line = plot_timeseries(db, ["run_a", "run_b"], "Electricity:Facility", tmp_path / "elec.png")
    for path in (bar, line):
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert path.with_suffix(".csv").exists()
    assert "WWR 50%" in (tmp_path / "elec.csv").read_text()
    with pytest.raises(QueryError):
        plot_metric_comparison(db, ["run_a"], "not_a_metric", tmp_path / "x.png")
