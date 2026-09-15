"""Real compatibility switch between two official binaries, not a Desktop update.
The previous official release is a temporary test installation outside the bridge.
"""

from __future__ import annotations
import hashlib, http.server, json, os, pathlib, subprocess, tarfile, threading, time, uuid
import httpx
from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.discovery import Discovery
from codex_control_mcp import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
asset = json.loads(
    (ROOT / "evidence/previous-official-release.json").read_text("utf-8")
)
info = asset["asset"]
expected = info["digest"].split(":", 1)[1]
cache = pathlib.Path(
    os.environ.get(
        "CCM_COMPAT_CACHE",
        str(ROOT.parent / "下载缓存/Codex-Control-MCP-compatibility"),
    )
)
cache.mkdir(parents=True, exist_ok=True)
archive = cache / ("rust-v0.153.3-" + info["name"])
folder = pathlib.Path(
    os.environ.get(
        "CCM_COMPAT_EXTRACT",
        str(ROOT.parent / "临时构建/Codex-Control-MCP-compatibility/rust-v0.153.3"),
    )
)
folder.mkdir(parents=True, exist_ok=True)
record = {
    "started_at": time.time(),
    "scope": "two distinct official binary versions selected in a temporary bridge; not a user Desktop install/update",
    "desktop_unchanged": True,
    "official_runtime_bundled_in_deliverable": False,
}


def shasum(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


try:
    env, _ = build_environment(Config.load())
    if not archive.exists() or shasum(archive) != expected:
        partial = archive.with_suffix(archive.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        with httpx.Client(
            proxy=env.get("HTTPS_PROXY"),
            trust_env=False,
            follow_redirects=True,
            timeout=httpx.Timeout(45, read=60),
        ) as client:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with client.stream(
                "GET", info["browser_download_url"], headers=headers
            ) as r:
                r.raise_for_status()
                if offset and r.status_code != 206:
                    offset = 0
                with partial.open("ab" if offset else "wb") as f:
                    for chunk in r.iter_bytes(1024 * 1024):
                        f.write(chunk)
        if partial.stat().st_size != info["size"] or shasum(partial) != expected:
            raise RuntimeError("Official asset checksum or length mismatch")
        partial.replace(archive)
    if not (folder / "verified-extraction.json").exists():
        with tarfile.open(archive, "r:gz") as tar:
            # Python's data filter refuses absolute/traversal paths and unsafe file types.
            tar.extractall(folder, filter="data")
        (folder / "verified-extraction.json").write_text(
            json.dumps({"asset_sha256": expected}), "utf-8"
        )
    candidates = list(folder.glob("**/codex.exe"))
    if not candidates:
        candidates = list(folder.glob("**/codex-x86_64-pc-windows-msvc.exe"))
    if len(candidates) != 1:
        raise RuntimeError("Official test binary was not uniquely located")
    old = candidates[0]
    current = Discovery(Config.load()).find()
    old_version = (
        subprocess.check_output(
            [str(old), "--version"],
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        .decode()
        .strip()
    )
    if old_version == current.cli_version:
        raise RuntimeError("The test requires two different actual versions")
    run = ROOT / "test-workspace" / ("version-switch-" + uuid.uuid4().hex[:8])
    run.mkdir()
    alias = run / "official-current"
    if old.name != pathlib.Path(current.codex_path).name:
        raise RuntimeError(
            "Stable alias requires matching official executable filenames"
        )

    def point_to(target):
        if alias.exists():
            if not alias.is_junction():
                raise RuntimeError("Refusing to remove a non-junction test path")
            alias.rmdir()
        change = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if (
            change.returncode
            or not alias.is_junction()
            or alias.resolve() != pathlib.Path(target).resolve()
        ):
            raise RuntimeError("Controlled runtime pointer could not be created")

    point_to(old.parent)
    fixed_path = str(alias / old.name)
    cfg = Config(home=run / "home", cwd=str(run), codex_path=fixed_path)
    cfg.initialize_storage()
    attempts = []

    class Sentinel(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            attempts.append({"method": "POST", "path": self.path.split("?")[0]})
            self.send_response(503)
            self.end_headers()

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    guard = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sentinel)
    threading.Thread(target=guard.serve_forever, daemon=True).start()
    (cfg.runtime_home / "config.toml").write_text(
        "\n".join(
            [
                'sandbox_mode="danger-full-access"',
                'approval_policy="never"',
                'model="execution-only-compatibility"',
                'model_provider="execution_test"',
                "[analytics]",
                "enabled=false",
                "[feedback]",
                "enabled=false",
                "[model_providers.execution_test]",
                'name="Compatibility test sentinel"',
                f'base_url="http://127.0.0.1:{guard.server_port}/v1"',
                'wire_api="responses"',
                "requires_openai_auth=false",
                "",
            ]
        ),
        "utf-8",
    )
    b = Bridge(cfg)
    try:
        before = b.execute(
            "exec_command", {"command": "Write-Output 'BEFORE_OFFICIAL_SWITCH_OK'"}
        )
        first = b.runtime.as_dict()
        old_pid = b.rpc.proc.pid
        old_generation = b.rpc.generation
        assert before["ok"] and before["result"]["exit_code"] == 0
        point_to(pathlib.Path(current.codex_path).parent)
        # Allow the real 60-second installation-fingerprint cache to expire; no clock or cache mocking.
        waiting = time.monotonic()
        time.sleep(61)
        b.ensure_ready(force=False)
        assert cfg.codex_path == fixed_path
        record.update(
            {
                "automatic_rediscovery": True,
                "configuration_path_unchanged": True,
                "actual_wait_seconds": round(time.monotonic() - waiting, 3),
                "controlled_junction_switch": True,
            }
        )
        second = b.runtime.as_dict()
        new_pid = b.rpc.proc.pid
        new_generation = b.rpc.generation
        after = b.execute(
            "exec_command", {"command": "Write-Output 'AFTER_OFFICIAL_SWITCH_OK'"}
        )
        proof = run / "switch-proof.txt"
        text = "OFFICIAL_VERSION_SWITCH_中文"
        write = b.execute("file_add", {"path": str(proof), "content": text})
        read = b.execute("read_file", {"path": str(proof)})
        assert (
            after["ok"]
            and after["result"]["stdout"].strip() == "AFTER_OFFICIAL_SWITCH_OK"
        )
        assert write["ok"] and read["ok"] and read["result"]["content"] == text
        assert (
            first["cli_version"] != second["cli_version"]
            and old_generation != new_generation
        )
        record.update(
            {
                "old_runtime": first,
                "new_runtime": second,
                "old_pid": old_pid,
                "new_pid": new_pid,
                "different_owned_generations": True,
                "schema": b.schema_info,
                "before": before,
                "after": after,
                "file_roundtrip_passed": True,
                "archive_sha256": expected,
                "pass": True,
            }
        )
    finally:
        b.close()
        guard.shutdown()
        guard.server_close()
        if alias.is_junction():
            alias.rmdir()
        record["configured_model_endpoint_attempts"] = attempts
        if attempts:
            record["pass"] = False
except BaseException as exc:
    record["pass"] = False
    record["error"] = type(exc).__name__ + ": " + str(exc)[:2500]
record["finished_at"] = time.time()
(
    ROOT
    / "evidence"
    / ("automatic-runtime-switch-v" + __version__.replace(".", "") + ".json")
).write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps(record, ensure_ascii=True, indent=2))
raise SystemExit(0 if record["pass"] else 1)
