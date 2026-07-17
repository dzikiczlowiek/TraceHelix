"""Helpers for identifying frames covered by traceback privacy assertions."""

from __future__ import annotations

from pathlib import PureWindowsPath


def is_tracehelix_training_frame(filename: str, module_filename: str | None = None) -> bool:
    """Return whether *filename* identifies a frame in the training package."""
    parts = PureWindowsPath(filename).parts
    if module_filename is None:
        return "tracehelix_training" in parts
    return any(
        package == "tracehelix_training" and child == module_filename
        for package, child in zip(parts, parts[1:], strict=False)
    )
