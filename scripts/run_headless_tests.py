"""Run this candidate's tests without console windows or production mutation.

The wrapper changes child-console presentation only. It does not change Windows
security policy, privileges, or the official execution backend. Tests use their
own temporary homes/ports. WSL activation remains outside this Windows-only
profile; OAuth and protocol fault regressions are included.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["PYTHONPATH"] = str(ROOT / "src")
os.environ["PYTHONIOENCODING"] = "utf-8"


def main():
    import pytest
    import codex_control_mcp

    if (
        Path(codex_control_mcp.__file__).resolve().parent
        != ROOT / "src/codex_control_mcp"
    ):
        raise RuntimeError("Refusing to test a different source tree")
    started = time.time()
    observed = []
    original = subprocess.Popen.__init__

    def no_console(self, *args, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        command = args[0] if args else kwargs.get("args", [])
        executable = (
            str(command[0])
            if isinstance(command, (tuple, list)) and command
            else "<structured-command>"
        )
        if Path(executable).name.lower() in {"wsl.exe", "shutdown.exe"}:
            raise RuntimeError(
                "This headless test scope excludes WSL activation and Windows restart"
            )
        observed.append(
            {
                "executable_name": Path(executable).name,
                "no_console_window": bool(
                    kwargs.get("creationflags", 0)
                    & getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            }
        )
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = no_console
    arguments = sys.argv[1:] or [
        "tests",
        "-q",
        "--tb=short",
        "-k",
        "not test_wsl_availability_is_reported_not_assumed",
        "--junitxml=evidence/headless-regression.xml",
    ]
    try:
        result = int(pytest.main(arguments))
    finally:
        subprocess.Popen.__init__ = original
    report = {
        "started_at": started,
        "finished_at": time.time(),
        "pytest_exit_code": result,
        "source": str(ROOT),
        "subprocess_starts": len(observed),
        "all_observed_children_no_console_window": all(
            x["no_console_window"] for x in observed
        ),
        "spawn_summary": observed,
        "scope_exclusions": [
            "WSL activation",
            "production stop/restart",
            "desktop GUI/clipboard actions (dedicated headless Tabbit is tested)",
        ],
        "production_service_actions_requested": False,
        "windows_restart_requested": False,
    }
    (ROOT / "evidence/headless-runner.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {
                "headless_runner": report["all_observed_children_no_console_window"],
                "subprocess_starts": len(observed),
                "exit_code": result,
            }
        )
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
