"""Native Windows maintenance requests; no PowerShell or policy changes.

The existing, independently scheduled recovery controller performs the restart.
This module only checks that task and atomically submits a bounded request.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
import uuid

from .common import InstanceLock
from .errors import BridgeError

CONTROLLER_TASK = "Codex-Control-MCP-Core-Controller"


class MaintenanceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def controller_task():
    if sys.platform != "win32":
        raise MaintenanceError("windows_required", "维护入口仅支持 Windows。")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        task = scheduler.GetFolder("\\").GetTask(CONTROLLER_TASK)
        if not task.Enabled:
            raise MaintenanceError("controller_disabled", "维护控制器未启用；未提交重启请求。")
        return task
    except MaintenanceError:
        raise
    except Exception as exc:
        raise MaintenanceError("controller_unavailable", "无法访问已安装的维护控制器；未修改系统策略。") from exc


def validate_home(home: Path) -> Path:
    home = home.expanduser().resolve()
    if not home.is_dir() or not (home / "state" / "core-controller-config.json").is_file():
        raise MaintenanceError("controller_not_installed", "此目录未安装维护控制器，请先完成正式安装。")
    try:
        config = json.loads((home / "state" / "core-controller-config.json").read_text("utf-8-sig"))
        configured_home = Path(config["home"]).expanduser().resolve()
        configured_queue = Path(config["request"]).expanduser().resolve()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise MaintenanceError("invalid_controller_config", "维护控制器配置不可读或不完整。") from exc
    if configured_home != home or configured_queue != home / "state" / "core-controller-requests":
        raise MaintenanceError("controller_home_mismatch", "控制器配置与目标目录不匹配，已停止。")
    return home


def submit_request(home: Path, task: Any, *, check_only: bool = False) -> dict:
    home = validate_home(home)
    if not task.Enabled:
        raise MaintenanceError("controller_disabled", "维护控制器未启用。")
    base = {
        "ok": True,
        "controller_task": CONTROLLER_TASK,
        "entrypoint": "native_windows_task_scheduler",
        "powershell_invoked": False,
        "execution_policy_changed": False,
        "platform_safety_settings_changed": False,
    }
    if check_only:
        return {**base, "check_only": True, "request_created": False,
                "controller_state": int(task.State), "message": "原生维护入口检查通过，未重启服务。"}
    # A running controller may already have consumed its queue. Do not leave a
    # request which a later watchdog could unexpectedly interpret as a restart.
    if int(task.State) in (2, 4):
        raise MaintenanceError("controller_busy", "维护控制器正在运行，未重复提交请求。")
    # Serialize competing submitters across threads AND separate CLI processes.
    guard = InstanceLock(home / "state" / "maintenance-submit.guard")
    try:
        guard.acquire()
    except BridgeError as exc:
        raise MaintenanceError("controller_busy", "另一个维护请求正在提交，未重复执行。") from exc
    try:
        if int(task.State) in (2, 4):
            raise MaintenanceError("controller_busy", "维护控制器正在运行，未重复提交请求。")
        queue = home / "state" / "core-controller-requests"
        queue.mkdir(exist_ok=True)
        if next(queue.glob("*.json"), None) is not None:
            raise MaintenanceError("pending_request_exists", "已有维护请求尚未处理，请先核对回执；未叠加重启。")
        request_id = uuid.uuid4().hex
        target = queue / (request_id + ".json")
        stage = queue / (request_id + ".tmp")
        record = {"schema": 1, "operation": "restart", "request_id": request_id,
                  "requested_by_pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with stage.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage, target)
            try:
                task.Run("")
            except Exception as exc:
                # Remove only our unclaimed request. If a controller has claimed it,
                # completion is unknown and must be checked, never blindly retried.
                withdrawn = queue / (request_id + ".withdrawn")
                try:
                    os.replace(target, withdrawn)
                except FileNotFoundError:
                    code = "restart_state_unknown"
                else:
                    withdrawn.unlink(missing_ok=True)
                    code = "controller_start_failed"
                raise MaintenanceError(code, "维护启动未确认，请核对控制器回执；未自动重试。") from exc
        finally:
            stage.unlink(missing_ok=True)
        return {**base, "accepted": True, "request_created": True, "request_id": request_id,
                "restart_completed": False,
                "report_path": str(home / "state" / "core-controller-report.json"),
                "message": "重启请求已提交。请按请求编号核对控制器完成回执，提交不等于重启成功。"}
    finally:
        guard.close()



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex-Control-MCP 原生维护入口，不修改 PowerShell 策略。")
    parser.add_argument("--home", type=Path, default=Path.home() / ".codex-control-mcp")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查入口，不重启（默认）")
    mode.add_argument("--restart", action="store_true", help="提交一次正式服务重启请求")
    args = parser.parse_args(argv)
    try:
        validate_home(args.home)
        task = controller_task()
        result = submit_request(args.home, task, check_only=not args.restart)
    except MaintenanceError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": str(exc)},
                  "execution_policy_changed": False, "automatic_retry_performed": False}
    except Exception as exc:
        result = {"ok": False, "error": {"code": "maintenance_failed", "type": type(exc).__name__,
                  "message": "维护失败，请检查本机权限及控制器状态。"}, "automatic_retry_performed": False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
