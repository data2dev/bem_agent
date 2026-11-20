"""HVAC systems via EnergyPlus HVACTemplate objects.

HVACTemplate objects are expanded into full systems by EnergyPlus's ExpandObjects step,
so the model must be run with `energyplus --expandobjects` (`-x`). Equipment is
autosized, which is what lets the agent read back equipment capacities.

ExpandObjects does not fill in IDD defaults for blank or missing fields, so every
template object is written out in full: IDD defaults (from `_template_fields.py`) plus
the overrides below.
"""

from __future__ import annotations

from enum import StrEnum

from . import schedules as s
from ._template_fields import TEMPLATE_FIELDS
from .idf import FieldValue, IDFObject

THERMOSTAT = "Thermostat"
VAV_SYSTEM = "VAV_System"


class HVACSystem(StrEnum):
    IDEAL_LOADS = "ideal_loads"  # pure load calculation, no equipment
    VAV = "vav_chiller_boiler"  # central VAV, air-cooled chiller, gas boiler, HW reheat
    PTHP = "pthp"  # packaged terminal heat pump in each zone

    @property
    def description(self) -> str:
        return {
            HVACSystem.IDEAL_LOADS: "Ideal loads (no equipment): pure heating/cooling loads",
            HVACSystem.VAV: "VAV with hot-water reheat, air-cooled chiller and gas boiler",
            HVACSystem.PTHP: "Packaged terminal heat pump per zone",
        }[self]


def template(type_: str, **overrides: FieldValue) -> IDFObject:
    """A fully written object (HVACTemplate or Sizing:Zone): IDD defaults + overrides.

    Override keys are field names with spaces/dashes as underscores, case-insensitive,
    e.g. `zone_name="F1_Core"`, `reheat_coil_type="HotWater"`.
    """
    spec = TEMPLATE_FIELDS[type_]
    keys = [_key(name) for name, _ in spec]
    values: list[FieldValue] = [default for _, default in spec]
    for key, value in overrides.items():
        k = key.lower()
        if k not in keys:
            raise KeyError(f"{type_} has no field {key!r}")
        values[keys.index(k)] = value
    return IDFObject(type_, values, comments=[name for name, _ in spec])


def _key(field_name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in field_name.lower()).strip("_")


def hvac_objects(system: HVACSystem, zone_names: list[str]) -> list[IDFObject]:
    objects = [
        IDFObject("HVACTemplate:Thermostat", [THERMOSTAT, s.HEATING_SP, "", s.COOLING_SP, ""])
    ]
    if system is HVACSystem.IDEAL_LOADS:
        for z in zone_names:
            objects.append(
                template(
                    "HVACTemplate:Zone:IdealLoadsAirSystem",
                    zone_name=z,
                    template_thermostat_name=THERMOSTAT,
                )
            )
            # Unlike the equipment templates, the ideal-loads template creates no
            # Sizing:Zone, and without one EnergyPlus reports no zone design loads.
            # Supply temperatures match the template's 13 °C / 50 °C limits.
            objects.append(
                template(
                    "Sizing:Zone",
                    zone_or_zonelist_name=z,
                    zone_cooling_design_supply_air_temperature=13.0,
                    zone_cooling_design_supply_air_temperature_difference=11.11,
                    zone_heating_design_supply_air_temperature=50.0,
                    zone_heating_design_supply_air_temperature_difference=30.0,
                    zone_cooling_design_supply_air_humidity_ratio=0.0085,
                    zone_heating_design_supply_air_humidity_ratio=0.008,
                    zone_heating_sizing_factor=1.0,
                    zone_cooling_sizing_factor=1.0,
                )
            )
    elif system is HVACSystem.PTHP:
        objects += [
            template("HVACTemplate:Zone:PTHP", zone_name=z, template_thermostat_name=THERMOSTAT)
            for z in zone_names
        ]
    elif system is HVACSystem.VAV:
        objects += [
            template(
                "HVACTemplate:Zone:VAV",
                zone_name=z,
                template_vav_system_name=VAV_SYSTEM,
                template_thermostat_name=THERMOSTAT,
                reheat_coil_type="HotWater",
            )
            for z in zone_names
        ]
        objects += [
            template("HVACTemplate:System:VAV", name=VAV_SYSTEM, heating_coil_type="HotWater"),
            template("HVACTemplate:Plant:ChilledWaterLoop", name="Chilled_Water_Loop"),
            template(
                "HVACTemplate:Plant:Chiller",
                name="Chiller",
                chiller_type="ElectricReciprocatingChiller",
                nominal_cop=3.2,
                condenser_type="AirCooled",
                priority=1,
            ),
            template("HVACTemplate:Plant:HotWaterLoop", name="Hot_Water_Loop"),
            template(
                "HVACTemplate:Plant:Boiler",
                name="Boiler",
                boiler_type="HotWaterBoiler",
                efficiency=0.8,
                fuel_type="NaturalGas",
                priority=1,
            ),
        ]
    else:  # pragma: no cover
        raise ValueError(f"unsupported HVAC system {system}")
    return objects
