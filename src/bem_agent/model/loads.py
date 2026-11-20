"""Internal loads (people, lights, equipment) and infiltration, applied to all zones."""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import schedules as s
from .idf import IDFObject

ALL_ZONES = "All_Zones"


class InternalLoads(BaseModel):
    people_per_m2: float = Field(0.054, ge=0, le=1.0, description="~5 people per 1,000 ft²")
    lighting_w_per_m2: float = Field(8.0, ge=0, le=50)
    equipment_w_per_m2: float = Field(8.0, ge=0, le=100)
    infiltration_m3s_per_m2_ext: float = Field(
        0.0003, ge=0, le=0.005, description="Per m² of exterior surface area"
    )


LOAD_PRESETS: dict[str, InternalLoads] = {
    "office": InternalLoads(),
    "retail": InternalLoads(people_per_m2=0.16, lighting_w_per_m2=12.0, equipment_w_per_m2=3.0),
    "data_heavy_office": InternalLoads(equipment_w_per_m2=20.0),
}


def load_objects(loads: InternalLoads, zone_names: list[str]) -> list[IDFObject]:
    return [
        IDFObject("ZoneList", [ALL_ZONES, *zone_names]),
        IDFObject(
            "People",
            [
                "People",
                ALL_ZONES,
                s.OCCUPANCY,
                "People/Area",
                "",
                loads.people_per_m2,
                "",
                0.3,  # fraction radiant
                "autocalculate",  # sensible heat fraction
                s.ACTIVITY,
            ],
        ),
        IDFObject(
            "Lights",
            [
                "Lights",
                ALL_ZONES,
                s.LIGHTING,
                "Watts/Area",
                "",
                loads.lighting_w_per_m2,
                "",
                0.0,  # return air fraction
                0.42,  # fraction radiant
                0.18,  # fraction visible
                1.0,  # fraction replaceable
            ],
        ),
        IDFObject(
            "ElectricEquipment",
            [
                "Equipment",
                ALL_ZONES,
                s.EQUIPMENT,
                "Watts/Area",
                "",
                loads.equipment_w_per_m2,
                "",
                0.0,  # fraction latent
                0.5,  # fraction radiant
                0.0,  # fraction lost
            ],
        ),
        IDFObject(
            "ZoneInfiltration:DesignFlowRate",
            [
                "Infiltration",
                ALL_ZONES,
                s.ALWAYS_ON,
                "Flow/ExteriorArea",
                "",
                "",
                loads.infiltration_m3s_per_m2_ext,
                "",
                1.0,
                0.0,
                0.0,
                0.0,
            ],
        ),
    ]
