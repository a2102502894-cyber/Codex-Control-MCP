"""Real Windows named-event shutdown of a temporary bridge, not user services."""

import asyncio
import json
import socket
import subprocess
import sys
import time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG


def test_actual_http_service_graceful_stop(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = DEFAULT_CONFIG.replace("8774", str(port))
    (home / "config.toml").write_text(config, "utf-8")
    cfg = Config.load(home)
    cfg.initialize_storage()
    owner_token(cfg, create=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_control_mcp",
            "--home",
            str(home),
            "serve",
            "--transport",
            "streamable-http",
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    async def exercise():
        token = owner_token(cfg)
        async with httpx.AsyncClient(
            trust_env=False, timeout=25, headers={"Authorization": "Bearer " + token}
        ) as client:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=client
            ) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 33
                    execution = await session.call_tool('exec_command', {'command': "Write-Output 'LIFECYCLE_OFFICIAL_OK'"})
                    assert not execution.isError
                    assert execution.structuredContent['result']['stdout'].strip() == 'LIFECYCLE_OFFICIAL_OK'

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode(
                    "utf-8", "replace"
                )
                time.sleep(0.1)
        else:
            raise AssertionError("Temporary service did not start")
        asyncio.run(exercise())
        record = json.loads((home / "state/service.json").read_text("utf-8"))
        assert record["graceful_stop_supported"] and record["pid"] > 0
        output = subprocess.run(
            [sys.executable, "-m", "codex_control_mcp", "--home", str(home), "stop"],
            cwd=tmp_path,
            capture_output=True,
            timeout=55,
        )
        assert output.returncode == 0, output.stdout.decode("utf-8", "replace")
        assert json.loads(output.stdout)["state"] == "stopped"
        assert proc.wait(10) == 0
        assert not (home / "state/service.json").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)


def test_owned_job_really_terminates_child_when_closed():
    import win32api
    from codex_control_mcp.lifecycle import create_owned_process_job

    # Use the actual interpreter, not the venv launcher's extra parent process.
    child = subprocess.Popen(
        [sys._base_executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    job = None
    try:
        job = create_owned_process_job(child._handle)
        time.sleep(0.15)
        assert child.poll() is None
        win32api.CloseHandle(job)
        job = None
        child.wait(5)
        assert child.poll() is not None
    finally:
        if job is not None:
            win32api.CloseHandle(job)
        if child.poll() is None:
            child.terminate()
            child.wait(5)
