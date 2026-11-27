import pytest

from bem_agent.agent import TRUNCATED_NOTE, Agent
from bem_agent.llm import LLMResponse, Usage
from bem_agent.sim import ParametricStudy, expand_cases
from bem_agent.sim.parametric import comparisons
from bem_agent.units import describe_difference, with_ip_units


def test_ip_units_added_next_to_si():
    row = with_ip_units(
        {
            "run_id": "r1",
            "cooling_capacity_kw": 165.3,
            "heating_capacity_kw": 142.8,
            "site_eui_kwh_m2": 190.2,
            "status": "completed",
        }
    )
    assert row["cooling_capacity_tons"] == 47.0
    assert row["heating_capacity_kbtuh"] == 487
    assert row["site_eui_kbtu_ft2"] == 60.3
    assert list(row)[:3] == ["run_id", "cooling_capacity_kw", "cooling_capacity_tons"]
    assert not any(k.startswith("heating") and k.endswith("tons") for k in row)


def test_describe_difference():
    # PTHP 158.7 kW vs VAV 165.3 kW: the numbers from the 2026-11-27 recording
    assert describe_difference("cooling_capacity_kw", 158.7, 165.3) == "-6.6 kW (-1.9 tons, -4.0%)"
    assert (
        describe_difference("heating_capacity_kw", 158.7, 142.8) == "+15.9 kW (+54 kBtu/h, +11.1%)"
    )
    assert (
        describe_difference("site_eui_kwh_m2", 92.8, 190.2)
        == "-97.4 kWh/m² (-30.9 kBtu/ft², -51.2%)"
    )


def test_study_comparisons_hold_other_parameters_fixed():
    study = ParametricStudy(
        name="HVAC x WWR",
        parameters={
            "hvac": ["vav_chiller_boiler", "pthp"],
            "geometry.window_to_wall_ratio": [0.3, 0.5],
        },
    )
    cases = expand_cases(study)
    cap = {
        "VAV / WWR 30%": 165.3,
        "PTHP / WWR 30%": 158.7,
        "VAV / WWR 50%": 190.8,
        "PTHP / WWR 50%": 185.0,
    }
    rows = [{"case": c.index, "label": c.label, "cooling_capacity_kw": cap[c.label]} for c in cases]
    comps = comparisons(study, cases, rows)
    assert comps["PTHP / WWR 30% vs VAV / WWR 30%"] == {
        "cooling_capacity_kw": "-6.6 kW (-1.9 tons, -4.0%)"
    }
    assert comps["PTHP / WWR 50% vs VAV / WWR 50%"]["cooling_capacity_kw"].endswith("-3.0%)")
    assert comps["VAV / WWR 50% vs VAV / WWR 30%"]["cooling_capacity_kw"].startswith("+25.5 kW")
    assert len(comps) == 4
    # a failed case has no row, so nothing is compared against it
    assert len(comparisons(study, cases, rows[1:])) == 2


class OneShot:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason

    def chat(self, messages, tools, max_tokens):
        return LLMResponse(
            text="The PTHP needs", tool_calls=[], stop_reason=self.stop_reason, usage=Usage(10, 10)
        )


@pytest.mark.parametrize("reason, cut", [("length", True), ("max_tokens", True), ("stop", False)])
def test_truncated_answers_are_marked(tmp_path, reason, cut):
    from bem_agent.tools import ToolContext

    answer = Agent(OneShot(reason), ToolContext(base_dir=tmp_path)).run("q").answer
    assert answer.startswith("The PTHP needs")
    assert (TRUNCATED_NOTE in answer) is cut
