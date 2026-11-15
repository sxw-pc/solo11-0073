"""Entity-group isolation and temporal leakage checks.

Two independent temporal checks live here:

* the original global split-order check compares the single ``time_column``
  across ``temporal.split_order``;
* the per-feature prediction-time check compares each configured feature's
  event/availability time against a per-row ``prediction_at`` cutoff, under
  either ``event_time`` or ``serving_time`` semantics.
"""

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
    metrics: dict[str, object] = {
        "enabled": options.enabled,
        "global_enabled": options.enabled and bool(config.time_column),
        "split_order": list(options.split_order),
        "min_gap_seconds": options.min_gap_seconds,
        "invalid_records": 0,
        "matches": 0,
        "splits": {},
        "feature_prediction": _feature_config_metrics(options),
    }
    findings: list[Finding] = []

    # The global split-order check remains independent of the per-feature
    # prediction-time semantics: it keeps comparing the single ``time_column``
    # across splits and is never replaced by feature-level checks.
    if options.enabled and config.time_column:
        global_findings, global_metrics = _run_split_order_checks(
            dataset, assignments, config
        )
        findings.extend(global_findings)
        metrics["invalid_records"] = global_metrics["invalid_records"]
        metrics["matches"] = global_metrics["matches"]
        metrics["splits"] = global_metrics["splits"]

    if options.enabled and options.feature_time_rules and options.prediction_at_column:
        feature_findings, feature_metrics = _run_feature_time_checks(
            dataset, assignments, config
        )
        findings.extend(feature_findings)
        metrics["feature_prediction"] = feature_metrics
    return CheckResult(findings, metrics)


def _feature_config_metrics(options: object) -> dict[str, object]:
    rules = getattr(options, "feature_time_rules", ())
    enabled = bool(
        getattr(options, "enabled", False)
        and rules
        and getattr(options, "prediction_at_column", None)
    )
    semantics = getattr(options, "semantics", "event_time")
    return {
        "enabled": enabled,
        "semantics": semantics,
        "semantics_explicit": bool(getattr(options, "semantics_explicit", False)),
        "prediction_at_column": getattr(options, "prediction_at_column", None),
        "entity_key_column": getattr(options, "entity_key_column", None),
        "rule_count": len(rules),
        "checked_records": 0,
        "unique_records_checked": 0,
        "violating_records": 0,
        "unknown_records": 0,
        "not_evaluated_records": 0,
        "unverified_columns": [],
        "data_bounds": {},
        "rules": [],
    }


def _run_split_order_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> tuple[list[Finding], dict[str, object]]:
    options = config.temporal
    metrics: dict[str, object] = {
        "invalid_records": 0,
        "matches": 0,
        "splits": {},
    }

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
    return findings, metrics


def _check_time_value(raw: str) -> tuple[datetime | None, str | None]:
    """Parse one timestamp; return ``(value, error_kind)`` without raising."""
    text = (raw or "").strip()
    if not text:
        return None, "missing"
    try:
        return parse_timestamp(text), None
    except (ValueError, TypeError, OverflowError):
        return None, "invalid"


def _bounds(
    minimum: datetime | None, maximum: datetime | None, value: datetime
) -> tuple[datetime, datetime]:
    return (
        value if minimum is None or value < minimum else minimum,
        value if maximum is None or value > maximum else maximum,
    )


def _rule_for(rules: list[object], feature: str) -> object:
    return next(rule for rule in rules if getattr(rule, "feature") == feature)


def _run_feature_time_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> tuple[list[Finding], dict[str, object]]:
    """Per-feature prediction-time leakage audit.

    ``event_time`` semantics only require that the event a feature describes
    happened no later than ``prediction_at``. ``serving_time`` semantics
    additionally require the feature to have already been available at
    ``prediction_at``; a late-arriving value is a leak even when its event time
    is earlier. Unparseable/missing values are reported under their own
    configurable severity and stay "unknown" rather than being treated as
    safely before the cutoff.
    """
    options = config.temporal
    semantics = options.semantics
    cutoff_column = options.prediction_at_column
    entity_column = options.entity_key_column
    severity = options.feature_time_invalid_severity
    rules = list(options.feature_time_rules)

    rule_metrics: list[dict[str, object]] = []
    event_bounds: dict[str, list[datetime | None]] = {}
    available_bounds: dict[str, list[datetime | None]] = {}
    for rule in rules:
        rule_metrics.append(
            {
                "feature": rule.feature,
                "event_time_column": rule.event_time_column,
                "available_at_column": rule.available_at_column,
                "semantics": semantics,
                "records_considered": 0,
                "checked_records": 0,
                "violating_records": 0,
                "invalid_records": 0,
                "missing_records": 0,
                "unverifiable_records": 0,
                "unverified_reason": None,
                "event_time_min": None,
                "event_time_max": None,
                "available_at_min": None,
                "available_at_max": None,
            }
        )
        event_bounds[rule.feature] = [None, None]
        available_bounds[rule.feature] = [None, None]
    rule_index = {rule.feature: index for index, rule in enumerate(rules)}

    invalid_records: list[dict[str, object]] = []
    missing_records: list[dict[str, object]] = []
    violations: list[dict[str, object]] = []
    evaluations: list[dict[str, object]] = []
    checked_record_ids: set[str] = set()
    considered_record_ids: set[str] = set()
    cutoff_bounds: list[datetime | None] = [None, None]
    # Records in the dataset that are not part of a unique, valid assignment
    # (unknown split, duplicate manifest row, missing manifest row) are never
    # fed into this check; they are surfaced here instead of being silently
    # mixed into the feature evidence.
    excluded_record_ids = sorted(set(dataset.records) - set(assignments))

    for record_id in sorted(assignments):
        record = dataset.records[record_id]
        split = assignments[record_id]
        cutoff_raw = record.values.get(cutoff_column, "")
        cutoff, cutoff_error = _check_time_value(cutoff_raw)
        if cutoff is not None:
            cutoff_bounds[0], cutoff_bounds[1] = _bounds(
                cutoff_bounds[0], cutoff_bounds[1], cutoff
            )

        for rule in rules:
            stats = rule_metrics[rule_index[rule.feature]]
            stats["records_considered"] = int(stats["records_considered"]) + 1
            considered_record_ids.add(record_id)

            if cutoff is None:
                # Without a cutoff neither semantics can be decided; the row
                # stays "unknown" rather than being counted as safe.
                cutoff_entry = {
                    "record_id": record_id,
                    "split": split,
                    "feature": rule.feature,
                    "field_name": cutoff_column,
                    "error": cutoff_error,
                    "value": cutoff_raw,
                    "line": record.line_no,
                }
                if cutoff_error == "missing":
                    missing_records.append(cutoff_entry)
                    stats["missing_records"] = int(stats["missing_records"]) + 1
                else:
                    invalid_records.append(cutoff_entry)
                    stats["invalid_records"] = int(stats["invalid_records"]) + 1
                stats["unverifiable_records"] = int(stats["unverifiable_records"]) + 1
                stats["unverified_reason"] = "missing_or_invalid_prediction_at"
                continue

            event_raw = record.values.get(rule.event_time_column, "")
            event_value, event_error = _check_time_value(event_raw)
            available_value: datetime | None = None
            available_raw = ""
            available_error: str | None = None
            if rule.available_at_column is not None:
                available_raw = record.values.get(rule.available_at_column, "")
                available_value, available_error = _check_time_value(available_raw)

            if event_value is not None:
                event_bounds[rule.feature][0], event_bounds[rule.feature][1] = _bounds(
                    event_bounds[rule.feature][0],
                    event_bounds[rule.feature][1],
                    event_value,
                )
            if available_value is not None:
                available_bounds[rule.feature][
                    0
                ], available_bounds[rule.feature][1] = _bounds(
                    available_bounds[rule.feature][0],
                    available_bounds[rule.feature][1],
                    available_value,
                )

            # Under event_time semantics only the event timestamp drives the
            # decision, so an unparseable availability value is reported as a
            # data-quality note but does not make the row unverifiable. Under
            # serving_time the availability value is the decision input and an
            # invalid/missing value must stay unknown.
            field_errors: list[tuple[str, str | None, str]] = []
            if event_value is None:
                field_errors.append((rule.event_time_column, event_error, event_raw))
            if semantics == "serving_time" and available_value is None:
                field_errors.append(
                    (rule.available_at_column, available_error, available_raw)
                )
            if field_errors:
                for field_name, error_kind, raw_value in field_errors:
                    entry = {
                        "record_id": record_id,
                        "split": split,
                        "feature": rule.feature,
                        "field_name": field_name,
                        "error": error_kind,
                        "value": raw_value,
                        "line": record.line_no,
                        "semantics": semantics,
                    }
                    if error_kind == "missing":
                        missing_records.append(entry)
                        stats["missing_records"] = int(stats["missing_records"]) + 1
                    else:
                        invalid_records.append(entry)
                        stats["invalid_records"] = int(stats["invalid_records"]) + 1
                stats["unverifiable_records"] = int(stats["unverifiable_records"]) + 1
                stats["unverified_reason"] = "missing_or_invalid_feature_time"
                continue

            stats["checked_records"] = int(stats["checked_records"]) + 1
            checked_record_ids.add(record_id)
            # ``serving_time`` compares against availability; ``event_time``
            # deliberately ignores availability even when the column exists.
            served_at = (
                available_value
                if semantics == "serving_time" and available_value is not None
                else event_value
            )
            evaluation: dict[str, object] = {
                "record_id": record_id,
                "split": split,
                "feature": rule.feature,
                "semantics": semantics,
                "event_time_column": rule.event_time_column,
                "event_time_raw": event_raw,
                "event_time_utc": iso_utc(event_value),
                "available_at_column": rule.available_at_column,
                "available_at_raw": (
                    available_raw if rule.available_at_column is not None else None
                ),
                "available_at_utc": (
                    iso_utc(available_value) if available_value is not None else None
                ),
                "compared_time_utc": iso_utc(served_at),
                "prediction_at_column": cutoff_column,
                "prediction_at_raw": cutoff_raw,
                "prediction_at_utc": iso_utc(cutoff),
                "compliant": served_at <= cutoff,
                "violated_semantics": None if served_at <= cutoff else semantics,
            }
            if entity_column:
                evaluation["entity_key"] = record.values.get(entity_column, "")
            evaluations.append(evaluation)
            if served_at <= cutoff:
                continue

            late_arrival = bool(
                semantics == "serving_time"
                and available_value is not None
                and event_value <= cutoff < available_value
            )
            violation: dict[str, object] = {
                "record_id": record_id,
                "split": split,
                "feature": rule.feature,
                "semantics": semantics,
                "violated_semantics": semantics,
                "event_time_column": rule.event_time_column,
                "event_time_raw": event_raw,
                "event_time_utc": iso_utc(event_value),
                "available_at_column": rule.available_at_column,
                "available_at_raw": (
                    available_raw if rule.available_at_column is not None else None
                ),
                "available_at_utc": (
                    iso_utc(available_value) if available_value is not None else None
                ),
                "compared_time_utc": iso_utc(served_at),
                "prediction_at_column": cutoff_column,
                "prediction_at_raw": cutoff_raw,
                "prediction_at_utc": iso_utc(cutoff),
                "late_arrival": late_arrival,
                "late_arrival_explanation": (
                    "事件时间早于截止，但线上预测时该特征尚未到达"
                    if late_arrival
                    else None
                ),
                "line": record.line_no,
            }
            if entity_column:
                violation["entity_key"] = record.values.get(entity_column, "")
            violations.append(violation)
            stats["violating_records"] = int(stats["violating_records"]) + 1

    for index, rule in enumerate(rules):
        metrics_entry = rule_metrics[index]
        event_min, event_max = event_bounds[rule.feature]
        available_min, available_max = available_bounds[rule.feature]
        metrics_entry["event_time_min"] = iso_utc(event_min) if event_min else None
        metrics_entry["event_time_max"] = iso_utc(event_max) if event_max else None
        metrics_entry["available_at_min"] = (
            iso_utc(available_min) if available_min else None
        )
        metrics_entry["available_at_max"] = (
            iso_utc(available_max) if available_max else None
        )

    findings: list[Finding] = []
    # One finding per (feature, record) keeps the evidence of every
    # unique/valid record intact: multiple feature columns on the same record
    # and multiple rows of the same entity never overwrite each other.
    for violation in sorted(
        violations,
        key=lambda item: (str(item["feature"]), str(item["record_id"])),
    ):
        feature = str(violation["feature"])
        rule = _rule_for(rules, feature)
        record_id = str(violation["record_id"])
        late_arrival = bool(violation.get("late_arrival"))
        message = (
            f"记录 {record_id} 的特征 {feature} 在预测截止 "
            f"{violation['prediction_at_utc']} 时不可用（{semantics} 语义）"
        )
        if late_arrival:
            message += "：事件时间早于截止，但线上预测时该特征尚未到达"
        findings.append(
            Finding(
                code="TEMPORAL_FEATURE_LEAKAGE",
                severity="block",
                message=message,
                split=None,
                record_ids=[record_id],
                evidence={
                    "feature": feature,
                    "record_id": record_id,
                    "split": violation["split"],
                    "entity_key": violation.get("entity_key"),
                    "entity_key_column": entity_column,
                    "semantics": semantics,
                    "violated_semantics": semantics,
                    "prediction_at_column": cutoff_column,
                    "event_time_column": rule.event_time_column,
                    "available_at_column": rule.available_at_column,
                    "late_arrival": late_arrival,
                    "record": violation,
                },
            )
        )

    for code, collection, field_label in (
        ("TEMPORAL_FEATURE_INVALID_TIMESTAMP", invalid_records, "非法"),
        ("TEMPORAL_FEATURE_MISSING_TIMESTAMP", missing_records, "缺失"),
    ):
        if not collection:
            continue
        findings.append(
            Finding(
                code=code,
                severity=severity,
                message=(
                    f"{len(collection)} 条特征的{field_label}时间值无法核验，"
                    "已按未知处理而不是视为早于截止"
                ),
                split=None,
                record_ids=sorted({str(item["record_id"]) for item in collection}),
                evidence={
                    "count": len(collection),
                    "semantics": semantics,
                    "severity_policy": severity,
                    "prediction_at_column": cutoff_column,
                    "unknown_verification": True,
                    "records": sorted(
                        collection,
                        key=lambda item: (
                            str(item["record_id"]),
                            str(item["feature"]),
                            str(item["field_name"]),
                        ),
                    ),
                },
            )
        )

    unverified_columns = sorted(
        {str(item["field_name"]) for item in invalid_records}
        | {str(item["field_name"]) for item in missing_records}
    )
    metrics: dict[str, object] = {
        "enabled": True,
        "semantics": semantics,
        "semantics_explicit": options.semantics_explicit,
        "prediction_at_column": cutoff_column,
        "entity_key_column": entity_column,
        "rule_count": len(rules),
        "checked_records": len(checked_record_ids),
        "unique_records_checked": len(checked_record_ids),
        "considered_records": len(considered_record_ids),
        "violating_records": len({str(item["record_id"]) for item in violations}),
        "unknown_records": len(
            {str(item["record_id"]) for item in invalid_records}
            | {str(item["record_id"]) for item in missing_records}
        ),
        "not_evaluated_records": max(0, len(assignments) - len(considered_record_ids)),
        "unverified_columns": unverified_columns,
        "invalid_value_severity": severity,
        "rules": rule_metrics,
        "failing_features": sorted({str(item["feature"]) for item in violations}),
        "per_record_basis": True,
        "evaluations": sorted(
            evaluations,
            key=lambda item: (str(item["feature"]), str(item["record_id"])),
        ),
        "data_bounds": {
            "prediction_at_column": cutoff_column,
            "prediction_at_min": (
                iso_utc(cutoff_bounds[0]) if cutoff_bounds[0] else None
            ),
            "prediction_at_max": (
                iso_utc(cutoff_bounds[1]) if cutoff_bounds[1] else None
            ),
        },
        "excluded_records": {
            "count": len(excluded_record_ids),
            "record_ids": excluded_record_ids,
            "reason": "not_a_unique_valid_assignment",
        },
    }
    return findings, metrics
