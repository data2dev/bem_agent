from pathlib import Path

import pytest
from pydantic import ValidationError

from bem_agent.model import BuildingSpec, Geometry, HVACSystem, OperatingHours, build_idf
from bem_agent.model.building import build_objects
from bem_agent.model.geometry import geometry_objects, polygon_area, zones
from bem_agent.model.idf import IDFObject, find, format_value, missing_references, parse_idf
from bem_agent.model.schedules import compact_schedule, day_profile
from bem_agent.model.weather import WeatherError, design_day_objects, resolve_weather

FIXTURES = Path(__file__).parent / "fixtures"
DDY = (FIXTURES / "sample.ddy").read_text()


def site():
    return design_day_objects(DDY)


def vertices(obj: IDFObject) -> list[tuple[float, float, float]]:
    """Vertex coordinates from a surface object (after the vertex-count field)."""
    start = 11 if obj.type == "BuildingSurface:Detailed" else 9
    c = [float(v) for v in obj.fields[start:]]
    return [tuple(c[i : i + 3]) for i in range(0, len(c), 3)]


def newell_normal(pts):
    nx = ny = nz = 0.0
    for i, (x1, y1, z1) in enumerate(pts):
        x2, y2, z2 = pts[(i + 1) % len(pts)]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return nx, ny, nz


def area3d(pts):
    nx, ny, nz = newell_normal(pts)
    return 0.5 * (nx * nx + ny * ny + nz * nz) ** 0.5


# ---------- IDF basics ----------


def test_format_and_roundtrip():
    assert format_value(1.50) == "1.5"
    assert format_value(-0.0) == "0"
    assert format_value(None) == ""
    obj = IDFObject("Zone", ["Core", 0.0, None])
    parsed = parse_idf(obj.render())
    assert parsed[0].type == "Zone"
    assert parsed[0].fields == ["Core", "0", ""]


def test_missing_references_detected():
    objs = [IDFObject("Zone", ["Z1"]), IDFObject("ZoneList", ["L", "Z1", "Z2"])]
    assert missing_references(objs) == ["ZoneList 'L': zone 'Z2' not defined"]


# ---------- geometry ----------


@pytest.mark.parametrize(
    "geom, n_zones",
    [
        (Geometry(length_m=40, width_m=25, num_floors=3), 15),  # perimeter + core
        (Geometry(length_m=8, width_m=8, num_floors=2), 2),  # too small: one zone per floor
    ],
)
def test_zone_areas_sum_to_floor_area(geom, n_zones):
    zs = zones(geom)
    assert len(zs) == n_zones
    assert all(polygon_area(z.polygon) > 0 for z in zs), "polygons must be counterclockwise"
    assert sum(z.area_m2 for z in zs) == pytest.approx(geom.total_floor_area_m2)


def test_from_floor_area():
    g = Geometry.from_floor_area(2787.0, num_floors=3, aspect_ratio=1.5)
    assert g.total_floor_area_m2 == pytest.approx(2787.0)
    assert g.length_m / g.width_m == pytest.approx(1.5)


@pytest.mark.parametrize("wwr", [0.1, 0.3, 0.6])
def test_window_to_wall_ratio(wwr):
    geom = Geometry(length_m=40, width_m=25, num_floors=2, window_to_wall_ratio=wwr)
    objs, _ = geometry_objects(geom)
    walls = {
        o.name: o
        for o in find(objs, "BuildingSurface:Detailed")
        if o.fields[1] == "Wall" and o.fields[5] == "Outdoors"
    }
    windows = find(objs, "FenestrationSurface:Detailed")
    assert len(windows) == len(walls) == 8
    wall_area = sum(area3d(vertices(w)) for w in walls.values())
    window_area = sum(area3d(vertices(w)) for w in windows)
    assert window_area / wall_area == pytest.approx(wwr, rel=1e-3)
    for w in windows:  # window faces the same way as its wall
        parent = vertices(walls[w.fields[3]])
        a, b = newell_normal(vertices(w)), newell_normal(parent)
        assert sum(x * y for x, y in zip(a, b, strict=True)) > 0


def test_no_windows_when_wwr_zero():
    objs, _ = geometry_objects(Geometry(window_to_wall_ratio=0.0))
    assert not find(objs, "FenestrationSurface:Detailed")


def test_surfaces_face_outward():
    geom = Geometry(length_m=40, width_m=25, num_floors=2)
    objs, _ = geometry_objects(geom)
    cx, cy = geom.length_m / 2, geom.width_m / 2
    for s in find(objs, "BuildingSurface:Detailed"):
        pts = vertices(s)
        nx, ny, nz = newell_normal(pts)
        kind, boundary = s.fields[1], s.fields[5]
        if kind == "Floor":
            assert nz < 0, s.name
        elif kind in ("Roof", "Ceiling"):
            assert nz > 0, s.name
        elif boundary == "Outdoors":  # exterior wall: normal points away from plan center
            mx = sum(p[0] for p in pts) / 4 - cx
            my = sum(p[1] for p in pts) / 4 - cy
            assert nx * mx + ny * my > 0, s.name


def test_boundary_conditions_by_floor():
    objs, _ = geometry_objects(Geometry(num_floors=3))
    by_name = {o.name: o for o in find(objs, "BuildingSurface:Detailed")}
    assert by_name["F1_Core_Floor"].fields[5] == "Ground"
    assert by_name["F2_Core_Floor"].fields[5] == "Adiabatic"
    assert by_name["F2_Core_Ceiling"].fields[5] == "Adiabatic"
    assert by_name["F3_Core_Roof"].fields[5] == "Outdoors"


def test_geometry_validation():
    with pytest.raises(ValidationError):
        Geometry(window_to_wall_ratio=0.95)
    with pytest.raises(ValidationError):
        Geometry(num_floors=0)


# ---------- schedules ----------


def test_day_profile():
    assert day_profile((8, 18), 1.0, 0.0) == [(8, 0.0), (18, 1.0), (24, 0.0)]
    assert day_profile((8, 18), 1.0, 0.0, lead=2) == [(6, 0.0), (18, 1.0), (24, 0.0)]
    assert day_profile((0, 24), 1.0, 0.0) == [(24, 1.0)]
    assert day_profile(None, 1.0, 0.2) == [(24, 0.2)]


def test_compact_schedule_covers_all_day_types():
    fields = compact_schedule("S", "Fraction", [(24, 1.0)], [(24, 0.5)], [(24, 0.0)]).fields
    day_types = [f for f in fields if isinstance(f, str) and f.startswith("For:")]
    assert day_types == [
        "For: Weekdays",
        "For: SummerDesignDay",
        "For: WinterDesignDay",
        "For: Saturday",
        "For: AllOtherDays",
    ]


def test_operating_hours_validation():
    with pytest.raises(ValidationError):
        OperatingHours(weekdays=(18, 8))
    with pytest.raises(ValidationError):
        OperatingHours(heating_c=(50.0, 15.0))


# ---------- weather ----------


def test_design_day_selection():
    loc, heating, cooling = design_day_objects(DDY)
    assert loc.type == "Site:Location"
    assert "Htg 99.6% Condns DB" in heating.name
    assert "Wind" not in heating.name
    assert "Clg .4% Condns DB=>MWB" in cooling.name


def test_resolve_city_keeps_dotted_stem(tmp_path):
    from bem_agent.model.weather import CITIES

    folder = tmp_path / "WeatherData"
    folder.mkdir()
    for ext in ("epw", "ddy"):
        (folder / f"{CITIES['chicago']}.{ext}").write_text(DDY)
    w = resolve_weather("Chicago", eplus_dir=tmp_path)
    assert w.epw.name == "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    assert len(w.site_objects()) == 3


def test_resolve_weather_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
    with pytest.raises(WeatherError, match="ENERGYPLUS_DIR"):
        resolve_weather("chicago")
    with pytest.raises(WeatherError, match="Unknown location"):
        resolve_weather("atlantis")
    epw = tmp_path / "x.epw"
    epw.write_text("")
    with pytest.raises(WeatherError, match="not found"):
        resolve_weather(str(epw))


# ---------- HVAC templates ----------


def test_template_objects_are_complete():
    from bem_agent.model._template_fields import TEMPLATE_FIELDS
    from bem_agent.model.hvac import hvac_objects, template

    for obj in hvac_objects(HVACSystem.VAV, ["Z1", "Z2"]):
        if obj.type in TEMPLATE_FIELDS:
            assert len(obj.fields) == len(TEMPLATE_FIELDS[obj.type]), obj.type
    zone = template("HVACTemplate:Zone:VAV", zone_name="Z1", reheat_coil_type="HotWater")
    assert zone.fields[0] == "Z1"
    assert "HotWater" in zone.fields
    with pytest.raises(KeyError):
        template("HVACTemplate:Zone:VAV", not_a_field=1)


# ---------- full model ----------


@pytest.mark.parametrize("hvac", list(HVACSystem))
def test_full_model_has_no_broken_references(hvac):
    spec = BuildingSpec(geometry=Geometry(num_floors=2), hvac=hvac)
    objs = build_objects(spec, site())  # raises on broken references
    zones_ = find(objs, "Zone")
    assert len(zones_) == 10
    template = {
        HVACSystem.IDEAL_LOADS: "HVACTemplate:Zone:IdealLoadsAirSystem",
        HVACSystem.VAV: "HVACTemplate:Zone:VAV",
        HVACSystem.PTHP: "HVACTemplate:Zone:PTHP",
    }[hvac]
    assert len(find(objs, template)) == len(zones_)
    # ideal loads needs explicit zone sizing objects (the template creates none)
    expected_sizing = len(zones_) if hvac is HVACSystem.IDEAL_LOADS else 0
    assert len(find(objs, "Sizing:Zone")) == expected_sizing


def test_build_idf_text():
    spec = BuildingSpec(name="Demo Office", run_period="annual")
    text = build_idf(spec, site_objects=site())
    parsed = parse_idf(text)
    assert find(parsed, "Version")[0].fields == ["25.1"]
    assert find(parsed, "RunPeriod")
    assert find(parsed, "Output:SQLite")
    assert len(find(parsed, "SizingPeriod:DesignDay")) == 2
    assert "--expandobjects" in text


def test_presets_by_name_and_errors():
    spec = BuildingSpec(envelope="efficient", loads="retail", schedules="retail")
    assert spec.envelope.window_u_si == 1.4
    assert spec.schedules.sunday_holidays == (11, 18)
    with pytest.raises(ValidationError, match="unknown envelope preset"):
        BuildingSpec(envelope="glass_box")
    with pytest.raises(ValidationError):
        BuildingSpec(name="bad;name")
