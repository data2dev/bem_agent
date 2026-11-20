"""Query the results database: compare runs, aggregate time series, read-only SQL."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

Freq = Literal["hourly", "daily", "monthly", "annual"]
Agg = Literal["sum", "mean", "max", "min"]

COMPARE_COLUMNS = [
    "run_id",
    "name",
    "location",
    "hvac",
    "envelope",
    "window_to_wall_ratio",
    "orientation_deg",
    "floor_area_m2",
    "status",
    "peak_cooling_kw",
    "peak_heating_kw",
    "peak_cooling_w_m2",
    "peak_heating_w_m2",
    "site_eui_kwh_m2",
    "unmet_heating_h",
    "unmet_cooling_h",
]
MAX_ROWS = 200


class QueryError(ValueError):
    pass


def _ro(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).is_file():
        raise QueryError(f"No results database at {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def list_runs(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    with closing(_ro(db_path)) as con:
        return _rows(
            con.execute(
                "SELECT run_id, name, location, hvac, run_period, status, loaded_at "
                "FROM runs ORDER BY loaded_at DESC LIMIT ?",
                (limit,),
            )
        )


def compare_runs(
    db_path: Path, run_ids: list[str], sort_by: str | None = None
) -> list[dict[str, Any]]:
    """Headline inputs and results side by side, one dict per run."""
    if not run_ids:
        raise QueryError("Give at least one run_id")
    if sort_by and sort_by not in COMPARE_COLUMNS:
        raise QueryError(f"sort_by must be one of {COMPARE_COLUMNS}")
    marks = ",".join("?" * len(run_ids))
    order = f" ORDER BY {sort_by}" if sort_by else ""
    with closing(_ro(db_path)) as con:
        rows = _rows(
            con.execute(
                f"SELECT {','.join(COMPARE_COLUMNS)} FROM runs WHERE run_id IN ({marks}){order}",
                run_ids,
            )
        )
    missing = set(run_ids) - {r["run_id"] for r in rows}
    if missing:
        raise QueryError(f"Unknown run_id(s): {', '.join(sorted(missing))}")
    if not sort_by:
        rows.sort(key=lambda r: run_ids.index(r["run_id"]))
    return rows


def equipment_sizes(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    with closing(_ro(db_path)) as con:
        return _rows(
            con.execute(
                "SELECT component_type, component, description, value, units "
                "FROM equipment_sizing WHERE run_id = ? ORDER BY component_type, component",
                (run_id,),
            )
        )


def end_uses(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    with closing(_ro(db_path)) as con:
        return _rows(
            con.execute(
                "SELECT end_use, fuel, energy_kwh FROM end_uses WHERE run_id = ? "
                "ORDER BY energy_kwh DESC",
                (run_id,),
            )
        )


_GROUP = {
    "hourly": ("month, day, hour", "month, day, hour"),
    "daily": ("month, day", "month, day"),
    "monthly": ("month", "month"),
    "annual": ("1 AS year", "year"),
}


def timeseries(
    db_path: Path,
    run_id: str,
    variable: str,
    freq: Freq = "monthly",
    agg: Agg | None = None,
    key: str = "Facility",
) -> list[dict[str, Any]]:
    """Aggregate an hourly series. Energy (kWh) defaults to sum, temperatures to mean.

    For zone variables pass the zone name as `key`, or "*" to average across zones.
    """
    if freq not in _GROUP:
        raise QueryError(f"freq must be one of {list(_GROUP)}")
    with closing(_ro(db_path)) as con:
        units_row = con.execute(
            "SELECT units FROM hourly WHERE run_id = ? AND variable = ? LIMIT 1",
            (run_id, variable),
        ).fetchone()
        if units_row is None:
            raise QueryError(
                f"No hourly data for {variable!r} in run {run_id} (only annual runs have it)"
            )
        units = units_row[0]
        agg = agg or ("sum" if units == "kWh" else "mean")
        if agg not in ("sum", "mean", "max", "min"):
            raise QueryError("agg must be sum, mean, max or min")
        select, group = _GROUP[freq]
        sql_agg = "AVG" if agg == "mean" else agg.upper()
        key_clause, params = ("", [run_id, variable])
        if key != "*":
            key_clause = " AND key = ?"
            params.append(key.upper() if key != "Facility" else key)
        inner_group = "month, day, hour"
        # Average across zones first when key="*", so sums are per-hour means.
        rows = _rows(
            con.execute(
                f"""
                SELECT {select}, {sql_agg}(v) AS value FROM (
                    SELECT month, day, hour, AVG(value) AS v FROM hourly
                    WHERE run_id = ? AND variable = ?{key_clause}
                    GROUP BY {inner_group}
                ) GROUP BY {group} ORDER BY {group}
                """,
                params,
            )
        )
    for r in rows:
        r["value"] = round(r["value"], 3)
        r["units"] = units
        r.pop("year", None)
    return rows


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


def run_sql(db_path: Path, sql: str, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Run one read-only SELECT for the agent. Returns columns, rows, and truncation flag.

    Defense in depth: read-only connection with query_only, a single statement that
    must start with SELECT/WITH, no write/DDL keywords, and a row cap.
    """
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise QueryError("Only one statement is allowed")
    if not re.match(r"^(select|with)\b", statement, re.IGNORECASE):
        raise QueryError("Only SELECT queries are allowed")
    if _FORBIDDEN.search(statement):
        raise QueryError("Query contains a forbidden keyword")
    with closing(_ro(db_path)) as con:
        try:
            cur = con.execute(statement)
        except sqlite3.Error as exc:
            raise QueryError(f"SQL error: {exc}") from exc
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    return {
        "columns": columns,
        "rows": [list(r) for r in rows[:max_rows]],
        "truncated": truncated,
    }


def schema_description() -> str:
    """Short description of the results tables, for the agent's tool docs."""
    return (
        "runs(run_id, name, location, hvac, run_period, envelope, loads, schedules, "
        "floor_area_m2, num_floors, window_to_wall_ratio, orientation_deg, status, "
        "peak_cooling_kw, peak_heating_kw, peak_cooling_w_m2, peak_heating_w_m2, "
        "site_energy_kwh, site_eui_kwh_m2, unmet_heating_h, unmet_cooling_h); "
        "zone_sizing(run_id, zone, load_type, design_load_kw, design_day, peak_time); "
        "equipment_sizing(run_id, component_type, component, description, value, units); "
        "end_uses(run_id, end_use, fuel, energy_kwh); "
        "hourly(run_id, variable, key, units, month, day, hour, value)"
    )
