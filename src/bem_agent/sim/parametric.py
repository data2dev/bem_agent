"""Parametric studies: a base building, a few parameters to vary, every combination.

    study = ParametricStudy(
        name="Office HVAC x WWR",
        base=BuildingSpec(location="chicago", geometry={"num_floors": 3}),
        parameters={"hvac": ["ideal_loads", "vav_chiller_boiler", "pthp"],
                    "geometry.window_to_wall_ratio": [0.3, 0.5]},
    )
    result = run_study(study)          # 6 cases, run in parallel, loaded into results.db
    result.table()                     # one row per case: parameters + headline results

Only whitelisted parameters can vary, and every case is validated before any
simulation starts, so a typo fails the whole study up front instead of halfway.
"""

from __future__ import annotations

import itertools
import json
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..model.building import BuildingSpec
from ..results.etl import load_run, record_study
from .pipeline import SimulationRun, results_db_path, runs_dir, simulate

# Parameter path -> short label used in case names and chart labels.
PARAMETERS: dict[str, str] = {
    "location": "location",
    "hvac": "HVAC",
    "envelope": "envelope",
    "loads": "loads",
    "schedules": "schedules",
    "geometry.window_to_wall_ratio": "WWR",
    "geometry.orientation_deg": "orientation",
    "geometry.num_floors": "floors",
    "geometry.length_m": "length",
    "geometry.width_m": "width",
    "geometry.floor_to_floor_m": "floor height",
    "envelope.window_u_si": "window U",
    "envelope.window_shgc": "SHGC",
    "envelope.wall_insulation_r_si": "wall R",
    "envelope.roof_insulation_r_si": "roof R",
    "loads.lighting_w_per_m2": "LPD",
    "loads.equipment_w_per_m2": "EPD",
    "loads.people_per_m2": "occupancy",
}
MAX_CASES = 48
MAX_VALUES_PER_PARAMETER = 8

TABLE_METRICS = (
    "peak_cooling_kw",
    "peak_heating_kw",
    "peak_cooling_w_m2",
    "peak_heating_w_m2",
    "cooling_capacity_kw",
    "heating_capacity_kw",
    "site_eui_kwh_m2",
)
SUMMARY_METRICS = (
    "peak_cooling_kw",
    "peak_heating_kw",
    "cooling_capacity_kw",
    "heating_capacity_kw",
    "site_eui_kwh_m2",
)

HVAC_LABELS = {"ideal_loads": "Ideal", "vav_chiller_boiler": "VAV", "pthp": "PTHP"}


class StudyError(ValueError):
    pass


class ParametricStudy(BaseModel):
    name: str = Field(..., min_length=1, max_length=60, pattern=r"^[^\r\n]+$")
    base: BuildingSpec = Field(default_factory=BuildingSpec)
    parameters: dict[str, list[Any]] = Field(..., min_length=1)

    @field_validator("parameters")
    @classmethod
    def _check_parameters(cls, params: dict[str, list[Any]]) -> dict[str, list[Any]]:
        for path, values in params.items():
            if path not in PARAMETERS:
                raise ValueError(
                    f"Cannot vary {path!r}. Allowed parameters: {', '.join(PARAMETERS)}"
                )
            if not values:
                raise ValueError(f"{path}: give at least one value")
            if len(values) > MAX_VALUES_PER_PARAMETER:
                raise ValueError(f"{path}: at most {MAX_VALUES_PER_PARAMETER} values")
            if len({json.dumps(v, sort_keys=True) for v in values}) != len(values):
                raise ValueError(f"{path}: duplicate values")
        return params

    @model_validator(mode="after")
    def _check_size(self) -> ParametricStudy:
        n = self.n_cases
        if n > MAX_CASES:
            raise ValueError(f"{n} cases exceeds the limit of {MAX_CASES}; vary fewer values")
        # Conflicting paths: varying "envelope" (a preset) and "envelope.x" together.
        for path in self.parameters:
            if "." in path and path.split(".")[0] in self.parameters:
                raise ValueError(f"Cannot vary both {path.split('.')[0]!r} and {path!r}")
        return self

    @property
    def n_cases(self) -> int:
        n = 1
        for values in self.parameters.values():
            n *= len(values)
        return n


@dataclass
class Case:
    index: int
    overrides: dict[str, Any]
    label: str
    spec: BuildingSpec


def _format_value(path: str, value: Any) -> str:
    if path == "hvac":
        return HVAC_LABELS.get(value, str(value))
    if path == "geometry.window_to_wall_ratio":
        return f"{float(value):.0%}"
    if path == "geometry.orientation_deg":
        return f"{float(value):g}°"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def case_label(overrides: dict[str, Any]) -> str:
    parts = []
    for path, value in overrides.items():
        v = _format_value(path, value)
        parts.append(v if path == "hvac" else f"{PARAMETERS[path]} {v}")
    return " / ".join(parts)


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    head, _, rest = path.partition(".")
    if not rest:
        data[head] = value
        return
    current = data.get(head)
    if isinstance(current, str):  # a preset name: expand so one field can be overridden
        raise StudyError(f"internal: {head} should already be expanded")
    data.setdefault(head, {})
    data[head][rest] = value


def expand_cases(study: ParametricStudy) -> list[Case]:
    """Every combination of parameter values, each validated as a BuildingSpec."""
    base = json.loads(study.base.model_dump_json())
    base.pop("presets", None)
    # Keep the base's preset names unless a parameter changes that part of the spec.
    base_presets = study.base.presets
    paths = list(study.parameters)
    cases, errors = [], []
    for i, combo in enumerate(itertools.product(*study.parameters.values()), start=1):
        overrides = dict(zip(paths, combo, strict=True))
        data = json.loads(json.dumps(base))
        presets = dict(base_presets)
        for path, value in overrides.items():
            _set_path(data, path, value)
            top = path.split(".")[0]
            if top in presets:
                presets[top] = value if "." not in path and isinstance(value, str) else "custom"
        label = case_label(overrides)
        data["name"] = label[:60]
        data["presets"] = presets
        try:
            spec = BuildingSpec.model_validate(data)
        except ValidationError as exc:
            errors.append(f"case {i} ({label}): {_first_error(exc)}")
            continue
        cases.append(Case(i, overrides, label, spec))
    if errors:
        raise StudyError("Invalid cases, nothing was run:\n" + "\n".join(errors[:10]))
    return cases


def _first_error(exc: ValidationError) -> str:
    e = exc.errors()[0]
    loc = ".".join(str(p) for p in e["loc"])
    return f"{loc}: {e['msg']}"


@dataclass
class StudyResult:
    study_id: str
    study: ParametricStudy
    cases: list[Case]
    runs: dict[int, SimulationRun] = field(default_factory=dict)  # case index -> run
    db_path: Path | None = None

    def table(self) -> list[dict[str, Any]]:
        capacities = self._capacities()
        rows = []
        for case in self.cases:
            run = self.runs.get(case.index)
            summary = run.summary() if run else {"status": "not_run"}
            summary = {**summary, **capacities.get(summary.get("run_id"), {})}
            rows.append(
                {
                    "case": case.index,
                    "label": case.label,
                    **{PARAMETERS[p]: v for p, v in case.overrides.items()},
                    "run_id": summary.get("run_id"),
                    "status": summary["status"],
                    **{k: summary.get(k) for k in TABLE_METRICS if summary.get(k) is not None},
                }
            )
        return rows

    def _capacities(self) -> dict[str, dict[str, float]]:
        """Installed capacities per run_id, as computed by the ETL into results.db."""
        if not self.db_path or not Path(self.db_path).exists():
            return {}
        ids = [r.run_id for r in self.runs.values()]
        if not ids:
            return {}
        from ..results.queries import compare_runs

        try:
            rows = compare_runs(self.db_path, ids)
        except Exception:  # noqa: BLE001 - capacities are optional extras
            return {}
        return {
            r["run_id"]: {
                k: r[k] for k in ("cooling_capacity_kw", "heating_capacity_kw") if r[k] is not None
            }
            for r in rows
        }

    def summary(self) -> dict[str, Any]:
        """Compact study summary for the LLM: counts, best cases, main effects."""
        table = self.table()
        ok = [r for r in table if r["status"] in ("completed", "completed_with_warnings")]
        out: dict[str, Any] = {
            "study_id": self.study_id,
            "name": self.study.name,
            "cases": len(table),
            "completed": len(ok),
        }
        failed = [f"case {r['case']} ({r['label']}): {r['status']}" for r in table if r not in ok]
        if failed:
            out["failed"] = failed
        metrics = [m for m in SUMMARY_METRICS if any(m in r for r in ok)]
        out["best"] = {
            m: min((r for r in ok if r.get(m) is not None), key=lambda r: r[m])["label"]
            for m in metrics
            if any(r.get(m) is not None for r in ok)
        }
        out["main_effects"] = main_effects(self.study, self.cases, ok, metrics)
        return out


def main_effects(
    study: ParametricStudy, cases: list[Case], rows: list[dict], metrics: list[str]
) -> dict[str, dict[str, dict[str, float]]]:
    """Mean of each metric for each value of each parameter (averaged over the others)."""
    by_case = {r["case"]: r for r in rows}
    effects: dict[str, dict[str, dict[str, float]]] = {}
    for path, values in study.parameters.items():
        if len(values) < 2:
            continue
        name = PARAMETERS[path]
        effects[name] = {}
        for metric in metrics:
            means = {}
            for value in values:
                vals = [
                    by_case[c.index][metric]
                    for c in cases
                    if c.index in by_case
                    and c.overrides[path] == value
                    and by_case[c.index].get(metric) is not None
                ]
                if vals:
                    means[_format_value(path, value)] = round(sum(vals) / len(vals), 1)
            if means:
                effects[name][metric] = means
    return effects


def default_workers() -> int:
    """Parallel EnergyPlus runs: $BEM_MAX_WORKERS, else usable CPUs (max 4).

    os.cpu_count() can report the host's CPUs inside a container, so prefer the CPUs
    this process may actually use.
    """
    if os.environ.get("BEM_MAX_WORKERS"):
        return max(1, int(os.environ["BEM_MAX_WORKERS"]))
    try:
        usable = len(os.sched_getaffinity(0))
    except AttributeError:  # not available on macOS
        usable = os.cpu_count() or 1
    return max(1, min(4, usable))


def run_study(
    study: ParametricStudy,
    base_dir: Path | None = None,
    max_workers: int | None = None,
    timeout_s: float = 900,
    on_case_done: Callable[[Case, SimulationRun], None] | None = None,
) -> StudyResult:
    """Run every case in parallel; one failing case never stops the others."""
    cases = expand_cases(study)  # raises before any simulation if a case is invalid
    base = base_dir or runs_dir()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    result = StudyResult(f"study_{stamp}_{uuid.uuid4().hex[:6]}", study, cases)
    result.db_path = results_db_path(base)

    # EnergyPlus runs happen in worker threads (each is a subprocess); results are loaded
    # into SQLite from this thread only, so the database never sees concurrent writers.
    with ThreadPoolExecutor(max_workers=max_workers or default_workers()) as pool:
        futures = {pool.submit(simulate, c.spec, base, timeout_s, False): c for c in cases}
        for future in as_completed(futures):
            case = futures[future]
            run = future.result()
            load_run(run.out_dir, result.db_path)
            result.runs[case.index] = run
            if on_case_done:
                on_case_done(case, run)

    record_study(
        result.db_path,
        result.study_id,
        study.name,
        {p: list(v) for p, v in study.parameters.items()},
        [(c.index, result.runs[c.index].run_id, c.label, c.overrides) for c in cases],
    )
    study_dir = base / "studies" / result.study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "study.json").write_text(study.model_dump_json(indent=2))
    (study_dir / "summary.json").write_text(
        json.dumps({**result.summary(), "table": result.table()}, indent=2)
    )
    return result
