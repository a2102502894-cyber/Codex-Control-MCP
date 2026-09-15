import base64, json, os, pathlib, subprocess, sys, time, uuid
from codex_control_mcp.config import Config
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.common import ps_argv, ps_quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
run = ROOT / "evidence" / ("gui-controlled-" + uuid.uuid4().hex[:8])
run.mkdir()
title = "Codex-Control-MCP Test " + uuid.uuid4().hex[:8]
proof = run / "proof.txt"
fixture_log = (run / "fixture.log").open("wb")
fixture = subprocess.Popen(
    [str(ROOT / "test-workspace/CodexControlGuiFixture.exe"), title, str(proof)],
    stdout=fixture_log,
    stderr=fixture_log,
)
b = Bridge(
    Config(
        home=run / "home", cwd=str(ROOT / "test-workspace"), computer_use_enabled=True
    )
)
records = []
print(
    json.dumps(
        {
            "ready": True,
            "fixture_title": title,
            "fixture_pid": fixture.pid,
            "proof_path": str(proof),
        },
        ensure_ascii=True,
    ),
    flush=True,
)
(ROOT / "evidence/gui-console-current.json").write_text(
    json.dumps(
        {
            "run": str(run),
            "title": title,
            "request": str(run / "request.json"),
            "response": str(run / "response.json"),
        }
    ),
    encoding="utf-8",
)


def requests():
    previous = None
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        path = run / "request.json"
        if path.exists():
            try:
                req = json.loads(path.read_text("utf-8-sig"))
            except (ValueError, OSError):
                time.sleep(0.1)
                continue
            if req.get("id") != previous:
                previous = req.get("id")
                yield req
        time.sleep(0.1)


try:
    for req in requests():
        if req.get("tool") == "close":
            break
        result = b.execute(req["tool"], req.get("args", {}))
        images = result.pop("_image_blocks", [])
        saved = []
        for idx, image in enumerate(images):
            ext = ".png" if image["mimeType"] == "image/png" else ".jpg"
            p = run / (str(len(records)) + "-" + str(idx) + ext)
            p.write_bytes(base64.b64decode(image["data"]))
            saved.append(str(p))
        if (
            req["tool"] == "computer_snapshot"
            and not req.get("args", {}).get("window_id")
            and result.get("result")
        ):
            result["result"]["windows"] = [
                w
                for w in result["result"].get("windows", [])
                if w.get("title") == title
            ]
        result["saved_test_screenshots"] = saved
        result["request_id"] = req.get("id")
        records.append({"request": req, "response": result})
        (run / "response.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True), flush=True)
finally:
    b.close()
    fixture.terminate()
    try:
        fixture.wait(5)
    except subprocess.TimeoutExpired:
        fixture.kill()
        fixture.wait(5)
    fixture_log.close()
    report = {
        "fixture_title": title,
        "proof_path": str(proof),
        "proof_content": proof.read_text("utf-8") if proof.exists() else None,
        "owned_fixture_stopped": fixture.poll() is not None,
        "steps": records,
        "model_turn_started": False,
        "full_network_trace": "not_performed",
    }
    (ROOT / "evidence/computer-use-controlled.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "closed": True,
                "proof_content": report["proof_content"],
                "steps": len(records),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
