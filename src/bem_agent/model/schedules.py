"""Occupancy, lighting, equipment, and thermostat schedules.

Every schedule set is built from operating hours, so presets and custom schedules go
through the same code. Design days use the weekday profile (peak internal gains for
cooling sizing) and constant occupied setpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .idf import IDFObject

DayProfile = list[tuple[int, float]]  # (until hour 1..24, value), last entry must be 24

# Schedule names referenced elsewhere in the model.
OCCUPANCY = "Occupancy"
LIGHTING = "Lighting"
EQUIPMENT = "Equipment"
HEATING_SP = "Heating_Setpoint"
COOLING_SP = "Cooling_Setpoint"
ACTIVITY = "Activity_Level"
ALWAYS_ON = "Always_On"

Hours = tuple[int, int] | None


def _check_hours(v: Hours) -> Hours:
    if v is None:
        return v
    start, end = v
    if not 0 <= start < end <= 24:
        raise ValueError(f"hours must satisfy 0 <= start < end <= 24, got {v}")
    return v


class OperatingHours(BaseModel):
    """When the building is occupied. None means closed all day."""

    weekdays: Hours = (8, 18)
    saturday: Hours = None
    sunday_holidays: Hours = None
    # Fractions of peak during and outside occupied hours.
    occupancy: tuple[float, float] = (0.95, 0.0)
    lighting: tuple[float, float] = (0.9, 0.05)
    equipment: tuple[float, float] = (0.9, 0.3)
    # Thermostat setpoints (°C): occupied, setback. HVAC starts `hvac_lead_hours` early.
    heating_c: tuple[float, float] = (21.1, 15.6)
    cooling_c: tuple[float, float] = (24.0, 29.4)
    hvac_lead_hours: int = Field(2, ge=0, le=6)

    _v_weekdays = field_validator("weekdays", "saturday", "sunday_holidays")(_check_hours)

    @field_validator("heating_c", "cooling_c")
    @classmethod
    def _setpoints(cls, v: tuple[float, float]) -> tuple[float, float]:
        if not all(5.0 <= t <= 40.0 for t in v):
            raise ValueError("setpoints must be between 5 and 40 °C")
        return v

    @field_validator("occupancy", "lighting", "equipment")
    @classmethod
    def _fractions(cls, v: tuple[float, float]) -> tuple[float, float]:
        if not all(0.0 <= f <= 1.0 for f in v):
            raise ValueError("fractions must be between 0 and 1")
        return v


SCHEDULE_PRESETS: dict[str, OperatingHours] = {
    "office": OperatingHours(weekdays=(8, 18), saturday=(8, 13)),
    "retail": OperatingHours(
        weekdays=(9, 21),
        saturday=(9, 21),
        sunday_holidays=(11, 18),
        equipment=(0.9, 0.2),
    ),
    "24_7": OperatingHours(
        weekdays=(0, 24),
        saturday=(0, 24),
        sunday_holidays=(0, 24),
        occupancy=(0.9, 0.9),
        lighting=(0.9, 0.9),
        equipment=(0.9, 0.9),
    ),
}


def day_profile(hours: Hours, on: float, off: float, lead: int = 0) -> DayProfile:
    if hours is None:
        return [(24, off)]
    start, end = max(hours[0] - lead, 0), hours[1]
    profile: DayProfile = []
    if start > 0:
        profile.append((start, off))
    profile.append((end, on))
    if end < 24:
        profile.append((24, off))
    return profile


def compact_schedule(
    name: str,
    type_limits: str,
    weekdays: DayProfile,
    saturday: DayProfile,
    sunday_holidays: DayProfile,
    summer_design_day: DayProfile | None = None,
    winter_design_day: DayProfile | None = None,
) -> IDFObject:
    fields: list = [name, type_limits, "Through: 12/31"]
    for label, profile in [
        ("Weekdays", weekdays),
        ("SummerDesignDay", summer_design_day or weekdays),
        ("WinterDesignDay", winter_design_day or weekdays),
        ("Saturday", saturday),
        ("AllOtherDays", sunday_holidays),
    ]:
        if profile[-1][0] != 24:
            raise ValueError(f"{name}: {label} profile must end at hour 24")
        fields.append(f"For: {label}")
        for hour, value in profile:
            fields.extend([f"Until: {hour:02d}:00", float(value)])
    return IDFObject("Schedule:Compact", fields)


def schedule_objects(hours: OperatingHours) -> list[IDFObject]:
    def profiles(on: float, off: float, lead: int = 0) -> tuple[DayProfile, ...]:
        return tuple(
            day_profile(h, on, off, lead)
            for h in (hours.weekdays, hours.saturday, hours.sunday_holidays)
        )

    lead = hours.hvac_lead_hours
    heat_on, heat_off = hours.heating_c
    cool_on, cool_off = hours.cooling_c
    return [
        IDFObject("ScheduleTypeLimits", ["Fraction", 0.0, 1.0, "Continuous"]),
        IDFObject("ScheduleTypeLimits", ["Temperature", -60.0, 200.0, "Continuous", "Temperature"]),
        IDFObject("ScheduleTypeLimits", ["Any Number"]),
        compact_schedule(OCCUPANCY, "Fraction", *profiles(*hours.occupancy)),
        compact_schedule(LIGHTING, "Fraction", *profiles(*hours.lighting)),
        compact_schedule(EQUIPMENT, "Fraction", *profiles(*hours.equipment)),
        compact_schedule(
            HEATING_SP,
            "Temperature",
            *profiles(heat_on, heat_off, lead),
            summer_design_day=[(24, heat_on)],
            winter_design_day=[(24, heat_on)],
        ),
        compact_schedule(
            COOLING_SP,
            "Temperature",
            *profiles(cool_on, cool_off, lead),
            summer_design_day=[(24, cool_on)],
            winter_design_day=[(24, cool_on)],
        ),
        IDFObject("Schedule:Constant", [ACTIVITY, "Any Number", 120.0]),  # W/person
        IDFObject("Schedule:Constant", [ALWAYS_ON, "Fraction", 1.0]),
    ]
