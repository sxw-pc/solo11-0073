"""Cross-split exact duplicate detection."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from ..config import Config
from ..models import CheckResult, Dataset, Finding
from ..normalize import combine_text_fields, is_empty_content, redacted_summary


def run_exact_duplicate_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    metrics = {
        "enabled": bool(config.text_columns),
        "content_groups": 0,
        "cross_split_groups": 0,
        "within_split_groups": 0,
        "cross_split_records": 0,
        "within_split_records": 0,
    }
    if not config.text_columns:
        return CheckResult([], metrics)

    groups: dict[str, dict[str, object]] = defaultdict(
        lambda: {"normalized": "", "records": []}
    )
    for record_id in sorted(assignments):
        record = dataset.records[record_id]
        normalized = combine_text_fields(record.values, config.text_columns)
        if is_empty_content(normalized):
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        group = groups[digest]
        group["normalized"] = normalized
        records = group["records"]
        assert isinstance(records, list)
        records.append((record_id, assignments[record_id]))

    findings: list[Finding] = []
    for digest in sorted(groups):
        group = groups[digest]
        normalized = group["normalized"]
        assert isinstance(normalized, str)
        records = group["records"]
        assert isinstance(records, list)
        if len(records) < 2:
            continue
        record_ids = sorted(record_id for record_id, _ in records)
        splits = sorted({split for _, split in records})
        split_record_counts = {
            split: sum(1 for _, value in records if value == split)
            for split in splits
        }
        metrics["content_groups"] += 1
        evidence = {
            "content_sha256": digest,
            "content_fingerprint": digest[:16],
            "record_count": len(records),
            "record_ids": record_ids,
            "splits": splits,
            "split_record_counts": split_record_counts,
            "field_names": list(config.text_columns),
            "summary": redacted_summary(normalized),
            "records": [
                {"record_id": record_id, "split": split}
                for record_id, split in sorted(records)
            ],
        }
        if len(splits) > 1:
            metrics["cross_split_groups"] += 1
            metrics["cross_split_records"] += len(records)
            findings.append(
                Finding(
                    code="EXACT_DUPLICATE_CROSS_SPLIT",
                    severity="block",
                    message=(
                        f"内容哈希 {digest[:12]} 出现在 {len(splits)} 个切分中，"
                        f"涉及 {len(records)} 条记录"
                    ),
                    split=None,
                    record_ids=record_ids,
                    evidence=evidence,
                )
            )
        else:
            metrics["within_split_groups"] += 1
            metrics["within_split_records"] += len(records)
            split = splits[0]
            findings.append(
                Finding(
                    code="EXACT_DUPLICATE_WITHIN_SPLIT",
                    severity="warn",
                    message=(
                        f"内容哈希 {digest[:12]} 在切分 {split} 内重复 "
                        f"{len(records)} 次"
                    ),
                    split=split,
                    record_ids=record_ids,
                    evidence=evidence,
                )
            )
    return CheckResult(findings, metrics)
