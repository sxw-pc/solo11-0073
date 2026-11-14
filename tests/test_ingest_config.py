import json

import pytest

from splitguard.config import load_config
from splitguard.ingest import load_dataset, load_manifest
from splitguard.models import InputError
from tests.helpers import make_config, row


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_dataset_supports_utf8_bom_and_line_numbers(tmp_path) -> None:
    path = tmp_path / "dataset.csv"
    path.write_bytes(
        "﻿record_id,text,entity_id,event_time,label\n"
        "record-0001,alpha,entity-0001,2025-01-01T00:00:00Z,demo-label-a\n".encode("utf-8")
    )
    dataset = load_dataset(path, make_config())
    assert dataset.records["record-0001"].values["text"] == "alpha"
    assert dataset.records["record-0001"].line_no == 2


def test_dataset_rejects_duplicate_and_empty_headers(tmp_path) -> None:
    config = make_config()
    duplicate = tmp_path / "duplicate.csv"
    _write(duplicate, "record_id,text,text,entity_id,event_time,label\na,b,c,d,e,f\n")
    with pytest.raises(InputError) as error:
        load_dataset(duplicate, config)
    assert error.value.code == "DATASET_DUPLICATE_HEADER"
    assert error.value.line == 1

    empty = tmp_path / "empty_header.csv"
    _write(empty, ",text,entity_id,event_time,label\n")
    with pytest.raises(InputError) as error:
        load_dataset(empty, config)
    assert error.value.code == "DATASET_EMPTY_HEADER_FIELD"


def test_dataset_rejects_missing_column_empty_and_duplicate_ids(tmp_path) -> None:
    config = make_config()
    missing = tmp_path / "missing.csv"
    _write(missing, "record_id,text,entity_id,event_time\nrecord-0001,x,g,t\n")
    with pytest.raises(InputError) as error:
        load_dataset(missing, config)
    assert error.value.code == "DATASET_MISSING_COLUMN"

    empty_id = tmp_path / "empty_id.csv"
    _write(
        empty_id,
        "record_id,text,entity_id,event_time,label\n,alpha,entity-0001,t,demo-label-a\n",
    )
    with pytest.raises(InputError) as error:
        load_dataset(empty_id, config)
    assert error.value.code == "DATASET_EMPTY_ID"
    assert error.value.line == 2

    duplicate = tmp_path / "duplicate_id.csv"
    _write(
        duplicate,
        "record_id,text,entity_id,event_time,label\n"
        "record-0001,alpha,entity-0001,t,demo-label-a\n"
        "record-0001,beta,entity-0002,t,demo-label-a\n",
    )
    with pytest.raises(InputError) as error:
        load_dataset(duplicate, config)
    assert error.value.code == "DATASET_DUPLICATE_ID"
    assert "line 2" in str(error.value)


def test_manifest_only_allows_expected_columns_and_keeps_empty_split(tmp_path) -> None:
    config = make_config()
    manifest_path = tmp_path / "splits.csv"
    _write(manifest_path, "record_id,split\nrecord-0001,\n")
    manifest = load_manifest(manifest_path, config)
    assert manifest.entries[0].split == ""
    assert manifest.entries[0].line_no == 2

    extra = tmp_path / "extra.csv"
    _write(extra, "record_id,split,note\nrecord-0001,train,x\n")
    with pytest.raises(InputError) as error:
        load_manifest(extra, config)
    assert error.value.code == "MANIFEST_UNEXPECTED_COLUMN"

    custom = tmp_path / "custom.csv"
    _write(custom, "record_id,partition\nrecord-0001,train\n")
    custom_config = make_config(split_column="partition")
    assert load_manifest(custom, custom_config).entries[0].split == "train"


def test_input_file_not_found_and_empty_file(tmp_path) -> None:
    with pytest.raises(InputError) as error:
        load_dataset(tmp_path / "absent.csv", make_config())
    assert error.value.code == "DATASET_NOT_FOUND"
    empty = tmp_path / "empty.csv"
    _write(empty, "")
    with pytest.raises(InputError) as error:
        load_dataset(empty, make_config())
    assert error.value.code == "DATASET_EMPTY_FILE"


def test_config_validation_and_json_errors(tmp_path) -> None:
    invalid = tmp_path / "config.json"
    _write(invalid, "{not json")
    with pytest.raises(InputError) as error:
        load_config(invalid)
    assert error.value.code == "CONFIG_INVALID_JSON"
    assert error.value.line == 1

    bad_threshold = tmp_path / "threshold.json"
    bad_threshold.write_text(
        json.dumps(
            {
                "near_duplicate": {"enabled": True, "ngram": 5, "jaccard_threshold": 2}
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(InputError) as error:
        load_config(bad_threshold)
    assert error.value.code == "CONFIG_INVALID_VALUE"

    missing_time = tmp_path / "time.json"
    missing_time.write_text(
        json.dumps({"temporal": {"enabled": True}}), encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_config(missing_time)
    assert error.value.code == "CONFIG_INVALID_VALUE"

    group = tmp_path / "group.json"
    group.write_text(json.dumps({"group_isolation": True}), encoding="utf-8")
    with pytest.raises(InputError) as error:
        load_config(group)
    assert error.value.code == "CONFIG_INVALID_VALUE"


def test_config_unknown_ratio_and_order_are_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"expected_ratios": {"holdout": 0.1}}), encoding="utf-8"
    )
    with pytest.raises(InputError):
        load_config(path)
    path.write_text(
        json.dumps({"temporal": {"split_order": ["train", "unknown"]}}),
        encoding="utf-8",
    )
    with pytest.raises(InputError):
        load_config(path)


def test_config_defaults_are_stable() -> None:
    config = make_config()
    assert config.dataset_id_column == "record_id"
    assert config.split_column == "split"
    assert config.expected_splits == ("train", "valid", "test")
    assert config.to_dict()["near_duplicate"]["ngram"] == 5


def test_row_helper_is_synthetic() -> None:
    assert row("record-0001")["text"] == ""
