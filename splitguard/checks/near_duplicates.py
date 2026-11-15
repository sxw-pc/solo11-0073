"""Deterministic character n-gram near duplicate detection.

候选生成提供两种明确策略（``near_duplicate.candidate_mode``）：

- ``exact``（默认）：检查所有可能满足 Jaccard 阈值的跨切分候选对，绝不在
  候选数量上提前停止。倒排索引按分片全局频率排序，并使用前缀过滤把高频公共
  分片（页眉/模板）从常驻索引中剔除；索引超预算时切换为逐块外排，全程不常驻
  候选 pair 集合，因此可以给出完整结论。
- ``bounded``：在同样的频率排序前缀索引上运行，但受 ``max_candidates`` 候选
  预算约束，达到预算即停止，并通过 warn finding 把结论标记为 incomplete/review。
"""

from __future__ import annotations

import hashlib
import heapq
import math
import os
import tempfile
from collections import Counter
from pathlib import Path

from ..config import Config
from ..models import CheckResult, Dataset, Finding
from ..normalize import combine_text_fields, is_empty_content

# 常驻内存的倒排条目（(rank, record_id) posting）软预算。超出后 exact 模式
# 切换到逐块外排路径。测试可通过环境变量调小以强制走外排。
MAX_RESIDENT_POSTINGS = 200_000
# 外排路径中单个排序块持有的条目数上限。
POSTING_CHUNK_SIZE = 50_000
PAIR_CHUNK_SIZE = 50_000

_LIMIT_CODE = "NEAR_DUPLICATE_CANDIDATE_LIMIT"
_MATCH_CODE = "NEAR_DUPLICATE_CROSS_SPLIT"


def _shingle_set(text: str, ngram: int, intern: dict[str, str]) -> set[str]:
    if not text:
        return set()
    if len(text) <= ngram:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {intern.setdefault(digest, digest)}
    return {
        intern.setdefault(digest, digest)
        for digest in (
            hashlib.sha256(text[index : index + ngram].encode("utf-8")).hexdigest()
            for index in range(len(text) - ngram + 1)
        )
    }


def _deduplicate_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_limit(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _prefix_tokens(
    shingles: set[str], rank_by_shingle: dict[str, int], threshold: float
) -> list[str]:
    """返回记录在频率排序索引上的前缀分片。

    只考虑全局频率 >= 2 的可共享分片（频率 1 的分片不可能成为任何 pair 的
    交集）。设可共享分片数为 s、全集合大小为 n，PPJoin 前缀长度为
    ``s - ceil(threshold * n) + 1``；若该值 <= 0，说明即使共享全部可共享分片
    也达不到阈值，该记录不可能与任何记录匹配，返回空前缀。
    """

    indexable = [shingle for shingle in shingles if shingle in rank_by_shingle]
    if not indexable:
        return []
    required = math.ceil(threshold * len(shingles))
    bound = len(indexable) - required + 1
    if bound <= 0:
        return []
    indexable.sort(key=rank_by_shingle.__getitem__)
    return indexable[:bound]


class _StreamingPrefixIndex:
    """频率排序的流式前缀倒排索引。

    只常驻每个分片集合的前缀 posting；高频公共分片因排名靠后而不进入索引，
    因此 10 万条记录共享同一模板分片时不会产生二次级 posting。候选 pair 在
    生成时即被消费，不保留全局 pair 集合。
    """

    def __init__(
        self,
        rank_by_shingle: dict[str, int],
        split_by_id: dict[str, str],
        content_by_id: dict[str, str],
        threshold: float,
        *,
        budget: int | None,
    ) -> None:
        self._rank_by_shingle = rank_by_shingle
        self._split_by_id = split_by_id
        self._content_by_id = content_by_id
        self._threshold = threshold
        # None 表示 exact 模式：不设候选预算。
        self._budget = budget
        self.postings: dict[int, list[str]] = {}
        self.candidate_pairs_generated = 0
        self.budget_hit = False
        self.stopped_at_record: str | None = None
        self.records_processed = 0

    def add_record(
        self, record_id: str, shingles: set[str]
    ) -> list[tuple[str, str]]:
        prefix = _prefix_tokens(shingles, self._rank_by_shingle, self._threshold)
        split = self._split_by_id[record_id]
        content_hash = self._content_by_id[record_id]
        emitted: list[tuple[str, str]] = []
        local_pairs: set[tuple[str, str]] = set()
        for shingle in prefix:
            rank = self._rank_by_shingle[shingle]
            for other_id in self.postings.get(rank, ()):  # 按插入 ID 有序
                if self._split_by_id[other_id] == split:
                    continue
                if self._content_by_id[other_id] == content_hash:
                    # 完全相同内容交给精确重复检查，不计候选预算。
                    continue
                pair = tuple(sorted((record_id, other_id)))
                if pair in local_pairs:
                    continue
                local_pairs.add(pair)
                if (
                    self._budget is not None
                    and self.candidate_pairs_generated >= self._budget
                ):
                    self.budget_hit = True
                    self.stopped_at_record = record_id
                    return emitted
                emitted.append(pair)
                self.candidate_pairs_generated += 1
        for shingle in prefix:
            self.postings.setdefault(self._rank_by_shingle[shingle], []).append(
                record_id
            )
        self.records_processed += 1
        return emitted


class _SpillWriter:
    """累积内存块，达到块大小后排序并外写到临时目录。"""

    def __init__(self, temp_dir: Path, prefix: str, chunk_size: int) -> None:
        self._temp_dir = temp_dir
        self._prefix = prefix
        self._chunk_size = chunk_size
        self._buffer: list[tuple] = []
        self.paths: list[Path] = []
        self.peak_resident = 0

    def add(self, row: tuple) -> None:
        self._buffer.append(row)
        if len(self._buffer) > self.peak_resident:
            self.peak_resident = len(self._buffer)
        if len(self._buffer) >= self._chunk_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        self._buffer.sort()
        path = self._temp_dir / f"{self._prefix}-chunk-{len(self.paths):06d}.tsv"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(
                "\t".join(str(part) for part in row) + "\n" for row in self._buffer
            )
        self.paths.append(path)
        self._buffer = []

    @property
    def buffered(self) -> int:
        return len(self._buffer)


def _iter_merged_rows(paths: list[Path], tail: list[tuple], parse_row):
    """对落盘排序块与内存尾块做确定性 k 路归并，逐行产出。"""

    def _read(path: Path):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                yield parse_row(line.rstrip("\n").split("\t"))

    streams = [_read(path) for path in paths]
    heap: list[tuple] = []
    for index, iterator in enumerate(streams):
        try:
            heapq.heappush(heap, (next(iterator), index))
        except StopIteration:
            pass
    tail_sorted = sorted(tail)
    tail_index = 0
    while heap or tail_index < len(tail_sorted):
        if not heap or (
            tail_index < len(tail_sorted)
            and tail_sorted[tail_index] <= heap[0][0]
        ):
            yield tail_sorted[tail_index]
            tail_index += 1
            continue
        row, stream_index = heapq.heappop(heap)
        yield row
        try:
            heapq.heappush(heap, (next(streams[stream_index]), stream_index))
        except StopIteration:
            pass


def _run_external_exact(
    record_ids: list[str],
    shingles_by_id: dict[str, set[str]],
    content_by_id: dict[str, str],
    split_by_id: dict[str, str],
    rank_by_shingle: dict[str, int],
    threshold: float,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """exact 模式的逐块外排实现。

    第一趟把前缀 posting 外排为 (rank, record_id) 块，归并枚举候选；候选
    pair 再次外排为 (left, right) 块，归并去重后才逐对计算 Jaccard。posting
    只含各记录的前缀分片，高频模板分片排名靠后、不进入任何前缀，因此单个分
    片组不会出现二次级枚举；任何时刻常驻内存都被块大小约束，且不保留全局
    pair 集合。
    """

    posting_limit = _chunk_limit(
        "SPLITGUARD_NEAR_DUP_POSTING_CHUNK", POSTING_CHUNK_SIZE
    )
    pair_limit = _chunk_limit("SPLITGUARD_NEAR_DUP_PAIR_CHUNK", PAIR_CHUNK_SIZE)
    stats = {
        "index_postings": 0,
        "peak_resident_postings": 0,
        "spill_chunks": 0,
        "pair_spill_chunks": 0,
    }
    temp_owner = tempfile.TemporaryDirectory(prefix="splitguard-neardup-")
    temp_dir = Path(temp_owner.name)
    try:
        posting_writer = _SpillWriter(temp_dir, "postings", posting_limit)
        for record_id in record_ids:
            prefix = _prefix_tokens(
                shingles_by_id[record_id], rank_by_shingle, threshold
            )
            for shingle in prefix:
                posting_writer.add((rank_by_shingle[shingle], record_id))
                stats["index_postings"] += 1
        posting_writer.flush()
        stats["peak_resident_postings"] = posting_writer.peak_resident
        stats["spill_chunks"] = len(posting_writer.paths)

        pair_writer = _SpillWriter(temp_dir, "pairs", pair_limit)
        current_rank: int | None = None
        group: list[tuple[str, str, str]] = []

        def flush_group() -> None:
            if current_rank is None or len(group) < 2:
                return
            # 组内成员都把当前分片包含在自身前缀中；高频分片不会进入前缀，
            # 因此这里的枚举规模有界。重复发射由 pair 外排归并去重。
            for left_index in range(len(group) - 1):
                left_id, left_split, left_hash = group[left_index]
                for right_id, right_split, right_hash in group[
                    left_index + 1 :
                ]:
                    if left_split == right_split:
                        continue
                    if left_hash == right_hash:
                        # 完全相同内容交给精确重复检查，且可避免同模板记录的
                        # 二次级 pair 落盘。
                        continue
                    pair_writer.add(tuple(sorted((left_id, right_id))))

        def parse_posting(parts: list[str]) -> tuple[int, str]:
            return int(parts[0]), parts[1]

        for row in _iter_merged_rows(posting_writer.paths, [], parse_posting):
            rank, record_id = row
            if rank != current_rank:
                flush_group()
                current_rank = rank
                group = []
            group.append(
                (record_id, split_by_id[record_id], content_by_id[record_id])
            )
        flush_group()
        pair_writer.flush()
        stats["pair_spill_chunks"] = len(pair_writer.paths)
        stats["peak_resident_postings"] = max(
            stats["peak_resident_postings"], pair_writer.peak_resident
        )

        def parse_pair(parts: list[str]) -> tuple[str, str]:
            return parts[0], parts[1]

        candidates: list[tuple[str, str]] = []
        previous: tuple[str, str] | None = None
        for row in _iter_merged_rows(pair_writer.paths, [], parse_pair):
            if row != previous:
                candidates.append(row)
                previous = row
        return candidates, stats
    finally:
        # 中断（异常/取消）时同样清理；每次运行使用唯一临时目录，重跑绝不会
        # 复用残留块。
        temp_owner.cleanup()


def _build_records(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, str]]:
    content_by_id: dict[str, str] = {}
    shingles_by_id: dict[str, set[str]] = {}
    split_by_id: dict[str, str] = {}
    # 驻留一份分片哈希字符串，模板/页眉分片在 10 万条记录间共享同一对象，
    # 避免每条记录各持一份 64 字节哈希。
    intern: dict[str, str] = {}
    for record_id in sorted(assignments):
        record = dataset.records[record_id]
        normalized = combine_text_fields(record.values, config.text_columns)
        if is_empty_content(normalized):
            continue
        content_by_id[record_id] = _deduplicate_content_hash(normalized)
        shingles_by_id[record_id] = _shingle_set(
            normalized, config.near_duplicate.ngram, intern
        )
        split_by_id[record_id] = assignments[record_id]
    return content_by_id, shingles_by_id, split_by_id


def _limit_finding(options, evidence: dict) -> Finding:  # type: ignore[no-untyped-def]
    return Finding(
        code=_LIMIT_CODE,
        severity="warn",
        message=(
            f"bounded 近似重复候选预算 {options.max_candidates} 已达到，"
            "审计在候选预算内提前停止，结论不完整（incomplete/review），"
            "未检查范围见 evidence"
        ),
        split=None,
        record_ids=[],
        evidence=evidence,
    )


def run_near_duplicate_checks(
    dataset: Dataset, assignments: dict[str, str], config: Config
) -> CheckResult:
    options = config.near_duplicate
    mode = options.candidate_mode
    threshold = options.jaccard_threshold
    metrics: dict = {
        "enabled": options.enabled and bool(config.text_columns),
        "ngram": options.ngram,
        "jaccard_threshold": threshold,
        "max_candidates": options.max_candidates,
        "candidate_mode": mode,
        "completeness": "complete",
        "candidate_pairs_generated": 0,
        "candidate_pairs_checked": 0,
        "candidate_limit_hit": False,
        "candidate_budget_hit": False,
        "matches": 0,
        "index_records": 0,
        "index_distinct_shingles": 0,
        "index_postings": 0,
        "peak_resident_postings": 0,
        "spill_used": False,
        "spill_chunks": 0,
        "pair_spill_chunks": 0,
        "records_unchecked": 0,
        "shingle_groups_unchecked": 0,
        "stopped_at_record": None,
    }
    if not options.enabled or not config.text_columns:
        return CheckResult([], metrics)

    content_by_id, shingles_by_id, split_by_id = _build_records(
        dataset, assignments, config
    )
    record_ids = sorted(shingles_by_id)
    metrics["index_records"] = len(record_ids)

    frequencies = Counter(
        shingle
        for record_id in record_ids
        for shingle in shingles_by_id[record_id]
    )
    # 全局频率升序、分片哈希升序的确定性排名。高频模板分片排名靠后，前缀过滤
    # 自然将其排除在候选索引之外；排名不依赖遍历顺序，随机化输入不改变结果。
    # 频率 1 的分片不可能被任何 pair 共享，不进入索引。
    rank_by_shingle = {
        shingle: rank
        for rank, shingle in enumerate(
            sorted(
                (shingle for shingle, count in frequencies.items() if count >= 2),
                key=lambda item: (frequencies[item], item),
            )
        )
    }
    del frequencies
    metrics["index_distinct_shingles"] = len(rank_by_shingle)

    prefix_size_by_id: dict[str, int] = {}
    prefix_posting_total = 0
    for record_id in record_ids:
        size = len(
            _prefix_tokens(shingles_by_id[record_id], rank_by_shingle, threshold)
        )
        prefix_size_by_id[record_id] = size
        prefix_posting_total += size
    metrics["index_postings"] = prefix_posting_total

    findings: list[Finding] = []
    matched: list[tuple[float, str, str]] = []
    spill_used = False

    def evaluate(pair: tuple[str, str]) -> None:
        left, right = pair
        if content_by_id[left] == content_by_id[right]:
            # 完全相同内容已由精确重复检查负责。
            return
        metrics["candidate_pairs_checked"] += 1
        left_shingles = shingles_by_id[left]
        right_shingles = shingles_by_id[right]
        union = left_shingles | right_shingles
        if not union:
            return
        intersection = left_shingles & right_shingles
        similarity = len(intersection) / len(union)
        if similarity >= threshold:
            matched.append((similarity, left, right))

    resident_limit = _chunk_limit(
        "SPLITGUARD_NEAR_DUP_RESIDENT_LIMIT", MAX_RESIDENT_POSTINGS
    )
    use_spill = mode == "exact" and prefix_posting_total > resident_limit

    if use_spill:
        spill_used = True
        candidates, spill_stats = _run_external_exact(
            record_ids,
            shingles_by_id,
            content_by_id,
            split_by_id,
            rank_by_shingle,
            threshold,
        )
        metrics["peak_resident_postings"] = spill_stats["peak_resident_postings"]
        metrics["spill_chunks"] = spill_stats["spill_chunks"]
        metrics["pair_spill_chunks"] = spill_stats["pair_spill_chunks"]
        metrics["index_postings"] = spill_stats["index_postings"]
        metrics["candidate_pairs_generated"] = len(candidates)
        for pair in candidates:
            evaluate(pair)
    else:
        budget = options.max_candidates if mode == "bounded" else None
        index = _StreamingPrefixIndex(
            rank_by_shingle,
            split_by_id,
            content_by_id,
            threshold,
            budget=budget,
        )
        for record_id in record_ids:
            for pair in index.add_record(record_id, shingles_by_id[record_id]):
                evaluate(pair)
            if index.budget_hit:
                break
        metrics["candidate_pairs_generated"] = index.candidate_pairs_generated
        metrics["peak_resident_postings"] = sum(
            len(bucket) for bucket in index.postings.values()
        )
        if index.budget_hit:
            metrics["candidate_limit_hit"] = True
            metrics["candidate_budget_hit"] = True
            metrics["completeness"] = "incomplete"
            metrics["stopped_at_record"] = index.stopped_at_record
            metrics["records_unchecked"] = len(record_ids) - index.records_processed
            covered_ranks = set(index.postings)
            metrics["shingle_groups_unchecked"] = (
                len(rank_by_shingle) - len(covered_ranks)
            )
            findings.append(
                _limit_finding(
                    options,
                    {
                        "candidate_mode": mode,
                        "completeness": "incomplete",
                        "max_candidates": options.max_candidates,
                        "candidate_pairs_generated": index.candidate_pairs_generated,
                        "candidate_pairs_checked": metrics[
                            "candidate_pairs_checked"
                        ],
                        "truncated": True,
                        "stopped_at_record": index.stopped_at_record,
                        "records_processed": index.records_processed,
                        "records_unchecked": metrics["records_unchecked"],
                        "shingle_groups_unchecked": metrics[
                            "shingle_groups_unchecked"
                        ],
                        "peak_resident_postings": metrics[
                            "peak_resident_postings"
                        ],
                        "budget": {
                            "candidate_budget": options.max_candidates,
                            "candidate_budget_hit": True,
                            "resident_posting_limit": resident_limit,
                            "resident_postings_peak": metrics[
                                "peak_resident_postings"
                            ],
                        },
                    },
                )
            )

    metrics["spill_used"] = spill_used
    metrics["matches"] = len(matched)

    matched.sort(key=lambda item: (-round(item[0], 6), item[1], item[2]))
    for similarity, left, right in matched:
        splits = sorted((split_by_id[left], split_by_id[right]))
        shared = len(shingles_by_id[left] & shingles_by_id[right])
        union = len(shingles_by_id[left] | shingles_by_id[right])
        record_id_pair = sorted((left, right))
        findings.append(
            Finding(
                code=_MATCH_CODE,
                severity="block",
                message=(
                    f"近似重复 {left} / {right} 的 Jaccard={similarity:.6f}，"
                    f"跨切分 {splits[0]}, {splits[1]}"
                ),
                split=None,
                record_ids=record_id_pair,
                evidence={
                    "candidate_mode": mode,
                    "completeness": metrics["completeness"],
                    "similarity": round(similarity, 6),
                    "record_ids": record_id_pair,
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
                    "threshold": threshold,
                    "field_names": list(config.text_columns),
                },
            )
        )
    return CheckResult(findings, metrics)
