from splitguard.checks.manifest import run_manifest_checks
from tests.helpers import make_config, make_dataset, make_manifest, row


def test_manifest_reports_all_structural_problems() -> None:
    dataset = make_dataset([row(f"record-000{i}", text=f"text {i}") for i in range(1, 6)])
    manifest = make_manifest(
        [
            ("record-0001", "train"),
            ("record-0002", "valid"),
            ("record-0002", "test"),
            ("record-0003", "holdout"),
            ("record-0004", ""),
            ("record-9999", "valid"),
        ]
    )
    result = run_manifest_checks(dataset, manifest, make_config())
    codes = {finding.code for finding in result.findings}
    assert {
        "MANIFEST_DUPLICATE_ASSIGNMENT",
        "MANIFEST_UNKNOWN_SPLIT",
        "MANIFEST_EMPTY_SPLIT_VALUE",
        "MANIFEST_UNKNOWN_RECORD",
        "MANIFEST_MISSING_ASSIGNMENT",
        "SPLIT_EMPTY",
    } <= codes
    assert result.metrics["missing_ids_count"] == 1
    assert result.metrics["unknown_ids_count"] == 1
    assert result.metrics["duplicate_assignment_count"] == 1
    assert result.metrics["assigned_records"] == 1
    assert result.assignments == {"record-0001": "train"}
    empty = next(item for item in result.findings if item.code == "SPLIT_EMPTY")
    assert empty.severity == "block"


def test_empty_split_severity_is_configurable() -> None:
    dataset = make_dataset([row("record-0001")])
    manifest = make_manifest({"record-0001": "train"})
    config = make_config(empty_split_severity="warn", expected_ratios={})
    result = run_manifest_checks(dataset, manifest, config)
    empty = [item for item in result.findings if item.code == "SPLIT_EMPTY"]
    assert {item.split for item in empty} == {"valid", "test"}
    assert all(item.severity == "warn" for item in empty)


def test_ratio_drift_threshold_and_metrics() -> None:
    dataset = make_dataset([row(f"record-{index:04d}", text=f"text {index}") for index in range(10)])
    manifest = make_manifest(
        [(f"record-{index:04d}", "train") for index in range(8)]
        + [
            ("record-0008", "valid"),
            ("record-0009", "test"),
        ]
    )
    strict = run_manifest_checks(dataset, manifest, make_config())
    drift = [item for item in strict.findings if item.code == "SPLIT_RATIO_DRIFT"]
    assert [item.split for item in drift] == ["train"]
    assert strict.metrics["split_counts"] == {"train": 8, "valid": 1, "test": 1}
    assert strict.metrics["split_ratios"]["train"] == 0.8
    assert strict.metrics["ratio_deltas"]["train"] == 0.1

    tolerant = run_manifest_checks(
        dataset, manifest, make_config(ratio_tolerance=0.2)
    )
    assert not [item for item in tolerant.findings if item.code == "SPLIT_RATIO_DRIFT"]


def test_missing_and_unknown_ids_are_lists() -> None:
    dataset = make_dataset([row("record-0001"), row("record-0002")])
    manifest = make_manifest([("record-0001", "train"), ("record-9999", "train")])
    result = run_manifest_checks(dataset, manifest, make_config(expected_ratios={}))
    missing = next(item for item in result.findings if item.code == "MANIFEST_MISSING_ASSIGNMENT")
    unknown = next(item for item in result.findings if item.code == "MANIFEST_UNKNOWN_RECORD")
    assert missing.record_ids == ["record-0002"]
    assert unknown.record_ids == ["record-9999"]
    assert missing.severity == "block" and unknown.severity == "block"


def test_duplicate_assignment_excludes_record_from_downstream_assignment() -> None:
    dataset = make_dataset([row("record-0001")])
    manifest = make_manifest([("record-0001", "train"), ("record-0001", "train")])
    result = run_manifest_checks(dataset, manifest, make_config(expected_ratios={}))
    assert result.assignments == {}
    duplicate = next(
        item for item in result.findings if item.code == "MANIFEST_DUPLICATE_ASSIGNMENT"
    )
    assert duplicate.record_ids == ["record-0001"]
    assert duplicate.evidence["assignments"][0]["splits"] == ["train", "train"]
