"""ETL: EnergyPlus run outputs -> one cleaned SQLite results database.

Extract from each run's eplusout.sql, spec.json and run.json; transform (units to kWh /
kW / W·m⁻², design-day noise dropped, names normalized); load into `results.db`, where
runs can be compared with plain SQL.

Tables:
    runs              one row per run: inputs, status, headline results
    zone_sizing       design heating/cooling load per zone
    equipment_sizing  autosized component capacities (chiller, boiler, coils, fans, ...)
    end_uses          annual energy by end use and fuel (annual runs)
    hourly            hourly meters and zone temperatures (annual runs)
    studies           parametric studies: name and the parameters varied
    study_cases       which run is which case of a study
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

J_PER_KWH = 3.6e6
GJ_TO_KWH = 1e9 / J_PER_KWH

# Hourly series kept from each annual run: (EnergyPlus name, is_meter).
HOURLY_SERIES = [
    ("Electricity:Facility", True),
    ("NaturalGas:Facility", True),
    ("DistrictCooling:Facility", True),
    ("DistrictHeatingWater:Facility", True),
    ("Zone Mean Air Temperature", False),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT, location TEXT, hvac TEXT, run_period TEXT,
    envelope TEXT, loads TEXT, schedules TEXT,
    floor_area_m2 REAL, num_floors INTEGER, length_m REAL, width_m REAL,
    window_to_wall_ratio REAL, orientation_deg REAL,
    status TEXT, elapsed_s REAL, warnings INTEGER, severe INTEGER,
    peak_cooling_kw REAL, peak_heating_kw REAL,
    peak_cooling_w_m2 REAL, peak_heating_w_m2 REAL,
    site_energy_kwh REAL, site_eui_kwh_m2 REAL,
    unmet_heating_h REAL, unmet_cooling_h REAL,
    loaded_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS zone_sizing (
    run_id TEXT, zone TEXT, load_type TEXT,
    design_load_kw REAL, design_day TEXT, peak_time TEXT,
    PRIMARY KEY (run_id, zone, load_type)
);
CREATE TABLE IF NOT EXISTS equipment_sizing (
    run_id TEXT, component_type TEXT, component TEXT, description TEXT,
    value REAL, units TEXT
);
CREATE TABLE IF NOT EXISTS end_uses (
    run_id TEXT, end_use TEXT, fuel TEXT, energy_kwh REAL,
    PRIMARY KEY (run_id, end_use, fuel)
);
CREATE TABLE IF NOT EXISTS hourly (
    run_id TEXT, variable TEXT, key TEXT, units TEXT,
    month INTEGER, day INTEGER, hour INTEGER,  -- hour = hour ending, 1..24
    value REAL
);
CREATE TABLE IF NOT EXISTS studies (
    study_id TEXT PRIMARY KEY, name TEXT, parameters_json TEXT, n_cases INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS study_cases (
    study_id TEXT, case_index INTEGER, run_id TEXT, label TEXT, overrides_json TEXT,
    PRIMARY KEY (study_id, case_index)
);
CREATE INDEX IF NOT EXISTS hourly_lookup ON hourly (run_id, variable, key);
CREATE INDEX IF NOT EXISTS equipment_lookup ON equipment_sizing (run_id);
"""

CHILD_TABLES = ("zone_sizing", "equipment_sizing", "end_uses", "hourly")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


def _source(sql_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{sql_path}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    return {r[0] for r in rows}


def _float(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------- extract + transform ----------


def extract_zone_sizing(src: sqlite3.Connection) -> list[tuple]:
    if "ZoneSizes" not in _tables(src):
        return []
    rows = src.execute(
        "SELECT ZoneName, LoadType, UserDesLoad, DesDayName, PeakHrMin FROM ZoneSizes"
    ).fetchall()
    return [(z, t, round(load / 1000, 3), dd, peak) for z, t, load, dd, peak in rows]


def extract_equipment_sizing(src: sqlite3.Connection) -> list[tuple]:
    if "ComponentSizes" not in _tables(src):
        return []
    rows = src.execute(
        "SELECT CompType, CompName, Description, Value, Units FROM ComponentSizes"
    ).fetchall()
    return [(t, n, d, _float(v), u) for t, n, d, v, u in rows if _float(v) is not None]


def extract_end_uses(src: sqlite3.Connection) -> list[tuple]:
    if "TabularDataWithStrings" not in _tables(src):
        return []
    rows = src.execute(
        "SELECT RowName, ColumnName, Units, Value FROM TabularDataWithStrings "
        "WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary' "
        "AND TableName = 'End Uses' AND RowName NOT IN ('', 'Total End Uses')"
    ).fetchall()
    out = []
    for end_use, fuel, units, value in rows:
        v = _float(value)
        if v is None or v == 0 or units != "GJ" or fuel.lower().startswith("water"):
            continue
        out.append((end_use.strip(), fuel.strip(), round(v * GJ_TO_KWH, 1)))
    return out


def extract_hourly(src: sqlite3.Connection) -> list[tuple]:
    """Hourly values from the weather-file run period only (no warmup, no design days)."""
    needed = {"ReportData", "ReportDataDictionary", "Time", "EnvironmentPeriods"}
    if not needed <= _tables(src):
        return []
    names = [n for n, _ in HOURLY_SERIES]
    placeholders = ",".join("?" * len(names))
    rows = src.execute(
        f"""
        SELECT d.Name, d.KeyValue, d.Units, d.IsMeter, t.Month, t.Day, t.Hour, r.Value
        FROM ReportData r
        JOIN ReportDataDictionary d ON d.ReportDataDictionaryIndex = r.ReportDataDictionaryIndex
        JOIN Time t ON t.TimeIndex = r.TimeIndex
        JOIN EnvironmentPeriods e ON e.EnvironmentPeriodIndex = t.EnvironmentPeriodIndex
        WHERE d.ReportingFrequency = 'Hourly'
          AND e.EnvironmentType = 3
          AND COALESCE(t.WarmupFlag, 0) = 0
          AND d.Name IN ({placeholders})
        """,
        names,
    ).fetchall()
    out = []
    for name, key, units, is_meter, month, day, hour, value in rows:
        if units == "J":
            value, units = value / J_PER_KWH, "kWh"
        key = "Facility" if is_meter else (key or "").upper()
        out.append((name, key, units, month, day, hour, value))
    return out


def run_row(run_dir: Path) -> dict[str, Any]:
    spec = json.loads((run_dir / "spec.json").read_text())
    record = json.loads((run_dir / "run.json").read_text())
    g = spec["geometry"]
    floor_area = g["length_m"] * g["width_m"] * g["num_floors"]
    eui = record.get("site_eui_kwh_m2")
    unmet = record.get("unmet_hours") or {}
    eplus = record.get("energyplus") or {}

    presets = spec.get("presets") or {}
    return {
        "run_id": record["run_id"],
        "name": spec["name"],
        "location": spec["location"],
        "hvac": spec["hvac"],
        "run_period": spec["run_period"],
        "envelope": presets.get("envelope", "custom"),
        "loads": presets.get("loads", "custom"),
        "schedules": presets.get("schedules", "custom"),
        "floor_area_m2": round(floor_area, 1),
        "num_floors": g["num_floors"],
        "length_m": g["length_m"],
        "width_m": g["width_m"],
        "window_to_wall_ratio": g["window_to_wall_ratio"],
        "orientation_deg": g["orientation_deg"],
        "status": record["status"],
        "elapsed_s": eplus.get("elapsed_s"),
        "warnings": eplus.get("warnings"),
        "severe": eplus.get("severe"),
        "peak_cooling_kw": record.get("peak_cooling_kw"),
        "peak_heating_kw": record.get("peak_heating_kw"),
        "peak_cooling_w_m2": record.get("peak_cooling_w_m2"),
        "peak_heating_w_m2": record.get("peak_heating_w_m2"),
        "site_energy_kwh": round(eui * floor_area, 1) if eui is not None else None,
        "site_eui_kwh_m2": eui,
        "unmet_heating_h": unmet.get("heating"),
        "unmet_cooling_h": unmet.get("cooling"),
    }


# ---------- load ----------


def load_run(run_dir: str | Path, db_path: str | Path) -> dict[str, int]:
    """Load one run folder into the results database (replacing any earlier copy).

    Returns row counts per table. Runs that never reached EnergyPlus are still recorded
    in `runs`, so failed and invalid attempts stay visible.
    """
    run_dir = Path(run_dir)
    row = run_row(run_dir)
    run_id = row["run_id"]
    sql_path = run_dir / "eplusout.sql"
    data: dict[str, list[tuple]] = {t: [] for t in CHILD_TABLES}
    if sql_path.exists():
        with closing(_source(sql_path)) as src:
            data["zone_sizing"] = extract_zone_sizing(src)
            data["equipment_sizing"] = extract_equipment_sizing(src)
            data["end_uses"] = extract_end_uses(src)
            data["hourly"] = extract_hourly(src)

    with closing(connect(db_path)) as con, con:
        for table in ("runs", *CHILD_TABLES):
            con.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        cols = ",".join(row)
        con.execute(
            f"INSERT INTO runs ({cols}) VALUES ({','.join('?' * len(row))})",
            list(row.values()),
        )
        inserts = {
            "zone_sizing": "INSERT INTO zone_sizing VALUES (?,?,?,?,?,?)",
            "equipment_sizing": "INSERT INTO equipment_sizing VALUES (?,?,?,?,?,?)",
            "end_uses": "INSERT INTO end_uses VALUES (?,?,?,?)",
            "hourly": "INSERT INTO hourly VALUES (?,?,?,?,?,?,?,?)",
        }
        for table, stmt in inserts.items():
            con.executemany(stmt, [(run_id, *r) for r in data[table]])
    return {"runs": 1, **{t: len(v) for t, v in data.items()}}


def record_study(
    db_path: str | Path,
    study_id: str,
    name: str,
    parameters: dict[str, list[Any]],
    cases: list[tuple[int, str, str, dict[str, Any]]],
) -> None:
    """Record a parametric study; `cases` = (case index, run_id, label, overrides)."""
    with closing(connect(Path(db_path))) as con, con:
        con.execute("DELETE FROM study_cases WHERE study_id = ?", (study_id,))
        con.execute(
            "INSERT OR REPLACE INTO studies (study_id, name, parameters_json, n_cases) "
            "VALUES (?,?,?,?)",
            (study_id, name, json.dumps(parameters), len(cases)),
        )
        con.executemany(
            "INSERT INTO study_cases VALUES (?,?,?,?,?)",
            [(study_id, i, run_id, label, json.dumps(o)) for i, run_id, label, o in cases],
        )
