"""Keep all test-owned child processes in the background on Windows."""

import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def background_test_processes():
    original = subprocess.Popen
    children = []
    source = str(Path(__file__).resolve().parents[1] / "src")
    old_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [source, old_path]))

    def start(*args, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        proc = original(*args, **kwargs)
        children.append(
            {
                "pid": proc.pid,
                "no_console_window": bool(
                    kwargs.get("creationflags", 0)
                    & getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            }
        )
        return proc

    subprocess.Popen = start
    try:
        yield
    finally:
        subprocess.Popen = original
        if old_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_path
        evidence = Path(__file__).resolve().parents[1] / "evidence"
        evidence.mkdir(exist_ok=True)
        profile = os.environ.get('CCM_TEST_PROFILE')
        assert profile in (None, 'standard', 'admin')
        name = 'children-' + profile + '.json' if profile else 'test-child-processes.json'
        (evidence / name).write_text(
            json.dumps(children, indent=2), encoding="utf-8"
        )
