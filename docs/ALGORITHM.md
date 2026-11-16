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

### 4.2 候选策略（`near_duplicate.candidate_mode`）

近似重复检查为每条记录构建 shingle 哈希集合，候选对的生成方式由
`near_duplicate.candidate_mode` 决定，报告在 `metrics.near_duplicates.candidate_mode`
与 `summary.near_duplicate` 中绑定所用策略及结论完整度（`completeness`）。

#### exact（默认）：完整性优先

目标：证明没有遗漏任何满足 Jaccard 阈值的跨切分对，绝不把"候选上限达到"
伪装成无命中。`max_candidates` 在该模式下不生效（报告中
`budget.enforced=false`）。

1. 统计全局 shingle 频率，按（频率升序、哈希升序）构造**前缀倒排索引**：
   每条记录只索引其 `size - ceil(threshold * size) + 1` 个最稀有 shingle
   （ceil 用精确整数运算，避免浮点误差缩小前缀）。由前缀过滤定理，
   任何 Jaccard 不低于阈值的记录对必共享至少一个前缀 shingle，
   因此扫描前缀桶是完备候选过滤；高频模板 shingle 排在频率序末尾，
   几乎不进入前缀，其桶保持很小。前缀索引规模不超过 shingle 总量（线性）。
2. 桶内先按规范化内容哈希分组，同内容记录对交给精确重复检查、不成对枚举；
   再按切分分组，只枚举跨切分、跨内容记录对。
3. 候选对按块（`spill_chunk_size`，默认 50000）排序后外排到临时目录
   （`temp_dir` 可配置，默认系统临时目录），多路归并去重后逐候选流式输出；
   **常驻内存的候选对不超过一个块**，完整 pair 集合不常驻内存。
   临时目录每次运行新建、退出（含异常中断）时清理，重跑绝不复用残留。
4. 对每个去重后的候选对计算精确 Jaccard（逐候选精确交集）。

代价是更多 CPU 或临时磁盘；收益是结论完整（`completeness=complete`）。

#### bounded：资源预算优先

保留确定性全倒排扫描：按 shingle 哈希升序、记录 ID 升序枚举跨切分候选对，
候选集合达到 `max_candidates` 立即停止（内存由预算严格约束）。预算耗尽时：

- 产生 `NEAR_DUPLICATE_CANDIDATE_LIMIT`（`warn`），结论至少为 `review`，
  绝不输出 clean pass；
- `metrics.near_duplicates` 记录 `budget`（上限、是否耗尽）与 `scan`
  （桶总数、已扫完整桶、部分扫描桶、未扫描桶及未扫描 shingle 出现次数），
  `completeness=incomplete`；
- finding 证据包含预算状态与未检查范围。

预算未耗尽时扫描覆盖全部候选，`completeness=complete`，可以 `pass`。

两种模式的 Jaccard 阈值、匹配判定、finding 证据与确定性排序完全一致；
预算充足时二者输出的匹配集合相同。

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
