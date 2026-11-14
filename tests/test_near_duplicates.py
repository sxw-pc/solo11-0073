from splitguard.checks.near_duplicates import run_near_duplicate_checks
from tests.helpers import make_config, make_dataset, row

LONG = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu"
)


def test_high_similarity_cross_split_is_blocked() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG.replace("golf", "golfx")),
        ]
    )
    result = run_near_duplicate_checks(
        dataset, {"record-0001": "train", "record-0002": "test"}, make_config()
    )
    assert [item.code for item in result.findings] == ["NEAR_DUPLICATE_CROSS_SPLIT"]
    finding = result.findings[0]
    assert finding.severity == "block"
    assert finding.record_ids == ["record-0001", "record-0002"]
    assert finding.evidence["similarity"] > 0.9
    assert finding.evidence["splits"] == ["test", "train"]
    assert "alpha bravo" not in str(finding.evidence)


def test_low_similarity_and_same_split_are_not_blocked() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text="completely different synthetic content with no overlap"),
        ]
    )
    low = run_near_duplicate_checks(
        dataset, {"record-0001": "train", "record-0002": "test"}, make_config()
    )
    assert not [item for item in low.findings if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"]

    same = make_dataset([row("record-0001", text=LONG), row("record-0002", text=LONG)])
    result = run_near_duplicate_checks(
        same, {"record-0001": "train", "record-0002": "train"}, make_config()
    )
    assert not result.findings


def test_exact_duplicate_content_is_left_to_exact_check() -> None:
    dataset = make_dataset(
        [row("record-0001", text=LONG), row("record-0002", text=LONG)]
    )
    result = run_near_duplicate_checks(
        dataset, {"record-0001": "train", "record-0002": "valid"}, make_config()
    )
    assert not [item for item in result.findings if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"]


def test_candidate_limit_warning_is_deterministic() -> None:
    common = "shared alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    dataset = make_dataset(
        [
            row("record-0001", text=common + " unique train one"),
            row("record-0002", text=common + " unique train two"),
            row("record-0003", text=common + " unique valid one"),
            row("record-0004", text=common + " unique valid two"),
        ]
    )
    config = make_config(
        near_duplicate={
            "enabled": True,
            "ngram": 5,
            "jaccard_threshold": 0.2,
            "max_candidates": 1,
        }
    )
    result = run_near_duplicate_checks(
        dataset,
        {
            "record-0001": "train",
            "record-0002": "train",
            "record-0003": "valid",
            "record-0004": "valid",
        },
        config,
    )
    warning = next(
        item for item in result.findings if item.code == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
    )
    assert warning.severity == "warn"
    assert warning.evidence["max_candidates"] == 1
    assert result.metrics["candidate_limit_hit"] is True


def test_ngram_configuration_affects_metrics() -> None:
    dataset = make_dataset([row("record-0001", text=LONG)])
    result = run_near_duplicate_checks(
        dataset,
        {"record-0001": "train"},
        make_config(near_duplicate={"ngram": 3, "enabled": True}),
    )
    assert result.metrics["ngram"] == 3
    assert result.findings == []


def test_matches_are_sorted_by_similarity_then_record_id() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG.replace("golf", "golfx")),
            row("record-0003", text=LONG.replace("alpha bravo", "alpha bravo extra")),
        ]
    )
    config = make_config(
        near_duplicate={"jaccard_threshold": 0.5, "max_candidates": 500}
    )
    result = run_near_duplicate_checks(
        dataset,
        {"record-0001": "train", "record-0002": "test", "record-0003": "valid"},
        config,
    )
    matches = [item for item in result.findings if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"]
    similarities = [item.evidence["similarity"] for item in matches]
    assert similarities == sorted(similarities, reverse=True)
    for finding in matches:
        assert finding.record_ids == sorted(finding.record_ids)
