import os
import shutil
import subprocess

import pytest

import bem_agent


def test_version():
    assert bem_agent.__version__ == "0.1.0"


@pytest.mark.energyplus
def test_energyplus_on_path():
    if not os.environ.get("ENERGYPLUS_DIR"):
        pytest.skip("ENERGYPLUS_DIR not set")
    exe = shutil.which("energyplus")
    assert exe, "energyplus executable not found on PATH"
    result = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True)
    assert "EnergyPlus" in result.stdout
