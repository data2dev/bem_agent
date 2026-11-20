"""Parse EnergyPlus's eplusout.err file."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUMMARY = re.compile(
    r"EnergyPlus (Completed Successfully|Terminated--Fatal Error Detected)"
    r"[-.\s]*(\d+) Warning;\s*(\d+) Severe Errors"
)
_START = re.compile(r"^\s*\*\*\s*(Warning|Severe|Fatal)\s*\*\*\s?(.*)$")
_CONTINUE = re.compile(r"^\s*\*\*\s*~~~\s*\*\*\s?(.*)$")


@dataclass
class ErrMessage:
    level: str  # "Warning" | "Severe" | "Fatal"
    text: str

    def short(self, limit: int = 300) -> str:
        t = " ".join(self.text.split())
        return t if len(t) <= limit else t[: limit - 3] + "..."


@dataclass
class ErrSummary:
    completed: bool = False
    fatal: bool = False
    warnings: int = 0
    severe: int = 0
    messages: list[ErrMessage] = field(default_factory=list)
    found_summary: bool = False

    def by_level(self, level: str) -> list[ErrMessage]:
        return [m for m in self.messages if m.level == level]


def parse_err(text: str) -> ErrSummary:
    summary = ErrSummary()
    current: ErrMessage | None = None
    for line in text.splitlines():
        start = _START.match(line)
        if start:
            current = ErrMessage(start.group(1), start.group(2).strip())
            summary.messages.append(current)
            continue
        cont = _CONTINUE.match(line)
        if cont and current is not None:
            current.text += "\n" + cont.group(1).strip()
            continue
        current = None
    matches = list(_SUMMARY.finditer(text))
    if matches:
        last = matches[-1]
        summary.found_summary = True
        summary.completed = last.group(1) == "Completed Successfully"
        summary.fatal = not summary.completed
        summary.warnings = int(last.group(2))
        summary.severe = int(last.group(3))
    else:
        summary.fatal = any(m.level == "Fatal" for m in summary.messages)
        summary.warnings = len(summary.by_level("Warning"))
        summary.severe = len(summary.by_level("Severe"))
    return summary
