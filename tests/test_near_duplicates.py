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
            "candidate_mode": "bounded",
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
    assert warning.evidence["candidate_mode"] == "bounded"
    assert warning.evidence["completeness"] == "incomplete"
    assert warning.evidence["truncated"] is True
    assert warning.evidence["budget"]["candidate_budget_hit"] is True
    assert result.metrics["candidate_limit_hit"] is True
    assert result.metrics["candidate_budget_hit"] is True
    assert result.metrics["completeness"] == "incomplete"
    assert result.metrics["candidate_mode"] == "bounded"


def test_exact_mode_ignores_candidate_budget_and_completes() -> None:
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
            "candidate_mode": "exact",
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
    assert not [
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
    ]
    assert result.metrics["candidate_limit_hit"] is False
    assert result.metrics["completeness"] == "complete"
    # 预算为 1，但 exact 模式必须检查全部 4 个跨切分候选对。
    assert result.metrics["candidate_pairs_generated"] == 4
    assert result.metrics["candidate_pairs_checked"] == 4
    assert result.metrics["matches"] >= 1


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


# ---------------------------------------------------------------------------
# candidate_mode 策略与高频公共分片验收
# ---------------------------------------------------------------------------

import glob  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from splitguard.checks import near_duplicates as nd  # noqa: E402
from splitguard.config import config_from_dict  # noqa: E402
from splitguard.models import conclusion_for  # noqa: E402
from tests.helpers import config_data  # noqa: E402

HEADER = (
    "COMMON HEADER TEMPLATE FIELD alpha bravo charlie delta echo foxtrot "
    "golf hotel india juliet kilo lima "
) * 3


def _high_frequency_dataset(total: int, tail_pairs: int):
    """十万级记录共享高频页眉分片，真实近似对放在扫描末端。"""
    rows = []
    assignments = {}
    for index in range(total):
        record_id = f"record-{index:06d}"
        rows.append(
            row(record_id, text=HEADER + f" unique body number {index} tail filler")
        )
        assignments[record_id] = "train" if index % 2 == 0 else "test"
    base = total
    for pair_index in range(tail_pairs):
        left_id = f"record-{base + pair_index:06d}-a"
        right_id = f"record-{base + pair_index:06d}-b"
        passage = (
            "distinct retrieval passage marker " * 30
            + f"variant{pair_index} "
            + "closing words of the sample " * 15
        )
        rows.append(row(left_id, text=passage))
        rows.append(row(right_id, text=passage.replace("marker", "marke")))
        assignments[left_id] = "train"
        assignments[right_id] = "test"
    return make_dataset(rows), assignments


def test_high_frequency_shingle_does_not_explode_candidate_index() -> None:
    dataset, assignments = _high_frequency_dataset(100_000, 0)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.85,
            "max_candidates": 100,
            "candidate_mode": "exact",
        }
    )
    result = run_near_duplicate_checks(dataset, assignments, config)
    # 10 万条记录共享页眉，但共享分片频率高、被前缀过滤排除：候选规模远小于
    # 记录数的二次量级，且没有任何预算/截断警告伪装成无命中。
    assert result.metrics["completeness"] == "complete"
    assert result.metrics["candidate_limit_hit"] is False
    assert result.metrics["spill_used"] is False
    assert result.metrics["candidate_pairs_generated"] < 100_000
    assert result.metrics["peak_resident_postings"] < 100_000
    assert not [
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
    ]
    assert not [
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    ]


def test_exact_mode_finds_real_pairs_at_scan_tail_of_high_frequency_shingle() -> None:
    dataset, assignments = _high_frequency_dataset(100_000, 5)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.7,
            "max_candidates": 50,
            "candidate_mode": "exact",
        }
    )
    result = run_near_duplicate_checks(dataset, assignments, config)
    matches = [
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    ]
    # 即使候选预算只有 50，exact 模式也必须找到扫描末端的全部 5 对。
    assert len(matches) == 5
    assert result.metrics["matches"] == 5
    assert result.metrics["completeness"] == "complete"
    assert result.metrics["candidate_limit_hit"] is False
    for finding in matches:
        assert finding.evidence["candidate_mode"] == "exact"
        assert finding.evidence["completeness"] == "complete"
        assert finding.evidence["similarity"] >= 0.7


def test_bounded_mode_stops_at_budget_and_never_passes_clean() -> None:
    dataset, assignments = _high_frequency_dataset(100_000, 5)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.7,
            "max_candidates": 25,
            "candidate_mode": "bounded",
        }
    )
    result = run_near_duplicate_checks(dataset, assignments, config)
    assert result.metrics["candidate_budget_hit"] is True
    assert result.metrics["candidate_limit_hit"] is True
    assert result.metrics["completeness"] == "incomplete"
    assert result.metrics["candidate_pairs_generated"] == 25
    assert result.metrics["records_unchecked"] > 0
    assert result.metrics["shingle_groups_unchecked"] >= 0
    warning = next(
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
    )
    assert warning.severity == "warn"
    assert warning.evidence["truncated"] is True
    assert warning.evidence["budget"]["candidate_budget_hit"] is True
    assert warning.evidence["records_unchecked"] == result.metrics["records_unchecked"]
    # warn 存在时最终结论必须是 review，不能输出 clean pass。
    assert conclusion_for(result.findings) == "review"


def test_bounded_mode_without_budget_hit_keeps_complete_conclusion() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG.replace("golf", "golfx")),
        ]
    )
    config = make_config(
        near_duplicate={"max_candidates": 500, "candidate_mode": "bounded"}
    )
    result = run_near_duplicate_checks(
        dataset, {"record-0001": "train", "record-0002": "test"}, config
    )
    assert result.metrics["candidate_budget_hit"] is False
    assert result.metrics["completeness"] == "complete"
    match = next(
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    )
    assert match.evidence["completeness"] == "complete"


def test_same_content_across_splits_exact_mode_lists_all_near_pairs() -> None:
    rows = []
    assignments = {}
    # 3 组内容，每组 3 份改写副本，分散到三个切分。
    for group_index in range(3):
        base_text = (
            f"group{group_index} passage " * 25
            + "stable shared sentence of text " * 15
        )
        for copy_index, suffix in enumerate(("aaa", "aab", "abb")):
            record_id = f"g{group_index}-r{copy_index}"
            rows.append(row(record_id, text=base_text + f" variant suffix {suffix}"))
            assignments[record_id] = ("train", "valid", "test")[copy_index]
    dataset = make_dataset(rows)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.6,
            "max_candidates": 2,
            "candidate_mode": "exact",
        }
    )
    result = run_near_duplicate_checks(dataset, assignments, config)
    matches = [
        item
        for item in result.findings
        if item.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    ]
    # 每组 3 个副本两两配对 = 3 对，共 9 对；预算 2 不得截断 exact 模式。
    assert len(matches) == 9
    assert result.metrics["candidate_pairs_generated"] >= 9
    assert result.metrics["completeness"] == "complete"
    pair_ids = {tuple(item.record_ids) for item in matches}
    assert len(pair_ids) == 9
    similarities = [item.evidence["similarity"] for item in matches]
    assert similarities == sorted(similarities, reverse=True)


def test_results_are_stable_under_shuffled_input_order() -> None:
    import random

    dataset, assignments = _high_frequency_dataset(2_000, 3)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.7,
            "max_candidates": 50,
            "candidate_mode": "exact",
        }
    )
    baseline = run_near_duplicate_checks(dataset, assignments, config)
    shuffled_ids = list(assignments)
    random.Random(1234).shuffle(shuffled_ids)
    shuffled_assignments = {record_id: assignments[record_id] for record_id in shuffled_ids}
    rerun = run_near_duplicate_checks(dataset, shuffled_assignments, config)
    key = lambda result: [  # noqa: E731
        (
            item.code,
            item.severity,
            item.record_ids,
            item.evidence.get("similarity"),
            item.evidence.get("completeness"),
        )
        for item in result.findings
    ]
    assert key(rerun) == key(baseline)
    assert rerun.metrics["matches"] == baseline.metrics["matches"]
    assert rerun.metrics["candidate_pairs_generated"] == (
        baseline.metrics["candidate_pairs_generated"]
    )


def test_external_spill_matches_resident_results_and_cleans_temp_files(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SPLITGUARD_NEAR_DUP_RESIDENT_LIMIT", "200")
    monkeypatch.setenv("SPLITGUARD_NEAR_DUP_POSTING_CHUNK", "100")
    monkeypatch.setenv("SPLITGUARD_NEAR_DUP_PAIR_CHUNK", "100")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    dataset, assignments = _high_frequency_dataset(800, 4)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.7,
            "max_candidates": 50,
            "candidate_mode": "exact",
        }
    )
    spill_result = run_near_duplicate_checks(dataset, assignments, config)
    assert spill_result.metrics["spill_used"] is True
    assert spill_result.metrics["spill_chunks"] >= 1
    assert spill_result.metrics["pair_spill_chunks"] >= 1
    # 峰值常驻 posting 被块大小约束，未持有全量索引。
    assert spill_result.metrics["peak_resident_postings"] <= 100

    monkeypatch.delenv("SPLITGUARD_NEAR_DUP_RESIDENT_LIMIT")
    resident_result = run_near_duplicate_checks(dataset, assignments, config)
    assert resident_result.metrics["spill_used"] is False

    def signature(result):
        return sorted(
            (
                item.code,
                tuple(item.record_ids),
                item.evidence.get("similarity"),
            )
            for item in result.findings
        )

    assert signature(spill_result) == signature(resident_result)
    assert spill_result.metrics["matches"] == resident_result.metrics["matches"]
    # 外排临时目录必须清理干净，重跑不复用残留。
    assert glob.glob(str(tmp_path / "splitguard-neardup-*")) == []


def test_external_spill_interruption_cleans_temp_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SPLITGUARD_NEAR_DUP_RESIDENT_LIMIT", "1")
    monkeypatch.setenv("SPLITGUARD_NEAR_DUP_POSTING_CHUNK", "50")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    dataset, assignments = _high_frequency_dataset(400, 2)
    config = make_config(
        near_duplicate={
            "jaccard_threshold": 0.7,
            "max_candidates": 10,
            "candidate_mode": "exact",
        }
    )
    original = nd._SpillWriter.flush

    def failing_flush(self):  # noqa: ANN001
        if len(self.paths) >= 1:
            raise RuntimeError("simulated external sort interruption")
        return original(self)

    monkeypatch.setattr(nd._SpillWriter, "flush", failing_flush)
    with pytest.raises(RuntimeError, match="simulated external sort"):
        run_near_duplicate_checks(dataset, assignments, config)
    assert glob.glob(str(tmp_path / "splitguard-neardup-*")) == []

    # 修复后重跑：不存在残留块被复用，正常完成。
    monkeypatch.undo()
    result = run_near_duplicate_checks(dataset, assignments, config)
    assert result.metrics["completeness"] == "complete"
    assert glob.glob(str(tmp_path / "splitguard-neardup-*")) == []


def test_invalid_candidate_mode_is_rejected() -> None:
    data = config_data()
    data["near_duplicate"]["candidate_mode"] = "unbounded-magic"
    with pytest.raises(Exception) as error:  # noqa: PT011
        config_from_dict(data, "<test-config>")
    assert error.value.code == "CONFIG_INVALID_VALUE"


def test_default_candidate_mode_is_exact_and_serialized() -> None:
    config = make_config()
    assert config.near_duplicate.candidate_mode == "exact"
    assert config.to_dict()["near_duplicate"]["candidate_mode"] == "exact"
