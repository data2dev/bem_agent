"""US (IP) unit companions for SI results, so the LLM never converts numbers itself.

`with_ip_units(row)` adds e.g. `cooling_capacity_tons` next to `cooling_capacity_kw`.
Cooling is reported in tons, heating in kBtu/h (tons are a cooling unit), EUI in kBtu/ft².
"""

from __future__ import annotations

from typing import Any

KW_PER_TON = 3.517
KBTUH_PER_KW = 3.412
KBTU_FT2_PER_KWH_M2 = 3.412 / 10.764  # 1 kWh/m² = 0.317 kBtu/ft²

# SI field -> (IP field, factor, decimals)
IP_FIELDS: dict[str, tuple[str, float, int]] = {
    "peak_cooling_kw": ("peak_cooling_tons", 1 / KW_PER_TON, 1),
    "cooling_capacity_kw": ("cooling_capacity_tons", 1 / KW_PER_TON, 1),
    "peak_heating_kw": ("peak_heating_kbtuh", KBTUH_PER_KW, 0),
    "heating_capacity_kw": ("heating_capacity_kbtuh", KBTUH_PER_KW, 0),
    "site_eui_kwh_m2": ("site_eui_kbtu_ft2", KBTU_FT2_PER_KWH_M2, 1),
}


def with_ip_units(row: dict[str, Any]) -> dict[str, Any]:
    """Copy of `row` with an IP field after each SI field it knows about."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value
        if key in IP_FIELDS and isinstance(value, int | float):
            ip_key, factor, decimals = IP_FIELDS[key]
            out[ip_key] = round(value * factor, decimals)
    return out


def describe_difference(metric: str, new: float, base: float) -> str:
    """'-6.6 kW (-1.9 tons, -4.0%)' style text for new minus base."""
    diff = new - base
    pct = f", {diff / base * 100:+.1f}%" if base else ""
    if metric == "site_eui_kwh_m2":
        return f"{diff:+.1f} kWh/m² ({diff * KBTU_FT2_PER_KWH_M2:+.1f} kBtu/ft²{pct})"
    if metric in IP_FIELDS:
        ip_key, factor, decimals = IP_FIELDS[metric]
        unit = "tons" if ip_key.endswith("tons") else "kBtu/h"
        return f"{diff:+.1f} kW ({diff * factor:+.{decimals}f} {unit}{pct})"
    return f"{diff:+.1f}{pct.replace(', ', ' (')}{')' if pct else ''}"
