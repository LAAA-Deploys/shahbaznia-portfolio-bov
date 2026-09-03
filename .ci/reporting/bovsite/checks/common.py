"""Shared reporting and CLI helpers for BOV validators."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterable

from reporting.bovsite.contracts.loader import ContractError
from reporting.bovsite.contracts.workspace import DealWorkspace, WorkspaceError


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: str = "error"


@dataclass
class CheckReport:
    name: str
    findings: list[Finding] = field(default_factory=list)

    def error(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message, "error"))

    def warn(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message, "warning"))

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "check": self.name,
            "ok": self.ok,
            "errors": [finding.__dict__ for finding in self.errors],
            "warnings": [finding.__dict__ for finding in self.warnings],
        }

    def render(self) -> str:
        lines = [f"BOV {self.name} check: {'PASS' if self.ok else 'FAIL'}"]
        for finding in self.findings:
            lines.append(
                f"  {finding.severity.upper()} {finding.code}: {finding.message}"
            )
        if not self.findings:
            lines.append("  no findings")
        return "\n".join(lines)


def contract_failure(name: str, exc: Exception) -> CheckReport:
    report = CheckReport(name)
    report.error("contract", str(exc))
    return report


def run_cli(name: str, checker, argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=f"Run the BOV {name} check")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        report = checker(DealWorkspace.from_path(args.workspace))
    except (ContractError, WorkspaceError, OSError, ValueError) as exc:
        report = contract_failure(name, exc)
    print(json.dumps(report.as_dict(), indent=2) if args.as_json else report.render())
    return 0 if report.ok else 1
