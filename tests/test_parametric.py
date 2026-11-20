import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_results import fake_eplus_sql

from bem_agent.model import BuildingSpec
from bem_agent.results import list_studies, run_sql, study_table
from bem_agent.results.plots import plot_study
from bem_agent.sim import ParametricStudy, StudyError, expand_cases, parametric, run_study

HVAC_COOLING = {"ideal_loads": 100.0, "vav_chiller_boiler": 120.0, "pthp": 110.0}


def study(**kw) -> ParametricStudy:
    params = kw.pop(
        "parameters",
        {
            "hvac": ["ideal_loads", "vav_chiller_boiler", "pthp"],
            "geometry.window_to_wall_ratio": [0.3, 0.5],
        },
    )
    return ParametricStudy(name=kw.pop("name", "HVAC x WWR"), parameters=params, **kw)


# ---------- case matrix ----------


def test_expand_cases():
    cases = expand_cases(study())
    assert len(cases) == 6
    assert cases[0].label == "Ideal / WWR 30%"
    assert cases[-1].label == "PTHP / WWR 50%"
    assert cases[3].spec.hvac == "vav_chiller_boiler"
    assert cases[3].spec.geometry.window_to_wall_ratio == 0.5
    assert cases[3].spec.name == "VAV / WWR 50%"
    # untouched base settings carry through
    assert all(c.spec.geometry.num_floors == 1 for c in cases)


def test_presets_tracked_per_case():
    s = study(
        base=BuildingSpec(envelope="efficient"),
        parameters={"envelope.window_u_si": [1.0, 2.0], "loads": ["office", "retail"]},
    )
    cases = expand_cases(s)
    assert cases[0].spec.presets["envelope"] == "custom"
    assert cases[0].spec.envelope.window_shgc == 0.25  # rest of the efficient preset kept
    assert [c.spec.presets["loads"] for c in cases[:2]] == ["office", "retail"]


def test_invalid_case_fails_before_running():
    s = study(parameters={"geometry.window_to_wall_ratio": [0.3, 0.95]})
    with pytest.raises(StudyError, match=r"case 2 \(WWR 95%\).*window_to_wall_ratio"):
        expand_cases(s)


@pytest.mark.parametrize(
    "parameters, message",
    [
        ({"geometry.secret": [1, 2]}, "Cannot vary"),
        ({"hvac": []}, "at least one"),
        ({"hvac": ["pthp", "pthp"]}, "duplicate"),
        ({"envelope": ["typical"], "envelope.window_u_si": [1.0]}, "both"),
        (
            {
                "geometry.window_to_wall_ratio": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
                "geometry.orientation_deg": [0, 45, 90, 135, 180, 225, 270, 315],
            },
            "exceeds",
        ),
    ],
)
def test_study_validation(parameters, message):
    with pytest.raises(ValidationError, match=message):
        study(parameters=parameters)


# ---------- batch runner (EnergyPlus replaced by a deterministic fake) ----------


class FakeRun:
    def __init__(self, run_id: str, out_dir: Path, status: str, summary: dict):
        self.run_id, self.out_dir, self.status, self._summary = run_id, out_dir, status, summary

    def summary(self):
        return self._summary


def fake_simulate(threads: set):
    def _simulate(spec, base_dir, timeout_s, load_results):
        assert load_results is False  # the study loads results itself, from one thread
        threads.add(threading.get_ident())
        run_id = "run_" + spec.name.replace(" / ", "_").replace(" ", "").replace("%", "")
        out = Path(base_dir) / run_id
        out.mkdir(parents=True)
        wwr = spec.geometry.window_to_wall_ratio
        failed = spec.hvac == "pthp" and wwr == 0.5
        cooling = HVAC_COOLING[spec.hvac] + 100 * wwr
        summary = {"run_id": run_id, "status": "failed" if failed else "completed"}
        if not failed:
            summary.update(
                peak_cooling_kw=cooling, peak_heating_kw=50.0, peak_cooling_w_m2=cooling / 6
            )
        (out / "spec.json").write_text(spec.model_dump_json())
        (out / "run.json").write_text(json.dumps(summary))
        if not failed:
            fake_eplus_sql(out / "eplusout.sql")
        return FakeRun(run_id, out, summary["status"], summary)

    return _simulate


@pytest.fixture
def ran(tmp_path, monkeypatch):
    threads: set = set()
    monkeypatch.setattr(parametric, "simulate", fake_simulate(threads))
    done = []
    result = run_study(
        study(), base_dir=tmp_path, max_workers=3, on_case_done=lambda c, r: done.append(c.index)
    )
    return result, tmp_path, threads, done


def test_run_study(ran):
    result, base, threads, done = ran
    assert sorted(done) == [1, 2, 3, 4, 5, 6]
    assert len(threads) > 1  # cases really ran in parallel
    table = result.table()
    assert [r["status"] for r in table].count("failed") == 1
    assert table[0]["HVAC"] == "ideal_loads" and table[0]["WWR"] == 0.3
    summary = result.summary()
    assert summary["cases"] == 6 and summary["completed"] == 5
    assert summary["failed"] == ["case 6 (PTHP / WWR 50%): failed"]
    assert summary["best"]["peak_cooling_kw"] == "Ideal / WWR 30%"
    effects = summary["main_effects"]
    # WWR effect averaged over HVAC systems: 30% -> (130+150+140)/3, 50% -> (150+170)/2
    assert effects["WWR"]["peak_cooling_kw"] == {"30%": 140.0, "50%": 160.0}
    assert effects["HVAC"]["peak_cooling_kw"] == {"Ideal": 140.0, "VAV": 160.0, "PTHP": 140.0}
    saved = json.loads((base / "studies" / result.study_id / "summary.json").read_text())
    assert len(saved["table"]) == 6


def test_study_in_results_db(ran):
    result, base, _, _ = ran
    db = base / "results.db"
    assert list_studies(db)[0]["study_id"] == result.study_id
    rows = study_table(db, result.study_id)
    assert [r["case"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[3]["overrides"] == {
        "hvac": "vav_chiller_boiler",
        "geometry.window_to_wall_ratio": 0.5,
    }
    assert rows[3]["peak_cooling_kw"] == 170.0
    by_cooling = study_table(db, result.study_id, sort_by="peak_cooling_kw")
    assert by_cooling[0]["label"] == "Ideal / WWR 30%"
    assert by_cooling[-1]["peak_cooling_kw"] is None  # failed case sorts last
    # the agent can join studies to results with plain SQL
    q = run_sql(
        db,
        "SELECT c.label, r.status FROM study_cases c JOIN runs r USING (run_id) "
        f"WHERE c.study_id = '{result.study_id}' AND r.status = 'failed'",
    )
    assert q["rows"] == [["PTHP / WWR 50%", "failed"]]


def test_plot_study(ran, tmp_path):
    result, base, _, _ = ran
    png = plot_study(
        base / "results.db", result.study_id, "peak_cooling_kw", tmp_path / "study.png"
    )
    assert png.read_bytes()[:4] == b"\x89PNG"
    csv_rows = png.with_suffix(".csv").read_text().splitlines()
    assert len(csv_rows) == 6  # header + 5 completed cases
