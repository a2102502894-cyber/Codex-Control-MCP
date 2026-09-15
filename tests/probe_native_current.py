"""Read-only independent bridge probe, with no fabricated runtime metadata."""

import json
from pathlib import Path
import tempfile
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config

root = Path(__file__).resolve().parents[1]
run = Path(tempfile.mkdtemp(prefix="native-", dir=root / "test-workspace"))
bridge = Bridge(Config(home=run / "home", cwd=str(run), computer_use_enabled=True))
try:
    result = bridge.execute("computer_snapshot", {})
    if result.get("result"):
        windows = result["result"].pop("windows", [])
        result["result"]["window_count"] = len(windows)
    report = {
        "scope": "read-only native discovery from an independent bridge; no model turn, window content, input or metadata supplied",
        "result": result,
    }
    (root / "evidence/native-readonly-current.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
finally:
    bridge.close()
