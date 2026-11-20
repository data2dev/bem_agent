"""Run the reference case and (re)write tests/reference/office_chicago_ideal.json.

Only run this after an intentional modeling change, and review the diff: the
regression test exists to catch unintentional changes to the computed loads.

    ENERGYPLUS_DIR=... python scripts/update_reference.py [--print-only]
"""

import json
import sys
import tempfile
from pathlib import Path

from bem_agent.model import BuildingSpec
from bem_agent.sim import simulate

REFERENCE = (
    Path(__file__).resolve().parents[1] / "tests" / "reference" / "office_chicago_ideal.json"
)

SPEC = BuildingSpec(
    name="Reference Office Chicago",
    location="chicago",
    geometry={
        "length_m": 50,
        "width_m": 30,
        "num_floors": 3,
        "window_to_wall_ratio": 0.35,
        "orientation_deg": 0,
    },
    envelope="typical",
    loads="office",
    schedules="office",
    hvac="ideal_loads",
)
KEYS = ["peak_cooling_kw", "peak_heating_kw", "peak_cooling_w_m2", "peak_heating_w_m2"]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run = simulate(SPEC, base_dir=Path(tmp))
    if run.status not in ("completed", "completed_with_warnings"):
        sys.exit(f"Reference run failed: {json.dumps(run.summary(), indent=2)}")
    summary = run.summary()
    record = {
        "description": "3-story office in Chicago, ideal loads. Guards against drift in "
        "computed design loads.",
        "energyplus_version": "25.1",
        "rel_tolerance": 0.02,
        "spec": json.loads(SPEC.model_dump_json()),
        "expected": {k: summary[k] for k in KEYS},
    }
    text = json.dumps(record, indent=2) + "\n"
    if "--print-only" in sys.argv:
        print(text)
    else:
        REFERENCE.write_text(text)
        print(f"Wrote {REFERENCE}")


if __name__ == "__main__":
    main()
