"""Run EnergyPlus on an IDF and collect the outcome."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errfile import ErrSummary, parse_err


class EnergyPlusNotFound(RuntimeError):
    pass


@dataclass
class RunResult:
    out_dir: Path
    returncode: int | None
    elapsed_s: float
    timed_out: bool
    err: ErrSummary = field(default_factory=ErrSummary)

    @property
    def success(self) -> bool:
        return (
            not self.timed_out
            and self.returncode == 0
            and self.err.completed
            and self.err.severe == 0
        )

    @property
    def sql_path(self) -> Path:
        return self.out_dir / "eplusout.sql"

    @property
    def err_path(self) -> Path:
        return self.out_dir / "eplusout.err"


def find_energyplus() -> Path:
    """The energyplus executable: $ENERGYPLUS_DIR/energyplus, else the one on PATH."""
    eplus_dir = os.environ.get("ENERGYPLUS_DIR")
    if eplus_dir:
        candidate = Path(eplus_dir) / "energyplus"
        if candidate.is_file():
            return candidate
    found = shutil.which("energyplus")
    if found:
        return Path(found)
    raise EnergyPlusNotFound("energyplus not found: set ENERGYPLUS_DIR or add it to PATH")


def run_energyplus(
    idf: Path,
    epw: Path,
    out_dir: Path,
    *,
    expand_objects: bool = True,
    timeout_s: float = 900,
) -> RunResult:
    """Run EnergyPlus without a shell; never raises on simulation failure."""
    exe = find_energyplus()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "--weather", str(Path(epw).resolve()), "--output-directory", str(out_dir)]
    if expand_objects:
        cmd.append("--expandobjects")
    cmd.append(str(Path(idf).resolve()))
    start = time.monotonic()
    timed_out = False
    try:
        # cwd = the run folder: ExpandObjects writes temporary files (in.idf,
        # expanded.idf) to the working directory, so parallel runs sharing one
        # directory would collide.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=out_dir)
        returncode: int | None = proc.returncode
        console = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, returncode = True, None
        console = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    elapsed = time.monotonic() - start
    (out_dir / "console.log").write_text(console)
    err_file = out_dir / "eplusout.err"
    err_text = err_file.read_text(errors="replace") if err_file.exists() else console
    return RunResult(
        out_dir=out_dir,
        returncode=returncode,
        elapsed_s=round(elapsed, 2),
        timed_out=timed_out,
        err=parse_err(err_text),
    )
