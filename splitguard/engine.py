"""Audit orchestration: load, checks, metrics and deterministic report core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, TOOL_NAME, VERSION
from .checks.composition import run_composition_checks
from .checks.duplicates import run_exact_duplicate_checks
from .checks.leakage import run_group_checks, run_temporal_checks
from .checks.manifest import run_manifest_checks
from .checks.near_duplicates import run_near_duplicate_checks
from .config import Config, load_config
from .fingerprint import canonical_sha256, iso_utc, sha256_file, utc_now
from .ingest import load_dataset, load_manifest
from .models import Dataset, InputError, Manifest, conclusion_for, severity_counts, sort_findings


@dataclass
class AuditBundle:
    report: dict[str, Any]


@dataclass
class LoadedInputs:
    config: Config
    dataset: Dataset
    manifest: Manifest
    dataset_sha256: str
    splits_sha256: str
    config_sha256: str
    config_canonical_sha256: str


def _safe_hash(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise InputError(
            "INPUT_READ_ERROR", f"cannot hash input file: {exc}", path=path
        ) from exc


def load_inputs(
    dataset_path: str | Path, splits_path: str | Path, config_path: str | Path
) -> LoadedInputs:
    dataset_file = Path(dataset_path)
    splits_file = Path(splits_path)
    config_file = Path(config_path)
    config = load_config(config_file)
    dataset = load_dataset(dataset_file, config)
    manifest = load_manifest(splits_file, config)
    return LoadedInputs(
        config=config,
        dataset=dataset,
        manifest=manifest,
        dataset_sha256=_safe_hash(dataset_file),
        splits_sha256=_safe_hash(splits_file),
        config_sha256=_safe_hash(config_file),
        config_canonical_sha256=canonical_sha256(config.to_dict()),
    )


def _run_checks(loaded: LoadedInputs) -> tuple[list[Any], dict[str, Any]]:
    config = loaded.config
    manifest_result = run_manifest_checks(loaded.dataset, loaded.manifest, config)
    assignments = manifest_result.assignments
    exact_result = run_exact_duplicate_checks(loaded.dataset, assignments, config)
    near_result = run_near_duplicate_checks(loaded.dataset, assignments, config)
    group_result = run_group_checks(loaded.dataset, assignments, config)
    temporal_result = run_temporal_checks(loaded.dataset, assignments, config)
    composition_result = run_composition_checks(loaded.dataset, assignments, config)

    findings = sort_findings(
        [
            *manifest_result.findings,
            *exact_result.findings,
            *near_result.findings,
            *group_result.findings,
            *temporal_result.findings,
            *composition_result.findings,
        ]
    )
    metrics: dict[str, Any] = {
        **manifest_result.metrics,
        "exact_duplicates": exact_result.metrics,
        "near_duplicates": near_result.metrics,
        "group_isolation": group_result.metrics,
        "temporal": temporal_result.metrics,
        "composition": composition_result.metrics,
    }
    return findings, metrics


def build_audit(
    dataset_path: str | Path, splits_path: str | Path, config_path: str | Path
) -> AuditBundle:
    started = utc_now()
    loaded = load_inputs(dataset_path, splits_path, config_path)
    findings, metrics = _run_checks(loaded)
    generated = utc_now()
    finding_dicts = [finding.to_dict() for finding in findings]
    counts = severity_counts(findings)
    conclusion = conclusion_for(findings)
    near_duplicate_metrics = metrics.get("near_duplicates", {})
    summary = {
        "conclusion": conclusion,
        "finding_counts": counts,
        "findings_total": len(findings),
        "total_records": metrics["total_records"],
        "assigned_records": metrics["assigned_records"],
        "split_counts": metrics["split_counts"],
        "near_duplicate": {
            "candidate_mode": near_duplicate_metrics.get("candidate_mode"),
            "completeness": near_duplicate_metrics.get("completeness"),
        },
    }
    identity = {
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "schema_version": SCHEMA_VERSION,
        "input_sha256": {
            "dataset": loaded.dataset_sha256,
            "splits": loaded.splits_sha256,
            "config": loaded.config_sha256,
        },
        "config": loaded.config.to_dict(),
    }
    run_id = "run-" + canonical_sha256(identity)[:16]
    core_payload = {
        "run_id": run_id,
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "schema_version": SCHEMA_VERSION,
        "input_sha256": identity["input_sha256"],
        "config": loaded.config.to_dict(),
        "summary": summary,
        "metrics": metrics,
        "findings": finding_dicts,
    }
    report_core_sha256 = canonical_sha256(core_payload)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "started_at": iso_utc(started),
        "generated_at": iso_utc(generated),
        "finished_at": iso_utc(generated),
        "inputs": {
            "dataset": {
                "path": loaded.dataset.path.as_posix(),
                "sha256": loaded.dataset_sha256,
                "record_count": len(loaded.dataset.records),
            },
            "splits": {
                "path": loaded.manifest.path.as_posix(),
                "sha256": loaded.splits_sha256,
                "entry_count": len(loaded.manifest.entries),
            },
            "config": {
                "path": Path(config_path).as_posix(),
                "sha256": loaded.config_sha256,
                "canonical_sha256": loaded.config_canonical_sha256,
            },
        },
        "config": loaded.config.to_dict(),
        "conclusion": conclusion,
        "summary": summary,
        "metrics": metrics,
        "findings": finding_dicts,
        "reproducibility": {
            "report_core_sha256": report_core_sha256,
            "canonicalization": (
                "JSON with sorted keys, compact separators, UTF-8; "
                "record IDs and findings sorted"
            ),
            "excluded_runtime_fields": [
                "started_at",
                "generated_at",
                "finished_at",
            ],
        },
    }
    return AuditBundle(report)
