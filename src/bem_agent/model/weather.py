"""Weather files and design days.

Demo cities map to the weather files bundled with EnergyPlus. Site location and sizing
design days are read from the matching .ddy file: the 99.6% heating and 0.4% cooling
conditions (falling back to 99% / 1%), the usual basis for HVAC sizing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .idf import IDFObject, find, parse_idf

# City key -> weather file stem in EnergyPlus's WeatherData directory.
CITIES: dict[str, str] = {
    "chicago": "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3",
    "san_francisco": "USA_CA_San.Francisco.Intl.AP.724940_TMY3",
    "golden_co": "USA_CO_Golden-NREL.724666_TMY3",
    "tampa": "USA_FL_Tampa.Intl.AP.722110_TMY3",
    "washington_dc": "USA_VA_Sterling-Washington.Dulles.Intl.AP.724030_TMY3",
}

HEATING_PATTERNS = ("htg 99.6% condns db", "htg 99% condns db")
COOLING_PATTERNS = ("clg .4% condns db=>mwb", "clg 1% condns db=>mwb")


class WeatherError(ValueError):
    pass


@dataclass
class Weather:
    epw: Path
    ddy: Path

    def site_objects(self) -> list[IDFObject]:
        return design_day_objects(self.ddy.read_text(errors="replace"))


def energyplus_dir(explicit: str | os.PathLike | None = None) -> Path:
    value = explicit or os.environ.get("ENERGYPLUS_DIR")
    if not value:
        raise WeatherError("EnergyPlus install not found: set ENERGYPLUS_DIR.")
    return Path(value)


def resolve_weather(location: str, eplus_dir: str | os.PathLike | None = None) -> Weather:
    """`location` is a city key from CITIES or a path to an .epw with a .ddy beside it."""
    key = location.strip().lower().replace(" ", "_")
    if key in CITIES:
        base = energyplus_dir(eplus_dir) / "WeatherData" / CITIES[key]
        epw, ddy = base.with_suffix(".epw"), base.with_suffix(".ddy")
    else:
        epw = Path(location)
        if epw.suffix.lower() != ".epw":
            raise WeatherError(
                f"Unknown location {location!r}. Use one of {sorted(CITIES)} or an .epw path."
            )
        ddy = epw.with_suffix(".ddy")
    for p in (epw, ddy):
        if not p.is_file():
            raise WeatherError(f"Weather file not found: {p}")
    return Weather(epw=epw, ddy=ddy)


def _pick(design_days: list[IDFObject], patterns: tuple[str, ...]) -> IDFObject | None:
    for pattern in patterns:
        for dd in design_days:
            if pattern in dd.name.lower():
                return dd
    return None


def design_day_objects(ddy_text: str) -> list[IDFObject]:
    """Site:Location plus one heating and one cooling design day from .ddy text."""
    objects = parse_idf(ddy_text)
    locations = find(objects, "Site:Location")
    if not locations:
        raise WeatherError("No Site:Location in .ddy file")
    design_days = find(objects, "SizingPeriod:DesignDay")
    heating = _pick(design_days, HEATING_PATTERNS)
    cooling = _pick(design_days, COOLING_PATTERNS)
    if heating is None or cooling is None:
        raise WeatherError("Could not find 99.6%/99% heating and 0.4%/1% cooling design days")
    return [locations[0], heating, cooling]
