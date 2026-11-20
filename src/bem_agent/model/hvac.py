"""HVAC systems via EnergyPlus HVACTemplate objects.

HVACTemplate objects are expanded into full systems by EnergyPlus's ExpandObjects step,
so the model must be run with `energyplus --expandobjects` (`-x`). Equipment is
autosized, which is what lets the agent read back equipment capacities.
"""

from __future__ import annotations

from enum import StrEnum

from . import schedules as s
from .idf import IDFObject

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


def hvac_objects(system: HVACSystem, zone_names: list[str]) -> list[IDFObject]:
    objects = [
        IDFObject("HVACTemplate:Thermostat", [THERMOSTAT, s.HEATING_SP, "", s.COOLING_SP, ""])
    ]
    if system is HVACSystem.IDEAL_LOADS:
        objects += [
            IDFObject("HVACTemplate:Zone:IdealLoadsAirSystem", [z, THERMOSTAT]) for z in zone_names
        ]
    elif system is HVACSystem.PTHP:
        objects += [IDFObject("HVACTemplate:Zone:PTHP", [z, THERMOSTAT]) for z in zone_names]
    elif system is HVACSystem.VAV:
        objects += [
            IDFObject("HVACTemplate:Zone:VAV", [z, VAV_SYSTEM, THERMOSTAT]) for z in zone_names
        ]
        objects += [
            IDFObject("HVACTemplate:System:VAV", [VAV_SYSTEM]),
            IDFObject("HVACTemplate:Plant:ChilledWaterLoop", ["Chilled_Water_Loop"]),
            IDFObject(
                "HVACTemplate:Plant:Chiller",
                ["Chiller", "ElectricReciprocatingChiller", "autosize", 3.2, "AirCooled"],
            ),
            IDFObject("HVACTemplate:Plant:HotWaterLoop", ["Hot_Water_Loop"]),
            IDFObject(
                "HVACTemplate:Plant:Boiler",
                ["Boiler", "HotWaterBoiler", "autosize", 0.8, "NaturalGas"],
            ),
        ]
    else:  # pragma: no cover
        raise ValueError(f"unsupported HVAC system {system}")
    return objects
