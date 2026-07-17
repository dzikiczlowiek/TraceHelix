from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

from tracehelix_training import package_name


def test_package_name() -> None:
    assert package_name() == "tracehelix-training"


def test_production_wheel_installs_and_imports_contracts_without_dev_group(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    wheel_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(wheel_dir), str(project)],
        check=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        policy_files = [name for name in archive.namelist() if name.endswith("/redaction-v1.json")]
    assert policy_files == ["tracehelix_training/redaction-v1.json"]
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--offline", "--python", str(python), str(wheel)],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; "
            "assert any(r.startswith('jsonschema==4.26.0') "
            "for r in m.requires('tracehelix-training')); "
            "import tracehelix_training.contracts; "
            "from tracehelix_training.redact import load_default_config, redact; "
            "cfg = load_default_config(); "
            "out, report = redact('owner@example.test', cfg); "
            "assert out == '<REDACTED:EMAIL:1>'; "
            "assert report.version == 'redaction-v1'; "
            "from tracehelix_training.contracts import construct_candidate; "
            "raw = dict(schema_version='1.0.0', run_id='r', event_id='e', "
            "task_group_id='t', lineage_id='l', context_before=[], "
            "event_text='Bearer synthetic-token', context_after=[], "
            "event_kind='message', source_category='fixture', source_hash='a'*64, "
            "adapter='a', adapter_version='1', redaction_version='redaction-v1', "
            "license_or_consent='fixture'); "
            "assert '<REDACTED:AUTH:1>' in construct_candidate(**raw).event_text",
        ],
        cwd=tmp_path,
        check=True,
    )
