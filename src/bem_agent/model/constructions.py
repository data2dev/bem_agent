"""Envelope constructions from a few simple presets.

Insulation is modeled as a no-mass resistance layer between mass layers, so each preset
is defined by a handful of numbers. Values are representative, not tied to a specific
energy code edition.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import geometry as g
from .idf import IDFObject


class Envelope(BaseModel):
    wall_insulation_r_si: float = Field(2.3, gt=0, le=15, description="m²·K/W")
    roof_insulation_r_si: float = Field(4.4, gt=0, le=20, description="m²·K/W")
    slab_insulation_r_si: float = Field(
        1.76, gt=0, le=10, description="m²·K/W under the ground-floor slab"
    )
    window_u_si: float = Field(2.4, ge=0.5, le=6.0, description="W/m²·K")
    window_shgc: float = Field(0.38, gt=0, lt=1)
    window_vt: float = Field(0.5, gt=0, lt=1)


ENVELOPE_PRESETS: dict[str, Envelope] = {
    "efficient": Envelope(
        wall_insulation_r_si=3.5,
        roof_insulation_r_si=6.0,
        slab_insulation_r_si=2.6,
        window_u_si=1.4,
        window_shgc=0.25,
        window_vt=0.45,
    ),
    "typical": Envelope(),
    "older": Envelope(
        wall_insulation_r_si=0.7,
        roof_insulation_r_si=1.5,
        slab_insulation_r_si=0.2,
        window_u_si=5.0,
        window_shgc=0.6,
        window_vt=0.7,
    ),
}


def _material(name, roughness, thickness, conductivity, density, specific_heat, solar_abs):
    return IDFObject(
        "Material",
        [
            name,
            roughness,
            thickness,
            conductivity,
            density,
            specific_heat,
            0.9,
            solar_abs,
            solar_abs,
        ],
    )


def construction_objects(env: Envelope) -> list[IDFObject]:
    return [
        _material("Stucco_25mm", "Smooth", 0.0254, 0.72, 1856.0, 840.0, 0.7),
        _material("Gypsum_13mm", "Smooth", 0.0127, 0.16, 800.0, 1090.0, 0.4),
        _material("Concrete_200mm", "MediumRough", 0.2, 1.73, 2243.0, 837.0, 0.65),
        _material("Roof_Membrane_10mm", "VeryRough", 0.0095, 0.16, 1121.0, 1460.0, 0.7),
        _material("Metal_Deck_1.5mm", "MediumSmooth", 0.0015, 45.0, 7680.0, 418.0, 0.6),
        IDFObject(
            "Material:NoMass",
            ["Wall_Insulation", "MediumRough", env.wall_insulation_r_si, 0.9, 0.7, 0.7],
        ),
        IDFObject(
            "Material:NoMass",
            ["Roof_Insulation", "MediumRough", env.roof_insulation_r_si, 0.9, 0.7, 0.7],
        ),
        IDFObject(
            "Material:NoMass",
            ["Slab_Insulation", "MediumRough", env.slab_insulation_r_si, 0.9, 0.7, 0.7],
        ),
        IDFObject(
            "WindowMaterial:SimpleGlazingSystem",
            ["Glazing", env.window_u_si, env.window_shgc, env.window_vt],
        ),
        IDFObject("Construction", [g.EXT_WALL, "Stucco_25mm", "Wall_Insulation", "Gypsum_13mm"]),
        IDFObject(
            "Construction", [g.ROOF, "Roof_Membrane_10mm", "Roof_Insulation", "Metal_Deck_1.5mm"]
        ),
        # Insulation below the slab; without it an 18 °C ground pulls enough heat out of
        # ground-floor core zones to cancel their internal gains.
        IDFObject("Construction", [g.GROUND_FLOOR, "Slab_Insulation", "Concrete_200mm"]),
        IDFObject("Construction", [g.INTERIOR_FLOOR, "Concrete_200mm"]),
        IDFObject("Construction", [g.INTERIOR_WALL, "Gypsum_13mm", "Gypsum_13mm"]),
        IDFObject("Construction", [g.WINDOW, "Glazing"]),
        # Ground contact temperature under the slab (EnergyPlus default is 18 °C too,
        # but declaring it avoids a warning and makes the assumption visible).
        IDFObject("Site:GroundTemperature:BuildingSurface", [18.0] * 12),
    ]
