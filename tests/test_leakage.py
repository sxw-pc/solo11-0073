from splitguard.checks.leakage import (
    parse_timestamp,
    run_group_checks,
    run_temporal_checks,
)
from tests.helpers import make_config, make_dataset, row


def _base_rows() -> list[dict[str, str]]:
    return [
        row("record-0001", text="a", group="entity-0001", time="2025-01-01T10:00:00Z"),
        row("record-0002", text="b", group="entity-0002", time="2025-01-01T09:00:00Z"),
    ]


def test_group_leak_across_splits_is_one_aggregated_block() -> None:
    rows = [
        row("record-0001", text="a", group="entity-0001"),
        row("record-0002", text="b", group="entity-0001"),
        row("record-0003", text="c", group="entity-0002"),
    ]
    result = run_group_checks(
        make_dataset(rows),
        {"record-0001": "train", "record-0002": "test", "record-0003": "train"},
        make_config(),
    )
    leaked = [item for item in result.findings if item.code == "GROUP_LEAKAGE"]
    assert len(leaked) == 1
    assert leaked[0].severity == "block"
    assert leaked[0].evidence["group_id"] == "entity-0001"
    assert leaked[0].evidence["record_count"] == 2
    assert leaked[0].evidence["splits"] == ["test", "train"]
    assert result.metrics["leaked_groups"] == 1


def test_group_in_single_split_and_disabled_check_do_not_leak() -> None:
    rows = [
        row("record-0001", group="entity-0001"),
        row("record-0002", group="entity-0001"),
    ]
    dataset = make_dataset(rows)
    same = run_group_checks(
        dataset,
        {"record-0001": "train", "record-0002": "train"},
        make_config(),
    )
    assert same.findings == []
    disabled = run_group_checks(
        dataset,
        {"record-0001": "train", "record-0002": "test"},
        make_config(group_isolation=False),
    )
    assert disabled.findings == []
    assert disabled.metrics["enabled"] is False


def test_empty_group_is_warning() -> None:
    result = run_group_checks(
        make_dataset([row("record-0001", group="")]),
        {"record-0001": "train"},
        make_config(),
    )
    assert result.findings[0].code == "GROUP_EMPTY_VALUE"
    assert result.findings[0].severity == "warn"
    assert result.metrics["empty_group_records"] == 1


def test_temporal_leak_detects_earlier_record() -> None:
    dataset = make_dataset(_base_rows())
    config = make_config(temporal={"enabled": True})
    result = run_temporal_checks(
        dataset,
        {"record-0001": "train", "record-0002": "valid"},
        config,
    )
    finding = next(item for item in result.findings if item.code == "TEMPORAL_LEAKAGE")
    assert finding.severity == "block"
    assert finding.split == "valid"
    assert finding.record_ids == ["record-0002"]
    assert finding.evidence["prior_max_time"] == "2025-01-01T10:00:00.000000Z"
    metrics = result.metrics["splits"]
    assert metrics["train"]["max_time"] == "2025-01-01T10:00:00.000000Z"
    assert metrics["valid"]["violating_records"] == 1


def test_temporal_correct_order_and_gap_are_clean() -> None:
    rows = [
        row("record-0001", time="2025-01-01T10:00:00Z"),
        row("record-0002", time="2025-01-01T11:00:00Z"),
    ]
    dataset = make_dataset(rows)
    config = make_config(temporal={"enabled": True})
    result = run_temporal_checks(
        dataset,
        {"record-0001": "train", "record-0002": "valid"},
        config,
    )
    assert not [item for item in result.findings if item.code == "TEMPORAL_LEAKAGE"]

    gapped = make_config(
        temporal={"enabled": True, "min_gap_seconds": 3600}
    )
    result = run_temporal_checks(
        make_dataset(
            [
                row("record-0001", time="2025-01-01T10:00:00Z"),
                row("record-0002", time="2025-01-01T09:30:00Z"),
            ]
        ),
        {"record-0001": "train", "record-0002": "valid"},
        gapped,
    )
    assert not [item for item in result.findings if item.code == "TEMPORAL_LEAKAGE"]


def test_invalid_timestamp_is_structured_and_configurable() -> None:
    dataset = make_dataset(
        [row("record-0001", time="not-a-time"), row("record-0002", time="2025-01-01T10:00:00Z")]
    )
    result = run_temporal_checks(
        dataset,
        {"record-0001": "train", "record-0002": "valid"},
        make_config(temporal={"enabled": True}, invalid_time_severity="warn"),
    )
    finding = next(item for item in result.findings if item.code == "TEMPORAL_INVALID_TIMESTAMP")
    assert finding.severity == "warn"
    assert finding.evidence["records"][0]["record_id"] == "record-0001"
    assert finding.evidence["field_name"] == "event_time"
    assert result.metrics["invalid_records"] == 1


def test_parse_timestamp_handles_z_and_offsets() -> None:
    assert parse_timestamp("2025-01-01T08:00:00+08:00").isoformat() == "2025-01-01T00:00:00+00:00"
    assert parse_timestamp("2025-01-01T00:00:00Z").isoformat() == "2025-01-01T00:00:00+00:00"
