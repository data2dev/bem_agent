"""Read results from EnergyPlus's SQLite output (eplusout.sql)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ZoneSizing:
    zone: str
    load_type: str  # "Cooling" | "Heating"
    calc_load_w: float  # calculated design load
    user_load_w: float  # design load after sizing factor
    design_day: str
    peak_time: str


def _connect(sql_path: Path) -> sqlite3.Connection:
    if not Path(sql_path).is_file():
        raise FileNotFoundError(sql_path)
    return sqlite3.connect(f"file:{sql_path}?mode=ro", uri=True)


def zone_sizing(sql_path: Path) -> list[ZoneSizing]:
    with closing(_connect(sql_path)) as con:
        rows = con.execute(
            "SELECT ZoneName, LoadType, CalcDesLoad, UserDesLoad, DesDayName, PeakHrMin "
            "FROM ZoneSizes ORDER BY ZoneName, LoadType"
        ).fetchall()
    return [ZoneSizing(r[0], r[1], float(r[2]), float(r[3]), r[4] or "", r[5] or "") for r in rows]


def tabular_value(sql_path: Path, report: str, table: str, row: str, column: str) -> str | None:
    """One cell from the tabular (summary) reports, matched case-insensitively."""
    with closing(_connect(sql_path)) as con:
        r = con.execute(
            "SELECT Value FROM TabularDataWithStrings WHERE ReportName = ? COLLATE NOCASE "
            "AND TableName = ? COLLATE NOCASE AND RowName = ? COLLATE NOCASE "
            "AND ColumnName = ? COLLATE NOCASE LIMIT 1",
            (report, table, row, column),
        ).fetchone()
    return r[0].strip() if r and r[0] is not None else None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def unmet_hours(sql_path: Path) -> tuple[float, float] | None:
    """Facility hours with heating / cooling setpoint not met (annual runs)."""
    heat = _float(
        tabular_value(
            sql_path, "SystemSummary", "Time Setpoint Not Met", "Facility", "During Heating"
        )
    )
    cool = _float(
        tabular_value(
            sql_path, "SystemSummary", "Time Setpoint Not Met", "Facility", "During Cooling"
        )
    )
    return None if heat is None or cool is None else (heat, cool)


def site_energy_gj(sql_path: Path) -> float | None:
    """Total annual site energy in GJ (annual runs)."""
    return _float(
        tabular_value(
            sql_path,
            "AnnualBuildingUtilityPerformanceSummary",
            "Site and Source Energy",
            "Total Site Energy",
            "Total Energy",
        )
    )
