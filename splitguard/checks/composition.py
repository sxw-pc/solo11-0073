"""Label distribution, missing label and small-split checks."""

from __future__ import annotations

from collections import Counter

from ..config import Config
from ..models import CheckResult, Dataset, Finding

_LABEL_MISSING = "LABEL_MISSING_IN_SPLIT"


def _round6(value: float) -> float:
    return round(value, 6)


def run_composition_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    split_sizes = {split: 0 for split in config.expected_splits}
    for split in assignments.values():
        if split in split_sizes:
            split_sizes[split] += 1

    metrics: dict[str, object] = {
        "enabled": config.label_column is not None,
        "reference_split": config.label_reference_split,
        "small_split_threshold": config.small_split_threshold,
        "counts": {},
        "ratios": {},
    }
    findings: list[Finding] = []

    for split, size in split_sizes.items():
        if 0 < size < config.small_split_threshold:
            findings.append(
                Finding(
                    code="SPLIT_SMALL_SAMPLE",
                    severity="info",
                    message=(
                        f"切分 {split} 仅包含 {size} 条记录，低于小样本阈值 "
                        f"{config.small_split_threshold}"
                    ),
                    split=split,
                    record_ids=[],
                    evidence={
                        "split": split,
                        "record_count": size,
                        "threshold": config.small_split_threshold,
                    },
                )
            )

    if not config.label_column:
        return CheckResult(findings, metrics)

    counters: dict[str, Counter[str]] = {
        split: Counter() for split in config.expected_splits
    }
    for record_id in sorted(assignments):
        split = assignments[record_id]
        if split not in counters:
            continue
        label = dataset.records[record_id].values.get(config.label_column, "")
        counters[split][label] += 1

    counts: dict[str, dict[str, int]] = {}
    ratios: dict[str, dict[str, float]] = {}
    for split in config.expected_splits:
        size = split_sizes[split]
        counts[split] = {
            label: counters[split][label] for label in sorted(counters[split])
        }
        ratios[split] = {
            label: _round6(value / size) if size else 0.0
            for label, value in counts[split].items()
        }

    reference = config.label_reference_split
    if reference in counts:
        reference_counts = counts[reference]
        reference_labels = sorted(
            label for label, count in reference_counts.items() if count > 0
        )
        for target in config.expected_splits:
            if target == reference:
                continue
            missing = sorted(
                label for label in reference_labels if counts[target].get(label, 0) == 0
            )
            if missing:
                findings.append(
                    Finding(
                        code=_LABEL_MISSING,
                        severity="warn",
                        message=(
                            f"切分 {target} 缺少参考切分 {reference} 中的 "
                            f"{len(missing)} 个标签"
                        ),
                        split=target,
                        record_ids=[],
                        evidence={
                            "reference_split": reference,
                            "target_split": target,
                            "missing_labels": missing,
                            "reference_counts": reference_counts,
                            "target_counts": counts[target],
                        },
                    )
                )

    metrics["counts"] = counts
    metrics["ratios"] = ratios
    return CheckResult(findings, metrics)
