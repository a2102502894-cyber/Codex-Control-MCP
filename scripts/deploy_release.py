"""Versioned Windows owner deployment with offline recovery and rollback.

Only manages the two manual tasks belonging to this project. Never terminates
processes by name, changes system policy, reboots, or prints credentials.
"""

from __future__ import annotations
import argparse
import asyncio
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import time
import tomllib

# This script can be launched from the currently running onefile bridge.
# Force any different packaged executable to start as a new PyInstaller app.
os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

import httpx
from codex_control_mcp.client import call_running_service
from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.common import atomic_json
from codex_control_mcp.oauth import valid_issuer
from public_transport import SafeConnectTransport

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".codex-control-mcp"
PROGRAMS = Path(os.environ["LOCALAPPDATA"]) / "Programs/Codex-Control-MCP"
NAMES = ["Codex-Control-MCP-OnDemand", "Codex-Control-MCP-HTTPS-OnDemand"]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps(script, timeout=30):
    command = (
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        + script
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(
            "Owned task transaction failed: "
            + result.stderr.decode("utf-8", "replace")[-1800:]
        )
    return result.stdout.decode("utf-8", "replace").strip()


def cli(exe, *args, timeout=55):
    result = subprocess.run(
        [str(exe), "--home", str(HOME), *args],
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(
            "Owned service command failed: "
            + result.stdout.decode("utf-8", "replace")[-1500:]
        )
    return json.loads(result.stdout)


async def verify_core(version):
    out = await call_running_service(
        Config.load(HOME), "codex_health", {"active": True}
    )
    if not out or not out.get("ok") or out["result"].get("bridge_version") != version:
        raise RuntimeError("New service identity/version is not yet verified")
    if out["result"].get("child_is_admin_verified") is not True:
        raise RuntimeError("Official child administrator token verification failed")
    return out


async def verify_https(cfg, hostname, expected_oauth):
    env, _ = build_environment(cfg)
    url = "https://" + hostname + "/mcp"
    async with httpx.AsyncClient(
        trust_env=False, transport=SafeConnectTransport(proxy=env.get("HTTPS_PROXY")), timeout=12
    ) as client:
        response = await client.get(url)
        if response.status_code != 401:
            raise RuntimeError("Public HTTPS did not reach the authenticated bridge")
        if expected_oauth:
            if "oauth-protected-resource/mcp" not in response.headers.get(
                "www-authenticate", ""
            ):
                raise RuntimeError("OAuth challenge is absent from public HTTPS")
            meta = await client.get(
                "https://" + hostname + "/.well-known/oauth-authorization-server"
            )
            if (
                meta.status_code != 200
                or meta.json().get("issuer") != cfg.oauth["issuer"]
            ):
                raise RuntimeError(
                    f"Public OAuth discovery is not available (HTTP {meta.status_code})"
                )
        return {
            "url": url,
            "unauthenticated_status": response.status_code,
            "oauth_discovery": expected_oauth,
        }


def tabbit_config(text, executable, profile):
    """Change only the approved Browser backend settings; retain other tables."""
    previous = tomllib.loads(text)
    browser = {**previous.get('browser', {}), 'backend': 'tabbit',
               'executable_path': str(executable), 'profile_directory': str(profile),
               'headless': True}
    expected = {**previous, 'browser_use_enabled': True, 'browser': browser}
    first_table = re.search(r'(?m)^[ \t]*\[', text)
    at = first_table.start() if first_table else len(text)
    root, tables = text[:at], text[at:]
    root, count = re.subn(r'(?m)^browser_use_enabled[ \t]*=[ \t]*(?:true|false)[ \t]*(?:#[^\n]*)?$',
                          'browser_use_enabled = true', root)
    if not count:
        root = 'browser_use_enabled = true\n' + root
    def scalar(value):
        if type(value) is bool:
            return 'true' if value else 'false'
        if type(value) is int:
            return str(value)
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        raise RuntimeError('Unsupported existing browser configuration value')
    block = '[browser]\n' + '\n'.join(k + ' = ' + scalar(v) for k, v in browser.items()) + '\n\n'
    table = re.compile(r'(?ms)^\[browser\]\s*\n.*?(?=^\[|\Z)')
    if table.search(tables):
        tables = table.sub(lambda _: block, tables, count=1)
    else:
        tables = tables.rstrip() + '\n\n' + block
    updated = root + tables
    if tomllib.loads(updated) != expected:
        raise RuntimeError('Browser enabling would change unrelated configuration')
    return updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--enable-oauth", action="store_true")
    ap.add_argument("--enable-computer", action="store_true",
                    help="Enable the desktop tools after verified HTTP GUI acceptance")
    ap.add_argument('--enable-browser', type=Path, metavar='PACKAGED_HTTP_PROOF',
                    help='Enable installed Tabbit after this EXE passes all eight Browser tools over HTTP')
    ap.add_argument('--enable-owner-preapproval', type=Path, metavar='PACKAGED_HTTP_PROOF',
                    help='Apply the owner\'s standing app-access approval after a no-callback packaged GUI test')
    args = ap.parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError(
            "Use an administrator terminal; this deployment does not bypass UAC"
        )
    source = args.release / "Codex-Control-MCP.exe"
    build = json.loads((ROOT / "evidence/windows-build.json").read_text("utf-8"))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if sha != build["sha256"] or str(source.resolve()) != str(
        Path(build["executable"]).resolve()
    ):
        raise RuntimeError(
            "Release path or SHA256 differs from the verified build manifest"
        )
    host_source = args.release / "Codex-Control-MCP-Host.exe"
    if hashlib.sha256(host_source.read_bytes()).hexdigest() != build.get(
        "background_host_sha256"
    ):
        raise RuntimeError("Background host differs from the verified build")
    info = json.loads(
        subprocess.check_output(
            [str(source), "version"],
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    )
    version = info["version"]
    if info["name"] != "Codex-Control-MCP" or not all(
        p.isdigit() for p in version.split(".")
    ):
        raise RuntimeError("Invalid release identity")
    if args.enable_owner_preapproval:
        proof = json.loads(args.enable_owner_preapproval.read_text('utf-8'))
        approvals = [s.get('authorization') or {} for s in proof.get('snapshots', [])]
        if not (proof.get('pass') is True and proof.get('version') == version
                and proof.get('executable_sha256') == sha
                and proof.get('client_elicitation_callback') is False
                and proof.get('owned_service_stopped') is True
                and any(a.get('decision') == 'accept'
                        and a.get('source') == 'explicit_owner_preapproval' for a in approvals)):
            raise RuntimeError('Owner preapproval requires a matching packaged GUI receipt without client UI')
    if args.enable_computer:
        gui = json.loads((ROOT / 'evidence' / ('gui-v' + version.replace('.', '') + '-acceptance.json')).read_text('utf-8'))
        if not (gui.get('pass') and gui.get('bridge_version') == version
                and gui.get('acceptance_transport') == 'streamable_http'
                and gui.get('owned_fixture_stopped')):
            raise RuntimeError('Desktop tools require this version\'s completed HTTP GUI acceptance')
    browser_proof = None
    if args.enable_browser:
        browser_proof = json.loads(args.enable_browser.read_text('utf-8'))
        operations = next((c.get('operations', []) for c in browser_proof.get('checks', [])
                           if c['name'] == 'eight_actions' and c['pass']), [])
        required = {'start', 'snapshot', 'click', 'fill', 'press', 'scroll', 'navigate', 'close'}
        if not (browser_proof.get('pass') and browser_proof.get('version') == version
                and browser_proof.get('executable_sha256') == sha
                and browser_proof.get('acceptance_transport') == 'streamable_http'
                and browser_proof.get('owned_fixture_stopped')
                and browser_proof.get('owned_service_stopped')
                and required.issubset(operations)
                and browser_proof['runtime'].get('official_browser_backend') is False):
            raise RuntimeError('Browser tools require completed packaged Tabbit HTTP acceptance')
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = ROOT / "evidence" / ("deployment-" + version + "-" + stamp)
    snapshot.mkdir()
    report = {
        "version": version,
        "sha256": sha,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": str(snapshot),
        "agentdock_modified": False,
        "desktop_modified": False,
        "automatic_startup": False,
        "system_policy_changed": False,
        "restart_performed": False,
        "clash_modified": False,
        "system_proxy_modified": False,
    }
    destination = PROGRAMS / version
    destination.mkdir(parents=True, exist_ok=True)
    exe = destination / source.name
    if exe.exists() and hashlib.sha256(exe.read_bytes()).hexdigest() != sha:
        raise RuntimeError(
            "A different binary already occupies this release version; publish a new version"
        )
    if not exe.exists():
        shutil.copyfile(source, exe)
    host_exe = destination / host_source.name
    if (
        host_exe.exists()
        and hashlib.sha256(host_exe.read_bytes()).hexdigest()
        != build["background_host_sha256"]
    ):
        raise RuntimeError("A different background host occupies this release version")
    if not host_exe.exists():
        shutil.copyfile(host_source, host_exe)
    if not (HOME / "config.toml").exists():
        cli(exe, "install")
    shutil.copyfile(HOME / "config.toml", snapshot / "config-before.toml")
    inspect = (
        r"""
$homePath=HOME;$root=PROGRAM_ROOT;$snapshot=SNAPSHOT
$items=@()
foreach($name in @('Codex-Control-MCP-OnDemand','Codex-Control-MCP-HTTPS-OnDemand')){
 $t=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
 if($t){
  if(@($t.Actions).Count -ne 1){throw 'Unexpected task action count'}
  $action=$t.Actions[0]
  if(-not $action.Execute.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Existing task is not owned by this project'}
  $suffix=if($name -eq 'Codex-Control-MCP-OnDemand'){' serve --transport streamable-http'}else{' tunnel'}
  $expected='--home "'+$homePath+'"'+$suffix
  if($action.Arguments -ne $expected){throw 'Existing task arguments do not match this owner home'}
  if(@($t.Triggers|Where-Object {$null -ne $_}).Count -ne 0){throw 'Unexpected automatic triggers; no task was changed'}
  Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $snapshot ($name+'.xml')) -Encoding Unicode
  $items+=@{name=$name;exists=$true;state=$t.State.ToString();execute=$action.Execute;working_directory=$action.WorkingDirectory}
 }else{$items+=@{name=$name;exists=$false;state='Absent'}}
}
$listener=@(Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue)
$live=@()
foreach($l in $listener){
 $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$l.OwningProcess)
 if(-not $p.ExecutablePath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Port 8767 belongs to another application'}
 $live+=@{pid=$p.ProcessId;executable=$p.ExecutablePath;created=$p.CreationDate.ToString('o')}
}
@{tasks=$items;listeners=$live}|ConvertTo-Json -Depth 6 -Compress
""".replace("HOME", quote(HOME))
        .replace("PROGRAM_ROOT", quote(PROGRAMS))
        .replace("SNAPSHOT", quote(snapshot))
    )
    before = json.loads(ps(inspect))
    atomic_json(snapshot / "before.json", before)
    report["initial_state"] = before
    current = asyncio.run(call_running_service(Config.load(HOME), "session_list", {}))
    if before["listeners"]:
        if not current or not current.get("ok"):
            raise RuntimeError(
                "The listener is live but owned session state cannot be verified"
            )
        if any(
            s["state"] in ("starting", "running") for s in current["result"]["sessions"]
        ):
            raise RuntimeError(
                "Active execution sessions exist; deployment will not interrupt them"
            )
    elif current:
        raise RuntimeError("Listener and service record disagree; no switch attempted")
    tunnel_file = HOME / "state/tunnel.json"
    tunnel = (
        json.loads(tunnel_file.read_text("utf-8")) if tunnel_file.exists() else None
    )
    if args.enable_oauth and not tunnel:
        raise RuntimeError(
            "Configure the independent HTTPS tunnel before enabling OAuth"
        )
    switched = False
    try:
        switched = True
        # The new CLI also recognizes a proven dead legacy process record.
        report["stop_https"] = cli(exe, "stop", "--target", "https")
        report["stop_core"] = cli(exe, "stop", "--target", "core")
        switched = True
        ps(
            "if(Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue){throw 'Previous listener has not exited'}"
        )
        if args.enable_oauth:
            issuer = valid_issuer("https://" + tunnel["hostname"])
            cfg = Config.load(HOME)
            text = (HOME / "config.toml").read_text("utf-8-sig")
            if cfg.oauth:
                if not cfg.oauth.get("enabled") or cfg.oauth.get("issuer") != issuer:
                    raise RuntimeError(
                        "Existing OAuth configuration differs; it was not overwritten"
                    )
            else:
                (HOME / "config.toml").write_text(
                    text.rstrip()
                    + "\n\n[oauth]\nenabled=true\nissuer="
                    + json.dumps(issuer)
                    + "\n",
                    "utf-8",
                )
        if args.enable_computer:
            text = (HOME / 'config.toml').read_text('utf-8-sig')
            root, separator, tables = text.partition('[')
            root, count = re.subn(r'(?m)^computer_use_enabled\s*=\s*(?:true|false)\s*(?:#[^\n]*)?$',
                                 'computer_use_enabled = true', root)
            if not count:
                root = 'computer_use_enabled = true\n' + root
            updated = root + separator + tables
            if tomllib.loads(updated) != {**tomllib.loads(text), 'computer_use_enabled': True}:
                raise RuntimeError('Desktop enabling would change unrelated configuration')
            (HOME / 'config.toml').write_text(updated, 'utf-8')
        if browser_proof:
            browser_exe = Path(browser_proof['runtime']['executable_path'])
            if not browser_exe.is_file():
                raise RuntimeError('The verified Tabbit executable is absent')
            profile = Path.home() / '.agentdock/browser/profiles/ccm-tabbit-production'
            text = (HOME / 'config.toml').read_text('utf-8-sig')
            updated = tabbit_config(text, browser_exe, profile)
            (HOME / 'config.toml').write_text(updated, 'utf-8')
            report['browser_backend'] = 'playwright.chromium -> installed Tabbit'
            report['browser_profile'] = str(profile)
            report['browser_acceptance'] = str(args.enable_browser.resolve())
        if args.enable_owner_preapproval:
            text = (HOME / 'config.toml').read_text('utf-8-sig')
            first_table = re.search(r'(?m)^[ \t]*\[', text)
            at = first_table.start() if first_table else len(text)
            root, tables = text[:at], text[at:]
            root, count = re.subn(r'(?m)^auto_approve_application_access[ \t]*=[ \t]*(?:true|false)[ \t]*(?:#[^\n]*)?$',
                                 'auto_approve_application_access = true', root)
            if not count:
                root = 'auto_approve_application_access = true\n' + root
            updated = root + tables
            if tomllib.loads(updated) != {**tomllib.loads(text), 'auto_approve_application_access': True}:
                raise RuntimeError('Owner preapproval would change unrelated configuration')
            (HOME / 'config.toml').write_text(updated, 'utf-8')
            report['application_access_policy'] = 'explicit_owner_preapproval'
            report['application_preapproval_proof'] = str(args.enable_owner_preapproval.resolve())
        cfg = Config.load(HOME)
        report['computer_use_enabled'] = cfg.computer_use_enabled
        report['browser_use_enabled'] = cfg.browser_use_enabled
        script = (
            r"""
$exe=EXE;$homePath=HOME;$work=WORK
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
foreach($name in NAMES){
 $suffix=if($name -eq 'Codex-Control-MCP-OnDemand'){' serve --transport streamable-http'}else{' tunnel'}
 $action=New-ScheduledTaskAction -Execute $exe -Argument ('--home "'+$homePath+'"'+$suffix) -WorkingDirectory $work
 $old=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
 if($old){Set-ScheduledTask -TaskName $name -Action $action | Out-Null}
 else{Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null}
}
Start-ScheduledTask -TaskName 'Codex-Control-MCP-OnDemand'
""".replace("EXE", quote(host_exe))
            .replace("HOME", quote(HOME))
            .replace(
                "WORK",
                quote(
                    next(
                        (
                            t.get("working_directory")
                            for t in before["tasks"]
                            if t.get("working_directory")
                        ),
                        cfg.cwd,
                    )
                ),
            )
            .replace(
                "NAMES",
                "@("
                + ",".join(quote(n) for n in (NAMES if tunnel else NAMES[:1]))
                + ")",
            )
        )
        ps(script)
        deadline = time.monotonic() + 40
        last = None
        while time.monotonic() < deadline:
            try:
                report["doctor"] = asyncio.run(verify_core(version))
                break
            except Exception as exc:
                last = exc
                time.sleep(0.5)
        else:
            raise RuntimeError("Core verification failed: " + type(last).__name__)
        if tunnel:
            ps("Start-ScheduledTask -TaskName 'Codex-Control-MCP-HTTPS-OnDemand'")
            deadline = time.monotonic() + 65
            while time.monotonic() < deadline:
                try:
                    report["https"] = asyncio.run(
                        verify_https(
                            cfg, tunnel["hostname"], bool(cfg.oauth.get("enabled"))
                        )
                    )
                    break
                except Exception as exc:
                    last = exc
                    time.sleep(1)
            else:
                raise RuntimeError(
                    "HTTPS/OAuth verification failed: "
                    + (
                        str(last)
                        if isinstance(last, RuntimeError)
                        else type(last).__name__
                    )
                )
        for name in ["Start-Admin.cmd", "Stop.cmd", "Check.cmd", "Connect-ChatGPT.cmd"]:
            path = ROOT / "scripts" / name
            if path.exists():
                shutil.copyfile(path, destination / name)
        report["executable"] = str(exe)
        report["background_host"] = str(host_exe)
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)[:2500]
        if switched:
            try:
                cli(exe, "stop", "--target", "https")
                cli(exe, "stop", "--target", "core")
                shutil.copyfile(snapshot / "config-before.toml", HOME / "config.toml")
                for t in before["tasks"]:
                    name = t["name"]
                    if t["exists"]:
                        ps(
                            "$xml=Get-Content -LiteralPath "
                            + quote(snapshot / (name + ".xml"))
                            + " -Raw;Register-ScheduledTask -TaskName "
                            + quote(name)
                            + " -Xml $xml -Force | Out-Null"
                        )
                        if t["state"] == "Running":
                            ps("Start-ScheduledTask -TaskName " + quote(name))
                    else:
                        ps(
                            "$t=Get-ScheduledTask -TaskName "
                            + quote(name)
                            + " -ErrorAction SilentlyContinue;if($t -and $t.Actions[0].Execute -eq "
                            + quote(host_exe)
                            + "){Unregister-ScheduledTask -TaskName "
                            + quote(name)
                            + " -Confirm:$false}"
                        )
                report["rollback"] = (
                    "Prior task definitions, prior run states and owner config restored"
                )
            except Exception as rollback:
                report["rollback_error"] = str(rollback)[:1200]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(ROOT / "evidence" / ("installed-" + version + ".json"), report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
