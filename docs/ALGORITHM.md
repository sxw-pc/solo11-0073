# SplitGuard 算法说明

本文描述审计链路、确定性规则和严重级别。所有检查只读取本地 CSV/JSON，不执行输入中的代码。

## 1. 装载与有效分派

1. 配置经 JSON 解析和字段校验。非法值返回稳定错误码，并带文件、字段（JSON 解析错误还带行号）。
2. 数据集 CSV 使用 UTF-8-SIG 读取。拒绝空文件、空表头、空表头字段、重复表头、缺失必需列、非 UTF-8 数据和字段数不一致的行。
3. 记录 ID 去除首尾空白后必须非空且全局唯一。
4. 切分清单只允许 `record_id` 和 `split`（或配置中的 `split_column`）两列。清单行允许空 ID、空切分和未知切分，由后续检查生成结构化发现。
5. 一笔数据集的“有效分派”必须同时满足：
   - ID 存在于数据集中；
   - 清单中该 ID 恰好出现一次；
   - 切分值非空且属于 `expected_splits`。

比例、精确重复、近似重复、实体组、时间和标签检查都只使用有效分派，避免在归属不明的记录上产生误导性结论。结构异常本身会产生阻断或警告。

## 2. 文本规范化

对每个配置文本列依次执行：

1. Unicode NFKC 归一化；
2. Unicode casefold（不是简单 lower）；
3. 连续空白折叠为单个 ASCII 空格；
4. 去除首尾空白。

多个文本列按配置顺序，用固定分隔符 U+001F（Unit Separator）拼接。每个字段先独立规范化，再拼接。拼接结果全部为空时，该记录不参与重复检测。

报告不输出规范化文本或原文，只输出内容 SHA-256、字段名、记录 ID、切分、脱敏长度/词数摘要。

## 3. 精确重复

对规范化拼接内容计算 UTF-8 字节的 SHA-256：

```text
content_sha256 = SHA256(UTF-8(normalized_field_1 + US + normalized_field_2 + ...))
```

相同 `content_sha256` 的记录聚合为一组：

- 组内记录来自至少两个切分：`EXACT_DUPLICATE_CROSS_SPLIT`，`block`；
- 组内记录只来自一个切分：`EXACT_DUPLICATE_WITHIN_SPLIT`，`warn`；
- 记录文本为空：跳过，不把空内容视为重复。

一个内容组只产生一条 finding。证据包含哈希、记录 ID、切分、各切分计数、字段名和脱敏摘要。

## 4. 近似重复

### 4.1 shingle

对规范化拼接文本取字符 n-gram（默认 `ngram=5`）：

```text
shingle_i = text[i : i + ngram]
shingle_hash_i = SHA256(UTF-8(shingle_i))
```

若文本长度小于等于 `ngram`，整个文本作为一个 shingle。使用 SHA-256 而不是 Python 随机哈希，保证跨进程稳定。

### 4.2 倒排桶和候选

为每条记录构建 shingle 哈希集合，然后建立 `shingle_hash -> [record_id...]` 倒排桶。桶内跨切分记录组成候选对：

- 只生成分属不同切分的记录对；
- 同一无序记录对只保留一次；
- 为确定性选择，按 shingle 哈希升序、记录 ID 升序遍历桶；
- 候选集合达到 `max_candidates` 后停止，并产生 `NEAR_DUPLICATE_CANDIDATE_LIMIT` 警告；
- 证据记录上限、已检查候选数和 `truncated=true`。

候选上限是针对跨切分唯一候选对的硬上限；没有上限触发时不产生该警告。

### 4.3 Jaccard 判定

对每个候选对计算规范化文本的 shingle 集合：

```text
J(A, B) = |A ∩ B| / |A ∪ B|
```

- `J >= jaccard_threshold` 且分属不同切分：`NEAR_DUPLICATE_CROSS_SPLIT`，`block`；
- 规范化内容 SHA-256 完全相同：交给精确重复检查，不重复生成近似重复 finding；
- 同一切分内的近似重复不判定为跨切分泄漏。

近似重复 finding 按相似度降序、记录 ID 升序排列。相似度只作为数值证据，不输出原文。

## 5. 实体组泄漏

当 `group_isolation=true` 且配置了 `group_column`：

- 每个实体组收集其有效分派记录及切分；
- 组 ID 去除首尾空白后为空的记录，产生聚合 `GROUP_EMPTY_VALUE`，级别 `warn`；
- 同一组出现在两个或更多切分：每个组一条 `GROUP_LEAKAGE`，级别 `block`；
- 证据包含组 ID、涉及切分、总记录数、各切分计数和前 10 个代表记录 ID。

组 ID 使用精确字符串比较，不做模糊匹配。

## 6. 时间穿越

解析 `time_column`：

- 支持 ISO 8601 日期/日期时间；
- 尾部 `Z`/`z` 转为 `+00:00`；
- 无时区时间按 UTC；
- 带偏移时间转换为 UTC；
- 失败或空值聚合为 `TEMPORAL_INVALID_TIMESTAMP`，级别由 `invalid_time_severity` 决定（默认 `block`）。

对 `temporal.split_order` 中的每个切分按顺序累计：

```text
prior_max = 所有更早顺序切分中已成功解析时间的最大值
threshold = prior_max - timedelta(seconds=min_gap_seconds)
violating = 当前切分中 timestamp < threshold 的记录
```

当前切分有越界记录时产生一条 `TEMPORAL_LEAKAGE`，级别 `block`。证据包含：

- 当前切分、前序切分列表、前序最大时间、允许间隔、阈值；
- 越界记录数、最多 20 条代表记录及 UTC 时间；
- 当前切分最小/最大 UTC 时间。

每个切分的最小时间、最大时间、记录数、解析成功数和越界数同时写入 temporal metrics。相等时间不视为越界。

### 6.1 逐特征预测截止检查（独立于 6 的全局顺序检查）

全局 split 顺序检查只回答“切分之间的事件时间是否单调”。它不能回答“模型训练时是否用到了预测当时尚不可获得的信息”：一个特征所描述的事件可能早已发生，但该特征本身直到预测之后才入库（晚到数据）。为此新增逐特征规则，并且**不替换**第 6 节的全局检查，两者同时运行。

配置：

```json
{
  "temporal": {
    "enabled": true,
    "semantics": "serving_time",
    "prediction_at_column": "prediction_at",
    "entity_key_column": "entity_id",
    "feature_time_invalid_severity": "block",
    "feature_time_rules": [
      {"feature": "click_count", "event_time_column": "click_event_time", "available_at_column": "click_available_at"},
      {"feature": "page_views", "event_time_column": "views_event_time"}
    ]
  }
}
```

- `feature_time_rules` 存在时必须显式声明 `semantics`，避免默认口径被静默套用；`prediction_at_column` 必须配置，且每个特征时间列（以及可用时间列）必须是数据集列。
- `serving_time` 下每条规则的 `available_at_column` 必填；`event_time` 下可选，配置了也不会参与判定。
- 每条规则的 `feature` 名唯一；`available_at_column` 不得与 `event_time_column` 相同。
- 逐特征检查只读取“唯一、有效分派”的记录；未知 split、重复分派、清单缺失的记录不参与，并计入 `excluded_records`。全局顺序检查仍按第 6 节各自运行。

两种语义：

| 语义 | 判定 |
| --- | --- |
| `event_time` | `event_time <= prediction_at`，只问特征描述的事件是否已发生，适合离线回顾 |
| `serving_time` | `available_at <= prediction_at`，还要求特征在预测时已可用；晚到数据即使 `event_time` 更早也阻断 |

对每条有效记录、每条规则、每种类别输出：

- `TEMPORAL_FEATURE_LEAKAGE`（`block`）：每个 (feature, record) 一条 finding，证据包含 `record_id`、`split`、实体键、`event_time`/`available_at`/`prediction_at` 的原始值与 UTC 归一值、`compared_time_utc`、`violated_semantics`、`late_arrival` 及其解释。相等（`<=`）视为安全，超过截止一秒即违规。
- `TEMPORAL_FEATURE_INVALID_TIMESTAMP` / `TEMPORAL_FEATURE_MISSING_TIMESTAMP`：非法/缺失时间值，级别取 `feature_time_invalid_severity`，证据标 `unknown_verification: true`。这些记录保持“未知”，绝不当作早于截止的安全值。`event_time` 语义下只有事件时间列导致未知；`serving_time` 语义下可用时间列缺失/非法也导致未知。

metrics 的 `temporal.feature_prediction` 记录所选语义及是否显式、截止与实体键列、每条规则的已核验/违规/未知数、事件与可用时间的 min/max 数据边界、`unverified_columns` 和未参与记录。Markdown、HTML 和只读复核台都会展示“所选语义、未能核验的列、数据边界”，避免只看到结论看不到口径。

## 7. 比例、标签和组成

### 7.1 比例

有效分派集合中：

```text
actual_ratio[split] = split_count[split] / assigned_records
delta[split] = actual_ratio[split] - expected_ratio[split]
```

若 `|delta| > ratio_tolerance`，产生 `SPLIT_RATIO_DRIFT`（`warn`）。差额同时写入 metrics，Markdown 报告给出实际/期望/偏差表。

期望切分有效记录数为零时产生 `SPLIT_EMPTY`，级别取 `empty_split_severity`（默认 `block`，也可固定为 `warn`）。

### 7.2 标签

当配置 `label_column`：

- 统计每个切分内各标签计数和占该切分有效记录数的比例；
- 以 `label_reference_split`（默认 `train`）为参考；
- 参考切分中存在、但在某个其他切分中计数为 0 的标签，产生该目标切分的 `LABEL_MISSING_IN_SPLIT`（`warn`）；
- 证据列出缺失标签和两边的标签计数，标签名排序稳定。

标签值使用原始字符串精确计数，空值计入空字符串键。

### 7.3 小样本

`0 < split_count < small_split_threshold` 时产生 `SPLIT_SMALL_SAMPLE`（`info`）。空切分不重复生成小样本提示，由空切分检查负责。

## 8. 严重级别与结论

| 级别 | 含义 |
| --- | --- |
| `block` | 阻断级泄漏或结构错误；默认 CLI 以退出码 2 结束 |
| `warn` | 需要人工复核，但不触发默认阻断退出码 |
| `info` | 背景提示 |

最终结论：

- 任一 `block` -> `block`
- 无 `block` 且有 `warn` -> `review`
- 否则 -> `pass`

`info` 不影响结论。

finding 全局排序键为：

1. 严重级别（block、warn、info）；
2. finding code；
3. 近似重复按相似度降序，其余按切分名；
4. 记录 ID 元组；
5. 结构化证据的 canonical JSON。

## 9. 可复现性

- 数据集、切分清单和配置文件分别计算原始字节 SHA-256。
- 规范化配置计算 canonical JSON SHA-256。
- 运行 ID 由工具版本、schema 版本、三个输入 SHA-256 和规范化配置共同派生；相同输入得到相同 ID。
- 核心报告哈希覆盖：运行 ID、工具标识、输入指纹、配置、summary、metrics、排序后的 findings。
- 核心报告哈希排除 `started_at`、`generated_at`、`finished_at`、输出路径和 artifact 哈希。
- JSON 使用 `sort_keys=True`、UTF-8、固定分隔符序列化；集合在输出前排序。
- 因此，除运行时间字段及受其影响的 artifact 文件哈希外，相同输入/配置/版本得到相同指标、排序、结论和指纹。
