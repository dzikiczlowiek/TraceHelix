from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tracehelix_training import package_name


def test_package_name() -> None:
    assert package_name() == "tracehelix-training"


def test_production_wheel_installs_and_imports_contracts_without_dev_group(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    wheel_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(project)], check=True
    )
    wheel = next(wheel_dir.glob("*.whl"))
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True)
    subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; "
            "assert any(r.startswith('jsonschema==4.26.0') "
            "for r in m.requires('tracehelix-training')); "
            "import tracehelix_training.contracts",
        ],
        cwd=tmp_path,
        check=True,
    )
