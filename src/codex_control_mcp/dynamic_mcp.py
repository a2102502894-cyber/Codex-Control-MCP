from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import urllib.parse

import httpx
from jsonschema import Draft7Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .common import atomic_json, utc_now
from .errors import BridgeError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ESSENTIAL_ENV = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "COMSPEC", "PATHEXT",
}


class DynamicMCPManager:
    """Persistent registry and on-demand client for independent MCP servers."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = pathlib.Path(cfg.home) / "state" / "dynamic-mcp.json"
        self.servers = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("mcp_registry_corrupt", "Dynamic MCP registry cannot be read.") from exc
        return data if isinstance(data, dict) else {}

    def _save(self):
        atomic_json(self.path, self.servers)

    def _name(self, value):
        value = str(value or "").strip()
        if not NAME_RE.fullmatch(value):
            raise BridgeError("invalid_arguments", "MCP server name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}.")
        return value

    def _public(self, item):
        return {
            "name": item["name"],
            "description": item.get("description", ""),
            "transport": item["transport"],
            "url": item.get("url", ""),
            "command": item.get("command", ""),
            "args": item.get("args", []),
            "cwd": item.get("cwd", ""),
            "enabled": bool(item.get("enabled", True)),
            "timeout_ms": int(item.get("timeout_ms", 30000)),
            "header_keys": sorted((item.get("headers") or {}).keys() | (item.get("header_files") or {}).keys()),
            "env_keys": sorted((item.get("env") or {}).keys() | (item.get("env_from_env") or {}).keys() | (item.get("env_files") or {}).keys()),
            "tool_count": len(item.get("tools") or []),
            "status": item.get("status", "never_refreshed"),
            "last_error": item.get("last_error", ""),
            "refreshed_at": item.get("refreshed_at", ""),
        }

    def _normalize(self, args, existing=None):
        item = dict(existing or {})
        name = self._name(args.get("name") or item.get("name"))
        transport = str(args.get("transport") or item.get("transport") or "").strip().lower()
        if transport not in {"streamable_http", "stdio"}:
            raise BridgeError("invalid_arguments", "transport must be streamable_http or stdio.")
        item.update({
            "name": name,
            "description": str(args.get("description", item.get("description", ""))).strip(),
            "transport": transport,
            "enabled": bool(args.get("enabled", item.get("enabled", True))),
            "timeout_ms": int(args.get("timeout_ms") or item.get("timeout_ms") or 30000),
        })
        if not 1000 <= item["timeout_ms"] <= 300000:
            raise BridgeError("invalid_arguments", "timeout_ms must be between 1000 and 300000.")
        for key in ("headers", "header_files", "header_prefixes", "env", "env_from_env", "env_files"):
            if key in args:
                value = args.get(key) or {}
                if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                    raise BridgeError("invalid_arguments", f"{key} must be an object of string values.")
                item[key] = dict(value)
            else:
                item.setdefault(key, {})
        if transport == "streamable_http":
            url = str(args.get("url") or item.get("url") or "").strip()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise BridgeError("invalid_arguments", "streamable_http requires an http(s) URL.")
            if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise BridgeError("invalid_arguments", "Remote MCP HTTP URLs must use HTTPS.")
            item["url"] = url
            item["command"] = ""
            item["args"] = []
            item["cwd"] = ""
        else:
            command = str(args.get("command") or item.get("command") or "").strip()
            if not command:
                raise BridgeError("invalid_arguments", "stdio requires command.")
            item["command"] = command
            item["args"] = list(args.get("args", item.get("args", [])) or [])
            item["cwd"] = str(args.get("cwd", item.get("cwd", "")) or "").strip()
            item["url"] = ""
        item.setdefault("tools", [])
        item.setdefault("status", "never_refreshed")
        item.setdefault("last_error", "")
        item.setdefault("refreshed_at", "")
        return item

    def _headers(self, item):
        headers = dict(item.get("headers") or {})
        prefixes = item.get("header_prefixes") or {}
        for name, file_path in (item.get("header_files") or {}).items():
            try:
                value = pathlib.Path(file_path).expanduser().read_text("utf-8").strip()
            except OSError as exc:
                raise BridgeError("mcp_secret_unavailable", f"Header source for {name} cannot be read.") from exc
            headers[name] = str(prefixes.get(name, "")) + value
        return headers

    def _env(self, item):
        env = {key: os.environ[key] for key in ESSENTIAL_ENV if key in os.environ}
        env.update(item.get("env") or {})
        for target, source in (item.get("env_from_env") or {}).items():
            if source in os.environ:
                env[target] = os.environ[source]
        for target, file_path in (item.get("env_files") or {}).items():
            try:
                env[target] = pathlib.Path(file_path).expanduser().read_text("utf-8").strip()
            except OSError as exc:
                raise BridgeError("mcp_secret_unavailable", f"Environment source for {target} cannot be read.") from exc
        return env

    async def _with_session(self, item, operation):
        timeout = item.get("timeout_ms", 30000) / 1000
        if item["transport"] == "streamable_http":
            async with httpx.AsyncClient(trust_env=False, headers=self._headers(item), timeout=timeout) as http:
                async with streamable_http_client(item["url"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await operation(session)
        params = StdioServerParameters(
            command=item["command"],
            args=item.get("args") or [],
            cwd=item.get("cwd") or None,
            env=self._env(item),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await operation(session)

    def _run(self, item, operation):
        try:
            return asyncio.run(self._with_session(item, operation))
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError("mcp_unavailable", f"Dynamic MCP operation failed ({type(exc).__name__}).", retryable=True) from exc

    def refresh(self, name):
        name = self._name(name)
        item = self.servers.get(name)
        if not item:
            raise BridgeError("mcp_not_found", "Dynamic MCP server was not found.")
        if not item.get("enabled", True):
            raise BridgeError("mcp_disabled", "Dynamic MCP server is disabled.")

        async def list_tools(session):
            result = await session.list_tools()
            return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools]

        try:
            tools = self._run(item, list_tools)
            item["tools"] = tools
            item["status"] = "ready"
            item["last_error"] = ""
            item["refreshed_at"] = utc_now()
            self._save()
            return {"server": self._public(item), "tools": [{"name": t.get("name"), "description": t.get("description", "")} for t in tools]}
        except BridgeError as exc:
            item["status"] = "error"
            item["last_error"] = exc.code
            item["refreshed_at"] = utc_now()
            self._save()
            raise

    def manage(self, args):
        action = str(args.get("action") or "").strip().lower()
        name = str(args.get("name") or "").strip()
        if action == "list":
            return {"action": action, "servers": [self._public(v) for _, v in sorted(self.servers.items())], "registry": str(self.path)}
        if action == "register":
            item = self._normalize(args)
            if item["name"] in self.servers:
                raise BridgeError("mcp_exists", "Dynamic MCP server already exists.")
            self.servers[item["name"]] = item
            self._save()
            return {"action": action, "server": self._public(item), "registry": str(self.path)}
        if action == "get":
            item = self.servers.get(self._name(name))
            if not item:
                raise BridgeError("mcp_not_found", "Dynamic MCP server was not found.")
            return {"action": action, "server": self._public(item), "tools": item.get("tools") or [], "registry": str(self.path)}
        if action == "update":
            key = self._name(name)
            if key not in self.servers:
                raise BridgeError("mcp_not_found", "Dynamic MCP server was not found.")
            item = self._normalize(args, self.servers[key])
            self.servers[key] = item
            self._save()
            return {"action": action, "server": self._public(item), "registry": str(self.path)}
        if action in {"enable", "disable"}:
            key = self._name(name)
            if key not in self.servers:
                raise BridgeError("mcp_not_found", "Dynamic MCP server was not found.")
            self.servers[key]["enabled"] = action == "enable"
            if action == "disable":
                self.servers[key]["status"] = "disabled"
            self._save()
            return {"action": action, "server": self._public(self.servers[key]), "registry": str(self.path)}
        if action == "remove":
            key = self._name(name)
            if key not in self.servers:
                raise BridgeError("mcp_not_found", "Dynamic MCP server was not found.")
            removed = self.servers.pop(key)
            self._save()
            return {"action": action, "removed": self._public(removed), "registry": str(self.path)}
        if action == "refresh":
            return {"action": action, **self.refresh(name), "registry": str(self.path)}
        raise BridgeError("invalid_arguments", "Unsupported mcp_manage action.", details={"allowed": ["register", "list", "get", "update", "enable", "disable", "refresh", "remove"]})

    def _qualified(self, qualified):
        value = str(qualified or "").strip()
        if ":" not in value:
            raise BridgeError("invalid_arguments", "Dynamic MCP tool name must use <server>:<tool>.")
        server, tool = value.split(":", 1)
        return self._name(server), tool.strip()

    def _cached_tools(self, item):
        if not item.get("tools") and item.get("enabled", True):
            self.refresh(item["name"])
        return item.get("tools") or []

    def search(self, args):
        query = str(args.get("query") or "").strip().lower()
        server_filter = str(args.get("server") or "").strip()
        limit = max(1, min(int(args.get("limit") or 20), 100))
        results = []
        for name, item in sorted(self.servers.items()):
            if server_filter and name != server_filter:
                continue
            if not item.get("enabled", True):
                continue
            for tool in self._cached_tools(item):
                hay = " ".join([str(tool.get("name", "")), str(tool.get("title", "")), str(tool.get("description", ""))]).lower()
                if query and all(part not in hay for part in query.split()):
                    continue
                results.append({"name": tool.get("name"), "qualified_name": f"{name}:{tool.get('name')}", "title": tool.get("title", ""), "description": tool.get("description", ""), "server": name})
                if len(results) >= limit:
                    return {"tools": results, "count": len(results)}
        return {"tools": results, "count": len(results)}

    def inspect(self, args):
        server, tool_name = self._qualified(args.get("name"))
        item = self.servers.get(server)
        if not item or not item.get("enabled", True):
            raise BridgeError("mcp_unavailable", "Dynamic MCP server is absent or disabled.")
        for tool in self._cached_tools(item):
            if tool.get("name") == tool_name:
                return {"qualified_name": f"{server}:{tool_name}", "server": self._public(item), "tool": tool}
        raise BridgeError("mcp_tool_not_found", "Dynamic MCP tool was not found in the latest schema.")

    def call(self, args):
        server, tool_name = self._qualified(args.get("name"))
        item = self.servers.get(server)
        if not item or not item.get("enabled", True):
            raise BridgeError("mcp_unavailable", "Dynamic MCP server is absent or disabled.")
        tool = next((t for t in self._cached_tools(item) if t.get("name") == tool_name), None)
        if not tool:
            raise BridgeError("mcp_tool_not_found", "Dynamic MCP tool was not found in the latest schema.")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_arguments", "arguments must be an object.")
        schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
        errors = sorted(Draft7Validator(schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            raise BridgeError("invalid_arguments", "Dynamic MCP tool arguments failed cached schema validation.", details={"validation_error": errors[0].message})

        async def invoke(session):
            result = await session.call_tool(tool_name, arguments)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

        result = self._run(item, invoke)
        return {"qualified_name": f"{server}:{tool_name}", "result": result}
