"""Read-only WSL readiness; decode native Windows CLI output correctly."""

import ctypes
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]


def decode(data):
    if b"\0" in data[:200]:
        return data.decode("utf-16", "replace").strip()
    return data.decode("utf-8", "replace").strip()


report = {"administrator": bool(ctypes.windll.shell32.IsUserAnAdmin()), "checks": []}
for args in [["--version"], ["--status"], ["--list", "--verbose"]]:
    p = subprocess.run(
        ["wsl.exe", *args],
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    report["checks"].append(
        {
            "args": args,
            "exit_code": p.returncode,
            "stdout": decode(p.stdout),
            "stderr": decode(p.stderr),
        }
    )
(root / "evidence/wsl-readonly-current.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
)
print(json.dumps(report, ensure_ascii=True))
