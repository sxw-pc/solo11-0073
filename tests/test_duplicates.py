from splitguard.checks.duplicates import run_exact_duplicate_checks
from tests.helpers import make_config, make_dataset, row


def test_cross_split_exact_duplicate_is_blocked_without_raw_text() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text="  Hello   WORLD  "),
            row("record-0002", text="hello world"),
        ]
    )
    result = run_exact_duplicate_checks(
        dataset, {"record-0001": "train", "record-0002": "test"}, make_config()
    )
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.code == "EXACT_DUPLICATE_CROSS_SPLIT"
    assert finding.severity == "block"
    assert finding.record_ids == ["record-0001", "record-0002"]
    assert len(finding.evidence["content_sha256"]) == 64
    assert finding.evidence["splits"] == ["test", "train"]
    assert finding.evidence["field_names"] == ["text"]
    assert "Hello" not in str(finding.evidence)
    assert finding.evidence["summary"]["masked"] == "***"
    assert result.metrics["cross_split_groups"] == 1


def test_same_split_duplicate_is_only_warning() -> None:
    dataset = make_dataset(
        [row("record-0001", text="repeat"), row("record-0002", text="REPEAT")]
    )
    result = run_exact_duplicate_checks(
        dataset,
        {"record-0001": "train", "record-0002": "train"},
        make_config(),
    )
    assert result.findings[0].code == "EXACT_DUPLICATE_WITHIN_SPLIT"
    assert result.findings[0].severity == "warn"
    assert result.findings[0].split == "train"


def test_no_duplicate_and_empty_text() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text="alpha"),
            row("record-0002", text="beta"),
            row("record-0003", text="   "),
            row("record-0004", text=""),
        ]
    )
    result = run_exact_duplicate_checks(
        dataset,
        {
            "record-0001": "train",
            "record-0002": "valid",
            "record-0003": "test",
            "record-0004": "test",
        },
        make_config(),
    )
    assert result.findings == []
    assert result.metrics["content_groups"] == 0


def test_multiple_text_columns_are_normalized_and_combined() -> None:
    dataset = make_dataset(
        [
            {**row("record-0001"), "title": "Ａｌｐｈａ"},
            {**row("record-0002"), "title": "alpha"},
        ]
    )
    result = run_exact_duplicate_checks(
        dataset,
        {"record-0001": "train", "record-0002": "valid"},
        make_config(text_columns=["title"]),
    )
    assert result.findings[0].code == "EXACT_DUPLICATE_CROSS_SPLIT"
