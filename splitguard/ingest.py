"""CSV dataset and split manifest loading with stable input errors."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .config import Config
from .models import Dataset, InputError, Manifest, ManifestEntry, Record

Rows = list[tuple[int, list[str]]]


def _display(path: Path, display_path: Path | None) -> Path:
    """Path shown in errors and reports (the audited source, not a snapshot)."""
    return display_path if display_path is not None else path


def _ensure_input_file(path: Path, kind: str, display_path: Path | None = None) -> None:
    shown = _display(path, display_path)
    code = kind.upper()
    if not path.exists():
        raise InputError(f"{code}_NOT_FOUND", f"{kind} file does not exist", path=shown)
    if not path.is_file():
        raise InputError(f"{code}_NOT_A_FILE", f"{kind} path is not a file", path=shown)


def _read_rows(path: Path, kind: str, display_path: Path | None = None) -> Rows:
    shown = _display(path, display_path)
    _ensure_input_file(path, kind, shown)
    code = kind.upper()
    rows: Rows = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            for row in reader:
                rows.append((reader.line_num, row))
    except UnicodeDecodeError as exc:
        raise InputError(
            f"{code}_DECODE_ERROR", "file is not valid UTF-8", path=shown
        ) from exc
    except csv.Error as exc:
        raise InputError(
            f"{code}_CSV_ERROR", f"malformed CSV: {exc}", path=shown
        ) from exc
    except OSError as exc:
        raise InputError(
            f"{code}_READ_ERROR", f"cannot read file: {exc}", path=shown
        ) from exc
    if not rows:
        raise InputError(f"{code}_EMPTY_FILE", "file is empty", path=shown)
    return rows


def _validate_header(path: Path, kind: str, header: list[str]) -> tuple[str, ...]:
    code = kind.upper()
    if not header or all(not cell.strip() for cell in header):
        raise InputError(f"{code}_EMPTY_HEADER", "header is empty", path=path, line=1)
    empty_positions = [index + 1 for index, cell in enumerate(header) if not cell.strip()]
    if empty_positions:
        raise InputError(
            f"{code}_EMPTY_HEADER_FIELD",
            f"empty header fields at position(s) {empty_positions}",
            path=path,
            line=1,
        )
    seen: set[str] = set()
    duplicates: set[str] = set()
    for cell in header:
        if cell in seen:
            duplicates.add(cell)
        seen.add(cell)
    if duplicates:
        raise InputError(
            f"{code}_DUPLICATE_HEADER",
            f"duplicate header field(s): {', '.join(sorted(duplicates))}",
            path=path,
            line=1,
            field_name=sorted(duplicates)[0],
        )
    return tuple(header)


def _data_rows(path: Path, kind: str, rows: Rows) -> Iterable[tuple[int, list[str]]]:
    header = _validate_header(path, kind, rows[0][1])
    width = len(header)
    for line_no, row in rows[1:]:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != width:
            raise InputError(
                f"{kind.upper()}_ROW_LENGTH",
                f"row has {len(row)} fields but header has {width}",
                path=path,
                line=line_no,
            )
        yield line_no, row


def _required_dataset_columns(config: Config) -> list[str]:
    required = [config.dataset_id_column, *config.text_columns]
    if config.group_isolation and config.group_column is not None:
        required.append(config.group_column)
    if config.temporal.enabled and config.time_column is not None:
        required.append(config.time_column)
    if config.label_column is not None:
        required.append(config.label_column)
    # Preserve configuration order while removing accidental duplicates.
    return list(dict.fromkeys(required))


def load_dataset(
    path: str | Path, config: Config, *, display_path: str | Path | None = None
) -> Dataset:
    dataset_path = Path(path)
    shown = _display(dataset_path, None if display_path is None else Path(display_path))
    rows = _read_rows(dataset_path, "dataset", shown)
    header = _validate_header(shown, "dataset", rows[0][1])
    required = _required_dataset_columns(config)
    missing = sorted(set(required) - set(header))
    if missing:
        raise InputError(
            "DATASET_MISSING_COLUMN",
            f"missing required column(s): {', '.join(missing)}",
            path=shown,
            line=1,
            field_name=missing[0],
        )
    index = {name: position for position, name in enumerate(header)}
    id_column = config.dataset_id_column
    records: dict[str, Record] = {}
    first_seen: dict[str, int] = {}
    for line_no, row in _data_rows(shown, "dataset", rows):
        values = {name: row[position] for name, position in index.items()}
        record_id = values[id_column].strip()
        if not record_id:
            raise InputError(
                "DATASET_EMPTY_ID",
                "record identifier is empty",
                path=shown,
                line=line_no,
                field_name=id_column,
            )
        if record_id in records:
            raise InputError(
                "DATASET_DUPLICATE_ID",
                f"record identifier {record_id!r} first appeared on line {first_seen[record_id]}",
                path=shown,
                line=line_no,
                field_name=id_column,
            )
        first_seen[record_id] = line_no
        records[record_id] = Record(record_id, values, line_no)
    return Dataset(shown, header, records)


def load_manifest(
    path: str | Path, config: Config, *, display_path: str | Path | None = None
) -> Manifest:
    manifest_path = Path(path)
    shown = _display(manifest_path, None if display_path is None else Path(display_path))
    rows = _read_rows(manifest_path, "splits", shown)
    header = _validate_header(shown, "splits", rows[0][1])
    expected_columns = ("record_id", config.split_column)
    header_set = set(header)
    missing = [name for name in expected_columns if name not in header_set]
    if missing:
        raise InputError(
            "MANIFEST_MISSING_COLUMN",
            f"missing required column(s): {', '.join(missing)}",
            path=shown,
            line=1,
            field_name=missing[0],
        )
    extra = sorted(header_set - set(expected_columns))
    if extra:
        raise InputError(
            "MANIFEST_UNEXPECTED_COLUMN",
            f"only record_id and {config.split_column} are allowed; unexpected: {', '.join(extra)}",
            path=shown,
            line=1,
            field_name=extra[0],
        )
    entry_index = {name: position for position, name in enumerate(header)}
    entries: list[ManifestEntry] = []
    for line_no, row in _data_rows(shown, "splits", rows):
        entries.append(
            ManifestEntry(
                record_id=row[entry_index["record_id"]].strip(),
                split=row[entry_index[config.split_column]].strip(),
                line_no=line_no,
            )
        )
    return Manifest(shown, entries, config.split_column)
