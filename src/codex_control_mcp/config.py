from __future__ import annotations
import dataclasses, os, pathlib, tomllib
from urllib.parse import urlsplit
from .errors import BridgeError

DEFAULT_CONFIG = """# Trusted owner, no sandbox. Does not modify Codex Desktop.
permission_mode = "trusted_owner_full_access"
experimental = true
inherit_system_proxy = true
output_limit_bytes = 262144
file_limit_bytes = 8388608
session_cache_bytes = 2097152
max_sessions = 32
[http]
host = "127.0.0.1"
port = 8774
allowed_hosts = ["127.0.0.1:8774", "localhost:8774"]
allowed_origins = []
token_env = "CODEX_CONTROL_MCP_TOKEN"
request_limit_bytes = 2097152
requests_per_minute = 120
"""


@dataclasses.dataclass
class Config:
    home: pathlib.Path
    cwd: str
    codex_path: str | None = None
    git_path: str | None = None
    rg_path: str | None = None
    experimental: bool = True
    computer_use_enabled: bool = False
    browser_use_enabled: bool = False
    browser: dict = dataclasses.field(default_factory=dict)
    local_application_consent: bool = False
    auto_approve_application_access: bool = False
    inherit_system_proxy: bool = True
    output_limit_bytes: int = 262144
    file_limit_bytes: int = 8388608
    session_cache_bytes: int = 2097152
    max_sessions: int = 32
    http: dict = dataclasses.field(default_factory=dict)
    oauth: dict = dataclasses.field(default_factory=dict)
    runtime_home_override: str | None = None
    permission_mode: str = "trusted_owner_full_access"

    @property
    def application_access_policy(self):
        if self.auto_approve_application_access:
            return "owner_preapproved"
        return "local_dialog" if self.local_application_consent else "client_interactive"

    @property
    def requires_client_elicitation(self):
        official_gui = self.computer_use_enabled or (
            self.browser_use_enabled and self.browser.get('backend', 'official') == 'official'
        )
        return official_gui and not (
            self.auto_approve_application_access or self.local_application_consent
        )

    @property
    def runtime_home(self):
        return (
            pathlib.Path(self.runtime_home_override)
            if self.runtime_home_override
            else self.home / "runtime-home"
        )

    @classmethod
    def load(cls, home=None):
        root = (
            pathlib.Path(
                home
                or os.environ.get(
                    "CODEX_CONTROL_HOME", pathlib.Path.home() / ".codex-control-mcp"
                )
            )
            .expanduser()
            .resolve()
        )
        p = root / "config.toml"
        data = (
            tomllib.loads(p.read_text("utf-8-sig"))
            if p.exists()
            else tomllib.loads(DEFAULT_CONFIG)
        )
        if (
            data.get("permission_mode", "trusted_owner_full_access")
            != "trusted_owner_full_access"
        ):
            raise BridgeError(
                "invalid_config",
                "This owner build uses the explicitly authorized full-access mode.",
            )
        cwd = str(pathlib.Path(data.get("cwd", os.getcwd())).resolve())
        if not pathlib.Path(cwd).is_dir():
            raise BridgeError("invalid_config", "Configured cwd does not exist.")
        obj = cls(home=root, cwd=cwd)
        for key in {f.name for f in dataclasses.fields(cls)} - {"home", "cwd"}:
            if key in data:
                setattr(obj, key, data[key])
        for key in (
            "output_limit_bytes",
            "file_limit_bytes",
            "session_cache_bytes",
            "max_sessions",
        ):
            if type(getattr(obj, key)) is not int or getattr(obj, key) < 1:
                raise BridgeError("invalid_config", f"{key} must be positive.")
        for key in (
            "experimental",
            "computer_use_enabled",
            "browser_use_enabled",
            "local_application_consent",
            "auto_approve_application_access",
            "inherit_system_proxy",
        ):
            if type(getattr(obj, key)) is not bool:
                raise BridgeError("invalid_config", f"{key} must be a boolean.")
        if not isinstance(obj.http, dict) or not isinstance(obj.oauth, dict):
            raise BridgeError(
                "invalid_config", "http and oauth must be configuration tables."
            )
        if not isinstance(obj.browser, dict) or obj.browser.get('backend', 'official') not in ('official', 'tabbit'):
            raise BridgeError('invalid_config', 'browser.backend must be official or tabbit.')
        if type(obj.browser.get('headless', True)) is not bool:
            raise BridgeError('invalid_config', 'browser.headless must be a boolean.')
        for key, default, upper in (('max_pages', 8, 32), ('timeout_ms', 20000, 30000)):
            value = obj.browser.get(key, default)
            if type(value) is not int or not 1 <= value <= upper:
                raise BridgeError('invalid_config', f'browser.{key} is outside its supported range.')
        for key in ('executable_path', 'profile_directory'):
            if key in obj.browser and (not isinstance(obj.browser[key], str) or not obj.browser[key].strip()):
                raise BridgeError('invalid_config', f'browser.{key} must be a nonempty path.')
        for key, default, lower, upper in (
            ("port", 8774, 1, 65535),
            ("request_limit_bytes", 2097152, 1024, 16777216),
            ("requests_per_minute", 120, 1, 100000),
            ("body_timeout_seconds", 15, 1, 120),
        ):
            value = obj.http.get(key, default)
            if type(value) is not int or not lower <= value <= upper:
                raise BridgeError(
                    "invalid_config",
                    f"http.{key} must be an integer between {lower} and {upper}.",
                )
        for key in ("allowed_hosts", "allowed_origins"):
            values = obj.http.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v and "*" not in v for v in values
            ):
                raise BridgeError(
                    "invalid_config",
                    f"http.{key} must contain explicit host/origin strings without wildcards.",
                )
        if type(obj.oauth.get("enabled", False)) is not bool:
            raise BridgeError("invalid_config", "oauth.enabled must be a boolean.")
        if obj.oauth.get("enabled"):
            from .oauth import valid_issuer

            obj.oauth["issuer"] = valid_issuer(obj.oauth.get("issuer", ""))
        return obj

    def initialize_storage(self):
        for name in ("cache", "state", "logs"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        if not self.runtime_home_override:
            self.runtime_home.mkdir(parents=True, exist_ok=True)
            cfg = self.runtime_home / "config.toml"
            if not cfg.exists():
                cfg.write_text(
                    'sandbox_mode = "danger-full-access"\napproval_policy = "never"\n[analytics]\nenabled = false\n[feedback]\nenabled = false\n',
                    encoding="utf-8",
                )


def build_environment(cfg):
    env = os.environ.copy()
    env["CODEX_HOME"] = str(cfg.runtime_home)
    # Official wsl.exe otherwise emits UTF-16 diagnostics into the App Server's
    # UTF-8 output channel. This changes only our child environment.
    env['WSL_UTF8'] = '1'
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    detected = {
        k: bool(env.get(k) or env.get(k.lower()))
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    }
    source = (
        "environment"
        if any(detected[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
        else "none"
    )
    system_proxy_present = False
    if os.name == "nt" and cfg.inherit_system_proxy:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as reg:
                enabled = winreg.QueryValueEx(reg, "ProxyEnable")[0]
                raw = winreg.QueryValueEx(reg, "ProxyServer")[0] if enabled else ""
                system_proxy_present = bool(raw)
            if raw and source == "none":
                parts = dict(s.split("=", 1) for s in raw.split(";") if "=" in s)
                for key, value in {
                    "HTTP_PROXY": parts.get("http", raw if not parts else ""),
                    "HTTPS_PROXY": parts.get(
                        "https", parts.get("http", raw if not parts else "")
                    ),
                }.items():
                    if value:
                        value = value if "://" in value else "http://" + value
                        u = urlsplit(value)
                        if (
                            u.scheme in ("http", "https")
                            and u.hostname
                            and u.port
                            and not u.username
                            and not u.password
                        ):
                            env[key] = value
                            source = "wininet_explicit_http"
        except (OSError, ValueError):
            pass
    entries = [
        x.strip()
        for x in env.get("NO_PROXY", env.get("no_proxy", "")).split(",")
        if x.strip()
    ]
    for x in ("localhost", "127.0.0.1", "::1"):
        if x not in entries:
            entries.append(x)
    env["NO_PROXY"] = ",".join(entries)
    # wsl.exe does not inherit arbitrary Windows environment variables. Use
    # Microsoft's directional sharing list without altering existing entries.
    # /u forwards URLs verbatim to Linux; /p would incorrectly translate them.
    shared = [entry for entry in env.get("WSLENV", "").split(":") if entry]
    already_shared = {entry.split("/", 1)[0] for entry in shared}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if env.get(name) and name not in already_shared:
            shared.append(name + "/u")
    env["WSLENV"] = ":".join(shared)
    return env, {
        "source": source,
        "system_proxy_detected": system_proxy_present,
        "env_names_present": [
            k
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
            if env.get(k)
        ],
        "child_environment_verified": False,
        "runtime_route_verified": "unknown",
        "winhttp": "not_tested",
    }
