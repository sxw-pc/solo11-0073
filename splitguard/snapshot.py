"""Per-run immutable input snapshots.

Every audit run first captures the bytes of its three inputs (dataset,
split manifest, config) into a private temporary directory.  Parsers and
the SHA-256 fingerprints both read from these captured bytes, so the
report identity always describes exactly the bytes that were audited —
even if a source path is replaced or overwritten while the run is in
progress.  Path, size and mtime are never used as content identity; the
only fingerprint is the SHA-256 of the captured bytes.

Lifecycle guarantees:

- atomic creation: the run directory is created with
  ``tempfile.mkdtemp`` (mode 0o700) and each snapshot file with
  ``O_CREAT | O_EXCL`` (mode 0o600), so no other process can pre-create
  or observe the snapshot material;
- single directory boundary: all snapshots of a run live together in one
  private directory — never next to the inputs — so leftover material
  can never be picked up as audit input by a later run;
- cleanup on every exit path: the context manager removes the whole
  directory on success, on input errors and on unexpected exceptions
  alike.  A hard kill can only leave a random-named directory inside the
  system temp area, which no later run ever reads.

If the source file is detected to change while its bytes are being
captured (an in-place write would make the snapshot a torn mix of two
versions), the run aborts with a stable ``*_CHANGED_DURING_READ`` input
error instead of auditing mixed old/new content.  The fstat tripwire
only decides this abort; it is not a content fingerprint.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .models import InputError

SNAPSHOT_DIR_PREFIX = "splitguard-inputs-"
_COPY_CHUNK = 1024 * 1024


def _stat_signature(info: os.stat_result) -> tuple[int, int]:
    """Tripwire-only change signal; never used as a content fingerprint."""
    return (info.st_mtime_ns, info.st_size)


def _stream_copy(source, target, path: Path, code: str) -> None:
    """Copy bytes from ``source`` to ``target`` in bounded chunks.

    Source read failures become stable input errors; failures writing to
    our own private snapshot propagate as plain ``OSError`` (an internal
    I/O problem, not a malformed input).
    """
    while True:
        try:
            chunk = source.read(_COPY_CHUNK)
        except OSError as exc:
            raise InputError(
                f"{code}_READ_ERROR", f"cannot read file: {exc}", path=path
            ) from exc
        if not chunk:
            return
        target.write(chunk)


class SnapshotSession:
    """Capture run inputs into one private directory of immutable snapshots."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = None if base_dir is None else str(base_dir)
        self._dir: Path | None = None

    def __enter__(self) -> "SnapshotSession":
        self._dir = Path(
            tempfile.mkdtemp(prefix=SNAPSHOT_DIR_PREFIX, dir=self._base_dir)
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    @property
    def directory(self) -> Path | None:
        """The private snapshot directory, or ``None`` after cleanup."""
        return self._dir

    def cleanup(self) -> None:
        """Remove all snapshot material; safe to call more than once."""
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def capture(self, source: str | Path, kind: str) -> Path:
        """Capture ``source`` into the snapshot directory and return its path.

        ``kind`` is ``"dataset"``, ``"splits"`` or ``"config"`` and selects
        the stable error-code prefix.  The returned path is immutable for
        the rest of the session: parse and hash must both read from it.
        """
        if self._dir is None:
            raise RuntimeError("snapshot session is not active")
        src_path = Path(source)
        code = kind.upper()
        if not src_path.exists():
            raise InputError(
                f"{code}_NOT_FOUND", f"{kind} file does not exist", path=src_path
            )
        if not src_path.is_file():
            raise InputError(
                f"{code}_NOT_A_FILE", f"{kind} path is not a file", path=src_path
            )
        dest = self._dir / f"{kind}.snapshot"
        try:
            src = src_path.open("rb")
        except OSError as exc:
            raise InputError(
                f"{code}_READ_ERROR", f"cannot read file: {exc}", path=src_path
            ) from exc
        with src:
            before = os.fstat(src.fileno())
            # Atomic, private creation: O_EXCL fails rather than following a
            # pre-existing path; the file is 0o600 inside the 0o700 run dir.
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as out:
                    _stream_copy(src, out, src_path, code)
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
            after = os.fstat(src.fileno())
        if _stat_signature(before) != _stat_signature(after):
            # The source was rewritten in place while we copied it, so the
            # snapshot may mix old and new bytes.  Abort with a stable
            # input-changed error instead of auditing a torn mixture.
            dest.unlink(missing_ok=True)
            raise InputError(
                f"{code}_CHANGED_DURING_READ",
                "input changed while it was being captured; "
                "re-run the audit with stable inputs",
                path=src_path,
            )
        return dest
