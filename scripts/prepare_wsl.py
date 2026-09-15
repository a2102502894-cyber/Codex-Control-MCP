"""Install Microsoft's WSL package; never reboot or alter an existing distro."""

import ctypes
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
if not ctypes.windll.shell32.IsUserAnAdmin():
    raise SystemExit("An existing administrator terminal is required.")
args = ["wsl.exe", "--install", "--no-distribution", "--web-download"]
record = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "args": args,
    "reboot_requested": False,
    "distributions_changed": False,
}


def decode(data):
    return data.decode("utf-16" if b"\0" in data[:200] else "utf-8", "replace").strip()


try:
    p = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    record.update(
        exit_code=p.returncode, stdout=decode(p.stdout), stderr=decode(p.stderr)
    )
except subprocess.TimeoutExpired:
    record["error"] = (
        "Official WSL installation exceeded the bounded wait; inspect status before retrying."
    )
record["finished_at"] = datetime.now(timezone.utc).isoformat()
(root / "evidence/wsl-preparation.json").write_text(
    json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
)
print(json.dumps(record, ensure_ascii=True))
