"""Entity-group isolation and temporal ordering leakage checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..fingerprint import iso_utc
from ..models import CheckResult, Dataset, Finding


def run_group_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    metrics = {
        "enabled": bool(config.group_isolation and config.group_column),
        "groups_checked": 0,
        "leaked_groups": 0,
        "empty_group_records": 0,
    }
    if not config.group_isolation or not config.group_column:
        return CheckResult([], metrics)

    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    empty_group_ids: list[str] = []
    for record_id in sorted(assignments):
        raw_group = dataset.records[record_id].values.get(config.group_column, "")
        group_id = raw_group.strip()
        if not group_id:
            empty_group_ids.append(record_id)
            continue
        groups[group_id].append((record_id, assignments[record_id]))

    findings: list[Finding] = []
    for group_id in sorted(groups):
        members = sorted(groups[group_id])
        splits = sorted({split for _, split in members})
        if len(splits) < 2:
            continue
        split_counts: dict[str, int] = {}
        for split in splits:
            split_counts[split] = sum(1 for _, value in members if value == split)
        representative = sorted(record_id for record_id, _ in members)[:10]
        metrics["leaked_groups"] += 1
        findings.append(
            Finding(
                code="GROUP_LEAKAGE",
                severity="block",
                message=(
                    f"实体组 {group_id} 出现在 {len(splits)} 个切分中，"
                    f"涉及 {len(members)} 条记录"
                ),
                split=None,
                record_ids=representative,
                evidence={
                    "group_id": group_id,
                    "group_column": config.group_column,
                    "splits": splits,
                    "record_count": len(members),
                    "record_counts_by_split": split_counts,
                    "representative_record_ids": representative,
                },
            )
        )
    metrics["groups_checked"] = len(groups)
    if empty_group_ids:
        metrics["empty_group_records"] = len(empty_group_ids)
        findings.append(
            Finding(
                code="GROUP_EMPTY_VALUE",
                severity="warn",
                message=f"{len(empty_group_ids)} 条记录的实体组为空",
                split=None,
                record_ids=empty_group_ids,
                evidence={
                    "group_column": config.group_column,
                    "count": len(empty_group_ids),
                    "record_ids": empty_group_ids,
                },
            )
        )
    return CheckResult(findings, metrics)


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_temporal_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    options = config.temporal
    metrics = {
        "enabled": options.enabled,
        "split_order": list(options.split_order),
        "min_gap_seconds": options.min_gap_seconds,
        "invalid_records": 0,
        "matches": 0,
        "splits": {},
    }
    if not options.enabled or not config.time_column:
        return CheckResult([], metrics)

    records_by_split: dict[str, list[str]] = {
        split: [] for split in options.split_order
    }
    for record_id in sorted(assignments):
        split = assignments[record_id]
        if split in records_by_split:
            records_by_split[split].append(record_id)

    invalid: list[dict[str, object]] = []
    times_by_split: dict[str, list[tuple[str, datetime]]] = {}
    for split in options.split_order:
        parsed: list[tuple[str, datetime]] = []
        for record_id in records_by_split[split]:
            raw = dataset.records[record_id].values.get(config.time_column, "")
            try:
                parsed.append((record_id, parse_timestamp(raw)))
            except (ValueError, TypeError):
                invalid.append(
                    {
                        "record_id": record_id,
                        "split": split,
                        "value": raw,
                        "line": dataset.records[record_id].line_no,
                    }
                )
        times_by_split[split] = sorted(parsed, key=lambda item: (item[1], item[0]))

    findings: list[Finding] = []
    metrics["invalid_records"] = len(invalid)
    if invalid:
        findings.append(
            Finding(
                code="TEMPORAL_INVALID_TIMESTAMP",
                severity=config.invalid_time_severity,
                message=f"{len(invalid)} 条记录的时间字段无法解析为 ISO 8601",
                split=None,
                record_ids=sorted(item["record_id"] for item in invalid),
                evidence={
                    "field_name": config.time_column,
                    "count": len(invalid),
                    "severity_policy": config.invalid_time_severity,
                    "records": invalid,
                },
            )
        )

    summaries: dict[str, dict[str, object]] = {}
    prior_max: datetime | None = None
    prior_splits: list[str] = []
    total_matches = 0
    for split in options.split_order:
        parsed = times_by_split[split]
        record_count = len(records_by_split[split])
        invalid_count = sum(1 for item in invalid if item["split"] == split)
        summary: dict[str, object] = {
            "record_count": record_count,
            "parsed_records": len(parsed),
            "invalid_records": invalid_count,
            "min_time": iso_utc(parsed[0][1]) if parsed else None,
            "max_time": iso_utc(parsed[-1][1]) if parsed else None,
            "violating_records": 0,
        }
        if parsed and prior_max is not None:
            threshold = prior_max - timedelta(seconds=options.min_gap_seconds)
            violations = [(record_id, value) for record_id, value in parsed if value < threshold]
            if violations:
                total_matches += len(violations)
                summary["violating_records"] = len(violations)
                representative = [
                    {"record_id": record_id, "timestamp": iso_utc(value)}
                    for record_id, value in violations[:20]
                ]
                finding_ids = sorted(record_id for record_id, _ in violations)
                findings.append(
                    Finding(
                        code="TEMPORAL_LEAKAGE",
                        severity="block",
                        message=(
                            f"切分 {split} 有 {len(violations)} 条记录早于前序切分最大时间 "
                            f"{iso_utc(prior_max)}"
                        ),
                        split=split,
                        record_ids=finding_ids,
                        evidence={
                            "split": split,
                            "prior_splits": list(prior_splits),
                            "prior_max_time": iso_utc(prior_max),
                            "min_gap_seconds": options.min_gap_seconds,
                            "violation_threshold": iso_utc(threshold),
                            "violating_count": len(violations),
                            "representative_records": representative,
                            "split_min_time": iso_utc(parsed[0][1]),
                            "split_max_time": iso_utc(parsed[-1][1]),
                        },
                    )
                )
        summaries[split] = summary
        if parsed:
            candidate_max = parsed[-1][1]
            if prior_max is None or candidate_max > prior_max:
                prior_max = candidate_max
        prior_splits.append(split)

    metrics["matches"] = total_matches
    metrics["splits"] = summaries
    return CheckResult(findings, metrics)
