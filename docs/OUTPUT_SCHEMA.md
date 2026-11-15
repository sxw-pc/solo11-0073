# SplitGuard 输出契约

## 1. `report.json`

顶层为 JSON 对象，写入时使用 `sort_keys=True`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | string | 当前 `1.0` |
| `run_id` | string | `run-` + 16 位十六进制确定性 ID |
| `tool` | object | `{"name":"splitguard","version":"1.0.0"}` |
| `started_at` | string | UTC ISO 8601，运行时间字段 |
| `generated_at` | string | UTC ISO 8601，运行时间字段 |
| `finished_at` | string | UTC ISO 8601，运行时间字段 |
| `inputs` | object | 数据集、清单、配置的路径、SHA-256 和计数 |
| `config` | object | 校验后的规范化配置 |
| `conclusion` | string | `block` / `review` / `pass` |
| `summary` | object | 结论、发现计数、总数、切分计数 |
| `metrics` | object | 所有结构化指标 |
| `findings` | array | 稳定排序的 finding 对象 |
| `reproducibility` | object | 核心哈希、canonical 规则、排除的运行时间字段 |

`inputs`：

```json
{
  "dataset": {"path": "fixtures/leaky/dataset.csv", "sha256": "...", "record_count": 18},
  "splits": {"path": "fixtures/leaky/splits.csv", "sha256": "...", "entry_count": 19},
  "config": {"path": "fixtures/leaky/config.json", "sha256": "...", "canonical_sha256": "..."}
}
```

`summary`：

```json
{
  "conclusion": "block",
  "finding_counts": {"block": 9, "warn": 7, "info": 1},
  "findings_total": 17,
  "total_records": 18,
  "assigned_records": 15,
  "split_counts": {"train": 7, "valid": 3, "test": 5}
}
```

## 2. Finding 对象

```json
{
  "code": "EXACT_DUPLICATE_CROSS_SPLIT",
  "severity": "block",
  "split": null,
  "record_ids": ["record-0002", "record-0013"],
  "evidence": {"content_sha256": "...", "splits": ["test", "train"]},
  "message": "内容哈希 ... 出现在 2 个切分中，涉及 2 条记录"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 稳定 finding code |
| `severity` | string | `block` / `warn` / `info` |
| `split` | string/null | 单切分问题时为目标切分；跨切分为 null |
| `record_ids` | array | 排序去重后的记录 ID |
| `evidence` | object | 结构化证据，不包含完整原始文本 |
| `message` | string | 人可读说明，不包含完整原始文本 |

### 稳定 finding code

| code | 默认级别 | 触发条件 |
| --- | --- | --- |
| `MANIFEST_EMPTY_RECORD_ID` | block | 清单中记录 ID 为空 |
| `MANIFEST_UNKNOWN_RECORD` | block | 清单 ID 不在数据集中 |
| `MANIFEST_DUPLICATE_ASSIGNMENT` | block | 同一 ID 在清单中出现多次 |
| `MANIFEST_EMPTY_SPLIT_VALUE` | block | 清单切分值为空 |
| `MANIFEST_UNKNOWN_SPLIT` | block | 切分值不在 `expected_splits` |
| `MANIFEST_MISSING_ASSIGNMENT` | block | 数据集记录在清单中没有归属 |
| `SPLIT_EMPTY` | block/warn | 期望切分无有效记录，按 `empty_split_severity` |
| `SPLIT_RATIO_DRIFT` | warn | 实际占比与期望占比偏差超容差 |
| `EXACT_DUPLICATE_CROSS_SPLIT` | block | 规范化内容跨切分重复 |
| `EXACT_DUPLICATE_WITHIN_SPLIT` | warn | 规范化内容仅在单切分内重复 |
| `NEAR_DUPLICATE_CROSS_SPLIT` | block | 近似重复 Jaccard 达阈值且跨切分 |
| `NEAR_DUPLICATE_CANDIDATE_LIMIT` | warn | 候选对数量达到上限，候选被截断 |
| `GROUP_LEAKAGE` | block | 同一实体组跨切分出现 |
| `GROUP_EMPTY_VALUE` | warn | 实体组值为空 |
| `TEMPORAL_LEAKAGE` | block | 后序切分记录早于前序最大时间减允许间隔 |
| `TEMPORAL_INVALID_TIMESTAMP` | block/warn | 全局时间列无法解析，按 `invalid_time_severity` |
| `TEMPORAL_FEATURE_LEAKAGE` | block | 逐特征预测截止检查中特征在 `prediction_at` 不可用（每条 feature+record 一条） |
| `TEMPORAL_FEATURE_INVALID_TIMESTAMP` | block/warn | 特征事件/可用时间无法解析，按 `feature_time_invalid_severity`，保持未知 |
| `TEMPORAL_FEATURE_MISSING_TIMESTAMP` | block/warn | 特征事件/可用/截止时间缺失，按 `feature_time_invalid_severity`，保持未知 |
| `LABEL_MISSING_IN_SPLIT` | warn | 参考切分中的标签在目标切分缺失 |
| `SPLIT_SMALL_SAMPLE` | info | 非空切分记录数低于小样本阈值 |

finding 全局排序为严重级别、code、切分/相似度、记录 ID、canonical evidence。精确/近似重复 finding 的 `evidence.splits` 和 `record_ids` 升序；近似重复相似度降序。

## 3. `metrics`

顶层指标：

| key | 说明 |
| --- | --- |
| `total_records` | 数据集记录数 |
| `assigned_records` | 有效分派记录数 |
| `split_counts` | 每个期望切分的有效记录数 |
| `split_ratios` | 有效分派占比，分母为 `assigned_records` |
| `missing_ids_count` | 数据集 ID 在清单中零次出现的数量 |
| `unknown_ids_count` | 清单中非空、但数据集中不存在的唯一 ID 数 |
| `duplicate_assignment_count` | 清单中出现多次的唯一 ID 数 |
| `manifest_entry_count` | 清单数据行数 |
| `empty_record_id_count` | 空记录 ID 清单行数 |
| `empty_split_value_count` | 空切分值清单行数 |
| `unknown_split_values` | 未知切分值列表 |
| `expected_ratios` | 配置中提供的期望比例 |
| `ratio_deltas` | 实际减期望 |
| `ratio_tolerance` | 比例容差 |
| `exact_duplicates` | 精确重复计数与开关 |
| `near_duplicates` | ngram、阈值、候选数、上限是否触发、匹配数 |
| `group_isolation` | 组检查数、泄漏组数、空组记录数 |
| `temporal` | 开关、拆分顺序、最小间隔、非法数、匹配数、各切分摘要、逐特征预测截止摘要 |
| `composition` | 标签开关、参考切分、小样本阈值、计数、占比 |

`temporal.splits.<split>` 包含：

```json
{
  "record_count": 5,
  "parsed_records": 5,
  "invalid_records": 0,
  "min_time": "2025-01-31T23:00:00.000000Z",
  "max_time": "2025-02-01T13:00:00.000000Z",
  "violating_records": 1
}
```

`composition.counts` 为 `split -> label -> count`；`composition.ratios` 为 `split -> label -> ratio`。比例保留 6 位小数。

`temporal` 顶层保持原有字段（`enabled`、`global_enabled`、`split_order`、`min_gap_seconds`、`invalid_records`、`matches`、`splits`），并新增 `feature_prediction`：

```json
{
  "enabled": true,
  "semantics": "serving_time",
  "semantics_explicit": true,
  "prediction_at_column": "prediction_at",
  "entity_key_column": "entity_id",
  "rule_count": 2,
  "checked_records": 12,
  "unique_records_checked": 12,
  "considered_records": 14,
  "violating_records": 1,
  "unknown_records": 1,
  "not_evaluated_records": 0,
  "unverified_columns": ["click_available_at"],
  "invalid_value_severity": "block",
  "per_record_basis": true,
  "evaluations": [
    {
      "record_id": "record-0001",
      "split": "train",
      "feature": "click_count",
      "semantics": "serving_time",
      "event_time_column": "click_event_time",
      "event_time_raw": "2025-03-01T10:00:00Z",
      "event_time_utc": "2025-03-01T10:00:00.000000Z",
      "available_at_column": "click_available_at",
      "available_at_raw": "2025-03-01T18:00:00Z",
      "available_at_utc": "2025-03-01T18:00:00.000000Z",
      "compared_time_utc": "2025-03-01T18:00:00.000000Z",
      "prediction_at_column": "prediction_at",
      "prediction_at_raw": "2025-03-01T12:00:00Z",
      "prediction_at_utc": "2025-03-01T12:00:00.000000Z",
      "compliant": false,
      "violated_semantics": "serving_time",
      "entity_key": "entity-0001"
    }
  ],
  "data_bounds": {
    "prediction_at_column": "prediction_at",
    "prediction_at_min": "2025-01-01T00:00:00.000000Z",
    "prediction_at_max": "2025-01-01T23:00:00.000000Z"
  },
  "excluded_records": {
    "count": 2,
    "record_ids": ["record-0017", "record-0018"],
    "reason": "not_a_unique_valid_assignment"
  },
  "rules": [
    {
      "feature": "click_count",
      "event_time_column": "click_event_time",
      "available_at_column": "click_available_at",
      "semantics": "serving_time",
      "records_considered": 14,
      "checked_records": 12,
      "violating_records": 1,
      "invalid_records": 0,
      "missing_records": 1,
      "unverifiable_records": 1,
      "unverified_reason": "missing_or_invalid_feature_time",
      "event_time_min": "...",
      "event_time_max": "...",
      "available_at_min": "...",
      "available_at_max": "..."
    }
  ],
  "failing_features": ["click_count"]
}
```

`TEMPORAL_FEATURE_LEAKAGE.evidence.record` 的字段：`record_id`、`split`、`feature`、`semantics`/`violated_semantics`、`event_time_column`、`event_time_raw`、`event_time_utc`、`available_at_column`、`available_at_raw`、`available_at_utc`、`compared_time_utc`、`prediction_at_column`、`prediction_at_raw`、`prediction_at_utc`、`late_arrival`、`late_arrival_explanation`（晚到时为“事件时间早于截止，但线上预测时该特征尚未到达”）、`entity_key`、`line`。

## 4. SQLite 台账

`ledger.sqlite` 使用标准库 `sqlite3` 生成。表结构：

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    dataset_sha256 TEXT NOT NULL,
    splits_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    block_count INTEGER NOT NULL,
    warn_count INTEGER NOT NULL
);

CREATE TABLE findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    split TEXT,
    record_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    metric_value TEXT NOT NULL
);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
```

- `input_fingerprint` 是三个输入 SHA-256 的 canonical JSON 哈希。
- 嵌套 metrics 以点分路径扁平化，叶子值转为文本；对象/数组叶子使用 canonical JSON。
- `artifacts` 记录 `report.json`、`report.md`、`report.html`、`run_manifest.json` 的相对路径与 SHA-256；台账不记录自身哈希，避免自引用。
- 相同 `run_id` 重跑时，先删除该 run 的旧 findings/metrics/artifacts/run 行再写入，避免重复。

## 5. `run_manifest.json`

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 报告 schema 版本 |
| `run_id` | 运行 ID |
| `tool` | 工具名与版本 |
| `started_at` / `generated_at` / `finished_at` | 运行时间字段 |
| `input_fingerprints` | 数据集、清单、配置、canonical 配置 SHA-256 |
| `record_counts` | 总记录、有效分派、各切分计数 |
| `finding_counts` | 各级别发现数 |
| `conclusion` | 最终结论 |
| `report_core_sha256` | 核心报告 canonical 哈希 |
| `artifacts` | 已写报告的相对路径与 SHA-256 |

配置指纹有两种：

- `config`：配置文件原始字节 SHA-256，反映文件内容；
- `config_canonical` / `config_canonical_sha256`：规范化配置对象按排序键 JSON 序列化后的 SHA-256，反映语义配置。

## 6. 指纹计算

```text
canonical_json(x) = JSON(x, sort_keys=true, ensure_ascii=false,
                        separators=(",", ":"), allow_nan=false)

sha256_text(s) = SHA-256(UTF-8(s))
sha256_file(p) = SHA-256(raw bytes of p)

run_id = "run-" + sha256_text(canonical_json({
  "tool": {"name":"splitguard", "version":"1.0.0"},
  "schema_version":"1.0",
  "input_sha256": {"dataset":..., "splits":..., "config":...},
  "config": <normalized config>
}))[:16]

report_core_sha256 = sha256_text(canonical_json({
  "run_id":...,
  "tool":...,
  "schema_version":...,
  "input_sha256":...,
  "config":...,
  "summary":...,
  "metrics":...,
  "findings":...
}))
```

`report_core_sha256` 不包含 `started_at`、`generated_at`、`finished_at`、输入/输出路径和 artifact 哈希。输入路径仅出现在报告元数据中，不进入运行 ID 或核心哈希。

## 7. 报告文件与 HTML

- `report.md` 包含结论、摘要表、切分比例与偏差表、finding 表和复现指纹。
- `report.html` 是单文件 HTML，内联 `<style>`，不含 `<link>`、`<script src>`、CDN、字体或外链资源。
- 二者均由排序后的报告数据生成，不包含完整原始文本。
