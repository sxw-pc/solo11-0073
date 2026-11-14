"""Writing deterministic report artifacts and the SQLite ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, TOOL_NAME, VERSION
from .fingerprint import atomic_write_text, sha256_file
from .ledger import write_ledger
from .models import WrittenRun
from .render import render_html, render_markdown


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def summary_lines(report: dict[str, Any], out_dir: str | Path) -> list[str]:
    summary = report.get("summary", {})
    counts = summary.get("finding_counts", {})
    split_counts = summary.get("split_counts", {})
    split_text = " ".join(
        f"{split}={count}" for split, count in split_counts.items()
    )
    return [
        f"run_id: {report.get('run_id')}",
        f"total_records: {summary.get('total_records', 0)}",
        f"split_counts: {split_text or '-'}",
        f"block: {counts.get('block', 0)}",
        f"warn: {counts.get('warn', 0)}",
        f"conclusion: {report.get('conclusion')}",
        f"output: {Path(out_dir)}",
    ]


def write_run(report: dict[str, Any], out_dir: str | Path) -> WrittenRun:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    markdown_path = output / "report.md"
    html_path = output / "report.html"
    manifest_path = output / "run_manifest.json"
    ledger_path = output / "ledger.sqlite"

    atomic_write_text(report_path, _json_text(report))
    atomic_write_text(markdown_path, render_markdown(report))
    atomic_write_text(html_path, render_html(report))

    artifacts = {
        "report.json": {
            "relative_path": "report.json",
            "sha256": sha256_file(report_path),
        },
        "report.md": {
            "relative_path": "report.md",
            "sha256": sha256_file(markdown_path),
        },
        "report.html": {
            "relative_path": "report.html",
            "sha256": sha256_file(html_path),
        },
    }
    summary = report.get("summary", {})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": report.get("run_id"),
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "started_at": report.get("started_at"),
        "generated_at": report.get("generated_at"),
        "finished_at": report.get("finished_at"),
        "input_fingerprints": {
            "dataset": report.get("inputs", {}).get("dataset", {}).get("sha256"),
            "splits": report.get("inputs", {}).get("splits", {}).get("sha256"),
            "config": report.get("inputs", {}).get("config", {}).get("sha256"),
            "config_canonical": report.get("inputs", {})
            .get("config", {})
            .get("canonical_sha256"),
        },
        "record_counts": {
            "total_records": summary.get("total_records", 0),
            "assigned_records": summary.get("assigned_records", 0),
            "split_counts": summary.get("split_counts", {}),
        },
        "finding_counts": summary.get("finding_counts", {}),
        "conclusion": report.get("conclusion"),
        "report_core_sha256": report.get("reproducibility", {}).get(
            "report_core_sha256"
        ),
        "artifacts": artifacts,
    }
    atomic_write_text(manifest_path, _json_text(manifest))
    artifacts["run_manifest.json"] = {
        "relative_path": "run_manifest.json",
        "sha256": sha256_file(manifest_path),
    }
    write_ledger(output, report, artifacts)
    return WrittenRun(
        run_id=str(report.get("run_id")),
        out_dir=output,
        report_path=report_path,
        markdown_path=markdown_path,
        html_path=html_path,
        manifest_path=manifest_path,
        ledger_path=ledger_path,
        report=report,
    )
