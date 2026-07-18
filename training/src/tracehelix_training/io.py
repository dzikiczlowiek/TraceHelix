"""Canonical, crash-safe JSONL output helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Iterable

from tracehelix_training.contracts import TrainingCandidate, canonical_json_bytes


class ExportError(Exception):
    """A public, input-private export failure."""


def atomic_write_candidates(path: Path, rows: Iterable[TrainingCandidate]) -> int:
    """Write canonical JSONL; ``os.replace`` is the successful commit point."""
    parent = path.parent
    try:
        final_status = path.lstat()
    except FileNotFoundError:
        final_status = None
    except OSError:
        raise ExportError("unsafe output path") from None
    # lstat is intentional: never follow or replace an existing symlink/reparse point.
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    is_reparse_point = final_status is not None and bool(
        getattr(final_status, "st_file_attributes", 0) & reparse_attribute
    )
    if (
        not parent.is_dir()
        or is_reparse_point
        or final_status is not None
        and not stat.S_ISREG(final_status.st_mode)
    ):
        raise ExportError("unsafe output path")
    descriptor = -1
    directory_fd = -1
    temporary: Path | None = None
    count = 0
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        temporary = Path(name)
        os.chmod(name, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            for row in rows:
                stream.write(canonical_json_bytes(row))
                stream.write(b"\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
        os.replace(temporary, path)
        temporary = None
        # The candidate is committed. Directory durability is best effort and must
        # not make callers believe that the now-published output was not written.
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            try:
                os.close(directory_fd)
            except OSError:
                pass
            directory_fd = -1
        return count
    except ExportError:
        raise
    except Exception:
        raise ExportError("candidate export failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
