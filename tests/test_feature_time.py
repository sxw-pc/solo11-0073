"""Targeted tests for the per-feature prediction-time leakage audit."""

from __future__ import annotations

import pytest

from splitguard.checks.leakage import parse_timestamp, run_temporal_checks
from splitguard.config import config_from_dict
from tests.helpers import config_data, make_config, make_dataset, row


def _config(**temporal: object):
    temporal_data = {
        "enabled": True,
        "feature_time_rules": [
            {
                "feature": "click_count",
                "event_time_column": "click_event_time",
                "available_at_column": "click_available_at",
            }
        ],
        "prediction_at_column": "prediction_at",
        "entity_key_column": "entity_id",
    }
    temporal_data.update(temporal)
    return make_config(temporal=temporal_data)


def _rows() -> list[dict[str, str]]:
    return [
        row(
            "record-17",
            group="entity-a",
            click_event_time="2025-01-01T10:00:00Z",
            click_available_at="2025-01-01T18:00:00Z",
            prediction_at="2025-01-01T12:00:00Z",
        )
    ]


def _feature_findings(result):
    return [
        item for item in result.findings if item.code == "TEMPORAL_FEATURE_LEAKAGE"
    ]


def test_acceptance_record17_event_time_allows_but_serving_time_blocks() -> None:
    dataset = make_dataset(_rows())
    assignments = {"record-17": "train"}

    event_result = run_temporal_checks(
        dataset, assignments, _config(semantics="event_time")
    )
    assert _feature_findings(event_result) == []
    event_metrics = event_result.metrics["feature_prediction"]
    assert event_metrics["semantics"] == "event_time"
    assert event_metrics["checked_records"] == 1
    assert event_metrics["violating_records"] == 0

    serving_result = run_temporal_checks(
        dataset, assignments, _config(semantics="serving_time")
    )
    findings = _feature_findings(serving_result)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "block"
    assert finding.record_ids == ["record-17"]
    record = finding.evidence["record"]
    assert record["event_time_utc"] == "2025-01-01T10:00:00.000000Z"
    assert record["available_at_utc"] == "2025-01-01T18:00:00.000000Z"
    assert record["prediction_at_utc"] == "2025-01-01T12:00:00.000000Z"
    assert record["compared_time_utc"] == "2025-01-01T18:00:00.000000Z"
    assert record["late_arrival"] is True
    assert record["violated_semantics"] == "serving_time"
    assert "尚未到达" in finding.message
    assert serving_result.metrics["feature_prediction"]["violating_records"] == 1


def test_semantics_is_reported_and_cannot_be_silently_defaulted() -> None:
    data = config_data(
        temporal={
            "enabled": True,
            "feature_time_rules": [
                {
                    "feature": "click_count",
                    "event_time_column": "click_event_time",
                    "available_at_column": "click_available_at",
                }
            ],
            "prediction_at_column": "prediction_at",
        }
    )
    with pytest.raises(Exception) as error:
        config_from_dict(data)
    assert "semantics" in str(error.value)

    explicit = run_temporal_checks(
        make_dataset(_rows()), {"record-17": "train"}, _config(semantics="event_time")
    )
    summary = explicit.metrics["feature_prediction"]
    assert summary["semantics"] == "event_time"
    assert summary["semantics_explicit"] is True


def test_global_split_order_check_still_runs_independently() -> None:
    rows = [
        row(
            "record-0001",
            time="2025-01-01T10:00:00Z",
            click_event_time="2025-01-01T10:00:00Z",
            click_available_at="2025-01-01T10:00:00Z",
            prediction_at="2025-01-01T12:00:00Z",
        ),
        row(
            "record-0002",
            time="2025-01-01T09:00:00Z",
            click_event_time="2025-01-01T09:00:00Z",
            click_available_at="2025-01-01T09:00:00Z",
            prediction_at="2025-01-01T12:00:00Z",
        ),
    ]
    result = run_temporal_checks(
        make_dataset(rows),
        {"record-0001": "train", "record-0002": "valid"},
        _config(semantics="serving_time"),
    )
    codes = {item.code for item in result.findings}
    # Serving-time features are clean, but the classic split-order leak stands.
    assert "TEMPORAL_LEAKAGE" in codes
    assert "TEMPORAL_FEATURE_LEAKAGE" not in codes
    assert result.metrics["matches"] == 1


def test_event_and_serving_semantics_diverge_for_the_same_late_sample() -> None:
    dataset = make_dataset(_rows())
    assignments = {"record-17": "train"}
    event = run_temporal_checks(dataset, assignments, _config(semantics="event_time"))
    serving = run_temporal_checks(
        dataset, assignments, _config(semantics="serving_time")
    )
    assert not _feature_findings(event)
    assert len(_feature_findings(serving)) == 1


def test_equal_cutoff_is_safe_and_one_second_later_is_stable_violation() -> None:
    dataset = make_dataset(
        [
            row(
                "record-equal",
                click_event_time="2025-01-01T12:00:00Z",
                click_available_at="2025-01-01T12:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-late",
                click_event_time="2025-01-01T12:00:01Z",
                click_available_at="2025-01-01T12:00:01Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    assignments = {"record-equal": "train", "record-late": "valid"}
    result = run_temporal_checks(dataset, assignments, _config(semantics="event_time"))
    leaked = _feature_findings(result)
    assert [item.record_ids for item in leaked] == [["record-late"]]
    # Deterministic across repeated runs with identical inputs.
    again = run_temporal_checks(dataset, assignments, _config(semantics="event_time"))
    assert [item.to_dict() for item in leaked] == [
        item.to_dict() for item in _feature_findings(again)
    ]


def test_utc_offsets_representing_same_instant_agree() -> None:
    dataset = make_dataset(
        [
            row(
                "record-utc",
                click_event_time="2025-01-01T12:00:00Z",
                click_available_at="2025-01-01T12:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-offset",
                click_event_time="2025-01-01T20:00:00+08:00",
                click_available_at="2025-01-01T20:00:00+08:00",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    result = run_temporal_checks(
        dataset,
        {"record-utc": "train", "record-offset": "valid"},
        _config(semantics="serving_time"),
    )
    assert _feature_findings(result) == []
    metrics = result.metrics["feature_prediction"]
    assert metrics["checked_records"] == 2


def test_invalid_and_missing_values_stay_unknown_with_configurable_severity() -> None:
    dataset = make_dataset(
        [
            row(
                "record-bad",
                click_event_time="not-a-time",
                click_available_at="2025-01-01T10:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-missing",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-ok",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T10:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    assignments = {
        "record-bad": "train",
        "record-missing": "train",
        "record-ok": "train",
    }
    result = run_temporal_checks(
        dataset,
        assignments,
        _config(semantics="serving_time", feature_time_invalid_severity="warn"),
    )
    invalid = next(
        item
        for item in result.findings
        if item.code == "TEMPORAL_FEATURE_INVALID_TIMESTAMP"
    )
    missing = next(
        item
        for item in result.findings
        if item.code == "TEMPORAL_FEATURE_MISSING_TIMESTAMP"
    )
    assert invalid.severity == "warn"
    assert missing.severity == "warn"
    assert invalid.record_ids == ["record-bad"]
    assert missing.record_ids == ["record-missing"]
    assert invalid.evidence["unknown_verification"] is True
    metrics = result.metrics["feature_prediction"]
    assert metrics["unknown_records"] == 2
    assert metrics["checked_records"] == 1
    assert "click_event_time" in metrics["unverified_columns"]
    assert "click_available_at" in metrics["unverified_columns"]
    # Unknown rows must not be laundered into safe or violating records.
    assert _feature_findings(result) == []


def test_invalid_prediction_at_is_unknown_for_both_semantics() -> None:
    dataset = make_dataset(
        [
            row(
                "record-1",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T10:00:00Z",
                prediction_at="",
            )
        ]
    )
    for semantics in ("event_time", "serving_time"):
        result = run_temporal_checks(
            dataset, {"record-1": "train"}, _config(semantics=semantics)
        )
        assert _feature_findings(result) == []
        assert result.metrics["feature_prediction"]["unknown_records"] == 1
        assert result.metrics["feature_prediction"]["checked_records"] == 0


def test_multiple_feature_columns_and_entities_keep_separate_evidence() -> None:
    dataset = make_dataset(
        [
            row(
                "record-1",
                group="entity-a",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T18:00:00Z",
                views_event_time="2025-01-01T11:00:00Z",
                views_available_at="2025-01-01T19:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-2",
                group="entity-a",
                click_event_time="2025-01-01T09:00:00Z",
                click_available_at="2025-01-01T20:00:00Z",
                views_event_time="2025-01-01T09:30:00Z",
                views_available_at="2025-01-01T09:30:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    config = make_config(
        temporal={
            "enabled": True,
            "semantics": "serving_time",
            "feature_time_rules": [
                {
                    "feature": "click_count",
                    "event_time_column": "click_event_time",
                    "available_at_column": "click_available_at",
                },
                {
                    "feature": "page_views",
                    "event_time_column": "views_event_time",
                    "available_at_column": "views_available_at",
                },
            ],
            "prediction_at_column": "prediction_at",
            "entity_key_column": "entity_id",
        }
    )
    result = run_temporal_checks(
        dataset,
        {"record-1": "train", "record-2": "valid"},
        config,
    )
    leaked = _feature_findings(result)
    pairs = {
        (item.evidence["feature"], item.evidence["record_id"]) for item in leaked
    }
    assert pairs == {
        ("click_count", "record-1"),
        ("page_views", "record-1"),
        ("click_count", "record-2"),
    }
    # Same entity, different rows: evidence is per record, not collapsed.
    record_one = [
        item
        for item in leaked
        if item.evidence["record_id"] == "record-1"
    ]
    assert len(record_one) == 2
    assert {item.evidence["entity_key"] for item in leaked} == {"entity-a"}


def test_unknown_split_and_missing_manifest_rows_are_excluded_not_silently_safe() -> (
    None
):
    dataset = make_dataset(
        [
            row(
                "record-train",
                group="entity-a",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T18:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-holdout",
                group="entity-b",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T18:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-missing",
                group="entity-c",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T18:00:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    # Assignments only contain the unique/valid record; the other two lack a
    # valid assignment (unknown split / absent manifest row) upstream.
    result = run_temporal_checks(
        dataset, {"record-train": "train"}, _config(semantics="serving_time")
    )
    leaked = _feature_findings(result)
    assert [item.record_ids for item in leaked] == [["record-train"]]
    excluded = result.metrics["feature_prediction"]["excluded_records"]
    assert excluded["count"] == 2
    assert excluded["record_ids"] == ["record-holdout", "record-missing"]
    assert excluded["reason"] == "not_a_unique_valid_assignment"


def test_event_time_semantics_ignores_unparseable_availability() -> None:
    dataset = make_dataset(
        [
            row(
                "record-1",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="garbage",
                prediction_at="2025-01-01T12:00:00Z",
            )
        ]
    )
    result = run_temporal_checks(
        dataset, {"record-1": "train"}, _config(semantics="event_time")
    )
    assert _feature_findings(result) == []
    metrics = result.metrics["feature_prediction"]
    assert metrics["checked_records"] == 1
    assert metrics["unknown_records"] == 0


def test_config_requires_available_column_for_serving_time_and_valid_columns() -> (
    None
):
    with pytest.raises(Exception) as error:
        config_from_dict(
            config_data(
                temporal={
                    "enabled": True,
                    "semantics": "serving_time",
                    "feature_time_rules": [
                        {"feature": "click_count", "event_time_column": "e"}
                    ],
                    "prediction_at_column": "prediction_at",
                }
            )
        )
    assert "available_at_column" in str(error.value)

    with pytest.raises(Exception) as error:
        config_from_dict(
            config_data(
                temporal={
                    "enabled": True,
                    "semantics": "bogus",
                    "feature_time_rules": [
                        {
                            "feature": "click_count",
                            "event_time_column": "e",
                            "available_at_column": "a",
                        }
                    ],
                    "prediction_at_column": "prediction_at",
                }
            )
        )
    assert "semantics" in str(error.value)

    with pytest.raises(Exception) as error:
        config_from_dict(
            config_data(
                temporal={
                    "enabled": True,
                    "semantics": "event_time",
                    "feature_time_rules": [
                        {
                            "feature": "click_count",
                            "event_time_column": "same",
                            "available_at_column": "same",
                        }
                    ],
                    "prediction_at_column": "prediction_at",
                }
            )
        )
    assert "available_at_column" in str(error.value)


def test_config_serializes_temporal_semantics_and_rules() -> None:
    config = _config(semantics="serving_time")
    data = config.to_dict()["temporal"]
    assert data["semantics"] == "serving_time"
    assert data["prediction_at_column"] == "prediction_at"
    assert data["feature_time_rules"][0]["feature"] == "click_count"
    # Default follows invalid_time_severity unless explicitly overridden.
    assert config.temporal.feature_time_invalid_severity == "block"
    assert data["feature_time_invalid_severity"] == "block"


def test_late_feature_fixture_diverges_by_semantics_end_to_end() -> None:
    from pathlib import Path

    from splitguard.engine import build_audit

    root = Path(__file__).resolve().parents[1] / "fixtures" / "late_feature"
    serving = build_audit(
        root / "dataset.csv", root / "splits.csv", root / "config.json"
    ).report
    event = build_audit(
        root / "dataset.csv", root / "splits.csv", root / "config_event_time.json"
    ).report

    serving_feature = serving["metrics"]["temporal"]["feature_prediction"]
    event_feature = event["metrics"]["temporal"]["feature_prediction"]
    assert serving_feature["semantics"] == "serving_time"
    assert event_feature["semantics"] == "event_time"
    serving_leaks = [
        item for item in serving["findings"] if item["code"] == "TEMPORAL_FEATURE_LEAKAGE"
    ]
    event_leaks = [
        item for item in event["findings"] if item["code"] == "TEMPORAL_FEATURE_LEAKAGE"
    ]
    assert {item["record_ids"][0] for item in serving_leaks} == {
        "record-0001",
        "record-0004",
        "record-0006",
    }
    assert event_leaks == []
    assert serving_feature["violating_records"] == 3
    assert event_feature["violating_records"] == 0
    assert serving_feature["unknown_records"] == 2
    # Both runs remain blocking/review due to the unknown values, never pass.
    assert serving["conclusion"] == "block"
    assert event["conclusion"] == "review"
    for report in (serving, event):
        feature = report["metrics"]["temporal"]["feature_prediction"]
        assert "prediction_at" in feature["unverified_columns"]
        assert "click_event_time" in feature["unverified_columns"]
        assert feature["data_bounds"]["prediction_at_min"]


def test_late_feature_fixture_reports_global_split_order_separately() -> None:
    from pathlib import Path

    from splitguard.engine import build_audit

    root = Path(__file__).resolve().parents[1] / "fixtures" / "late_feature"
    report = build_audit(
        root / "dataset.csv", root / "splits.csv", root / "config.json"
    ).report
    temporal = report["metrics"]["temporal"]
    assert temporal["global_enabled"] is True
    assert temporal["enabled"] is True
    assert temporal["invalid_records"] == 0
    assert temporal["matches"] == 0


def test_ingest_requires_configured_feature_and_cutoff_columns(tmp_path) -> None:
    from splitguard.ingest import load_dataset
    from splitguard.models import InputError

    path = tmp_path / "dataset.csv"
    path.write_text("record_id,text,label\nrecord-1,x,y\n", encoding="utf-8")
    config = _config(semantics="serving_time")
    with pytest.raises(InputError) as error:
        load_dataset(path, config)
    assert error.value.code == "DATASET_MISSING_COLUMN"
    missing = str(error.value)
    for column in (
        "prediction_at",
        "click_event_time",
        "click_available_at",
        "entity_id",
    ):
        assert column in missing


def test_feature_rules_require_temporal_enabled() -> None:
    with pytest.raises(Exception) as error:
        config_from_dict(
            config_data(
                temporal={
                    "enabled": False,
                    "semantics": "event_time",
                    "feature_time_rules": [
                        {"feature": "click_count", "event_time_column": "e"}
                    ],
                    "prediction_at_column": "prediction_at",
                }
            )
        )
    assert "enabled" in str(error.value)


def test_every_checked_record_reports_both_times_utc_and_violated_semantics() -> None:
    dataset = make_dataset(
        [
            row(
                "record-a",
                click_event_time="2025-01-01T20:00:00+08:00",
                click_available_at="2025-01-01T21:00:00+08:00",
                prediction_at="2025-01-01T12:00:00Z",
            ),
            row(
                "record-b",
                click_event_time="2025-01-01T10:00:00Z",
                click_available_at="2025-01-01T10:30:00Z",
                prediction_at="2025-01-01T12:00:00Z",
            ),
        ]
    )
    result = run_temporal_checks(
        dataset,
        {"record-a": "train", "record-b": "train"},
        _config(semantics="serving_time"),
    )
    evaluations = result.metrics["feature_prediction"]["evaluations"]
    assert [item["record_id"] for item in evaluations] == ["record-a", "record-b"]
    by_id = {item["record_id"]: item for item in evaluations}
    assert by_id["record-a"]["event_time_utc"] == "2025-01-01T12:00:00.000000Z"
    assert by_id["record-a"]["available_at_utc"] == "2025-01-01T13:00:00.000000Z"
    assert by_id["record-a"]["prediction_at_utc"] == "2025-01-01T12:00:00.000000Z"
    assert by_id["record-a"]["compliant"] is False
    assert by_id["record-a"]["violated_semantics"] == "serving_time"
    assert by_id["record-b"]["compliant"] is True
    assert by_id["record-b"]["violated_semantics"] is None
    # Same-instant offsets are normalized before comparison.
    assert parse_timestamp(by_id["record-a"]["event_time_raw"]).hour == 12


def test_report_surfaces_semantics_unverified_columns_and_bounds() -> None:
    from pathlib import Path

    from splitguard.engine import build_audit
    from splitguard.render import render_html, render_markdown
    from splitguard.views import render_run

    root = Path(__file__).resolve().parents[1] / "fixtures" / "late_feature"
    report = build_audit(
        root / "dataset.csv", root / "splits.csv", root / "config.json"
    ).report
    markdown = render_markdown(report)
    assert "时间与预测截止" in markdown
    assert "`serving_time`" in markdown
    assert "未能核验的列" in markdown
    assert "`click_event_time, prediction_at`" in markdown
    assert "数据边界" in markdown

    html = render_html(report)
    assert "时间与预测截止" in html
    assert "serving_time" in html
    assert "未能核验的列" in html

    console = render_run(report)
    assert "时间与预测截止" in console
    assert "serving_time" in console
    assert "数据边界" in console
    # Read-only console never embeds raw payload text or external resources.
    assert "http://" not in console.lower()
    assert "https://" not in console.lower()
