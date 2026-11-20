"""Validation issues and reports shared by pre-run and post-run checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Level = Literal["error", "warning", "info"]


@dataclass
class Issue:
    level: Level
    code: str
    message: str


@dataclass
class ValidationReport:
    stage: Literal["pre_run", "post_run"]
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: Level, code: str, message: str) -> None:
        self.issues.append(Issue(level, code, message))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> list[str]:
        return [i.code for i in self.issues]

    def to_dict(self) -> dict:
        return {"stage": self.stage, "ok": self.ok, "issues": [asdict(i) for i in self.issues]}
