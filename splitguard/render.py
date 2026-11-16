"""Self-contained Markdown and HTML report rendering."""

from __future__ import annotations

import html
import json
from typing import Any

_SEVERITY_LABEL = {"block": "阻断", "warn": "警告", "info": "提示"}
_CONCLUSION_LABEL = {"block": "阻断", "review": "待复核", "pass": "通过"}
_COMPLETENESS_LABEL = {
    "complete": "完整",
    "incomplete": "不完整",
    "not_run": "未运行",
}


def _completeness_text(value: Any) -> str:
    token = str(value)
    label = _COMPLETENESS_LABEL.get(token, token)
    return f"{label} ({token})"


def _cell(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _record_summary(finding: dict[str, Any]) -> str:
    ids = finding.get("record_ids", [])
    if not ids:
        return "-"
    shown = ", ".join(str(item) for item in ids[:6])
    if len(ids) > 6:
        shown += f" …(+{len(ids) - 6})"
    return shown


def _ratio_rows(report: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    metrics = report.get("metrics", {})
    counts = metrics.get("split_counts", {})
    actual = metrics.get("split_ratios", {})
    expected = metrics.get("expected_ratios", {})
    deltas = metrics.get("ratio_deltas", {})
    rows: list[tuple[str, str, str, str, str]] = []
    for split in report.get("config", {}).get("expected_splits", []):
        rows.append(
            (
                str(split),
                str(counts.get(split, 0)),
                str(actual.get(split, 0.0)),
                str(expected.get(split, "-")),
                str(deltas.get(split, "-")),
            )
        )
    return rows


def _findings_table(findings: list[dict[str, Any]]) -> list[str]:
    if not findings:
        return ["未发现任何问题。", ""]
    lines = [
        "| 严重级别 | finding code | 切分 | 记录数 | 记录 ID 摘要 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for finding in findings:
        lines.append(
            "| {severity} | {code} | {split} | {count} | {records} | {message} |".format(
                severity=_SEVERITY_LABEL.get(
                    finding.get("severity"), finding.get("severity", "")
                ),
                code=_cell(finding.get("code")),
                split=_cell(finding.get("split") or "-"),
                count=len(finding.get("record_ids", [])),
                records=_cell(_record_summary(finding)),
                message=_cell(finding.get("message")),
            )
        )
    lines.append("")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    counts = summary.get("finding_counts", {})
    conclusion = report.get("conclusion", "")
    near_duplicate = summary.get("near_duplicate", {})
    lines = [
        "# 切分防漏台审计报告",
        "",
        f"- 运行 ID：`{report.get('run_id', '')}`",
        f"- 工具版本：`{report.get('tool', {}).get('version', '')}`",
        f"- 生成时间：`{report.get('generated_at', '')}`",
        f"- 最终结论：**{_CONCLUSION_LABEL.get(conclusion, conclusion)}** (`{conclusion}`)",
        f"- 近似重复候选策略：`{near_duplicate.get('candidate_mode', '-')}`",
        f"- 近似重复结论完整度：**{_completeness_text(near_duplicate.get('completeness', '-'))}**",
        "",
        "## 摘要",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 总记录数 | {summary.get('total_records', 0)} |",
        f"| 有效分派记录数 | {summary.get('assigned_records', 0)} |",
        f"| 阻断发现 | {counts.get('block', 0)} |",
        f"| 警告发现 | {counts.get('warn', 0)} |",
        f"| 提示发现 | {counts.get('info', 0)} |",
        f"| 近似重复候选策略 | {near_duplicate.get('candidate_mode', '-')} |",
        f"| 近似重复结论完整度 | {_completeness_text(near_duplicate.get('completeness', '-'))} |",
        "",
        "## 切分比例与偏差",
        "",
        "| 切分 | 记录数 | 实际占比 | 期望占比 | 偏差 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in _ratio_rows(report):
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines.extend(
        [
            "",
            f"比例容差：`{report.get('metrics', {}).get('ratio_tolerance', '-')}`。",
            "",
            "## 发现",
            "",
            *_findings_table(report.get("findings", [])),
            "## 可复现性",
            "",
            f"- 核心报告指纹：`{report.get('reproducibility', {}).get('report_core_sha256', '')}`",
            f"- 数据集 SHA-256：`{report.get('inputs', {}).get('dataset', {}).get('sha256', '')}`",
            f"- 切分清单 SHA-256：`{report.get('inputs', {}).get('splits', {}).get('sha256', '')}`",
            f"- 配置 SHA-256：`{report.get('inputs', {}).get('config', {}).get('sha256', '')}`",
            "",
            "报告不包含原始文本，仅包含哈希、脱敏摘要和结构化证据。",
            "",
        ]
    )
    return "\n".join(lines)


def _html_finding(finding: dict[str, Any]) -> str:
    severity = html.escape(str(finding.get("severity", "")))
    label = _SEVERITY_LABEL.get(str(finding.get("severity")), severity)
    record_ids = finding.get("record_ids", [])
    evidence = html.escape(
        json.dumps(finding.get("evidence", {}), ensure_ascii=False, indent=2, sort_keys=True)
    )
    code = html.escape(str(finding.get("code", "")))
    message = html.escape(str(finding.get("message", "")))
    split = html.escape(str(finding.get("split") or "-"))
    return f"""
    <article class="finding">
      <h3><span class="sev {severity}">{html.escape(label)}</span>
        <code>{code}</code></h3>
      <p>{message}</p>
      <p class="meta">split: {split} · records: {len(record_ids)}</p>
      <details><summary>结构化证据</summary><pre>{evidence}</pre></details>
    </article>"""


def render_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    counts = summary.get("finding_counts", {})
    conclusion = str(report.get("conclusion", ""))
    ratio_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_cell(value))}</td>" for value in row) + "</tr>"
        for row in _ratio_rows(report)
    )
    findings = "\n".join(_html_finding(item) for item in report.get("findings", []))
    if not findings:
        findings = "<p>未发现任何问题。</p>"
    label_summary = report.get("metrics", {}).get("composition", {}).get("counts", {})
    label_html = html.escape(
        json.dumps(label_summary, ensure_ascii=False, indent=2, sort_keys=True)
    )
    run_id = html.escape(str(report.get("run_id", "")))
    tool_version = html.escape(str(report.get("tool", {}).get("version", "")))
    generated_at = html.escape(str(report.get("generated_at", "")))
    conclusion_html = html.escape(conclusion)
    conclusion_text = html.escape(_CONCLUSION_LABEL.get(conclusion, conclusion))
    near_duplicate = summary.get("near_duplicate", {})
    near_mode = html.escape(str(near_duplicate.get("candidate_mode", "-")))
    near_completeness = html.escape(
        _completeness_text(near_duplicate.get("completeness", "-"))
    )
    total_records = summary.get("total_records", 0)
    assigned_records = summary.get("assigned_records", 0)
    block_count = counts.get("block", 0)
    warn_count = counts.get("warn", 0)
    info_count = counts.get("info", 0)
    reproducibility = report.get("reproducibility", {})
    inputs = report.get("inputs", {})
    core_sha = html.escape(str(reproducibility.get("report_core_sha256", "")))
    dataset_sha = html.escape(str(inputs.get("dataset", {}).get("sha256", "")))
    splits_sha = html.escape(str(inputs.get("splits", {}).get("sha256", "")))
    config_sha = html.escape(str(inputs.get("config", {}).get("sha256", "")))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>切分防漏台审计报告 {run_id}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1100px; margin: 0 auto; padding: 28px; line-height: 1.5;
       color: #1d2433; background: #f6f7f9; }}
h1, h2, h3 {{ color: #12203a; }}
.card {{ background: #fff; border: 1px solid #dce1e8; border-radius: 10px;
        padding: 18px 22px; margin: 18px 0; }}
.badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px;
         font-weight: 700; color: #fff; background: #64748b; }}
.badge.block, .sev.block {{ background: #b42318; }}
.badge.review, .sev.warn {{ background: #b54708; }}
.badge.pass {{ background: #067647; }}
.sev.info {{ background: #175cd3; }}
.sev {{ display: inline-block; color: #fff; border-radius: 6px;
       padding: 1px 7px; font-size: 12px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #e4e7ec; padding: 8px; text-align: left;
         vertical-align: top; }}
code {{ background: #eef2f6; border-radius: 4px; padding: 1px 5px; }}
.finding {{ border-top: 1px dashed #d0d5dd; padding-top: 10px; margin-top: 12px; }}
.meta {{ color: #475467; font-size: 13px; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f2f4f7;
      padding: 12px; border-radius: 8px; font-size: 12px; }}
</style>
</head>
<body>
<h1>切分防漏台审计报告</h1>
<div class="card">
<p>运行 ID：<code>{run_id}</code></p>
<p>最终结论：<span class="badge {conclusion_html}">{conclusion_text}</span>
   (<code>{conclusion_html}</code>)</p>
<p>近似重复候选策略：<code>{near_mode}</code> · 结论完整度：{near_completeness}</p>
<p>工具版本：{tool_version} · 生成时间：{generated_at}</p>
<table>
<tr><th>总记录数</th><th>有效分派</th><th>阻断</th><th>警告</th><th>提示</th></tr>
<tr><td>{total_records}</td><td>{assigned_records}</td>
<td>{block_count}</td><td>{warn_count}</td><td>{info_count}</td></tr>
</table>
</div>
<div class="card">
<h2>切分比例与偏差</h2>
<table>
<tr><th>切分</th><th>记录数</th><th>实际占比</th><th>期望占比</th><th>偏差</th></tr>
{ratio_rows}
</table>
</div>
<div class="card">
<h2>发现</h2>
{findings}
</div>
<div class="card">
<h2>标签计数</h2>
<pre>{label_html}</pre>
</div>
<div class="card">
<h2>可复现性</h2>
<p>核心报告指纹：<code>{core_sha}</code></p>
<p>数据集 SHA-256：<code>{dataset_sha}</code></p>
<p>切分清单 SHA-256：<code>{splits_sha}</code></p>
<p>配置 SHA-256：<code>{config_sha}</code></p>
<p class="meta">页面由本地生成，样式内联，不加载外部资源。</p>
</div>
</body>
</html>
"""
