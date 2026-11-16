"""Acceptance tests for near_duplicate.candidate_mode (exact vs bounded)."""

from __future__ import annotations

import json
import random
import re

import pytest

from splitguard.checks import near_duplicates
from splitguard.checks.near_duplicates import (
    _bounded_candidate_pairs,
    _exact_candidate_stream,
    _shingle_set,
    run_near_duplicate_checks,
)
from splitguard.config import load_config
from splitguard.engine import build_audit
from splitguard.models import InputError
from splitguard.normalize import combine_text_fields
from tests.helpers import make_config, make_dataset, row, write_case

LONG = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu"
)

LONG2 = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod tempor "
    "incididunt labore dolore magna aliqua enim minim veniam quis nostrud "
    "exercitation ullamco laboris nisi aliquip commodo consequat"
)


def _near_pairs(result):
    return {
        tuple(finding.record_ids)
        for finding in result.findings
        if finding.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    }


def test_exact_mode_handles_hot_shingle_at_scale() -> None:
    """100k records share one template shingle; only planted pairs match."""
    count = 100_000
    rows = [
        row(f"record-{index:06d}", text=f"shared page header {index:06d} body")
        for index in range(count)
    ]
    # Two genuine cross-split near-duplicate pairs hidden among the templates.
    # The two pairs use disjoint vocabularies so they do not match each other.
    rows[10] = row("record-000010", text=LONG)
    rows[60_000] = row("record-060000", text=LONG.replace("golf", "golfx"))
    rows[20] = row("record-000020", text=LONG2)
    rows[80_001] = row("record-080001", text=LONG2.replace("veniam", "veniamx"))
    dataset = make_dataset(rows)
    assignments = {
        f"record-{index:06d}": ("train", "valid", "test")[index % 3]
        for index in range(count)
    }

    result = run_near_duplicate_checks(dataset, assignments, make_config())

    assert _near_pairs(result) == {
        ("record-000010", "record-060000"),
        ("record-000020", "record-080001"),
    }
    metrics = result.metrics
    assert metrics["candidate_mode"] == "exact"
    assert metrics["completeness"] == "complete"
    assert metrics["candidate_limit_hit"] is False
    assert metrics["budget"] == {
        "max_candidates": 2000,
        "enforced": False,
        "exhausted": False,
    }
    # Quadratic blow-up would be ~5e9 pairs; the prefix index keeps the
    # candidate stream linear (~2.7e6 here) and the external sort bounds
    # the resident pair memory by the chunk size.
    assert metrics["candidate_pairs_generated"] < 10_000_000
    assert metrics["spill"]["externalized"] is True
    assert metrics["spill"]["chunks"] > 1

    bounded = run_near_duplicate_checks(
        dataset,
        assignments,
        make_config(near_duplicate={"candidate_mode": "bounded", "max_candidates": 1}),
    )
    assert bounded.metrics["candidate_limit_hit"] is True
    assert bounded.metrics["completeness"] == "incomplete"
    assert bounded.metrics["budget"]["exhausted"] is True
    # A budget of one can check at most one candidate, so it cannot prove
    # the two real pairs exist; exact mode found both.
    assert len(_near_pairs(bounded)) < 2


def test_exact_finds_pair_beyond_bounded_scan_budget() -> None:
    """A real pair whose shared shingle sorts last is missed by bounded only."""
    shingles_by_id = {
        "rec-a": {"hot", "u1"},
        "rec-b": {"hot", "u2"},
        "rec-c": {"hot", "u3"},
        "rec-d": {"hot", "tail"},
        "rec-e": {"hot", "tail"},
    }
    split_by_id = {
        "rec-a": "train",
        "rec-b": "valid",
        "rec-c": "train",
        "rec-d": "train",
        "rec-e": "test",
    }
    content_by_id = {record_id: f"content-{record_id}" for record_id in shingles_by_id}

    pairs, stats = _bounded_candidate_pairs(shingles_by_id, split_by_id, limit=1)
    assert stats["limit_hit"] is True
    assert stats["stopped_at_shingle"] == "hot"
    assert stats["buckets_scanned"] == 0
    assert stats["buckets_partial"] == 1
    assert stats["buckets_unscanned"] == 4  # tail, u1, u2, u3 never scanned
    assert ("rec-d", "rec-e") not in pairs

    with _exact_candidate_stream(
        shingles_by_id, content_by_id, split_by_id, 0.5, 100, None
    ) as (stream, _):
        exact_pairs = set(stream)
    assert ("rec-d", "rec-e") in exact_pairs
    assert ("rec-a", "rec-b") in exact_pairs
    # Same-split pairs are never candidates.
    assert ("rec-a", "rec-c") not in exact_pairs


def test_exact_mode_outputs_all_pairs_across_many_splits() -> None:
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG.replace("golf", "golfx")),
            row("record-0003", text=LONG.replace("hotel", "hotely")),
            row("record-0004", text=LONG.replace("golf", "golfz")),
        ]
    )
    assignments = {
        "record-0001": "train",
        "record-0002": "valid",
        "record-0003": "test",
        "record-0004": "train",
    }
    config = make_config(near_duplicate={"max_candidates": 1})
    result = run_near_duplicate_checks(dataset, assignments, config)
    # record-0001/record-0004 share a split; every other pair is cross-split
    # and above the default 0.85 threshold. max_candidates is not enforced
    # in exact mode, so nothing is truncated.
    assert _near_pairs(result) == {
        ("record-0001", "record-0002"),
        ("record-0001", "record-0003"),
        ("record-0002", "record-0003"),
        ("record-0002", "record-0004"),
        ("record-0003", "record-0004"),
    }
    assert result.metrics["completeness"] == "complete"
    assert result.metrics["candidate_limit_hit"] is False
    assert result.metrics["matches"] == 5

    # Identical content stays delegated to the exact duplicate check.
    identical = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG),
            row("record-0003", text=LONG),
        ]
    )
    exact_only = run_near_duplicate_checks(
        identical,
        {"record-0001": "train", "record-0002": "valid", "record-0003": "test"},
        make_config(),
    )
    assert _near_pairs(exact_only) == set()
    assert exact_only.metrics["completeness"] == "complete"


def test_exact_mode_matches_brute_force_on_random_corpus() -> None:
    rng = random.Random(20240611)
    words = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
        "lima mike november oscar papa quebec romeo sierra tango"
    ).split()
    texts: dict[str, str] = {}
    for index in range(60):
        texts[f"record-{index:04d}"] = " ".join(
            rng.choice(words) for _ in range(rng.randint(5, 30))
        )
    # Plant mutated copies so several true pairs exist.
    for index in (60, 61, 62, 63):
        source = texts[f"record-{index - 60:04d}"].split()
        source[rng.randrange(len(source))] = rng.choice(words)
        texts[f"record-{index:04d}"] = " ".join(source)
    dataset = make_dataset([row(record_id, text=text) for record_id, text in texts.items()])
    splits = ("train", "valid", "test")
    assignments = {
        record_id: splits[index % 3] for index, record_id in enumerate(sorted(texts))
    }

    for threshold in (0.35, 0.6, 0.85, 1.0):
        config = make_config(
            near_duplicate={
                "candidate_mode": "exact",
                "jaccard_threshold": threshold,
                "spill_chunk_size": 7,  # force the external sort path
            }
        )
        result = run_near_duplicate_checks(dataset, assignments, config)
        shingle_sets = {
            record_id: _shingle_set(
                combine_text_fields(record.values, ("text",)), 5
            )
            for record_id, record in dataset.records.items()
        }
        expected: set[tuple[str, str]] = set()
        ids = sorted(texts)
        for left_index, left in enumerate(ids):
            for right in ids[left_index + 1 :]:
                if assignments[left] == assignments[right]:
                    continue
                if texts[left] == texts[right]:
                    continue
                union = shingle_sets[left] | shingle_sets[right]
                if not union:
                    continue
                jaccard = len(shingle_sets[left] & shingle_sets[right]) / len(union)
                if jaccard >= threshold:
                    expected.add((left, right))
        assert _near_pairs(result) == expected
        assert result.metrics["completeness"] == "complete"
        if threshold == 0.35:
            # The low-threshold run generates enough candidates to spill.
            assert result.metrics["spill"]["chunks"] > 0


def test_exact_results_stable_when_scan_order_shuffled(monkeypatch) -> None:
    common = "shared alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    rows = [
        row("record-0001", text=LONG),
        row("record-0002", text=LONG.replace("golf", "golfx")),
        row("record-0003", text=LONG.replace("hotel", "hotely")),
    ]
    rows += [
        row(f"record-{index:04d}", text=f"{common} variant {index}")
        for index in range(4, 12)
    ]
    dataset = make_dataset(rows)
    assignments = {
        record_id: ("train", "valid", "test")[index % 3]
        for index, record_id in enumerate(sorted(record.record_id for record in dataset.records.values()))
    }
    config = make_config(
        near_duplicate={"jaccard_threshold": 0.5, "spill_chunk_size": 3}
    )
    baseline = run_near_duplicate_checks(dataset, assignments, config)
    assert baseline.metrics["spill"]["chunks"] > 0

    shuffler = random.Random(987654)
    monkeypatch.setattr(
        near_duplicates,
        "_exact_scan_order",
        lambda keys: shuffler.sample(keys, len(keys)),
    )
    shuffled = run_near_duplicate_checks(dataset, assignments, config)

    assert [finding.to_dict() for finding in shuffled.findings] == [
        finding.to_dict() for finding in baseline.findings
    ]
    assert shuffled.metrics["matches"] == baseline.metrics["matches"]
    assert (
        shuffled.metrics["candidate_pairs_generated"]
        == baseline.metrics["candidate_pairs_generated"]
    )
    assert shuffled.metrics["completeness"] == "complete"


def test_interrupted_spill_cleans_up_and_rerun_does_not_reuse_residue(
    tmp_path, monkeypatch
) -> None:
    spill_root = tmp_path / "spill"
    common = "shared alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    dataset = make_dataset(
        [
            row(f"record-{index:04d}", text=f"{common} unique tail {index}")
            for index in range(1, 9)
        ]
    )
    assignments = {
        f"record-{index:04d}": ("train", "valid")[index % 2] for index in range(1, 9)
    }
    config = make_config(
        near_duplicate={
            "candidate_mode": "exact",
            "jaccard_threshold": 0.5,
            "spill_chunk_size": 2,
            "temp_dir": str(spill_root),
        }
    )

    real_spill = near_duplicates._spill_chunk
    calls = {"count": 0}

    def flaky_spill(pairs, path):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash during external sort")
        return real_spill(pairs, path)

    monkeypatch.setattr(near_duplicates, "_spill_chunk", flaky_spill)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_near_duplicate_checks(dataset, assignments, config)
    # The interrupted run removed its temporary spill directory.
    assert spill_root.is_dir()
    assert list(spill_root.iterdir()) == []

    # A stale residue directory from another crashed process is never reused.
    monkeypatch.setattr(near_duplicates, "_spill_chunk", real_spill)
    stale = spill_root / "splitguard-near-dup-stale"
    stale.mkdir()
    (stale / "chunk-000000.jsonl").write_text(
        '["record-9999","record-9998"]\n', encoding="utf-8"
    )
    rerun = run_near_duplicate_checks(dataset, assignments, config)
    assert (stale / "chunk-000000.jsonl").read_text(encoding="utf-8") == (
        '["record-9999","record-9998"]\n'
    )
    assert "record-9999" not in {
        record_id for finding in rerun.findings for record_id in finding.record_ids
    }

    reference = run_near_duplicate_checks(
        dataset,
        assignments,
        make_config(near_duplicate={"jaccard_threshold": 0.5}),
    )
    assert [finding.to_dict() for finding in rerun.findings] == [
        finding.to_dict() for finding in reference.findings
    ]
    assert rerun.metrics["spill"]["chunks"] > 0
    # The successful rerun cleaned up after itself as well.
    assert [path.name for path in spill_root.iterdir()] == [stale.name]


def test_bounded_budget_exhaustion_marks_report_incomplete(tmp_path) -> None:
    common = "shared alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    rows = [
        row(
            f"record-{index:04d}",
            text=f"{common} tail {index}",
            group=f"entity-{index:04d}",
        )
        for index in range(1, 7)
    ]
    assignments = [
        (f"record-{index:04d}", ("train", "valid", "test")[index % 3])
        for index in range(1, 7)
    ]
    paths = write_case(
        tmp_path,
        rows,
        assignments,
        near_duplicate={
            "candidate_mode": "bounded",
            "max_candidates": 1,
            "jaccard_threshold": 0.99,
        },
        expected_ratios={"train": 1 / 3, "valid": 1 / 3, "test": 1 / 3},
        small_split_threshold=0,
    )
    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report

    # Budget exhaustion must never surface as a clean pass.
    assert report["conclusion"] == "review"
    assert report["summary"]["near_duplicate"] == {
        "candidate_mode": "bounded",
        "completeness": "incomplete",
    }
    near = report["metrics"]["near_duplicates"]
    assert near["candidate_limit_hit"] is True
    assert near["budget"] == {
        "max_candidates": 1,
        "enforced": True,
        "exhausted": True,
    }
    assert near["scan"]["buckets_total"] > 0
    limit = next(
        finding
        for finding in report["findings"]
        if finding["code"] == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
    )
    assert limit["severity"] == "warn"
    assert limit["evidence"]["budget_exhausted"] is True
    assert limit["evidence"]["completeness"] == "incomplete"
    unchecked = limit["evidence"]["unchecked"]
    assert unchecked["buckets_unscanned"] >= 0
    assert (
        unchecked["buckets_scanned"]
        + unchecked["buckets_partial"]
        + unchecked["buckets_unscanned"]
        == unchecked["buckets_total"]
    )


def test_bounded_within_budget_stays_complete_and_can_pass(tmp_path) -> None:
    rows = [
        row("record-0001", text="completely unique alpha text one", group="entity-0001"),
        row("record-0002", text="completely unique bravo text two", group="entity-0002"),
        row("record-0003", text="completely unique charlie text three", group="entity-0003"),
    ]
    assignments = [
        ("record-0001", "train"),
        ("record-0002", "valid"),
        ("record-0003", "test"),
    ]
    paths = write_case(
        tmp_path,
        rows,
        assignments,
        near_duplicate={"candidate_mode": "bounded", "max_candidates": 100},
        expected_ratios={"train": 1 / 3, "valid": 1 / 3, "test": 1 / 3},
        small_split_threshold=0,
    )
    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report
    assert report["conclusion"] == "pass"
    assert report["summary"]["near_duplicate"] == {
        "candidate_mode": "bounded",
        "completeness": "complete",
    }
    near = report["metrics"]["near_duplicates"]
    assert near["candidate_limit_hit"] is False
    assert near["budget"]["exhausted"] is False
    assert not any(
        finding["code"] == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
        for finding in report["findings"]
    )


def test_exact_mode_report_binds_strategy_and_completeness(tmp_path) -> None:
    rows = [
        row("record-0001", text="completely unique alpha text one", group="entity-0001"),
        row("record-0002", text="completely unique bravo text two", group="entity-0002"),
        row("record-0003", text="completely unique charlie text three", group="entity-0003"),
    ]
    assignments = [
        ("record-0001", "train"),
        ("record-0002", "valid"),
        ("record-0003", "test"),
    ]
    paths = write_case(
        tmp_path,
        rows,
        assignments,
        expected_ratios={"train": 1 / 3, "valid": 1 / 3, "test": 1 / 3},
        small_split_threshold=0,
    )
    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report
    assert report["conclusion"] == "pass"
    assert report["summary"]["near_duplicate"] == {
        "candidate_mode": "exact",
        "completeness": "complete",
    }
    near = report["metrics"]["near_duplicates"]
    assert near["candidate_mode"] == "exact"
    assert near["budget"]["enforced"] is False
    assert near["index"]["prefix_entries"] > 0
    assert near["spill"]["externalized"] is False


def test_exact_and_bounded_agree_when_budget_not_exhausted() -> None:
    common = "shared alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    dataset = make_dataset(
        [
            row("record-0001", text=LONG),
            row("record-0002", text=LONG.replace("golf", "golfx")),
            row("record-0003", text=LONG.replace("hotel", "hotely")),
            row("record-0004", text=f"{common} unique one"),
            row("record-0005", text=f"{common} unique two"),
            row("record-0006", text="entirely different content here"),
        ]
    )
    assignments = {
        "record-0001": "train",
        "record-0002": "valid",
        "record-0003": "test",
        "record-0004": "train",
        "record-0005": "valid",
        "record-0006": "test",
    }
    exact = run_near_duplicate_checks(
        dataset,
        assignments,
        make_config(near_duplicate={"candidate_mode": "exact"}),
    )
    bounded = run_near_duplicate_checks(
        dataset,
        assignments,
        make_config(
            near_duplicate={"candidate_mode": "bounded", "max_candidates": 5000}
        ),
    )
    exact_matches = [
        finding.to_dict()
        for finding in exact.findings
        if finding.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    ]
    bounded_matches = [
        finding.to_dict()
        for finding in bounded.findings
        if finding.code == "NEAR_DUPLICATE_CROSS_SPLIT"
    ]
    assert exact_matches == bounded_matches
    assert exact_matches  # the corpus does produce matches
    assert bounded.metrics["completeness"] == "complete"
    assert bounded.metrics["candidate_limit_hit"] is False
    assert not any(
        finding.code == "NEAR_DUPLICATE_CANDIDATE_LIMIT"
        for finding in bounded.findings
    )


def test_exact_stream_merges_many_chunks() -> None:
    """chunk_size=1 forces >64 spill files and the multi-round merge."""
    shingles_by_id = {
        f"rec-{index:02d}": {"shared-x", "shared-y", f"uniq-{index}"}
        for index in range(20)
    }
    split_by_id = {
        f"rec-{index:02d}": ("train", "valid", "test")[index % 3] for index in range(20)
    }
    content_by_id = {record_id: f"content-{record_id}" for record_id in shingles_by_id}
    with _exact_candidate_stream(
        shingles_by_id, content_by_id, split_by_id, 0.1, 1, None
    ) as (stream, stats):
        pairs = list(stream)
    assert stats["spill_chunks"] > 64
    assert pairs == sorted(set(pairs))
    expected = {
        (left, right)
        for left_index, left in enumerate(sorted(shingles_by_id))
        for right in sorted(shingles_by_id)[left_index + 1 :]
        if split_by_id[left] != split_by_id[right]
    }
    assert set(pairs) == expected


def test_candidate_mode_config_validation(tmp_path) -> None:
    bad_mode = tmp_path / "mode.json"
    bad_mode.write_text(
        json.dumps({"near_duplicate": {"candidate_mode": "fast"}}), encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_config(bad_mode)
    assert error.value.code == "CONFIG_INVALID_VALUE"
    assert error.value.field_name == "near_duplicate.candidate_mode"

    blank_mode = tmp_path / "blank.json"
    blank_mode.write_text(
        json.dumps({"near_duplicate": {"candidate_mode": "  "}}), encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_config(blank_mode)
    assert error.value.code == "CONFIG_INVALID_VALUE"

    bad_chunk = tmp_path / "chunk.json"
    bad_chunk.write_text(
        json.dumps({"near_duplicate": {"spill_chunk_size": 0}}), encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_config(bad_chunk)
    assert error.value.code == "CONFIG_INVALID_VALUE"
    assert error.value.field_name == "near_duplicate.spill_chunk_size"

    bad_temp = tmp_path / "temp.json"
    bad_temp.write_text(
        json.dumps({"near_duplicate": {"temp_dir": 5}}), encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_config(bad_temp)
    assert error.value.code == "CONFIG_INVALID_VALUE"

    ok = tmp_path / "ok.json"
    ok.write_text(
        json.dumps(
            {
                "near_duplicate": {
                    "candidate_mode": "Bounded",
                    "spill_chunk_size": 10,
                    "temp_dir": str(tmp_path / "spill"),
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_config(ok)
    assert config.near_duplicate.candidate_mode == "bounded"
    assert config.near_duplicate.spill_chunk_size == 10
    serialized = config.to_dict()["near_duplicate"]
    assert serialized["candidate_mode"] == "bounded"
    assert serialized["spill_chunk_size"] == 10
    assert serialized["temp_dir"] == str(tmp_path / "spill")


def test_candidate_mode_defaults_to_exact() -> None:
    config = make_config()
    assert config.near_duplicate.candidate_mode == "exact"
    assert config.near_duplicate.spill_chunk_size >= 1
    assert config.near_duplicate.temp_dir is None
    serialized = config.to_dict()["near_duplicate"]
    assert serialized["candidate_mode"] == "exact"
    assert "spill_chunk_size" in serialized
    assert "temp_dir" in serialized
