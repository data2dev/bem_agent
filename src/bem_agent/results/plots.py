"""Static PNG charts for comparing runs (matplotlib, headless).

Follows a small chart spec: one y-axis per chart, 2 px lines, thin bars, recessive
grid, a legend whenever there are 2+ series, categorical colors assigned in fixed
order by run (never by rank). Each chart also writes a CSV beside it as the table
view of the same numbers.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from .queries import Freq, QueryError, compare_runs, timeseries  # noqa: E402

# Validated categorical palette (light mode), fixed order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
MAX_SERIES = len(SERIES)
UNIT_LABELS = {"C": "°C"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

METRIC_LABELS = {
    "peak_cooling_kw": "Peak cooling load (kW)",
    "peak_heating_kw": "Peak heating load (kW)",
    "peak_cooling_w_m2": "Peak cooling load (W/m²)",
    "peak_heating_w_m2": "Peak heating load (W/m²)",
    "site_eui_kwh_m2": "Site EUI (kWh/m²·yr)",
    "unmet_heating_h": "Unmet heating hours",
    "unmet_cooling_h": "Unmet cooling hours",
}


def _style(ax, title: str, ylabel: str, legend_above: bool = False) -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=TEXT, fontsize=12, pad=26 if legend_above else 12)
    ax.set_ylabel(ylabel, color=TEXT_SECONDARY, fontsize=9)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _legend(ax, n: int) -> None:
    """Legend in one row above the plot area, so it never covers data."""
    if n >= 2:
        ax.legend(
            frameon=False,
            fontsize=8,
            labelcolor=TEXT_SECONDARY,
            loc="lower left",
            bbox_to_anchor=(0, 1.0),
            ncol=min(n, 4),
            handlelength=1.5,
            borderaxespad=0.3,
        )


def _labels(rows: list[dict]) -> list[str]:
    """Short series labels: run name, disambiguated by run_id suffix if names repeat."""
    names = [r["name"] for r in rows]
    return [
        n if names.count(n) == 1 else f"{n} ({r['run_id'][-6:]})"
        for n, r in zip(names, rows, strict=True)
    ]


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.with_suffix(".csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def plot_metric_comparison(db_path: Path, run_ids: list[str], metric: str, out: Path) -> Path:
    """Bar chart of one headline metric across runs."""
    if metric not in METRIC_LABELS:
        raise QueryError(f"metric must be one of {list(METRIC_LABELS)}")
    if len(run_ids) > MAX_SERIES:
        raise QueryError(f"At most {MAX_SERIES} runs per chart")
    rows = compare_runs(db_path, run_ids)
    labels = _labels(rows)
    values = [r[metric] for r in rows]
    if any(v is None for v in values):
        raise QueryError(f"{metric} is missing for some runs (EUI/unmet hours need annual runs)")
    fig, ax = plt.subplots(figsize=(max(4.5, 1.1 * len(rows) + 2), 3.6), dpi=150)
    bars = ax.bar(
        range(len(rows)),
        values,
        width=0.6,
        color=[SERIES[i] for i in range(len(rows))],
        edgecolor=SURFACE,
        linewidth=2,
    )
    ax.bar_label(bars, labels=[f"{v:,.1f}" for v in values], fontsize=8, color=TEXT, padding=3)
    ax.set_xticks(
        range(len(rows)),
        labels,
        rotation=20 if len(rows) > 3 else 0,
        ha="right" if len(rows) > 3 else "center",
    )
    _style(ax, METRIC_LABELS[metric], METRIC_LABELS[metric].split("(")[-1].rstrip(")"))
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    _write_csv(out, ["run_id", "name", metric], [[r["run_id"], r["name"], r[metric]] for r in rows])
    return out


def plot_timeseries(
    db_path: Path,
    run_ids: list[str],
    variable: str,
    out: Path,
    freq: Freq = "monthly",
    key: str = "Facility",
) -> Path:
    """Line chart of an aggregated hourly series, one line per run."""
    if freq not in ("monthly", "daily", "hourly"):
        raise QueryError("freq must be monthly, daily or hourly")
    if len(run_ids) > MAX_SERIES:
        raise QueryError(f"At most {MAX_SERIES} runs per chart")
    runs = compare_runs(db_path, run_ids)
    labels = _labels(runs)
    fig, ax = plt.subplots(figsize=(7.5, 3.6), dpi=150)
    table: list[list] = []
    units = ""
    for i, (run, label) in enumerate(zip(runs, labels, strict=True)):
        series = timeseries(db_path, run["run_id"], variable, freq=freq, key=key)
        units = series[0]["units"] if series else units
        values = [p["value"] for p in series]
        xs = [p["month"] for p in series] if freq == "monthly" else range(1, len(values) + 1)
        ax.plot(
            xs,
            values,
            color=SERIES[i],
            linewidth=2,
            label=label,
            marker="o" if freq == "monthly" else None,
            markersize=4,
        )
        for idx, p in enumerate(series):
            table.append([run["run_id"], label, idx + 1, p["value"], units])
    if freq == "monthly":
        ax.set_xticks(range(1, 13), MONTHS)
        xlabel = ""
    else:
        xlabel = "Day of year" if freq == "daily" else "Hour of year"
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel(xlabel, color=TEXT_SECONDARY, fontsize=9)
    agg_word = "total" if units == "kWh" else "mean"
    _style(ax, f"{variable} ({freq} {agg_word})", UNIT_LABELS.get(units, units), len(runs) >= 2)
    _legend(ax, len(runs))
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    _write_csv(out, ["run_id", "label", "period", "value", "units"], table)
    return out


def plot_study(db_path: Path, study_id: str, metric: str, out: Path) -> Path:
    """Horizontal bars, one per study case, sorted by the metric (lowest first).

    A single series (every bar is the same quantity), so one color and no legend; case
    labels sit on the axis. Scales to every case a study can have.
    """
    from .queries import study_table

    if metric not in METRIC_LABELS:
        raise QueryError(f"metric must be one of {list(METRIC_LABELS)}")
    rows = [r for r in study_table(db_path, study_id) if r.get(metric) is not None]
    if not rows:
        raise QueryError(f"No completed cases with {metric} (EUI/unmet hours need annual runs)")
    rows.sort(key=lambda r: r[metric], reverse=True)  # barh draws bottom-up: lowest on top
    labels = [r["label"] for r in rows]
    values = [r[metric] for r in rows]
    fig, ax = plt.subplots(figsize=(7.5, 1.0 + 0.38 * len(rows)), dpi=150)
    bars = ax.barh(
        range(len(rows)), values, height=0.62, color=SERIES[0], edgecolor=SURFACE, linewidth=2
    )
    ax.bar_label(bars, labels=[f"{v:,.1f}" for v in values], fontsize=8, color=TEXT, padding=3)
    ax.set_yticks(range(len(rows)), labels)
    _style(ax, METRIC_LABELS[metric], "")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(True)
    ax.spines["left"].set_color(GRID)
    ax.margins(x=0.12)
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    _write_csv(
        out,
        ["case", "label", "run_id", metric],
        [[r["case"], r["label"], r["run_id"], r[metric]] for r in rows],
    )
    return out
