from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from typing import Any

from splitguard.config import Config, config_from_dict
from splitguard.models import Dataset, Manifest, ManifestEntry, Record

BASE_CONFIG: dict[str, Any] = {
    "dataset_id_column": "record_id",
    "text_columns": ["text"],
    "group_column": "entity_id",
    "group_isolation": True,
    "time_column": "event_time",
    "label_column": "label",
    "split_column": "split",
    "expected_splits": ["train", "valid", "test"],
    "expected_ratios": {"train": 0.7, "valid": 0.15, "test": 0.15},
    "ratio_tolerance": 0.05,
    "near_duplicate": {
        "enabled": True,
        "ngram": 5,
        "jaccard_threshold": 0.85,
        "max_candidates": 2000,
    },
    "temporal": {
        "enabled": False,
        "split_order": ["train", "valid", "test"],
        "min_gap_seconds": 0,
    },
    "empty_split_severity": "block",
    "invalid_time_severity": "block",
    "small_split_threshold": 1,
    "label_reference_split": "train",
}


def config_data(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data


def make_config(**overrides: Any) -> Config:
    return config_from_dict(config_data(**overrides), "<test-config>")


def make_dataset(rows: list[dict[str, str]]) -> Dataset:
    if not rows:
        return Dataset(Path("dataset.csv"), ("record_id",), {})
    fieldnames = tuple(rows[0].keys())
    records: dict[str, Record] = {}
    for index, row in enumerate(rows):
        record_id = row["record_id"]
        records[record_id] = Record(record_id, dict(row), index + 2)
    return Dataset(Path("dataset.csv"), fieldnames, records)


def make_manifest(assignments: list[tuple[str, str]] | dict[str, str]) -> Manifest:
    pairs = (
        list(assignments.items()) if isinstance(assignments, dict) else list(assignments)
    )
    return Manifest(
        Path("splits.csv"),
        [
            ManifestEntry(record_id, split, index + 2)
            for index, (record_id, split) in enumerate(pairs)
        ],
    )


def row(
    record_id: str,
    text: str = "",
    group: str = "",
    time: str = "2025-01-01T00:00:00Z",
    label: str = "demo-label-a",
    **extra: str,
) -> dict[str, str]:
    values = {
        "record_id": record_id,
        "text": text,
        "entity_id": group,
        "event_time": time,
        "label": label,
    }
    values.update(extra)
    return values


def write_case(
    tmp_path: Path,
    rows: list[dict[str, str]],
    assignments: list[tuple[str, str]],
    **config_overrides: Any,
) -> dict[str, Path]:
    dataset_path = tmp_path / "dataset.csv"
    splits_path = tmp_path / "splits.csv"
    config_path = tmp_path / "config.json"
    with dataset_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with splits_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["record_id", "split"])
        writer.writerows(assignments)
    config_path.write_text(
        json.dumps(config_data(**config_overrides), ensure_ascii=False),
        encoding="utf-8",
    )
    return {"dataset": dataset_path, "splits": splits_path, "config": config_path}
