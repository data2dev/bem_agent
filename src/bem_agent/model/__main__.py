"""Write an IDF from the command line, e.g.:

    python -m bem_agent.model --location chicago --floors 3 --length 50 --width 30 \\
        --wwr 0.4 --hvac vav_chiller_boiler -o runs/demo/in.idf
"""

from __future__ import annotations

import argparse

from .building import BuildingSpec, write_idf
from .geometry import Geometry
from .hvac import HVACSystem


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m bem_agent.model", description=__doc__)
    p.add_argument("--name", default="Building")
    p.add_argument("--location", default="chicago")
    p.add_argument("--length", type=float, default=30.0, help="m")
    p.add_argument("--width", type=float, default=20.0, help="m")
    p.add_argument("--floors", type=int, default=1)
    p.add_argument("--wwr", type=float, default=0.3)
    p.add_argument("--orientation", type=float, default=0.0, help="degrees")
    p.add_argument("--envelope", default="typical")
    p.add_argument("--loads", default="office")
    p.add_argument("--schedules", default="office")
    p.add_argument(
        "--hvac", default=HVACSystem.IDEAL_LOADS.value, choices=[h.value for h in HVACSystem]
    )
    p.add_argument("--annual", action="store_true", help="also run the full weather year")
    p.add_argument("-o", "--output", default="in.idf")
    a = p.parse_args(argv)

    spec = BuildingSpec(
        name=a.name,
        location=a.location,
        geometry=Geometry(
            length_m=a.length,
            width_m=a.width,
            num_floors=a.floors,
            window_to_wall_ratio=a.wwr,
            orientation_deg=a.orientation,
        ),
        envelope=a.envelope,
        loads=a.loads,
        schedules=a.schedules,
        hvac=HVACSystem(a.hvac),
        run_period="annual" if a.annual else "sizing_only",
    )
    path = write_idf(spec, a.output)
    print(f"Wrote {path}  (run with: energyplus --expandobjects -w <epw> {path})")


if __name__ == "__main__":
    main()
