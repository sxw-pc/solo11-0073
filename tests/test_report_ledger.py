import json
import sqlite3
from pathlib import Path

import pytest

from splitguard.engine import build_audit
from splitguard.fingerprint import sha256_file
from splitguard.models import Finding, conclusion_for, sort_findings
from splitguard.report import write_run

ROOT = Path(__file__).resolve().parents[1]


def test_conclusion_rules_and_finding_order() -> None:
    block = Finding("B_CODE", "block", "block")
    warn = Finding("A_CODE", "warn", "warn")
    info = Finding("C_CODE", "info", "info")
    assert conclusion_for([]) == "pass"
    assert conclusion_for([info]) == "pass"
    assert conclusion_for([warn, info]) == "review"
    assert conclusion_for([warn, block, info]) == "block"
    assert [item.code for item in sort_findings([info, warn, block])] == [
        "B_CODE",
        "A_CODE",
        "C_CODE",
    ]


def _build(name: str, tmp_path: Path):
    base = ROOT / "fixtures" / name
    report = build_audit(base / "dataset.csv", base / "splits.csv", base / "config.json").report
    return write_run(report, tmp_path / name)


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory):
    return _build("clean", tmp_path_factory.mktemp("report"))


@pytest.fixture(scope="module")
def leaky_run(tmp_path_factory):
    return _build("leaky", tmp_path_factory.mktemp("leaky"))


def test_report_json_is_sorted_and_deterministic(clean_run, tmp_path) -> None:
    raw = clean_run.report_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert list(parsed) == sorted(parsed)
    assert raw == json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert parsed["conclusion"] == "pass"
    assert parsed["summary"]["finding_counts"] == {"block": 0, "warn": 0, "info": 0}
    second = build_audit(
        ROOT / "fixtures" / "clean" / "dataset.csv",
        ROOT / "fixtures" / "clean" / "splits.csv",
        ROOT / "fixtures" / "clean" / "config.json",
    ).report
    assert second["run_id"] == parsed["run_id"]
    assert second["reproducibility"]["report_core_sha256"] == parsed["reproducibility"]["report_core_sha256"]


def test_report_does_not_contain_raw_dataset_text(clean_run, leaky_run) -> None:
    clean_text = clean_run.report_path.read_text(encoding="utf-8")
    leaky_text = leaky_run.report_path.read_text(encoding="utf-8")
    assert "clean sample zero one" not in clean_text
    assert "synthetic retrieval passage" not in leaky_text
    assert leaky_run.report["findings"]


def test_markdown_contains_conclusion_and_ratio_table(clean_run) -> None:
    text = clean_run.markdown_path.read_text(encoding="utf-8")
    assert "# 切分防漏台审计报告" in text
    assert "最终结论" in text and "通过" in text
    assert "切分比例与偏差" in text
    assert "| train | 20 |" in text
    assert "核心报告指纹" in text


def test_html_is_self_contained(clean_run) -> None:
    text = clean_run.html_path.read_text(encoding="utf-8")
    lowered = text.lower()
    assert lowered.startswith("<!doctype html>")
    assert "<style>" in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "<link" not in lowered
    assert "<script" not in lowered
    assert "切分防漏台审计报告" in text


def test_run_manifest_contains_fingerprints_and_artifact_hashes(clean_run) -> None:
    manifest = json.loads(clean_run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == clean_run.run_id
    assert manifest["conclusion"] == "pass"
    assert manifest["input_fingerprints"]["dataset"] == clean_run.report["inputs"]["dataset"]["sha256"]
    assert manifest["artifacts"]["report.json"]["sha256"] == sha256_file(clean_run.report_path)
    assert manifest["artifacts"]["report.md"]["sha256"] == sha256_file(clean_run.markdown_path)
    assert manifest["record_counts"]["total_records"] == 30
    assert manifest["finding_counts"] == {"block": 0, "warn": 0, "info": 0}


def test_sqlite_ledger_tables_are_queryable(clean_run) -> None:
    connection = sqlite3.connect(clean_run.ledger_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"runs", "findings", "metrics", "artifacts"} <= tables
        run = connection.execute(
            "SELECT conclusion, block_count, warn_count FROM runs WHERE run_id=?",
            (clean_run.run_id,),
        ).fetchone()
        assert run == ("pass", 0, 0)
        metric = connection.execute(
            "SELECT metric_value FROM metrics WHERE run_id=? AND metric_key='total_records'",
            (clean_run.run_id,),
        ).fetchone()
        assert metric == ("30",)
        artifact_count = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE run_id=?", (clean_run.run_id,)
        ).fetchone()[0]
        assert artifact_count == 4
    finally:
        connection.close()


def test_leaky_ledger_findings_and_idempotent_rewrite(leaky_run) -> None:
    connection = sqlite3.connect(leaky_run.ledger_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id=?", (leaky_run.run_id,)
        ).fetchone()[0]
        assert count == len(leaky_run.report["findings"])
        codes = {
            row[0]
            for row in connection.execute(
                "SELECT code FROM findings WHERE run_id=?", (leaky_run.run_id,)
            )
        }
        assert "EXACT_DUPLICATE_CROSS_SPLIT" in codes
        assert "GROUP_LEAKAGE" in codes
        assert "TEMPORAL_LEAKAGE" in codes
    finally:
        connection.close()
    write_run(leaky_run.report, leaky_run.out_dir)
    connection = sqlite3.connect(leaky_run.ledger_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == len(
            leaky_run.report["findings"]
        )
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 4
    finally:
        connection.close()
