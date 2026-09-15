"""Complete the owned Debian WSL1 fixture after Windows enables the component.

Installs only a uniquely named test distribution from a hash-verified official
image. Never modifies another distribution, reboots Windows, or changes firmware.
Execution acceptance uses the actual release EXE -> official Codex command/exec.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from codex_control_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
NAME = "Codex-Control-MCP-Debian"
EXPECTED = "5ec7dc68216e75d1d4d4761474e99d8461a98d316537110314b137122a879e0f"


def decode(b):
    return b.decode("utf-16" if b"\0" in b[:200] else "utf-8", "replace").strip()


def run(argv, timeout=60):
    p = subprocess.run(
        argv,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return {
        "exit_code": p.returncode,
        "stdout": decode(p.stdout),
        "stderr": decode(p.stderr),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    args = ap.parse_args()
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "distro": NAME,
        "wsl_version": 1,
        "pass": False,
        "reboot_requested": False,
        "other_distributions_changed": False,
        "steps": [],
    }
    try:
        with args.image.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != EXPECTED:
                raise RuntimeError("Official Debian image digest mismatch")
        # Registry state is read-only here; a pending component must be completed
        # by normal Windows startup before attempting distro creation.
        feature = run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux).State.ToString()",
            ]
        )
        report["component"] = feature
        if feature["exit_code"] or feature["stdout"] != "Enabled":
            report["status"] = "WINDOWS_COMPONENT_RESTART_REQUIRED"
            return 2
        listed = run(["wsl.exe", "--list", "--quiet"])
        names = (
            listed["stdout"].replace("\0", "").splitlines()
            if listed["exit_code"] == 0
            else []
        )
        install = (
            Path(os.environ["LOCALAPPDATA"]) / "Programs/Codex-Control-MCP/wsl/Debian"
        )
        owner = install.parent / "owner.json"
        marker = {
            "project": "Codex-Control-MCP",
            "name": NAME,
            "image_sha256": EXPECTED,
            "directory": str(install),
        }
        if NAME in names:
            if not owner.is_file() or json.loads(owner.read_text("utf-8")) != marker:
                raise RuntimeError(
                    "The distribution name is already owned by another installation"
                )
        else:
            if owner.exists() and json.loads(owner.read_text("utf-8")) != marker:
                raise RuntimeError(
                    "Refusing to reuse a WSL directory with a different owner marker"
                )
            if install.exists() and any(install.iterdir()) and not owner.exists():
                raise RuntimeError("Refusing to overwrite an unowned WSL directory")
            install.mkdir(parents=True, exist_ok=True)
            owner.write_text(json.dumps(marker), "utf-8")
            imported = run(
                [
                    "wsl.exe",
                    "--import",
                    NAME,
                    str(install),
                    str(args.image.resolve()),
                    "--version",
                    "1",
                ],
                300,
            )
            report["steps"].append({"import": imported})
            if imported["exit_code"]:
                if (
                    "WSL_E_WSL1_NOT_SUPPORTED"
                    in imported["stdout"] + imported["stderr"]
                ):
                    report["status"] = "WINDOWS_COMPONENT_RESTART_REQUIRED"
                    return 2
                raise RuntimeError(
                    "WSL import did not complete; inspect the recorded Windows error before retrying"
                )
        # An independent home keeps the packaged acceptance free of real model credentials.
        base = ROOT / "test-workspace"
        base.mkdir(exist_ok=True)
        test = Path(tempfile.mkdtemp(prefix="wsl-", dir=base))
        exe = (
            Path(os.environ["LOCALAPPDATA"])
            / "Programs/Codex-Control-MCP"
            / __version__
            / "Codex-Control-MCP.exe"
        )
        command = "set -eu; work=$(mktemp -d /tmp/ccm-v014-XXXXXX); trap 'rm -rf -- \"$work\"' EXIT; cd \"$work\"; printf '%s' 'CCM_WSL_中文' > proof.txt; test \"$(cat proof.txt)\" = 'CCM_WSL_中文'; printf 'CCM_WSL_014_OK\\n'; uname -s; pwd; exit 0"
        params = {
            "shell": "wsl",
            "wsl_distribution": NAME,
            "wsl_cwd": "/tmp",
            "command": command,
            "timeout_ms": 30000,
        }
        actual = run(
            [
                str(exe),
                "--home",
                str(test / "home"),
                "call",
                "exec_command",
                "--args-json",
                json.dumps(params, ensure_ascii=True),
            ],
            90,
        )
        report["execution_cli"] = actual
        payload = json.loads(actual["stdout"])
        value = payload.get("result") or {}
        report["pass"] = bool(
            payload.get("ok")
            and value.get("exit_code") == 0
            and "CCM_WSL_014_OK" in value.get("stdout", "")
            and value.get("execution_backend") == "codex_app_server.command_exec"
        )
        report["status"] = "PASS" if report["pass"] else "FAIL"
        return 0 if report["pass"] else 1
    except Exception as e:
        report["error"] = type(e).__name__ + ": " + str(e)[:1800]
        report.setdefault("status", "FAIL")
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (ROOT / "evidence/wsl-acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    raise SystemExit(main())
