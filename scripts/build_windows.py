"""Build only the bridge. The installed official Codex runtime is never bundled."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, default=ROOT.parent / "delivery" / "Codex-Control-MCP"
    )
    args = parser.parse_args()
    # PyInstaller runs with ROOT as cwd. Resolve once in the caller's directory
    # so relative delivery paths and the subsequent hash check refer to one file.
    args.out = args.out.resolve()
    if os.name != "nt":
        raise SystemExit("Build the Windows executable on Windows.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = ROOT.parent / "build-temp" / "Codex-Control-MCP" / stamp
    work.mkdir(parents=True, exist_ok=False)
    args.out.mkdir(parents=True, exist_ok=True)
    log = ROOT / "evidence" / ("build-" + stamp + ".log")
    command = [
        sys.executable,
        str(ROOT / "scripts" / "pyinstaller_safe.py"),
        "--noconfirm",
        "--onefile",
        "--console",
        "--name",
        "Codex-Control-MCP",
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(args.out),
        "--workpath",
        str(work / "work"),
        "--specpath",
        str(work),
        "--recursive-copy-metadata",
        "mcp",
        "--copy-metadata",
        "jsonschema",
        "--collect-data",
        "jsonschema_specifications",
        "--collect-submodules",
        "mcp.client",
        "--collect-submodules",
        "mcp.server",
        "--collect-submodules",
        "mcp.shared",
        "--exclude-module",
        "mcp.cli",
        "--collect-submodules",
        "uvicorn",
        "--collect-all",
        "playwright",
        "--runtime-hook",
        str(ROOT / "scripts" / "pyi_runtime_no_wmi.py"),
        "--add-data",
        str(ROOT / "src/codex_control_mcp/tabbit_worker.cjs") + ";codex_control_mcp",
        "--hidden-import",
        "win32clipboard",
        "--hidden-import",
        "win32timezone",
        "--hidden-import",
        "win32job",
        "--hidden-import",
        "win32api",
        str(ROOT / "scripts" / "entrypoint.py"),
    ]
    # Default Windows manifest is asInvoker. Elevated execution is inherited from
    # the parent, or explicitly requested through a normal UAC launch by the owner.
    build_env = os.environ.copy()
    bootstrap = ROOT / "scripts" / "pyinstaller_bootstrap"
    inherited_pythonpath = build_env.get("PYTHONPATH")
    build_env["PYTHONPATH"] = str(bootstrap) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    with log.open("wb") as stream:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=build_env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if result.returncode:
        print(
            json.dumps(
                {"ok": False, "build_log": str(log), "exit_code": result.returncode}
            )
        )
        return result.returncode
    executable = args.out / "Codex-Control-MCP.exe"
    host = args.out / "Codex-Control-MCP-Host.exe"
    compiler = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    with log.open("ab") as stream:
        result = subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/target:winexe",
                "/optimize+",
                "/out:" + str(host),
                str(ROOT / "scripts/WindowsHost.cs"),
            ],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if result.returncode:
        return result.returncode
    report = {
        "ok": True,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "executable": str(executable),
        "size_bytes": executable.stat().st_size,
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "background_host": str(host),
        "background_host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
        "background_host_subsystem": "windows_gui",
        "python": sys.version,
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "mcp_sdk": importlib.metadata.version("mcp"),
        "manifest_execution_level": "asInvoker",
        "code_signed": False,
        "official_codex_bundled": False,
        "git_or_ripgrep_bundled": False,
        "build_log": str(log),
        "build_work_directory": str(work),
    }
    (ROOT / "evidence" / "windows-build.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
