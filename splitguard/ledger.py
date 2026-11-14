"""SQLite ledger persistence for auditable run history."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .fingerprint import canonical_json, canonical_sha256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
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
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    split TEXT,
    record_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    metric_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run_id ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_metrics_run_id ON metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
"""


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return canonical_json(value)


def flatten_metrics(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value, key=str):
                child = f"{prefix}.{key}" if prefix else str(key)
                walk(value[key], child)
            return
        rows.append((prefix, _scalar(value)))

    walk(metrics, "")
    return sorted(rows)


def write_ledger(
    out_dir: str | Path,
    report: dict[str, Any],
    artifacts: dict[str, dict[str, str]],
) -> Path:
    path = Path(out_dir) / "ledger.sqlite"
    run_id = str(report.get("run_id"))
    summary = report.get("summary", {})
    counts = summary.get("finding_counts", {})
    inputs = report.get("inputs", {})
    dataset_sha = str(inputs.get("dataset", {}).get("sha256", ""))
    splits_sha = str(inputs.get("splits", {}).get("sha256", ""))
    config_sha = str(inputs.get("config", {}).get("sha256", ""))
    input_fingerprint = canonical_sha256(
        {
            "dataset": dataset_sha,
            "splits": splits_sha,
            "config": config_sha,
        }
    )
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(_SCHEMA)
        with connection:
            for table in ("findings", "metrics", "artifacts", "runs"):
                connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, started_at, tool_version, input_fingerprint,
                    dataset_sha256, splits_sha256, config_sha256,
                    conclusion, block_count, warn_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(report.get("started_at", "")),
                    str(report.get("tool", {}).get("version", "")),
                    input_fingerprint,
                    dataset_sha,
                    splits_sha,
                    config_sha,
                    str(report.get("conclusion", "")),
                    int(counts.get("block", 0)),
                    int(counts.get("warn", 0)),
                ),
            )
            for finding in report.get("findings", []):
                connection.execute(
                    """
                    INSERT INTO findings (
                        run_id, code, severity, split, record_ids_json,
                        evidence_json, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(finding.get("code", "")),
                        str(finding.get("severity", "")),
                        finding.get("split"),
                        canonical_json(finding.get("record_ids", [])),
                        canonical_json(finding.get("evidence", {})),
                        str(finding.get("message", "")),
                    ),
                )
            for metric_key, metric_value in flatten_metrics(report.get("metrics", {})):
                connection.execute(
                    """
                    INSERT INTO metrics (run_id, metric_key, metric_value)
                    VALUES (?, ?, ?)
                    """,
                    (run_id, metric_key, metric_value),
                )
            for artifact_name in sorted(artifacts):
                artifact = artifacts[artifact_name]
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        run_id, artifact_name, relative_path, sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        artifact_name,
                        str(artifact.get("relative_path", artifact_name)),
                        str(artifact.get("sha256", "")),
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return path
