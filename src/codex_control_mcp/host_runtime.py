from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shlex

from .common import atomic_json, ps_quote
from .errors import BridgeError
from .results import execution_view


class HostManager:
    """Named host registry with local, MCP-node, SSH, and Docker routing."""

    def __init__(self, bridge, dynamic_mcp):
        self.bridge = bridge
        self.dynamic_mcp = dynamic_mcp
        self.path = pathlib.Path(bridge.cfg.home) / "state" / "hosts.json"
        self.hosts = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("host_registry_corrupt", "Host registry cannot be read.") from exc
        return data if isinstance(data, dict) else {}

    def _save(self):
        atomic_json(self.path, self.hosts)

    def _name(self, value, allow_local=True):
        value = str(value or "").strip()
        if not value:
            raise BridgeError("invalid_arguments", "host is required.")
        if len(value) > 64 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in value):
            raise BridgeError("invalid_arguments", "Host name contains unsupported characters.")
        if not allow_local and value.lower() == "local":
            raise BridgeError("invalid_arguments", "local is a reserved built-in host.")
        return value

    def _local(self):
        return {"name": "local", "transport": "local", "enabled": True, "description": "This Codex-Control-MCP host"}

    def _public(self, item):
        return {k: v for k, v in item.items() if k not in {"secret", "password"}}

    def _get(self, name):
        name = self._name(name)
        if name.lower() == "local":
            return self._local()
        item = self.hosts.get(name)
        if not item:
            raise BridgeError("host_not_found", "Host was not found.")
        if not item.get("enabled", True):
            raise BridgeError("host_disabled", "Host is disabled.")
        return item

    def manage(self, args):
        action = str(args.get("action") or "").strip().lower()
        if action == "list":
            items = [self._local()] + [self._public(v) for _, v in sorted(self.hosts.items())]
            return {"action": action, "hosts": items, "count": len(items), "registry": str(self.path)}
        if action in {"register", "update"}:
            name = self._name(args.get("name"), False)
            if action == "register" and name in self.hosts:
                raise BridgeError("host_exists", "Host already exists.")
            if action == "update" and name not in self.hosts:
                raise BridgeError("host_not_found", "Host was not found.")
            old = dict(self.hosts.get(name) or {})
            transport = str(args.get("transport") or old.get("transport") or "").strip().lower()
            if transport not in {"mcp", "ssh", "docker"}:
                raise BridgeError("invalid_arguments", "transport must be mcp, ssh, or docker.")
            item = {
                "name": name,
                "transport": transport,
                "description": str(args.get("description", old.get("description", "")) or "").strip(),
                "enabled": bool(args.get("enabled", old.get("enabled", True))),
                "platform": str(args.get("platform", old.get("platform", "linux")) or "linux").strip().lower(),
            }
            if item["platform"] not in {"windows", "linux", "macos"}:
                raise BridgeError("invalid_arguments", "platform must be windows, linux, or macos.")
            if transport == "mcp":
                item["mcp_server"] = str(args.get("mcp_server") or old.get("mcp_server") or "").strip()
                if not item["mcp_server"]:
                    raise BridgeError("invalid_arguments", "mcp transport requires mcp_server.")
            elif transport == "ssh":
                item["address"] = str(args.get("address") or old.get("address") or "").strip()
                item["user"] = str(args.get("user") or old.get("user") or "").strip()
                item["port"] = int(args.get("port") or old.get("port") or 22)
                item["identity_file"] = str(args.get("identity_file", old.get("identity_file", "")) or "").strip()
                if not item["address"] or not item["user"] or not 1 <= item["port"] <= 65535:
                    raise BridgeError("invalid_arguments", "ssh requires address, user, and a valid port.")
            else:
                item["container"] = str(args.get("container") or old.get("container") or "").strip()
                if not item["container"]:
                    raise BridgeError("invalid_arguments", "docker transport requires container.")
            self.hosts[name] = item
            self._save()
            return {"action": action, "host": self._public(item), "registry": str(self.path)}
        if action == "get":
            return {"action": action, "host": self._public(self._get(args.get("name"))), "registry": str(self.path)}
        if action in {"enable", "disable"}:
            name = self._name(args.get("name"), False)
            if name not in self.hosts:
                raise BridgeError("host_not_found", "Host was not found.")
            self.hosts[name]["enabled"] = action == "enable"
            self._save()
            return {"action": action, "host": self._public(self.hosts[name]), "registry": str(self.path)}
        if action == "remove":
            name = self._name(args.get("name"), False)
            item = self.hosts.pop(name, None)
            if not item:
                raise BridgeError("host_not_found", "Host was not found.")
            self._save()
            return {"action": action, "removed": self._public(item), "registry": str(self.path)}
        if action == "status":
            return {"action": action, **self.status(args.get("name"))}
        raise BridgeError("invalid_arguments", "Unsupported host_manage action.", details={"allowed": ["register", "update", "list", "get", "status", "enable", "disable", "remove"]})

    def _ssh_prefix(self, item):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(item["port"])]
        if item.get("identity_file"):
            argv += ["-i", item["identity_file"]]
        argv.append(f"{item['user']}@{item['address']}")
        return argv

    def _remote_script(self, item, command, options):
        windows = item.get("platform") == "windows"
        shell = options.get("shell")
        if shell is not None and not (windows and shell == "powershell"):
            raise BridgeError("invalid_arguments", "This shell route supports PowerShell on Windows and POSIX sh on Linux/macOS; omit shell for its native default.")
        if options.get("tty") or options.get("wsl_distribution") or options.get("wsl_cwd"):
            raise BridgeError("invalid_arguments", "TTY and WSL options are not supported by this remote shell route.")
        stop = options.get("shell_error_policy", "stop") == "stop"
        parts = ["$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'" if stop else
                 "$ErrorActionPreference='Continue';$ProgressPreference='SilentlyContinue'"] if windows else (["set -e"] if stop else [])
        if windows:
            parts.append("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$OutputEncoding=[Console]::OutputEncoding")
        for key, value in (options.get("env") or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise BridgeError("invalid_arguments", "Remote environment name contains unsupported characters.")
            if windows:
                val = "$null" if value is None else ps_quote(value)
                parts.append(f"[Environment]::SetEnvironmentVariable({ps_quote(key)},{val},'Process')")
            else:
                parts.append(f"unset {key}" if value is None else f"export {key}={shlex.quote(value)}")
        if options.get("cwd"):
            parts.append(f"Set-Location -LiteralPath {ps_quote(options['cwd'])}" if windows else f"cd -- {shlex.quote(options['cwd'])}")
        parts.append(command)
        if windows:
            parts.append("if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")
        return ";".join(parts)

    def _remote_command(self, item, command, options=None):
        options = options or {}
        command = self._remote_script(item, command, options)
        windows = item.get("platform") == "windows"
        shell_argv = (["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                       base64.b64encode(command.encode("utf-16-le")).decode("ascii")]
                      if windows else ["sh", "-lc", command])
        forwarded = {k: v for k, v in options.items() if k in {"timeout_ms", "execution_mode", "yield_time_ms", "output_limit_bytes"}}
        if item["transport"] == "ssh":
            # SSH transmits a shell command string, not an execve argv array.
            remote = " ".join(shell_argv) if windows else shlex.join(shell_argv)
            argv = self._ssh_prefix(item) + [remote]
            return self.bridge._do("exec_command", {"argv": argv, **forwarded})
        if item["transport"] == "docker":
            return self.bridge._do("exec_command", {"argv": ["docker", "exec", item["container"], *shell_argv], **forwarded})
        raise BridgeError("host_transport_error", "Remote command transport is not shell based.")

    def status(self, name):
        item = self._get(name)
        if item["transport"] == "local":
            return {"host": self._public(item), "online": True, "route": "official_codex_runtime"}
        if item["transport"] == "mcp":
            try:
                result = self.dynamic_mcp.refresh(item["mcp_server"])
                return {"host": self._public(item), "online": True, "route": "dynamic_mcp", "mcp": result["server"]}
            except BridgeError as exc:
                return {"host": self._public(item), "online": False, "route": "dynamic_mcp", "error": exc.as_dict()}
        try:
            marker = "CCM_HOST_OK"
            result = self._remote_command(item, f"echo {marker}", {"timeout_ms": 10000, "execution_mode": "buffered", "output_limit_bytes": 1024})
            return {"host": self._public(item), "online": result.get("exit_code") == 0 and marker in result.get("stdout", ""), "route": item["transport"], "probe": {"exit_code": result.get("exit_code")}}
        except BridgeError as exc:
            return {"host": self._public(item), "online": False, "route": item["transport"], "error": exc.as_dict()}

    def route(self, args):
        item = self._get(args.get("host"))
        operation = str(args.get("operation") or "exec").strip().lower()
        supported = {
            "local": ["exec", "read", "list", "write", "delete", "search"],
            "mcp": ["exec", "read", "list", "write", "delete", "search"],
            "ssh": ["exec", "read", "list", "write", "delete", "search"],
            "docker": ["exec", "read", "list", "write", "delete", "search"],
        }[item["transport"]]
        return {"host": self._public(item), "operation": operation, "supported": operation in supported, "supported_operations": supported, "route": item["transport"]}

    def exec(self, args):
        item = self._get(args.get("host"))
        forwarded = {k: v for k, v in args.items() if k in {"command", "argv", "cwd", "shell", "shell_error_policy", "wsl_distribution", "wsl_cwd", "env", "timeout_ms", "output_limit_bytes", "execution_mode", "yield_time_ms", "tty"}}
        if item["transport"] == "local":
            return {"host": "local", "route": "official_codex_runtime", "result": self.bridge._do("exec_command", forwarded)}
        if item["transport"] == "mcp":
            result = self.dynamic_mcp.call({"name": f"{item['mcp_server']}:exec_command", "arguments": forwarded})
            out = {"host": item["name"], "route": "dynamic_mcp", "result": result}
            action = execution_view(result).get("next_action")
            if isinstance(result.get("next_action"), dict):
                out["next_action"] = result["next_action"]
            elif isinstance(action, dict) and action.get("tool") == "session_read":
                out["next_action"] = {"tool": "mcp_tool_call", "arguments": {
                    "name": f"{item['mcp_server']}:session_read", "arguments": action.get("arguments") or {}}}
            return out
        command = str(args.get("command") or "").strip()
        if command and args.get("argv"):
            raise BridgeError("invalid_arguments", "Use command or argv, not both.")
        if not command:
            argv = args.get("argv") or []
            if not argv:
                raise BridgeError("invalid_arguments", "Remote host_exec requires command or argv.")
            command = ("& " + " ".join(ps_quote(str(x)) for x in argv)) if item.get("platform") == "windows" else shlex.join([str(x) for x in argv])
        result = self._remote_command(item, command, forwarded)
        return {"host": item["name"], "route": item["transport"], "result": result}

    def files(self, args):
        item = self._get(args.get("host"))
        action = str(args.get("action") or "").strip().lower()
        path = str(args.get("path") or "").strip()
        if action not in {"read", "list", "write", "delete", "search"} or not path:
            raise BridgeError("invalid_arguments", "host_files requires action read/list/write/delete/search and path.")
        local_map = {"read": "read_file", "list": "list_dir", "write": "file_add", "delete": "file_delete", "search": "search_text"}
        if item["transport"] == "local":
            payload = {k: v for k, v in args.items() if k not in {"host", "action"}}
            return {"host": "local", "route": "official_codex_runtime", "result": self.bridge._do(local_map[action], payload)}
        if item["transport"] == "mcp":
            tool = local_map[action]
            payload = {k: v for k, v in args.items() if k not in {"host", "action"}}
            return {"host": item["name"], "route": "dynamic_mcp", "result": self.dynamic_mcp.call({"name": f"{item['mcp_server']}:{tool}", "arguments": payload})}
        windows = item.get("platform") == "windows"
        unsupported = [k for k in ("offset", "limit", "max_lines", "glob", "max_depth", "max_results") if k in args]
        if unsupported or args.get("regex") or args.get("encoding", "utf-8").lower().replace("_", "-") not in {"utf-8", "utf8"}:
            raise BridgeError("invalid_arguments", "These range/filter options require a structured MCP node; the remote shell route cannot silently ignore them.", details={"unsupported_options": unsupported})
        if action == "read":
            command = f"Get-Content -Raw -LiteralPath {ps_quote(path)}" if windows else f"cat -- {shlex.quote(path)}"
        elif action == "list":
            command = f"Get-ChildItem -Force -LiteralPath {ps_quote(path)} | Select-Object Name,Length,Mode | ConvertTo-Json -Compress" if windows else f"ls -la -- {shlex.quote(path)}"
        elif action == "delete":
            command = (f"Remove-Item -LiteralPath {ps_quote(path)} -Force -ErrorAction {'SilentlyContinue' if args.get('force') else 'Stop'}" if windows else f"rm {'-f ' if args.get('force') else ''}-- {shlex.quote(path)}")
        elif action == "search":
            query = str(args.get("query") or "")
            if not query:
                raise BridgeError("invalid_arguments", "search requires query.")
            command = (f"Get-ChildItem {'-Recurse ' if args.get('recursive') else ''}-File -LiteralPath {ps_quote(path)} | Select-String -SimpleMatch {ps_quote(query)}" if windows else f"grep {'-R ' if args.get('recursive') else ''}-n -F -- {shlex.quote(query)} {shlex.quote(path)}")
        else:
            content = str(args.get("content") or "")
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            if windows:
                guard = "" if args.get("force") else f"if (Test-Path -LiteralPath {ps_quote(path)}) {{ throw 'Destination already exists' }};"
                command = guard + f"$p=[IO.Path]::GetFullPath({ps_quote(path)});[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($p))|Out-Null;[IO.File]::WriteAllBytes($p,[Convert]::FromBase64String('{encoded}'))"
            else:
                parent = str(pathlib.PurePosixPath(path).parent)
                guard = "" if args.get("force") else "set -C;"
                command = f"mkdir -p -- {shlex.quote(parent)} && ({guard}printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)})"
        result = self._remote_command(item, command, {"timeout_ms": 120000, "execution_mode": "buffered", "output_limit_bytes": args.get("max_bytes", 32768)})
        return {"host": item["name"], "route": item["transport"], "action": action, "path": path, "result": result}
