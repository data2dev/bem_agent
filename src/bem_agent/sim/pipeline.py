"""validate -> build -> run -> check: the one entry point the agent's tools will call."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..model.building import BuildingSpec, write_idf
from ..model.geometry import zones
from ..model.weather import resolve_weather
from ..results.etl import load_run
from ..validation.post import RunMetrics, check_metrics, check_run, extract_metrics
from ..validation.pre import validate_spec
from ..validation.report import ValidationReport
from .runner import RunResult, run_energyplus

Status = Literal["invalid", "failed", "completed", "completed_with_warnings"]


def runs_dir() -> Path:
    """Where run folders go: $BEM_RUNS_DIR (a mounted bucket on Cloud Run) or ./runs."""
    return Path(os.environ.get("BEM_RUNS_DIR", "runs"))


def new_run_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "run"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{slug}_{uuid.uuid4().hex[:6]}"


@dataclass
class SimulationRun:
    run_id: str
    spec: BuildingSpec
    out_dir: Path
    status: Status
    pre: ValidationReport
    post: ValidationReport | None = None
    run: RunResult | None = None
    metrics: RunMetrics | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-safe summary: small enough to hand back to an LLM."""
        out: dict[str, Any] = {"run_id": self.run_id, "status": self.status}
        issues = self.pre.issues + (self.post.issues if self.post else [])
        if issues:
            out["issues"] = [f"{i.level.upper()} {i.code}: {i.message}" for i in issues]
        if self.run:
            out["energyplus"] = {
                "elapsed_s": self.run.elapsed_s,
                "warnings": self.run.err.warnings,
                "severe": self.run.err.severe,
            }
        if self.metrics:
            m = self.metrics
            out["floor_area_m2"] = round(m.floor_area_m2, 1)
            out["peak_cooling_kw"] = round(m.peak_cooling_w / 1000, 1)
            out["peak_heating_kw"] = round(m.peak_heating_w / 1000, 1)
            out["peak_cooling_w_m2"] = round(m.peak_cooling_w / m.floor_area_m2, 1)
            out["peak_heating_w_m2"] = round(m.peak_heating_w / m.floor_area_m2, 1)
            if m.eui_kwh_m2 is not None:
                out["site_eui_kwh_m2"] = round(m.eui_kwh_m2, 1)
            if m.unmet_hours is not None:
                out["unmet_hours"] = {"heating": m.unmet_hours[0], "cooling": m.unmet_hours[1]}
        return out


def results_db_path(base_dir: Path | None = None) -> Path:
    return (base_dir or runs_dir()) / "results.db"


def simulate(
    spec: BuildingSpec,
    base_dir: Path | None = None,
    timeout_s: float = 900,
    load_results: bool = True,
) -> SimulationRun:
    """Validate, build, run and check one model; then load it into results.db."""
    run_id = new_run_id(spec.name)
    out_dir = (base_dir or runs_dir()) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "spec.json").write_text(spec.model_dump_json(indent=2))

    pre = validate_spec(spec)
    result = SimulationRun(run_id, spec, out_dir, "invalid", pre)
    if not pre.ok:
        return _finish(result, base_dir, load_results)

    weather = resolve_weather(spec.location)
    # Not "in.idf": EnergyPlus creates its own in.idf link in the run folder.
    idf = write_idf(spec, out_dir / "model.idf", site_objects=weather.site_objects())
    result.run = run_energyplus(idf, weather.epw, out_dir, timeout_s=timeout_s)
    result.post = check_run(result.run)
    if result.post.ok:
        zone_names = [z.name for z in zones(spec.geometry)]
        result.metrics = extract_metrics(result.run, zone_names, spec.geometry.total_floor_area_m2)
        check_metrics(result.metrics, result.post)
    if not result.post.ok:
        result.status = "failed"
    elif result.post.warnings or pre.warnings:
        result.status = "completed_with_warnings"
    else:
        result.status = "completed"
    return _finish(result, base_dir, load_results)


def _finish(result: SimulationRun, base_dir: Path | None, load_results: bool) -> SimulationRun:
    _save(result)
    if load_results:
        result.extra["loaded_rows"] = load_run(result.out_dir, results_db_path(base_dir))
    return result


def _save(result: SimulationRun) -> SimulationRun:
    record = {
        **result.summary(),
        "pre_run": result.pre.to_dict(),
        "post_run": result.post.to_dict() if result.post else None,
    }
    (result.out_dir / "run.json").write_text(json.dumps(record, indent=2))
    return result
