"""Real lifecycle acceptance for only this project's manually started services.
Refuses to interrupt active execution sessions. On failure, attempts to restore
the same owned services to Running; it never stops processes by image name.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import httpx
from codex_control_mcp import __version__
from codex_control_mcp.client import call_running_service
from codex_control_mcp.common import atomic_json
from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.lifecycle import process_alive

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from public_transport import SafeConnectTransport
CFG = Config.load()
HOME = CFG.home
INSTALL = Path(os.environ["LOCALAPPDATA"]) / "Programs/Codex-Control-MCP" / __version__
EXE = INSTALL / "Codex-Control-MCP.exe"
HOST_EXE = INSTALL / "Codex-Control-MCP-Host.exe"
TASKS = ["Codex-Control-MCP-OnDemand", "Codex-Control-MCP-HTTPS-OnDemand"]


def state():
    return {
        k: json.loads((HOME / "state" / name).read_text("utf-8"))
        for k, name in [("core", "service.json"), ("https", "tunnel-service.json")]
    }


def launcher(name, directory=INSTALL):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(directory / name)],
        cwd=directory,
        capture_output=True,
        timeout=65,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(
            "Owned launcher failed: "
            + name
            + " "
            + result.stdout.decode("utf-8", "replace")[-1000:]
            + result.stderr.decode("utf-8", "replace")[-1600:]
        )
    return result.stdout.decode("utf-8", "replace")


def powershell(script):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
            + script,
        ],
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Task identity inspection failed")
    return json.loads(result.stdout)


async def verify_ready():
    result = await call_running_service(CFG, "codex_health", {"active": True})
    if (
        not result
        or not result.get("ok")
        or result["result"]["bridge_version"] != __version__
    ):
        raise RuntimeError("Current bridge has not become ready")
    env, _ = build_environment(CFG)
    async with httpx.AsyncClient(
        trust_env=False, transport=SafeConnectTransport(proxy=env.get("HTTPS_PROXY")), timeout=10
    ) as http:
        response = await http.get(CFG.oauth["issuer"].rstrip('/') + "/mcp")
        if (
            response.status_code != 401
            or "oauth-protected-resource/mcp"
            not in response.headers.get("www-authenticate", "")
        ):
            raise RuntimeError(
                "Current public authenticated endpoint has not become ready"
            )
    return result


def wait_ready():
    deadline = time.monotonic() + 60
    last = None
    while time.monotonic() < deadline:
        try:
            return asyncio.run(verify_ready())
        except Exception as exc:
            last = type(exc).__name__
            time.sleep(0.6)
    raise RuntimeError("Restart did not recover core and HTTPS: " + str(last))


def main():
    report = {
        "version": __version__,
        "started_at": time.time(),
        "checks": {},
        "scope": "owned manual tasks only",
        "other_services_changed": False,
    }
    need_restore = False
    try:
        before = state()
        report["before"] = before
        observed = asyncio.run(call_running_service(CFG, "session_list", {}))
        if not observed or not observed.get("ok"):
            raise RuntimeError("Unable to verify live execution sessions")
        active = [
            s
            for s in observed["result"]["sessions"]
            if s["state"] in ("starting", "running")
        ]
        if active:
            raise RuntimeError("Active execution sessions exist; no stop was attempted")
        sha = hashlib.sha256(EXE.read_bytes()).hexdigest()
        build = json.loads((ROOT / "evidence/windows-build.json").read_text("utf-8"))
        assert sha == build["sha256"]
        report["binary_sha256"] = sha
        report["checks"]["installed_matches_build"] = True
        tasks = powershell(
            "$a=@();foreach($n in @('Codex-Control-MCP-OnDemand','Codex-Control-MCP-HTTPS-OnDemand')){$t=Get-ScheduledTask -TaskName $n;$a+=@{name=$n;state=$t.State.ToString();execute=$t.Actions[0].Execute;arguments=$t.Actions[0].Arguments;trigger_count=@($t.Triggers|Where-Object{$null -ne $_}).Count;run_level=$t.Principal.RunLevel.ToString()}};ConvertTo-Json -InputObject $a -Depth 5 -Compress"
        )
        assert len(tasks) == 2 and all(
            t["execute"] == str(HOST_EXE)
            and t["state"] == "Running"
            and t["trigger_count"] == 0
            and t["run_level"] == "Highest"
            for t in tasks
        )
        report["tasks"] = tasks
        report["checks"]["owned_tasks_running_no_auto_triggers"] = True
        launcher("Start-Admin.cmd")
        again = state()
        assert all(
            again[k]["instance_id"] == before[k]["instance_id"]
            and again[k]["pid"] == before[k]["pid"]
            for k in ("core", "https")
        )
        report["checks"]["repeat_start_does_not_duplicate"] = True
        if os.environ.get('CCM_DELIVERY_DIR'):
            launcher('Start-Admin.cmd', Path(os.environ['CCM_DELIVERY_DIR']))
            copied = state()
            assert all(copied[k]['instance_id'] == before[k]['instance_id'] for k in ('core', 'https'))
            report['checks']['delivery_folder_launcher_verified_installed_release'] = True
        browser_pids = []
        if CFG.browser_use_enabled and CFG.browser.get('backend') == 'tabbit':
            page = asyncio.run(call_running_service(CFG, 'browser_start', {'browser_id':'tabbit','url':'about:blank'}))
            assert page and page.get('ok'), 'Browser must be live before shutdown verification'
            worker_pid = page['result']['runtime']['worker_pid']
            browser_pids = powershell("$ids=[Collections.Generic.List[int]]::new();$ids.Add(" + str(worker_pid) + ");for($i=0;$i -lt $ids.Count;$i++){Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$ids[$i])|ForEach-Object{$ids.Add([int]$_.ProcessId)}};ConvertTo-Json -Compress -InputObject @($ids)")
            assert len(browser_pids) > 1
            report['owned_browser_pids_before_stop'] = browser_pids
            report['checks']['live_tabbit_tree_before_stop'] = True
        need_restore = True
        launcher("Stop.cmd")
        assert (
            not (HOME / "state/service.json").exists()
            and not (HOME / "state/tunnel-service.json").exists()
        )
        deadline = time.monotonic() + 10
        pids = browser_pids + [
            before["core"]["pid"],
            before["https"]["pid"],
            before["https"]["child_pid"],
        ]
        while time.monotonic() < deadline and any(
            process_alive(pid) is not False for pid in pids
        ):
            time.sleep(0.2)
        assert all(process_alive(pid) is False for pid in pids)
        report["checks"]["graceful_stop_exits_owned_core_tunnel_child"] = True
        if browser_pids:
            report['checks']['graceful_stop_exits_live_tabbit_tree'] = True
        launcher("Start-Admin.cmd")
        report["health"] = wait_ready()
        after = state()
        assert all(
            after[k]["instance_id"] != before[k]["instance_id"]
            for k in ("core", "https")
        )
        assert report["health"]["result"]["child_is_admin_verified"] is True
        report["after"] = after
        report["checks"]["restart_creates_new_owned_instances"] = True
        report["checks"]["restart_admin_official_child"] = True
        report["checks"]["public_oauth_challenge_restored"] = True
        report["pass"] = True
        need_restore = False
    except BaseException as exc:
        report["pass"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1600]}
    finally:
        if need_restore:
            try:
                launcher("Start-Admin.cmd")
                wait_ready()
                report["recovery"] = "owned services restored to Running"
            except Exception as exc:
                report["recovery_error"] = type(exc).__name__ + ": " + str(exc)[:1000]
        report["finished_at"] = time.time()
        atomic_json(
            ROOT
            / "evidence"
            / ("service-lifecycle-v" + __version__.replace(".", "") + ".json"),
            report,
        )
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
