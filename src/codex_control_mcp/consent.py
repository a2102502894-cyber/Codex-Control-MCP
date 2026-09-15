"""Official application access, using the owner's saved choice or a local dialog.
No input injection, upstream policy changes, or automatic form-data submission.
"""

from __future__ import annotations
import ctypes
import os
import threading
from ctypes import wintypes
from .errors import BridgeError

_DIALOG_LOCK = threading.Lock()


def application_access_app(request: dict) -> str | None:
    """Recognize the official empty application-access form, not arbitrary forms."""
    if not isinstance(request, dict) or request.get("mode", "form") != "form":
        return None
    meta = request.get("_meta") or request.get("meta") or {}
    schema = request.get("requestedSchema") or {}
    if not isinstance(meta, dict) or not isinstance(schema, dict):
        return None
    params = meta.get("tool_params") or {}
    if not isinstance(params, dict):
        return None
    app = params.get("app")
    if (
        meta.get("connector_id") != "computer-use"
        or not isinstance(app, str)
        or not 0 < len(app) <= 512
        or not app.strip()
        or "\0" in app
        or schema.get("type", "object") != "object"
        or schema.get("properties", {}) != {}
        or schema.get("required", []) != []
        or set(schema) - {"type", "properties", "required", "additionalProperties", "title", "description", "$schema"}
    ):
        return None
    return app


def owner_preapproved_application_consent(request: dict) -> dict | None:
    """Return the owner's explicit standing application-access decision.

    Call only when auto_approve_application_access is enabled in owner config.
    An unrelated request remains on the normal client/local consent path.
    """
    app = application_access_app(request)
    if app is None:
        return None
    return {
        "action": "accept",
        "content": {},
        "_meta": {"codex_control_mcp": {
            "decision_source": "explicit_owner_preapproval",
            "app": app,
        }},
    }


def resolve_local_application_consent(cfg, request: dict, audit=None) -> dict | None:
    """Use the same saved owner decision for MCP and direct local entry points."""
    if getattr(cfg, "auto_approve_application_access", False):
        decision = owner_preapproved_application_consent(request)
        if decision is not None:
            if audit is not None:
                audit.emit("application_access_decision", action="accept",
                           **decision["_meta"]["codex_control_mcp"])
            return decision
    if getattr(cfg, "local_application_consent", False):
        return native_application_consent(request)
    return None


def native_application_consent(request: dict, timeout_seconds: int = 120) -> dict:
    """Accept only a human Yes click on a narrowly scoped official CUA prompt.
    Unknown requests and missing Windows APIs fail closed. No blanket approvals.
    """
    app = application_access_app(request)
    if os.name != "nt" or app is None:
        return {"action": "cancel"}
    seconds = max(10, min(int(timeout_seconds), 180))
    message = (
        "Codex-Control-MCP 请求你亲自授权\n\n"
        + str(request.get("message", "允许官方 Computer Use 使用以下应用？"))[:1200]
        + "\n\n应用："
        + app
        + "\n\n允许后，外部 GPT 可以通过官方 Codex 对此应用截图、点击和输入。"
        + "\n这不是管理员提权，也不会授权其他应用。"
        + "\n只在你认可当前任务时选择“是”；关闭或超时等同于取消。"
    )
    with _DIALOG_LOCK:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dialog = getattr(user32, "MessageBoxTimeoutW", None)
        if dialog is None:
            raise BridgeError(
                "authorization_required",
                "Native consent dialog is unavailable; use MCP form elicitation.",
            )
        dialog.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.UINT,
            wintypes.WORD,
            wintypes.DWORD,
        ]
        dialog.restype = ctypes.c_int
        # Yes/No, warning, No selected by default, topmost. There is no input injection.
        result = dialog(
            None,
            message,
            "Codex-Control-MCP：应用访问授权",
            0x00000004 | 0x00000030 | 0x00000100 | 0x00040000 | 0x00010000,
            0,
            seconds * 1000,
        )
    return {"action": "accept", "content": {}} if result == 6 else {"action": "cancel"}
