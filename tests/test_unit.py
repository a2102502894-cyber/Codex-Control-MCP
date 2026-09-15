import base64, concurrent.futures, json, pathlib
import pytest
from codex_control_mcp.common import InstanceLock, Audit, digest, ps_quote
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.idempotency import Idempotency
from codex_control_mcp.sessions import Session, SessionStore
from codex_control_mcp.schema import SchemaRegistry, ALLOWED_METHODS
from codex_control_mcp.tools import validate_tool, TOOL_SPECS


def test_ps_literal_quote():
    assert ps_quote("a'b") == "'a''b'"


def test_single_owner_lock(tmp_path):
    a = InstanceLock(tmp_path / "owner")
    a.acquire()
    b = InstanceLock(tmp_path / "owner")
    try:
        with pytest.raises(BridgeError, match="Another bridge"):
            b.acquire()
    finally:
        a.close()
    b.acquire()
    b.close()


def test_audit_metadata(tmp_path):
    a = Audit(tmp_path / "audit.jsonl")
    a.emit("rpc_send", method="command/exec", param_keys=["command", "env"])
    assert a.methods["command/exec"] == 1
    assert json.loads((tmp_path / "audit.jsonl").read_text())["event"] == "rpc_send"


def test_idempotency_conflict_and_replay(tmp_path):
    p = tmp_path / "i.sqlite3"
    a = Idempotency(p)
    assert a.reserve("k", "tool", {"x": 1}) is None
    a.finish("k", {"ok": True})
    assert a.reserve("k", "tool", {"x": 1}) == {"ok": True}
    with pytest.raises(BridgeError) as e:
        a.reserve("k", "tool", {"x": 2})
    assert e.value.code == "idempotency_conflict"
    a.close()
    a = Idempotency(p)
    with pytest.raises(BridgeError) as e:
        a.reserve("k", "tool", {"x": 1})
    assert e.value.code == "execution_state_unknown"
    a.close()


def test_session_utf8_split():
    s = Session("x", "g", ".", 4096)
    raw = "中文输入".encode()
    for chunk in (raw[:1], raw[1:5], raw[5:]):
        s.append({"deltaBase64": base64.b64encode(chunk).decode(), "stream": "stdout"})
    assert s.read()["stdout"] == "中文输入"


def test_session_ring_cursor_and_bound():
    s = Session("x", "g", ".", 2048)
    s.append(
        {"deltaBase64": base64.b64encode(b"x" * 8192).decode(), "stream": "stdout"}
    )
    d = s.read(max_bytes=1024)
    assert d["cursor_gap"] and d["output_truncated"]
    assert sum(x["size"] for x in d["chunks"]) <= 1024
    assert s.bytes_cached <= 2048
    assert d["has_more"]
    assert not s.read(d["next_cursor"])["has_more"]


def test_session_finish_and_lost_state(tmp_path):
    store = SessionStore(tmp_path / "sessions.json", 2048, 2)
    s = store.create("g", ".")
    f = concurrent.futures.Future()
    f.set_result({"exitCode": 3})
    s.finish(f)
    assert s.state == "exited" and s.exit_code == 3 and s.finished.is_set()
    s2 = store.create("g", ".")
    store.save()
    again = SessionStore(tmp_path / "sessions.json", 2048, 2)
    assert any(x["session_id"] == s2.id and x["state"] == "lost" for x in again.list())


def test_session_owner_and_limit(tmp_path):
    s = SessionStore(tmp_path / "s.json", 2048, 1)
    a = s.create("g", ".")
    with pytest.raises(BridgeError):
        s.create("g", ".")
    with pytest.raises(BridgeError):
        s.get(a.id, "other")


@pytest.mark.parametrize(
    "method",
    [
        "turn/start",
        "turn/steer",
        "review/start",
        "process/spawn",
        "account/login/start",
        "codex/exec",
    ],
)
def test_inference_and_unreviewed_rpc_absent(method):
    assert method not in ALLOWED_METHODS


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {}),
        ("exec_command", {"shell": "sh"}),
        ("session_read", {"session_id": "x", "cursor": -1}),
        ("git_commit", {"paths": [], "message": "x"}),
        ("codex_health", {"unknown": True}),
        ("turn/start", {}),
    ],
)
def test_tool_validation(tool, args):
    with pytest.raises(BridgeError):
        validate_tool(tool, args)


def test_fixed_schema_valid():
    from jsonschema import Draft7Validator

    for spec in TOOL_SPECS.values():
        Draft7Validator.check_schema(spec["inputSchema"])


def schema_fixture(path, kind="string", response_kind="string"):
    path.mkdir()
    (path / "ClientRequest.json").write_text(
        json.dumps(
            {
                "oneOf": [
                    {
                        "properties": {
                            "method": {"enum": ["command/exec"]},
                            "params": {"$ref": "#/definitions/P"},
                        }
                    }
                ],
                "definitions": {
                    "P": {
                        "type": "object",
                        "properties": {"command": {"type": kind}},
                        "required": ["command"],
                    }
                },
            }
        )
    )
    (path / "CommandExecResponse.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"stdout": {"type": response_kind}},
                "required": ["stdout"],
            }
        )
    )
    return SchemaRegistry(path)


def test_schema_nested_request_change(tmp_path):
    a = schema_fixture(tmp_path / "a")
    b = schema_fixture(tmp_path / "b", "array")
    d = b.compare(a)
    assert "command/exec" in d["known_adapters_retest_required"]
    assert a.hash != b.hash
    with pytest.raises(BridgeError):
        a.validate("turn/start", {})


def test_schema_response_change(tmp_path):
    a = schema_fixture(tmp_path / "a")
    b = schema_fixture(tmp_path / "b", response_kind="integer")
    assert a.hash != b.hash
    assert "command/exec" in b.compare(a)["known_adapters_retest_required"]
    with pytest.raises(BridgeError):
        a.validate_response("command/exec", {"stdout": 1})


def test_schema_no_raw_value_in_error(tmp_path):
    a = schema_fixture(tmp_path / "a")
    with pytest.raises(BridgeError) as e:
        a.validate("command/exec", {"command": {"secret": "NEVER_LOG_ME"}})
    assert "NEVER_LOG_ME" not in str(e.value)
