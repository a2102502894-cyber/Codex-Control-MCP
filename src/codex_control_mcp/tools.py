"""Fixed MCP contracts; new upstream RPCs are never exposed automatically."""

import json
from jsonschema import Draft7Validator
from .errors import BridgeError

S = {"type": "string"}
B = {"type": "boolean"}


def integer(lo=0, hi=1000000):
    return {"type": "integer", "minimum": lo, "maximum": hi}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(item=S, min_items=0, max_items=1000):
    return {
        "type": "array",
        "items": item,
        "minItems": min_items,
        "maxItems": max_items,
    }


def definition(description, properties, required=(), read=False):
    return {
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                **properties,
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": list(required),
            "additionalProperties": False,
        },
        "readOnlyHint": read,
    }


EXEC = {
    "command": {"type": "string", "minLength": 1},
    "argv": array(S, 1, 1024),
    "cwd": S,
    "shell": enum("powershell", "cmd", "wsl"),
    "wsl_distribution": S,
    "wsl_cwd": S,
    "env": {"type": "object", "additionalProperties": {"type": ["string", "null"]}},
    "timeout_ms": integer(1, 86400000),
    "output_limit_bytes": integer(1024, 4194304),
    "execution_mode": enum("buffered", "session"),
    "tty": B,
}
SESSION = {**EXEC, "timeout_ms": integer(0, 86400000)}
TOOL_SPECS = {
    "codex_health": definition(
        "检查实际官方 Runtime、Schema、权限及能力。active 执行探针；refresh 重新导出 Schema。",
        {"active": B, "refresh": B},
        read=True,
    ),
    "codex_capabilities": definition(
        "返回真实安装、已验证能力、实验能力和不可用原因。", {}, read=True
    ),
    "exec_command": definition(
        "通过官方 command/exec 执行调用方提供的 command 或 argv，使用 dangerFullAccess，无工作区沙箱。权限与服务进程相同；命令可能修改文件、运行程序或访问网络。返回退出码、stdout、stderr 及执行后端。",
        EXEC,
    ),
    "session_start": definition(
        "通过官方执行层启动流式长任务。timeout_ms=0 明确禁用底层期限。", SESSION
    ),
    "session_read": definition(
        "按游标读取本桥拥有的会话输出，报告截断和数据缺口。",
        {"session_id": S, "cursor": integer(0, 9007199254740991), "max_bytes": integer(1024, 4194304)},
        ["session_id"],
        True,
    ),
    "session_write": definition(
        "向本桥拥有的会话写入 UTF-8；未知执行结果不会自动重发。",
        {"session_id": S, "text": S, "close_stdin": B},
        ["session_id"],
    ),
    "session_kill": definition(
        "请求官方终止本桥拥有的会话，随后读取状态确认退出。",
        {"session_id": S},
        ["session_id"],
    ),
    "session_resize": definition(
        "调整本桥官方 PTY 会话的字符网格。",
        {"session_id": S, "rows": integer(1, 1000), "cols": integer(1, 1000)},
        ["session_id", "rows", "cols"],
    ),
    "session_list": definition(
        "仅列本桥当前和历史会话，不枚举 Desktop 私有线程。", {}, read=True
    ),
    "read_file": definition(
        "经官方执行层有界读取文件；返回内容 SHA-256、行范围、截断及续读位置。",
        {
            "path": S,
            "start_line": integer(1),
            "end_line": integer(1),
            "max_lines": integer(1, 100000),
            "max_bytes": integer(1024, 4194304),
            "encoding": S,
        },
        ["path"],
        True,
    ),
    "list_dir": definition(
        "通过官方 fs/readDirectory 分页列出目录，不扫描整盘。",
        {"path": S, "offset": integer(), "limit": integer(1, 5000)},
        ["path"],
        True,
    ),
    "search_files": definition(
        "通过官方执行层复用已安装或官方捆绑的 ripgrep 搜索文件。",
        {
            "path": S,
            "glob": S,
            "max_depth": integer(1, 20),
            "max_results": integer(1, 1000),
        },
        ["path"],
        True,
    ),
    "search_text": definition(
        "通过官方执行层 ripgrep 有界搜索文本，默认字面匹配。",
        {
            "path": S,
            "query": S,
            "glob": S,
            "regex": B,
            "max_depth": integer(1, 20),
            "max_results": integer(1, 1000),
        },
        ["path", "query"],
        True,
    ),
    "file_add": definition(
        "通过官方 fs/writeFile 写入临时文件，再原子拒绝覆盖地移动到目标并回读校验。",
        {"path": S, "content": S},
        ["path", "content"],
    ),
    "file_move": definition(
        "通过官方执行层移动文件，不覆盖目标，不承诺跨卷原子性。",
        {"source": S, "destination": S},
        ["source", "destination"],
    ),
    "file_delete": definition(
        "通过官方 fs/remove 删除明确路径；默认不递归、缺失时报错。",
        {"path": S, "recursive": B, "force": B},
        ["path"],
    ),
    "file_patch": definition(
        "通过官方 command/exec 复用 git apply；先检查补丁和基础 SHA，不冒充原生 Codex Patch。",
        {
            "cwd": S,
            "patch": S,
            "expected_sha256": {
                "type": "object",
                "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        ["patch"],
    ),
    "git_status": definition("读取指定仓库工作区与暂存区状态。", {"cwd": S}, read=True),
    "git_diff": definition(
        "读取 Git 差异，禁用外部 diff 和 textconv。",
        {"cwd": S, "staged": B, "paths": array()},
        read=True,
    ),
    "git_log": definition(
        "读取有限条 Git 提交记录。", {"cwd": S, "limit": integer(1, 200)}, read=True
    ),
    "git_branch": definition(
        "列出、创建或切换分支，不删除分支、不重写历史。",
        {"cwd": S, "action": enum("list", "create", "switch"), "name": S},
    ),
    "git_commit": definition(
        "仅提交明确文件，不包含其他已暂存文件；不 push、不 amend。",
        {
            "cwd": S,
            "message": {"type": "string", "minLength": 1},
            "paths": array(S, 1, 1000),
        },
        ["message", "paths"],
    ),
    "remote_status": definition(
        "读取本桥专属 App Server 的 Remote 状态，不推断 Desktop 或手机在线。",
        {},
        read=True,
    ),
    "task_manage": definition(
        "持久化可恢复任务状态：目标、步骤、检查点、阻塞原因、恢复、最终复核和完成条件。",
        {
            "action": enum("create", "list", "get", "checkpoint", "block", "resume", "final_review", "complete"),
            "title": {"type": "string", "minLength": 1, "maxLength": 2048},
            "goal": {"type": "string", "minLength": 1, "maxLength": 16384},
            "project": S,
            "host": S,
            "completion_conditions": array(S, 0, 64),
            "steps": array({"type": "object", "properties": {"id": S, "title": {"type": "string", "minLength": 1}}, "required": ["title"], "additionalProperties": False}, 0, 128),
            "status": enum("active", "blocked", "completed"),
            "limit": integer(1, 200),
            "task_id": S,
            "step_id": S,
            "step_status": enum("pending", "in_progress", "completed"),
            "completed_step_ids": array(S, 0, 128),
            "current_step_id": S,
            "summary": S,
            "evidence": array(S, 0, 64),
            "review_status": enum("pass", "failed"),
            "verified": array(S, 0, 64),
            "risks": array(S, 0, 64),
            "missing_checks": array(S, 0, 64),
            "expected_revision": integer(1, 2147483647),
        },
        ["action"],
    ),
    "mcp_manage": definition(
        "管理独立动态 MCP：注册、查看、更新、启停、刷新或删除。第三方工具不会注入 Core 顶层工具表。",
        {
            "action": enum("register", "list", "get", "update", "enable", "disable", "refresh", "remove"),
            "name": S,
            "description": S,
            "transport": enum("streamable_http", "stdio"),
            "url": S,
            "command": S,
            "args": array(S, 0, 256),
            "cwd": S,
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "header_files": {"type": "object", "additionalProperties": {"type": "string"}},
            "header_prefixes": {"type": "object", "additionalProperties": {"type": "string"}},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "env_from_env": {"type": "object", "additionalProperties": {"type": "string"}},
            "env_files": {"type": "object", "additionalProperties": {"type": "string"}},
            "enabled": B,
            "timeout_ms": integer(1000, 300000),
        },
        ["action"],
    ),
    "mcp_tool_search": definition(
        "搜索已启用动态 MCP 的轻量工具摘要；返回 <server>:<tool> 限定名。",
        {"query": S, "server": S, "limit": integer(1, 100)},
        read=True,
    ),
    "mcp_tool_inspect": definition(
        "读取一个动态 MCP 工具的完整 schema，名称格式为 <server>:<tool>。",
        {"name": {"type": "string", "minLength": 3}},
        ["name"],
        True,
    ),
    "mcp_tool_call": definition(
        "调用已发现的独立 MCP 工具；先按缓存 schema 校验 arguments，再转发到目标 MCP。",
        {"name": {"type": "string", "minLength": 3}, "arguments": {"type": "object", "additionalProperties": True}},
        ["name", "arguments"],
    ),
    "host_manage": definition(
        "管理命名主机：local 内建；可注册远端 Codex-Control-MCP 节点、SSH 主机或 Docker 容器。",
        {
            "action": enum("register", "update", "list", "get", "status", "enable", "disable", "remove"),
            "name": S,
            "transport": enum("mcp", "ssh", "docker"),
            "description": S,
            "enabled": B,
            "platform": enum("windows", "linux", "macos"),
            "mcp_server": S,
            "address": S,
            "user": S,
            "port": integer(1, 65535),
            "identity_file": S,
            "container": S,
        },
        ["action"],
    ),
    "host_exec": definition(
        "在命名主机执行命令。local 复用官方 Codex Runtime；MCP 节点复用远端 exec_command；SSH/Docker 使用宿主执行层路由。",
        {**EXEC, "host": {"type": "string", "minLength": 1}},
        ["host"],
    ),
    "host_files": definition(
        "对命名主机执行 read/list/write/delete/search 文件操作；MCP 节点复用远端结构化文件工具。",
        {
            "host": {"type": "string", "minLength": 1},
            "action": enum("read", "list", "write", "delete", "search"),
            "path": {"type": "string", "minLength": 1},
            "content": S,
            "query": S,
            "recursive": B,
            "force": B,
            "offset": integer(0, 1000000),
            "limit": integer(1, 5000),
            "max_lines": integer(1, 100000),
            "max_bytes": integer(1024, 4194304),
            "encoding": S,
            "glob": S,
            "regex": B,
            "max_depth": integer(1, 20),
            "max_results": integer(1, 1000),
        },
        ["host", "action", "path"],
    ),
    "host_route": definition(
        "只读解释某个命名主机针对指定操作会走 local、dynamic MCP、SSH 还是 Docker。",
        {"host": {"type": "string", "minLength": 1}, "operation": enum("exec", "read", "list", "write", "delete", "search")},
        ["host", "operation"],
        True,
    ),
    "skill_package": definition(
        "管理版本化 Skill 包：校验、安装、激活、回滚、卸载、列出或读取。支持本地目录、zip 和 Git URL。",
        {
            "action": enum("validate", "install", "activate", "rollback", "uninstall", "list", "inspect"),
            "source": S,
            "skill": S,
            "version": S,
            "digest": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
            "channel": enum("stable", "development", "canary", "pinned"),
            "activate": B,
            "max_bytes": integer(1024, 67108864),
        },
        ["action"],
    ),
}
GUI_SPECS = {
    "browser_start": (
        {"url": S, "browser_id": S, "discover_only": B, "screenshot": B},
        (),
    ),
    "browser_snapshot": ({"page_id": S, "screenshot": B}, ("page_id",)),
    "browser_click": (
        {
            "page_id": S,
            "element_index": integer(),
            "x": {"type": "number"},
            "y": {"type": "number"},
            "snapshot_id": S,
            "screenshot": B,
        },
        ("page_id", "snapshot_id"),
    ),
    "browser_fill": (
        {
            "page_id": S,
            "element_index": integer(),
            "text": S,
            "snapshot_id": S,
            "screenshot": B,
        },
        ("page_id", "element_index", "text", "snapshot_id"),
    ),
    "browser_press": (
        {"page_id": S, "key": S, "snapshot_id": S, "screenshot": B},
        ("page_id", "key", "snapshot_id"),
    ),
    "browser_scroll": (
        {
            "page_id": S,
            "element_index": integer(),
            "x": {"type": "number"},
            "y": {"type": "number"},
            "direction": enum("up", "down", "left", "right"),
            "pages": {"type": "number", "minimum": 0.1, "maximum": 10},
            "snapshot_id": S,
            "screenshot": B,
        },
        ("page_id", "direction", "snapshot_id"),
    ),
    "browser_navigate": ({"page_id": S, "url": S, "screenshot": B}, ("page_id", "url")),
    "browser_close": ({"page_id": S}, ("page_id",)),
    "computer_snapshot": ({"window_id": integer(1, 2**53 - 1), "include_text": B}, ()),
    "computer_click": (
        {
            "snapshot_id": S,
            "x": {"type": "number"},
            "y": {"type": "number"},
            "element_index": integer(),
        },
        ("snapshot_id",),
    ),
    "computer_type": ({"snapshot_id": S, "text": S}, ("snapshot_id", "text")),
    "computer_press": ({"snapshot_id": S, "key": S}, ("snapshot_id", "key")),
    "computer_scroll": (
        {
            "snapshot_id": S,
            "x": {"type": "number"},
            "y": {"type": "number"},
            "delta_x": {"type": "number"},
            "delta_y": {"type": "number"},
        },
        ("snapshot_id", "x", "y", "delta_y"),
    ),
    "computer_wait": ({"milliseconds": integer(1, 10000)}, ("milliseconds",)),
}
for name, (props, req) in GUI_SPECS.items():
    TOOL_SPECS[name] = definition(
        ("通过配置的 Browser 后端控制本项目专属浏览器页面。Tabbit 后端使用 Playwright，不调用浏览器内置 AI。先 browser_start，再从 browser_snapshot 返回的 elements 选 element_index；点击、输入、按键、滚动必须提交该页最新 snapshot_id，操作后旧快照失效。坐标单位为视口 CSS 像素；screenshot=true 返回页面截图。browser_close 只关闭本桥页面。"
         if name.startswith('browser_') else
         "官方 Computer Use 桌面适配器。必须使用新鲜窗口快照和已获得授权的官方直接执行路径。"),
        props,
        req,
        name.endswith("_snapshot"),
    )
VALIDATORS = {
    name: Draft7Validator(spec["inputSchema"]) for name, spec in TOOL_SPECS.items()
}


def validate_tool(name, args):
    validator = VALIDATORS.get(name)
    if validator is None:
        raise BridgeError("capability_unavailable", "Unknown MCP tool.")
    try:
        # Escaped lone surrogates are accepted by Python's JSON decoder but
        # cannot cross the official UTF-8/native tool boundary reliably.
        json.dumps(args, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (UnicodeError, ValueError, TypeError) as exc:
        raise BridgeError(
            'invalid_arguments',
            'Input must contain finite JSON values and valid Unicode text.',
        ) from exc
    error = next(validator.iter_errors(args), None)
    if error:
        raise BridgeError(
            "invalid_arguments",
            f"Input rejected at {list(error.absolute_path)} (rule={error.validator}); values are not logged.",
        )
