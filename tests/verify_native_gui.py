"""Bounded GUI acceptance using the owner's actual application decision.
Explicit preauthorization applies only to this disposable fixture, never other apps.
"""

import argparse, base64, json, os, pathlib, subprocess, time, uuid
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.common import ELICITATION_FORWARDER, atomic_json
from codex_control_mcp.consent import native_application_consent
from codex_control_mcp import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--preauthorized-fixture', action='store_true',
                    help='Use the owner\'s explicit prior authorization for this fixture only')
parser.add_argument('--http-home', type=pathlib.Path,
                    help='Exercise the already-running local MCP service for this owner home')
options = parser.parse_args()
fixture_exe = ROOT / "test-workspace/CodexControlGuiFixture.exe"
fixture_exe.parent.mkdir(parents=True, exist_ok=True)
compiler = (
    pathlib.Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
)
subprocess.run(
    [
        str(compiler),
        "/nologo",
        "/target:winexe",
        "/reference:System.Windows.Forms.dll",
        "/reference:System.Drawing.dll",
        "/out:" + str(fixture_exe),
        str(ROOT / "tests/GuiFixture.cs"),
    ],
    check=True,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
TAG = 'v' + __version__.replace('.', '')
RUN = ROOT / "evidence" / ("gui-" + TAG + '-' + uuid.uuid4().hex[:8])
RUN.mkdir()
TITLE = "Codex-Control-MCP Test " + uuid.uuid4().hex[:8]
PROOF = RUN / "proof.txt"
records = []
decisions = []
fixture = subprocess.Popen(
    [str(ROOT / "test-workspace/CodexControlGuiFixture.exe"), TITLE, str(PROOF)]
)
bridge = None
entry = {
    "run": str(RUN),
    "title": TITLE,
    "fixture_pid": fixture.pid,
    "request": str(RUN / "request.json"),
    "response": str(RUN / "response.json"),
}
atomic_json(ROOT / "evidence/gui-live-current.json", entry)


def consent(req):
    meta = req.get("_meta") or req.get("meta") or {}
    app = (meta.get('tool_params') or {}).get('app')
    authorized_fixture = (
        options.preauthorized_fixture
        and meta.get('connector_id') == 'computer-use'
        and req.get('mode', 'form') == 'form'
        and not (req.get('requestedSchema') or {}).get('properties')
        and isinstance(app, str)
        and app.casefold() == fixture_exe.name.casefold()
    )
    if authorized_fixture:
        result = {'action': 'accept', 'content': {}}
        source = 'explicit_user_pre_authorization_for_this_fixture'
    else:
        result = native_application_consent(req, 180)
        source = 'actual_Windows_dialog_result'
    decisions.append(
        {
            "app": app,
            "decision": result["action"],
            "source": source,
            "at": time.time(),
        }
    )
    atomic_json(RUN / "decisions.json", decisions)
    return result


def execute(tool, args, request_id=None):
    token = ELICITATION_FORWARDER.set(consent)
    try:
        out = bridge.execute(tool, args)
    finally:
        ELICITATION_FORWARDER.reset(token)
    paths = []
    for idx, im in enumerate(out.pop("_image_blocks", [])):
        target = RUN / (
            f"{len(records)}-{idx}.png"
            if im["mimeType"] == "image/png"
            else f"{len(records)}-{idx}.jpg"
        )
        target.write_bytes(base64.b64decode(im["data"]))
        paths.append(str(target))
    if tool == "computer_snapshot" and not args.get("window_id") and out.get("result"):
        out["result"]["windows"] = [
            w for w in out["result"].get("windows", []) if w.get("title") == TITLE
        ]
    out["saved_test_screenshots"] = paths
    out["request_id"] = request_id
    records.append({"tool": tool, "args": args, "response": out})
    atomic_json(RUN / "response.json", out)
    print(
        json.dumps(
            {
                "tool": tool,
                "ok": out["ok"],
                "response_path": str(RUN / "response.json"),
                "error": out.get("error"),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    return out


try:
    if options.http_home:
        from gui_http_client import HttpFixtureBridge
        bridge = HttpFixtureBridge(options.http_home, consent)
    else:
        bridge = Bridge(Config(home=RUN / 'home', cwd=str(ROOT / 'test-workspace'), computer_use_enabled=True))
    listed = execute("computer_snapshot", {})
    windows = (listed.get("result") or {}).get("windows", [])
    if len(windows) != 1:
        raise RuntimeError("Owned fixture not uniquely available")
    observed = execute("computer_snapshot", {"window_id": windows[0]["id"]})
    if observed["ok"]:
        previous = None
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            p = RUN / "request.json"
            if p.exists():
                try:
                    req = json.loads(p.read_text("utf-8-sig"))
                except (ValueError, OSError):
                    time.sleep(0.1)
                    continue
                if req.get("id") != previous:
                    previous = req.get("id")
                    if req.get("tool") == "close":
                        break
                    if not req.get("tool", "").startswith("computer_"):
                        raise ValueError("Only GUI fixture tools allowed")
                    execute(req["tool"], req.get("args", {}), req["id"])
            time.sleep(0.15)
except Exception as e:
    print(
        json.dumps({"error": type(e).__name__ + ": " + str(e)}, ensure_ascii=True),
        flush=True,
    )
finally:
    if bridge:
        bridge.close()
    fixture.terminate()
    try:
        fixture.wait(5)
    except subprocess.TimeoutExpired:
        fixture.kill()
        fixture.wait(5)
    report = {
        "finished_at": time.time(),
        "fixture_title": TITLE,
        "decisions": decisions,
        "steps": records,
        "proof_content": PROOF.read_text("utf-8") if PROOF.exists() else None,
        "owned_fixture_stopped": fixture.poll() is not None,
        "model_turn_started": False,
    }
    exercised = {r["tool"] for r in records if r["response"].get("ok")}
    report["bridge_version"] = __version__
    report['acceptance_transport'] = 'streamable_http' if options.http_home else 'direct_bridge'
    report['preauthorized_fixture_requested'] = options.preauthorized_fixture
    report["pass"] = report["proof_content"] == ('CCM_NATIVE_' + __version__ + '_中文') and {
        "computer_snapshot",
        "computer_click",
        "computer_type",
        "computer_press",
        "computer_scroll",
    }.issubset(exercised)
    # A rejected malformed input must not prevent the diagnostic report from
    # being saved. ASCII JSON escaping preserves its exact surrogate values.
    report['failed_attempt_count'] = sum(not r['response']['ok'] for r in records)
    for destination in (RUN / 'acceptance.json', ROOT / 'evidence' / ('gui-' + TAG + '-acceptance.json')):
        temporary = destination.with_suffix('.tmp-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2), 'utf-8')
        temporary.replace(destination)
    print(
        json.dumps(
            {"finished": True, "proof": report["proof_content"]}, ensure_ascii=True
        ),
        flush=True,
    )
