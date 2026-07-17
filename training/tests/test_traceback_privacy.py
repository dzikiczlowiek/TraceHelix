from __future__ import annotations

import pytest

from traceback_privacy import is_tracehelix_training_frame


@pytest.mark.parametrize(
    ("filename", "module_filename"),
    [
        (r"C:\work\TraceHelix\training\src\tracehelix_training\contracts.py", None),
        (r"C:\work\TraceHelix\training\src\tracehelix_training\redact.py", "redact.py"),
        ("/work/TraceHelix/training/src/tracehelix_training/contracts.py", None),
        ("/work/TraceHelix/training/src/tracehelix_training/redact.py", "redact.py"),
    ],
)
def test_windows_paths_identify_privacy_checked_frames(
    filename: str, module_filename: str | None
) -> None:
    assert is_tracehelix_training_frame(filename, module_filename)


@pytest.mark.parametrize(
    ("filename", "module_filename"),
    [
        (r"C:\work\tracehelix_training_backup\contracts.py", None),
        (r"C:\work\tracehelix_training\contracts.py", "redact.py"),
        ("/work/tracehelix_training_backup/redact.py", "redact.py"),
    ],
)
def test_unrelated_frames_are_not_privacy_checked(
    filename: str, module_filename: str | None
) -> None:
    assert not is_tracehelix_training_frame(filename, module_filename)
