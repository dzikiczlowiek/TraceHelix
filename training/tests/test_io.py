import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracehelix_training.io import ExportError, atomic_write_candidates


def test_directory_open_failure_is_precommit_and_preserves_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "candidates.jsonl"
    output.write_bytes(b"old\n")
    real_open = os.open

    def fail_directory_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == tmp_path:
            raise OSError("injected directory open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_directory_open)
    with pytest.raises(ExportError, match="candidate export failed"):
        atomic_write_candidates(output, [])

    assert output.read_bytes() == b"old\n"
    assert list(tmp_path.glob(".candidates.jsonl.*.tmp")) == []


def test_directory_fsync_failure_after_commit_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "candidates.jsonl"
    output.write_bytes(b"old\n")
    real_fsync = os.fsync
    calls = 0

    def fail_second_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_fsync)
    assert atomic_write_candidates(output, []) == 0
    assert output.read_bytes() == b""
    assert list(tmp_path.glob(".candidates.jsonl.*.tmp")) == []


def test_directory_close_failure_after_commit_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "candidates.jsonl"
    output.write_bytes(b"old\n")
    real_open = os.open
    real_close = os.close
    directory_fd = -1

    def capture_directory_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal directory_fd
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == tmp_path:
            directory_fd = descriptor
        return descriptor

    def fail_directory_close(fd: int) -> None:
        if fd == directory_fd:
            real_close(fd)
            raise OSError("injected directory close failure")
        real_close(fd)

    monkeypatch.setattr(os, "open", capture_directory_open)
    monkeypatch.setattr(os, "close", fail_directory_close)
    assert atomic_write_candidates(output, []) == 0
    assert output.read_bytes() == b""


def test_windows_reparse_point_is_rejected_before_temporary_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "candidates.jsonl"
    output.write_bytes(b"old\n")
    real_lstat = Path.lstat
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def fake_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        if path == output:
            # These are the actual Windows stat fields: a reparse point can also
            # report a regular-file st_mode when it is not a symlink.
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_file_attributes=reparse_attribute,
            )
        return real_lstat(path)

    temporary_attempted = False

    def forbidden_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal temporary_attempted
        temporary_attempted = True
        raise AssertionError("temporary output must not be created")

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(tempfile, "mkstemp", forbidden_mkstemp)
    with pytest.raises(ExportError, match="unsafe output path"):
        atomic_write_candidates(output, [])

    assert not temporary_attempted
    assert output.read_bytes() == b"old\n"
