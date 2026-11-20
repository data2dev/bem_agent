"""Parametric rectangular building geometry.

Each floor is split into four perimeter zones and a core zone (or one zone per floor if
the footprint is too small for a perimeter/core split). Interior partitions and the
floors/ceilings between stories are modeled as adiabatic, a standard simplification for
load and sizing studies. Orientation is applied through the Building object's north
axis, so the geometry itself stays axis-aligned:

    y
    ^   North facade (y = W)
    |  +-----------------+
    |  |\\      N       /|
    |  | +-------------+ |
    |  |W|    Core     |E|
    |  | +-------------+ |
    |  |/      S       \\|
    |  +-----------------+
    +------------------------> x   South facade (y = 0)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .idf import IDFObject

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]

# Construction names that geometry refers to (defined in constructions.py).
EXT_WALL = "Ext_Wall"
ROOF = "Roof"
GROUND_FLOOR = "Ground_Floor"
INTERIOR_FLOOR = "Interior_Floor"
INTERIOR_WALL = "Interior_Wall"
WINDOW = "Window_Construction"

MIN_CORE_M = 2.0  # smallest core dimension before falling back to one zone per floor


class Geometry(BaseModel):
    """Shape of a rectangular building. All dimensions in meters."""

    length_m: float = Field(30.0, gt=3, le=300, description="Footprint along the x axis")
    width_m: float = Field(20.0, gt=3, le=300, description="Footprint along the y axis")
    num_floors: int = Field(1, ge=1, le=20)
    floor_to_floor_m: float = Field(4.0, ge=2.5, le=8.0)
    window_to_wall_ratio: float = Field(0.3, ge=0.0, le=0.8)
    orientation_deg: float = Field(
        0.0, ge=0.0, lt=360.0, description="Building north axis, degrees clockwise from true north"
    )
    perimeter_depth_m: float = Field(4.57, gt=0, le=10.0, description="15 ft by default")

    @classmethod
    def from_floor_area(
        cls,
        total_floor_area_m2: float,
        num_floors: int = 1,
        aspect_ratio: float = 1.5,
        **kwargs: float,
    ) -> Geometry:
        """Size the footprint from total floor area; aspect_ratio = length / width."""
        if total_floor_area_m2 <= 0 or aspect_ratio <= 0:
            raise ValueError("total_floor_area_m2 and aspect_ratio must be positive")
        footprint = total_floor_area_m2 / num_floors
        width = math.sqrt(footprint / aspect_ratio)
        return cls(length_m=width * aspect_ratio, width_m=width, num_floors=num_floors, **kwargs)

    @property
    def footprint_area_m2(self) -> float:
        return self.length_m * self.width_m

    @property
    def total_floor_area_m2(self) -> float:
        return self.footprint_area_m2 * self.num_floors

    @property
    def perimeter_core(self) -> bool:
        return min(self.length_m, self.width_m) - 2 * self.perimeter_depth_m >= MIN_CORE_M


@dataclass
class ZoneGeometry:
    name: str
    floor: int
    polygon: list[Point2]  # counterclockwise seen from above
    exterior_edges: list[int]  # indices i of edges polygon[i] -> polygon[i+1] on the facade

    @property
    def area_m2(self) -> float:
        return polygon_area(self.polygon)


def polygon_area(poly: list[Point2]) -> float:
    """Signed shoelace area; positive for counterclockwise polygons."""
    n = len(poly)
    return 0.5 * sum(
        poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n)
    )


def floor_plan(geom: Geometry) -> list[tuple[str, list[Point2], list[int]]]:
    """Zone footprints for one floor: (suffix, CCW polygon, exterior edge indices)."""
    L, W, d = geom.length_m, geom.width_m, geom.perimeter_depth_m
    if not geom.perimeter_core:
        return [("Zone", [(0, 0), (L, 0), (L, W), (0, W)], [0, 1, 2, 3])]
    return [
        ("South", [(0, 0), (L, 0), (L - d, d), (d, d)], [0]),
        ("East", [(L, 0), (L, W), (L - d, W - d), (L - d, d)], [0]),
        ("North", [(L, W), (0, W), (d, W - d), (L - d, W - d)], [0]),
        ("West", [(0, W), (0, 0), (d, d), (d, W - d)], [0]),
        ("Core", [(d, d), (L - d, d), (L - d, W - d), (d, W - d)], []),
    ]


def zones(geom: Geometry) -> list[ZoneGeometry]:
    out = []
    for floor in range(1, geom.num_floors + 1):
        for suffix, poly, ext in floor_plan(geom):
            out.append(ZoneGeometry(f"F{floor}_{suffix}", floor, poly, ext))
    return out


def wall_vertices(a: Point2, b: Point2, z0: float, z1: float) -> list[Point3]:
    """Wall on polygon edge a -> b, counterclockwise from outside, upper-left first."""
    return [(a[0], a[1], z1), (a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1)]


def window_vertices(a: Point2, b: Point2, z0: float, z1: float, wwr: float) -> list[Point3]:
    """A centered window giving the requested window-to-wall ratio on this wall."""
    length = math.dist(a, b)
    height = z1 - z0
    width_frac = 0.9  # window spans 90% of the wall; height makes up the rest of the ratio
    win_w = length * width_frac
    win_h = height * wwr / width_frac
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    s0 = (length - win_w) / 2
    s1 = s0 + win_w
    zc = z0 + height / 2
    zb, zt = zc - win_h / 2, zc + win_h / 2
    p0 = (a[0] + ux * s0, a[1] + uy * s0)
    p1 = (a[0] + ux * s1, a[1] + uy * s1)
    return [(p0[0], p0[1], zt), (p0[0], p0[1], zb), (p1[0], p1[1], zb), (p1[0], p1[1], zt)]


def _flat(vertices: list[Point3]) -> list[float]:
    return [round(c, 4) for v in vertices for c in v]


def _surface(
    name: str,
    surface_type: str,
    construction: str,
    zone: str,
    boundary: str,
    vertices: list[Point3],
) -> IDFObject:
    exposed = boundary == "Outdoors"
    return IDFObject(
        "BuildingSurface:Detailed",
        [
            name,
            surface_type,
            construction,
            zone,
            "",  # space name
            boundary,
            "",  # outside boundary condition object
            "SunExposed" if exposed else "NoSun",
            "WindExposed" if exposed else "NoWind",
            "autocalculate",
            len(vertices),
            *_flat(vertices),
        ],
    )


def geometry_objects(geom: Geometry) -> tuple[list[IDFObject], list[str]]:
    """Zone, surface, and window objects. Returns (objects, zone names)."""
    objects: list[IDFObject] = []
    names: list[str] = []
    h = geom.floor_to_floor_m
    for z in zones(geom):
        names.append(z.name)
        objects.append(IDFObject("Zone", [z.name]))
        z0, z1 = (z.floor - 1) * h, z.floor * h
        poly = z.polygon
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            wall = f"{z.name}_Wall{i + 1}"
            exterior = i in z.exterior_edges
            objects.append(
                _surface(
                    wall,
                    "Wall",
                    EXT_WALL if exterior else INTERIOR_WALL,
                    z.name,
                    "Outdoors" if exterior else "Adiabatic",
                    wall_vertices(a, b, z0, z1),
                )
            )
            if exterior and geom.window_to_wall_ratio > 0:
                objects.append(
                    IDFObject(
                        "FenestrationSurface:Detailed",
                        [
                            f"{wall}_Window",
                            "Window",
                            WINDOW,
                            wall,
                            "",  # outside boundary condition object
                            "autocalculate",
                            "",  # frame and divider
                            1,
                            4,
                            *_flat(window_vertices(a, b, z0, z1, geom.window_to_wall_ratio)),
                        ],
                    )
                )
        ground = z.floor == 1
        top = z.floor == geom.num_floors
        floor_pts = [(x, y, z0) for x, y in reversed(poly)]  # clockwise from above = faces down
        ceil_pts = [(x, y, z1) for x, y in poly]  # counterclockwise from above = faces up
        objects.append(
            _surface(
                f"{z.name}_Floor",
                "Floor",
                GROUND_FLOOR if ground else INTERIOR_FLOOR,
                z.name,
                "Ground" if ground else "Adiabatic",
                floor_pts,
            )
        )
        objects.append(
            _surface(
                f"{z.name}_{'Roof' if top else 'Ceiling'}",
                "Roof" if top else "Ceiling",
                ROOF if top else INTERIOR_FLOOR,
                z.name,
                "Outdoors" if top else "Adiabatic",
                ceil_pts,
            )
        )
    return objects, names
