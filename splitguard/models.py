"""Core data types shared by SplitGuard modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .fingerprint import canonical_json

SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}


class InputError(Exception):
    """A deterministic, user-facing input error with a stable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | Path | None = None,
        line: int | None = None,
        field_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None
        self.line = line
        self.field_name = field_name

    def __str__(self) -> str:
        location: list[str] = []
        if self.path:
            location.append(self.path)
        if self.line is not None:
            location.append(f"line {self.line}")
        if self.field_name:
            location.append(f"field {self.field_name}")
        prefix = f"[{self.code}] "
        if location:
            return prefix + ", ".join(location) + ": " + self.message
        return prefix + self.message


@dataclass
class Record:
    record_id: str
    values: dict[str, str]
    line_no: int


@dataclass
class Dataset:
    path: Path
    fieldnames: tuple[str, ...]
    records: dict[str, Record]

    @property
    def ids(self) -> list[str]:
        return sorted(self.records)


@dataclass
class ManifestEntry:
    record_id: str
    split: str
    line_no: int


@dataclass
class Manifest:
    path: Path
    entries: list[ManifestEntry]
    split_column: str = "split"


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    split: str | None = None
    record_ids: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "split": self.split,
            "record_ids": sorted(set(self.record_ids)),
            "evidence": self.evidence,
            "message": self.message,
        }


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def finding_sort_key(finding: Finding) -> tuple[Any, ...]:
    """Stable global order: severity, code, then check-specific tie breaks."""
    rank = SEVERITY_RANK.get(finding.severity, 99)
    ids = tuple(sorted(set(finding.record_ids)))
    evidence = canonical_json(finding.evidence)
    if finding.code == "NEAR_DUPLICATE_CROSS_SPLIT":
        try:
            similarity = -round(float(finding.evidence.get("similarity", 0.0)), 6)
        except (TypeError, ValueError):
            similarity = 0.0
        return (rank, finding.code, similarity, ids, evidence)
    return (rank, finding.code, finding.split or "", ids, evidence)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=finding_sort_key)


def severity_counts(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"block": 0, "warn": 0, "info": 0}
    for finding in findings:
        if finding.severity in counts:
            counts[finding.severity] += 1
    return counts


def conclusion_for(findings: Iterable[Finding]) -> str:
    counts = severity_counts(findings)
    if counts["block"]:
        return "block"
    if counts["warn"]:
        return "review"
    return "pass"


@dataclass
class WrittenRun:
    run_id: str
    out_dir: Path
    report_path: Path
    markdown_path: Path
    html_path: Path
    manifest_path: Path
    ledger_path: Path
    report: dict[str, Any]
