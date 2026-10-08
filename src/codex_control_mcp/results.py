"""Preserve nested evidence while exposing its actual execution status."""
from __future__ import annotations


def execution_view(data):
    current = data
    for _ in range(12):
        if not isinstance(current, dict):
            return {}
        nested = current.get("structuredContent")
        if not isinstance(nested, dict):
            nested = current.get("result")
        if not isinstance(nested, dict):
            return current
        current = nested
    return current if isinstance(current, dict) else {}


def execution_error(data):
    current = data
    fallback = None
    for _ in range(12):
        if not isinstance(current, dict):
            return fallback
        if isinstance(current.get("error"), dict) and current["error"]:
            return dict(current["error"])
        if current.get("isError") is True or current.get("ok") is False:
            fallback = fallback or {"code": "remote_tool_failed", "message": "The routed tool reported failure; see its original result.",
                                    "retryable": False, "details": {"origin": "routed_tool"}}
        if current.get("exit_code") not in (None, 0):
            code = current["exit_code"]
            error = {"code": "command_failed", "message": "Command returned a nonzero exit code.",
                     "retryable": False, "details": {"origin": "command_process", "exit_code": code}}
            if code == 124:
                error.update(code="command_exit_124", message="命令返回 124，可能达到运行期限，也可能由程序主动返回；请核对生效期限和输出。")
                error["details"].update(effective_timeout_ms=current.get("effective_timeout_ms"), timeout_confirmed=False)
            fallback = error
        current = current.get("structuredContent") if isinstance(current.get("structuredContent"), dict) else current.get("result")
    return fallback


def promote_execution(data):
    view = execution_view(data)
    for key in ("session_id", "origin_operation_id", "runtime_generation", "state", "exit_code", "completed",
                "next_cursor", "has_more", "next_action", "effective_timeout_ms", "elapsed_ms", "status_message",
                "output_truncated", "dropped_output_bytes", "output_truncation", "process_completed",
                "continuation_required", "final_receipt_ready", "output_complete", "result_status",
                "heartbeat", "history_only", "recovery_warning", "read_action"):
        if key in view:
            data.setdefault(key, view[key])
    return data
