"""Deterministic character n-gram near duplicate detection."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from ..config import Config
from ..models import CheckResult, Dataset, Finding
from ..normalize import combine_text_fields, is_empty_content


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


def _candidate_pairs(
    shingles_by_id: dict[str, set[str]],
    split_by_id: dict[str, str],
    limit: int,
) -> tuple[list[tuple[str, str]], bool]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for record_id in sorted(shingles_by_id):
        for shingle in sorted(shingles_by_id[record_id]):
            buckets[shingle].append(record_id)
    pairs: set[tuple[str, str]] = set()
    limit_hit = False
    stop = False
    for shingle in sorted(buckets):
        ids = sorted(buckets[shingle])
        for left_index in range(len(ids) - 1):
            left = ids[left_index]
            for right in ids[left_index + 1 :]:
                if split_by_id[left] == split_by_id[right]:
                    continue
                pair = (left, right)
                if pair in pairs:
                    continue
                if len(pairs) >= limit:
                    limit_hit = True
                    stop = True
                    break
                pairs.add(pair)
            if stop:
                break
        if stop:
            break
    return sorted(pairs), limit_hit


def run_near_duplicate_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    options = config.near_duplicate
    metrics = {
        "enabled": options.enabled and bool(config.text_columns),
        "ngram": options.ngram,
        "jaccard_threshold": options.jaccard_threshold,
        "max_candidates": options.max_candidates,
        "candidate_pairs_generated": 0,
        "candidate_pairs_checked": 0,
        "candidate_limit_hit": False,
        "matches": 0,
    }
    if not options.enabled or not config.text_columns:
        return CheckResult([], metrics)

    normalized_by_id: dict[str, str] = {}
    content_by_id: dict[str, str] = {}
    shingles_by_id: dict[str, set[str]] = {}
    split_by_id: dict[str, str] = {}
    for record_id in sorted(assignments):
        record = dataset.records[record_id]
        normalized = combine_text_fields(record.values, config.text_columns)
        if is_empty_content(normalized):
            continue
        normalized_by_id[record_id] = normalized
        content_by_id[record_id] = _deduplicate_content_hash(normalized)
        shingles_by_id[record_id] = _shingle_set(normalized, options.ngram)
        split_by_id[record_id] = assignments[record_id]

    candidates, limit_hit = _candidate_pairs(
        shingles_by_id, split_by_id, options.max_candidates
    )
    metrics["candidate_pairs_generated"] = len(candidates)
    metrics["candidate_limit_hit"] = limit_hit

    findings: list[Finding] = []
    if limit_hit:
        findings.append(
            Finding(
                code="NEAR_DUPLICATE_CANDIDATE_LIMIT",
                severity="warn",
                message=(
                    f"近似重复候选对达到上限 {options.max_candidates}，"
                    "后续候选未检查"
                ),
                split=None,
                record_ids=[],
                evidence={
                    "max_candidates": options.max_candidates,
                    "candidate_pairs_checked": len(candidates),
                    "truncated": True,
                },
            )
        )

    matched: list[tuple[float, str, str]] = []
    for left, right in candidates:
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
        if similarity >= options.jaccard_threshold:
            matched.append((similarity, left, right))

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
    metrics["matches"] = len(matched)
    return CheckResult(findings, metrics)
