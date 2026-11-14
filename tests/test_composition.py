from splitguard.checks.composition import run_composition_checks
from tests.helpers import make_config, make_dataset, row


def test_missing_labels_and_stable_distribution_order() -> None:
    rows = [
        row("record-0001", label="demo-label-b"),
        row("record-0002", label="demo-label-a"),
        row("record-0003", label="demo-label-c"),
        row("record-0004", label="demo-label-a"),
        row("record-0005", label="demo-label-a"),
        row("record-0006", label="demo-label-a"),
        row("record-0007", label="demo-label-b"),
    ]
    result = run_composition_checks(
        make_dataset(rows),
        {
            "record-0001": "train",
            "record-0002": "train",
            "record-0003": "train",
            "record-0004": "valid",
            "record-0005": "valid",
            "record-0006": "valid",
            "record-0007": "test",
        },
        make_config(),
    )
    missing = [item for item in result.findings if item.code == "LABEL_MISSING_IN_SPLIT"]
    assert {item.split for item in missing} == {"valid", "test"}
    for item in missing:
        assert item.severity == "warn"
        assert "demo-label-c" in item.evidence["missing_labels"]
    counts = result.metrics["counts"]
    assert list(counts) == ["train", "valid", "test"]
    assert list(counts["train"]) == ["demo-label-a", "demo-label-b", "demo-label-c"]
    assert result.metrics["ratios"]["train"]["demo-label-a"] == 0.333333


def test_no_missing_labels_when_each_split_has_all() -> None:
    labels = ["demo-label-a", "demo-label-b"]
    rows = [row(f"record-{index:04d}", label=labels[index % 2]) for index in range(6)]
    result = run_composition_checks(
        make_dataset(rows),
        {f"record-{index:04d}": split for index, split in enumerate(["train", "train", "valid", "valid", "test", "test"])},
        make_config(),
    )
    assert not [item for item in result.findings if item.code == "LABEL_MISSING_IN_SPLIT"]


def test_small_split_is_info_and_does_not_crash() -> None:
    rows = [row(f"record-{index:04d}") for index in range(3)]
    dataset = make_dataset(rows)
    result = run_composition_checks(
        dataset,
        {"record-0000": "train", "record-0001": "train", "record-0002": "valid"},
        make_config(small_split_threshold=3, label_column=None),
    )
    small = [item for item in result.findings if item.code == "SPLIT_SMALL_SAMPLE"]
    assert len(small) == 2
    assert all(item.severity == "info" for item in small)
    assert result.metrics["enabled"] is False
