#!/usr/bin/env bash
# Runs EnergyPlus's bundled single-zone example model and checks it completes.
# Requires ENERGYPLUS_DIR to point at an extracted EnergyPlus install.
set -euo pipefail

: "${ENERGYPLUS_DIR:?Set ENERGYPLUS_DIR to the EnergyPlus install directory}"

IDF="$ENERGYPLUS_DIR/ExampleFiles/1ZoneUncontrolled.idf"
EPW="$ENERGYPLUS_DIR/WeatherData/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
OUT="${1:-/tmp/eplus-smoke}"

rm -rf "$OUT"
mkdir -p "$OUT"

energyplus --weather "$EPW" --output-directory "$OUT" "$IDF"

if grep -q "EnergyPlus Completed Successfully" "$OUT/eplusout.err"; then
  echo "Smoke test passed."
  grep "EnergyPlus Completed Successfully" "$OUT/eplusout.err"
else
  echo "Smoke test failed. eplusout.err:"
  cat "$OUT/eplusout.err"
  exit 1
fi
