"""Deterministic character n-gram near duplicate detection.

Two candidate strategies are available via ``near_duplicate.candidate_mode``:

- ``exact`` (default): proves that no cross-split pair at or above the
  Jaccard threshold was missed. A frequency-ordered prefix index keeps
  high-frequency template shingles out of most buckets, and candidate
  pairs are deduplicated through a chunked external sort so the full pair
  set is never resident in memory. ``max_candidates`` is not enforced in
  this mode; the cost is CPU and (for large candidate volumes) temporary
  disk.
- ``bounded``: keeps the in-memory shingle-ordered scan with a hard
  ``max_candidates`` budget. When the budget is exhausted the scan stops
  early, the report records the budget state and the unchecked range, and
  the audit conclusion can never be a clean pass.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import Config
from ..models import CheckResult, Dataset, Finding
from ..normalize import combine_text_fields, is_empty_content

_SPILL_DIR_PREFIX = "splitguard-near-dup-"
_MERGE_FAN_IN = 64


def _shingle_set(text: str, ngram: int) -> set[str]:
    if not text:
        return set()
    if len(text) <= ngram:
        return {hashlib.sha256(text.encode("utf-8")).hexdigest()}
    return {
        hashlib.sha256(text[index : index + ngram].encode("utf-8")).hexdigest()
        for index in range(len(text) - ngram + 1)
    }


def _deduplicate_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_candidate_pairs(
    shingles_by_id: dict[str, set[str]],
    split_by_id: dict[str, str],
    limit: int,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Legacy in-memory scan with a hard candidate budget.

    Buckets are scanned in ascending shingle-hash order and pairs in
    ascending record-id order, so the truncation point is deterministic.
    The returned stats describe the unchecked range when the budget stops
    the scan early.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for record_id in sorted(shingles_by_id):
        for shingle in sorted(shingles_by_id[record_id]):
            buckets[shingle].append(record_id)
    ordered_shingles = sorted(buckets)
    stats: dict[str, Any] = {
        "limit_hit": False,
        "buckets_total": len(ordered_shingles),
        "buckets_scanned": 0,
        "buckets_partial": 0,
        "buckets_unscanned": 0,
        "shingle_occurrences_unscanned": 0,
        "stopped_at_shingle": None,
    }
    pairs: set[tuple[str, str]] = set()
    stop_index: int | None = None
    for position, shingle in enumerate(ordered_shingles):
        ids = sorted(buckets[shingle])
        stop = False
        for left_index in range(len(ids) - 1):
            left = ids[left_index]
            for right in ids[left_index + 1 :]:
                if split_by_id[left] == split_by_id[right]:
                    continue
                pair = (left, right)
                if pair in pairs:
                    continue
                if len(pairs) >= limit:
                    stats["limit_hit"] = True
                    stats["buckets_partial"] = 1
                    stats["stopped_at_shingle"] = shingle
                    stop = True
                    break
                pairs.add(pair)
            if stop:
                break
        if stop:
            stop_index = position
            break
        stats["buckets_scanned"] += 1
    if stop_index is not None:
        remaining = ordered_shingles[stop_index + 1 :]
        stats["buckets_unscanned"] = len(remaining)
        stats["shingle_occurrences_unscanned"] = sum(
            len(buckets[shingle]) for shingle in remaining
        )
    return sorted(pairs), stats


def _shingle_frequencies(shingles_by_id: dict[str, set[str]]) -> dict[str, int]:
    frequencies: dict[str, int] = defaultdict(int)
    for shingles in shingles_by_id.values():
        for shingle in shingles:
            frequencies[shingle] += 1
    return frequencies


def _prefix_index(
    shingles_by_id: dict[str, set[str]],
    frequencies: dict[str, int],
    threshold: float,
) -> tuple[dict[str, list[str]], int]:
    """Frequency-ordered prefix index.

    Each record contributes its ``size - ceil(threshold * size) + 1``
    rarest shingles (ties broken by hash). By the prefix-filter theorem,
    any pair of records whose Jaccard similarity is at least ``threshold``
    shares at least one indexed prefix shingle, so scanning these buckets
    is a complete candidate filter. High-frequency template shingles sort
    last and therefore appear in few prefixes, which keeps their buckets
    small. The ceil is computed with exact integer arithmetic so floating
    point rounding can never shrink a prefix below the complete size.
    """
    threshold_num, threshold_den = threshold.as_integer_ratio()
    index: dict[str, list[str]] = defaultdict(list)
    entries = 0
    for record_id in sorted(shingles_by_id):
        shingles = shingles_by_id[record_id]
        ordered = sorted(shingles, key=lambda shingle: (frequencies[shingle], shingle))
        size = len(shingles)
        overlap = -(-threshold_num * size // threshold_den)
        prefix_size = size - overlap + 1
        for shingle in ordered[:prefix_size]:
            index[shingle].append(record_id)
            entries += 1
    return index, entries


def _iter_bucket_pairs(
    record_ids: list[str],
    content_by_id: dict[str, str],
    split_by_id: dict[str, str],
) -> Iterator[tuple[str, str]]:
    """Yield cross-split, distinct-content pairs from one bucket.

    Records with identical normalized content are delegated to the exact
    duplicate check, so pairs inside one content group are never
    candidates; grouping first avoids enumerating them pairwise.
    """
    by_content: dict[str, dict[str, list[str]]] = {}
    for record_id in record_ids:
        by_content.setdefault(content_by_id[record_id], {}).setdefault(
            split_by_id[record_id], []
        ).append(record_id)
    contents = sorted(by_content)
    for left_index, left_content in enumerate(contents):
        left_splits = by_content[left_content]
        for right_content in contents[left_index + 1 :]:
            right_splits = by_content[right_content]
            for left_split in sorted(left_splits):
                left_ids = left_splits[left_split]
                for right_split in sorted(right_splits):
                    if left_split == right_split:
                        continue
                    for left in left_ids:
                        for right in right_splits[right_split]:
                            if left < right:
                                yield (left, right)
                            else:
                                yield (right, left)


def _exact_scan_order(shingles: list[str]) -> list[str]:
    """Deterministic bucket scan order for the exact candidate stream.

    The stream is sorted and deduplicated downstream, so any total order
    yields the same final candidates; sorting keeps generation stable.
    """
    return sorted(shingles)


def _spill_chunk(pairs: list[tuple[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for left, right in pairs:
            handle.write(json.dumps([left, right], ensure_ascii=False))
            handle.write("\n")


def _iter_chunk(handle: Any) -> Iterator[list[str]]:
    for line in handle:
        if line.strip():
            yield json.loads(line)


def _merge_streams(paths: list[Path]) -> Iterator[tuple[str, str]]:
    handles = [path.open("r", encoding="utf-8") for path in paths]
    try:
        previous: Any = object()
        for pair in heapq.merge(*(_iter_chunk(handle) for handle in handles)):
            if pair != previous:
                yield (pair[0], pair[1])
                previous = pair
    finally:
        for handle in handles:
            handle.close()


def _dedup_sorted(pairs: list[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    previous: Any = object()
    for pair in pairs:
        if pair != previous:
            yield pair
            previous = pair


def _reduce_chunks(paths: list[Path], spill_dir: Path) -> list[Path]:
    """Merge chunk files until at most ``_MERGE_FAN_IN`` remain."""
    round_no = 0
    while len(paths) > _MERGE_FAN_IN:
        reduced: list[Path] = []
        for start in range(0, len(paths), _MERGE_FAN_IN):
            group = paths[start : start + _MERGE_FAN_IN]
            out_path = spill_dir / f"merge-{round_no:02d}-{len(reduced):06d}.jsonl"
            with out_path.open("w", encoding="utf-8") as handle:
                for left, right in _merge_streams(group):
                    handle.write(json.dumps([left, right], ensure_ascii=False))
                    handle.write("\n")
            reduced.append(out_path)
        for path in paths:
            path.unlink(missing_ok=True)
        paths = reduced
        round_no += 1
    return paths


def _make_spill_dir(temp_dir: str | None) -> Path:
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=_SPILL_DIR_PREFIX, dir=temp_dir))


@contextlib.contextmanager
def _exact_candidate_stream(
    shingles_by_id: dict[str, set[str]],
    content_by_id: dict[str, str],
    split_by_id: dict[str, str],
    threshold: float,
    chunk_size: int,
    temp_dir: str | None,
) -> Iterator[tuple[Iterator[tuple[str, str]], dict[str, Any]]]:
    """Stream unique candidate pairs without holding the full pair set.

    Pairs are buffered up to ``chunk_size`` and spilled to sorted chunk
    files under a fresh temporary directory; a k-way merge then yields the
    deduplicated pairs. The temporary directory is removed on exit —
    including exceptional exit — and is never reused by a later run.
    """
    frequencies = _shingle_frequencies(shingles_by_id)
    index, prefix_entries = _prefix_index(shingles_by_id, frequencies, threshold)
    stats: dict[str, Any] = {
        "distinct_shingles": len(frequencies),
        "prefix_entries": prefix_entries,
        "prefix_buckets": len(index),
        "pairs_written": 0,
        "spill_chunks": 0,
    }
    spill_dir: Path | None = None
    chunks: list[Path] = []
    buffer: list[tuple[str, str]] = []
    stream: Iterator[tuple[str, str]] | None = None
    try:
        for shingle in _exact_scan_order(list(index)):
            for pair in _iter_bucket_pairs(index[shingle], content_by_id, split_by_id):
                buffer.append(pair)
                if len(buffer) >= chunk_size:
                    if spill_dir is None:
                        spill_dir = _make_spill_dir(temp_dir)
                    chunk_path = spill_dir / f"chunk-{len(chunks):06d}.jsonl"
                    buffer.sort()
                    _spill_chunk(buffer, chunk_path)
                    stats["pairs_written"] += len(buffer)
                    chunks.append(chunk_path)
                    buffer = []
        if chunks:
            if buffer:
                if spill_dir is None:  # pragma: no cover - chunks imply spill_dir
                    spill_dir = _make_spill_dir(temp_dir)
                chunk_path = spill_dir / f"chunk-{len(chunks):06d}.jsonl"
                buffer.sort()
                _spill_chunk(buffer, chunk_path)
                stats["pairs_written"] += len(buffer)
                chunks.append(chunk_path)
                buffer = []
            stats["spill_chunks"] = len(chunks)
            chunks = _reduce_chunks(chunks, spill_dir)
            stream = _merge_streams(chunks)
        else:
            buffer.sort()
            stats["pairs_written"] += len(buffer)
            stream = _dedup_sorted(buffer)
        yield stream, stats
    finally:
        if stream is not None and hasattr(stream, "close"):
            stream.close()
        if spill_dir is not None:
            shutil.rmtree(spill_dir, ignore_errors=True)


def _collect_matches(
    pairs: Iterator[tuple[str, str]] | list[tuple[str, str]],
    shingles_by_id: dict[str, set[str]],
    content_by_id: dict[str, str],
    threshold: float,
    metrics: dict[str, Any],
) -> list[tuple[float, str, str]]:
    matched: list[tuple[float, str, str]] = []
    for left, right in pairs:
        metrics["candidate_pairs_generated"] += 1
        if content_by_id[left] == content_by_id[right]:
            # Exact duplicate content is already represented by the exact check.
            continue
        metrics["candidate_pairs_checked"] += 1
        left_shingles = shingles_by_id[left]
        right_shingles = shingles_by_id[right]
        union = left_shingles | right_shingles
        if not union:
            continue
        intersection = left_shingles & right_shingles
        similarity = len(intersection) / len(union)
        if similarity >= threshold:
            matched.append((similarity, left, right))
    return matched


def _build_match_findings(
    matched: list[tuple[float, str, str]],
    shingles_by_id: dict[str, set[str]],
    split_by_id: dict[str, str],
    content_by_id: dict[str, str],
    config: Config,
) -> list[Finding]:
    options = config.near_duplicate
    findings: list[Finding] = []
    matched.sort(key=lambda item: (-round(item[0], 6), item[1], item[2]))
    for similarity, left, right in matched:
        splits = sorted((split_by_id[left], split_by_id[right]))
        shared = len(shingles_by_id[left] & shingles_by_id[right])
        union = len(shingles_by_id[left] | shingles_by_id[right])
        record_ids = sorted((left, right))
        findings.append(
            Finding(
                code="NEAR_DUPLICATE_CROSS_SPLIT",
                severity="block",
                message=(
                    f"近似重复 {left} / {right} 的 Jaccard={similarity:.6f}，"
                    f"跨切分 {splits[0]}, {splits[1]}"
                ),
                split=None,
                record_ids=record_ids,
                evidence={
                    "similarity": round(similarity, 6),
                    "record_ids": record_ids,
                    "splits": splits,
                    "split_by_record": {
                        left: split_by_id[left],
                        right: split_by_id[right],
                    },
                    "content_sha256": {
                        left: content_by_id[left],
                        right: content_by_id[right],
                    },
                    "shared_shingle_count": shared,
                    "union_shingle_count": union,
                    "ngram": options.ngram,
                    "threshold": options.jaccard_threshold,
                    "field_names": list(config.text_columns),
                },
            )
        )
    return findings


def _candidate_limit_finding(
    config: Config, generated: int, scan_stats: dict[str, Any]
) -> Finding:
    options = config.near_duplicate
    return Finding(
        code="NEAR_DUPLICATE_CANDIDATE_LIMIT",
        severity="warn",
        message=(
            f"近似重复候选预算 {options.max_candidates} 已耗尽（bounded 模式），"
            "部分候选未检查，审计结论不完整，不能视为无泄漏"
        ),
        split=None,
        record_ids=[],
        evidence={
            "candidate_mode": "bounded",
            "completeness": "incomplete",
            "max_candidates": options.max_candidates,
            "candidate_pairs_generated": generated,
            "candidate_pairs_checked": generated,
            "budget_exhausted": True,
            "truncated": True,
            "unchecked": {
                "buckets_total": scan_stats["buckets_total"],
                "buckets_scanned": scan_stats["buckets_scanned"],
                "buckets_partial": scan_stats["buckets_partial"],
                "buckets_unscanned": scan_stats["buckets_unscanned"],
                "shingle_occurrences_unscanned": scan_stats[
                    "shingle_occurrences_unscanned"
                ],
                "stopped_at_shingle": scan_stats["stopped_at_shingle"],
            },
        },
    )


def run_near_duplicate_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    options = config.near_duplicate
    metrics: dict[str, Any] = {
        "enabled": options.enabled and bool(config.text_columns),
        "ngram": options.ngram,
        "jaccard_threshold": options.jaccard_threshold,
        "max_candidates": options.max_candidates,
        "candidate_mode": options.candidate_mode,
        "completeness": "complete",
        "candidate_pairs_generated": 0,
        "candidate_pairs_checked": 0,
        "candidate_limit_hit": False,
        "budget_exhausted": False,
        "matches": 0,
    }
    if not options.enabled or not config.text_columns:
        metrics["completeness"] = "not_run"
        return CheckResult([], metrics)

    content_by_id: dict[str, str] = {}
    shingles_by_id: dict[str, set[str]] = {}
    split_by_id: dict[str, str] = {}
    for record_id in sorted(assignments):
        record = dataset.records[record_id]
        normalized = combine_text_fields(record.values, config.text_columns)
        if is_empty_content(normalized):
            continue
        content_by_id[record_id] = _deduplicate_content_hash(normalized)
        shingles_by_id[record_id] = _shingle_set(normalized, options.ngram)
        split_by_id[record_id] = assignments[record_id]

    findings: list[Finding] = []
    if options.candidate_mode == "bounded":
        candidates, scan_stats = _bounded_candidate_pairs(
            shingles_by_id, split_by_id, options.max_candidates
        )
        limit_hit = bool(scan_stats["limit_hit"])
        metrics["candidate_limit_hit"] = limit_hit
        metrics["budget_exhausted"] = limit_hit
        metrics["budget"] = {
            "max_candidates": options.max_candidates,
            "enforced": True,
            "exhausted": limit_hit,
        }
        metrics["scan"] = {
            key: scan_stats[key]
            for key in (
                "buckets_total",
                "buckets_scanned",
                "buckets_partial",
                "buckets_unscanned",
                "shingle_occurrences_unscanned",
                "stopped_at_shingle",
            )
        }
        if limit_hit:
            metrics["completeness"] = "incomplete"
            findings.append(
                _candidate_limit_finding(config, len(candidates), scan_stats)
            )
        matched = _collect_matches(
            candidates,
            shingles_by_id,
            content_by_id,
            options.jaccard_threshold,
            metrics,
        )
    else:
        metrics["budget"] = {
            "max_candidates": options.max_candidates,
            "enforced": False,
            "exhausted": False,
        }
        with _exact_candidate_stream(
            shingles_by_id,
            content_by_id,
            split_by_id,
            options.jaccard_threshold,
            options.spill_chunk_size,
            options.temp_dir,
        ) as (stream, stream_stats):
            metrics["index"] = {
                "distinct_shingles": stream_stats["distinct_shingles"],
                "prefix_entries": stream_stats["prefix_entries"],
                "prefix_buckets": stream_stats["prefix_buckets"],
            }
            metrics["spill"] = {
                "chunk_size": options.spill_chunk_size,
                "chunks": stream_stats["spill_chunks"],
                "pairs_written": stream_stats["pairs_written"],
                "temp_dir": options.temp_dir,
                "externalized": stream_stats["spill_chunks"] > 0,
            }
            matched = _collect_matches(
                stream,
                shingles_by_id,
                content_by_id,
                options.jaccard_threshold,
                metrics,
            )

    findings.extend(
        _build_match_findings(
            matched, shingles_by_id, split_by_id, content_by_id, config
        )
    )
    metrics["matches"] = len(matched)
    return CheckResult(findings, metrics)
