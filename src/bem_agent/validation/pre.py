"""Checks run before a simulation, so bad inputs fail fast with a clear message.

Pydantic already enforces hard bounds on each field. These checks look at combinations
of inputs and at values that are allowed but unusual enough to flag.
"""

from __future__ import annotations

from ..model.building import BuildingSpec, build_objects
from ..model.weather import WeatherError, resolve_weather
from .report import ValidationReport

MAX_ZONES = 60


def validate_spec(spec: BuildingSpec, check_weather: bool = True) -> ValidationReport:
    r = ValidationReport("pre_run")
    g, h, loads = spec.geometry, spec.schedules, spec.loads

    # Thermostat logic
    heat_occ, heat_setback = h.heating_c
    cool_occ, cool_setback = h.cooling_c
    if cool_occ - heat_occ < 1.0:
        r.add(
            "error",
            "SETPOINT_DEADBAND",
            f"Occupied cooling setpoint {cool_occ} °C must be at least 1 °C above heating "
            f"setpoint {heat_occ} °C.",
        )
    if heat_setback > heat_occ:
        r.add("error", "HEATING_SETBACK", "Heating setback is higher than the occupied setpoint.")
    if cool_setback < cool_occ:
        r.add("error", "COOLING_SETBACK", "Cooling setback is lower than the occupied setpoint.")
    if cool_setback - heat_setback < 1.0:
        r.add("error", "SETBACK_DEADBAND", "Setback heating and cooling setpoints overlap.")

    # Geometry
    aspect = max(g.length_m, g.width_m) / min(g.length_m, g.width_m)
    if aspect > 5:
        r.add("warning", "ASPECT_RATIO", f"Very elongated footprint (aspect ratio {aspect:.1f}).")
    if g.window_to_wall_ratio > 0.6:
        r.add(
            "warning",
            "HIGH_WWR",
            f"Window-to-wall ratio {g.window_to_wall_ratio:.0%} is high; expect large solar loads.",
        )
    if not g.perimeter_core:
        r.add(
            "info",
            "SINGLE_ZONE_FLOORS",
            "Footprint too small for perimeter/core zoning; using one zone per floor.",
        )
    n_zones = g.num_floors * (5 if g.perimeter_core else 1)
    if n_zones > MAX_ZONES:
        r.add("warning", "LARGE_MODEL", f"{n_zones} zones; simulations will be slow.")

    # Internal loads
    if loads.lighting_w_per_m2 > 16:
        r.add("warning", "HIGH_LPD", f"Lighting {loads.lighting_w_per_m2} W/m² is unusually high.")
    if loads.equipment_w_per_m2 > 40:
        r.add(
            "warning", "HIGH_EPD", f"Equipment {loads.equipment_w_per_m2} W/m² is unusually high."
        )
    if loads.people_per_m2 > 0.5:
        r.add("warning", "HIGH_OCCUPANCY", f"{loads.people_per_m2} people/m² is very dense.")
    if h.weekdays is None and h.saturday is None and h.sunday_holidays is None:
        r.add("warning", "NEVER_OCCUPIED", "The building is never occupied.")

    # Weather file and model integrity
    site = None
    if check_weather:
        try:
            site = resolve_weather(spec.location).site_objects()
        except WeatherError as exc:
            r.add("error", "WEATHER", str(exc))
    if site is not None:
        try:
            build_objects(spec, site)
        except ValueError as exc:
            r.add("error", "MODEL_REFERENCES", str(exc))
    return r
