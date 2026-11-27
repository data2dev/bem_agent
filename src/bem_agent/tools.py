"""The agent's tools: one registry shared by the MCP server and the built-in agent loop.

Each tool has a compact JSON schema (sent to the LLM on every call, so it is kept
small) and returns a compact JSON result. Errors come back as {"error": "..."} so the
model can correct itself instead of the session crashing.

Guardrails enforced here, not left to the model:
- only these tools exist; every input is validated (pydantic) before anything runs
- simulations per session are capped (BEM_MAX_SIMULATIONS, default 60)
- SQL is read-only; results and charts stay inside the runs directory
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import storage
from .llm import ToolSpec
from .model import (
    CITIES,
    ENVELOPE_PRESETS,
    LOAD_PRESETS,
    SCHEDULE_PRESETS,
    BuildingSpec,
    Geometry,
    HVACSystem,
)
from .results import (
    QueryError,
    compare_runs,
    end_uses,
    equipment_sizes,
    list_runs,
    list_studies,
    run_sql,
    schema_description,
    study_table,
    timeseries,
)
from .results.plots import METRIC_LABELS, plot_metric_comparison, plot_study, plot_timeseries
from .sim import ParametricStudy, StudyError, run_study, simulate
from .sim.parametric import MAX_CASES, PARAMETERS
from .sim.pipeline import results_db_path, runs_dir
from .units import with_ip_units

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000
HOURLY_VARIABLES = [
    "Electricity:Facility",
    "NaturalGas:Facility",
    "DistrictCooling:Facility",
    "DistrictHeatingWater:Facility",
    "Zone Mean Air Temperature",
]
SORTABLE = [
    "peak_cooling_kw",
    "peak_heating_kw",
    "cooling_capacity_kw",
    "heating_capacity_kw",
    "site_eui_kwh_m2",
]


class ToolError(ValueError):
    """A problem the model can fix (bad input, unknown id, budget exceeded)."""


# ---------- building schema (flat, shared by several tools) ----------

OVERRIDE_KEYS = {
    "lighting_w_per_m2": "loads",
    "equipment_w_per_m2": "loads",
    "people_per_m2": "loads",
    "window_u_si": "envelope",
    "window_shgc": "envelope",
    "wall_insulation_r_si": "envelope",
    "roof_insulation_r_si": "envelope",
    "heating_setpoint_c": "schedules",
    "cooling_setpoint_c": "schedules",
    "weekday_hours": "schedules",
}

BUILDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short label"},
        "location": {"type": "string", "enum": sorted(CITIES)},
        "floor_area_m2": {
            "type": "number",
            "description": "Total floor area; sets the footprint with aspect_ratio. "
            "Or give length_m and width_m",
        },
        "aspect_ratio": {"type": "number", "description": "length/width, default 1.5"},
        "length_m": {"type": "number"},
        "width_m": {"type": "number"},
        "num_floors": {"type": "integer", "minimum": 1, "maximum": 20},
        "floor_to_floor_m": {"type": "number", "description": "default 4.0"},
        "window_to_wall_ratio": {"type": "number", "minimum": 0, "maximum": 0.8},
        "orientation_deg": {"type": "number", "description": "clockwise from north"},
        "envelope": {"type": "string", "enum": sorted(ENVELOPE_PRESETS)},
        "loads": {"type": "string", "enum": sorted(LOAD_PRESETS)},
        "schedules": {"type": "string", "enum": sorted(SCHEDULE_PRESETS)},
        "hvac": {"type": "string", "enum": [h.value for h in HVACSystem]},
        "run_period": {
            "type": "string",
            "enum": ["sizing_only", "annual"],
            "description": "annual (slower) adds energy use, EUI, unmet hours, hourly data",
        },
        "overrides": {
            "type": "object",
            "description": "Optional custom values: "
            + ", ".join(OVERRIDE_KEYS)
            + " (weekday_hours = [start, end])",
        },
    },
    "required": ["location"],
}


LOCATION_ALIASES = {
    "washington": "washington_dc",
    "dc": "washington_dc",
    "washington_d_c": "washington_dc",
    "sterling_va": "washington_dc",
    "sf": "san_francisco",
    "golden": "golden_co",
    "golden_colorado": "golden_co",
    "chicago_il": "chicago",
    "tampa_fl": "tampa",
}


def normalize_location(value: str) -> str:
    """'Washington DC' -> 'washington_dc', 'San Francisco, CA' -> 'san_francisco'."""
    key = re.sub(r"[^a-z]+", "_", value.lower()).strip("_")
    if key in CITIES:
        return key
    if key in LOCATION_ALIASES:
        return LOCATION_ALIASES[key]
    for city in CITIES:  # "san_francisco_ca", "chicago_illinois", ...
        if key.startswith(city + "_"):
            return city
    return key


def spec_from_args(args: dict[str, Any]) -> BuildingSpec:
    """Turn the flat tool arguments into a validated BuildingSpec."""
    args = dict(args or {})
    overrides = args.pop("overrides", None) or {}
    unknown = set(args) - set(BUILDING_SCHEMA["properties"])
    if unknown:
        raise ToolError(f"Unknown building fields: {sorted(unknown)}")
    location = normalize_location(str(args.get("location", "")))
    if location not in CITIES:
        # Tools only accept bundled weather files: never paths chosen by the model.
        raise ToolError(f"Unknown location {args.get('location')!r}; choose from {sorted(CITIES)}")
    args["location"] = location
    bad = set(overrides) - set(OVERRIDE_KEYS)
    if bad:
        raise ToolError(f"Unknown overrides {sorted(bad)}; allowed: {list(OVERRIDE_KEYS)}")

    geometry: dict[str, Any] = {
        k: args.pop(k)
        for k in (
            "length_m",
            "width_m",
            "num_floors",
            "floor_to_floor_m",
            "window_to_wall_ratio",
            "orientation_deg",
        )
        if k in args
    }
    area = args.pop("floor_area_m2", None)
    aspect = args.pop("aspect_ratio", 1.5)
    if area is not None:
        if "length_m" in geometry or "width_m" in geometry:
            raise ToolError("Give floor_area_m2 or length_m/width_m, not both")
        floors = geometry.pop("num_floors", 1)
        try:
            geometry = Geometry.from_floor_area(
                float(area), num_floors=floors, aspect_ratio=float(aspect), **geometry
            ).model_dump()
        except (ValueError, ValidationError) as exc:
            raise ToolError(_short_error(exc)) from exc
    data: dict[str, Any] = {**args, "geometry": geometry}

    # Overrides: start from the chosen (or default) preset and change single values.
    parts = {"envelope": ENVELOPE_PRESETS, "loads": LOAD_PRESETS, "schedules": SCHEDULE_PRESETS}
    defaults = {"envelope": "typical", "loads": "office", "schedules": "office"}
    presets = {k: data.get(k, defaults[k]) for k in parts}
    for key, value in overrides.items():
        part = OVERRIDE_KEYS[key]
        if not isinstance(data.get(part), dict):
            preset_name = data.get(part, defaults[part])
            if preset_name not in parts[part]:
                raise ToolError(f"Unknown {part} preset {preset_name!r}")
            data[part] = parts[part][preset_name].model_dump()
        presets[part] = "custom"
        if key == "heating_setpoint_c":
            data[part]["heating_c"] = (value, data[part]["heating_c"][1])
        elif key == "cooling_setpoint_c":
            data[part]["cooling_c"] = (value, data[part]["cooling_c"][1])
        elif key == "weekday_hours":
            data[part]["weekdays"] = tuple(value) if value else None
        else:
            data[part][key] = value
    data["presets"] = presets
    try:
        return BuildingSpec.model_validate(data)
    except ValidationError as exc:
        raise ToolError(_short_error(exc)) from exc


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        msgs = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(p) for p in e["loc"] if not isinstance(p, int))
            msgs.append(f"{loc}: {e['msg']}" if loc else e["msg"])
        return "; ".join(msgs)
    return str(exc)


# ---------- context ----------


@dataclass
class ToolContext:
    base_dir: Path = field(default_factory=runs_dir)
    max_simulations: int = field(
        default_factory=lambda: int(os.environ.get("BEM_MAX_SIMULATIONS", "60"))
    )
    simulations_used: int = 0
    timeout_s: float = 900
    max_workers: int | None = None

    @property
    def db(self) -> Path:
        return results_db_path(self.base_dir)

    def reserve(self, n: int) -> None:
        left = self.max_simulations - self.simulations_used
        if n > left:
            raise ToolError(
                f"Simulation budget: {n} requested, {left} left of {self.max_simulations} "
                "this session. Reuse existing run_ids or run fewer cases."
            )
        self.simulations_used += n

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise ToolError(f"Invalid run_id {run_id!r}")
        path = storage.find_run_dir(self.base_dir, run_id)
        if path is None:
            raise ToolError(f"Unknown run_id {run_id!r}")
        return path


# ---------- tool implementations ----------


def _list_options(ctx: ToolContext, args: dict) -> dict:
    return {
        "locations": sorted(CITIES),
        "hvac": {h.value: h.description for h in HVACSystem},
        "envelope_presets": {k: v.model_dump() for k, v in ENVELOPE_PRESETS.items()},
        "load_presets": {k: v.model_dump() for k, v in LOAD_PRESETS.items()},
        "schedule_presets": {
            k: {"weekdays": v.weekdays, "saturday": v.saturday, "sunday": v.sunday_holidays}
            for k, v in SCHEDULE_PRESETS.items()
        },
        "study_parameters": list(PARAMETERS),
        "max_study_cases": MAX_CASES,
        "units": "Inputs SI (1 ft2 = 0.0929 m2). Results carry SI and IP fields "
        "(_tons, _kbtuh, _kbtu_ft2); quote them, do not convert.",
        "simulations_left": ctx.max_simulations - ctx.simulations_used,
    }


def _run_simulation(ctx: ToolContext, args: dict) -> dict:
    spec = spec_from_args(args)
    ctx.reserve(1)
    run = simulate(spec, base_dir=ctx.base_dir, timeout_s=ctx.timeout_s)
    if run.status == "invalid":  # stopped by pre-run checks: EnergyPlus never ran
        ctx.simulations_used -= 1
    summary = with_ip_units(run.summary())
    storage.archive_run(run.out_dir)
    storage.snapshot_db(ctx.db)
    return summary


def _run_study(ctx: ToolContext, args: dict) -> dict:
    name = args.get("name") or "Study"
    parameters = args.get("parameters") or {}
    base = spec_from_args(args.get("base") or {"location": "chicago"})
    try:
        study = ParametricStudy(name=name, base=base, parameters=parameters)
    except ValidationError as exc:
        raise ToolError(_short_error(exc)) from exc
    ctx.reserve(study.n_cases)
    result = run_study(
        study, base_dir=ctx.base_dir, max_workers=ctx.max_workers, timeout_s=ctx.timeout_s
    )
    table = [
        with_ip_units({k: v for k, v in row.items() if v is not None and k != "case"})
        for row in result.table()
    ]
    summary = {**result.summary(), "table": table}
    for run in result.runs.values():
        storage.archive_run(run.out_dir)
    storage.archive_run(ctx.base_dir / "studies" / result.study_id, subdir="studies")
    storage.snapshot_db(ctx.db)
    return summary


def _get_run_details(ctx: ToolContext, args: dict) -> dict:
    run_id = args.get("run_id", "")
    section = args.get("section", "zone_loads")
    run_dir = ctx.run_dir(run_id)
    if section == "issues":
        record = json.loads((run_dir / "run.json").read_text())
        return {
            "run_id": run_id,
            "status": record["status"],
            "issues": record.get("issues", []),
            "energyplus": record.get("energyplus"),
        }
    if section == "zone_loads":
        res = run_sql(
            ctx.db,
            "SELECT zone, load_type, ROUND(design_load_kw, 2) AS kw, peak_time FROM zone_sizing "
            f"WHERE run_id = '{run_id}' ORDER BY load_type, zone",
        )
        return {"run_id": run_id, "columns": res["columns"], "rows": res["rows"]}
    if section == "equipment":
        rows = [
            r
            for r in equipment_sizes(ctx.db, run_id)
            if r["description"].lower().startswith("design size") and r["units"] in ("W", "m3/s")
        ]
        compact = [
            [
                r["component_type"],
                r["component"],
                r["description"][12:],
                round(r["value"], 3),
                r["units"],
            ]
            for r in rows[:60]
        ]
        return {
            "run_id": run_id,
            "columns": ["type", "component", "size", "value", "units"],
            "rows": compact,
        }
    if section == "end_uses":
        uses = end_uses(ctx.db, run_id)
        if not uses:
            return {"run_id": run_id, "note": "No end uses: only annual runs have them"}
        return {
            "run_id": run_id,
            "end_uses_kwh": [[u["end_use"], u["fuel"], u["energy_kwh"]] for u in uses],
        }
    raise ToolError("section must be one of issues, zone_loads, equipment, end_uses")


def _compare_runs(ctx: ToolContext, args: dict) -> dict:
    run_ids = args.get("run_ids") or []
    sort_by = args.get("sort_by")
    rows = compare_runs(ctx.db, run_ids, sort_by=sort_by)
    return {"runs": [with_ip_units({k: v for k, v in r.items() if v is not None}) for r in rows]}


def _get_timeseries(ctx: ToolContext, args: dict) -> dict:
    run_id = args.get("run_id", "")
    variable = args.get("variable", "Electricity:Facility")
    if variable not in HOURLY_VARIABLES:
        raise ToolError(f"variable must be one of {HOURLY_VARIABLES}")
    freq = args.get("freq", "monthly")
    if freq == "hourly":
        raise ToolError(
            "Hourly series are too long to return; use daily or monthly, or query_results"
        )
    key = args.get("key", "Facility" if ":" in variable else "*")
    points = timeseries(ctx.db, run_id, variable, freq=freq, key=key)
    units = points[0]["units"] if points else ""
    label_keys = [k for k in ("month", "day") if points and k in points[0]]
    return {
        "run_id": run_id,
        "variable": variable,
        "freq": freq,
        "units": units,
        "points": [[*(p[k] for k in label_keys), p["value"]] for p in points],
        "columns": [*label_keys, "value"],
    }


def _query_results(ctx: ToolContext, args: dict) -> dict:
    return run_sql(ctx.db, args.get("sql", ""), max_rows=int(args.get("max_rows", 50)))


def _make_chart(ctx: ToolContext, args: dict) -> dict:
    kind = args.get("kind")
    out = ctx.base_dir / "charts" / f"{kind}_{uuid.uuid4().hex[:8]}.png"
    if kind == "metric":
        path = plot_metric_comparison(
            ctx.db, args.get("run_ids") or [], args.get("metric", ""), out
        )
    elif kind == "timeseries":
        path = plot_timeseries(
            ctx.db,
            args.get("run_ids") or [],
            args.get("variable", "Electricity:Facility"),
            out,
            freq=args.get("freq", "monthly"),
            key=args.get("key", "Facility"),
        )
    elif kind == "study":
        path = plot_study(ctx.db, args.get("study_id", ""), args.get("metric", ""), out)
    else:
        raise ToolError("kind must be metric, timeseries or study")
    storage.archive_file(path, "charts")
    storage.archive_file(path.with_suffix(".csv"), "charts")
    return {"chart": str(path), "table": str(path.with_suffix(".csv"))}


def _list_history(ctx: ToolContext, args: dict) -> dict:
    kind = args.get("kind", "runs")
    limit = int(args.get("limit", 15))
    if not ctx.db.exists():
        return {kind: []}
    if kind == "studies":
        return {
            "studies": [
                {k: v for k, v in s.items() if k != "parameters_json"}
                for s in list_studies(ctx.db, limit)
            ]
        }
    if kind == "study_cases":
        rows = study_table(ctx.db, args.get("study_id", ""))
        return {
            "cases": [
                {k: v for k, v in r.items() if v is not None and k != "overrides"} for r in rows
            ]
        }
    return {"runs": list_runs(ctx.db, limit)}


# ---------- registry ----------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[ToolContext, dict], dict]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


TOOLS: list[Tool] = [
    Tool(
        "list_options",
        "Locations, HVAC systems, presets, study parameters, units, simulations left.",
        _obj({}),
        _list_options,
    ),
    Tool(
        "run_simulation",
        "Validate, build and run one EnergyPlus model (invalid input is rejected before "
        "running, with the issues). Returns run_id, status, issues, "
        "peak zone loads (kW, W/m2), installed capacity (kW) and, for annual runs, EUI "
        "and unmet hours.",
        BUILDING_SCHEMA,
        _run_simulation,
    ),
    Tool(
        "run_parametric_study",
        "Run every combination of parameter values in parallel. Returns study_id, a table "
        f"of cases, the best case per metric and main effects. Max {MAX_CASES} cases.",
        _obj(
            {
                "name": {"type": "string"},
                "base": {
                    "type": "object",
                    "description": "Building shared by all cases: same fields as run_simulation",
                },
                "parameters": {
                    "type": "object",
                    "description": "Map of parameter -> list of values. Parameters: "
                    + ", ".join(PARAMETERS),
                },
            },
            ["name", "base", "parameters"],
        ),
        _run_study,
    ),
    Tool(
        "get_run_details",
        "Details of one run: issues, zone_loads (design load per zone), equipment "
        "(autosized capacities and air flows) or end_uses (annual kWh).",
        _obj(
            {
                "run_id": {"type": "string"},
                "section": {
                    "type": "string",
                    "enum": ["issues", "zone_loads", "equipment", "end_uses"],
                },
            },
            ["run_id", "section"],
        ),
        _get_run_details,
    ),
    Tool(
        "compare_runs",
        "Inputs and headline results of several runs side by side.",
        _obj(
            {
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "sort_by": {"type": "string", "enum": SORTABLE},
            },
            ["run_ids"],
        ),
        _compare_runs,
    ),
    Tool(
        "get_timeseries",
        "Daily or monthly totals (energy, kWh) or means (temperature) from an annual run.",
        _obj(
            {
                "run_id": {"type": "string"},
                "variable": {"type": "string", "enum": HOURLY_VARIABLES},
                "freq": {"type": "string", "enum": ["monthly", "daily", "annual"]},
                "key": {
                    "type": "string",
                    "description": "Zone name for zone variables; default all zones",
                },
            },
            ["run_id", "variable"],
        ),
        _get_timeseries,
    ),
    Tool(
        "query_results",
        "Read-only SQL (one SELECT) over the results database. Tables: " + schema_description(),
        _obj({"sql": {"type": "string"}, "max_rows": {"type": "integer", "maximum": 200}}, ["sql"]),
        _query_results,
    ),
    Tool(
        "make_chart",
        "Save a PNG chart (and CSV): kind=metric (bars across run_ids), timeseries "
        "(lines, annual runs) or study (all cases of study_id).",
        _obj(
            {
                "kind": {"type": "string", "enum": ["metric", "timeseries", "study"]},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "study_id": {"type": "string"},
                "metric": {"type": "string", "enum": list(METRIC_LABELS)},
                "variable": {"type": "string", "enum": HOURLY_VARIABLES},
                "freq": {"type": "string", "enum": ["monthly", "daily"]},
            },
            ["kind"],
        ),
        _make_chart,
    ),
    Tool(
        "list_history",
        "Earlier runs, studies, or the cases of one study (kind=study_cases + study_id).",
        _obj(
            {
                "kind": {"type": "string", "enum": ["runs", "studies", "study_cases"]},
                "study_id": {"type": "string"},
                "limit": {"type": "integer", "maximum": 50},
            }
        ),
        _list_history,
    ),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def relax_schema(schema: Any) -> Any:
    """Move enums and numeric ranges from the schema into descriptions.

    Some providers (Groq) check the model's tool call against the schema and reject the
    whole request on a mismatch, e.g. "Washington DC" for an enum that says
    "washington_dc". Our own validation then never runs and the model gets no useful
    feedback. With the constraints in the descriptions the model still sees the allowed
    values, and our validators (pydantic, run_sql, ...) enforce them with clear errors.
    """
    if isinstance(schema, list):
        return [relax_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: relax_schema(v) for k, v in schema.items() if k not in ("enum", "minimum", "maximum")}
    notes = []
    if "enum" in schema:
        notes.append("one of: " + ", ".join(str(v) for v in schema["enum"]))
    if "minimum" in schema or "maximum" in schema:
        notes.append(f"range: {schema.get('minimum', '')}..{schema.get('maximum', '')}")
    if notes:
        desc = out.get("description", "")
        out["description"] = (desc + "; " if desc else "") + "; ".join(notes)
    return out


def tool_specs() -> list[ToolSpec]:
    """Tool specs for the LLM, with relaxed schemas (see relax_schema)."""
    return [ToolSpec(t.name, t.description, relax_schema(t.parameters)) for t in TOOLS]


def execute(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Run a tool; never raises for problems the model could fix."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}. Tools: {', '.join(TOOLS_BY_NAME)}"}
    if args is not None and not isinstance(args, dict):
        return {"error": "Arguments must be a JSON object"}
    try:
        return tool.fn(ctx, args or {})
    except (ToolError, QueryError, StudyError) as exc:
        return {"error": str(exc)}
    except ValidationError as exc:
        return {"error": _short_error(exc)}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the session
        log.exception("tool %s failed", name)
        return {"error": f"Internal error in {name}: {type(exc).__name__}: {exc}"}


def to_text(result: dict[str, Any], max_chars: int = MAX_RESULT_CHARS) -> str:
    """Compact JSON for the model, cut to max_chars."""
    text = json.dumps(result, separators=(",", ":"), default=str)
    if len(text) > max_chars:
        text = text[: max_chars - 40] + '..."(truncated; ask for less)"'
    return text
