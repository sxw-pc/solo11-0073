"""JSON configuration loading and validation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import InputError

_SEVERITIES = {"block", "warn"}


@dataclass(frozen=True)
class NearDuplicateConfig:
    enabled: bool = True
    ngram: int = 5
    jaccard_threshold: float = 0.85
    max_candidates: int = 20000


@dataclass(frozen=True)
class TemporalConfig:
    enabled: bool = False
    split_order: tuple[str, ...] = ()
    min_gap_seconds: int = 0


@dataclass(frozen=True)
class Config:
    dataset_id_column: str = "record_id"
    text_columns: tuple[str, ...] = ()
    group_column: str | None = None
    group_isolation: bool = False
    time_column: str | None = None
    label_column: str | None = None
    split_column: str = "split"
    expected_splits: tuple[str, ...] = ("train", "valid", "test")
    expected_ratios: dict[str, float] | None = None
    ratio_tolerance: float = 0.02
    near_duplicate: NearDuplicateConfig = NearDuplicateConfig()
    temporal: TemporalConfig = TemporalConfig()
    empty_split_severity: str = "block"
    invalid_time_severity: str = "block"
    small_split_threshold: int = 5
    label_reference_split: str = "train"

    def __post_init__(self) -> None:
        if self.expected_ratios is None:
            object.__setattr__(self, "expected_ratios", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id_column": self.dataset_id_column,
            "text_columns": list(self.text_columns),
            "group_column": self.group_column,
            "group_isolation": self.group_isolation,
            "time_column": self.time_column,
            "label_column": self.label_column,
            "split_column": self.split_column,
            "expected_splits": list(self.expected_splits),
            "expected_ratios": dict(sorted((self.expected_ratios or {}).items())),
            "ratio_tolerance": self.ratio_tolerance,
            "near_duplicate": {
                "enabled": self.near_duplicate.enabled,
                "ngram": self.near_duplicate.ngram,
                "jaccard_threshold": self.near_duplicate.jaccard_threshold,
                "max_candidates": self.near_duplicate.max_candidates,
            },
            "temporal": {
                "enabled": self.temporal.enabled,
                "split_order": list(self.temporal.split_order),
                "min_gap_seconds": self.temporal.min_gap_seconds,
            },
            "empty_split_severity": self.empty_split_severity,
            "invalid_time_severity": self.invalid_time_severity,
            "small_split_threshold": self.small_split_threshold,
            "label_reference_split": self.label_reference_split,
        }


def _fail(path: Path, field: str, message: str) -> InputError:
    return InputError("CONFIG_INVALID_VALUE", message, path=path, field_name=field)


def _string(
    data: dict[str, Any], key: str, default: str, path: Path, *, allow_empty: bool = False
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise _fail(path, key, "must be a string")
    if not allow_empty and not value.strip():
        raise _fail(path, key, "must not be empty")
    return value


def _optional_string(data: dict[str, Any], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, key, "must be null or a non-empty string")
    return value


def _boolean(data: dict[str, Any], key: str, default: bool, path: Path) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise _fail(path, key, "must be a boolean")
    return value


def _number(data: dict[str, Any], key: str, default: float, path: Path) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, key, "must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise _fail(path, key, "must be a finite number")
    return number


def _integer(data: dict[str, Any], key: str, default: int, path: Path) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, key, "must be an integer")
    return value


def _string_list(
    data: dict[str, Any], key: str, default: list[str] | tuple[str, ...], path: Path
) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _fail(path, key, "must be a list of strings")
    if any(not item.strip() for item in value):
        raise _fail(path, key, "must not contain empty strings")
    if len(set(value)) != len(value):
        raise _fail(path, key, "must not contain duplicate values")
    return tuple(value)


def config_from_dict(data: Any, path: str | Path = "<config>") -> Config:
    config_path = Path(path)
    if not isinstance(data, dict):
        raise _fail(config_path, "$", "configuration must be a JSON object")

    expected_splits = _string_list(
        data, "expected_splits", ["train", "valid", "test"], config_path
    )
    if not expected_splits:
        raise _fail(config_path, "expected_splits", "must contain at least one split")

    ratio_value = data.get("expected_ratios", {})
    if not isinstance(ratio_value, dict):
        raise _fail(config_path, "expected_ratios", "must be an object")
    expected_ratios: dict[str, float] = {}
    for name, ratio in ratio_value.items():
        if not isinstance(name, str) or name not in expected_splits:
            raise _fail(config_path, "expected_ratios", f"unknown split {name!r}")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise _fail(config_path, f"expected_ratios.{name}", "must be a number")
        number = float(ratio)
        if not math.isfinite(number) or number < 0 or number > 1:
            raise _fail(config_path, f"expected_ratios.{name}", "must be between 0 and 1")
        expected_ratios[name] = number

    near_data = data.get("near_duplicate", {})
    if not isinstance(near_data, dict):
        raise _fail(config_path, "near_duplicate", "must be an object")
    ngram = _integer(near_data, "ngram", 5, config_path)
    threshold = _number(near_data, "jaccard_threshold", 0.85, config_path)
    max_candidates = _integer(near_data, "max_candidates", 20000, config_path)
    if ngram < 1:
        raise _fail(config_path, "near_duplicate.ngram", "must be at least 1")
    if not 0 < threshold <= 1:
        raise _fail(
            config_path, "near_duplicate.jaccard_threshold", "must be in (0, 1]"
        )
    if max_candidates < 1:
        raise _fail(config_path, "near_duplicate.max_candidates", "must be at least 1")

    temporal_data = data.get("temporal", {})
    if not isinstance(temporal_data, dict):
        raise _fail(config_path, "temporal", "must be an object")
    temporal_enabled = _boolean(temporal_data, "enabled", False, config_path)
    raw_order = temporal_data.get("split_order", list(expected_splits))
    if not isinstance(raw_order, list) or any(
        not isinstance(item, str) for item in raw_order
    ):
        raise _fail(config_path, "temporal.split_order", "must be a list of strings")
    split_order = tuple(raw_order)
    if len(set(split_order)) != len(split_order):
        raise _fail(
            config_path, "temporal.split_order", "must not contain duplicates"
        )
    unknown_order = sorted(set(split_order) - set(expected_splits))
    if unknown_order:
        raise _fail(
            config_path,
            "temporal.split_order",
            f"unknown splits: {', '.join(unknown_order)}",
        )
    min_gap_seconds = _integer(temporal_data, "min_gap_seconds", 0, config_path)
    if min_gap_seconds < 0:
        raise _fail(config_path, "temporal.min_gap_seconds", "must be non-negative")

    group_column = _optional_string(data, "group_column", config_path)
    group_isolation = _boolean(data, "group_isolation", False, config_path)
    if group_isolation and group_column is None:
        raise _fail(
            config_path, "group_column", "is required when group_isolation is true"
        )
    time_column = _optional_string(data, "time_column", config_path)
    if temporal_enabled and time_column is None:
        raise _fail(config_path, "time_column", "is required when temporal.enabled is true")

    empty_severity = _string(
        data, "empty_split_severity", "block", config_path
    ).lower()
    invalid_severity = _string(
        data, "invalid_time_severity", "block", config_path
    ).lower()
    for field_name, severity in (
        ("empty_split_severity", empty_severity),
        ("invalid_time_severity", invalid_severity),
    ):
        if severity not in _SEVERITIES:
            raise _fail(config_path, field_name, "must be 'block' or 'warn'")

    ratio_tolerance = _number(data, "ratio_tolerance", 0.02, config_path)
    if ratio_tolerance < 0:
        raise _fail(config_path, "ratio_tolerance", "must be non-negative")
    small_threshold = _integer(data, "small_split_threshold", 5, config_path)
    if small_threshold < 0:
        raise _fail(config_path, "small_split_threshold", "must be non-negative")
    reference = _string(data, "label_reference_split", "train", config_path)
    if reference not in expected_splits:
        raise _fail(
            config_path, "label_reference_split", "must be one of expected_splits"
        )

    return Config(
        dataset_id_column=_string(data, "dataset_id_column", "record_id", config_path),
        text_columns=_string_list(data, "text_columns", [], config_path),
        group_column=group_column,
        group_isolation=group_isolation,
        time_column=time_column,
        label_column=_optional_string(data, "label_column", config_path),
        split_column=_string(data, "split_column", "split", config_path),
        expected_splits=expected_splits,
        expected_ratios=expected_ratios,
        ratio_tolerance=ratio_tolerance,
        near_duplicate=NearDuplicateConfig(
            enabled=_boolean(near_data, "enabled", True, config_path),
            ngram=ngram,
            jaccard_threshold=threshold,
            max_candidates=max_candidates,
        ),
        temporal=TemporalConfig(
            enabled=temporal_enabled,
            split_order=split_order,
            min_gap_seconds=min_gap_seconds,
        ),
        empty_split_severity=empty_severity,
        invalid_time_severity=invalid_severity,
        small_split_threshold=small_threshold,
        label_reference_split=reference,
    )


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise InputError("CONFIG_NOT_FOUND", "configuration file does not exist", path=config_path)
    if not config_path.is_file():
        raise InputError("CONFIG_NOT_A_FILE", "configuration path is not a file", path=config_path)
    try:
        raw = config_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(
            "CONFIG_DECODE_ERROR", "configuration is not valid UTF-8", path=config_path
        ) from exc
    except OSError as exc:
        raise InputError(
            "CONFIG_READ_ERROR", f"cannot read configuration: {exc}", path=config_path
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(
            "CONFIG_INVALID_JSON",
            f"invalid JSON: {exc.msg}",
            path=config_path,
            line=exc.lineno,
        ) from exc
    return config_from_dict(data, config_path)
