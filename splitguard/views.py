"""Inline-CSS HTML views for the read-only local review server."""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

_SEVERITY = {"block": "阻断", "warn": "警告", "info": "提示"}
_CONCLUSION = {"block": "阻断", "review": "待复核", "pass": "通过"}

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1120px; margin: 0 auto; padding: 24px; line-height: 1.5;
       color: #1d2433; background: #f5f7fa; }
h1, h2, h3 { color: #12203a; }
a { color: #175cd3; }
.card { background: #fff; border: 1px solid #dce1e8; border-radius: 10px;
        padding: 16px 20px; margin: 16px 0; }
.badge { display: inline-block; border-radius: 999px; padding: 2px 9px;
         color: #fff; font-weight: 700; font-size: 13px; }
.badge.block, .sev.block { background: #b42318; }
.badge.review, .sev.warn { background: #b54708; }
.badge.pass { background: #067647; }
.sev.info { background: #175cd3; }
.sev { display: inline-block; color: #fff; border-radius: 5px;
       padding: 1px 6px; font-size: 12px; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid #e4e7ec; padding: 8px; text-align: left;
         vertical-align: top; }
code { background: #eef2f6; border-radius: 4px; padding: 1px 5px; }
.meta { color: #475467; font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f2f4f7;
      padding: 12px; border-radius: 8px; font-size: 12px; }
"""


def page(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{safe_title}</h1>
{body}
<p class="meta">SplitGuard 本地只读复核台 · 数据仅来自本机工作目录</p>
</body>
</html>
"""


def error_page(status: int, message: str) -> str:
    return page(f"{status} · 请求失败", f"<div class='card'><p>{html.escape(message)}</p></div>")


def render_index(reports: list[dict[str, Any]]) -> str:
    if not reports:
        body = "<div class='card'><p>尚无审计运行。先运行 demo 或 audit。</p></div>"
        return page("切分防漏台 · 运行列表", body)
    rows = []
    for report in reports:
        run_id = str(report.get("run_id", ""))
        escaped_id = html.escape(run_id, quote=True)
        href = "/run/" + quote(run_id, safe="")
        conclusion = str(report.get("conclusion", ""))
        counts = report.get("summary", {}).get("finding_counts", {})
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(href, quote=True)}'>{escaped_id}</a></td>"
            f"<td><span class='badge {html.escape(conclusion)}'>"
            f"{html.escape(_CONCLUSION.get(conclusion, conclusion))}</span></td>"
            f"<td>{report.get('summary', {}).get('total_records', 0)}</td>"
            f"<td>{counts.get('block', 0)}</td>"
            f"<td>{counts.get('warn', 0)}</td>"
            f"<td>{html.escape(str(report.get('generated_at', '')))}</td>"
            "</tr>"
        )
    body = f"""
<div class="card">
<p>仅监听 loopback，只读展示。点击运行 ID 查看详情。</p>
<table>
<tr><th>运行 ID</th><th>结论</th><th>总记录</th><th>阻断</th><th>警告</th><th>生成时间</th></tr>
{''.join(rows)}
</table>
</div>"""
    return page("切分防漏台 · 运行列表", body)


def _artifact_links(report: dict[str, Any]) -> str:
    run_id = str(report.get("run_id", ""))
    prefix = "/artifact/" + quote(run_id, safe="") + "/"
    names = ("report.md", "report.html", "report.json", "run_manifest.json", "ledger.sqlite")
    links = ", ".join(
        f"<a href='{prefix}{name}'>{name}</a>" for name in names
    )
    return f"<div class='card'><h2>报告下载</h2><p>{links}</p></div>"


def _findings_html(report: dict[str, Any]) -> str:
    findings = report.get("findings", [])
    if not findings:
        return "<p>未发现任何问题。</p>"
    rows = []
    for finding in findings:
        severity = str(finding.get("severity", ""))
        evidence = html.escape(
            json.dumps(finding.get("evidence", {}), ensure_ascii=False, indent=2, sort_keys=True)
        )
        rows.append(
            "<tr>"
            f"<td><span class='sev {html.escape(severity)}'>"
            f"{html.escape(_SEVERITY.get(severity, severity))}</span></td>"
            f"<td><code>{html.escape(str(finding.get('code', '')))}</code></td>"
            f"<td>{html.escape(str(finding.get('split') or '-'))}</td>"
            f"<td>{len(finding.get('record_ids', []))}</td>"
            f"<td>{html.escape(str(finding.get('message', '')))}"
            f"<details><summary>证据</summary><pre>{evidence}</pre></details></td>"
            "</tr>"
        )
    return (
        "<table><tr><th>级别</th><th>code</th><th>切分</th><th>记录数</th>"
        "<th>说明</th></tr>" + "".join(rows) + "</table>"
    )


def render_run(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    counts = summary.get("finding_counts", {})
    conclusion = str(report.get("conclusion", ""))
    metrics = report.get("metrics", {})
    split_counts = summary.get("split_counts", {})
    split_text = " · ".join(f"{key}: {value}" for key, value in split_counts.items())
    labels = metrics.get("composition", {}).get("counts", {})
    labels_text = html.escape(json.dumps(labels, ensure_ascii=False, sort_keys=True))
    run_id = html.escape(str(report.get("run_id", "")))
    conclusion_html = html.escape(conclusion)
    conclusion_label = html.escape(_CONCLUSION.get(conclusion, conclusion))
    body = f"""
<div class="card">
<p>运行 ID：<code>{run_id}</code></p>
<p>结论：<span class="badge {conclusion_html}">{conclusion_label}</span>
   · 总记录 {summary.get('total_records', 0)}
   · 有效分派 {summary.get('assigned_records', 0)}
   · 阻断 {counts.get('block', 0)}
   · 警告 {counts.get('warn', 0)}</p>
<p class="meta">切分计数：{html.escape(split_text or '-')}<br>
生成时间：{html.escape(str(report.get('generated_at', '')))}</p>
</div>
<div class="card"><h2>发现列表</h2>{_findings_html(report)}</div>
<div class="card"><h2>指标摘要</h2>
<p>实际占比：<code>{html.escape(json.dumps(metrics.get('split_ratios', {}), sort_keys=True))}</code></p>
<p>期望占比：<code>{html.escape(json.dumps(metrics.get('expected_ratios', {}), sort_keys=True))}</code></p>
<p>标签计数：<code>{labels_text}</code></p>
</div>
{_artifact_links(report)}
"""
    return page("运行详情 · " + str(report.get("run_id", "")), body)
