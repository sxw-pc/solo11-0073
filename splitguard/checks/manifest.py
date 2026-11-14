"""Split manifest completeness, integrity and ratio checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..models import Dataset, Finding, Manifest


def _round6(value: float) -> float:
    return round(value, 6)


@dataclass
class ManifestAnalysis:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    assignments: dict[str, str] = field(default_factory=dict)
    split_counts: dict[str, int] = field(default_factory=dict)


def _entry_evidence(entries: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": entry.record_id,
            "split": entry.split,
            "line": entry.line_no,
        }
        for entry in entries
    ]


def run_manifest_checks(
    dataset: Dataset, manifest: Manifest, config: Config
) -> ManifestAnalysis:
    entries_by_id: dict[str, list[Any]] = defaultdict(list)
    for entry in manifest.entries:
        entries_by_id[entry.record_id].append(entry)

    expected = list(config.expected_splits)
    dataset_ids = set(dataset.records)
    all_manifest_ids = set(entries_by_id)
    nonempty_manifest_ids = {value for value in all_manifest_ids if value}

    empty_id_entries = entries_by_id.get("", [])
    unknown_ids = sorted(nonempty_manifest_ids - dataset_ids)
    duplicate_ids = sorted(
        record_id
        for record_id, entries in entries_by_id.items()
        if record_id and len(entries) > 1
    )
    missing_ids = sorted(dataset_ids - all_manifest_ids)

    empty_split_entries = [entry for entry in manifest.entries if not entry.split]
    unknown_split_entries = [
        entry for entry in manifest.entries if entry.split and entry.split not in expected
    ]
    unknown_split_values = sorted({entry.split for entry in unknown_split_entries})

    assignments: dict[str, str] = {}
    for record_id in dataset.ids:
        entries = entries_by_id.get(record_id, [])
        if len(entries) == 1:
            entry = entries[0]
            if entry.split and entry.split in expected:
                assignments[record_id] = entry.split

    split_counts = {split: 0 for split in expected}
    for split in assignments.values():
        split_counts[split] += 1
    assigned_count = len(assignments)
    split_ratios = {
        split: _round6(count / assigned_count) if assigned_count else 0.0
        for split, count in split_counts.items()
    }
    expected_ratios = {
        split: config.expected_ratios[split]
        for split in expected
        if split in (config.expected_ratios or {})
    }
    deltas = {
        split: _round6(split_ratios[split] - ratio)
        for split, ratio in expected_ratios.items()
    }

    findings: list[Finding] = []
    if empty_id_entries:
        findings.append(
            Finding(
                code="MANIFEST_EMPTY_RECORD_ID",
                severity="block",
                message=f"切分清单有 {len(empty_id_entries)} 行记录 ID 为空",
                record_ids=[],
                evidence={
                    "count": len(empty_id_entries),
                    "entries": _entry_evidence(empty_id_entries),
                },
            )
        )
    if unknown_ids:
        findings.append(
            Finding(
                code="MANIFEST_UNKNOWN_RECORD",
                severity="block",
                message=f"切分清单包含 {len(unknown_ids)} 个数据集中不存在的记录 ID",
                record_ids=unknown_ids,
                evidence={
                    "count": len(unknown_ids),
                    "entries": _entry_evidence(
                        [entry for value in unknown_ids for entry in entries_by_id[value]]
                    ),
                },
            )
        )
    if duplicate_ids:
        duplicate_evidence = []
        for record_id in duplicate_ids:
            entries = entries_by_id[record_id]
            duplicate_evidence.append(
                {
                    "record_id": record_id,
                    "splits": sorted(entry.split for entry in entries),
                    "lines": sorted(entry.line_no for entry in entries),
                }
            )
        findings.append(
            Finding(
                code="MANIFEST_DUPLICATE_ASSIGNMENT",
                severity="block",
                message=f"{len(duplicate_ids)} 个记录 ID 在切分清单中出现了多次",
                record_ids=duplicate_ids,
                evidence={
                    "count": len(duplicate_ids),
                    "assignments": duplicate_evidence,
                },
            )
        )
    if empty_split_entries:
        affected = sorted({entry.record_id for entry in empty_split_entries if entry.record_id})
        findings.append(
            Finding(
                code="MANIFEST_EMPTY_SPLIT_VALUE",
                severity="block",
                message=f"切分清单有 {len(empty_split_entries)} 行切分值为空",
                record_ids=affected,
                evidence={
                    "count": len(empty_split_entries),
                    "entries": _entry_evidence(empty_split_entries),
                },
            )
        )
    if unknown_split_entries:
        affected = sorted(
            {entry.record_id for entry in unknown_split_entries if entry.record_id}
        )
        findings.append(
            Finding(
                code="MANIFEST_UNKNOWN_SPLIT",
                severity="block",
                message="切分清单包含未知切分: " + ", ".join(unknown_split_values),
                record_ids=affected,
                evidence={
                    "unknown_splits": unknown_split_values,
                    "count": len(unknown_split_entries),
                    "entries": _entry_evidence(unknown_split_entries),
                },
            )
        )
    if missing_ids:
        findings.append(
            Finding(
                code="MANIFEST_MISSING_ASSIGNMENT",
                severity="block",
                message=f"{len(missing_ids)} 条数据集记录在切分清单中没有归属",
                record_ids=missing_ids,
                evidence={"count": len(missing_ids)},
            )
        )
    for split in expected:
        if split_counts[split] == 0:
            findings.append(
                Finding(
                    code="SPLIT_EMPTY",
                    severity=config.empty_split_severity,
                    message=f"期望切分 {split} 没有任何有效记录",
                    split=split,
                    record_ids=[],
                    evidence={
                        "split": split,
                        "assigned_records": assigned_count,
                        "severity_policy": config.empty_split_severity,
                    },
                )
            )
    for split, ratio in expected_ratios.items():
        delta = split_ratios[split] - ratio
        if abs(delta) > config.ratio_tolerance:
            findings.append(
                Finding(
                    code="SPLIT_RATIO_DRIFT",
                    severity="warn",
                    message=(
                        f"切分 {split} 实际占比 {split_ratios[split]:.4f} 与期望 "
                        f"{ratio:.4f} 相差 {delta:+.4f}"
                    ),
                    split=split,
                    record_ids=[],
                    evidence={
                        "split": split,
                        "expected_ratio": ratio,
                        "actual_ratio": split_ratios[split],
                        "delta": _round6(delta),
                        "tolerance": config.ratio_tolerance,
                        "count": split_counts[split],
                    },
                )
            )

    metrics: dict[str, Any] = {
        "total_records": len(dataset.records),
        "assigned_records": assigned_count,
        "split_counts": split_counts,
        "split_ratios": split_ratios,
        "missing_ids_count": len(missing_ids),
        "unknown_ids_count": len(unknown_ids),
        "duplicate_assignment_count": len(duplicate_ids),
        "manifest_entry_count": len(manifest.entries),
        "empty_record_id_count": len(empty_id_entries),
        "empty_split_value_count": len(empty_split_entries),
        "unknown_split_values": unknown_split_values,
        "expected_ratios": expected_ratios,
        "ratio_deltas": deltas,
        "ratio_tolerance": config.ratio_tolerance,
    }
    return ManifestAnalysis(findings, metrics, assignments, split_counts)
