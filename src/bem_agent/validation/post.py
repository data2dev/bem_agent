"""Checks run after a simulation, so the agent never reports results from a broken run.

Thresholds are deliberately wide: they catch broken models (missing zones, loads off by
an order of magnitude), not ordinary design variation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..results import sql
from ..sim.runner import RunResult
from .report import ValidationReport

# Building peak design load per floor area, W/m².
COOLING_W_M2 = (15.0, 250.0)
HEATING_W_M2 = (5.0, 250.0)
# Annual site energy use intensity, kWh/m².
EUI_KWH_M2 = (50.0, 800.0)
MAX_UNMET_HOURS = 300.0  # ASHRAE 90.1 compliance limit, heating + cooling
MANY_WARNINGS = 200


@dataclass
class RunMetrics:
    """Numbers the post-run checks need, extracted from a run's outputs."""

    zones_expected: list[str]
    zones_sized: dict[str, dict[str, float]]  # zone -> {"Cooling": W, "Heating": W}
    floor_area_m2: float
    eui_kwh_m2: float | None = None
    unmet_hours: tuple[float, float] | None = None

    @property
    def peak_cooling_w(self) -> float:
        return sum(z.get("Cooling", 0.0) for z in self.zones_sized.values())

    @property
    def peak_heating_w(self) -> float:
        return sum(z.get("Heating", 0.0) for z in self.zones_sized.values())


def extract_metrics(run: RunResult, zone_names: list[str], floor_area_m2: float) -> RunMetrics:
    sized: dict[str, dict[str, float]] = {}
    for row in sql.zone_sizing(run.sql_path):
        sized.setdefault(row.zone.upper(), {})[row.load_type] = row.user_load_w
    energy_gj = sql.site_energy_gj(run.sql_path)
    eui = energy_gj * 1e9 / 3.6e6 / floor_area_m2 if energy_gj else None
    return RunMetrics(
        zones_expected=[z.upper() for z in zone_names],
        zones_sized=sized,
        floor_area_m2=floor_area_m2,
        eui_kwh_m2=eui,
        unmet_hours=sql.unmet_hours(run.sql_path),
    )


def check_run(run: RunResult) -> ValidationReport:
    r = ValidationReport("post_run")
    if run.timed_out:
        r.add("error", "TIMEOUT", f"EnergyPlus timed out after {run.elapsed_s:.0f} s.")
        return r
    if not run.err.completed or run.err.severe:
        detail = "; ".join(
            m.short(200) for m in (run.err.by_level("Severe") + run.err.by_level("Fatal"))[:5]
        )
        r.add(
            "error",
            "SIMULATION_FAILED",
            f"EnergyPlus did not complete cleanly ({run.err.severe} severe errors). "
            f"{detail}".strip(),
        )
        return r
    if not run.sql_path.exists():
        r.add("error", "NO_SQL_OUTPUT", "EnergyPlus finished but wrote no SQLite output.")
    if run.err.warnings > MANY_WARNINGS:
        r.add("info", "MANY_WARNINGS", f"{run.err.warnings} EnergyPlus warnings; see eplusout.err.")
    return r


def check_metrics(m: RunMetrics, report: ValidationReport | None = None) -> ValidationReport:
    r = report or ValidationReport("post_run")
    missing = sorted(set(m.zones_expected) - set(m.zones_sized))
    if missing:
        r.add("error", "ZONES_NOT_SIZED", f"No sizing results for zones: {', '.join(missing)}.")
    unexpected_zero = [z for z, loads in m.zones_sized.items() if loads.get("Cooling", 0.0) <= 0]
    if unexpected_zero:
        r.add(
            "warning",
            "ZERO_COOLING_ZONES",
            f"Zero cooling design load: {', '.join(unexpected_zero)}.",
        )

    area = m.floor_area_m2
    for label, total, (lo, hi), code in [
        ("cooling", m.peak_cooling_w, COOLING_W_M2, "COOLING_DENSITY"),
        ("heating", m.peak_heating_w, HEATING_W_M2, "HEATING_DENSITY"),
    ]:
        density = total / area if area else 0.0
        if not lo <= density <= hi:
            r.add(
                "warning",
                code,
                f"Peak {label} load {density:.0f} W/m² is outside the plausible {lo:.0f}-{hi:.0f} "
                "W/m² range; check inputs.",
            )
    if m.eui_kwh_m2 is not None and not EUI_KWH_M2[0] <= m.eui_kwh_m2 <= EUI_KWH_M2[1]:
        r.add(
            "warning",
            "EUI_RANGE",
            f"Site EUI {m.eui_kwh_m2:.0f} kWh/m² is outside "
            f"{EUI_KWH_M2[0]:.0f}-{EUI_KWH_M2[1]:.0f}.",
        )
    if m.unmet_hours is not None and sum(m.unmet_hours) > MAX_UNMET_HOURS:
        heat, cool = m.unmet_hours
        r.add(
            "warning",
            "UNMET_HOURS",
            f"{heat:.0f} heating + {cool:.0f} cooling unmet hours exceed {MAX_UNMET_HOURS:.0f}; "
            "equipment may be undersized or controls misconfigured.",
        )
    return r
