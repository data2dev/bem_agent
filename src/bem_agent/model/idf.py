"""Minimal IDF (EnergyPlus input file) objects: render, parse, and reference checks.

We write IDF text directly instead of depending on eppy, which needs EnergyPlus's IDD
file at import time. EnergyPlus itself is the final check on validity (see the
`energyplus` tests run in CI).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

FieldValue = str | int | float | None


@dataclass
class IDFObject:
    type: str
    fields: list[FieldValue]
    comments: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str(self.fields[0]) if self.fields else ""

    def render(self) -> str:
        if not self.fields:
            return f"{self.type};\n"
        lines = [f"{self.type},"]
        last = len(self.fields) - 1
        for i, value in enumerate(self.fields):
            sep = ";" if i == last else ","
            text = f"  {format_value(value)}{sep}"
            comment = self.comments[i] if i < len(self.comments) else ""
            if comment:
                text = f"{text:<30} !- {comment}"
            lines.append(text)
        return "\n".join(lines) + "\n"


def format_value(value: FieldValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text
    return str(value)


def render_idf(objects: Iterable[IDFObject], header: str = "") -> str:
    parts = []
    if header:
        parts.append("".join(f"! {line}\n" for line in header.splitlines()))
    parts.extend(obj.render() for obj in objects)
    return "\n".join(parts)


_COMMENT = re.compile(r"!.*$", re.MULTILINE)


def parse_idf(text: str) -> list[IDFObject]:
    """Parse IDF text into objects (comments dropped, field values kept as strings)."""
    body = _COMMENT.sub("", text)
    objects = []
    for chunk in body.split(";"):
        parts = [p.strip() for p in chunk.split(",")]
        if not parts or not parts[0]:
            continue
        objects.append(IDFObject(type=parts[0], fields=list(parts[1:])))
    return objects


def find(objects: Sequence[IDFObject], type_: str) -> list[IDFObject]:
    t = type_.lower()
    return [o for o in objects if o.type.lower() == t]


# Fields that reference other objects by name, checked by `missing_references`.
# (object type, field index, referenced object types)
_SCHEDULE_TYPES = ("Schedule:Compact", "Schedule:Constant")
_REFERENCES: list[tuple[str, int, tuple[str, ...]]] = [
    ("BuildingSurface:Detailed", 2, ("Construction",)),
    ("BuildingSurface:Detailed", 3, ("Zone",)),
    ("FenestrationSurface:Detailed", 2, ("Construction",)),
    ("FenestrationSurface:Detailed", 3, ("BuildingSurface:Detailed",)),
    ("People", 1, ("ZoneList", "Zone")),
    ("People", 2, _SCHEDULE_TYPES),
    ("People", 9, _SCHEDULE_TYPES),
    ("Lights", 1, ("ZoneList", "Zone")),
    ("Lights", 2, _SCHEDULE_TYPES),
    ("ElectricEquipment", 1, ("ZoneList", "Zone")),
    ("ElectricEquipment", 2, _SCHEDULE_TYPES),
    ("ZoneInfiltration:DesignFlowRate", 1, ("ZoneList", "Zone")),
    ("ZoneInfiltration:DesignFlowRate", 2, _SCHEDULE_TYPES),
    ("HVACTemplate:Thermostat", 1, _SCHEDULE_TYPES),
    ("HVACTemplate:Thermostat", 3, _SCHEDULE_TYPES),
    ("HVACTemplate:Zone:IdealLoadsAirSystem", 0, ("Zone",)),
    ("HVACTemplate:Zone:IdealLoadsAirSystem", 1, ("HVACTemplate:Thermostat",)),
    ("HVACTemplate:Zone:VAV", 0, ("Zone",)),
    ("HVACTemplate:Zone:VAV", 1, ("HVACTemplate:System:VAV",)),
    ("HVACTemplate:Zone:VAV", 2, ("HVACTemplate:Thermostat",)),
    ("HVACTemplate:Zone:PTHP", 0, ("Zone",)),
    ("HVACTemplate:Zone:PTHP", 1, ("HVACTemplate:Thermostat",)),
]


def missing_references(objects: Sequence[IDFObject]) -> list[str]:
    """Return a message for each name reference that points at no defined object.

    Also checks Construction layers and ZoneList members.
    """
    names: dict[str, set[str]] = {}
    for o in objects:
        names.setdefault(o.type.lower(), set()).add(o.name.lower())

    def defined(name: str, types: Iterable[str]) -> bool:
        return any(name.lower() in names.get(t.lower(), set()) for t in types)

    problems = []
    for type_, idx, targets in _REFERENCES:
        for o in find(objects, type_):
            if idx >= len(o.fields):
                continue
            value = format_value(o.fields[idx])
            if value and not defined(value, targets):
                problems.append(f"{type_} {o.name!r}: field {idx} -> {value!r} not defined")
    layer_types = ("Material", "Material:NoMass", "WindowMaterial:SimpleGlazingSystem")
    for o in find(objects, "Construction"):
        for layer in o.fields[1:]:
            if not defined(format_value(layer), layer_types):
                problems.append(f"Construction {o.name!r}: layer {layer!r} not defined")
    for o in find(objects, "ZoneList"):
        for zone in o.fields[1:]:
            if not defined(format_value(zone), ("Zone",)):
                problems.append(f"ZoneList {o.name!r}: zone {zone!r} not defined")
    return problems
