from __future__ import annotations
import dataclasses, json, os, pathlib, re, shutil
from .common import digest, run_metadata, ps_argv
from .errors import BridgeError


@dataclasses.dataclass
class RuntimeInfo:
    codex_path: str
    cli_version: str
    desktop_version: str | None
    source: str
    file_fingerprint: str
    resources_path: str | None = None
    git_path: str | None = None
    rg_path: str | None = None
    app_server_supported: bool = True
    browser_supported: str = "unverified"
    computer_use_supported: str = "unverified"
    remote_supported: str = "unverified"

    def as_dict(self):
        return dataclasses.asdict(self)


class Discovery:
    def __init__(self, config):
        self.cfg = config

    def find(self):
        candidates = []
        desktop_version, resources = None, None
        if self.cfg.codex_path:
            candidates.append((pathlib.Path(self.cfg.codex_path), "explicit"))
        if os.name == "nt":
            try:
                r = run_metadata(
                    ps_argv(
                        "$p=Get-AppxPackage 'OpenAI.Codex'|Select-Object -First 1;if($p){@{version=$p.Version.ToString();path=$p.InstallLocation}|ConvertTo-Json -Compress}"
                    ),
                    timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    p = json.loads(r.stdout)
                    desktop_version = p["version"]
                    resources = pathlib.Path(p["path"]) / "app/resources"
            except (BridgeError, ValueError, KeyError):
                pass
            local = (
                pathlib.Path(
                    os.environ.get(
                        "LOCALAPPDATA", str(pathlib.Path.home() / "AppData/Local")
                    )
                )
                / "OpenAI/Codex/bin"
            )
            if local.is_dir():
                for p in sorted(
                    local.glob("*/codex.exe"),
                    key=lambda p: p.stat().st_mtime_ns,
                    reverse=True,
                )[:8]:
                    candidates.append((p, "official_appdata"))
            if resources:
                candidates.append((resources / "codex.exe", "app_package"))
        p = shutil.which("codex")
        if p:
            candidates.append((pathlib.Path(p), "path"))
        seen = set()
        for path, source in candidates:
            if str(path).lower() in seen:
                continue
            seen.add(str(path).lower())
            if not path.is_file():
                continue
            try:
                r = run_metadata([str(path), "--version"], timeout=8)
                version = r.stdout.strip()
                if r.returncode or not re.fullmatch(
                    r"codex-cli\s+[0-9]+\.[0-9]+\.[0-9]+[^\r\n]*", version
                ):
                    continue
                st = path.stat()
                identity = digest(
                    {
                        "path": str(path.resolve()),
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "version": version,
                        "desktop_version": desktop_version,
                    }
                )
                git = self._find_git()
                rg = self._find_rg(resources)
                return RuntimeInfo(
                    str(path.resolve()),
                    version,
                    desktop_version,
                    source,
                    identity,
                    str(resources) if resources else None,
                    git,
                    rg,
                )
            except BridgeError:
                continue
        raise BridgeError(
            "runtime_missing",
            "No verified official Codex executable was found. No fallback executor is used.",
        )

    def _find_git(self):
        paths = []
        if self.cfg.git_path:
            paths.append(pathlib.Path(self.cfg.git_path))
        if shutil.which("git"):
            paths.append(pathlib.Path(shutil.which("git")))
        if os.name == "nt":
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                        paths.append(
                            pathlib.Path(winreg.QueryValueEx(key, "InstallPath")[0])
                            / "cmd/git.exe"
                        )
                except OSError:
                    pass
            for base in (
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("LOCALAPPDATA", "") + "/Programs",
            ):
                paths.append(pathlib.Path(base) / "Git/cmd/git.exe")
            paths += [
                pathlib.Path.home() / "scoop/apps/git/current/cmd/git.exe",
                self.cfg.home / "tools/mingit/cmd/git.exe",
                pathlib.Path.home() / ".codex-control-mcp/tools/mingit/cmd/git.exe",
            ]
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    userpath = winreg.QueryValueEx(key, "Path")[0]
                for d in os.path.expandvars(userpath).split(";"):
                    if d:
                        paths.append(pathlib.Path(d) / "git.exe")
            except OSError:
                pass
        for p in paths:
            if p.is_file():
                return str(p.resolve())
        return None

    def _find_rg(self, resources):
        candidates = []
        if self.cfg.rg_path:
            candidates.append(pathlib.Path(self.cfg.rg_path))
        if shutil.which("rg"):
            candidates.append(pathlib.Path(shutil.which("rg")))
        candidates += [
            self.cfg.home / "tools/ripgrep/rg.exe",
            pathlib.Path.home() / ".codex-control-mcp/tools/ripgrep/rg.exe",
        ]
        if resources:
            candidates.append(resources / "rg.exe")
        for p in candidates:
            if not p.is_file():
                continue
            try:
                r = run_metadata([str(p), "--version"], timeout=5)
                if r.returncode == 0 and r.stdout.startswith("ripgrep "):
                    return str(p.resolve())
            except BridgeError:
                continue
        return None
