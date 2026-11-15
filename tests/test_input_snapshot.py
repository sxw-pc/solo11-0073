"""Race-condition and lifecycle tests for per-run input snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

import pytest

from splitguard import SCHEMA_VERSION, TOOL_NAME, VERSION
from splitguard import engine, snapshot
from splitguard.engine import build_audit, load_inputs
from splitguard.fingerprint import canonical_sha256, sha256_file
from splitguard.models import InputError
from splitguard.report import write_run
from splitguard.snapshot import SNAPSHOT_DIR_PREFIX, SnapshotSession
from tests.helpers import row, write_case


def _snapshot_names(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(SNAPSHOT_DIR_PREFIX)]


def test_snapshot_capture_copies_bytes_with_private_permissions(tmp_path) -> None:
    payload = "record_id,text\nrecord-0001,alpha\n".encode("utf-8")
    source = tmp_path / "dataset.csv"
    source.write_bytes(payload)
    base = tmp_path / "snap-root"
    base.mkdir()

    with SnapshotSession(base_dir=base) as session:
        snap_dir = session.directory
        assert snap_dir is not None and snap_dir.parent == base
        assert snap_dir.name.startswith(SNAPSHOT_DIR_PREFIX)
        assert stat.S_IMODE(snap_dir.stat().st_mode) == 0o700
        captured = session.capture(source, "dataset")
        assert captured.parent == snap_dir
        assert captured.read_bytes() == payload
        assert sha256_file(captured) == hashlib.sha256(payload).hexdigest()
        assert stat.S_IMODE(captured.stat().st_mode) == 0o600

    assert session.directory is None
    assert not snap_dir.exists()
    assert list(base.iterdir()) == []
    session.cleanup()  # cleanup is idempotent


def test_snapshot_capture_error_codes_are_stable(tmp_path) -> None:
    with SnapshotSession(base_dir=tmp_path) as session:
        with pytest.raises(InputError) as missing:
            session.capture(tmp_path / "absent.csv", "dataset")
        assert missing.value.code == "DATASET_NOT_FOUND"
        with pytest.raises(InputError) as not_file:
            session.capture(tmp_path, "config")
        assert not_file.value.code == "CONFIG_NOT_A_FILE"


def test_snapshot_capture_requires_active_session() -> None:
    with pytest.raises(RuntimeError):
        SnapshotSession().capture("x", "dataset")


def test_snapshot_session_cleanup_on_exception(tmp_path) -> None:
    source = tmp_path / "dataset.csv"
    source.write_bytes(b"record_id\nrecord-0001\n")
    with pytest.raises(RuntimeError):
        with SnapshotSession(base_dir=tmp_path) as session:
            session.capture(source, "dataset")
            raise RuntimeError("boom")
    assert _snapshot_names(tmp_path) == []


def test_dataset_overwritten_between_parse_and_hash_keeps_snapshot_binding(
    tmp_path, monkeypatch
) -> None:
    rows = [
        row("record-0001", text="shared-alpha", group="entity-0001"),
        row("record-0002", text="shared-alpha", group="entity-0002"),
    ]
    assignments = [("record-0001", "train"), ("record-0002", "test")]
    paths = write_case(tmp_path, rows, assignments)
    original = paths["dataset"].read_bytes()
    # Same-length overwrite: size/mtime heuristics cannot save us here.
    replacement = original.replace(b"shared-alpha", b"shared-omega", 1)
    assert len(replacement) == len(original) and replacement != original

    real_load_manifest = engine.load_manifest

    def overwrite_between_parse_and_hash(path, config, *, display_path=None):
        # The dataset source is replaced after it was parsed but before its
        # fingerprint is computed; the run must stay on the captured bytes.
        paths["dataset"].write_bytes(replacement)
        return real_load_manifest(path, config, display_path=display_path)

    monkeypatch.setattr(engine, "load_manifest", overwrite_between_parse_and_hash)

    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report

    old_sha = hashlib.sha256(original).hexdigest()
    new_sha = hashlib.sha256(replacement).hexdigest()
    assert old_sha != new_sha
    # The old row set is bound to the old SHA-256, never to the new one.
    assert report["inputs"]["dataset"]["sha256"] == old_sha
    duplicates = [
        finding
        for finding in report["findings"]
        if finding["code"] == "EXACT_DUPLICATE_CROSS_SPLIT"
    ]
    assert duplicates, "findings must come from the captured (old) rows"
    assert duplicates[0]["record_ids"] == ["record-0001", "record-0002"]
    # The source path now holds the replacement; the report does not claim it.
    assert sha256_file(paths["dataset"]) == new_sha


def test_manifest_replaced_mid_run_keeps_single_snapshot_assignment(
    tmp_path, monkeypatch
) -> None:
    rows = [row("record-0001"), row("record-0002"), row("record-0003")]
    assignments = [("record-0001", "train"), ("record-0002", "valid"), ("record-0003", "test")]
    paths = write_case(tmp_path, rows, assignments)
    old_splits = paths["splits"].read_bytes()

    real_load_manifest = engine.load_manifest

    def replace_after_capture(path, config, *, display_path=None):
        # Atomic rename-replace of the source after its snapshot was captured.
        new_splits = tmp_path / "splits-replacement.csv"
        new_splits.write_text(
            "record_id,split\nrecord-0001,test\nrecord-0002,test\nrecord-0003,test\n",
            encoding="utf-8",
        )
        os.replace(new_splits, paths["splits"])
        return real_load_manifest(path, config, display_path=display_path)

    monkeypatch.setattr(engine, "load_manifest", replace_after_capture)

    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report

    # The assignment is entirely the captured one — no old/new mixture.
    assert report["summary"]["split_counts"] == {"train": 1, "valid": 1, "test": 1}
    assert report["inputs"]["splits"]["sha256"] == hashlib.sha256(old_splits).hexdigest()
    assert report["inputs"]["splits"]["sha256"] != sha256_file(paths["splits"])


def test_manifest_changed_during_capture_aborts_without_residue(
    tmp_path, monkeypatch
) -> None:
    paths = write_case(tmp_path, [row("record-0001")], [("record-0001", "train")])
    controlled = tmp_path / "snap-root"
    controlled.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(controlled))

    real_copy = snapshot._stream_copy

    def rewrite_mid_copy(source, target, path, code):
        if Path(path) == paths["splits"]:
            # In-place rewrite while the capture streams; the longer content
            # makes the tripwire deterministic regardless of mtime granularity.
            paths["splits"].write_bytes(
                paths["splits"].read_bytes() + b"record-0002,test\n"
            )
        return real_copy(source, target, path, code)

    monkeypatch.setattr(snapshot, "_stream_copy", rewrite_mid_copy)

    with pytest.raises(InputError) as excinfo:
        load_inputs(paths["dataset"], paths["splits"], paths["config"])
    assert excinfo.value.code == "SPLITS_CHANGED_DURING_READ"
    assert excinfo.value.path == str(paths["splits"])
    assert list(controlled.iterdir()) == []


def test_report_hashes_recomputable_from_audited_snapshot_bytes(tmp_path) -> None:
    rows = [row("record-0001"), row("record-0002")]
    assignments = [("record-0001", "train"), ("record-0002", "test")]
    paths = write_case(tmp_path, rows, assignments)

    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report

    expected = {
        "dataset": hashlib.sha256(paths["dataset"].read_bytes()).hexdigest(),
        "splits": hashlib.sha256(paths["splits"].read_bytes()).hexdigest(),
        "config": hashlib.sha256(paths["config"].read_bytes()).hexdigest(),
    }
    for kind, digest in expected.items():
        assert report["inputs"][kind]["sha256"] == digest
    # run_id derives from the same snapshot identity shown in the report.
    identity = {
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "schema_version": SCHEMA_VERSION,
        "input_sha256": expected,
        "config": report["config"],
    }
    assert report["run_id"] == "run-" + canonical_sha256(identity)[:16]
    # Downstream artifacts (run manifest, ledger) carry the same identity.
    written = write_run(report, tmp_path / "out")
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    assert manifest["input_fingerprints"]["dataset"] == expected["dataset"]
    assert manifest["input_fingerprints"]["splits"] == expected["splits"]
    assert manifest["input_fingerprints"]["config"] == expected["config"]
    connection = sqlite3.connect(written.ledger_path)
    try:
        stored = connection.execute(
            "SELECT dataset_sha256, splits_sha256, config_sha256 FROM runs WHERE run_id=?",
            (report["run_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert stored == (expected["dataset"], expected["splits"], expected["config"])


def test_load_inputs_cleans_snapshot_dir_on_success_and_parse_failure(
    tmp_path, monkeypatch
) -> None:
    controlled = tmp_path / "snap-root"
    controlled.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(controlled))
    paths = write_case(tmp_path, [row("record-0001")], [("record-0001", "train")])

    load_inputs(paths["dataset"], paths["splits"], paths["config"])
    assert list(controlled.iterdir()) == []

    paths["dataset"].write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(InputError) as excinfo:
        load_inputs(paths["dataset"], paths["splits"], paths["config"])
    assert excinfo.value.code == "DATASET_DECODE_ERROR"
    assert list(controlled.iterdir()) == []

    paths["config"].write_text("{not json", encoding="utf-8")
    with pytest.raises(InputError) as excinfo:
        load_inputs(paths["dataset"], paths["splits"], paths["config"])
    assert excinfo.value.code == "CONFIG_INVALID_JSON"
    assert list(controlled.iterdir()) == []


def test_repeated_audits_of_same_inputs_share_identity_and_findings(tmp_path) -> None:
    rows = [
        row("record-0001", text="shared-alpha", group="entity-0001"),
        row("record-0002", text="shared-alpha", group="entity-0002"),
    ]
    assignments = [("record-0001", "train"), ("record-0002", "test")]
    paths = write_case(tmp_path, rows, assignments)

    first = build_audit(paths["dataset"], paths["splits"], paths["config"]).report
    second = build_audit(paths["dataset"], paths["splits"], paths["config"]).report

    assert first["run_id"] == second["run_id"]
    assert first["inputs"] == second["inputs"]
    assert first["findings"] == second["findings"]
    assert (
        first["reproducibility"]["report_core_sha256"]
        == second["reproducibility"]["report_core_sha256"]
    )


def test_run_identity_follows_snapshot_content_not_path(tmp_path) -> None:
    rows = [row("record-0001", text="shared-alpha")]
    assignments = [("record-0001", "train")]
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    paths_a = write_case(dir_a, rows, assignments)
    paths_b = write_case(dir_b, rows, assignments)

    report_a = build_audit(paths_a["dataset"], paths_a["splits"], paths_a["config"]).report
    report_b = build_audit(paths_b["dataset"], paths_b["splits"], paths_b["config"]).report

    assert report_a["run_id"] == report_b["run_id"]
    assert (
        report_a["reproducibility"]["report_core_sha256"]
        == report_b["reproducibility"]["report_core_sha256"]
    )


def test_errors_and_report_paths_reference_source_not_snapshot(
    tmp_path, monkeypatch
) -> None:
    controlled = tmp_path / "snap-root"
    controlled.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(controlled))
    paths = write_case(tmp_path, [row("record-0001")], [("record-0001", "train")])

    paths["dataset"].write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(InputError) as excinfo:
        load_inputs(paths["dataset"], paths["splits"], paths["config"])
    message = str(excinfo.value)
    assert excinfo.value.path == str(paths["dataset"])
    assert SNAPSHOT_DIR_PREFIX not in message
    assert str(controlled) not in message

    paths["dataset"].write_bytes(b"record_id,text,entity_id,event_time,label\nrecord-0001,x,g,t,demo-label-a\n")
    report = build_audit(paths["dataset"], paths["splits"], paths["config"]).report
    assert report["inputs"]["dataset"]["path"] == paths["dataset"].as_posix()
    assert report["inputs"]["splits"]["path"] == paths["splits"].as_posix()
    assert report["inputs"]["config"]["path"] == paths["config"].as_posix()


def test_missing_inputs_keep_stable_error_precedence(tmp_path) -> None:
    with pytest.raises(InputError) as first:
        load_inputs(
            tmp_path / "no-dataset.csv",
            tmp_path / "no-splits.csv",
            tmp_path / "no-config.json",
        )
    assert first.value.code == "CONFIG_NOT_FOUND"

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(InputError) as second:
        load_inputs(tmp_path / "no-dataset.csv", tmp_path / "no-splits.csv", config_path)
    assert second.value.code == "DATASET_NOT_FOUND"

    dataset_path = tmp_path / "dataset.csv"
    dataset_path.write_text("record_id\nrecord-0001\n", encoding="utf-8")
    with pytest.raises(InputError) as third:
        load_inputs(dataset_path, tmp_path / "no-splits.csv", config_path)
    assert third.value.code == "SPLITS_NOT_FOUND"
