# 切分防漏台（SplitGuard）

**一句话定位：** 对已经声明好的 train / valid / test 切分做纯本地静态核验，回答“这份切分清单是否发生泄漏、隔离破坏、时间穿越或不可复现”。

## 适用与不适用边界

**适用：**

- 已有本地 CSV 数据集和本地 CSV 切分清单，需要在训练前做一次可归档的泄漏审计。
- 需要检查清单完整性、跨切分精确/近似重复、实体组泄漏、时间顺序泄漏、标签组成和比例偏差。
- 需要把结构化报告、可复现指纹和 SQLite 台账一起归档。
- 需要只在 `127.0.0.1` / `localhost` / `::1` 上做只读人工复核。

**不适用：**

- 不生成生产用切分，不重新抽样，不自动修复、删除或重写数据。
- 不训练模型、不评估模型指标、不搜索超参数。
- 不连接远程数据库、对象存储、消息队列或外部 API。
- 不做登录、权限、审计后台、多人协作、在线标注或标注一致性。
- 不做敏感字段发现、脱敏、隐私合规判定、数据漂移归因、A/B 实验分析、湖仓维护或血缘抽取。
- 不加载插件、不执行用户数据中的代码，不使用 `eval` / `exec` 处理配置或数据。

## 离线与合规声明

- 运行期只使用 Python 标准库，不发起任何网络请求；本地复核台只监听 loopback。
- 报告页面 CSS 全部内联，不引用 CDN、字体或外部资源。
- 所有 fixture 均为合成数据，记录 ID、实体组、标签、文本和时间均为 `record-0001`、`entity-0001`、`demo-label-a` 一类占位符。
- 审计只读取输入 CSV 和 JSON 配置，不执行其中内容。
- 报告只保存内容哈希、记录 ID、切分和脱敏摘要，不输出完整原始文本。

## 快速开始

在仓库根目录执行：

```bash
# 测试依赖（运行期不需要任何第三方库）
python -m pip install -r requirements-dev.txt

# 一条命令跑通演示：clean fixture 通过，leaky fixture 阻断
python -m splitguard demo --workdir .splitguard_work

# 一条命令跑测试
python -m pytest -q

# 一条命令启动只读本地复核台
python -m splitguard serve --workdir .splitguard_work --host 127.0.0.1 --port 8765
```

浏览器访问 `127.0.0.1:8765` 可查看最近运行、结论、发现列表、指标摘要并下载报告。复核台没有删除、修改或重跑接口，`POST` 返回 405。

## 命令与退出码

```bash
python -m splitguard --version
python -m splitguard audit \
  --dataset fixtures/clean/dataset.csv \
  --splits fixtures/clean/splits.csv \
  --config fixtures/clean/config.json \
  --out .splitguard_out
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 审计完成，且没有达到 `--fail-on` 阈值；demo 始终以 0 表示演示链路完成 |
| 1 | 输入、配置、IO 或服务参数错误，未完成审计 |
| 2 | 审计完成，但存在达到 `--fail-on` 阈值的发现 |

`audit` 默认 `--fail-on block`。可选 `warn` 或 `never`。`demo` 不因 leaky fixture 的阻断发现返回非零。

CLI 摘要会打印运行 ID、总记录数、各切分计数、阻断数、警告数、最终结论和输出目录。

## 配置字段

配置文件是 UTF-8 JSON 对象。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `dataset_id_column` | `record_id` | 数据集中的唯一记录 ID 列 |
| `text_columns` | `[]` | 精确/近似重复检测使用的文本列；按配置顺序规范化后拼接 |
| `group_column` | `null` | 实体组列，例如 `entity_id` |
| `group_isolation` | `false` | 为 true 时要求每个实体组只出现在一个切分内；此时必须有 `group_column` |
| `time_column` | `null` | 事件时间列，必须是 ISO 8601 字符串 |
| `label_column` | `null` | 标签列 |
| `split_column` | `split` | 切分清单中的切分列名；清单固定只允许 `record_id` 与该列 |
| `expected_splits` | `["train","valid","test"]` | 期望切分名称，必须唯一且非空 |
| `expected_ratios` | `{}` | 例如 `{"train":0.7,"valid":0.15,"test":0.15}`；未提供的切分跳过比例比较 |
| `ratio_tolerance` | `0.02` | 占比偏差绝对值容差 |
| `near_duplicate.enabled` | `true` | 是否启用近似重复检测 |
| `near_duplicate.ngram` | `5` | 字符 n-gram 大小 |
| `near_duplicate.jaccard_threshold` | `0.85` | Jaccard 判定阈值，范围 `(0,1]` |
| `near_duplicate.max_candidates` | `20000` | 跨切分候选对上限；超限产生 warn |
| `temporal.enabled` | `false` | 是否启用时间穿越检测；为 true 时必须有 `time_column` |
| `temporal.split_order` | `expected_splits` | 时间顺序，例如 `["train","valid","test"]` |
| `temporal.min_gap_seconds` | `0` | 后序切分允许早于前序最大时间的秒数 |
| `empty_split_severity` | `block` | 期望切分为空时的级别，只能为 `block` 或 `warn` |
| `invalid_time_severity` | `block` | 时间解析失败时的级别，只能为 `block` 或 `warn` |
| `small_split_threshold` | `5` | 非空切分小于该记录数时产生 `info`；0 表示关闭 |
| `label_reference_split` | `train` | 标签缺失比较的参考切分 |

比例计算规则：先只保留“在数据集中、在清单中恰好出现一次、且切分名合法”的有效分派，占比分母为有效分派记录数。重复分派、未知切分、空切分值记录不进入下游重复/组/时间/标签分析，并各自产生结构化发现。详见 `docs/ALGORITHM.md`。

## 输入格式

数据集 CSV 必须使用 UTF-8（支持 UTF-8-SIG），第一行为表头。下表中的 `text`、`entity_id`、`event_time`、`label` 列由配置决定是否必需。

```csv
record_id,text,entity_id,event_time,label
record-0001,Synthetic record one with unique content.,entity-0001,2025-01-01T00:00:00Z,demo-label-a
record-0002,Synthetic record two with different content.,entity-0002,2025-01-01T01:00:00Z,demo-label-b
```

切分清单 CSV 只允许两列：`record_id` 和 `split`（若配置了自定义 `split_column`，第二列使用该名称）。每条记录必须有且只能出现一次。

```csv
record_id,split
record-0001,train
record-0002,valid
```

最小配置：

```json
{
  "dataset_id_column": "record_id",
  "text_columns": ["text"],
  "group_column": "entity_id",
  "group_isolation": true,
  "time_column": "event_time",
  "label_column": "label",
  "expected_splits": ["train", "valid", "test"],
  "expected_ratios": {"train": 0.7, "valid": 0.15, "test": 0.15},
  "ratio_tolerance": 0.05,
  "near_duplicate": {"enabled": true, "ngram": 5, "jaccard_threshold": 0.85, "max_candidates": 20000},
  "temporal": {"enabled": true, "split_order": ["train", "valid", "test"], "min_gap_seconds": 0}
}
```

## 输出文件

每次审计输出目录至少包含：

| 文件 | 内容 |
| --- | --- |
| `report.json` | 完整结构化报告，键排序稳定 |
| `report.md` | 人可读摘要、比例差值表、发现表、复现指纹 |
| `report.html` | 自包含 HTML，内联 CSS，无外部资源 |
| `run_manifest.json` | 工具版本、输入 SHA-256、配置指纹、计数、发现数、结论、报告 artifact 哈希 |
| `ledger.sqlite` | `runs`、`findings`、`metrics`、`artifacts` 四类台账表 |

`report.json` 的完整字段、finding code、metrics 命名和 SQLite 结构见 `docs/OUTPUT_SCHEMA.md`。

## 检测能力

- **清单完整性**：未知 ID、缺失 ID、重复分派、空/未知切分、空切分值、期望切分为空。
- **精确重复**：对配置文本列做 NFKC、casefold、空白折叠、去首尾空白和固定分隔符拼接，再计算 SHA-256；跨切分重复阻断，同切分重复警告。
- **近似重复**：字符 n-gram shingle 的 SHA-256 倒排桶生成确定性候选对，Jaccard 达阈值且跨切分时阻断；候选超过上限时有明确警告。
- **实体组泄漏**：同一组 ID 跨切分出现时，每个组一条聚合阻断发现；空组值产生警告。
- **时间穿越**：按 `split_order` 累积比较前序最大时间与 `min_gap_seconds`，后序记录过早则阻断；非法时间可配置为阻断或警告。
- **组成**：切分比例偏差、参考切分标签在验证/测试缺失、小样本提示。
- **可复现性**：输入文件 SHA-256、规范化配置指纹、排序后的 canonical 核心报告哈希和确定性运行 ID。

## 已知限制

- 近似重复是确定性字符 n-gram 算法，不做语义相似度判断；短文本或 n-gram 大于文本长度时会退化为单个 shingle。
- 候选对上限触发后，后续桶不再检查；报告会给出截断说明和检查数量。
- 文本分析只覆盖配置的 `text_columns`，不自动发现其他字段中的重复。
- 实体组用精确字符串比较；时间比较只接受 ISO 8601，naive 时间按 UTC 解释。
- 重复分派、未知切分等结构异常记录不会参与下游泄漏分析，以避免在不可信归属上产生误导性结论。
- 工具不自动修复任何数据；阻断项需要人工复核后由上游流程处理。
- 报告只保存哈希、ID 和脱敏摘要，不保存完整文本，因此不能仅凭报告还原原文。

## 常见问题

**中文 CSV 出现乱码或表头识别失败？**\
请保存为 UTF-8 或 UTF-8-SIG。工具会去除 BOM，但不会猜测 GBK 等编码；非法 UTF-8 会返回带文件名的稳定错误码。

**时间字段支持哪些格式？**\
支持 ISO 8601 日期/日期时间、`Z`、带偏移的时间；无时区时间按 UTC 解释。带偏移时间会先转换为 UTC，报告统一输出 `Z` 格式。非法时间按 `invalid_time_severity` 处理。

**近似重复阈值怎么调？**\
阈值越高越严格，默认 `0.85`。`ngram` 越小对局部改写越敏感，候选也可能更多；`max_candidates` 控制确定性候选上限。超过上限会有 `NEAR_DUPLICATE_CANDIDATE_LIMIT` 警告。

**报告为什么不完全逐字节一致？**\
运行时间字段和由它们带来的 artifact 哈希会变化，这是预期行为。相同输入、配置和工具版本会得到相同运行 ID、指标、finding 排序、核心报告指纹和结论。

**为什么报告里看不到原文？**\
按设计只输出内容哈希、记录 ID、切分和脱敏摘要（长度/词数），避免审计报告成为原始数据副本。

**为什么库里只有部分记录进入比例和泄漏分析？**\
只有归属明确（ID 在数据集中、清单中恰好一条、切分合法）的记录可用于下游分析。结构异常会在清单检查中阻断并明确列出，不会被静默忽略。

## 项目结构

```text
splitguard/                 运行时包（仅标准库）
  checks/                   清单、精确重复、近似重复、泄漏、组成检查
  cli.py                    命令入口
  engine.py                 编排与 canonical 报告核心
  report.py / render.py     报告与台账输出
  server.py / views.py      只读 loopback 复核台
fixtures/clean/             合成通过样例
fixtures/leaky/             合成泄漏样例
tests/                      pytest 测试
docs/ALGORITHM.md           算法说明
docs/OUTPUT_SCHEMA.md       输出契约
```
