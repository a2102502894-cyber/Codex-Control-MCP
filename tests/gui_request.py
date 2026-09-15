import json, pathlib, sys, time, uuid

root = pathlib.Path(__file__).resolve().parents[1]
current = json.loads((root / "evidence/gui-live-current.json").read_text("utf-8"))
# PowerShell sends UTF-8 through redirected stdin. Python's Windows locale
# decoder can turn these bytes into unpaired surrogate characters.
req = json.loads(sys.stdin.buffer.read().decode('utf-8-sig', errors='strict'))
req["id"] = uuid.uuid4().hex
p = pathlib.Path(current["request"])
temp = p.with_suffix(".new")
temp.write_text(json.dumps(req), encoding="utf-8")
temp.replace(p)
if req["tool"] == "close":
    print("CLOSE_REQUEST_SENT")
    raise SystemExit(0)
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    try:
        result = json.loads(pathlib.Path(current["response"]).read_text("utf-8"))
        if result.get("request_id") == req["id"]:
            print(json.dumps(result, ensure_ascii=True))
            raise SystemExit(0 if result["ok"] else 1)
    except (OSError, ValueError):
        pass
    time.sleep(0.1)
print("RESPONSE_NOT_READY")
raise SystemExit(2)
