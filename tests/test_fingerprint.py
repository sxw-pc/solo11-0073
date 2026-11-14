import hashlib
from datetime import datetime, timedelta, timezone

from splitguard.fingerprint import (
    atomic_write_text,
    canonical_json,
    canonical_sha256,
    iso_utc,
    sha256_file,
    sha256_text,
)


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 2, "a": [1, "中"]}) == '{"a":[1,"中"],"b":2}'


def test_canonical_hash_ignores_mapping_order() -> None:
    left = {"b": {"y": 2, "x": 1}, "a": [1, 2]}
    right = {"a": [1, 2], "b": {"x": 1, "y": 2}}
    assert canonical_sha256(left) == canonical_sha256(right)
    assert canonical_sha256(left) != canonical_sha256({"a": [2, 1], "b": left["b"]})


def test_sha256_helpers(tmp_path) -> None:
    path = tmp_path / "data.txt"
    atomic_write_text(path, "synthetic")
    assert sha256_file(path) == hashlib.sha256(b"synthetic").hexdigest()
    assert sha256_text("synthetic") == sha256_file(path)


def test_iso_utc_normalizes_offsets() -> None:
    value = datetime(2025, 1, 1, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    assert iso_utc(value) == "2025-01-01T00:30:00.000000Z"
